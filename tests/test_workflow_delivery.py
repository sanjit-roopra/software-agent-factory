import json
from collections.abc import Callable
from pathlib import Path

import pytest
from factory_testing import build_config, git, triage_hook, work_item

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig, RiskAssessmentConfig
from software_agent_factory.delivery import DeliveryTarget
from software_agent_factory.github import GitHubError, UnexpectedRepositoryError
from software_agent_factory.models import (
    AcceptedReplyReceipt,
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    CICheckEvidence,
    CIReport,
    Complexity,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    RepairContext,
    ResumeClassification,
    ReviewAcceptanceReason,
    ReviewDispositionStatus,
    ReviewFindingCategory,
    ReviewFindingDisposition,
    ReviewFindingDraft,
    ReviewImpasseKind,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
    TriageResult,
    WorkflowState,
    utc_now,
)
from software_agent_factory.observability import _compute_aggregate_metrics
from software_agent_factory.publishing import MergeResult, PublishResult
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController, delivery_policy_fingerprint
from software_agent_factory.workspace import GitWorktreeWorkspace, WorkspaceLockError

pytestmark = pytest.mark.project_delivery


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


class LocalPublisher:
    def __init__(
        self, *, crash: bool = False, error: Exception | None = None, fail_from_call: int = 1
    ) -> None:
        self.calls = 0
        self.crash = crash
        self.error = error
        self.fail_from_call = fail_from_call
        self.parents: list[str] = []
        self.commits: list[str] = []
        self.flags: list[list[str]] = []

    def resolve_base_branch(self, source_repo: Path) -> str:
        return "main"

    def publish(self, **kwargs) -> PublishResult:
        self.calls += 1
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt
        if self.error is not None and self.calls >= self.fail_from_call:
            raise self.error
        path = kwargs["workspace_path"]
        self.parents.append(kwargs["expected_parent_sha"])
        git(path, "add", "-A")
        if git(path, "diff", "--cached", "--name-only").strip():
            git(path, "commit", "-m", kwargs["commit_message"])
        head = git(path, "rev-parse", "HEAD").strip()
        self.commits.append(head)
        kwargs["record_commit"](head)
        return PublishResult(
            commit_sha=head,
            base_branch="main",
            pull_request_url="https://github.com/acme/repo/pull/42",
            created_pull_request=kwargs["existing_pull_request_url"] is None,
        )

    def flag_needs_look(self, **kwargs) -> None:
        self.flags.append(list(kwargs["reasons"]))


class Observer:
    def __init__(
        self,
        reports: list[CIReport] | None = None,
        *,
        crash: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.reports = list(reports or [CIReport(overall="PASS")])
        self.calls = 0
        self.crash = crash
        self.error = error

    def observe(self, **kwargs) -> CIReport:
        self.calls += 1
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt
        if self.error is not None:
            raise self.error
        report = self.reports[min(self.calls - 1, len(self.reports) - 1)]
        return report.model_copy(update={"repair_attempts_used": kwargs["repair_attempts_used"]})


class Merger:
    def __init__(self, error: Exception | None = None, *, crash: bool = False) -> None:
        self.calls: list[dict] = []
        self.error = error
        self.crash = crash

    def validate_repository(self, repo_path: Path) -> str:
        return "acme/repo"

    def merge(self, **kwargs) -> MergeResult:
        self.calls.append(kwargs)
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt
        if self.error is not None:
            raise self.error
        return MergeResult(commit_sha="b" * 40, pull_request_url=kwargs["pull_request_url"])


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest):
        self.requests.append(request)
        return self.delegate.run(request)


def _config(
    tmp_path: Path,
    *,
    merge: bool = True,
    verify: list[str] | None = None,
    max_total_attempts: int = 6,
) -> FactoryConfig:
    payload = build_config(
        tmp_path / "data",
        verify=verify or ["true"],
        max_total_attempts=max_total_attempts,
        pull_request={"enabled": True, "draft": False, "base_branch": "main"},
        ci={"enabled": True, "repair_attempts": 2},
    ).model_dump(mode="json")
    payload["merge"] = {
        "enabled": merge,
        "allowed_repositories": ["acme/repo"],
        "required_checks": ["quality"],
    }
    return FactoryConfig.model_validate(payload)


def _controller(config, *, publisher=None, observer=None, merger=None, runtime=None):
    store = FileRunStore(config.data_dir)
    return WorkflowController(
        config,
        store,
        runtime or FakeAgentRuntime(),
        publisher=publisher or LocalPublisher(),
        ci_observer=observer or Observer(),
        merger=merger or Merger(),
        delivery_base_resolver=lambda repo, expected: DeliveryTarget(
            expected, "github.com", git(repo, "rev-parse", "HEAD").strip()
        ),
    ), store


