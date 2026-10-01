"""Asset tests for the dashboard shell, routes, refresh and layout (slice 1 of #80).

No JavaScript runner is available (ADR-016), so these tests read ``app.js``,
``style.css`` and the index page as text.
"""

from __future__ import annotations

import re

import pytest
from dashboard_js import function_source, normalized, object_literal_source

from software_agent_factory.dashboard import assets as dashboard_assets

# --------------------------------------------------------------------------
# Shell, navigation and hash routes (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------

_INDEX_HTML = dashboard_assets.render_index_html(token="fixture-token")

#: (section id, heading id) for the one section each view renders into.
_VIEW_SECTIONS = (
    ("view-runs", "runs-heading"),
    ("view-run", "detail-heading"),
    ("view-compare", "compare-heading"),
    ("view-projects", "projects-heading"),
    ("view-health", "health-heading"),
)

#: Every table the page ships is named, so each one can be checked on its own.
_TABLE_IDS = re.findall(r'<table\s+id="([^"]+)"', _INDEX_HTML)


def test_index_html_has_a_main_nav_landmark_with_the_four_links() -> None:
    nav = re.search(r'<nav\s+aria-label="Main">(.*?)</nav>', _INDEX_HTML, flags=re.DOTALL)
    assert nav is not None
    links = re.findall(r'<a\s+href="(#[a-z]+)"\s+data-route="([a-z]+)">([^<]+)</a>', nav.group(1))
    assert links == [
        ("#runs", "runs", "Runs"),
        ("#compare", "compare", "Compare"),
        ("#projects", "projects", "Projects"),
        ("#health", "health", "Health"),
    ]


@pytest.mark.parametrize(("section_id", "heading_id"), _VIEW_SECTIONS)
def test_each_view_is_one_hidden_section_with_a_focusable_heading(
    section_id: str, heading_id: str
) -> None:
    section = rf'<section\s+id="{section_id}"\s+aria-labelledby="{heading_id}"\s+hidden>'
    assert re.search(section, _INDEX_HTML)
    assert re.search(rf'<h1\s+id="{heading_id}"\s+tabindex="-1">', _INDEX_HTML)


def test_totals_stay_on_the_runs_view() -> None:
    runs_view = _INDEX_HTML.split('<section id="view-runs"')[1].split("</section>")[0]
    assert 'id="totals-body"' in runs_view
    assert 'id="runs-body"' in runs_view


def test_script_is_deferred_in_the_head_and_not_in_the_body() -> None:
    head, body = _INDEX_HTML.split("</head>")
    assert re.search(
        r'<script\s+defer\s+src="/assets/app\.js\?token=fixture-token"></script>', head
    )
    assert "<script" not in body


def test_route_parser_pins_every_route_shape() -> None:
    js = dashboard_assets.APP_JS
    parser = function_source(js, "parseRoute")
    # A run needs exactly one id part; compare takes none or a pair of ids.
    assert 'name === "run" && parts.length === 2' in parser
    assert 'return { view: "run", runId: parts[1] };' in parser
    assert 'name === "compare" && (parts.length === 1 || parts.length === 3)' in parser
    assert 'return { view: "compare" };' in parser
    assert "parts.length === 1 && SIMPLE_VIEWS.has(name)" in parser
    assert "return { view: name };" in parser
    assert parser.endswith("return null; }")
    assert 'const SIMPLE_VIEWS = new Set(["runs", "projects", "health"]);' in js


def test_an_unknown_hash_redirects_in_apply_route_and_resolve_route_has_no_side_effects() -> None:
    js = dashboard_assets.APP_JS
    resolver = function_source(js, "resolveRoute")
    assert resolver == (
        "function resolveRoute() { "
        'return parseRoute(globalThis.location.hash) || { view: "runs" }; }'
    )
    assert "history" not in resolver
    assert function_source(js, "isUnknownHash") == (
        "function isUnknownHash(hash) { "
        'return hash !== "" && hash !== "#" && parseRoute(hash) === null; }'
    )
    apply_route = function_source(js, "applyRoute")
    assert apply_route.startswith(
        "function applyRoute(moveFocus) { if (isUnknownHash(globalThis.location.hash)) { "
        'globalThis.history.replaceState(null, "", "#runs"); } const route = resolveRoute();'
    )


