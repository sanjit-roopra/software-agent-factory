# Architecture Decisions

## ADR-001: Build one small executable vertical slice

Phase 1 combines the original fake-workflow and Git-worktree milestones.

Reason:
- a workflow without a repository boundary proves too little
- adding workspaces later would force the runtime and controller APIs to change
- one synchronous path is easier to understand and test

The slice keeps the intended stages, typed artifacts, deterministic routing,
bounded repair, filesystem persistence and independent fake review.

## ADR-002: Keep authority and evidence deterministic

Only `WorkflowController` changes run state.

Agents return typed outcomes but do not transition runs. The controller derives
changed files and `patch.diff` from Git, including newly created files.
Verification command results are also controller-produced evidence.

## ADR-003: Use one repair budget

Every implementation or repair entry appends an attempt record and consumes one
global maximum. Verification and review failures share this budget.

This prevents alternating gate failures from bypassing bounded retry policy.

## ADR-004: Defer scheduler architecture

Phase 1 is a synchronous manual command with concurrency one.

Polling, reconciliation, tracker adapters, retry timers, activity heartbeats and
multi-task scheduling are deferred until `factory start`. A per-work-item
exclusive lock and subprocess timeouts provide the necessary local safety now.

## ADR-005: Treat Symphony as coordination inspiration

The project follows Symphony's control-loop and workspace principles, but is not
a conforming implementation. Copilot execution, finite persisted repair
budgets, typed SDLC artifacts, controller-owned Git/PR behavior and independent
quality gates are deliberate extensions.

## ADR-006: `PR_READY` is a completed endpoint, not a terminal state

Terminal states are `DONE`, `NEEDS_HUMAN` and `FAILED`.

`PR_READY` stays reachable for pull-request-enabled runs (it transitions to
`PR_CREATED`), so it cannot be terminal. But when `pull_request.enabled` is
false it *is* where the manual flow legitimately ends.

The controller therefore finalizes it explicitly (`finalize_pr_ready`) by
stamping `completed_at`, and `workflow.is_run_finished` is the single predicate
that distinguishes "the manual flow completed here" from "a PR-enabled run was
interrupted at the publishing boundary". The scheduler uses that predicate
rather than comparing states directly.

Transitions also clear a stale `completed_at`/`failure_reason` whenever a run
becomes active again, so a repaired run never carries a completion timestamp
from an earlier cycle.

## ADR-007: Two separate, persisted retry budgets

`AttemptBudget.IMPLEMENTATION` covers the whole pre-PR loop: implementer
failures, deterministic verification failures, reviewer rejections and
scope-drift replans all consume `retries.max_total_attempts`.

`AttemptBudget.CI_REPAIR` is a separate budget bounded by `ci.repair_attempts`.
It also hard-caps how many times a PR may be updated, so a CI loop cannot push
forever.

Both attempt numbers are derived from persisted `FactoryRun.attempt_records`,
never from a local counter. A restarted process therefore cannot widen a budget.
Scope replans are bounded independently by `scope_drift.max_replans`, counted
from persisted records whose `triggered_by` is `SCOPE`.

## ADR-008: Lock contention is not a persisted failure

If another live run already owns a work item's workspace, `WorkflowController.run`
returns a non-persisted `FAILED` outcome explaining that the work item is
already active, and writes nothing to the run store.

Persisting a junk `FAILED` run would pollute the store, count against nothing,
and later force reconciliation to explain a run that never did any work. Since
no workspace is prepared and no artifact is written, there is nothing to
corrupt or recover.

## ADR-009: Research runs; it does not escalate

Phase 1 escalated `needs_research=true` to `NEEDS_HUMAN` because no researcher
existed. The researcher now runs exactly once per run, its `ResearchReport` is
persisted, it is handed to the planner, and the run continues. Research is never
re-run, so a task cannot repeatedly pay for it.

## ADR-010: The independent tester returns a `TestReport`

Earlier wiring mapped the tester role onto `VerificationReport`. That conflated
a model's judgement with deterministic, factory-produced evidence, which
directly contradicts "a model does not approve its own work".

