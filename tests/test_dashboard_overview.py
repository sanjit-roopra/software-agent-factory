"""The readable overview of the dashboard: run outcome, reason line, failing step, models and
the one-row summary of the run store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from software_agent_factory.dashboard.aggregate import models_summary, role_label, run_totals
from software_agent_factory.dashboard.overview import (
    FLAG_LAST_CALL,
    FLAG_REJECTED,
    flag_failing_call,
    headline,
    reason_line,
    run_outcome,
    snapshot_overview,
)
from software_agent_factory.dashboard.view import (
    run_detail_view,
    run_summary_view,
    summary_view,
)
from software_agent_factory.models import (
    AgentRole,
    FactoryRun,
    InvocationRecord,
    UsageMetrics,
    WorkflowState,
)
from software_agent_factory.observability import build_monitoring_snapshot
from software_agent_factory.redaction import REASON_LIMIT
from software_agent_factory.store import FileRunStore

RUN_ID = "run-1"
FAILED = "FAILED"
SUCCESS = "SUCCESS"
REVIEWER = "REVIEWER"
IMPLEMENTER = "IMPLEMENTER"
TRIAGE = "TRIAGE"
MINI = "gpt-5-mini"
SOL = "gpt-6.1-sol"
HAIKU = "claude-haiku-4.5"
LEGACY_REASON = "reviewer used legacy string blocker fields; leave them empty"
SECRET = "GH_TOKEN=ghp_abcdefgh12345678"
SECRET_TOKEN = "ghp_abcdefgh12345678"
FAILURE_LINK = "failure_link"
FAILURE_REASON = "failure_reason"
DURATION_MS = "duration_ms"
STARTED = "2026-10-02T11:22:17.854307Z"


def _call(role: str, model: str = MINI, status: str = SUCCESS, **extra: Any) -> dict[str, Any]:
    return {"role": role, "model": model, "status": status, **extra}


def _run(state: str, **extra: Any) -> dict[str, Any]:
    return {"run_id": RUN_ID, "state": state, **extra}


@pytest.mark.parametrize(
    ("state", "kind", "label"),
    [
        ("DONE", "done", "Done"),
        ("PR_READY", "done", "PR ready"),
        ("FAILED", "failed", "Failed"),
        ("NEEDS_HUMAN", "needs_you", "Needs you"),
        ("IMPLEMENTING", "active", "Active"),
        ("CI_RUNNING", "active", "Active"),
    ],
)
def test_run_outcome_names_every_state_in_words(state: str, kind: str, label: str) -> None:
    assert run_outcome(_run(state)) == {"kind": kind, "label": label}


def test_an_active_run_that_is_stale_says_so_in_its_label() -> None:
    assert run_outcome(_run("IMPLEMENTING", is_stale=True)) == {
        "kind": "active",
        "label": "Active, stale",
    }


def test_an_unknown_state_counts_as_active() -> None:
    assert run_outcome({"run_id": RUN_ID})["kind"] == "active"


@pytest.mark.parametrize(
    ("state", "reason", "expected"),
    [
        (FAILED, "boom", "boom"),
        ("NEEDS_HUMAN", "stopped", "stopped"),
        ("DONE", "left over", None),
        ("IMPLEMENTING", "retrying", None),
        (FAILED, None, None),
        (FAILED, "", None),
    ],
)
def test_reason_line_shows_the_reason_only_of_a_stopped_run(
    state: str, reason: str | None, expected: str | None
) -> None:
    assert reason_line(_run(state, failure_reason=reason)) == expected


def test_a_failed_run_headline_leads_with_failed_and_the_reason() -> None:
    assert headline(_run(FAILED, failure_reason="boom")) == {
        "kind": "failed",
        "text": "Failed: boom",
    }


def test_a_failed_run_without_a_reason_says_only_failed() -> None:
    assert headline(_run(FAILED)) == {"kind": "failed", "text": "Failed"}


def test_a_waiting_run_headline_says_needs_you_and_the_reason() -> None:
    assert headline(_run("NEEDS_HUMAN", failure_reason="the plan needs decisions")) == {
        "kind": "needs_you",
        "text": "Needs you: the plan needs decisions",
    }


def test_a_waiting_run_headline_without_a_reason_says_needs_you() -> None:
    assert headline(_run("NEEDS_HUMAN")) == {"kind": "needs_you", "text": "Needs you"}


@pytest.mark.parametrize(
    ("state", "text"),
    [("DONE", "Done"), ("PR_READY", "PR ready"), ("VERIFYING", "In progress: VERIFYING")],
)
def test_other_headlines_say_the_state_in_words(state: str, text: str) -> None:
    assert headline(_run(state))["text"] == text


def _links(calls: list[dict[str, Any]]) -> list[str | None]:
    return [call[FAILURE_LINK] for call in calls]


def test_a_reason_that_names_the_role_of_the_last_call_rejects_that_call() -> None:
    calls = [_call(IMPLEMENTER), _call(REVIEWER)]

    flag_failing_call(_run(FAILED, failure_reason=LEGACY_REASON), calls)

    assert _links(calls) == [None, FLAG_REJECTED]


def test_the_role_match_ignores_case_and_needs_the_whole_word() -> None:
    calls = [_call(TRIAGE)]

    flag_failing_call(_run(FAILED, failure_reason="Triage output was empty"), calls)

    assert _links(calls) == [FLAG_REJECTED]


def test_a_reason_that_does_not_name_the_role_marks_the_last_call_only() -> None:
    calls = [_call(IMPLEMENTER), _call("TESTER")]

    flag_failing_call(_run(FAILED, failure_reason="verification failed: 2 tests"), calls)

    assert _links(calls) == [None, FLAG_LAST_CALL]


def test_a_reason_that_names_an_earlier_role_marks_the_last_call_only() -> None:
    calls = [_call(REVIEWER), _call(IMPLEMENTER)]

    flag_failing_call(_run(FAILED, failure_reason=LEGACY_REASON), calls)

    assert _links(calls) == [None, FLAG_LAST_CALL]


def test_a_failed_last_call_needs_no_mark() -> None:
    calls = [_call(REVIEWER, status=FAILED)]

    flag_failing_call(_run(FAILED, failure_reason=LEGACY_REASON), calls)

    assert _links(calls) == [None]


def test_a_failed_run_without_a_reason_marks_the_last_call_only() -> None:
    calls = [_call(REVIEWER)]

    flag_failing_call(_run(FAILED), calls)

    assert _links(calls) == [FLAG_LAST_CALL]


@pytest.mark.parametrize("state", ["DONE", "NEEDS_HUMAN", "IMPLEMENTING"])
def test_a_run_that_did_not_fail_marks_no_call(state: str) -> None:
    calls = [_call(REVIEWER)]

    flag_failing_call(_run(state, failure_reason=LEGACY_REASON), calls)

    assert _links(calls) == [None]


def test_a_failed_run_without_calls_has_nothing_to_mark() -> None:
    calls: list[dict[str, Any]] = []

    flag_failing_call(_run(FAILED, failure_reason=LEGACY_REASON), calls)

    assert calls == []


def _raw_invocation(number: int, role: str, model: str = MINI) -> dict[str, Any]:
    return {
        "invocation_number": number,
        "role": role,
        "model": model,
        "success": True,
        "started_at": "2026-10-02T11:00:00+00:00",
        "completed_at": "2026-10-02T11:00:02+00:00",
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5},
    }


def _raw_failed_detail(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "state": FAILED,
        "failure_reason": LEGACY_REASON,
        "invocations": [_raw_invocation(1, IMPLEMENTER, SOL), _raw_invocation(2, REVIEWER, HAIKU)],
        **extra,
    }


def test_the_detail_view_marks_the_rejected_call_and_leads_with_the_failure() -> None:
    detail = run_detail_view(_raw_failed_detail())

    assert [call[FAILURE_LINK] for call in detail["invocations"]] == [None, FLAG_REJECTED]
    assert detail["outcome"] == {"kind": "failed", "label": "Failed"}
    assert detail["headline"] == {"kind": "failed", "text": f"Failed: {LEGACY_REASON}"}


def test_the_detail_view_of_a_failed_run_has_no_needs_you_step() -> None:
    assert run_detail_view(_raw_failed_detail())["next_step"]["kind"] == "none"


def test_the_detail_view_totals_the_tokens_of_every_call() -> None:
    totals = run_detail_view(_raw_failed_detail())["totals"]

    assert totals["total_tokens"] == {"total": 250, "reported_count": 2}


def test_run_totals_total_tokens_reads_the_usage_of_a_call_without_a_total() -> None:
    calls = [{"usage": {"input_tokens": 10, "reasoning_tokens": 99}}, {"usage": None}]

    assert run_totals(calls)["total_tokens"] == {"total": 10, "reported_count": 1}


def test_models_summary_collapses_a_model_that_several_roles_used() -> None:
    calls = [
        _call("TRIAGE"),
        _call("REFINER"),
        _call("PLANNER"),
        _call(IMPLEMENTER, SOL),
        _call("TESTER"),
        _call(REVIEWER, HAIKU),
    ]

    assert models_summary(calls) == {
        "text": f"{MINI} ×4 · impl {SOL} · review {HAIKU}",
        "detail": f"triage, refiner, planner, tester: {MINI}; impl: {SOL}; review: {HAIKU}",
    }


def test_models_summary_names_the_role_of_a_model_one_role_used_even_for_many_calls() -> None:
    calls = [_call(IMPLEMENTER, SOL), _call(IMPLEMENTER, SOL)]

    assert models_summary(calls)["text"] == f"impl {SOL}"


def test_models_summary_lists_each_model_of_a_role_that_changed_model() -> None:
    calls = [_call(IMPLEMENTER, SOL), _call(IMPLEMENTER, HAIKU)]

    assert models_summary(calls)["text"] == f"impl {SOL} · impl {HAIKU}"


def test_models_summary_skips_calls_without_a_model() -> None:
    calls = [{"role": TRIAGE, "model": None}, {"role": TRIAGE}, _call(TRIAGE, "")]

    assert models_summary(calls) == {"text": "", "detail": ""}


def test_role_label_falls_back_to_the_lower_case_role() -> None:
    assert role_label("SCRIBE") == "scribe"


def _raw_summary(**extra: Any) -> dict[str, Any]:
    return {
        "run_id": RUN_ID,
        "work_item_id": "WI-1",
        "title": "Add a farewell function",
        "state": FAILED,
        "created_at": STARTED,
        "invocation_count": 2,
        "failure_reason": LEGACY_REASON,
        "usage": {"list_price_estimate_usd": 0.039188, "premium_request_cost": 2.0},
        "calls": [
            {"role": IMPLEMENTER, "model": SOL, DURATION_MS: 1500},
            {"role": REVIEWER, "model": HAIKU, DURATION_MS: 500},
        ],
        **extra,
    }


def test_a_run_list_row_carries_what_the_list_shows() -> None:
    run = run_summary_view(_raw_summary())

    assert run["outcome"] == {"kind": "failed", "label": "Failed"}
    assert run["why"] == LEGACY_REASON
    assert run["models"] == {
        "text": f"impl {SOL} · review {HAIKU}",
        "detail": f"impl: {SOL}; review: {HAIKU}",
    }
    assert run[DURATION_MS] == 2000
    assert run["invocation_count"] == 2
    assert run["usage"]["list_price_estimate_usd"] == pytest.approx(0.039188)
    assert run["usage"]["premium_request_cost"] == pytest.approx(2.0)
    assert "calls" not in run


def test_a_run_list_row_without_calls_has_no_models_and_no_duration() -> None:
    run = run_summary_view({"run_id": RUN_ID, "state": "CREATED"})

    assert run["models"] == {"text": "", "detail": ""}
    assert run[DURATION_MS] is None
    assert run["why"] is None


def test_a_run_list_row_redacts_the_reason_the_title_and_the_models() -> None:
    run = run_summary_view(
        _raw_summary(
            title=f"fix {SECRET}",
            failure_reason=f"boom {SECRET}",
            calls=[{"role": TRIAGE, "model": f"m {SECRET}", DURATION_MS: 1}],
        )
    )

    assert SECRET_TOKEN not in str(run)
    assert run["why"] == "boom [REDACTED]"
    assert run["title"] == "fix [REDACTED]"


def test_a_run_list_row_bounds_a_long_reason() -> None:
    run = run_summary_view(_raw_summary(failure_reason="x" * (REASON_LIMIT * 4)))

    assert len(run["why"]) <= REASON_LIMIT
    assert run["failure_reason_truncated"] is True


def test_a_run_list_row_drops_a_call_field_it_does_not_list() -> None:
    call = {"role": TRIAGE, "model": MINI, DURATION_MS: 5, "log": SECRET}

    run = run_summary_view(
        _raw_summary(calls=[call, "not a call", {"role": "x y", DURATION_MS: -1}])
    )

    assert SECRET_TOKEN not in str(run)
    assert run[DURATION_MS] == 5


def test_a_run_list_row_ignores_calls_that_are_not_a_list() -> None:
    assert run_summary_view(_raw_summary(calls="nope"))["models"]["text"] == ""


def _raw_snapshot(**extra: Any) -> dict[str, Any]:
    return {
        "counts": {"succeeded": 2, "escalated": 1, "failed": 3, "active": 4, "stale_active": 0},
        "needs_human_count": 1,
        "failed_last_24h": 2,
        "tokens_last_24h": 900,
        "scan_truncated": False,
        "metrics": {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "reasoning_tokens": 500,
                "cache_read_tokens": 30,
                "cache_write_tokens": None,
                "list_price_estimate_usd": 0.5,
                "premium_request_cost": 3.0,
            }
        },
        **extra,
    }


def test_the_overview_counts_runs_by_state_and_totals_tokens_without_reasoning() -> None:
    overview = snapshot_overview(_raw_snapshot())

    assert overview["runs"] == 10
    assert (overview["succeeded"], overview["failed"]) == (2, 3)
    assert (overview["active"], overview["needs_you"]) == (4, 1)
    assert overview["tokens"] == 1230
    assert overview["failed_last_24h"] == 2
    assert overview["tokens_last_24h"] == 900


def test_the_overview_keeps_the_two_cost_units_apart() -> None:
    overview = snapshot_overview(_raw_snapshot())

    assert overview["list_price_usd"] == pytest.approx(0.5)
    assert overview["premium_requests"] == pytest.approx(3.0)


def test_the_overview_reports_a_cost_unit_no_call_reported_as_none() -> None:
    usage = {"input_tokens": None, "list_price_estimate_usd": None}

    overview = snapshot_overview(_raw_snapshot(metrics={"usage": usage}))

    assert overview["list_price_usd"] is None
    assert overview["premium_requests"] is None
    assert overview["tokens"] is None


def test_the_overview_of_an_empty_snapshot_is_zero_runs_and_no_figures() -> None:
    overview = snapshot_overview({})

    assert overview["runs"] == 0
    assert overview["tokens"] is None
    assert overview["list_price_usd"] is None
    assert overview["scan_truncated"] is False


def test_the_overview_says_when_the_scan_was_cut() -> None:
    assert snapshot_overview(_raw_snapshot(scan_truncated=True))["scan_truncated"] is True


def test_the_overview_ignores_a_reported_count_that_is_not_a_count() -> None:
    overview = snapshot_overview(_raw_snapshot(counts={"succeeded": True, "failed": -1}))

    assert overview["runs"] == 0


def test_summary_view_drops_the_run_page_and_adds_the_overview() -> None:
    summary = summary_view(_raw_snapshot(runs=[{"run_id": RUN_ID}], page={"limit": 1}))

    assert "runs" not in summary
    assert "page" not in summary
    assert summary["overview"]["runs"] == 10
    assert summary["counts"]["failed"] == 3


def test_summary_view_of_something_that_is_not_a_snapshot_is_an_empty_overview() -> None:
    summary = summary_view(None)

    assert summary["overview"]["runs"] == 0


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _stored_run(run_id: str, state: WorkflowState, usage: UsageMetrics | None) -> FactoryRun:
    started = NOW - timedelta(minutes=5)
    call = InvocationRecord(
        invocation_number=1,
        role=AgentRole.REVIEWER,
        model=HAIKU,
        reasoning="low",
        started_at=started,
        completed_at=started + timedelta(seconds=20),
        success=True,
        usage=usage,
    )
    return FactoryRun(
        id=run_id,
        work_item_id=f"WI-{run_id}",
        state=state,
        created_at=started,
        updated_at=NOW,
        completed_at=NOW if state is WorkflowState.FAILED else None,
        invocation_records=[call],
    )


def test_the_overview_reads_the_fields_of_a_real_snapshot(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    usage = UsageMetrics(input_tokens=100, output_tokens=20, list_price_estimate_usd=0.25)
    store.save_run(_stored_run("run-a", WorkflowState.FAILED, usage))
    store.save_run(_stored_run("run-b", WorkflowState.IMPLEMENTING, None))

    snapshot = build_monitoring_snapshot(store, now=NOW).model_dump(mode="json")
    overview = snapshot_overview(snapshot)

    assert (overview["runs"], overview["failed"], overview["active"]) == (2, 1, 1)
    assert overview["tokens"] == 120
    assert overview["list_price_usd"] == pytest.approx(0.25)
    assert overview["premium_requests"] is None
    assert overview["failed_last_24h"] == 1
    assert overview["tokens_last_24h"] == 120


def test_the_run_list_view_of_a_real_summary_names_its_models_and_length(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    store.save_run(_stored_run("run-a", WorkflowState.FAILED, None))

    snapshot = build_monitoring_snapshot(store, now=NOW).model_dump(mode="json")
    run = run_summary_view(snapshot["runs"][0])

    assert run["models"]["text"] == f"review {HAIKU}"
    assert run[DURATION_MS] == 20_000
    assert run["outcome"]["kind"] == "failed"
    assert run["invocation_count"] == 1
