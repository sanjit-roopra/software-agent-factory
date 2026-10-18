"""Accept the open findings of a review impasse from the dashboard (ADR-059)."""

from collections.abc import Callable
from pathlib import Path

import pytest
from factory_testing import build_config, work_item
from test_workflow_delivery import LocalPublisher, Merger, _config, _controller

from software_agent_factory import run_reopen
from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.models import (
    AgentRole,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    ResumeClassification,
    ReviewAcceptanceReason,
    ReviewFindingCategory,
    ReviewFindingDraft,
    ReviewReport,
    ReviewSourceLocation,
    WorkflowState,
    utc_now,
)
from software_agent_factory.publishing import PublishResult
from software_agent_factory.resume_classes import is_valid_findings_acceptance_context
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

ACCEPT = ResumeClassification.ACCEPT_FINDINGS


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


def _security_reviewer(request: AgentRequest) -> AgentResult:
    """A reviewer that blocks on a security finding the review policy never accepts."""
    finding = ReviewFindingDraft(
        category=ReviewFindingCategory.SECURITY,
        message="Security defect.",
        locations=[ReviewSourceLocation(path="FACTORY_NOTES.md", start_line=1, end_line=1)],
    )
    return AgentResult(
        role=AgentRole.REVIEWER,
        success=True,
        review_report=ReviewReport(approved=False, blocking_findings=[finding]),
    )


