"""Stop requests and steer notes for a run, from ``factory cancel``/``steer`` and the dashboard.

Neither changes a run: the controller that drives the run reads the file (ADR-047).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from .dashboard.snapshot import (
        SteerRequester,
        SteerRequestResult,
        StopRequester,
        StopRequestResult,
    )
    from .models import FactoryRun
    from .store import FileRunStore


def request_run_stop(
    store: FileRunStore, run_id: str, source: Literal["dashboard", "cli"], created_at: datetime
) -> StopRequestResult:
    """Store a stop request for a run that has not finished (ADR-047). Create-only.

    The controller that drives the run reads it, kills the agent call and halts the run.
    """
    from .lifecycle import is_run_finished
    from .models import RunStopRequest

    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError):
        return "run_missing"
    if is_run_finished(run):
        return "finished"
    request = RunStopRequest(run_id=run_id, source=source, created_at=created_at)
    try:
        created = store.create_stop_request(request)
    except FileNotFoundError:
        return "run_missing"
    return "created" if created else "exists"


def _can_take_steer_note(run: FactoryRun) -> bool:
    """Whether a later agent call of ``run`` can read a steer note.

    A working run can. A halted run can only when its halt can resume: a note on any other
    halt would wait for a call that never comes.
    """
    from .lifecycle import is_run_finished
    from .models import ResumeClassification, WorkflowState

    if not is_run_finished(run):
        return True
    return (
        run.state is WorkflowState.NEEDS_HUMAN
        and run.escalation is not None
        and run.escalation.resume_classification is not ResumeClassification.NOT_RESUMABLE
    )


def request_run_steer(
    store: FileRunStore,
    run_id: str,
    note: str,
    source: Literal["dashboard", "cli"],
    created_at: datetime,
) -> SteerRequestResult:
    """Store a steer note for a run that has not finished (ADR-047, ADR-051).

    ``note`` is already clean. A newer note replaces the waiting one. The controller adds
    the note to the next agent prompt.
    """
    from .models import RunSteerNote

    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError):
        return "run_missing"
    if not _can_take_steer_note(run):
        return "finished"
    request = RunSteerNote(run_id=run_id, source=source, note=note, created_at=created_at)
    try:
        replaced = store.replace_steer_note(request)
    except FileNotFoundError:
        return "run_missing"
    return "replaced" if replaced else "created"


def build_stop_requester(store: FileRunStore) -> StopRequester:
    """The requester of the dashboard's stop route."""

    def stop(run_id: str, created_at: datetime) -> StopRequestResult:
        return request_run_stop(store, run_id, "dashboard", created_at)

    return stop


def build_steer_requester(store: FileRunStore) -> SteerRequester:
    """The requester of the dashboard's steer route."""

    def steer(run_id: str, note: str, created_at: datetime) -> SteerRequestResult:
        return request_run_steer(store, run_id, note, "dashboard", created_at)

    return steer
