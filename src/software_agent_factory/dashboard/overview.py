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
from ..resume_classes import RESUME_CLASSES
from .aggregate import STATUS_FAILED
from .sanitize import sanitize_usage
from .validators import is_count, is_number

#: The ``kind`` of a run outcome. The page picks a color and a class from it.
OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"
OUTCOME_NEEDS_YOU = "needs_you"
OUTCOME_ACTIVE = "active"
#: An active run with no new signal for longer than the stale limit.
OUTCOME_STALE = "stale"

#: What a run that waits for a person needs: the ``action`` of a ``needs_you`` outcome. A
#: resumable class names its own (``RESUME_CLASSES``) and has an action on the run page;
#: ``stopped`` has only a command.
ACTION_STOPPED = "stopped"
_STOPPED = (ACTION_STOPPED, "Stopped, needs you")

#: How a call that did not fail itself relates to a failed run (see :func:`flag_failing_call`).
FLAG_REJECTED = "rejected"
FLAG_LAST_CALL = "last_call"

_OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_DONE: "Done",
    OUTCOME_FAILED: "Failed",
    OUTCOME_NEEDS_YOU: "Needs you",
    OUTCOME_ACTIVE: "Active",
    OUTCOME_STALE: "Active, stale",
}
_PR_READY_LABEL = "PR ready"
_NEEDS_LOOK_LABEL = "Done, needs look"
#: State words the stage label keeps in capitals.
_ACRONYMS = frozenset({"CI", "PR"})


def _outcome(kind: str, label: str | None = None) -> dict[str, str]:
    return {"kind": kind, "label": label or _OUTCOME_LABELS[kind]}


def _state(run: Mapping[str, Any]) -> Any:
    return run.get("state")


def _done_outcome(run: Mapping[str, Any], label: str | None = None) -> dict[str, str]:
    """A done outcome. It says ``needs look`` when the pull request skipped a gate."""
    return _outcome(OUTCOME_DONE, _NEEDS_LOOK_LABEL if run.get("needs_look") else label)


def _resume_classification(run: Mapping[str, Any]) -> Any:
    """The run list row carries it at the top, the run detail in its escalation block."""
    escalation = run.get("escalation")
    nested = escalation.get("resume_classification") if isinstance(escalation, Mapping) else None
    return run.get("resume_classification", nested)


def _needs_you_outcome(run: Mapping[str, Any]) -> dict[str, str]:
    """A ``needs_you`` outcome whose label and ``action`` say what the person must do."""
    spec = RESUME_CLASSES.get(_resume_classification(run))
    action, label = (spec.needs_you_action, spec.needs_you_label) if spec else _STOPPED
    return {**_outcome(OUTCOME_NEEDS_YOU, label), "action": action}


def run_outcome(run: Mapping[str, Any]) -> dict[str, str]:
    """The outcome of a run as a ``kind`` and a ``label``: done, failed, needs you, active or stale.

    The label always names the outcome in words, so a badge never relies on its color.
    ``PR_READY`` is done only once the run is finished (``is_finished``). Until then it is
    active, as :func:`software_agent_factory.observability.build_monitoring_snapshot` counts it.
    A done run with ``needs_look`` reasons says ``Done, needs look``. A run that waits for a
    person also has an ``action``: ``waiting`` to approve or answer, ``retry`` to publish
    again, ``grant`` to grant one more attempt, ``accept`` to accept open review findings,
    ``stopped`` when only a command can help.
    Its label says the same in words.
    """
    state = _state(run)
    if state == WorkflowState.FAILED:
        return _outcome(OUTCOME_FAILED)
    if state == WorkflowState.NEEDS_HUMAN:
        return _needs_you_outcome(run)
    if state == WorkflowState.PR_READY and run.get("is_finished") is True:
        return _done_outcome(run, _PR_READY_LABEL)
    if state == WorkflowState.DONE:
        return _done_outcome(run)
    return _outcome(OUTCOME_STALE if run.get("is_stale") is True else OUTCOME_ACTIVE)


def stage_label(state: Any) -> str | None:
    """A workflow state in words, ``CI_RUNNING`` as ``CI running``, or ``None`` for no state."""
    if not isinstance(state, str) or not state:
        return None
    text = " ".join(word if word in _ACRONYMS else word.lower() for word in state.split("_"))
    return text[0].upper() + text[1:]


