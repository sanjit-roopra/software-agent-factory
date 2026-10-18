"""Pull request delivery for :class:`~software_agent_factory.workflow.WorkflowController`:
publish the reviewed tree, watch CI, repair it within its budget, merge or leave the pull
request open.

These functions are part of the controller. Each takes the controller as its first
argument and changes the run only through it, so the controller stays the one place that
changes a run's state.
"""

from __future__ import annotations

import functools
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from . import review_ledger
from .git_safety import GitPublishError
from .github import SHA_PATTERN, GitHubError, build_pr_body
from .governance import ScopeAssessment, ScopeDecision, assess_publish_gate
from .models import (
    PUBLISH_FAILED_HALT_REASON,
    UNRESOLVED_DECISIONS_HALT_REASON,
    AttemptBudget,
    CIReport,
    ExecutionRoute,
    FactoryRun,
    WorkflowState,
    utc_now,
)
from .publishing import CIObserver, PullRequestPublisher
from .repair_context import ci_repair_context
from .run_context import RunContext, describe_scope
from .run_watch import watched
from .suppression_scan import suppression_items
from .workspace import WorkspaceError, WorkspaceEvidence

if TYPE_CHECKING:
    from .workflow import WorkflowController

logger = logging.getLogger(__name__)

#: CI failure categories that may legitimately be repaired by another code
#: change. Everything else (flaky/infra/dependency/unknown/cancelled) is an
#: operator problem, not a code problem, and escalates with evidence.
REPAIRABLE_CI_CATEGORIES: frozenset[str] = frozenset({"CODE_FAILURE", "TEST_FAILURE"})


def resolve_publisher(controller: WorkflowController) -> PullRequestPublisher:
    if controller._publisher is None:
        controller._publisher = PullRequestPublisher(controller._config)
    return controller._publisher


def resolve_ci_observer(controller: WorkflowController) -> CIObserver:
    if controller._ci_observer is None:
        controller._ci_observer = CIObserver(controller._config)
    return controller._ci_observer


def publish_and_observe(
    controller: WorkflowController, run: FactoryRun, context: RunContext
) -> FactoryRun:
    run = publish(controller, run, context)
    if not controller._config.ci.enabled:
        if run.needs_look:
            return leave_open(controller, run, context)
        return controller.transition(run, WorkflowState.DONE)
    return ci_loop(controller, run, context)


