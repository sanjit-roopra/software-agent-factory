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

Two bounds worth knowing:

- `--stale-after-seconds` overrides the staleness threshold, which defaults to
  `scheduler.stall_timeout_seconds`.
- `--max-scanned-runs` (default `1000`) caps how many run files one call parses.

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

## Read-only dashboard

```bash
factory dashboard                      # http://127.0.0.1:8765/?token=...
factory dashboard --port 0 --open-browser
```

This is the only thing in the factory that ever opens a socket. Nothing in
`factory run` or `factory start` listens on a port.

- Binds `127.0.0.1` and nothing else.
- Answers `GET` only.
- Requires a token generated for that process. The tokenized URL is printed to
  stdout once and never written to the log.
- Blocks in the foreground. Ctrl-C stops it and closes the socket.

It shows project state and task/PR/merge progress, plus the run list, run
detail, workflow state, attempt history and derived metrics. It shows no
command logs, diffs, prompts, or raw artifacts. Those items can leak repository
content and private material into a browser.

It cannot approve, retry, cancel or reconfigure anything. Authority stays with
the workflow controller.

It is built from the Python standard library. No framework, no npm, no bundler,
no build step.

## Background service (macOS)

An opt-in per-user launchd agent that runs `factory start`.

```bash
factory service install \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml

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

When a security-sensitive backlog needs the higher-cost route, use
`--model-profile security`. This route uses the Astra Tester and Sol Reviewer.

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
