"""Real GitHub Copilot subprocess runtime.

This module provides the production ``AgentRuntime`` implementation needed for
the real Copilot-backed phases. It deliberately keeps workflow authority in the
controller: the runtime only builds role-scoped prompts, runs the Copilot CLI
with constrained permissions, and validates one typed artifact from the final
response.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import ValidationError

from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import (
    AgentPurpose,
    AgentRole,
    ModelBase,
    ModelUsage,
    ProjectPlan,
    RepositorySkill,
    UsageMetrics,
)
from .prompts import (
    RoleName,
    artifact_model_for_role,
    build_prompt,
    normalize_role,
)

ResultField: TypeAlias = Literal[
    "triage_result",
    "specification",
    "research_report",
    "execution_plan",
    "change_set",
    "test_report",
    "review_report",
    "repository_skill",
    "project_plan",
]

logger = logging.getLogger(__name__)

READ_ONLY_TOOLS = ("glob", "grep", "view")
#: The skill researcher reads public documentation only: no repository
#: filesystem access, no shell, no edits and therefore no Git.
SKILL_RESEARCH_TOOLS = ("web_fetch",)
#: Defence in depth on top of ``--available-tools``: even if the tool surface
#: were widened, shell (and therefore Git) and filesystem writes stay denied.
SKILL_RESEARCH_DENIED_PERMISSIONS = ("shell", "write")
IMPLEMENTER_TOOLS = ("glob", "grep", "view", "create", "edit", "bash")
GITHUB_CREDENTIAL_ENV_VARS = frozenset(
    {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GIT_ASKPASS",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_PAT",
        "GITHUB_TOKEN",
    }
)
TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)


@dataclass(frozen=True)
class _PermissionProfile:
    available_tools: tuple[str, ...]
    denied_permissions: tuple[str, ...]


@dataclass(frozen=True)
class _ArtifactSpec:
    model_class: type[ModelBase]
    result_field: ResultField


ARTIFACT_SPECS: dict[str, _ArtifactSpec] = {
    "TRIAGE": _ArtifactSpec(
        model_class=artifact_model_for_role("TRIAGE"),
        result_field="triage_result",
    ),
    "REFINER": _ArtifactSpec(
        model_class=artifact_model_for_role("REFINER"),
        result_field="specification",
    ),
    "RESEARCHER": _ArtifactSpec(
        model_class=artifact_model_for_role("RESEARCHER"),
        result_field="research_report",
    ),
    "PLANNER": _ArtifactSpec(
        model_class=artifact_model_for_role("PLANNER"),
        result_field="execution_plan",
    ),
    "IMPLEMENTER": _ArtifactSpec(
        model_class=artifact_model_for_role("IMPLEMENTER"),
        result_field="change_set",
    ),
    # The independent tester produces a TestReport (AI judgement), never a
    # VerificationReport -- deterministic evidence is factory-produced.
    "TESTER": _ArtifactSpec(
        model_class=artifact_model_for_role("TESTER"),
        result_field="test_report",
    ),
    "REVIEWER": _ArtifactSpec(
        model_class=artifact_model_for_role("REVIEWER"),
        result_field="review_report",
    ),
}


class CopilotAgentRuntime(AgentRuntime):
    """Invoke the installed ``copilot`` CLI and parse one typed artifact."""

    def __init__(self, *, executable: str = "copilot", max_error_chars: int = 4000) -> None:
        self._executable = executable
        self._max_error_chars = max_error_chars

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER and not request.workspace_path:
            raise ValueError("IMPLEMENTER requests require workspace_path")
        if request.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")

        cwd = self._cwd_for(request)
        prompt = build_prompt(request)
        child_env, scrubbed_values = _build_child_env()

        with tempfile.TemporaryDirectory(
            prefix="software-agent-factory-usage-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            usage_path = Path(temp_dir) / "usage.json"
            command = self._build_command(
                request,
                prompt=prompt,
                cwd=cwd,
                usage_output_path=usage_path,
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                # A missing or unusable copilot executable is an agent failure the
                # controller can record and bound, not a factory crash.
                reason = _format_failure_reason(
                    role=request.role,
                    message=f"copilot could not be started ({type(exc).__name__})",
                    stdout="",
                    stderr=str(exc),
                    scrubbed_values=scrubbed_values,
                    limit=self._max_error_chars,
                )
                return AgentResult(role=request.role, success=False, failure_reason=reason)
            try:
                stdout, stderr = process.communicate(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _kill_process_group(process)
                stdout = _merge_timeout_output(exc.stdout, stdout)
                stderr = _merge_timeout_output(exc.stderr, stderr)
                usage = _load_usage_metrics(usage_path, stdout=stdout)
                reason = _format_failure_reason(
                    role=request.role,
                    message=f"copilot timed out after {request.timeout_seconds}s",
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
                )
            except BaseException:
                _kill_process_group(process)
                raise

            usage = _load_usage_metrics(usage_path, stdout=stdout)
            if process.returncode != 0:
                reason = _format_failure_reason(
                    role=request.role,
                    message=f"copilot exited with code {process.returncode}",
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
                )

            try:
                artifact = parse_copilot_artifact(
                    request.role,
                    purpose=request.purpose,
                    stdout=stdout,
                )
            except ValueError as exc:
                reason = _format_failure_reason(
                    role=request.role,
                    message=str(exc),
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
                )

            result_field = _artifact_spec(request.role, request.purpose).result_field
            return AgentResult.model_validate(
                {
                    "role": request.role,
                    "success": True,
                    "usage": usage,
                    result_field: artifact,
                }
            )

    def _cwd_for(self, request: AgentRequest) -> Path:
        if request.workspace_path:
            return Path(request.workspace_path).expanduser().resolve()
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            # The skill researcher must run in the neutral run directory the
            # workflow passes, never in the operator's or repository's cwd.
            raise ValueError(
                "repository skill generation requires workspace_path (the neutral run directory)"
            )
        return Path(os.getcwd()).expanduser().resolve()

    def _build_command(
        self,
        request: AgentRequest,
        *,
        prompt: str,
        cwd: Path,
        usage_output_path: Path | None = None,
    ) -> list[str]:
        profile = _permission_profile(request)
        command = [
            self._executable,
            "-C",
            str(cwd),
            "--model",
            request.model,
            "--reasoning-effort",
            request.reasoning,
            "--context",
            str(request.context_tier),
            "--output-format",
            "json",
            "--stream",
            "off",
            "--no-remote",
            "--no-remote-export",
            "--no-auto-update",
            "--no-ask-user",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--allow-all-tools",
            "--available-tools",
            ",".join(profile.available_tools),
        ]
        if usage_output_path is not None:
            command.extend(["--usage-output-file", str(usage_output_path)])
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            command.append("--no-custom-instructions")
            for url in _skill_research_urls(request):
                command.extend(["--allow-url", url])
        for denied_permission in profile.denied_permissions:
            command.extend(["--deny-tool", denied_permission])
        command.extend(["-p", prompt])
        return command


def _load_usage_metrics(path: Path, *, stdout: str) -> UsageMetrics | None:
    file_metrics: UsageMetrics | None = None
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("could not read Copilot usage output at %s: %s", path, exc)
    else:
        try:
            file_metrics = parse_copilot_usage(json.loads(raw))
        except (JSONDecodeError, ValueError) as exc:
            logger.warning("ignored malformed Copilot usage output at %s: %s", path, exc)

    result_metrics = _parse_stream_usage(stdout)
    if file_metrics is None:
        return result_metrics
    if result_metrics is None:
        return file_metrics
    updates: dict[str, object] = {}
    if result_metrics.premium_requests is not None:
        updates["premium_requests"] = result_metrics.premium_requests
    if result_metrics.total_nano_aiu is not None and file_metrics.total_nano_aiu is None:
        updates["total_nano_aiu"] = result_metrics.total_nano_aiu
    if result_metrics.total_api_duration_ms is not None:
        updates["total_api_duration_ms"] = (
            file_metrics.total_api_duration_ms
            if file_metrics.total_api_duration_ms is not None
            else result_metrics.total_api_duration_ms
        )
    if result_metrics.session_duration_ms is not None:
        updates["session_duration_ms"] = result_metrics.session_duration_ms
    return file_metrics.model_copy(update=updates)


def parse_copilot_usage(payload: object) -> UsageMetrics | None:
    """Parse the experimental aggregate usage file conservatively."""

    if not isinstance(payload, dict):
        return None

    model_usage: list[ModelUsage] = []
    raw_model_metrics = payload.get("modelMetrics")
    if isinstance(raw_model_metrics, dict):
        for model, raw_metrics in raw_model_metrics.items():
            if not isinstance(model, str) or not model.strip() or not isinstance(raw_metrics, dict):
                continue
            requests = raw_metrics.get("requests")
            usage = raw_metrics.get("usage")
            requests = requests if isinstance(requests, dict) else {}
            usage = usage if isinstance(usage, dict) else {}
            parsed = ModelUsage(
                model=model.strip(),
                requests=_non_negative_int(requests.get("count")),
                premium_request_cost=_non_negative_float(requests.get("cost")),
                input_tokens=_non_negative_int(usage.get("inputTokens")),
                output_tokens=_non_negative_int(usage.get("outputTokens")),
                reasoning_tokens=_non_negative_int(usage.get("reasoningTokens")),
                cache_read_tokens=_non_negative_int(usage.get("cacheReadTokens")),
                cache_write_tokens=_non_negative_int(usage.get("cacheWriteTokens")),
                total_nano_aiu=_non_negative_int(raw_metrics.get("totalNanoAiu")),
            )
            if any(value is not None for name, value in parsed if name != "model"):
                model_usage.append(parsed)

    current_model = payload.get("currentModel")
    token_details = payload.get("tokenDetails")
    token_details = token_details if isinstance(token_details, dict) else {}
    metrics = UsageMetrics(
        current_model=(
            current_model.strip()
            if isinstance(current_model, str) and current_model.strip()
            else None
        ),
        total_premium_request_cost=_non_negative_float(payload.get("totalPremiumRequestCost")),
        total_user_requests=_non_negative_int(payload.get("totalUserRequests")),
        total_nano_aiu=_non_negative_int(payload.get("totalNanoAiu")),
        total_api_duration_ms=_non_negative_int(payload.get("totalApiDurationMs")),
        input_tokens=_token_detail_count(token_details.get("input")),
        output_tokens=_token_detail_count(token_details.get("output")),
        reasoning_tokens=_token_detail_count(token_details.get("reasoning")),
        cache_read_tokens=_token_detail_count(token_details.get("cache_read")),
        cache_write_tokens=_token_detail_count(token_details.get("cache_write")),
        last_call_input_tokens=_non_negative_int(payload.get("lastCallInputTokens")),
        last_call_output_tokens=_non_negative_int(payload.get("lastCallOutputTokens")),
        model_usage=tuple(model_usage),
    )
    return metrics if _has_reported_usage(metrics) else None


def _parse_result_usage(stdout: str) -> UsageMetrics | None:
    latest: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        usage = event.get("usage")
        data = event.get("data")
        if isinstance(usage, dict):
            latest = usage
        elif isinstance(data, dict):
            latest = data
        else:
            latest = event
    if latest is None:
        return None
    metrics = UsageMetrics(
        premium_requests=_non_negative_float(latest.get("premiumRequests")),
        total_api_duration_ms=_non_negative_int(latest.get("totalApiDurationMs")),
        session_duration_ms=_non_negative_int(latest.get("sessionDurationMs")),
    )
    return metrics if _has_reported_usage(metrics) else None


def _parse_usage_checkpoint(stdout: str) -> UsageMetrics | None:
    latest: dict[str, object] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "session.usage_checkpoint":
            continue
        data = event.get("data")
        if isinstance(data, dict):
            latest = data
    if latest is None:
        return None
    metrics = UsageMetrics(
        premium_requests=_non_negative_float(latest.get("totalPremiumRequests")),
        total_nano_aiu=_non_negative_int(latest.get("totalNanoAiu")),
        total_api_duration_ms=_non_negative_int(latest.get("totalApiDurationMs")),
        session_duration_ms=_non_negative_int(latest.get("sessionDurationMs")),
    )
    return metrics if _has_reported_usage(metrics) else None


def _parse_stream_usage(stdout: str) -> UsageMetrics | None:
    checkpoint = _parse_usage_checkpoint(stdout)
    result = _parse_result_usage(stdout)
    if checkpoint is None:
        return result
    if result is None:
        return checkpoint
    return checkpoint.model_copy(
        update={
            name: value
            for name in (
                "premium_requests",
                "total_api_duration_ms",
                "session_duration_ms",
            )
            if (value := getattr(result, name)) is not None
        }
    )


def _has_reported_usage(metrics: UsageMetrics) -> bool:
    numeric_fields = (
        metrics.premium_requests,
        metrics.total_premium_request_cost,
        metrics.total_user_requests,
        metrics.total_nano_aiu,
        metrics.total_api_duration_ms,
        metrics.session_duration_ms,
        metrics.input_tokens,
        metrics.output_tokens,
        metrics.reasoning_tokens,
        metrics.cache_read_tokens,
        metrics.cache_write_tokens,
        metrics.last_call_input_tokens,
        metrics.last_call_output_tokens,
    )
    return any(value is not None for value in numeric_fields) or bool(metrics.model_usage)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _non_negative_float(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


def _token_detail_count(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    return _non_negative_int(value.get("tokenCount"))


def parse_copilot_artifact(
    role: RoleName,
    *,
    stdout: str,
    purpose: AgentPurpose = AgentPurpose.STANDARD,
) -> ModelBase:
    """Extract and validate a single typed artifact from Copilot output."""

    spec = _artifact_spec(role, purpose)
    candidates = _assistant_response_candidates(stdout)
    if not candidates:
        assistant_text = extract_assistant_text(stdout)
        candidates = _candidate_texts(assistant_text, stdout)

    found_object = False
    first_validation_error: ValidationError | None = None
    for candidate in candidates:
        for payload in _iter_json_objects(candidate):
            found_object = True
            try:
                return spec.model_class.model_validate(payload)
            except ValidationError as exc:
                if first_validation_error is None:
                    first_validation_error = exc

    role_name = normalize_role(role)
    if first_validation_error is not None:
        raise ValueError(
            f"{role_name} response did not validate as {spec.model_class.__name__}: "
            f"{_summarize_validation_error(first_validation_error)}"
        )
    if not found_object:
        raise ValueError(
            f"{role_name} response did not contain a parseable JSON object for "
            f"{spec.model_class.__name__}"
        )
    raise ValueError(f"{role_name} response did not contain a valid {spec.model_class.__name__}")


def extract_assistant_text(stdout: str) -> str:
    """Collect assistant text fragments from Copilot JSONL output.

    If no recognized JSONL fragments are found, the plain stdout text is
    returned unchanged so direct-JSON and plain-text fallbacks still work.
    """

    fragments: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        event_type = event.get("type")
        if (
            event_type
            in {
                "assistant.message",
                "assistant.message.delta",
                "model.response",
                "model.response.delta",
                "response.completed",
            }
            or event_type is None
        ):
            fragments.extend(_extract_text_fragments(event))

    cleaned = [fragment.strip() for fragment in fragments if fragment.strip()]
    if cleaned:
        return "\n".join(_dedupe_fragments(cleaned))
    return stdout.strip()


def _artifact_spec(
    role: RoleName,
    purpose: AgentPurpose = AgentPurpose.STANDARD,
) -> _ArtifactSpec:
    if purpose is AgentPurpose.DECOMPOSE_PROJECT:
        if normalize_role(role) != AgentRole.PLANNER.value:
            raise ValueError("project decomposition requires the PLANNER role")
        return _ArtifactSpec(
            model_class=ProjectPlan,
            result_field="project_plan",
        )
    if purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
        if normalize_role(role) != AgentRole.RESEARCHER.value:
            raise ValueError("repository skill generation requires the RESEARCHER role")
        return _ArtifactSpec(
            model_class=RepositorySkill,
            result_field="repository_skill",
        )
    normalized_role = normalize_role(role)
    try:
        return ARTIFACT_SPECS[normalized_role]
    except KeyError as exc:  # pragma: no cover - defensive programmer error
        raise ValueError(f"unsupported agent role: {role!r}") from exc


def _permission_profile(request: AgentRequest) -> _PermissionProfile:
    if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
        return _PermissionProfile(
            available_tools=SKILL_RESEARCH_TOOLS,
            denied_permissions=SKILL_RESEARCH_DENIED_PERMISSIONS,
        )
    if request.role is AgentRole.IMPLEMENTER:
        return _PermissionProfile(
            available_tools=IMPLEMENTER_TOOLS,
            denied_permissions=("url", "shell(git commit)", "shell(git push)", "shell(gh:*)"),
        )
    return _PermissionProfile(
        available_tools=READ_ONLY_TOOLS,
        denied_permissions=("url",),
    )


def _skill_research_urls(request: AgentRequest) -> tuple[str, ...]:
    """Combine both configured URL lists into one deduplicated allowlist.

    The result is the complete set of ``--allow-url`` grants for a skill
    request. Non-HTTPS or credential-bearing entries are rejected here as well
    as in configuration, so a hand-built request cannot widen the sandbox.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    for url in (
        *request.official_documentation_origins,
        *request.practice_reference_urls,
    ):
        _validate_skill_research_url(url)
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)

    if not ordered:
        raise ValueError("repository skill generation requires at least one allowed URL")
    return tuple(ordered)


