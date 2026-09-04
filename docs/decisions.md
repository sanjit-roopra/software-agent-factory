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
