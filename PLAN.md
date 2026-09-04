# Implementation Plan

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

# Phase 1. Domain + fake workflow

Implement:
- WorkItem
- FactoryRun
- WorkflowState

Typed artifacts:
- TriageResult
- Specification
- ResearchReport
- ExecutionPlan
- ChangeSet
- VerificationReport
- ReviewReport

Implement:
- workflow transitions
- FileRunStore
- ModelRouter
- simple retry policy
- FakeAgentRuntime

CLI:
- factory run
- factory runs
- factory show

For Phase 1:

```bash
factory run \
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

using fake agents.

Persist artifacts under:

```text
~/.software-factory/runs/
```

## Acceptance criteria

- all workflow transitions tested
- invalid transitions rejected
- artifacts serialize to JSON
- run survives process termination as inspectable files
- retry counts persist
- no network access required
- no LLM access required
- `uv run pytest` passes

Stop after completing this phase and inspect the design.

# Phase 2. Real Git workspaces

Implement Git worktree support.

Input:

```text
--repo /path/to/repository
```

When a run starts:

```text
source repository
     ↓
create branch
     ↓
create worktree
     ↓
execute against isolated worktree
```

Branch naming:

```text
factory/<task-id>
```

Implement:
- prepare workspace
- locate workspace
- collect changed files
- collect Git diff
- cleanup policy

Fake implementation agent may make a deterministic sample modification for tests.

## Acceptance criteria

- source working tree remains untouched
- each run gets isolated workspace
- diff can be collected
- interrupted workspace remains recoverable
- duplicate task does not accidentally create conflicting active workspace
- tests cover workspace lifecycle

Stop and review.

# Phase 3. Real Copilot Planner

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

# Phase 4. Real Specification + Triage

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

# Phase 5. Real Implementer

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

# Phase 6. Deterministic local verification

Implement repository configuration.

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

Run these automatically after implementation.

Capture:
- command
- exit code
- stdout/stderr reference
- duration

Failure returns workflow to bounded implementation repair.

## Acceptance criteria

Broken code cannot reach `REVIEWING` while required deterministic checks fail.

# Phase 7. Tester + Reviewer

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

# Phase 8. Complexity routing

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

Implement deterministic ModelRouter.

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

# Phase 9. Research

Enable optional Researcher:

```text
GPT-5.6 Sol
```

Invoke only if triage/planning determines research is necessary.

Output: `ResearchReport`

Do not make every task pay for a research step.

# Phase 10. Scope drift

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

# Phase 11. Pull request creation

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

# Phase 12. GitHub Actions observation

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

# Phase 13. CI repair

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

# Phase 14. Local backlog daemon

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

# Phase 15. Parallelism

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

# Phase 16. Later integrations

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
