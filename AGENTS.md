# Software Agent Factory. Agent Instructions

## What this repository is

This repository implements a local-first autonomous software engineering factory.

The factory takes software work through:

```text
Work Item
    ↓
  Triage
    ↓
  Refine
    ↓
 Research if needed
    ↓
   Plan
    ↓
 Implement
    ↓
  Verify
    ↓
  Review
    ↓
    PR
    ↓
    CI
    ↓
 Repair if needed
    ↓
   Done
```

The initial version runs on a developer MacBook.

The LLMs run remotely through GitHub Copilot.

Repository work, shell commands, Git worktrees, tests, builds and orchestration run locally.

## Core principle

**LLMs provide intelligence. The factory provides authority.**

Agents may:
- understand work
- refine requirements
- research
- plan
- edit source code
- write tests
- run commands
- review
- diagnose failures

Agents do NOT control:
- workflow state
- retry budgets
- model routing policy
- quality gates
- task ownership
- branch protection
- merging
- deployment policy
- production credentials
- whether their own output is accepted

Those decisions belong to deterministic factory code.

## Architectural baseline

OpenAI Symphony is the primary orchestration reference for this project.

Before making substantial orchestration changes, read:

`docs/symphony-alignment.md`

We intentionally reuse Symphony concepts for:
- polling
- reconciliation
- task claiming
- scheduler ownership
- bounded concurrency
- deterministic per-task workspaces
- retries
- stall detection
- workspace lifecycle
- tracker/filesystem-driven recovery

We extend Symphony with:
- explicit SDLC stages
- multiple specialized agents
- multiple models
- complexity-based model routing
- risk-based governance
- typed artifacts between agents
- independent testing and review
- deterministic quality gates

Do not invent a fundamentally different orchestration model without documenting why.

## Important implementation rules

### 1. One authoritative workflow controller
Only the workflow controller may transition a FactoryRun between states.

Agents return artifacts and outcomes.

They do not mutate orchestration state directly.

### 2. Agents communicate using typed artifacts
Do not pass one giant conversation between agents.

Use:

```text
WorkItem
  ↓
TriageResult
  ↓
Specification
  ↓
ResearchReport if required
  ↓
ExecutionPlan
  ↓
ChangeSet
  ↓
VerificationReport
  ↓
ReviewReport
```

Persist these artifacts.

Each agent receives only the context needed for its job.

### 3. A model does not approve its own work
The implementer's success claim is not a quality gate.

Use deterministic validation first.

Then use an independent Tester and Reviewer.

Prefer a different model family for final review.

### 4. Retries are bounded
Never implement an unlimited retry loop.

All retries must have explicit limits and recorded reasons.

### 5. Complexity and risk are separate concepts
Complexity selects model strength.

Risk selects governance and required validation.

A trivial change may be high risk.

A difficult change may be low operational risk.

### 6. Prefer deterministic checks
If something can be checked programmatically, check it programmatically.

Examples:
- Git diff
- changed files
- unexpected modules
- new dependencies
- lint
- formatting
- type checking
- unit tests
- integration tests
- build
- security scanners
- CI checks

LLM judgement supplements deterministic evidence.

It does not replace it.

### 7. Keep V1 small
Do not introduce unless explicitly required:
- Temporal
- PostgreSQL
- SQLite
- Redis
- Kafka
- Kubernetes
- cloud infrastructure
- web dashboard
- Jira
- Slack
- Teams
- vector database
- long-term semantic memory
- agent swarm
- distributed workers
- autonomous deployment
- autonomous merge
- complex plugin architecture

The first version uses filesystem persistence.

## Initial technologies
Prefer:
- Python 3.13+
- uv
- Pydantic v2
- Typer
- pytest
- Ruff
- Pyright or mypy
- Git CLI
- Git worktrees
- filesystem JSON persistence
- GitHub Copilot for agents
- GitHub CLI/API when PR support is introduced

Avoid large frameworks unless they solve a real demonstrated requirement.

Do not introduce LangGraph.

## Initial model roles
Configuration must remain outside application code.

Initial desired routing:

```text
Triage
  Claude Sonnet 5

Specification Refiner
  Claude Opus 5

Researcher
  GPT-5.6 Sol

Planner
  Claude Opus 5

L0 Worker
  MAI-Code-1.1-Flash

L1 Worker
  Claude Sonnet 5

L2 Worker
  Claude Opus 5

L3 Worker
  Claude Opus 5

Tester
  Claude Sonnet 5

Reviewer
  GPT-5.6 Sol

Failure Investigator
  Claude Opus 5
```

Do not scatter literal model names through the source.

## Before coding
Read in this order:
1. `AGENTS.md`
2. `docs/architecture.md`
3. `docs/symphony-alignment.md`
4. `PLAN.md`

Then implement only the currently requested phase.

Do not automatically continue into later phases.

## Quality expectations
Prefer:
- explicit domain concepts
- strong typing
- small cohesive modules
- simple functions
- straightforward control flow
- testability
- dependency inversion only where useful
- structured logs
- descriptive errors

Avoid:
- god classes
- generic Manager objects
- unnecessary inheritance
- enormous prompts
- premature extensibility frameworks
- generic workflow DSLs
- clever metaprogramming

When a simpler solution satisfies the architecture, choose it.

## Testing rule
Normal unit and integration tests must not require paid LLM calls.

Provide fake/test implementations of external boundaries.

This includes AgentRuntime.

Fake agents are test doubles, not production architecture.

They exist so retry, escalation, failures and state transitions can be tested deterministically.

## Scope discipline
If implementation reveals that this architecture should change:
1. stop before making a large structural divergence,
2. describe the problem,
3. propose the smallest correction,
4. update architecture documentation,
5. then implement.

Do not silently redesign the system.
