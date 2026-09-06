# Implementation Plan

## Status

Phases 0-14 and Phases 16-17 are implemented and integrated.

Five Phase 15 sub-phases were explicitly requested and are now implemented:
15.0 factory CI, 15.1 tag-driven release/CD, 15.2 macOS runtime packaging and
the user launchd service, 15.5 local monitoring/health and 15.11 the read-only
local dashboard. Every other Phase 15 item — including staging (15.3) and
deployment (15.4) — stays deferred and unimplemented.

Two things about 15.1 can only be *proven* in GitHub Actions, never on a
developer machine: the published release of an actual `v*` tag, and the
native Intel (`macos-15-intel`) build. Both are implemented and statically
tested here; the first real tag is what exercises them end to end.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture sanity check | done |
| 1 | Deterministic vertical slice | done |
| 2 | Real Copilot planner | done (`CopilotAgentRuntime`) |
| 3 | Real specification + triage | done |
| 4 | Real implementer | done |
| 5 | Repository verification policy | done (`governance.RepositoryVerifier`) |
| 6 | Tester + reviewer | done (independent `TestReport`/`ReviewReport`) |
| 7 | Routing calibration | done (`routing.ModelRouter`) |
| 8 | Research | done (optional, at most once per run) |
| 9 | Scope drift | done (`governance.ScopeDriftPolicy`) |
| 10 | Pull request creation | done, opt-in (`pull_request.enabled`) |
| 11 | GitHub Actions observation | done, opt-in (`ci.enabled`) |
| 12 | CI repair | done, bounded by `ci.repair_attempts` |
| 13 | Local backlog daemon | done (`factory start`, opt-in) |
| 14 | Parallelism | done (`max_concurrent_tasks` 1 or 2) |
| 15.0 | Factory CI for this repository | done (`.github/workflows/ci.yml`) |
| 15.1 | Tag-driven release / continuous delivery | done (`release.yml`; first `v*` tag proves it end to end) |
| 15.2 | macOS runtime packaging + user launchd service | done (`factory service`, PyInstaller `onedir`) |
| 15.3 | Staging environment | deferred |
| 15.4 | Deployment / promotion | deferred |
| 15.5 | Local monitoring and health | done (`factory doctor`, `factory status`) |
| 15.6 | Docker sandboxed execution | deferred |
| 15.7 | Remote workers | deferred |
| 15.8 | Postgres run store | deferred |
| 15.9 | Temporal / durable workflow engine | deferred |
| 15.10 | Jira and other trackers | deferred |
| 15.11 | Read-only local dashboard | done (`factory dashboard`) |
| 15.12 | Kubernetes workers | deferred |
| 16 | Repository capability layer + bounded post-green polish | done (`repository_profile`, `polish.enabled`, `GENERATE_REPOSITORY_SKILL`) |
| 17 | Project brief decomposition + bounded project execution | done (`factory project`) |

Every integration is disabled by default: with the packaged configuration
`factory run` performs no network access, makes no paid model call
(`--runtime fake` is the default) and finishes at `PR_READY`. No dashboard
listens unless `factory dashboard` is running, and no launchd service exists
unless someone ran `factory service install`.

## Principle

Build a complete but extremely small vertical slice first.

Do not try to implement the final factory in one pass.

Each milestone should leave the project working.

# Phase 0. Architecture sanity check

Before substantial coding:

1. read `AGENTS.md`
2. read `docs/architecture.md`
3. read `docs/symphony-alignment.md`
4. critically inspect the proposed architecture
5. simplify anything unnecessarily complex
6. document any intentional architectural change

Do not expand project scope.

Deliverable:

```text
docs architecture is internally consistent
```

No production integrations required.

# Phase 1. Small deterministic vertical slice

Implement one synchronous, useful path rather than a workflow with no repository
boundary.

Domain:
- WorkItem
- FactoryRun
- WorkflowState
- persisted implementation AttemptRecord

Typed artifacts:
- TriageResult
- Specification
- ResearchReport
- ExecutionPlan
- ChangeSet
- VerificationReport
- ReviewReport

Runtime:
- one authoritative WorkflowController
- FileRunStore with versioned atomic JSON writes
- deterministic ModelRouter
- one global bounded implementation/repair budget
- FakeAgentRuntime
- Git worktree workspace handling
- deterministic repository command runner
- controller-derived changed files and Git diff
- minimal risk gate

CLI:
- factory run
- factory runs
- factory show

```bash
factory run \
  --repo /path/to/repository \
  --title "Test task" \
  --description "A demonstration task"
```

should execute:

```text
CREATED
  ↓
TRIAGING
  ↓
REFINING
  ↓
PLANNING
  ↓
IMPLEMENTING
  ↓
VERIFYING
  ↓
REVIEWING
  ↓
PR_READY
```

using fake agents. The fake implementer makes a deterministic sample edit so
the slice proves that changes happen only inside an isolated worktree.

Persist state and artifacts under:

```text
~/.software-factory/runs/
```

