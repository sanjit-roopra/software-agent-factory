"""Project plan and project execution models."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, NamedTuple

from pydantic import Field, model_validator

from software_agent_factory.model_base import (
    ModelBase,
    NonBlankStr,
    UtcDateTime,
    VersionedModel,
    utc_now,
    without_blank_entries,
)
from software_agent_factory.report_models import VerificationReport
from software_agent_factory.usage_models import InvocationRecord

PROJECT_ID_PATTERN = r"^[A-Za-z0-9._-]{1,80}$"


#: The run classes a run list can narrow to. They are the classes the monitoring snapshot
#: counts: a run that waits for a person, a failed run, a finished run and any other run,
#: plus ``stuck``, an active run with no sign of life within ``stale_after``.
RUN_LIST_STATES: frozenset[str] = frozenset({"escalated", "failed", "succeeded", "active", "stuck"})


class RunListFilter(NamedTuple):
    """What narrows and orders one page of the run list. The default keeps every run.

    ``text`` matches, without case, part of the run id, the work item id, the issue id or
    the title. ``state`` is one of :data:`RUN_LIST_STATES`.
    """

    text: str | None = None
    state: str | None = None
    project_id: str | None = None
    oldest_first: bool = False


MAX_PROJECT_TASKS = 12
MAX_PROJECT_TASK_ACCEPTANCE_CRITERIA = 8
MAX_SINGLE_PROJECT_TASK_ACCEPTANCE_CRITERIA = 6
MAX_SINGLE_PROJECT_TASK_DESCRIPTION_CHARS = 2000


class ProjectState(StrEnum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class ProjectTaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class ProjectBrief(VersionedModel):
    id: str = Field(pattern=PROJECT_ID_PATTERN)
    title: NonBlankStr = Field(min_length=1, max_length=300)
    description: NonBlankStr = Field(min_length=1, max_length=20000)
    repository_path: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def _drop_blank_entries(cls, data: object) -> object:
        return without_blank_entries(data, ("acceptance_criteria", "constraints"))


class ProjectTask(ModelBase):
    id: int = Field(ge=1)
    title: NonBlankStr = Field(min_length=1, max_length=300)
    description: NonBlankStr = Field(min_length=1, max_length=10000)
    acceptance_criteria: tuple[NonBlankStr, ...] = Field(
        min_length=1,
        max_length=MAX_PROJECT_TASK_ACCEPTANCE_CRITERIA,
    )
    constraints: tuple[NonBlankStr, ...] = Field(default=(), max_length=30)
    dependencies: tuple[int, ...] = Field(default=(), max_length=8)
    priority: str | None = Field(default=None, max_length=20)
    labels: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _validate_dependencies(self) -> ProjectTask:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("project task dependencies must be unique")
        if any(dependency < 1 or dependency >= self.id for dependency in self.dependencies):
            raise ValueError("project task dependencies must reference valid earlier task ids")
        return self


class ProjectPlan(VersionedModel):
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    summary: NonBlankStr = Field(min_length=1, max_length=2000)
    delivery_approach: NonBlankStr = Field(min_length=1, max_length=2000)
    tasks: tuple[ProjectTask, ...] = Field(min_length=1, max_length=MAX_PROJECT_TASKS)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_tasks(self) -> ProjectPlan:
        expected_ids = list(range(1, len(self.tasks) + 1))
        task_ids = [task.id for task in self.tasks]
        if task_ids != expected_ids:
            raise ValueError("project task ids must be contiguous and ordered from 1")
        normalized_titles = [task.title.strip().casefold() for task in self.tasks]
        if len(set(normalized_titles)) != len(normalized_titles):
            raise ValueError("project task titles must be unique")
        if len(self.tasks) == 1:
            task = self.tasks[0]
            if len(task.acceptance_criteria) > MAX_SINGLE_PROJECT_TASK_ACCEPTANCE_CRITERIA:
                raise ValueError(
                    "a single project task may have at most "
                    f"{MAX_SINGLE_PROJECT_TASK_ACCEPTANCE_CRITERIA} acceptance criteria; "
                    "split independently verifiable or prerequisite outcomes into a task DAG"
                )
            if len(task.description) > MAX_SINGLE_PROJECT_TASK_DESCRIPTION_CHARS:
                raise ValueError(
                    "a single project task description may have at most "
                    f"{MAX_SINGLE_PROJECT_TASK_DESCRIPTION_CHARS} characters; split the "
                    "project into reviewable, independently verifiable task outcomes"
                )
        return self


class ProjectTaskExecution(ModelBase):
    task_id: int = Field(ge=1)
    work_item_id: str = Field(min_length=1)
    state: ProjectTaskState = ProjectTaskState.PENDING
    issue_url: str | None = None
    run_id: str | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None
    merge_commit_sha: str | None = None
    failure_reason: str | None = None


class ProjectExecution(VersionedModel):
    project_id: str = Field(pattern=PROJECT_ID_PATTERN)
    state: ProjectState
    tasks: tuple[ProjectTaskExecution, ...] = ()
    invocation_records: list[InvocationRecord] = Field(default_factory=list)
    integration_workspace: str | None = None
    integration_branch: str | None = None
    delivery_mode: Literal["local", "merge"] = "local"
    github_repository: str | None = None
    delivery_base_branch: str | None = None
    delivery_repository: str | None = None
    delivery_host: str | None = None
    delivery_policy_fingerprint: str | None = None
    risk_assessment_enabled: bool = True
    #: ``factory.unattended`` when the project started (ADR-044).
    unattended: bool = False
    created_at: UtcDateTime = Field(default_factory=utc_now)
    updated_at: UtcDateTime = Field(default_factory=utc_now)
    completed_at: UtcDateTime | None = None
    failure_reason: str | None = None
    warnings: tuple[str, ...] = ()
    #: Why an unattended project ends ``DONE`` with work that is not on the target:
    #: a task that did not merge, a task skipped because of it, a failed final check (ADR-044).
    needs_look: tuple[str, ...] = ()
    verification_report: VerificationReport | None = None

    @model_validator(mode="after")
    def _validate_execution(self) -> ProjectExecution:
        task_ids = [task.task_id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("project execution task ids must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must be greater than or equal to created_at")
        return self
