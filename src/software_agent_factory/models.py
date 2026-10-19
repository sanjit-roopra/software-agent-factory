"""Run, route, triage, specification and plan models.

The other model modules are re-exported here, so callers can import any model
from ``software_agent_factory.models``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from software_agent_factory.escalation_models import (
    CI_REPAIR_BUDGET_HALT_REASON,
    CI_TIMEOUT_HALT_REASON,
    CONTEXT_FINGERPRINT_PATTERN,
    DASHBOARD_USER_LOGIN,
    EPISODE_ID_PATTERN,
    HALT_REASON_COPY,
    IMPLEMENTATION_BUDGET_HALT_REASON,
    MAX_CI_RETRY_CHECKS,
    MAX_PLAN_DECISIONS,
    MAX_SCOPE_APPROVAL_PATHS,
    MAX_STEER_NOTE_CHARS,
    PUBLISH_FAILED_HALT_REASON,
    REPLY_CURSOR_CLOSED,
    STALLED_HALT_REASON,
    STOPPED_HALT_REASON,
    UNRESOLVED_DECISIONS_HALT_REASON,
    UNRESOLVED_DECISIONS_REPLACE_ACTION,
    AcceptedReplyReceipt,
    ActiveInvocationStatus,
    AttemptGrantContext,
    CIRepairRetryContext,
    DashboardRequestStaleReason,
    DashboardResumeRequest,
    DeliveryRetryContext,
    EscalationRecord,
    EscalationStatus,
    EscalationTargetType,
    FindingsAcceptanceContext,
    HaltReasonCode,
    OperatorEscalation,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
    ReplySource,
    ResumeClassification,
    ResumeRefusal,
    ReviewAcceptance,
    ReviewFinding,
    ReviewFindingDisposition,
    ReviewFindingDraft,
    ReviewLedger,
    ReviewSourceLocation,
    RiskApprovalContext,
    RiskRationale,
    RunSteerNote,
    RunStopRequest,
    ScopeApprovalContext,
    unresolved_decisions_count,
    unresolved_decisions_summary,
)
from software_agent_factory.model_base import (
    CLAUDE_CODE_EFFORT_LEVELS,
    MAX_GUIDANCE_FINDINGS,
    MAX_OPEN_REVIEW_FINDINGS,
    MAX_PERFORMANCE_MAP_ENTRIES,
    MAX_PERFORMANCE_METRICS,
    MAX_PERFORMANCE_NAME_LENGTH,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    Complexity,
    ContextTier,
    ExecutionRoute,
    ModelBase,
    NonBlankStr,
    ReviewAcceptanceReason,
    ReviewDispositionStatus,
    ReviewFindingCategory,
    ReviewFindingOrigin,
    ReviewImpasseKind,
    Risk,
    RuntimeFallback,
    RuntimeName,
    UtcDateTime,
    VersionedModel,
    WorkflowState,
    check_claude_code_effort,
    check_pi_provider,
    utc_now,
)
from software_agent_factory.project_models import (
    PROJECT_ID_PATTERN,
    RUN_LIST_SORTS,
    RUN_LIST_STATES,
    ProjectBrief,
    ProjectExecution,
    ProjectPlan,
    ProjectState,
    ProjectTask,
    ProjectTaskExecution,
    ProjectTaskState,
    RunListFilter,
)
from software_agent_factory.report_models import (
    ChangeSet,
    CICheckEvidence,
    CIReport,
    CommandResult,
    RepairContext,
    ReviewImpasse,
    ReviewReport,
    TestReport,
    VerificationReport,
)
from software_agent_factory.repository_models import (
    MAX_COMMAND_NOTES,
    MAX_COMMAND_TEXT_LENGTH,
    DependencyEcosystem,
    MutationReport,
    MutationStatus,
    RejectedCommand,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    RepositoryDependency,
    RepositoryPackageManager,
    RepositoryProfile,
    RepositoryTechnology,
    RepositoryTestTool,
    SetupState,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSetupPlan,
    ToolchainSlot,
    ToolchainSlotBinding,
    WorkItem,
)
from software_agent_factory.usage_models import (
    COST_UNIT_FIELDS,
    NANO_AIU_PER_USD,
    TOKEN_CLASS_FIELDS,
    TOTAL_TOKEN_FIELDS,
    ActiveInvocation,
    AttemptRecord,
    InvocationRecord,
    ModelUsage,
    PerformanceMetric,
    PerformanceRecord,
    RunLaunch,
    RunLease,
    UsageMetrics,
)

__all__ = [
    "AcceptedReplyReceipt",
    "ActiveInvocation",
    "ActiveInvocationStatus",
    "AgentPurpose",
    "AgentRole",
    "AttemptBudget",
    "AttemptRecord",
    "AttemptTrigger",
    "CICheckEvidence",
    "CIReport",
    "CLAUDE_CODE_EFFORT_LEVELS",
    "CONTEXT_FINGERPRINT_PATTERN",
    "COST_UNIT_FIELDS",
    "NANO_AIU_PER_USD",
    "ChangeSet",
    "CommandResult",
    "Complexity",
    "ContextTier",
    "DASHBOARD_USER_LOGIN",
    "DashboardRequestStaleReason",
    "DashboardResumeRequest",
    "DeliveryRetryContext",
    "AttemptGrantContext",
    "FindingsAcceptanceContext",
    "ScopeApprovalContext",
    "CIRepairRetryContext",
    "CI_REPAIR_BUDGET_HALT_REASON",
    "CI_TIMEOUT_HALT_REASON",
    "MAX_CI_RETRY_CHECKS",
    "IMPLEMENTATION_BUDGET_HALT_REASON",
    "DependencyEcosystem",
    "EPISODE_ID_PATTERN",
    "EscalationRecord",
    "EscalationStatus",
    "EscalationTargetType",
    "ExecutionPlan",
    "ExecutionRoute",
    "ExpectedScope",
    "FactoryRun",
    "HALT_REASON_COPY",
    "HaltReasonCode",
    "InvocationRecord",
    "MAX_COMMAND_NOTES",
    "MAX_COMMAND_TEXT_LENGTH",
    "MAX_GUIDANCE_FINDINGS",
    "MAX_OPEN_REVIEW_FINDINGS",
    "MAX_PERFORMANCE_MAP_ENTRIES",
    "MAX_PERFORMANCE_METRICS",
    "MAX_PERFORMANCE_NAME_LENGTH",
    "MAX_PLAN_DECISIONS",
    "MAX_SCOPE_APPROVAL_PATHS",
    "MAX_STEER_NOTE_CHARS",
    "ModelBase",
    "ModelUsage",
    "MutationReport",
    "MutationStatus",
    "OperatorEscalation",
    "PROJECT_ID_PATTERN",
    "PUBLISH_FAILED_HALT_REASON",
    "PerformanceMetric",
    "PerformanceRecord",
    "PlanDecisionAnswer",
    "PlanDecisionAnswers",
    "PlanDecisionContext",
    "PlanStep",
    "PlanningResult",
    "ProjectBrief",
    "ProjectExecution",
    "ProjectPlan",
    "ProjectState",
    "ProjectTask",
    "ProjectTaskExecution",
    "ProjectTaskState",
    "REPLY_CURSOR_CLOSED",
    "RUN_LIST_SORTS",
    "RUN_LIST_STATES",
    "RejectedCommand",
    "RepairContext",
    "ReplySource",
    "RepositoryCommandsPlan",
    "RepositoryCommandsSource",
    "RepositoryDependency",
    "RepositoryPackageManager",
    "RepositoryProfile",
    "RepositoryTechnology",
    "RepositoryTestTool",
    "ResumeClassification",
    "ResumeRefusal",
    "ReviewAcceptance",
    "ReviewAcceptanceReason",
    "ReviewDispositionStatus",
    "ReviewFinding",
    "ReviewFindingCategory",
    "ReviewFindingDisposition",
    "ReviewFindingDraft",
    "ReviewFindingOrigin",
    "ReviewImpasse",
    "ReviewImpasseKind",
    "ReviewLedger",
    "ReviewReport",
    "ReviewSourceLocation",
    "Risk",
    "RiskApprovalContext",
    "RiskRationale",
    "RouteAdjustment",
    "RouteDecision",
    "RouteOption",
    "RunLaunch",
    "RunLease",
    "RunListFilter",
    "RunSteerNote",
    "RunStopRequest",
    "RuntimeFallback",
    "RuntimeName",
    "STALLED_HALT_REASON",
    "STOPPED_HALT_REASON",
    "SetupState",
    "Specification",
    "TOKEN_CLASS_FIELDS",
    "TOTAL_TOKEN_FIELDS",
    "TestReport",
    "ToolchainInventory",
    "ToolchainLane",
    "ToolchainProvider",
    "ToolchainSetupPlan",
    "ToolchainSlot",
    "ToolchainSlotBinding",
    "TriageResult",
    "UNRESOLVED_DECISIONS_HALT_REASON",
    "UNRESOLVED_DECISIONS_REPLACE_ACTION",
    "UsageMetrics",
    "UtcDateTime",
    "VerificationReport",
    "VersionedModel",
    "WorkItem",
    "WorkflowState",
    "check_claude_code_effort",
    "check_pi_provider",
    "unresolved_decisions_count",
    "unresolved_decisions_summary",
    "utc_now",
]


class RouteAdjustment(ModelBase):
    from_route: ExecutionRoute
    to_route: ExecutionRoute
    reason: str = Field(min_length=1, max_length=500)
    triggered_at: UtcDateTime = Field(default_factory=utc_now)


class RouteOption(ModelBase):
    id: str = Field(min_length=1, max_length=64)
    route: ExecutionRoute
    complexity: Complexity | None = None
    risk: Risk | None = None
    model_profile: str | None = None
    description: str = Field(default="", max_length=500)

    @field_validator("model_profile", mode="after")
    @classmethod
    def _normalize_default_profile(cls, v: str | None) -> str | None:
        if v == "default":
            return None
        return v

    @model_validator(mode="after")
    def _validate_option_contract(self) -> Self:
        if self.route is ExecutionRoute.FULL_REVIEW:
            raise ValueError(
                "FULL_REVIEW is a controller-only execution route and cannot be configured"
            )
        if self.route is ExecutionRoute.MANUAL_TRIAGE:
            if self.complexity is not None:
                raise ValueError("complexity must be absent for MANUAL_TRIAGE route options")
            if self.risk is not None:
                raise ValueError("risk must be absent for MANUAL_TRIAGE route options")
        elif self.route in {ExecutionRoute.SINGLE, ExecutionRoute.CRITIQUE}:
            if self.complexity is None:
                raise ValueError(f"complexity is required for {self.route.value} route options")
            if self.risk is None:
                raise ValueError(f"risk is required for {self.route.value} route options")
        elif self.route is ExecutionRoute.FULL:
            if self.complexity is None:
                raise ValueError(f"complexity is required for {self.route.value} route options")
        return self


class RouteDecision(VersionedModel):
    """Auditable route decision made for one run."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(min_length=1)
    offered_options: list[RouteOption]
    selected_option: str = Field(min_length=1)
    initial_route: ExecutionRoute
    effective_route: ExecutionRoute
    selected_worker_complexity: Complexity | None = None
    selected_risk: Risk | None = None
    selected_model_profile: str | None = None
    source: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    probabilities: dict[str, float] | None = None
    model_id: str | None = None
    protocol_version: str = "1.0"
    latency_ms: float | None = Field(default=None, ge=0.0)
    usage: dict[str, Any] | None = None
    request_hash: str = Field(min_length=1, max_length=128)
    fallback_reason: str | None = None
    abstention_reason: str | None = None
    adjustments: list[RouteAdjustment] = Field(default_factory=list)
    decided_at: UtcDateTime = Field(default_factory=utc_now)

    @field_validator("selected_model_profile", mode="after")
    @classmethod
    def _normalize_default_profile(cls, v: str | None) -> str | None:
        if v == "default":
            return None
        return v

    def record_adjustment(self, to_route: ExecutionRoute, reason: str) -> None:
        self.adjustments.append(
            RouteAdjustment(
                from_route=self.effective_route,
                to_route=to_route,
                reason=reason,
            )
        )
        self.effective_route = to_route


