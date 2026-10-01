"""Tests for the pure resume rules shared by the GitHub poller and dashboard requests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from software_agent_factory import resume
from software_agent_factory.escalation import parse_plan_decision_answers
from software_agent_factory.resume import (
    MAX_PLAN_DECISION_ANSWER_CHARS,
    build_plan_answers,
    clean_plan_answer,
)

# -- purity ------------------------------------------------------------------


def test_resume_imports_no_github_subprocess_workflow_or_service() -> None:
    # The package __init__ imports subprocess, so a sys.modules check cannot tell.
    # Inspect the module's own imports instead.
    tree = ast.parse(Path(resume.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                imported.add(node.module or "")
            else:
                imported.add(f".{node.module or ''}")

    forbidden = {"subprocess", ".github", ".workflow", ".service", ".escalation", ".store"}
    assert imported.isdisjoint(forbidden)
    assert imported <= {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "re",
        "secrets",
        "typing",
        ".config",
        ".escalation_protocol",
        ".models",
    }


# -- answer rules ------------------------------------------------------------


def test_answer_at_the_character_limit_is_accepted_and_one_over_is_not() -> None:
    assert MAX_PLAN_DECISION_ANSWER_CHARS == 500
    assert clean_plan_answer("a" * 500) == "a" * 500
    assert clean_plan_answer("a" * 501) is None


def test_answer_is_trimmed_before_it_is_counted() -> None:
    assert clean_plan_answer("  " + "a" * 500 + "  ") == "a" * 500


@pytest.mark.parametrize("text", ["", "   ", "line one\nline two", "line one\rline two"])
def test_blank_and_multi_line_answers_are_rejected(text: str) -> None:
    assert clean_plan_answer(text) is None


@pytest.mark.parametrize(
    "text",
    ["see https://example.com/x", "edit /etc/passwd", "token ghp_" + "a" * 20],
)
def test_unsafe_answers_are_rejected(text: str) -> None:
    assert clean_plan_answer(text) is None


def test_build_numbers_one_answer_per_decision_in_order() -> None:
    answers = build_plan_answers(["Use JSON.", "Keep it local."], decision_count=2)

    assert answers is not None
    assert [(a.decision_number, a.answer) for a in answers] == [
        (1, "Use JSON."),
        (2, "Keep it local."),
    ]


@pytest.mark.parametrize("texts", [[], ["only one"], ["one", "two", "three"]])
def test_build_needs_exactly_one_answer_per_decision(texts: list[str]) -> None:
    assert build_plan_answers(texts, decision_count=2) is None


@pytest.mark.parametrize("count", [0, 25])
def test_build_rejects_a_decision_count_outside_the_protocol_range(count: int) -> None:
    assert build_plan_answers(["x"] * count, decision_count=count) is None


def test_build_rejects_the_whole_set_when_one_answer_breaks_a_rule() -> None:
    assert build_plan_answers(["fine", "a" * 501], decision_count=2) is None
    assert build_plan_answers(["fine", "two\nlines"], decision_count=2) is None


def _reply(*answers: str) -> str:
    lines = [f"{n}. {answer}" for n, answer in enumerate(answers, start=1)]
    return "\n".join(["@factory answer v1 run=r1 episode=ep-1", *lines])


def test_github_reply_follows_the_same_answer_rules() -> None:
    assert parse_plan_decision_answers(_reply("a" * 500), decision_count=1) is not None
    assert parse_plan_decision_answers(_reply("a" * 501), decision_count=1) is None


def test_github_reply_with_a_bare_carriage_return_is_ignored_not_a_crash() -> None:
    body = "@factory answer v1 run=r1 episode=ep-1\n1. first\rsecond"

    assert parse_plan_decision_answers(body, decision_count=1) is None
