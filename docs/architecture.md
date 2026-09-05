# Architecture

## Objective

Build a local autonomous software engineering factory that can eventually process backlog work through the entire SDLC.

The implemented pipeline is:

```text
task (manual or GitHub issue)
   ↓
prepare worktree
   ↓
deterministic repository profile
   ↓
triage
   ↓
refine
   ↓
research (only when triage asks for it, at most once)
   ↓
plan
   ↓
implement
   ↓
deterministic verification (install → verify → build)
   ↓
scope-drift governance
   ↓
one bounded implementer polish pass (when enabled)
   ↓
deterministic verification again
   ↓
scope-drift governance again
   ↓
independent tester
   ↓
independent reviewer
   ↓
ready for PR
   ↓
pull request        (opt-in: pull_request.enabled)
   ↓
CI observation      (opt-in: ci.enabled)
   ↓
bounded CI repair
   ↓
done
```

Everything after "ready for PR" is strictly opt-in. With the packaged
configuration a run performs no network access at all and completes at
`PR_READY`.

Still out of scope (deferred Phase 15 items):
- staging (15.3)
- deployment/promotion (15.4)
- Jira, Postgres, Temporal, Docker/Kubernetes workers, remote workers

Implemented (requested Phase 15 sub-phases, see `PLAN.md`):
- 15.0 factory CI for this repository
- 15.1 tag-driven release of native macOS artifacts
- 15.2 macOS runtime packaging and an opt-in user launchd service
- 15.5 local monitoring and health (`factory doctor`, `factory status`)
- 15.11 a read-only, loopback-only local dashboard (`factory dashboard`)

Phase 16 is also implemented: deterministic repository capability profiling and
an optional bounded post-green polish pass.

None of those change what the factory is allowed to do autonomously. They make
it installable, observable and inspectable on one MacBook.

## High-level architecture

```text
            Task Source
                │
                ▼
         Workflow Controller
                │
     ┌──────────┼──────────┐
     │          │          │
   Policy    Routing    Run Store
     │          │          │
     └──────────┼──────────┘
                │
                ▼
            Agent Runtime
                │
     ┌──────────┼────────────┐
     ▼          ▼            ▼
   Opus       Sonnet         MAI
     │
     ▼
  GPT-5.6 Sol

                │
                ▼
          Local Workspace
                │
        Git Worktree + Shell
                │
                ▼
     deterministic verification

Later:
                │
                ▼
              GitHub
                │
                ▼
          GitHub Actions
```

## Domain concepts

### WorkItem

Represents the software task.

Minimum properties:

```text
id
external_id
source
title
description
acceptance_criteria
constraints
labels
priority
complexity
risk
```

Initial sources:

```text
MANUAL
GITHUB
```

Jira comes later.

### FactoryRun

One execution of a WorkItem.

Suggested properties:

```text
id
work_item_id
state
attempt_records
workspace_path
branch_name
created_at
updated_at
last_activity_at
lease
completed_at
failure_reason
commit_sha
pull_request_url
```

`attempt_records` is the durable retry budget: every implementation, polish or
repair attempt appends exactly one record carrying its `budget`
(`IMPLEMENTATION`/`CI_REPAIR`) and `triggered_by` reason. Attempt numbers are
always derived from this persisted list, never from an in-process counter, so a
restart cannot grant a run a fresh budget.

`lease` records the host/pid currently executing the run, and
`last_activity_at` is refreshed on every transition so the scheduler can detect
a stalled run without inspecting lock files.

## Workflow states

The implemented SDLC states are exactly:

```text
CREATED
TRIAGING
REFINING
RESEARCHING
PLANNING
IMPLEMENTING
VERIFYING
REVIEWING
PR_READY
PR_CREATED
CI_RUNNING
CI_DIAGNOSIS
DONE
NEEDS_HUMAN
FAILED
```

There is deliberately no `REPAIRING`, `PLAN_READY` or `BLOCKED` state: repair is
a bounded transition back to `IMPLEMENTING` (or, for scope drift, back to
`PLANNING`), not a second workflow, and "blocked" is expressed as
`NEEDS_HUMAN` with a recorded reason.

