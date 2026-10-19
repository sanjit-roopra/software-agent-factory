"""A run's state history: each move it made between states, kept within a bound (ADR-067).

Every path that changes a run's state calls :func:`leave_state` and saves the list it
returns, so the run page can show how long each stage took and how often it came back.
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    MAX_STATE_TRANSITIONS,
    MAX_TRANSITION_REASON_CHARS,
    FactoryRun,
    StateTransition,
    WorkflowState,
)


def leave_state(
    run: FactoryRun,
    new_state: WorkflowState,
    now: datetime,
    reason: str | None = None,
    *,
    timed: bool = True,
) -> list[StateTransition]:
    """The run's moves with this one added, the oldest dropped past the bound.

    When ``timed``, the time spent in the state it leaves is added to the run's
    ``stage.<state>`` duration. A restart after downtime passes ``timed=False``.
    """
    if timed:
        stage_name = run.state.value
        stage_started_at = run.state_started_at or run.updated_at
        run.performance.record_duration(
            f"stage.{stage_name}",
            max(0.0, (now - stage_started_at).total_seconds() * 1000.0),
            stage=stage_name,
            operation="stage",
            accumulate=True,
        )
    move = StateTransition(
        from_state=run.state,
        to_state=new_state,
        at=now,
        reason=reason[:MAX_TRANSITION_REASON_CHARS] if reason else None,
    )
    return [*run.transitions, move][-MAX_STATE_TRANSITIONS:]
