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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from dashboard_js import function_source, normalized, strip_comments

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
    sanitize_active_invocation,
    sanitize_attempt,
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
from software_agent_factory.dashboard.view import project_view, run_detail_view

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


NON_ASCII_TOKENS = ["caf\u00e9", "\u00e9" * 43]


@pytest.mark.parametrize("token", NON_ASCII_TOKENS, ids=["short", "full length"])
def test_a_non_ascii_header_token_is_401_not_an_error(
    running_server: RunningServer, token: str
) -> None:
    headers = {"Host": f"127.0.0.1:{running_server.port}", TOKEN_HEADER: token}

    response = running_server.request("GET", "/api/summary", headers=headers)

    assert response.status == 401


@pytest.mark.parametrize("token", NON_ASCII_TOKENS, ids=["short", "full length"])
def test_a_non_ascii_query_token_is_401_not_an_error(
    running_server: RunningServer, token: str
) -> None:
    headers = {"Host": f"127.0.0.1:{running_server.port}"}

    response = running_server.request("GET", f"/?token={quote(token)}", headers=headers)

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
    assert set(project) <= PROJECT_FIELDS | {"tasks", "models", "totals"}
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


@pytest.mark.parametrize(
    "tokens", [pytest.param(1200, id="reported"), pytest.param(None, id="unknown-stays-null")]
)
def test_summary_passes_the_key_figures_through(tokens: int | None) -> None:
    def provider(*, limit: int, offset: int) -> dict[str, Any]:
        return {
            **fake_snapshot_provider(limit=limit, offset=offset),
            "needs_human_count": 1,
            "failed_last_24h": 2,
            "tokens_last_24h": tokens,
        }

    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=provider,
        run_detail_provider=fake_run_detail_provider,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/summary", headers=running.authed_headers())
        payload = _body_json(response)
    finally:
        _stop(running)

    assert payload["needs_human_count"] == 1
    assert payload["failed_last_24h"] == 2
    assert payload["tokens_last_24h"] == tokens


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
GH_SECRET = "GH_TOKEN=ghp_abcdefgh12345678"


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
        "failure_reason": f"traceback containing {GH_SECRET}",
        "token_usage": {"api_key": SECRET_MARKER},
        "usage": {**detail["usage"], "api_key": SECRET_MARKER},
        "raw_artifact": SECRET_MARKER,
        "attempts": [
            {
                **attempt,
                "reasoning": f"attempt chain of thought: {SECRET_MARKER}",
                "failure_reason": f"attempt traceback: {GH_SECRET}",
                "tool_output": SECRET_MARKER,
                "raw_command_log": SECRET_MARKER,
            }
            for attempt in detail["attempts"]
        ],
        "invocations": [
            {
                **invocation,
                "success": False,
                "failure_reason": GH_SECRET,
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
        assert "ghp_abcdefgh12345678" not in raw_body
        payload = json.loads(raw_body)
        assert set(payload) <= RUN_DETAIL_FIELDS | {
            "active_invocation",
            "attempts",
            "invocations",
            "totals",
            "next_step",
        }
        assert "logs" not in payload
        assert "diff" not in payload
        assert "prompt" not in payload
        assert "tool_output" not in payload
        assert "reasoning" not in payload
        assert payload["failure_reason"] == "traceback containing [REDACTED]"
        assert "token_usage" not in payload
        assert "raw_artifact" not in payload
        for attempt in payload["attempts"]:
            assert set(attempt) <= ATTEMPT_FIELDS
            assert "reasoning" not in attempt
            assert attempt["failure_reason"] == "attempt traceback: [REDACTED]"
            assert "tool_output" not in attempt
            assert "raw_command_log" not in attempt
        for invocation in payload["invocations"]:
            assert set(invocation) <= INVOCATION_FIELDS
            assert invocation["failure_reason"] == "[REDACTED]"
            assert SECRET_MARKER not in json.dumps(invocation)
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


@pytest.mark.parametrize(
    "field", ["status", "target_type", "reason_code", "resume_classification", "last_action"]
)
@pytest.mark.parametrize("value", [["NOTIFIED"], {"a": 1}])
def test_an_unhashable_escalation_value_is_dropped_not_raised(field: str, value: object) -> None:
    payload = sanitize_run_detail(
        {**FIXTURE_DETAILS["run-001"], "escalation": {"status": "NOTIFIED", field: value}}
    )

    assert field not in payload["escalation"]


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
        r"document\.write",
        r"\beval\(",
        r"\bnew Function\(",
    ],
)
def test_app_js_never_uses_dangerous_rendering_apis(forbidden: str) -> None:
    assert re.search(forbidden, strip_comments(dashboard_assets.APP_JS)) is None


def test_app_js_renders_server_text_with_textcontent() -> None:
    js = dashboard_assets.APP_JS
    assert "textContent" in js
    assert "Active call" in js
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


def test_dashboard_unreported_cost_and_tokens_show_not_reported_and_never_swap_units() -> None:
    """No JS runner is available, so pin the exact rendering helper source and
    the row wiring instead of grepping for loose substrings."""
    js = dashboard_assets.APP_JS

    assert function_source(js, "displayUsd") == (
        "function displayUsd(value) { "
        'if (!isFiniteNumber(value)) { return NOT_REPORTED; } return "$" + value.toFixed(6); }'
    )
    assert function_source(js, "displayNumber") == (
        "function displayNumber(value) { "
        "if (!isFiniteNumber(value)) { return NOT_REPORTED; } "
        'return value.toLocaleString("en-US"); }'
    )
    # Every usage table puts the AI usage value, then the premium requests, then
    # the list-price estimate in adjacent cells, matching their header order; the
    # estimate is read from its own field, never from usage_value_usd.
    assert (
        "appendCell(row, displayUsd(usage.usage_value_usd)); "
        "appendCell(row, displayNumber(usage.total_premium_request_cost)); "
        "appendCell(row, displayUsd(usage.list_price_estimate_usd));"
    ) in function_source(js, "modelRow")
    code = normalized(js)
    assert "displayUsd(usage.usage_value_usd)" in code
    assert "displayUsd(usage.list_price_estimate_usd)" in code
    assert "displayListPriceEstimate" not in code


