# How it works

A run is one execution of a work item.
The factory takes each run through a controlled sequence of stages.
The [interactive pipeline](../index.md#the-pipeline) explains these stages in six groups.
For artifact fields, read [Architecture](../architecture.md).

## The shape of the system

<div class="saf-boundaries" markdown>

<div class="saf-boundary" markdown>

<span class="saf-small-label">01 / Input</span>

### Submit the work

A work item describes one requested change.
The command line accepts manual work items.
The optional scheduler selects eligible GitHub issues.

</div>

<div class="saf-boundary saf-boundary--accent" markdown>

<span class="saf-small-label">02 / Control</span>

### Apply workflow rules

The controller is code that enforces workflow rules.
It selects models through configuration.
It controls the stages and required checks.

</div>

<div class="saf-boundary" markdown>

<span class="saf-small-label">03 / Execution</span>

### Produce the evidence

Agents produce changes and findings.
Repository commands produce verification results.
The factory retains the evidence in local JSON files.

</div>

</div>

`WorkflowController` is the only thing that transitions a run. Agents return
artifacts and outcomes. They do not mutate orchestration state. The scheduler
owns claiming and concurrency. It never mutates a run directly. It operates
through the controller.

## Workflow states

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

The allowed transitions are declared as data and enforced on every call:

```text
CREATED      → TRIAGING
TRIAGING     → REFINING
REFINING     → RESEARCHING | PLANNING
RESEARCHING  → PLANNING
PLANNING     → IMPLEMENTING
IMPLEMENTING → VERIFYING
VERIFYING    → REVIEWING | IMPLEMENTING | PLANNING
REVIEWING    → PR_READY | IMPLEMENTING
PR_READY     → PR_CREATED
PR_CREATED   → CI_RUNNING | DONE
CI_RUNNING   → DONE | CI_DIAGNOSIS
CI_DIAGNOSIS → IMPLEMENTING
```

Every non-terminal state can also transition to:

- `NEEDS_HUMAN`: A business decision. Eligibility, risk, scope, an exhausted
  budget, or a CI failure that is not repairable.
- `FAILED`: An operational failure. An agent or infrastructure problem.

Terminal states are `DONE`, `NEEDS_HUMAN` and `FAILED`.

There is deliberately no `REPAIRING`, `PLAN_READY` or `BLOCKED` state. Repair is
a bounded transition back to `IMPLEMENTING`, or back to `PLANNING` for scope
drift (not a second workflow). "Blocked" is `NEEDS_HUMAN` with a recorded
reason.

`PR_READY` is not terminal. With pull requests enabled it continues to
`PR_CREATED`. With them disabled it is the completed endpoint of the manual
flow, and the controller finalizes it explicitly.

Repository profiling happens after workspace preparation and before
`TRIAGING`, without adding a state. The optional post-green polish is an
`IMPLEMENTER` attempt with fixed guidance through the existing
`IMPLEMENTING → VERIFYING` transition. There is no `POLISHING` state.

## Typed artifacts, not one long conversation

Each stage produces a validated artifact and hands it to the next. Nothing
accumulates a giant shared transcript.

```text
WorkItem
  → RepositoryProfile
  → TriageResult
  → Specification
  → [ResearchReport]
  → ExecutionPlan
  → ChangeSet
  → VerificationReport
  → TestReport
  → ReviewReport
  → [CIReport]
```

They are persisted as versioned JSON in the run directory, with a per-attempt
snapshot under `attempts/NN/`. Writes are atomically replaced, because the
filesystem is the recovery source of truth.

Each agent receives only the context its job needs. That keeps prompts small,
keeps failures attributable, and means a later stage cannot be persuaded by an
earlier stage's narrative.

`RepositoryProfile` is factory-produced before triage. It records detected technologies, test tools,
package managers, markers, warnings, and version files. It also records exact
dependency declarations, a semantic `dependency_fingerprint`, and a
`manifest_fingerprint`.

## The agents

| Agent | Job | Sees |
| --- | --- | --- |
| Triage | Assign complexity, risk, and whether research is needed. | The work item. |
| Specification Refiner | Turn the request into acceptance criteria. | Work item, triage. |
| Researcher | Answer specific open questions. | Specification. |
| Planner | Produce an execution plan with an expected scope. | Specification, research. |
| Implementer | Edit the worktree. | Plan and repository. The fixed simplify and polish guidance and the review lenses are provided only during the bounded polish attempt. |
| Tester | Judge whether the change is actually tested. | Work item, specification, execution plan, controller-derived diff, changed files, and deterministic results. |
| Reviewer | Independent review. | Work item, specification, execution plan, controller-derived diff and changed files, deterministic results, independent TestReport, implementation snapshot number, and typed open review findings. It also receives the review lenses for the changed files. A repair review also receives the exact diff since the previous reviewed tree. |
| Failure Investigator | Diagnose a CI failure. | Normalized CI evidence. |

The tester and reviewer never see the implementer's own summary. That is
deliberate: a model's claim about its work is not evidence.

Planner, Tester and Reviewer typed-output failures get bounded same-model
correction attempts. The validation error is passed back as correction context.
Tester and Reviewer corrections do not spend implementation attempts.

The first review establishes typed blockers with exact source locations. The
controller assigns their ids and persists them. A repair review must mark every
open blocker `RESOLVED`, `UNRESOLVED` or `WITHDRAWN`. Repair regressions join
the open set. One late batch can also be adopted so a serious missed defect is
not silently accepted, but later drip-fed findings are advisory. Repeated
blockers on one path, one unresolved id, or repeated blocker replacement stop
early with `review-impasse.json`.

Research runs, but it does not escalate. A researcher that finds nothing useful
returns a report and the run continues.

Guidance and lenses never change tools, models, commands, states, retry
budgets, permissions, gates, dependencies or scope.

## Repository capabilities

The controller scans repository-local paths and a small allowlist of bounded
manifests. It never executes a command, imports target code or contacts the
network. It captures exact dependency evidence. This records package names,
declared versions, manifests, and resolved lockfile versions.

On the Python side that means `pyproject.toml` (PEP 621 dependency tables,
`dependency-groups`, `requires-python`, and the Poetry dependency, dev and
group tables), `requirements.txt`/`requirements-*.txt` for pip projects, and
`setup.cfg`/`tox.ini` for pytest evidence. On the JavaScript side it means
`package.json` runtime, dev, peer and optional dependencies plus
`packageManager`. Exact versions come from `uv.lock`, `package-lock.json` and
`pnpm-lock.yaml`. The files `poetry.lock`, `yarn.lock` and `bun.lock` identify
the package manager and are fingerprinted, but are not parsed for exact
versions.

No model writes or selects guidance. The polish attempt gets fixed guidance:
the factory's `simplify` and `polish` templates, and the review lenses for the
changed files. A stack lens for React, Vue or Angular applies only when the
repository declares one of its dependencies. The polish attempt makes no
Researcher call and no web request.

One bounded existing Implementer attempt applies the guidance, simplification
first and polish second. Then the full deterministic verification runs again.
The guidance is never available before the initial green baseline. Polish is an
optional improvement on an already-verified change.

## Complexity and risk are separate

**Complexity** selects model strength: `L0` through `L3` map to the four
configured worker models. Mechanical work gets a cheap model.

**Risk** selects governance: `R0` through `R3` decide whether a human must
approve, and whether a sensitive scope finding escalates rather than replans.

They do not correlate. A one-line change to an auth check is trivial and high
risk. A large refactor of a test helper is hard and low risk.

## Model routing

`ModelRouter` maps role and complexity to a configured model and reasoning
level. Model names live in configuration, not in the source.

Implementation escalation is bounded: after
`retries.same_model_attempts` failures the router moves to a stronger model, up
to `retries.max_total_attempts` total. The chosen model and the attempt number
are persisted on every attempt record, so routing can be calibrated later
against real success and cost data.

## Adaptive execution routing

Simple tasks do not always need the full pipeline.
When enabled, Jev acts as an external classifier.
Jev is a classifier from TypeSafe.
The factory calls it over HTTPS.
System One is the TypeSafe product that serves Jev.
The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.

`SINGLE` runs the Implementer and deterministic verification.
`CRITIQUE` runs the Implementer, deterministic verification, and the independent Reviewer.
`FULL` runs the complete multi-agent pipeline.
`MANUAL_TRIAGE` halts safely before implementation for human inspection.
`FULL_REVIEW` is a post-implementation upgrade that runs full independent review gates without restarting earlier stages.

Read the [adaptive routing guide](../guides/adaptive-routing.md) for configuration and usage.

## Deterministic gates

Before any model judges the change, the factory computes:

- the Git diff and the changed file list, from the worktree
- `install`, `verify` and `build` results from your configured commands
- the failure category when a phase fails: lint, type, test, dependency or build
- scope drift against the plan's expected scope
- protected file matches
- changed-file count against the ceiling

With `polish.enabled`, the first successful verification and scope assessment
schedule at most one more Implementer pass before testing and review. The pass
consumes the implementation budget, can make no edits, never runs during CI
repair, and is always verified again. The tester and reviewer run only after
the final green result. LLM judgement supplements deterministic evidence. It
does not replace deterministic evidence.

## Workspaces

Each work item gets its own Git worktree:

```text
<data_dir>/workspaces/<work-item-id>/
```

Paths are sanitized and contained under the workspace root. Cleanup refuses
anything outside it. A short-lived exclusive lock stops two processes owning the
same work item. Workspaces are preserved by default so you can inspect the
change afterwards.

The `WorkspaceProvider` interface is small on purpose: `prepare`, `get_path`,
`diff`, `cleanup`. There is no generic remote-worker abstraction.

## Persistence

Filesystem JSON. No database.

```text
<data_dir>/
├── runs/<run-id>/
│   ├── run.json          state, attempts, budgets, lease, timestamps
│   ├── work-item.json
│   ├── repository-profile.json
│   ├── toolchain-inventory.json
│   ├── repository-commands.json
│   ├── mutation.json
│   ├── triage.json
│   ├── specification.json
│   ├── research.json
│   ├── execution-plan.json
│   ├── change-set.json
│   ├── patch.diff
│   ├── verification.json
│   ├── test-report.json
│   ├── review.json
│   ├── ci.json
│   ├── logs/             per-command output, bounded and redacted
│   └── attempts/NN/      per-attempt snapshots
├── workspaces/
├── locks/
└── logs/factory.log
```

`RunStore` is a small interface with methods `save_run`, `load_run`,
`list_runs`, `save_artifact`, and `load_artifact`. It has one implementation:
`FileRunStore`. A `PostgresRunStore` is possible later and deliberately not
built now.

Health and metrics are *derived* from these files on demand. There is no counter
store and no time-series database, so metrics can never drift out of sync with
what actually happened.

## Scheduling

Scheduling ownership is separate from SDLC state. `Scheduler` owns reservations,
task order, bounded concurrency, and stall detection entirely in memory. It
never mutates a `FactoryRun`.

`FactoryService` composes the scheduler with the GitHub issue provider and
workflow controller. It dispatches runs through a thread pool bounded by
`scheduler.max_concurrent_tasks`.

The pattern of polling, reconciliation, reservation before dispatch, bounded
concurrency, and recovery from the tracker and the filesystem comes from OpenAI
Symphony. See [Symphony alignment](../symphony-alignment.md) for what was
reused, what was extended and what was rejected.

## Where to read next

- [Architecture](../architecture.md): The full document with every artifact
  field.
- [Symphony alignment](../symphony-alignment.md): The orchestration lineage.
- [Decisions](../decisions.md): The reasons for design choices.
- [Safety and trust boundaries](../reference/safety.md): What the system will
  not do.
