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

The writing policy is advisory and has no configuration switch. See
[Writing policy](writing-policy.md).

Plan decision answers use the existing `escalation` settings. They do not add
a command-line parameter or a configuration key.

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

## performance

```yaml
performance:
  mode: "standard"
  fast_model_profile: "economy"
```

The standard mode uses the selected model profile and permits the optional
polish pass. The fast mode is an explicit low-risk optimization.

The controller uses the fast mode only for `L0` or `L1` work with `R0` or
`R1` risk. Triage must not require research. Planned and changed files must
not include protected, sensitive, manifest, or version files.

If a condition fails, the controller uses the standard path. It records the
fallback reason in the run. Both modes keep deterministic verification, the
independent Tester, the independent Reviewer, and publishing controls.

The `fast_model_profile` must name a complete entry under `model_profiles`.
The controller uses that profile for the Refiner and Planner. It skips the
optional polish pass only while the run remains eligible.

## repository

```yaml
repository:
  branch_prefix: "factory/"
  command_timeout_seconds: 900
  commands:
    install: []
    verify: []
    build: []
  mutation_gate: true
  derive_commands: true
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
| `mutation_gate` | bool | `true` | After the verify commands pass, run `mutmut` on the changed Python modules and add the surviving mutants to the verification report (ADR-034). Advisory: it never fails verification. |
| `derive_commands` | bool | `true` | If all `commands` lists are empty, derive commands from the tools in the repository and run them on the base commit before triage (ADR-034). This runs repository code. |
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
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `true` in packaged default/example, `false` when omitted | Run at most one post-green Implementer polish attempt with the fixed simplify and polish guidance. |

The class fallback for `enabled` is `false`, so legacy configurations that omit
`polish` retain their previous one-pass behavior. The packaged default and
`config/factory.example.yaml` explicitly enable it.

Older configurations can contain `official_documentation_origins` and
`practice_reference_urls`. The factory ignores these two keys (ADR-034).

Polish runs only after the first successful deterministic verification and
scope assessment, before testing and review. No model writes or selects the
guidance. The polish attempt gets the bodies of the factory's `simplify` and
`polish` templates and the review lenses for the changed files. A stack lens
for React, Vue or Angular applies only when the repository declares one of its
dependencies. The attempt makes no Researcher call and no web request.

The controller applies the guidance (simplify first, then polish) in one
existing bounded worker attempt. That attempt records `AttemptTrigger.POLISH`,
consumes the implementation budget, can make no edits, and is always fully
verified again. Polish never runs during CI repair and runs only when one later
recovery attempt remains.

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

## escalation

```yaml
escalation:
  enabled: false
  authorized_identities: []
  allowed_associations:
    - "OWNER"
    - "MEMBER"
    - "COLLABORATOR"
  max_reopens: 3
  reply_window_hours: 168
  max_reply_polls_per_tick: 10
  max_notification_attempts: 3
  allowed_hosts:
    - "github.com"
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether GitHub escalation notices and replies are active. |
| `authorized_identities` | list of string | `[]` | Allowed GitHub logins or numeric user IDs. All-digit entries match user IDs only. |
| `allowed_associations` | list of string | `["OWNER", "MEMBER", "COLLABORATOR"]` | Allowed author association values. |
| `max_reopens` | int between 1 and 3 | `3` | Maximum reopens per run. |
| `reply_window_hours` | int between 1 and 336 | `168` | Hours before an escalation episode expires. |
| `max_reply_polls_per_tick` | int > 0 | `10` | Maximum escalated runs polled per cycle. |
| `max_notification_attempts` | int > 0 | `3` | Delivery retry ceiling for notice comments. |
| `allowed_hosts` | list of string | `["github.com"]` | Allowed hostnames for GitHub issues and pull requests. |

When enabled, `authorized_identities` must contain at least one entry.

## pi

Settings for `--runtime pi`. See [Real pi runs](../get-started/pi.md). The block
is optional. The packaged configuration omits it and every key has a default.

