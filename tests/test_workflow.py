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
from pathlib import Path
from typing import Callable

import pytest
from pydantic import ValidationError

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.governance import (
    CheckPhase,
    RepositoryVerificationResult,
    VerificationFailureKind,
)
from software_agent_factory.models import (
    GENERIC_SKILL_TARGET,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    ChangeSet,
    Complexity,
    ContextTier,
    DependencyEcosystem,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanStep,
    RepairContext,
    RepositoryDependency,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillOverlay,
    RepositorySkillUse,
    ResearchReport,
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
    RunLease,
    SkillGuidance,
    SkillOverlayMode,
    SkillSelectionSource,
    SkillSource,
    SkillTarget,
    Specification,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from software_agent_factory.observability import _compute_aggregate_metrics
from software_agent_factory.repository_skills import RepositorySkillManager
from software_agent_factory.store import ArtifactModel, FileRunStore
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
    performance_mode: str = "standard",
    fast_model_profile: str = "economy",
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
                "refiner": {"model": "claude-opus-5", "reasoning": "high"},
                "researcher": {"model": "gpt-5.6-sol", "reasoning": "high"},
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
                    "refiner": {"model": "gpt-5.6-terra", "reasoning": "high"},
                    "researcher": {"model": "gemini-3.8-flash", "reasoning": "medium"},
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
            "performance": {
                "mode": performance_mode,
                "fast_model_profile": fast_model_profile,
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
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=complexity,
                risk=risk,
                requirements_quality="clear",
                needs_research=needs_research,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
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
        AgentRole.REFINER,
        AgentRole.RESEARCHER,
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
    assert run.invocation_records[4].budget is AttemptBudget.IMPLEMENTATION
    assert run.invocation_records[5].attempt_number == 1
    assert run.invocation_records[6].attempt_number == 1
    assert run.attempt_records[0].invocation_number == 5
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
                    "PLANNER response did not validate as ExecutionPlan: "
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
    assert "Return one complete ExecutionPlan JSON object" in requests[1].repair_context
    assert all(active is not None for active in active_invocations)
    assert [active.attempt_number for active in active_invocations if active is not None] == [1, 2]
    assert run.active_invocation is None


def test_planner_retries_once_after_writing_policy_failure(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def planner(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if len(requests) == 1:
            return AgentResult(
                role=AgentRole.PLANNER,
                success=True,
                execution_plan=ExecutionPlan(
                    summary="Use a robust and comprehensive implementation.",
                    steps=[PlanStep(id="one", goal="Change the parser.")],
                    expected_scope=ExpectedScope(
                        modules=["FACTORY_NOTES.md"],
                        estimated_files_min=1,
                        estimated_files_max=1,
                    ),
                ),
            )
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(planner=planner),
    ).run(_work_item("WI-planner-writing-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(requests) == 2
    assert isinstance(requests[1].repair_context, str)
    assert "ExecutionPlan did not satisfy writing policy" in requests[1].repair_context
    assert "Rewrite only the prose fields" in requests[1].repair_context
    assert "Use a robust and comprehensive implementation." in requests[1].repair_context
    assert '"FACTORY_NOTES.md"' in requests[1].repair_context


def test_planner_allows_only_one_writing_policy_correction(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []

    def planner(request: AgentRequest) -> AgentResult:
        requests.append(request)
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Use a robust and comprehensive implementation.",
                steps=[PlanStep(id="one", goal="Change the parser.")],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=1,
                ),
            ),
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=4),
        FileRunStore(data_dir),
        FakeAgentRuntime(planner=planner),
    ).run(_work_item("WI-planner-writing-limit"), source_repo)

    assert run.state is WorkflowState.FAILED
    assert len(requests) == 2


def test_implementer_allows_only_one_writing_policy_correction(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []

    def implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="Use a robust and comprehensive implementation."),
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=4, max_total_attempts=4),
        FileRunStore(data_dir),
        FakeAgentRuntime(implementer=implementer),
    ).run(_work_item("WI-implementer-writing-limit"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(requests) == 2
    assert requests[1].repair_context is not None
    assert "Previous rejected artifact" in requests[1].repair_context.failures[0]
    assert (
        "Use a robust and comprehensive implementation." in (requests[1].repair_context.failures[0])
    )


def test_implementer_change_set_prose_correction_succeeds_without_extra_attempt(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []

    def implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            assert request.change_set is not None
            assert request.diff is None
            assert request.repair_context is not None
            assert (
                "Do not edit, add, or remove any source files or workspace files. "
                "Source edits are strictly forbidden for this artifact-only correction."
            ) in request.repair_context.summary
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=True,
                change_set=ChangeSet(
                    summary="Add requested notes.",
                    tests_added=[],
                    commands_run=[],
                ),
            )
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "FACTORY_NOTES.md").write_text("repaired notes\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Use a robust and comprehensive implementation.",
                tests_added=["tests/test_notes.py"],
                commands_run=["echo 1"],
            ),
        )

    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=2),
        store,
        FakeAgentRuntime(implementer=implementer),
    ).run(_work_item("WI-change-set-correction"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(requests) == 2
    assert requests[0].purpose is AgentPurpose.STANDARD
    assert requests[1].purpose is AgentPurpose.CORRECT_CHANGE_SET
    assert len(run.attempt_records) == 1
    assert run.attempt_records[0].attempt_number == 1
    assert run.attempt_records[0].outcome == "succeeded"
    saved_patch = store.load_patch(run.id, attempt=1)
    assert "repaired notes" in saved_patch
    saved_change_set = store.load_artifact(run.id, ChangeSet, attempt=1)
    assert saved_change_set.summary == "Add requested notes."
    assert saved_change_set.tests_added == ["tests/test_notes.py"]
    assert saved_change_set.commands_run == ["echo 1"]
    assert saved_change_set.changed_files == ["FACTORY_NOTES.md"]
    assert run.performance.counters.get("rework.change_set_artifact_correction") == 1


def test_implementer_change_set_correction_is_capped_at_one_across_attempts(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []
    attempt_count = 0

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal attempt_count
        requests.append(request)
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=True,
                change_set=ChangeSet(summary="Add clean notes."),
            )
        attempt_count += 1
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "FACTORY_NOTES.md").write_text(f"work {attempt_count}\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="Use a robust and comprehensive implementation."),
        )

    config = _config(
        data_dir,
        verify=["test -f pass_flag.txt"],
        same_model_attempts=2,
        max_total_attempts=3,
    )
    run = WorkflowController(
        config,
        FileRunStore(data_dir),
        FakeAgentRuntime(implementer=implementer),
    ).run(_work_item("WI-correction-capped"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    correction_requests = [r for r in requests if r.purpose is AgentPurpose.CORRECT_CHANGE_SET]
    assert len(correction_requests) == 1


def test_implementer_change_set_correction_rejected_if_worktree_diff_changes(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []

    def implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        assert request.workspace_path is not None
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            (Path(request.workspace_path) / "unexpected.txt").write_text("sneaky edit\n")
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=True,
                change_set=ChangeSet(summary="Add clean notes."),
            )
        (Path(request.workspace_path) / "FACTORY_NOTES.md").write_text("initial edit\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="Use a robust and comprehensive implementation."),
        )

    run = WorkflowController(
        _config(data_dir, same_model_attempts=1, max_total_attempts=1),
        FileRunStore(data_dir),
        FakeAgentRuntime(implementer=implementer),
    ).run(_work_item("WI-diff-change-rejected"), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(requests) == 2
    assert requests[1].purpose is AgentPurpose.CORRECT_CHANGE_SET
    assert len(run.attempt_records) == 1
    assert run.attempt_records[0].outcome == "failed"
    assert (
        "ChangeSet correction changed the Git diff; the artifact-only correction was rejected"
        in (run.attempt_records[0].failure_reason or "")
    )


def test_triage_retries_once_after_writing_policy_failure(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def triage(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if len(requests) == 1:
            return AgentResult(
                role=AgentRole.TRIAGE,
                success=True,
                triage_result=TriageResult(
                    factory_eligible=True,
                    complexity=Complexity.L1,
                    risk=Risk.R1,
                    requirements_quality="A robust and comprehensive task.",
                    needs_research=False,
                    confidence=0.8,
                ),
            )
        return default_runtime.run(request)

    run = WorkflowController(
        _config(data_dir, same_model_attempts=2),
        FileRunStore(data_dir),
        FakeAgentRuntime(triage=triage),
    ).run(_work_item("WI-triage-writing-retry"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(requests) == 2
    assert isinstance(requests[1].repair_context, str)
    assert "TriageResult did not satisfy writing policy" in requests[1].repair_context


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
    assert [request.role for request in runtime.requests] == [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
        AgentRole.PLANNER,
        AgentRole.IMPLEMENTER,
        AgentRole.RESEARCHER,
        AgentRole.IMPLEMENTER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ]
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    skill_request = runtime.requests[4]
    assert skill_request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    polish_request = runtime.requests[5]
    assert isinstance(polish_request.repair_context, RepairContext)
    assert polish_request.repair_context.trigger is AttemptTrigger.POLISH
    assert "reusable repository guidance" in polish_request.repair_context.summary
    assert "Simplify first, then apply version-specific polish." in (
        polish_request.repair_context.summary
    )
    assert polish_request.repository_skill is not None
    assert store.load_artifact(run.id, RepositorySkill) == polish_request.repository_skill
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
    assert all(
        request.purpose is not AgentPurpose.GENERATE_REPOSITORY_SKILL
        for request in runtime.requests
    )
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]


def test_post_green_research_exception_gets_one_retry(source_repo: Path, data_dir: Path) -> None:
    class RaisingResearchRuntime:
        def __init__(self) -> None:
            self.delegate = FakeAgentRuntime()
            self.generation_calls = 0

        def run(self, request: AgentRequest) -> AgentResult:
            if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
                self.generation_calls += 1
                raise RuntimeError("research process unavailable")
            return self.delegate.run(request)

    store = FileRunStore(data_dir)
    runtime = RaisingResearchRuntime()
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    )

    run = controller.run(_work_item("WI-research-exception"), source_repo)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "repository skill generation failed after 2 attempts" in warning
        and "repository skill research could not run: research process unavailable" in warning
        for warning in profile.warnings
    )
    assert runtime.generation_calls == 2


def test_invalid_repository_skill_gets_one_bounded_correction(
    source_repo: Path, data_dir: Path
) -> None:
    class CorrectingResearchRuntime:
        def __init__(self) -> None:
            self.delegate = FakeAgentRuntime()
            self.requests: list[AgentRequest] = []
            self.generation_calls = 0

        def run(self, request: AgentRequest) -> AgentResult:
            self.requests.append(request)
            if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
                self.generation_calls += 1
                if self.generation_calls == 1:
                    return AgentResult(
                        role=AgentRole.RESEARCHER,
                        success=False,
                        failure_reason=(
                            "RESEARCHER response did not validate as RepositorySkill: "
                            "practice sources must use the version scope 'general'"
                        ),
                    )
            return self.delegate.run(request)

    runtime = CorrectingResearchRuntime()
    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    ).run(_work_item("WI-skill-correction"), source_repo)

    generation_requests = [
        request
        for request in runtime.requests
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    ]
    assert run.state is WorkflowState.PR_READY
    assert len(generation_requests) == 2
    assert generation_requests[0].repair_context is None
    assert isinstance(generation_requests[1].repair_context, str)
    assert "Failure reason" in generation_requests[1].repair_context
    assert "practice sources must use the version scope 'general'" in (
        generation_requests[1].repair_context
    )
    assert "version_scope exactly 'general'" in generation_requests[1].repair_context
    assert [
        (record.success, record.attempt_number)
        for record in run.invocation_records
        if record.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    ] == [(False, 1), (True, 2)]
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    assert store.load_artifact(run.id, RepositorySkill)


def test_two_invalid_repository_skills_safely_skip_polish(
    source_repo: Path, data_dir: Path
) -> None:
    class InvalidResearchRuntime:
        def __init__(self) -> None:
            self.delegate = FakeAgentRuntime()
            self.generation_requests: list[AgentRequest] = []

        def run(self, request: AgentRequest) -> AgentResult:
            if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
                self.generation_requests.append(request)
                return AgentResult(
                    role=AgentRole.RESEARCHER,
                    success=False,
                    failure_reason="repository skill schema remained invalid",
                )
            return self.delegate.run(request)

    runtime = InvalidResearchRuntime()
    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    ).run(_work_item("WI-skill-correction-exhausted"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(runtime.generation_requests) == 2
    assert runtime.generation_requests[0].repair_context is None
    assert runtime.generation_requests[1].repair_context is not None
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "repository skill generation failed after 2 attempts; "
        "initial rejection: repository skill schema remained invalid; "
        "correction rejection: repository skill schema remained invalid" in warning
        for warning in profile.warnings
    )


def test_repository_skill_process_failure_gets_one_retry(source_repo: Path, data_dir: Path) -> None:
    class FailedProcessRuntime:
        def __init__(self) -> None:
            self.delegate = FakeAgentRuntime()
            self.generation_calls = 0

        def run(self, request: AgentRequest) -> AgentResult:
            if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
                self.generation_calls += 1
                return AgentResult(
                    role=AgentRole.RESEARCHER,
                    success=False,
                    failure_reason="Copilot process exited with code 1",
                )
            return self.delegate.run(request)

    runtime = FailedProcessRuntime()
    store = FileRunStore(data_dir)
    run = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    ).run(_work_item("WI-skill-process-failure"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert runtime.generation_calls == 2
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "repository skill generation failed after 2 attempts" in warning
        and "Copilot process exited with code 1" in warning
        for warning in profile.warnings
    )


def test_invalid_repository_skill_provenance_gets_one_correction(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()

    class CorrectingProvenanceRuntime:
        def __init__(self) -> None:
            self.delegate = FakeAgentRuntime()
            self.generation_requests: list[AgentRequest] = []

        def run(self, request: AgentRequest) -> AgentResult:
            if request.purpose is not AgentPurpose.GENERATE_REPOSITORY_SKILL:
                return self.delegate.run(request)
            self.generation_requests.append(request)
            if len(self.generation_requests) == 1:
                return AgentResult(
                    role=AgentRole.RESEARCHER,
                    success=True,
                    repository_skill=_react_skill(
                        profile,
                        official_sources=(
                            SkillSource(
                                title="Untrusted version guidance",
                                url="https://example.com/react",
                                version_scope="19.1.0",
                                applies_to=("react", "react-dom"),
                            ),
                        ),
                    ),
                )
            return self.delegate.run(request)

    runtime = CorrectingProvenanceRuntime()
    run = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        runtime,
        repository_profiler=lambda path: profile,
    ).run(_work_item("WI-skill-provenance-correction"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert len(runtime.generation_requests) == 2
    correction = runtime.generation_requests[1].repair_context
    assert isinstance(correction, str)
    assert "outside polish.official_documentation_origins" in correction
    assert [
        (record.success, record.attempt_number)
        for record in run.invocation_records
        if record.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    ] == [(True, 1), (True, 2)]
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]


def test_refreshed_profile_persistence_failure_skips_skill_selection(
    source_repo: Path, data_dir: Path
) -> None:
    class RefreshedProfileFailingStore(FileRunStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.profile_writes = 0

        def save_artifact(
            self,
            run_id: str,
            artifact: ArtifactModel,
            filename: str | None = None,
            *,
            attempt: int | None = None,
        ) -> Path:
            if isinstance(artifact, RepositoryProfile):
                self.profile_writes += 1
                if self.profile_writes == 2:
                    raise OSError("profile storage unavailable")
            return super().save_artifact(run_id, artifact, filename, attempt=attempt)

    runtime = RecordingRuntime(FakeAgentRuntime())
    store = RefreshedProfileFailingStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
    )

    run = controller.run(_work_item("WI-profile-persistence"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert all(
        request.purpose is not AgentPurpose.GENERATE_REPOSITORY_SKILL
        for request in runtime.requests
    )
    profile = FileRunStore(data_dir).load_artifact(run.id, RepositoryProfile)
    assert any(
        "refreshed repository profile could not be persisted" in warning
        for warning in profile.warnings
    )


def test_repository_skill_rejects_sources_outside_factory_allowlist(
    source_repo: Path, data_dir: Path
) -> None:
    def researcher(request: AgentRequest) -> AgentResult:
        assert request.repository_profile is not None
        return AgentResult(
            role=AgentRole.RESEARCHER,
            success=True,
            repository_skill=RepositorySkill(
                dependency_fingerprint=request.repository_profile.dependency_fingerprint,
                official_sources=(
                    SkillSource(
                        title="Untrusted advice",
                        url="https://example.com/react",
                        version_scope="19",
                        applies_to=("react",),
                    ),
                ),
                simplify=SkillGuidance(
                    summary="Simplify.",
                    guidance=("Use direct code.",),
                ),
                polish=SkillGuidance(
                    summary="Polish.",
                    guidance=("Use current APIs.",),
                ),
            ),
        )

    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(researcher=researcher),
    )

    store = FileRunStore(data_dir)
    run = controller.run(_work_item("WI-disallowed-source"), source_repo)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "outside polish.official_documentation_origins" in warning for warning in profile.warnings
    )


PRACTICE_REFERENCE_URL = (
    "https://raw.githubusercontent.com/bdfinst/agentic-dev-team/"
    "52cc5efd1c445e71c55b956837c003911346d7e7/"
    "plugins/dev-team/agents/quality-reviewer.md"
)


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


def _react_skill(
    profile: RepositoryProfile,
    *,
    official_sources: tuple[SkillSource, ...],
    practice_sources: tuple[SkillSource, ...] = (),
) -> RepositorySkill:
    return RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        targets=tuple(
            SkillTarget(
                ecosystem=dependency.ecosystem,
                name=dependency.name,
                declared_version=dependency.declared_version,
                resolved_version=dependency.resolved_version,
                evidence=(dependency.manifest_path,),
            )
            for dependency in profile.dependencies
        ),
        official_sources=official_sources,
        practice_sources=practice_sources,
        simplify=SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
        uncertainties=("Fixture skill.",),
    )


def _skill_run(
    source_repo: Path,
    data_dir: Path,
    work_item_id: str,
    skill_factory: Callable[[RepositoryProfile], RepositorySkill],
) -> tuple[FactoryRun, FileRunStore, RecordingRuntime]:
    profile = _react_profile()

    def researcher(request: AgentRequest) -> AgentResult:
        assert request.repository_profile is not None
        return AgentResult(
            role=AgentRole.RESEARCHER,
            success=True,
            repository_skill=skill_factory(request.repository_profile),
        )

    runtime = RecordingRuntime(FakeAgentRuntime(researcher=researcher))
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
        repository_profiler=lambda path: profile,
    )
    return controller.run(_work_item(work_item_id), source_repo), store, runtime


def test_official_source_may_ground_several_detected_dependency_names(
    source_repo: Path, data_dir: Path
) -> None:
    def skill(profile: RepositoryProfile) -> RepositorySkill:
        return _react_skill(
            profile,
            official_sources=(
                SkillSource(
                    title="React documentation",
                    url="https://react.dev/reference/react",
                    version_scope="19.1.0",
                    applies_to=("react", "react-dom"),
                ),
            ),
            practice_sources=(
                SkillSource(
                    title="Quality review heuristics",
                    url=PRACTICE_REFERENCE_URL,
                    version_scope="general",
                    applies_to=(GENERIC_SKILL_TARGET,),
                ),
            ),
        )

    run, store, runtime = _skill_run(source_repo, data_dir, "WI-shared-source", skill)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert not any("polish skipped" in warning for warning in profile.warnings)
    accepted = store.load_artifact(run.id, RepositorySkill)
    assert accepted.official_sources[0].applies_to == ("react", "react-dom")
    polish_request = next(
        request
        for request in runtime.requests
        if request.role is AgentRole.IMPLEMENTER
        and isinstance(request.repair_context, RepairContext)
        and request.repair_context.trigger is AttemptTrigger.POLISH
    )
    assert polish_request.repository_skill == accepted


def test_official_source_claiming_an_undetected_dependency_is_rejected(
    source_repo: Path, data_dir: Path
) -> None:
    def skill(profile: RepositoryProfile) -> RepositorySkill:
        return _react_skill(
            profile,
            official_sources=(
                SkillSource(
                    title="React documentation",
                    url="https://react.dev/reference/react",
                    version_scope="19.1.0",
                    applies_to=("react", "react-dom", "next"),
                ),
            ),
        )

    run, store, runtime = _skill_run(source_repo, data_dir, "WI-undetected-target", skill)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "official source for dependencies that are not in the repository profile" in warning
        and "next" in warning
        for warning in profile.warnings
    )
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkill)
    assert all(
        request.repository_skill is None
        for request in runtime.requests
        if request.role in {AgentRole.IMPLEMENTER, AgentRole.TESTER, AgentRole.REVIEWER}
    )


def test_required_version_target_without_official_source_is_rejected(
    source_repo: Path, data_dir: Path
) -> None:
    def skill(profile: RepositoryProfile) -> RepositorySkill:
        return _react_skill(
            profile,
            official_sources=(
                SkillSource(
                    title="React documentation",
                    url="https://react.dev/reference/react",
                    version_scope="19.1.0",
                    applies_to=("react",),
                ),
            ),
        )

    run, store, _ = _skill_run(source_repo, data_dir, "WI-ungrounded-target", skill)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "without official source provenance for: react-dom" in warning
        for warning in profile.warnings
    )
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkill)