There is also no `POLISHING` state or `POLISHER` role. When enabled, an
eligible bounded polish attempt first transitions `VERIFYING → RESEARCHING`
to generate a version-specific `RepositorySkill`, then `RESEARCHING →
IMPLEMENTING` for one ordinary `IMPLEMENTER` attempt triggered by `POLISH`,
followed by the normal `VERIFYING` transition. `RESEARCHING` remains a
temporary transition, not a new role. When the research or its validation
fails, the run stays on its existing green path: the reason is recorded as a
profile warning and the controller transitions straight to `REVIEWING`.

The workflow controller owns every transition. The full table is declared as
data in `workflow.ALLOWED_TRANSITIONS` and enforced on every call:

```text
CREATED      → TRIAGING
TRIAGING     → REFINING
REFINING     → RESEARCHING | PLANNING
RESEARCHING  → PLANNING | IMPLEMENTING
PLANNING     → IMPLEMENTING
IMPLEMENTING → VERIFYING
VERIFYING    → REVIEWING | IMPLEMENTING | PLANNING | RESEARCHING
REVIEWING    → PR_READY | IMPLEMENTING
PR_READY     → PR_CREATED
PR_CREATED   → CI_RUNNING | DONE
CI_RUNNING   → DONE | CI_DIAGNOSIS
CI_DIAGNOSIS → IMPLEMENTING
```

Every non-terminal state may additionally escalate to `NEEDS_HUMAN` (a business
decision: eligibility, risk, scope, exhausted budget, non-repairable CI) or
`FAILED` (an operational agent/infrastructure failure).

Terminal states are `DONE`, `NEEDS_HUMAN` and `FAILED`.

`PR_READY` is *not* terminal. When pull requests are enabled it continues to
`PR_CREATED`; when they are disabled it is the completed endpoint of the manual
flow and the controller finalizes it explicitly by stamping `completed_at`.
`workflow.is_run_finished` is the single predicate that expresses this, and the
scheduler uses it rather than a raw state comparison.

## Scheduling state

Scheduling ownership is separate from detailed SDLC state. `Scheduler` owns
reservations, ordering, bounded concurrency and stall detection entirely
in-memory; it never mutates a `FactoryRun`. `FactoryService` composes it with
`GitHubIssueProvider` and `WorkflowController`, and dispatches through a thread
pool bounded by `scheduler.max_concurrent_tasks` (1 or 2).

Two configured bounds are enforced, and both are supplied by the composition
root rather than assumed by the scheduler:

```text
scheduler.max_concurrent_tasks   how much may run at once   (1 or 2, in memory)
scheduler.max_runs_per_day       how much may be claimed    (per rolling UTC
                                 per day                     day, counted from
                                                             persisted runs)
```

The daily ceiling is counted from persisted `FactoryRun.created_at` timestamps,
so it survives a restart instead of resetting with the process. A tick stopped
by it reports `rate_limited` rather than looking like an empty backlog, and
reconciliation of already-running work is unaffected.

Recovery is conservative: a persisted, non-terminal run left behind by a dead
process is transitioned to `NEEDS_HUMAN` *through the controller*, never
auto-resumed. No paid retry is spent, the persisted budget is untouched, and the
workspace plus artifacts stay on disk.

Potential concepts:

```text
UNCLAIMED
CLAIMED
RUNNING
RETRY_QUEUED
RELEASED
```

Avoid conflating:
- what SDLC step is happening
- whether the scheduler owns this task

## Artifacts

### RepositoryProfile

Produced deterministically after the worktree is prepared and before
`TRIAGING`, and again before an eligible bounded polish attempt. It records:

```text
detector_version
manifest_fingerprint
dependency_fingerprint
markers
version_files
technologies
test_tools
package_managers
dependencies
warnings
```

