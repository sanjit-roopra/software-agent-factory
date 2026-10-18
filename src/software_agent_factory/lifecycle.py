"""The run lifecycle as data: the transition table and the finished-run predicate.

It imports only :mod:`software_agent_factory.models`, so read-only callers such
as :mod:`software_agent_factory.observability` can classify a run without
importing the workflow controller. Only
:class:`~software_agent_factory.workflow.WorkflowController` moves a run.
"""

from __future__ import annotations

from .models import FactoryRun, WorkflowState

#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.DONE, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
)

# The single declared transition table. Every non-terminal state may also
# escalate to NEEDS_HUMAN (business decision, e.g. risk/eligibility/scope) or
# FAILED (operational agent/infrastructure failure).
ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset(
        {WorkflowState.TRIAGING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.TRIAGING: frozenset(
        {WorkflowState.PLANNING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    # No new run enters REFINING or RESEARCHING (ADR-035). An old run halted
    # there can only stop for a human, then reopen at PLANNING.
    WorkflowState.REFINING: frozenset({WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}),
    WorkflowState.RESEARCHING: frozenset({WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}),
    WorkflowState.PLANNING: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.VERIFYING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    # Unattended only (ADR-040): a used-up attempt budget publishes the work
    # as it is (PR_READY) or leaves the open pull request as it is (DONE).
    WorkflowState.IMPLEMENTING: frozenset(
        {
            WorkflowState.VERIFYING,
            WorkflowState.PR_READY,
            WorkflowState.DONE,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.VERIFYING: frozenset(
        {
            WorkflowState.REVIEWING,
            WorkflowState.IMPLEMENTING,
            WorkflowState.PLANNING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.REVIEWING: frozenset(
        {
            WorkflowState.PR_READY,
            WorkflowState.IMPLEMENTING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    # Unattended only (ADR-041): a CI repair that cannot be published leaves
    # the open pull request as it is (DONE).
    WorkflowState.PR_READY: frozenset(
        {
            WorkflowState.PR_CREATED,
            WorkflowState.DONE,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.PR_CREATED: frozenset(
        {
            WorkflowState.CI_RUNNING,
            WorkflowState.DONE,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.CI_RUNNING: frozenset(
        {
            WorkflowState.DONE,
            WorkflowState.CI_DIAGNOSIS,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.CI_DIAGNOSIS: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.DONE,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.DONE: frozenset(),
    WorkflowState.NEEDS_HUMAN: frozenset(),
    WorkflowState.FAILED: frozenset(),
}


#: Controller-internal recovery moves outside ``ALLOWED_TRANSITIONS``, by the
#: helper that makes them. A factory restart puts an interrupted run back at an
#: earlier stage (ADR-043). An operator escalation may move a run to
#: ``PLANNING`` (ADR-055). A reopen out of ``NEEDS_HUMAN`` picks its state from
#: the halt category in ``WorkflowController._transition_reopened``.
RECOVERY_TRANSITIONS: dict[str, frozenset[WorkflowState]] = {
    "restart": frozenset(
        {WorkflowState.CREATED, WorkflowState.PLANNING, WorkflowState.IMPLEMENTING}
    ),
    "operator_escalation": frozenset({WorkflowState.PLANNING}),
}


def is_run_finished(run: FactoryRun) -> bool:
    """True when a persisted run needs no further factory work.

    Terminal states always qualify. ``PR_READY`` qualifies only once the
    controller has explicitly finalized it (``completed_at`` stamped), which
    is what distinguishes "the manual, PR-disabled flow completed here" from
    "a PR-enabled run was interrupted at the publishing boundary".
    """
    if run.state in TERMINAL_STATES:
        return True
    return run.state is WorkflowState.PR_READY and run.completed_at is not None
