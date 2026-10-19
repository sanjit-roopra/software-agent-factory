"""Opt-in browser notifications for runs that need you or went stale."""

from __future__ import annotations

from dashboard_js import JsCall, function_source, listener_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets

_JS = dashboard_assets.APP_JS


def _alert(run_id: str, kind: str = "needs_you") -> dict[str, str]:
    return {"id": run_id, "kind": kind, "title": "Title " + run_id}


def _fresh(seen: list[str] | None, current: list[dict[str, str]]) -> dict[str, object]:
    value = run_functions(_JS, [JsCall("freshAlerts", (seen, current))])[0].value
    assert isinstance(value, dict)
    return value


def test_first_check_without_memory_remembers_the_waiting_runs_and_tells_nothing() -> None:
    result = _fresh(None, [_alert("r1"), _alert("r2", "stale")])
    assert result == {"fresh": [], "seen": ["needs_you:r1", "stale:r2"]}


def test_a_run_that_newly_needs_you_notifies_once_across_polls() -> None:
    first = _fresh([], [_alert("r1")])
    assert first["fresh"] == [_alert("r1")]
    second = _fresh(first["seen"], [_alert("r1")])  # type: ignore[arg-type]
    assert second["fresh"] == []
    assert second["seen"] == ["needs_you:r1"]


def test_a_run_that_leaves_and_comes_back_does_not_notify_again() -> None:
    first = _fresh(["needs_you:r1"], [])
    assert _fresh(first["seen"], [_alert("r1")])["fresh"] == []  # type: ignore[arg-type]


def test_the_same_run_turning_stale_is_a_new_alert() -> None:
    result = _fresh(["needs_you:r1"], [_alert("r1", "stale")])
    assert result["fresh"] == [_alert("r1", "stale")]


def test_a_run_listed_twice_in_one_check_notifies_once() -> None:
    assert _fresh([], [_alert("r1"), _alert("r1")])["fresh"] == [_alert("r1")]


def test_memory_keeps_only_the_newest_keys() -> None:
    seen = [f"needs_you:old{index}" for index in range(200)]
    result = _fresh(seen, [_alert("new")])
    kept = result["seen"]
    assert isinstance(kept, list)
    assert len(kept) == 200
    assert kept[-1] == "needs_you:new"
    assert "needs_you:old0" not in kept


def test_the_toggle_sits_in_the_sidebar_hidden_until_the_script_shows_it() -> None:
    html = dashboard_assets.render_index_html(token="t")
    sidebar = html.split('<aside class="sidebar">')[1].split("</aside>")[0]
    assert '<button id="notify-toggle" type="button" aria-pressed="false" hidden>Notify me' in (
        sidebar
    )


def test_permission_is_asked_only_from_the_click() -> None:
    assert _JS.count("requestPermission(") == 1
    assert "requestPermission(" in function_source(_JS, "onNotifyToggle")
    assert "onNotifyToggle" in function_source(_JS, "bindNotify")


def test_clicking_a_notification_focuses_the_tab_and_opens_the_run() -> None:
    listener = listener_source(_JS, "showNotification", "note", "click")
    assert "globalThis.focus();" in listener
    assert 'globalThis.location.hash = "#run/" + encodeURIComponent(alert.id);' in listener


def test_the_check_reads_the_needs_you_and_stale_run_lists() -> None:
    body = function_source(_JS, "checkNotify")
    assert "if (!notifyOn || notifyInFlight)" in body
    assert '"/api/runs?limit=100&state=" + spec.state' in body
    assert "freshAlerts(notifySeen" in body
    assert "checkNotify();" in function_source(_JS, "pollTick")
