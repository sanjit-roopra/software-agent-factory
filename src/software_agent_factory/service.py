"""Concrete composition of the scheduler, tracker and workflow controller.

``PLAN.md`` Phases 13-14: ``factory start`` polls a GitHub Issues backlog and
dispatches eligible issues through the same :class:`WorkflowController` that
``factory run`` uses, with bounded concurrency (1 or 2).

Ownership stays exactly where the architecture puts it:

- :class:`~software_agent_factory.scheduler.Scheduler` decides *when* and *in
  what order* work starts. It never mutates a ``FactoryRun``.
- :class:`~software_agent_factory.workflow.WorkflowController` performs every
  state transition, including the conservative ``NEEDS_HUMAN`` recovery of
  runs abandoned by a previous process.
- :class:`~software_agent_factory.github_tracker.GitHubIssueProvider` is the
  only component that talks to the backlog.

Recovery is deliberately conservative (``ADR-004``): an abandoned non-terminal
run is escalated to ``NEEDS_HUMAN`` through the controller rather than
auto-resumed, so a restart never silently spends another paid attempt, and the
workspace plus every persisted artifact stay on disk for inspection.

Dispatch is likewise once-only: because the factory holds no write access to
the backlog and GitHub never withdraws an issue by itself, an item with any
persisted ``FactoryRun`` is excluded by the scheduler. Otherwise a finished
issue would be re-dispatched on the very next tick under a new run id with a
fresh, empty retry budget.

Two configured safety bounds are applied here rather than left implicit
(``PLAN.md`` Phase 15): ``scheduler.max_concurrent_tasks`` bounds how much
work runs at once, and ``scheduler.max_runs_per_day`` bounds how much work may
be *claimed* per UTC calendar day. Both are passed to the scheduler by this
composition root; the scheduler owns their enforcement.

Dispatch and completion are also emitted as structured, ``run_id``-tagged log
records (``observability.log_run_event``), so an installed launchd service
leaves a durable, bounded audit trail under ``<data_dir>/logs/factory.log``
without any workflow change.
"""

from __future__ import annotations

