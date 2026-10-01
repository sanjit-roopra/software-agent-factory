"""The escalation reply protocol leaf: grammar shared by the controller and the dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from software_agent_factory.escalation import (
    parse_plan_decision_answers,
    parse_resume_command,
)
from software_agent_factory.escalation_protocol import (
    MAX_PLAN_DECISIONS,
    format_answer_command,
    format_resume_command,
    reply_closed_cause,
)
from software_agent_factory.models import EscalationRecord, EscalationStatus

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
WINDOW_HOURS = 24


def test_the_resume_command_is_the_exact_text_the_parser_reads() -> None:
    command = format_resume_command("run-1", "ep-0123")

    assert command == "@factory resume v1 run=run-1 episode=ep-0123"
    assert parse_resume_command(command) == ("run-1", "ep-0123")


def test_the_answer_command_is_the_exact_text_the_parser_reads() -> None:
    header = format_answer_command("run-1", "ep-0123")

    assert header == "@factory answer v1 run=run-1 episode=ep-0123"
    parsed = parse_plan_decision_answers(f"{header}\n1. yes", decision_count=1)
    assert parsed is not None
    assert parsed[:2] == ("run-1", "ep-0123")


def test_the_parser_accepts_exactly_the_most_decisions_the_protocol_names() -> None:
    header = format_answer_command("run-1", "ep-1")

    def body(count: int) -> str:
        return "\n".join([header, *(f"{n}. yes" for n in range(1, count + 1))])

    assert parse_plan_decision_answers(body(MAX_PLAN_DECISIONS), decision_count=MAX_PLAN_DECISIONS)
    assert (
        parse_plan_decision_answers(
            body(MAX_PLAN_DECISIONS + 1), decision_count=MAX_PLAN_DECISIONS + 1
        )
        is None
    )


def _record(**fields: Any) -> EscalationRecord:
    fields.setdefault("status", EscalationStatus.NOTIFIED)
    fields.setdefault("remote_resume_enabled", True)
    fields.setdefault("created_at", NOW - timedelta(hours=1))
    return EscalationRecord(episode_id="ep-1", **fields)


def _cause(record: EscalationRecord, **config: Any) -> str | None:
    config.setdefault("max_reopens", 3)
    config.setdefault("reply_window_hours", WINDOW_HOURS)
    config.setdefault("enabled", True)
    config.setdefault("now", NOW)
    return reply_closed_cause(record, **config)


def test_a_notified_notice_with_instructions_inside_its_window_is_open() -> None:
    assert _cause(_record()) is None


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        (EscalationStatus.PENDING_NOTIFICATION, "the notice is not sent yet"),
        (EscalationStatus.NOTIFICATION_FAILED, "the notice was not sent"),
        (EscalationStatus.EXPIRED, "the reply window expired"),
        (EscalationStatus.REOPENED, "the run already resumed from a reply"),
        (EscalationStatus.RESUMED, "the run already resumed from a reply"),
    ],
)
def test_a_status_other_than_notified_closes_the_reply(
    status: EscalationStatus, cause: str
) -> None:
    assert _cause(_record(status=status)) == cause


def test_a_notified_notice_without_reply_instructions_is_closed() -> None:
    # The notice fallback writes NOTIFIED with remote_resume_enabled=False.
    record = _record(remote_resume_enabled=False, reply_cursor="closed")

    assert _cause(record) == "the notice has no reply instructions"


def test_a_closed_reply_cursor_closes_the_reply() -> None:
    assert _cause(_record(reply_cursor="closed")) == "the factory stopped reading replies"


def test_an_open_reply_cursor_does_not_close_the_reply() -> None:
    assert _cause(_record(reply_cursor='{"page": 2}')) is None


def test_the_reply_window_closes_after_its_last_moment_not_at_it() -> None:
    record = _record(created_at=NOW - timedelta(hours=WINDOW_HOURS))

    assert _cause(record) is None
    assert _cause(record, now=NOW + timedelta(seconds=1)) == "the reply window expired"


@pytest.mark.parametrize(("count", "closed"), [(2, False), (3, True), (4, True)])
def test_the_reopen_limit_closes_the_reply_at_the_limit(count: int, closed: bool) -> None:
    cause = _cause(_record(reopen_count=count))

    assert cause == ("the reopen limit is reached" if closed else None)


def test_a_disabled_escalation_closes_the_reply() -> None:
    assert _cause(_record(), enabled=False) == "escalation replies are turned off"


def test_unknown_config_values_do_not_close_the_reply() -> None:
    record = _record(reopen_count=9, created_at=NOW - timedelta(days=30))
    unknown: dict[str, Any] = {"max_reopens": None, "reply_window_hours": None, "enabled": None}

    assert _cause(record, **unknown) is None
    assert _cause(record, **{**unknown, "max_reopens": 3}) == "the reopen limit is reached"
    assert _cause(record, **{**unknown, "reply_window_hours": 24}) == "the reply window expired"
