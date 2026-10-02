"""Run totals for the dashboard: pure, no I/O.

Each figure carries its ``total`` and ``reported_count`` (how many calls
reported it), so the page can say "N of M calls reported". ``total`` is
``None`` when no call reported the figure, and a reported ``0`` counts as
reported. Cost units are never added together.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..models import COST_UNIT_FIELDS, TOKEN_CLASS_FIELDS, TOTAL_TOKEN_FIELDS
from .validators import is_number

#: The two outcomes a finished call reports. ``OUTCOMES`` in ``static/app.js``
#: keys on these same strings.
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"


def _figure(values: Iterable[Any]) -> dict[str, Any]:
    """Sum the reported (numeric, non-bool) values; ``None`` when none were."""
    reported = [v for v in values if is_number(v)]
    return {"total": sum(reported) if reported else None, "reported_count": len(reported)}


def _usage_figure(calls: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    usages = (call.get("usage") for call in calls)
    return _figure(u.get(field) for u in usages if isinstance(u, Mapping))


def call_total_tokens(usage: Mapping[str, Any]) -> int | float | None:
    """One call's total tokens, or ``None`` when it reported none of the classes."""
    total: int | float | None = _figure(usage.get(field) for field in TOTAL_TOKEN_FIELDS)["total"]
    return total


def run_totals(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Total sanitized finished calls by token class and by cost unit.

    ``calls`` is the finished calls only; the active call has no usage yet.
    ``failed_calls`` counts calls whose ``status`` is ``STATUS_FAILED`` out of those
    that reported a ``STATUS_SUCCESS`` or ``STATUS_FAILED`` status.
    """
    outcomes = (STATUS_SUCCESS, STATUS_FAILED)
    statuses = [call.get("status") for call in calls if call.get("status") in outcomes]
    return {
        "calls": len(calls),
        "failed_calls": {
            "total": statuses.count(STATUS_FAILED) if statuses else None,
            "reported_count": len(statuses),
        },
        "duration_ms": _figure(call.get("duration_ms") for call in calls),
        "tokens": {field: _usage_figure(calls, field) for field in TOKEN_CLASS_FIELDS},
        "costs": {field: _usage_figure(calls, field) for field in COST_UNIT_FIELDS},
    }


#: The role of a call that names no usable role.
UNKNOWN_ROLE = "UNKNOWN"


def _role_of(call: Mapping[str, Any]) -> str:
    role = call.get("role")
    return role if isinstance(role, str) and role else UNKNOWN_ROLE


def models_of(calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """The distinct model names the calls report, sorted. Calls without one add none."""
    return sorted(
        {model for call in calls if isinstance(model := call.get("model"), str) and model}
    )


def role_breakdown(calls: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """One :func:`run_totals` per role, plus the ``models`` that role's calls used.

    Roles come in the order of their first call. A call without a usable role counts
    under ``UNKNOWN_ROLE``. No calls gives no roles.
    """
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for call in calls:
        by_role.setdefault(_role_of(call), []).append(call)
    return {
        role: {**run_totals(role_calls), "models": models_of(role_calls)}
        for role, role_calls in by_role.items()
    }


def compare_roles(
    a_calls: Sequence[Mapping[str, Any]], b_calls: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The roles of two runs side by side: one row per role in either run.

    A row is ``{"role", "a", "b"}``. A side holds that run's :func:`role_breakdown`
    entry, or ``None`` when the run made no call in the role. Rows come in order of first
    call: run A's roles, then the roles only run B used. Costs of the two runs are never
    added: each side keeps its own.
    """
    a_roles = role_breakdown(a_calls)
    b_roles = role_breakdown(b_calls)
    return [
        {"role": role, "a": a_roles.get(role), "b": b_roles.get(role)}
        for role in (*a_roles, *(role for role in b_roles if role not in a_roles))
    ]
