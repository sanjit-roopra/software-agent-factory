"""Approve a wider scope from the dashboard after a scope halt (ADR-060)."""

from pathlib import Path

import pytest
from factory_testing import build_config, work_item

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.halt_contexts import build_scope_approval_context
from software_agent_factory.models import (
    AgentRole,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    HaltReasonCode,
    ResumeClassification,
    ReviewFindingCategory,
    ReviewFindingDraft,
    ReviewReport,
    ReviewSourceLocation,
    WorkflowState,
    utc_now,
)
from software_agent_factory.resume_classes import is_valid_scope_approval_context
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

WIDEN = ResumeClassification.SCOPE_APPROVAL
FIRST = "migrations/0001_init.py"
SECOND = "migrations/0002_more.py"


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


class Rig:
    """A local controller whose implementer adds a migration file, a sensitive change."""

    def __init__(self, tmp_path: Path, *, reject_first_review: bool = False) -> None:
        self.config: FactoryConfig = build_config(tmp_path / "data")
        self.store = FileRunStore(self.config.data_dir)
        self.implementer_calls = 0
        self.reviews = 0
        self.reject_first_review = reject_first_review
        default = FakeAgentRuntime()

        def implementer(request: AgentRequest) -> AgentResult:
            self.implementer_calls += 1
            assert request.workspace_path is not None
            workspace = Path(request.workspace_path)
            (workspace / "migrations").mkdir(exist_ok=True)
            names = [FIRST] if self.implementer_calls == 1 else [FIRST, SECOND]
            for name in names:
                (workspace / name).write_text(f"# {name}\n", encoding="utf-8")
            return default.run(request)

        def reviewer(request: AgentRequest) -> AgentResult:
            self.reviews += 1
            if self.reject_first_review and self.reviews == 1:
                finding = ReviewFindingDraft(
                    category=ReviewFindingCategory.CORRECTNESS,
                    message="Add a note line.",
                    locations=[
                        ReviewSourceLocation(path="FACTORY_NOTES.md", start_line=1, end_line=1)
                    ],
                )
                return AgentResult(
                    role=AgentRole.REVIEWER,
                    success=True,
                    review_report=ReviewReport(approved=False, blocking_findings=[finding]),
                )
            return default.run(request)

        self.controller = WorkflowController(
            self.config,
            self.store,
            FakeAgentRuntime(implementer=implementer, reviewer=reviewer),
        )

    def halt(self, source_repo: Path) -> FactoryRun:
        run = self.controller.run(work_item(), source_repo)
        assert run.state is WorkflowState.NEEDS_HUMAN, run.failure_reason
        return run

    def approve(self, run: FactoryRun, fingerprint: str | None = None) -> None:
        assert run.escalation is not None
        context = run.escalation.scope_approval_context
        assert context is not None
        request = DashboardResumeRequest(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            context_fingerprint=fingerprint or context.context_fingerprint,
            action=WIDEN,
        )
        assert self.store.create_dashboard_request(run.id, request)
        ingest_dashboard_request(run, self.store, self.config, utc_now())


def test_a_scope_halt_offers_its_paths_for_approval(tmp_path: Path, source_repo: Path) -> None:
    rig = Rig(tmp_path)

    halted = rig.halt(source_repo)

    assert (halted.failure_reason or "").startswith("scope drift requires human review")
    assert halted.escalation is not None
    assert halted.escalation.resume_classification is WIDEN
    assert halted.escalation.reason_code == HaltReasonCode.SCOPE_REVIEW
    assert halted.escalation.remote_resume_enabled is False
    context = halted.escalation.scope_approval_context
    assert context is not None
    assert context.paths == [FIRST]
    assert is_valid_scope_approval_context(context, halted.id, halted.escalation.episode_id)


def test_an_approved_run_passes_the_scope_check_with_the_same_files(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.approve(halted)

    approved = rig.controller.reopen(halted.id, source_repo)

    assert approved.state is WorkflowState.PR_READY, approved.failure_reason
    assert approved.approved_scope_paths == [FIRST]
    assert rig.implementer_calls == 1
    assert rig.reviews == 1
    assert approved.escalation is not None
    assert approved.escalation.status is EscalationStatus.RESUMED
    assert approved.escalation.reopen_count == 1


def test_an_approved_run_halts_again_on_a_new_file_outside_the_scope(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path, reject_first_review=True)
    halted = rig.halt(source_repo)
    rig.approve(halted)

    again = rig.controller.reopen(halted.id, source_repo)

    assert again.state is WorkflowState.NEEDS_HUMAN, again.failure_reason
    assert rig.implementer_calls == 2
    assert again.escalation is not None
    assert again.escalation.resume_classification is WIDEN
    context = again.escalation.scope_approval_context
    assert context is not None
    assert context.paths == [SECOND]


def test_an_approval_for_an_old_context_goes_stale(tmp_path: Path, source_repo: Path) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)

    rig.approve(halted, fingerprint="f" * 64)

    run = rig.store.load_run(halted.id)
    assert run.escalation is not None
    assert run.escalation.status is not EscalationStatus.REOPENED
    (request,) = rig.store.list_dashboard_requests(run.id, run.escalation.episode_id)
    assert (request.status, request.reason) == ("stale", "context_changed")


def test_a_changed_worktree_refuses_the_approval(tmp_path: Path, source_repo: Path) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.approve(halted)
    assert halted.workspace_path is not None
    (Path(halted.workspace_path) / "FACTORY_NOTES.md").write_text("changed\n", encoding="utf-8")

    refused = rig.controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert refused.approved_scope_paths == []
    assert "workspace changes do not match" in (refused.failure_reason or "")
    assert refused.escalation is not None
    assert refused.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE


def test_a_tampered_receipt_cannot_reopen_the_run(tmp_path: Path, source_repo: Path) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.approve(halted)
    run = rig.store.load_run(halted.id)
    assert run.escalation is not None
    receipts = [
        receipt.model_copy(update={"scope_approval_context_fingerprint": "d" * 64})
        for receipt in run.escalation.accepted_replies
    ]
    escalation = run.escalation.model_copy(update={"accepted_replies": receipts})
    rig.store.save_run(run.model_copy(update={"escalation": escalation}))

    refused = rig.controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert "receipt fingerprint does not match" in (refused.failure_reason or "")


@pytest.mark.parametrize(
    "update",
    [{"state": WorkflowState.PR_READY}, {"pull_request_url": "https://github.com/o/r/pull/1"}],
    ids=["publish-time", "after-pull-request"],
)
def test_a_scope_halt_outside_the_implementation_loop_has_no_context(
    tmp_path: Path, source_repo: Path, update: dict[str, object]
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    before_halt = halted.model_copy(update={"state": WorkflowState.VERIFYING, **update})

    assert build_scope_approval_context(before_halt, rig.store, rig.config, "e" * 32) is None
