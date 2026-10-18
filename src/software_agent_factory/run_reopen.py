"""Human-driven restarts for :class:`~software_agent_factory.workflow.WorkflowController`:
reopen a run a person answered (``NEEDS_HUMAN`` to its resume state) and escalate a run
to a higher route with a person's note (ADR-055).

These functions are part of the controller. Each takes the controller as its first
argument and changes the run only through it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import pr_delivery, review_ledger, run_recovery
from .models import (
    MAX_OPEN_REVIEW_FINDINGS,
    REPLY_CURSOR_CLOSED,
    AcceptedReplyReceipt,
    AttemptBudget,
    AttemptTrigger,
    EscalationRecord,
    EscalationStatus,
    ExecutionPlan,
    ExecutionRoute,
    FactoryRun,
    HaltReasonCode,
    PlanDecisionAnswers,
    RepairContext,
    RepositoryProfile,
    ResumeClassification,
    ReviewAcceptance,
    ReviewAcceptanceReason,
    RouteDecision,
    RunLease,
    Specification,
    TriageResult,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .repair_context import PLANNING_RESULT_REQUEST, planner_human_decision_context
from .resume import (
    escalation_route,
    is_valid_delivery_retry_context,
    is_valid_plan_decision_answers,
    is_valid_plan_decision_context,
    is_valid_risk_approval_context,
    receipt_approves_risk_context,
)
from .resume_classes import (
    RESUME_CLASSES,
    is_valid_attempt_grant_context,
    is_valid_findings_acceptance_context,
)
from .run_context import Halt, TransitionError
from .run_recovery import DELIVERY_REPO_CHANGED
from .workspace import GitWorktreeWorkspace, WorkspaceError

if TYPE_CHECKING:
    from .workflow import WorkflowController

logger = logging.getLogger(__name__)


def transition_reopened(controller: WorkflowController, run: FactoryRun) -> FactoryRun:
    """Controller-internal transition from NEEDS_HUMAN to its guarded resume state.

    Guarantees that the run has a durable resume-pending status (REOPENED)
    and an accepted receipt bound to the current run and episode before
    leaving NEEDS_HUMAN, and marks the escalation as RESUMED upon exit.
    """
    if run.state is not WorkflowState.NEEDS_HUMAN:
        raise TransitionError(
            f"cannot reopen run {run.id} in state {run.state}; must be in NEEDS_HUMAN"
        )
    escalation, receipt = _reopen_receipt(run)
    target_state = _reopen_target_state(controller, run, escalation, receipt)

    now = utc_now()
    stage_name = run.state.value
    stage_started_at = run.state_started_at or run.updated_at
    stage_duration_ms = max(0.0, (now - stage_started_at).total_seconds() * 1000.0)
    run.performance.record_duration(
        f"stage.{stage_name}",
        stage_duration_ms,
        stage=stage_name,
        operation="stage",
        accumulate=True,
    )
    updated_receipts = [
        (
            r.model_copy(update={"dispatched_at": now})
            if (
                r.run_id == run.id
                and r.episode_id == escalation.episode_id
                and r.dispatched_at is None
            )
            else r
        )
        for r in escalation.accepted_replies
    ]
    updated_escalation = escalation.model_copy(
        update={
            "status": EscalationStatus.RESUMED,
            "accepted_replies": updated_receipts,
            "updated_at": now,
        }
    )
    updates: dict[str, object] = {
        "state": target_state,
        "updated_at": now,
        "state_started_at": now,
        "last_activity_at": now,
        "completed_at": None,
        "failure_reason": None,
        "escalation": updated_escalation,
    }
    if run.lease is not None:
        updates["lease"] = run.lease.model_copy(update={"heartbeat_at": now})

    run = run.model_copy(update=updates)
    # A stop request filed as the run halted must not stop the resume a person asked for.
    controller._store.delete_stop_request(run.id)
    controller._store.save_run(run)
    logger.info("run %s -> %s (reopened from escalation)", run.id, target_state.value)
    return run


def _reopen_receipt(run: FactoryRun) -> tuple[EscalationRecord, AcceptedReplyReceipt]:
    """Return the REOPENED escalation and the accepted receipt bound to its episode."""
    escalation = run.escalation
    if escalation is None:
        raise ValueError(f"run {run.id} has no escalation record; cannot reopen")
    if escalation.status is not EscalationStatus.REOPENED:
        raise ValueError(
            f"run {run.id} escalation status is {escalation.status}, not REOPENED; cannot reopen"
        )
    receipt = next(
        (
            r
            for r in reversed(escalation.accepted_replies)
            if r.run_id == run.id and r.episode_id == escalation.episode_id
        ),
        None,
    )
    if receipt is None:
        raise ValueError(
            f"run {run.id} has no accepted reply receipt bound to episode {escalation.episode_id}"
        )
    return escalation, receipt


def _reopen_target_state(
    controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> WorkflowState:
    """Check the decision a person gave and return the state the run resumes at."""
    classification = escalation.resume_classification
    spec = RESUME_CLASSES.get(classification)
    check = _REOPEN_CHECKS.get(classification)
    if spec is None or check is None:
        raise ValueError(f"run {run.id} halt category {classification} cannot be reopened")
    check(controller, run, escalation, receipt)
    return spec.reopen_state


def _check_risk_approval(
    _controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    if escalation.approval_context is None or not is_valid_risk_approval_context(
        escalation.approval_context, run.id, escalation.episode_id
    ):
        raise ValueError(f"run {run.id} has missing or invalid risk approval decision context")
    if not receipt_approves_risk_context(receipt, escalation.approval_context):
        raise ValueError(f"run {run.id} receipt fingerprint does not match active approval context")


def _check_plan_decision(
    controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    context = escalation.plan_decision_context
    if context is None or not is_valid_plan_decision_context(
        context, run.id, escalation.episode_id
    ):
        raise ValueError(f"run {run.id} has missing or invalid plan decision context")
    answers = controller._store.load_artifact(run.id, PlanDecisionAnswers)
    if not is_valid_plan_decision_answers(
        answers,
        context,
        run_id=run.id,
        episode_id=escalation.episode_id,
        receipt=receipt,
    ):
        raise ValueError(f"run {run.id} has missing or invalid plan decision answers")
    plan = controller._store.load_artifact(run.id, ExecutionPlan)
    plan_payload = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    plan_fingerprint = hashlib.sha256(plan_payload.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(plan_fingerprint, context.plan_fingerprint):
        raise ValueError(f"run {run.id} execution plan no longer matches decision context")


def _check_delivery_retry(
    _controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    retry = escalation.delivery_retry_context
    if retry is None or not is_valid_delivery_retry_context(retry, run.id, escalation.episode_id):
        raise ValueError(f"run {run.id} has missing or invalid delivery retry context")
    if receipt.delivery_retry_context_fingerprint is None or not secrets.compare_digest(
        receipt.delivery_retry_context_fingerprint, retry.context_fingerprint
    ):
        raise ValueError(f"run {run.id} receipt fingerprint does not match active delivery context")
    if (run.reviewed_tree_sha, run.base_commit_sha, run.branch_name) != (
        retry.reviewed_tree_sha,
        retry.base_commit_sha,
        retry.branch_name,
    ):
        raise ValueError(f"run {run.id} no longer matches its delivery retry context")


def _check_attempt_grant(
    _controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    grant = escalation.attempt_grant_context
    if grant is None or not is_valid_attempt_grant_context(grant, run.id, escalation.episode_id):
        raise ValueError(f"run {run.id} has missing or invalid attempt grant context")
    if receipt.attempt_grant_context_fingerprint is None or not secrets.compare_digest(
        receipt.attempt_grant_context_fingerprint, grant.context_fingerprint
    ):
        raise ValueError(f"run {run.id} receipt fingerprint does not match active grant context")
    if run.attempts_used(grant.budget) != grant.attempts_used:
        raise ValueError(f"run {run.id} no longer matches its attempt grant context")


def _check_findings_acceptance(
    _controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    accept = escalation.findings_acceptance_context
    if accept is None or not is_valid_findings_acceptance_context(
        accept, run.id, escalation.episode_id
    ):
        raise ValueError(f"run {run.id} has missing or invalid findings acceptance context")
    if receipt.findings_acceptance_context_fingerprint is None or not secrets.compare_digest(
        receipt.findings_acceptance_context_fingerprint, accept.context_fingerprint
    ):
        raise ValueError(f"run {run.id} receipt fingerprint does not match active acceptance")
    ledger = run.review_ledger
    if (ledger.last_reviewed_tree_sha, [finding.id for finding in ledger.open_findings]) != (
        accept.reviewed_tree_sha,
        accept.finding_ids,
    ):
        raise ValueError(f"run {run.id} no longer matches its findings acceptance context")


#: The check a reopen runs on the decision a person gave, one per resumable class. Each
#: raises ``ValueError`` when the decision no longer matches the run.
_REOPEN_CHECKS: dict[
    ResumeClassification,
    Callable[[WorkflowController, FactoryRun, EscalationRecord, AcceptedReplyReceipt], None],
] = {
    ResumeClassification.RISK_APPROVAL: _check_risk_approval,
    ResumeClassification.PLAN_DECISION: _check_plan_decision,
    ResumeClassification.DELIVERY_RETRY: _check_delivery_retry,
    ResumeClassification.ATTEMPT_GRANT: _check_attempt_grant,
    ResumeClassification.ACCEPT_FINDINGS: _check_findings_acceptance,
}


def fail_reopen(
    controller: WorkflowController,
    run: FactoryRun,
    reason: str,
    *,
    reason_code: HaltReasonCode = HaltReasonCode.RECOVERY_INTERVENTION,
) -> FactoryRun:
    """Handle a failure during workspace validation or pre-transition reconciliation in reopen.

    Transitions the run out of the dispatchable REOPENED state by persisting
    a new non-resumable escalation episode (status PENDING_NOTIFICATION,
    resume_classification NOT_RESUMABLE, reason_code RECOVERY_INTERVENTION,
    reply_cursor closed). Preserves failure_reason, increments episode_number,
    retains accepted_replies and reopen_count, and clears the lease.
    Prevents tight redispatch loops while safely capturing the failure.
    """
    from .escalation import generate_episode_id

    now = utc_now()
    prev_escalation = run.escalation
    episode_num = (prev_escalation.episode_number + 1) if prev_escalation else 1
    reopen_count = prev_escalation.reopen_count if prev_escalation else 0
    accepted_replies = prev_escalation.accepted_replies if prev_escalation else []

    new_escalation = EscalationRecord(
        episode_id=generate_episode_id(),
        episode_number=episode_num,
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.NOT_RESUMABLE,
        reason_code=reason_code,
        reopen_count=reopen_count,
        accepted_replies=accepted_replies,
        reply_cursor=REPLY_CURSOR_CLOSED,
        created_at=now,
        updated_at=now,
    )

    run = run.model_copy(
        update={
            "state": WorkflowState.NEEDS_HUMAN,
            "failure_reason": reason,
            "escalation": new_escalation,
            "lease": None,
            "completed_at": None,
            "last_activity_at": now,
            "updated_at": now,
        }
    )
    controller._store.save_run(run)
    logger.warning(
        "run %s reopen failed: %s; created non-resumable escalation episode %s",
        run.id,
        reason,
        new_escalation.episode_id,
    )
    return run


def reopen(controller: WorkflowController, run_id: str, source_repo: Path) -> FactoryRun:
    """Controller-owned API to reopen an eligible run halted in NEEDS_HUMAN.

    This is the single controller-owned path out of NEEDS_HUMAN. Validates
    workspace identity, reopen limits, durable resume-pending status,
    accepted reply receipt bound to current run and episode, and the supported
    resumable class (RISK_APPROVAL or PLAN_DECISION -> PLANNING, DELIVERY_RETRY ->
    PR_READY and publish again, ATTEMPT_GRANT -> IMPLEMENTING with one more attempt,
    ACCEPT_FINDINGS -> PR_READY with the open findings accepted). Only ATTEMPT_GRANT adds
    attempt budget, and only one attempt per reopen.
    """
    run = controller._store.load_run(run_id)
    if run.state is not WorkflowState.NEEDS_HUMAN:
        raise ValueError(f"run {run_id} is in state {run.state}, not NEEDS_HUMAN; cannot reopen")

    refused = _refuse_reopen(controller, run)
    if refused is not None:
        return refused

    workspace = GitWorktreeWorkspace(
        controller._config.data_dir,
        source_repo,
        run.work_item_id,
        branch_prefix=controller._config.repository.branch_prefix,
    )
    workspace.acquire_lock()
    try:
        run = controller._store.load_run(run_id)
        if run.state is not WorkflowState.NEEDS_HUMAN:
            return run

        if run.escalation is None or run.escalation.status is not EscalationStatus.REOPENED:
            return run
        reopened_class = run.escalation.resume_classification

        if (
            not workspace.path.is_dir()
            or run.workspace_path is None
            or Path(run.workspace_path).resolve() != workspace.path.resolve()
            or run.branch_name != workspace.branch_name
        ):
            return fail_reopen(
                controller, run, "workspace identity changed or workspace is missing"
            )

        try:
            workspace.prepare()
            if controller._config.merge.enabled:
                assert controller._merger is not None
                repository = controller._merger.validate_repository(source_repo)
                if repository != run.delivery_repository:
                    raise ValueError(DELIVERY_REPO_CHANGED)

            now = utc_now()
            run = run.model_copy(
                update={
                    "lease": RunLease(host=socket.gethostname(), pid=os.getpid(), heartbeat_at=now),
                    "last_activity_at": now,
                    "updated_at": now,
                }
            )
            controller._store.save_run(run)

            if reopened_class is ResumeClassification.DELIVERY_RETRY:
                return reopen_delivery_retry(controller, run, workspace, source_repo)
            if reopened_class is ResumeClassification.ATTEMPT_GRANT:
                return reopen_attempt_grant(controller, run, workspace, source_repo)
            if reopened_class is ResumeClassification.ACCEPT_FINDINGS:
                return reopen_findings_acceptance(controller, run, workspace, source_repo)

            return _resume_reopened(controller, run, workspace, source_repo)
        except Halt as halt:
            return halt.run
        except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
            return fail_reopen(
                controller,
                controller._store.load_run(run_id),
                f"could not reconcile reopened run: {exc}",
            )
    finally:
        workspace.release_lock()


def _refuse_reopen(controller: WorkflowController, run: FactoryRun) -> FactoryRun | None:
    """Check a reopen before taking the lock; return the failed run, or None to go on."""
    escalation = run.escalation
    if escalation is None:
        raise ValueError(f"run {run.id} has no escalation record; cannot reopen")

    if escalation.status is not EscalationStatus.REOPENED:
        raise ValueError(
            f"run {run.id} escalation status is {escalation.status}, not REOPENED; cannot reopen"
        )

    receipt = next(
        (
            r
            for r in reversed(escalation.accepted_replies)
            if r.run_id == run.id and r.episode_id == escalation.episode_id
        ),
        None,
    )
    if receipt is None:
        return fail_reopen(
            controller,
            run,
            f"run {run.id} has no accepted reply receipt bound to episode {escalation.episode_id}",
            reason_code=HaltReasonCode.RECOVERY_INTERVENTION,
        )

    if escalation.resume_classification not in RESUME_CLASSES:
        return fail_reopen(
            controller,
            run,
            f"halt category {escalation.resume_classification} is not resumable via reopen",
            reason_code=HaltReasonCode.MANUAL_INSPECTION,
        )

    if escalation.reopen_count > controller._config.escalation.max_reopens:
        return fail_reopen(
            controller,
            run,
            f"run {run.id} exceeded maximum reopens ({controller._config.escalation.max_reopens})",
            reason_code=HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED,
        )
    return None


def _resume_reopened(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Resume a reopened risk approval or plan decision run at PLANNING."""
    work_item = controller._store.load_artifact(run.id, WorkItem)
    triage_result = controller._store.load_artifact(run.id, TriageResult)
    repository_profile = controller._store.load_artifact(run.id, RepositoryProfile)
    try:
        route_decision = run.route_decision or controller._store.load_artifact(
            run.id, RouteDecision
        )
    except FileNotFoundError:
        route_decision = None

    if route_decision is not None and run.route_decision is None:
        run = run.model_copy(
            update={
                "route_decision": route_decision,
                "initial_route": route_decision.initial_route,
                "effective_route": route_decision.effective_route,
            }
        )
        controller._store.save_run(run)

    if run.escalation is None:
        return fail_reopen(controller, run, "reopened run is missing its escalation record")
    resume_classification = run.escalation.resume_classification
    run = transition_reopened(controller, run)
    if resume_classification is ResumeClassification.PLAN_DECISION:
        previous_plan = controller._store.load_artifact(run.id, ExecutionPlan)
        decision_answers = controller._store.load_artifact(run.id, PlanDecisionAnswers)
        specification = controller._store.load_artifact(run.id, Specification)
        return controller._drive_from_planning(
            run,
            work_item,
            triage_result,
            specification,
            workspace,
            source_repo,
            repository_profile,
            planner_context=planner_human_decision_context(previous_plan, decision_answers),
            route_decision=route_decision,
        )
    return controller._drive_from_planning(
        run,
        work_item,
        triage_result,
        None,
        workspace,
        source_repo,
        repository_profile,
        route_decision=route_decision,
    )


