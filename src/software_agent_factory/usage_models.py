"""Usage, performance, invocation and attempt models."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from software_agent_factory.model_base import (
    MAX_PERFORMANCE_MAP_ENTRIES,
    MAX_PERFORMANCE_METRICS,
    MAX_PERFORMANCE_NAME_LENGTH,
    MAX_PERFORMANCE_OPERATION_LENGTH,
    MAX_PERFORMANCE_STAGE_LENGTH,
    MAX_PERFORMANCE_UNIT_LENGTH,
    AgentPurpose,
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    ContextTier,
    ModelBase,
    RuntimeName,
    UtcDateTime,
)


class ModelUsage(ModelBase):
    """Runtime-reported usage for one model within an invocation."""

    model: str = Field(min_length=1)
    runtime: RuntimeName | None = None
    """The runtime that served the call. ``None`` for the fake runtime and for
    usage persisted before ADR-045 slice 2."""
    requests: int | None = Field(default=None, ge=0)
    premium_request_cost: float | None = Field(default=None, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    total_nano_aiu: int | None = Field(default=None, ge=0)
    list_price_estimate_usd: float | None = Field(default=None, ge=0.0)
    """List-price USD estimate the runtime computed from its own price
    catalog (pi), never what the provider actually billed. ``None`` when the
    runtime did not report one."""


class UsageMetrics(ModelBase):
    """Optional aggregate usage reported by the Copilot, pi or Claude Code runtime.

    Values are persisted exactly as reported by whichever runtime produced
    them, never coerced and never zero-filled for a missing value. Copilot's
    ``total_nano_aiu``/premium-request units are Copilot-specific and are
    not converted to AI Credits or USD. ``list_price_estimate_usd`` is the
    list-price estimate in USD that pi or Claude Code computed -- never what a
    provider actually billed.
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
    list_price_estimate_usd: float | None = Field(default=None, ge=0.0)
    """List-price USD estimate the runtime computed from its own price
    catalog (pi), never what the provider actually billed. ``None`` when the
    runtime did not report one (including every run persisted before this
    field existed)."""


#: Token classes and cost units a call reports, named once here. The dashboard
#: totals, the key figures and the page keys all read these. An unreported value
#: is ``None``, never ``0``. ``usage_value_usd`` is derived from ``total_nano_aiu``,
#: so it is not a ``UsageMetrics`` field.
NANO_AIU_PER_USD = 100_000_000_000
TOKEN_CLASS_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
COST_UNIT_FIELDS: tuple[str, ...] = (
    "total_premium_request_cost",
    "usage_value_usd",
    "list_price_estimate_usd",
)

#: The token classes one call's total adds up. Reasoning tokens stay out: the
#: runtime already counts them inside the output tokens (see "Usage mapping" in
#: docs/specs/pi-agent-runtime.md). Cache classes stay in: they are separate from
#: input. The Copilot usage fixture in tests/test_copilot_runtime.py reports 1195
#: input tokens beside 47104 cache-read tokens, so cache read cannot be part of input.
TOTAL_TOKEN_FIELDS: tuple[str, ...] = tuple(
    field for field in TOKEN_CLASS_FIELDS if field != "reasoning_tokens"
)


class PerformanceMetric(ModelBase):
    """One bounded typed performance metric observation."""

    name: str = Field(min_length=1, max_length=MAX_PERFORMANCE_NAME_LENGTH)
    value: float = Field(ge=0.0)
    unit: str = Field(default="ms", max_length=MAX_PERFORMANCE_UNIT_LENGTH)
    stage: str | None = Field(default=None, max_length=MAX_PERFORMANCE_STAGE_LENGTH)
    operation: str | None = Field(default=None, max_length=MAX_PERFORMANCE_OPERATION_LENGTH)


