from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest
from factory_testing import build_config, git, triage_hook

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanStep,
    ProjectBrief,
    ProjectExecution,
    ProjectPlan,
    ProjectState,
    ProjectTask,
    Risk,
    WorkflowState,
)
from software_agent_factory.projects import FileProjectStore, ProjectError, ProjectRunner
from software_agent_factory.store import FileRunStore


class _RecordingGitHubClient:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.created: list[tuple[str, str, tuple[str, ...]]] = []
        self.closed: list[str] = []
        self.fail_close = fail_close

    def create_issue(
        self,
        _repo_path: Path,
        *,
        repository: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
    ) -> str:
        self.created.append((title, body, tuple(labels)))
        return f"https://github.com/{repository}/issues/{len(self.created)}"

    def close_issue(self, _repo_path: Path, *, repository: str, issue: str) -> None:
        assert repository == "acme/repo"
        if self.fail_close:
            raise OSError("temporary close failure")
        self.closed.append(issue)


def _project_planner(request: AgentRequest) -> AgentResult:
    if request.purpose is AgentPurpose.DECOMPOSE_PROJECT:
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id=request.project_brief.id,
                summary="Two sequential outcomes.",
                delivery_approach=(
                    "Use two tasks because the second explicitly depends on the first."
                ),
                tasks=(
                    ProjectTask(
                        id=1,
                        title="Create the base behavior",
                        description="Implement the first required outcome.",
                        acceptance_criteria=("The base behavior exists.",),
                    ),
                    ProjectTask(
                        id=2,
                        title="Build on the base behavior",
                        description="Implement the dependent outcome.",
                        acceptance_criteria=("The dependent behavior exists.",),
                        dependencies=(1,),
                    ),
                ),
            ),
        )
    return AgentResult(
        role=AgentRole.PLANNER,
        success=True,
        execution_plan=ExecutionPlan(
            summary=f"Implement {request.work_item.title}.",
            steps=(
                PlanStep(
                    id="implement",
                    goal=request.work_item.description,
                    validation=("Run configured verification.",),
                ),
            ),
            expected_scope=ExpectedScope(
                modules=(),
                estimated_files_min=1,
                estimated_files_max=3,
            ),
            test_strategy=("Run configured verification.",),
        ),
    )


