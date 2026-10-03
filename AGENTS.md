# Software Agent Factory. Agent Instructions

## What this repository is

This repository implements a local-first autonomous software engineering factory.

The factory takes software work through:

```text
Project brief (optional)
    ↓
Smallest sufficient work breakdown
    ↓
Work Item
    ↓
Prepare worktree
    ↓
Profile repository
    ↓
  Triage
    ↓
   Plan (specification and plan in one call)
    ↓
 Implement
    ↓
  Verify
    ↓
Polish once if enabled
    ↓
  Verify again
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
- write specifications
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

## Delivery principle

**Choose the fastest sufficient solution.**

For both project decomposition and individual task planning:

- prefer one coherent work item when it can safely deliver the requested outcome
- split work only for independently verifiable outcomes, hard prerequisites,
  safe parallel execution or an existing scope limit
- treat each generated project task as one reviewable pull request; a shared
  product goal or safety boundary does not justify one issue spanning multiple
  independently verifiable capabilities
- express merge-before-start requirements as task dependencies and leave safe
  parallel tasks dependency-free so the controller can run isolated worktrees
- reuse existing code and boundaries before adding abstractions, dependencies,
  services, configuration or infrastructure
- do not create separate work items for tests, documentation, setup or cleanup
  when they belong to the same functional outcome
- stop when the acceptance criteria and configured quality gates pass

Agents explain the selected approach. Deterministic factory code validates
task bounds and dependencies, owns execution order, and rejects malformed
plans.

## Writing principle

Use concise technical English for all factory-authored text.

- Use the pinned
  [SimpleEnglish skill](https://github.com/AminBlg/SimpleEnglish/blob/61ee200efbd423050aab982eed94226229891ae0/skills/simple-english/SKILL.md)
  for every change to `README.md` or `docs/`.
- Apply the skill's strict ASD-STE100 guidance during the writing review.
- Classify each passage as procedural or descriptive.
- Use active voice and simple sentences.
- Use simple tenses and American spelling.
- Put each condition before its instruction.
- Put one action or fact in each sentence.
- Use 20 words or fewer for instructions.
- Use 25 words or fewer for descriptions.
- Use `can`, `will` or `must` for modal meaning.
- Do not use contractions.
- Use one term for one meaning.
- Define each unfamiliar concept at its first use.
- Remove filler, semicolons, em dashes and Latin abbreviations.
- Do not use bold lead-ins or decorative emphasis.
- Put the command or condition before the risk in a warning.
- Preserve facts, uncertainty, code, identifiers, paths, commands and quoted errors.

Run this local check after each documentation change:

```bash
uv run --no-sync python scripts/docs/check_simple_english.py
```

The check uses selected rules from SimpleEnglish. A successful check does not
prove formal ASD-STE100 compliance.

## Repository delivery workflow

After completing and verifying repository changes, commit and push the current
branch, then create or update a pull request so the changes are reviewable,
unless the user explicitly asks to leave the changes uncommitted, local-only,
or without a pull request.

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
RepositoryProfile
  ↓
ToolchainInventory
  ↓
RepositoryCommandsPlan
  ↓
TriageResult
  ↓
Specification and ExecutionPlan (one Planner call, ADR-035)
  ↓
ChangeSet
  ↓
VerificationReport
  ↓
ReviewReport
```

Persist these artifacts.

Each agent receives only the context needed for its job.

Repository capabilities are deterministic, factory-owned advisory context.
After preparing the worktree and before triage, scan only repository-local
paths and allowlisted manifests. Do not execute code, import target modules,
open a shell or use the network. Persist `repository-profile.json`, recording
exact dependency evidence: direct declarations (ecosystem, name, declared
version, optional exact resolved version/resolution path, manifest path,
dependency group) parsed from `pyproject.toml` (PEP 621 tables,
`dependency-groups`, `requires-python` and the Poetry tables),
`requirements.txt`/`requirements-*.txt` (`pip`) and `package.json`, with exact
versions resolved from `uv.lock`, `package-lock.json` and `pnpm-lock.yaml` when
unambiguous. `poetry.lock`, `yarn.lock` and `bun.lock` identify their package
manager and are fingerprinted only. Also record `version_files`, a semantic
`dependency_fingerprint` and a `manifest_fingerprint` kept as file-content
provenance.

The repository commands step (ADR-034) is the exception to the no-execution
rule above. When the YAML has no repository commands and
`repository.derive_commands` is on, it runs the derived install and verify
commands on the clean base commit before triage, and persists
`repository-commands.json`. The setup run (`factory setup`) is the second
exception: it runs package managers in a setup worktree to change the
manifest and the lockfile. It installs nothing. Its JavaScript commands run
no package scripts, but Python locking can run the project's build backend.

