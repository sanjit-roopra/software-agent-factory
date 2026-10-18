"""Decision contexts for a halt that a person can resume.

When a run halts in ``NEEDS_HUMAN`` with a resumable class, the factory snapshots the
context the person decides: the risk approval, the plan decisions, the reviewed work to
publish again or the used-up attempt budget. :func:`build_resume_context` picks the builder
from the class, one per entry of :data:`.resume_classes.RESUME_CLASSES`. The builders read
the run's artifacts through the store, so they live here and not in the pure
:mod:`.resume_classes`.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from collections.abc import Callable

from .config import FactoryConfig
from .escalation_protocol import MAX_PLAN_DECISIONS
from .models import (
    AttemptBudget,
    AttemptGrantContext,
    DeliveryRetryContext,
    ExecutionPlan,
    FactoryRun,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    TriageResult,
    WorkflowState,
    WorkItem,
)
from .redaction import redact_secrets
from .resume import (
    compute_approval_context_fingerprint,
    compute_delivery_retry_context_fingerprint,
    compute_plan_decision_context_fingerprint,
    contains_unsafe_content,
    receipt_approves_risk_context,
)
from .resume_classes import (
    RESUME_CLASSES,
    ResumeContext,
    compute_attempt_grant_context_fingerprint,
)
from .store import FileRunStore

logger = logging.getLogger(__name__)


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
        f"Approve advancing run {run.id} to PLANNING under risk policy {triage_result.risk.value}."
    )
    authorized_actions = [
        "Transition workflow from NEEDS_HUMAN to PLANNING.",
        "Write an explicit specification and plan within approved scope.",
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
        "Approval resumes the same run at PLANNING.",
        "Approval does not reset run history or attempt budgets.",
    ]

    clean_rationale = RiskRationale(
        intended_outcome=clean_outcome,
        sensitive_boundary=clean_boundary,
        necessity=clean_necessity,
        credible_scenario=clean_scenario,
        known_mitigations=clean_mitigations,
        residual_risk=clean_residual,
    )

    fingerprint = compute_approval_context_fingerprint(
        run_id=run.id,
        episode_id=current_episode_id,
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk=triage_result.risk.value,
        complexity=triage_result.complexity.value,
        rationale=clean_rationale,
        decision_requested=decision_requested,
        next_state=WorkflowState.PLANNING.value,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
    )

    return RiskApprovalContext(
        risk=triage_result.risk,
        complexity=triage_result.complexity,
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk_rationale=clean_rationale,
        decision_requested=decision_requested,
        next_state=WorkflowState.PLANNING,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
        context_fingerprint=fingerprint,
    )


def has_dispatched_risk_approval(
    run: FactoryRun,
    store: FileRunStore,
    *,
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
            work_item=work_item,
            triage_result=triage_result,
            episode_id=receipt.episode_id,
        )
        if context is None:
            continue
        if receipt_approves_risk_context(receipt, context):
            return True
    return False


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


def build_delivery_retry_context(
    run: FactoryRun, *, episode_id: str
) -> DeliveryRetryContext | None:
    """Snapshot the reviewed work a person may publish again, or ``None`` when it is unknown."""
    if not run.reviewed_tree_sha or not run.base_commit_sha or not run.branch_name:
        return None
    return DeliveryRetryContext(
        reviewed_tree_sha=run.reviewed_tree_sha,
        base_commit_sha=run.base_commit_sha,
        branch_name=run.branch_name,
        context_fingerprint=compute_delivery_retry_context_fingerprint(
            run_id=run.id,
            episode_id=episode_id,
            reviewed_tree_sha=run.reviewed_tree_sha,
            base_commit_sha=run.base_commit_sha,
            branch_name=run.branch_name,
        ),
    )


def _delivery_retry_context(
    run: FactoryRun, _store: FileRunStore, _config: FactoryConfig, episode_id: str
) -> DeliveryRetryContext | None:
    # Only a publish that failed from PR_READY can be published again, and only transition()
    # sees the run in that state (ADR-042).
    if run.state is not WorkflowState.PR_READY:
        return None
    return build_delivery_retry_context(run, episode_id=episode_id)


#: The last failure a grant context shows when the repair text is missing or unsafe.
_UNKNOWN_LAST_FAILURE = "The last attempt failed. The run artifacts hold the details."


def build_attempt_grant_context(run: FactoryRun, *, episode_id: str) -> AttemptGrantContext | None:
    """Snapshot the used-up implementation budget a person may extend by one attempt."""
    budget = AttemptBudget.IMPLEMENTATION
    used = run.attempts_used(budget)
    if used < 1:
        return None
    repair = run.exhausted_repair
    summary = normalize_whitespace(redact_secrets(repair.summary))[:300] if repair else ""
    if not summary or contains_unsafe_content(summary)[0]:
        summary = _UNKNOWN_LAST_FAILURE
    return AttemptGrantContext(
        budget=budget,
        attempts_used=used,
        last_failure=summary,
        context_fingerprint=compute_attempt_grant_context_fingerprint(
            run_id=run.id,
            episode_id=episode_id,
            budget=budget.value,
            attempts_used=used,
            last_failure=summary,
        ),
    )


#: How a halt snapshots its decision context, one builder per resumable class.
_CONTEXT_BUILDERS: dict[
    ResumeClassification,
    Callable[[FactoryRun, FileRunStore, FactoryConfig, str], ResumeContext | None],
] = {
    ResumeClassification.RISK_APPROVAL: lambda run, store, _config, episode_id: (
        build_risk_approval_context(run, store, episode_id=episode_id)
    ),
    ResumeClassification.PLAN_DECISION: lambda run, store, _config, episode_id: (
        build_plan_decision_context(run, store, episode_id=episode_id)
    ),
    ResumeClassification.DELIVERY_RETRY: _delivery_retry_context,
    ResumeClassification.ATTEMPT_GRANT: lambda run, _store, _config, episode_id: (
        build_attempt_grant_context(run, episode_id=episode_id)
    ),
}


def build_resume_context(
    run: FactoryRun,
    store: FileRunStore,
    config: FactoryConfig,
    classification: ResumeClassification,
    episode_id: str,
) -> tuple[ResumeClassification, dict[str, ResumeContext]]:
    """The class a new halt episode keeps, and its decision context as escalation fields.

    A class a GitHub reply cannot resume has no other way to resume than its context, so
    without one it becomes ``NOT_RESUMABLE``. The fields are empty when there is no context.
    """
    spec = RESUME_CLASSES.get(classification)
    builder = _CONTEXT_BUILDERS.get(classification)
    if spec is None or builder is None:
        return classification, {}
    context = builder(run, store, config, episode_id)
    if context is None:
        if not spec.github_replies:
            return ResumeClassification.NOT_RESUMABLE, {}
        return classification, {}
    return classification, {spec.context_field: context}
