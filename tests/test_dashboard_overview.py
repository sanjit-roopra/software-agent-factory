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
    run_stage,
    snapshot_overview,
    stage_label,
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
REDACTED = "[REDACTED]"
REVIEWER = "REVIEWER"
IMPLEMENTER = "IMPLEMENTER"
TRIAGE = "TRIAGE"
MINI = "gpt-5-mini"
SOL = "gpt-6.1-sol"
HAIKU = "claude-haiku-4.5"
LEGACY_REASON = "reviewer used legacy string blocker fields; leave them empty"
SECRET = "GH_TOKEN=ghp_abcdefgh12345678"
SECRET_TOKEN = "ghp_abcdefgh12345678"
FAILED_COUNT = "failed"
ACTIVE_COUNT = "active"
STATE_KEY = "state"
FIGURE = "figure"
MODEL_KEY = "model"
RUN_ID_KEY = "run_id"
LABEL_KEY = "label"
EXPECTED = "expected"
USAGE_KEY = "usage"
INVOCATION_COUNT = "invocation_count"
FAILED_LAST_24H = "failed_last_24h"
TOKENS_LAST_24H = "tokens_last_24h"
SCAN_TRUNCATED = "scan_truncated"
SUCCEEDED_COUNT = "succeeded"
INPUT_TOKENS = "input_tokens"
LIST_PRICE_ESTIMATE = "list_price_estimate_usd"
LIST_PRICE_USD = "list_price_usd"
PREMIUM_REQUESTS = "premium_requests"
TOKENS = "tokens"
RUN_A = "run-a"
OUTCOME_KEY = "outcome"
DETAIL_KEY = "detail"
PREMIUM_REQUEST_COST = "premium_request_cost"
MODELS_KEY = "models"
OVERVIEW_KEY = "overview"
FIGURE_AND_EXPECTED = (FIGURE, EXPECTED)
STATE_DONE = "DONE"
STATE_PR_READY = "PR_READY"
STATE_NEEDS_HUMAN = "NEEDS_HUMAN"
STATE_IMPLEMENTING = "IMPLEMENTING"
KIND_DONE = "done"
KIND_FAILED = "failed"
KIND_NEEDS_YOU = "needs_you"
KIND_ACTIVE = "active"
KIND_STALE = "stale"
LABEL_ACTIVE = "Active"
LABEL_FAILED = "Failed"
LABEL_PR_READY = "PR ready"
IS_FINISHED = "is_finished"
FAILURE_LINK = "failure_link"
FAILURE_REASON = "failure_reason"
DURATION_MS = "duration_ms"
STALE_LABEL = "Active, stale"
STARTED = "2026-10-02T11:22:17.854307Z"


def _impl(model: str) -> str:
    return f"impl {model}"


def _call(role: str, model: str = MINI, status: str = SUCCESS, **extra: Any) -> dict[str, Any]:
    return {"role": role, MODEL_KEY: model, "status": status, **extra}


def _run(state: str, **extra: Any) -> dict[str, Any]:
    return {RUN_ID_KEY: RUN_ID, STATE_KEY: state, **extra}


@pytest.mark.parametrize(
    (STATE_KEY, "kind", LABEL_KEY),
    [
        (STATE_DONE, KIND_DONE, "Done"),
        (FAILED, KIND_FAILED, LABEL_FAILED),
        (STATE_IMPLEMENTING, KIND_ACTIVE, LABEL_ACTIVE),
        ("CI_RUNNING", KIND_ACTIVE, LABEL_ACTIVE),
    ],
)
def test_run_outcome_names_every_state_in_words(state: str, kind: str, label: str) -> None:
    assert run_outcome(_run(state)) == {"kind": kind, LABEL_KEY: label}