def test_merge_run_starts_from_fetched_base_not_unpushed_local_head(
    tmp_path: Path, source_repo: Path
) -> None:
    base = git(source_repo, "rev-parse", "HEAD").strip()
    (source_repo / "unreviewed.txt").write_text("local-only work\n")
    git(source_repo, "add", "-A")
    git(source_repo, "commit", "-m", "local unreviewed commit")
    local_head = git(source_repo, "rev-parse", "HEAD").strip()
    config = _config(tmp_path)
    store = FileRunStore(config.data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(),
        publisher=LocalPublisher(),
        ci_observer=Observer(),
        merger=Merger(),
        delivery_base_resolver=lambda repo, expected: DeliveryTarget(expected, "github.com", base),
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert not (Path(run.workspace_path) / "unreviewed.txt").exists()
    assert git(source_repo, "rev-parse", "HEAD").strip() == local_head
    assert (
        run.reviewed_tree_sha == git(Path(run.workspace_path), "rev-parse", "HEAD^{tree}").strip()
    )


def test_live_repository_drift_blocks_publication(tmp_path: Path, source_repo: Path) -> None:
    class ChangingRepositoryMerger(Merger):
        def __init__(self) -> None:
            super().__init__()
            self.validations = 0

        def validate_repository(self, repo_path: Path) -> str:
            self.validations += 1
            return "acme/repo" if self.validations == 1 else "acme/other"

    publisher = LocalPublisher()
    merger = ChangingRepositoryMerger()
    controller, _ = _controller(_config(tmp_path), publisher=publisher, merger=merger)
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "repository changed" in run.failure_reason
    assert publisher.calls == 0
    assert merger.calls == []


def test_green_ci_merges_and_persists_actual_merge_commit(
    tmp_path: Path, source_repo: Path
) -> None:
    merger = Merger()
    controller, store = _controller(_config(tmp_path), merger=merger)
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert run.merge_commit_sha == "b" * 40
    assert store.load_run(run.id).merge_commit_sha == run.merge_commit_sha
    assert merger.calls[0]["expected_head_sha"] == run.commit_sha
    assert run.reviewed_commit_sha == run.commit_sha
    assert merger.calls[0]["base_branch"] == "main"
    assert run.delivery_policy_fingerprint == delivery_policy_fingerprint(_config(tmp_path))


def test_default_pr_ci_mode_does_not_merge(tmp_path: Path, source_repo: Path) -> None:
    merger = Merger()
    controller, _ = _controller(_config(tmp_path, merge=False), merger=merger)
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert run.merge_commit_sha is None
    assert merger.calls == []


def test_merge_rejection_is_not_done(tmp_path: Path, source_repo: Path) -> None:
    controller, _ = _controller(
        _config(tmp_path), merger=Merger(GitHubError("required check missing"))
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "required check missing" in run.failure_reason
    assert run.merge_commit_sha is None


def test_repository_authorization_precedes_all_agent_calls(
    tmp_path: Path, source_repo: Path
) -> None:
    class UnauthorizedMerger(Merger):
        def validate_repository(self, repo_path: Path) -> str:
            raise GitHubError("repository is not allowlisted")

    runtime = RecordingRuntime()
    controller, _ = _controller(_config(tmp_path), runtime=runtime, merger=UnauthorizedMerger())
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "not allowlisted" in run.failure_reason
    assert runtime.requests == []


def _failed_ci(category: str = "TEST_FAILURE") -> CIReport:
    return CIReport(
        overall="FAIL",
        checks=[CICheckEvidence(name="tests", status="FAIL", failure_category=category)],
    )


def test_ci_repair_reverifies_reviews_and_merges_latest_head(
    tmp_path: Path, source_repo: Path
) -> None:
    runtime = RecordingRuntime()
    merger = Merger()
    publisher = LocalPublisher()
    controller, _ = _controller(
        _config(tmp_path),
        runtime=runtime,
        publisher=publisher,
        merger=merger,
        observer=Observer([_failed_ci(), CIReport(overall="PASS")]),
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert publisher.calls == 2
    assert publisher.parents == [run.base_commit_sha, publisher.commits[0]]
    assert len(merger.calls) == 1
    assert merger.calls[0]["expected_head_sha"] == run.commit_sha
    assert [record.budget for record in run.attempt_records] == [
        AttemptBudget.IMPLEMENTATION,
        AttemptBudget.CI_REPAIR,
    ]
    assert sum(request.role is AgentRole.REVIEWER for request in runtime.requests) == 2
    assert run.reviewed_commit_sha == run.commit_sha


def test_rework_telemetry_initial_plus_ci_repair(tmp_path: Path, source_repo: Path) -> None:
    runtime = RecordingRuntime()
    merger = Merger()
    publisher = LocalPublisher()
    controller, _ = _controller(
        _config(tmp_path),
        runtime=runtime,
        publisher=publisher,
        merger=merger,
        observer=Observer([_failed_ci(), CIReport(overall="PASS")]),
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert [record.budget for record in run.attempt_records] == [
        AttemptBudget.IMPLEMENTATION,
        AttemptBudget.CI_REPAIR,
    ]
    assert [record.triggered_by for record in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.CI,
    ]

    # First CI repair is counted even though its per-budget attempt number is 1
    assert run.performance.counters.get("rework_total") == 1
    assert run.performance.counters.get("rework.repair_attempt") == 1
    assert run.performance.counters.get("gate_failures_total", 0) == 0

    metrics = _compute_aggregate_metrics([run])
    assert metrics.performance.rework.total_rework_attempts == 1
    assert metrics.performance.rework.runs_with_rework == 1
    assert metrics.performance.rework.total_gate_failures == 0


def test_unbound_review_cannot_authorize_merge_after_resume(
    tmp_path: Path, source_repo: Path
) -> None:
    merger = Merger()
    controller, store = _controller(_config(tmp_path), observer=Observer(crash=True), merger=merger)
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="review-bound")
    run = store.load_run("review-bound")
    store.save_run(run.model_copy(update={"reviewed_commit_sha": "a" * 40}))
    recovered = controller.resume(run.id, source_repo)
    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert "review authorization" in recovered.failure_reason
    assert merger.calls == []


def test_low_risk_reviewer_rejection_continues_through_pr_and_merge(
    tmp_path: Path, source_repo: Path
) -> None:
    def reject(request: AgentRequest) -> AgentResult:
        if request.prior_review_findings:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=[
                    ReviewFindingDisposition(
                        finding_id=finding.id,
                        status=ReviewDispositionStatus.UNRESOLVED,
                        rationale="The defect remains.",
                    )
                    for finding in request.prior_review_findings
                ],
            )
        else:
            report = ReviewReport(
                approved=False,
                blocking_findings=[
                    ReviewFindingDraft(
                        category=ReviewFindingCategory.CORRECTNESS,
                        message="Not correct yet",
                        locations=[
                            ReviewSourceLocation(
                                path="FACTORY_NOTES.md",
                                start_line=1,
                                end_line=1,
                            )
                        ],
                    )
                ],
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=report,
        )

    publisher = LocalPublisher()
    merger = Merger()
    runtime = FakeAgentRuntime(reviewer=reject)
    controller, _ = _controller(
        _config(tmp_path), runtime=runtime, publisher=publisher, merger=merger
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.DONE
    assert run.review_acceptance is not None
    assert publisher.calls == 1
    assert len(merger.calls) == 1


def test_ci_repair_carries_accepted_debt_without_reopening_unchanged_finding(
    tmp_path: Path,
    source_repo: Path,
) -> None:
    reviewer_requests: list[AgentRequest] = []

    def reject_until_accepted(request: AgentRequest) -> AgentResult:
        reviewer_requests.append(request)
        finding = ReviewFindingDraft(
            category=ReviewFindingCategory.CORRECTNESS,
            message="Not correct yet",
            locations=[
                ReviewSourceLocation(
                    path="FACTORY_NOTES.md",
                    start_line=1,
                    end_line=1,
                )
            ],
        )
        if request.accepted_review_findings:
            report = ReviewReport(approved=False, blocking_findings=[finding])
        elif request.prior_review_findings:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=[
                    ReviewFindingDisposition(
                        finding_id=prior.id,
                        status=ReviewDispositionStatus.UNRESOLVED,
                        rationale="The defect remains.",
                    )
                    for prior in request.prior_review_findings
                ],
            )
        else:
            report = ReviewReport(approved=False, blocking_findings=[finding])
        return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=report)

    publisher = LocalPublisher()
    controller, _ = _controller(
        _config(tmp_path),
        runtime=FakeAgentRuntime(reviewer=reject_until_accepted),
        publisher=publisher,
        observer=Observer([_failed_ci(), CIReport(overall="PASS")]),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    assert publisher.calls == 2
    assert run.review_acceptance is not None
    assert run.review_acceptance.reason is ReviewAcceptanceReason.CARRIED_FORWARD
    assert run.review_acceptance.reviewed_tree_sha == run.reviewed_tree_sha
    assert reviewer_requests[-1].accepted_review_findings
    assert run.review_ledger.open_findings == []


def test_changes_after_review_cannot_be_published(tmp_path: Path, source_repo: Path) -> None:
    def approve_then_change(request: AgentRequest) -> AgentResult:
        (Path(request.workspace_path) / "unreviewed.txt").write_text("not in reviewed diff")
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=True),
        )

    publisher = LocalPublisher()
    runtime = FakeAgentRuntime(reviewer=approve_then_change)
    controller, _ = _controller(_config(tmp_path), runtime=runtime, publisher=publisher)
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "after independent review" in run.failure_reason
    assert publisher.calls == 0


