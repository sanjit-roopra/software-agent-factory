"""Retry CI repair from the dashboard after CI stopped a run (ADR-061)."""

from collections.abc import Callable
from pathlib import Path

import pytest
from factory_testing import build_config, git, work_item

from software_agent_factory import run_reopen
from software_agent_factory.agents import FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.delivery import DeliveryTarget
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.github import GitHubError, PullRequestState
from software_agent_factory.halt_contexts import build_ci_repair_retry_context
from software_agent_factory.models import (
    AttemptBudget,
    CICheckEvidence,
    CIReport,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    HaltReasonCode,
    ResumeClassification,
    WorkflowState,
    utc_now,
)
from software_agent_factory.publishing import MergeResult, PublishResult
from software_agent_factory.resume_classes import RESUME_CLASSES, is_valid_ci_repair_retry_context
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

RETRY = ResumeClassification.CI_REPAIR_RETRY
PR_URL = "https://github.com/acme/repo/pull/42"


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


def _failed(category: str = "TEST_FAILURE") -> CIReport:
    return CIReport(
        overall="FAIL",
        checks=[CICheckEvidence(name="tests", status="FAIL", failure_category=category)],
    )


class Publisher:
    def __init__(self) -> None:
        self.calls = 0

    def resolve_base_branch(self, source_repo: Path) -> str:
        return "main"

    def publish(self, **kwargs: object) -> PublishResult:
        self.calls += 1
        path = Path(str(kwargs["workspace_path"]))
        git(path, "add", "-A")
        if git(path, "diff", "--cached", "--name-only").strip():
            git(path, "commit", "-m", str(kwargs["commit_message"]))
        head = git(path, "rev-parse", "HEAD").strip()
        record = kwargs["record_commit"]
        assert callable(record)
        record(head)
        return PublishResult(
            commit_sha=head,
            base_branch="main",
            pull_request_url=PR_URL,
            created_pull_request=kwargs["existing_pull_request_url"] is None,
        )

    def flag_needs_look(self, **kwargs: object) -> None:
        pass


class Observer:
    """CI results in order; the last one repeats."""

    def __init__(self, reports: list[CIReport]) -> None:
        self.reports = reports
        self.calls = 0

    def observe(self, **kwargs: object) -> CIReport:
        self.calls += 1
        report = self.reports[min(self.calls - 1, len(self.reports) - 1)]
        return report.model_copy(update={"repair_attempts_used": kwargs["repair_attempts_used"]})


class Merger:
    def __init__(self) -> None:
        self.calls = 0

    def validate_repository(self, repo_path: Path) -> str:
        return "acme/repo"

    def merge(self, **kwargs: object) -> MergeResult:
        self.calls += 1
        return MergeResult(commit_sha="f" * 40, pull_request_url=PR_URL)


class GitHub:
    """The pull request as GitHub reports it: open on the run's head unless a test says else."""

    def __init__(self) -> None:
        self.state = "OPEN"
        self.head: str | None = None
        self.store: FileRunStore | None = None
        self.run_id = ""

    def get_pull_request(self, repo_path: Path, pr: str, **_: object) -> PullRequestState:
        assert pr == PR_URL
        assert self.store is not None
        head = self.head or self.store.load_run(self.run_id).commit_sha or ""
        return PullRequestState(url=PR_URL, state=self.state, head_ref_oid=head)


class Rig:
    def __init__(self, tmp_path: Path, reports: list[CIReport]) -> None:
        payload = build_config(
            tmp_path / "data",
            verify=["true"],
            pull_request={"enabled": True, "draft": False, "base_branch": "main"},
            ci={"enabled": True, "repair_attempts": 1},
        ).model_dump(mode="json")
        payload["merge"] = {
            "enabled": True,
            "allowed_repositories": ["acme/repo"],
            "required_checks": ["quality"],
        }
        self.config = FactoryConfig.model_validate(payload)
        self.store = FileRunStore(self.config.data_dir)
        self.publisher = Publisher()
        self.observer = Observer(reports)
        self.merger = Merger()
        self.github = GitHub()
        self.github.store = self.store
        self.controller = WorkflowController(
            self.config,
            self.store,
            FakeAgentRuntime(),
            publisher=self.publisher,
            ci_observer=self.observer,
            merger=self.merger,
            github_client=self.github,  # type: ignore[arg-type]
            delivery_base_resolver=lambda repo, expected: DeliveryTarget(
                expected, "github.com", git(repo, "rev-parse", "HEAD").strip()
            ),
        )

    def halt(self, source_repo: Path) -> FactoryRun:
        run = self.controller.run(work_item(), source_repo)
        self.github.run_id = run.id
        return run

    def retry(self, run: FactoryRun) -> None:
        assert run.escalation is not None
        context = run.escalation.ci_repair_retry_context
        assert context is not None
        request = DashboardResumeRequest(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            context_fingerprint=context.context_fingerprint,
            action=RETRY,
        )
        assert self.store.create_dashboard_request(run.id, request)
        assert ingest_dashboard_request(run, self.store, self.config, utc_now()) is not None


