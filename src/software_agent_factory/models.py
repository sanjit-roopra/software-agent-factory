from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_normalize_utc)]
MAX_OPEN_REVIEW_FINDINGS = 24


class WorkflowState(StrEnum):
    CREATED = "CREATED"
    TRIAGING = "TRIAGING"
    REFINING = "REFINING"
    RESEARCHING = "RESEARCHING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    PR_READY = "PR_READY"
    PR_CREATED = "PR_CREATED"
    CI_RUNNING = "CI_RUNNING"
    CI_DIAGNOSIS = "CI_DIAGNOSIS"
    DONE = "DONE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


class Complexity(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class Risk(StrEnum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class AgentRole(StrEnum):
    TRIAGE = "TRIAGE"
    REFINER = "REFINER"
    RESEARCHER = "RESEARCHER"
    PLANNER = "PLANNER"
    IMPLEMENTER = "IMPLEMENTER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"


class AgentPurpose(StrEnum):
    """The typed output contract for an agent invocation."""

    STANDARD = "STANDARD"
    DECOMPOSE_PROJECT = "DECOMPOSE_PROJECT"
    GENERATE_REPOSITORY_SKILL = "GENERATE_REPOSITORY_SKILL"


class ContextTier(StrEnum):
    DEFAULT = "default"
    LONG_CONTEXT = "long_context"


class AttemptBudget(StrEnum):
    """Which bounded retry budget an attempt consumes.

    ``IMPLEMENTATION`` covers the pre-PR implement/verify/review repair loop
    (``ADR-003``: one global budget shared by implementer, verification,
    review and scope failures). ``CI_REPAIR`` is the separate, independently
    bounded post-PR budget.
    """

    IMPLEMENTATION = "IMPLEMENTATION"
    CI_REPAIR = "CI_REPAIR"


class AttemptTrigger(StrEnum):
    """Why an attempt was started, recorded for auditability."""

    INITIAL = "INITIAL"
    POLISH = "POLISH"
    IMPLEMENTER_FAILURE = "IMPLEMENTER_FAILURE"
    VERIFICATION = "VERIFICATION"
    REVIEW = "REVIEW"
    SCOPE = "SCOPE"
    CI = "CI"


class ReviewFindingCategory(StrEnum):
    CORRECTNESS = "CORRECTNESS"
    SCOPE = "SCOPE"
    SECURITY = "SECURITY"
    COMPATIBILITY = "COMPATIBILITY"


class ReviewFindingOrigin(StrEnum):
    INITIAL = "INITIAL"
    LATE_ADOPTED = "LATE_ADOPTED"
    REPAIR_REGRESSION_DIRECT = "REPAIR_REGRESSION_DIRECT"
    REPAIR_REGRESSION_INDIRECT = "REPAIR_REGRESSION_INDIRECT"


class ReviewDispositionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    WITHDRAWN = "WITHDRAWN"


class ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionedModel(ModelBase):
    schema_version: Literal[1] = 1


class RepositoryTechnology(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    REACT = "react"
    VITE = "vite"


class RepositoryTestTool(StrEnum):
    PYTEST = "pytest"
    VITEST = "vitest"


class RepositoryPackageManager(StrEnum):
    UV = "uv"
    PIP = "pip"
    POETRY = "poetry"
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


class DependencyEcosystem(StrEnum):
    PYTHON = "python"
    NPM = "npm"


class RepositoryDependency(ModelBase):
    """One direct dependency declaration retained with its version evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ecosystem: DependencyEcosystem
    name: str = Field(min_length=1, max_length=200)
    declared_version: str = Field(min_length=1, max_length=500)
    resolved_version: str | None = Field(default=None, max_length=200)
    manifest_path: str = Field(min_length=1, max_length=1000)
    resolution_path: str | None = Field(default=None, max_length=1000)
    group: str = Field(min_length=1, max_length=100)


class RepositoryProfile(VersionedModel):
    """Deterministic, read-only repository facts used for skill research."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector_version: Literal[2] = 2
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    markers: tuple[str, ...] = ()
    version_files: tuple[str, ...] = ()
    technologies: tuple[RepositoryTechnology, ...] = ()
    test_tools: tuple[RepositoryTestTool, ...] = ()
    package_managers: tuple[RepositoryPackageManager, ...] = ()
    dependencies: tuple[RepositoryDependency, ...] = Field(default=(), max_length=200)
    warnings: tuple[str, ...] = ()


GENERIC_SKILL_TARGET = "repository"
"""Applicability marker for guidance that is not tied to a detected dependency."""

GENERIC_PRACTICE_VERSION_SCOPE = "general"
"""The only version scope a curated general-practice source may claim."""

REQUIRED_SKILL_TARGET_NAMES: tuple[str, ...] = (
    "python",
    "pytest",
    "react",
    "react-dom",
    "vite",
    "vitest",
)
"""Recognized dependency names that must be targeted and officially grounded."""


class SkillTarget(ModelBase):
    """A package/runtime version to which generated guidance applies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ecosystem: DependencyEcosystem
    name: str = Field(min_length=1, max_length=200)
    declared_version: str = Field(min_length=1, max_length=500)
    resolved_version: str | None = Field(default=None, max_length=200)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=3)


class SkillSource(ModelBase):
    """Official documentation or release material consulted by the researcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=9, max_length=1000, pattern=r"^https://")
    version_scope: str = Field(min_length=1, max_length=200)
    applies_to: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = Field(
        min_length=1,
        max_length=24,
        description=(
            "Names of the detected dependencies this source grounds. General-practice "
            f"sources may instead use the single value '{GENERIC_SKILL_TARGET}'."
        ),
    )

    @model_validator(mode="after")
    def _require_distinct_applicability(self) -> SkillSource:
        if len(set(self.applies_to)) != len(self.applies_to):
            raise ValueError("skill source applicability names must be distinct")
        if GENERIC_SKILL_TARGET in self.applies_to and len(self.applies_to) > 1:
            raise ValueError(
                f"'{GENERIC_SKILL_TARGET}' applicability cannot be combined with dependency names"
            )
        return self


class SkillGuidance(ModelBase):
    """One bounded advisory section generated for the current repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=300)
    guidance: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...] = Field(
        min_length=1, max_length=12
    )
    avoid: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...] = Field(
        default=(), max_length=8
    )
    validation: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...] = Field(
        default=(), max_length=8
    )


class RepositorySkill(VersionedModel):
    """Researcher-generated simplify and polish guidance for one profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generator_version: Literal[1] = 1
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: UtcDateTime = Field(default_factory=utc_now)
    targets: tuple[SkillTarget, ...] = Field(default=(), max_length=24)
    official_sources: tuple[SkillSource, ...] = Field(default=(), max_length=20)
    practice_sources: tuple[SkillSource, ...] = Field(default=(), max_length=20)
    simplify: SkillGuidance
    polish: SkillGuidance
    uncertainties: tuple[Annotated[str, Field(min_length=1, max_length=1000)], ...] = Field(
        default=(), max_length=10
    )

    @model_validator(mode="after")
    def _require_grounding_or_uncertainty(self) -> RepositorySkill:
        if not self.official_sources and not self.uncertainties:
            raise ValueError("repository skill requires official sources or explicit uncertainty")
        for source in self.official_sources:
            if GENERIC_SKILL_TARGET in source.applies_to:
                raise ValueError(
                    "official sources must name the dependencies they ground, not "
                    f"'{GENERIC_SKILL_TARGET}'"
                )
        for source in self.practice_sources:
            if source.applies_to != (GENERIC_SKILL_TARGET,):
                raise ValueError(
                    "practice sources must apply only to the generic repository target"
                )
            if source.version_scope.casefold() != GENERIC_PRACTICE_VERSION_SCOPE:
                raise ValueError(
                    f"practice sources must use the version scope "
                    f"'{GENERIC_PRACTICE_VERSION_SCOPE}'"
                )
        return self


CONTENT_HASH_PATTERN = r"^[0-9a-f]{64}$"
"""SHA-256 hex digest of a canonically serialized artifact."""

REPOSITORY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""A safe, single filesystem path component identifying one source repository."""


class SkillOverlayMode(StrEnum):
    """How human-owned overlay guidance combines with generated guidance."""

    EXTEND = "extend"
    REPLACE = "replace"


class RepositorySkillOverlay(VersionedModel):
    """Human-owned, repository-scoped guidance that supplements a generated skill.

    Deliberately much narrower than :class:`RepositorySkill`. The overlay
    carries advisory prose only: it may never claim targets, sources,
    resolved versions, a dependency fingerprint, generator provenance or a
    generation timestamp, because those are machine-owned evidence produced
    from the repository itself. ``extra="forbid"`` turns every such field
    into a schema error rather than a silently ignored key, so a human who
    tries to assert provenance gets told, not obeyed.

    It is scoped to a repository, not to a dependency fingerprint: the same
    overlay keeps applying after dependencies change and a different
    generated skill is selected.

    The factory only ever *reads* the overlay file. Nothing in the factory
    writes, normalizes, reformats or deletes it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SkillOverlayMode = SkillOverlayMode.EXTEND
    simplify: SkillGuidance | None = None
    polish: SkillGuidance | None = None

    @model_validator(mode="after")
    def _require_at_least_one_section(self) -> RepositorySkillOverlay:
        if self.simplify is None and self.polish is None:
            raise ValueError("overlay must supply simplify guidance, polish guidance, or both")
        return self


class SkillSelectionSource(StrEnum):
    """Whether a run generated its repository skill or reused a stored one."""

    GENERATED = "generated"
    REUSED = "reused"


class RepositorySkillUse(VersionedModel):
    """Audit record of which skill (and overlay) one run actually used.

    Bounded and fully typed on purpose: it records hashes and provenance,
    never guidance text, so the audit trail stays small and comparable
    across runs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_key: str = Field(pattern=REPOSITORY_KEY_PATTERN)
    dependency_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_at: UtcDateTime = Field(default_factory=utc_now)
    source: SkillSelectionSource
    generated_skill_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    overlay_hash: str | None = Field(default=None, pattern=CONTENT_HASH_PATTERN)
    overlay_mode: SkillOverlayMode | None = None
    overlay_applied: bool = False
    effective_skill_hash: str = Field(pattern=CONTENT_HASH_PATTERN)

    @model_validator(mode="after")
    def _validate_overlay_consistency(self) -> RepositorySkillUse:
        if self.overlay_hash is None:
            if self.overlay_mode is not None or self.overlay_applied:
                raise ValueError("an applied overlay must record its hash")
        elif self.overlay_mode is None:
            raise ValueError("a recorded overlay must record its mode")
        if not self.overlay_applied and self.effective_skill_hash != self.generated_skill_hash:
            raise ValueError(
                "the effective skill must equal the generated skill when no overlay is applied"
            )
        return self


class WorkItem(VersionedModel):
    id: str = Field(min_length=1)
    external_id: str | None = None
    source: Literal["MANUAL", "GITHUB"] = "MANUAL"
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    priority: str | None = None
    complexity: Complexity | None = None
    risk: Risk | None = None
    project_id: str | None = None
    project_task_id: int | None = Field(default=None, ge=1)
    depends_on: list[int] = Field(default_factory=list)
    created_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_project_linkage(self) -> WorkItem:
        if self.project_task_id is not None and self.project_id is None:
            raise ValueError("project_task_id requires project_id")
        if self.depends_on and self.project_task_id is None:
            raise ValueError("depends_on requires project_task_id")
        if self.project_task_id is not None and any(
            dependency >= self.project_task_id for dependency in self.depends_on
        ):
            raise ValueError("work item dependencies must reference earlier project task ids")
        return self


PROJECT_ID_PATTERN = r"^[A-Za-z0-9._-]{1,80}$"
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
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=20000)
    repository_path: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    created_at: UtcDateTime = Field(default_factory=utc_now)


class ProjectTask(ModelBase):
    id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=10000)
    acceptance_criteria: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_PROJECT_TASK_ACCEPTANCE_CRITERIA,
    )
    constraints: tuple[str, ...] = Field(default=(), max_length=30)
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
    summary: str = Field(min_length=1, max_length=2000)
    delivery_approach: str = Field(min_length=1, max_length=2000)
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
    created_at: UtcDateTime = Field(default_factory=utc_now)
    updated_at: UtcDateTime = Field(default_factory=utc_now)
    completed_at: UtcDateTime | None = None
    failure_reason: str | None = None
    warnings: tuple[str, ...] = ()
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


class ModelUsage(ModelBase):
    """Runtime-reported usage for one model within an invocation."""

    model: str = Field(min_length=1)
    requests: int | None = Field(default=None, ge=0)
    premium_request_cost: float | None = Field(default=None, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_nano_aiu: int | None = Field(default=None, ge=0)


class UsageMetrics(ModelBase):
    """Optional aggregate usage reported by the Copilot runtime.

    Values are persisted exactly as reported. They are not converted to AI
    Credits or USD, and missing values remain unknown rather than becoming
    zero.
    """

    current_model: str | None = None
    premium_requests: float | None = Field(default=None, ge=0.0)
    total_premium_request_cost: float | None = Field(default=None, ge=0.0)
    total_user_requests: int | None = Field(default=None, ge=0)
    total_nano_aiu: int | None = Field(default=None, ge=0)
    total_api_duration_ms: int | None = Field(default=None, ge=0)
    session_duration_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    last_call_input_tokens: int | None = Field(default=None, ge=0)
    last_call_output_tokens: int | None = Field(default=None, ge=0)
    model_usage: tuple[ModelUsage, ...] = ()


class InvocationRecord(ModelBase):
    """One persisted agent invocation, independent of retry-budget attempts."""

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: AgentPurpose = AgentPurpose.STANDARD
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    context_tier: ContextTier = ContextTier.DEFAULT
    started_at: UtcDateTime
    completed_at: UtcDateTime
    success: bool
    failure_reason: str | None = None
    attempt_number: int | None = Field(default=None, ge=1)
    budget: AttemptBudget | None = None
    usage: UsageMetrics | None = None

    @model_validator(mode="after")
    def _validate_invocation(self) -> InvocationRecord:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be greater than or equal to started_at")
        if not self.success and not self.failure_reason:
            raise ValueError("failure_reason is required when success is False")
        return self


class ActiveInvocation(ModelBase):
    """One agent invocation currently owned by the workflow controller."""

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: AgentPurpose = AgentPurpose.STANDARD
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    context_tier: ContextTier = ContextTier.DEFAULT
    started_at: UtcDateTime
    attempt_number: int | None = Field(default=None, ge=1)
    budget: AttemptBudget | None = None


class AttemptRecord(ModelBase):
    attempt_number: int = Field(ge=1)
    role: AgentRole
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    context_tier: ContextTier = ContextTier.DEFAULT
    invocation_number: int | None = Field(default=None, ge=1)
    started_at: UtcDateTime
    completed_at: UtcDateTime
    outcome: str = Field(min_length=1)
    failure_reason: str | None = None
    budget: AttemptBudget = AttemptBudget.IMPLEMENTATION
    triggered_by: AttemptTrigger = AttemptTrigger.INITIAL
    reviewed_tree_sha: str | None = None

    @model_validator(mode="after")
    def _validate_timestamps(self) -> AttemptRecord:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be greater than or equal to started_at")
        return self


class RunLease(ModelBase):
    """Ownership marker for a run held by one local process.

    Recorded so a restarted factory (or the scheduler's reconciliation pass)
    can tell an actively owned run from an abandoned one without a database.
    """

    host: str = Field(min_length=1)
    pid: int = Field(ge=1)
    heartbeat_at: UtcDateTime


class ReviewSourceLocation(ModelBase):
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("path must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
            raise ValueError("path must be a normalized repository-relative path")
        return value

    @model_validator(mode="after")
    def _validate_lines(self) -> ReviewSourceLocation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReviewFindingDraft(ModelBase):
    category: ReviewFindingCategory
    message: str = Field(min_length=1, max_length=1000)
    locations: list[ReviewSourceLocation] = Field(min_length=1, max_length=8)


class ReviewFinding(ReviewFindingDraft):
    id: str = Field(min_length=1, max_length=80)
    origin: ReviewFindingOrigin
    first_seen_snapshot: int = Field(ge=1)


class ReviewFindingDisposition(ModelBase):
    finding_id: str = Field(min_length=1, max_length=80)
    status: ReviewDispositionStatus
    rationale: str = Field(min_length=1, max_length=1000)


class ReviewLedger(ModelBase):
    open_findings: list[ReviewFinding] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    last_reviewed_tree_sha: str | None = None
    late_adoption_rounds: int = Field(default=0, ge=0)
    consecutive_replacement_rounds: int = Field(default=0, ge=0)
    path_streaks: dict[str, int] = Field(default_factory=dict)
    unresolved_streaks: dict[str, int] = Field(default_factory=dict)


class FactoryRun(VersionedModel):
    id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    state: WorkflowState
    attempt_records: list[AttemptRecord] = Field(default_factory=list)
    invocation_records: list[InvocationRecord] = Field(default_factory=list)
    scope_replans: int = Field(default=0, ge=0)
    workspace_path: str | None = None
    branch_name: str | None = None
    created_at: UtcDateTime = Field(default_factory=utc_now)
    updated_at: UtcDateTime = Field(default_factory=utc_now)
    last_activity_at: UtcDateTime | None = None
    lease: RunLease | None = None
    active_invocation: ActiveInvocation | None = None
    completed_at: UtcDateTime | None = None
    failure_reason: str | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None
    merge_commit_sha: str | None = None
    delivery_base_branch: str | None = None
    delivery_policy_fingerprint: str | None = None
    reviewed_commit_sha: str | None = None
    delivery_repository: str | None = None
    delivery_host: str | None = None
    reviewed_tree_sha: str | None = None
    base_commit_sha: str | None = None
    pending_commit_sha: str | None = None
    review_ledger: ReviewLedger = Field(default_factory=ReviewLedger)

    @model_validator(mode="after")
    def _validate_completion(self) -> FactoryRun:
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must be greater than or equal to created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        if self.last_activity_at is not None and self.last_activity_at < self.created_at:
            raise ValueError("last_activity_at must be greater than or equal to created_at")
        return self


class TriageResult(VersionedModel):
    factory_eligible: bool
    complexity: Complexity
    risk: Risk
    requirements_quality: str = Field(min_length=1)
    needs_research: bool
    dependencies: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class Specification(VersionedModel):
    problem: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ResearchReport(VersionedModel):
    question: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    implications: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)


class PlanStep(ModelBase):
    id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    likely_files: list[str] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)

    @field_validator("likely_files")
    @classmethod
    def _validate_likely_files(cls, paths: list[str]) -> list[str]:
        normalized_paths: list[str] = []
        for path in paths:
            if (
                not path
                or path != path.strip()
                or "\\" in path
                or any(character in path for character in "*?[]{}")
                or path.startswith("/")
                or path.startswith("./")
                or ":" in path
            ):
                raise ValueError("likely_files entries must be repository-relative paths")
            parts = PurePosixPath(path).parts
            if not parts or any(part in {".", ".."} for part in parts):
                raise ValueError(
                    "likely_files entries must be repository-relative paths without traversal"
                )
            normalized_paths.append("/".join(parts))
        if len(set(normalized_paths)) != len(normalized_paths):
            raise ValueError("likely_files entries must be unique")
        return normalized_paths


class ExpectedScope(ModelBase):
    modules: list[str] = Field(
        min_length=1,
        description=(
            "Repository-relative path prefixes such as 'src', "
            "'src/software_agent_factory', 'tests', or 'pyproject.toml'; "
            "never conceptual labels."
        ),
    )
    estimated_files_min: int = Field(ge=0)
    estimated_files_max: int = Field(ge=0)

    @field_validator("modules")
    @classmethod
    def _validate_modules(cls, modules: list[str]) -> list[str]:
        normalized_modules: list[str] = []
        for module in modules:
            if (
                not module
                or module != module.strip()
                or "\\" in module
                or any(character.isspace() for character in module)
                or any(character in module for character in "*?[]{}")
                or module.startswith("/")
                or ":" in module
            ):
                raise ValueError(
                    "expected_scope.modules entries must be repository-relative "
                    "path prefixes such as 'src', 'tests', or 'pyproject.toml'"
                )
            parts = PurePosixPath(module).parts
            if not parts or any(part in {".", ".."} for part in parts):
                raise ValueError(
                    "expected_scope.modules entries must be repository-relative "
                    "path prefixes without traversal"
                )
            normalized_modules.append("/".join(parts))
        if len(set(normalized_modules)) != len(normalized_modules):
            raise ValueError("expected_scope.modules entries must be unique")
        return normalized_modules

    @model_validator(mode="after")
    def _validate_file_bounds(self) -> ExpectedScope:
        if self.estimated_files_max < self.estimated_files_min:
            raise ValueError(
                "estimated_files_max must be greater than or equal to estimated_files_min"
            )
        return self


class ExecutionPlan(VersionedModel):
    summary: str = Field(min_length=1)
    steps: list[PlanStep] = Field(default_factory=list)
    expected_scope: ExpectedScope
    test_strategy: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ChangeSet(VersionedModel):
    summary: str = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)


class CommandResult(ModelBase):
    command: str = Field(min_length=1)
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(ge=0.0)
    timed_out: bool = False


class VerificationReport(VersionedModel):
    """Deterministic, factory-produced evidence only.

    Every field here is derived from executing repository-configured
    commands (``verification.DeterministicVerifier``); nothing in this model
    is an agent claim. Independent AI tester judgement lives in
    :class:`TestReport`.
    """

    passed: bool
    deterministic_checks: list[CommandResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    coverage_change: float | None = None
    test_findings: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class TestReport(VersionedModel):
    """Independent AI tester output.

    Kept distinct from :class:`VerificationReport` so a model's judgement is
    never mistaken for deterministic evidence ("a model does not approve its
    own work"). ``passed`` is advisory: the workflow controller still relies
    on the deterministic report for gating.
    """

    passed: bool
    findings: list[str] = Field(default_factory=list)
    suggested_tests: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    # Not a pytest test class despite the ``Test`` prefix.
    __test__ = False


class RepairContext(ModelBase):
    """Minimal, bounded context handed to a repair attempt.

    Deliberately small: only why the repair is happening plus the relevant
    excerpt, so historical failure context is never dumped wholesale into a
    prompt (``PLAN.md`` Phase 12). The *current* diff is not carried here --
    it travels as controller-derived Git evidence on ``AgentRequest.diff``.
    """

    trigger: AttemptTrigger
    summary: str = Field(min_length=1)
    failures: list[str] = Field(default_factory=list)
    log_excerpt: str | None = None


class ReviewReport(VersionedModel):
    approved: bool
    findings: list[str] = Field(default_factory=list)
    scope_concerns: list[str] = Field(default_factory=list)
    security_concerns: list[str] = Field(default_factory=list)
    compatibility_concerns: list[str] = Field(default_factory=list)
    suggested_changes: list[str] = Field(default_factory=list)
    blocking_findings: list[ReviewFindingDraft] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    prior_finding_dispositions: list[ReviewFindingDisposition] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    repair_regressions: list[ReviewFindingDraft] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )


class ReviewImpasse(VersionedModel):
    snapshot: int = Field(ge=1)
    reason: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class CICheckEvidence(ModelBase):
    """One normalized CI check outcome.

    Deliberately expressed with plain strings rather than the enums declared
    in :mod:`software_agent_factory.github`: this module is the domain layer
    and must not depend on the ``gh`` adapter. The controller normalizes a
    ``github.CIStatus`` into this shape before persisting it.
    """

    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    description: str = ""
    details_url: str = ""
    failure_category: str | None = None
    log_excerpt: str = ""


class CIReport(VersionedModel):
    """Persisted, normalized CI evidence for one observation cycle.

    Controller-produced deterministic evidence (``PLAN.md`` Phase 11): it
    records what GitHub Actions reported, never a model's opinion of it.
    """

    overall: str = Field(min_length=1)
    checks: list[CICheckEvidence] = Field(default_factory=list)
    observed_at: UtcDateTime = Field(default_factory=utc_now)
    repair_attempts_used: int = Field(ge=0, default=0)
    timed_out: bool = False

    @property
    def failed_checks(self) -> list[CICheckEvidence]:
        return [check for check in self.checks if check.status == "FAIL"]
