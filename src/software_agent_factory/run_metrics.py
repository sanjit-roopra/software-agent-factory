"""Run classification, usage totals and aggregate metrics for monitoring.

Pure functions of persisted ``FactoryRun``/``AttemptRecord``/``InvocationRecord``
data, split out of :mod:`software_agent_factory.observability`. Runtime-reported
usage is summarized only from persisted ``InvocationRecord`` values. Missing
fields stay unknown, and no value is reconstructed from a price table or
converted to USD (ADR-017).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Iterable, TypeVar

from pydantic import Field

from .lifecycle import is_run_finished
from .models import (
    TOKEN_CLASS_FIELDS,
    TOTAL_TOKEN_FIELDS,
    AttemptBudget,
    AttemptTrigger,
    FactoryRun,
    InvocationRecord,
    ModelBase,
    UsageMetrics,
    WorkflowState,
)

#: How many days of usage the cost trend covers, one :class:`UsageSummary` a day.
COST_TREND_DAYS = 7


def _classify_run(run: FactoryRun) -> str:
    """One of ``"escalated"``, ``"failed"``, ``"succeeded"``, ``"active"``.

    The single three-way branch :class:`RunStateCounts`, aggregate metrics,
    and stale-run health findings all build on, factored out once so every
    consumer agrees on exactly the same classification rule.
    """
    if run.state is WorkflowState.NEEDS_HUMAN:
        return "escalated"
    if run.state is WorkflowState.FAILED:
        return "failed"
    if is_run_finished(run):
        return "succeeded"
    return "active"


class UsageSummary(ModelBase):
    """Safe totals of explicitly reported invocation usage."""

    invocation_count: int = Field(ge=0)
    reported_invocations: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    premium_requests: float | None = Field(default=None, ge=0.0)
    premium_request_cost: float | None = Field(default=None, ge=0.0)
    total_nano_aiu: int | None = Field(default=None, ge=0)
    list_price_estimate_usd: float | None = Field(default=None, ge=0.0)
    """Summed pi list-price estimate (never spend). ``None`` when no
    invocation reported one."""


class FirstPassSuccessMetric(ModelBase):
    """Deterministic first-pass success measurement.

    A succeeded run (``_classify_run(run) == "succeeded"``: ``DONE`` or a
    finalized ``PR_READY``) counts as a first-pass success when it needed
    exactly one non-polish ``AttemptBudget.IMPLEMENTATION`` attempt to reach
    ``PR_READY`` -- i.e. the initial implementer attempt was never repaired
    for an implementer failure, a deterministic verification failure, an
    independent-reviewer rejection, or a bounded scope-drift replan. Every
    one of those repair paths in ``WorkflowController._drive_to_pr_ready``
    re-invokes the implementer under the same ``IMPLEMENTATION`` budget
    (``workflow.py``). The optional, planned ``POLISH`` attempt is excluded
    because it is not a recovery from first-pass failure.

    ``denominator`` is every succeeded run in the scanned set; ``rate`` is
    ``None`` (never ``0.0``) when the denominator is zero, since an
    undefined rate must never be reported as "0% first-pass success".
    """

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0.0, le=1.0)


class DurationSummary(ModelBase):
    """Summary of a duration distribution, in seconds. Every ``*_seconds``
    field is ``None`` (never ``0.0``) when ``count == 0``: an empty
    distribution has no minimum/maximum/average, and reporting zero would
    misrepresent "no data" as "instantaneous"."""

    count: int = Field(ge=0)
    min_seconds: float | None = Field(default=None, ge=0.0)
    max_seconds: float | None = Field(default=None, ge=0.0)
    average_seconds: float | None = Field(default=None, ge=0.0)


class ReworkMetrics(ModelBase):
    """Aggregate gate failure and rework metrics across scanned runs."""

    total_gate_failures: int = Field(default=0, ge=0)
    verification_gate_failures: int = Field(default=0, ge=0)
    review_gate_failures: int = Field(default=0, ge=0)
    total_rework_attempts: int = Field(default=0, ge=0)
    runs_with_rework: int = Field(default=0, ge=0)
    rework_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class PerformanceSummary(ModelBase):
    """Aggregate performance telemetry across scanned runs."""

    stage_durations: dict[str, DurationSummary] = Field(default_factory=dict)
    operation_durations: dict[str, DurationSummary] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    total_prompt_chars: int = Field(default=0, ge=0)
    total_response_chars: int = Field(default=0, ge=0)
    rework: ReworkMetrics = Field(default_factory=ReworkMetrics)


class AggregateMetrics(ModelBase):
    """Store-wide (scanned-subset) aggregate metrics: pure functions of
    persisted ``FactoryRun``/``AttemptRecord``/``InvocationRecord`` data.
    Usage totals include only explicitly reported values; missing telemetry
    remains unknown and is never reconstructed from a price table (ADR-017).

    ``completed_run_durations`` covers every *finished* run in the scanned
    set (``DONE``, ``NEEDS_HUMAN``, ``FAILED``, or a finalized ``PR_READY``
    -- i.e. every run for which ``completed_at`` is stamped), measuring
    ``completed_at - created_at``; it is not restricted to successes, since
    overall cycle time is useful regardless of outcome.
    """

    total_attempts: int = Field(ge=0)
    average_attempts_per_run: float | None = Field(default=None, ge=0.0)
    implementation_attempts: int = Field(ge=0)
    ci_repair_attempts: int = Field(ge=0)
    scope_replans: int = Field(ge=0)
    first_pass_success: FirstPassSuccessMetric
    completed_run_durations: DurationSummary
    usage: UsageSummary
    performance: PerformanceSummary = Field(default_factory=PerformanceSummary)


def _failed_since(runs: list[FactoryRun], cutoff: datetime) -> int:
    """Failed runs that completed strictly after ``cutoff``."""
    return sum(
        1
        for run in runs
        if _classify_run(run) == "failed"
        and run.completed_at is not None
        and run.completed_at > cutoff
    )


def _reported_tokens(usage: UsageMetrics | None) -> int | None:
    """One invocation's tokens across ``TOTAL_TOKEN_FIELDS``, or ``None`` if it reported none.

    A class the call did not report adds nothing, but a call that reported no class at all is
    unknown, not zero (ADR-017).
    """
    if usage is None:
        return None
    resolved = resolve_usage(usage)
    values = [
        value for field in TOTAL_TOKEN_FIELDS if (value := getattr(resolved, field)) is not None
    ]
    return sum(values) if values else None


def _tokens_since(runs: list[FactoryRun], cutoff: datetime) -> int | None:
    """Reported tokens of every invocation that completed strictly after ``cutoff``.

    ``0`` when no invocation completed after ``cutoff``. ``None`` when some did and none of
    them reported a counted token class.
    """
    recent = [
        invocation
        for run in runs
        for invocation in run.invocation_records
        if invocation.completed_at > cutoff
    ]
    if not recent:
        return 0
    reported = [
        tokens
        for invocation in recent
        if (tokens := _reported_tokens(invocation.usage)) is not None
    ]
    return sum(reported) if reported else None


def _daily_usage(runs: list[FactoryRun], reference_time: datetime) -> list[UsageSummary]:
    """The usage of the calls that completed in each of the last ``COST_TREND_DAYS`` days.

    Day ``COST_TREND_DAYS - 1`` is the 24 hours before ``reference_time``; a call that
    completed earlier, or after ``reference_time``, is left out.
    """
    days: list[list[InvocationRecord]] = [[] for _ in range(COST_TREND_DAYS)]
    for run in runs:
        for invocation in run.invocation_records:
            days_ago = (reference_time - invocation.completed_at) // timedelta(days=1)
            if 0 <= days_ago < COST_TREND_DAYS:
                days[COST_TREND_DAYS - 1 - days_ago].append(invocation)
    return [summarize_usage(day) for day in days]


def _compute_attempt_tallies(
    runs: list[FactoryRun],
) -> tuple[dict[str, int], dict[str, int]]:
    by_role: dict[str, int] = {}
    by_model: dict[str, int] = {}
    for run in runs:
        for attempt in run.attempt_records:
            by_role[attempt.role.value] = by_role.get(attempt.role.value, 0) + 1
            by_model[attempt.model] = by_model.get(attempt.model, 0) + 1
    return by_role, by_model


def _duration_summary(durations_seconds: list[float]) -> DurationSummary:
    if not durations_seconds:
        return DurationSummary(count=0, min_seconds=None, max_seconds=None, average_seconds=None)
    return DurationSummary(
        count=len(durations_seconds),
        min_seconds=min(durations_seconds),
        max_seconds=max(durations_seconds),
        average_seconds=sum(durations_seconds) / len(durations_seconds),
    )


def _attempt_counts(run: FactoryRun) -> tuple[int, int, int]:
    """Per-run ``(counted implementation, CI repair, legacy scope replan)`` attempts.

    Counted implementation attempts leave out polish attempts. Legacy scope replans
    count attempts triggered by scope, for runs saved before ``scope_replans`` existed.
    """
    implementation = 0
    ci_repair = 0
    scope = 0
    for attempt in run.attempt_records:
        if attempt.budget is AttemptBudget.IMPLEMENTATION:
            implementation += int(attempt.triggered_by is not AttemptTrigger.POLISH)
        elif attempt.budget is AttemptBudget.CI_REPAIR:
            ci_repair += 1
        scope += int(attempt.triggered_by is AttemptTrigger.SCOPE)
    return implementation, ci_repair, scope


def _collect_performance_samples(
    run: FactoryRun,
    stage_samples: dict[str, list[float]],
    operation_samples: dict[str, list[float]],
    counters: dict[str, int],
) -> None:
    """Add ``run``'s stage and operation durations (in seconds) and counters to the totals."""
    for key, value in run.performance.durations_ms.items():
        if key.startswith("stage."):
            stage_samples.setdefault(key.removeprefix("stage."), []).append(value / 1000.0)
        elif key.startswith("operation."):
            operation_samples.setdefault(key.removeprefix("operation."), []).append(value / 1000.0)
    for key, count in run.performance.counters.items():
        counters[key] = counters.get(key, 0) + count