def test_dashboard_run_detail_lists_list_price_estimate_after_the_usage_value_rows() -> None:
    usage_fields = function_source(dashboard_assets.APP_JS, "usageFields")

    value_row = '["AI usage value (USD)", displayUsd(usage.usage_value_usd)],'
    estimate_row = '["List-price estimate", displayUsd(usage.list_price_estimate_usd)]'
    assert value_row in usage_fields
    assert estimate_row in usage_fields
    assert usage_fields.index(value_row) < usage_fields.index(estimate_row)


def test_dashboard_project_usage_table_ends_with_the_list_price_estimate_column() -> None:
    """The project models table lists the AI usage value, the premium requests, then
    the estimate last, in the order its row builder fills the cells."""
    js = dashboard_assets.APP_JS

    model_headers = re.search(r"const MODEL_HEADERS = \[(.*?)\];", js, flags=re.DOTALL)
    assert model_headers is not None
    assert re.findall(r'"([^"]*)"', model_headers.group(1))[-3:] == [
        "AI usage value (USD)",
        "Premium requests",
        "List-price estimate",
    ]


def test_dashboard_totals_show_list_price_estimate_row_as_not_reported_when_missing() -> None:
    """renderTotals surfaces the List-price estimate row through ``displayUsd``,
    which shows "not reported" for a missing value, rather than the generic
    renderer's ``[object Object]`` for a field nested two levels deep
    (``metrics.usage.list_price_estimate_usd``)."""
    js = dashboard_assets.APP_JS

    assert function_source(js, "withListPriceEstimate") == (
        "function withListPriceEstimate(metrics) { return { ...metrics, "
        "list_price_estimate_usd: displayUsd(metrics.usage?.list_price_estimate_usd) }; }"
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


def _stored_run_detail_payload(tmp_path: Path, reason: str) -> dict[str, Any]:
    """Store a run whose run, attempt and call all failed with ``reason`` and read its
    detail over HTTP through the real ``build_run_detail`` provider."""
    from datetime import UTC, datetime, timedelta

    from software_agent_factory.models import (
        AgentRole,
        AttemptRecord,
        FactoryRun,
        InvocationRecord,
        WorkflowState,
    )
    from software_agent_factory.observability import build_run_detail
    from software_agent_factory.store import FileRunStore

    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    done = started + timedelta(minutes=1)
    store = FileRunStore(tmp_path / "data")
    store.save_run(
        FactoryRun(
            id="real-failed-run",
            work_item_id="WI-1",
            state=WorkflowState.FAILED,
            failure_reason=reason,
            attempt_records=[
                AttemptRecord(
                    attempt_number=1,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.6-sol",
                    reasoning="high",
                    started_at=started,
                    completed_at=done,
                    outcome="failed",
                    failure_reason=reason,
                )
            ],
            invocation_records=[
                InvocationRecord(
                    invocation_number=1,
                    role=AgentRole.IMPLEMENTER,
                    model="gpt-5.6-sol",
                    reasoning="high",
                    started_at=started,
                    completed_at=done,
                    success=False,
                    failure_reason=reason,
                )
            ],
        )
    )
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=lambda run_id: build_run_detail(store, run_id),
    )
    running = _start(config)
    try:
        response = running.request(
            "GET", "/api/runs/real-failed-run", headers=running.authed_headers()
        )
        assert response.status == 200
        return _body_json(response)
    finally:
        _stop(running)


def test_stored_run_with_a_secret_in_its_failure_reasons_shows_them_redacted(
    tmp_path: Path,
) -> None:
    payload = _stored_run_detail_payload(tmp_path, f"deploy failed: {GH_SECRET}")

    shown = [payload, payload["attempts"][0], payload["invocations"][0]]
    for level in shown:
        assert level["failure_reason"] == "deploy failed: [REDACTED]"
        assert level["failure_reason_truncated"] is False
    assert "ghp_" not in json.dumps(payload)
    assert "GH_TOKEN" not in json.dumps(payload)
    assert payload["invocations"][0]["reasoning"] == "high"


def test_stored_run_with_a_600_character_failure_reason_shows_it_cut_and_marked(
    tmp_path: Path,
) -> None:
    payload = _stored_run_detail_payload(tmp_path, "a" * 300 + "b" * 300)

    shown = [payload, payload["attempts"][0], payload["invocations"][0]]
    for level in shown:
        assert level["failure_reason_truncated"] is True
        assert len(level["failure_reason"]) <= 500
        assert "factory show real-failed-run" in level["failure_reason"]
        assert level["failure_reason"].startswith("a")
        assert level["failure_reason"].endswith("b")


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
# Resume request reader: queued approvals reach the next step.
# ---------------------------------------------------------------------------

_FINGERPRINT = "f" * 64
_QUEUED_REQUEST = {
    "action": "RISK_APPROVAL",
    "context_fingerprint": _FINGERPRINT,
    "status": "pending",
    "reason": None,
    "created_at": "2026-10-01T09:30:00Z",
}


def _waiting_detail(run_id: str) -> dict[str, Any] | None:
    if run_id != "run-001":
        return None
    return {
        "run_id": "run-001",
        "state": "NEEDS_HUMAN",
        "escalation": {
            "status": "NOTIFIED",
            "reason_code": "RISK_APPROVAL",
            "resume_classification": "RISK_APPROVAL",
            "episode_id": "ep-1",
            "context_fingerprint": _FINGERPRINT,
            "reopen_count": 0,
            "reopen_max": 3,
            "reply_closed_cause": None,
            "dashboard_action_refusal": None,
            "approval_scope": {
                "decision_requested": "Approve it.",
                "authorized_actions": ["Run agents."],
                "unauthorized_actions": ["Change scope."],
                "conditions_in_force": ["Gates stay on."],
            },
        },
    }


def _next_step_over_http(reader: Callable[[str, str], Any] | None) -> dict[str, Any]:
    running = _start(
        DashboardConfig(
            host="127.0.0.1",
            port=0,
            snapshot_provider=fake_snapshot_provider,
            run_detail_provider=_waiting_detail,
            resume_request_reader=reader,
        )
    )
    try:
        response = running.request("GET", "/api/runs/run-001", headers=running.authed_headers())
        assert response.status == 200
        step: dict[str, Any] = _body_json(response)["next_step"]
        return step
    finally:
        _stop(running)


