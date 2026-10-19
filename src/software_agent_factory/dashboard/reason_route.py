"""The ``GET /api/runs/<id>/reason`` route's data (ADR-046, ADR-062).

Without a query it answers the run's failure reason. ``?attempt=n`` answers the reason of
the attempt with that ``attempt_number``, and ``?call=n`` the reason of the call with that
``invocation_number``. The run detail cuts each of these at ``REASON_LIMIT``; this route
redacts the same text and cuts it only past :data:`FULL_REASON_LIMIT`.
"""

from __future__ import annotations

import re
from typing import Any

from ..redaction import bounded_reason
from .snapshot import RunDetailProvider, to_json_safe
from .validators import run_id_of

#: Largest failure reason ``GET /api/runs/<id>/reason`` returns, after redaction.
FULL_REASON_LIMIT = 20_000

#: An attempt or call number: 1 to 9999, no sign or leading zero.
_NUMBER_PATTERN = re.compile(r"^[1-9]\d{0,3}$", re.ASCII)
#: Each query key, with the run detail list it picks from and the record's number field.
_RECORD_KINDS = {
    "attempt": ("attempts", "attempt_number"),
    "call": ("invocations", "invocation_number"),
}


def sanitize_full_reason(raw: Any) -> dict[str, Any]:
    """The record's redacted failure reason, cut only past :data:`FULL_REASON_LIMIT`."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run detail must serialize to a JSON object")
    value = data.get("failure_reason")
    if not isinstance(value, str) or not value:
        return {"failure_reason": None, "failure_reason_truncated": False}
    reason, truncated = bounded_reason(value, FULL_REASON_LIMIT, run_id_of(data))
    return {"failure_reason": reason, "failure_reason_truncated": truncated}


def reason_source(
    provider: RunDetailProvider, query: dict[str, list[str]]
) -> RunDetailProvider | None:
    """The provider of the record ``query`` names, or ``None`` when the query is not valid.

    The returned provider gives ``None`` for an unknown run and for a run without that
    record, so both answer ``404``.
    """
    picked = [(key, values) for key, values in query.items() if key in _RECORD_KINDS]
    if not picked:
        return provider
    if len(picked) > 1 or len(picked[0][1]) != 1 or not _NUMBER_PATTERN.match(picked[0][1][0]):
        return None
    list_key, number_key = _RECORD_KINDS[picked[0][0]]
    number = int(picked[0][1][0])

    def record(run_id: str) -> Any | None:
        data = to_json_safe(provider(run_id))
        if data is None:
            return None
        if not isinstance(data, dict):
            raise TypeError("run detail must serialize to a JSON object")
        records = data.get(list_key)
        for item in records if isinstance(records, list) else []:
            if (
                isinstance(item, dict)
                and type(item.get(number_key)) is int
                and item[number_key] == number
            ):
                return {"run_id": run_id_of(data), "failure_reason": item.get("failure_reason")}
        return None

    return record
