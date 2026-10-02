"""The readable overview figures: a run's outcome, its reason line, which step failed it, and
the one-row summary of the run store.

Pure: no I/O. These functions read data that already went through
:mod:`software_agent_factory.dashboard.sanitize`, so they copy text and never redact it. The
page only formats the numbers and the times they return.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..models import TOTAL_TOKEN_FIELDS, WorkflowState
from .aggregate import STATUS_FAILED
from .validators import is_count, is_number

#: The ``kind`` of a run outcome. The page picks a color and a class from it.
OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_NEEDS_YOU = "needs_you"
OUTCOME_ACTIVE = "active"

#: How a call that did not fail itself relates to a failed run (see :func:`flag_failing_call`).
FLAG_REJECTED = "rejected"
FLAG_LAST_CALL = "last_call"

_OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_DONE: "Done",
    OUTCOME_FAILED: "Failed",
    OUTCOME_NEEDS_YOU: "Needs you",
    OUTCOME_ACTIVE: "Active",
}
_PR_READY_LABEL = "PR ready"
_STALE_LABEL = "Active, stale"


def _outcome(kind: str, label: str | None = None) -> dict[str, str]:
    return {"kind": kind, "label": label or _OUTCOME_LABELS[kind]}


def _state(run: Mapping[str, Any]) -> Any:
    return run.get("state")


def run_outcome(run: Mapping[str, Any]) -> dict[str, str]:
    """The outcome of a run as a ``kind`` and a ``label``: done, failed, needs you or active.

    The label always names the outcome in words, so a badge never relies on its color.
    ``PR_READY`` is done only once the run is finished (``is_finished``). Until then it is
    active, as :func:`software_agent_factory.observability.build_monitoring_snapshot` counts it.
    """
    state = _state(run)
    if state == WorkflowState.FAILED:
        return _outcome(OUTCOME_FAILED)
    if state == WorkflowState.NEEDS_HUMAN:
        return _outcome(OUTCOME_NEEDS_YOU)
    if state == WorkflowState.PR_READY and run.get("is_finished") is True:
        return _outcome(OUTCOME_DONE, _PR_READY_LABEL)
    if state == WorkflowState.DONE:
        return _outcome(OUTCOME_DONE)
    return _outcome(OUTCOME_ACTIVE, _STALE_LABEL if run.get("is_stale") is True else None)


def reason_line(run: Mapping[str, Any]) -> str | None:
    """Why a run stopped: its redacted, bounded failure reason, only while it is stopped."""
    reason = run.get("failure_reason")
    stopped = run_outcome(run)["kind"] in (OUTCOME_FAILED, OUTCOME_NEEDS_YOU)
    return reason if stopped and isinstance(reason, str) and reason else None


def headline(run: Mapping[str, Any]) -> dict[str, str]:
    """The one outcome line of the run page, as a ``kind`` and ``text``.

    A failed run says ``Failed: <reason>``. A run that waits for a person says ``Needs you``
    and the reason it stopped, when it has one. The other runs say their state in words.
    """
    outcome = run_outcome(run)
    kind, label = outcome["kind"], outcome["label"]
    reason = reason_line(run)
    if kind in (OUTCOME_FAILED, OUTCOME_NEEDS_YOU):
        return {"kind": kind, "text": f"{label}: {reason}" if reason else label}
    if kind == OUTCOME_ACTIVE and isinstance(state := _state(run), str):
        return {"kind": kind, "text": f"In progress: {state}"}
    return {"kind": kind, "text": label}


def _names_role(reason: str, role: str) -> bool:
    return re.search(rf"\b{re.escape(role)}\b", reason, re.IGNORECASE) is not None


def flag_failing_call(run: Mapping[str, Any], calls: Sequence[dict[str, Any]]) -> None:
    """Give every call a ``failure_link`` in place, and set it on the call a failed run failed on.

    The agent call itself reports success when the factory rejects what it returned, so its own
    status cannot say. The rule, for a ``FAILED`` run only, looks at the last finished call:

    - it already failed: nothing to add.
    - the run's failure reason names its role (``reviewer used legacy fields``): the factory
      words such a reason with the role whose output it rejected, so the call gets
      ``failure_link == FLAG_REJECTED``.
    - otherwise the run failed after it, for a reason that does not name its role (for example
      a verification failure): the call gets ``failure_link == FLAG_LAST_CALL``.

    The match is on the role name as a whole word, ignoring case. Every other call keeps ``None``.
    """
    for call in calls:
        call["failure_link"] = None
    if _state(run) != WorkflowState.FAILED or not calls:
        return
    last = calls[-1]
    if last.get("status") == STATUS_FAILED:
        return
    reason, role = run.get("failure_reason"), last.get("role")
    names_role = isinstance(reason, str) and isinstance(role, str) and _names_role(reason, role)
    last["failure_link"] = FLAG_REJECTED if names_role else FLAG_LAST_CALL


def _count(value: Any) -> int:
    return value if is_count(value) else 0


def _total_tokens(usage: Mapping[str, Any]) -> int | float | None:
    reported = [usage[field] for field in TOTAL_TOKEN_FIELDS if is_number(usage.get(field))]
    return sum(reported) if reported else None


def _usage_of(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = snapshot.get("metrics")
    usage = metrics.get("usage") if isinstance(metrics, Mapping) else None
    return usage if isinstance(usage, Mapping) else {}


def _reported(value: Any) -> int | float | None:
    return value if is_number(value) else None


def snapshot_overview(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The one-row summary of the run store: run counts, tokens and cost.

    A cost unit is ``None`` when no call reported it. The two units stay apart: the list-price
    estimate in US dollars and the premium request cost are never added together. The counts
    cover the scanned runs, and ``scan_truncated`` says when that is not every run.
    """
    counts = snapshot.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    usage = _usage_of(snapshot)
    shown = {key: _count(counts.get(key)) for key in ("succeeded", "failed", "active")}
    return {
        "runs": sum(shown.values()) + _count(counts.get("escalated")),
        **shown,
        "needs_you": _count(snapshot.get("needs_human_count")),
        "failed_last_24h": _reported(snapshot.get("failed_last_24h")),
        "tokens": _total_tokens(usage),
        "tokens_last_24h": _reported(snapshot.get("tokens_last_24h")),
        "list_price_usd": _reported(usage.get("list_price_estimate_usd")),
        "premium_requests": _reported(usage.get("premium_request_cost")),
        "scan_truncated": snapshot.get("scan_truncated") is True,
    }
