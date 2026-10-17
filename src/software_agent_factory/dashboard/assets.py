"""Static asset bodies for the dashboard's single-page app.

The page HTML is rendered here. The script and stylesheet live as plain files
in ``static/`` and ship as package data. There is no framework, no bundler and
no build step -- the files are exactly what a browser receives.
"""

from __future__ import annotations

from importlib import resources

from .snapshot import MAX_PAGE_LIMIT

#: Name of the ``<meta>`` tag the initial HTML uses to hand the token to
#: ``app.js`` without ever placing it in an inline ``<script>`` (the CSP
#: below forbids inline/eval script execution entirely). The page script sends the token
#: in a header on a write. The cookie is ``HttpOnly``, so the script cannot read it.
TOKEN_META_NAME = "factory-dashboard-token"

#: The one note on cost units, shown as a tooltip beside every cost heading.
COST_NOTE = (
    "Cost is in USD: the AI usage value Copilot reported (1 AI credit = $0.01), else the "
    "list-price estimate. Neither is what a provider billed; your invoice may be lower or "
    "zero. Premium requests are a separate count. Units are never added together."
)
#: The info mark that carries :data:`COST_NOTE`. Screen readers read the note as its label.
COST_INFO = (
    f'<span class="info" role="img" tabindex="0" title="{COST_NOTE}" '
    f'aria-label="About cost: {COST_NOTE}"></span>'
)


