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
import re
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from dashboard_js import function_source, normalized

from software_agent_factory.dashboard import (
    DashboardConfig,
    DashboardServer,
    InvalidBindHostError,
    create_server,
)
from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.handler import (
    _MAX_LOGGED_METHOD_LENGTH,
    _MAX_LOGGED_PATH_LENGTH,
    _log_safe_method,
    _log_safe_path,
)
from software_agent_factory.dashboard.sanitize import (
    ACTIVE_INVOCATION_FIELDS,
    ATTEMPT_FIELDS,
    INVOCATION_FIELDS,
    PROJECT_FIELDS,
    PROJECT_MODEL_FIELDS,
    PROJECT_TASK_FIELDS,
    RUN_DETAIL_FIELDS,
    RUN_SUMMARY_FIELDS,
    sanitize_invocation,
    sanitize_project,
    sanitize_run_detail,
    sanitize_usage,
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
        "source_external_id": f"acme/example#{index}",
        "title": f"Fixture run {index}",
        "state": "DONE" if index % 2 == 0 else "FAILED",
        "complexity": "L1",
        "risk": "R1",
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T01:00:00+00:00",
        "age_seconds": 3600.0,
        "idle_seconds": 60.0,
        "attempt_count": 1,
        "invocation_count": 1,
        "implementation_attempts": 1,
        "ci_repair_attempts": 0,
        "usage": {
            "invocation_count": 1,
            "reported_invocations": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "premium_request_cost": 1.0,
        },
        "is_finished": True,
        "is_stale": index == 3,
        "requested_performance_mode": "fast",
        "risk_assessment_enabled": False,
        "effective_performance_mode": "standard",
        "performance_model_profile": "economy",
        "waiting_for_human": False,
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
                "budget": "IMPLEMENTATION",
                "triggered_by": "INITIAL",
                "outcome": "SUCCESS",
                "started_at": "2024-01-01T00:00:00+00:00",
                "completed_at": "2024-01-01T00:05:00+00:00",
            }
        ],
        "invocations": [
            {
                "invocation_number": 1,
                "role": "IMPLEMENTER",
                "purpose": "STANDARD",
                "model": "fake-model",
                "context_tier": "default",
                "success": True,
                "started_at": "2024-01-01T00:00:00+00:00",
                "completed_at": "2024-01-01T00:05:00+00:00",
                "attempt_number": 1,
                "usage": {
                    "last_call_input_tokens": 100,
                    "last_call_output_tokens": 20,
                    "total_premium_request_cost": 1.0,
                },
            }
        ],
        "active_invocation": {
            "invocation_number": 2,
            "role": "IMPLEMENTER",
            "purpose": "STANDARD",
            "model": "fake-model",
            "context_tier": "default",
            "status": "running",
            "started_at": "2024-01-01T00:06:00+00:00",
            "attempt_number": 2,
            "prompt": "must-not-be-exposed",
        },
        "merge_commit_sha": "a" * 40,
        "verification": {
            "passed": True,
            "check_count": 2,
            "failed_check_count": 0,
            "coverage_change": 1.5,
            "stdout": "must-not-be-exposed",
        },
        "artifacts": ["work-item.json", "verification.json", "patch.diff"],
        "escalation": {
            "status": "NOTIFIED",
            "target_type": "ISSUE",
            "comment_url": "https://github.com/acme/example/issues/1#issuecomment-1",
            "reason_code": "RISK_APPROVAL",
            "resume_classification": "RISK_APPROVAL",
            "waiting_for_human": True,
            "waiting_since": "2024-01-01T01:00:00+00:00",
            "episode_number": 1,
            "reopen_count": 0,
            "accepted_reply_count": 0,
            "last_responder": None,
            "last_action": None,
            "last_response_at": None,
            "is_resumed": False,
            "resumed_at": None,
            "raw_comment_body": "must-not-be-exposed",
        },
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


def fake_project_provider() -> dict[str, Any]:
    return {
        "projects": [
            {
                "project_id": "project-001",
                "state": "RUNNING",
                "delivery_mode": "merge",
                "delivery_repository": "acme/example",
                "delivery_base_branch": "main",
                "integration_branch": "factory/project-project-001",
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T01:00:00+00:00",
                "completed_at": None,
                "task_count": 1,
                "tasks": [
                    {
                        "task_id": 1,
                        "title": "Build feature",
                        "state": "RUNNING",
                        "run_id": "run-001",
                        "issue_url": None,
                        "pull_request_url": "https://github.com/acme/example/pull/1",
                        "commit_sha": None,
                        "merge_commit_sha": None,
                    }
                ],
                "models": [
                    {
                        "scope": "task 1",
                        "task_id": 1,
                        "invocation_number": 1,
                        "role": "IMPLEMENTER",
                        "purpose": "STANDARD",
                        "model": "fake-model",
                        "context_tier": "default",
                        "success": True,
                        "started_at": "2024-01-01T00:00:00+00:00",
                        "completed_at": "2024-01-01T00:05:00+00:00",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_nano_aiu": 38_483_200_000,
                            "total_premium_request_cost": 1.0,
                        },
                    }
                ],
            }
        ]
    }


def fake_run_detail_provider(run_id: str) -> dict[str, Any] | None:
    return FIXTURE_DETAILS.get(run_id)


def failing_snapshot_provider(*, limit: int, offset: int) -> dict[str, Any]:
    raise RuntimeError("boom: simulated snapshot backend failure")


def failing_detail_provider(run_id: str) -> dict[str, Any] | None:
    raise RuntimeError("boom: simulated detail backend failure")


def failing_health_provider() -> dict[str, Any]:
    raise RuntimeError("boom: simulated health backend failure")


def failing_project_provider() -> dict[str, Any]:
    raise RuntimeError("boom: simulated project backend failure")


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
        project_provider=fake_project_provider,
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
    assert js_response.read_body == _static_bytes("app.js")  # type: ignore[attr-defined]

    css_response = running_server.request(
        "GET",
        f"/assets/style.css?token={running_server.token}",
        headers={"Host": f"127.0.0.1:{running_server.port}"},
    )
    assert css_response.status == 200
    assert "css" in css_response.getheader("Content-Type", "")
    assert css_response.read_body == _static_bytes("style.css")  # type: ignore[attr-defined]


def _static_bytes(name: str) -> bytes:
    return resources.files("software_agent_factory.dashboard").joinpath("static", name).read_bytes()


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


def test_projects_show_project_and_task_progress(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/projects", headers=running_server.authed_headers()
    )
    assert response.status == 200
    payload = _body_json(response)
    project = payload["projects"][0]
    assert set(project) <= PROJECT_FIELDS | {"tasks", "models"}
    assert project["project_id"] == "project-001"
    assert project["state"] == "RUNNING"
    assert set(project["tasks"][0]) <= PROJECT_TASK_FIELDS
    assert project["tasks"][0]["pull_request_url"].endswith("/pull/1")
    assert set(project["models"][0]) <= PROJECT_MODEL_FIELDS
    assert project["models"][0]["model"] == "fake-model"
    assert project["models"][0]["usage"]["input_tokens"] == 100
    assert project["models"][0]["usage"]["usage_value_usd"] == pytest.approx(0.384832)


