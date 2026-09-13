"""Controller-owned GitHub escalation notices and authorized human reply loop.

Implements the core escalation and authorized human reply loop:
- When a run enters ``NEEDS_HUMAN``, the factory posts a concise, safe status
  comment on the persisted open factory PR if available, falling back to the
  source GitHub issue reference.
- An authorized human contributor may reply on that exact thread with:
  ``@factory resume v1 run=<run-id> episode=<opaque-id>``
- The controller validates the comment, author, timing, target, and episode,
  records a durable decision receipt, and reopens the run when the halt category
  is supported (``RISK_APPROVAL`` -> ``REFINING``).
- Comment text never enters agent prompts and cannot alter models, commands,
  paths, URLs, retry policy, or arbitrary workflow state.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from .config import FactoryConfig
from .github import (
    GitHubClient,
    GitHubComment,
    GitHubError,
    RepositoryRef,
    parse_issue_reference,
    parse_pull_request_url,
)
from .models import (
    AcceptedReplyReceipt,
    EscalationRecord,
    EscalationStatus,
    EscalationTargetType,
    ExecutionPlan,
    FactoryRun,
    ResumeClassification,
    ReviewImpasse,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    TriageResult,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .store import FileRunStore
from .verification import redact_secrets

logger = logging.getLogger(__name__)

MAX_ESCALATION_COMMENT_CHARS: int = 4000
UNRESOLVED_DECISIONS_REASON_CODE: str = "UNRESOLVED_DECISIONS"
UNRESOLVED_DECISIONS_HALT_PREFIX: str = "execution plan has unresolved decisions"


class EscalationComment(str):
    """Rendered escalation notice comment bound to an explicit remote-resume outcome."""

    body: str
    remote_resume_enabled: bool

    def __new__(cls, body: str, *, remote_resume_enabled: bool) -> EscalationComment:
        instance = super().__new__(cls, body)
        instance.body = body
        instance.remote_resume_enabled = remote_resume_enabled
        return instance


# Absolute, network, and system file system paths
_ABSOLUTE_OR_NETWORK_PATH_PATTERN = re.compile(
    r"(?i)"
    r"(?:(?<![A-Za-z0-9.~/@\\<])(?<!&lt;)/(?:[A-Za-z0-9_.-]+)[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])~[\\/][^\s\"'`>)]+)"
    r"|(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])\\\\[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.:])//[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
)

# Credentials embedded in URLs
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@[^\s/]+"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s@]+@[^\s/]+"
)

# Tokens, API keys, credentials, and private keys
_TOKEN_AND_KEY_PATTERN = re.compile(
    r"(?i)\bxox[baprse]-[0-9A-Za-z-]{10,}\b"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,})\b"
    r"|\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"
    r"|\bsk-(?:proj-|ant-)?[0-9a-zA-Z_-]{20,}\b"
    r"|\b(?:authorization|proxy[_-]?authorization)\s*[:=]\s*[^\r\n]+"
    r"|\b(?:cookie|set[_-]?cookie|set[_-]?cookie2)\s*[:=]\s*[^\r\n]+"
    r"|\bBearer\s+[A-Za-z0-9_.\-/+=]{20,}"
    r"|\bBasic\s+[A-Za-z0-9+/]{8,}={1,2}(?!\S)"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|session[_-]?id|session[_-]?token|session[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_.-]{8,}"
    r"|-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"
)

# External URLs (http, https, ftp) and bare www. domains
_EXTERNAL_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|ftp)://[^\s\"'`<>)]+"
    r"|\bwww\.[A-Za-z0-9_.-]+\.[A-Za-z]{2,}[^\s\"'`<>)]*"
)

# Raw diagnostics / stack traces / diff output
_RAW_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(?:traceback \(most recent call last\)|subprocess\.calledprocesserror|"
    r"file \"[^\"]+\", line \d+|diff --git|@@ -\d+,\d+ \+\d+,\d+ @@|\+[A-Z0-9_]+=[^\s]+)"
)

_RESUME_COMMAND_PATTERN = re.compile(
    r"^@factory\s+resume\s+v1\s+run=(?P<run>[A-Za-z0-9._-]+)\s+episode=(?P<episode>[A-Za-z0-9._-]+)$"
)

_ESCALATION_MARKER_TEMPLATE = (
    "<!-- software-agent-factory:escalation run={run_id} episode={episode_id} -->"
)
_ESCALATION_MARKER_REGEX = re.compile(
    r"<!--\s*software-agent-factory:escalation\s+run=(?P<run>[A-Za-z0-9._-]+)\s+episode=(?P<episode>[A-Za-z0-9._-]+)\s*-->"
)


def generate_episode_id() -> str:
    """Generate an unpredictable, cryptographically stable episode token."""
    return f"ep-{secrets.token_hex(12)}"


def format_escalation_marker(run_id: str, episode_id: str) -> str:
    """Build the stable hidden HTML marker bound to run + escalation episode."""
    return _ESCALATION_MARKER_TEMPLATE.format(run_id=run_id, episode_id=episode_id)


def parse_resume_command(body: str) -> tuple[str, str] | None:
    """Parse exact ASCII command: @factory resume v1 run=<run-id> episode=<opaque-id>."""
    cleaned = body.strip()
    match = _RESUME_COMMAND_PATTERN.fullmatch(cleaned)
    if match is None:
        return None
    return match.group("run"), match.group("episode")


def normalize_whitespace(text: str) -> str:
    """Normalize newlines and multiple whitespace into a single trimmed line."""
    if not text:
        return text
    return re.sub(r"\s+", " ", text).strip()


def escape_notice_text(text: str) -> str:
    """Escape Markdown and HTML control syntax and neutralize mentions for safe GitHub rendering."""
    if not text:
        return text
    # 1. HTML escape (&, <, >, ", ')
    escaped = html.escape(text, quote=True)
    # 2. Neutralize HTML comments
    escaped = escaped.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    # 3. Protect &#x27; from hash replacement
    escaped = escaped.replace("&#x27;", "__APOS_PLACEHOLDER__")
    # 4. Escape literal '#' so issue/PR autolinks (#123) and headings are neutralized
    escaped = escaped.replace("#", "&#35;")
    escaped = escaped.replace("__APOS_PLACEHOLDER__", "&#x27;")
    # 5. Neutralize @-mentions completely
    escaped = escaped.replace("@", "&#64;")
    # 6. Escape Markdown control syntax:
    escaped = escaped.replace("`", "&#96;")
    escaped = escaped.replace("[", "&#91;").replace("]", "&#93;")
    escaped = escaped.replace("*", "&#42;")
    escaped = escaped.replace("_", "&#95;")
    escaped = escaped.replace("~", "&#126;")
    escaped = escaped.replace("|", "&#124;")
    # 7. Neutralize URL scheme and domain prefixes to prevent GFM autolinking
    escaped = re.sub(r"(?i)\b(https?|ftp)://", r"\1&#58;&#47;&#47;", escaped)
    escaped = re.sub(r"(?i)\bwww\.", "www&#46;", escaped)
    return escaped


def contains_unsafe_content(text: str) -> tuple[bool, str]:
    """Check whether text contains paths, embedded credentials, tokens, URLs, or diagnostics."""
    if not text:
        return False, ""
    if _URL_CREDENTIAL_PATTERN.search(text):
        return True, "contains URL-embedded credentials"
    if _EXTERNAL_URL_PATTERN.search(text):
        return True, "contains external URL or link"
    if _TOKEN_AND_KEY_PATTERN.search(text):
        return True, "contains token or credential"
    if _ABSOLUTE_OR_NETWORK_PATH_PATTERN.search(text):
        return True, "contains local or network file system path"
    if _RAW_DIAGNOSTIC_PATTERN.search(text):
        return True, "contains raw diagnostic or diff output"
    return False, ""


def compute_approval_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    work_item_id: str,
    work_item_title: str,
    risk: str,
    complexity: str,
    intended_outcome: str,
    sensitive_boundary: str,
    necessity: str,
    credible_scenario: str,
    known_mitigations: Sequence[str],
    residual_risk: str,
    decision_requested: str,
    next_state: str,
    authorized_actions: Sequence[str],
    unauthorized_actions: Sequence[str],
    conditions_in_force: Sequence[str],
) -> str:
    """Compute deterministic SHA-256 binding displayed and authority fields to episode."""
    payload = json.dumps(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "work_item_id": work_item_id,
            "work_item_title": work_item_title,
            "risk": risk,
            "complexity": complexity,
            "intended_outcome": intended_outcome,
            "sensitive_boundary": sensitive_boundary,
            "necessity": necessity,
            "credible_scenario": credible_scenario,
            "known_mitigations": list(known_mitigations),
            "residual_risk": residual_risk,
            "decision_requested": decision_requested,
            "next_state": next_state,
            "authorized_actions": list(authorized_actions),
            "unauthorized_actions": list(unauthorized_actions),
            "conditions_in_force": list(conditions_in_force),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_risk_approval_context(
    run: FactoryRun,
    store: FileRunStore,
    *,
    config: FactoryConfig | None = None,
    work_item: WorkItem | None = None,
    triage_result: TriageResult | None = None,
    episode_id: str | None = None,
) -> RiskApprovalContext | None:
    """Snapshot an informed approval context from accepted artifacts and deterministic facts."""
    if work_item is None:
        try:
            work_item = store.load_artifact(run.id, WorkItem)
        except (FileNotFoundError, ValueError):
            logger.warning(
                "run %s missing WorkItem artifact; cannot build approval context", run.id
            )
            return None

    if triage_result is None:
        try:
            triage_result = store.load_artifact(run.id, TriageResult)
        except (FileNotFoundError, ValueError):
            logger.warning(
                "run %s missing TriageResult artifact; cannot build approval context", run.id
            )
            return None

    if triage_result.risk not in {Risk.R2, Risk.R3} or triage_result.risk_rationale is None:
        logger.warning(
            "run %s triage risk is %s without rationale; cannot build approval context",
            run.id,
            triage_result.risk,
        )
        return None

    rationale = triage_result.risk_rationale
    fields_to_check = [
        work_item.id,
        work_item.title,
        rationale.intended_outcome,
        rationale.sensitive_boundary,
        rationale.necessity,
        rationale.credible_scenario,
        *rationale.known_mitigations,
        rationale.residual_risk,
    ]
    for field_val in fields_to_check:
        is_unsafe, reason = contains_unsafe_content(field_val)
        if is_unsafe:
            logger.warning("run %s approval context rejected: %s", run.id, reason)
            return None

    clean_id = escape_notice_text(normalize_whitespace(redact_secrets(work_item.id)))[:128]
    clean_title = escape_notice_text(normalize_whitespace(redact_secrets(work_item.title)))[:120]
    clean_outcome = escape_notice_text(
        normalize_whitespace(redact_secrets(rationale.intended_outcome))
    )[:240]
    clean_boundary = escape_notice_text(
        normalize_whitespace(redact_secrets(rationale.sensitive_boundary))
    )[:240]
    clean_necessity = escape_notice_text(normalize_whitespace(redact_secrets(rationale.necessity)))[
        :240
    ]
    clean_scenario = escape_notice_text(
        normalize_whitespace(redact_secrets(rationale.credible_scenario))
    )[:300]
    clean_mitigations = [
        escape_notice_text(normalize_whitespace(redact_secrets(m)))[:160]
        for m in rationale.known_mitigations[:5]
    ]
    clean_residual = escape_notice_text(
        normalize_whitespace(redact_secrets(rationale.residual_risk))
    )[:240]

    if not (
        clean_id
        and clean_title
        and clean_outcome
        and clean_boundary
        and clean_necessity
        and clean_scenario
        and clean_mitigations
        and clean_residual
    ):
        logger.warning("run %s approval context has empty cleaned fields", run.id)
        return None

    current_episode_id = episode_id or (run.escalation.episode_id if run.escalation else "")

    decision_requested = (
        f"Approve advancing run {run.id} to REFINING under risk policy {triage_result.risk.value}."
    )
    authorized_actions = [
        "Transition workflow from NEEDS_HUMAN to REFINING.",
        "Refine requirements into an explicit specification.",
        "Plan implementation steps within approved scope.",
        "Execute code changes in an isolated workspace.",
        "Run deterministic verification, tests, and review.",
    ]
    unauthorized_actions = [
        "Approval does not change task scope.",
        "Approval does not increase retry budgets.",
        "Approval does not bypass quality gates.",
        "Approval does not alter credential or permission policy.",
        "Approval does not change deployment policy.",
        "Approval does not override configured merge policy.",
    ]
    conditions_in_force = [
        "The approved scope remains restricted to this task.",
        "Deterministic verification must pass before review.",
        "Independent testing and review remain mandatory.",
        "Quality gates must pass before pull request creation.",
        "Approval resumes the same run at REFINING.",
        "Approval does not reset run history or attempt budgets.",
    ]

    fingerprint = compute_approval_context_fingerprint(
        run_id=run.id,
        episode_id=current_episode_id,
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk=triage_result.risk.value,
        complexity=triage_result.complexity.value,
        intended_outcome=clean_outcome,
        sensitive_boundary=clean_boundary,
        necessity=clean_necessity,
        credible_scenario=clean_scenario,
        known_mitigations=clean_mitigations,
        residual_risk=clean_residual,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING.value,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
    )

    clean_rationale = RiskRationale(
        intended_outcome=clean_outcome,
        sensitive_boundary=clean_boundary,
        necessity=clean_necessity,
        credible_scenario=clean_scenario,
        known_mitigations=clean_mitigations,
        residual_risk=clean_residual,
    )

    return RiskApprovalContext(
        risk=triage_result.risk,
        complexity=triage_result.complexity,
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk_rationale=clean_rationale,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
        context_fingerprint=fingerprint,
    )


def is_valid_risk_approval_context(
    context: RiskApprovalContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify that an approval context is complete, safe, and bound to this run and episode."""
    if not isinstance(context, RiskApprovalContext):
        return False
    if context.risk not in {Risk.R2, Risk.R3}:
        return False
    if context.next_state is not WorkflowState.REFINING:
        return False

    rationale = context.risk_rationale
    fields = [
        context.work_item_id,
        context.work_item_title,
        context.decision_requested,
        rationale.intended_outcome,
        rationale.sensitive_boundary,
        rationale.necessity,
        rationale.credible_scenario,
        *rationale.known_mitigations,
        rationale.residual_risk,
        *context.authorized_actions,
        *context.unauthorized_actions,
        *context.conditions_in_force,
    ]
    for field_val in fields:
        is_unsafe, _ = contains_unsafe_content(field_val)
        if is_unsafe:
            return False

    expected_fp = compute_approval_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        work_item_id=context.work_item_id,
        work_item_title=context.work_item_title,
        risk=context.risk.value,
        complexity=context.complexity.value,
        intended_outcome=rationale.intended_outcome,
        sensitive_boundary=rationale.sensitive_boundary,
        necessity=rationale.necessity,
        credible_scenario=rationale.credible_scenario,
        known_mitigations=rationale.known_mitigations,
        residual_risk=rationale.residual_risk,
        decision_requested=context.decision_requested,
        next_state=context.next_state.value,
        authorized_actions=context.authorized_actions,
        unauthorized_actions=context.unauthorized_actions,
        conditions_in_force=context.conditions_in_force,
    )
    return secrets.compare_digest(context.context_fingerprint, expected_fp)


