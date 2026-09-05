# Configuration reference

The factory loads one YAML file. Without `--config` it uses the packaged
default, which is byte-identical to `config/factory.example.yaml` in the
repository.

```bash
cp config/factory.example.yaml ~/my-factory.yaml
factory run --config ~/my-factory.yaml ...
```

The loader is strict. An unknown key is rejected rather than silently ignored,
and an invalid file fails with an explicit message and exit code `2`.

## factory

```yaml
factory:
  data_dir: "~/.software-factory"
  agent_timeout_seconds: 900
  retries:
    same_model_attempts: 2
    max_total_attempts: 6
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `data_dir` | path | `~/.software-factory` | Where runs, workspaces, locks and logs live. `~` is expanded. |
| `agent_timeout_seconds` | int > 0 | `900` | Per-agent-invocation timeout. |
| `retries.same_model_attempts` | int > 0 | `2` | Attempts with the same model before escalating to a stronger one. |
| `retries.max_total_attempts` | int > 0 | `6` | Hard ceiling on implementation attempts per run. Must be at least `same_model_attempts`. |

The retry budget is persisted on the run. Restarting the process does not grant
a run a fresh budget.

## models

```yaml
models:
  triage:     { model: "claude-sonnet-5",     reasoning: "medium" }
  refiner:    { model: "claude-opus-5",       reasoning: "high" }
  researcher: { model: "gpt-5.6-sol",         reasoning: "high" }
  planner:    { model: "claude-opus-5",       reasoning: "high" }
  workers:
    L0:       { model: "mai-code-1.1-flash",  reasoning: "medium" }
    L1:       { model: "claude-sonnet-5",     reasoning: "medium" }
    L2:       { model: "claude-opus-5",       reasoning: "high" }
    L3:       { model: "claude-opus-5",       reasoning: "high" }
  tester:     { model: "claude-sonnet-5",     reasoning: "high" }
  reviewer:   { model: "gpt-5.6-sol",         reasoning: "high" }
```

Every role takes a `model` name and a `reasoning` level. Both are non-empty
strings passed through to the Copilot CLI; the factory does not maintain a
whitelist of model names.

`workers` must define exactly `L0`, `L1`, `L2` and `L3`. Triage assigns the
complexity level and that selects the worker.

!!! note "The reviewer must come from a different model family"

    Configuration is rejected if `models.reviewer`'s model family matches any
    worker's. Independent review from the same family as the implementer is not
    independent enough to be a gate.

Model names appear only in configuration. They are not scattered through the
source.

## repository

```yaml
repository:
  branch_prefix: "factory/"
  command_timeout_seconds: 900
  commands:
    install: []
    verify: []
    build: []
  env_passthrough: []
  log_capture_bytes: 32768
  max_changed_files: 100
  protected_file_patterns: [...]
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `branch_prefix` | string | `factory/` | Required prefix for factory branches. Enforced before any push. |
| `command_timeout_seconds` | int > 0 | `900` | Timeout per repository command. |
| `commands.install` | list of strings | `[]` | Dependency installation, run first. |
| `commands.verify` | list of strings | `[]` | Lint, types, tests. Run second. |
| `commands.build` | list of strings | `[]` | Build. Run last. |
| `env_passthrough` | list of env var names | `[]` | Extra variables repository commands may read. |
| `log_capture_bytes` | int > 0 | `32768` | Max stdout/stderr bytes retained per command, after redaction. |
| `max_changed_files` | int > 0 | `100` | Hard ceiling on changed files in one change. |
| `protected_file_patterns` | list of globs | see below | Paths a change may never touch. |

Commands are argument lists executed directly. There is no shell, so `&&`,
pipes, globs and shell profile lookups do not work. Split each step onto its own
line.

Commands never inherit your environment. They get `PATH`, `HOME`, `LANG` and
`TERM`, plus the names in `env_passthrough`. Credentials such as `GH_TOKEN` and
`AWS_*` are never passed implicitly.

`env_passthrough` accepts variable *names* only. Values are read from your
environment at run time and are not stored.

### protected_file_patterns

