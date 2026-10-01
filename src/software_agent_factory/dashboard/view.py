"""View models composed from sanitized data: run totals and the next step.

``sanitize_*`` only allowlists, redacts and validates. These functions run it
first, then add the figures the page shows. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..models import WorkflowState
from .aggregate import run_totals
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