@pytest.mark.parametrize(
    ("run", "action", LABEL_KEY),
    [
        ({"resume_classification": "RISK_APPROVAL"}, "waiting", "Needs approval"),
        ({"escalation": {"resume_classification": "PLAN_DECISION"}}, "waiting", "Needs answers"),
        ({"resume_classification": "DELIVERY_RETRY"}, "retry", "Needs retry"),
        ({"resume_classification": "ATTEMPT_GRANT"}, "grant", "Needs attempt"),
        ({"resume_classification": "ACCEPT_FINDINGS"}, "accept", "Needs acceptance"),
        ({"resume_classification": "SCOPE_APPROVAL"}, "widen", "Needs scope approval"),
        ({"resume_classification": "CI_REPAIR_RETRY"}, "repair", "Needs CI retry"),
        ({"resume_classification": "NOT_RESUMABLE"}, "stopped", "Stopped, needs you"),
        ({"escalation": "not a block"}, "stopped", "Stopped, needs you"),
        ({}, "stopped", "Stopped, needs you"),
    ],
    ids=[
        "approve",
        "answer-in-escalation",
        "retry",
        "grant",
        "accept",
        "widen",
        "repair",
        "not-resumable",
        "bad-block",
        "none",
    ],
)
def test_a_waiting_run_outcome_says_what_it_needs(
    run: dict[str, Any], action: str, label: str
) -> None:
    assert run_outcome(_run(STATE_NEEDS_HUMAN, **run)) == {
        "kind": KIND_NEEDS_YOU,
        LABEL_KEY: label,
        "action": action,
    }


def test_a_finished_pr_ready_run_is_done() -> None:
    outcome = run_outcome(_run(STATE_PR_READY, is_finished=True))

    assert outcome == {"kind": KIND_DONE, LABEL_KEY: LABEL_PR_READY}


@pytest.mark.parametrize(
    "extra", [{IS_FINISHED: False}, {}, {IS_FINISHED: "yes"}], ids=["unfinished", "unknown", "text"]
)
def test_a_pr_ready_run_that_is_not_finished_is_active(extra: dict[str, Any]) -> None:
    outcome = run_outcome(_run(STATE_PR_READY, **extra))

    assert outcome == {"kind": KIND_ACTIVE, LABEL_KEY: LABEL_ACTIVE}


def test_an_unfinished_pr_ready_run_that_is_stale_says_so_in_its_label() -> None:
    outcome = run_outcome(_run(STATE_PR_READY, is_finished=False, is_stale=True))

    assert outcome == {"kind": KIND_STALE, LABEL_KEY: STALE_LABEL}


def test_an_active_run_that_is_stale_says_so_in_its_label() -> None:
    outcome = run_outcome(_run(STATE_IMPLEMENTING, is_stale=True))

    assert outcome == {"kind": KIND_STALE, LABEL_KEY: STALE_LABEL}


def test_an_unknown_state_counts_as_active() -> None:
    assert run_outcome({RUN_ID_KEY: RUN_ID})["kind"] == KIND_ACTIVE


