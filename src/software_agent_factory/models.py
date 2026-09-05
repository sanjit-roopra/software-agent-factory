from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_normalize_utc)]


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
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


class SkillId(StrEnum):
    PLAN_QUALITY = "plan-quality"
    SIMPLIFICATION = "simplification"
    PYTHON_QUALITY = "python-quality"
    VITE_QUALITY = "vite-quality"
    REACT_QUALITY = "react-quality"
    REACT_REACTIVITY = "react-reactivity"
    REACT_TESTING = "react-testing"
    TESTING_QUALITY = "testing-quality"


class SelectedSkill(ModelBase):
    """One immutable, factory-owned skill selected for a repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SkillId
    version: int = Field(ge=1)
    summary: str = Field(min_length=1)
    roles: tuple[AgentRole, ...]
    guidance: tuple[str, ...]
    evidence: tuple[str, ...] = Field(default=(), max_length=5)


class RepositoryProfile(VersionedModel):
    """Deterministic, read-only repository facts and selected skills."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector_version: Literal[1] = 1
    catalog_version: Literal[1] = 1
    markers: tuple[str, ...] = ()
    technologies: tuple[RepositoryTechnology, ...] = ()
    test_tools: tuple[RepositoryTestTool, ...] = ()
    package_managers: tuple[RepositoryPackageManager, ...] = ()
    selected_skills: tuple[SelectedSkill, ...] = ()
    warnings: tuple[str, ...] = ()


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
    created_at: UtcDateTime = Field(default_factory=utc_now)


class AttemptRecord(ModelBase):
    attempt_number: int = Field(ge=1)
    role: AgentRole
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    started_at: UtcDateTime
    completed_at: UtcDateTime
    outcome: str = Field(min_length=1)
    failure_reason: str | None = None
    budget: AttemptBudget = AttemptBudget.IMPLEMENTATION
    triggered_by: AttemptTrigger = AttemptTrigger.INITIAL

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


class FactoryRun(VersionedModel):
    id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    state: WorkflowState
    attempt_records: list[AttemptRecord] = Field(default_factory=list)
    workspace_path: str | None = None
    branch_name: str | None = None
    created_at: UtcDateTime = Field(default_factory=utc_now)
    updated_at: UtcDateTime = Field(default_factory=utc_now)
    last_activity_at: UtcDateTime | None = None
    lease: RunLease | None = None
    completed_at: UtcDateTime | None = None
    failure_reason: str | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None

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


class ExpectedScope(ModelBase):
    modules: list[str] = Field(default_factory=list)
    estimated_files_min: int = Field(ge=0)
    estimated_files_max: int = Field(ge=0)

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
