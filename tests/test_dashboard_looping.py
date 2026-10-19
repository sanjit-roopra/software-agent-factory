"""The ``Possibly looping`` note of an active run (ADR-071)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from factory_testing import REPLY_POLICY

from software_agent_factory.dashboard.looping import (
    REASON_PREFIX,
    active_looping_note,
    looping_note,
)
from software_agent_factory.dashboard.view import run_detail_view, run_summary_view
from software_agent_factory.models import (
    AgentRole,
    AttemptRecord,
    AttemptTrigger,
    FactoryRun,
    WorkflowState,
)
from software_agent_factory.observability import build_monitoring_snapshot, build_run_detail
from software_agent_factory.store import FileRunStore

ACTIVE = {"kind": "active", "label": "Active"}
TIMEOUT = "IMPLEMENTER: copilot timed out after 900s."
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _attempt(trigger: str = "INITIAL", reason: str | None = None) -> dict[str, Any]:
    return {"triggered_by": trigger, "failure_reason": reason, "outcome": "failed"}


@pytest.mark.parametrize(
    ("attempts", "note"),
    [
        ([], None),
        ([_attempt("REVIEW"), _attempt("REVIEW")], None),
        (
            [_attempt("INITIAL"), _attempt("REVIEW"), _attempt("REVIEW"), _attempt("REVIEW")],
            "Review sent the work back 3 times in a row",
        ),
        ([_attempt("CI")] * 3, "CI failed 3 times in a row"),
        ([_attempt("VERIFICATION")] * 3, "The checks failed 3 times in a row"),
        ([_attempt("REVIEW"), _attempt("CI"), _attempt("REVIEW")], None),
        ([_attempt("INITIAL"), _attempt("POLISH"), _attempt("POLISH")], None),
        ([_attempt("OPERATOR")] * 3, None),
        (
            [_attempt("INITIAL", TIMEOUT), *[_attempt("IMPLEMENTER_FAILURE", TIMEOUT)] * 2],
            f"Same failure in the last 3 attempts: {TIMEOUT}",
        ),
        (
            [_attempt("CI", "x" * REASON_PREFIX + tail) for tail in ("a", "b", "c")],
            "Same failure in the last 3 attempts: " + "x" * REASON_PREFIX,
        ),
        ([_attempt("SCOPE", "one"), _attempt("REVIEW", "two"), _attempt("SCOPE", "one")], None),
        (
            [_attempt("SCOPE", "a"), _attempt("SCOPE", None), _attempt("SCOPE", "a")],
            "The scope check sent the work back 3 times in a row",
        ),
        (
            [_attempt("CI", "  "), "not an attempt", _attempt("CI"), _attempt("CI")],
            "CI failed 3 times in a row",
        ),
    ],
)
def test_looping_note_names_the_repeat(attempts: list[Any], note: str | None) -> None:
    assert looping_note(attempts) == note


def test_whitespace_differences_do_not_hide_the_same_failure() -> None:
    attempts = [
        _attempt("CI", "pytest  failed\n in test_a"),
        *[_attempt("CI", "pytest failed in test_a")] * 2,
    ]
    assert looping_note(attempts) == "Same failure in the last 3 attempts: pytest failed in test_a"


@pytest.mark.parametrize("kind", ["active", "stale"])
def test_active_and_stale_runs_get_the_note(kind: str) -> None:
    assert active_looping_note({"kind": kind}, [_attempt("CI")] * 3) == "CI failed 3 times in a row"


@pytest.mark.parametrize("kind", ["done", "failed", "needs_you", None])
def test_a_run_that_stopped_gets_no_note(kind: str | None) -> None:
    assert active_looping_note({"kind": kind}, [_attempt("CI")] * 3) is None


def _raw_summary(state: str, attempts: Any) -> dict[str, Any]:
    return {"run_id": "run-1", "state": state, "recent_attempts": attempts}


def test_run_row_carries_the_note_and_not_the_attempts() -> None:
    run = run_summary_view(_raw_summary("REVIEWING", [_attempt("REVIEW")] * 3))
    assert run["looping"] == "Review sent the work back 3 times in a row"
    assert "recent_attempts" not in run


@pytest.mark.parametrize(
    ("state", "attempts"),
    [("DONE", [_attempt("REVIEW")] * 3), ("REVIEWING", None), ("REVIEWING", "bad")],
)
def test_run_row_without_a_repeat_has_no_note(state: str, attempts: Any) -> None:
    assert run_summary_view(_raw_summary(state, attempts))["looping"] is None


def test_run_detail_reads_its_attempt_history() -> None:
    raw = {"run_id": "run-1", "state": "IMPLEMENTING", "attempts": [_attempt("CI")] * 3}
    assert run_detail_view(raw)["looping"] == "CI failed 3 times in a row"
    assert run_detail_view({**raw, "state": "FAILED"})["looping"] is None


def _record(number: int, trigger: AttemptTrigger) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=number,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="medium",
        started_at=T0,
        completed_at=T0 + timedelta(minutes=1),
        outcome="succeeded",
        triggered_by=trigger,
    )


def _store_run(tmp_path: Path, state: WorkflowState) -> FileRunStore:
    store = FileRunStore(tmp_path)
    triggers = [AttemptTrigger.INITIAL, *[AttemptTrigger.REVIEW] * 3]
    store.save_run(
        FactoryRun(
            id="run-1",
            work_item_id="WI-1",
            state=state,
            created_at=T0,
            updated_at=T0,
            completed_at=T0 if state is WorkflowState.DONE else None,
            attempt_records=[_record(n, t) for n, t in enumerate(triggers, start=1)],
        )
    )
    return store


def test_an_unfinished_run_summary_carries_its_last_three_attempts(tmp_path: Path) -> None:
    store = _store_run(tmp_path, WorkflowState.REVIEWING)
    (summary,) = build_monitoring_snapshot(store, now=T0).runs
    assert [attempt.attempt_number for attempt in summary.recent_attempts] == [2, 3, 4]
    view = run_summary_view(summary.model_dump(mode="json"))
    assert view["looping"] == "Review sent the work back 3 times in a row"


def test_a_finished_run_summary_carries_no_attempts(tmp_path: Path) -> None:
    store = _store_run(tmp_path, WorkflowState.DONE)
    (summary,) = build_monitoring_snapshot(store, now=T0).runs
    assert summary.recent_attempts == []


def test_run_detail_keeps_the_full_history_without_recent_attempts(tmp_path: Path) -> None:
    store = _store_run(tmp_path, WorkflowState.REVIEWING)
    detail = build_run_detail(store, "run-1", now=T0, reply_policy=REPLY_POLICY)
    assert detail is not None
    assert len(detail.attempts) == 4
    assert "recent_attempts" not in detail.model_dump()
