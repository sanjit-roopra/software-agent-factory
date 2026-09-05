# Safety and trust boundaries

The design rule is one sentence: **LLMs provide intelligence, the factory
provides authority.**

## What agents may do

- Understand a work item, refine requirements, research, plan.
- Edit source code and write tests inside their assigned Git worktree.
- Run commands (implementer only).
- Review a change and diagnose a failure.

## What agents may not do

- Transition workflow state.
- Grant themselves a retry.
- Choose which model runs.
- Pass or waive a quality gate.
- Claim a task, or take a task from another run.
- Push, merge, or change branch protection.
- Deploy anything.
- See production credentials.
- Decide whether their own output is accepted.

Every item on that list is deterministic Python. A prompt cannot change it,
because a prompt is not what enforces it.

## Off by default

| Feature | Default |
| --- | --- |
| Agent runtime | `fake` — no model calls, no cost |
| `pull_request.enabled` | `false` |
| `ci.enabled` | `false` |
| `scheduler.enabled` | `false` |
| Dashboard | not running |
| launchd service | not installed |

With those defaults, `factory run` performs no network access and makes no paid
model call. The test suite runs entirely offline: it never calls a model and
never reaches GitHub.

## Network access

Nothing in the factory contacts the network unless you turned something on.

| Trigger | Talks to |
| --- | --- |
| `--runtime copilot` | GitHub Copilot, through the `copilot` CLI. Paid. |
| `pull_request.enabled` | GitHub, through `gh`. |
| `ci.enabled` | GitHub, through `gh`. |
| `scheduler.enabled` | GitHub Issues, through `gh`. |
| Your own `repository.commands` | Whatever they contact. `uv sync` hits a package index. |

There is no telemetry, no analytics, no crash reporting and no exporter. Logs
stay in the data directory.

## Money

`--runtime copilot` is the only thing that spends. It is never the default, on
any command.

Two extra bounds exist for the daemon:

- `factory service install` defaults to `--runtime fake`, so an
  installed-and-forgotten service cannot spend.
- `scheduler.max_runs_per_day` (default `20`) caps claims per UTC day,
  independently of concurrency.

Token usage and cost are reported only if the runtime returns them. No runtime
does today, so those fields stay unknown. They are never defaulted to zero and
never reconstructed from a price table. Use GitHub Copilot billing for real
numbers.

## Credentials

- The `copilot` child process starts with GitHub credential variables removed:
  `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`,
  `GITHUB_PAT`, `GIT_ASKPASS` and the Actions token variables. Agents never
  receive GitHub credentials.
- Only controller-owned code passes a token to `gh`, and only through the
  subprocess environment — never as a command-line argument, where it would land
  in the process list.
- Repository commands run with an environment allowlist: `PATH`, `HOME`, `LANG`,
  `TERM` and whatever you named in `env_passthrough`. Nothing else is inherited.
- Captured command output and structured logs are redacted for token-shaped
  strings before they are written.

## Repository safety

- Every work item runs in its own Git worktree under the data directory. The
  source checkout is not modified in place.
- Workspace paths are sanitized and must stay inside the workspace root. Cleanup
  refuses a path outside it.
- A short-lived exclusive lock prevents two processes owning the same work item.
- No command is run through a shell. Everything is an argument list.

## Git and publishing

- Branch names must start with `repository.branch_prefix` and may not be the
  base branch.
- The remote host must be in `pull_request.allowed_hosts`.
- The changed-file count must be within `repository.max_changed_files`.
- No changed file may match `repository.protected_file_patterns`.
- Scope drift is re-checked at the pull request boundary.
- Pull requests are drafts by default.
- The factory never force-pushes and never merges.

## Quality gates

Deterministic evidence comes first. Lint, type checks, tests, build, the changed
file list and the Git diff are computed by the factory, not reported by an agent.

The tester and reviewer receive the controller-derived diff, the changed files
and the deterministic results. They never see the implementer's own summary of
what it did. The implementer's success claim is not a gate.

The reviewer's model family must differ from every worker's. Configuration
enforces this.

Broken required checks cannot reach the tester or reviewer at all.

## Bounded everything

There is no unlimited retry loop anywhere.

| Budget | Default | Bounds |
| --- | --- | --- |
| `retries.same_model_attempts` | `2` | Retries before escalating to a stronger model. |
| `retries.max_total_attempts` | `6` | Implementation attempts per run. |
| `scope_drift.max_replans` | `1` | Replans after scope drift. |
| `ci.repair_attempts` | `3` | CI repair cycles. |
| `ci.max_wait_seconds` | `1800` | CI polling. |
| `factory.agent_timeout_seconds` | `900` | One agent invocation. |
| `repository.command_timeout_seconds` | `900` | One repository command. |
| `scheduler.max_concurrent_tasks` | `1` | Concurrent runs, max `2`. |
| `scheduler.max_runs_per_day` | `20` | Claims per UTC day. |
| `--max-scanned-runs` | `1000` | Run files parsed per `status` call or dashboard request. |

Budgets are persisted on the run. A restart does not reset them.

## Recovery is conservative

A persisted, non-terminal run left behind by a dead process is transitioned to
`NEEDS_HUMAN` through the controller. It is never auto-resumed. No paid retry is
spent, the budget is untouched, and the workspace and artifacts stay on disk.

`factory status` reports stale locks, orphaned worktrees and abandoned runs as
findings. Repairing one is an explicit operator action.

## The dashboard

The only exception to the V1 ban on web UIs, and deliberately a viewer rather
than a control plane.

- Started only by `factory dashboard`. It is the only command that opens a
  socket.
- Binds `127.0.0.1` only. Not configurable.
- `GET` only.
- Token generated per process, printed once, never logged.
- Renders the run list, run detail, workflow state, attempt history and derived
  metrics. Never command logs, diffs, prompts or raw artifacts.
- Data minimization is applied twice, independently: the detail provider builds
  a typed object containing only summary fields and attempt metadata — never
  failure reasons, agent reasoning or raw artifacts — and the request handler
  then allowlists the fields it renders. A future provider mistake still cannot
  leak content.
- Cannot approve, retry, cancel or reconfigure anything.
- Python standard library only. No framework, no npm, no bundler, no build step.

## The service

- macOS only, per-user, opt-in.
- Exactly one plist under `~/Library/LaunchAgents`. Nothing under `/Library`. No
  root `LaunchDaemon`.
- Installed only by `factory service install`. Never as a side effect of
  extracting an archive, running the factory or upgrading it.
- Refuses unless the configuration enables the scheduler, and refuses if
  `factory doctor` reports any error.
- Defaults to `--runtime fake`.
- `KeepAlive` is `Crashed`-only, so no exit code can produce a restart loop.
- Uninstall removes the plist and leaves all history on disk.

## Release artifacts

Release archives are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are deferred. macOS quarantines a downloaded archive until you
clear the attribute yourself.

Releases are write-once by workflow convention only. The release workflow
refuses to replace an existing release, but nothing stops an edit or delete
through the GitHub UI or API. GitHub's release immutability is a repository
setting, it is off by default, and the current releases report
`immutable=false`. Enable it in the repository settings before relying on
platform enforcement.

Verify `SHA256SUMS` before you extract anything.

See [Releases](../project/releases.md).

## What does not exist

Not implemented, not designed, and nothing in the codebase requires them:
autonomous merge, autonomous deployment, staging promotion, remote workers,
Docker or Kubernetes sandboxes, a hosted service, a multi-user application, a
control plane, telemetry, and long-term semantic memory.
