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
CREATED -> TRIAGING -> PLANNING -> IMPLEMENTING
    -> VERIFYING -> REVIEWING -> PR_READY [-> PR_CREATED -> CI_RUNNING -> DONE]
```

with bounded loops back to ``IMPLEMENTING`` (verification/review/CI repair),
a metadata-only pass through ``PLANNING`` after green scope drift, and early
exits to ``NEEDS_HUMAN``/``FAILED`` from every non-terminal state.

Terminal states are ``DONE``, ``NEEDS_HUMAN`` and ``FAILED``. ``PR_READY`` is
*not* terminal: with ``pull_request.enabled`` it continues to ``PR_CREATED``.
When pull requests are disabled it is the completed endpoint of the manual
flow, and the controller finalizes it explicitly
(:meth:`WorkflowController.finalize_pr_ready`) by stamping ``completed_at``.

The optional post-green polish attempt gets fixed guidance (ADR-034): the
factory's simplify and polish templates and the review lenses for the changed
files. No model writes or selects that guidance.

Budgets are derived from persisted state, never from a local counter, so a
restarted process can never grant a run a fresh retry budget (``ADR-003``):

- pre-PR implementer/verification/review repairs share
  ``config.retries.max_total_attempts`` (``AttemptBudget.IMPLEMENTATION``)
- post-PR CI repairs use the separate ``config.ci.repair_attempts``
  (``AttemptBudget.CI_REPAIR``)
- metadata-only scope replans are bounded by
  ``config.scope_drift.max_replans`` and persisted separately from worker
  attempts
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .agents import (
    AgentRequest,
    AgentResult,
    AgentRuntime,
    is_retryable_typed_artifact_failure,
    runtime_exception_failure_reason,
)
from .command_probe import ProbeLimits, probe_candidates
from .config import (
    FactoryConfig,
    RepositoryCommandsConfig,
    RiskAssessmentConfig,
    RoleModelConfig,
)
from .delivery import DeliveryTarget, fetch_delivery_target
from .github import (
    SHA_PATTERN,
    GitHubClient,
    GitHubError,
    GitPublishError,
    build_pr_body,
    resolve_github_token,
)
from .governance import (
    RepositoryVerificationResult,
    RepositoryVerifier,
    ScopeAssessment,
    ScopeDecision,
    ScopeDriftPolicy,
    assess_publish_gate,
    find_protected_matches,
)
from .models import (
    MAX_GUIDANCE_FINDINGS,
    MAX_OPEN_REVIEW_FINDINGS,
    REPLY_CURSOR_CLOSED,
    UNRESOLVED_DECISIONS_HALT_REASON,
    ActiveInvocation,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CIReport,
    EscalationRecord,
    EscalationStatus,
    ExecutionPlan,
    ExecutionRoute,
    ExpectedScope,
    FactoryRun,
    HaltReasonCode,
    InvocationRecord,
    MutationReport,
    MutationStatus,
    PlanDecisionAnswers,
    PlanningResult,
    PlanStep,
    RepairContext,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    RepositoryProfile,
    ResumeClassification,
    ReviewAcceptance,
    ReviewAcceptanceReason,
    ReviewDispositionStatus,
    ReviewFinding,
    ReviewFindingDraft,
    ReviewFindingOrigin,
    ReviewImpasse,
    ReviewImpasseKind,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
    RouteDecision,
    RunLease,
    Specification,
    TestReport,
    ToolchainInventory,
    ToolchainLane,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .mutation_gate import (
    MutationTarget,
    mutation_check_result,
    mutation_targets,
    run_mutation_gate,
)
from .publishing import CIObserver, PullRequestMerger, PullRequestPublisher
from .repository_profile import (
    generic_repository_profile,
    is_version_file,
    profile_repository,
)
from .resume import (
    is_valid_plan_decision_answers,
    is_valid_plan_decision_context,
    is_valid_risk_approval_context,
    receipt_approves_risk_context,
)
from .routing import (
    COMPLEXITY_ORDER,
    ModelRouter,
    derive_named_paths,
    determine_route,
)
from .store import FileRunStore
from .telemetry import (
    count_operation,
    measure_operation,
    record_gate_failure,
    record_rework,
)
from .toolchain import degraded_toolchain_inventory, inventory_toolchain
from .toolchain_commands import candidate_commands, root_version_files, select_package_runner
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
MAX_LATE_REVIEW_ADOPTION_ROUNDS = 1
MAX_CONSECUTIVE_BLOCKING_REVIEWS_PER_PATH = 3
MAX_CONSECUTIVE_UNRESOLVED_REVIEWS = 3
MAX_CONSECUTIVE_REPLACEMENT_REVIEWS = 2

_DIFF_HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")

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


_PLANNING_RESULT_REQUEST = (
    "Return a complete PlanningResult JSON object with the specification and the execution plan."
)


def _planner_clarification_context(unresolved_decisions: Sequence[str]) -> str:
    decisions_list = "\n".join(f"- {decision}" for decision in unresolved_decisions)
    return (
        "The previous execution plan listed these unresolved decisions:\n"
        f"{decisions_list}\n\n"
        "Resolve any item that repository evidence or existing constraints answer. "
        "Retain only genuinely human-owned choices. " + _PLANNING_RESULT_REQUEST
    )


def _planner_human_decision_context(
    previous_plan: ExecutionPlan,
    decision_answers: PlanDecisionAnswers,
) -> str:
    """Format only durable, validated answers for the replacement planning call."""
    decisions = [
        {
            "decision_number": answer.decision_number,
            "question": previous_plan.unresolved_decisions[answer.decision_number - 1],
            "answer": answer.answer,
        }
        for answer in decision_answers.answers
    ]
    return (
        "An authorized human resolved these previous plan decisions. "
        "Treat each answer as a hard planning constraint. Do not change scope, policy, "
        "budgets, or delivery settings.\n\n"
        f"{json.dumps(decisions, ensure_ascii=True)}\n\n" + _PLANNING_RESULT_REQUEST
    )


def _typed_artifact_repair_context(
    failure_reason: str,
    artifact_name: str,
    prior_context: RepairContext | str | None = None,
) -> str:
    validation_error = failure_reason.split(" stdout=", 1)[0].strip()
    correction = (
        "Your previous response failed deterministic schema validation.\n"
        f"{validation_error}\n"
        f"Correct only the output shape. Return one complete {artifact_name} JSON object. "
        "Do not add markdown or text outside the JSON."
    )
    if prior_context is None:
        return correction
    if isinstance(prior_context, str):
        return f"{prior_context}\n\n{correction}"
    return f"{prior_context.model_dump_json()}\n\n{correction}"


def _review_contract_repair_context(failure_reason: str) -> str:
    return (
        "Your previous ReviewReport passed JSON schema validation but violated the deterministic "
        f"review contract. Contract error: {failure_reason}. Correct the complete ReviewReport "
        "using the typed blocker, disposition, and regression fields exactly as instructed. "
        "Do not change repository files."
    )


class TransitionError(Exception):
    """Raised when a caller attempts a workflow state transition that is
    not present in ``ALLOWED_TRANSITIONS``."""


def delivery_policy_fingerprint(config: FactoryConfig) -> str:
    """Bind recovery to the human policy under which the run was started."""
    payload = config.model_dump(
        mode="json",
        include={"repository", "pull_request", "ci", "merge", "risk", "scope_drift"},
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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
        merger: PullRequestMerger | None = None,
        delivery_base_resolver: Callable[[Path, str], DeliveryTarget] | None = None,
        repository_profiler: Callable[[Path], RepositoryProfile] | None = None,
        toolchain_inventory: Callable[[Path, RepositoryProfile], ToolchainInventory] | None = None,
        github_client: GitHubClient | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runtime = runtime
        self._router = router if router is not None else ModelRouter(config)
        self._command_runner = verifier if verifier is not None else DeterministicVerifier()
        self._verifier = (
            repository_verifier
            if repository_verifier is not None
            else RepositoryVerifier(self._command_runner)
        )
        self._scope_policy = (
            scope_policy
            if scope_policy is not None
            else ScopeDriftPolicy(
                approved_sensitive_files=config.scope_drift.approved_sensitive_files
            )
        )
        self._repository_profiler = repository_profiler or profile_repository
        self._toolchain_inventory = toolchain_inventory or inventory_toolchain
        # Constructed eagerly when the integration is enabled so two concurrent
        # runs sharing one controller cannot race on lazy initialization, and
        # so a misconfiguration surfaces before any work is done.
        self._publisher = publisher
        if self._publisher is None and config.pull_request.enabled:
            self._publisher = PullRequestPublisher(config)
        self._ci_observer = ci_observer
        if self._ci_observer is None and config.ci.enabled:
            self._ci_observer = CIObserver(config)
        self._merger = merger
        if self._merger is None and config.merge.enabled:
            self._merger = PullRequestMerger(config)
        self._delivery_base_resolver = delivery_base_resolver or (
            lambda repo, expected: fetch_delivery_target(config, repo, expected)
        )
        self._github = github_client
        if self._github is None and self._publisher is not None:
            self._github = getattr(self._publisher, "_client", None)
        if self._github is None and (config.escalation.enabled or config.pull_request.enabled):
            self._github = GitHubClient(token=resolve_github_token())

    def with_risk_assessment(self, enabled: bool) -> WorkflowController:
        """Return a controller that starts new runs with ``risk_assessment.enabled`` set.

        Project resume uses it so tasks not yet dispatched follow the project's
        persisted choice. Existing runs always follow their own persisted value.
        """
        if enabled == self._config.risk_assessment.enabled:
            return self
        clone = copy.copy(self)
        clone._config = self._config.model_copy(
            update={"risk_assessment": RiskAssessmentConfig(enabled=enabled)}
        )
        return clone

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
        updates: dict[str, object] = {
            "state": new_state,
            "updated_at": now,
            "state_started_at": now,
            "last_activity_at": now,
        }
        if new_state in TERMINAL_STATES:
            updates["completed_at"] = now
            updates["lease"] = None
            updates["active_invocation"] = None
        else:
            updates["completed_at"] = None
            if run.lease is not None:
                updates["lease"] = run.lease.model_copy(update={"heartbeat_at": now})
        updates["failure_reason"] = failure_reason

        if new_state is WorkflowState.NEEDS_HUMAN:
            from .escalation import (
                build_plan_decision_context,
                build_risk_approval_context,
                classify_halt_reason,
                generate_episode_id,
            )

            classification, code, summary, action = classify_halt_reason(
                run.model_copy(update={"state": new_state, "failure_reason": failure_reason}),
                self._store,
            )
            prev_escalation = run.escalation
            episode_num = (prev_escalation.episode_number + 1) if prev_escalation else 1
            reopen_count = prev_escalation.reopen_count if prev_escalation else 0
            accepted_replies = prev_escalation.accepted_replies if prev_escalation else []
            episode_id = generate_episode_id()
            approval_context = None
            plan_decision_context = None
            remote_resume_enabled = False
            if classification is ResumeClassification.RISK_APPROVAL:
                approval_context = build_risk_approval_context(
                    run=run,
                    store=self._store,
                    config=self._config,
                    episode_id=episode_id,
                )
                if approval_context is not None:
                    from .escalation import build_escalation_comment

                    rendered = build_escalation_comment(
                        run_id=run.id,
                        episode_id=episode_id,
                        classification=classification,
                        reason_code=code,
                        summary=summary,
                        next_action=action,
                        attempts_consumed=len(run.attempt_records),
                        reopen_count=reopen_count,
                        max_reopens=self._config.escalation.max_reopens,
                        approval_context=approval_context,
                    )
                    remote_resume_enabled = getattr(rendered, "remote_resume_enabled", False)
            elif classification is ResumeClassification.PLAN_DECISION:
                plan_decision_context = build_plan_decision_context(
                    run=run,
                    store=self._store,
                    episode_id=episode_id,
                )
                if plan_decision_context is not None:
                    from .escalation import build_escalation_comment

                    rendered = build_escalation_comment(
                        run_id=run.id,
                        episode_id=episode_id,
                        classification=classification,
                        reason_code=code,
                        summary=summary,
                        next_action=action,
                        attempts_consumed=len(run.attempt_records),
                        reopen_count=reopen_count,
                        max_reopens=self._config.escalation.max_reopens,
                        plan_decision_context=plan_decision_context,
                    )
                    remote_resume_enabled = getattr(rendered, "remote_resume_enabled", False)
            updates["escalation"] = EscalationRecord(
                episode_id=episode_id,
                episode_number=episode_num,
                status=EscalationStatus.PENDING_NOTIFICATION,
                resume_classification=classification,
                reason_code=code,
                reopen_count=reopen_count,
                accepted_replies=accepted_replies,
                approval_context=approval_context,
                plan_decision_context=plan_decision_context,
                remote_resume_enabled=remote_resume_enabled,
            )

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
                "active_invocation": None,
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
        if run.active_invocation is not None:
            active = run.active_invocation
            now = utc_now()
            run = run.model_copy(
                update={
                    "invocation_records": [
                        *run.invocation_records,
                        InvocationRecord(
                            invocation_number=active.invocation_number,
                            role=active.role,
                            purpose=active.purpose,
                            model=active.model,
                            reasoning=active.reasoning,
                            context_tier=active.context_tier,
                            started_at=active.started_at,
                            completed_at=now,
                            success=False,
                            failure_reason=reason,
                            attempt_number=active.attempt_number,
                            budget=active.budget,
                        ),
                    ],
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            self._store.save_run(run)
        if run.state is WorkflowState.NEEDS_HUMAN:
            now = utc_now()
            run = run.model_copy(
                update={
                    "failure_reason": reason,
                    "lease": None,
                    "last_activity_at": now,
                    "updated_at": now,
                }
            )
            self._store.save_run(run)
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
        try:
            self._store.load_run(resolved_run_id)
        except FileNotFoundError:
            pass
        else:
            raise ValueError(f"run {resolved_run_id!r} already exists; resume it instead")
        created_at = utc_now()
        run = FactoryRun(
            id=resolved_run_id,
            work_item_id=work_item.id,
            state=WorkflowState.CREATED,
            created_at=created_at,
            updated_at=created_at,
            state_started_at=created_at,
            risk_assessment_enabled=self._config.risk_assessment.enabled,
            unattended=self._config.factory.unattended,
            delivery_policy_fingerprint=delivery_policy_fingerprint(self._config),
        )
        if not run.risk_assessment_enabled:
            logger.warning(
                "run %s: risk assessment is disabled; no risk level stops the run for approval "
                "and triage writes no risk rationale",
                run.id,
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
            delivery_base: str | None = None
            if self._config.merge.enabled:
                assert self._merger is not None
                try:
                    repository = self._merger.validate_repository(source_repo)
                    target = self._delivery_base_resolver(source_repo, repository)
                    if target.repository != repository:
                        raise GitHubError("delivery base resolved from a different repository")
                    delivery_base = target.commit_sha
                except (GitHubError, GitPublishError, OSError) as exc:
                    return self.transition(
                        run,
                        WorkflowState.NEEDS_HUMAN,
                        failure_reason=f"delivery repository is not authorized: {exc}",
                    )
                run = run.model_copy(
                    update={
                        "delivery_repository": repository,
                        "delivery_host": target.host,
                    }
                )
                self._store.save_run(run)
            try:
                with measure_operation(
                    run.performance, "operation.workspace_prepare", operation="workspace_prepare"
                ):
                    workspace_path = (
                        workspace.prepare(base_ref=delivery_base)
                        if delivery_base is not None
                        else workspace.prepare()
                    )
            except WorkspaceError as exc:
                return self._end_failed(run, f"could not prepare workspace: {exc}")

            run = run.model_copy(
                update={
                    "workspace_path": str(workspace_path),
                    "branch_name": workspace.branch_name,
                    "base_commit_sha": workspace.base_commit,
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
                with measure_operation(
                    run.performance,
                    "operation.repository_profile",
                    operation="repository_profile",
                ):
                    repository_profile = self._repository_profiler(workspace_path)
            except (OSError, ValueError) as exc:
                repository_profile = generic_repository_profile(
                    warning=f"repository profiling degraded: {exc}"
                )
            self._store.save_artifact(run.id, repository_profile)
            inventory = self._save_toolchain_inventory(run, workspace_path, repository_profile)
            self._resolve_repository_commands(run, workspace, repository_profile, inventory)
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

    def _save_toolchain_inventory(
        self,
        run: FactoryRun,
        workspace_path: Path,
        repository_profile: RepositoryProfile,
    ) -> ToolchainInventory:
        """Persist the toolchain inventory. It is advisory, so a failure only degrades it."""
        try:
            with measure_operation(
                run.performance,
                "operation.toolchain_inventory",
                operation="toolchain_inventory",
            ):
                inventory = self._toolchain_inventory(workspace_path, repository_profile)
        except (OSError, ValueError) as exc:
            inventory = degraded_toolchain_inventory(
                f"toolchain inventory degraded: {type(exc).__name__}"
            )
        self._store.save_artifact(run.id, inventory)
        return inventory

    def _resolve_repository_commands(
        self,
        run: FactoryRun,
        workspace: GitWorktreeWorkspace,
        repository_profile: RepositoryProfile,
        inventory: ToolchainInventory,
    ) -> None:
        """Persist the commands this run uses (ADR-034).

        Configured commands always win. Without them, and unless derivation is
        turned off, the factory derives commands from the inventory and keeps
        only those that pass on the unchanged base commit.
        """
        configured = self._config.repository.commands
        if configured.install or configured.verify or configured.build:
            plan = RepositoryCommandsPlan(
                source=RepositoryCommandsSource.CONFIG,
                install=tuple(configured.install),
                verify=tuple(configured.verify),
                build=tuple(configured.build),
            )
        elif not self._config.repository.derive_commands:
            plan = RepositoryCommandsPlan(
                source=RepositoryCommandsSource.NONE,
                notes=("command derivation is turned off",),
            )
        else:
            limits = ProbeLimits.from_repository(self._config.repository)
            try:
                with measure_operation(
                    run.performance,
                    "operation.repository_commands",
                    operation="repository_commands",
                ):
                    plan = probe_candidates(
                        self._command_runner,
                        workspace,
                        candidate_commands(inventory, repository_profile),
                        limits,
                    )
            except (OSError, ValueError, WorkspaceError) as exc:
                plan = RepositoryCommandsPlan(
                    source=RepositoryCommandsSource.NONE,
                    notes=(f"command derivation degraded: {type(exc).__name__}",),
                )
        self._store.save_artifact(run.id, plan)

    def _commands_for_run(self, run_id: str) -> RepositoryCommandsConfig:
        """Return the commands a run uses. Runs without a plan use the configuration."""
        try:
            plan = self._store.load_artifact(run_id, RepositoryCommandsPlan)
        except FileNotFoundError:
            return self._config.repository.commands
        return RepositoryCommandsConfig(
            install=list(plan.install), verify=list(plan.verify), build=list(plan.build)
        )

    def resume(self, run_id: str, source_repo: Path) -> FactoryRun:
        """Reconcile a delivery checkpoint without resetting any attempt budget.

        Earlier interrupted agent work is deliberately not replayed: its outcome
        is ambiguous. A preserved terminal outcome is never reopened.
        """
        run = self._store.load_run(run_id)
        if is_run_finished(run):
            return run
        if run.delivery_policy_fingerprint != delivery_policy_fingerprint(self._config):
            raise ValueError("delivery policy changed since this run started; refusing to resume")
        workspace = GitWorktreeWorkspace(
            self._config.data_dir,
            source_repo,
            run.work_item_id,
            branch_prefix=self._config.repository.branch_prefix,
        )
        workspace.acquire_lock()
        try:
            run = self._store.load_run(run_id)
            if is_run_finished(run):
                return run
            if run.state not in {
                WorkflowState.PR_READY,
                WorkflowState.PR_CREATED,
                WorkflowState.CI_RUNNING,
                WorkflowState.CI_DIAGNOSIS,
            }:
                return self.recover_abandoned_run(
                    run,
                    "interrupted before a safe delivery checkpoint; "
                    "workspace and attempt budgets were preserved",
                )
            if self._config.merge.enabled:
                assert self._merger is not None
                repository = self._merger.validate_repository(source_repo)
                if repository != run.delivery_repository:
                    raise ValueError("delivery repository changed since this run started")
            if (
                not workspace.path.is_dir()
                or run.workspace_path != str(workspace.path)
                or run.branch_name != workspace.branch_name
            ):
                return self.recover_abandoned_run(
                    run, "delivery workspace identity changed or the workspace is missing"
                )
            try:
                workspace.prepare()
                self._check_delivery_workspace(run, workspace.path)
                context = self._restore_delivery_context(run, workspace, source_repo)
                now = utc_now()
                run = run.model_copy(
                    update={
                        "lease": RunLease(
                            host=socket.gethostname(), pid=os.getpid(), heartbeat_at=now
                        ),
                        "last_activity_at": now,
                        "updated_at": now,
                    }
                )
                self._store.save_run(run)
                if run.state is WorkflowState.PR_READY:
                    if not self._config.pull_request.enabled:
                        return self.finalize_pr_ready(run)
                    return self._publish_and_observe(run, context)
                if not self._config.pull_request.enabled:
                    raise ValueError("cannot resume published work with pull requests disabled")
                if not self._config.ci.enabled:
                    if run.needs_look:
                        return self._leave_open(run, context)
                    return self.transition(run, WorkflowState.DONE)
                return self._ci_loop(run, context)
            except _Halt as halt:
                return halt.run
            except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
                return self.recover_abandoned_run(
                    self._store.load_run(run_id), f"could not reconcile delivery checkpoint: {exc}"
                )
        finally:
            workspace.release_lock()

    def _transition_reopened(self, run: FactoryRun) -> FactoryRun:
        """Controller-internal transition from NEEDS_HUMAN to its guarded resume state.

        Guarantees that the run has a durable resume-pending status (REOPENED)
        and an accepted receipt bound to the current run and episode before
        leaving NEEDS_HUMAN, and marks the escalation as RESUMED upon exit.
        """
        if run.state is not WorkflowState.NEEDS_HUMAN:
            raise TransitionError(
                f"cannot reopen run {run.id} in state {run.state}; must be in NEEDS_HUMAN"
            )
        escalation = run.escalation
        if escalation is None:
            raise ValueError(f"run {run.id} has no escalation record; cannot reopen")
        if escalation.status is not EscalationStatus.REOPENED:
            raise ValueError(
                f"run {run.id} escalation status is {escalation.status}, "
                "not REOPENED; cannot reopen"
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
                f"run {run.id} has no accepted reply receipt bound to "
                f"episode {escalation.episode_id}"
            )

        target_state: WorkflowState
        if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
            if escalation.approval_context is None or not is_valid_risk_approval_context(
                escalation.approval_context, run.id, escalation.episode_id
            ):
                raise ValueError(
                    f"run {run.id} has missing or invalid risk approval decision context"
                )
            if not receipt_approves_risk_context(receipt, escalation.approval_context):
                raise ValueError(
                    f"run {run.id} receipt fingerprint does not match active approval context"
                )
            # Approval contexts written before ADR-035 name REFINING. Both resume at PLANNING.
            target_state = WorkflowState.PLANNING
        elif escalation.resume_classification is ResumeClassification.PLAN_DECISION:
            context = escalation.plan_decision_context
            if context is None or not is_valid_plan_decision_context(
                context, run.id, escalation.episode_id
            ):
                raise ValueError(f"run {run.id} has missing or invalid plan decision context")
            answers = self._store.load_artifact(run.id, PlanDecisionAnswers)
            if not is_valid_plan_decision_answers(
                answers,
                context,
                run_id=run.id,
                episode_id=escalation.episode_id,
                receipt=receipt,
            ):
                raise ValueError(f"run {run.id} has missing or invalid plan decision answers")
            plan = self._store.load_artifact(run.id, ExecutionPlan)
            plan_payload = json.dumps(
                plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            )
            plan_fingerprint = hashlib.sha256(plan_payload.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(plan_fingerprint, context.plan_fingerprint):
                raise ValueError(f"run {run.id} execution plan no longer matches decision context")
            target_state = WorkflowState.PLANNING
        else:
            raise ValueError(
                f"run {run.id} halt category {escalation.resume_classification} cannot be reopened"
            )

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
        self._store.save_run(run)
        logger.info("run %s -> %s (reopened from escalation)", run.id, target_state.value)
        return run

    def _fail_reopen(
        self,
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
        self._store.save_run(run)
        logger.warning(
            "run %s reopen failed: %s; created non-resumable escalation episode %s",
            run.id,
            reason,
            new_escalation.episode_id,
        )
        return run

    def reopen(self, run_id: str, source_repo: Path) -> FactoryRun:
        """Controller-owned API to reopen an eligible run halted in NEEDS_HUMAN.

        This is the single controller-owned path out of NEEDS_HUMAN. Validates
        workspace identity, reopen limits, durable resume-pending status,
        accepted reply receipt bound to current run and episode, and the supported
        resumable class (RISK_APPROVAL or PLAN_DECISION -> PLANNING) without granting
        additional attempt budget.
        """
        run = self._store.load_run(run_id)
        if run.state is not WorkflowState.NEEDS_HUMAN:
            raise ValueError(
                f"run {run_id} is in state {run.state}, not NEEDS_HUMAN; cannot reopen"
            )

        escalation = run.escalation
        if escalation is None:
            raise ValueError(f"run {run_id} has no escalation record; cannot reopen")

        if escalation.status is not EscalationStatus.REOPENED:
            raise ValueError(
                f"run {run_id} escalation status is {escalation.status}, "
                "not REOPENED; cannot reopen"
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
            return self._fail_reopen(
                run,
                f"run {run_id} has no accepted reply receipt bound to "
                f"episode {escalation.episode_id}",
                reason_code=HaltReasonCode.RECOVERY_INTERVENTION,
            )

        if escalation.resume_classification not in {
            ResumeClassification.RISK_APPROVAL,
            ResumeClassification.PLAN_DECISION,
        }:
            return self._fail_reopen(
                run,
                f"halt category {escalation.resume_classification} is not resumable via reopen",
                reason_code=HaltReasonCode.MANUAL_INSPECTION,
            )

        if escalation.reopen_count > self._config.escalation.max_reopens:
            return self._fail_reopen(
                run,
                f"run {run_id} exceeded maximum reopens ({self._config.escalation.max_reopens})",
                reason_code=HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED,
            )

        workspace = GitWorktreeWorkspace(
            self._config.data_dir,
            source_repo,
            run.work_item_id,
            branch_prefix=self._config.repository.branch_prefix,
        )
        workspace.acquire_lock()
        try:
            run = self._store.load_run(run_id)
            if run.state is not WorkflowState.NEEDS_HUMAN:
                return run

            if run.escalation is None or run.escalation.status is not EscalationStatus.REOPENED:
                return run

            if (
                not workspace.path.is_dir()
                or run.workspace_path is None
                or Path(run.workspace_path).resolve() != workspace.path.resolve()
                or run.branch_name != workspace.branch_name
            ):
                return self._fail_reopen(run, "workspace identity changed or workspace is missing")

            try:
                workspace.prepare()
                if self._config.merge.enabled:
                    assert self._merger is not None
                    repository = self._merger.validate_repository(source_repo)
                    if repository != run.delivery_repository:
                        raise ValueError("delivery repository changed since this run started")

                now = utc_now()
                run = run.model_copy(
                    update={
                        "lease": RunLease(
                            host=socket.gethostname(), pid=os.getpid(), heartbeat_at=now
                        ),
                        "last_activity_at": now,
                        "updated_at": now,
                    }
                )
                self._store.save_run(run)

                work_item = self._store.load_artifact(run.id, WorkItem)
                triage_result = self._store.load_artifact(run.id, TriageResult)
                repository_profile = self._store.load_artifact(run.id, RepositoryProfile)
                try:
                    route_decision = run.route_decision or self._store.load_artifact(
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
                    self._store.save_run(run)

                if run.escalation is None:
                    return self._fail_reopen(run, "reopened run is missing its escalation record")
                resume_classification = run.escalation.resume_classification
                run = self._transition_reopened(run)
                if resume_classification is ResumeClassification.PLAN_DECISION:
                    previous_plan = self._store.load_artifact(run.id, ExecutionPlan)
                    decision_answers = self._store.load_artifact(run.id, PlanDecisionAnswers)
                    specification = self._store.load_artifact(run.id, Specification)
                    return self._drive_from_planning(
                        run,
                        work_item,
                        triage_result,
                        specification,
                        workspace,
                        source_repo,
                        repository_profile,
                        planner_context=_planner_human_decision_context(
                            previous_plan, decision_answers
                        ),
                        route_decision=route_decision,
                    )
                return self._drive_from_planning(
                    run,
                    work_item,
                    triage_result,
                    None,
                    workspace,
                    source_repo,
                    repository_profile,
                    route_decision=route_decision,
                )
            except _Halt as halt:
                return halt.run
            except (OSError, ValueError, WorkspaceError, subprocess.TimeoutExpired) as exc:
                return self._fail_reopen(
                    self._store.load_run(run_id), f"could not reconcile reopened run: {exc}"
                )
        finally:
            workspace.release_lock()

    def _triage_authorizes_delivery(
        self, run: FactoryRun, work_item: WorkItem, triage: TriageResult
    ) -> bool:
        """Whether the persisted triage lets a resumed run deliver.

        A risk that needs human approval authorizes delivery only when a dispatched
        approval receipt still matches the approval context (see
        :func:`~software_agent_factory.escalation.has_dispatched_risk_approval`).
        """
        from .escalation import has_dispatched_risk_approval

        if not triage.factory_eligible and not run.unattended:
            return False
        if not self._approval_required(run, triage.risk):
            return True
        return has_dispatched_risk_approval(
            run,
            self._store,
            config=self._config,
            work_item=work_item,
            triage_result=triage,
        )

    def _restore_delivery_context(
        self, run: FactoryRun, workspace: GitWorktreeWorkspace, source_repo: Path
    ) -> _RunContext:
        work_item = self._store.load_artifact(run.id, WorkItem)
        if work_item.id != run.work_item_id:
            raise ValueError("persisted work item does not match run")
        triage = self._store.load_artifact(run.id, TriageResult)
        if not self._triage_authorizes_delivery(run, work_item, triage):
            raise ValueError("persisted triage does not authorize delivery")
        route_decision: RouteDecision | None = None
        try:
            route_decision = self._store.load_artifact(run.id, RouteDecision)
        except FileNotFoundError:
            pass
        context = _RunContext(
            work_item=work_item,
            triage_result=triage,
            specification=self._store.load_artifact(run.id, Specification),
            execution_plan=self._store.load_artifact(run.id, ExecutionPlan),
            repository_profile=self._store.load_artifact(run.id, RepositoryProfile),
            workspace=workspace,
            source_repo=source_repo,
            route_decision=route_decision,
        )
        context.latest_evidence = workspace.collect_evidence()
        if context.latest_evidence.diff != self._store.load_patch(run.id):
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
            context.latest_verification = self._store.load_artifact(run.id, VerificationReport)
            context.latest_test_report = self._store.load_artifact(run.id, TestReport)
            context.latest_review = self._store.load_artifact(run.id, ReviewReport)
            if not context.latest_verification.passed or not self._review_authorizes_delivery(
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

    @staticmethod
    def _check_delivery_workspace(run: FactoryRun, path: Path) -> None:
        def git_output(*args: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise WorkspaceError(
                    f"could not inspect delivery workspace: {result.stderr.strip()}"
                )
            return result.stdout.strip()

        if git_output("branch", "--show-current") != run.branch_name:
            raise WorkspaceError("delivery workspace is on a different branch")
        if run.state in {WorkflowState.PR_CREATED, WorkflowState.CI_RUNNING}:
            if not run.commit_sha or git_output("rev-parse", "HEAD") != run.commit_sha:
                raise WorkspaceError("published head no longer matches the delivery workspace")
            if git_output("status", "--porcelain"):
                raise WorkspaceError("published delivery workspace has unreviewed changes")

    # -- internal orchestration --------------------------------------------

    def _synthesize_triage_result(
        self, work_item: WorkItem, route_decision: RouteDecision
    ) -> TriageResult:
        if route_decision.selected_worker_complexity is None:
            raise ValueError(
                f"Route {route_decision.effective_route.value} requires selected worker complexity"
            )
        if route_decision.selected_risk is None:
            raise ValueError(
                f"Route {route_decision.effective_route.value} requires a selected risk"
            )
        return TriageResult(
            factory_eligible=True,
            complexity=route_decision.selected_worker_complexity,
            risk=route_decision.selected_risk,
            dependencies=[],
            unknowns=[],
            confidence=route_decision.confidence if route_decision.confidence is not None else 1.0,
            risk_rationale=None,
            provenance="SYNTHESIZED",
        )

    def _synthesize_specification(self, work_item: WorkItem) -> Specification:
        return Specification(
            problem=f"SYNTHESIZED: {work_item.description}",
            acceptance_criteria=list(work_item.acceptance_criteria),
            constraints=list(work_item.constraints),
            assumptions=["SYNTHESIZED: Direct single-pass execution without a planner call"],
            unknowns=[],
            dependencies=[],
            risk_flags=[],
            confidence=1.0,
            provenance="SYNTHESIZED",
        )

    def _synthesize_execution_plan(
        self,
        work_item: WorkItem,
        verify_commands: Sequence[str],
    ) -> ExecutionPlan:
        # Synthesized plan scope cannot whitelist every repository root.
        # Derive only validated repository-relative paths explicitly named in the work item.
        modules = derive_named_paths(work_item)
        if "FACTORY_NOTES.md" not in modules:
            modules.append("FACTORY_NOTES.md")

        return ExecutionPlan(
            summary=f"SYNTHESIZED: {work_item.title}",
            steps=[
                PlanStep(
                    id="step-1",
                    goal=f"Implement: {work_item.title}",
                    likely_files=modules[:10],
                    validation=list(work_item.acceptance_criteria),
                )
            ],
            expected_scope=ExpectedScope(
                modules=modules,
                estimated_files_min=1,
                estimated_files_max=self._config.routing.single_max_changed_files,
            ),
            test_strategy=list(verify_commands),
            risks=[],
            unresolved_decisions=[],
            provenance="SYNTHESIZED",
        )

    def _ratchet_route(
        self,
        run: FactoryRun,
        context: _RunContext,
        to_route: ExecutionRoute,
        reason: str,
    ) -> FactoryRun:
        """Monotonically upgrade the effective route and record the adjustment."""
        current = context.effective_route
        route_precedence = {
            ExecutionRoute.MANUAL_TRIAGE: 0,
            ExecutionRoute.SINGLE: 1,
            ExecutionRoute.CRITIQUE: 2,
            ExecutionRoute.FULL_REVIEW: 3,
            ExecutionRoute.FULL: 4,
        }
        if route_precedence.get(to_route, 0) <= route_precedence.get(current, 0):
            return run  # Monotonic: never downgrade

        logger.info(
            "Ratcheting route for run %s: %s -> %s (reason: %s)",
            run.id,
            current,
            to_route,
            reason,
        )
        if context.route_decision is not None:
            context.route_decision.record_adjustment(to_route, reason)
            self._store.save_artifact(run.id, context.route_decision)
        context.effective_route = to_route
        run = run.model_copy(
            update={
                "effective_route": to_route,
                "route_decision": context.route_decision,
                "updated_at": utc_now(),
            }
        )
        self._store.save_run(run)
        record_rework(
            run.performance,
            f"route_ratchet.{to_route.value}",
            stage=run.state.value,
        )
        return run

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

            # Route decision right after profiling while in TRIAGING, before triage agent
            route_decision = determine_route(
                work_item,
                repository_profile,
                self._config,
            )
            self._store.save_artifact(run.id, route_decision)
            run = run.model_copy(
                update={
                    "initial_route": route_decision.initial_route,
                    "effective_route": route_decision.effective_route,
                    "route_decision": route_decision,
                    "updated_at": utc_now(),
                }
            )
            self._store.save_run(run)

            # Record observability
            run.performance.record_counter(
                f"route.{route_decision.effective_route.value}",
                1,
                stage=WorkflowState.TRIAGING.value,
                operation="routing",
            )

            # 1. Manual / Abstain route
            if (
                route_decision.effective_route is ExecutionRoute.MANUAL_TRIAGE
                and not run.unattended
            ):
                reason = (
                    route_decision.fallback_reason
                    or f"routing selected manual triage: {route_decision.selected_option}"
                )
                raise self._halt(run, WorkflowState.NEEDS_HUMAN, reason)

            # 2. FULL route. Unattended runs take it instead of manual triage.
            if route_decision.effective_route in {
                ExecutionRoute.FULL,
                ExecutionRoute.MANUAL_TRIAGE,
            }:
                triage_result = self._run_triage(run, work_item, workspace_path=workspace_path)
                if (
                    route_decision.source != "disabled"
                    and route_decision.selected_worker_complexity is not None
                ):
                    if COMPLEXITY_ORDER.index(
                        route_decision.selected_worker_complexity
                    ) > COMPLEXITY_ORDER.index(triage_result.complexity):
                        triage_result = triage_result.model_copy(
                            update={"complexity": route_decision.selected_worker_complexity}
                        )
                        self._store.save_artifact(run.id, triage_result)

                if not triage_result.factory_eligible and not run.unattended:
                    raise self._halt(
                        run, WorkflowState.NEEDS_HUMAN, "triage marked this work item ineligible"
                    )
                if self._approval_required(run, triage_result.risk):
                    raise self._halt(
                        run,
                        WorkflowState.NEEDS_HUMAN,
                        f"risk {triage_result.risk} requires human approval",
                    )

                run = self.transition(run, WorkflowState.PLANNING)
                return self._drive_from_planning(
                    run,
                    work_item,
                    triage_result,
                    None,
                    workspace,
                    source_repo,
                    repository_profile,
                    route_decision=route_decision,
                )

            # 3. SINGLE or CRITIQUE route
            saved_calls = 4 if route_decision.effective_route is ExecutionRoute.SINGLE else 3
            run.performance.record_counter(
                "route.saved_calls",
                saved_calls,
                stage=WorkflowState.TRIAGING.value,
                operation="routing",
            )

            triage_result = self._synthesize_triage_result(work_item, route_decision)
            self._store.save_artifact(run.id, triage_result)

            run = self.transition(run, WorkflowState.PLANNING)
            specification = self._synthesize_specification(work_item)
            self._store.save_artifact(run.id, specification)
            execution_plan = self._synthesize_execution_plan(
                work_item, self._commands_for_run(run.id).verify
            )
            self._store.save_artifact(run.id, execution_plan)

            context = _RunContext(
                work_item=work_item,
                triage_result=triage_result,
                specification=specification,
                execution_plan=execution_plan,
                repository_profile=repository_profile,
                workspace=workspace,
                source_repo=source_repo,
                route_decision=route_decision,
                original_synthesized_scope=tuple(execution_plan.expected_scope.modules),
            )

            run = self.transition(run, WorkflowState.IMPLEMENTING)
            run = self._drive_to_pr_ready(run, context, AttemptBudget.IMPLEMENTATION, None)

            if not self._config.pull_request.enabled:
                return self.finalize_pr_ready(run)

            return self._publish_and_observe(run, context)
        except _Halt as halt:
            return halt.run

    def _drive_from_planning(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        triage_result: TriageResult,
        specification: Specification | None,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
        repository_profile: RepositoryProfile,
        *,
        planner_context: str | None = None,
        route_decision: RouteDecision | None = None,
    ) -> FactoryRun:
        """Plan and continue from a controller-owned PLANNING state.

        One planner call writes the specification and the plan (ADR-035). A
        given ``specification`` is the one an earlier planner call wrote.
        """
        if run.state is not WorkflowState.PLANNING:
            raise TransitionError(f"run {run.id} must be PLANNING before plan execution")
        workspace_path = str(workspace.path)
        planning = self._run_planner(
            run,
            work_item,
            specification,
            triage_result=triage_result,
            workspace_path=workspace_path,
            repair_context=planner_context,
        )
        if planning.execution_plan.unresolved_decisions:
            clarification_context = _planner_clarification_context(
                planning.execution_plan.unresolved_decisions
            )
            if planner_context is not None:
                clarification_context = f"{planner_context}\n\n{clarification_context}"
            planning = self._run_planner(
                run,
                work_item,
                planning.specification,
                triage_result=triage_result,
                workspace_path=workspace_path,
                repair_context=clarification_context,
            )
        specification = planning.specification
        execution_plan = planning.execution_plan
        self._store.save_artifact(run.id, specification)
        if execution_plan.unresolved_decisions and not run.unattended:
            raise self._halt(run, WorkflowState.NEEDS_HUMAN, UNRESOLVED_DECISIONS_HALT_REASON)

        context = _RunContext(
            work_item=work_item,
            triage_result=triage_result,
            specification=specification,
            execution_plan=execution_plan,
            repository_profile=repository_profile,
            workspace=workspace,
            source_repo=source_repo,
            route_decision=route_decision,
        )

        run = self.transition(run, WorkflowState.IMPLEMENTING)
        run = self._drive_to_pr_ready(run, context, AttemptBudget.IMPLEMENTATION, None)

        if not self._config.pull_request.enabled:
            return self.finalize_pr_ready(run)

        return self._publish_and_observe(run, context)

    def _halt(self, run: FactoryRun, state: WorkflowState, reason: str) -> _Halt:
        run = self.transition(run, state, failure_reason=reason)
        if (
            state is WorkflowState.NEEDS_HUMAN
            and self._config.escalation.enabled
            and self._github is not None
        ):
            try:
                from .escalation import deliver_escalation_notification

                workspace_path = (
                    Path(run.workspace_path) if run.workspace_path else Path(self._config.data_dir)
                )
                run = deliver_escalation_notification(
                    run, self._store, self._config, self._github, workspace_path
                )
            except Exception as exc:
                logger.debug("escalation notice delivery failed: %s", exc)
        return _Halt(run)

    def _sensitive_scope_reason(
        self,
        paths: list[str],
        *,
        execution_plan: ExecutionPlan,
        risk: Risk,
        repository_profile: RepositoryProfile,
        scope: ScopeAssessment | None = None,
    ) -> str | None:
        cleaned_paths = [path for path in paths if path and path.strip()]
        protected = sorted(
            set(
                find_protected_matches(
                    cleaned_paths,
                    self._config.repository.protected_file_patterns,
                )
            )
        )
        if protected:
            return f"scope includes protected files: {', '.join(protected)}"

        version_files = set(repository_profile.version_files)
        manifest_paths = sorted(
            {
                path
                for path in cleaned_paths
                if path in version_files or is_version_file(PurePosixPath(path).name)
            }
        )
        if manifest_paths:
            return f"scope includes manifest or version files: {', '.join(manifest_paths)}"

        assessed = scope or self._scope_policy.assess(execution_plan, cleaned_paths, risk)
        sensitive_paths = sorted(
            {path for finding in assessed.findings if finding.sensitive for path in finding.paths}
            | {
                path
                for path in cleaned_paths
                if self._scope_policy._is_ci_workflow_path(path)
                or self._scope_policy._is_migration_path(path)
                or self._scope_policy._is_infrastructure_path(path)
            }
        )
        if sensitive_paths:
            return f"scope includes sensitive files: {', '.join(sensitive_paths)}"
        return None

    def _end_failed(self, run: FactoryRun, reason: str) -> FactoryRun:
        return self.transition(run, WorkflowState.FAILED, failure_reason=reason)

    # -- fixed-role agent invocations ---------------------------------------

    def _run_triage(
        self, run: FactoryRun, work_item: WorkItem, *, workspace_path: str
    ) -> TriageResult:
        request = self._build_request(AgentRole.TRIAGE, work_item, workspace_path=workspace_path)
        result: AgentResult | None = None
        repair_context: str | None = None
        for attempt_number in range(1, self._config.retries.same_model_attempts + 1):
            result = self._invoke_agent(
                run,
                request.model_copy(
                    update={
                        "attempt_number": attempt_number,
                        "repair_context": repair_context,
                        "risk_assessment_enabled": run.risk_assessment_enabled,
                    }
                ),
            )
            if result.success and result.triage_result is not None:
                rejection = self._triage_rationale_rejection(run, result.triage_result)
                if rejection is None:
                    self._store.save_artifact(run.id, result.triage_result)
                    return result.triage_result
                result = rejection
            if not is_retryable_typed_artifact_failure(result, TriageResult):
                break
            assert result.failure_reason is not None
            repair_context = _typed_artifact_repair_context(
                result.failure_reason,
                TriageResult.__name__,
            )
        assert result is not None
        raise self._halt(
            run,
            WorkflowState.FAILED,
            result.failure_reason or "triage agent failed to produce a result",
        )

    def _approval_required(self, run: FactoryRun, risk: Risk) -> bool:
        """Whether ``risk`` stops ``run`` for a human.

        The choice the run started with wins over the current configuration, so one
        run never changes policy on resume or reopen.
        """
        return (
            not run.unattended
            and run.risk_assessment_enabled
            and self._config.risk[risk].human_approval
        )

    def _triage_rationale_rejection(
        self, run: FactoryRun, triage: TriageResult
    ) -> AgentResult | None:
        """Reject a triage with no risk rationale, but only while risk assessment is on."""
        if not (run.risk_assessment_enabled and triage.lacks_required_risk_rationale()):
            return None
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=False,
            failure_reason=(
                f"TRIAGE response did not validate as {TriageResult.__name__}: "
                f"risk_rationale is required when risk is {triage.risk}"
            ),
        )

    def _run_planner(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        specification: Specification | None,
        *,
        triage_result: TriageResult | None = None,
        workspace_path: str,
        repair_context: RepairContext | str | None = None,
        diff: str | None = None,
        changed_files: list[str] | None = None,
    ) -> PlanningResult:
        """Run the planner. It returns the specification and the plan (ADR-035).

        Only the plan is saved here. The caller decides whether to keep the
        returned specification.
        """
        request = self._build_request(
            AgentRole.PLANNER,
            work_item,
            triage_result=triage_result,
            specification=specification,
            workspace_path=workspace_path,
            repair_context=repair_context,
            diff=diff,
            changed_files=changed_files or [],
        )
        result: AgentResult | None = None
        current_repair_context: RepairContext | str | None = repair_context
        for attempt_number in range(1, self._config.retries.same_model_attempts + 1):
            result = self._invoke_agent(
                run,
                request.model_copy(
                    update={
                        "attempt_number": attempt_number,
                        "repair_context": current_repair_context,
                    }
                ),
            )
            if (
                result.success
                and result.specification is not None
                and result.execution_plan is not None
            ):
                self._store.save_artifact(run.id, result.execution_plan)
                return PlanningResult(
                    specification=result.specification,
                    execution_plan=result.execution_plan,
                )
            if not is_retryable_typed_artifact_failure(result, PlanningResult):
                break
            assert result.failure_reason is not None
            current_repair_context = _typed_artifact_repair_context(
                result.failure_reason,
                PlanningResult.__name__,
                prior_context=repair_context,
            )
        assert result is not None
        raise self._halt(
            run,
            WorkflowState.FAILED,
            result.failure_reason or "planner agent failed to produce a result",
        )

    def _run_tester(
        self,
        run: FactoryRun,
        context: _RunContext,
        evidence: WorkspaceEvidence,
        verification_report: VerificationReport,
        snapshot: int,
        repair_diff: str | None,
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
            prior_review_findings=list(run.review_ledger.open_findings),
            accepted_review_findings=list(run.review_ledger.accepted_findings),
            repair_diff=repair_diff,
            workspace_path=str(context.workspace.path),
            attempt_number=snapshot,
        )
        result: AgentResult | None = None
        repair_context: str | None = None
        for _ in range(self._config.retries.same_model_attempts):
            result = self._invoke_agent(
                run,
                request.model_copy(update={"repair_context": repair_context}),
            )
            if result.success and result.test_report is not None:
                if result.test_report.skipped or result.test_report.provenance == "SKIPPED":
                    raise self._halt(
                        run,
                        WorkflowState.FAILED,
                        "agent returned TestReport claiming skipped/provenance SKIPPED; "
                        "only controller may synthesize skipped reports",
                    )
                self._store.save_artifact(run.id, result.test_report, attempt=snapshot)
                return result.test_report
            if not is_retryable_typed_artifact_failure(result, TestReport):
                break
            assert result.failure_reason is not None
            repair_context = _typed_artifact_repair_context(
                result.failure_reason,
                TestReport.__name__,
            )
        assert result is not None
        raise self._halt(
            run,
            WorkflowState.FAILED,
            result.failure_reason or "tester agent failed to produce a result",
        )

    def _run_reviewer(
        self,
        run: FactoryRun,
        context: _RunContext,
        evidence: WorkspaceEvidence,
        verification_report: VerificationReport,
        test_report: TestReport | None,
        snapshot: int,
        repair_diff: str | None,
    ) -> ReviewReport:
        prior_findings = list(run.review_ledger.open_findings)
        request = self._build_request(
            AgentRole.REVIEWER,
            context.work_item,
            specification=context.specification,
            execution_plan=context.execution_plan,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
            verification_report=verification_report,
            test_report=test_report,
            prior_review_findings=prior_findings,
            accepted_review_findings=list(run.review_ledger.accepted_findings),
            repair_diff=repair_diff,
            dependency_names=_dependency_names(context.repository_profile),
            workspace_path=str(context.workspace.path),
            attempt_number=snapshot,
        )
        result: AgentResult | None = None
        repair_context: str | None = None
        semantic_failure: str | None = None
        for _ in range(self._config.retries.same_model_attempts):
            result = self._invoke_agent(
                run,
                request.model_copy(update={"repair_context": repair_context}),
            )
            if result.success and result.review_report is not None:
                if result.review_report.skipped or result.review_report.provenance == "SKIPPED":
                    raise self._halt(
                        run,
                        WorkflowState.FAILED,
                        "agent returned ReviewReport claiming skipped/provenance SKIPPED; "
                        "only controller may synthesize skipped reports",
                    )
                semantic_failure = self._review_contract_failure(
                    result.review_report,
                    prior_findings,
                    evidence,
                    context.workspace,
                )
                if semantic_failure is None:
                    return result.review_report
                repair_context = _review_contract_repair_context(semantic_failure)
                continue
            if not is_retryable_typed_artifact_failure(result, ReviewReport):
                break
            assert result.failure_reason is not None
            repair_context = _typed_artifact_repair_context(
                result.failure_reason,
                ReviewReport.__name__,
            )
        assert result is not None
        raise self._halt(
            run,
            WorkflowState.FAILED,
            semantic_failure
            or result.failure_reason
            or "reviewer agent failed to produce a result",
        )

    def _review_contract_failure(
        self,
        review: ReviewReport,
        prior_findings: list[ReviewFinding],
        evidence: WorkspaceEvidence,
        workspace: GitWorktreeWorkspace,
    ) -> str | None:
        legacy = [
            *review.findings,
            *review.scope_concerns,
            *review.security_concerns,
            *review.compatibility_concerns,
        ]
        if legacy:
            return (
                "reviewer used legacy string blocker fields; leave them empty and use "
                "blocking_findings with typed source locations"
            )
        if evidence.tree_sha is None:
            return "review evidence is missing its immutable Git tree"
        all_findings = [*review.blocking_findings, *review.repair_regressions]
        all_paths = [location.path for finding in all_findings for location in finding.locations]
        try:
            line_counts = (
                workspace.file_line_counts(evidence.tree_sha, all_paths) if all_paths else {}
            )
        except WorkspaceError as exc:
            return f"review finding cites an invalid repository path: {exc}"
        for finding in all_findings:
            for location in finding.locations:
                line_count = line_counts.get(location.path)
                if line_count is None:
                    return (
                        f"review finding cites {location.path!r}, which does not exist in "
                        "the reviewed tree"
                    )
                if location.end_line > line_count:
                    return (
                        f"review finding cites {location.path}:{location.start_line}-"
                        f"{location.end_line}, but the reviewed file has {line_count} line(s)"
                    )
        if not prior_findings:
            if review.prior_finding_dispositions:
                return "initial review must leave prior_finding_dispositions empty"
            if review.repair_regressions:
                return "initial review must use blocking_findings, not repair_regressions"
            if review.approved == bool(review.blocking_findings):
                return (
                    "initial review approved must be true exactly when blocking_findings is empty"
                )
            return None

        expected_ids = {finding.id for finding in prior_findings}
        returned_ids = [disposition.finding_id for disposition in review.prior_finding_dispositions]
        if len(returned_ids) != len(set(returned_ids)):
            return "repair review contains duplicate prior finding dispositions"
        returned_id_set = set(returned_ids)
        missing = sorted(expected_ids - returned_id_set)
        extra = sorted(returned_id_set - expected_ids)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if extra:
                details.append("unknown: " + ", ".join(extra))
            return (
                "repair review must disposition every prior finding exactly once ("
                + "; ".join(details)
                + ")"
            )
        return None

    def _build_request(
        self,
        role: AgentRole,
        work_item: WorkItem,
        *,
        purpose: AgentPurpose = AgentPurpose.STANDARD,
        role_model: RoleModelConfig | None = None,
        triage_result: TriageResult | None = None,
        specification: Specification | None = None,
        execution_plan: ExecutionPlan | None = None,
        diff: str | None = None,
        changed_files: list[str] | None = None,
        verification_report: VerificationReport | None = None,
        test_report: TestReport | None = None,
        prior_review_findings: list[ReviewFinding] | None = None,
        accepted_review_findings: list[ReviewFinding] | None = None,
        repair_diff: str | None = None,
        repair_context: RepairContext | str | None = None,
        repository_profile: RepositoryProfile | None = None,
        dependency_names: tuple[str, ...] = (),
        workspace_path: str | None = None,
        attempt_number: int | None = None,
    ) -> AgentRequest:
        resolved = role_model if role_model is not None else self._router.model_for_role(role)
        return AgentRequest(
            role=role,
            purpose=purpose,
            model=resolved.model,
            reasoning=resolved.reasoning,
            context_tier=resolved.context_tier,
            work_item=work_item,
            triage_result=triage_result,
            specification=specification,
            execution_plan=execution_plan,
            diff=diff,
            changed_files=changed_files or [],
            verification_report=verification_report,
            test_report=test_report,
            prior_review_findings=prior_review_findings or [],
            accepted_review_findings=accepted_review_findings or [],
            repair_diff=repair_diff,
            repair_context=repair_context,
            repository_profile=repository_profile,
            dependency_names=dependency_names,
            workspace_path=workspace_path,
            attempt_number=attempt_number,
            timeout_seconds=self._config.agent_timeout_seconds,
        )

    def _invoke_agent(
        self,
        run: FactoryRun,
        request: AgentRequest,
        *,
        budget: AttemptBudget | None = None,
        reraise_runtime_errors: bool = False,
    ) -> AgentResult:
        """Run one agent and persist its routing and reported usage."""

        started_at = utc_now()
        invocation_number = len(run.invocation_records) + 1
        run.active_invocation = ActiveInvocation(
            invocation_number=invocation_number,
            role=request.role,
            purpose=request.purpose,
            model=request.model,
            reasoning=request.reasoning,
            context_tier=request.context_tier,
            started_at=started_at,
            attempt_number=request.attempt_number,
            budget=budget,
        )
        run.updated_at = started_at
        run.last_activity_at = started_at
        self._store.save_run(run)
        try:
            result = self._runtime.run(request)
        except (OSError, RuntimeError, ValueError) as exc:
            completed_at = utc_now()
            invocation_duration_ms = (completed_at - started_at).total_seconds() * 1000.0
            run.performance.record_duration(
                f"invocation.{request.role.value}",
                invocation_duration_ms,
                stage=run.state.value,
                operation="invocation",
                accumulate=True,
            )
            failure_reason = runtime_exception_failure_reason(exc)
            run.invocation_records.append(
                InvocationRecord(
                    invocation_number=invocation_number,
                    role=request.role,
                    purpose=request.purpose,
                    model=request.model,
                    reasoning=request.reasoning,
                    context_tier=request.context_tier,
                    started_at=started_at,
                    completed_at=completed_at,
                    success=False,
                    failure_reason=failure_reason,
                    attempt_number=request.attempt_number,
                    budget=budget,
                )
            )
            run.active_invocation = None
            run.updated_at = completed_at
            run.last_activity_at = completed_at
            self._store.save_run(run)
            if reraise_runtime_errors:
                raise
            return AgentResult(
                role=request.role,
                success=False,
                failure_reason=failure_reason,
            )
        completed_at = utc_now()
        invocation_duration_ms = (completed_at - started_at).total_seconds() * 1000.0
        run.performance.record_duration(
            f"invocation.{request.role.value}",
            invocation_duration_ms,
            stage=run.state.value,
            operation="invocation",
            accumulate=True,
        )
        if result.performance is not None:
            run.performance.record_size(
                prompt_chars=result.performance.prompt_chars,
                response_chars=result.performance.response_chars,
            )
        run.invocation_records.append(
            InvocationRecord(
                invocation_number=invocation_number,
                role=request.role,
                purpose=request.purpose,
                model=request.model,
                reasoning=request.reasoning,
                context_tier=request.context_tier,
                started_at=started_at,
                completed_at=completed_at,
                success=result.success,
                failure_reason=result.failure_reason,
                attempt_number=request.attempt_number,
                budget=budget,
                usage=result.usage,
                performance=result.performance,
            )
        )
        run.active_invocation = None
        run.updated_at = completed_at
        run.last_activity_at = completed_at
        self._store.save_run(run)
        return result

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
        legacy_scope_attempts = sum(
            1 for attempt in run.attempt_records if attempt.triggered_by is AttemptTrigger.SCOPE
        )
        return max(run.scope_replans, legacy_scope_attempts)

    def _select_worker(
        self, run: FactoryRun, context: _RunContext, budget: AttemptBudget
    ) -> tuple[RoleModelConfig | None, int]:
        used = self._attempts_used(run, budget)
        attempt_number = used + 1
        model_profile = (
            context.route_decision.selected_model_profile
            if context.route_decision is not None
            and context.route_decision.selected_model_profile is not None
            and budget is AttemptBudget.IMPLEMENTATION
            else None
        )
        if model_profile == "default":
            model_profile = None
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
            self._router.model_for_implementer(
                context.triage_result.complexity,
                attempt_number,
                model_profile=model_profile,
            ),
            attempt_number,
        )

    def _budget_exhausted_reason(self, budget: AttemptBudget, used: int) -> str:
        if budget is AttemptBudget.CI_REPAIR:
            return f"CI repair budget exhausted after {used} attempt(s)"
        return f"implementation attempt budget exhausted after {used} attempt(s)"

    @staticmethod
    def _is_rework_attempt(
        budget: AttemptBudget,
        attempt_number: int,
        trigger: AttemptTrigger,
    ) -> bool:
        if trigger is AttemptTrigger.POLISH:
            return False
        if budget is AttemptBudget.CI_REPAIR:
            return True
        return attempt_number > 1

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
        verification_generated_paths: set[str] = set()
        while True:
            try:
                role_model, attempt_number = self._select_worker(run, context, budget)
            except Exception as exc:
                raise self._halt(
                    run,
                    WorkflowState.FAILED,
                    f"worker model selection failed: {exc}",
                ) from exc
            if role_model is None:
                reason = self._budget_exhausted_reason(budget, attempt_number - 1)
                if run.unattended:
                    return self._publish_as_is(run, context, reason)
                raise self._halt(run, WorkflowState.NEEDS_HUMAN, reason)

            # Snapshot directories are keyed by the run-global attempt index so
            # a CI repair (whose per-budget attempt_number restarts at 1) can
            # never overwrite the pre-PR attempt's immutable evidence.
            # attempts/NN therefore always corresponds to attempt_records[NN-1].
            snapshot = len(run.attempt_records) + 1
            trigger = (
                repair_context.trigger if repair_context is not None else AttemptTrigger.INITIAL
            )
            if run.review_acceptance is not None:
                run = run.model_copy(update={"review_acceptance": None, "updated_at": utc_now()})
                self._store.save_run(run)
            if self._is_rework_attempt(budget, attempt_number, trigger):
                record_rework(
                    run.performance,
                    "repair_attempt",
                    stage=WorkflowState.IMPLEMENTING.value,
                )
                count_operation(
                    run.performance,
                    "rework.implementation",
                    stage=WorkflowState.IMPLEMENTING.value,
                    operation="rework",
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

            retained_generated_paths = sorted(
                verification_generated_paths.intersection(evidence.changed_files)
            )
            if retained_generated_paths:
                repair_context = self._verification_artifact_repair_context(
                    retained_generated_paths
                )
                continue
            verification_generated_paths.clear()

            run = self.transition(run, WorkflowState.VERIFYING)
            verification = self._verify(run, context)
            self._store.save_artifact(run.id, verification.report, attempt=snapshot)

            if not verification.report.passed:
                record_gate_failure(
                    run.performance,
                    "verification",
                    stage=WorkflowState.VERIFYING.value,
                )
                count_operation(
                    run.performance,
                    "rework_cause.verification_failure",
                    stage=WorkflowState.VERIFYING.value,
                    operation="rework",
                )
                if context.effective_route is ExecutionRoute.SINGLE:
                    run = self._ratchet_route(
                        run,
                        context,
                        to_route=ExecutionRoute.CRITIQUE,
                        reason="verification failure: deterministic verification did not pass",
                    )
                repair_context = self._verification_repair_context(verification)
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            verified_evidence = context.workspace.collect_evidence()
            if (
                verified_evidence.diff != evidence.diff
                or verified_evidence.tree_sha != evidence.tree_sha
            ):
                verification_generated_paths.update(
                    set(verified_evidence.changed_files) - set(evidence.changed_files)
                )
                context.latest_evidence = verified_evidence
                change_set = self._store.load_artifact(run.id, ChangeSet, attempt=snapshot)
                self._store.save_artifact(
                    run.id,
                    change_set.model_copy(
                        update={"changed_files": verified_evidence.changed_files}
                    ),
                    attempt=snapshot,
                )
                self._store.save_patch(run.id, verified_evidence.diff, attempt=snapshot)
                repair_context = self._verification_mutation_repair_context(
                    evidence,
                    verified_evidence,
                )
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue
            evidence = verified_evidence

            # Monotonic post-implementation ratchets
            # 1. Evaluate unexpected scope against original synthesized scope
            # before any scope replan
            if context.effective_route in {ExecutionRoute.SINGLE, ExecutionRoute.CRITIQUE}:
                original_scope = (
                    context.original_synthesized_scope
                    if context.original_synthesized_scope is not None
                    else tuple(context.execution_plan.expected_scope.modules)
                )
                unexpected_changes = [
                    f
                    for f in evidence.changed_files
                    if not any(f == m or f.startswith(f"{m}/") for m in original_scope)
                ]
                if unexpected_changes:
                    unrelated_desc = ", ".join(unexpected_changes)
                    run = self._ratchet_route(
                        run,
                        context,
                        to_route=ExecutionRoute.FULL_REVIEW,
                        reason=f"unrelated changes outside synthesized scope: {unrelated_desc}",
                    )

            scope = self._scope_policy.assess(
                context.execution_plan,
                evidence.changed_files,
                context.triage_result.risk,
            )
            while scope.decision is ScopeDecision.REPLAN:
                if (
                    run.unattended
                    and self._replans_used(run) >= self._config.scope_drift.max_replans
                ):
                    break
                record_rework(
                    run.performance,
                    "scope_replan",
                    stage=WorkflowState.PLANNING.value,
                )
                previous_findings = tuple(
                    (finding.category, finding.message) for finding in scope.findings
                )
                run = self._replan(run, context, scope, evidence)
                scope = self._scope_policy.assess(
                    context.execution_plan,
                    evidence.changed_files,
                    context.triage_result.risk,
                )
                current_findings = tuple(
                    (finding.category, finding.message) for finding in scope.findings
                )
                if scope.decision is ScopeDecision.REPLAN and current_findings == previous_findings:
                    if run.unattended:
                        break
                    raise self._halt(
                        run,
                        WorkflowState.NEEDS_HUMAN,
                        "scope metadata replan made no progress: " + _describe_scope(scope),
                    )
            if scope.decision is ScopeDecision.NEEDS_HUMAN and not run.unattended:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "scope drift requires human review: " + _describe_scope(scope),
                )

            # Monotonic post-implementation ratchets (continued)
            # 2. Excessive changed files
            if len(evidence.changed_files) > self._config.routing.single_max_changed_files:
                if context.effective_route in {ExecutionRoute.SINGLE, ExecutionRoute.CRITIQUE}:
                    run = self._ratchet_route(
                        run,
                        context,
                        to_route=ExecutionRoute.FULL_REVIEW,
                        reason=(
                            f"excessive changed-file count: {len(evidence.changed_files)} files "
                            f"exceeds threshold {self._config.routing.single_max_changed_files}"
                        ),
                    )

            # 3. Protected/sensitive/manifest/version paths
            protected_reason = self._sensitive_scope_reason(
                list(evidence.changed_files),
                execution_plan=context.execution_plan,
                risk=context.triage_result.risk,
                repository_profile=context.repository_profile,
                scope=scope,
            )
            if protected_reason is not None and context.effective_route in {
                ExecutionRoute.SINGLE,
                ExecutionRoute.CRITIQUE,
            }:
                run = self._ratchet_route(
                    run,
                    context,
                    to_route=ExecutionRoute.FULL_REVIEW,
                    reason=protected_reason,
                )

            # 4. Dependency fingerprint change
            if context.effective_route in {
                ExecutionRoute.SINGLE,
                ExecutionRoute.CRITIQUE,
            }:
                try:
                    current_profile = self._repository_profiler(context.workspace.path)
                    if (
                        current_profile.dependency_fingerprint
                        != context.repository_profile.dependency_fingerprint
                    ):
                        run = self._ratchet_route(
                            run,
                            context,
                            to_route=ExecutionRoute.FULL_REVIEW,
                            reason="dependency fingerprint changed during implementation",
                        )
                except (OSError, RuntimeError, ValueError) as exc:
                    run = self._ratchet_route(
                        run,
                        context,
                        to_route=ExecutionRoute.FULL_REVIEW,
                        reason=f"dependency fingerprint validation failed: {exc}",
                    )

            # 5. Scope drift findings
            if scope.decision is not ScopeDecision.CONTINUE and context.effective_route in {
                ExecutionRoute.SINGLE,
                ExecutionRoute.CRITIQUE,
            }:
                run = self._ratchet_route(
                    run,
                    context,
                    to_route=ExecutionRoute.FULL_REVIEW,
                    reason=f"scope decision was {scope.decision.value}",
                )

            if self._should_polish(run, budget, context):
                context.polish_attempted = True
                repair_context = self._polish_context()
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            run = self.transition(run, WorkflowState.REVIEWING)

            # If CI repair, always use FULL review semantics via recorded ratchet
            if budget is AttemptBudget.CI_REPAIR and context.effective_route in {
                ExecutionRoute.SINGLE,
                ExecutionRoute.CRITIQUE,
            }:
                run = self._ratchet_route(
                    run,
                    context,
                    to_route=ExecutionRoute.FULL_REVIEW,
                    reason="CI repair requires full review semantics",
                )

            # SINGLE route: deterministic verification accepted under sufficiency conditions
            if context.effective_route is ExecutionRoute.SINGLE:
                test_report = TestReport(
                    passed=True,
                    confidence=1.0,
                    findings=[],
                    suggested_tests=[],
                    skipped=True,
                    provenance="SKIPPED",
                    skip_reason="Tester agent skipped by SINGLE route",
                )
                self._store.save_artifact(run.id, test_report, attempt=snapshot)

                review_report = ReviewReport(
                    approved=True,
                    findings=[],
                    skipped=True,
                    provenance="SKIPPED",
                    skip_reason="Reviewer skipped: accepted by deterministic verification",
                )
                self._store.save_artifact(run.id, review_report, attempt=snapshot)
                run = self._record_reviewed_tree(run, snapshot, evidence.tree_sha)
                context.latest_evidence = evidence
                context.latest_verification = verification.report
                context.latest_test_report = test_report
                context.latest_review = review_report
                return self.transition(run, WorkflowState.PR_READY)

            repair_diff = self._repair_review_diff(run, context.workspace, evidence)

            # CRITIQUE route: tester agent skipped, independent reviewer active
            if context.effective_route is ExecutionRoute.CRITIQUE:
                test_report = TestReport(
                    passed=True,
                    confidence=1.0,
                    findings=[],
                    suggested_tests=[],
                    skipped=True,
                    provenance="SKIPPED",
                    skip_reason="Tester agent skipped by CRITIQUE route",
                )
                self._store.save_artifact(run.id, test_report, attempt=snapshot)
                reviewer_test_report = None
            else:
                test_report = self._run_tester(
                    run,
                    context,
                    evidence,
                    verification.report,
                    snapshot,
                    repair_diff,
                )
                reviewer_test_report = test_report

            review_report = self._run_reviewer(
                run,
                context,
                evidence,
                verification.report,
                reviewer_test_report,
                snapshot,
                repair_diff,
            )
            run = self._record_reviewed_tree(run, snapshot, evidence.tree_sha)
            run, review_report, impasse = self._apply_review_report(
                run,
                review_report,
                snapshot,
                evidence.tree_sha,
                repair_diff,
            )
            self._store.save_artifact(run.id, review_report, attempt=snapshot)

            review_rounds = self._review_rounds_used(run, budget)
            acceptance_reason: ReviewAcceptanceReason | None = None
            attended_accepts_impasse = impasse is not None and impasse.kind in {
                ReviewImpasseKind.REPEATED_PATH,
                ReviewImpasseKind.REPEATED_FINDING,
            }
            if impasse is not None and (run.unattended or attended_accepts_impasse):
                if not attended_accepts_impasse:
                    run = self._add_needs_look(run, f"review impasse {impasse.kind} was accepted")
                acceptance_reason = ReviewAcceptanceReason.REVIEW_IMPASSE
            elif (
                impasse is None
                and run.review_ledger.open_findings
                and review_rounds >= self._config.review.max_rounds
            ):
                acceptance_reason = ReviewAcceptanceReason.REVIEW_ROUND_LIMIT

            attempts_limit = (
                self._config.ci.repair_attempts
                if budget is AttemptBudget.CI_REPAIR
                else self._config.retries.max_total_attempts
            )
            if (
                acceptance_reason is None
                and impasse is None
                and run.review_ledger.open_findings
                and self._attempts_used(run, budget) >= attempts_limit
            ):
                acceptance_reason = ReviewAcceptanceReason.ATTEMPT_BUDGET_LIMIT

            if acceptance_reason is not None:
                acceptance = self._accept_review_findings(
                    run,
                    context,
                    verification.report,
                    review_report,
                    snapshot,
                    evidence.tree_sha,
                    review_rounds,
                    acceptance_reason,
                )
                if acceptance is not None:
                    run = self._persist_review_acceptance(run, acceptance)
                    context.latest_evidence = evidence
                    context.latest_verification = verification.report
                    context.latest_test_report = test_report
                    context.latest_review = review_report
                    return self.transition(run, WorkflowState.PR_READY)
                if impasse is None:
                    impasse_kind = (
                        ReviewImpasseKind.ATTEMPT_BUDGET_LIMIT
                        if acceptance_reason is ReviewAcceptanceReason.ATTEMPT_BUDGET_LIMIT
                        else ReviewImpasseKind.REVIEW_ROUND_LIMIT
                    )
                    impasse = self._review_impasse(
                        run,
                        snapshot,
                        "review reached its configured limit and the remaining findings "
                        "are not eligible for automatic acceptance",
                        kind=impasse_kind,
                    )

            if impasse is not None:
                self._store.save_artifact(run.id, impasse, attempt=snapshot)
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    impasse.reason,
                )

            if run.review_ledger.open_findings:
                record_gate_failure(
                    run.performance,
                    "review",
                    stage=WorkflowState.REVIEWING.value,
                )
                count_operation(
                    run.performance,
                    "rework_cause.review_rejection",
                    stage=WorkflowState.REVIEWING.value,
                    operation="rework",
                )
                repair_context = self._review_repair_context(
                    run.review_ledger.open_findings,
                    test_report,
                )
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue

            context.latest_evidence = evidence
            context.latest_verification = verification.report
            context.latest_test_report = test_report
            context.latest_review = review_report
            if not evidence.tree_sha:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "review evidence is missing its immutable Git tree",
                )
            run = run.model_copy(
                update={
                    "reviewed_tree_sha": evidence.tree_sha,
                    "review_acceptance": (
                        ReviewAcceptance(
                            snapshot=snapshot,
                            reason=ReviewAcceptanceReason.CARRIED_FORWARD,
                            risk=context.triage_result.risk,
                            review_rounds=review_rounds,
                            reviewed_tree_sha=evidence.tree_sha,
                            findings=run.review_ledger.accepted_findings,
                        )
                        if run.review_ledger.accepted_findings
                        else None
                    ),
                    "review_ledger": run.review_ledger.model_copy(
                        update={
                            "open_findings": [],
                            "last_reviewed_tree_sha": None,
                            "consecutive_replacement_rounds": 0,
                            "path_streaks": {},
                            "unresolved_streaks": {},
                        }
                    ),
                }
            )
            self._store.save_run(run)
            if run.review_acceptance is not None:
                self._store.save_artifact(
                    run.id,
                    run.review_acceptance,
                    attempt=snapshot,
                )
            return self.transition(run, WorkflowState.PR_READY)

    def _replan(
        self,
        run: FactoryRun,
        context: _RunContext,
        scope: ScopeAssessment,
        evidence: WorkspaceEvidence,
    ) -> FactoryRun:
        """Update scope metadata for an already-green diff without reimplementation."""
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
            summary=(
                "The existing implementation passed deterministic verification, but its "
                "execution-plan scope metadata does not describe the verified diff. Revise "
                "metadata only; do not request implementation changes."
            ),
            failures=[finding.message for finding in scope.findings][:MAX_REPAIR_FAILURES],
            log_excerpt=None,
        )
        if used == 0:
            self._store.save_artifact_once(
                run.id,
                context.execution_plan,
                filename="execution-plan.initial.json",
            )
        run = self.transition(run, WorkflowState.PLANNING)
        # A scope replan changes plan metadata only. It keeps the specification.
        context.execution_plan = self._run_planner(
            run,
            context.work_item,
            context.specification,
            workspace_path=str(context.workspace.path),
            repair_context=repair_context,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
        ).execution_plan
        self._store.save_artifact_once(
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
        self._store.save_run(run)
        return self.transition(run, WorkflowState.VERIFYING)

    def _verify(self, run: FactoryRun, context: _RunContext) -> RepositoryVerificationResult:
        """Run install, verify and build with persisted logs, then the mutation gate."""
        result = self._verifier.run(
            self._commands_for_run(run.id),
            cwd=context.workspace.path,
            run_dir=self._store.run_dir(run.id),
            timeout_seconds=self._config.repository.command_timeout_seconds,
            env_passthrough=self._config.repository.env_passthrough,
            capture_bytes=self._config.repository.log_capture_bytes,
        )
        if not result.report.passed:
            return result
        return self._apply_mutation_gate(run, context, result)

    def _apply_mutation_gate(
        self,
        run: FactoryRun,
        context: _RunContext,
        result: RepositoryVerificationResult,
    ) -> RepositoryVerificationResult:
        """Add the advisory mutation gate to a passed verification (ADR-034).

        The gate runs only for changed Python modules in a lane whose mutation
        tool and package runner are known. Its result is one more check in the
        verification report, which the tester and the reviewer read. It never
        fails verification, because a surviving mutant can be equivalent.
        """
        exec_prefix = self._mutation_exec_prefix(run)
        evidence = context.latest_evidence
        if exec_prefix is None or evidence is None or not result.report.deterministic_checks:
            # No verify command ran, so the repository's code is not run here either.
            return result
        targets = mutation_targets(evidence.changed_files)
        started = time.monotonic()
        if not targets:
            report = MutationReport(
                status=MutationStatus.SKIPPED, reason="no changed Python source modules"
            )
        else:
            report = self._run_mutation_gate(context, exec_prefix, targets)
        # Saved on every passed verification, so no report from an older attempt stays.
        self._store.save_artifact(run.id, report)
        check = mutation_check_result(report, time.monotonic() - started)
        verification = result.report.model_copy(
            update={"deterministic_checks": [*result.report.deterministic_checks, check]}
        )
        return RepositoryVerificationResult(
            report=verification,
            command_logs=result.command_logs,
            failure_kind=result.failure_kind,
            failed_phase=result.failed_phase,
            failed_command=result.failed_command,
        )

    def _run_mutation_gate(
        self, context: _RunContext, exec_prefix: str, targets: tuple[MutationTarget, ...]
    ) -> MutationReport:
        try:
            return run_mutation_gate(
                self._command_runner,
                context.workspace.path,
                exec_prefix,
                targets,
                ProbeLimits.from_repository(self._config.repository),
            )
        except Exception as exc:
            # The gate is advisory. Its own failure never fails the run.
            return MutationReport(
                status=MutationStatus.SKIPPED,
                modules=tuple(target.module for target in targets),
                reason=f"mutation gate error: {type(exc).__name__}",
            )

    def _mutation_exec_prefix(self, run: FactoryRun) -> str | None:
        """Return the Python package runner prefix when the gate can run, else ``None``."""
        if not self._config.repository.mutation_gate:
            return None
        try:
            inventory = self._store.load_artifact(run.id, ToolchainInventory)
            profile = self._store.load_artifact(run.id, RepositoryProfile)
        except (OSError, ValueError):
            # The gate is an extra check. Unreadable run artifacts skip it.
            return None
        if ToolchainLane.PYTHON not in inventory.mutation_tool_lanes:
            return None
        runner, _note = select_package_runner(ToolchainLane.PYTHON, root_version_files(profile))
        return None if runner is None else runner.exec_prefix

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
            context_tier=role_model.context_tier,
            work_item=context.work_item,
            specification=context.specification,
            execution_plan=context.execution_plan,
            repair_context=repair_context,
            diff=current_diff if repair_context is not None else None,
            changed_files=(
                list(context.latest_evidence.changed_files)
                if repair_context is not None and context.latest_evidence is not None
                else []
            ),
            dependency_names=_dependency_names(context.repository_profile),
            workspace_path=str(context.workspace.path),
            attempt_number=attempt_number,
            timeout_seconds=self._config.agent_timeout_seconds,
        )
        result = self._invoke_agent(run, request, budget=budget)
        completed_at = utc_now()

        if not result.success:
            failure_reason = result.failure_reason or "implementer reported failure"
            try:
                evidence = context.workspace.collect_evidence()
                context.latest_evidence = evidence
                self._store.save_patch(run.id, evidence.diff, attempt=snapshot)
            except WorkspaceError as exc:
                failure_reason = (
                    f"{failure_reason}; could not capture partial worktree evidence: {exc}"
                )
            run = self._record_attempt(
                run,
                attempt_number,
                role_model,
                started_at,
                completed_at,
                outcome="failed",
                failure_reason=failure_reason,
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
            context_tier=role_model.context_tier,
            invocation_number=(
                run.invocation_records[-1].invocation_number if run.invocation_records else None
            ),
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
            summary=(
                "The previous implementation attempt did not complete. The working tree "
                "may contain partial, unverified edits from that attempt; inspect and "
                "reconcile them before continuing."
            ),
            failures=[last.failure_reason or "implementer reported failure"],
            log_excerpt=None,
        )

    def _should_polish(
        self,
        run: FactoryRun,
        budget: AttemptBudget,
        context: _RunContext,
    ) -> bool:
        if (
            budget is not AttemptBudget.IMPLEMENTATION
            or not self._config.polish.enabled
            or context.effective_route in {ExecutionRoute.SINGLE, ExecutionRoute.CRITIQUE}
        ):
            return False
        if context.polish_attempted:
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
                "simplification pass using the fixed simplify and polish guidance supplied "
                "with this request. Simplify first, then polish. "
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

    def _verification_mutation_repair_context(
        self,
        before: WorkspaceEvidence,
        after: WorkspaceEvidence,
    ) -> RepairContext:
        before_files = set(before.changed_files)
        after_files = set(after.changed_files)
        added = sorted(after_files - before_files)
        removed = sorted(before_files - after_files)
        details = [
            "Repository verification commands changed the Git tree after implementation "
            "evidence was captured. Verification must leave source contents unchanged. "
            "Remove generated artifacts from the worktree and Git index, then add appropriate "
            "ignore rules so verification cannot stage them again."
        ]
        if added:
            details.append(f"Newly tracked after verification: {', '.join(added)}")
        if removed:
            details.append(f"No longer tracked after verification: {', '.join(removed)}")
        if not added and not removed:
            details.append("One or more already-changed files were modified during verification.")
        return RepairContext(
            trigger=AttemptTrigger.VERIFICATION,
            summary="Deterministic verification modified the repository.",
            failures=details[:MAX_REPAIR_FAILURES],
            log_excerpt=None,
        )

    def _verification_artifact_repair_context(
        self,
        retained_paths: list[str],
    ) -> RepairContext:
        return RepairContext(
            trigger=AttemptTrigger.VERIFICATION,
            summary="Verification-generated artifacts are still part of the proposed change.",
            failures=[
                "Remove these generated paths from the worktree and Git index before retrying: "
                + ", ".join(retained_paths)
            ],
            log_excerpt=None,
        )

    def _record_reviewed_tree(
        self,
        run: FactoryRun,
        snapshot: int,
        tree_sha: str | None,
    ) -> FactoryRun:
        if tree_sha is None:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "review evidence is missing its immutable Git tree",
            )
        if snapshot > len(run.attempt_records):
            raise self._halt(
                run,
                WorkflowState.FAILED,
                f"review snapshot {snapshot} has no matching implementation attempt",
            )
        attempts = list(run.attempt_records)
        attempts[snapshot - 1] = attempts[snapshot - 1].model_copy(
            update={"reviewed_tree_sha": tree_sha}
        )
        run = run.model_copy(update={"attempt_records": attempts, "updated_at": utc_now()})
        self._store.save_run(run)
        return run

    def _repair_review_diff(
        self,
        run: FactoryRun,
        workspace: GitWorktreeWorkspace,
        evidence: WorkspaceEvidence,
    ) -> str | None:
        if not run.review_ledger.open_findings:
            return None
        previous_tree = run.review_ledger.last_reviewed_tree_sha
        current_tree = evidence.tree_sha
        if previous_tree is None or current_tree is None:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "repair review cannot compare immutable Git trees",
            )
        try:
            return workspace.diff_trees(previous_tree, current_tree)
        except WorkspaceError as exc:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                f"repair review could not derive its change delta: {exc}",
            ) from exc

    def _apply_review_report(
        self,
        run: FactoryRun,
        review: ReviewReport,
        snapshot: int,
        tree_sha: str | None,
        repair_diff: str | None,
    ) -> tuple[FactoryRun, ReviewReport, ReviewImpasse | None]:
        if tree_sha is None:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "review evidence is missing its immutable Git tree",
            )
        ledger = run.review_ledger
        prior = list(ledger.open_findings)
        if not prior:
            accepted = list(ledger.accepted_findings)
            new_drafts = [
                draft
                for draft in review.blocking_findings
                if not any(_review_draft_matches_finding(draft, finding) for finding in accepted)
            ]
            open_findings = self._mint_review_findings(
                new_drafts,
                ReviewFindingOrigin.INITIAL,
                snapshot,
            )
            ledger = ledger.model_copy(
                update={
                    "open_findings": open_findings,
                    "last_reviewed_tree_sha": tree_sha if open_findings else None,
                    "late_adoption_rounds": 0,
                    "consecutive_replacement_rounds": 0,
                    "path_streaks": dict.fromkeys(_review_finding_paths(open_findings), 1),
                    "unresolved_streaks": {finding.id: 1 for finding in open_findings},
                }
            )
            effective = review.model_copy(update={"approved": not open_findings})
            run = run.model_copy(update={"review_ledger": ledger, "updated_at": utc_now()})
            self._store.save_run(run)
            return run, effective, None

        dispositions = {
            disposition.finding_id: disposition for disposition in review.prior_finding_dispositions
        }
        unresolved_ids = {
            finding.id
            for finding in prior
            if dispositions[finding.id].status is ReviewDispositionStatus.UNRESOLVED
        }
        prior_by_id = {finding.id: finding for finding in prior}

        regression_overlaps, regression_drafts = _partition_review_drafts(
            prior,
            review.repair_regressions,
        )
        latent_overlaps, latent_drafts = _partition_review_drafts(
            prior,
            review.blocking_findings,
        )
        unresolved_ids.update(regression_overlaps)
        unresolved_ids.update(latent_overlaps)

        changed_ranges = _changed_line_ranges(repair_diff or "")
        regressions: list[ReviewFinding] = []
        for draft in regression_drafts:
            origin = (
                ReviewFindingOrigin.REPAIR_REGRESSION_DIRECT
                if _review_draft_intersects_ranges(draft, changed_ranges)
                else ReviewFindingOrigin.REPAIR_REGRESSION_INDIRECT
            )
            regressions.extend(self._mint_review_findings([draft], origin, snapshot))

        safety_late_drafts = [
            draft
            for draft in latent_drafts
            if draft.category in self._config.review.blocked_categories
        ]
        adopt_late = bool(latent_drafts) and (
            ledger.late_adoption_rounds < MAX_LATE_REVIEW_ADOPTION_ROUNDS
        )
        adopted_late = (
            self._mint_review_findings(
                latent_drafts,
                ReviewFindingOrigin.LATE_ADOPTED,
                snapshot,
            )
            if adopt_late
            else self._mint_review_findings(
                safety_late_drafts,
                ReviewFindingOrigin.LATE_ADOPTED,
                snapshot,
            )
        )
        unresolved = [finding for finding in prior if finding.id in unresolved_ids]
        current = _deduplicate_review_findings([*unresolved, *regressions, *adopted_late])
        too_many_findings = len(current) > MAX_OPEN_REVIEW_FINDINGS
        if too_many_findings:
            current = current[:MAX_OPEN_REVIEW_FINDINGS]

        replacement_rounds = (
            ledger.consecutive_replacement_rounds + 1
            if not unresolved and bool(regressions or adopted_late)
            else 0
        )
        current_paths = _review_finding_paths(current)
        path_streaks = {path: ledger.path_streaks.get(path, 0) + 1 for path in current_paths}
        unresolved_streaks = {
            finding.id: (
                ledger.unresolved_streaks.get(finding.id, 0) + 1 if finding.id in prior_by_id else 1
            )
            for finding in current
        }
        ledger = ledger.model_copy(
            update={
                "open_findings": current,
                "last_reviewed_tree_sha": tree_sha if current else None,
                "late_adoption_rounds": ledger.late_adoption_rounds + int(adopt_late),
                "consecutive_replacement_rounds": replacement_rounds,
                "path_streaks": path_streaks,
                "unresolved_streaks": unresolved_streaks,
            }
        )
        suggestions = list(review.suggested_changes)
        advisory_late_drafts = [draft for draft in latent_drafts if draft not in safety_late_drafts]
        if advisory_late_drafts and not adopt_late:
            suggestions.extend(
                f"Late review finding left advisory after the bounded adoption round: "
                f"{draft.message}"
                for draft in advisory_late_drafts
            )
        effective = review.model_copy(
            update={
                "approved": not current,
                "suggested_changes": suggestions,
            }
        )
        run = run.model_copy(update={"review_ledger": ledger, "updated_at": utc_now()})
        self._store.save_run(run)

        if too_many_findings:
            return (
                run,
                effective,
                self._review_impasse(
                    run,
                    snapshot,
                    f"more than {MAX_OPEN_REVIEW_FINDINGS} blockers remained open",
                    kind=ReviewImpasseKind.TOO_MANY_FINDINGS,
                ),
            )
        path_limit = sorted(
            path
            for path, count in path_streaks.items()
            if count >= MAX_CONSECUTIVE_BLOCKING_REVIEWS_PER_PATH
        )
        unresolved_limit = sorted(
            finding_id
            for finding_id, count in unresolved_streaks.items()
            if count >= MAX_CONSECUTIVE_UNRESOLVED_REVIEWS
        )
        if path_limit:
            return (
                run,
                effective,
                self._review_impasse(
                    run,
                    snapshot,
                    "review blockers kept returning on the same path(s): " + ", ".join(path_limit),
                    kind=ReviewImpasseKind.REPEATED_PATH,
                ),
            )
        if unresolved_limit:
            return (
                run,
                effective,
                self._review_impasse(
                    run,
                    snapshot,
                    "review findings remained unresolved across repeated repairs: "
                    + ", ".join(unresolved_limit),
                    kind=ReviewImpasseKind.REPEATED_FINDING,
                ),
            )
        if replacement_rounds >= MAX_CONSECUTIVE_REPLACEMENT_REVIEWS:
            return (
                run,
                effective,
                self._review_impasse(
                    run,
                    snapshot,
                    "consecutive repairs replaced every previous blocker with new blockers",
                    kind=ReviewImpasseKind.REPLACEMENT_LOOP,
                ),
            )
        return run, effective, None

    def _mint_review_findings(
        self,
        drafts: list[ReviewFindingDraft],
        origin: ReviewFindingOrigin,
        snapshot: int,
    ) -> list[ReviewFinding]:
        findings = [
            ReviewFinding(
                id=_review_finding_id(draft),
                category=draft.category,
                message=draft.message,
                locations=draft.locations,
                origin=origin,
                first_seen_snapshot=snapshot,
            )
            for draft in drafts
        ]
        return _deduplicate_review_findings(findings)

    def _review_impasse(
        self,
        run: FactoryRun,
        snapshot: int,
        summary: str,
        *,
        kind: ReviewImpasseKind = ReviewImpasseKind.UNKNOWN,
    ) -> ReviewImpasse:
        findings = run.review_ledger.open_findings[:MAX_GUIDANCE_FINDINGS]
        paths = sorted(_review_finding_paths(findings))
        details = [f"[{finding.id}] {finding.message}" for finding in findings]
        reason = f"review failed to converge at snapshot {snapshot}: {summary}"
        if paths:
            reason += "; blocking paths: " + ", ".join(paths)
        return ReviewImpasse(
            snapshot=snapshot,
            kind=kind,
            reason=reason,
            paths=paths,
            finding_ids=[finding.id for finding in findings],
            findings=details,
        )

    def _review_rounds_used(self, run: FactoryRun, budget: AttemptBudget) -> int:
        return sum(
            1
            for attempt in run.attempt_records
            if attempt.budget is budget and attempt.reviewed_tree_sha is not None
        )

    def _accept_review_findings(
        self,
        run: FactoryRun,
        context: _RunContext,
        verification: VerificationReport,
        review: ReviewReport,
        snapshot: int,
        tree_sha: str | None,
        review_rounds: int,
        reason: ReviewAcceptanceReason,
    ) -> ReviewAcceptance | None:
        findings = _deduplicate_review_findings(
            [*run.review_ledger.accepted_findings, *run.review_ledger.open_findings]
        )
        if len(findings) > MAX_OPEN_REVIEW_FINDINGS:
            logger.warning(
                "review acceptance keeps the newest %d of %d findings",
                MAX_OPEN_REVIEW_FINDINGS,
                len(findings),
            )
            findings = findings[-MAX_OPEN_REVIEW_FINDINGS:]
        if not tree_sha or not verification.passed or not findings:
            return None
        if not run.unattended and (
            context.triage_result.risk not in self._config.review.accepted_risks
            or len(findings) > self._config.review.max_accepted_findings
            or any(
                finding.category in self._config.review.blocked_categories for finding in findings
            )
            or any(
                finding.origin
                not in {
                    ReviewFindingOrigin.INITIAL,
                    ReviewFindingOrigin.LATE_ADOPTED,
                }
                for finding in findings
            )
            or review.repair_regressions
            or any(
                finding.category in self._config.review.blocked_categories
                for finding in review.blocking_findings
            )
        ):
            return None
        return ReviewAcceptance(
            snapshot=snapshot,
            reason=reason,
            risk=context.triage_result.risk,
            review_rounds=review_rounds,
            reviewed_tree_sha=tree_sha,
            findings=findings,
        )

    def _persist_review_acceptance(
        self,
        run: FactoryRun,
        acceptance: ReviewAcceptance,
    ) -> FactoryRun:
        ledger = run.review_ledger.model_copy(
            update={
                "open_findings": [],
                "accepted_findings": acceptance.findings,
                "last_reviewed_tree_sha": None,
                "consecutive_replacement_rounds": 0,
                "path_streaks": {},
                "unresolved_streaks": {},
            }
        )
        run = run.model_copy(
            update={
                "reviewed_tree_sha": acceptance.reviewed_tree_sha,
                "review_acceptance": acceptance,
                "review_ledger": ledger,
                "updated_at": utc_now(),
            }
        )
        self._store.save_run(run)
        self._store.save_artifact(run.id, acceptance, attempt=acceptance.snapshot)
        return run

    def _review_authorizes_delivery(
        self,
        run: FactoryRun,
        review: ReviewReport,
        risk: Risk,
    ) -> bool:
        if review.skipped or review.provenance == "SKIPPED":
            if run.effective_route is not ExecutionRoute.SINGLE:
                return False
            latest_verification = self._store.load_artifact(
                run.id, VerificationReport, attempt=len(run.attempt_records)
            )
            if latest_verification is None or not latest_verification.passed:
                return False
            if run.reviewed_tree_sha is None:
                return False
            return True

        acceptance = run.review_acceptance
        if acceptance is None:
            return review.approved
        if not (
            run.reviewed_tree_sha is not None
            and acceptance.reviewed_tree_sha == run.reviewed_tree_sha
            and acceptance.findings == run.review_ledger.accepted_findings
        ):
            return False
        return run.unattended or self._acceptance_within_policy(acceptance, review, risk)

    def _acceptance_within_policy(
        self, acceptance: ReviewAcceptance, review: ReviewReport, risk: Risk
    ) -> bool:
        """Whether the configured review policy allows an attended run to
        accept these findings."""
        return (
            acceptance.risk is risk
            and acceptance.risk in self._config.review.accepted_risks
            and 0 < len(acceptance.findings) <= self._config.review.max_accepted_findings
            and not review.repair_regressions
            and not any(
                finding.category in self._config.review.blocked_categories
                for finding in acceptance.findings
            )
            and not any(
                finding.category in self._config.review.blocked_categories
                for finding in review.blocking_findings
            )
        )

    def _review_repair_context(
        self,
        findings: list[ReviewFinding],
        test_report: TestReport,
    ) -> RepairContext:
        excerpt = None
        if test_report.findings:
            excerpt = _bounded("\n".join(test_report.findings))
        return RepairContext(
            trigger=AttemptTrigger.REVIEW,
            summary="The independent reviewer rejected the change.",
            failures=[
                f"[{finding.id}] {finding.message}" for finding in findings[:MAX_REPAIR_FAILURES]
            ],
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
            if run.needs_look:
                return self._leave_open(run, context)
            return self.transition(run, WorkflowState.DONE)
        return self._ci_loop(run, context)

    def _skipped_gates(
        self, run: FactoryRun, context: _RunContext, scope: ScopeAssessment
    ) -> list[str]:
        """The gates an unattended run let continue that would have stopped an
        attended run (ADR-039, ADR-040)."""
        triage = context.triage_result
        reasons: list[str] = []
        if not triage.factory_eligible:
            reasons.append("triage marked this work item ineligible")
        route = context.route_decision
        if route is not None and route.effective_route is ExecutionRoute.MANUAL_TRIAGE:
            reasons.append("routing selected manual triage")
        if run.risk_assessment_enabled and self._config.risk[triage.risk].human_approval:
            reasons.append(f"risk {triage.risk} requires human approval")
        if context.execution_plan.unresolved_decisions:
            reasons.append(UNRESOLVED_DECISIONS_HALT_REASON)
        if scope.decision is not ScopeDecision.CONTINUE:
            reasons.append("scope drift: " + _describe_scope(scope))
        review = context.latest_review
        acceptance = run.review_acceptance
        if (
            review is not None
            and acceptance is not None
            and (
                not self._acceptance_within_policy(acceptance, review, triage.risk)
                or any(
                    finding.origin
                    not in {ReviewFindingOrigin.INITIAL, ReviewFindingOrigin.LATE_ADOPTED}
                    for finding in acceptance.findings
                )
            )
        ):
            reasons.append("review findings were accepted beyond the review policy")
        return reasons

    def _add_needs_look(self, run: FactoryRun, reason: str) -> FactoryRun:
        if reason in run.needs_look:
            return run
        run = run.model_copy(
            update={"needs_look": [*run.needs_look, reason], "updated_at": utc_now()}
        )
        self._store.save_run(run)
        return run

    def _publish_as_is(self, run: FactoryRun, context: _RunContext, reason: str) -> FactoryRun:
        """Unattended attempt budget used up (ADR-040).

        Before a pull request exists, publish the current work as it is. After
        one exists, leave it open as it is: the failed repair is not pushed.
        """
        if run.pull_request_url is not None:
            return self._leave_open(run, context, reason)
        evidence = context.workspace.collect_evidence()
        if not evidence.changed_files or not evidence.tree_sha:
            raise self._halt(run, WorkflowState.NEEDS_HUMAN, f"{reason}; nothing to publish")
        run = self._add_needs_look(run, reason)
        run = run.model_copy(update={"reviewed_tree_sha": evidence.tree_sha})
        self._store.save_run(run)
        self._store.save_patch(run.id, evidence.diff)
        context.latest_evidence = evidence
        context.latest_test_report = None
        context.latest_review = None
        return self.transition(run, WorkflowState.PR_READY)

    def _leave_open(
        self, run: FactoryRun, context: _RunContext, reason: str | None = None
    ) -> FactoryRun:
        """End an unattended run with its pull request open, labelled and not
        merged (ADR-040)."""
        if reason is not None:
            run = self._add_needs_look(run, reason)
        assert run.pull_request_url is not None
        try:
            self._resolve_publisher().flag_needs_look(
                workspace_path=context.workspace.path,
                pull_request_url=run.pull_request_url,
                reasons=run.needs_look,
                repository=run.delivery_repository,
            )
        except (GitHubError, OSError, ValueError) as exc:
            # The label tells people to look. Skipping the merge is what keeps
            # the pull request safe, so a labelling error does not stop the run.
            logger.warning("could not label pull request %s: %s", run.pull_request_url, exc)
        return self.transition(run, WorkflowState.DONE)

    def _publish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """PR boundary: re-run the deterministic gates, then commit/push/open."""
        evidence = context.latest_evidence
        assert evidence is not None
        if context.latest_review is None:
            # Only an unattended run whose attempt budget ran out publishes
            # work without a review (ADR-040).
            authorized = run.unattended and bool(run.needs_look)
        else:
            authorized = self._review_authorizes_delivery(
                run, context.latest_review, context.triage_result.risk
            )
        if not authorized:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "independent review or a matching bounded controller acceptance is required "
                "for every PR",
            )
        current_evidence = context.workspace.collect_evidence()
        if (
            current_evidence.diff != evidence.diff
            or not run.reviewed_tree_sha
            or current_evidence.tree_sha != run.reviewed_tree_sha
        ):
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "repository changed after independent review; refusing to publish unreviewed work",
            )
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
        if scope.decision is not ScopeDecision.CONTINUE and not run.unattended:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "scope drift requires human review before publishing: " + _describe_scope(scope),
            )
        if run.unattended:
            for reason in self._skipped_gates(run, context, scope):
                run = self._add_needs_look(run, reason)

        publisher = self._resolve_publisher()
        branch_name = run.branch_name
        assert branch_name is not None
        parent_sha = run.commit_sha or run.base_commit_sha
        if not parent_sha:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "publication is missing its authorized parent commit",
            )

        def record_commit(commit_sha: str) -> None:
            nonlocal run
            if SHA_PATTERN.fullmatch(commit_sha) is None:
                raise GitHubError("publication receipt must contain an exact commit SHA")
            if run.pending_commit_sha is not None and run.pending_commit_sha != commit_sha:
                raise GitHubError("a different publication commit is already recorded")
            now = utc_now()
            run = run.model_copy(
                update={
                    "pending_commit_sha": commit_sha,
                    "updated_at": now,
                    "last_activity_at": now,
                }
            )
            self._store.save_run(run)

        try:
            if self._config.merge.enabled:
                assert self._merger is not None
                if (
                    self._merger.validate_repository(context.workspace.path)
                    != run.delivery_repository
                ):
                    raise GitHubError("delivery repository changed before publication")
            base_branch = publisher.resolve_base_branch(context.source_repo)
            if run.delivery_base_branch is not None and run.delivery_base_branch != base_branch:
                raise GitHubError("refusing to publish to a different delivery base branch")
            run = run.model_copy(update={"delivery_base_branch": base_branch})
            self._store.save_run(run)
            result = publisher.publish(
                workspace_path=context.workspace.path,
                branch_name=branch_name,
                base_branch=base_branch,
                commit_message=_commit_message(context, run.id),
                title=context.execution_plan.summary.strip().rstrip(".").strip(),
                body=self._build_pr_body(run, context, changed_files),
                existing_pull_request_url=run.pull_request_url,
                expected_tree_sha=run.reviewed_tree_sha,
                expected_repository=run.delivery_repository,
                expected_host=run.delivery_host,
                expected_parent_sha=parent_sha,
                prepared_commit_sha=run.pending_commit_sha,
                record_commit=record_commit,
            )
            if result.commit_sha != run.pending_commit_sha:
                raise GitHubError(
                    "published commit does not match the persisted publication receipt"
                )
        except (GitPublishError, GitHubError, OSError) as exc:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                f"could not publish the pull request: {exc}",
            ) from exc

        run = run.model_copy(
            update={
                "commit_sha": result.commit_sha,
                "reviewed_commit_sha": result.commit_sha,
                "pending_commit_sha": None,
                "pull_request_url": result.pull_request_url,
                "updated_at": utc_now(),
            }
        )
        self._store.save_run(run)
        return self.transition(run, WorkflowState.PR_CREATED)

    def _build_pr_body(
        self, run: FactoryRun, context: _RunContext, changed_files: list[str]
    ) -> str:
        body = build_pr_body(
            work_item=context.work_item,
            specification=context.specification,
            plan=context.execution_plan,
            changed_files=changed_files,
            verification=context.latest_verification,
            test_report=context.latest_test_report,
            review=context.latest_review,
            review_acceptance=run.review_acceptance,
            run_id=run.id,
        )
        if run.reviewed_tree_sha is not None:
            if run.needs_look:
                label = "Git tree published without a passing review"
            elif run.review_acceptance is not None:
                label = "Controller-accepted reviewed Git tree"
            else:
                label = "Reviewer-approved Git tree"
            body += f"\n{label}: `{run.reviewed_tree_sha}`\n"
        return body

    def _ci_loop(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """Poll CI, and repair (bounded by ``ci.repair_attempts``) when the
        failure is genuinely a code/test failure."""
        observer = self._resolve_ci_observer()
        while True:
            if run.state is not WorkflowState.CI_RUNNING:
                run = self.transition(run, WorkflowState.CI_RUNNING)
            assert run.pull_request_url is not None
            try:
                report = observer.observe(
                    repo_path=context.workspace.path,
                    pull_request_url=run.pull_request_url,
                    repair_attempts_used=self._attempts_used(run, AttemptBudget.CI_REPAIR),
                )
            except (GitPublishError, GitHubError, OSError) as exc:
                raise self._halt(
                    run, WorkflowState.NEEDS_HUMAN, f"could not observe CI: {exc}"
                ) from exc
            self._store.save_artifact(run.id, report)

            if report.timed_out:
                reason = "CI checks were still pending after the configured wait budget"
                if run.unattended:
                    return self._leave_open(run, context, reason)
                raise self._halt(run, WorkflowState.NEEDS_HUMAN, reason)
            if report.overall == "PASS":
                return self._merge_and_finish(run, context)

            run = self.transition(run, WorkflowState.CI_DIAGNOSIS)
            stop_reason = self._ci_stop_reason(run, report)
            if stop_reason is not None:
                if run.unattended:
                    return self._leave_open(run, context, stop_reason)
                raise self._halt(run, WorkflowState.NEEDS_HUMAN, stop_reason)

            repair_context = self._ci_repair_context(report)
            run = self.transition(run, WorkflowState.IMPLEMENTING)
            run = self._drive_to_pr_ready(run, context, AttemptBudget.CI_REPAIR, repair_context)
            if run.state is WorkflowState.DONE:
                return run
            run = self._publish(run, context)

    def _ci_stop_reason(self, run: FactoryRun, report: CIReport) -> str | None:
        """Why a red CI result cannot be repaired, or ``None`` to repair it."""
        failed = report.failed_checks
        if report.overall == "CANCELLED" or not failed:
            return f"CI finished with status {report.overall} and no repairable failure"
        if not {check.failure_category or "UNKNOWN" for check in failed} <= (
            REPAIRABLE_CI_CATEGORIES
        ):
            return "CI failure is not repairable by a code change: " + ", ".join(
                f"{check.name}={check.failure_category or 'UNKNOWN'}" for check in failed
            )
        used = self._attempts_used(run, AttemptBudget.CI_REPAIR)
        if used >= self._config.ci.repair_attempts:
            return self._budget_exhausted_reason(AttemptBudget.CI_REPAIR, used)
        return None

    def _merge_and_finish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        if run.needs_look:
            return self._leave_open(run, context)
        if self._config.merge.enabled:
            assert self._merger is not None
            if (
                not run.commit_sha
                or not run.pull_request_url
                or not run.delivery_base_branch
                or run.reviewed_commit_sha != run.commit_sha
                or not run.delivery_repository
                or not run.delivery_host
            ):
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "missing delivery evidence or review authorization for the current head",
                )
            try:
                self._check_delivery_workspace(run, context.workspace.path)
                result = self._merger.merge(
                    repo_path=context.workspace.path,
                    pull_request_url=run.pull_request_url,
                    expected_head_sha=run.commit_sha,
                    base_branch=run.delivery_base_branch,
                    expected_repository=run.delivery_repository,
                    expected_host=run.delivery_host,
                )
            except (
                GitHubError,
                GitPublishError,
                OSError,
                WorkspaceError,
                subprocess.TimeoutExpired,
            ) as exc:
                raise self._halt(
                    run, WorkflowState.NEEDS_HUMAN, f"could not merge the pull request: {exc}"
                ) from exc
            run = run.model_copy(
                update={"merge_commit_sha": result.commit_sha, "updated_at": utc_now()}
            )
            self._store.save_run(run)
        return self.transition(run, WorkflowState.DONE)


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
        execution_plan: ExecutionPlan,
        repository_profile: RepositoryProfile,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
        route_decision: RouteDecision | None = None,
        effective_route: ExecutionRoute = ExecutionRoute.FULL,
        original_synthesized_scope: tuple[str, ...] | None = None,
    ) -> None:
        self.work_item = work_item
        self.triage_result = triage_result
        self.specification = specification
        self.execution_plan = execution_plan
        self.repository_profile = repository_profile
        self.route_decision = route_decision
        self.effective_route = (
            route_decision.effective_route if route_decision is not None else effective_route
        )
        self.original_synthesized_scope = original_synthesized_scope
        self.polish_attempted = False
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


def _review_finding_id(draft: ReviewFindingDraft) -> str:
    locations = ",".join(
        f"{location.path}:{location.start_line}-{location.end_line}"
        for location in sorted(
            draft.locations,
            key=lambda item: (item.path, item.start_line, item.end_line),
        )
    )
    digest = hashlib.sha256(f"{draft.category.value}:{locations}".encode("utf-8")).hexdigest()[:16]
    return f"review-{draft.category.value.lower()}-{digest}"


def _deduplicate_review_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    deduplicated: dict[str, ReviewFinding] = {}
    for finding in findings:
        deduplicated.setdefault(finding.id, finding)
    return list(deduplicated.values())


def _review_finding_paths(findings: list[ReviewFinding]) -> set[str]:
    return {location.path for finding in findings for location in finding.locations}


def _locations_overlap(
    left: ReviewSourceLocation,
    right: ReviewSourceLocation,
) -> bool:
    return (
        left.path == right.path
        and left.start_line <= right.end_line
        and right.start_line <= left.end_line
    )


def _review_draft_overlaps_finding(
    draft: ReviewFindingDraft,
    finding: ReviewFinding,
) -> bool:
    return any(
        _locations_overlap(draft_location, finding_location)
        for draft_location in draft.locations
        for finding_location in finding.locations
    )


def _review_draft_matches_finding(
    draft: ReviewFindingDraft,
    finding: ReviewFinding,
) -> bool:
    return (
        draft.category is finding.category
        and draft.message == finding.message
        and draft.locations == finding.locations
    )


def _partition_review_drafts(
    prior: list[ReviewFinding],
    drafts: list[ReviewFindingDraft],
) -> tuple[set[str], list[ReviewFindingDraft]]:
    overlapping_ids: set[str] = set()
    new_drafts: list[ReviewFindingDraft] = []
    for draft in drafts:
        overlapping = {
            finding.id for finding in prior if _review_draft_overlaps_finding(draft, finding)
        }
        if overlapping:
            overlapping_ids.update(overlapping)
        else:
            new_drafts.append(draft)
    return overlapping_ids, new_drafts


def _changed_line_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            ranges.setdefault(current_path, [])
            continue
        if current_path is None:
            continue
        match = _DIFF_HUNK_PATTERN.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        end = start + max(count, 1) - 1
        ranges[current_path].append((start, end))
    return ranges


def _review_draft_intersects_ranges(
    draft: ReviewFindingDraft,
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> bool:
    return any(
        location.start_line <= end and start <= location.end_line
        for location in draft.locations
        for start, end in changed_ranges.get(location.path, [])
    )


def _dependency_names(profile: RepositoryProfile) -> tuple[str, ...]:
    """Return the dependency names the profile declares, for stack review lenses."""
    return tuple(sorted({dependency.name for dependency in profile.dependencies}))


def _commit_message(context: _RunContext, run_id: str) -> str:
    summary = context.execution_plan.summary.strip().rstrip(".").strip()
    return (
        f"{summary}\n\n"
        f"{context.specification.problem.strip()}\n\n"
        f"Factory run: `{run_id}`\n"
        f"Work item: `{context.work_item.id}`"
    )
