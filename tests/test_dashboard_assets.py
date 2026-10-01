"""Asset tests for the dashboard shell, routes, refresh and layout (slice 1 of #80).

No JavaScript runner is available (ADR-016), so these tests read ``app.js``,
``style.css`` and the index page as text.
"""

from __future__ import annotations

import re
from typing import get_args

import pytest
from dashboard_js import (
    function_source,
    listener_source,
    normalized,
    object_literal_source,
    strip_comments,
)

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.responses import ConflictReason
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.snapshot import MAX_PAGE_LIMIT
from software_agent_factory.observability import MonitoringSnapshot
from software_agent_factory.resume import MAX_PLAN_DECISION_ANSWER_CHARS

# --------------------------------------------------------------------------
# Shell, navigation and hash routes (asset tests; no JS runner, ADR-016)
# --------------------------------------------------------------------------

_INDEX_HTML = dashboard_assets.render_index_html(token="fixture-token")

#: (section id, heading id) for the one section each view renders into.
_VIEW_SECTIONS = (
    ("view-runs", "runs-heading"),
    ("view-run-detail", "run-detail-heading"),
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
    # A run needs exactly one id part; compare takes up to two ids, run A then run B.
    assert 'name === "run" && parts.length === 2' in parser
    assert 'return { view: "run", runId: parts[1] };' in parser
    assert 'name === "compare" && parts.length <= 3' in parser
    assert 'return { view: "compare", a: parts[1], b: parts[2] };' in parser
    assert "parts.length === 1 && SIMPLE_VIEWS.has(name)" in parser
    assert "return { view: name, filter: routeFilter(query) };" in parser
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
    assert "RUN_ID_PATTERN.test(runId)" in function_source(js, "prepareRunDetailView")
    assert "RUN_ID_PATTERN.test(route.runId)" in function_source(js, "validRunId")


def test_the_run_view_heading_names_the_run_or_reports_an_unknown_one() -> None:
    prepare = function_source(dashboard_assets.APP_JS, "prepareRunDetailView")
    assert prepare == (
        "function prepareRunDetailView(runId) { "
        'const heading = document.getElementById("run-detail-heading"); '
        "if (!RUN_ID_PATTERN.test(runId)) { heading.textContent = VIEWS.run.label; "
        'setRunDetailStatus("Unknown run"); return; } '
        'heading.textContent = "Run " + runId; setRunDetailStatus("Loading\\u2026"); }'
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
    assert '"No runs yet."' in function_source(js, "emptyRunsText")
    assert "emptyRunsText(request)" in function_source(js, "renderRunsStatus")
    assert 'setRunDetailStatus("Loading\\u2026");' in function_source(js, "prepareRunDetailView")
    assert re.search(r'<p\s+id="runs-status">Loading&hellip;</p>', _INDEX_HTML)


def test_empty_run_table_stays_hidden_until_rows_arrive() -> None:
    assert re.search(
        r'<div\s+class="table-wrap"\s+hidden>\s*<table\s+id="runs-table">', _INDEX_HTML
    )
    assert (
        'document.querySelector("#view-runs .table-wrap").hidden = shown === 0;'
        in function_source(dashboard_assets.APP_JS, "renderRunsStatus")
    )


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
    assert re.findall(r"(\w+): (?:function|refreshCompare)", refreshers) == [
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
    assert 'active.matches("input, textarea")' in guard
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
        ("renderRunDetail", ["syncDefinitionList(", "syncRows("]),
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
    notice = r'<p\s+id="notice"\s+role="status"\s+aria-live="polite"\s+tabindex="-1"></p>'
    assert re.search(notice, _INDEX_HTML)
    # The compare status line is the only other status region (slice 5 review).
    assert re.findall(r'<p\s+id="([^"]+)"\s+role="status"', _INDEX_HTML) == [
        "notice",
        "compare-status",
    ]


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
    "refreshRunDetail",
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


def test_the_project_usage_value_comes_from_the_server_totals_not_a_client_sum() -> None:
    js = dashboard_assets.APP_JS
    assert "totalUsageValue" not in js
    assert "totals?.costs?.usage_value_usd" in function_source(js, "usageSummary")
    assert "usageSummary(project.totals)" in function_source(js, "projectCard")


# --------------------------------------------------------------------------
# Carried from the slice 1 review: one label, one constant, one stem
# --------------------------------------------------------------------------


def test_one_premium_requests_label_is_used_everywhere() -> None:
    js = dashboard_assets.APP_JS
    for text in (js, _INDEX_HTML):
        assert "Premium-request" not in text
        assert "premium-request" not in text
    assert '"Premium requests",' in function_source(js, "usageFields")
    model_headers = re.search(r"const MODEL_HEADERS = \[(.*?)\];", js, re.DOTALL)
    assert model_headers is not None
    assert '"Premium requests"' in model_headers.group(1)
    assert "Premium requests are a separate legacy metric." in js
    assert 'label: "Premium requests"' in _constant_source("COST_UNITS")


def test_unreported_cost_and_tokens_use_one_not_reported_constant() -> None:
    code = strip_comments(dashboard_assets.APP_JS)
    assert code.count('"not reported"') == 1
    assert 'const NOT_REPORTED = "not reported";' in code
    assert '"unknown"' not in code
    assert "displayListPriceEstimate" not in code


def test_project_model_rows_show_unreported_tokens_as_not_reported() -> None:
    row = function_source(dashboard_assets.APP_JS, "modelRow")
    assert "appendCell(row, displayNumber(usage.input_tokens));" in row
    assert "appendCell(row, displayNumber(usage.output_tokens));" in row


def test_an_empty_task_list_says_planning_only_in_a_planning_state() -> None:
    js = dashboard_assets.APP_JS
    message = function_source(js, "emptyTasksText")
    assert 'projectState === "PLANNING"' in message
    assert "Planning is in progress" in message
    assert '"No tasks."' in message
    assert js.count("Planning is in progress") == 1
    assert "emptyTasksText(projectState)" in function_source(js, "emptyTasksRow")
    assert "tasksTable(asArray(project.tasks), project.state)" in function_source(js, "projectCard")


def test_the_run_view_uses_one_run_detail_stem_for_ids_functions_and_styles() -> None:
    js = dashboard_assets.APP_JS
    for text in (js, _INDEX_HTML, dashboard_assets.STYLE_CSS):
        assert "detail-" not in text.replace("run-detail-", "")
    assert not re.search(r"\b(?:render|refresh|set|prepare)(?:Detail|RunView)\b", js)
    for element_id in ("view-run-detail", "run-detail-status", "run-detail-content"):
        assert f'id="{element_id}"' in _INDEX_HTML
    assert '<dl id="run-detail-body"></dl>' in _INDEX_HTML


# --------------------------------------------------------------------------
# Run detail: timeline, totals and the "Needs you" panel (slice 2 of #80)
# --------------------------------------------------------------------------

_RUN_DETAIL_HTML = _INDEX_HTML.split('<section id="view-run-detail"')[1].split("</section>\n\n")[0]


def _constant_source(name: str) -> str:
    """Normalized ``const NAME = ...;`` from the script, up to its closing line."""
    code = strip_comments(dashboard_assets.APP_JS)
    found = re.search(rf"const {name} = .*?\n  \S*;", code, re.DOTALL)
    assert found is not None, f"constant {name} not found in the dashboard script"
    return normalized(found.group(0))


def test_app_js_never_writes_html() -> None:
    js = dashboard_assets.APP_JS
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc"):
        assert sink not in js


def test_every_element_id_the_script_reads_exists_in_the_page() -> None:
    ids_read = set(re.findall(r'getElementById\("([^"]+)"\)', dashboard_assets.APP_JS))
    ids_in_page = set(re.findall(r'\bid="([^"]+)"', _INDEX_HTML))
    assert ids_read
    assert ids_read <= ids_in_page


@pytest.mark.parametrize(
    "element_id",
    [
        "next-step",
        "next-step-body",
        "run-totals",
        "timeline-status",
        "timeline-wrap",
        "timeline-body",
        "attempts-body",
    ],
)
def test_the_run_detail_view_holds_every_region_the_script_renders_into(element_id: str) -> None:
    assert f'id="{element_id}"' in _RUN_DETAIL_HTML


def test_the_timeline_heads_the_default_columns_in_order() -> None:
    head = _RUN_DETAIL_HTML.split('class="timeline-head"')[1].split("</div>")[0]
    assert re.findall(r"<span>([^<]*)</span>", head) == [
        "#",
        "Role",
        "Model",
        "Outcome",
        "Duration",
        "Total tokens",
        "Cost",
    ]


def test_the_timeline_group_is_named_by_its_heading() -> None:
    assert '<h2 id="timeline-heading">Call timeline</h2>' in _RUN_DETAIL_HTML
    assert re.search(
        r'<div\s+class="timeline"\s+role="group"\s+aria-labelledby="timeline-heading">',
        _RUN_DETAIL_HTML,
    )


def test_the_visual_header_row_is_hidden_from_assistive_technology() -> None:
    assert re.search(r'<div\s+class="timeline-head"\s+aria-hidden="true">', _RUN_DETAIL_HTML)


_TABLE_ROLE_CALL = re.compile(
    r"""setAttribute\(\s*["']role["']\s*,\s*["'](?:table|row|cell|columnheader|grid)["']"""
)


def test_the_table_role_check_matches_the_calls_it_is_meant_to_catch() -> None:
    assert _TABLE_ROLE_CALL.search('row.setAttribute("role", "row");')
    assert _TABLE_ROLE_CALL.search("cell.setAttribute( 'role' , 'columnheader' )")
    assert not _TABLE_ROLE_CALL.search('status.setAttribute("role", "status");')


def test_a_timeline_row_is_not_given_table_roles() -> None:
    timeline = _RUN_DETAIL_HTML.split('class="timeline"')[1].split("</section>")[0]
    for role in ("table", "row", "cell", "columnheader", "grid"):
        assert f'role="{role}"' not in timeline
    assert _TABLE_ROLE_CALL.search(strip_comments(dashboard_assets.APP_JS)) is None


def test_each_call_cell_starts_with_a_hidden_label_for_its_column() -> None:
    js = dashboard_assets.APP_JS
    assert '"visually-hidden", label + ": "' in function_source(js, "buildCell")
    assert "buildCell(label)" in function_source(js, "buildCall")


def test_the_call_cell_labels_match_the_visible_column_headers() -> None:
    labels = re.findall(r'"([^"]+)"', _constant_source("CALL_COLUMNS"))
    head = _RUN_DETAIL_HTML.split('class="timeline-head"')[1].split("</div>")[0]
    visible = re.findall(r"<span>([^<]*)</span>", head)
    assert labels[0] == "Call number"
    assert labels[1:] == visible[1:]
    assert len(labels) == len(visible)


def test_patching_a_call_writes_the_value_after_the_hidden_label() -> None:
    # The value is the last child of the cell, so the label stays in place.
    assert "cells[index].lastElementChild" in function_source(dashboard_assets.APP_JS, "patchCall")


def test_the_hidden_label_style_hides_text_visually_but_not_from_screen_readers() -> None:
    css = dashboard_assets.STYLE_CSS
    rule = re.search(r"\.visually-hidden\s*\{([^}]*)\}", css)
    assert rule is not None
    for declaration in (
        "position: absolute;",
        "width: 1px;",
        "height: 1px;",
        "clip: rect(0 0 0 0);",
        "clip-path: inset(50%);",
        "white-space: nowrap;",
    ):
        assert declaration in rule.group(1)
    assert "display: none" not in rule.group(1)
    assert "visibility: hidden" not in rule.group(1)


def test_the_row_caret_marks_only_the_first_cell_not_its_hidden_label() -> None:
    css = dashboard_assets.STYLE_CSS
    assert ".call-row > span:first-child::before" in css
    assert not re.search(r"\.call-row span:first-child", css)


def test_the_timeline_says_there_are_no_calls_yet_until_a_call_arrives() -> None:
    assert re.search(r'<p\s+id="timeline-status">No calls yet\.</p>', _RUN_DETAIL_HTML)
    source = function_source(dashboard_assets.APP_JS, "renderTimeline")
    assert 'document.getElementById("timeline-status").hidden = calls.length > 0;' in source
    assert 'document.getElementById("timeline-wrap").hidden = calls.length === 0;' in source


def test_the_script_names_the_same_token_classes_and_cost_units_as_the_server() -> None:
    from software_agent_factory.dashboard.aggregate import COST_UNIT_FIELDS, TOKEN_CLASS_FIELDS

    assert re.findall(r'key: "([a-z_]+_tokens)"', _constant_source("TOKEN_CLASSES")) == list(
        TOKEN_CLASS_FIELDS
    )
    assert re.findall(r'key: "([a-z_]+)"', _constant_source("COST_UNITS")) == list(COST_UNIT_FIELDS)


def test_each_cost_unit_has_its_own_phrase_and_a_one_line_help_text() -> None:
    units = _constant_source("COST_UNITS")
    assert units.count("help:") == 3
    assert units.count("phrase:") == 3
    assert '" USD AI usage"' in units
    assert '" USD list price"' in units
    assert "premium request" in function_source(dashboard_assets.APP_JS, "premiumRequestsPhrase")
    for help_text in re.findall(r'help:\s*"([^"]+)"', units):
        assert help_text.endswith(".")
        assert help_text.count(".") == 1


def test_a_call_row_wires_the_default_columns_from_the_call_fields() -> None:
    cells = function_source(dashboard_assets.APP_JS, "callCells")
    for wired in (
        "displayValue(call.invocation_number)",
        "displayValue(call.role)",
        "displayValue(call.model)",
        "outcomeOf(call.status).text",
        "durationText(call.duration_ms)",
        "displayNumber(call.total_tokens)",
        "costText(usage)",
    ):
        assert wired in cells


def test_an_expanded_call_shows_purpose_reasoning_start_five_token_classes_and_the_reason() -> None:
    js = dashboard_assets.APP_JS
    fields = function_source(js, "callFields")
    for wired in (
        '"Purpose", displayValue(call.purpose, NOT_REPORTED)',
        '"Reasoning level", displayValue(call.reasoning, NOT_REPORTED)',
        '"Started", displayValue(call.started_at, NOT_REPORTED)',
        "TOKEN_CLASSES.map(",
        "displayNumber(usage[tokenClass.key])",
        "reasonFields(call.failure_reason, call.failure_reason_truncated, runId)",
    ):
        assert wired in fields


def test_an_outcome_shows_its_text_next_to_its_color() -> None:
    js = dashboard_assets.APP_JS
    outcomes = _constant_source("OUTCOMES")
    assert 'SUCCESS", { text: "success", className: "status-ok"' in outcomes
    assert 'FAILED", { text: "failed", className: "status-error"' in outcomes
    assert '"running", { text: "running", className: "status-active"' in outcomes
    assert "UNREPORTED_OUTCOME = { text: NOT_REPORTED" in js
    css = dashboard_assets.STYLE_CSS
    for name, token in (
        ("status-ok", "--ok"),
        ("status-error", "--error"),
        ("status-warn", "--warn"),
        ("status-active", "--accent"),
    ):
        assert re.search(rf"\.{name}\s*\{{[^}}]*color:\s*var\({token}\);", css)


def test_the_script_names_an_outcome_for_every_status_the_server_sends() -> None:
    from software_agent_factory.dashboard.aggregate import STATUS_FAILED, STATUS_SUCCESS
    from software_agent_factory.dashboard.sanitize import ACTIVE_INVOCATION_STATUSES

    keys = re.findall(r'\["([A-Za-z]+)", \{', _constant_source("OUTCOMES"))

    assert sorted(keys) == sorted({STATUS_SUCCESS, STATUS_FAILED, *ACTIVE_INVOCATION_STATUSES})


def test_a_total_cost_and_a_call_cost_skip_the_unreported_units() -> None:
    cost = function_source(dashboard_assets.APP_JS, "costText")
    assert "isFiniteNumber(usage[unit.key])" in cost
    assert 'join(", ")' in cost
    assert "return NOT_REPORTED;" in cost
    # The server adds the token classes up, so the script has no sum of its own.
    assert "totalTokens" not in dashboard_assets.APP_JS


def test_totals_name_the_calls_that_reported_a_partial_figure() -> None:
    js = dashboard_assets.APP_JS
    note = function_source(js, "partialNote")
    assert 'reported + " of " + calls + " calls reported"' in note
    assert "reported === 0" in note
    assert "reported >= calls" in note
    card = function_source(js, "figureCard")
    assert "isFiniteNumber(total) ? format(total) : NOT_REPORTED" in card
    assert "partialNote(figure?.reported_count, calls)" in card


def test_totals_cards_cover_calls_failures_duration_tokens_and_each_cost_unit() -> None:
    js = dashboard_assets.APP_JS
    cards = " ".join(
        function_source(js, name)
        for name in ("totalsCards", "headlineCards", "tokenCards", "costCards")
    )
    for wired in (
        'label: "Calls"',
        'figureCard("Failed calls", totals.failed_calls',
        'figureCard("Duration", totals.duration_ms',
        "totals.tokens?.[tokenClass.key]",
        "totals.costs?.[unit.key]",
        "help: unit.help",
    ):
        assert wired in cards
    assert function_source(js, "totalsCards") == (
        "function totalsCards(totals) { return [...headlineCards(totals), "
        "...tokenCards(totals), ...costCards(totals)]; }"
    )


def test_the_run_detail_render_patches_every_region_in_place() -> None:
    source = function_source(dashboard_assets.APP_JS, "renderRunDetail")
    for call in (
        'syncStats(document.getElementById("run-totals"), totalsCards(detail.totals || {}))',
        "renderTimeline(detail, runId)",
        "renderNextStep(detail, runId)",
        "reasonFields(detail.failure_reason, detail.failure_reason_truncated, runId)",
    ):
        assert call in source
    for clearing in ("clearChildren(", "replaceChildren(", "innerHTML"):
        assert clearing not in source
        assert clearing not in function_source(dashboard_assets.APP_JS, "renderTimeline")


def test_an_expanded_call_stays_open_across_a_refresh() -> None:
    js = dashboard_assets.APP_JS
    patch = function_source(js, "patchCall")
    # Only a different call number closes the row; the same node is reused otherwise.
    assert 'const key = runId + "/" + displayValue(call.invocation_number);' in patch
    assert (
        "if (details.dataset.call !== key) { details.dataset.call = key; details.open = false; }"
        in patch
    )
    timeline = function_source(js, "renderTimeline")
    assert "callAt(body, index)" in timeline
    assert "trimChildren(body, calls.length)" in timeline
    assert '"details"' in function_source(js, "buildCall")
    assert '"summary"' in function_source(js, "buildCall")


def test_the_running_call_joins_the_timeline_after_the_finished_calls() -> None:
    timeline = function_source(dashboard_assets.APP_JS, "renderTimeline")
    assert "asArray(detail.invocations)" in timeline
    assert "detail.active_invocation" in timeline


def test_a_cut_reason_is_marked_and_names_factory_show() -> None:
    js = dashboard_assets.APP_JS
    fields = function_source(js, "reasonFields")
    assert 'const fields = [["Failure reason", reason]];' in fields
    assert "truncated === true" in fields
    assert '"Reason was cut"' in fields
    note = function_source(js, "cutNote")
    assert "factory show" in note
    assert '"<run>"' in note


@pytest.mark.parametrize(
    "kind_wiring",
    [
        ('step.kind !== "none"', "isStepVisible"),
        ("resumeClassificationLine(step)", "nextStepSections"),
        ("reopensLine(step)", "nextStepSections"),
        ("approvalScopeSection(step.approval_scope)", "nextStepSections"),
        ("decisionsSection(step.decisions)", "nextStepSections"),
        ("failureSection(step, runId)", "nextStepSections"),
        ("commentLinkLine(step.comment_url)", "nextStepSections"),
        ("replySection(step.reply_text)", "nextStepSections"),
    ],
)
def test_the_needs_you_panel_renders_each_part_of_the_next_step(
    kind_wiring: tuple[str, str],
) -> None:
    wired, function = kind_wiring
    assert wired in function_source(dashboard_assets.APP_JS, function)


def test_no_needs_you_panel_is_shown_for_a_run_that_needs_nothing() -> None:
    source = function_source(dashboard_assets.APP_JS, "renderNextStep")
    assert "panel.hidden = !visible;" in source
    assert "clearChildren(body)" in source
    assert re.search(r'<section\s+id="next-step"[^>]*\bhidden>', _RUN_DETAIL_HTML)


def test_the_needs_you_panel_is_rebuilt_only_when_the_step_changes() -> None:
    source = function_source(dashboard_assets.APP_JS, "renderNextStep")
    assert "JSON.stringify([runId, step])" in source
    assert "panel.dataset.signature !== signature" in source


def test_a_plain_object_is_never_an_array() -> None:
    check = function_source(dashboard_assets.APP_JS, "isPlainObject")
    assert "!Array.isArray(value)" in check


def test_the_comment_link_shows_only_for_an_https_url() -> None:
    link = function_source(dashboard_assets.APP_JS, "commentLinkLine")
    assert "!isHttpsUrl(url)" in link
    assert "createLink(url)" in link


def test_the_approval_scope_lists_the_decision_actions_and_conditions() -> None:
    scope = function_source(dashboard_assets.APP_JS, "approvalScopeSection")
    assert "decisionRequestedLine(scope)" in scope
    for wired in (
        '"Authorized actions", scope.authorized_actions',
        '"Excluded actions", scope.unauthorized_actions',
        '"Conditions in force", scope.conditions_in_force',
    ):
        assert wired in scope


def test_plan_decisions_show_as_a_numbered_list() -> None:
    source = function_source(dashboard_assets.APP_JS, "decisionsSection")
    assert 'listSection("Decisions", items.map(' in source
    assert "decision.question" in source
    assert '"ol"' in function_source(dashboard_assets.APP_JS, "listSection")


def test_reopens_show_as_used_of_max() -> None:
    source = function_source(dashboard_assets.APP_JS, "reopensLine")
    assert '"Reopens used " + step.reopens_used + " of " + step.reopens_max' in source


def test_the_reply_has_a_copy_button_that_announces_copied() -> None:
    js = dashboard_assets.APP_JS
    assert 'const COPIED_TEXT = "Copied";' in js
    control = function_source(js, "copyControl")
    assert 'setAttribute("role", "status")' in control
    assert 'setAttribute("aria-live", "polite")' in control
    assert "copyText(text).then(announceCopied(status), announceCopyFailed(status))" in control
    assert "setText(status, COPIED_TEXT)" in function_source(js, "announceCopied")
    assert "setText(status, COPY_FAILED_TEXT)" in function_source(js, "announceCopyFailed")
    assert "writeText(text)" in function_source(js, "copyText")
    assert '"pre"' in function_source(js, "replySection")


def test_a_run_that_cannot_continue_says_so_and_shows_the_failure_reason() -> None:
    source = function_source(dashboard_assets.APP_JS, "failureSection")
    assert "step.failure_reason" in source
    assert "reasonFields(step.failure_reason, step.failure_reason_truncated, runId)" in source


def test_the_run_attempts_table_shows_the_failure_reason() -> None:
    assert '<th scope="col">Failure reason</th>' in _RUN_DETAIL_HTML
    assert "attempt.failure_reason" in function_source(dashboard_assets.APP_JS, "attemptRowSpec")


def test_the_timeline_scrolls_inside_its_card() -> None:
    assert re.search(r'<div\s+class="table-wrap"\s+id="timeline-wrap"\s+hidden>', _RUN_DETAIL_HTML)
    css = dashboard_assets.STYLE_CSS
    assert re.search(r"\.timeline\s*\{[^}]*min-width:\s*\d+rem;", css)
    assert re.search(r"\.call-row\s*\{[^}]*display:\s*grid;", css)


@pytest.mark.parametrize(
    "old", ["Invocations", "Active invocation", "Usage reported", "invocations yet"]
)
def test_the_page_says_call_where_the_wire_says_invocation(old: str) -> None:
    assert old not in strip_comments(dashboard_assets.APP_JS)


def test_the_run_detail_and_project_labels_say_call() -> None:
    js = dashboard_assets.APP_JS
    assert '["Calls", detail.invocation_count]' in function_source(js, "identityFields")
    assert '["Active call", activeInvocationText(detail.active_invocation)]' in function_source(
        js, "identityFields"
    )
    assert '["Calls with usage", usage.reported_invocations]' in function_source(js, "usageFields")
    assert '"No calls yet."' in function_source(js, "projectCard")


# --------------------------------------------------------------------------
# Approve and answer from the run detail (slice 4 of #80, ADR-033)
# --------------------------------------------------------------------------

_JS = dashboard_assets.APP_JS


def _plain_strings(literal: str) -> dict[str, str]:
    """The ``key: "text"`` pairs of an object literal, with numeric or word keys."""
    return dict(re.findall(r'(\w+): "([^"]+)"', literal))


def test_each_status_has_the_message_the_plan_names() -> None:
    assert _plain_strings(object_literal_source(_JS, "STATUS_MESSAGES")) == {
        "401": "dashboard restarted, reload the page",
        "403": "open the dashboard from the link it printed",
        "404": "this run no longer exists",
    }


def test_each_conflict_reason_has_the_message_the_plan_names() -> None:
    literal = object_literal_source(_JS, "CONFLICT_MESSAGES")
    assert _plain_strings(literal) == {
        "existing_request": "already approved",
        "reopen_limit": "reopen limit reached",
        "wrong_action": "this run needs a different action, reload the page",
        "expired": "approval expired, approve again",
    }
    run_changed = {"stale_episode", "stale_fingerprint", "not_waiting"}
    assert set(re.findall(r"(\w+): RUN_CHANGED_TEXT", literal)) == run_changed
    assert 'const RUN_CHANGED_TEXT = "the run changed, review again";' in _JS


def test_answers_say_answers_where_the_conflict_message_names_the_action() -> None:
    literal = object_literal_source(_JS, "ANSWER_CONFLICT_MESSAGES")
    assert "...CONFLICT_MESSAGES" in literal
    assert _plain_strings(literal) == {
        "existing_request": "answers already sent",
        "expired": "answers expired, send them again",
    }
    pick = function_source(_JS, "conflictMessages")
    assert "action === ANSWER_ACTION ? ANSWER_CONFLICT_MESSAGES : CONFLICT_MESSAGES" in pick
    assert 'const ANSWER_ACTION = "answer";' in _JS
    assert "action: ANSWER_ACTION" in function_source(_JS, "sendAnswers")


def test_the_page_maps_every_conflict_reason_the_server_sends() -> None:
    literal = object_literal_source(_JS, "CONFLICT_MESSAGES")
    keys = set(re.findall(r"(\w+): ", literal))
    assert keys == set(get_args(ConflictReason))


def test_any_other_status_and_a_failed_network_say_the_request_failed() -> None:
    assert 'const FAILURE_TEXT = "the request failed, try again";' in _JS
    lookup = function_source(_JS, "lookupMessage")
    assert lookup == (
        "function lookupMessage(table, key) { "
        "return Object.hasOwn(table, key) ? table[key] : FAILURE_TEXT; }"
    )
    post = function_source(_JS, "postAction")
    assert "return { status: 0, body: {} };" in post


def test_a_400_names_the_decision_and_a_409_uses_its_reason() -> None:
    message = function_source(_JS, "failureMessage")
    assert "outcome.status === CONFLICT_STATUS" in message
    assert "lookupMessage(conflictMessages(action), outcome.body.reason)" in message
    assert (
        "outcome.status === BAD_REQUEST_STATUS && isDecisionNumber(outcome.body.decision)"
        in message
    )
    assert '"decision " + outcome.body.decision + ": use " + ANSWER_HINT' in message
    assert "lookupMessage(STATUS_MESSAGES, outcome.status)" in message


def test_the_status_codes_and_the_close_value_are_named_constants() -> None:
    for line in (
        "const ACCEPTED_STATUS = 202;",
        "const BAD_REQUEST_STATUS = 400;",
        "const CONFLICT_STATUS = 409;",
        'const CLOSED_ACCEPTED = "accepted";',
    ):
        assert line in _JS
    for name in ("failureMessage", "focusAfterFailure", "onActionRejected", "sendAction"):
        assert not re.search(r"status === \d{3}", function_source(_JS, name))
    assert '"accepted"' not in function_source(_JS, "onActionAccepted")


def test_the_answer_limit_matches_the_server_rule() -> None:
    assert "const MIN_ANSWER_CHARS = 1;" in _JS
    assert f"const MAX_ANSWER_CHARS = {MAX_PLAN_DECISION_ANSWER_CHARS};" in _JS
    assert "no paths, links or secrets" in _JS


def test_a_write_carries_the_token_header_a_json_body_and_the_encoded_run_id() -> None:
    post = function_source(_JS, "postAction")
    assert 'method: "POST"' in post
    assert f'"{TOKEN_HEADER}": token' in post
    assert '"Content-Type": "application/json"' in post
    assert "body: JSON.stringify(payload)" in post
    assert '"/api/runs/" + encodeURIComponent(runId) + "/" + action' in post
    assert "signal: controller.signal" in post


def test_the_request_body_names_the_episode_and_the_fingerprint_the_panel_showed() -> None:
    payload = function_source(_JS, "contextPayload")
    assert "episode_id: step.episode_id" in payload
    assert "context_fingerprint: step.context_fingerprint" in payload
    assert "...contextPayload(step)" in function_source(_JS, "sendAnswers")
    assert "payload: contextPayload(step)" in function_source(_JS, "buildApproveDialog")


def test_a_button_is_disabled_while_the_request_runs() -> None:
    send = function_source(_JS, "sendAction")
    assert send.index("setDisabled(submission.controls, true)") < send.index("postAction(")
    rejected = function_source(_JS, "onActionRejected")
    assert "setDisabled(submission.controls, false)" in rejected
    assert "setDisabled(" not in function_source(_JS, "onActionAccepted")


def test_nothing_is_sent_before_the_operator_confirms() -> None:
    build = function_source(_JS, "buildApproveDialog")
    confirm = listener_source(_JS, "buildApproveDialog", "confirm", "click")
    cancel = listener_source(_JS, "buildApproveDialog", "cancel", "click")
    assert "sendAction(" in confirm
    assert build.count("sendAction(") == confirm.count("sendAction(") == 1
    assert "sendAction" not in cancel
    opener = function_source(_JS, "approveSection")
    assert "sendAction" not in opener
    assert "dialog.showModal()" in opener


def test_answers_are_sent_only_when_the_form_is_submitted() -> None:
    section = function_source(_JS, "answerSection")
    submit = listener_source(_JS, "answerSection", "form", "submit")
    typing = listener_source(_JS, "answerSection", "form", "input")
    assert "sendAnswers(" in submit
    assert section.count("sendAnswers(") == submit.count("sendAnswers(") == 1
    assert "sendAction" not in section
    assert "sendAnswers" not in typing
    assert "sendAction" not in typing


def test_the_approve_dialog_is_a_labelled_native_modal_that_lists_the_scope() -> None:
    build = function_source(_JS, "buildApproveDialog")
    assert 'element("dialog", "approve-dialog")' in build
    assert 'dialog.setAttribute("aria-labelledby", APPROVE_TITLE_ID)' in build
    fill = function_source(_JS, "fillApproveDialog")
    assert (
        'dialog.appendChild(element("h2", "", "Approve this run?")).id = APPROVE_TITLE_ID' in fill
    )
    assert '"Agent work starts on this run once you confirm."' in fill
    assert "decisionRequestedLine(actions)" in fill
    assert 'listSection("Authorized actions", actions.authorized_actions)' in fill
    assert 'listSection("Excluded actions", actions.unauthorized_actions)' in fill
    assert 'listSection("Conditions in force", actions.conditions_in_force)' in fill
    assert '"Decision requested: " + displayValue(scope.decision_requested)' in function_source(
        _JS, "decisionRequestedLine"
    )
    assert 'const APPROVE_TITLE_ID = "approve-dialog-title";' in _JS


def test_focus_moves_into_the_dialog_and_returns_to_approve_on_escape_or_cancel() -> None:
    build = function_source(_JS, "buildApproveDialog")
    assert "cancel.autofocus = true;" in build
    assert 'cancel.addEventListener("click", function () { dialog.close(); });' in build
    assert (
        'dialog.addEventListener("close", function () { if (dialog.returnValue === "") {' in build
    )
    assert "opener.focus();" in build
    # Opening resets the return value, so an earlier failure cannot hold focus back.
    opener = function_source(_JS, "approveSection")
    assert opener.index('dialog.returnValue = "";') < opener.index("dialog.showModal()")


def test_an_error_closes_the_dialog_without_taking_focus_from_the_message() -> None:
    assert 'const FOCUS_ON_MESSAGE = "message";' in _JS
    rejected = function_source(_JS, "onActionRejected")
    assert "submission.dialog?.close(FOCUS_ON_MESSAGE)" in rejected
    assert rejected.index("close(FOCUS_ON_MESSAGE)") < rejected.index("focusAfterFailure(")


def test_after_an_error_focus_moves_to_the_named_field_or_to_the_message() -> None:
    focus = function_source(_JS, "focusAfterFailure")
    assert "outcome.status === BAD_REQUEST_STATUS" in focus
    assert "submission.fields?.[outcome.body.decision - 1]" in focus
    assert '(field || document.getElementById("notice")).focus()' in focus


def test_results_go_in_the_one_status_region_and_a_connection_notice_wins() -> None:
    assert "state.actionMessage = text; renderNotice();" in function_source(_JS, "setActionMessage")
    message = function_source(_JS, "noticeMessage")
    assert message.index("ERROR_UNAUTHORIZED") < message.index("return state.actionMessage")
    assert message.index("ERROR_CONNECTION") < message.index("return state.actionMessage")


def test_an_accepted_request_draws_the_panel_again_and_moves_focus_to_its_sentence() -> None:
    accepted = function_source(_JS, "onActionAccepted")
    assert "state.draft = null;" in accepted
    assert "document.activeElement?.blur();" in accepted
    assert "submission.dialog?.close(CLOSED_ACCEPTED);" in accepted
    assert "refreshView().then(focusQueuedSentence)" in accepted
    sentence = function_source(_JS, "buildNextStep")
    assert "sentence.tabIndex = -1;" in sentence


def test_a_conflict_draws_the_panel_again_so_the_operator_can_review() -> None:
    rejected = function_source(_JS, "onActionRejected")
    assert "if (outcome.status === CONFLICT_STATUS) { void refreshView(); }" in rejected


def test_the_answer_form_has_a_labelled_one_line_field_for_each_decision() -> None:
    field = function_source(_JS, "answerField")
    assert (
        'element("label", "", "Decision " + displayValue(decision.number) + ": " + question)'
        in field
    )
    assert "label.htmlFor = input.id;" in field
    assert 'input.type = "text";' in field
    assert "input.maxLength = MAX_ANSWER_CHARS;" in field
    assert 'input.setAttribute("aria-describedby", hint.id);' in field
    assert 'element("p", "field-hint", ANSWER_HINT)' in field
    assert (
        'const ANSWER_HINT = MIN_ANSWER_CHARS + " to " + MAX_ANSWER_CHARS + '
        '" characters, one line, no paths, links or secrets";'
    ) in normalized(_JS)
    section = function_source(_JS, "answerSection")
    assert 'form.setAttribute("aria-labelledby", ANSWER_TITLE_ID);' in section
    assert "asArray(step.decisions).entries()" in section


def test_submit_waits_until_every_answer_is_filled_in() -> None:
    ready = function_source(_JS, "answersReady")
    assert "inputs.every(" in ready
    assert "input.value.trim().length" in ready
    assert "length >= MIN_ANSWER_CHARS && length <= MAX_ANSWER_CHARS" in ready
    section = function_source(_JS, "answerSection")
    assert "submit.disabled = !answersReady(inputs);" in section
    assert section.count("submit.disabled = !answersReady(inputs);") == 2
    assert 'form.addEventListener("input", function () {' in section
    assert "if (answersReady(inputs)) { sendAnswers(step, runId, submit, inputs); }" in section
    assert "event.preventDefault();" in section


def test_typed_answers_return_to_a_form_that_is_drawn_again() -> None:
    section = function_source(_JS, "answerSection")
    assert "const typed = typedAnswers(step, runId);" in section
    assert 'typed[index] ?? ""' in section
    assert "rememberAnswers(step, runId, inputs);" in section
    assert "state.draft?.key === draftKey(step, runId)" in function_source(_JS, "typedAnswers")


def test_typed_answers_belong_to_one_run_episode_and_context() -> None:
    key = function_source(_JS, "draftKey")
    assert "JSON.stringify([runId, step.episode_id, step.context_fingerprint])" in key
    assert "key: draftKey(step, runId)" in function_source(_JS, "rememberAnswers")
    assert "typedAnswers(step, runId)" in function_source(_JS, "answerSection")
    assert "rememberAnswers(step, runId, inputs)" in function_source(_JS, "answerSection")


def test_an_answer_that_arrives_after_the_operator_left_only_gives_the_controls_back() -> None:
    on_run = function_source(_JS, "isOnRun")
    assert 'state.view === "run" && state.runId === runId' in on_run
    left = function_source(_JS, "leftTheRun")
    assert "if (isOnRun(submission.runId)) { return false; }" in left
    assert left.index("isOnRun(") < left.index("setDisabled(submission.controls, false)")
    assert "setActionMessage" not in left
    assert ".focus()" not in left
    for handler in ("onActionAccepted", "onActionRejected"):
        source = function_source(_JS, handler)
        assert "if (leftTheRun(submission)) { return; }" in source
        # Nothing that speaks, moves focus or refreshes runs before the guard.
        guard = source.index("leftTheRun(")
        assert not any(word in source[:guard] for word in ("setActionMessage", "focus", "refresh"))


def test_the_decision_list_gives_way_to_the_form_that_asks_each_question() -> None:
    sections = function_source(_JS, "nextStepSections")
    assert 'step.kind === "answer" ? null : decisionsSection(step.decisions)' in sections
    assert "actionSection(step, runId)" in sections
    assert "staleLine(step.stale_sentence)" in sections


def test_an_action_is_offered_only_for_the_kinds_that_have_one() -> None:
    action = function_source(_JS, "actionSection")
    assert 'typeof step.episode_id === "string"' in action
    assert 'typeof step.context_fingerprint === "string"' in action
    assert 'step.kind === "approve"' in action
    assert 'step.kind === "answer"' in action
    assert "queued" not in action
    assert "remote_approval_unavailable" not in action


def test_a_stale_reason_shows_as_its_own_sentence() -> None:
    stale = function_source(_JS, "staleLine")
    assert 'element("p", "stale-sentence", sentence)' in stale
    assert 'typeof sentence === "string"' in stale


def test_leaving_the_view_drops_the_message_and_closes_an_open_dialog() -> None:
    reset = function_source(_JS, "resetActionState")
    assert 'state.actionMessage = "";' in reset
    assert 'document.querySelectorAll("dialog[open]")' in reset
    assert "dialog.close(FOCUS_ON_MESSAGE)" in reset
    assert "resetActionState();" in function_source(_JS, "applyRoute")


def test_the_script_reads_the_token_from_the_page_and_never_from_the_address() -> None:
    assert function_source(_JS, "readToken").count("location") == 0
    assert "location.search" not in _JS


def test_the_dialog_and_the_form_have_styles_and_a_disabled_button_looks_disabled() -> None:
    css = dashboard_assets.STYLE_CSS
    for selector in (".approve-dialog", ".dialog-actions", ".answer-field input", ".field-hint"):
        assert re.search(rf"{re.escape(selector)}\s*\{{", css)
    assert re.search(r"button:disabled\s*\{[^}]*cursor:\s*not-allowed;", css)
    assert re.search(r"input:disabled\s*\{[^}]*cursor:\s*not-allowed;", css)
    assert re.search(r"\.approve-dialog\s*\{[^}]*background:\s*var\(--surface\);", css)
    assert re.search(r"\.approve-dialog\s*\{[^}]*color:\s*var\(--text\);", css)


def test_the_sidebar_no_longer_claims_the_dashboard_is_read_only() -> None:
    assert "Read-only" not in _INDEX_HTML
    assert "no mutation" not in _INDEX_HTML


# --------------------------------------------------------------------------
# Key figures and the "needs you" list (slice 5, step 5.3; asset tests, ADR-016)
# --------------------------------------------------------------------------

_RUNS_HTML = _INDEX_HTML.split('<section id="view-runs"')[1].split("</section>")[0]
_COMPARE_HTML = _INDEX_HTML.split('<section id="view-compare"')[1].split("</section>")[0]
_FIGURE_IDS = ("figure-active", "figure-needs-you", "figure-failed", "figure-tokens")


def test_the_runs_view_holds_one_key_figure_for_each_summary_number() -> None:
    for figure_id in _FIGURE_IDS:
        assert re.search(rf'<p\s+id="{figure_id}"\s+class="stat-value">', _RUNS_HTML)
    labels = re.findall(r'<p class="stat-label">([^<]+)</p>', _RUNS_HTML)
    assert labels == [
        "Active runs",
        "Needs you",
        "Failed runs in the last 24 hours",
        "Tokens in the last 24 hours",
    ]


def test_the_key_figures_come_before_the_totals() -> None:
    assert _RUNS_HTML.index('id="key-figures"') < _RUNS_HTML.index('id="totals-body"')
    assert re.search(
        r'<ul\s+id="key-figures"[^>]*aria-labelledby="key-figures-heading">', _RUNS_HTML
    )
    assert '<h2 id="key-figures-heading">Key figures</h2>' in _RUNS_HTML


def test_each_key_figure_reads_its_summary_field() -> None:
    figures = _constant_source("KEY_FIGURES")
    for figure_id, field in (
        ("figure-active", "summary?.counts?.active"),
        ("figure-needs-you", "summary?.needs_human_count"),
        ("figure-failed", "summary?.failed_last_24h"),
        ("figure-tokens", "summary?.tokens_last_24h"),
    ):
        assert re.search(
            rf'id: "{figure_id}", read: function \(summary\) {{ return {re.escape(field)};', figures
        )


def test_the_key_figure_fields_the_page_reads_are_snapshot_fields() -> None:
    figures = _constant_source("KEY_FIGURES")
    for field in ("needs_human_count", "failed_last_24h", "tokens_last_24h"):
        assert field in MonitoringSnapshot.model_fields
        assert f"summary?.{field};" in figures


def test_key_figures_show_whole_numbers_with_a_thousands_separator() -> None:
    render = function_source(_JS, "renderKeyFigures")
    assert (
        "setText(document.getElementById(figure.id), displayNumber(figure.read(summary)))" in render
    )
    assert 'value.toLocaleString("en-US")' in function_source(_JS, "displayNumber")


def test_an_unknown_token_figure_shows_not_reported_and_not_zero() -> None:
    display = function_source(_JS, "displayNumber")
    assert "if (!isFiniteNumber(value)) { return NOT_REPORTED; }" in display
    assert 'const NOT_REPORTED = "not reported";' in _JS


def test_the_summary_feeds_the_key_figures_and_the_totals() -> None:
    assert function_source(_JS, "renderSummary") == (
        "function renderSummary(summary) { renderKeyFigures(summary); renderTotals(summary); }"
    )
    assert 'apiFetch("/api/summary").then(whenLatest(request, renderSummary))' in function_source(
        _JS, "refreshTotals"
    )


def test_the_needs_you_figure_is_a_real_link_to_the_filtered_list() -> None:
    assert re.search(
        r'<a\s+id="figure-needs-you-link"\s+href="#runs\?filter=needs-you">'
        r"Show runs that need you</a>",
        _RUNS_HTML,
    )
    assert 'const FILTER_NEEDS_YOU = "needs-you";' in _JS


def test_the_route_filter_accepts_only_needs_you() -> None:
    assert function_source(_JS, "routeFilter") == (
        "function routeFilter(query) { "
        'const filter = new URLSearchParams(query).get("filter"); '
        "return filter === FILTER_NEEDS_YOU ? filter : null; }"
    )
    assert 'hash.replace(/^#/, "").split("?")' in function_source(_JS, "parseRoute")


def test_a_filter_reads_one_page_of_the_most_runs_the_server_returns() -> None:
    from software_agent_factory.dashboard.snapshot import MAX_PAGE_LIMIT

    assert f"const MAX_RUNS_LIMIT = {MAX_PAGE_LIMIT};" in _JS
    assert function_source(_JS, "applyRunsFilter") == (
        "function applyRunsFilter(filter) { state.filter = filter; "
        "state.limit = filter === null ? PAGE_SIZE : MAX_RUNS_LIMIT; "
        "if (filter !== null) { state.offset = 0; } }"
    )
    assert "applyRunsFilter(route.filter ?? null);" in function_source(_JS, "applyRoute")
    assert "filter: state.filter" in function_source(_JS, "beginRequest")


def test_runs_that_need_you_come_first_and_a_filter_keeps_only_them() -> None:
    assert function_source(_JS, "needsYou") == (
        "function needsYou(run) { return run.waiting_for_human === true; }"
    )
    assert function_source(_JS, "orderRuns") == (
        "function orderRuns(runs, filter) { const waiting = runs.filter(needsYou); "
        "if (filter !== null) { return waiting; } "
        "return [...waiting, ...runs.filter(function (run) { return !needsYou(run); })]; }"
    )
    assert "orderRuns(asArray(payload.runs), request.filter)" in function_source(_JS, "renderRuns")


def test_a_run_that_needs_you_carries_a_badge_that_is_text_and_not_only_color() -> None:
    assert function_source(_JS, "needsYouCell") == (
        "function needsYouCell(run) { return needsYou(run) ? "
        '{ value: NEEDS_YOU_TEXT, className: "badge-needs-you" } : ""; }'
    )
    assert 'const NEEDS_YOU_TEXT = "Needs you";' in _JS
    assert re.search(
        r"\.badge-needs-you\s*\{[^}]*color:\s*var\(--warn\);", dashboard_assets.STYLE_CSS
    )
    assert re.search(r"\.badge-needs-you\s*\{[^}]*font-weight:\s*700;", dashboard_assets.STYLE_CSS)
    assert '<th scope="col">Attention</th>' in _RUNS_HTML


def test_a_filter_shows_its_own_line_and_a_way_back_and_hides_the_pager() -> None:
    assert re.search(
        r'<p\s+id="runs-filter"\s+hidden>.*?<a href="#runs">Show all runs</a>',
        _RUNS_HTML,
        flags=re.DOTALL,
    )
    assert function_source(_JS, "renderRunsFilter") == (
        "function renderRunsFilter(filter) { "
        'document.getElementById("runs-filter").hidden = filter === null; '
        'document.getElementById("runs-toolbar").hidden = filter !== null; }'
    )
    assert "renderRunsFilter(request.filter);" in function_source(_JS, "renderRuns")
    assert '"No runs need you."' in function_source(_JS, "emptyRunsText")


def test_each_run_row_has_a_compare_with_link_that_makes_that_run_run_a() -> None:
    assert '<th scope="col">Compare</th>' in _RUNS_HTML
    assert function_source(_JS, "compareCell") == (
        'function compareCell(runId) { if (typeof runId !== "string") { return ""; } '
        'return { value: "Compare with\\u2026", '
        'href: COMPARE_HASH + "/" + encodeURIComponent(runId), hidden: " " + runId }; }'
    )
    assert "compareCell(runId)" in function_source(_JS, "runRowSpec")


def test_the_compare_link_names_its_run_for_a_screen_reader_and_survives_a_refresh() -> None:
    patch = function_source(_JS, "patchLinkCell")
    assert 'link.append(element("span"), element("span", "visually-hidden"))' in patch
    assert "setText(link.lastElementChild, entry.hidden)" in patch
    assert 'if (link?.tagName !== "A")' in patch
    assert "patchLinkCell(cell, entry)" in function_source(_JS, "patchCell")


def test_a_click_on_a_link_in_a_run_row_does_not_open_the_run() -> None:
    click = listener_source(_JS, "bindControls", 'document.getElementById("runs-body")', "click")
    assert 'if (row && !event.target.closest("a"))' in click


# --------------------------------------------------------------------------
# Compare view (slice 5, step 5.3; asset tests, ADR-016)
# --------------------------------------------------------------------------


def test_the_compare_view_is_a_pair_of_labelled_pickers_a_status_line_and_a_table() -> None:
    assert "coming soon" not in _COMPARE_HTML
    for letter in ("a", "b"):
        assert re.search(
            rf'<label for="compare-{letter}">Run {letter.upper()}</label>\s*'
            rf'<select id="compare-{letter}"></select>',
            _COMPARE_HTML,
        )
    assert re.search(
        r'<p id="compare-status" role="status" aria-live="polite">Choose two runs to compare.</p>',
        _COMPARE_HTML,
    )
    assert re.search(r'<div id="compare-content" class="card" hidden>', _COMPARE_HTML)
    assert "<caption>The roles of run A and run B side by side</caption>" in _COMPARE_HTML
    assert '<tbody id="compare-body"></tbody>' in _COMPARE_HTML


def test_the_compare_table_names_every_column_and_row_for_a_screen_reader() -> None:
    head = _COMPARE_HTML.split("<thead>")[1].split("</thead>")[0]
    assert '<th scope="col" rowspan="2">Role</th>' in head
    assert re.search(r'<th id="compare-a-head" scope="colgroup" colspan="6">Run A</th>', head)
    assert re.search(r'<th id="compare-b-head" scope="colgroup" colspan="6">Run B</th>', head)
    second_row = head.split("</tr>")[1]
    assert (
        re.findall(r'<th scope="col">([^<]+)</th>', second_row)
        == [
            "Calls",
            "Failed calls",
            "Models",
            "Tokens",
            "Duration",
            "Cost",
        ]
        * 2
    )
    assert "const COMPARE_RUN_COLUMNS = 6;" in _JS
    assert 'head.scope = "row";' in function_source(_JS, "compareRow")


def test_a_picker_option_shows_the_start_time_state_task_and_model_profile() -> None:
    assert function_source(_JS, "runOption") == (
        "function runOption(run) { const runId = runIdOf(run); "
        "const parts = [run.created_at, run.state, run.title || runId]; "
        "if (run.performance_model_profile) { "
        'parts.push("profile " + run.performance_model_profile); } '
        'return { value: runId, label: parts.map(displayValue).join(" | ") }; }'
    )


def test_run_a_is_not_offered_as_run_b() -> None:
    renderer = function_source(_JS, "renderCompareRuns")
    assert "pickerOptions(runs, selection.a, null), selection.a" in renderer
    assert "pickerOptions(runs, selection.b, selection.a)" in renderer
    assert "option.value !== excluded" in function_source(_JS, "pickerOptions")
    assert function_source(_JS, "normalizeSelection") == (
        "function normalizeSelection(a, b) { return { a: a, b: b === a ? null : b }; }"
    )
    assert "return normalizeSelection(a, b);" in function_source(_JS, "readCompareSelection")
    assert "return normalizeSelection(a, b);" in function_source(_JS, "compareSelection")


def test_a_picked_run_missing_from_the_list_keeps_its_option() -> None:
    options = function_source(_JS, "pickerOptions")
    assert "options.unshift({ value: selected, label: selected })" in options


def test_a_picker_is_rebuilt_only_when_its_options_or_pick_changed() -> None:
    picker = function_source(_JS, "renderPicker")
    assert "JSON.stringify([options, selected])" in picker
    assert "select.dataset.signature === signature" in picker
    assert 'select.value = selected ?? "";' in picker
    assert 'optionNode("", PICKER_PLACEHOLDER)' in picker
    assert 'const PICKER_PLACEHOLDER = "Choose a run";' in _JS


def test_a_role_one_run_did_not_use_shows_no_calls_across_that_runs_columns() -> None:
    cells = function_source(_JS, "appendRunCells")
    assert "if (!isPlainObject(entry))" in cells
    assert 'element("td", "no-calls", NO_CALLS_TEXT)).colSpan = COMPARE_RUN_COLUMNS' in cells
    assert 'const NO_CALLS_TEXT = "no calls";' in _JS
    assert re.search(
        r"\.no-calls\s*\{[^}]*color:\s*var\(--text-muted\);", dashboard_assets.STYLE_CSS
    )


def test_each_run_shows_calls_failures_models_tokens_duration_and_cost_in_its_own_cells() -> None:
    cells = function_source(_JS, "appendRunCells")
    order = [
        "textCell(figureText(calls))",
        "textCell(figureText(failed))",
        "textCell(displayValue(joinList(entry.models)))",
        "figureListCell(tokenCards(entry))",
        "textCell(figureText(duration))",
        "figureListCell(costCards(entry))",
    ]
    positions = [cells.index(call) for call in order]
    assert positions == sorted(positions)
    assert "headlineCards(entry)" in cells
    assert "appendRunCells(row, role.a);" in function_source(_JS, "compareRow")
    assert "appendRunCells(row, role.b);" in function_source(_JS, "compareRow")


def test_the_compare_cells_reuse_the_run_detail_figures_and_never_add_cost_units() -> None:
    # One card per cost unit, each from the run's own figure, so no unit is summed.
    assert "totals.costs?.[unit.key]" in function_source(_JS, "costCards")
    cell = function_source(_JS, "figureListCell")
    assert 'card.label + ": " + figureText(card)' in cell
    assert "card.value !== NOT_REPORTED" in cell
    assert "return textCell(NOT_REPORTED);" in cell
    assert "reduce(" not in function_source(_JS, "appendRunCells")
    assert "partialNote(figure?.reported_count, calls)" in function_source(_JS, "figureCard")


def test_a_run_heading_names_the_run_and_its_task() -> None:
    assert function_source(_JS, "runHeading") == (
        "function runHeading(letter, run) { "
        'const title = run?.title ? " \\u2014 " + run.title : ""; '
        'return "Run " + letter + ": " + displayValue(run?.run_id) + title; }'
    )


def test_the_comparison_table_is_rebuilt_only_when_the_comparison_changed() -> None:
    render = function_source(_JS, "renderComparison")
    assert "body.dataset.signature !== signature" in render
    assert "body.replaceChildren(...roles.map(compareRow))" in render
    assert "setCompareStatus(roles.length === 0 ? NO_ROLES_TEXT : null)" in render


def test_compare_routes_carry_up_to_two_run_ids_checked_with_the_run_pattern() -> None:
    assert function_source(_JS, "validCompareId") == (
        "function validCompareId(value) { "
        'return typeof value === "string" && RUN_ID_PATTERN.test(value) ? value : null; }'
    )
    assert "state.compare = compareSelection(route);" in function_source(_JS, "applyRoute")
    assert "compare: state.compare" in function_source(_JS, "beginRequest")
    assert "resetCompareStatus(state.compare);" in function_source(_JS, "applyRoute")


def test_the_pickers_always_load_and_the_comparison_loads_once_both_runs_are_chosen() -> None:
    refresh = function_source(_JS, "refreshCompare")
    assert "const tasks = [refreshCompareRuns(request)];" in refresh
    assert (
        "if (isCompleteSelection(request.compare)) { tasks.push(refreshComparison(request)); }"
        in refresh
    )
    assert "compare: refreshCompare," in object_literal_source(_JS, "REFRESHERS")
    compare_path = function_source(_JS, "refreshComparison")
    assert "encodeURIComponent(request.compare.a)" in compare_path
    assert '"&b=" + encodeURIComponent(request.compare.b)' in compare_path
    assert "whenLatest(request, renderComparison)" in compare_path
    assert 'const query = "limit=" + MAX_RUNS_LIMIT + "&offset=0";' in function_source(
        _JS, "refreshCompareRuns"
    )


def test_a_pick_updates_the_address_without_a_route_change_and_refreshes() -> None:
    pick = function_source(_JS, "onComparePick")
    assert "state.compare = readCompareSelection();" in pick
    assert 'globalThis.history.replaceState(null, "", compareHash(state.compare));' in pick
    assert "location.hash" not in pick
    assert pick.endswith("void refreshView(); }")
    bindings = function_source(_JS, "bindControls")
    for picker in ("compare-a", "compare-b"):
        assert (
            f'document.getElementById("{picker}").addEventListener("change", onComparePick);'
            in bindings
        )
    assert "force" not in function_source(_JS, "refreshView")


def test_the_compare_address_carries_run_a_then_run_b() -> None:
    assert function_source(_JS, "compareHash") == (
        "function compareHash(selection) { if (selection.b !== null) { "
        'return COMPARE_HASH + "/" + encodeURIComponent(selection.a ?? "") + "/" + '
        "encodeURIComponent(selection.b); } "
        "return selection.a === null ? COMPARE_HASH : "
        'COMPARE_HASH + "/" + encodeURIComponent(selection.a); }'
    )
    assert 'const COMPARE_HASH = "#compare";' in _JS


def test_a_response_keeps_its_status_and_body_so_a_404_can_name_the_missing_run() -> None:
    reader = function_source(_JS, "readResponse")
    assert "error.status = response.status;" in reader
    assert "error.body = body;" in reader
    assert "response.json().catch(" in reader
    assert "const NOT_FOUND_STATUS = 404;" in _JS


def test_a_deleted_run_is_named_in_the_status_line_from_the_answer() -> None:
    text = function_source(_JS, "missingRunsText")
    assert 'return "Runs A and B are no longer available.";' in text
    assert 'return "Run A is no longer available.";' in text
    assert 'return b ? "Run B is no longer available." : null;' in text
    assert 'sides.includes("a")' in text
    assert 'sides.includes("b")' in text
    for retired in ("reportMissingRuns", "runExists", "rethrow"):
        assert retired not in _JS


def test_a_failed_comparison_still_reaches_the_refresh_dispatcher() -> None:
    failure = function_source(_JS, "onCompareFailure")
    assert failure.count("throw error;") == 1
    assert failure.endswith("throw error; }; }")
    assert "if (isLatest(request)) {" in failure
    assert (
        "error.status === NOT_FOUND_STATUS ? missingRunsText(error.body?.missing) : null" in failure
    )
    assert "if (gone !== null) { setCompareStatus(gone); }" in failure
    assert "onCompareFailure(request)" in function_source(_JS, "refreshComparison")


def test_a_loaded_table_stays_after_a_failure_that_is_not_a_missing_run() -> None:
    failure = function_source(_JS, "onCompareFailure")
    # Only a table that is still hidden gets the "unavailable" line; a shown one stays.
    assert (
        'else if (document.getElementById("compare-content").hidden) '
        "{ setCompareStatus(COMPARE_UNAVAILABLE_TEXT); }"
    ) in failure
    assert "replaceChildren" not in failure
    assert "clearChildren" not in failure


def test_the_compare_status_message_hides_the_table_and_a_null_shows_it() -> None:
    assert function_source(_JS, "setCompareStatus") == (
        "function setCompareStatus(message) { "
        'const status = document.getElementById("compare-status"); '
        'setText(status, message === null ? "" : message); '
        "status.hidden = message === null; "
        'document.getElementById("compare-content").hidden = message !== null; }'
    )
    assert function_source(_JS, "resetCompareStatus") == (
        "function resetCompareStatus(selection) { "
        "setCompareStatus(isCompleteSelection(selection) ? LOADING_TEXT : CHOOSE_TWO_TEXT); }"
    )


def test_the_compare_view_has_styles_that_use_only_theme_tokens() -> None:
    css = dashboard_assets.STYLE_CSS
    for selector in (".compare-pickers", ".picker label", ".picker select", ".figure-list"):
        assert re.search(rf"{re.escape(selector)}\s*\{{", css)
    select_rule = re.search(r"\.picker select\s*\{([^}]*)\}", css)
    assert select_rule is not None
    assert "background: var(--surface);" in select_rule.group(1)
    assert "color: var(--text);" in select_rule.group(1)


def test_a_focused_picker_skips_only_its_own_redraw_so_the_comparison_still_refreshes() -> None:
    picker = function_source(_JS, "renderPicker")
    assert picker.startswith("function renderPicker(select, options, selected) { ")
    assert "if (select === document.activeElement) { return; }" in picker
    assert picker.index("document.activeElement") < picker.index("select.replaceChildren(")
    assert "isDirty" not in function_source(_JS, "refreshCompare")
    assert "isDirty" not in function_source(_JS, "renderComparison")
    assert 'matches("input, textarea")' in function_source(_JS, "isDirty")


def test_the_compare_pickers_take_only_valid_ids_from_the_page() -> None:
    read = function_source(_JS, "readCompareSelection")
    assert 'validCompareId(document.getElementById("compare-a").value)' in read
    assert 'validCompareId(document.getElementById("compare-b").value)' in read


def test_one_helper_reads_a_run_id_from_a_run_or_a_detail() -> None:
    assert function_source(_JS, "runIdOf") == (
        "function runIdOf(entry) { return preferDefined(entry.run_id, entry.id); }"
    )
    assert re.findall(r"preferDefined\(\w+\.run_id, \w+\.id\)", _JS) == [
        "preferDefined(entry.run_id, entry.id)"
    ]


def test_the_run_list_note_names_the_page_limit_the_server_applies() -> None:
    assert f"from the newest {MAX_PAGE_LIMIT} runs." in _RUNS_HTML


def test_the_compare_status_is_announced_as_a_status() -> None:
    assert re.search(r'<p id="compare-status" role="status" aria-live="polite">', _COMPARE_HTML)


def test_each_run_heading_spans_its_own_group_of_columns() -> None:
    table = _COMPARE_HTML.split("<thead>")[0]
    assert re.findall(r'<colgroup span="(\d+)"></colgroup>', table) == ["1", "6", "6"]