@pytest.mark.parametrize(
    (STATE_KEY, "reason", EXPECTED),
    [
        (FAILED, "boom", "boom"),
        (STATE_NEEDS_HUMAN, "stopped", "stopped"),
        (STATE_DONE, "left over", None),
        (STATE_IMPLEMENTING, "retrying", None),
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
        "kind": KIND_FAILED,
        "text": "Failed: boom",
    }


def test_a_failed_run_without_a_reason_says_only_failed() -> None:
    assert headline(_run(FAILED)) == {"kind": KIND_FAILED, "text": LABEL_FAILED}


def test_a_waiting_run_headline_says_needs_you_and_the_reason() -> None:
    assert headline(_run(STATE_NEEDS_HUMAN, failure_reason="the plan needs decisions")) == {
        "kind": KIND_NEEDS_YOU,
        "text": "Stopped, needs you: the plan needs decisions",
    }


def test_a_waiting_run_headline_without_a_reason_says_needs_you() -> None:
    assert headline(_run(STATE_NEEDS_HUMAN)) == {
        "kind": KIND_NEEDS_YOU,
        "text": "Stopped, needs you",
    }


@pytest.mark.parametrize(
    (STATE_KEY, "text"), [(STATE_DONE, "Done"), ("VERIFYING", "In progress: Verifying")]
)
def test_other_headlines_say_the_state_in_words(state: str, text: str) -> None:
    assert headline(_run(state))["text"] == text


def test_a_finished_pr_ready_headline_says_pr_ready() -> None:
    assert headline(_run(STATE_PR_READY, is_finished=True))["text"] == LABEL_PR_READY


def test_an_unfinished_pr_ready_headline_says_it_is_in_progress() -> None:
    headline_of_run = headline(_run(STATE_PR_READY, is_finished=False))

    assert headline_of_run == {"kind": KIND_ACTIVE, "text": "In progress: PR ready"}


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


@pytest.mark.parametrize(
    "reason", ["reviewers rejected it", "prereviewer rejected it", "reviewer2 rejected it"]
)
def test_a_reason_that_only_contains_the_role_inside_a_word_marks_the_last_call_only(
    reason: str,
) -> None:
    calls = [_call(REVIEWER)]

    flag_failing_call(_run(FAILED, failure_reason=reason), calls)

    assert _links(calls) == [FLAG_LAST_CALL]


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


@pytest.mark.parametrize(STATE_KEY, [STATE_DONE, STATE_NEEDS_HUMAN, STATE_IMPLEMENTING])
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
        MODEL_KEY: model,
        "success": True,
        "started_at": "2026-10-02T11:00:00+00:00",
        "completed_at": "2026-10-02T11:00:02+00:00",
        USAGE_KEY: {INPUT_TOKENS: 100, "output_tokens": 20, "cache_read_tokens": 5},
    }


def _raw_failed_detail(**extra: Any) -> dict[str, Any]:
    return {
        RUN_ID_KEY: RUN_ID,
        STATE_KEY: FAILED,
        FAILURE_REASON: LEGACY_REASON,
        "invocations": [_raw_invocation(1, IMPLEMENTER, SOL), _raw_invocation(2, REVIEWER, HAIKU)],
        **extra,
    }


def test_the_detail_view_marks_the_rejected_call() -> None:
    detail = run_detail_view(_raw_failed_detail())

    assert [call[FAILURE_LINK] for call in detail["invocations"]] == [None, FLAG_REJECTED]


def test_the_detail_view_names_the_outcome_of_a_failed_run() -> None:
    detail = run_detail_view(_raw_failed_detail())

    assert detail[OUTCOME_KEY] == {"kind": KIND_FAILED, LABEL_KEY: LABEL_FAILED}


def test_the_detail_view_leads_with_the_failure() -> None:
    detail = run_detail_view(_raw_failed_detail())

    assert detail["headline"] == {"kind": KIND_FAILED, "text": f"Failed: {LEGACY_REASON}"}


def test_the_detail_view_of_a_run_whose_pr_is_not_finished_is_active() -> None:
    detail = run_detail_view({RUN_ID_KEY: RUN_ID, STATE_KEY: STATE_PR_READY, IS_FINISHED: False})

    assert detail[OUTCOME_KEY] == {"kind": KIND_ACTIVE, LABEL_KEY: LABEL_ACTIVE}


def test_the_detail_view_of_a_failed_run_has_no_needs_you_step() -> None:
    assert run_detail_view(_raw_failed_detail())["next_step"]["kind"] == "none"


def test_the_detail_view_totals_the_tokens_of_every_call() -> None:
    totals = run_detail_view(_raw_failed_detail())["totals"]

    assert totals["total_tokens"] == {"total": 250, "reported_count": 2}


def test_run_totals_total_tokens_reads_the_usage_of_a_call_without_a_total() -> None:
    calls = [{USAGE_KEY: {INPUT_TOKENS: 10, "reasoning_tokens": 99}}, {USAGE_KEY: None}]

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
        "text": f"{MINI} ×4 · {_impl(SOL)} · review {HAIKU}",
        DETAIL_KEY: f"triage, refiner, planner, tester: {MINI}; impl: {SOL}; review: {HAIKU}",
    }


def test_models_summary_names_the_role_of_a_model_one_role_used_even_for_many_calls() -> None:
    calls = [_call(IMPLEMENTER, SOL), _call(IMPLEMENTER, SOL)]

    assert models_summary(calls)["text"] == _impl(SOL)


