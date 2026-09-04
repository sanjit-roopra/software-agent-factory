"""Tests for software_agent_factory.scheduler.

Every test is fully deterministic: no real sleeping, no threads, no
filesystem access beyond ``FileRunStore`` (already exercised by
tests/test_store.py) via pytest's ``tmp_path``, and no network. Time and
"stop" signals are controlled by small fakes (``FakeClock``, ``FakeWaiter``)
rather than ``time.sleep`` or ``threading.Event``, matching the "generic
local backlog scheduling" contract in PLAN.md Phases 13-14.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import pytest

from software_agent_factory.models import AgentRole, AttemptRecord, FactoryRun, WorkflowState
from software_agent_factory.scheduler import (
    DispatchOutcome,
    ReconciliationAction,
    Scheduler,
    StallDecision,
    TickReport,
    TrackerItem,
    deterministic_work_item_id,
    opaque_id_from_work_item_id,
)
from software_agent_factory.store import FileRunStore

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _dt(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def make_item(
    opaque_id: str,
    *,
    identifier: str | None = None,
    created_at: datetime | None = None,
    priority: str | None = None,
    blockers: tuple[str, ...] = (),
    dispatchable: bool = True,
    repository_path: str = "/repo",
) -> TrackerItem:
    return TrackerItem(
        opaque_id=opaque_id,
        identifier=identifier or opaque_id,
        title=f"title-{opaque_id}",
        description="",
        state="open",
        labels=(),
        priority=priority,
        created_at=created_at if created_at is not None else _dt(),
        blockers=blockers,
        dispatchable=dispatchable,
        repository_path=repository_path,
    )


class FakeClock:
    """Injected clock the test advances explicitly; never wall-clock time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@dataclass
class FakeHandle:
    """RunHandle test double. Completion/outcome/activity are entirely
    controlled by the test, never by a real thread, process, or clock."""

    outcome_value: DispatchOutcome = DispatchOutcome.SUCCEEDED
    done: bool = True
    activity_at: datetime = field(default_factory=lambda: _dt())
    cancelled: bool = False
    run_id: str = "fake-run"

    def is_done(self) -> bool:
        return self.done

    def outcome(self) -> DispatchOutcome:
        return self.outcome_value

    def last_activity_at(self) -> datetime:
        return self.activity_at

    def cancel(self) -> None:
        self.cancelled = True


class FakeProvider:
    """TrackerProvider test double. ``candidates`` and ``by_id`` are kept
    separate (rather than derived from one another) so tests can freely
    simulate divergence between a stale ``fetch_candidates`` snapshot and
    the freshest ``fetch_by_ids`` state."""

    def __init__(self, items: Sequence[TrackerItem]) -> None:
        self.candidates: list[TrackerItem] = list(items)
        self.by_id: dict[str, TrackerItem] = {item.opaque_id: item for item in items}
        self.fetch_candidates_calls = 0
        self.fetch_by_ids_calls: list[list[str]] = []

    def fetch_candidates(self) -> list[TrackerItem]:
        self.fetch_candidates_calls += 1
        return list(self.candidates)

    def fetch_by_ids(self, opaque_ids: Sequence[str]) -> list[TrackerItem]:
        self.fetch_by_ids_calls.append(list(opaque_ids))
        return [self.by_id[opaque_id] for opaque_id in opaque_ids if opaque_id in self.by_id]


class FakeWaiter:
    """Waiter test double: stops the polling loop after a fixed number of
    ticks without ever calling ``time.sleep``."""

    def __init__(self, stop_after_ticks: int) -> None:
        self.stop_after_ticks = stop_after_ticks
        self.wait_calls = 0
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, timeout: float) -> bool:
        self.wait_calls += 1
        if self.wait_calls >= self.stop_after_ticks:
            self._stopped = True
        return self._stopped


# ---------------------------------------------------------------------------
# Reconciliation ordering
# ---------------------------------------------------------------------------


