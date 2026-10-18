"""An agent that prints nothing stops the heartbeat, so the stall path fires (ADR-048)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from factory_testing import build_config

from software_agent_factory.agents import FakeAgentRuntime
from software_agent_factory.models import FactoryRun, RunLease, WorkflowState, utc_now
from software_agent_factory.run_watch import LiveSignals, heartbeat, live_activity
from software_agent_factory.store import FileRunStore
from software_agent_factory.transcripts import report_event_line
from software_agent_factory.workflow import WorkflowController


class CountingStore(FileRunStore):
    """Counts the heartbeats the heartbeat thread saves."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.saves = 0

    def save_run(self, run: FactoryRun) -> None:
        self.saves += 1
        super().save_run(run)


def _setup(data_dir: Path) -> tuple[WorkflowController, CountingStore, FactoryRun]:
    store = CountingStore(data_dir)
    controller = WorkflowController(build_config(data_dir), store, FakeAgentRuntime())
    controller.heartbeat_interval_seconds = 0.01
    run = FactoryRun(
        id="run-quiet",
        work_item_id="w",
        state=WorkflowState.IMPLEMENTING,
        lease=RunLease(host="h", pid=1, heartbeat_at=utc_now()),
    )
    return controller, store, run


def _beat_in_background(
    controller: WorkflowController, run: FactoryRun, live: LiveSignals
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    beat = threading.Thread(target=heartbeat, args=(controller, run, stop, 60, None, live))
    beat.start()
    return stop, beat


def test_a_silent_agent_gets_one_beat_and_then_none(factory_data_dir: Path) -> None:
    controller, store, run = _setup(factory_data_dir)

    with live_activity(run) as live:
        stop, beat = _beat_in_background(controller, run, live)
        time.sleep(0.3)
        stop.set()
        beat.join()

    assert store.saves == 1


def test_an_agent_that_prints_keeps_the_heartbeat_fresh(factory_data_dir: Path) -> None:
    controller, store, run = _setup(factory_data_dir)

    with live_activity(run) as live:
        stop, beat = _beat_in_background(controller, run, live)
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            report_event_line("still working")
            time.sleep(0.01)
        stop.set()
        beat.join()

    assert store.saves >= 3


def test_checks_without_live_signals_keep_the_plain_heartbeat(factory_data_dir: Path) -> None:
    controller, store, run = _setup(factory_data_dir)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()

    heartbeat(controller, run, stop, 60)

    assert store.saves >= 3
