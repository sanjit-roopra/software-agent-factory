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
| `data_dir` | path | `~/.software-factory` | Where runs, workspaces, locks, logs and reusable repository guidance live. `~` is expanded. |
| `agent_timeout_seconds` | int > 0 | `900` | Per-agent-invocation timeout. |
| `retries.same_model_attempts` | int > 0 | `2` | Per-stage same-model attempt limit for implementation routing and supported typed-output correction. |
| `retries.max_total_attempts` | int > 0 | `6` | Hard ceiling on implementation attempts per run. Must be at least `same_model_attempts`. |

The retry budget is persisted on the run. Restarting the process does not grant
a run a fresh budget.

The writing policy is mandatory and has no configuration switch. See
[Writing policy](writing-policy.md).

## models

```yaml
models:
  triage:     { model: "gpt-5.6-terra",        reasoning: "medium", context_tier: "default" }
  refiner:    { model: "gpt-5.5",              reasoning: "high",   context_tier: "default" }
  researcher: { model: "claude-opus-5",        reasoning: "high",   context_tier: "default" }
  planner:    { model: "claude-opus-5",        reasoning: "high",   context_tier: "default" }
  workers:
    L0:       { model: "mai-code-1.1-flash",   reasoning: "medium", context_tier: "default" }
    L1:       { model: "gemini-3.8-flash",     reasoning: "high",   context_tier: "default" }
    L2:       { model: "claude-sonnet-5",      reasoning: "high",   context_tier: "default" }
    L3:       { model: "claude-opus-5",        reasoning: "high",   context_tier: "default" }
  tester:     { model: "gemini-3.8-flash",     reasoning: "high",   context_tier: "default" }
  reviewer:   { model: "gpt-5.6-sol",          reasoning: "high",   context_tier: "default" }

model_profiles:
  economy:
    triage:     { model: "gpt-5.6-luna",       reasoning: "medium", context_tier: "default" }
    refiner:    { model: "gpt-5.6-terra",      reasoning: "high",   context_tier: "default" }
    researcher: { model: "gemini-3.8-flash",   reasoning: "medium", context_tier: "default" }
    planner:    { model: "gpt-5.6-terra",      reasoning: "high",   context_tier: "default" }
    workers:
      L0:       { model: "mai-code-1.1-flash", reasoning: "medium", context_tier: "default" }
      L1:       { model: "gemini-3.8-flash",   reasoning: "high",   context_tier: "default" }
      L2:       { model: "gemini-3.8-flash",   reasoning: "high",   context_tier: "default" }
      L3:       { model: "gemini-3.8-flash",   reasoning: "high",   context_tier: "default" }
    tester:     { model: "gemini-3.8-flash",   reasoning: "high",   context_tier: "default" }
    reviewer:   { model: "gpt-5.6-sol",        reasoning: "high",   context_tier: "default" }
  security:
    triage:     { model: "gpt-5.6-terra",      reasoning: "medium", context_tier: "default" }
    refiner:    { model: "gpt-5.5",            reasoning: "high",   context_tier: "default" }
    researcher: { model: "claude-opus-5",      reasoning: "high",   context_tier: "default" }
    planner:    { model: "claude-opus-5",      reasoning: "high",   context_tier: "default" }
    workers:
      L0:       { model: "mai-code-1.1-flash", reasoning: "medium", context_tier: "default" }
      L1:       { model: "gemini-3.8-flash",   reasoning: "high",   context_tier: "default" }
      L2:       { model: "claude-sonnet-5",    reasoning: "high",   context_tier: "default" }
      L3:       { model: "claude-opus-5",      reasoning: "high",   context_tier: "default" }
    tester:     { model: "gpt-6-astra",        reasoning: "high",   context_tier: "default" }
    reviewer:   { model: "gpt-5.6-sol",        reasoning: "high",   context_tier: "default" }
```

Every role takes a `model`, `reasoning` level and `context_tier`. The context
tier is `default` or `long_context`. The runtime always passes it explicitly
to Copilot, so a persisted interactive CLI setting cannot change a factory
run. Model names and reasoning levels are passed through without a catalog
whitelist, so unsupported combinations fail when Copilot executes.

The top-level `models` block is the `default` profile. Additional complete
profiles live under `model_profiles`. Select one on any agent-invoking command:

```bash
factory run ... --model-profile economy
factory project ... --model-profile economy
factory start ... --model-profile economy
factory skill refresh ... --model-profile economy
factory run ... --model-profile security
```

`factory doctor` validates the selected profile, and `factory service install`
stores the selected name in the LaunchAgent arguments. An unknown profile
fails with exit code `2` before a workspace or paid call is created. Profiles
are complete `models` blocks, not partial overlays. The `security` profile
uses Astra for adversarial testing and Sol for an independent final review.
It is intentionally much more expensive than `economy`.

`workers` must define exactly `L0`, `L1`, `L2` and `L3`. Triage assigns the
complexity level and that selects the worker.

