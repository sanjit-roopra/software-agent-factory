"""When the Compare view fetches, run in ``node`` (ADR-074).

The picker list loads once per route entry, search and page, never on a plain
poll. The comparison polls only until both picked runs have finished.
"""

from __future__ import annotations

import re

import pytest
from dashboard_js import JsCall, function_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.lifecycle import TERMINAL_STATES

_JS = dashboard_assets.APP_JS
_PAIR = {"a": "r1", "b": "r2"}
_FIRST_PAGE = {"q": "", "offset": 0}
_FIRST_PATH = "/api/runs?limit=100&offset=0"
_NOTHING_LOADED = {"list": None, "settled": None}


def _call(function: str, *args: object) -> object:
    return run_functions(_JS, [JsCall(function, args)])[0].value


@pytest.mark.parametrize(
    ("list_", "expected"),
    [
        (_FIRST_PAGE, _FIRST_PATH),
        ({"q": "fix bug", "offset": 100}, "/api/runs?limit=100&offset=100&q=fix+bug"),
        ({"q": "a&b=c", "offset": 0}, "/api/runs?limit=100&offset=0&q=a%26b%3Dc"),
    ],
)
def test_the_picker_asks_the_server_for_one_page_of_the_search(
    list_: dict[str, object], expected: str
) -> None:
    assert _call("compareListPath", list_) == expected


@pytest.mark.parametrize(
    ("list_", "selection", "loaded", "expected"),
    [
        # A route entry has loaded nothing, so the list loads.
        (_FIRST_PAGE, {"a": None, "b": None}, _NOTHING_LOADED, {"list": True, "comparison": False}),
        # A poll with the same search and page loads nothing new.
        (
            _FIRST_PAGE,
            {"a": "r1", "b": None},
            {"list": _FIRST_PATH, "settled": None},
            {"list": False, "comparison": False},
        ),
        # Typing or paging changes the path, so the list loads again.
        (
            {"q": "older", "offset": 100},
            {"a": None, "b": None},
            {"list": _FIRST_PATH, "settled": None},
            {"list": True, "comparison": False},
        ),
        # A pair with a run still going keeps polling.
        (
            _FIRST_PAGE,
            _PAIR,
            {"list": _FIRST_PATH, "settled": None},
            {"list": False, "comparison": True},
        ),
        # A pair whose runs have both finished stops.
        (
            _FIRST_PAGE,
            _PAIR,
            {"list": _FIRST_PATH, "settled": "r1/r2"},
            {"list": False, "comparison": False},
        ),
        # A new pair loads even when another pair had settled.
        (
            _FIRST_PAGE,
            {"a": "r1", "b": "r3"},
            {"list": _FIRST_PATH, "settled": "r1/r2"},
            {"list": False, "comparison": True},
        ),
    ],
)
def test_a_compare_refresh_fetches_only_what_may_have_changed(
    list_: dict[str, object],
    selection: dict[str, object],
    loaded: dict[str, object],
    expected: dict[str, bool],
) -> None:
    assert _call("compareDue", list_, selection, loaded) == expected


@pytest.mark.parametrize(
    ("a_state", "b_state", "expected"),
    [
        ("DONE", "FAILED", "r1/r2"),
        ("NEEDS_HUMAN", "DONE", "r1/r2"),
        ("DONE", "IMPLEMENTING", None),
        ("CI_RUNNING", "DONE", None),
        (None, "DONE", None),
    ],
)
def test_a_comparison_settles_once_both_runs_have_finished(
    a_state: str | None, b_state: str | None, expected: str | None
) -> None:
    payload = {"a": {"state": a_state}, "b": {"state": b_state}}
    assert _call("settledKey", payload, _PAIR) == expected


def test_the_finished_states_match_the_terminal_states_of_the_lifecycle() -> None:
    match = re.search(r"const FINISHED_STATES = new Set\(\[([^\]]*)\]\)", _JS)
    assert match is not None
    assert set(re.findall(r'"([A-Z_]+)"', match.group(1))) == {str(s) for s in TERMINAL_STATES}


def test_the_compare_search_waits_for_a_pause_in_typing() -> None:
    bind = function_source(_JS, "bindCompareSearch")
    assert "SEARCH_DELAY_MS" in bind
    assert 'getElementById("compare-search-bar").addEventListener("submit"' in bind
    assert "MAX_RUNS_LIMIT" in bind


def test_a_pick_redraws_the_pickers_from_the_held_page_and_reloads_the_comparison() -> None:
    assert "renderCompareRuns(list.page, request)" in function_source(_JS, "refreshCompare")
    assert "state.compareLoaded.settled = null" in function_source(_JS, "onComparePick")


def test_the_compare_search_box_never_pauses_the_compare_refresh() -> None:
    assert 'compare: "compare-content"' in _JS


def test_the_compare_view_has_a_labelled_search_box_and_a_pager() -> None:
    html = dashboard_assets.render_index_html(token="t")
    assert (
        '<label class="visually-hidden" for="compare-search">Find runs to compare</label>' in html
    )
    assert re.search(r'<input id="compare-search" type="search" maxlength="\d+"', html)
    for button in (
        'id="compare-prev" type="button">Newer',
        'id="compare-next" type="button">Older',
    ):
        assert button in html
