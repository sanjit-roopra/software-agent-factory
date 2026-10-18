"""Restart and resume for :class:`~software_agent_factory.workflow.WorkflowController`:
continue a run that a factory restart interrupted, reconcile a delivery checkpoint, and
rebuild a run's context from its stored artifacts.

These functions are part of the controller. Each takes the controller as its first
argument and changes the run only through it.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import pr_delivery
from .config import RoleModelConfig
from .lifecycle import RECOVERY_TRANSITIONS, is_run_finished
from .models import (
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    ExecutionPlan,
    FactoryRun,
    RepairContext,
    RepositoryProfile,
    ReviewReport,
    RouteDecision,
    RunLease,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .run_context import (
    SYNTHESIZED_ROUTES,
    Halt,
    RunContext,
    TransitionError,
    delivery_policy_fingerprint,
)
from .workspace import GitWorktreeWorkspace, WorkspaceError

if TYPE_CHECKING:
    from .workflow import WorkflowController

logger = logging.getLogger(__name__)

#: Factory restarts one unattended run may spend before it ends as it is (ADR-043).
MAX_RESTART_RECOVERIES = 2
RESTART_REASON = "interrupted by a factory restart"
DELIVERY_REPO_CHANGED = "delivery repository changed since this run started"
RESTART_REPAIR_CONTEXT = RepairContext(
    trigger=AttemptTrigger.IMPLEMENTER_FAILURE,
    summary=(
        "A factory restart interrupted the previous attempt. The working tree may contain "
        "partial, unverified edits from it; inspect and reconcile them before continuing."
    ),
    failures=[RESTART_REASON],
    log_excerpt=None,
)


def resume(controller: WorkflowController, run_id: str, source_repo: Path) -> FactoryRun:
    """Reconcile a delivery checkpoint without resetting any attempt budget.

    Earlier interrupted agent work is deliberately not replayed: its outcome
    is ambiguous. An unattended run interrupted before a delivery checkpoint
    continues instead (ADR-043). A preserved terminal outcome is never reopened.
    """
    run = controller._store.load_run(run_id)
    if is_run_finished(run):
        return run
    require_unchanged_delivery_policy(controller, run)
    workspace = GitWorktreeWorkspace(
        controller._config.data_dir,
        source_repo,
        run.work_item_id,
        branch_prefix=controller._config.repository.branch_prefix,
    )
    workspace.acquire_lock()
    try:
        run = controller._store.load_run(run_id)
        if is_run_finished(run):
            return run
        if run.state not in {
            WorkflowState.PR_READY,
            WorkflowState.PR_CREATED,
            WorkflowState.CI_RUNNING,
            WorkflowState.CI_DIAGNOSIS,
        }:
            if run.unattended:
                return restart_interrupted_run(controller, run, workspace, source_repo)
            return controller.recover_abandoned_run(
                run,
                "interrupted before a safe delivery checkpoint; "
                "workspace and attempt budgets were preserved",
            )
        if controller._config.merge.enabled:
            assert controller._merger is not None
            repository = controller._merger.validate_repository(source_repo)
            if repository != run.delivery_repository:
                raise ValueError(DELIVERY_REPO_CHANGED)
        if not workspace_matches(run, workspace):
            return recover_interrupted_delivery(
                controller,
                run,
                workspace,
                source_repo,
                "delivery workspace identity changed or the workspace is missing",
            )
        try:
            context = reconcile_delivery_checkpoint(controller, run, workspace, source_repo)
            now = utc_now()
            run = run.model_copy(
                update={
                    "lease": RunLease(host=socket.gethostname(), pid=os.getpid(), heartbeat_at=now),
                    "last_activity_at": now,
                    "updated_at": now,
                }
            )
            controller._store.save_run(run)
            return _continue_delivery(controller, run, context)
        except Halt as halt:
            return halt.run
        except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
            return recover_interrupted_delivery(
                controller,
                controller._store.load_run(run_id),
                workspace,
                source_repo,
                f"could not reconcile delivery checkpoint: {exc}",
            )
    finally:
        workspace.release_lock()


def _continue_delivery(
    controller: WorkflowController, run: FactoryRun, context: RunContext
) -> FactoryRun:
    """Go on from a reconciled delivery checkpoint: publish, watch CI or finish."""
    if run.state is WorkflowState.PR_READY:
        if not controller._config.pull_request.enabled:
            return controller.finalize_pr_ready(run)
        return pr_delivery.publish_and_observe(controller, run, context)
    if not controller._config.pull_request.enabled:
        raise ValueError("cannot resume published work with pull requests disabled")
    if not controller._config.ci.enabled:
        if run.needs_look:
            return pr_delivery.leave_open(controller, run, context)
        return controller.transition(run, WorkflowState.DONE)
    return pr_delivery.ci_loop(controller, run, context)


def workspace_matches(run: FactoryRun, workspace: GitWorktreeWorkspace) -> bool:
    return (
        workspace.path.is_dir()
        and run.workspace_path == str(workspace.path)
        and run.branch_name == workspace.branch_name
    )


def restart_interrupted_run(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Continue an unattended run that a factory restart interrupted before delivery (ADR-043).

    The run keeps its id, its attempt records and its stored artifacts. The attempt an
    interrupted Implementer was making counts as a failed attempt, so a restart never
    widens the attempt budget. The output of interrupted work is ambiguous, so work starts
    again at planning, or from the start when the run stored no triage or lost its
    workspace. A run that a restart interrupted more than ``MAX_RESTART_RECOVERIES`` times
    or while it had a pull request ends as it is, like a run that used its budget.
    """
    try:
        run = count_restart(controller, run)
        work_item = controller._store.load_artifact(run.id, WorkItem)
        if run.restart_recoveries > MAX_RESTART_RECOVERIES or run.pull_request_url is not None:
            return end_restarted_run(controller, run, workspace, source_repo)
        try:
            triage_result = controller._store.load_artifact(run.id, TriageResult)
        except FileNotFoundError:
            triage_result = None
        if triage_result is None or not workspace_matches(run, workspace):
            # Nothing was published, so a clean worktree on the current base loses nothing.
            workspace.discard()
            run = restart_at(controller, run, WorkflowState.CREATED)
            return controller._prepare_and_execute(run, work_item, workspace, source_repo)
        workspace.prepare()
        repository_profile = controller._store.load_artifact(run.id, RepositoryProfile)
        run = restart_at(controller, run, WorkflowState.PLANNING)
        if run.initial_route in SYNTHESIZED_ROUTES:
            return controller._drive_synthesized(
                run,
                work_item,
                triage_result,
                run.route_decision,
                workspace,
                source_repo,
                repository_profile,
                repair_context=RESTART_REPAIR_CONTEXT,
            )
        return controller._drive_from_planning(
            run,
            work_item,
            triage_result,
            None,
            workspace,
            source_repo,
            repository_profile,
            route_decision=run.route_decision,
            repair_context=RESTART_REPAIR_CONTEXT,
        )
    except Halt as halt:
        return halt.run
    except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
        return recover_interrupted_delivery(
            controller,
            controller._store.load_run(run.id),
            workspace,
            source_repo,
            f"could not restart interrupted run: {exc}",
        )