def test_models_summary_lists_each_model_of_a_role_that_changed_model() -> None:
    calls = [_call(IMPLEMENTER, SOL), _call(IMPLEMENTER, HAIKU)]

    assert models_summary(calls)["text"] == f"{_impl(SOL)} · {_impl(HAIKU)}"


def test_models_summary_skips_calls_without_a_model() -> None:
    calls = [{"role": TRIAGE, MODEL_KEY: None}, {"role": TRIAGE}, _call(TRIAGE, "")]

    assert models_summary(calls) == {"text": "", DETAIL_KEY: ""}


def test_role_label_falls_back_to_the_lower_case_role() -> None:
    assert role_label("SCRIBE") == "scribe"


def _raw_summary(**extra: Any) -> dict[str, Any]:
    return {
        RUN_ID_KEY: RUN_ID,
        "work_item_id": "WI-1",
        "title": "Add a farewell function",
        STATE_KEY: FAILED,
        "created_at": STARTED,
        INVOCATION_COUNT: 2,
        FAILURE_REASON: LEGACY_REASON,
        USAGE_KEY: {"list_price_estimate_usd": 0.039188, PREMIUM_REQUEST_COST: 2.0},
        "calls": [
            {"role": IMPLEMENTER, MODEL_KEY: SOL, DURATION_MS: 1500},
            {"role": REVIEWER, MODEL_KEY: HAIKU, DURATION_MS: 500},
        ],
        **extra,
    }


def test_a_run_list_row_names_its_outcome() -> None:
    run = run_summary_view(_raw_summary())

    assert run[OUTCOME_KEY] == {"kind": KIND_FAILED, LABEL_KEY: LABEL_FAILED}


def test_a_run_list_row_carries_the_reason_of_a_stopped_run() -> None:
    assert run_summary_view(_raw_summary())["why"] == LEGACY_REASON


def test_a_run_list_row_names_its_models() -> None:
    run = run_summary_view(_raw_summary())

    assert run[MODELS_KEY] == {
        "text": f"{_impl(SOL)} · review {HAIKU}",
        DETAIL_KEY: f"impl: {SOL}; review: {HAIKU}",
    }


def test_a_run_list_row_adds_up_the_length_of_its_calls() -> None:
    assert run_summary_view(_raw_summary())[DURATION_MS] == 2000


def test_a_run_list_row_keeps_its_call_count() -> None:
    assert run_summary_view(_raw_summary())[INVOCATION_COUNT] == 2


@pytest.mark.parametrize(
    ("unit", EXPECTED),
    [(LIST_PRICE_ESTIMATE, 0.039188), (PREMIUM_REQUEST_COST, 2.0)],
)
def test_a_run_list_row_keeps_each_cost_unit(unit: str, expected: float) -> None:
    assert run_summary_view(_raw_summary())[USAGE_KEY][unit] == pytest.approx(expected)


def test_a_run_list_row_replaces_its_calls() -> None:
    assert "calls" not in run_summary_view(_raw_summary())


@pytest.mark.parametrize(
    ("field", EXPECTED),
    [(MODELS_KEY, {"text": "", DETAIL_KEY: ""}), (DURATION_MS, None), ("why", None)],
)
def test_a_run_list_row_without_calls_has_no_models_no_duration_and_no_reason(
    field: str, expected: object
) -> None:
    run = run_summary_view({RUN_ID_KEY: RUN_ID, STATE_KEY: "CREATED"})

    assert run[field] == expected


def _redacting_row() -> dict[str, Any]:
    return run_summary_view(
        _raw_summary(
            title=f"fix {SECRET}",
            failure_reason=f"boom {SECRET}",
            calls=[{"role": TRIAGE, MODEL_KEY: f"m {SECRET}", DURATION_MS: 1}],
        )
    )


def test_a_run_list_row_redacts_the_reason() -> None:
    assert _redacting_row()["why"] == f"boom {REDACTED}"


def test_a_run_list_row_redacts_the_title() -> None:
    assert _redacting_row()["title"] == f"fix {REDACTED}"