Workspace behavior:
- create or safely restore a worktree under the configured data directory
- preserve workspaces by default
- reject unsafe or conflicting paths
- prevent two active runs from owning the same work item
- include untracked files in the collected diff

The controller, not the agent, derives `changed_files` and `patch.diff` from Git.

Repository verification commands are optional. An empty command list passes.
Configured commands run with a timeout and their results are persisted.

Every implementation or repair entry appends one attempt record and consumes
one global maximum. Verification and review failures share that budget.
Exhaustion ends in `NEEDS_HUMAN`.

## Acceptance criteria

- all workflow transitions are exhaustively tested
- invalid transitions are rejected
- artifacts serialize to versioned JSON
- state writes are atomic
- run state and attempt history survive process termination
- source working tree remains untouched
- each run gets an isolated workspace
- new and modified files appear in controller-derived Git evidence
- interrupted workspaces remain recoverable
- duplicate active work is rejected without corrupting a workspace
- deterministic verification failure drives bounded model escalation
- risk requiring human approval cannot reach `PR_READY`
- no network or LLM access is required
- `uv run ruff check .` passes
- `uv run pytest` passes

Explicitly deferred:
- scheduler and `factory start`
- backlog polling and tracker adapters
- durable scheduler claims and retry timers
- multi-task concurrency
- long-running worker heartbeats
- real Copilot calls
- pull requests and CI
- scope-drift policy beyond collecting authoritative Git evidence

The synchronous CLI uses short-lived per-work-item lock files and command
timeouts as the smallest useful substitutes for claim ownership and stall
detection.

Stop after completing this phase and inspect the design.

# Phase 2. Real Copilot Planner

Status: done. `CopilotAgentRuntime` runs every role; `--runtime copilot`
opts in, `--runtime fake` (the default) never spends money.

Implement `CopilotAgentRuntime`, but initially connect ONLY the Planner.

Use:

```text
Claude Opus 5
high reasoning
```

Input:
- WorkItem
- Specification
- repository context

Require structured output matching `ExecutionPlan`.

Validate output with Pydantic.

Malformed output should become an explicit agent failure.

Do not connect other real agents yet.

## Acceptance criteria

A manually supplied task against a real repository produces a valid persisted ExecutionPlan.

Normal unit tests still use FakeAgentRuntime.

Stop and review.

# Phase 3. Real Specification + Triage

Status: done.

Connect:

```text
Triage
  Claude Sonnet 5

Refiner
  Claude Opus 5
```

Produce structured:
- TriageResult
- Specification

Research should remain disabled unless necessary.

## Acceptance criteria

A vague task can become an explicit specification containing:
- problem
- acceptance criteria
- assumptions
- unknowns
- constraints

The refiner must distinguish assumptions from facts.

# Phase 4. Real Implementer

Status: done. Complexity routing arrived with Phase 7.

Initially use only:

```text
Claude Sonnet 5
```

Do NOT implement complexity routing yet.

Implementer receives:
- Specification
- ExecutionPlan
- workspace

It may:
- read
- edit
- shell
- run repository commands

It must not:
- push
- merge
- alter workflow state

After execution, persist:
- ChangeSet
- patch.diff

## Acceptance criteria

A simple task can produce a real code change inside the worktree.

# Phase 5. Repository verification policy

Status: done. `governance.RepositoryVerifier` runs install -> verify -> build,
persists per-command logs under `runs/RUN-ID/logs/`, and classifies failures.

Expand the Phase 1 command runner into repository-specific verification policy.

Example:

```yaml
commands:
  verify:
    - bun run lint
    - bun run typecheck
    - bun test

  build:
    - bun run build
```

Run these automatically after real implementation.

Capture:
- command
- exit code
- stdout/stderr reference
- duration

Add install/build policy, durable log files, and failure classification. Failure
continues to use the global bounded implementation repair budget.

## Acceptance criteria

Broken code cannot reach `REVIEWING` while required deterministic checks fail.

# Phase 6. Tester + Reviewer

Status: done. The tester returns a `TestReport` (never a `VerificationReport`),
and neither gate receives the implementer's `ChangeSet` summary.

Add:

```text
Tester
  Claude Sonnet 5

Reviewer
  GPT-5.6 Sol
```

Tester receives:
- Specification
- ExecutionPlan
- diff
- repository

Reviewer receives:
- Specification
- ExecutionPlan
- diff
- deterministic verification
- Tester findings

Do not feed them implementer self-justification.

## Acceptance criteria

Successful flow becomes:

```text
IMPLEMENT
   ↓
deterministic verification
   ↓
independent tester
   ↓
independent reviewer
   ↓
PR_READY
```

Reviewer rejection enters a bounded repair cycle.

# Phase 7. Routing calibration

Status: done. Model choice and attempt are persisted on every `AttemptRecord`.

Enable:

```text
L0
  MAI-Code-1.1-Flash

L1
  Claude Sonnet 5

L2
  Claude Opus 5

L3
  Claude Opus 5
```

