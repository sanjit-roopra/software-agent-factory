"""Timestamps and a run's state moves, reduced to what the run page shows (ADR-067).

A state move keeps its two state names, its time and its reason. The reason is free
text, so it is redacted and cut like a failure reason. At most
:data:`~software_agent_factory.models.MAX_STATE_TRANSITIONS` moves are sent, the newest.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import MAX_STATE_TRANSITIONS, WorkflowState
from ..redaction import bounded_reason

_STATES = frozenset(state.value for state in WorkflowState)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timezone-aware ISO timestamp; anything else is unreported."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def timestamp(value: Any) -> str | None:
    return value if parse_timestamp(value) is not None else None


def _transition(item: Any, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    at = timestamp(item.get("at"))
    if at is None or item.get("from_state") not in _STATES or item.get("to_state") not in _STATES:
        return None
    reason = item.get("reason")
    return {
        "from_state": item["from_state"],
        "to_state": item["to_state"],
        "at": at,
        "reason": bounded_reason(reason, run_id=run_id)[0] if isinstance(reason, str) else None,
    }


def sanitize_transitions(value: Any, run_id: str | None) -> list[dict[str, Any]]:
    """The valid state moves in ``value``, the newest :data:`MAX_STATE_TRANSITIONS` of them."""
    if not isinstance(value, list):
        return []
    moves = (_transition(item, run_id) for item in value[-MAX_STATE_TRANSITIONS:])
    return [move for move in moves if move is not None]