def test_a_run_list_row_redacts_the_model_names() -> None:
    assert _redacting_row()[MODELS_KEY] == {
        "text": f"triage m {REDACTED}",
        DETAIL_KEY: f"triage: m {REDACTED}",
    }


def test_a_run_list_row_never_carries_a_secret() -> None:
    assert SECRET_TOKEN not in str(_redacting_row())


def test_a_run_list_row_bounds_a_long_reason() -> None:
    run = run_summary_view(_raw_summary(failure_reason="x" * (REASON_LIMIT * 4)))

    assert len(run["why"]) <= REASON_LIMIT


def test_a_run_list_row_sends_a_stopped_run_reason_once_as_why() -> None:
    run = run_summary_view(_raw_summary(failure_reason="x" * (REASON_LIMIT * 4)))

    assert run["why"].startswith("x")
    assert "failure_reason" not in run
    assert "failure_reason_truncated" not in run


def test_a_run_list_row_keeps_only_the_cost_totals_of_its_usage() -> None:
    usage = {"input_tokens": 5, "list_price_estimate_usd": 0.5, "premium_request_cost": 2}
    run = run_summary_view(_raw_summary(usage=usage))

    assert run["usage"] == {"list_price_estimate_usd": 0.5, "premium_request_cost": 2}


def _junk_calls_row() -> dict[str, Any]:
    call = {"role": TRIAGE, MODEL_KEY: MINI, DURATION_MS: 5, "log": SECRET}
    junk = ["not a call", {"role": "x y", DURATION_MS: -1}, {"role": TRIAGE, MODEL_KEY: 42}]
    return run_summary_view(_raw_summary(calls=[call, *junk]))


def test_a_run_list_row_drops_a_call_field_it_does_not_list() -> None:
    assert SECRET_TOKEN not in str(_junk_calls_row())


def test_a_run_list_row_drops_a_call_that_names_no_model_text() -> None:
    assert _junk_calls_row()[MODELS_KEY]["text"] == f"triage {MINI}"


def test_a_run_list_row_drops_a_length_that_is_not_a_count() -> None:
    assert _junk_calls_row()[DURATION_MS] == 5


def test_a_run_list_row_ignores_calls_that_are_not_a_list() -> None:
    assert run_summary_view(_raw_summary(calls="nope"))[MODELS_KEY]["text"] == ""


def _raw_snapshot(**extra: Any) -> dict[str, Any]:
    return {
        "counts": {
            SUCCEEDED_COUNT: 2,
            "escalated": 1,
            FAILED_COUNT: 3,
            ACTIVE_COUNT: 4,
            "stale_active": 0,
        },
        "needs_human_count": 1,
        FAILED_LAST_24H: 2,
        TOKENS_LAST_24H: 900,
        SCAN_TRUNCATED: False,
        "metrics": {
            USAGE_KEY: {
                INPUT_TOKENS: 1000,
                "output_tokens": 200,
                "reasoning_tokens": 500,
                "cache_read_tokens": 30,
                "cache_write_tokens": None,
                LIST_PRICE_ESTIMATE: 0.5,
                PREMIUM_REQUEST_COST: 3.0,
            }
        },
        **extra,
    }


@pytest.mark.parametrize(
    FIGURE_AND_EXPECTED,
    [
        ("runs", 10),
        (SUCCEEDED_COUNT, 2),
        (FAILED_COUNT, 3),
        (ACTIVE_COUNT, 4),
        ("needs_you", 1),
        (TOKENS, 1230),
        (FAILED_LAST_24H, 2),
        (TOKENS_LAST_24H, 900),
    ],
)
def test_the_overview_counts_runs_by_state_and_totals_tokens_without_reasoning(
    figure: str, expected: int
) -> None:
    assert snapshot_overview(_raw_snapshot())[figure] == expected


def test_a_stale_active_run_is_not_counted_twice_in_the_runs_total() -> None:
    counts = {
        SUCCEEDED_COUNT: 1,
        "escalated": 1,
        FAILED_COUNT: 1,
        ACTIVE_COUNT: 4,
        "stale_active": 3,
    }

    assert snapshot_overview(_raw_snapshot(counts=counts))["runs"] == 7


