"""Watch a run while its thread waits on slow work (ADR-048, ADR-049).

An agent call, the verify commands, the advisory checks and the CI wait can each
take many minutes. :func:`watched` keeps the run's lease heartbeat fresh during
that work, so the scheduler does not take a healthy run for a stalled one. It
also looks for a stop request, and kills the processes the run's thread started
when it finds one, so a stop does not wait for a long command to end.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .models import FactoryRun, utc_now
from .subprocess_utils import kill_thread_children, release_thread
from .transcripts import live_tool_calls

if TYPE_CHECKING:
    from .workflow import WorkflowController

logger = logging.getLogger(__name__)

#: How often a watched run writes a lease heartbeat, in seconds.
HEARTBEAT_INTERVAL_SECONDS = 30.0
#: How often a watched run looks for a stop request, in seconds.
STOP_POLL_INTERVAL_SECONDS = 2.0
#: How many of the latest tool calls the active call keeps for the live tail (dashboard X6).
LIVE_TOOL_CALLS = 20


@contextmanager
def live_activity(run: FactoryRun) -> Iterator[threading.Event]:
    """Keep the running agent's tool calls and output time on the active call (dashboard X6).

    Yields the event that is set when a new tool call is on the active call.
    """
    changed = threading.Event()
    with live_tool_calls(lambda call: note_tool_call(run, call, changed), lambda: note_output(run)):
        yield changed


def note_tool_call(run: FactoryRun, call: dict[str, str], changed: threading.Event) -> None:
    """Keep a tool call the running agent started on the active call.

    Only the active call is replaced, in one assignment; the heartbeat thread saves it.
    """
    active = run.active_invocation
    if active is None:
        return
    run.active_invocation = active.model_copy(
        update={
            "recent_tool_calls": [*active.recent_tool_calls, call][-LIVE_TOOL_CALLS:],
            "tool_call_count": active.tool_call_count + 1,
        }
    )
    changed.set()


def note_output(run: FactoryRun) -> None:
    """Stamp the time the running agent last printed a line; the next heartbeat saves it."""
    active = run.active_invocation
    if active is not None:
        run.active_invocation = active.model_copy(update={"last_output_at": utc_now()})


@contextmanager
def watched(
    controller: WorkflowController,
    run: FactoryRun,
    timeout_seconds: int,
    changed: threading.Event | None = None,
) -> Iterator[None]:
    """Write heartbeats on ``run`` and honor a stop request while the body runs.

    The heartbeat stops once the body outlives ``timeout_seconds``, so work that
    ignores its own timeout still looks stalled to the service. When a stop or a
    stall cancel killed the body's processes, the run halts or raises
    :class:`~software_agent_factory.workflow.RunCancelledError` after the body.
    """
    stop = threading.Event()
    killed = threading.Event()
    thread_id = threading.get_ident()
    beat = threading.Thread(
        target=heartbeat,
        args=(controller, run, stop, timeout_seconds, thread_id, changed, killed),
        name=f"heartbeat-{run.id}",
        daemon=True,
    )
    # Publish the thread before the cancel check: a stop that lands after the check
    # finds the thread, and any process it starts later is killed at once.
    controller._agent_threads[run.id] = thread_id
    try:
        controller._raise_if_cancelled(run)
        beat.start()
        try:
            yield
        finally:
            stop.set()
            beat.join()
        if killed.is_set() or run.id in controller._cancel_requests:
            controller._raise_if_cancelled(run)
    finally:
        controller._agent_threads.pop(run.id, None)
        release_thread(thread_id)


def heartbeat(
    controller: WorkflowController,
    run: FactoryRun,
    stop: threading.Event,
    timeout_seconds: int,
    watched_thread: int | None = None,
    changed: threading.Event | None = None,
    killed: threading.Event | None = None,
) -> None:
    """Persist a heartbeat on ``run`` until ``stop`` is set or ``timeout_seconds`` pass.

    Between beats it looks for a stop request, and kills the processes of
    ``watched_thread`` when it finds one. When ``changed`` is set, a new tool call is
    on the active call, and the run is saved at the next poll, not the next beat. The
    watched thread changes only ``run.active_invocation`` meanwhile.
    """
    interval = controller.heartbeat_interval_seconds
    started = last_beat = time.monotonic()
    poll = min(interval, STOP_POLL_INTERVAL_SECONDS)
    while not stop.wait(poll):
        now_monotonic = time.monotonic()
        if now_monotonic - started > timeout_seconds:
            return
        if watched_thread is not None:
            _kill_on_stop_request(controller, run, watched_thread, killed)
        if _take(changed) or now_monotonic - last_beat >= interval:
            last_beat = now_monotonic
            write_heartbeat(controller, run)


def _kill_on_stop_request(
    controller: WorkflowController,
    run: FactoryRun,
    watched_thread: int,
    killed: threading.Event | None,
) -> None:
    """Kill the processes of ``watched_thread`` when a stop request for ``run`` is on disk."""
    if controller._store.load_stop_request(run.id) is None:
        return
    kill_thread_children(watched_thread)
    if killed is not None:
        killed.set()


def _take(changed: threading.Event | None) -> bool:
    """Clear ``changed`` and return whether it was set."""
    if changed is None or not changed.is_set():
        return False
    changed.clear()
    return True


def write_heartbeat(controller: WorkflowController, run: FactoryRun) -> None:
    """Save ``run`` with a fresh lease heartbeat, or a fresh activity time without a lease."""
    now = utc_now()
    if run.lease is not None:
        run.lease = run.lease.model_copy(update={"heartbeat_at": now})
    else:
        run.last_activity_at = now
    try:
        controller._store.save_run(run)
    except OSError as exc:
        logger.warning("could not write the heartbeat of run %s: %s", run.id, exc)
