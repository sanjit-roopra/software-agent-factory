(function () {
  "use strict";

  // The poll runs fast while the open view shows a run that can still change, else slowly.
  const POLL_ACTIVE_MS = 2000;
  const POLL_IDLE_MS = 15000;
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
  const FILTER_ACTIVE = "active";
  // needs_you_action of each entry in resume_classes.py: a waiting run the page can act on.
  const ACTIONABLE = new Set(["waiting", "retry", "grant", "accept", "widen", "repair"]);
  const RUNS_COLUMN_COUNT = 10;
  const SVG_NS = "http://www.w3.org/2000/svg";
  // The sparkline draws in the viewBox of #figure-week-trend in dashboard/assets.py.
  const SPARK_WIDTH = 70;
  const SPARK_HEIGHT = 20;
  const PERCENT = 100;
  // The tab icon gets a dot while a run needs the operator. Both are served by the handler.
  const FAVICON_PATH = "/assets/favicon.svg";
  const FAVICON_ALERT_PATH = "/assets/favicon-alert.svg";
  const COMPARE_HASH = "#compare";
  const COMPARE_ICON = "\u21c4";
  const MERGED_ICON = "\u2713";
  // ExecutionRoute.FULL in model_base.py: the route most runs take, so the run list shows no chip.
  const DEFAULT_ROUTE = "FULL";
  // OperatorEscalation.status in escalation_models.py: the run list chip and the run page line tone.
  const ESCALATION_CHIPS = {
    running: "escalating",
    done: "escalated",
    failed: "escalation failed"
  };
  const ESCALATION_TONES = { running: "active", done: "done", failed: "failed" };
  const PULL_NUMBER_PATTERN = /\/pull\/(\d+)\/?$/;
  // A run idle for less than this shows no idle time.
  const IDLE_SHOWN_SECONDS = 60;
  // MAX_PAGE_LIMIT in dashboard/snapshot.py: the most runs one request returns.
  const MAX_RUNS_LIMIT = 100;
  // MAX_PAGE_OFFSET in dashboard/snapshot.py: the largest offset the server takes.
  const MAX_RUNS_OFFSET = 1000000;
  // RUN_LIST_STATES in project_models.py, MAX_SEARCH_LENGTH in dashboard/snapshot.py and
  // PROJECT_ID_PATTERN in project_models.py: what the run list filters on the server take.
  const RUN_STATES = new Set(["escalated", "active", "stuck", "failed", "succeeded"]);
  const MAX_SEARCH_LENGTH = 100;
  const PROJECT_ID_PATTERN = /^[A-Za-z0-9._-]{1,80}$/;
  const SORT_OLDEST = "oldest";
  // RUN_LIST_SORT_KEYS in project_models.py: a key sorts low to high, "-" and a key high to
  // low. The Started column sorts newest first ("") or oldest first.
  const SORT_KEYS = ["title", "state", "duration", "cost"];
  const SORTS = new Set([SORT_OLDEST, ...SORT_KEYS, ...SORT_KEYS.map(function (key) {
    return "-" + key;
  })]);
  const SORT_STARTED = "started";
  // A first click sorts these high to low; text sorts from A.
  const SORT_HIGH_FIRST = new Set(["duration", "cost"]);
  const SORT_ARROWS = { ascending: " \u2191", descending: " \u2193" };
  const EMPTY_SEARCH = { q: "", state: "", project: "", sort: "" };
  // Typing waits this long before the list asks the server again.
  const SEARCH_DELAY_MS = 300;
  const RUN_TABS = ["overview", "activity", "changes", "review", "checks", "raw"];
  const DEFAULT_TAB = "overview";
  const TAB_STEPS = { ArrowRight: 1, ArrowLeft: -1 };
  // g then one of these keys opens a view.
  const KEY_ROUTES = { r: "#runs", c: "#compare", p: "#projects", h: "#health" };
  const KEY_COMMANDS = {
    g: "await-go",
    j: "next",
    k: "previous",
    Enter: "open",
    n: "needs-you",
    f: "failure",
    "/": "search",
    r: "refresh",
    "?": "help"
  };
  const NOT_FOUND_STATUS = 404;
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
  // While the operator types, a refresh skips only this region of the view. Other views
  // skip the whole section.
  const DIRTY_REGIONS = { run: "next-step", runs: "runs-body", compare: "compare-content" };

  // What a screen reader hears before each cell of a call row. The visible header
  // row is hidden from assistive technology, so the row carries its own labels.
  const CALL_COLUMN_COUNT = 7;
  // The total row has the cells of a call row, except that its label spans the first
  // three columns.
  const TOTAL_LABEL_SPAN = 3;
  const TOTAL_LABEL = "Total";
  const OUTCOME_CELL = 3;
  const ROLE_CELL = 1;
  const MODEL_CELL = 2;
  const DURATION_CELL = 4;
  const STAT_PART_CLASSES = ["stat-label", "stat-value", "stat-note", "stat-help"];
  const COPIED_TEXT = "Copied";
  const COPY_FAILED_TEXT = "Copy failed, select the text and copy it.";
  const COPIED_VISIBLE_MS = 2000;
  const MS_PER_SECOND = 1000;
  const TIMEOUT_WARN_SHARE = 0.8;
  // How often a running timer on the page moves on between polls.
  const TICK_INTERVAL_MS = MS_PER_SECOND;
  const SECONDS_PER_MINUTE = 60;
  const MINUTES_PER_HOUR = 60;
  const HOURS_PER_DAY = 24;
  const DAYS_SHOWN_RELATIVE = 30;
  const UTC_MINUTE_LENGTH = 16;
  const SMALLEST_SHOWN_USD = 0.0005;
  const ONE_DOLLAR_ROUNDED = 0.9995;
  const SMALL_USD_DECIMALS = 3;
  const USD_DECIMALS = 2;
  // Field names and order match TOKEN_CLASS_FIELDS and COST_UNIT_FIELDS in usage_models.py;
  // an asset test pins them. The labels, help and phrases here are display text only.
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
        return moneyText(value) + " AI usage";
      }
    },
    {
      key: "list_price_estimate_usd",
      label: "List-price estimate (USD)",
      help: "Estimate in USD from list prices, not what a provider billed.",
      phrase: moneyText
    }
  ];
  // The run list's order of cost units: a step or total row shows the first one reported.
  const PRIMARY_COST_KEYS = [
    "usage_value_usd", "list_price_estimate_usd", "total_premium_request_cost"
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
  // failure_link is FLAG_REJECTED and FLAG_LAST_CALL in dashboard/overview.py. A call
  // reports success when the factory rejects what it returned, so the server marks the
  // call a failed run failed on.
  const FAILURE_LINKS = new Map([
    ["rejected", { text: "rejected", className: "status-error" }],
    ["last_call", { text: "success, run failed after", className: "status-warn" }]
  ]);
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
  // compareList is the picker's search, page and the last page loaded; compareLoaded says
  // what the Compare view already holds, so a poll fetches only what may have changed.
  const state = {
    offset: 0, limit: PAGE_SIZE, runId: null, view: "runs", noticeKind: null,
    actionMessage: "", draft: null, filter: null, compare: { a: null, b: null },
    baseTitle: document.title, needsYouCount: 0, fullReason: null, shownReason: null, recordReasons: {},
    evidenceDrawn: { checks: null, review: null, plan: null, diff: null },
    evidence: { plan: null, diff: null, checks: null, review: null }, runDetail: null, diffAttempt: null, checksAttempt: null, diffKey: null,
    search: EMPTY_SEARCH, tab: DEFAULT_TAB, selectedRunId: null, pendingGo: false,
    projectsListed: false, shownRuns: null, selectFirstRow: false,
    compareList: { q: "", offset: 0, page: null }, compareLoaded: { list: null, settled: null },
    streamOpen: false
  };
  // Per view: seq numbers the latest request, inFlight counts unfinished ones.
  const requests = Object.fromEntries(Object.keys(VIEWS).map(function (name) {
    return [name, { seq: 0, inFlight: 0 }];
  }));
  let lastSuccessAt = null;
  // The views that have drawn data once. A failed load shows Retry only before that.
  const loadedViews = new Set();

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

  // The latest tagged answer per path, for If-None-Match. The oldest path goes first.
  const heldAnswers = new Map();
  const MAX_HELD_ANSWERS = 32;

  function holdAnswer(path, etag, body) {
    heldAnswers.delete(path);
    if (!etag) {
      return;
    }
    heldAnswers.set(path, { etag: etag, body: body });
    if (heldAnswers.size > MAX_HELD_ANSWERS) {
      heldAnswers.delete(heldAnswers.keys().next().value);
    }
  }

  // A 304 reuses the held answer; any other answer is read and held under its new tag.
  function readTagged(path, held, response) {
    if (response.status === 304 && held) {
      return held.body;
    }
    return readResponse(response).then(function (body) {
      holdAnswer(path, response.headers.get("ETag"), body);
      return body;
    });
  }

  // Rejects with an error whose "kind" tells a restarted server (401) from a
  // lost connection (network failure, timeout or 5xx) and from any other bad
  // request. The timeout covers reading the body too.
  //
  // A poll sends the tag of the answer it already holds; a 304 reuses that answer, so an
  // unchanged run is neither sent again nor parsed again.
  function apiFetch(path) {
    const controller = new AbortController();
    const timer = globalThis.setTimeout(function () {
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    const held = heldAnswers.get(path);
    const headers = { "X-Factory-Token": token };
    if (held) {
      headers["If-None-Match"] = held.etag;
    }
    return fetch(path, {
      method: "GET",
      headers: headers,
      credentials: "same-origin",
      signal: controller.signal
    })
      .then(function (response) {
        return readTagged(path, held, response);
      }, onNetworkError)
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
      search: state.search,
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
    return (
      "Connection lost, updated " +
      Math.floor((Date.now() - lastSuccessAt) / MS_PER_SECOND) +
      "s ago"
    );
  }

  function renderNotice() {
    setText(document.getElementById("notice"), noticeMessage());
    renderLive();
  }

  // The sidebar dot: live while the server answers. The notice says how long ago it last did.
  function liveText() {
    if (lastSuccessAt === null) {
      return "Connecting\u2026";
    }
    return state.noticeKind === null ? "Live" : "Offline";
  }

  function renderLive() {
    const live = document.getElementById("live");
    setText(live, liveText());
    live.className = state.noticeKind === null && lastSuccessAt !== null ? "live live-on" : "live";
  }

  // Under the live dot: whether factory start ticked lately (ADR-075). No data hides it.
  function renderService(service) {
    const node = document.getElementById("service-live");
    const known = isPlainObject(service) && typeof service.running === "boolean";
    node.hidden = !known;
    const since = known && service.running ? service.ticked_at : null;
    syncTick(node, since, "Service running \u00b7 ticked ");
    node.dataset.tickSuffix = " ago";
    node.className = since === null ? "live" : "live live-on";
    setText(node, since === null ? "Service not running" : node.dataset.tickPrefix +
      elapsedText(since, Date.now()) + " ago");
  }

  // The views without the summary read it only for the service line; a failure keeps the line.
  // One read at a time, so a slow summary never piles up requests across polls.
  let serviceInFlight = false;
  function refreshService() {
    if (serviceInFlight) {
      return;
    }
    serviceInFlight = true;
    apiFetch("/api/summary").then(function (summary) {
      renderService(summary?.service);
    }, function () {}).finally(function () {
      serviceInFlight = false;
    });
  }

  // The Runs link counts the runs that need the operator. None hides the count.
  function renderRunsCount(needsYouCount) {
    const badge = document.getElementById("runs-count");
    const shown = isFiniteNumber(needsYouCount) && needsYouCount > 0;
    badge.hidden = !shown;
    setText(badge, shown ? String(needsYouCount) : "");
    const count = shown ? needsYouCount : 0;
    const changed = count !== state.needsYouCount;
    state.needsYouCount = count;
    renderAlert();
    // The run list heading quotes this count, so it follows when the count lands later.
    if (changed && state.shownRuns !== null) {
      renderRuns(...state.shownRuns);
    }
  }

  // "(2) Runs — Factory dashboard" while two runs need the operator.
  function alertTitle(title, count) {
    return count > 0 ? "(" + count + ") " + title : title;
  }

  // The tab title and icon say when a run needs the operator, so a background tab shows it.
  function renderAlert() {
    const title = alertTitle(state.baseTitle, state.needsYouCount);
    if (document.title !== title) {
      document.title = title;
    }
    const icon = document.getElementById("favicon");
    const href = state.needsYouCount > 0 ? FAVICON_ALERT_PATH : FAVICON_PATH;
    if (icon && icon.getAttribute("href") !== href) {
      icon.setAttribute("href", href);
    }
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

  // An AgentRole in model_base.py is upper case, "IMPLEMENTER"; the page shows "Implementer".
  function roleText(role) {
    const text = displayValue(role);
    return /^[A-Z_]+$/.test(text) ? text.charAt(0) + text.slice(1).toLowerCase().replaceAll("_", " ") : text;
  }

  function displayValue(value, fallback = EMPTY_VALUE) {
    return value === undefined || value === null || value === "" ? fallback : String(value);
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
    link.firstElementChild.setAttribute("aria-hidden", entry.icon === true ? "true" : "false");
    setText(link.lastElementChild, entry.hidden);
    setTitle(link, entry.title);
    setExternal(link, entry.external === true);
    syncChips(cell, asArray(entry.chips));
    cell.className = "";
    setTitle(cell, undefined);
  }

  // A link out of the dashboard opens in a new tab and sends no referrer.
  function setExternal(link, external) {
    if (external) {
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.removeAttribute("target");
      link.removeAttribute("rel");
    }
  }

  // Chips sit after the link, one span each, with the chip title as the tooltip.
  function syncChips(cell, chips) {
    let box = cell.children[1];
    if (chips.length === 0) {
      box?.remove();
      return;
    }
    box = box || cell.appendChild(element("span", "chips"));
    trimChildren(box, chips.length);
    for (const [index, chip] of chips.entries()) {
      const node = childAt(box, index, "span");
      node.className = chip.failed ? "chip chip-failed" : "chip";
      setText(node, chip.text);
      setTitle(node, chip.title);
    }
  }

  // A badge entry holds the badge kind and the text of the badge. The text is
  // always words, so the color is never the only sign of the outcome.
  function patchBadgeCell(cell, entry) {
    let badge = cell.firstElementChild;
    if (badge?.tagName !== "SPAN") {
      clearChildren(cell);
      badge = cell.appendChild(element("span"));
    }
    badge.className = "badge badge-" + entry.badge + (entry.filled ? " badge-filled" : "");
    setText(badge, entry.value);
    setTitle(badge, entry.title);
    cell.className = "";
    setTitle(cell, undefined);
  }

  // A title is the tooltip. An empty or missing one removes the attribute.
  function setTitle(node, text) {
    if (typeof text === "string" && text !== "") {
      if (node.getAttribute("title") !== text) {
        node.setAttribute("title", text);
      }
    } else {
      node.removeAttribute("title");
    }
  }

  // A clamp entry shows its value in a block the style cuts to one line. The cell stays
  // a table cell, which a line clamp on the cell itself would break.
  function patchClampCell(cell, entry) {
    let block = cell.firstElementChild;
    if (block?.className !== "clamp") {
      clearChildren(cell);
      block = cell.appendChild(element("div", "clamp"));
    }
    setText(block, displayValue(entry.value));
    cell.className = entry.className || "";
    setTitle(cell, entry.title);
  }

  // An entry is a plain value, an object with a value, a class name and a
  // tooltip, a link entry, a badge entry or a clamp entry.
  function patchCell(cell, entry) {
    syncTick(cell, isPlainObject(entry) ? entry.tickSince : undefined, "");
    if (isPlainObject(entry) && entry.clamp === true) {
      patchClampCell(cell, entry);
      return;
    }
    if (isPlainObject(entry) && entry.href !== undefined) {
      patchLinkCell(cell, entry);
      return;
    }
    if (isPlainObject(entry) && entry.badge !== undefined) {
      patchBadgeCell(cell, entry);
      return;
    }
    if (isPlainObject(entry) && entry.reason !== undefined) {
      patchReasonCell(cell, entry);
      return;
    }
    const isObject = entry !== null && typeof entry === "object";
    setText(cell, displayValue(isObject ? entry.value : entry));
    cell.className = isObject ? entry.className || "" : "";
    setTitle(cell, isObject ? entry.title : undefined);
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
      const cell = childAt(row, index, "td");
      patchCell(cell, entry);
      cell.colSpan = isPlainObject(entry) && entry.colSpan ? entry.colSpan : 1;
    }
  }

  // A node with a start time counts up from it every tick (see tickTimers).
  function syncTick(node, since, prefix) {
    if (typeof since === "string") {
      node.dataset.tickSince = since;
      node.dataset.tickPrefix = prefix;
    } else {
      delete node.dataset.tickSince;
      delete node.dataset.tickPrefix;
    }
  }

  function tickTimers() {
    const nowMs = Date.now();
    for (const node of document.querySelectorAll("[data-tick-since]")) {
      const suffix = node.dataset.tickSuffix ?? "";
      setText(node, node.dataset.tickPrefix + elapsedText(node.dataset.tickSince, nowMs) + suffix);
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

  // ---- Health ------------------------------------------------------------

  function checkLine(check) {
    return displayValue(check.name) + " [" + displayValue(check.status) + "]: " +
      displayValue(check.message);
  }

  function staleRunItem(run) {
    const runId = typeof run.run_id === "string" ? run.run_id : "";
    const idle = isFiniteNumber(run.idle_seconds) ? ", idle " + idleText(run.idle_seconds) : "";
    return {
      text: displayValue(run.run_id) + " \u00b7 " + displayValue(run.state) + idle,
      href: RUN_ID_PATTERN.test(runId) ? "#run/" + encodeURIComponent(runId) : null
    };
  }

  function modifiedItem(name, modifiedAt, nowMs) {
    return {
      text: displayValue(name) + " \u00b7 changed " + relativeTimeText(modifiedAt, nowMs),
      href: null
    };
  }

  // One card per check the health report runs, with the count and the items it found.
  const HEALTH_CHECKS = [
    {
      key: "stale_runs",
      title: "Stale runs",
      empty: "No run has gone quiet.",
      item: staleRunItem
    },
    {
      key: "stale_locks",
      title: "Stale locks",
      empty: "No lock was left behind.",
      item: function (lock, nowMs) {
        return modifiedItem(lock.lock_name, lock.modified_at, nowMs);
      }
    },
    {
      key: "orphaned_workspaces",
      title: "Orphaned workspaces",
      empty: "No workspace is left without a run.",
      item: function (workspace, nowMs) {
        return modifiedItem(workspace.workspace_name, workspace.modified_at, nowMs);
      }
    }
  ];

  // What the Health view shows: a Healthy or Degraded line, its reasons, when the report
  // was made, and one card per check. Pure, so the page tests can call it.
  function healthView(health, nowMs) {
    const degraded = health.degraded === true;
    const reasons = asArray(health.degraded_reasons).map(function (reason) {
      return displayValue(reason);
    });
    return {
      headline: { text: degraded ? "Degraded" : "Healthy", kind: degraded ? "failed" : "done" },
      reasons: reasons,
      checked: typeof health.generated_at === "string"
        ? "Checked " + relativeTimeText(health.generated_at, nowMs)
        : "",
      cards: HEALTH_CHECKS.map(function (check) {
        const items = asArray(health[check.key]).filter(isPlainObject).map(function (item) {
          return check.item(item, nowMs);
        });
        return { title: check.title, count: items.length, empty: check.empty, items: items };
      })
    };
  }

  function healthItemNode(item) {
    const node = element("li");
    if (item.href) {
      const link = node.appendChild(element("a", "", item.text));
      link.href = item.href;
    } else {
      node.textContent = item.text;
    }
    return node;
  }

  function healthCardNode(card) {
    const node = element("section", "card health-card" + (card.count > 0 ? " health-card-warn" : ""));
    const heading = node.appendChild(element("h2", "", card.title + " "));
    heading.appendChild(element("span", "health-count", String(card.count)));
    if (card.count === 0) {
      node.appendChild(element("p", "health-note", card.empty));
      return node;
    }
    const list = node.appendChild(element("ul", "health-items"));
    for (const item of card.items) {
      list.appendChild(healthItemNode(item));
    }
    return node;
  }

  function buildHealth(container, view) {
    clearChildren(container);
    const head = container.appendChild(element("div", "card health-head"));
    const badge = head.appendChild(element("span", "badge badge-" + view.headline.kind));
    badge.textContent = view.headline.text;
    if (view.checked) {
      head.appendChild(element("span", "health-note", " " + view.checked));
    }
    if (view.reasons.length > 0) {
      const list = head.appendChild(element("ul", "health-reasons"));
      for (const reason of view.reasons) {
        list.appendChild(element("li", "", reason));
      }
    }
    const grid = container.appendChild(element("div", "health-grid"));
    for (const card of view.cards) {
      grid.appendChild(healthCardNode(card));
    }
  }

  // The one line the view shows instead of cards, or null when there are cards to show.
  function healthText(health) {
    if (!isPlainObject(health)) {
      return "No health provider configured.";
    }
    return typeof health.error === "string" ? "The health check is unavailable." : null;
  }

  // The view is rebuilt only when its text changes, so a refresh keeps a focused link.
  function renderHealth(summary) {
    loadedViews.add("health");
    const container = document.getElementById("health-body");
    const health = summary.health;
    renderService(summary.service);
    const text = healthText(health);
    if (text !== null) {
      delete container.dataset.signature;
      setText(container, text);
      return;
    }
    if (Array.isArray(health.checks)) {
      delete container.dataset.signature;
      syncList(container, health.checks.map(checkLine));
      return;
    }
    const view = healthView(health, Date.now());
    const signature = JSON.stringify(view);
    if (container.dataset.signature !== signature) {
      container.dataset.signature = signature;
      buildHealth(container, view);
    }
  }

  // ---- Overview row ------------------------------------------------------

  // The server derives every figure in "overview" of /api/summary, so the page only
  // formats them. A figure the server could not report shows as "not reported".
  const OVERVIEW_FIGURES = [
    { id: "figure-needs-you", read: function (overview) { return overview.needs_you; } },
    { id: "figure-active", read: function (overview) { return overview.active; } },
    {
      id: "figure-success",
      read: function (overview) { return overview.success_rate; },
      format: percentText
    },
    { id: "figure-week-cost", read: weekCost, format: weekMoney }
  ];
  // A note shows only while it has text.
  const OVERVIEW_NOTES = [
    { id: "figure-success-note", read: successNote },
    { id: "figure-week-note", read: weekNote },
    { id: "overview-note", read: scanTruncatedNote }
  ];
  // WEEK_COST_UNITS in dashboard/overview.py: the unit the week's cost is in, and its tag.
  const WEEK_UNIT_TAGS = new Map([
    ["usage_value_usd", "AI usage value"],
    ["list_price_estimate_usd", "list-price estimate"]
  ]);

  // The week's cost in its one unit: the AI usage value when reported, else the list price.
  function weekCost(overview) {
    return overview.week_cost_unit === "usage_value_usd"
      ? overview.week_usage_value_usd
      : overview.week_list_price_usd;
  }

  // A week with no reported cost shows a dash, not "not reported" in the large type.
  function weekMoney(value) {
    return isFiniteNumber(value) ? moneyText(value) : EMPTY_VALUE;
  }

  function successNote(overview) {
    if (!isFiniteNumber(overview.success_rate)) {
      return "";
    }
    return displayNumber(overview.succeeded) + " done \u00b7 " +
      displayNumber(overview.failed) + " failed";
  }

  // The unit of the week's cost, then the units kept apart from it, each in its own unit.
  function weekNote(overview) {
    if (overview.week_calls === 0) {
      return "No calls in the last 7 days";
    }
    const parts = [WEEK_UNIT_TAGS.get(overview.week_cost_unit)];
    const listPrice = overview.week_list_price_usd;
    if (overview.week_cost_unit === "usage_value_usd" && isFiniteNumber(listPrice)) {
      parts.push(moneyText(listPrice) + " list price");
    }
    if (isFiniteNumber(overview.week_premium_requests)) {
      parts.push(compactNumber(overview.week_premium_requests) + " premium req.");
    }
    if (isFiniteNumber(overview.week_tokens)) {
      parts.push(compactNumber(overview.week_tokens) + " tokens");
    }
    return parts.filter(Boolean).join(" \u00b7 ");
  }

  // "75%" for a share of 0.75.
  function percentText(share) {
    return isFiniteNumber(share) ? Math.round(share * PERCENT) + "%" : NOT_REPORTED;
  }

  // "451M" for 451,234,567: short numbers for the tiles and the run list.
  function compactNumber(value) {
    if (!isFiniteNumber(value)) {
      return NOT_REPORTED;
    }
    return value.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 });
  }

  // One point a day, oldest on the left, scaled to the busiest day. Fewer than two days
  // draw nothing.
  function sparklinePoints(values) {
    const days = asArray(values).filter(isFiniteNumber);
    if (days.length < 2) {
      return "";
    }
    const top = Math.max(...days) || 1;
    const step = SPARK_WIDTH / (days.length - 1);
    return days.map(function (value, index) {
      const y = SPARK_HEIGHT - (value / top) * (SPARK_HEIGHT - 2) - 1;
      return (index * step).toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
  }

  function renderSparkline(svg, values) {
    const points = sparklinePoints(values);
    let line = svg.firstElementChild;
    if (points === "") {
      line?.remove();
      return;
    }
    line = line || svg.appendChild(document.createElementNS(SVG_NS, "polyline"));
    line.setAttribute("points", points);
  }

  // The note says so when the figures leave out older runs. It is empty otherwise.
  function scanTruncatedNote(overview) {
    return overview.scan_truncated === true
      ? "The figures cover only the newest runs the dashboard scanned."
      : "";
  }

  function renderOverview(summary) {
    const overview = isPlainObject(summary?.overview) ? summary.overview : {};
    for (const figure of OVERVIEW_FIGURES) {
      const format = figure.format || displayNumber;
      setText(document.getElementById(figure.id), format(figure.read(overview)));
    }
    for (const note of OVERVIEW_NOTES) {
      patchOptional(document.getElementById(note.id), note.read(overview));
    }
    renderSparkline(document.getElementById("figure-week-trend"), overview.week_cost_by_day);
    renderRunsCount(overview.needs_you);
    renderNowRunning(summary?.now_running);
    renderService(summary?.service);
  }

  // One call in progress: a link to its run, the run title, the role and model, and the
  // start time the timer counts up from. A call without a run id has nowhere to link.
  function nowRunningSpec(call) {
    const runId = runIdOf(call);
    if (typeof runId !== "string") {
      return null;
    }
    return {
      href: "#run/" + encodeURIComponent(runId),
      title: displayValue(call.title, runId),
      detail: roleText(call.role) + " \u00b7 " + displayValue(call.model),
      since: call.started_at
    };
  }

  // The strip above the run list: every call in progress, across every project.
  function renderNowRunning(calls) {
    const specs = asArray(calls).filter(isPlainObject).map(nowRunningSpec).filter(Boolean);
    const list = document.getElementById("now-running-list");
    document.getElementById("now-running").hidden = specs.length === 0;
    trimChildren(list, specs.length);
    const nowMs = Date.now();
    for (const [index, spec] of specs.entries()) {
      const item = childAt(list, index, "li");
      if (item.firstElementChild?.tagName !== "A") {
        item.appendChild(element("a")).append(
          element("span", "now-title"), element("span", "now-detail"), element("span", "now-elapsed")
        );
      }
      const link = item.firstElementChild;
      link.setAttribute("href", spec.href);
      const [title, detail, elapsed] = link.children;
      setText(title, spec.title);
      setText(detail, spec.detail);
      syncTick(elapsed, spec.since, "");
      setText(elapsed, elapsedText(spec.since, nowMs));
    }
  }

  // ---- Runs view ---------------------------------------------------------

  function needsYou(run) {
    return run.waiting_for_human === true;
  }

  // The badge names the outcome in words, so it does not rely on its color. The kind
  // comes from the server; a kind the page does not know gets no color class.
  const OUTCOME_KINDS = new Set(["done", "failed", "needs_you", "active", "stale"]);
  const ACTIVE_KINDS = new Set(["active", "stale"]);

  function badgeKind(outcome) {
    return OUTCOME_KINDS.has(outcome?.kind) ? outcome.kind : "neutral";
  }

  // A waiting run the page can approve, answer or retry. A stopped one has only a command.
  function isActionable(run) {
    return run.outcome?.kind === "needs_you" && ACTIONABLE.has(run.outcome.action);
  }

  function isActiveRun(run) {
    return ACTIVE_KINDS.has(run.outcome?.kind);
  }

  // Whole minutes as "18 min" or "2 h 5 min".
  function idleText(seconds) {
    const minutes = Math.floor(seconds / SECONDS_PER_MINUTE);
    if (minutes < MINUTES_PER_HOUR) {
      return minutes + " min";
    }
    return Math.floor(minutes / MINUTES_PER_HOUR) + " h " + (minutes % MINUTES_PER_HOUR) + " min";
  }

  // "idle 18 min" for an active run with no signal for a minute or more, else "".
  function idlePhrase(run) {
    const idle = run.idle_seconds;
    if (!isActiveRun(run) || !isFiniteNumber(idle) || idle < IDLE_SHOWN_SECONDS) {
      return "";
    }
    return "idle " + idleText(idle);
  }

  // What an active run is doing: its stage, the role at work and the idle time.
  function stateNote(run) {
    if (!isActiveRun(run)) {
      return "";
    }
    return [displayValue(run.stage, ""), idlePhrase(run)].filter(Boolean).join(" \u00b7 ");
  }

  function stateCell(run) {
    const label = displayValue(run.outcome?.label, displayValue(run.state));
    return {
      badge: badgeKind(run.outcome),
      filled: isActionable(run),
      value: label,
      title: displayValue(run.state, "")
    };
  }

  function plural(count, word) {
    return count + " " + word + (count === 1 ? "" : "s");
  }

  // The route (the default full one only with "everyRoute"), the last escalation,
  // unattended, restarts, needs look and possibly looping, each as a short chip.
  function runChips(run, everyRoute = false) {
    const chips = [];
    const route = preferDefined(run.effective_route, run.initial_route);
    if (typeof route === "string" && route !== "" && (everyRoute || route !== DEFAULT_ROUTE)) {
      chips.push({ text: route.toLowerCase().replaceAll("_", " ") + " route", title: "Route" });
    }
    const escalation = run.operator_escalation;
    if (isPlainObject(escalation) && Object.hasOwn(ESCALATION_CHIPS, escalation.status)) {
      chips.push({
        text: ESCALATION_CHIPS[escalation.status],
        title: escalationText(escalation),
        failed: escalation.status === "failed"
      });
    }
    if (run.unattended === true) {
      chips.push({ text: "unattended", title: "Started with factory.unattended" });
    }
    if (isFiniteNumber(run.restart_recoveries) && run.restart_recoveries > 0) {
      chips.push({ text: plural(run.restart_recoveries, "restart"), title: "Factory restarts" });
    }
    const reasons = asArray(run.needs_look);
    if (reasons.length > 0) {
      chips.push({ text: "needs look", title: reasons.join("\n") });
    }
    // The server sets it only for an active run whose last attempts repeat (ADR-071).
    if (typeof run.looping === "string" && run.looping !== "") {
      chips.push({ text: "possibly looping", title: run.looping, failed: true });
    }
    return chips;
  }

  // A title links to the run. Its run id, then the models role by role, is the
  // tooltip; the run id is the link text when the run has no title. Chips follow it.
  function titleCell(run, runId) {
    if (typeof runId !== "string") {
      return { value: displayValue(run.title) };
    }
    const models = displayValue(run.models?.detail, "");
    return {
      href: "#run/" + encodeURIComponent(runId),
      value: displayValue(run.title, runId),
      hidden: "",
      title: models === "" ? runId : runId + "\n" + models,
      chips: runChips(run)
    };
  }

  // The pull request as "#7", with a check mark once it merged. It opens on GitHub.
  function pullRequestCell(run) {
    const url = run.pull_request_url;
    if (!isHttpsUrl(url)) {
      return { value: EMPTY_VALUE };
    }
    const number = PULL_NUMBER_PATTERN.exec(url)?.[1];
    const merged = typeof run.merge_commit_sha === "string" && run.merge_commit_sha !== "";
    return {
      href: url,
      external: true,
      value: (number ? "#" + number : "PR") + (merged ? " " + MERGED_ICON : ""),
      hidden: merged ? " merged" : "",
      title: merged ? "Pull request, merged" : "Pull request"
    };
  }

  // The reason is cut to one line by the style. The full text is the tooltip.
  // An active run has no reason, so the cell says what it is doing instead.
  function whyCell(run) {
    const note = displayValue(run.why, "") === "" ? stateNote(run) : "";
    if (note !== "") {
      return { value: note, clamp: true, className: "why-cell muted", title: note };
    }
    return {
      value: displayValue(run.why),
      clamp: true,
      className: "why-cell",
      title: displayValue(run.why, "")
    };
  }

  // How long from the start until "nowMs", "0 s" under a second, or the dash.
  function elapsedText(iso, nowMs) {
    const time = typeof iso === "string" ? Date.parse(iso) : Number.NaN;
    if (Number.isNaN(time) || !isFiniteNumber(nowMs)) {
      return EMPTY_VALUE;
    }
    const ms = Math.max(0, nowMs - time);
    return ms < MS_PER_SECOND ? "0 s" : durationText(ms);
  }

  // When a stopped run stopped: its completion time, else its last update.
  function stoppedAtMs(run) {
    const end = typeof run.completed_at === "string" ? run.completed_at : run.updated_at;
    return typeof end === "string" ? Date.parse(end) : Number.NaN;
  }

  // Wall-clock time from the start: it counts up while the run is active. The agent
  // time, the length of its calls, is the tooltip.
  function durationCell(run, nowMs) {
    const agent = isFiniteNumber(run.duration_ms)
      ? "Agent time: " + durationText(run.duration_ms)
      : "";
    if (isActiveRun(run)) {
      return { value: elapsedText(run.created_at, nowMs), tickSince: run.created_at, title: agent };
    }
    return { value: elapsedText(run.created_at, stoppedAtMs(run)), title: agent };
  }

  // One USD figure with its unit, the AI usage value when reported, else the list-price
  // estimate. Premium requests are their own count: the tooltip, or the value when no USD
  // figure was reported. No unit is added to another.
  function listCostText(usage) {
    const premium = isFiniteNumber(usage?.premium_request_cost)
      ? displayNumber(usage.premium_request_cost) + " premium req."
      : "";
    let money = "";
    if (isFiniteNumber(usage?.usage_value_usd)) {
      money = moneyText(usage.usage_value_usd) + " usage";
    } else if (isFiniteNumber(usage?.list_price_estimate_usd)) {
      money = moneyText(usage.list_price_estimate_usd) + " list";
    }
    if (money === "") {
      return { value: premium || EMPTY_VALUE, title: "" };
    }
    return { value: money, title: premium };
  }

  function tokensCell(run) {
    return {
      value: isFiniteNumber(run.tokens) ? compactNumber(run.tokens) : EMPTY_VALUE,
      title: isFiniteNumber(run.tokens) ? displayNumber(run.tokens) + " tokens" : ""
    };
  }

  function startedCell(run, nowMs) {
    return {
      value: relativeTimeText(run.created_at, nowMs),
      title: absoluteTimeText(run.created_at, "")
    };
  }

  function compareCell(runId) {
    if (typeof runId !== "string") {
      return { value: "", className: "" };
    }
    return {
      value: COMPARE_ICON,
      icon: true,
      href: COMPARE_HASH + "/" + encodeURIComponent(runId),
      hidden: "Compare with another run: " + runId,
      title: "Compare this run with another run"
    };
  }

  function runRowSpec(run, nowMs) {
    const runId = runIdOf(run);
    return {
      runId: runId,
      className: "row-" + badgeKind(run.outcome),
      cells: [
        titleCell(run, runId),
        stateCell(run),
        whyCell(run),
        pullRequestCell(run),
        run.invocation_count,
        durationCell(run, nowMs),
        tokensCell(run),
        listCostText(run.usage),
        startedCell(run, nowMs),
        compareCell(runId)
      ]
    };
  }

  // Runs that need the operator come first, the ones the page can act on before the
  // stopped ones. The needs-you filter keeps only those, the active filter only active runs.
  function orderRuns(runs, filter) {
    if (filter === FILTER_ACTIVE) {
      return runs.filter(isActiveRun);
    }
    const waiting = [
      ...runs.filter(function (run) { return needsYou(run) && isActionable(run); }),
      ...runs.filter(function (run) { return needsYou(run) && !isActionable(run); })
    ];
    if (filter !== null) {
      return waiting;
    }
    return [...waiting, ...runs.filter(function (run) {
      return !needsYou(run);
    })];
  }

  function groupRowSpec(cell) {
    const entry = isPlainObject(cell) ? cell : { value: cell };
    return { className: "group-row", cells: [{ ...entry, colSpan: RUNS_COLUMN_COUNT }] };
  }

  // The inbox heading counts the waiting runs on this page. When the nav badge counts
  // more, it says "15 of 18" and links to the needs-you filter, which lists them all.
  function needsYouHeading(waiting, total) {
    if (!isFiniteNumber(total) || total <= waiting) {
      return { value: "Needs you \u00b7 " + waiting };
    }
    return {
      href: "#runs?filter=" + FILTER_NEEDS_YOU,
      value: "Needs you \u00b7 " + waiting + " of " + total,
      hidden: "",
      title: "Show all " + total + " runs that need you"
    };
  }

  // The runs that need the operator sit under their own group row, the inbox, above the
  // others. A filtered list, or one with no such run or only such runs, has no group rows.
  function runListSpecs(runs, filter, nowMs, needsYouTotal) {
    const specs = runs.map(function (run) { return runRowSpec(run, nowMs); });
    const waiting = runs.filter(needsYou).length;
    if (filter !== null || waiting === 0 || waiting === runs.length) {
      return specs;
    }
    return [
      groupRowSpec(needsYouHeading(waiting, needsYouTotal)),
      ...specs.slice(0, waiting),
      groupRowSpec("Other runs"),
      ...specs.slice(waiting)
    ];
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

  const NO_RUNS_TEXT =
    "No runs yet. Start one with factory run, or let factory start pick up your backlog.";

  function emptyRunsText(request) {
    if (request.filter === FILTER_ACTIVE) {
      return "No runs are active.";
    }
    if (request.filter !== null) {
      return "No runs need you.";
    }
    if (searchParams(request.search, 0) !== "") {
      return "No runs match the search.";
    }
    return request.offset === 0 ? NO_RUNS_TEXT : "No runs on this page.";
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
    document.getElementById("runs-search-bar").hidden = filter !== null;
    for (const button of sortButtons()) {
      button.disabled = filter !== null;
    }
    document.getElementById("runs-filter-name").textContent =
      filter === FILTER_ACTIVE ? "active runs" : "runs that need you";
  }

  function renderRuns(payload, request) {
    loadedViews.add("runs");
    state.shownRuns = [payload, request];
    const runs = orderRuns(asArray(payload.runs), request.filter);
    const nowMs = Date.now();
    syncRows(
      document.getElementById("runs-body"),
      runListSpecs(runs, request.filter, nowMs, state.needsYouCount)
    );
    markSelectedRow();
    if (state.selectFirstRow) {
      state.selectFirstRow = false;
      const rows = runRows();
      selectRowAt(rows, steppedIndex(rows.length, -1, 1));
    }
    renderRunsPager(payload.page || {}, runs.length, request);
    renderRunsStatus(runs.length, request);
    renderRunsFilter(request.filter);
  }

  function runsQuery(request) {
    const filters = searchParams(request.search, 0);
    return "limit=" + encodeURIComponent(request.limit) +
      "&offset=" + encodeURIComponent(request.offset) + (filters === "" ? "" : "&" + filters);
  }

  function refreshRuns(request) {
    const query = runsQuery(request);
    return apiFetch("/api/runs?" + query).then(
      whenLatest(request, renderRuns),
      failBeforeLoad(request, "runs-status", "The runs could not load.")
    );
  }

  function refreshTotals(request) {
    return apiFetch("/api/summary").then(whenLatest(request, renderOverview));
  }

  function refreshHealth(request) {
    return apiFetch("/api/summary").then(
      whenLatest(request, renderHealth),
      failBeforeLoad(request, "health-body", "The health report could not load.")
    );
  }

  // A status line with the text and a Retry button that refreshes the open view now.
  function showLoadFailure(container, text) {
    clearChildren(container);
    container.appendChild(element("span", "", text + " "));
    const retry = container.appendChild(actionButton("Retry", "button"));
    retry.addEventListener("click", function () {
      void refreshView();
    });
    container.hidden = false;
  }

  // A failed load says so, with Retry, only while the view has never drawn data: a
  // loaded view keeps its data, and the notice says the connection is lost.
  function failBeforeLoad(request, containerId, text) {
    return function (error) {
      if (isLatest(request) && !loadedViews.has(request.view)) {
        showLoadFailure(document.getElementById(containerId), text);
      }
      throw error;
    };
  }

  // ---- Projects view -----------------------------------------------------

  // The list sends each project's rollup; its calls load from /api/projects/<id> only
  // while its "Calls" section is open (ADR-078). Each poll patches the cards in place.

  function projectMeta(project) {
    return "Delivery: " + displayValue(project.delivery_mode) +
      " | Target: " + displayValue(project.delivery_repository) +
      "#" + displayValue(project.delivery_base_branch) +
      " | Last activity: " + displayValue(project.last_activity_at ?? project.updated_at);
  }

  // Tasks are saved after planning, so an empty list is only expected while
  // the project is still planning.
  function emptyTasksText(projectState) {
    if (projectState === "PLANNING") {
      return "Planning is in progress; tasks are not persisted yet.";
    }
    return "No tasks.";
  }

  function linkEntry(value) {
    return isHttpsUrl(value) ? { href: value, value: value, external: true } : value;
  }

  function taskSpecs(tasks, projectState) {
    if (tasks.length === 0) {
      return [{ cells: [{ value: emptyTasksText(projectState), colSpan: TASK_HEADERS.length }] }];
    }
    return tasks.map(function (task) {
      return {
        cells: [
          task.task_id, task.title, task.state, task.run_id,
          linkEntry(task.issue_url), linkEntry(task.pull_request_url), task.merge_commit_sha
        ]
      };
    });
  }

  function modelSpec(model) {
    const usage = model.usage || {};
    return {
      cells: [
        model.scope,
        roleText(model.role),
        model.model,
        model.purpose,
        model.status,
        model.started_at,
        displayNumber(usage.input_tokens),
        displayNumber(usage.output_tokens),
        moneyText(usage.usage_value_usd),
        displayNumber(usage.total_premium_request_cost),
        moneyText(usage.list_price_estimate_usd)
      ]
    };
  }

  function usageSummary(totals) {
    const usageValue = totals?.costs?.usage_value_usd;
    return "AI usage value: " + moneyText(usageValue?.total) + ". " + USAGE_NOTE;
  }

  function projectCallsText(project) {
    const failed = project.totals?.failed_calls?.total;
    return displayNumber(project.totals?.calls ?? 0) + " finished, " +
      displayNumber(failed ?? 0) + " failed, " +
      displayNumber(project.active_calls ?? 0) + " in progress";
  }

  function emptyTable(headers) {
    const table = document.createElement("table");
    table.appendChild(tableHeader(headers));
    table.appendChild(document.createElement("tbody"));
    return wrapTable(table);
  }

  function renderProjectCalls(card, project) {
    const body = card.querySelector(".project-calls > div");
    const models = asArray(project.models);
    if (models.length === 0) {
      setText(body, "No calls yet.");
      return;
    }
    if (body.firstElementChild?.className !== "table-wrap") {
      clearChildren(body);
      body.appendChild(emptyTable(MODEL_HEADERS));
    }
    syncRows(body.querySelector("tbody"), models.map(modelSpec));
  }

  // The toggle and each poll may both ask; only the newest answer draws.
  function loadProjectCalls(card) {
    const seq = String(Number(card.dataset.callsSeq || 0) + 1);
    card.dataset.callsSeq = seq;
    return apiFetch("/api/projects/" + encodeURIComponent(card.dataset.projectId)).then(
      function (project) {
        if (card.dataset.callsSeq === seq) {
          renderProjectCalls(card, project);
        }
      },
      function () {
        if (card.dataset.callsSeq === seq) {
          setText(card.querySelector(".project-calls > div"), "The calls could not load.");
        }
      }
    );
  }

  // A card's nodes are built once per project; each poll patches their text and rows.
  function projectCard(projectId) {
    const card = element("article", "project-card");
    card.dataset.projectId = projectId;
    card.append(
      element("h3"),
      element("p", "project-meta"),
      element("div"),
      emptyTable(TASK_HEADERS),
      element("h4", "", "Models used"),
      element("p", "project-meta")
    );
    const calls = card.appendChild(element("details", "project-calls"));
    calls.append(element("summary"), element("div", "", "Loading…"));
    calls.addEventListener("toggle", function () {
      if (calls.open) {
        void loadProjectCalls(card);
      }
    });
    return card;
  }

  function patchProjectCard(card, project) {
    const [title, meta, needs, tasks, , usage, calls] = card.children;
    setText(title, displayValue(project.project_id) + " — " + displayValue(project.state));
    setText(meta, projectMeta(project));
    syncList(needs, asArray(project.needs_look).map(function (reason) {
      return "Needs a look: " + displayValue(reason);
    }));
    syncRows(tasks.querySelector("tbody"), taskSpecs(asArray(project.tasks), project.state));
    setText(usage, usageSummary(project.totals));
    setText(calls.firstElementChild, "Calls: " + projectCallsText(project));
    if (calls.open) {
      void loadProjectCalls(card);
    }
  }

  function renderProjects(payload) {
    loadedViews.add("projects");
    const container = document.getElementById("projects-body");
    const projects = asArray(payload.projects);
    if (projects.length === 0) {
      setText(container, "No persisted projects.");
      return;
    }
    const cards = new Map();
    for (const card of container.querySelectorAll("article.project-card")) {
      cards.set(card.dataset.projectId, card);
    }
    if (cards.size === 0) {
      clearChildren(container);
    }
    for (const [index, project] of projects.entries()) {
      const id = String(project.project_id);
      const card = cards.get(id) ?? projectCard(id);
      cards.delete(id);
      if (container.children[index] !== card) {
        container.insertBefore(card, container.children[index] ?? null);
      }
      patchProjectCard(card, project);
    }
    for (const stale of cards.values()) {
      stale.remove();
    }
  }

  function refreshProjects(request) {
    return apiFetch("/api/projects").then(
      whenLatest(request, renderProjects),
      failBeforeLoad(request, "projects-body", "The projects could not load.")
    );
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

  // Grey bars while a view's first fetch is pending; screen readers hear the
  // loading text instead. assets.skeleton renders the same markup in the page.
  function skeletonNodes(rows) {
    const bars = element("span", "skeleton");
    bars.ariaHidden = "true";
    for (let index = 0; index < rows; index += 1) {
      bars.append(element("span", "skeleton-row"));
    }
    return [element("span", "visually-hidden", LOADING_TEXT), bars];
  }

  function activeInvocationText(active) {
    if (!active) {
      return null;
    }
    const tools = asArray(active.tool_calls);
    const last = tools.length === 0 ? "" : " \u2014 last tool: " + toolLine(tools.at(-1));
    return active.role + " (" + active.model + ") \u2014 " + active.status +
      " since " + active.started_at + last;
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
      ["AI usage value (USD)", moneyText(usage.usage_value_usd)],
      ["Premium requests", displayNumber(usage.premium_request_cost)],
      ["Nano AIU", usage.total_nano_aiu],
      ["List-price estimate", moneyText(usage.list_price_estimate_usd)]
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

  function attemptRowSpec(attempt, runId) {
    const query = "attempt=" + attempt.attempt_number;
    const reason = recordReason(runId, query, attempt.failure_reason,
      attempt.failure_reason_truncated);
    return {
      cells: [
        attempt.attempt_number,
        roleText(attempt.role),
        attempt.model,
        attempt.budget,
        attempt.triggered_by,
        attempt.outcome,
        attempt.started_at,
        attempt.completed_at,
        { value: reason.text, className: "reason-cell", reason: reason }
      ]
    };
  }

  // ---- Run detail: values ------------------------------------------------

  function premiumRequestsPhrase(value) {
    return displayNumber(value) + (value === 1 ? " premium request" : " premium requests");
  }

  // Money for a person: under one dollar it keeps three decimals ($0.039), from one
  // dollar up it keeps two ($12.35). An amount too small for three decimals says so.
  function moneyText(value) {
    if (!isFiniteNumber(value) || value < 0) {
      return NOT_REPORTED;
    }
    if (value === 0) {
      return "$0";
    }
    if (value < SMALLEST_SHOWN_USD) {
      return "<$0.001";
    }
    if (value < ONE_DOLLAR_ROUNDED) {
      return "$" + value.toFixed(SMALL_USD_DECIMALS);
    }
    return "$" + value.toLocaleString("en-US", {
      minimumFractionDigits: USD_DECIMALS, maximumFractionDigits: USD_DECIMALS
    });
  }

  function durationText(ms) {
    if (!isFiniteNumber(ms)) {
      return NOT_REPORTED;
    }
    if (ms < MS_PER_SECOND) {
      return ms + " ms";
    }
    const seconds = Math.round(ms / MS_PER_SECOND);
    if (seconds < SECONDS_PER_MINUTE) {
      return seconds + " s";
    }
    const minutes = Math.floor(seconds / SECONDS_PER_MINUTE);
    if (minutes < MINUTES_PER_HOUR) {
      return minutes + " min " + (seconds % SECONDS_PER_MINUTE) + " s";
    }
    return Math.floor(minutes / MINUTES_PER_HOUR) + " h " + (minutes % MINUTES_PER_HOUR) + " min";
  }

  // The time as "2026-10-02 11:22 UTC", or the fallback when it is not a time.
  function absoluteTimeText(iso, fallback = EMPTY_VALUE) {
    const time = typeof iso === "string" ? Date.parse(iso) : Number.NaN;
    if (Number.isNaN(time)) {
      return fallback;
    }
    return new Date(time).toISOString().slice(0, UTC_MINUTE_LENGTH).replace("T", " ") + " UTC";
  }

  // How long ago the time was, "12 min ago". Older than a month it shows the date.
  // The caller passes the current time in milliseconds, so the text is the same on
  // every call with the same input.
  function relativeTimeText(iso, nowMs) {
    const time = typeof iso === "string" ? Date.parse(iso) : Number.NaN;
    if (Number.isNaN(time) || !isFiniteNumber(nowMs)) {
      return EMPTY_VALUE;
    }
    const minutes = Math.floor((nowMs - time) / (MS_PER_SECOND * SECONDS_PER_MINUTE));
    if (minutes < 1) {
      return "just now";
    }
    if (minutes < MINUTES_PER_HOUR) {
      return minutes + " min ago";
    }
    const hours = Math.floor(minutes / MINUTES_PER_HOUR);
    if (hours < HOURS_PER_DAY) {
      return hours + " h ago";
    }
    const days = Math.floor(hours / HOURS_PER_DAY);
    return days < DAYS_SHOWN_RELATIVE ? days + " d ago" : absoluteTimeText(iso);
  }

  function outcomeOf(status) {
    return OUTCOMES.get(status) || UNREPORTED_OUTCOME;
  }

  // Each cost unit reported, as its own phrase, in the run list's order: the AI usage value,
  // the list price, then premium requests. No unit is added to another.
  function costPhrases(costs) {
    return PRIMARY_COST_KEYS.filter(function (key) {
      return isFiniteNumber(costs[key]);
    }).map(function (key) {
      return COST_UNITS.find(function (unit) { return unit.key === key; }).phrase(costs[key]);
    });
  }

  // A step shows one cost unit, like the run list; the others go in its tooltip.
  function costText(costs) {
    return costPhrases(costs)[0] ?? NOT_REPORTED;
  }

  function costTitle(costs) {
    return costPhrases(costs).slice(1).join(", ");
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

  function modelText(call) {
    const model = displayValue(call.model);
    return call.reasoning ? model + " (" + call.reasoning + ")" : model;
  }

  function callOutcome(call) {
    return FAILURE_LINKS.get(call.failure_link) || outcomeOf(call.status);
  }

  function callCells(call, verdict) {
    const usage = call.usage || {};
    return [
      displayValue(call.invocation_number),
      stepSummary(call, verdict),
      modelText(call),
      callOutcome(call).text,
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
      ["Runtime", displayValue(call.runtime, NOT_REPORTED)],
      ["Started", displayValue(call.started_at, NOT_REPORTED)],
      ...TOKEN_CLASSES.map(function (tokenClass) {
        return [tokenClass.label, displayNumber(usage[tokenClass.key])];
      }),
      ...fallbackFields(call.fallback_reason),
      ...timingFields(call.performance, call.duration_ms),
      ...toolCallFields(call.tool_calls, call.tool_call_count),
      ...reasonFields(call.failure_reason, call.failure_reason_truncated, runId)
    ];
  }

  // The call's reason row shows the whole text once it has loaded.
  function callFieldsWithReason(call, runId, reason) {
    const fields = callFields(call, runId);
    if (reason.text === call.failure_reason) {
      return fields;
    }
    return fields.filter(function (field) {
      return field[0] !== "Reason was cut";
    }).map(function (field) {
      return field[0] === "Failure reason" ? [field[0], reason.text] : field;
    });
  }

  function fallbackFields(reason) {
    return typeof reason === "string" && reason !== "" ? [["Why it fell back", reason]] : [];
  }

  // Where the call's time went: starting the runtime, waiting for its first event, in all.
  // Shown only when the runtime measured the start or the first event.
  function timingFields(performance, totalMs) {
    const parts = [];
    for (const [label, ms] of [
      ["boot", performance?.process_boot_ms],
      ["first event", performance?.first_event_ms]
    ]) {
      if (isFiniteNumber(ms)) {
        parts.push(label + " " + durationText(Math.round(ms)));
      }
    }
    if (parts.length > 0 && isFiniteNumber(totalMs)) {
      parts.push("total " + durationText(totalMs));
    }
    return parts.length === 0 ? [] : [["Timing", parts.join(" \u00b7 ")]];
  }

  function toolLine(tool) {
    return tool.preview ? tool.name + "  " + tool.preview : tool.name;
  }

  // One line per tool the call used: its name, then what it ran or read. A running call
  // keeps only its latest tools, so the label says how many it started in all.
  function toolCallFields(toolCalls, total) {
    const lines = asArray(toolCalls).map(toolLine);
    if (lines.length === 0) {
      return [];
    }
    const count = typeof total === "number" && total > lines.length
      ? "latest " + lines.length + " of " + total
      : String(lines.length);
    return [["Tool calls (" + count + ")", lines.join("\n")]];
  }

  // ---- Run detail: what each step did --------------------------------------

  // Tools that write a file. Their preview is the file path (transcripts.py).
  const FILE_TOOLS = new Set(["edit", "multiedit", "write", "create", "notebookedit"]);

  // The files a call wrote, each once, in the order it first wrote them.
  function filesTouched(toolCalls) {
    const paths = asArray(toolCalls).filter(function (tool) {
      return FILE_TOOLS.has(String(tool.name).toLowerCase()) && Boolean(tool.preview);
    }).map(function (tool) { return tool.preview; });
    return Array.from(new Set(paths));
  }

  // The attempt a call worked on, as its place among all the run's attempts: the
  // attempts/NN number that checks and review rounds use. It counts the attempts that
  // had started when the call started. A call outside an attempt, such as triage, has none.
  function attemptIndex(call, attempts) {
    const begin = startMs(call);
    if (!isFiniteNumber(call.attempt_number) || begin === null) {
      return null;
    }
    const count = asArray(attempts).filter(function (attempt) {
      return Date.parse(attempt.started_at) <= begin;
    }).length;
    return count > 0 ? count : null;
  }

  function entryFor(items, key, index) {
    return asArray(items).find(function (item) { return item[key] === index; });
  }

  // What the reviewer or the tester decided about the attempt, from the saved reports.
  function stepReviewVerdict(round) {
    if (!round) {
      return "";
    }
    if (round.skipped) {
      return "review skipped";
    }
    return round.approved ? "approved" : "changes requested";
  }

  function stepTesterVerdict(result) {
    if (!result) {
      return "";
    }
    if (result.skipped) {
      return "tester skipped";
    }
    return result.passed ? "tests passed" : "tests failed";
  }

  function stepVerdict(call, index, evidence) {
    if (call.role === "REVIEWER") {
      return stepReviewVerdict(entryFor(evidence.review?.rounds, "attempt_number", index));
    }
    if (call.role === "TESTER") {
      return stepTesterVerdict(entryFor(evidence.checks?.tester_history, "attempt", index));
    }
    return "";
  }

  function stepSummary(call, verdict) {
    return verdict ? roleText(call.role) + " \u00b7 " + verdict : roleText(call.role);
  }

  // "2 of 3 passed" for the verification checks the attempt saved.
  function checksText(index, checks) {
    const entry = entryFor(checks?.verification_history, "attempt", index);
    const ran = asArray(entry?.checks);
    if (ran.length === 0) {
      return "";
    }
    const passed = ran.filter(function (check) { return check.passed === true; }).length;
    return passed + " of " + ran.length + " passed";
  }

  // The files the call wrote, the checks of its attempt and the verdict on it.
  function stepFields(call, index, evidence) {
    const files = filesTouched(call.tool_calls);
    const checks = checksText(index, evidence.checks);
    const verdict = stepVerdict(call, index, evidence);
    return [
      ...(files.length > 0 ? [["Files touched (" + files.length + ")", files.join("\n")]] : []),
      ...(checks ? [["Checks of attempt " + index, checks]] : []),
      ...(verdict ? [["Verdict", verdict]] : [])
    ];
  }

  // One small line icon per role, drawn in the text color.
  const ROLE_ICONS = {
    TRIAGE: "M2 3h12L9.5 8.5V13l-3 1V8.5z",
    REFINER: "M3 13l1-3 7-7 2 2-7 7z",
    PLANNER: "M6 4h8M6 8h8M6 12h8M2 4h1M2 8h1M2 12h1",
    IMPLEMENTER: "M6 4L2 8l4 4M10 4l4 4-4 4",
    TESTER: "M6 2h4M7 2v4l-4 7.5h10L9 6V2",
    REVIEWER: "M1.5 8s2.5-4.5 6.5-4.5 6.5 4.5 6.5 4.5-2.5 4.5-6.5 4.5S1.5 8 1.5 8zM8 6.5v3"
  };
  const OTHER_ROLE_ICON = "M8 3a5 5 0 1 0 0 10A5 5 0 0 0 8 3z";

  function roleIcon(role) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "role-icon");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    const path = svg.appendChild(document.createElementNS(SVG_NS, "path"));
    path.setAttribute("d", ROLE_ICONS[role] || OTHER_ROLE_ICON);
    return svg;
  }

  // The role cell: the role's icon, then the summary text.
  function patchRoleText(node, role, text) {
    if (node.dataset.role !== String(role)) {
      node.dataset.role = String(role);
      node.replaceChildren(roleIcon(role), document.createTextNode(""));
    }
    node.lastChild.data = text;
  }

  // The reduced view shows one line per call; "Show all detail" shows every column.
  const STEPS_DETAIL_KEY = "factory-dashboard-steps-detail";

  function readStepsDetail() {
    try {
      return globalThis.localStorage.getItem(STEPS_DETAIL_KEY) === "full";
    } catch {
      // Storage is blocked, so the reduced view shows.
      return false;
    }
  }

  function showStepsDetail(full) {
    document.getElementById("timeline-table").classList.toggle("timeline-reduced", !full);
    const button = document.getElementById("steps-view-toggle");
    button.setAttribute("aria-pressed", String(full));
    setText(button, full ? "Show less detail" : "Show all detail");
  }

  function storeStepsDetail(full) {
    try {
      globalThis.localStorage.setItem(STEPS_DETAIL_KEY, full ? "full" : "reduced");
    } catch {
      // Storage is blocked: the choice lasts for this page view only.
    }
  }

  function toggleStepsDetail() {
    const full = document.getElementById("timeline-table").classList.contains("timeline-reduced");
    showStepsDetail(full);
    storeStepsDetail(full);
  }

  // ---- Run detail: step tags, runtime chips and the duration bar ----------

  // A tag shows only when the call is not the usual kind: a purpose other than STANDARD,
  // a context tier other than the default.
  function stepTags(call) {
    const tags = [];
    if (typeof call.purpose === "string" && call.purpose !== "STANDARD") {
      tags.push({ text: roleText(call.purpose).toLowerCase() });
    }
    if (call.context_tier === "long_context") {
      tags.push({ text: "long context" });
    }
    return tags;
  }

  // The runtime that served the call, and a warning chip when a fallback served it.
  function runtimeChips(call) {
    const chips = [];
    if (typeof call.runtime === "string") {
      chips.push({ text: call.runtime });
    }
    if (typeof call.fallback_reason === "string" && call.fallback_reason !== "") {
      chips.push({ text: "fell back", title: call.fallback_reason, className: "chip chip-warn" });
    }
    return chips;
  }

  function syncStepChips(container, chips) {
    trimChildren(container, chips.length);
    for (const [index, chip] of chips.entries()) {
      const node = childAt(container, index, "span");
      node.className = chip.className || "chip";
      setText(node, chip.text);
      if (chip.title) {
        node.title = chip.title;
      } else {
        node.removeAttribute("title");
      }
    }
  }

  function startMs(call) {
    const time = Date.parse(call.started_at);
    return Number.isFinite(time) ? time : null;
  }

  // The first start and the longest duration among the calls, for the start offset
  // and the width of each duration bar.
  function stepScale(calls) {
    const starts = calls.map(startMs).filter(function (time) { return time !== null; });
    const durations = calls.map(function (call) { return call.duration_ms; }).filter(isFiniteNumber);
    return {
      firstStart: starts.length === 0 ? null : Math.min(...starts),
      longest: durations.length === 0 ? 0 : Math.max(...durations)
    };
  }

  function offsetText(call, scale) {
    const begin = startMs(call);
    if (begin === null || scale.firstStart === null) {
      return "";
    }
    const offset = begin - scale.firstStart;
    return offset > 0 ? "at +" + durationText(offset) : "at start";
  }

  function patchDurationBar(container, call, scale) {
    const bar = childAt(container, 0, "span");
    bar.className = "duration-bar";
    const share = isFiniteNumber(call.duration_ms) && scale.longest > 0
      ? call.duration_ms / scale.longest
      : 0;
    bar.style.width = Math.max(2, Math.round(share * 100)) + "%";
    bar.hidden = !isFiniteNumber(call.duration_ms);
    const offset = childAt(container, 1, "span");
    offset.className = "step-offset";
    setText(offset, offsetText(call, scale));
  }

  function patchStepExtras(row, call, scale) {
    syncStepChips(row.children[ROLE_CELL].lastElementChild, stepTags(call));
    syncStepChips(row.children[MODEL_CELL].lastElementChild, runtimeChips(call));
    patchDurationBar(row.children[DURATION_CELL].lastElementChild, call, scale);
    row.dataset.outcome = callOutcome(call).className;
  }

  // A call is two table rows: the default columns, and below them a row that holds the
  // rest of the fields. The button in the first cell shows and hides that second row.
  const ROWS_PER_CALL = 2;
  const CALL_MORE_ID_PREFIX = "call-more-";

  function setCallOpen(row, open) {
    row.firstElementChild.firstElementChild.setAttribute("aria-expanded", String(open));
    row.nextElementSibling.hidden = !open;
  }

  function buildToggle(row, detailId) {
    const button = element("button", "call-toggle");
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", detailId);
    button.append(element("span", "visually-hidden", "Details of call "), element("span"));
    button.addEventListener("click", function () {
      setCallOpen(row, button.getAttribute("aria-expanded") !== "true");
    });
    return button;
  }

  function buildCall(body) {
    const detailId = CALL_MORE_ID_PREFIX + body.children.length / ROWS_PER_CALL;
    const row = body.appendChild(element("tr", "call-row"));
    for (let column = 0; column < CALL_COLUMN_COUNT; column += 1) {
      const cell = row.appendChild(element("td"));
      if (column > 0) {
        cell.append(element("span"), element("span", "step-extra"));
      }
    }
    row.firstElementChild.appendChild(buildToggle(row, detailId));
    const detail = body.appendChild(element("tr", "call-detail"));
    detail.id = detailId;
    detail.hidden = true;
    const cell = detail.appendChild(element("td"));
    cell.colSpan = CALL_COLUMN_COUNT;
    cell.append(element("dl", "call-fields"), reasonButton(), buildTranscript(),
      callFileBox("prompt"));
    return row;
  }

  // ---- Run detail: call transcript and prompt -----------------------------

  // kind is "transcript" or "prompt" (ADR-070).
  function callFilePath(runId, number, kind) {
    return runDetailPath(runId) + "/calls/" + encodeURIComponent(number) + "/" + kind;
  }

  function transcriptLine(event) {
    return event.text ? event.type + "  " + event.text : event.type;
  }

  // One line per event, after a note when the server left earlier events out.
  function transcriptText(body) {
    const lines = asArray(body.events).map(transcriptLine);
    if (body.truncated === true) {
      lines.unshift("(earlier events left out)");
    }
    return lines.length === 0 ? "No events." : lines.join("\n");
  }

  function callFileBox(kind) {
    const box = element("div", "call-transcript");
    box.dataset.kind = kind;
    const button = box.appendChild(element("button", "", "View " + kind));
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    box.appendChild(element("pre", kind + "-events")).hidden = true;
    button.addEventListener("click", function () {
      toggleCallFile(box);
    });
    return box;
  }

  function buildTranscript() {
    const box = callFileBox("transcript");
    const raw = box.appendChild(element("details", "transcript-raw"));
    raw.append(element("summary", "", "Raw text (end)"), element("pre", "transcript-tail"));
    raw.hidden = true;
    return box;
  }

  // A different call in the slot starts closed and loads its own transcript or prompt.
  function resetCallFile(box, runId, number) {
    box.hidden = typeof number !== "number";
    box.dataset.key = runId + "/" + number;
    box.dataset.loaded = "";
    setCallFileOpen(box, false);
  }

  function setCallFileOpen(box, open) {
    const [button, events, raw] = box.children;
    button.setAttribute("aria-expanded", String(open));
    setText(button, (open ? "Hide " : "View ") + box.dataset.kind);
    events.hidden = !open;
    if (raw) {
      raw.hidden = !open || box.dataset.loaded !== "yes";
    }
  }

  function showCallFile(box, body) {
    const [, events, raw] = box.children;
    if (box.dataset.kind === "prompt") {
      setText(events, typeof body.text === "string" ? body.text : "");
      return;
    }
    setText(events, transcriptText(body));
    setText(raw.lastElementChild, typeof body.tail === "string" ? body.tail : "");
  }

  // The file loads once, when a person opens it. It is not polled.
  function toggleCallFile(box) {
    const [button, events] = box.children;
    const kind = box.dataset.kind;
    const open = button.getAttribute("aria-expanded") !== "true";
    setCallFileOpen(box, open);
    if (!open || box.dataset.loaded === "yes") {
      return;
    }
    const key = box.dataset.key;
    const slash = key.lastIndexOf("/");
    setText(events, "Loading the " + kind + "...");
    button.disabled = true;
    apiFetch(callFilePath(key.slice(0, slash), key.slice(slash + 1), kind))
      .then(function (body) {
        if (box.dataset.key === key) {
          box.dataset.loaded = "yes";
          showCallFile(box, body);
          setCallFileOpen(box, button.getAttribute("aria-expanded") === "true");
        }
      }, function () {
        if (box.dataset.key === key) {
          setText(events, kind === "prompt"
            ? "No prompt was kept for this call. Set factory.store_prompts to keep prompts."
            : "No transcript was kept for this call, or it could not load.");
        }
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  function callAt(body, index) {
    return body.children[index * ROWS_PER_CALL] || buildCall(body);
  }

  // The same rows are reused for the same call, so an open call stays open across a
  // refresh. A different call in the slot starts closed.
  function patchCall(row, call, runId, scale, attempts) {
    const key = runId + "/" + displayValue(call.invocation_number);
    if (row.dataset.call !== key) {
      row.dataset.call = key;
      setCallOpen(row, false);
      const prompt = row.nextElementSibling.firstElementChild.lastElementChild;
      resetCallFile(prompt.previousElementSibling, runId, call.invocation_number);
      resetCallFile(prompt, runId, call.invocation_number);
    }
    const index = attemptIndex(call, attempts);
    const evidence = state.evidence;
    const [number, ...columns] = callCells(call, stepVerdict(call, index, evidence));
    setText(row.firstElementChild.firstElementChild.lastElementChild, number);
    for (const [column, text] of columns.entries()) {
      const node = row.children[column + 1].firstElementChild;
      if (column + 1 === ROLE_CELL) {
        patchRoleText(node, call.role, text);
      } else {
        setText(node, text);
      }
    }
    setTitle(row.lastElementChild, costTitle(call.usage || {}));
    patchStepExtras(row, call, scale);
    row.children[OUTCOME_CELL].className = callOutcome(call).className;
    row.classList.toggle("call-rejected", call.failure_link === "rejected");
    const fields = row.nextElementSibling.firstElementChild.firstElementChild;
    const query = "call=" + call.invocation_number;
    const reason = recordReason(runId, query, call.failure_reason, call.failure_reason_truncated);
    syncDefinitionList(fields, [
      ...stepFields(call, index, evidence), ...callFieldsWithReason(call, runId, reason)
    ]);
    syncReasonButton(fields.nextElementSibling, reason);
  }

  // The total of each cost unit the calls reported, keyed like one call's usage.
  function totalCosts(totals) {
    return Object.fromEntries(COST_UNITS.map(function (unit) {
      return [unit.key, totals.costs?.[unit.key]?.total];
    }));
  }

  function callCountText(calls) {
    if (!isFiniteNumber(calls)) {
      return NOT_REPORTED;
    }
    return displayNumber(calls) + (calls === 1 ? " call" : " calls");
  }

  function totalCells(totals) {
    return [
      TOTAL_LABEL,
      callCountText(totals.calls),
      durationText(totals.duration_ms?.total),
      displayNumber(totals.total_tokens?.total),
      costText(totalCosts(totals))
    ];
  }

  // The label is the header of the row; the figures follow in the last four columns.
  function patchTotalRow(row, totals) {
    const [label, ...figures] = totalCells(totals);
    const header = childAt(row, 0, "th");
    header.setAttribute("scope", "row");
    header.colSpan = TOTAL_LABEL_SPAN;
    setText(header, label);
    for (const [index, text] of figures.entries()) {
      setText(childAt(row, index + 1, "td"), text);
    }
    setTitle(row.lastElementChild, costTitle(totalCosts(totals)));
  }

  // Finished calls come sorted by number; the running call comes last. The total row
  // sums the finished calls: the running call has no usage yet.
  function renderTimeline(detail, runId) {
    const active = detail.active_invocation ? [detail.active_invocation] : [];
    const calls = [...asArray(detail.invocations), ...active];
    const body = document.getElementById("timeline-body");
    trimChildren(body, calls.length * ROWS_PER_CALL);
    const scale = stepScale(calls);
    for (const [index, call] of calls.entries()) {
      patchCall(callAt(body, index), call, runId, scale, detail.attempts);
    }
    patchTotalRow(document.getElementById("timeline-total"), detail.totals || {});
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

  // The changed files beyond the approved scope that a scope approval adds (ADR-060).
  function scopePathsSection(paths) {
    const items = asArray(paths);
    return items.length === 0 ? null : listSection("Files outside the approved scope", items);
  }

  // A cut reason offers its full text in place of the note; the loaded text replaces it.
  function failureSection(step, runId) {
    const full = fullReasonFor(runId);
    const cut = step.failure_reason_truncated === true && full === null;
    const reason = full ?? step.failure_reason;
    const fields = reasonFields(reason, false, runId);
    if (fields.length === 0) {
      return null;
    }
    const section = element("div", "next-step-section");
    const className = full === null ? "reason-text" : "reason-text reason-full";
    section.appendChild(element("p", className, fields[0][0] + ": " + fields[0][1]));
    if (cut && runId !== null) {
      const more = section.appendChild(actionButton(SHOW_REASON_LABEL, "button"));
      more.addEventListener("click", function () {
        showFullReason(more);
      });
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
  function copyControl(text, label = "Copy reply") {
    const row = element("div", "copy-row");
    const button = row.appendChild(element("button", "", label));
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

  // The command for a step the page has no action for, with a copy button.
  function commandSection(command) {
    if (typeof command !== "string" || command === "") {
      return null;
    }
    const section = element("div", "next-step-section");
    section.appendChild(element("h3", "", "Run in a terminal"));
    section.appendChild(element("pre", "reply-text", command));
    section.appendChild(copyControl(command, "Copy command"));
    return section;
  }

  // ---- Run detail: approve, answer and retry -----------------------------

  // Mirror MAX_PLAN_DECISION_ANSWER_CHARS in resume.py. The server checks again.
  const MIN_ANSWER_CHARS = 1;
  const MAX_ANSWER_CHARS = 500;
  const ANSWER_HINT = MIN_ANSWER_CHARS + " to " + MAX_ANSWER_CHARS +
    " characters, one line, no paths, links or secrets";
  const ACCEPTED_STATUS = 202;
  const BAD_REQUEST_STATUS = 400;
  const CONFLICT_STATUS = 409;
  const ANSWER_ACTION = "answer";
  const RETRY_ACTION = "retry";
  const GRANT_ACTION = "grant";
  const ACCEPT_ACTION = "accept";
  const WIDEN_ACTION = "widen";
  const REPAIR_ACTION = "repair";
  const STOP_ACTION = "cancel";
  const STEER_ACTION = "steer";
  const ESCALATE_ACTION = "escalate";
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
    not_waiting: RUN_CHANGED_TEXT,
    not_active: "this run has already finished",
    not_escalatable: "this run cannot escalate now, reload the page"
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

  // The same for a retry of publishing.
  const RETRY_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "retry already sent",
    expired: "retry expired, retry again"
  };

  // The same for one more attempt (ADR-058).
  const GRANT_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "attempt already granted",
    expired: "grant expired, grant again"
  };

  // The same for accepting open review findings (ADR-059).
  const ACCEPT_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "findings already accepted",
    expired: "acceptance expired, accept again"
  };

  // The same for approving a wider scope (ADR-060).
  const WIDEN_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "wider scope already approved",
    expired: "scope approval expired, approve again"
  };

  // The same for retrying CI (ADR-061).
  const REPAIR_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "CI retry already requested",
    expired: "CI retry expired, retry again"
  };

  // The same for a stop (ADR-047).
  const STOP_CONFLICT_MESSAGES = {
    ...CONFLICT_MESSAGES,
    existing_request: "stop already requested"
  };

  function conflictMessages(action) {
    if (action === STOP_ACTION) {
      return STOP_CONFLICT_MESSAGES;
    }
    if (action === RETRY_ACTION) {
      return RETRY_CONFLICT_MESSAGES;
    }
    if (action === GRANT_ACTION) {
      return GRANT_CONFLICT_MESSAGES;
    }
    if (action === ACCEPT_ACTION) {
      return ACCEPT_CONFLICT_MESSAGES;
    }
    if (action === WIDEN_ACTION) {
      return WIDEN_CONFLICT_MESSAGES;
    }
    if (action === REPAIR_ACTION) {
      return REPAIR_CONFLICT_MESSAGES;
    }
    return action === ANSWER_ACTION ? ANSWER_CONFLICT_MESSAGES : CONFLICT_MESSAGES;
  }

  function failureMessage(outcome, action) {
    if (outcome.status === CONFLICT_STATUS) {
      return lookupMessage(conflictMessages(action), outcome.body.reason);
    }
    if (outcome.status === BAD_REQUEST_STATUS &&
      (action === STEER_ACTION || action === ESCALATE_ACTION)) {
      return "the note is empty, too long or holds a secret";
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
    submission.onAccepted?.();
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

  // Publishing again, granting an attempt, accepting findings, approving a wider scope or
  // retrying CI takes two clicks: the first arms the button and says what happens, the second
  // sends it. Cancel disarms it. The pull request link shows in the run header.
  function confirmSection(step, runId, spec) {
    const section = element("div", "next-step-section");
    const note = section.appendChild(element("p", "retry-note", spec.note));
    note.hidden = true;
    const button = section.appendChild(actionButton(spec.label, "button"));
    const cancel = section.appendChild(actionButton("Cancel", "button"));
    cancel.hidden = true;
    function arm(armed) {
      button.dataset.armed = String(armed);
      button.textContent = armed ? spec.confirmLabel : spec.label;
      note.hidden = !armed;
      cancel.hidden = !armed;
    }
    button.addEventListener("click", function () {
      if (button.dataset.armed !== "true") {
        arm(true);
        return;
      }
      void sendAction({
        runId: runId,
        action: spec.action,
        payload: contextPayload(step),
        controls: [button, cancel]
      });
    });
    cancel.addEventListener("click", function () {
      arm(false);
      button.focus();
    });
    return section;
  }

  // The two-click actions, by next step kind.
  const CONFIRM_SECTIONS = {
    retry: {
      action: RETRY_ACTION,
      label: "Retry publishing",
      confirmLabel: "Confirm retry publishing",
      note: "The factory pushes the reviewed work again and opens or updates its pull request."
    },
    grant: {
      action: GRANT_ACTION,
      label: "Grant +1 attempt",
      confirmLabel: "Confirm +1 attempt",
      note: "The factory makes one more implementation attempt with the last failure. " +
        "If it fails, the run stops here again."
    },
    accept: {
      action: ACCEPT_ACTION,
      label: "Accept with findings",
      confirmLabel: "Confirm accept with findings",
      note: "The factory publishes the reviewed work with the open findings accepted. " +
        "The pull request lists them and gets the factory:needs-look label, so it is not merged."
    },
    widen: {
      action: WIDEN_ACTION,
      label: "Approve wider scope",
      confirmLabel: "Confirm wider scope",
      note: "The listed files join the approved scope of this run. The factory checks the " +
        "same work again and goes on to review. A new file outside the scope stops it again."
    },
    repair: {
      action: REPAIR_ACTION,
      label: "Retry CI repair",
      confirmLabel: "Confirm CI repair retry",
      note: "If CI ran out of time, the factory watches it again. If CI failed, the factory " +
        "makes one more CI repair attempt and pushes the fix. The pull request must still be " +
        "open on the same commit. If CI fails again, the run stops here again."
    }
  };

  const SHOW_REASON_LABEL = "Show full failure reason";
  const RECORD_QUERY = /^(attempt|call)=[1-9]\d{0,3}$/;

  // ---- Run detail: stop (ADR-047) -----------------------------------------

  const STOP_LABEL = "Stop run";
  const STOP_CONFIRM_LABEL = "Confirm stop";
  const STOP_SENT_LABEL = "Stop requested";

  function stopControls() {
    return {
      box: document.getElementById("run-stop"),
      button: document.getElementById("run-stop-button"),
      cancel: document.getElementById("run-stop-cancel"),
      note: document.getElementById("run-stop-note")
    };
  }

  // Stopping takes two clicks, like publishing again: the first arms the button and says
  // what happens, the second sends it. Keep running disarms it.
  function armStop(armed) {
    const { button, cancel, note } = stopControls();
    button.dataset.armed = String(armed);
    button.textContent = armed ? STOP_CONFIRM_LABEL : STOP_LABEL;
    note.hidden = !armed;
    cancel.hidden = !armed;
  }

  function resetStop() {
    const { button, cancel } = stopControls();
    armStop(false);
    setDisabled([button, cancel], false);
  }

  // "Stop requested 3 min ago" while a stop waits for the controller (ADR-049), or null.
  function stopRequestedText(stopRequest, nowMs) {
    const at = stopRequest?.created_at;
    return at ? STOP_SENT_LABEL + " " + relativeTimeText(at, nowMs) : null;
  }

  // Shown only on a run that is still working. Another run, or a run that stopped,
  // starts again from an unarmed button. A waiting stop keeps the button disabled, so a
  // reload does not offer a second one.
  function renderStopControl(detail, runId) {
    const { box, button, cancel } = stopControls();
    const show = runId !== null && isActiveRun(detail);
    const key = show ? runId : "";
    const pending = show ? stopRequestedText(detail.stop_request, Date.now()) : null;
    if (box.dataset.runId !== key || (pending === null && box.dataset.pending === "true")) {
      box.dataset.runId = key;
      resetStop();
    }
    box.dataset.pending = String(pending !== null);
    if (pending !== null) {
      armStop(false);
      button.textContent = pending;
      setDisabled([button, cancel], true);
    }
    box.hidden = !show;
  }

  function onStopClick() {
    const { box, button, cancel } = stopControls();
    if (button.dataset.armed !== "true") {
      armStop(true);
      return;
    }
    void sendAction({
      runId: box.dataset.runId,
      action: STOP_ACTION,
      payload: {},
      controls: [button, cancel],
      onAccepted: function () {
        armStop(false);
        button.textContent = STOP_SENT_LABEL;
      }
    });
  }

  function bindStopControl() {
    const { button, cancel } = stopControls();
    button.addEventListener("click", onStopClick);
    cancel.addEventListener("click", function () {
      armStop(false);
      button.focus();
    });
  }

  // ---- Run detail: steer (ADR-047, dashboard X5) ----------------------------

  const STEER_ACTIVE_HINT = "The agent reads this note at its next call, not in the middle of one.";
  const STEER_HALTED_HINT = "The agent reads this note at its next call, once the run resumes.";
  const NOT_RESUMABLE = "NOT_RESUMABLE";
  const STEER_SEND_LABEL = "Send note";
  const STEER_REPLACE_LABEL = "Replace note";

  // The waiting note the next agent call reads, and the send label: a new note replaces
  // the waiting one (ADR-051).
  function steerWaitingView(steerNote, nowMs) {
    if (!steerNote?.note) {
      return { waiting: "", sendLabel: STEER_SEND_LABEL };
    }
    const sent = relativeTimeText(steerNote.created_at, nowMs);
    return {
      waiting: "Waiting for the next agent call, sent " + sent + ": " + steerNote.note,
      sendLabel: STEER_REPLACE_LABEL
    };
  }

  function steerControls() {
    return {
      box: document.getElementById("run-steer"),
      note: document.getElementById("run-steer-note"),
      hint: document.getElementById("run-steer-hint"),
      waiting: document.getElementById("run-steer-waiting"),
      send: document.getElementById("run-steer-send"),
      status: document.getElementById("run-steer-status")
    };
  }

  // A halt that cannot resume has no later agent call, so a note there would never be read.
  function haltTakesSteerNote(detail) {
    const classification = detail.next_step?.resume_classification;
    return detail.outcome?.kind === "needs_you" && Boolean(classification) &&
      classification !== NOT_RESUMABLE;
  }

  // Shown on a working run and on a halt that can resume. Another run starts from an empty
  // note.
  function renderSteerControl(detail, runId) {
    const controls = steerControls();
    const halted = haltTakesSteerNote(detail);
    const show = runId !== null && (halted || isActiveRun(detail));
    const key = show ? runId : "";
    if (controls.box.dataset.runId !== key) {
      controls.box.dataset.runId = key;
      controls.note.value = "";
      controls.status.textContent = "";
      setDisabled([controls.note, controls.send], false);
    }
    controls.hint.textContent = halted ? STEER_HALTED_HINT : STEER_ACTIVE_HINT;
    const view = steerWaitingView(detail.steer_note, Date.now());
    controls.waiting.textContent = view.waiting;
    controls.waiting.hidden = view.waiting === "";
    controls.send.textContent = view.sendLabel;
    controls.box.hidden = !show;
  }

  function onSteerSend() {
    const { box, note, send, status } = steerControls();
    status.textContent = "";
    const replacing = send.textContent === STEER_REPLACE_LABEL;
    void sendAction({
      runId: box.dataset.runId,
      action: STEER_ACTION,
      payload: { note: note.value },
      controls: [note, send],
      onAccepted: function () {
        note.value = "";
        setDisabled([note, send], false);
        status.textContent = replacing
          ? "Note replaced for the next agent call."
          : "Note saved for the next agent call.";
      }
    });
  }

  function bindSteerControl() {
    steerControls().send.addEventListener("click", onSteerSend);
  }

  // ---- Run detail: escalate (ADR-055) ---------------------------------------

  function escalateControls() {
    return {
      box: document.getElementById("run-escalate"),
      note: document.getElementById("run-escalate-note"),
      send: document.getElementById("run-escalate-send"),
      status: document.getElementById("run-escalate-status")
    };
  }

  // Shown while the server says the run can escalate, and names the route it moves to.
  function renderEscalateControl(detail, runId) {
    const controls = escalateControls();
    const route = typeof detail.escalation_route === "string" ? detail.escalation_route : "";
    const key = runId !== null && route !== "" ? runId : "";
    if (controls.box.dataset.runId !== key) {
      controls.box.dataset.runId = key;
      controls.note.value = "";
      controls.status.textContent = "";
      setDisabled([controls.note, controls.send], false);
    }
    controls.send.textContent = "Escalate to " + route;
    controls.box.hidden = key === "";
  }

  function onEscalateSend() {
    const { box, note, send, status } = escalateControls();
    status.textContent = "";
    void sendAction({
      runId: box.dataset.runId,
      action: ESCALATE_ACTION,
      payload: { note: note.value },
      controls: [note, send]
    });
  }

  // "Escalated SINGLE -> CRITIQUE · <time> · <note> · failed: <error>".
  function escalationText(escalation) {
    const from = displayValue(escalation.from_route, "");
    const to = displayValue(escalation.to_route, "");
    let outcome = "running";
    if (escalation.status === "done") {
      outcome = "finished";
    } else if (escalation.status === "failed") {
      outcome = "failed: " + displayValue(escalation.error, "no reason given");
    }
    return [
      to === "" ? "Escalation" : "Escalated " + from + " \u2192 " + to,
      absoluteTimeText(escalation.started_at, ""),
      displayValue(escalation.note, ""),
      outcome
    ].filter(Boolean).join(" \u00b7 ");
  }

  // The last escalation of the run at the top of the summary, in the tone of how it went.
  function renderEscalationLine(escalation) {
    const line = document.getElementById("run-escalation");
    const tone = isPlainObject(escalation) ? ESCALATION_TONES[escalation.status] : undefined;
    line.hidden = tone === undefined;
    if (!line.hidden) {
      line.className = "headline headline-" + tone;
      setText(line, escalationText(escalation));
    }
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
    if (Object.hasOwn(CONFIRM_SECTIONS, step.kind)) {
      return confirmSection(step, runId, CONFIRM_SECTIONS[step.kind]);
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
      failureSection(step, runId),
      staleLine(step.stale_sentence),
      resumeClassificationLine(step),
      reopensLine(step),
      approvalScopeSection(step.approval_scope),
      step.kind === "answer" ? null : decisionsSection(step.decisions),
      scopePathsSection(step.scope_paths),
      actionSection(step, runId),
      commandSection(step.cli_command),
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

  // The callout heading says what to do. The warn border is only for a step the page can act on.
  const STEP_HEADINGS = new Map([
    ["approve", "Approve to continue"],
    ["answer", "Answer to continue"],
    ["retry", "Retry to continue"],
    ["grant", "Grant an attempt to continue"],
    ["accept", "Accept the findings to continue"],
    ["widen", "Approve a wider scope to continue"],
    ["repair", "Retry CI to continue"],
    ["queued", "Queued"],
    ["cannot_continue", "Cannot continue"],
    ["remote_approval_unavailable", "Inspect the run"]
  ]);
  const ACTION_STEP_KINDS = new Set([
    "approve", "answer", "retry", "grant", "accept", "widen", "repair"
  ]);

  function renderNextStepFrame(panel, step) {
    const heading = STEP_HEADINGS.get(step.kind) || "Needs you";
    setText(document.getElementById("next-step-heading"), heading);
    const hasAction = ACTION_STEP_KINDS.has(step.kind);
    panel.className = "card next-step" + (hasAction ? " next-step-action" : "");
  }

  // The panel is rebuilt only when the step changes, so a refresh keeps the
  // focused copy button and its "Copied" status.
  // While the operator types in the panel, a changed step leaves the panel as it is and
  // a line says the run changed; the rest of the run view keeps refreshing.
  function renderNextStep(detail, runId) {
    const panel = document.getElementById("next-step");
    const body = document.getElementById("next-step-body");
    const step = detail.next_step;
    const visible = isStepVisible(step);
    const loaded = fullReasonFor(runId, detail) !== null;
    const signature = visible ? JSON.stringify([runId, step, loaded]) : "";
    const changed = (panel.dataset.signature ?? "") !== signature;
    const frozen = changed && isDirty(panel);
    document.getElementById("next-step-changed").hidden = !frozen;
    if (frozen) {
      return;
    }
    panel.hidden = !visible;
    if (!visible) {
      clearChildren(body);
      delete panel.dataset.signature;
      return;
    }
    renderNextStepFrame(panel, step);
    if (changed) {
      panel.dataset.signature = signature;
      buildNextStep(body, step, runId);
    }
  }

  // ---- Run detail: render ------------------------------------------------

  function knownRunId(detail) {
    const runId = runIdOf(detail);
    return typeof runId === "string" && RUN_ID_PATTERN.test(runId) ? runId : null;
  }

  // The key numbers under the title: what the run took and cost. Only a cost unit the
  // calls reported gets a card, and one card says so when none did.
  function keyNumberCards(totals) {
    const [calls, , callTime] = headlineCards(totals);
    // The wall-clock time is in the status line; this card is the time spent in calls.
    const duration = { ...callTime, label: "Agent time" };
    const tokens = figureCard("Tokens", totals.total_tokens, totals.calls, displayNumber);
    const costs = costCards(totals).filter(function (card) {
      return card.value !== NOT_REPORTED;
    });
    const cost = { label: "Cost", value: NOT_REPORTED, note: "", help: "" };
    return [duration, calls, tokens, ...(costs.length === 0 ? [cost] : costs)];
  }

  function tickPart(prefix, since, nowMs) {
    return { text: prefix + elapsedText(since, nowMs), prefix: prefix, tickSince: since };
  }

  // The live tail of the running call: its last tool, with the latest ones in the tooltip.
  function liveToolPart(active) {
    const lines = asArray(active.tool_calls).map(toolLine);
    if (lines.length === 0) {
      return null;
    }
    const count = Math.max(lines.length, Number(active.tool_call_count) || 0);
    return {
      text: count + (count === 1 ? " tool" : " tools") + " \u00b7 last: " + lines.at(-1),
      title: lines.join("\n"),
      className: "live-tool"
    };
  }

  // The time limit of the running call, a warning once 80% of it has passed.
  function timeoutPart(active, nowMs) {
    const limitMs = Number(active.timeout_seconds) * MS_PER_SECOND;
    if (!Number.isFinite(limitMs) || limitMs <= 0) {
      return null;
    }
    const late = nowMs - Date.parse(active.started_at) >= TIMEOUT_WARN_SHARE * limitMs;
    return {
      text: "/ timeout " + durationText(limitMs),
      title: late ? "Over 80% of the call time limit has passed" : "The call stops at this limit",
      className: late ? "status-warn" : ""
    };
  }

  // The running timers of an active run: since its start, in its stage and in its
  // active call, then the idle time. A stopped run says how long it took.
  function timeParts(detail, nowMs) {
    if (!isActiveRun(detail)) {
      const took = elapsedText(detail.created_at, stoppedAtMs(detail));
      return took === EMPTY_VALUE ? [] : [{ text: "Took " + took }];
    }
    const parts = [tickPart("Running ", detail.created_at, nowMs)];
    if (typeof detail.state_started_at === "string") {
      parts.push(tickPart("in stage ", detail.state_started_at, nowMs));
    }
    const active = detail.active_invocation;
    if (isPlainObject(active) && typeof active.started_at === "string") {
      const role = displayValue(active.role, "active").toLowerCase();
      parts.push(tickPart(role + " call ", active.started_at, nowMs));
      const limit = timeoutPart(active, nowMs);
      if (limit !== null) {
        parts.push(limit);
      }
    }
    const tools = isPlainObject(active) ? liveToolPart(active) : null;
    if (tools !== null) {
      parts.push(tools);
    }
    const idle = idlePhrase(detail);
    // The idle text changes each minute, so it is patched in place, not rebuilt.
    return idle === "" ? parts : [...parts, { text: idle, live: true }];
  }

  // The line under the headline: chips, the pull request link and the times.
  function statusParts(detail, nowMs) {
    const parts = runChips(detail, true).map(function (chip) {
      const className = chip.failed ? "chip chip-failed" : "chip";
      return { text: chip.text, title: chip.title, className: className };
    });
    const pr = pullRequestCell(detail);
    if (pr.href !== undefined) {
      parts.push({ text: pr.value + pr.hidden, href: pr.href, title: pr.title });
    }
    return [...parts, ...timeParts(detail, nowMs)];
  }

  function statusNode(part) {
    const node = part.href === undefined ? element("span") : createLink(part.href);
    node.className = part.className || "";
    setText(node, part.text);
    setTitle(node, part.title);
    syncTick(node, part.tickSince, part.prefix);
    return node;
  }

  // Rebuilt only when a part changes, not on every poll, so a focused link stays.
  // A ticking part keys on its start time and a live part on its place, not on its text.
  function statusKeyOf(part) {
    const text = part.live ? "live" : part.prefix || part.text;
    return [part.href, part.title, part.className, part.tickSince, text];
  }

  function renderStatusLine(detail) {
    const line = document.getElementById("run-status-line");
    const parts = statusParts(detail, Date.now());
    const key = JSON.stringify(parts.map(statusKeyOf));
    line.hidden = parts.length === 0;
    if (line.dataset.key !== key) {
      line.dataset.key = key;
      line.replaceChildren(...parts.map(statusNode));
      return;
    }
    parts.forEach(function (part, index) {
      if (part.live) {
        setText(line.children[index], part.text);
      }
    });
  }

  // ---- Run detail: stage stepper (ADR-067) -------------------------------

  // The six stages a run moves through, and the states that belong to each.
  const STAGES = [
    ["Triage", ["TRIAGING"]],
    ["Plan", ["PLANNING", "REFINING", "RESEARCHING"]],
    ["Implement", ["IMPLEMENTING"]],
    ["Verify", ["VERIFYING"]],
    ["Review", ["REVIEWING"]],
    ["Deliver", ["PR_READY", "PR_CREATED", "CI_RUNNING", "CI_DIAGNOSIS"]]
  ];

  function stageIndex(stateName) {
    return STAGES.findIndex(function ([, states]) { return states.includes(stateName); });
  }

  // Adds each stage's entries and time from the run's state moves. When the oldest
  // moves were dropped (or the run began before moves were kept), the time before the
  // first kept move is unknown, so it is not counted. Returns the time of the last move.
  function tallyStageMoves(steps, moves, createdAt) {
    let since = moves[0].from_state === "CREATED" ? Date.parse(createdAt) : Number.NaN;
    for (const move of moves) {
      const at = Date.parse(move.at);
      const from = stageIndex(move.from_state);
      const to = stageIndex(move.to_state);
      if (from >= 0 && Number.isFinite(since) && Number.isFinite(at)) {
        steps[from].ms += Math.max(0, at - since);
      }
      if (to >= 0 && to !== from) {
        steps[to].entries += 1;
      }
      since = at;
    }
    return since;
  }

  // Marks the stage the run is in as "current" (adding its time so far), or the stage
  // a stopped run halted in as "halted".
  function markLiveStage(steps, detail, lastMoveMs, nowMs) {
    const current = stageIndex(detail.state);
    if (current >= 0 && detail.is_finished !== true) {
      const started = Date.parse(detail.state_started_at);
      steps[current].status = "current";
      steps[current].ms += Math.max(0, nowMs - (Number.isFinite(started) ? started : lastMoveMs));
    } else if (detail.state === "NEEDS_HUMAN" || detail.state === "FAILED") {
      const halted = stageIndex(asArray(detail.transitions).at(-1).from_state);
      if (halted >= 0) {
        steps[halted].status = "halted";
      }
    }
  }

  // Each stage's entries, time and status from the run's state moves. A stage the run
  // left is "done", the one it is in is "current", and the one it stopped in is "halted".
  // A run recorded before the moves were kept gets no stages.
  function stageSteps(detail, nowMs) {
    const moves = asArray(detail.transitions);
    if (moves.length === 0) {
      return [];
    }
    const steps = STAGES.map(function ([label]) {
      return { label: label, entries: 0, ms: 0, status: "todo" };
    });
    const lastMoveMs = tallyStageMoves(steps, moves, detail.created_at);
    for (const step of steps) {
      step.status = step.entries > 0 ? "done" : "todo";
    }
    markLiveStage(steps, detail, lastMoveMs, nowMs);
    return steps.map(function (step) {
      const entered = step.entries > 1 ? " \u00d7" + step.entries : "";
      return {
        text: step.label + entered,
        time: step.entries > 0 ? durationText(Math.round(step.ms)) : "",
        status: step.status
      };
    });
  }

  function renderStages(detail) {
    const list = document.getElementById("run-stages");
    const steps = stageSteps(detail, Date.now());
    list.hidden = steps.length === 0;
    list.replaceChildren(...steps.map(function (step) {
      const item = element("li", "stage stage-" + step.status);
      item.appendChild(element("span", "stage-name", step.text));
      item.appendChild(element("span", "stage-time", step.time));
      return item;
    }));
  }

  // The page heading is the run title, and the run id sits under it. With no title the
  // id is the heading and the line under it is hidden, so neither shows twice.
  function renderRunHeading(detail, runId) {
    const title = displayValue(detail.title, "");
    setText(document.getElementById("run-detail-heading"), title || runId || "Run");
    const idLine = document.getElementById("run-id");
    setText(idLine, runId || "");
    idLine.hidden = title === "" || !runId;
  }

  // The summary card: state, the one outcome line and the key numbers. The
  // outcome line and the badge come from the server and always name the state in words.
  function renderRunSummary(detail, runId) {
    const kind = badgeKind(detail.outcome);
    renderRunHeading(detail, runId);
    const badge = document.getElementById("run-badge");
    // Filled only when the callout below has an action, not only a command.
    const filled = ACTION_STEP_KINDS.has(detail.next_step?.kind) ? " badge-filled" : "";
    badge.className = "badge badge-" + kind + filled;
    setText(badge, displayValue(detail.outcome?.label, displayValue(detail.state)));
    const headline = document.getElementById("run-headline");
    headline.className = "headline headline-" + badgeKind(detail.headline);
    setText(headline, displayValue(detail.headline?.text, ""));
    // A waiting run says why in the "Needs you" callout, so the headline would repeat it.
    headline.hidden = headline.textContent === "" || isStepVisible(detail.next_step);
    renderEscalationLine(detail.operator_escalation);
    renderStatusLine(detail);
    renderStages(detail);
    renderPullRequestLink(detail.pull_request_url);
    renderStopControl(detail, runId);
    renderSteerControl(detail, runId);
    renderEscalateControl(detail, runId);
    renderFullReason(detail, runId);
    syncStats(document.getElementById("run-key-numbers"), keyNumberCards(detail.totals || {}));
  }

  function renderPullRequestLink(url) {
    const link = document.getElementById("run-pr-link");
    link.hidden = !isHttpsUrl(url);
    if (!link.hidden && link.getAttribute("href") !== url) {
      link.href = url;
    }
  }

  // The loaded text fits only while the run still shows the same cut reason.
  function matchingFullReason(cached, runId, detail) {
    const fits = cached?.runId === runId && detail.failure_reason_truncated === true &&
      detail.failure_reason === cached.cut;
    return fits ? cached.text : null;
  }

  // With the run detail, a reason that changed or cleared drops the loaded text.
  function fullReasonFor(runId, detail) {
    if (detail !== undefined && matchingFullReason(state.fullReason, runId, detail) === null) {
      state.fullReason = null;
    }
    return state.fullReason?.runId === runId ? state.fullReason.text : null;
  }

  // A cut run reason offers "Show full failure reason" under the badge. A "Needs you"
  // callout shows the reason itself, so then the button sits there instead.
  function renderFullReason(detail, runId) {
    const full = fullReasonFor(runId, detail);
    state.shownReason = detail.failure_reason;
    const inCallout = isStepVisible(detail.next_step) &&
      typeof detail.next_step.failure_reason === "string";
    const cut = detail.failure_reason_truncated === true && runId !== null && !inCallout;
    document.getElementById("run-reason-more").hidden = !cut || full !== null;
    const text = document.getElementById("run-reason-full");
    text.hidden = full === null || inCallout;
    setText(text, full ?? "");
  }

  function reasonPath(runId) {
    return runDetailPath(runId) + "/reason";
  }

  // Loads the whole reason once for the open run, then draws the run again with it.
  function showFullReason(button) {
    const runId = state.runId;
    const cut = state.shownReason;
    button.disabled = true;
    apiFetch(reasonPath(runId))
      .then(function (body) {
        if (state.runId === runId && typeof body.failure_reason === "string") {
          state.fullReason = { runId: runId, cut: cut, text: body.failure_reason };
          void refreshView();
        }
      }, function () {
        setActionMessage("The full failure reason could not load.");
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  // ---- Run detail: attempt and call reasons (ADR-062) ---------------------

  // The reason to show for one attempt or call: the whole text once loaded for the same
  // cut text, and whether "Show full failure reason" can still load it. A record without
  // a number cannot be asked for.
  function recordReason(runId, query, reason, truncated) {
    const cut = truncated === true && typeof reason === "string" && runId !== null &&
      RECORD_QUERY.test(query);
    const cached = cut ? state.recordReasons[runId + "?" + query] : undefined;
    const full = cached?.cut === reason ? cached.text : null;
    return { text: full ?? reason, query: query, shown: reason, cut: cut && full === null };
  }

  function reasonButton() {
    const button = actionButton(SHOW_REASON_LABEL, "button");
    button.className = "reason-more";
    button.hidden = true;
    button.addEventListener("click", function () {
      showRecordReason(button);
    });
    return button;
  }

  function syncReasonButton(button, reason) {
    button.hidden = !reason.cut;
    button.dataset.query = reason.query;
    button.dataset.shown = reason.cut ? reason.shown : "";
  }

  // A cut attempt reason keeps its text in the first span and its button after it.
  function patchReasonCell(cell, entry) {
    if (cell.firstElementChild?.className !== "reason-value") {
      clearChildren(cell);
      cell.append(element("span", "reason-value"), reasonButton());
    }
    setText(cell.firstElementChild, displayValue(entry.value));
    syncReasonButton(cell.lastElementChild, entry.reason);
    cell.className = entry.className || "";
    setTitle(cell, undefined);
  }

  // Loads one attempt's or call's whole reason, then draws the run again with it.
  function showRecordReason(button) {
    const runId = state.runId;
    const query = button.dataset.query;
    const cut = button.dataset.shown;
    button.disabled = true;
    apiFetch(reasonPath(runId) + "?" + query)
      .then(function (body) {
        if (state.runId === runId && typeof body.failure_reason === "string") {
          state.recordReasons[runId + "?" + query] = { cut: cut, text: body.failure_reason };
          void refreshView();
        }
      }, function () {
        setActionMessage("The full failure reason could not load.");
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  // The details list leaves out a row that has nothing to show.
  function isBlankField(field) {
    const value = field[1];
    return value === undefined || value === null || value === "" ||
      value === EMPTY_VALUE || value === NOT_REPORTED;
  }

  function renderRunDetail(detail) {
    const runId = knownRunId(detail);
    state.runDetail = detail;
    renderCompletion();
    const fields = [
      ...identityFields(detail),
      ...reasonFields(detail.failure_reason, detail.failure_reason_truncated, runId),
      ...usageFields(detail.usage || {}),
      ...guidanceFields(detail),
      ...evidenceFields(detail),
      ...escalationFields(detail),
      ...referenceFields(detail)
    ];
    renderRunSummary(detail, runId);
    syncDefinitionList(
      document.getElementById("run-detail-body"),
      fields.filter(function (field) { return !isBlankField(field); })
    );
    syncRows(
      document.getElementById("attempts-body"), asArray(detail.attempts).map(function (attempt) {
        return attemptRowSpec(attempt, runId);
      })
    );
    syncStats(document.getElementById("run-totals"), totalsCards(detail.totals || {}));
    renderTimeline(detail, runId);
    renderNextStep(detail, runId);
    setRunDetailStatus(null);
  }

  // ---- Checks and review (ADR-047) ---------------------------------------

  // One check line: the outcome word, the exit code and the time it took.
  function checkStatusText(check) {
    let word = "failed";
    if (check.passed === true) {
      word = "passed";
    } else if (check.timed_out === true) {
      word = "timed out";
    }
    const parts = [word];
    if (isFiniteNumber(check.exit_code)) {
      parts.push("exit " + check.exit_code);
    }
    if (isFiniteNumber(check.duration_seconds)) {
      parts.push(durationText(Math.round(check.duration_seconds * MS_PER_SECOND)));
    }
    return parts.join(" · ");
  }

  // A check is a disclosure: its label and status, then its output tail. A failed
  // check opens by default, since its output is what the operator came for.
  // tone: "done" (passed), "failed" (open by default) or "" (neutral, e.g. pending CI).
  function evidenceItem(label, status, tone, output) {
    const item = element("details", "evidence-item");
    item.open = tone === "failed";
    const summary = item.appendChild(element("summary", ""));
    summary.appendChild(element("span", tone ? "badge badge-" + tone : "badge", status));
    summary.appendChild(element("code", "evidence-label", label));
    item.appendChild(element("pre", "evidence-output", output || "No output."));
    return item;
  }

  function checkNode(check) {
    return evidenceItem(
      displayValue(check.command),
      checkStatusText(check),
      check.passed === true ? "done" : "failed",
      check.output
    );
  }

  function ciCheckNode(check) {
    const tone = { PASS: "done", FAIL: "failed" }[check.status] || "";
    const status = [check.status, check.failure_category].filter(Boolean).join(" · ");
    const item = evidenceItem(displayValue(check.name), status || EMPTY_VALUE, tone, check.output);
    if (typeof check.description === "string" && check.description !== "") {
      item.insertBefore(element("p", "evidence-note", check.description), item.lastChild);
    }
    if (isHttpsUrl(check.details_url)) {
      item.insertBefore(createLink(check.details_url), item.lastChild);
    }
    return item;
  }

  function evidenceGroup(container, title, nodes) {
    if (nodes.length === 0) {
      return;
    }
    container.appendChild(element("h3", "", title));
    for (const node of nodes) {
      container.appendChild(node);
    }
  }

  // Every attempt that saved verification or CI checks, oldest first.
  function checkAttempts(payload) {
    const numbers = asArray(payload.verification_history).concat(asArray(payload.ci_history))
      .map(function (entry) { return entry.attempt; });
    return Array.from(new Set(numbers)).sort(function (a, b) { return a - b; });
  }

  // The checks of one attempt; null is the latest saved reports.
  function checksForAttempt(payload, attempt) {
    if (attempt === null) {
      return { checks: asArray(payload.checks), ci_checks: asArray(payload.ci_checks) };
    }
    function picked(history) {
      const entry = asArray(history).find(function (item) { return item.attempt === attempt; });
      return entry ? asArray(entry.checks) : [];
    }
    return { checks: picked(payload.verification_history), ci_checks: picked(payload.ci_history) };
  }

  function renderChecksPicker(attempts) {
    const select = document.getElementById("checks-attempt");
    document.getElementById("checks-attempt-label").hidden = attempts.length < 2;
    clearChildren(select);
    select.appendChild(new Option("Latest", ""));
    for (const number of attempts) {
      select.appendChild(new Option(String(number), String(number)));
    }
    select.value = state.checksAttempt === null ? "" : String(state.checksAttempt);
  }

  function renderChecks(body, payload) {
    const attempts = checkAttempts(payload);
    if (!attempts.includes(state.checksAttempt)) {
      state.checksAttempt = null;
    }
    renderChecksPicker(attempts);
    const shown = checksForAttempt(payload, state.checksAttempt);
    evidenceGroup(body, "Verification", shown.checks.map(checkNode));
    evidenceGroup(body, "CI", shown.ci_checks.map(ciCheckNode));
    if (body.childElementCount === 0 && attempts.length > 0) {
      body.appendChild(element("p", "evidence-note", "This attempt saved no checks."));
    }
    return body.childElementCount > 0;
  }

  // "path:12-14", or "path:12" for one line.
  function locationText(location) {
    const end = location.end_line === location.start_line ? "" : "-" + location.end_line;
    return location.path + ":" + location.start_line + end;
  }

  function findingNode(finding) {
    const item = element("li", "finding");
    const head = [finding.id, finding.category].filter(Boolean).join(" · ");
    if (head !== "") {
      item.appendChild(element("span", "chip", head));
    }
    item.appendChild(element("span", "finding-message", displayValue(finding.message)));
    const places = asArray(finding.locations).map(locationText).join(", ");
    if (places !== "") {
      item.appendChild(element("code", "finding-where", places));
    }
    return item;
  }

  function findingList(container, title, findings) {
    const items = asArray(findings);
    if (items.length === 0) {
      return;
    }
    container.appendChild(element("h4", "", title));
    const list = container.appendChild(element("ul", "findings"));
    for (const finding of items) {
      list.appendChild(findingNode(finding));
    }
  }

  function textList(container, title, texts) {
    const items = asArray(texts);
    if (items.length === 0) {
      return;
    }
    container.appendChild(element("h4", "", title));
    const list = container.appendChild(element("ul", "findings"));
    for (const text of items) {
      list.appendChild(element("li", "", text));
    }
  }

  const DISPOSITION_WORDS = { RESOLVED: "fixed", UNRESOLVED: "not fixed", WITHDRAWN: "withdrawn" };

  // Each finding with its life across rounds: the attempt that raised it, then
  // what each later round said about it, then its place in the ledger now.
  function findingThreads(payload) {
    const threads = new Map();
    for (const round of asArray(payload.rounds)) {
      const raised = asArray(round.blocking_findings).concat(asArray(round.repair_regressions));
      for (const finding of raised) {
        if (finding.id && !threads.has(finding.id)) {
          threads.set(finding.id, { finding: finding, events: [{ attempt: round.attempt_number, word: "raised" }] });
        }
      }
      for (const item of asArray(round.prior_finding_dispositions)) {
        const thread = threads.get(item.finding_id);
        if (thread) {
          const word = DISPOSITION_WORDS[item.status] || displayValue(item.status);
          thread.events.push({ attempt: round.attempt_number, word: word, rationale: item.rationale });
        }
      }
    }
    const ledger = [["open", payload.open_findings], ["accepted", payload.accepted_findings]];
    return Array.from(threads.values(), function (thread) {
      const match = ledger.find(function (entry) {
        return asArray(entry[1]).some(function (item) { return item.id === thread.finding.id; });
      });
      return { ...thread, ledger: match ? match[0] : null };
    });
  }

  // "raised attempt 1 · fixed attempt 2"
  function threadLifeText(thread) {
    return thread.events.map(function (event) {
      return event.word + " attempt " + displayValue(event.attempt);
    }).join(" · ");
  }

  function threadNode(thread) {
    const item = findingNode(thread.finding);
    const life = element("span", "finding-life", threadLifeText(thread));
    if (thread.ledger) {
      life.appendChild(element("span", "badge", thread.ledger));
    }
    item.insertBefore(life, item.querySelector(".finding-where"));
    for (const event of thread.events) {
      if (typeof event.rationale === "string" && event.rationale !== "") {
        item.appendChild(element("p", "evidence-note", "Attempt " + event.attempt + ": " + event.rationale));
      }
    }
    return item;
  }

  function reviewVerdict(round) {
    if (round.skipped === true) {
      return { text: "skipped", passed: true };
    }
    return round.approved === true
      ? { text: "approved", passed: true }
      : { text: "changes requested", passed: false };
  }

  // One review round as a card: its verdict, then each list it holds.
  function roundNode(round, open) {
    const verdict = reviewVerdict(round);
    const item = element("details", "evidence-item review-round");
    item.open = open;
    const summary = item.appendChild(element("summary", ""));
    const kind = verdict.passed ? "badge-done" : "badge-failed";
    summary.appendChild(element("span", "badge " + kind, verdict.text));
    summary.appendChild(element("span", "evidence-label", "Attempt " + displayValue(round.attempt_number)));
    if (typeof round.skip_reason === "string") {
      item.appendChild(element("p", "evidence-note", round.skip_reason));
    }
    findingList(item, "Blocking findings", round.blocking_findings);
    findingList(item, "Regressions", round.repair_regressions);
    textList(item, "Suggested changes", round.suggested_changes);
    textList(item, "Findings", round.findings);
    textList(item, "Scope concerns", round.scope_concerns);
    textList(item, "Security concerns", round.security_concerns);
    textList(item, "Compatibility concerns", round.compatibility_concerns);
    return item;
  }

  // The finding history, then the newest round first, and only it opens.
  function renderReview(body, payload) {
    findingList(body, "Open findings", payload.open_findings);
    findingList(body, "Accepted findings", payload.accepted_findings);
    const threads = findingThreads(payload);
    if (threads.length > 0) {
      body.appendChild(element("h4", "", "Finding history"));
      const list = body.appendChild(element("ul", "findings"));
      threads.forEach(function (thread) { list.appendChild(threadNode(thread)); });
    }
    const rounds = asArray(payload.rounds).slice().reverse();
    rounds.forEach(function (round, index) {
      body.appendChild(roundNode(round, index === 0));
    });
    return body.childElementCount > 0;
  }

  // ---- Plan, changes and result (ADR-047) --------------------------------

  function textBlock(container, title, text) {
    if (typeof text !== "string" || text === "") {
      return;
    }
    container.appendChild(element("h3", "", title));
    container.appendChild(element("p", "plan-text", text));
  }

  function planStepNode(step) {
    const item = element("li", "plan-step", displayValue(step.goal));
    const files = asArray(step.likely_files).join(", ");
    if (files !== "") {
      item.appendChild(element("code", "finding-where", files));
    }
    return item;
  }

  // "src, tests · 2-5 files", from the plan's expected scope.
  function scopeText(scope) {
    if (!scope) {
      return "";
    }
    const parts = [asArray(scope.modules).join(", ")];
    if (isFiniteNumber(scope.estimated_files_min) && isFiniteNumber(scope.estimated_files_max)) {
      parts.push(scope.estimated_files_min + "-" + scope.estimated_files_max + " files");
    }
    return parts.filter(Boolean).join(" · ");
  }

  function renderPlanSection(body, plan) {
    textBlock(body, "Plan", plan.summary);
    const steps = asArray(plan.steps);
    if (steps.length > 0) {
      const list = body.appendChild(element("ol", "plan-steps"));
      steps.forEach(function (step) { list.appendChild(planStepNode(step)); });
    }
    textBlock(body, "Expected scope", scopeText(plan.expected_scope));
    textList(body, "Open decisions", plan.unresolved_decisions);
    textList(body, "Risks", plan.risks);
  }

  // The goal, the plan and what the agent says it changed.
  function renderPlan(body, payload) {
    const spec = payload.specification;
    if (spec) {
      textBlock(body, "Goal", spec.problem);
      textList(body, "Acceptance criteria", spec.acceptance_criteria);
    }
    if (payload.execution_plan) {
      renderPlanSection(body, payload.execution_plan);
    }
    const changes = payload.change_set;
    if (changes) {
      textBlock(body, "What changed", changes.summary);
      const fileCount = asArray(changes.changed_files).length;
      textBlock(body, "Files changed", fileCount + (fileCount === 1 ? " file" : " files"));
      textList(body, "Outside the planned modules", payload.out_of_scope_paths);
      textList(body, "Tests added", changes.tests_added);
      textList(body, "Commands run", changes.commands_run);
    }
    return body.childElementCount > 0;
  }

  const DIFF_LINE_CLASSES = { "+": "diff-add", "-": "diff-del", "@": "diff-hunk" };

  // ---- Changes: a small syntax colour and the split view -------------------

  // One line at a time, so a string or comment that spans lines is not coloured.
  // The patterns are raw strings, not regex literals, so the test reader can parse them.
  const SYNTAX_STRING = String.raw`"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'`;
  const SYNTAX_NUMBER = String.raw`\b\d+(?:\.\d+)?\b`;
  const SYNTAX_NAME = String.raw`[A-Za-z_$][\w$]*`;
  const JS_WORDS = "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof interface let new null return static switch this throw true try type typeof undefined var void while yield";
  const SYNTAX_RULES = {
    py: { comment: "#.*", words: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return self True try while with yield" },
    js: { comment: String.raw`//.*|/\*.*?\*/`, words: JS_WORDS, extra: "|`[^`]*`" },
    json: { comment: "", words: "true false null" },
    yaml: { comment: "#.*", words: "true false null yes no on off", key: String.raw`^\s*-?\s*[\w.-]+(?=:)` },
    md: { comment: "", words: "", key: "^#{1,6} .*", extra: "|`[^`]*`" }
  };
  // One compiled pattern per language, built on first use.
  const SYNTAX_PATTERNS = new Map();
  const SYNTAX_EXTENSIONS = {
    py: "py", js: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", jsx: "js",
    json: "json", yml: "yaml", yaml: "yaml", md: "md"
  };

  function syntaxLanguage(path) {
    const name = String(path || "").toLowerCase();
    const dot = name.lastIndexOf(".");
    return dot < 0 ? null : SYNTAX_EXTENSIONS[name.slice(dot + 1)] || null;
  }

  // [class, text] pieces of one code line: "syn-kw", "syn-str", "syn-com", "syn-num" or "".
  function syntaxTokens(text, language) {
    const rules = SYNTAX_RULES[language];
    if (!rules) {
      return [["", text]];
    }
    if (!SYNTAX_PATTERNS.has(language)) {
      const groups = [rules.key || "(?!)", rules.comment || "(?!)", SYNTAX_STRING + (rules.extra || ""), SYNTAX_NUMBER, SYNTAX_NAME];
      SYNTAX_PATTERNS.set(language, new RegExp(groups.map(function (group) { return "(" + group + ")"; }).join("|"), "g"));
    }
    const pattern = SYNTAX_PATTERNS.get(language);
    const words = new Set(rules.words.split(" "));
    const pieces = [];
    const push = function (kind, piece) {
      const last = pieces.at(-1);
      if (last && last[0] === kind) {
        last[1] += piece;
      } else if (piece) {
        pieces.push([kind, piece]);
      }
    };
    let index = 0;
    for (const match of text.matchAll(pattern)) {
      push("", text.slice(index, match.index));
      let kind = ["syn-kw", "syn-com", "syn-str", "syn-num"][match.slice(1, 5).findIndex(Boolean)] || "";
      if (match[5] !== undefined) {
        kind = words.has(match[5]) ? "syn-kw" : "";
      }
      push(kind, match[0]);
      index = match.index + match[0].length;
    }
    push("", text.slice(index));
    return pieces;
  }

  // Rows of the split view. Lines before the first hunk and hunk heads span both sides;
  // a run of removed lines pairs, line by line, with the added lines that follow it.
  function splitDiffRows(text) {
    const rows = [];
    let removed = [];
    let added = [];
    let inHunks = false;
    const flush = function () {
      for (let index = 0; index < Math.max(removed.length, added.length); index += 1) {
        rows.push({ left: removed[index] ?? null, right: added[index] ?? null });
      }
      removed = [];
      added = [];
    };
    for (const line of text.split("\n")) {
      inHunks = inHunks || line.startsWith("@@");
      const sign = line[0];
      if (inHunks && sign === "-") {
        if (added.length > 0) {
          flush();
        }
        removed.push(line);
      } else if (inHunks && sign === "+") {
        added.push(line);
      } else {
        flush();
        rows.push(inHunks && sign === " " ? { left: line, right: line } : { full: line });
      }
    }
    flush();
    return rows;
  }

  // One diff line: the sign keeps the add or remove colour, the code is coloured by language.
  function diffLineNode(tag, line, language, inHunks) {
    const sign = line[0];
    const kind = inHunks ? DIFF_LINE_CLASSES[sign] || "" : "diff-meta";
    const node = element(tag, kind);
    if (!inHunks || sign === "@" || sign === "\\") {
      node.textContent = line;
      return node;
    }
    node.appendChild(element("span", "diff-sign", line.slice(0, 1)));
    for (const [cls, piece] of syntaxTokens(line.slice(1), language)) {
      node.appendChild(cls ? element("span", cls, piece) : document.createTextNode(piece));
    }
    return node;
  }

  function fillDiffText(pre, text, language) {
    let inHunks = false;
    for (const line of text.split("\n")) {
      inHunks = inHunks || line.startsWith("@@");
      pre.append(diffLineNode("span", line, language, inHunks), "\n");
    }
  }

  function fillDiffSplit(pre, text, language) {
    let inHunks = false;
    for (const row of splitDiffRows(text)) {
      if (row.full !== undefined) {
        inHunks = inHunks || row.full.startsWith("@@");
        pre.appendChild(diffLineNode("div", row.full, language, inHunks)).classList.add("diff-full");
        continue;
      }
      for (const side of [row.left, row.right]) {
        pre.appendChild(side === null ? element("div", "diff-empty") : diffLineNode("div", side, language, true));
      }
    }
  }

  // The Changes card shows one column, or old and new side by side; the page remembers it.
  const DIFF_VIEW_KEY = "factory-dashboard-diff-view";

  function readDiffSplit() {
    try {
      return globalThis.localStorage.getItem(DIFF_VIEW_KEY) === "split";
    } catch {
      // Storage is blocked, so the unified view shows.
      return false;
    }
  }

  function diffIsSplit() {
    return document.getElementById("diff-view-toggle").getAttribute("aria-pressed") === "true";
  }

  function showDiffSplit(split) {
    const button = document.getElementById("diff-view-toggle");
    button.setAttribute("aria-pressed", String(split));
    setText(button, split ? "Unified view" : "Split view");
    for (const item of document.querySelectorAll("#diff-body .diff-file")) {
      item.dispatchEvent(new Event("toggle"));
    }
  }

  function storeDiffSplit(split) {
    try {
      globalThis.localStorage.setItem(DIFF_VIEW_KEY, split ? "split" : "unified");
    } catch {
      // Storage is blocked: the choice lasts for this page view only.
    }
  }

  function toggleDiffSplit() {
    const split = !diffIsSplit();
    showDiffSplit(split);
    storeDiffSplit(split);
  }

  // Draw an open file in the current view, once per view.
  function drawDiffFile(item, pre, file) {
    const view = diffIsSplit() ? "split" : "unified";
    if (!item.open || pre.dataset.view === view) {
      return;
    }
    pre.replaceChildren();
    pre.dataset.view = view;
    pre.classList.toggle("diff-split", view === "split");
    const language = syntaxLanguage(file.path);
    (view === "split" ? fillDiffSplit : fillDiffText)(pre, file.patch, language);
  }

  function countsText(item) {
    return "+" + displayNumber(item.additions) + " \u2212" + displayNumber(item.deletions);
  }

  // A file is closed until opened; its text is drawn on first open, which keeps
  // a large diff cheap.
  function diffFileNode(file) {
    const item = element("details", "evidence-item diff-file");
    const summary = item.appendChild(element("summary", ""));
    summary.appendChild(element("span", "chip", displayValue(file.status)));
    summary.appendChild(element("code", "evidence-label", displayValue(file.path)));
    summary.appendChild(element("span", "diff-counts", countsText(file)));
    const pre = item.appendChild(element("pre", "evidence-output diff-text"));
    if (typeof file.patch !== "string") {
      setText(pre, "This file is past the size limit, so its text is not shown.");
      return item;
    }
    item.addEventListener("toggle", function () { drawDiffFile(item, pre, file); });
    return item;
  }

  function renderAttemptPicker(payload) {
    const select = document.getElementById("diff-attempt");
    const attempts = asArray(payload.attempts);
    document.getElementById("diff-attempt-label").hidden = attempts.length < 2;
    clearChildren(select);
    select.appendChild(new Option("Latest", ""));
    for (const number of attempts) {
      select.appendChild(new Option(String(number), String(number)));
    }
    select.value = payload.attempt === null ? "" : String(payload.attempt);
  }

  function renderDiff(body, payload) {
    renderAttemptPicker(payload);
    const files = asArray(payload.files);
    if (files.length === 0) {
      return payload.attempts?.length > 0;
    }
    const noun = files.length === 1 ? " file" : " files";
    body.appendChild(element("p", "diff-summary", files.length + noun + " · " + countsText(payload)));
    if (payload.truncated === true) {
      body.appendChild(element("p", "evidence-note", "The diff is cut at 256 KB; later files show counts only."));
    }
    files.forEach(function (file) { body.appendChild(diffFileNode(file)); });
    return true;
  }

  function verificationResult(detail) {
    const verification = detail.verification;
    if (!verification) {
      return EMPTY_VALUE;
    }
    return verification.passed ? "passed" : "failed";
  }

  // The result card of a finished run: what changed, how long it took and where it went.
  function renderCompletion() {
    const detail = state.runDetail;
    const card = document.getElementById("completion-card");
    card.hidden = detail?.is_finished !== true;
    if (card.hidden) {
      return;
    }
    const diff = state.evidence.diff;
    const files = diff ? asArray(diff.files).length : null;
    syncStats(document.getElementById("completion-stats"), [
      { label: "Files", value: displayNumber(files), note: "", help: "" },
      { label: "Lines", value: diff ? countsText(diff) : NOT_REPORTED, note: "", help: "" },
      { label: "Run time", value: elapsedText(detail.created_at, stoppedAtMs(detail)), note: "", help: "" },
      { label: "Checks", value: verificationResult(detail), note: "", help: "" }
    ]);
    const fields = [
      ["Branch", state.evidence.plan?.branch_name],
      ["Pull request", detail.pull_request_url, true]
    ];
    syncDefinitionList(
      document.getElementById("completion-body"),
      fields.filter(function (field) { return !isBlankField(field); })
    );
  }

  const EVIDENCE_RENDERERS = {
    checks: renderChecks, review: renderReview, plan: renderPlan, diff: renderDiff
  };

  // Draws a card again only when its answer changed, so a poll keeps open items open.
  // A diff of a picked older attempt is drawn but not kept, so the result card
  // always shows the latest patch.
  function renderEvidence(kind, runId, payload, keep = true) {
    if (keep && kind in state.evidence) {
      state.evidence[kind] = payload;
      renderCompletion();
    }
    const key = runId + "\n" + JSON.stringify(payload);
    if (state.evidenceDrawn[kind] === key) {
      return;
    }
    state.evidenceDrawn[kind] = key;
    // The Steps list joins the checks and review answers, so it changes with them.
    if (keep && state.runDetail) {
      renderTimeline(state.runDetail, runId);
    }
    const body = document.getElementById(kind + "-body");
    clearChildren(body);
    const shown = payload !== null && EVIDENCE_RENDERERS[kind](body, payload);
    document.getElementById(kind + "-card").hidden = !shown;
  }

  function refreshRunPage(request) {
    return [
      refreshRunDetail(request).then(function () { return refreshDiff(request); }),
      refreshEvidence(request, "checks"),
      refreshEvidence(request, "review"),
      refreshEvidence(request, "plan")
    ];
  }

  // The diff is large, so it loads again only when the run or the picked attempt changed.
  function refreshDiff(request) {
    const attempt = state.diffAttempt;
    const key = [request.runId, attempt, state.runDetail?.updated_at].join("\n");
    if (state.diffKey === key) {
      return Promise.resolve();
    }
    const query = attempt === null ? "" : "?attempt=" + attempt;
    // The key is kept only once the diff is drawn, so a dropped or failed load
    // is tried again on the next poll.
    return refreshEvidence(request, "diff", query).then(function (drawn) {
      if (drawn) {
        state.diffKey = key;
      }
    });
  }

  // A failed load hides the card; it never marks the server unreachable.
  // Resolves to true only when the answer was drawn.
  function refreshEvidence(request, kind, query = "") {
    return apiFetch(runDetailPath(request.runId) + "/" + kind + query).then(
      function (payload) {
        if (!isLatest(request)) {
          return false;
        }
        renderEvidence(kind, request.runId, payload, query === "");
        return true;
      },
      function () {
        if (isLatest(request)) {
          renderEvidence(kind, request.runId, null, query === "");
        }
        return false;
      }
    );
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
          setRunDetailStatus("");
          showLoadFailure(
            document.getElementById("run-detail-status"), "Run detail is currently unavailable."
          );
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

  // The list rows carry no model. The models each run used show in the table
  // once chosen.
  function runOption(run) {
    const runId = runIdOf(run);
    const parts = [run.created_at, run.state, run.title || runId];
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
    const head = row.appendChild(element("th", "", roleText(role.role)));
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

  // The picker lists one page of the runs that match the search, newest first.
  function compareListPath(list) {
    const params = new URLSearchParams({ limit: String(MAX_RUNS_LIMIT), offset: String(list.offset) });
    if (list.q !== "") {
      params.set("q", list.q);
    }
    return "/api/runs?" + params.toString();
  }

  function selectionKey(selection) {
    return isCompleteSelection(selection) ? selection.a + "/" + selection.b : null;
  }

  // The states of lifecycle.TERMINAL_STATES: a run in one of them no longer changes.
  const FINISHED_STATES = new Set(["DONE", "NEEDS_HUMAN", "FAILED"]);

  // The pair a comparison answer settles: both runs finished, so polling it again
  // would show the same table. Null while either run is still going.
  function settledKey(payload, selection) {
    const finished = FINISHED_STATES.has(payload?.a?.state) && FINISHED_STATES.has(payload?.b?.state);
    return finished ? selectionKey(selection) : null;
  }

  // What a compare refresh still has to load. The list loads once per route entry,
  // search and page, not on every poll. The comparison loads until both picked runs
  // have finished.
  function compareDue(list, selection, loaded) {
    const key = selectionKey(selection);
    return {
      list: loaded.list !== compareListPath(list),
      comparison: key !== null && loaded.settled !== key
    };
  }

  function renderComparePager(payload, offset) {
    const shown = asArray(payload.runs).length;
    const page = payload.page || {};
    const total = typeof page.total === "number" ? page.total : null;
    const info = document.getElementById("compare-page-info");
    if (shown === 0) {
      setText(info, offset === 0 ? "No runs match." : "No more runs.");
    } else {
      setText(info, "showing " + (offset + 1) + "\u2013" + (offset + shown) +
        (total === null ? "" : " of " + total));
    }
    document.getElementById("compare-prev").disabled = offset <= 0;
    document.getElementById("compare-next").disabled =
      !hasMoreRuns(page, shown, total, { offset: offset, limit: MAX_RUNS_LIMIT });
  }

  function refreshCompareRuns(request, list) {
    const path = compareListPath(list);
    return apiFetch(path).then(whenLatest(request, function (payload) {
      state.compareLoaded.list = path;
      state.compareList.page = payload;
      renderComparePager(payload, list.offset);
      renderCompareRuns(payload, request);
    }));
  }

  function refreshComparison(request) {
    const path = "/api/compare?a=" + encodeURIComponent(request.compare.a) +
      "&b=" + encodeURIComponent(request.compare.b);
    return apiFetch(path).then(whenLatest(request, function (payload) {
      state.compareLoaded.settled = settledKey(payload, request.compare);
      renderComparison(payload);
    }), onCompareFailure(request));
  }

  // The pickers redraw from the held page, so a pick needs no new list. With
  // nothing due, the refresh still checks that the server answers.
  function refreshCompare(request) {
    const list = state.compareList;
    if (list.page !== null) {
      renderCompareRuns(list.page, request);
    }
    const due = compareDue(list, request.compare, state.compareLoaded);
    const tasks = [];
    if (due.list) {
      tasks.push(refreshCompareRuns(request, { q: list.q, offset: list.offset }));
    }
    if (due.comparison) {
      tasks.push(refreshComparison(request));
    }
    return tasks.length === 0 ? [pingServer()] : tasks;
  }

  // ---- Routing -----------------------------------------------------------

  // The filter a hash asks for, needs-you or active, or null for any other query.
  function routeFilter(query) {
    const filter = new URLSearchParams(query).get("filter");
    return filter === FILTER_NEEDS_YOU || filter === FILTER_ACTIVE ? filter : null;
  }

  // The run list search a hash asks for. A value the server would refuse is dropped.
  function routeSearch(query) {
    const params = new URLSearchParams(query);
    const runState = params.get("state") ?? "";
    const project = params.get("project") ?? "";
    return {
      q: (params.get("q") ?? "").trim().slice(0, MAX_SEARCH_LENGTH),
      state: RUN_STATES.has(runState) ? runState : "",
      project: PROJECT_ID_PATTERN.test(project) ? project : "",
      sort: SORTS.has(params.get("sort")) ? params.get("sort") : ""
    };
  }

  function routeOffset(query) {
    const offset = Number(new URLSearchParams(query).get("offset"));
    return Number.isInteger(offset) && offset > 0 && offset <= MAX_RUNS_OFFSET ? offset : 0;
  }

  function routeTab(query) {
    const tab = new URLSearchParams(query).get("tab");
    return RUN_TABS.includes(tab) ? tab : DEFAULT_TAB;
  }

  // The search fields that are set, and the offset when past the first page.
  function searchParams(search, offset) {
    const params = new URLSearchParams();
    for (const key of Object.keys(EMPTY_SEARCH)) {
      if (search[key]) {
        params.set(key, search[key]);
      }
    }
    if (offset > 0) {
      params.set("offset", String(offset));
    }
    return params.toString();
  }

  function runsHash(search, offset) {
    const query = searchParams(search, offset);
    return query === "" ? "#runs" : "#runs?" + query;
  }

  function runHash(runId, tab) {
    const path = "#run/" + encodeURIComponent(runId);
    return tab === DEFAULT_TAB ? path : path + "?tab=" + tab;
  }

  // Returns null for an empty or unknown hash. The run id is only checked
  // for shape later, so a bad id still opens the run view with "Unknown run".
  // Compare takes up to two ids, run A then run B, checked for shape later too.
  function parseRoute(hash) {
    const [path, query] = hash.replace(/^#/, "").split("?");
    const parts = path.split("/");
    const name = parts[0];
    if (name === "run" && parts.length === 2) {
      return { view: "run", runId: parts[1], tab: routeTab(query) };
    }
    if (name === "compare" && parts.length <= 3) {
      return { view: "compare", a: parts[1], b: parts[2] };
    }
    if (parts.length === 1 && SIMPLE_VIEWS.has(name)) {
      return {
        view: name,
        filter: routeFilter(query),
        search: routeSearch(query),
        offset: routeOffset(query)
      };
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
  function applyRunsFilter(filter, offset) {
    state.filter = filter;
    state.limit = filter === null ? PAGE_SIZE : MAX_RUNS_LIMIT;
    state.offset = filter === null ? offset : 0;
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
    document.getElementById("run-id").hidden = true;
    if (!RUN_ID_PATTERN.test(runId)) {
      heading.textContent = VIEWS.run.label;
      setRunDetailStatus("Unknown run");
      return;
    }
    heading.textContent = "Run " + runId;
    setRunDetailStatus("");
    document.getElementById("run-detail-status").replaceChildren(...skeletonNodes(4));
  }

  // What each view refreshes. A run view with an unknown id has no run id in
  // the request and loads nothing.
  const REFRESHERS = {
    runs: function (request) {
      return [refreshRuns(request), refreshTotals(request)];
    },
    run: function (request) {
      return request.runId === null ? [pingServer()] : refreshRunPage(request);
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
    if (view !== "runs" && view !== "health") {
      refreshService();
    }
    const refresher = REFRESHERS[view];
    if (!refresher || isDirty(document.getElementById(DIRTY_REGIONS[view] ?? VIEWS[view].section))) {
      return Promise.resolve();
    }
    const request = beginRequest(view);
    return settle(request, refresher(request));
  }

  // A poll tick waits while the open view still has a request in flight.
  // A hidden tab does not poll; it refreshes once when it becomes visible again.
  function pollView() {
    if (document.hidden) {
      return;
    }
    if (requests[state.view].inFlight === 0) {
      void refreshView();
    }
  }

  // Whether the open view shows a run that can still change: an active row in the run
  // list, an active open run, or a comparison whose runs have not both finished.
  function viewHasActiveRun(view, held) {
    if (view === "runs") {
      return asArray(held.shownRuns?.[0]?.runs).some(isActiveRun);
    }
    if (view === "run") {
      return held.runDetail !== null && isActiveRun(held.runDetail);
    }
    const key = view === "compare" ? selectionKey(held.compare) : null;
    return key !== null && held.compareLoaded.settled !== key;
  }

  // With the change stream open (ADR-077) a change refreshes the view, so the poll stays slow.
  function pollDelay(view, held) {
    if (held.streamOpen) {
      return POLL_IDLE_MS;
    }
    return viewHasActiveRun(view, held) ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  }

  // A short timer chain checks every POLL_ACTIVE_MS whether the open view is due, so a
  // run that turns active, or a newly opened active run, polls fast without waiting out
  // a slow delay. A check that is not due sends nothing.
  let lastPollAt = 0;
  // The next check is set first, so an error in this one cannot stop the chain.
  function pollTick() {
    globalThis.setTimeout(pollTick, POLL_ACTIVE_MS);
    const nowMs = Date.now();
    if (nowMs - lastPollAt >= pollDelay(state.view, state)) {
      lastPollAt = nowMs;
      pollView();
    }
  }

  // Whether a change to these runs can alter the open view. A run page cares only about its
  // own run; null names any run.
  function changeTouchesView(view, openRunId, runs) {
    return view !== "run" || !Array.isArray(runs) || runs.includes(openRunId);
  }

  // A change refreshes the open view at once, or on the next check while a request is out.
  function onRunsChanged(event) {
    let runs = null;
    try {
      runs = JSON.parse(event.data).runs;
    } catch {
      runs = null;
    }
    if (!changeTouchesView(state.view, state.runId, runs)) {
      return;
    }
    if (requests[state.view].inFlight === 0) {
      lastPollAt = Date.now();
      pollView();
    } else {
      lastPollAt = 0;
    }
  }

  // The stream from /api/events (ADR-077). The browser reconnects on its own with the last
  // event id; while it is not open, and when the server refuses it, the page polls as before.
  function openChangeStream() {
    if (typeof globalThis.EventSource !== "function") {
      return;
    }
    const source = new globalThis.EventSource("/api/events");
    source.addEventListener("open", function () {
      state.streamOpen = true;
    });
    source.addEventListener("error", function () {
      state.streamOpen = false;
    });
    source.addEventListener("runs-changed", onRunsChanged);
  }

  function applyRoute(moveFocus) {
    if (isUnknownHash(globalThis.location.hash)) {
      globalThis.history.replaceState(null, "", "#runs");
    }
    const route = resolveRoute();
    state.view = route.view;
    const runId = validRunId(route);
    if (runId !== state.runId) {
      state.diffAttempt = null;
      state.checksAttempt = null;
      state.diffKey = null;
      state.runDetail = null;
      state.evidence = { plan: null, diff: null, checks: null, review: null };
      state.recordReasons = {};
    }
    state.runId = runId;
    applyRunsFilter(route.filter ?? null, route.offset ?? 0);
    state.search = route.search ?? EMPTY_SEARCH;
    state.tab = route.tab ?? DEFAULT_TAB;
    syncSearchControls(state.search);
    showRunTab(state.tab);
    if (route.view === "runs") {
      listProjects();
    }
    state.compare = compareSelection(route);
    supersede(route.view);
    resetActionState();
    showView(route.view);
    if (route.view === "run") {
      prepareRunDetailView(route.runId);
    }
    if (route.view === "compare") {
      state.compareLoaded = { list: null, settled: null };
      resetCompareStatus(state.compare);
    }
    state.baseTitle = routeTitle(route);
    renderAlert();
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

  // A new page or a new search is a new history entry, so Back returns to the last one.
  function navigateRuns(search, offset, replace = false) {
    const hash = runsHash(search, offset);
    if (replace) {
      globalThis.history.replaceState(null, "", hash);
    } else {
      globalThis.history.pushState(null, "", hash);
    }
    applyRoute(false);
  }

  function goToOffset(offset) {
    navigateRuns(state.search, offset);
  }

  // ---- Search and sort ---------------------------------------------------

  function readSearch() {
    return {
      q: document.getElementById("runs-search").value.trim().slice(0, MAX_SEARCH_LENGTH),
      state: document.getElementById("runs-state").value,
      project: document.getElementById("runs-project").value,
      sort: state.search.sort
    };
  }

  // A field the operator is in keeps what they typed.
  function setControlValue(id, value) {
    const control = document.getElementById(id);
    if (control !== document.activeElement && control.value !== value) {
      control.value = value;
    }
  }

  function addProjectOption(select, projectId) {
    if (![...select.options].some(function (option) { return option.value === projectId; })) {
      select.appendChild(optionNode(projectId, projectId));
    }
  }

  function syncSearchControls(search) {
    if (search.project !== "") {
      addProjectOption(document.getElementById("runs-project"), search.project);
    }
    setControlValue("runs-search", search.q);
    setControlValue("runs-state", search.state);
    setControlValue("runs-project", search.project);
    for (const button of sortButtons()) {
      const direction = columnSort(search.sort, button.dataset.sort);
      const head = button.closest("th");
      if (direction === null) {
        head.removeAttribute("aria-sort");
      } else {
        head.setAttribute("aria-sort", direction);
      }
      button.textContent = button.dataset.label + (SORT_ARROWS[direction] ?? "");
    }
  }

  function sortButtons() {
    return document.querySelectorAll("#runs-table .sort-button");
  }

  // How the column sorted by key reads under sort: ascending, descending or null.
  function columnSort(sort, key) {
    if (key === SORT_STARTED) {
      if (sort === "") {
        return "descending";
      }
      return sort === SORT_OLDEST ? "ascending" : null;
    }
    if (sort === key) {
      return "ascending";
    }
    return sort === "-" + key ? "descending" : null;
  }

  // The sort a click on a column asks for: the other way when the list is sorted by it,
  // else its first way.
  function clickedSort(sort, key) {
    if (key === SORT_STARTED) {
      return sort === "" ? SORT_OLDEST : "";
    }
    if (sort === key) {
      return "-" + key;
    }
    if (sort === "-" + key) {
      return key;
    }
    return SORT_HIGH_FIRST.has(key) ? "-" + key : key;
  }

  // The project picker lists the projects once per page load. A failed load tries again
  // the next time the run list opens.
  function listProjects() {
    if (state.projectsListed) {
      return;
    }
    state.projectsListed = true;
    apiFetch("/api/projects").then(function (payload) {
      const select = document.getElementById("runs-project");
      for (const project of asArray(payload.projects)) {
        if (typeof project.project_id === "string") {
          addProjectOption(select, project.project_id);
        }
      }
      setControlValue("runs-project", state.search.project);
    }, function () {
      state.projectsListed = false;
    });
  }

  let searchTimer = null;

  function onSearchInput() {
    globalThis.clearTimeout(searchTimer);
    searchTimer = globalThis.setTimeout(function () {
      navigateRuns(readSearch(), 0, true);
    }, SEARCH_DELAY_MS);
  }

  function onSortClick(event) {
    const sort = clickedSort(state.search.sort, event.currentTarget.dataset.sort);
    navigateRuns({ ...state.search, sort: sort }, 0);
  }

  function bindSearch() {
    const form = document.getElementById("runs-search-bar");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      globalThis.clearTimeout(searchTimer);
      navigateRuns(readSearch(), 0, true);
    });
    document.getElementById("runs-search").addEventListener("input", onSearchInput);
    for (const id of ["runs-state", "runs-project"]) {
      document.getElementById(id).addEventListener("change", function () {
        navigateRuns(readSearch(), 0);
      });
    }
    for (const button of sortButtons()) {
      button.addEventListener("click", onSortClick);
    }
  }

  // ---- Run detail tabs ---------------------------------------------------

  function showRunTab(tab) {
    for (const name of RUN_TABS) {
      const selected = name === tab;
      const button = document.getElementById("run-tab-" + name);
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      document.getElementById("run-panel-" + name).hidden = !selected;
    }
  }

  // The tab goes in the address, so a link or a reload opens the same tab.
  function pickRunTab(tab, moveFocus) {
    state.tab = tab;
    showRunTab(tab);
    if (state.runId !== null) {
      globalThis.history.replaceState(null, "", runHash(state.runId, tab));
    }
    if (moveFocus) {
      document.getElementById("run-tab-" + tab).focus();
    }
  }

  function bindRunTabs() {
    const list = document.getElementById("run-tabs");
    list.addEventListener("click", function (event) {
      const button = event.target.closest("[data-tab]");
      if (button) {
        pickRunTab(button.dataset.tab, false);
      }
    });
    list.addEventListener("keydown", function (event) {
      const step = TAB_STEPS[event.key];
      if (step === undefined) {
        return;
      }
      event.preventDefault();
      const index = RUN_TABS.indexOf(state.tab);
      pickRunTab(RUN_TABS[(index + step + RUN_TABS.length) % RUN_TABS.length], true);
    });
  }

  // ---- Keyboard ----------------------------------------------------------

  // The command a key asks for, or null. After g, only a view key counts.
  function keyCommand(key, pendingGo) {
    if (pendingGo) {
      return Object.hasOwn(KEY_ROUTES, key) ? "go:" + KEY_ROUTES[key] : null;
    }
    return Object.hasOwn(KEY_COMMANDS, key) ? KEY_COMMANDS[key] : null;
  }

  // The row index a step of j or k moves to: the first or last row when none is chosen.
  function steppedIndex(count, current, step) {
    if (count === 0) {
      return -1;
    }
    if (current < 0) {
      return step > 0 ? 0 : count - 1;
    }
    return Math.min(Math.max(current + step, 0), count - 1);
  }

  // The next row after current that matches, wrapping round, or -1 when none does.
  function nextMatchIndex(matches, current) {
    for (let step = 1; step <= matches.length; step += 1) {
      const index = (current + step + matches.length) % matches.length;
      if (matches[index]) {
        return index;
      }
    }
    return -1;
  }

  function runRows() {
    return [...document.querySelectorAll("#runs-body tr[data-run-id]")];
  }

  function markSelectedRow() {
    for (const row of runRows()) {
      if (row.dataset.runId === state.selectedRunId) {
        row.setAttribute("aria-current", "true");
      } else {
        row.removeAttribute("aria-current");
      }
    }
  }

  function selectRowAt(rows, index) {
    if (index < 0) {
      return false;
    }
    const row = rows[index];
    state.selectedRunId = row.dataset.runId;
    markSelectedRow();
    row.tabIndex = -1;
    row.focus();
    row.scrollIntoView({ block: "nearest" });
    return true;
  }

  function selectedIndex(rows) {
    return rows.findIndex(function (row) { return row.dataset.runId === state.selectedRunId; });
  }

  function moveRow(step) {
    const rows = runRows();
    return selectRowAt(rows, steppedIndex(rows.length, selectedIndex(rows), step));
  }

  // Where n or f goes: the next matching row below the chosen one, else the list that holds
  // only such runs, else (on that list already) its next page, else round to its first row.
  function jumpTarget(matches, current, onList, hasMore) {
    const next = matches.findIndex(function (match, index) { return match && index > current; });
    if (next >= 0) {
      return { index: next };
    }
    if (!onList) {
      return { go: "list" };
    }
    return hasMore ? { go: "next-page" } : { index: nextMatchIndex(matches, current) };
  }

  // n and f: the row class each jumps to, and the list that holds only those runs.
  const JUMPS = {
    "needs-you": {
      className: "row-needs_you",
      onList: function () { return state.filter === FILTER_NEEDS_YOU; },
      hash: function () { return "#runs?filter=" + FILTER_NEEDS_YOU; }
    },
    failure: {
      className: "row-failed",
      onList: function () { return state.filter === null && state.search.state === "failed"; },
      hash: function () { return runsHash({ ...state.search, state: "failed" }, 0); }
    }
  };

  function jumpToRow(name) {
    const jump = JUMPS[name];
    const rows = runRows();
    const matches = rows.map(function (row) { return row.classList.contains(jump.className); });
    const hasMore = state.filter === null && !document.getElementById("runs-next").disabled;
    const target = jumpTarget(matches, selectedIndex(rows), jump.onList(), hasMore);
    if (target.go === undefined) {
      return selectRowAt(rows, target.index);
    }
    state.selectFirstRow = true;
    if (target.go === "next-page") {
      goToOffset(state.offset + state.limit);
    } else {
      globalThis.history.pushState(null, "", jump.hash());
      applyRoute(false);
    }
    return true;
  }

  function openSelectedRow() {
    if (selectedIndex(runRows()) < 0) {
      return false;
    }
    navigateToRun(state.selectedRunId);
    return true;
  }

  function focusSearch() {
    const field = document.getElementById("runs-search");
    field.focus();
    field.select();
    return true;
  }

  function showKeysHelp() {
    const dialog = document.getElementById("keys-help");
    if (!dialog.open) {
      dialog.showModal();
    }
    return true;
  }

  // Row keys work on the run list only.
  function onRunsView(action) {
    return function () {
      return state.view === "runs" && action();
    };
  }

  const KEY_ACTIONS = {
    next: onRunsView(function () { return moveRow(1); }),
    previous: onRunsView(function () { return moveRow(-1); }),
    open: onRunsView(openSelectedRow),
    "needs-you": onRunsView(function () { return jumpToRow("needs-you"); }),
    failure: onRunsView(function () { return jumpToRow("failure"); }),
    search: onRunsView(focusSearch),
    refresh: function () {
      void refreshView();
      return true;
    },
    help: showKeysHelp
  };

  function runKeyCommand(command) {
    if (command.startsWith("go:")) {
      globalThis.location.hash = command.slice("go:".length);
      return true;
    }
    return KEY_ACTIONS[command]();
  }

  // A key typed in a field, or with a modifier, is the browser's.
  function isOwnKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return false;
    }
    const target = event.target;
    if (target.closest("input, textarea, select, [contenteditable], dialog")) {
      return false;
    }
    return !(event.key === "Enter" && target.closest("a, button, summary"));
  }

  function onKeydown(event) {
    if (!isOwnKey(event)) {
      return;
    }
    const command = keyCommand(event.key, state.pendingGo);
    state.pendingGo = command === "await-go";
    if (command === null) {
      return;
    }
    if (state.pendingGo || runKeyCommand(command)) {
      event.preventDefault();
    }
  }

  function bindKeys() {
    document.addEventListener("keydown", onKeydown);
    document.getElementById("keys-help-open").addEventListener("click", showKeysHelp);
    document.getElementById("keys-help-close").addEventListener("click", function () {
      document.getElementById("keys-help").close();
    });
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
    state.compareLoaded.settled = null;
    globalThis.history.pushState(null, "", compareHash(state.compare));
    resetCompareStatus(state.compare);
    void refreshView();
  }

  // Typing searches after a pause; Newer and Older page through the matches.
  let compareSearchTimer = null;

  function showCompareRuns(q, offset) {
    globalThis.clearTimeout(compareSearchTimer);
    state.compareList.q = q;
    state.compareList.offset = offset;
    void refreshView();
  }

  function readCompareSearch() {
    return document.getElementById("compare-search").value.trim().slice(0, MAX_SEARCH_LENGTH);
  }

  function bindCompareSearch() {
    document.getElementById("compare-search-bar").addEventListener("submit", function (event) {
      event.preventDefault();
      showCompareRuns(readCompareSearch(), 0);
    });
    document.getElementById("compare-search").addEventListener("input", function () {
      globalThis.clearTimeout(compareSearchTimer);
      compareSearchTimer = globalThis.setTimeout(function () {
        showCompareRuns(readCompareSearch(), 0);
      }, SEARCH_DELAY_MS);
    });
    document.getElementById("compare-prev").addEventListener("click", function () {
      showCompareRuns(state.compareList.q, Math.max(0, state.compareList.offset - MAX_RUNS_LIMIT));
    });
    document.getElementById("compare-next").addEventListener("click", function () {
      showCompareRuns(state.compareList.q, state.compareList.offset + MAX_RUNS_LIMIT);
    });
  }

  function bindControls() {
    document.getElementById("diff-attempt").addEventListener("change", function (event) {
      const value = event.target.value;
      state.diffAttempt = value === "" ? null : Number(value);
      void refreshView();
    });
    document.getElementById("checks-attempt").addEventListener("change", function (event) {
      const value = event.target.value;
      state.checksAttempt = value === "" ? null : Number(value);
      state.evidenceDrawn.checks = null;
      void refreshView();
    });
    document.getElementById("runs-prev").addEventListener("click", function () {
      goToOffset(Math.max(0, state.offset - state.limit));
    });
    document.getElementById("runs-next").addEventListener("click", function () {
      goToOffset(state.offset + state.limit);
    });
    document.getElementById("steps-view-toggle").addEventListener("click", toggleStepsDetail);
    showStepsDetail(readStepsDetail());
    document.getElementById("diff-view-toggle").addEventListener("click", toggleDiffSplit);
    showDiffSplit(readDiffSplit());
    document.getElementById("theme-toggle").addEventListener("click", function () {
      const next = otherTheme(currentTheme());
      applyTheme(next);
      storeTheme(next);
    });
    bindStopControl();
    bindSteerControl();
    escalateControls().send.addEventListener("click", onEscalateSend);
    bindSearch();
    bindRunTabs();
    bindKeys();
    const reasonButton = document.getElementById("run-reason-show");
    reasonButton.addEventListener("click", function () {
      showFullReason(reasonButton);
    });
    document.getElementById("compare-a").addEventListener("change", onComparePick);
    document.getElementById("compare-b").addEventListener("change", onComparePick);
    bindCompareSearch();
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
    lastPollAt = Date.now();
    globalThis.setTimeout(pollTick, POLL_ACTIVE_MS);
    globalThis.setInterval(tickTimers, TICK_INTERVAL_MS);
    document.addEventListener("visibilitychange", pollView);
    openChangeStream();
  }

  start();
})();
