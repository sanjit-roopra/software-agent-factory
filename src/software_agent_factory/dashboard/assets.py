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
          <th scope="col">Budget</th>
          <th scope="col">Trigger</th>
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
          <th scope="col">List-price estimate</th>
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


def _read_static(name: str) -> str:
    return resources.files(__package__).joinpath("static", name).read_text(encoding="utf-8")


#: Stylesheet and script bodies, read once from the package's ``static/``
#: directory. They ship as package data (``pyproject.toml`` and the
#: PyInstaller spec), so there is still no bundler and no build step.
STYLE_CSS: str = _read_static("style.css")
APP_JS: str = _read_static("app.js")