def test_all_recognized_dependency_versions_must_be_targeted(data_dir: Path) -> None:
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
        version_files=("package.json", "apps/legacy/package.json"),
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
                name="react",
                declared_version="18.3.1",
                manifest_path="apps/legacy/package.json",
                group="dependencies",
            ),
        ),
    )
    skill = RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        targets=(
            SkillTarget(
                ecosystem=DependencyEcosystem.NPM,
                name="react",
                declared_version="19.1.0",
                evidence=("package.json",),
            ),
        ),
        official_sources=(
            SkillSource(
                title="React documentation",
                url="https://react.dev/reference/react",
                version_scope="19.1.0",
                applies_to=("react",),
            ),
        ),
        simplify=SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
    )

    error = controller._repository_skill_validation_error(skill, profile)

    assert error is not None
    assert "react@18.3.1" in error


def test_all_recognized_dependency_versions_must_be_targeted_and_may_be_covered(
    data_dir: Path,
) -> None:
    """Targeting every detected identity of a recognized name is accepted."""
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
        version_files=("package.json", "apps/legacy/package.json"),
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
                name="react",
                declared_version="18.3.1",
                manifest_path="apps/legacy/package.json",
                group="dependencies",
            ),
        ),
    )
    skill = RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        targets=(
            SkillTarget(
                ecosystem=DependencyEcosystem.NPM,
                name="react",
                declared_version="19.1.0",
                evidence=("package.json",),
            ),
            SkillTarget(
                ecosystem=DependencyEcosystem.NPM,
                name="react",
                declared_version="18.3.1",
                evidence=("apps/legacy/package.json",),
            ),
        ),
        official_sources=(
            SkillSource(
                title="React documentation",
                url="https://react.dev/reference/react",
                version_scope="18.3.1, 19.1.0",
                applies_to=("react",),
            ),
        ),
        simplify=SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
    )

    assert controller._repository_skill_validation_error(skill, profile) is None