@pytest.mark.parametrize("report", [_failed_ci(), _failed_ci("INFRA_FAILURE")])
def test_failed_ci_never_merges(tmp_path: Path, source_repo: Path, report: CIReport) -> None:
    merger = Merger()
    controller, _ = _controller(_config(tmp_path), merger=merger, observer=Observer([report]))
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert merger.calls == []
    assert sum(a.budget is AttemptBudget.CI_REPAIR for a in run.attempt_records) <= 2


def test_ci_identity_failure_halts_cleanly(tmp_path: Path, source_repo: Path) -> None:
    observer = Observer(error=UnexpectedRepositoryError("persisted PR host changed"))
    controller, _ = _controller(_config(tmp_path), observer=observer)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason == "could not observe CI: persisted PR host changed"


@pytest.mark.parametrize("boundary", ["publish", "observe", "merge"])
def test_resume_delivery_does_not_repeat_agents_or_reset_budgets(
    tmp_path: Path, source_repo: Path, boundary: str
) -> None:
    runtime = RecordingRuntime()
    config = _config(tmp_path)
    publisher = LocalPublisher(crash=boundary == "publish")
    observer = Observer(crash=boundary == "observe")
    merger = Merger(crash=boundary == "merge")
    controller, store = _controller(
        config, publisher=publisher, observer=observer, merger=merger, runtime=runtime
    )
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="recover-me")
    checkpoint = store.load_run("recover-me")
    agent_count = len(runtime.requests)
    controller, _ = _controller(
        config, publisher=publisher, observer=observer, merger=merger, runtime=runtime
    )
    recovered = controller.resume(checkpoint.id, source_repo)
    assert recovered.state is WorkflowState.DONE
    assert recovered.merge_commit_sha
    assert recovered.attempt_records == checkpoint.attempt_records
    assert len(runtime.requests) == agent_count
    assert controller.resume(checkpoint.id, source_repo) == recovered


def _with_risk_assessment(config: FactoryConfig, *, enabled: bool) -> FactoryConfig:
    return config.model_copy(update={"risk_assessment": RiskAssessmentConfig(enabled=enabled)})


def test_resume_keeps_the_risk_assessment_choice_the_run_started_with(
    tmp_path: Path, source_repo: Path
) -> None:
    started = _with_risk_assessment(_config(tmp_path), enabled=False)
    publisher = LocalPublisher(crash=True)
    runtime = FakeAgentRuntime(triage=triage_hook(risk=Risk.R2))
    controller, store = _controller(started, publisher=publisher, runtime=runtime)
    item = work_item()
    with pytest.raises(KeyboardInterrupt):
        controller.run(item, source_repo, run_id="autonomous")
    assert store.load_run("autonomous").state is WorkflowState.PR_READY

    resumed_controller, _ = _controller(
        _with_risk_assessment(started, enabled=True), publisher=publisher, runtime=runtime
    )
    recovered = resumed_controller.resume("autonomous", source_repo)

    assert recovered.state is WorkflowState.DONE
    assert recovered.risk_assessment_enabled is False


def test_resume_with_the_switch_off_cannot_bypass_an_approval_the_run_started_with(
    tmp_path: Path, source_repo: Path
) -> None:
    started = _with_risk_assessment(_config(tmp_path), enabled=True)
    publisher = LocalPublisher(crash=True)
    controller, store = _controller(started, publisher=publisher)
    item = work_item()
    with pytest.raises(KeyboardInterrupt):
        controller.run(item, source_repo, run_id="guarded")
    triage = store.load_artifact("guarded", TriageResult)
    store.save_artifact("guarded", triage.model_copy(update={"risk": Risk.R2}))
    calls_before = publisher.calls

    resumed_controller, _ = _controller(
        _with_risk_assessment(started, enabled=False), publisher=publisher
    )
    recovered = resumed_controller.resume("guarded", source_repo)

    assert recovered.state is not WorkflowState.DONE
    assert recovered.risk_assessment_enabled is True
    assert publisher.calls == calls_before


APPROVED_RUN_ID = "approved"


def _risk_runtime(risk: Risk = Risk.R2) -> FakeAgentRuntime:
    return FakeAgentRuntime(triage=triage_hook(risk=risk))


_Delivery = tuple[LocalPublisher, Observer, FileRunStore, FactoryConfig]


def _halted_for_approval(
    tmp_path: Path, source_repo: Path, boundary: str, risk: Risk
) -> tuple[WorkflowController, _Delivery, FactoryRun]:
    """Halt a run for approval, with a publisher or observer that crashes at ``boundary``."""
    config = _config(tmp_path)
    config = config.model_copy(
        update={"escalation": config.escalation.model_copy(update={"enabled": True})}
    )
    publisher = LocalPublisher(crash=boundary == "publish")
    observer = Observer(crash=boundary == "observe")
    controller, store = _controller(
        config, publisher=publisher, observer=observer, runtime=_risk_runtime(risk)
    )
    halted = controller.run(work_item(), source_repo, run_id=APPROVED_RUN_ID)
    assert halted.state is WorkflowState.NEEDS_HUMAN
    assert halted.escalation is not None
    assert halted.escalation.approval_context is not None
    return controller, (publisher, observer, store, config), halted


def _github_approved_interrupted_at(
    tmp_path: Path, source_repo: Path, boundary: str, *, risk: Risk = Risk.R2
) -> _Delivery:
    """Halt a run for approval, record a GitHub approval, reopen it, then crash in delivery.

    The receipt is written the way the reply poller persists it; ``reopen`` is the real path.
    """
    controller, delivery, halted = _halted_for_approval(tmp_path, source_repo, boundary, risk)
    escalation = halted.escalation
    assert escalation is not None
    assert escalation.approval_context is not None
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={APPROVED_RUN_ID} episode={escalation.episode_id}",
        episode_id=escalation.episode_id,
        run_id=APPROVED_RUN_ID,
        approval_context_fingerprint=escalation.approval_context.context_fingerprint,
    )
    delivery[2].save_run(
        halted.model_copy(
            update={
                "escalation": escalation.model_copy(
                    update={
                        "status": EscalationStatus.REOPENED,
                        "accepted_replies": [receipt],
                        "reopen_count": 1,
                    }
                )
            }
        )
    )
    with pytest.raises(KeyboardInterrupt):
        controller.reopen(APPROVED_RUN_ID, source_repo)
    return delivery