@pytest.mark.parametrize(FIGURE_AND_EXPECTED, [(LIST_PRICE_USD, 0.5), (PREMIUM_REQUESTS, 3.0)])
def test_the_overview_keeps_the_two_cost_units_apart(figure: str, expected: float) -> None:
    assert snapshot_overview(_raw_snapshot())[figure] == pytest.approx(expected)


@pytest.mark.parametrize(FIGURE, [LIST_PRICE_USD, PREMIUM_REQUESTS, TOKENS])
def test_the_overview_reports_a_figure_no_call_reported_as_none(figure: str) -> None:
    usage = {INPUT_TOKENS: None, LIST_PRICE_ESTIMATE: None}

    assert snapshot_overview(_raw_snapshot(metrics={USAGE_KEY: usage}))[figure] is None


@pytest.mark.parametrize(
    FIGURE_AND_EXPECTED,
    [("runs", 0), (TOKENS, None), (LIST_PRICE_USD, None), (SCAN_TRUNCATED, False)],
)
def test_the_overview_of_an_empty_snapshot_is_zero_runs_and_no_figures(
    figure: str, expected: object
) -> None:
    assert snapshot_overview({})[figure] == expected


def test_the_overview_says_when_the_scan_was_cut() -> None:
    assert snapshot_overview(_raw_snapshot(scan_truncated=True))[SCAN_TRUNCATED] is True


def test_the_overview_ignores_a_reported_count_that_is_not_a_count() -> None:
    overview = snapshot_overview(_raw_snapshot(counts={SUCCEEDED_COUNT: True, FAILED_COUNT: -1}))

    assert overview["runs"] == 0


def test_summary_view_carries_the_overview() -> None:
    summary = summary_view(_raw_snapshot(runs=[{RUN_ID_KEY: RUN_ID}], page={"limit": 1}))

    assert summary[OVERVIEW_KEY]["runs"] == 10


def test_summary_view_keeps_only_the_overview_and_the_calls_in_progress() -> None:
    summary = summary_view(_raw_snapshot(attempts_by_model={SECRET: 3}, runs=[], page={}))

    assert sorted(summary) == ["now_running", OVERVIEW_KEY]


def test_summary_view_of_something_that_is_not_a_snapshot_is_an_empty_overview() -> None:
    summary = summary_view(None)

    assert summary[OVERVIEW_KEY]["runs"] == 0


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _stored_run(
    run_id: str, state: WorkflowState, usage: UsageMetrics | None, *, finalized: bool = False
) -> FactoryRun:
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
        completed_at=NOW if state is WorkflowState.FAILED or finalized else None,
        invocation_records=[call],
    )


def _real_snapshot(tmp_path: Path, *runs: FactoryRun) -> dict[str, Any]:
    store = FileRunStore(tmp_path / "data")
    for run in runs:
        store.save_run(run)
    return build_monitoring_snapshot(store, now=NOW).model_dump(mode="json")


def _real_overview(tmp_path: Path) -> dict[str, Any]:
    usage = UsageMetrics(input_tokens=100, output_tokens=20, list_price_estimate_usd=0.25)
    failed = _stored_run(RUN_A, WorkflowState.FAILED, usage)
    active = _stored_run("run-b", WorkflowState.IMPLEMENTING, None)
    return snapshot_overview(_real_snapshot(tmp_path, failed, active))


@pytest.mark.parametrize(
    FIGURE_AND_EXPECTED,
    [
        ("runs", 2),
        (FAILED_COUNT, 1),
        (ACTIVE_COUNT, 1),
        (TOKENS, 120),
        (FAILED_LAST_24H, 1),
        (TOKENS_LAST_24H, 120),
        (PREMIUM_REQUESTS, None),
    ],
)
def test_the_overview_reads_the_fields_of_a_real_snapshot(
    tmp_path: Path, figure: str, expected: object
) -> None:
    assert _real_overview(tmp_path)[figure] == expected