def test_projects_are_empty_when_provider_is_not_configured() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/projects", headers=running.authed_headers())
        assert response.status == 200
        assert _body_json(response) == {"projects": []}
    finally:
        _stop(running)


def test_project_provider_failure_returns_503_without_traceback() -> None:
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        project_provider=failing_project_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/projects", headers=running.authed_headers())
        assert response.status == 503
        body_text = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert "Traceback" not in body_text
        assert "boom" not in body_text
    finally:
        _stop(running)


def test_project_response_drops_unrendered_provider_fields() -> None:
    def adversarial_project_provider() -> dict[str, Any]:
        project = fake_project_provider()["projects"][0]
        return {
            "projects": [
                {
                    **project,
                    "prompt": SECRET_MARKER,
                    "failure_reason": SECRET_MARKER,
                    "models": [
                        {
                            "scope": "project",
                            "model": "fake-model",
                            "prompt": SECRET_MARKER,
                            "failure_reason": SECRET_MARKER,
                            "usage": {
                                "input_tokens": 100,
                                "api_key": SECRET_MARKER,
                            },
                        }
                    ],
                    "tasks": [
                        {
                            **project["tasks"][0],
                            "description": SECRET_MARKER,
                            "failure_reason": SECRET_MARKER,
                        }
                    ],
                }
            ]
        }

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        project_provider=adversarial_project_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/projects", headers=running.authed_headers())
        assert response.status == 200
        raw_body = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert SECRET_MARKER not in raw_body
    finally:
        _stop(running)


@pytest.mark.parametrize(
    "unsafe_tasks",
    [{"secret": "PROJECT-SECRET-MARKER"}, "PROJECT-SECRET-MARKER", 42],
)
def test_project_response_drops_non_list_tasks(unsafe_tasks: object) -> None:
    def adversarial_project_provider() -> dict[str, Any]:
        return {"projects": [{"project_id": "project-001", "tasks": unsafe_tasks}]}

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        project_provider=adversarial_project_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/projects", headers=running.authed_headers())
        assert response.status == 200
        raw_body = response.read_body.decode("utf-8")  # type: ignore[attr-defined]
        assert "PROJECT-SECRET-MARKER" not in raw_body
        assert "tasks" not in _body_json(response)["projects"][0]
    finally:
        _stop(running)


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
    assert set(payload["active_invocation"]) <= ACTIVE_INVOCATION_FIELDS
    assert payload["active_invocation"]["status"] == "running"
    assert "prompt" not in payload["active_invocation"]
    assert payload["source_external_id"] == "acme/example#1"
    assert payload["requested_performance_mode"] == "fast"
    assert payload["effective_performance_mode"] == "standard"
    assert payload["performance_model_profile"] == "economy"
    assert payload["risk_assessment_enabled"] is False
    assert payload["verification"] == {
        "passed": True,
        "check_count": 2,
        "failed_check_count": 0,
        "coverage_change": 1.5,
    }
    assert payload["artifacts"] == ["verification.json", "work-item.json"]
    assert payload["escalation"]["reason_code"] == "RISK_APPROVAL"
    assert "raw_comment_body" not in payload["escalation"]
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


def test_sanitize_run_detail_keeps_copilot_usage_value_and_pi_estimate_separate() -> None:
    payload = sanitize_run_detail(
        {
            "run_id": "run-001",
            "usage": {
                "total_nano_aiu": 38_483_200_000,
                "list_price_estimate_usd": 0.42,
            },
        }
    )

    assert payload["usage"]["usage_value_usd"] == pytest.approx(0.384832)
    assert payload["usage"]["list_price_estimate_usd"] == pytest.approx(0.42)


def test_sanitize_run_detail_omits_list_price_estimate_when_not_reported() -> None:
    payload = sanitize_run_detail({"run_id": "run-001", "usage": {"total_nano_aiu": 1}})

    assert "list_price_estimate_usd" not in payload["usage"]


def test_non_object_active_invocation_is_dropped() -> None:
    sanitized = sanitize_run_detail(
        {
            "run_id": "run-001",
            "active_invocation": [
                {
                    "reasoning": "must-not-be-exposed",
                    "failure_reason": "must-not-be-exposed",
                }
            ],
        }
    )

    assert "active_invocation" not in sanitized


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
            "usage": {**run["usage"], "api_key": SECRET_MARKER},
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
        "usage": {**detail["usage"], "api_key": SECRET_MARKER},
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
        "invocations": [
            {
                **invocation,
                "failure_reason": SECRET_MARKER,
                "usage": {
                    **invocation["usage"],
                    "api_key": SECRET_MARKER,
                    "model_usage": [
                        {
                            "model": "fake-model",
                            "input_tokens": 100,
                            "api_key": SECRET_MARKER,
                        }
                    ],
                },
            }
            for invocation in detail["invocations"]
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
            assert "usage" not in run
            assert "invocation_count" not in run
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
        assert set(payload) <= RUN_DETAIL_FIELDS | {
            "active_invocation",
            "attempts",
            "invocations",
        }
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
        for invocation in payload["invocations"]:
            assert set(invocation) <= INVOCATION_FIELDS
            assert "failure_reason" not in invocation
            assert SECRET_MARKER not in json.dumps(invocation)
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


def test_new_dashboard_fields_reject_untrusted_values() -> None:
    payload = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "source_external_id": SECRET_MARKER,
            "performance_model_profile": "secret profile/invalid",
            "verification": {
                "passed": True,
                "check_count": 1,
                "failed_check_count": 0,
                "stdout": SECRET_MARKER,
            },
            "artifacts": ["verification.json", "patch.diff", SECRET_MARKER],
            "escalation": {
                "status": "NOTIFIED",
                "target_type": "ISSUE",
                "comment_url": "file:///tmp/private",
                "reason_code": "RISK_APPROVAL",
                "resume_classification": "RISK_APPROVAL",
                "waiting_for_human": True,
                "episode_number": 1,
                "reopen_count": 0,
                "accepted_reply_count": 0,
                "last_responder": "invalid responder/name",
                "last_action": "RESUME",
                "is_resumed": False,
                "raw_comment_body": SECRET_MARKER,
            },
        }
    )

    assert "source_external_id" not in payload
    assert "performance_model_profile" not in payload
    assert "stdout" not in payload["verification"]
    assert payload["artifacts"] == ["verification.json"]
    assert "comment_url" not in payload["escalation"]
    assert "last_responder" not in payload["escalation"]
    assert "raw_comment_body" not in payload["escalation"]
    assert SECRET_MARKER not in json.dumps(payload)


