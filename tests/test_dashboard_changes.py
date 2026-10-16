"""Plan, diff and result card of the dashboard run page (ADR-047)."""

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
    DIFF_CHAR_LIMIT,
    MAX_EVIDENCE_ITEMS,
    REVIEW_TEXT_LIMIT,
    sanitize_run_diff,
    sanitize_run_plan,
)
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, DashboardServer, create_server
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanStep,
    Specification,
    WorkflowState,
)
from software_agent_factory.observability import build_run_diff, build_run_evidence
from software_agent_factory.store import FileRunStore

GH_SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)

PATCH = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 1..2 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-old = 1\n"
    f"+token = '{GH_SECRET}'\n"
    "+new = 2\n"
    "diff --git a/docs/new.md b/docs/new.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/docs/new.md\n"
    "@@ -0,0 +1 @@\n"
    "+# New\n"
    "diff --git a/gone.txt b/gone.txt\n"
    "deleted file mode 100644\n"
    "--- a/gone.txt\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n"
    "-bye\n"
    "diff --git a/old.py b/moved.py\n"
    "similarity index 100%\n"
    "rename from old.py\n"
    "rename to moved.py\n"
)

PLAN_EVIDENCE: dict[str, Any] = {
    "run_id": "run-001",
    "branch_name": "factory/run-001",
    "specification": {
        "problem": f"Fix it {GH_SECRET}",
        "acceptance_criteria": ["Works"],
        "constraints": [],
        "confidence": 0.9,
        "provenance": "must-not-be-exposed",
    },
    "execution_plan": {
        "summary": "Do the thing",
        "steps": [{"id": "s1", "goal": "Edit a.py", "likely_files": ["src/a.py"], "x": 1}],
        "expected_scope": {"modules": ["src"], "estimated_files_min": 1, "estimated_files_max": 3},
        "risks": ["None"],
        "provenance": "must-not-be-exposed",
    },
    "change_set": {
        "summary": "Edited a.py",
        "changed_files": ["src/a.py"],
        "tests_added": ["tests/test_a.py"],
        "commands_run": ["pytest"],
        "schema_version": 1,
    },
}


def test_plan_keeps_allowlisted_fields_and_redacts_text() -> None:
    payload = sanitize_run_plan(PLAN_EVIDENCE)

    assert payload["branch_name"] == "factory/run-001"
    assert payload["specification"] == {
        "problem": "Fix it [REDACTED]",
        "acceptance_criteria": ["Works"],
        "constraints": [],
        "assumptions": [],
        "unknowns": [],
    }
    plan = payload["execution_plan"]
    assert plan["steps"] == [
        {"id": "s1", "goal": "Edit a.py", "likely_files": ["src/a.py"], "validation": []}
    ]
    assert plan["expected_scope"] == {
        "modules": ["src"],
        "estimated_files_min": 1,
        "estimated_files_max": 3,
    }
    assert payload["change_set"] == {
        "summary": "Edited a.py",
        "changed_files": ["src/a.py"],
        "tests_added": ["tests/test_a.py"],
        "commands_run": ["pytest"],
    }
    assert "must-not-be-exposed" not in json.dumps(payload)


def test_plan_without_artifacts_or_with_bad_shapes_is_empty() -> None:
    payload = sanitize_run_plan(
        {"execution_plan": {"expected_scope": {"estimated_files_min": True}}}
    )

    assert payload["specification"] is None
    assert payload["change_set"] is None
    assert payload["branch_name"] is None
    assert payload["execution_plan"]["expected_scope"]["estimated_files_min"] is None
    plan = sanitize_run_plan({"execution_plan": {"summary": "s"}})["execution_plan"]
    assert plan["expected_scope"] is None


def test_plan_text_is_bounded() -> None:
    long = "x" * (REVIEW_TEXT_LIMIT * 2)
    payload = sanitize_run_plan(
        {"change_set": {"summary": long, "changed_files": ["f"] * (MAX_EVIDENCE_ITEMS + 5)}}
    )

    assert len(payload["change_set"]["summary"]) <= REVIEW_TEXT_LIMIT
    assert len(payload["change_set"]["changed_files"]) == MAX_EVIDENCE_ITEMS


