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
  dashboard   Serve the local dashboard until interrupted.
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

## Runtimes

Each command that starts agents takes `--runtime <fake|copilot|pi>`. The default
is `fake`, which makes no model calls. Pi is the recommended real runtime. See
[Real pi runs](../get-started/pi.md).

An unknown runtime value is rejected.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The command failed, or a run ended in `NEEDS_HUMAN` or `FAILED`. |
| `2` | Configuration error or missing prerequisite. Nothing was started. |

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
| `--repo <path>` | yes | none | Path to the target Git repository. |
| `--title <str>` | yes | none | Short title for the work item. |
| `--description <str>` | yes | none | Description of the work to perform. |
| `--acceptance-criterion <str>` | no | none | Required outcome. Repeat as needed. |
| `--constraint <str>` | no | none | Work item constraint. Repeat as needed. |
| `--work-item-id <str>` | no | random | Stable work item id. Use the scheduler's `tracker-owner/repo#12` form so a manual run and the daemon cannot duplicate the same work. |
| `--runtime <fake\|copilot\|pi>` | no | `fake` | `fake` avoids model calls. If `routing.enabled` is `true`, the factory still calls Jev over HTTPS. `copilot` makes paid Copilot calls. `pi` makes paid calls through pi. |
| `--model-profile <name>` | no | `default` | Select a configured model profile, such as the packaged `economy` profile. |
| `--no-risk-assessment` | no | off | Turn off risk assessment for this run. See `risk_assessment` in the configuration reference. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Prints the run id, final state, workspace path and the controller-derived
changed files.
If routing is active, the command also prints the effective execution route.
Creates an isolated Git worktree under the data directory.

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
| `--repo <path>` | yes | none | Path to the target Git repository. |
| `--title <str>` | unless resuming | none | Short project title. |
| `--description <str>` | unless resuming | none | High-level product or feature description. |
| `--acceptance-criterion <str>` | no | none | Required outcome. Repeat as needed. |
| `--constraint <str>` | no | none | Project constraint. Repeat as needed. |
| `--project-id <str>` | no | random | Stable project identifier. |
| `--resume` | no | `false` | Reconcile the stored project. Requires `--project-id` and reuses its brief and plan. |
| `--github-repo <OWNER/NAME>` | no | none | Create one GitHub issue per validated task and close it after integration or confirmed merge. |
| `--runtime <fake\|copilot\|pi>` | no | `fake` | `fake` creates one deterministic task. `copilot` and `pi` derive the real plan. |
| `--model-profile <name>` | no | `default` | Select a configured model profile, such as `economy`. |
| `--no-risk-assessment` | no | off | Turn off risk assessment for every child run. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

The planner is read-only and returns a typed `ProjectPlan`. Task ids are
contiguous. Dependencies can point only to earlier tasks. At most 12 tasks
are accepted. One task is preferred whenever one coherent change is
sufficient.

Dependency-ready tasks run in waves using
`scheduler.max_concurrent_tasks` (`1` or `2`). Each task still uses the full
triage, plan, implement, verify, test, and review pipeline. Successful
task commits are cherry-picked onto one persistent project integration branch,
so downstream tasks see predecessor changes. The configured repository commands
run once more against the complete integration branch before the project is
`DONE`. A conflict, failed child run, final verification failure, or
human-approval gate stops the project instead of guessing.

With PR/CI/merge disabled, project execution produces a local integration
branch. With all three enabled, tasks run serially through PR creation.
Then they pass through bounded CI repair and guarded automatic merge.
Each task
starts from the refreshed target including its merged predecessors. Final
verification and confirmed child merges determine project completion.
GitHub issue publication is optional.
It does not apply the scheduler's `agent-ready` label.
Thus, the project command remains the single execution owner.

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
| `--repo <path>` | yes | none | Path to the target Git repository. |
| `--github-repo <str>` | yes | none | Backlog repository as `OWNER/NAME`. |
| `--runtime <fake\|copilot\|pi>` | no | `fake` | Agent runtime. |
| `--model-profile <name>` | no | `default` | Select a configured model profile for every dispatched run. |
| `--no-risk-assessment` | no | off | Turn off risk assessment for every dispatched run. |
| `--once` | no | off | Run one bounded tick instead of polling forever. |
| `--config <path>` | no | packaged | Config YAML. |
| `--data-dir <path>` | no | configured | Data directory override. |

