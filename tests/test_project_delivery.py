"""Opt-in remote project delivery and explicit project recovery (ADR-022).

Everything here is offline and deterministic: the "remote" is a local bare Git
repository, and publishing/CI/merging are test doubles that perform ordinary
local Git operations. No GitHub call, no network socket and no paid model call
is possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pytest
from factory_testing import (
    ScriptedController,
    build_config,
    git,
    planner_with_dependencies,
    triage_hook,
)

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.delivery import DeliveryTarget
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
    ProjectTaskState,
    Risk,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.projects import (
    FileProjectStore,
    ProjectError,
    ProjectRunner,
    resolve_delivery_repository,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController
from software_agent_factory.workspace import GitWorktreeWorkspace

pytestmark = pytest.mark.project_delivery

PROJECT_LOG = "PROJECT_LOG.md"


def bare_git(bare_repo: Path, *args: str) -> str:
    """Read from the local bare "remote" repository."""
    return git(bare_repo, "--git-dir=.", *args)


# -- deterministic agents ---------------------------------------------------


def _two_task_plan(project_id: str) -> ProjectPlan:
    return ProjectPlan(
        project_id=project_id,
        summary="Two sequential outcomes.",
        delivery_approach="The second task depends on the first.",
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
    )


def _planner(request: AgentRequest) -> AgentResult:
    if request.purpose is AgentPurpose.DECOMPOSE_PROJECT:
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=_two_task_plan(request.project_brief.id),
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
                modules=(PROJECT_LOG,),
                estimated_files_min=1,
                estimated_files_max=3,
            ),
            test_strategy=("Run configured verification.",),
        ),
    )


_THREE_TASKS = planner_with_dependencies(_planner, (), (1,), ())


def _appending_implementer(request: AgentRequest) -> AgentResult:
    """Append one line so a task's own change never hides its predecessor's."""
    log = Path(request.workspace_path or ".") / PROJECT_LOG
    existing = log.read_text(encoding="utf-8") if log.is_file() else ""
    line = f"{request.work_item.id}\n"
    if line not in existing:
        log.write_text(existing + line, encoding="utf-8")
    return AgentResult(
        role=AgentRole.IMPLEMENTER,
        success=True,
        change_set=ChangeSet(
            summary=f"Recorded {request.work_item.id}.",
            changed_files=[PROJECT_LOG],
            tests_added=[],
            commands_run=[],
        ),
    )


def _runtime() -> FakeAgentRuntime:
    return FakeAgentRuntime(planner=_planner, implementer=_appending_implementer)


# -- delivery doubles -------------------------------------------------------


@dataclass(frozen=True)
class _PublishResult:
    commit_sha: str
    base_branch: str
    pull_request_url: str | None
    created_pull_request: bool


@dataclass(frozen=True)
class _MergeResult:
    commit_sha: str
    pull_request_url: str


class LocalRemotePublisher:
    """Commits the workspace and pushes the branch to the local bare remote."""

    def __init__(self) -> None:
        self.published: list[str] = []

    def flag_needs_look(self, **kwargs: object) -> None:
        """The label is a GitHub call. These tests only need the run to end open."""

    def resolve_base_branch(self, source_repo: Path) -> str:
        return "main"

    def publish(self, **kwargs: object) -> _PublishResult:
        workspace = Path(str(kwargs["workspace_path"]))
        branch = str(kwargs["branch_name"])
        git(workspace, "add", "-A")
        if git(workspace, "diff", "--cached", "--name-only").strip():
            git(workspace, "commit", "-m", str(kwargs["commit_message"]))
        head = git(workspace, "rev-parse", "HEAD").strip()
        record_commit = kwargs["record_commit"]
        assert callable(record_commit)
        record_commit(head)
        git(workspace, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        self.published.append(head)
        existing = kwargs.get("existing_pull_request_url")
        url = (
            str(existing)
            if existing
            else f"https://github.com/acme/repo/pull/{len(self.published)}"
        )
        return _PublishResult(
            commit_sha=head,
            base_branch="main",
            pull_request_url=url,
            created_pull_request=existing is None,
        )


class LocalRemoteMerger:
    """Fast-forwards the local bare remote's target branch to the reviewed head."""

    def __init__(
        self, *, merge_to_target: bool = True, refused_pull_requests: Sequence[str] = ()
    ) -> None:
        self.merges: list[dict[str, object]] = []
        self.merge_to_target = merge_to_target
        self.refused_pull_requests = set(refused_pull_requests)

    def validate_repository(self, repo_path: Path) -> str:
        return "acme/repo"

    def merge(self, **kwargs: object) -> _MergeResult:
        self.merges.append(dict(kwargs))
        if kwargs["pull_request_url"] in self.refused_pull_requests:
            raise OSError("merge refused")
        repo_path = Path(str(kwargs["repo_path"]))
        head = str(kwargs["expected_head_sha"])
        base = str(kwargs["base_branch"])
        if self.merge_to_target:
            git(repo_path, "push", "--quiet", "origin", f"{head}:refs/heads/{base}")
        return _MergeResult(commit_sha=head, pull_request_url=str(kwargs["pull_request_url"]))


class PassingObserver:
    def observe(self, **kwargs: object):
        from software_agent_factory.models import CIReport

        return CIReport(overall="PASS", repair_attempts_used=int(kwargs["repair_attempts_used"]))


class RecordingController:
    """Wraps the real controller to observe (and optionally crash) dispatch."""

    def __init__(
        self,
        delegate: WorkflowController,
        *,
        crash_on_tasks: Sequence[int] = (),
    ) -> None:
        self._delegate = delegate
        self.crash_on_tasks = set(crash_on_tasks)
        self.dispatched: list[tuple[str, str | None]] = []
        self.resumed: list[str] = []
        self.work_items: list[WorkItem] = []

    def run(
        self, work_item: WorkItem, source_repo: Path, *, run_id: str | None = None
    ) -> FactoryRun:
        if work_item.project_task_id in self.crash_on_tasks:
            raise KeyboardInterrupt
        self.work_items.append(work_item)
        self.dispatched.append((work_item.id, run_id))
        return self._delegate.run(work_item, source_repo, run_id=run_id)

    def resume(self, run_id: str, source_repo: Path) -> FactoryRun:
        self.resumed.append(run_id)
        return self._delegate.resume(run_id, source_repo)