_REMOVED_RUN_KEYS = frozenset(
    {
        "requested_performance_mode",
        "effective_performance_mode",
        "performance_model_profile",
        "performance_fallback_reason",
    }
)


#: The most state moves a run keeps; the oldest go first (ADR-067).
MAX_STATE_TRANSITIONS = 200
#: The longest reason a state move keeps, in characters (ADR-067).
MAX_TRANSITION_REASON_CHARS = 500


class StateTransition(ModelBase):
    """One move of a run from one state to the next, and why when it halted or failed."""

    from_state: WorkflowState
    to_state: WorkflowState
    at: UtcDateTime
    reason: str | None = Field(default=None, max_length=MAX_TRANSITION_REASON_CHARS)


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
    state_started_at: UtcDateTime | None = None
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
    risk_assessment_enabled: bool = True
    #: ``factory.unattended`` when the run started (ADR-039).
    unattended: bool = False
    #: Why an unattended run published or kept open a pull request that skipped
    #: a gate. A pull request with reasons gets the ``factory:needs-look``
    #: label and is never merged (ADR-040).
    needs_look: list[str] = Field(default_factory=list)
    #: How often a factory restart interrupted this unattended run before delivery (ADR-043).
    restart_recoveries: int = Field(default=0, ge=0)
    #: Attempt records before this index do not count against attempt budgets.
    #: An operator escalation moves it, so it starts fresh budgets (ADR-055).
    budget_start: int = Field(default=0, ge=0)
    #: Attempts a person granted per budget after it ran out (ADR-058). An escalation clears it.
    attempt_grants: dict[AttemptBudget, int] = Field(default_factory=dict)
    #: The repair the next attempt would have had when the budget ran out (ADR-058).
    exhausted_repair: RepairContext | None = None
    #: Changed paths a person approved beyond the plan's scope (ADR-060). The scope check
    #: ignores them for this run.
    approved_scope_paths: list[str] = Field(default_factory=list)
    #: The repository the run started from and the command settings (ADR-055).
    #: ``None`` on runs recorded before ``factory escalate`` read them.
    source_repo: str | None = None
    launch: RunLaunch | None = None
    initial_route: ExecutionRoute | None = None
    effective_route: ExecutionRoute | None = None
    route_decision: RouteDecision | None = None
    review_ledger: ReviewLedger = Field(default_factory=ReviewLedger)
    review_acceptance: ReviewAcceptance | None = None
    performance: PerformanceRecord = Field(default_factory=PerformanceRecord)
    escalation: EscalationRecord | None = None
    #: The last :data:`MAX_STATE_TRANSITIONS` state moves, oldest first (ADR-067).
    #: Runs recorded before ADR-067 have none.
    transitions: list[StateTransition] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_performance_mode(cls, data: object) -> object:
        """Load run records written before ADR-036 removed the fast performance mode."""
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if key not in _REMOVED_RUN_KEYS}
        return data

    def attempts_used(self, budget: AttemptBudget) -> int:
        """Implementer attempts spent from ``budget`` since ``budget_start`` (ADR-003, ADR-055)."""
        return sum(
            1
            for attempt in self.attempt_records[self.budget_start :]
            if attempt.budget is budget and attempt.role is AgentRole.IMPLEMENTER
        )

    @model_validator(mode="after")
    def _validate_completion(self) -> FactoryRun:
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must be greater than or equal to created_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be greater than or equal to created_at")
        if self.state_started_at is not None and self.state_started_at < self.created_at:
            raise ValueError("state_started_at must be greater than or equal to created_at")
        if self.last_activity_at is not None and self.last_activity_at < self.created_at:
            raise ValueError("last_activity_at must be greater than or equal to created_at")
        return self


