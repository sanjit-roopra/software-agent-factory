from pathlib import Path

import pytest
from factory_testing import build_config, git, work_item

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.delivery import DeliveryTarget
from software_agent_factory.github import GitHubError, UnexpectedRepositoryError
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    CICheckEvidence,
    CIReport,
    FactoryRun,
    ReviewReport,
    WorkflowState,
)
from software_agent_factory.publishing import MergeResult, PublishResult
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController, delivery_policy_fingerprint
from software_agent_factory.workspace import GitWorktreeWorkspace, WorkspaceLockError


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


class LocalPublisher:
    def __init__(self, *, crash: bool = False) -> None:
        self.calls = 0
        self.crash = crash
        self.parents: list[str] = []
        self.commits: list[str] = []

    def resolve_base_branch(self, source_repo: Path) -> str:
        return "main"

    def publish(self, **kwargs) -> PublishResult:
        self.calls += 1
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt
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


def _config(tmp_path: Path, *, merge: bool = True) -> FactoryConfig:
    payload = build_config(
        tmp_path / "data",
        verify=["true"],
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
    assert "Reviewer approval" in recovered.failure_reason
    assert merger.calls == []


def test_reviewer_rejection_blocks_every_pr_and_merge(tmp_path: Path, source_repo: Path) -> None:
    def reject(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=False, findings=["Not correct yet"]),
        )

    publisher = LocalPublisher()
    merger = Merger()
    runtime = FakeAgentRuntime(reviewer=reject)
    controller, _ = _controller(
        _config(tmp_path), runtime=runtime, publisher=publisher, merger=merger
    )
    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert publisher.calls == 0
    assert merger.calls == []


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