def test_more_recognized_versions_than_the_target_bound_safely_skip(data_dir: Path) -> None:
    """The bounded target list cannot cover >24 identities, so guidance is skipped."""
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    manifests = tuple(f"packages/app{index:02d}/package.json" for index in range(25))
    profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
        version_files=manifests,
        dependencies=tuple(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.NPM,
                name="react",
                declared_version=f"19.1.{index}",
                manifest_path=manifest,
                group="dependencies",
            )
            for index, manifest in enumerate(manifests)
        ),
    )
    targets = tuple(
        SkillTarget(
            ecosystem=DependencyEcosystem.NPM,
            name="react",
            declared_version=f"19.1.{index}",
            evidence=(manifest,),
        )
        for index, manifest in enumerate(manifests)
    )
    official_sources = (
        SkillSource(
            title="React documentation",
            url="https://react.dev/reference/react",
            version_scope="19.1.x",
            applies_to=("react",),
        ),
    )
    guidance = {
        "simplify": SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        "polish": SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
    }

    # The typed artifact itself bounds how many versions may be targeted.
    with pytest.raises(ValidationError):
        RepositorySkill(
            dependency_fingerprint=profile.dependency_fingerprint,
            targets=targets,
            official_sources=official_sources,
            **guidance,
        )

    skill = RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        targets=targets[:24],
        official_sources=official_sources,
        **guidance,
    )

    error = controller._repository_skill_validation_error(skill, profile)

    assert error is not None
    assert "react@19.1.24" in error


