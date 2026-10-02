"""View models composed from sanitized data: run totals, the next step and the overview figures.

``sanitize_*`` only allowlists, redacts and validates. These functions run it
first, then add the figures the page shows. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..models import WorkflowState
from .aggregate import compare_roles, models_of, models_summary, run_totals
from .next_step import next_step
from .overview import flag_failing_call, headline, reason_line, run_outcome, snapshot_overview
from .sanitize import (
    is_active_status,
    sanitize_project,
    sanitize_run_detail,
    sanitize_run_summary,
)
from .validators import is_episode_id, run_id_of

RequestsFor = Callable[[str, str], Iterable[Any]]


def _queued_requests(detail: dict[str, Any], requests_for: RequestsFor | None) -> Iterable[Any]:
    """The dashboard requests of a waiting run's current episode, or none."""
    escalation = detail.get("escalation")
    run_id = run_id_of(detail)
    if (
        requests_for is None
        or detail.get("state") != WorkflowState.NEEDS_HUMAN
        or run_id is None
        or not isinstance(escalation, dict)
        or not is_episode_id(escalation.get("episode_id"))
    ):
        return ()
    return requests_for(run_id, escalation["episode_id"])


def run_detail_view(raw: Any, requests_for: RequestsFor | None = None) -> dict[str, Any]:
    """A sanitized run detail with its ``totals``, ``next_step``, ``outcome`` and ``headline``.

    ``requests_for`` supplies the dashboard requests of the run's current episode. Only a
    waiting run asks for them. The finished call a failed run most likely failed on carries
    ``failure_link`` (see :func:`.overview.flag_failing_call`).
    """
    detail = sanitize_run_detail(raw)
    if "invocations" in detail:
        detail["totals"] = run_totals(detail["invocations"])
        flag_failing_call(detail, detail["invocations"])
    detail["next_step"] = next_step(detail, _queued_requests(detail, requests_for))
    detail["outcome"] = run_outcome(detail)
    detail["headline"] = headline(detail)
    return detail


def _duration_ms(calls: list[dict[str, Any]]) -> int | None:
    """The total length of the calls, or ``None`` when no call reported one."""
    lengths = [ms for call in calls if (ms := call.get("duration_ms")) is not None]
    return sum(lengths) if lengths else None


def run_summary_view(raw: Any) -> dict[str, Any]:
    """A sanitized run list row with what the list shows: outcome, reason, models and length.

    ``calls`` is replaced by ``models`` (``text`` for the cell, ``detail`` for its tooltip)
    and ``duration_ms``. ``why`` is the redacted, bounded failure reason of a stopped run.
    """
    run = sanitize_run_summary(raw)
    calls = run.pop("calls", [])
    run["outcome"] = run_outcome(run)
    run["why"] = reason_line(run)
    run["models"] = models_summary(calls)
    run["duration_ms"] = _duration_ms(calls)
    return run


def summary_view(snapshot: Any) -> dict[str, Any]:
    """The snapshot without its run page, plus the ``overview`` row the Runs page shows."""
    payload = {
        key: value
        for key, value in (snapshot if isinstance(snapshot, dict) else {}).items()
        if key not in ("runs", "page")
    }
    payload["overview"] = snapshot_overview(payload)
    return payload


def project_view(raw: Any) -> dict[str, Any]:
    """A sanitized project with its ``totals`` over the finished calls in the models it lists.

    The active calls stay in ``models`` for the page, but they have no usage yet, so
    the totals leave them out.
    """
    project = sanitize_project(raw)
    if "models" in project:
        finished = [call for call in project["models"] if not is_active_status(call.get("status"))]
        project["totals"] = run_totals(finished)
    return project


def _run_header(run_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    """What the picker shows for one run: its start time, state, task and models."""
    return {
        "run_id": run_id,
        "created_at": detail.get("created_at"),
        "state": detail.get("state"),
        "title": detail.get("title"),
        "models": models_of(detail.get("invocations", ())),
    }


def compare_view(
    a_id: str, a_detail: dict[str, Any], b_id: str, b_detail: dict[str, Any]
) -> dict[str, Any]:
    """Two sanitized run details side by side: a header per run and the roles of both.

    ``a_detail`` and ``b_detail`` are the output of :func:`sanitize_run_detail`.
    """
    return {
        "a": _run_header(a_id, a_detail),
        "b": _run_header(b_id, b_detail),
        "roles": compare_roles(a_detail.get("invocations", ()), b_detail.get("invocations", ())),
    }
