"""Static asset bodies for the dashboard's single-page app.

The page HTML is rendered here. The script and stylesheet live as plain files
in ``static/`` and ship as package data. There is no framework, no bundler and
no build step -- the files are exactly what a browser receives.
"""

from __future__ import annotations

from importlib import resources

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
<script defer src="/assets/app.js?token={token}"></script>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <header>
      <p class="brand">Software Agent Factory</p>
      <p class="subtitle">Read-only local dashboard &mdash; loopback only, no mutation.</p>
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
    <p id="notice" role="status" aria-live="polite"></p>

    <section id="view-runs" aria-labelledby="runs-heading" hidden>
      <h1 id="runs-heading" tabindex="-1">Runs</h1>
      <div class="card">
        <h2 id="totals-heading">Totals</h2>
        <div id="totals-body">Loading&hellip;</div>
      </div>
      <div class="card">
        <h2 id="run-list-heading">Run list</h2>
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
                <th scope="col">Run</th>
                <th scope="col">Source</th>
                <th scope="col">State</th>
                <th scope="col">Review</th>
                <th scope="col">Created</th>
                <th scope="col">Idle</th>
                <th scope="col">Attempts</th>
                <th scope="col">Stale</th>
              </tr>
            </thead>
            <tbody id="runs-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="view-run-detail" aria-labelledby="run-detail-heading" hidden>
      <h1 id="run-detail-heading" tabindex="-1">Run detail</h1>
      <p><a href="#runs">&larr; Back to runs</a></p>
      <p id="run-detail-status">Loading&hellip;</p>
      <div id="run-detail-content" class="card" hidden>
        <dl id="run-detail-body"></dl>
        <h2>Attempts</h2>
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
              </tr>
            </thead>
            <tbody id="attempts-body"></tbody>
          </table>
        </div>
        <h2>Agent invocations</h2>
        <div class="table-wrap">
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
                <th scope="col">Premium requests</th>
                <th scope="col">List-price estimate</th>
              </tr>
            </thead>
            <tbody id="invocations-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="view-compare" aria-labelledby="compare-heading" hidden>
      <h1 id="compare-heading" tabindex="-1">Compare</h1>
      <div class="card">
        <p>Compare two runs &mdash; coming soon</p>
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
