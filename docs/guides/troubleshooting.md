# Troubleshooting

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command succeeded. For `run`, the run finished successfully. |
| `1` | The run ended in `NEEDS_HUMAN` or `FAILED`, or the command failed. |
| `2` | Configuration error or a missing prerequisite. The factory refused to start. |

Exit code `2` is always a refusal before work starts, printed as one explicit
line. You should never see a Python traceback for it.

## A command refuses to start

**`git` not found.** Install Git and make sure it is on `PATH`. Run
`factory doctor`.

**`gh` not found.** You enabled `pull_request.enabled`, `ci.enabled` or
`scheduler.enabled`. Either install and authenticate `gh`, or set those back to
`false`.

**`copilot` not found.** You passed `--runtime copilot`. Install and
authenticate the Copilot CLI, or drop the flag to use the fake runtime.

**`factory start` refuses.** `scheduler.enabled` is `false` in the config you
passed. It is `false` in the packaged default. Set it to `true` in your own
config file and pass it with `--config`.

**`ci.enabled requires pull_request.enabled`.** Enable pull requests too, or
turn CI observation off. There is nothing for CI to watch without a PR.

**`reviewer model family must differ from all worker model families`.** The
final review must come from a different model family than the code. Change
`models.reviewer.model` or the worker models. See
[Configuration](../reference/configuration.md#models).

**Unknown configuration key.** The loader is strict and rejects extra keys.
Check for a typo against
[the configuration reference](../reference/configuration.md).

## A run ends in NEEDS_HUMAN

`NEEDS_HUMAN` is a business decision, not a crash. Common causes:

- Triage assigned a risk level whose `human_approval` is `true` (`R2` and `R3`
  by default).
- Scope drift found a sensitive change — dependency, migration, CI or
  infrastructure — on an `R2`/`R3` run.
- The implementation retry budget ran out.
- CI failed in a way that is not repairable: flaky, infra, dependency or
  unknown.
- The scheduler found a non-terminal run left behind by a dead process.

Look at the run:

```bash
factory show RUN_ID
```

`run.json` records the reason. The workspace and every artifact stay on disk.

## A run ends in FAILED

`FAILED` is operational: an agent or infrastructure failure, not a judgement.
Typical causes are an agent timeout (`factory.agent_timeout_seconds`, 900s by
default), a `copilot` process that returned output the runtime could not
validate as a typed artifact, or a Git or filesystem error.

Check `<data_dir>/logs/factory.log` for the structured record of the failing
agent invocation.

## Verification keeps failing

**Your commands need a shell.** They do not get one. `a && b` is not a command;
it is two commands. Put each on its own line in `repository.commands`.

**A command needs an environment variable.** Only `PATH`, `HOME`, `LANG` and
`TERM` are provided by default. Add the name to `repository.env_passthrough`.

**A command times out.** Raise `repository.command_timeout_seconds`.

**Output is cut off.** Only `repository.log_capture_bytes` (32 KiB) is retained
per command. Raise it, or make the command less chatty.

## The change touched too much

**`excessive-file-count`.** The change exceeded the plan's estimate. The run
replans, up to `scope_drift.max_replans`. If this happens constantly, the work
item is probably too big — split it.

**`max_changed_files` exceeded.** A hard ceiling, not a replan. Either the work
is too large or the implementer went wrong.

**A protected file changed.** The publish gate blocks it. This is working as
intended. Check what the agent tried to touch in `patch.diff`.

## The dashboard will not load

The URL must include the token generated for that process. It is printed once at
startup and never written to the log. If you lost it, stop the server and start
it again.

The dashboard binds `127.0.0.1` only. It is not reachable from another machine,
and that is not configurable.

If the port is in use, run `factory dashboard --port 0` to let the OS pick one.

## The service is installed but nothing happens

```bash
factory service status --json
factory doctor --config ~/my-factory.yaml
tail -f ~/.software-factory/logs/factory.log
```

Check in order:

1. Does `scheduler.enabled` hold in the config the service loads?
2. Are there open issues with the `agent-ready` label?
3. Did you hit `scheduler.max_runs_per_day`? A rate-limited tick reports
   `rate_limited`, which is distinct from an empty backlog.
4. Were those issues already dispatched in an earlier fake-runtime run? The
   scheduler will not dispatch them twice. Use a separate `--data-dir` for
   fake-runtime testing.
5. launchd agents get a minimal environment. `service install` captures a `PATH`
   snapshot at install time; if you have since moved `git`, `gh` or `copilot`,
   reinstall the service.

## status says DEGRADED

The scan was truncated by `--max-scanned-runs`, or it hit an unreadable run. The
numbers are incomplete. Raise the cap, or find the broken run directory. Do not
read a `DEGRADED` report as "everything is fine".

## Still stuck

Open an issue at
[github.com/sanjit-roopra/software-agent-factory/issues](https://github.com/sanjit-roopra/software-agent-factory/issues).
Include the output of `factory doctor --json` and `factory show RUN_ID`. Redact
anything private first — `show` prints your work item text.