The profiler walks repository-local paths, prunes generated/vendor directories
and reads only an allowlist of bounded manifests. It never uses a shell,
network or imports.

Each `dependencies` entry is a direct declaration with exact evidence:
ecosystem, name, declared version, an optional exact
`resolved_version`/`resolution_path`, manifest path and dependency group. The
parsed manifests are:

| Manifest | Ecosystem | Recorded as |
| --- | --- | --- |
| `pyproject.toml` (`project.dependencies`, `project.optional-dependencies.*`, `dependency-groups.*`) | Python | one declaration per requirement, grouped by table; `requires-python` becomes the `python` runtime target |
| `pyproject.toml` (`tool.poetry.dependencies`, `tool.poetry.dev-dependencies`, `tool.poetry.group.*.dependencies`) | Python | one declaration per entry, grouped by table; also marks the `poetry` package manager |
| `requirements.txt`, `requirements-*.txt` | Python | one declaration per requirement in group `requirements`; marks the `pip` package manager |
| `setup.cfg`, `tox.ini` | Python | technology and pytest evidence only — no versions |
| `package.json` (`dependencies`, `devDependencies`, `peerDependencies`, `optionalDependencies`, `packageManager`) | npm | one declaration per entry, grouped by table |

Exact versions are resolved from `uv.lock`, `package-lock.json` and
`pnpm-lock.yaml` when unambiguous; an ambiguous resolution records a warning
instead of a version. `poetry.lock`, `yarn.lock`, `bun.lock`/`bun.lockb`,
`Pipfile.lock` and `pylock.toml` mark their package manager where applicable
and are fingerprinted as `version_files`, but exact graph parsing is not
claimed for them.

Two SHA-256 fingerprints are recorded and they are not interchangeable:

- `dependency_fingerprint` is semantic. It digests the detected technologies,
  test tools, package managers and normalized dependency declarations. It is
  the identity a generated skill is bound to.
- `manifest_fingerprint` is provenance. It digests the content of every
  `version_files` path (`package.json`, `pyproject.toml`, requirements files
  and lockfiles). Formatting or comment-only manifest edits change it without
  invalidating a skill.

There is no fixed built-in skill catalog. See RepositorySkill below for how
version-specific guidance is generated.

### RepositorySkill

Generated on demand — not selected from a catalog — only when
`polish.enabled` and the bounded polish attempt is eligible. After the first
successful deterministic verification and scope assessment, the controller
re-profiles the post-implementation worktree, transitions through a
temporary `RESEARCHING` state, and calls the configured Researcher (`GPT-5.6
Sol` by default) with purpose `GENERATE_REPOSITORY_SKILL`, at most once per
run.

That invocation is web-only and deliberately blind to the repository. It runs
with the run's own persistence directory as its working directory, not the
worktree, and its only tool is `web_fetch`. Repository custom instructions are
disabled for it. It receives the normalized `RepositoryProfile`, the
controller-derived changed file paths, the two configured URL lists and the
factory-owned generation rules — never source code, README content, task prose
or the diff. It may fetch only:

- `polish.official_documentation_origins`: official documentation, migration
  guides and release notes. These are authoritative for every version claim.
- `polish.practice_reference_urls`: exact curated general-practice references
  (by default the reviewed `bdfinst/agentic-dev-team` notes, pinned to commit
  `52cc5efd`, not a mutable branch). They may contribute generic quality
  heuristics only, synthesized rather than copied, and never version claims,
  tools, commands or orchestration.

It returns one typed artifact, persisted as `repository-skill.json`:

```text
generator_version
dependency_fingerprint
generated_at
targets
official_sources
practice_sources
simplify
polish
uncertainties
```

`targets` are bounded package/runtime versions with evidence paths.
`official_sources` and `practice_sources` are HTTPS citations from the
respective configured lists; each names, in `applies_to`, the detected
dependencies it grounds, and a practice source may instead use the single
generic marker `repository`. `simplify` and `polish` are each a bounded
`SkillGuidance` (summary, guidance, things to avoid, validation). The model
itself refuses a skill that has neither an official source nor an explicit
uncertainty, and refuses an official source claiming generic applicability.

