# Configure a repository

Out of the box the factory runs no checks against your project. The
`repository.commands` lists are empty in the packaged configuration, so
verification has nothing deterministic to assert. This is the first thing to
change.

## Copy the example config

```bash
cp config/factory.example.yaml ~/my-factory.yaml
```

`config/factory.example.yaml` mirrors the packaged default exactly. Pass your
copy explicitly:

```bash
factory run --config ~/my-factory.yaml ...
```

The loader is strict: an unknown key is rejected, not ignored. A bad config
fails with an explicit message and exit code `2`.

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

They run in order: `install`, then `verify`, then `build`. A failure in any
phase stops the run's verification and sends the work back to the implementer,
within the retry budget.

Three properties matter:

- **No shell.** Each command is split into an argument list and executed
  directly. `&&`, pipes, globs and shell profile lookups do not work. Put each
  step on its own line.
- **No inherited environment.** Commands get `PATH`, `HOME`, `LANG` and `TERM`,
  plus whatever you list in `env_passthrough`. Credentials such as `GH_TOKEN` or
  `AWS_*` are never passed implicitly.
- **Bounded, redacted output.** At most `log_capture_bytes` (32 KiB by default)
  of stdout and stderr are retained per command, after credential redaction, and
  written to a durable per-command log under the run directory.

To let a command read an extra variable:

```yaml
repository:
  env_passthrough:
    - "CARGO_HOME"
    - "npm_config_cache"
```

Only names are allowed, not values. The factory reads them from your
environment; it does not store them.

## Verify the commands exist

```bash
factory doctor --config ~/my-factory.yaml
```

`doctor` resolves the executable behind each configured command and reports it.
A typo shows up here rather than three minutes into a run.

## Deterministic gates

Verification classifies a failure rather than just reporting a nonzero exit:
lint, type check, test, dependency and build failures are distinguished, and the
category is persisted in `verification.json`.

The tester and reviewer only run after deterministic verification succeeds. A
broken build cannot reach them, and a model cannot talk its way past a failing
test.

## Changed-file limits

```yaml
repository:
  max_changed_files: 100
  branch_prefix: "factory/"
```

`max_changed_files` is a hard ceiling on how many files one change may touch.

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

These are glob patterns matched against repository-relative changed paths. A
change that touches one of them is blocked at the publish gate. The defaults
cover dotenv files, private keys, `.npmrc`, `.netrc`, `.pypirc`,
`.git-credentials`, credential and secret JSON/YAML files, and the `.aws` and
`.ssh` directories.

Add your own patterns; do not remove the defaults unless you have a specific
reason.

## Scope drift

After verification passes, the factory compares what actually changed with what
the plan said it would change. This is deterministic — it reads the Git diff,
not the agent's summary.

It flags:

| Finding | Trigger |
| --- | --- |
| `unexpected-module` | Files outside the plan's expected top-level modules. |
| `excessive-file-count` | More files than the plan's estimated maximum. |
| `dependency-change` | A dependency manifest or lockfile changed. |
| `migration-change` | A path under a migrations directory changed. |
| `ci-change` | A CI workflow file changed. |
| `infrastructure-change` | An infrastructure file changed. |

The decision:

- No findings: continue.
- Findings, and risk is `R0` or `R1`: replan, up to `scope_drift.max_replans`
  (default `1`).
- Sensitive findings (dependency, migration, CI, infrastructure), and risk is
  `R2` or `R3`: escalate to `NEEDS_HUMAN`.

The check runs again at the pull request boundary, so a later attempt cannot
sneak a widened change past it.

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

With `human_approval: true`, the run stops at `NEEDS_HUMAN` for a person to
decide instead of proceeding automatically.

## Retry budgets

```yaml
factory:
  agent_timeout_seconds: 900
  retries:
    same_model_attempts: 2
    max_total_attempts: 6
```

`same_model_attempts` is how many times the same model is retried before
escalating to a stronger one. `max_total_attempts` is the hard ceiling for the
run. The budget is persisted on the run, so restarting the process does not hand
a run a fresh budget. There is no unbounded retry anywhere.

CI repair has its own separate budget, `ci.repair_attempts`.

## Next

- [Configuration reference](../reference/configuration.md) for every key.
- [GitHub backlog, PRs and CI](github.md) to turn on the integrations.
