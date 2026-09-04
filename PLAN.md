# Implementation Plan

## Status

Phases 0-14 are implemented and integrated.

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
- Python 3.13, `uv sync --group dev`
- `uv run ruff check .`
- `uv run pytest`
- native macOS runners for both target architectures: `macos-15` (arm64) and
  `macos-15-intel` (x86_64)
- no secrets, no model calls, no network-dependent tests

This is the factory's own CI. It is unrelated to `ci.enabled`, which is how a
run observes CI on a *target* repository.

### Acceptance criteria

- a pull request that breaks lint or tests reports a failing check (marking it
  *required* is a branch-protection setting, not a workflow one)
- the workflow completes with no repository secret configured
- the workflow fails if any job attempts a paid model call: the suite only uses
  `FakeAgentRuntime`, and `tests/conftest.py` blocks non-loopback sockets and
  direct execution of the real `gh`/`copilot` binaries process-wide
- both architecture jobs pass independently. To bound hosted-runner cost they
  run on `main`, on release tags and on manual dispatch rather than on every
  pull request, which relies on the Ubuntu job
- a clean checkout passes with no manual step beyond `uv sync --group dev`

## Phase 15.1. Tag-driven release (continuous delivery)

Status: done. `.github/workflows/release.yml` plus `scripts/release/`. The
acceptance criteria below are the ones only a real `v*` tag can demonstrate;
everything that can be checked without publishing (tag/version match, archive
shape, `INSTALL.txt` contents, offline smoke of the built executable, action
pinning, permissions) is covered by `tests/test_release_workflows.py` and by
the local build-and-smoke procedure.

CD here means *continuous delivery of immutable artifacts*: pushing a version
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