def _ci_repairs(run: FactoryRun) -> int:
    return sum(1 for record in run.attempt_records if record.budget is AttemptBudget.CI_REPAIR)


def test_a_ci_repair_budget_halt_offers_a_ci_repair_retry(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path, [_failed()])

    halted = rig.halt(source_repo)

    assert halted.state is WorkflowState.NEEDS_HUMAN
    assert halted.failure_reason == "CI repair budget exhausted after 1 attempt(s)"
    assert halted.escalation is not None
    assert halted.escalation.reason_code == HaltReasonCode.CI_INTERVENTION
    assert halted.escalation.resume_classification is RETRY
    context = halted.escalation.ci_repair_retry_context
    assert context is not None
    assert (context.pull_request_url, context.head_sha) == (PR_URL, halted.commit_sha)
    assert (context.failed_checks, context.repair_attempts_used) == (["tests"], 1)
    assert context.timed_out is False
    assert is_valid_ci_repair_retry_context(context, halted.id, halted.escalation.episode_id)


def test_a_retry_runs_one_more_ci_repair_and_can_merge(tmp_path: Path, source_repo: Path) -> None:
    rig = Rig(tmp_path, [_failed(), _failed(), CIReport(overall="PASS")])
    halted = rig.halt(source_repo)
    rig.retry(halted)

    retried = rig.controller.reopen(halted.id, source_repo)

    assert retried.state is WorkflowState.DONE, retried.failure_reason
    assert _ci_repairs(retried) == 2
    assert retried.attempt_grants == {AttemptBudget.CI_REPAIR: 1}
    assert rig.observer.calls == 3
    assert rig.merger.calls == 1
    assert retried.escalation is not None
    assert retried.escalation.status is EscalationStatus.RESUMED


def test_a_retried_repair_that_fails_halts_again_for_another_retry(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path, [_failed()])
    halted = rig.halt(source_repo)
    rig.retry(halted)

    again = rig.controller.reopen(halted.id, source_repo)

    assert again.state is WorkflowState.NEEDS_HUMAN
    assert again.failure_reason == "CI repair budget exhausted after 2 attempt(s)"
    assert _ci_repairs(again) == 2
    assert again.escalation is not None
    assert again.escalation.resume_classification is RETRY
    assert again.escalation.episode_number == 2


def test_a_ci_wait_that_ran_out_watches_ci_again_without_a_grant(
    tmp_path: Path, source_repo: Path
) -> None:
    pending = CIReport(overall="PENDING", timed_out=True)
    rig = Rig(tmp_path, [pending, CIReport(overall="PASS")])
    halted = rig.halt(source_repo)
    assert halted.escalation is not None
    context = halted.escalation.ci_repair_retry_context
    assert context is not None
    assert context.timed_out is True
    assert RESUME_CLASSES[RETRY].resume_state(halted.escalation) is WorkflowState.CI_RUNNING
    rig.retry(halted)

    retried = rig.controller.reopen(halted.id, source_repo)

    assert retried.state is WorkflowState.DONE, retried.failure_reason
    assert retried.attempt_grants == {}
    assert _ci_repairs(retried) == 0
    assert rig.observer.calls == 2


@pytest.mark.parametrize(
    ("state", "head", "refusal"),
    [
        ("OPEN", "0" * 40, "pull request head changed"),
        ("CLOSED", None, "pull request is CLOSED"),
        ("MERGED", None, "pull request is MERGED"),
    ],
    ids=["new-head", "closed", "merged"],
)
def test_a_retry_for_a_changed_pull_request_is_stale_and_runs_nothing(
    tmp_path: Path, source_repo: Path, state: str, head: str | None, refusal: str
) -> None:
    rig = Rig(tmp_path, [_failed()])
    halted = rig.halt(source_repo)
    rig.retry(halted)
    rig.github.state, rig.github.head = state, head

    refused = rig.controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert refusal in (refused.failure_reason or "")
    assert refused.escalation is not None
    assert refused.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert _ci_repairs(refused) == 1
    assert refused.attempt_grants == {}