The controller then validates the artifact deterministically and rejects it
when:

- its `dependency_fingerprint` does not match the profile it was generated
  from,
- a target is not an exact profiled dependency declaration
  (ecosystem, name, declared version, resolved version),
- target evidence paths are not profile `version_files`, manifest paths or
  resolution paths,
- a detected `python`, `pytest`, `react`, `react-dom`, `vite` or `vitest`
  dependency has no target, or is not named by the `applies_to` of at least
  one accepted official source,
- a source claims applicability to a dependency the profile did not detect, or
- a cited source falls outside `polish.official_documentation_origins`
  (compared by origin) or is not an exact `polish.practice_reference_urls`
  entry.

Rejection never fails an already-green run. The reason is appended to the
persisted profile's `warnings`, polish is skipped, and the run continues to
testing and review. The same applies when the re-profile itself fails. Before
testing and review the controller re-profiles once more: if profiling fails or
the `dependency_fingerprint` has changed since generation, the skill is
treated as stale, disabled for the Tester and Reviewer, and the reason is
recorded as a profile warning.

The skill reaches only the polish Implementer, Tester and Reviewer, and is
never available before the initial green baseline. It is regenerated fresh
for every eligible run; there is no cross-run cache. It is advisory and
cannot alter tools, models, workflow states, gates, commands or permissions.

### TriageResult

Fields approximately:

```text
factory_eligible
complexity
risk
requirements_quality
needs_research
dependencies
unknowns
confidence
```

### Specification

Fields approximately:

```text
problem
acceptance_criteria
constraints
assumptions
unknowns
dependencies
risk_flags
confidence
```

Unknown information must remain explicit.

Do not silently invent requirements.

### ResearchReport

Only produced when necessary.

Fields approximately:

```text
question
findings
evidence
implications
uncertainty
```

### ExecutionPlan

Fields approximately:

```text
summary

steps:
  - id
  - goal
  - likely_files
  - validation

expected_scope:
  modules
  estimated_files_min
  estimated_files_max

test_strategy

risks
```

### ChangeSet

Fields approximately:

```text
changed_files
summary
tests_added
commands_run
```

`changed_files` and the actual Git diff are derived by the controller from the
workspace. They are not trusted agent claims. Git evidence must include
untracked files.

### VerificationReport

Deterministic, factory-produced evidence only. Nothing in it is an agent claim.

Fields approximately:

```text
passed
deterministic_checks
failures
coverage_change
test_findings
confidence
```

### TestReport

Independent AI tester judgement, deliberately a *separate* artifact so a
model's opinion can never be mistaken for deterministic evidence. `passed` is
advisory: gating still uses the `VerificationReport`.

Fields approximately:

```text
passed
findings
suggested_tests
confidence
```

### CIReport

Normalized, persisted CI evidence (`ci.json`). Produced by the controller from
`gh pr checks` output; expressed with plain strings so the domain layer has no
dependency on the `gh` adapter.

Fields approximately:

```text
overall
checks:
  - name
  - status
  - description
  - details_url
  - failure_category
  - log_excerpt
observed_at
repair_attempts_used
timed_out
```

### ReviewReport

Fields approximately:

```text
approved
findings
scope_concerns
security_concerns
compatibility_concerns
suggested_changes
```

## Complexity model

### L0
Mechanical work:
- formatting
- lint
- straightforward Sonar finding
- simple rename
- trivial CSS adjustment
- obvious duplication
- simple type error

Default worker: `MAI-Code-1.1-Flash`

### L1
Normal isolated task.

Default: `Claude Sonnet 5`

### L2
Examples:
- cross-module change
- difficult defect
- significant new functionality
- complicated integration behavior

Default: `Claude Opus 5`

### L3
Examples:
- architecture
- unfamiliar subsystem
- large ambiguity
- repeated failures

Default: `Claude Opus 5`

Potentially invoke research first.

## Risk model