def recover_interrupted_delivery(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
    reason: str,
) -> FactoryRun:
    """Stop an interrupted run that cannot continue (ADR-043).

    An unattended run with a pull request leaves it open, labelled and not merged.
    Every other run stops for a person.
    """
    if run.unattended and run.pull_request_url is not None:
        try:
            context = stored_context(controller, run, workspace, source_repo)
        except (OSError, ValueError):
            pass
        else:
            return pr_delivery.leave_open(
                controller, controller._close_active_invocation(run, reason), context, reason
            )
    return controller.recover_abandoned_run(run, reason)


def count_restart(controller: WorkflowController, run: FactoryRun) -> FactoryRun:
    """Close the interrupted invocation and count this restart.

    The last Implementer invocation with no attempt record counts as one failed attempt
    of its budget. The controller records an attempt only after the Implementer returns,
    so an interruption during the call, or just after it, leaves none.
    """
    run = controller._close_active_invocation(run, RESTART_REASON)
    now = utc_now()
    run = run.model_copy(
        update={
            "restart_recoveries": run.restart_recoveries + 1,
            "lease": RunLease(host=socket.gethostname(), pid=os.getpid(), heartbeat_at=now),
            "last_activity_at": now,
            "updated_at": now,
        }
    )
    controller._store.save_run(run)
    implementer = next(
        (r for r in reversed(run.invocation_records) if r.role is AgentRole.IMPLEMENTER), None
    )
    recorded = {attempt.invocation_number for attempt in run.attempt_records}
    if implementer is not None and implementer.invocation_number not in recorded:
        run = controller._record_attempt(
            run,
            implementer.attempt_number or 1,
            RoleModelConfig(
                model=implementer.model,
                reasoning=implementer.reasoning,
                context_tier=implementer.context_tier,
            ),
            implementer.started_at,
            now,
            outcome="failed",
            failure_reason=RESTART_REASON,
            budget=implementer.budget or AttemptBudget.IMPLEMENTATION,
            trigger=AttemptTrigger.INITIAL,
        )
    return run


def restart_at(controller: WorkflowController, run: FactoryRun, state: WorkflowState) -> FactoryRun:
    """Controller-internal move of an interrupted unattended run back to ``state``.

    The transition table has no edge back to an earlier stage from every state. The
    interrupted stage is not timed, because its duration would include the downtime.
    """
    if state not in RECOVERY_TRANSITIONS["restart"]:
        raise TransitionError(f"cannot restart run {run.id} at {state}")
    now = utc_now()
    run = run.model_copy(
        update={
            "state": state,
            "updated_at": now,
            "state_started_at": now,
            "last_activity_at": now,
            "completed_at": None,
            "failure_reason": None,
        }
    )
    controller._store.save_run(run)
    logger.info("run %s -> %s (restarted after a factory restart)", run.id, state.value)
    return run


