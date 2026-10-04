"""Tests for software_agent_factory.workflow.WorkflowController.

Test repositories are created fresh under pytest's ``tmp_path`` with local
(not global) Git identity configuration, ``commit.gpgsign`` disabled, and
global/system Git config suppressed via environment variables (mirroring
tests/test_workspace.py) so these tests never depend on the developer
machine's global Git configuration, commit signing setup, or hooks.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Callable

import pytest
from pydantic import ValidationError

from software_agent_factory.agent_artifact import parse_agent_artifact
from software_agent_factory.agents import AgentHook, AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.governance import (
    CheckPhase,
    RepositoryVerificationResult,
    VerificationFailureKind,
)
from software_agent_factory.models import (
    UNRESOLVED_DECISIONS_HALT_REASON,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    Complexity,
    ContextTier,
    DashboardResumeRequest,
    DependencyEcosystem,
    EscalationStatus,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    MutationReport,
    MutationStatus,
    PlanDecisionAnswer,
    PlanStep,
    RepairContext,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    RepositoryDependency,
    RepositoryProfile,
    ResumeClassification,
    ReviewAcceptance,
    ReviewAcceptanceReason,
    ReviewDispositionStatus,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingDisposition,
    ReviewFindingDraft,
    ReviewFindingOrigin,
    ReviewImpasse,
    ReviewImpasseKind,
    ReviewLedger,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
    RiskRationale,
    RunLease,
    Specification,
    ToolchainInventory,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from software_agent_factory.observability import _compute_aggregate_metrics
from software_agent_factory.prompts import build_prompt
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import ARTIFACT_FILENAMES, FileRunStore
from software_agent_factory.verification import DeterministicVerifier
from software_agent_factory.workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    WorkflowController,
    _RunContext,
    is_run_finished,
)
from software_agent_factory.workspace import GitWorktreeWorkspace, WorkspaceError


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never depend on global Git config, signing or hooks."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def _config(
    data_dir: Path,
    *,
    verify: list[str] | None = None,
    same_model_attempts: int = 2,
    max_total_attempts: int = 6,
    polish_enabled: bool = False,
) -> FactoryConfig:
    return FactoryConfig.model_validate(
        {
            "factory": {
                "data_dir": str(data_dir),
                "retries": {
                    "same_model_attempts": same_model_attempts,
                    "max_total_attempts": max_total_attempts,
                },
            },
            "models": {
                "triage": {"model": "claude-sonnet-5", "reasoning": "medium"},
                "planner": {"model": "claude-opus-5", "reasoning": "high"},
                "workers": {
                    "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                    "L1": {"model": "claude-sonnet-5", "reasoning": "medium"},
                    "L2": {"model": "claude-opus-5", "reasoning": "high"},
                    "L3": {"model": "claude-opus-5", "reasoning": "high"},
                },
                "tester": {"model": "claude-sonnet-5", "reasoning": "high"},
                "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
            },
            "model_profiles": {
                "economy": {
                    "triage": {"model": "gpt-5.6-luna", "reasoning": "medium"},
                    "planner": {"model": "gpt-5.6-terra", "reasoning": "high"},
                    "workers": {
                        "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                        "L1": {"model": "gemini-3.8-flash", "reasoning": "medium"},
                        "L2": {"model": "claude-sonnet-5", "reasoning": "high"},
                        "L3": {"model": "claude-opus-5", "reasoning": "high"},
                    },
                    "tester": {"model": "gemini-3.8-flash", "reasoning": "high"},
                    "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
                }
            },
            "repository": {
                "branch_prefix": "factory/",
                "command_timeout_seconds": 30,
                "commands": {"install": [], "verify": verify or [], "build": []},
            },
            "polish": {"enabled": polish_enabled},
            "risk": {
                "R0": {"human_approval": False},
                "R1": {"human_approval": False},
                "R2": {"human_approval": True},
                "R3": {"human_approval": True},
            },
        }
    )


def _work_item(work_item_id: str = "WI-1") -> WorkItem:
    return WorkItem(
        id=work_item_id,
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
    )


def _review_finding(
    message: str,
    *,
    path: str = "FACTORY_NOTES.md",
    line: int = 1,
    category: ReviewFindingCategory = ReviewFindingCategory.CORRECTNESS,
) -> ReviewFindingDraft:
    return ReviewFindingDraft(
        category=category,
        message=message,
        locations=[ReviewSourceLocation(path=path, start_line=line, end_line=line)],
    )


def _resolve_prior(
    request: AgentRequest,
    status: ReviewDispositionStatus = ReviewDispositionStatus.RESOLVED,
) -> list[ReviewFindingDisposition]:
    return [
        ReviewFindingDisposition(
            finding_id=finding.id,
            status=status,
            rationale="Checked the current implementation against the cited location.",
        )
        for finding in request.prior_review_findings
    ]


def _triage_hook(complexity: Complexity, risk: Risk, *, needs_research: bool = False):
    def hook(request: AgentRequest) -> AgentResult:
        rationale = (
            RiskRationale(
                intended_outcome="Update deployment credentials safely.",
                sensitive_boundary="Production deployment configuration.",
                necessity="Task requires modifying production deployment keys.",
                credible_scenario="Misconfiguration could cause service outage.",
                known_mitigations=["Validate syntax before deployment."],
                residual_risk="Manual operator review required before release.",
            )
            if risk in {Risk.R2, Risk.R3}
            else None
        )
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=complexity,
                risk=risk,
                needs_research=needs_research,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
                risk_rationale=rationale,
            ),
        )

    return hook


class RecordingRuntime:
    def __init__(self, delegate: FakeAgentRuntime) -> None:
        self._delegate = delegate
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return self._delegate.run(request)


# -- transition matrix -------------------------------------------------------


def test_transition_table_covers_declared_states_only() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(WorkflowState)
    assert TERMINAL_STATES == {
        WorkflowState.DONE,
        WorkflowState.NEEDS_HUMAN,
        WorkflowState.FAILED,
    }
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()

    # PR_READY is not terminal: a pull-request-enabled run continues from it.
    assert WorkflowState.PR_CREATED in ALLOWED_TRANSITIONS[WorkflowState.PR_READY]

    # Every non-terminal state can escalate to a human or fail operationally.
    for state, allowed in ALLOWED_TRANSITIONS.items():
        if state in TERMINAL_STATES:
            continue
        assert WorkflowState.NEEDS_HUMAN in allowed, state
        assert WorkflowState.FAILED in allowed, state


def test_exhaustive_transition_matrix_matches_declared_table(data_dir: Path) -> None:
    """Every State x State pair either succeeds (if declared allowed) or
    raises TransitionError (if not), with no exceptions."""
    controller = WorkflowController(_config(data_dir), FileRunStore(data_dir), FakeAgentRuntime())
    all_states = list(WorkflowState)

    for from_state, to_state in itertools.product(all_states, all_states):
        run = FactoryRun(id=f"RUN-{from_state}-{to_state}", work_item_id="WI-X", state=from_state)
        allowed = to_state in ALLOWED_TRANSITIONS[from_state]
        if allowed:
            result = controller.transition(run, to_state)
            assert result.state is to_state
            if to_state in TERMINAL_STATES:
                assert result.completed_at is not None
        else:
            with pytest.raises(TransitionError):
                controller.transition(run, to_state)


def test_invalid_transition_from_terminal_state_is_rejected(data_dir: Path) -> None:
    controller = WorkflowController(_config(data_dir), FileRunStore(data_dir), FakeAgentRuntime())
    run = FactoryRun(id="RUN-terminal", work_item_id="WI-X", state=WorkflowState.PR_READY)

    with pytest.raises(TransitionError):
        controller.transition(run, WorkflowState.TRIAGING)


def test_invalid_repository_produces_persisted_failed_run(tmp_path: Path, data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime())

    run = controller.run(_work_item(), tmp_path / "missing-repository")

    assert run.state is WorkflowState.FAILED
    assert run.completed_at is not None
    assert "initialize workspace" in (run.failure_reason or "")
    assert store.load_run(run.id) == run


# -- happy path ---------------------------------------------------------------


def test_happy_path_reaches_pr_ready_and_persists_all_artifacts(
    source_repo: Path, data_dir: Path
) -> None:
    head_before = _git(source_repo, "rev-parse", "HEAD").strip()

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.completed_at is not None
    assert len(run.attempt_records) == 1
    assert run.attempt_records[0].outcome == "succeeded"

    # Source repo must remain completely untouched.
    assert _git(source_repo, "status", "--porcelain") == ""
    assert _git(source_repo, "rev-parse", "HEAD").strip() == head_before

    # Persisted artifacts exist for every stage.
    run_dir = store.runs_dir / run.id
    for filename in (
        "run.json",
        "work-item.json",
        "repository-profile.json",
        "toolchain-inventory.json",
        "triage.json",
        "specification.json",
        "execution-plan.json",
        "change-set.json",
        "patch.diff",
        "verification.json",
        "review.json",
    ):
        assert (run_dir / filename).exists(), f"missing {filename}"

    change_set = store.load_artifact(run.id, ChangeSet)
    assert "FACTORY_NOTES.md" in change_set.changed_files

    patch_text = (run_dir / "patch.diff").read_text(encoding="utf-8")
    assert "FACTORY_NOTES.md" in patch_text

    # The new file exists inside the isolated workspace, not the source repo.
    assert run.workspace_path is not None
    assert (Path(run.workspace_path) / "FACTORY_NOTES.md").exists()
    assert not (source_repo / "FACTORY_NOTES.md").exists()


def test_fake_agent_lying_about_changed_files_cannot_affect_persisted_evidence(
    source_repo: Path, data_dir: Path
) -> None:
    def lying_implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "real_file.txt").write_text("real content\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="lied about the changed files",
                changed_files=["totally_fake_file.txt", "another_lie.py"],
            ),
        )

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(implementer=lying_implementer))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    change_set = store.load_artifact(run.id, ChangeSet)
    assert change_set.changed_files == ["real_file.txt"]
    assert "totally_fake_file.txt" not in change_set.changed_files


def test_all_repository_reading_roles_receive_the_exact_workspace_path(
    source_repo: Path, data_dir: Path
) -> None:
    # needs_research=True no longer adds a call: one planner call follows triage (ADR-035).
    runtime = RecordingRuntime(
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R1, needs_research=True))
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, runtime)

    run = controller.run(_work_item("WI-workspace-path"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.workspace_path is not None
    assert run.workspace_path != str(source_repo)

    expected_roles = [
        AgentRole.TRIAGE,
        AgentRole.PLANNER,
        AgentRole.IMPLEMENTER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ]
    assert [request.role for request in runtime.requests] == expected_roles
    assert all(request.workspace_path == run.workspace_path for request in runtime.requests)
    assert [record.role for record in run.invocation_records] == expected_roles
    assert [record.invocation_number for record in run.invocation_records] == list(
        range(1, len(expected_roles) + 1)
    )
    assert all(record.context_tier.value == "default" for record in run.invocation_records)
    assert run.invocation_records[2].budget is AttemptBudget.IMPLEMENTATION
    assert run.invocation_records[3].attempt_number == 1
    assert run.invocation_records[4].attempt_number == 1
    assert run.attempt_records[0].invocation_number == 3
    assert store.load_run(run.id).invocation_records == run.invocation_records


def test_planner_retries_once_after_malformed_execution_plan(
    source_repo: Path,
    data_dir: Path,
) -> None:
    calls = 0
    requests: list[AgentRequest] = []
    active_invocations = []
    default_runtime = FakeAgentRuntime()
    store = FileRunStore(data_dir)

    def planner(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        requests.append(request)
        active_invocations.append(store.list_runs()[0].active_invocation)
        if calls == 1:
            return AgentResult(
                role=AgentRole.PLANNER,
                success=False,
                failure_reason=(
                    "PLANNER response did not validate as PlanningResult: "
                    "steps.0.goal: Field required; expected_scope: Input should be a valid "
                    "dictionary"
                ),
            )
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        store,
        FakeAgentRuntime(planner=planner),
    ).run(_work_item("WI-planner-schema-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    planner_invocations = [
        record for record in run.invocation_records if record.role is AgentRole.PLANNER
    ]
    assert [record.success for record in planner_invocations] == [False, True]
    assert [record.attempt_number for record in planner_invocations] == [1, 2]
    assert requests[0].repair_context is None
    assert isinstance(requests[1].repair_context, str)
    assert "steps.0.goal: Field required" in requests[1].repair_context
    assert "expected_scope: Input should be a valid dictionary" in requests[1].repair_context
    assert "stdout=" not in requests[1].repair_context
    assert "Return one complete PlanningResult JSON object" in requests[1].repair_context
    assert all(active is not None for active in active_invocations)
    assert [active.attempt_number for active in active_invocations if active is not None] == [1, 2]
    assert run.active_invocation is None


@pytest.mark.parametrize(
    ("role", "hook", "artifact"),
    [
        (AgentRole.TRIAGE, "triage", "TriageResult"),
    ],
)
def test_structural_retry_prompt_names_the_failure_for_early_roles(
    source_repo: Path,
    data_dir: Path,
    role: AgentRole,
    hook: str,
    artifact: str,
) -> None:
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()
    reason = f"{role.value} response did not validate as {artifact}: confidence: Field required"

    def failing_first(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if len(requests) == 1:
            return AgentResult(role=role, success=False, failure_reason=reason)
        return default_runtime.run(request)

    hooks: dict[str, AgentHook] = {hook: failing_first}
    runtime = FakeAgentRuntime(**hooks)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        runtime,
    ).run(_work_item(f"WI-{hook}-structural-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(requests) == 2
    assert requests[0].repair_context is None
    retry_prompt = build_prompt(requests[1])
    assert "Previous output rejection" in retry_prompt
    assert "confidence: Field required" in retry_prompt


def test_blank_plan_summary_takes_the_structural_retry(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()
    blank_plan = (
        '{"specification":{"problem":"Fix it.","confidence":0.9},'
        '"execution_plan":{"summary":"  ","steps":[],"expected_scope":'
        '{"modules":["src"],"estimated_files_min":1,"estimated_files_max":1}}}'
    )

    def planner(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if len(requests) > 1:
            return default_runtime.run(request)
        try:
            parse_agent_artifact(AgentRole.PLANNER, text=blank_plan)
        except ValueError as exc:
            return AgentResult(role=AgentRole.PLANNER, success=False, failure_reason=str(exc))
        raise AssertionError("a blank summary must not validate")

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(planner=planner),
    ).run(_work_item("WI-blank-plan-summary"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(requests) == 2
    assert isinstance(requests[1].repair_context, str)
    assert "summary" in requests[1].repair_context
    assert "must not be blank" in requests[1].repair_context


def test_synthesized_artifacts_skip_blank_human_criteria(data_dir: Path, source_repo: Path) -> None:
    controller = WorkflowController(_config(data_dir), FileRunStore(data_dir), FakeAgentRuntime())
    item = WorkItem(
        id="WI-blank-criteria",
        title="Reject empty names",
        description="Return HTTP 400.",
        acceptance_criteria=["", "Blank names return 400."],
        constraints=[" "],
    )

    specification = controller._synthesize_specification(item)
    plan = controller._synthesize_execution_plan(item, ())

    assert specification.acceptance_criteria == ["Blank names return 400."]
    assert specification.constraints == []
    assert plan.steps[0].validation == ["Blank names return 400."]


def test_planner_does_not_retry_non_schema_failure(
    source_repo: Path,
    data_dir: Path,
) -> None:
    calls = 0

    def planner(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        return AgentResult(
            role=AgentRole.PLANNER,
            success=False,
            failure_reason="PLANNER: copilot exited with code 1",
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(planner=planner),
    ).run(_work_item("WI-planner-terminal-failure"), source_repo)

    assert run.state is WorkflowState.FAILED
    assert calls == 1


def test_tester_retries_typed_artifact_failure_without_spending_implementation_attempt(
    source_repo: Path,
    data_dir: Path,
) -> None:
    calls = 0
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def tester(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            return AgentResult(
                role=AgentRole.TESTER,
                success=False,
                failure_reason=(
                    "TESTER response did not contain a parseable JSON object for TestReport"
                ),
            )
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(tester=tester),
    ).run(_work_item("WI-tester-schema-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    tester_invocations = [
        record for record in run.invocation_records if record.role is AgentRole.TESTER
    ]
    assert [record.success for record in tester_invocations] == [False, True]
    assert [record.attempt_number for record in tester_invocations] == [1, 1]
    assert len(run.attempt_records) == 1
    assert requests[0].repair_context is None
    assert isinstance(requests[1].repair_context, str)
    assert "parseable JSON object for TestReport" in requests[1].repair_context
    assert "Return one complete TestReport JSON object" in requests[1].repair_context


def test_reviewer_retries_typed_artifact_failure_without_spending_implementation_attempt(
    source_repo: Path,
    data_dir: Path,
) -> None:
    calls = 0
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            return AgentResult(
                role=AgentRole.REVIEWER,
                success=False,
                failure_reason=("REVIEWER response did not contain a valid ReviewReport"),
            )
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-reviewer-schema-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    reviewer_invocations = [
        record for record in run.invocation_records if record.role is AgentRole.REVIEWER
    ]
    assert [record.success for record in reviewer_invocations] == [False, True]
    assert [record.attempt_number for record in reviewer_invocations] == [1, 1]
    assert len(run.attempt_records) == 1
    assert requests[0].repair_context is None
    assert isinstance(requests[1].repair_context, str)
    assert "did not contain a valid ReviewReport" in requests[1].repair_context
    assert "Return one complete ReviewReport JSON object" in requests[1].repair_context


def test_verification_workspace_mutation_requires_repair_before_review(
    source_repo: Path,
    data_dir: Path,
) -> None:
    class MutatingVerifier:
        def run(self, *args: object, **kwargs: object) -> RepositoryVerificationResult:
            workspace = Path(str(kwargs["cwd"]))
            (workspace / ".coverage").write_text("generated\n")
            return RepositoryVerificationResult(
                report=VerificationReport(passed=True, confidence=1.0),
                command_logs=(),
                failure_kind=None,
                failed_phase=None,
                failed_command=None,
            )

    implementer_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        implementer_requests.append(request)
        if len(implementer_requests) == 2:
            assert request.workspace_path is not None
            workspace = Path(request.workspace_path)
            (workspace / ".gitignore").write_text(".coverage\n")
            (workspace / ".coverage").unlink(missing_ok=True)
        return default_runtime.run(request)

    runtime = RecordingRuntime(FakeAgentRuntime(implementer=implementer))
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        FileRunStore(data_dir),
        runtime,
        repository_verifier=MutatingVerifier(),
    ).run(_work_item("WI-verification-mutation"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 2
    assert run.attempt_records[1].triggered_by is AttemptTrigger.VERIFICATION
    assert isinstance(implementer_requests[1].repair_context, RepairContext)
    assert "modified the repository" in implementer_requests[1].repair_context.summary
    assert ".coverage" in implementer_requests[1].repair_context.failures[1]
    tester_request = next(
        request for request in runtime.requests if request.role is AgentRole.TESTER
    )
    assert ".coverage" not in tester_request.changed_files
    assert ".gitignore" in tester_request.changed_files


def test_verification_generated_artifact_must_be_removed_not_only_ignored(
    source_repo: Path,
    data_dir: Path,
) -> None:
    class MutatingVerifier:
        def run(self, *args: object, **kwargs: object) -> RepositoryVerificationResult:
            workspace = Path(str(kwargs["cwd"]))
            (workspace / ".coverage").write_text("generated\n")
            return RepositoryVerificationResult(
                report=VerificationReport(passed=True, confidence=1.0),
                command_logs=(),
                failure_kind=None,
                failed_phase=None,
                failed_command=None,
            )

    calls = 0
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert request.workspace_path is not None
            (Path(request.workspace_path) / ".gitignore").write_text(".coverage\n")
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(implementer=implementer),
        repository_verifier=MutatingVerifier(),
    ).run(_work_item("WI-verification-artifact-retained"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 2
    assert "implementation attempt budget exhausted" in (run.failure_reason or "")


def test_non_default_context_tier_reaches_requests_and_persisted_records(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    models = config.models.model_copy(
        update={
            "triage": config.models.triage.model_copy(
                update={"context_tier": ContextTier.LONG_CONTEXT}
            ),
            "workers": {
                **config.models.workers,
                Complexity.L1: config.models.workers[Complexity.L1].model_copy(
                    update={"context_tier": ContextTier.LONG_CONTEXT}
                ),
            },
        }
    )
    config = config.model_copy(update={"models": models})
    runtime = RecordingRuntime(FakeAgentRuntime())

    run = WorkflowController(config, FileRunStore(data_dir), runtime).run(
        _work_item("WI-long-context"),
        source_repo,
    )

    triage_request = next(
        request for request in runtime.requests if request.role is AgentRole.TRIAGE
    )
    implementer_request = next(
        request for request in runtime.requests if request.role is AgentRole.IMPLEMENTER
    )
    assert triage_request.context_tier is ContextTier.LONG_CONTEXT
    assert implementer_request.context_tier is ContextTier.LONG_CONTEXT
    assert run.invocation_records[0].context_tier is ContextTier.LONG_CONTEXT
    assert run.attempt_records[0].context_tier is ContextTier.LONG_CONTEXT


def test_post_green_polish_is_bounded_and_reverified(source_repo: Path, data_dir: Path) -> None:
    runtime = RecordingRuntime(FakeAgentRuntime())
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    )

    run = controller.run(_work_item("WI-polish"), source_repo)

    assert run.state is WorkflowState.PR_READY
    # The polish attempt is the only extra agent invocation.
    assert [request.role for request in runtime.requests] == [
        AgentRole.TRIAGE,
        AgentRole.PLANNER,
        AgentRole.IMPLEMENTER,
        AgentRole.IMPLEMENTER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ]
    assert all(request.purpose is AgentPurpose.STANDARD for request in runtime.requests)
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    polish_request = runtime.requests[3]
    assert isinstance(polish_request.repair_context, RepairContext)
    assert polish_request.repair_context.trigger is AttemptTrigger.POLISH
    assert "fixed simplify and polish guidance" in polish_request.repair_context.summary
    assert "Simplify first, then polish." in polish_request.repair_context.summary
    prompt = build_prompt(polish_request)
    assert "Simplify and polish guidance:" in prompt
    assert "Review lenses for the changed files:" in prompt
    assert store.list_attempts(run.id) == [1, 2]
    assert store.load_artifact(run.id, VerificationReport, attempt=1).passed is True
    assert store.load_artifact(run.id, VerificationReport, attempt=2).passed is True


def test_post_green_polish_reserves_one_recovery_attempt(source_repo: Path, data_dir: Path) -> None:
    runtime = RecordingRuntime(FakeAgentRuntime())
    controller = WorkflowController(
        _config(data_dir, max_total_attempts=2, same_model_attempts=1, polish_enabled=True),
        FileRunStore(data_dir),
        runtime,
    )

    run = controller.run(_work_item("WI-polish-budget"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert [request.role for request in runtime.requests].count(AgentRole.IMPLEMENTER) == 1
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]


def _react_profile(fingerprint: str = "1" * 64) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint=fingerprint,
        dependency_fingerprint=fingerprint,
        version_files=("package.json",),
        dependencies=(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.NPM,
                name="react",
                declared_version="19.1.0",
                manifest_path="package.json",
                group="dependencies",
            ),
            RepositoryDependency(
                ecosystem=DependencyEcosystem.NPM,
                name="react-dom",
                declared_version="19.1.0",
                manifest_path="package.json",
                group="dependencies",
            ),
        ),
    )


def test_repository_profiler_failure_degrades_to_generic_profile(
    source_repo: Path, data_dir: Path
) -> None:
    def failing_profiler(path: Path) -> RepositoryProfile:
        raise OSError(f"cannot inspect {path.name}")

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(),
        repository_profiler=failing_profiler,
    )

    run = controller.run(_work_item("WI-profile-fallback"), source_repo)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert profile.dependencies == ()
    assert profile.warnings and "profiling degraded" in profile.warnings[0]
    inventory = store.load_artifact(run.id, ToolchainInventory)
    assert inventory.lanes == ()
    assert inventory.complete is False


def test_toolchain_inventory_is_built_from_the_workspace_and_its_profile(
    source_repo: Path, data_dir: Path
) -> None:
    seen: list[tuple[Path, RepositoryProfile]] = []

    def recording_inventory(path: Path, profile: RepositoryProfile) -> ToolchainInventory:
        seen.append((path, profile))
        return ToolchainInventory(warnings=("recorded",))

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(),
        toolchain_inventory=recording_inventory,
    )

    run = controller.run(_work_item("WI-toolchain-inventory"), source_repo)

    assert run.workspace_path is not None
    assert len(seen) == 1
    assert seen[0][0] == Path(run.workspace_path)
    assert seen[0][1] == store.load_artifact(run.id, RepositoryProfile)
    assert store.load_artifact(run.id, ToolchainInventory).warnings == ("recorded",)


def test_toolchain_inventory_failure_degrades_and_the_run_continues(
    source_repo: Path, data_dir: Path
) -> None:
    def failing_inventory(path: Path, profile: RepositoryProfile) -> ToolchainInventory:
        raise ValueError("hostile manifest")

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(),
        toolchain_inventory=failing_inventory,
    )

    run = controller.run(_work_item("WI-toolchain-fallback"), source_repo)

    assert run.state is WorkflowState.PR_READY
    inventory = store.load_artifact(run.id, ToolchainInventory)
    assert inventory.complete is False
    assert inventory.warnings == ("toolchain inventory degraded: ValueError",)


def test_polish_and_review_requests_carry_the_declared_dependency_names(
    source_repo: Path, data_dir: Path
) -> None:
    run, _, runtime = _polish_run(
        source_repo, data_dir, "WI-polish-dependencies", profile=_react_profile()
    )

    assert run.state is WorkflowState.PR_READY
    polish_implementer, tester, reviewer = _guidance_consumers(runtime)
    assert polish_implementer.dependency_names == ("react", "react-dom")
    assert reviewer.dependency_names == ("react", "react-dom")
    assert tester.dependency_names == ()


def test_failed_post_polish_verification_uses_normal_bounded_repair(
    source_repo: Path, data_dir: Path
) -> None:
    class SequenceVerifier:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *args: object, **kwargs: object) -> RepositoryVerificationResult:
            self.calls += 1
            passed = self.calls != 2
            return RepositoryVerificationResult(
                report=VerificationReport(
                    passed=passed,
                    failures=[] if passed else ["post-polish verification failed"],
                    confidence=1.0,
                ),
                command_logs=(),
                failure_kind=None if passed else VerificationFailureKind.TEST,
                failed_phase=None if passed else CheckPhase.VERIFY,
                failed_command=None,
            )

    verifier = SequenceVerifier()
    config = _config(
        data_dir,
        same_model_attempts=3,
        max_total_attempts=3,
        polish_enabled=True,
    )
    controller = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(),
        repository_verifier=verifier,  # type: ignore[arg-type]
    )

    run = controller.run(_work_item("WI-polish-repair"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert verifier.calls == 3
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
        AttemptTrigger.VERIFICATION,
    ]


# -- repository-scoped reuse and human overlays -------------------------------


def _polish_run(
    source_repo: Path,
    data_dir: Path,
    work_item_id: str,
    *,
    profile: RepositoryProfile,
    store: FileRunStore | None = None,
    run_id: str | None = None,
) -> tuple[FactoryRun, FileRunStore, RecordingRuntime]:
    runtime = RecordingRuntime(FakeAgentRuntime())
    resolved_store = store if store is not None else FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        resolved_store,
        runtime,
        repository_profiler=lambda path: profile,
    )
    run = controller.run(_work_item(work_item_id), source_repo, run_id=run_id)
    return run, resolved_store, runtime


def _guidance_consumers(runtime: RecordingRuntime) -> list[AgentRequest]:
    """The polish implementer, tester and reviewer: every agent that may
    receive repository guidance."""
    polish_implementer = next(
        request
        for request in runtime.requests
        if request.role is AgentRole.IMPLEMENTER
        and isinstance(request.repair_context, RepairContext)
        and request.repair_context.trigger is AttemptTrigger.POLISH
    )
    tester = next(request for request in runtime.requests if request.role is AgentRole.TESTER)
    reviewer = next(request for request in runtime.requests if request.role is AgentRole.REVIEWER)
    return [polish_implementer, tester, reviewer]


def test_verification_failure_with_l0_triage_escalates_and_ends_needs_human(
    source_repo: Path, data_dir: Path
) -> None:
    config = _config(data_dir, verify=["false"], same_model_attempts=2, max_total_attempts=6)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(triage=_triage_hook(Complexity.L0, Risk.R1)),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason is not None
    assert "attempt budget exhausted" in run.failure_reason
    assert len(run.attempt_records) == 6

    models_used = [attempt.model for attempt in run.attempt_records]
    assert models_used == [
        "mai-code-1.1-flash",
        "mai-code-1.1-flash",
        "claude-sonnet-5",
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-5",
    ]
    assert all(attempt.outcome == "succeeded" for attempt in run.attempt_records)


def test_low_risk_reviewer_rejection_continues_at_global_attempt_budget(
    source_repo: Path, data_dir: Path
) -> None:
    def rejecting_reviewer(request: AgentRequest) -> AgentResult:
        if request.prior_review_findings:
            review = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(
                    request,
                    ReviewDispositionStatus.UNRESOLVED,
                ),
            )
        else:
            review = ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("not good enough")],
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=review,
        )

    # L2's worker and L3's worker are the same model (claude-opus-5), so with
    # same_model_attempts=3 the router stays on that single distinct model for
    # every attempt and max_total_attempts=3 is the only thing that bounds
    # the repair loop.
    config = _config(data_dir, same_model_attempts=3, max_total_attempts=3)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(triage=_triage_hook(Complexity.L2, Risk.R1), reviewer=rejecting_reviewer),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 3
    assert all(attempt.outcome == "succeeded" for attempt in run.attempt_records)
    assert all(attempt.model == "claude-opus-5" for attempt in run.attempt_records)
    assert run.review_acceptance is not None


def test_reviewer_findings_are_blocking_and_suggestions_stay_advisory(
    source_repo: Path,
    data_dir: Path,
) -> None:
    reviewer_calls = 0
    reviewer_requests: list[AgentRequest] = []
    implementer_requests: list[AgentRequest] = []
    tester_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        implementer_requests.append(request)
        return default_runtime.run(request)

    def tester(request: AgentRequest) -> AgentResult:
        tester_requests.append(request)
        return default_runtime.run(request)

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal reviewer_calls
        reviewer_calls += 1
        reviewer_requests.append(request)
        if reviewer_calls == 1:
            return AgentResult(
                role=AgentRole.REVIEWER,
                success=True,
                review_report=ReviewReport(
                    approved=False,
                    blocking_findings=[_review_finding("A concrete correctness defect remains.")],
                    suggested_changes=["Consider renaming a helper later."],
                ),
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=True,
                prior_finding_dispositions=_resolve_prior(request),
            ),
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(implementer=implementer, tester=tester, reviewer=reviewer),
    ).run(_work_item("WI-reviewer-finding-gate"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 2
    repair_context = implementer_requests[1].repair_context
    assert isinstance(repair_context, RepairContext)
    assert repair_context.failures[0].endswith("A concrete correctness defect remains.")
    assert reviewer_requests[0].prior_review_findings == []
    assert [finding.message for finding in reviewer_requests[1].prior_review_findings] == [
        "A concrete correctness defect remains."
    ]
    assert reviewer_requests[1].repair_diff is not None
    assert tester_requests[0].repair_diff is None
    assert tester_requests[1].repair_diff is not None
    assert [finding.message for finding in tester_requests[1].prior_review_findings] == [
        "A concrete correctness defect remains."
    ]


def test_tester_receives_repair_diff_and_prior_accepted_findings_during_review_repair(
    source_repo: Path,
    data_dir: Path,
) -> None:
    store = FileRunStore(data_dir)
    default_runtime = FakeAgentRuntime()
    tester_requests: list[AgentRequest] = []

    def tester(request: AgentRequest) -> AgentResult:
        tester_requests.append(request)
        return default_runtime.run(request)

    runtime = FakeAgentRuntime(tester=tester)
    config = _config(data_dir)
    controller = WorkflowController(config, store, runtime)

    accepted_finding = ReviewFinding(
        id="review-compatibility-accepted",
        category=ReviewFindingCategory.COMPATIBILITY,
        message="A legacy response remains accepted debt.",
        locations=[
            ReviewSourceLocation(
                path="FACTORY_NOTES.md",
                start_line=1,
                end_line=1,
            )
        ],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )
    open_finding = ReviewFinding(
        id="review-correctness-open",
        category=ReviewFindingCategory.CORRECTNESS,
        message="A concrete correctness defect remains.",
        locations=[
            ReviewSourceLocation(
                path="FACTORY_NOTES.md",
                start_line=1,
                end_line=1,
            )
        ],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )

    workspace = GitWorktreeWorkspace(
        config.data_dir,
        source_repo,
        "WI-tester-repair-context",
        branch_prefix=config.repository.branch_prefix,
    )
    workspace.acquire_lock()
    try:
        workspace.prepare()
        (workspace.path / "FACTORY_NOTES.md").write_text("repaired notes\n")
        evidence = workspace.collect_evidence()

        run = FactoryRun(
            id="RUN-tester-repair-context",
            work_item_id="WI-tester-repair-context",
            state=WorkflowState.REVIEWING,
            review_ledger=ReviewLedger(
                accepted_findings=[accepted_finding],
                open_findings=[open_finding],
            ),
        )
        context = _RunContext(
            work_item=_work_item("WI-tester-repair-context"),
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=Complexity.L1,
                risk=Risk.R0,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
            ),
            specification=Specification(
                problem="Fix defect.",
                acceptance_criteria=["Notes are updated."],
                confidence=0.9,
            ),
            execution_plan=ExecutionPlan(
                summary="Fix defect.",
                steps=[PlanStep(id="1", goal="Update notes.")],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=1,
                ),
            ),
            repository_profile=RepositoryProfile(
                manifest_fingerprint="0" * 64,
                dependency_fingerprint="0" * 64,
            ),
            workspace=workspace,
            source_repo=source_repo,
        )
        repair_diff = (
            "diff --git a/FACTORY_NOTES.md b/FACTORY_NOTES.md\n@@ -1 +1 @@\n-old\n+repaired notes\n"
        )
        verification_report = VerificationReport(passed=True, confidence=1.0)

        report = controller._run_tester(
            run,
            context,
            evidence,
            verification_report,
            snapshot=2,
            repair_diff=repair_diff,
        )

        assert report.passed is True
        assert len(tester_requests) == 1
        tester_request = tester_requests[0]
        assert tester_request.repair_diff == repair_diff
        assert tester_request.accepted_review_findings == [accepted_finding]
        assert tester_request.prior_review_findings == [open_finding]
    finally:
        workspace.release_lock()


def test_review_repair_loop_passes_repair_diff_and_prior_accepted_findings_to_tester(
    source_repo: Path,
    data_dir: Path,
) -> None:
    accepted_finding = ReviewFinding(
        id="review-compatibility-accepted",
        category=ReviewFindingCategory.COMPATIBILITY,
        message="A legacy response remains accepted debt.",
        locations=[
            ReviewSourceLocation(
                path="FACTORY_NOTES.md",
                start_line=1,
                end_line=1,
            )
        ],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )
    reviewer_calls = 0
    tester_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def tester(request: AgentRequest) -> AgentResult:
        tester_requests.append(request)
        return default_runtime.run(request)

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal reviewer_calls
        reviewer_calls += 1
        if reviewer_calls == 1:
            return AgentResult(
                role=AgentRole.REVIEWER,
                success=True,
                review_report=ReviewReport(
                    approved=False,
                    blocking_findings=[_review_finding("A concrete correctness defect remains.")],
                ),
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=True,
                prior_finding_dispositions=_resolve_prior(request),
            ),
        )

    store = FileRunStore(data_dir)
    config = _config(data_dir, same_model_attempts=1, max_total_attempts=2)
    workspace = GitWorktreeWorkspace(
        config.data_dir,
        source_repo,
        "WI-tester-repair-loop",
        branch_prefix=config.repository.branch_prefix,
    )
    workspace.acquire_lock()
    try:
        workspace.prepare()
        run = FactoryRun(
            id="RUN-tester-repair-loop",
            work_item_id="WI-tester-repair-loop",
            state=WorkflowState.IMPLEMENTING,
            workspace_path=str(workspace.path),
            branch_name=workspace.branch_name,
            base_commit_sha=workspace.base_commit,
            review_ledger=ReviewLedger(accepted_findings=[accepted_finding]),
        )
        work_item = _work_item("WI-tester-repair-loop")
        spec = Specification(
            problem="Fix defect.",
            acceptance_criteria=["Notes are updated."],
            confidence=0.9,
        )
        plan = ExecutionPlan(
            summary="Fix defect.",
            steps=[PlanStep(id="1", goal="Update notes.")],
            expected_scope=ExpectedScope(
                modules=["FACTORY_NOTES.md"],
                estimated_files_min=1,
                estimated_files_max=1,
            ),
        )
        profile = RepositoryProfile(
            manifest_fingerprint="0" * 64,
            dependency_fingerprint="0" * 64,
        )
        store.save_run(run)
        store.save_artifact(run.id, work_item)
        store.save_artifact(run.id, spec)
        store.save_artifact(run.id, plan)
        store.save_artifact(run.id, profile)

        context = _RunContext(
            work_item=work_item,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=Complexity.L1,
                risk=Risk.R0,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
            ),
            specification=spec,
            execution_plan=plan,
            repository_profile=profile,
            workspace=workspace,
            source_repo=source_repo,
        )

        controller = WorkflowController(
            config,
            store,
            FakeAgentRuntime(tester=tester, reviewer=reviewer),
        )
        completed_run = controller._drive_to_pr_ready(
            run, context, AttemptBudget.IMPLEMENTATION, None
        )

        assert completed_run.state is WorkflowState.PR_READY
        assert len(tester_requests) == 2
        # Round 1 before repair: no repair diff, but accepted debt is present
        assert tester_requests[0].repair_diff is None
        assert tester_requests[0].accepted_review_findings == [accepted_finding]
        # Round 2 during review repair: repair diff and accepted debt are both present
        assert tester_requests[1].repair_diff is not None
        assert tester_requests[1].accepted_review_findings == [accepted_finding]
        assert [f.message for f in tester_requests[1].prior_review_findings] == [
            "A concrete correctness defect remains."
        ]
    finally:
        workspace.release_lock()


def test_reviewer_semantic_contract_gets_bounded_same_model_correction(
    source_repo: Path,
    data_dir: Path,
) -> None:
    reviewer_calls = 0

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal reviewer_calls
        reviewer_calls += 1
        if reviewer_calls == 1:
            return AgentResult(
                role=AgentRole.REVIEWER,
                success=True,
                review_report=ReviewReport(
                    approved=False,
                    suggested_changes=["Correct the reported return type."],
                ),
            )
        assert isinstance(request.repair_context, str)
        assert "violated the deterministic review contract" in request.repair_context
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=True),
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2, max_total_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-reviewer-suggestion-fallback"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 1
    assert reviewer_calls == 2


def test_reviewer_ledger_persists_typed_open_findings(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[
                    _review_finding(
                        "newest actionable defect",
                        category=ReviewFindingCategory.SECURITY,
                    )
                ],
            ),
        )

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=1),
        store,
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-review-ledger"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert [finding.message for finding in run.review_ledger.open_findings] == [
        "newest actionable defect"
    ]
    reloaded = store.load_run(run.id)
    assert reloaded.review_ledger == run.review_ledger


def test_repair_regression_joins_ledger_and_requires_next_disposition(
    source_repo: Path,
    data_dir: Path,
) -> None:
    reviewer_requests: list[AgentRequest] = []

    def reviewer(request: AgentRequest) -> AgentResult:
        reviewer_requests.append(request)
        call = len(reviewer_requests)
        if call == 1:
            report = ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Fix the original defect.", line=1)],
            )
        elif call == 2:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                repair_regressions=[_review_finding("The repair broke attempt metadata.", line=4)],
            )
        else:
            assert [finding.message for finding in request.prior_review_findings] == [
                "The repair broke attempt metadata."
            ]
            assert request.prior_review_findings[0].origin is (
                ReviewFindingOrigin.REPAIR_REGRESSION_DIRECT
            )
            report = ReviewReport(
                approved=True,
                prior_finding_dispositions=_resolve_prior(request),
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=report,
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=3),
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-review-regression-ledger"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 3
    assert all(record.reviewed_tree_sha for record in run.attempt_records)
    assert reviewer_requests[1].repair_diff is not None
    assert reviewer_requests[2].repair_diff is not None


def test_only_one_round_of_late_findings_can_expand_repair_scope(
    source_repo: Path,
    data_dir: Path,
) -> None:
    reviewer_requests: list[AgentRequest] = []
    implementer_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        implementer_requests.append(request)
        return default_runtime.run(request)

    def reviewer(request: AgentRequest) -> AgentResult:
        reviewer_requests.append(request)
        call = len(reviewer_requests)
        if call == 1:
            report = ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Initial blocker.", line=1)],
            )
        elif call == 2:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                blocking_findings=[_review_finding("Late blocker batch.", line=2)],
            )
        else:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                blocking_findings=[_review_finding("Drip-fed blocker.", line=3)],
            )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=report,
        )

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=4),
        store,
        FakeAgentRuntime(implementer=implementer, reviewer=reviewer),
    ).run(_work_item("WI-review-late-adoption"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 3
    assert run.review_ledger.late_adoption_rounds == 1
    assert len(implementer_requests) == 3
    first_repair = implementer_requests[1].repair_context
    second_repair = implementer_requests[2].repair_context
    assert isinstance(first_repair, RepairContext)
    assert isinstance(second_repair, RepairContext)
    assert "Initial blocker." in first_repair.failures[0]
    assert "Late blocker batch." in second_repair.failures[0]
    latest_review = store.load_artifact(run.id, ReviewReport, attempt=3)
    assert latest_review.approved is True
    assert any("Drip-fed blocker." in item for item in latest_review.suggested_changes)


def test_repeated_low_risk_review_blocker_is_accepted_after_three_rounds(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        report = (
            ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Contradictory sanitizer requirement.")],
            )
            if not request.prior_review_findings
            else ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(
                    request,
                    ReviewDispositionStatus.UNRESOLVED,
                ),
            )
        )
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=report,
        )

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=6),
        store,
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-review-impasse"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 3
    assert run.failure_reason is None
    assert run.review_acceptance is not None
    assert run.review_acceptance.review_rounds == 3
    assert run.review_acceptance.reviewed_tree_sha == run.reviewed_tree_sha
    assert [finding.message for finding in run.review_acceptance.findings] == [
        "Contradictory sanitizer requirement."
    ]
    persisted = store.load_artifact(run.id, ReviewAcceptance, attempt=3)
    assert persisted == run.review_acceptance


@pytest.mark.parametrize(
    "category", [ReviewFindingCategory.CORRECTNESS, ReviewFindingCategory.SIMPLICITY]
)
def test_low_risk_finding_is_accepted_at_configured_review_round_limit(
    source_repo: Path,
    data_dir: Path,
    category: ReviewFindingCategory,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Bounded low-risk defect.", category=category)],
            ),
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=3)
    config.review.max_rounds = 1
    run = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-review-round-limit"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(run.attempt_records) == 1
    assert run.review_acceptance is not None
    assert run.review_acceptance.reason is ReviewAcceptanceReason.REVIEW_ROUND_LIMIT
    assert [finding.category for finding in run.review_acceptance.findings] == [category]


def test_ineligible_finding_stops_at_configured_review_round_limit(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[
                    _review_finding(
                        "Security defect.",
                        category=ReviewFindingCategory.SECURITY,
                    )
                ],
            ),
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=3)
    config.review.max_rounds = 1
    store = FileRunStore(data_dir)
    run = WorkflowController(
        config,
        store,
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-ineligible-round-limit"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 1
    assert run.review_acceptance is None
    impasse = store.load_artifact(run.id, ReviewImpasse, attempt=1)
    assert impasse.kind is ReviewImpasseKind.REVIEW_ROUND_LIMIT


def test_unattended_blocked_finding_on_r2_is_accepted_at_review_round_limit(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[
                    _review_finding(
                        "Security defect.",
                        category=ReviewFindingCategory.SECURITY,
                    )
                ],
            ),
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=3)
    config.review.max_rounds = 1
    config.factory.unattended = True
    run = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2), reviewer=reviewer),
    ).run(_work_item("WI-unattended-round-limit"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.review_acceptance is not None
    assert [f.category for f in run.review_acceptance.findings] == [ReviewFindingCategory.SECURITY]


def test_approved_followup_cannot_bypass_carried_acceptance_policy(
    data_dir: Path,
) -> None:
    finding = ReviewFinding(
        id="review-security-carried",
        category=ReviewFindingCategory.SECURITY,
        message="Unsafe carried debt.",
        locations=[
            ReviewSourceLocation(
                path="FACTORY_NOTES.md",
                start_line=1,
                end_line=1,
            )
        ],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )
    acceptance = ReviewAcceptance(
        snapshot=1,
        reason=ReviewAcceptanceReason.CARRIED_FORWARD,
        risk=Risk.R1,
        review_rounds=1,
        reviewed_tree_sha="a" * 40,
        findings=[finding],
    )
    run = FactoryRun(
        id="run-carried-policy",
        work_item_id="WI-carried-policy",
        state=WorkflowState.PR_READY,
        reviewed_tree_sha="a" * 40,
        review_acceptance=acceptance,
        review_ledger=ReviewLedger(accepted_findings=[finding]),
    )
    controller = WorkflowController(
        _config(data_dir),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )

    assert not controller._review_authorizes_delivery(
        run,
        ReviewReport(approved=True),
        Risk.R1,
    )


def test_security_review_blocker_still_requires_human_after_three_rounds(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        report = (
            ReviewReport(
                approved=False,
                blocking_findings=[
                    _review_finding(
                        "Authentication can be bypassed.",
                        category=ReviewFindingCategory.SECURITY,
                    )
                ],
            )
            if not request.prior_review_findings
            else ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(
                    request,
                    ReviewDispositionStatus.UNRESOLVED,
                ),
            )
        )
        return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=report)

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=6),
        store,
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-security-impasse"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.review_acceptance is None
    impasse = store.load_artifact(run.id, ReviewImpasse, attempt=3)
    assert impasse.finding_ids


def test_high_risk_review_blocker_cannot_be_accepted(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Risky behavior remains.")],
            ),
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=1)
    config.risk[Risk.R2].human_approval = False
    run = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L1, Risk.R2),
            reviewer=reviewer,
        ),
    ).run(_work_item("WI-high-risk-impasse"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.review_acceptance is None


def test_repair_regression_cannot_be_accepted_at_attempt_limit(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        if not request.prior_review_findings:
            report = ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Initial blocker.")],
            )
        else:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                repair_regressions=[_review_finding("Repair introduced a defect.", line=4)],
            )
        return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=report)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-regression-impasse"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.review_acceptance is None
    assert run.review_ledger.open_findings[0].origin is (
        ReviewFindingOrigin.REPAIR_REGRESSION_DIRECT
    )


def test_finding_count_over_policy_limit_cannot_be_accepted(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[
                    _review_finding("Correctness defect."),
                    _review_finding(
                        "Compatibility defect.",
                        category=ReviewFindingCategory.COMPATIBILITY,
                    ),
                ],
            ),
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=1)
    config.review.max_accepted_findings = 1
    run = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-too-many-accepted-findings"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.review_acceptance is None
    assert len(run.review_ledger.open_findings) == 2


def test_late_security_finding_remains_blocking_after_adoption_round(
    source_repo: Path,
    data_dir: Path,
) -> None:
    calls = 0

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            report = ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Initial correctness defect.")],
            )
        elif calls == 2:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                blocking_findings=[
                    _review_finding(
                        "Late compatibility defect.",
                        line=2,
                        category=ReviewFindingCategory.COMPATIBILITY,
                    )
                ],
            )
        else:
            report = ReviewReport(
                approved=False,
                prior_finding_dispositions=_resolve_prior(request),
                blocking_findings=[
                    _review_finding(
                        "Late security defect.",
                        line=3,
                        category=ReviewFindingCategory.SECURITY,
                    )
                ],
            )
        return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=report)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=3),
        FileRunStore(data_dir),
        FakeAgentRuntime(reviewer=reviewer),
    ).run(_work_item("WI-late-security"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.review_acceptance is None
    assert run.review_ledger.late_adoption_rounds == 1
    assert run.review_ledger.open_findings[0].category is ReviewFindingCategory.SECURITY
    latest_review = FileRunStore(data_dir).load_artifact(run.id, ReviewReport, attempt=3)
    assert not any("Late security defect." in item for item in latest_review.suggested_changes)


def test_implementer_failures_consume_the_shared_attempt_budget(
    source_repo: Path, data_dir: Path
) -> None:
    def always_failing_implementer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.IMPLEMENTER, success=False, failure_reason="simulated crash"
        )

    config = _config(data_dir, same_model_attempts=1, max_total_attempts=2)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config, store, FakeAgentRuntime(implementer=always_failing_implementer)
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 2
    assert all(attempt.outcome == "failed" for attempt in run.attempt_records)
    assert all(attempt.failure_reason == "simulated crash" for attempt in run.attempt_records)
    assert run.active_invocation is None


def test_implementer_retry_receives_partial_worktree_diff(
    source_repo: Path, data_dir: Path
) -> None:
    calls = 0
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            assert request.workspace_path is not None
            Path(request.workspace_path, "partial.py").write_text(
                "PARTIAL = True\n", encoding="utf-8"
            )
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                failure_reason="IMPLEMENTER: copilot timed out after 900s",
            )
        return default_runtime.run(request)

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        store,
        FakeAgentRuntime(implementer=implementer),
    ).run(_work_item("WI-partial-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert calls == 2
    assert requests[1].diff is not None
    assert "partial.py" in requests[1].diff
    assert isinstance(requests[1].repair_context, RepairContext)
    assert "partial, unverified edits" in requests[1].repair_context.summary
    assert (store.run_dir(run.id) / "attempts" / "01" / "patch.diff").is_file()


def test_failed_partial_evidence_capture_does_not_abort_retry_loop(
    source_repo: Path,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always_failing_implementer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=False,
            failure_reason="simulated timeout",
        )

    def fail_evidence(self: object) -> object:
        raise WorkspaceError("locked index")

    monkeypatch.setattr(GitWorktreeWorkspace, "collect_evidence", fail_evidence)
    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        store,
        FakeAgentRuntime(implementer=always_failing_implementer),
    ).run(_work_item("WI-broken-partial-evidence"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 2
    assert all(
        "could not capture partial worktree evidence: locked index"
        in (attempt.failure_reason or "")
        for attempt in run.attempt_records
    )


# -- triage-driven human gates ------------------------------------------------


def test_r2_triage_ends_needs_human_and_never_reaches_pr_ready(
    source_repo: Path, data_dir: Path
) -> None:
    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config, store, FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2))
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "R2" in (run.failure_reason or "")
    assert run.attempt_records == []


def test_unattended_r2_triage_continues_to_pr_ready(source_repo: Path, data_dir: Path) -> None:
    config = _config(data_dir)
    config.factory.unattended = True
    controller = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2)),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert config.requires_human_approval(Risk.R2) is False


def test_full_run_makes_one_planner_call_for_the_specification_and_plan(
    source_repo: Path, data_dir: Path
) -> None:
    """ADR-035: triage, then one planner call, then the implementer."""
    store = FileRunStore(data_dir)
    planning_states: list[WorkflowState] = []

    def planner(request: AgentRequest) -> AgentResult:
        planning_states.append(store.list_runs()[0].state)
        return FakeAgentRuntime().run(request)

    runtime = RecordingRuntime(FakeAgentRuntime(planner=planner))

    run = WorkflowController(_config(data_dir), store, runtime).run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    roles = [request.role for request in runtime.requests]
    assert roles[: roles.index(AgentRole.IMPLEMENTER)] == [AgentRole.TRIAGE, AgentRole.PLANNER]
    planner_request = runtime.requests[1]
    assert planner_request.triage_result == store.load_artifact(run.id, TriageResult)
    assert planner_request.specification is None
    specification = store.load_artifact(run.id, Specification)
    assert specification.acceptance_criteria
    later = runtime.requests[2:]
    assert all(request.specification == specification for request in later)
    assert planning_states == [WorkflowState.PLANNING]


def test_ineligible_triage_ends_needs_human(source_repo: Path, data_dir: Path) -> None:
    def ineligible_triage(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=False,
                complexity=Complexity.L1,
                risk=Risk.R1,
                dependencies=[],
                unknowns=["scope unclear"],
                confidence=0.3,
            ),
        )

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(triage=ineligible_triage))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "ineligible" in (run.failure_reason or "")


@pytest.mark.parametrize("unattended", [False, True])
def test_unattended_sensitive_scope_continues_instead_of_stopping(
    source_repo: Path, data_dir: Path, unattended: bool
) -> None:
    default_runtime = FakeAgentRuntime()

    def implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        workflows = Path(request.workspace_path) / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "ci.yml").write_text("on: push\n")
        return default_runtime.run(request)

    config = _config(data_dir)
    config.factory.unattended = unattended
    run = WorkflowController(
        config, FileRunStore(data_dir), FakeAgentRuntime(implementer=implementer)
    ).run(_work_item("WI-sensitive-scope"), source_repo)

    expected = WorkflowState.PR_READY if unattended else WorkflowState.NEEDS_HUMAN
    assert run.state is expected


def test_unattended_ineligible_triage_continues_to_pr_ready(
    source_repo: Path, data_dir: Path
) -> None:
    def ineligible_triage(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=False,
                complexity=Complexity.L1,
                risk=Risk.R1,
                dependencies=[],
                unknowns=["scope unclear"],
                confidence=0.3,
            ),
        )

    config = _config(data_dir)
    config.factory.unattended = True
    controller = WorkflowController(
        config, FileRunStore(data_dir), FakeAgentRuntime(triage=ineligible_triage)
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY


# -- operational agent failures ----------------------------------------------


def test_planner_agent_failure_produces_persisted_failed_run(
    source_repo: Path, data_dir: Path
) -> None:
    def crashing_planner(request: AgentRequest) -> AgentResult:
        return AgentResult(role=AgentRole.PLANNER, success=False, failure_reason="planner crashed")

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(planner=crashing_planner))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.FAILED
    assert run.failure_reason == "planner crashed"
    assert run.completed_at is not None
    assert [record.role for record in run.invocation_records] == [
        AgentRole.TRIAGE,
        AgentRole.PLANNER,
    ]
    assert run.invocation_records[-1].success is False
    assert run.invocation_records[-1].failure_reason == "planner crashed"
    persisted = store.load_run(run.id)
    assert persisted == run


def test_runtime_exception_produces_persisted_failed_invocation(
    source_repo: Path, data_dir: Path
) -> None:
    def unavailable_planner(request: AgentRequest) -> AgentResult:
        raise RuntimeError("runtime unavailable")

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(planner=unavailable_planner))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.FAILED
    assert [record.role for record in run.invocation_records] == [
        AgentRole.TRIAGE,
        AgentRole.PLANNER,
    ]
    invocation = run.invocation_records[-1]
    assert invocation.success is False
    assert invocation.failure_reason == "RuntimeError: runtime unavailable"
    assert store.load_run(run.id) == run


# -- workspace locking --------------------------------------------------------


def test_duplicate_active_work_item_ends_failed_without_corrupting_workspace(
    source_repo: Path, data_dir: Path
) -> None:
    from software_agent_factory.workspace import GitWorktreeWorkspace

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    work_item = _work_item("WI-locked")

    holder = GitWorktreeWorkspace(
        config.data_dir, source_repo, work_item.id, branch_prefix=config.repository.branch_prefix
    )
    holder.acquire_lock()
    try:
        controller = WorkflowController(config, store, FakeAgentRuntime())
        run = controller.run(work_item, source_repo)

        assert run.state is WorkflowState.FAILED
        assert "lock" in (run.failure_reason or "")
    finally:
        holder.release_lock()


def test_workspace_lock_is_released_when_post_prepare_persistence_fails(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())
    original_save_run = store.save_run
    calls = 0

    def fail_second_save(run: FactoryRun) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        return original_save_run(run)

    monkeypatch.setattr(store, "save_run", fail_second_save)

    with pytest.raises(OSError, match="simulated disk failure"):
        controller.run(_work_item("WI-lock-release"), source_repo)

    lock_path = data_dir / "locks" / "WI-lock-release.lock"
    assert not lock_path.exists()


# -- completion, leases and activity ------------------------------------------


def test_transition_clears_stale_completion_and_failure_on_an_active_state(
    data_dir: Path,
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime())
    run = FactoryRun(
        id="RUN-stale", work_item_id="WI-X", state=WorkflowState.CI_DIAGNOSIS
    ).model_copy(update={"failure_reason": "an earlier CI failure", "completed_at": utc_now()})

    moved = controller.transition(run, WorkflowState.IMPLEMENTING)

    assert moved.completed_at is None
    assert moved.failure_reason is None
    assert moved.last_activity_at is not None
    assert moved.last_activity_at >= run.created_at


def test_transition_refreshes_last_activity_and_the_lease_heartbeat(
    data_dir: Path,
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime())
    lease = RunLease(host="localhost", pid=4242, heartbeat_at=utc_now())
    run = FactoryRun(id="RUN-lease", work_item_id="WI-X", state=WorkflowState.PLANNING, lease=lease)

    active = controller.transition(run, WorkflowState.IMPLEMENTING)
    assert active.lease is not None
    assert active.lease.heartbeat_at >= lease.heartbeat_at
    assert active.last_activity_at == active.updated_at

    terminal = controller.transition(active, WorkflowState.NEEDS_HUMAN, failure_reason="stop")
    assert terminal.lease is None, "a terminal run must not keep an ownership lease"
    assert terminal.completed_at is not None


def test_finalize_pr_ready_completes_only_a_pr_ready_run(data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime())
    ready = FactoryRun(id="RUN-ready", work_item_id="WI-X", state=WorkflowState.PR_READY)

    finalized = controller.finalize_pr_ready(ready)

    assert finalized.completed_at is not None
    assert finalized.lease is None
    assert is_run_finished(finalized)
    assert store.load_run("RUN-ready") == finalized

    with pytest.raises(TransitionError):
        controller.finalize_pr_ready(
            FactoryRun(id="RUN-other", work_item_id="WI-X", state=WorkflowState.PLANNING)
        )


def test_is_run_finished_distinguishes_completed_from_interrupted_pr_ready() -> None:
    interrupted = FactoryRun(id="a", work_item_id="w", state=WorkflowState.PR_READY)
    completed = interrupted.model_copy(update={"completed_at": utc_now()})

    assert is_run_finished(interrupted) is False
    assert is_run_finished(completed) is True
    for state in (WorkflowState.DONE, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED):
        assert is_run_finished(FactoryRun(id="b", work_item_id="w", state=state)) is True
    assert (
        is_run_finished(FactoryRun(id="c", work_item_id="w", state=WorkflowState.CI_RUNNING))
        is False
    )


def test_polish_runs_by_default_with_top_level_models(
    source_repo: Path,
    data_dir: Path,
) -> None:
    recorded_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def recording_runtime(request: AgentRequest) -> AgentResult:
        recorded_requests.append(request)
        return default_runtime.run(request)

    config = _config(data_dir, polish_enabled=True)
    controller = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=recording_runtime,
            tester=recording_runtime,
            reviewer=recording_runtime,
        ),
    )

    run = controller.run(_work_item("WI-standard-mode"), source_repo)

    assert run.state is WorkflowState.PR_READY
    planner_req = next(r for r in recorded_requests if r.role is AgentRole.PLANNER)
    assert planner_req.model == config.models.planner.model
    polish_attempts = [
        att for att in run.attempt_records if att.triggered_by is AttemptTrigger.POLISH
    ]
    assert len(polish_attempts) == 1


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("README.md", "scope includes protected files: README.md"),
        ("package.json", "scope includes manifest or version files: package.json"),
        (".github/workflows/ci.yml", "scope includes sensitive files: .github/workflows/ci.yml"),
        ("src/app.py", None),
    ],
)
def test_sensitive_scope_reason_names_the_first_matching_rule(
    data_dir: Path,
    path: str,
    reason: str | None,
) -> None:
    config = _config(data_dir)
    config.repository.protected_file_patterns = ["README.md"]
    controller = WorkflowController(config, FileRunStore(data_dir), FakeAgentRuntime())
    plan = ExecutionPlan(
        summary="Change one file.",
        steps=[PlanStep(id="1", goal="Edit the file.", likely_files=[path])],
        expected_scope=ExpectedScope(modules=[path], estimated_files_min=1, estimated_files_max=1),
    )

    assert (
        controller._sensitive_scope_reason(
            [path],
            execution_plan=plan,
            risk=Risk.R0,
            repository_profile=_react_profile(),
        )
        == reason
    )


def test_reviewer_source_location_model_validation_rejects_invalid_paths() -> None:
    invalid_paths = [
        ".",
        "foo\nbar.py",
        "foo\0bar.py",
        "foo\rbar.py",
        "/abs/path.py",
        "../escape.py",
        "a\\b.py",
    ]
    for invalid in invalid_paths:
        with pytest.raises(ValidationError):
            ReviewSourceLocation(path=invalid, start_line=1, end_line=5)


def test_reviewer_invalid_path_rejected_by_workspace_validation_does_not_strand_reviewing(
    source_repo: Path,
    data_dir: Path,
) -> None:
    reviewer_attempts = 0

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal reviewer_attempts
        reviewer_attempts += 1
        invalid_path = "." if reviewer_attempts == 1 else "foo\nbar.py"
        report = ReviewReport.model_construct(
            approved=False,
            blocking_findings=[],
            repair_regressions=[],
            prior_finding_dispositions=[],
        )
        draft = ReviewFindingDraft.model_construct(
            category=ReviewFindingCategory.CORRECTNESS,
            message="Invalid path finding",
            locations=[
                ReviewSourceLocation.model_construct(
                    path=invalid_path,
                    start_line=1,
                    end_line=1,
                )
            ],
        )
        report.blocking_findings.append(draft)
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=report,
        )

    config = _config(data_dir, same_model_attempts=2)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(reviewer=reviewer),
    )
    run = controller.run(_work_item("WI-reviewer-invalid-path"), source_repo)

    assert run.state is WorkflowState.FAILED
    assert reviewer_attempts == 2
    assert "review finding cites an invalid repository path" in (run.failure_reason or "")


def test_rework_counters_verification_and_review_not_double_counted(
    source_repo: Path,
    data_dir: Path,
) -> None:
    implementer_attempts = 0
    reviewer_calls = 0

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal implementer_attempts
        implementer_attempts += 1
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "app.txt").write_text(f"attempt {implementer_attempts}\n")
        if implementer_attempts >= 2:
            (Path(request.workspace_path) / "verification_passed.txt").write_text("ok\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary=f"Attempt {implementer_attempts}"),
        )

    def planner_with_app_txt(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with app.txt.",
                steps=[
                    PlanStep(
                        id="1",
                        goal="Edit app.txt.",
                        likely_files=["app.txt", "verification_passed.txt"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["app.txt", "verification_passed.txt"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
            ),
        )

    def reviewer(request: AgentRequest) -> AgentResult:
        nonlocal reviewer_calls
        reviewer_calls += 1
        if reviewer_calls == 1:
            return AgentResult(
                role=AgentRole.REVIEWER,
                success=True,
                review_report=ReviewReport(
                    approved=False,
                    blocking_findings=[
                        ReviewFindingDraft(
                            category=ReviewFindingCategory.CORRECTNESS,
                            message="Please fix this bug.",
                            locations=[
                                ReviewSourceLocation(
                                    path="app.txt",
                                    start_line=1,
                                    end_line=1,
                                )
                            ],
                        )
                    ],
                ),
            )
        dispositions = [
            ReviewFindingDisposition(
                finding_id=f.id,
                status=ReviewDispositionStatus.RESOLVED,
                rationale="Fixed in attempt 3.",
            )
            for f in request.prior_review_findings
        ]
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=True,
                blocking_findings=[],
                prior_finding_dispositions=dispositions,
            ),
        )

    config = _config(
        data_dir,
        verify=["test -f verification_passed.txt"],
        same_model_attempts=3,
        max_total_attempts=5,
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(planner=planner_with_app_txt, implementer=implementer, reviewer=reviewer),
    )
    run = controller.run(_work_item("WI-rework-no-double-count"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert implementer_attempts == 3

    assert run.performance.counters.get("rework_total") == 2
    assert run.performance.counters.get("rework.repair_attempt") == 2

    assert run.performance.counters.get("gate_failures_total") == 2
    assert run.performance.counters.get("gate_failure.verification") == 1
    assert run.performance.counters.get("gate_failure.review") == 1

    assert run.performance.counters.get("rework_cause.verification_failure") == 1
    assert run.performance.counters.get("rework_cause.review_rejection") == 1

    valid_states = {s.value for s in WorkflowState}
    for metric in run.performance.metrics:
        if metric.stage is not None:
            assert metric.stage in valid_states, f"Invalid stage label: {metric.stage}"


def test_rework_telemetry_initial_plus_polish(source_repo: Path, data_dir: Path) -> None:
    profile = _react_profile()
    run, _, _ = _polish_run(source_repo, data_dir, "WI-polish-rework-telemetry", profile=profile)

    assert run.state is WorkflowState.PR_READY
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    # Rework telemetry excludes initial implementation and optional POLISH
    assert run.performance.counters.get("rework_total", 0) == 0
    assert run.performance.counters.get("rework.repair_attempt", 0) == 0
    assert run.performance.counters.get("gate_failures_total", 0) == 0

    metrics = _compute_aggregate_metrics([run])
    assert metrics.performance.rework.total_rework_attempts == 0
    assert metrics.performance.rework.runs_with_rework == 0
    assert metrics.performance.rework.total_gate_failures == 0


def test_rework_telemetry_initial_plus_verification_repair(
    source_repo: Path,
    data_dir: Path,
) -> None:
    implementer_attempts = 0

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal implementer_attempts
        implementer_attempts += 1
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "app.txt").write_text(f"attempt {implementer_attempts}\n")
        if implementer_attempts >= 2:
            (Path(request.workspace_path) / "verification_passed.txt").write_text("ok\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary=f"Attempt {implementer_attempts}"),
        )

    def planner_with_app_txt(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with app.txt.",
                steps=[
                    PlanStep(
                        id="1",
                        goal="Edit app.txt.",
                        likely_files=["app.txt", "verification_passed.txt"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["app.txt", "verification_passed.txt"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
            ),
        )

    config = _config(
        data_dir,
        verify=["test -f verification_passed.txt"],
        same_model_attempts=3,
        max_total_attempts=5,
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(planner=planner_with_app_txt, implementer=implementer),
    )
    run = controller.run(_work_item("WI-rework-verification-repair"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert implementer_attempts == 2
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.VERIFICATION,
    ]

    # Exactly 1 rework attempt counted, verification gate failure is the cause
    assert run.performance.counters.get("rework_total") == 1
    assert run.performance.counters.get("rework.repair_attempt") == 1
    assert run.performance.counters.get("rework_cause.verification_failure") == 1
    assert run.performance.counters.get("gate_failures_total") == 1
    assert run.performance.counters.get("gate_failure.verification") == 1

    metrics = _compute_aggregate_metrics([run])
    assert metrics.performance.rework.total_rework_attempts == 1
    assert metrics.performance.rework.runs_with_rework == 1
    assert metrics.performance.rework.total_gate_failures == 1
    assert metrics.performance.rework.verification_gate_failures == 1


def test_unready_first_plan_then_ready_clarification_proceeds(
    source_repo: Path,
    data_dir: Path,
) -> None:
    planner_requests: list[AgentRequest] = []
    call_count = 0

    def planner_hook(request: AgentRequest) -> AgentResult:
        nonlocal call_count
        call_count += 1
        planner_requests.append(request)
        if call_count == 1:
            return AgentResult(
                role=AgentRole.PLANNER,
                success=True,
                execution_plan=ExecutionPlan(
                    summary="First attempt with unready decisions",
                    steps=[
                        PlanStep(
                            id="step-1",
                            goal="Implement core parser",
                            likely_files=["FACTORY_NOTES.md"],
                            validation=["Run tests"],
                        )
                    ],
                    expected_scope=ExpectedScope(
                        modules=["FACTORY_NOTES.md"],
                        estimated_files_min=1,
                        estimated_files_max=2,
                    ),
                    test_strategy=["Run verification"],
                    unresolved_decisions=[
                        "Need choice between SQLite and PostgreSQL for persistence layer.",
                    ],
                ),
            )
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Clarified ready execution plan",
                steps=[
                    PlanStep(
                        id="step-1",
                        goal="Implement core parser",
                        likely_files=["FACTORY_NOTES.md"],
                        validation=["Run tests"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
                test_strategy=["Run verification"],
                unresolved_decisions=[],
            ),
        )

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(planner=planner_hook),
    )
    run = controller.run(_work_item("WI-clarify-ready"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert call_count == 2
    assert isinstance(planner_requests[1].repair_context, str)
    assert "Need choice between SQLite and PostgreSQL" in planner_requests[1].repair_context
    assert (
        "Resolve any item that repository evidence or existing constraints answer"
        in planner_requests[1].repair_context
    )
    assert "Retain only genuinely human-owned choices" in planner_requests[1].repair_context
    assert "Return a complete PlanningResult JSON object" in planner_requests[1].repair_context
    # ADR-035: the re-plan gets the specification of the first call and returns both again.
    assert planner_requests[0].specification is None
    assert planner_requests[1].specification is not None
    assert store.load_artifact(run.id, Specification) == planner_requests[1].specification

    planner_invocations = [r for r in run.invocation_records if r.role is AgentRole.PLANNER]
    assert len(planner_invocations) == 2

    implementer_invocations = [r for r in run.invocation_records if r.role is AgentRole.IMPLEMENTER]
    assert len(implementer_invocations) >= 1


def test_unresolved_first_and_clarification_plans_end_needs_human(
    source_repo: Path,
    data_dir: Path,
) -> None:
    planner_calls = 0

    def planner_hook(request: AgentRequest) -> AgentResult:
        nonlocal planner_calls
        planner_calls += 1
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with persistent unresolved decisions",
                steps=[
                    PlanStep(
                        id="step-1",
                        goal="Implement parser",
                        likely_files=["FACTORY_NOTES.md"],
                        validation=["Run tests"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
                test_strategy=["Run verification"],
                unresolved_decisions=[
                    "Choice between SQLite and PostgreSQL requires human architecture input.",
                    "Delivery protocol requires human choice between gRPC and REST.",
                ],
            ),
        )

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(planner=planner_hook),
    )
    run = controller.run(_work_item("WI-unresolved-halt"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason == UNRESOLVED_DECISIONS_HALT_REASON
    assert planner_calls == 2

    assert run.escalation is not None
    assert run.escalation.reason_code == "UNRESOLVED_DECISIONS"
    assert run.escalation.resume_classification == ResumeClassification.PLAN_DECISION

    persisted_plan = store.load_artifact(run.id, ExecutionPlan)
    assert len(persisted_plan.unresolved_decisions) == 2
    assert persisted_plan.is_ready is False

    implementer_invocations = [r for r in run.invocation_records if r.role is AgentRole.IMPLEMENTER]
    assert len(implementer_invocations) == 0


def test_unattended_plan_with_unresolved_decisions_is_implemented(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def planner_hook(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with an unresolved decision",
                steps=[
                    PlanStep(
                        id="step-1",
                        goal="Implement parser",
                        likely_files=["FACTORY_NOTES.md"],
                        validation=["Run tests"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
                test_strategy=["Run verification"],
                unresolved_decisions=["Choice between SQLite and PostgreSQL."],
            ),
        )

    config = _config(data_dir)
    config.factory.unattended = True
    run = WorkflowController(
        config, FileRunStore(data_dir), FakeAgentRuntime(planner=planner_hook)
    ).run(_work_item("WI-unattended-unresolved"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert any(r.role is AgentRole.IMPLEMENTER for r in run.invocation_records)


def test_scope_replan_with_unresolved_decisions_does_not_halt_verified_change(
    source_repo: Path,
    data_dir: Path,
) -> None:
    planner_calls = 0

    def planner_hook(request: AgentRequest) -> AgentResult:
        nonlocal planner_calls
        planner_calls += 1
        if request.repair_context is not None and isinstance(request.repair_context, RepairContext):
            return AgentResult(
                role=AgentRole.PLANNER,
                success=True,
                execution_plan=ExecutionPlan(
                    summary="Scope replan with unresolved decisions",
                    steps=[
                        PlanStep(
                            id="step-1",
                            goal="Implement core parser",
                            likely_files=["IMPLEMENTATION_NOTES.md"],
                            validation=["Run tests"],
                        )
                    ],
                    expected_scope=ExpectedScope(
                        modules=["IMPLEMENTATION_NOTES.md"],
                        estimated_files_min=1,
                        estimated_files_max=2,
                    ),
                    test_strategy=["Run verification"],
                    unresolved_decisions=["Uncertainty about extra file long term retention."],
                ),
            )
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Initial ready plan",
                steps=[
                    PlanStep(
                        id="step-1",
                        goal="Implement core parser",
                        likely_files=["FACTORY_NOTES.md"],
                        validation=["Run tests"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
                test_strategy=["Run verification"],
                unresolved_decisions=[],
            ),
        )

    def implementer_touching_extra_file(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        ws = Path(request.workspace_path)
        (ws / "IMPLEMENTATION_NOTES.md").write_text("extra content\n", encoding="utf-8")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Touched extra file",
                changed_files=["IMPLEMENTATION_NOTES.md"],
            ),
        )

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(
            planner=planner_hook,
            implementer=implementer_touching_extra_file,
        ),
    )
    run = controller.run(_work_item("WI-scope-replan-unresolved"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.scope_replans == 1
    assert planner_calls == 2


def test_default_fake_planner_regression(
    source_repo: Path,
    data_dir: Path,
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(),
    )
    run = controller.run(_work_item("WI-default-fake"), source_repo)

    assert run.state is WorkflowState.PR_READY
    plan = store.load_artifact(run.id, ExecutionPlan)
    assert plan.unresolved_decisions == []
    assert plan.is_ready is True
    assert plan.ready is True

    planner_invocations = [r for r in run.invocation_records if r.role is AgentRole.PLANNER]
    assert len(planner_invocations) == 1


def test_a_dashboard_risk_approval_reopens_the_run_at_planning_without_new_budget(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config, store, FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2))
    )
    run = controller.run(_work_item("WI-dashboard-risk"), source_repo, run_id="run-dashboard-risk")
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.escalation is not None
    assert run.escalation.approval_context is not None
    assert run.attempt_records == []
    store.create_dashboard_request(
        run.id,
        DashboardResumeRequest(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            context_fingerprint=run.escalation.approval_context.context_fingerprint,
            action=ResumeClassification.RISK_APPROVAL,
        ),
    )

    now = run.escalation.created_at + timedelta(minutes=1)

    receipt = ingest_dashboard_request(run, store, config, now)

    assert receipt is not None
    ingested = store.load_run(run.id)
    assert ingested.state is WorkflowState.NEEDS_HUMAN
    assert ingested.attempt_records == run.attempt_records
    reopened = controller.reopen(run.id, source_repo)
    assert reopened.state is WorkflowState.PR_READY
    assert reopened.escalation is not None
    assert reopened.escalation.status is EscalationStatus.RESUMED
    assert [r.source for r in reopened.escalation.accepted_replies] == ["dashboard"]
    roles = [record.role for record in reopened.invocation_records]
    assert roles.count(AgentRole.TRIAGE) == 1
    assert roles.count(AgentRole.PLANNER) == 1
    assert AgentRole.REFINER not in roles
    assert [(a.attempt_number, a.budget) for a in reopened.attempt_records] == [
        (1, AttemptBudget.IMPLEMENTATION)
    ]


def test_dashboard_plan_answers_reach_the_planning_prompt_after_reopen(
    source_repo: Path,
    data_dir: Path,
) -> None:
    answer = "Use JSON files in the data dir."
    planner_contexts: list[str | None] = []

    def planner(request: AgentRequest) -> AgentResult:
        planner_contexts.append(request.repair_context)
        answered = request.repair_context is not None and answer in request.repair_context
        decisions = [] if answered else ["Choose the local persistence format."]
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan the change.",
                steps=[],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"], estimated_files_min=1, estimated_files_max=1
                ),
                unresolved_decisions=decisions,
            ),
        )

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(planner=planner))
    run = controller.run(_work_item("WI-dashboard-plan"), source_repo, run_id="run-dashboard-plan")
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.escalation is not None
    assert run.escalation.resume_classification is ResumeClassification.PLAN_DECISION
    assert run.escalation.plan_decision_context is not None
    store.create_dashboard_request(
        run.id,
        DashboardResumeRequest(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            context_fingerprint=run.escalation.plan_decision_context.context_fingerprint,
            action=ResumeClassification.PLAN_DECISION,
            answers=[PlanDecisionAnswer(decision_number=1, answer=answer)],
        ),
    )
    now = run.escalation.created_at + timedelta(minutes=1)

    assert ingest_dashboard_request(run, store, config, now) is not None
    reopened = controller.reopen(run.id, source_repo)

    assert reopened.state is WorkflowState.PR_READY
    assert planner_contexts[-1] is not None
    assert answer in planner_contexts[-1]
    assert reopened.attempt_records[0].triggered_by is AttemptTrigger.INITIAL


# double-waiver: B1 — DeterministicVerifier spawns subprocesses; this records commands instead.
class _ScriptedCommandRunner(DeterministicVerifier):
    """Records commands and fails the ones named in ``failing``. Runs nothing."""

    def __init__(
        self,
        failing: frozenset[str] = frozenset(),
        raises: frozenset[str] = frozenset(),
        error: type[Exception] = OSError,
    ) -> None:
        super().__init__()
        self.failing = failing
        self.raises = raises
        self.error = error
        self.commands: list[str] = []

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 32768,
    ) -> VerificationReport:
        results: list[CommandResult] = []
        for command in commands:
            self.commands.append(command)
            if command in self.raises:
                raise self.error("spawn failed")
            exit_code = 1 if command in self.failing else 0
            results.append(
                CommandResult(
                    command=command,
                    exit_code=exit_code,
                    stdout="",
                    stderr="",
                    duration_seconds=0.0,
                )
            )
            if exit_code:
                break
        passed = all(result.exit_code == 0 for result in results)
        return VerificationReport(
            passed=passed,
            deterministic_checks=results,
            failures=[] if passed else ["failed"],
            confidence=1.0 if passed else 0.0,
        )


@pytest.fixture
def uv_python_repo(tmp_path: Path) -> Path:
    """A Python repository with a root uv.lock, ruff configured and pytest declared."""
    repo = tmp_path / "uv-source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[dependency-groups]\ndev = ["pytest", "ruff"]\n\n[tool.ruff]\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


_UV_INSTALL = "uv sync --locked"
_RUFF_FORMAT = "CI=true uv run --no-sync ruff format --check ."
_RUFF_LINT = "CI=true uv run --no-sync ruff check --no-fix ."
_PYTEST = "CI=true uv run --no-sync pytest -q"


def test_configured_commands_win_over_derived_commands(
    uv_python_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner()
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, verify=["make check"]), store, FakeAgentRuntime(), verifier=runner
    )

    run = controller.run(_work_item("WI-config-commands"), uv_python_repo)

    plan = store.load_artifact(run.id, RepositoryCommandsPlan)
    assert plan == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.CONFIG, verify=("make check",)
    )
    assert set(runner.commands) == {"make check"}


def test_derived_commands_are_probed_then_used_for_verification(
    uv_python_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner(failing=frozenset({_RUFF_LINT}))
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime(), verifier=runner)

    run = controller.run(_work_item("WI-derived-commands"), uv_python_repo)

    plan = store.load_artifact(run.id, RepositoryCommandsPlan)
    assert plan.source is RepositoryCommandsSource.DERIVED
    assert plan.install == (_UV_INSTALL,)
    assert plan.verify == (_RUFF_FORMAT, _PYTEST)
    probe = [_UV_INSTALL, _RUFF_FORMAT, _RUFF_LINT, _PYTEST]
    assert runner.commands[: len(probe)] == probe
    assert runner.commands[len(probe) :] == [_UV_INSTALL, _RUFF_FORMAT, _PYTEST]


def test_turning_derivation_off_runs_no_repository_code(
    uv_python_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner()
    config = _config(data_dir)
    config = config.model_copy(
        update={"repository": config.repository.model_copy(update={"derive_commands": False})}
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(), verifier=runner)

    run = controller.run(_work_item("WI-derivation-off"), uv_python_repo)

    assert store.load_artifact(run.id, RepositoryCommandsPlan) == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.NONE, notes=("command derivation is turned off",)
    )
    assert runner.commands == []


def test_repository_without_a_language_lane_records_why(source_repo: Path, data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime())

    run = controller.run(_work_item("WI-no-commands"), source_repo)

    assert store.load_artifact(run.id, RepositoryCommandsPlan) == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.NONE, notes=("no supported language lane",)
    )


@pytest.mark.parametrize("error", [OSError, ValueError, WorkspaceError])
def test_command_runner_error_degrades_and_the_run_continues(
    uv_python_repo: Path, data_dir: Path, error: type[Exception]
) -> None:
    runner = _ScriptedCommandRunner(raises=frozenset({_UV_INSTALL}), error=error)
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, FakeAgentRuntime(), verifier=runner)

    run = controller.run(_work_item("WI-derived-degraded"), uv_python_repo)

    assert store.load_artifact(run.id, RepositoryCommandsPlan) == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.NONE,
        notes=(f"command derivation degraded: {error.__name__}",),
    )
    assert run.attempt_records, "the run went on to implementation"


def test_runs_without_a_commands_plan_use_the_configuration(
    source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir, verify=["true"]), store, FakeAgentRuntime())
    run = controller.run(_work_item("WI-legacy-commands"), source_repo)
    (store.run_dir(run.id) / ARTIFACT_FILENAMES[RepositoryCommandsPlan]).unlink()

    assert controller._commands_for_run(run.id).verify == ["true"]


@pytest.fixture
def uv_mutmut_repo(uv_python_repo: Path) -> Path:
    (uv_python_repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[dependency-groups]\ndev = ["pytest", "ruff", "mutmut"]\n\n'
        "[tool.ruff]\n",
        encoding="utf-8",
    )
    _git(uv_python_repo, "commit", "-qam", "add mutmut")
    return uv_python_repo


def _python_editing_implementer(request: AgentRequest) -> AgentResult:
    result = FakeAgentRuntime().run(request)
    assert request.workspace_path is not None
    source = Path(request.workspace_path) / "src"
    source.mkdir(exist_ok=True)
    (source / "app.py").write_text("x = 2\n", encoding="utf-8")
    return result


def _docs_editing_implementer(request: AgentRequest) -> AgentResult:
    result = FakeAgentRuntime().run(request)
    assert request.workspace_path is not None
    (Path(request.workspace_path) / "NOTES.md").write_text("notes\n", encoding="utf-8")
    return result


_MUTMUT_RUN_APP = "uv run --no-sync mutmut run 'app.*'"


def _mutation_controller(
    data_dir: Path,
    runner: _ScriptedCommandRunner,
    implementer: Callable[[AgentRequest], AgentResult] = _python_editing_implementer,
    **repository: object,
) -> tuple[WorkflowController, FileRunStore]:
    config = _config(data_dir)
    config = config.model_copy(
        update={"repository": config.repository.model_copy(update=repository)}
    )
    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime(implementer=implementer)
    return WorkflowController(config, store, runtime, verifier=runner), store


def test_mutation_gate_adds_an_advisory_check_for_changed_python(
    uv_mutmut_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner()
    controller, store = _mutation_controller(data_dir, runner)

    run = controller.run(_work_item("WI-mutation-gate"), uv_mutmut_repo)

    assert _MUTMUT_RUN_APP in runner.commands
    report = store.load_artifact(run.id, MutationReport)
    assert report.modules == ("app",)
    verification = store.load_artifact(run.id, VerificationReport)
    assert verification.passed is True
    assert verification.deterministic_checks[-1].command == "mutmut run app"


def test_mutation_gate_error_is_recorded_and_does_not_fail_the_run(
    uv_mutmut_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner(raises=frozenset({_MUTMUT_RUN_APP}))
    controller, store = _mutation_controller(data_dir, runner)

    run = controller.run(_work_item("WI-mutation-error"), uv_mutmut_repo)

    assert store.load_artifact(run.id, MutationReport) == MutationReport(
        status=MutationStatus.SKIPPED, modules=("app",), reason="mutation gate error: OSError"
    )
    assert store.load_artifact(run.id, VerificationReport).passed is True


def test_mutation_gate_records_a_change_without_python_sources(
    uv_mutmut_repo: Path, data_dir: Path
) -> None:
    runner = _ScriptedCommandRunner()
    controller, store = _mutation_controller(data_dir, runner, _docs_editing_implementer)

    run = controller.run(_work_item("WI-mutation-docs"), uv_mutmut_repo)

    assert store.load_artifact(run.id, MutationReport) == MutationReport(
        status=MutationStatus.SKIPPED, reason="no changed Python source modules"
    )
    assert not any("mutmut" in command for command in runner.commands)


@pytest.mark.parametrize("switch", ["mutation_gate", "derive_commands"])
def test_mutation_gate_is_off_without_the_switch_or_verify_commands(
    uv_mutmut_repo: Path, data_dir: Path, switch: str
) -> None:
    runner = _ScriptedCommandRunner()
    controller, _store = _mutation_controller(data_dir, runner, **{switch: False})

    controller.run(_work_item("WI-mutation-off"), uv_mutmut_repo)

    assert not any("mutmut" in command for command in runner.commands)
