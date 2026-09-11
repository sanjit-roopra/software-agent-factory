# Configure a repository

By default, the factory runs no checks against your project. The
`repository.commands` lists are empty in the packaged configuration.
Verification has no deterministic commands to run. Configure these commands
first.

## Copy the example config

```bash
cp config/factory.example.yaml ~/my-factory.yaml
```

`config/factory.example.yaml` mirrors the packaged default configuration. Pass your
copy explicitly:

```bash
factory run --config ~/my-factory.yaml ...
```

The loader is strict. It rejects unknown configuration keys.
An invalid configuration fails with an explicit message and exit code `2`.

## Set the three command phases

```yaml
repository:
  command_timeout_seconds: 900
  commands:
    install:
      - "uv sync --locked"
    verify:
      - "uv run --no-sync ruff check ."
      - "uv run --no-sync mypy src"
      - "uv run --no-sync pytest -q"
    build:
      - "uv build"
```

Commands run in order: `install`, `verify`, and `build`.
A failure in a phase stops verification.
The factory returns the work to the implementer if retry budget remains.

Three properties matter:

- No shell: Each command is split into arguments and executed directly.
  Operators such as `&&`, pipes, and globs do not work. Put each step on its own line.
- No inherited environment: Commands receive `PATH`, `HOME`, `LANG`, and `TERM`,
  plus variables in `env_passthrough`. The factory never passes credentials implicitly.
- Bounded output: The factory captures at most `log_capture_bytes` (32 KiB by default)
  of output per command. It redacts credentials and writes output to a durable log file.

To let a command read an extra variable:

```yaml
repository:
  env_passthrough:
    - "CARGO_HOME"
    - "npm_config_cache"
```

Only names are allowed, not values. The factory reads them from your environment.
It does not store them.

## Make sure that commands exist

```bash
factory doctor --config ~/my-factory.yaml
```

`doctor` resolves the executable behind each configured command and reports it.
A typo appears here before you start a run.

## Deterministic gates

Verification classifies a failure instead of reporting only a nonzero exit.
It distinguishes lint, type check, test, dependency, and build failures.
The category is saved in `verification.json`.

The tester and reviewer run only after deterministic verification succeeds.
A broken build cannot reach them. A model cannot bypass a failing test.

## Changed-file limits

```yaml
repository:
  max_changed_files: 100
  branch_prefix: "factory/"
```

`max_changed_files` is a hard limit on how many files one change can touch.

## Protected files

```yaml
repository:
  protected_file_patterns:
    - ".env"
    - "**/*.pem"
    - "**/*.key"
    - "**/id_rsa"
    - "**/.aws/**"
    - "**/.ssh/**"
    # ... see the example config for the full default list
```

These glob patterns match repository-relative changed paths.
A change that touches a protected pattern is blocked at the publish gate.
Defaults cover dotenv files, private keys, `.npmrc`, `.netrc`, `.pypirc`,
`.git-credentials`, credential JSON and YAML files, and `.aws` and `.ssh` directories.

Add your own patterns. Do not remove default patterns without a specific reason.

## Scope drift

After verification passes, the factory compares actual changes with planned changes.
This check is deterministic. It reads the Git diff, not the summary from the agent.

It flags:

| Finding | Trigger |
| --- | --- |
| `unexpected-module` | Files outside the plan's expected top-level modules. |
| `dependency-change` | A dependency manifest or lockfile changed. |
| `migration-change` | A path under a migrations directory changed. |
| `ci-change` | A CI workflow file changed. |
| `infrastructure-change` | An infrastructure file changed. |

The decision:

- No findings: continue.
- Non-sensitive findings: replan, up to `scope_drift.max_replans` (default `1`).
- Sensitive findings (dependency, migration, CI, infrastructure): escalate to `NEEDS_HUMAN`.

The estimated file range in the plan is advisory.
The `repository.max_changed_files` setting is the hard limit from the controller.

The check runs again at the pull request boundary.
A later attempt cannot bypass scope limits.

```yaml
scope_drift:
  max_replans: 1
```

## Risk and approval

Triage assigns a risk level. Risk selects governance, separately from
complexity, which selects model strength. A trivial change can be high risk.

```yaml
risk:
  R0: { human_approval: false }
  R1: { human_approval: false }
  R2: { human_approval: true }
  R3: { human_approval: true }
```

With `human_approval: true`, the run stops at `NEEDS_HUMAN` for human review.

## Retry budgets

```yaml
factory:
  agent_timeout_seconds: 900
  retries:
    same_model_attempts: 2
    max_total_attempts: 6
```

`same_model_attempts` sets the number of same-model implementation attempts
before escalation. It also bounds typed-output correction for supported roles.
`max_total_attempts` is the hard ceiling for implementation attempts in the run.
The factory persists this budget. Restarting the process does not grant a fresh budget.
There is no unbounded retry anywhere.

CI repair has its own separate budget, `ci.repair_attempts`.

## Next

- [Configuration reference](../reference/configuration.md) for every key.
- [GitHub backlog, PRs and CI](github.md) to turn on the integrations.
