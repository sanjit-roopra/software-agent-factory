"""Check output and review text routes of the dashboard (ADR-047)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dashboard_js import JsCall, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.evidence import (
    MAX_EVIDENCE_ITEMS,
    OUTPUT_CHAR_LIMIT,
    OUTPUT_CUT_MARKER,
    OUTPUT_LINE_LIMIT,
    REVIEW_TEXT_LIMIT,
    output_tail,
    sanitize_run_checks,
    sanitize_run_review,
)
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, DashboardServer, create_server
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    CICheckEvidence,
    CIReport,
    CommandResult,
    FactoryRun,
    ReviewFinding,
    ReviewFindingDraft,
    ReviewLedger,
    ReviewReport,
    ReviewSourceLocation,
    VerificationReport,
    WorkflowState,
)
from software_agent_factory.observability import build_run_evidence
from software_agent_factory.store import FileRunStore

GH_SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

EVIDENCE: dict[str, Any] = {
    "run_id": "run-001",
    "checks": [
        {
            "command": "pytest -q",
            "exit_code": 1,
            "stdout": "collected 3 items\nFAILED test_a.py::test_x",
            "stderr": f"token {GH_SECRET}",
            "duration_seconds": 2.5,
            "timed_out": False,
            "env": "must-not-be-exposed",
        },
        {"command": "ruff check .", "exit_code": 0, "stdout": "ok", "duration_seconds": 0.1},
    ],
    "ci_checks": [
        {
            "name": "build",
            "status": "FAIL",
            "description": "Build failed",
            "details_url": "https://github.com/o/r/actions/runs/1",
            "failure_category": "TEST",
            "log_excerpt": "error: boom",
        },
        {"name": "lint", "status": "PASS", "details_url": "javascript:alert(1)"},
    ],
    "review_rounds": [
        {
            "attempt_number": 1,
            "approved": False,
            "findings": ["A finding", 7],
            "suggested_changes": [f"Remove {GH_SECRET}"],
            "blocking_findings": [
                {
                    "category": "CORRECTNESS",
                    "message": "Off by one",
                    "locations": [
                        {"path": "src/a.py", "start_line": 3, "end_line": 5},
                        {"path": "src/b.py", "start_line": 0, "end_line": 1},
                    ],
                }
            ],
            "prior_finding_dispositions": [
                {"finding_id": "review-1", "status": "FIXED", "rationale": "Done"}
            ],
            "reasoning": "must-not-be-exposed",
        }
    ],
    "open_findings": [
        {
            "id": "review-2",
            "category": "not-a-category",
            "message": "Still open",
            "locations": [{"path": "src/c.py", "start_line": 1, "end_line": 1}],
            "origin": "must-not-be-exposed",
        }
    ],
    "accepted_findings": [],
}


def test_checks_keep_allowlisted_fields_and_redact_output() -> None:
    payload = sanitize_run_checks(EVIDENCE)

    failed, passed = payload["checks"]
    assert failed == {
        "command": "pytest -q",
        "exit_code": 1,
        "duration_seconds": 2.5,
        "timed_out": False,
        "passed": False,
        "output": "collected 3 items\nFAILED test_a.py::test_x\ntoken [REDACTED]",
        "output_truncated": False,
    }
    assert passed["passed"] is True
    build, lint = payload["ci_checks"]
    assert build["details_url"] == "https://github.com/o/r/actions/runs/1"
    assert build["output"] == "error: boom"
    assert build["failure_category"] == "TEST"
    assert lint["details_url"] is None
    assert "must-not-be-exposed" not in json.dumps(payload)


def test_a_timed_out_or_malformed_check_never_shows_as_passed() -> None:
    checks = [
        {"command": "x", "exit_code": 0, "timed_out": True},
        {"command": "y", "exit_code": True, "duration_seconds": -1},
        "not a check",
    ]
    payload = sanitize_run_checks({"checks": checks, "ci_checks": "nope"})

    timed_out, malformed = payload["checks"]
    assert timed_out["passed"] is False
    assert malformed["exit_code"] is None
    assert malformed["duration_seconds"] is None
    assert malformed["passed"] is False
    assert payload["ci_checks"] == []


def test_output_keeps_the_last_lines_with_a_cut_marker() -> None:
    text = "\n".join(f"line {number}" for number in range(OUTPUT_LINE_LIMIT + 20))

    tail, cut = output_tail(text)

    assert cut is True
    assert tail.startswith(OUTPUT_CUT_MARKER)
    assert tail.endswith(f"line {OUTPUT_LINE_LIMIT + 19}")
    assert "line 19\n" not in tail
    assert len(tail.splitlines()) == OUTPUT_LINE_LIMIT + 1


def test_output_is_cut_to_its_character_limit_after_redaction() -> None:
    tail, cut = output_tail(GH_SECRET + " " + "x" * (OUTPUT_CHAR_LIMIT * 2))

    assert cut is True
    assert len(tail) == OUTPUT_CHAR_LIMIT
    assert output_tail("short") == ("short", False)


def test_review_keeps_allowlisted_fields_and_redacts_text() -> None:
    payload = sanitize_run_review(EVIDENCE)

    [review] = payload["rounds"]
    assert review["attempt_number"] == 1
    assert review["approved"] is False
    assert review["findings"] == ["A finding"]
    assert review["suggested_changes"] == ["Remove [REDACTED]"]
    assert review["blocking_findings"] == [
        {
            "category": "CORRECTNESS",
            "message": "Off by one",
            "locations": [{"path": "src/a.py", "start_line": 3, "end_line": 5}],
        }
    ]
    assert review["prior_finding_dispositions"] == [
        {"finding_id": "review-1", "status": "FIXED", "rationale": "Done"}
    ]
    assert payload["open_findings"][0]["id"] == "review-2"
    assert payload["open_findings"][0]["category"] is None
    assert "must-not-be-exposed" not in json.dumps(payload)
    assert GH_SECRET not in json.dumps(payload)


def test_review_text_and_lists_are_bounded() -> None:
    rounds = [{"findings": ["f" * (REVIEW_TEXT_LIMIT * 2)] * (MAX_EVIDENCE_ITEMS + 5)}]

    [review] = sanitize_run_review({"review_rounds": rounds})["rounds"]

    assert len(review["findings"]) == MAX_EVIDENCE_ITEMS
    assert all(len(text) <= REVIEW_TEXT_LIMIT for text in review["findings"])
    assert review["attempt_number"] is None


@pytest.mark.parametrize("sanitize", [sanitize_run_checks, sanitize_run_review])
def test_evidence_that_is_not_an_object_is_refused(sanitize: Any) -> None:
    with pytest.raises(TypeError):
        sanitize(["not", "an", "object"])


# -- build_run_evidence ------------------------------------------------------


def _attempt(number: int) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=number,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="medium",
        started_at=T0,
        completed_at=T0 + timedelta(minutes=1),
        outcome="ok",
        budget=AttemptBudget.IMPLEMENTATION,
        triggered_by=AttemptTrigger.INITIAL,
    )


def _finding() -> ReviewFinding:
    return ReviewFinding(
        id="review-1",
        category="CORRECTNESS",
        message="Open",
        locations=[ReviewSourceLocation(path="a.py", start_line=1, end_line=1)],
        origin="INITIAL",
        first_seen_snapshot=1,
    )


def test_build_run_evidence_reads_checks_ci_rounds_and_ledger(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-e",
        work_item_id="WI-1",
        state=WorkflowState.NEEDS_HUMAN,
        created_at=T0,
        updated_at=T0,
        attempt_records=[_attempt(1), _attempt(2), _attempt(3)],
        review_ledger=ReviewLedger(open_findings=[_finding()]),
    )
    store.save_run(run)
    check = CommandResult(command="pytest", exit_code=1, stdout="boom", duration_seconds=1.0)
    store.save_artifact(
        run.id, VerificationReport(passed=False, deterministic_checks=[check], confidence=1.0)
    )
    store.save_artifact(
        run.id, CIReport(overall="FAIL", checks=[CICheckEvidence(name="ci", status="FAIL")])
    )
    blocking = ReviewFindingDraft(
        category="CORRECTNESS",
        message="Bad",
        locations=[ReviewSourceLocation(path="a.py", start_line=1, end_line=2)],
    )
    store.save_artifact(
        run.id, ReviewReport(approved=False, blocking_findings=[blocking]), attempt=1
    )
    store.save_artifact(run.id, ReviewReport(approved=True), attempt=3)

    evidence = build_run_evidence(store, run.id)

    assert evidence is not None
    assert [item["command"] for item in evidence["checks"]] == ["pytest"]
    assert evidence["checks"][0]["stdout"] == "boom"
    assert [item["name"] for item in evidence["ci_checks"]] == ["ci"]
    assert [item["attempt_number"] for item in evidence["review_rounds"]] == [1, 3]
    assert evidence["review_rounds"][0]["blocking_findings"][0]["message"] == "Bad"
    assert [item["id"] for item in evidence["open_findings"]] == ["review-1"]
    assert evidence["accepted_findings"] == []


def test_build_run_evidence_is_empty_without_artifacts_and_none_without_a_run(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(
        FactoryRun(
            id="run-bare",
            work_item_id="WI",
            state=WorkflowState.CREATED,
            created_at=T0,
            updated_at=T0,
        )
    )

    assert build_run_evidence(store, "run-bare") == {
        "run_id": "run-bare",
        "checks": [],
        "ci_checks": [],
        "review_rounds": [],
        "open_findings": [],
        "accepted_findings": [],
    }
    assert build_run_evidence(store, "run-missing") is None


# -- routes ------------------------------------------------------------------


def _snapshot(*, limit: int, offset: int) -> dict[str, Any]:
    return {"runs": [], "page": {"limit": limit, "offset": offset}}


def _failing(run_id: str) -> Any:
    raise RuntimeError(f"cannot read {run_id}")


@pytest.fixture
def start() -> Iterator[Any]:
    servers: list[DashboardServer] = []

    def _start(evidence_provider: Any) -> DashboardServer:
        server = create_server(
            DashboardConfig(
                snapshot_provider=_snapshot,
                run_detail_provider=lambda run_id: None,
                run_evidence_provider=evidence_provider,
            )
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(server: DashboardServer, path: str) -> tuple[int, Any]:
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(
            "GET", path, headers={TOKEN_HEADER: server.token, "Host": f"{host}:{port}"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()


def test_the_checks_and_review_routes_answer_sanitized_evidence(start: Any) -> None:
    server = start(lambda run_id: EVIDENCE if run_id == "run-001" else None)

    checks_status, checks = _get(server, "/api/runs/run-001/checks")
    review_status, review = _get(server, "/api/runs/run-001/review")

    assert (checks_status, review_status) == (200, 200)
    assert checks == sanitize_run_checks(EVIDENCE)
    assert review == sanitize_run_review(EVIDENCE)


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/runs/run-404/checks", 404),
        ("/api/runs/..%2Fetc/review", 404),
        ("/api/runs/run-001/other", 404),
    ],
)
def test_the_evidence_routes_refuse_an_unknown_or_bad_run(
    start: Any, path: str, status: int
) -> None:
    server = start(lambda run_id: EVIDENCE if run_id == "run-001" else None)
    assert _get(server, path)[0] == status


def test_the_evidence_routes_are_404_without_a_provider(start: Any) -> None:
    assert _get(start(None), "/api/runs/run-001/checks")[0] == 404


def test_the_evidence_routes_answer_503_when_the_provider_fails(start: Any) -> None:
    status, body = _get(start(_failing), "/api/runs/run-001/review")
    assert status == 503
    assert "run-001" not in json.dumps(body)


# -- page --------------------------------------------------------------------


def test_the_run_page_has_checks_and_review_cards_and_loads_them() -> None:
    html = dashboard_assets.render_index_html(token="t")
    for card in ("checks", "review"):
        assert f'<section id="{card}-card" class="card"' in html
        assert f'<div id="{card}-body"></div>' in html
    js = dashboard_assets.APP_JS
    assert 'refreshEvidence(request, "checks")' in js
    assert 'refreshEvidence(request, "review")' in js


def test_check_status_and_location_text() -> None:
    calls = [
        JsCall("checkStatusText", ({"passed": True, "exit_code": 0, "duration_seconds": 2.4},)),
        JsCall("checkStatusText", ({"passed": False, "timed_out": True},)),
        JsCall("checkStatusText", ({"passed": False, "exit_code": 2},)),
        JsCall("locationText", ({"path": "a.py", "start_line": 3, "end_line": 5},)),
        JsCall("locationText", ({"path": "a.py", "start_line": 3, "end_line": 3},)),
        JsCall("reviewVerdict", ({"skipped": True},)),
        JsCall("reviewVerdict", ({"approved": False},)),
    ]
    values = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]
    assert values == [
        "passed · exit 0 · 2 s",
        "timed out",
        "failed · exit 2",
        "a.py:3-5",
        "a.py:3",
        {"text": "skipped", "passed": True},
        {"text": "changes requested", "passed": False},
    ]