class ValidationResult(tuple[bool, str]):
    """Result of candidate comment validation.

    Acts as a 2-tuple (is_valid, reason) for backward compatibility,
    with an additional `retryable` boolean indicating whether a failure
    was transient/retryable (e.g. comment re-fetch or identity resolution failure)
    versus permanent (grammar, run/episode mismatch, edited comment, unauthorized author).
    """

    is_valid: bool
    reason: str
    retryable: bool

    def __new__(
        cls,
        is_valid: bool,
        reason: str,
        *,
        retryable: bool = False,
    ) -> ValidationResult:
        instance = super().__new__(cls, (is_valid, reason))
        instance.is_valid = is_valid
        instance.reason = reason
        instance.retryable = retryable
        return instance


def classify_halt_reason(
    run: FactoryRun,
    store: FileRunStore | None = None,
) -> tuple[ResumeClassification, str, str, str]:
    """Deterministically classify a halted run into a typed resume category.

    Returns:
        (classification, reason_code, summary, next_action)
    """
    if run.state is not WorkflowState.NEEDS_HUMAN:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "MANUAL_INSPECTION",
            "The run is not in NEEDS_HUMAN state.",
            "Inspect the typed run artifacts.",
        )

    if store is not None:
        try:
            impasse = store.load_artifact(run.id, ReviewImpasse)
        except (FileNotFoundError, ValueError):
            impasse = None
        if impasse is not None:
            return (
                ResumeClassification.NOT_RESUMABLE,
                "REVIEW_IMPASSE",
                "Independent review did not converge within the safe automatic policy.",
                "Inspect review-impasse.json, resolve or accept the listed findings, then retry.",
            )

    reason = (run.failure_reason or "").lower()
    if reason.startswith(UNRESOLVED_DECISIONS_HALT_PREFIX):
        unresolved_count: int | None = None
        if store is not None:
            try:
                plan = store.load_artifact(run.id, ExecutionPlan)
                if plan is not None and plan.unresolved_decisions:
                    unresolved_count = len(plan.unresolved_decisions)
            except (FileNotFoundError, ValueError):
                pass
        if unresolved_count is None:
            count_match = re.search(r"\b(\d+)\s+unresolved", reason) or re.search(
                r"\((\d+)\)", reason
            )
            if count_match:
                unresolved_count = int(count_match.group(1))
        if unresolved_count is not None:
            decisions_label = "decision" if unresolved_count == 1 else "decisions"
            summary = (
                f"The execution plan has {unresolved_count} unresolved architectural "
                f"{decisions_label}."
            )
        else:
            summary = "The execution plan has unresolved architectural decisions."
        return (
            ResumeClassification.NOT_RESUMABLE,
            UNRESOLVED_DECISIONS_REASON_CODE,
            summary,
            "Inspect execution-plan.json, resolve the decisions, then retry.",
        )
    if "scope" in reason:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "SCOPE_REVIEW",
            "The proposed changes exceeded the approved scope.",
            "Review the planned and changed files, then update the scope or retry.",
        )
    if re.fullmatch(r"risk r[23] requires human approval", reason):
        return (
            ResumeClassification.RISK_APPROVAL,
            "RISK_APPROVAL",
            "The run requires approval under the configured risk policy.",
            "Review the work item risk and approve or change the policy before retrying.",
        )
    if "budget" in reason or "attempt" in reason:
        return (
            ResumeClassification.NOT_RESUMABLE,
            "ATTEMPT_BUDGET_EXHAUSTED",
            "The run exhausted a bounded retry budget.",
            "Inspect the run artifacts, correct the underlying issue, then retry.",
        )
    if "ci " in reason or reason.startswith("ci"):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "CI_INTERVENTION",
            "CI could not be completed or repaired automatically.",
            "Inspect the pull request checks, fix the failing check, then retry delivery.",
        )
    if any(term in reason for term in ("publish", "pull request", "merge", "permission")):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "DELIVERY_INTERVENTION",
            "The controller could not complete pull request delivery.",
            "Check repository permissions and delivery settings, then retry delivery.",
        )
    if any(term in reason for term in ("abandon", "interrupt", "workspace")):
        return (
            ResumeClassification.NOT_RESUMABLE,
            "RECOVERY_INTERVENTION",
            "The run could not safely recover its persisted workspace.",
            "Inspect the run and workspace metadata before starting a replacement run.",
        )
    return (
        ResumeClassification.NOT_RESUMABLE,
        "MANUAL_INSPECTION",
        "The controller stopped at a manual decision boundary.",
        "Inspect the typed run artifacts and decide whether to retry or replace the run.",
    )