### R0
Examples:
- documentation
- formatting
- harmless refactor
- visual-only adjustment

### R1
Normal application behavior.

### R2
Examples:
- authentication
- authorization
- database migration
- public API
- security-sensitive behavior
- dependency changes

### R3
Examples:
- production infrastructure
- secrets
- destructive migration
- deployment control
- critical security behavior

Risk controls required gates.

It does not directly select the worker model.

## Initial agents

### Triage
Model: `Claude Sonnet 5`

Permissions:
- repository read

Output: `TriageResult`

### Specification Refiner
Model: `Claude Opus 5`

Permissions:
- repository read

Output: `Specification`

### Researcher
Model: `GPT-5.6 Sol`

Invoke only when required.

Permissions:
- repository read
- research capability

Output: `ResearchReport`

The same role also serves the `GENERATE_REPOSITORY_SKILL` purpose for an
eligible polish attempt. That invocation is different: it has no repository
read at all, runs in the run's persistence directory, has only `web_fetch`
restricted to the configured official documentation origins and curated
practice references, and outputs a `RepositorySkill` instead of a
`ResearchReport`.

### Planner
Model: `Claude Opus 5`

Permissions:
- repository read
- read-only commands where useful

No source modifications.

Output: `ExecutionPlan`

### Implementer
Model selected by complexity.

Permissions:
- assigned workspace
- repository edit
- shell
- tests

Output: `ChangeSet`

Receives the researcher-generated `RepositorySkill` only during the optional
post-green polish attempt, applying simplification first and version-specific
polish second; the initial implementation attempt receives none.

### Tester
Model: `Claude Sonnet 5`

Receives:
- Specification
- ExecutionPlan
- actual diff
- repository

The implementer's `ChangeSet` (including its summary) is never provided: the
tester sees only controller-derived Git evidence plus deterministic results.

Receives the same post-green `RepositorySkill` as the polish Implementer,
once one has been generated and while it is still current; none before that
point, and none when the skill was disabled as stale.

Output: `TestReport`

### Reviewer
Model: `GPT-5.6 Sol`

Receives:
- Specification
- ExecutionPlan
- controller-derived diff and changed files
- deterministic `VerificationReport`
- independent `TestReport`

Never receives the implementer's `ChangeSet` summary.

Receives the same post-green `RepositorySkill` as the polish Implementer,
once one has been generated and while it is still current; none before that
point, and none when the skill was disabled as stale.

Checks:
- correctness
- requirements
- edge cases
- regression risk
- security
- unnecessary complexity
- maintainability
- API compatibility
- scope drift

Output: `ReviewReport`

### Failure Investigator
Model: `Claude Opus 5`

Not part of happy-path V1.

Later invoked after repeated implementation or CI failures.

## Model router

Model routing must be deterministic configuration.

Agents may recommend:

```text
complexity = L2
```

but the controller maps:

```text
L2 → Claude Opus 5
```

Do not let arbitrary agent output choose arbitrary models.

## Policy engine

Do not build a large policy framework in V1.

Start with explicit functions/configuration.

Eventually policies answer questions such as:

```text
may_run_task(...)
required_checks(...)
should_research(...)
can_retry(...)
should_escalate(...)
requires_human(...)
may_create_pr(...)
```

Keep business policy outside prompts.

## Retry policy

Initial proposal:

```text
same implementation model attempts: 2
maximum total implementation and repair attempts: 6
later CI repair attempts: 3
```

Escalation:

```text
MAI fails twice
    ↓
Sonnet

Sonnet fails twice
    ↓
Opus

Opus continues failing
    ↓
NEEDS_HUMAN
```

Every entry into `IMPLEMENTING` appends one attempt record. Verification,
review and the optional post-green polish consume the same monotonic
implementation budget so no path can evade the limit. Polish runs at most once,
only after the first successful deterministic verification, never during CI
repair, and only when one later recovery attempt remains available. It may make
no edits; deterministic verification and scope assessment always run again.
Actual limits belong in configuration.

