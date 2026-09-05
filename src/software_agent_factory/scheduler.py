"""Generic local backlog scheduling and reconciliation (PLAN.md Phases 13-14).

Implements the coordination-layer control loop described in
``docs/symphony-alignment.md`` ("Reconciliation before dispatch", "Reserve
before dispatch", "Bounded concurrency", "Stall detection", "Recovery using
tracker + filesystem") without introducing a database, a web server, an
asyncio framework, or any network access:

```text
scheduler tick
     |
     v
reconcile active runs (poll handles, release completed, detect stalls)
     |
     v
discover candidates (TrackerProvider.fetch_candidates)
     |
     v
evaluate eligibility (dispatchable, no blockers, not already active)
     |
     v
sort/prioritize (priority, oldest creation, identifier)
     |
     v
stale-candidate revalidation (TrackerProvider.fetch_by_ids)
     |
     v
reserve -> dispatch (while capacity remains)
```

Per ``AGENTS.md`` ("One authoritative workflow controller" / "Agents do NOT
control ... workflow state"), :class:`Scheduler` never mutates a
``FactoryRun``'s ``WorkflowState`` itself. It only decides *when* and *in
what order* to start work and tracks *its own* bookkeeping (reservations,
stall attempts, escalations) in memory. Starting a unit of work, and any
``WorkflowController`` transitions that implies, is delegated entirely to an
injected ``dispatch`` callable -- typically a thin wrapper around
``WorkflowController.run`` -- so this module has no dependency on how a run
is actually executed.

``TrackerProvider`` is deliberately generic: this module has no knowledge of
GitHub, Jira, or any other concrete backlog. A GitHub Issues provider (the
Phase 13 default, eligibility ``label = agent-ready``) can be implemented
elsewhere and passed in without touching this file.

Recovery (startup inspection of persisted non-terminal runs) and stall
handling (timeout-based cancellation) are both surfaced through small
injected callbacks so the *policy* (requeue vs. ``NEEDS_HUMAN``, retry vs.
escalate, and any actual ``WorkflowState`` transition) stays with the
integrator/controller layer, per ADR-002 and ADR-004. This module never
deletes uncertain work; at most it excludes an opaque id from future
dispatch within its own process lifetime.

## Integration assumptions

- **Bounded concurrency at 1 or 2 only.** Per ``PLAN.md`` Phase 13/14 ("Do
  not jump directly to large concurrency"), ``max_concurrent_tasks`` is
  validated to be exactly ``1`` or ``2``. Larger values are out of scope for
  this module and must go through a deliberate, documented change instead
  of a configuration knob.

- **Deterministic tracked work item ids.** Use :func:`deterministic_work_item_id`
  (derived only from ``TrackerItem.opaque_id``) -- not a randomly generated
  id -- as the ``WorkItem.id``/``FactoryRun.work_item_id`` for any dispatch
  of a tracker-originated item. This is what lets the scheduler tell that a
  manually-run ``factory run`` and a concurrently polling daemon target the
  *same* underlying tracker item: on every tick, candidates whose
  deterministic id already has a persisted, non-terminal ``FactoryRun`` (via
  the optional ``store``) are treated as ineligible, so the daemon never
  double-dispatches work a human already started by hand (and vice versa).

- **Persisted attempt budgets are the resume source of truth.** Per
  ADR-003, ``WorkflowController`` already persists one bounded
  implementation/repair budget per run as ``FactoryRun.attempt_records``.
  This module does not reimplement that budget or reset it on restart:
  :meth:`Scheduler.recover` opportunistically seeds its own short-lived,
  in-process stall-retry counters from ``len(run.attempt_records)`` (via
  the inverse of ``deterministic_work_item_id``) so a scheduler restart
  cannot silently grant a stalled/retried tracker item a fresh, unbounded
  retry budget it would not have had pre-restart.

- **Lease/heartbeat liveness, not lock-file inspection.** While a run is
  active in this process, this module's only notion of "still alive" is a
  ``RunHandle``'s heartbeat -- ``last_activity_at()`` -- compared against
  ``stall_timeout_seconds`` using the injected ``clock``. This module never
  opens, parses, or reasons about any workspace lock file, and in
  particular never assumes that an on-disk lock file's mere presence (or
  absence) proves a run is stale or alive -- that would already be
  incorrect for today's ``O_EXCL`` lock files after an unclean process
  exit, and becomes actively misleading once workspace locking moves to
  ``flock`` (whose advisory locks are released automatically by the kernel
  on process death, so a lock *file* can legitimately persist on disk with
  nothing holding it). Determining a persisted, non-terminal run's true
  liveness on restart -- by whatever mechanism the execution layer actually
  uses to lock a workspace -- is entirely the job of the injected
  :data:`RecoveryCallback` passed to :meth:`Scheduler.recover`; this module
  never inspects that state itself and never deletes a run or workspace it
  is unsure about.

- **Global Git worktree operations are not scheduler-safe by default.**
  ``git worktree add``/``prune`` mutate repository-wide administrative
  state, not just one task's worktree. This module has no Git dependency
  and does not perform such operations, but when ``max_concurrent_tasks``
  is ``2`` a dispatch implementation that wraps per-repository worktree
  preparation must serialize that step itself. :meth:`Scheduler.repository_lock`
  offers a generic, git-agnostic, per-``repository_path`` mutual-exclusion
  primitive for exactly that purpose; it is optional and unused unless a
  dispatch implementation opts in.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .models import FactoryRun, UtcDateTime, WorkflowState, utc_now
from .store import FileRunStore
from .workflow import is_run_finished

logger = logging.getLogger(__name__)

__all__ = [
    "TrackerItem",
    "TrackerProvider",
    "DispatchOutcome",
    "RunHandle",
    "StallDecision",
    "ReconciliationAction",
    "RecoveryRecord",
    "TickReport",
    "Waiter",
    "Scheduler",
    "DEFAULT_PRIORITY_ORDER",
    "deterministic_work_item_id",
    "opaque_id_from_work_item_id",
]


# ---------------------------------------------------------------------------
# Tracker-facing model and provider boundary
# ---------------------------------------------------------------------------


class TrackerItem(BaseModel):
    """Normalized view of one candidate unit of work from any tracker.

    Concrete providers (GitHub Issues, Jira, a manual file-backed backlog,
    ...) are responsible for mapping their native representation onto this
    shape. The scheduler never depends on a specific tracker's API or
    payload format -- only on these fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    opaque_id: str = Field(
        min_length=1, description="Stable provider-internal id, e.g. a GitHub node id."
    )
    identifier: str = Field(
        min_length=1, description="Human-facing identifier, e.g. 'owner/repo#123'."
    )
    title: str = Field(min_length=1)
    description: str = ""
    state: str = Field(min_length=1, description="Provider-native state, e.g. 'open'.")
    labels: tuple[str, ...] = ()
    priority: str | None = None
    created_at: UtcDateTime
    blockers: tuple[str, ...] = Field(
        default=(), description="Identifiers of other items that must complete first."
    )
    dispatchable: bool = Field(
        default=True,
        description=(
            "The provider's own eligibility signal (e.g. required label present, "
            "not a draft/locked issue). The scheduler still applies its own "
            "generic eligibility rules on top of this."
        ),
    )
    repository_path: str = Field(
        min_length=1, description="Local repository path this item targets."
    )
    repository_ref: str | None = Field(
        default=None, description="Optional base ref/branch the work should target."
    )