!!! note "The reviewer must come from a different model family"

    Configuration is rejected if `models.reviewer`'s model family matches any
    worker's. Independent review from the same family as the implementer is not
    independent enough to be a gate.

Model names appear only in configuration. They are not scattered through the
source.

See [Model selection, cost and benchmarks](model-selection.md) for the current
Copilot catalog, prices, context and reasoning capabilities, benchmark
evidence, and role-specific tradeoffs.

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
| `env_passthrough` | list of env var names | `[]` | Extra variables repository commands can read. |
| `log_capture_bytes` | int > 0 | `32768` | Max stdout/stderr bytes retained per command, after redaction. |
| `max_changed_files` | int > 0 | `100` | Hard ceiling on changed files in one change. |
| `protected_file_patterns` | list of globs | see below | Paths a change can never touch. |

Commands are strings executed by a non-login `/bin/sh`. Shell operators such as
`&&`, pipes, redirects and globs work, but login profiles are not loaded.

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
  approved_sensitive_files: []
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `max_replans` | int >= 0 | `1` | How many times scope drift can send a run back to planning. |
| `approved_sensitive_files` | list of exact paths | `[]` | Human authorization for named dependency/CI files, effective only when the same exact files appear in the task plan's steps. No globs, absolute paths or traversal. |

For example, authorize `pyproject.toml`, `uv.lock`, and
`.github/workflows/ci.yml` to bootstrap a Python repository. This does not waive
risk approval, protected-file policy, file-count/module bounds or independent
review. Migration and infrastructure findings are never exempted by this list.

