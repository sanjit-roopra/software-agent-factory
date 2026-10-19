"""The ``factory start`` heartbeat file and how the dashboard reads it (ADR-075)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from software_agent_factory import service_heartbeat
from software_agent_factory.dashboard.view import service_view
from software_agent_factory.service_heartbeat import (
    HEARTBEAT_FILE,
    service_liveness,
    write_service_heartbeat,
)

TICK = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)


def test_a_tick_writes_the_pid_host_and_time(tmp_path: Path) -> None:
    write_service_heartbeat(tmp_path, TICK)

    data = json.loads((tmp_path / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert isinstance(data["host"], str)
    assert data["ticked_at"] == TICK.isoformat()


def test_the_service_runs_until_three_poll_intervals_pass(tmp_path: Path) -> None:
    write_service_heartbeat(tmp_path, TICK)

    fresh = service_liveness(tmp_path, 30, TICK + timedelta(seconds=90))
    stale = service_liveness(tmp_path, 30, TICK + timedelta(seconds=91))

    assert fresh == {"running": True, "ticked_at": TICK.isoformat()}
    assert stale == {"running": False, "ticked_at": TICK.isoformat()}


def test_a_heartbeat_from_the_future_does_not_count(tmp_path: Path) -> None:
    write_service_heartbeat(tmp_path, TICK)

    assert service_liveness(tmp_path, 30, TICK - timedelta(seconds=1))["running"] is False


@pytest.mark.parametrize(
    "content",
    [None, "not json", "[]", '{"ticked_at": 5}', '{"ticked_at": "2026-10-01T09:30:00"}'],
)
def test_a_missing_or_bad_heartbeat_reads_as_not_running(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / HEARTBEAT_FILE).write_text(content, encoding="utf-8")

    assert service_liveness(tmp_path, 30, TICK) == {"running": False, "ticked_at": None}


def test_a_failed_write_is_logged_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=service_heartbeat.__name__):
        write_service_heartbeat(blocker / "data", TICK)

    assert "could not write the service heartbeat" in caplog.text


def test_the_page_gets_only_running_and_a_valid_tick_time() -> None:
    ticked_at = TICK.isoformat()

    assert service_view({"service": {"running": True, "ticked_at": ticked_at, "pid": 1}}) == {
        "running": True,
        "ticked_at": ticked_at,
    }
    assert service_view({"service": {"running": False, "ticked_at": "2026-10-01"}}) == {
        "running": False,
        "ticked_at": None,
    }
    assert service_view({"service": {"running": True, "ticked_at": 5}})["ticked_at"] is None
    assert service_view({"service": {"running": "yes"}}) is None
    assert service_view({}) is None
    assert service_view(None) is None
