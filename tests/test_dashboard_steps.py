"""The Steps timeline: tags, runtime chips, tool calls and the start offset (slice 12)."""

from __future__ import annotations

import re
from typing import Any

from dashboard_js import JsCall, function_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets

_JS = dashboard_assets.APP_JS


def _values(*calls: JsCall) -> list[Any]:
    return [result.value for result in run_functions(_JS, calls)]


def test_tags_show_only_an_unusual_purpose_or_context_tier() -> None:
    usual = {"purpose": "STANDARD", "context_tier": "default"}
    unusual = {"purpose": "DECOMPOSE_PROJECT", "context_tier": "long_context"}

    assert _values(JsCall("stepTags", (usual,)), JsCall("stepTags", (unusual,))) == [
        [],
        [{"text": "decompose project"}, {"text": "long context"}],
    ]


def test_runtime_chips_name_the_runtime_and_flag_a_fallback_with_its_reason() -> None:
    plain = {"runtime": "claude-code", "fallback_reason": None}
    fell_back = {"runtime": "pi", "fallback_reason": "usage limit"}

    assert _values(
        JsCall("runtimeChips", ({},)),
        JsCall("runtimeChips", (plain,)),
        JsCall("runtimeChips", (fell_back,)),
    ) == [
        [],
        [{"text": "claude-code"}],
        [
            {"text": "pi"},
            {"text": "fell back", "title": "usage limit", "className": "chip chip-warn"},
        ],
    ]


def test_tool_calls_and_the_fallback_reason_join_the_expanded_fields() -> None:
    tools = [{"name": "Bash", "preview": "pytest -q"}, {"name": "TodoWrite", "preview": ""}]

    assert _values(
        JsCall("toolCallFields", (None,)),
        JsCall("toolCallFields", (tools,)),
        JsCall("toolCallFields", (tools, 2)),
        JsCall("toolCallFields", (tools, 37)),
        JsCall("fallbackFields", ("",)),
        JsCall("fallbackFields", ("usage limit",)),
    ) == [
        [],
        [["Tool calls (2)", "Bash  pytest -q\nTodoWrite"]],
        [["Tool calls (2)", "Bash  pytest -q\nTodoWrite"]],
        [["Tool calls (latest 2 of 37)", "Bash  pytest -q\nTodoWrite"]],
        [],
        [["Why it fell back", "usage limit"]],
    ]
    fields = function_source(_JS, "callFields")
    assert '["Runtime", displayValue(call.runtime, NOT_REPORTED)]' in fields
    assert "...toolCallFields(call.tool_calls, call.tool_call_count)" in fields


def test_each_step_says_when_it_started_relative_to_the_first() -> None:
    calls = [
        {"started_at": "2026-01-01T00:00:00+00:00", "duration_ms": 1000},
        {"started_at": "2026-01-01T00:01:30+00:00", "duration_ms": 4000},
        {"started_at": "not a time"},
    ]
    scale = {"firstStart": 1767225600000, "longest": 4000}

    assert _values(
        JsCall("stepScale", (calls,)),
        JsCall("stepScale", ([],)),
        JsCall("offsetText", (calls[0], scale)),
        JsCall("offsetText", (calls[1], scale)),
        JsCall("offsetText", (calls[2], scale)),
    ) == [scale, {"firstStart": None, "longest": 0}, "at start", "at +1 min 30 s", ""]


def test_steps_draw_a_line_with_one_outcome_dot_per_call() -> None:
    css = dashboard_assets.STYLE_CSS
    assert re.search(
        r"\.call-row > td:first-child::before\s*\{[^}]*background: var\(--border\)", css
    )
    for outcome, token in (("ok", "--ok"), ("error", "--error"), ("active", "--accent")):
        assert re.search(
            rf'\.call-row\[data-outcome="status-{outcome}"\][^{{]*\{{[^}}]*var\({token}\)', css
        )
    assert "row.dataset.outcome = callOutcome(call).className;" in function_source(
        _JS, "patchStepExtras"
    )
    assert "const scale = stepScale(calls);" in function_source(_JS, "renderTimeline")


def test_a_hidden_duration_bar_is_not_drawn() -> None:
    assert ".duration-bar[hidden] { display: none; }" in dashboard_assets.STYLE_CSS


def test_step_chips_do_not_replace_the_link_cell_chips() -> None:
    assert len(re.findall(r"function syncChips\(", _JS)) == 1
    assert "function syncStepChips(container, chips)" in _JS


# What each step did: files, checks and verdict joined from the attempt records.

_ATTEMPTS = [
    {"attempt_number": 1, "started_at": "2026-09-10T13:21:30.562Z"},
    {"attempt_number": 2, "started_at": "2026-09-10T13:37:13.336Z"},
]
_EVIDENCE = {
    "review": {"rounds": [{"attempt_number": 2, "approved": False, "skipped": False}]},
    "checks": {
        "verification_history": [
            {"attempt": 2, "checks": [{"passed": True}, {"passed": False}, {"passed": True}]}
        ],
        "tester_history": [{"attempt": 2, "passed": True, "skipped": False}],
    },
}


