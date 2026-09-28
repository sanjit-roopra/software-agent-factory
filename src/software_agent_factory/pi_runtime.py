"""``pi`` (``@earendil-works/pi-coding-agent``) agent runtime.

:meth:`PiAgentRuntime.run` drives one ``pi --mode rpc`` subprocess per call
(built by ``_build_command``/:func:`~software_agent_factory.agents.workspace_cwd`/
``_child_env``, Step 3.2) over
:class:`~software_agent_factory.pi_rpc.PiRpcClient` (Step 3.1): a ``prompt``
command, ``wait_for_settled``, a ``get_messages`` check of the final
assistant message's ``stopReason`` (Step 3.4 -- the same ``get_messages``
call ``scripts/performance/pi_cache_probe.py`` already uses to read
per-message usage), then ``get_last_assistant_text`` parsed with
:func:`~software_agent_factory.agent_artifact.parse_agent_artifact` -- the
same runtime-neutral parser the Copilot runtime uses, so a malformed
response fails with byte-identical wording (Step 3.3).

Step 3.4 maps every other pi failure mode to a failed, sanitized
``AgentResult``, mirroring how ``copilot_runtime.py``'s
``_format_failure_reason`` and timeout path do it: an assistant message that
ends ``error`` or ``aborted``, a non-zero exit or EOF before settling
(``PiRpcProcessExited``), an unparsable protocol line
(``PiRpcProtocolError``), a command pi rejected (``PiRpcCommandError``), a
missing executable (``OSError`` starting the process), and a timeout
(``PiRpcTimeout``) -- which sends a best-effort ``abort``, waits a 1 second
grace period, then escalates to
:func:`~software_agent_factory.subprocess_utils.kill_process_group` if pi is
still alive. Every failure reason is sanitized with
:func:`~software_agent_factory.subprocess_utils.sanitize_output` against the
credential values :meth:`PiAgentRuntime._child_env_and_scrubbed` removed
from the child environment, so no credential can appear in a failure reason.
Unexpected exceptions from starting or driving the process still propagate
to the caller, exactly as they do from the Copilot runtime (the
workflow/project/CLI layers convert them with
``agents.runtime_exception_failure_reason``).
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .agent_artifact import build_success_result, parse_agent_artifact
from .agent_capabilities import AgentCapability, capability_for
from .agents import AgentRequest, AgentResult, AgentRuntime, workspace_cwd
from .models import PerformanceRecord
from .pi_rpc import (
    PiProcessHandle,
    PiRpcClient,
    PiRpcCommandError,
    PiRpcError,
    PiRpcProcessExited,
    PiRpcProtocolError,
    PiRpcTimeout,
)
from .prompts import build_prompt
from .subprocess_utils import build_child_env, kill_process_group, sanitize_output

if TYPE_CHECKING:
    from .config import PiConfig

logger = logging.getLogger(__name__)

#: Pi ``--tools`` value per :class:`AgentCapability`, per the "Role tool
#: allowlists (v1)" table in ``docs/specs/pi-agent-runtime.md``.
#: ``AgentCapability.WEB_RESEARCH`` has no entry: pi has no web-fetch tool,
#: so :meth:`PiAgentRuntime._build_command` raises before this lookup runs.
_TOOL_ARGS: dict[AgentCapability, tuple[str, ...]] = {
    AgentCapability.IMPLEMENTER_WRITE: ("--tools", "read,bash,edit,write,grep,find,ls"),
    AgentCapability.READ_ONLY: ("--tools", "read,grep,find,ls"),
    AgentCapability.NO_TOOLS: ("--no-tools",),
}

#: Grace period :meth:`PiAgentRuntime._abort_and_kill` waits for pi to exit on
#: its own after a best-effort ``abort`` command, before escalating to
#: :func:`~software_agent_factory.subprocess_utils.kill_process_group` --
#: matching that function's own default ``grace_seconds`` (build-time
#: decision, ``plans/pi-agent-runtime.md``: "Abort grace before kill: 1
#: second, same as the Copilot runtime's ``_kill_process_group``").
_ABORT_GRACE_SECONDS = 1.0

#: Starts one already-configured ``pi`` subprocess: ``(command, cwd, env) ->
#: PiProcessHandle``. Injectable so tests drive :meth:`PiAgentRuntime.run`
#: against a fake process instead of a real ``pi`` executable.
ProcessFactory = Callable[[Sequence[str], Path, dict[str, str]], PiProcessHandle]


def _default_process_factory(
    command: Sequence[str], cwd: Path, env: dict[str, str]
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _failure_reason_for(exc: PiRpcError) -> str:
    """Map one non-timeout ``PiRpcError`` to its failure-reason wording.

    ``PiRpcTimeout`` is handled separately in :meth:`PiAgentRuntime.run`
    because, unlike the other ``PiRpcError`` subtypes, it also needs
    :meth:`PiAgentRuntime._abort_and_kill` before building the failure
    result.
    """
    if isinstance(exc, PiRpcProcessExited):
        return f"pi process exited with code {exc.returncode} before settling: {exc.stderr_tail}"
    if isinstance(exc, PiRpcProtocolError):
        return f"pi wrote an invalid protocol record: {exc.line_excerpt}"
    if isinstance(exc, PiRpcCommandError):
        return f"pi command {exc.command.get('type')!r} failed: {exc.error}"
    return f"pi RPC error: {exc}"


class PiAgentRuntime(AgentRuntime):
    """Production ``AgentRuntime`` backed by the ``pi`` CLI.

    ``config`` is the loaded ``pi:`` configuration block
    (:class:`~software_agent_factory.config.PiConfig`); ``data_dir`` is the
    factory's configured data directory, used by later slices for the
    per-work-item session store under ``<data_dir>/pi-sessions``.
    """

    def __init__(
        self,
        config: PiConfig,
        data_dir: Path,
        *,
        process_factory: ProcessFactory = _default_process_factory,
    ) -> None:
        self._config = config
        self._data_dir = data_dir
        self._process_factory = process_factory

    def run(self, request: AgentRequest) -> AgentResult:
        command = self._build_command(request, session_arg=["--no-session"])
        cwd = workspace_cwd(request)
        env, scrubbed_values = self._child_env_and_scrubbed()
        prompt = build_prompt(request)
        prompt_chars = len(prompt)

        boot_start = time.perf_counter()
        try:
            process = self._process_factory(command, cwd, env)
        except OSError as exc:
            boot_ms = (time.perf_counter() - boot_start) * 1000.0
            return self._failed(
                request,
                prompt_chars=prompt_chars,
                boot_ms=boot_ms,
                scrubbed_values=scrubbed_values,
                message=(
                    f"pi could not be started ({type(exc).__name__}): {self._config.executable}"
                ),
            )
        boot_ms = (time.perf_counter() - boot_start) * 1000.0

        client = PiRpcClient(process)
        try:
            deadline = time.monotonic() + request.timeout_seconds
            try:
                client.request({"type": "prompt", "message": prompt}, deadline=deadline)
                client.wait_for_settled(deadline=deadline)
                stop_reason, error_message = self._final_assistant_stop_reason(
                    client, deadline=deadline
                )
                if stop_reason == "error":
                    return self._failed(
                        request,
                        prompt_chars=prompt_chars,
                        boot_ms=boot_ms,
                        scrubbed_values=scrubbed_values,
                        message=f"pi assistant error: {error_message}",
                    )
                if stop_reason == "aborted":
                    return self._failed(
                        request,
                        prompt_chars=prompt_chars,
                        boot_ms=boot_ms,
                        scrubbed_values=scrubbed_values,
                        message="pi assistant call was aborted",
                    )
                response = client.request({"type": "get_last_assistant_text"}, deadline=deadline)
            except PiRpcTimeout:
                self._abort_and_kill(client, process)
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=f"pi timed out after {request.timeout_seconds} seconds",
                )
            except PiRpcError as exc:
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=_failure_reason_for(exc),
                )

            data = response.get("data") or {}
            text = str(data.get("text", ""))

            performance = PerformanceRecord(
                prompt_chars=prompt_chars,
                response_chars=len(text),
                process_boot_ms=boot_ms,
            )

            try:
                artifact = parse_agent_artifact(request.role, text=text, purpose=request.purpose)
            except ValueError as exc:
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=str(exc),
                    response_chars=len(text),
                )

            return build_success_result(
                request.role,
                purpose=request.purpose,
                artifact=artifact,
                performance=performance,
            )
        finally:
            client.close()

    def _build_command(
        self,
        request: AgentRequest,
        *,
        session_arg: Sequence[str],
    ) -> list[str]:
        """Build the ``pi --mode rpc`` command line for one agent call.

        ``session_arg`` is ``["--no-session"]`` or ``["--session", <path>]``;
        Slice 6 supplies the persisted-session path, so every caller in this
        slice passes ``["--no-session"]``.
        """
        capability = capability_for(request)
        if capability is AgentCapability.WEB_RESEARCH:
            raise ValueError("repository skill generation is not supported on pi")
        logger.debug(
            "ignoring context_tier=%s for pi request (no --context equivalent)",
            request.context_tier,
        )
        return [
            self._config.executable,
            "--mode",
            "rpc",
            "--provider",
            self._config.provider,
            "--model",
            request.model,
            "--thinking",
            request.reasoning,
            *_TOOL_ARGS[capability],
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            *session_arg,
        ]

    def _child_env(self) -> dict[str, str]:
        """Build the scrubbed environment pi's child process runs in.

        ``build_child_env`` scrubs :data:`subprocess_utils.GITHUB_CREDENTIAL_ENV_VARS`;
        ``COPILOT_GITHUB_TOKEN`` is not in that set, so it survives when set --
        pi's ``github-copilot`` provider uses ``~/.pi/agent/auth.json`` for an
        interactive login, but authenticates headless from that variable when
        present.
        """
        env, _scrubbed_values = self._child_env_and_scrubbed()
        return env

    def _child_env_and_scrubbed(self) -> tuple[dict[str, str], set[str]]:
        """Same as :meth:`_child_env`, plus the credential values it scrubbed.

        :meth:`run` needs the scrubbed values too, to sanitize them out of any
        failure reason built from pi's stderr or protocol output (mirroring
        ``copilot_runtime.py``'s ``_format_failure_reason``, which sanitizes
        against ``build_child_env``'s ``scrubbed_values`` the same way).
        """
        env, scrubbed_values = build_child_env()
        env["PI_CACHE_RETENTION"] = self._config.cache_retention
        return env, scrubbed_values

    def _failed(
        self,
        request: AgentRequest,
        *,
        prompt_chars: int,
        boot_ms: float,
        scrubbed_values: set[str],
        message: str,
        response_chars: int = 0,
    ) -> AgentResult:
        """Build a failed, sanitized ``AgentResult`` for one pi failure mode.

        ``message`` is sanitized with
        :func:`~software_agent_factory.subprocess_utils.sanitize_output`
        against ``scrubbed_values`` so a credential embedded in pi's stderr,
        an error message, or a protocol excerpt can never reach the stored
        ``failure_reason``. ``response_chars`` defaults to ``0`` (no assistant
        text was produced); the malformed-artifact path passes the actual
        length of the text pi did produce.
        """
        return AgentResult(
            role=request.role,
            success=False,
            failure_reason=sanitize_output(message, scrubbed_values),
            performance=PerformanceRecord(
                prompt_chars=prompt_chars,
                response_chars=response_chars,
                process_boot_ms=boot_ms,
            ),
        )

    def _final_assistant_stop_reason(
        self, client: PiRpcClient, *, deadline: float
    ) -> tuple[str | None, str | None]:
        """Return ``(stopReason, errorMessage)`` off the settled call's final message.

        Reads ``get_messages`` -- the same command
        ``scripts/performance/pi_cache_probe.py`` already uses to read
        per-message ``usage`` -- and returns the ``stopReason``/``errorMessage``
        of the last message that carries a ``stopReason`` at all (an assistant
        message; user/tool messages don't). ``(None, None)`` when no message
        carries one, so a successful call falls through to
        ``get_last_assistant_text`` unaffected.
        """
        response = client.request({"type": "get_messages"}, deadline=deadline)
        data = response.get("data") or {}
        messages = data.get("messages") or []
        for message in reversed(messages):
            if isinstance(message, Mapping) and "stopReason" in message:
                stop_reason = message.get("stopReason")
                error_message = message.get("errorMessage")
                return (
                    None if stop_reason is None else str(stop_reason),
                    None if error_message is None else str(error_message),
                )
        return None, None

    def _abort_and_kill(self, client: PiRpcClient, process: PiProcessHandle) -> None:
        """Best-effort abort, then escalate to killing pi's process group.

        Sends ``{"type": "abort"}`` without waiting for a response -- pi may
        already be wedged, so a failed send (``PiRpcError``, e.g. a broken
        pipe) is not itself an error here. Gives pi
        :data:`_ABORT_GRACE_SECONDS` to exit on its own before escalating to
        :func:`~software_agent_factory.subprocess_utils.kill_process_group`.
        """
        # Unlike the Copilot runtime's timeout path (which has no RPC channel
        # and goes straight to kill_process_group), pi is driven over an RPC
        # connection it can still hear on even mid-timeout, so we ask it to
        # abort cleanly first and only escalate to a hard kill if it doesn't.
        try:
            client.send({"type": "abort"})
        except PiRpcError:
            pass
        try:
            process.wait(timeout=_ABORT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            kill_process_group(process)