def reopen_delivery_retry(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Reopen a run to ``PR_READY`` and publish its reviewed work again.

    It restores the delivery context first, so work that changed since the halt fails
    closed before the run leaves ``NEEDS_HUMAN``. It plans nothing and spends no attempt.
    """
    run_recovery.require_unchanged_delivery_policy(controller, run)
    context = run_recovery.reconcile_delivery_checkpoint(controller, run, workspace, source_repo)
    run = transition_reopened(controller, run)
    return pr_delivery.publish_and_observe(controller, run, context)


def reopen_attempt_grant(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Reopen a run whose implementation budget ran out at ``IMPLEMENTING`` with one more
    attempt (ADR-058).

    The grant is counted on the run, so the budget allows exactly one attempt more than
    before. The attempt has the trigger ``OPERATOR`` and carries the failure the next attempt
    would have had. If it fails, the run halts again on the same budget.
    """
    context = run_recovery.stored_context(controller, run, workspace, source_repo)
    budget = AttemptBudget.IMPLEMENTATION
    last = run.exhausted_repair
    grants = {**run.attempt_grants, budget: run.attempt_grants.get(budget, 0) + 1}
    run = run.model_copy(update={"attempt_grants": grants, "exhausted_repair": None})
    run = transition_reopened(controller, run)
    repair_context = RepairContext(
        trigger=AttemptTrigger.OPERATOR,
        summary=(
            "A person granted one more attempt after the attempt budget ran out. "
            + (last.summary if last is not None else "Fix the last failure.")
        ),
        failures=list(last.failures) if last is not None else [],
        log_excerpt=last.log_excerpt if last is not None else None,
    )
    run = controller._drive_to_pr_ready(run, context, budget, repair_context)
    if not controller._config.pull_request.enabled:
        return controller.finalize_pr_ready(run)
    return pr_delivery.publish_and_observe(controller, run, context)


def reopen_findings_acceptance(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Reopen a run that stopped on a review impasse at ``PR_READY`` with its open findings
    accepted by a person (ADR-059).

    The findings move to accepted in the ledger and a :class:`ReviewAcceptance` with the reason
    ``OPERATOR`` records them. They are also a ``needs_look`` reason, so the pull request is
    labelled and never merged by the factory. The run publishes the same reviewed tree: the
    delivery checkpoint fails closed if the worktree changed since the review.
    """
    accept = run.escalation.findings_acceptance_context if run.escalation else None
    if accept is None:
        raise ValueError(f"run {run.id} has no findings acceptance context")
    ledger = run.review_ledger
    findings = review_ledger.deduplicate_findings(
        [*ledger.accepted_findings, *ledger.open_findings]
    )
    acceptance = ReviewAcceptance(
        snapshot=max(1, len(run.attempt_records)),
        reason=ReviewAcceptanceReason.OPERATOR,
        risk=controller._store.load_artifact(run.id, TriageResult).risk,
        review_rounds=max(1, sum(1 for a in run.attempt_records if a.reviewed_tree_sha)),
        reviewed_tree_sha=accept.reviewed_tree_sha,
        findings=findings[-MAX_OPEN_REVIEW_FINDINGS:],
    )
    reason = "a person accepted open review findings: " + ", ".join(accept.finding_ids)
    run = run.model_copy(
        update={
            "reviewed_tree_sha": acceptance.reviewed_tree_sha,
            "review_acceptance": acceptance,
            "review_ledger": ledger.model_copy(update={"accepted_findings": acceptance.findings}),
            "needs_look": [*run.needs_look, reason],
        }
    )
    # The checkpoint check reads the acceptance from the run, so it runs before any save. The
    # reopen check still needs the open findings, so they are cleared after the transition.
    context = run_recovery.reconcile_delivery_checkpoint(controller, run, workspace, source_repo)
    run = transition_reopened(controller, run)
    run = run.model_copy(
        update={"review_ledger": review_ledger.clear_open_findings(run.review_ledger)}
    )
    controller._store.save_run(run)
    controller._store.save_artifact(run.id, acceptance, attempt=acceptance.snapshot)
    if not controller._config.pull_request.enabled:
        return controller.finalize_pr_ready(run)
    return pr_delivery.publish_and_observe(controller, run, context)


def escalate(controller: WorkflowController, run_id: str, note: str) -> FactoryRun:
    """Redo a run one route higher with a person's note (ADR-055).

    The run keeps its worktree, branch and pull request. ``CRITIQUE`` implements again with
    the note; ``FULL`` plans again with it first. Fresh attempt budgets start, so the old
    budget does not end the escalation. ``ValueError`` refuses a run that cannot escalate
    and ``WorkspaceLockError`` one that a live process works on.
    """
    run = controller._store.load_run(run_id)
    if run.source_repo is None:
        raise ValueError(f"run {run_id} does not record its repository")
    source_repo = Path(run.source_repo)
    workspace = GitWorktreeWorkspace(
        controller._config.data_dir,
        source_repo,
        run.work_item_id,
        branch_prefix=controller._config.repository.branch_prefix,
    )
    workspace.acquire_lock()
    try:
        run = controller._store.load_run(run_id)
        route = escalation_route(run)
        if route is None:
            raise ValueError(f"run {run_id} cannot escalate from {run.state}")
        run_recovery.require_unchanged_delivery_policy(controller, run)
        if controller._config.merge.enabled:
            assert controller._merger is not None
            if controller._merger.validate_repository(source_repo) != run.delivery_repository:
                raise ValueError(DELIVERY_REPO_CHANGED)
        if not run_recovery.workspace_matches(run, workspace):
            raise ValueError(f"run {run_id} workspace is missing or changed")
        workspace.prepare()
        context = run_recovery.stored_context(controller, run, workspace, source_repo)
        run = controller._record_route(run, context, route, "operator escalation")
        run = run.model_copy(
            update={
                "lease": RunLease(
                    host=socket.gethostname(), pid=os.getpid(), heartbeat_at=utc_now()
                ),
                "budget_start": len(run.attempt_records),
                "attempt_grants": {},
                "exhausted_repair": None,
                "needs_look": [],
            }
        )
        controller._store.delete_stop_request(run.id)
        run = controller.transition(run, WorkflowState.PLANNING, operator_escalation=True)
        repair_context = RepairContext(
            trigger=AttemptTrigger.OPERATOR,
            summary=(
                "A person was not happy with the earlier change and asked for the work "
                "again. Follow their note."
            ),
            failures=[note],
        )
        try:
            if route is ExecutionRoute.CRITIQUE:
                return controller._drive_synthesized(
                    run,
                    context.work_item,
                    context.triage_result,
                    context.route_decision,
                    workspace,
                    source_repo,
                    context.repository_profile,
                    repair_context=repair_context,
                )
            return controller._drive_from_planning(
                run,
                context.work_item,
                context.triage_result,
                None,
                workspace,
                source_repo,
                context.repository_profile,
                planner_context=(
                    f"{repair_context.summary}\n\nNote: {note}\n\n{PLANNING_RESULT_REQUEST}"
                ),
                route_decision=context.route_decision,
                repair_context=repair_context,
            )
        except Halt as halt:
            return halt.run
        except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
            return run_recovery.recover_interrupted_delivery(
                controller,
                controller._store.load_run(run_id),
                workspace,
                source_repo,
                f"escalation failed: {exc}",
            )
    finally:
        workspace.release_lock()
