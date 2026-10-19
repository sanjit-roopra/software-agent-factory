"""Per-attempt check history and review finding threads on the dashboard (ADR-047)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dashboard_js import JsCall, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.evidence import MAX_HISTORY, sanitize_run_checks
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    CICheckEvidence,
    CIReport,
    CommandResult,
    FactoryRun,
    ReviewDispositionStatus,
    ReviewFindingDraft,
    ReviewReport,
    ReviewSourceLocation,
    TestReport,
    VerificationReport,
    WorkflowState,
)
from software_agent_factory.review_ledger import finding_id
from software_agent_factory.run_evidence import HISTORY_LIMIT, build_run_evidence
from software_agent_factory.store import FileRunStore

GH_SECRET = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


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


def _run(store: FileRunStore, attempts: int) -> FactoryRun:
    run = FactoryRun(
        id="run-h",
        work_item_id="WI-1",
        state=WorkflowState.NEEDS_HUMAN,
        created_at=T0,
        updated_at=T0,
        attempt_records=[_attempt(n) for n in range(1, attempts + 1)],
    )
    store.save_run(run)
    return run


def _verification(command: str) -> VerificationReport:
    check = CommandResult(command=command, exit_code=1, stdout="boom", duration_seconds=1.0)
    return VerificationReport(passed=False, deterministic_checks=[check], confidence=1.0)


def test_history_lists_each_attempt_and_keeps_the_last_ones(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _run(store, HISTORY_LIMIT + 2)
    for number in range(1, HISTORY_LIMIT + 3):
        store.save_artifact(run.id, _verification(f"check-{number}"), attempt=number)
    store.save_artifact(
        run.id,
        CIReport(overall="FAIL", checks=[CICheckEvidence(name="ci", status="FAIL")]),
        attempt=2,
    )

    evidence = build_run_evidence(store, run.id)

    assert evidence is not None
    history = evidence["verification_history"]
    assert [item["attempt"] for item in history] == list(range(3, HISTORY_LIMIT + 3))
    assert history[-1]["checks"][0]["command"] == f"check-{HISTORY_LIMIT + 2}"
    assert evidence["ci_history"] == [
        {"attempt": 2, "overall": "FAIL", "checks": [evidence["ci_history"][0]["checks"][0]]}
    ]
    assert evidence["ci_history"][0]["checks"][0]["name"] == "ci"


def test_review_rounds_carry_the_ledger_id_of_each_new_finding(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _run(store, 1)
    draft = ReviewFindingDraft(
        category="CORRECTNESS",
        message="Bad",
        locations=[ReviewSourceLocation(path="a.py", start_line=1, end_line=2)],
    )
    store.save_artifact(run.id, ReviewReport(approved=False, repair_regressions=[draft]), attempt=1)

    evidence = build_run_evidence(store, run.id)

    assert evidence is not None
    assert evidence["review_rounds"][0]["repair_regressions"][0]["id"] == finding_id(draft)


def test_the_sanitizer_cap_matches_the_provider_cap() -> None:
    assert MAX_HISTORY == HISTORY_LIMIT


def test_sanitizer_redacts_history_and_drops_bad_entries() -> None:
    raw: dict[str, Any] = {
        "run_id": "run-h",
        "verification_history": [
            {"attempt": 1, "checks": [{"command": "pytest", "exit_code": 0, "stdout": GH_SECRET}]},
            {"attempt": 0, "checks": []},
            {"attempt": "2", "checks": []},
        ],
        "ci_history": [
            {
                "attempt": 2,
                "overall": "FAIL; rm -rf",
                "checks": [{"name": "ci", "status": "FAIL", "log_excerpt": GH_SECRET}],
                "extra": "dropped",
            }
        ]
        + [{"attempt": n, "overall": "PASS", "checks": []} for n in range(3, 20)],
    }

    payload = sanitize_run_checks(raw)

    [verification] = payload["verification_history"]
    assert verification["attempt"] == 1
    assert verification["checks"][0]["passed"] is True
    assert GH_SECRET not in verification["checks"][0]["output"]
    ci = payload["ci_history"]
    assert [item["attempt"] for item in ci] == list(range(10, 20))
    assert all(set(item) == {"attempt", "overall", "checks"} for item in ci)
    assert sanitize_run_checks({"ci_history": [raw["ci_history"][0]]})["ci_history"][0] == {
        "attempt": 2,
        "overall": None,
        "checks": [
            {
                "name": "ci",
                "status": "FAIL",
                "description": None,
                "details_url": None,
                "failure_category": None,
                "output": "[REDACTED]",
                "output_truncated": False,
            }
        ],
    }


def test_checks_for_an_attempt_and_the_attempt_list() -> None:
    payload = {
        "checks": [{"command": "latest"}],
        "ci_checks": [],
        "verification_history": [{"attempt": 1, "checks": [{"command": "first"}]}],
        "ci_history": [{"attempt": 3, "checks": [{"name": "ci"}]}, {"attempt": 1, "checks": []}],
    }
    calls = [
        JsCall("checkAttempts", (payload,)),
        JsCall("checksForAttempt", (payload, None)),
        JsCall("checksForAttempt", (payload, 1)),
        JsCall("checksForAttempt", (payload, 3)),
    ]
    values = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]
    assert values == [
        [1, 3],
        {"checks": [{"command": "latest"}], "ci_checks": []},
        {"checks": [{"command": "first"}], "ci_checks": []},
        {"checks": [], "ci_checks": [{"name": "ci"}]},
    ]


def test_findings_are_threaded_by_id_across_rounds() -> None:
    fixed = {"id": "review-a", "message": "A"}
    still_open = {"id": "review-b", "message": "B"}
    payload = {
        "rounds": [
            {"attempt_number": 1, "blocking_findings": [fixed, still_open]},
            {
                "attempt_number": 2,
                "blocking_findings": [still_open],
                "prior_finding_dispositions": [
                    {"finding_id": "review-a", "status": "RESOLVED", "rationale": "done"},
                    {"finding_id": "review-b", "status": "UNRESOLVED", "rationale": "no"},
                    {"finding_id": "review-z", "status": "RESOLVED", "rationale": "unknown"},
                ],
            },
            {
                "attempt_number": 3,
                "repair_regressions": [{"id": "review-c", "message": "C"}],
                "prior_finding_dispositions": [
                    {"finding_id": "review-b", "status": "WITHDRAWN", "rationale": "ok"}
                ],
            },
        ],
        "open_findings": [{"id": "review-c"}],
        "accepted_findings": [],
    }
    calls = [JsCall("findingThreads", (payload,))]
    [threads] = [result.value for result in run_functions(dashboard_assets.APP_JS, calls)]
    assert [thread["finding"]["id"] for thread in threads] == ["review-a", "review-b", "review-c"]
    assert [thread["ledger"] for thread in threads] == [None, None, "open"]
    life = run_functions(
        dashboard_assets.APP_JS, [JsCall("threadLifeText", (thread,)) for thread in threads]
    )
    assert [result.value for result in life] == [
        "raised attempt 1 · fixed attempt 2",
        "raised attempt 1 · not fixed attempt 2 · withdrawn attempt 3",
        "raised attempt 3",
    ]


def test_the_checks_card_has_an_attempt_picker() -> None:
    html = dashboard_assets.render_index_html(token="t")
    assert '<select id="checks-attempt"></select>' in html
    assert 'getElementById("checks-attempt").addEventListener("change"' in dashboard_assets.APP_JS


def test_disposition_statuses_have_words() -> None:
    js = dashboard_assets.APP_JS
    for status in ReviewDispositionStatus:
        assert f"{status.value}:" in js


def test_tester_history_keeps_each_attempts_verdict_and_nothing_else(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _run(store, 2)
    store.save_artifact(run.id, TestReport(passed=False, findings=["x"], confidence=0.5), attempt=2)

    evidence = build_run_evidence(store, run.id)

    assert evidence is not None
    assert evidence["tester_history"] == [{"attempt": 2, "passed": False, "skipped": False}]
    raw = {"tester_history": [{"attempt": 2, "passed": True, "findings": ["x"]}, {"attempt": 0}]}
    assert sanitize_run_checks(raw)["tester_history"] == [
        {"attempt": 2, "passed": True, "skipped": False}
    ]