def test_a_hash_change_applies_the_route_and_moves_focus() -> None:
    bindings = function_source(dashboard_assets.APP_JS, "bindControls")
    assert (
        'globalThis.addEventListener("hashchange", function () { applyRoute(true); });' in bindings
    )


def test_a_run_row_click_navigates_to_the_encoded_run_hash() -> None:
    js = dashboard_assets.APP_JS
    assert function_source(js, "navigateToRun") == (
        "function navigateToRun(runId) { "
        'globalThis.location.hash = "run/" + encodeURIComponent(runId); }'
    )


def test_app_js_validates_hash_run_ids_with_the_server_pattern() -> None:
    from software_agent_factory.dashboard import snapshot

    js = dashboard_assets.APP_JS
    # JS \w without the u flag is exactly [A-Za-z0-9_], the server's character set.
    js_pattern = snapshot._RUN_ID_PATTERN.pattern.replace("A-Za-z0-9_", r"\w")
    assert f"const RUN_ID_PATTERN = /{js_pattern}/;" in js
    assert "RUN_ID_PATTERN.test(runId)" in function_source(js, "prepareRunView")
    assert "RUN_ID_PATTERN.test(route.runId)" in function_source(js, "validRunId")


def test_the_run_view_heading_names_the_run_or_reports_an_unknown_one() -> None:
    prepare = function_source(dashboard_assets.APP_JS, "prepareRunView")
    assert prepare == (
        "function prepareRunView(runId) { "
        'const heading = document.getElementById("detail-heading"); '
        "if (!RUN_ID_PATTERN.test(runId)) { heading.textContent = VIEWS.run.label; "
        'setDetailStatus("Unknown run"); return; } '
        'heading.textContent = "Run " + runId; setDetailStatus("Loading\\u2026"); }'
    )


def test_a_title_names_the_view_and_the_run() -> None:
    js = dashboard_assets.APP_JS
    assert 'const TITLE_SUFFIX = " \\u2014 Factory dashboard";' in js
    title = function_source(js, "routeTitle")
    assert "VIEWS[route.view].label + TITLE_SUFFIX" in title
    assert '"Run " + route.runId : "Unknown run"' in title
    assert "document.title = routeTitle(route);" in function_source(js, "applyRoute")


def test_app_js_marks_the_active_link_and_focuses_the_heading() -> None:
    js = dashboard_assets.APP_JS
    mark = function_source(js, "markNavLink")
    assert 'link.setAttribute("aria-current", "page")' in mark
    assert 'link.removeAttribute("aria-current")' in mark
    assert "link.dataset.route === VIEWS[name].nav" in function_source(js, "showView")
    assert (
        "if (moveFocus) { document.getElementById(VIEWS[route.view].heading).focus(); }"
        in function_source(js, "applyRoute")
    )


def test_loading_and_empty_states() -> None:
    js = dashboard_assets.APP_JS
    assert '"No runs yet."' in function_source(js, "renderRunsStatus")
    assert 'setDetailStatus("Loading\\u2026");' in function_source(js, "prepareRunView")
    assert re.search(r'<p\s+id="runs-status">Loading&hellip;</p>', _INDEX_HTML)


def test_empty_run_table_stays_hidden_until_rows_arrive() -> None:
    assert re.search(
        r'<div\s+class="table-wrap"\s+hidden>\s*<table\s+id="runs-table">', _INDEX_HTML
    )
    assert (
        'document.querySelector("#view-runs .table-wrap").hidden = shown === 0;'
        in function_source(dashboard_assets.APP_JS, "renderRunsStatus")
    )


def test_compare_view_is_a_placeholder_card() -> None:
    compare = _INDEX_HTML.split('<section id="view-compare"')[1].split("</section>")[0]
    assert "Compare two runs &mdash; coming soon" in compare


def test_every_static_table_has_an_id() -> None:
    assert _TABLE_IDS
    assert len(_TABLE_IDS) == len(re.findall(r"<table\b", _INDEX_HTML))


@pytest.mark.parametrize("table_id", _TABLE_IDS)
def test_each_static_table_sits_in_a_table_wrap(table_id: str) -> None:
    wrapped = rf'<div\s+class="table-wrap"(?:\s+hidden)?>\s*<table\s+id="{table_id}">'
    assert re.search(wrapped, _INDEX_HTML)