def test_the_overview_reads_the_list_price_of_a_real_snapshot(tmp_path: Path) -> None:
    assert _real_overview(tmp_path)[LIST_PRICE_USD] == pytest.approx(0.25)


def _real_row(tmp_path: Path, *runs: FactoryRun) -> dict[str, Any]:
    return run_summary_view(_real_snapshot(tmp_path, *runs)["runs"][0])


def test_the_run_list_view_of_a_real_summary_names_its_models(tmp_path: Path) -> None:
    run = _real_row(tmp_path, _stored_run(RUN_A, WorkflowState.FAILED, None))

    assert run[MODELS_KEY]["text"] == f"review {HAIKU}"


def test_the_run_list_view_of_a_real_summary_names_its_length(tmp_path: Path) -> None:
    run = _real_row(tmp_path, _stored_run(RUN_A, WorkflowState.FAILED, None))

    assert run[DURATION_MS] == 20_000


def test_the_run_list_view_of_a_real_summary_names_its_outcome(tmp_path: Path) -> None:
    run = _real_row(tmp_path, _stored_run(RUN_A, WorkflowState.FAILED, None))

    assert run[OUTCOME_KEY]["kind"] == KIND_FAILED


def test_the_run_list_view_of_a_real_summary_keeps_its_call_count(tmp_path: Path) -> None:
    run = _real_row(tmp_path, _stored_run(RUN_A, WorkflowState.FAILED, None))

    assert run[INVOCATION_COUNT] == 1


def _unfinalized_pr_ready(tmp_path: Path) -> dict[str, Any]:
    return _real_snapshot(tmp_path, _stored_run(RUN_A, WorkflowState.PR_READY, None))


def _finalized_pr_ready(tmp_path: Path) -> dict[str, Any]:
    run = _stored_run(RUN_A, WorkflowState.PR_READY, None, finalized=True)
    return _real_snapshot(tmp_path, run)


def test_a_real_pr_ready_run_that_is_not_finalized_has_an_active_badge(tmp_path: Path) -> None:
    badge = run_summary_view(_unfinalized_pr_ready(tmp_path)["runs"][0])[OUTCOME_KEY]

    assert badge["kind"] == KIND_ACTIVE


def test_a_real_pr_ready_run_that_is_not_finalized_counts_as_active(tmp_path: Path) -> None:
    overview = snapshot_overview(_unfinalized_pr_ready(tmp_path))

    assert overview[ACTIVE_COUNT] == 1


def test_a_real_finalized_pr_ready_run_has_a_done_badge(tmp_path: Path) -> None:
    badge = run_summary_view(_finalized_pr_ready(tmp_path)["runs"][0])[OUTCOME_KEY]

    assert badge["kind"] == KIND_DONE


def test_a_real_finalized_pr_ready_run_counts_as_succeeded(tmp_path: Path) -> None:
    overview = snapshot_overview(_finalized_pr_ready(tmp_path))

    assert overview[SUCCEEDED_COUNT] == 1


@pytest.mark.parametrize(
    ("state", "label"),
    [
        (STATE_IMPLEMENTING, "Implementing"),
        ("CI_RUNNING", "CI running"),
        (STATE_PR_READY, "PR ready"),
        ("CI_DIAGNOSIS", "CI diagnosis"),
        ("", None),
        (None, None),
        (3, None),
    ],
)
def test_a_stage_label_says_the_state_in_words(state: object, label: str | None) -> None:
    assert stage_label(state) == label


def test_an_active_run_stage_names_the_role_of_the_call_in_progress() -> None:
    assert run_stage(_run(STATE_IMPLEMENTING), "IMPLEMENTER") == "Implementing \u00b7 implementer"
    assert run_stage(_run(STATE_IMPLEMENTING), None) == "Implementing"
    assert run_stage(_run(STATE_IMPLEMENTING, is_stale=True), "") == "Implementing"


def test_a_finished_run_has_no_stage() -> None:
    assert run_stage(_run(STATE_DONE), "reviewer") is None
    assert run_stage(_run(FAILED), "reviewer") is None
    assert run_stage({RUN_ID_KEY: RUN_ID}, "reviewer") is None


