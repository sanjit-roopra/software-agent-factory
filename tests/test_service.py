"""Tests for the scheduler/tracker/controller composition (``factory start``).

No network access: the tracker provider is a local fake, agents are the
deterministic ``FakeAgentRuntime``, and pull requests/CI stay disabled.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest
from factory_testing import build_config, git

from software_agent_factory.agents import FakeAgentRuntime
from software_agent_factory.github import GitHubCommandError
from software_agent_factory.models import FactoryRun, WorkflowState
from software_agent_factory.scheduler import (
    ReconciliationAction,
    TrackerItem,
    deterministic_work_item_id,
)
from software_agent_factory.service import (
    FactoryService,
    ThreadPoolRunHandle,
    build_work_item,
    default_recovery_decision,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _item(number: int, repository_path: Path, *, title: str | None = None) -> TrackerItem:
    return TrackerItem(
        opaque_id=f"acme/repo#{number}",
        identifier=f"acme/repo#{number}",
        title=title or f"Issue {number}",
        description=f"Do the work described in issue {number}.",
        state="OPEN",
        labels=("agent-ready",),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=number),
        repository_path=str(repository_path),
    )


class LocalProvider:
    """In-memory ``TrackerProvider``: never touches GitHub."""

    def __init__(self, items: Sequence[TrackerItem]) -> None:
        self._items = list(items)
        self.fetch_calls = 0

    def fetch_candidates(self) -> Sequence[TrackerItem]:
        self.fetch_calls += 1
        return list(self._items)

    def fetch_by_ids(self, opaque_ids: Sequence[str]) -> Sequence[TrackerItem]:
        wanted = set(opaque_ids)
        return [item for item in self._items if item.opaque_id in wanted]


def _service(
    data_dir: Path,
    source_repo: Path,
    provider: LocalProvider,
    *,
    max_concurrent_tasks: int = 1,
) -> FactoryService:
    config = build_config(
        data_dir,
        scheduler={
            "enabled": True,
            "poll_interval_seconds": 1,
            "max_concurrent_tasks": max_concurrent_tasks,
            "stall_timeout_seconds": 300,
            "required_label": "agent-ready",
        },
    )
    return FactoryService(
        config=config,
        store=FileRunStore(data_dir),
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="acme/repo",
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Work item mapping
# ---------------------------------------------------------------------------


def test_build_work_item_uses_the_deterministic_tracker_id(tmp_path: Path) -> None:
    item = _item(12, tmp_path)

    work_item = build_work_item(item)

    assert work_item.id == deterministic_work_item_id(item)
    assert work_item.id == "tracker-acme/repo#12"
    assert work_item.source == "GITHUB"
    assert work_item.external_id == "acme/repo#12"
    assert work_item.labels == ["agent-ready"]


def test_build_work_item_falls_back_to_the_title_for_an_empty_body(tmp_path: Path) -> None:
    item = _item(3, tmp_path).model_copy(update={"description": "   "})

    assert build_work_item(item).description == "Issue 3"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_run_once_dispatches_and_completes_a_tracked_item(
    source_repo: Path, data_dir: Path
) -> None:
    provider = LocalProvider([_item(1, source_repo)])
    service = _service(data_dir, source_repo, provider)

    try:
        report = service.run_once(drain_timeout_seconds=60)
    finally:
        service.shutdown()

    assert report.dispatched == ("acme/repo#1",)
    runs = service.store.list_runs()
    assert len(runs) == 1
    assert runs[0].state is WorkflowState.PR_READY
    assert runs[0].completed_at is not None
    assert runs[0].work_item_id == "tracker-acme/repo#1"


def test_concurrency_two_dispatches_two_items_with_isolated_workspaces(
    source_repo: Path, data_dir: Path
) -> None:
    provider = LocalProvider([_item(1, source_repo), _item(2, source_repo)])
    service = _service(data_dir, source_repo, provider, max_concurrent_tasks=2)

    try:
        report = service.run_once(drain_timeout_seconds=120)
    finally:
        service.shutdown()

    assert sorted(report.dispatched) == ["acme/repo#1", "acme/repo#2"]
    runs = service.store.list_runs()
    assert len(runs) == 2
    assert all(run.state is WorkflowState.PR_READY for run in runs)
    workspaces = {run.workspace_path for run in runs}
    assert len(workspaces) == 2, "each run gets its own worktree"
    branches = {run.branch_name for run in runs}
    assert len(branches) == 2
    assert all(branch.startswith("factory/") for branch in branches)
    # The source repository is untouched by either run.
    assert git(source_repo, "status", "--porcelain") == ""


def test_already_running_item_is_not_dispatched_twice(source_repo: Path, data_dir: Path) -> None:
    provider = LocalProvider([_item(1, source_repo)])
    service = _service(data_dir, source_repo, provider)

    try:
        first = service.scheduler.tick()
        second = service.scheduler.tick()
        service.drain(60)
    finally:
        service.shutdown()

    assert first.dispatched == ("acme/repo#1",)
    assert second.dispatched == ()
    assert len(service.store.list_runs()) == 1


def test_persisted_nonterminal_run_blocks_a_duplicate_dispatch(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    item = _item(7, source_repo)
    store.save_run(
        FactoryRun(
            id="run-manual",
            work_item_id=deterministic_work_item_id(item),
            state=WorkflowState.IMPLEMENTING,
        )
    )
    provider = LocalProvider([item])
    service = _service(data_dir, source_repo, provider)

    try:
        report = service.scheduler.tick()
    finally:
        service.shutdown()

    assert report.dispatched == ()
    assert report.eligible_count == 0


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_recovery_escalates_abandoned_runs_through_the_controller(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    item = _item(9, source_repo)
    abandoned = FactoryRun(
        id="run-abandoned",
        work_item_id=deterministic_work_item_id(item),
        state=WorkflowState.IMPLEMENTING,
        workspace_path=str(data_dir / "workspaces" / "tracker-acme-repo-9"),
    )
    store.save_run(abandoned)
    provider = LocalProvider([item])
    service = _service(data_dir, source_repo, provider)

    try:
        records = service.recover()
    finally:
        service.shutdown()

    assert [record.action for record in records] == [ReconciliationAction.NEEDS_HUMAN]
    recovered = store.load_run("run-abandoned")
    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert "abandoned" in (recovered.failure_reason or "")
    # No paid retry was spent and the workspace reference is preserved.
    assert recovered.attempt_records == []
    assert recovered.workspace_path == abandoned.workspace_path


def test_default_recovery_decision_leaves_finished_runs_alone() -> None:
    finished = FactoryRun(id="r", work_item_id="w", state=WorkflowState.DONE)
    unfinished = FactoryRun(id="r2", work_item_id="w", state=WorkflowState.PLANNING)

    assert default_recovery_decision(finished) is ReconciliationAction.LEAVE
    assert default_recovery_decision(unfinished) is ReconciliationAction.NEEDS_HUMAN


def test_service_refuses_to_start_when_the_scheduler_is_disabled(
    source_repo: Path, data_dir: Path
) -> None:
    config = build_config(data_dir)
    with pytest.raises(ValueError, match="scheduler.enabled"):
        FactoryService(
            config=config,
            store=FileRunStore(data_dir),
            runtime=FakeAgentRuntime(),
            source_repo=source_repo,
            github_repo="acme/repo",
            provider=LocalProvider([]),
        )


def test_service_does_not_construct_a_github_provider_when_one_is_injected(
    source_repo: Path, data_dir: Path
) -> None:
    provider = LocalProvider([])
    service = _service(data_dir, source_repo, provider)
    try:
        assert service.provider is provider
    finally:
        service.shutdown()


# ---------------------------------------------------------------------------
# Run handle behavior
# ---------------------------------------------------------------------------


def test_run_handle_reports_activity_from_the_persisted_run(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    handle = ThreadPoolRunHandle("run-x", store, started)

    # No persisted run yet: falls back to the dispatch time.
    assert handle.last_activity_at() == started

    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    run = controller.run(build_work_item(_item(4, source_repo)), source_repo, run_id="run-x")
    assert run.state is WorkflowState.PR_READY
    assert handle.last_activity_at() > started


def test_shutdown_cancels_active_work_and_releases_reservations(
    source_repo: Path, data_dir: Path
) -> None:
    provider = LocalProvider([_item(1, source_repo)])
    service = _service(data_dir, source_repo, provider)
    service.scheduler.tick()
    assert service.scheduler.active_count == 1
    service.drain(60)

    service.shutdown()

    assert service.scheduler.active_count == 0


def test_run_forever_stops_on_the_stop_event(source_repo: Path, data_dir: Path) -> None:
    provider = LocalProvider([])
    service = _service(data_dir, source_repo, provider)
    stop_event = threading.Event()
    stop_event.set()

    service.run_forever(stop_event)

    assert provider.fetch_calls == 0


def test_run_forever_retries_transient_github_poll_failures(
    source_repo: Path,
    data_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingOnceProvider(LocalProvider):
        def fetch_candidates(self) -> Sequence[TrackerItem]:
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                raise GitHubCommandError(("issue", "list"), 1, "temporary outage")
            return []

    class StopAfterTwoWaits:
        def __init__(self) -> None:
            self.wait_calls = 0

        def is_set(self) -> bool:
            return False

        def wait(self, _timeout: float) -> bool:
            self.wait_calls += 1
            return self.wait_calls == 2

    provider = FailingOnceProvider([])
    service = _service(data_dir, source_repo, provider)
    waiter = StopAfterTwoWaits()

    with caplog.at_level("ERROR", logger="software_agent_factory.service"):
        service.run_forever(waiter)

    assert provider.fetch_calls == 2
    assert waiter.wait_calls == 2
    assert "GitHub backlog polling failed; retrying after 1.0 seconds" in caplog.text


def test_run_forever_does_not_hide_unexpected_poll_failures(
    source_repo: Path, data_dir: Path
) -> None:
    class BrokenProvider(LocalProvider):
        def fetch_candidates(self) -> Sequence[TrackerItem]:
            raise RuntimeError("programming error")

    service = _service(data_dir, source_repo, BrokenProvider([]))

    with pytest.raises(RuntimeError, match="programming error"):
        service.run_forever(threading.Event())


# ---------------------------------------------------------------------------
# Once-only dispatch of an item the backlog never withdraws
# ---------------------------------------------------------------------------


def test_a_finished_item_is_never_redispatched(source_repo: Path, data_dir: Path) -> None:
    """GitHub keeps an issue open and labelled after a run finishes, and the
    factory holds no write access to the backlog. Re-dispatching would mint a
    fresh, empty retry budget on every tick, so it must not happen."""
    provider = LocalProvider([_item(1, source_repo)])
    service = _service(data_dir, source_repo, provider)

    try:
        first = service.run_once(drain_timeout_seconds=60)
        second = service.scheduler.tick()
        third = service.scheduler.tick()
    finally:
        service.shutdown()

    assert first.dispatched == ("acme/repo#1",)
    assert second.dispatched == ()
    assert second.eligible_count == 0
    assert third.dispatched == ()
    assert len(service.store.list_runs()) == 1
    # The tracker still reports the item as an open candidate.
    assert len(provider.fetch_candidates()) == 1


def test_an_escalated_item_is_not_redispatched_after_a_restart(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    item = _item(2, source_repo)
    store.save_run(
        FactoryRun(
            id="run-escalated",
            work_item_id=deterministic_work_item_id(item),
            state=WorkflowState.NEEDS_HUMAN,
            failure_reason="a human must look at this",
        )
    )

    # A brand new process (fresh service) must honor that decision.
    provider = LocalProvider([item])
    service = _service(data_dir, source_repo, provider)
    try:
        service.recover()
        report = service.scheduler.tick()
    finally:
        service.shutdown()

    assert report.dispatched == ()
    assert store.load_run("run-escalated").state is WorkflowState.NEEDS_HUMAN


def test_already_run_filter_hides_items_from_both_provider_methods(
    source_repo: Path, data_dir: Path
) -> None:
    from software_agent_factory.service import AlreadyRunFilter

    store = FileRunStore(data_dir)
    fresh = _item(3, source_repo)
    done = _item(4, source_repo)
    store.save_run(
        FactoryRun(
            id="run-done",
            work_item_id=deterministic_work_item_id(done),
            state=WorkflowState.DONE,
        )
    )
    filtered = AlreadyRunFilter(LocalProvider([fresh, done]), store)

    assert [item.opaque_id for item in filtered.fetch_candidates()] == [fresh.opaque_id]
    assert [
        item.opaque_id for item in filtered.fetch_by_ids([fresh.opaque_id, done.opaque_id])
    ] == [fresh.opaque_id]


# ---------------------------------------------------------------------------
# Configured safety bounds reach the scheduler
# ---------------------------------------------------------------------------


def _service_with_scheduler(
    data_dir: Path, source_repo: Path, provider: LocalProvider, **scheduler: object
) -> FactoryService:
    settings: dict[str, object] = {
        "enabled": True,
        "poll_interval_seconds": 1,
        "max_concurrent_tasks": 1,
        "stall_timeout_seconds": 300,
        "required_label": "agent-ready",
    }
    settings.update(scheduler)
    return FactoryService(
        config=build_config(data_dir, scheduler=settings),
        store=FileRunStore(data_dir),
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="acme/repo",
        provider=provider,
    )


def test_configured_daily_run_limit_reaches_the_scheduler(
    source_repo: Path, data_dir: Path
) -> None:
    service = _service_with_scheduler(data_dir, source_repo, LocalProvider([]), max_runs_per_day=7)
    try:
        assert service.scheduler.max_runs_per_day == 7
        assert service.scheduler.store is service.store
    finally:
        service.shutdown()


def test_daily_run_limit_stops_dispatch_once_the_quota_is_spent(
    source_repo: Path, data_dir: Path
) -> None:
    """The bound is enforced against persisted runs, so it survives a
    restart instead of resetting with the process."""
    store = FileRunStore(data_dir)
    now = datetime.now(timezone.utc)
    store.save_run(
        FactoryRun(
            id="run-earlier-today",
            work_item_id="tracker-acme/repo#999",
            state=WorkflowState.DONE,
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
    )
    service = _service_with_scheduler(
        data_dir, source_repo, LocalProvider([_item(1, source_repo)]), max_runs_per_day=1
    )

    try:
        report = service.run_once(drain_timeout_seconds=60)
    finally:
        service.shutdown()

    assert report.dispatched == ()
    assert report.rate_limited is True
    assert [run.id for run in store.list_runs()] == ["run-earlier-today"]


def test_a_null_daily_run_limit_is_unbounded(source_repo: Path, data_dir: Path) -> None:
    service = _service_with_scheduler(
        data_dir, source_repo, LocalProvider([_item(1, source_repo)]), max_runs_per_day=None
    )

    try:
        report = service.run_once(drain_timeout_seconds=60)
    finally:
        service.shutdown()

    assert service.scheduler.max_runs_per_day is None
    assert report.rate_limited is False
    assert report.dispatched == ("acme/repo#1",)


def test_dispatch_and_completion_are_logged_with_run_correlation(
    source_repo: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``factory start`` under launchd has no console; the structured record
    is how an operator later reconstructs what ran."""
    service = _service_with_scheduler(data_dir, source_repo, LocalProvider([_item(1, source_repo)]))

    # Attach the capture handler to the service logger directly: the package
    # logger stops propagating once structured logging is configured, so
    # relying on propagation to the root logger would be fragile.
    service_logger = logging.getLogger("software_agent_factory.service")
    service_logger.addHandler(caplog.handler)
    previous_level = service_logger.level
    service_logger.setLevel(logging.INFO)
    try:
        service.run_once(drain_timeout_seconds=60)
    finally:
        service.shutdown()
        service_logger.removeHandler(caplog.handler)
        service_logger.setLevel(previous_level)

    tagged = [record for record in caplog.records if getattr(record, "run_id", None)]
    assert tagged, "expected run-tagged dispatch/completion records"
    assert {record.state for record in tagged} >= {WorkflowState.PR_READY}
    assert any("tick:" in record.message for record in caplog.records)
