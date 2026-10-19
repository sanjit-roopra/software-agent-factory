"""Static asset bodies for the dashboard's single-page app.

The page HTML is rendered here. The script and stylesheet live as plain files
in ``static/`` and ship as package data. There is no framework, no bundler and
no build step -- the files are exactly what a browser receives.
"""

from __future__ import annotations

from importlib import resources

from .snapshot import MAX_PAGE_LIMIT, MAX_SEARCH_LENGTH

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


def sort_button(key: str, label: str) -> str:
    """A run list heading button that sorts by ``key``; the page adds the arrow to ``label``."""
    return (
        f'<button class="sort-button" type="button" data-sort="{key}" '
        f'data-label="{label}">{label}</button>'
    )


def skeleton(rows: int) -> str:
    """Grey placeholder bars shown while a view's first fetch is pending.

    ``app.js`` builds the same markup in ``skeletonNodes`` when it loads a run again.
    """
    bars = '<span class="skeleton-row"></span>' * rows
    return (
        '<span class="visually-hidden">Loading&hellip;</span>'
        f'<span class="skeleton" aria-hidden="true">{bars}</span>'
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
        Local dashboard &mdash; loopback only. Can approve, answer, retry, grant an attempt,
        accept findings, widen the scope, retry CI, stop, steer or escalate a run.
      </p>
      <button id="theme-toggle" type="button">Switch theme</button>
      <button id="keys-help-open" type="button" aria-keyshortcuts="?">Keyboard shortcuts</button>
    </header>
    <nav aria-label="Main">
      <a href="#runs" data-route="runs">Runs
        <span id="runs-count" class="nav-count" hidden></span></a>
      <a href="#compare" data-route="compare">Compare</a>
      <a href="#projects" data-route="projects">Projects</a>
      <a href="#health" data-route="health">Health</a>
    </nav>
    <p id="live" class="live">Connecting&hellip;</p>
    <p id="service-live" class="live" hidden></p>
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
          <li class="stat stat-link">
            <a id="figure-success-link" href="#runs?state=succeeded">
              <span class="stat-label">Success rate</span>
              <span id="figure-success" class="stat-value">&mdash;</span>
              <span id="figure-success-note" class="stat-note" hidden></span>
            </a>
          </li>
          <li class="stat">
            <p class="stat-label">Cost, last 7 days {COST_INFO}</p>
            <p id="figure-week-cost" class="stat-value">&mdash;</p>
            <p id="figure-week-note" class="stat-note" hidden></p>
            <svg id="figure-week-trend" class="sparkline" viewBox="0 0 70 20"
              preserveAspectRatio="none" aria-hidden="true" focusable="false"></svg>
            <a id="figure-week-link" class="stat-note" href="#runs?state=failed">
              Show failed runs</a>
          </li>
        </ul>
        <p id="overview-note" class="stat-note" hidden></p>
      </div>
      <div id="now-running" class="card" hidden>
        <h2 id="now-running-heading">Now running</h2>
        <ul id="now-running-list" class="now-running" aria-labelledby="now-running-heading"></ul>
      </div>
      <div class="card">
        <h2 id="run-list-heading">Run list</h2>
        <p id="runs-filter" hidden>
          Showing only <span id="runs-filter-name">runs that need you</span>,
          from the newest {MAX_PAGE_LIMIT} runs.
          <a href="#runs">Show all runs</a>
        </p>
        <form id="runs-search-bar" class="runs-search" role="search">
          <label class="visually-hidden" for="runs-search">Search runs</label>
          <input id="runs-search" type="search" maxlength="{MAX_SEARCH_LENGTH}"
            placeholder="Search title, run id or issue" autocomplete="off"
            aria-keyshortcuts="/">
          <label class="visually-hidden" for="runs-state">State</label>
          <select id="runs-state">
            <option value="">All states</option>
            <option value="escalated">Needs you</option>
            <option value="active">Active</option>
            <option value="stuck">Stuck</option>
            <option value="failed">Failed</option>
            <option value="succeeded">Done</option>
          </select>
          <label class="visually-hidden" for="runs-project">Project</label>
          <select id="runs-project">
            <option value="">All projects</option>
          </select>
        </form>
        <div id="runs-toolbar">
          <button id="runs-prev" type="button">Previous</button>
          <span id="runs-page-info"></span>
          <button id="runs-next" type="button">Next</button>
        </div>
        <p id="runs-status">{skeleton(6)}</p>
        <div class="table-wrap" hidden>
          <table id="runs-table">
            <thead>
              <tr>
                <th scope="col">{sort_button("title", "Title")}</th>
                <th scope="col">{sort_button("state", "State")}</th>
                <th scope="col">Why</th>
                <th scope="col">PR</th>
                <th scope="col">Calls</th>
                <th scope="col">{sort_button("duration", "Duration")}</th>
                <th scope="col">Tokens</th>
                <th scope="col">{sort_button("cost", "Cost")} {COST_INFO}</th>
                <th scope="col" aria-sort="descending">{sort_button("started", "Started")}</th>
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
      <p id="run-detail-status">{skeleton(4)}</p>
      <div id="run-detail-content" hidden>
        <section id="run-summary" class="card run-summary" aria-labelledby="run-detail-heading">
          <p id="run-escalation" class="headline" role="status" hidden></p>
          <div class="run-summary-head">
            <span id="run-badge" class="badge">&mdash;</span>
            <a id="run-pr-link" target="_blank" rel="noopener noreferrer" hidden>Pull request</a>
            <span id="run-stop" class="run-stop" hidden>
              <button id="run-stop-button" type="button">Stop run</button>
              <button id="run-stop-cancel" type="button" hidden>Keep running</button>
            </span>
          </div>
          <p id="run-stop-note" class="retry-note" hidden>
            The factory kills the agent call, keeps the commits and halts the run for you.
          </p>
          <p id="run-headline" class="headline" hidden></p>
          <p id="run-status-line" class="status-line" hidden></p>
          <ol id="run-stages" class="stages" aria-label="Stages" hidden></ol>
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
        <section id="run-steer" class="card steer" aria-labelledby="run-steer-label" hidden>
          <label id="run-steer-label" for="run-steer-note">Steer the agent</label>
          <textarea id="run-steer-note" rows="3" maxlength="2000"
            placeholder="Tell the agent what to do or what to avoid."
            aria-describedby="run-steer-hint"></textarea>
          <p id="run-steer-hint" class="retry-note"></p>
          <p id="run-steer-waiting" class="steer-waiting" role="status" hidden></p>
          <p class="steer-actions">
            <button id="run-steer-send" type="button">Send note</button>
            <span id="run-steer-status" class="retry-note" role="status"></span>
          </p>
        </section>
        <section id="run-escalate" class="card steer" aria-labelledby="run-escalate-label" hidden>
          <label id="run-escalate-label" for="run-escalate-note">Escalate this run</label>
          <textarea id="run-escalate-note" rows="3" maxlength="2000"
            placeholder="Tell the agents what to change."
            aria-describedby="run-escalate-hint"></textarea>
          <p id="run-escalate-hint" class="retry-note">
            The factory does the work again one route higher, on the same branch and pull request.
          </p>
          <p class="steer-actions">
            <button id="run-escalate-send" type="button">Escalate</button>
            <span id="run-escalate-status" class="retry-note" role="status"></span>
          </p>
        </section>
        <div id="run-tabs" class="tabs" role="tablist" aria-label="Run sections">
          <button id="run-tab-overview" type="button" role="tab" data-tab="overview"
            aria-controls="run-panel-overview" aria-selected="true" tabindex="0">Overview</button>
          <button id="run-tab-activity" type="button" role="tab" data-tab="activity"
            aria-controls="run-panel-activity" aria-selected="false" tabindex="-1">Activity</button>
          <button id="run-tab-changes" type="button" role="tab" data-tab="changes"
            aria-controls="run-panel-changes" aria-selected="false" tabindex="-1">Changes</button>
          <button id="run-tab-review" type="button" role="tab" data-tab="review"
            aria-controls="run-panel-review" aria-selected="false" tabindex="-1">Review</button>
          <button id="run-tab-checks" type="button" role="tab" data-tab="checks"
            aria-controls="run-panel-checks" aria-selected="false" tabindex="-1">Checks</button>
          <button id="run-tab-raw" type="button" role="tab" data-tab="raw"
            aria-controls="run-panel-raw" aria-selected="false" tabindex="-1">Raw</button>
        </div>
        <div id="run-panel-overview" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-overview">
          <p class="tab-empty">Nothing to show here yet.</p>
          <section id="completion-card" class="card" aria-labelledby="completion-heading" hidden>
            <h2 id="completion-heading">Result</h2>
            <div id="completion-stats" class="stats"></div>
            <dl id="completion-body"></dl>
          </section>
          <section id="plan-card" class="card" aria-labelledby="plan-heading" hidden>
            <h2 id="plan-heading">Plan</h2>
            <div id="plan-body"></div>
          </section>
        </div>
        <div id="run-panel-activity" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-activity" hidden>
          <p class="tab-empty">Nothing to show here yet.</p>
          <div class="card">
            <div class="diff-head">
              <h2 id="timeline-heading">Steps</h2>
              <button id="steps-view-toggle" type="button" aria-pressed="false"
                aria-controls="timeline-table">Show all detail</button>
            </div>
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
        </div>
        <div id="run-panel-changes" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-changes" hidden>
          <p class="tab-empty">Nothing to show here yet.</p>
          <section id="diff-card" class="card" aria-labelledby="diff-heading" hidden>
            <div class="diff-head">
              <h2 id="diff-heading">Changes</h2>
              <label id="diff-attempt-label" hidden>Attempt
                <select id="diff-attempt"></select>
              </label>
              <button id="diff-view-toggle" type="button" aria-pressed="false"
                aria-controls="diff-body">Split view</button>
            </div>
            <div id="diff-body"></div>
          </section>
        </div>
        <div id="run-panel-review" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-review" hidden>
          <p class="tab-empty">Nothing to show here yet.</p>
          <section id="review-card" class="card" aria-labelledby="review-heading" hidden>
            <h2 id="review-heading">Review</h2>
            <div id="review-body"></div>
          </section>
        </div>
        <div id="run-panel-checks" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-checks" hidden>
          <p class="tab-empty">Nothing to show here yet.</p>
          <section id="checks-card" class="card" aria-labelledby="checks-heading" hidden>
            <div class="diff-head">
              <h2 id="checks-heading">Checks</h2>
              <label id="checks-attempt-label" hidden>Attempt
                <select id="checks-attempt"></select>
              </label>
            </div>
            <div id="checks-body"></div>
          </section>
        </div>
        <div id="run-panel-raw" class="tab-panel" role="tabpanel"
          aria-labelledby="run-tab-raw" hidden>
          <p class="tab-empty">Nothing to show here yet.</p>
          <details id="run-details" class="card details-card" open>
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
      </div>
    </section>

    <section id="view-compare" aria-labelledby="compare-heading" hidden>
      <h1 id="compare-heading" tabindex="-1">Compare</h1>
      <div class="card">
        <form id="compare-search-bar" class="runs-search" role="search">
          <label class="visually-hidden" for="compare-search">Find runs to compare</label>
          <input id="compare-search" type="search" maxlength="{MAX_SEARCH_LENGTH}"
            placeholder="Find a run by title, run id or issue" autocomplete="off">
        </form>
        <div class="compare-pager">
          <button id="compare-prev" type="button">Newer</button>
          <span id="compare-page-info"></span>
          <button id="compare-next" type="button">Older</button>
        </div>
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
<dialog id="keys-help" class="keys-dialog" aria-labelledby="keys-help-heading">
  <h2 id="keys-help-heading">Keyboard shortcuts</h2>
  <dl class="keys-list">
    <dt><kbd>g</kbd> <kbd>r</kbd></dt><dd>Runs</dd>
    <dt><kbd>g</kbd> <kbd>c</kbd></dt><dd>Compare</dd>
    <dt><kbd>g</kbd> <kbd>p</kbd></dt><dd>Projects</dd>
    <dt><kbd>g</kbd> <kbd>h</kbd></dt><dd>Health</dd>
    <dt><kbd>j</kbd> <kbd>k</kbd></dt><dd>Next or previous run in the list</dd>
    <dt><kbd>Enter</kbd></dt><dd>Open the chosen run</dd>
    <dt><kbd>n</kbd></dt><dd>Next run that needs you, then the list of them</dd>
    <dt><kbd>f</kbd></dt><dd>Next failed run, then the list of them</dd>
    <dt><kbd>/</kbd></dt><dd>Search runs</dd>
    <dt><kbd>r</kbd></dt><dd>Refresh now</dd>
    <dt><kbd>?</kbd></dt><dd>Show this list</dd>
  </dl>
  <p class="keys-note">Keys do nothing while you type in a field.</p>
  <div class="dialog-actions">
    <button id="keys-help-close" type="button">Close</button>
  </div>
</dialog>
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

#: Every file served under ``/assets/``: its media type and body. The font is the Latin
#: subset of Inter (SIL OFL 1.1, ``static/inter-LICENSE.txt``), served from the page's own
#: origin because the CSP allows nothing else.
STATIC_ROUTES: dict[str, tuple[str, bytes]] = {
    "/assets/app.js": ("text/javascript; charset=utf-8", APP_JS.encode("utf-8")),
    "/assets/style.css": ("text/css; charset=utf-8", STYLE_CSS.encode("utf-8")),
    "/assets/favicon.svg": ("image/svg+xml", FAVICON_SVG.encode("utf-8")),
    "/assets/favicon-alert.svg": ("image/svg+xml", FAVICON_ALERT_SVG.encode("utf-8")),
    "/assets/inter-latin.woff2": (
        "font/woff2",
        resources.files(__package__).joinpath("static", "inter-latin.woff2").read_bytes(),
    ),
}
