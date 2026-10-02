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
<link rel="stylesheet" href="/assets/style.css">
<script defer src="/assets/app.js"></script>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <header>
      <p class="brand">Software Agent Factory</p>
      <p class="subtitle">Local dashboard &mdash; loopback only. Can approve or answer a run.</p>
      <button id="theme-toggle" type="button">Switch theme</button>
    </header>
    <nav aria-label="Main">
      <a href="#runs" data-route="runs">Runs</a>
      <a href="#compare" data-route="compare">Compare</a>
      <a href="#projects" data-route="projects">Projects</a>
      <a href="#health" data-route="health">Health</a>
    </nav>
  </aside>
  <main>
    <p id="notice" role="status" aria-live="polite" tabindex="-1"></p>

    <section id="view-runs" aria-labelledby="runs-heading" hidden>
      <h1 id="runs-heading" tabindex="-1">Runs</h1>
      <div class="card">
        <h2 id="key-figures-heading">Overview</h2>
        <ul id="key-figures" class="stats key-figures" aria-labelledby="key-figures-heading">
          <li class="stat">
            <p class="stat-label">Runs</p>
            <p id="figure-runs" class="stat-value">&mdash;</p>
          </li>
          <li class="stat">
            <p class="stat-label">Succeeded</p>
            <p id="figure-succeeded" class="stat-value">&mdash;</p>
          </li>
          <li class="stat">
            <p class="stat-label">Failed</p>
            <p id="figure-failed" class="stat-value">&mdash;</p>
            <p id="figure-failed-note" class="stat-note" hidden></p>
          </li>
          <li class="stat">
            <p class="stat-label">Active</p>
            <p id="figure-active" class="stat-value">&mdash;</p>
          </li>
          <li class="stat">
            <p class="stat-label">Needs you</p>
            <p id="figure-needs-you" class="stat-value">&mdash;</p>
            <a id="figure-needs-you-link" href="#runs?filter=needs-you">Show runs that need you</a>
          </li>
          <li class="stat">
            <p class="stat-label">Tokens</p>
            <p id="figure-tokens" class="stat-value">&mdash;</p>
            <p id="figure-tokens-note" class="stat-note" hidden></p>
          </li>
          <li id="stat-list-price" class="stat" hidden>
            <p class="stat-label">List-price estimate</p>
            <p id="figure-list-price" class="stat-value">&mdash;</p>
          </li>
          <li id="stat-premium" class="stat" hidden>
            <p class="stat-label">Premium requests</p>
            <p id="figure-premium" class="stat-value">&mdash;</p>
          </li>
          <li id="stat-cost-none" class="stat" hidden>
            <p class="stat-label">Cost</p>
            <p class="stat-value">not reported</p>
          </li>
        </ul>
        <p class="field-hint">
          List-price estimate is in USD from list prices, not what a provider billed.
          Premium requests are the count the calls reported. The two are never added.
        </p>
        <p id="overview-note" class="stat-note" hidden></p>
      </div>
      <div class="card">
        <h2 id="run-list-heading">Run list</h2>
        <p id="runs-filter" hidden>
          Showing only runs that need you, from the newest {MAX_PAGE_LIMIT} runs.
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
                <th scope="col">Models</th>
                <th scope="col">Calls</th>
                <th scope="col">Duration</th>
                <th scope="col">Cost</th>
                <th scope="col">Started</th>
                <th scope="col"><span class="visually-hidden">Compare</span></th>
              </tr>
            </thead>
            <tbody id="runs-body"></tbody>
          </table>
        </div>
        <p class="field-hint">
          Cost is the list-price estimate in USD, not what a provider billed.
          A run that reported premium requests shows them too.
        </p>
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
          </div>
          <p id="run-headline" class="headline" hidden></p>
          <div id="run-key-numbers" class="stats"></div>
        </section>
        <section id="next-step" class="card next-step" aria-labelledby="next-step-heading" hidden>
          <h2 id="next-step-heading">Needs you</h2>
          <div id="next-step-body"></div>
        </section>
        <div class="card">
          <h2 id="timeline-heading">Steps</h2>
          <p id="timeline-status">No calls yet.</p>
          <div class="table-wrap" id="timeline-wrap" hidden>
            <div class="timeline" role="group" aria-labelledby="timeline-heading">
              <div class="timeline-head" aria-hidden="true">
                <span>#</span>
                <span>Role</span>
                <span>Model</span>
                <span>Outcome</span>
                <span>Duration</span>
                <span>Tokens</span>
                <span>Cost</span>
              </div>
              <div id="timeline-body"></div>
              <div id="timeline-total" class="timeline-total"></div>
            </div>
          </div>
          <p class="field-hint">
            Cost is the list-price estimate in USD, not what a provider billed.
          </p>
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
      <div class="card">
        <div id="health-body">Loading&hellip;</div>
      </div>
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
