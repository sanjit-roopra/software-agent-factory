# Architecture

## Objective

Build a local autonomous software engineering factory that can eventually process backlog work through the entire SDLC.

V1 focuses only on:

```text
manual task
   ↓
triage
   ↓
refine
   ↓
plan
   ↓
implement
   ↓
verify
   ↓
review
   ↓
ready for PR
```

Later phases introduce:
- PR
- CI observation
- repair
- backlog polling
- prioritization
- staging
- deployment

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
attempt
workspace_path
branch_name
created_at
updated_at
completed_at
failure_reason
pull_request_url
```

## Workflow states

Initial detailed SDLC states:

```text
CREATED
TRIAGING
REFINING
RESEARCHING
PLANNING
PLAN_READY
IMPLEMENTING
VERIFYING
REVIEWING
PR_READY
PR_CREATED
CI_RUNNING
CI_DIAGNOSIS
REPAIRING
DONE
BLOCKED
NEEDS_HUMAN
FAILED
```

Not every state must be implemented in Phase 1.

The workflow controller owns transitions.

## Scheduling state

Scheduling ownership is separate from detailed SDLC state.

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

The actual Git diff is stored separately as evidence.

### VerificationReport

Fields approximately:

```text
passed
deterministic_checks
failures
coverage_change
test_findings
confidence
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

### Tester
Model: `Claude Sonnet 5`

Receives:
- Specification
- ExecutionPlan
- actual diff
- repository

Do not provide implementer's self-assessment unless explicitly necessary.

Output: `VerificationReport`

### Reviewer
Model: `GPT-5.6 Sol`

Receives:
- Specification
- ExecutionPlan
- diff
- deterministic test results

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
maximum total implementation attempts: 4
review repair attempts: 2
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
│       ├── triage.json
│       ├── specification.json
│       ├── research.json
│       ├── execution-plan.json
│       ├── change-set.json
│       ├── patch.diff
│       ├── verification.json
│       ├── review.json
│       └── logs/
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

Writes should be atomic where practical.

## Workspace abstraction

Provide something conceptually like `WorkspaceProvider`.

Operations:
- prepare()
- get_path()
- diff()
- cleanup()

Initial implementation: `GitWorktreeWorkspace`

Do not build generic remote-worker abstractions yet.

## Agent runtime abstraction

Conceptually:

```text
AgentRuntime.run(
    role,
    model,
    reasoning,
    instructions,
    context
) -> AgentResult
```

Initial production runtime: `CopilotAgentRuntime`

Tests use: `FakeAgentRuntime`

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

Only after they pass should independent AI verification/review occur.

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

Unexpected scope should cause `REPLAN` or `NEEDS_HUMAN` depending on risk.

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

No dashboard initially.

JSON + structured logs are sufficient.

## Long-term architecture

The current abstractions should permit later addition of:
- GitHub Issues polling
- Jira
- parallel runs
- PR lifecycle
- GitHub Actions
- repair loops
- staging
- deployment
- Postgres
- remote workers
- Kubernetes
- dashboard

Do not implement those merely to prove future compatibility.