def _dashboard_approved_interrupted_at(
    tmp_path: Path, source_repo: Path, boundary: str, *, risk: Risk = Risk.R2
) -> _Delivery:
    """Halt a run for approval, approve it through a dashboard request, reopen it, then crash.

    The approval goes through ``create_dashboard_request`` and ``ingest_dashboard_request``;
    ``reopen`` is the real path.
    """
    controller, delivery, halted = _halted_for_approval(tmp_path, source_repo, boundary, risk)
    _, _, store, config = delivery
    escalation = halted.escalation
    assert escalation is not None
    assert escalation.approval_context is not None
    request = DashboardResumeRequest(
        run_id=APPROVED_RUN_ID,
        episode_id=escalation.episode_id,
        context_fingerprint=escalation.approval_context.context_fingerprint,
        action=ResumeClassification.RISK_APPROVAL,
    )
    assert store.create_dashboard_request(APPROVED_RUN_ID, request)
    assert ingest_dashboard_request(halted, store, config, utc_now()) is not None
    with pytest.raises(KeyboardInterrupt):
        controller.reopen(APPROVED_RUN_ID, source_repo)
    return delivery


@pytest.mark.parametrize(
    ("boundary", "checkpoint", "publish_calls", "observe_calls"),
    [
        # Each boundary crashed once, then the resume retried it.
        ("publish", WorkflowState.PR_READY, 2, 1),
        ("observe", WorkflowState.CI_RUNNING, 1, 2),
    ],
)
def test_resume_continues_delivery_for_a_human_approved_risk(
    tmp_path: Path,
    source_repo: Path,
    boundary: str,
    checkpoint: WorkflowState,
    publish_calls: int,
    observe_calls: int,
) -> None:
    publisher, observer, store, config = _github_approved_interrupted_at(
        tmp_path, source_repo, boundary
    )
    assert store.load_run(APPROVED_RUN_ID).state is checkpoint

    resumed_controller, _ = _controller(
        config, publisher=publisher, observer=observer, runtime=_risk_runtime()
    )
    recovered = resumed_controller.resume(APPROVED_RUN_ID, source_repo)

    assert recovered.state is WorkflowState.DONE
    assert not recovered.failure_reason
    assert publisher.calls == publish_calls
    assert observer.calls == observe_calls


@pytest.mark.parametrize("risk", [Risk.R2, Risk.R3], ids=["R2", "R3"])
def test_resume_continues_delivery_for_a_dashboard_approved_risk(
    tmp_path: Path, source_repo: Path, risk: Risk
) -> None:
    publisher, observer, store, config = _dashboard_approved_interrupted_at(
        tmp_path, source_repo, "publish", risk=risk
    )
    approved = store.load_run(APPROVED_RUN_ID)
    assert approved.state is WorkflowState.PR_READY
    assert approved.escalation is not None
    assert [r.source for r in approved.escalation.accepted_replies] == ["dashboard"]

    resumed_controller, _ = _controller(
        config, publisher=publisher, observer=observer, runtime=_risk_runtime(risk)
    )
    recovered = resumed_controller.resume(APPROVED_RUN_ID, source_repo)

    assert recovered.state is WorkflowState.DONE
    assert not recovered.failure_reason
    assert recovered.escalation is not None
    assert recovered.escalation.reopen_count == 1
    assert recovered.escalation.episode_id == approved.escalation.episode_id
    assert publisher.calls == 2
    assert observer.calls == 1


def _assert_resume_refused_after(
    tmp_path: Path, source_repo: Path, tamper: Callable[[FileRunStore], None]
) -> None:
    """Tamper with the persisted run after its approval, resume, and expect a refusal.

    The controller gets the R2 triage hook like the happy path, though resume never re-triages.
    """
    publisher, observer, store, config = _github_approved_interrupted_at(
        tmp_path, source_repo, "publish"
    )
    tamper(store)
    calls_before = publisher.calls

    resumed_controller, _ = _controller(
        config, publisher=publisher, observer=observer, runtime=_risk_runtime()
    )
    recovered = resumed_controller.resume(APPROVED_RUN_ID, source_repo)

    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert "persisted triage does not authorize delivery" in recovered.failure_reason
    assert publisher.calls == calls_before


def _tamper_triage(store: FileRunStore, **update: object) -> None:
    triage = store.load_artifact(APPROVED_RUN_ID, TriageResult)
    store.save_artifact(APPROVED_RUN_ID, triage.model_copy(update=update))


def _tamper_rationale(store: FileRunStore) -> None:
    # The new text passes contains_unsafe_content, so the refusal comes from the
    # fingerprint mismatch and not from the context failing to build.
    triage = store.load_artifact(APPROVED_RUN_ID, TriageResult)
    assert triage.risk_rationale is not None
    rationale = triage.risk_rationale.model_copy(
        update={"residual_risk": "Nobody reviews the release."}
    )
    _tamper_triage(store, risk_rationale=rationale)


def _tamper_complexity(store: FileRunStore) -> None:
    # Another valid complexity: the context still builds, only its fingerprint differs.
    _tamper_triage(store, complexity=Complexity.L3)


def _tamper_receipts(store: FileRunStore, rewrite: Callable[[list], list]) -> None:
    run = store.load_run(APPROVED_RUN_ID)
    assert run.escalation is not None
    escalation = run.escalation.model_copy(
        update={"accepted_replies": rewrite(list(run.escalation.accepted_replies))}
    )
    store.save_run(run.model_copy(update={"escalation": escalation}))


def _tamper_receipt_never_dispatched(store: FileRunStore) -> None:
    _tamper_receipts(
        store, lambda receipts: [r.model_copy(update={"dispatched_at": None}) for r in receipts]
    )


def _tamper_receipt_from_another_run(store: FileRunStore) -> None:
    _tamper_receipts(
        store, lambda receipts: [r.model_copy(update={"run_id": "other-run"}) for r in receipts]
    )


def _tamper_receipt_without_fingerprint(store: FileRunStore) -> None:
    _tamper_receipts(
        store,
        lambda receipts: [
            r.model_copy(update={"approval_context_fingerprint": None}) for r in receipts
        ],
    )


def _tamper_no_receipts(store: FileRunStore) -> None:
    _tamper_receipts(store, lambda receipts: [])