The tester now returns `TestReport` (advisory), while `VerificationReport`
remains exclusively controller-produced. Tester and reviewer receive the
authoritative diff, the controller-derived changed-file list and the
deterministic report; neither ever receives the implementer's `ChangeSet`
summary.

## ADR-011: Conservative scheduler recovery

A persisted, non-terminal run found at startup is escalated to `NEEDS_HUMAN`
through `WorkflowController.recover_abandoned_run` rather than auto-resumed.

Auto-resuming would spend a paid attempt on a run whose true state (workspace
contents, partially applied edits, an already-pushed commit) cannot be
established cheaply. Escalating preserves every artifact and the workspace,
consumes no budget, and leaves a human in control. The scheduler itself still
never mutates run state.

## ADR-012: Worktree administration is serialized per source repository

`git worktree add` and `git worktree prune` both rewrite repository-global
administrative metadata. With `scheduler.max_concurrent_tasks = 2` two runs can
prepare workspaces against the same repository simultaneously, so the whole
`prepare()` sequence is held under a per-source-repo `flock`. Per-work-item
workspace locks remain separate and are what prevent duplicate active work.

## ADR-013: Tracked work is dispatched at most once

The generic `Scheduler` prevents *concurrent* duplicates and otherwise assumes
a tracker withdraws an item once work starts. GitHub Issues do not: an issue
stays open and keeps its `agent-ready` label after a run finishes, and this
factory deliberately holds no write access to the backlog.

Without an additional rule, the tick after a run reached
`DONE`/`NEEDS_HUMAN`/`FAILED` would dispatch the same issue again under a new
`FactoryRun` with an empty `attempt_records` list — an unbounded loop of paid
work that mints a fresh retry budget every cycle, defeating ADR-003/ADR-007.

`service.AlreadyRunFilter` therefore makes any tracker item with a persisted
`FactoryRun` (finished or not) ineligible. Re-running is an explicit operator
action: archive or remove the previous run, or invoke
`factory run --work-item-id` by hand. This keeps the rule durable across
restarts without adding GitHub write permissions or a database.

## ADR-014: Phase 15 is opened selectively, not as a whole

Phase 15 was a single "later integrations" bucket. That made it impossible to
say yes to delivery work without appearing to say yes to Temporal, Postgres,
Kubernetes, Jira and autonomous deployment.

Phase 15 is therefore split into numbered sub-phases with independent statuses.
Exactly five are open: 15.0 factory CI, 15.1 tag-driven release, 15.2 macOS
packaging and the launchd service, 15.5 local monitoring/health and 15.11 the
read-only dashboard.

Every other sub-phase — staging (15.3), deployment (15.4), Docker (15.6),
remote workers (15.7), Postgres (15.8), Temporal (15.9), Jira (15.10),
Kubernetes (15.12) — stays deferred. Nothing in the open sub-phases may depend
on a deferred one, and no deferred item is unblocked by proximity.

The selection is operational, not architectural: it makes the existing factory
installable, observable and inspectable on one MacBook. It does not widen what
the factory is allowed to do autonomously.

## ADR-015: CD means delivering immutable artifacts, never deploying

"Continuous delivery" in this project stops at a published, immutable GitHub
Release. A version tag builds artifacts and attaches them. Nothing installs,
restarts, promotes or self-updates, and there is no mutable pointer a client
follows automatically. Autonomous deployment stays banned by `AGENTS.md`.

Two native macOS builds are produced — arm64 on `macos-15` and x86_64 on
`macos-15-intel` — as separate PyInstaller `onedir` archives. `universal2` is
rejected: it requires universal wheels for every native dependency, produces a
larger artifact for a single-user tool, and turns one architecture's packaging
problem into a total build failure. Building each slice natively on its own
runner keeps failures isolated and diagnosable.

The release also contains a wheel and an sdist for people who already have
Python, plus `SHA256SUMS` and a `build-info.json` recording tag, commit,
runner image, Python version, PyInstaller version and architecture, so any
downloaded artifact is traceable to a build.

