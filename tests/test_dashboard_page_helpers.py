"""Behavior of the dashboard page helpers, run in ``node`` (slice H of #88, ADR-016).

Each case calls one pure helper of ``app.js`` with JSON input and checks the
JSON it returns. Every case runs in one ``node`` process per test module, so
the suite pays for one start. The tests skip when ``node`` is not on ``PATH``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import NamedTuple

import pytest
from dashboard_js import JsCall, JsResult, find_node, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.security import TOKEN_QUERY_PARAM

NOT_REPORTED = "not reported"
DASH = "—"
PREMIUM = "total_premium_request_cost"
USAGE_USD = "usage_value_usd"
LIST_USD = "list_price_estimate_usd"
RUN_ID_MAX_LENGTH = 128
FILTER_NEEDS_YOU = "needs-you"
COMPARE = "#compare"


class Case(NamedTuple):
    case_id: str
    call: JsCall
    expected: object


def _case(function: str, case_id: str, expected: object, *args: object) -> Case:
    return Case(f"{function}-{case_id}", JsCall(function, args), expected)


def _paragraph(text: str) -> dict[str, str]:
    return {"tagName": "p", "className": "", "textContent": text}


_duration = partial(_case, "durationText")
_partial_note = partial(_case, "partialNote")
_cost = partial(_case, "costText")
_premium = partial(_case, "premiumRequestsPhrase")
_reopens = partial(_case, "reopensLine")

FORMATTER_CASES = [
    _duration("null", NOT_REPORTED, None),
    _duration("text", NOT_REPORTED, "5"),
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
    _duration("one-hour-stays-in-minutes", "60 min 0 s", 3_600_000),
    _duration("large", "2057 min 37 s", 123_456_789),
    _partial_note("reported-missing", "", None, 3),
    _partial_note("calls-missing", "", 3, None),
    _partial_note("reported-text", "", "2", 3),
    _partial_note("none-reported", "", 0, 3),
    _partial_note("all-reported", "", 3, 3),
    _partial_note("more-reported-than-calls", "", 4, 3),
    _partial_note("no-calls", "", 0, 0),
    _partial_note("one-of-three", "1 of 3 calls reported", 1, 3),
    _partial_note("two-of-three", "2 of 3 calls reported", 2, 3),
    _cost("empty", NOT_REPORTED, {}),
    _cost("null-units", NOT_REPORTED, {PREMIUM: None, USAGE_USD: None, LIST_USD: None}),
    _cost("text-unit", NOT_REPORTED, {PREMIUM: "3"}),
    _cost("one-premium-request", "1 premium request", {PREMIUM: 1}),
    _cost("zero-premium-requests", "0 premium requests", {PREMIUM: 0}),
    _cost("two-premium-requests", "2 premium requests", {PREMIUM: 2}),
    _cost("fractional-premium-requests", "1.5 premium requests", {PREMIUM: 1.5}),
    _cost("large-premium-requests", "1,234,567 premium requests", {PREMIUM: 1_234_567}),
    _cost("zero-usd", "0.00 USD AI usage", {USAGE_USD: 0}),
    _cost("usd-two-decimals", "0.50 USD AI usage", {USAGE_USD: 0.5}),
    _cost("usd-separator", "1,234.50 USD AI usage", {USAGE_USD: 1234.5}),
    _cost("usd-rounds-at-six-decimals", "0.123457 USD AI usage", {USAGE_USD: 0.1234567}),
    _cost("list-price", "12.00 USD list price", {LIST_USD: 12}),
    _cost(
        "all-units-in-order",
        "2 premium requests, 3.25 USD AI usage, 4.00 USD list price",
        {LIST_USD: 4, USAGE_USD: 3.25, PREMIUM: 2},
    ),
    _cost(
        "skips-the-unreported-unit",
        "2 premium requests, 4.00 USD list price",
        {PREMIUM: 2, USAGE_USD: None, LIST_USD: 4},
    ),
    _premium("one", "1 premium request", 1),
    _premium("zero", "0 premium requests", 0),
    _premium("two", "2 premium requests", 2),
    _premium("fraction", "0.5 premium requests", 0.5),
    _premium("thousands", "1,000 premium requests", 1000),
    _premium("millions", "1,000,000 premium requests", 1_000_000),
    _reopens(
        "used-of-max", _paragraph("Reopens used 1 of 3"), {"reopens_used": 1, "max_reopens": 3}
    ),
    _reopens("zero-used", _paragraph("Reopens used 0 of 3"), {"reopens_used": 0, "max_reopens": 3}),
    _reopens("zero-max", _paragraph("Reopens used 0 of 0"), {"reopens_used": 0, "max_reopens": 0}),
    _reopens("no-fields", None, {}),
    _reopens("used-missing", None, {"max_reopens": 3}),
    _reopens("max-missing", None, {"reopens_used": 1}),
    _reopens("used-null", None, {"reopens_used": None, "max_reopens": 3}),
    _reopens("max-text", None, {"reopens_used": 1, "max_reopens": "3"}),
]


def _run(run_id: str, *, waiting: object = False) -> dict[str, object]:
    return {"run_id": run_id, "waiting_for_human": waiting}


_RUNS = [_run("a"), _run("b", waiting=True), _run("c"), _run("d", waiting=True)]
_NO_FLAG = {"run_id": "a"}
_FULL_RUN = {
    "run_id": "r1",
    "created_at": "2026-10-01T10:00:00Z",
    "state": "DONE",
    "title": "Fix the bug",
    "performance_model_profile": "fast",
}
_OPTION_LABEL = "2026-10-01T10:00:00Z | DONE | Fix the bug"

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
    _order("empty", [], [], None),
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
    _hash("run-a-only", f"{COMPARE}/run-1", _pair("run-1", None)),
    _hash("both-runs", f"{COMPARE}/run-1/run-2", _pair("run-1", "run-2")),
    _hash("run-b-only-leaves-a-empty", f"{COMPARE}//run-2", _pair(None, "run-2")),
    _hash("encodes-reserved-characters", f"{COMPARE}/a%20b%2Fc/d%3Fe%23f", _pair("a b/c", "d?e#f")),
    _hash("encodes-non-ascii", f"{COMPARE}/%C3%A9", _pair("é", None)),
    _option(
        "all-parts",
        {"value": "r1", "label": f"{_OPTION_LABEL} | profile fast"},
        _FULL_RUN,
    ),
    _option(
        "no-profile",
        {"value": "r1", "label": _OPTION_LABEL},
        _without(_FULL_RUN, "performance_model_profile"),
    ),
    _option(
        "empty-profile",
        {"value": "r1", "label": _OPTION_LABEL},
        {**_FULL_RUN, "performance_model_profile": ""},
    ),
    _option(
        "no-title-names-the-run",
        {"value": "r1", "label": "2026-10-01T10:00:00Z | DONE | r1 | profile fast"},
        _without(_FULL_RUN, "title"),
    ),
    _option(
        "no-start-or-state",
        {"value": "r1", "label": f"{DASH} | {DASH} | Fix the bug"},
        {"run_id": "r1", "title": "Fix the bug"},
    ),
    _option(
        "id-field-when-no-run-id",
        {"value": "r2", "label": f"{DASH} | {DASH} | r2"},
        {"id": "r2"},
    ),
    _option(
        "run-id-wins-over-id",
        {"value": "r1", "label": f"{DASH} | {DASH} | r1"},
        {"run_id": "r1", "id": "r2"},
    ),
    _option("no-id", {"label": f"{DASH} | {DASH} | {DASH}"}, {}),
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
    if find_node() is None:
        pytest.skip("node is not on PATH")
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
    assert results(case.case_id).history == case.history