def test_project_runner_composes_dependent_tasks_on_one_branch(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    git(factory_source_repo, "config", "commit.gpgsign", "true")
    config = build_config(factory_data_dir)
    store = FileRunStore(factory_data_dir)
    runner = ProjectRunner(
        config,
        store,
        FakeAgentRuntime(planner=_project_planner),
    )
    brief = ProjectBrief(
        id="project-validation",
        title="Build customer validation",
        description="Implement customer validation end to end.",
        repository_path=str(factory_source_repo),
        acceptance_criteria=["The requested validation is implemented."],
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert [task.state.value for task in execution.tasks] == ["DONE", "DONE"]
    assert all(task.run_id for task in execution.tasks)
    assert all(task.commit_sha for task in execution.tasks)
    assert execution.integration_workspace is not None
    integration = Path(execution.integration_workspace)
    assert "project-validation-task-2" in (integration / "FACTORY_NOTES.md").read_text()
    assert len(store.list_runs()) == 2
    assert FileProjectStore(factory_data_dir).load_plan(brief.id).tasks[1].dependencies == (1,)
    assert len(git(integration, "log", "--oneline").splitlines()) == 3


def test_fake_project_planner_defaults_to_one_task(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
    )
    brief = ProjectBrief(
        id="project-small",
        title="Make one small change",
        description="Implement one coherent behavior.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo)
    plan = FileProjectStore(factory_data_dir).load_plan(brief.id)

    assert execution.state is ProjectState.DONE
    assert len(plan.tasks) == 1
    assert "one coherent work item" in plan.delivery_approach


def test_project_normalizes_planner_project_id(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    def planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return _project_planner(request)
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id="model-invented-id",
                summary="One sufficient task.",
                delivery_approach="Use one coherent task.",
                tasks=(
                    ProjectTask(
                        id=1,
                        title="Implement the outcome",
                        description="Implement the requested outcome.",
                        acceptance_criteria=("The outcome works.",),
                    ),
                ),
            ),
        )

    brief = ProjectBrief(
        id="factory-owned-id",
        title="Keep deterministic identity",
        description="Ignore a model-invented project identifier.",
        repository_path=str(factory_source_repo),
    )
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=planner),
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert FileProjectStore(factory_data_dir).load_plan(brief.id).project_id == brief.id


def test_project_stops_when_a_required_task_needs_human(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    store = FileRunStore(factory_data_dir)
    runner = ProjectRunner(
        config,
        store,
        FakeAgentRuntime(
            planner=_project_planner,
            triage=triage_hook(risk=Risk.R2),
        ),
    )
    brief = ProjectBrief(
        id="project-risky",
        title="Perform risky work",
        description="Perform work that requires approval.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert execution.tasks[0].state.value == "NEEDS_HUMAN"
    assert execution.tasks[1].state.value == "PENDING"
    assert len(store.list_runs()) == 1


def test_parallel_wave_persists_every_child_result_before_stopping(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(
        factory_data_dir,
        scheduler={"max_concurrent_tasks": 2},
    )
    store = FileRunStore(factory_data_dir)

    def independent_planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return _project_planner(request)
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id=request.project_brief.id,
                summary="Two independent tasks.",
                delivery_approach="Two independently verifiable outcomes can run in parallel.",
                tasks=(
                    ProjectTask(
                        id=1,
                        title="First outcome",
                        description="Implement the first outcome.",
                        acceptance_criteria=("The first outcome works.",),
                    ),
                    ProjectTask(
                        id=2,
                        title="Second outcome",
                        description="Implement the second outcome.",
                        acceptance_criteria=("The second outcome works.",),
                    ),
                ),
            ),
        )

    runner = ProjectRunner(
        config,
        store,
        FakeAgentRuntime(
            planner=independent_planner,
            triage=triage_hook(risk=Risk.R2),
        ),
    )
    brief = ProjectBrief(
        id="project-parallel-risk",
        title="Run two risky tasks",
        description="Both outcomes require approval.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert all(task.run_id for task in execution.tasks)
    assert all(task.state.value == "NEEDS_HUMAN" for task in execution.tasks)
    assert len(store.list_runs()) == 2


def test_final_verification_checks_fully_composed_integration_branch(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(
        factory_data_dir,
        scheduler={"max_concurrent_tasks": 2},
        verify=["test ! -f task-1.txt -o ! -f task-2.txt"],
    )

    def planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return _project_planner(request)
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id=request.project_brief.id,
                summary="Two independently green tasks.",
                delivery_approach="Run two independently verifiable outcomes in parallel.",
                tasks=(
                    ProjectTask(
                        id=1,
                        title="Create first marker",
                        description="Create the first marker.",
                        acceptance_criteria=("The first marker exists.",),
                    ),
                    ProjectTask(
                        id=2,
                        title="Create second marker",
                        description="Create the second marker.",
                        acceptance_criteria=("The second marker exists.",),
                    ),
                ),
            ),
        )

    def implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        assert request.work_item.project_task_id is not None
        marker = Path(request.workspace_path) / f"task-{request.work_item.project_task_id}.txt"
        marker.write_text("done\n", encoding="utf-8")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary=f"Created {marker.name}.",
                changed_files=(marker.name,),
            ),
        )

    brief = ProjectBrief(
        id="project-final-verification",
        title="Verify the composed tree",
        description="Each task passes alone but the two markers must not coexist.",
        repository_path=str(factory_source_repo),
    )
    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=planner, implementer=implementer),
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert all(task.state.value == "DONE" for task in execution.tasks)
    assert execution.verification_report is not None
    assert not execution.verification_report.passed
    assert "verify:" in (execution.failure_reason or "")
    assert (factory_data_dir / "projects/project-final-verification/logs").is_dir()