def test_every_table_built_in_the_script_is_wrapped() -> None:
    js = dashboard_assets.APP_JS
    assert 'element("div", "table-wrap")' in function_source(js, "wrapTable")
    callers = re.findall(r"(\w+)\((?:tasksTable|modelsTable)\(", js)
    assert callers
    assert set(callers) == {"wrapTable"}


def test_table_wrap_scrolls_sideways_and_the_page_never_does() -> None:
    css = dashboard_assets.STYLE_CSS
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto;", css)
    assert re.search(r"main\s*\{[^}]*min-width:\s*0;", css)
    assert re.search(r"dd\s*\{[^}]*overflow-wrap:\s*anywhere;", css)
    assert "overflow-x: hidden" not in css
    assert "overflow: hidden" not in css


def test_sidebar_collapses_to_a_top_bar_under_900px() -> None:
    media = re.search(
        r"@media\s*\(max-width:\s*900px\)\s*\{(.*?)\n\}", dashboard_assets.STYLE_CSS, re.DOTALL
    )
    assert media is not None
    block = normalized(media.group(1))
    assert ".shell { grid-template-columns: minmax(0, 1fr);" in block
    assert re.search(r"\.sidebar \{[^}]*position: static;[^}]*height: auto;", block)
    assert "nav { flex-direction: row; flex-wrap: wrap; }" in block


def test_cards_use_a_12_to_16_pixel_radius_on_the_surface_token() -> None:
    rule = re.search(r"\.card\s*\{([^}]*)\}", dashboard_assets.STYLE_CSS)
    assert rule is not None
    assert "background: var(--surface);" in rule.group(1)
    radius = re.search(r"border-radius:\s*(\d+)px;", rule.group(1))
    assert radius is not None
    assert 12 <= int(radius.group(1)) <= 16


def test_active_nav_link_uses_the_accent_token() -> None:
    rule = re.search(r'nav a\[aria-current="page"\]\s*\{([^}]*)\}', dashboard_assets.STYLE_CSS)
    assert rule is not None
    assert "background: var(--accent);" in rule.group(1)


# --------------------------------------------------------------------------
# Refresh dispatcher and connection notices (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------


def test_one_poll_refreshes_only_the_open_view() -> None:
    js = dashboard_assets.APP_JS
    code = normalized(js)
    assert "const POLL_INTERVAL_MS = 5000;" in code
    assert code.count("setInterval(") == 1
    assert "globalThis.setInterval(pollView, POLL_INTERVAL_MS);" in code
    refreshers = object_literal_source(js, "REFRESHERS")
    assert re.findall(r"(\w+): function", refreshers) == [
        "runs",
        "run",
        "compare",
        "projects",
        "health",
    ]
    assert "REFRESHERS[view]" in function_source(js, "refreshView")


def test_start_applies_the_theme_then_wires_controls_then_opens_the_route() -> None:
    start = function_source(dashboard_assets.APP_JS, "start")
    order = [
        "applyStoredTheme();",
        "bindControls();",
        "seedBackHistory();",
        "applyRoute(false);",
        "globalThis.setInterval(pollView, POLL_INTERVAL_MS);",
    ]
    positions = [start.index(call) for call in order]
    assert positions == sorted(positions)


def test_a_route_change_refreshes_the_view_it_opens() -> None:
    apply_route = function_source(dashboard_assets.APP_JS, "applyRoute")
    assert apply_route.endswith("void refreshView(); }")


def test_dirty_guard_checks_focused_fields_and_open_dialogs() -> None:
    js = dashboard_assets.APP_JS
    guard = function_source(js, "isDirty")
    assert "region.contains(active)" in guard
    assert 'active.matches("input, textarea, select")' in guard
    assert 'region.querySelector("dialog[open]") !== null' in guard
    refresh_view = function_source(js, "refreshView")
    assert "isDirty(document.getElementById(VIEWS[view].section))" in refresh_view


def test_refresh_patches_rows_and_text_in_place() -> None:
    js = dashboard_assets.APP_JS
    for name in ("syncRows", "syncList", "syncDefinitionList", "setText"):
        assert f"function {name}(" in js
    assert "if (node.textContent !== text)" in normalized(js)
    assert "signature !== lastProjectsSignature" in function_source(js, "renderProjectsOnChange")