Default:

```yaml
protected_file_patterns:
  - ".env"
  - ".env.*"
  - "**/.env"
  - "**/.env.*"
  - "**/*.pem"
  - "**/*.key"
  - "**/*.p12"
  - "**/*.pfx"
  - "**/id_rsa"
  - "**/id_ed25519"
  - "**/.npmrc"
  - "**/.netrc"
  - "**/.pypirc"
  - "**/.git-credentials"
  - "**/credentials.json"
  - "**/secrets.json"
  - "**/secrets.yaml"
  - "**/secrets.yml"
  - "**/.aws/**"
  - "**/.ssh/**"
```

Glob patterns matched against repository-relative changed paths. Setting this
key replaces the default list, so include the defaults you still want.

## scope_drift

```yaml
scope_drift:
  max_replans: 1
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_replans` | int >= 0 | `1` | How many times scope drift may send a run back to planning. |

See [Configure a repository](../guides/configure-repository.md#scope-drift) for
the finding categories and decisions.

## polish

```yaml
polish:
  enabled: true
  official_documentation_origins:
    - "https://docs.pytest.org"
    - "https://docs.python.org"
    - "https://nodejs.org"
    - "https://packaging.python.org"
    - "https://react.dev"
    - "https://testing-library.com"
    - "https://vite.dev"
    - "https://vitest.dev"
    - "https://www.typescriptlang.org"
  practice_reference_urls:
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/a11y-review.md"
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/component-architecture-review.md"
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/js-fp-review.md"
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/quality-reviewer.md"
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/react-reactivity-review.md"
    - "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/52cc5efd1c445e71c55b956837c003911346d7e7/plugins/dev-team/agents/refactor-opportunity-review.md"
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `true` in packaged default/example; `false` when omitted | Run at most one post-green Implementer polish attempt, informed by a version-specific `RepositorySkill`. |
| `official_documentation_origins` | list of HTTPS origins | the nine official documentation origins shown above | Authoritative sources for version-specific claims. Non-empty, unique, at most 25 entries. Each must be an HTTPS origin with no path, credentials, whitespace, query, fragment or trailing slash. |
| `practice_reference_urls` | list of exact HTTPS URLs | the six curated `bdfinst/agentic-dev-team` review references shown above, pinned to commit `52cc5efd` | Optional curated general-practice references. May be empty; unique, at most 12 entries. Each must be an exact HTTPS document URL (a real path, no trailing slash) with no credentials, whitespace, query or fragment. |

Both lists are the complete fetch allowlist for the skill-generation
Researcher. The curated practice references are pinned to an immutable commit
(`52cc5efd1c445e71c55b956837c003911346d7e7`) rather than a mutable branch, so
the exact reviewed text is what gets fetched; re-pin deliberately after
reviewing a newer revision. Official documentation, migration guides and release notes are
authoritative. Curated practice references may only contribute generic quality
heuristics, synthesized rather than copied; they never supply version claims,
commands, tools or orchestration, and the controller validates them by exact
URL rather than by origin.

The class fallback for `enabled` is `false`, so legacy configurations that omit
`polish` retain their previous one-pass behavior. The packaged default and
`config/factory.example.yaml` explicitly enable it. Omitting only the URL lists
keeps the defaults above.

Polish runs only after the first successful deterministic verification and
scope assessment, before testing and review. When eligible, the controller
re-profiles the post-implementation worktree — capturing any dependency change
the task made — and transitions through a temporary `RESEARCHING` state to
call the configured Researcher (`GPT-5.6 Sol` by default) with purpose
`GENERATE_REPOSITORY_SKILL`, at most once per run. That call runs in the run's
own directory rather than the worktree, receives only the normalized
`RepositoryProfile` and the controller-derived changed file paths — never source
code, README content, task prose or the diff — has `web_fetch` as its only
tool, and runs without repository custom instructions.

The researcher returns one `RepositorySkill` bound to the profile's
`dependency_fingerprint`, with bounded targets, HTTPS source provenance and
separate simplify and polish guidance. The controller rejects it when the
fingerprint does not match, a target or its evidence path is not in the
profile, a detected `python`/`pytest`/`react`/`react-dom`/`vite`/`vitest`
dependency is missing a target or is not named by an accepted official source,
a source claims applicability to an undetected dependency, or a source falls
outside the two configured lists.

Nothing here can fail an already-green run. A failed re-profile, failed
research, rejected skill, or a skill that became stale (the
`dependency_fingerprint` changed after generation) records a warning on the
persisted `repository-profile.json` and safely skips or disables polish.

When the skill is accepted it is applied — simplify first, then
version-specific polish — by one existing bounded worker attempt, which records
`AttemptTrigger.POLISH`, consumes the implementation budget, may make no edits
and is always fully verified and scope-assessed again. Polish never runs during
CI repair and runs only when one later recovery attempt would still remain. The
skill is generated fresh for every eligible run; there is no cross-run cache.

## pull_request

```yaml
pull_request:
  enabled: false
  remote: "origin"
  base_branch: null
  draft: true
  allowed_hosts:
    - "github.com"
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether to commit, push and open a pull request. |
| `remote` | string | `origin` | Git remote to push to. |
| `base_branch` | string or null | `null` | Base branch. `null` means the remote's default branch. |
| `draft` | bool | `true` | Open the PR as a draft. |
| `allowed_hosts` | list of hosts | `["github.com"]` | The remote's host must be in this list. |

Requires `gh` on `PATH`. Never force-pushes. Never merges.

## ci

```yaml
ci:
  enabled: false
  poll_interval_seconds: 30
  max_wait_seconds: 1800
  repair_attempts: 3
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Poll the pull request's checks after creation. |
| `poll_interval_seconds` | int > 0 | `30` | Delay between polls. |
| `max_wait_seconds` | int > 0 | `1800` | Total polling budget. Not unbounded. |
| `repair_attempts` | int > 0 | `3` | CI repair budget, separate from the implementation budget. |

`ci.enabled: true` requires `pull_request.enabled: true`. The combination is
rejected otherwise. Requires `gh`.

## scheduler

```yaml
scheduler:
  enabled: false
  poll_interval_seconds: 30
  max_concurrent_tasks: 1
  stall_timeout_seconds: 900
  required_label: "agent-ready"
  max_runs_per_day: 20
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether `factory start` may run at all. |
| `poll_interval_seconds` | int > 0 | `30` | Backlog poll interval. |
| `max_concurrent_tasks` | `1` or `2` | `1` | Concurrent runs. Validated; higher values are rejected. |
| `stall_timeout_seconds` | int > 0 | `900` | Idle time before a run is treated as stalled. Also the default staleness threshold for `factory status`. |
| `required_label` | string | `agent-ready` | Issues must carry this label to be dispatched. |
| `max_runs_per_day` | int > 0 or null | `20` | Runs that may be claimed per UTC calendar day. `null` disables the cap. |

`max_runs_per_day` is counted from persisted run timestamps, so it survives a
restart. It is a cost bound: `scheduler.enabled` and `--runtime copilot` are
independent knobs, and a daemon with the real runtime would otherwise spend at
whatever rate the backlog allows.

Requires `gh`.

## risk

```yaml
risk:
  R0: { human_approval: false }
  R1: { human_approval: false }
  R2: { human_approval: true }
  R3: { human_approval: true }
```

All four levels must be defined. `human_approval: true` stops a run of that risk
level at `NEEDS_HUMAN` instead of proceeding automatically.

Risk selects governance. Complexity selects model strength. They are separate:
a one-line change can be `R3`, and a hard change can be `R0`.

## Cross-field validation

The loader rejects a configuration when:

- `retries.max_total_attempts` is less than `retries.same_model_attempts`
- `models.workers` does not define exactly `L0`, `L1`, `L2` and `L3`
- `models.reviewer`'s model family matches any worker's model family
- `risk` does not define exactly `R0`, `R1`, `R2` and `R3`
- `ci.enabled` is true while `pull_request.enabled` is false
- `scheduler.max_concurrent_tasks` is greater than `2`
- any key is not recognized