def _validate_skill_research_url(url: str) -> None:
    if url != url.strip() or any(character.isspace() for character in url):
        raise ValueError(f"repository skill research URL must not contain whitespace: {url!r}")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"repository skill research URL is not parseable: {url!r}") from exc
    if parsed.scheme != "https" or not hostname:
        raise ValueError(f"repository skill research URLs must be HTTPS URLs: {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"repository skill research URLs must not carry credentials: {url!r}")


def _build_child_env() -> tuple[dict[str, str], set[str]]:
    env = dict(os.environ)
    scrubbed_values: set[str] = set()
    for name in GITHUB_CREDENTIAL_ENV_VARS:
        value = env.pop(name, None)
        if value:
            scrubbed_values.add(value)
    return env, scrubbed_values


def _decode_timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _merge_timeout_output(previous: object, final: str) -> str:
    prefix = _decode_timeout_text(previous)
    if not prefix or final.startswith(prefix):
        return final
    return f"{prefix}{final}"


def _kill_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return process.communicate()


def _candidate_texts(assistant_text: str, stdout: str) -> list[str]:
    candidates: list[str] = []
    for text in (assistant_text.strip(), stdout.strip()):
        if text and text not in candidates:
            candidates.append(text)
    return candidates


def _assistant_response_candidates(stdout: str) -> list[str]:
    direct_candidates: list[str] = []
    fallback_candidates: list[str] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        _collect_event_candidates(event, direct_candidates, fallback_candidates)

    ordered: list[str] = []
    for candidate in reversed(direct_candidates):
        text = candidate.strip()
        if text and text not in ordered:
            ordered.append(text)
    for candidate in reversed(fallback_candidates):
        text = candidate.strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def _iter_json_objects(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, object]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
    return objects


def _collect_event_candidates(
    event: dict[str, object],
    direct_candidates: list[str],
    fallback_candidates: list[str],
) -> None:
    event_type = event.get("type")
    if event_type == "assistant.message":
        data = event.get("data")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str):
                direct_candidates.append(content)
                return
    if event_type == "model.response":
        data = event.get("data")
        if isinstance(data, dict):
            response = data.get("response")
            if isinstance(response, dict):
                content = response.get("content")
                if isinstance(content, str):
                    direct_candidates.append(content)
                    return
    if event_type == "response.completed":
        response = event.get("response")
        if isinstance(response, dict):
            content = response.get("content")
            if isinstance(content, str):
                direct_candidates.append(content)
                return
    if event_type in {"assistant.message.delta", "model.response.delta"}:
        fallback_candidates.extend(_extract_text_fragments(event))


