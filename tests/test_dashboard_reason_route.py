"""The full attempt and call reasons of ``GET /api/runs/<id>/reason`` (ADR-062)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from dashboard_js import JsCall, function_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.reason_route import FULL_REASON_LIMIT, reason_source
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, DashboardServer, create_server
from software_agent_factory.redaction import REASON_LIMIT

GH_SECRET = "GH_TOKEN=ghp_abcdefgh12345678"
LONG_REASON = f"{GH_SECRET} " + "a" * 900 + " the end"
DETAIL: dict[str, Any] = {
    "run_id": "run-001",
    "failure_reason": "run failed",
    "attempts": [
        {"attempt_number": 1, "failure_reason": "first"},
        {"attempt_number": 2, "failure_reason": LONG_REASON},
        {"attempt_number": True, "failure_reason": "a flag, not a number"},
    ],
    "invocations": [
        {"invocation_number": 4, "failure_reason": "c" * (FULL_REASON_LIMIT + 50)},
        {"invocation_number": 5, "failure_reason": None},
    ],
}


def _snapshot(*, limit: int, offset: int) -> dict[str, Any]:
    return {"runs": [], "page": {"limit": limit, "offset": offset}}


@pytest.fixture
def server() -> Iterator[DashboardServer]:
    running = create_server(
        DashboardConfig(
            snapshot_provider=_snapshot,
            run_detail_provider=lambda run_id: DETAIL if run_id == "run-001" else None,
        )
    )
    threading.Thread(target=running.serve_forever, daemon=True).start()
    yield running
    running.shutdown()
    running.server_close()


def _get(server: DashboardServer, path: str) -> tuple[int, Any]:
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(
            "GET", path, headers={"Host": f"{host}:{port}", TOKEN_HEADER: server.token}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


def test_an_attempt_reason_comes_whole_and_redacted(server: DashboardServer) -> None:
    status, payload = _get(server, "/api/runs/run-001/reason?attempt=2")

    assert status == 200
    assert payload["failure_reason_truncated"] is False
    assert len(payload["failure_reason"]) > REASON_LIMIT
    assert payload["failure_reason"].endswith("a" * 900 + " the end")
    assert "ghp_abcdefgh12345678" not in payload["failure_reason"]


def test_a_call_reason_is_cut_past_the_full_limit(server: DashboardServer) -> None:
    status, payload = _get(server, "/api/runs/run-001/reason?call=4")

    assert status == 200
    assert payload["failure_reason_truncated"] is True
    assert len(payload["failure_reason"]) <= FULL_REASON_LIMIT
    assert "factory show run-001" in payload["failure_reason"]


def test_a_call_without_a_reason_answers_none(server: DashboardServer) -> None:
    assert _get(server, "/api/runs/run-001/reason?call=5") == (
        200,
        {"failure_reason": None, "failure_reason_truncated": False},
    )


def test_without_a_record_the_route_still_answers_the_run_reason(server: DashboardServer) -> None:
    assert _get(server, "/api/runs/run-001/reason")[1]["failure_reason"] == "run failed"


@pytest.mark.parametrize(
    "query",
    [
        "attempt=9",
        "call=1",
        "attempt=0",
        "attempt=01",
        "attempt=-1",
        "attempt=x",
        "attempt=",
        "attempt=12345",
        "attempt=1&call=4",
        "attempt=1&attempt=2",
    ],
)
def test_a_bad_or_unknown_record_is_404(server: DashboardServer, query: str) -> None:
    assert _get(server, f"/api/runs/run-001/reason?{query}")[0] == 404


def test_a_boolean_record_number_never_matches(server: DashboardServer) -> None:
    assert _get(server, "/api/runs/run-001/reason?attempt=1")[1]["failure_reason"] == "first"
    assert _get(server, "/api/runs/run-404/reason?attempt=1")[0] == 404


def test_a_detail_that_is_not_an_object_is_refused() -> None:
    source = reason_source(lambda run_id: ["not", "an", "object"], {"call": ["1"]})

    assert source is not None
    with pytest.raises(TypeError):
        source("run-001")


def test_the_page_offers_the_whole_reason_of_a_cut_attempt_or_call() -> None:
    calls = [
        JsCall("recordReason", ("run-001", "attempt=2", "cut text", True)),
        JsCall("recordReason", ("run-001", "attempt=1", "short", False)),
        JsCall("recordReason", (None, "call=4", "cut text", True)),
    ]

    values = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]

    assert values == [
        {"text": "cut text", "query": "attempt=2", "shown": "cut text", "cut": True},
        {"text": "short", "query": "attempt=1", "shown": "short", "cut": False},
        {"text": "cut text", "query": "call=4", "shown": "cut text", "cut": False},
    ]
    loader = function_source(dashboard_assets.APP_JS, "showRecordReason")
    assert 'reasonPath(runId) + "?" + query' in loader