def build_escalation_comment(
    *,
    run_id: str,
    episode_id: str,
    classification: ResumeClassification,
    reason_code: str,
    summary: str,
    next_action: str,
    attempts_consumed: int,
    reopen_count: int,
    max_reopens: int,
    approval_context: RiskApprovalContext | None = None,
) -> EscalationComment:
    """Build concise, safe GitHub comment content.

    Raw failure_reason, workspace paths, issue body/title, command output,
    diffs, logs, and model reasoning are never published.
    """
    marker = format_escalation_marker(run_id, episode_id)
    if classification is ResumeClassification.RISK_APPROVAL:
        if approval_context is not None and is_valid_risk_approval_context(
            approval_context, run_id, episode_id
        ):
            rat = approval_context.risk_rationale
            mitigations_block = "\n".join(f"  - {m}" for m in rat.known_mitigations)
            auth_block = "\n".join(f"- {a}" for a in approval_context.authorized_actions)
            unauth_block = "\n".join(f"- {u}" for u in approval_context.unauthorized_actions)
            cond_block = "\n".join(f"- {c}" for c in approval_context.conditions_in_force)

            lines = [
                marker,
                "### Factory Risk Approval Notice",
                "",
                (
                    f"The run `{run_id}` halted because risk "
                    f"`{approval_context.risk.value}` requires human approval."
                ),
                "",
                f"- **Reason code**: `{reason_code}`",
                (
                    f"- **Work item**: `{approval_context.work_item_id}` - "
                    f"{approval_context.work_item_title}"
                ),
                f"- **Attempts recorded**: {attempts_consumed}",
                f"- **Reopens**: {reopen_count}/{max_reopens}",
                "",
                "#### Why approval is required",
                f"- Intended outcome: {rat.intended_outcome}",
                f"- Sensitive boundary: {rat.sensitive_boundary}",
                f"- Necessity: {rat.necessity}",
                f"- Credible scenario: {rat.credible_scenario}",
                "- Known mitigations:",
                mitigations_block,
                f"- Residual risk: {rat.residual_risk}",
                "",
                "#### Decision requested",
                approval_context.decision_requested,
                "",
                "#### Approval authorizes",
                auth_block,
                "",
                "#### Approval does not authorize",
                unauth_block,
                "",
                "#### Conditions that remain in force",
                cond_block,
                "",
                "#### Residual risk accepted",
                rat.residual_risk,
                "",
                "#### Resume instructions",
                (
                    "To approve this request, an authorized contributor must reply "
                    "on this thread with:"
                ),
                "",
                "```",
                f"@factory resume v1 run={run_id} episode={episode_id}",
                "```",
                "",
            ]
            rendered = "\n".join(lines)
            if len(rendered) <= MAX_ESCALATION_COMMENT_CHARS:
                return EscalationComment(rendered, remote_resume_enabled=True)
            logger.warning(
                "run %s risk approval comment exceeded size limit (%d chars)",
                run_id,
                len(rendered),
            )

        fallback_msg = (
            "This risk approval escalation notice exceeded the maximum comment size limit. "
            "Remote resume is disabled. Manual inspection of local artifacts is required."
            if (
                approval_context is not None
                and is_valid_risk_approval_context(approval_context, run_id, episode_id)
            )
            else (
                "This risk approval escalation lacks complete valid decision context. "
                "Remote resume is disabled. Manual inspection of local artifacts is required."
            )
        )

        lines = [
            marker,
            "### Factory Escalation Notice",
            "",
            f"The run `{run_id}` requires human attention.",
            "",
            f"- **Reason code**: `{reason_code}`",
            f"- **Summary**: {summary}",
            f"- **Next action**: {next_action}",
            f"- **Attempts recorded**: {attempts_consumed}",
            f"- **Reopens**: {reopen_count}/{max_reopens}",
            "",
            "#### Resume instructions",
            "",
            fallback_msg,
            "",
        ]
        return EscalationComment("\n".join(lines), remote_resume_enabled=False)

    lines = [
        marker,
        "### Factory Escalation Notice",
        "",
        f"The run `{run_id}` requires human attention.",
        "",
        f"- **Reason code**: `{reason_code}`",
        f"- **Summary**: {summary}",
        f"- **Next action**: {next_action}",
        f"- **Attempts recorded**: {attempts_consumed}",
        f"- **Reopens**: {reopen_count}/{max_reopens}",
        "",
        "#### Resume instructions",
        "",
        "This halt category cannot be resumed automatically via GitHub reply. "
        "Manual inspection of local artifacts is required.",
        "",
    ]
    return EscalationComment("\n".join(lines), remote_resume_enabled=False)


