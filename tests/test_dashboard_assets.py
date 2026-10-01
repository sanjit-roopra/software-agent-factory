"""Asset tests for the dashboard shell, routes, refresh and layout (slice 1 of #80).

No JavaScript runner is available (ADR-016), so these tests read ``app.js``,
``style.css`` and the index page as text.
"""

from __future__ import annotations

import re

import pytest
from dashboard_js import function_source, normalized, object_literal_source, strip_comments

from software_agent_factory.dashboard import assets as dashboard_assets

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
    assert '"No runs yet."' in function_source(js, "renderRunsStatus")
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
    cards = function_source(dashboard_assets.APP_JS, "totalsCards")
    for wired in (
        'label: "Calls"',
        'figureCard("Failed calls", totals.failed_calls',
        'figureCard("Duration", totals.duration_ms',
        "totals.tokens?.[tokenClass.key]",
        "totals.costs?.[unit.key]",
        "help: unit.help",
    ):
        assert wired in cards


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
    for wired in (
        "scope.decision_requested",
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