def skipped_gates(
    controller: WorkflowController, run: FactoryRun, context: RunContext, scope: ScopeAssessment
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
    if run.risk_assessment_enabled and controller._config.risk[triage.risk].human_approval:
        reasons.append(f"risk {triage.risk} requires human approval")
    if context.execution_plan.unresolved_decisions:
        reasons.append(UNRESOLVED_DECISIONS_HALT_REASON)
    if scope.decision is not ScopeDecision.CONTINUE:
        reasons.append("scope drift: " + describe_scope(scope))
    review = context.latest_review
    acceptance = run.review_acceptance
    if (
        review is not None
        and acceptance is not None
        and (
            not controller._acceptance_within_policy(acceptance, review, triage.risk)
            or not review_ledger.has_acceptable_origins(acceptance.findings)
        )
    ):
        reasons.append("review findings were accepted beyond the review policy")
    return reasons


def add_needs_look(controller: WorkflowController, run: FactoryRun, reason: str) -> FactoryRun:
    if reason in run.needs_look:
        return run
    run = run.model_copy(update={"needs_look": [*run.needs_look, reason], "updated_at": utc_now()})
    controller._store.save_run(run)
    return run


def publish_as_is(
    controller: WorkflowController, run: FactoryRun, context: RunContext, reason: str
) -> FactoryRun:
    """Unattended attempt budget used up (ADR-040).

    Before a pull request exists, publish the current work as it is. After
    one exists, leave it open as it is: the failed repair is not pushed.
    """
    if run.pull_request_url is not None:
        return leave_open(controller, run, context, reason)
    run = adopt_current_tree(controller, run, context, context.workspace.collect_evidence(), reason)
    context.latest_test_report = None
    context.latest_review = None
    return controller.transition(run, WorkflowState.PR_READY)


def adopt_current_tree(
    controller: WorkflowController,
    run: FactoryRun,
    context: RunContext,
    evidence: WorkspaceEvidence,
    reason: str,
) -> FactoryRun:
    """Make the current worktree the tree an unattended run publishes."""
    if not evidence.changed_files or not evidence.tree_sha:
        raise controller._stop(run, context, f"{reason}; nothing to publish")
    run = add_needs_look(controller, run, reason)
    run = run.model_copy(update={"reviewed_tree_sha": evidence.tree_sha})
    controller._store.save_run(run)
    controller._store.save_patch(run.id, evidence.diff)
    context.latest_evidence = evidence
    return run


def leave_open(
    controller: WorkflowController, run: FactoryRun, context: RunContext, reason: str | None = None
) -> FactoryRun:
    """End an unattended run with its pull request open, labelled and not
    merged (ADR-040)."""
    if reason is not None:
        run = add_needs_look(controller, run, reason)
    assert run.pull_request_url is not None
    try:
        resolve_publisher(
            controller,
        ).flag_needs_look(
            workspace_path=context.workspace.path,
            pull_request_url=run.pull_request_url,
            reasons=run.needs_look,
            repository=run.delivery_repository,
        )
    except (GitHubError, OSError, ValueError) as exc:
        # The label tells people to look. Skipping the merge is what keeps
        # the pull request safe, so a labelling error does not stop the run.
        logger.warning("could not label pull request %s: %s", run.pull_request_url, exc)
    return controller.transition(run, WorkflowState.DONE)


def publish(controller: WorkflowController, run: FactoryRun, context: RunContext) -> FactoryRun:
    """PR boundary: re-run the deterministic gates, then commit/push/open."""
    run, evidence = _reviewed_evidence(controller, run, context)
    changed_files = list(evidence.changed_files)
    run = _check_publish_scope(controller, run, context, changed_files)

    publisher = resolve_publisher(controller)
    branch_name = run.branch_name
    assert branch_name is not None
    parent_sha = run.commit_sha or run.base_commit_sha
    if not parent_sha:
        raise controller._stop(
            run,
            context,
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
        controller._store.save_run(run)

    try:
        if controller._config.merge.enabled:
            assert controller._merger is not None
            if (
                controller._merger.validate_repository(context.workspace.path)
                != run.delivery_repository
            ):
                raise GitHubError("delivery repository changed before publication")
        base_branch = publisher.resolve_base_branch(context.source_repo)
        if run.delivery_base_branch is not None and run.delivery_base_branch != base_branch:
            raise GitHubError("refusing to publish to a different delivery base branch")
        run = run.model_copy(update={"delivery_base_branch": base_branch})
        controller._store.save_run(run)
        result = publisher.publish(
            workspace_path=context.workspace.path,
            branch_name=branch_name,
            base_branch=base_branch,
            commit_message=commit_message(context, run.id),
            title=context.execution_plan.summary.strip().rstrip(".").strip(),
            body=_build_pr_body(controller, run, context, changed_files),
            existing_pull_request_url=run.pull_request_url,
            expected_tree_sha=run.reviewed_tree_sha,
            expected_repository=run.delivery_repository,
            expected_host=run.delivery_host,
            expected_parent_sha=parent_sha,
            prepared_commit_sha=run.pending_commit_sha,
            record_commit=record_commit,
        )
        if result.commit_sha != run.pending_commit_sha:
            raise GitHubError("published commit does not match the persisted publication receipt")
    except (GitPublishError, GitHubError, OSError) as exc:
        raise controller._stop(run, context, f"{PUBLISH_FAILED_HALT_REASON} {exc}") from exc

    run = run.model_copy(
        update={
            "commit_sha": result.commit_sha,
            "reviewed_commit_sha": result.commit_sha,
            "pending_commit_sha": None,
            "pull_request_url": result.pull_request_url,
            "updated_at": utc_now(),
        }
    )
    controller._store.save_run(run)
    return controller.transition(run, WorkflowState.PR_CREATED)


def _reviewed_evidence(
    controller: WorkflowController, run: FactoryRun, context: RunContext
) -> tuple[FactoryRun, WorkspaceEvidence]:
    """Require a review that authorizes delivery and return the evidence to publish."""
    evidence = context.latest_evidence
    assert evidence is not None
    if context.latest_review is None:
        # Only an unattended run whose attempt budget ran out publishes
        # work without a review (ADR-040).
        authorized = run.unattended and bool(run.needs_look)
    else:
        authorized = controller._review_authorizes_delivery(
            run, context.latest_review, context.triage_result.risk
        )
    if not authorized:
        raise controller._stop(
            run,
            context,
            "independent review or a matching bounded controller acceptance is required "
            "for every PR",
        )
    current_evidence = context.workspace.collect_evidence()
    if (
        current_evidence.diff != evidence.diff
        or not run.reviewed_tree_sha
        or current_evidence.tree_sha != run.reviewed_tree_sha
    ):
        if not run.unattended:
            raise controller._halt(
                run,
                WorkflowState.NEEDS_HUMAN,
                "repository changed after independent review; refusing to publish unreviewed work",
            )
        run = adopt_current_tree(
            controller, run, context, current_evidence, "repository changed after review"
        )
        evidence = current_evidence
    return run, evidence


def _check_publish_scope(
    controller: WorkflowController, run: FactoryRun, context: RunContext, changed_files: list[str]
) -> FactoryRun:
    """Re-run the publish gate and the scope policy on the files a pull request changes."""
    gate = assess_publish_gate(
        changed_files,
        max_changed_files=controller._config.repository.max_changed_files,
        protected_file_patterns=controller._config.repository.protected_file_patterns,
    )
    if not gate.allowed:
        raise controller._stop(
            run,
            context,
            "refusing to publish: " + "; ".join(gate.violations),
        )

    scope = controller._scope_policy.assess(
        context.execution_plan, changed_files, context.triage_result.risk
    )
    if scope.decision is not ScopeDecision.CONTINUE and not run.unattended:
        raise controller._halt(
            run,
            WorkflowState.NEEDS_HUMAN,
            "scope drift requires human review before publishing: " + describe_scope(scope),
        )
    if run.unattended:
        for reason in skipped_gates(controller, run, context, scope):
            run = add_needs_look(controller, run, reason)
    return run


def _build_pr_body(
    controller: WorkflowController, run: FactoryRun, context: RunContext, changed_files: list[str]
) -> str:
    body = build_pr_body(
        work_item=context.work_item,
        specification=context.specification,
        plan=context.execution_plan,
        changed_files=changed_files,
        suppressions=suppression_items(context.latest_evidence),
        verification=context.latest_verification,
        test_report=context.latest_test_report,
        review=context.latest_review,
        review_acceptance=run.review_acceptance,
        rejected_commands=controller._rejected_commands(run.id),
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


def ci_loop(controller: WorkflowController, run: FactoryRun, context: RunContext) -> FactoryRun:
    """Poll CI, and repair (bounded by ``ci.repair_attempts``) when the
    failure is genuinely a code/test failure."""
    observer = resolve_ci_observer(
        controller,
    )
    # One poll of slack past the wait budget before the heartbeat stops.
    ci_wait = controller._config.ci.max_wait_seconds + controller._config.ci.poll_interval_seconds
    while True:
        if run.state is not WorkflowState.CI_RUNNING:
            run = controller.transition(run, WorkflowState.CI_RUNNING)
        assert run.pull_request_url is not None
        try:
            with watched(controller, run, ci_wait):
                report = observer.observe(
                    repo_path=context.workspace.path,
                    pull_request_url=run.pull_request_url,
                    repair_attempts_used=controller._attempts_used(run, AttemptBudget.CI_REPAIR),
                    before_wait=functools.partial(controller._raise_if_cancelled, run),
                )
        except (GitPublishError, GitHubError, OSError) as exc:
            raise controller._stop(run, context, f"could not observe CI: {exc}") from exc
        controller._store.save_artifact(run.id, report)

        if report.timed_out:
            raise controller._stop(
                run, context, "CI checks were still pending after the configured wait budget"
            )
        if report.overall == "PASS":
            return merge_and_finish(controller, run, context)

        run = controller.transition(run, WorkflowState.CI_DIAGNOSIS)
        stop_reason = ci_stop_reason(controller, run, report)
        if stop_reason is not None:
            raise controller._stop(run, context, stop_reason)

        repair_context = ci_repair_context(report)
        run = controller.transition(run, WorkflowState.IMPLEMENTING)
        run = controller._drive_to_pr_ready(run, context, AttemptBudget.CI_REPAIR, repair_context)
        if run.state is WorkflowState.DONE:
            return run
        run = publish(controller, run, context)


def ci_stop_reason(controller: WorkflowController, run: FactoryRun, report: CIReport) -> str | None:
    """Why a red CI result cannot be repaired, or ``None`` to repair it."""
    failed = report.failed_checks
    if report.overall == "CANCELLED" or not failed:
        return f"CI finished with status {report.overall} and no repairable failure"
    if not {check.failure_category or "UNKNOWN" for check in failed} <= (REPAIRABLE_CI_CATEGORIES):
        return "CI failure is not repairable by a code change: " + ", ".join(
            f"{check.name}={check.failure_category or 'UNKNOWN'}" for check in failed
        )
    used = controller._attempts_used(run, AttemptBudget.CI_REPAIR)
    if used >= controller._config.ci.repair_attempts:
        return controller._budget_exhausted_reason(AttemptBudget.CI_REPAIR, used)
    return None


def merge_and_finish(
    controller: WorkflowController, run: FactoryRun, context: RunContext
) -> FactoryRun:
    if run.needs_look:
        return leave_open(controller, run, context)
    if controller._config.merge.enabled:
        assert controller._merger is not None
        if (
            not run.commit_sha
            or not run.pull_request_url
            or not run.delivery_base_branch
            or run.reviewed_commit_sha != run.commit_sha
            or not run.delivery_repository
            or not run.delivery_host
        ):
            raise controller._stop(
                run,
                context,
                "missing delivery evidence or review authorization for the current head",
            )
        try:
            check_delivery_workspace(run, context.workspace.path)
            result = controller._merger.merge(
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
            raise controller._stop(
                run, context, f"could not merge the pull request: {exc}"
            ) from exc
        run = run.model_copy(
            update={"merge_commit_sha": result.commit_sha, "updated_at": utc_now()}
        )
        controller._store.save_run(run)
    return controller.transition(run, WorkflowState.DONE)


def check_delivery_workspace(run: FactoryRun, path: Path) -> None:
    def git_output(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise WorkspaceError(f"could not inspect delivery workspace: {result.stderr.strip()}")
        return result.stdout.strip()

    if git_output("branch", "--show-current") != run.branch_name:
        raise WorkspaceError("delivery workspace is on a different branch")
    if run.state in {WorkflowState.PR_CREATED, WorkflowState.CI_RUNNING}:
        if not run.commit_sha or git_output("rev-parse", "HEAD") != run.commit_sha:
            raise WorkspaceError("published head no longer matches the delivery workspace")
        if git_output("status", "--porcelain"):
            raise WorkspaceError("published delivery workspace has unreviewed changes")


def commit_message(context: RunContext, run_id: str) -> str:
    summary = context.execution_plan.summary.strip().rstrip(".").strip()
    return (
        f"{summary}\n\n"
        f"{context.specification.problem.strip()}\n\n"
        f"Factory run: `{run_id}`\n"
        f"Work item: `{context.work_item.id}`"
    )
