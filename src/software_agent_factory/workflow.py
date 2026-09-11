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
a metadata-only pass through ``PLANNING`` after green scope drift, and early
exits to ``NEEDS_HUMAN``/``FAILED`` from every non-terminal state.

Terminal states are ``DONE``, ``NEEDS_HUMAN`` and ``FAILED``. ``PR_READY`` is
*not* terminal: with ``pull_request.enabled`` it continues to ``PR_CREATED``.
When pull requests are disabled it is the completed endpoint of the manual
flow, and the controller finalizes it explicitly
(:meth:`WorkflowController.finalize_pr_ready`) by stamping ``completed_at``.

Repository guidance for the optional post-green polish attempt is a shared,
repository-scoped asset rather than a per-run one. The controller reuses the
generated :class:`~software_agent_factory.models.RepositorySkill` stored for
the current ``dependency_fingerprint``, revalidating it in full against the
current profile and the configured source allowlists before use, and only
enters ``RESEARCHING`` when no generated file exists yet. The human-owned
overlay is read (never written) through
:class:`~software_agent_factory.repository_skills.RepositorySkillManager`, and
what the run actually used is snapshotted create-once into the run directory
before any agent sees it.

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

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .agents import (
    AgentRequest,
    AgentResult,
    AgentRuntime,
    is_retryable_typed_artifact_failure,
    runtime_exception_failure_reason,
)
from .config import FactoryConfig, RoleModelConfig
from .delivery import DeliveryTarget, fetch_delivery_target
from .github import SHA_PATTERN, GitHubError, GitPublishError, build_pr_body
from .governance import (
    RepositoryVerificationResult,
    RepositoryVerifier,
    ScopeAssessment,
    ScopeDecision,
    ScopeDriftPolicy,
    assess_publish_gate,
)
from .models import (
    MAX_OPEN_REVIEW_FINDINGS,
    ActiveInvocation,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CIReport,
    ExecutionPlan,
    FactoryRun,
    InvocationRecord,
    RepairContext,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillUse,
    ResearchReport,
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
    RunLease,
    SkillSelectionSource,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    VersionedModel,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .publishing import CIObserver, PullRequestMerger, PullRequestPublisher
from .repository_profile import (
    generic_repository_profile,
    profile_repository,
)
from .repository_skills import (
    MAX_REPOSITORY_SKILL_GENERATION_ATTEMPTS,
    RepositorySkillError,
    RepositorySkillManager,
    RepositorySkillSelection,
    repository_skill_correction_context,
    repository_skill_exhausted_warning,
    repository_skill_validation_error,
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
MAX_LATE_REVIEW_ADOPTION_ROUNDS = 1
MAX_CONSECUTIVE_BLOCKING_REVIEWS_PER_PATH = 3
MAX_CONSECUTIVE_UNRESOLVED_REVIEWS = 3
MAX_CONSECUTIVE_REPLACEMENT_REVIEWS = 2
MAX_REVIEW_IMPASSE_FINDINGS = 12

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
        {
            WorkflowState.PLANNING,
            WorkflowState.IMPLEMENTING,
            WorkflowState.REVIEWING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.PLANNING: frozenset(
        {
            WorkflowState.IMPLEMENTING,
            WorkflowState.VERIFYING,
            WorkflowState.NEEDS_HUMAN,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.IMPLEMENTING: frozenset(
        {WorkflowState.VERIFYING, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
    ),
    WorkflowState.VERIFYING: frozenset(
        {
            WorkflowState.REVIEWING,
            WorkflowState.IMPLEMENTING,
            WorkflowState.PLANNING,
            WorkflowState.RESEARCHING,
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


def _typed_artifact_schema_repair_context(
    failure_reason: str,
    artifact_name: str,
    prior_context: RepairContext | None = None,
) -> str:
    validation_error = failure_reason.split(" stdout=", 1)[0].strip()
    schema_context = (
        "Your previous response was rejected by deterministic schema validation. "
        f"Validation error: {validation_error}. "
        f"Correct only the output shape and return exactly one complete {artifact_name} JSON "
        "object matching the schema in this prompt. Do not omit required fields, add prose, "
        "or wrap the JSON in markdown."
    )
    if prior_context is None:
        return schema_context
    return f"{prior_context.model_dump_json()}\n\n{schema_context}"


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


class WorkItemAlreadyActiveError(Exception):
    """Raised internally when another live run owns this work item's
    workspace. Surfaced as a non-persisted outcome, never as a junk run."""


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
    ) -> None:
        self._config = config
        self._store = store
        self._runtime = runtime
        self._router = router if router is not None else ModelRouter(config)
        self._verifier = (
            repository_verifier if repository_verifier is not None else RepositoryVerifier(verifier)
        )
        self._scope_policy = (
            scope_policy
            if scope_policy is not None
            else ScopeDriftPolicy(
                approved_sensitive_files=config.scope_drift.approved_sensitive_files
            )
        )
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
        self._merger = merger
        if self._merger is None and config.merge.enabled:
            self._merger = PullRequestMerger(config)
        self._delivery_base_resolver = delivery_base_resolver or (
            lambda repo, expected: fetch_delivery_target(config, repo, expected)
        )

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
            updates["active_invocation"] = None
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
        run = FactoryRun(
            id=resolved_run_id,
            work_item_id=work_item.id,
            state=WorkflowState.CREATED,
            delivery_policy_fingerprint=delivery_policy_fingerprint(self._config),
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

    def _restore_delivery_context(
        self, run: FactoryRun, workspace: GitWorktreeWorkspace, source_repo: Path
    ) -> _RunContext:
        work_item = self._store.load_artifact(run.id, WorkItem)
        if work_item.id != run.work_item_id:
            raise ValueError("persisted work item does not match run")
        triage = self._store.load_artifact(run.id, TriageResult)
        if not triage.factory_eligible or self._router.requires_human_approval(triage.risk):
            raise ValueError("persisted triage does not authorize delivery")
        context = _RunContext(
            work_item=work_item,
            triage_result=triage,
            specification=self._store.load_artifact(run.id, Specification),
            research_report=(
                self._store.load_artifact(run.id, ResearchReport) if triage.needs_research else None
            ),
            execution_plan=self._store.load_artifact(run.id, ExecutionPlan),
            repository_profile=self._store.load_artifact(run.id, RepositoryProfile),
            workspace=workspace,
            source_repo=source_repo,
        )
        context.latest_evidence = workspace.collect_evidence()
        if context.latest_evidence.diff != self._store.load_patch(run.id):
            raise ValueError("workspace changes do not match the reviewed delivery checkpoint")
        if not run.reviewed_tree_sha or context.latest_evidence.tree_sha != run.reviewed_tree_sha:
            raise ValueError("workspace tree does not match the reviewed delivery checkpoint")
        if run.base_commit_sha != workspace.base_commit:
            raise ValueError("workspace base does not match the recorded delivery base")
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
        try:
            self._store.load_artifact(run.id, RepositorySkillUse)
        except FileNotFoundError:
            pass
        else:
            repository_skill = self._store.load_artifact(run.id, RepositorySkill)
            if (
                repository_skill.dependency_fingerprint
                == context.repository_profile.dependency_fingerprint
            ):
                context.repository_skill = repository_skill
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
        result = self._invoke_agent(run, request)
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
        result = self._invoke_agent(run, request)
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
        result = self._invoke_agent(run, request)
        if not result.success or result.research_report is None:
            raise self._halt(
                run,
                WorkflowState.FAILED,
                result.failure_reason or "researcher agent failed to produce a result",
            )
        self._store.save_artifact(run.id, result.research_report)
        return result.research_report

    def _run_repository_skill_researcher(
        self,
        run: FactoryRun,
        context: _RunContext,
        repository_profile: RepositoryProfile,
    ) -> tuple[RepositorySkill | None, str | None]:
        """Generate reusable repository guidance for one dependency state.

        The request deliberately carries no work item evidence -- no changed
        files, diff, specification or plan -- because the result is stored
        once per ``dependency_fingerprint`` and reused by every later run of
        this repository. ``generated_at`` is stamped exactly here, on the
        guidance the controller accepts, and never restamped afterwards.
        """

        rejection: str | None = None
        initial_rejection: str | None = None
        for _attempt in range(1, MAX_REPOSITORY_SKILL_GENERATION_ATTEMPTS + 1):
            try:
                request = self._build_request(
                    AgentRole.RESEARCHER,
                    context.work_item,
                    purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
                    repair_context=(
                        repository_skill_correction_context(rejection)
                        if rejection is not None
                        else None
                    ),
                    repository_profile=repository_profile,
                    official_documentation_origins=list(
                        self._config.polish.official_documentation_origins
                    ),
                    practice_reference_urls=list(self._config.polish.practice_reference_urls),
                    workspace_path=str(self._store.run_dir(run.id)),
                    attempt_number=_attempt,
                )
                result = self._invoke_agent(run, request, reraise_runtime_errors=True)
            except (OSError, RuntimeError, ValueError) as exc:
                rejection = f"repository skill research could not run: {exc}"
                if _attempt == 1:
                    initial_rejection = rejection
                    continue
                assert initial_rejection is not None
                return None, repository_skill_exhausted_warning(initial_rejection, rejection)

            skill = result.repository_skill
            if not result.success:
                rejection = (
                    result.failure_reason
                    or "researcher failed to produce version-specific repository guidance"
                )
                if _attempt == 1:
                    initial_rejection = rejection
                    continue
                if initial_rejection is not None:
                    return (
                        None,
                        repository_skill_exhausted_warning(initial_rejection, rejection),
                    )
                return None, rejection
            if skill is None:
                rejection = "researcher reported success without repository guidance"
                if initial_rejection is not None:
                    return (
                        None,
                        repository_skill_exhausted_warning(initial_rejection, rejection),
                    )
                return None, rejection
            rejection = self._repository_skill_validation_error(skill, repository_profile)
            if rejection is None:
                return skill.model_copy(update={"generated_at": utc_now()}), None
            if _attempt == 1:
                initial_rejection = rejection
                continue

        assert rejection is not None
        assert initial_rejection is not None
        return (
            None,
            repository_skill_exhausted_warning(initial_rejection, rejection),
        )

    # -- repository-scoped skill reuse and overlay ---------------------------

    def _select_repository_skill(
        self,
        run: FactoryRun,
        context: _RunContext,
        repository_profile: RepositoryProfile,
    ) -> tuple[FactoryRun, RepositorySkillSelection | None, tuple[str, ...]]:
        """Reuse, or generate exactly once, this repository's guidance.

        Reuse is attempted first and is a pure read, so a run whose
        fingerprint already has stored guidance never enters ``RESEARCHING``
        and never spends a research call. Stored guidance is revalidated in
        full against the current profile and the configured allowlists before
        it is used; guidance that does not revalidate is left on disk exactly
        as written and polish is skipped with an actionable warning.

        Returns the (possibly transitioned) run, the selection to use (or
        ``None`` when polish must be skipped), and warnings to record on the
        persisted profile.
        """

        fingerprint = repository_profile.dependency_fingerprint
        hint = _skill_refresh_hint(context.source_repo)
        try:
            manager = RepositorySkillManager.for_repository(
                self._config.data_dir, context.source_repo
            )
        except (RepositorySkillError, OSError) as exc:
            return run, None, (f"repository skill storage is unavailable: {exc}. {hint}",)

        try:
            selection = manager.reuse(fingerprint)
        except (RepositorySkillError, OSError) as exc:
            return (
                run,
                None,
                (
                    "stored repository guidance could not be read and was left unchanged: "
                    f"{exc}. {hint}",
                ),
            )

        if selection is not None:
            if error := self._stored_guidance_error(selection, repository_profile, hint):
                return run, None, (error,)
            return run, selection, _overlay_warnings(manager, selection)

        # Nothing is stored for this dependency state yet: this is the only
        # path that may spend a research call.
        run = self.transition(run, WorkflowState.RESEARCHING)
        skill, warning = self._run_repository_skill_researcher(run, context, repository_profile)
        if skill is None:
            assert warning is not None
            return run, None, (warning,)

        try:
            selection = manager.select(skill)
        except (RepositorySkillError, OSError) as exc:
            # Publication is no-clobber, so this covers both "could not be
            # written" and "another run's file is there but unreadable".
            # Either way nothing on disk was changed.
            return (
                run,
                None,
                (
                    "newly generated repository guidance could not be published, and the "
                    f"stored guidance for this dependency state was left unchanged: {exc}. "
                    f"{hint}",
                ),
            )
        if selection.use.source is SkillSelectionSource.REUSED:
            # Another run published guidance for this fingerprint first. That
            # winner is what was kept, so it must satisfy the same rules.
            if error := self._stored_guidance_error(selection, repository_profile, hint):
                return run, None, (error,)
        return run, selection, _overlay_warnings(manager, selection)

    def _stored_guidance_error(
        self,
        selection: RepositorySkillSelection,
        repository_profile: RepositoryProfile,
        hint: str,
    ) -> str | None:
        error = self._repository_skill_validation_error(
            selection.generated_skill, repository_profile
        )
        if error is None:
            return None
        return (
            f"stored repository guidance at {selection.generated_path} did not revalidate "
            f"and was left unchanged: {error}. {hint}"
        )

    def _snapshot_repository_skill(
        self, run: FactoryRun, selection: RepositorySkillSelection
    ) -> str | None:
        """Record create-once what this run is about to give its agents.

        Written before any agent sees the guidance, so the run's audit trail
        describes what it actually used. Later human edits to the shared
        overlay therefore affect later runs only.

        ``repository-skill.json`` is written **last**, because it is the
        run's claim that this exact guidance was consumed. Writing it after
        the provenance record and the overlay means a partially written
        snapshot can never assert a consumption that the audit trail cannot
        explain -- and polish is skipped on any failure, so the claim is
        never made at all.
        """

        artifacts: tuple[VersionedModel, ...] = tuple(
            artifact
            for artifact in (selection.use, selection.overlay, selection.effective_skill)
            if artifact is not None
        )
        for artifact in artifacts:
            try:
                self._store.save_artifact_once(run.id, artifact)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "repository guidance snapshot failed",
                    extra={
                        "run_id": run.id,
                        "artifact": type(artifact).__name__,
                        "error": str(exc),
                    },
                )
                return f"repository guidance snapshot could not be persisted: {exc}"
        return None

    def _save_advisory_artifact(self, run: FactoryRun, artifact: VersionedModel) -> str | None:
        """Persist an advisory post-green artifact.

        The polish pass is optional, so a boundary failure here degrades to a
        recorded skip instead of failing an already-green run.
        """

        try:
            self._store.save_artifact(run.id, artifact)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "advisory artifact persistence failed",
                extra={"run_id": run.id, "artifact": type(artifact).__name__, "error": str(exc)},
            )
            return str(exc)
        return None

    def _repository_skill_validation_error(
        self,
        skill: RepositorySkill,
        repository_profile: RepositoryProfile,
    ) -> str | None:
        """Apply the shared provenance rules with this factory's allowlists.

        The same check runs on freshly generated guidance and on every later
        load, so stored guidance can never outlive the configuration that
        made it acceptable.
        """
        return repository_skill_validation_error(
            skill,
            repository_profile,
            official_documentation_origins=self._config.polish.official_documentation_origins,
            practice_reference_urls=self._config.polish.practice_reference_urls,
        )

    def _run_planner(
        self,
        run: FactoryRun,
        work_item: WorkItem,
        specification: Specification,
        research_report: ResearchReport | None,
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
            if result.success and result.execution_plan is not None:
                self._store.save_artifact(run.id, result.execution_plan)
                return result.execution_plan
            if not is_retryable_typed_artifact_failure(result, ExecutionPlan):
                break
            assert result.failure_reason is not None
            current_repair_context = _typed_artifact_schema_repair_context(
                result.failure_reason,
                ExecutionPlan.__name__,
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
            repository_skill=context.repository_skill,
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
                self._store.save_artifact(run.id, result.test_report, attempt=snapshot)
                return result.test_report
            if not is_retryable_typed_artifact_failure(result, TestReport):
                break
            assert result.failure_reason is not None
            repair_context = _typed_artifact_schema_repair_context(
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
        test_report: TestReport,
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
            repository_skill=context.repository_skill,
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
            repair_context = _typed_artifact_schema_repair_context(
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
        for finding in [*review.blocking_findings, *review.repair_regressions]:
            for location in finding.locations:
                line_count = workspace.file_line_count(evidence.tree_sha, location.path)
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
        research_report: ResearchReport | None = None,
        execution_plan: ExecutionPlan | None = None,
        change_set: ChangeSet | None = None,
        diff: str | None = None,
        changed_files: list[str] | None = None,
        verification_report: VerificationReport | None = None,
        test_report: TestReport | None = None,
        prior_review_findings: list[ReviewFinding] | None = None,
        accepted_review_findings: list[ReviewFinding] | None = None,
        repair_diff: str | None = None,
        repair_context: RepairContext | str | None = None,
        repository_profile: RepositoryProfile | None = None,
        repository_skill: RepositorySkill | None = None,
        official_documentation_origins: list[str] | None = None,
        practice_reference_urls: list[str] | None = None,
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
            research_report=research_report,
            execution_plan=execution_plan,
            change_set=change_set,
            diff=diff,
            changed_files=changed_files or [],
            verification_report=verification_report,
            test_report=test_report,
            prior_review_findings=prior_review_findings or [],
            accepted_review_findings=accepted_review_findings or [],
            repair_diff=repair_diff,
            repair_context=repair_context,
            repository_profile=repository_profile,
            repository_skill=repository_skill,
            official_documentation_origins=official_documentation_origins or [],
            practice_reference_urls=practice_reference_urls or [],
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
        verification_generated_paths: set[str] = set()
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
            if run.review_acceptance is not None:
                run = run.model_copy(update={"review_acceptance": None, "updated_at": utc_now()})
                self._store.save_run(run)
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

            scope = self._scope_policy.assess(
                context.execution_plan,
                evidence.changed_files,
                context.triage_result.risk,
            )
            while scope.decision is ScopeDecision.REPLAN:
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
                    raise self._halt(
                        run,
                        WorkflowState.NEEDS_HUMAN,
                        "scope metadata replan made no progress: " + _describe_scope(scope),
                    )
            if scope.decision is ScopeDecision.NEEDS_HUMAN:
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "scope drift requires human review: " + _describe_scope(scope),
                )

            if self._should_polish(run, budget, context):
                context.polish_attempted = True
                run = self._prepare_polish(run, context)
                if context.repository_skill is not None:
                    repair_context = self._polish_context()
                    run = self.transition(run, WorkflowState.IMPLEMENTING)
                    continue

            if context.repository_skill is not None:
                try:
                    current_profile = self._repository_profiler(context.workspace.path)
                except (OSError, RuntimeError, ValueError) as exc:
                    context.repository_skill = None
                    self._publish_profile(
                        run,
                        context,
                        context.repository_profile,
                        f"repository skill disabled because profile validation failed: {exc}",
                    )
                else:
                    staleness: tuple[str, ...] = ()
                    if (
                        current_profile.dependency_fingerprint
                        != context.repository_skill.dependency_fingerprint
                    ):
                        context.repository_skill = None
                        staleness = (
                            "repository skill disabled because dependency versions changed "
                            "after the guidance was selected",
                        )
                    self._publish_profile(run, context, current_profile, *staleness)

            run = self.transition(run, WorkflowState.REVIEWING)
            test_report = self._run_tester(run, context, evidence, verification.report, snapshot)
            repair_diff = self._repair_review_diff(run, context.workspace, evidence)
            review_report = self._run_reviewer(
                run,
                context,
                evidence,
                verification.report,
                test_report,
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
            if impasse is not None and impasse.kind in {
                ReviewImpasseKind.REPEATED_PATH,
                ReviewImpasseKind.REPEATED_FINDING,
            }:
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

    def _prepare_polish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """Resolve the guidance for one optional polish attempt.

        Sets ``context.repository_skill`` when polish may proceed and leaves
        it ``None`` when polish must be skipped. Every skip is recorded as an
        actionable warning on the persisted profile and never fails the
        already-green run.
        """
        try:
            refreshed_profile = self._repository_profiler(context.workspace.path)
        except (OSError, RuntimeError, ValueError) as exc:
            self._publish_profile(
                run,
                context,
                context.repository_profile,
                f"polish skipped because repository profiling failed: {exc}",
            )
            return run

        context.repository_profile = refreshed_profile
        if persistence_error := self._save_advisory_artifact(run, refreshed_profile):
            self._publish_profile(
                run,
                context,
                refreshed_profile,
                "polish skipped because the refreshed repository profile could not be "
                f"persisted: {persistence_error}",
            )
            return run

        run, selection, warnings = self._select_repository_skill(run, context, refreshed_profile)
        if selection is not None:
            # The snapshot is the run's record of what its agents were given,
            # so it is taken before any agent receives the guidance.
            if snapshot_error := self._snapshot_repository_skill(run, selection):
                warnings = (*warnings, f"polish skipped: {snapshot_error}")
                selection = None
        if warnings:
            self._publish_profile(run, context, refreshed_profile, *warnings)
        if selection is not None:
            # Held in memory for the rest of the run: a human editing the
            # shared overlay mid-run affects later runs only.
            context.repository_skill = selection.effective_skill
        return run

    def _publish_profile(
        self,
        run: FactoryRun,
        context: _RunContext,
        profile: RepositoryProfile,
        *warnings: str,
    ) -> None:
        """Persist ``profile`` carrying every advisory warning this run raised.

        The profile is re-derived from the workspace several times after the
        first green verification, so warnings are accumulated on the context
        rather than on any one profile object; otherwise a later re-profile
        would silently drop an earlier explanation.
        """
        context.profile_warnings = tuple(dict.fromkeys((*context.profile_warnings, *warnings)))
        context.repository_profile = _profile_with_warnings(profile, context.profile_warnings)
        self._save_advisory_artifact(run, context.repository_profile)

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
        context.execution_plan = self._run_planner(
            run,
            context.work_item,
            context.specification,
            context.research_report,
            workspace_path=str(context.workspace.path),
            repair_context=repair_context,
            diff=evidence.diff,
            changed_files=list(evidence.changed_files),
        )
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
            context_tier=role_model.context_tier,
            work_item=context.work_item,
            specification=context.specification,
            research_report=context.research_report,
            execution_plan=context.execution_plan,
            repair_context=repair_context,
            diff=current_diff if repair_context is not None else None,
            changed_files=(
                list(context.latest_evidence.changed_files)
                if repair_context is not None and context.latest_evidence is not None
                else []
            ),
            repository_skill=context.repository_skill,
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
        if budget is not AttemptBudget.IMPLEMENTATION or not self._config.polish.enabled:
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
                "simplification pass using the reusable repository guidance supplied "
                "with this request. Simplify first, then apply version-specific polish. "
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
                    "path_streaks": {path: 1 for path in _review_finding_paths(open_findings)},
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
        findings = run.review_ledger.open_findings[:MAX_REVIEW_IMPASSE_FINDINGS]
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
        if (
            not tree_sha
            or not verification.passed
            or context.triage_result.risk not in self._config.review.accepted_risks
            or not findings
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
        acceptance = run.review_acceptance
        if acceptance is None:
            return review.approved
        return (
            run.reviewed_tree_sha is not None
            and acceptance.reviewed_tree_sha == run.reviewed_tree_sha
            and acceptance.risk is risk
            and acceptance.risk in self._config.review.accepted_risks
            and 0 < len(acceptance.findings) <= self._config.review.max_accepted_findings
            and acceptance.findings == run.review_ledger.accepted_findings
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
            return self.transition(run, WorkflowState.DONE)
        return self._ci_loop(run, context)

    def _publish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
        """PR boundary: re-run the deterministic gates, then commit/push/open."""
        evidence = context.latest_evidence
        assert evidence is not None
        if context.latest_review is None or not self._review_authorizes_delivery(
            run,
            context.latest_review,
            context.triage_result.risk,
        ):
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
        if scope.decision is not ScopeDecision.CONTINUE:
            raise self._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "scope drift requires human review before publishing: " + _describe_scope(scope),
            )

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
                title=context.work_item.title,
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
            label = (
                "Controller-accepted reviewed Git tree"
                if run.review_acceptance is not None
                else "Reviewer-approved Git tree"
            )
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
                raise self._halt(
                    run,
                    WorkflowState.NEEDS_HUMAN,
                    "CI checks were still pending after the configured wait budget",
                )
            if report.overall == "PASS":
                return self._merge_and_finish(run, context)

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

    def _merge_and_finish(self, run: FactoryRun, context: _RunContext) -> FactoryRun:
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
        self.profile_warnings: tuple[str, ...] = ()
        self.repository_skill: RepositorySkill | None = None
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


def _profile_with_warnings(
    profile: RepositoryProfile, warnings: tuple[str, ...]
) -> RepositoryProfile:
    """Append ``warnings`` the profile does not already carry, in order."""
    existing = set(profile.warnings)
    added = tuple(warning for warning in warnings if warning not in existing)
    if not added:
        return profile
    return profile.model_copy(update={"warnings": (*profile.warnings, *added)})


def _skill_refresh_hint(source_repo: Path) -> str:
    """Name the one command that may deliberately replace stored guidance.

    A normal run never overwrites a shared generated file, so a warning about
    unusable stored guidance is only actionable when it says how to replace
    it.
    """
    return f"Replace it deliberately with: factory skill refresh --repo {source_repo}"


def _overlay_warnings(
    manager: RepositorySkillManager, selection: RepositorySkillSelection
) -> tuple[str, ...]:
    """Report an overlay the run could not honour, naming the exact file.

    A human's overlay never blocks or fails a run: the file is left exactly
    as written, generated guidance still applies, and the reason is recorded
    where an operator will see it.
    """
    if selection.overlay_error is None:
        return ()
    return (
        f"repository skill overlay at {manager.overlay_path} was not applied and was left "
        f"unchanged; the run used generated repository guidance only: "
        f"{selection.overlay_error}",
    )


def _commit_message(context: _RunContext, run_id: str) -> str:
    return (
        f"{context.work_item.title}\n\n"
        f"{context.specification.problem.strip()}\n\n"
        f"Factory run: {run_id}\n"
        f"Work item: {context.work_item.id}"
    )