def resolve_escalation_target(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    expected_repository: str | None = None,
) -> tuple[RepositoryRef, int, EscalationTargetType, str | None] | None:
    """Resolve the destination for escalation comments: open factory PR first,
    otherwise source issue reference.

    Enforces allowed hosts and binds PR-first targets to authoritative repository
    identities (source issue repository, delivery repository, or service repository).
    Never searches GitHub for arbitrary linked PRs.
    """
    allowed_hosts = {host.casefold() for host in config.escalation.allowed_hosts}

    # Establish authoritative repository identities
    try:
        work_item = store.load_artifact(run.id, WorkItem)
    except (FileNotFoundError, ValueError):
        work_item = None

    source_issue_repo: RepositoryRef | None = None
    issue_number: int | None = None
    hosts = config.escalation.allowed_hosts
    pr_hosts = config.pull_request.allowed_hosts
    default_host = (
        run.delivery_host
        or client.host
        or (pr_hosts[0] if pr_hosts else None)
        or (hosts[0] if hosts else "github.com")
    )
    if work_item and work_item.external_id:
        try:
            source_issue_repo, issue_number = parse_issue_reference(
                work_item.external_id, default_host=default_host
            )
        except ValueError:
            source_issue_repo = None
            issue_number = None

    authoritative_identities: set[tuple[str, str]] = set()
    if source_issue_repo is not None:
        authoritative_identities.add(
            (source_issue_repo.host.casefold(), source_issue_repo.full_name.casefold())
        )
    if run.delivery_repository:
        delivery_repo_host = (
            run.delivery_host
            or (source_issue_repo.host if source_issue_repo else None)
            or default_host
        ).casefold()
        authoritative_identities.add((delivery_repo_host, run.delivery_repository.casefold()))
    if expected_repository:
        if "/" in expected_repository and expected_repository.count("/") >= 2:
            parts = expected_repository.split("/", 1)
            authoritative_identities.add((parts[0].casefold(), parts[1].casefold()))
        else:
            exp_host = (
                run.delivery_host
                or (source_issue_repo.host if source_issue_repo else None)
                or default_host
            ).casefold()
            authoritative_identities.add((exp_host, expected_repository.casefold()))

    # 1. Prefer open persisted factory PR matching authoritative repository
    if run.pull_request_url:
        try:
            pr_repo_ref, pr_number = parse_pull_request_url(run.pull_request_url)
        except ValueError:
            pr_repo_ref, pr_number = None, None

        if pr_repo_ref is not None and pr_number is not None:
            pr_host = pr_repo_ref.host.casefold()
            pr_full_name = pr_repo_ref.full_name.casefold()
            pr_identity = (pr_host, pr_full_name)

            host_is_allowed = pr_host in allowed_hosts
            source_host_agrees = (
                source_issue_repo.host.casefold() == pr_host
                if source_issue_repo is not None
                else True
            )
            delivery_host_agrees = (
                run.delivery_host.casefold() == pr_host if run.delivery_host is not None else True
            )

            matches_authoritative = (
                host_is_allowed
                and source_host_agrees
                and delivery_host_agrees
                and pr_identity in authoritative_identities
            )
            if matches_authoritative:
                try:
                    pr_state = client.get_pull_request(
                        repo_path,
                        str(pr_number),
                        repository=pr_repo_ref.full_name,
                        hostname=pr_repo_ref.host,
                    )
                    if pr_state.state.upper() == "OPEN":
                        return (
                            pr_repo_ref,
                            pr_number,
                            EscalationTargetType.PULL_REQUEST,
                            run.pull_request_url,
                        )
                except GitHubError:
                    logger.debug("could not verify PR state for %s", run.pull_request_url)
            else:
                logger.warning(
                    "ignoring PR url %s: does not match authoritative repo identities %s "
                    "or allowed hosts",
                    run.pull_request_url,
                    authoritative_identities,
                )

    # 2. Fall back to source GitHub issue reference
    if (
        source_issue_repo is not None
        and issue_number is not None
        and source_issue_repo.host.casefold() in allowed_hosts
    ):
        target_url = (
            work_item.external_id
            if work_item and work_item.external_id and work_item.external_id.startswith("http")
            else f"https://{source_issue_repo.host}/{source_issue_repo.full_name}/issues/{issue_number}"
        )
        return source_issue_repo, issue_number, EscalationTargetType.ISSUE, target_url

    return None


