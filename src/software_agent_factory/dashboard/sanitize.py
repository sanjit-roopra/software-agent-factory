"""Response data minimization: field allowlists applied inside the handler.

Every provider in :mod:`software_agent_factory.dashboard.snapshot` is trusted
to already return dashboard-safe data -- but "trusted" is not "enforced", and
a future provider (or a bug in one) could accidentally include a command log,
a diff, a prompt, tool output, a token/secret, or free-form failure text in
its payload. This module is the second, independent line of defense: the
handler allowlists exactly the fields the UI actually renders and drops
everything else, so a provider mistake can leak at most an unused-but-safe
field name, never its content.

``failure_reason`` is deliberately excluded from every allowlist below, on
both a run and an attempt. It is free-form text that could contain repository
content, and nothing in this package can verify a provider redacted it before
returning it, so the safe default is to omit it entirely rather than trust an
unenforceable "already redacted" claim.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..store import ARTIFACT_FILENAMES
from .snapshot import to_json_safe

_GITHUB_EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_MODEL_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_GITHUB_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_SAFE_ARTIFACT_NAMES = frozenset(ARTIFACT_FILENAMES.values())

#: Fields rendered in the paginated run table (``/api/runs``). Includes both
#: ``run_id`` (the real ``observability.RunSummary`` field name) and ``id``
#: (accepted from simpler providers/tests) since the client tolerates either.
RUN_SUMMARY_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "id",
        "work_item_id",
        "source_external_id",
        "title",
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
    }
)

#: Fields rendered per attempt in the run detail's attempt history table.
#: Explicitly excludes ``reasoning`` (free-form model justification text) and
#: ``failure_reason`` (see module docstring): neither is a command log or a
#: diff, but both are unbounded free text this package has no way to vet.
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
    }
)

INVOCATION_FIELDS: frozenset[str] = frozenset(
    {
        "invocation_number",
        "role",
        "model",
        "context_tier",
        "success",
        "usage",
        "performance",
    }
)

ACTIVE_INVOCATION_FIELDS: frozenset[str] = frozenset(
    {
        "invocation_number",
        "role",
        "purpose",
        "model",
        "context_tier",
        "status",
        "started_at",
        "attempt_number",
    }
)

USAGE_FIELDS: frozenset[str] = frozenset(
    {
        "total_premium_request_cost",
        "total_nano_aiu",
        "total_api_duration_ms",
        "session_duration_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
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
        "title",
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


def _is_safe_https_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def _sanitize_summary_fields(data: dict[str, Any], sanitized: dict[str, Any]) -> None:
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
    if not isinstance(sanitized.get("waiting_for_human"), bool):
        sanitized.pop("waiting_for_human", None)


def sanitize_performance(raw: Any) -> dict[str, Any]:
    """Reduce one performance record to bounded, safe numeric telemetry."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key in ("prompt_chars", "response_chars"):
        val = data.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            sanitized[key] = val
    for key in ("process_boot_ms", "first_event_ms"):
        val = data.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 0:
            sanitized[key] = float(val)
    durations = data.get("durations_ms")
    if isinstance(durations, dict):
        sanitized["durations_ms"] = {
            str(k)[:64]: float(v)
            for k, v in list(durations.items())[:100]
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0
        }
    counters = data.get("counters")
    if isinstance(counters, dict):
        sanitized["counters"] = {
            str(k)[:64]: int(v)
            for k, v in list(counters.items())[:100]
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0
        }
    return sanitized