class TrackerProvider(Protocol):
    """Boundary between the scheduler and a concrete backlog tracker.

    Implementations (e.g. a GitHub Issues adapter filtering on
    ``label = agent-ready``) are added by the integrator; this module only
    depends on this protocol.
    """

    def fetch_candidates(self) -> Sequence[TrackerItem]:
        """Return the current set of potentially eligible items."""
        ...

    def fetch_by_ids(self, opaque_ids: Sequence[str]) -> Sequence[TrackerItem]:
        """Return the freshest known state for the given opaque ids.

        Used for stale-candidate revalidation immediately before reserving
        and dispatching a candidate discovered by ``fetch_candidates``. Ids
        that no longer exist (closed, deleted, ...) are simply omitted from
        the result.
        """
        ...


_WORK_ITEM_ID_PREFIX = "tracker-"


def deterministic_work_item_id(item: TrackerItem) -> str:
    """Derive a stable ``WorkItem.id``/``FactoryRun.work_item_id`` from a
    tracker item's opaque id.

    Integrators MUST use this (never a randomly generated id, e.g. a fresh
    ``uuid4()``) when constructing the ``WorkItem`` for a dispatch of a
    ``TrackerItem`` -- see "Deterministic tracked work item ids" in this
    module's docstring for why that matters for manual/daemon duplicate
    prevention.
    """
    return f"{_WORK_ITEM_ID_PREFIX}{item.opaque_id}"


