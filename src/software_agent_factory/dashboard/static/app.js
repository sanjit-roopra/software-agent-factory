(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var PAGE_SIZE = 20;

  var tokenMeta = document.querySelector('meta[name="factory-dashboard-token"]');
  var token = tokenMeta ? tokenMeta.getAttribute("content") : "";

  var state = {
    offset: 0, limit: PAGE_SIZE, total: null, runId: null, view: "runs", notice: null
  };
  var lastSuccessAt = null;
  var lastProjectsSignature = null;

  var ERROR_UNAUTHORIZED = "unauthorized";
  var ERROR_CONNECTION = "connection";
  var ERROR_REQUEST = "request";

  var THEME_STORAGE_KEY = "factory-dashboard-theme";
  var DARK_QUERY = "(prefers-color-scheme: dark)";

  function readStoredTheme() {
    try {
      var stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      return stored === "light" || stored === "dark" ? stored : null;
    } catch (error) {
      return null;
    }
  }

  function storeTheme(theme) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (error) {
      // Storage is blocked: the choice lasts for this page view only.
    }
  }

  function systemTheme() {
    return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || systemTheme();
  }

  function updateThemeToggle() {
    var toggle = document.getElementById("theme-toggle");
    if (toggle) {
      toggle.textContent = "Switch to " + (currentTheme() === "dark" ? "light" : "dark") + " theme";
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    updateThemeToggle();
  }

  // Run before anything else so a remembered theme shows without a flash. With
  // no valid stored value the CSS follows the system theme on its own.
  var initialTheme = readStoredTheme();
  if (initialTheme) {
    document.documentElement.setAttribute("data-theme", initialTheme);
  }

  function apiError(kind, message) {
    var error = new Error(message);
    error.kind = kind;
    return error;
  }

  // Rejects with an error whose "kind" tells a restarted server (401) from a
  // lost connection (network failure or 5xx) and from any other bad request.
  function apiFetch(path) {
    return fetch(path, {
      method: "GET",
      headers: { "X-Factory-Token": token },
      credentials: "same-origin"
    }).then(function (response) {
      if (response.status === 401) {
        throw apiError(ERROR_UNAUTHORIZED, "unauthorized");
      }
      if (!response.ok) {
        var kind = response.status >= 500 ? ERROR_CONNECTION : ERROR_REQUEST;
        throw apiError(kind, "request failed: " + response.status);
      }
      return response.json();
    }, function () {
      throw apiError(ERROR_CONNECTION, "network error");
    });
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  // Writes only when the text changed, so a refresh keeps a text selection.
  function setText(node, text) {
    if (node.textContent !== text) {
      node.textContent = text;
    }
  }

  function noticeMessage() {
    if (state.notice === ERROR_UNAUTHORIZED) {
      return "Dashboard restarted, reload the page.";
    }
    if (state.notice !== ERROR_CONNECTION) {
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
    state.notice = null;
    lastSuccessAt = Date.now();
    renderNotice();
  }

  function onRefreshFailure(error) {
    if (error.kind === ERROR_REQUEST) {
      return;
    }
    state.notice = error.kind === ERROR_UNAUTHORIZED ? ERROR_UNAUTHORIZED : ERROR_CONNECTION;
    renderNotice();
  }

  // A region is dirty while the operator works in it: a focused field or an
  // open dialog. A refresh must not re-render a dirty region.
  function isDirty(region) {
    var active = document.activeElement;
    if (active && region.contains(active) && active.matches("input, textarea, select")) {
      return true;
    }
    return region.querySelector("dialog[open]") !== null;
  }

  function displayValue(value) {
    return value === undefined || value === null || value === "" ? "\u2014" : String(value);
  }

  function textCell(row, value) {
    var cell = document.createElement("td");
    cell.textContent = displayValue(value);
    row.appendChild(cell);
    return cell;
  }

  // Reuses the existing <ul> and <li> nodes so a refresh patches the text in
  // place instead of rebuilding the list.
  function syncList(container, lines) {
    var list = container.firstElementChild;
    if (!list || list.tagName !== "UL") {
      clearChildren(container);
      list = container.appendChild(document.createElement("ul"));
    }
    while (list.children.length > lines.length) {
      list.removeChild(list.lastChild);
    }
    lines.forEach(function (line, index) {
      var item = list.children[index] || list.appendChild(document.createElement("li"));
      setText(item, line);
    });
  }

  function keyValueLines(data) {
    var lines = [];
    Object.keys(data).forEach(function (key) {
      var value = data[key];
      if (Array.isArray(value)) {
        lines.push(key + ": " + value.length + " item(s)");
      } else if (value !== null && typeof value === "object") {
        Object.keys(value).forEach(function (nestedKey) {
          lines.push(key + "." + nestedKey + ": " + displayValue(value[nestedKey]));
        });
      } else {
        lines.push(key + ": " + displayValue(value));
      }
    });
    return lines;
  }

  function renderKeyValueList(container, data, options) {
    if (!data || typeof data !== "object") {
      container.textContent = options && options.emptyMessage
        ? options.emptyMessage
        : "No data available.";
      return;
    }
    syncList(container, keyValueLines(data));
  }

  function renderHealth(health) {
    var container = document.getElementById("health-body");
    if (!health) {
      container.textContent = "No health provider configured.";
      return;
    }
    if (Array.isArray(health.checks)) {
      syncList(container, health.checks.map(function (check) {
        return displayValue(check.name) + " [" + displayValue(check.status) + "]: " +
          displayValue(check.message);
      }));
      return;
    }
    renderKeyValueList(container, health, { emptyMessage: "No health data available." });
  }

  function renderTotals(summary) {
    var container = document.getElementById("totals-body");
    // Render every scalar/one-level-nested field except "health", which has
    // its own section, and "runs"/"page", which the API never includes here.
    var totals = {};
    Object.keys(summary || {}).forEach(function (key) {
      if (key !== "health" && key !== "runs" && key !== "page") {
        totals[key] = summary[key];
      }
    });
    // "metrics.usage" is itself nested two levels deep, past what the
    // generic one-level flattening below descends into, so surface its
    // "unknown" fallback (consistent with the detail/invocation views)
    // directly on "metrics" as its own row; every other totals field is
    // untouched.
    if (totals.metrics && typeof totals.metrics === "object") {
      var listPriceEstimate = totals.metrics.usage
        ? totals.metrics.usage.list_price_estimate_usd
        : null;
      totals.metrics = Object.assign({}, totals.metrics, {
        list_price_estimate_usd: displayListPriceEstimate(listPriceEstimate)
      });
    }
    renderKeyValueList(container, totals, { emptyMessage: "No totals available." });
  }

  function navigateToRun(runId) {
    window.location.hash = "run/" + encodeURIComponent(runId);
  }

  // Patches the rows of a table body in place. A spec is { cells, className,
  // runId }; a cell is a value or { value, className }.
  function patchCell(cell, entry) {
    var isObject = entry !== null && typeof entry === "object";
    setText(cell, displayValue(isObject ? entry.value : entry));
    cell.className = isObject ? entry.className : "";
  }

  function patchRow(row, spec) {
    row.className = spec.className || "";
    if (spec.runId !== undefined) {
      row.setAttribute("data-run-id", spec.runId);
    }
    while (row.children.length > spec.cells.length) {
      row.removeChild(row.lastChild);
    }
    spec.cells.forEach(function (entry, index) {
      var cell = row.children[index] || row.appendChild(document.createElement("td"));
      patchCell(cell, entry);
    });
  }

  function syncRows(tbody, specs) {
    while (tbody.children.length > specs.length) {
      tbody.removeChild(tbody.lastChild);
    }
    specs.forEach(function (spec, index) {
      patchRow(tbody.children[index] || tbody.appendChild(document.createElement("tr")), spec);
    });
  }

  function runRowSpec(run) {
    var runId = run.run_id !== undefined ? run.run_id : run.id;
    var isStale = run.is_stale !== undefined ? run.is_stale : run.stale;
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
        { value: isStale ? "yes" : "no", className: isStale ? "stale-yes" : "" }
      ]
    };
  }

  function hasMoreRuns(page, shown) {
    if (typeof page.has_more === "boolean") {
      return page.has_more;
    }
    if (state.total !== null) {
      return state.offset + state.limit < state.total;
    }
    return shown >= state.limit;
  }

  function renderRunsPager(page, shown) {
    state.total = typeof page.total === "number" ? page.total : null;
    var rangeStart = shown === 0 ? 0 : state.offset + 1;
    var totalText = state.total === null ? "" : " of " + state.total;
    document.getElementById("runs-page-info").textContent =
      "showing " + rangeStart + "\u2013" + (state.offset + shown) + totalText;
    document.getElementById("runs-prev").disabled = state.offset <= 0;
    document.getElementById("runs-next").disabled = !hasMoreRuns(page, shown);
  }

  function renderRunsStatus(shown) {
    var status = document.getElementById("runs-status");
    status.hidden = shown > 0;
    status.textContent = state.offset === 0 ? "No runs yet." : "No runs on this page.";
    document.querySelector("#view-runs .table-wrap").hidden = shown === 0;
  }

  function renderRuns(payload) {
    var runs = Array.isArray(payload.runs) ? payload.runs : [];
    syncRows(document.getElementById("runs-body"), runs.map(runRowSpec));
    renderRunsPager(payload.page || {}, runs.length);
    renderRunsStatus(runs.length);
  }

  function wrapTable(table) {
    var wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.appendChild(table);
    return wrap;
  }

  function appendLinkCell(row, value) {
    var cell = document.createElement("td");
    if (typeof value === "string" && value.indexOf("https://") === 0) {
      var link = document.createElement("a");
      link.href = value;
      link.textContent = value;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      cell.appendChild(link);
    } else {
      cell.textContent = displayValue(value);
    }
    row.appendChild(cell);
  }

  function displayUsd(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "—";
    }
    return "$" + value.toFixed(6);
  }

  function displayListPriceEstimate(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "unknown";
    }
    return displayUsd(value);
  }

  function renderProjects(payload) {
    var container = document.getElementById("projects-body");
    clearChildren(container);
    var projects = Array.isArray(payload.projects) ? payload.projects : [];
    if (projects.length === 0) {
      container.textContent = "No persisted projects.";
      return;
    }
    projects.forEach(function (project) {
      var card = document.createElement("article");
      card.className = "project-card";
      var heading = document.createElement("h3");
      heading.textContent =
        displayValue(project.project_id) + " — " + displayValue(project.state);
      card.appendChild(heading);
      var meta = document.createElement("p");
      meta.className = "project-meta";
      meta.textContent =
        "Delivery: " + displayValue(project.delivery_mode) +
        " | Target: " + displayValue(project.delivery_repository) +
        "#" + displayValue(project.delivery_base_branch) +
        " | Updated: " + displayValue(project.updated_at);
      card.appendChild(meta);

      var table = document.createElement("table");
      var head = document.createElement("thead");
      var headRow = document.createElement("tr");
      [
        "Task", "Title", "State", "Run", "Issue", "Pull request", "Merged commit"
      ].forEach(function (label) {
        var th = document.createElement("th");
        th.scope = "col";
        th.textContent = label;
        headRow.appendChild(th);
      });
      head.appendChild(headRow);
      table.appendChild(head);
      var body = document.createElement("tbody");
      var tasks = Array.isArray(project.tasks) ? project.tasks : [];
      if (tasks.length === 0) {
        var pendingRow = document.createElement("tr");
        var pendingCell = document.createElement("td");
        pendingCell.colSpan = 7;
        pendingCell.textContent = "Planning is in progress; tasks are not persisted yet.";
        pendingRow.appendChild(pendingCell);
        body.appendChild(pendingRow);
      }
      tasks.forEach(function (task) {
        var row = document.createElement("tr");
        textCell(row, task.task_id);
        textCell(row, task.title);
        textCell(row, task.state);
        textCell(row, task.run_id);
        appendLinkCell(row, task.issue_url);
        appendLinkCell(row, task.pull_request_url);
        textCell(row, task.merge_commit_sha);
        body.appendChild(row);
      });
      table.appendChild(body);
      card.appendChild(wrapTable(table));

      var modelsHeading = document.createElement("h4");
      modelsHeading.textContent = "Models used";
      card.appendChild(modelsHeading);
      var modelsHelp = document.createElement("p");
      modelsHelp.className = "project-meta";
      var models = Array.isArray(project.models) ? project.models : [];
      var projectUsageValue = 0;
      var hasProjectUsageValue = false;
      models.forEach(function (model) {
        var value = model.usage ? model.usage.usage_value_usd : null;
        if (typeof value === "number" && Number.isFinite(value)) {
          projectUsageValue += value;
          hasProjectUsageValue = true;
        }
      });
      modelsHelp.textContent =
        "AI usage value: " +
        (hasProjectUsageValue ? displayUsd(projectUsageValue) : "—") +
        ". Calculated from Copilot-reported nano-AIU at 1 AI credit = $0.01. " +
        "Your invoice charge may be lower or zero when included credits apply. " +
        "Premium-request units are a separate legacy metric.";
      card.appendChild(modelsHelp);
      if (models.length === 0) {
        var emptyModels = document.createElement("p");
        emptyModels.textContent = "No model invocations yet.";
        card.appendChild(emptyModels);
      } else {
        var modelsTable = document.createElement("table");
        var modelsHead = document.createElement("thead");
        var modelsHeadRow = document.createElement("tr");
        [
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
        ].forEach(function (label) {
          var th = document.createElement("th");
          th.scope = "col";
          th.textContent = label;
          modelsHeadRow.appendChild(th);
        });
        modelsHead.appendChild(modelsHeadRow);
        modelsTable.appendChild(modelsHead);
        var modelsBody = document.createElement("tbody");
        models.forEach(function (model) {
          var usage = model.usage || {};
          var row = document.createElement("tr");
          textCell(row, model.scope);
          textCell(row, model.role);
          textCell(row, model.model);
          textCell(row, model.purpose);
          textCell(
            row,
            model.status ||
              (model.success === true ? "SUCCESS" : model.success === false ? "FAILED" : null)
          );
          textCell(row, model.started_at);
          textCell(row, usage.input_tokens);
          textCell(row, usage.output_tokens);
          textCell(row, displayUsd(usage.usage_value_usd));
          textCell(row, usage.total_premium_request_cost);
          textCell(row, displayListPriceEstimate(usage.list_price_estimate_usd));
          modelsBody.appendChild(row);
        });
        modelsTable.appendChild(modelsBody);
        card.appendChild(wrapTable(modelsTable));
      }
      container.appendChild(card);
    });
  }

  // The projects payload is nested and rebuilt wholesale, so an unchanged
  // payload skips the rebuild and leaves the DOM as the operator left it.
  function refreshProjects() {
    return apiFetch("/api/projects").then(function (payload) {
      var signature = JSON.stringify(payload);
      if (signature !== lastProjectsSignature) {
        renderProjects(payload);
        lastProjectsSignature = signature;
      }
    });
  }

  function refreshTotals() {
    return apiFetch("/api/summary").then(renderTotals);
  }

  function refreshHealth() {
    return apiFetch("/api/summary").then(function (payload) {
      renderHealth(payload.health);
    });
  }

  function refreshRuns() {
    var query =
      "limit=" + encodeURIComponent(state.limit) +
      "&offset=" + encodeURIComponent(state.offset);
    return apiFetch("/api/runs?" + query).then(renderRuns);
  }

  // A message shows in the status line and hides the detail card; null shows
  // the card instead.
  function setDetailStatus(message) {
    var status = document.getElementById("detail-status");
    status.textContent = message === null ? "" : message;
    status.hidden = message === null;
    document.getElementById("detail-content").hidden = message !== null;
  }

  function setValueNode(dd, value, asLink) {
    if (!asLink || typeof value !== "string" || value.indexOf("https://") !== 0) {
      setText(dd, displayValue(value));
      return;
    }
    var existing = dd.firstElementChild;
    if (existing && existing.getAttribute("href") === value) {
      return;
    }
    clearChildren(dd);
    var link = document.createElement("a");
    link.href = value;
    link.textContent = value;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    dd.appendChild(link);
  }

  // A field is [label, value, asLink]. The label list is fixed, so the usual
  // refresh patches each dt and dd in place.
  function syncDefinitionList(dl, fields) {
    while (dl.children.length > fields.length * 2) {
      dl.removeChild(dl.lastChild);
    }
    fields.forEach(function (pair, index) {
      var dt = dl.children[index * 2] || dl.appendChild(document.createElement("dt"));
      var dd = dl.children[index * 2 + 1] || dl.appendChild(document.createElement("dd"));
      setText(dt, pair[0]);
      setValueNode(dd, pair[1], pair[2]);
    });
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
    var usage = invocation.usage || {};
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
    return {
      className: "invocation-active",
      cells: [
        active.invocation_number,
        active.role,
        active.model,
        active.context_tier,
        active.status,
        "\u2014", "\u2014", "\u2014", "\u2014", "\u2014", "\u2014", "\u2014"
      ]
    };
  }

  function renderDetail(detail) {
    var fields = [
      ["Run", detail.run_id !== undefined ? detail.run_id : detail.id],
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
      [
        "Active invocation",
        detail.active_invocation
          ? detail.active_invocation.role +
            " (" + detail.active_invocation.model + ") — " +
            detail.active_invocation.status +
            " since " + detail.active_invocation.started_at
          : null
      ],
      ["Usage reported", detail.usage ? detail.usage.reported_invocations : null],
      ["Input tokens", detail.usage ? detail.usage.input_tokens : null],
      ["Output tokens", detail.usage ? detail.usage.output_tokens : null],
      ["Reasoning tokens", detail.usage ? detail.usage.reasoning_tokens : null],
      ["Cache read tokens", detail.usage ? detail.usage.cache_read_tokens : null],
      [
        "AI usage value (USD)",
        detail.usage ? displayUsd(detail.usage.usage_value_usd) : null
      ],
      ["Premium-request cost", detail.usage ? detail.usage.premium_request_cost : null],
      ["Nano AIU", detail.usage ? detail.usage.total_nano_aiu : null],
      [
        "List-price estimate",
        displayListPriceEstimate(detail.usage ? detail.usage.list_price_estimate_usd : null)
      ],
      ["Decision status", detail.guidance ? detail.guidance.status : detail.review_status],
      ["What happened", detail.guidance ? detail.guidance.summary : null],
      ["Next action", detail.guidance ? detail.guidance.next_action : null],
      ["Evidence artifact", detail.guidance ? detail.guidance.artifact : null],
      [
        detail.guidance &&
        (detail.guidance.reason_code === "UNRESOLVED_DECISIONS" ||
          detail.guidance.decision_count !== undefined)
          ? "Decision count"
          : "Finding count",
        detail.guidance
          ? detail.guidance.reason_code === "UNRESOLVED_DECISIONS" ||
            detail.guidance.decision_count !== undefined
            ? detail.guidance.decision_count
            : detail.guidance.finding_count
          : null
      ],
      [
        "Finding IDs",
        detail.guidance && Array.isArray(detail.guidance.finding_ids)
          ? detail.guidance.finding_ids.join(", ")
          : null
      ],
      [
        "Finding categories",
        detail.guidance && detail.guidance.category_counts
          ? Object.keys(detail.guidance.category_counts)
              .map(function (category) {
                return category + ": " + detail.guidance.category_counts[category];
              })
              .join(", ")
          : null
      ],
      [
        "Verification",
        detail.verification
          ? (detail.verification.passed ? "passed" : "failed") +
            " (" + detail.verification.failed_check_count +
            "/" + detail.verification.check_count + " failed)"
          : null
      ],
      ["Coverage change", detail.verification ? detail.verification.coverage_change : null],
      ["Artifacts", Array.isArray(detail.artifacts) ? detail.artifacts.join(", ") : null],
      ["Escalation status", detail.escalation ? detail.escalation.status : null],
      ["Escalation reason", detail.escalation ? detail.escalation.reason_code : null],
      [
        "Resume classification",
        detail.escalation ? detail.escalation.resume_classification : null
      ],
      [
        "Waiting for human",
        detail.escalation ? detail.escalation.waiting_for_human : detail.waiting_for_human
      ],
      ["Escalation episode", detail.escalation ? detail.escalation.episode_number : null],
      [
        "Escalation comment",
        detail.escalation ? detail.escalation.comment_url : null,
        true
      ],
      [
        "Last authorized responder",
        detail.escalation ? detail.escalation.last_responder : null
      ],
      ["Last authorized action", detail.escalation ? detail.escalation.last_action : null],
      ["Resumed", detail.escalation ? detail.escalation.is_resumed : null],
      ["Resumed at", detail.escalation ? detail.escalation.resumed_at : null],
      ["Commit", detail.commit_sha],
      ["Merged commit", detail.merge_commit_sha],
      ["Pull request", detail.pull_request_url, true]
    ];
    syncDefinitionList(document.getElementById("detail-body"), fields);

    var attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
    syncRows(document.getElementById("attempts-body"), attempts.map(attemptRowSpec));

    var invocations = Array.isArray(detail.invocations) ? detail.invocations : [];
    var invocationSpecs = invocations.map(invocationRowSpec);
    if (detail.active_invocation) {
      invocationSpecs.push(activeInvocationRowSpec(detail.active_invocation));
    }
    syncRows(document.getElementById("invocations-body"), invocationSpecs);

    setDetailStatus(null);
  }

  // A failure shows the status line only while no detail has loaded; a loaded
  // card stays visible.
  function refreshDetail() {
    var runId = state.runId;
    return apiFetch("/api/runs/" + encodeURIComponent(runId)).then(function (payload) {
      if (state.runId === runId) {
        renderDetail(payload);
      }
    }, function (error) {
      if (state.runId === runId && document.getElementById("detail-content").hidden) {
        setDetailStatus("Run detail is currently unavailable.");
      }
      throw error;
    });
  }

  document.getElementById("runs-prev").addEventListener("click", function () {
    state.offset = Math.max(0, state.offset - state.limit);
    refreshView();
  });
  document.getElementById("runs-next").addEventListener("click", function () {
    state.offset = state.offset + state.limit;
    refreshView();
  });
  document.getElementById("theme-toggle").addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    storeTheme(next);
  });
  document.getElementById("runs-body").addEventListener("click", function (event) {
    var row = event.target.closest("tr[data-run-id]");
    if (row) {
      navigateToRun(row.getAttribute("data-run-id"));
    }
  });
  window.matchMedia(DARK_QUERY).addEventListener("change", updateThemeToggle);
  updateThemeToggle();

  var RUN_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
  var TITLE_SUFFIX = " \u2014 Factory dashboard";
  var VIEWS = {
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

  // Returns null for an empty or unknown hash. The run id is only checked
  // for shape later, so a bad id still opens the run view with "Unknown run".
  function parseRoute(hash) {
    var parts = hash.replace(/^#/, "").split("/");
    var name = parts[0];
    if (name === "run" && parts.length === 2) {
      return { view: "run", runId: parts[1] };
    }
    if (name === "compare" && (parts.length === 1 || parts.length === 3)) {
      return { view: "compare" };
    }
    if (parts.length === 1 && ["runs", "projects", "health"].indexOf(name) !== -1) {
      return { view: name };
    }
    return null;
  }

  function currentRoute() {
    var hash = window.location.hash;
    var route = parseRoute(hash);
    if (route) {
      return route;
    }
    if (hash !== "" && hash !== "#") {
      window.history.replaceState(null, "", "#runs");
    }
    return { view: "runs" };
  }

  function showView(name) {
    Object.keys(VIEWS).forEach(function (key) {
      document.getElementById(VIEWS[key].section).hidden = key !== name;
    });
    var links = document.querySelectorAll('nav[aria-label="Main"] a');
    Array.prototype.forEach.call(links, function (link) {
      if (link.getAttribute("data-route") === VIEWS[name].nav) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function routeTitle(route) {
    if (route.view !== "run") {
      return VIEWS[route.view].label + TITLE_SUFFIX;
    }
    return (RUN_ID_PATTERN.test(route.runId) ? "Run " + route.runId : "Unknown run") + TITLE_SUFFIX;
  }

  function openRun(runId) {
    var heading = document.getElementById("detail-heading");
    if (!RUN_ID_PATTERN.test(runId)) {
      heading.textContent = VIEWS.run.label;
      setDetailStatus("Unknown run");
      return;
    }
    heading.textContent = "Run " + runId;
    setDetailStatus("Loading\u2026");
  }

  // What each view refreshes. Compare has nothing to load. A run view with an
  // unknown id has no run id in state and loads nothing.
  var REFRESHERS = {
    runs: function () {
      return [refreshRuns(), refreshTotals()];
    },
    run: function () {
      return state.runId === null ? [] : [refreshDetail()];
    },
    projects: function () {
      return [refreshProjects()];
    },
    health: function () {
      return [refreshHealth()];
    }
  };

  // Refreshes only the open view, and skips it while the operator works in it.
  // The notice shows the outcome of the latest refresh and clears on success.
  function refreshView() {
    renderNotice();
    var refresher = REFRESHERS[state.view];
    if (!refresher || isDirty(document.getElementById(VIEWS[state.view].section))) {
      return Promise.resolve();
    }
    var tasks = refresher();
    if (tasks.length === 0) {
      return Promise.resolve();
    }
    return Promise.all(tasks).then(onRefreshSuccess, onRefreshFailure);
  }

  function applyRoute(moveFocus) {
    var route = currentRoute();
    state.view = route.view;
    state.runId =
      route.view === "run" && RUN_ID_PATTERN.test(route.runId) ? route.runId : null;
    showView(route.view);
    if (route.view === "run") {
      openRun(route.runId);
    }
    document.title = routeTitle(route);
    if (moveFocus) {
      document.getElementById(VIEWS[route.view].heading).focus();
    }
    refreshView();
  }

  // A deep link opens with the run list one step back, so Back leaves the deep
  // view for the list. The marker keeps a reload from adding another entry.
  function seedBackHistory() {
    var hash = window.location.hash;
    var route = parseRoute(hash);
    var seeded = window.history.state !== null && window.history.state.seeded === true;
    if (route === null || route.view === "runs" || seeded) {
      return;
    }
    window.history.replaceState(null, "", "#runs");
    window.history.pushState({ seeded: true }, "", hash);
  }

  window.addEventListener("hashchange", function () {
    applyRoute(true);
  });

  seedBackHistory();
  applyRoute(false);
  window.setInterval(refreshView, POLL_INTERVAL_MS);
})();
