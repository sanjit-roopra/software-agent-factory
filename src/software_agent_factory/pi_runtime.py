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

Session continuation (Slice 6): the IMPLEMENTER and REVIEWER roles keep one
persisted pi session per work item. :func:`~software_agent_factory.pi_sessions.persists_session`
is the single place that decides this by role. Those roles launch pi with
``--session <path>``, where
:class:`~software_agent_factory.pi_sessions.PiSessionStore` chooses the file
of the previous call (resumed) or a new one. Every other role launches with
``--no-session``. A ``CORRECT_CHANGE_SET`` call belongs to the IMPLEMENTER, so
it resumes that session, with ``--no-tools`` as before. After the call, the
store records whether pi settled cleanly: a timeout, a lost process, an RPC
error or an assistant error makes the next round start a new session. A
response that only fails artifact parsing still counts as settled, so the
correction call can resume the conversation that produced it. A record that
cannot be written is logged and never replaces the call's result.

Continuation prompt (Slice 7): a call that resumes a session sends
:func:`~software_agent_factory.prompts.build_continuation_prompt`, the
round-specific sections only, because the session already holds the rest. A
call that starts a session sends the full :func:`~software_agent_factory.prompts.build_prompt`.
When a call could resume but has nothing round-specific to send, the runtime
starts a new session with :meth:`~software_agent_factory.pi_sessions.PiSessionStore.fresh`
and sends the full prompt. The recorded prompt size is that of the prompt sent.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
from .pi_providers import (
    API_KEY_SUFFIX,
    PI_PROVIDER_CREDENTIAL_ENV_VARS,
    pi_provider_credential_vars,
)
from .pi_rpc import (
    PiProcessHandle,
    PiRpcClient,
    PiRpcCommandError,
    PiRpcError,
    PiRpcProcessExited,
    PiRpcProtocolError,
    PiRpcTimeout,
)
from .pi_sessions import Continue, PiSessionStore, SessionSettings, persists_session
from .prompts import build_continuation_prompt, build_prompt
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

_NO_SESSION_ARG = ("--no-session",)


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


def _response_data(response: Mapping[str, Any], command: str) -> Mapping[str, Any]:
    """Return a response's ``data`` mapping (``{}`` when absent).

    Raises :class:`_UnexpectedResponse` when ``data`` is present but not a
    mapping, instead of letting a bare ``.get()`` call raise
    ``AttributeError``.
    """
    data = response.get("data")
    if data is not None and not isinstance(data, Mapping):
        raise _UnexpectedResponse(command)
    return data or {}


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


