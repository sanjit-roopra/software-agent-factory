"""The heartbeat of ``factory start``: one small file the dashboard reads (ADR-075).

The service writes ``<data_dir>/service-heartbeat.json`` with its pid, host and tick time
at the start of every scheduler cycle. :func:`service_liveness` reads it back: the service
counts as running while the last tick is at most :data:`STALE_POLL_INTERVALS` poll
intervals old. A missing, unreadable or future-dated file reads as not running.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

from .atomic_write import write_text_atomic
from .model_base import utc_now

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = "service-heartbeat.json"
#: How many poll intervals may pass without a tick before the service counts as stopped.
STALE_POLL_INTERVALS = 3


def write_service_heartbeat(data_dir: Path, now: datetime | None = None) -> None:
    """Record one scheduler tick. A failed write is logged and never stops the service."""
    stamp = (now or utc_now()).isoformat()
    payload = {"pid": os.getpid(), "host": socket.gethostname(), "ticked_at": stamp}
    try:
        write_text_atomic(data_dir / HEARTBEAT_FILE, json.dumps(payload))
    except OSError as exc:
        logger.warning("could not write the service heartbeat: %s", exc)


def _ticked_at(data_dir: Path) -> datetime | None:
    try:
        data = json.loads((data_dir / HEARTBEAT_FILE).read_text(encoding="utf-8"))
        ticked_at = datetime.fromisoformat(data["ticked_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return ticked_at if ticked_at.tzinfo is not None else None


def service_liveness(
    data_dir: Path, poll_interval_seconds: float, now: datetime | None = None
) -> dict[str, object]:
    """``running`` and the last ``ticked_at`` (ISO text, or ``None`` when never read)."""
    ticked_at = _ticked_at(data_dir)
    if ticked_at is None:
        return {"running": False, "ticked_at": None}
    age = (now or utc_now()) - ticked_at
    limit = timedelta(seconds=poll_interval_seconds * STALE_POLL_INTERVALS)
    return {"running": timedelta(0) <= age <= limit, "ticked_at": ticked_at.isoformat()}
