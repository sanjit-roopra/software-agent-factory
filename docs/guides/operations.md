# Monitor and run continuously

## factory status

```bash
factory status
factory status --json --limit 50 --offset 50
```

`status` recomputes everything from persisted artifacts on each call. There is
no counter store and no time-series database. It creates, mutates and repairs
nothing. It will not even create the data directory.

```text
data dir: ~/.software-factory
generated at: 2026-09-05T09:06:50+00:00
runs: 1 total, 1 scanned
states: 1 succeeded, 0 escalated, 0 failed, 0 active (0 stale)
attempts: 1 total, 1 implementation, 0 CI repair, 0 scope replan(s)
first-pass success: 100% (1/1)
completed run duration: 1 run(s), avg 0s, max 0s
stale threshold: 900s

health:
  stale runs: 0
  stale locks: 0 (of 0 checked)
  orphaned workspaces: 0 (of 1 checked)

status: complete
```

The health section reports findings, not repairs. A stale lock, an orphaned
worktree, or an abandoned run requires your action. The factory will not
silently clean it up.

When `factory.unattended` is `true`, a factory restart does not abandon a run.
The factory resumes the run under its own run id and keeps its attempt budget.

Two bounds worth knowing:

- `--stale-after-seconds` overrides the staleness threshold, which defaults to
  `scheduler.stall_timeout_seconds`.
- `--max-scanned-runs` (default `1000`) caps how many run files one call parses.

A run that shows no activity for `scheduler.stall_timeout_seconds` is stalled.
While a check or a CI wait runs, the run writes a heartbeat every 30 seconds.
While an agent works, the run writes a heartbeat only when the agent prints output
or starts a tool. So an agent that hangs and prints nothing is stalled after that time.
The service stops a stalled run. An unattended run continues under its own run id.
Any other run halts in `NEEDS_HUMAN` as `STALLED`.

A scan that was truncated, or that hit an unreadable run, reports `DEGRADED`
instead of presenting itself as a complete picture. Do not treat a `DEGRADED`
report as a clean bill of health.

## Logs

`run`, `start` and `dashboard` write structured JSON logs to:

```text
<data_dir>/logs/factory.log
```

The file is rotated and size-bounded. Credentials are redacted with the same
rules applied to captured command output. Nothing is exported anywhere: no
telemetry backend, no exporter, no network egress.

The log records each agent invocation with run id, role, model, and
reasoning level. It also records context tier, duration, attempt number,
and result. The Copilot runtime persists reported token counts, elapsed time,
nano-AIU, and premium-request cost.
When nano-AIU is available, the dashboard shows its USD-equivalent AI usage value.
It uses the GitHub conversion of one AI credit to $0.01.
This value is not necessarily the invoice charge.
Included or pooled credits can cover the usage.
The factory persists in-progress invocations before Copilot starts.
It shows them separately with lease-derived liveness.
Thus, a failed prior attempt cannot hide a current retry.
Missing fields remain unknown.

## Dashboard

```bash
factory dashboard                      # http://127.0.0.1:8765/?token=...
factory dashboard --port 0 --open-browser
```

This is the only thing in the factory that ever opens a socket. Nothing in
`factory run` or `factory start` listens on a port.

- Binds `127.0.0.1` and nothing else.
- Reads with `GET`. The only writes are the actions below.
- Requires a token generated for that process. The tokenized URL is printed to
  stdout once and never written to the log.
- Returns the page and sets a session cookie on the first request for that URL.
  The page then removes the token from the address bar and the current history
  entry, and a reload works. Some browsers can still keep the first address in
  their visit records until the dashboard restarts. After a restart, open the
  new link.
- Uses the token header alone when a read has that header. A wrong header gets
  `401`, even with a right cookie. The cookie counts only when the header is
  absent.
- The cookie is as strong as the token. Any program that holds the cookie can
  load the page and read the token. This includes another local server on
  `127.0.0.1` that the browser visits. The `Host` and `Origin` checks stop
  browsers only. Anything that listens on `127.0.0.1` is trusted, including a
  forwarded port such as `ssh -L` or a container port.
- Blocks in the foreground. Ctrl-C stops it and closes the socket.

