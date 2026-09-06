"""Project-level decomposition and bounded execution.

The project layer is intentionally small: one planner proposes a typed flat
task DAG, deterministic code validates and persists it, and the existing
``WorkflowController`` executes every task. A local integration worktree
composes successful child commits so dependent tasks see predecessor changes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from .agents import AgentRequest, AgentRuntime
from .config import FactoryConfig
from .github import GitHubClient, GitHubCommandError
from .governance import RepositoryVerifier, assess_publish_gate
from .models import (
    AgentPurpose,
    AgentRole,
    FactoryRun,
    ProjectBrief,
    ProjectExecution,
    ProjectPlan,
    ProjectState,
    ProjectTask,
    ProjectTaskExecution,
    ProjectTaskState,
    VersionedModel,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .publishing import resolve_github_token
from .repository_profile import profile_repository
from .routing import ModelRouter
from .store import FileRunStore, ImmutableArtifactConflictError
from .workflow import TransitionError, WorkflowController
from .workspace import GitWorktreeWorkspace, WorkspaceError

ProjectArtifact = TypeVar("ProjectArtifact", bound=VersionedModel)
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SUCCESS_STATES = frozenset({WorkflowState.PR_READY, WorkflowState.DONE})


class ProjectError(RuntimeError):
    """Raised when project planning or deterministic integration cannot continue."""


class FileProjectStore:
    """Atomic filesystem persistence under ``<data_dir>/projects``."""

    def __init__(self, data_dir: str | Path) -> None:
        self._projects_dir = Path(data_dir).expanduser() / "projects"

    def project_dir(self, project_id: str) -> Path:
        return self._project_dir(project_id, create=True)

    def exists(self, project_id: str) -> bool:
        return (self._project_dir(project_id, create=False) / "execution.json").is_file()

    def save_brief_once(self, brief: ProjectBrief) -> Path:
        return self._save_once(brief.id, "project-brief.json", brief)

    def save_plan_once(self, plan: ProjectPlan) -> Path:
        return self._save_once(plan.project_id, "project-plan.json", plan)

    def save_execution(self, execution: ProjectExecution) -> Path:
        destination = self._project_dir(execution.project_id, create=True) / "execution.json"
        self._write_atomic(destination, self._model_text(execution))
        return destination

    def load_brief(self, project_id: str) -> ProjectBrief:
        return self._load(project_id, "project-brief.json", ProjectBrief)

    def load_plan(self, project_id: str) -> ProjectPlan:
        return self._load(project_id, "project-plan.json", ProjectPlan)

    def load_execution(self, project_id: str) -> ProjectExecution:
        return self._load(project_id, "execution.json", ProjectExecution)

    def _project_dir(self, project_id: str, *, create: bool) -> Path:
        if not isinstance(project_id, str) or not _PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ValueError("project_id must be 1-80 ASCII letters, digits, '.', '_' or '-'")
        if project_id in {".", ".."}:
            raise ValueError("project_id must not be a path traversal token")
        path = self._projects_dir / project_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _save_once(
        self,
        project_id: str,
        filename: str,
        artifact: ProjectArtifact,
    ) -> Path:
        destination = self._project_dir(project_id, create=True) / filename
        content = self._model_text(artifact)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temp.write_text(content, encoding="utf-8")
        try:
            try:
                os.link(temp, destination)
            except FileExistsError:
                if destination.read_text(encoding="utf-8") != content:
                    raise ImmutableArtifactConflictError(
                        f"project artifact already exists with different content: {destination}"
                    ) from None
        finally:
            temp.unlink(missing_ok=True)
        return destination

    def _load(
        self,
        project_id: str,
        filename: str,
        model: type[ProjectArtifact],
    ) -> ProjectArtifact:
        path = self._project_dir(project_id, create=False) / filename
        return model.model_validate(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _model_text(model: VersionedModel) -> str:
        return f"{model.model_dump_json(indent=2)}\n"

    @staticmethod
    def _write_atomic(destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temp.write_text(content, encoding="utf-8")
        try:
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)


class ProjectRunner:
    """Plan and execute one bounded project using the existing task workflow."""

    def __init__(
        self,
        config: FactoryConfig,
        run_store: FileRunStore,
        runtime: AgentRuntime,
        *,
        project_store: FileProjectStore | None = None,
        github_client: GitHubClient | None = None,
        controller: WorkflowController | None = None,
        repository_verifier: RepositoryVerifier | None = None,
    ) -> None:
        if config.pull_request.enabled or config.ci.enabled:
            raise ValueError(
                "project execution currently requires pull_request.enabled=false and "
                "ci.enabled=false so child changes can be composed on one local branch"
            )
        self._config = config
        self._run_store = run_store
        self._runtime = runtime
        self._project_store = project_store or FileProjectStore(config.data_dir)
        self._github = github_client or GitHubClient(token=resolve_github_token())
        self._controller = controller or WorkflowController(config, run_store, runtime)
        self._repository_verifier = repository_verifier or RepositoryVerifier()
        self._router = ModelRouter(config)

    def run(
        self,
        brief: ProjectBrief,
        source_repo: Path,
        *,
        github_repository: str | None = None,
    ) -> ProjectExecution:
        source_repo = source_repo.expanduser().resolve()
        if self._project_store.exists(brief.id):
            existing = self._project_store.load_execution(brief.id)
            if existing.state in {ProjectState.PLANNING, ProjectState.RUNNING}:
                existing = existing.model_copy(
                    update={
                        "state": ProjectState.NEEDS_HUMAN,
                        "failure_reason": (
                            "project execution was abandoned by a previous process; "
                            "artifacts and worktrees were preserved"
                        ),
                        "updated_at": utc_now(),
                        "completed_at": utc_now(),
                    }
                )
                self._project_store.save_execution(existing)
            raise ProjectError(
                f"project {brief.id!r} already exists; choose a new --project-id"
            )
        self._project_store.save_brief_once(brief)
        execution = ProjectExecution(project_id=brief.id, state=ProjectState.PLANNING)
        self._project_store.save_execution(execution)
        project_workspace: GitWorktreeWorkspace | None = None
        workspace_locked = False

        try:
            project_workspace = GitWorktreeWorkspace(
                self._config.data_dir,
                source_repo,
                f"project-{brief.id}",
                branch_prefix=self._config.repository.branch_prefix,
            )
            project_workspace.acquire_lock()
            workspace_locked = True
            integration_path = project_workspace.prepare()
            execution = execution.model_copy(
                update={
                    "integration_workspace": str(integration_path),
                    "integration_branch": project_workspace.branch_name,
                    "updated_at": utc_now(),
                }
            )
            self._project_store.save_execution(execution)

            plan = self._plan(brief, integration_path)
            self._project_store.save_plan_once(plan)
            task_executions = [
                ProjectTaskExecution(
                    task_id=task.id,
                    work_item_id=self._work_item_id(brief.id, task.id),
                )
                for task in plan.tasks
            ]
            execution = execution.model_copy(
                update={
                    "state": ProjectState.RUNNING,
                    "tasks": tuple(task_executions),
                    "updated_at": utc_now(),
                }
            )
            self._project_store.save_execution(execution)

            if github_repository is not None:
                execution = self._publish_issues(
                    brief,
                    plan,
                    execution,
                    source_repo,
                    github_repository,
                )

            execution = self._execute_plan(
                brief,
                plan,
                execution,
                source_repo,
                integration_path,
                github_repository,
            )
        except (
            GitHubCommandError,
            ImmutableArtifactConflictError,
            OSError,
            ProjectError,
            RuntimeError,
            TransitionError,
            ValueError,
            WorkspaceError,
        ) as exc:
            try:
                execution = self._project_store.load_execution(brief.id)
            except FileNotFoundError:
                pass
            execution = execution.model_copy(
                update={
                    "state": ProjectState.FAILED,
                    "failure_reason": str(exc),
                    "updated_at": utc_now(),
                    "completed_at": utc_now(),
                }
            )
            self._project_store.save_execution(execution)
        finally:
            if workspace_locked and project_workspace is not None:
                project_workspace.release_lock()
        return execution

    def _plan(self, brief: ProjectBrief, source_repo: Path) -> ProjectPlan:
        profile = profile_repository(source_repo)
        model = self._router.model_for_role(AgentRole.PLANNER)
        synthetic_work_item = WorkItem(
            id=brief.id,
            title=brief.title,
            description=brief.description,
            acceptance_criteria=list(brief.acceptance_criteria),
            constraints=list(brief.constraints),
            project_id=brief.id,
        )
        result = self._runtime.run(
            AgentRequest(
                role=AgentRole.PLANNER,
                purpose=AgentPurpose.DECOMPOSE_PROJECT,
                model=model.model,
                reasoning=model.reasoning,
                work_item=synthetic_work_item,
                project_brief=brief,
                repository_profile=profile,
                workspace_path=str(source_repo),
                timeout_seconds=self._config.agent_timeout_seconds,
            )
        )
        if not result.success or result.project_plan is None:
            raise ProjectError(
                result.failure_reason or "project planner failed to produce a ProjectPlan"
            )
        return result.project_plan.model_copy(update={"project_id": brief.id})

    def _publish_issues(
        self,
        brief: ProjectBrief,
        plan: ProjectPlan,
        execution: ProjectExecution,
        source_repo: Path,
        repository: str,
    ) -> ProjectExecution:
        issue_urls: dict[int, str] = {}
        records = list(execution.tasks)
        for task in plan.tasks:
            body = self._issue_body(brief, task, issue_urls)
            issue_url = self._github.create_issue(
                source_repo,
                repository=repository,
                title=task.title,
                body=body,
            )
            issue_urls[task.id] = issue_url
            records[task.id - 1] = records[task.id - 1].model_copy(
                update={"issue_url": issue_url}
            )
            execution = execution.model_copy(
                update={"tasks": tuple(records), "updated_at": utc_now()}
            )
            self._project_store.save_execution(execution)
        return execution

    def _execute_plan(
        self,
        brief: ProjectBrief,
        plan: ProjectPlan,
        execution: ProjectExecution,
        source_repo: Path,
        integration_path: Path,
        github_repository: str | None,
    ) -> ProjectExecution:
        completed: set[int] = set()
        pending = {task.id: task for task in plan.tasks}
        while pending:
            ready = [
                task for task in pending.values() if set(task.dependencies).issubset(completed)
            ]
            if not ready:
                raise ProjectError("project plan has unfinished tasks but no ready task")
            ready.sort(key=lambda task: task.id)
            wave = ready[: self._config.scheduler.max_concurrent_tasks]
            execution = self._mark_running(execution, wave)
            results, errors = self._run_wave(
                brief,
                wave,
                execution,
                integration_path,
            )
            for task in wave:
                run = results.get(task.id)
                if run is not None:
                    execution = self._record_run(execution, task.id, run)

            terminal_failure = False
            for task in wave:
                run = results.get(task.id)
                if run is None:
                    execution = self._finish_failure(
                        execution,
                        task.id,
                        ProjectState.FAILED,
                        errors[task.id],
                    )
                    terminal_failure = True
                    continue
                if run.state not in _SUCCESS_STATES:
                    state = (
                        ProjectState.NEEDS_HUMAN
                        if run.state is WorkflowState.NEEDS_HUMAN
                        else ProjectState.FAILED
                    )
                    execution = self._finish_failure(
                        execution,
                        task.id,
                        state,
                        run.failure_reason or f"task {task.id} did not complete",
                    )
                    terminal_failure = True
                    continue

                try:
                    child_commit_sha = self._commit_child(run, task)
                    integration_commit_sha = self._cherry_pick(
                        integration_path,
                        child_commit_sha,
                    )
                    execution = self._finish_task(
                        execution,
                        task.id,
                        integration_commit_sha,
                    )
                except (OSError, ProjectError) as exc:
                    execution = self._finish_failure(
                        execution,
                        task.id,
                        ProjectState.NEEDS_HUMAN,
                        str(exc),
                    )
                    terminal_failure = True
                    continue

                if github_repository is not None:
                    issue_url = execution.tasks[task.id - 1].issue_url
                    if issue_url is not None:
                        try:
                            self._github.close_issue(
                                source_repo,
                                repository=github_repository,
                                issue=issue_url,
                            )
                        except (GitHubCommandError, OSError) as exc:
                            execution = self._add_warning(
                                execution,
                                f"task {task.id} was integrated but its issue could not "
                                f"be closed: {exc}",
                            )
                completed.add(task.id)
                del pending[task.id]
            if terminal_failure:
                return execution

        verification = self._repository_verifier.run(
            self._config.repository.commands,
            cwd=integration_path,
            run_dir=self._project_store.project_dir(brief.id),
            timeout_seconds=self._config.repository.command_timeout_seconds,
            env_passthrough=self._config.repository.env_passthrough,
            capture_bytes=self._config.repository.log_capture_bytes,
        )
        execution = execution.model_copy(
            update={"verification_report": verification.report, "updated_at": utc_now()}
        )
        self._project_store.save_execution(execution)
        if not verification.report.passed:
            reason = (
                verification.report.failures[0]
                if verification.report.failures
                else "final project verification failed"
            )
            execution = execution.model_copy(
                update={
                    "state": ProjectState.NEEDS_HUMAN,
                    "failure_reason": reason,
                    "updated_at": utc_now(),
                    "completed_at": utc_now(),
                }
            )
            self._project_store.save_execution(execution)
            return execution

        execution = execution.model_copy(
            update={
                "state": ProjectState.DONE,
                "updated_at": utc_now(),
                "completed_at": utc_now(),
            }
        )
        self._project_store.save_execution(execution)
        return execution

    def _run_wave(
        self,
        brief: ProjectBrief,
        tasks: list[ProjectTask],
        execution: ProjectExecution,
        integration_path: Path,
    ) -> tuple[dict[int, FactoryRun], dict[int, str]]:
        records = {record.task_id: record for record in execution.tasks}
        futures: dict[int, Future[FactoryRun]] = {}
        with ThreadPoolExecutor(
            max_workers=self._config.scheduler.max_concurrent_tasks,
            thread_name_prefix=f"project-{brief.id}",
        ) as executor:
            for task in tasks:
                work_item = self._to_work_item(
                    brief,
                    task,
                    issue_url=records[task.id].issue_url,
                )
                futures[task.id] = executor.submit(
                    self._controller.run,
                    work_item,
                    integration_path,
                )
        results: dict[int, FactoryRun] = {}
        errors: dict[int, str] = {}
        for task_id, future in futures.items():
            try:
                results[task_id] = future.result()
            except (OSError, RuntimeError, TransitionError, ValueError, WorkspaceError) as exc:
                errors[task_id] = str(exc)
        return results, errors

    def _mark_running(
        self,
        execution: ProjectExecution,
        tasks: list[ProjectTask],
    ) -> ProjectExecution:
        running_ids = {task.id for task in tasks}
        records = tuple(
            record.model_copy(update={"state": ProjectTaskState.RUNNING})
            if record.task_id in running_ids
            else record
            for record in execution.tasks
        )
        updated = execution.model_copy(update={"tasks": records, "updated_at": utc_now()})
        self._project_store.save_execution(updated)
        return updated

    def _add_warning(self, execution: ProjectExecution, warning: str) -> ProjectExecution:
        updated = execution.model_copy(
            update={
                "warnings": (*execution.warnings, warning),
                "updated_at": utc_now(),
            }
        )
        self._project_store.save_execution(updated)
        return updated

    def _record_run(
        self,
        execution: ProjectExecution,
        task_id: int,
        run: FactoryRun,
    ) -> ProjectExecution:
        records = list(execution.tasks)
        records[task_id - 1] = records[task_id - 1].model_copy(update={"run_id": run.id})
        updated = execution.model_copy(update={"tasks": tuple(records), "updated_at": utc_now()})
        self._project_store.save_execution(updated)
        return updated

    def _finish_task(
        self,
        execution: ProjectExecution,
        task_id: int,
        commit_sha: str | None,
    ) -> ProjectExecution:
        records = list(execution.tasks)
        records[task_id - 1] = records[task_id - 1].model_copy(
            update={"state": ProjectTaskState.DONE, "commit_sha": commit_sha}
        )
        updated = execution.model_copy(update={"tasks": tuple(records), "updated_at": utc_now()})
        self._project_store.save_execution(updated)
        return updated

    def _finish_failure(
        self,
        execution: ProjectExecution,
        task_id: int,
        state: ProjectState,
        reason: str,
    ) -> ProjectExecution:
        records = list(execution.tasks)
        task_state = (
            ProjectTaskState.NEEDS_HUMAN
            if state is ProjectState.NEEDS_HUMAN
            else ProjectTaskState.FAILED
        )
        records[task_id - 1] = records[task_id - 1].model_copy(
            update={"state": task_state, "failure_reason": reason}
        )
        resolved_state = (
            ProjectState.FAILED
            if ProjectState.FAILED in {execution.state, state}
            else ProjectState.NEEDS_HUMAN
        )
        reasons = [item for item in (execution.failure_reason, reason) if item]
        updated = execution.model_copy(
            update={
                "state": resolved_state,
                "tasks": tuple(records),
                "failure_reason": "; ".join(dict.fromkeys(reasons)),
                "updated_at": utc_now(),
                "completed_at": utc_now(),
            }
        )
        self._project_store.save_execution(updated)
        return updated

    @staticmethod
    def _work_item_id(project_id: str, task_id: int) -> str:
        return f"{project_id}-task-{task_id}"

    def _to_work_item(
        self,
        brief: ProjectBrief,
        task: ProjectTask,
        *,
        issue_url: str | None,
    ) -> WorkItem:
        return WorkItem(
            id=self._work_item_id(brief.id, task.id),
            external_id=issue_url,
            source="MANUAL",
            title=task.title,
            description=task.description,
            acceptance_criteria=list(task.acceptance_criteria),
            constraints=list(task.constraints),
            labels=list(task.labels),
            priority=task.priority,
            project_id=brief.id,
            project_task_id=task.id,
            depends_on=list(task.dependencies),
        )

    @staticmethod
    def _issue_body(
        brief: ProjectBrief,
        task: ProjectTask,
        issue_urls: dict[int, str],
    ) -> str:
        criteria = "\n".join(f"- [ ] {item}" for item in task.acceptance_criteria)
        constraints = "\n".join(f"- {item}" for item in task.constraints) or "- None"
        dependencies = (
            "\n".join(f"- {issue_urls[item]}" for item in task.dependencies)
            if task.dependencies
            else "- None"
        )
        suggested_labels = "\n".join(f"- {label}" for label in task.labels) or "- None"
        return (
            f"Project: {brief.title}\n\n"
            f"{task.description}\n\n"
            f"## Acceptance criteria\n{criteria}\n\n"
            f"## Constraints\n{constraints}\n\n"
            f"## Depends on\n{dependencies}\n\n"
            f"## Suggested labels\n{suggested_labels}\n\n"
            f"<!-- software-agent-factory project={brief.id} task={task.id} -->"
        )

    def _commit_child(self, run: FactoryRun, task: ProjectTask) -> str:
        if run.workspace_path is None:
            raise ProjectError(f"task {task.id} completed without a workspace")
        workspace = Path(run.workspace_path)
        _run_git(workspace, "add", "-A")
        names = _run_git(workspace, "diff", "--cached", "--name-only").stdout
        changed_files = [line for line in names.splitlines() if line]
        if not changed_files:
            raise ProjectError(f"task {task.id} completed without repository changes")
        gate = assess_publish_gate(
            changed_files,
            max_changed_files=self._config.repository.max_changed_files,
            protected_file_patterns=self._config.repository.protected_file_patterns,
        )
        if not gate.allowed:
            raise ProjectError("; ".join(gate.violations))
        _run_git(
            workspace,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            f"Implement project task {task.id}: {task.title}",
        )
        return _run_git(workspace, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _cherry_pick(integration_path: Path, commit_sha: str) -> str | None:
        result = _run_git(
            integration_path,
            "-c",
            "commit.gpgsign=false",
            "cherry-pick",
            commit_sha,
            check=False,
        )
        if result.returncode == 0:
            return _run_git(integration_path, "rev-parse", "HEAD").stdout.strip()
        status = _run_git(integration_path, "status", "--porcelain", check=False)
        if not status.stdout.strip():
            skipped = _run_git(
                integration_path,
                "-c",
                "commit.gpgsign=false",
                "cherry-pick",
                "--skip",
                check=False,
            )
            if skipped.returncode == 0:
                return None
        _run_git(integration_path, "cherry-pick", "--abort", check=False)
        raise ProjectError(
            "independent project tasks produced conflicting changes while integrating "
            f"{commit_sha}: {result.stderr.strip()}"
        )


def _run_git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise ProjectError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result
