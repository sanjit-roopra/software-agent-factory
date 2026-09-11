# GitHub backlog, PRs and CI

Four separate integrations. All are disabled in the packaged configuration.
With the defaults, the factory makes no network request at all.

| Setting | Default | What it turns on |
| --- | --- | --- |
| `pull_request.enabled` | `false` | Commit, push and open a draft PR. |
| `ci.enabled` | `false` | Poll checks on that PR and repair some failures. |
| `merge.enabled` | `false` | Merge verified PRs into an explicitly configured target. |
| `scheduler.enabled` | `false` | Poll GitHub Issues and dispatch work. |

All four require `gh` on `PATH` and authenticated. `ci.enabled` also requires
`pull_request.enabled`. The configuration loader rejects the combination otherwise.

Automatic merging requires its own explicit policy. There is no autonomous
deployment or permission to execute production migrations.

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

When enabled, a run continues past `PR_READY` to `PR_CREATED`. The controller
commits the worktree, pushes the branch, and opens the pull request through `gh`.
Agents do not do these actions.

The factory passes `OWNER/REPO` to `gh`, so GitHub operations use the current
authenticated `gh` account and host configuration. The Git remote can use a
different allowlisted SSH host alias for push transport.

The controller fixes the repository and GitHub host identity for the run. It
rechecks the pull request identity during CI and merge operations. A mismatch
stops delivery instead of observing or merging a different pull request.

Guards before anything leaves the machine:

- The branch name must start with `repository.branch_prefix` (default `factory/`)
  and must not be the base branch.
- The remote host must be in `allowed_hosts`.
- The changed-file count must be within `repository.max_changed_files`.
- No changed file matches `repository.protected_file_patterns`.
- The scope-drift check runs again at this boundary.

The factory never force-pushes. Pull requests are drafts by default.
Automatic delivery requires non-draft pull requests and the configured merge policy.

Commits carry a `Co-authored-by: Copilot` trailer so machine-produced changes
are attributable in history.

Credentials go to the `gh` subprocess through its environment, never as a
command-line argument. Agents never see them.

Branch push gets one bounded retry for transient transport or remote-backend
failures. Before retrying, and once more after the final failure, the controller
reads the exact remote branch tip. It accepts a lost response only when the
remote tip is the expected commit. Authentication, authorization, policy and
non-fast-forward failures are not retried.

## CI observation and repair

```yaml
ci:
  enabled: true
  poll_interval_seconds: 30
  max_wait_seconds: 1800
  repair_attempts: 3
```

After `PR_CREATED`, the run enters `CI_RUNNING` and polls the pull request
checks. Polling is bounded by `max_wait_seconds`. It does not wait forever.

Each failing check is classified from its name and a log excerpt:

| Category | What happens |
| --- | --- |
| `CODE_FAILURE` | Bounded code repair. |
| `TEST_FAILURE` | Bounded code repair. |
| `FLAKY_TEST` | Escalates to `NEEDS_HUMAN` with evidence. |
| `INFRA_FAILURE` | Escalates to `NEEDS_HUMAN` with evidence. |
| `DEPENDENCY_FAILURE` | Escalates to `NEEDS_HUMAN` with evidence. |
| `UNKNOWN` | Escalates to `NEEDS_HUMAN` with evidence. |

Only the first two categories send the run back to `IMPLEMENTING`.
Other categories require human intervention.
The factory does not retry a flaky test until it passes, and it does not
guess at a broken runner.

Repair is bounded by `ci.repair_attempts`, a budget separate from the
implementation retry budget. Each repair attempt receives a small explicit
repair context: the normalized CI evidence, not the whole run history.

An unrecognized `gh` check status is treated conservatively as still pending
rather than as a pass.

GitHub can briefly report that no checks exist before Actions registers the
workflow runs. The factory treats that response as pending too. The normal
`ci.max_wait_seconds` bound still applies, so missing checks cannot wait
forever.

## Automatic merging

Edit these sections in a complete copy of the example configuration:

```yaml
pull_request:
  enabled: true
  remote: "origin"
  base_branch: "main"
  draft: false
  allowed_hosts: ["github.com"]
ci:
  enabled: true
  poll_interval_seconds: 30
  max_wait_seconds: 1800
  repair_attempts: 3
merge:
  enabled: true
  method: "squash"
  allowed_repositories: ["acme/example"]
  required_checks: ["quality", "tests"]
```

Use actual check names for your repository, and configure nonempty
`repository.commands.verify`. The controller requires current, successful
checks, the reviewed head, the allowlisted repository and target, and GitHub
merge eligibility. The active GitHub protection policy of the target must
also enforce required checks. The factory never creates or weakens those
rules. It supplies an expected-head guard to a synchronous merge request.
The factory confirms the actual merged commit before reporting `DONE`.
It does not use administrator overrides or implicitly enqueue a merge.

The factory stops delivery for missing or skipped checks, stale heads, branch
protection, merge conflicts, or an outdated base.
It does not merge uncertain work.
Bounded CI repair handles code and test failures before this boundary.
Project mode serializes task delivery and refreshes the target between tasks.
It does not add a merge-queue service. Read [Projects](projects.md).

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

1. Fetch open issues in `--github-repo` with `required_label` (`agent-ready`).
   Pull requests are excluded.
2. Reconcile persisted runs before dispatching new work.
3. Reserve a work item before dispatch to prevent duplicate pickup.
4. Dispatch tasks through a thread pool bounded by `max_concurrent_tasks`.

Issue labels can specify priority (numeric `p0` labels and named
priorities are recognized). The highest priority on an issue wins,
and higher priority is dispatched first.

Work items get a stable identifier in the format `tracker-owner/repo#12`.
A manual `factory run --work-item-id` that uses this identifier will not duplicate
scheduler work.

### Two independent bounds

| Setting | Bounds |
| --- | --- |
| `max_concurrent_tasks` | How many tasks run at once. Validated to be `1` or `2`. |
| `max_runs_per_day` | How many runs can be claimed per UTC calendar day. Default `20`, `null` to disable. |

The factory reports both values at startup. The daily ceiling is calculated
from persisted run timestamps. It survives a process restart. A tick stopped
by this ceiling reports `rate_limited` instead of an empty backlog.

The daily limit exists because `scheduler.enabled` and `--runtime copilot`
are independent settings. A daemon running with the real runtime can spend
money quickly if the backlog is large.

### Recovery

The controller transitions a non-terminal run from a dead process to
`NEEDS_HUMAN`. The factory never resumes this run automatically. No paid retry
is spent. The persisted budget remains untouched. The workspace and artifacts
remain on disk for inspection.

### The fake runtime is a real dry run

`--runtime fake` persists completed runs. The scheduler will not dispatch
those backlog items again. Before you poll real `agent-ready` issues, switch
to `--runtime copilot` or use a separate `--data-dir` for fake-runtime tests.

## Next

- [Monitor and run continuously](operations.md)
- [Safety and trust boundaries](../reference/safety.md)