def test_a_queued_request_from_the_reader_makes_the_next_step_pending() -> None:
    asked: list[tuple[str, str]] = []

    # double-waiver: B1 — the injected ResumeRequestReader boundary; the production reader has
    # its own tests against a real store
    def reader(run_id: str, episode_id: str) -> list[dict[str, Any]]:
        asked.append((run_id, episode_id))
        return [_QUEUED_REQUEST]

    step = _next_step_over_http(reader)

    assert step["kind"] == "queued"
    assert step["sentence"].startswith("Approved at 2026-10-01 09:30 UTC, queued")
    assert asked == [("run-001", "ep-1")]


def test_without_a_reader_the_next_step_is_the_normal_panel() -> None:
    assert _next_step_over_http(None)["kind"] == "approve"


def test_a_failing_reader_shows_the_normal_panel_and_logs_the_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # double-waiver: B1 — a reader that fails, as a damaged request file would make it
    def failing_reader(run_id: str, episode_id: str) -> list[dict[str, Any]]:
        raise OSError("disk gone")

    with caplog.at_level(logging.ERROR, logger="software_agent_factory.dashboard"):
        step = _next_step_over_http(failing_reader)

    assert step["kind"] == "approve"
    assert "Resume request reader failed for run run-001" in caplog.text


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
# Run detail call timeline (#80 slice 2, step 2.2)
# --------------------------------------------------------------------------

CALL_FIELD_ORDER = [
    "invocation_number",
    "role",
    "purpose",
    "model",
    "reasoning",
    "context_tier",
    "status",
    "success",
    "attempt_number",
    "started_at",
    "completed_at",
    "duration_ms",
    "usage",
    "total_tokens",
    "failure_reason",
    "failure_reason_truncated",
]
TOKEN_CLASSES = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
COST_UNITS = ("total_premium_request_cost", "usage_value_usd", "list_price_estimate_usd")


def _raw_call(**overrides: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "invocation_number": 1,
        "role": "IMPLEMENTER",
        "purpose": "STANDARD",
        "model": "fake-model",
        "reasoning": "high",
        "context_tier": "default",
        "success": True,
        "attempt_number": 1,
        "started_at": "2024-01-01T00:00:00+00:00",
        "completed_at": "2024-01-01T00:00:02.500000+00:00",
        "failure_reason": None,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    return {**call, **overrides}


def test_sanitized_call_lists_timeline_fields_in_a_fixed_order() -> None:
    call = sanitize_invocation(_raw_call())

    assert list(call) == CALL_FIELD_ORDER
    assert call["purpose"] == "STANDARD"
    assert call["reasoning"] == "high"
    assert call["started_at"] == "2024-01-01T00:00:00+00:00"
    assert call["completed_at"] == "2024-01-01T00:00:02.500000+00:00"
    assert call["duration_ms"] == 2500
    assert call["attempt_number"] == 1
    assert call["status"] == "SUCCESS"
    assert call["failure_reason"] is None
    assert call["failure_reason_truncated"] is False


def test_failed_call_has_failed_status() -> None:
    call = sanitize_invocation(_raw_call(success=False, failure_reason="boom"))

    assert call["status"] == "FAILED"
    assert call["failure_reason"] == "boom"


def test_call_with_unknown_outcome_has_no_status() -> None:
    assert sanitize_invocation(_raw_call(success="yes"))["status"] is None


@pytest.mark.parametrize(
    ("reported", "shown"), [(0, 0), (1200, 1200), (None, None)], ids=["zero", "value", "nothing"]
)
def test_reported_zero_stays_zero_and_unreported_is_null(
    reported: int | None, shown: int | None
) -> None:
    usage = {} if reported is None else {"reasoning_tokens": reported}

    call = sanitize_invocation(_raw_call(usage=usage))

    assert call["usage"]["reasoning_tokens"] == shown
    assert (call["usage"]["reasoning_tokens"] is None) is (shown is None)


def test_every_token_class_and_cost_unit_is_present_and_null_without_usage() -> None:
    call = sanitize_invocation(_raw_call(usage=None))

    assert {key: call["usage"][key] for key in (*TOKEN_CLASSES, *COST_UNITS)} == dict.fromkeys(
        (*TOKEN_CLASSES, *COST_UNITS)
    )


def test_each_call_reports_cost_in_its_own_unit() -> None:
    copilot = sanitize_invocation(
        _raw_call(usage={"total_premium_request_cost": 1.0, "total_nano_aiu": 4_000_000_000})
    )
    pi = sanitize_invocation(_raw_call(usage={"list_price_estimate_usd": 0.02}))

    assert copilot["usage"]["total_premium_request_cost"] == 1.0
    assert copilot["usage"]["usage_value_usd"] == pytest.approx(0.04)
    assert copilot["usage"]["list_price_estimate_usd"] is None
    assert pi["usage"]["list_price_estimate_usd"] == pytest.approx(0.02)
    assert pi["usage"]["total_premium_request_cost"] is None
    assert pi["usage"]["usage_value_usd"] is None


def test_reported_zero_cost_stays_zero() -> None:
    usage = sanitize_invocation(
        _raw_call(
            usage={
                "total_premium_request_cost": 0,
                "total_nano_aiu": 0,
                "list_price_estimate_usd": 0.0,
            }
        )
    )["usage"]

    assert [usage[unit] for unit in COST_UNITS] == [0, 0.0, 0.0]


@pytest.mark.parametrize(
    ("started", "completed", "expected"),
    [
        ("2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", 0),
        ("2024-01-01T00:00:00+00:00", "2024-01-01T00:01:00+00:00", 60_000),
        ("2024-01-01T00:00:00+00:00", None, None),
        (None, "2024-01-01T00:00:00+00:00", None),
        ("not a time", "2024-01-01T00:00:00+00:00", None),
        ("2024-01-01T00:00:05+00:00", "2024-01-01T00:00:00+00:00", None),
        ("2024-01-01T00:00:00", "2024-01-01T00:00:00+00:00", None),
    ],
    ids=["zero", "minute", "no-end", "no-start", "garbage", "negative", "naive"],
)
def test_call_duration_is_derived_from_its_timestamps(
    started: str | None, completed: str | None, expected: int | None
) -> None:
    call = sanitize_invocation(_raw_call(started_at=started, completed_at=completed))

    assert call["duration_ms"] == expected


#: The Copilot usage fixture in tests/test_copilot_runtime.py: 1195 input tokens beside
#: 47104 cache-read tokens, so the cache classes add to input. Its 18 reasoning tokens
#: are already inside the 59 output tokens.
COPILOT_USAGE = {
    "input_tokens": 1195,
    "output_tokens": 59,
    "reasoning_tokens": 18,
    "cache_read_tokens": 47104,
    "cache_write_tokens": 0,
}


def test_call_total_tokens_adds_input_output_and_cache_but_not_reasoning() -> None:
    call = sanitize_invocation(_raw_call(usage=COPILOT_USAGE))

    assert call["total_tokens"] == 1195 + 59 + 47104 + 0


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"input_tokens": 10, "cache_write_tokens": 5}, 15),
        ({"output_tokens": 0}, 0),
        ({"reasoning_tokens": 7}, None),
        ({}, None),
        (None, None),
    ],
    ids=["partial", "reported-zero", "reasoning-only", "empty", "no-usage"],
)
def test_call_total_tokens_is_null_unless_a_counted_class_was_reported(
    usage: dict[str, int] | None, expected: int | None
) -> None:
    assert sanitize_invocation(_raw_call(usage=usage))["total_tokens"] == expected


