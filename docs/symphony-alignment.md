# OpenAI Symphony Alignment

## Purpose

OpenAI Symphony is the primary coordination reference for this project.

We are NOT attempting to reproduce Symphony exactly.

We are also NOT building a Codex-specific factory.

The goal is:

```text
Symphony orchestration principles
              +
specialized multi-model SDLC pipeline
```

This project is Symphony-inspired at the coordination layer. It is not a
Symphony implementation or conformance target: it uses GitHub Copilot rather
than the Codex app-server protocol and gives deterministic factory code broader
ownership of SDLC transitions, Git operations, quality gates and acceptance.

The alignment was reviewed against OpenAI Symphony Draft v1 at commit
`8001b52e3062495a16e520e4ceaf8f9de868c4d0` on 2026-09-04.

# Symphony concepts to preserve

## 1. One authoritative orchestrator

The orchestrator owns scheduler reservations, active execution, retries,
concurrency and factory SDLC transitions.

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

## 4. Reserve before dispatch

An eligible item must be reserved before work starts.

```text
candidate
   ↓
 reserve
   ↓
ensure not already active
   ↓
dispatch
```

In Symphony this is an in-memory duplicate-prevention mechanism, not a durable
tracker lease. The manual Phase 1 CLI uses an exclusive per-work-item lock. The
future scheduler must revalidate tracker state immediately before dispatch.

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

Failed attempts preserve the workspace for retry and diagnosis. Workspace
cleanup is explicit in the manual phase and later follows configured terminal
tracker state rather than treating every internal failure as terminal.

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
│       ├── repository-profile.json
│       ├── specification.json
│       └── ...
└── workspaces/
    └── TASK-123/
```

Durable run artifacts and retry history are a factory extension. Symphony's
live reservations and retry timers are in-memory and do not survive restart.

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

The synchronous manual phase uses hard agent and command timeouts. Activity
tracking and worker heartbeats arrive with the scheduler.

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

## Explicit SDLC stages and roles

```text
DETERMINISTIC REPOSITORY PROFILE
   ↓
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
SCOPE
   ↓
POLISH once if enabled
   ↓
VERIFY again
   ↓
SCOPE again
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
RepositoryProfile
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

`RepositoryProfile` is produced by deterministic factory code after workspace
preparation and before triage, and again before an eligible polish attempt. It
never grants tools or authority, and there is no fixed built-in skill catalog
and no way for a repository to supply skill definitions. Instead, one bounded,
research-grounded `RepositorySkill` is generated by the configured Researcher
for the repository as a whole, bound to the profile's semantic
`dependency_fingerprint`, stored under `factory.data_dir` outside the target
repository, and reused by later runs until that fingerprint changes. An
optional human-written `repository-skill-overlay.yaml` sits beside it and is
never modified by the factory. The effective guidance reaches only the polish
attempt's Implementer, Tester and Reviewer. If it cannot be loaded, generated
or verified, it is dropped with a recorded warning and the already-green run
continues.

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

## Explicit trust boundary

The controller owns tracker and future GitHub credentials. Agent processes
receive only credentials required for their assigned role. Approval or input
requests do not wait indefinitely; they terminate the active invocation,
preserve workspace evidence and move the run to `NEEDS_HUMAN`.

## Adopted Symphony concepts

`factory start` now implements the coordination layer:

- **Polling, not webhooks.** `Scheduler.run_forever` blocks on an injected
  stop/sleep abstraction; there is no server, no webhook and no asyncio
  framework.
- **Reconciliation before dispatch.** Every tick reconciles this process's
  active handles and re-reads persisted `FactoryRun` state before evaluating
  candidates, so a manual `factory run` started since the last tick is honored
  immediately.
- **Reserve before dispatch.** A candidate is reserved in-memory before
  `dispatch()` is called, and revalidated against the tracker immediately
  before reservation.
- **Tracker adapters and candidate normalization.** `TrackerProvider` is
  generic; `GitHubIssueProvider` normalizes GitHub issues (label
  `agent-ready`) into `TrackerItem`s. The scheduler has no GitHub dependency.
- **Bounded concurrency.** `scheduler.max_concurrent_tasks` is validated to be
  1 or 2 and dispatch runs through a thread pool.
- **Deterministic per-task workspaces.** `deterministic_work_item_id` keys both
  the workspace and duplicate prevention, so a manual run and the daemon can
  never dispatch the same issue twice.
- **Worker activity heartbeats.** Every controller transition refreshes
  `FactoryRun.last_activity_at` and the `RunLease` heartbeat; stall detection
  reads that persisted signal rather than inspecting lock files.
- **Recovery using tracker + filesystem.** Startup reconciliation inspects
  persisted runs and escalates abandoned ones to `NEEDS_HUMAN` through the
  controller (ADR-011).

## Deliberately not adopted

- **Repository-owned, hot-reloaded workflow configuration.** The factory's
  typed configuration (`FactoryConfig`, strict `extra="forbid"`) plus the
  role-scoped prompt builders in `prompts.py` are the intentional replacement.
  A repository cannot redefine the factory's stages, gates or budgets: those
  are factory authority, not repository input. Deterministic profiling never
  loads repository-defined skills or plugins; the one bounded
  `RepositorySkill` it enables is generated by the configured Researcher — not
  selected from a built-in catalog — kept outside the target repository, and
  reused across runs until the dependency fingerprint changes. That invocation
  is web-only, with fetches restricted to
  `polish.official_documentation_origins` and the exact
  `polish.practice_reference_urls`. Humans customize guidance through a
  factory-owned overlay file outside the repository, not through files the
  target repository ships. A repository only supplies its own
  `install`/`verify`/`build` commands.
- **Durable scheduler claims.** Live reservations stay in-memory. The durable
  source of truth is the persisted `FactoryRun` plus the workspace `flock`,
  which is sufficient for a single local process and avoids introducing a
  database (`AGENTS.md`: "Keep V1 small").
- **The Codex app-server protocol.** Execution goes through
  `CopilotAgentRuntime`.

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
typed project-brief decomposition
bounded dependency-aware project execution
SDLC decomposition
model specialization
typed evidence
risk governance
deterministic quality
independent verification
deterministic repository capabilities
bounded post-green polish
```

## Project-level extension beyond Symphony

Symphony schedules work already represented as tracker issues; it does not
define a project brief, backlog-generation artifact, portable task DAG, or
aggregate project-completion predicate. The factory therefore adds one narrow
layer above the Symphony-aligned scheduler:

```text
ProjectBrief
   ↓ Planner proposes
ProjectPlan
   ↓ deterministic validation
dependency-ready WorkItems
   ↓ existing WorkflowController
FactoryRuns
   ↓ deterministic integration and aggregation
ProjectExecution
```

The extension preserves Symphony's ownership rules. The Planner proposes only
typed tasks and dependencies. Factory code validates the graph, bounds it to 12
tasks, creates optional GitHub issues, selects ready tasks, and composes commits
on one local integration branch. Every child run retains the normal bounded
retry, verification, review and risk gates. Factory code then runs the
configured deterministic repository commands against the fully composed branch
before deriving aggregate completion.

No portfolio service, database, workflow DSL, recursive task tree or autonomous
agent swarm is introduced. Bounded parallelism remains `1` or `2`, and a merge
conflict or failed dependency stops for human attention.
