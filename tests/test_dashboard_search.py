"""Run list search, keys, deep links and run page tabs (slice 13)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dashboard_js import JsCall, function_source, run_functions

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, DashboardServer, create_server
from software_agent_factory.dashboard.snapshot import MAX_SEARCH_LENGTH, parse_run_filter
from software_agent_factory.models import (
    RUN_LIST_STATES,
    FactoryRun,
    RunListFilter,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.observability import build_monitoring_snapshot
from software_agent_factory.store import FileRunStore

_JS = dashboard_assets.APP_JS
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def _values(*calls: JsCall) -> list[Any]:
    return [result.value for result in run_functions(_JS, calls)]


# -- snapshot filter -----------------------------------------------------------


def _store(tmp_path: Path) -> FileRunStore:
    """Three runs, newest first: a failed one in project alpha, a waiting one, a done one."""
    store = FileRunStore(tmp_path / "data")
    rows = [
        ("run-a", WorkflowState.FAILED, "Fix the login page", "acme/web#17", "alpha"),
        ("run-b", WorkflowState.NEEDS_HUMAN, "Add a search box", None, None),
        ("run-c", WorkflowState.DONE, "Login copy tweak", None, "beta"),
    ]
    for index, (run_id, state, title, external_id, project_id) in enumerate(rows):
        created = T0 - timedelta(minutes=index)
        store.save_run(
            FactoryRun(
                id=run_id,
                work_item_id=f"WI-{run_id}",
                state=state,
                created_at=created,
                updated_at=created,
                completed_at=created if state is WorkflowState.DONE else None,
            )
        )
        store.save_artifact(
            run_id,
            WorkItem(
                id=f"WI-{run_id}",
                title=title,
                description="d",
                external_id=external_id,
                project_id=project_id,
            ),
        )
    return store


def _ids(store: FileRunStore, run_filter: RunListFilter | None) -> list[str]:
    snapshot = build_monitoring_snapshot(store, now=T0, run_filter=run_filter)
    assert snapshot.page.total == len(snapshot.runs)
    return [run.run_id for run in snapshot.runs]


def test_the_filter_narrows_and_orders_the_page_but_not_the_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _ids(store, None) == ["run-a", "run-b", "run-c"]
    assert _ids(store, RunListFilter()) == ["run-a", "run-b", "run-c"]
    assert _ids(store, RunListFilter(text="LOGIN")) == ["run-a", "run-c"]
    assert _ids(store, RunListFilter(text="web#17")) == ["run-a"]
    assert _ids(store, RunListFilter(text="wi-run-b")) == ["run-b"]
    assert _ids(store, RunListFilter(state="escalated")) == ["run-b"]
    assert _ids(store, RunListFilter(state="succeeded")) == ["run-c"]
    assert _ids(store, RunListFilter(project_id="beta")) == ["run-c"]
    assert _ids(store, RunListFilter(text="login", project_id="alpha")) == ["run-a"]
    assert _ids(store, RunListFilter(oldest_first=True)) == ["run-c", "run-b", "run-a"]
    assert _ids(store, RunListFilter(text="nothing like it")) == []

    snapshot = build_monitoring_snapshot(store, now=T0, run_filter=RunListFilter(state="failed"))
    assert (snapshot.counts.failed, snapshot.counts.escalated, snapshot.counts.succeeded) == (
        1,
        1,
        1,
    )


def test_parse_run_filter_reads_the_query_and_refuses_what_it_cannot_take() -> None:
    assert parse_run_filter({}) == RunListFilter()
    assert parse_run_filter(
        {"q": ["  login "], "state": ["failed"], "project": ["alpha"], "sort": ["oldest"]}
    ) == RunListFilter("login", "failed", "alpha", True)
    assert parse_run_filter({"q": ["   "], "sort": ["newest"]}) == RunListFilter()
    for bad in (
        {"q": ["x" * (MAX_SEARCH_LENGTH + 1)]},
        {"state": ["stuck"]},
        {"project": ["../etc"]},
        {"sort": ["random"]},
    ):
        assert parse_run_filter(bad) is None
    assert RUN_LIST_STATES == {"escalated", "failed", "succeeded", "active"}


# -- /api/runs -----------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[tuple[DashboardServer, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    def provider(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"runs": [], "page": {}}

    running = create_server(
        DashboardConfig(snapshot_provider=provider, run_detail_provider=lambda run_id: None)
    )
    threading.Thread(target=running.serve_forever, daemon=True).start()
    yield running, seen
    running.shutdown()
    running.server_close()


def _get(server: DashboardServer, path: str) -> tuple[int, Any]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
    connection.request("GET", path, headers={TOKEN_HEADER: server.token})
    response = connection.getresponse()
    body = json.loads(response.read() or b"null")
    connection.close()
    return response.status, body


def test_api_runs_passes_a_filter_only_when_one_is_asked_for(
    server: tuple[DashboardServer, list[dict[str, Any]]],
) -> None:
    running, seen = server

    assert _get(running, "/api/runs?limit=5&offset=0")[0] == 200
    assert _get(running, "/api/runs?q=login&state=failed&project=alpha&sort=oldest")[0] == 200

    assert seen == [
        {"limit": 5, "offset": 0},
        {"limit": 20, "offset": 0, "filters": RunListFilter("login", "failed", "alpha", True)},
    ]


@pytest.mark.parametrize("query", ["state=stuck", "sort=up", "project=a%2Fb", "q=" + "x" * 101])
def test_api_runs_refuses_a_filter_it_cannot_take(
    server: tuple[DashboardServer, list[dict[str, Any]]], query: str
) -> None:
    running, seen = server

    assert _get(running, "/api/runs?" + query) == (400, {"error": "invalid filter"})
    assert seen == []


# -- page script ---------------------------------------------------------------


def test_a_runs_hash_carries_the_search_and_page_and_reads_back_the_same() -> None:
    search = {"q": "login page", "state": "failed", "project": "alpha", "sort": "oldest"}
    empty = {"q": "", "state": "", "project": "", "sort": ""}

    assert _values(
        JsCall("runsHash", (empty, 0)),
        JsCall("runsHash", (search, 40)),
        JsCall("routeSearch", ("q=login+page&state=failed&project=alpha&sort=oldest",)),
        JsCall("routeSearch", ("state=stuck&project=..%2Fx&sort=up&q=",)),
        JsCall("routeOffset", ("offset=40",)),
        JsCall("routeOffset", ("offset=-3",)),
        JsCall("routeOffset", ("offset=2.5",)),
        JsCall("routeOffset", ("offset=99999999",)),
    ) == [
        "#runs",
        "#runs?q=login+page&state=failed&project=alpha&sort=oldest&offset=40",
        search,
        empty,
        40,
        0,
        0,
        0,
    ]


def test_a_run_hash_names_the_tab_unless_it_is_the_first() -> None:
    assert _values(
        JsCall("runHash", ("run-1", "overview")),
        JsCall("runHash", ("run-1", "changes")),
        JsCall("routeTab", ("tab=checks",)),
        JsCall("routeTab", ("tab=secret",)),
        JsCall("routeTab", ("",)),
    ) == ["#run/run-1", "#run/run-1?tab=changes", "checks", "overview", "overview"]


def test_keys_map_to_commands_and_g_waits_for_a_view() -> None:
    assert _values(
        JsCall("keyCommand", ("j", False)),
        JsCall("keyCommand", ("Enter", False)),
        JsCall("keyCommand", ("?", False)),
        JsCall("keyCommand", ("g", False)),
        JsCall("keyCommand", ("p", True)),
        JsCall("keyCommand", ("x", True)),
        JsCall("keyCommand", ("x", False)),
        JsCall("keyCommand", ("toString", False)),
    ) == ["next", "open", "help", "await-go", "go:#projects", None, None, None]


def test_row_steps_stay_in_the_list_and_jumps_wrap_round() -> None:
    assert _values(
        JsCall("steppedIndex", (0, -1, 1)),
        JsCall("steppedIndex", (3, -1, 1)),
        JsCall("steppedIndex", (3, -1, -1)),
        JsCall("steppedIndex", (3, 2, 1)),
        JsCall("steppedIndex", (3, 0, -1)),
        JsCall("nextMatchIndex", ([False, True, False, True], 1)),
        JsCall("nextMatchIndex", ([False, True, False, True], 3)),
        JsCall("nextMatchIndex", ([False, True], -1)),
        JsCall("nextMatchIndex", ([False, False], 0)),
    ) == [-1, 0, 2, 2, 0, 3, 1, 1, -1]


def test_keys_leave_fields_and_modifiers_to_the_browser() -> None:
    own = function_source(_JS, "isOwnKey")
    assert "event.ctrlKey || event.metaKey || event.altKey" in own
    assert 'target.closest("input, textarea, select, [contenteditable], dialog")' in own
    assert 'document.addEventListener("keydown", onKeydown);' in function_source(_JS, "bindKeys")
    assert "bindKeys();" in function_source(_JS, "bindControls")


def test_paging_and_searching_add_history_entries_and_typing_replaces_one() -> None:
    navigate = function_source(_JS, "navigateRuns")
    assert 'globalThis.history.pushState(null, "", hash);' in navigate
    assert 'globalThis.history.replaceState(null, "", hash);' in navigate
    assert "navigateRuns(state.search, offset);" in function_source(_JS, "goToOffset")
    assert "navigateRuns(readSearch(), 0, true);" in function_source(_JS, "onSearchInput")


def test_every_tab_has_a_panel_and_the_first_is_shown() -> None:
    html = dashboard_assets.render_index_html(token="t")
    for index, tab in enumerate(["overview", "activity", "changes", "review", "checks", "raw"]):
        selected = "true" if index == 0 else "false"
        assert f'id="run-tab-{tab}"' in html
        assert f'aria-controls="run-panel-{tab}" aria-selected="{selected}"' in html
        assert f'id="run-panel-{tab}"' in html
    assert 'const RUN_TABS = ["overview", "activity", "changes", "review", "checks", "raw"];' in _JS
    for card, panel in (
        ("completion-card", "overview"),
        ("plan-card", "overview"),
        ("timeline-heading", "activity"),
        ("diff-card", "changes"),
        ("review-card", "review"),
        ("checks-card", "checks"),
    ):
        after = html.split(f'id="run-panel-{panel}"')[1].split('id="run-panel-')[0]
        assert f'id="{card}"' in after