def _gate_failures(counters: dict[str, int], gate: str, state: str) -> int:
    """Gate failures counted under the gate name plus the older workflow-state name."""
    return counters.get(f"gate_failure.{gate}", 0) + counters.get(f"gate_failure.{state}", 0)


def _compute_aggregate_metrics(runs: list[FactoryRun]) -> AggregateMetrics:
    """See :class:`AggregateMetrics` for the exact, deterministic definition
    of each field. Pure function of ``runs`` (the scanned readable subset);
    no wall-clock/timezone dependence beyond the persisted timestamps
    themselves."""
    total_attempts = 0
    implementation_attempts = 0
    ci_repair_attempts = 0
    scope_replans = 0
    succeeded_total = 0
    first_pass_numerator = 0
    durations: list[float] = []

    stage_duration_samples: dict[str, list[float]] = {}
    operation_duration_samples: dict[str, list[float]] = {}
    aggregated_counters: dict[str, int] = {}
    total_prompt_chars = 0
    total_response_chars = 0
    total_gate_failures = 0
    verification_gate_failures = 0
    review_gate_failures = 0
    total_rework_attempts = 0
    runs_with_rework = 0

    for run in runs:
        counted_implementation, run_ci_repair_attempts, legacy_scope_replans = _attempt_counts(run)
        total_attempts += len(run.attempt_records)
        implementation_attempts += sum(
            attempt.budget is AttemptBudget.IMPLEMENTATION for attempt in run.attempt_records
        )
        ci_repair_attempts += run_ci_repair_attempts
        run_scope_replans = max(run.scope_replans, legacy_scope_replans)
        scope_replans += run_scope_replans

        if run.completed_at is not None and is_run_finished(run):
            durations.append(max((run.completed_at - run.created_at).total_seconds(), 0.0))

        if _classify_run(run) == "succeeded":
            succeeded_total += 1
            first_pass_numerator += int(counted_implementation == 1)

        _collect_performance_samples(
            run, stage_duration_samples, operation_duration_samples, aggregated_counters
        )
        total_prompt_chars += run.performance.prompt_chars or 0
        total_response_chars += run.performance.response_chars or 0

        counters = run.performance.counters
        total_gate_failures += counters.get("gate_failures_total", 0)
        verification_gate_failures += _gate_failures(counters, "verification", "VERIFYING")
        review_gate_failures += _gate_failures(counters, "review", "REVIEWING")
        fallback_rework = (
            max(0, counted_implementation - 1) + run_ci_repair_attempts + run_scope_replans
        )
        run_rework = counters.get("rework_total", 0) or fallback_rework
        total_rework_attempts += run_rework
        runs_with_rework += int(run_rework > 0)

    average_attempts_per_run = (total_attempts / len(runs)) if runs else None
    first_pass_success = FirstPassSuccessMetric(
        numerator=first_pass_numerator,
        denominator=succeeded_total,
        rate=(first_pass_numerator / succeeded_total) if succeeded_total > 0 else None,
    )
    completed_run_durations = _duration_summary(durations)
    stage_durations = {
        stage: _duration_summary(samples)
        for stage, samples in sorted(stage_duration_samples.items())
    }
    operation_durations = {
        op: _duration_summary(samples) for op, samples in sorted(operation_duration_samples.items())
    }
    rework = ReworkMetrics(
        total_gate_failures=total_gate_failures,
        verification_gate_failures=verification_gate_failures,
        review_gate_failures=review_gate_failures,
        total_rework_attempts=total_rework_attempts,
        runs_with_rework=runs_with_rework,
        rework_rate=(runs_with_rework / len(runs)) if runs else None,
    )
    performance = PerformanceSummary(
        stage_durations=stage_durations,
        operation_durations=operation_durations,
        counters=aggregated_counters,
        total_prompt_chars=total_prompt_chars,
        total_response_chars=total_response_chars,
        rework=rework,
    )

    return AggregateMetrics(
        total_attempts=total_attempts,
        average_attempts_per_run=average_attempts_per_run,
        implementation_attempts=implementation_attempts,
        ci_repair_attempts=ci_repair_attempts,
        scope_replans=scope_replans,
        first_pass_success=first_pass_success,
        completed_run_durations=completed_run_durations,
        usage=summarize_usage(invocation for run in runs for invocation in run.invocation_records),
        performance=performance,
    )


