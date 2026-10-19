"""The attempt history the dashboard may render, built from a run's attempt records."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field

from .models import AgentRole, AttemptBudget, AttemptRecord, AttemptTrigger, ModelBase, UtcDateTime

#: How many of an unfinished run's last attempts its run list row carries, so the dashboard
#: can tell when the run repeats itself (``dashboard.looping``).
RECENT_ATTEMPTS = 3


class RunAttemptSummary(ModelBase):
    """One attempt, reduced to the fields the dashboard may render.

    Deliberately excludes the attempt ``reasoning`` level. ``failure_reason``
    is carried as the raw stored text: it is free text that could quote
    repository content, so this model is not safe to show as is. The
    dashboard sanitizer (``dashboard.sanitize``) redacts and bounds it before
    it leaves the process.
    """

    attempt_number: int = Field(ge=1)
    role: AgentRole
    model: str
    budget: AttemptBudget
    triggered_by: AttemptTrigger
    outcome: str
    started_at: UtcDateTime
    completed_at: UtcDateTime
    failure_reason: str | None = None


def attempt_summaries(records: Iterable[AttemptRecord]) -> list[RunAttemptSummary]:
    """Each attempt record as a :class:`RunAttemptSummary`, in the same order."""
    return [
        RunAttemptSummary(
            attempt_number=attempt.attempt_number,
            role=attempt.role,
            model=attempt.model,
            budget=attempt.budget,
            triggered_by=attempt.triggered_by,
            outcome=attempt.outcome,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            failure_reason=attempt.failure_reason,
        )
        for attempt in records
    ]
