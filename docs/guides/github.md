# GitHub backlog, PRs and CI

Three separate integrations. All are disabled in the packaged configuration.
With the defaults, the factory makes no network request at all.

| Setting | Default | What it turns on |
| --- | --- | --- |
| `pull_request.enabled` | `false` | Commit, push and open a draft PR. |
| `ci.enabled` | `false` | Poll checks on that PR and repair some failures. |
| `scheduler.enabled` | `false` | Poll GitHub Issues and dispatch work. |

All three require `gh` on `PATH` and authenticated. `ci.enabled` also requires
`pull_request.enabled`; the config loader rejects the combination otherwise.

The factory never merges anything, ever. There is no autonomous merge and no
autonomous deployment.

## Pull requests

```yaml
pull_request:
  enabled: true
  remote: "origin"
  base_branch: null      # null = the remote's default branch
  draft: true
  allowed_hosts:
    - "github.com"
```

When enabled, a run continues past `PR_READY` to `PR_CREATED`. The controller —
not an agent — commits the worktree, pushes the branch and opens the pull
request through `gh`.

Guards before anything leaves the machine:

- The branch name must start with `repository.branch_prefix` (default
  `factory/`) and must not be the base branch.
- The remote host must be in `allowed_hosts`.
- The changed-file count must be within `repository.max_changed_files`.
- No changed file may match `repository.protected_file_patterns`.
- The scope-drift check runs again at this boundary.

It never force-pushes. It never merges. PRs are drafts by default.

Commits carry a `Co-authored-by: Copilot` trailer so machine-produced changes
are attributable in history.

Credentials go to the `gh` subprocess through its environment, never as a
command-line argument. Agents never see them.

## CI observation and repair

```yaml
ci:
  enabled: true
  poll_interval_seconds: 30
  max_wait_seconds: 1800
  repair_attempts: 3
```

After `PR_CREATED`, the run enters `CI_RUNNING` and polls the pull request's
checks. Polling is bounded by `max_wait_seconds`; it does not wait forever.

Each failing check is classified from its name and a log excerpt:

| Category | What happens |
| --- | --- |
| `CODE_FAILURE` | Bounded code repair. |
| `TEST_FAILURE` | Bounded code repair. |
| `FLAKY_TEST` | Escalates to `NEEDS_HUMAN` with evidence. |
| `INFRA_FAILURE` | Escalates to `NEEDS_HUMAN` with evidence. |
| `DEPENDENCY_FAILURE` | Escalates to `NEEDS_HUMAN` with evidence. |
| `UNKNOWN` | Escalates to `NEEDS_HUMAN` with evidence. |

Only the first two may send the run back to `IMPLEMENTING`. Everything else is a
human's call. The factory does not retry a flaky test until it passes, and it
does not guess at a broken runner.

Repair is bounded by `ci.repair_attempts`, a budget separate from the
implementation retry budget. Each repair attempt receives a small explicit
repair context — the normalized CI evidence — not the whole run history.

An unrecognized `gh` check status is treated conservatively as still pending
rather than as a pass.

## Backlog daemon

```yaml
scheduler:
  enabled: true
  poll_interval_seconds: 30
  max_concurrent_tasks: 1
  stall_timeout_seconds: 900
  required_label: "agent-ready"
  max_runs_per_day: 20
```

```bash
uv run factory start \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml
```

`factory start` refuses to run, and never contacts GitHub, unless
`scheduler.enabled` is true. Use `--once` for a single bounded tick instead of
polling forever.

### What a tick does

1. Fetch open issues in `--github-repo` carrying `required_label`
   (`agent-ready`). Pull requests are excluded.
2. Reconcile persisted runs before dispatching anything new.
3. Reserve a work item before dispatch, so the same issue cannot be picked up
   twice.
4. Dispatch through a thread pool bounded by `max_concurrent_tasks`.

Issue labels can carry a priority (numeric `p0`-style labels and named
priorities are both recognized); the highest priority found on an issue wins,
and higher priority is dispatched first.

Work items get a stable id of the form `tracker-owner/repo#12`. A manual
`factory run --work-item-id` using that same id will not duplicate scheduler
work.

### Two independent bounds

| Setting | Bounds |
| --- | --- |
| `max_concurrent_tasks` | How much runs at once. Validated to be `1` or `2`. |
| `max_runs_per_day` | How much may be *claimed* per UTC calendar day. Default `20`, `null` to disable. |

Both are reported at startup. The daily ceiling is counted from persisted run
timestamps, so it survives a restart. A tick stopped by it says `rate_limited`
rather than looking like an empty backlog.

The daily cap exists because `scheduler.enabled` and `--runtime copilot` are
independent knobs. A daemon left running with the real runtime would otherwise
spend money at whatever rate the backlog allows.

### Recovery

A persisted, non-terminal run left behind by a dead process is transitioned to
`NEEDS_HUMAN` through the controller. It is never auto-resumed. No paid retry is
spent, the persisted budget is untouched, and the workspace and artifacts stay
on disk for you to inspect.

### The fake runtime is a real dry run

`--runtime fake` still persists completed runs, and the scheduler will not
dispatch those backlog items again. Before polling real `agent-ready` issues,
either switch to `--runtime copilot`, or use a separate `--data-dir` for
fake-runtime testing.

## Next

- [Monitor and run continuously](operations.md)
- [Safety and trust boundaries](../reference/safety.md)