def test_a_retry_for_a_run_with_a_new_head_cannot_reopen_it(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path, [_failed()])
    halted = rig.halt(source_repo)
    rig.retry(halted)
    rig.github.head = halted.commit_sha
    moved = rig.store.load_run(halted.id).model_copy(update={"commit_sha": "0" * 40})
    rig.store.save_run(moved)

    refused = rig.controller.reopen(halted.id, source_repo)

    assert refused.state is WorkflowState.NEEDS_HUMAN
    assert "no longer matches its CI repair retry context" in (refused.failure_reason or "")
    assert _ci_repairs(refused) == 1


def test_a_cancelled_ci_run_or_a_merged_pull_request_is_not_resumable(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    config = build_config(tmp_path / "data")
    run = FactoryRun(
        id="run-x",
        work_item_id="task-x",
        state=WorkflowState.CI_DIAGNOSIS,
        pull_request_url=PR_URL,
        commit_sha="a" * 40,
    )
    store.save_run(run)
    store.save_artifact(run.id, CIReport(overall="CANCELLED"))
    assert build_ci_repair_retry_context(run, store, config, "episode-1") is None

    store.save_artifact(run.id, _failed("INFRA_FAILURE"))
    assert build_ci_repair_retry_context(run, store, config, "episode-1") is not None
    merged = run.model_copy(update={"merge_commit_sha": "b" * 40})
    assert build_ci_repair_retry_context(merged, store, config, "episode-1") is None


@pytest.mark.parametrize(
    "reason",
    [
        "CI repair budget exhausted after 2 attempt(s)",
        "CI checks were still pending after the configured wait budget",
    ],
)
def test_ci_halts_are_ci_interventions_with_the_retry_class(reason: str) -> None:
    run = FactoryRun(
        id="run-x", work_item_id="task-x", state=WorkflowState.NEEDS_HUMAN, failure_reason=reason
    )

    classification, code, _, _ = classify_halt_reason(run)

    assert (classification, code) == (RETRY, HaltReasonCode.CI_INTERVENTION)


def _with_escalation(run: FactoryRun, **update: object) -> FactoryRun:
    assert run.escalation is not None
    return run.model_copy(update={"escalation": run.escalation.model_copy(update=update)})


def _without_context(run: FactoryRun) -> FactoryRun:
    return _with_escalation(run, ci_repair_retry_context=None)


def _with_other_receipt_fingerprint(run: FactoryRun) -> FactoryRun:
    assert run.escalation is not None
    receipts = [
        receipt.model_copy(update={"ci_repair_retry_context_fingerprint": "d" * 64})
        for receipt in run.escalation.accepted_replies
    ]
    return _with_escalation(run, accepted_replies=receipts)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_without_context, "missing or invalid CI repair retry context"),
        (_with_other_receipt_fingerprint, "receipt fingerprint does not match"),
    ],
    ids=["no-context", "other-receipt"],
)
def test_a_retry_that_no_longer_matches_its_context_cannot_reopen_the_run(
    tmp_path: Path,
    source_repo: Path,
    tamper: Callable[[FactoryRun], FactoryRun],
    message: str,
) -> None:
    rig = Rig(tmp_path, [_failed()])
    halted = rig.halt(source_repo)
    rig.retry(halted)
    reopening = tamper(rig.store.load_run(halted.id))

    with pytest.raises(ValueError, match=message):
        run_reopen.transition_reopened(rig.controller, reopening)


class BrokenGitHub(GitHub):
    def get_pull_request(self, repo_path: Path, pr: str, **_: object) -> PullRequestState:
        raise GitHubError("gh is down")


@pytest.mark.parametrize(
    ("github", "refusal"),
    [(None, "cannot read the pull request"), (BrokenGitHub(), "could not read the pull request")],
    ids=["no-client", "gh-error"],
)
def test_a_retry_that_cannot_read_the_pull_request_runs_nothing(
    tmp_path: Path, source_repo: Path, github: GitHub | None, refusal: str
) -> None:
    rig = Rig(tmp_path, [_failed()])
    halted = rig.halt(source_repo)
    rig.retry(halted)
    rig.controller._github = github  # type: ignore[assignment]

    refused = rig.controller.reopen(halted.id, source_repo)

    assert refusal in (refused.failure_reason or "")
    assert _ci_repairs(refused) == 1


def test_a_run_without_a_ci_report_gets_no_retry_context(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = FactoryRun(
        id="run-y",
        work_item_id="task-y",
        state=WorkflowState.CI_RUNNING,
        pull_request_url=PR_URL,
        commit_sha="a" * 40,
    )
    store.save_run(run)

    assert build_ci_repair_retry_context(run, store, build_config(tmp_path), "ep-1") is None