Calibrate the deterministic ModelRouter using measured outcomes.

Add escalation:

```text
MAI failure threshold
      ↓
   Sonnet
      ↓
Opus if needed
```

Persist model choice and attempt.

## Acceptance criteria

Model routing is fully unit tested without calling models.

# Phase 8. Research

Status: done. Triage's `needs_research` runs the researcher exactly once, the
`ResearchReport` is persisted, and planning continues. It no longer escalates.

Enable optional Researcher:

```text
GPT-5.6 Sol
```

Invoke only if triage/planning determines research is necessary.

Output: `ResearchReport`

Do not make every task pay for a research step.

# Phase 9. Scope drift

Status: done. Assessed after successful deterministic verification and again at
the PR boundary. `REPLAN` is bounded by `scope_drift.max_replans`.

Compare `ExecutionPlan.expected_scope` against actual Git diff.

Detect initial cases:
- unexpected directories/modules
- excessive changed-file count
- package/dependency modifications
- migration files
- CI workflow modifications
- infrastructure files

Result may be:
- continue
- replan
- NEEDS_HUMAN

depending on risk.

# Phase 10. Pull request creation

Status: done, opt-in via `pull_request.enabled` (default false). Never merges.

Controller handles:
- commit
- push
- create PR

Do not allow implementation agents to own this.

PR description should include:
- original task
- specification summary
- implementation plan summary
- changed files
- tests/checks
- reviewer result
- run ID

Do not merge automatically.

# Phase 11. GitHub Actions observation

Status: done, opt-in via `ci.enabled` (default false, and it requires
`pull_request.enabled`). Normalized CI evidence is persisted as `ci.json`.

Poll PR check status.

No webhook infrastructure required initially.

State:

```text
PR_CREATED
    ↓
CI_RUNNING
    ↓
PASS / FAIL
```

Classify failures:
- CODE_FAILURE
- TEST_FAILURE
- FLAKY_TEST
- INFRA_FAILURE
- DEPENDENCY_FAILURE
- UNKNOWN

Only appropriate failure types should cause code repair.

# Phase 12. CI repair

Status: done. Only `CODE_FAILURE`/`TEST_FAILURE` may trigger code repair; every
other category escalates to `NEEDS_HUMAN` with evidence. The CI budget is
separate from the pre-PR implementation budget and also caps PR update cycles.

Add bounded CI repair.

Initial maximum:

```text
3 attempts
```

Use relevant logs only.

Do not dump the entire historical CI context into the agent prompt.

Repeated failures eventually:

```text
NEEDS_HUMAN
```

# Phase 13. Local backlog daemon

Status: done, opt-in via `scheduler.enabled` (default false). GitHub is only
contacted when `factory start` runs with the scheduler enabled.

Only after manual flow is reliable.

Add:

```bash
factory start
```

Scheduler cycle:

```text
reconcile
   ↓
poll
   ↓
discover
   ↓
evaluate
   ↓
claim
   ↓
dispatch
```

Initial provider:

```text
GitHub Issues
```

Example eligibility:

```text
label = agent-ready
```

Initial concurrency:

```text
1
```

Polling should be configurable.

# Phase 14. Parallelism

Status: done. `scheduler.max_concurrent_tasks` is validated to be 1 or 2, work
is dispatched through a thread pool, and repository-global `git worktree`
administration is serialized under a per-source-repo lock.

After single-task scheduling is stable:

```text
max_concurrent_tasks = 2
```

Test:
- claiming
- workspace isolation
- reconciliation
- duplicate protection
- cancellation
- restart behavior

Do not jump directly to large concurrency.

# Phase 15. Delivery, operations and the deferred rest

Phase 15 is no longer one undifferentiated bucket. It is split into numbered
sub-phases so that "requested" and "deferred" are recorded per item instead of
per phase.

Requested and implemented: 15.0, 15.1, 15.2, 15.5, 15.11.

Deferred and unimplemented: 15.3, 15.4, 15.6, 15.7, 15.8, 15.9, 15.10, 15.12.

Nothing in the codebase may depend on a deferred item, and none of them may be
added without a documented, demonstrated need.

Rules that apply to every sub-phase below:

- the packaged configuration stays offline and opt-in; delivery must not enable
  any integration
- `--runtime fake` remains the default everywhere, including under launchd
- no sub-phase may introduce a database, a queue, a cloud service, a web
  framework or a JavaScript build step
- deployment is not part of delivery: the factory ships artifacts, it does not
  install, update or promote itself

## Phase 15.0. Factory CI for this repository

Status: done. `.github/workflows/ci.yml`.

Run this repository's own deterministic checks in GitHub Actions on push and
pull request.

Scope:
- Python 3.13 and 3.14, `uv sync --locked --group dev`
- parallel fast-feedback jobs for formatting/linting/type checking, tests and
  package verification
