# CLI reference

The executable is `factory`. From a source checkout, prefix every command with
`uv run`.

```text
factory [OPTIONS] COMMAND [ARGS]...

  Local-first autonomous software engineering factory.

Options:
  --version, -V   Show the factory version and exit.

Commands:
  run         Run one work item synchronously through the factory workflow.
  project     Derive and execute a bounded project work breakdown.
  start       Poll a GitHub Issues backlog and dispatch eligible work.
  runs        List persisted runs, most recently created last.
  show        Show the persisted details of one run as JSON.
  doctor      Check this machine's prerequisites for the configured feature set.
  status      Report derived run metrics and operational health, read-only.
  skill       Inspect, validate and refresh repository guidance.
  dashboard   Serve the read-only local dashboard until interrupted.
  service     Manage the opt-in per-user macOS launchd service.
```

## Common options

Most commands accept these.

| Option | Default | Effect |
| --- | --- | --- |
| `--config <path>` | packaged config | Factory config YAML to load. |
| `--data-dir <path>` | `factory.data_dir` | Override the configured data directory. |
| `--model-profile <name>` | `default` | Select the top-level `models` routing or a complete named entry from `model_profiles`. Available on agent-invoking commands, `doctor`, and `service install`. |

`--data-dir` is how you keep an experiment out of `~/.software-factory`. The
test suite uses it for exactly that.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The command failed, or a run ended in `NEEDS_HUMAN` or `FAILED`. |
| `2` | Configuration error or missing prerequisite; nothing was started. |

---

## factory run

Run one work item synchronously through the whole workflow.

```bash
factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --acceptance-criterion "Empty or whitespace-only names return HTTP 400."
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--title <str>` | yes | — | Short title for the work item. |
| `--description <str>` | yes | — | Description of the work to perform. |
| `--acceptance-criterion <str>` | no | none | Required outcome; repeat as needed. |
| `--constraint <str>` | no | none | Work item constraint; repeat as needed. |
| `--work-item-id <str>` | no | random | Stable work item id. Use the scheduler's `tracker-owner/repo#12` form so a manual run and the daemon cannot duplicate the same work. |
| `--runtime <fake\|copilot>` | no | `fake` | `fake` makes no model calls. `copilot` is paid. |
| `--model-profile <name>` | no | `default` | Select a configured model profile, such as the packaged `economy` profile. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Prints the run id, final state, workspace path and the controller-derived
changed files. Creates an isolated Git worktree under the data directory.

Refuses with exit code `2` if a prerequisite for the enabled feature set is
missing.

---

## factory project

Turn a high-level project description into the smallest sufficient set of
work items, then execute them through the existing workflow.

```bash
factory project \
  --repo ~/projects/example \
  --title "Build customer onboarding" \
  --description "Add signup, email verification, and the first-login flow." \
  --acceptance-criterion "A new customer can complete onboarding." \
  --runtime copilot
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--title <str>` | unless resuming | — | Short project title. |
| `--description <str>` | unless resuming | — | High-level product or feature description. |
| `--acceptance-criterion <str>` | no | none | Required outcome; repeat as needed. |
| `--constraint <str>` | no | none | Project constraint; repeat as needed. |
| `--project-id <str>` | no | random | Stable project identifier. |
| `--resume` | no | `false` | Reconcile the stored project; requires `--project-id` and reuses its brief and plan. |
| `--github-repo <OWNER/NAME>` | no | none | Create one GitHub issue per validated task and close it after integration or confirmed merge. |
| `--runtime <fake\|copilot>` | no | `fake` | `fake` creates one deterministic task; `copilot` derives the real plan. |
| `--model-profile <name>` | no | `default` | Select a configured model profile, such as `economy`. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

The planner is read-only and returns a typed `ProjectPlan`. Task ids are
contiguous, dependencies may point only to earlier tasks, and at most 12 tasks
are accepted. One task is preferred whenever one coherent change is
sufficient.

Dependency-ready tasks run in waves using
`scheduler.max_concurrent_tasks` (`1` or `2`). Each task still uses the full
triage, refine, plan, implement, verify, test, and review pipeline. Successful
task commits are cherry-picked onto one persistent project integration branch,
so downstream tasks see predecessor changes. The configured repository commands
run once more against the complete integration branch before the project is
`DONE`. A conflict, failed child run, final verification failure, or
human-approval gate stops the project instead of guessing.