class RejectingController:
    """Returns a persisted, terminal ``NEEDS_HUMAN`` child run."""

    def __init__(self, store: FileRunStore) -> None:
        self._store = store
        self.calls = 0

    def run(
        self, work_item: WorkItem, source_repo: Path, *, run_id: str | None = None
    ) -> FactoryRun:
        self.calls += 1
        run = FactoryRun(
            id=run_id or "run-rejected",
            work_item_id=work_item.id,
            state=WorkflowState.NEEDS_HUMAN,
            failure_reason="retry budget exhausted",
        )
        self._store.save_run(run)
        return run

    def resume(self, run_id: str, source_repo: Path) -> FactoryRun:  # pragma: no cover - guard
        raise AssertionError("resume must never reopen a rejected child run")


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def remote_repo(tmp_path: Path, factory_git_env: None) -> Path:
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    return remote


@pytest.fixture
def source_repo(factory_source_repo: Path, remote_repo: Path) -> Path:
    git(factory_source_repo, "remote", "add", "origin", str(remote_repo))
    git(factory_source_repo, "push", "--quiet", "origin", "main")
    return factory_source_repo


def local_delivery_base(repo: Path, expected: str) -> DeliveryTarget:
    """Stand in for the pinned GitHub fetch against a local bare remote.

    The production resolver refuses a remote that is not an allowlisted GitHub
    repository, so these offline tests inject the same contract (fetch the
    configured base branch, return its commit) over a filesystem remote.
    """
    git(repo, "fetch", "--quiet", "origin", "main")
    return DeliveryTarget(
        repository=expected,
        host="github.com",
        commit_sha=git(repo, "rev-parse", "FETCH_HEAD").strip(),
    )


def _local_config(data_dir: Path, **kwargs: object) -> FactoryConfig:
    return build_config(data_dir, **kwargs)  # type: ignore[arg-type]


def _merge_config(data_dir: Path, *, max_concurrent_tasks: int = 2) -> FactoryConfig:
    payload = build_config(
        data_dir,
        verify=["true"],
        pull_request={"enabled": True, "draft": False, "base_branch": "main"},
        ci={"enabled": True, "repair_attempts": 1},
        scheduler={"max_concurrent_tasks": max_concurrent_tasks},
    ).model_dump(mode="json")
    payload["merge"] = {
        "enabled": True,
        "allowed_repositories": ["acme/repo"],
        "required_checks": ["quality"],
    }
    return FactoryConfig.model_validate(payload)


def _remote_runner(
    config: FactoryConfig,
    *,
    merger: LocalRemoteMerger | None = None,
    crash_on_tasks: Sequence[int] = (),
    runtime: FakeAgentRuntime | None = None,
    project_delivery_base: Callable[[Path, str], DeliveryTarget] = local_delivery_base,
) -> tuple[ProjectRunner, RecordingController, FileRunStore]:
    store = FileRunStore(config.data_dir)
    runtime = runtime or _runtime()
    controller = RecordingController(
        WorkflowController(
            config,
            store,
            runtime,
            publisher=LocalRemotePublisher(),
            ci_observer=PassingObserver(),
            merger=merger or LocalRemoteMerger(),
            delivery_base_resolver=local_delivery_base,
        ),
        crash_on_tasks=crash_on_tasks,
    )
    runner = ProjectRunner(
        config,
        store,
        runtime,
        controller=controller,  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=project_delivery_base,
    )
    return runner, controller, store


def _brief(source_repo: Path, project_id: str = "delivery-project") -> ProjectBrief:
    return ProjectBrief(
        id=project_id,
        title="Deliver the requested outcome",
        description="Implement the requested outcome end to end.",
        repository_path=str(source_repo),
        acceptance_criteria=["The outcome is implemented."],
        constraints=["Never touch production data."],
    )


# -- remote delivery --------------------------------------------------------


def test_remote_project_merges_each_task_before_the_dependent_task_starts(
    source_repo: Path,
    remote_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    runner, controller, store = _remote_runner(config)

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.delivery_mode == "merge"
    assert execution.delivery_base_branch == "main"
    assert execution.delivery_repository == "acme/repo"
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.DONE,
        ProjectTaskState.DONE,
    ]
    assert all(task.pull_request_url for task in execution.tasks)
    assert all(task.merge_commit_sha for task in execution.tasks)
    # Serial dispatch even though the local wave cap allows two.
    assert [item[0] for item in controller.dispatched] == [
        "delivery-project-task-1",
        "delivery-project-task-2",
    ]
    assert [item[1] for item in controller.dispatched] == [
        "run-delivery-project-task-1",
        "run-delivery-project-task-2",
    ]
    # The dependent task built on top of the merged predecessor.
    merged_log = bare_git(remote_repo, "show", f"main:{PROJECT_LOG}")
    assert merged_log.splitlines() == [
        "delivery-project-task-1",
        "delivery-project-task-2",
    ]
    integration = Path(str(execution.integration_workspace))
    assert (
        git(integration, "rev-parse", "HEAD").strip()
        == bare_git(remote_repo, "rev-parse", "main").strip()
    )
    assert store.load_run("run-delivery-project-task-2").merge_commit_sha is not None
    # The user's own checkout was never touched.
    assert git(source_repo, "branch", "--show-current").strip() == "main"
    assert git(source_repo, "status", "--porcelain").strip() == ""