def sanitize_run_summary(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run summary must serialize to a JSON object")
    sanitized = _allowlist(data, RUN_SUMMARY_FIELDS)
    _sanitize_summary_fields(data, sanitized)
    if "performance" in sanitized:
        sanitized["performance"] = sanitize_performance(sanitized["performance"])
    return sanitized


def sanitize_attempt(raw: Any) -> dict[str, Any]:
    """Reduce one attempt record to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    return _allowlist(data, ATTEMPT_FIELDS)


def sanitize_usage(raw: Any) -> dict[str, Any]:
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, value in _allowlist(data, USAGE_FIELDS).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            sanitized[key] = value
    total_nano_aiu = sanitized.get("total_nano_aiu")
    if total_nano_aiu is not None:
        sanitized["usage_value_usd"] = total_nano_aiu / NANO_AIU_PER_USD
    return sanitized


def sanitize_invocation(raw: Any) -> dict[str, Any]:
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized = _allowlist(data, INVOCATION_FIELDS)
    if "usage" in sanitized:
        sanitized["usage"] = sanitize_usage(sanitized["usage"])
    if "performance" in sanitized:
        sanitized["performance"] = sanitize_performance(sanitized["performance"])
    return sanitized


def sanitize_run_detail(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run detail to only safe, known fields.

    Handles ``attempts`` specially: each entry is independently sanitized
    through :func:`sanitize_attempt` rather than passed through as-is, so an
    attempt carrying (for example) captured command output cannot leak just
    because the surrounding run object was otherwise safe.
    """
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run detail must serialize to a JSON object")
    sanitized = _allowlist(data, RUN_DETAIL_FIELDS)
    _sanitize_summary_fields(data, sanitized)
    if "usage" in sanitized:
        sanitized["usage"] = sanitize_usage(sanitized["usage"])
    if "performance" in sanitized:
        sanitized["performance"] = sanitize_performance(sanitized["performance"])
    attempts = data.get("attempts")
    if isinstance(attempts, list):
        sanitized["attempts"] = [sanitize_attempt(item) for item in attempts]
    invocations = data.get("invocations")
    if isinstance(invocations, list):
        sanitized["invocations"] = [sanitize_invocation(item) for item in invocations]
    active_invocation = data.get("active_invocation")
    if isinstance(active_invocation, dict):
        sanitized["active_invocation"] = _allowlist(active_invocation, ACTIVE_INVOCATION_FIELDS)
    guidance = data.get("guidance")
    if isinstance(guidance, dict):
        sanitized_guidance = _sanitize_guidance(guidance)
        if sanitized_guidance is not None:
            sanitized["guidance"] = sanitized_guidance
    verification = data.get("verification")
    if isinstance(verification, dict):
        safe_verification = _allowlist(verification, VERIFICATION_FIELDS)
        for key in ("passed",):
            if not isinstance(safe_verification.get(key), bool):
                safe_verification.pop(key, None)
        for key in ("check_count", "failed_check_count"):
            value = safe_verification.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                safe_verification.pop(key, None)
        coverage = safe_verification.get("coverage_change")
        if coverage is not None and (
            not isinstance(coverage, (int, float)) or isinstance(coverage, bool)
        ):
            safe_verification.pop("coverage_change", None)
        sanitized["verification"] = safe_verification
    artifacts = data.get("artifacts")
    if isinstance(artifacts, list):
        sanitized["artifacts"] = sorted(
            {item for item in artifacts if isinstance(item, str) and item in _SAFE_ARTIFACT_NAMES}
        )
    escalation = data.get("escalation")
    if isinstance(escalation, dict):
        safe_escalation = _allowlist(escalation, ESCALATION_FIELDS)
        if safe_escalation.get("status") not in {
            "PENDING_NOTIFICATION",
            "NOTIFIED",
            "NOTIFICATION_FAILED",
            "REOPENED",
            "RESUMED",
            "EXPIRED",
        }:
            safe_escalation.pop("status", None)
        if safe_escalation.get("target_type") not in {None, "PULL_REQUEST", "ISSUE"}:
            safe_escalation.pop("target_type", None)
        if not _is_safe_https_url(safe_escalation.get("comment_url")):
            safe_escalation.pop("comment_url", None)
        if safe_escalation.get("reason_code") not in GUIDANCE_COPY:
            safe_escalation.pop("reason_code", None)
        if safe_escalation.get("resume_classification") not in {
            "RISK_APPROVAL",
            "PLAN_DECISION",
            "NOT_RESUMABLE",
        }:
            safe_escalation.pop("resume_classification", None)
        for key in ("waiting_for_human", "is_resumed"):
            if not isinstance(safe_escalation.get(key), bool):
                safe_escalation.pop(key, None)
        for key in ("episode_number", "reopen_count", "accepted_reply_count"):
            value = safe_escalation.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                safe_escalation.pop(key, None)
        responder = safe_escalation.get("last_responder")
        if responder is not None and (
            not isinstance(responder, str) or not _GITHUB_LOGIN_PATTERN.fullmatch(responder)
        ):
            safe_escalation.pop("last_responder", None)
        if safe_escalation.get("last_action") not in {None, "ANSWER", "RESUME"}:
            safe_escalation.pop("last_action", None)
        sanitized["escalation"] = safe_escalation
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
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 12:
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
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 12
            }
    else:
        decision_count = data.get("decision_count")
        if (
            isinstance(decision_count, int)
            and not isinstance(decision_count, bool)
            and 0 <= decision_count <= 24
        ):
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
            _allowlist(task, PROJECT_TASK_FIELDS) for task in tasks if isinstance(task, dict)
        ]
    models = data.get("models")
    if isinstance(models, list):
        sanitized["models"] = []
        for model in models:
            if not isinstance(model, dict):
                continue
            model_data = _allowlist(model, PROJECT_MODEL_FIELDS)
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

    sanitized = _allowlist(data, HEALTH_ALLOWED_FIELDS)

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
                    _allowlist(check_safe, frozenset({"name", "status", "message", "remediation"}))
                )
        sanitized["checks"] = sanitized_checks

    return sanitized
