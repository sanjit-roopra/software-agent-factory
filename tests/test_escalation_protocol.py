"""The escalation reply protocol leaf: grammar shared by the controller and the dashboard."""

from __future__ import annotations

from software_agent_factory.escalation import (
    parse_plan_decision_answers,
    parse_resume_command,
)
from software_agent_factory.escalation_protocol import (
    MAX_PLAN_DECISIONS,
    format_answer_command,
    format_resume_command,
)


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