class PerformanceRecord(ModelBase):
    """Cohesive bounded telemetry record for operations, timings, sizes, and rework."""

    durations_ms: dict[str, float] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    prompt_chars: int | None = Field(default=None, ge=0)
    response_chars: int | None = Field(default=None, ge=0)
    process_boot_ms: float | None = Field(default=None, ge=0.0)
    first_event_ms: float | None = Field(default=None, ge=0.0)
    metrics: list[PerformanceMetric] = Field(
        default_factory=list,
        max_length=MAX_PERFORMANCE_METRICS,
    )

    @field_validator("durations_ms")
    @classmethod
    def _validate_durations(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > MAX_PERFORMANCE_MAP_ENTRIES:
            raise ValueError(f"durations_ms cannot exceed {MAX_PERFORMANCE_MAP_ENTRIES} entries")
        for k, v in value.items():
            if not k or len(k) > MAX_PERFORMANCE_NAME_LENGTH:
                raise ValueError("duration key must be between 1 and 100 characters")
            if v < 0:
                raise ValueError("duration value must be non-negative")
        return value

    @field_validator("counters")
    @classmethod
    def _validate_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if len(value) > MAX_PERFORMANCE_MAP_ENTRIES:
            raise ValueError(f"counters cannot exceed {MAX_PERFORMANCE_MAP_ENTRIES} entries")
        for k, v in value.items():
            if not k or len(k) > MAX_PERFORMANCE_NAME_LENGTH:
                raise ValueError("counter key must be between 1 and 100 characters")
            if v < 0:
                raise ValueError("counter value must be non-negative")
        return value

    def record_duration(
        self,
        name: str,
        duration_ms: float,
        *,
        stage: str | None = None,
        operation: str | None = None,
        accumulate: bool = False,
    ) -> None:
        clean_name = str(name)[:MAX_PERFORMANCE_NAME_LENGTH]
        duration = max(0.0, float(duration_ms))
        if accumulate:
            val = self.durations_ms.get(clean_name, 0.0) + duration
        else:
            val = duration
        if len(self.durations_ms) < MAX_PERFORMANCE_MAP_ENTRIES or clean_name in self.durations_ms:
            self.durations_ms[clean_name] = val
        if len(self.metrics) < MAX_PERFORMANCE_METRICS:
            self.metrics.append(
                PerformanceMetric(
                    name=clean_name,
                    value=duration,
                    unit="ms",
                    stage=stage,
                    operation=operation,
                )
            )

    def record_counter(
        self,
        name: str,
        delta: int = 1,
        *,
        stage: str | None = None,
        operation: str | None = None,
    ) -> None:
        if delta < 0:
            raise ValueError("delta must be non-negative")
        clean_name = str(name)[:MAX_PERFORMANCE_NAME_LENGTH]
        if len(self.counters) < MAX_PERFORMANCE_MAP_ENTRIES or clean_name in self.counters:
            self.counters[clean_name] = self.counters.get(clean_name, 0) + delta
        if len(self.metrics) < MAX_PERFORMANCE_METRICS:
            self.metrics.append(
                PerformanceMetric(
                    name=clean_name,
                    value=float(delta),
                    unit="count",
                    stage=stage,
                    operation=operation,
                )
            )

    def add_metric(
        self,
        name: str,
        value: float,
        unit: str = "ms",
        *,
        stage: str | None = None,
        operation: str | None = None,
    ) -> None:
        clean_name = str(name)[:MAX_PERFORMANCE_NAME_LENGTH]
        if len(self.metrics) < MAX_PERFORMANCE_METRICS:
            self.metrics.append(
                PerformanceMetric(
                    name=clean_name,
                    value=max(0.0, float(value)),
                    unit=unit,
                    stage=stage,
                    operation=operation,
                )
            )

    def record_size(
        self,
        *,
        prompt_chars: int | None = None,
        response_chars: int | None = None,
    ) -> None:
        if prompt_chars is not None:
            self.prompt_chars = max(0, (self.prompt_chars or 0) + prompt_chars)
        if response_chars is not None:
            self.response_chars = max(0, (self.response_chars or 0) + response_chars)


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
    performance: PerformanceRecord | None = None
    fallback_reason: str | None = None
    """Why the configured runtime could not serve the call, when its fallback
    did (ADR-045). ``model`` stays the configured model; ``usage`` names the
    runtime and model that served the call."""
    transcript_path: str | None = None
    """Run-relative path of the call's redacted event stream
    (``calls/<n>.jsonl``), when the runtime returned one."""
    session_ref: str | None = None
    """Data-dir-relative path of the pi session file the call ran in, when it kept one."""

    @model_validator(mode="before")
    @classmethod
    def _drop_removed_writing_findings(cls, data: object) -> object:
        """Load invocation records written before ADR-036 removed ``writing_findings``."""
        if isinstance(data, dict) and "writing_findings" in data:
            return {key: value for key, value in data.items() if key != "writing_findings"}
        return data

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
    #: The latest tool calls the call started, oldest first, each a ``name`` and a
    #: one-line ``preview`` (dashboard X6). Only a runtime that streams its events fills it.
    recent_tool_calls: list[dict[str, str]] = Field(default_factory=list)
    #: Every tool call the call started so far, not only the recent ones.
    tool_call_count: int = Field(default=0, ge=0)
    #: When the call last printed a line of output; only a streaming runtime sets it.
    last_output_at: UtcDateTime | None = None
    #: The time limit the runtime gave this call; ``None`` on a run file written before it.
    timeout_seconds: int | None = Field(default=None, ge=1)


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


class RunLaunch(ModelBase):
    """The command settings a run started with, so ``factory escalate`` builds the same
    controller again (ADR-055). Roles that name their own runtime take it from the config."""

    #: The ``--runtime`` choice: the runtime of every role that names none.
    runtime: str = Field(min_length=1)
    model_profile: str = "default"
    #: ``None`` for the packaged configuration.
    config_path: str | None = None


class RunLease(ModelBase):
    """Ownership marker for a run held by one local process.

    Recorded so a restarted factory (or the scheduler's reconciliation pass)
    can tell an actively owned run from an abandoned one without a database.
    """

    host: str = Field(min_length=1)
    pid: int = Field(ge=1)
    heartbeat_at: UtcDateTime
