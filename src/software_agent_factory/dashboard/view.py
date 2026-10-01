"""View models composed from sanitized data: run totals and the next step.

``sanitize_*`` only allowlists, redacts and validates. These functions run it
first, then add the figures the page shows. Pure: no I/O.
"""

from __future__ import annotations

from typing import Any

from .aggregate import run_totals
from .next_step import next_step
from .sanitize import sanitize_project, sanitize_run_detail


def run_detail_view(raw: Any) -> dict[str, Any]:
    """A sanitized run detail with its ``totals`` and ``next_step``."""
    detail = sanitize_run_detail(raw)
    if "invocations" in detail:
        detail["totals"] = run_totals(detail["invocations"])
    detail["next_step"] = next_step(detail)
    return detail


def project_view(raw: Any) -> dict[str, Any]:
    """A sanitized project with its ``totals`` over the models it lists."""
    project = sanitize_project(raw)
    if "models" in project:
        project["totals"] = run_totals(project["models"])
    return project