_UsageNumber = TypeVar("_UsageNumber", int, float)


def _reported_or_model_sum(
    reported: _UsageNumber | None, model_values: Sequence[_UsageNumber]
) -> _UsageNumber | None:
    """Return ``reported`` when the aggregate itself reported the field, else
    the sum of ``model_values`` when any per-model entry reported it, else
    ``None``.

    Shared by every :class:`~software_agent_factory.models.UsageMetrics`
    field that also has a
    :class:`~software_agent_factory.models.ModelUsage` per-model
    counterpart -- callers still build ``model_values`` themselves (it must
    already exclude entries the model did not report) so this only factors
    out the "aggregate, else per-model sum, else unknown" decision.
    """
    if reported is not None:
        return reported
    if model_values:
        return sum(model_values)
    return None


# UsageMetrics fields that also have a per-model ModelUsage counterpart of the
# same name, so a runtime may report them only per model.
_MODEL_BACKED_USAGE_FIELDS = (
    *TOKEN_CLASS_FIELDS,
    "total_nano_aiu",
    "list_price_estimate_usd",
)


def _sum_or_none(values: Sequence[Any]) -> Any:
    return sum(values) if values else None


def _usage_field_value(usage: UsageMetrics, field: str) -> Any:
    """One invocation's value for ``field``: the aggregate, else the per-model sum."""
    model_values = [
        value for item in usage.model_usage if (value := getattr(item, field)) is not None
    ]
    return _reported_or_model_sum(getattr(usage, field), model_values)


