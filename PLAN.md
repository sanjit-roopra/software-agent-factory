# Implementation Plan

## Status

Phases 0-14 are implemented and integrated.

Phase 15 is explicitly NOT implemented and remains optional.

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
| 15 | Later integrations | NOT implemented, optional |

Every integration is disabled by default: with the packaged configuration
`factory run` performs no network access, makes no paid model call
(`--runtime fake` is the default) and finishes at `PR_READY`.

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

# Phase 15. Later integrations

Status: NOT implemented, and deliberately optional. Nothing in the codebase
depends on any of these, and none may be added without a documented need.

Only after actual usage demonstrates need:
- Jira
- dashboard
- Postgres
- Temporal
- remote workers
- Docker sandbox
- Kubernetes workers
- staging
- deployment
- production monitoring

These are explicitly NOT part of the initial build.

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