Refuses to run, and never touches GitHub, unless `scheduler.enabled` is true in
the configuration. It blocks in the foreground. Press Ctrl-C to stop it after
the current tick.

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

The JSON output includes the initial route, effective route, and route decision details.
You can also inspect the typed `route-decision.json` artifact in the run directory:

```bash
cat <data_dir>/runs/<run-id>/route-decision.json
```

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
| `--runtime <fake\|copilot\|pi>` | `fake` | Check prerequisites for this runtime. `copilot` additionally requires the `copilot` executable. `pi` additionally requires pi, Node and a provider credential. |
| `--model-profile <name>` | `default` | Validate this configured model profile. |
| `--json` | off | Emit the report as JSON. |
| `--config <path>` | packaged | Config YAML. |
| `--data-dir <path>` | configured | Data directory override. |

Checks the platform and the build type.
Also checks `launchctl`, `git`, configuration validity, and data directory writability.
Checks the executables behind configured repository commands.
Checks `gh` only when the configuration enables pull requests, CI observation,
or the scheduler.

Never makes a paid model call. The only `copilot` interaction is a bounded
`copilot --version` probe.
`factory doctor` does not verify the Jev key or Jev network connectivity.

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

## factory setup

```bash
factory setup --repo PATH [--dry-run] [--publish] [--config FILE] [--data-dir DIR]
```

Adds the missing development tools to a repository (ADR-034).

1. The factory detects the stack and the tools that the repository already has.
2. It plans the missing tools: a formatter, a linter, a type checker, a test runner and the mutation tool.
3. It runs the package manager of each lane in a factory worktree at the source HEAD, on its own branch.
   The commands change only the manifest and the lockfile. They install nothing.
   The JavaScript commands run no package scripts. Python locking can run the build backend of the project.
4. It records the plan in `.factory/setup.json` in that worktree.

The factory never replaces a tool that the repository already has.
The source checkout does not change. Without `--publish`, the factory does not commit or push.
The factory never merges a setup pull request.
If the setup worktree for the same HEAD is not clean, the command refuses to run.
If the checkout holds uncommitted changes, `--dry-run` says so, because a setup run uses HEAD.

| Option | Effect |
| --- | --- |
| `--dry-run` | Print the plan and change nothing. |
| `--publish` | Commit the setup worktree, push its branch and open a pull request. It needs `pull_request.enabled`. |

The output has one `add:` line for each command, one `write:` line for each repository file and one `note:` line for each skipped lane.
The repository files are the managed block in `AGENTS.md`, `CLAUDE.md` as a link to `AGENTS.md`, the skills in `.agents/skills/` with links in `.claude/skills/`, and a Python review agent.
If a command fails, the exit code is `1` and the factory keeps the worktree.

## factory dashboard

Serve the local dashboard until interrupted.

```bash
factory dashboard
factory dashboard --port 0 --open-browser
```

| Option | Default | Effect |
| --- | --- | --- |
| `--port <int>` | `8765` | Loopback port. `0` asks the OS for a free port. |
| `--open-browser` | off | Open the tokenized link in the default browser. |
| `--max-scanned-runs <int>` | `1000` | Hard cap on run files parsed per request. |
| `--config <path>` | packaged | Config YAML. |
| `--data-dir <path>` | configured | Data directory override. |