def test_diff_is_split_per_file_with_counts_and_redacted() -> None:
    payload = sanitize_run_diff({"attempt": 2, "attempts": [1, 2, True, 0], "patch": PATCH})

    assert payload["attempt"] == 2
    assert payload["attempts"] == [1, 2]
    assert payload["has_patch"] is True
    summary = [(f["path"], f["status"], f["additions"], f["deletions"]) for f in payload["files"]]
    assert summary == [
        ("src/a.py", "modified", 2, 1),
        ("docs/new.md", "added", 1, 0),
        ("gone.txt", "deleted", 0, 1),
        ("moved.py", "renamed", 0, 0),
    ]
    assert (payload["additions"], payload["deletions"]) == (3, 2)
    assert payload["truncated"] is False
    assert GH_SECRET not in json.dumps(payload)
    assert payload["files"][0]["patch"].startswith("diff --git a/src/a.py")


def test_diff_counts_lines_that_look_like_file_headers_inside_a_hunk() -> None:
    patch = (
        "diff --git a/q.sql b/q.sql\n"
        "--- a/q.sql\n"
        "+++ b/q.sql\n"
        "@@ -1,2 +1,2 @@\n"
        "--- old comment\n"
        "+++ new comment\n"
        " select 1;\n"
    )
    payload = sanitize_run_diff({"attempt": 1, "attempts": [1], "patch": patch})

    assert (payload["additions"], payload["deletions"]) == (1, 1)


def test_a_missing_patch_is_empty() -> None:
    payload = sanitize_run_diff({"attempt": "x", "patch": None})

    assert payload == {
        "attempt": None,
        "attempts": [],
        "has_patch": False,
        "files": [],
        "additions": 0,
        "deletions": 0,
        "truncated": False,
    }


def test_files_past_the_diff_limit_keep_counts_only() -> None:
    big = "diff --git a/big b/big\n+++ b/big\n@@ -0,0 +1 @@\n" + "+x\n" * (
        DIFF_CHAR_LIMIT // 3 + 10
    )
    small = "diff --git a/small b/small\n+++ b/small\n+y\n"

    payload = sanitize_run_diff({"patch": "preamble\n" + small + big + small})

    first, cut, last = payload["files"]
    assert first["patch"] is not None
    assert cut["patch"] is None
    assert cut["additions"] == DIFF_CHAR_LIMIT // 3 + 10
    assert last["patch"] is not None
    assert payload["truncated"] is True


def test_too_many_files_are_cut() -> None:
    patch = "".join(f"diff --git a/f{n} b/f{n}\n+++ b/f{n}\n+x\n" for n in range(205))

    payload = sanitize_run_diff({"patch": patch})

    assert len(payload["files"]) == MAX_EVIDENCE_ITEMS
    assert payload["truncated"] is True


@pytest.mark.parametrize("sanitize", [sanitize_run_plan, sanitize_run_diff])
def test_evidence_that_is_not_an_object_is_refused(sanitize: Any) -> None:
    with pytest.raises(TypeError):
        sanitize(["nope"])


# -- builders ----------------------------------------------------------------


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


def _store_with_run(tmp_path: Path) -> FileRunStore:
    store = FileRunStore(tmp_path)
    store.save_run(
        FactoryRun(
            id="run-d",
            work_item_id="WI",
            state=WorkflowState.NEEDS_HUMAN,
            created_at=T0,
            updated_at=T0,
            branch_name="factory/run-d",
            attempt_records=[_attempt(1), _attempt(2)],
        )
    )
    return store