@pytest.mark.parametrize("state", [STATE_DONE, STATE_PR_READY])
def test_a_done_run_with_needs_look_reasons_says_so(state: str) -> None:
    run = {**_run(state, is_finished=True), "needs_look": ["review skipped"]}

    assert run_outcome(run) == {"kind": "done", LABEL_KEY: "Done, needs look"}


def test_a_run_list_row_carries_the_stage_and_drops_the_active_role() -> None:
    row = run_summary_view({**_run(STATE_IMPLEMENTING), "active_role": "implementer"})

    assert row["stage"] == "Implementing \u00b7 implementer"
    assert "active_role" not in row


@pytest.mark.parametrize(
    ("value", "label"),
    [("RISK_APPROVAL", "Needs approval"), ("<script>", "Stopped, needs you")],
)
def test_a_run_list_row_keeps_only_a_known_resume_classification(value: str, label: str) -> None:
    row = run_summary_view({**_run(STATE_NEEDS_HUMAN), "resume_classification": value})

    assert row["outcome"]["label"] == label
    assert row.get("resume_classification") in (None, "RISK_APPROVAL")


def test_a_run_detail_headline_names_the_active_call_role() -> None:
    detail = run_detail_view({**_run("REVIEWING"), "active_invocation": {"role": "REVIEWER"}})

    assert detail["headline"]["text"] == "In progress: Reviewing \u00b7 reviewer"


_NANO_AIU_PER_DOLLAR = 100_000_000_000


def test_the_week_cost_is_the_usage_value_and_keeps_the_list_price_apart() -> None:
    days = [
        {"invocation_count": 0},
        {"invocation_count": 1, "total_nano_aiu": 2 * _NANO_AIU_PER_DOLLAR, "input_tokens": 5},
        {"invocation_count": 2, "list_price_estimate_usd": 1.5, "premium_request_cost": 3},
    ]

    overview = snapshot_overview({"daily_usage": days})

    assert overview["week_calls"] == 3
    assert overview["week_cost_unit"] == "usage_value_usd"
    assert overview["week_usage_value_usd"] == pytest.approx(2.0)
    assert overview["week_list_price_usd"] == 1.5
    assert overview["week_cost_by_day"] == [0, pytest.approx(2.0), 0]
    assert overview["week_tokens"] == 5
    assert overview["week_premium_requests"] == 3


def test_the_week_cost_falls_back_to_the_list_price() -> None:
    overview = snapshot_overview({"daily_usage": [{"list_price_estimate_usd": 0.25}, {}]})

    assert overview["week_cost_unit"] == "list_price_estimate_usd"
    assert overview["week_cost_by_day"] == [0.25, 0]
    assert overview["week_usage_value_usd"] is None


@pytest.mark.parametrize(
    "days", [None, "x", [], [{}, {}]], ids=["missing", "text", "empty", "none"]
)
def test_a_week_with_no_cost_reports_none_and_not_zero(days: object) -> None:
    overview = snapshot_overview({"daily_usage": days})

    assert overview["week_calls"] is None
    assert overview["week_cost_unit"] is None
    assert overview["week_cost_by_day"] == []
    assert overview["week_tokens"] is None
    assert overview["week_premium_requests"] is None


@pytest.mark.parametrize(
    ("succeeded", "failed", "expected"),
    [(3, 1, 0.75), (0, 2, 0.0), (0, 0, None)],
    ids=["mixed", "all-failed", "nothing-finished"],
)
def test_the_success_rate_is_the_share_of_finished_runs_that_succeeded(
    succeeded: int, failed: int, expected: float | None
) -> None:
    counts = {"succeeded": succeeded, "failed": failed}

    assert snapshot_overview({"counts": counts})["success_rate"] == expected


def test_a_run_list_row_carries_its_total_tokens_and_the_usage_value() -> None:
    row = run_summary_view(
        {
            "run_id": RUN_ID,
            "state": "DONE",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_nano_aiu": 50_000_000_000},
        }
    )

    assert row["tokens"] == 15
    assert row["usage"] == {"usage_value_usd": 0.5}
