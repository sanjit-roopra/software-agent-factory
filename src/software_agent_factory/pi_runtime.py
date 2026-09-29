"""``pi`` (``@earendil-works/pi-coding-agent``) agent runtime.

:meth:`PiAgentRuntime.run` drives one ``pi --mode rpc`` subprocess per call
(built by ``_build_command``/:func:`~software_agent_factory.agents.workspace_cwd`/
``_child_env_and_scrubbed``, Step 3.2) over
:class:`~software_agent_factory.pi_rpc.PiRpcClient` (Step 3.1): a ``prompt``
command, ``wait_for_settled``, one ``get_messages`` call read for both the
final assistant message's ``stopReason`` (Step 3.4) and its per-model
``usage`` (Step 4.2, mapped by :func:`usage_from_pi_messages` -- the same
``get_messages`` shape ``scripts/performance/pi_cache_probe.py`` already
reads for its cache-reporting verdict), then ``get_last_assistant_text``
parsed with :func:`~software_agent_factory.agent_artifact.parse_agent_artifact`
-- the same runtime-neutral parser the Copilot runtime uses, so a malformed
response fails with byte-identical wording (Step 3.3).

Step 3.4 maps every other pi failure mode to a failed, sanitized
``AgentResult``, mirroring how ``copilot_runtime.py``'s
``_format_failure_reason`` and timeout path do it: an assistant message that
ends ``error`` or ``aborted``, a non-zero exit or EOF before settling
(``PiRpcProcessExited``), an unparsable protocol line
(``PiRpcProtocolError``), a command pi rejected (``PiRpcCommandError``), a
missing executable (``OSError`` starting the process), and a timeout
(``PiRpcTimeout``) -- which sends a best-effort ``abort`` and closes pi's
stdin (the trigger pi actually exits on), waits a 1 second grace period,
then escalates to
:func:`~software_agent_factory.subprocess_utils.kill_process_group` if pi is
still alive. Every failure reason is sanitized with
:func:`~software_agent_factory.subprocess_utils.sanitize_output` against the
credential values :meth:`PiAgentRuntime._child_env_and_scrubbed` collects --
whether it removed them from the child environment or a provider still needs
them and they were merely recorded for redaction -- so no credential can
appear in a failure reason.
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
from typing import TYPE_CHECKING, Any, Callable

from .agent_artifact import build_success_result, parse_agent_artifact
from .agent_capabilities import AgentCapability, capability_for
from .agents import (
    AgentRequest,
    AgentResult,
    AgentRuntime,
    validate_runtime_request,
    workspace_cwd,
)
from .models import ModelUsage, PerformanceRecord, UsageMetrics
from .pi_providers import PI_PROVIDER_CREDENTIAL_ENV_VARS, pi_provider_credential_vars
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
from .subprocess_utils import (
    build_child_env,
    kill_process_group,
    redact_secrets,
    sanitize_output,
)
from .usage_values import non_negative_float, non_negative_int

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

#: Suffix of provider API key variables pi can read beyond the ones
#: :data:`~software_agent_factory.pi_providers.PI_PROVIDER_CREDENTIAL_ENV_VARS`
#: names; all are removed unless they belong to the configured provider.
_API_KEY_SUFFIX = "_API_KEY"

#: Default of ``routing.api_key_env_var`` (:class:`~software_agent_factory.config.RoutingConfig`).
_DEFAULT_ROUTING_API_KEY_ENV_VAR = "JEV_API_KEY"

#: Trailing characters of pi's stderr tail put in a process-exit failure
#: reason. The final lines hold the actual error, and
#: :func:`~software_agent_factory.subprocess_utils.sanitize_output` keeps only
#: the *first* 600 characters, so the reason takes the end of the tail up front.
_STDERR_REASON_CHARS = 500

#: Grace period :meth:`PiAgentRuntime._abort_and_kill` waits for pi to exit on
#: its own after a best-effort ``abort`` command, before escalating to
#: :func:`~software_agent_factory.subprocess_utils.kill_process_group` --
#: matching that function's own default ``grace_seconds`` (build-time
#: decision, ``plans/pi-agent-runtime.md``: "Abort grace before kill: 1
#: second, same as the Copilot runtime's ``_kill_process_group``").
_ABORT_GRACE_SECONDS = 1.0

#: Bound on the best-effort ``get_messages`` call :meth:`PiAgentRuntime.run`
#: makes when a ``PiRpcTimeout``/``PiRpcProcessExited`` struck before the
#: settled call's own ``get_messages`` read ever ran, so a timed-out or
#: exited call still gets one bounded chance to report the usage pi spent
#: before it stopped responding (AC6, build-time decision: "a short deadline
#: (<= 2 s)"). Independent of ``request.timeout_seconds``, which has already
#: elapsed by the time this runs.
_BEST_EFFORT_USAGE_DEADLINE_SECONDS = 2.0

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
        # ``PiRpcClient`` reads the raw fds, so ``communicate()`` (called when
        # pi is killed) may find a UTF-8 character cut in half; strict
        # decoding would raise there and lose the usage read so far.
        errors="replace",
        start_new_session=True,
    )


class _UnexpectedResponse(Exception):
    """Raised when a pi RPC response's ``data`` field is not a mapping.

    ``get_messages``/``get_last_assistant_text`` responses are documented as
    carrying an object ``data``; a response that instead carries a
    non-mapping ``data`` (a string, list, number, ...) is a protocol surprise
    -- not the caller's bug -- so this is a distinct type from
    :class:`~software_agent_factory.pi_rpc.PiRpcError`, mapped to a failed
    ``AgentResult`` rather than an ``AttributeError`` from calling ``.get()``
    on a non-mapping.
    """

    def __init__(self, command: str) -> None:
        super().__init__(f"pi returned an unexpected response for {command}")


def _failure_reason_for(exc: PiRpcError) -> str:
    """Map one non-timeout ``PiRpcError`` to its failure-reason wording.

    ``PiRpcTimeout`` is handled separately in :meth:`PiAgentRuntime.run`
    because, unlike the other ``PiRpcError`` subtypes, it also needs
    :meth:`PiAgentRuntime._abort_and_kill` before building the failure
    result.
    """
    if isinstance(exc, PiRpcProcessExited):
        stderr_end = exc.stderr_tail.strip()[-_STDERR_REASON_CHARS:]
        return f"pi process exited with code {exc.returncode} before settling: {stderr_end}"
    if isinstance(exc, PiRpcProtocolError):
        return f"pi wrote an invalid protocol record: {exc.line_excerpt}"
    if isinstance(exc, PiRpcCommandError):
        return f"pi command {exc.command.get('type')!r} failed: {exc.error}"
    return f"pi RPC error: {exc}"


def _stop_reason_from_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """Return ``(stopReason, errorMessage)`` off the settled call's final message.

    ``messages`` is one ``get_messages`` call's message list; returns the
    ``stopReason``/``errorMessage`` of the last message that carries a
    ``stopReason`` at all (an assistant message; user/tool messages don't).
    ``(None, None)`` when no message carries one, so a successful call falls
    through to ``get_last_assistant_text`` unaffected.
    """
    for message in reversed(messages):
        if isinstance(message, Mapping) and "stopReason" in message:
            stop_reason = message.get("stopReason")
            error_message = message.get("errorMessage")
            return (
                None if stop_reason is None else str(stop_reason),
                None if error_message is None else str(error_message),
            )
    return None, None


def _sum_usage_field(usages: Sequence[Mapping[str, Any]], key: str) -> int | None:
    """Sum ``usages[*][key]`` over the messages that reported it.

    A message that never carries ``key`` contributes nothing -- not a ``0``
    -- so the field stays ``None`` only when *no* message in ``usages``
    reported it at all; a message reporting ``0`` still makes the field
    "known" (spec: "A field pi did not report stays ``None``, never zero.").
    """
    reported = [
        count for usage in usages if (count := non_negative_int(usage.get(key))) is not None
    ]
    if not reported:
        return None
    return sum(reported)


def _sum_cache_write_field(usages: Sequence[Mapping[str, Any]]) -> int | None:
    """Sum ``cacheWrite`` plus ``cacheWrite1h`` (when present) over ``usages``.

    Per the "Usage mapping" table: ``cacheWrite`` (+ ``cacheWrite1h``) ->
    ``cache_write_tokens``. Either key reported on a message is enough to
    make the field "known"; a message reporting neither contributes nothing.
    """
    total = 0
    reported = False
    for usage in usages:
        for key in ("cacheWrite", "cacheWrite1h"):
            count = non_negative_int(usage.get(key))
            if count is not None:
                total += count
                reported = True
    return total if reported else None


def _sum_cost_field(usages: Sequence[Mapping[str, Any]]) -> float | None:
    """Sum ``cost.total`` over ``usages`` -- pi's own list-price estimate."""
    reported = []
    for usage in usages:
        cost = usage.get("cost")
        if isinstance(cost, Mapping):
            total = non_negative_float(cost.get("total"))
            if total is not None:
                reported.append(total)
    if not reported:
        return None
    return sum(reported)


def _model_usage(model: str, usages: Sequence[Mapping[str, Any]]) -> ModelUsage:
    """Build one model's :class:`ModelUsage` entry from its assistant messages' usage."""
    return ModelUsage(
        model=model,
        requests=len(usages),
        input_tokens=_sum_usage_field(usages, "input"),
        output_tokens=_sum_usage_field(usages, "output"),
        reasoning_tokens=_sum_usage_field(usages, "reasoning"),
        cache_read_tokens=_sum_usage_field(usages, "cacheRead"),
        cache_write_tokens=_sum_cache_write_field(usages),
        list_price_estimate_usd=_sum_cost_field(usages),
    )


def usage_from_pi_messages(messages: list[dict[str, Any]]) -> UsageMetrics:
    """Map one call's ``get_messages`` records to :class:`UsageMetrics`.

    An assistant message is one whose ``usage`` field is itself a mapping --
    the same rule ``pi_cache_probe.verdict_from_messages`` uses to find
    reportable usage. Each field is summed per the "Usage mapping" table in
    ``docs/specs/pi-agent-runtime.md`` (see :func:`_sum_usage_field`,
    :func:`_sum_cache_write_field`, :func:`_sum_cost_field`); a field no
    message reported stays ``None``, never ``0``.
    :attr:`ModelUsage.model`/:attr:`UsageMetrics.model_usage` group messages
    by their own ``model`` field, per the spec's "summed per model"; pi
    always reports it in practice, so a message without one contributes to
    the aggregate totals but not to any ``model_usage`` entry (there is no
    request-supplied fallback in this pure function's signature).
    ``current_model`` is the *last* assistant message's own ``model`` field,
    ``None`` when that specific message did not report one -- even if an
    earlier assistant message did.
    ``premium_request_cost``/``total_nano_aiu`` are Copilot-only units and
    stay ``None`` on every :class:`ModelUsage` this builds. No assistant
    messages yields ``requests=0`` and every token field ``None`` -- a call
    that produced no assistant messages is a fact worth recording, not an
    unknown. Callers that must never propagate a ``ValueError``/
    :class:`~pydantic.ValidationError` from a still-malformed record (e.g.
    one whose numeric fields overflow past what
    :func:`~software_agent_factory.usage_values.non_negative_int` filters)
    should call :func:`_safe_usage_from_pi_messages` instead.
    """
    assistant_messages = [
        message
        for message in messages
        if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping)
    ]

    usages_by_model: dict[str, list[Mapping[str, Any]]] = {}
    model_order: list[str] = []
    for message in assistant_messages:
        model = message.get("model")
        if not isinstance(model, str) or not model:
            continue
        if model not in usages_by_model:
            usages_by_model[model] = []
            model_order.append(model)
        usages_by_model[model].append(message["usage"])

    current_model: str | None = None
    if assistant_messages:
        last_model = assistant_messages[-1].get("model")
        current_model = last_model if isinstance(last_model, str) and last_model else None

    all_usages = [message["usage"] for message in assistant_messages]
    model_usage = tuple(_model_usage(model, usages_by_model[model]) for model in model_order)

    return UsageMetrics(
        current_model=current_model,
        total_user_requests=len(assistant_messages),
        input_tokens=_sum_usage_field(all_usages, "input"),
        output_tokens=_sum_usage_field(all_usages, "output"),
        reasoning_tokens=_sum_usage_field(all_usages, "reasoning"),
        cache_read_tokens=_sum_usage_field(all_usages, "cacheRead"),
        cache_write_tokens=_sum_cache_write_field(all_usages),
        list_price_estimate_usd=_sum_cost_field(all_usages),
        model_usage=model_usage,
    )


