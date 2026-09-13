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
from .config import FactoryConfig
from .github import GitHubClient, GitHubCommandError, resolve_github_token
from .github_tracker import GitHubIssueProvider
from .models import (
    EscalationStatus,
    FactoryRun,
    ResumeClassification,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .observability import log_run_event
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
from .store import FileRunStore
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
            state=WorkflowState.REFINING,
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
        """Deliver undelivered escalation notices and poll authorized replies."""
        if not self.config.escalation.enabled:
            return

        assert self.controller is not None
        client = self.github_client or self.controller._github
        if client is None:
            return

        from .escalation import poll_escalation_reply, reconcile_undelivered_notifications

        # 1. Reconcile undelivered escalation notices, bound to authoritative repository
        reconcile_undelivered_notifications(
            self.store,
            self.config,
            client,
            self.source_repo,
            max_runs=self.config.escalation.max_reply_polls_per_tick,
            expected_repository=self.github_repo,
        )

        active_count = len([h for h in self._handles.values() if not h.is_done()])
        runs = self.store.list_runs()
        remaining_quota = self.scheduler._remaining_daily_quota(runs)

        # 2. Reconcile durable resume-pending runs (NEEDS_HUMAN + REOPENED)
        # Crash-recovered REOPENED receipt is already a durable quota reservation.
        # Dispatching that pending reopen must not require another quota slot.
        for run in runs:
            if run.state is not WorkflowState.NEEDS_HUMAN:
                continue
            if run.escalation is None or run.escalation.status is not EscalationStatus.REOPENED:
                continue

            # Fail closed immediately if configuration changed after persistence
            if run.escalation.reopen_count > self.config.escalation.max_reopens:
                logger.warning(
                    "run %s reopen limit reduced below reopen_count (%s > %s); failing reopen",
                    run.id,
                    run.escalation.reopen_count,
                    self.config.escalation.max_reopens,
                )
                if self.controller is not None:
                    max_limit = self.config.escalation.max_reopens
                    self.controller._fail_reopen(
                        run,
                        f"run {run.id} exceeded maximum reopens ({max_limit})",
                        reason_code="ATTEMPT_BUDGET_EXHAUSTED",
                    )
                continue

            if active_count >= self.config.scheduler.max_concurrent_tasks:
                logger.debug("escalation resume dispatch skipped: at capacity")
                break

            handle = self._handles.get(run.id)
            if handle is not None and not handle.is_done():
                continue

            logger.info("reconciling and dispatching resume-pending run %s", run.id)
            self._dispatch_reopen(run.id, run.work_item_id)
            active_count += 1

        # 3. Poll reply commands for runs in NEEDS_HUMAN with NOTIFIED status
        # Reopened work must use the same executor, concurrency limit, and daily quota.
        if active_count >= self.config.scheduler.max_concurrent_tasks:
            logger.debug("escalation reply polling skipped: at capacity")
            return

        if remaining_quota is not None and remaining_quota <= 0:
            logger.debug("escalation reply polling skipped: daily run limit reached")
            return

        # Close reply cursors for non-resumable NOTIFIED runs
        for run in runs:
            if run.state is not WorkflowState.NEEDS_HUMAN or run.escalation is None:
                continue
            if run.escalation.status is EscalationStatus.NOTIFIED:
                if (
                    run.escalation.resume_classification is not ResumeClassification.RISK_APPROVAL
                    or run.escalation.reply_cursor == "closed"
                ):
                    if run.escalation.reply_cursor != "closed":
                        escalation = run.escalation.model_copy(
                            update={"reply_cursor": "closed", "updated_at": utc_now()}
                        )
                        self.store.save_run(run.model_copy(update={"escalation": escalation}))

        # Collect eligible runs for reply polling
        eligible_runs: list[FactoryRun] = [
            r
            for r in runs
            if r.state is WorkflowState.NEEDS_HUMAN
            and r.escalation is not None
            and r.escalation.status is EscalationStatus.NOTIFIED
            and r.escalation.resume_classification is ResumeClassification.RISK_APPROVAL
            and r.escalation.reply_cursor != "closed"
        ]

        if not eligible_runs:
            return

        # Deterministic sorting
        eligible_runs.sort(
            key=lambda r: (r.escalation.created_at if r.escalation is not None else utc_now(), r.id)
        )

        max_polls = self.config.escalation.max_reply_polls_per_tick
        if len(eligible_runs) <= max_polls:
            runs_to_poll = list(eligible_runs)
            self._reply_poll_cursor_id = eligible_runs[-1].id
            self._save_reply_poll_cursor(self._reply_poll_cursor_id)
        else:
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

        for run in runs_to_poll:
            if active_count >= self.config.scheduler.max_concurrent_tasks:
                break
            if remaining_quota is not None and remaining_quota <= 0:
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
                active_count += 1
                if remaining_quota is not None:
                    remaining_quota -= 1

    def run_once(self, drain_timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> TickReport:
        """One bounded cycle: recover, reconcile escalation, tick once, wait for dispatched work."""
        self.recover()
        self.reconcile_escalation()
        report = self.scheduler.tick()
        self._log_tick(report)
        self.drain(drain_timeout_seconds)
        return report

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
