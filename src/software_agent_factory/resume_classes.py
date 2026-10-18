"""The resumable halt classes, and what each one asks a person to decide.

A run that halts in ``NEEDS_HUMAN`` with a resumable :class:`ResumeClassification` waits for a
person. :data:`RESUME_CLASSES` holds one :class:`ResumeClass` per such class: where its
decision context lives on the escalation, how that context is checked, the reply command its
receipt records, the state a reopen resumes at, its dashboard action and copy, and whether a
GitHub reply may answer it. Code that differs per class looks the class up here, so a new
class is one entry, not a branch in every module.

This module also holds the context fingerprints and validity checks the entries name. Like
:mod:`.resume`, it is pure: it imports only :mod:`.models`, :mod:`.escalation_protocol`,
:mod:`.redaction` and the standard library. The context builders read run artifacts, so they
live in :mod:`.escalation`, and the reopen checks need the controller, so they live in
:mod:`.run_reopen`. Both key on this table, and a test checks they cover it.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .escalation_protocol import MAX_PLAN_DECISIONS, format_answer_command, format_resume_command
from .models import (
    AttemptBudget,
    AttemptGrantContext,
    DeliveryRetryContext,
    EscalationRecord,
    FindingsAcceptanceContext,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    ScopeApprovalContext,
    WorkflowState,
)
from .redaction import contains_secret

#: The decision context a resumable halt stores on its escalation.
ResumeContext = (
    RiskApprovalContext
    | PlanDecisionContext
    | DeliveryRetryContext
    | AttemptGrantContext
    | FindingsAcceptanceContext
    | ScopeApprovalContext
)

# Absolute, network, and system file system paths
_ABSOLUTE_OR_NETWORK_PATH_PATTERN = re.compile(
    r"(?i)"
    r"(?:(?<![A-Za-z0-9.~/@\\<])(?<!&lt;)/(?:[A-Za-z0-9_.-]+)[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])~[\\/][^\s\"'`>)]+)"
    r"|(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])\\\\[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.:])//[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
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


def contains_unsafe_content(text: str) -> tuple[bool, str]:
    """Check whether text contains paths, embedded credentials, tokens, URLs, or diagnostics."""
    if not text:
        return False, ""
    if contains_secret(text):
        return True, "contains token or credential"
    if _EXTERNAL_URL_PATTERN.search(text):
        return True, "contains external URL or link"
    if _ABSOLUTE_OR_NETWORK_PATH_PATTERN.search(text):
        return True, "contains local or network file system path"
    if _RAW_DIAGNOSTIC_PATTERN.search(text):
        return True, "contains raw diagnostic or diff output"
    return False, ""


def _fingerprint_dict(data: dict[str, object]) -> str:
    """SHA-256 of ``data`` as canonical JSON: sorted keys, no spaces."""
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_approval_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    work_item_id: str,
    work_item_title: str,
    risk: str,
    complexity: str,
    rationale: RiskRationale,
    decision_requested: str,
    next_state: str,
    authorized_actions: Sequence[str],
    unauthorized_actions: Sequence[str],
    conditions_in_force: Sequence[str],
) -> str:
    """Compute deterministic SHA-256 binding displayed and authority fields to episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "work_item_id": work_item_id,
            "work_item_title": work_item_title,
            "risk": risk,
            "complexity": complexity,
            "intended_outcome": rationale.intended_outcome,
            "sensitive_boundary": rationale.sensitive_boundary,
            "necessity": rationale.necessity,
            "credible_scenario": rationale.credible_scenario,
            "known_mitigations": list(rationale.known_mitigations),
            "residual_risk": rationale.residual_risk,
            "decision_requested": decision_requested,
            "next_state": next_state,
            "authorized_actions": list(authorized_actions),
            "unauthorized_actions": list(unauthorized_actions),
            "conditions_in_force": list(conditions_in_force),
        }
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
    # REFINING is the next state in approval contexts written before ADR-035.
    if context.next_state not in {WorkflowState.PLANNING, WorkflowState.REFINING}:
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
        rationale=rationale,
        decision_requested=context.decision_requested,
        next_state=context.next_state.value,
        authorized_actions=context.authorized_actions,
        unauthorized_actions=context.unauthorized_actions,
        conditions_in_force=context.conditions_in_force,
    )
    return secrets.compare_digest(context.context_fingerprint, expected_fp)


