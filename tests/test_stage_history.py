"""A run's state moves: kept on every path, bounded, sanitized and shown as stages (ADR-067)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from dashboard_js import JsCall, run_functions
from factory_testing import build_config, work_item

from software_agent_factory import run_recovery
from software_agent_factory.agents import FakeAgentRuntime
from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.sanitize import sanitize_run_detail
from software_agent_factory.dashboard.transitions import sanitize_transitions
from software_agent_factory.models import (
    MAX_STATE_TRANSITIONS,
    MAX_TRANSITION_REASON_CHARS,
    FactoryRun,
    StateTransition,
    WorkflowState,
    utc_now,
)
from software_agent_factory.stage_history import leave_state
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

_JS = dashboard_assets.APP_JS
_SECRET = "ghp_abcdefgh1234567890abcdefgh1234567890"


def _controller(tmp_path: Path) -> WorkflowController:
    config = build_config(tmp_path / "data")
    return WorkflowController(config, FileRunStore(config.data_dir), FakeAgentRuntime())


def _assert_chained(run: FactoryRun) -> None:
    moves = run.transitions
    assert moves[0].from_state is WorkflowState.CREATED
    for before, after in zip(moves, moves[1:], strict=False):
        assert after.from_state is before.to_state
        assert after.at >= before.at
    assert moves[-1].to_state is run.state


def test_a_run_keeps_every_state_move_in_order(tmp_path: Path, factory_source_repo: Path) -> None:
    run = _controller(tmp_path).run(work_item(), factory_source_repo)

    _assert_chained(run)
    states = [move.to_state for move in run.transitions]
    assert states[:2] == [WorkflowState.TRIAGING, WorkflowState.PLANNING]
    assert WorkflowState.REVIEWING in states


def test_a_halt_keeps_its_reason_and_a_restart_is_not_timed(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    run = FactoryRun(id="run-1", work_item_id="WI-1", state=WorkflowState.REVIEWING)

    halted = controller.transition(run, WorkflowState.NEEDS_HUMAN, failure_reason="review impasse")
    restarted = run_recovery.restart_at(controller, run, WorkflowState.PLANNING)

    assert halted.transitions[-1].reason == "review impasse"
    assert halted.transitions[-1].from_state is WorkflowState.REVIEWING
    assert restarted.transitions[-1].reason == run_recovery.RESTART_REASON
    assert restarted.transitions[-1].to_state is WorkflowState.PLANNING


def test_the_history_keeps_only_the_newest_moves_and_cuts_long_reasons() -> None:
    now = utc_now()
    old = [
        StateTransition(
            from_state=WorkflowState.VERIFYING,
            to_state=WorkflowState.IMPLEMENTING,
            at=now - timedelta(seconds=index),
        )
        for index in range(MAX_STATE_TRANSITIONS)
    ]
    run = FactoryRun(
        id="run-1", work_item_id="WI-1", state=WorkflowState.IMPLEMENTING, transitions=old
    )

    moves = leave_state(run, WorkflowState.FAILED, now, "x" * 2000)

    assert len(moves) == MAX_STATE_TRANSITIONS
    assert moves[0] == old[1]
    assert moves[-1].to_state is WorkflowState.FAILED
    assert moves[-1].reason == "x" * MAX_TRANSITION_REASON_CHARS


def test_a_run_recorded_before_the_history_loads_with_none() -> None:
    run = FactoryRun.model_validate(
        {"id": "run-1", "work_item_id": "WI-1", "state": "DONE", "schema_version": 1}
    )

    assert run.transitions == []


def test_the_page_gets_valid_moves_with_redacted_reasons() -> None:
    at = "2026-10-19T10:00:00+00:00"
    raw: list[Any] = [
        {"from_state": "REVIEWING", "to_state": "NEEDS_HUMAN", "at": at, "reason": _SECRET},
        {"from_state": "BOGUS", "to_state": "DONE", "at": at},
        {"from_state": "PLANNING", "to_state": "DONE", "at": "yesterday"},
        "not a move",
        {"from_state": "PLANNING", "to_state": "IMPLEMENTING", "at": at, "reason": "y" * 900},
    ]

    moves = sanitize_transitions(raw, "run-1")
    detail = sanitize_run_detail({"run_id": "run-1", "state": "DONE", "transitions": raw})

    assert [move["to_state"] for move in moves] == ["NEEDS_HUMAN", "IMPLEMENTING"]
    assert _SECRET not in str(moves)
    assert len(moves[1]["reason"]) <= 500
    assert detail["transitions"] == moves
    assert sanitize_transitions(None, None) == []


def _move(from_state: str, to_state: str, minute: int) -> dict[str, str]:
    return {"from_state": from_state, "to_state": to_state, "at": f"2026-10-19T10:{minute:02d}:00Z"}


def test_the_stepper_counts_entries_and_time_per_stage() -> None:
    moves = [
        _move("CREATED", "TRIAGING", 0),
        _move("TRIAGING", "PLANNING", 1),
        _move("PLANNING", "IMPLEMENTING", 3),
        _move("IMPLEMENTING", "VERIFYING", 6),
        _move("VERIFYING", "REVIEWING", 7),
        _move("REVIEWING", "IMPLEMENTING", 8),
        _move("IMPLEMENTING", "VERIFYING", 10),
        _move("VERIFYING", "REVIEWING", 11),
    ]
    detail = {
        "created_at": "2026-10-19T10:00:00Z",
        "state": "REVIEWING",
        "state_started_at": "2026-10-19T10:11:00Z",
        "is_finished": False,
        "transitions": moves,
    }
    halted = {**detail, "state": "NEEDS_HUMAN", "is_finished": True}
    halted["transitions"] = [*moves, _move("REVIEWING", "NEEDS_HUMAN", 12)]
    now = 1_792_404_780_000  # 2026-10-19T10:13:00Z

    active, stopped, old = (
        result.value
        for result in run_functions(
            _JS,
            [
                JsCall("stageSteps", (detail, now)),
                JsCall("stageSteps", (halted, now)),
                JsCall("stageSteps", ({"state": "DONE"}, now)),
            ],
        )
    )

    assert [step["text"] for step in active] == [
        "Triage",
        "Plan",
        "Implement ×2",
        "Verify ×2",
        "Review ×2",
        "Deliver",
    ]
    assert [step["status"] for step in active] == [
        "done",
        "done",
        "done",
        "done",
        "current",
        "todo",
    ]
    assert active[2]["time"] == "5 min 0 s"
    assert active[4]["time"] == "3 min 0 s"
    assert active[5]["time"] == ""
    assert [step["status"] for step in stopped][4] == "halted"
    assert stopped[4]["time"] == "2 min 0 s"
    assert old == []


def test_the_stepper_skips_time_before_the_oldest_kept_move() -> None:
    trimmed = {
        "created_at": "2026-10-19T09:00:00Z",
        "state": "DONE",
        "is_finished": True,
        "transitions": [
            _move("IMPLEMENTING", "VERIFYING", 30),
            _move("VERIFYING", "PR_READY", 31),
            _move("PR_READY", "DONE", 32),
        ],
    }

    (result,) = run_functions(_JS, [JsCall("stageSteps", (trimmed, 0))])

    assert result.value[2]["time"] == ""
    assert result.value[3]["time"] == "1 min 0 s"
    assert result.value[5]["time"] == "1 min 0 s"