def test_sanitize_run_detail_preserves_resumed_escalation_status() -> None:
    """Dashboard sanitizer must preserve the valid RESUMED escalation status."""
    payload = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "escalation": {
                "status": "RESUMED",
                "target_type": "PULL_REQUEST",
                "comment_url": "https://github.com/acme/example/pull/1#issuecomment-1",
                "reason_code": "RISK_APPROVAL",
                "resume_classification": "RISK_APPROVAL",
                "waiting_for_human": False,
                "episode_number": 1,
                "reopen_count": 1,
                "accepted_reply_count": 1,
                "is_resumed": True,
            },
        }
    )
    assert payload["escalation"]["status"] == "RESUMED"
    assert payload["escalation"]["is_resumed"] is True

    # Unknown or invalid status must be dropped
    invalid_payload = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "escalation": {
                "status": "NOT_A_VALID_STATUS",
            },
        }
    )
    assert "status" not in invalid_payload["escalation"]


def test_run_guidance_is_reconstructed_from_safe_reason_code() -> None:
    payload = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "guidance": {
                "status": SECRET_MARKER,
                "reason_code": "REVIEW_IMPASSE",
                "summary": SECRET_MARKER,
                "next_action": SECRET_MARKER,
                "artifact": SECRET_MARKER,
                "finding_count": 1,
                "finding_ids": ["review-correctness-1234", SECRET_MARKER],
                "category_counts": {"CORRECTNESS": 1, SECRET_MARKER: 99},
            },
        }
    )

    guidance = payload["guidance"]
    assert guidance["status"] == "ACTION_REQUIRED"
    assert guidance["artifact"] == "review-impasse.json"
    assert guidance["finding_count"] == 1
    assert "decision_count" not in guidance
    assert guidance["finding_ids"] == ["review-correctness-1234"]
    assert guidance["category_counts"] == {"CORRECTNESS": 1}
    assert SECRET_MARKER not in json.dumps(guidance)


def test_run_guidance_unresolved_decisions_sanitized_with_bounded_decision_count() -> None:
    secret_decision = "Choice between SQLite and PostgreSQL"
    payload = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "guidance": {
                "status": SECRET_MARKER,
                "reason_code": "UNRESOLVED_DECISIONS",
                "summary": f"{secret_decision}; {SECRET_MARKER}",
                "next_action": SECRET_MARKER,
                "artifact": SECRET_MARKER,
                "finding_count": 0,
                "decision_count": 2,
            },
        }
    )

    guidance = payload["guidance"]
    assert guidance["status"] == "ACTION_REQUIRED"
    assert guidance["reason_code"] == "UNRESOLVED_DECISIONS"
    assert guidance["summary"] == "The execution plan has unresolved architectural decisions."
    assert (
        guidance["next_action"]
        == "Inspect execution-plan.json, resolve the decisions, then start a replacement run."
    )
    assert guidance["artifact"] == "execution-plan.json"
    assert guidance["decision_count"] == 2
    assert "finding_count" not in guidance
    assert secret_decision not in json.dumps(guidance)
    assert SECRET_MARKER not in json.dumps(guidance)

    # Test out-of-bounds decision count
    out_of_bounds = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "guidance": {
                "reason_code": "UNRESOLVED_DECISIONS",
                "decision_count": 999,
            },
        }
    )
    assert "decision_count" not in out_of_bounds["guidance"]

    reply_enabled = sanitize_run_detail(
        {
            **FIXTURE_DETAILS["run-001"],
            "guidance": {
                "reason_code": "UNRESOLVED_DECISIONS",
                "decision_count": 1,
                "next_action": "Reply with complete numbered decisions on the escalation thread.",
            },
        }
    )
    assert (
        reply_enabled["guidance"]["next_action"]
        == "Reply with complete numbered decisions on the escalation thread."
    )


def test_dashboard_ui_labeling_for_unresolved_decisions_and_finding_count() -> None:
    js = dashboard_assets.APP_JS

    # One condition decides both the label and the value, so they cannot drift apart.
    assert function_source(js, "isDecisionGuidance") == (
        "function isDecisionGuidance(guidance) { "
        'return guidance.reason_code === "UNRESOLVED_DECISIONS" || '
        "guidance.decision_count !== undefined; }"
    )
    assert function_source(js, "countField") == (
        "function countField(guidance) { if (isDecisionGuidance(guidance)) { "
        'return ["Decision count", guidance.decision_count]; } '
        'return ["Finding count", guidance.finding_count]; }'
    )
    assert normalized(js).count('"UNRESOLVED_DECISIONS"') == 1
    assert "countField(guidance)," in function_source(js, "guidanceFields")


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


@pytest.mark.parametrize(
    "forbidden",
    [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "createContextualFragment",
        "srcdoc",
        "document.write",
        "eval(",
        "new Function(",
    ],
)
def test_app_js_never_uses_dangerous_rendering_apis(forbidden: str) -> None:
    assert forbidden not in dashboard_assets.APP_JS


def test_app_js_renders_server_text_with_textcontent() -> None:
    js = dashboard_assets.APP_JS
    assert "textContent" in js
    assert "Active invocation" in js
    assert "model.status" in js


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


def test_dashboard_health_sanitization_omits_workspace_paths() -> None:
    """Review Finding 5: Dashboard health JSON responses must omit absolute workspace paths."""
    from software_agent_factory.dashboard.sanitize import sanitize_health
    from software_agent_factory.models import WorkflowState, utc_now
    from software_agent_factory.observability import OperationalHealthReport, StaleRunFinding

    report = OperationalHealthReport(
        generated_at=utc_now(),
        stale_after_seconds=900.0,
        max_scanned_runs=100,
        total_runs=1,
        scanned_runs=1,
        scan_truncated=False,
        unreadable_runs=0,
        degraded=False,
        lock_check_supported=True,
        locks_checked=1,
        workspaces_checked=1,
        stale_runs=[
            StaleRunFinding(
                run_id="run-secret",
                work_item_id="task-s",
                state=WorkflowState.IMPLEMENTING,
                idle_seconds=1200.0,
                workspace_path="/Users/secret/path/to/workspaces/ws-secret",
            )
        ],
    )
    sanitized = sanitize_health(report)
    assert sanitized is not None
    assert len(sanitized["stale_runs"]) == 1
    stale_finding = sanitized["stale_runs"][0]
    assert stale_finding["run_id"] == "run-secret"
    assert stale_finding["work_item_id"] == "task-s"
    assert "workspace_path" not in stale_finding
    assert "/Users/secret" not in json.dumps(sanitized)


