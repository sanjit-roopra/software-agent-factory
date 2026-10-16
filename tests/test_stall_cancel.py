"""In-call heartbeat and real stall cancel (dashboard X1 and X2)."""

from __future__ import annotations

import subprocess
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from factory_testing import build_config
from test_service import LocalProvider, _config, _item

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.models import (
    STALLED_HALT_REASON,
    AgentRole,
    FactoryRun,
    HaltReasonCode,
    RunLease,
    WorkflowState,
    utc_now,
)
from software_agent_factory.scheduler import StallDecision
from software_agent_factory.service import FactoryService, ThreadPoolRunHandle
from software_agent_factory.store import FileRunStore
from software_agent_factory.subprocess_utils import (
    kill_thread_children,
    release_thread,
    track_child,
)
from software_agent_factory.workflow import RunCancelledError, WorkflowController


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


class HeartbeatWatchingRuntime:
    """Waits in the Implementer call until the run file shows a newer lease heartbeat."""

    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self.seen: list[datetime] = []
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER and not self.seen:
            run_id = self.store.list_runs()[0].id
            first = self._heartbeat(run_id)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                current = self._heartbeat(run_id)
                if current > first:
                    self.seen.append(current)
                    break
                time.sleep(0.01)
        return self._delegate.run(request)

    def _heartbeat(self, run_id: str) -> datetime:
        lease = self.store.load_run(run_id).lease
        assert lease is not None
        return lease.heartbeat_at


class StallingRuntime:
    """Fires the service's stall callback once, during the first Implementer call."""

    def __init__(self) -> None:
        self.service: FactoryService | None = None
        self.item_opaque_id = ""
        self.stalls = 0
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER and self.stalls == 0:
            self.stalls += 1
            assert self.service is not None
            item = next(
                entry
                for entry in self.service.provider.fetch_candidates()  # type: ignore[union-attr]
                if entry.opaque_id == self.item_opaque_id
            )
            decision = self.service._on_stall(item, 1)
            assert decision is StallDecision.ESCALATE
            return AgentResult(role=request.role, success=False, failure_reason="killed")
        return self._delegate.run(request)


def _stalling_service(
    data_dir: Path, source_repo: Path, *, unattended: bool
) -> tuple[FactoryService, StallingRuntime]:
    item = _item(21, source_repo)
    runtime = StallingRuntime()
    service = FactoryService(
        config=_config(data_dir, unattended=unattended),
        store=FileRunStore(data_dir),
        runtime=runtime,
        source_repo=source_repo,
        github_repo="acme/repo",
        provider=LocalProvider([item]),
    )
    runtime.service = service
    runtime.item_opaque_id = item.opaque_id
    return service, runtime


