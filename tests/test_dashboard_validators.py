"""The shape checks the dashboard modules share (slice 2 of #80)."""

from __future__ import annotations

from typing import Any

import pytest

from software_agent_factory.dashboard.validators import (
    RESUME_CLASSIFICATIONS,
    is_context_fingerprint,
    is_count,
    is_episode_id,
    is_number,
    is_positive_int,
    is_safe_https_url,
    run_id_of,
)
from software_agent_factory.escalation import parse_plan_decision_answers, parse_resume_command
from software_agent_factory.models import ResumeClassification


@pytest.mark.parametrize(
    ("value", "number", "count", "positive"),
    [
        (0, True, True, False),
        (3, True, True, True),
        (-1, True, False, False),
        (1.5, True, False, False),
        (True, False, False, False),
        (False, False, False, False),
        ("1", False, False, False),
        (None, False, False, False),
    ],
    ids=["zero", "positive", "negative", "float", "true", "false", "string", "none"],
)
def test_number_checks_never_accept_a_bool(
    value: Any, number: bool, count: bool, positive: bool
) -> None:
    assert is_number(value) is number
    assert is_count(value) is count
    assert is_positive_int(value) is positive


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://github.com/o/r/pull/1", True),
        ("http://github.com/o/r", False),
        ("https://user:pw@github.com/o/r", False),
        ("https://github.com:notaport/o/r", False),
        (" https://github.com/o/r", False),
        ("https://github.com/" + "a" * 2048, False),
        (None, False),
    ],
    ids=["https", "http", "credentials", "bad-port", "padded", "too-long", "none"],
)
def test_only_a_plain_https_url_is_safe(url: Any, safe: bool) -> None:
    assert is_safe_https_url(url) is safe


@pytest.mark.parametrize("episode_id", ["ep-0123", "ep.v1_2", "a" * 128])
def test_an_episode_id_is_safe_exactly_when_the_reply_parsers_accept_it(episode_id: str) -> None:
    assert is_episode_id(episode_id)
    assert parse_resume_command(f"@factory resume v1 run=run-1 episode={episode_id}") is not None
    answer = f"@factory answer v1 run=run-1 episode={episode_id}\n1. yes"
    assert parse_plan_decision_answers(answer, decision_count=1) is not None


@pytest.mark.parametrize("episode_id", ["", "ep one", "ep/1", "a" * 129, "ep\n", None, 7])
def test_an_episode_id_the_reply_parsers_would_not_read_is_unsafe(episode_id: Any) -> None:
    assert not is_episode_id(episode_id)


@pytest.mark.parametrize(
    ("fingerprint", "valid"),
    [
        ("f" * 64, True),
        ("F" * 63, False),
        ("f" * 65, False),
        ("f" * 63 + "-", False),
        (None, False),
    ],
)
def test_a_context_fingerprint_is_64_letters_or_digits(fingerprint: Any, valid: bool) -> None:
    assert is_context_fingerprint(fingerprint) is valid


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"run_id": "run-001"}, "run-001"),
        ({"id": "run-002"}, "run-002"),
        ({"run_id": "run-001", "id": "other"}, "run-001"),
        ({"run_id": "run.001"}, None),
        ({"run_id": "bad id"}, None),
        ({"run_id": "run-001\n"}, None),
        ({"run_id": 5}, None),
        ({}, None),
    ],
    ids=["run_id", "id", "run_id-first", "dot", "space", "newline", "not-a-string", "missing"],
)
def test_the_run_id_of_a_record_is_the_server_run_id_shape(
    record: dict[str, Any], expected: str | None
) -> None:
    assert run_id_of(record) == expected


def test_resume_classifications_are_the_enum_values() -> None:
    assert RESUME_CLASSIFICATIONS == {item.value for item in ResumeClassification}
