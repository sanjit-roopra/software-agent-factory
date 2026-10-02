(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 10000;
  const PAGE_SIZE = 20;
  const EMPTY_VALUE = "\u2014";
  const NOT_REPORTED = "not reported";

  // TOKEN_QUERY_PARAM in dashboard/security.py: the query field of the page link.
  const TOKEN_QUERY_PARAM = "token";

  const ERROR_UNAUTHORIZED = "unauthorized";
  const ERROR_CONNECTION = "connection";
  const ERROR_REQUEST = "request";

  const THEME_STORAGE_KEY = "factory-dashboard-theme";
  const THEME_ATTRIBUTE = "data-theme";
  const THEME_LIGHT = "light";
  const THEME_DARK = "dark";
  const DARK_QUERY = "(prefers-color-scheme: dark)";

  const RUN_ID_PATTERN = /^[\w-]{1,128}$/;
  const FILTER_NEEDS_YOU = "needs-you";
  const COMPARE_HASH = "#compare";
  // MAX_PAGE_LIMIT in dashboard/snapshot.py: the most runs one request returns.
  const MAX_RUNS_LIMIT = 100;
  const NOT_FOUND_STATUS = 404;
  const NEEDS_YOU_TEXT = "Needs you";
  const LOADING_TEXT = "Loading\u2026";
  const TITLE_SUFFIX = " \u2014 Factory dashboard";
  const SIMPLE_VIEWS = new Set(["runs", "projects", "health"]);
  const VIEWS = {
    runs: { section: "view-runs", heading: "runs-heading", nav: "runs", label: "Runs" },
    run: { section: "view-run-detail", heading: "run-detail-heading", nav: "runs", label: "Run detail" },
    compare: {
      section: "view-compare", heading: "compare-heading", nav: "compare", label: "Compare"
    },
    projects: {
      section: "view-projects", heading: "projects-heading", nav: "projects", label: "Projects"
    },
    health: { section: "view-health", heading: "health-heading", nav: "health", label: "Health" }
  };

  const TOTALS_EXCLUDED_KEYS = new Set(["health", "runs", "page"]);
  // What a screen reader hears before each cell of a call row. The visible header
  // row is hidden from assistive technology, so the row carries its own labels.
  const CALL_COLUMNS = [
    "Call number", "Role", "Model", "Outcome", "Duration", "Total tokens", "Cost"
  ];
  const OUTCOME_CELL = 3;
  const STAT_PART_CLASSES = ["stat-label", "stat-value", "stat-note", "stat-help"];
  const COPIED_TEXT = "Copied";
  const COPY_FAILED_TEXT = "Copy failed, select the text and copy it.";
  const COPIED_VISIBLE_MS = 2000;
  const MS_PER_SECOND = 1000;
  const SECONDS_PER_MINUTE = 60;
  // Field names match the server's TOKEN_CLASS_FIELDS and COST_UNIT_FIELDS.
  const TOKEN_CLASSES = [
    { key: "input_tokens", label: "Input tokens" },
    { key: "output_tokens", label: "Output tokens" },
    { key: "reasoning_tokens", label: "Reasoning tokens" },
    { key: "cache_read_tokens", label: "Cache read tokens" },
    { key: "cache_write_tokens", label: "Cache write tokens" }
  ];
  // Each cost unit stays in its own unit and is never added to another.
  const COST_UNITS = [
    {
      key: "total_premium_request_cost",
      label: "Premium requests",
      help: "Count of Copilot premium requests the calls reported.",
      phrase: premiumRequestsPhrase
    },
    {
      key: "usage_value_usd",
      label: "AI usage value (USD)",
      help: "Copilot AI usage in USD at 1 credit = 1 cent; your invoice may be lower or zero.",
      phrase: function (value) {
        return usdAmount(value) + " USD AI usage";
      }
    },
    {
      key: "list_price_estimate_usd",
      label: "List-price estimate (USD)",
      help: "Estimate in USD from list prices, not what a provider billed.",
      phrase: function (value) {
        return usdAmount(value) + " USD list price";
      }
    }
  ];
  // SUCCESS and FAILED are STATUS_SUCCESS and STATUS_FAILED in dashboard/aggregate.py.
  // The rest are the liveness statuses in ACTIVE_INVOCATION_STATUSES in dashboard/sanitize.py.
  const OUTCOMES = new Map([
    ["SUCCESS", { text: "success", className: "status-ok" }],
    ["FAILED", { text: "failed", className: "status-error" }],
    ["running", { text: "running", className: "status-active" }],
    ["stale", { text: "stale", className: "status-warn" }],
    ["crashed", { text: "crashed", className: "status-error" }],
    ["abandoned", { text: "abandoned", className: "status-warn" }]
  ]);
  const UNREPORTED_OUTCOME = { text: NOT_REPORTED, className: "" };
  const TASK_HEADERS = [
    "Task", "Title", "State", "Run", "Issue", "Pull request", "Merged commit"
  ];
  const MODEL_HEADERS = [
    "Scope",
    "Role",
    "Model",
    "Purpose",
    "Status",
    "Started",
    "Reported input tokens",
    "Reported output tokens",
    "AI usage value (USD)",
    "Premium requests",
    "List-price estimate"
  ];
  const USAGE_NOTE =
    "Calculated from Copilot-reported nano-AIU at 1 AI credit = $0.01. " +
    "Your invoice charge may be lower or zero when included credits apply. " +
    "Premium requests are a separate legacy metric.";

  function readToken() {
    const meta = document.querySelector('meta[name="factory-dashboard-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  const token = readToken();
  // actionMessage is the result of the last approve or answer. draft holds the
  // answers typed so far and the run, episode and context they were typed for, so
  // a form that is drawn again for the same context gets them back.
  // filter is the run list filter from the hash, and compare holds the ids picked
  // on the Compare view: both come from the route and the pickers, never from the server.
  const state = {
    offset: 0, limit: PAGE_SIZE, runId: null, view: "runs", noticeKind: null,
    actionMessage: "", draft: null, filter: null, compare: { a: null, b: null }
  };
  // Per view: seq numbers the latest request, inFlight counts unfinished ones.
  const requests = Object.fromEntries(Object.keys(VIEWS).map(function (name) {
    return [name, { seq: 0, inFlight: 0 }];
  }));
  let lastSuccessAt = null;
  let lastProjectsSignature = null;

  // ---- Theme -------------------------------------------------------------

  function readStoredTheme() {
    try {
      const stored = globalThis.localStorage.getItem(THEME_STORAGE_KEY);
      return stored === THEME_LIGHT || stored === THEME_DARK ? stored : null;
    } catch {
      // Storage is blocked, so there is no remembered theme.
      return null;
    }
  }

  function storeTheme(theme) {
    try {
      globalThis.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Storage is blocked: the choice lasts for this page view only.
    }
  }

  function otherTheme(theme) {
    return theme === THEME_DARK ? THEME_LIGHT : THEME_DARK;
  }

  function systemTheme() {
    return globalThis.matchMedia(DARK_QUERY).matches ? THEME_DARK : THEME_LIGHT;
  }

  function currentTheme() {
    return document.documentElement.getAttribute(THEME_ATTRIBUTE) || systemTheme();
  }

  function updateThemeToggle() {
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.textContent = "Switch to " + otherTheme(currentTheme()) + " theme";
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute(THEME_ATTRIBUTE, theme);
    updateThemeToggle();
  }

  // Applies the stored theme first thing on start. With no valid stored value
  // the CSS follows the system theme on its own.
  function applyStoredTheme() {
    const initialTheme = readStoredTheme();
    if (initialTheme) {
      document.documentElement.setAttribute(THEME_ATTRIBUTE, initialTheme);
    }
  }

  // ---- Requests ----------------------------------------------------------

  function apiError(kind, message) {
    const error = new Error(message);
    error.kind = kind;
    return error;
  }

  function onNetworkError() {
    throw apiError(ERROR_CONNECTION, "network error");
  }

  // An error answer may carry a body that says more, such as which compared run is gone.
  function onUnreadableBody() {
    return null;
  }

  function readResponse(response) {
    if (response.status === 401) {
      throw apiError(ERROR_UNAUTHORIZED, "unauthorized");
    }
    if (!response.ok) {
      return response.json().catch(onUnreadableBody).then(function (body) {
        const kind = response.status >= 500 ? ERROR_CONNECTION : ERROR_REQUEST;
        const error = apiError(kind, "request failed: " + response.status);
        error.status = response.status;
        error.body = body;
        throw error;
      });
    }
    return response.json().catch(onNetworkError);
  }

  // Rejects with an error whose "kind" tells a restarted server (401) from a
  // lost connection (network failure, timeout or 5xx) and from any other bad
  // request. The timeout covers reading the body too.
  function apiFetch(path) {
    const controller = new AbortController();
    const timer = globalThis.setTimeout(function () {
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    return fetch(path, {
      method: "GET",
      headers: { "X-Factory-Token": token },
      credentials: "same-origin",
      signal: controller.signal
    })
      .then(readResponse, onNetworkError)
      .finally(function () {
        globalThis.clearTimeout(timer);
      });
  }

  function supersede(view) {
    requests[view].seq += 1;
  }

  function beginRequest(view) {
    supersede(view);
    requests[view].inFlight += 1;
    return {
      view: view,
      seq: requests[view].seq,
      offset: state.offset,
      limit: state.limit,
      runId: state.runId,
      filter: state.filter,
      compare: state.compare
    };
  }

  function endRequest(request) {
    requests[request.view].inFlight -= 1;
  }

  function isLatest(request) {
    return requests[request.view].seq === request.seq;
  }

  // Wraps a handler so a response that a newer request for the same view has
  // overtaken is dropped.
  function whenLatest(request, handler) {
    return function (value) {
      if (isLatest(request)) {
        handler(value, request);
      }
    };
  }

  // ---- Notice ------------------------------------------------------------

  // Writes only when the text changed, so a refresh keeps a text selection.
  function setText(node, text) {
    if (node.textContent !== text) {
      node.textContent = text;
    }
  }

  function noticeMessage() {
    if (state.noticeKind === ERROR_UNAUTHORIZED) {
      return "Dashboard restarted, open the new link from factory dashboard.";
    }
    if (state.noticeKind !== ERROR_CONNECTION) {
      return state.actionMessage;
    }
    if (lastSuccessAt === null) {
      return "Connection lost, not updated yet";
    }
    return "Connection lost, updated " + Math.floor((Date.now() - lastSuccessAt) / 1000) + "s ago";
  }

  function renderNotice() {
    setText(document.getElementById("notice"), noticeMessage());
  }

  // Runs when the server answered: after a success, or after a 4xx.
  function onServerReachable() {
    state.noticeKind = null;
    lastSuccessAt = Date.now();
    renderNotice();
  }

  // Only a restarted server or a lost connection changes the notice. Any other
  // HTTP error still proves the server answered. An error without a kind is a
  // bug in this page, not a connection problem.
  function onRefreshFailure(error) {
    if (error.kind === ERROR_UNAUTHORIZED || error.kind === ERROR_CONNECTION) {
      state.noticeKind = error.kind;
      renderNotice();
    } else if (error.kind === ERROR_REQUEST) {
      onServerReachable();
    } else {
      console.error(error);
    }
  }

  // A region is dirty while the operator works in it: a focused field or an
  // open dialog. A refresh must not re-render a dirty region. A picker is not a
  // field here: it only skips its own redraw, so its view keeps refreshing.
  function isDirty(region) {
    const active = document.activeElement;
    if (active && region.contains(active) && active.matches("input, textarea")) {
      return true;
    }
    return region.querySelector("dialog[open]") !== null;
  }

  // ---- Values and DOM helpers --------------------------------------------

  function isFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function isHttpsUrl(value) {
    return typeof value === "string" && value.startsWith("https://");
  }

  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function joinList(value) {
    return Array.isArray(value) ? value.join(", ") : null;
  }

  function preferDefined(value, fallback) {
    return value === undefined ? fallback : value;
  }

  function runIdOf(entry) {
    return preferDefined(entry.run_id, entry.id);
  }

  function displayValue(value, fallback = EMPTY_VALUE) {
    return value === undefined || value === null || value === "" ? fallback : String(value);
  }

  function displayUsd(value) {
    if (!isFiniteNumber(value)) {
      return NOT_REPORTED;
    }
    return "$" + value.toFixed(6);
  }

  // A reported 0 shows as 0; only a missing value shows as not reported.
  function displayNumber(value) {
    if (!isFiniteNumber(value)) {
      return NOT_REPORTED;
    }
    return value.toLocaleString("en-US");
  }

  function clearChildren(node) {
    node.replaceChildren();
  }

  function element(tagName, className, text) {
    const node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function createLink(url) {
    const link = element("a", "", url);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function appendCell(row, value, asLink) {
    const cell = row.appendChild(document.createElement("td"));
    if (asLink && isHttpsUrl(value)) {
      cell.appendChild(createLink(value));
    } else {
      cell.textContent = displayValue(value);
    }
    return cell;
  }

  function tableHeader(labels) {
    const head = document.createElement("thead");
    const row = head.appendChild(document.createElement("tr"));
    for (const label of labels) {
      const th = row.appendChild(element("th", "", label));
      th.scope = "col";
    }
    return head;
  }

  function wrapTable(table) {
    const wrap = element("div", "table-wrap");
    wrap.appendChild(table);
    return wrap;
  }

  // ---- In-place patching -------------------------------------------------

  function trimChildren(parent, count) {
    while (parent.children.length > count) {
      parent.lastElementChild.remove();
    }
  }

  function childAt(parent, index, tagName) {
    return parent.children[index] || parent.appendChild(document.createElement(tagName));
  }

  // Reuses the existing list and item nodes so a refresh patches the text in
  // place instead of rebuilding the list.
  function syncList(container, lines) {
    let list = container.firstElementChild;
    if (list?.tagName !== "UL") {
      clearChildren(container);
      list = container.appendChild(document.createElement("ul"));
    }
    trimChildren(list, lines.length);
    for (const [index, line] of lines.entries()) {
      setText(childAt(list, index, "li"), line);
    }
  }

  // A link entry holds the link text, the address and the text only a screen
  // reader hears after it. The link node stays, so a refresh keeps its focus.
  function patchLinkCell(cell, entry) {
    let link = cell.firstElementChild;
    if (link?.tagName !== "A") {
      clearChildren(cell);
      link = cell.appendChild(element("a"));
      link.append(element("span"), element("span", "visually-hidden"));
    }
    if (link.getAttribute("href") !== entry.href) {
      link.setAttribute("href", entry.href);
    }
    setText(link.firstElementChild, entry.value);
    setText(link.lastElementChild, entry.hidden);
    cell.className = "";
  }

  // An entry is a plain value, an object with a value and a class name, or a
  // link entry.
  function patchCell(cell, entry) {
    if (isPlainObject(entry) && entry.href !== undefined) {
      patchLinkCell(cell, entry);
      return;
    }
    const isObject = entry !== null && typeof entry === "object";
    setText(cell, displayValue(isObject ? entry.value : entry));
    cell.className = isObject ? entry.className : "";
  }

  // A spec holds the cell entries, an optional row class name and an optional
  // run id for the row's data attribute.
  function patchRow(row, spec) {
    row.className = spec.className || "";
    if (spec.runId === undefined) {
      delete row.dataset.runId;
    } else {
      row.dataset.runId = spec.runId;
    }
    trimChildren(row, spec.cells.length);
    for (const [index, entry] of spec.cells.entries()) {
      patchCell(childAt(row, index, "td"), entry);
    }
  }

  function syncRows(tbody, specs) {
    trimChildren(tbody, specs.length);
    for (const [index, spec] of specs.entries()) {
      patchRow(childAt(tbody, index, "tr"), spec);
    }
  }

  function setValueNode(dd, value, asLink) {
    if (!asLink || !isHttpsUrl(value)) {
      setText(dd, displayValue(value));
      return;
    }
    if (dd.firstElementChild?.getAttribute("href") === value) {
      return;
    }
    clearChildren(dd);
    dd.appendChild(createLink(value));
  }

  // A field holds a label, a value and an optional flag that shows the value
  // as a link. The label list is fixed, so the usual refresh patches each dt
  // and dd in place.
  function syncDefinitionList(dl, fields) {
    trimChildren(dl, fields.length * 2);
    for (const [index, field] of fields.entries()) {
      setText(childAt(dl, index * 2, "dt"), field[0]);
      setValueNode(childAt(dl, index * 2 + 1, "dd"), field[1], field[2]);
    }
  }

  // ---- Totals and health -------------------------------------------------

  function fieldLines(key, value) {
    if (Array.isArray(value)) {
      return [key + ": " + value.length + " item(s)"];
    }
    if (value !== null && typeof value === "object") {
      return Object.keys(value).map(function (nestedKey) {
        return key + "." + nestedKey + ": " + displayValue(value[nestedKey]);
      });
    }
    return [key + ": " + displayValue(value)];
  }

  function keyValueLines(data) {
    return Object.keys(data).flatMap(function (key) {
      return fieldLines(key, data[key]);
    });
  }

  function renderKeyValueList(container, data, emptyMessage) {
    if (!data || typeof data !== "object") {
      container.textContent = emptyMessage;
      return;
    }
    syncList(container, keyValueLines(data));
  }

  function checkLine(check) {
    return displayValue(check.name) + " [" + displayValue(check.status) + "]: " +
      displayValue(check.message);
  }

  function renderHealth(summary) {
    const container = document.getElementById("health-body");
    const health = summary.health;
    if (!health) {
      container.textContent = "No health provider configured.";
      return;
    }
    if (Array.isArray(health.checks)) {
      syncList(container, health.checks.map(checkLine));
      return;
    }
    renderKeyValueList(container, health, "No health data available.");
  }

  // Every scalar or one-level-nested field except "health", which has its own
  // view, and "runs" and "page", which the API never includes here.
  function totalsFields(summary) {
    return Object.fromEntries(
      Object.entries(summary || {}).filter(function (entry) {
        return !TOTALS_EXCLUDED_KEYS.has(entry[0]);
      })
    );
  }

  // "metrics.usage" is nested two levels deep, past what the generic one-level
  // flattening descends into, so its estimate (or "not reported") is shown
  // directly on "metrics" as its own row.
  function withListPriceEstimate(metrics) {
    return {
      ...metrics,
      list_price_estimate_usd: displayUsd(metrics.usage?.list_price_estimate_usd)
    };
  }

  function renderTotals(summary) {
    const totals = totalsFields(summary);
    if (totals.metrics && typeof totals.metrics === "object") {
      totals.metrics = withListPriceEstimate(totals.metrics);
    }
    renderKeyValueList(document.getElementById("totals-body"), totals, "No totals available.");
  }

  // ---- Key figures -------------------------------------------------------

  // Each figure reads one field of the summary. The server derives the three
  // 24 hour figures, so the page only formats them.
  const KEY_FIGURES = [
    { id: "figure-active", read: function (summary) { return summary?.counts?.active; } },
    { id: "figure-needs-you", read: function (summary) { return summary?.needs_human_count; } },
    { id: "figure-failed", read: function (summary) { return summary?.failed_last_24h; } },
    { id: "figure-tokens", read: function (summary) { return summary?.tokens_last_24h; } }
  ];

  function renderKeyFigures(summary) {
    for (const figure of KEY_FIGURES) {
      setText(document.getElementById(figure.id), displayNumber(figure.read(summary)));
    }
  }

  function renderSummary(summary) {
    renderKeyFigures(summary);
    renderTotals(summary);
  }

  // ---- Runs view ---------------------------------------------------------

  function staleCell(isStale) {
    if (isStale) {
      return { value: "yes", className: "stale-yes" };
    }
    return { value: "no", className: "" };
  }

  function needsYou(run) {
    return run.waiting_for_human === true;
  }

  // The badge is text, so it does not rely on its color.
  function needsYouCell(run) {
    return needsYou(run) ? { value: NEEDS_YOU_TEXT, className: "badge-needs-you" } : "";
  }

  function compareCell(runId) {
    if (typeof runId !== "string") {
      return { value: "", className: "" };
    }
    return {
      value: "Compare with\u2026",
      href: COMPARE_HASH + "/" + encodeURIComponent(runId),
      hidden: " " + runId
    };
  }

  function runRowSpec(run) {
    const runId = runIdOf(run);
    return {
      runId: runId,
      cells: [
        runId,
        run.source_external_id || run.work_item_id,
        run.state,
        run.review_status,
        run.created_at,
        run.idle_seconds,
        run.attempt_count,
        staleCell(preferDefined(run.is_stale, run.stale)),
        needsYouCell(run),
        compareCell(runId)
      ]
    };
  }

  // Runs that need the operator come first. A filter keeps only those.
  function orderRuns(runs, filter) {
    const waiting = runs.filter(needsYou);
    if (filter !== null) {
      return waiting;
    }
    return [...waiting, ...runs.filter(function (run) {
      return !needsYou(run);
    })];
  }

  function hasMoreRuns(page, shown, total, request) {
    if (typeof page.has_more === "boolean") {
      return page.has_more;
    }
    if (total !== null) {
      return request.offset + request.limit < total;
    }
    return shown >= request.limit;
  }

  // The request holds the offset and limit the page was asked for, so the
  // pager shows what was fetched even if the operator has paged on since.
  function renderRunsPager(page, shown, request) {
    const total = typeof page.total === "number" ? page.total : null;
    const rangeStart = shown === 0 ? 0 : request.offset + 1;
    const totalText = total === null ? "" : " of " + total;
    document.getElementById("runs-page-info").textContent =
      "showing " + rangeStart + "\u2013" + (request.offset + shown) + totalText;
    document.getElementById("runs-prev").disabled = request.offset <= 0;
    document.getElementById("runs-next").disabled = !hasMoreRuns(page, shown, total, request);
  }

  function emptyRunsText(request) {
    if (request.filter !== null) {
      return "No runs need you.";
    }
    return request.offset === 0 ? "No runs yet." : "No runs on this page.";
  }

  function renderRunsStatus(shown, request) {
    const status = document.getElementById("runs-status");
    status.hidden = shown > 0;
    status.textContent = emptyRunsText(request);
    document.querySelector("#view-runs .table-wrap").hidden = shown === 0;
  }

  // A filter shows its own line and hides the pager, as it reads one big page.
  function renderRunsFilter(filter) {
    document.getElementById("runs-filter").hidden = filter === null;
    document.getElementById("runs-toolbar").hidden = filter !== null;
  }

  function renderRuns(payload, request) {
    const runs = orderRuns(asArray(payload.runs), request.filter);
    syncRows(document.getElementById("runs-body"), runs.map(runRowSpec));
    renderRunsPager(payload.page || {}, runs.length, request);
    renderRunsStatus(runs.length, request);
    renderRunsFilter(request.filter);
  }

  function refreshRuns(request) {
    const query =
      "limit=" + encodeURIComponent(request.limit) +
      "&offset=" + encodeURIComponent(request.offset);
    return apiFetch("/api/runs?" + query).then(whenLatest(request, renderRuns));
  }

  function refreshTotals(request) {
    return apiFetch("/api/summary").then(whenLatest(request, renderSummary));
  }

  function refreshHealth(request) {
    return apiFetch("/api/summary").then(whenLatest(request, renderHealth));
  }

  // ---- Projects view -----------------------------------------------------

  function projectMeta(project) {
    return "Delivery: " + displayValue(project.delivery_mode) +
      " | Target: " + displayValue(project.delivery_repository) +
      "#" + displayValue(project.delivery_base_branch) +
      " | Updated: " + displayValue(project.updated_at);
  }

  // Tasks are saved after planning, so an empty list is only expected while
  // the project is still planning.
  function emptyTasksText(projectState) {
    if (projectState === "PLANNING") {
      return "Planning is in progress; tasks are not persisted yet.";
    }
    return "No tasks.";
  }

  function emptyTasksRow(projectState) {
    const row = document.createElement("tr");
    const cell = row.appendChild(element("td", "", emptyTasksText(projectState)));
    cell.colSpan = TASK_HEADERS.length;
    return row;
  }

  function taskRow(task) {
    const row = document.createElement("tr");
    appendCell(row, task.task_id);
    appendCell(row, task.title);
    appendCell(row, task.state);
    appendCell(row, task.run_id);
    appendCell(row, task.issue_url, true);
    appendCell(row, task.pull_request_url, true);
    appendCell(row, task.merge_commit_sha);
    return row;
  }

  function tasksTable(tasks, projectState) {
    const table = document.createElement("table");
    table.appendChild(tableHeader(TASK_HEADERS));
    const body = table.appendChild(document.createElement("tbody"));
    if (tasks.length === 0) {
      body.appendChild(emptyTasksRow(projectState));
    }
    for (const task of tasks) {
      body.appendChild(taskRow(task));
    }
    return table;
  }

  function modelRow(model) {
    const usage = model.usage || {};
    const row = document.createElement("tr");
    appendCell(row, model.scope);
    appendCell(row, model.role);
    appendCell(row, model.model);
    appendCell(row, model.purpose);
    appendCell(row, model.status);
    appendCell(row, model.started_at);
    appendCell(row, displayNumber(usage.input_tokens));
    appendCell(row, displayNumber(usage.output_tokens));
    appendCell(row, displayUsd(usage.usage_value_usd));
    appendCell(row, displayNumber(usage.total_premium_request_cost));
    appendCell(row, displayUsd(usage.list_price_estimate_usd));
    return row;
  }

  function modelsTable(models) {
    const table = document.createElement("table");
    table.appendChild(tableHeader(MODEL_HEADERS));
    const body = table.appendChild(document.createElement("tbody"));
    for (const model of models) {
      body.appendChild(modelRow(model));
    }
    return table;
  }

  function usageSummary(totals) {
    const usageValue = totals?.costs?.usage_value_usd;
    return "AI usage value: " + displayUsd(usageValue?.total) + ". " + USAGE_NOTE;
  }

  function projectCard(project) {
    const models = asArray(project.models);
    const card = element("article", "project-card");
    card.appendChild(element(
      "h3", "", displayValue(project.project_id) + " \u2014 " + displayValue(project.state)
    ));
    card.appendChild(element("p", "project-meta", projectMeta(project)));
    card.appendChild(wrapTable(tasksTable(asArray(project.tasks), project.state)));
    card.appendChild(element("h4", "", "Models used"));
    card.appendChild(element("p", "project-meta", usageSummary(project.totals)));
    if (models.length === 0) {
      card.appendChild(element("p", "", "No calls yet."));
    } else {
      card.appendChild(wrapTable(modelsTable(models)));
    }
    return card;
  }

  function renderProjects(payload) {
    const container = document.getElementById("projects-body");
    clearChildren(container);
    const projects = asArray(payload.projects);
    if (projects.length === 0) {
      container.textContent = "No persisted projects.";
      return;
    }
    for (const project of projects) {
      container.appendChild(projectCard(project));
    }
  }

  // The projects payload is nested and rebuilt wholesale, so an unchanged
  // payload skips the rebuild and leaves the DOM as the operator left it.
  function renderProjectsOnChange(payload) {
    const signature = JSON.stringify(payload);
    if (signature !== lastProjectsSignature) {
      renderProjects(payload);
      lastProjectsSignature = signature;
    }
  }

  function refreshProjects(request) {
    return apiFetch("/api/projects").then(whenLatest(request, renderProjectsOnChange));
  }

  // ---- Run detail view ---------------------------------------------------

  // A message shows in the status line and hides the detail card; null shows
  // the card instead.
  function setRunDetailStatus(message) {
    const status = document.getElementById("run-detail-status");
    status.textContent = message === null ? "" : message;
    status.hidden = message === null;
    document.getElementById("run-detail-content").hidden = message !== null;
  }

  function activeInvocationText(active) {
    if (!active) {
      return null;
    }
    return active.role + " (" + active.model + ") \u2014 " + active.status +
      " since " + active.started_at;
  }

  function identityFields(detail) {
    return [
      ["Run", runIdOf(detail)],
      ["Work item", detail.work_item_id],
      ["GitHub issue", detail.source_external_id],
      ["Title", detail.title],
      ["State", detail.state],
      ["Complexity", detail.complexity],
      ["Risk", detail.risk],
      ["Requested performance mode", detail.requested_performance_mode],
      ["Effective performance mode", detail.effective_performance_mode],
      ["Performance model profile", detail.performance_model_profile],
      ["Risk assessment", detail.risk_assessment_enabled === false ? "disabled" : "enabled"],
      ["Created", detail.created_at],
      ["Updated", detail.updated_at],
      ["Completed", detail.completed_at],
      ["Calls", detail.invocation_count],
      ["Active call", activeInvocationText(detail.active_invocation)]
    ];
  }

  function usageFields(usage) {
    return [
      ["Calls with usage", usage.reported_invocations],
      ["Input tokens", displayNumber(usage.input_tokens)],
      ["Output tokens", displayNumber(usage.output_tokens)],
      ["Reasoning tokens", displayNumber(usage.reasoning_tokens)],
      ["Cache read tokens", displayNumber(usage.cache_read_tokens)],
      ["AI usage value (USD)", displayUsd(usage.usage_value_usd)],
      ["Premium requests", displayNumber(usage.premium_request_cost)],
      ["Nano AIU", usage.total_nano_aiu],
      ["List-price estimate", displayUsd(usage.list_price_estimate_usd)]
    ];
  }

  function isDecisionGuidance(guidance) {
    return guidance.reason_code === "UNRESOLVED_DECISIONS" ||
      guidance.decision_count !== undefined;
  }

  function countField(guidance) {
    if (isDecisionGuidance(guidance)) {
      return ["Decision count", guidance.decision_count];
    }
    return ["Finding count", guidance.finding_count];
  }

  function categoryText(counts) {
    if (!counts) {
      return null;
    }
    return Object.keys(counts).map(function (category) {
      return category + ": " + counts[category];
    }).join(", ");
  }

  function guidanceFields(detail) {
    const guidance = detail.guidance || {};
    return [
      ["Decision status", detail.guidance ? guidance.status : detail.review_status],
      ["What happened", guidance.summary],
      ["Next action", guidance.next_action],
      ["Evidence artifact", guidance.artifact],
      countField(guidance),
      ["Finding IDs", joinList(guidance.finding_ids)],
      ["Finding categories", categoryText(guidance.category_counts)]
    ];
  }

  function verificationText(verification) {
    if (!verification) {
      return null;
    }
    return (verification.passed ? "passed" : "failed") +
      " (" + verification.failed_check_count + "/" + verification.check_count + " failed)";
  }

  function evidenceFields(detail) {
    return [
      ["Verification", verificationText(detail.verification)],
      ["Coverage change", detail.verification?.coverage_change],
      ["Artifacts", joinList(detail.artifacts)]
    ];
  }

  function escalationFields(detail) {
    const escalation = detail.escalation || {};
    const waiting = detail.escalation ? escalation.waiting_for_human : detail.waiting_for_human;
    return [
      ["Escalation status", escalation.status],
      ["Escalation reason", escalation.reason_code],
      ["Resume classification", escalation.resume_classification],
      ["Waiting for human", waiting],
      ["Escalation episode", escalation.episode_number],
      ["Escalation comment", escalation.comment_url, true],
      ["Last authorized responder", escalation.last_responder],
      ["Last authorized action", escalation.last_action],
      ["Resumed", escalation.is_resumed],
      ["Resumed at", escalation.resumed_at]
    ];
  }

  function referenceFields(detail) {
    return [
      ["Commit", detail.commit_sha],
      ["Merged commit", detail.merge_commit_sha],
      ["Pull request", detail.pull_request_url, true]
    ];
  }

  function attemptRowSpec(attempt) {
    return {
      cells: [
        attempt.attempt_number,
        attempt.role,
        attempt.model,
        attempt.budget,
        attempt.triggered_by,
        attempt.outcome,
        attempt.started_at,
        attempt.completed_at,
        { value: attempt.failure_reason, className: "reason-cell" }
      ]
    };
  }

  // ---- Run detail: values ------------------------------------------------

  function premiumRequestsPhrase(value) {
    return displayNumber(value) + (value === 1 ? " premium request" : " premium requests");
  }

  function usdAmount(value) {
    return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  }

  function durationText(ms) {
    if (!isFiniteNumber(ms)) {
      return NOT_REPORTED;
    }
    if (ms < MS_PER_SECOND) {
      return ms + " ms";
    }
    const seconds = Math.round(ms / MS_PER_SECOND);
    return seconds < SECONDS_PER_MINUTE
      ? seconds + " s"
      : Math.floor(seconds / SECONDS_PER_MINUTE) + " min " + (seconds % SECONDS_PER_MINUTE) + " s";
  }

  function outcomeOf(status) {
    return OUTCOMES.get(status) || UNREPORTED_OUTCOME;
  }

  // Every cost unit the call reported, each in its own unit.
  function costText(usage) {
    const phrases = COST_UNITS.filter(function (unit) {
      return isFiniteNumber(usage[unit.key]);
    }).map(function (unit) {
      return unit.phrase(usage[unit.key]);
    });
    if (phrases.length === 0) {
      return NOT_REPORTED;
    }
    return phrases.join(", ");
  }

  function cutNote(runId) {
    return "Run factory show " + (runId || "<run>") + " for the full text.";
  }

  // The reason text, and a note that names `factory show` when it was cut.
  function reasonFields(reason, truncated, runId) {
    if (typeof reason !== "string" || reason === "") {
      return [];
    }
    const fields = [["Failure reason", reason]];
    if (truncated === true) {
      fields.push(["Reason was cut", cutNote(runId)]);
    }
    return fields;
  }

  // ---- Run detail: totals ------------------------------------------------

  function partialNote(reported, calls) {
    if (!isFiniteNumber(reported) || !isFiniteNumber(calls)) {
      return "";
    }
    if (reported === 0 || reported >= calls) {
      return "";
    }
    return reported + " of " + calls + " calls reported";
  }

  function figureCard(label, figure, calls, format) {
    const total = figure?.total;
    return {
      label: label,
      value: isFiniteNumber(total) ? format(total) : NOT_REPORTED,
      note: partialNote(figure?.reported_count, calls),
      help: ""
    };
  }

  // Calls, failed calls and duration, in that order.
  function headlineCards(totals) {
    const calls = totals.calls;
    return [
      { label: "Calls", value: displayNumber(calls), note: "", help: "" },
      figureCard("Failed calls", totals.failed_calls, calls, displayNumber),
      figureCard("Duration", totals.duration_ms, calls, durationText)
    ];
  }

  function tokenCards(totals) {
    return TOKEN_CLASSES.map(function (tokenClass) {
      return figureCard(
        tokenClass.label, totals.tokens?.[tokenClass.key], totals.calls, displayNumber
      );
    });
  }

  function costCards(totals) {
    return COST_UNITS.map(function (unit) {
      const card = figureCard(unit.label, totals.costs?.[unit.key], totals.calls, unit.phrase);
      return { ...card, help: unit.help };
    });
  }

  function totalsCards(totals) {
    return [...headlineCards(totals), ...tokenCards(totals), ...costCards(totals)];
  }

  function buildStat() {
    const card = element("div", "stat");
    for (const className of STAT_PART_CLASSES) {
      card.appendChild(element("p", className));
    }
    return card;
  }

  function statAt(parent, index) {
    return parent.children[index] || parent.appendChild(buildStat());
  }

  // A note or help line shows only while it has text.
  function patchOptional(node, text) {
    setText(node, text);
    node.hidden = text === "";
  }

  function patchStat(card, spec) {
    setText(card.children[0], spec.label);
    setText(card.children[1], spec.value);
    patchOptional(card.children[2], spec.note);
    patchOptional(card.children[3], spec.help);
  }

  function syncStats(container, specs) {
    trimChildren(container, specs.length);
    for (const [index, spec] of specs.entries()) {
      patchStat(statAt(container, index), spec);
    }
  }

  // ---- Run detail: call timeline -----------------------------------------

  function callCells(call) {
    const usage = call.usage || {};
    return [
      displayValue(call.invocation_number),
      displayValue(call.role),
      displayValue(call.model),
      outcomeOf(call.status).text,
      durationText(call.duration_ms),
      displayNumber(call.total_tokens),
      costText(usage)
    ];
  }

  function callFields(call, runId) {
    const usage = call.usage || {};
    return [
      ["Purpose", displayValue(call.purpose, NOT_REPORTED)],
      ["Reasoning level", displayValue(call.reasoning, NOT_REPORTED)],
      ["Started", displayValue(call.started_at, NOT_REPORTED)],
      ...TOKEN_CLASSES.map(function (tokenClass) {
        return [tokenClass.label, displayNumber(usage[tokenClass.key])];
      }),
      ...reasonFields(call.failure_reason, call.failure_reason_truncated, runId)
    ];
  }

  // A cell is a hidden column label followed by its value.
  function buildCell(label) {
    const cell = element("span");
    cell.appendChild(element("span", "visually-hidden", label + ": "));
    cell.appendChild(element("span"));
    return cell;
  }

  // A call is a native details element: the summary holds the default columns,
  // the body holds the rest.
  function buildCall() {
    const details = element("details", "call");
    const summary = details.appendChild(element("summary", "call-row"));
    for (const label of CALL_COLUMNS) {
      summary.appendChild(buildCell(label));
    }
    details.appendChild(element("dl", "call-fields"));
    return details;
  }

  function callAt(parent, index) {
    return parent.children[index] || parent.appendChild(buildCall());
  }

  // The same node is reused for the same call, so an open row stays open
  // across a refresh. A different call in the slot starts closed.
  function patchCall(details, call, runId) {
    const key = runId + "/" + displayValue(call.invocation_number);
    if (details.dataset.call !== key) {
      details.dataset.call = key;
      details.open = false;
    }
    const cells = details.firstElementChild.children;
    for (const [index, text] of callCells(call).entries()) {
      setText(cells[index].lastElementChild, text);
    }
    cells[OUTCOME_CELL].className = outcomeOf(call.status).className;
    syncDefinitionList(details.lastElementChild, callFields(call, runId));
  }

  // Finished calls come sorted by number; the running call comes last.
  function renderTimeline(detail, runId) {
    const active = detail.active_invocation ? [detail.active_invocation] : [];
    const calls = [...asArray(detail.invocations), ...active];
    const body = document.getElementById("timeline-body");
    trimChildren(body, calls.length);
    for (const [index, call] of calls.entries()) {
      patchCall(callAt(body, index), call, runId);
    }
    document.getElementById("timeline-status").hidden = calls.length > 0;
    document.getElementById("timeline-wrap").hidden = calls.length === 0;
  }

  // ---- Run detail: "Needs you" panel -------------------------------------

  function listSection(title, items, ordered) {
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", title));
    const list = section.appendChild(element(ordered ? "ol" : "ul"));
    for (const item of asArray(items)) {
      list.appendChild(element("li", "", item));
    }
    return section;
  }

  function resumeClassificationLine(step) {
    return step.resume_classification
      ? element("p", "", "Resume classification: " + step.resume_classification)
      : null;
  }

  function reopensLine(step) {
    if (!isFiniteNumber(step.reopens_used) || !isFiniteNumber(step.max_reopens)) {
      return null;
    }
    return element("p", "", "Reopens used " + step.reopens_used + " of " + step.max_reopens);
  }

  function decisionRequestedLine(scope) {
    return element("p", "", "Decision requested: " + displayValue(scope.decision_requested));
  }

  function approvalScopeSection(scope) {
    if (!isPlainObject(scope)) {
      return null;
    }
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", "Approval scope"));
    section.appendChild(decisionRequestedLine(scope));
    section.appendChild(listSection("Authorized actions", scope.authorized_actions));
    section.appendChild(listSection("Excluded actions", scope.unauthorized_actions));
    section.appendChild(listSection("Conditions in force", scope.conditions_in_force));
    return section;
  }

  function decisionsSection(decisions) {
    const items = asArray(decisions);
    if (items.length === 0) {
      return null;
    }
    return listSection("Decisions", items.map(function (decision) {
      return displayValue(decision.question);
    }), true);
  }

  function failureSection(step, runId) {
    const fields = reasonFields(step.failure_reason, step.failure_reason_truncated, runId);
    if (fields.length === 0) {
      return null;
    }
    const section = element("div", "next-step-section");
    for (const field of fields) {
      section.appendChild(element("p", "reason-text", field[0] + ": " + field[1]));
    }
    return section;
  }

  function commentLinkLine(url) {
    if (!isHttpsUrl(url)) {
      return null;
    }
    const line = element("p", "", "Comment: ");
    line.appendChild(createLink(url));
    return line;
  }

  function copyText(text) {
    const clipboard = globalThis.navigator?.clipboard;
    if (!clipboard) {
      return Promise.reject(new Error("clipboard unavailable"));
    }
    return clipboard.writeText(text);
  }

  // The status text clears after a moment, so a second copy is announced again.
  function announceCopied(status) {
    return function () {
      setText(status, COPIED_TEXT);
      globalThis.setTimeout(function () {
        setText(status, "");
      }, COPIED_VISIBLE_MS);
    };
  }

  function announceCopyFailed(status) {
    return function () {
      setText(status, COPY_FAILED_TEXT);
    };
  }

  // The status node exists before the click, so a screen reader announces its text.
  function copyControl(text) {
    const row = element("div", "copy-row");
    const button = row.appendChild(element("button", "", "Copy reply"));
    button.type = "button";
    const status = row.appendChild(element("span", "copy-status"));
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    button.addEventListener("click", function () {
      copyText(text).then(announceCopied(status), announceCopyFailed(status));
    });
    return row;
  }

  function replySection(text) {
    if (typeof text !== "string" || text === "") {
      return null;
    }
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", "Reply to continue"));
    section.appendChild(element("pre", "reply-text", text));
    section.appendChild(copyControl(text));
    return section;
  }

  // ---- Run detail: approve and answer ------------------------------------

  // Mirror MAX_PLAN_DECISION_ANSWER_CHARS in resume.py. The server checks again.
  const MIN_ANSWER_CHARS = 1;
  const MAX_ANSWER_CHARS = 500;
  const ANSWER_HINT = MIN_ANSWER_CHARS + " to " + MAX_ANSWER_CHARS +
    " characters, one line, no paths, links or secrets";
  const ACCEPTED_STATUS = 202;
  const BAD_REQUEST_STATUS = 400;
  const CONFLICT_STATUS = 409;
  const ANSWER_ACTION = "answer";
  const FAILURE_TEXT = "the request failed, try again";
  const RUN_CHANGED_TEXT = "the run changed, review again";
  const APPROVE_TITLE_ID = "approve-dialog-title";
  const ANSWER_TITLE_ID = "answer-title";
  // A dialog closed with this value leaves focus to the message, not to Approve.
  const FOCUS_ON_MESSAGE = "message";
  // A dialog closed with this value was closed by an accepted request.
  const CLOSED_ACCEPTED = "accepted";
  // Statuses with their own wording. A 400 names the decision and a 409 uses its
  // reason code, so neither is listed here. Any other status is FAILURE_TEXT.
  const STATUS_MESSAGES = {
    401: "dashboard restarted, open the new link from factory dashboard",
    403: "open the dashboard from the link it printed",
    404: "this run no longer exists"
  };
  // The reason codes of a 409, as dashboard/actions.py sends them.
  const CONFLICT_MESSAGES = {
    existing_request: "already approved",
    reopen_limit: "reopen limit reached",
    wrong_action: "this run needs a different action, reload the page",
    expired: "approval expired, approve again",
    stale_episode: RUN_CHANGED_TEXT,
    stale_fingerprint: RUN_CHANGED_TEXT,
    not_waiting: RUN_CHANGED_TEXT
  };
  // The same for answers: only the two reasons that name the action differ.
  const ANSWER_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "answers already sent",
    expired: "answers expired, send them again"
  };

  function setActionMessage(text) {
    state.actionMessage = text;
    renderNotice();
  }

  function lookupMessage(table, key) {
    return Object.hasOwn(table, key) ? table[key] : FAILURE_TEXT;
  }

  function isDecisionNumber(value) {
    return Number.isInteger(value) && value > 0;
  }

  function conflictMessages(action) {
    return action === ANSWER_ACTION ? ANSWER_CONFLICT_MESSAGES : CONFLICT_MESSAGES;
  }

  function failureMessage(outcome, action) {
    if (outcome.status === CONFLICT_STATUS) {
      return lookupMessage(conflictMessages(action), outcome.body.reason);
    }
    if (outcome.status === BAD_REQUEST_STATUS && isDecisionNumber(outcome.body.decision)) {
      return "decision " + outcome.body.decision + ": use " + ANSWER_HINT;
    }
    return lookupMessage(STATUS_MESSAGES, outcome.status);
  }

  // Every answer, whatever its status, resolves with the status and the JSON
  // body (an object, maybe empty). A network failure or a timeout is status 0.
  function readOutcome(response) {
    return response.json().catch(function () {
      return {};
    }).then(function (body) {
      return { status: response.status, body: isPlainObject(body) ? body : {} };
    });
  }

  function postAction(runId, action, payload) {
    const controller = new AbortController();
    const timer = globalThis.setTimeout(function () {
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    return fetch("/api/runs/" + encodeURIComponent(runId) + "/" + action, {
      method: "POST",
      headers: { "X-Factory-Token": token, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
      signal: controller.signal
    })
      .then(readOutcome, function () {
        return { status: 0, body: {} };
      })
      .finally(function () {
        globalThis.clearTimeout(timer);
      });
  }

  function setDisabled(controls, disabled) {
    for (const control of controls) {
      control.disabled = disabled;
    }
  }

  function focusQueuedSentence() {
    document.querySelector("#next-step-body .next-step-sentence")?.focus();
  }

  function isOnRun(runId) {
    return state.view === "run" && state.runId === runId;
  }

  // An answer that arrives after the operator left the run has nobody to tell: it
  // only gives the controls back. The panel is drawn again when they return.
  function leftTheRun(submission) {
    if (isOnRun(submission.runId)) {
      return false;
    }
    setDisabled(submission.controls, false);
    return true;
  }

  // The panel is drawn again from the server, so it shows the queued state. The
  // focused field is released first: a refresh skips a view while one has focus.
  function onActionAccepted(submission) {
    if (leftTheRun(submission)) {
      return;
    }
    state.draft = null;
    document.activeElement?.blur();
    submission.dialog?.close(CLOSED_ACCEPTED);
    void refreshView().then(focusQueuedSentence);
  }

  function focusAfterFailure(submission, outcome) {
    const named = outcome.status === BAD_REQUEST_STATUS;
    const field = named ? submission.fields?.[outcome.body.decision - 1] : undefined;
    (field || document.getElementById("notice")).focus();
  }

  // A 409 means the run moved on, so the panel is drawn again to show how.
  function onActionRejected(submission, outcome) {
    if (leftTheRun(submission)) {
      return;
    }
    setDisabled(submission.controls, false);
    submission.dialog?.close(FOCUS_ON_MESSAGE);
    setActionMessage(failureMessage(outcome, submission.action));
    focusAfterFailure(submission, outcome);
    if (outcome.status === CONFLICT_STATUS) {
      void refreshView();
    }
  }

  // The controls stay disabled while the request runs, and after it was accepted.
  function sendAction(submission) {
    setActionMessage("");
    setDisabled(submission.controls, true);
    const { runId, action, payload } = submission;
    return postAction(runId, action, payload).then(function (outcome) {
      if (outcome.status === ACCEPTED_STATUS) {
        onActionAccepted(submission);
      } else {
        onActionRejected(submission, outcome);
      }
    });
  }

  function contextPayload(step) {
    return { episode_id: step.episode_id, context_fingerprint: step.context_fingerprint };
  }

  function actionButton(label, type) {
    const button = element("button", "", label);
    button.type = type;
    return button;
  }

  function fillApproveDialog(dialog, scope) {
    const actions = isPlainObject(scope) ? scope : {};
    dialog.appendChild(element("h2", "", "Approve this run?")).id = APPROVE_TITLE_ID;
    dialog.appendChild(element("p", "", "Agent work starts on this run once you confirm."));
    dialog.appendChild(decisionRequestedLine(actions));
    dialog.appendChild(listSection("Authorized actions", actions.authorized_actions));
    dialog.appendChild(listSection("Excluded actions", actions.unauthorized_actions));
    dialog.appendChild(listSection("Conditions in force", actions.conditions_in_force));
  }

  // A native modal: focus moves in, Tab stays inside and Escape closes it. Focus
  // goes back to Approve unless a failure sent it to the message.
  function buildApproveDialog(step, runId, opener) {
    const dialog = element("dialog", "approve-dialog");
    dialog.setAttribute("aria-labelledby", APPROVE_TITLE_ID);
    fillApproveDialog(dialog, step.approval_scope);
    const buttons = dialog.appendChild(element("div", "dialog-actions"));
    const confirm = buttons.appendChild(actionButton("Confirm approval", "button"));
    const cancel = buttons.appendChild(actionButton("Cancel", "button"));
    cancel.autofocus = true;
    cancel.addEventListener("click", function () {
      dialog.close();
    });
    confirm.addEventListener("click", function () {
      void sendAction({
        runId: runId,
        action: "approve",
        payload: contextPayload(step),
        controls: [confirm, opener],
        dialog: dialog
      });
    });
    dialog.addEventListener("close", function () {
      if (dialog.returnValue === "") {
        opener.focus();
      }
    });
    return dialog;
  }

  function approveSection(step, runId) {
    const section = element("div", "next-step-section");
    const opener = section.appendChild(actionButton("Approve", "button"));
    const dialog = section.appendChild(buildApproveDialog(step, runId, opener));
    opener.addEventListener("click", function () {
      setActionMessage("");
      dialog.returnValue = "";
      dialog.showModal();
    });
    return section;
  }

  function answersReady(inputs) {
    return inputs.every(function (input) {
      const length = input.value.trim().length;
      return length >= MIN_ANSWER_CHARS && length <= MAX_ANSWER_CHARS;
    });
  }

  // Answers belong to one run, episode and context: a form for another one starts empty.
  function draftKey(step, runId) {
    return JSON.stringify([runId, step.episode_id, step.context_fingerprint]);
  }

  function typedAnswers(step, runId) {
    return state.draft?.key === draftKey(step, runId) ? state.draft.values : [];
  }

  function rememberAnswers(step, runId, inputs) {
    state.draft = {
      key: draftKey(step, runId),
      values: inputs.map(function (input) {
        return input.value;
      })
    };
  }

  // A label and a one-line input, with the limits in text the input points to.
  function answerField(decision, index, initial) {
    const row = element("div", "answer-field");
    const input = element("input");
    const hint = element("p", "field-hint", ANSWER_HINT);
    const question = displayValue(decision.question);
    const label = element("label", "", "Decision " + displayValue(decision.number) + ": " + question);
    input.id = "answer-" + index;
    hint.id = "answer-hint-" + index;
    label.htmlFor = input.id;
    input.type = "text";
    input.maxLength = MAX_ANSWER_CHARS;
    input.autocomplete = "off";
    input.value = initial;
    input.setAttribute("aria-describedby", hint.id);
    row.append(label, input, hint);
    return row;
  }

  function sendAnswers(step, runId, submit, inputs) {
    void sendAction({
      runId: runId,
      action: ANSWER_ACTION,
      payload: {
        ...contextPayload(step),
        answers: inputs.map(function (input) {
          return input.value;
        })
      },
      controls: [submit, ...inputs],
      fields: inputs
    });
  }

  // One field for each decision. Submit waits until every field is filled.
  function answerSection(step, runId) {
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", "Answer the decisions")).id = ANSWER_TITLE_ID;
    const form = section.appendChild(element("form", "answer-form"));
    form.noValidate = true;
    form.setAttribute("aria-labelledby", ANSWER_TITLE_ID);
    const typed = typedAnswers(step, runId);
    for (const [index, decision] of asArray(step.decisions).entries()) {
      form.appendChild(answerField(decision, index, typed[index] ?? ""));
    }
    const inputs = [...form.querySelectorAll("input")];
    const submit = form.appendChild(actionButton("Submit answers", "submit"));
    submit.disabled = !answersReady(inputs);
    form.addEventListener("input", function () {
      rememberAnswers(step, runId, inputs);
      submit.disabled = !answersReady(inputs);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (answersReady(inputs)) {
        sendAnswers(step, runId, submit, inputs);
      }
    });
    return section;
  }

  function staleLine(sentence) {
    return typeof sentence === "string" && sentence !== ""
      ? element("p", "stale-sentence", sentence)
      : null;
  }

  // The context the request must carry is the one the panel was drawn from.
  function actionSection(step, runId) {
    const hasContext = typeof step.episode_id === "string" &&
      typeof step.context_fingerprint === "string";
    if (runId === null || !hasContext) {
      return null;
    }
    if (step.kind === "approve") {
      return approveSection(step, runId);
    }
    return step.kind === "answer" ? answerSection(step, runId) : null;
  }

  // Leaving a view drops its message and closes a dialog that is still open.
  function resetActionState() {
    state.actionMessage = "";
    for (const dialog of document.querySelectorAll("dialog[open]")) {
      dialog.close(FOCUS_ON_MESSAGE);
    }
  }

  // The answer form asks each question itself, so the list is for the other kinds.
  function nextStepSections(step, runId) {
    return [
      staleLine(step.stale_sentence),
      resumeClassificationLine(step),
      reopensLine(step),
      approvalScopeSection(step.approval_scope),
      step.kind === "answer" ? null : decisionsSection(step.decisions),
      actionSection(step, runId),
      failureSection(step, runId),
      commentLinkLine(step.comment_url),
      replySection(step.reply_text)
    ].filter(Boolean);
  }

  function buildNextStep(body, step, runId) {
    clearChildren(body);
    const sentence = body.appendChild(
      element("p", "next-step-sentence", displayValue(step.sentence))
    );
    sentence.tabIndex = -1;
    for (const section of nextStepSections(step, runId)) {
      body.appendChild(section);
    }
  }

  function isStepVisible(step) {
    return isPlainObject(step) && step.kind !== "none";
  }

  // The panel is rebuilt only when the step changes, so a refresh keeps the
  // focused copy button and its "Copied" status.
  function renderNextStep(detail, runId) {
    const panel = document.getElementById("next-step");
    const body = document.getElementById("next-step-body");
    const step = detail.next_step;
    const visible = isStepVisible(step);
    panel.hidden = !visible;
    if (!visible) {
      clearChildren(body);
      delete panel.dataset.signature;
      return;
    }
    const signature = JSON.stringify([runId, step]);
    if (panel.dataset.signature !== signature) {
      panel.dataset.signature = signature;
      buildNextStep(body, step, runId);
    }
  }

  // ---- Run detail: render ------------------------------------------------

  function knownRunId(detail) {
    const runId = runIdOf(detail);
    return typeof runId === "string" && RUN_ID_PATTERN.test(runId) ? runId : null;
  }

  function renderRunDetail(detail) {
    const runId = knownRunId(detail);
    const fields = [
      ...identityFields(detail),
      ...reasonFields(detail.failure_reason, detail.failure_reason_truncated, runId),
      ...usageFields(detail.usage || {}),
      ...guidanceFields(detail),
      ...evidenceFields(detail),
      ...escalationFields(detail),
      ...referenceFields(detail)
    ];
    syncDefinitionList(document.getElementById("run-detail-body"), fields);
    syncRows(
      document.getElementById("attempts-body"), asArray(detail.attempts).map(attemptRowSpec)
    );
    syncStats(document.getElementById("run-totals"), totalsCards(detail.totals || {}));
    renderTimeline(detail, runId);
    renderNextStep(detail, runId);
    setRunDetailStatus(null);
  }

  function runDetailPath(runId) {
    return "/api/runs/" + encodeURIComponent(runId);
  }

  // A failure shows the status line only while no detail has loaded; a loaded
  // card stays visible.
  function refreshRunDetail(request) {
    return apiFetch(runDetailPath(request.runId))
      .then(whenLatest(request, renderRunDetail), function (error) {
        if (isLatest(request) && document.getElementById("run-detail-content").hidden) {
          setRunDetailStatus("Run detail is currently unavailable.");
        }
        throw error;
      });
  }

  // ---- Compare view ------------------------------------------------------

  // Run A, run B and the roles the two runs used, from /api/compare. A role one
  // run did not use has no entry for it. The six cells of a run are Calls, Failed
  // calls, Models, Tokens, Duration and Cost: keep COMPARE_RUN_COLUMNS in step
  // with the table head in the page.
  const COMPARE_RUN_COLUMNS = 6;
  const CHOOSE_TWO_TEXT = "Choose two runs to compare.";
  const COMPARE_UNAVAILABLE_TEXT = "The comparison is currently unavailable.";
  const NO_CALLS_TEXT = "no calls";
  const NO_ROLES_TEXT = "Neither run has made a call yet.";
  const PICKER_PLACEHOLDER = "Choose a run";

  function isCompleteSelection(selection) {
    return selection.a !== null && selection.b !== null;
  }

  // A message shows in the status line and hides the table; null shows the table.
  function setCompareStatus(message) {
    const status = document.getElementById("compare-status");
    setText(status, message === null ? "" : message);
    status.hidden = message === null;
    document.getElementById("compare-content").hidden = message !== null;
  }

  function resetCompareStatus(selection) {
    setCompareStatus(isCompleteSelection(selection) ? LOADING_TEXT : CHOOSE_TWO_TEXT);
  }

  // The list rows carry no model, so an option names the model profile the run
  // was started with. The models each run used show in the table once chosen.
  function runOption(run) {
    const runId = runIdOf(run);
    const parts = [run.created_at, run.state, run.title || runId];
    if (run.performance_model_profile) {
      parts.push("profile " + run.performance_model_profile);
    }
    return { value: runId, label: parts.map((part) => displayValue(part)).join(" | ") };
  }

  // A picked run that the list no longer holds keeps an option, so the picker
  // still shows what was picked.
  function pickerOptions(runs, selected, excluded) {
    const options = runs.map(runOption).filter(function (option) {
      return typeof option.value === "string" && option.value !== excluded;
    });
    if (selected !== null && !options.some(function (option) {
      return option.value === selected;
    })) {
      options.unshift({ value: selected, label: selected });
    }
    return options;
  }

  function optionNode(value, label) {
    const node = element("option", "", label);
    node.value = value;
    return node;
  }

  // Rebuilt only when the options or the pick changed. A picker the operator has
  // open is left alone, and the next refresh after it loses focus catches it up.
  function renderPicker(select, options, selected) {
    if (select === document.activeElement) {
      return;
    }
    const signature = JSON.stringify([options, selected]);
    if (select.dataset.signature === signature) {
      return;
    }
    select.dataset.signature = signature;
    select.replaceChildren(
      optionNode("", PICKER_PLACEHOLDER),
      ...options.map(function (option) {
        return optionNode(option.value, option.label);
      })
    );
    select.value = selected ?? "";
  }

  // Run A is not offered as run B.
  function renderCompareRuns(payload, request) {
    const runs = asArray(payload.runs);
    const selection = request.compare;
    renderPicker(
      document.getElementById("compare-a"), pickerOptions(runs, selection.a, null), selection.a
    );
    renderPicker(
      document.getElementById("compare-b"),
      pickerOptions(runs, selection.b, selection.a),
      selection.b
    );
  }

  function textCell(text) {
    return element("td", "", text);
  }

  function figureText(card) {
    return card.note === "" ? card.value : card.value + " (" + card.note + ")";
  }

  // Only the figures a run reported are listed. With none, the cell says so.
  function figureListCell(cards) {
    const reported = cards.filter(function (card) {
      return card.value !== NOT_REPORTED;
    });
    if (reported.length === 0) {
      return textCell(NOT_REPORTED);
    }
    const cell = element("td");
    const list = cell.appendChild(element("ul", "figure-list"));
    for (const card of reported) {
      list.appendChild(element("li", "", card.label + ": " + figureText(card)));
    }
    return cell;
  }

  // The same figures as the run detail totals, one cell each.
  function appendRunCells(row, entry) {
    if (!isPlainObject(entry)) {
      row.appendChild(element("td", "no-calls", NO_CALLS_TEXT)).colSpan = COMPARE_RUN_COLUMNS;
      return;
    }
    const [calls, failed, duration] = headlineCards(entry);
    row.append(
      textCell(figureText(calls)),
      textCell(figureText(failed)),
      textCell(displayValue(joinList(entry.models))),
      figureListCell(tokenCards(entry)),
      textCell(figureText(duration)),
      figureListCell(costCards(entry))
    );
  }

  function compareRow(role) {
    const row = element("tr");
    const head = row.appendChild(element("th", "", displayValue(role.role)));
    head.scope = "row";
    appendRunCells(row, role.a);
    appendRunCells(row, role.b);
    return row;
  }

  function runHeading(letter, run) {
    const title = run?.title ? " \u2014 " + run.title : "";
    return "Run " + letter + ": " + displayValue(run?.run_id) + title;
  }

  // The table is rebuilt only when the comparison changed.
  function renderComparison(payload) {
    const roles = asArray(payload.roles);
    setText(document.getElementById("compare-a-head"), runHeading("A", payload.a));
    setText(document.getElementById("compare-b-head"), runHeading("B", payload.b));
    const body = document.getElementById("compare-body");
    const signature = JSON.stringify(payload);
    if (body.dataset.signature !== signature) {
      body.dataset.signature = signature;
      body.replaceChildren(...roles.map(compareRow));
    }
    setCompareStatus(roles.length === 0 ? NO_ROLES_TEXT : null);
  }

  // The compare answer names the missing runs by side, "a" or "b". Null when it
  // names none.
  function missingRunsText(missing) {
    const sides = asArray(missing);
    const a = sides.includes("a");
    const b = sides.includes("b");
    if (a && b) {
      return "Runs A and B are no longer available.";
    }
    if (a) {
      return "Run A is no longer available.";
    }
    return b ? "Run B is no longer available." : null;
  }

  // A loaded table stays visible after a failure, unless the answer says a run is
  // gone. The error still goes on, so the refresh dispatcher sets its notice as it
  // does for every view.
  function onCompareFailure(request) {
    return function (error) {
      if (isLatest(request)) {
        const gone = error.status === NOT_FOUND_STATUS ? missingRunsText(error.body?.missing) : null;
        if (gone !== null) {
          setCompareStatus(gone);
        } else if (document.getElementById("compare-content").hidden) {
          setCompareStatus(COMPARE_UNAVAILABLE_TEXT);
        }
      }
      throw error;
    };
  }

  function refreshComparison(request) {
    const path = "/api/compare?a=" + encodeURIComponent(request.compare.a) +
      "&b=" + encodeURIComponent(request.compare.b);
    return apiFetch(path).then(whenLatest(request, renderComparison), onCompareFailure(request));
  }

  function refreshCompareRuns(request) {
    const query = "limit=" + MAX_RUNS_LIMIT + "&offset=0";
    return apiFetch("/api/runs?" + query).then(whenLatest(request, renderCompareRuns));
  }

  // The pickers always load. The comparison loads once both runs are chosen.
  function refreshCompare(request) {
    const tasks = [refreshCompareRuns(request)];
    if (isCompleteSelection(request.compare)) {
      tasks.push(refreshComparison(request));
    }
    return tasks;
  }

  // ---- Routing -----------------------------------------------------------

  // The one filter a hash can ask for, or null for any other query.
  function routeFilter(query) {
    const filter = new URLSearchParams(query).get("filter");
    return filter === FILTER_NEEDS_YOU ? filter : null;
  }

  // Returns null for an empty or unknown hash. The run id is only checked
  // for shape later, so a bad id still opens the run view with "Unknown run".
  // Compare takes up to two ids, run A then run B, checked for shape later too.
  function parseRoute(hash) {
    const [path, query] = hash.replace(/^#/, "").split("?");
    const parts = path.split("/");
    const name = parts[0];
    if (name === "run" && parts.length === 2) {
      return { view: "run", runId: parts[1] };
    }
    if (name === "compare" && parts.length <= 3) {
      return { view: "compare", a: parts[1], b: parts[2] };
    }
    if (parts.length === 1 && SIMPLE_VIEWS.has(name)) {
      return { view: name, filter: routeFilter(query) };
    }
    return null;
  }

  function isUnknownHash(hash) {
    return hash !== "" && hash !== "#" && parseRoute(hash) === null;
  }

  // Free of side effects: an unknown or empty hash resolves to the run list.
  function resolveRoute() {
    return parseRoute(globalThis.location.hash) || { view: "runs" };
  }

  function validRunId(route) {
    return route.view === "run" && RUN_ID_PATTERN.test(route.runId) ? route.runId : null;
  }

  function validCompareId(value) {
    return typeof value === "string" && RUN_ID_PATTERN.test(value) ? value : null;
  }

  // A run is never compared with itself, so a repeated id leaves run B empty.
  function normalizeSelection(a, b) {
    return { a: a, b: b === a ? null : b };
  }

  function compareSelection(route) {
    const a = validCompareId(route.a);
    const b = validCompareId(route.b);
    return normalizeSelection(a, b);
  }

  // The list filter needs the whole newest page, so it asks for the largest one.
  function applyRunsFilter(filter) {
    state.filter = filter;
    state.limit = filter === null ? PAGE_SIZE : MAX_RUNS_LIMIT;
    if (filter !== null) {
      state.offset = 0;
    }
  }

  function markNavLink(link, isActive) {
    if (isActive) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  }

  function showView(name) {
    for (const key of Object.keys(VIEWS)) {
      document.getElementById(VIEWS[key].section).hidden = key !== name;
    }
    for (const link of document.querySelectorAll('nav[aria-label="Main"] a')) {
      markNavLink(link, link.dataset.route === VIEWS[name].nav);
    }
  }

  function routeTitle(route) {
    if (route.view !== "run") {
      return VIEWS[route.view].label + TITLE_SUFFIX;
    }
    return (RUN_ID_PATTERN.test(route.runId) ? "Run " + route.runId : "Unknown run") + TITLE_SUFFIX;
  }

  function prepareRunDetailView(runId) {
    const heading = document.getElementById("run-detail-heading");
    if (!RUN_ID_PATTERN.test(runId)) {
      heading.textContent = VIEWS.run.label;
      setRunDetailStatus("Unknown run");
      return;
    }
    heading.textContent = "Run " + runId;
    setRunDetailStatus("Loading\u2026");
  }

  // What each view refreshes. A run view with an unknown id has no run id in
  // the request and loads nothing.
  const REFRESHERS = {
    runs: function (request) {
      return [refreshRuns(request), refreshTotals(request)];
    },
    run: function (request) {
      return request.runId === null ? [pingServer()] : [refreshRunDetail(request)];
    },
    compare: refreshCompare,
    projects: function (request) {
      return [refreshProjects(request)];
    },
    health: function (request) {
      return [refreshHealth(request)];
    }
  };

  // A view with nothing to load still checks that the server answers, so the
  // connection notice stays true.
  function pingServer() {
    return apiFetch("/healthz");
  }

  // Waits for every task, so the request stays in flight until the last one
  // ends, then rejects with the first failure, or resolves with nothing.
  function rejectOnFirstFailure(tasks) {
    return Promise.allSettled(tasks).then(function (results) {
      const failed = results.find(function (result) {
        return result.status === "rejected";
      });
      return failed ? Promise.reject(failed.reason) : undefined;
    });
  }

  function settle(request, tasks) {
    return rejectOnFirstFailure(tasks)
      .then(whenLatest(request, onServerReachable), whenLatest(request, onRefreshFailure))
      .catch(function () {
        // A failure while drawing the view must not escape as an unhandled
        // rejection; the next poll tries again.
      })
      .finally(function () {
        endRequest(request);
      });
  }

  // Refreshes only the open view, and skips it while the operator works in it.
  // The notice shows the outcome of the latest refresh and clears on success.
  function refreshView() {
    renderNotice();
    const view = state.view;
    const refresher = REFRESHERS[view];
    if (!refresher || isDirty(document.getElementById(VIEWS[view].section))) {
      return Promise.resolve();
    }
    const request = beginRequest(view);
    return settle(request, refresher(request));
  }

  // A poll tick waits while the open view still has a request in flight.
  function pollView() {
    if (requests[state.view].inFlight === 0) {
      void refreshView();
    }
  }

  function applyRoute(moveFocus) {
    if (isUnknownHash(globalThis.location.hash)) {
      globalThis.history.replaceState(null, "", "#runs");
    }
    const route = resolveRoute();
    state.view = route.view;
    state.runId = validRunId(route);
    applyRunsFilter(route.filter ?? null);
    state.compare = compareSelection(route);
    supersede(route.view);
    resetActionState();
    showView(route.view);
    if (route.view === "run") {
      prepareRunDetailView(route.runId);
    }
    if (route.view === "compare") {
      resetCompareStatus(state.compare);
    }
    document.title = routeTitle(route);
    if (moveFocus) {
      document.getElementById(VIEWS[route.view].heading).focus();
    }
    void refreshView();
  }

  // A deep link opens with the run list one step back, so Back leaves the deep
  // view for the list. A tab is seeded once: the session flag keeps a reload,
  // or a reload after in-page navigation, from adding another entry.
  const SEEDED_KEY = "factory-dashboard-seeded";

  function tabWasSeeded() {
    try {
      return globalThis.sessionStorage.getItem(SEEDED_KEY) === "1";
    } catch {
      return globalThis.history.state?.seeded === true;
    }
  }

  function markTabSeeded() {
    try {
      globalThis.sessionStorage.setItem(SEEDED_KEY, "1");
    } catch {
      // Without storage the history marker is the only guard.
    }
  }

  function seedBackHistory() {
    const hash = globalThis.location.hash;
    const route = parseRoute(hash);
    const deepLink = route !== null && route.view !== "runs";
    if (deepLink && !tabWasSeeded()) {
      globalThis.history.replaceState(null, "", "#runs");
      globalThis.history.pushState({ seeded: true }, "", hash);
    }
    markTabSeeded();
  }

  // ---- Wiring ------------------------------------------------------------

  function goToOffset(offset) {
    state.offset = offset;
    void refreshView();
  }

  function navigateToRun(runId) {
    globalThis.location.hash = "run/" + encodeURIComponent(runId);
  }

  function readCompareSelection() {
    const a = validCompareId(document.getElementById("compare-a").value);
    const b = validCompareId(document.getElementById("compare-b").value);
    return normalizeSelection(a, b);
  }

  function compareHash(selection) {
    if (selection.b !== null) {
      return COMPARE_HASH + "/" + encodeURIComponent(selection.a ?? "") + "/" +
        encodeURIComponent(selection.b);
    }
    return selection.a === null ? COMPARE_HASH : COMPARE_HASH + "/" + encodeURIComponent(selection.a);
  }

  // A pick updates the address without a route change, so focus stays on the
  // picker.
  function onComparePick() {
    state.compare = readCompareSelection();
    globalThis.history.replaceState(null, "", compareHash(state.compare));
    resetCompareStatus(state.compare);
    void refreshView();
  }

  function bindControls() {
    document.getElementById("runs-prev").addEventListener("click", function () {
      goToOffset(Math.max(0, state.offset - state.limit));
    });
    document.getElementById("runs-next").addEventListener("click", function () {
      goToOffset(state.offset + state.limit);
    });
    document.getElementById("theme-toggle").addEventListener("click", function () {
      const next = otherTheme(currentTheme());
      applyTheme(next);
      storeTheme(next);
    });
    document.getElementById("runs-body").addEventListener("click", function (event) {
      const row = event.target.closest("tr[data-run-id]");
      // A link in the row goes where it points, not to the run.
      if (row && !event.target.closest("a")) {
        navigateToRun(row.dataset.runId);
      }
    });
    document.getElementById("compare-a").addEventListener("change", onComparePick);
    document.getElementById("compare-b").addEventListener("change", onComparePick);
    globalThis.matchMedia(DARK_QUERY).addEventListener("change", updateThemeToggle);
    updateThemeToggle();
    globalThis.addEventListener("hashchange", function () {
      applyRoute(true);
    });
  }

  // The page link holds the token in its query. The server has set the cookie by now, so
  // the address bar and the current history entry drop the query. The hash stays.
  function stripTokenFromAddress() {
    const hasToken = new URLSearchParams(globalThis.location.search).has(TOKEN_QUERY_PARAM);
    if (hasToken) {
      const { pathname, hash } = globalThis.location;
      globalThis.history.replaceState(null, "", pathname + hash);
    }
  }

  function start() {
    applyStoredTheme();
    stripTokenFromAddress();
    bindControls();
    seedBackHistory();
    applyRoute(false);
    globalThis.setInterval(pollView, POLL_INTERVAL_MS);
  }

  start();
})();