## Local workspace

Base directory:

```text
~/.software-factory/
```

Suggested layout:

```text
~/.software-factory/
├── runs/
│   └── RUN-ID/
│       ├── run.json
│       ├── work-item.json
│       ├── repository-profile.json
│       ├── triage.json
│       ├── specification.json
│       ├── research.json
│       ├── execution-plan.json
│       ├── change-set.json
│       ├── patch.diff
│       ├── verification.json
│       ├── test-report.json
│       ├── review.json
│       ├── ci.json
│       ├── logs/
│       └── attempts/
│           └── NN/
│               ├── change-set.json
│               ├── patch.diff
│               ├── verification.json
│               ├── test-report.json
│               └── review.json
└── workspaces/
    └── TASK-ID/
        └── repository worktree
```

## Persistence

V1 uses filesystem persistence.

Provide a small `RunStore` interface with behavior conceptually similar to:
- save_run()
- load_run()
- list_runs()
- save_artifact()
- load_artifact()

Initial implementation: `FileRunStore`

A future implementation might be: `PostgresRunStore`

Do not implement a database until needed.

Writes are atomically replaced and versioned because filesystem data is the V1
recovery source of truth.

## Workspace abstraction

Provide something conceptually like `WorkspaceProvider`.

Operations:
- prepare()
- get_path()
- diff()
- cleanup()

Initial implementation: `GitWorktreeWorkspace`

Do not build generic remote-worker abstractions yet.

Workspaces use sanitized, root-contained paths and are preserved by default.
Cleanup must refuse paths outside the configured workspace root. A short-lived
exclusive lock prevents simultaneous ownership of the same work item.

## Agent runtime abstraction

Conceptually:

```text
AgentRuntime.run(
    request
) -> AgentResult
```

`AgentRequest` includes the role, configured model and reasoning level, typed
context, assigned workspace path where applicable, and a timeout.

Production runtime: `CopilotAgentRuntime` (`--runtime copilot`), which builds a
role-scoped prompt, runs the `copilot` CLI with constrained tool permissions and
a scrubbed environment, and validates exactly one typed artifact from the final
response. Malformed output is an explicit agent failure, never a silent pass.

Default runtime: `FakeAgentRuntime` (`--runtime fake`). It is the CLI default so
no command can make a paid call by accident, and it is the only runtime the test
suite uses.

The domain and workflow layers must not depend on Copilot-specific SDK objects.

## Fake agents

Fake agents are deterministic test doubles.

They allow tests such as:

```text
attempt 1 → fail
attempt 2 → fail
escalation
attempt 3 → success
```

without:
- paid model calls
- network
- nondeterminism

Keep them simple.

## Local verification

Repository configuration defines commands.

Example:

```yaml
install:
  - bun install

verify:
  - bun run lint
  - bun run typecheck
  - bun test

build:
  - bun run build
```

The factory runs deterministic checks after implementation.

When `polish.enabled` is true, the first successful deterministic verification
and scope assessment are followed by one bounded web-only research call and at
most one `IMPLEMENTER` polish attempt. The full deterministic verification and
scope assessment then run again before the tester and reviewer. If research or
skill validation fails, polish is skipped with a recorded warning and the
already-green run proceeds unchanged. The packaged default and example enable
polish; a legacy configuration that omits the section uses the model fallback
of `false`.

The small command runner is part of Phase 1. Empty command lists pass, keeping
repositories usable before they add factory-specific configuration.

## Scope drift

Compare plan expectations with actual Git diff.

Deterministically detect at least:
- files outside expected modules
- excessive file count
- dependency file changes
- migration creation
- CI/workflow modification
- infrastructure modification

Later add:
- public API detection
- authentication/authorization changes

Unexpected scope causes `REPLAN` or `NEEDS_HUMAN` depending on risk.

