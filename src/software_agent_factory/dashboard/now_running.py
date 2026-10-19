"""The ``now_running`` list of ``/api/summary``: the calls in progress across every run.

Like :mod:`.sanitize`, it keeps only what the page shows. Each call keeps its run id when
it is shaped like a real one, its role when it is a known role, the run title and model
name redacted with :func:`software_agent_factory.redaction.redact_secrets`, and its start
time when it is a timestamp. A call without a valid run id is dropped, and so is any
entry past :data:`MAX_NOW_RUNNING`.
"""

from __future__ import annotations

from typing import Any

from ..models import AgentRole
from ..redaction import redact_secrets
from .transitions import timestamp
from .validators import run_id_of

_ROLES = frozenset(role.value for role in AgentRole)
#: The most calls sent, whatever the provider lists (the snapshot lists at most 20 too).
MAX_NOW_RUNNING = 20


def _redacted(value: Any) -> str | None:
    return redact_secrets(value) if isinstance(value, str) else None


def sanitize_active_calls(raw: Any) -> list[dict[str, Any]]:
    """The snapshot's ``active_calls`` cut down to the fields the "Now running" strip shows."""
    items = raw[:MAX_NOW_RUNNING] if isinstance(raw, list) else []
    calls = []
    for item in items:
        run_id = run_id_of(item) if isinstance(item, dict) else None
        if run_id is None:
            continue
        calls.append(
            {
                "run_id": run_id,
                "title": _redacted(item.get("title")),
                "role": item.get("role") if item.get("role") in _ROLES else None,
                "model": _redacted(item.get("model")),
                "started_at": timestamp(item.get("started_at")),
            }
        )
    return calls
