"""The waiting stop request and steer note of a run, for the run page (ADR-049, ADR-051).

:func:`with_pending` sets them on a run detail. :func:`sanitize_pending` keeps only their
safe fields: the source, the time, and the note redacted and cut.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..models import MAX_STEER_NOTE_CHARS
from ..redaction import redact_secrets
from .snapshot import to_json_safe

if TYPE_CHECKING:
    from ..store import FileRunStore

_SOURCES = frozenset({"dashboard", "cli"})


def with_pending[Detail: BaseModel](store: FileRunStore, run_id: str, detail: Detail) -> Detail:
    """``detail`` with the ``stop_request`` and ``steer_note`` of the run, or ``None``."""
    return detail.model_copy(
        update={
            "stop_request": store.load_stop_request(run_id),
            "steer_note": store.load_steer_note(run_id),
        }
    )


def sanitize_pending(raw: Any) -> dict[str, Any]:
    """The safe ``stop_request`` and ``steer_note`` of a run detail, each when valid."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        return {}
    sanitized: dict[str, Any] = {}
    stop = _waiting(data.get("stop_request"))
    if stop is not None:
        sanitized["stop_request"] = stop
    steer = _waiting(data.get("steer_note"))
    note = data["steer_note"].get("note") if steer is not None else None
    if steer is not None and isinstance(note, str) and note:
        sanitized["steer_note"] = {**steer, "note": redact_secrets(note)[:MAX_STEER_NOTE_CHARS]}
    return sanitized


def _waiting(value: Any) -> dict[str, str] | None:
    """The source and time of a waiting request, or ``None`` when either is bad."""
    if not isinstance(value, dict) or value.get("source") not in _SOURCES:
        return None
    created_at = value.get("created_at")
    if not isinstance(created_at, str):
        return None
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        return None
    return {"source": value["source"], "created_at": created_at}