def compute_plan_decision_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    plan_fingerprint: str,
    decisions: Sequence[str],
) -> str:
    """Bind a numbered decision set to one run and escalation episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "plan_fingerprint": plan_fingerprint,
            "decisions": list(decisions),
        }
    )


def is_valid_plan_decision_context(
    context: PlanDecisionContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify an answerable decision context is safe and bound to its episode."""
    if not isinstance(context, PlanDecisionContext):
        return False
    if not 1 <= len(context.decisions) <= MAX_PLAN_DECISIONS:
        return False
    if any(not value or contains_unsafe_content(value)[0] for value in context.decisions):
        return False
    expected = compute_plan_decision_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        plan_fingerprint=context.plan_fingerprint,
        decisions=context.decisions,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


def compute_delivery_retry_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    reviewed_tree_sha: str,
    base_commit_sha: str,
    branch_name: str,
) -> str:
    """Bind the reviewed work a publish retry would deliver to one run and escalation episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "reviewed_tree_sha": reviewed_tree_sha,
            "base_commit_sha": base_commit_sha,
            "branch_name": branch_name,
        }
    )


def is_valid_delivery_retry_context(
    context: DeliveryRetryContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify a publish retry context is bound to its run and episode."""
    if not isinstance(context, DeliveryRetryContext):
        return False
    expected = compute_delivery_retry_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        reviewed_tree_sha=context.reviewed_tree_sha,
        base_commit_sha=context.base_commit_sha,
        branch_name=context.branch_name,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


def compute_attempt_grant_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    budget: str,
    attempts_used: int,
    last_failure: str,
) -> str:
    """Bind the used-up budget a person may extend to one run and escalation episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "budget": budget,
            "attempts_used": attempts_used,
            "last_failure": last_failure,
        }
    )


def is_valid_attempt_grant_context(
    context: AttemptGrantContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify an attempt grant context is safe, for the implementation budget, and bound."""
    if not isinstance(context, AttemptGrantContext):
        return False
    if context.budget is not AttemptBudget.IMPLEMENTATION:
        return False
    if contains_unsafe_content(context.last_failure)[0]:
        return False
    expected = compute_attempt_grant_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        budget=context.budget.value,
        attempts_used=context.attempts_used,
        last_failure=context.last_failure,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


def compute_findings_acceptance_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    finding_ids: Sequence[str],
    reviewed_tree_sha: str,
) -> str:
    """Bind the open findings a person may accept, and the tree they are on, to one episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "finding_ids": list(finding_ids),
            "reviewed_tree_sha": reviewed_tree_sha,
        }
    )


def is_valid_findings_acceptance_context(
    context: FindingsAcceptanceContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify a findings acceptance context is bound to its run and episode."""
    if not isinstance(context, FindingsAcceptanceContext):
        return False
    expected = compute_findings_acceptance_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        finding_ids=context.finding_ids,
        reviewed_tree_sha=context.reviewed_tree_sha,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


def compute_scope_approval_context_fingerprint(
    *, run_id: str, episode_id: str, paths: Sequence[str], patch_sha256: str
) -> str:
    """Bind the paths a person may approve, and the patch they are in, to one episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "paths": list(paths),
            "patch_sha256": patch_sha256,
        }
    )


def is_valid_scope_approval_context(
    context: ScopeApprovalContext | None, run_id: str, episode_id: str
) -> bool:
    """Verify a scope approval context is bound to its run and episode."""
    if not isinstance(context, ScopeApprovalContext):
        return False
    expected = compute_scope_approval_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        paths=context.paths,
        patch_sha256=context.patch_sha256,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


@dataclass(frozen=True)
class ResumeClass:
    """How one resumable halt class asks for, records and applies a person's decision."""

    #: The :class:`EscalationRecord` field that holds the decision context.
    context_field: str
    #: The :class:`AcceptedReplyReceipt` field that records the fingerprint the person saw.
    receipt_field: str
    #: Whether a stored context is complete, safe and bound to ``(run_id, episode_id)``.
    is_valid_context: Callable[[Any, str, str], bool]
    #: The reply command a receipt records, from ``(run_id, episode_id)``.
    command: Callable[[str, str], str]
    #: The state a reopened run resumes at.
    reopen_state: WorkflowState
    #: The dashboard write route, as in ``POST /api/runs/<id>/<action>``.
    dashboard_action: str
    #: The ``action`` and label of the dashboard ``needs_you`` outcome.
    needs_you_action: str
    needs_you_label: str
    #: The next-step sentence for a queued dashboard request; ``{moment}`` and ``{start_hint}``.
    queued_sentence: str
    #: The next-step sentence for a dashboard request refused as ``expired``.
    expired_sentence: str
    #: Whether a GitHub reply may resume it. A dashboard-only class needs its context to
    #: resume at all (ADR-042).
    github_replies: bool

    def context(self, escalation: EscalationRecord) -> ResumeContext | None:
        """The decision context ``escalation`` stores for this class, or ``None``."""
        return cast("ResumeContext | None", getattr(escalation, self.context_field))

    def fingerprint(self, escalation: EscalationRecord) -> str | None:
        """The fingerprint of the stored decision context, or ``None`` when there is none."""
        context = self.context(escalation)
        return context.context_fingerprint if context is not None else None


