"""Behavior of the dashboard page helpers, run in ``node`` (slice H of #88, ADR-016).

Each case calls one pure helper of ``app.js`` with JSON input and checks the
JSON it returns. Every case runs in one ``node`` process per test module, so
the suite pays for one start. Without ``node`` on ``PATH`` the tests skip on a
developer machine and fail in CI.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import NamedTuple

import pytest
from dashboard_js import UNDEFINED, JsCall, JsResult, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.security import TOKEN_QUERY_PARAM

NOT_REPORTED = "not reported"
DASH = "—"
PREMIUM = "total_premium_request_cost"
USAGE_USD = "usage_value_usd"
LIST_USD = "list_price_estimate_usd"
RUN_ID_MAX_LENGTH = 128
RUN_ID = "run_id"
TITLE = "title"
PROFILE = "performance_model_profile"
REOPENS_USED = "reopens_used"
MAX_REOPENS = "max_reopens"
VALUE = "value"
LABEL = "label"
STARTED_AT = "2026-10-01T10:00:00Z"
BUG_TITLE = "Fix the bug"
TWO_PREMIUM_REQUESTS = "2 premium requests"
FILTER_NEEDS_YOU = "needs-you"
COMPARE = "#compare"
CALLS = "calls"
DURATION_MS = "duration_ms"
RUN_1 = "run-1"
STATE_FAILED = "FAILED"
REASON = "reason"
TOTAL = "total"
REPORTED_COUNT = "reported_count"
KIND_FAILED = "failed"
LABEL_FAILED = "Failed"
DETAIL = "detail"
CLASS_NAME = "className"
EMPTY = "empty"
LIST_PRICE_TEXT = "$0.039"
NEUTRAL = "neutral"
SOL = "gpt-6.1-sol"
REJECTED = "rejected"
PREMIUM_REQUEST_COST = "premium_request_cost"
TOTAL_LABEL = "Total"
DURATION_TEXT = "1 min 3 s"
HIDDEN = "hidden"
STATUS = "status"
SUCCESS = "SUCCESS"
FAILURE_LINK = "failure_link"
OVERVIEW_LIST_PRICE = "list_price_usd"
OVERVIEW_PREMIUM = "premium_requests"
SCAN_TRUNCATED = "scan_truncated"
SCAN_NOTE = "The figures cover only the newest runs the dashboard scanned."
INFINITY_ID = "infinity"
MISSING_ID = "missing"
NAN = math.nan
INF = math.inf


class Case(NamedTuple):
    case_id: str
    call: JsCall
    expected: object


def _case(function: str, case_id: str, expected: object, *args: object) -> Case:
    return Case(f"{function}-{case_id}", JsCall(function, args), expected)


_TOTALS = {
    CALLS: 6,
    "failed_calls": {TOTAL: 0, REPORTED_COUNT: 6},
    DURATION_MS: {TOTAL: 63_000, REPORTED_COUNT: 6},
    "total_tokens": {TOTAL: 40_990, REPORTED_COUNT: 6},
    "costs": {LIST_USD: {TOTAL: 0.039188, REPORTED_COUNT: 6}},
}
_FAILED_RUN = {
    RUN_ID: RUN_1,
    TITLE: BUG_TITLE,
    "state": STATE_FAILED,
    "outcome": {"kind": KIND_FAILED, LABEL: LABEL_FAILED},
    "why": REASON,
    "models": {"text": "gpt-5-mini \u00d74 \u00b7 impl gpt-6.1-sol", DETAIL: DETAIL},
    "invocation_count": 6,
    DURATION_MS: 63_000,
    "usage": {LIST_USD: 0.039188},
    "created_at": "2026-10-02T11:48:00Z",
}
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC).timestamp() * 1000


def _minutes_before(minutes: float) -> str:
    moment = datetime.fromtimestamp(_NOW / 1000, UTC) - timedelta(minutes=minutes)
    return moment.isoformat().replace("+00:00", "Z")


def _paragraph(text: str) -> dict[str, str]:
    return {"tagName": "p", CLASS_NAME: "", "textContent": text}


_duration = partial(_case, "durationText")
_partial_note = partial(_case, "partialNote")
_cost = partial(_case, "costText")
_premium = partial(_case, "premiumRequestsPhrase")
_money = partial(_case, "moneyText")
_relative = partial(_case, "relativeTimeText")
_absolute = partial(_case, "absoluteTimeText")
_list_cost = partial(_case, "listCostText")
_note = partial(_case, "last24HoursNote")
_blank = partial(_case, "isBlankField")
_badge = partial(_case, "badgeKind")
_model = partial(_case, "modelText")
_row = partial(_case, "runRowSpec")
_title_cell = partial(_case, "titleCell")
_key_numbers = partial(_case, "keyNumberCards")
_total_cells = partial(_case, "totalCells")
_call_outcome = partial(_case, "callOutcome")
_reopens = partial(_case, "reopensLine")
_has_list_price = partial(_case, "hasListPrice")
_has_premium = partial(_case, "hasPremiumRequests")
_has_no_cost = partial(_case, "hasNoCost")
_scan_note = partial(_case, "scanTruncatedNote")

# A number that is not finite is not a reported number. Every formatter below keeps it out
# through ``isFiniteNumber``, so NaN, Infinity and a missing value read as "not reported".
FORMATTER_CASES = [
    _duration("null", NOT_REPORTED, None),
    _duration("text", NOT_REPORTED, "5"),
    _duration("undefined", NOT_REPORTED, UNDEFINED),
    _duration("nan", NOT_REPORTED, NAN),
    _duration(INFINITY_ID, NOT_REPORTED, INF),
    _duration("negative-infinity", NOT_REPORTED, -INF),
    _duration("zero", "0 ms", 0),
    _duration("fraction", "0.5 ms", 0.5),
    _duration("last-millisecond", "999 ms", 999),
    _duration("one-second", "1 s", 1000),
    _duration("rounds-down", "1 s", 1499),
    _duration("rounds-up", "2 s", 1500),
    _duration("last-second", "59 s", 59499),
    _duration("rounds-to-a-minute", "1 min 0 s", 59500),
    _duration("one-minute", "1 min 0 s", 60000),
    _duration("minute-and-second", "1 min 1 s", 61000),
    _duration("last-second-of-an-hour", "59 min 59 s", 3_599_000),
    _duration("one-hour", "1 h 0 min", 3_600_000),
    _duration("hour-and-minutes", "1 h 5 min", 3_900_000),
    _duration("large", "34 h 17 min", 123_456_789),
    _partial_note("reported-missing", "", None, 3),
    _partial_note("calls-missing", "", 3, None),
    _partial_note("reported-undefined", "", UNDEFINED, 3),
    _partial_note("calls-undefined", "", 3, UNDEFINED),
    _partial_note("reported-nan", "", NAN, 3),
    _partial_note("calls-nan", "", 1, NAN),
    _partial_note("reported-infinity", "", INF, 3),
    _partial_note("calls-infinity", "", 1, INF),
    _partial_note("reported-text", "", "2", 3),
    _partial_note("none-reported", "", 0, 3),
    _partial_note("all-reported", "", 3, 3),
    _partial_note("more-reported-than-calls", "", 4, 3),
    _partial_note("no-calls", "", 0, 0),
    _partial_note("one-of-three", "1 of 3 calls reported", 1, 3),
    _partial_note("two-of-three", "2 of 3 calls reported", 2, 3),
    _cost(EMPTY, NOT_REPORTED, {}),
    _cost("null-units", NOT_REPORTED, {PREMIUM: None, USAGE_USD: None, LIST_USD: None}),
    _cost("undefined-unit", NOT_REPORTED, {PREMIUM: UNDEFINED}),
    _cost("nan-unit", NOT_REPORTED, {PREMIUM: NAN}),
    _cost("infinite-usd", NOT_REPORTED, {USAGE_USD: INF}),
    _cost("negative-infinite-list-price", NOT_REPORTED, {LIST_USD: -INF}),
    _cost("skips-the-non-finite-unit", TWO_PREMIUM_REQUESTS, {PREMIUM: 2, LIST_USD: NAN}),
    _cost("text-unit", NOT_REPORTED, {PREMIUM: "3"}),
    _cost("one-premium-request", "1 premium request", {PREMIUM: 1}),
    _cost("zero-premium-requests", "0 premium requests", {PREMIUM: 0}),
    _cost("two-premium-requests", TWO_PREMIUM_REQUESTS, {PREMIUM: 2}),
    _cost("fractional-premium-requests", "1.5 premium requests", {PREMIUM: 1.5}),
    _cost("large-premium-requests", "1,234,567 premium requests", {PREMIUM: 1_234_567}),
    _cost("zero-usd", "$0 AI usage", {USAGE_USD: 0}),
    _cost("usd-three-decimals-under-a-dollar", "$0.500 AI usage", {USAGE_USD: 0.5}),
    _cost("usd-separator", "$1,234.50 AI usage", {USAGE_USD: 1234.5}),
    _cost("usd-rounds-at-three-decimals", "$0.123 AI usage", {USAGE_USD: 0.1234567}),
    _cost("list-price-has-no-unit-suffix", "$12.00", {LIST_USD: 12}),
    _cost("list-price-under-a-dollar", LIST_PRICE_TEXT, {LIST_USD: 0.039188}),
    _cost(
        "all-units-in-order",
        "2 premium requests, $3.25 AI usage, $4.00",
        {LIST_USD: 4, USAGE_USD: 3.25, PREMIUM: 2},
    ),
    _cost(
        "skips-the-unreported-unit",
        "2 premium requests, $4.00",
        {PREMIUM: 2, USAGE_USD: None, LIST_USD: 4},
    ),
    _money("null", NOT_REPORTED, None),
    _money("text", NOT_REPORTED, "1"),
    _money("nan", NOT_REPORTED, NAN),
    _money(INFINITY_ID, NOT_REPORTED, INF),
    _money("negative", NOT_REPORTED, -0.01),
    _money("zero", "$0", 0),
    _money("too-small-for-three-decimals", "<$0.001", 0.0004),
    _money("smallest-shown", "$0.001", 0.0005),
    _money("three-decimals", LIST_PRICE_TEXT, 0.039188),
    _money("keeps-trailing-zeros", "$0.500", 0.5),
    _money("just-under-a-dollar", "$0.999", 0.9994),
    _money("rounds-up-to-a-dollar-with-two-decimals", "$1.00", 0.9996),
    _money("one-dollar", "$1.00", 1),
    _money("two-decimals-from-a-dollar", "$12.35", 12.345),
    _money("separator", "$1,234.50", 1234.5),
    _relative("minutes-ago", "12 min ago", _minutes_before(12), _NOW),
    _relative("under-a-minute", "just now", _minutes_before(0.5), _NOW),
    _relative("in-the-future", "just now", _minutes_before(-5), _NOW),
    _relative("last-minute-of-an-hour", "59 min ago", _minutes_before(59.9), _NOW),
    _relative("exactly-one-minute", "1 min ago", _minutes_before(1), _NOW),
    _relative("one-hour", "1 h ago", _minutes_before(60), _NOW),
    _relative("last-hour-of-a-day", "23 h ago", _minutes_before(24 * 60 - 1), _NOW),
    _relative("days", "3 d ago", _minutes_before(3 * 24 * 60), _NOW),
    _relative(
        "a-month-or-more-shows-the-date", "2026-09-02 12:00 UTC", _minutes_before(30 * 1440), _NOW
    ),
    _relative("microseconds-parse", "52 min ago", "2026-10-02T11:07:17.854307Z", _NOW),
    _relative("no-time", DASH, None, _NOW),
    _relative("text-that-is-not-a-time", DASH, "soon", _NOW),
    _relative("no-now", DASH, _minutes_before(5), None),
    _absolute("utc-minute", "2026-10-02 11:22 UTC", "2026-10-02T11:22:17.854307Z"),
    _absolute("offset-is-converted", "2026-10-02 10:22 UTC", "2026-10-02T12:22:00+02:00"),
    _absolute("fallback", DASH, None),
    _absolute("custom-fallback", "", "nope", ""),
    _list_cost("nothing", DASH, {}),
    _list_cost("no-usage", DASH, None),
    _list_cost("list-price", LIST_PRICE_TEXT, {LIST_USD: 0.039188}),
    _list_cost("premium", "2 premium req.", {PREMIUM_REQUEST_COST: 2}),
    _list_cost(
        "both-units-stay-apart",
        "$0.039 \u00b7 2 premium req.",
        {LIST_USD: 0.039188, PREMIUM_REQUEST_COST: 2},
    ),
    _list_cost("reported-zero", "$0 \u00b7 0 premium req.", {LIST_USD: 0, PREMIUM_REQUEST_COST: 0}),
    _list_cost("not-numbers", DASH, {LIST_USD: "1", PREMIUM_REQUEST_COST: NAN}),
    _note("same-as-total", "", 3, 3),
    _note("less-than-total", "1 in the last 24 hours", 1, 3),
    _note("none", "", None, 3),
    _note("zero-of-some", "0 in the last 24 hours", 0, 3),
    _blank("undefined", True, ["x", UNDEFINED]),
    _blank("null", True, ["x", None]),
    _blank(EMPTY, True, ["x", ""]),
    _blank("dash", True, ["x", DASH]),
    _blank("not-reported", True, ["x", NOT_REPORTED]),
    _blank("false-is-a-value", False, ["x", False]),
    _blank("zero-is-a-value", False, ["x", 0]),
    _blank("text", False, ["x", VALUE]),
    _badge("known", KIND_FAILED, {"kind": KIND_FAILED, LABEL: LABEL_FAILED}),
    _badge("needs-you", "needs_you", {"kind": "needs_you"}),
    _badge("unknown-kind", NEUTRAL, {"kind": "paused"}),
    _badge("no-outcome", NEUTRAL, None),
    _key_numbers(
        "duration-calls-tokens-and-the-reported-cost",
        [
            {LABEL: "Duration", VALUE: DURATION_TEXT, "note": "", "help": ""},
            {LABEL: "Calls", VALUE: "6", "note": "", "help": ""},
            {LABEL: "Tokens", VALUE: "40,990", "note": "", "help": ""},
            {
                LABEL: "List-price estimate (USD)",
                VALUE: LIST_PRICE_TEXT,
                "note": "",
                "help": "Estimate in USD from list prices, not what a provider billed.",
            },
        ],
        _TOTALS,
    ),
    _key_numbers(
        "one-card-says-no-cost-was-reported",
        [
            {LABEL: "Duration", VALUE: NOT_REPORTED, "note": "", "help": ""},
            {LABEL: "Calls", VALUE: "0", "note": "", "help": ""},
            {LABEL: "Tokens", VALUE: NOT_REPORTED, "note": "", "help": ""},
            {LABEL: "Cost", VALUE: NOT_REPORTED, "note": "", "help": ""},
        ],
        {CALLS: 0},
    ),
    _total_cells(
        "the-total-row",
        [TOTAL_LABEL, "6 calls", DURATION_TEXT, "40,990", LIST_PRICE_TEXT],
        _TOTALS,
    ),
    _total_cells(
        "the-total-row-keeps-each-cost-unit",
        [TOTAL_LABEL, "1 call", "1 s", "5", "2 premium requests, $0.500 AI usage, $0.039"],
        {
            CALLS: 1,
            DURATION_MS: {TOTAL: 1000},
            "total_tokens": {TOTAL: 5},
            "costs": {
                PREMIUM: {TOTAL: 2},
                USAGE_USD: {TOTAL: 0.5},
                LIST_USD: {TOTAL: 0.039188},
            },
        },
    ),
    _total_cells(
        "an-empty-total-row",
        [TOTAL_LABEL, NOT_REPORTED, NOT_REPORTED, NOT_REPORTED, NOT_REPORTED],
        {},
    ),
    _row(
        "a-failed-run",
        {
            "runId": RUN_1,
            "cells": [
                {
                    "href": "#run/run-1",
                    VALUE: BUG_TITLE,
                    HIDDEN: "",
                    TITLE: RUN_1,
                },
                {"badge": KIND_FAILED, VALUE: LABEL_FAILED, TITLE: STATE_FAILED},
                {VALUE: REASON, "clamp": True, CLASS_NAME: "why-cell", TITLE: REASON},
                {
                    VALUE: "gpt-5-mini \u00d74 \u00b7 impl gpt-6.1-sol",
                    "chunks": ["gpt-5-mini \u00d74", "impl gpt-6.1-sol"],
                    CLASS_NAME: "models-cell",
                    TITLE: DETAIL,
                },
                6,
                DURATION_TEXT,
                LIST_PRICE_TEXT,
                {VALUE: "12 min ago", TITLE: "2026-10-02 11:48 UTC"},
                {
                    VALUE: "\u21c4",
                    "icon": True,
                    "href": "#compare/run-1",
                    HIDDEN: "Compare with another run: run-1",
                    TITLE: "Compare this run with another run",
                },
            ],
        },
        _FAILED_RUN,
        _NOW,
    ),
    _row(
        "a-run-with-nothing-to-show",
        {
            "runId": UNDEFINED,
            "cells": [
                {VALUE: DASH},
                {"badge": NEUTRAL, VALUE: DASH, TITLE: ""},
                {VALUE: DASH, "clamp": True, CLASS_NAME: "why-cell", TITLE: ""},
                {VALUE: DASH, "chunks": [], CLASS_NAME: "models-cell", TITLE: ""},
                UNDEFINED,
                DASH,
                DASH,
                {VALUE: DASH, TITLE: ""},
                {VALUE: "", CLASS_NAME: ""},
            ],
        },
        {},
        _NOW,
    ),
    _title_cell(
        "no-title-links-by-the-run-id-and-encodes-it",
        {"href": "#run/r%201", VALUE: "r 1", HIDDEN: "", TITLE: "r 1"},
        {RUN_ID: "r 1"},
        "r 1",
    ),
    _title_cell("no-run-id-is-plain-text", {VALUE: BUG_TITLE}, {TITLE: BUG_TITLE}, UNDEFINED),
    _model("with-reasoning", "gpt-6.1-sol (high)", {"model": SOL, "reasoning": "high"}),
    _model("without-reasoning", SOL, {"model": SOL}),
    _model("without-a-model", DASH, {}),
    _call_outcome("success", {"text": "success", CLASS_NAME: "status-ok"}, {STATUS: SUCCESS}),
    _call_outcome(
        REJECTED,
        {"text": REJECTED, CLASS_NAME: "status-error"},
        {STATUS: SUCCESS, FAILURE_LINK: REJECTED},
    ),
    _call_outcome(
        "run-failed-after",
        {"text": "success, run failed after", CLASS_NAME: "status-warn"},
        {STATUS: SUCCESS, FAILURE_LINK: "last_call"},
    ),
    _call_outcome(
        "unknown-link-falls-back-to-the-status",
        {"text": KIND_FAILED, CLASS_NAME: "status-error"},
        {STATUS: STATE_FAILED, FAILURE_LINK: "other"},
    ),
    _call_outcome(
        "no-link-no-status", {"text": NOT_REPORTED, CLASS_NAME: ""}, {FAILURE_LINK: None}
    ),
    _premium("one", "1 premium request", 1),
    _premium("zero", "0 premium requests", 0),
    _premium("two", TWO_PREMIUM_REQUESTS, 2),
    _premium("fraction", "0.5 premium requests", 0.5),
    _premium("thousands", "1,000 premium requests", 1000),
    _premium("millions", "1,000,000 premium requests", 1_000_000),
    _reopens("used-of-max", _paragraph("Reopens used 1 of 3"), {REOPENS_USED: 1, MAX_REOPENS: 3}),
    _reopens("zero-used", _paragraph("Reopens used 0 of 3"), {REOPENS_USED: 0, MAX_REOPENS: 3}),
    _reopens("zero-max", _paragraph("Reopens used 0 of 0"), {REOPENS_USED: 0, MAX_REOPENS: 0}),
    # ``null`` (None), never ``undefined`` (UNDEFINED): the line is absent, not unset.
    _reopens("no-fields", None, {}),
    _reopens("used-nan", None, {REOPENS_USED: NAN, MAX_REOPENS: 3}),
    _reopens("used-undefined", None, {REOPENS_USED: UNDEFINED, MAX_REOPENS: 3}),
    _reopens("max-infinity", None, {REOPENS_USED: 1, MAX_REOPENS: INF}),
    _reopens("max-nan", None, {REOPENS_USED: 1, MAX_REOPENS: NAN}),
    _reopens("used-missing", None, {MAX_REOPENS: 3}),
    _reopens("max-missing", None, {REOPENS_USED: 1}),
    _reopens("used-null", None, {REOPENS_USED: None, MAX_REOPENS: 3}),
    _reopens("max-text", None, {REOPENS_USED: 1, MAX_REOPENS: "3"}),
    _has_list_price("finite", True, {OVERVIEW_LIST_PRICE: 0.5}),
    _has_list_price("zero-is-reported", True, {OVERVIEW_LIST_PRICE: 0}),
    _has_list_price("null", False, {OVERVIEW_LIST_PRICE: None}),
    _has_list_price("nan", False, {OVERVIEW_LIST_PRICE: NAN}),
    _has_list_price(INFINITY_ID, False, {OVERVIEW_LIST_PRICE: INF}),
    _has_list_price("text", False, {OVERVIEW_LIST_PRICE: "0.5"}),
    _has_list_price(MISSING_ID, False, {}),
    _has_premium("finite", True, {OVERVIEW_PREMIUM: 2}),
    _has_premium("zero-is-reported", True, {OVERVIEW_PREMIUM: 0}),
    _has_premium("null", False, {OVERVIEW_PREMIUM: None}),
    _has_premium("nan", False, {OVERVIEW_PREMIUM: NAN}),
    _has_premium(INFINITY_ID, False, {OVERVIEW_PREMIUM: INF}),
    _has_premium("text", False, {OVERVIEW_PREMIUM: "2"}),
    _has_premium(MISSING_ID, False, {}),
    _has_no_cost("nothing-reported", True, {}),
    _has_no_cost("nothing-finite", True, {OVERVIEW_LIST_PRICE: NAN, OVERVIEW_PREMIUM: "2"}),
    _has_no_cost("list-price-only", False, {OVERVIEW_LIST_PRICE: 0.5}),
    _has_no_cost("premium-requests-only", False, {OVERVIEW_PREMIUM: 2}),
    _has_no_cost("a-reported-zero-is-a-cost", False, {OVERVIEW_LIST_PRICE: 0}),
    _has_no_cost("both-units", False, {OVERVIEW_LIST_PRICE: 0.5, OVERVIEW_PREMIUM: 2}),
    _scan_note("cut", SCAN_NOTE, {SCAN_TRUNCATED: True}),
    _scan_note("complete", "", {SCAN_TRUNCATED: False}),
    _scan_note("text-is-not-true", "", {SCAN_TRUNCATED: "true"}),
    _scan_note("number-is-not-true", "", {SCAN_TRUNCATED: 1}),
    _scan_note(MISSING_ID, "", {}),
]


def _run(run_id: str, *, waiting: object = False) -> dict[str, object]:
    return {RUN_ID: run_id, "waiting_for_human": waiting}


_RUNS = [_run("a"), _run("b", waiting=True), _run("c"), _run("d", waiting=True)]
_NO_FLAG = {RUN_ID: "a"}
_FULL_RUN = {
    RUN_ID: "r1",
    "created_at": STARTED_AT,
    "state": "DONE",
    TITLE: BUG_TITLE,
    PROFILE: "fast",
}
_OPTION_LABEL = f"{STARTED_AT} | DONE | {BUG_TITLE}"

_order = partial(_case, "orderRuns")
_hash = partial(_case, "compareHash")
_option = partial(_case, "runOption")
_normalize = partial(_case, "normalizeSelection")
_selection = partial(_case, "compareSelection")


def _pair(a: object, b: object) -> dict[str, object]:
    return {"a": a, "b": b}


def _without(run: Mapping[str, object], key: str) -> dict[str, object]:
    return {name: value for name, value in run.items() if name != key}


LONGEST_ID = "x" * RUN_ID_MAX_LENGTH

LIST_AND_COMPARE_CASES = [
    _order(EMPTY, [], [], None),
    _order("empty-filtered", [], [], FILTER_NEEDS_YOU),
    _order("needs-you-first-in-list-order", [_RUNS[1], _RUNS[3], _RUNS[0], _RUNS[2]], _RUNS, None),
    _order("filter-keeps-only-needs-you", [_RUNS[1], _RUNS[3]], _RUNS, FILTER_NEEDS_YOU),
    _order("filter-with-none-waiting", [], [_RUNS[0], _RUNS[2]], FILTER_NEEDS_YOU),
    _order(
        "only-true-needs-you",
        [_run("y", waiting=True), _run("x", waiting="true"), _run("z", waiting=1)],
        [_run("x", waiting="true"), _run("y", waiting=True), _run("z", waiting=1)],
        None,
    ),
    _order("flag-missing", [_NO_FLAG], [_NO_FLAG], None),
    _hash("nothing-picked", COMPARE, _pair(None, None)),
    _hash("run-a-only", f"{COMPARE}/run-1", _pair(RUN_1, None)),
    _hash("both-runs", f"{COMPARE}/run-1/run-2", _pair(RUN_1, "run-2")),
    _hash("run-b-only-leaves-a-empty", f"{COMPARE}//run-2", _pair(None, "run-2")),
    _hash("encodes-reserved-characters", f"{COMPARE}/a%20b%2Fc/d%3Fe%23f", _pair("a b/c", "d?e#f")),
    _hash("encodes-non-ascii", f"{COMPARE}/%C3%A9", _pair("é", None)),
    _option(
        "all-parts",
        {VALUE: "r1", LABEL: f"{_OPTION_LABEL} | profile fast"},
        _FULL_RUN,
    ),
    _option(
        "no-profile",
        {VALUE: "r1", LABEL: _OPTION_LABEL},
        _without(_FULL_RUN, PROFILE),
    ),
    _option(
        "empty-profile",
        {VALUE: "r1", LABEL: _OPTION_LABEL},
        {**_FULL_RUN, PROFILE: ""},
    ),
    _option(
        "no-title-names-the-run",
        {VALUE: "r1", LABEL: f"{STARTED_AT} | DONE | r1 | profile fast"},
        _without(_FULL_RUN, TITLE),
    ),
    _option(
        "no-start-or-state",
        {VALUE: "r1", LABEL: f"{DASH} | {DASH} | {BUG_TITLE}"},
        {RUN_ID: "r1", TITLE: BUG_TITLE},
    ),
    _option(
        "id-field-when-no-run-id",
        {VALUE: "r2", LABEL: f"{DASH} | {DASH} | r2"},
        {"id": "r2"},
    ),
    _option(
        "run-id-wins-over-id",
        {VALUE: "r1", LABEL: f"{DASH} | {DASH} | r1"},
        {RUN_ID: "r1", "id": "r2"},
    ),
    # No ``run_id`` and no ``id`` leaves the value ``undefined``; the picker filters on that.
    _option("no-id", {VALUE: UNDEFINED, LABEL: f"{DASH} | {DASH} | {DASH}"}, {}),
    _normalize("two-runs", _pair("r1", "r2"), "r1", "r2"),
    _normalize("b-equal-to-a-is-cleared", _pair("r1", None), "r1", "r1"),
    _normalize("no-b", _pair("r1", None), "r1", None),
    _normalize("no-a", _pair(None, "r2"), None, "r2"),
    _normalize("nothing", _pair(None, None), None, None),
    _selection("valid-ids", _pair("r1", "r_2-x"), _pair("r1", "r_2-x")),
    _selection("a-with-a-space", _pair(None, "r2"), _pair("bad id", "r2")),
    _selection("b-with-a-slash", _pair("r1", None), _pair("r1", "r/2")),
    _selection("b-equal-to-a", _pair("r1", None), _pair("r1", "r1")),
    _selection("same-invalid-id-on-both-sides", _pair(None, None), _pair("!", "!")),
    _selection("empty-ids", _pair(None, None), _pair("", "")),
    _selection("longest-id", _pair(LONGEST_ID, None), _pair(LONGEST_ID, None)),
    _selection("id-too-long", _pair(None, None), _pair(LONGEST_ID + "x", None)),
    _selection("ids-that-are-not-text", _pair(None, None), _pair(5, ["r1"])),
    _selection("no-ids", _pair(None, None), {}),
]

ALL_CASES = FORMATTER_CASES + LIST_AND_COMPARE_CASES


class AddressCase(NamedTuple):
    case_id: str
    location: dict[str, str]
    history: list[list[object]]


_PAGE_HASH = "#compare/r1"
_CLEANED_URL = "/dashboard" + _PAGE_HASH


def _address(case_id: str, search: str, *, cleaned: bool) -> AddressCase:
    history: list[list[object]] = [[None, "", _CLEANED_URL]] if cleaned else []
    location = {"search": search, "pathname": "/dashboard", "hash": _PAGE_HASH}
    return AddressCase(f"stripTokenFromAddress-{case_id}", location, history)


ADDRESS_CASES = [
    _address("token-only", f"?{TOKEN_QUERY_PARAM}=secret", cleaned=True),
    _address("token-after-another-field", f"?other=1&{TOKEN_QUERY_PARAM}=secret", cleaned=True),
    _address("token-without-a-value", f"?{TOKEN_QUERY_PARAM}", cleaned=True),
    _address("no-query", "", cleaned=False),
    _address("other-field", "?other=1", cleaned=False),
    _address("field-that-ends-in-token", f"?my{TOKEN_QUERY_PARAM}=1", cleaned=False),
]


@pytest.fixture(scope="module")
def results() -> Callable[[str], JsResult]:
    """Run every case in one ``node`` process and look the results up by case id."""
    calls = [case.call for case in ALL_CASES] + [
        JsCall("stripTokenFromAddress", (), case.location) for case in ADDRESS_CASES
    ]
    ids = [case.case_id for case in ALL_CASES] + [case.case_id for case in ADDRESS_CASES]
    outcomes = dict(zip(ids, run_functions(dashboard_assets.APP_JS, calls), strict=True))
    return outcomes.__getitem__


def test_case_ids_are_unique() -> None:
    ids = [case.case_id for case in ALL_CASES] + [case.case_id for case in ADDRESS_CASES]

    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("case", FORMATTER_CASES, ids=lambda case: case.case_id)
def test_a_run_detail_formatter_returns(case: Case, results: Callable[[str], JsResult]) -> None:
    assert results(case.case_id).value == case.expected


@pytest.mark.parametrize("case", LIST_AND_COMPARE_CASES, ids=lambda case: case.case_id)
def test_a_list_or_compare_helper_returns(case: Case, results: Callable[[str], JsResult]) -> None:
    assert results(case.case_id).value == case.expected


@pytest.mark.parametrize("case", ADDRESS_CASES, ids=lambda case: case.case_id)
def test_the_token_query_is_dropped_from_the_address_and_the_hash_stays(
    case: AddressCase, results: Callable[[str], JsResult]
) -> None:
    result = results(case.case_id)

    assert result.error is None
    assert result.history == case.history
