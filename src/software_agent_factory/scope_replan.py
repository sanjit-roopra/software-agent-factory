"""Scope replans for :class:`~software_agent_factory.workflow.WorkflowController`.

A verified diff whose files the plan's scope does not describe gets a metadata-only pass
through ``PLANNING``, bounded by ``config.scope_drift.max_replans``. These functions are part
of the controller. Each takes the controller as its first argument and changes the run only
through it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .governance import ScopeAssessment
from .models import AttemptTrigger, FactoryRun, RepairContext, WorkflowState, utc_now
from .repair_context import MAX_REPAIR_FAILURES
from .run_context import RunContext, describe_scope
from .workspace import WorkspaceEvidence

if TYPE_CHECKING:
    from .workflow import WorkflowController


def replans_used(run: FactoryRun) -> int:
    """Scope replans spent, counting scope attempts recorded before replans had a counter."""
    legacy_scope_attempts = sum(
        1 for attempt in run.attempt_records if attempt.triggered_by is AttemptTrigger.SCOPE
    )
    return max(run.scope_replans, legacy_scope_attempts)


def replan(
    controller: WorkflowController,
    run: FactoryRun,
    context: RunContext,
    scope: ScopeAssessment,
    evidence: WorkspaceEvidence,
) -> FactoryRun:
    """Update scope metadata for an already-green diff without reimplementation."""
    used = replans_used(run)
    if used >= controller._config.scope_drift.max_replans:
        raise controller._halt(
            run,
            WorkflowState.NEEDS_HUMAN,
            (
                f"scope drift replan budget exhausted after {used} replan(s): "
                + describe_scope(scope)
            ),
        )

    repair_context = RepairContext(
        trigger=AttemptTrigger.SCOPE,
        summary=(
            "The existing implementation passed deterministic verification, but its "
            "execution-plan scope metadata does not describe the verified diff. Revise "
            "metadata only; do not request implementation changes."
        ),
        failures=[finding.message for finding in scope.findings][:MAX_REPAIR_FAILURES],
        log_excerpt=None,
    )
    if used == 0:
        controller._store.save_artifact_once(
            run.id,
            context.execution_plan,
            filename="execution-plan.initial.json",
        )
    run = controller.transition(run, WorkflowState.PLANNING)
    # A scope replan changes plan metadata only. It keeps the specification.
    context.execution_plan = controller._run_planner(
        run,
        context.work_item,
        context.specification,
        workspace_path=str(context.workspace.path),
        repair_context=repair_context,
        diff=evidence.diff,
        changed_files=list(evidence.changed_files),
    ).execution_plan
    controller._store.save_artifact_once(
        run.id,
        context.execution_plan,
        filename=f"execution-plan.replan-{used + 1}.json",
    )
    run = run.model_copy(
        update={
            "scope_replans": used + 1,
            "updated_at": utc_now(),
            "last_activity_at": utc_now(),
        }
    )
    controller._store.save_run(run)
    return controller.transition(run, WorkflowState.VERIFYING)