def test_dashboard_api_summary_sanitizes_health_response(tmp_path: Path) -> None:
    """Review Finding 5: /api/summary strips absolute workspace paths from operational health."""
    from software_agent_factory.models import WorkflowState, utc_now
    from software_agent_factory.observability import OperationalHealthReport, StaleRunFinding

    def health_with_secret_path() -> Any:
        return OperationalHealthReport(
            generated_at=utc_now(),
            stale_after_seconds=900.0,
            max_scanned_runs=100,
            total_runs=1,
            scanned_runs=1,
            scan_truncated=False,
            unreadable_runs=0,
            degraded=False,
            lock_check_supported=True,
            locks_checked=1,
            workspaces_checked=1,
            stale_runs=[
                StaleRunFinding(
                    run_id="run-secret-2",
                    work_item_id="task-s2",
                    state=WorkflowState.IMPLEMENTING,
                    idle_seconds=1200.0,
                    workspace_path="/private/var/folders/secret/ws-leak",
                )
            ],
        )

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=fake_run_detail_provider,
        health_provider=health_with_secret_path,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert response.status == 200
        payload = _body_json(response)
        health = payload.get("health")
        assert health is not None
        assert len(health["stale_runs"]) == 1
        assert "workspace_path" not in health["stale_runs"][0]
        assert "/private/var/folders/secret" not in json.dumps(payload)
    finally:
        _stop(running)


def test_usage_sanitizer_keeps_only_non_negative_numeric_fields() -> None:
    sanitized = sanitize_usage(
        {
            "current_model": 123,
            "input_tokens": True,
            "output_tokens": -1,
            "reasoning_tokens": 5,
            "api_key": SECRET_MARKER,
            "model_usage": [
                {
                    "model": "gpt-5.6-sol",
                    "input_tokens": 100,
                    "output_tokens": False,
                    "api_key": SECRET_MARKER,
                }
            ],
        }
    )

    assert sanitized == {
        "reasoning_tokens": 5,
    }


def test_usage_sanitizer_converts_nano_aiu_to_usd_value() -> None:
    sanitized = sanitize_usage({"total_nano_aiu": 38_483_200_000})

    assert sanitized == {
        "total_nano_aiu": 38_483_200_000,
        "usage_value_usd": pytest.approx(0.384832),
    }


def test_usage_sanitizer_passes_through_list_price_estimate_as_a_separate_field() -> None:
    sanitized = sanitize_usage({"total_nano_aiu": 38_483_200_000, "list_price_estimate_usd": 0.42})

    assert sanitized == {
        "total_nano_aiu": 38_483_200_000,
        "usage_value_usd": pytest.approx(0.384832),
        "list_price_estimate_usd": pytest.approx(0.42),
    }


def test_usage_sanitizer_omits_list_price_estimate_when_not_reported() -> None:
    sanitized = sanitize_usage({"input_tokens": 10})

    assert "list_price_estimate_usd" not in sanitized


def test_dashboard_explains_and_renders_usage_value() -> None:
    js = dashboard_assets.APP_JS

    assert "1 AI credit = $0.01" in js
    assert "Your invoice charge may be lower or zero" in js
    assert "AI usage value (USD)" in js


def test_sanitize_invocation_keeps_list_price_estimate_beside_unchanged_usage_value() -> None:
    invocation = sanitize_invocation(
        {
            "invocation_number": 1,
            "usage": {"total_nano_aiu": 38_483_200_000, "list_price_estimate_usd": 0.42},
        }
    )

    assert invocation["usage"]["usage_value_usd"] == pytest.approx(0.384832)
    assert invocation["usage"]["list_price_estimate_usd"] == pytest.approx(0.42)


def test_sanitize_project_model_usage_keeps_list_price_estimate_beside_unchanged_usage_value() -> (
    None
):
    project = sanitize_project(
        {
            "project_id": "project-001",
            "models": [
                {
                    "model": "fake-model",
                    "usage": {"total_nano_aiu": 38_483_200_000, "list_price_estimate_usd": 0.42},
                }
            ],
        }
    )

    usage = project["models"][0]["usage"]
    assert usage["usage_value_usd"] == pytest.approx(0.384832)
    assert usage["list_price_estimate_usd"] == pytest.approx(0.42)


def test_dashboard_list_price_estimate_falls_back_to_unknown_and_never_replaces_usage_value() -> (
    None
):
    """No JS runner is available, so pin the exact rendering helper source and
    the row wiring instead of grepping for loose substrings."""
    js = dashboard_assets.APP_JS

    assert function_source(js, "displayListPriceEstimate") == (
        "function displayListPriceEstimate(value) { "
        'if (!isFiniteNumber(value)) { return "unknown"; } return displayUsd(value); }'
    )
    # Every usage table puts the AI usage value, then the premium-request cost, then
    # the list-price estimate in adjacent cells, matching their header order; the
    # estimate is read from its own field, never from usage_value_usd.
    assert (
        "appendCell(row, displayUsd(usage.usage_value_usd)); "
        "appendCell(row, usage.total_premium_request_cost); "
        "appendCell(row, displayListPriceEstimate(usage.list_price_estimate_usd));"
    ) in function_source(js, "modelRow")
    assert (
        "displayUsd(usage.usage_value_usd), "
        "usage.total_premium_request_cost, "
        "displayListPriceEstimate(usage.list_price_estimate_usd)"
    ) in function_source(js, "invocationRowSpec")
    code = normalized(js)
    assert "displayListPriceEstimate(usage.usage_value_usd)" not in code
    assert "displayUsd(usage.list_price_estimate_usd)" not in code


def test_dashboard_run_detail_lists_list_price_estimate_after_the_usage_value_rows() -> None:
    usage_fields = function_source(dashboard_assets.APP_JS, "usageFields")

    value_row = '["AI usage value (USD)", displayUsd(usage.usage_value_usd)],'
    estimate_row = (
        '["List-price estimate", displayListPriceEstimate(usage.list_price_estimate_usd)]'
    )
    assert value_row in usage_fields
    assert estimate_row in usage_fields
    assert usage_fields.index(value_row) < usage_fields.index(estimate_row)


def test_dashboard_usage_tables_end_with_the_list_price_estimate_column() -> None:
    """Both usage tables list the AI usage value, the premium-request column, then
    the estimate last, in the order their row builders fill the cells."""
    html = dashboard_assets.render_index_html(token="tok")
    js = dashboard_assets.APP_JS

    invocations_head = re.search(
        r'<table id="invocations-table">\s*<thead>(.*?)</thead>', html, flags=re.DOTALL
    )
    assert invocations_head is not None
    invocation_headers = re.findall(r'<th scope="col">([^<]*)</th>', invocations_head.group(1))
    assert invocation_headers[-3:] == [
        "AI usage value (USD)",
        "Premium-request cost",
        "List-price estimate",
    ]
    model_headers = re.search(r"const MODEL_HEADERS = \[(.*?)\];", js, flags=re.DOTALL)
    assert model_headers is not None
    assert re.findall(r'"([^"]*)"', model_headers.group(1))[-3:] == [
        "AI usage value (USD)",
        "Premium-request units",
        "List-price estimate",
    ]


