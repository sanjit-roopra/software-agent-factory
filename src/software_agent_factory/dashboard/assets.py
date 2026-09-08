"""Static asset bodies for the dashboard's single-page app.

Embedded as plain string constants rather than separate files on disk: the
package's build metadata is owned elsewhere (this task may not touch
``pyproject.toml``), so keeping assets as importable Python data guarantees
they ship with the package regardless of how it is packaged. There is no
framework, no bundler and no build step -- what is below is exactly what a
browser receives.
"""

from __future__ import annotations

#: Name of the ``<meta>`` tag the initial HTML uses to hand the token to
#: ``app.js`` without ever placing it in an inline ``<script>`` (the CSP
#: below forbids inline/eval script execution entirely).
TOKEN_META_NAME = "factory-dashboard-token"


def render_index_html(*, token: str) -> str:
    """Render the single static page. ``token`` is a server-generated value,
    never user input, so embedding it directly as an attribute is safe."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="{TOKEN_META_NAME}" content="{token}">
<title>Software Agent Factory &mdash; Dashboard</title>
<link rel="stylesheet" href="/assets/style.css?token={token}">
</head>
<body>
<header>
  <h1>Software Agent Factory</h1>
  <p class="subtitle">Read-only local dashboard &mdash; loopback only, no mutation.</p>
</header>
<main>
  <section id="projects-section" aria-labelledby="projects-heading">
    <h2 id="projects-heading">Projects</h2>
    <div id="projects-body">Loading&hellip;</div>
  </section>

  <section id="health-section" aria-labelledby="health-heading">
    <h2 id="health-heading">Health</h2>
    <div id="health-body">Loading&hellip;</div>
  </section>

  <section id="totals-section" aria-labelledby="totals-heading">
    <h2 id="totals-heading">Totals</h2>
    <div id="totals-body">Loading&hellip;</div>
  </section>

  <section id="runs-section" aria-labelledby="runs-heading">
    <h2 id="runs-heading">Runs</h2>
    <div id="runs-toolbar">
      <button id="runs-prev" type="button">Previous</button>
      <span id="runs-page-info"></span>
      <button id="runs-next" type="button">Next</button>
    </div>
    <table id="runs-table">
      <thead>
        <tr>
          <th scope="col">Run</th>
          <th scope="col">Work item</th>
          <th scope="col">State</th>
          <th scope="col">Created</th>
          <th scope="col">Idle</th>
          <th scope="col">Attempts</th>
          <th scope="col">Stale</th>
        </tr>
      </thead>
      <tbody id="runs-body"></tbody>
    </table>
  </section>

  <section id="detail-section" aria-labelledby="detail-heading" hidden>
    <h2 id="detail-heading">Run detail</h2>
    <button id="detail-close" type="button">Close</button>
    <dl id="detail-body"></dl>
    <h3>Attempts</h3>
    <table id="attempts-table">
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">Role</th>
          <th scope="col">Model</th>
          <th scope="col">Outcome</th>
          <th scope="col">Started</th>
          <th scope="col">Completed</th>
        </tr>
      </thead>
      <tbody id="attempts-body"></tbody>
    </table>
    <h3>Agent invocations</h3>
    <table id="invocations-table">
      <thead>
        <tr>
          <th scope="col">#</th>
          <th scope="col">Role</th>
          <th scope="col">Model</th>
          <th scope="col">Context</th>
          <th scope="col">Success</th>
          <th scope="col">Input tokens</th>
          <th scope="col">Output tokens</th>
          <th scope="col">API duration (ms)</th>
          <th scope="col">Session duration (ms)</th>
          <th scope="col">AI usage value (USD)</th>
          <th scope="col">Premium-request cost</th>
        </tr>
      </thead>
      <tbody id="invocations-body"></tbody>
    </table>
  </section>

  <p id="error-banner" role="alert" hidden></p>
</main>
<script src="/assets/app.js?token={token}"></script>
</body>
</html>
"""


STYLE_CSS = """\
:root {
  color-scheme: light dark;
  --border: #ccc;
  --stale: #b45309;
  --error: #b91c1c;
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, sans-serif;
  margin: 0;
  padding: 0 1.5rem 2rem;
}
header {
  padding: 1rem 0;
  border-bottom: 1px solid var(--border);
}
h1 { margin: 0; font-size: 1.4rem; }
.subtitle { margin: 0.25rem 0 0; color: #666; font-size: 0.9rem; }
section { margin-top: 1.5rem; }
table { border-collapse: collapse; width: 100%; }
th, td {
  border: 1px solid var(--border);
  padding: 0.4rem 0.6rem;
  text-align: left;
  font-size: 0.9rem;
}
th { background: rgba(127, 127, 127, 0.1); }
tr[data-run-id] { cursor: pointer; }
tr[data-run-id]:hover { background: rgba(127, 127, 127, 0.08); }
.project-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-bottom: 1rem;
  padding: 0.75rem;
}
.project-card h3 { margin: 0 0 0.5rem; }
.project-meta { margin: 0 0 0.75rem; color: #666; }
.project-card table { margin-top: 0.5rem; }
.state-active { font-weight: 700; }
.stale-yes { color: var(--stale); font-weight: 600; }
#runs-toolbar { margin-bottom: 0.5rem; display: flex; gap: 0.75rem; align-items: center; }
#error-banner {
  color: var(--error);
  border: 1px solid var(--error);
  padding: 0.5rem 0.75rem;
  border-radius: 4px;
}
dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.25rem 1rem; }
dt { font-weight: 600; }
button { cursor: pointer; }
"""


APP_JS = """\
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var PAGE_SIZE = 20;

  var tokenMeta = document.querySelector('meta[name="factory-dashboard-token"]');
  var token = tokenMeta ? tokenMeta.getAttribute("content") : "";

  var state = { offset: 0, limit: PAGE_SIZE, total: null };

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
    return value === undefined || value === null || value === "" ? "\\u2014" : String(value);
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
      textCell(row, run.work_item_id);
      textCell(row, run.state);
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
    info.textContent = "showing " + rangeStart + "\\u2013" + rangeEnd + totalText;

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
      return "\u2014";
    }
    return "$" + value.toFixed(6);
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
        displayValue(project.project_id) + " \u2014 " + displayValue(project.state);
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
      ["Task", "Title", "State", "Run", "Pull request", "Merged commit"].forEach(function (label) {
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
        pendingCell.colSpan = 6;
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
        (hasProjectUsageValue ? displayUsd(projectUsageValue) : "\u2014") +
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
          "Success",
          "Reported input tokens",
          "Reported output tokens",
          "AI usage value (USD)",
          "Premium-request units"
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
          textCell(row, model.success);
          textCell(row, usage.input_tokens);
          textCell(row, usage.output_tokens);
          textCell(row, displayUsd(usage.usage_value_usd));
          textCell(row, usage.total_premium_request_cost);
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
      ["Title", detail.title],
      ["State", detail.state],
      ["Complexity", detail.complexity],
      ["Risk", detail.risk],
      ["Created", detail.created_at],
      ["Updated", detail.updated_at],
      ["Completed", detail.completed_at],
      ["Invocations", detail.invocation_count],
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
      ["Failure reason", detail.failure_reason],
      ["Commit", detail.commit_sha],
      ["Pull request", detail.pull_request_url]
    ];
    fields.forEach(function (pair) {
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = displayValue(pair[1]);
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
      invocationsBody.appendChild(row);
    });

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
"""