def opaque_id_from_work_item_id(work_item_id: str) -> str | None:
    """Best-effort inverse of :func:`deterministic_work_item_id`.

    Returns ``None`` for any ``work_item_id`` not produced by that function
    (e.g. a manually chosen id unrelated to a tracker item). Used only to
    opportunistically seed/exclude this scheduler's in-memory bookkeeping
    during :meth:`Scheduler.recover`; never required for correctness.
    """
    if not work_item_id.startswith(_WORK_ITEM_ID_PREFIX):
        return None
    return work_item_id[len(_WORK_ITEM_ID_PREFIX) :]


DEFAULT_PRIORITY_ORDER: tuple[str, ...] = ("P0", "P1", "P2", "P3")


def _priority_rank(priority: str | None, order: Sequence[str]) -> int:
    """Recognized priorities sort by their position in ``order``; ``None``
    or an unsupported value sorts after all recognized priorities."""
    if priority is None:
        return len(order)
    try:
        return order.index(priority)
    except ValueError:
        return len(order)


# ---------------------------------------------------------------------------
# Dispatch boundary
# ---------------------------------------------------------------------------


class DispatchOutcome(StrEnum):
    """The terminal result of one dispatched unit of work, as reported by a
    ``RunHandle``. Any of these releases the scheduler's reservation."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANCELLED = "CANCELLED"


class RunHandle(Protocol):
    """What an injected ``dispatch`` callable returns for one started unit
    of work.

    Keeps synchronous and thread-pool dispatch equally simple: a dispatch
    implementation that runs a ``WorkflowController`` to completion
    in-line before returning can wrap the already-known result in a handle
    whose ``is_done()`` is always ``True``. A dispatch implementation that
    submits work to its own ``concurrent.futures.ThreadPoolExecutor`` can
    instead wrap the returned ``Future`` (``is_done`` -> ``future.done()``,
    ``outcome`` derived from ``future.result()``). Either way, the scheduler
    only ever polls this protocol -- it never manages threads itself.
    """

    run_id: str
    """The underlying ``FactoryRun.id``, purely for structured logging and
    cross-referencing with persisted state; the scheduler treats it as an
    opaque string."""

    def is_done(self) -> bool: ...

    def outcome(self) -> DispatchOutcome:
        """Only valid once ``is_done()`` is ``True``."""
        ...

    def last_activity_at(self) -> datetime:
        """Most recent known activity timestamp, used for stall detection.
        Implementations with no finer-grained activity signal should return
        the dispatch start time and update it as their own work progresses."""
        ...

    def cancel(self) -> None:
        """Best-effort cooperative cancellation request. Must not raise."""
        ...


DispatchCallable = Callable[[TrackerItem], RunHandle]


class StallDecision(StrEnum):
    """Decision returned by an injected stall callback for one cancelled,
    stalled unit of work."""

    RETRY = "RETRY"
    ESCALATE = "ESCALATE"


StallCallback = Callable[[TrackerItem, int], StallDecision]
"""Given the stalled item and the 1-based number of stall attempts observed
for its opaque id so far, decide whether it should become eligible for
dispatch again (``RETRY``) or be excluded until a human intervenes
(``ESCALATE``)."""


# ---------------------------------------------------------------------------
# Startup recovery
# ---------------------------------------------------------------------------


class ReconciliationAction(StrEnum):
    """Decision returned by an injected recovery callback for one persisted,
    non-terminal ``FactoryRun`` discovered at startup."""

    REQUEUE = "REQUEUE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    LEAVE = "LEAVE"


RecoveryCallback = Callable[[FactoryRun], ReconciliationAction]
"""Given a persisted, non-terminal ``FactoryRun`` found on startup, decide
what should happen to it. The callback -- not this module -- is responsible
for any actual ``WorkflowController`` transition (e.g. into
``NEEDS_HUMAN``); this module never mutates a ``FactoryRun`` itself and
never deletes workspaces or run artifacts."""


@dataclass(frozen=True)
class RecoveryRecord:
    run_id: str
    work_item_id: str
    previous_state: WorkflowState
    action: ReconciliationAction


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


class Waiter(Protocol):
    """Injected stop/sleep abstraction so the polling loop never busy-waits
    and never needs a real sleep under test control.

    A ``threading.Event`` already satisfies this protocol (``wait(timeout)
    -> bool``, ``is_set() -> bool``), so real usage is simply::

        stop_event = threading.Event()
        scheduler.run_forever(stop_event, poll_interval_seconds=30.0)
        # elsewhere: stop_event.set()

    Tests can supply a fake object that reports elapsed "time" without
    ever calling ``time.sleep``.
    """

    def wait(self, timeout: float) -> bool:
        """Block for up to ``timeout`` seconds or until stop is requested;
        return ``True`` if stop was requested."""
        ...

    def is_set(self) -> bool: ...


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@dataclass
class _ActiveEntry:
    item: TrackerItem
    handle: RunHandle | None
    started_at: datetime


@dataclass(frozen=True)
class TickReport:
    """Summary of one ``Scheduler.tick()`` invocation, primarily useful for
    tests and structured logging."""

    candidates_fetched: int
    eligible_count: int
    dispatched: tuple[str, ...]
    completed: tuple[tuple[str, DispatchOutcome], ...]
    stalled: tuple[str, ...]
    skipped_stale: tuple[str, ...]
    at_capacity: bool
    rate_limited: bool = False


class Scheduler:
    """In-memory, single-process backlog scheduler.

    Owns:
    - bounded concurrency (``max_concurrent_tasks``, restricted to ``1`` or
      ``2`` -- see "Integration assumptions" above)
    - a bounded daily dispatch-rate safety control (``max_runs_per_day``,
      independent of ``max_concurrent_tasks``; requires ``store`` to count
      persisted runs against, see :meth:`_remaining_daily_quota`)
    - reservation-before-dispatch duplicate prevention, both in-memory
      (this process's own active work) and, when ``store`` is supplied,
      against persisted ``FactoryRun`` state so a manual invocation and
      this scheduler never dispatch the same tracker item twice
    - deterministic candidate ordering
    - stale-candidate revalidation immediately before dispatch
    - stall detection and cancellation, informed by persisted attempt
      history on restart (see :meth:`recover`)
    - a non-busy-waiting polling loop that reconciles persisted,
      non-terminal runs before it starts polling

    Does NOT own:
    - any ``WorkflowState`` transition (delegated to the injected
      ``dispatch`` callable and, on recovery, to the injected
      ``RecoveryCallback``)
    - concrete tracker access (delegated to ``TrackerProvider``)
    - workspace locking or liveness detection (delegated entirely to the
      injected ``RecoveryCallback``; see "Lease/heartbeat liveness" above)
    - persistence of scheduler state across process restarts (per
      ``docs/symphony-alignment.md``, live reservations are in-memory; only
      persisted ``FactoryRun``/workspace state survives a restart, and that
      is reconciled via :meth:`recover`)
    """

    #: PLAN.md Phase 13 starts at 1; Phase 14 raises this to 2. Larger
    #: concurrency is explicitly out of scope for this module.
    SUPPORTED_CONCURRENCY_LEVELS: tuple[int, ...] = (1, 2)

    def __init__(
        self,
        provider: TrackerProvider,
        dispatch: DispatchCallable,
        *,
        max_concurrent_tasks: int = 1,
        clock: Callable[[], datetime] = utc_now,
        priority_order: Sequence[str] = DEFAULT_PRIORITY_ORDER,
        stall_timeout_seconds: float | None = None,
        max_stall_retries: int = 1,
        on_stall: StallCallback | None = None,
        store: FileRunStore | None = None,
        max_runs_per_day: int | None = None,
    ) -> None:
        if max_concurrent_tasks not in self.SUPPORTED_CONCURRENCY_LEVELS:
            raise ValueError(
                "max_concurrent_tasks must be one of "
                f"{self.SUPPORTED_CONCURRENCY_LEVELS} (PLAN.md Phase 13/14); "
                f"got {max_concurrent_tasks!r}"
            )
        if stall_timeout_seconds is not None and stall_timeout_seconds <= 0:
            raise ValueError("stall_timeout_seconds must be > 0 when provided")
        if max_stall_retries < 0:
            raise ValueError("max_stall_retries must be >= 0")
        if max_runs_per_day is not None and max_runs_per_day <= 0:
            raise ValueError("max_runs_per_day must be > 0 when provided")

        self.provider = provider
        self.dispatch = dispatch
        self.max_concurrent_tasks = max_concurrent_tasks
        self.clock = clock
        self.priority_order = tuple(priority_order)
        self.stall_timeout_seconds = stall_timeout_seconds
        self.max_stall_retries = max_stall_retries
        self.on_stall = on_stall
        self.store = store
        """Optional ``FileRunStore`` used for three purposes: (1) each tick,
        excluding candidates that already have a persisted, non-terminal
        ``FactoryRun`` under their :func:`deterministic_work_item_id`
        (manual/daemon duplicate prevention), (2) as the default target
        of :meth:`recover` when called from :meth:`run_forever`, and (3)
        counting today's (UTC) persisted runs against ``max_runs_per_day``.
        Leave unset for pure in-memory usage (e.g. most tests)."""
        self.max_runs_per_day = max_runs_per_day
        """Bounded dispatch-rate safety control (PLAN.md Phase 15 core
        safety foundation), independent of ``max_concurrent_tasks``: the
        maximum number of runs this scheduler may claim within one rolling
        UTC day, counted from persisted ``FactoryRun.created_at`` timestamps
        via ``store``. ``None`` means unbounded. Requires ``store`` to have
        any effect -- without a store there is nothing to count against, so
        the limit is silently not enforced, matching the no-op behavior of
        the other ``store``-optional features above."""

        self._active: dict[str, _ActiveEntry] = {}
        self._escalated: set[str] = set()
        self._stall_attempts: dict[str, int] = {}
        self._repository_locks: dict[str, threading.Lock] = {}
        self._repository_locks_guard = threading.Lock()

    # -- introspection ----------------------------------------------------

    @property
    def active_count(self) -> int:
        return len(self._active)

    def is_active(self, opaque_id: str) -> bool:
        return opaque_id in self._active

    def is_escalated(self, opaque_id: str) -> bool:
        return opaque_id in self._escalated

    def repository_lock(self, repository_path: str) -> threading.Lock:
        """Return a process-wide mutual-exclusion lock keyed by
        ``repository_path``.

        Git worktree administration (``git worktree add``/``prune``) is a
        repository-global operation, not scoped to one task's worktree. A
        dispatch implementation that wraps such per-repository worktree
        preparation should serialize just that step when
        ``max_concurrent_tasks`` is ``2``, e.g.::

            with scheduler.repository_lock(item.repository_path):
                workspace.prepare()
            # release before running the (possibly long) agent work itself

        This module has no Git dependency and never acquires this lock
        itself; it only hands out (and reuses) one lock per repository path
        for dispatch implementations that opt in. Two dispatches against
        different repositories never contend.
        """
        with self._repository_locks_guard:
            lock = self._repository_locks.get(repository_path)
            if lock is None:
                lock = threading.Lock()
                self._repository_locks[repository_path] = lock
            return lock

    # -- recovery (startup, or any time before/between polling cycles) ----

    def recover(self, store: FileRunStore, decide: RecoveryCallback) -> list[RecoveryRecord]:
        """Inspect persisted runs and surface a decision for every
        non-terminal one via ``decide``.

        Per "Reconciliation before dispatch" (``docs/symphony-alignment.md``),
        this is intended to run before any polling begins -- :meth:`run_forever`
        calls it automatically as its first action when given ``store`` and
        ``recovery_decide`` -- but it is also safe to call standalone (e.g.
        at process startup, or periodically) since it never touches
        ``self._active`` and never deletes runs or workspaces. It only
        reports what was found so the integrator's own
        ``WorkflowController``/workspace-liveness inspection (see
        "Lease/heartbeat liveness" above) can act on it.

        For any non-terminal run whose ``work_item_id`` was produced by
        :func:`deterministic_work_item_id` (i.e. it can be traced back to a
        tracker opaque id), this also opportunistically updates this
        scheduler's own in-memory bookkeeping so a restart cannot bypass
        bounded retries or immediately re-dispatch something a human just
        escalated:

        - ``REQUEUE`` seeds this process's stall-retry counter for that
          opaque id from the persisted ``len(run.attempt_records)`` (the
          durable budget from ADR-003), rather than starting back at zero.
        - ``NEEDS_HUMAN`` excludes that opaque id from dispatch for the
          rest of this process's lifetime, mirroring whatever terminal
          transition the callback itself is expected to persist.
        - ``LEAVE`` makes no bookkeeping change.
        """
        records: list[RecoveryRecord] = []
        for run in store.list_runs():
            if is_run_finished(run):
                continue
            action = decide(run)
            records.append(
                RecoveryRecord(
                    run_id=run.id,
                    work_item_id=run.work_item_id,
                    previous_state=run.state,
                    action=action,
                )
            )

            opaque_id = opaque_id_from_work_item_id(run.work_item_id)
            if opaque_id is None:
                continue
            if action is ReconciliationAction.NEEDS_HUMAN:
                self._escalated.add(opaque_id)
            elif action is ReconciliationAction.REQUEUE:
                self._stall_attempts[opaque_id] = max(
                    self._stall_attempts.get(opaque_id, 0), len(run.attempt_records)
                )
        return records

    # -- eligibility / ordering ---------------------------------------------

    def _persisted_active_work_item_ids(self) -> frozenset[str]:
        """Deterministic ids of every persisted, unfinished ``FactoryRun``,
        computed once per tick (not once per candidate) to avoid repeated
        store scans. Empty when no ``store`` was configured.

        "Unfinished" is ``workflow.is_run_finished``: terminal states plus an
        explicitly finalized ``PR_READY`` (the completed endpoint of the
        manual, pull-request-disabled flow)."""
        if self.store is None:
            return frozenset()
        return frozenset(
            run.work_item_id for run in self.store.list_runs() if not is_run_finished(run)
        )

    def _remaining_daily_quota(self) -> int | None:
        """Remaining number of runs this scheduler may still claim within
        the current UTC calendar day, per ``max_runs_per_day`` (PLAN.md Phase
        15 core safety foundation). ``None`` means unbounded: either no
        ``max_runs_per_day`` was configured, or no ``store`` is available to
        count persisted runs against (mirrors ``_persisted_active_work_item_ids``:
        the feature is a no-op without a store). Never negative -- floored at
        ``0`` once the day's quota is exhausted. Counting only persisted
        runs means an in-flight dispatch this scheduler already reserved
        in-memory (but whose ``FactoryRun`` a slower dispatch implementation
        has not yet persisted) is not double-counted against tomorrow's
        quota either; the loop in :meth:`tick` compensates by also counting
        runs it dispatches within the same tick (see ``dispatched`` there)."""
        if self.max_runs_per_day is None or self.store is None:
            return None
        # ``clock()`` is injectable and not guaranteed to return a
        # UTC-zoned datetime (unlike ``FactoryRun.created_at``, which
        # ``UtcDateTime`` always normalizes to UTC) -- convert explicitly so
        # a non-UTC-offset clock can never compute the wrong calendar day.
        today = self.clock().astimezone(timezone.utc).date()
        created_today = sum(1 for run in self.store.list_runs() if run.created_at.date() == today)
        return max(0, self.max_runs_per_day - created_today)

    def _is_eligible(self, item: TrackerItem, persisted_active_ids: frozenset[str]) -> bool:
        if not item.dispatchable:
            return False
        if item.blockers:
            return False
        if item.opaque_id in self._active:
            return False
        if item.opaque_id in self._escalated:
            return False
        if deterministic_work_item_id(item) in persisted_active_ids:
            # A persisted, non-terminal FactoryRun already exists for this
            # tracker item -- most likely a manual `factory run` invocation
            # racing this scheduler's own polling. Refuse the duplicate.
            return False
        return True

    def _sort_key(self, item: TrackerItem) -> tuple[int, datetime, str]:
        rank = _priority_rank(item.priority, self.priority_order)
        return (rank, item.created_at, item.identifier)

    def _revalidate(
        self, item: TrackerItem, persisted_active_ids: frozenset[str]
    ) -> TrackerItem | None:
        fresh_items = self.provider.fetch_by_ids([item.opaque_id])
        matching = [candidate for candidate in fresh_items if candidate.opaque_id == item.opaque_id]
        if not matching:
            return None
        fresh = matching[0]
        if not self._is_eligible(fresh, persisted_active_ids):
            return None
        return fresh

    # -- reconciliation of already-active work -----------------------------

    def _reconcile_active(self) -> tuple[tuple[tuple[str, DispatchOutcome], ...], tuple[str, ...]]:
        completed: list[tuple[str, DispatchOutcome]] = []
        stalled: list[str] = []
        for opaque_id, entry in list(self._active.items()):
            if entry.handle is None:
                # Reserved earlier this tick but not yet dispatched; nothing
                # to reconcile yet (see _dispatch_eligible).
                continue
            if entry.handle.is_done():
                outcome = entry.handle.outcome()
                del self._active[opaque_id]
                completed.append((opaque_id, outcome))
                continue
            if self.stall_timeout_seconds is not None:
                elapsed = (self.clock() - entry.handle.last_activity_at()).total_seconds()
                if elapsed >= self.stall_timeout_seconds:
                    self._handle_stall(opaque_id, entry)
                    stalled.append(opaque_id)
        return tuple(completed), tuple(stalled)

    def _handle_stall(self, opaque_id: str, entry: _ActiveEntry) -> None:
        assert entry.handle is not None
        try:
            entry.handle.cancel()
        except Exception:  # noqa: BLE001 - cancellation must never break reconciliation
            logger.exception("cancel() raised while handling a stall for %s", opaque_id)

        attempts = self._stall_attempts.get(opaque_id, 0) + 1
        self._stall_attempts[opaque_id] = attempts

        decision = (
            self.on_stall(entry.item, attempts)
            if self.on_stall is not None
            else (StallDecision.ESCALATE)
        )
        if attempts > self.max_stall_retries:
            # Bounded retries are a factory policy, not a callback opinion:
            # per AGENTS.md ("Retries are bounded"), no injected callback
            # can grant unlimited retries.
            decision = StallDecision.ESCALATE

        del self._active[opaque_id]
        if decision is StallDecision.ESCALATE:
            self._escalated.add(opaque_id)

    # -- explicit cancellation / shutdown -----------------------------------

    def cancel(self, opaque_id: str) -> bool:
        """Request cancellation of one active unit of work and release its
        reservation immediately. Returns ``False`` if it was not active."""
        entry = self._active.get(opaque_id)
        if entry is None:
            return False
        if entry.handle is not None:
            try:
                entry.handle.cancel()
            except Exception:  # noqa: BLE001
                logger.exception("cancel() raised while cancelling %s", opaque_id)
        del self._active[opaque_id]
        return True

    def shutdown(self) -> tuple[str, ...]:
        """Cancel every active unit of work and release all reservations.
        Does not wait for handles to finish; a subsequent process restart
        relies on :meth:`recover` to reconcile persisted state."""
        cancelled = tuple(self._active)
        for opaque_id in cancelled:
            self.cancel(opaque_id)
        return cancelled

    # -- one scheduling cycle ------------------------------------------------

    def tick(self) -> TickReport:
        """Reconcile active runs, then discover/evaluate/sort/revalidate/
        reserve/dispatch new candidates while capacity remains.

        Reconciliation always happens first -- both of this scheduler's own
        in-memory active work (freeing capacity for new dispatches within
        this same call) and, via ``persisted_active_ids`` below, of
        whatever non-terminal ``FactoryRun``s currently exist in ``store``
        (so a manual invocation started since the last tick is honored
        immediately, not only at startup).
        """
        completed, stalled = self._reconcile_active()

        if len(self._active) >= self.max_concurrent_tasks:
            return TickReport(
                candidates_fetched=0,
                eligible_count=0,
                dispatched=(),
                completed=completed,
                stalled=stalled,
                skipped_stale=(),
                at_capacity=True,
            )

        remaining_quota = self._remaining_daily_quota()
        if remaining_quota == 0:
            # Daily dispatch-rate quota is exhausted (PLAN.md Phase 15 core
            # safety foundation): reconciliation above still ran, so
            # existing in-flight work is unaffected, but no new candidate
            # may be claimed until the next UTC calendar day.
            return TickReport(
                candidates_fetched=0,
                eligible_count=0,
                dispatched=(),
                completed=completed,
                stalled=stalled,
                skipped_stale=(),
                at_capacity=False,
                rate_limited=True,
            )

        persisted_active_ids = self._persisted_active_work_item_ids()

        candidates = list(self.provider.fetch_candidates())
        eligible = sorted(
            (c for c in candidates if self._is_eligible(c, persisted_active_ids)),
            key=self._sort_key,
        )

        dispatched: list[str] = []
        skipped_stale: list[str] = []
        rate_limited = False
        for item in eligible:
            if len(self._active) >= self.max_concurrent_tasks:
                break
            if remaining_quota is not None and len(dispatched) >= remaining_quota:
                # Enough candidates remained eligible this tick to exceed the
                # day's remaining quota; stop claiming new work without
                # touching what has already been reserved/dispatched above.
                rate_limited = True
                break

            fresh = self._revalidate(item, persisted_active_ids)
            if fresh is None:
                skipped_stale.append(item.opaque_id)
                continue

            # Reserve before dispatch (handle=None marks "reserved, not yet
            # dispatched") so nothing else in this process can pick the same
            # opaque id up again before dispatch() returns.
            self._active[fresh.opaque_id] = _ActiveEntry(
                item=fresh, handle=None, started_at=self.clock()
            )
            try:
                handle = self.dispatch(fresh)
            except Exception:  # noqa: BLE001 - dispatch is integrator code
                logger.exception("dispatch failed for %s", fresh.identifier)
                del self._active[fresh.opaque_id]
                continue

            self._active[fresh.opaque_id].handle = handle
            dispatched.append(fresh.opaque_id)

        return TickReport(
            candidates_fetched=len(candidates),
            eligible_count=len(eligible),
            dispatched=tuple(dispatched),
            completed=completed,
            stalled=stalled,
            skipped_stale=tuple(skipped_stale),
            at_capacity=False,
            rate_limited=rate_limited,
        )

    # -- polling daemon loop --------------------------------------------------

    def run_forever(
        self,
        stop_event: Waiter,
        poll_interval_seconds: float,
        *,
        recovery_decide: RecoveryCallback | None = None,
        store: FileRunStore | None = None,
        on_recovery: Callable[[list[RecoveryRecord]], None] | None = None,
        on_tick: Callable[[TickReport], None] | None = None,
    ) -> None:
        """Reconcile persisted, non-terminal runs, then run :meth:`tick`
        repeatedly until ``stop_event`` is set.

        Per "Reconciliation before dispatch"/"Polling first"
        (``docs/symphony-alignment.md``), reconciliation of whatever
        non-terminal work already exists happens once, before this loop
        starts polling at all: when both ``recovery_decide`` and a store
        (``store``, or the ``store`` given to the constructor) are
        available, :meth:`recover` is called exactly once up front and its
        records are handed to ``on_recovery`` if supplied. Every
        subsequent :meth:`tick` continues to revalidate against persisted
        state on its own (see ``_persisted_active_work_item_ids``), so a
        manual invocation started *after* this loop begins is still
        honored without a second recovery pass.

        Uses ``stop_event.wait(poll_interval_seconds)`` between ticks, so
        the loop never busy-waits: it blocks efficiently until either the
        interval elapses or the caller requests a stop.
        """
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be >= 0")

        resolved_store = store if store is not None else self.store
        if resolved_store is not None and recovery_decide is not None:
            records = self.recover(resolved_store, recovery_decide)
            if on_recovery is not None:
                on_recovery(records)

        while not stop_event.is_set():
            report = self.tick()
            if on_tick is not None:
                on_tick(report)
            if stop_event.wait(poll_interval_seconds):
                break