def test_remote_task_without_confirmed_merge_stops_the_project(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(
        config, merger=LocalRemoteMerger(merge_to_target=False)
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert execution.tasks[0].state is ProjectTaskState.NEEDS_HUMAN
    assert "not in the fetched" in (execution.failure_reason or "")
    assert execution.tasks[1].state is ProjectTaskState.PENDING


_PULL_REQUEST_1 = "https://github.com/acme/repo/pull/1"
_LEFT_OPEN_1 = (
    f"task 1: pull request {_PULL_REQUEST_1} was left open: "
    "could not merge the pull request: merge refused"
)
_SKIPPED_2 = "task 2 was skipped: it depends on task 1, which did not merge"


def _refused_remote_runner(
    data_dir: Path, *, unattended: bool
) -> tuple[ProjectRunner, RecordingController]:
    """A three-task project whose first pull request cannot merge."""
    config = _merge_config(data_dir)
    config.factory.unattended = unattended
    runner, controller, _store = _remote_runner(
        config,
        merger=LocalRemoteMerger(refused_pull_requests=[_PULL_REQUEST_1]),
        runtime=FakeAgentRuntime(planner=_THREE_TASKS, implementer=_appending_implementer),
    )
    return runner, controller


def test_unattended_project_skips_the_dependents_of_an_unmerged_task_and_ships_the_rest(
    source_repo: Path,
    remote_repo: Path,
    factory_data_dir: Path,
) -> None:
    runner, controller = _refused_remote_runner(factory_data_dir, unattended=True)

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.unattended is True
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.DONE,
        ProjectTaskState.PENDING,
        ProjectTaskState.DONE,
    ]
    # Task 1 keeps its open pull request and has no merge. Task 3 merged.
    assert execution.tasks[0].pull_request_url == _PULL_REQUEST_1
    assert execution.tasks[0].merge_commit_sha is None
    assert execution.tasks[2].merge_commit_sha is not None
    assert [item[0] for item in controller.dispatched] == [
        "delivery-project-task-1",
        "delivery-project-task-3",
    ]
    assert execution.needs_look == (_LEFT_OPEN_1, _SKIPPED_2)
    assert bare_git(remote_repo, "show", f"main:{PROJECT_LOG}").splitlines() == [
        "delivery-project-task-3"
    ]


def test_unattended_remote_resume_keeps_a_left_open_task_unmerged_and_never_dispatches_it(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    runner, _controller = _refused_remote_runner(factory_data_dir, unattended=True)
    execution = runner.run(_brief(source_repo), source_repo)
    FileProjectStore(factory_data_dir).save_execution(
        execution.model_copy(update={"state": ProjectState.RUNNING})
    )
    resuming_runner, controller = _refused_remote_runner(factory_data_dir, unattended=True)

    resumed = resuming_runner.resume(execution.project_id, source_repo)

    assert resumed.state is ProjectState.DONE
    assert controller.dispatched == []
    assert [task.state for task in resumed.tasks] == [
        ProjectTaskState.DONE,
        ProjectTaskState.PENDING,
        ProjectTaskState.DONE,
    ]
    assert resumed.needs_look == (_LEFT_OPEN_1, _SKIPPED_2)


def test_attended_project_still_stops_when_a_task_does_not_merge(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    runner, controller = _refused_remote_runner(factory_data_dir, unattended=False)

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert execution.needs_look == ()
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.NEEDS_HUMAN,
        ProjectTaskState.PENDING,
        ProjectTaskState.PENDING,
    ]
    assert [item[0] for item in controller.dispatched] == ["delivery-project-task-1"]


def test_unattended_target_fetch_failure_after_a_merge_fails_the_project(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    config.factory.unattended = True
    fetches = 0

    def fetch_that_fails_after_the_first_merge(repo: Path, expected: str) -> DeliveryTarget:
        nonlocal fetches
        fetches += 1
        # Fetch 1 starts the project, 2 precedes task 1 and 3 checks its merge.
        if fetches == 3:
            raise OSError("remote unreachable")
        return local_delivery_base(repo, expected)

    runner, controller, _store = _remote_runner(
        config, project_delivery_base=fetch_that_fails_after_the_first_merge
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.FAILED
    assert "remote unreachable" in (execution.failure_reason or "")
    assert execution.needs_look == ()
    # The merged task did not skip its dependent.
    assert execution.tasks[1].state is ProjectTaskState.PENDING
    assert [item[0] for item in controller.dispatched] == ["delivery-project-task-1"]


def test_remote_resume_skips_already_merged_tasks_and_delivers_the_rest(
    source_repo: Path,
    remote_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    crashing, _controller, _store = _remote_runner(config, crash_on_tasks=[2])
    brief = _brief(source_repo)
    with pytest.raises(KeyboardInterrupt):
        crashing.run(brief, source_repo)

    interrupted = FileProjectStore(factory_data_dir).load_execution(brief.id)
    assert interrupted.tasks[0].state is ProjectTaskState.DONE
    assert interrupted.tasks[0].merge_commit_sha

    resumed_runner, resumed_controller, _ = _remote_runner(config)
    execution = resumed_runner.resume(brief.id, source_repo)

    assert execution.state is ProjectState.DONE
    assert [item[0] for item in resumed_controller.dispatched] == ["delivery-project-task-2"]
    assert len(execution.invocation_records) == 1
    assert bare_git(remote_repo, "show", f"main:{PROJECT_LOG}").splitlines() == [
        "delivery-project-task-1",
        "delivery-project-task-2",
    ]


def test_remote_task_merged_without_reviewer_bound_head_stops_the_project(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """A CI repair push may never inherit an earlier revision's approval."""
    config = _merge_config(factory_data_dir)
    store = FileRunStore(config.data_dir)

    class StaleReviewController:
        def run(self, work_item: WorkItem, repo: Path, *, run_id: str | None = None) -> FactoryRun:
            run = FactoryRun(
                id=run_id or "run-stale",
                work_item_id=work_item.id,
                state=WorkflowState.DONE,
                commit_sha="a" * 40,
                reviewed_commit_sha="b" * 40,
                pull_request_url="https://github.com/acme/repo/pull/7",
                merge_commit_sha="a" * 40,
                delivery_base_branch="main",
            )
            store.save_run(run)
            return run

    runner = ProjectRunner(
        config,
        store,
        _runtime(),
        controller=StaleReviewController(),  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=local_delivery_base,
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert "review authorization" in (execution.failure_reason or "")
    assert execution.tasks[0].merge_commit_sha is None


def test_unauthorized_repository_is_rejected_before_planning_or_fetching(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    git(factory_source_repo, "remote", "add", "origin", "git@github.com:acme/repo.git")
    payload = _merge_config(factory_data_dir).model_dump(mode="json")
    payload["merge"]["allowed_repositories"] = ["acme/other"]
    config = FactoryConfig.model_validate(payload)

    class ExplodingRuntime:
        def run(self, request: AgentRequest) -> AgentResult:  # pragma: no cover - guard
            raise AssertionError("no agent may run before the target is authorized")

    runner = ProjectRunner(config, FileRunStore(config.data_dir), ExplodingRuntime())  # type: ignore[arg-type]

    with pytest.raises(ProjectError, match="merge.allowed_repositories"):
        runner.run(_brief(factory_source_repo), factory_source_repo)

    assert not FileProjectStore(config.data_dir).exists("delivery-project")


def test_delivery_repository_resolution_enforces_host_and_repository_allowlists(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    git(factory_source_repo, "remote", "add", "origin", "https://gitlab.com/acme/repo.git")
    with pytest.raises(ProjectError, match="not in the allowed hosts"):
        resolve_delivery_repository(config, factory_source_repo)

    git(factory_source_repo, "remote", "set-url", "origin", "git@github.com:acme/other.git")
    with pytest.raises(ProjectError, match="merge.allowed_repositories"):
        resolve_delivery_repository(config, factory_source_repo)

    git(factory_source_repo, "remote", "set-url", "origin", "git@github.com:acme/repo.git")
    assert resolve_delivery_repository(config, factory_source_repo) == "acme/repo"


def test_integration_worktree_is_rooted_on_the_fetched_target(
    source_repo: Path,
    remote_repo: Path,
    factory_data_dir: Path,
) -> None:
    """Unpushed local commits must never reach a delivered task."""
    (source_repo / "LOCAL_ONLY.md").write_text("not pushed anywhere\n", encoding="utf-8")
    git(source_repo, "add", "-A")
    git(source_repo, "commit", "-m", "local work that was never pushed")
    local_only = git(source_repo, "rev-parse", "HEAD").strip()

    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.DONE
    integration = Path(str(execution.integration_workspace))
    assert not (integration / "LOCAL_ONLY.md").exists()
    assert local_only not in git(integration, "log", "--format=%H")
    assert local_only not in bare_git(remote_repo, "log", "--format=%H", "main")


def test_integration_branch_with_unpublished_commits_is_refused(
    source_repo: Path,
    remote_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)
    brief = _brief(source_repo)
    workspace = GitWorktreeWorkspace(
        config.data_dir,
        source_repo,
        f"project-{brief.id}",
        branch_prefix=config.repository.branch_prefix,
    )
    integration = workspace.prepare()
    (integration / "SNEAKY.md").write_text("added outside the factory\n", encoding="utf-8")
    git(integration, "add", "-A")
    git(integration, "commit", "-m", "unpublished integration commit")

    execution = runner.run(brief, source_repo)

    assert execution.state is ProjectState.FAILED
    reason = execution.failure_reason or ""
    assert "clean at the fetched target" in reason or "unpublished history" in reason
    assert "SNEAKY.md" not in bare_git(remote_repo, "ls-tree", "-r", "--name-only", "main")


@pytest.mark.parametrize(
    "base_branch",
    [
        "--upload-pack=touch pwned",
        "-o",
        "main:../evil",
        "refs/heads/main..HEAD",
        "main@{1}",
        "main release",
        "feature/^head",
        "main.lock",
        "refs/heads/main",
    ],
)
def test_configured_base_branch_is_validated_before_any_fetch(
    source_repo: Path,
    factory_data_dir: Path,
    base_branch: str,
) -> None:
    payload = _merge_config(factory_data_dir).model_dump(mode="json")
    payload["pull_request"]["base_branch"] = base_branch
    config = FactoryConfig.model_validate(payload)

    class ExplodingRuntime:
        def run(self, request: AgentRequest) -> AgentResult:  # pragma: no cover - guard
            raise AssertionError("no agent may run for an invalid delivery target")

    runner = ProjectRunner(
        config,
        FileRunStore(config.data_dir),
        ExplodingRuntime(),  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=local_delivery_base,
    )

    with pytest.raises(ProjectError, match="pull_request.base_branch must be a plain Git name"):
        runner.run(_brief(source_repo), source_repo)


def test_local_project_still_refuses_publishing_configuration(factory_data_dir: Path) -> None:
    config = build_config(
        factory_data_dir,
        pull_request={"enabled": True, "base_branch": "main"},
    )
    with pytest.raises(ValueError, match="merge.enabled"):
        ProjectRunner(config, FileRunStore(factory_data_dir), _runtime())


# -- recovery ---------------------------------------------------------------


def _local_runner(
    config: FactoryConfig,
    *,
    crash_on_tasks: Sequence[int] = (),
) -> tuple[ProjectRunner, RecordingController, FileRunStore]:
    store = FileRunStore(config.data_dir)
    runtime = _runtime()
    controller = RecordingController(
        WorkflowController(config, store, runtime), crash_on_tasks=crash_on_tasks
    )
    runner = ProjectRunner(
        config,
        store,
        runtime,
        controller=controller,  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=local_delivery_base,
    )
    return runner, controller, store


def test_resume_continues_an_interrupted_project_without_replanning(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    crashing, _controller, _store = _local_runner(config, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        crashing.run(brief, factory_source_repo)

    project_store = FileProjectStore(factory_data_dir)
    interrupted = project_store.load_execution(brief.id)
    assert interrupted.tasks[0].state is ProjectTaskState.DONE
    assert interrupted.tasks[1].run_id == "run-delivery-project-task-2"

    resumed_runner, resumed_controller, store = _local_runner(config)
    execution = resumed_runner.resume(brief.id, factory_source_repo)

    assert execution.state is ProjectState.DONE
    # No planner call and no repeat of the delivered task.
    assert len(execution.invocation_records) == 1
    assert [item[0] for item in resumed_controller.dispatched] == ["delivery-project-task-2"]
    assert len(store.list_runs()) == 2
    integration = Path(str(execution.integration_workspace))
    assert (integration / PROJECT_LOG).read_text().splitlines() == [
        "delivery-project-task-1",
        "delivery-project-task-2",
    ]


def test_resume_dispatches_remaining_tasks_under_the_policy_the_project_started_with(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    started = _local_config(
        factory_data_dir,
        scheduler={"max_concurrent_tasks": 1},
        risk_assessment_enabled=False,
    )
    store = FileRunStore(factory_data_dir)
    runtime = FakeAgentRuntime(
        planner=_planner,
        implementer=_appending_implementer,
        triage=triage_hook(risk=Risk.R2),
    )
    crashing = ProjectRunner(
        started,
        store,
        runtime,
        controller=RecordingController(  # type: ignore[arg-type]
            WorkflowController(started, store, runtime), crash_on_tasks=[2]
        ),
    )
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        crashing.run(brief, factory_source_repo)
    project_store = FileProjectStore(factory_data_dir)
    assert project_store.load_execution(brief.id).risk_assessment_enabled is False

    enabled = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    resumed = ProjectRunner(enabled, store, runtime).resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    child_runs = store.list_runs()
    assert {run.risk_assessment_enabled for run in child_runs} == {False}
    assert {run.state for run in child_runs} == {WorkflowState.PR_READY}


def test_resume_dispatches_remaining_tasks_unattended_when_the_project_started_that_way(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    started = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    started.factory.unattended = True
    crashing, _controller, store = _local_runner(started, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        crashing.run(brief, factory_source_repo)
    project_store = FileProjectStore(factory_data_dir)
    assert project_store.load_execution(brief.id).unattended is True

    attended = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    resumed = ProjectRunner(attended, store, _runtime()).resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    assert {run.unattended for run in store.list_runs()} == {True}


def test_unattended_project_records_a_rejected_task_and_skips_its_dependents(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    config.factory.unattended = True
    store = FileRunStore(factory_data_dir)
    runner = ProjectRunner(
        config,
        store,
        _runtime(),
        controller=RejectingController(store),  # type: ignore[arg-type]
    )

    execution = runner.run(_brief(factory_source_repo), factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.tasks[0].state is ProjectTaskState.NEEDS_HUMAN
    assert execution.tasks[1].state is ProjectTaskState.PENDING
    assert execution.needs_look == (
        "task 1: retry budget exhausted",
        "task 2 was skipped: it depends on task 1, which did not merge",
    )


def test_unattended_resume_does_not_refuse_a_recorded_rejection(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    config.factory.unattended = True
    runner, _controller, _store = _local_runner(config, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        runner.run(brief, factory_source_repo)
    project_store = FileProjectStore(factory_data_dir)
    interrupted = project_store.load_execution(brief.id)
    records = list(interrupted.tasks)
    records[0] = records[0].model_copy(
        update={"state": ProjectTaskState.NEEDS_HUMAN, "failure_reason": "retry budget exhausted"}
    )
    project_store.save_execution(
        interrupted.model_copy(update={"tasks": tuple(records), "state": ProjectState.NEEDS_HUMAN})
    )

    resumed_runner, resumed_controller, _ = _local_runner(config)
    resumed = resumed_runner.resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    assert resumed_controller.dispatched == []
    # The crash left task 2 RUNNING. A skipped task goes back to PENDING.
    assert [task.state for task in resumed.tasks] == [
        ProjectTaskState.NEEDS_HUMAN,
        ProjectTaskState.PENDING,
    ]
    assert resumed.needs_look == (
        "task 1: retry budget exhausted",
        "task 2 was skipped: it depends on task 1, which did not merge",
    )


def test_resume_keeps_an_attended_project_attended_when_the_configuration_is_unattended(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    started = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    crashing, _controller, store = _local_runner(started, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        crashing.run(brief, factory_source_repo)

    unattended = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    unattended.factory.unattended = True
    resumed = ProjectRunner(unattended, store, _runtime()).resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    assert resumed.unattended is False
    assert {run.unattended for run in store.list_runs()} == {False}


def _scripted_local_runner(
    config: FactoryConfig,
    outcomes: Mapping[int, WorkflowState | Exception | tuple[str, ...]],
    *dependencies: tuple[int, ...],
) -> tuple[ProjectRunner, FileRunStore]:
    """An unattended local project whose tasks have these dependencies."""
    config.factory.unattended = True
    store = FileRunStore(config.data_dir)
    runtime = FakeAgentRuntime(
        planner=planner_with_dependencies(_planner, *dependencies),
        implementer=_appending_implementer,
    )
    controller = ScriptedController(WorkflowController(config, store, runtime), store, outcomes)
    return ProjectRunner(config, store, runtime, controller=controller), store  # type: ignore[arg-type]


def _skipped(task: int, blocker: int) -> str:
    return f"task {task} was skipped: it depends on task {blocker}, which did not merge"


def test_unattended_skips_follow_a_chain_and_name_the_unmerged_dependency(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 2})
    # Task 3 needs a merged task 1 and an unmerged task 2. Task 4 needs task 3.
    runner, _store = _scripted_local_runner(
        config, {2: WorkflowState.NEEDS_HUMAN}, (), (), (1, 2), (3,)
    )

    execution = runner.run(_brief(factory_source_repo), factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.DONE,
        ProjectTaskState.NEEDS_HUMAN,
        ProjectTaskState.PENDING,
        ProjectTaskState.PENDING,
    ]
    assert execution.needs_look == (
        "task 2: scripted rejection",
        _skipped(3, 2),
        _skipped(4, 3),
    )


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        pytest.param(WorkflowState.FAILED, "scripted rejection", id="child-run-failed"),
        pytest.param(RuntimeError("child crashed"), "child crashed", id="child-run-raised"),
    ],
)
def test_unattended_failed_child_is_recorded_and_its_dependent_skipped(
    factory_source_repo: Path,
    factory_data_dir: Path,
    outcome: WorkflowState | Exception,
    reason: str,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _store = _scripted_local_runner(config, {1: outcome}, (), (1,))

    execution = runner.run(_brief(factory_source_repo), factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.failure_reason is None
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.FAILED,
        ProjectTaskState.PENDING,
    ]
    assert execution.tasks[0].failure_reason == reason
    assert execution.needs_look == (f"task 1: {reason}", _skipped(2, 1))


def test_unattended_integration_conflict_records_the_loser_and_ships_the_winner(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 2})
    # Tasks 1 and 2 run in one wave and both create the same file.
    runner, _store = _scripted_local_runner(config, {}, (), (), (2,))

    execution = runner.run(_brief(factory_source_repo), factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert [task.state for task in execution.tasks] == [
        ProjectTaskState.DONE,
        ProjectTaskState.NEEDS_HUMAN,
        ProjectTaskState.PENDING,
    ]
    assert execution.needs_look[0].startswith(
        "task 2: independent project tasks produced conflicting changes"
    )
    assert execution.needs_look[1:] == (_skipped(3, 2),)
    integration = Path(str(execution.integration_workspace))
    assert (integration / PROJECT_LOG).read_text().splitlines() == ["delivery-project-task-1"]


def test_unattended_local_project_carries_the_reasons_of_a_merged_child(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _store = _scripted_local_runner(config, {1: ("scope drift: pyproject.toml",)}, ())

    execution = runner.run(_brief(factory_source_repo), factory_source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.tasks[0].state is ProjectTaskState.DONE
    assert execution.needs_look == ("task 1: scope drift: pyproject.toml",)


def test_resume_never_integrates_the_same_task_twice(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _controller, _store = _local_runner(config, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        runner.run(brief, factory_source_repo)

    project_store = FileProjectStore(factory_data_dir)
    execution = project_store.load_execution(brief.id)
    integration = Path(str(execution.integration_workspace))
    commits_before = len(git(integration, "log", "--oneline").splitlines())
    # Model a crash between cherry-picking task 1 and persisting its outcome.
    records = list(execution.tasks)
    records[0] = records[0].model_copy(
        update={"state": ProjectTaskState.RUNNING, "commit_sha": None}
    )
    project_store.save_execution(execution.model_copy(update={"tasks": tuple(records)}))

    resumed_runner, resumed_controller, _ = _local_runner(config)
    resumed = resumed_runner.resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    assert resumed_controller.dispatched == [
        ("delivery-project-task-2", "run-delivery-project-task-2")
    ]
    assert len(git(integration, "log", "--oneline").splitlines()) == commits_before + 1
    assert (integration / PROJECT_LOG).read_text().splitlines() == [
        "delivery-project-task-1",
        "delivery-project-task-2",
    ]


def test_resume_retains_a_recorded_rejection_and_never_reopens_the_child_run(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    store = FileRunStore(factory_data_dir)
    controller = RejectingController(store)
    runner = ProjectRunner(
        config,
        store,
        _runtime(),
        controller=controller,  # type: ignore[arg-type]
    )
    brief = _brief(factory_source_repo)

    execution = runner.run(brief, factory_source_repo)
    assert execution.state is ProjectState.NEEDS_HUMAN
    assert execution.tasks[0].state is ProjectTaskState.NEEDS_HUMAN

    with pytest.raises(ProjectError, match="rejection"):
        runner.resume(brief.id, factory_source_repo)
    assert controller.calls == 1


def test_resume_rejects_policy_drift_and_a_different_repository(
    factory_source_repo: Path,
    factory_data_dir: Path,
    tmp_path: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _controller, _store = _local_runner(config, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        runner.run(brief, factory_source_repo)

    drifted = _local_config(
        factory_data_dir, verify=["true"], scheduler={"max_concurrent_tasks": 1}
    )
    drifted_runner, _, _ = _local_runner(drifted)
    with pytest.raises(ProjectError, match="delivery policy changed"):
        drifted_runner.resume(brief.id, factory_source_repo)

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main")
    git(other, "config", "user.email", "factory-test@example.invalid")
    git(other, "config", "user.name", "Factory Test")
    (other / "README.md").write_text("other\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "initial commit")
    same_runner, _, _ = _local_runner(config)
    with pytest.raises(ProjectError, match="--repo does not match"):
        same_runner.resume(brief.id, other)


def test_resume_of_a_completed_project_is_idempotent(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _controller, store = _local_runner(config)
    brief = _brief(factory_source_repo)
    execution = runner.run(brief, factory_source_repo)
    assert execution.state is ProjectState.DONE

    resumed_runner, resumed_controller, _ = _local_runner(config)
    resumed = resumed_runner.resume(brief.id, factory_source_repo)

    assert resumed.state is ProjectState.DONE
    assert resumed_controller.dispatched == []
    assert resumed_controller.resumed == []
    assert len(store.list_runs()) == 2
    assert resumed.completed_at == execution.completed_at


def test_unknown_project_cannot_be_resumed(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    runner, _controller, _store = _local_runner(_local_config(factory_data_dir))
    with pytest.raises(ProjectError, match="no persisted execution"):
        runner.resume("never-planned", factory_source_repo)


def test_a_live_project_lock_prevents_marking_it_abandoned(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, _controller, _store = _local_runner(config, crash_on_tasks=[2])
    brief = _brief(factory_source_repo)
    with pytest.raises(KeyboardInterrupt):
        runner.run(brief, factory_source_repo)

    holder = GitWorktreeWorkspace(
        config.data_dir,
        factory_source_repo,
        f"project-{brief.id}",
        branch_prefix=config.repository.branch_prefix,
    )
    holder.acquire_lock()
    try:
        blocked_runner, _, _ = _local_runner(config)
        with pytest.raises(ProjectError, match="already being executed"):
            blocked_runner.run(_brief(factory_source_repo), factory_source_repo)
        with pytest.raises(ProjectError, match="already being executed"):
            blocked_runner.resume(brief.id, factory_source_repo)
    finally:
        holder.release_lock()

    # The live project was not reconciled to NEEDS_HUMAN behind its own lock.
    assert FileProjectStore(factory_data_dir).load_execution(brief.id).state is ProjectState.RUNNING


def test_project_constraints_are_applied_to_every_child_work_item(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _local_config(factory_data_dir, scheduler={"max_concurrent_tasks": 1})
    runner, controller, _store = _local_runner(config)

    runner.run(_brief(factory_source_repo), factory_source_repo)

    assert controller.work_items
    for work_item in controller.work_items:
        assert "Never touch production data." in work_item.constraints


# -- CLI ---------------------------------------------------------------------


def test_cli_resume_requires_a_project_id_and_rejects_new_brief_options(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    from typer.testing import CliRunner

    from software_agent_factory.cli import app

    cli = CliRunner()
    missing_id = cli.invoke(
        app,
        [
            "project",
            "--repo",
            str(factory_source_repo),
            "--resume",
            "--data-dir",
            str(factory_data_dir),
        ],
    )
    assert missing_id.exit_code == 2
    assert "--resume requires --project-id" in missing_id.output

    with_title = cli.invoke(
        app,
        [
            "project",
            "--repo",
            str(factory_source_repo),
            "--resume",
            "--project-id",
            "delivery-project",
            "--title",
            "New title",
            "--data-dir",
            str(factory_data_dir),
        ],
    )
    assert with_title.exit_code == 2
    assert "--title cannot be combined with --resume" in with_title.output

    missing_brief = cli.invoke(
        app,
        ["project", "--repo", str(factory_source_repo), "--data-dir", str(factory_data_dir)],
    )
    assert missing_brief.exit_code == 2
    assert "--title and --description are required unless --resume" in missing_brief.output


def test_cli_resume_is_idempotent_for_a_delivered_project(
    factory_source_repo: Path,
    factory_data_dir: Path,
) -> None:
    from typer.testing import CliRunner

    from software_agent_factory.cli import app

    cli = CliRunner()
    first = cli.invoke(
        app,
        [
            "project",
            "--repo",
            str(factory_source_repo),
            "--project-id",
            "cli-project",
            "--title",
            "Build customer validation",
            "--description",
            "Reject blank customer names.",
            "--data-dir",
            str(factory_data_dir),
        ],
    )
    assert first.exit_code == 0, first.output
    assert "delivery: local" in first.output

    resumed = cli.invoke(
        app,
        [
            "project",
            "--repo",
            str(factory_source_repo),
            "--project-id",
            "cli-project",
            "--resume",
            "--data-dir",
            str(factory_data_dir),
        ],
    )

    assert resumed.exit_code == 0, resumed.output
    assert "state: DONE" in resumed.output
    assert "task 1: DONE" in resumed.output
    assert len(FileRunStore(factory_data_dir).list_runs()) == 1


def test_cli_run_reports_the_merge_commit_and_target_branch(
    factory_source_repo: Path,
    factory_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DONE`` alone is ambiguous, so a merged run states where it landed."""
    from typer.testing import CliRunner

    from software_agent_factory import cli as cli_module

    class MergedController:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def run(self, work_item: WorkItem, source_repo: Path, **kwargs: object) -> FactoryRun:
            return FactoryRun(
                id="run-merged",
                work_item_id=work_item.id,
                state=WorkflowState.DONE,
                commit_sha="a" * 40,
                reviewed_commit_sha="a" * 40,
                pull_request_url="https://github.com/acme/repo/pull/7",
                merge_commit_sha="b" * 40,
                delivery_repository="acme/repo",
                delivery_base_branch="main",
            )

    monkeypatch.setattr(cli_module, "WorkflowController", MergedController)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "run",
            "--repo",
            str(factory_source_repo),
            "--title",
            "Deliver validation",
            "--description",
            "Reject blank customer names.",
            "--data-dir",
            str(factory_data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pull request: https://github.com/acme/repo/pull/7" in result.output
    assert f"merged commit: {'b' * 40}" in result.output
    assert "merged into: acme/repo@main" in result.output


def test_cli_run_reports_an_unmerged_pull_request(
    factory_source_repo: Path,
    factory_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from software_agent_factory import cli as cli_module

    class UnmergedController:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def run(self, work_item: WorkItem, source_repo: Path, **kwargs: object) -> FactoryRun:
            return FactoryRun(
                id="run-unmerged",
                work_item_id=work_item.id,
                state=WorkflowState.NEEDS_HUMAN,
                pull_request_url="https://github.com/acme/repo/pull/8",
                failure_reason="continuous integration did not pass",
            )

    monkeypatch.setattr(cli_module, "WorkflowController", UnmergedController)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "run",
            "--repo",
            str(factory_source_repo),
            "--title",
            "Deliver validation",
            "--description",
            "Reject blank customer names.",
            "--data-dir",
            str(factory_data_dir),
        ],
    )

    assert result.exit_code == 1
    assert "merged: no (the pull request was not merged by this run)" in result.output
    assert "merged commit:" not in result.output


def test_cli_project_summary_reports_the_delivery_target_and_merged_tasks(
    source_repo: Path,
    factory_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A merge-mode project must show the target and per-task merge evidence."""
    from typer.testing import CliRunner

    from software_agent_factory import cli as cli_module

    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)
    execution = runner.run(_brief(source_repo), source_repo)
    assert execution.state is ProjectState.DONE

    class ReadOnlyRunner:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def resume(self, project_id: str, repo: Path) -> ProjectExecution:
            return execution

    monkeypatch.setattr(cli_module, "ProjectRunner", ReadOnlyRunner)
    monkeypatch.setattr(cli_module, "_load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli_module, "_require_prerequisites", lambda **kwargs: None)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "project",
            "--repo",
            str(source_repo),
            "--project-id",
            execution.project_id,
            "--resume",
            "--data-dir",
            str(factory_data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "delivery: merge" in result.output
    assert "target: acme/repo@main" in result.output
    assert f"merged tasks: {len(execution.tasks)}/{len(execution.tasks)}" in result.output
    for task in execution.tasks:
        assert task.merge_commit_sha is not None
        assert f"merged {task.merge_commit_sha}" in result.output


def test_cli_project_summary_lists_what_needs_a_look(
    source_repo: Path,
    factory_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from software_agent_factory import cli as cli_module

    config = _merge_config(factory_data_dir)
    execution = ProjectExecution(
        project_id="delivery-project",
        state=ProjectState.DONE,
        unattended=True,
        needs_look=("task 1: retry budget exhausted", "task 2 was skipped: it needs task 1"),
    )

    class ReadOnlyRunner:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        def resume(self, project_id: str, repo: Path) -> ProjectExecution:
            return execution

    monkeypatch.setattr(cli_module, "ProjectRunner", ReadOnlyRunner)
    monkeypatch.setattr(cli_module, "_load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(cli_module, "_require_prerequisites", lambda **kwargs: None)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "project",
            "--repo",
            str(source_repo),
            "--project-id",
            execution.project_id,
            "--resume",
            "--data-dir",
            str(factory_data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "needs look: task 1: retry budget exhausted" in result.output
    assert "needs look: task 2 was skipped: it needs task 1" in result.output


def test_resume_accepts_a_recased_authorized_repository(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """``OWNER/REPO`` is case-insensitive, so re-casing the allowlist entry
    (which the authorization helper echoes back) is not identity drift."""
    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)
    execution = runner.run(_brief(source_repo), source_repo)
    assert execution.state is ProjectState.DONE

    store = FileProjectStore(factory_data_dir)
    stored = store.load_execution(execution.project_id)
    store.save_execution(stored.model_copy(update={"delivery_repository": "ACME/Repo"}))

    resumed_runner, _controller, _store = _remote_runner(config)
    resumed = resumed_runner.resume(execution.project_id, source_repo)

    assert resumed.state is ProjectState.DONE


def test_remote_task_without_a_reviewer_approved_tree_stops_the_project(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """A commit id alone is not review evidence: the approved tree must be bound."""
    config = _merge_config(factory_data_dir)
    store = FileRunStore(config.data_dir)
    head = git(source_repo, "rev-parse", "HEAD").strip()

    class TreelessController:
        def run(self, work_item: WorkItem, repo: Path, *, run_id: str | None = None) -> FactoryRun:
            run = FactoryRun(
                id=run_id or "run-treeless",
                work_item_id=work_item.id,
                state=WorkflowState.DONE,
                commit_sha=head,
                reviewed_commit_sha=head,
                pull_request_url="https://github.com/acme/repo/pull/7",
                merge_commit_sha=head,
                delivery_base_branch="main",
            )
            store.save_run(run)
            return run

    runner = ProjectRunner(
        config,
        store,
        _runtime(),
        controller=TreelessController(),  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=local_delivery_base,
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert "reviewed Git tree" in (execution.failure_reason or "")
    assert execution.tasks[0].merge_commit_sha is None


def test_remote_task_with_a_tree_the_reviewer_never_approved_stops_the_project(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    store = FileRunStore(config.data_dir)
    head = git(source_repo, "rev-parse", "HEAD").strip()

    class SwappedTreeController:
        def run(self, work_item: WorkItem, repo: Path, *, run_id: str | None = None) -> FactoryRun:
            run = FactoryRun(
                id=run_id or "run-swapped",
                work_item_id=work_item.id,
                state=WorkflowState.DONE,
                commit_sha=head,
                reviewed_commit_sha=head,
                reviewed_tree_sha="c" * 40,
                pull_request_url="https://github.com/acme/repo/pull/7",
                merge_commit_sha=head,
                delivery_base_branch="main",
            )
            store.save_run(run)
            return run

    runner = ProjectRunner(
        config,
        store,
        _runtime(),
        controller=SwappedTreeController(),  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=local_delivery_base,
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.NEEDS_HUMAN
    assert "review authorization was bound" in (execution.failure_reason or "")


def test_refresh_target_refuses_an_integration_branch_ahead_of_the_target(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """``merge --ff-only`` is a silent no-op when ahead, so equality is asserted."""
    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)
    delivery = runner._delivery_settings(source_repo)  # noqa: SLF001
    workspace = GitWorktreeWorkspace(
        config.data_dir,
        source_repo,
        "project-ahead",
        branch_prefix=config.repository.branch_prefix,
    )
    target = local_delivery_base(source_repo, "acme/repo").commit_sha
    integration = workspace.prepare(base_ref=target)
    (integration / "AHEAD.md").write_text("never pushed\n", encoding="utf-8")
    git(integration, "add", "-A")
    git(integration, "commit", "-m", "ahead of the delivery target")
    ahead = git(integration, "rev-parse", "HEAD").strip()

    with pytest.raises(ProjectError, match="unpublished history"):
        runner._refresh_target(integration, delivery)  # noqa: SLF001

    assert git(integration, "rev-parse", "HEAD").strip() == ahead


def test_every_fetch_revalidates_the_configured_remote_identity(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """A remote renamed or re-pointed mid-project must stop delivery."""
    config = _merge_config(factory_data_dir)
    calls: list[str] = []

    def drifting_base(repo: Path, expected: str) -> DeliveryTarget:
        calls.append(expected)
        if len(calls) > 1:
            from software_agent_factory.github import MergeNotAllowedError

            raise MergeNotAllowedError("delivery repository identity changed before target fetch")
        return local_delivery_base(repo, expected)

    store = FileRunStore(config.data_dir)
    runtime = _runtime()
    controller = RecordingController(
        WorkflowController(
            config,
            store,
            runtime,
            publisher=LocalRemotePublisher(),
            ci_observer=PassingObserver(),
            merger=LocalRemoteMerger(),
            delivery_base_resolver=local_delivery_base,
        )
    )
    runner = ProjectRunner(
        config,
        store,
        runtime,
        controller=controller,  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=drifting_base,
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state in {ProjectState.FAILED, ProjectState.NEEDS_HUMAN}
    assert "identity changed" in (execution.failure_reason or "")
    assert all(expected == "acme/repo" for expected in calls)


def test_remote_delivery_pins_the_authorized_host_for_every_later_fetch(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    """A remote re-pointed to another host mid-project must stop delivery."""
    config = _merge_config(factory_data_dir)
    calls: list[int] = []

    def host_drifting_base(repo: Path, expected: str) -> DeliveryTarget:
        target = local_delivery_base(repo, expected)
        calls.append(1)
        if len(calls) > 1:
            return DeliveryTarget(
                repository=target.repository,
                host="evil.example",
                commit_sha=target.commit_sha,
            )
        return target

    store = FileRunStore(config.data_dir)
    runtime = _runtime()
    controller = RecordingController(
        WorkflowController(
            config,
            store,
            runtime,
            publisher=LocalRemotePublisher(),
            ci_observer=PassingObserver(),
            merger=LocalRemoteMerger(),
            delivery_base_resolver=local_delivery_base,
        )
    )
    runner = ProjectRunner(
        config,
        store,
        runtime,
        controller=controller,  # type: ignore[arg-type]
        delivery_repository_resolver=lambda _repo: "acme/repo",
        delivery_base_resolver=host_drifting_base,
    )

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state in {ProjectState.FAILED, ProjectState.NEEDS_HUMAN}
    assert "delivery host changed" in (execution.failure_reason or "")


def test_remote_delivery_records_the_authorized_host(
    source_repo: Path,
    factory_data_dir: Path,
) -> None:
    config = _merge_config(factory_data_dir)
    runner, _controller, _store = _remote_runner(config)

    execution = runner.run(_brief(source_repo), source_repo)

    assert execution.state is ProjectState.DONE
    assert execution.delivery_host == "github.com"
