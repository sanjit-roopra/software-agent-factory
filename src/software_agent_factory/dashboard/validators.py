"""Shape checks shared by the dashboard modules. Pure: no I/O.

This is a leaf: it imports only ``models``, the escalation protocol leaf and
:mod:`.snapshot`, so the
aggregate, sanitize and next-step modules can all use it without importing each
other.

The number checks exclude ``bool``, because ``True`` is an ``int`` in Python and
must never count as a reported figure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from ..escalation_protocol import EPISODE_ID_PATTERN
from ..models import EscalationStatus, EscalationTargetType, ResumeClassification
from .snapshot import is_valid_run_id

_FINGERPRINT_PATTERN = re.compile(r"[A-Za-z0-9]{64}")

#: The one set of resume classifications, built from the enum.
RESUME_CLASSIFICATIONS: frozenset[str] = frozenset(item.value for item in ResumeClassification)
#: The escalation statuses and target types, built from their enums. A run
#: without a target has ``None`` for its type.
ESCALATION_STATUSES: frozenset[str] = frozenset(item.value for item in EscalationStatus)
ESCALATION_TARGET_TYPES: frozenset[str | None] = frozenset(
    {None, *(item.value for item in EscalationTargetType)}
)


def is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_count(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_positive_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_safe_https_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def is_episode_id(value: Any) -> TypeGuard[str]:
    """An episode id the reply parsers accept.

    It is wider than a run id, which never holds a ``.`` (see
    :func:`software_agent_factory.dashboard.snapshot.is_valid_run_id`).
    """
    return isinstance(value, str) and EPISODE_ID_PATTERN.fullmatch(value) is not None


def is_context_fingerprint(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _FINGERPRINT_PATTERN.fullmatch(value) is not None


def run_id_of(data: Mapping[str, Any]) -> str | None:
    """The run id of a run record, when it is shaped like a real one."""
    candidate = data.get("run_id", data.get("id"))
    return candidate if isinstance(candidate, str) and is_valid_run_id(candidate) else None