def _call(role: str, started_at: str, attempt: int | None = 1, **extra: Any) -> dict[str, Any]:
    return {"role": role, "started_at": started_at, "attempt_number": attempt, **extra}


def test_the_attempt_of_a_call_is_the_count_of_attempts_started_before_it() -> None:
    planner = _call("PLANNER", "2026-09-10T13:20:14Z")
    first = _call("IMPLEMENTER", "2026-09-10T13:21:30.562Z")
    reviewer = _call("REVIEWER", "2026-09-10T13:42:40Z", 2)
    triage = _call("TRIAGE", "2026-09-10T13:50:00Z", None)

    assert _values(
        *(JsCall("attemptIndex", (call, _ATTEMPTS)) for call in (planner, first, reviewer, triage))
    ) == [None, 1, 2, None]


def test_files_touched_are_the_written_paths_once_each_in_order() -> None:
    tools = [
        {"name": "Read", "preview": "a.py"},
        {"name": "Edit", "preview": "b.py"},
        {"name": "write", "preview": "c.py"},
        {"name": "MultiEdit", "preview": "b.py"},
        {"name": "Edit", "preview": ""},
        {"name": "Bash", "preview": "pytest -q"},
    ]

    assert _values(JsCall("filesTouched", (tools,)), JsCall("filesTouched", (None,))) == [
        ["b.py", "c.py"],
        [],
    ]


def test_the_verdict_comes_from_the_review_round_or_tester_report_of_the_attempt() -> None:
    reviewer = _call("REVIEWER", "2026-09-10T13:42:40Z", 2)
    tester = _call("TESTER", "2026-09-10T13:39:16Z", 2)
    approved = {"review": {"rounds": [{"attempt_number": 2, "approved": True}]}}
    skipped = {"checks": {"tester_history": [{"attempt": 2, "skipped": True}]}}

    assert _values(
        JsCall("stepVerdict", (reviewer, 2, _EVIDENCE)),
        JsCall("stepVerdict", (reviewer, 2, approved)),
        JsCall("stepVerdict", (reviewer, 1, _EVIDENCE)),
        JsCall("stepVerdict", (tester, 2, _EVIDENCE)),
        JsCall("stepVerdict", (tester, 2, skipped)),
        JsCall("stepVerdict", (_call("IMPLEMENTER", ""), 2, _EVIDENCE)),
        JsCall("stepVerdict", (reviewer, 2, {"review": None, "checks": None})),
    ) == ["changes requested", "approved", "", "tests passed", "tester skipped", "", ""]


def test_the_reduced_line_names_the_role_and_its_verdict() -> None:
    reviewer = _call("REVIEWER", "")

    assert _values(
        JsCall("stepSummary", (reviewer, "approved")),
        JsCall("stepSummary", (reviewer, "")),
    ) == ["Reviewer · approved", "Reviewer"]


def test_the_expanded_step_lists_files_checks_and_verdict() -> None:
    reviewer = _call(
        "REVIEWER", "2026-09-10T13:42:40Z", 2, tool_calls=[{"name": "Edit", "preview": "a.py"}]
    )

    assert _values(
        JsCall("stepFields", (reviewer, 2, _EVIDENCE)),
        JsCall("stepFields", (_call("TRIAGE", ""), None, _EVIDENCE)),
    ) == [
        [
            ["Files touched (1)", "a.py"],
            ["Checks of attempt 2", "2 of 3 passed"],
            ["Verdict", "changes requested"],
        ],
        [],
    ]


def test_the_steps_view_is_reduced_until_a_person_asks_for_all_detail() -> None:
    page = dashboard_assets.render_index_html(token="fixture-token")
    assert 'const STEPS_DETAIL_KEY = "factory-dashboard-steps-detail";' in _JS
    assert '=== "full"' in function_source(_JS, "readStepsDetail")
    assert "showStepsDetail(readStepsDetail())" in _JS
    assert 'id="steps-view-toggle" type="button" aria-pressed="false"' in page


def test_timing_shows_boot_first_event_and_total_when_the_runtime_measured_them() -> None:
    assert _values(
        JsCall("timingFields", (None, 5000)),
        JsCall("timingFields", ({}, 5000)),
        JsCall("timingFields", ({"process_boot_ms": 350.4, "first_event_ms": 2100.0}, 64000)),
        JsCall("timingFields", ({"first_event_ms": 900.0}, None)),
    ) == [
        [],
        [],
        [["Timing", "boot 350 ms · first event 2 s · total 1 min 4 s"]],
        [["Timing", "first event 900 ms"]],
    ]
    assert "...timingFields(call.performance, call.duration_ms)" in function_source(
        _JS, "callFields"
    )
