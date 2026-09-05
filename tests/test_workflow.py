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

import pytest

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.governance import (
    CheckPhase,
    RepositoryVerificationResult,
    VerificationFailureKind,
)
from software_agent_factory.models import (
    AgentRole,
    AttemptTrigger,
    ChangeSet,
    Complexity,
    FactoryRun,
    RepairContext,
    RepositoryProfile,
    ResearchReport,
    ReviewReport,
    Risk,
    RunLease,
    SkillId,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
    utc_now,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    WorkflowController,
    is_run_finished,
)


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
        AgentRole.IMPLEMENTER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ]
    assert [attempt.triggered_by for attempt in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.POLISH,
    ]
    polish_request = runtime.requests[4]
    assert isinstance(polish_request.repair_context, RepairContext)
    assert polish_request.repair_context.trigger is AttemptTrigger.POLISH
    assert "factory-selected repository skills" in polish_request.repair_context.summary
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


def test_repository_profiler_failure_degrades_to_generic_skills(
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
    assert [skill.id for skill in profile.selected_skills] == [
        SkillId.PLAN_QUALITY,
        SkillId.SIMPLIFICATION,
    ]
    assert profile.warnings and "profiling degraded" in profile.warnings[0]


def test_repository_skills_are_persisted_and_injected_into_four_roles(
    source_repo: Path, data_dir: Path
) -> None:
    (source_repo / "pyproject.toml").write_text(
        """
[project]
name = "example"
dependencies = ["pytest>=8"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    _git(source_repo, "add", "pyproject.toml")
    _git(source_repo, "commit", "-m", "add Python project")
    runtime = RecordingRuntime(FakeAgentRuntime())
    store = FileRunStore(data_dir)
    controller = WorkflowController(_config(data_dir), store, runtime)

    run = controller.run(_work_item("WI-capabilities"), source_repo)

    profile = store.load_artifact(run.id, RepositoryProfile)
    assert [skill.id for skill in profile.selected_skills] == [
        SkillId.PLAN_QUALITY,
        SkillId.SIMPLIFICATION,
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    requests = {request.role: request for request in runtime.requests}
    assert [skill.id for skill in requests[AgentRole.PLANNER].selected_skills] == [
        SkillId.PLAN_QUALITY,
        SkillId.SIMPLIFICATION,
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    assert [skill.id for skill in requests[AgentRole.IMPLEMENTER].selected_skills] == [
        SkillId.SIMPLIFICATION,
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    assert [skill.id for skill in requests[AgentRole.TESTER].selected_skills] == [
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    assert [skill.id for skill in requests[AgentRole.REVIEWER].selected_skills] == [
        SkillId.SIMPLIFICATION,
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    assert requests[AgentRole.TRIAGE].selected_skills == []
    assert requests[AgentRole.REFINER].selected_skills == []


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


def test_reviewer_rejection_is_bounded_by_same_global_attempt_budget(
    source_repo: Path, data_dir: Path
) -> None:
    def rejecting_reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=False, findings=["not good enough"]),
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

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 3
    assert all(attempt.outcome == "succeeded" for attempt in run.attempt_records)
    assert all(attempt.model == "claude-opus-5" for attempt in run.attempt_records)


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
    persisted = store.load_run(run.id)
    assert persisted == run


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