def _is_matching_factory_author(
    comment: GitHubComment,
    *,
    factory_verified: bool,
    factory_login: str | None,
    factory_id: int | None,
) -> bool:
    """Verify that comment author matches the live authenticated factory account."""
    if not factory_verified:
        return False
    if factory_id is not None and comment.user_id is not None:
        return comment.user_id == factory_id
    if (
        factory_login
        and comment.user_login
        and comment.user_login.casefold() == factory_login.casefold()
    ):
        return True
    return False


def is_authorized_author(
    comment: GitHubComment,
    *,
    authorized_identities: Sequence[str],
    allowed_associations: Sequence[str],
    factory_login: str | None = None,
    factory_id: int | None = None,
) -> bool:
    """Verify that comment author is a human, not a bot, not the factory account,
    matches authorized identities, and possesses an allowed association."""
    # 1. Reject bot
    if comment.user_type.lower() == "bot" or comment.user_login.casefold().endswith("[bot]"):
        return False

    # 2. Reject self even if configured in authorized_identities
    if factory_login and comment.user_login.casefold() == factory_login.casefold():
        return False
    if factory_id is not None and comment.user_id is not None and comment.user_id == factory_id:
        return False

    # 3. Check allowed association
    allowed_set = {assoc.upper() for assoc in allowed_associations}
    if comment.author_association.upper() not in allowed_set:
        return False

    # 4. All-digit entries are immutable user IDs only. Other entries are
    # logins only, so a numeric login cannot impersonate an allowlisted ID.
    authorized_logins = {
        identity.casefold() for identity in authorized_identities if not identity.isdigit()
    }
    authorized_ids = {int(identity) for identity in authorized_identities if identity.isdigit()}
    login_matches = comment.user_login.casefold() in authorized_logins
    id_matches = comment.user_id is not None and comment.user_id in authorized_ids
    return login_matches or id_matches