def test_build_run_diff_reads_latest_and_attempt_patches(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    store.save_patch("run-d", "first", attempt=1)
    store.save_patch("run-d", "second", attempt=2)

    latest = build_run_diff(store, "run-d")
    picked = build_run_diff(store, "run-d", 1)

    assert latest == {"run_id": "run-d", "attempt": None, "attempts": [1, 2], "patch": "second"}
    assert picked is not None and picked["patch"] == "first"
    assert build_run_diff(store, "run-d", 9) == {
        "run_id": "run-d",
        "attempt": 9,
        "attempts": [1, 2],
        "patch": None,
    }
    assert build_run_diff(store, "run-missing") is None


def test_build_run_evidence_carries_plan_artifacts(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    store.save_artifact("run-d", Specification(problem="P", confidence=0.5))
    store.save_artifact(
        "run-d",
        ExecutionPlan(
            summary="S",
            steps=[PlanStep(id="1", goal="G")],
            expected_scope=ExpectedScope(
                modules=["src"], estimated_files_min=1, estimated_files_max=2
            ),
        ),
    )
    store.save_artifact("run-d", ChangeSet(summary="C", changed_files=["a.py"]))

    evidence = build_run_evidence(store, "run-d")

    assert evidence is not None
    payload = sanitize_run_plan(evidence)
    assert payload["branch_name"] == "factory/run-d"
    assert payload["specification"]["problem"] == "P"
    assert payload["execution_plan"]["steps"][0]["goal"] == "G"
    assert payload["change_set"]["changed_files"] == ["a.py"]


# -- routes ------------------------------------------------------------------


def _snapshot(*, limit: int, offset: int) -> dict[str, Any]:
    return {"runs": [], "page": {"limit": limit, "offset": offset}}


@pytest.fixture
def start() -> Iterator[Any]:
    servers: list[DashboardServer] = []

    def _start(diff_provider: Any, evidence_provider: Any = None) -> DashboardServer:
        server = create_server(
            DashboardConfig(
                snapshot_provider=_snapshot,
                run_detail_provider=lambda run_id: None,
                run_evidence_provider=evidence_provider,
                run_diff_provider=diff_provider,
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


def _diff(run_id: str, attempt: int | None) -> dict[str, Any] | None:
    if run_id != "run-001":
        return None
    return {"run_id": run_id, "attempt": attempt, "attempts": [1, 2], "patch": PATCH}


def test_the_diff_route_passes_the_attempt_and_sanitizes(start: Any) -> None:
    server = start(_diff)

    status, latest = _get(server, "/api/runs/run-001/diff")
    picked_status, picked = _get(server, "/api/runs/run-001/diff?attempt=2")

    assert (status, picked_status) == (200, 200)
    assert latest == sanitize_run_diff(_diff("run-001", None))
    assert picked["attempt"] == 2


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/runs/run-001/diff?attempt=0", 400),
        ("/api/runs/run-001/diff?attempt=-1", 400),
        ("/api/runs/run-001/diff?attempt=1000", 400),
        ("/api/runs/run-404/diff", 404),
        ("/api/runs/..%2Fetc/diff", 404),
    ],
)
def test_the_diff_route_refuses_bad_input(start: Any, path: str, status: int) -> None:
    assert _get(start(_diff), path)[0] == status


def test_the_diff_route_is_404_without_a_provider(start: Any) -> None:
    assert _get(start(None), "/api/runs/run-001/diff")[0] == 404


def test_the_plan_route_answers_sanitized_plan(start: Any) -> None:
    server = start(None, lambda run_id: PLAN_EVIDENCE)

    status, payload = _get(server, "/api/runs/run-001/plan")

    assert status == 200
    assert payload == sanitize_run_plan(PLAN_EVIDENCE)


# -- page --------------------------------------------------------------------


def test_the_run_page_has_plan_diff_and_result_cards() -> None:
    html = dashboard_assets.render_index_html(token="t")
    for card in ("plan", "diff", "completion"):
        assert f'<section id="{card}-card" class="card"' in html
    assert '<select id="diff-attempt"></select>' in html
    js = dashboard_assets.APP_JS
    assert 'refreshEvidence(request, "plan")' in js
    assert 'refreshEvidence(request, "diff", query)' in js


def test_scope_counts_and_verification_text() -> None:
    calls = [
        JsCall(
            "scopeText",
            ({"modules": ["src", "tests"], "estimated_files_min": 2, "estimated_files_max": 5},),
        ),
        JsCall("scopeText", ({"modules": ["src"]},)),
        JsCall("scopeText", (None,)),
        JsCall("countsText", ({"additions": 1200, "deletions": 3},)),
        JsCall("verificationResult", ({"verification": {"passed": True}},)),
        JsCall("verificationResult", ({"verification": {"passed": False}},)),
        JsCall("verificationResult", ({},)),
    ]
    values = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]
    assert values == [
        "src, tests · 2-5 files",
        "src",
        "",
        "+1,200 −3",
        "passed",
        "failed",
        "—",
    ]