def _safe_usage_from_pi_messages(messages: list[dict[str, Any]]) -> UsageMetrics | None:
    """:func:`usage_from_pi_messages`, treating a malformed record as unknown usage.

    :func:`~software_agent_factory.usage_values.non_negative_int` and
    :func:`~software_agent_factory.usage_values.non_negative_float` already
    filter out non-finite/negative/fractional-count numeric fields
    before they ever reach a :class:`~software_agent_factory.models.UsageMetrics`/
    :class:`~software_agent_factory.models.ModelUsage` validator, but this is
    a second, defense-in-depth layer: any ``ValueError`` a still-invalid
    record trips there (:class:`pydantic.ValidationError` is itself a
    ``ValueError`` subclass) is caught here and logged at debug, rather than
    escaping :meth:`PiAgentRuntime.run` -- which, on the timeout path, would
    skip the ``self._abort_and_kill(...)`` call that must still run.
    """
    try:
        return usage_from_pi_messages(messages)
    except ValueError:
        logger.debug("pi reported malformed usage data; treating usage as unknown", exc_info=True)
        return None


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
        routing_api_key_env_var: str = _DEFAULT_ROUTING_API_KEY_ENV_VAR,
    ) -> None:
        self._config = config
        self._data_dir = data_dir
        self._process_factory = process_factory
        self._routing_api_key_env_var = routing_api_key_env_var

    def run(self, request: AgentRequest) -> AgentResult:
        validate_runtime_request(request)
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

        client = PiRpcClient(process, redact=lambda text: redact_secrets(text, scrubbed_values))
        usage: UsageMetrics | None = None
        try:
            deadline = time.monotonic() + request.timeout_seconds
            try:
                client.request({"type": "prompt", "message": prompt}, deadline=deadline)
                client.wait_for_settled(deadline=deadline)
                messages = self._get_messages(client, deadline=deadline)
                usage = _safe_usage_from_pi_messages(messages)
                stop_reason, error_message = _stop_reason_from_messages(messages)
                if stop_reason == "error":
                    return self._failed(
                        request,
                        prompt_chars=prompt_chars,
                        boot_ms=boot_ms,
                        scrubbed_values=scrubbed_values,
                        message=f"pi assistant error: {error_message or '(no error message)'}",
                        usage=usage,
                    )
                if stop_reason == "aborted":
                    return self._failed(
                        request,
                        prompt_chars=prompt_chars,
                        boot_ms=boot_ms,
                        scrubbed_values=scrubbed_values,
                        message="pi assistant call was aborted",
                        usage=usage,
                    )
                response = client.request({"type": "get_last_assistant_text"}, deadline=deadline)
            except PiRpcTimeout:
                if usage is None:
                    usage = self._best_effort_usage(client)
                self._abort_and_kill(client, process)
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=f"pi timed out after {request.timeout_seconds} seconds",
                    usage=usage,
                )
            except PiRpcProcessExited as exc:
                if usage is None:
                    usage = self._best_effort_usage(client)
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=_failure_reason_for(exc),
                    usage=usage,
                )
            except PiRpcError as exc:
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=_failure_reason_for(exc),
                    usage=usage,
                )
            except _UnexpectedResponse as exc:
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=str(exc),
                    usage=usage,
                )

            data = response.get("data")
            if data is not None and not isinstance(data, Mapping):
                return self._failed(
                    request,
                    prompt_chars=prompt_chars,
                    boot_ms=boot_ms,
                    scrubbed_values=scrubbed_values,
                    message=str(_UnexpectedResponse("get_last_assistant_text")),
                    usage=usage,
                )
            data = data or {}
            text = str(data.get("text") or "")

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
                    usage=usage,
                )

            return build_success_result(
                request.role,
                purpose=request.purpose,
                artifact=artifact,
                performance=performance,
                usage=usage,
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

    def _child_env_and_scrubbed(self) -> tuple[dict[str, str], set[str]]:
        """Build the scrubbed environment pi's child process runs in.

        ``build_child_env`` scrubs :data:`subprocess_utils.GITHUB_CREDENTIAL_ENV_VARS`.
        On top of that, only the credential variables of ``self._config.provider``
        (:func:`~software_agent_factory.pi_providers.pi_provider_credential_vars`)
        stay: ``COPILOT_GITHUB_TOKEN`` for ``github-copilot`` (it authenticates
        headless when ``~/.pi/agent/auth.json`` has no interactive login), the
        API key, OAuth token or AWS variables of another provider, the derived
        ``<PROVIDER>_API_KEY`` for a provider the map does not know. Every
        other known credential variable, every other ``*_API_KEY`` and the
        routing API key (pi never needs it) are removed.

        The second return value is the credential values to redact from any
        failure reason built from pi's stderr or protocol output (mirroring
        ``copilot_runtime.py``'s ``_format_failure_reason``). It holds the
        values ``build_child_env`` removed plus *every* known credential
        value, kept or removed, so none can leak into a failure reason.
        """
        env, scrubbed_values = build_child_env()
        env["PI_CACHE_RETENTION"] = self._config.cache_retention

        own_vars = set(pi_provider_credential_vars(self._config.provider))
        # pi reads many more provider keys than the map names (XAI_API_KEY,
        # MISTRAL_API_KEY, ...), so every *_API_KEY variable is treated as one.
        other_api_keys = [name for name in env if name.endswith(_API_KEY_SUFFIX)]
        credential_vars = {
            *(name for names in PI_PROVIDER_CREDENTIAL_ENV_VARS.values() for name in names),
            *own_vars,
            self._routing_api_key_env_var,
            *other_api_keys,
        }
        for name in sorted(credential_vars):
            value = env.get(name)
            if not value:
                continue
            scrubbed_values.add(value)
            if name not in own_vars:
                del env[name]

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
        usage: UsageMetrics | None = None,
    ) -> AgentResult:
        """Build a failed, sanitized ``AgentResult`` for one pi failure mode.

        ``message`` is sanitized with
        :func:`~software_agent_factory.subprocess_utils.sanitize_output`
        against ``scrubbed_values`` so a credential embedded in pi's stderr,
        an error message, or a protocol excerpt can never reach the stored
        ``failure_reason``. ``response_chars`` defaults to ``0`` (no assistant
        text was produced); the malformed-artifact path passes the actual
        length of the text pi did produce. ``usage`` defaults to ``None``
        (nothing reported yet); a failure that struck after a successful
        ``get_messages`` read, or after :meth:`_best_effort_usage`, passes
        the usage recorded so far (AC6).
        """
        return AgentResult(
            role=request.role,
            success=False,
            failure_reason=sanitize_output(message, scrubbed_values),
            usage=usage,
            performance=PerformanceRecord(
                prompt_chars=prompt_chars,
                response_chars=response_chars,
                process_boot_ms=boot_ms,
            ),
        )

    def _get_messages(self, client: PiRpcClient, *, deadline: float) -> list[dict[str, Any]]:
        """Read the settled call's full message list via ``get_messages``.

        The same command ``scripts/performance/pi_cache_probe.py`` already
        uses to read per-message ``usage``. :meth:`run` reads this once per
        call and derives both the final assistant message's ``stopReason``
        (:func:`_stop_reason_from_messages`) and its usage
        (:func:`usage_from_pi_messages`) from the same result, rather than
        issuing two RPC round trips for one already-settled conversation.

        Raises :class:`_UnexpectedResponse` when the response's ``data`` is
        present but not a mapping, instead of letting a bare ``.get()`` call
        raise ``AttributeError``.
        """
        response = client.request({"type": "get_messages"}, deadline=deadline)
        data = response.get("data")
        if data is not None and not isinstance(data, Mapping):
            raise _UnexpectedResponse("get_messages")
        data = data or {}
        messages = data.get("messages") or []
        return [message for message in messages if isinstance(message, dict)]

    def _best_effort_usage(self, client: PiRpcClient) -> UsageMetrics | None:
        """Best-effort ``get_messages`` after pi failed to settle in time.

        Only called when :meth:`run` never reached its own ``get_messages``
        read (a ``PiRpcTimeout``/``PiRpcProcessExited`` struck before or
        during ``wait_for_settled``) -- so there is a real chance pi still
        has something to report, and a real chance it does not. Uses its own
        :data:`_BEST_EFFORT_USAGE_DEADLINE_SECONDS` deadline, independent of
        the request's own (already-elapsed) deadline, so a call that already
        timed out still gets one bounded chance to report the tokens pi
        spent before it stopped responding (AC6). Any further failure (the
        process is unresponsive, already gone, answers something else, or
        answers with a non-mapping ``data`` (:class:`_UnexpectedResponse`) is
        swallowed and reported as ``None`` usage -- this is a best-effort
        nicety, never a reason to fail differently or block longer.
        """
        try:
            messages = self._get_messages(
                client, deadline=time.monotonic() + _BEST_EFFORT_USAGE_DEADLINE_SECONDS
            )
        except (PiRpcError, _UnexpectedResponse):
            return None
        return _safe_usage_from_pi_messages(messages)

    def _abort_and_kill(self, client: PiRpcClient, process: PiProcessHandle) -> None:
        """Best-effort abort, close stdin, then escalate to killing pi's process group.

        pi exits when its stdin reaches EOF -- not necessarily in direct
        response to the RPC ``abort`` command itself, which is why this both
        sends a best-effort ``{"type": "abort"}`` (pi may already be wedged,
        so a failed send -- ``PiRpcError``, e.g. a broken pipe -- is not
        itself an error here) and closes pi's stdin, the actual shutdown
        trigger. Gives pi :data:`_ABORT_GRACE_SECONDS` to exit on its own
        after that before escalating to
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
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=_ABORT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                kill_process_group(process)
            except UnicodeDecodeError:
                # Only the read of pi's leftover output failed; pi is already
                # signalled, and a failed cleanup read must not replace the
                # failed result (and its partial usage) this timeout returns.
                pass