import bisect
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .agents import AgentRuntime
from .command_probe import ProbeLimits
from .config import FactoryConfig
from .github import GitHubClient, GitHubCommandError, resolve_github_token
from .github_tracker import GitHubIssueProvider
from .models import (
    REPLY_CURSOR_CLOSED,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    HaltReasonCode,
    ResumeClassification,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .observability import log_run_event
from .publishing import PullRequestPublisher
from .resume import awaits_human, unsettled_requests
from .resume_writes import ingest_dashboard_request
from .scheduler import (
    DispatchOutcome,
    ReconciliationAction,
    RecoveryRecord,
    Scheduler,
    TickReport,
    TrackerItem,
    TrackerProvider,
    Waiter,
    deterministic_work_item_id,
    opaque_id_from_work_item_id,
)
from .setup_run import SetupTrigger
from .store import FileRunStore
from .verification import DeterministicVerifier
from .workflow import WorkflowController, is_run_finished

logger = logging.getLogger(__name__)

__all__ = [
    "AlreadyRunFilter",
    "FactoryService",
    "ThreadPoolRunHandle",
    "build_work_item",
    "default_recovery_decision",
]

#: How long ``run_once`` waits for dispatched work to finish before giving up
#: and letting normal reconciliation handle it on a later tick.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 900.0


def build_work_item(item: TrackerItem) -> WorkItem:
    """Map a tracker item onto a ``WorkItem`` with a deterministic id.

    The deterministic id is what stops a manual ``factory run`` and the daemon
    from dispatching the same issue twice (see ``scheduler`` module docs).
    """
    return WorkItem(
        id=deterministic_work_item_id(item),
        external_id=item.identifier,
        source="GITHUB",
        title=item.title,
        description=item.description.strip() or item.title,
        labels=list(item.labels),
        priority=item.priority,
    )


class AlreadyRunFilter:
    """Hides tracker items this factory has already run.

    The generic :class:`~software_agent_factory.scheduler.Scheduler` only
    prevents *concurrent* duplicates: it assumes a tracker withdraws an item
    once work starts. GitHub Issues do not -- an issue keeps its ``agent-ready``
    label and stays open after a run finishes, and this factory deliberately
    holds no write access to the backlog.

    Without this filter, the tick after a run reached ``DONE``/``NEEDS_HUMAN``/
    ``FAILED`` would dispatch the very same issue again under a brand new
    ``FactoryRun`` with an empty ``attempt_records`` list -- an unbounded loop
    of paid work that also mints a fresh retry budget on every cycle, defeating
    ``ADR-003``/``ADR-007``.

    So an item is dispatchable at most once per data directory: any persisted
    ``FactoryRun`` for its deterministic work item id (finished or not) makes it
    ineligible. Re-running is an explicit operator action -- archive or remove
    the previous run, or invoke ``factory run --work-item-id`` by hand.
    """

    def __init__(self, provider: TrackerProvider, store: FileRunStore) -> None:
        self._provider = provider
        self._store = store

    def fetch_candidates(self) -> Sequence[TrackerItem]:
        return self._filter(self._provider.fetch_candidates())

    def fetch_by_ids(self, opaque_ids: Sequence[str]) -> Sequence[TrackerItem]:
        return self._filter(self._provider.fetch_by_ids(opaque_ids))

    def _filter(self, items: Sequence[TrackerItem]) -> list[TrackerItem]:
        known = {run.work_item_id for run in self._store.list_runs()}
        kept: list[TrackerItem] = []
        for item in items:
            if deterministic_work_item_id(item) in known:
                logger.debug("skipping %s: it already has a persisted run", item.identifier)
                continue
            kept.append(item)
        return kept


class ThreadPoolRunHandle:
    """``RunHandle`` over a ``concurrent.futures.Future``.

    ``last_activity_at`` is read back from the persisted run so stall
    detection uses the controller's real progress signal rather than a
    dispatch-time constant.
    """

    def __init__(self, run_id: str, store: FileRunStore, started_at: datetime) -> None:
        self.run_id = run_id
        self._store = store
        self._started_at = started_at
        self._future: Future[FactoryRun] | None = None
        self._cancelled = threading.Event()

    def attach(self, future: Future[FactoryRun]) -> None:
        self._future = future

    @property
    def future(self) -> Future[FactoryRun] | None:
        return self._future

    @property
    def cancel_requested(self) -> bool:
        return self._cancelled.is_set()

    def is_done(self) -> bool:
        return self._future is not None and self._future.done()

    def outcome(self) -> DispatchOutcome:
        if self._future is None:  # pragma: no cover - defensive
            return DispatchOutcome.CANCELLED
        if self._future.cancelled():
            return DispatchOutcome.CANCELLED
        error = self._future.exception()
        if error is not None:
            logger.error("run %s raised: %s", self.run_id, error)
            return DispatchOutcome.FAILED
        run = self._future.result()
        if run.state is WorkflowState.NEEDS_HUMAN:
            return DispatchOutcome.NEEDS_HUMAN
        if run.state is WorkflowState.FAILED:
            return DispatchOutcome.FAILED
        return DispatchOutcome.SUCCEEDED

    def last_activity_at(self) -> datetime:
        try:
            run = self._store.load_run(self.run_id)
        except (FileNotFoundError, ValueError):
            return self._started_at
        return run.last_activity_at or run.updated_at

    def cancel(self) -> None:
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()


def default_recovery_decision(run: FactoryRun) -> ReconciliationAction:
    """Escalate every abandoned non-terminal run to a human."""
    return ReconciliationAction.LEAVE if is_run_finished(run) else ReconciliationAction.NEEDS_HUMAN


@dataclass
class _CycleBudget:
    """Executor slots and daily run quota still free in one reconcile cycle.

    ``quota`` is ``None`` when the daily limit is unbounded.
    """

    slots: int
    quota: int | None

    def has_slot(self) -> bool:
        return self.slots > 0

    def has_quota(self) -> bool:
        return self.quota is None or self.quota > 0

    def has_room(self) -> bool:
        return self.has_slot() and self.has_quota()

    def take_slot(self) -> None:
        self.slots -= 1

    def take_quota(self) -> None:
        if self.quota is not None:
            self.quota -= 1


@dataclass
class FactoryService:
    """Wires ``GitHubIssueProvider`` -> ``Scheduler`` -> ``WorkflowController``."""

    config: FactoryConfig
    store: FileRunStore
    runtime: AgentRuntime
    source_repo: Path
    github_repo: str
    provider: TrackerProvider | None = None
    controller: WorkflowController | None = None
    github_client: GitHubClient | None = None
    setup_trigger: SetupTrigger | None = None

    def __post_init__(self) -> None:
        if not self.config.scheduler.enabled:
            raise ValueError(
                "scheduler.enabled must be true to start the backlog daemon; "
                "set scheduler.enabled in the factory configuration"
            )
        if self.controller is None:
            self.controller = WorkflowController(
                self.config,
                self.store,
                self.runtime,
                github_client=self.github_client,
            )
        if self.github_client is None:
            self.github_client = (
                self.controller._github
                if self.controller and self.controller._github
                else GitHubClient(token=resolve_github_token())
            )
        if self.provider is None:
            self.provider = GitHubIssueProvider(
                repository=self.github_repo,
                required_label=self.config.scheduler.required_label,
                local_repository_path=self.source_repo,
            )
        if (
            self.setup_trigger is None
            and self.config.setup.enabled
            and self.config.pull_request.enabled
            and self.config.repository.derive_commands
        ):
            self.setup_trigger = SetupTrigger(
                source_repo=self.source_repo,
                data_dir=self.config.data_dir,
                branch_prefix=self.config.repository.branch_prefix,
                limits=ProbeLimits.from_repository(self.config.repository),
                command_runner=DeterministicVerifier(),
                publisher=PullRequestPublisher(self.config),
            )
            logger.info(
                "setup check is on: the factory opens a pull request when %s misses "
                "development tools (setup.enabled)",
                self.source_repo,
            )
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.scheduler.max_concurrent_tasks,
            thread_name_prefix="factory-run",
        )
        self._completion_event = threading.Event()
        self._handles: dict[str, ThreadPoolRunHandle] = {}
        self._reply_poll_cursor_id: str | None = self._load_reply_poll_cursor()
        self.scheduler = Scheduler(
            self.provider,
            self._dispatch,
            max_concurrent_tasks=self.config.scheduler.max_concurrent_tasks,
            stall_timeout_seconds=float(self.config.scheduler.stall_timeout_seconds),
            store=self.store,
            max_runs_per_day=self.config.scheduler.max_runs_per_day,
            exclude_any_persisted_run=True,
        )

    @property
    def _reply_poll_cursor_file(self) -> Path:
        return self.config.data_dir / "reply_poll_cursor.json"

    def _load_reply_poll_cursor(self) -> str | None:
        try:
            if self._reply_poll_cursor_file.is_file():
                data = json.loads(self._reply_poll_cursor_file.read_text("utf-8"))
                if isinstance(data, dict):
                    return data.get("last_polled_run_id")
        except (OSError, ValueError):
            pass
        return None

    def _save_reply_poll_cursor(self, run_id: str | None) -> None:
        try:
            self.config.data_dir.mkdir(parents=True, exist_ok=True)
            self._reply_poll_cursor_file.write_text(
                json.dumps({"last_polled_run_id": run_id, "updated_at": utc_now().isoformat()}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.debug("could not persist reply poll cursor: %s", exc)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, item: TrackerItem) -> ThreadPoolRunHandle:
        work_item = build_work_item(item)
        run_id = f"run-{uuid4().hex}"
        handle = ThreadPoolRunHandle(run_id, self.store, utc_now())
        repository = Path(item.repository_path or self.source_repo)
        future = self._executor.submit(self._execute, work_item, repository, run_id)
        handle.attach(future)
        future.add_done_callback(lambda _future: self._completion_event.set())
        self._handles[run_id] = handle
        return handle

    def _execute(self, work_item: WorkItem, repository: Path, run_id: str) -> FactoryRun:
        assert self.controller is not None
        log_run_event(
            logger,
            f"dispatching {work_item.id} as run {run_id}",
            run_id=run_id,
            state=WorkflowState.CREATED,
        )
        run = self.controller.run(work_item, repository, run_id=run_id)
        log_run_event(
            logger,
            f"run {run_id} finished for {work_item.id}",
            run_id=run_id,
            state=run.state,
        )
        return run

    def _dispatch_reopen(self, run_id: str, work_item_id: str) -> ThreadPoolRunHandle:
        handle = ThreadPoolRunHandle(run_id, self.store, utc_now())
        future = self._executor.submit(self._execute_reopen, run_id, self.source_repo)
        handle.attach(future)
        future.add_done_callback(lambda _future: self._completion_event.set())
        self._handles[run_id] = handle
        opaque_id = opaque_id_from_work_item_id(work_item_id) or work_item_id
        self.scheduler.register_active_handle(opaque_id, handle)
        return handle

    def _execute_reopen(self, run_id: str, repository: Path) -> FactoryRun:
        assert self.controller is not None
        log_run_event(
            logger,
            f"reopening run {run_id}",
            run_id=run_id,
            state=WorkflowState.NEEDS_HUMAN,
        )
        run = self.controller.reopen(run_id, repository)
        log_run_event(
            logger,
            f"reopened run {run_id} finished",
            run_id=run_id,
            state=run.state,
        )
        return run

    # -- lifecycle --------------------------------------------------------

    def recover(self) -> list[RecoveryRecord]:
        """Reconcile persisted non-terminal runs before any dispatch."""
        assert self.controller is not None
        records = self.scheduler.recover(self.store, default_recovery_decision)
        for record in records:
            if record.action is not ReconciliationAction.NEEDS_HUMAN:
                continue
            try:
                run = self.store.load_run(record.run_id)
            except (FileNotFoundError, ValueError):  # pragma: no cover - defensive
                continue
            self.controller.recover_abandoned_run(
                run,
                "run was abandoned by a previous factory process; "
                "workspace and artifacts preserved for inspection",
            )
        return records

    def reconcile_escalation(self) -> None:
        """Deliver GitHub notices, reopen approved runs, then poll GitHub replies.

        Notices need no slot or quota, so they go first, as before. Then, in a fixed order
        that does not depend on GitHub: requests of runs that no longer wait are settled
        (no slot, no quota), runs already ``REOPENED`` are dispatched, and dashboard requests
        are ingested while a slot and quota last. Reply polling comes last and uses what is
        left. The notice and polling steps run only when escalation is enabled and a client
        exists. Capacity and quota only defer a dashboard request; they never make it stale.

        The runs are listed once, after the notices, and shared by every later step. A step
        that needs a run's current state loads that run again.
        """
        client = self._escalation_client()
        if client is not None:
            self._deliver_notices(client)

        runs = self.store.list_runs()
        self._settle_requests_of_runs_not_waiting(runs)
        budget = _CycleBudget(
            slots=self.config.scheduler.max_concurrent_tasks
            - len([h for h in self._handles.values() if not h.is_done()]),
            quota=self.scheduler._remaining_daily_quota(runs),
        )
        self._dispatch_reopened_runs(runs, budget)
        self._ingest_dashboard_requests(runs, budget)
        if client is not None:
            self._poll_replies(client, runs, budget)

    def _dispatch_reopened_runs(self, runs: Sequence[FactoryRun], budget: _CycleBudget) -> None:
        """Dispatch runs a reply already reopened (``NEEDS_HUMAN`` + ``REOPENED``).

        A crash-recovered ``REOPENED`` receipt is already a durable quota reservation, so
        dispatching it takes a slot and no quota.
        """
        for run in runs:
            if run.state is not WorkflowState.NEEDS_HUMAN:
                continue
            if run.escalation is None or run.escalation.status is not EscalationStatus.REOPENED:
                continue
            if self._fail_reopen_over_limit(run):
                continue

            if not budget.has_slot():
                logger.debug("escalation resume dispatch skipped: at capacity")
                break

            handle = self._handles.get(run.id)
            if handle is not None and not handle.is_done():
                continue

            logger.info("reconciling and dispatching resume-pending run %s", run.id)
            self._dispatch_reopen(run.id, run.work_item_id)
            budget.take_slot()

    def _fail_reopen_over_limit(self, run: FactoryRun) -> bool:
        """Fail closed when configuration changed after persistence.

        True if the run is handled and must not be dispatched: it failed, or it changed or
        vanished since the cycle listed it. Failing writes the whole run, so it starts from the
        stored run, never the listed snapshot.
        """
        escalation = run.escalation
        assert escalation is not None  # the caller checked
        max_limit = self.config.escalation.max_reopens
        if escalation.reopen_count <= max_limit:
            return False
        try:
            current = self.store.load_run(run.id)
        except FileNotFoundError:
            return True
        current_escalation = current.escalation
        if (
            current.state is not WorkflowState.NEEDS_HUMAN
            or current_escalation is None
            or current_escalation.status is not EscalationStatus.REOPENED
        ):
            return True
        logger.warning(
            "run %s reopen limit reduced below reopen_count (%s > %s); failing reopen",
            run.id,
            current_escalation.reopen_count,
            max_limit,
        )
        if self.controller is not None:
            self.controller._fail_reopen(
                current,
                f"run {run.id} exceeded maximum reopens ({max_limit})",
                reason_code=HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED,
            )
        return True

    def _pending_requests(self, run: FactoryRun) -> list[DashboardResumeRequest]:
        """The pending dashboard requests for the current episode of ``run``."""
        escalation = run.escalation
        if escalation is None:
            return []
        return [
            request
            for request in self.store.list_dashboard_requests(run.id, escalation.episode_id)
            if request.status == "pending"
        ]

    def _settle_requests_of_runs_not_waiting(self, runs: Sequence[FactoryRun]) -> None:
        """Mark the pending requests of runs that no longer wait for a human as stale.

        A GitHub reply, a failure or an expiry can end the wait after a request was made, or
        just before it. Ingest marks such a request stale as ``state_changed`` (``context_changed``
        for another context of the episode) and leaves one this run accepted itself pending.
        Requests the run already accepted are dropped first, so a finished run is not read
        again every cycle. This needs no slot and no quota, and reads only the
        requests of each escalated run's current episode: one directory listing per run, and
        a file is parsed only when it exists. ``list_runs`` already reads every run file in
        the cycle, which costs more.
        """
        for run in runs:
            if run.escalation is None or awaits_human(run):
                continue
            pending = unsettled_requests(run, self._pending_requests(run))
            if pending:
                ingest_dashboard_request(run, self.store, self.config, utc_now(), requests=pending)

    def _ingest_dashboard_requests(self, runs: Sequence[FactoryRun], budget: _CycleBudget) -> None:
        """Reopen runs whose operator approved or answered on the dashboard.

        Stops when a slot or the quota is used up, leaving the remaining requests unread
        for the first cycle with room. Only runs that wait for a human and have a pending
        request for their current episode are read, one directory listing per waiting run.
        """
        for run, pending in self._runs_with_pending_dashboard_request(runs):
            if not budget.has_room():
                logger.debug("dashboard request ingest deferred: no free slot or daily quota")
                return
            receipt = ingest_dashboard_request(
                run, self.store, self.config, utc_now(), requests=pending
            )
            if receipt is None:
                continue
            logger.info("accepted dashboard request for run %s", run.id)
            self._dispatch_reopen(run.id, run.work_item_id)
            budget.take_slot()
            budget.take_quota()

    def _runs_with_pending_dashboard_request(
        self, runs: Sequence[FactoryRun]
    ) -> list[tuple[FactoryRun, list[DashboardResumeRequest]]]:
        """Waiting runs with their pending requests for the current episode, oldest first.

        The requests are listed once here and handed to ingest, which does not list again.
        """
        found: list[tuple[datetime, FactoryRun, list[DashboardResumeRequest]]] = []
        for run in runs:
            escalation = run.escalation
            if escalation is None or not awaits_human(run):
                continue
            pending = self._pending_requests(run)
            if pending:
                found.append((escalation.created_at, run, pending))
        found.sort(key=lambda item: (item[0], item[1].id))
        return [(run, pending) for _, run, pending in found]

    def _escalation_client(self) -> GitHubClient | None:
        """The GitHub client for notices and replies, or ``None`` when they are off."""
        if not self.config.escalation.enabled:
            return None
        assert self.controller is not None
        return self.github_client or self.controller._github

    def _deliver_notices(self, client: GitHubClient) -> None:
        """Post undelivered escalation notices, bound to the authoritative repository."""
        from .escalation import reconcile_undelivered_notifications

        reconcile_undelivered_notifications(
            self.store,
            self.config,
            client,
            self.source_repo,
            max_runs=self.config.escalation.max_reply_polls_per_tick,
            expected_repository=self.github_repo,
        )

    def _poll_replies(
        self, client: GitHubClient, runs: Sequence[FactoryRun], budget: _CycleBudget
    ) -> None:
        """Poll authorized replies of notified runs, with the slots and quota left."""
        from .escalation import poll_escalation_reply

        # Reopened work uses the same executor, concurrency limit, and daily quota.
        if not budget.has_slot():
            logger.debug("escalation reply polling skipped: at capacity")
            return

        if not budget.has_quota():
            logger.debug("escalation reply polling skipped: daily run limit reached")
            return

        # The dashboard requests and failed reopens changed what runs wait for: load those again.
        runs = self._reloaded_notified_runs(runs)
        self._close_unpollable_reply_cursors(runs)

        for run in self._next_runs_to_poll(runs):
            if not budget.has_room():
                break

            receipt = poll_escalation_reply(
                run,
                self.store,
                self.config,
                client,
                self.source_repo,
            )
            if receipt is not None:
                logger.info(
                    "accepted authorized reply for run %s from @%s",
                    run.id,
                    receipt.user_login,
                )
                self._dispatch_reopen(run.id, run.work_item_id)
                budget.take_slot()
                budget.take_quota()

    def _reloaded_notified_runs(self, runs: Sequence[FactoryRun]) -> list[FactoryRun]:
        """``runs`` with each notified, waiting run read again, as earlier steps may change it.

        A run deleted since the listing is left out.
        """
        reloaded: list[FactoryRun] = []
        for run in runs:
            waiting = (
                run.state is WorkflowState.NEEDS_HUMAN
                and run.escalation is not None
                and run.escalation.status is EscalationStatus.NOTIFIED
            )
            if not waiting:
                reloaded.append(run)
                continue
            try:
                reloaded.append(self.store.load_run(run.id))
            except FileNotFoundError:
                logger.debug("run %s was deleted after the cycle listed it; skipped", run.id)
        return reloaded

    def _close_unpollable_reply_cursors(self, runs: Sequence[FactoryRun]) -> None:
        """Close the reply cursor of notified runs that no reply can resume."""
        for run in runs:
            if run.state is not WorkflowState.NEEDS_HUMAN or run.escalation is None:
                continue
            if (
                run.escalation.status is EscalationStatus.NOTIFIED
                and run.escalation.resume_classification
                not in {
                    ResumeClassification.RISK_APPROVAL,
                    ResumeClassification.PLAN_DECISION,
                }
                and run.escalation.reply_cursor != REPLY_CURSOR_CLOSED
            ):
                escalation = run.escalation.advanced_cursor(REPLY_CURSOR_CLOSED, utc_now())
                self.store.save_run(run.model_copy(update={"escalation": escalation}))

    def _next_runs_to_poll(self, runs: Sequence[FactoryRun]) -> list[FactoryRun]:
        """Pick this cycle's runs for reply polling, rotating so none starves."""
        eligible_runs: list[FactoryRun] = [
            r
            for r in runs
            if r.state is WorkflowState.NEEDS_HUMAN
            and r.escalation is not None
            and r.escalation.status is EscalationStatus.NOTIFIED
            and r.escalation.resume_classification
            in {
                ResumeClassification.RISK_APPROVAL,
                ResumeClassification.PLAN_DECISION,
            }
            and r.escalation.reply_cursor != REPLY_CURSOR_CLOSED
        ]

        if not eligible_runs:
            return []

        # Deterministic sorting
        eligible_runs.sort(
            key=lambda r: (r.escalation.created_at if r.escalation is not None else utc_now(), r.id)
        )

        max_polls = self.config.escalation.max_reply_polls_per_tick
        if len(eligible_runs) <= max_polls:
            runs_to_poll = list(eligible_runs)
            self._reply_poll_cursor_id = eligible_runs[-1].id
            self._save_reply_poll_cursor(self._reply_poll_cursor_id)
            return runs_to_poll

        # Deterministic rotating cursor to prevent starvation
        start_idx = 0
        if self._reply_poll_cursor_id is not None:
            run_ids = [r.id for r in eligible_runs]
            if self._reply_poll_cursor_id in run_ids:
                start_idx = (run_ids.index(self._reply_poll_cursor_id) + 1) % len(eligible_runs)
            else:
                start_idx = bisect.bisect_right(run_ids, self._reply_poll_cursor_id) % len(
                    eligible_runs
                )

        runs_to_poll = [
            eligible_runs[(start_idx + i) % len(eligible_runs)] for i in range(max_polls)
        ]
        self._reply_poll_cursor_id = runs_to_poll[-1].id
        self._save_reply_poll_cursor(self._reply_poll_cursor_id)
        return runs_to_poll

    def run_once(self, drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> TickReport:
        """One bounded cycle: recover, reconcile escalation, tick once, wait for dispatched work."""
        self.recover()
        self.reconcile_escalation()
        self.check_setup()
        report = self.scheduler.tick()
        self._log_tick(report)
        self.drain(drain_timeout_seconds)
        return report

    def check_setup(self) -> None:
        """Open a setup pull request when the repository misses tools (ADR-034).

        A setup problem never stops the backlog: it is logged and the tick goes on.
        """
        if self.setup_trigger is None:
            return
        try:
            state = self.setup_trigger.tick()
        except Exception:  # noqa: BLE001 - setup is advisory and must not stop the service
            logger.exception("setup check failed")
            return
        if state is not None:
            logger.info(
                "setup check at %s: %s",
                state.head_commit[:12],
                state.note or state.pull_request_url or "nothing to add",
            )

    def _log_tick(self, report: TickReport) -> None:
        """Emit one structured record per tick, so a rate-limited or
        at-capacity cycle is visible in the on-disk log and not only in the
        foreground CLI output."""
        logger.info(
            "tick: %d candidate(s), %d eligible, dispatched %s%s%s",
            report.candidates_fetched,
            report.eligible_count,
            ", ".join(report.dispatched) or "(none)",
            "; at capacity" if report.at_capacity else "",
            "; daily run limit reached" if report.rate_limited else "",
        )

    def drain(self, timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> None:
        """Block until every dispatched run finishes (or the timeout lapses)."""
        for handle in list(self._handles.values()):
            future = handle.future
            if future is None:
                continue
            try:
                future.result(timeout=timeout_seconds)
            except TimeoutError:  # pragma: no cover - slow-path safety net
                logger.warning("timed out waiting for run %s", handle.run_id)
            except Exception:  # noqa: BLE001 - reported through DispatchOutcome
                logger.exception("run %s failed", handle.run_id)

    def run_forever(self, stop_event: Waiter) -> None:
        """Poll until ``stop_event`` is set, reconciling before the first tick."""
        try:
            self.recover()
            poll_interval = float(self.config.scheduler.poll_interval_seconds)
            while not stop_event.is_set():
                # Clear before the reconciliation snapshot. A completion that
                # races with tick() sets the event again and causes an
                # immediate follow-up tick instead of being lost.
                self._completion_event.clear()
                try:
                    self.reconcile_escalation()
                    self.check_setup()
                    report = self.scheduler.tick()
                except GitHubCommandError:
                    logger.exception(
                        "GitHub backlog polling failed; retrying after %.1f seconds",
                        poll_interval,
                    )
                else:
                    self._log_tick(report)
                if self._wait_for_stop_or_completion(stop_event, poll_interval):
                    break
        finally:
            self.shutdown()

    def _wait_for_stop_or_completion(
        self,
        stop_event: Waiter,
        timeout_seconds: float,
    ) -> bool:
        """Return true on stop, or false when work completes or polling is due."""
        if timeout_seconds <= 0:
            return stop_event.is_set()
        if not isinstance(stop_event, threading.Event):
            return stop_event.wait(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._completion_event.wait(min(remaining, 0.25)):
                return stop_event.is_set()
        return True

    def shutdown(self) -> None:
        """Cancel active work and shut the executor down cleanly."""
        self.scheduler.shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)