@dataclass(frozen=True)
class _CallContext:
    """The per-call values every failed ``AgentResult`` of one ``run`` shares."""

    request: AgentRequest
    prompt_chars: int
    boot_ms: float
    scrubbed_values: set[str]

    def failed(
        self,
        message: str,
        *,
        usage: UsageMetrics | None = None,
        response_chars: int = 0,
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
        ``get_messages`` read, or after
        :meth:`PiAgentRuntime._best_effort_usage`, passes the usage recorded
        so far (AC6).
        """
        return AgentResult(
            role=self.request.role,
            success=False,
            failure_reason=sanitize_output(message, self.scrubbed_values),
            usage=usage,
            performance=PerformanceRecord(
                prompt_chars=self.prompt_chars,
                response_chars=response_chars,
                process_boot_ms=self.boot_ms,
            ),
        )


@dataclass(frozen=True)
class _SessionUse:
    """The persisted session one call runs in: its file, its settings, and whether it resumes."""

    path: Path
    settings: SessionSettings
    continued: bool

    @property
    def args(self) -> tuple[str, str]:
        return ("--session", str(self.path))


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
    """Sum the cache writes over ``usages``: ``cacheWrite``, else ``cacheWrite1h``.

    Per the "Usage mapping" table: ``cacheWrite`` -> ``cache_write_tokens``.
    pi reports ``cacheWrite1h`` as a subset of ``cacheWrite``, so adding both
    would count the one-hour writes twice. A message that reports only
    ``cacheWrite1h`` contributes that count; one that reports neither
    contributes nothing.
    """
    reported = [count for usage in usages if (count := _cache_write_of(usage)) is not None]
    return sum(reported) if reported else None


def _cache_write_of(usage: Mapping[str, Any]) -> int | None:
    count = non_negative_int(usage.get("cacheWrite"))
    if count is None:
        count = non_negative_int(usage.get("cacheWrite1h"))
    return count


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


def _stop_reason_failure_message(stop_reason: object, error_message: str | None) -> str | None:
    """Failure message for an ``error`` or ``aborted`` stop reason, else ``None``."""
    if stop_reason == "error":
        return f"pi assistant error: {error_message or '(no error message)'}"
    if stop_reason == "aborted":
        return "pi assistant call was aborted"
    return None


class PiAgentRuntime(AgentRuntime):
    """Production ``AgentRuntime`` backed by the ``pi`` CLI.

    ``config`` is the loaded ``pi:`` configuration block
    (:class:`~software_agent_factory.config.PiConfig`); ``data_dir`` is the
    factory's configured data directory. The per-work-item session store lives
    under ``<data_dir>/pi-sessions`` and reuses a session for
    ``config.session_reuse_max_age_seconds``. ``clock`` replaces the store's
    UTC clock, for tests.
    """

    def __init__(
        self,
        config: PiConfig,
        data_dir: Path,
        *,
        process_factory: ProcessFactory = _default_process_factory,
        routing_api_key_env_var: str = _DEFAULT_ROUTING_API_KEY_ENV_VAR,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._process_factory = process_factory
        self._routing_api_key_env_var = routing_api_key_env_var
        self._sessions = PiSessionStore(
            data_dir / "pi-sessions",
            max_age_seconds=config.session_reuse_max_age_seconds,
            clock=clock,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        validate_runtime_request(request)
        session, prompt = self._session_and_prompt(request)
        command = self._build_command(
            request, session_arg=session.args if session else _NO_SESSION_ARG
        )
        cwd = workspace_cwd(request)
        env, scrubbed_values = self._child_env_and_scrubbed()
        prompt_chars = len(prompt)

        boot_start = time.perf_counter()
        try:
            process = self._process_factory(command, cwd, env)
        except OSError as exc:
            ctx = _CallContext(request, prompt_chars, _elapsed_ms(boot_start), scrubbed_values)
            return ctx.failed(
                f"pi could not be started ({type(exc).__name__}): {self._config.executable}"
            )
        ctx = _CallContext(request, prompt_chars, _elapsed_ms(boot_start), scrubbed_values)

        settled = False
        try:
            result, settled = self._converse(ctx, process, prompt)
            return result
        finally:
            if session is not None:
                self._record_session(request, session, success=settled)

    def _session_for(self, request: AgentRequest) -> _SessionUse | None:
        """Return the persisted session this call runs in, ``None`` for a role without one."""
        if not persists_session(request.role):
            return None
        settings = SessionSettings(
            model=request.model, provider=self._config.provider, reasoning=request.reasoning
        )
        decision = self._sessions.resolve(request.work_item.id, request.role, settings)
        return _SessionUse(decision.path, settings, continued=isinstance(decision, Continue))

    def _session_and_prompt(self, request: AgentRequest) -> tuple[_SessionUse | None, str]:
        """Return the session this call runs in and the prompt it sends.

        A resumed session gets the continuation prompt. When the request holds
        nothing round-specific, the call moves to a new session and sends the full
        prompt instead, so no session ever receives an empty round.
        """
        session = self._session_for(request)
        if session is not None and session.continued:
            continuation = build_continuation_prompt(request)
            if continuation is not None:
                return session, continuation
            new_session = self._sessions.fresh(request.work_item.id, request.role)
            session = _SessionUse(new_session.path, session.settings, continued=False)
        return session, build_prompt(request)

    def _record_session(
        self, request: AgentRequest, session: _SessionUse, *, success: bool
    ) -> None:
        """Tell the store how the call ended. A record that cannot be written is only logged."""
        try:
            self._sessions.record(
                request.work_item.id, request.role, session.path, session.settings, success=success
            )
        except OSError:
            logger.warning("could not record the pi session outcome", exc_info=True)

    def _converse(
        self, ctx: _CallContext, process: PiProcessHandle, prompt: str
    ) -> tuple[AgentResult, bool]:
        """Drive one started pi process; return the result and whether pi settled cleanly.

        The flag is ``False`` for a timeout, a lost process, an RPC error and an
        assistant error or abort. It is ``True`` once pi answered, even when the
        answer then fails artifact parsing.
        """
        request = ctx.request
        client = PiRpcClient(process, redact=lambda text: redact_secrets(text, ctx.scrubbed_values))
        usage: UsageMetrics | None = None
        try:
            deadline = time.monotonic() + request.timeout_seconds
            try:
                client.request({"type": "prompt", "message": prompt}, deadline=deadline)
                client.wait_for_settled(deadline=deadline)
                messages = self._get_messages(client, deadline=deadline)
                usage = _safe_usage_from_pi_messages(messages)
                stop_reason, error_message = _stop_reason_from_messages(messages)
                failure_message = _stop_reason_failure_message(stop_reason, error_message)
                if failure_message is not None:
                    return ctx.failed(failure_message, usage=usage), False
                response = client.request({"type": "get_last_assistant_text"}, deadline=deadline)
                data = _response_data(response, "get_last_assistant_text")
            except PiRpcTimeout:
                if usage is None:
                    usage = self._best_effort_usage(client)
                self._abort_and_kill(client, process)
                return (
                    ctx.failed(
                        f"pi timed out after {request.timeout_seconds} seconds", usage=usage
                    ),
                    False,
                )
            except PiRpcProcessExited as exc:
                if usage is None:
                    usage = self._best_effort_usage(client)
                return ctx.failed(_failure_reason_for(exc), usage=usage), False
            except PiRpcError as exc:
                return ctx.failed(_failure_reason_for(exc), usage=usage), False
            except _UnexpectedResponse as exc:
                return ctx.failed(str(exc), usage=usage), False

            return self._result_from_response(ctx, data, usage=usage), True
        finally:
            client.close()

    @staticmethod
    def _result_from_response(
        ctx: _CallContext,
        data: Mapping[str, Any],
        *,
        usage: UsageMetrics | None,
    ) -> AgentResult:
        """Turn the ``get_last_assistant_text`` ``data`` into a success or failed result."""
        text = str(data.get("text") or "")

        performance = PerformanceRecord(
            prompt_chars=ctx.prompt_chars,
            response_chars=len(text),
            process_boot_ms=ctx.boot_ms,
        )

        try:
            artifact = parse_agent_artifact(
                ctx.request.role, text=text, purpose=ctx.request.purpose
            )
        except ValueError as exc:
            return ctx.failed(str(exc), response_chars=len(text), usage=usage)

        return build_success_result(
            ctx.request.role,
            purpose=ctx.request.purpose,
            artifact=artifact,
            performance=performance,
            usage=usage,
        )

    def _build_command(
        self,
        request: AgentRequest,
        *,
        session_arg: Sequence[str],
    ) -> list[str]:
        """Build the ``pi --mode rpc`` command line for one agent call.

        ``session_arg`` is ``["--no-session"]`` or ``["--session", <path>]``;
        :meth:`run` passes the second form for a role that keeps a session
        (see :func:`~software_agent_factory.pi_sessions.persists_session`).
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
        ``<PROVIDER>_API_KEY`` for a provider the map does not list. Every
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
        # A provider newer than the map may read a key the map does not name,
        # so every *_API_KEY variable is treated as a credential.
        other_api_keys = [name for name in env if name.endswith(API_KEY_SUFFIX)]
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

    def _get_messages(self, client: PiRpcClient, *, deadline: float) -> list[dict[str, Any]]:
        """Read the settled call's full message list via ``get_messages``.

        The same command ``scripts/performance/pi_cache_probe.py`` already
        uses to read per-message ``usage``. :meth:`run` reads this once per
        call and derives both the final assistant message's ``stopReason``
        (:func:`_stop_reason_from_messages`) and its usage
        (:func:`usage_from_pi_messages`) from the same result, rather than
        issuing two RPC round trips for one already-settled conversation.

        Raises :class:`_UnexpectedResponse` when the response's ``data`` is
        present but not a mapping (see :func:`_response_data`).
        """
        response = client.request({"type": "get_messages"}, deadline=deadline)
        data = _response_data(response, "get_messages")
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