def _tamper_no_escalation(store: FileRunStore) -> None:
    run = store.load_run(APPROVED_RUN_ID)
    store.save_run(run.model_copy(update={"escalation": None}))


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_rationale,
        _tamper_complexity,
        _tamper_receipt_never_dispatched,
        _tamper_receipt_from_another_run,
        _tamper_receipt_without_fingerprint,
        _tamper_no_receipts,
        _tamper_no_escalation,
    ],
)
def test_resume_refuses_a_risk_approval_that_no_longer_holds(
    tmp_path: Path, source_repo: Path, tamper: Callable[[FileRunStore], None]
) -> None:
    _assert_resume_refused_after(tmp_path, source_repo, tamper)


def test_resume_skips_a_receipt_from_another_run_and_uses_the_valid_one_after_it(
    tmp_path: Path, source_repo: Path
) -> None:
    publisher, observer, store, config = _github_approved_interrupted_at(
        tmp_path, source_repo, "publish"
    )
    _tamper_receipts(
        store,
        lambda receipts: [
            receipts[0].model_copy(update={"run_id": "other-run", "comment_id": 2}),
            *receipts,
        ],
    )

    resumed_controller, _ = _controller(
        config, publisher=publisher, observer=observer, runtime=_risk_runtime()
    )
    recovered = resumed_controller.resume(APPROVED_RUN_ID, source_repo)

    assert recovered.state is WorkflowState.DONE


def test_resume_refuses_policy_changes_without_mutating_checkpoint(
    tmp_path: Path, source_repo: Path
) -> None:
    config = _config(tmp_path)
    controller, store = _controller(config, observer=Observer(crash=True))
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="recover-me")
    before = store.load_run("recover-me")
    payload = config.model_dump(mode="json")
    payload["merge"]["required_checks"] = ["different"]
    controller, _ = _controller(FactoryConfig.model_validate(payload))
    with pytest.raises(ValueError, match="policy changed"):
        controller.resume(before.id, source_repo)
    assert store.load_run(before.id) == before


@pytest.mark.parametrize("change", ["dirty", "head", "branch", "patch"])
def test_resume_rejects_unreviewed_or_mismatched_workspace(
    tmp_path: Path, source_repo: Path, change: str
) -> None:
    config = _config(tmp_path)
    controller, store = _controller(config, observer=Observer(crash=True))
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="recover-me")
    run = store.load_run("recover-me")
    path = Path(run.workspace_path)
    if change == "patch":
        store.save_patch(run.id, "different reviewed patch")
    elif change == "branch":
        git(path, "switch", "-c", "different")
    else:
        (path / "unreviewed.txt").write_text("unreviewed")
        if change == "head":
            git(path, "add", "-A")
            git(path, "commit", "-m", "unreviewed")
    recovered = controller.resume(run.id, source_repo)
    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert recovered.merge_commit_sha is None
    assert recovered.attempt_records == run.attempt_records


def test_resume_preserves_ambiguous_implementation_without_restarting_it(
    tmp_path: Path, source_repo: Path
) -> None:
    config = _config(tmp_path)
    controller, store = _controller(config)
    run = FactoryRun(
        id="in-flight",
        work_item_id="WI-1",
        state=WorkflowState.IMPLEMENTING,
        delivery_policy_fingerprint=delivery_policy_fingerprint(config),
    )
    store.save_run(run)
    recovered = controller.resume(run.id, source_repo)
    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert "safe delivery checkpoint" in recovered.failure_reason
    assert recovered.attempt_records == []


@pytest.mark.parametrize("state", [WorkflowState.REFINING, WorkflowState.RESEARCHING])
def test_resume_routes_an_old_run_halted_before_planning_to_a_human(
    tmp_path: Path, source_repo: Path, state: WorkflowState
) -> None:
    """ADR-035: no new run enters these states, but an old run record still loads."""
    config = _config(tmp_path)
    controller, store = _controller(config)
    run = FactoryRun(
        id=f"old-{state.value.lower()}",
        work_item_id="WI-1",
        state=state,
        delivery_policy_fingerprint=delivery_policy_fingerprint(config),
    )
    store.save_run(run)
    old_research = {"schema_version": 1, "question": "Which validator?", "findings": []}
    (store.run_dir(run.id) / "research.json").write_text(json.dumps(old_research))

    recovered = controller.resume(run.id, source_repo)

    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert "safe delivery checkpoint" in (recovered.failure_reason or "")
    assert recovered.attempt_records == []
    assert store.load_run(run.id).state is WorkflowState.NEEDS_HUMAN


def test_resume_does_not_modify_a_live_locked_run(tmp_path: Path, source_repo: Path) -> None:
    config = _config(tmp_path)
    controller, store = _controller(config, observer=Observer(crash=True))
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="recover-me")
    before = store.load_run("recover-me")
    with GitWorktreeWorkspace(config.data_dir, source_repo, before.work_item_id):
        with pytest.raises(WorkspaceLockError):
            controller.resume(before.id, source_repo)
    assert store.load_run(before.id) == before


def test_existing_run_id_cannot_overwrite_evidence(tmp_path: Path, source_repo: Path) -> None:
    controller, store = _controller(_config(tmp_path))
    run = controller.run(work_item(), source_repo, run_id="once")
    with pytest.raises(ValueError, match="already exists"):
        controller.run(work_item(), source_repo, run_id=run.id)
    assert store.load_run(run.id) == run


def test_commit_receipt_survives_crash_before_branch_advance(
    tmp_path: Path, source_repo: Path
) -> None:
    class ReceiptPublisher(LocalPublisher):
        def publish(self, **kwargs) -> PublishResult:
            path = kwargs["workspace_path"]
            parent = kwargs["expected_parent_sha"]
            prepared = kwargs["prepared_commit_sha"]
            if prepared is None:
                assert git(path, "rev-parse", "HEAD").strip() == parent
                prepared = git(
                    path,
                    "commit-tree",
                    kwargs["expected_tree_sha"],
                    "-p",
                    parent,
                    "-m",
                    kwargs["commit_message"],
                ).strip()
                kwargs["record_commit"](prepared)
                raise KeyboardInterrupt
            git(path, "update-ref", f"refs/heads/{kwargs['branch_name']}", prepared, parent)
            return PublishResult(
                commit_sha=prepared,
                base_branch="main",
                pull_request_url="https://github.com/acme/repo/pull/42",
                created_pull_request=True,
            )

    runtime = RecordingRuntime()
    publisher = ReceiptPublisher()
    controller, store = _controller(_config(tmp_path), publisher=publisher, runtime=runtime)
    with pytest.raises(KeyboardInterrupt):
        controller.run(work_item(), source_repo, run_id="receipt-recovery")
    checkpoint = store.load_run("receipt-recovery")
    assert checkpoint.pending_commit_sha is not None
    assert checkpoint.commit_sha is None
    assert checkpoint.state is WorkflowState.PR_READY
    path = Path(checkpoint.workspace_path)
    assert git(path, "rev-parse", "HEAD").strip() == checkpoint.base_commit_sha
    assert git(path, "show", "-s", "--format=%P", checkpoint.pending_commit_sha).strip() == (
        checkpoint.base_commit_sha
    )
    invocation_count = len(runtime.requests)
    recovered = controller.resume(checkpoint.id, source_repo)
    assert recovered.state is WorkflowState.DONE
    assert recovered.commit_sha == checkpoint.pending_commit_sha
    assert recovered.pending_commit_sha is None
    assert recovered.attempt_records == checkpoint.attempt_records
    assert len(runtime.requests) == invocation_count