def test_call_reasoning_level_must_be_a_short_token() -> None:
    chain_of_thought = "first I will " + SECRET_MARKER

    assert sanitize_invocation(_raw_call(reasoning=chain_of_thought))["reasoning"] is None
    assert sanitize_invocation(_raw_call(reasoning=None))["reasoning"] is None


@pytest.mark.parametrize("invalid", [0, -1, True, "1", 1.5], ids=repr)
def test_call_attempt_number_must_be_a_positive_integer(invalid: Any) -> None:
    assert sanitize_invocation(_raw_call(attempt_number=invalid))["attempt_number"] is None


@pytest.mark.parametrize("purpose", ["Summarize the secret plan", "x" * 65, "", 5, None])
def test_call_purpose_must_be_a_short_token_not_free_text(purpose: Any) -> None:
    assert sanitize_invocation(_raw_call(purpose=purpose))["purpose"] is None
    assert sanitize_active_invocation(_raw_active(purpose=purpose))["purpose"] is None


def test_calls_sort_by_number() -> None:
    detail = sanitize_run_detail(
        {
            "run_id": "run-001",
            "invocations": [
                _raw_call(invocation_number=3),
                _raw_call(invocation_number=1),
                {"role": "TRIAGE"},
                _raw_call(invocation_number=2),
            ],
        }
    )

    assert [call["invocation_number"] for call in detail["invocations"]] == [1, 2, 3, None]


def test_call_drops_fields_outside_the_allowlist() -> None:
    call = sanitize_invocation(
        _raw_call(
            prompt=SECRET_MARKER,
            tool_output=SECRET_MARKER,
            raw_command_log=SECRET_MARKER,
            usage={"input_tokens": 1, "api_key": SECRET_MARKER},
        )
    )

    assert list(call) == CALL_FIELD_ORDER
    assert SECRET_MARKER not in json.dumps(call)


def test_call_keeps_sanitized_performance_only_when_provided() -> None:
    assert "performance" not in sanitize_invocation(_raw_call())

    call = sanitize_invocation(_raw_call(performance={"prompt_chars": 5, "logs": SECRET_MARKER}))

    assert call["performance"] == {"prompt_chars": 5}
    assert list(call)[-1] == "performance"


# --------------------------------------------------------------------------
# Redacted, bounded failure reasons for the run, an attempt and a call
# --------------------------------------------------------------------------

REASON_LEVELS = ("run", "attempt", "call")
REASON_SECRETS = {
    "token assignment": (GH_SECRET, ("ghp_abcdefgh12345678",)),
    "bearer header": ("Authorization: Bearer abc.def.gh", ("abc.def.gh",)),
}


def _detail_with_reason(level: str, reason: str) -> dict[str, Any]:
    detail = dict(FIXTURE_DETAILS["run-001"])
    if level == "run":
        detail["failure_reason"] = reason
    elif level == "attempt":
        detail["attempts"] = [{**detail["attempts"][0], "failure_reason": reason}]
    else:
        detail["invocations"] = [
            {**detail["invocations"][0], "success": False, "failure_reason": reason}
        ]
    return detail


def _shown_reason(level: str, payload: dict[str, Any]) -> tuple[str, bool]:
    shown = {"run": payload, "attempt": payload["attempts"][0], "call": payload["invocations"][0]}[
        level
    ]
    return shown["failure_reason"], shown["failure_reason_truncated"]


def _reason_of(level: str, reason: str) -> tuple[str, bool]:
    """The level's reason and cut flag, from the run detail view."""
    return _shown_reason(level, run_detail_view(_detail_with_reason(level, reason)))


def _reason_through_api(level: str, reason: str) -> tuple[str, bool]:
    """Open the run detail over HTTP and return the level's reason and cut flag."""
    detail = _detail_with_reason(level, reason)
    config = DashboardConfig(
        host="127.0.0.1",
        port=0,
        snapshot_provider=fake_snapshot_provider,
        run_detail_provider=lambda run_id: detail if run_id == "run-001" else None,
    )
    running = _start(config)
    try:
        response = running.request("GET", "/api/runs/run-001", headers=running.authed_headers())
        assert response.status == 200
        return _shown_reason(level, _body_json(response))
    finally:
        _stop(running)


@pytest.mark.parametrize("level", REASON_LEVELS)
def test_a_reason_is_redacted_and_cut_before_it_leaves_the_server(level: str) -> None:
    reason, truncated = _reason_through_api(level, f"{GH_SECRET} " + "a" * 600)

    assert truncated is True
    assert len(reason) <= 500
    assert "[REDACTED]" in reason
    assert "ghp_" not in reason
    assert "GH_TOKEN" not in reason
    assert "factory show run-001" in reason


@pytest.mark.parametrize("level", REASON_LEVELS)
@pytest.mark.parametrize("secret", list(REASON_SECRETS), ids=list(REASON_SECRETS))
def test_secret_in_a_failure_reason_is_redacted(level: str, secret: str) -> None:
    text, fragments = REASON_SECRETS[secret]

    reason, _ = _reason_of(level, text)

    assert "[REDACTED]" in reason
    assert not any(fragment in reason for fragment in fragments)


