"""Steer a run with a note: the note file, the prompt section and ``factory steer`` (X5)."""

from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from factory_testing import REPLY_POLICY, build_config
from test_service import _item
from typer.testing import CliRunner

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.cli import app, request_run_steer
from software_agent_factory.dashboard.actions import ResumeActions, accept_steer
from software_agent_factory.dashboard.responses import WriteRejected
from software_agent_factory.models import (
    AgentRole,
    EscalationRecord,
    FactoryRun,
    ResumeClassification,
    RunSteerNote,
    WorkflowState,
)
from software_agent_factory.prompts import build_prompt
from software_agent_factory.redaction import REDACTION_PLACEHOLDER
from software_agent_factory.resume import clean_steer_note
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
NOTE = "Keep the public API as it is."
runner = CliRunner()


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _saved(store: FileRunStore, run_id: str, state: WorkflowState) -> FactoryRun:
    run = FactoryRun(id=run_id, work_item_id="w", state=state)
    store.save_run(run)
    return run


class SteerBeforeImplementer:
    """Sends a note during the Planner call and records the note each call received."""

    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self.notes: list[tuple[AgentRole, str | None]] = []
        self.prompts: list[str] = []
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        self.notes.append((request.role, request.operator_note))
        self.prompts.append(build_prompt(request))
        if request.role is AgentRole.PLANNER:
            run_id = self.store.list_runs()[0].id
            self.store.create_steer_note(RunSteerNote(run_id=run_id, source="cli", note=NOTE))
        return self._delegate.run(request)


# -- store and rules -----------------------------------------------------------------------


def test_a_steer_note_is_created_once_and_deleted(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-a", WorkflowState.IMPLEMENTING)
    note = RunSteerNote(run_id="run-a", source="cli", note=NOTE, created_at=NOW)

    assert store.create_steer_note(note) is True
    assert store.create_steer_note(note) is False
    assert store.load_steer_note("run-a") == note

    store.delete_steer_note("run-a")
    store.delete_steer_note("run-a")
    assert store.load_steer_note("run-a") is None


def test_a_steer_note_for_a_missing_run_creates_nothing(data_dir: Path) -> None:
    store = FileRunStore(data_dir)

    note = RunSteerNote(run_id="run-gone", source="cli", note=NOTE)

    with pytest.raises(FileNotFoundError):
        store.create_steer_note(note)

    assert not (data_dir / "runs" / "run-gone").exists()


def test_a_damaged_steer_note_reads_as_none(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-bad", WorkflowState.IMPLEMENTING)
    (data_dir / "runs" / "run-bad" / "steer-note.json").write_text("{", encoding="utf-8")

    assert store.load_steer_note("run-bad") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  Fix src/app.py first.\nThen rerun.  ", "Fix src/app.py first.\nThen rerun."),
        ("", None),
        ("   ", None),
        ("x" * 2001, None),
        ("token ghp_" + "a" * 36, None),
        (f"use {REDACTION_PLACEHOLDER}", None),
    ],
)
def test_clean_steer_note(text: str, expected: str | None) -> None:
    assert clean_steer_note(text) == expected


# -- controller ----------------------------------------------------------------------------


def _work_item(source_repo: Path) -> Any:
    from software_agent_factory.service import build_work_item

    return build_work_item(_item(42, source_repo))


