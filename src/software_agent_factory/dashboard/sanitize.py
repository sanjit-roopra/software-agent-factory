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

from typing import Any

from .snapshot import to_json_safe

#: Fields rendered in the paginated run table (``/api/runs``). Includes both
#: ``run_id`` (the real ``observability.RunSummary`` field name) and ``id``
#: (accepted from simpler providers/tests) since the client tolerates either.
RUN_SUMMARY_FIELDS: frozenset[str] = frozenset(
    {
        "run_id",
        "id",
        "work_item_id",
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
        "invocation_count",
        "usage",
        "guidance",
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


def sanitize_run_summary(raw: Any) -> dict[str, Any]:
    """Reduce one provider-supplied run to only the fields the UI renders."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run summary must serialize to a JSON object")
    return _allowlist(data, RUN_SUMMARY_FIELDS)


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
    if "usage" in sanitized:
        sanitized["usage"] = sanitize_usage(sanitized["usage"])
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
    return sanitized


def _sanitize_guidance(data: dict[str, Any]) -> dict[str, Any] | None:
    reason_code = data.get("reason_code")
    if not isinstance(reason_code, str) or reason_code not in GUIDANCE_COPY:
        return None
    status, summary, next_action, artifact = GUIDANCE_COPY[reason_code]
    result: dict[str, Any] = {
        "status": status,
        "reason_code": reason_code,
        "summary": summary,
        "next_action": next_action,
    }
    if artifact is not None:
        result["artifact"] = artifact
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
