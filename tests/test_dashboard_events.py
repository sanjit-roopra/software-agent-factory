"""Tests for the ``/api/events`` change stream (ADR-077).

The stream runs on a real loopback server and reads a real runs directory in
``tmp_path``. The check interval is cut to a few milliseconds so a test does not wait.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from dashboard_js import JsCall, run_functions

from software_agent_factory.dashboard import (
    DashboardConfig,
    DashboardServer,
    create_server,
    events_route,
)
from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.events_route import (
    change_event,
    changed_runs,
    parse_event_id,
    run_versions_reader,
    stream_changes,
)
from software_agent_factory.dashboard.security import TOKEN_HEADER, cookie_name


def _snapshot(**_: object) -> dict[str, object]:
    return {"runs": [], "total": 0}


def _detail(_: str) -> None:
    return None


def _write_run(runs_dir: Path, run_id: str, mtime_ns: int) -> None:
    path = runs_dir / run_id / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    return path


@pytest.fixture
def server(runs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DashboardServer]:
    monkeypatch.setattr(events_route, "CHECK_SECONDS", 0.01)
    instance = create_server(
        DashboardConfig(
            snapshot_provider=_snapshot,
            run_detail_provider=_detail,
            run_versions_provider=run_versions_reader(runs_dir),
        )
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _open(
    server: DashboardServer, headers: Mapping[str, str] | None = None, *, cookie: bool = True
) -> http.client.HTTPResponse:
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    sent = {"Host": f"{host}:{port}", **(headers or {})}
    if cookie:
        sent["Cookie"] = f"{cookie_name(port)}={server.token}"
    conn.request("GET", "/api/events", headers=sent)
    return conn.getresponse()


def _read_block(response: http.client.HTTPResponse) -> dict[str, str]:
    """The fields of the next event block, read line by line up to the blank line."""
    fields: dict[str, str] = {}
    while True:
        line = response.readline().decode("utf-8").rstrip("\n")
        if not line:
            return fields
        name, _, value = line.partition(": ")
        fields[name] = value


def _next_event(response: http.client.HTTPResponse) -> dict[str, str]:
    while True:
        block = _read_block(response)
        if "event" in block:
            return block


def test_the_stream_names_a_run_whose_file_changed(server: DashboardServer, runs_dir: Path) -> None:
    _write_run(runs_dir, "run-a", 1_000)
    response = _open(server)
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
    assert response.getheader("Cache-Control") == "no-store"
    assert _read_block(response) == {"retry": str(events_route.RETRY_MS)}
    assert _read_block(response) == {"id": "1000"}

    _write_run(runs_dir, "run-b", 5_000)

    event = _next_event(response)
    assert event["event"] == "runs-changed"
    assert event["id"] == "5000"
    assert json.loads(event["data"]) == {"runs": ["run-b"]}
    response.close()


def test_a_resumed_stream_names_every_run_newer_than_the_last_event_id(
    server: DashboardServer, runs_dir: Path
) -> None:
    _write_run(runs_dir, "old", 1_000)
    _write_run(runs_dir, "new", 3_000)
    _write_run(runs_dir, "newest", 4_000)

    response = _open(server, {"Last-Event-ID": "2000"})
    _read_block(response)

    event = _next_event(response)
    assert event["id"] == "4000"
    assert json.loads(event["data"]) == {"runs": ["new", "newest"]}
    response.close()


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-token"),
        pytest.param({TOKEN_HEADER: "wrong"}, id="wrong-header"),
    ],
)
def test_the_stream_needs_the_token(server: DashboardServer, headers: dict[str, str]) -> None:
    response = _open(server, headers, cookie=False)

    assert response.status == 401
    assert json.loads(response.read()) == {"error": "unauthorized"}


def test_one_stream_past_the_limit_gets_503(server: DashboardServer) -> None:
    server.event_streams = threading.BoundedSemaphore(1)
    first = _open(server)
    assert first.status == 200

    second = _open(server)

    assert second.status == 503
    assert json.loads(second.read()) == {"error": "too many streams"}
    assert second.getheader("Content-Security-Policy") is not None
    first.close()


def test_a_server_without_a_versions_provider_answers_404() -> None:
    instance = create_server(
        DashboardConfig(snapshot_provider=_snapshot, run_detail_provider=_detail)
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        response = _open(instance)
        assert response.status == 404
        assert json.loads(response.read()) == {"error": "not found"}
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_head_sends_the_stream_headers_only(server: DashboardServer) -> None:
    host, port = server.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(
        "HEAD",
        "/api/events",
        headers={"Host": f"{host}:{port}", "Cookie": f"{cookie_name(port)}={server.token}"},
    )
    response = conn.getresponse()

    assert response.status == 200
    assert response.read() == b""


def test_the_reader_skips_entries_without_a_run_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_run(runs, "kept", 7_000)
    (runs / "empty").mkdir()

    assert run_versions_reader(runs)() == {"kept": 7_000}
    assert run_versions_reader(tmp_path / "missing")() == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", 42),
        (" 42 ", 42),
        (None, None),
        ("", None),
        ("-1", None),
        ("x", None),
        ("9" * 21, None),
    ],
)
def test_parse_event_id(raw: str | None, expected: int | None) -> None:
    assert parse_event_id(raw) == expected


def test_changed_runs_covers_new_changed_and_removed_runs() -> None:
    assert changed_runs(None, {"a": 5}, None) == []
    assert changed_runs({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 9, "d": 4}, None) == [
        "b",
        "c",
        "d",
    ]


def test_an_event_naming_too_many_runs_names_none() -> None:
    runs = [f"r{n}" for n in range(events_route.MAX_NAMED_RUNS + 1)]

    assert json.loads(change_event(1, runs).split("data: ")[1]) == {"runs": None}


def test_the_stream_keeps_its_id_and_ends_after_its_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_route, "CHECK_SECONDS", 0)
    monkeypatch.setattr(events_route, "STREAM_SECONDS", 25)
    monkeypatch.setattr(events_route, "KEEPALIVE_SECONDS", 15)
    versions: list[dict[str, int]] = [{"a": 9}, {"a": 9}, {}, {}]
    ticks = iter(range(0, 100, 10))
    written: list[str] = []

    stream_changes(
        lambda: versions.pop(0), written.append, threading.Event(), None, lambda: next(ticks)
    )

    assert written == [
        f"retry: {events_route.RETRY_MS}\n\n",
        "id: 9\n\n",
        'id: 9\nevent: runs-changed\ndata: {"runs": ["a"]}\n\n',
    ]


@pytest.mark.parametrize(
    ("view", "runs", "expected"),
    [
        ("runs", ["x"], True),
        ("run", ["x", "open"], True),
        ("run", ["x"], False),
        ("run", None, True),
    ],
)
def test_the_page_refreshes_only_for_a_change_its_view_shows(
    view: str, runs: Any, expected: bool
) -> None:
    calls = [JsCall("changeTouchesView", (view, "open", runs))]

    assert run_functions(dashboard_assets.APP_JS, calls)[0].value is expected


def test_the_page_polls_slowly_while_the_stream_is_open() -> None:
    held = {"streamOpen": True, "runDetail": {"outcome": {"kind": "active"}}}

    result = run_functions(dashboard_assets.APP_JS, [JsCall("pollDelay", ("run", held))])[0]

    assert result.value == 15000


def test_the_page_opens_the_stream_at_start() -> None:
    assert 'new globalThis.EventSource("/api/events")' in dashboard_assets.APP_JS
    assert "openChangeStream();" in dashboard_assets.APP_JS