@pytest.mark.parametrize("level", REASON_LEVELS)
def test_reason_of_500_characters_is_shown_in_full(level: str) -> None:
    reason, truncated = _reason_of(level, "a" * 500)

    assert reason == "a" * 500
    assert truncated is False


@pytest.mark.parametrize("level", REASON_LEVELS)
def test_reason_of_501_characters_is_cut_and_names_factory_show(level: str) -> None:
    reason, truncated = _reason_of(level, "a" * 250 + "b" + "c" * 250)

    assert truncated is True
    assert len(reason) <= 500
    assert "factory show run-001" in reason
    assert reason.startswith("a")
    assert reason.endswith("c")


def _cut_edges() -> tuple[int, int]:
    """How many raw characters a cut keeps at the head and at the tail."""
    reason, _ = _reason_of("run", "H" * 2000 + "T" * 2000)
    head = len(reason) - len(reason.lstrip("H"))
    tail = len(reason) - len(reason.rstrip("T"))
    return head, tail


#: Parts of ``GH_SECRET`` a half-cut copy of it still shows.
_SECRET_FRAGMENTS = ("GH_TOKEN", "ghp_", "bcdefgh", "12345678")


def _secret_across_the_head_edge() -> str:
    head, _ = _cut_edges()
    inside = len(GH_SECRET) // 2
    # The space keeps the filler out of the secret: its name may start with letters.
    return "x" * (head - inside - 1) + " " + GH_SECRET + " " + "y" * 1000


def _secret_across_the_tail_edge() -> str:
    _, tail = _cut_edges()
    inside = len(GH_SECRET) - len(GH_SECRET) // 2
    return "x" * 1000 + " " + GH_SECRET + " " + "y" * (tail - inside - 1)


@pytest.mark.parametrize("level", REASON_LEVELS)
@pytest.mark.parametrize(
    "build_text",
    [_secret_across_the_head_edge, _secret_across_the_tail_edge],
    ids=["head-edge", "tail-edge"],
)
def test_reason_is_cut_after_redaction_so_a_secret_is_never_split(
    level: str, build_text: Callable[[], str]
) -> None:
    text = build_text()
    head, tail = _cut_edges()
    # Cutting the raw text first would leave half of the secret on show.
    cut_first = text[:head] + text[len(text) - tail :]
    assert any(fragment in cut_first for fragment in _SECRET_FRAGMENTS)

    reason, truncated = _reason_of(level, text)

    assert truncated is True
    assert "[REDACTED]" in reason
    assert not any(fragment in reason for fragment in _SECRET_FRAGMENTS)


@pytest.mark.parametrize("raw", [None, "", 42, ["a"]], ids=repr)
def test_missing_or_empty_reason_is_null_and_not_cut(raw: Any) -> None:
    call = sanitize_invocation(_raw_call(failure_reason=raw))
    attempt = sanitize_attempt({"attempt_number": 1, "failure_reason": raw})
    detail = sanitize_run_detail({"run_id": "run-001", "failure_reason": raw})

    for shown in (call, attempt, detail):
        assert shown["failure_reason"] is None
        assert shown["failure_reason_truncated"] is False


def test_cut_marker_uses_a_placeholder_when_the_run_id_is_unknown_or_invalid() -> None:
    long_reason = "a" * 600

    assert (
        "factory show <run>" in sanitize_attempt({"failure_reason": long_reason})["failure_reason"]
    )


@pytest.mark.parametrize("run_id", ["bad id\n", "run.001"])
def test_cut_marker_ignores_a_run_id_the_dashboard_route_would_reject(run_id: str) -> None:
    detail = sanitize_run_detail({"run_id": run_id, "failure_reason": "a" * 600})

    assert "factory show <run>" in detail["failure_reason"]


def test_cut_marker_names_the_run_by_its_id_when_run_id_is_missing() -> None:
    detail = sanitize_run_detail({"id": "run-009", "failure_reason": "a" * 600})

    assert "factory show run-009" in detail["failure_reason"]


def test_project_model_status_is_derived_by_the_server() -> None:
    project = sanitize_project(
        {
            "project_id": "project-001",
            "models": [
                {"model": "m", "success": True},
                {"model": "m", "success": False},
                {"model": "m"},
                {"model": "m", "success": True, "status": "running"},
            ],
        }
    )

    assert [model["status"] for model in project["models"]] == [
        "SUCCESS",
        "FAILED",
        None,
        "running",
    ]


def _raw_active(**overrides: Any) -> dict[str, Any]:
    active: dict[str, Any] = {
        "invocation_number": 2,
        "role": "REVIEWER",
        "purpose": "STANDARD",
        "model": "fake-model",
        "reasoning": "medium",
        "context_tier": "default",
        "status": "running",
        "started_at": "2024-01-01T00:06:00+00:00",
        "attempt_number": 2,
    }
    return {**active, **overrides}


def test_running_call_has_the_shape_of_a_finished_call_with_nothing_reported() -> None:
    call = sanitize_active_invocation(_raw_active())

    assert list(call) == CALL_FIELD_ORDER
    assert call["status"] == "running"
    assert call["started_at"] == "2024-01-01T00:06:00+00:00"
    assert call["success"] is None
    assert call["total_tokens"] is None
    assert call["completed_at"] is None
    assert call["duration_ms"] is None
    assert call["failure_reason"] is None
    assert call["failure_reason_truncated"] is False
    assert {key: call["usage"][key] for key in (*TOKEN_CLASSES, *COST_UNITS)} == dict.fromkeys(
        (*TOKEN_CLASSES, *COST_UNITS)
    )


@pytest.mark.parametrize("status", ["stale", "crashed", "abandoned"])
def test_running_call_keeps_a_liveness_status_the_provider_reports(status: str) -> None:
    assert sanitize_active_invocation(_raw_active(status=status))["status"] == status


@pytest.mark.parametrize("status", [None, "", "weird", "FAILED", ["running"], 5])
def test_running_call_shows_running_for_a_missing_or_unknown_status(status: Any) -> None:
    assert sanitize_active_invocation(_raw_active(status=status))["status"] == "running"