There is no repository-provided skill plugin system. Two fixed catalogs are
factory-owned (ADR-034): the setup templates that a setup pull request writes
into the target repository, and the review lenses in `review_lenses.py`. The
Reviewer prompt gets the lens checklists that match the changed files. A stack
lens (React, Vue or Angular) also needs the repository profile to declare one
of its dependencies. No model selects lenses.

No model writes or selects guidance. When `polish.enabled` and the bounded
polish attempt is eligible, one existing Implementer attempt gets fixed
guidance: the bodies of the factory's `simplify` and `polish` setup templates,
and the review lenses for the changed files. It applies simplify first, then
polish, after the first successful deterministic verification. Full
deterministic verification runs again afterwards. The polish attempt needs no
web access. The guidance is advisory: it never
changes tools, models, commands, workflow states, retry budgets, permissions,
quality gates, dependencies, scope or workflow authority.

### 3. A model does not approve its own work
The implementer's success claim is not a quality gate.

Use deterministic validation first.

Deterministic verification can accept `SINGLE` route execution only when every configured sufficiency condition holds.
All other work requires independent model review.

Prefer a different model family for final review.

### 4. Retries are bounded
Never implement an unlimited retry loop.

All retries must have explicit limits and recorded reasons.

The optional post-green polish is one `IMPLEMENTER` attempt with trigger
`POLISH`, not a new state or role. It consumes the existing implementation
budget, may make no edits, is always verified again, never runs during CI
repair and runs only when one later recovery attempt would still remain.

### 5. Complexity and risk are separate concepts
Complexity selects model strength.

Risk selects governance and required validation.

A trivial change may be high risk.

A difficult change may be low operational risk.

### Execution routing
Route controls workflow stages.
Model profile controls worker strength.

The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.
`SINGLE` runs the Implementer and deterministic verification.
`CRITIQUE` runs the Implementer, deterministic verification, and the independent Reviewer.
`FULL` runs the complete multi-agent pipeline.
It retains triage, planning, implementation, verification, optional polish attempt, Tester, and Reviewer.
`MANUAL_TRIAGE` stops safely before implementation.

When enabled, the controller picks the lightest legal route with a fixed rule (ADR-037).
The order is `SINGLE`, `CRITIQUE`, `FULL`, then `MANUAL_TRIAGE`.
Ties keep the configuration order.
Routing makes no network call.

The controller enforces deterministic safety floors before it picks a route.
The factory does not treat governance categories as an intrinsic truth.
Governance categories are human-owned configuration in `routing.full_only_terms`.
The configured risk policy decides which routes are legal.
Configured sensitive terms, repository labels, protected paths, and explicit work item risk serve as deterministic floors.
Worker model escalation is the existing cascade behavior in the factory.
We do not add a duplicate cascade route.

Deterministic ratchets protect execution after implementation.
When verification fails, the controller upgrades `SINGLE` to `CRITIQUE`.
Post-implementation ratchets upgrade `SINGLE` or `CRITIQUE` to `FULL_REVIEW`.
`FULL_REVIEW` runs full independent Tester and Reviewer gates without restarting earlier stages.
The controller never downgrades a route.

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
- web dashboard (one narrow exception below)
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

The explicit exception to autonomous merge is the opt-in, controller-owned
delivery path in ADR-022: allowlisted repository and target, required green
checks for the reviewed head, no branch-protection bypass, and confirmed merge
evidence. Agents still never merge or choose delivery policy themselves.

The first version uses filesystem persistence.

#### The one permitted dashboard

A local dashboard has been explicitly requested (Phase 15.11, ADR-016,
amended by ADR-033). It is the only exception to the ban above and is allowed
only as:

- bound to `127.0.0.1`, started by an explicit command, disabled by default
- read-only, except two named write actions: approve a risk approval, and
  answer plan decisions. They only create a request file. The factory service
  ingests it and is the single writer of `run.json`. No endpoint may change
  configuration or workflow state in any other way
- token protected. The per-start token guards the HTTP route only. The factory
  service never checks it and trusts any request file in the run directory. So
  write access to `<data_dir>/runs` is the real authority for a local approval,
  the same as write access to `run.json`. Agents work in their own workspace,
  and the factory does not give them the data directory
- served from the Python standard library, with no web framework, no npm, no
  bundler and no build step
- no logs and no diffs rendered by default

Everything else in the list stays banned. Nothing may become a hosted service,
a multi-user application or a control plane. If a dashboard change would need a
framework, a package manager or a third write action, stop and update the ADR first.

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
  GPT-5.6 Terra

Planner (specification and plan)
  Claude Opus 5

L0 Worker
  MAI-Code-1.1-Flash

L1 Worker
  Gemini 3.8 Flash

L2 Worker
  Claude Sonnet 5

L3 Worker
  Claude Opus 5

Tester
  Gemini 3.8 Flash

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
