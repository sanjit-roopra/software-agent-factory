"""Claude Code CLI subprocess runtime (ADR-045).

One ``claude -p`` process per call, in the run worktree, authenticated by the
logged-in Claude subscription. The process loads none of the user's settings,
plugins, hooks, MCP servers or auto-memory, keeps no session, and gets the
same tool profile per :class:`AgentCapability` as the Copilot runtime. The
``acceptEdits`` permission mode keeps file tools inside the worktree: a read or
edit outside it needs approval, and ``-p`` has nobody to give it. The final
``result`` event of the ``stream-json`` output carries the assistant text and
the usage.
"""

from __future__ import annotations

import json
import subprocess
import time
from json import JSONDecodeError

from .agent_artifact import build_success_result, parse_agent_artifact
from .agent_capabilities import AgentCapability, capability_for
from .agents import (
    RUNTIME_FAILURE_REASON_LIMIT,
    AgentRequest,
    AgentResult,
    AgentRuntime,
    validate_runtime_request,
    workspace_cwd,
)
from .models import ModelUsage, PerformanceRecord, UsageMetrics, utc_now
from .prompts import build_prompt
from .subprocess_utils import (
    build_child_env,
    extract_first_event_ms,
    format_failure_reason,
    merge_timeout_output,
    sanitize_output,
)
from .subprocess_utils import kill_process_group as _kill_process_group
from .usage_values import non_negative_float, non_negative_int

#: The values ``claude --effort`` accepts; ``reasoning`` passes through 1:1.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
IMPLEMENTER_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Grep", "Glob")
READ_ONLY_DENIED: tuple[str, ...] = ("WebFetch", "WebSearch")
#: Mirrors the Copilot implementer deny list; ``curl`` and ``wget`` stand in for
#: the Copilot ``url`` deny, as for pi (ADR-032).
IMPLEMENTER_DENIED: tuple[str, ...] = (
    *READ_ONLY_DENIED,
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(gh:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
)
#: Removed from the child so ``claude`` uses the logged-in subscription, never
#: an API key or a cloud provider that bills per token.
ANTHROPIC_BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
)


class ClaudeCodeAgentRuntime(AgentRuntime):
    """Invoke the installed ``claude`` CLI and parse one typed artifact."""

    def __init__(
        self, *, executable: str = "claude", max_error_chars: int = RUNTIME_FAILURE_REASON_LIMIT
    ) -> None:
        self._executable = executable
        self._max_error_chars = max_error_chars

    def run(self, request: AgentRequest) -> AgentResult:
        validate_runtime_request(request)
        if request.reasoning not in EFFORT_LEVELS:
            return AgentResult(
                role=request.role,
                success=False,
                failure_reason=(
                    f"{request.role.value}: claude-code does not accept reasoning "
                    f"{request.reasoning!r}; use one of {', '.join(EFFORT_LEVELS)}."
                ),
            )

        cwd = workspace_cwd(request)
        prompt = build_prompt(request)
        child_env, scrubbed_values = build_child_env()
        for name in ANTHROPIC_BILLING_ENV_VARS:
            if value := child_env.pop(name, None):
                scrubbed_values.add(value)
        child_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        started_at = utc_now()
        command = self.build_command(request)

        def build_failure_result(
            message: str,
            stdout: str,
            stderr: str,
            *,
            usage: UsageMetrics | None = None,
            performance: PerformanceRecord | None = None,
        ) -> AgentResult:
            reason = format_failure_reason(
                role=request.role,
                message=message,
                stdout=stdout,
                stderr=stderr,
                scrubbed_values=scrubbed_values,
                limit=self._max_error_chars,
            )
            return AgentResult(
                role=request.role,
                success=False,
                failure_reason=reason,
                usage=usage,
                performance=performance,
            )

        boot_start = time.perf_counter()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            perf = PerformanceRecord(
                prompt_chars=len(prompt),
                response_chars=0,
                process_boot_ms=(time.perf_counter() - boot_start) * 1000.0,
            )
            return build_failure_result(
                f"claude could not be started ({type(exc).__name__})",
                "",
                str(exc),
                performance=perf,
            )
        boot_ms = (time.perf_counter() - boot_start) * 1000.0

        timed_out = False
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout, stderr = _kill_process_group(process)
            stdout = merge_timeout_output(exc.stdout, stdout)
            stderr = merge_timeout_output(exc.stderr, stderr)
        except BaseException:
            _kill_process_group(process)
            raise

        result_event = _final_result_event(stdout)
        usage = usage_from_result_event(result_event) if result_event is not None else None
        perf = PerformanceRecord(
            prompt_chars=len(prompt),
            response_chars=len(stdout),
            process_boot_ms=boot_ms,
            first_event_ms=extract_first_event_ms(stdout, started_at),
        )
        if timed_out:
            return build_failure_result(
                f"claude timed out after {request.timeout_seconds}s",
                stdout,
                stderr,
                usage=usage,
                performance=perf,
            )
        if result_event is None or result_event.get("is_error") is True:
            message = (
                f"claude reported an error ({result_event.get('subtype')}): "
                f"{sanitize_output(str(result_event.get('result')), scrubbed_values)}"
                if result_event is not None
                else f"claude exited with code {process.returncode} and no result event"
            )
            return build_failure_result(message, stdout, stderr, usage=usage, performance=perf)
        if process.returncode != 0:
            return build_failure_result(
                f"claude exited with code {process.returncode}",
                stdout,
                stderr,
                usage=usage,
                performance=perf,
            )

        text = result_event.get("result")
        try:
            artifact = parse_agent_artifact(
                request.role,
                text=text if isinstance(text, str) else "",
                purpose=request.purpose,
            )
        except ValueError as exc:
            return build_failure_result(str(exc), stdout, stderr, usage=usage, performance=perf)
        return build_success_result(
            request.role,
            purpose=request.purpose,
            artifact=artifact,
            usage=usage,
            performance=perf,
        )

    def build_command(self, request: AgentRequest) -> list[str]:
        """The ``claude`` argv; the prompt itself goes to stdin."""
        if capability_for(request) is AgentCapability.IMPLEMENTER_WRITE:
            tools, denied = IMPLEMENTER_TOOLS, IMPLEMENTER_DENIED
            # Bash needs an allow rule; file tools stay inside the worktree.
            allowed = ["--allowedTools", "Bash"]
        else:
            tools, denied, allowed = READ_ONLY_TOOLS, READ_ONLY_DENIED, []
        return [
            self._executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            request.model,
            "--effort",
            request.reasoning,
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            ",".join(tools),
            *allowed,
            "--disallowedTools",
            *denied,
        ]