def test_tick_reconciles_active_runs_before_dispatching_new_candidates() -> None:
    item_a = make_item("A", created_at=_dt(0))
    item_b = make_item("B", created_at=_dt(1))
    provider = FakeProvider([item_a, item_b])

    handles: dict[str, FakeHandle] = {}

    def dispatch(item: TrackerItem) -> FakeHandle:
        handle = FakeHandle(done=False)
        handles[item.opaque_id] = handle
        return handle

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=1)

    first = scheduler.tick()
    assert first.dispatched == ("A",)
    assert scheduler.active_count == 1

    # Capacity is exhausted: B cannot be dispatched yet.
    second = scheduler.tick()
    assert second.at_capacity is True
    assert second.dispatched == ()

    # A finishes and (as a real tracker would once work completes) leaves
    # the backlog. The same tick that reconciles A must free enough
    # capacity to dispatch B, proving reconciliation happens before
    # discovery.
    handles["A"].done = True
    provider.candidates = [item_b]
    third = scheduler.tick()
    assert third.completed == (("A", DispatchOutcome.SUCCEEDED),)
    assert third.dispatched == ("B",)


def test_completed_work_releases_reservation_allowing_immediate_redispatch() -> None:
    item = make_item("solo")
    provider = FakeProvider([item])
    handle = FakeHandle(done=False)
    call_count = 0

    def dispatch(_: TrackerItem) -> FakeHandle:
        nonlocal call_count
        call_count += 1
        return handle

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=1)

    scheduler.tick()
    assert scheduler.active_count == 1
    assert call_count == 1

    handle.done = True
    report = scheduler.tick()
    assert report.completed == (("solo", DispatchOutcome.SUCCEEDED),)
    # Released, still an eligible/dispatchable candidate -> redispatched
    # within the same tick.
    assert report.dispatched == ("solo",)
    assert call_count == 2


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


def test_eligible_candidates_sort_by_priority_then_created_at_then_identifier() -> None:
    items = [
        make_item("z-p1", identifier="z-p1", priority="P1", created_at=_dt(10)),
        make_item("a-p1", identifier="a-p1", priority="P1", created_at=_dt(10)),
        make_item("p0-new", identifier="p0-new", priority="P0", created_at=_dt(20)),
        make_item("unsupported", identifier="unsupported", priority="ZZZ", created_at=_dt(0)),
        make_item("no-priority", identifier="no-priority", priority=None, created_at=_dt(5)),
    ]
    provider = FakeProvider(items)
    dispatched_order: list[str] = []

    def dispatch(item: TrackerItem) -> FakeHandle:
        dispatched_order.append(item.opaque_id)
        # A real tracker item would leave the open backlog once dispatched
        # (e.g. the issue gets an "in-progress" label); simulate that so
        # draining the list across ticks terminates.
        provider.candidates = [c for c in provider.candidates if c.opaque_id != item.opaque_id]
        return FakeHandle(done=True)

    # max_concurrent_tasks is restricted to 1 or 2 (PLAN.md Phase 13/14), so
    # draining all 5 candidates in priority order takes several ticks; each
    # dispatched handle is immediately done, so the next tick's
    # reconciliation frees capacity before it discovers/dispatches more.
    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=2)
    while len(dispatched_order) < len(items):
        scheduler.tick()

    # P0 first; then P1 tied on created_at, broken by identifier; then
    # unrecognized/absent priorities (rank tied at the bottom) ordered by
    # created_at.
    assert dispatched_order == ["p0-new", "a-p1", "z-p1", "unsupported", "no-priority"]


# ---------------------------------------------------------------------------
# Eligibility filtering
# ---------------------------------------------------------------------------


def test_blocked_and_non_dispatchable_items_are_excluded() -> None:
    blocked = make_item("blocked", blockers=("other",))
    not_ready = make_item("not-ready", dispatchable=False)
    eligible = make_item("eligible")
    provider = FakeProvider([blocked, not_ready, eligible])
    dispatched: list[str] = []

    def dispatch(item: TrackerItem) -> FakeHandle:
        dispatched.append(item.opaque_id)
        return FakeHandle()

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=2)
    report = scheduler.tick()

    assert dispatched == ["eligible"]
    assert report.eligible_count == 1
    assert report.candidates_fetched == 3


def test_already_active_candidate_is_not_reconsidered() -> None:
    item = make_item("busy")
    provider = FakeProvider([item])
    scheduler = Scheduler(provider, lambda i: FakeHandle(done=False), max_concurrent_tasks=2)

    first = scheduler.tick()
    assert first.dispatched == ("busy",)

    second = scheduler.tick()
    assert second.dispatched == ()
    assert second.eligible_count == 0


# ---------------------------------------------------------------------------
# Stale-candidate revalidation
# ---------------------------------------------------------------------------


