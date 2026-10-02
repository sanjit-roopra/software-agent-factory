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

Nothing here composes a view model. :mod:`software_agent_factory.dashboard.view`
adds the run totals and the next step to what these functions return.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from ..escalation_protocol import MAX_PLAN_DECISIONS, REPLY_CLOSED_CAUSES
from ..redaction import bounded_reason, redact_secrets
from ..store import ARTIFACT_FILENAMES
from .aggregate import (
    COST_UNIT_FIELDS,
    STATUS_FAILED,
    STATUS_SUCCESS,
    TOKEN_CLASS_FIELDS,
    call_total_tokens,
)
from .snapshot import to_json_safe
from .validators import (
    ESCALATION_STATUSES,
    ESCALATION_TARGET_TYPES,
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
_MODEL_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_SHORT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_ARTIFACT_NAMES = frozenset(ARTIFACT_FILENAMES.values())

#: The one spelling of the title field: it is allowlisted and redacted by this name.
_TITLE_FIELD = "title"

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
        "implementation_attempts",
        "ci_repair_attempts",
        "is_finished",
        "is_stale",
        "stale",
        "review_status",
        "requested_performance_mode",
        "effective_performance_mode",
        "performance_model_profile",
        "risk_assessment_enabled",
        "waiting_for_human",
        "performance",
    }
)

#: Fields rendered on the run detail page (``/api/runs/{id}``), excluding the
#: ``attempts`` list itself (handled separately via ``ATTEMPT_FIELDS`` so each
#: attempt is independently minimized too).
RUN_DETAIL_FIELDS: frozenset[str] = RUN_SUMMARY_FIELDS | frozenset(
    {
        "completed_at",
        "commit_sha",
        "pull_request_url",
        "merge_commit_sha",
        "invocation_count",
        "usage",
        "guidance",
        "verification",
        "artifacts",
        "escalation",
        "failure_reason",
        "failure_reason_truncated",
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
        "reopen_max",
        "reply_closed_cause",
        "dashboard_action_refusal",
        "approval_scope",
        "decisions",
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
    }
)

#: The active call has the same shape as a finished call, without telemetry.
ACTIVE_INVOCATION_FIELDS: frozenset[str] = INVOCATION_FIELDS - {"performance"}

#: What the provider may say about the active call. Any other value shows as
#: ``running``.
ACTIVE_INVOCATION_STATUSES: frozenset[str] = frozenset({"running", "stale", "crashed", "abandoned"})

_ACTIVE_INPUT_KEYS = (
    "invocation_number",
    "role",
    "purpose",
    "model",
    "reasoning",
    "context_tier",
    "started_at",
    "attempt_number",
)

#: ``usage_value_usd`` is derived from ``total_nano_aiu``; a provider value for
#: it is never read, so it stays out of the allowlist.
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