With PR/CI/merge disabled, project execution produces a local integration
branch. With all three enabled, tasks run serially through PR creation, bounded
CI repair and guarded automatic merge to the configured target. Each task
starts from the refreshed target including its merged predecessors. Final
verification and confirmed child merges determine project completion.
GitHub issue publication is optional and does not
apply the scheduler's `agent-ready` label, so the project command remains the
single execution owner.

`--resume` uses the stored brief and immutable plan, reconciles persisted
child delivery checkpoints, and never resets attempt budgets. It refuses
policy/repository drift and does not replay ambiguous interrupted agent work.
See [Autonomous project delivery](../guides/projects.md#autonomous-delivery).

Artifacts are stored under:

```text
<data_dir>/projects/<project-id>/
├── project-brief.json
├── project-plan.json
├── execution.json       # includes planner invocation usage when reported
└── logs/
```

---

## factory start

Poll a GitHub Issues backlog and dispatch eligible work.

```bash
factory start --repo ~/projects/example --github-repo acme/example --config ~/my-factory.yaml
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--github-repo <str>` | yes | — | Backlog repository as `OWNER/NAME`. |
| `--runtime <fake\|copilot>` | no | `fake` | Agent runtime. |
| `--model-profile <name>` | no | `default` | Select a configured model profile for every dispatched run. |
| `--once` | no | off | Run one bounded tick instead of polling forever. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Refuses to run, and never touches GitHub, unless `scheduler.enabled` is true in
the configuration. Blocks in the foreground; Ctrl-C stops it after the current
tick.

See [GitHub backlog, PRs and CI](../guides/github.md).

---

## factory runs

List persisted runs, most recently created last.

```bash
factory runs
```

Tab-separated: run id, state, work item id, creation timestamp.

Options: `--config`, `--data-dir`.

---

## factory show

Show the persisted details of one run as JSON.

```bash
factory show run-9bb36bbbdf114f53bd9599a103122976
```

| Argument | Required | Effect |
| --- | --- | --- |
| `run_id` | yes | The run id to display. |

Options: `--config`, `--data-dir`.

Prints the work item text, so redact before sharing.

---

## factory doctor

Check this machine's prerequisites for the configured feature set.

```bash
factory doctor
factory doctor --json --config ~/my-factory.yaml
```

| Option | Default | Effect |
| --- | --- | --- |
| `--runtime <fake\|copilot>` | `fake` | Check prerequisites for this runtime. `copilot` additionally requires the `copilot` executable. |
| `--model-profile <name>` | `default` | Validate this configured model profile. |
| `--json` | off | Emit the report as JSON. |
| `--config <path>` | packaged | Config YAML. |
| `--data-dir <path>` | configured | Data directory override. |

Checks the platform, whether this is a frozen or source build, `launchctl`,
`git`, configuration validity, the executables behind configured repository
commands, and data directory writability. `gh` is checked only when
configuration enables pull requests, CI observation or the scheduler.

Never makes a paid model call. The only `copilot` interaction is a bounded
`copilot --version` probe.

Exits nonzero if any check errored. Warnings alone do not fail it.

---

## factory status

Report derived run metrics and operational health. Read-only.

```bash
factory status
factory status --json --limit 50 --offset 50
```

| Option | Default | Effect |
| --- | --- | --- |
| `--limit <int>` | `20` | How many runs to list. Minimum `1`. |
| `--offset <int>` | `0` | Where to start the listing. |
| `--stale-after-seconds <int>` | `scheduler.stall_timeout_seconds` | Idle time before a non-terminal run counts as stale. |
| `--max-scanned-runs <int>` | `1000` | Hard cap on run files parsed per call. |
| `--json` | off | Emit snapshot and health as JSON. |
| `--config <path>` | packaged | Config YAML. |
| `--data-dir <path>` | configured | Data directory override. |

Everything is recomputed from persisted artifacts on each call. This command
never creates, mutates or repairs a run, a workspace, a lock or the data
directory itself. A truncated or partially unreadable scan reports `DEGRADED`.
For normal workflow runs, the human and JSON views include totals derived from
persisted invocation records. Missing runtime-reported fields remain unknown,
and premium-request cost and nano-AIU are raw Copilot units, not USD.

---

## factory skill

Inspect, validate and refresh the repository guidance used by the optional
post-green polish attempt. Guidance lives under the configured data directory,
in repository-scoped storage keyed by the repository and its dependency
fingerprint — never inside the target repository. See
[Repository skills and overlays](../guides/repository-skills.md).

### factory skill path

Print the generated-skill and overlay locations discovered for a repository.

```bash
factory skill path --repo ~/projects/example
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Read-only. It creates nothing, including the overlay file.

The repository key comes from the canonical local Git common directory, so
linked worktrees of one checkout report the same directory, and moving or
re-cloning a repository reports a different one. Run this before moving a
repository if you want to move or copy its guidance to the new location.

### factory skill validate

Validate the current generated skill and overlay for a repository.

```bash
factory skill validate --repo ~/projects/example
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Reports whether the stored guidance matches the repository's current dependency
fingerprint, whether every cited source is still inside the configured
allowlists, and why an overlay would be ignored. Read-only: it never repairs,
reformats, rewrites or creates a file, and an invalid overlay is left exactly
as written.

### factory skill refresh

Refresh generated guidance for a repository, explicitly.

```bash
factory skill refresh --repo ~/projects/example --runtime copilot
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Path to the target Git repository. |
| `--runtime <fake\|copilot>` | no | `fake` | `fake` makes no model calls. `copilot` is paid. |
| `--model-profile <name>` | no | `default` | Select the Researcher configuration used for generation. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Touches generated guidance only. It never creates, rewrites or deletes your
`repository-skill-overlay.yaml`, and it changes no run, workspace or
configuration.

This is the only command that may replace an existing generated file, and it is
what a run's warning points to when stored guidance no longer revalidates.
The standalone generation invocation is persisted as `last-invocation.json` in
the neutral generation directory. Each refresh replaces that command-specific
record; normal run telemetry remains in the run artifact.

---

## factory dashboard

Serve the read-only local dashboard until interrupted.

```bash
factory dashboard
factory dashboard --port 0 --open-browser
```

| Option | Default | Effect |
| --- | --- | --- |
| `--port <int>` | `8765` | Loopback port. `0` asks the OS for a free port. |
| `--open-browser` | off | Open the tokenized URL in the default browser. |
| `--max-scanned-runs <int>` | `1000` | Hard cap on run files parsed per request. |
| `--config <path>` | packaged | Config YAML. |
| `--data-dir <path>` | configured | Data directory override. |

The dashboard shows active and completed projects, their task/PR/merge progress,
models used by project and task invocations, individual workflow runs,
attempts, usage totals and operational health. When Copilot reports nano-AIU,
the dashboard converts it to an AI usage value in USD using GitHub's published
conversion of 1 AI credit to $0.01. This is the priced value of the reported
model usage, not necessarily the amount added to the bill: included or pooled
credits may cover it. Input/output token counts and legacy premium-request
units remain separate metrics and are never multiplied.

Blocks in the foreground. Binds `127.0.0.1` and nothing else, answers `GET`
only, and requires a token generated for that process. The tokenized URL is
printed to stdout once and never written to the log. Ctrl-C stops it and closes
the socket.

This is the only command that opens a socket.

---

## factory service

Manage the opt-in per-user macOS launchd service. macOS only.

### factory service install

```bash
factory service install \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | — | Absolute path to the target Git repository. |
| `--github-repo <str>` | yes | — | Backlog repository as `OWNER/NAME`. |
| `--config <path>` | no | packaged | Config the service loads. Must enable `scheduler.enabled`. |
| `--data-dir <path>` | no | configured | Data directory for the service. |
| `--runtime <fake\|copilot>` | no | `fake` | Runtime the service runs with. |
| `--model-profile <name>` | no | `default` | Profile retained in the installed `factory start` arguments. |
| `--executable <path>` | no | this build | Explicit `factory` executable to run. |
| `--label <str>` | no | `com.github.software-agent-factory` | LaunchAgent label. |
| `--allow-source-dev` | no | off | Permit an executable in an otherwise-refused location, such as a source checkout. |
| `--json` | no | off | Emit the resulting status as JSON. |

Writes one plist under `~/Library/LaunchAgents`. Refuses unless the target
configuration enables the scheduler, and refuses if `factory doctor` reports any
error. Defaults to `--runtime fake` so an installed-but-forgotten agent cannot
spend money.

Nothing installs a service as a side effect of extracting an archive, running
the factory or upgrading it.

### factory service status

```bash
factory service status --json
```

| Option | Default | Effect |
| --- | --- | --- |
| `--label <str>` | `com.github.software-agent-factory` | LaunchAgent label to inspect. |
| `--json` | off | Emit as JSON. |

Read-only.

### factory service uninstall

```bash
factory service uninstall
```

| Option | Default | Effect |
| --- | --- | --- |
| `--label <str>` | `com.github.software-agent-factory` | LaunchAgent label to remove. |
| `--json` | off | Emit the result as JSON. |

Unloads the agent and removes the plist. Leaves every run, artifact and
workspace on disk.
