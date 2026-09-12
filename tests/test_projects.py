from __future__ import annotations

import json
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
    ProjectTaskExecution,
    Risk,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.projects import FileProjectStore, ProjectError, ProjectRunner
from software_agent_factory.store import FileRunStore

pytestmark = pytest.mark.project_delivery


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
    task_id = request.work_item.project_task_id
    modules = (
        ("FACTORY_NOTES.md", f"task-{task_id}.txt")
        if task_id is not None
        else ("FACTORY_NOTES.md",)
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
                modules=modules,
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
    persisted_execution = FileProjectStore(factory_data_dir).load_execution(brief.id)
    assert len(persisted_execution.invocation_records) == 1
    assert persisted_execution.invocation_records[0].role is AgentRole.PLANNER
    assert persisted_execution.invocation_records[0].purpose is AgentPurpose.DECOMPOSE_PROJECT
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


def test_project_retries_rejected_decomposition_with_feedback(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    decomposition_requests: list[AgentRequest] = []

    def planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return _project_planner(request)
        decomposition_requests.append(request)
        if len(decomposition_requests) == 1:
            return AgentResult(
                role=AgentRole.PLANNER,
                success=False,
                failure_reason=(
                    "a single project task may have at most 6 acceptance criteria; "
                    "split the project into a task DAG"
                ),
            )
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=_project_planner(request).project_plan,
        )

    brief = ProjectBrief(
        id="project-corrected-decomposition",
        title="Build customer validation",
        description="Implement two dependent validation outcomes.",
        repository_path=str(factory_source_repo),
    )
    project_store = FileProjectStore(factory_data_dir)
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=planner),
        project_store=project_store,
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert len(decomposition_requests) == 2
    assert decomposition_requests[0].repair_context is None
    assert "single project task" in str(decomposition_requests[1].repair_context)
    assert len(project_store.load_plan(brief.id).tasks) == 2
    assert len(project_store.load_execution(brief.id).invocation_records) == 2


def test_project_plan_rejects_overpacked_single_task() -> None:
    with pytest.raises(ValueError, match="single project task may have at most 6"):
        ProjectPlan(
            project_id="overpacked-project",
            summary="One oversized task.",
            delivery_approach="Put every capability in one issue.",
            tasks=(
                ProjectTask(
                    id=1,
                    title="Build the entire system",
                    description="Implement every independently verifiable capability.",
                    acceptance_criteria=tuple(f"Outcome {index} works." for index in range(1, 8)),
                ),
            ),
        )


def test_project_plan_rejects_zero_dependency() -> None:
    with pytest.raises(ValueError, match="valid earlier task ids"):
        ProjectPlan(
            project_id="zero-dependency",
            summary="Reject invalid dependency ids.",
            delivery_approach="Use two tasks.",
            tasks=(
                ProjectTask(
                    id=1,
                    title="Foundation",
                    description="Create the foundation.",
                    acceptance_criteria=("The foundation exists.",),
                ),
                ProjectTask(
                    id=2,
                    title="Dependent task",
                    description="Build on the foundation.",
                    acceptance_criteria=("The dependent behavior exists.",),
                    dependencies=(0,),
                ),
            ),
        )


def test_project_work_item_preserves_sibling_task_boundaries(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    captured: list[WorkItem] = []

    class CapturingController:
        def run(
            self,
            work_item: WorkItem,
            _source_repo: Path,
            *,
            run_id: str | None = None,
        ) -> FactoryRun:
            captured.append(work_item)
            return FactoryRun(
                id=run_id or f"run-{work_item.id}",
                work_item_id=work_item.id,
                state=WorkflowState.NEEDS_HUMAN,
                failure_reason="stop after capturing task boundary",
            )

        def resume(self, run_id: str, _source_repo: Path) -> FactoryRun:
            raise AssertionError(f"unexpected resume for {run_id}")

    brief = ProjectBrief(
        id="project-task-boundary",
        title="Build two outcomes",
        description="Deliver two separate capabilities.",
        repository_path=str(factory_source_repo),
    )
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=_project_planner),
        controller=CapturingController(),  # type: ignore[arg-type]
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert len(captured) == 1
    boundary = captured[0].constraints[-1]
    assert "Do not implement project task 2" in boundary
    assert "Build on the base behavior" in boundary
    assert captured[0].description == "Implement the first required outcome."
    assert "Deliver two separate capabilities." not in captured[0].description


def test_project_work_item_keeps_lower_id_independent_sibling_out_of_scope(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    brief = ProjectBrief(
        id="parallel-boundary",
        title="Build parallel outcomes",
        description="Deliver independent capabilities.",
        repository_path=str(factory_source_repo),
    )
    tasks = (
        ProjectTask(
            id=1,
            title="Build first capability",
            description="Implement the first capability.",
            acceptance_criteria=("The first capability works.",),
        ),
        ProjectTask(
            id=2,
            title="Build second capability",
            description="Implement the second capability.",
            acceptance_criteria=("The second capability works.",),
        ),
    )
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=_project_planner),
    )

    work_item = runner._to_work_item(
        brief,
        tasks[1],
        project_tasks=tasks,
        issue_url=None,
    )

    boundary = work_item.constraints[-1]
    assert "Do not implement project task 1" in boundary
    assert "Build first capability" in boundary