class BodyPublisher(LocalPublisher):
    """A local publisher that keeps the pull request body it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[str] = []

    def publish(self, **kwargs: object) -> PublishResult:
        self.bodies.append(str(kwargs["body"]))
        return super().publish(**kwargs)


def _halt(controller: WorkflowController, source_repo: Path) -> FactoryRun:
    controller._config.review.max_rounds = 1
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN, run.failure_reason
    return run


def _accept(
    run: FactoryRun, store: FileRunStore, config: FactoryConfig, fingerprint: str | None = None
) -> None:
    assert run.escalation is not None
    context = run.escalation.findings_acceptance_context
    assert context is not None
    request = DashboardResumeRequest(
        run_id=run.id,
        episode_id=run.escalation.episode_id,
        context_fingerprint=fingerprint or context.context_fingerprint,
        action=ACCEPT,
    )
    assert store.create_dashboard_request(run.id, request)
    ingest_dashboard_request(run, store, config, utc_now())


def _local(tmp_path: Path) -> tuple[WorkflowController, FileRunStore, FactoryConfig]:
    config = build_config(tmp_path / "data")
    store = FileRunStore(config.data_dir)
    runtime = FakeAgentRuntime(reviewer=_security_reviewer)
    return WorkflowController(config, store, runtime), store, config


def test_a_review_impasse_offers_its_open_findings_for_acceptance(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, _, _ = _local(tmp_path)

    halted = _halt(controller, source_repo)

    assert halted.escalation is not None
    assert halted.escalation.resume_classification is ACCEPT
    assert halted.escalation.remote_resume_enabled is False
    context = halted.escalation.findings_acceptance_context
    assert context is not None
    assert context.finding_ids == [finding.id for finding in halted.review_ledger.open_findings]
    assert context.reviewed_tree_sha == halted.review_ledger.last_reviewed_tree_sha
    assert is_valid_findings_acceptance_context(context, halted.id, halted.escalation.episode_id)


def test_accepting_moves_the_findings_and_reaches_pr_ready_needing_a_look(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, store, config = _local(tmp_path)
    halted = _halt(controller, source_repo)
    open_ids = [finding.id for finding in halted.review_ledger.open_findings]
    _accept(halted, store, config)

    accepted = controller.reopen(halted.id, source_repo)

    assert accepted.state is WorkflowState.PR_READY, accepted.failure_reason
    assert accepted.review_ledger.open_findings == []
    assert [finding.id for finding in accepted.review_ledger.accepted_findings] == open_ids
    assert accepted.review_acceptance is not None
    assert accepted.review_acceptance.reason is ReviewAcceptanceReason.OPERATOR
    assert accepted.reviewed_tree_sha == halted.review_ledger.last_reviewed_tree_sha
    reason = "a person accepted open review findings: " + ", ".join(open_ids)
    assert reason in accepted.needs_look
    assert accepted.escalation is not None
    assert accepted.escalation.status is EscalationStatus.RESUMED
    assert accepted.escalation.reopen_count == 1


def test_an_accepted_run_opens_a_labelled_pull_request_and_is_not_merged(
    tmp_path: Path, source_repo: Path
) -> None:
    publisher = BodyPublisher()
    merger = Merger()
    config = _config(tmp_path)
    controller, store = _controller(
        config,
        runtime=FakeAgentRuntime(reviewer=_security_reviewer),
        publisher=publisher,
        merger=merger,
    )
    halted = _halt(controller, source_repo)
    _accept(halted, store, config)

    done = controller.reopen(halted.id, source_repo)

    assert done.state is WorkflowState.DONE, done.failure_reason
    assert publisher.flags == [done.needs_look]
    assert merger.calls == []
    (body,) = publisher.bodies
    assert "A person accepted these findings." in body
    assert done.review_ledger.accepted_findings[0].id in body


def test_an_acceptance_for_an_old_context_goes_stale(tmp_path: Path, source_repo: Path) -> None:
    controller, store, config = _local(tmp_path)
    halted = _halt(controller, source_repo)

    _accept(halted, store, config, fingerprint="f" * 64)

    run = store.load_run(halted.id)
    assert run.escalation is not None
    assert run.escalation.status is not EscalationStatus.REOPENED
    (request,) = store.list_dashboard_requests(run.id, run.escalation.episode_id)
    assert (request.status, request.reason) == ("stale", "context_changed")


def _with_other_tree(run: FactoryRun) -> FactoryRun:
    ledger = run.review_ledger.model_copy(update={"last_reviewed_tree_sha": "b" * 40})
    return run.model_copy(update={"review_ledger": ledger})


def _without_context(run: FactoryRun) -> FactoryRun:
    assert run.escalation is not None
    escalation = run.escalation.model_copy(update={"findings_acceptance_context": None})
    return run.model_copy(update={"escalation": escalation})


def _with_other_receipt_fingerprint(run: FactoryRun) -> FactoryRun:
    assert run.escalation is not None
    receipts = [
        receipt.model_copy(update={"findings_acceptance_context_fingerprint": "d" * 64})
        for receipt in run.escalation.accepted_replies
    ]
    escalation = run.escalation.model_copy(update={"accepted_replies": receipts})
    return run.model_copy(update={"escalation": escalation})


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_without_context, "missing or invalid findings acceptance context"),
        (_with_other_receipt_fingerprint, "receipt fingerprint does not match"),
        (_with_other_tree, "no longer matches its findings acceptance context"),
    ],
    ids=["no-context", "other-receipt", "other-tree"],
)
def test_an_acceptance_that_no_longer_matches_cannot_reopen_the_run(
    tmp_path: Path,
    source_repo: Path,
    tamper: Callable[[FactoryRun], FactoryRun],
    message: str,
) -> None:
    controller, store, config = _local(tmp_path)
    halted = _halt(controller, source_repo)
    _accept(halted, store, config)
    reopening = tamper(store.load_run(halted.id))

    with pytest.raises(ValueError, match=message):
        run_reopen.transition_reopened(controller, reopening)


def test_a_changed_worktree_refuses_the_acceptance(tmp_path: Path, source_repo: Path) -> None:
    controller, store, config = _local(tmp_path)
    halted = _halt(controller, source_repo)
    _accept(halted, store, config)
    assert halted.workspace_path is not None
    (Path(halted.workspace_path) / "FACTORY_NOTES.md").write_text("changed\n", encoding="utf-8")

    refused = controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert refused.review_acceptance is None
    assert refused.escalation is not None
    assert refused.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE


def test_a_changed_delivery_policy_refuses_the_acceptance(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, store, config = _local(tmp_path)
    halted = _halt(controller, source_repo)
    _accept(halted, store, config)
    store.save_run(
        store.load_run(halted.id).model_copy(update={"delivery_policy_fingerprint": "e" * 64})
    )

    refused = controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert refused.review_acceptance is None
    assert "delivery policy changed" in (refused.failure_reason or "")