NANO_AIU_PER_USD = 100_000_000_000
GUIDANCE_COPY: dict[str, tuple[str, str, str, str | None]] = {
    "BOUNDED_REVIEW_ACCEPTANCE": (
        "ACCEPTED_WITH_FINDINGS",
        "The controller continued after the bounded review limit.",
        "Review the accepted findings in the pull request before merging.",
        "review-acceptance.json",
    ),
    "REVIEW_IMPASSE": (
        "ACTION_REQUIRED",
        "Independent review did not converge within the safe automatic policy.",
        "Inspect review-impasse.json, resolve or accept the listed findings, then retry.",
        "review-impasse.json",
    ),
    "UNRESOLVED_DECISIONS": (
        "ACTION_REQUIRED",
        "The execution plan has unresolved architectural decisions.",
        "Reply with complete numbered decisions on the escalation thread.",
        "execution-plan.json",
    ),
    "RISK_APPROVAL": (
        "ACTION_REQUIRED",
        "The run requires approval under the configured risk policy.",
        "Review the work item risk and approve or change the policy before retrying.",
        None,
    ),
    "SCOPE_REVIEW": (
        "ACTION_REQUIRED",
        "The proposed changes exceeded the approved scope.",
        "Review the planned and changed files, then update the scope or retry.",
        None,
    ),
    "ATTEMPT_BUDGET_EXHAUSTED": (
        "ACTION_REQUIRED",
        "The run exhausted a bounded retry budget.",
        "Inspect the run artifacts, correct the underlying issue, then retry.",
        None,
    ),
    "CI_INTERVENTION": (
        "ACTION_REQUIRED",
        "CI could not be completed or repaired automatically.",
        "Inspect the pull request checks, fix the failing check, then retry delivery.",
        None,
    ),
    "DELIVERY_INTERVENTION": (
        "ACTION_REQUIRED",
        "The controller could not complete pull request delivery.",
        "Check repository permissions and delivery settings, then retry delivery.",
        None,
    ),
    "RECOVERY_INTERVENTION": (
        "ACTION_REQUIRED",
        "The run could not safely recover its persisted workspace.",
        "Inspect the run and workspace metadata before starting a replacement run.",
        None,
    ),
    "MANUAL_INSPECTION": (
        "ACTION_REQUIRED",
        "The controller stopped at a manual decision boundary.",
        "Inspect the typed run artifacts and decide whether to retry or replace the run.",
        None,
    ),
}


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


def _sanitize_summary_fields(sanitized: dict[str, Any]) -> None:
    _redact_names(sanitized)
    external_id = sanitized.get("source_external_id")
    if not isinstance(external_id, str) or not _GITHUB_EXTERNAL_ID_PATTERN.fullmatch(external_id):
        sanitized.pop("source_external_id", None)
    for key in ("requested_performance_mode", "effective_performance_mode"):
        if sanitized.get(key) not in {"standard", "fast"}:
            sanitized.pop(key, None)
    model_profile = sanitized.get("performance_model_profile")
    if model_profile is not None and (
        not isinstance(model_profile, str) or not _MODEL_PROFILE_PATTERN.fullmatch(model_profile)
    ):
        sanitized.pop("performance_model_profile", None)
    for flag in ("waiting_for_human", "risk_assessment_enabled"):
        if not isinstance(sanitized.get(flag), bool):
            sanitized.pop(flag, None)


def sanitize_performance(raw: Any) -> dict[str, Any]:
    """Reduce one performance record to bounded, safe numeric telemetry."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key in ("prompt_chars", "response_chars"):
        val = data.get(key)
        if is_count(val):
            sanitized[key] = val
    for key in ("process_boot_ms", "first_event_ms"):
        val = data.get(key)
        if is_number(val) and val >= 0:
            sanitized[key] = float(val)
    durations = data.get("durations_ms")
    if isinstance(durations, dict):
        sanitized["durations_ms"] = {
            str(k)[:64]: float(v)
            for k, v in list(durations.items())[:100]
            if is_number(v) and v >= 0
        }
    counters = data.get("counters")
    if isinstance(counters, dict):
        sanitized["counters"] = {
            str(k)[:64]: int(v) for k, v in list(counters.items())[:100] if is_count(v)
        }
    return sanitized


def sanitize_run_summary(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run summary must serialize to a JSON object")
    sanitized = _allowlist(data, RUN_SUMMARY_FIELDS)
    _sanitize_summary_fields(sanitized)
    if "performance" in sanitized:
        sanitized["performance"] = sanitize_performance(sanitized["performance"])
    return sanitized


def _positive_int(value: Any) -> int | None:
    return value if is_positive_int(value) else None


def _short_token(value: Any) -> str | None:
    if isinstance(value, str) and _SHORT_TOKEN_PATTERN.fullmatch(value):
        return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO timestamp; anything else is unreported."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _timestamp(value: Any) -> str | None:
    return value if _parse_timestamp(value) is not None else None


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(completed_at)
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
        "started_at": _timestamp(data.get("started_at")),
        "completed_at": _timestamp(data.get("completed_at")),
        "duration_ms": _duration_ms(data.get("started_at"), data.get("completed_at")),
        "usage": usage,
        "total_tokens": call_total_tokens(usage),
        **_reason_fields(data.get("failure_reason"), run_id),
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


def sanitize_active_invocation(raw: Any, run_id: str | None = None) -> dict[str, Any]:
    """Reduce the call that has not finished to the same shape as a finished call."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    call = _sanitize_call({key: data.get(key) for key in _ACTIVE_INPUT_KEYS}, run_id)
    status = data.get("status")
    known = isinstance(status, str) and status in ACTIVE_INVOCATION_STATUSES
    call["status"] = status if known else "running"
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
    "reason_code": _one_of(GUIDANCE_COPY.keys()),
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
    "reopen_max": is_count,
    "reply_closed_cause": _one_of({None, *REPLY_CLOSED_CAUSES}),
    "dashboard_action_refusal": _one_of({None, *RESUME_REFUSALS}),
}