@pytest.mark.parametrize(
    ("renderer", "patchers"),
    [
        ("renderRuns", ["syncRows("]),
        ("renderDetail", ["syncDefinitionList(", "syncRows("]),
    ],
)
def test_the_live_views_patch_in_place_and_never_clear_their_content(
    renderer: str, patchers: list[str]
) -> None:
    source = function_source(dashboard_assets.APP_JS, renderer)
    for patcher in patchers:
        assert patcher in source
    assert "clearChildren(" not in source
    assert "replaceChildren(" not in source


def test_a_failed_refresh_leaves_the_views_as_they_were() -> None:
    js = dashboard_assets.APP_JS
    failure = function_source(js, "onRefreshFailure")
    assert "clearChildren" not in failure
    assert "replaceChildren" not in failure
    assert "hidden" not in failure
    assert "renderNotice();" in failure
    # The notice writes one status line and touches nothing else.
    assert "getElementById" not in failure
    assert 'getElementById("notice")' in function_source(js, "renderNotice")


def test_notices_live_in_one_polite_status_region() -> None:
    assert re.search(r'<p\s+id="notice"\s+role="status"\s+aria-live="polite"></p>', _INDEX_HTML)
    assert _INDEX_HTML.count('role="status"') == 1


def test_notice_texts_report_a_lost_connection_and_a_restart() -> None:
    message = function_source(dashboard_assets.APP_JS, "noticeMessage")
    assert (
        '"Connection lost, updated " + Math.floor((Date.now() - lastSuccessAt) / 1000) + "s ago"'
        in message
    )
    assert '"Connection lost, not updated yet"' in message
    assert '"Dashboard restarted, reload the page."' in message


def test_a_401_is_told_apart_from_a_network_error() -> None:
    js = dashboard_assets.APP_JS
    reader = function_source(js, "readResponse")
    assert "response.status === 401" in reader
    assert "apiError(ERROR_UNAUTHORIZED" in reader
    assert "response.status >= 500 ? ERROR_CONNECTION : ERROR_REQUEST" in reader
    assert "apiError(ERROR_CONNECTION" in function_source(js, "onNetworkError")
    failure = function_source(js, "onRefreshFailure")
    assert "error.kind === ERROR_UNAUTHORIZED || error.kind === ERROR_CONNECTION" in failure
    assert "state.noticeKind = error.kind;" in failure


def test_an_answered_request_clears_the_connection_notice() -> None:
    failure = function_source(dashboard_assets.APP_JS, "onRefreshFailure")
    request_branch = failure.split("error.kind === ERROR_REQUEST")[1].split("} else {")[0]
    assert "onServerReachable();" in request_branch


def test_a_page_bug_is_logged_and_never_shown_as_a_lost_connection() -> None:
    failure = function_source(dashboard_assets.APP_JS, "onRefreshFailure")
    fallback = failure.split("} else {")[-1]
    assert "console.error(error);" in fallback
    assert "noticeKind" not in fallback


def test_views_with_nothing_to_load_still_check_the_server() -> None:
    js = dashboard_assets.APP_JS
    assert 'apiFetch("/healthz")' in function_source(js, "pingServer")
    refreshers = object_literal_source(js, "REFRESHERS")
    assert "request.runId === null ? [pingServer()]" in refreshers
    assert "compare: function () { return [pingServer()]; }" in refreshers


def test_a_refresh_stays_in_flight_until_every_task_ends() -> None:
    js = dashboard_assets.APP_JS
    assert "Promise.allSettled(tasks)" in function_source(js, "rejectOnFirstFailure")
    settle = function_source(js, "settle")
    assert "rejectOnFirstFailure(tasks)" in settle
    assert "Promise.all(" not in settle


def test_a_reused_row_drops_a_stale_run_id() -> None:
    patch = function_source(dashboard_assets.APP_JS, "patchRow")
    assert "delete row.dataset.runId;" in patch


def test_a_successful_refresh_clears_the_notice() -> None:
    js = dashboard_assets.APP_JS
    success = function_source(js, "onServerReachable")
    assert "state.noticeKind = null;" in success
    assert "lastSuccessAt = Date.now();" in success
    assert "whenLatest(request, onServerReachable)" in function_source(js, "settle")