def test_fake_runtime_guidance_satisfies_controller_provenance_rules(data_dir: Path) -> None:
    """The fake double must only target dependency names it can officially ground."""
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
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
            RepositoryDependency(
                ecosystem=DependencyEcosystem.NPM,
                name="left-pad",
                declared_version="1.3.0",
                manifest_path="package.json",
                group="dependencies",
            ),
        ),
    )
    result = FakeAgentRuntime().run(
        AgentRequest(
            role=AgentRole.RESEARCHER,
            purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
            model="gpt-5.6-sol",
            reasoning="high",
            work_item=_work_item("WI-fake-provenance"),
            repository_profile=profile,
            official_documentation_origins=list(
                _config(data_dir).polish.official_documentation_origins
            ),
            timeout_seconds=60,
        )
    )

    skill = result.repository_skill
    assert skill is not None
    assert {target.name for target in skill.targets} == {"react", "react-dom"}
    assert controller._repository_skill_validation_error(skill, profile) is None


def test_practice_source_with_a_version_claim_is_rejected_at_the_controller(
    data_dir: Path,
) -> None:
    """Defense in depth: curated references may not carry a framework version."""
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    profile = RepositoryProfile(manifest_fingerprint="a" * 64, dependency_fingerprint="b" * 64)
    skill = RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        practice_sources=(
            SkillSource(
                title="Quality review heuristics",
                url=PRACTICE_REFERENCE_URL,
                version_scope="General",
                applies_to=(GENERIC_SKILL_TARGET,),
            ),
        ),
        simplify=SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
        uncertainties=("No official source was consulted.",),
    )

    # A case-insensitive 'general' scope stays acceptable.
    assert controller._repository_skill_validation_error(skill, profile) is None

    versioned = skill.model_copy(
        update={
            "practice_sources": (
                SkillSource(
                    title="Quality review heuristics",
                    url=PRACTICE_REFERENCE_URL,
                    version_scope="react 19.1.0",
                    applies_to=(GENERIC_SKILL_TARGET,),
                ),
            )
        }
    )

    error = controller._repository_skill_validation_error(versioned, profile)

    assert error is not None
    assert "practice source carrying a version claim" in error


def test_every_version_specific_target_requires_official_grounding(data_dir: Path) -> None:
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
    )
    profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
        version_files=("package.json",),
        dependencies=(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.NPM,
                name="typescript",
                declared_version="5.9.0",
                manifest_path="package.json",
                group="devDependencies",
            ),
        ),
    )
    skill = RepositorySkill(
        dependency_fingerprint=profile.dependency_fingerprint,
        targets=(
            SkillTarget(
                ecosystem=DependencyEcosystem.NPM,
                name="typescript",
                declared_version="5.9.0",
                evidence=("package.json",),
            ),
        ),
        simplify=SkillGuidance(summary="Simplify.", guidance=("Use direct code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use current APIs.",)),
        uncertainties=("No official source was returned.",),
    )

    error = controller._repository_skill_validation_error(skill, profile)

    assert error is not None
    assert "without official source provenance for: typescript" in error


def test_practice_source_outside_the_reference_allowlist_is_still_rejected(
    source_repo: Path, data_dir: Path
) -> None:
    def skill(profile: RepositoryProfile) -> RepositorySkill:
        return _react_skill(
            profile,
            official_sources=(
                SkillSource(
                    title="React documentation",
                    url="https://react.dev/reference/react",
                    version_scope="19.1.0",
                    applies_to=("react", "react-dom"),
                ),
            ),
            practice_sources=(
                SkillSource(
                    title="Untrusted heuristics",
                    url="https://example.com/review.md",
                    version_scope="general",
                    applies_to=(GENERIC_SKILL_TARGET,),
                ),
            ),
        )

    run, store, _ = _skill_run(source_repo, data_dir, "WI-practice-allowlist", skill)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert any("outside polish.practice_reference_urls" in warning for warning in profile.warnings)
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkill)


def test_dependency_change_during_polish_rejects_stale_skill(
    source_repo: Path, data_dir: Path
) -> None:
    profiles = iter(
        [
            RepositoryProfile(
                manifest_fingerprint="1" * 64,
                dependency_fingerprint="1" * 64,
            ),
            RepositoryProfile(
                manifest_fingerprint="2" * 64,
                dependency_fingerprint="2" * 64,
            ),
            RepositoryProfile(
                manifest_fingerprint="3" * 64,
                dependency_fingerprint="3" * 64,
            ),
        ]
    )

    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(),
        repository_profiler=lambda path: next(profiles),
    )

    run = controller.run(_work_item("WI-stale-polish"), source_repo)

    assert run.state is WorkflowState.PR_READY
    profile = FileRunStore(data_dir).load_artifact(run.id, RepositoryProfile)
    assert any("dependency versions changed" in warning for warning in profile.warnings)


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


def test_post_green_research_uses_versions_changed_by_initial_implementation(
    source_repo: Path, data_dir: Path
) -> None:
    profiles = iter(
        [
            RepositoryProfile(
                manifest_fingerprint="1" * 64,
                dependency_fingerprint="1" * 64,
                dependencies=(
                    RepositoryDependency(
                        ecosystem=DependencyEcosystem.NPM,
                        name="react",
                        declared_version="18.3.0",
                        manifest_path="package.json",
                        group="dependencies",
                    ),
                ),
            ),
            RepositoryProfile(
                manifest_fingerprint="2" * 64,
                dependency_fingerprint="2" * 64,
                dependencies=(
                    RepositoryDependency(
                        ecosystem=DependencyEcosystem.NPM,
                        name="react",
                        declared_version="19.1.0",
                        manifest_path="package.json",
                        group="dependencies",
                    ),
                ),
            ),
            RepositoryProfile(
                manifest_fingerprint="2" * 64,
                dependency_fingerprint="2" * 64,
                dependencies=(
                    RepositoryDependency(
                        ecosystem=DependencyEcosystem.NPM,
                        name="react",
                        declared_version="19.1.0",
                        manifest_path="package.json",
                        group="dependencies",
                    ),
                ),
            ),
        ]
    )

    def changing_profiler(path: Path) -> RepositoryProfile:
        assert path.is_dir()
        return next(profiles)

    runtime = RecordingRuntime(FakeAgentRuntime())
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
        repository_profiler=changing_profiler,
    )

    run = controller.run(_work_item("WI-capabilities"), source_repo)

    assert run.state is WorkflowState.PR_READY
    profile = store.load_artifact(run.id, RepositoryProfile)
    assert {item.name: item.declared_version for item in profile.dependencies} == {
        "react": "19.1.0"
    }
    skill_request = next(
        request
        for request in runtime.requests
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    )
    assert skill_request.repository_profile is not None
    assert skill_request.repository_profile.dependencies[0].declared_version == "19.1.0"
    assert skill_request.repository_profile.manifest_fingerprint == "2" * 64

    generated_skill = store.load_artifact(run.id, RepositorySkill)
    assert generated_skill.dependency_fingerprint == profile.dependency_fingerprint
    polish_request = next(
        request
        for request in runtime.requests
        if request.role is AgentRole.IMPLEMENTER
        and isinstance(request.repair_context, RepairContext)
        and request.repair_context.trigger is AttemptTrigger.POLISH
    )
    tester_request = next(
        request for request in runtime.requests if request.role is AgentRole.TESTER
    )
    reviewer_request = next(
        request for request in runtime.requests if request.role is AgentRole.REVIEWER
    )
    assert polish_request.repository_skill == generated_skill
    assert tester_request.repository_skill == generated_skill
    assert reviewer_request.repository_skill == generated_skill


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

OVERLAY_YAML = """mode: extend

polish:
  summary: House rules for polish in this service.
  guidance:
    - Name tests after the behaviour they pin, not the function they call.
"""

EDITED_OVERLAY_YAML = """mode: extend

polish:
  summary: Revised house rules for polish in this service.
  guidance:
    - Keep assertions in one place per behaviour.
"""

OVERLAY_POLISH_GUIDANCE = "Name tests after the behaviour they pin, not the function they call."
EDITED_OVERLAY_POLISH_GUIDANCE = "Keep assertions in one place per behaviour."

SNAPSHOT_FILENAMES = (
    "repository-skill.json",
    "repository-skill-use.json",
    "repository-skill-overlay.json",
)


