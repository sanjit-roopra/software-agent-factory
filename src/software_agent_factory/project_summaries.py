"""The dashboard's project reads: every project for ``/api/projects``, one for its detail.

Task runs come from the dashboard's shared run scan cache, so a poll parses no ``run.json``
the run list already parsed. A task run the scan does not hold (older than the scan cap) is
read from the store. Read only; never workflow authority.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import timedelta
    from pathlib import Path

    from .models import FactoryRun, InvocationRecord
    from .projects import FileProjectStore
    from .run_scan_cache import RunScanCache
    from .store import FileRunStore

__all__ = ["ProjectSummaries"]

_READ_ERRORS = (FileNotFoundError, OSError, ValueError)


def _invocation_row(invocation: InvocationRecord) -> dict[str, object]:
    # Resolve per-model usage so the project totals count it like a run's.
    from .run_metrics import resolve_usage

    row = invocation.model_dump(mode="json")
    if invocation.usage is not None:
        row["usage"] = resolve_usage(invocation.usage).model_dump(mode="json")
    return row


class ProjectSummaries:
    """Project summaries with their tasks and the calls of the project and its task runs."""

    def __init__(
        self,
        data_dir: Path,
        store: FileRunStore,
        scan_cache: RunScanCache,
        *,
        stale_after: timedelta,
        max_scanned_runs: int,
    ) -> None:
        from .projects import FileProjectStore

        self._projects_dir = data_dir / "projects"
        self._project_store: FileProjectStore = FileProjectStore(data_dir)
        self._store = store
        self._scan_cache = scan_cache
        self._stale_after = stale_after
        self._max_scanned_runs = max_scanned_runs

    def list(self) -> dict[str, object]:
        """The newest ``max_scanned_runs`` projects, most recently changed first."""
        if not self._projects_dir.is_dir():
            return {"projects": []}
        execution_paths = sorted(
            self._projects_dir.glob("*/execution.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[: self._max_scanned_runs]
        runs = self._scanned_runs()
        projects = (self._build(path.parent.name, runs) for path in execution_paths)
        return {"projects": [project for project in projects if project is not None]}

    def get(self, project_id: str) -> dict[str, object] | None:
        """One project, or ``None`` when it is missing, unreadable or the id is not valid."""
        try:
            if not self._project_store.exists(project_id):
                return None
        except ValueError:
            return None
        return self._build(project_id, self._scanned_runs())

    def _scanned_runs(self) -> dict[str, FactoryRun]:
        scan = self._scan_cache.get_scan(self._max_scanned_runs)
        return {run.id: run for run in scan.readable_runs}

    def _load_run(self, run_id: str, runs: dict[str, FactoryRun]) -> FactoryRun | None:
        run = runs.get(run_id)
        if run is not None:
            return run
        try:
            return self._store.load_run(run_id)
        except _READ_ERRORS:
            return None

    def _build(self, project_id: str, runs: dict[str, FactoryRun]) -> dict[str, object] | None:
        from .observability import build_active_invocation_summary

        try:
            execution = self._project_store.load_execution(project_id)
        except _READ_ERRORS:
            return None
        try:
            plan = self._project_store.load_plan(project_id)
            titles = {task.id: task.title for task in plan.tasks}
        except _READ_ERRORS:
            titles = {}
        data = execution.model_dump(mode="json")
        tasks = [
            {**task.model_dump(mode="json"), "title": titles.get(task.task_id)}
            for task in execution.tasks
        ]
        models: list[dict[str, object]] = [
            {**_invocation_row(invocation), "scope": "project", "task_id": None}
            for invocation in execution.invocation_records
        ]
        for task in execution.tasks:
            run = None if task.run_id is None else self._load_run(task.run_id, runs)
            if run is None:
                continue
            scope = {"scope": f"task {task.task_id}", "task_id": task.task_id}
            models.extend({**_invocation_row(call), **scope} for call in run.invocation_records)
            active = build_active_invocation_summary(run, stale_after=self._stale_after)
            if active is not None:
                models.append(
                    {
                        **active.model_dump(mode="json"),
                        **scope,
                        "success": None,
                        "completed_at": None,
                        "usage": None,
                    }
                )
        fields = (
            "project_id",
            "state",
            "delivery_mode",
            "delivery_repository",
            "delivery_base_branch",
            "integration_branch",
            "created_at",
            "updated_at",
            "completed_at",
            "needs_look",
        )
        return {
            **{field: data[field] for field in fields},
            "task_count": len(tasks),
            "tasks": tasks,
            "models": models,
        }