RESUME_CLASSES: Mapping[ResumeClassification, ResumeClass] = {
    ResumeClassification.RISK_APPROVAL: ResumeClass(
        context_field="approval_context",
        receipt_field="approval_context_fingerprint",
        is_valid_context=is_valid_risk_approval_context,
        command=format_resume_command,
        # Approval contexts written before ADR-035 name REFINING. Both resume at PLANNING.
        reopen_state=WorkflowState.PLANNING,
        dashboard_action="approve",
        needs_you_action="waiting",
        needs_you_label="Needs approval",
        queued_sentence="Approved at {moment}, queued for the factory service. {start_hint}",
        expired_sentence="approval expired, approve again",
        github_replies=True,
    ),
    ResumeClassification.PLAN_DECISION: ResumeClass(
        context_field="plan_decision_context",
        receipt_field="plan_decision_context_fingerprint",
        is_valid_context=is_valid_plan_decision_context,
        command=format_answer_command,
        reopen_state=WorkflowState.PLANNING,
        dashboard_action="answer",
        needs_you_action="waiting",
        needs_you_label="Needs answers",
        queued_sentence="Answers sent at {moment}, queued for the factory service",
        expired_sentence="answers expired, send them again",
        github_replies=True,
    ),
    ResumeClassification.DELIVERY_RETRY: ResumeClass(
        context_field="delivery_retry_context",
        receipt_field="delivery_retry_context_fingerprint",
        is_valid_context=is_valid_delivery_retry_context,
        command=format_resume_command,
        reopen_state=WorkflowState.PR_READY,
        dashboard_action="retry",
        needs_you_action="retry",
        needs_you_label="Needs retry",
        queued_sentence=(
            "Retry requested at {moment}, queued for the factory service. {start_hint}"
        ),
        expired_sentence="retry expired, retry again",
        github_replies=False,
    ),
    ResumeClassification.ATTEMPT_GRANT: ResumeClass(
        context_field="attempt_grant_context",
        receipt_field="attempt_grant_context_fingerprint",
        is_valid_context=is_valid_attempt_grant_context,
        command=format_resume_command,
        reopen_state=WorkflowState.IMPLEMENTING,
        dashboard_action="grant",
        needs_you_action="grant",
        needs_you_label="Needs attempt",
        queued_sentence=(
            "One more attempt granted at {moment}, queued for the factory service. {start_hint}"
        ),
        expired_sentence="grant expired, grant again",
        github_replies=False,
    ),
    ResumeClassification.ACCEPT_FINDINGS: ResumeClass(
        context_field="findings_acceptance_context",
        receipt_field="findings_acceptance_context_fingerprint",
        is_valid_context=is_valid_findings_acceptance_context,
        command=format_resume_command,
        reopen_state=WorkflowState.PR_READY,
        dashboard_action="accept",
        needs_you_action="accept",
        needs_you_label="Needs acceptance",
        queued_sentence=(
            "Findings accepted at {moment}, queued for the factory service. {start_hint}"
        ),
        expired_sentence="acceptance expired, accept again",
        github_replies=False,
    ),
    ResumeClassification.SCOPE_APPROVAL: ResumeClass(
        context_field="scope_approval_context",
        receipt_field="scope_approval_context_fingerprint",
        is_valid_context=is_valid_scope_approval_context,
        command=format_resume_command,
        # The run verifies the same work again, then goes on past the scope check (ADR-060).
        reopen_state=WorkflowState.IMPLEMENTING,
        dashboard_action="widen",
        needs_you_action="widen",
        needs_you_label="Needs scope approval",
        queued_sentence=(
            "Wider scope approved at {moment}, queued for the factory service. {start_hint}"
        ),
        expired_sentence="scope approval expired, approve again",
        github_replies=False,
    ),
}

#: The classes a GitHub reply may resume. The others are dashboard only (ADR-042, ADR-057).
GITHUB_REPLY_CLASSES: frozenset[ResumeClassification] = frozenset(
    classification for classification, spec in RESUME_CLASSES.items() if spec.github_replies
)
