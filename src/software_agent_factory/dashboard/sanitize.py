"""Response data minimization: field allowlists applied inside the handler.

Every provider in :mod:`software_agent_factory.dashboard.snapshot` is trusted
to already return dashboard-safe data -- but "trusted" is not "enforced", and
a future provider (or a bug in one) could accidentally include a command log,
a diff, a prompt, tool output, a token/secret, or free-form failure text in
its payload. This module is the second, independent line of defense: the
handler allowlists exactly the fields the UI actually renders and drops
everything else, so a provider mistake can leak at most an unused-but-safe
field name, never its content.

A failure reason is free-form text that could contain repository content, and
nothing in this package can verify a provider redacted it. So the run, attempt
and call ``failure_reason`` are redacted here and cut to a bounded length by
:func:`software_agent_factory.redaction.bounded_reason`. Each carries a
``failure_reason_truncated`` flag. The escalation's approval scope and decision
questions are free text too, so each string gets the same redact and cut, and
an over-long list is dropped whole. ``reasoning`` on a call is the reasoning
level (for example ``high``), never model text: it is kept only when it is a
short token.
The run and task ``title`` and the attempt, call and project ``model`` names are
redacted with :func:`software_agent_factory.redaction.redact_secrets`. They are not cut.
A value that is not text becomes ``None``. The health messages, remediation text,
degraded reasons and error get the same redaction.

Each state move's reason gets the same redact and cut (see :mod:`.transitions`).

A call's ``tool_calls`` are the one free-text field that comes from what an
agent did: each is a tool name and a one-line preview, such as a shell command
or a file path. They are redacted and cut like the fields above, and every
absolute path in them is made relative: a path inside a run's worktree keeps
only the part after the worktree, and any other absolute path keeps only its
last part. Tool output is never sent.

Nothing here composes a view model. :mod:`software_agent_factory.dashboard.view`
adds the run totals and the next step to what these functions return.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import timedelta
from typing import Any, get_args

from ..escalation_protocol import MAX_PLAN_DECISIONS, REPLY_CLOSED_CAUSES
from ..models import (
    COST_UNIT_FIELDS,
    HALT_REASON_COPY,
    MAX_GUIDANCE_FINDINGS,
    MAX_SCOPE_APPROVAL_PATHS,
    NANO_AIU_PER_USD,
    TOKEN_CLASS_FIELDS,
    UNRESOLVED_DECISIONS_REPLACE_ACTION,
    ActiveInvocationStatus,
    HaltReasonCode,
    ReviewFindingCategory,
    RuntimeName,
)
from ..redaction import bounded_reason, redact_secrets
from ..store import ARTIFACT_FILENAMES
from .aggregate import STATUS_FAILED, STATUS_SUCCESS, call_total_tokens
from .snapshot import to_json_safe
from .transitions import parse_timestamp, sanitize_transitions, timestamp
from .validators import (
    ESCALATION_STATUSES,
    ESCALATION_TARGET_TYPES,
    HALT_REASON_CODES,
    RESUME_CLASSIFICATIONS,
    RESUME_REFUSALS,
    is_context_fingerprint,
    is_count,
    is_episode_id,
    is_number,
    is_positive_int,
    is_safe_https_url,
    run_id_of,
)

_GITHUB_EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_SHORT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_SAFE_ARTIFACT_NAMES = frozenset(ARTIFACT_FILENAMES.values())

#: The one spelling of the title field: it is allowlisted and redacted by this name.
_TITLE_FIELD = "title"
#: The one spelling of the run list's call field: it is allowlisted and sanitized by this name.
_CALLS_FIELD = "calls"

#: Fields rendered in the paginated run table (``/api/runs``). Includes both
#: ``run_id`` (the real ``observability.RunSummary`` field name) and ``id``
#: (accepted from simpler providers/tests) since the client tolerates either.
RUN_SUMMARY_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "id",
        "work_item_id",
        "source_external_id",
        _TITLE_FIELD,
        "state",
        "complexity",
        "risk",
        "created_at",
        "updated_at",
        "age_seconds",
        "idle_seconds",
        "attempt_count",
        "invocation_count",
        "implementation_attempts",
        "ci_repair_attempts",
        "usage",
        "failure_reason",
        "failure_reason_truncated",
        _CALLS_FIELD,
        "is_finished",
        "is_stale",
        "stale",
        "review_status",
        "risk_assessment_enabled",
        "waiting_for_human",
        "state_started_at",
        "completed_at",
        "pull_request_url",
        "merge_commit_sha",
        "initial_route",
        "effective_route",
        "unattended",
        "needs_look",
        "restart_recoveries",
        "operator_escalation",
        "active_role",
        "resume_classification",
    }
)

#: Fields rendered on the run detail page (``/api/runs/{id}``), excluding the
#: ``attempts`` list itself (handled separately via ``ATTEMPT_FIELDS`` so each
#: attempt is independently minimized too).
RUN_DETAIL_FIELDS: frozenset[str] = (RUN_SUMMARY_FIELDS - {_CALLS_FIELD}) | frozenset(
    {
        "commit_sha",
        "guidance",
        "verification",
        "artifacts",
        "escalation",
        "escalation_route",
        "transitions",
    }
)

VERIFICATION_FIELDS: frozenset[str] = frozenset(
    {"passed", "check_count", "failed_check_count", "coverage_change"}
)

ESCALATION_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "target_type",
        "comment_url",
        "reason_code",
        "resume_classification",
        "waiting_for_human",
        "waiting_since",
        "episode_number",
        "reopen_count",
        "accepted_reply_count",
        "last_responder",
        "last_action",
        "last_response_at",
        "is_resumed",
        "resumed_at",
        "episode_id",
        "context_fingerprint",
        "max_reopens",
        "reply_closed_cause",
        "dashboard_action_refusal",
        "approval_scope",
        "decisions",
        "scope_paths",
    }
)

#: Fields rendered per attempt in the run detail's attempt history table.
#: Excludes ``reasoning``. ``failure_reason`` is redacted and bounded (see
#: module docstring).
ATTEMPT_FIELDS: frozenset[str] = frozenset(
    {
        "attempt_number",
        "role",
        "model",
        "budget",
        "triggered_by",
        "outcome",
        "started_at",
        "completed_at",
        "failure_reason",
        "failure_reason_truncated",
    }
)

#: Every key a sanitized call (``invocations[*]``) can carry. The call is built
#: key by key, so a provider field outside this set is dropped. ``performance``
#: is added only when the provider sent it.
INVOCATION_FIELDS: frozenset[str] = frozenset(
    {
        "invocation_number",
        "role",
        "purpose",
        "model",
        "reasoning",
        "context_tier",
        "status",
        "success",
        "attempt_number",
        "started_at",
        "completed_at",
        "duration_ms",
        "usage",
        "total_tokens",
        "failure_reason",
        "failure_reason_truncated",
        "performance",
        "runtime",
        "fallback_reason",
        "tool_calls",
    }
)

#: The active call: a finished call's shape without telemetry, plus tool count, output, limit.
ACTIVE_INVOCATION_FIELDS: frozenset[str] = (INVOCATION_FIELDS - {"performance"}) | {
    "tool_call_count",
    "last_output_at",
    "timeout_seconds",
}

_RUNTIME_NAMES: frozenset[str] = frozenset(runtime.value for runtime in RuntimeName)

#: Most tool calls one call sends, and the longest name or preview kept.
MAX_CALL_TOOL_CALLS = 40
TOOL_CALL_TEXT_LIMIT = 160

#: What the provider may say about the active call. Any other value shows as
#: ``running``.
ACTIVE_INVOCATION_STATUSES: frozenset[ActiveInvocationStatus] = frozenset(
    get_args(ActiveInvocationStatus)
)


def is_active_status(value: Any) -> bool:
    """Whether ``value`` is a status the run view can send for the active call."""
    return isinstance(value, str) and value in ACTIVE_INVOCATION_STATUSES


_ACTIVE_INPUT_KEYS = (
    "invocation_number",
    "role",
    "purpose",
    "model",
    "reasoning",
    "context_tier",
    "started_at",
    "attempt_number",
    "tool_calls",
)

#: Derived from ``total_nano_aiu``, so a provider value is never read or allowed.
_DERIVED_USAGE_FIELDS: frozenset[str] = frozenset({"usage_value_usd"})

USAGE_FIELDS: frozenset[str] = (
    frozenset(TOKEN_CLASS_FIELDS)
    | (frozenset(COST_UNIT_FIELDS) - _DERIVED_USAGE_FIELDS)
    | {
        "total_nano_aiu",
        "total_api_duration_ms",
        "session_duration_ms",
        "reported_invocations",
        "premium_request_cost",
    }
)

PROJECT_FIELDS: frozenset[str] = frozenset(
    {
        "project_id",
        "state",
        "delivery_mode",
        "delivery_repository",
        "delivery_base_branch",
        "integration_branch",
        "created_at",
        "updated_at",
        "completed_at",
        "task_count",
    }
)

PROJECT_TASK_FIELDS: frozenset[str] = frozenset(
    {
        "task_id",
        _TITLE_FIELD,
        "state",
        "run_id",
        "issue_url",
        "pull_request_url",
        "commit_sha",
        "merge_commit_sha",
    }
)

PROJECT_MODEL_FIELDS: frozenset[str] = frozenset(
    {
        "scope",
        "task_id",
        "invocation_number",
        "role",
        "purpose",
        "model",
        "context_tier",
        "success",
        "started_at",
        "completed_at",
        "usage",
        "status",
    }
)

#: The run file each guidance names. The words come from ``HALT_REASON_COPY``.
_GUIDANCE_ARTIFACTS: dict[HaltReasonCode, str] = {
    HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE: "review-acceptance.json",
    HaltReasonCode.REVIEW_IMPASSE: "review-impasse.json",
    HaltReasonCode.UNRESOLVED_DECISIONS: "execution-plan.json",
}

_GUIDANCE_CATEGORIES = frozenset(category.value for category in ReviewFindingCategory)


def _allowlist(data: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: data[key] for key in fields if key in data}


#: Name fields that hold free text: a run or task title and a model name.
_NAME_KEYS = (_TITLE_FIELD, "model")

#: Free text in a health report: a check message, its fix and the report error.
_HEALTH_TEXT_KEYS = ("message", "remediation", "error")


def _redacted(value: Any) -> str | None:
    """``value`` with secret shapes redacted when it is text, else ``None``."""
    return redact_secrets(value) if isinstance(value, str) else None


def _redact_keys(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """``data`` with the value under each present key redacted, in place."""
    for key in keys:
        if key in data:
            data[key] = _redacted(data[key])
    return data


def _redact_names(data: dict[str, Any]) -> dict[str, Any]:
    return _redact_keys(data, _NAME_KEYS)


#: Run fields kept only when their check passes; a value that fails is dropped.
_RUN_FIELD_CHECKS: dict[str, Callable[[Any], bool]] = {
    "source_external_id": lambda value: (
        isinstance(value, str) and _GITHUB_EXTERNAL_ID_PATTERN.fullmatch(value) is not None
    ),
    "waiting_for_human": lambda value: isinstance(value, bool),
    "risk_assessment_enabled": lambda value: isinstance(value, bool),
    "unattended": lambda value: isinstance(value, bool),
    "restart_recoveries": is_count,
    "pull_request_url": is_safe_https_url,
    "merge_commit_sha": lambda value: (
        isinstance(value, str) and _COMMIT_SHA_PATTERN.fullmatch(value) is not None
    ),
    "initial_route": lambda value: _short_token(value) is not None,
    "effective_route": lambda value: _short_token(value) is not None,
    "escalation_route": lambda value: _short_token(value) is not None,
    "active_role": lambda value: _short_token(value) is not None,
    "resume_classification": lambda value: value in RESUME_CLASSIFICATIONS,
}


def _bounded_reasons(value: Any) -> list[str]:
    """Each needs-look reason redacted and cut, at most :data:`MAX_NEEDS_LOOK_ITEMS` of them."""
    if not isinstance(value, list):
        return []
    return [
        bounded_reason(item, run_id=None)[0]
        for item in value[:MAX_NEEDS_LOOK_ITEMS]
        if isinstance(item, str) and item
    ]


def _sanitize_summary_fields(sanitized: dict[str, Any]) -> None:
    _redact_names(sanitized)
    for field, check in _RUN_FIELD_CHECKS.items():
        if field in sanitized and not check(sanitized[field]):
            sanitized.pop(field)
    if "needs_look" in sanitized:
        sanitized["needs_look"] = _bounded_reasons(sanitized["needs_look"])
    if "operator_escalation" in sanitized:
        sanitized["operator_escalation"] = _operator_escalation(sanitized["operator_escalation"])


#: ``OperatorEscalation.status`` in ``escalation_models.py``.
_OPERATOR_ESCALATION_STATUSES = frozenset({"running", "done", "failed"})


def _operator_escalation(value: Any) -> dict[str, Any] | None:
    """The last operator escalation: routes, time, status, and a redacted, cut note and error."""
    if not isinstance(value, dict) or value.get("status") not in _OPERATOR_ESCALATION_STATUSES:
        return None
    return {
        "from_route": _short_token(value.get("from_route")),
        "to_route": _short_token(value.get("to_route")),
        "started_at": timestamp(value.get("started_at")),
        "status": value["status"],
        "note": _bounded_text(value.get("note")),
        "error": _bounded_text(value.get("error")),
    }


def _bounded_text(value: Any) -> str | None:
    return bounded_reason(value, run_id=None)[0] if isinstance(value, str) and value else None


def sanitize_performance(raw: Any) -> dict[str, Any]:
    """Keep only the call timings the Steps view shows: runtime start and first event."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    return {
        key: float(data[key])
        for key in ("process_boot_ms", "first_event_ms")
        if is_number(data.get(key)) and data[key] >= 0
    }


