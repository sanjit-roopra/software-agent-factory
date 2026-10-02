"""Golden digests for the two context fingerprints.

A fingerprint is stored in ``run.json`` and in every receipt. A release that changes how it is
computed makes each stored approval and each open decision fail closed. These digests pin the
persisted format: change one only on purpose, with a migration plan.
"""

from __future__ import annotations

from software_agent_factory.models import (
    Complexity,
    PlanDecisionContext,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.resume import (
    compute_approval_context_fingerprint,
    compute_plan_decision_context_fingerprint,
    is_valid_plan_decision_context,
    is_valid_risk_approval_context,
)

RUN_ID = "run-golden"
EPISODE_ID = "ep-golden"
WORK_ITEM_ID = "task-1"
WORK_ITEM_TITLE = "Add refunds"
DECISION_REQUESTED = "Approve the work."
AUTHORIZED = ["Edit code."]
UNAUTHORIZED = ["Merge."]
CONDITIONS = ["Stay in scope."]
PLAN_FINGERPRINT = "a" * 64
DECISIONS = ["Pick a storage format.", "Pick a cache size."]

GOLDEN_APPROVAL_DIGEST = "2c954c4a0398049fcd36ff295dd6cad775aad72110998922992f37218d5a4bd8"
GOLDEN_PLAN_DIGEST = "20fb8a54c3d686c2b5649bbb16aadb2408424d10fa27b6619491a04edb21883c"

RATIONALE = RiskRationale(
    intended_outcome="Ship the change.",
    sensitive_boundary="Payments code.",
    necessity="Customers wait.",
    credible_scenario="A bad refund.",
    known_mitigations=["Review.", "Tests."],
    residual_risk="Low.",
)


def _approval_digest() -> str:
    return compute_approval_context_fingerprint(
        run_id=RUN_ID,
        episode_id=EPISODE_ID,
        work_item_id=WORK_ITEM_ID,
        work_item_title=WORK_ITEM_TITLE,
        risk=Risk.R2.value,
        complexity=Complexity.L2.value,
        rationale=RATIONALE,
        decision_requested=DECISION_REQUESTED,
        next_state=WorkflowState.REFINING.value,
        authorized_actions=AUTHORIZED,
        unauthorized_actions=UNAUTHORIZED,
        conditions_in_force=CONDITIONS,
    )


def _plan_digest() -> str:
    return compute_plan_decision_context_fingerprint(
        run_id=RUN_ID,
        episode_id=EPISODE_ID,
        plan_fingerprint=PLAN_FINGERPRINT,
        decisions=DECISIONS,
    )


def test_the_approval_context_digest_is_the_golden_value() -> None:
    assert _approval_digest() == GOLDEN_APPROVAL_DIGEST


def test_the_plan_decision_context_digest_is_the_golden_value() -> None:
    assert _plan_digest() == GOLDEN_PLAN_DIGEST


def test_a_stored_approval_context_with_the_golden_digest_is_valid() -> None:
    context = RiskApprovalContext(
        risk=Risk.R2,
        complexity=Complexity.L2,
        work_item_id=WORK_ITEM_ID,
        work_item_title=WORK_ITEM_TITLE,
        risk_rationale=RATIONALE,
        decision_requested=DECISION_REQUESTED,
        authorized_actions=AUTHORIZED,
        unauthorized_actions=UNAUTHORIZED,
        conditions_in_force=CONDITIONS,
        context_fingerprint=GOLDEN_APPROVAL_DIGEST,
    )

    assert is_valid_risk_approval_context(context, RUN_ID, EPISODE_ID) is True


def test_a_stored_plan_decision_context_with_the_golden_digest_is_valid() -> None:
    context = PlanDecisionContext(
        plan_fingerprint=PLAN_FINGERPRINT,
        decisions=DECISIONS,
        context_fingerprint=GOLDEN_PLAN_DIGEST,
    )

    assert is_valid_plan_decision_context(context, RUN_ID, EPISODE_ID) is True