The dashboard shows active and completed projects, issue references, pull
requests, and merge progress. Run details show models,
verification summaries, safe artifact names, retries, and escalation status.
The factory persists an active invocation before Copilot starts.
The run lease labels it `running`, `stale`, `crashed`, or `abandoned`.
The previous completed attempt does not determine this label.
When Copilot reports nano-AIU, the dashboard converts it to an AI usage value in USD.
The conversion uses GitHub's published rate of 1 AI credit to $0.01.
This value is the price of the reported model usage.
It is not necessarily the amount added to the bill.
Included or pooled credits can cover it.
Input and output token counts remain separate metrics.
Legacy premium-request units also remain separate and are never multiplied.

The Compare view shows two runs side by side. For each agent role, it lists calls,
failed calls, models, tokens, duration and cost for run A and run B. Each run keeps
its cost in its own units. The `/api/compare` route serves this view.
The Runs view starts with one overview row. It shows the number of runs, the
succeeded, failed and active runs, and the runs that need you. It also shows total
tokens and the cost of all runs. The cost is the list-price estimate in USD and the
premium requests. The page shows each cost only when a call reported it. The page
never adds the two costs together. A note shows the failed runs and the tokens of the
last 24 hours when they differ from the totals.

The run list shows these columns for each run: title, state badge, the reason it
stopped, models, calls, duration, cost and start time. The run page starts with a
summary and the steps of the run. The long list of run facts is under Details.

Blocks in the foreground. Binds `127.0.0.1` and nothing else, and requires a token
generated for that process. It answers `GET` for reads. Two `POST` routes are the
only writes: approve a risk halt and answer plan decisions (ADR-033). The
tokenized link is printed to stdout once and never written to the log. Ctrl-C stops
it and closes the socket.

Open the printed link once. The first request returns the page and sets a session
cookie. The page then removes the token from the address bar and the current
history entry. Some browsers can still keep the first address in their visit
records until the dashboard restarts. A reload works from the cookie.

A write also needs the token in a header, which the page sends. A read that has
the token header uses the header alone. A wrong header gets `401`, even with a
right cookie. The cookie counts only when the header is absent.

The cookie is as strong as the token. Any program that holds the cookie can load
the page and read the token. This includes another local server on `127.0.0.1`
that the browser visits. The `Host` and `Origin` checks stop browsers only. Treat
anything that listens on `127.0.0.1` as trusted, including a forwarded port such
as `ssh -L` or a container port.

A new `factory dashboard` start makes a new token. Open the new link after a
restart.

This is the only command that opens a socket.

---

## factory service

Manage the opt-in per-user macOS launchd service. macOS only.

### factory service install

```bash
factory service install \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml \
  --runtime copilot \
  --model-profile economy
```

| Option | Required | Default | Effect |
| --- | --- | --- | --- |
| `--repo <path>` | yes | none | Absolute path to the target Git repository. |
| `--github-repo <str>` | yes | none | Backlog repository as `OWNER/NAME`. |
| `--config <path>` | no | packaged | Config the service loads. Must enable `scheduler.enabled`. |
| `--data-dir <path>` | no | configured | Data directory for the service. |
| `--runtime <fake\|copilot\|pi>` | no | `fake` | Runtime the service runs with. `pi` prints the startup warning. Doctor accepts only a saved pi login for the service. A shell variable does not reach the service. |
| `--model-profile <name>` | no | `default` | Profile retained in the installed `factory start` arguments. |
| `--no-risk-assessment` | no | off | Flag retained in the installed `factory start` arguments. |
| `--executable <path>` | no | this build | Explicit `factory` executable to run. |
| `--label <str>` | no | `com.github.software-agent-factory` | LaunchAgent label. |
| `--allow-source-dev` | no | off | Permit an executable in an otherwise-refused location, such as a source checkout. |
| `--json` | no | off | Emit the resulting status as JSON. |

Writes one plist under `~/Library/LaunchAgents`. Refuses unless the target
configuration enables the scheduler, and refuses if `factory doctor` reports any
error. Defaults to `--runtime fake` so an installed-but-forgotten agent cannot
spend money.

Nothing installs a service when you extract an archive, run the factory, or
upgrade the build.

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
