"""Retry CI from the dashboard after CI stopped a run with an open pull request (ADR-061).

A run halts with ``CI_REPAIR_RETRY`` when its CI repair budget ran out, when CI failed in a
way the factory does not repair by itself, or when the checks did not finish in time. A
retry checks that the pull request is still open on the same head, then either watches CI
again (``CI_RUNNING``) or grants one more CI repair attempt and repairs the stored CI result
(``CI_DIAGNOSIS``). These functions are part of the reopen in :mod:`.run_reopen`.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from . import pr_delivery, run_recovery
from .github import GitHubError
from .models import (
    AcceptedReplyReceipt,
    AttemptBudget,
    CIRepairRetryContext,
    CIReport,
    EscalationRecord,
    FactoryRun,
    WorkflowState,
)
from .resume_classes import is_valid_ci_repair_retry_context
from .workspace import GitWorktreeWorkspace

if TYPE_CHECKING:
    from .workflow import WorkflowController


def check_ci_repair_retry(
    _controller: WorkflowController,
    run: FactoryRun,
    escalation: EscalationRecord,
    receipt: AcceptedReplyReceipt,
) -> None:
    """Raise ``ValueError`` when the retry no longer matches the run it was asked for."""
    retry = escalation.ci_repair_retry_context
    if retry is None or not is_valid_ci_repair_retry_context(retry, run.id, escalation.episode_id):
        raise ValueError(f"run {run.id} has missing or invalid CI repair retry context")
    if receipt.ci_repair_retry_context_fingerprint is None or not secrets.compare_digest(
        receipt.ci_repair_retry_context_fingerprint, retry.context_fingerprint
    ):
        raise ValueError(f"run {run.id} receipt fingerprint does not match active CI retry")
    if (run.pull_request_url, run.commit_sha, run.attempts_used(AttemptBudget.CI_REPAIR)) != (
        retry.pull_request_url,
        retry.head_sha,
        retry.repair_attempts_used,
    ):
        raise ValueError(f"run {run.id} no longer matches its CI repair retry context")


def _require_open_pull_request(
    controller: WorkflowController, retry: CIRepairRetryContext, path: Path
) -> None:
    """Raise ``ValueError`` unless the pull request is open on the head CI ran on."""
    github = controller._github
    if github is None:
        raise ValueError("cannot read the pull request to retry CI")
    try:
        pull_request = github.get_pull_request(path, retry.pull_request_url)
    except GitHubError as exc:
        raise ValueError(f"could not read the pull request: {exc}") from exc
    if pull_request.state != "OPEN":
        raise ValueError(f"pull request is {pull_request.state or 'in an unknown state'}")
    if pull_request.head_ref_oid != retry.head_sha.lower():
        raise ValueError("pull request head changed since CI stopped the run")


def reopen_ci_repair_retry(
    controller: WorkflowController,
    run: FactoryRun,
    workspace: GitWorktreeWorkspace,
    source_repo: Path,
) -> FactoryRun:
    """Reopen a run CI stopped: watch CI again, or repair it with one more attempt.

    The delivery workspace and the pull request must still hold the reviewed work on the
    head CI ran on. A red CI result gets one more ``CI_REPAIR`` attempt on top of the
    budget, counted on the run like an attempt grant (ADR-058). If it fails, the run halts
    again for another retry.
    """
    from .run_reopen import transition_reopened  # run_reopen imports this module

    retry = run.escalation.ci_repair_retry_context if run.escalation is not None else None
    if retry is None:
        raise ValueError(f"run {run.id} has no CI repair retry context")
    run_recovery.require_unchanged_delivery_policy(controller, run)
    context = run_recovery.reconcile_delivery_checkpoint(controller, run, workspace, source_repo)
    _require_open_pull_request(controller, retry, workspace.path)
    if not retry.timed_out:
        budget = AttemptBudget.CI_REPAIR
        grants = {**run.attempt_grants, budget: run.attempt_grants.get(budget, 0) + 1}
        run = run.model_copy(update={"attempt_grants": grants})
    run = transition_reopened(controller, run)
    if run.state is WorkflowState.CI_DIAGNOSIS:
        report = controller._store.load_artifact(run.id, CIReport)
        run = pr_delivery.repair_ci(controller, run, context, report)
        if run.state is WorkflowState.DONE:
            return run
    return pr_delivery.ci_loop(controller, run, context)
