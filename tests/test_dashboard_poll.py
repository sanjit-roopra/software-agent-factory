"""How often the dashboard polls, run in ``node`` (ADR-076).

The poll runs every 2 s while the open view shows a run that can still change and
every 15 s otherwise.
"""

from __future__ import annotations

import pytest
from dashboard_js import JsCall, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets

_FAST = 2000
_SLOW = 15000
_ACTIVE = {"outcome": {"kind": "active"}}
_DONE = {"outcome": {"kind": "succeeded"}}
_NO_PAIR = {"a": None, "b": None}
_PAIR = {"a": "r1", "b": "r2"}


def _held(
    *,
    runs: list[object] | None = None,
    detail: object = None,
    compare: dict[str, object] = _NO_PAIR,
    settled: str | None = None,
) -> dict[str, object]:
    return {
        "shownRuns": None if runs is None else [{"runs": runs}, {}],
        "runDetail": detail,
        "compare": compare,
        "compareLoaded": {"list": None, "settled": settled},
    }


@pytest.mark.parametrize(
    ("view", "held", "expected"),
    [
        pytest.param("runs", _held(runs=[_DONE, _ACTIVE]), _FAST, id="list-with-active-run"),
        pytest.param("runs", _held(runs=[_DONE]), _SLOW, id="list-all-finished"),
        pytest.param("runs", _held(), _SLOW, id="list-not-loaded"),
        pytest.param("run", _held(detail=_ACTIVE), _FAST, id="open-run-active"),
        pytest.param("run", _held(detail={"outcome": {"kind": "stale"}}), _FAST, id="stale"),
        pytest.param("run", _held(detail=_DONE), _SLOW, id="open-run-finished"),
        pytest.param("run", _held(), _SLOW, id="open-run-not-loaded"),
        pytest.param("compare", _held(compare=_PAIR), _FAST, id="pair-still-going"),
        pytest.param("compare", _held(compare=_PAIR, settled="r1/r2"), _SLOW, id="pair-settled"),
        pytest.param("compare", _held(), _SLOW, id="no-pair"),
        pytest.param("projects", _held(runs=[_ACTIVE]), _SLOW, id="projects"),
        pytest.param("health", _held(detail=_ACTIVE), _SLOW, id="health"),
    ],
)
def test_the_poll_is_fast_only_while_the_open_view_shows_a_changing_run(
    view: str, held: dict[str, object], expected: int
) -> None:
    result = run_functions(dashboard_assets.APP_JS, [JsCall("pollDelay", (view, held))])[0]

    assert result.value == expected
