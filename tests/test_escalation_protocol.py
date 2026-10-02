"""The escalation reply protocol leaf: grammar shared by the controller and the dashboard."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from factory_testing import REPLY_POLICY

from software_agent_factory.config import EscalationConfig
from software_agent_factory.escalation import (
    parse_plan_decision_answers,
    parse_resume_command,
)
from software_agent_factory.escalation_protocol import (
    MAX_PLAN_DECISIONS,
    REPLY_CLOSED_CAUSES,
    ReplyClosedCause,
    ReplyPolicy,
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


def _cause(record: EscalationRecord, *, now: datetime = NOW, **policy_changes: Any) -> str | None:
    return reply_closed_cause(record, replace(REPLY_POLICY, **policy_changes), now)


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


@pytest.mark.parametrize(
    ("count", "cause"),
    [(2, None), (3, "the reopen limit is reached"), (4, "the reopen limit is reached")],
)
def test_the_reopen_limit_closes_the_reply_at_the_limit(count: int, cause: str | None) -> None:
    assert _cause(_record(reopen_count=count)) == cause


def test_a_notice_host_that_is_not_allowed_closes_the_reply() -> None:
    record = _record(target_host="ghe.example.com")

    assert _cause(record) == "the notice host is no longer allowed"
    assert _cause(record, allowed_hosts=("github.com", "ghe.example.com")) is None


def test_the_notice_host_is_compared_without_regard_to_case() -> None:
    assert _cause(_record(target_host="GitHub.com")) is None
    assert _cause(_record(target_host="github.com"), allowed_hosts=("GitHub.com",)) is None


def test_a_notice_without_a_stored_host_uses_the_first_allowed_host() -> None:
    assert _cause(_record(), allowed_hosts=("ghe.example.com", "github.com")) is None
    assert _cause(_record(), allowed_hosts=()) == "the notice host is no longer allowed"


@pytest.mark.parametrize(
    "record",
    [
        _record(status=EscalationStatus.NOTIFICATION_FAILED),
        _record(status=EscalationStatus.EXPIRED),
        _record(remote_resume_enabled=False),
        _record(reply_cursor="closed"),
        _record(created_at=NOW - timedelta(days=30)),
        _record(reopen_count=3),
        _record(target_host="ghe.example.com"),
    ],
)
def test_every_closed_cause_is_a_known_phrase(record: EscalationRecord) -> None:
    cause = _cause(record)

    assert cause in REPLY_CLOSED_CAUSES


def test_the_disabled_cause_is_a_known_phrase() -> None:
    assert _cause(_record(), escalation_enabled=False) in REPLY_CLOSED_CAUSES


def test_a_disabled_escalation_closes_the_reply() -> None:
    assert _cause(_record(), escalation_enabled=False) == "escalation replies are turned off"


def test_a_reply_policy_is_built_from_the_escalation_config() -> None:
    config = EscalationConfig(
        enabled=True,
        authorized_identities=["lead-dev"],
        max_reopens=2,
        reply_window_hours=5,
        allowed_hosts=["ghe.example.com"],
    )

    policy = ReplyPolicy.from_config(config)

    assert policy == ReplyPolicy(
        max_reopens=2,
        reply_window_hours=5,
        escalation_enabled=True,
        allowed_hosts=("ghe.example.com",),
    )


def test_a_reply_policy_cannot_change_after_it_is_built() -> None:
    with pytest.raises(FrozenInstanceError):
        REPLY_POLICY.max_reopens = 1  # type: ignore[misc]


def test_a_reply_policy_has_no_default_that_reads_as_open() -> None:
    with pytest.raises(TypeError):
        ReplyPolicy()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "status", [s for s in EscalationStatus if s is not EscalationStatus.NOTIFIED]
)
def test_every_status_other_than_notified_has_a_cause_of_its_own(status: EscalationStatus) -> None:
    assert _cause(_record(status=status)) != ReplyClosedCause.STATUS_UNKNOWN
