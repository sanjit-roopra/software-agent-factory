"""The "Now running" strip and the run row link of the Runs page (audit U6 and U8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard_js import JsCall, function_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.now_running import MAX_NOW_RUNNING, sanitize_active_calls
from software_agent_factory.dashboard.view import summary_view
from software_agent_factory.models import (
    ActiveInvocation,
    AgentRole,
    AttemptBudget,
    FactoryRun,
    WorkflowState,
)
from software_agent_factory.now_running import MAX_ACTIVE_CALLS, active_call_briefs

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
STARTED = "2026-10-01T10:00:00Z"
_JS = dashboard_assets.APP_JS
_RUNS_HTML = dashboard_assets.render_index_html(token="t").split('<section id="view-runs"')[1]


def _run(run_id: str, *, state: WorkflowState, started_minutes: int | None) -> FactoryRun:
    active = (
        None
        if started_minutes is None
        else ActiveInvocation(
            invocation_number=1,
            role=AgentRole.REVIEWER,
            model="gpt-6.1-sol",
            reasoning="high",
            started_at=T0 + timedelta(minutes=started_minutes),
            budget=AttemptBudget.IMPLEMENTATION,
        )
    )
    return FactoryRun(
        id=run_id,
        work_item_id=f"WI-{run_id}",
        state=state,
        created_at=T0,
        updated_at=T0,
        active_invocation=active,
    )


def test_each_unfinished_run_with_a_call_in_progress_is_listed_newest_call_first() -> None:
    runs = [
        _run("run-old", state=WorkflowState.REVIEWING, started_minutes=1),
        _run("run-idle", state=WorkflowState.PLANNING, started_minutes=None),
        _run("run-done", state=WorkflowState.DONE, started_minutes=9),
        _run("run-new", state=WorkflowState.IMPLEMENTING, started_minutes=5),
    ]

    briefs = active_call_briefs(runs, lambda run_id: f"Title of {run_id}")

    assert [brief.run_id for brief in briefs] == ["run-new", "run-old"]
    assert briefs[0].title == "Title of run-new"
    assert briefs[0].role is AgentRole.REVIEWER
    assert briefs[0].model == "gpt-6.1-sol"
    assert briefs[0].started_at == T0 + timedelta(minutes=5)


def test_the_list_stops_at_the_cap_and_reads_titles_only_for_the_runs_listed() -> None:
    runs = [
        _run(f"run-{n}", state=WorkflowState.REVIEWING, started_minutes=n)
        for n in range(MAX_ACTIVE_CALLS + 3)
    ]
    asked: list[str] = []

    briefs = active_call_briefs(runs, lambda run_id: asked.append(run_id) or None)

    assert len(briefs) == MAX_ACTIVE_CALLS
    assert asked == [brief.run_id for brief in briefs]


def test_the_sanitizer_keeps_the_shown_fields_and_drops_bad_ids_and_values() -> None:
    raw = [
        {
            "run_id": "run-1",
            "title": "Fix ghp_" + "a" * 36,
            "role": "REVIEWER",
            "model": "gpt-6.1-sol",
            "started_at": STARTED,
            "prompt": "never sent",
        },
        {"run_id": "../etc", "title": "bad id"},
        "not an object",
        {"run_id": "run-2", "role": "HACKER", "model": 3, "started_at": "soon", "title": None},
    ]

    calls = sanitize_active_calls(raw)

    assert [call["run_id"] for call in calls] == ["run-1", "run-2"]
    assert set(calls[0]) == {"run_id", "title", "role", "model", "started_at"}
    assert "ghp_" + "a" * 36 not in calls[0]["title"]
    assert calls[0]["role"] == "REVIEWER"
    assert calls[0]["started_at"] == STARTED
    assert calls[1] == {
        "run_id": "run-2",
        "title": None,
        "role": None,
        "model": None,
        "started_at": None,
    }
    assert sanitize_active_calls(None) == []
    assert len(sanitize_active_calls([raw[0]] * (MAX_NOW_RUNNING + 1))) == MAX_NOW_RUNNING


def test_the_summary_carries_the_calls_in_progress() -> None:
    snapshot = {"active_calls": [{"run_id": "run-1", "role": "TESTER", "started_at": STARTED}]}

    summary = summary_view(snapshot)

    assert set(summary) == {"overview", "now_running"}
    assert summary["now_running"][0]["run_id"] == "run-1"
    assert summary_view({})["now_running"] == []


def test_a_call_in_progress_links_to_its_run_and_names_its_role_and_model() -> None:
    call = {"run_id": "run 1", "title": "Fix the bug", "role": "REVIEWER", "model": "gpt-6.1-sol"}
    untitled = {"run_id": "run-2", "role": None, "model": None, "started_at": STARTED}
    results = run_functions(
        _JS,
        [
            JsCall("nowRunningSpec", ({**call, "started_at": STARTED},)),
            JsCall("nowRunningSpec", (untitled,)),
            JsCall("nowRunningSpec", ({"title": "No id"},)),
        ],
    )

    assert [result.value for result in results] == [
        {
            "href": "#run/run%201",
            "title": "Fix the bug",
            "detail": "Reviewer · gpt-6.1-sol",
            "since": STARTED,
        },
        {"href": "#run/run-2", "title": "run-2", "detail": "— · —", "since": STARTED},
        None,
    ]


def test_the_strip_sits_above_the_run_list_and_its_timers_tick() -> None:
    assert _RUNS_HTML.index('id="now-running"') < _RUNS_HTML.index('id="runs-body"')
    assert '<div id="now-running" class="card" hidden>' in _RUNS_HTML
    render = function_source(_JS, "renderNowRunning")
    assert "syncTick(elapsed, spec.since, " in render
    assert "renderNowRunning(summary?.now_running)" in function_source(_JS, "renderOverview")


def test_a_run_row_opens_through_its_stretched_title_link_not_a_click_handler() -> None:
    bindings = function_source(_JS, "bindControls")
    assert 'getElementById("runs-body").addEventListener' not in bindings
    css = dashboard_assets.STYLE_CSS
    assert "#runs-body tr[data-run-id] { position: relative; }" in css
    assert 'td:first-child > a::after { content: ""; position: absolute; inset: 0; }' in css
    assert "tr[data-run-id]:focus-within" in css
    assert "tr[data-run-id] { cursor: pointer; }" not in css