Artifacts are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are explicitly deferred: they need a paid account and secrets in
CI, and neither is justified for a local-first single-user tool yet. The
consequence is that Gatekeeper will quarantine a downloaded archive, so release
notes must say so plainly and document the manual step. Silence here would look
like a broken build.

A frozen artifact is not self-sufficient. It bundles Python and the factory,
but `git` must exist on `PATH`; `gh` is required only when a GitHub-touching
feature is enabled — pull requests, CI observation, or the backlog daemon,
which polls GitHub Issues through `gh` — and `copilot` only for `--runtime
copilot`. Preflight therefore validates prerequisites for *enabled* features,
so the default offline run does not demand tools it will never call.

## ADR-016: The local dashboard is a bounded exception to the V1 ban

`AGENTS.md` bans a web dashboard in V1. One narrow exception is now explicitly
requested and granted, because inspecting runs, states, attempts and metrics by
reading JSON under `~/.software-factory` is genuinely worse than a page.

The exception holds only within these boundaries:
- loopback bind, explicit start command, disabled by default
- read-only: `GET` only, no route mutates runs, workspaces or configuration
- token protected, token generated per start and never logged
- Python standard library only — no framework, no npm, no bundler, no build
- no command logs and no diffs rendered, because those are the two places where
  repository content and near-secrets would leak into a browser. Minimization
  is applied twice and independently: the detail view is built from an
  allowlisted typed model (no `failure_reason`, no agent reasoning, no raw
  artifact), and the request handler allowlists again before responding

The ban itself is unchanged for everything else. This is a local viewer, not a
control plane: it cannot approve a run, cannot retry one, cannot enable an
integration and has no multi-user concept. If a change would need a write path,
a framework or a non-loopback listener, that is a new ADR, not a refactor.

## ADR-017: Health and metrics are derived, never accumulated

Persisted run artifacts remain the single source of truth. Health and metrics
are pure functions over the run store, computed on demand.

No counter store, no time-series database and no separate metrics file is
introduced. A derived view cannot drift from the runs it describes, can be
recomputed after any crash, and is trivially testable against a fixture store.
Health and metrics are strictly read-only: they never repair a lock, prune a
worktree or transition a run — they report those as findings for an operator.

Cost is deliberately not fabricated. Token usage and cost appear only when the
runtime actually reported them; otherwise the value is unknown, never zero and
never inferred from a hard-coded price table. A confidently wrong spend number
is worse than no number. In practice no runtime reports usage today and
`AttemptRecord` persists none, so no usage or cost figure is reported at all;
reporting one starts by adding a typed field to `AttemptRecord`.

Monitoring stays local: structured JSON logs bounded in size inside the data
directory, with the same credential redaction already applied to command
output. No exporter, no cloud backend, no telemetry leaves the machine.

## ADR-018: The launchd service is an opt-in user agent

Running `factory start` continuously is a `launchd` job, but a deliberately
timid one.

It is a per-user `LaunchAgent` under `~/Library/LaunchAgents`, installed only
by an explicit CLI command. It is never a root `LaunchDaemon`, never installed
by extracting an archive, and never installed as a side effect of running the
factory. A background process that can spend money and push branches must be an
explicit, reversible act.

The installed job defaults to `--runtime fake`, so an accidentally loaded agent
costs nothing until someone deliberately changes it. Because launchd gives
agents a minimal environment, the installer captures an explicit `PATH`
snapshot; otherwise the service would fail to find `git`, `gh` or `copilot` in
a way that looks like a factory bug. Install also refuses unless the given
configuration enables the scheduler and `factory doctor` is clean, because a
service that cannot work is worse than no service.

Logging goes to the factory's own bounded, rotating structured log under the
configured data directory; launchd's stdout/stderr are pointed at `/dev/null`
precisely because launchd never rotates what it captures. `KeepAlive` is
`Crashed`-only, so no exit code — including the configuration-error code 2 —
can create a restart loop. Uninstall unloads the agent and removes the plist
while leaving runs and workspaces untouched.