def end_restarted_run(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """End a restarted run as it is (ADR-043): publish its work or keep its pull request."""
    reason = (
        f"{RESTART_REASON} while its pull request was open"
        if run.pull_request_url is not None
        else f"{RESTART_REASON} too often to continue"
    )
    try:
        controller._store.load_artifact(run.id, Specification)
        controller._store.load_artifact(run.id, ExecutionPlan)
    except FileNotFoundError:
        return controller._halt(run, WorkflowState.NEEDS_HUMAN, f"{reason}; nothing to publish").run
    workspace.prepare()
    context = stored_context(controller, run, workspace, source_repo)
    run = restart_at(controller, run, WorkflowState.IMPLEMENTING)
    run = pr_delivery.publish_as_is(controller, run, context, reason)
    if run.state is not WorkflowState.PR_READY:
        return run
    if not controller._config.pull_request.enabled:
        return controller.finalize_pr_ready(run)
    return pr_delivery.publish_and_observe(controller, run, context)


def require_unchanged_delivery_policy(controller: WorkflowController, run: FactoryRun) -> None:
    if run.delivery_policy_fingerprint != delivery_policy_fingerprint(controller._config):
        raise ValueError("delivery policy changed since this run started; refusing to resume")


def reconcile_delivery_checkpoint(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> RunContext:
    """Check the delivery workspace against the run and rebuild the delivery context.

    ``resume`` and ``reopen`` of a publish retry share it. It raises when the workspace or
    the reviewed work no longer match the run.
    """
    workspace.prepare()
    pr_delivery.check_delivery_workspace(run, workspace.path)
    return restore_delivery_context(controller, run, workspace, source_repo)


def triage_authorizes_delivery(
    controller: WorkflowController, run: FactoryRun, work_item: WorkItem, triage: TriageResult
) -> bool:
    """Whether the persisted triage lets a resumed run deliver.

    A risk that needs human approval authorizes delivery only when a dispatched
    approval receipt still matches the approval context (see
    :func:`~software_agent_factory.escalation.has_dispatched_risk_approval`).
    """
    from .escalation import has_dispatched_risk_approval

    if not triage.factory_eligible and not run.unattended:
        return False
    if not controller._approval_required(run, triage.risk):
        return True
    return has_dispatched_risk_approval(
        run,
        controller._store,
        config=controller._config,
        work_item=work_item,
        triage_result=triage,
    )


def stored_context(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> RunContext:
    """Rebuild the run context from stored artifacts, without checking the workspace."""
    work_item = controller._store.load_artifact(run.id, WorkItem)
    if work_item.id != run.work_item_id:
        raise ValueError("persisted work item does not match run")
    route_decision: RouteDecision | None = None
    try:
        route_decision = controller._store.load_artifact(run.id, RouteDecision)
    except FileNotFoundError:
        pass
    return RunContext(
        work_item=work_item,
        triage_result=controller._store.load_artifact(run.id, TriageResult),
        specification=controller._store.load_artifact(run.id, Specification),
        execution_plan=controller._store.load_artifact(run.id, ExecutionPlan),
        repository_profile=controller._store.load_artifact(run.id, RepositoryProfile),
        workspace=workspace,
        source_repo=source_repo,
        route_decision=route_decision,
    )


def restore_delivery_context(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> RunContext:
    context = stored_context(controller, run, workspace, source_repo)
    if not triage_authorizes_delivery(controller, run, context.work_item, context.triage_result):
        raise ValueError("persisted triage does not authorize delivery")
    context.latest_evidence = workspace.collect_evidence()
    if context.latest_evidence.diff != controller._store.load_patch(run.id):
        raise ValueError("workspace changes do not match the reviewed delivery checkpoint")
    if not run.reviewed_tree_sha or context.latest_evidence.tree_sha != run.reviewed_tree_sha:
        raise ValueError("workspace tree does not match the reviewed delivery checkpoint")
    if run.base_commit_sha != workspace.base_commit:
        raise ValueError("workspace base does not match the recorded delivery base")
    published_as_is = (
        run.unattended
        and bool(run.needs_look)
        and (
            not run.attempt_records
            or run.attempt_records[-1].reviewed_tree_sha != run.reviewed_tree_sha
        )
    )
    if not published_as_is:
        # A run published as it is after its attempt budget ran out has no
        # passing review to restore (ADR-040).
        context.latest_verification = controller._store.load_artifact(run.id, VerificationReport)
        context.latest_test_report = controller._store.load_artifact(run.id, TestReport)
        context.latest_review = controller._store.load_artifact(run.id, ReviewReport)
        if not context.latest_verification.passed or not controller._review_authorizes_delivery(
            run,
            context.latest_review,
            context.triage_result.risk,
        ):
            raise ValueError(
                "delivery checkpoint has not passed verification and bounded review policy"
            )
    context.polish_attempted = any(
        attempt.triggered_by is AttemptTrigger.POLISH for attempt in run.attempt_records
    )
    return context
