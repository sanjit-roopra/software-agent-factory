"""Controller-owned GitHub escalation notices and authorized human reply loop.

Implements the core escalation and authorized human reply loop:
- When a run enters ``NEEDS_HUMAN``, the factory posts a concise, safe status
  comment on the persisted open factory PR if available, falling back to the
  source GitHub issue reference.
- An authorized human contributor may reply on that exact thread with:
  ``@factory resume v1 run=<run-id> episode=<opaque-id>``
- The controller validates the comment, author, timing, target, and episode,
  records a durable decision receipt, and reopens the run when the halt category
  is supported (``RISK_APPROVAL`` -> ``REFINING`` or
  ``PLAN_DECISION`` -> ``PLANNING``).
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
from .escalation_protocol import (
    ANSWER_COMMAND_PATTERN,
    MAX_PLAN_DECISIONS,
    RESUME_COMMAND_PATTERN,
    format_answer_command,
    format_resume_command,
    notice_host,
)
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
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
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
from .resume import (
    ReplyIdentity,
    accept_resume,
    build_plan_answers,
    compute_approval_context_fingerprint,
    compute_plan_decision_context_fingerprint,
    contains_unsafe_content,
    resume_refusal,
)
from .resume import is_valid_plan_decision_context as is_valid_plan_decision_context
from .resume import is_valid_risk_approval_context as is_valid_risk_approval_context
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


_NUMBERED_ANSWER_PATTERN = re.compile(r"^(?P<number>[1-9][0-9]?)\.\s+(?P<answer>\S.*)$")

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
    match = RESUME_COMMAND_PATTERN.fullmatch(cleaned)
    if match is None:
        return None
    return match.group("run"), match.group("episode")


def parse_plan_decision_answers(
    body: str, *, decision_count: int
) -> tuple[str, str, list[PlanDecisionAnswer]] | None:
    """Parse a strict, complete numbered response to a plan-decision notice."""
    if not 1 <= decision_count <= MAX_PLAN_DECISIONS:
        return None
    normalized_body = body.replace("\r\n", "\n")
    if normalized_body != normalized_body.strip():
        return None
    lines = normalized_body.split("\n")
    if len(lines) != decision_count + 1:
        return None
    header = ANSWER_COMMAND_PATTERN.fullmatch(lines[0])
    if header is None:
        return None
    texts: list[str] = []
    for expected_number, line in enumerate(lines[1:], start=1):
        match = _NUMBERED_ANSWER_PATTERN.fullmatch(line)
        if match is None or int(match.group("number")) != expected_number:
            return None
        texts.append(match.group("answer"))
    answers = build_plan_answers(texts, decision_count=decision_count)
    if answers is None:
        return None
    return header.group("run"), header.group("episode"), answers


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


def receipt_approves_risk_context(
    receipt: AcceptedReplyReceipt, context: RiskApprovalContext
) -> bool:
    """Whether ``receipt`` carries the fingerprint of exactly this approval ``context``."""
    return receipt.approval_context_fingerprint is not None and secrets.compare_digest(
        receipt.approval_context_fingerprint, context.context_fingerprint
    )


def has_dispatched_risk_approval(
    run: FactoryRun,
    store: FileRunStore,
    *,
    config: FactoryConfig,
    work_item: WorkItem,
    triage_result: TriageResult,
) -> bool:
    """Whether a human already approved the persisted work item and triage for ``run``.

    Needs an accepted reply receipt for this run that was acted on (``dispatched_at``)
    and carries an approval fingerprint. The approval context is rebuilt from
    ``work_item`` and ``triage_result`` for the receipt's own episode, and the
    fingerprints must match.

    The approval binds the context the human was shown, not the whole triage: run id,
    episode id, work item id and title, risk, complexity, and the cleaned, truncated
    risk rationale. A change to any of those after approval revokes it. The rebuilt
    context also holds fixed action and condition text, so a release that changes that
    text makes older approvals fail closed on resume.

    A receipt matches only its own episode, because the episode id is part of the
    fingerprint. Receipts from earlier episodes are kept across escalations and still
    count.
    """
    if run.escalation is None:
        return False
    for receipt in run.escalation.accepted_replies:
        if (
            receipt.run_id != run.id
            or receipt.dispatched_at is None
            or receipt.approval_context_fingerprint is None
        ):
            continue
        context = build_risk_approval_context(
            run=run,
            store=store,
            config=config,
            work_item=work_item,
            triage_result=triage_result,
            episode_id=receipt.episode_id,
        )
        if context is None:
            continue
        if receipt_approves_risk_context(receipt, context):
            return True
    return False


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


def _execution_plan_fingerprint(plan: ExecutionPlan) -> str:
    payload = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan_decision_context(
    run: FactoryRun,
    store: FileRunStore,
    *,
    episode_id: str,
) -> PlanDecisionContext | None:
    """Snapshot safe unanswered plan decisions before asking for human input."""
    try:
        plan = store.load_artifact(run.id, ExecutionPlan)
    except (FileNotFoundError, ValueError):
        logger.warning("run %s has no valid execution plan for decision escalation", run.id)
        return None
    if not plan.unresolved_decisions or len(plan.unresolved_decisions) > MAX_PLAN_DECISIONS:
        logger.warning("run %s has an invalid unresolved decision count", run.id)
        return None
    decisions = [normalize_whitespace(redact_secrets(value)) for value in plan.unresolved_decisions]
    if any(not decision or contains_unsafe_content(decision)[0] for decision in decisions):
        logger.warning("run %s has unsafe unresolved decision content", run.id)
        return None
    plan_fingerprint = _execution_plan_fingerprint(plan)
    return PlanDecisionContext(
        plan_fingerprint=plan_fingerprint,
        decisions=decisions,
        context_fingerprint=compute_plan_decision_context_fingerprint(
            run_id=run.id,
            episode_id=episode_id,
            plan_fingerprint=plan_fingerprint,
            decisions=decisions,
        ),
    )


def is_valid_plan_decision_answers(
    answers: PlanDecisionAnswers | None,
    context: PlanDecisionContext,
    *,
    run_id: str,
    episode_id: str,
    receipt: AcceptedReplyReceipt,
) -> bool:
    """Verify persisted human answers still bind to the active decision episode."""
    if not isinstance(answers, PlanDecisionAnswers):
        return False
    if (
        answers.run_id != run_id
        or answers.episode_id != episode_id
        or answers.source != receipt.source
        or answers.comment_id != receipt.comment_id
        or answers.user_login != receipt.user_login
        or answers.user_id != receipt.user_id
        or answers.author_association != receipt.author_association
    ):
        return False
    if not (
        secrets.compare_digest(answers.plan_fingerprint, context.plan_fingerprint)
        and secrets.compare_digest(answers.context_fingerprint, context.context_fingerprint)
        and receipt.plan_decision_context_fingerprint is not None
        and secrets.compare_digest(
            receipt.plan_decision_context_fingerprint, context.context_fingerprint
        )
    ):
        return False
    rebuilt = build_plan_answers(
        [answer.answer for answer in answers.answers], decision_count=len(context.decisions)
    )
    return rebuilt == answers.answers


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
            ResumeClassification.PLAN_DECISION,
            UNRESOLVED_DECISIONS_REASON_CODE,
            summary,
            "Reply with complete numbered decisions on this GitHub thread.",
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
    plan_decision_context: PlanDecisionContext | None = None,
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
                format_resume_command(run_id, episode_id),
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

    if classification is ResumeClassification.PLAN_DECISION:
        if plan_decision_context is not None and is_valid_plan_decision_context(
            plan_decision_context, run_id, episode_id
        ):
            decision_lines = [
                f"{index}. {escape_notice_text(decision)}"
                for index, decision in enumerate(plan_decision_context.decisions, start=1)
            ]
            answer_lines = [
                f"{index}. <answer>" for index in range(1, len(plan_decision_context.decisions) + 1)
            ]
            lines = [
                marker,
                "### Factory Plan Decision Notice",
                "",
                f"The run `{run_id}` needs {len(plan_decision_context.decisions)} decision(s).",
                "",
                f"- **Reason code**: `{reason_code}`",
                f"- **Attempts recorded**: {attempts_consumed}",
                f"- **Reopens**: {reopen_count}/{max_reopens}",
                "",
                "#### Decisions",
                *decision_lines,
                "",
                "#### Reply instructions",
                "An authorized contributor must reply with every numbered answer:",
                "",
                "```",
                format_answer_command(run_id, episode_id),
                *answer_lines,
                "```",
                "",
                "The factory will replan with these answers before it changes code.",
                "",
            ]
            rendered = "\n".join(lines)
            if len(rendered) <= MAX_ESCALATION_COMMENT_CHARS:
                return EscalationComment(rendered, remote_resume_enabled=True)
            logger.warning(
                "run %s plan decision comment exceeded size limit (%d chars)",
                run_id,
                len(rendered),
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
            "#### Reply instructions",
            "",
            "The plan decision context cannot be safely resumed from GitHub. "
            "Inspect local artifacts and start a replacement run.",
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
        plan_decision_context = None
        if classification is ResumeClassification.PLAN_DECISION:
            plan_decision_context = build_plan_decision_context(
                run,
                store,
                episode_id=episode_id,
            )
        escalation = EscalationRecord(
            episode_id=episode_id,
            episode_number=1,
            status=EscalationStatus.PENDING_NOTIFICATION,
            resume_classification=classification,
            reason_code=code,
            approval_context=approval_context,
            plan_decision_context=plan_decision_context,
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
        plan_decision_context=escalation.plan_decision_context,
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
    parsed_answers: list[PlanDecisionAnswer] | None = None
    if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
        parsed = parse_resume_command(comment.body)
        if parsed is None:
            return ValidationResult(False, "command does not match exact resume grammar")
    elif escalation.resume_classification is ResumeClassification.PLAN_DECISION:
        context = escalation.plan_decision_context
        if context is None or not is_valid_plan_decision_context(
            context, run.id, escalation.episode_id
        ):
            return ValidationResult(
                False, "missing or invalid plan decision context; local inspection required"
            )
        parsed_answer_command = parse_plan_decision_answers(
            comment.body, decision_count=len(context.decisions)
        )
        if parsed_answer_command is None:
            return ValidationResult(False, "command does not contain complete numbered answers")
        cmd_run, cmd_episode, parsed_answers = parsed_answer_command
        parsed = (cmd_run, cmd_episode)
    else:
        return ValidationResult(
            False, f"halt category {escalation.resume_classification} is not resumable via reply"
        )

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

    # Remote resume enabled check
    if not escalation.remote_resume_enabled:
        return ValidationResult(
            False, "remote resume is disabled for this escalation; local inspection required"
        )

    # Decision context check
    if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
        if escalation.approval_context is None or not is_valid_risk_approval_context(
            escalation.approval_context, run.id, escalation.episode_id
        ):
            return ValidationResult(
                False,
                "missing or invalid risk approval decision context; local inspection required",
            )
    elif parsed_answers is None:
        return ValidationResult(False, "plan decision reply is missing validated answers")

    # Replay check
    if any(
        receipt.source == "github" and receipt.comment_id == comment.id
        for receipt in escalation.accepted_replies
    ):
        return ValidationResult(False, f"comment {comment.id} has already been accepted")

    # Target host check
    target_host = notice_host(escalation, config.escalation.allowed_hosts)
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

    if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
        fresh_parsed = parse_resume_command(fresh.body)
        if fresh_parsed != parsed:
            return ValidationResult(
                False, "re-fetched comment body no longer matches resume command"
            )
    else:
        context = escalation.plan_decision_context
        assert context is not None
        fresh_answer_command = parse_plan_decision_answers(
            fresh.body, decision_count=len(context.decisions)
        )
        original_answer_command = parse_plan_decision_answers(
            comment.body, decision_count=len(context.decisions)
        )
        if fresh_answer_command is None or fresh_answer_command != original_answer_command:
            return ValidationResult(
                False, "re-fetched comment body no longer matches complete numbered answers"
            )

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
    refusal = resume_refusal(run, config, current_time)
    if not escalation.remote_resume_enabled or refusal == "context_changed":
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

    if refusal == "expired":
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

    if refusal == "reopen_limit":
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

    target_host = notice_host(escalation, config.escalation.allowed_hosts)
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
                plan_answers: list[PlanDecisionAnswer] | None = None
                if escalation.resume_classification is ResumeClassification.PLAN_DECISION:
                    context = escalation.plan_decision_context
                    assert context is not None
                    parsed_plan_reply = parse_plan_decision_answers(
                        comment.body, decision_count=len(context.decisions)
                    )
                    if parsed_plan_reply is None:
                        raise ValueError("validated plan decision reply could not be parsed")
                    _, _, plan_answers = parsed_plan_reply
                accepted_receipt = accept_resume(
                    run,
                    store,
                    config,
                    reply=ReplyIdentity(
                        source="github",
                        comment_id=comment.id,
                        user_login=comment.user_login,
                        user_id=comment.user_id,
                        author_association=comment.author_association,
                        created_at=comment.created_at,
                    ),
                    answers=plan_answers,
                    now=current_time,
                )
                if accepted_receipt is None:
                    # The stored run no longer accepts a reply (the dashboard path got there
                    # first). Saving this snapshot's cursor would overwrite its work.
                    return None
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
