"""View models composed from sanitized data: run totals and the next step.

``sanitize_*`` only allowlists, redacts and validates. These functions run it
first, then add the figures the page shows. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..models import WorkflowState
from .aggregate import compare_roles, models_of, run_totals
from .next_step import next_step
from .sanitize import sanitize_project, sanitize_run_detail
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
    """A sanitized run detail with its ``totals`` and ``next_step``.

    ``requests_for`` supplies the dashboard requests of the run's current episode. Only a
    waiting run asks for them.
    """
    detail = sanitize_run_detail(raw)
    if "invocations" in detail:
        detail["totals"] = run_totals(detail["invocations"])
    detail["next_step"] = next_step(detail, _queued_requests(detail, requests_for))
    return detail


def project_view(raw: Any) -> dict[str, Any]:
    """A sanitized project with its ``totals`` over the models it lists."""
    project = sanitize_project(raw)
    if "models" in project:
        project["totals"] = run_totals(project["models"])
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