def _final_result_event(stdout: str) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            latest = event
    return latest


def usage_from_result_event(event: dict[str, object]) -> UsageMetrics | None:
    """Map the ``result`` event's usage. ``total_cost_usd`` is a list price.

    A subscription does not bill per call, so the cost goes to
    ``list_price_estimate_usd`` as it does for pi.
    """
    model_usage: list[ModelUsage] = []
    raw_model_usage = event.get("modelUsage")
    if isinstance(raw_model_usage, dict):
        for model_name, raw_entry in raw_model_usage.items():
            if (
                not isinstance(model_name, str)
                or not model_name.strip()
                or not isinstance(raw_entry, dict)
            ):
                continue
            model_usage.append(
                ModelUsage(
                    model=model_name.strip(),
                    input_tokens=non_negative_int(raw_entry.get("inputTokens")),
                    output_tokens=non_negative_int(raw_entry.get("outputTokens")),
                    reasoning_tokens=non_negative_int(raw_entry.get("thinkingTokens")),
                    cache_read_tokens=non_negative_int(raw_entry.get("cacheReadInputTokens")),
                    cache_write_tokens=non_negative_int(raw_entry.get("cacheCreationInputTokens")),
                    list_price_estimate_usd=non_negative_float(raw_entry.get("costUSD")),
                )
            )
    raw_usage = event.get("usage")
    usage_fields = raw_usage if isinstance(raw_usage, dict) else {}
    raw_details = usage_fields.get("output_tokens_details")
    output_details = raw_details if isinstance(raw_details, dict) else {}
    metrics = UsageMetrics(
        current_model=model_usage[0].model if len(model_usage) == 1 else None,
        total_api_duration_ms=non_negative_int(event.get("duration_api_ms")),
        session_duration_ms=non_negative_int(event.get("duration_ms")),
        input_tokens=non_negative_int(usage_fields.get("input_tokens")),
        output_tokens=non_negative_int(usage_fields.get("output_tokens")),
        reasoning_tokens=non_negative_int(output_details.get("thinking_tokens")),
        cache_read_tokens=non_negative_int(usage_fields.get("cache_read_input_tokens")),
        cache_write_tokens=non_negative_int(usage_fields.get("cache_creation_input_tokens")),
        model_usage=tuple(model_usage),
        list_price_estimate_usd=non_negative_float(event.get("total_cost_usd")),
    )
    reported = metrics.model_dump(exclude={"current_model", "model_usage"}).values()
    return metrics if model_usage or any(v is not None for v in reported) else None