def test_a_run_writes_a_lease_heartbeat_while_its_agent_works(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    runtime = HeartbeatWatchingRuntime(store)
    controller = WorkflowController(build_config(data_dir), store, runtime)
    controller.heartbeat_interval_seconds = 0.01

    run = controller.run(_work_item(source_repo), source_repo, run_id="run-beat")

    assert run.state is WorkflowState.PR_READY
    assert len(runtime.seen) == 1


def test_the_heartbeat_stops_once_the_call_outlives_its_timeout(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    controller.heartbeat_interval_seconds = 0.01
    run = FactoryRun(id="run-late", work_item_id="w", state=WorkflowState.IMPLEMENTING)

    controller._heartbeat(run, threading.Event(), 0)

    with pytest.raises(FileNotFoundError):
        store.load_run("run-late")


def test_the_heartbeat_marks_activity_on_a_run_without_a_lease(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    controller.heartbeat_interval_seconds = 0.01
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = FactoryRun(
        id="run-free",
        work_item_id="w",
        state=WorkflowState.IMPLEMENTING,
        created_at=old,
        updated_at=old,
        last_activity_at=old,
    )
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()

    controller._heartbeat(run, stop, 60)

    stored = store.load_run("run-free")
    assert stored.last_activity_at is not None
    assert stored.last_activity_at > old


def test_a_heartbeat_that_cannot_be_written_is_logged(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenStore(FileRunStore):
        def save_run(self, run: FactoryRun) -> None:
            raise OSError("disk full")

    controller = WorkflowController(
        build_config(data_dir), BrokenStore(data_dir), FakeAgentRuntime()
    )
    controller.heartbeat_interval_seconds = 0.01
    run = FactoryRun(id="run-disk", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()

    controller._heartbeat(run, stop, 60)

    assert "could not write the heartbeat of run run-disk" in caplog.text


def test_a_cancelled_run_stops_before_its_next_agent_call(
    source_repo: Path, data_dir: Path
) -> None:
    controller = WorkflowController(build_config(data_dir), FileRunStore(data_dir), CancelAt())
    controller._runtime.controller = controller  # type: ignore[attr-defined]

    with pytest.raises(RunCancelledError) as caught:
        controller.run(_work_item(source_repo), source_repo, run_id="run-cancel")

    assert caught.value.run_id == "run-cancel"
    controller.clear_cancel("run-cancel")
    assert "run-cancel" not in controller._cancel_requests


class CancelAt:
    """Asks the controller to cancel during the Implementer call, then raises like a killed CLI."""

    def __init__(self) -> None:
        self.controller: WorkflowController | None = None
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER:
            assert self.controller is not None
            self.controller.cancel_run("run-cancel")
            raise RuntimeError("agent was killed")
        return self._delegate.run(request)


def test_a_stalled_attended_run_halts_as_stalled(source_repo: Path, data_dir: Path) -> None:
    service, runtime = _stalling_service(data_dir, source_repo, unattended=False)

    try:
        service.run_once(drain_timeout_seconds=60)
    finally:
        service.shutdown()

    [run] = service.store.list_runs()
    assert runtime.stalls == 1
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason is not None
    assert run.failure_reason.startswith(STALLED_HALT_REASON)
    assert run.active_invocation is None
    assert classify_halt_reason(run)[1] is HaltReasonCode.STALLED
    assert run.escalation is not None
    assert run.escalation.reason_code == HaltReasonCode.STALLED


def test_a_stalled_unattended_run_is_queued_and_continues(
    source_repo: Path, data_dir: Path
) -> None:
    service, runtime = _stalling_service(data_dir, source_repo, unattended=True)

    try:
        service.run_once(drain_timeout_seconds=60)
        [stalled] = service.store.list_runs()
        assert stalled.state is WorkflowState.IMPLEMENTING
        assert service._interrupted == {stalled.id: stalled.work_item_id}
        service.reconcile_escalation()
        service.drain(60)
    finally:
        service.shutdown()

    [run] = service.store.list_runs()
    assert runtime.stalls == 1
    assert run.state is WorkflowState.PR_READY
    assert run.restart_recoveries == 1


def test_a_stall_for_unknown_work_only_escalates(source_repo: Path, data_dir: Path) -> None:
    service, _ = _stalling_service(data_dir, source_repo, unattended=False)
    try:
        assert service._on_stall(_item(99, source_repo), 1) is StallDecision.ESCALATE
    finally:
        service.shutdown()


def test_a_settled_finished_run_is_left_alone(source_repo: Path, data_dir: Path) -> None:
    service, _ = _stalling_service(data_dir, source_repo, unattended=True)
    service.store.save_run(
        FactoryRun(id="run-done", work_item_id="w", state=WorkflowState.DONE, unattended=True)
    )
    try:
        assert service._settle_stalled_run("run-done").state is WorkflowState.DONE
    finally:
        service.shutdown()
    assert service._interrupted == {}


def test_run_handle_counts_a_lease_heartbeat_as_activity(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    beat = old + timedelta(minutes=10)
    store.save_run(
        FactoryRun(
            id="run-beat",
            work_item_id="w",
            state=WorkflowState.IMPLEMENTING,
            created_at=old,
            updated_at=old,
            last_activity_at=old,
            lease=RunLease(host="h", pid=1, heartbeat_at=beat),
        )
    )

    assert ThreadPoolRunHandle("run-beat", store, old).last_activity_at() == beat


def test_cancel_run_kills_the_agent_process_of_the_run_thread(data_dir: Path) -> None:
    controller = WorkflowController(build_config(data_dir), FileRunStore(data_dir), _NoRuntime())
    process = track_child(
        subprocess.Popen(["sleep", "30"], text=True, start_new_session=True)  # noqa: S607
    )
    controller._agent_threads["run-kill"] = threading.get_ident()

    controller.cancel_run("run-kill")

    assert process.wait(timeout=10) != 0
    assert kill_thread_children(threading.get_ident()) == 0
    release_thread(threading.get_ident())


def test_kill_thread_children_skips_a_thread_with_no_children() -> None:
    assert kill_thread_children(-1) == 0


class _NoRuntime:
    def run(self, request: AgentRequest) -> AgentResult:  # pragma: no cover - never called
        raise AssertionError(request)


def _work_item(source_repo: Path) -> Any:
    from software_agent_factory.service import build_work_item

    return build_work_item(_item(31, source_repo))


def test_a_child_started_after_a_kill_is_stopped_at_once() -> None:
    def start_after_kill() -> None:
        kill_thread_children(threading.get_ident())
        started.append(
            track_child(
                subprocess.Popen(["sleep", "30"], text=True, start_new_session=True)  # noqa: S607
            )
        )
        release_thread(threading.get_ident())
        started.append(
            track_child(
                subprocess.Popen(["sleep", "0"], text=True, start_new_session=True)  # noqa: S607
            )
        )

    started: list[subprocess.Popen[str]] = []
    worker = threading.Thread(target=start_after_kill)
    worker.start()
    worker.join()

    assert started[0].wait(timeout=10) != 0
    assert started[1].wait(timeout=10) == 0


def test_a_cancel_before_the_agent_call_skips_the_runtime(
    source_repo: Path, data_dir: Path
) -> None:
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="high",
        work_item=_work_item(source_repo),
        timeout_seconds=30,
    )
    controller = WorkflowController(build_config(data_dir), FileRunStore(data_dir), _NoRuntime())
    run = FactoryRun(id="run-early", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    controller.cancel_run("run-early")

    with pytest.raises(RunCancelledError):
        controller._call_runtime(run, request)

    assert "run-early" not in controller._agent_threads


def test_a_stall_after_the_run_finished_leaves_no_stop_request(
    source_repo: Path, data_dir: Path
) -> None:
    service, _ = _stalling_service(data_dir, source_repo, unattended=False)
    item = _item(7, source_repo)
    handle = ThreadPoolRunHandle("run-old", service.store, utc_now())
    done: Future[FactoryRun] = Future()
    done.set_result(FactoryRun(id="run-old", work_item_id="w", state=WorkflowState.DONE))
    handle.attach(done)
    service._handles["run-old"] = handle
    service._run_ids[item.opaque_id] = "run-old"
    try:
        assert service._on_stall(item, 1) is StallDecision.ESCALATE
    finally:
        service.shutdown()
    assert service.controller is not None
    assert "run-old" not in service.controller._cancel_requests