def test_running_call_ignores_fields_that_only_a_finished_call_has() -> None:
    call = sanitize_active_invocation(
        _raw_active(
            success=False,
            completed_at="2024-01-01T00:07:00+00:00",
            usage={"input_tokens": 99},
            failure_reason=SECRET_MARKER,
            prompt=SECRET_MARKER,
        )
    )

    assert call["success"] is None
    assert call["completed_at"] is None
    assert call["usage"]["input_tokens"] is None
    assert call["failure_reason"] is None
    assert SECRET_MARKER not in json.dumps(call)


def test_running_call_is_dropped_when_it_is_not_an_object() -> None:
    assert sanitize_active_invocation("running") == {}
    assert "active_invocation" not in sanitize_run_detail(
        {"run_id": "run-001", "active_invocation": "running"}
    )


def test_run_detail_api_returns_calls_in_number_order_with_the_timeline_fields(
    running_server: RunningServer,
) -> None:
    response = running_server.request(
        "GET", "/api/runs/run-001", headers=running_server.authed_headers()
    )

    payload = _body_json(response)
    assert [list(call) for call in payload["invocations"]] == [CALL_FIELD_ORDER]
    assert list(payload["active_invocation"]) == CALL_FIELD_ORDER
    assert payload["invocations"][0]["duration_ms"] == 300_000
    assert payload["failure_reason"] is None
    assert payload["failure_reason_truncated"] is False


def test_run_detail_api_carries_totals_by_unit(running_server: RunningServer) -> None:
    response = running_server.request(
        "GET", "/api/runs/run-001", headers=running_server.authed_headers()
    )

    totals = _body_json(response)["totals"]
    assert totals["calls"] == 1
    assert totals["duration_ms"] == {"total": 300_000, "reported_count": 1}
    assert totals["costs"]["total_premium_request_cost"] == {"total": 1.0, "reported_count": 1}
    assert totals["costs"]["usage_value_usd"] == {"total": None, "reported_count": 0}


def test_sanitizing_a_run_detail_composes_no_view_model() -> None:
    sanitized = sanitize_run_detail(FIXTURE_DETAILS["run-001"])

    assert "totals" not in sanitized
    assert "next_step" not in sanitized
    assert sanitized["invocations"]


def test_run_detail_without_a_calls_list_has_no_totals() -> None:
    assert "totals" not in run_detail_view({"run_id": "run-001"})


def test_run_detail_with_no_calls_yet_has_every_total_unreported() -> None:
    totals = run_detail_view({"run_id": "run-001", "invocations": []})["totals"]

    assert totals["calls"] == 0
    assert totals["duration_ms"] == {"total": None, "reported_count": 0}


def test_sanitizing_a_project_composes_no_totals() -> None:
    assert "totals" not in sanitize_project({"project_id": "project-001", "models": []})


def test_project_carries_totals_over_its_models() -> None:
    project = project_view(
        {
            "project_id": "project-001",
            "models": [
                {"usage": {"total_nano_aiu": 100_000_000_000}, "success": True},
                {"usage": {}, "success": False},
            ],
        }
    )

    assert project["totals"]["calls"] == 2
    assert project["totals"]["costs"]["usage_value_usd"] == {"total": 1.0, "reported_count": 1}


# --------------------------------------------------------------------------
# Provider -> sanitizer: one usage definition and the running call's reasoning
# --------------------------------------------------------------------------


def _stored_detail(tmp_path: Path, **run_fields: Any) -> Any:
    from datetime import UTC, datetime

    from software_agent_factory.models import FactoryRun, WorkflowState
    from software_agent_factory.observability import build_run_detail
    from software_agent_factory.store import FileRunStore

    store = FileRunStore(tmp_path / "data")
    state = run_fields.pop("state", WorkflowState.DONE)
    store.save_run(
        FactoryRun(id="stored-run", work_item_id="WI-1", state=state, **run_fields),
    )
    return build_run_detail(store, "stored-run", now=datetime(2026, 9, 1, 12, 5, tzinfo=UTC))


def _usage_call(number: int, usage: Any) -> Any:
    from datetime import UTC, datetime, timedelta

    from software_agent_factory.models import AgentRole, InvocationRecord

    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return InvocationRecord(
        invocation_number=number,
        role=AgentRole.IMPLEMENTER,
        model="gpt-5.6-sol",
        reasoning="high",
        started_at=started,
        completed_at=started + timedelta(minutes=1),
        success=True,
        usage=usage,
    )


def test_a_call_that_reports_usage_only_per_model_still_counts_in_the_totals(
    tmp_path: Path,
) -> None:
    from software_agent_factory.models import ModelUsage, UsageMetrics

    per_model_only = UsageMetrics(
        model_usage=(
            ModelUsage(
                model="claude-sonnet-5",
                premium_request_cost=1.0,
                input_tokens=100,
                output_tokens=20,
                reasoning_tokens=5,
                total_nano_aiu=100_000_000_000,
            ),
            ModelUsage(model="gpt-5.6-sol", premium_request_cost=0.5, input_tokens=10),
        )
    )
    aggregate = UsageMetrics(input_tokens=1000, total_premium_request_cost=2.0)
    detail = _stored_detail(
        tmp_path,
        invocation_records=[_usage_call(1, per_model_only), _usage_call(2, aggregate)],
    )

    shown = run_detail_view(detail)

    first, second = shown["invocations"]
    assert first["usage"]["input_tokens"] == 110
    assert first["usage"]["output_tokens"] == 20
    assert first["usage"]["reasoning_tokens"] == 5
    assert first["usage"]["total_premium_request_cost"] == 1.5
    assert first["usage"]["usage_value_usd"] == 1.0
    assert first["total_tokens"] == 130
    assert second["usage"]["input_tokens"] == 1000
    assert second["usage"]["output_tokens"] is None
    assert second["usage"]["total_premium_request_cost"] == 2.0
    totals = shown["totals"]
    assert totals["tokens"]["input_tokens"] == {"total": 1110, "reported_count": 2}
    assert totals["costs"]["total_premium_request_cost"] == {"total": 3.5, "reported_count": 2}
    assert totals["costs"]["usage_value_usd"] == {"total": 1.0, "reported_count": 1}