```yaml
pi:
  executable: "pi"
  provider: "github-copilot"
  session_reuse_max_age_seconds: 3600
  cache_retention: "long"
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `executable` | non-empty string | `pi` | The pi program. Doctor looks for it on `PATH`. |
| `provider` | non-empty string | `github-copilot` | The pi provider passed with `--provider`. Doctor checks for a credential for it. |
| `session_reuse_max_age_seconds` | int > 0 | `3600` | Longest gap after which the Implementer or Reviewer can continue its saved session. The factory never continues an older session. |
| `cache_retention` | `short` or `long` | `long` | Value of `PI_CACHE_RETENTION` for the pi process. |

The `models` block sets the model and reasoning level for each role on pi, as it
does on Copilot. Pi ignores `context_tier`. See
[Model selection](model-selection.md#models-on-the-pi-runtime).

## routing

Policy for adaptive execution routing with Jev.
Jev is a classifier from TypeSafe.
The factory calls it over HTTPS.
System One is the TypeSafe product that serves Jev.
It chooses one controller-offered Choice option with probabilities and confidence.

Read the [adaptive routing guide](../guides/adaptive-routing.md) for complete setup instructions.

```yaml
routing:
  enabled: false
  api_url: "https://api.typesafe.ai/v1/systemone"
  model: "jev-1.13.0"
  api_key_env_var: "JEV_API_KEY"
  timeout_seconds: 5.0
  min_confidence: 0.7
  min_probability: 0.5
  max_prompt_chars: 4000
  max_response_bytes: 65536
  single_max_changed_files: 5
  rubric_version: "1.0"
  full_only_terms:
    - "auth"
    - "authentication"
    - "authorization"
    - "permission"
    - "permissions"
    - "encrypt"
    - "encryption"
    - "credential"
    - "credentials"
    - "secret"
    - "secrets"
    - "migration"
    - "migrations"
    - "deploy"
    - "deployment"
    - "production"
    - "prod"
    - "billing"
    - "payment"
    - "payments"
  options:
    - id: "single_l0"
      route: "SINGLE"
      complexity: "L0"
      risk: "R0"
      description: "Single-pass execution with L0 worker"
    - id: "single_l1"
      route: "SINGLE"
      complexity: "L1"
      risk: "R0"
      description: "Single-pass execution with L1 worker"
    - id: "critique_l1"
      route: "CRITIQUE"
      complexity: "L1"
      risk: "R1"
      description: "Execution with L1 worker and independent review"
    - id: "critique_l2"
      route: "CRITIQUE"
      complexity: "L2"
      risk: "R1"
      description: "Execution with L2 worker and independent review"
    - id: "full_l2"
      route: "FULL"
      complexity: "L2"
      description: "Full SDLC factory pipeline with L2 worker"
    - id: "full_l3"
      route: "FULL"
      complexity: "L3"
      description: "Full SDLC factory pipeline with L3 worker"
    - id: "manual_triage"
      route: "MANUAL_TRIAGE"
      description: "Escalate for human manual triage"
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | Whether adaptive routing with Jev is active. |
| `api_url` | string | `"https://api.typesafe.ai/v1/systemone"` | TypeSafe System One API endpoint. |
| `model` | string | `"jev-1.13.0"` | Pinned Jev model version string. |
| `api_key_env_var` | string | `"JEV_API_KEY"` | Environment variable holding the TypeSafe API key. |
| `timeout_seconds` | float between 0 and 60 | `5.0` | Maximum seconds for the HTTPS request. |
| `min_confidence` | float between 0 and 1 | `0.7` | Minimum classifier confidence score. |
| `min_probability` | float between 0 and 1 | `0.5` | Minimum probability for the selected option. |
| `max_prompt_chars` | int between 500 and 16000 | `4000` | Maximum characters in outbound prompt text. |
| `max_response_bytes` | int between 1024 and 1048576 | `65536` | Maximum response bytes accepted from Jev. |
| `single_max_changed_files` | int between 1 and 50 | `5` | File change ceiling for SINGLE and CRITIQUE routes before ratcheting to FULL_REVIEW. |
| `rubric_version` | string | `"1.0"` | Version tag for the routing rubric. |
| `full_only_terms` | list of string | list of 20 terms | Terms that disallow short routes when matched. |
| `options` | list of option objects | list of 7 options | Route options offered to the classifier. |