def render_index_html(*, token: str) -> str:
    """Render the single static page. ``token`` is a server-generated value,
    never user input, so embedding it directly as an attribute is safe.

    The handler renders this only for a request that holds the token. The asset links
    carry no token: the cookie authenticates them.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="{TOKEN_META_NAME}" content="{token}">
<title>Software Agent Factory &mdash; Dashboard</title>
<link id="favicon" rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
<link rel="stylesheet" href="/assets/style.css">
<script defer src="/assets/app.js"></script>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <header>
      <p class="brand">Software Agent Factory</p>
      <p class="subtitle">
        Local dashboard &mdash; loopback only. Can approve, answer or retry a run.
      </p>
      <button id="theme-toggle" type="button">Switch theme</button>
    </header>
    <nav aria-label="Main">
      <a href="#runs" data-route="runs">Runs
        <span id="runs-count" class="nav-count" hidden></span></a>
      <a href="#compare" data-route="compare">Compare</a>
      <a href="#projects" data-route="projects">Projects</a>
      <a href="#health" data-route="health">Health</a>
    </nav>
    <p id="live" class="live">Connecting&hellip;</p>
  </aside>
  <main>
    <p id="notice" role="status" aria-live="polite" tabindex="-1"></p>

    <section id="view-runs" aria-labelledby="runs-heading" hidden>
      <h1 id="runs-heading" tabindex="-1">Runs</h1>
      <div class="card">
        <h2 id="key-figures-heading">Overview</h2>
        <ul id="key-figures" class="stats key-figures" aria-labelledby="key-figures-heading">
          <li class="stat stat-link">
            <a id="figure-needs-you-link" href="#runs?filter=needs-you">
              <span class="stat-label">Needs you</span>
              <span id="figure-needs-you" class="stat-value">&mdash;</span>
              <span class="stat-note">Show runs that need you</span>
            </a>
          </li>
          <li class="stat stat-link">
            <a id="figure-active-link" href="#runs?filter=active">
              <span class="stat-label">Active</span>
              <span id="figure-active" class="stat-value">&mdash;</span>
              <span class="stat-note">Show active runs</span>
            </a>
          </li>
          <li class="stat">
            <p class="stat-label">Success rate</p>
            <p id="figure-success" class="stat-value">&mdash;</p>
            <p id="figure-success-note" class="stat-note" hidden></p>
          </li>
          <li class="stat">
            <p class="stat-label">Cost, last 7 days {COST_INFO}</p>
            <p id="figure-week-cost" class="stat-value">&mdash;</p>
            <p id="figure-week-note" class="stat-note" hidden></p>
            <svg id="figure-week-trend" class="sparkline" viewBox="0 0 70 20"
              preserveAspectRatio="none" aria-hidden="true" focusable="false"></svg>
          </li>
        </ul>
        <p id="overview-note" class="stat-note" hidden></p>
      </div>
      <div class="card">
        <h2 id="run-list-heading">Run list</h2>
        <p id="runs-filter" hidden>
          Showing only <span id="runs-filter-name">runs that need you</span>,
          from the newest {MAX_PAGE_LIMIT} runs.
          <a href="#runs">Show all runs</a>
        </p>
        <div id="runs-toolbar">
          <button id="runs-prev" type="button">Previous</button>
          <span id="runs-page-info"></span>
          <button id="runs-next" type="button">Next</button>
        </div>
        <p id="runs-status">Loading&hellip;</p>
        <div class="table-wrap" hidden>
          <table id="runs-table">
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">State</th>
                <th scope="col">Why</th>
                <th scope="col">PR</th>
                <th scope="col">Calls</th>
                <th scope="col">Duration</th>
                <th scope="col">Tokens</th>
                <th scope="col">Cost {COST_INFO}</th>
                <th scope="col">Started</th>
                <th scope="col"><span class="visually-hidden">Compare</span></th>
              </tr>
            </thead>
            <tbody id="runs-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="view-run-detail" aria-labelledby="run-detail-heading" hidden>
      <h1 id="run-detail-heading" tabindex="-1">Run detail</h1>
      <p id="run-id" class="run-id" hidden></p>
      <p><a href="#runs">&larr; Back to runs</a></p>
      <p id="run-detail-status">Loading&hellip;</p>
      <div id="run-detail-content" hidden>
        <section id="run-summary" class="card run-summary" aria-labelledby="run-detail-heading">
          <div class="run-summary-head">
            <span id="run-badge" class="badge">&mdash;</span>
            <a id="run-pr-link" target="_blank" rel="noopener noreferrer" hidden>Pull request</a>
          </div>
          <p id="run-headline" class="headline" hidden></p>
          <p id="run-status-line" class="status-line" hidden></p>
          <p id="run-reason-more" hidden>
            <button id="run-reason-show" type="button">Show full failure reason</button>
          </p>
          <pre id="run-reason-full" class="reason-text reason-full" hidden></pre>
          <div id="run-key-numbers" class="stats"></div>
        </section>
        <section id="next-step" class="card next-step" aria-labelledby="next-step-heading" hidden>
          <h2 id="next-step-heading">Needs you</h2>
          <p id="next-step-changed" class="next-step-changed" role="status" hidden>
            This run changed since you started typing. Leave the field to see the new state.
          </p>
          <div id="next-step-body"></div>
        </section>
        <div class="card">
          <h2 id="timeline-heading">Steps</h2>
          <p id="timeline-status">No calls yet.</p>
          <div class="table-wrap" id="timeline-wrap" hidden>
            <table id="timeline-table" class="timeline" aria-labelledby="timeline-heading">
              <thead>
                <tr>
                  <th scope="col">
                    <span aria-hidden="true">#</span>
                    <span class="visually-hidden">Call number</span>
                  </th>
                  <th scope="col">Role</th>
                  <th scope="col">Model</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Tokens</th>
                  <th scope="col">Cost {COST_INFO}</th>
                </tr>
              </thead>
              <tbody id="timeline-body"></tbody>
              <tfoot>
                <tr id="timeline-total" class="timeline-total"></tr>
              </tfoot>
            </table>
          </div>
        </div>
        <details id="run-details" class="card details-card">
          <summary>Details</summary>
          <h3>Totals</h3>
          <div id="run-totals" class="stats"></div>
          <h3>Run facts</h3>
          <dl id="run-detail-body"></dl>
          <h3>Attempts</h3>
          <div class="table-wrap">
            <table id="attempts-table">
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Role</th>
                  <th scope="col">Model</th>
                  <th scope="col">Budget</th>
                  <th scope="col">Trigger</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Started</th>
                  <th scope="col">Completed</th>
                  <th scope="col">Failure reason</th>
                </tr>
              </thead>
              <tbody id="attempts-body"></tbody>
            </table>
          </div>
        </details>
      </div>
    </section>

    <section id="view-compare" aria-labelledby="compare-heading" hidden>
      <h1 id="compare-heading" tabindex="-1">Compare</h1>
      <div class="card">
        <div class="compare-pickers">
          <div class="picker">
            <label for="compare-a">Run A</label>
            <select id="compare-a"></select>
          </div>
          <div class="picker">
            <label for="compare-b">Run B</label>
            <select id="compare-b"></select>
          </div>
        </div>
        <p id="compare-status" role="status" aria-live="polite">Choose two runs to compare.</p>
      </div>
      <div id="compare-content" class="card" hidden>
        <div class="table-wrap">
          <table id="compare-table">
            <caption>The roles of run A and run B side by side</caption>
            <colgroup span="1"></colgroup>
            <colgroup span="6"></colgroup>
            <colgroup span="6"></colgroup>
            <thead>
              <tr>
                <th scope="col" rowspan="2">Role</th>
                <th id="compare-a-head" scope="colgroup" colspan="6">Run A</th>
                <th id="compare-b-head" scope="colgroup" colspan="6">Run B</th>
              </tr>
              <tr>
                <th scope="col">Calls</th>
                <th scope="col">Failed calls</th>
                <th scope="col">Models</th>
                <th scope="col">Tokens</th>
                <th scope="col">Duration</th>
                <th scope="col">Cost</th>
                <th scope="col">Calls</th>
                <th scope="col">Failed calls</th>
                <th scope="col">Models</th>
                <th scope="col">Tokens</th>
                <th scope="col">Duration</th>
                <th scope="col">Cost</th>
              </tr>
            </thead>
            <tbody id="compare-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="view-projects" aria-labelledby="projects-heading" hidden>
      <h1 id="projects-heading" tabindex="-1">Projects</h1>
      <div class="card">
        <div id="projects-body">Loading&hellip;</div>
      </div>
    </section>

    <section id="view-health" aria-labelledby="health-heading" hidden>
      <h1 id="health-heading" tabindex="-1">Health</h1>
      <div id="health-body">Loading&hellip;</div>
    </section>
  </main>
</div>
</body>
</html>
"""


def _read_static(name: str) -> str:
    return resources.files(__package__).joinpath("static", name).read_text(encoding="utf-8")


#: Stylesheet and script bodies, read once from the package's ``static/``
#: directory. They ship as package data (``pyproject.toml`` and the
#: PyInstaller spec), so there is still no bundler and no build step.
STYLE_CSS: str = _read_static("style.css")
APP_JS: str = _read_static("app.js")

#: The tab icons: a plain square, and the same with an amber dot while a run needs the
#: operator. Served from ``/assets/``, as the CSP allows images from the page's origin only.
FAVICON_SVG: str = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect x="2" y="2" width="28" height="28" rx="6" fill="#2563eb"/></svg>'
)
FAVICON_ALERT_SVG: str = FAVICON_SVG.replace(
    "</svg>",
    '<circle cx="24" cy="8" r="7" fill="#f59e0b" stroke="#ffffff" stroke-width="2"/></svg>',
)