- `uv run --no-sync ruff format --check .`
- `uv run --no-sync ruff check .`
- strict mypy with the Pydantic plugin
- offline pytest with branch coverage and a 90% floor
- wheel/sdist metadata, contents, clean-install and CLI smoke checks
- pull-request and scheduled locked-environment dependency auditing, plus CodeQL
- CodeQL findings enforced from retained SARIF artifacts when GitHub Advanced
  Security is unavailable for the private repository
- weekly Python 3.15 prerelease compatibility coverage
- Dependabot updates for uv dependencies and SHA-pinned Actions, grouping only
  minor and patch updates while keeping major upgrades isolated
- native macOS runners for both target architectures: `macos-15` (arm64) and
  `macos-15-intel` (x86_64)
- no secrets, no model calls, no network-dependent tests

This is the factory's own CI. It is unrelated to `ci.enabled`, which is how a
run observes CI on a *target* repository.

### Acceptance criteria

- a pull request that breaks lint or tests reports a failing check (marking it
  *required* is a branch-protection setting, not a workflow one)
- a pull request that breaks formatting, typing, coverage or package integrity
  also reports a failing check
- the workflow completes with no repository secret configured
- the workflow fails if any job attempts a paid model call: the suite only uses
  `FakeAgentRuntime`, and `tests/conftest.py` blocks non-loopback sockets and
  direct execution of the real `gh`/`copilot` binaries process-wide
- both architecture jobs pass independently. To bound hosted-runner cost they
  run on `main` and on manual dispatch rather than on every pull request;
  release tags are owned by the release workflow and are not built twice
- a clean checkout passes with no manual step beyond `uv sync --locked --group dev`

## Phase 15.1. Tag-driven release (continuous delivery)

Status: done. `.github/workflows/release.yml` plus `scripts/release/`. The
acceptance criteria below are the ones only a real `v*` tag can demonstrate;
everything that can be checked without publishing (tag/version match, archive
shape, `INSTALL.txt` contents, offline smoke of the built executable, action
pinning, permissions, distribution validation and release-identity consistency)
is covered by `tests/test_release_workflows.py` and by the local
build-and-smoke procedure. Public-repository release artifacts receive GitHub
build-provenance attestations; private repositories require the GitHub plan
that supports artifact attestations.

CD here means *continuous delivery of versioned artifacts*: pushing a version
tag produces a downloadable, runnable macOS build attached to a GitHub Release.
It never deploys, installs, restarts or updates anything.

Triggered by a `v*` tag. Produces:

```text
software-agent-factory-<version>-macos-arm64.tar.gz
software-agent-factory-<version>-macos-x86_64.tar.gz
software_agent_factory-<version>-py3-none-any.whl
software_agent_factory-<version>.tar.gz
SHA256SUMS
build-info.json
```

`build-info.json` records at least: tag, commit SHA, build timestamp, runner
image, Python version, PyInstaller version and target architecture.

Artifacts are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are explicitly deferred, so release notes must state that macOS
Gatekeeper will quarantine a downloaded archive and must document the manual
step required to run it.

### Acceptance criteria

- pushing tag `vX.Y.Z` creates a GitHub Release containing every artifact above
- a release build fails if the tag does not match the packaged project version
- re-running the release workflow for an existing tag does not silently replace
  published artifacts
- `shasum -a 256 -c SHA256SUMS` succeeds against the downloaded archives
- the release job performs no deployment, no install and no `latest`-style
  mutable pointer that a client auto-follows
- release notes contain the Gatekeeper explanation and the unsigned-artifact
  warning
- no publishing to any package index

## Phase 15.2. macOS runtime packaging and the user launchd service

Status: done. `packaging/pyinstaller.spec`, `service_install.py`,
`factory service install|status|uninstall`. Native Intel execution and Rosetta
behavior can only be demonstrated on the matching hardware/runner.

### Packaging

PyInstaller `onedir` builds, one per native architecture, built on the matching
native runner. No `universal2`: the two builds stay separate, so a native wheel
that has no universal2 form cannot break the build and each archive stays
smaller.

The frozen runtime bundles Python and the factory only. It still requires an
external `git` on `PATH`. `gh` is a prerequisite only when
`pull_request.enabled`, `ci.enabled` or `scheduler.enabled` is true — the
backlog daemon polls GitHub Issues through `gh` — and `copilot` only when
`--runtime copilot` is used.

`factory doctor` reports missing prerequisites for *enabled* features and must
not demand `gh` or `copilot` from an offline default run. `factory run` and
`factory start` apply the same rule as a cheap `PATH`-only gate before doing
any work.

### launchd

A per-user LaunchAgent in `~/Library/LaunchAgents`, installed only by an
explicit CLI command. Never a root `LaunchDaemon`, never installed
automatically, never installed by the release archive.

The agent:
- defaults to `--runtime fake`
- captures an explicit `PATH` snapshot at install time, because launchd gives
  agents a minimal environment
- leaves launchd's own stdout/stderr at `/dev/null` and relies on the factory's
  bounded, rotating structured log under `<data_dir>/logs/factory.log`
  (Phase 15.5); a launchd-captured stdio file is never rotated and would grow
  without bound