See [Configure a repository](../guides/configure-repository.md#scope-drift) for
the finding categories and decisions.

## review

```yaml
review:
  max_rounds: 3
  max_accepted_findings: 5
  accepted_risks: [R0, R1]
  blocked_categories: [SECURITY, SCOPE]
```

This is an absolute bound on review-driven repair. After three logical reviews,
the controller can continue low-risk work with a small number of correctness or
compatibility findings. If the remaining findings are not eligible, the run
stops in `NEEDS_HUMAN` at the same limit. The decision is stored separately
from the Reviewer's report and is bound to the exact reviewed tree. Security,
scope, repair-regression and high-risk findings still require a human.

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
| `enabled` | bool | `true` in packaged default/example, `false` when omitted | Run at most one post-green Implementer polish attempt, informed by the repository's `RepositorySkill` and any human overlay. |
| `official_documentation_origins` | list of HTTPS origins | the nine official documentation origins shown above | Authoritative sources for version-specific claims. Non-empty, unique, at most 25 entries. Each must be an HTTPS origin with no path, credentials, whitespace, query, fragment or trailing slash. |
| `practice_reference_urls` | list of exact HTTPS URLs | the six curated `bdfinst/agentic-dev-team` review references shown above, pinned to commit `52cc5efd` | Optional curated general-practice references. Can be empty. Unique, at most 12 entries. Each must be an exact HTTPS document URL (a real path, no trailing slash) with no credentials, whitespace, query or fragment. |

Both lists are the complete fetch allowlist for the skill-generation
Researcher. The curated practice references are pinned to an immutable commit
(`52cc5efd1c445e71c55b956837c003911346d7e7`) rather than a mutable branch, so
the exact reviewed text is what gets fetched. Re-pin deliberately after
reviewing a newer revision. Official documentation, migration guides and release notes are
authoritative. Curated practice references can only contribute generic quality
heuristics, synthesized rather than copied. They never supply version claims,
commands, tools or orchestration, and the controller validates them by exact
URL rather than by origin.

The class fallback for `enabled` is `false`, so legacy configurations that omit
`polish` retain their previous one-pass behavior. The packaged default and
`config/factory.example.yaml` explicitly enable it. Omitting only the URL lists
keeps the defaults above.

Polish runs only after the first successful deterministic verification and
scope assessment, before testing and review. When eligible, the controller
re-profiles the post-implementation worktree to capture any dependency change
the task made. It loads the generated `RepositorySkill` stored for that
repository and `dependency_fingerprint` under `factory.data_dir` using the
template `<data_dir>/repository-skills/v1/<repository-key>/...`. Guidance is
never stored in, or loaded from, the target repository or its worktree.

Only when the current fingerprint has no generated skill does the controller
enter a temporary `RESEARCHING` state. It calls the configured Researcher
(`Claude Opus 5` in the default profile) with purpose
`GENERATE_REPOSITORY_SKILL`. Invalid typed output or provenance receives one
bounded retry carrying the exact rejection reason. An infrastructure failure
also receives one retry. The calls run in the run's own directory rather than the
worktree, receive only the normalized `RepositoryProfile` and the two
configured URL lists. They never receive changed filenames, source code,
README content, task prose, or the diff. They have `web_fetch` as their only
tool, and run without repository custom instructions. An existing
generated file is never overwritten. A dependency change selects a new file and
earlier files remain. There is no TTL. Two concurrent first runs for the
same missing fingerprint can each run the bounded initial-plus-correction
sequence. Publication is atomic and no-clobber, so one winner is kept and
revalidated by both. This costs at most one extra sequence.

The repository key derives from the canonical local Git common directory.
Linked worktrees share one directory. A moved or re-cloned repository gets a
new key with no guidance.
If you want to carry its guidance across, use `factory skill path` before the move.

The `RepositorySkill` is bound to the profile's `dependency_fingerprint` and
carries bounded targets, HTTPS source provenance and separate simplify and
polish guidance. On every load, the controller rejects an invalid skill.
Rejection happens if the fingerprint mismatches, or a target is not in the
profile. It also rejects missing official provenance for detected frameworks,
unknown dependency claims, or sources outside configured lists.

Your own house rules go in a repository-level `repository-skill-overlay.yaml`
in the same storage, outside the target repository. It holds guidance prose
only (`mode: extend|replace` plus optional `simplify` and `polish` blocks), has
no targets, sources, versions or fingerprints, and survives dependency changes.
The factory never creates, rewrites, normalizes, refreshes or deletes it. An
invalid overlay is preserved, warned about, and ignored while valid generated
guidance still applies. `factory skill path`, `factory skill validate` and
`factory skill refresh` operate on these files explicitly. See
[Repository skills and overlays](../guides/repository-skills.md).

Nothing here can fail an already-green run. A failed re-profile, rejected
skill, invalid overlay, or stale guidance records a warning on
`repository-profile.json`. In those cases, the factory skips or disables
polish. Stored guidance that fails revalidation is left on disk
untouched and its warning points at `factory skill refresh`.

When guidance is accepted, the controller applies it (simplify first, then
version-specific polish) in one existing bounded worker attempt. That attempt
records `AttemptTrigger.POLISH`, consumes the implementation budget, can make no
edits, and is always fully verified again. Polish never runs during CI repair
and runs only when one later recovery attempt remains. Each run snapshots the
effective skill, valid overlay, and guidance provenance before agents see it.
Mid-run edits affect later runs only.

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

Requires `gh` on `PATH`. The factory passes `OWNER/REPO` to `gh`, which uses
the currently authenticated account and host configuration. Git transport can
use an allowlisted SSH host alias. Never force-pushes. Merging is
controlled separately.

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

`ci.enabled: false` disables factory observation/repair, not GitHub Actions
workflow triggers.

## merge

```yaml
merge:
  enabled: false
  method: "squash"
  allowed_repositories: []
  required_checks: []
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Merge after deterministic verification, independent review and CI pass. |
| `method` | `squash`, `merge`, `rebase` | `squash` | Normal GitHub merge method, never administrator override. |
| `allowed_repositories` | list of `OWNER/REPO` | `[]` | Exact repositories authorized for automatic merging, with no wildcards. |
| `required_checks` | list of check names | `[]` | Every named check must be present and successful on the current PR head. Missing, skipped and pending required checks cannot authorize merging. |

Enabling merging requires PR and CI enabled, `pull_request.draft: false`, an
explicit `pull_request.base_branch`, nonempty repository/check allowlists and
nonempty `repository.commands.verify`. GitHub branch rules must permit the
configured merge method and unattended delivery. Required human reviews are
not bypassed. The configured required checks must also be enforced by the
target's active GitHub protection policy, without a bypass that defeats
the server-side gate. A client-side check list alone cannot prevent a check
rerun racing a merge. The policy must require PRs and up-to-date branches.
Classic protection must enforce administrators. Supported active repository
or organization rulesets must have no bypass actors. Missing or unreadable
enforcement metadata is not treated as approval.
Classic PR bypass allowances for users, teams and apps must be explicitly
empty, and outstanding GitHub-required reviews still block the merge.
Conflicting, outdated or otherwise ineligible PRs stop with an
explicit reason. A run is `DONE` only after the actual merge is confirmed.

Project delivery requires either all three integrations disabled (local
integration) or all three enabled (serial PR-to-target delivery). See
[Projects](../guides/projects.md#autonomous-delivery).

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
| `enabled` | bool | `false` | Whether `factory start` can run at all. |
| `poll_interval_seconds` | int > 0 | `30` | Backlog poll interval. |
| `max_concurrent_tasks` | `1` or `2` | `1` | Concurrent runs. Validated, and higher values are rejected. |
| `stall_timeout_seconds` | int > 0 | `900` | Idle time before a run is treated as stalled. Also the default staleness threshold for `factory status`. |
| `required_label` | string | `agent-ready` | Issues must carry this label to be dispatched. |
| `max_runs_per_day` | int > 0 or null | `20` | Runs that can be claimed per UTC calendar day. `null` disables the cap. |

`max_runs_per_day` is counted from persisted run timestamps, so it survives a
restart. It is a cost bound: `scheduler.enabled` and `--runtime copilot` are
independent knobs, and a daemon with the real runtime can spend at whatever
rate the backlog allows.

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