def test_the_next_agent_call_gets_the_note_once(source_repo: Path, data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    runtime = SteerBeforeImplementer(store)
    controller = WorkflowController(build_config(data_dir), store, runtime)

    controller.run(_work_item(source_repo), source_repo, run_id="run-steer")

    planner = next(i for i, (role, _) in enumerate(runtime.notes) if role is AgentRole.PLANNER)
    received = [note for _, note in runtime.notes]
    assert received[: planner + 1] == [None] * (planner + 1)
    assert received[planner + 1] == NOTE
    assert all(note is None for note in received[planner + 2 :])
    assert "Operator note:\n" in runtime.prompts[planner + 1]
    assert runtime.prompts[planner + 1].rstrip().endswith(NOTE)
    assert store.load_steer_note("run-steer") is None


class FailsOnce:
    """Raises on the first call and records the note each call received."""

    def __init__(self) -> None:
        self.notes: list[str | None] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.notes.append(request.operator_note)
        if len(self.notes) == 1:
            raise RuntimeError("runtime unavailable")
        return AgentResult(role=request.role, success=True)


def test_a_failed_agent_call_puts_the_note_back(source_repo: Path, data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    runtime = FailsOnce()
    controller = WorkflowController(build_config(data_dir), store, runtime)
    run = _saved(store, "run-retry", WorkflowState.IMPLEMENTING)
    store.create_steer_note(RunSteerNote(run_id="run-retry", source="cli", note=NOTE))
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="high",
        work_item=_work_item(source_repo),
        timeout_seconds=30,
    )

    with pytest.raises(RuntimeError):
        controller._call_with_steer_note(run, request)
    assert store.load_steer_note("run-retry") is not None
    controller._call_with_steer_note(run, request)

    assert runtime.notes == [NOTE, NOTE]
    assert store.load_steer_note("run-retry") is None


def test_a_lost_note_is_only_logged(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())

    controller._restore_steer_note(RunSteerNote(run_id="run-gone", source="cli", note=NOTE))

    assert "lost its steer note" in caplog.text


@pytest.mark.parametrize(
    ("state", "kept"),
    [(WorkflowState.NEEDS_HUMAN, True), (WorkflowState.DONE, False), (WorkflowState.FAILED, False)],
)
def test_a_halt_keeps_the_note_and_an_end_drops_it(
    data_dir: Path, state: WorkflowState, kept: bool
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    run = _saved(store, "run-end", WorkflowState.CI_RUNNING)
    store.create_steer_note(RunSteerNote(run_id="run-end", source="cli", note=NOTE))

    controller.transition(run, state, failure_reason="stopped for a test")

    assert (store.load_steer_note("run-end") is not None) is kept


# -- request_run_steer, the route and factory steer ----------------------------------------


def test_request_run_steer_reports_each_outcome(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-live", WorkflowState.IMPLEMENTING)
    waiting = _saved(store, "run-wait", WorkflowState.NEEDS_HUMAN)
    waiting.escalation = EscalationRecord(
        episode_id="e1", resume_classification=ResumeClassification.RISK_APPROVAL
    )
    store.save_run(waiting)
    stuck = _saved(store, "run-stuck", WorkflowState.NEEDS_HUMAN)
    stuck.escalation = EscalationRecord(episode_id="e1")
    store.save_run(stuck)
    _saved(store, "run-bare", WorkflowState.NEEDS_HUMAN)
    _saved(store, "run-done", WorkflowState.DONE)

    assert request_run_steer(store, "run-live", NOTE, "cli", NOW) == "created"
    assert request_run_steer(store, "run-live", NOTE, "cli", NOW) == "exists"
    assert request_run_steer(store, "run-wait", NOTE, "cli", NOW) == "created"
    assert request_run_steer(store, "run-stuck", NOTE, "cli", NOW) == "finished"
    assert request_run_steer(store, "run-bare", NOTE, "cli", NOW) == "finished"
    assert request_run_steer(store, "run-done", NOTE, "cli", NOW) == "finished"
    assert request_run_steer(store, "run-none", NOTE, "cli", NOW) == "run_missing"


def test_the_steer_route_is_not_found_without_a_steerer() -> None:
    actions = ResumeActions(
        run_reader=lambda run_id: None,
        requester=lambda run_id, request: "created",
        reply_policy=REPLY_POLICY,
    )

    with pytest.raises(WriteRejected) as caught:
        accept_steer(actions, "run-x", {"note": NOTE})

    assert caught.value.status is HTTPStatus.NOT_FOUND


def test_factory_steer_saves_a_note(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-cli", WorkflowState.REVIEWING)

    result = runner.invoke(app, ["steer", "run-cli", NOTE, "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "note saved for run-cli" in result.output
    note = store.load_steer_note("run-cli")
    assert note is not None
    assert (note.source, note.note) == ("cli", NOTE)


@pytest.mark.parametrize(
    ("run_id", "note", "message"),
    [
        ("run-old", NOTE, "has finished or cannot resume"),
        ("run-cli", "   ", "the note is empty"),
        ("run-none", NOTE, "no such run"),
    ],
)
def test_factory_steer_fails_and_saves_nothing(
    data_dir: Path, run_id: str, note: str, message: str
) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-old", WorkflowState.DONE)
    _saved(store, "run-cli", WorkflowState.REVIEWING)

    result = runner.invoke(app, ["steer", run_id, note, "--data-dir", str(data_dir)])

    assert result.exit_code != 0
    assert message in result.output
    assert store.load_steer_note("run-cli") is None