- can be inspected and removed by CLI

### Acceptance criteria

- each archive extracts and runs `factory --version` and `factory run` (fake
  runtime) on a machine with no Python, no `uv` and no repository checkout
- the arm64 archive runs natively on Apple Silicon and the x86_64 archive runs
  under Rosetta 2; neither archive is universal2
- with `git` removed from `PATH`, the frozen binary fails with an explicit
  prerequisite error, not a traceback
- with `pull_request.enabled`, `ci.enabled` and `scheduler.enabled` all false,
  preflight passes without `gh` installed
- installing the service writes exactly one plist under
  `~/Library/LaunchAgents`, and nothing under `/Library`
- installation refuses unless the given configuration enables the scheduler and
  `factory doctor` reports no errors
- an installed service that is not explicitly reconfigured runs with
  `--runtime fake` and therefore spends no money
- uninstall removes the plist and unloads the agent, leaving runs and
  workspaces on disk
- installation never happens as a side effect of extracting, updating or
  running the factory

## Phase 15.3. Staging environment

Status: deferred. Not implemented, not designed, nothing depends on it.

## Phase 15.4. Deployment / promotion

Status: deferred. The factory delivers artifacts; it does not deploy software,
its own or anyone else's. Autonomous deployment stays banned.

## Phase 15.5. Local monitoring and health

Status: done. `observability.py`, `doctor.py`, `factory status`,
`factory doctor`.

Local only. Structured JSON logs on disk, plus metrics computed from persisted
run artifacts. No cloud observability backend, no exporter, no telemetry
egress.

`factory status` and the dashboard derive stale work-item locks, orphaned
worktrees and non-terminal runs left by a dead process from the filesystem run
store. Environment/configuration prerequisites are reported separately by
`factory doctor`, and launchd registration by `factory service status`; the
dashboard does not execute those subprocess checks every five seconds.

Metrics are pure functions over the run store: runs by final state, first-pass
success rate, attempts per run, replans, CI repairs, escalations to
`NEEDS_HUMAN`, and durations. Token usage and cost are reported only when the
runtime actually returned them; a missing value is reported as unknown and
never as zero or an estimate. No runtime reports usage today and
`AttemptRecord` persists none, so no usage or cost figure is reported at all
(ADR-017); adding one starts with a typed field on `AttemptRecord`.

### Acceptance criteria

- metrics functions are unit tested against a fixture run store with no
  network, no model call and no clock dependence beyond persisted timestamps
- the same run store always yields the same metrics (pure, recomputable)
- no metric is derived from a hard-coded price table or invented token count
- a run whose runtime reported no usage is counted in run metrics and excluded
  from usage metrics; while no runtime reports usage, no usage metric exists at
  all rather than a zeroed one
- health checks report a stale lock, an orphaned worktree and an abandoned
  non-terminal run as distinct, actionable findings
- health and metrics never mutate a run, a workspace or configuration
- log files stay inside the configured data directory and are bounded in size
- credentials are redacted from structured logs, as they already are from
  command output

## Phase 15.6. Docker sandboxed execution

Status: deferred.

## Phase 15.7. Remote workers

Status: deferred.

## Phase 15.8. Postgres run store

Status: deferred. Filesystem persistence remains the source of truth.

## Phase 15.9. Temporal or another durable workflow engine

Status: deferred. `WorkflowController` remains the only authority.

## Phase 15.10. Jira and other trackers

Status: deferred. GitHub Issues remains the only provider.

## Phase 15.11. Read-only local dashboard

Status: done. `dashboard/` plus `factory dashboard`.

This is an explicitly requested exception to the "no web dashboard" rule in
`AGENTS.md`, recorded in ADR-016. The general ban still applies to everything
else.

Constraints:
- binds `127.0.0.1` only, started by an explicit command, disabled by default
- read-only: `GET` only, no endpoint mutates runs, configuration or state
- protected by a token generated per start
- Python standard library only: no web framework, no npm, no bundler, no build
  step, no JavaScript dependency to install
- renders run list, run detail, workflow state, attempt history and the Phase
  15.5 metrics
- does not render command logs or diffs by default

### Acceptance criteria

- the listening socket is bound to loopback; a request to a non-loopback
  address of the host is refused
- a request without the token is rejected with 401/403 and reveals no run data
- the token is not written to a world-readable location and is not logged
- every non-`GET` method returns 405
- there is no route that writes to the run store, the workspace root or the
  configuration
- starting the dashboard requires an explicit command and never happens as a
  side effect of `factory run` or `factory start`
- `package.json`, `node_modules` and any bundler config are absent from the
  repository
- run detail pages contain no command log output and no patch content by
  default
- the dashboard is tested with the stdlib test client against a fixture run
  store, with no browser and no network

## Phase 15.12. Kubernetes workers

Status: deferred.

# Phase 16. Repository capability layer and bounded post-green polish

