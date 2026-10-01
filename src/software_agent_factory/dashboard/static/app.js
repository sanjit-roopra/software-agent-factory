(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 10000;
  const PAGE_SIZE = 20;
  const EMPTY_VALUE = "\u2014";
  const NOT_REPORTED = "not reported";

  const ERROR_UNAUTHORIZED = "unauthorized";
  const ERROR_CONNECTION = "connection";
  const ERROR_REQUEST = "request";

  const THEME_STORAGE_KEY = "factory-dashboard-theme";
  const THEME_ATTRIBUTE = "data-theme";
  const THEME_LIGHT = "light";
  const THEME_DARK = "dark";
  const DARK_QUERY = "(prefers-color-scheme: dark)";

  const RUN_ID_PATTERN = /^[\w-]{1,128}$/;
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
  const CALL_CELL_COUNT = 7;
  const OUTCOME_CELL = 3;
  const STAT_PART_CLASSES = ["stat-label", "stat-value", "stat-note", "stat-help"];
  const COPIED_TEXT = "Copied";
  const COPY_FAILED_TEXT = "Copy failed, select the text and copy it.";
  const COPIED_VISIBLE_MS = 2000;
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
  const state = {
    offset: 0, limit: PAGE_SIZE, runId: null, view: "runs", noticeKind: null
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

  function readResponse(response) {
    if (response.status === 401) {
      throw apiError(ERROR_UNAUTHORIZED, "unauthorized");
    }
    if (!response.ok) {
      const kind = response.status >= 500 ? ERROR_CONNECTION : ERROR_REQUEST;
      throw apiError(kind, "request failed: " + response.status);
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
      runId: state.runId
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
      return "Dashboard restarted, reload the page.";
    }
    if (state.noticeKind !== ERROR_CONNECTION) {
      return "";
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
  // open dialog. A refresh must not re-render a dirty region.
  function isDirty(region) {
    const active = document.activeElement;
    if (active && region.contains(active) && active.matches("input, textarea, select")) {
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
    return value !== null && typeof value === "object";
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

  // An entry is either a plain value or an object with a value and a class name.
  function patchCell(cell, entry) {
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

  // ---- Runs view ---------------------------------------------------------

  function staleCell(isStale) {
    if (isStale) {
      return { value: "yes", className: "stale-yes" };
    }
    return { value: "no", className: "" };
  }

  function runRowSpec(run) {
    const runId = preferDefined(run.run_id, run.id);
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
        staleCell(preferDefined(run.is_stale, run.stale))
      ]
    };
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

  function renderRunsStatus(shown, request) {
    const status = document.getElementById("runs-status");
    status.hidden = shown > 0;
    status.textContent = request.offset === 0 ? "No runs yet." : "No runs on this page.";
    document.querySelector("#view-runs .table-wrap").hidden = shown === 0;
  }

  function renderRuns(payload, request) {
    const runs = asArray(payload.runs);
    syncRows(document.getElementById("runs-body"), runs.map(runRowSpec));
    renderRunsPager(payload.page || {}, runs.length, request);
    renderRunsStatus(runs.length, request);
  }

  function refreshRuns(request) {
    const query =
      "limit=" + encodeURIComponent(request.limit) +
      "&offset=" + encodeURIComponent(request.offset);
    return apiFetch("/api/runs?" + query).then(whenLatest(request, renderRuns));
  }

  function refreshTotals(request) {
    return apiFetch("/api/summary").then(whenLatest(request, renderTotals));
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

  function pendingTasksRow(projectState) {
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
      body.appendChild(pendingTasksRow(projectState));
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
      card.appendChild(element("p", "", "No model invocations yet."));
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
      ["Run", preferDefined(detail.run_id, detail.id)],
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
      ["Invocations", detail.invocation_count],
      ["Active invocation", activeInvocationText(detail.active_invocation)]
    ];
  }

  function usageFields(usage) {
    return [
      ["Usage reported", usage.reported_invocations],
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
    if (ms < 1000) {
      return ms + " ms";
    }
    const seconds = Math.round(ms / 1000);
    return seconds < 60 ? seconds + " s" : Math.floor(seconds / 60) + " min " + (seconds % 60) + " s";
  }

  function outcomeOf(status) {
    return OUTCOMES.get(status) || UNREPORTED_OUTCOME;
  }

  // The sum of the token classes the call reported, or null when it reported none.
  function totalTokens(usage) {
    const reported = TOKEN_CLASSES.map(function (tokenClass) {
      return usage[tokenClass.key];
    }).filter(isFiniteNumber);
    if (reported.length === 0) {
      return null;
    }
    return reported.reduce(function (sum, value) {
      return sum + value;
    }, 0);
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

  function totalsCards(totals) {
    const calls = totals.calls;
    return [
      { label: "Calls", value: displayNumber(calls), note: "", help: "" },
      figureCard("Failed calls", totals.failed_calls, calls, displayNumber),
      figureCard("Duration", totals.duration_ms, calls, durationText),
      ...TOKEN_CLASSES.map(function (tokenClass) {
        return figureCard(tokenClass.label, totals.tokens?.[tokenClass.key], calls, displayNumber);
      }),
      ...COST_UNITS.map(function (unit) {
        const card = figureCard(unit.label, totals.costs?.[unit.key], calls, unit.phrase);
        return { ...card, help: unit.help };
      })
    ];
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
      displayNumber(totalTokens(usage)),
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

  // A call is a native details element: the summary holds the default columns,
  // the body holds the rest.
  function buildCall() {
    const details = element("details", "call");
    const summary = details.appendChild(element("summary", "call-row"));
    for (let index = 0; index < CALL_CELL_COUNT; index += 1) {
      summary.appendChild(element("span"));
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
      setText(cells[index], text);
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

  function resumeClassLine(step) {
    return step.resume_class ? element("p", "", "Resume class: " + step.resume_class) : null;
  }

  function reopensLine(step) {
    if (!isFiniteNumber(step.reopens_used) || !isFiniteNumber(step.reopens_max)) {
      return null;
    }
    return element("p", "", "Reopens used " + step.reopens_used + " of " + step.reopens_max);
  }

  function approvalScopeSection(scope) {
    if (!isPlainObject(scope)) {
      return null;
    }
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", "Approval scope"));
    section.appendChild(
      element("p", "", "Decision requested: " + displayValue(scope.decision_requested))
    );
    section.appendChild(listSection("Authorized actions", scope.authorized_actions));
    section.appendChild(listSection("Excluded actions", scope.excluded_actions));
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

  function nextStepSections(step, runId) {
    return [
      resumeClassLine(step),
      reopensLine(step),
      approvalScopeSection(step.approval_scope),
      decisionsSection(step.decisions),
      failureSection(step, runId),
      commentLinkLine(step.comment_url),
      replySection(step.reply_text)
    ].filter(Boolean);
  }

  function buildNextStep(body, step, runId) {
    clearChildren(body);
    body.appendChild(element("p", "next-step-sentence", displayValue(step.sentence)));
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
    const runId = preferDefined(detail.run_id, detail.id);
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

  // A failure shows the status line only while no detail has loaded; a loaded
  // card stays visible.
  function refreshRunDetail(request) {
    const path = "/api/runs/" + encodeURIComponent(request.runId);
    return apiFetch(path).then(whenLatest(request, renderRunDetail), function (error) {
      if (isLatest(request) && document.getElementById("run-detail-content").hidden) {
        setRunDetailStatus("Run detail is currently unavailable.");
      }
      throw error;
    });
  }

  // ---- Routing -----------------------------------------------------------

  // Returns null for an empty or unknown hash. The run id is only checked
  // for shape later, so a bad id still opens the run view with "Unknown run".
  function parseRoute(hash) {
    const parts = hash.replace(/^#/, "").split("/");
    const name = parts[0];
    if (name === "run" && parts.length === 2) {
      return { view: "run", runId: parts[1] };
    }
    if (name === "compare" && (parts.length === 1 || parts.length === 3)) {
      return { view: "compare" };
    }
    if (parts.length === 1 && SIMPLE_VIEWS.has(name)) {
      return { view: name };
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

  // What each view refreshes. Compare has nothing to load. A run view with an
  // unknown id has no run id in the request and loads nothing.
  const REFRESHERS = {
    runs: function (request) {
      return [refreshRuns(request), refreshTotals(request)];
    },
    run: function (request) {
      return request.runId === null ? [pingServer()] : [refreshRunDetail(request)];
    },
    compare: function () {
      return [pingServer()];
    },
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
    supersede(route.view);
    showView(route.view);
    if (route.view === "run") {
      prepareRunDetailView(route.runId);
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
      if (row) {
        navigateToRun(row.dataset.runId);
      }
    });
    globalThis.matchMedia(DARK_QUERY).addEventListener("change", updateThemeToggle);
    updateThemeToggle();
    globalThis.addEventListener("hashchange", function () {
      applyRoute(true);
    });
  }

  function start() {
    applyStoredTheme();
    bindControls();
    seedBackHistory();
    applyRoute(false);
    globalThis.setInterval(pollView, POLL_INTERVAL_MS);
  }

  start();
})();