def test_dashboard_totals_show_list_price_estimate_row_with_unknown_fallback() -> None:
    """renderTotals surfaces the List-price estimate row with the same
    ``displayListPriceEstimate`` "unknown" fallback the detail/invocation
    views use, rather than the generic renderer's ``[object Object]`` for a
    field nested two levels deep (``metrics.usage.list_price_estimate_usd``)."""
    js = dashboard_assets.APP_JS

    assert function_source(js, "withListPriceEstimate") == (
        "function withListPriceEstimate(metrics) { return { ...metrics, "
        "list_price_estimate_usd: "
        "displayListPriceEstimate(metrics.usage?.list_price_estimate_usd) }; }"
    )
    assert "totals.metrics = withListPriceEstimate(totals.metrics);" in function_source(
        js, "renderTotals"
    )


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


def test_dashboard_shares_single_scan_across_refresh_cycle(tmp_path: Path) -> None:
    from software_agent_factory.models import FactoryRun, WorkflowState
    from software_agent_factory.observability import (
        RunScanCache,
        build_monitoring_snapshot,
        build_operational_health,
    )
    from software_agent_factory.store import FileRunStore

    store = FileRunStore(tmp_path / "data")
    for index in range(1, 4):
        run = FactoryRun(
            id=f"shared-run-{index:03d}",
            work_item_id=f"WI-{index:03d}",
            state=WorkflowState.DONE,
        )
        store.save_run(run)

    scan_cache = RunScanCache(store, ttl=2.0)
    original_load_run = store.load_run
    load_calls = 0

    def counting_load_run(run_id: str) -> FactoryRun:
        nonlocal load_calls
        load_calls += 1
        return original_load_run(run_id)

    store.load_run = counting_load_run  # type: ignore[assignment]

    def cached_snapshot_provider(*, limit: int, offset: int) -> Any:
        return build_monitoring_snapshot(
            store,
            limit=limit,
            offset=offset,
            scan=scan_cache.get_scan(),
        )

    def cached_health_provider() -> Any:
        return build_operational_health(
            store,
            data_dir=tmp_path / "data",
            scan=scan_cache.get_scan(),
        )

    def detail_provider(run_id: str) -> dict[str, Any] | None:
        try:
            return store.load_run(run_id).model_dump(mode="json")
        except (OSError, ValueError):
            return None

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=cached_snapshot_provider,
        health_provider=cached_health_provider,
        run_detail_provider=detail_provider,
    )
    running = _start(config)
    try:
        # A dashboard refresh cycle makes /api/summary (which queries snapshot + health)
        # and /api/runs (which queries snapshot)
        summary_response = running.request("GET", "/api/summary", headers=running.authed_headers())
        assert summary_response.status == 200
        summary_payload = _body_json(summary_response)
        assert summary_payload["counts"]["succeeded"] == 3

        runs_response = running.request(
            "GET", "/api/runs?limit=10&offset=0", headers=running.authed_headers()
        )
        assert runs_response.status == 200
        runs_payload = _body_json(runs_response)
        assert len(runs_payload["runs"]) == 3

        # Exactly 3 load_run calls (each of the 3 runs loaded once during the single shared scan),
        # instead of 9 loads across summary snapshot, summary health, and runs endpoint.
        assert load_calls == 3
        assert scan_cache.misses == 1
        assert scan_cache.hits == 2
    finally:
        _stop(running)


# ---------------------------------------------------------------------------
# Log injection: request paths reach the log only after sanitisation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/api/runs?token=secret", "/api/runs"),
        ("/api/runs\r\nINFO forged line", "/api/runs??INFO forged line"),
        ("/api/\x00bin\x1b[31m", "/api/?bin?[31m"),
        ("/über", "/?ber"),
        ("/" + "a" * 500, ("/" + "a" * 500)[:_MAX_LOGGED_PATH_LENGTH]),
    ],
    ids=[
        "strips-query",
        "strips-crlf",
        "strips-control-chars",
        "strips-non-ascii",
        "bounds-length",
    ],
)
def test_log_safe_path_strips_query_control_chars_and_bounds_length(
    raw: str, expected: str
) -> None:
    assert _log_safe_path(raw) == expected


def test_forged_request_path_reaches_log_as_one_sanitised_record(
    running_server: RunningServer, caplog: pytest.LogCaptureFixture
) -> None:
    # End-to-end: the wiring in log_message, not just the helper. A bare CR/LF
    # cannot survive an HTTP request line, but ESC can, and self.path is logged
    # before any unquote, so the raw byte must be sent over a socket.
    request = (
        b"GET /api/runs\x1b[2Jforged?token=x HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{running_server.port}\r\n".encode()
        + f"{TOKEN_HEADER}: {running_server.token}\r\n".encode()
        + b"Connection: close\r\n\r\n"
    )
    with caplog.at_level(logging.INFO, logger="software_agent_factory.dashboard"):
        with socket.create_connection(("127.0.0.1", running_server.port), timeout=5) as sock:
            sock.sendall(request)
            while sock.recv(4096):
                pass
    records = [r for r in caplog.records if r.name == "software_agent_factory.dashboard"]
    access = [r for r in records if r.getMessage().startswith("GET ")]
    assert len(access) == 1
    message = access[0].getMessage()
    assert "\x1b" not in message
    assert "token=" not in message
    assert "/api/runs?[2Jforged" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GET", "GET"),
        (None, ""),
        ("\x1b[2JGET", "?[2JGET"),
        ("A" * 40, ("A" * 40)[:_MAX_LOGGED_METHOD_LENGTH]),
    ],
    ids=["plain", "none", "strips-escape", "bounds-length"],
)
def test_log_safe_method_strips_control_chars_and_bounds_length(
    raw: str | None, expected: str
) -> None:
    assert _log_safe_method(raw) == expected


def test_unknown_method_with_escape_reaches_log_sanitised(
    running_server: RunningServer, caplog: pytest.LogCaptureFixture
) -> None:
    # An unknown method is rejected by BaseHTTPRequestHandler before any token
    # check, and logged through log_message on the way out.
    # http.client refuses control characters in the method, so speak raw HTTP.
    request = (
        b"\x1b[2JGET /api/health HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{running_server.port}\r\n".encode()
        + b"Connection: close\r\n\r\n"
    )
    with caplog.at_level(logging.INFO, logger="software_agent_factory.dashboard"):
        with socket.create_connection(("127.0.0.1", running_server.port), timeout=5) as sock:
            sock.sendall(request)
            while sock.recv(4096):
                pass
    messages = [
        r.getMessage() for r in caplog.records if r.name == "software_agent_factory.dashboard"
    ]
    assert messages
    assert all("\x1b" not in m for m in messages)