def test_run_usage_and_run_totals_report_the_same_figures(tmp_path: Path) -> None:
    from software_agent_factory.models import ModelUsage, UsageMetrics

    per_model_only = UsageMetrics(
        model_usage=(ModelUsage(model="m", premium_request_cost=1.0, input_tokens=100),)
    )
    detail = _stored_detail(
        tmp_path,
        invocation_records=[_usage_call(1, per_model_only), _usage_call(2, per_model_only)],
    )

    shown = run_detail_view(detail)

    assert shown["usage"]["input_tokens"] == shown["totals"]["tokens"]["input_tokens"]["total"]
    assert (
        shown["usage"]["premium_request_cost"]
        == shown["totals"]["costs"]["total_premium_request_cost"]["total"]
    )


def test_the_running_calls_reasoning_level_goes_from_the_provider_to_the_page(
    tmp_path: Path,
) -> None:
    import os
    import socket
    from datetime import UTC, datetime

    from software_agent_factory.models import (
        ActiveInvocation,
        AgentRole,
        RunLease,
        WorkflowState,
    )

    started = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    detail = _stored_detail(
        tmp_path,
        state=WorkflowState.IMPLEMENTING,
        lease=RunLease(host=socket.gethostname(), pid=os.getpid(), heartbeat_at=started),
        active_invocation=ActiveInvocation(
            invocation_number=2,
            role=AgentRole.REVIEWER,
            model="gpt-5.6-sol",
            reasoning="xhigh",
            started_at=started,
        ),
    )

    shown = sanitize_run_detail(detail)

    assert shown["active_invocation"]["reasoning"] == "xhigh"
    assert shown["active_invocation"]["status"] == "running"


# --------------------------------------------------------------------------
# Two-run comparison (#80 slice 5)
# --------------------------------------------------------------------------

_RUN_A = "run-A"
_RUN_B = "run-B"
_MISSING_RUN = "missing-run"
_TRAVERSAL_ID = "../etc"
_SECRET_TEXT = "must-not-be-exposed"


def _compare_call(role: str, model: str, *, success: bool = True, **usage: float) -> dict[str, Any]:
    return {
        "role": role,
        "model": model,
        "success": success,
        "started_at": "2026-10-01T10:00:00Z",
        "completed_at": "2026-10-01T10:00:05Z",
        "usage": usage,
        "prompt": _SECRET_TEXT,
    }


def _compare_detail(run_id: str, calls: list[dict[str, Any]] | None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "run_id": run_id,
        "title": f"Task of {run_id}",
        "state": "DONE",
        "created_at": "2026-10-01T09:00:00Z",
        "logs": _SECRET_TEXT,
    }
    if calls is not None:
        detail["invocations"] = calls
    return detail


_COMPARE_DETAILS: dict[str, dict[str, Any]] = {
    _RUN_A: _compare_detail(
        _RUN_A,
        [
            _compare_call("TRIAGE", "model-t", input_tokens=10, total_premium_request_cost=1.0),
            _compare_call("TRIAGE", "model-t", input_tokens=20, total_premium_request_cost=1.0),
            _compare_call("IMPLEMENTER", "model-i", input_tokens=100),
            _compare_call("REVIEWER", "model-r", input_tokens=7),
        ],
    ),
    _RUN_B: _compare_detail(
        _RUN_B,
        [
            _compare_call("TRIAGE", "model-t", input_tokens=5, total_nano_aiu=200_000_000_000),
            _compare_call("IMPLEMENTER", "model-i", success=False, input_tokens=60),
            _compare_call("IMPLEMENTER", "model-j", input_tokens=40),
        ],
    ),
    "run-empty": _compare_detail("run-empty", []),
    "run-no-calls-key": _compare_detail("run-no-calls-key", None),
}


@pytest.fixture
def compare_server() -> Iterator[RunningServer]:
    running = _start(
        DashboardConfig(
            host="127.0.0.1",
            port=0,
            snapshot_provider=fake_snapshot_provider,
            run_detail_provider=_COMPARE_DETAILS.get,
        )
    )
    try:
        yield running
    finally:
        _stop(running)


def _compare(running: RunningServer, a: str, b: str) -> http.client.HTTPResponse:
    path = f"/api/compare?a={quote(a, safe='')}&b={quote(b, safe='')}"
    return running.request("GET", path, headers=running.authed_headers())


def _role_row(payload: dict[str, Any], role: str) -> dict[str, Any]:
    (row,) = [row for row in payload["roles"] if row["role"] == role]
    return row


def test_compare_gives_each_role_for_both_runs(compare_server: RunningServer) -> None:
    response = _compare(compare_server, _RUN_A, _RUN_B)

    assert response.status == 200
    payload = _body_json(response)
    assert [row["role"] for row in payload["roles"]] == ["TRIAGE", "IMPLEMENTER", "REVIEWER"]
    triage = _role_row(payload, "TRIAGE")
    assert [triage["a"]["calls"], triage["b"]["calls"]] == [2, 1]
    assert triage["a"]["tokens"]["input_tokens"] == {"total": 30, "reported_count": 2}
    assert triage["a"]["duration_ms"] == {"total": 10000, "reported_count": 2}
    implementer = _role_row(payload, "IMPLEMENTER")
    assert [implementer["a"]["calls"], implementer["b"]["calls"]] == [1, 2]
    assert implementer["a"]["failed_calls"] == {"total": 0, "reported_count": 1}
    assert implementer["b"]["failed_calls"] == {"total": 1, "reported_count": 2}
    assert implementer["a"]["models"] == ["model-i"]
    assert implementer["b"]["models"] == ["model-i", "model-j"]


def test_compare_keeps_each_runs_cost_in_its_own_units(compare_server: RunningServer) -> None:
    payload = _body_json(_compare(compare_server, _RUN_A, _RUN_B))

    triage = _role_row(payload, "TRIAGE")
    assert triage["a"]["costs"]["total_premium_request_cost"] == {
        "total": 2.0,
        "reported_count": 2,
    }
    assert triage["a"]["costs"]["usage_value_usd"] == {"total": None, "reported_count": 0}
    assert triage["b"]["costs"]["total_premium_request_cost"] == {
        "total": None,
        "reported_count": 0,
    }
    assert triage["b"]["costs"]["usage_value_usd"] == {"total": 2.0, "reported_count": 1}