It shows project state, issue references, pull requests, merge progress, and
workflow state. Run details show model use, retries, safe
artifact names, verification summaries, and escalation status. The dashboard
does not show command logs, diffs, prompts, raw comments, or raw artifacts.

The Runs page starts with four tiles: **Needs you**, **Active**, **Success
rate**, and **Cost, last 7 days**. Select **Needs you** or **Active** to show
only those runs. Select **Success rate** to show the done runs. The cost tile
has a line with the cost of each day and a link to the failed runs. The state
list above the run list has a **Stuck** choice. It shows the active runs with
no new signal for longer than the stale limit.

Each run row shows its tokens and one cost in USD. The cost is the AI usage
value when Copilot reported it. If not, it is the list-price estimate. The word
after the amount says which one it is: `usage` or `list`. Premium requests are
a separate count in the tooltip. Each step on the run page also shows one
cost, in the same order, and the other units are in its tooltip. The page never
adds two units together. The `i` mark beside each cost heading explains the
units.

Under the run's status line, the run page shows its six stages: Triage, Plan,
Implement, Verify, Review and Deliver. Each stage shows the time the run spent
in it. A stage the run came back to shows how often, for example `Review ×2`.
The stage the run is in has a blue bar, and the stage where it stopped has a
red bar. Runs from before this change have no stages. Their Steps list still
shows each call.

An active run whose last three attempts repeat shows a red **possibly looping**
chip on its row and on its status line. A run repeats when each attempt failed
with a reason that starts the same way. It also repeats when the same check,
for example review or CI, sent the work back each time. Point at the chip to see which repeat
it found. The chip is only a hint. The run goes on until its own limits stop it.

### Approve, answer or retry a halted run

A run that needs you shows a panel on its detail page. The panel says why the
run stopped and what to do. It has a colored border only when the page can act
on the run.

The run list puts these runs at the top, under a "Needs you" row. Each run says what it needs: **Needs approval**, **Needs answers**,
**Needs retry**, **Needs attempt**, **Needs acceptance**, **Needs scope approval**, or **Stopped, needs you**. A filled badge means the page can
act on the run. A run the page cannot act on, for example after its reply window
expired, says **Stopped, needs you**. A stopped run gives a `factory show` command to copy and run in
a terminal. The "Needs you" row counts the waiting runs on the page. When more runs
wait than one page shows, it says so, for example "Needs you · 15 of 18". It then links to
the list of all runs that need you. While a run needs you, the browser tab title starts with the count,
for example `(2)`, and the tab icon shows a dot.
To get a browser notification as well, choose **Notify me** in the sidebar. The
browser asks for permission only then. The page tells you once about each run that
starts to need you or that becomes stale. Click the notification to open that run.
Choose **Notify me** again to stop.

- For a risk approval, choose **Approve**. A dialog shows the decision
  requested, the actions the approval allows, the actions it does not, and the
  conditions in force. It says that agent work starts. Nothing is recorded until
  you choose **Confirm approval**. Press Escape to close the dialog.
- For plan decisions, type one answer for each decision. Each answer is one line
  of 1 to 500 characters, with no path, link or secret in it. **Submit answers**
  stays disabled until every field is filled in.

- For a run that failed to open its first pull request, choose **Retry
  publishing**. The factory publishes the reviewed work again. It runs no agent
  and grants no new attempt. Each retry counts as a reopen.
- For a run that used all of its implementation attempts, choose **Grant +1
  attempt**, then **Confirm +1 attempt**. The run goes back to the implement step
  and makes exactly one more attempt. That attempt sees the last failure. If it
  fails, the run stops here again. Each grant counts as a reopen. To redo the
  work with fresh attempts on a higher route, escalate the run instead.
- For a run whose review did not settle, read the open findings on the Review
  tab. To ship them, choose **Accept with findings**, then **Confirm accept with
  findings**. The factory publishes the reviewed work as it is. The pull request
  lists the findings and gets the `factory:needs-look` label, and the factory
  does not merge it. To have the agent fix them, escalate the run with a note.
- For a run whose changes went beyond the approved scope, the panel lists the
  files outside it. To allow them, choose **Approve wider scope**, then
  **Confirm wider scope**. The files join the approved scope of this run only.
  The factory checks the same work again, then reviews and publishes it. It
  makes no new attempt. If the work changed in the worktree since the stop, the
  service refuses. A new file outside the scope stops the run again.