def test_project_store_rejects_duplicate_execution_and_path_traversal(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    store = FileProjectStore(factory_data_dir)
    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
        project_store=store,
    )
    brief = ProjectBrief(
        id="project-once",
        title="Run once",
        description="Do not dispatch this project twice.",
        repository_path=str(factory_source_repo),
    )

    assert runner.run(brief, factory_source_repo).state is ProjectState.DONE

    with pytest.raises(ProjectError, match="already exists"):
        runner.run(brief, factory_source_repo)

    with pytest.raises(ValueError, match="project_id"):
        store.load_execution("../escape")


def test_duplicate_nonterminal_project_is_reconciled_to_needs_human(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    store = FileProjectStore(factory_data_dir)
    store.save_execution(
        ProjectExecution(project_id="project-abandoned", state=ProjectState.RUNNING)
    )
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
        project_store=store,
    )
    brief = ProjectBrief(
        id="project-abandoned",
        title="Recover an abandoned project",
        description="Do not leave persisted state running forever.",
        repository_path=str(factory_source_repo),
    )

    with pytest.raises(ProjectError, match="already exists"):
        runner.run(brief, factory_source_repo)

    execution = store.load_execution(brief.id)
    assert execution.state is ProjectState.NEEDS_HUMAN
    assert execution.completed_at is not None
    assert "abandoned by a previous process" in (execution.failure_reason or "")


def test_project_can_publish_and_close_issues_without_daemon_label(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    github = _RecordingGitHubClient()

    def planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return _project_planner(request)
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id=request.project_brief.id,
                summary="One published task.",
                delivery_approach="One task is sufficient.",
                tasks=(
                    ProjectTask(
                        id=1,
                        title="Implement the feature",
                        description="Implement and verify the requested behavior.",
                        acceptance_criteria=("The behavior works.",),
                        labels=("project", "agent-ready"),
                    ),
                ),
            ),
        )

    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=planner),
        github_client=github,  # type: ignore[arg-type]
    )
    brief = ProjectBrief(
        id="project-github",
        title="Publish project task",
        description="Create and execute one tracked task.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo, github_repository="acme/repo")

    assert execution.state is ProjectState.DONE
    assert github.created[0][2] == ()
    assert "## Suggested labels\n- project\n- agent-ready" in github.created[0][1]
    assert "software-agent-factory project=project-github task=1" in github.created[0][1]
    assert github.closed == ["https://github.com/acme/repo/issues/1"]


def test_issue_close_failure_is_a_warning_after_successful_integration(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    github = _RecordingGitHubClient(fail_close=True)
    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
        github_client=github,  # type: ignore[arg-type]
    )
    brief = ProjectBrief(
        id="project-close-warning",
        title="Complete despite tracker warning",
        description="Implement the project even if issue closure fails.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo, github_repository="acme/repo")

    assert execution.state is ProjectState.DONE
    assert execution.tasks[0].state.value == "DONE"
    assert "could not be closed" in execution.warnings[0]


def test_project_commit_rejects_protected_files_and_empty_changes(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = build_config(factory_data_dir)
    runner = ProjectRunner(
        config,
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
    )
    task = ProjectTask(
        id=1,
        title="Unsafe task",
        description="Attempt an unsafe change.",
        acceptance_criteria=("The task is rejected.",),
    )
    run = FactoryRun(
        id="run-project-guard",
        work_item_id="project-guard-task-1",
        state=WorkflowState.PR_READY,
        workspace_path=str(factory_source_repo),
    )

    with pytest.raises(ProjectError, match="without repository changes"):
        runner._commit_child(run, task)

    (factory_source_repo / ".env").write_text("SECRET=value\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="protected patterns"):
        runner._commit_child(run, task)