def test_run_id_with_trailing_newline_is_rejected() -> None:
    from software_agent_factory.dashboard.snapshot import is_valid_run_id

    assert is_valid_run_id("abc")
    assert not is_valid_run_id("abc\n")


def test_malformed_request_line_gets_400_and_logs_without_crashing(
    running_server: RunningServer, caplog: pytest.LogCaptureFixture
) -> None:
    # parse_request rejects the line before self.path exists; the logging hook
    # runs on that error path and must not raise.
    with caplog.at_level(logging.INFO, logger="software_agent_factory.dashboard"):
        with socket.create_connection(("127.0.0.1", running_server.port), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/x\r\n\r\n")
            chunks = []
            while chunk := sock.recv(4096):
                chunks.append(chunk)
    raw = b"".join(chunks)
    assert b"400" in raw and b"Bad request" in raw


# --------------------------------------------------------------------------
# Test helpers for the asset tests below (no JS runner, ADR-016)
# --------------------------------------------------------------------------


def test_function_source_returns_the_balanced_body_without_comments() -> None:
    js = """
    function first(a) {
      // a brace in a comment: }
      const text = "} not a close {";
      if (a) { return 'x'; }
      return text;
    }
    function second() { return 1; }
    """
    assert function_source(js, "first") == (
        "function first(a) { "
        "const text = \"} not a close {\"; if (a) { return 'x'; } return text; }"
    )
    assert function_source(js, "second") == "function second() { return 1; }"


def test_function_source_raises_when_the_function_is_missing() -> None:
    with pytest.raises(AssertionError, match="missing not found"):
        function_source("function other() { return 1; }", "missing")


def test_function_source_raises_when_braces_do_not_balance() -> None:
    with pytest.raises(AssertionError, match="unbalanced"):
        function_source("function broken() { if (a) { return 1; }", "broken")


# --------------------------------------------------------------------------
# Shell, navigation and hash routes (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------

_INDEX_HTML = dashboard_assets.render_index_html(token="fixture-token")

#: (section id, heading id) for the one section each view renders into.
_VIEW_SECTIONS = (
    ("view-runs", "runs-heading"),
    ("view-run", "detail-heading"),
    ("view-compare", "compare-heading"),
    ("view-projects", "projects-heading"),
    ("view-health", "health-heading"),
)

#: Every table the page ships is named, so each one can be checked on its own.
_TABLE_IDS = re.findall(r'<table\s+id="([^"]+)"', _INDEX_HTML)


def test_index_html_has_a_main_nav_landmark_with_the_four_links() -> None:
    nav = re.search(r'<nav\s+aria-label="Main">(.*?)</nav>', _INDEX_HTML, flags=re.DOTALL)
    assert nav is not None
    links = re.findall(r'<a\s+href="(#[a-z]+)"\s+data-route="([a-z]+)">([^<]+)</a>', nav.group(1))
    assert links == [
        ("#runs", "runs", "Runs"),
        ("#compare", "compare", "Compare"),
        ("#projects", "projects", "Projects"),
        ("#health", "health", "Health"),
    ]


@pytest.mark.parametrize(("section_id", "heading_id"), _VIEW_SECTIONS)
def test_each_view_is_one_hidden_section_with_a_focusable_heading(
    section_id: str, heading_id: str
) -> None:
    section = rf'<section\s+id="{section_id}"\s+aria-labelledby="{heading_id}"\s+hidden>'
    assert re.search(section, _INDEX_HTML)
    assert re.search(rf'<h1\s+id="{heading_id}"\s+tabindex="-1">', _INDEX_HTML)


def test_totals_stay_on_the_runs_view() -> None:
    runs_view = _INDEX_HTML.split('<section id="view-runs"')[1].split("</section>")[0]
    assert 'id="totals-body"' in runs_view
    assert 'id="runs-body"' in runs_view


def test_script_is_deferred_in_the_head_and_not_in_the_body() -> None:
    head, body = _INDEX_HTML.split("</head>")
    assert re.search(
        r'<script\s+defer\s+src="/assets/app\.js\?token=fixture-token"></script>', head
    )
    assert "<script" not in body


def test_route_parser_pins_every_route_shape() -> None:
    js = dashboard_assets.APP_JS
    parser = function_source(js, "parseRoute")
    # A run needs exactly one id part; compare takes none or a pair of ids.
    assert 'name === "run" && parts.length === 2' in parser
    assert 'return { view: "run", runId: parts[1] };' in parser
    assert 'name === "compare" && (parts.length === 1 || parts.length === 3)' in parser
    assert 'return { view: "compare" };' in parser
    assert "parts.length === 1 && SIMPLE_VIEWS.includes(name)" in parser
    assert "return { view: name };" in parser
    assert parser.endswith("return null; }")
    assert 'const SIMPLE_VIEWS = ["runs", "projects", "health"];' in js


def test_an_unknown_hash_redirects_in_apply_route_and_resolve_route_has_no_side_effects() -> None:
    js = dashboard_assets.APP_JS
    resolver = function_source(js, "resolveRoute")
    assert resolver == (
        "function resolveRoute() { "
        'return parseRoute(globalThis.location.hash) || { view: "runs" }; }'
    )
    assert "history" not in resolver
    assert function_source(js, "isUnknownHash") == (
        "function isUnknownHash(hash) { "
        'return hash !== "" && hash !== "#" && parseRoute(hash) === null; }'
    )
    apply_route = function_source(js, "applyRoute")
    assert apply_route.startswith(
        "function applyRoute(moveFocus) { if (isUnknownHash(globalThis.location.hash)) { "
        'globalThis.history.replaceState(null, "", "#runs"); } const route = resolveRoute();'
    )


def test_a_hash_change_applies_the_route_and_moves_focus() -> None:
    bindings = function_source(dashboard_assets.APP_JS, "bindControls")
    assert (
        'globalThis.addEventListener("hashchange", function () { applyRoute(true); });' in bindings
    )


def test_a_run_row_click_navigates_to_the_encoded_run_hash() -> None:
    js = dashboard_assets.APP_JS
    assert function_source(js, "navigateToRun") == (
        "function navigateToRun(runId) { "
        'globalThis.location.hash = "run/" + encodeURIComponent(runId); }'
    )


def test_app_js_validates_hash_run_ids_with_the_server_pattern() -> None:
    from software_agent_factory.dashboard import snapshot

    js = dashboard_assets.APP_JS
    # JS \w without the u flag is exactly [A-Za-z0-9_], the server's character set.
    js_pattern = snapshot._RUN_ID_PATTERN.pattern.replace("A-Za-z0-9_", r"\w")
    assert f"const RUN_ID_PATTERN = /{js_pattern}/;" in js
    assert "RUN_ID_PATTERN.test(runId)" in function_source(js, "prepareRunView")
    assert "RUN_ID_PATTERN.test(route.runId)" in function_source(js, "validRunId")


def test_the_run_view_heading_names_the_run_or_reports_an_unknown_one() -> None:
    prepare = function_source(dashboard_assets.APP_JS, "prepareRunView")
    assert prepare == (
        "function prepareRunView(runId) { "
        'const heading = document.getElementById("detail-heading"); '
        "if (!RUN_ID_PATTERN.test(runId)) { heading.textContent = VIEWS.run.label; "
        'setDetailStatus("Unknown run"); return; } '
        'heading.textContent = "Run " + runId; setDetailStatus("Loading\\u2026"); }'
    )


def test_a_title_names_the_view_and_the_run() -> None:
    js = dashboard_assets.APP_JS
    assert 'const TITLE_SUFFIX = " \\u2014 Factory dashboard";' in js
    title = function_source(js, "routeTitle")
    assert "VIEWS[route.view].label + TITLE_SUFFIX" in title
    assert '"Run " + route.runId : "Unknown run"' in title
    assert "document.title = routeTitle(route);" in function_source(js, "applyRoute")


def test_app_js_marks_the_active_link_and_focuses_the_heading() -> None:
    js = dashboard_assets.APP_JS
    mark = function_source(js, "markNavLink")
    assert 'link.setAttribute("aria-current", "page")' in mark
    assert 'link.removeAttribute("aria-current")' in mark
    assert 'link.getAttribute("data-route") === VIEWS[name].nav' in function_source(js, "showView")
    assert (
        "if (moveFocus) { document.getElementById(VIEWS[route.view].heading).focus(); }"
        in function_source(js, "applyRoute")
    )


def test_loading_and_empty_states() -> None:
    js = dashboard_assets.APP_JS
    assert '"No runs yet."' in function_source(js, "renderRunsStatus")
    assert 'setDetailStatus("Loading\\u2026");' in function_source(js, "prepareRunView")
    assert re.search(r'<p\s+id="runs-status">Loading&hellip;</p>', _INDEX_HTML)


def test_empty_run_table_stays_hidden_until_rows_arrive() -> None:
    assert re.search(
        r'<div\s+class="table-wrap"\s+hidden>\s*<table\s+id="runs-table">', _INDEX_HTML
    )
    assert (
        'document.querySelector("#view-runs .table-wrap").hidden = shown === 0;'
        in function_source(dashboard_assets.APP_JS, "renderRunsStatus")
    )


def test_compare_view_is_a_placeholder_card() -> None:
    compare = _INDEX_HTML.split('<section id="view-compare"')[1].split("</section>")[0]
    assert "Compare two runs &mdash; coming soon" in compare


def test_every_static_table_has_an_id() -> None:
    assert _TABLE_IDS
    assert len(_TABLE_IDS) == len(re.findall(r"<table\b", _INDEX_HTML))


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_each_static_table_sits_in_a_table_wrap(table_id: str) -> None:
    wrapped = rf'<div\s+class="table-wrap"(?:\s+hidden)?>\s*<table\s+id="{table_id}">'
    assert re.search(wrapped, _INDEX_HTML)


def test_every_table_built_in_the_script_is_wrapped() -> None:
    js = dashboard_assets.APP_JS
    assert 'element("div", "table-wrap")' in function_source(js, "wrapTable")
    callers = re.findall(r"(\w+)\((?:tasksTable|modelsTable)\(", js)
    assert callers
    assert set(callers) == {"wrapTable"}


def test_table_wrap_scrolls_sideways_and_the_page_never_does() -> None:
    css = dashboard_assets.STYLE_CSS
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto;", css)
    assert re.search(r"main\s*\{[^}]*min-width:\s*0;", css)
    assert re.search(r"dd\s*\{[^}]*overflow-wrap:\s*anywhere;", css)
    assert "overflow-x: hidden" not in css
    assert "overflow: hidden" not in css


def test_sidebar_collapses_to_a_top_bar_under_900px() -> None:
    css = dashboard_assets.STYLE_CSS
    assert re.search(r"@media\s*\(max-width:\s*900px\)", css)


def test_cards_use_a_12_to_16_pixel_radius_on_the_surface_token() -> None:
    rule = re.search(r"\.card\s*\{([^}]*)\}", dashboard_assets.STYLE_CSS)
    assert rule is not None
    assert "background: var(--surface);" in rule.group(1)
    radius = re.search(r"border-radius:\s*(\d+)px;", rule.group(1))
    assert radius is not None
    assert 12 <= int(radius.group(1)) <= 16


def test_active_nav_link_uses_the_accent_token() -> None:
    rule = re.search(r'nav a\[aria-current="page"\]\s*\{([^}]*)\}', dashboard_assets.STYLE_CSS)
    assert rule is not None
    assert "background: var(--accent);" in rule.group(1)


# --------------------------------------------------------------------------
# Refresh dispatcher and connection notices (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------


def test_one_poll_refreshes_only_the_open_view() -> None:
    js = dashboard_assets.APP_JS
    code = normalized(js)
    assert "const POLL_INTERVAL_MS = 5000;" in code
    assert code.count("setInterval(") == 1
    assert "globalThis.setInterval(pollView, POLL_INTERVAL_MS);" in code
    refreshers = re.search(r"const REFRESHERS = \{(.*?)\n  \};", js, re.DOTALL)
    assert refreshers is not None
    assert re.findall(r"^    (\w+): function", refreshers.group(1), re.MULTILINE) == [
        "runs",
        "run",
        "projects",
        "health",
    ]
    assert "REFRESHERS[view]" in function_source(js, "refreshView")


def test_start_applies_the_theme_then_wires_controls_then_opens_the_route() -> None:
    start = function_source(dashboard_assets.APP_JS, "start")
    order = [
        "applyStoredTheme();",
        "bindControls();",
        "seedBackHistory();",
        "applyRoute(false);",
        "globalThis.setInterval(pollView, POLL_INTERVAL_MS);",
    ]
    positions = [start.index(call) for call in order]
    assert positions == sorted(positions)


def test_a_route_change_refreshes_the_view_it_opens() -> None:
    apply_route = function_source(dashboard_assets.APP_JS, "applyRoute")
    assert apply_route.endswith("refreshView(); }")


def test_dirty_guard_checks_focused_fields_and_open_dialogs() -> None:
    js = dashboard_assets.APP_JS
    guard = function_source(js, "isDirty")
    assert "region.contains(active)" in guard
    assert 'active.matches("input, textarea, select")' in guard
    assert 'region.querySelector("dialog[open]") !== null' in guard
    refresh_view = function_source(js, "refreshView")
    assert "isDirty(document.getElementById(VIEWS[view].section))" in refresh_view


def test_refresh_patches_rows_and_text_in_place() -> None:
    js = dashboard_assets.APP_JS
    for name in ("syncRows", "syncList", "syncDefinitionList", "setText"):
        assert f"function {name}(" in js
    assert "if (node.textContent !== text)" in normalized(js)
    assert "signature !== lastProjectsSignature" in function_source(js, "renderProjectsOnChange")


@pytest.mark.parametrize(
    ("renderer", "patchers"),
    [
        ("renderRuns", ["syncRows("]),
        ("renderDetail", ["syncDefinitionList(", "syncRows("]),
    ],
)
def test_the_live_views_patch_in_place_and_never_clear_their_content(
    renderer: str, patchers: list[str]
) -> None:
    source = function_source(dashboard_assets.APP_JS, renderer)
    for patcher in patchers:
        assert patcher in source
    assert "clearChildren(" not in source
    assert "replaceChildren(" not in source


def test_a_failed_refresh_leaves_the_views_as_they_were() -> None:
    js = dashboard_assets.APP_JS
    failure = function_source(js, "onRefreshFailure")
    assert "clearChildren" not in failure
    assert "replaceChildren" not in failure
    assert "hidden" not in failure
    assert "renderNotice();" in failure
    # The notice writes one status line and touches nothing else.
    assert "getElementById" not in failure
    assert 'getElementById("notice")' in function_source(js, "renderNotice")


def test_notices_live_in_one_polite_status_region() -> None:
    assert re.search(r'<p\s+id="notice"\s+role="status"\s+aria-live="polite"></p>', _INDEX_HTML)
    assert _INDEX_HTML.count('role="status"') == 1


def test_notice_texts_report_a_lost_connection_and_a_restart() -> None:
    message = function_source(dashboard_assets.APP_JS, "noticeMessage")
    assert (
        '"Connection lost, updated " + Math.floor((Date.now() - lastSuccessAt) / 1000) + "s ago"'
        in message
    )
    assert '"Connection lost, not updated yet"' in message
    assert '"Dashboard restarted, reload the page."' in message


def test_a_401_is_told_apart_from_a_network_error() -> None:
    js = dashboard_assets.APP_JS
    reader = function_source(js, "readResponse")
    assert "response.status === 401" in reader
    assert "apiError(ERROR_UNAUTHORIZED" in reader
    assert "response.status >= 500 ? ERROR_CONNECTION : ERROR_REQUEST" in reader
    assert "apiError(ERROR_CONNECTION" in function_source(js, "onNetworkError")
    failure = function_source(js, "onRefreshFailure")
    assert "error.kind === ERROR_UNAUTHORIZED ? ERROR_UNAUTHORIZED : ERROR_CONNECTION" in failure


def test_a_successful_refresh_clears_the_notice() -> None:
    js = dashboard_assets.APP_JS
    success = function_source(js, "onRefreshSuccess")
    assert "state.noticeKind = null;" in success
    assert "lastSuccessAt = Date.now();" in success
    assert "whenLatest(request, onRefreshSuccess)" in function_source(js, "settle")


def test_a_deep_link_seeds_history_so_back_returns_to_the_run_list() -> None:
    js = dashboard_assets.APP_JS
    seed = function_source(js, "seedBackHistory")
    assert seed.index('replaceState(null, "", "#runs")') < seed.index("pushState(")
    assert 'pushState({ seeded: true }, "", hash)' in seed
    assert "seeded" in seed.split("return;")[0]


# --------------------------------------------------------------------------
# Stale responses, in-flight polls and timeouts (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------

_REFRESH_FUNCTIONS = (
    "refreshRuns",
    "refreshTotals",
    "refreshHealth",
    "refreshProjects",
    "refreshDetail",
)


def test_a_request_records_the_view_sequence_offset_limit_and_run() -> None:
    begin = function_source(dashboard_assets.APP_JS, "beginRequest")
    for captured in (
        "seq: requests[view].seq",
        "offset: state.offset",
        "limit: state.limit",
        "runId: state.runId",
    ):
        assert captured in begin
    assert "requests[view].inFlight += 1" in begin


@pytest.mark.parametrize("name", _REFRESH_FUNCTIONS)
def test_every_refresh_drops_a_response_that_a_newer_request_overtook(name: str) -> None:
    source = function_source(dashboard_assets.APP_JS, name)
    assert "whenLatest(request, " in source


def test_a_response_is_dropped_unless_its_request_is_the_latest_for_the_view() -> None:
    js = dashboard_assets.APP_JS
    assert function_source(js, "isLatest") == (
        "function isLatest(request) { return requests[request.view].seq === request.seq; }"
    )
    assert function_source(js, "whenLatest") == (
        "function whenLatest(request, handler) { return function (value) { "
        "if (isLatest(request)) { handler(value, request); } }; }"
    )


def test_a_route_change_supersedes_older_requests_for_the_view_it_opens() -> None:
    apply_route = function_source(dashboard_assets.APP_JS, "applyRoute")
    assert apply_route.index("supersede(route.view);") < apply_route.index("refreshView();")


def test_the_runs_pager_reads_the_requested_page_and_never_the_live_state() -> None:
    js = dashboard_assets.APP_JS
    assert "renderRunsPager(payload.page || {}, runs.length, request)" in function_source(
        js, "renderRuns"
    )
    refresh = function_source(js, "refreshRuns")
    assert "encodeURIComponent(request.limit)" in refresh
    assert "encodeURIComponent(request.offset)" in refresh
    for name in ("renderRunsPager", "hasMoreRuns", "renderRunsStatus", "refreshRuns"):
        assert "state." not in function_source(js, name)


def test_a_refresh_never_ends_in_an_unhandled_rejection() -> None:
    settle = function_source(dashboard_assets.APP_JS, "settle")
    assert settle.index(".then(") < settle.index(".catch(") < settle.index(".finally(")
    assert "endRequest(request)" in settle.split(".finally(")[1]


def test_a_poll_tick_waits_while_the_open_view_has_a_request_in_flight() -> None:
    assert function_source(dashboard_assets.APP_JS, "pollView") == (
        "function pollView() { if (requests[state.view].inFlight === 0) { refreshView(); } }"
    )


def test_a_request_times_out_after_ten_seconds_and_counts_as_a_lost_connection() -> None:
    js = dashboard_assets.APP_JS
    assert "const REQUEST_TIMEOUT_MS = 10000;" in js
    fetcher = function_source(js, "apiFetch")
    assert "new AbortController()" in fetcher
    assert "controller.abort()" in fetcher
    assert "}, REQUEST_TIMEOUT_MS);" in fetcher
    assert "signal: controller.signal" in fetcher
    assert ".then(readResponse, onNetworkError)" in fetcher
    # The timer stops only once the body is read, so a stalled body times out too.
    assert fetcher.index(".then(readResponse") < fetcher.index(".finally(")
    assert "globalThis.clearTimeout(timer)" in fetcher.split(".finally(")[1]
    assert "response.json().catch(onNetworkError)" in function_source(js, "readResponse")