- For a run that CI stopped while its pull request is open, choose **Retry CI
  repair**, then **Confirm CI repair retry**. If the checks did not finish in
  time, the factory watches CI again. If CI failed, the factory makes one more
  CI repair attempt on top of the budget and pushes the fix. If the pull request
  was closed, merged or got a new commit since the stop, the service refuses. If
  CI fails again, the run stops here again.

The dashboard does not resume the run. It writes one request file in the run
directory. The factory service reads it and reopens the run. The panel then
shows "Approved at <time>, queued for the factory service". An answer shows
"Answers sent at <time>, queued for the factory service". A retry shows "Retry
requested at <time>, queued for the factory service". A grant shows "One more
attempt granted at <time>, queued for the factory service". An acceptance shows
"Findings accepted at <time>, queued for the factory service". A scope approval
shows "Wider scope approved at <time>, queued for the factory service". A CI
retry shows "CI repair retry requested at <time>, queued for the factory
service". The sentence then says what the service does with it:

| Sentence | Meaning |
| --- | --- |
| `The factory service is running and picks it up on its next tick.` | The service ticked in the last three poll intervals. |
| `The factory service picked it up and starts the run when a slot is free.` | The service ticked after the request, but has no free slot or daily quota yet. |
| `The factory service is not running, start it with factory start.` | No tick in the last three poll intervals. The request waits until the service runs. |
| `If factory start is not running, start it.` | The dashboard cannot tell. |

#### Service liveness

On each cycle, `factory start` writes `service-heartbeat.json` in the data
directory, with its process id, host and tick time. The dashboard reads this
file. The service counts as running while its last tick is at most three
`scheduler.poll_interval_seconds` old. Under the live dot in the sidebar, the
page shows `Service running · ticked 3 s ago` or `Service not running`. A
missing or broken file reads as not running. The process id and host stay off
the page.

#### When the service refuses a request

The dashboard saves your request. The factory service reads it later and can
still refuse it. The panel then shows one sentence above the new panel, and the
request stays on file marked stale. These sentences come from the service.

| Sentence | Meaning |
| --- | --- |
| `approval expired, approve again` (`answers expired, send them again` for answers) | The reply window ended before the service read the request. |
| `reopen limit reached, inspect with factory show` | The run used all of its reopens. |
| `the run changed, review again` | The run moved on. Read the new panel, then act again. |
| `the run state changed, review again` | The run no longer waited for you when the service read the request. A GitHub reply can do this. |

#### When the request failed

The dashboard checks a request before it saves anything. If it refuses the
request, or the page gets no answer, the status line shows one of these messages
and focus moves to it. These messages come from the page.

| Message | Cause |
| --- | --- |
| `dashboard restarted, open the new link from factory dashboard` | The token no longer matches (`401`). Open the new link that `factory dashboard` printed. |
| `open the dashboard from the link it printed` | The request did not come from the page (`403`). |
| `this run no longer exists` | The run was removed (`404`). |
| `already approved` (`answers already sent` for answers) | A request for this run and context exists. |
| `reopen limit reached` | The run used all of its reopens. |
| `this run needs a different action, reload the page` | The run needs an answer, not an approval, or the other way round. |
| `the run changed, review again` | The run is in a new episode or no longer waits. |
| `approval expired, approve again` (`answers expired, send them again` for answers) | The reply window ended. |
| `decision N: use 1 to 500 characters, one line, ...` | The answer for decision N is not valid. |
| `the request failed, try again` | Any other failure, or no answer from the server. |

Answers you typed stay in the form when a request fails. If the run moved to a
new episode or context, the form starts empty.

### Stop a working run

A working run shows **Stop run** next to its badge. The first click arms it and
says what happens. **Confirm stop** sends the request, and **Keep running** puts
the button back. `factory cancel <run>` does the same from the terminal.
While the stop waits, the button says **Stop requested** with the time, also
after a reload, and you cannot send a second one.

The process that drives the run stops the agent or the running check command
within a few seconds. A run that waits for CI stops within one poll interval.
The run halts in Needs you with its workspace and commits kept.

### Steer a run with a note