def _extract_text_fragments(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        item_fragments: list[str] = []
        for item in value:
            item_fragments.extend(_extract_text_fragments(item))
        return item_fragments
    if not isinstance(value, dict):
        return []

    fragments: list[str] = []
    text = value.get("text")
    if isinstance(text, str):
        fragments.append(text)

    delta = value.get("delta")
    if isinstance(delta, str):
        fragments.append(delta)
    else:
        fragments.extend(_extract_text_fragments(delta))

    for key in (
        "content",
        "data",
        "message",
        "assistant",
        "response",
        "output",
        "item",
        "payload",
        "result",
    ):
        if key in value:
            fragments.extend(_extract_text_fragments(value[key]))
    return fragments


def _dedupe_fragments(fragments: list[str]) -> list[str]:
    deduped: list[str] = []
    previous = None
    for fragment in fragments:
        if fragment == previous:
            continue
        deduped.append(fragment)
        previous = fragment
    return deduped


def _summarize_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    if not details:
        return str(error)
    summaries: list[str] = []
    for detail in details[:8]:
        location = ".".join(str(part) for part in detail.get("loc", ()))
        message = str(detail.get("msg", "validation error"))
        summaries.append(f"{location}: {message}" if location else message)
    if len(details) > len(summaries):
        summaries.append(f"{len(details) - len(summaries)} more validation error(s)")
    return "; ".join(summaries)


def _format_failure_reason(
    *,
    role: AgentRole,
    message: str,
    stdout: str,
    stderr: str,
    scrubbed_values: set[str],
    limit: int,
) -> str:
    sections: list[str] = [f"{role.value}: {message}."]
    cleaned_stdout = _sanitize_output(stdout, scrubbed_values)
    cleaned_stderr = _sanitize_output(stderr, scrubbed_values)
    if cleaned_stdout:
        sections.append(f"stdout={cleaned_stdout}")
    if cleaned_stderr:
        sections.append(f"stderr={cleaned_stderr}")
    combined = " ".join(sections)
    if len(combined) <= limit:
        return combined
    return f"{combined[: limit - 12].rstrip()}...[truncated]"


def _sanitize_output(text: str, scrubbed_values: set[str]) -> str:
    sanitized = text
    for value in sorted(scrubbed_values, key=len, reverse=True):
        if len(value) >= 4:
            sanitized = sanitized.replace(value, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) <= 600:
        return sanitized
    return f"{sanitized[:597].rstrip()}..."