class TriageResult(VersionedModel):
    factory_eligible: bool
    complexity: Complexity
    risk: Risk
    #: No longer used (ADR-035). Kept with a default so old triage files load.
    needs_research: bool = False
    dependencies: list[NonBlankStr] = Field(default_factory=list)
    unknowns: list[NonBlankStr] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    risk_rationale: RiskRationale | None = None
    provenance: Literal["AGENT", "SYNTHESIZED"] = "AGENT"

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_requirements_quality(cls, data: object) -> object:
        """Load triage files written before the factory removed ``requirements_quality``."""
        if isinstance(data, dict) and "requirements_quality" in data:
            return {key: value for key, value in data.items() if key != "requirements_quality"}
        return data

    def lacks_required_risk_rationale(self) -> bool:
        """Whether risk is R2 or R3 with no rationale.

        Not a model validator: the controller enforces it only while
        ``risk_assessment.enabled`` is true, and a triage stored with
        assessment off must still load.
        """
        return self.risk in {Risk.R2, Risk.R3} and self.risk_rationale is None


class Specification(VersionedModel):
    problem: NonBlankStr = Field(min_length=1)
    acceptance_criteria: list[NonBlankStr] = Field(default_factory=list)
    constraints: list[NonBlankStr] = Field(default_factory=list)
    assumptions: list[NonBlankStr] = Field(default_factory=list)
    unknowns: list[NonBlankStr] = Field(default_factory=list)
    dependencies: list[NonBlankStr] = Field(default_factory=list)
    risk_flags: list[NonBlankStr] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: Literal["AGENT", "SYNTHESIZED"] = "AGENT"


class PlanStep(ModelBase):
    id: str = Field(min_length=1)
    goal: NonBlankStr = Field(min_length=1)
    likely_files: list[str] = Field(default_factory=list)
    validation: list[NonBlankStr] = Field(default_factory=list)

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
    summary: NonBlankStr = Field(min_length=1)
    steps: list[PlanStep] = Field(default_factory=list)
    expected_scope: ExpectedScope
    test_strategy: list[NonBlankStr] = Field(default_factory=list)
    risks: list[NonBlankStr] = Field(default_factory=list)
    unresolved_decisions: list[NonBlankStr] = Field(default_factory=list)
    provenance: Literal["AGENT", "SYNTHESIZED"] = "AGENT"

    @property
    def is_ready(self) -> bool:
        return not self.unresolved_decisions

    @property
    def ready(self) -> bool:
        return self.is_ready


class PlanningResult(ModelBase):
    """The planner's one output: the specification and the plan (ADR-035).

    The controller persists the two parts as separate artifacts.
    """

    specification: Specification
    execution_plan: ExecutionPlan
