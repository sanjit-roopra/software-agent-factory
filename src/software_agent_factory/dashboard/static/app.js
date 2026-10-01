(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var PAGE_SIZE = 20;

  var tokenMeta = document.querySelector('meta[name="factory-dashboard-token"]');
  var token = tokenMeta ? tokenMeta.getAttribute("content") : "";

  var state = { offset: 0, limit: PAGE_SIZE, total: null };

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

  function apiFetch(path) {
    return fetch(path, {
      method: "GET",
      headers: { "X-Factory-Token": token },
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("request failed: " + response.status);
      }
      return response.json();
    });
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showError(message) {
    var banner = document.getElementById("error-banner");
    banner.textContent = message;
    banner.hidden = false;
  }

  function clearError() {
    var banner = document.getElementById("error-banner");
    banner.hidden = true;
    banner.textContent = "";
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

  function renderKeyValueList(container, data, options) {
    clearChildren(container);
    if (!data || typeof data !== "object") {
      container.textContent = options && options.emptyMessage
        ? options.emptyMessage
        : "No data available.";
      return;
    }
    var list = document.createElement("ul");
    Object.keys(data).forEach(function (key) {
      var value = data[key];
      if (value !== null && typeof value === "object" && !Array.isArray(value)) {
        Object.keys(value).forEach(function (nestedKey) {
          var nestedItem = document.createElement("li");
          nestedItem.textContent = key + "." + nestedKey + ": " + displayValue(value[nestedKey]);
          list.appendChild(nestedItem);
        });
        return;
      }
      var item = document.createElement("li");
      if (Array.isArray(value)) {
        item.textContent = key + ": " + value.length + " item(s)";
      } else {
        item.textContent = key + ": " + displayValue(value);
      }
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function renderHealth(health) {
    var container = document.getElementById("health-body");
    clearChildren(container);
    if (!health) {
      container.textContent = "No health provider configured.";
      return;
    }
    if (health && Array.isArray(health.checks)) {
      var list = document.createElement("ul");
      health.checks.forEach(function (check) {
        var item = document.createElement("li");
        var name = displayValue(check.name);
        var status = displayValue(check.status);
        var message = displayValue(check.message);
        item.textContent = name + " [" + status + "]: " + message;
        list.appendChild(item);
      });
      container.appendChild(list);
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

  function renderRuns(payload) {
    var body = document.getElementById("runs-body");
    clearChildren(body);
    var runs = Array.isArray(payload.runs) ? payload.runs : [];
    runs.forEach(function (run) {
      var runId = run.run_id !== undefined ? run.run_id : run.id;
      var isStale = run.is_stale !== undefined ? run.is_stale : run.stale;
      var row = document.createElement("tr");
      row.setAttribute("data-run-id", runId);
      textCell(row, runId);
      textCell(row, run.source_external_id || run.work_item_id);
      textCell(row, run.state);
      textCell(row, run.review_status);
      textCell(row, run.created_at);
      textCell(row, run.idle_seconds);
      textCell(row, run.attempt_count);
      var staleCell = textCell(row, isStale ? "yes" : "no");
      if (isStale) {
        staleCell.classList.add("stale-yes");
      }
      row.addEventListener("click", function () {
        loadDetail(runId);
      });
      body.appendChild(row);
    });

    var page = payload.page || {};
    state.total = typeof page.total === "number" ? page.total : null;
    var info = document.getElementById("runs-page-info");
    var shown = runs.length;
    var rangeStart = shown === 0 ? 0 : state.offset + 1;
    var rangeEnd = state.offset + shown;
    var totalText = state.total === null ? "" : " of " + state.total;
    info.textContent = "showing " + rangeStart + "\u2013" + rangeEnd + totalText;

    document.getElementById("runs-prev").disabled = state.offset <= 0;
    document.getElementById("runs-next").disabled =
      typeof page.has_more === "boolean"
        ? !page.has_more
        : state.total !== null
          ? state.offset + state.limit >= state.total
          : shown < state.limit;
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
      card.appendChild(table);

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
        card.appendChild(modelsTable);
      }
      container.appendChild(card);
    });
  }

  function loadProjects() {
    return apiFetch("/api/projects")
      .then(function (payload) {
        renderProjects(payload);
        clearError();
      })
      .catch(function () {
        showError("Project status is currently unavailable.");
      });
  }

  function loadSummary() {
    return apiFetch("/api/summary")
      .then(function (payload) {
        renderHealth(payload.health);
        renderTotals(payload);
        clearError();
      })
      .catch(function () {
        showError("Summary is currently unavailable.");
      });
  }

  function loadRuns() {
    var query =
      "limit=" + encodeURIComponent(state.limit) +
      "&offset=" + encodeURIComponent(state.offset);
    return apiFetch("/api/runs?" + query)
      .then(function (payload) {
        renderRuns(payload);
        clearError();
      })
      .catch(function () {
        showError("Run list is currently unavailable.");
      });
  }

  function renderDetail(detail) {
    var section = document.getElementById("detail-section");
    var dl = document.getElementById("detail-body");
    clearChildren(dl);

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
    fields.forEach(function (pair) {
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      if (pair[2] && typeof pair[1] === "string" && pair[1].indexOf("https://") === 0) {
        var link = document.createElement("a");
        link.href = pair[1];
        link.textContent = pair[1];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        dd.appendChild(link);
      } else {
        dd.textContent = displayValue(pair[1]);
      }
      dl.appendChild(dt);
      dl.appendChild(dd);
    });

    var attemptsBody = document.getElementById("attempts-body");
    clearChildren(attemptsBody);
    var attempts = Array.isArray(detail.attempts) ? detail.attempts : [];
    attempts.forEach(function (attempt) {
      var row = document.createElement("tr");
      textCell(row, attempt.attempt_number);
      textCell(row, attempt.role);
      textCell(row, attempt.model);
      textCell(row, attempt.budget);
      textCell(row, attempt.triggered_by);
      textCell(row, attempt.outcome);
      textCell(row, attempt.started_at);
      textCell(row, attempt.completed_at);
      attemptsBody.appendChild(row);
    });

    var invocationsBody = document.getElementById("invocations-body");
    clearChildren(invocationsBody);
    var invocations = Array.isArray(detail.invocations) ? detail.invocations : [];
    invocations.forEach(function (invocation) {
      var usage = invocation.usage || {};
      var row = document.createElement("tr");
      textCell(row, invocation.invocation_number);
      textCell(row, invocation.role);
      textCell(row, invocation.model);
      textCell(row, invocation.context_tier);
      textCell(row, invocation.success);
      textCell(row, usage.input_tokens);
      textCell(row, usage.output_tokens);
      textCell(row, usage.total_api_duration_ms);
      textCell(row, usage.session_duration_ms);
      textCell(row, displayUsd(usage.usage_value_usd));
      textCell(row, usage.total_premium_request_cost);
      textCell(row, displayListPriceEstimate(usage.list_price_estimate_usd));
      invocationsBody.appendChild(row);
    });
    if (detail.active_invocation) {
      var active = detail.active_invocation;
      var activeRow = document.createElement("tr");
      activeRow.className = "invocation-active";
      textCell(activeRow, active.invocation_number);
      textCell(activeRow, active.role);
      textCell(activeRow, active.model);
      textCell(activeRow, active.context_tier);
      textCell(activeRow, active.status);
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      textCell(activeRow, "—");
      invocationsBody.appendChild(activeRow);
    }

    section.hidden = false;
  }

  function loadDetail(runId) {
    return apiFetch("/api/runs/" + encodeURIComponent(runId))
      .then(function (payload) {
        renderDetail(payload);
        clearError();
      })
      .catch(function () {
        showError("Run detail is currently unavailable.");
      });
  }

  document.getElementById("runs-prev").addEventListener("click", function () {
    state.offset = Math.max(0, state.offset - state.limit);
    loadRuns();
  });
  document.getElementById("runs-next").addEventListener("click", function () {
    state.offset = state.offset + state.limit;
    loadRuns();
  });
  document.getElementById("theme-toggle").addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    storeTheme(next);
  });
  window.matchMedia(DARK_QUERY).addEventListener("change", updateThemeToggle);
  updateThemeToggle();
  document.getElementById("detail-close").addEventListener("click", function () {
    document.getElementById("detail-section").hidden = true;
  });

  function refresh() {
    loadProjects();
    loadSummary();
    loadRuns();
  }

  refresh();
  window.setInterval(refresh, POLL_INTERVAL_MS);
})();