def deliver_escalation_notification(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    expected_repository: str | None = None,
) -> FactoryRun:
    """Attempt bounded delivery of an escalation notice comment.

    Errors are persisted on the run's escalation record and never prevent
    the run from remaining in NEEDS_HUMAN.
    """
    if not config.escalation.enabled:
        return run

    if run.state is not WorkflowState.NEEDS_HUMAN:
        return run

    escalation = run.escalation
    if escalation is None:
        classification, code, summary, action = classify_halt_reason(run, store)
        episode_id = generate_episode_id()
        approval_context = None
        if classification is ResumeClassification.RISK_APPROVAL:
            approval_context = build_risk_approval_context(
                run,
                store,
                config=config,
                episode_id=episode_id,
            )
        escalation = EscalationRecord(
            episode_id=episode_id,
            episode_number=1,
            status=EscalationStatus.PENDING_NOTIFICATION,
            resume_classification=classification,
            reason_code=code,
            approval_context=approval_context,
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)

    if escalation.status is EscalationStatus.NOTIFIED:
        return run

    if escalation.delivery_attempts >= config.escalation.max_notification_attempts:
        if escalation.status is not EscalationStatus.NOTIFICATION_FAILED:
            escalation = escalation.model_copy(
                update={
                    "status": EscalationStatus.NOTIFICATION_FAILED,
                    "remote_resume_enabled": False,
                    "reply_cursor": "closed",
                    "updated_at": utc_now(),
                }
            )
            run = run.model_copy(update={"escalation": escalation})
            store.save_run(run)
        return run

    attempts = escalation.delivery_attempts + 1
    is_terminal = attempts >= config.escalation.max_notification_attempts

    target = resolve_escalation_target(
        run, store, config, client, repo_path, expected_repository=expected_repository
    )
    if target is None:
        escalation = escalation.model_copy(
            update={
                "delivery_attempts": attempts,
                "delivery_error": "no valid escalation target resolved",
                "status": EscalationStatus.NOTIFICATION_FAILED,
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    repo_ref, target_number, target_type, target_url = target
    classification, code, summary, action = classify_halt_reason(run, store)
    if escalation.resume_classification is not None:
        classification = escalation.resume_classification
        if escalation.reason_code:
            code = escalation.reason_code
        elif classification is ResumeClassification.RISK_APPROVAL:
            code = "RISK_APPROVAL"
    rendered_notice = build_escalation_comment(
        run_id=run.id,
        episode_id=escalation.episode_id,
        classification=classification,
        reason_code=code,
        summary=summary,
        next_action=action,
        attempts_consumed=len(run.attempt_records),
        reopen_count=escalation.reopen_count,
        max_reopens=config.escalation.max_reopens,
        approval_context=escalation.approval_context,
    )
    comment_body = str(rendered_notice)
    remote_resume_enabled = getattr(rendered_notice, "remote_resume_enabled", False)
    reply_cursor = None if remote_resume_enabled else "closed"

    factory_login: str | None = None
    factory_id: int | None = None
    factory_verified = False
    try:
        identity = client.get_authenticated_user(repo_path, hostname=repo_ref.host)
        factory_login = identity.login
        factory_id = identity.id
        if factory_login or factory_id is not None:
            factory_verified = True
    except GitHubError as exc:
        logger.debug(
            "could not resolve authenticated factory user on %s for notification check: %s",
            repo_ref.host,
            exc,
        )

    marker = format_escalation_marker(run.id, escalation.episode_id)
    comments_with_marker: list[GitHubComment] = []
    try:
        # Check if already posted before creating a duplicate, bounded up to 3 pages
        since_time = escalation.created_at - timedelta(minutes=2)
        for page_idx in range(1, 4):
            existing_comments = client.list_issue_comments(
                repo_path,
                repository=repo_ref.full_name,
                issue_number=target_number,
                since=since_time,
                page=page_idx,
                per_page=100,
                hostname=repo_ref.host,
            )
            for existing in existing_comments:
                if marker in existing.body:
                    comments_with_marker.append(existing)
            if comments_with_marker or len(existing_comments) < 100:
                break
    except GitHubError as exc:
        logger.debug("could not check existing comments for run %s: %s", run.id, exc)

    matching_notice: GitHubComment | None = None
    for existing in comments_with_marker:
        body_matches = existing.body.replace("\r\n", "\n") == comment_body.replace("\r\n", "\n")
        author_matches = _is_matching_factory_author(
            existing,
            factory_verified=factory_verified,
            factory_login=factory_login,
            factory_id=factory_id,
        )
        if body_matches and author_matches:
            matching_notice = existing
            break

    if matching_notice is not None:
        notified_at = escalation.last_notified_at or matching_notice.created_at or utc_now()
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "comment_id": matching_notice.id,
                "comment_url": matching_notice.html_url or matching_notice.url,
                "status": EscalationStatus.NOTIFIED,
                "delivery_error": None,
                "last_notified_at": notified_at,
                "remote_resume_enabled": remote_resume_enabled,
                "reply_cursor": reply_cursor,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    if comments_with_marker:
        # A comment with the marker exists, but body differs, author differs,
        # or author cannot be verified. Idempotency does not permit posting a duplicate
        # marker notice, so fail closed for local inspection.
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "delivery_error": (
                    "existing comment with escalation marker has altered body or "
                    "unverified author; failing closed for local inspection"
                ),
                "status": EscalationStatus.NOTIFICATION_FAILED,
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "last_notified_at": None,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    if not factory_verified:
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "delivery_error": (
                    "cannot verify authenticated factory account; "
                    "failing closed for local inspection"
                ),
                "status": EscalationStatus.NOTIFICATION_FAILED,
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "last_notified_at": None,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return run

    try:
        posted = client.create_issue_comment(
            repo_path,
            repository=repo_ref.full_name,
            issue_number=target_number,
            body=comment_body,
            hostname=repo_ref.host,
        )
        notified_at = posted.created_at or utc_now()
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "comment_id": posted.id,
                "comment_url": posted.html_url or posted.url,
                "status": EscalationStatus.NOTIFIED,
                "delivery_error": None,
                "last_notified_at": notified_at,
                "remote_resume_enabled": remote_resume_enabled,
                "reply_cursor": reply_cursor,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
    except GitHubError as exc:
        escalation = escalation.model_copy(
            update={
                "target_type": target_type,
                "target_host": repo_ref.host,
                "target_repository": repo_ref.full_name,
                "target_number": target_number,
                "target_url": target_url,
                "delivery_attempts": attempts,
                "delivery_error": f"notification delivery error: {exc}",
                "status": (
                    EscalationStatus.NOTIFICATION_FAILED
                    if is_terminal
                    else EscalationStatus.PENDING_NOTIFICATION
                ),
                "remote_resume_enabled": (
                    False if is_terminal else escalation.remote_resume_enabled
                ),
                "reply_cursor": "closed" if is_terminal else escalation.reply_cursor,
                "updated_at": utc_now(),
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
    return run


def validate_reply_candidate(
    comment: GitHubComment,
    *,
    run: FactoryRun,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    factory_login: str | None = None,
    factory_id: int | None = None,
    now: datetime | None = None,
) -> ValidationResult:
    """Validate a candidate comment against all security and protocol rules.

    Returns ValidationResult(is_valid, reason, retryable=...).
    """
    escalation = run.escalation
    if escalation is None or escalation.status is not EscalationStatus.NOTIFIED:
        return ValidationResult(False, "run does not have an active notified escalation")

    if escalation.target_repository is None or escalation.target_number is None:
        return ValidationResult(False, "run escalation target is incomplete")

    # Check command grammar
    parsed = parse_resume_command(comment.body)
    if parsed is None:
        return ValidationResult(False, "command does not match exact resume grammar")

    cmd_run, cmd_episode = parsed
    if cmd_run != run.id:
        return ValidationResult(False, f"command run id {cmd_run!r} does not match {run.id!r}")

    if cmd_episode != escalation.episode_id:
        return ValidationResult(
            False, f"command episode id {cmd_episode!r} does not match {escalation.episode_id!r}"
        )

    # Timing: must be created after the informed notice was successfully posted
    if escalation.last_notified_at is None:
        return ValidationResult(False, "escalation has not been successfully notified")

    if comment.created_at < escalation.last_notified_at:
        return ValidationResult(
            False, "comment was created before escalation notification was posted"
        )

    # Window check
    current_time = now or utc_now()
    window_deadline = escalation.created_at + timedelta(hours=config.escalation.reply_window_hours)
    if current_time > window_deadline:
        return ValidationResult(False, "reply window has expired")

    # Reopen limits
    if escalation.reopen_count >= config.escalation.max_reopens:
        return ValidationResult(
            False,
            f"reopen limit reached ({escalation.reopen_count}/{config.escalation.max_reopens})",
        )

    # Resumable class check
    if escalation.resume_classification is not ResumeClassification.RISK_APPROVAL:
        return ValidationResult(
            False, f"halt category {escalation.resume_classification} is not resumable via reply"
        )

    # Remote resume enabled check
    if not escalation.remote_resume_enabled:
        return ValidationResult(
            False, "remote resume is disabled for this escalation; local inspection required"
        )

    # Risk approval decision context check
    if escalation.approval_context is None or not is_valid_risk_approval_context(
        escalation.approval_context, run.id, escalation.episode_id
    ):
        return ValidationResult(
            False, "missing or invalid risk approval decision context; local inspection required"
        )

    # Replay check
    if any(receipt.comment_id == comment.id for receipt in escalation.accepted_replies):
        return ValidationResult(False, f"comment {comment.id} has already been accepted")

    # Target host check
    target_host = escalation.target_host or (
        config.escalation.allowed_hosts[0] if config.escalation.allowed_hosts else "github.com"
    )
    allowed_hosts = {h.casefold() for h in config.escalation.allowed_hosts}
    if target_host.casefold() not in allowed_hosts:
        return ValidationResult(False, f"target host {target_host!r} is not allowed")

    # Author authorization (must verify authenticated factory identity to fail closed)
    if factory_login is None and factory_id is None:
        try:
            identity = client.get_authenticated_user(repo_path, hostname=target_host)
            factory_login = identity.login
            factory_id = identity.id
        except GitHubError as exc:
            return ValidationResult(
                False, f"cannot prove author is not factory account: {exc}", retryable=True
            )
        if not factory_login and factory_id is None:
            return ValidationResult(
                False,
                "cannot prove author is not factory account: identity unresolved",
                retryable=True,
            )

    if not is_authorized_author(
        comment,
        authorized_identities=config.escalation.authorized_identities,
        allowed_associations=config.escalation.allowed_associations,
        factory_login=factory_login,
        factory_id=factory_id,
    ):
        return ValidationResult(False, "author is not authorized")

    # Immediate re-fetch on exact host to detect edits or deletions

    try:
        fresh = client.get_issue_comment(
            repo_path,
            repository=escalation.target_repository,
            comment_id=comment.id,
            hostname=target_host,
        )
    except GitHubError as exc:
        return ValidationResult(
            False, f"could not re-fetch comment {comment.id}: {exc}", retryable=True
        )

    if fresh.id != comment.id:
        return ValidationResult(False, "re-fetched comment id mismatch")

    if fresh.created_at != comment.created_at:
        return ValidationResult(False, "re-fetched comment creation time mismatch")

    if fresh.updated_at != fresh.created_at:
        return ValidationResult(False, "comment was edited after creation")

    fresh_parsed = parse_resume_command(fresh.body)
    if fresh_parsed != parsed:
        return ValidationResult(False, "re-fetched comment body no longer matches resume command")

    if not is_authorized_author(
        fresh,
        authorized_identities=config.escalation.authorized_identities,
        allowed_associations=config.escalation.allowed_associations,
        factory_login=factory_login,
        factory_id=factory_id,
    ):
        return ValidationResult(False, "re-fetched author is not authorized")

    return ValidationResult(True, "valid")


def poll_escalation_reply(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    *,
    factory_login: str | None = None,
    factory_id: int | None = None,
    now: datetime | None = None,
) -> AcceptedReplyReceipt | None:
    """Poll GitHub comments for an authorized reply to an escalated run.

    Uses bounded pagination across ticks with a persisted cursor to avoid
    missing comments and avoid unbounded scans. If a valid reply is found,
    records the decision receipt on the run and persists it to disk before returning.
    """
    if not config.escalation.enabled:
        return None

    if run.state is not WorkflowState.NEEDS_HUMAN:
        return None

    escalation = run.escalation
    if escalation is None or escalation.status is not EscalationStatus.NOTIFIED:
        return None

    if escalation.last_notified_at is None:
        return None
    notified_at = escalation.last_notified_at

    if escalation.reply_cursor == "closed":
        return None

    target_repo = escalation.target_repository
    target_num = escalation.target_number
    if target_repo is None or target_num is None:
        return None

    current_time = now or utc_now()
    if (
        escalation.resume_classification is not ResumeClassification.RISK_APPROVAL
        or not escalation.remote_resume_enabled
        or escalation.approval_context is None
        or not is_valid_risk_approval_context(
            escalation.approval_context, run.id, escalation.episode_id
        )
    ):
        escalation = escalation.model_copy(
            update={
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    window_deadline = escalation.created_at + timedelta(hours=config.escalation.reply_window_hours)
    if current_time > window_deadline:
        escalation = escalation.model_copy(
            update={
                "status": EscalationStatus.EXPIRED,
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    if escalation.reopen_count >= config.escalation.max_reopens:
        escalation = escalation.model_copy(
            update={
                "remote_resume_enabled": False,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return None

    target_host = escalation.target_host or (
        config.escalation.allowed_hosts[0] if config.escalation.allowed_hosts else "github.com"
    )
    allowed_hosts = {h.casefold() for h in config.escalation.allowed_hosts}
    if target_host.casefold() not in allowed_hosts:
        return None

    if factory_login is None and factory_id is None:
        try:
            identity = client.get_authenticated_user(repo_path, hostname=target_host)
            factory_login = identity.login
            factory_id = identity.id
        except GitHubError as exc:
            logger.warning(
                "could not resolve authenticated github user on %s; skipping reply polling: %s",
                target_host,
                exc,
            )
            return None
        if not factory_login and factory_id is None:
            logger.warning(
                "authenticated github user unverified on %s; skipping reply polling",
                target_host,
            )
            return None

    cursor_page = 1
    cursor_since: datetime = notified_at
    cursor_last_id: int | None = None
    if escalation.reply_cursor and escalation.reply_cursor != "closed":
        try:
            cursor_data = json.loads(escalation.reply_cursor)
            if isinstance(cursor_data, dict):
                cursor_page = max(1, int(cursor_data.get("page", 1)))
                if "since" in cursor_data and cursor_data["since"]:
                    cursor_since = datetime.fromisoformat(cursor_data["since"])
                if "last_id" in cursor_data and cursor_data["last_id"] is not None:
                    cursor_last_id = int(cursor_data["last_id"])
        except (ValueError, TypeError, json.JSONDecodeError):
            cursor_page = 1
            cursor_since = notified_at
            cursor_last_id = None

    max_pages_per_poll = 2
    current_page = cursor_page
    next_cursor = None
    accepted_receipt = None
    latest_id = cursor_last_id
    latest_timestamp: datetime = cursor_since

    for _ in range(max_pages_per_poll):
        try:
            comments = client.list_issue_comments(
                repo_path,
                repository=target_repo,
                issue_number=target_num,
                since=cursor_since,
                page=current_page,
                per_page=100,
                hostname=target_host,
            )
        except GitHubError as exc:
            logger.debug("error listing comments for run %s: %s", run.id, exc)
            break

        if not comments:
            next_cursor = json.dumps(
                {
                    "page": 1,
                    "since": latest_timestamp.isoformat(),
                    "last_id": latest_id,
                }
            )
            break

        retryable_stopped = False
        for comment in comments:
            if (
                cursor_last_id is not None
                and comment.created_at == cursor_since
                and comment.id <= cursor_last_id
            ):
                continue

            result = validate_reply_candidate(
                comment,
                run=run,
                config=config,
                client=client,
                repo_path=repo_path,
                factory_login=factory_login,
                factory_id=factory_id,
                now=current_time,
            )
            if result.is_valid:
                app_fp = (
                    escalation.approval_context.context_fingerprint
                    if (
                        escalation.resume_classification is ResumeClassification.RISK_APPROVAL
                        and escalation.approval_context is not None
                    )
                    else None
                )
                accepted_receipt = AcceptedReplyReceipt(
                    comment_id=comment.id,
                    user_login=comment.user_login,
                    user_id=comment.user_id,
                    author_association=comment.author_association,
                    created_at=comment.created_at,
                    accepted_at=current_time,
                    command=f"@factory resume v1 run={run.id} episode={escalation.episode_id}",
                    episode_id=escalation.episode_id,
                    run_id=run.id,
                    approval_context_fingerprint=app_fp,
                )
                break

            if getattr(result, "retryable", False):
                logger.info(
                    "retryable validation failure for comment %s on run %s: %s; "
                    "stopping cursor advancement",
                    comment.id,
                    run.id,
                    result.reason,
                )
                retryable_stopped = True
                break

            if latest_id is None or comment.id > latest_id:
                latest_id = comment.id
            if comment.created_at > latest_timestamp:
                latest_timestamp = comment.created_at

        if accepted_receipt is not None or retryable_stopped:
            if retryable_stopped and latest_timestamp is not None:
                next_cursor = json.dumps(
                    {
                        "page": 1,
                        "since": latest_timestamp.isoformat(),
                        "last_id": latest_id,
                    }
                )
            break

        if len(comments) < 100:
            next_cursor = json.dumps(
                {
                    "page": 1,
                    "since": latest_timestamp.isoformat(),
                    "last_id": latest_id,
                }
            )
            break
        else:
            current_page += 1
            next_cursor = json.dumps(
                {
                    "page": current_page,
                    "since": cursor_since.isoformat(),
                    "last_id": latest_id,
                }
            )

    if accepted_receipt is not None:
        escalation = escalation.model_copy(
            update={
                "accepted_replies": [*escalation.accepted_replies, accepted_receipt],
                "reopen_count": escalation.reopen_count + 1,
                "status": EscalationStatus.REOPENED,
                "reply_cursor": "closed",
                "updated_at": current_time,
            }
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)
        return accepted_receipt

    if next_cursor is not None and next_cursor != escalation.reply_cursor:
        escalation = escalation.model_copy(
            update={"reply_cursor": next_cursor, "updated_at": current_time}
        )
        run = run.model_copy(update={"escalation": escalation})
        store.save_run(run)

    return None


def reconcile_undelivered_notifications(
    store: FileRunStore,
    config: FactoryConfig,
    client: GitHubClient,
    repo_path: Path,
    *,
    max_runs: int = 10,
    expected_repository: str | None = None,
) -> list[FactoryRun]:
    """Find and deliver notifications for runs in NEEDS_HUMAN needing delivery."""
    if not config.escalation.enabled:
        return []

    updated_runs: list[FactoryRun] = []
    runs = store.list_runs()
    for run in runs:
        if len(updated_runs) >= max_runs:
            break
        if run.state is not WorkflowState.NEEDS_HUMAN:
            continue
        escalation = run.escalation
        needs_notification = escalation is None or (
            escalation.status
            in {
                EscalationStatus.PENDING_NOTIFICATION,
                EscalationStatus.NOTIFICATION_FAILED,
            }
            and escalation.delivery_attempts < config.escalation.max_notification_attempts
        )
        if needs_notification:
            updated = deliver_escalation_notification(
                run,
                store,
                config,
                client,
                repo_path,
                expected_repository=expected_repository,
            )
            updated_runs.append(updated)

    return updated_runs
