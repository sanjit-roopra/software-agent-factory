"""Stop a run on request: the stop file, the controller halt and ``factory cancel`` (X3)."""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from factory_testing import REPLY_POLICY, build_config
from test_service import _item
from typer.testing import CliRunner

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.cli import app, request_run_stop
from software_agent_factory.dashboard.actions import ResumeActions, accept_stop
from software_agent_factory.dashboard.responses import WriteRejected
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.models import (
    STOPPED_HALT_REASON,
    AgentRole,
    FactoryRun,
    HaltReasonCode,
    RunStopRequest,
    WorkflowState,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.subprocess_utils import release_thread, track_child
from software_agent_factory.workflow import WorkflowController

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _work_item(source_repo: Path) -> Any:
    from software_agent_factory.service import build_work_item

    return build_work_item(_item(41, source_repo))


def _saved(store: FileRunStore, run_id: str, state: WorkflowState) -> FactoryRun:
    run = FactoryRun(id=run_id, work_item_id="w", state=state)
    store.save_run(run)
    return run


class StopDuringImplementer:
    """Asks for a stop during the Implementer call, then fails or answers like the agent."""

    def __init__(self, store: FileRunStore, *, raise_after: bool) -> None:
        self.store = store
        self.raise_after = raise_after
        self.roles: list[AgentRole] = []
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        self.roles.append(request.role)
        if request.role is AgentRole.IMPLEMENTER:
            run_id = self.store.list_runs()[0].id
            self.store.create_stop_request(RunStopRequest(run_id=run_id, source="dashboard"))
            if self.raise_after:
                raise RuntimeError("agent was killed")
        return self._delegate.run(request)


# -- store ---------------------------------------------------------------------------------


def test_a_stop_request_is_created_once_and_deleted(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-a", WorkflowState.IMPLEMENTING)
    request = RunStopRequest(run_id="run-a", source="cli", created_at=NOW)

    assert store.create_stop_request(request) is True
    assert store.create_stop_request(request) is False
    assert store.load_stop_request("run-a") == request

    store.delete_stop_request("run-a")
    store.delete_stop_request("run-a")
    assert store.load_stop_request("run-a") is None


def test_a_stop_request_for_a_missing_run_creates_nothing(data_dir: Path) -> None:
    store = FileRunStore(data_dir)

    with pytest.raises(FileNotFoundError):
        store.create_stop_request(RunStopRequest(run_id="run-gone", source="cli"))

    assert not (data_dir / "runs" / "run-gone").exists()


def test_a_damaged_stop_request_reads_as_none(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-bad", WorkflowState.IMPLEMENTING)
    (data_dir / "runs" / "run-bad" / "stop-request.json").write_text("{", encoding="utf-8")

    assert store.load_stop_request("run-bad") is None


# -- controller ----------------------------------------------------------------------------


@pytest.mark.parametrize("raise_after", [True, False])
def test_a_stop_request_halts_the_run_for_a_person_and_keeps_its_workspace(
    source_repo: Path, data_dir: Path, raise_after: bool
) -> None:
    store = FileRunStore(data_dir)
    runtime = StopDuringImplementer(store, raise_after=raise_after)
    controller = WorkflowController(build_config(data_dir), store, runtime)

    run = controller.run(_work_item(source_repo), source_repo, run_id="run-stop")

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason is not None
    assert run.failure_reason.startswith(f"{STOPPED_HALT_REASON} from the dashboard at ")
    assert run.active_invocation is None
    assert run.invocation_records[-1].role is AgentRole.IMPLEMENTER
    assert run.invocation_records[-1].success is False
    assert runtime.roles[-1] is AgentRole.IMPLEMENTER
    assert store.load_stop_request("run-stop") is None
    assert classify_halt_reason(run)[1] is HaltReasonCode.MANUAL_INSPECTION
    assert run.workspace_path is not None
    assert Path(run.workspace_path).is_dir()


def test_a_finished_run_drops_a_stop_request_it_never_reached(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    run = _saved(store, "run-late", WorkflowState.CI_RUNNING)
    store.create_stop_request(RunStopRequest(run_id="run-late", source="cli"))

    controller.transition(run, WorkflowState.DONE)

    assert store.load_stop_request("run-late") is None


def test_the_heartbeat_kills_the_agent_when_a_stop_request_appears(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    controller.heartbeat_interval_seconds = 0.01
    run = _saved(store, "run-kill", WorkflowState.IMPLEMENTING)
    process = track_child(
        subprocess.Popen(["sleep", "30"], text=True, start_new_session=True)  # noqa: S607
    )
    store.create_stop_request(RunStopRequest(run_id="run-kill", source="dashboard"))
    stop = threading.Event()
    threading.Thread(target=lambda: (process.wait(timeout=10), stop.set()), daemon=True).start()

    try:
        controller._heartbeat(run, stop, 60, threading.get_ident())
    finally:
        release_thread(threading.get_ident())

    assert process.wait(timeout=10) != 0


# -- request_run_stop and factory cancel ---------------------------------------------------


def test_request_run_stop_reports_each_outcome(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-live", WorkflowState.IMPLEMENTING)
    _saved(store, "run-done", WorkflowState.DONE)

    assert request_run_stop(store, "run-live", "cli", NOW) == "created"
    assert request_run_stop(store, "run-live", "cli", NOW) == "exists"
    assert request_run_stop(store, "run-done", "cli", NOW) == "finished"
    assert request_run_stop(store, "run-none", "cli", NOW) == "run_missing"
    assert request_run_stop(store, "../bad", "cli", NOW) == "run_missing"


def test_factory_cancel_queues_a_stop(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-cli", WorkflowState.REVIEWING)

    result = runner.invoke(app, ["cancel", "run-cli", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "stop requested for run-cli" in result.output
    request = store.load_stop_request("run-cli")
    assert request is not None
    assert request.source == "cli"


def test_factory_cancel_fails_for_a_finished_run(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    _saved(store, "run-old", WorkflowState.NEEDS_HUMAN)

    result = runner.invoke(app, ["cancel", "run-old", "--data-dir", str(data_dir)])

    assert result.exit_code != 0
    assert "has already finished" in result.output
    assert store.load_stop_request("run-old") is None


def test_the_stop_route_is_not_found_without_a_stopper() -> None:
    actions = ResumeActions(
        run_reader=lambda run_id: None,
        requester=lambda run_id, request: "created",
        reply_policy=REPLY_POLICY,
    )

    with pytest.raises(WriteRejected) as caught:
        accept_stop(actions, "run-x", {})

    assert caught.value.status is HTTPStatus.NOT_FOUND