Status: done. The controller profiles each prepared worktree before
`TRIAGING`, persists `repository-profile.json`, and optionally schedules one
research-grounded post-green `IMPLEMENTER` pass. There is no fixed built-in
skill catalog: repository guidance comes from two separate artifacts with
different trust — reusable guidance generated by the configured Researcher, and
an optional human-written overlay — both stored outside the target repository
under `factory.data_dir`.

Repository profiling is deterministic and read-only:

- scan repository-local paths and allowlisted manifests only
- do not execute commands, import target code or contact the network
- record technologies, test tools, package managers, markers, warnings,
  `version_files`, and exact dependency evidence: direct declarations with
  ecosystem, name, declared version, optional exact resolved
  version/resolution path, manifest path and dependency group
- parse declarations from `pyproject.toml` (PEP 621 `project.dependencies`,
  `project.optional-dependencies.*`, `dependency-groups.*` and
  `requires-python` as the `python` runtime target, plus the Poetry dependency,
  dev-dependency and group tables), `requirements.txt`/`requirements-*.txt`
  (marking the `pip` package manager) and `package.json`
  (`dependencies`, `devDependencies`, `peerDependencies`,
  `optionalDependencies`, `packageManager`); read `setup.cfg`/`tox.ini` for
  Python and pytest evidence only
- resolve exact versions from `uv.lock`, `package-lock.json` and
  `pnpm-lock.yaml` when unambiguous and warn instead of guessing when they are
  not; detect the package manager from `poetry.lock`, `yarn.lock` and
  `bun.lock`/`bun.lockb` and fingerprint those (plus `Pipfile.lock` and
  `pylock.toml`) as version files without claiming exact graph parsing
- record two distinct SHA-256 fingerprints: the semantic
  `dependency_fingerprint` over technologies, test tools, package managers and
  normalized dependency declarations, which binds generated guidance, and the
  `manifest_fingerprint` over version-file content, which is provenance only

When `polish.enabled` and the bounded polish attempt is eligible, after the
first successful deterministic verification and scope assessment the
controller re-profiles the post-implementation worktree (capturing any
dependency upgrades the task made) and looks for reusable generated guidance
for that repository and `dependency_fingerprint`. Generated skills live under
`factory.data_dir` in repository-scoped storage keyed by the canonical local
repository identity and the fingerprint, following the template
`<data_dir>/repository-skills/v1/<repository-key>/...`. They are never stored
in, or auto-loaded from, the target repository or its worktree.

Reuse is the normal path:

- when a generated skill exists for the current `dependency_fingerprint`, the
  run loads and reuses it and performs no research
- generation happens only when the current fingerprint has no generated skill
- an existing generated file is never overwritten
- every load revalidates in full: schema, agreement with the current profile,
  and every cited source against the configured allowlists
- a changed dependency fingerprint selects a new generated file; earlier files
  remain on disk
- there is no TTL and no time-based expiry
- stored guidance that fails revalidation is left on disk exactly as written,
  polish is skipped, and the warning directs the operator to
  `factory skill refresh`

Reuse bounds research per fingerprint, not per process. Two truly concurrent
first runs for the same missing fingerprint may each make one bounded
Researcher call; publication is atomic and no-clobber, so one winner is kept,
the losing run loads that winner, and both revalidate it in full before use.
The race costs at most one extra research call and cannot affect correctness,
stored state or the overlay.

Repository identity is the canonical local Git common directory. Linked
worktrees of one checkout share a skill directory, no remote URL is consulted,
and moving or re-cloning a repository selects a new key with no guidance: the
factory neither follows the move nor deletes the old directory, and
`factory skill path` is how an operator finds the directory to move, copy or
recreate.

When generation is required, the controller transitions through a temporary
`RESEARCHING` state and invokes the configured Researcher (`GPT-5.6 Sol` by
default) with purpose `GENERATE_REPOSITORY_SKILL`, at most once per run.

That call is bounded, web-only and repository-wide rather than task-scoped:

- it runs in the neutral run directory, not the worktree
- `web_fetch` is its only tool, and repository custom instructions are disabled
- it sees the normalized profile, the configured URL lists and the
  factory-owned generation rules — never changed filenames, source code,
  README content, task prose or the diff
- `polish.official_documentation_origins` (defaults: official pytest, Python,
  Node.js, Python Packaging Authority, React, Testing Library, Vite, Vitest and
  TypeScript documentation) is authoritative. Official documentation, migration
  guides and release notes decide every version claim, and operators may extend
  the list with other official origins.
- `polish.practice_reference_urls` holds exact curated general-practice
  references (by default reviewed `bdfinst/agentic-dev-team` notes, pinned to
  commit `52cc5efd1c445e71c55b956837c003911346d7e7` rather than a mutable
  branch). They may contribute generic heuristics only, synthesized rather than
  copied, and never version claims, commands, tools or orchestration.

It returns one typed `RepositorySkill`, persisted as `repository-skill.json`,
bound to the profile's `dependency_fingerprint` and containing bounded targets
with evidence paths, official and practice sources that each declare which
detected dependencies they ground (`applies_to`, with the generic marker
`repository` reserved for practice sources), separate `simplify` and `polish`
guidance, and uncertainties. The type refuses a skill with neither an official
source nor an explicit uncertainty, and refuses a generic official source.

