"""Run totals for the dashboard: pure, no I/O.

Each figure carries its ``total`` and ``reported_count`` (how many calls
reported it), so the page can say "N of M calls reported". ``total`` is
``None`` when no call reported the figure, and a reported ``0`` counts as
reported. Cost units are never added together.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Token classes and cost units a call always reports. An unreported value is
#: ``None``, never ``0``.
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


def _figure(values: Iterable[Any]) -> dict[str, Any]:
    """Sum the reported (numeric, non-bool) values; ``None`` when none were."""
    reported = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {"total": sum(reported) if reported else None, "reported_count": len(reported)}


def _usage_figure(calls: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    usages = (call.get("usage") for call in calls)
    return _figure(u.get(field) for u in usages if isinstance(u, Mapping))


def run_totals(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Total sanitized finished calls by token class and by cost unit.

    ``calls`` is the finished calls only; the active call has no usage yet.
    ``failed_calls`` counts calls whose ``status`` is ``FAILED`` out of those
    that reported a ``SUCCESS`` or ``FAILED`` status.
    """
    statuses = [call.get("status") for call in calls if call.get("status") in ("SUCCESS", "FAILED")]
    return {
        "calls": len(calls),
        "failed_calls": {
            "total": statuses.count("FAILED") if statuses else None,
            "reported_count": len(statuses),
        },
        "duration_ms": _figure(call.get("duration_ms") for call in calls),
        "tokens": {field: _usage_figure(calls, field) for field in TOKEN_CLASS_FIELDS},
        "costs": {field: _usage_figure(calls, field) for field in COST_UNIT_FIELDS},
    }
