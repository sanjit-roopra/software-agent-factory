"""The single authoritative :class:`WorkflowController`.

Per ``AGENTS.md`` ("One authoritative workflow controller"), only this
controller mutates a :class:`~software_agent_factory.models.FactoryRun`'s state.
Its pull request delivery (:mod:`.pr_delivery`), restart and resume
(:mod:`.run_recovery`) and reopen and escalation (:mod:`.run_reopen`) live in
their own modules as functions that take the controller as their first argument.
Agents
(via :mod:`software_agent_factory.agents`) return typed artifacts and
outcomes; they never transition the run themselves, and their claims about
what changed on disk are never trusted -- ``changed_files`` and the diff are
always re-derived from :meth:`GitWorktreeWorkspace.collect_evidence`.

The allowed transition table is declared as data in
:mod:`software_agent_factory.lifecycle` (``ALLOWED_TRANSITIONS``) and enforced
by :meth:`WorkflowController.transition`:

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
import logging
import os
import socket
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from . import pr_delivery, review_ledger, run_recovery, run_reopen, scope_replan
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
from .git_safety import GitPublishError
from .github import GitHubClient, GitHubError, resolve_github_token
from .governance import (
    RepositoryVerificationResult,
    RepositoryVerifier,
    ScopeAssessment,
    ScopeDecision,
    ScopeDriftPolicy,
    find_protected_matches,
)
from .lifecycle import (
    ALLOWED_TRANSITIONS,
    RECOVERY_TRANSITIONS,
    TERMINAL_STATES,
    is_run_finished,
)
from .models import (
    MAX_OPEN_REVIEW_FINDINGS,
    STOPPED_HALT_REASON,
    UNRESOLVED_DECISIONS_HALT_REASON,
    ActiveInvocation,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
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
    PlanningResult,
    PlanStep,
    RejectedCommand,
    RepairContext,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    RepositoryProfile,
    ReviewAcceptance,
    ReviewAcceptanceReason,
    ReviewFinding,
    ReviewImpasse,
    ReviewImpasseKind,
    ReviewReport,
    Risk,
    RouteDecision,
    RunLaunch,
    RunLease,
    RunSteerNote,
    RunStopRequest,
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
from .redaction import redact_secrets
from .repair_context import (
    implementer_failure_context,
    planner_clarification_context,
    polish_context,
    review_contract_repair_context,
    review_repair_context,
    typed_artifact_repair_context,
    verification_artifact_repair_context,
    verification_mutation_repair_context,
    verification_repair_context,
    with_repair_memory,
)
from .repository_profile import (
    generic_repository_profile,
    is_version_file,
    profile_repository,
)
from .revert_check import revert_check
from .routing import ModelRouter, derive_named_paths, determine_route
from .run_context import (
    SYNTHESIZED_ROUTES,
    Halt,
    RunContext,
    describe_scope,
)
from .run_context import (
    TransitionError as TransitionError,
)
from .run_context import (
    delivery_policy_fingerprint as delivery_policy_fingerprint,
)
from .run_watch import HEARTBEAT_INTERVAL_SECONDS, live_activity, watched
from .store import FileRunStore
from .subprocess_utils import kill_thread_children
from .suppression_scan import suppression_scan
from .telemetry import (
    count_operation,
    measure_operation,
    record_gate_failure,
    record_rework,
)
from .toolchain import degraded_toolchain_inventory, inventory_toolchain
from .toolchain_commands import candidate_commands, root_version_files, select_package_runner
from .transcripts import store_call_transcript
from .verification import DeterministicVerifier
from .workspace import (
    GitWorktreeWorkspace,
    WorkspaceError,
    WorkspaceEvidence,
    WorkspaceLockError,
)

logger = logging.getLogger(__name__)


def _fold_legacy_concerns(review: ReviewReport) -> ReviewReport:
    """Move text from the legacy string concern fields into ``suggested_changes``.

    Only typed ``blocking_findings`` with source locations can block a change.
    A reviewer that still fills the legacy fields gives notes that the
    controller cannot act on, so they become non-blocking suggestions instead
    of failing the run.
    """
    legacy = [
        *review.findings,
        *review.scope_concerns,
        *review.security_concerns,
        *review.compatibility_concerns,
    ]
    if not legacy:
        return review
    return review.model_copy(
        update={
            "findings": [],
            "scope_concerns": [],
            "security_concerns": [],
            "compatibility_concerns": [],
            "suggested_changes": [*review.suggested_changes, *legacy],
        }
    )


class RunCancelledError(BaseException):  # NOSONAR(S5709) must pass `except Exception` guards
    """The service asked a run to stop because it stalled.

    A ``BaseException`` like ``KeyboardInterrupt``, so the broad ``except Exception``
    guards on the way up do not swallow it. ``finally`` blocks still release locks.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


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
        launch: RunLaunch | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._runtime = runtime
        self._launch = launch
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
        #: Seconds between lease heartbeats during an agent call. Tests shorten it.
        self.heartbeat_interval_seconds = HEARTBEAT_INTERVAL_SECONDS
        #: Run ids the service asked to stop, and the thread each run's agent call runs on.
        #: Shared by ``with_pinned_policy`` clones on purpose.
        self._cancel_requests: set[str] = set()
        self._agent_threads: dict[str, int] = {}
        if self._github is None and self._publisher is not None:
            self._github = getattr(self._publisher, "_client", None)
        if self._github is None and (config.escalation.enabled or config.pull_request.enabled):
            self._github = GitHubClient(token=resolve_github_token())

    def cancel_run(self, run_id: str) -> None:
        """Stop a stalled run: kill its live agent and stop it before its next agent call.

        The run's own thread raises :class:`RunCancelledError` at the next check.
        """
        self._cancel_requests.add(run_id)
        thread_id = self._agent_threads.get(run_id)
        if thread_id is not None:
            kill_thread_children(thread_id)

    def clear_cancel(self, run_id: str) -> None:
        """Forget a stop request once the run's thread has returned."""
        self._cancel_requests.discard(run_id)

    def _raise_if_cancelled(self, run: FactoryRun) -> None:
        """Halt on a person's stop request (ADR-047), or raise for a stall (ADR-048)."""
        request = self._store.load_stop_request(run.id)
        if request is not None:
            raise self._halt_on_stop_request(run, request)
        if run.id in self._cancel_requests:
            raise RunCancelledError(run.id)

    def _halt_on_stop_request(self, run: FactoryRun, request: RunStopRequest) -> Halt:
        """Halt ``run`` for a person, keeping its workspace and commits.

        No escalation notice: the person who asked for the stop already knows.
        """
        reason = (
            f"{STOPPED_HALT_REASON} from the {request.source} at "
            f"{request.created_at.isoformat(timespec='seconds')}"
        )
        logger.warning("run %s %s", run.id, reason)
        run = self._close_active_invocation(run, reason)
        # The terminal transition deletes the request.
        return Halt(self.transition(run, WorkflowState.NEEDS_HUMAN, failure_reason=reason))

    def _call_with_steer_note(self, run: FactoryRun, request: AgentRequest) -> AgentResult:
        """Run the agent with a person's waiting steer note added to ``request`` (ADR-047).

        The note reaches exactly one agent call that returns: the next one after it was
        sent. A call that raises puts the note back, create-only, so a newer note wins and
        the retry or the next attempt still gets it.
        """
        note = self._store.take_steer_note(run.id)
        if note is None:
            return self._call_runtime(run, request)
        logger.info("run %s sends the %s steer note to the %s", run.id, note.source, request.role)
        steered = request.model_copy(update={"operator_note": redact_secrets(note.note)})
        try:
            return self._call_runtime(run, steered)
        except BaseException:
            self._restore_steer_note(note)
            raise

    def _restore_steer_note(self, note: RunSteerNote) -> None:
        """Put back a steer note whose agent call failed. A failure here is only logged."""
        try:
            self._store.create_steer_note(note)
        except OSError as exc:
            logger.warning("run %s lost its steer note: %s", note.run_id, type(exc).__name__)

    def _call_runtime(self, run: FactoryRun, request: AgentRequest) -> AgentResult:
        """Run the agent while a side thread writes the lease heartbeat (dashboard X1).

        The heartbeat stops once the call outlives its timeout, and pauses while the agent
        is silent, so a runtime that hangs looks stalled to the service (ADR-048).
        """
        with live_activity(run) as live, watched(self, run, request.timeout_seconds, live):
            return self._runtime.run(request)

    def with_pinned_policy(self, *, risk_assessment: bool, unattended: bool) -> WorkflowController:
        """Return a controller that starts new runs with the given policy.

        Project resume uses it so tasks not yet dispatched follow the choices the
        project started with. Existing runs always follow their own persisted values.
        """
        clone = copy.copy(self)
        clone._config = self._config.model_copy(
            update={
                "risk_assessment": RiskAssessmentConfig(enabled=risk_assessment),
                "factory": self._config.factory.model_copy(update={"unattended": unattended}),
            }
        )
        return clone

    # -- public transition API -----------------------------------------

    def transition(
        self,
        run: FactoryRun,
        new_state: WorkflowState,
        *,
        failure_reason: str | None = None,
        operator_escalation: bool = False,
    ) -> FactoryRun:
        """Move ``run`` to ``new_state``, persisting the result.

        Raises :class:`TransitionError` if ``new_state`` is not reachable
        from ``run.state`` per ``ALLOWED_TRANSITIONS``. Only an operator
        escalation may also move a run to ``PLANNING`` (ADR-055). Every transition
        refreshes ``last_activity_at`` (the scheduler's stall signal) and
        either stamps (terminal) or clears (active) ``completed_at`` so a
        repaired run never keeps a stale completion timestamp.
        """
        allowed = ALLOWED_TRANSITIONS.get(run.state, frozenset())
        if operator_escalation:
            allowed = allowed | RECOVERY_TRANSITIONS["operator_escalation"]
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
            # A stop request the run did not reach must not stop a later resume of it.
            self._store.delete_stop_request(run.id)
            if new_state is not WorkflowState.NEEDS_HUMAN:
                # A halted run keeps its steer note for the call after it resumes.
                self._store.delete_steer_note(run.id)
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
                classify_halt_reason,
                generate_episode_id,
                github_resume_enabled,
            )
            from .halt_contexts import build_resume_context

            classification, code, summary, action = classify_halt_reason(
                run.model_copy(update={"state": new_state, "failure_reason": failure_reason}),
                self._store,
            )
            prev_escalation = run.escalation
            episode_num = (prev_escalation.episode_number + 1) if prev_escalation else 1
            reopen_count = prev_escalation.reopen_count if prev_escalation else 0
            accepted_replies = prev_escalation.accepted_replies if prev_escalation else []
            episode_id = generate_episode_id()
            classification, context_fields = build_resume_context(
                run, self._store, self._config, classification, episode_id
            )
            remote_resume_enabled = github_resume_enabled(
                run,
                self._config,
                episode_id=episode_id,
                classification=classification,
                halt=(code, summary, action),
                reopen_count=reopen_count,
                context_fields=context_fields,
            )
            updates["escalation"] = EscalationRecord(
                episode_id=episode_id,
                episode_number=episode_num,
                status=EscalationStatus.PENDING_NOTIFICATION,
                resume_classification=classification,
                reason_code=code,
                reopen_count=reopen_count,
                accepted_replies=accepted_replies,
                remote_resume_enabled=remote_resume_enabled,
                **context_fields,
            )

        run = run.model_copy(update=updates)
        self._store.save_run(run)
        logger.info(
            "run %s -> %s%s",
            run.id,
            new_state.value,
            f" ({failure_reason})" if failure_reason else "",
            # Correlation fields for the structured JSON log
            # (factory_logging._RedactingJsonFormatter); ignored by a plain
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

    def _close_active_invocation(self, run: FactoryRun, reason: str) -> FactoryRun:
        """Record the invocation a dead process left open as failed."""
        active = run.active_invocation
        if active is None:
            return run
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
                "active_invocation": None,
                "updated_at": now,
                "last_activity_at": now,
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
        run = self._close_active_invocation(run, reason)
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
            source_repo=str(source_repo.expanduser().resolve()),
            launch=self._launch,
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
            return self._prepare_and_execute(run, work_item, workspace, source_repo)
        finally:
            # Workspaces are preserved by default (docs/architecture.md,
            # "Workspace lifecycle"): only the lock is released here, the
            # worktree itself is left in place for inspection/reuse.
            workspace.release_lock()

    def _prepare_and_execute(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
    ) -> FactoryRun:
        """Prepare the worktree, profile the repository, then run the pipeline.

        The caller holds the workspace lock. A new run and an unattended run that has no
        stored work to continue (ADR-043) both start here.
        """
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

    def _rejected_commands(self, run_id: str) -> tuple[RejectedCommand, ...]:
        """Return the derived commands the run did not use, for the PR body."""
        try:
            return self._store.load_artifact(run_id, RepositoryCommandsPlan).rejected
        except FileNotFoundError:
            return ()

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
        """Reconcile a delivery checkpoint without resetting any attempt budget."""
        return run_recovery.resume(self, run_id, source_repo)

    def reopen(self, run_id: str, source_repo: Path) -> FactoryRun:
        """Controller-owned API to reopen an eligible run halted in NEEDS_HUMAN."""
        return run_reopen.reopen(self, run_id, source_repo)

    def escalate(self, run_id: str, note: str) -> FactoryRun:
        """Redo a run one route higher with a person's note (ADR-055)."""
        return run_reopen.escalate(self, run_id, note)

    def _fail_reopen(
        self,
        run: FactoryRun,
        reason: str,
        *,
        reason_code: HaltReasonCode = HaltReasonCode.RECOVERY_INTERVENTION,
    ) -> FactoryRun:
        """End a failed reopen with a non-resumable escalation episode for the service."""
        return run_reopen.fail_reopen(self, run, reason, reason_code=reason_code)

    # -- internal orchestration --------------------------------------------

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
        context: RunContext,
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
        return self._record_route(run, context, to_route, reason)

    def _record_route(
        self,
        run: FactoryRun,
        context: RunContext,
        to_route: ExecutionRoute,
        reason: str,
    ) -> FactoryRun:
        """Move the effective route to ``to_route`` and record the adjustment."""
        logger.info(
            "Ratcheting route for run %s: %s -> %s (reason: %s)",
            run.id,
            context.effective_route,
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
            run = self.transition(run, WorkflowState.TRIAGING)
            triage_result = self._run_triage(run, work_item, workspace_path=str(workspace.path))
            run, route_decision = self._decide_route(
                run, work_item, repository_profile, triage_result
            )

            # Unattended runs take FULL instead of manual triage.
            if (
                route_decision.effective_route is ExecutionRoute.MANUAL_TRIAGE
                and not run.unattended
            ):
                reason = (
                    route_decision.fallback_reason
                    or f"routing selected manual triage: {route_decision.selected_option}"
                )
                raise self._halt(run, WorkflowState.NEEDS_HUMAN, reason)
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
            if route_decision.effective_route in SYNTHESIZED_ROUTES:
                # SINGLE skips the planner, tester and reviewer; CRITIQUE keeps the reviewer.
                saved_calls = 3 if route_decision.effective_route is ExecutionRoute.SINGLE else 2
                run.performance.record_counter(
                    "route.saved_calls",
                    saved_calls,
                    stage=WorkflowState.TRIAGING.value,
                    operation="routing",
                )
                return self._drive_synthesized(
                    run,
                    work_item,
                    triage_result,
                    route_decision,
                    workspace,
                    source_repo,
                    repository_profile,
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
        except Halt as halt:
            return halt.run

    def _decide_route(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        repository_profile: RepositoryProfile,
        triage_result: TriageResult,
    ) -> tuple[FactoryRun, RouteDecision]:
        """Pick the route from the triage result and record it on the run (ADR-037)."""
        route_decision = determine_route(work_item, repository_profile, self._config, triage_result)
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
        run.performance.record_counter(
            f"route.{route_decision.effective_route.value}",
            1,
            stage=WorkflowState.TRIAGING.value,
            operation="routing",
        )
        return run, route_decision

    def _drive_synthesized(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        triage_result: TriageResult,
        route_decision: RouteDecision | None,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
        repository_profile: RepositoryProfile,
        *,
        repair_context: RepairContext | None = None,
    ) -> FactoryRun:
        """Synthesize the specification and the plan, then continue from ``PLANNING``.

        The ``SINGLE`` and ``CRITIQUE`` routes make no Planner call. A given ``repair_context``
        goes to the first Implementer attempt.
        """
        specification = self._synthesize_specification(work_item)
        self._store.save_artifact(run.id, specification)
        execution_plan = self._synthesize_execution_plan(
            work_item, self._commands_for_run(run.id).verify
        )
        self._store.save_artifact(run.id, execution_plan)

        context = RunContext(
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
        run = self._drive_to_pr_ready(run, context, AttemptBudget.IMPLEMENTATION, repair_context)

        if not self._config.pull_request.enabled:
            return self.finalize_pr_ready(run)

        return pr_delivery.publish_and_observe(self, run, context)

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
        repair_context: RepairContext | None = None,
    ) -> FactoryRun:
        """Plan and continue from a controller-owned PLANNING state.

        One planner call writes the specification and the plan (ADR-035). A
        given ``specification`` is the one an earlier planner call wrote. A given
        ``repair_context`` goes to the first Implementer attempt.
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
            clarification_context = planner_clarification_context(
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

        context = RunContext(
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
        run = self._drive_to_pr_ready(run, context, AttemptBudget.IMPLEMENTATION, repair_context)

        if not self._config.pull_request.enabled:
            return self.finalize_pr_ready(run)

        return pr_delivery.publish_and_observe(self, run, context)

    def _halt(self, run: FactoryRun, state: WorkflowState, reason: str) -> Halt:
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
                logger.warning("run %s: escalation notice delivery failed: %s", run.id, exc)
        return Halt(run)

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
            repair_context = typed_artifact_repair_context(
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
            current_repair_context = typed_artifact_repair_context(
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
        context: RunContext,
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
            repair_context = typed_artifact_repair_context(
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
        context: RunContext,
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
                review = _fold_legacy_concerns(result.review_report)
                semantic_failure = self._review_contract_failure(
                    review,
                    prior_findings,
                    evidence,
                    context.workspace,
                )
                if semantic_failure is None:
                    return review
                repair_context = review_contract_repair_context(semantic_failure)
                continue
            if not is_retryable_typed_artifact_failure(result, ReviewReport):
                break
            assert result.failure_reason is not None
            repair_context = typed_artifact_repair_context(
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
            runtime=resolved.runtime,
            provider=resolved.provider,
            fallback=resolved.fallback,
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
        self._raise_if_cancelled(run)
        self._store.save_run(run)
        try:
            result = self._call_with_steer_note(run, request)
        except (OSError, RuntimeError, ValueError) as exc:
            self._raise_if_cancelled(run)
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
        self._raise_if_cancelled(run)
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
                fallback_reason=result.fallback_reason,
                transcript_path=store_call_transcript(
                    self._store.run_dir(run.id), invocation_number, result.transcript
                ),
                session_ref=result.session_ref,
            )
        )
        run.active_invocation = None
        run.updated_at = completed_at
        run.last_activity_at = completed_at
        self._store.save_run(run)
        return result

    # -- bounded implementation/repair loop ---------------------------------

    def _attempt_limit(self, run: FactoryRun, budget: AttemptBudget) -> int:
        """The attempts ``budget`` allows: the configured ones plus any a person granted."""
        configured = (
            self._config.ci.repair_attempts
            if budget is AttemptBudget.CI_REPAIR
            else self._config.retries.max_total_attempts
        )
        return configured + run.attempt_grants.get(budget, 0)

    def _select_worker(
        self, run: FactoryRun, context: RunContext, budget: AttemptBudget
    ) -> tuple[RoleModelConfig | None, int]:
        used = run.attempts_used(budget)
        attempt_number = used + 1
        if used >= self._attempt_limit(run, budget):
            return None, attempt_number
        model_profile = (
            context.route_decision.selected_model_profile
            if context.route_decision is not None
            and context.route_decision.selected_model_profile is not None
            and budget is AttemptBudget.IMPLEMENTATION
            else None
        )
        if model_profile == "default":
            model_profile = None
        # A granted or CI repair attempt routes as the last configured attempt does.
        routing_attempt = min(attempt_number, self._config.retries.max_total_attempts)
        return (
            self._router.model_for_implementer(
                context.triage_result.complexity, routing_attempt, model_profile=model_profile
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
        context: RunContext,
        budget: AttemptBudget,
        repair_context: RepairContext | None,
        resume_evidence: WorkspaceEvidence | None = None,
    ) -> FactoryRun:
        """Drive an ``IMPLEMENTING`` run through the deterministic and
        independent gates until it reaches ``PR_READY``.

        Shared by the pre-PR loop and the post-CI repair loop; only the
        consumed :class:`AttemptBudget` differs. With ``resume_evidence`` the first
        round verifies that work again instead of implementing.
        """
        verification_generated_paths: set[str] = set()
        while True:
            if resume_evidence is None:
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
                        return pr_delivery.publish_as_is(self, run, context, reason)
                    run = run.model_copy(update={"exhausted_repair": repair_context})
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
                    run = run.model_copy(
                        update={"review_acceptance": None, "updated_at": utc_now()}
                    )
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
                repair_context = with_repair_memory(
                    context.repair_signatures, repair_context, snapshot - 1
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
                    repair_context = implementer_failure_context(run.attempt_records[-1])
                    continue
                assert evidence is not None

                retained_generated_paths = sorted(
                    verification_generated_paths.intersection(evidence.changed_files)
                )
                if retained_generated_paths:
                    repair_context = verification_artifact_repair_context(retained_generated_paths)
                    continue
                verification_generated_paths.clear()
            else:  # A person approved a wider scope (ADR-060): check the halted attempt again.
                evidence, resume_evidence = resume_evidence, None
                snapshot = len(run.attempt_records)
                context.latest_evidence = evidence

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
                repair_context = verification_repair_context(verification)
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue
            if not verification.report.deterministic_checks:
                run = pr_delivery.add_needs_look(self, run, "no verify command ran")

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
                repair_context = verification_mutation_repair_context(
                    evidence,
                    verified_evidence,
                )
                run = self.transition(run, WorkflowState.IMPLEMENTING)
                continue
            evidence = verified_evidence

            # Monotonic post-implementation ratchets
            # 1. Evaluate unexpected scope against original synthesized scope
            # before any scope replan
            if context.effective_route in SYNTHESIZED_ROUTES:
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
                run.approved_scope_paths,
            )
            while scope.decision is ScopeDecision.REPLAN:
                if (
                    run.unattended
                    and scope_replan.replans_used(run) >= self._config.scope_drift.max_replans
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
                run = scope_replan.replan(self, run, context, scope, evidence)
                scope = self._scope_policy.assess(
                    context.execution_plan,
                    evidence.changed_files,
                    context.triage_result.risk,
                    run.approved_scope_paths,
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
                        "scope metadata replan made no progress: " + describe_scope(scope),
                    )
            if scope.decision is ScopeDecision.NEEDS_HUMAN and not run.unattended:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "scope drift requires human review: " + describe_scope(scope),
                )

            # Monotonic post-implementation ratchets (continued)
            # 2. Excessive changed files
            if len(evidence.changed_files) > self._config.routing.single_max_changed_files:
                if context.effective_route in SYNTHESIZED_ROUTES:
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
                repair_context = polish_context()
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
                    run = pr_delivery.add_needs_look(
                        self, run, f"review impasse {impasse.kind} was accepted"
                    )
                acceptance_reason = ReviewAcceptanceReason.REVIEW_IMPASSE
            elif (
                impasse is None
                and run.review_ledger.open_findings
                and review_rounds >= self._config.review.max_rounds
            ):
                acceptance_reason = ReviewAcceptanceReason.REVIEW_ROUND_LIMIT

            if (
                acceptance_reason is None
                and impasse is None
                and run.review_ledger.open_findings
                and run.attempts_used(budget) >= self._attempt_limit(run, budget)
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
                    impasse = review_ledger.impasse(
                        run.review_ledger,
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
                repair_context = review_repair_context(
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
                    "review_ledger": review_ledger.clear_open_findings(run.review_ledger),
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

    def _verify(self, run: FactoryRun, context: RunContext) -> RepositoryVerificationResult:
        """Run install, verify and build with persisted logs, then the advisory checks.

        Each step is watched: it writes heartbeats, and a stop kills its command.
        """
        commands = self._commands_for_run(run.id)
        command_timeout = self._config.repository.command_timeout_seconds
        all_commands = len(commands.install) + len(commands.verify) + len(commands.build)
        with watched(self, run, command_timeout * max(all_commands, 1)):
            result = self._verifier.run(
                commands,
                cwd=context.workspace.path,
                run_dir=self._store.run_dir(run.id),
                timeout_seconds=command_timeout,
                env_passthrough=self._config.repository.env_passthrough,
                capture_bytes=self._config.repository.log_capture_bytes,
                before_command=lambda: self._raise_if_cancelled(run),
            )
        evidence = context.latest_evidence
        if not result.report.passed or evidence is None or not result.report.deterministic_checks:
            # No verify command ran, so the repository's code is not run here either.
            return result
        with watched(self, run, command_timeout):
            mutation = self._mutation_check(run, context, evidence)
        with watched(self, run, command_timeout * max(len(commands.verify), 1)):
            reverted = revert_check(
                self._command_runner,
                context.workspace,
                evidence,
                commands.verify,
                self._config.repository,
            )
        advisory = (mutation, reverted, suppression_scan(evidence.diff))
        return result.with_checks(check for check in advisory if check is not None)

    def _mutation_check(
        self, run: FactoryRun, context: RunContext, evidence: WorkspaceEvidence
    ) -> CommandResult | None:
        """Return the advisory mutation gate check for a passed verification (ADR-034).

        The gate runs only for changed Python modules in a lane whose mutation
        tool and package runner are known. Its result is one more check in the
        verification report, which the tester and the reviewer read. It never
        fails verification, because a surviving mutant can be equivalent.
        """
        exec_prefix = self._mutation_exec_prefix(run)
        if exec_prefix is None:
            return None
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
        return mutation_check_result(report, time.monotonic() - started)

    def _run_mutation_gate(
        self, context: RunContext, exec_prefix: str, targets: tuple[MutationTarget, ...]
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
        context: RunContext,
        budget: AttemptBudget,
        trigger: AttemptTrigger,
        repair_context: RepairContext | None,
    ) -> tuple[FactoryRun, bool, WorkspaceEvidence | None]:
        started_at = utc_now()
        # A first attempt starts clean: only a repair sees the current diff.
        evidence = context.latest_evidence if repair_context is not None else None
        request = AgentRequest(
            role=AgentRole.IMPLEMENTER,
            model=role_model.model,
            reasoning=role_model.reasoning,
            context_tier=role_model.context_tier,
            runtime=role_model.runtime,
            provider=role_model.provider,
            fallback=role_model.fallback,
            work_item=context.work_item,
            specification=context.specification,
            execution_plan=context.execution_plan,
            repair_context=repair_context,
            diff=evidence.diff if evidence is not None else None,
            changed_files=list(evidence.changed_files) if evidence is not None else [],
            dependency_names=_dependency_names(context.repository_profile),
            workspace_path=str(context.workspace.path),
            attempt_number=attempt_number,
            verification_commands=tuple(self._commands_for_run(run.id).verify),
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

    def _should_polish(
        self,
        run: FactoryRun,
        budget: AttemptBudget,
        context: RunContext,
    ) -> bool:
        if (
            budget is not AttemptBudget.IMPLEMENTATION
            or not self._config.polish.enabled
            or context.effective_route in SYNTHESIZED_ROUTES
        ):
            return False
        if context.polish_attempted:
            return False
        if any(attempt.triggered_by is AttemptTrigger.POLISH for attempt in run.attempt_records):
            return False
        return run.attempts_used(budget) + 1 < self._config.retries.max_total_attempts

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
        ledger, effective, impasse = review_ledger.apply_review(
            run.review_ledger,
            review,
            snapshot=snapshot,
            tree_sha=tree_sha,
            repair_diff=repair_diff,
            blocked_categories=self._config.review.blocked_categories,
        )
        run = run.model_copy(update={"review_ledger": ledger, "updated_at": utc_now()})
        self._store.save_run(run)
        return run, effective, impasse

    def _review_rounds_used(self, run: FactoryRun, budget: AttemptBudget) -> int:
        return sum(
            1
            for attempt in run.attempt_records[run.budget_start :]
            if attempt.budget is budget and attempt.reviewed_tree_sha is not None
        )

    def _accept_review_findings(
        self,
        run: FactoryRun,
        context: RunContext,
        verification: VerificationReport,
        review: ReviewReport,
        snapshot: int,
        tree_sha: str | None,
        review_rounds: int,
        reason: ReviewAcceptanceReason,
    ) -> ReviewAcceptance | None:
        findings = review_ledger.deduplicate_findings(
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
        if not run.unattended and not (
            review_ledger.within_policy(
                self._config.review, context.triage_result.risk, findings, review
            )
            and review_ledger.has_acceptable_origins(findings)
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
        ledger = review_ledger.clear_open_findings(run.review_ledger).model_copy(
            update={"accepted_findings": acceptance.findings}
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
            return run.reviewed_tree_sha is not None

        acceptance = run.review_acceptance
        if acceptance is None:
            return review.approved
        if not (
            run.reviewed_tree_sha is not None
            and acceptance.reviewed_tree_sha == run.reviewed_tree_sha
            and acceptance.findings == run.review_ledger.accepted_findings
        ):
            return False
        exempt = run.unattended or acceptance.reason is ReviewAcceptanceReason.OPERATOR  # ADR-059
        return exempt or self._acceptance_within_policy(acceptance, review, risk)

    def _acceptance_within_policy(
        self, acceptance: ReviewAcceptance, review: ReviewReport, risk: Risk
    ) -> bool:
        """Whether the configured review policy allows an attended run to
        accept these findings."""
        return acceptance.risk is risk and review_ledger.within_policy(
            self._config.review, acceptance.risk, acceptance.findings, review
        )

    # -- publishing and CI observation ---------------------------------------

    def _stop(self, run: FactoryRun, context: RunContext, reason: str) -> Halt:
        """Stop a run. An unattended run with a pull request leaves it open
        instead of waiting for a person (ADR-040, ADR-041)."""
        if run.unattended and run.pull_request_url is not None:
            return Halt(pr_delivery.leave_open(self, run, context, reason))
        return self._halt(run, WorkflowState.NEEDS_HUMAN, reason)


def _dependency_names(profile: RepositoryProfile) -> tuple[str, ...]:
    """Return the dependency names the profile declares, for stack review lenses."""
    return tuple(sorted({dependency.name for dependency in profile.dependencies}))
