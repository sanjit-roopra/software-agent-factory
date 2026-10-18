"""The call transcript route of the dashboard (ADR-047, ADR-050)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dashboard_js import JsCall, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard import transcript_route
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, DashboardServer, create_server
from software_agent_factory.dashboard.transcript_route import sanitize_transcript
from software_agent_factory.transcripts import (
    TRANSCRIPT_CUT_MARKER,
    call_transcript_reader,
    event_preview,
    transcript_name,
)

GH_SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

TRANSCRIPT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I will look."},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                    ]
                },
            }
        ),
        json.dumps({"type": "assistant.message", "data": {"content": f"key {GH_SECRET}"}}),
        json.dumps({"type": "result", "result": "Done."}),
        "plain text line",
        "",
    ]
)


def test_event_previews_name_tools_and_text() -> None:
    lines = TRANSCRIPT.splitlines()

    previews = [event_preview(line) for line in lines if line]

    assert previews[0] == {"type": "system", "text": ""}
    assert previews[1] == {"type": "assistant", "text": "Bash ls -la"}
    assert previews[3] == {"type": "result", "text": "Done."}
    assert previews[4] == {"type": "text", "text": "plain text line"}
    assert event_preview("[1, 2]") == {"type": "text", "text": "[1, 2]"}
    assert event_preview('{"type": 3, "text": "x"}') == {"type": "event", "text": "x"}
    assert event_preview('{"message": {"content": [{"text": "a"}, {"text": "b"}]}}')["text"] == (
        "a b"
    )
    assert len(event_preview("x" * 1000)["text"]) == 400


def test_sanitize_redacts_and_keeps_the_tail() -> None:
    payload = sanitize_transcript(TRANSCRIPT)

    assert GH_SECRET not in json.dumps(payload)
    assert len(payload["events"]) == 5
    assert payload["tail"].endswith("plain text line\n")
    assert payload["truncated"] is False


def test_sanitize_caps_events_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    many = "".join(f'{{"type": "e{n}"}}\n' for n in range(transcript_route.MAX_EVENTS + 5))
    events = sanitize_transcript(many)
    assert events["truncated"] is True
    assert len(events["events"]) == transcript_route.MAX_EVENTS
    assert events["events"][-1]["type"] == f"e{transcript_route.MAX_EVENTS + 4}"

    monkeypatch.setattr(transcript_route, "MAX_TRANSCRIPT_CHARS", 30)
    big = sanitize_transcript("a" * 40 + "\n" + "last line\n")
    assert big["truncated"] is True
    assert [event["text"] for event in big["events"]] == ["last line"]
    one_line = sanitize_transcript("b" * 40)
    assert one_line["tail"] == "b" * 30
    assert sanitize_transcript("x\n" + TRANSCRIPT_CUT_MARKER)["truncated"] is True


# -- route -------------------------------------------------------------------


def _snapshot(*, limit: int, offset: int) -> dict[str, Any]:
    return {"runs": [], "page": {"limit": limit, "offset": offset}}


@pytest.fixture
def start() -> Iterator[Any]:
    servers: list[DashboardServer] = []

    def _start(provider: Any) -> DashboardServer:
        server = create_server(
            DashboardConfig(
                snapshot_provider=_snapshot,
                run_detail_provider=lambda run_id: None,
                run_transcript_provider=provider,
            )
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(server: DashboardServer, path: str, *, token: bool = True) -> tuple[int, Any]:
    host, port = server.address
    headers = {"Host": f"{host}:{port}"}
    if token:
        headers[TOKEN_HEADER] = server.token
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run-001" / transcript_name(3)
    path.parent.mkdir(parents=True)
    path.write_text(TRANSCRIPT, encoding="utf-8")
    return tmp_path


def test_the_route_serves_a_redacted_transcript(start: Any, runs_dir: Path) -> None:
    server = start(call_transcript_reader(runs_dir))

    status, payload = _get(server, "/api/runs/run-001/calls/3/transcript")

    assert status == 200
    assert payload["run_id"] == "run-001"
    assert payload["call"] == 3
    assert payload["events"][1] == {"type": "assistant", "text": "Bash ls -la"}
    assert GH_SECRET not in json.dumps(payload)
    assert "[REDACTED]" in payload["tail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/run-001/calls/4/transcript",
        "/api/runs/run-404/calls/3/transcript",
        "/api/runs/run-001/calls/0/transcript",
        "/api/runs/run-001/calls/03/transcript",
        "/api/runs/run-001/calls/-3/transcript",
        "/api/runs/run-001/calls/10000/transcript",
        "/api/runs/run-001/calls/..%2F..%2Frun.json/transcript",
        "/api/runs/..%2Frun-001/calls/3/transcript",
        "/api/runs/run-001/calls/3/transcript/x",
    ],
)
def test_the_route_refuses_bad_input(start: Any, runs_dir: Path, path: str) -> None:
    assert _get(start(call_transcript_reader(runs_dir)), path)[0] == 404


def test_the_route_needs_the_token(start: Any, runs_dir: Path) -> None:
    server = start(call_transcript_reader(runs_dir))

    assert _get(server, "/api/runs/run-001/calls/3/transcript", token=False)[0] == 401


def test_the_route_is_404_without_a_provider(start: Any) -> None:
    assert _get(start(None), "/api/runs/run-001/calls/3/transcript")[0] == 404


def test_a_failing_provider_is_503_without_details(start: Any) -> None:
    def broken(run_id: str, number: int) -> str | None:
        raise OSError(GH_SECRET)

    status, payload = _get(start(broken), "/api/runs/run-001/calls/3/transcript")

    assert (status, payload) == (503, {"error": "transcript unavailable"})


def test_the_page_lists_events_one_per_line() -> None:
    body = {"events": [{"type": "assistant", "text": "Bash ls"}, {"type": "system", "text": ""}]}
    calls = [
        JsCall("transcriptText", (body,)),
        JsCall("transcriptText", ({**body, "truncated": True},)),
        JsCall("transcriptText", ({},)),
        JsCall("transcriptPath", ("run 1", 3)),
    ]

    values = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]

    assert values == [
        "assistant  Bash ls\nsystem",
        "(earlier events left out)\nassistant  Bash ls\nsystem",
        "No events.",
        "/api/runs/run%201/calls/3/transcript",
    ]
