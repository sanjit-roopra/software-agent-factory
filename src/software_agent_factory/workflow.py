"""The single authoritative :class:`WorkflowController`.

Per ``AGENTS.md`` ("One authoritative workflow controller"), only this module
mutates a :class:`~software_agent_factory.models.FactoryRun`'s state. Agents
(via :mod:`software_agent_factory.agents`) return typed artifacts and
outcomes; they never transition the run themselves, and their claims about
what changed on disk are never trusted -- ``changed_files`` and the diff are
always re-derived from :meth:`GitWorktreeWorkspace.collect_evidence`.

The allowed transition table is declared as data (``ALLOWED_TRANSITIONS``)
and enforced by :meth:`WorkflowController.transition`:

```text
CREATED -> TRIAGING -> REFINING -> [RESEARCHING] -> PLANNING -> IMPLEMENTING
    -> VERIFYING -> REVIEWING -> PR_READY [-> PR_CREATED -> CI_RUNNING -> DONE]
```

with bounded loops back to ``IMPLEMENTING`` (verification/review/CI repair),
back to ``PLANNING`` (bounded scope-drift replan), and early exits to
``NEEDS_HUMAN``/``FAILED`` from every non-terminal state.

Terminal states are ``DONE``, ``NEEDS_HUMAN`` and ``FAILED``. ``PR_READY`` is
*not* terminal: with ``pull_request.enabled`` it continues to ``PR_CREATED``.
When pull requests are disabled it is the completed endpoint of the manual
flow, and the controller finalizes it explicitly
(:meth:`WorkflowController.finalize_pr_ready`) by stamping ``completed_at``.

Budgets are derived from persisted state, never from a local counter, so a
restarted process can never grant a run a fresh retry budget (``ADR-003``):

- pre-PR implementer/verification/review/scope repairs share
  ``config.retries.max_total_attempts`` (``AttemptBudget.IMPLEMENTATION``)
- post-PR CI repairs use the separate ``config.ci.repair_attempts``
  (``AttemptBudget.CI_REPAIR``)
- scope replans are bounded by ``config.scope_drift.max_replans``, counted
  from persisted ``AttemptRecord``s triggered by ``AttemptTrigger.SCOPE``
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .agents import AgentRequest, AgentRuntime
from .config import FactoryConfig, RoleModelConfig
from .github import GitHubError, GitPublishError, build_pr_body
from .governance import (
    RepositoryVerificationResult,
    RepositoryVerifier,
    ScopeAssessment,
    ScopeDecision,
    ScopeDriftPolicy,
    assess_publish_gate,
)
from .models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CIReport,
    ExecutionPlan,
    FactoryRun,
    RepairContext,
    RepositoryProfile,
    ResearchReport,
    ReviewReport,
    RunLease,
    SelectedSkill,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .publishing import CIObserver, PullRequestPublisher
from .repository_profile import (
    generic_repository_profile,
    profile_repository,
    skills_for_role,
)
from .routing import ModelRouter
from .store import FileRunStore
from .verification import DeterministicVerifier
from .workspace import (
    GitWorktreeWorkspace,
    WorkspaceError,
    WorkspaceEvidence,
    WorkspaceLockError,
)

logger = logging.getLogger(__name__)

#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.DONE, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
)

#: CI failure categories that may legitimately be repaired by another code
#: change. Everything else (flaky/infra/dependency/unknown/cancelled) is an
#: operator problem, not a code problem, and escalates with evidence.
REPAIRABLE_CI_CATEGORIES: frozenset[str] = frozenset({"CODE_FAILURE", "TEST_FAILURE"})

#: Bound on how much failure text is copied into a repair prompt.
MAX_REPAIR_EXCERPT_CHARS = 4000
MAX_REPAIR_FAILURES = 10

# The single declared transition table. Every non-terminal state may also
# escalate to NEEDS_HUMAN (business decision, e.g. risk/eligibility/scope) or
# FAILED (operational agent/infrastructure failure).
ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset(
        {WorkflowState.TRIAGING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.TRIAGING: frozenset(
        {WorkflowState.REFINING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.REFINING: frozenset(
        {
            WorkflowState.RESEARCHING,
            WorkflowState.PLANNING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.RESEARCHING: frozenset(
        {WorkflowState.PLANNING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.PLANNING: frozenset(
        {WorkflowState.IMPLEMENTING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.IMPLEMENTING: frozenset(
        {WorkflowState.VERIFYING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
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
    WorkflowState.PR_READY: frozenset(
        {WorkflowState.PR_CREATED, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
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
        {WorkflowState.IMPLEMENTING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.DONE: frozenset(),
    WorkflowState.NEEDS_HUMAN: frozenset(),
    WorkflowState.FAILED: frozenset(),
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


class TransitionError(Exception):
    """Raised when a caller attempts a workflow state transition that is
    not present in ``ALLOWED_TRANSITIONS``."""


class WorkItemAlreadyActiveError(Exception):
    """Raised internally when another live run owns this work item's
    workspace. Surfaced as a non-persisted outcome, never as a junk run."""


class _Halt(Exception):
    """Internal control-flow signal: the run has already reached a terminal
    state and been persisted; unwind to the caller of ``run()``."""

    def __init__(self, run: FactoryRun) -> None:
        super().__init__(run.state)
        self.run = run


class WorkflowController:
    """The only object permitted to change a :class:`FactoryRun`'s state."""

    def __init__(
        self,
        config: FactoryConfig,
        store: FileRunStore,
        runtime: AgentRuntime,
        router: ModelRouter | None = None,
        verifier: DeterministicVerifier | None = None,
        *,
        repository_verifier: RepositoryVerifier | None = None,
        scope_policy: ScopeDriftPolicy | None = None,
        publisher: PullRequestPublisher | None = None,
        ci_observer: CIObserver | None = None,
        repository_profiler: Callable[[Path], RepositoryProfile] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runtime = runtime
        self._router = router if router is not None else ModelRouter(config)
        self._verifier = (
            repository_verifier if repository_verifier is not None else RepositoryVerifier(verifier)
        )
        self._scope_policy = scope_policy if scope_policy is not None else ScopeDriftPolicy()
        self._repository_profiler = repository_profiler or profile_repository
        # Constructed eagerly when the integration is enabled so two concurrent
        # runs sharing one controller cannot race on lazy initialization, and
        # so a misconfiguration surfaces before any work is done.
        self._publisher = publisher
        if self._publisher is None and config.pull_request.enabled:
            self._publisher = PullRequestPublisher(config)
        self._ci_observer = ci_observer
        if self._ci_observer is None and config.ci.enabled:
            self._ci_observer = CIObserver(config)

    # -- public transition API -----------------------------------------

    def transition(
        self,
        run: FactoryRun,
        new_state: WorkflowState,
        *,
        failure_reason: str | None = None,
    ) -> FactoryRun:
        """Move ``run`` to ``new_state``, persisting the result.

        Raises :class:`TransitionError` if ``new_state`` is not reachable
        from ``run.state`` per ``ALLOWED_TRANSITIONS``. Every transition
        refreshes ``last_activity_at`` (the scheduler's stall signal) and
        either stamps (terminal) or clears (active) ``completed_at`` so a
        repaired run never keeps a stale completion timestamp.
        """
        allowed = ALLOWED_TRANSITIONS.get(run.state, frozenset())
        if new_state not in allowed:
            raise TransitionError(f"cannot transition from {run.state} to {new_state}")

        now = utc_now()
        updates: dict[str, object] = {
            "state": new_state,
            "updated_at": now,
            "last_activity_at": now,
        }
        if new_state in TERMINAL_STATES:
            updates["completed_at"] = now
            updates["lease"] = None
        else:
            updates["completed_at"] = None
            if run.lease is not None:
                updates["lease"] = run.lease.model_copy(update={"heartbeat_at": now})
        updates["failure_reason"] = failure_reason

        run = run.model_copy(update=updates)
        self._store.save_run(run)
        logger.info(
            "run %s -> %s%s",
            run.id,
            new_state.value,
            f" ({failure_reason})" if failure_reason else "",
            # Correlation fields for the structured JSON log
            # (observability._RedactingJsonFormatter); ignored by a plain
            # console handler, so this costs nothing when logging is not
            # configured.
            extra={"run_id": run.id, "state": new_state.value},
        )
        return run

    def finalize_pr_ready(self, run: FactoryRun) -> FactoryRun:
        """Mark a ``PR_READY`` run as the completed endpoint of the manual
        flow (pull requests disabled). ``PR_READY`` stays reachable for
        PR-enabled runs, so completion is explicit rather than implied by the
        transition itself."""
        if run.state is not WorkflowState.PR_READY:
            raise TransitionError(f"cannot finalize a run in state {run.state}")
        now = utc_now()
        run = run.model_copy(
            update={
                "completed_at": now,
                "updated_at": now,
                "last_activity_at": now,
                "lease": None,
            }
        )
        self._store.save_run(run)
        return run

    def recover_abandoned_run(
        self, run: FactoryRun, reason: str = "run was abandoned by a previous process"
    ) -> FactoryRun:
        """Conservatively move an abandoned, non-terminal run to
        ``NEEDS_HUMAN``.

        Used by the scheduler's startup reconciliation. Deliberately does not
        auto-resume: no paid retry is spent on recovery, the persisted attempt
        budget is left untouched (so a restart can never widen it), and the
        workspace plus every artifact stay on disk for inspection.
        """
        if is_run_finished(run):
            return run
        return self.transition(run, WorkflowState.NEEDS_HUMAN, failure_reason=reason)

    # -- entry point ------------------------------------------------------

    def run(
        self,
        work_item: WorkItem,
        source_repo: Path,
        *,
        run_id: str | None = None,
    ) -> FactoryRun:
        """Synchronously drive ``work_item`` from ``CREATED`` to completion,
        persisting the run and every artifact along the way."""
        resolved_run_id = run_id or f"run-{uuid4().hex}"
        run = FactoryRun(
            id=resolved_run_id,
            work_item_id=work_item.id,
            state=WorkflowState.CREATED,
        )

        try:
            workspace = GitWorktreeWorkspace(
                self._config.data_dir,
                source_repo,
                work_item.id,
                branch_prefix=self._config.repository.branch_prefix,
            )
        except (WorkspaceError, ValueError) as exc:
            self._store.save_run(run)
            self._store.save_artifact(run.id, work_item)
            return self._end_failed(run, f"could not initialize workspace: {exc}")

        try:
            workspace.acquire_lock()
        except WorkspaceLockError as exc:
            # Lock contention means another live run already owns this work
            # item. Returning a *non-persisted* FAILED outcome keeps the run
            # store free of junk runs that reconciliation would later have to
            # explain (see docs/decisions.md ADR-008).
            logger.warning("work item %s is already active: %s", work_item.id, exc)
            return run.model_copy(
                update={
                    "state": WorkflowState.FAILED,
                    "failure_reason": (
                        f"work item {work_item.id!r} is already active in another run "
                        f"(workspace lock held): {exc}"
                    ),
                    "completed_at": utc_now(),
                }
            )

        self._store.save_run(run)
        self._store.save_artifact(run.id, work_item)

        try:
            try:
                workspace_path = workspace.prepare()
            except WorkspaceError as exc:
                return self._end_failed(run, f"could not prepare workspace: {exc}")

            run = run.model_copy(
                update={
                    "workspace_path": str(workspace_path),
                    "branch_name": workspace.branch_name,
                    "updated_at": utc_now(),
                    "last_activity_at": utc_now(),
                    "lease": RunLease(
                        host=socket.gethostname(),
                        pid=os.getpid(),
                        heartbeat_at=utc_now(),
                    ),
                }
            )
            self._store.save_run(run)
            try:
                repository_profile = self._repository_profiler(workspace_path)
            except (OSError, ValueError) as exc:
                repository_profile = generic_repository_profile(
                    warning=f"repository profiling degraded: {exc}"
                )
            self._store.save_artifact(run.id, repository_profile)
            return self._execute(
                run,
                work_item,
                workspace,
                source_repo,
                repository_profile,
            )
        finally:
            # Workspaces are preserved by default (docs/architecture.md,
            # "Workspace lifecycle"): only the lock is released here, the
            # worktree itself is left in place for inspection/reuse.
            workspace.release_lock()

    # -- internal orchestration --------------------------------------------

    def _execute(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
        repository_profile: RepositoryProfile,
    ) -> FactoryRun:
        try:
            workspace_path = str(workspace.path)
            run = self.transition(run, WorkflowState.TRIAGING)
            triage_result = self._run_triage(run, work_item, workspace_path=workspace_path)

            if not triage_result.factory_eligible:
                raise self._halt(
                    run, WorkflowState.NEEDS_HUMAN, "triage marked this work item ineligible"
                )
            if self._router.requires_human_approval(triage_result.risk):
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    f"risk {triage_result.risk} requires human approval",
                )

            run = self.transition(run, WorkflowState.REFINING)
            specification = self._run_refiner(
                run,
                work_item,
                triage_result,
                workspace_path=workspace_path,
            )

            research_report: ResearchReport | None = None
            if triage_result.needs_research:
                run = self.transition(run, WorkflowState.RESEARCHING)
                research_report = self._run_researcher(
                    run,
                    work_item,
                    triage_result,
                    specification,
                    workspace_path=workspace_path,
                )
                run = self.transition(run, WorkflowState.PLANNING)
            else:
                run = self.transition(run, WorkflowState.PLANNING)

            execution_plan = self._run_planner(
                run,
                work_item,
                specification,
                research_report,
                workspace_path=workspace_path,
                repository_profile=repository_profile,
            )

            context = _RunContext(
                work_item=work_item,
                triage_result=triage_result,
                specification=specification,
                research_report=research_report,
                execution_plan=execution_plan,
                repository_profile=repository_profile,
                workspace=workspace,
                source_repo=source_repo,
            )

            run = self.transition(run, WorkflowState.IMPLEMENTING)
            run = self._drive_to_pr_ready(run, context, AttemptBudget.IMPLEMENTATION, None)

            if not self._config.pull_request.enabled:
                return self.finalize_pr_ready(run)

            run = self._publish_and_observe(run, context)
            return run
        except _Halt as halt:
            return halt.run

    def _halt(self, run: FactoryRun, state: WorkflowState, reason: str) -> _Halt:
        run = self.transition(run, state, failure_reason=reason)
        return _Halt(run)

    def _end_failed(self, run: FactoryRun, reason: str) -> FactoryRun:
        return self.transition(run, WorkflowState.FAILED, failure_reason=reason)

    # -- fixed-role agent invocations ---------------------------------------

    def _run_triage(
        self, run: FactoryRun, work_item: WorkItem, *, workspace_path: str
    ) -> TriageResult:
        request = self._build_request(AgentRole.TRIAGE, work_item, workspace_path=workspace_path)
        result = self._runtime.run(request)
        if not result.success or result.triage_result is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "triage agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.triage_result)
        return result.triage_result

    def _run_refiner(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        triage_result: TriageResult,
        *,
        workspace_path: str,
    ) -> Specification:
        request = self._build_request(
            AgentRole.REFINER,
            work_item,
            triage_result=triage_result,
            workspace_path=workspace_path,
        )
        result = self._runtime.run(request)
        if not result.success or result.specification is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "refiner agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.specification)
        return result.specification

    def _run_researcher(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        triage_result: TriageResult,
        specification: Specification,
        *,
        workspace_path: str,
    ) -> ResearchReport:
        """Run the optional researcher exactly once (``PLAN.md`` Phase 8)."""
        request = self._build_request(
            AgentRole.RESEARCHER,
            work_item,
            triage_result=triage_result,
            specification=specification,
            workspace_path=workspace_path,
        )
        result = self._runtime.run(request)
        if not result.success or result.research_report is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "researcher agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.research_report)
        return result.research_report

    def _run_planner(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        specification: Specification,
        research_report: ResearchReport | None,
        repository_profile: RepositoryProfile,
        *,
        workspace_path: str,
        repair_context: RepairContext | None = None,
        diff: str | None = None,
        changed_files: list[str] | None = None,
    ) -> ExecutionPlan:
        request = self._build_request(
            AgentRole.PLANNER,
            work_item,
            specification=specification,
            research_report=research_report,
            workspace_path=workspace_path,
            repair_context=repair_context,
            diff=diff,
            changed_files=changed_files or [],
            selected_skills=list(skills_for_role(repository_profile, AgentRole.PLANNER)),
        )
        result = self._runtime.run(request)
        if not result.success or result.execution_plan is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "planner agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.execution_plan)
        return result.execution_plan

    def _run_tester(
        self,
        run: FactoryRun,
        context: _RunContext,
        evidence: WorkspaceEvidence,
        verification_report: VerificationReport,
        snapshot: int,
    ) -> TestReport:
        """Independent AI tester. Sees controller-derived Git evidence and
        deterministic results only -- never the implementer's own summary."""
        request = self._build_request(
            AgentRole.TESTER,
            context.work_item,
            specification=context.specification,
            execution_plan=context.execution_plan,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
            verification_report=verification_report,
            selected_skills=list(skills_for_role(context.repository_profile, AgentRole.TESTER)),
            workspace_path=str(context.workspace.path),
        )
        result = self._runtime.run(request)
        if not result.success or result.test_report is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "tester agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.test_report, attempt=snapshot)
        return result.test_report

    def _run_reviewer(
        self,
        run: FactoryRun,
        context: _RunContext,
        evidence: WorkspaceEvidence,
        verification_report: VerificationReport,
        test_report: TestReport,
        snapshot: int,
    ) -> ReviewReport:
        request = self._build_request(
            AgentRole.REVIEWER,
            context.work_item,
            specification=context.specification,
            execution_plan=context.execution_plan,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
            verification_report=verification_report,
            test_report=test_report,
            selected_skills=list(skills_for_role(context.repository_profile, AgentRole.REVIEWER)),
            workspace_path=str(context.workspace.path),
        )
        result = self._runtime.run(request)
        if not result.success or result.review_report is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "reviewer agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.review_report, attempt=snapshot)
        return result.review_report

    def _build_request(
        self,
        role: AgentRole,
        work_item: WorkItem,
        *,
        role_model: RoleModelConfig | None = None,
        triage_result: TriageResult | None = None,
        specification: Specification | None = None,
        research_report: ResearchReport | None = None,
        execution_plan: ExecutionPlan | None = None,
        change_set: ChangeSet | None = None,
        diff: str | None = None,
        changed_files: list[str] | None = None,
        verification_report: VerificationReport | None = None,
        test_report: TestReport | None = None,
        repair_context: RepairContext | str | None = None,
        selected_skills: list[SelectedSkill] | None = None,
        workspace_path: str | None = None,
        attempt_number: int | None = None,
    ) -> AgentRequest:
        resolved = role_model if role_model is not None else self._router.model_for_role(role)
        return AgentRequest(
            role=role,
            model=resolved.model,
            reasoning=resolved.reasoning,
            work_item=work_item,
            triage_result=triage_result,
            specification=specification,
            research_report=research_report,
            execution_plan=execution_plan,
            change_set=change_set,
            diff=diff,
            changed_files=changed_files or [],
            verification_report=verification_report,
            test_report=test_report,
            repair_context=repair_context,
            selected_skills=selected_skills or [],
            workspace_path=workspace_path,
            attempt_number=attempt_number,
            timeout_seconds=self._config.agent_timeout_seconds,
        )

    # -- bounded implementation/repair loop ---------------------------------

    def _attempts_used(self, run: FactoryRun, budget: AttemptBudget) -> int:
        """Attempts already spent from ``budget``, derived from persisted
        state so a restart can never reset it (``ADR-003``)."""
        return sum(
            1
            for attempt in run.attempt_records
            if attempt.budget is budget and attempt.role is AgentRole.IMPLEMENTER
        )

    def _replans_used(self, run: FactoryRun) -> int:
        return sum(
            1 for attempt in run.attempt_records if attempt.triggered_by is AttemptTrigger.SCOPE
        )

    def _select_worker(
        self, run: FactoryRun, context: _RunContext, budget: AttemptBudget
    ) -> tuple[RoleModelConfig | None, int]:
        used = self._attempts_used(run, budget)
        attempt_number = used + 1
        if budget is AttemptBudget.CI_REPAIR:
            if used >= self._config.ci.repair_attempts:
                return None, attempt_number
            routing_attempt = min(attempt_number, self._config.retries.max_total_attempts)
            return (
                self._router.model_for_implementer(
                    context.triage_result.complexity, routing_attempt
                ),
                attempt_number,
            )
        return (
            self._router.model_for_implementer(context.triage_result.complexity, attempt_number),
            attempt_number,
        )

    def _budget_exhausted_reason(self, budget: AttemptBudget, used: int) -> str:
        if budget is AttemptBudget.CI_REPAIR:
            return f"CI repair budget exhausted after {used} attempt(s)"
        return f"implementation attempt budget exhausted after {used} attempt(s)"

    def _drive_to_pr_ready(
        self,
        run: FactoryRun,
        context: _RunContext,
        budget: AttemptBudget,
        repair_context: RepairContext | None,
    ) -> FactoryRun:
        """Drive an ``IMPLEMENTING`` run through the deterministic and
        independent gates until it reaches ``PR_READY``.

        Shared by the pre-PR loop and the post-CI repair loop; only the
        consumed :class:`AttemptBudget` differs.
        """
        while True:
            role_model, attempt_number = self._select_worker(run, context, budget)
            if role_model is None:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    self._budget_exhausted_reason(budget, attempt_number - 1),
                )

            # Snapshot directories are keyed by the run-global attempt index so
            # a CI repair (whose per-budget attempt_number restarts at 1) can
            # never overwrite the pre-PR attempt's immutable evidence.
            # attempts/NN therefore always corresponds to attempt_records[NN-1].
            snapshot = len(run.attempt_records) + 1
            trigger = (
                repair_context.trigger if repair_context is not None else AttemptTrigger.INITIAL
            )
            run, implemented, evidence = self._invoke_implementer(
                run,
                attempt_number,
                snapshot,
                role_model,
                context,
                budget,
                trigger,
                repair_context,
            )
            if not implemented:
                repair_context = self._implementer_failure_context(run)
                continue
            assert evidence is not None

            run = self.transition(run, WorkflowState.VERIFYING)
            verification = self._verify(run, context)
            self._store.save_artifact(run.id, verification.report, attempt=snapshot)

            if not verification.report.passed:
                repair_context = self._verification_repair_context(verification)
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            scope = self._scope_policy.assess(
                context.execution_plan,
                evidence.changed_files,
                context.triage_result.risk,
            )
            if scope.decision is ScopeDecision.NEEDS_HUMAN:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "scope drift requires human review: " + _describe_scope(scope),
                )
            if scope.decision is ScopeDecision.REPLAN:
                run, repair_context = self._replan(run, context, scope, evidence)
                continue

            if self._should_polish(run, budget):
                repair_context = self._polish_context()
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            run = self.transition(run, WorkflowState.REVIEWING)
            test_report = self._run_tester(run, context, evidence, verification.report, snapshot)
            review_report = self._run_reviewer(
                run, context, evidence, verification.report, test_report, snapshot
            )

            if not review_report.approved:
                repair_context = self._review_repair_context(review_report, test_report)
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            context.latest_evidence = evidence
            context.latest_verification = verification.report
            context.latest_test_report = test_report
            context.latest_review = review_report
            return self.transition(run, WorkflowState.PR_READY)

    def _replan(
        self,
        run: FactoryRun,
        context: _RunContext,
        scope: ScopeAssessment,
        evidence: WorkspaceEvidence,
    ) -> tuple[FactoryRun, RepairContext]:
        """Bounded scope-drift replan: VERIFYING -> PLANNING -> IMPLEMENTING."""
        used = self._replans_used(run)
        if used >= self._config.scope_drift.max_replans:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                (
                    f"scope drift replan budget exhausted after {used} replan(s): "
                    + _describe_scope(scope)
                ),
            )

        repair_context = RepairContext(
            trigger=AttemptTrigger.SCOPE,
            summary="The previous change drifted outside the planned scope.",
            failures=[finding.message for finding in scope.findings][:MAX_REPAIR_FAILURES],
            log_excerpt=None,
        )
        run = self.transition(run, WorkflowState.PLANNING)
        context.execution_plan = self._run_planner(
            run,
            context.work_item,
            context.specification,
            context.research_report,
            workspace_path=str(context.workspace.path),
            repair_context=repair_context,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
            repository_profile=context.repository_profile,
        )
        run = self.transition(run, WorkflowState.IMPLEMENTING)
        return run, repair_context

    def _verify(self, run: FactoryRun, context: _RunContext) -> RepositoryVerificationResult:
        """Run install -> verify -> build with per-command persisted logs."""
        return self._verifier.run(
            self._config.repository.commands,
            cwd=context.workspace.path,
            run_dir=self._store.run_dir(run.id),
            timeout_seconds=self._config.repository.command_timeout_seconds,
            env_passthrough=self._config.repository.env_passthrough,
            capture_bytes=self._config.repository.log_capture_bytes,
        )

    def _invoke_implementer(
        self,
        run: FactoryRun,
        attempt_number: int,
        snapshot: int,
        role_model: RoleModelConfig,
        context: _RunContext,
        budget: AttemptBudget,
        trigger: AttemptTrigger,
        repair_context: RepairContext | None,
    ) -> tuple[FactoryRun, bool, WorkspaceEvidence | None]:
        started_at = utc_now()
        current_diff = context.latest_evidence.diff if context.latest_evidence else None
        request = AgentRequest(
            role=AgentRole.IMPLEMENTER,
            model=role_model.model,
            reasoning=role_model.reasoning,
            work_item=context.work_item,
            specification=context.specification,
            research_report=context.research_report,
            execution_plan=context.execution_plan,
            repair_context=repair_context,
            diff=current_diff if repair_context is not None else None,
            selected_skills=list(
                skills_for_role(context.repository_profile, AgentRole.IMPLEMENTER)
            ),
            workspace_path=str(context.workspace.path),
            attempt_number=attempt_number,
            timeout_seconds=self._config.agent_timeout_seconds,
        )
        result = self._runtime.run(request)
        completed_at = utc_now()

        if not result.success:
            run = self._record_attempt(
                run,
                attempt_number,
                role_model,
                started_at,
                completed_at,
                outcome="failed",
                failure_reason=result.failure_reason or "implementer reported failure",
                budget=budget,
                trigger=trigger,
            )
            return run, False, None

        # Controller-derived evidence only: the agent's own ChangeSet.changed_files
        # claim is discarded and replaced with what Git actually recorded,
        # including newly created untracked files.
        evidence = context.workspace.collect_evidence()
        reported = result.change_set or ChangeSet(summary="Implementer produced no summary.")
        change_set = reported.model_copy(update={"changed_files": evidence.changed_files})
        self._store.save_artifact(run.id, change_set, attempt=snapshot)
        self._store.save_patch(run.id, evidence.diff, attempt=snapshot)
        context.latest_evidence = evidence

        run = self._record_attempt(
            run,
            attempt_number,
            role_model,
            started_at,
            completed_at,
            outcome="succeeded",
            failure_reason=None,
            budget=budget,
            trigger=trigger,
        )
        return run, True, evidence

    def _record_attempt(
        self,
        run: FactoryRun,
        attempt_number: int,
        role_model: RoleModelConfig,
        started_at: datetime,
        completed_at: datetime,
        *,
        outcome: str,
        failure_reason: str | None,
        budget: AttemptBudget,
        trigger: AttemptTrigger,
    ) -> FactoryRun:
        attempt = AttemptRecord(
            attempt_number=attempt_number,
            role=AgentRole.IMPLEMENTER,
            model=role_model.model,
            reasoning=role_model.reasoning,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            failure_reason=failure_reason,
            budget=budget,
            triggered_by=trigger,
        )
        now = utc_now()
        run = run.model_copy(
            update={
                "attempt_records": [*run.attempt_records, attempt],
                "updated_at": now,
                "last_activity_at": now,
            }
        )
        self._store.save_run(run)
        return run

    # -- repair context builders --------------------------------------------

    def _implementer_failure_context(self, run: FactoryRun) -> RepairContext:
        last = run.attempt_records[-1]
        if last.triggered_by is AttemptTrigger.POLISH:
            return RepairContext(
                trigger=AttemptTrigger.IMPLEMENTER_FAILURE,
                summary=(
                    "The optional polish attempt did not complete. Restore or preserve "
                    "the last verified behavior and resolve any partial polish edits."
                ),
                failures=[last.failure_reason or "polish implementer reported failure"],
                log_excerpt=None,
            )
        return RepairContext(
            trigger=AttemptTrigger.IMPLEMENTER_FAILURE,
            summary="The previous implementation attempt did not complete.",
            failures=[last.failure_reason or "implementer reported failure"],
            log_excerpt=None,
        )

    def _should_polish(self, run: FactoryRun, budget: AttemptBudget) -> bool:
        if budget is not AttemptBudget.IMPLEMENTATION or not self._config.polish.enabled:
            return False
        if any(attempt.triggered_by is AttemptTrigger.POLISH for attempt in run.attempt_records):
            return False
        used = self._attempts_used(run, budget)
        return used + 1 < self._config.retries.max_total_attempts

    def _polish_context(self) -> RepairContext:
        return RepairContext(
            trigger=AttemptTrigger.POLISH,
            summary=(
                "Deterministic verification passed. Apply a final bounded polish and "
                "simplification pass using the factory-selected repository skills. "
                "Preserve required behavior, public interfaces, scope, dependencies, "
                "security checks, and verification policy. Make no edit when no safe "
                "improvement exists."
            ),
            failures=[],
            log_excerpt=None,
        )

    def _verification_repair_context(
        self, verification: RepositoryVerificationResult
    ) -> RepairContext:
        report = verification.report
        failed = [
            check
            for check in report.deterministic_checks
            if check.timed_out or check.exit_code != 0
        ]
        excerpt = None
        if failed:
            last = failed[-1]
            excerpt = _bounded(f"$ {last.command}\n{last.stdout}\n{last.stderr}")
        phase = verification.failed_phase.value if verification.failed_phase else "verify"
        kind = verification.failure_kind.value if verification.failure_kind else "unknown"
        return RepairContext(
            trigger=AttemptTrigger.VERIFICATION,
            summary=f"Deterministic {phase} failed ({kind} failure).",
            failures=list(report.failures)[:MAX_REPAIR_FAILURES],
            log_excerpt=excerpt,
        )

    def _review_repair_context(
        self, review: ReviewReport, test_report: TestReport
    ) -> RepairContext:
        findings = [
            *review.findings,
            *review.scope_concerns,
            *review.security_concerns,
            *review.compatibility_concerns,
            *review.suggested_changes,
        ]
        if not findings:
            findings = ["The independent reviewer rejected the change without detail."]
        excerpt = None
        if test_report.findings:
            excerpt = _bounded("\n".join(test_report.findings))
        return RepairContext(
            trigger=AttemptTrigger.REVIEW,
            summary="The independent reviewer rejected the change.",
            failures=findings[:MAX_REPAIR_FAILURES],
            log_excerpt=excerpt,
        )

    def _ci_repair_context(self, report: CIReport) -> RepairContext:
        failed = report.failed_checks
        excerpts = [
            check.log_excerpt or check.description
            for check in failed
            if check.log_excerpt or check.description
        ]
        return RepairContext(
            trigger=AttemptTrigger.CI,
            summary="Continuous integration reported a failing check.",
            failures=[f"{check.name}: {check.failure_category or 'UNKNOWN'}" for check in failed][
                :MAX_REPAIR_FAILURES
            ],
            log_excerpt=_bounded("\n\n".join(excerpts)) if excerpts else None,
        )

    # -- publishing and CI observation ---------------------------------------

    def _resolve_publisher(self) -> PullRequestPublisher:
        if self._publisher is None:
            self._publisher = PullRequestPublisher(self._config)
        return self._publisher

    def _resolve_ci_observer(self) -> CIObserver:
        if self._ci_observer is None:
            self._ci_observer = CIObserver(self._config)
        return self._ci_observer

    def _publish_and_observe(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        run = self._publish(run, context)
        if not self._config.ci.enabled:
            return self.transition(run, WorkflowState.DONE)
        return self._ci_loop(run, context)

    def _publish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """PR boundary: re-run the deterministic gates, then commit/push/open."""
        evidence = context.latest_evidence
        assert evidence is not None
        changed_files = list(evidence.changed_files)

        gate = assess_publish_gate(
            changed_files,
            max_changed_files=self._config.repository.max_changed_files,
            protected_file_patterns=self._config.repository.protected_file_patterns,
        )
        if not gate.allowed:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "refusing to publish: " + "; ".join(gate.violations),
            )

        scope = self._scope_policy.assess(
            context.execution_plan, changed_files, context.triage_result.risk
        )
        if scope.decision is ScopeDecision.NEEDS_HUMAN:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "scope drift requires human review before publishing: " + _describe_scope(scope),
            )

        publisher = self._resolve_publisher()
        assert run.branch_name is not None
        try:
            base_branch = publisher.resolve_base_branch(context.source_repo)
            result = publisher.publish(
                workspace_path=context.workspace.path,
                branch_name=run.branch_name,
                base_branch=base_branch,
                commit_message=_commit_message(context, run.id),
                title=context.work_item.title,
                body=self._build_pr_body(run, context, changed_files),
                existing_pull_request_url=run.pull_request_url,
            )
        except (GitPublishError, GitHubError) as exc:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                f"could not publish the pull request: {exc}",
            ) from exc

        run = run.model_copy(
            update={
                "commit_sha": result.commit_sha,
                "pull_request_url": result.pull_request_url,
                "updated_at": utc_now(),
            }
        )
        self._store.save_run(run)
        return self.transition(run, WorkflowState.PR_CREATED)

    def _build_pr_body(
        self, run: FactoryRun, context: _RunContext, changed_files: list[str]
    ) -> str:
        return build_pr_body(
            work_item=context.work_item,
            specification=context.specification,
            plan=context.execution_plan,
            changed_files=changed_files,
            verification=context.latest_verification,
            test_report=context.latest_test_report,
            review=context.latest_review,
            run_id=run.id,
        )

    def _ci_loop(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """Poll CI, and repair (bounded by ``ci.repair_attempts``) when the
        failure is genuinely a code/test failure."""
        observer = self._resolve_ci_observer()
        while True:
            run = self.transition(run, WorkflowState.CI_RUNNING)
            assert run.pull_request_url is not None
            report = observer.observe(
                repo_path=context.workspace.path,
                pull_request_url=run.pull_request_url,
                repair_attempts_used=self._attempts_used(run, AttemptBudget.CI_REPAIR),
            )
            self._store.save_artifact(run.id, report)

            if report.timed_out:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "CI checks were still pending after the configured wait budget",
                )
            if report.overall == "PASS":
                return self.transition(run, WorkflowState.DONE)

            run = self.transition(run, WorkflowState.CI_DIAGNOSIS)
            failed = report.failed_checks
            if report.overall == "CANCELLED" or not failed:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    f"CI finished with status {report.overall} and no repairable failure",
                )

            categories = {check.failure_category or "UNKNOWN" for check in failed}
            if not categories <= REPAIRABLE_CI_CATEGORIES:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "CI failure is not repairable by a code change: "
                    + ", ".join(
                        f"{check.name}={check.failure_category or 'UNKNOWN'}" for check in failed
                    ),
                )

            used = self._attempts_used(run, AttemptBudget.CI_REPAIR)
            if used >= self._config.ci.repair_attempts:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    self._budget_exhausted_reason(AttemptBudget.CI_REPAIR, used),
                )

            repair_context = self._ci_repair_context(report)
            run = self.transition(run, WorkflowState.IMPLEMENTING)
            run = self._drive_to_pr_ready(run, context, AttemptBudget.CI_REPAIR, repair_context)
            run = self._publish(run, context)


