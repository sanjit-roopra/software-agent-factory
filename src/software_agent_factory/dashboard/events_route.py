"""The ``GET /api/events`` change stream (ADR-077).

The page opens one Server-Sent Events stream. Once a second the stream reads the
modification time of each ``runs/<id>/run.json`` through a :data:`RunVersionsProvider`
and sends a ``runs-changed`` event that names the runs whose file changed, appeared or
went away. The event id is the newest modification time seen, in nanoseconds, so it
only grows within a stream. A browser that reconnects sends it back as
``Last-Event-ID``; the first check then names every run whose file is newer.

The stream sends nothing from a run but its id. It ends after :data:`STREAM_SECONDS`,
and the browser connects again. At most :data:`MAX_STREAMS` streams are open at once;
one more gets ``503``. The page polls when it has no stream. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Protocol

EVENTS_PATH = "/api/events"
#: Streams open at once, over every tab. Each one holds a server thread.
MAX_STREAMS = 8
#: How often a stream reads the run files.
CHECK_SECONDS = 1.0
#: A stream with no change sends its id this often, so a dead connection shows.
KEEPALIVE_SECONDS = 15.0
#: A stream ends after this long; the browser connects again with ``Last-Event-ID``.
STREAM_SECONDS = 300.0
#: How long the browser waits before it connects again.
RETRY_MS = 2000
#: An event names at most this many runs; more send ``null``, which means "any run".
MAX_NAMED_RUNS = 100

_EVENT_ID_PATTERN = re.compile(r"^\d{1,20}$", re.ASCII)

#: Run id to the modification time of its ``run.json`` in nanoseconds.
RunVersionsProvider = Callable[[], Mapping[str, int]]


class EventsServer(Protocol):
    """The server attributes the stream needs."""

    run_versions_provider: RunVersionsProvider | None
    event_streams: threading.BoundedSemaphore
    event_stop: threading.Event


def run_versions_reader(runs_dir: Path) -> RunVersionsProvider:
    """A provider that stats ``<runs_dir>/<id>/run.json`` for each run directory."""

    def read() -> dict[str, int]:
        versions: dict[str, int] = {}
        try:
            entries = list(os.scandir(runs_dir))
        except OSError:
            return versions
        for entry in entries:
            try:
                versions[entry.name] = os.stat(os.path.join(entry.path, "run.json")).st_mtime_ns
            except OSError:
                continue
        return versions

    return read


def parse_event_id(raw: str | None) -> int | None:
    """The cursor in a ``Last-Event-ID`` header, or ``None`` when it is absent or malformed."""
    if raw is None or not _EVENT_ID_PATTERN.match(raw.strip()):
        return None
    return int(raw.strip())


def changed_runs(
    previous: Mapping[str, int] | None, current: Mapping[str, int], since: int | None
) -> list[str]:
    """The runs to name: changed since ``previous``, or newer than ``since`` on the first check."""
    if previous is None:
        return sorted(
            run for run, version in current.items() if since is not None and version > since
        )
    return sorted(
        run for run in current.keys() | previous.keys() if current.get(run) != previous.get(run)
    )


def change_event(cursor: int, runs: list[str]) -> str:
    """One ``runs-changed`` event in the SSE wire format."""
    named = runs if len(runs) <= MAX_NAMED_RUNS else None
    return f"id: {cursor}\nevent: runs-changed\ndata: {json.dumps({'runs': named})}\n\n"


def stream_changes(
    provider: RunVersionsProvider,
    write: Callable[[str], None],
    stop: threading.Event,
    since: int | None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Write events until the stream times out, ``stop`` is set or ``write`` raises.

    A block with only an ``id`` line sets the browser's last event id without firing an
    event; it opens the stream and keeps it alive.
    """
    write(f"retry: {RETRY_MS}\n\n")
    previous: Mapping[str, int] | None = None
    cursor = since or 0
    started = clock()
    sent_at: float | None = None
    while True:
        current = provider()
        runs = changed_runs(previous, current, since)
        previous = current
        cursor = max(cursor, *current.values()) if current else cursor
        now = clock()
        if runs:
            write(change_event(cursor, runs))
            sent_at = now
        elif sent_at is None or now - sent_at >= KEEPALIVE_SECONDS:
            write(f"id: {cursor}\n\n")
            sent_at = now
        if now - started >= STREAM_SECONDS or stop.wait(CHECK_SECONDS):
            return


def serve_events(
    handler: BaseHTTPRequestHandler,
    server: EventsServer,
    headers: tuple[tuple[str, str], ...],
    send_body: bool,
) -> None:
    """Stream changes on ``handler``, or refuse with a JSON error.

    ``404`` when the server has no provider; ``503`` when :data:`MAX_STREAMS` are open.
    """
    provider = server.run_versions_provider
    if provider is None:
        _refuse(handler, HTTPStatus.NOT_FOUND, "not found", headers, send_body)
        return
    if not server.event_streams.acquire(blocking=False):
        _refuse(handler, HTTPStatus.SERVICE_UNAVAILABLE, "too many streams", headers, send_body)
        return
    try:
        _send_head(handler, HTTPStatus.OK, "text/event-stream; charset=utf-8", headers)
        if send_body:
            since = parse_event_id(handler.headers.get("Last-Event-ID"))
            stream_changes(provider, lambda text: _write(handler, text), server.event_stop, since)
    except OSError:
        pass  # the browser went away; it connects again on its own
    finally:
        server.event_streams.release()


def _refuse(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    error: str,
    headers: tuple[tuple[str, str], ...],
    send_body: bool,
) -> None:
    body = json.dumps({"error": error}).encode("utf-8")
    _send_head(
        handler,
        status,
        "application/json; charset=utf-8",
        (*headers, ("Content-Length", str(len(body)))),
    )
    if send_body:
        handler.wfile.write(body)


def _send_head(
    handler: BaseHTTPRequestHandler,
    status: HTTPStatus,
    content_type: str,
    headers: tuple[tuple[str, str], ...],
) -> None:
    """The status line and headers; the connection closes after this answer."""
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()


def _write(handler: BaseHTTPRequestHandler, text: str) -> None:
    handler.wfile.write(text.encode("utf-8"))
    handler.wfile.flush()