The controller validates the artifact deterministically and rejects a
fingerprint mismatch, a target that is not an exact profiled dependency
declaration, evidence paths outside the profile, a missing target or missing
per-dependency official provenance for a detected `python`, `pytest`, `react`,
`react-dom`, `vite` or `vitest` dependency, a source claiming applicability to
an undetected dependency, or a source outside the two configured lists.

Because polish only ever runs on an already-green change, none of this can fail
the run. A failed re-profile, failed research, rejected skill, or a skill that
became stale (the `dependency_fingerprint` changed after it was loaded) appends
the reason to the persisted profile's `warnings` and safely skips polish or
disables the guidance for the Tester and Reviewer. The verified change proceeds.

Human customization is a separate repository-level
`repository-skill-overlay.yaml`, stored in the same repository-scoped storage
outside the target repository. It carries guidance prose only: `mode:
extend|replace` plus optional `simplify` and `polish` `SkillGuidance` blocks.
It declares no targets, sources, versions or fingerprints, so it survives
dependency changes and keeps applying after a new generated file is selected.
The factory never creates, rewrites, normalizes, refreshes or deletes it. An
invalid overlay is preserved exactly as written, recorded as a warning and
ignored for that run, while valid generated guidance may still apply.

`factory skill path --repo PATH` discovers the generated and overlay paths, and
`factory skill validate --repo PATH` validates the current files; both are
read-only. `factory skill refresh --repo PATH [--runtime fake|copilot]`
explicitly refreshes generated guidance only and never touches the overlay.
The read-only dashboard gains no skill or overlay write path.

Before agents consume guidance, each run stores create-once snapshots:
`repository-skill.json` (the effective guidance used),
`repository-skill-overlay.json` (the overlay exactly as read, when valid) and
`repository-skill-use.json` (provenance: repository key, dependency
fingerprint, selection source, overlay mode and whether it applied, and content
hashes rather than guidance text). Human edits made while a run is in flight
affect later runs only.

Simplify is applied first and must preserve tests, behavior, public
interfaces, security and error handling; polish is applied second with
version-specific practices. Both execute within the same existing single
`POLISH` Implementer attempt, then full deterministic verification and scope
assessment run again. The effective guidance reaches only the polish
Implementer, Tester and Reviewer, and is never available before the initial
green baseline. There is no plugin system: a repository cannot supply skill
definitions, and a dependency version change is what causes new research.
The fake runtime generates deterministic offline guidance for this step and
tests make no paid calls.

`polish.enabled` still controls one optional pass after the first successful
deterministic verification and initial scope assessment, but before testing
and review. It uses the existing Implementer and worker routing, records
`AttemptTrigger.POLISH`, consumes the implementation budget, may make no edits
and is always followed by another deterministic verification and scope
assessment. It never runs during CI repair, never runs more than once and runs
only when one later recovery attempt would still remain. There is no
`POLISHING` state or `POLISHER` role, and no new role was added for research;
`RESEARCHING` is a temporary transition that resolves to either `PLANNING`
(initial triage-driven research) or `IMPLEMENTING` (skill research before
polish).

The class fallback is `false` for legacy configurations that omit `polish`.
The packaged default and `config/factory.example.yaml` set it to `true`, so a
normal packaged fake run has two implementation attempts: the initial pass and
the bounded polish pass.

## Acceptance criteria

- profiling happens after workspace preparation and before triage, and again
  before an eligible bounded polish attempt
- `repository-profile.json` is versioned, persisted for every run, and
  includes both fingerprints, version files and exact dependency declarations
- malformed or oversized allowlisted manifests/lockfiles produce warnings,
  not execution
- generated skills are stored under `factory.data_dir` in repository-scoped
  storage keyed by the canonical local repository identity and
  `dependency_fingerprint`, never inside the target repository or its worktree,
  and are never auto-loaded from the target repository
- a run with a valid generated skill for the current fingerprint reuses it and
  makes no research call; generation happens only when that fingerprint has no
  generated skill; an existing generated file is never overwritten; a changed
  fingerprint selects a new file while earlier files remain; there is no TTL
- concurrent first runs for the same missing fingerprint may each make one
  bounded Researcher call; atomic no-clobber publication keeps one winner that
  every participant revalidates, so the race costs at most one extra call and
  never affects stored state, the overlay or which guidance is used
- the repository key derives from the canonical local Git common directory, so
  linked worktrees share one directory and a moved or re-cloned repository
  selects a new key rather than silently reusing or deleting old guidance
- schema, profile-agreement and source validation run on every load, not only
  at generation time
- the skill research call receives only normalized profile data and the
  configured source lists — never changed filenames, source code, README
  content, task prose or the diff — runs in the neutral run directory, and has
  no filesystem/shell/edit tools, only `web_fetch` restricted to
  `polish.official_documentation_origins` and `polish.practice_reference_urls`
