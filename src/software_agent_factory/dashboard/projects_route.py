"""The ``GET /api/projects`` and ``GET /api/projects/<id>`` routes' data (ADR-078).

The list sends each project's rollup: its fields, tasks, ``totals`` over its finished calls,
``active_calls`` and ``last_activity_at``. It leaves out the ``models`` table, the bulk of a
project; the page reads it from ``/api/projects/<id>`` only while a project's table is open.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import PROJECT_ID_PATTERN
from .sanitize import is_active_status
from .snapshot import ProjectDetailProvider, ProjectProvider, to_json_safe
from .transitions import parse_timestamp
from .view import project_view

#: ``/api/projects/<id>``: one project with its ``models``.
PROJECT_PATH_PATTERN = re.compile(r"^/api/projects/([^/]+)$")
_PROJECT_ID = re.compile(PROJECT_ID_PATTERN)


def _last_activity(project: dict[str, Any], models: list[dict[str, Any]]) -> str | None:
    """The latest of the project's ``updated_at`` and its calls' start and end times."""
    times = [project.get("updated_at")]
    times += [call.get(key) for call in models for key in ("started_at", "completed_at")]
    parsed = [(when, text) for text in times if (when := parse_timestamp(text)) is not None]
    return max(parsed)[1] if parsed else None


def project_rollup(raw: Any) -> dict[str, Any]:
    """:func:`project_view` without ``models``, plus its active call count and last activity."""
    project = project_view(raw)
    models = project.pop("models", None)
    if isinstance(models, list):
        project["active_calls"] = sum(1 for call in models if is_active_status(call.get("status")))
        project["last_activity_at"] = _last_activity(project, models)
    return project


def projects_body(provider: ProjectProvider | None) -> dict[str, Any]:
    """Every project's rollup. Raises when the provider fails or sends a bad project."""
    if provider is None:
        return {"projects": []}
    payload = to_json_safe(provider())
    raw_projects = payload.get("projects", []) if isinstance(payload, dict) else []
    if not isinstance(raw_projects, list):
        return {"projects": []}
    return {"projects": [project_rollup(project) for project in raw_projects]}


def project_body(provider: ProjectDetailProvider | None, project_id: str) -> dict[str, Any] | None:
    """One project with its ``models`` and ``totals``, or ``None`` for an unknown project."""
    if provider is None or not _PROJECT_ID.fullmatch(project_id) or project_id in {".", ".."}:
        return None
    raw = to_json_safe(provider(project_id))
    return None if raw is None else project_view(raw)