#: The most items kept in one approval scope list. A longer list is dropped whole, because
#: a part of what an approval allows would mislead the person who reads it.
MAX_SCOPE_ITEMS = 24


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
    """Redact and bound the approval scope and the decision questions, or drop them when malformed.

    Decisions are all or nothing: a reply must answer every numbered question, so a
    list over :data:`MAX_PLAN_DECISIONS` is dropped, never cut.
    """
    if "approval_scope" in safe:
        scope = _clean_approval_scope(safe["approval_scope"], run_id)
        if scope is None:
            del safe["approval_scope"]
        else:
            safe["approval_scope"] = scope
    if "decisions" in safe:
        decisions = _text_list(safe["decisions"], run_id, MAX_PLAN_DECISIONS)
        if decisions is None:
            del safe["decisions"]
        else:
            safe["decisions"] = decisions


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
    if "performance" in sanitized:
        sanitized["performance"] = sanitize_performance(sanitized["performance"])
    run_id = run_id_of(data)
    sanitized.update(_reason_fields(data.get("failure_reason"), run_id))
    sanitized.update(_sanitize_calls(data, run_id))
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


def _sanitize_guidance(data: dict[str, Any]) -> dict[str, Any] | None:
    reason_code = data.get("reason_code")
    if not isinstance(reason_code, str) or reason_code not in GUIDANCE_COPY:
        return None
    status, summary, next_action, artifact = GUIDANCE_COPY[reason_code]
    plan_reply_action = "Reply with complete numbered decisions on the escalation thread."
    if reason_code == "UNRESOLVED_DECISIONS" and data.get("next_action") == plan_reply_action:
        next_action = plan_reply_action
    elif reason_code == "UNRESOLVED_DECISIONS":
        next_action = (
            "Inspect execution-plan.json, resolve the decisions, then start a replacement run."
        )
    result: dict[str, Any] = {
        "status": status,
        "reason_code": reason_code,
        "summary": summary,
        "next_action": next_action,
    }
    if artifact is not None:
        result["artifact"] = artifact
    if reason_code != "UNRESOLVED_DECISIONS":
        count = data.get("finding_count")
        if is_count(count) and count <= 12:
            result["finding_count"] = count
        finding_ids = data.get("finding_ids")
        if isinstance(finding_ids, list):
            result["finding_ids"] = [
                item
                for item in finding_ids[:12]
                if isinstance(item, str)
                and item.startswith("review-")
                and len(item) <= 64
                and item.replace("-", "").isalnum()
            ]
        category_counts = data.get("category_counts")
        if isinstance(category_counts, dict):
            result["category_counts"] = {
                key: value
                for key, value in category_counts.items()
                if key in {"CORRECTNESS", "SCOPE", "SECURITY", "COMPATIBILITY"}
                and is_count(value)
                and value <= 12
            }
    else:
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
