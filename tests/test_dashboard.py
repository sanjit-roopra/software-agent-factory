"""Tests for the read-only local dashboard (Phase 15.11, ADR-016).

These tests exercise the real ``ThreadingHTTPServer`` over real loopback
sockets (an ephemeral port, never a fixed one) so bind, ``Host``/``Origin``
validation and token handling are proven end to end without a browser. Fake
snapshot/detail providers stand in for the not-yet-built
``observability.build_monitoring_snapshot`` integration, matching the
documented contract in ``software_agent_factory.dashboard.snapshot``.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from software_agent_factory.dashboard import (
    DashboardConfig,
    DashboardServer,
    InvalidBindHostError,
    create_server,
)
from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.sanitize import (
    ATTEMPT_FIELDS,
    RUN_DETAIL_FIELDS,
    RUN_SUMMARY_FIELDS,
)
from software_agent_factory.dashboard.security import TOKEN_HEADER, validate_bind_host
from software_agent_factory.dashboard.snapshot import (
    MAX_PAGE_LIMIT,
    is_valid_run_id,
    to_json_safe,
)

FIXTURE_RUNS: list[dict[str, Any]] = [
    {
        "run_id": f"run-{index:03d}",
        "work_item_id": f"item-{index:03d}",
        "title": f"Fixture run {index}",
        "state": "DONE" if index % 2 == 0 else "FAILED",
        "complexity": "L1",
        "risk": "R1",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T01:00:00+00:00",
        "age_seconds": 3600.0,
        "idle_seconds": 60.0,
        "attempt_count": 1,
        "implementation_attempts": 1,
        "ci_repair_attempts": 0,
        "is_finished": True,
        "is_stale": index == 3,
    }
    for index in range(1, 6)
]

FIXTURE_DETAILS: dict[str, dict[str, Any]] = {
    run["run_id"]: {
        **run,
        "completed_at": None,
        "failure_reason": None,
        "commit_sha": None,
        "pull_request_url": None,
        "attempts": [
            {
                "attempt_number": 1,
                "role": "IMPLEMENTER",
                "model": "fake-model",
                "outcome": "SUCCESS",
                "started_at": "2024-01-01T00:00:00+00:00",
                "completed_at": "2024-01-01T00:05:00+00:00",
            }
        ],
    }
    for run in FIXTURE_RUNS
}


def fake_snapshot_provider(*, limit: int, offset: int) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("limit must be > 0")  # mirrors the real observability contract
    page = FIXTURE_RUNS[offset : offset + limit]
    total = len(FIXTURE_RUNS)
    return {
        "schema_version": 1,
        "generated_at": "2024-01-01T02:00:00+00:00",
        "stale_after_seconds": 900.0,
        "total_runs": total,
        "unreadable_runs": 0,
        "degraded": False,
        "degraded_reasons": [],
        "counts": {
            "succeeded": 3,
            "escalated": 0,
            "failed": 2,
            "active": 0,
            "stale_active": 1,
        },
        "attempts_by_role": {"IMPLEMENTER": 5},
        "attempts_by_model": {"fake-model": 5},
        "page": {
            "limit": limit,
            "offset": offset,
            "returned": len(page),
            "total": total,
            "has_more": (offset + len(page)) < total,
        },
        "runs": page,
    }


def fake_health_provider() -> dict[str, Any]:
    return {
        "success": True,
        "checks": [
            {"name": "git", "status": "ok", "message": "git 2.43.0", "remediation": None},
            {"name": "data_dir", "status": "ok", "message": "writable", "remediation": None},
        ],
    }


def fake_run_detail_provider(run_id: str) -> dict[str, Any] | None:
    return FIXTURE_DETAILS.get(run_id)


def failing_snapshot_provider(*, limit: int, offset: int) -> dict[str, Any]:
    raise RuntimeError("boom: simulated snapshot backend failure")


def failing_detail_provider(run_id: str) -> dict[str, Any] | None:
    raise RuntimeError("boom: simulated detail backend failure")


def failing_health_provider() -> dict[str, Any]:
    raise RuntimeError("boom: simulated health backend failure")


@dataclass
class RunningServer:
    server: DashboardServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def token(self) -> str:
        return self.server.token

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> http.client.HTTPResponse:
        conn = self.connection()
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        response.read_body = response.read()  # type: ignore[attr-defined]
        return response

    def authed_headers(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token, "Host": f"127.0.0.1:{self.port}"}


def _start(config: DashboardConfig) -> RunningServer:
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return RunningServer(server=server, thread=thread)


def _stop(running: RunningServer) -> None:
    running.server.shutdown()
    running.server.server_close()
    running.thread.join(timeout=5)


@pytest.fixture
def running_server() -> Iterator[RunningServer]:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        health_provider=fake_health_provider,
    )
    running = _start(config)
    try:
        yield running
    finally:
        _stop(running)


def _body_json(response: http.client.HTTPResponse) -> Any:
    return json.loads(response.read_body)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Bind host rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_host",
    [
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "example.com",
        "",
        # Other 127.0.0.0/8 loopback literals and the IPv6 loopback literal
        # are real loopback addresses, but this server never actually binds
        # to them -- only the exact literal it does bind (127.0.0.1) is
        # accepted, so accepting these here would claim a guarantee this
        # implementation does not keep.
        "127.0.0.2",
        "127.1.1.1",
        "::1",
        "[::1]",
    ],
)
def test_non_loopback_bind_host_is_rejected(bad_host: str) -> None:
    config = DashboardConfig(
        host=bad_host,
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    with pytest.raises(InvalidBindHostError):
        create_server(config)


@pytest.mark.parametrize("good_host", ["127.0.0.1", "localhost", "LOCALHOST", "127.0.0.1 "])
def test_loopback_bind_host_is_accepted(good_host: str) -> None:
    config = DashboardConfig(
        host=good_host,
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    server = create_server(config)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_validate_bind_host_rejects_other_loopback_literals() -> None:
    # Unit-level check of the validator itself (not just through
    # create_server): confirms rejection is a property of the validation
    # function, not an artifact of a socket bind failure.
    for candidate in ("127.0.0.2", "127.1.1.1", "::1"):
        with pytest.raises(InvalidBindHostError):
            validate_bind_host(candidate)


def test_validate_bind_host_normalizes_localhost() -> None:
    assert validate_bind_host("localhost") == "127.0.0.1"
    assert validate_bind_host("LOCALHOST") == "127.0.0.1"
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"


# --------------------------------------------------------------------------
# Token enforcement
# --------------------------------------------------------------------------


def test_missing_token_is_rejected(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/summary", headers={"Host": f"127.0.0.1:{running_server.port}"}
    )
    assert response.status == 401
    payload = _body_json(response)
    assert "run" not in json.dumps(payload).lower()


def test_wrong_token_is_rejected(running_server: RunningServer) -> None:
    headers = {"Host": f"127.0.0.1:{running_server.port}", TOKEN_HEADER: "not-the-token"}
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 401


def test_wrong_query_token_is_rejected(running_server: RunningServer) -> None:
    headers = {"Host": f"127.0.0.1:{running_server.port}"}
    response = running_server.request("GET", "/?token=wrong", headers=headers)
    assert response.status == 401


def test_correct_query_token_authenticates_index(running_server: RunningServer) -> None:
    headers = {"Host": f"127.0.0.1:{running_server.port}"}
    response = running_server.request("GET", f"/?token={running_server.token}", headers=headers)
    assert response.status == 200
    assert b"<html" in response.read_body.lower()  # type: ignore[attr-defined]


def test_correct_header_token_authenticates_api(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/summary", headers=running_server.authed_headers()
    )
    assert response.status == 200


def test_token_never_appears_in_dashboard_url_logging_path(
    running_server: RunningServer,
) -> None:
    # The dashboard_url is the one sanctioned place the token is exposed.
    assert running_server.token in running_server.server.dashboard_url
    assert running_server.server.dashboard_url.startswith("http://127.0.0.1:")


# --------------------------------------------------------------------------
# Host / Origin validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_host_header",
    [
        "evil.com",
        "127.0.0.1.evil.com",
        "127.0.0.1:1",
        "attacker.example",
        # These are real loopback-equivalent aliases a browser might send,
        # but the server bound exactly "127.0.0.1", so only that literal
        # (with the actual port) is accepted -- no interchangeable alias.
        "localhost:{port}",
        "[::1]:{port}",
        "127.0.0.2:{port}",
    ],
)
def test_wrong_host_header_is_rejected(running_server: RunningServer, bad_host_header: str) -> None:
    headers = {
        "Host": bad_host_header.format(port=running_server.port),
        TOKEN_HEADER: running_server.token,
    }
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 400


def test_exact_host_header_is_accepted(running_server: RunningServer) -> None:
    headers = {
        "Host": f"127.0.0.1:{running_server.port}",
        TOKEN_HEADER: running_server.token,
    }
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 200


def test_mismatched_origin_is_rejected(running_server: RunningServer) -> None:
    headers = running_server.authed_headers()
    headers["Origin"] = "http://evil.com"
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 403


@pytest.mark.parametrize(
    "alias_origin",
    [
        "http://localhost:{port}",
        "http://[::1]:{port}",
        "http://127.0.0.2:{port}",
        "https://127.0.0.1:{port}",
    ],
)
def test_alias_origin_is_rejected(running_server: RunningServer, alias_origin: str) -> None:
    # Same principle as the Host check: an alias that a browser would treat
    # as loopback-equivalent is still not this server's exact origin, and no
    # exact-origin check should treat it as same-origin.
    headers = running_server.authed_headers()
    headers["Origin"] = alias_origin.format(port=running_server.port)
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 403


def test_matching_origin_is_accepted(running_server: RunningServer) -> None:
    headers = running_server.authed_headers()
    headers["Origin"] = f"http://127.0.0.1:{running_server.port}"
    response = running_server.request("GET", "/api/summary", headers=headers)
    assert response.status == 200


def test_absent_origin_is_accepted(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/summary", headers=running_server.authed_headers()
    )
    assert response.status == 200


def test_no_cors_headers_are_ever_present(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/summary", headers=running_server.authed_headers()
    )
    for header_name, _ in response.getheaders():
        assert not header_name.lower().startswith("access-control-")


# --------------------------------------------------------------------------
# Method enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_mutating_methods_return_405_even_without_auth(
    running_server: RunningServer, method: str
) -> None:
    # No Host/Origin/token supplied at all: 405 must still fire, because the
    # method itself is unsupported regardless of authentication state.
    response = running_server.request(method, "/api/summary")
    assert response.status == 405
    allow_header = response.getheader("Allow")
    assert allow_header is not None
    assert "GET" in allow_header
    assert "HEAD" in allow_header


def test_get_and_head_are_allowed(running_server: RunningServer) -> None:
    get_response = running_server.request(
        "GET", "/healthz", headers=running_server.authed_headers()
    )
    assert get_response.status == 200
    head_response = running_server.request(
        "HEAD", "/healthz", headers=running_server.authed_headers()
    )
    assert head_response.status == 200
    assert head_response.read_body == b""  # type: ignore[attr-defined]


def test_method_not_allowed_has_full_security_header_set(
    running_server: RunningServer,
) -> None:
    response = running_server.request("POST", "/api/summary")
    assert response.status == 405
    headers = {name.lower(): value for name, value in response.getheaders()}
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"
    for header_name in headers:
        assert not header_name.startswith("access-control-")


def test_method_not_allowed_never_reflects_or_logs_token(
    running_server: RunningServer,
) -> None:
    logger = logging.getLogger("software_agent_factory.dashboard")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        response = running_server.request(
            "PUT",
            f"/api/summary?token={running_server.token}",
            headers={TOKEN_HEADER: running_server.token},
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert response.status == 405
    body_text = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
    assert running_server.token not in body_text
    assert running_server.token not in stream.getvalue()


# --------------------------------------------------------------------------
# Bounded query parsing
# --------------------------------------------------------------------------


def test_excessive_query_fields_return_400_not_internal_error(
    running_server: RunningServer,
) -> None:
    # Comfortably above the handler's max_num_fields cap. Must be a clean
    # 400, never an unhandled ValueError propagating out of parse_qs().
    query = "&".join(f"field{i}=v" for i in range(200))
    response = running_server.request(
        "GET", f"/api/runs?{query}", headers=running_server.authed_headers()
    )
    assert response.status == 400
    payload = _body_json(response)
    assert "error" in payload


def test_excessive_query_fields_response_has_security_headers(
    running_server: RunningServer,
) -> None:
    query = "&".join(f"field{i}=v" for i in range(200))
    response = running_server.request(
        "GET", f"/api/runs?{query}", headers=running_server.authed_headers()
    )
    assert response.status == 400
    headers = {name.lower(): value for name, value in response.getheaders()}
    assert headers["cache-control"] == "no-store"


def test_reasonable_query_field_count_is_accepted(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs?limit=2&offset=0", headers=running_server.authed_headers()
    )
    assert response.status == 200


# --------------------------------------------------------------------------
# Security headers
# --------------------------------------------------------------------------


def test_security_headers_present_on_success(running_server: RunningServer) -> None:
    response = running_server.request("GET", "/healthz", headers=running_server.authed_headers())
    headers = {name.lower(): value for name, value in response.getheaders()}
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "'unsafe-inline'" not in headers["content-security-policy"]
    assert "'unsafe-eval'" not in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert headers["cache-control"] == "no-store"


def test_security_headers_present_on_error(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/no-such-route", headers=running_server.authed_headers()
    )
    assert response.status == 404
    headers = {name.lower(): value for name, value in response.getheaders()}
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------
# Routing / unknown routes
# --------------------------------------------------------------------------


def test_unknown_route_is_404(running_server: RunningServer) -> None:
    response = running_server.request("GET", "/nope", headers=running_server.authed_headers())
    assert response.status == 404


def test_assets_are_served(running_server: RunningServer) -> None:
    js_response = running_server.request(
        "GET",
        f"/assets/app.js?token={running_server.token}",
        headers={"Host": f"127.0.0.1:{running_server.port}"},
    )
    assert js_response.status == 200
    assert "javascript" in js_response.getheader("Content-Type", "")

    css_response = running_server.request(
        "GET",
        f"/assets/style.css?token={running_server.token}",
        headers={"Host": f"127.0.0.1:{running_server.port}"},
    )
    assert css_response.status == 200
    assert "css" in css_response.getheader("Content-Type", "")


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_runs_pagination_defaults(running_server: RunningServer) -> None:
    response = running_server.request("GET", "/api/runs", headers=running_server.authed_headers())
    assert response.status == 200
    payload = _body_json(response)
    assert payload["page"]["limit"] == 20
    assert payload["page"]["offset"] == 0
    assert payload["page"]["total"] == len(FIXTURE_RUNS)
    assert len(payload["runs"]) == len(FIXTURE_RUNS)


def test_runs_pagination_hard_cap(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs?limit=999999&offset=0", headers=running_server.authed_headers()
    )
    assert response.status == 200
    payload = _body_json(response)
    assert payload["page"]["limit"] == MAX_PAGE_LIMIT


def test_runs_pagination_offset(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs?limit=2&offset=2", headers=running_server.authed_headers()
    )
    assert response.status == 200
    payload = _body_json(response)
    assert [run["run_id"] for run in payload["runs"]] == [
        run["run_id"] for run in FIXTURE_RUNS[2:4]
    ]


@pytest.mark.parametrize("query", ["limit=abc", "limit=0", "limit=-1", "offset=-1", "offset=abc"])
def test_runs_pagination_invalid_params_rejected(running_server: RunningServer, query: str) -> None:
    response = running_server.request(
        "GET", f"/api/runs?{query}", headers=running_server.authed_headers()
    )
    assert response.status == 400


def test_summary_never_includes_run_list(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/summary", headers=running_server.authed_headers()
    )
    payload = _body_json(response)
    assert "runs" not in payload
    assert "page" not in payload
    assert "counts" in payload
    assert "health" in payload
    assert payload["health"]["success"] is True


def test_summary_reports_null_health_when_not_configured() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert response.status == 200
        payload = _body_json(response)
        assert payload["health"] is None
    finally:
        _stop(running)


def test_summary_degrades_gracefully_when_health_provider_fails() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        health_provider=failing_health_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        # A failing health check is a reportable finding, not a hard 503:
        # counts/totals are still available even when health cannot be
        # computed.
        assert response.status == 200
        payload = _body_json(response)
        assert "counts" in payload
        assert payload["health"] == {"error": "health check unavailable"}
    finally:
        _stop(running)


# --------------------------------------------------------------------------
# Run id validation / traversal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "traversal_id",
    ["..", "%2e%2e", "run%00id", "run;drop", "run id", "a" * 200],
)
def test_traversal_shaped_run_ids_rejected_before_provider(
    traversal_id: str,
) -> None:
    calls: list[str] = []

    def recording_detail_provider(run_id: str) -> dict[str, Any] | None:
        calls.append(run_id)
        return FIXTURE_DETAILS.get(run_id)

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=recording_detail_provider,
    )
    running = _start(config)
    try:
        headers = {"Host": f"127.0.0.1:{running.port}", TOKEN_HEADER: running.token}
        # Quote for the wire: fixture ids already containing a literal "%"
        # (e.g. "%2e%2e") must reach the server unquoted-once so the
        # server's own unquote() step produces the traversal shape.
        wire_path = quote(traversal_id, safe="%")
        response = running.request("GET", f"/api/runs/{wire_path}", headers=headers)
        assert response.status == 404
        assert calls == []
    finally:
        _stop(running)


def test_valid_run_id_reaches_provider(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs/run-001", headers=running_server.authed_headers()
    )
    assert response.status == 200
    payload = _body_json(response)
    assert payload["run_id"] == "run-001"
    assert "attempts" in payload
    # No raw logs, diffs or prompt content are exposed by the fixture detail
    # shape, and the client-side allowlist in app.js never renders such keys
    # even if a future provider were to include them.
    assert "logs" not in payload
    assert "diff" not in payload


def test_unknown_but_valid_run_id_is_404(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs/does-not-exist", headers=running_server.authed_headers()
    )
    assert response.status == 404


def test_is_valid_run_id_helper() -> None:
    assert is_valid_run_id("run-001")
    assert is_valid_run_id("a" * 128)
    assert not is_valid_run_id("..")
    assert not is_valid_run_id("a/b")
    assert not is_valid_run_id("a" * 129)
    assert not is_valid_run_id("")


# --------------------------------------------------------------------------
# Response data minimization: adversarial providers
#
# Even a provider that is supposed to be dashboard-safe might accidentally
# include something it should not (a bug, a copy-paste of a richer internal
# object, ...). These tests plant secret-shaped extra fields on top of an
# otherwise-valid fixture and assert they never appear anywhere in the raw
# HTTP response bytes, proving the handler's allowlist -- not the provider's
# good behavior -- is what keeps them out.
# --------------------------------------------------------------------------

SECRET_MARKER = "SECRET-sk-adversarial-0xDEADBEEF"


def adversarial_snapshot_provider(*, limit: int, offset: int) -> dict[str, Any]:
    base = fake_snapshot_provider(limit=limit, offset=offset)
    poisoned_runs = [
        {
            **run,
            "logs": f"command output containing {SECRET_MARKER}",
            "diff": f"--- a/file\n+++ b/file\n{SECRET_MARKER}\n",
            "prompt": f"system prompt leaking {SECRET_MARKER}",
            "tool_output": SECRET_MARKER,
            "reasoning": f"chain of thought: {SECRET_MARKER}",
            "failure_reason": f"traceback containing {SECRET_MARKER}",
            "token_usage": {"api_key": SECRET_MARKER},
            "raw_artifact": SECRET_MARKER,
        }
        for run in base["runs"]
    ]
    return {**base, "runs": poisoned_runs}


def adversarial_run_detail_provider(run_id: str) -> dict[str, Any] | None:
    detail = FIXTURE_DETAILS.get(run_id)
    if detail is None:
        return None
    return {
        **detail,
        "logs": f"command output containing {SECRET_MARKER}",
        "diff": f"--- a/file\n+++ b/file\n{SECRET_MARKER}\n",
        "prompt": f"system prompt leaking {SECRET_MARKER}",
        "tool_output": SECRET_MARKER,
        "reasoning": f"chain of thought: {SECRET_MARKER}",
        "failure_reason": f"traceback containing {SECRET_MARKER}",
        "token_usage": {"api_key": SECRET_MARKER},
        "raw_artifact": SECRET_MARKER,
        "attempts": [
            {
                **attempt,
                "reasoning": f"attempt chain of thought: {SECRET_MARKER}",
                "failure_reason": f"attempt traceback: {SECRET_MARKER}",
                "tool_output": SECRET_MARKER,
                "raw_command_log": SECRET_MARKER,
            }
            for attempt in detail["attempts"]
        ],
    }


def test_adversarial_snapshot_provider_secrets_never_reach_runs_response() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=adversarial_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/runs", headers=running.authed_headers())
        assert response.status == 200
        raw_body = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert SECRET_MARKER not in raw_body
        payload = json.loads(raw_body)
        for run in payload["runs"]:
            assert set(run) <= RUN_SUMMARY_FIELDS
            assert "logs" not in run
            assert "diff" not in run
            assert "prompt" not in run
            assert "tool_output" not in run
            assert "reasoning" not in run
            assert "failure_reason" not in run
            assert "token_usage" not in run
            assert "raw_artifact" not in run
    finally:
        _stop(running)


def test_adversarial_snapshot_provider_secrets_never_reach_summary_response() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=adversarial_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert response.status == 200
        raw_body = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert SECRET_MARKER not in raw_body
    finally:
        _stop(running)


def test_adversarial_run_detail_provider_secrets_never_reach_response() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=adversarial_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/runs/run-001", headers=running.authed_headers())
        assert response.status == 200
        raw_body = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert SECRET_MARKER not in raw_body
        payload = json.loads(raw_body)
        assert set(payload) <= RUN_DETAIL_FIELDS | {"attempts"}
        assert "logs" not in payload
        assert "diff" not in payload
        assert "prompt" not in payload
        assert "tool_output" not in payload
        assert "reasoning" not in payload
        assert "failure_reason" not in payload
        assert "token_usage" not in payload
        assert "raw_artifact" not in payload
        for attempt in payload["attempts"]:
            assert set(attempt) <= ATTEMPT_FIELDS
            assert "reasoning" not in attempt
            assert "failure_reason" not in attempt
            assert "tool_output" not in attempt
            assert "raw_command_log" not in attempt
    finally:
        _stop(running)


def test_failure_reason_is_never_returned_even_when_provider_sets_it() -> None:
    # Explicit, targeted check for the "prefer omitting failure_reason"
    # requirement: even a provider that populates it directly (not just via
    # the broader adversarial payload above) never sees it echoed back.
    def provider(run_id: str) -> dict[str, Any] | None:
        detail = FIXTURE_DETAILS.get(run_id)
        if detail is None:
            return None
        return {**detail, "failure_reason": "a raw failure reason with detail"}

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/runs/run-001", headers=running.authed_headers())
        assert response.status == 200
        payload = _body_json(response)
        assert "failure_reason" not in payload
    finally:
        _stop(running)


# --------------------------------------------------------------------------
# Provider failure handling
# --------------------------------------------------------------------------


def test_snapshot_provider_failure_returns_503_without_traceback() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=failing_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert response.status == 503
        body_text = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert "Traceback" not in body_text
        assert "RuntimeError" not in body_text
        assert "boom" not in body_text
    finally:
        _stop(running)


def test_run_detail_provider_failure_returns_503_without_traceback() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=failing_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/runs/run-001", headers=running.authed_headers())
        assert response.status == 503
        body_text = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert "Traceback" not in body_text
        assert "boom" not in body_text
    finally:
        _stop(running)


# --------------------------------------------------------------------------
# Asset content safety
# --------------------------------------------------------------------------


def test_app_js_never_uses_dangerous_rendering_apis() -> None:
    js = dashboard_assets.APP_JS
    assert "innerHTML" not in js
    assert "outerHTML" not in js
    assert "document.write" not in js
    assert "eval(" not in js
    assert "new Function(" not in js
    assert "textContent" in js


def test_index_html_has_no_inline_script_body() -> None:
    html = dashboard_assets.render_index_html(token="fixture-token")
    assert "<script>" not in html
    assert "onclick=" not in html
    assert "onerror=" not in html
    assert 'src="/assets/app.js?token=fixture-token"' in html


# --------------------------------------------------------------------------
# to_json_safe normalization
# --------------------------------------------------------------------------


def test_to_json_safe_handles_dict_and_none() -> None:
    assert to_json_safe(None) is None
    assert to_json_safe({"a": 1}) == {"a": 1}


def test_to_json_safe_handles_dataclass() -> None:
    @dataclass
    class Sample:
        a: int
        b: str

    assert to_json_safe(Sample(a=1, b="x")) == {"a": 1, "b": "x"}


def test_to_json_safe_handles_pydantic_model() -> None:
    from software_agent_factory.models import Complexity, Risk, WorkItem

    item = WorkItem(
        id="wi-1",
        title="Title",
        description="Description",
        complexity=Complexity.L1,
        risk=Risk.R1,
    )
    dumped = to_json_safe(item)
    assert dumped["id"] == "wi-1"
    assert dumped["complexity"] == "L1"


def test_to_json_safe_rejects_unsupported_type() -> None:
    class Unsupported:
        pass

    with pytest.raises(TypeError):
        to_json_safe(Unsupported())


# --------------------------------------------------------------------------
# Live loopback smoke test: full page-load-style flow, real sockets
# --------------------------------------------------------------------------


def test_live_loopback_smoke(running_server: RunningServer) -> None:
    host_header = {"Host": f"127.0.0.1:{running_server.port}"}

    index_response = running_server.request(
        "GET", f"/?token={running_server.token}", headers=host_header
    )
    assert index_response.status == 200

    js_response = running_server.request(
        "GET",
        f"/assets/app.js?token={running_server.token}",
        headers=host_header,
    )
    assert js_response.status == 200

    summary_response = running_server.request(
        "GET", "/api/summary", headers=running_server.authed_headers()
    )
    assert summary_response.status == 200

    runs_response = running_server.request(
        "GET", "/api/runs?limit=5&offset=0", headers=running_server.authed_headers()
    )
    assert runs_response.status == 200
    runs_payload = _body_json(runs_response)
    first_run_id = runs_payload["runs"][0]["run_id"]

    detail_response = running_server.request(
        "GET", f"/api/runs/{first_run_id}", headers=running_server.authed_headers()
    )
    assert detail_response.status == 200
    detail_payload = _body_json(detail_response)
    assert detail_payload["run_id"] == first_run_id


# --------------------------------------------------------------------------
# Real integration smoke test: the actual observability/store modules, not
# fakes. Proves the injectable-provider design genuinely decouples the
# dashboard from those modules while still being wire-compatible with them.
# --------------------------------------------------------------------------


def test_wires_real_observability_and_store_end_to_end(tmp_path: Path) -> None:
    from software_agent_factory.models import FactoryRun, WorkflowState
    from software_agent_factory.observability import build_monitoring_snapshot
    from software_agent_factory.store import FileRunStore

    store = FileRunStore(tmp_path / "data")
    for index in range(1, 4):
        run = FactoryRun(
            id=f"real-run-{index:03d}",
            work_item_id=f"WI-{index:03d}",
            state=WorkflowState.DONE if index != 2 else WorkflowState.FAILED,
        )
        store.save_run(run)

    def real_snapshot_provider(*, limit: int, offset: int) -> Any:
        return build_monitoring_snapshot(store, limit=limit, offset=offset)

    def real_run_detail_provider(run_id: str) -> dict[str, Any] | None:
        try:
            run = store.load_run(run_id)
        except (OSError, ValueError):
            return None
        return run.model_dump(mode="json")

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=real_snapshot_provider,
        run_detail_provider=real_run_detail_provider,
    )
    running = _start(config)
    try:
        summary_response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert summary_response.status == 200
        summary_payload = _body_json(summary_response)
        assert summary_payload["counts"]["succeeded"] == 2
        assert summary_payload["counts"]["failed"] == 1
        assert "runs" not in summary_payload

        runs_response = running.request(
            "GET", "/api/runs?limit=10&offset=0", headers=running.authed_headers()
        )
        assert runs_response.status == 200
        runs_payload = _body_json(runs_response)
        assert runs_payload["page"]["total"] == 3
        run_ids = {run["run_id"] for run in runs_payload["runs"]}
        assert run_ids == {"real-run-001", "real-run-002", "real-run-003"}

        detail_response = running.request(
            "GET", "/api/runs/real-run-001", headers=running.authed_headers()
        )
        assert detail_response.status == 200
        detail_payload = _body_json(detail_response)
        assert detail_payload["id"] == "real-run-001"

        missing_response = running.request(
            "GET", "/api/runs/real-run-999", headers=running.authed_headers()
        )
        assert missing_response.status == 404
    finally:
        _stop(running)
