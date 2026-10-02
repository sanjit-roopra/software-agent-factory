"""Typed transitions of ``EscalationRecord``.

Each transition returns a new record and leaves the first one as it was. The expected values
are the plain field changes the call sites made before the transitions existed, so a drift in
a field value shows here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from software_agent_factory.models import (
    REPLY_CURSOR_CLOSED,
    AcceptedReplyReceipt,
    EscalationRecord,
    EscalationStatus,
)

BEFORE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
OPEN_CURSOR = '{"page": 1}'
NEXT_CURSOR = '{"page": 2}'
EPISODE_ID = "ep-transition"
RUN_ID = "run-transition"


def _receipt(comment_id: int) -> AcceptedReplyReceipt:
    return AcceptedReplyReceipt(
        comment_id=comment_id,
        user_login="lead-dev",
        created_at=BEFORE,
        command="resume",
        episode_id=EPISODE_ID,
        run_id=RUN_ID,
    )


def _notified() -> EscalationRecord:
    return EscalationRecord(
        episode_id=EPISODE_ID,
        status=EscalationStatus.NOTIFIED,
        remote_resume_enabled=True,
        reply_cursor=OPEN_CURSOR,
        accepted_replies=[_receipt(1)],
        reopen_count=1,
        created_at=BEFORE,
        updated_at=BEFORE,
    )


def test_advanced_cursor_moves_only_the_cursor_and_the_stamp() -> None:
    record = _notified()

    advanced = record.advanced_cursor(NEXT_CURSOR, NOW)

    assert advanced == record.model_copy(update={"reply_cursor": NEXT_CURSOR, "updated_at": NOW})


def test_advanced_cursor_accepts_the_closed_cursor() -> None:
    advanced = _notified().advanced_cursor(REPLY_CURSOR_CLOSED, NOW)

    assert advanced.reply_cursor == REPLY_CURSOR_CLOSED
    assert advanced.remote_resume_enabled is True


def test_closed_to_replies_closes_the_cursor_and_turns_remote_resume_off() -> None:
    record = _notified()

    closed = record.closed_to_replies(NOW)

    assert closed == record.model_copy(
        update={
            "remote_resume_enabled": False,
            "reply_cursor": REPLY_CURSOR_CLOSED,
            "updated_at": NOW,
        }
    )
    assert closed.status is EscalationStatus.NOTIFIED


def test_expired_is_closed_to_replies_with_the_expired_status() -> None:
    record = _notified()

    expired = record.expired(NOW)

    assert expired == record.model_copy(
        update={
            "status": EscalationStatus.EXPIRED,
            "remote_resume_enabled": False,
            "reply_cursor": REPLY_CURSOR_CLOSED,
            "updated_at": NOW,
        }
    )


def test_reopened_adds_the_receipt_counts_the_reopen_and_closes_the_cursor() -> None:
    record = _notified()
    receipt = _receipt(2)

    reopened = record.reopened(receipt, NOW)

    assert reopened == record.model_copy(
        update={
            "accepted_replies": [*record.accepted_replies, receipt],
            "reopen_count": 2,
            "status": EscalationStatus.REOPENED,
            "reply_cursor": REPLY_CURSOR_CLOSED,
            "updated_at": NOW,
        }
    )


def test_reopened_keeps_remote_resume_as_it_was() -> None:
    assert _notified().reopened(_receipt(2), NOW).remote_resume_enabled is True


def test_a_transition_leaves_the_record_it_was_called_on_unchanged() -> None:
    record = _notified()
    snapshot = record.model_copy(deep=True)

    record.reopened(_receipt(2), NOW)
    record.expired(NOW)
    record.closed_to_replies(NOW)
    record.advanced_cursor(NEXT_CURSOR, NOW)

    assert record == snapshot