def test_project_persists_failed_planner_invocation_when_runtime_raises(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    def unavailable_planner(request: AgentRequest) -> AgentResult:
        raise RuntimeError("planner runtime unavailable")

    brief = ProjectBrief(
        id="planner-runtime-failure",
        title="Record planner failures",
        description="Persist the failed invocation.",
        repository_path=str(factory_source_repo),
    )
    store = FileProjectStore(factory_data_dir)
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(planner=unavailable_planner),
        project_store=store,
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.FAILED
    assert len(execution.invocation_records) == 1
    invocation = execution.invocation_records[0]
    assert invocation.success is False
    assert invocation.failure_reason == "RuntimeError: planner runtime unavailable"
    assert store.load_execution(brief.id) == execution


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
                        labels=("project", "enhancement"),
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
    assert "## Suggested labels\n- `project`\n- `enhancement`" in github.created[0][1]
    assert "software-agent-factory project=project-github task=1" in github.created[0][1]
    assert github.closed == ["https://github.com/acme/repo/issues/1"]


def test_issue_text_is_fully_validated_before_any_issue_is_created(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    github = _RecordingGitHubClient()
    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
        github_client=github,  # type: ignore[arg-type]
    )
    brief = ProjectBrief(
        id="project-prevalidate",
        title="Validate issue text",
        description="Validate all issue text before publication.",
        repository_path=str(factory_source_repo),
    )
    plan = ProjectPlan(
        project_id=brief.id,
        summary="Two issue templates.",
        delivery_approach="Publish only after every template passes.",
        tasks=(
            ProjectTask(
                id=1,
                title="Create first issue",
                description="Create the first issue.",
                acceptance_criteria=("The first issue exists.",),
            ),
            ProjectTask(
                id=2,
                title="Create second issue",
                description="Create the second issue.",
                acceptance_criteria=("The second issue exists.",),
                constraints=tuple(" ".join(["word"] * 30) for _ in range(20)),
                dependencies=(1,),
            ),
        ),
    )
    execution = ProjectExecution(
        project_id=brief.id,
        state=ProjectState.PLANNING,
        tasks=(
            ProjectTaskExecution(task_id=1, work_item_id="project-prevalidate-task-1"),
            ProjectTaskExecution(task_id=2, work_item_id="project-prevalidate-task-2"),
        ),
    )

    with pytest.raises(ValueError, match="issue body did not satisfy writing policy"):
        runner._publish_issues(brief, plan, execution, factory_source_repo, "acme/repo")

    assert github.created == []


def test_file_project_store_rejects_corrupt_json(tmp_path: Path) -> None:
    store = FileProjectStore(tmp_path / "data")
    project_dir = tmp_path / "data" / "projects" / "proj-corrupt"
    project_dir.mkdir(parents=True)
    (project_dir / "project-plan.json").write_text(
        '{"schema_version": 1, "project_id": "proj-corrupt", invalid...',
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        store.load_plan("proj-corrupt")


def test_file_project_store_rejects_unknown_schema_version(tmp_path: Path) -> None:
    store = FileProjectStore(tmp_path / "data")
    project_dir = tmp_path / "data" / "projects" / "proj-bad-schema"
    project_dir.mkdir(parents=True)
    bad_plan = {
        "schema_version": 99,
        "project_id": "proj-bad-schema",
        "summary": "Bad",
        "delivery_approach": "None",
        "tasks": [],
    }
    (project_dir / "project-plan.json").write_text(
        json.dumps(bad_plan),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported ProjectPlan schema_version: 99"):
        store.load_plan("proj-bad-schema")


def test_file_project_store_rejects_missing_schema_version(tmp_path: Path) -> None:
    store = FileProjectStore(tmp_path / "data")
    project_dir = tmp_path / "data" / "projects" / "proj-bad-schema"
    project_dir.mkdir(parents=True)
    plan = {
        "project_id": "proj-bad-schema",
        "summary": "Bad",
        "delivery_approach": "None",
        "tasks": [],
    }
    (project_dir / "project-plan.json").write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported ProjectPlan schema_version: None"):
        store.load_plan("proj-bad-schema")


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


def test_project_commits_use_factory_identity_without_ambient_git_identity(
    factory_source_repo: Path,
    factory_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ambient Author")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ambient-author@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Ambient Committer")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ambient-committer@example.com")
    git(factory_source_repo, "config", "--unset-all", "user.name")
    git(factory_source_repo, "config", "--unset-all", "user.email")
    git(factory_source_repo, "config", "user.useConfigOnly", "true")

    runner = ProjectRunner(
        build_config(factory_data_dir),
        FileRunStore(factory_data_dir),
        FakeAgentRuntime(),
    )
    brief = ProjectBrief(
        id="project-factory-identity",
        title="Create a deterministic commit",
        description="Do not depend on the operator's Git identity.",
        repository_path=str(factory_source_repo),
    )

    execution = runner.run(brief, factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.integration_workspace is not None
    identity = git(
        Path(execution.integration_workspace),
        "show",
        "-s",
        "--format=%an <%ae>|%cn <%ce>",
        "HEAD",
    ).strip()
    assert identity == (
        "Software Agent Factory <software-agent-factory@example.invalid>|"
        "Software Agent Factory <software-agent-factory@example.invalid>"
    )