A working run, and a halted run that can resume, show a note box under Needs
you. Type what the agent must do or avoid, then select **Send note**. `factory steer <run> "<note>"`
does the same from the terminal.

The next agent call gets the note as an operator section at the end of its
prompt. Only that one call gets it. A working run reads it at its next call, not
in the middle of one. A halted run keeps the note until it resumes. A halt that
cannot resume, such as a stalled run, refuses a note: no later call reads
it. A note is at most 2,000 characters, and a note that
holds a secret is refused. One note waits at a time. The box shows the waiting
note, and **Replace note** sends a new one in its place.

If you do not like the change or pull request of a run, run
`factory escalate <run> --note "<what to change>"`. The run does the work again
one route higher on the same branch and pushes to the same pull request. It uses
the repository, runtime and models it started with. A run that a process works on, or whose change is merged, cannot
escalate. See [factory escalate](../reference/cli.md#factory-escalate).

### Escalate a run

A run that can escalate shows **Escalate this run** on its page, with a note
box. The button names the new route, for example **Escalate to CRITIQUE**. Type
what to change, then select it. The dashboard starts `factory escalate` for the
run as its own process. You do not need `factory start` for this.

A line at the top of the run page then shows the escalation, for example
"Escalated SINGLE → CRITIQUE · 2026-10-17 18:42 UTC · Use the shared
validator. · running". It ends in "finished", or in "failed:" and the error,
for example when a process holds the workspace lock. The run list marks the run
"escalating", "escalated" or "escalation failed". The run page always shows the
route chip, also for `FULL`.

The dashboard cannot reconfigure anything. It can retry only publishing, and it
can grant only one implementation attempt at a time. It can accept only the open
findings of a review impasse. It can widen the scope only by the files listed in
the panel, and only for that run. It can retry CI only on the commit CI stopped
on, with one CI repair attempt at a time.
Authority stays with the workflow controller and the factory service.

It is built from the Python standard library. No framework, no npm, no bundler,
no build step.

## Background service (macOS)

An opt-in per-user launchd agent that runs `factory start`.

```bash
factory service install \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml \
  --runtime copilot \
  --model-profile economy

factory service status --json
factory service uninstall
```

It writes exactly one plist under `~/Library/LaunchAgents` and nothing under
`/Library`. There is no root `LaunchDaemon`.

Archive extraction, factory execution, and software upgrades never install
a service automatically. Installation occurs only when you run this command.

`service install` refuses if:

- the platform is not macOS,
- the given configuration does not set `scheduler.enabled`,
- `factory doctor` reports any error.

It defaults to `--runtime fake`, so an installed-and-forgotten agent cannot
spend money. Use `--runtime copilot` to opt in deliberately.

Use `--model-profile economy` to persist the packaged lower-cost routing
selection in arguments for the LaunchAgent.

Use `--runtime claude-code --model-profile claude` to run the agents on a
Claude subscription. Log in once with `claude` in a terminal first. The
runtime does not load your Claude Code settings, plugins, hooks, MCP servers
or memory, and it saves no session. Claude Code reports a list price, not a
bill, so the factory stores it as `list_price_estimate_usd` (ADR-045).

Useful flags:

- `--executable` points at a specific `factory` build.
- `--allow-source-dev` permits installing from a source checkout, which is
  otherwise refused.
- `--label` changes the LaunchAgent label from
  `com.github.software-agent-factory`.

The installer captures a `PATH` snapshot, because launchd agents inherit a
minimal environment and do not find `git`, `gh`, or `copilot` without it.

The stdout and stderr output of launchd goes to `/dev/null`. The factory writes
its own bounded rotating log under the data directory. A stdio file captured
by launchd is never rotated. `KeepAlive` is set to `Crashed` only. No exit code
(including configuration-error code `2`) can produce a restart loop.

`service uninstall` unloads the agent and removes the plist. It leaves every
run, artifact and workspace on disk. Uninstalling stops future polling.
It does not delete history.

## Housekeeping

Workspaces are preserved by default. They accumulate. Delete the ones you no
longer need under `<data_dir>/workspaces/`, and check `factory status` for
orphaned worktrees first.

## Next

- [Troubleshooting](troubleshooting.md)
- [CLI reference](../reference/cli.md)
