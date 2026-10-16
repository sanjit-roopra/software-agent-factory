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
        JsCall("fallbackFields", ("",)),
        JsCall("fallbackFields", ("usage limit",)),
    ) == [
        [],
        [["Tool calls (2)", "Bash  pytest -q\nTodoWrite"]],
        [],
        [["Why it fell back", "usage limit"]],
    ]
    fields = function_source(_JS, "callFields")
    assert '["Runtime", displayValue(call.runtime, NOT_REPORTED)]' in fields
    assert "...toolCallFields(call.tool_calls)" in fields


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