Assessment runs after deterministic verification passes and before the tester,
reviewer or any publishing. `REPLAN` returns the run to `PLANNING` at most
`scope_drift.max_replans` times (counted from persisted attempt records
triggered by `SCOPE`), then escalates. The risk/sensitive-scope gate is
re-evaluated at the PR boundary, together with a deterministic publish gate
enforcing `repository.max_changed_files` and `repository.protected_file_patterns`.

## Git ownership

Agents edit files.

Controller owns:
- worktree creation
- branch creation
- commit
- push
- PR creation

Agents must not directly push protected branches.

Initial branch naming:

```text
factory/<task-id>
```

`GitPublisher` never force-pushes, never merges, never mutates repository
configuration or remotes (only `git remote get-url` is permitted), and refuses
remotes whose host is outside `pull_request.allowed_hosts`. A CI repair pushes
an additional normal commit to the same branch, updating the existing PR rather
than creating a new one.

GitHub credentials are read from the controller's own environment and handed to
`gh` through the child environment only. `CopilotAgentRuntime` independently
strips `GH_TOKEN`/`GITHUB_TOKEN`/etc. from every agent subprocess, so no agent
ever sees them.

## Command surface

One CLI, with an explicit split between commands that may change something and
commands that may not:

```text
factory --version              version only, no side effects
factory run                    mutates: creates a run, a worktree, artifacts
factory start                  mutates: dispatches runs (opt-in scheduler)
factory runs / show            read-only
factory doctor                 read-only apart from the data-dir write probe
factory status                 read-only; does not even create the data dir
factory dashboard              read-only server, explicit and blocking
factory service install        mutates: one per-user LaunchAgent plist
factory service status         read-only
factory service uninstall      mutates: removes that plist only
```

`run`, `start` and `dashboard` attach the bounded structured log under
`<data_dir>/logs` once configuration and the data directory are resolved. The
dashboard token is printed to stdout and never logged.

Exit codes are uniform: `2` means "this environment or configuration cannot do
what you asked" (invalid configuration, disabled feature, missing prerequisite,
unusable port, refused install), `1` means "the command ran and the answer is
no" (a run that did not succeed, an unknown run id, a doctor report with
errors).

## Observability

Record every agent invocation:
- run_id
- role
- model
- reasoning
- started_at
- completed_at
- duration
- attempt
- result
- token usage if available
- cost if available

Record task metrics:
- first-pass success
- total attempts
- human intervention
- time to ready-for-PR
- review findings
- final status

JSON + structured logs are the substrate. Structured logs are written locally,
bounded in size, inside the configured data directory, with the same credential
redaction already applied to captured command output. Nothing is exported: no
telemetry backend, no exporter, no network egress.

Token usage and cost are recorded only when the runtime actually reports them.
An unreported value stays unknown; it is never defaulted to zero and never
reconstructed from a price table (ADR-017).

## Health and metrics

Persisted run artifacts are the source of truth, so health and metrics are
*derived* on demand rather than accumulated. There is no counter store and no
time-series database.

Metrics are pure functions over the run store:

```text
runs by final state
first-pass success rate
attempts per run
scope replans
CI repair cycles
escalations to NEEDS_HUMAN
stage and run durations
```

`factory status` renders both surfaces (human-readable or `--json`) and is
strictly read-only: it will not even create the data directory.

Health reports operational facts about this machine:

```text
factory doctor
  data directory writable
  configuration valid
  git available
  prerequisites present for enabled features only
factory status / dashboard
  stale work-item locks
  orphaned worktrees
  non-terminal runs left behind by a dead process
factory service status
  launchd service registered / not registered
```

Both are strictly read-only. They report a stale lock, an orphaned worktree or
an abandoned run as findings; repairing one remains an explicit operator action
through the controller, exactly as in ADR-011.

## Delivery and packaging

Delivery ends at a published release artifact. A `v*` tag builds a GitHub
Release; it does not install, restart, promote or self-update anything
(ADR-015).

```text
version tag
    ↓
release quality gate (format + lint + types + tests + dependency audit)
    ↓
build + validate distributions and native macOS artifacts
    ↓
attest public-repository artifacts
    ↓
GitHub Release (workflow refuses to replace an existing one)
    ↓
human downloads and extracts
```