### Option contract

The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.
You cannot configure `FULL_REVIEW` in `options`.

Each entry in `options` defines a route candidate:

| Field | Type | Required | Effect |
| --- | --- | --- | --- |
| `id` | string (1-64 chars) | yes | Unique option identifier. |
| `route` | `SINGLE`, `CRITIQUE`, `FULL`, `MANUAL_TRIAGE` | yes | Execution route for this option. |
| `complexity` | `L0`, `L1`, `L2`, `L3` | varies | Worker complexity for implementation. |
| `risk` | `R0`, `R1`, `R2`, `R3` | varies | Provisional risk level for short routes. |
| `model_profile` | string | no | Named model profile override. |
| `description` | string (0-200 chars) | no | Description passed to Jev. |

The configuration loader enforces these option rules:

- For `MANUAL_TRIAGE` options, omit `complexity` and `risk`.
- For `SINGLE` and `CRITIQUE` options, provide both `complexity` and `risk`.
- For `FULL` options, provide `complexity`. You can omit `risk`.

TypeSafe Choice questions support at most 255 options.
The factory enforces this limit at configuration load.

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

## risk_assessment

```yaml
risk_assessment:
  enabled: true
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | boolean | `true` | Turn the risk approval gate and the triage risk rationale on or off. |

The section is optional. When `enabled` is `true`, the factory works as described in the `risk` section.

When `enabled` is `false`, the factory runs without human risk approval:

- Triage still sets `risk`. Routing uses it.
- No risk level stops a run at `NEEDS_HUMAN`. This applies to `R0`, `R1`, `R2` and `R3`.
- The factory does not create a risk escalation or an approval request.
- The triage prompt does not ask for a `risk_rationale`.
- The factory accepts a triage result for `R2` or `R3` with no `risk_rationale`.
- Routing options treat every risk level as not needing approval.

The `risk` table stays in the file, but no run uses its `human_approval` values.
Other stops stay active. Examples are an ineligible work item, protected files and failed verification.

Each run records `risk_assessment_enabled` in `run.json`. The factory also logs a warning when a run starts with the switch off.
Each run keeps the choice it started with. A resume or a reopen of that run ignores a different setting.
A project also keeps its starting choice. A resumed project starts its remaining tasks with that choice.
A resume with `--no-risk-assessment` never removes an approval that a run already needs.
A resume at a delivery checkpoint accepts an approved `R2` or `R3` run only when the approval fingerprint still matches the persisted work item and triage.
Use `--no-risk-assessment` on `factory run`, `factory project`, `factory start` or `factory service install` to turn it off for one invocation.

## setup

```yaml
setup:
  enabled: true
```

| Key | Type | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | Let `factory start` open a setup pull request when the repository misses development tools (ADR-034). |

The check needs `pull_request.enabled` and `repository.derive_commands`. It runs at each service tick, but it plans again only when the source HEAD changes.
It opens one pull request for each new plan. The factory never merges a setup pull request.
The data directory keeps the last decision for each repository in `setup-state/`.

## Cross-field validation

The loader rejects a configuration when:

- `retries.max_total_attempts` is less than `retries.same_model_attempts`
- `models.workers` does not define exactly `L0`, `L1`, `L2` and `L3`
- `models.reviewer`'s model family matches any worker's model family
- `risk` does not define exactly `R0`, `R1`, `R2` and `R3`
- `ci.enabled` is true while `pull_request.enabled` is false
- `escalation.enabled` is true while `escalation.authorized_identities` is empty
- `scheduler.max_concurrent_tasks` is greater than `2`
- `routing.model` does not match the pinned pattern `^jev-\d+\.\d+\.\d+$`
- `routing.api_url` is not an HTTPS URL
- `routing.options` is empty
- `routing.options` contains duplicate identifiers
- `routing.options` does not contain an option with `route: FULL`
- `routing.options` contains more than 255 options
- `routing.options` defines an invalid option contract
- `routing.options` references an unknown model profile
- any key is not recognized