class StateRecordingStore(FileRunStore):
    """Records every persisted workflow state, so a test can assert that a
    state (``RESEARCHING``) was never entered at all."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.states: list[WorkflowState] = []

    def save_run(self, run: FactoryRun) -> Path:
        self.states.append(run.state)
        return super().save_run(run)


def _skill_storage(data_dir: Path, source_repo: Path) -> RepositorySkillManager:
    return RepositorySkillManager.for_repository(data_dir, source_repo)


def _refresh_hint(source_repo: Path) -> str:
    """The recovery command a warning about unusable stored guidance must name."""
    return f"factory skill refresh --repo {source_repo}"


def _write_overlay(manager: RepositorySkillManager, text: str) -> Path:
    """Write the human-owned overlay the way a human would: by hand."""
    manager.repository_dir.mkdir(parents=True, exist_ok=True)
    manager.overlay_path.write_text(text, encoding="utf-8")
    return manager.overlay_path


def _polish_run(
    source_repo: Path,
    data_dir: Path,
    work_item_id: str,
    *,
    profile: RepositoryProfile,
    store: FileRunStore | None = None,
) -> tuple[FactoryRun, FileRunStore, RecordingRuntime]:
    runtime = RecordingRuntime(FakeAgentRuntime())
    resolved_store = store if store is not None else FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        resolved_store,
        runtime,
        repository_profiler=lambda path: profile,
    )
    return controller.run(_work_item(work_item_id), source_repo), resolved_store, runtime


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


def _generation_requests(runtime: RecordingRuntime) -> list[AgentRequest]:
    return [
        request
        for request in runtime.requests
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL
    ]


def test_generation_request_carries_no_work_item_or_change_evidence(
    source_repo: Path, data_dir: Path
) -> None:
    """Generated guidance is repository-level, so it is produced without any
    knowledge of the work item that happened to trigger generation."""
    run, store, runtime = _polish_run(
        source_repo, data_dir, "WI-generation-inputs", profile=_react_profile()
    )

    assert run.state is WorkflowState.PR_READY
    request = _generation_requests(runtime)[0]
    assert request.changed_files == []
    assert request.diff is None
    assert request.change_set is None
    assert request.specification is None
    assert request.execution_plan is None
    assert request.research_report is None
    assert request.verification_report is None
    assert request.test_report is None
    assert request.repair_context is None
    assert request.repository_profile is not None
    assert request.workspace_path == str(store.run_dir(run.id))
    assert request.workspace_path != run.workspace_path


def test_a_second_run_reuses_stored_guidance_without_researching(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()

    first_run, store, first_runtime = _polish_run(
        source_repo, data_dir, "WI-reuse-first", profile=profile
    )
    second_store = StateRecordingStore(data_dir)
    second_run, _, second_runtime = _polish_run(
        source_repo, data_dir, "WI-reuse-second", profile=profile, store=second_store
    )

    assert first_run.state is WorkflowState.PR_READY
    assert second_run.state is WorkflowState.PR_READY
    assert len(_generation_requests(first_runtime)) == 1
    assert _generation_requests(second_runtime) == []
    assert WorkflowState.RESEARCHING not in second_store.states
    assert [attempt.triggered_by for attempt in second_run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]

    # One shared generated file, byte-identical guidance in both runs.
    manager = _skill_storage(data_dir, source_repo)
    assert manager.list_generated_fingerprints() == (profile.dependency_fingerprint,)
    first_skill = store.load_artifact(first_run.id, RepositorySkill)
    second_skill = store.load_artifact(second_run.id, RepositorySkill)
    assert second_skill == first_skill
    assert manager.load_generated(profile.dependency_fingerprint) == first_skill
    assert all(
        request.repository_skill == second_skill for request in _guidance_consumers(second_runtime)
    )

    assert store.load_artifact(first_run.id, RepositorySkillUse).source is (
        SkillSelectionSource.GENERATED
    )
    reuse = store.load_artifact(second_run.id, RepositorySkillUse)
    assert reuse.source is SkillSelectionSource.REUSED
    assert reuse.repository_key == manager.repository_key
    assert reuse.dependency_fingerprint == profile.dependency_fingerprint
    assert reuse.overlay_hash is None
    assert reuse.overlay_applied is False


def test_a_valid_overlay_reaches_every_post_green_agent_across_runs(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    _write_overlay(manager, OVERLAY_YAML)

    first_run, store, first_runtime = _polish_run(
        source_repo, data_dir, "WI-overlay-first", profile=profile
    )
    second_run, _, second_runtime = _polish_run(
        source_repo, data_dir, "WI-overlay-second", profile=profile
    )

    for run, runtime in ((first_run, first_runtime), (second_run, second_runtime)):
        assert run.state is WorkflowState.PR_READY
        effective = store.load_artifact(run.id, RepositorySkill)
        generated = manager.load_generated(profile.dependency_fingerprint)
        assert generated is not None
        assert OVERLAY_POLISH_GUIDANCE in effective.polish.guidance
        assert OVERLAY_POLISH_GUIDANCE not in generated.polish.guidance
        # The overlay contributes prose only; provenance stays machine-owned.
        assert effective.targets == generated.targets
        assert effective.official_sources == generated.official_sources
        assert effective.dependency_fingerprint == generated.dependency_fingerprint
        assert all(
            request.repository_skill == effective for request in _guidance_consumers(runtime)
        )

        use = store.load_artifact(run.id, RepositorySkillUse)
        assert use.overlay_applied is True
        assert use.overlay_mode is SkillOverlayMode.EXTEND
        assert use.effective_skill_hash != use.generated_skill_hash
        snapshot = store.load_artifact(run.id, RepositorySkillOverlay)
        assert snapshot.polish is not None
        assert snapshot.polish.guidance == (OVERLAY_POLISH_GUIDANCE,)

    profile_artifact = store.load_artifact(second_run.id, RepositoryProfile)
    assert not any("overlay" in warning for warning in profile_artifact.warnings)
    # The factory only ever reads this file.
    assert manager.overlay_path.read_text(encoding="utf-8") == OVERLAY_YAML


def test_an_unusable_overlay_is_preserved_reported_and_bypassed(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    invalid_overlay = "mode: extend\npolish:\n  summary: A section with no guidance list.\n"
    _write_overlay(manager, invalid_overlay)

    run, store, runtime = _polish_run(source_repo, data_dir, "WI-overlay-invalid", profile=profile)

    assert run.state is WorkflowState.PR_READY
    # Generated polish still happened.
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    effective = store.load_artifact(run.id, RepositorySkill)
    assert effective == manager.load_generated(profile.dependency_fingerprint)
    assert all(request.repository_skill == effective for request in _guidance_consumers(runtime))

    profile_artifact = store.load_artifact(run.id, RepositoryProfile)
    overlay_warnings = [
        warning
        for warning in profile_artifact.warnings
        if str(manager.overlay_path) in warning and "was not applied" in warning
    ]
    assert len(overlay_warnings) == 1
    assert "guidance" in overlay_warnings[0]

    use = store.load_artifact(run.id, RepositorySkillUse)
    assert use.overlay_applied is False
    assert use.overlay_hash is None
    assert use.effective_skill_hash == use.generated_skill_hash
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkillOverlay)
    assert manager.overlay_path.read_text(encoding="utf-8") == invalid_overlay


def test_a_dependency_change_generates_new_guidance_while_the_overlay_survives(
    source_repo: Path, data_dir: Path
) -> None:
    manager = _skill_storage(data_dir, source_repo)
    _write_overlay(manager, OVERLAY_YAML)
    first_profile = _react_profile("1" * 64)
    second_profile = _react_profile("2" * 64)

    first_run, store, first_runtime = _polish_run(
        source_repo, data_dir, "WI-fingerprint-first", profile=first_profile
    )
    second_run, _, second_runtime = _polish_run(
        source_repo, data_dir, "WI-fingerprint-second", profile=second_profile
    )

    assert first_run.state is WorkflowState.PR_READY
    assert second_run.state is WorkflowState.PR_READY
    # A new dependency state means new generated guidance, and the earlier
    # generated file stays on disk untouched.
    assert len(_generation_requests(first_runtime)) == 1
    assert len(_generation_requests(second_runtime)) == 1
    assert manager.list_generated_fingerprints() == ("1" * 64, "2" * 64)

    first_use = store.load_artifact(first_run.id, RepositorySkillUse)
    second_use = store.load_artifact(second_run.id, RepositorySkillUse)
    assert first_use.source is SkillSelectionSource.GENERATED
    assert second_use.source is SkillSelectionSource.GENERATED
    assert first_use.dependency_fingerprint == "1" * 64
    assert second_use.dependency_fingerprint == "2" * 64
    assert first_use.generated_skill_hash != second_use.generated_skill_hash

    # The overlay is repository-scoped, so it applies to both.
    assert first_use.overlay_hash == second_use.overlay_hash
    assert first_use.overlay_applied and second_use.overlay_applied
    for run in (first_run, second_run):
        effective = store.load_artifact(run.id, RepositorySkill)
        assert OVERLAY_POLISH_GUIDANCE in effective.polish.guidance
    assert manager.overlay_path.read_text(encoding="utf-8") == OVERLAY_YAML


def test_run_snapshots_are_create_once_and_survive_a_later_overlay_edit(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    _write_overlay(manager, OVERLAY_YAML)

    first_run, store, _ = _polish_run(source_repo, data_dir, "WI-snapshot-first", profile=profile)

    first_dir = store.runs_dir / first_run.id
    recorded = {name: (first_dir / name).read_text(encoding="utf-8") for name in SNAPSHOT_FILENAMES}
    assert all(recorded.values())

    _write_overlay(manager, EDITED_OVERLAY_YAML)
    second_run, _, _ = _polish_run(source_repo, data_dir, "WI-snapshot-second", profile=profile)

    # The earlier run's audit trail describes the earlier run only.
    assert {
        name: (first_dir / name).read_text(encoding="utf-8") for name in SNAPSHOT_FILENAMES
    } == recorded
    second_effective = store.load_artifact(second_run.id, RepositorySkill)
    assert EDITED_OVERLAY_POLISH_GUIDANCE in second_effective.polish.guidance
    assert OVERLAY_POLISH_GUIDANCE not in second_effective.polish.guidance
    assert store.load_artifact(second_run.id, RepositorySkillUse).source is (
        SkillSelectionSource.REUSED
    )


def test_unreadable_stored_guidance_is_never_overwritten_and_safely_skips(
    source_repo: Path, data_dir: Path
) -> None:
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    generated_path = manager.generated_path(profile.dependency_fingerprint)
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = "{ this is not a repository skill"
    generated_path.write_text(corrupt, encoding="utf-8")

    run, store, runtime = _polish_run(source_repo, data_dir, "WI-corrupt-guidance", profile=profile)

    assert run.state is WorkflowState.PR_READY
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    assert _generation_requests(runtime) == []
    assert all(request.repository_skill is None for request in runtime.requests)
    profile_artifact = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "stored repository guidance could not be read and was left unchanged" in warning
        and str(generated_path) in warning
        and _refresh_hint(source_repo) in warning
        for warning in profile_artifact.warnings
    )
    assert generated_path.read_text(encoding="utf-8") == corrupt
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkill)


def test_stored_guidance_that_no_longer_revalidates_is_left_untouched(
    source_repo: Path, data_dir: Path
) -> None:
    """Configuration is authoritative on every load, not only at generation."""
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    stale = _react_skill(
        profile,
        official_sources=(
            SkillSource(
                title="Formerly trusted advice",
                url="https://example.com/react",
                version_scope="19.1.0",
                applies_to=("react", "react-dom"),
            ),
        ),
    )
    generated_path = manager.generated_path(profile.dependency_fingerprint)
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    stored_text = f"{stale.model_dump_json(indent=2)}\n"
    generated_path.write_text(stored_text, encoding="utf-8")

    run, store, runtime = _polish_run(source_repo, data_dir, "WI-stale-guidance", profile=profile)

    assert run.state is WorkflowState.PR_READY
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    assert _generation_requests(runtime) == []
    assert all(request.repository_skill is None for request in runtime.requests)
    profile_artifact = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "did not revalidate and was left unchanged" in warning
        and "outside polish.official_documentation_origins" in warning
        and _refresh_hint(source_repo) in warning
        for warning in profile_artifact.warnings
    )
    assert generated_path.read_text(encoding="utf-8") == stored_text


def test_a_concurrent_winner_is_revalidated_before_use(source_repo: Path, data_dir: Path) -> None:
    """The no-clobber winner -- not this run's own guidance -- is what every
    later run reads, so it must satisfy the same rules before it is used."""
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    generated_path = manager.generated_path(profile.dependency_fingerprint)
    winner = _react_skill(
        profile,
        official_sources=(
            SkillSource(
                title="Untrusted advice",
                url="https://example.com/react",
                version_scope="19.1.0",
                applies_to=("react", "react-dom"),
            ),
        ),
    )
    winner_text = f"{winner.model_dump_json(indent=2)}\n"

    def racing_researcher(request: AgentRequest) -> AgentResult:
        # Another run publishes first, between this run's reuse check and its
        # own create.
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text(winner_text, encoding="utf-8")
        assert request.repository_profile is not None
        return AgentResult(
            role=AgentRole.RESEARCHER,
            success=True,
            repository_skill=_react_skill(
                request.repository_profile,
                official_sources=(
                    SkillSource(
                        title="React documentation",
                        url="https://react.dev/reference/react",
                        version_scope="19.1.0",
                        applies_to=("react", "react-dom"),
                    ),
                ),
            ),
        )

    runtime = RecordingRuntime(FakeAgentRuntime(researcher=racing_researcher))
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir, polish_enabled=True),
        store,
        runtime,
        repository_profiler=lambda path: profile,
    )

    run = controller.run(_work_item("WI-concurrent-winner"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    assert generated_path.read_text(encoding="utf-8") == winner_text
    assert all(request.repository_skill is None for request in runtime.requests)
    profile_artifact = store.load_artifact(run.id, RepositoryProfile)
    assert any(
        "did not revalidate and was left unchanged" in warning
        and str(generated_path) in warning
        and _refresh_hint(source_repo) in warning
        for warning in profile_artifact.warnings
    )
    # The misleading "could not be stored" phrasing must not appear: the
    # winner is a complete file, it simply is not acceptable.
    assert not any("could not be published" in warning for warning in profile_artifact.warnings)
    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, RepositorySkill)


@pytest.mark.parametrize(
    "boundary_error",
    [OSError("read-only file system"), RuntimeError("store unavailable"), ValueError("bad path")],
    ids=["oserror", "runtimeerror", "valueerror"],
)
@pytest.mark.parametrize(
    "failing_artifact",
    [RepositorySkillUse, RepositorySkillOverlay, RepositorySkill],
    ids=["use", "overlay", "skill"],
)
def test_any_snapshot_failure_safely_skips_polish_without_claiming_guidance(
    source_repo: Path,
    data_dir: Path,
    failing_artifact: type[ArtifactModel],
    boundary_error: Exception,
) -> None:
    """Every create-once snapshot is a precondition of polish.

    ``repository-skill.json`` is the run's claim that its agents consumed
    exactly this guidance, so it is written last: no partially written
    snapshot may leave that claim on disk without the provenance record and
    overlay that explain it.
    """
    profile = _react_profile()
    manager = _skill_storage(data_dir, source_repo)
    _write_overlay(manager, OVERLAY_YAML)

    class SnapshotFailingStore(FileRunStore):
        def save_artifact_once(
            self,
            run_id: str,
            artifact: ArtifactModel,
            filename: str | None = None,
        ) -> Path:
            if isinstance(artifact, failing_artifact):
                raise boundary_error
            return super().save_artifact_once(run_id, artifact, filename)

    run, _, runtime = _polish_run(
        source_repo,
        data_dir,
        "WI-snapshot-failure",
        profile=profile,
        store=SnapshotFailingStore(data_dir),
    )

    assert run.state is WorkflowState.PR_READY
    assert [attempt.triggered_by for attempt in run.attempt_records] == [AttemptTrigger.INITIAL]
    assert all(request.repository_skill is None for request in runtime.requests)

    reader = FileRunStore(data_dir)
    profile_artifact = reader.load_artifact(run.id, RepositoryProfile)
    assert any(
        f"repository guidance snapshot could not be persisted: {boundary_error}" in warning
        for warning in profile_artifact.warnings
    )

    run_dir = reader.runs_dir / run.id
    written = {name for name in SNAPSHOT_FILENAMES if (run_dir / name).exists()}
    assert "repository-skill.json" not in written
    if failing_artifact is RepositorySkillUse:
        assert written == set()
    elif failing_artifact is RepositorySkillOverlay:
        assert written == {"repository-skill-use.json"}
    else:
        assert written == {"repository-skill-use.json", "repository-skill-overlay.json"}
    with pytest.raises(FileNotFoundError):
        reader.load_artifact(run.id, RepositorySkill)

    # The shared generated file was still published, so a later run reuses it.
    assert manager.list_generated_fingerprints() == (profile.dependency_fingerprint,)


# -- bounded escalation and repair -------------------------------------------


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
                requirements_quality="clear",
                needs_research=False,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
            ),
            specification=Specification(
                problem="Fix defect.",
                acceptance_criteria=["Notes are updated."],
                confidence=0.9,
            ),
            research_report=None,
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
                requirements_quality="clear",
                needs_research=False,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
            ),
            specification=spec,
            research_report=None,
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


def test_low_risk_finding_is_accepted_at_configured_review_round_limit(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                blocking_findings=[_review_finding("Bounded low-risk defect.")],
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


def test_needs_research_runs_the_researcher_once_and_then_plans(
    source_repo: Path, data_dir: Path
) -> None:
    """Research no longer escalates: the researcher runs exactly once, its
    report is persisted, and the run continues into planning."""
    research_requests: list[AgentRequest] = []
    planner_requests: list[AgentRequest] = []

    def recording_researcher(request: AgentRequest) -> AgentResult:
        research_requests.append(request)
        return FakeAgentRuntime()._default_researcher(request)

    def recording_planner(request: AgentRequest) -> AgentResult:
        planner_requests.append(request)
        return FakeAgentRuntime()._default_planner(request)

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L1, Risk.R1, needs_research=True),
            researcher=recording_researcher,
            planner=recording_planner,
        ),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.completed_at is not None
    assert len(research_requests) == 1
    assert research_requests[0].specification is not None
    assert (store.runs_dir / run.id / "research.json").exists()

    research_report = store.load_artifact(run.id, ResearchReport)
    assert research_report.findings
    # The planner receives the research report, and is not re-run for it.
    assert len(planner_requests) == 1
    assert planner_requests[0].research_report == research_report


def test_researcher_failure_fails_the_run(source_repo: Path, data_dir: Path) -> None:
    def crashing_researcher(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.RESEARCHER, success=False, failure_reason="research unavailable"
        )

    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config(data_dir),
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L1, Risk.R1, needs_research=True),
            researcher=crashing_researcher,
        ),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.FAILED
    assert run.failure_reason == "research unavailable"


def test_ineligible_triage_ends_needs_human(source_repo: Path, data_dir: Path) -> None:
    def ineligible_triage(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=False,
                complexity=Complexity.L1,
                risk=Risk.R1,
                requirements_quality="vague",
                needs_research=False,
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


# -- operational agent failures ----------------------------------------------


def test_refiner_agent_failure_produces_persisted_failed_run(
    source_repo: Path, data_dir: Path
) -> None:
    def crashing_refiner(request: AgentRequest) -> AgentResult:
        return AgentResult(role=AgentRole.REFINER, success=False, failure_reason="refiner crashed")

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(refiner=crashing_refiner))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.FAILED
    assert run.failure_reason == "refiner crashed"
    assert run.completed_at is not None
    assert [record.role for record in run.invocation_records] == [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
    ]
    assert run.invocation_records[-1].success is False
    assert run.invocation_records[-1].failure_reason == "refiner crashed"
    persisted = store.load_run(run.id)
    assert persisted == run


def test_runtime_exception_produces_persisted_failed_invocation(
    source_repo: Path, data_dir: Path
) -> None:
    def unavailable_refiner(request: AgentRequest) -> AgentResult:
        raise RuntimeError("runtime unavailable")

    config = _config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(refiner=unavailable_refiner))

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.FAILED
    assert [record.role for record in run.invocation_records] == [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
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


# -- performance mode and fast profile ----------------------------------------


def test_standard_performance_mode_is_default_and_runs_polish(
    source_repo: Path,
    data_dir: Path,
) -> None:
    recorded_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def recording_runtime(request: AgentRequest) -> AgentResult:
        recorded_requests.append(request)
        return default_runtime.run(request)

    config = _config(data_dir, polish_enabled=True, performance_mode="standard")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            refiner=recording_runtime,
            planner=recording_runtime,
            tester=recording_runtime,
            reviewer=recording_runtime,
        ),
    )

    run = controller.run(_work_item("WI-standard-mode"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.requested_performance_mode == "standard"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile is None
    assert run.performance_fallback_reason is None

    # Refiner and planner used top-level models, not fast profile
    refiner_req = next(r for r in recorded_requests if r.role is AgentRole.REFINER)
    planner_req = next(r for r in recorded_requests if r.role is AgentRole.PLANNER)
    assert refiner_req.model == config.models.refiner.model
    assert planner_req.model == config.models.planner.model

    # Optional polish pass ran because standard mode does not skip polish
    polish_attempts = [
        att for att in run.attempt_records if att.triggered_by is AttemptTrigger.POLISH
    ]
    assert len(polish_attempts) == 1

    # Reload from store confirms persistence
    persisted = store.load_run(run.id)
    assert persisted.requested_performance_mode == "standard"
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_model_profile is None
    assert persisted.performance_fallback_reason is None


def test_fast_performance_mode_eligible_runs_fast_refiner_planner_and_skips_polish(
    source_repo: Path,
    data_dir: Path,
) -> None:
    recorded_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def recording_runtime(request: AgentRequest) -> AgentResult:
        recorded_requests.append(request)
        return default_runtime.run(request)

    config = _config(data_dir, polish_enabled=True, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L1, Risk.R1),
            refiner=recording_runtime,
            planner=recording_runtime,
            tester=recording_runtime,
            reviewer=recording_runtime,
        ),
    )

    run = controller.run(_work_item("WI-fast-mode-eligible"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "fast"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason is None

    # Refiner and planner used fast profile models from config
    refiner_req = next(r for r in recorded_requests if r.role is AgentRole.REFINER)
    planner_req = next(r for r in recorded_requests if r.role is AgentRole.PLANNER)
    fast_profile = config.model_profiles["economy"]
    assert refiner_req.model == fast_profile.refiner.model
    assert planner_req.model == fast_profile.planner.model

    # Tester and reviewer preserved independent top-level models
    tester_req = next(r for r in recorded_requests if r.role is AgentRole.TESTER)
    reviewer_req = next(r for r in recorded_requests if r.role is AgentRole.REVIEWER)
    assert tester_req.model == config.models.tester.model
    assert reviewer_req.model == config.models.reviewer.model

    # Optional polish pass was skipped
    polish_attempts = [
        att for att in run.attempt_records if att.triggered_by is AttemptTrigger.POLISH
    ]
    assert len(polish_attempts) == 0

    # Reload from store confirms persistence
    persisted = store.load_run(run.id)
    assert persisted.requested_performance_mode == "fast"
    assert persisted.effective_performance_mode == "fast"
    assert persisted.performance_model_profile == "economy"
    assert persisted.performance_fallback_reason is None


def test_fast_performance_mode_fallback_triage_ineligible_complexity(
    source_repo: Path,
    data_dir: Path,
) -> None:
    recorded_requests: list[AgentRequest] = []
    default_runtime = FakeAgentRuntime()

    def recording_runtime(request: AgentRequest) -> AgentResult:
        recorded_requests.append(request)
        return default_runtime.run(request)

    config = _config(data_dir, polish_enabled=True, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L2, Risk.R1),
            refiner=recording_runtime,
            planner=recording_runtime,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-complexity"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == "complexity L2 is not eligible for fast mode"

    # Refiner and planner fell back to standard top-level models
    refiner_req = next(r for r in recorded_requests if r.role is AgentRole.REFINER)
    planner_req = next(r for r in recorded_requests if r.role is AgentRole.PLANNER)
    assert refiner_req.model == config.models.refiner.model
    assert planner_req.model == config.models.planner.model

    # Polish is not skipped after fallback to standard
    polish_attempts = [
        att for att in run.attempt_records if att.triggered_by is AttemptTrigger.POLISH
    ]
    assert len(polish_attempts) == 1

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == "complexity L2 is not eligible for fast mode"


def test_fast_performance_mode_fallback_triage_ineligible_risk(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir, performance_mode="fast")
    # Allow R2 without human approval so triage can proceed to fallback evaluation
    config.risk[Risk.R2].human_approval = False
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2)),
    )

    run = controller.run(_work_item("WI-fast-fallback-risk"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == "risk R2 is not eligible for fast mode"

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == "risk R2 is not eligible for fast mode"


def test_fast_performance_mode_fallback_triage_requires_research(
    source_repo: Path,
    data_dir: Path,
) -> None:
    research_requests: list[AgentRequest] = []
    planner_requests: list[AgentRequest] = []

    def recording_researcher(request: AgentRequest) -> AgentResult:
        research_requests.append(request)
        return FakeAgentRuntime()._default_researcher(request)

    def recording_planner(request: AgentRequest) -> AgentResult:
        planner_requests.append(request)
        return FakeAgentRuntime()._default_planner(request)

    config = _config(data_dir, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0, needs_research=True),
            researcher=recording_researcher,
            planner=recording_planner,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-research"), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == "triage requires research"
    assert len(research_requests) == 1
    assert planner_requests[0].model == config.models.planner.model

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == "triage requires research"


def test_fast_performance_mode_fallback_after_planning_protected_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def planner_planning_protected_file(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with protected file.",
                steps=[PlanStep(id="1", goal="Edit protected file.", likely_files=["README.md"])],
                expected_scope=ExpectedScope(
                    modules=["README.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
            ),
        )

    config = _config(data_dir, performance_mode="fast")
    config.repository.protected_file_patterns = ["README.md"]
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=planner_planning_protected_file,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-plan-protected"), source_repo)

    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == "scope includes protected files: README.md"

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == "scope includes protected files: README.md"


def test_fast_performance_mode_fallback_after_planning_manifest_or_version_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def planner_planning_manifest(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan updating pyproject.toml.",
                steps=[PlanStep(id="1", goal="Add dependency.", likely_files=["pyproject.toml"])],
                expected_scope=ExpectedScope(
                    modules=["pyproject.toml"],
                    estimated_files_min=1,
                    estimated_files_max=1,
                ),
            ),
        )

    config = _config(data_dir, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=planner_planning_manifest,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-plan-manifest"), source_repo)

    expected_reason = "scope includes manifest or version files: pyproject.toml"
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == expected_reason

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == expected_reason


def test_fast_performance_mode_fallback_after_planning_sensitive_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def planner_planning_ci_workflow(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan modifying CI workflow.",
                steps=[
                    PlanStep(
                        id="1",
                        goal="Update CI.",
                        likely_files=[".github/workflows/ci.yml"],
                    )
                ],
                expected_scope=ExpectedScope(
                    modules=[".github/workflows/ci.yml"],
                    estimated_files_min=1,
                    estimated_files_max=1,
                ),
            ),
        )

    config = _config(data_dir, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=planner_planning_ci_workflow,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-plan-sensitive"), source_repo)

    expected_reason = "scope includes sensitive files: .github/workflows/ci.yml"
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == expected_reason

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == expected_reason


def test_fast_performance_mode_fallback_actual_scope_manifest_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def implementer_changing_manifest(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        manifest = Path(request.workspace_path, "package.json")
        manifest.write_text('{"name": "test-pkg"}\n', encoding="utf-8")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(changed_files=["package.json"], summary="Add package.json"),
        )

    def planner_safe(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Safe plan.",
                steps=[PlanStep(id="1", goal="Safe work.", likely_files=["app.py"])],
                expected_scope=ExpectedScope(
                    modules=["package.json"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
            ),
        )

    config = _config(data_dir, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=planner_safe,
            implementer=implementer_changing_manifest,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-actual-manifest"), source_repo)

    expected_reason = "scope includes manifest or version files: package.json"
    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == expected_reason

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == expected_reason


def test_fast_performance_mode_fallback_actual_scope_sensitive_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def implementer_changing_migration(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        migration_dir = Path(request.workspace_path, "alembic", "versions")
        migration_dir.mkdir(parents=True, exist_ok=True)
        (migration_dir / "001_init.py").write_text("# migration\n", encoding="utf-8")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                changed_files=["alembic/versions/001_init.py"],
                summary="Add migration",
            ),
        )

    config = _config(data_dir, performance_mode="fast")
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            implementer=implementer_changing_migration,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-actual-sensitive"), source_repo)

    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert "scope includes sensitive files" in (run.performance_fallback_reason or "")
    assert "alembic/versions/001_init.py" in (run.performance_fallback_reason or "")

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert "scope includes sensitive files" in (persisted.performance_fallback_reason or "")


def test_fast_performance_mode_fallback_actual_scope_protected_file(
    source_repo: Path,
    data_dir: Path,
) -> None:
    def implementer_changing_protected(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        Path(request.workspace_path, "README.md").write_text("# new readme\n", encoding="utf-8")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(changed_files=["README.md"], summary="Edit README"),
        )

    config = _config(data_dir, performance_mode="fast")
    config.repository.protected_file_patterns = ["README.md"]
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            implementer=implementer_changing_protected,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-actual-protected"), source_repo)

    assert run.requested_performance_mode == "fast"
    assert run.effective_performance_mode == "standard"
    assert run.performance_model_profile == "economy"
    assert run.performance_fallback_reason == "scope includes protected files: README.md"

    persisted = store.load_run(run.id)
    assert persisted.effective_performance_mode == "standard"
    assert persisted.performance_fallback_reason == "scope includes protected files: README.md"


def test_implementer_change_set_correction_bound_enforced_across_recovery_records(
    source_repo: Path,
    data_dir: Path,
) -> None:
    requests: list[AgentRequest] = []
    attempt_count = 0

    def implementer(request: AgentRequest) -> AgentResult:
        nonlocal attempt_count
        requests.append(request)
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=True,
                change_set=ChangeSet(summary="Add clean notes."),
            )
        attempt_count += 1
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "FACTORY_NOTES.md").write_text(f"attempt {attempt_count}\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="Use a robust and comprehensive implementation."),
        )

    config = _config(
        data_dir,
        verify=["test -f pass_flag.txt"],
        same_model_attempts=2,
        max_total_attempts=3,
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(implementer=implementer),
    )
    run = controller.run(_work_item("WI-correction-bound-recovery"), source_repo)

    correction_invocations = [
        inv for inv in run.invocation_records if inv.purpose is AgentPurpose.CORRECT_CHANGE_SET
    ]
    assert len(correction_invocations) == 1

    workspace = GitWorktreeWorkspace(data_dir, source_repo, run.work_item_id)
    context = _RunContext(
        work_item=store.load_artifact(run.id, WorkItem),
        triage_result=store.load_artifact(run.id, TriageResult),
        specification=store.load_artifact(run.id, Specification),
        research_report=None,
        execution_plan=store.load_artifact(run.id, ExecutionPlan),
        repository_profile=store.load_artifact(run.id, RepositoryProfile),
        workspace=workspace,
        source_repo=source_repo,
    )
    assert controller._change_set_correction_used(run, context) is True


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


def test_fast_planned_scope_fallback_does_not_rerun_planner(
    source_repo: Path,
    data_dir: Path,
) -> None:
    planner_calls = 0
    planner_requests: list[AgentRequest] = []

    def planner_planning_protected_file(request: AgentRequest) -> AgentResult:
        nonlocal planner_calls
        planner_calls += 1
        planner_requests.append(request)
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Plan with protected file.",
                steps=[PlanStep(id="1", goal="Edit protected file.", likely_files=["README.md"])],
                expected_scope=ExpectedScope(
                    modules=["README.md"],
                    estimated_files_min=1,
                    estimated_files_max=2,
                ),
            ),
        )

    def implementer_touching_readme(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "README.md").write_text("updated readme\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="Edit README", changed_files=["README.md"]),
        )

    config = _config(data_dir, performance_mode="fast")
    config.repository.protected_file_patterns = ["README.md"]
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=_triage_hook(Complexity.L0, Risk.R0),
            planner=planner_planning_protected_file,
            implementer=implementer_touching_readme,
        ),
    )

    run = controller.run(_work_item("WI-fast-fallback-no-planner-rerun"), source_repo)

    assert planner_calls == 1
    fast_planner_model = controller._router.model_for_role(
        AgentRole.PLANNER, model_profile="economy"
    ).model
    assert planner_requests[0].model == fast_planner_model
    assert planner_requests[0].model != config.models.planner.model
    assert run.effective_performance_mode == "standard"
    assert run.performance_fallback_reason == "scope includes protected files: README.md"
    persisted_plan = store.load_artifact(run.id, ExecutionPlan)
    assert persisted_plan.summary == "Plan with protected file."