def _sanitize_call_brief(raw: Any) -> dict[str, Any]:
    """One call of the run list: a role, a model name and a length."""
    data = raw if isinstance(raw, dict) else {}
    duration = data.get("duration_ms")
    return {
        "role": _short_token(data.get("role")),
        "model": _redacted(data.get("model")),
        "duration_ms": duration if is_count(duration) else None,
    }


def sanitize_run_summary(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run summary must serialize to a JSON object")
    sanitized = _allowlist(data, RUN_SUMMARY_FIELDS)
    _sanitize_summary_fields(sanitized)
    if "usage" in sanitized:
        sanitized["usage"] = sanitize_usage(sanitized["usage"])
    if _CALLS_FIELD in sanitized:
        calls = sanitized[_CALLS_FIELD]
        sanitized[_CALLS_FIELD] = (
            [_sanitize_call_brief(call) for call in calls] if isinstance(calls, list) else []
        )
    sanitized.update(_reason_fields(data.get("failure_reason"), run_id_of(data)))
    return sanitized


def _positive_int(value: Any) -> int | None:
    return value if is_positive_int(value) else None


def _short_token(value: Any) -> str | None:
    if isinstance(value, str) and _SHORT_TOKEN_PATTERN.fullmatch(value):
        return value
    return None


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    start = parse_timestamp(started_at)
    end = parse_timestamp(completed_at)
    if start is None or end is None or end < start:
        return None
    return (end - start) // timedelta(milliseconds=1)


def _reason_fields(value: Any, run_id: str | None) -> dict[str, Any]:
    """Redacted, bounded ``failure_reason`` and its ``failure_reason_truncated`` flag."""
    if not isinstance(value, str) or not value:
        return {"failure_reason": None, "failure_reason_truncated": False}
    reason, truncated = bounded_reason(value, run_id=run_id)
    return {"failure_reason": reason, "failure_reason_truncated": truncated}


def sanitize_attempt(raw: Any, run_id: str | None = None) -> dict[str, Any]:
    """Reduce one attempt record to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized = _redact_names(_allowlist(data, ATTEMPT_FIELDS))
    sanitized.update(_reason_fields(data.get("failure_reason"), run_id))
    return sanitized


def sanitize_usage(raw: Any) -> dict[str, Any]:
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in _allowlist(data, USAGE_FIELDS).items():
        if is_number(value) and value >= 0:
            sanitized[key] = value
    total_nano_aiu = sanitized.get("total_nano_aiu")
    if total_nano_aiu is not None:
        sanitized["usage_value_usd"] = total_nano_aiu / NANO_AIU_PER_USD
    return sanitized


def _call_usage(raw: Any) -> dict[str, Any]:
    usage = sanitize_usage(raw)
    for key in (*TOKEN_CLASS_FIELDS, *COST_UNIT_FIELDS):
        usage.setdefault(key, None)
    return usage


def _outcome_status(success: Any) -> str | None:
    if isinstance(success, bool):
        return STATUS_SUCCESS if success else STATUS_FAILED
    return None


def _sanitize_call(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    success = data.get("success")
    usage = _call_usage(data.get("usage"))
    call = {
        "invocation_number": _positive_int(data.get("invocation_number")),
        "role": data.get("role"),
        "purpose": _short_token(data.get("purpose")),
        "model": data.get("model"),
        "reasoning": _short_token(data.get("reasoning")),
        "context_tier": data.get("context_tier"),
        "status": _outcome_status(success),
        "success": success if isinstance(success, bool) else None,
        "attempt_number": _positive_int(data.get("attempt_number")),
        "started_at": timestamp(data.get("started_at")),
        "completed_at": timestamp(data.get("completed_at")),
        "duration_ms": _duration_ms(data.get("started_at"), data.get("completed_at")),
        "usage": usage,
        "total_tokens": call_total_tokens(usage),
        **_reason_fields(data.get("failure_reason"), run_id),
        **_served_fields(data, run_id),
    }
    return _redact_names(call)


def sanitize_invocation(raw: Any, run_id: str | None = None) -> dict[str, Any]:
    """Reduce one finished call to its timeline fields, in a fixed order."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized = _sanitize_call(data, run_id)
    if "performance" in data:
        sanitized["performance"] = sanitize_performance(data["performance"])
    return sanitized


#: An absolute POSIX path that does not follow a word, a dot, a colon or a slash
#: (so the path part of a URL and a relative path stay as they are).
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.:/~])~?/(?:[^\s/'\"`]+/)*[^\s/'\"`]+")
_IN_WORKTREE_RE = re.compile(r"/worktrees/[^/]+/(.+)")


def _relative_path(match: re.Match[str]) -> str:
    """A path inside a worktree from the worktree root; any other path by its last part."""
    path = match.group(0)
    inside = _IN_WORKTREE_RE.search(path)
    return inside.group(1) if inside else path.rsplit("/", 1)[-1]


def _tool_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = redact_secrets(" ".join(value.split()))
    return _ABSOLUTE_PATH_RE.sub(_relative_path, text)[:TOOL_CALL_TEXT_LIMIT]


def _sanitize_tool_calls(raw: Any) -> list[dict[str, str]]:
    """Each tool call as a redacted, bounded name and one-line preview."""
    if not isinstance(raw, list):
        return []
    return [
        {"name": _tool_text(item.get("name")), "preview": _tool_text(item.get("preview"))}
        for item in raw[:MAX_CALL_TOOL_CALLS]
        if isinstance(item, dict) and _tool_text(item.get("name"))
    ]


def _served_fields(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    """Which runtime served a finished call, why a fallback did, and its tool calls."""
    runtime = data.get("runtime")
    fallback = data.get("fallback_reason")
    return {
        "runtime": runtime if runtime in _RUNTIME_NAMES else None,
        "fallback_reason": (
            bounded_reason(fallback, run_id=run_id)[0]
            if isinstance(fallback, str) and fallback
            else None
        ),
        "tool_calls": _sanitize_tool_calls(data.get("tool_calls")),
    }


def sanitize_active_invocation(raw: Any, run_id: str | None = None) -> dict[str, Any]:
    """Reduce the call that has not finished to the same shape as a finished call."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    call = _sanitize_call({key: data.get(key) for key in _ACTIVE_INPUT_KEYS}, run_id)
    call["status"] = status if is_active_status(status := data.get("status")) else "running"
    count = data.get("tool_call_count")
    call["tool_call_count"] = count if is_count(count) else len(call["tool_calls"])
    call["last_output_at"] = timestamp(data.get("last_output_at"))
    call["timeout_seconds"] = _positive_int(data.get("timeout_seconds"))
    return call


def _call_order(call: dict[str, Any]) -> tuple[bool, int]:
    number = call.get("invocation_number")
    return (number is None, number or 0)


def _drop_invalid(sanitized: dict[str, Any], checks: dict[str, Callable[[Any], bool]]) -> None:
    """Remove each checked key whose value fails its check."""
    for key, is_valid in checks.items():
        if key in sanitized and not is_valid(sanitized[key]):
            del sanitized[key]


_VERIFICATION_CHECKS: dict[str, Callable[[Any], bool]] = {
    "passed": lambda value: isinstance(value, bool),
    "check_count": is_count,
    "failed_check_count": is_count,
    "coverage_change": lambda value: value is None or is_number(value),
}


def _sanitize_verification(verification: dict[str, Any]) -> dict[str, Any]:
    safe = _allowlist(verification, VERIFICATION_FIELDS)
    _drop_invalid(safe, _VERIFICATION_CHECKS)
    return safe


def _is_github_login(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and bool(_GITHUB_LOGIN_PATTERN.fullmatch(value))
    )


def _one_of(allowed: Collection[str | None]) -> Callable[[Any], bool]:
    """Accept a string or None from ``allowed``; an unhashable value is invalid, not an error."""
    return lambda value: (value is None or isinstance(value, str)) and value in allowed


_ESCALATION_CHECKS: dict[str, Callable[[Any], bool]] = {
    "status": _one_of(ESCALATION_STATUSES),
    "target_type": _one_of(ESCALATION_TARGET_TYPES),
    "comment_url": is_safe_https_url,
    "reason_code": _one_of(HALT_REASON_CODES),
    "resume_classification": _one_of(RESUME_CLASSIFICATIONS),
    "waiting_for_human": lambda value: isinstance(value, bool),
    "is_resumed": lambda value: isinstance(value, bool),
    "episode_number": is_count,
    "reopen_count": is_count,
    "accepted_reply_count": is_count,
    "last_responder": _is_github_login,
    "last_action": _one_of({None, "ANSWER", "RESUME"}),
    "episode_id": is_episode_id,
    "context_fingerprint": is_context_fingerprint,
    "max_reopens": is_count,
    "reply_closed_cause": _one_of({None, *REPLY_CLOSED_CAUSES}),
    "dashboard_action_refusal": _one_of({None, *RESUME_REFUSALS}),
}


#: The most items kept in one approval scope list. A longer list is dropped whole, because
#: a part of what an approval allows would mislead the person who reads it.
MAX_SCOPE_ITEMS = 24
#: A project gives at most a few reasons per task, and a plan has at most 12 tasks.
MAX_NEEDS_LOOK_ITEMS = 48


def _text_list(value: Any, run_id: str | None, limit: int) -> list[str] | None:
    """Redacted, bounded copy of a list of 1 to ``limit`` non-empty strings, else ``None``."""
    if not isinstance(value, list) or not 0 < len(value) <= limit:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return [bounded_reason(item, run_id=run_id)[0] for item in value]


def _clean_approval_scope(value: Any, run_id: str | None) -> dict[str, Any] | None:
    """The approval scope with every text redacted and bounded, or ``None`` when malformed."""
    if not isinstance(value, dict):
        return None
    requested = value.get("decision_requested")
    lists = {
        key: _text_list(value.get(key), run_id, MAX_SCOPE_ITEMS)
        for key in ("authorized_actions", "unauthorized_actions", "conditions_in_force")
    }
    if not isinstance(requested, str) or not requested or None in lists.values():
        return None
    return {"decision_requested": bounded_reason(requested, run_id=run_id)[0], **lists}


def _sanitize_escalation(escalation: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    safe = _allowlist(escalation, ESCALATION_FIELDS)
    _drop_invalid(safe, _ESCALATION_CHECKS)
    _clean_escalation_text(safe, run_id)
    return safe


def _clean_escalation_text(safe: dict[str, Any], run_id: str | None) -> None:
    """Redact and bound the approval scope, decision questions and scope paths, else drop them.

    A list over its limit is dropped, never cut: a reply answers every question and an
    approval covers every path.
    """
    scope = _clean_approval_scope(safe.pop("approval_scope", None), run_id)
    if scope is not None:
        safe["approval_scope"] = scope
    for key, limit in (
        ("decisions", MAX_PLAN_DECISIONS),
        ("scope_paths", MAX_SCOPE_APPROVAL_PATHS),
    ):
        items = _text_list(safe.pop(key, None), run_id, limit)
        if items is not None:
            safe[key] = items


def sanitize_run_detail(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run detail to only safe, known fields.

    Handles ``attempts`` and ``invocations`` specially: each entry is
    independently sanitized through :func:`sanitize_attempt` and
    :func:`sanitize_invocation` rather than passed through as-is, so an
    attempt carrying (for example) captured command output cannot leak just
    because the surrounding run object was otherwise safe.
    """
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run detail must serialize to a JSON object")
    sanitized = _allowlist(data, RUN_DETAIL_FIELDS)
    _sanitize_summary_fields(sanitized)
    if "usage" in sanitized:
        sanitized["usage"] = sanitize_usage(sanitized["usage"])
    run_id = run_id_of(data)
    sanitized.update(_reason_fields(data.get("failure_reason"), run_id))
    sanitized.update(_sanitize_calls(data, run_id))
    sanitized["transitions"] = sanitize_transitions(data.get("transitions"), run_id)
    sanitized.update(_sanitize_detail_sections(data, run_id))
    return sanitized


def _sanitize_calls(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    """Attempts, calls in number order and the active call, each when provided."""
    sanitized: dict[str, Any] = {}
    attempts = data.get("attempts")
    if isinstance(attempts, list):
        sanitized["attempts"] = [sanitize_attempt(item, run_id) for item in attempts]
    invocations = data.get("invocations")
    if isinstance(invocations, list):
        calls = sorted((sanitize_invocation(item, run_id) for item in invocations), key=_call_order)
        sanitized["invocations"] = calls
    active_invocation = data.get("active_invocation")
    if isinstance(active_invocation, dict):
        sanitized["active_invocation"] = sanitize_active_invocation(active_invocation, run_id)
    return sanitized


def _sanitize_detail_sections(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    """Guidance, verification, artifacts and escalation, each when provided."""
    sanitized: dict[str, Any] = {}
    guidance = data.get("guidance")
    if isinstance(guidance, dict):
        sanitized_guidance = _sanitize_guidance(guidance)
        if sanitized_guidance is not None:
            sanitized["guidance"] = sanitized_guidance
    verification = data.get("verification")
    if isinstance(verification, dict):
        sanitized["verification"] = _sanitize_verification(verification)
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        sanitized["artifacts"] = sorted(
            {item for item in artifacts if isinstance(item, str) and item in _SAFE_ARTIFACT_NAMES}
        )
    escalation = data.get("escalation")
    if isinstance(escalation, dict):
        sanitized["escalation"] = _sanitize_escalation(escalation, run_id)
    return sanitized


def _valid_finding_id(item: Any) -> bool:
    return (
        isinstance(item, str)
        and item.startswith("review-")
        and len(item) <= 64
        and item.replace("-", "").isalnum()
    )


def _review_guidance_extras(data: dict[str, Any]) -> dict[str, Any]:
    """The finding count, ids and category counts of review guidance, each when valid."""
    extras: dict[str, Any] = {}
    count = data.get("finding_count")
    if is_count(count) and count <= MAX_GUIDANCE_FINDINGS:
        extras["finding_count"] = count
    finding_ids = data.get("finding_ids")
    if isinstance(finding_ids, list):
        extras["finding_ids"] = [
            item for item in finding_ids[:MAX_GUIDANCE_FINDINGS] if _valid_finding_id(item)
        ]
    category_counts = data.get("category_counts")
    if isinstance(category_counts, dict):
        extras["category_counts"] = {
            key: value
            for key, value in category_counts.items()
            if key in _GUIDANCE_CATEGORIES and is_count(value) and value <= MAX_GUIDANCE_FINDINGS
        }
    return extras


def _sanitize_guidance(data: dict[str, Any]) -> dict[str, Any] | None:
    reason_code = HaltReasonCode.parse(data.get("reason_code"))
    if reason_code is None:
        return None
    copy = HALT_REASON_COPY[reason_code]
    is_plan_decision = reason_code is HaltReasonCode.UNRESOLVED_DECISIONS
    next_action = copy.next_action
    if is_plan_decision and data.get("next_action") != next_action:
        next_action = UNRESOLVED_DECISIONS_REPLACE_ACTION
    accepted = reason_code is HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE
    result: dict[str, Any] = {
        "status": "ACCEPTED_WITH_FINDINGS" if accepted else "ACTION_REQUIRED",
        "reason_code": reason_code,
        "summary": copy.summary,
        "next_action": next_action,
    }
    artifact = _GUIDANCE_ARTIFACTS.get(reason_code)
    if artifact is not None:
        result["artifact"] = artifact
    if not is_plan_decision:
        result.update(_review_guidance_extras(data))
        return result
    decision_count = data.get("decision_count")
    if is_count(decision_count) and decision_count <= MAX_PLAN_DECISIONS:
        result["decision_count"] = decision_count
    return result


def sanitize_project(raw: Any) -> dict[str, Any]:
    """Reduce one project to state, task and delivery identifiers only."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("project summary must serialize to a JSON object")
    sanitized = _allowlist(data, PROJECT_FIELDS)
    if isinstance(data.get("needs_look"), list):
        sanitized["needs_look"] = _bounded_reasons(data["needs_look"])
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        sanitized["tasks"] = [
            _redact_names(_allowlist(task, PROJECT_TASK_FIELDS))
            for task in tasks
            if isinstance(task, dict)
        ]
    models = data.get("models")
    if isinstance(models, list):
        sanitized["models"] = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_data = _redact_names(_allowlist(model, PROJECT_MODEL_FIELDS))
            model_data["status"] = model_data.get("status") or _outcome_status(
                model_data.get("success")
            )
            if "usage" in model_data:
                model_data["usage"] = sanitize_usage(model_data["usage"])
            sanitized["models"].append(model_data)
    return sanitized


#: Fields of ``/api/summary``: the two the page reads. The snapshot behind it also lists
#: tallies keyed by model name, which nothing here redacts, so none of it is passed on.
SUMMARY_FIELDS: frozenset[str] = frozenset({"overview", "health"})


def sanitize_summary(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` cut down to :data:`SUMMARY_FIELDS`."""
    return _allowlist(data, SUMMARY_FIELDS)


HEALTH_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "generated_at",
        "stale_after_seconds",
        "max_scanned_runs",
        "total_runs",
        "scanned_runs",
        "scan_truncated",
        "unreadable_runs",
        "degraded",
        "degraded_reasons",
        "lock_check_supported",
        "locks_checked",
        "workspaces_checked",
        "stale_runs",
        "stale_locks",
        "orphaned_workspaces",
        "status",
        "success",
        "checks",
        "error",
    }
)

STALE_RUN_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "work_item_id",
        "state",
        "idle_seconds",
    }
)

