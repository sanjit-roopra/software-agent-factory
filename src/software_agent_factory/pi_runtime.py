"""``pi`` (``@earendil-works/pi-coding-agent``) agent runtime.

Step 3.3 of ``plans/pi-agent-runtime.md`` wires :meth:`PiAgentRuntime.run` to
a real ``pi --mode rpc`` subprocess: it starts one process per call (built by
``_build_command``/``_cwd_for``/``_child_env``, added in Step 3.2), drives it
over :class:`~software_agent_factory.pi_rpc.PiRpcClient` (Step 3.1) with a
``prompt`` command followed by ``wait_for_settled`` and
``get_last_assistant_text``, then parses the final assistant text with
:func:`~software_agent_factory.agent_artifact.parse_agent_artifact` -- the
same runtime-neutral parser the Copilot runtime uses, so a malformed
response fails with byte-identical wording. Failure and timeout handling
beyond a parse failure (assistant error/abort, non-zero exit, timeout) is
Step 3.4; unexpected exceptions from starting or driving the process
propagate to the caller today, exactly as they do from the Copilot runtime
(the workflow/project/CLI layers convert them with
``agents.runtime_exception_failure_reason``).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .agent_artifact import build_success_result, parse_agent_artifact
from .agent_capabilities import AgentCapability, capability_for
from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import AgentPurpose, PerformanceRecord
from .pi_rpc import PiProcessHandle, PiRpcClient
from .prompts import build_prompt
from .subprocess_utils import build_child_env

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
        cwd = self._cwd_for(request)
        env = self._child_env()
        prompt = build_prompt(request)
        prompt_chars = len(prompt)

        boot_start = time.perf_counter()
        process = self._process_factory(command, cwd, env)
        boot_ms = (time.perf_counter() - boot_start) * 1000.0

        client = PiRpcClient(process)
        try:
            deadline = time.monotonic() + request.timeout_seconds
            client.request({"type": "prompt", "message": prompt}, deadline=deadline)
            client.wait_for_settled(deadline=deadline)
            response = client.request({"type": "get_last_assistant_text"}, deadline=deadline)
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
                return AgentResult(
                    role=request.role,
                    success=False,
                    failure_reason=str(exc),
                    performance=performance,
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

    def _cwd_for(self, request: AgentRequest) -> Path:
        """Resolve the working directory for one pi call.

        Same rule as the Copilot runtime's ``_cwd_for``
        (``copilot_runtime.py``): the request's workspace when supplied,
        otherwise the process cwd -- except ``CORRECT_CHANGE_SET`` and
        ``GENERATE_REPOSITORY_SKILL`` always require an explicit workspace.
        """
        if request.workspace_path:
            return Path(request.workspace_path).expanduser().resolve()
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            raise ValueError("ChangeSet correction requires workspace_path")
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            raise ValueError(
                "repository skill generation requires workspace_path (the neutral run directory)"
            )
        return Path(os.getcwd()).expanduser().resolve()

    def _child_env(self) -> dict[str, str]:
        """Build the scrubbed environment pi's child process runs in.

        ``build_child_env`` scrubs :data:`subprocess_utils.GITHUB_CREDENTIAL_ENV_VARS`;
        ``COPILOT_GITHUB_TOKEN`` is not in that set, so it survives when set --
        pi's ``github-copilot`` provider uses ``~/.pi/agent/auth.json`` for an
        interactive login, but authenticates headless from that variable when
        present.
        """
        env, _scrubbed_values = build_child_env()
        env["PI_CACHE_RETENTION"] = self._config.cache_retention
        return env
