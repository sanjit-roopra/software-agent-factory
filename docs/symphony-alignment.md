# OpenAI Symphony Alignment

## Purpose

OpenAI Symphony is the primary orchestration reference for this project.

We are NOT attempting to reproduce Symphony exactly.

We are also NOT building a Codex-specific factory.

The goal is:

```text
Symphony orchestration principles
              +
specialized multi-model SDLC pipeline
```

# Symphony concepts to preserve

## 1. One authoritative orchestrator

The orchestrator owns task execution state.

Agents/workers produce results.

Workers must not become independent workflow controllers.

```text
tracker
   │
   ▼
orchestrator
   │
┌──┴───┐
▼      ▼
worker worker
│      │
└──┬───┘
   ▼
outcome
   │
   ▼
orchestrator
```

## 2. Reconciliation before dispatch

Every scheduler cycle should conceptually follow:

```text
scheduler tick
     │
     ▼
reconcile currently known work
     │
     ▼
discover candidates
     │
     ▼
evaluate eligibility
     │
     ▼
sort/prioritize
     │
     ▼
claim
     │
     ▼
dispatch while capacity exists
```

Do not discover tasks and blindly start workers before reconciling existing work.

## 3. Polling first

The local MacBook implementation should not require inbound webhooks.

Eventually:

```bash
factory start
```

can periodically ask GitHub whether there are issues labelled `agent-ready`.

Potential initial default:

```text
30 second polling interval
```

Manual execution remains available.

## 4. Claim before dispatch

An eligible item must be claimed before work starts.

```text
candidate
   ↓
 claim
   ↓
ensure not already active
   ↓
dispatch
```

Even while the factory is single-process, model this concept explicitly.

It will matter when parallel execution is introduced.

## 5. Deterministic per-task workspaces

Each task receives a stable workspace.

Example:

```text
~/.software-factory/workspaces/TASK-123/
```

For Git repositories, V1 should prefer Git worktrees.

Agent turns and retries operate against the same workspace.

Do not recreate the repository for every agent step.

## 6. Workspace lifecycle

```text
task claimed
    ↓
create/restore workspace
    ↓
execute
    ↓
task remains active
    ↓
preserve workspace
```

When terminal (`DONE`, `FAILED`, `CANCELLED`) the workspace may be cleaned according to policy.

For debugging, run artifacts should remain.

## 7. Recovery using tracker + filesystem

Do not require a workflow database for the local V1.

Durable information is primarily:

```text
tracker/manual WorkItem
          +
run artifact directory
          +
Git workspace
```

Example:

```text
~/.software-factory/
├── runs/
│   └── RUN-ID/
│       ├── run.json
│       ├── specification.json
│       └── ...
└── workspaces/
    └── TASK-123/
```

After process restart:

```text
discover runs
   ↓
inspect workspaces
   ↓
compare status
   ↓
reconcile safely
```

Do not delete uncertain work automatically.

## 8. Explicit retry scheduling

Retries are represented as state.

Do not hide retry loops inside agents.

```text
RUNNING
   │ failure
   ▼
RETRY_QUEUED
   │ backoff / policy
   ▼
eligible again
```

Persist:
- previous attempt
- reason
- previous model
- next eligible time if applicable

Implementation repair escalation is layered on top of this.

## 9. Bounded concurrency

The orchestrator owns concurrency.

Initial setting:

```text
max_concurrent_tasks: 1
```

Later:

```text
max_concurrent_tasks: 2
```

or more.

Agents must not spawn uncontrolled autonomous job trees.

## 10. Stall detection

A worker may hang without cleanly failing.

Track:

```text
started_at
last_activity_at
```

Agent/tool events update activity.

After a configurable timeout:

```text
worker considered stalled
   ↓
stop execution
   ↓
record reason
   ↓
retry or escalate
```

V1 can implement this simply.

Do not build complex distributed heartbeats.

## 11. Separation of layers

### Policy
Defines:
- retries
- risk rules
- model routing
- command permissions
- validation requirements
- PR behavior

### Configuration
Loads:
- repository configuration
- models
- limits
- paths
- timeouts

### Coordination
Owns:
- scheduler
- claims
- workflow
- reconciliation
- retries
- concurrency

No LLM intelligence belongs here.

### Execution
Owns:
- workspaces
- shell
- Git
- agent invocation
- local checks

### Integration
Adapters for:
- Copilot
- GitHub
- later Jira

### Observability
Initially:
- structured logs
- run artifacts
- status CLI
- model invocation telemetry

# Where this project intentionally extends Symphony

## Explicit SDLC roles

```text
TRIAGE
   ↓
REFINE
   ↓
RESEARCH optional
   ↓
PLAN
   ↓
IMPLEMENT
   ↓
VERIFY
   ↓
REVIEW
   ↓
PR
   ↓
CI
```

## Multi-model execution

```text
triage
  Sonnet 5

refine
  Opus 5

research
  GPT-5.6 Sol

plan
  Opus 5

implement L0
  MAI-Code-1.1-Flash

implement L1
  Sonnet 5

implement L2/L3
  Opus 5

test
  Sonnet 5

review
  GPT-5.6 Sol

investigate
  Opus 5
```

## Typed artifacts

```text
WorkItem
   ↓
TriageResult
   ↓
Specification
   ↓
ResearchReport?
   ↓
ExecutionPlan
   ↓
ChangeSet
   ↓
VerificationReport
   ↓
ReviewReport
```

Do not pass one continuous conversation between roles.

## Risk and complexity

Complexity (`L0`..`L3`) controls model strength.

Risk (`R0`..`R3`) controls governance.

These dimensions must remain independent.

## Independent review

```text
deterministic checks
      ↓
   tester
      ↓
independent reviewer
```

The reviewer should ideally use a different model family.

## Deterministic quality gates

Examples:
- changed files
- diff scope
- dependency changes
- lint
- typecheck
- tests
- build
- security scans
- GitHub Actions

These signals should be authoritative where applicable.

# Summary

Use Symphony to avoid reinventing:

```text
scheduling
reconciliation
claiming
retry semantics
workspace lifecycle
polling
concurrency
```

Build our differentiation in:

```text
SDLC decomposition
model specialization
typed evidence
risk governance
deterministic quality
independent verification
```
