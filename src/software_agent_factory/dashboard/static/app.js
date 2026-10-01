(function () {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 10000;
  const PAGE_SIZE = 20;
  const EMPTY_VALUE = "\u2014";

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
  const SIMPLE_VIEWS = ["runs", "projects", "health"];
  const VIEWS = {
    runs: { section: "view-runs", heading: "runs-heading", nav: "runs", label: "Runs" },
    run: { section: "view-run", heading: "detail-heading", nav: "runs", label: "Run detail" },
    compare: {
      section: "view-compare", heading: "compare-heading", nav: "compare", label: "Compare"
    },
    projects: {
      section: "view-projects", heading: "projects-heading", nav: "projects", label: "Projects"
    },
    health: { section: "view-health", heading: "health-heading", nav: "health", label: "Health" }
  };

  const TOTALS_EXCLUDED_KEYS = ["health", "runs", "page"];
  const INVOCATION_USAGE_COLUMNS = 7;
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
    "Premium-request units",
    "List-price estimate"
  ];
  const USAGE_NOTE =
    "Calculated from Copilot-reported nano-AIU at 1 AI credit = $0.01. " +
    "Your invoice charge may be lower or zero when included credits apply. " +
    "Premium-request units are a separate legacy metric.";

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

  // Runs before anything else so a remembered theme shows without a flash. With
  // no valid stored value the CSS follows the system theme on its own.
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

  function onRefreshSuccess() {
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
      onRefreshSuccess();
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

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function joinList(value) {
    return Array.isArray(value) ? value.join(", ") : null;
  }

  function preferDefined(value, fallback) {
    return value === undefined ? fallback : value;
  }

  function displayValue(value) {
    return value === undefined || value === null || value === "" ? EMPTY_VALUE : String(value);
  }

  function displayUsd(value) {
    if (!isFiniteNumber(value)) {
      return EMPTY_VALUE;
    }
    return "$" + value.toFixed(6);
  }

  function displayListPriceEstimate(value) {
    if (!isFiniteNumber(value)) {
      return "unknown";
    }
    return displayUsd(value);
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
    if (!list || list.tagName !== "UL") {
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
      row.removeAttribute("data-run-id");
    } else {
      row.setAttribute("data-run-id", spec.runId);
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
        return !TOTALS_EXCLUDED_KEYS.includes(entry[0]);
      })
    );
  }

  // "metrics.usage" is nested two levels deep, past what the generic one-level
  // flattening descends into, so its "unknown" fallback (consistent with the
  // detail and invocation views) is shown directly on "metrics" as its own row.
  function withListPriceEstimate(metrics) {
    return {
      ...metrics,
      list_price_estimate_usd: displayListPriceEstimate(metrics.usage?.list_price_estimate_usd)
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

  function pendingTasksRow() {
    const row = document.createElement("tr");
    const cell = row.appendChild(
      element("td", "", "Planning is in progress; tasks are not persisted yet.")
    );
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

  function tasksTable(tasks) {
    const table = document.createElement("table");
    table.appendChild(tableHeader(TASK_HEADERS));
    const body = table.appendChild(document.createElement("tbody"));
    if (tasks.length === 0) {
      body.appendChild(pendingTasksRow());
    }
    for (const task of tasks) {
      body.appendChild(taskRow(task));
    }
    return table;
  }

  function modelStatus(model) {
    if (model.status) {
      return model.status;
    }
    if (model.success === true) {
      return "SUCCESS";
    }
    return model.success === false ? "FAILED" : null;
  }

  function modelRow(model) {
    const usage = model.usage || {};
    const row = document.createElement("tr");
    appendCell(row, model.scope);
    appendCell(row, model.role);
    appendCell(row, model.model);
    appendCell(row, model.purpose);
    appendCell(row, modelStatus(model));
    appendCell(row, model.started_at);
    appendCell(row, usage.input_tokens);
    appendCell(row, usage.output_tokens);
    appendCell(row, displayUsd(usage.usage_value_usd));
    appendCell(row, usage.total_premium_request_cost);
    appendCell(row, displayListPriceEstimate(usage.list_price_estimate_usd));
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

  function totalUsageValue(models) {
    const values = models
      .map(function (model) {
        return model.usage?.usage_value_usd;
      })
      .filter(isFiniteNumber);
    if (values.length === 0) {
      return null;
    }
    return values.reduce(function (sum, value) {
      return sum + value;
    }, 0);
  }

  function usageSummary(models) {
    return "AI usage value: " + displayUsd(totalUsageValue(models)) + ". " + USAGE_NOTE;
  }

  function projectCard(project) {
    const models = asArray(project.models);
    const card = element("article", "project-card");
    card.appendChild(element(
      "h3", "", displayValue(project.project_id) + " \u2014 " + displayValue(project.state)
    ));
    card.appendChild(element("p", "project-meta", projectMeta(project)));
    card.appendChild(wrapTable(tasksTable(asArray(project.tasks))));
    card.appendChild(element("h4", "", "Models used"));
    card.appendChild(element("p", "project-meta", usageSummary(models)));
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
  function setDetailStatus(message) {
    const status = document.getElementById("detail-status");
    status.textContent = message === null ? "" : message;
    status.hidden = message === null;
    document.getElementById("detail-content").hidden = message !== null;
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
      ["Input tokens", usage.input_tokens],
      ["Output tokens", usage.output_tokens],
      ["Reasoning tokens", usage.reasoning_tokens],
      ["Cache read tokens", usage.cache_read_tokens],
      ["AI usage value (USD)", displayUsd(usage.usage_value_usd)],
      ["Premium-request cost", usage.premium_request_cost],
      ["Nano AIU", usage.total_nano_aiu],
      ["List-price estimate", displayListPriceEstimate(usage.list_price_estimate_usd)]
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
        attempt.completed_at
      ]
    };
  }

  function invocationRowSpec(invocation) {
    const usage = invocation.usage || {};
    return {
      cells: [
        invocation.invocation_number,
        invocation.role,
        invocation.model,
        invocation.context_tier,
        invocation.success,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_api_duration_ms,
        usage.session_duration_ms,
        displayUsd(usage.usage_value_usd),
        usage.total_premium_request_cost,
        displayListPriceEstimate(usage.list_price_estimate_usd)
      ]
    };
  }

  function activeInvocationRowSpec(active) {
    const unreported = Array.from({ length: INVOCATION_USAGE_COLUMNS }, function () {
      return EMPTY_VALUE;
    });
    return {
      className: "invocation-active",
      cells: [
        active.invocation_number,
        active.role,
        active.model,
        active.context_tier,
        active.status,
        ...unreported
      ]
    };
  }

  function invocationSpecs(detail) {
    const specs = asArray(detail.invocations).map(invocationRowSpec);
    if (detail.active_invocation) {
      specs.push(activeInvocationRowSpec(detail.active_invocation));
    }
    return specs;
  }

  function renderDetail(detail) {
    const fields = [
      ...identityFields(detail),
      ...usageFields(detail.usage || {}),
      ...guidanceFields(detail),
      ...evidenceFields(detail),
      ...escalationFields(detail),
      ...referenceFields(detail)
    ];
    syncDefinitionList(document.getElementById("detail-body"), fields);
    syncRows(
      document.getElementById("attempts-body"), asArray(detail.attempts).map(attemptRowSpec)
    );
    syncRows(document.getElementById("invocations-body"), invocationSpecs(detail));
    setDetailStatus(null);
  }

  // A failure shows the status line only while no detail has loaded; a loaded
  // card stays visible.
  function refreshDetail(request) {
    const path = "/api/runs/" + encodeURIComponent(request.runId);
    return apiFetch(path).then(whenLatest(request, renderDetail), function (error) {
      if (isLatest(request) && document.getElementById("detail-content").hidden) {
        setDetailStatus("Run detail is currently unavailable.");
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
    if (parts.length === 1 && SIMPLE_VIEWS.includes(name)) {
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
      markNavLink(link, link.getAttribute("data-route") === VIEWS[name].nav);
    }
  }

  function routeTitle(route) {
    if (route.view !== "run") {
      return VIEWS[route.view].label + TITLE_SUFFIX;
    }
    return (RUN_ID_PATTERN.test(route.runId) ? "Run " + route.runId : "Unknown run") + TITLE_SUFFIX;
  }

  function prepareRunView(runId) {
    const heading = document.getElementById("detail-heading");
    if (!RUN_ID_PATTERN.test(runId)) {
      heading.textContent = VIEWS.run.label;
      setDetailStatus("Unknown run");
      return;
    }
    heading.textContent = "Run " + runId;
    setDetailStatus("Loading\u2026");
  }

  // What each view refreshes. Compare has nothing to load. A run view with an
  // unknown id has no run id in the request and loads nothing.
  const REFRESHERS = {
    runs: function (request) {
      return [refreshRuns(request), refreshTotals(request)];
    },
    run: function (request) {
      return request.runId === null ? [pingServer()] : [refreshDetail(request)];
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
  // ends, then reports the first failure if there was one.
  function allSettled(tasks) {
    return Promise.allSettled(tasks).then(function (results) {
      const failed = results.find(function (result) {
        return result.status === "rejected";
      });
      return failed ? Promise.reject(failed.reason) : undefined;
    });
  }

  function settle(request, tasks) {
    return allSettled(tasks)
      .then(whenLatest(request, onRefreshSuccess), whenLatest(request, onRefreshFailure))
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
      refreshView();
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
      prepareRunView(route.runId);
    }
    document.title = routeTitle(route);
    if (moveFocus) {
      document.getElementById(VIEWS[route.view].heading).focus();
    }
    refreshView();
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
    refreshView();
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
        navigateToRun(row.getAttribute("data-run-id"));
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
