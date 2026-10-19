"""The ``Possibly looping`` note of an active run whose last attempts repeat (ADR-071).

The workflow judges a review impasse only when it halts (``review_ledger``). This note lets a
person see the repeat while the run still works. It reads the sanitized attempt records only:
no other file and nothing new is stored. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..attempt_summary import RECENT_ATTEMPTS
from .overview import OUTCOME_ACTIVE, OUTCOME_STALE

#: How many characters of a failure reason two attempts must share to count as the same failure.
REASON_PREFIX = 80

#: What each retry trigger says about the attempt before it, in words.
_TRIGGER_WORDS: dict[str, str] = {
    "IMPLEMENTER_FAILURE": "The implementer failed",
    "VERIFICATION": "The checks failed",
    "REVIEW": "Review sent the work back",
    "SCOPE": "The scope check sent the work back",
    "CI": "CI failed",
}


def _reason_prefix(attempt: Mapping[str, Any]) -> str | None:
    reason = attempt.get("failure_reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    return " ".join(reason.split())[:REASON_PREFIX]


def looping_note(attempts: Iterable[Any]) -> str | None:
    """Why the last :data:`RECENT_ATTEMPTS` attempts look like a loop, or ``None``.

    They repeat when each failed with a reason that starts the same way, or when the same
    retry trigger started each of them (for example, review sent the work back each time).
    """
    last = [attempt for attempt in attempts if isinstance(attempt, Mapping)][-RECENT_ATTEMPTS:]
    if len(last) < RECENT_ATTEMPTS:
        return None
    reasons = {_reason_prefix(attempt) for attempt in last}
    if len(reasons) == 1 and (reason := reasons.pop()) is not None:
        return f"Same failure in the last {RECENT_ATTEMPTS} attempts: {reason}"
    triggers = {attempt.get("triggered_by") for attempt in last}
    trigger = triggers.pop() if len(triggers) == 1 else None
    if isinstance(trigger, str) and trigger in _TRIGGER_WORDS:
        return f"{_TRIGGER_WORDS[trigger]} {RECENT_ATTEMPTS} times in a row"
    return None


def active_looping_note(outcome: Mapping[str, Any], attempts: Iterable[Any]) -> str | None:
    """:func:`looping_note` for an active or stale run; ``None`` for any other run."""
    if outcome.get("kind") not in (OUTCOME_ACTIVE, OUTCOME_STALE):
        return None
    return looping_note(attempts)