def run_stage(run: Mapping[str, Any], active_role: Any) -> str | None:
    """What an unfinished run is doing: its stage and the role of the call in progress.

    ``Reviewing · reviewer``: the role is in lower case. ``None`` once the run is done,
    failed or waits for a person.
    """
    if run_outcome(run)["kind"] not in (OUTCOME_ACTIVE, OUTCOME_STALE):
        return None
    stage = stage_label(_state(run))
    if stage is None or not isinstance(active_role, str) or not active_role:
        return stage
    return f"{stage} \u00b7 {active_role.lower()}"


def reason_line(run: Mapping[str, Any]) -> str | None:
    """Why a run stopped: its redacted, bounded failure reason, only while it is stopped."""
    reason = run.get("failure_reason")
    stopped = run_outcome(run)["kind"] in (OUTCOME_FAILED, OUTCOME_NEEDS_YOU)
    return reason if stopped and isinstance(reason, str) and reason else None


def headline(run: Mapping[str, Any], active_role: Any = None) -> dict[str, str]:
    """The one outcome line of the run page, as a ``kind`` and ``text``.

    A failed run says ``Failed: <reason>``. A run that waits for a person says what it needs
    and the reason it stopped, when it has one. An active run says its stage and the role of
    the call in progress (:func:`run_stage`). The other runs say their outcome in words.
    """
    outcome = run_outcome(run)
    kind, label = outcome["kind"], outcome["label"]
    reason = reason_line(run)
    if kind in (OUTCOME_FAILED, OUTCOME_NEEDS_YOU):
        return {"kind": kind, "text": f"{label}: {reason}" if reason else label}
    if (stage := run_stage(run, active_role)) is not None:
        return {"kind": kind, "text": f"In progress: {stage}"}
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


#: The USD cost units the week's cost can show, first choice first. They are never added.
WEEK_COST_UNITS: tuple[str, ...] = ("usage_value_usd", "list_price_estimate_usd")


def _sum_reported(days: Sequence[Mapping[str, Any]], field: str) -> int | float | None:
    values = [value for day in days if is_number(value := day.get(field))]
    return sum(values) if values else None


def _week_figures(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Calls, cost, tokens and premium requests over the last seven days, and the cost by day.

    The week's cost is in one unit: the AI usage value when a call reported it, else the
    list-price estimate. ``week_cost_by_day`` is that unit for each day, oldest first, with
    ``0`` for a day that reported none. Each unit has its own total; none is added to another.
    """
    raw_days = snapshot.get("daily_usage")
    raw_days = (
        [day for day in raw_days if isinstance(day, Mapping)] if isinstance(raw_days, list) else []
    )
    days = [sanitize_usage(day) for day in raw_days]
    unit = next((u for u in WEEK_COST_UNITS if _sum_reported(days, u) is not None), None)
    tokens = [total for day in days if (total := _total_tokens(day)) is not None]
    calls = [count for day in raw_days if is_count(count := day.get("invocation_count"))]
    return {
        "week_calls": sum(calls) if calls else None,
        "week_cost_unit": unit,
        "week_usage_value_usd": _sum_reported(days, "usage_value_usd"),
        "week_list_price_usd": _sum_reported(days, "list_price_estimate_usd"),
        "week_cost_by_day": [_reported(day.get(unit)) or 0 for day in days] if unit else [],
        "week_tokens": sum(tokens) if tokens else None,
        "week_premium_requests": _sum_reported(days, "premium_request_cost"),
    }


def _success_rate(succeeded: int, failed: int) -> float | None:
    finished = succeeded + failed
    return succeeded / finished if finished else None


def snapshot_overview(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The one-row summary of the run store: run counts, tokens and cost.

    A cost unit is ``None`` when no call reported it. The two units stay apart: the list-price
    estimate in US dollars and the premium request cost are never added together. The counts
    cover the scanned runs, and ``scan_truncated`` says when that is not every run.
    ``success_rate`` is the share of finished runs that succeeded; the ``week_`` figures
    come from :func:`_week_figures`.
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
        "success_rate": _success_rate(shown["succeeded"], shown["failed"]),
        **_week_figures(snapshot),
        "scan_truncated": snapshot.get("scan_truncated") is True,
    }
