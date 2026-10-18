"""Shared model base classes, validators and core enums."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_normalize_utc)]


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


#: Agent-authored prose. Whitespace-only text is a structural error, so it fails
#: model validation and takes the ordinary retry. The original text is kept.
NonBlankStr = Annotated[str, AfterValidator(_reject_blank)]


def without_blank_entries(data: object, fields: tuple[str, ...]) -> object:
    """Drop blank entries from the named human-input lists before validation."""
    if not isinstance(data, dict):
        return data
    cleaned = dict(data)
    for field in fields:
        values = cleaned.get(field)
        if isinstance(values, list | tuple):
            cleaned[field] = [
                value for value in values if not isinstance(value, str) or value.strip()
            ]
    return cleaned


MAX_OPEN_REVIEW_FINDINGS = 24
#: The most findings the run guidance names, and the largest count it shows for one category.
MAX_GUIDANCE_FINDINGS = 12
MAX_PERFORMANCE_METRICS = 250
MAX_PERFORMANCE_NAME_LENGTH = 100
MAX_PERFORMANCE_STAGE_LENGTH = 50
MAX_PERFORMANCE_OPERATION_LENGTH = 50
MAX_PERFORMANCE_UNIT_LENGTH = 20
MAX_PERFORMANCE_MAP_ENTRIES = 100


class WorkflowState(StrEnum):
    CREATED = "CREATED"
    TRIAGING = "TRIAGING"
    #: Kept so old run records load. New runs go from TRIAGING to PLANNING (ADR-035).
    REFINING = "REFINING"
    #: Kept so old run records load. New runs go from TRIAGING to PLANNING (ADR-035).
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


class ExecutionRoute(StrEnum):
    SINGLE = "SINGLE"
    CRITIQUE = "CRITIQUE"
    FULL = "FULL"
    MANUAL_TRIAGE = "MANUAL_TRIAGE"
    FULL_REVIEW = "FULL_REVIEW"


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
    #: Kept so old run records load. The planner writes the specification (ADR-035).
    REFINER = "REFINER"
    #: Kept so old run records load. Nothing invokes it (ADR-035).
    RESEARCHER = "RESEARCHER"
    PLANNER = "PLANNER"
    IMPLEMENTER = "IMPLEMENTER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"


class AgentPurpose(StrEnum):
    """The typed output contract for an agent invocation."""

    STANDARD = "STANDARD"
    DECOMPOSE_PROJECT = "DECOMPOSE_PROJECT"
    #: Kept so old run records load. Nothing creates it.
    GENERATE_REPOSITORY_SKILL = "GENERATE_REPOSITORY_SKILL"
    #: Kept so old run records load. Nothing creates it.
    CORRECT_CHANGE_SET = "CORRECT_CHANGE_SET"


class ContextTier(StrEnum):
    DEFAULT = "default"
    LONG_CONTEXT = "long_context"


class RuntimeName(StrEnum):
    """A real agent runtime a role can name (ADR-045)."""

    COPILOT = "copilot"
    PI = "pi"
    CLAUDE_CODE = "claude-code"


#: The reasoning values ``claude --effort`` accepts (ADR-045).
CLAUDE_CODE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


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
    #: ``factory escalate``: a person asked for the work again one route higher.
    OPERATOR = "OPERATOR"


class ReviewFindingCategory(StrEnum):
    CORRECTNESS = "CORRECTNESS"
    SCOPE = "SCOPE"
    SECURITY = "SECURITY"
    COMPATIBILITY = "COMPATIBILITY"
    SIMPLICITY = "SIMPLICITY"


class ReviewFindingOrigin(StrEnum):
    INITIAL = "INITIAL"
    LATE_ADOPTED = "LATE_ADOPTED"
    REPAIR_REGRESSION_DIRECT = "REPAIR_REGRESSION_DIRECT"
    REPAIR_REGRESSION_INDIRECT = "REPAIR_REGRESSION_INDIRECT"


class ReviewDispositionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    WITHDRAWN = "WITHDRAWN"


class ReviewAcceptanceReason(StrEnum):
    REVIEW_ROUND_LIMIT = "REVIEW_ROUND_LIMIT"
    REVIEW_IMPASSE = "REVIEW_IMPASSE"
    ATTEMPT_BUDGET_LIMIT = "ATTEMPT_BUDGET_LIMIT"
    CARRIED_FORWARD = "CARRIED_FORWARD"


class ReviewImpasseKind(StrEnum):
    UNKNOWN = "UNKNOWN"
    TOO_MANY_FINDINGS = "TOO_MANY_FINDINGS"
    REPEATED_PATH = "REPEATED_PATH"
    REPEATED_FINDING = "REPEATED_FINDING"
    REPLACEMENT_LOOP = "REPLACEMENT_LOOP"
    ATTEMPT_BUDGET_LIMIT = "ATTEMPT_BUDGET_LIMIT"
    REVIEW_ROUND_LIMIT = "REVIEW_ROUND_LIMIT"


class ModelBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_claude_code_effort(runtime: RuntimeName | None, reasoning: str) -> None:
    """Reject a ``claude-code`` reasoning value that ``--effort`` does not accept."""
    if runtime is RuntimeName.CLAUDE_CODE and reasoning not in CLAUDE_CODE_EFFORT_LEVELS:
        raise ValueError(
            f"runtime claude-code accepts reasoning {', '.join(CLAUDE_CODE_EFFORT_LEVELS)}, "
            f"not {reasoning!r}"
        )


def check_pi_provider(runtime: RuntimeName | None, provider: str | None) -> None:
    """Reject a ``provider`` on a runtime other than pi, which would ignore it."""
    if provider is not None and runtime not in (None, RuntimeName.PI):
        raise ValueError(f"provider is a pi setting; runtime {runtime} does not use it")


class RuntimeFallback(ModelBase):
    """The runtime and model that serve a call its first runtime cannot serve (ADR-045).

    The router uses it once, when the first runtime reports itself unavailable.
    It has no nested fallback.
    """

    runtime: RuntimeName
    model: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    context_tier: ContextTier = ContextTier.DEFAULT
    provider: str | None = Field(default=None, min_length=1)
    """The pi provider; ``None`` uses ``pi.provider``."""

    @model_validator(mode="after")
    def _validate_runtime_settings(self) -> Self:
        check_claude_code_effort(self.runtime, self.reasoning)
        check_pi_provider(self.runtime, self.provider)
        return self


class VersionedModel(ModelBase):
    schema_version: Literal[1] = 1