def test_publication_result_must_match_durable_receipt(tmp_path: Path, source_repo: Path) -> None:
    class IncorrectPublisher(LocalPublisher):
        def publish(self, **kwargs) -> PublishResult:
            result = super().publish(**kwargs)
            return PublishResult(
                commit_sha="f" * 40,
                base_branch=result.base_branch,
                pull_request_url=result.pull_request_url,
                created_pull_request=result.created_pull_request,
            )

    merger = Merger()
    controller, store = _controller(
        _config(tmp_path), publisher=IncorrectPublisher(), merger=merger
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "publication receipt" in run.failure_reason
    assert store.load_run(run.id).pending_commit_sha is not None
    assert merger.calls == []


def test_unattended_sensitive_scope_is_published(tmp_path: Path, source_repo: Path) -> None:
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        workflows = Path(request.workspace_path) / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "ci.yml").write_text("on: push\n")
        return default_runtime.run(request)

    config = _config(tmp_path)
    config.factory.unattended = True
    publisher = LocalPublisher()
    merger = Merger()
    controller, _ = _controller(
        config,
        runtime=FakeAgentRuntime(implementer=implementer),
        publisher=publisher,
        merger=merger,
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.unattended is True
    assert any(reason.startswith("scope drift") for reason in run.needs_look)
    assert publisher.flags == [run.needs_look]
    assert merger.calls == []


def _unattended(config: FactoryConfig) -> FactoryConfig:
    config.factory.unattended = True
    return config


def test_unattended_clean_run_merges_without_a_label(tmp_path: Path, source_repo: Path) -> None:
    publisher = LocalPublisher()
    merger = Merger()
    controller, _ = _controller(_unattended(_config(tmp_path)), publisher=publisher, merger=merger)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.needs_look == []
    assert publisher.flags == []
    assert len(merger.calls) == 1


@pytest.mark.parametrize(
    ("reports", "reason"),
    [
        ([CIReport(overall="PENDING", timed_out=True)], "still pending"),
        ([CIReport(overall="CANCELLED")], "no repairable failure"),
        ([_failed_ci("INFRASTRUCTURE")], "not repairable"),
        ([_failed_ci()], "CI repair budget exhausted after 2 attempt(s)"),
    ],
)
def test_unattended_red_ci_leaves_the_pull_request_open(
    tmp_path: Path, source_repo: Path, reports: list[CIReport], reason: str
) -> None:
    publisher = LocalPublisher()
    merger = Merger()
    controller, store = _controller(
        _unattended(_config(tmp_path)),
        publisher=publisher,
        merger=merger,
        observer=Observer(reports),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert len(run.needs_look) == 1
    assert reason in run.needs_look[0]
    assert publisher.flags == [run.needs_look]
    assert merger.calls == []
    assert store.load_run(run.id).needs_look == run.needs_look


_API_DOWN = GitHubError("API down")


@pytest.mark.parametrize(
    ("fakes", "reason"),
    [
        ({"observer": Observer(error=_API_DOWN)}, "could not observe CI: API down"),
        ({"merger": Merger(_API_DOWN)}, "could not merge the pull request: API down"),
        (
            {
                "publisher": LocalPublisher(error=_API_DOWN, fail_from_call=2),
                "observer": Observer([_failed_ci()]),
            },
            "could not publish the pull request: API down",
        ),
    ],
    ids=["observe", "merge", "republish"],
)
def test_unattended_delivery_error_leaves_the_pull_request_open(
    tmp_path: Path, source_repo: Path, fakes: dict, reason: str
) -> None:
    fakes = {"publisher": LocalPublisher(), **fakes}
    controller, store = _controller(_unattended(_config(tmp_path)), **fakes)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.needs_look == [reason]
    assert fakes["publisher"].flags == [[reason]]
    assert run.merge_commit_sha is None
    assert store.load_run(run.id).needs_look == [reason]


def test_unattended_publish_error_before_a_pull_request_stops_for_a_person(
    tmp_path: Path, source_repo: Path
) -> None:
    publisher = LocalPublisher(error=GitHubError("API down"))
    controller, _ = _controller(_unattended(_config(tmp_path)), publisher=publisher)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason == "could not publish the pull request: API down"
    assert run.pull_request_url is None
    assert publisher.flags == []


def _halted_on_publish_error(tmp_path: Path, source_repo: Path):
    """A run halted because the first publish failed, and the parts to retry it."""
    config = _unattended(_config(tmp_path))
    publisher = LocalPublisher(error=GitHubError("API down"))
    controller, store = _controller(config, publisher=publisher)
    halted = controller.run(work_item(), source_repo)
    return controller, publisher, store, config, halted


def _accept_dashboard_retry(store: FileRunStore, config: FactoryConfig, halted: FactoryRun) -> None:
    escalation = halted.escalation
    assert escalation is not None
    assert escalation.delivery_retry_context is not None
    request = DashboardResumeRequest(
        run_id=halted.id,
        episode_id=escalation.episode_id,
        context_fingerprint=escalation.delivery_retry_context.context_fingerprint,
        action=ResumeClassification.DELIVERY_RETRY,
    )
    assert store.create_dashboard_request(halted.id, request)
    assert ingest_dashboard_request(halted, store, config, utc_now()) is not None


def test_a_publish_error_halt_can_be_retried_and_publishes_the_reviewed_work(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, publisher, store, config, halted = _halted_on_publish_error(tmp_path, source_repo)
    assert halted.state is WorkflowState.NEEDS_HUMAN
    assert halted.escalation is not None
    assert halted.escalation.resume_classification is ResumeClassification.DELIVERY_RETRY
    publisher.error = None
    _accept_dashboard_retry(store, config, halted)

    retried = controller.reopen(halted.id, source_repo)

    assert retried.state is WorkflowState.DONE, retried.failure_reason
    assert retried.pull_request_url == "https://github.com/acme/repo/pull/42"
    assert retried.attempt_records == halted.attempt_records
    assert retried.escalation is not None
    assert retried.escalation.status is EscalationStatus.RESUMED
    assert retried.escalation.reopen_count == 1
    assert publisher.calls == 2
    published_tree = git(
        Path(halted.workspace_path), "rev-parse", f"{publisher.commits[-1]}^{{tree}}"
    )
    assert published_tree.strip() == halted.reviewed_tree_sha


def test_a_retry_that_fails_to_publish_again_halts_for_another_retry(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, publisher, store, config, halted = _halted_on_publish_error(tmp_path, source_repo)
    _accept_dashboard_retry(store, config, halted)

    again = controller.reopen(halted.id, source_repo)

    assert again.state is WorkflowState.NEEDS_HUMAN
    assert again.failure_reason == "could not publish the pull request: API down"
    assert again.escalation is not None
    assert again.escalation.resume_classification is ResumeClassification.DELIVERY_RETRY
    assert again.escalation.episode_number == 2
    assert again.escalation.reopen_count == 1
    assert publisher.calls == 2


def test_a_retry_fails_closed_when_the_workspace_changed_since_the_halt(
    tmp_path: Path, source_repo: Path
) -> None:
    controller, publisher, store, config, halted = _halted_on_publish_error(tmp_path, source_repo)
    assert halted.workspace_path is not None
    (Path(halted.workspace_path) / "tampered.txt").write_text("not reviewed")
    publisher.error = None
    _accept_dashboard_retry(store, config, halted)

    failed = controller.reopen(halted.id, source_repo)

    assert failed.state is WorkflowState.NEEDS_HUMAN
    assert failed.escalation is not None
    assert failed.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert "workspace changes do not match" in (failed.failure_reason or "")
    assert publisher.calls == 1


def _with_escalation(run: FactoryRun, **update: object) -> FactoryRun:
    assert run.escalation is not None
    return run.model_copy(update={"escalation": run.escalation.model_copy(update=update)})


def _without_retry_context(run: FactoryRun) -> FactoryRun:
    return _with_escalation(run, delivery_retry_context=None)


def _with_other_receipt_fingerprint(run: FactoryRun) -> FactoryRun:
    assert run.escalation is not None
    receipts = [
        receipt.model_copy(update={"delivery_retry_context_fingerprint": "d" * 64})
        for receipt in run.escalation.accepted_replies
    ]
    return _with_escalation(run, accepted_replies=receipts)


def _with_other_tree(run: FactoryRun) -> FactoryRun:
    return run.model_copy(update={"reviewed_tree_sha": "c" * 40})


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_without_retry_context, "missing or invalid delivery retry context"),
        (_with_other_receipt_fingerprint, "receipt fingerprint does not match"),
        (_with_other_tree, "no longer matches its delivery retry context"),
    ],
    ids=["no-context", "other-receipt", "other-tree"],
)
def test_a_retry_that_no_longer_matches_its_context_cannot_reopen_the_run(
    tmp_path: Path,
    source_repo: Path,
    tamper: Callable[[FactoryRun], FactoryRun],
    message: str,
) -> None:
    controller, _, store, config, halted = _halted_on_publish_error(tmp_path, source_repo)
    _accept_dashboard_retry(store, config, halted)
    reopening = tamper(store.load_run(halted.id))

    with pytest.raises(ValueError, match=message):
        controller._transition_reopened(reopening)


@pytest.mark.parametrize(
    ("state", "fields"),
    [
        (WorkflowState.PLANNING, {}),
        (WorkflowState.PR_READY, {"pull_request_url": "https://github.com/acme/repo/pull/42"}),
    ],
    ids=["not-at-pr-ready", "pull-request-exists"],
)
def test_a_publish_error_halt_without_a_publish_to_retry_is_not_resumable(
    tmp_path: Path, state: WorkflowState, fields: dict[str, str]
) -> None:
    controller, store = _controller(_config(tmp_path))
    run = FactoryRun(
        id="run-x",
        work_item_id="task-x",
        state=state,
        reviewed_tree_sha="a" * 40,
        base_commit_sha="b" * 40,
        branch_name="factory/task-x",
        **fields,
    )
    store.save_run(run)

    halted = controller.transition(
        run, WorkflowState.NEEDS_HUMAN, failure_reason="could not publish the pull request: down"
    )

    assert halted.escalation is not None
    assert halted.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert halted.escalation.delivery_retry_context is None


def test_unattended_changes_after_review_are_published_and_labelled(
    tmp_path: Path, source_repo: Path
) -> None:
    def approve_then_change(request: AgentRequest) -> AgentResult:
        (Path(request.workspace_path) / "unreviewed.txt").write_text("not in reviewed diff")
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=True),
        )

    publisher = LocalPublisher()
    merger = Merger()
    controller, store = _controller(
        _unattended(_config(tmp_path)),
        runtime=FakeAgentRuntime(reviewer=approve_then_change),
        publisher=publisher,
        merger=merger,
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.needs_look == ["repository changed after review"]
    assert publisher.flags == [run.needs_look]
    assert publisher.calls == 1
    assert merger.calls == []
    assert "unreviewed.txt" in store.load_patch(run.id)
    workspace = Path(run.workspace_path)
    assert run.reviewed_tree_sha == git(workspace, "rev-parse", "HEAD^{tree}").strip()


def test_attended_red_ci_still_stops_for_a_person(tmp_path: Path, source_repo: Path) -> None:
    publisher = LocalPublisher()
    controller, _ = _controller(
        _config(tmp_path), publisher=publisher, observer=Observer([_failed_ci("INFRASTRUCTURE")])
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "not repairable" in run.failure_reason
    assert publisher.flags == []


def test_unattended_used_up_attempts_publish_the_work_as_it_is(
    tmp_path: Path, source_repo: Path
) -> None:
    publisher = LocalPublisher()
    merger = Merger()
    observer = Observer()
    controller, _ = _controller(
        _unattended(_config(tmp_path, verify=["false"], max_total_attempts=2)),
        publisher=publisher,
        merger=merger,
        observer=observer,
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.needs_look == ["implementation attempt budget exhausted after 2 attempt(s)"]
    assert publisher.calls == 1
    assert run.pull_request_url is not None
    assert (
        run.reviewed_tree_sha == git(Path(run.workspace_path), "rev-parse", "HEAD^{tree}").strip()
    )
    assert observer.calls == 1
    assert publisher.flags == [run.needs_look]
    assert merger.calls == []


def test_unattended_used_up_ci_repairs_keep_the_published_head(
    tmp_path: Path, source_repo: Path
) -> None:
    publisher = LocalPublisher()
    merger = Merger()
    config = _unattended(_config(tmp_path, verify=["test ! -e .factory-red"]))
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        repair = request.repair_context
        if isinstance(repair, RepairContext) and repair.trigger is AttemptTrigger.CI:
            assert request.workspace_path is not None
            (Path(request.workspace_path) / ".factory-red").write_text("red\n")
        return default_runtime.run(request)

    controller, _ = _controller(
        config,
        runtime=FakeAgentRuntime(implementer=implementer),
        publisher=publisher,
        merger=merger,
        observer=Observer([_failed_ci()]),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert run.needs_look == ["CI repair budget exhausted after 2 attempt(s)"]
    assert publisher.calls == 1
    assert publisher.flags == [run.needs_look]
    assert merger.calls == []


def test_unattended_labelling_error_does_not_stop_the_run(
    tmp_path: Path, source_repo: Path
) -> None:
    class FailingFlagPublisher(LocalPublisher):
        def flag_needs_look(self, **kwargs) -> None:
            raise GitHubError("label denied")

    merger = Merger()
    controller, _ = _controller(
        _unattended(_config(tmp_path)),
        publisher=FailingFlagPublisher(),
        merger=merger,
        observer=Observer([CIReport(overall="CANCELLED")]),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert merger.calls == []


def _ineligible_triage(request: AgentRequest) -> AgentResult:
    result = FakeAgentRuntime().run(request)
    assert result.triage_result is not None
    return result.model_copy(
        update={
            "triage_result": result.triage_result.model_copy(update={"factory_eligible": False})
        }
    )


def _undecided_planner(request: AgentRequest) -> AgentResult:
    result = FakeAgentRuntime().run(request)
    assert result.execution_plan is not None
    plan = result.execution_plan.model_copy(
        update={"unresolved_decisions": ["Choice between SQLite and PostgreSQL."]}
    )
    return result.model_copy(update={"execution_plan": plan})


def _security_reviewer(request: AgentRequest) -> AgentResult:
    return AgentResult(
        role=AgentRole.REVIEWER,
        success=True,
        review_report=ReviewReport(
            approved=False,
            blocking_findings=[
                ReviewFindingDraft(
                    category=ReviewFindingCategory.SECURITY,
                    message="Security defect.",
                    locations=[
                        ReviewSourceLocation(path="FACTORY_NOTES.md", start_line=1, end_line=1)
                    ],
                )
            ],
        ),
    )


@pytest.mark.parametrize(
    ("hooks", "reason"),
    [
        ({"triage": triage_hook(Complexity.L1, Risk.R2)}, "risk R2 requires human approval"),
        ({"triage": _ineligible_triage}, "triage marked this work item ineligible"),
        ({"planner": _undecided_planner}, "unresolved"),
        ({"reviewer": _security_reviewer}, "accepted beyond the review policy"),
    ],
)
def test_unattended_skipped_gate_labels_the_pull_request_and_blocks_merge(
    tmp_path: Path, source_repo: Path, hooks: dict[str, Callable[..., AgentResult]], reason: str
) -> None:
    config = _unattended(_config(tmp_path))
    config.review.max_rounds = 1
    publisher = LocalPublisher()
    merger = Merger()
    controller, _ = _controller(
        config, runtime=FakeAgentRuntime(**hooks), publisher=publisher, merger=merger
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert any(reason in item for item in run.needs_look), run.needs_look
    assert publisher.flags == [run.needs_look]
    assert merger.calls == []


@pytest.mark.parametrize("crash_at", ["publish", "ci"])
def test_unattended_work_published_as_it_is_resumes_to_done(
    tmp_path: Path, source_repo: Path, crash_at: str
) -> None:
    publisher = LocalPublisher(crash=crash_at == "publish")
    observer = Observer(crash=crash_at == "ci")
    merger = Merger()
    controller, store = _controller(
        _unattended(_config(tmp_path, verify=["false"], max_total_attempts=2)),
        publisher=publisher,
        observer=observer,
        merger=merger,
    )
    item = work_item()
    with pytest.raises(KeyboardInterrupt):
        controller.run(item, source_repo, run_id="as-is")

    recovered = controller.resume("as-is", source_repo)

    assert recovered.state is WorkflowState.DONE, recovered.failure_reason
    assert recovered.needs_look == ["implementation attempt budget exhausted after 2 attempt(s)"]
    assert publisher.flags == [recovered.needs_look]
    assert merger.calls == []


def test_unattended_resume_without_ci_still_labels_the_pull_request(
    tmp_path: Path, source_repo: Path
) -> None:
    class CrashingFlagPublisher(LocalPublisher):
        def flag_needs_look(self, **kwargs) -> None:
            if not self.flags:
                self.flags.append([])
                raise KeyboardInterrupt
            super().flag_needs_look(**kwargs)

    config = _unattended(_config(tmp_path, verify=["false"], max_total_attempts=2))
    config.ci.enabled = False
    publisher = CrashingFlagPublisher()
    controller, store = _controller(config, publisher=publisher)
    item = work_item()
    with pytest.raises(KeyboardInterrupt):
        controller.run(item, source_repo, run_id="no-ci")
    assert store.load_run("no-ci").state is WorkflowState.PR_CREATED

    recovered = controller.resume("no-ci", source_repo)

    assert recovered.state is WorkflowState.DONE, recovered.failure_reason
    assert publisher.flags == [[], recovered.needs_look]


def test_unattended_review_impasse_an_attended_run_would_stop_on_is_labelled(
    tmp_path: Path, source_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[
                    ReviewFindingDraft(
                        category=ReviewFindingCategory.CORRECTNESS,
                        message="Defect.",
                        locations=[
                            ReviewSourceLocation(path="FACTORY_NOTES.md", start_line=1, end_line=1)
                        ],
                    )
                ],
                prior_finding_dispositions=[
                    ReviewFindingDisposition(
                        finding_id=finding.id,
                        status=ReviewDispositionStatus.UNRESOLVED,
                        rationale="Still present at the cited location.",
                    )
                    for finding in request.prior_review_findings
                ],
            ),
        )

    original = WorkflowController._apply_review_report

    def replacement_loop(self, *args, **kwargs):
        run, report, impasse = original(self, *args, **kwargs)
        if impasse is not None:
            impasse = impasse.model_copy(update={"kind": ReviewImpasseKind.REPLACEMENT_LOOP})
        return run, report, impasse

    monkeypatch.setattr(WorkflowController, "_apply_review_report", replacement_loop)
    publisher = LocalPublisher()
    merger = Merger()
    controller, _ = _controller(
        _unattended(_config(tmp_path)),
        runtime=FakeAgentRuntime(reviewer=reviewer),
        publisher=publisher,
        merger=merger,
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE, run.failure_reason
    assert "review impasse REPLACEMENT_LOOP was accepted" in run.needs_look
    assert merger.calls == []