STALE_LOCK_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "lock_name",
        "modified_at",
    }
)

ORPHANED_WORKSPACE_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "workspace_name",
        "modified_at",
    }
)


def _redact_health_text(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with its free text redacted in place: one check, or the whole report."""
    _redact_keys(report, _HEALTH_TEXT_KEYS)
    reasons = report.get("degraded_reasons")
    if isinstance(reasons, list):
        report["degraded_reasons"] = [_redacted(reason) for reason in reasons]
    return report


def sanitize_health(raw: Any) -> dict[str, Any] | None:
    """Sanitize operational health report for dashboard JSON responses.

    Strictly allowlists fields and strips all absolute workspace paths
    (such as StaleRunFinding.workspace_path).
    """
    if raw is None:
        return None
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return None

    sanitized = _redact_health_text(_allowlist(data, HEALTH_ALLOWED_FIELDS))

    stale_runs = data.get("stale_runs")
    if isinstance(stale_runs, list):
        sanitized_stale_runs = []
        for item in stale_runs:
            item_safe = to_json_safe(item)
            if isinstance(item_safe, dict):
                sanitized_stale_runs.append(_allowlist(item_safe, STALE_RUN_ALLOWED_FIELDS))
        sanitized["stale_runs"] = sanitized_stale_runs

    stale_locks = data.get("stale_locks")
    if isinstance(stale_locks, list):
        sanitized_stale_locks = []
        for item in stale_locks:
            item_safe = to_json_safe(item)
            if isinstance(item_safe, dict):
                sanitized_stale_locks.append(_allowlist(item_safe, STALE_LOCK_ALLOWED_FIELDS))
        sanitized["stale_locks"] = sanitized_stale_locks

    orphaned_workspaces = data.get("orphaned_workspaces")
    if isinstance(orphaned_workspaces, list):
        sanitized_orphaned = []
        for item in orphaned_workspaces:
            item_safe = to_json_safe(item)
            if isinstance(item_safe, dict):
                sanitized_orphaned.append(_allowlist(item_safe, ORPHANED_WORKSPACE_ALLOWED_FIELDS))
        sanitized["orphaned_workspaces"] = sanitized_orphaned

    checks = data.get("checks")
    if isinstance(checks, list):
        sanitized_checks = []
        for check in checks:
            check_safe = to_json_safe(check)
            if isinstance(check_safe, dict):
                sanitized_checks.append(
                    _redact_health_text(
                        _allowlist(
                            check_safe, frozenset({"name", "status", "message", "remediation"})
                        )
                    )
                )
        sanitized["checks"] = sanitized_checks

    return sanitized