def test_a_deep_link_seeds_history_so_back_returns_to_the_run_list() -> None:
    js = dashboard_assets.APP_JS
    seed = function_source(js, "seedBackHistory")
    assert seed.index('replaceState(null, "", "#runs")') < seed.index("pushState(")
    assert 'pushState({ seeded: true }, "", hash)' in seed
    assert "deepLink && !tabWasSeeded()" in seed
    assert re.search(r"markTabSeeded\(\);\s*\}\s*$", seed)


def test_a_tab_is_seeded_once_even_across_reloads() -> None:
    js = dashboard_assets.APP_JS
    was_seeded = function_source(js, "tabWasSeeded")
    assert "sessionStorage.getItem(SEEDED_KEY)" in was_seeded
    assert "history.state?.seeded === true" in was_seeded.split("catch")[1]
    assert "sessionStorage.setItem(SEEDED_KEY" in function_source(js, "markTabSeeded")


# --------------------------------------------------------------------------
# Stale responses, in-flight polls and timeouts (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------

_REFRESH_FUNCTIONS = (
    "refreshRuns",
    "refreshTotals",
    "refreshHealth",
    "refreshProjects",
    "refreshDetail",
)


def test_a_request_records_the_view_sequence_offset_limit_and_run() -> None:
    begin = function_source(dashboard_assets.APP_JS, "beginRequest")
    for captured in (
        "seq: requests[view].seq",
        "offset: state.offset",
        "limit: state.limit",
        "runId: state.runId",
    ):
        assert captured in begin
    assert "requests[view].inFlight += 1" in begin


@pytest.mark.parametrize("name", _REFRESH_FUNCTIONS)
def test_every_refresh_drops_a_response_that_a_newer_request_overtook(name: str) -> None:
    source = function_source(dashboard_assets.APP_JS, name)
    assert "whenLatest(request, " in source


def test_a_response_is_dropped_unless_its_request_is_the_latest_for_the_view() -> None:
    js = dashboard_assets.APP_JS
    assert function_source(js, "isLatest") == (
        "function isLatest(request) { return requests[request.view].seq === request.seq; }"
    )
    assert function_source(js, "whenLatest") == (
        "function whenLatest(request, handler) { return function (value) { "
        "if (isLatest(request)) { handler(value, request); } }; }"
    )


def test_a_route_change_supersedes_older_requests_for_the_view_it_opens() -> None:
    apply_route = function_source(dashboard_assets.APP_JS, "applyRoute")
    assert apply_route.index("supersede(route.view);") < apply_route.index("refreshView();")


def test_the_runs_pager_reads_the_requested_page_and_never_the_live_state() -> None:
    js = dashboard_assets.APP_JS
    assert "renderRunsPager(payload.page || {}, runs.length, request)" in function_source(
        js, "renderRuns"
    )
    refresh = function_source(js, "refreshRuns")
    assert "encodeURIComponent(request.limit)" in refresh
    assert "encodeURIComponent(request.offset)" in refresh
    for name in ("renderRunsPager", "hasMoreRuns", "renderRunsStatus", "refreshRuns"):
        assert "state." not in function_source(js, name)


def test_a_refresh_never_ends_in_an_unhandled_rejection() -> None:
    settle = function_source(dashboard_assets.APP_JS, "settle")
    assert settle.index(".then(") < settle.index(".catch(") < settle.index(".finally(")
    assert "endRequest(request)" in settle.split(".finally(")[1]


def test_a_poll_tick_waits_while_the_open_view_has_a_request_in_flight() -> None:
    assert function_source(dashboard_assets.APP_JS, "pollView") == (
        "function pollView() { if (requests[state.view].inFlight === 0) { void refreshView(); } }"
    )


def test_a_request_times_out_after_ten_seconds_and_counts_as_a_lost_connection() -> None:
    js = dashboard_assets.APP_JS
    assert "const REQUEST_TIMEOUT_MS = 10000;" in js
    fetcher = function_source(js, "apiFetch")
    assert "new AbortController()" in fetcher
    assert "controller.abort()" in fetcher
    assert "}, REQUEST_TIMEOUT_MS);" in fetcher
    assert "signal: controller.signal" in fetcher
    assert ".then(readResponse, onNetworkError)" in fetcher
    # The timer stops only once the body is read, so a stalled body times out too.
    assert fetcher.index(".then(readResponse") < fetcher.index(".finally(")
    assert "globalThis.clearTimeout(timer)" in fetcher.split(".finally(")[1]
    assert "response.json().catch(onNetworkError)" in function_source(js, "readResponse")