def test_candidate_that_became_ineligible_before_dispatch_is_skipped() -> None:
    item = make_item("stale")
    provider = FakeProvider([item])
    # Simulate the tracker changing between fetch_candidates and dispatch,
    # e.g. a required label being removed by another actor.
    provider.by_id["stale"] = item.model_copy(update={"dispatchable": False})

    dispatched: list[str] = []
    scheduler = Scheduler(provider, lambda i: (dispatched.append(i.opaque_id), FakeHandle())[1])
    report = scheduler.tick()

    assert dispatched == []
    assert report.skipped_stale == ("stale",)
    assert provider.fetch_by_ids_calls == [["stale"]]


def test_candidate_removed_from_tracker_before_dispatch_is_skipped() -> None:
    item = make_item("gone")
    provider = FakeProvider([item])
    del provider.by_id["gone"]  # still a stale entry in `candidates`

    dispatched: list[str] = []
    scheduler = Scheduler(provider, lambda i: (dispatched.append(i.opaque_id), FakeHandle())[1])
    report = scheduler.tick()

    assert dispatched == []
    assert report.skipped_stale == ("gone",)


def test_revalidation_uses_the_freshest_item_for_dispatch() -> None:
    stale_item = make_item("refresh-me")
    fresh_item = stale_item.model_copy(update={"priority": "P0"})
    provider = FakeProvider([stale_item])
    provider.by_id["refresh-me"] = fresh_item

    dispatched: list[TrackerItem] = []
    scheduler = Scheduler(provider, lambda i: (dispatched.append(i), FakeHandle())[1])
    scheduler.tick()

    assert dispatched == [fresh_item]


# ---------------------------------------------------------------------------
# Duplicate prevention
# ---------------------------------------------------------------------------


def test_duplicate_opaque_id_in_candidate_list_is_dispatched_once() -> None:
    item = make_item("dup")
    provider = FakeProvider([item, item])  # a buggy provider returning duplicates
    dispatched: list[str] = []

    def dispatch(candidate: TrackerItem) -> FakeHandle:
        dispatched.append(candidate.opaque_id)
        return FakeHandle()

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=2)
    report = scheduler.tick()

    assert dispatched == ["dup"]
    assert report.dispatched == ("dup",)
    assert report.skipped_stale == ("dup",)
    assert scheduler.active_count == 1


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------


def test_max_concurrent_tasks_one_dispatches_a_single_item_per_tick() -> None:
    items = [make_item(f"i{n}", created_at=_dt(n)) for n in range(3)]
    provider = FakeProvider(items)
    dispatched: list[str] = []

    def dispatch(item: TrackerItem) -> FakeHandle:
        dispatched.append(item.opaque_id)
        return FakeHandle(done=False)

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=1)
    report = scheduler.tick()

    assert report.dispatched == ("i0",)
    assert scheduler.active_count == 1


def test_max_concurrent_tasks_two_dispatches_two_items_per_tick() -> None:
    items = [make_item(f"i{n}", created_at=_dt(n)) for n in range(3)]
    provider = FakeProvider(items)
    dispatched: list[str] = []

    def dispatch(item: TrackerItem) -> FakeHandle:
        dispatched.append(item.opaque_id)
        return FakeHandle(done=False)

    scheduler = Scheduler(provider, dispatch, max_concurrent_tasks=2)
    report = scheduler.tick()

    assert report.dispatched == ("i0", "i1")
    assert scheduler.active_count == 2


def test_max_concurrent_tasks_rejects_values_other_than_one_or_two() -> None:
    provider = FakeProvider([])

    for invalid in (0, -1, 3, 10):
        try:
            Scheduler(provider, lambda i: FakeHandle(), max_concurrent_tasks=invalid)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for max_concurrent_tasks={invalid}")

    # 1 and 2 remain the only supported concurrency levels (PLAN.md Phase 13/14).
    Scheduler(provider, lambda i: FakeHandle(), max_concurrent_tasks=1)
    Scheduler(provider, lambda i: FakeHandle(), max_concurrent_tasks=2)


# ---------------------------------------------------------------------------
# Explicit cancellation and shutdown
# ---------------------------------------------------------------------------


def test_explicit_cancel_requests_cancellation_and_releases_reservation() -> None:
    item = make_item("cancel-me")
    provider = FakeProvider([item])
    handle = FakeHandle(done=False)
    scheduler = Scheduler(provider, lambda i: handle, max_concurrent_tasks=1)

    scheduler.tick()
    assert scheduler.active_count == 1

    assert scheduler.cancel("cancel-me") is True
    assert handle.cancelled is True
    assert scheduler.active_count == 0
    assert scheduler.cancel("cancel-me") is False


