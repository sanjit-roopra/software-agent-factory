# Troubleshooting

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command succeeded. For `run`, the run finished successfully. |
| `1` | The run ended in `NEEDS_HUMAN` or `FAILED`, or the command failed. |
| `2` | Configuration error or a missing prerequisite. The factory refused to start. |

Exit code `2` is always a refusal before work starts, printed as one explicit
line. The CLI never shows a Python traceback for it.

## A command refuses to start

### git not found

Install Git and make sure that it is on `PATH`. Run `factory doctor`.

### gh not found

You enabled `pull_request.enabled`, `ci.enabled`, or `scheduler.enabled`.
Install and authenticate `gh`, or set those options to `false`.

### copilot not found

You passed `--runtime copilot`.
Install and authenticate the Copilot CLI, or omit the flag to use the fake runtime.

### factory start refuses

`scheduler.enabled` is `false` in the loaded configuration.
Set `scheduler.enabled` to `true` in your configuration file and pass it with `--config`.

### ci.enabled requires pull_request.enabled

Enable pull requests too, or turn CI observation off.
There is nothing for CI to monitor without a pull request.

### reviewer model family must differ from all worker model families

The final review must come from a different model family than the code.
Change `models.reviewer.model` or the worker models.
Read [Configuration](../reference/configuration.md#models).

### Unknown configuration key

The loader is strict and rejects unknown keys.
Examine the configuration for typos against
[the configuration reference](../reference/configuration.md).

## A run ends in NEEDS_HUMAN

`NEEDS_HUMAN` is a business decision, not a crash. Common causes:

- Triage assigned a risk level that requires human approval (`R2` and `R3` by default).
- Scope drift found a sensitive change (dependency, migration, CI, or infrastructure)
  on an `R2` or `R3` run.
- The implementation retry budget ran out.
- Independent review did not converge after repeated repairs.
- CI failed in an unrepairable category: flaky test, infrastructure, dependency, or unknown.
- The scheduler found an incomplete run left behind by a stopped process.

Look at the run:

```bash
factory show RUN_ID
```

The dashboard shows a bounded summary of what happened, the next action,
and the relevant artifact name. Raw failure text, logs, and diffs are not sent
to the browser. `run.json` records the full reason. The workspace and every
artifact stay on disk.

For a review convergence stop, inspect:

```text
<data_dir>/runs/RUN_ID/review-impasse.json
```

It records the blocking paths and finding identifiers.
Examine the matching per-attempt `review.json`, `patch.diff`, and reviewed tree identifiers.
They show whether a defect remained or the repair introduced a regression.
They also show whether later reviews replaced the target.

An eligible low-risk run can instead continue as accepted with findings.
The dashboard and pull request identify that decision. Inspect
`review-acceptance.json` for the exact reviewed tree, review count, and accepted
typed findings. It is a controller decision, not Reviewer approval.

## A run ends in FAILED

`FAILED` is operational: an agent or infrastructure failure, not a judgement.
Typical causes include an agent timeout (`factory.agent_timeout_seconds`, 900s by default).
Other causes include a `copilot` process that returned invalid output, or a Git error.

Check `<data_dir>/logs/factory.log` for the structured record of the failing
agent invocation.

## Verification keeps failing

### Your commands need a shell

Shell execution is not provided. `a && b` is not one command.
It is two commands. Put each step on its own line in `repository.commands`.

### A command needs an environment variable

The factory provides only `PATH`, `HOME`, `LANG`, and `TERM` by default.
Add variable names to `repository.env_passthrough`.

### A command times out

Increase `repository.command_timeout_seconds`.

### Output is cut off

The factory retains at most `repository.log_capture_bytes` (32 KiB) per command.
Increase this value, or make the command output shorter.

### Verification modifies files

The controller compares the Git tree before and after verification.
If a command creates, stages, or rewrites files, the run returns to implementation
before review. Remove generated artifacts from the worktree and index, add ignore
rules, or configure commands to write outside the repository. Put intentional
generated source into the implementation before verification starts.

## PR publication or CI stops

### A transient push failed

The factory retries once. It checks whether the expected commit reached the remote
despite a lost response. Authentication, authorization, policy, and non-fast-forward
errors stop immediately.

### CI says no checks are reported

GitHub Actions can take time to register checks. The factory treats this response
as pending. The run keeps polling until checks appear or `ci.max_wait_seconds`
expires.

### The pull request identity does not match

Check the authenticated `gh` host and account, the configured repository,
and the pull request URL. The factory does not observe or merge a pull request
from another repository or host.

## The change touched too much

### The plan underestimated the file count

Plan estimates are advisory and do not stop the run. Review the diff and
independent review evidence to see if the task remained coherent.

### max_changed_files exceeded

This is a hard limit, not a replan. The change is too large, or the implementer
made errors.

### A protected file changed

The publish gate blocks the change. Examine what the agent edited in `patch.diff`.

## The dashboard will not load

The URL must include the token generated for that process. It is printed once at
startup and never written to the log. If you lost it, stop the server and start
it again.

The dashboard binds `127.0.0.1` only. It is not reachable from another machine.
That setting is not configurable.

If the port is in use, run `factory dashboard --port 0` to let the operating
system choose a port.

## The service is installed but nothing happens

```bash
factory service status --json
factory doctor --config ~/my-factory.yaml
tail -f ~/.software-factory/logs/factory.log
```

Check in order:

1. Does the configuration for the service enable `scheduler.enabled`?
2. Are there open issues with the `agent-ready` label?
3. Did you reach `scheduler.max_runs_per_day`? A rate-limited tick reports
   `rate_limited`, which differs from an empty backlog.
4. Did an earlier fake-runtime run dispatch those issues? The scheduler will
   not dispatch them twice. Use a separate `--data-dir` for fake-runtime tests.
5. LaunchAgent processes get a minimal environment. `service install` captures
   a `PATH` snapshot during installation. If you moved `git`, `gh`, or `copilot`
   after installation, install the service again.

## status says DEGRADED

The scan was truncated by `--max-scanned-runs`, or it found an unreadable run.
The reported numbers are incomplete. Increase the limit, or fix the broken run
directory. Do not read a `DEGRADED` report as complete.

## Still stuck

Open an issue at
[github.com/sanjit-roopra/software-agent-factory/issues](https://github.com/sanjit-roopra/software-agent-factory/issues).
Include the output of `factory doctor --json` and `factory show RUN_ID`.
Redact private information before you post.
The `show` command prints your work item text.