class _RunContext:
    """Mutable per-run orchestration context.

    Holds only what the controller needs to keep passing to agents. It never
    holds workflow state -- that lives exclusively on the persisted
    ``FactoryRun``.
    """

    def __init__(
        self,
        *,
        work_item: WorkItem,
        triage_result: TriageResult,
        specification: Specification,
        research_report: ResearchReport | None,
        execution_plan: ExecutionPlan,
        repository_profile: RepositoryProfile,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
    ) -> None:
        self.work_item = work_item
        self.triage_result = triage_result
        self.specification = specification
        self.research_report = research_report
        self.execution_plan = execution_plan
        self.repository_profile = repository_profile
        self.workspace = workspace
        self.source_repo = source_repo
        self.latest_evidence: WorkspaceEvidence | None = None
        self.latest_verification: VerificationReport | None = None
        self.latest_test_report: TestReport | None = None
        self.latest_review: ReviewReport | None = None


def _describe_scope(scope: ScopeAssessment) -> str:
    if not scope.findings:
        return "no findings recorded"
    return "; ".join(finding.message for finding in scope.findings)


def _bounded(text: str, limit: int = MAX_REPAIR_EXCERPT_CHARS) -> str:
    """Keep only the tail of a failure excerpt: the end explains the failure."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]


def _commit_message(context: _RunContext, run_id: str) -> str:
    return (
        f"{context.work_item.title}\n\n"
        f"{context.specification.problem.strip()}\n\n"
        f"Factory run: {run_id}\n"
        f"Work item: {context.work_item.id}"
    )
