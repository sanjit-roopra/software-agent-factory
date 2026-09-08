"""Project-level decomposition and bounded execution.

The project layer is intentionally small: one planner proposes a typed flat
task DAG, deterministic code validates and persists it, and the existing
``WorkflowController`` executes every task. A local integration worktree
composes successful child commits so dependent tasks see predecessor changes.

Two delivery modes exist (``ADR-022``):

``local`` (default)
    Child runs finish at ``PR_READY``/``DONE`` without publishing. Their
    commits are cherry-picked onto one local integration branch.

``merge`` (opt-in, ``merge.enabled``)
    Every child run publishes a pull request, passes CI (with the existing
    bounded repair budget) and is merged by the controller. Tasks run serially
    from a dedicated integration worktree that is fast-forwarded to the freshly
    fetched target branch, so a dependent task always sees its merged
    predecessors. The user's own checkout is never checked out or reset.

Both modes support explicit, evidence-based recovery through :meth:`
ProjectRunner.resume`: nothing is replanned, no attempt budget is reset, and
no already-integrated change is applied twice.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

from .agents import AgentRequest, AgentRuntime, runtime_exception_failure_reason
from .config import FactoryConfig
from .delivery import DeliveryTarget, fetch_delivery_target
from .github import (
    GitHubClient,
    GitHubCommandError,
    GitHubError,
    GitPublishError,
    is_safe_ref_name,
)
from .governance import RepositoryVerifier, assess_publish_gate
from .models import (
    AgentPurpose,
    AgentRole,
    FactoryRun,
    InvocationRecord,
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
from .publishing import PullRequestMerger, resolve_github_token
from .repository_profile import profile_repository
from .routing import ModelRouter
from .store import FileRunStore, ImmutableArtifactConflictError, validate_run_id
from .workflow import (
    TransitionError,
    WorkflowController,
    delivery_policy_fingerprint,
    is_run_finished,
)
from .workspace import GitWorktreeWorkspace, WorkspaceError, WorkspaceLockError

ProjectArtifact = TypeVar("ProjectArtifact", bound=VersionedModel)
_PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_SUCCESS_STATES = frozenset({WorkflowState.PR_READY, WorkflowState.DONE})
_FACTORY_GIT_NAME = "Software Agent Factory"
_FACTORY_GIT_EMAIL = "software-agent-factory@example.invalid"
_FACTORY_GIT_IDENTITY = (
    "-c",
    f"user.name={_FACTORY_GIT_NAME}",
    "-c",
    f"user.email={_FACTORY_GIT_EMAIL}",
)
_FACTORY_GIT_ENV = {
    "GIT_AUTHOR_NAME": _FACTORY_GIT_NAME,
    "GIT_AUTHOR_EMAIL": _FACTORY_GIT_EMAIL,
    "GIT_COMMITTER_NAME": _FACTORY_GIT_NAME,
    "GIT_COMMITTER_EMAIL": _FACTORY_GIT_EMAIL,
}
#: How far back the recovery duplicate-integration proof looks. A project plan
#: holds at most 12 tasks, so this comfortably covers every commit the factory
#: itself could have added to one integration branch.
_INTEGRATION_SEARCH_DEPTH = 40
_MAX_DECOMPOSITION_ATTEMPTS = 2


class ProjectError(RuntimeError):
    """Raised when project planning or deterministic integration cannot continue."""


#: Conservative Git remote/branch name shape, applied on top of the shared
#: :func:`~software_agent_factory.github.is_safe_ref_name` check so the project
#: layer can never be laxer than the merge adapter. Deliberately narrower than
#: ``git check-ref-format``: these values are configured by a human and are
#: passed to ``git fetch`` as positional arguments, so anything that could be
#: read as an option, a refspec, a path or a revision expression is refused.
_GIT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


def validate_git_name(value: str, field: str) -> str:
    """Validate one configured Git remote or branch name, or raise."""
    if (
        not is_safe_ref_name(value)
        or _GIT_NAME_PATTERN.fullmatch(value) is None
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "//" in value
        or "@{" in value
    ):
        raise ProjectError(
            f"{field} must be a plain Git name (letters, digits, '.', '_', '-', '/'), got {value!r}"
        )
    return value


@dataclass(frozen=True)
class DeliverySettings:
    """Deterministic, human-configured delivery identity for one project.

    Resolved *before* any planner call, network access or ``git fetch`` so an
    unauthorized repository or target branch costs nothing and reaches nothing.
    """

    mode: str
    remote: str | None = None
    base_branch: str | None = None
    repository: str | None = None
    #: Host the first authorized fetch resolved, pinned for every later fetch.
    host: str | None = None
    #: Commit the dedicated integration worktree was created from, used to tell
    #: untouched factory scaffolding from real integration history.
    integration_base: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.mode == "merge"


def resolve_delivery_repository(config: FactoryConfig, source_repo: Path) -> str:
    """Return the authorized ``OWNER/REPO`` for ``source_repo``'s remote.

    Delegates to the single controller-side authorization helper
    (:meth:`~software_agent_factory.publishing.PullRequestMerger.validate_repository`)
    so the project layer cannot drift from the merge adapter's host and
    repository allowlists. It only reads the configured remote URL from local
    Git configuration, so an unauthorized target is rejected before anything is
    fetched, planned or paid for.
    """
    try:
        return PullRequestMerger(config).validate_repository(source_repo)
    except (GitHubError, GitPublishError, OSError, ValueError) as exc:
        raise ProjectError(f"delivery repository is not authorized: {exc}") from exc


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
        delivery_repository_resolver: Callable[[Path], str] | None = None,
        delivery_base_resolver: Callable[[Path, str], DeliveryTarget] | None = None,
    ) -> None:
        if not config.merge.enabled and (config.pull_request.enabled or config.ci.enabled):
            raise ValueError(
                "local project execution requires pull_request.enabled=false and "
                "ci.enabled=false so child changes can be composed on one local branch; "
                "enable merge.enabled for autonomous remote delivery"
            )
        self._config = config
        self._run_store = run_store
        self._runtime = runtime
        self._project_store = project_store or FileProjectStore(config.data_dir)
        self._github = github_client or GitHubClient(token=resolve_github_token())
        self._controller = controller or WorkflowController(config, run_store, runtime)
        self._repository_verifier = repository_verifier or RepositoryVerifier()
        self._router = ModelRouter(config)
        self._delivery_repository_resolver = delivery_repository_resolver
        # Shared with the workflow controller: the transport URL is pinned, the
        # target ref is fetched into a private ref rather than shared
        # ``FETCH_HEAD``, and the repository identity is re-checked at fetch time.
        self._delivery_base_resolver = delivery_base_resolver

    # -- delivery policy ------------------------------------------------

    def _delivery_settings(self, source_repo: Path) -> DeliverySettings:
        """Authorize the delivery target before any planning or network use."""
        if not self._config.merge.enabled:
            return DeliverySettings(mode="local")
        base_branch = self._config.pull_request.base_branch
        if not base_branch:
            raise ProjectError("remote project delivery requires an explicit base branch")
        remote = self._config.pull_request.remote
        # Both values reach ``git fetch`` as positional arguments, so they are
        # validated as plain names here: no option, refspec or path can be
        # smuggled in through configuration.
        validate_git_name(remote, "pull_request.remote")
        validate_git_name(base_branch, "pull_request.base_branch")
        if self._delivery_repository_resolver is not None:
            repository = self._delivery_repository_resolver(source_repo)
        else:
            repository = resolve_delivery_repository(self._config, source_repo)
        return DeliverySettings(
            mode="merge",
            remote=remote,
            base_branch=base_branch,
            repository=repository,
        )

    def _project_workspace(self, project_id: str, source_repo: Path) -> GitWorktreeWorkspace:
        return GitWorktreeWorkspace(
            self._config.data_dir,
            source_repo,
            f"project-{project_id}",
            branch_prefix=self._config.repository.branch_prefix,
        )

    def run(
        self,
        brief: ProjectBrief,
        source_repo: Path,
        *,
        github_repository: str | None = None,
    ) -> ProjectExecution:
        source_repo = source_repo.expanduser().resolve()
        delivery = self._delivery_settings(source_repo)
        project_workspace = self._project_workspace(brief.id, source_repo)
        self._acquire_project_lock(project_workspace, brief.id)
        execution = ProjectExecution(project_id=brief.id, state=ProjectState.PLANNING)

        try:
            # Duplicate-project rejection happens before any failure recording:
            # a previous execution's persisted state is reconciled, never
            # overwritten by this invocation's outcome.
            if self._project_store.exists(brief.id):
                self._mark_abandoned(brief.id)
                raise ProjectError(
                    f"project {brief.id!r} already exists; choose a new --project-id "
                    "or resume it with --resume"
                )
            self._project_store.save_brief_once(brief)
            execution = execution.model_copy(
                update={
                    "delivery_mode": delivery.mode,
                    "github_repository": github_repository,
                    "delivery_base_branch": delivery.base_branch,
                    "delivery_repository": delivery.repository,
                    "delivery_policy_fingerprint": delivery_policy_fingerprint(self._config),
                    "updated_at": utc_now(),
                }
            )
            self._project_store.save_execution(execution)

            try:
                # Remote delivery must never start from the user's checkout: it
                # may be ahead of, or diverged from, the target and would carry
                # unreviewed commits into every pull request. The worktree is
                # created at exactly the fetched target commit instead.
                base_ref: str | None = None
                if delivery.is_remote:
                    initial = self._fetch_delivery_target(source_repo, delivery)
                    base_ref = initial.commit_sha
                    delivery = replace(delivery, host=initial.host)
                integration_path = project_workspace.prepare(base_ref=base_ref)
                delivery = replace(
                    delivery, integration_base=base_ref or project_workspace.base_commit
                )
                execution = execution.model_copy(
                    update={
                        "integration_workspace": str(integration_path),
                        "integration_branch": project_workspace.branch_name,
                        "delivery_host": delivery.host,
                        "updated_at": utc_now(),
                    }
                )
                self._project_store.save_execution(execution)

                plan = self._plan(brief, integration_path, execution)
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
                    delivery,
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
                execution = self._record_project_failure(brief.id, execution, exc)
        finally:
            project_workspace.release_lock()
        return execution

    def resume(self, project_id: str, source_repo: Path) -> ProjectExecution:
        """Continue an interrupted project from persisted evidence only.

        The stored brief and plan are authoritative: nothing is replanned, no
        completed task is implemented again, no retry budget is reset and a
        previously recorded rejection is retained.
        """
        source_repo = source_repo.expanduser().resolve()
        delivery = self._delivery_settings(source_repo)
        if not self._project_store.exists(project_id):
            raise ProjectError(f"project {project_id!r} has no persisted execution to resume")
        project_workspace = self._project_workspace(project_id, source_repo)
        self._acquire_project_lock(project_workspace, project_id)
        try:
            # Validation raises without rewriting persisted state: a project
            # that is already rejected, drifted or unresumable must be
            # preserved exactly as the previous process left it.
            brief = self._project_store.load_brief(project_id)
            execution = self._project_store.load_execution(project_id)
            if Path(brief.repository_path).expanduser().resolve() != source_repo:
                raise ProjectError(
                    "--repo does not match the repository this project was started against"
                )
            if execution.state is ProjectState.DONE:
                return execution
            self._check_delivery_identity(execution, delivery)
            if any(
                record.state in {ProjectTaskState.FAILED, ProjectTaskState.NEEDS_HUMAN}
                for record in execution.tasks
            ):
                raise ProjectError(
                    "this project has a recorded task rejection; it is preserved for a human "
                    "and is never retried automatically"
                )
            try:
                plan = self._project_store.load_plan(project_id)
            except FileNotFoundError as exc:
                raise ProjectError(
                    "project planning never completed; start a new project id instead of "
                    "replanning a partially planned project"
                ) from exc
            if not execution.tasks:
                raise ProjectError("project execution has no persisted task records to reconcile")

            integration_path = project_workspace.prepare()
            # The host the project was authorized against is pinned across the
            # resume, so a remote re-pointed to another host is refused.
            delivery = replace(
                delivery,
                integration_base=project_workspace.base_commit,
                host=execution.delivery_host,
            )
            if execution.integration_workspace != str(integration_path) or (
                execution.integration_branch != project_workspace.branch_name
            ):
                raise ProjectError(
                    "the project integration workspace identity changed; refusing to resume"
                )
            execution = execution.model_copy(
                update={"state": ProjectState.RUNNING, "updated_at": utc_now()}
            )
            self._project_store.save_execution(execution)
            try:
                execution = self._execute_plan(
                    brief,
                    plan,
                    execution,
                    source_repo,
                    integration_path,
                    execution.github_repository,
                    delivery,
                    resuming=True,
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
                execution = self._record_project_failure(project_id, execution, exc)
        finally:
            project_workspace.release_lock()
        return execution

    def _check_delivery_identity(
        self, execution: ProjectExecution, delivery: DeliverySettings
    ) -> None:
        current = delivery_policy_fingerprint(self._config)
        if execution.delivery_policy_fingerprint != current:
            raise ProjectError(
                "delivery policy changed since this project started; refusing to resume"
            )
        if execution.delivery_mode != delivery.mode:
            raise ProjectError("delivery mode changed since this project started")
        if execution.delivery_base_branch != delivery.base_branch:
            raise ProjectError("delivery base branch changed since this project started")
        # ``OWNER/REPO`` is case-insensitive on GitHub, and the authorization
        # helper returns the configured casing, so only a real identity change
        # (not a re-cased allowlist entry) may block a resume.
        stored_repository = (execution.delivery_repository or "").casefold()
        if stored_repository != (delivery.repository or "").casefold():
            raise ProjectError("delivery repository changed since this project started")

    def _acquire_project_lock(
        self, project_workspace: GitWorktreeWorkspace, project_id: str
    ) -> None:
        """Take the project lock *before* reading or reconciling state so a
        live run in another process is never mistaken for an abandoned one."""
        try:
            project_workspace.acquire_lock()
        except WorkspaceLockError as exc:
            raise ProjectError(
                f"project {project_id!r} is already being executed by another process: {exc}"
            ) from exc

    def _mark_abandoned(self, project_id: str) -> None:
        existing = self._project_store.load_execution(project_id)
        if existing.state in {ProjectState.PLANNING, ProjectState.RUNNING}:
            now = utc_now()
            self._project_store.save_execution(
                existing.model_copy(
                    update={
                        "state": ProjectState.NEEDS_HUMAN,
                        "failure_reason": (
                            "project execution was abandoned by a previous process; "
                            "artifacts and worktrees were preserved"
                        ),
                        "updated_at": now,
                        "completed_at": now,
                    }
                )
            )

    def _record_project_failure(
        self,
        project_id: str,
        execution: ProjectExecution,
        exc: Exception,
    ) -> ProjectExecution:
        try:
            execution = self._project_store.load_execution(project_id)
        except FileNotFoundError:
            pass
        now = utc_now()
        tasks = tuple(
            record.model_copy(
                update={
                    "state": ProjectTaskState.FAILED,
                    "failure_reason": str(exc),
                }
            )
            if record.state is ProjectTaskState.RUNNING
            else record
            for record in execution.tasks
        )
        execution = execution.model_copy(
            update={
                "state": ProjectState.FAILED,
                "tasks": tasks,
                "failure_reason": str(exc),
                "updated_at": now,
                "completed_at": now,
            }
        )
        self._project_store.save_execution(execution)
        return execution

    def _plan(
        self,
        brief: ProjectBrief,
        source_repo: Path,
        execution: ProjectExecution,
    ) -> ProjectPlan:
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
        rejection: str | None = None
        for _attempt in range(1, _MAX_DECOMPOSITION_ATTEMPTS + 1):
            request = AgentRequest(
                role=AgentRole.PLANNER,
                purpose=AgentPurpose.DECOMPOSE_PROJECT,
                model=model.model,
                reasoning=model.reasoning,
                context_tier=model.context_tier,
                work_item=synthetic_work_item,
                project_brief=brief,
                repository_profile=profile,
                repair_context=rejection,
                workspace_path=str(source_repo),
                timeout_seconds=self._config.agent_timeout_seconds,
            )
            started_at = utc_now()
            try:
                result = self._runtime.run(request)
            except (OSError, RuntimeError, ValueError) as exc:
                completed_at = utc_now()
                execution.invocation_records.append(
                    InvocationRecord(
                        invocation_number=len(execution.invocation_records) + 1,
                        role=request.role,
                        purpose=request.purpose,
                        model=request.model,
                        reasoning=request.reasoning,
                        context_tier=request.context_tier,
                        started_at=started_at,
                        completed_at=completed_at,
                        success=False,
                        failure_reason=runtime_exception_failure_reason(exc),
                    )
                )
                execution.updated_at = completed_at
                self._project_store.save_execution(execution)
                raise
            completed_at = utc_now()
            execution.invocation_records.append(
                InvocationRecord(
                    invocation_number=len(execution.invocation_records) + 1,
                    role=request.role,
                    purpose=request.purpose,
                    model=request.model,
                    reasoning=request.reasoning,
                    context_tier=request.context_tier,
                    started_at=started_at,
                    completed_at=completed_at,
                    success=result.success,
                    failure_reason=result.failure_reason,
                    usage=result.usage,
                )
            )
            execution.updated_at = completed_at
            self._project_store.save_execution(execution)
            if result.success and result.project_plan is not None:
                return result.project_plan.model_copy(update={"project_id": brief.id})
            rejection = result.failure_reason or "project planner failed to produce a ProjectPlan"
        raise ProjectError(rejection or "project planner failed to produce a ProjectPlan")

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
            records[task.id - 1] = records[task.id - 1].model_copy(update={"issue_url": issue_url})
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
        delivery: DeliverySettings,
        *,
        resuming: bool = False,
    ) -> ProjectExecution:
        pending = {task.id: task for task in plan.tasks}
        completed: set[int] = set()
        if resuming:
            execution, completed = self._reconcile_completed_tasks(
                execution, plan, integration_path, delivery
            )
            for task_id in completed:
                pending.pop(task_id, None)
        while pending:
            ready = [
                task for task in pending.values() if set(task.dependencies).issubset(completed)
            ]
            if not ready:
                raise ProjectError("project plan has unfinished tasks but no ready task")
            ready.sort(key=lambda task: task.id)
            # Remote delivery is deliberately serial: each task must start from
            # the target branch its merged predecessors are already on.
            wave_size = 1 if delivery.is_remote else self._config.scheduler.max_concurrent_tasks
            wave = ready[:wave_size]
            if delivery.is_remote:
                target = self._refresh_target(integration_path, delivery)
                self._assert_at_target(integration_path, target, delivery)
            execution = self._mark_running(execution, wave)
            execution = self._assign_run_ids(execution, brief, wave)
            results, errors = self._run_wave(
                brief,
                plan,
                wave,
                execution,
                integration_path,
                resuming=resuming,
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
                    if delivery.is_remote:
                        execution = self._integrate_remote_task(
                            execution, task, run, integration_path, delivery
                        )
                    else:
                        execution = self._integrate_local_task(
                            execution, task, run, integration_path
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

        final_target: str | None = None
        if delivery.is_remote:
            # The project is only green if the *fetched* target branch, with
            # every merged task on it, passes deterministic verification.
            final_target = self._refresh_target(integration_path, delivery)
            self._assert_at_target(integration_path, final_target, delivery)
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

        if final_target is not None:
            self._assert_at_target(integration_path, final_target, delivery)
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
        plan: ProjectPlan,
        tasks: list[ProjectTask],
        execution: ProjectExecution,
        integration_path: Path,
        *,
        resuming: bool = False,
    ) -> tuple[dict[int, FactoryRun], dict[int, str]]:
        records = {record.task_id: record for record in execution.tasks}
        futures: dict[int, Future[FactoryRun]] = {}
        results: dict[int, FactoryRun] = {}
        errors: dict[int, str] = {}
        with ThreadPoolExecutor(
            max_workers=self._config.scheduler.max_concurrent_tasks,
            thread_name_prefix=f"project-{brief.id}",
        ) as executor:
            for task in tasks:
                record = records[task.id]
                run_id = record.run_id
                assert run_id is not None
                existing = self._load_run(run_id)
                if existing is not None and not resuming:
                    errors[task.id] = (
                        f"child run {run_id!r} already exists for task {task.id}; "
                        "resume the project instead of dispatching duplicate work"
                    )
                    continue
                if existing is not None:
                    if is_run_finished(existing):
                        results[task.id] = existing
                        continue
                    futures[task.id] = executor.submit(
                        self._controller.resume,
                        run_id,
                        integration_path,
                    )
                    continue
                work_item = self._to_work_item(
                    brief,
                    task,
                    project_tasks=plan.tasks,
                    issue_url=record.issue_url,
                )
                futures[task.id] = executor.submit(
                    self._dispatch_child,
                    work_item,
                    integration_path,
                    run_id,
                )
        for task_id, future in futures.items():
            try:
                results[task_id] = future.result()
            except (OSError, RuntimeError, TransitionError, ValueError, WorkspaceError) as exc:
                errors[task_id] = str(exc)
        return results, errors

    def _dispatch_child(
        self, work_item: WorkItem, integration_path: Path, run_id: str
    ) -> FactoryRun:
        return self._controller.run(work_item, integration_path, run_id=run_id)

    def _load_run(self, run_id: str) -> FactoryRun | None:
        try:
            return self._run_store.load_run(run_id)
        except FileNotFoundError:
            return None

    def _assign_run_ids(
        self,
        execution: ProjectExecution,
        brief: ProjectBrief,
        tasks: list[ProjectTask],
    ) -> ProjectExecution:
        """Persist each child run id *before* dispatch so a crashed process can
        be reconciled against real run state instead of duplicating work."""
        records = list(execution.tasks)
        changed = False
        for task in tasks:
            record = records[task.id - 1]
            if record.run_id is not None:
                continue
            records[task.id - 1] = record.model_copy(
                update={"run_id": self._child_run_id(brief.id, task.id)}
            )
            changed = True
        if not changed:
            return execution
        updated = execution.model_copy(update={"tasks": tuple(records), "updated_at": utc_now()})
        self._project_store.save_execution(updated)
        return updated

    @staticmethod
    def _child_run_id(project_id: str, task_id: int) -> str:
        return validate_run_id(f"run-{project_id}-task-{task_id}")

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
        *,
        pull_request_url: str | None = None,
        merge_commit_sha: str | None = None,
    ) -> ProjectExecution:
        records = list(execution.tasks)
        records[task_id - 1] = records[task_id - 1].model_copy(
            update={
                "state": ProjectTaskState.DONE,
                "commit_sha": commit_sha,
                "pull_request_url": pull_request_url,
                "merge_commit_sha": merge_commit_sha,
            }
        )
        updated = execution.model_copy(update={"tasks": tuple(records), "updated_at": utc_now()})
        self._project_store.save_execution(updated)
        return updated

    # -- integration ----------------------------------------------------

    def _integrate_local_task(
        self,
        execution: ProjectExecution,
        task: ProjectTask,
        run: FactoryRun,
        integration_path: Path,
    ) -> ProjectExecution:
        child_commit_sha = self._commit_child(run, task)
        record = execution.tasks[task.id - 1]
        if record.commit_sha is not None and _commit_exists(integration_path, record.commit_sha):
            # A previous process integrated and persisted this task already.
            return self._finish_task(execution, task.id, record.commit_sha)
        if _patch_already_integrated(integration_path, child_commit_sha):
            return self._finish_task(execution, task.id, record.commit_sha)
        integration_commit_sha = self._cherry_pick(integration_path, child_commit_sha)
        return self._finish_task(execution, task.id, integration_commit_sha)

    def _integrate_remote_task(
        self,
        execution: ProjectExecution,
        task: ProjectTask,
        run: FactoryRun,
        integration_path: Path,
        delivery: DeliverySettings,
    ) -> ProjectExecution:
        if run.state is not WorkflowState.DONE:
            raise ProjectError(
                f"task {task.id} did not complete pull request delivery "
                f"(child run finished in {run.state})"
            )
        if not run.pull_request_url or not run.merge_commit_sha:
            raise ProjectError(f"task {task.id} has no persisted pull request and merge evidence")
        # Defense in depth over the controller's own gate: the merged head must
        # be exactly the revision the independent Reviewer approved, so a CI
        # repair push can never inherit an earlier revision's approval.
        if not run.commit_sha or run.reviewed_commit_sha != run.commit_sha:
            raise ProjectError(
                f"task {task.id} was delivered without independent Reviewer approval "
                "bound to its published head"
            )
        # The Reviewer approves a tree, not just a commit id: bind the published
        # head to the tree the independent review actually saw whenever that
        # commit object is available locally (it is for a merge-commit merge).
        if not run.reviewed_tree_sha:
            raise ProjectError(f"task {task.id} was delivered without a Reviewer-approved Git tree")
        target = self._refresh_target(integration_path, delivery)
        published_tree = _tree_of(integration_path, run.commit_sha)
        if published_tree is not None and published_tree != run.reviewed_tree_sha:
            raise ProjectError(
                f"task {task.id} merged tree {published_tree} but the independent Reviewer "
                f"approved {run.reviewed_tree_sha}"
            )
        if not _is_ancestor(integration_path, run.merge_commit_sha, target):
            raise ProjectError(
                f"task {task.id} reported merge commit {run.merge_commit_sha} but it is not "
                f"in the fetched {delivery.remote}/{delivery.base_branch} history"
            )
        return self._finish_task(
            execution,
            task.id,
            run.commit_sha,
            pull_request_url=run.pull_request_url,
            merge_commit_sha=run.merge_commit_sha,
        )

    def _refresh_target(self, integration_path: Path, delivery: DeliverySettings) -> str:
        """Re-fetch the authorized target branch and require the dedicated
        integration worktree to sit on exactly that commit. The user's own
        checkout is never inspected, checked out or reset.

        Only a genuine fast-forward is permitted. An ahead branch is refused
        before Git runs, and head equality is checked afterwards: success of
        ``git merge --ff-only`` alone would not exclude unpublished history.
        """
        assert delivery.remote is not None and delivery.base_branch is not None
        remote, base_branch = delivery.remote, delivery.base_branch
        target = self._fetch_target(integration_path, delivery)
        head = _run_git(integration_path, "rev-parse", "HEAD").stdout.strip()
        if head != target:
            if _run_git(integration_path, "status", "--porcelain").stdout.strip():
                raise ProjectError(
                    "the project integration worktree has uncommitted changes; refusing to "
                    f"re-root it on {remote}/{base_branch}"
                )
            if not _is_ancestor(integration_path, head, target):
                raise ProjectError(
                    "the project integration branch contains commits that are not on "
                    f"{remote}/{base_branch}; refusing to deliver work from unpublished history"
                )
            _run_git(integration_path, "merge", "--ff-only", "--quiet", target)
        self._assert_at_target(integration_path, target, delivery)
        return target

    def _fetch_target(self, repo_path: Path, delivery: DeliverySettings) -> str:
        return self._fetch_delivery_target(repo_path, delivery).commit_sha

    def _fetch_delivery_target(self, repo_path: Path, delivery: DeliverySettings) -> DeliveryTarget:
        """Fetch the configured target branch, re-validating repository identity.

        The resolver re-reads and re-authorizes the configured remote on every
        call and pins the parsed URL for the transport, so a remote that is
        renamed or re-pointed mid-project is refused instead of followed.
        """
        assert delivery.repository is not None
        try:
            target = (
                self._delivery_base_resolver(repo_path, delivery.repository)
                if self._delivery_base_resolver is not None
                else fetch_delivery_target(
                    self._config,
                    repo_path,
                    delivery.repository,
                    expected_host=delivery.host,
                )
            )
        except (GitHubError, GitPublishError, OSError, ValueError) as exc:
            raise ProjectError(
                f"could not fetch {delivery.remote}/{delivery.base_branch}: {exc}"
            ) from exc
        if target.repository.casefold() != delivery.repository.casefold():
            raise ProjectError("delivery repository identity changed before target fetch")
        if delivery.host is not None and target.host.casefold() != delivery.host.casefold():
            raise ProjectError(
                f"delivery host changed from {delivery.host} to {target.host}; refusing to fetch"
            )
        return target

    def _assert_at_target(
        self, integration_path: Path, target: str, delivery: DeliverySettings
    ) -> str:
        """Fail closed unless the integration head is exactly the fetched target."""
        head = _run_git(integration_path, "rev-parse", "HEAD").stdout.strip()
        if head != target:
            raise ProjectError(
                f"the project integration worktree is at {head}, not the fetched "
                f"{delivery.remote}/{delivery.base_branch} commit {target}"
            )
        if _run_git(integration_path, "status", "--porcelain").stdout.strip():
            raise ProjectError("project integration worktree contains unverified changes")
        return head

    def _reconcile_completed_tasks(
        self,
        execution: ProjectExecution,
        plan: ProjectPlan,
        integration_path: Path,
        delivery: DeliverySettings,
    ) -> tuple[ProjectExecution, set[int]]:
        """Confirm which recorded task outcomes are real before dispatching."""
        if len(execution.tasks) != len(plan.tasks):
            raise ProjectError("persisted task records do not match the immutable project plan")
        target = self._refresh_target(integration_path, delivery) if delivery.is_remote else None
        completed: set[int] = set()
        for record in execution.tasks:
            if record.state is not ProjectTaskState.DONE:
                continue
            if delivery.is_remote:
                assert target is not None
                if not record.merge_commit_sha or not _is_ancestor(
                    integration_path, record.merge_commit_sha, target
                ):
                    raise ProjectError(
                        f"task {record.task_id} is recorded as delivered but its merge commit "
                        "is not in the fetched target history"
                    )
            completed.add(record.task_id)
        return execution, completed

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
        project_tasks: tuple[ProjectTask, ...],
        issue_url: str | None,
    ) -> WorkItem:
        # Project-wide constraints are applied deterministically rather than
        # trusting the planner to copy them into every task.
        predecessors = "; ".join(
            f"task {candidate.id}: {candidate.title}"
            for candidate in project_tasks
            if candidate.id in task.dependencies
        )
        sibling_boundaries = "; ".join(
            f"task {candidate.id}: {candidate.title}"
            for candidate in project_tasks
            if candidate.id != task.id and candidate.id not in task.dependencies
        )
        predecessor_context = (
            (
                (
                    "Integrated project predecessors are already available in this branch and may "
                    f"be reused or extended where this task requires it: {predecessors}"
                ),
            )
            if predecessors
            else ()
        )
        future_boundaries = (
            (
                (
                    "Project task boundary: implement only this task. These outcomes are assigned "
                    f"to separate project tasks and must not be implemented here: "
                    f"{sibling_boundaries}"
                ),
            )
            if sibling_boundaries
            else ()
        )
        constraints = list(
            dict.fromkeys(
                (
                    *brief.constraints,
                    *task.constraints,
                    *predecessor_context,
                    *future_boundaries,
                )
            )
        )
        return WorkItem(
            id=self._work_item_id(brief.id, task.id),
            external_id=issue_url,
            source="MANUAL",
            title=task.title,
            description=(
                f"Project context: {brief.title}\n\n{brief.description}\n\n"
                f"Current task: {task.description}"
            ),
            acceptance_criteria=list(task.acceptance_criteria),
            constraints=constraints,
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
        message = self._child_commit_message(task)
        _run_git(workspace, "add", "-A")
        names = _run_git(workspace, "diff", "--cached", "--name-only").stdout
        changed_files = [line for line in names.splitlines() if line]
        if not changed_files:
            head = _run_git(workspace, "rev-parse", "HEAD").stdout.strip()
            subject = _run_git(workspace, "log", "-1", "--format=%s").stdout.strip()
            if subject == message:
                # A previous process already committed this task's work and was
                # interrupted before integration; reuse it instead of failing.
                return head
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
            *_FACTORY_GIT_IDENTITY,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            message,
            env_overrides=_FACTORY_GIT_ENV,
        )
        return _run_git(workspace, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _child_commit_message(task: ProjectTask) -> str:
        return f"Implement project task {task.id}: {task.title}"

    @staticmethod
    def _cherry_pick(integration_path: Path, commit_sha: str) -> str | None:
        result = _run_git(
            integration_path,
            *_FACTORY_GIT_IDENTITY,
            "-c",
            "commit.gpgsign=false",
            "cherry-pick",
            commit_sha,
            check=False,
            env_overrides=_FACTORY_GIT_ENV,
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
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = None
    if env_overrides is not None:
        env = {**os.environ, **env_overrides}
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise ProjectError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def _commit_exists(cwd: Path, commit_sha: str) -> bool:
    result = _run_git(cwd, "cat-file", "-e", f"{commit_sha}^{{commit}}", check=False)
    return result.returncode == 0


def _tree_of(repo_path: Path, commit: str) -> str | None:
    """Return ``commit``'s tree, or ``None`` when the object is not local."""
    completed = _run_git(
        repo_path, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}", check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _is_ancestor(cwd: Path, commit_sha: str, descendant: str) -> bool:
    if not _commit_exists(cwd, commit_sha) or not _commit_exists(cwd, descendant):
        return False
    result = _run_git(cwd, "merge-base", "--is-ancestor", commit_sha, descendant, check=False)
    return result.returncode == 0


def _patch_id(cwd: Path, commit_sha: str) -> str | None:
    """Content identity of one commit's diff, stable across cherry-picks."""
    shown = _run_git(cwd, "show", "--no-color", commit_sha, check=False)
    if shown.returncode != 0 or not shown.stdout.strip():
        return None
    computed = subprocess.run(
        ["git", "-C", str(cwd), "patch-id", "--stable"],
        input=shown.stdout,
        capture_output=True,
        text=True,
    )
    identity = computed.stdout.split(" ", 1)[0].strip()
    return identity or None


def _patch_already_integrated(
    integration_path: Path, commit_sha: str, *, depth: int = _INTEGRATION_SEARCH_DEPTH
) -> bool:
    """True when the integration branch already contains this exact change.

    Used only during recovery so an interrupted process can never cherry-pick
    the same task twice; absence of proof means the change is applied normally.
    """
    target = _patch_id(integration_path, commit_sha)
    if target is None:
        return False
    listed = _run_git(
        integration_path,
        "rev-list",
        "--no-merges",
        f"--max-count={depth}",
        "HEAD",
        check=False,
    )
    if listed.returncode != 0:
        return False
    for candidate in listed.stdout.split():
        if _patch_id(integration_path, candidate) == target:
            return True
    return False