def test_shutdown_cancels_every_active_run() -> None:
    items = [make_item("a"), make_item("b")]
    provider = FakeProvider(items)
    handles = {"a": FakeHandle(done=False), "b": FakeHandle(done=False)}
    scheduler = Scheduler(provider, lambda i: handles[i.opaque_id], max_concurrent_tasks=2)

    scheduler.tick()
    assert scheduler.active_count == 2

    cancelled = scheduler.shutdown()
    assert set(cancelled) == {"a", "b"}
    assert all(handle.cancelled for handle in handles.values())
    assert scheduler.active_count == 0


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


def test_recover_surfaces_nonterminal_runs_and_skips_terminal_ones(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    stuck = FactoryRun(id="run-stuck", work_item_id="WI-1", state=WorkflowState.IMPLEMENTING)
    # A PR_READY run counts as finished only once the controller stamped
    # completed_at (the manual, pull-request-disabled endpoint).
    done = FactoryRun(
        id="run-done",
        work_item_id="WI-2",
        state=WorkflowState.PR_READY,
        created_at=_dt(0),
        updated_at=_dt(1),
        completed_at=_dt(1),
    )
    store.save_run(stuck)
    store.save_run(done)

    seen: list[str] = []

    def decide(run: FactoryRun) -> ReconciliationAction:
        seen.append(run.id)
        return ReconciliationAction.NEEDS_HUMAN

    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    records = scheduler.recover(store, decide)

    # The finished PR_READY run is never handed to the callback.
    assert seen == ["run-stuck"]
    assert len(records) == 1
    assert records[0].run_id == "run-stuck"
    assert records[0].work_item_id == "WI-1"
    assert records[0].previous_state == WorkflowState.IMPLEMENTING
    assert records[0].action == ReconciliationAction.NEEDS_HUMAN

    # Recovery never deletes or otherwise mutates persisted state.
    assert store.load_run("run-stuck") == stuck
    assert store.load_run("run-done") == done


def test_recover_can_requeue_ambiguous_runs(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    ambiguous = FactoryRun(id="run-ambiguous", work_item_id="WI-9", state=WorkflowState.VERIFYING)
    store.save_run(ambiguous)

    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    records = scheduler.recover(store, lambda run: ReconciliationAction.REQUEUE)

    assert records == [
        records[0].__class__(
            run_id="run-ambiguous",
            work_item_id="WI-9",
            previous_state=WorkflowState.VERIFYING,
            action=ReconciliationAction.REQUEUE,
        )
    ]


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------


def test_stall_detection_cancels_retries_then_bounds_and_escalates() -> None:
    item = make_item("stalling")
    provider = FakeProvider([item])
    clock = FakeClock(_dt(0))
    handle = FakeHandle(done=False, activity_at=_dt(0))

    stall_calls: list[tuple[str, int]] = []

    def on_stall(tracker_item: TrackerItem, attempt: int) -> StallDecision:
        stall_calls.append((tracker_item.opaque_id, attempt))
        return StallDecision.RETRY  # always asks to retry

    scheduler = Scheduler(
        provider,
        lambda i: handle,
        max_concurrent_tasks=1,
        clock=clock,
        stall_timeout_seconds=30.0,
        max_stall_retries=1,
        on_stall=on_stall,
    )

    scheduler.tick()
    assert scheduler.active_count == 1
    assert handle.cancelled is False

    clock.advance(31)
    first_stall = scheduler.tick()
    assert first_stall.stalled == ("stalling",)
    assert handle.cancelled is True
    assert stall_calls == [("stalling", 1)]
    assert scheduler.is_escalated("stalling") is False
    # Attempt 1 <= max_stall_retries(1): released and redispatched same tick.
    assert first_stall.dispatched == ("stalling",)
    assert scheduler.active_count == 1

    # The redispatched run reuses the same (still stale) handle, so it is
    # immediately stale again without any further clock advance.
    second_stall = scheduler.tick()
    assert second_stall.stalled == ("stalling",)
    assert stall_calls == [("stalling", 1), ("stalling", 2)]
    # Attempt 2 exceeds max_stall_retries(1): the callback's RETRY answer is
    # overridden and the item is escalated instead (bounded retries).
    assert scheduler.is_escalated("stalling") is True
    assert second_stall.dispatched == ()
    assert scheduler.active_count == 0


def test_stall_detection_is_disabled_without_a_configured_timeout() -> None:
    item = make_item("never-checked")
    provider = FakeProvider([item])
    clock = FakeClock(_dt(0))
    handle = FakeHandle(done=False, activity_at=_dt(0))

    scheduler = Scheduler(provider, lambda i: handle, max_concurrent_tasks=1, clock=clock)

    scheduler.tick()
    clock.advance(10_000)
    report = scheduler.tick()

    assert report.stalled == ()
    assert scheduler.active_count == 1


# ---------------------------------------------------------------------------
# Polling daemon loop
# ---------------------------------------------------------------------------


def test_run_forever_stops_on_waiter_without_sleeping() -> None:
    item = make_item("looping")
    provider = FakeProvider([item])
    scheduler = Scheduler(provider, lambda i: FakeHandle(done=True), max_concurrent_tasks=1)
    waiter = FakeWaiter(stop_after_ticks=3)
    tick_reports: list[TickReport] = []

    scheduler.run_forever(waiter, poll_interval_seconds=3600.0, on_tick=tick_reports.append)

    assert len(tick_reports) == 3
    assert waiter.wait_calls == 3
    assert provider.fetch_candidates_calls == 3


def test_run_forever_does_not_tick_when_already_stopped() -> None:
    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    waiter = FakeWaiter(stop_after_ticks=0)
    waiter._stopped = True  # already stopped before the loop starts
    calls: list[TickReport] = []

    scheduler.run_forever(waiter, poll_interval_seconds=1.0, on_tick=calls.append)

    assert calls == []
    assert provider.fetch_candidates_calls == 0


# ---------------------------------------------------------------------------
# Deterministic tracked work item ids / manual+daemon duplicate prevention
# ---------------------------------------------------------------------------


def test_deterministic_work_item_id_round_trips_through_its_inverse() -> None:
    item = make_item("issue-42")
    work_item_id = deterministic_work_item_id(item)

    assert work_item_id == "tracker-issue-42"
    assert opaque_id_from_work_item_id(work_item_id) == "issue-42"
    # Ids not produced by deterministic_work_item_id have no known inverse.
    assert opaque_id_from_work_item_id("manually-chosen-id") is None


def test_candidate_with_existing_persisted_nonterminal_run_is_not_redispatched(
    tmp_path: Path,
) -> None:
    """A manual `factory run` (or a previous daemon dispatch) that already
    created a non-terminal FactoryRun for a tracker item's deterministic
    work item id must block this scheduler from dispatching it again."""
    store = FileRunStore(tmp_path / "data")
    item = make_item("issue-1")
    store.save_run(
        FactoryRun(
            id="run-manual",
            work_item_id=deterministic_work_item_id(item),
            state=WorkflowState.IMPLEMENTING,
        )
    )

    provider = FakeProvider([item])
    dispatched: list[str] = []
    scheduler = Scheduler(
        provider,
        lambda i: (dispatched.append(i.opaque_id), FakeHandle())[1],
        max_concurrent_tasks=1,
        store=store,
    )

    report = scheduler.tick()

    assert dispatched == []
    assert report.eligible_count == 0
    assert report.skipped_stale == ()


def test_candidate_becomes_dispatchable_again_once_persisted_run_completes(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    item = make_item("issue-2")
    work_item_id = deterministic_work_item_id(item)
    store.save_run(
        FactoryRun(
            id="run-1",
            work_item_id=work_item_id,
            state=WorkflowState.PR_READY,
            created_at=_dt(0),
            updated_at=_dt(1),
            completed_at=_dt(1),
        )
    )

    provider = FakeProvider([item])
    dispatched: list[str] = []
    scheduler = Scheduler(
        provider,
        lambda i: (dispatched.append(i.opaque_id), FakeHandle())[1],
        max_concurrent_tasks=1,
        store=store,
    )

    # A finalized PR_READY run is finished, so the earlier (already completed)
    # run does not block a fresh dispatch of the same tracker item.
    report = scheduler.tick()

    assert dispatched == ["issue-2"]
    assert report.eligible_count == 1


# ---------------------------------------------------------------------------
# Recovery seeds persisted attempt/stall budgets and escalation
# ---------------------------------------------------------------------------


def test_recover_seeds_stall_budget_from_persisted_attempt_records(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    item = make_item("issue-3")
    work_item_id = deterministic_work_item_id(item)
    attempt = AttemptRecord(
        attempt_number=1,
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="mid",
        started_at=_dt(0),
        completed_at=_dt(1),
        outcome="failed",
        failure_reason="timed out",
    )
    store.save_run(
        FactoryRun(
            id="run-resumed",
            work_item_id=work_item_id,
            state=WorkflowState.IMPLEMENTING,
            attempt_records=[attempt],
        )
    )

    provider = FakeProvider([item])
    scheduler = Scheduler(
        provider,
        lambda i: FakeHandle(),
        max_concurrent_tasks=1,
        max_stall_retries=1,
        on_stall=lambda tracker_item, attempt_number: StallDecision.RETRY,
    )

    scheduler.recover(store, lambda run: ReconciliationAction.REQUEUE)

    # The persisted run already recorded 1 prior attempt; a *second* stall
    # for the same opaque id must now be treated as attempt 2, exceeding
    # max_stall_retries(1) and forcing escalation, exactly as if this
    # process had lived through both attempts itself.
    clock = FakeClock(_dt(0))
    scheduler.clock = clock
    handle = FakeHandle(done=False, activity_at=_dt(0))
    scheduler.dispatch = lambda i: handle
    scheduler.tick()

    clock.advance(9999)
    scheduler.stall_timeout_seconds = 30.0
    report = scheduler.tick()

    assert report.stalled == ("issue-3",)
    assert scheduler.is_escalated("issue-3") is True


def test_recover_escalates_matching_opaque_id_immediately(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    item = make_item("issue-4")
    work_item_id = deterministic_work_item_id(item)
    store.save_run(
        FactoryRun(id="run-needs-human", work_item_id=work_item_id, state=WorkflowState.REVIEWING)
    )

    provider = FakeProvider([item])
    dispatched: list[str] = []
    scheduler = Scheduler(
        provider,
        lambda i: (dispatched.append(i.opaque_id), FakeHandle())[1],
        max_concurrent_tasks=1,
    )

    scheduler.recover(store, lambda run: ReconciliationAction.NEEDS_HUMAN)

    assert scheduler.is_escalated("issue-4") is True

    report = scheduler.tick()
    assert dispatched == []
    assert report.eligible_count == 0


def test_recover_with_unrelated_work_item_id_does_not_affect_bookkeeping(tmp_path: Path) -> None:
    """Runs whose work_item_id was not produced by deterministic_work_item_id
    (e.g. a pre-existing manual run unrelated to any tracker) are still
    reported, but cannot be mapped back to an opaque id, so recovery makes
    no scheduler-side bookkeeping change for them."""
    store = FileRunStore(tmp_path / "data")
    store.save_run(
        FactoryRun(id="run-manual-only", work_item_id="WI-manual", state=WorkflowState.PLANNING)
    )

    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    records = scheduler.recover(store, lambda run: ReconciliationAction.NEEDS_HUMAN)

    assert len(records) == 1
    assert scheduler.is_escalated("WI-manual") is False
    assert scheduler.active_count == 0


# ---------------------------------------------------------------------------
# Reconciliation before polling starts (run_forever)
# ---------------------------------------------------------------------------


def test_run_forever_reconciles_persisted_runs_before_the_first_tick(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    store.save_run(
        FactoryRun(
            id="run-ambiguous", work_item_id="tracker-issue-5", state=WorkflowState.VERIFYING
        )
    )

    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    waiter = FakeWaiter(stop_after_ticks=1)

    call_order: list[str] = []
    recovered: list[list] = []

    def decide(run: FactoryRun) -> ReconciliationAction:
        call_order.append("recover")
        return ReconciliationAction.NEEDS_HUMAN

    def on_tick(_: TickReport) -> None:
        call_order.append("tick")

    scheduler.run_forever(
        waiter,
        poll_interval_seconds=60.0,
        store=store,
        recovery_decide=decide,
        on_recovery=recovered.append,
        on_tick=on_tick,
    )

    # Reconciliation of the persisted, non-terminal run happens before the
    # first (or any) tick -- not interleaved with, or after, polling.
    assert call_order == ["recover", "tick"]
    assert len(recovered) == 1
    assert recovered[0][0].run_id == "run-ambiguous"
    assert scheduler.is_escalated("issue-5") is True


def test_run_forever_without_recovery_arguments_skips_reconciliation() -> None:
    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())
    waiter = FakeWaiter(stop_after_ticks=1)

    # No store/recovery_decide supplied (and none configured on the
    # scheduler itself): run_forever must still work, simply skipping the
    # reconciliation step.
    scheduler.run_forever(waiter, poll_interval_seconds=1.0)

    assert waiter.wait_calls == 1


# ---------------------------------------------------------------------------
# Generic per-repository serialization primitive
# ---------------------------------------------------------------------------


def test_repository_lock_is_reused_per_path_and_distinct_across_paths() -> None:
    provider = FakeProvider([])
    scheduler = Scheduler(provider, lambda i: FakeHandle())

    lock_a1 = scheduler.repository_lock("/repo/a")
    lock_a2 = scheduler.repository_lock("/repo/a")
    lock_b = scheduler.repository_lock("/repo/b")

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b

    # It is a real, usable mutual-exclusion lock.
    assert lock_a1.acquire(blocking=False) is True
    assert lock_a1.acquire(blocking=False) is False
    lock_a1.release()


# ---------------------------------------------------------------------------
# Bounded daily dispatch-rate control (PLAN.md Phase 15 core safety
# foundation): max_runs_per_day, independent of max_concurrent_tasks.
# ---------------------------------------------------------------------------


def _seed_persisted_run(
    store: FileRunStore, *, run_id: str, work_item_id: str, created_at: datetime
) -> None:
    store.save_run(
        FactoryRun(
            id=run_id,
            work_item_id=work_item_id,
            state=WorkflowState.PR_READY,
            created_at=created_at,
            updated_at=created_at,
        )
    )


def test_max_runs_per_day_rejects_non_positive_values() -> None:
    provider = FakeProvider([])

    with pytest.raises(ValueError, match="max_runs_per_day must be > 0"):
        Scheduler(provider, lambda i: FakeHandle(), max_runs_per_day=0)

    with pytest.raises(ValueError, match="max_runs_per_day must be > 0"):
        Scheduler(provider, lambda i: FakeHandle(), max_runs_per_day=-1)


def test_max_runs_per_day_is_a_noop_without_a_store() -> None:
    """Matches ``_persisted_active_work_item_ids``: a store-optional feature
    has nothing to count against without one, so it never blocks dispatch."""
    item = make_item("issue-1")
    provider = FakeProvider([item])
    scheduler = Scheduler(
        provider, lambda i: FakeHandle(), max_concurrent_tasks=1, max_runs_per_day=1
    )

    report = scheduler.tick()

    assert report.dispatched == ("issue-1",)
    assert report.rate_limited is False


def test_tick_stops_claiming_new_work_once_the_daily_quota_is_reached(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    clock = FakeClock(_dt(0))
    _seed_persisted_run(
        store, run_id="run-today-1", work_item_id="WI-existing-1", created_at=_dt(0)
    )
    _seed_persisted_run(
        store, run_id="run-today-2", work_item_id="WI-existing-2", created_at=_dt(60)
    )

    item = make_item("issue-1")
    provider = FakeProvider([item])
    scheduler = Scheduler(
        provider,
        lambda i: FakeHandle(),
        max_concurrent_tasks=1,
        clock=clock,
        store=store,
        max_runs_per_day=2,
    )

    report = scheduler.tick()

    assert report.dispatched == ()
    assert report.rate_limited is True
    assert report.at_capacity is False
    assert report.candidates_fetched == 0
    assert report.eligible_count == 0


def test_tick_dispatches_only_up_to_the_remaining_daily_quota(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    clock = FakeClock(_dt(0))
    _seed_persisted_run(
        store, run_id="run-today-1", work_item_id="WI-existing-1", created_at=_dt(0)
    )

    item_a = make_item("issue-a", created_at=_dt(0))
    item_b = make_item("issue-b", created_at=_dt(1))
    provider = FakeProvider([item_a, item_b])
    dispatched: list[str] = []

    def dispatch(item: TrackerItem) -> FakeHandle:
        dispatched.append(item.opaque_id)
        return FakeHandle(done=False)

    scheduler = Scheduler(
        provider,
        dispatch,
        max_concurrent_tasks=2,
        clock=clock,
        store=store,
        max_runs_per_day=2,
    )

    report = scheduler.tick()

    # Quota allows exactly one more run today (2 - 1 already persisted),
    # even though max_concurrent_tasks(2) and both candidates are eligible.
    assert dispatched == ["issue-a"]
    assert report.dispatched == ("issue-a",)
    assert report.rate_limited is True


def test_daily_quota_resets_on_a_new_utc_day(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    clock = FakeClock(_dt(0))
    _seed_persisted_run(
        store, run_id="run-today-1", work_item_id="WI-existing-1", created_at=_dt(0)
    )

    item = make_item("issue-1")
    provider = FakeProvider([item])
    scheduler = Scheduler(
        provider,
        lambda i: FakeHandle(),
        max_concurrent_tasks=1,
        clock=clock,
        store=store,
        max_runs_per_day=1,
    )

    exhausted = scheduler.tick()
    assert exhausted.rate_limited is True
    assert exhausted.dispatched == ()

    # Advance the clock past midnight UTC into the next rolling day: the
    # quota is computed from *today's* persisted runs only, so yesterday's
    # persisted run no longer counts against it.
    clock.advance(24 * 60 * 60)
    reset = scheduler.tick()

    assert reset.dispatched == ("issue-1",)
    assert reset.rate_limited is False


def test_rate_limiting_never_mutates_existing_in_flight_work(tmp_path: Path) -> None:
    """Exhausting the daily quota must stop *new* claims only: reconciliation
    of already-active runs (completion/stall detection) still happens."""
    store = FileRunStore(tmp_path / "data")
    clock = FakeClock(_dt(0))

    active_item = make_item("already-active")
    new_item = make_item("brand-new")
    provider = FakeProvider([active_item])
    handle = FakeHandle(done=False)

    def dispatch(item: TrackerItem) -> FakeHandle:
        # Mirrors a real dispatch implementation persisting a FactoryRun as
        # part of starting work, which is what the quota counts against.
        store.save_run(
            FactoryRun(
                id=f"run-{item.opaque_id}",
                work_item_id=item.opaque_id,
                state=WorkflowState.IMPLEMENTING,
                created_at=clock(),
                updated_at=clock(),
            )
        )
        return handle

    scheduler = Scheduler(
        provider,
        dispatch,
        max_concurrent_tasks=2,
        clock=clock,
        store=store,
        max_runs_per_day=1,
    )

    first = scheduler.tick()
    assert first.dispatched == ("already-active",)
    assert scheduler.active_count == 1

    # Quota (1) is now fully consumed by the persisted run above. A
    # brand-new candidate must not be claimed, but the already-active run's
    # own completion must still be reconciled normally.
    provider.candidates = [new_item]
    handle.done = True
    second = scheduler.tick()

    assert second.completed == (("already-active", DispatchOutcome.SUCCEEDED),)
    assert second.rate_limited is True
    assert second.dispatched == ()
    assert second.dispatched == ()


def test_daily_quota_uses_the_clocks_utc_calendar_day_not_its_local_offset(
    tmp_path: Path,
) -> None:
    """Regression test: an injected clock is not guaranteed to return a
    UTC-zoned datetime. If ``_remaining_daily_quota`` used the clock's own
    (non-UTC) wall-clock date instead of converting to UTC first, a clock
    running in, say, +09:00 could compute a different calendar day than the
    UTC day ``FactoryRun.created_at`` (always UTC-normalized) is compared
    against, silently under- or over-counting today's persisted runs."""
    store = FileRunStore(tmp_path / "data")
    utc_instant = datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)
    # The exact same instant, expressed in a +09:00 offset: its *local* date
    # (2026-01-02) differs from its *UTC* date (2026-01-01).
    plus_nine = timezone(timedelta(hours=9))
    same_instant_plus_nine = utc_instant.astimezone(plus_nine)
    assert same_instant_plus_nine.date() != utc_instant.date()

    _seed_persisted_run(
        store, run_id="run-today-1", work_item_id="WI-existing-1", created_at=utc_instant
    )

    item = make_item("issue-1")
    provider = FakeProvider([item])
    scheduler = Scheduler(
        provider,
        lambda i: FakeHandle(),
        max_concurrent_tasks=1,
        clock=lambda: same_instant_plus_nine,
        store=store,
        max_runs_per_day=1,
    )

    report = scheduler.tick()

    # If the quota were computed from the clock's raw (+09:00) local date
    # instead of its UTC date, the seeded run (UTC 2026-01-01) would not
    # count as "today" and this tick would incorrectly dispatch.
    assert report.dispatched == ()
    assert report.rate_limited is True