- the controller rejects a `dependency_fingerprint` mismatch, unverified
  dependency targets/evidence, missing required targets or provenance, and
  sources outside the configured lists
- `repository-skill-overlay.yaml` is a repository-level, human-owned file
  outside the target repository that carries guidance prose only
  (`mode: extend|replace`, optional simplify/polish guidance) with no targets,
  sources, versions or fingerprints; it survives dependency changes; the
  factory never creates, rewrites, normalizes, refreshes or deletes it; an
  invalid overlay is preserved, warned about and ignored while valid generated
  guidance may still apply
- `factory skill path --repo PATH` and `factory skill validate --repo PATH` are
  read-only, and `factory skill refresh --repo PATH [--runtime fake|copilot]`
  refreshes generated guidance only and never touches the overlay; the
  dashboard exposes no skill or overlay write path
- each run persists immutable snapshots of the effective skill, the overlay as
  read when valid, and usage/provenance metadata before agents consume the
  guidance, so mid-run human edits affect later runs only
- research, profiling, validation and staleness failures record a warning on
  `repository-profile.json` and skip or disable polish; they never fail or
  escalate an already-green run
- the effective guidance reaches only the polish Implementer, Tester and
  Reviewer, never the initial (pre-polish) attempt
- the single polish attempt simplifies first and polishes second, and the post-
  green pass is bounded, fully reverified and excluded from CI repair
- existing workflow states, tools, models, gates, commands, retry budgets,
  permissions, dependencies and scope are unchanged apart from the temporary
  `RESEARCHING → IMPLEMENTING` transition

# Phase 17. Project brief decomposition and bounded execution

Status: done.

`factory project` accepts a high-level project description, optional acceptance
criteria and constraints, and invokes the configured Planner once with purpose
`DECOMPOSE_PROJECT`. The result is a typed `ProjectPlan` with one to twelve
tasks. Task ids are contiguous and dependencies may reference earlier ids only,
which provides a deterministic DAG without a workflow framework.

The planner is instructed to choose the fastest sufficient solution:

- default to one coherent work item
- split only for independently verifiable outcomes, hard prerequisites, safe
  parallel execution, or an existing scope limit
- reuse existing mechanisms
- avoid speculative abstractions, dependencies, services, infrastructure,
  cleanup and future-proofing
- keep implementation, tests and directly related documentation in the same
  task

Project artifacts are persisted under
`<data_dir>/projects/<project-id>/`. Dependency-ready tasks execute in bounded
waves using `scheduler.max_concurrent_tasks` (`1` or `2`) and each task runs
through the existing `WorkflowController`. A project integration worktree
composes successful child commits before downstream tasks start. Conflicts,
failed child runs and approval gates stop with a persisted failure rather than
creating more work. After all commits are composed, the configured repository
commands run once more against the integration branch before the project can be
`DONE`. A later invocation reconciles abandoned `PLANNING` or `RUNNING`
executions to `NEEDS_HUMAN`.

`--github-repo OWNER/NAME` optionally creates one issue per validated task and
closes it after successful integration. Generated issues deliberately omit the
scheduler's `agent-ready` label so the local project runner remains the sole
execution owner and duplicate dispatch is impossible.

No new model role, dependency, database, generic DAG engine, recursive task
tree, unbounded replanning loop, autonomous merge or deployment path is added.
Project execution currently requires child PR and CI publication to be disabled
and produces one completed local integration branch.

## Acceptance criteria

- a broad brief produces a persisted typed project plan
- one task is valid and is the fake runtime default
- invalid ids, forward dependencies and duplicate task titles are rejected
- dependency-ready work runs with bounded concurrency
- successful child commits compose onto one integration branch
- the fully composed integration branch passes deterministic verification
- failed or human-blocked work stops descendants
- abandoned active executions are reconciled to a terminal state
- GitHub issue creation is explicit and optional
- generated issues are not automatically eligible for the backlog daemon
- all normal tests remain offline and deterministic

# First useful end-to-end demo

This milestone is reached. With the packaged configuration the demo runs
entirely locally and ends at `PR_READY`.

Target:

```bash
factory run \
  --repo ~/projects/sample-app \
  --title "Reject empty customer names" \
  --description "The API should reject empty or whitespace-only customer names with HTTP 400."
```

Expected:

```text
TRIAGE

complexity: L1
risk: R1
research: no

REFINE

acceptance criteria:
  ✓ empty rejected
  ✓ whitespace-only rejected
  ✓ valid requests unaffected

PLAN

plan produced

IMPLEMENT

model: Claude Sonnet 5
files changed: N

VERIFY

lint ✓
typecheck ✓
tests ✓
build ✓

POLISH

one bounded implementer pass

VERIFY AGAIN

lint ✓
typecheck ✓
tests ✓
build ✓

TESTER

✓

REVIEW

GPT-5.6 Sol
approved ✓

RESULT

PR_READY
```

That is the first major product milestone.

Not autonomous production deployment.