Releases are treated as write-once by convention, not by platform guarantee. The
release workflow fails if the tag's release already exists, so a re-run cannot
replace published artifacts. It cannot stop an edit or delete through the GitHub
UI or API. GitHub's own release immutability is a repository setting, it is off
by default, and the current releases report `immutable=false`; enable it in the
repository settings before relying on platform enforcement. `SHA256SUMS` and
`build-info.json` are what let a consumer detect a swapped artifact in the
meantime.

A release contains:

```text
software-agent-factory-<version>-macos-arm64.tar.gz     PyInstaller onedir
software-agent-factory-<version>-macos-x86_64.tar.gz    PyInstaller onedir
software_agent_factory-<version>-py3-none-any.whl
software_agent_factory-<version>.tar.gz
SHA256SUMS
build-info.json
```

The two macOS archives are built natively on their own runners. There is no
`universal2` build.

Artifacts are unsigned or ad-hoc signed; Developer ID signing and notarization
are deferred, so Gatekeeper quarantine is a documented, expected condition and
release notes must explain it.

A frozen runtime bundles Python and the factory, not the toolchain:

```text
required always      git
required if enabled  gh        (pull_request.enabled / ci.enabled /
                                scheduler.enabled)
required if chosen   copilot   (--runtime copilot)
```

Preflight validates prerequisites for *enabled* features only, so a default
offline run never demands `gh` or `copilot`. `gh` covers every GitHub-touching
feature, including the backlog daemon: `scheduler.enabled` polls GitHub Issues
through `gh`.

`factory doctor` runs the full report; `factory run` and `factory start` apply
the same rule as a cheap `PATH`-only gate that fails with one explicit line and
exit code 2 before any work starts.

## Local service

Continuous operation is a per-user `launchd` LaunchAgent, installed by an
explicit CLI command and by nothing else (ADR-018).

```text
~/Library/LaunchAgents/<label>.plist
    ↓
factory start
    ↓
--runtime fake by default
```

No root `LaunchDaemon`, no automatic installation, no installation as a side
effect of extracting an archive or running a command. The installer captures an
explicit `PATH` snapshot because launchd agents inherit a minimal environment,
refuses unless the given configuration enables the scheduler, and refuses while
`factory doctor` reports an error.

launchd's own stdout/stderr go to `/dev/null`: the factory writes its own
bounded, rotating structured log under `<data_dir>/logs/factory.log`, and a
launchd-captured stdio file is never rotated. `KeepAlive` is `Crashed`-only, so
no exit code — including the CLI's configuration-error code 2 — can produce a
restart loop.

Uninstall unloads the agent and removes the plist, leaving runs and workspaces
intact.

## Local dashboard

`AGENTS.md` bans web dashboards in V1. One narrow, explicitly requested
exception exists (ADR-016) and it is a viewer, not a control plane.

```text
factory dashboard          explicit command, disabled by default
    ↓
127.0.0.1 only
    ↓
token required (generated per start)
    ↓
GET only, read-only
```

Implemented with the Python standard library: no web framework, no npm, no
bundler, no build step. It renders the run list, run detail, workflow state,
attempt history and the derived metrics above. It renders no command logs and
no diffs at all, because those are where repository content and near-secret
material would leak into a browser.

Data minimization is applied twice, independently. The detail provider builds a
typed `RunDetail` containing only summary fields, completion facts and attempt
metadata — never `failure_reason`, agent reasoning or a raw artifact — and the
request handler then allowlists the fields it renders, so a future provider
mistake still cannot leak content. A run that does not exist, or whose id is
not even shaped like one, is a 404.

It cannot approve, retry, cancel or reconfigure anything. Authority stays with
`WorkflowController`.

## Long-term architecture

The current abstractions should permit later addition of:
- Jira
- staging
- deployment
- Postgres
- remote workers
- Kubernetes
- Docker sandboxes

Do not implement those merely to prove future compatibility.