def test_compare_labels_each_run_for_the_picker(compare_server: RunningServer) -> None:
    payload = _body_json(_compare(compare_server, _RUN_A, _RUN_B))

    assert payload["a"] == {
        "run_id": _RUN_A,
        "created_at": "2026-10-01T09:00:00Z",
        "state": "DONE",
        "title": "Task of run-A",
        "models": ["model-i", "model-r", "model-t"],
    }
    assert payload["b"]["run_id"] == _RUN_B
    assert payload["b"]["models"] == ["model-i", "model-j", "model-t"]


def test_compare_shows_a_role_only_one_run_used_as_null_for_the_other(
    compare_server: RunningServer,
) -> None:
    payload = _body_json(_compare(compare_server, _RUN_A, _RUN_B))

    reviewer = _role_row(payload, "REVIEWER")
    assert reviewer["a"]["calls"] == 1
    assert reviewer["b"] is None


@pytest.mark.parametrize("empty_run", ["run-empty", "run-no-calls-key"])
def test_compare_with_a_run_that_has_no_calls_lists_the_other_runs_roles(
    compare_server: RunningServer, empty_run: str
) -> None:
    payload = _body_json(_compare(compare_server, empty_run, _RUN_B))

    assert payload["a"]["models"] == []
    assert [row["a"] for row in payload["roles"]] == [None, None]
    assert [row["role"] for row in payload["roles"]] == ["TRIAGE", "IMPLEMENTER"]


def test_compare_of_two_runs_without_calls_has_no_roles(compare_server: RunningServer) -> None:
    payload = _body_json(_compare(compare_server, "run-empty", "run-no-calls-key"))

    assert payload["roles"] == []


def test_compare_drops_fields_the_sanitizer_does_not_allow(compare_server: RunningServer) -> None:
    response = _compare(compare_server, _RUN_A, _RUN_B)

    body = response.read_body.decode()  # type: ignore[attr-defined]
    assert response.status == 200
    # A positive field first, so an empty answer cannot pass for "nothing leaked".
    assert json.loads(body)["a"]["title"] == "Task of run-A"
    assert _SECRET_TEXT not in body


_BAD_REQUESTS = [
    (_RUN_A, _TRAVERSAL_ID),
    (_TRAVERSAL_ID, _RUN_A),
    (_RUN_A, ""),
    ("", _RUN_A),
    (_RUN_A, _RUN_A),
    (_TRAVERSAL_ID, _MISSING_RUN),
    (_MISSING_RUN, _MISSING_RUN),
    ("", ""),
]


@pytest.mark.parametrize(("a", "b"), _BAD_REQUESTS)
def test_compare_rejects_an_invalid_empty_or_identical_id_with_400(a: str, b: str) -> None:
    asked: list[str] = []

    def recording_provider(run_id: str) -> dict[str, Any] | None:
        asked.append(run_id)
        return _COMPARE_DETAILS.get(run_id)

    running = _start(
        DashboardConfig(
            host="127.0.0.1",
            port=0,
            snapshot_provider=fake_snapshot_provider,
            run_detail_provider=recording_provider,
        )
    )
    try:
        response = _compare(running, a, b)
        payload = _body_json(response)
    finally:
        _stop(running)

    assert response.status == 400
    assert payload == {"error": "invalid run ids"}
    assert asked == []


@pytest.mark.parametrize(
    "path", ["/api/compare", f"/api/compare?a={_RUN_A}", f"/api/compare?b={_RUN_A}"]
)
def test_compare_with_a_missing_id_parameter_is_400(
    compare_server: RunningServer, path: str
) -> None:
    response = compare_server.request("GET", path, headers=compare_server.authed_headers())

    assert response.status == 400


@pytest.mark.parametrize(
    ("a", "b", "missing"),
    [
        pytest.param(_RUN_A, _MISSING_RUN, ["b"], id="b-missing"),
        pytest.param(_MISSING_RUN, _RUN_A, ["a"], id="a-missing"),
        pytest.param(_MISSING_RUN, "other-missing-run", ["a", "b"], id="both-missing"),
    ],
)
def test_compare_with_an_unknown_run_is_404_naming_the_missing_side(
    compare_server: RunningServer, a: str, b: str, missing: list[str]
) -> None:
    response = _compare(compare_server, a, b)

    assert response.status == 404
    assert _body_json(response) == {"error": "not found", "missing": missing}


def test_compare_without_a_token_is_401(compare_server: RunningServer) -> None:
    path = f"/api/compare?a={_RUN_A}&b={_RUN_B}"

    response = compare_server.request(
        "GET", path, headers={"Host": f"127.0.0.1:{compare_server.port}"}
    )

    assert response.status == 401
    assert _body_json(response) == {"error": "unauthorized"}


def test_compare_with_a_wrong_host_is_rejected(compare_server: RunningServer) -> None:
    path = f"/api/compare?a={_RUN_A}&b={_RUN_B}"
    headers = {"Host": "evil.com", TOKEN_HEADER: compare_server.token}

    response = compare_server.request("GET", path, headers=headers)

    assert response.status == 400
    assert _body_json(response) == {"error": "invalid host"}


def test_compare_provider_failure_is_503_without_run_data() -> None:
    running = _start(
        DashboardConfig(
            host="127.0.0.1",
            port=0,
            snapshot_provider=fake_snapshot_provider,
            run_detail_provider=failing_detail_provider,
        )
    )
    try:
        response = _compare(running, _RUN_A, _RUN_B)
        payload = _body_json(response)
    finally:
        _stop(running)

    assert response.status == 503
    assert payload == {"error": "run detail unavailable"}


def test_compare_with_unsanitizable_provider_data_is_503_without_run_data() -> None:
    running = _start(
        DashboardConfig(
            host="127.0.0.1",
            port=0,
            snapshot_provider=fake_snapshot_provider,
            run_detail_provider=lambda run_id: _SECRET_TEXT,  # type: ignore[arg-type,return-value]
        )
    )
    try:
        response = _compare(running, _RUN_A, _RUN_B)
        payload = _body_json(response)
    finally:
        _stop(running)

    assert response.status == 503
    assert payload == {"error": "run detail unavailable"}
