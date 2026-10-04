"""Typed transitions of ``EscalationRecord``.

Each transition returns a new record and leaves the first one as it was. The fixture sets
every field to a non-default value. Each test lists the changed fields as literals and expects
all other fields to equal the fixture, so a field that a transition drops or resets shows here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from software_agent_factory.models import (
    REPLY_CURSOR_CLOSED,
    AcceptedReplyReceipt,
    Complexity,
    DeliveryRetryContext,
    EscalationRecord,
    EscalationStatus,
    EscalationTargetType,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)

BEFORE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
OPEN_CURSOR = '{"page": 1}'
NEXT_CURSOR = '{"page": 2}'
EPISODE_ID = "ep-transition"
RUN_ID = "run-transition"
FINGERPRINT = "b" * 64


def _receipt(comment_id: int) -> AcceptedReplyReceipt:
    return AcceptedReplyReceipt(
        comment_id=comment_id,
        user_login="lead-dev",
        created_at=BEFORE,
        accepted_at=BEFORE,
        command="resume",
        episode_id=EPISODE_ID,
        run_id=RUN_ID,
    )


def _notified() -> EscalationRecord:
    return EscalationRecord(
        episode_id=EPISODE_ID,
        episode_number=3,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_type=EscalationTargetType.PULL_REQUEST,
        target_host="github.example.com",
        target_repository="owner/repo",
        target_number=42,
        target_url="https://github.example.com/owner/repo/pull/42",
        comment_id=7,
        comment_url="https://github.example.com/owner/repo/pull/42#issuecomment-7",
        reason_code="RISK_APPROVAL",
        delivery_attempts=2,
        delivery_error="first attempt timed out",
        last_notified_at=BEFORE,
        reply_cursor=OPEN_CURSOR,
        accepted_replies=[_receipt(1)],
        reopen_count=2,
        approval_context=RiskApprovalContext(
            risk=Risk.R2,
            complexity=Complexity.L2,
            work_item_id="task-1",
            work_item_title="Add refunds",
            risk_rationale=RiskRationale(
                intended_outcome="Ship the change.",
                sensitive_boundary="Payments code.",
                necessity="Customers wait.",
                credible_scenario="A bad refund.",
                known_mitigations=["Review."],
                residual_risk="Low.",
            ),
            decision_requested="Approve the work.",
            next_state=WorkflowState.REFINING,
            authorized_actions=["Edit code."],
            unauthorized_actions=["Merge."],
            conditions_in_force=["Stay in scope."],
            context_fingerprint=FINGERPRINT,
        ),
        plan_decision_context=PlanDecisionContext(
            plan_fingerprint=FINGERPRINT,
            decisions=["Pick a storage format."],
            context_fingerprint=FINGERPRINT,
        ),
        delivery_retry_context=DeliveryRetryContext(
            reviewed_tree_sha="a" * 40,
            base_commit_sha="b" * 40,
            branch_name="factory/task-1",
            context_fingerprint=FINGERPRINT,
        ),
        remote_resume_enabled=True,
        created_at=BEFORE,
        updated_at=BEFORE,
    )


def test_the_fixture_sets_every_field_to_a_non_default_value() -> None:
    record = _notified()

    still_default = {
        name
        for name, field in EscalationRecord.model_fields.items()
        if getattr(record, name) == field.get_default(call_default_factory=True)
    }

    assert still_default == set()


def test_advanced_cursor_moves_only_the_cursor_and_the_stamp() -> None:
    record = _notified()

    advanced = record.advanced_cursor(NEXT_CURSOR, NOW)

    assert advanced.model_dump() == {
        **record.model_dump(),
        "reply_cursor": NEXT_CURSOR,
        "updated_at": NOW,
    }


def test_advanced_cursor_accepts_the_closed_cursor() -> None:
    record = _notified()

    advanced = record.advanced_cursor(REPLY_CURSOR_CLOSED, NOW)

    assert advanced.model_dump() == {
        **record.model_dump(),
        "reply_cursor": REPLY_CURSOR_CLOSED,
        "updated_at": NOW,
    }


def test_closed_to_replies_closes_the_cursor_and_turns_remote_resume_off() -> None:
    record = _notified()

    closed = record.closed_to_replies(NOW)

    assert closed.model_dump() == {
        **record.model_dump(),
        "remote_resume_enabled": False,
        "reply_cursor": REPLY_CURSOR_CLOSED,
        "updated_at": NOW,
    }


def test_expired_is_closed_to_replies_with_the_expired_status() -> None:
    record = _notified()

    expired = record.expired(NOW)

    assert expired.model_dump() == {
        **record.model_dump(),
        "status": EscalationStatus.EXPIRED,
        "remote_resume_enabled": False,
        "reply_cursor": REPLY_CURSOR_CLOSED,
        "updated_at": NOW,
    }


def test_reopened_adds_the_receipt_counts_the_reopen_and_closes_the_cursor() -> None:
    record = _notified()
    receipt = _receipt(2)

    reopened = record.reopened(receipt, NOW)

    assert reopened.model_dump() == {
        **record.model_dump(),
        "accepted_replies": [_receipt(1).model_dump(), receipt.model_dump()],
        "reopen_count": 3,
        "status": EscalationStatus.REOPENED,
        "reply_cursor": REPLY_CURSOR_CLOSED,
        "updated_at": NOW,
    }


def test_a_transition_leaves_the_record_it_was_called_on_unchanged() -> None:
    record = _notified()
    snapshot = record.model_copy(deep=True)

    record.reopened(_receipt(2), NOW)
    record.expired(NOW)
    record.closed_to_replies(NOW)
    record.advanced_cursor(NEXT_CURSOR, NOW)

    assert record == snapshot
