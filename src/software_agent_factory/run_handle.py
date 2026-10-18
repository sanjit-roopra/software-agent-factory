"""``RunHandle`` for a run the factory service runs on its thread pool."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future
from datetime import datetime

from .models import FactoryRun, WorkflowState
from .scheduler import DispatchOutcome
from .store import FileRunStore

logger = logging.getLogger(__name__)

__all__ = ["ThreadPoolRunHandle"]


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
        # Work that restarts an old run has no activity yet. The dispatch time counts then.
        # A lease heartbeat written during an agent call counts as activity too.
        heartbeat = run.lease.heartbeat_at if run.lease is not None else self._started_at
        return max(run.last_activity_at or run.updated_at, heartbeat, self._started_at)

    def cancel(self) -> None:
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