def _premium_request_costs(usage: UsageMetrics) -> list[float]:
    """The aggregate cost when reported, else each per-model cost that was reported."""
    if usage.total_premium_request_cost is not None:
        return [usage.total_premium_request_cost]
    return [
        item.premium_request_cost
        for item in usage.model_usage
        if item.premium_request_cost is not None
    ]


def resolve_usage(usage: UsageMetrics) -> UsageMetrics:
    """One invocation's usage with every aggregate field resolved.

    A runtime may report a figure only per model. Each token, nano-AIU, list-price
    and premium-request-cost field then takes the aggregate when reported, else the
    per-model sum, else stays ``None``. The run totals, the call timeline and the
    dashboard all read this one definition.
    """
    resolved: dict[str, Any] = {
        field: _usage_field_value(usage, field) for field in _MODEL_BACKED_USAGE_FIELDS
    }
    resolved["total_premium_request_cost"] = _sum_or_none(_premium_request_costs(usage))
    return usage.model_copy(update=resolved)


def summarize_usage(invocations: Iterable[InvocationRecord]) -> UsageSummary:
    """Sum runtime-reported usage; a field no invocation reported stays ``None``."""
    records = list(invocations)
    usages = [resolve_usage(record.usage) for record in records if record.usage is not None]
    totals = {
        field: _sum_or_none([v for usage in usages if (v := getattr(usage, field)) is not None])
        for field in _MODEL_BACKED_USAGE_FIELDS
    }
    premium_requests = [
        usage.premium_requests for usage in usages if usage.premium_requests is not None
    ]
    premium_request_costs = [
        usage.total_premium_request_cost
        for usage in usages
        if usage.total_premium_request_cost is not None
    ]
    return UsageSummary(
        invocation_count=len(records),
        reported_invocations=len(usages),
        premium_requests=_sum_or_none(premium_requests),
        premium_request_cost=_sum_or_none(premium_request_costs),
        **totals,
    )
