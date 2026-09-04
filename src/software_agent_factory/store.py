from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from .models import (
    ChangeSet,
    CIReport,
    ExecutionPlan,
    FactoryRun,
    ResearchReport,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    VersionedModel,
    WorkItem,
)

ArtifactModel = TypeVar("ArtifactModel", bound=VersionedModel)

ARTIFACT_FILENAMES: dict[type[VersionedModel], str] = {
    WorkItem: "work-item.json",
    TriageResult: "triage.json",
    Specification: "specification.json",
    ResearchReport: "research.json",
    ExecutionPlan: "execution-plan.json",
    ChangeSet: "change-set.json",
    VerificationReport: "verification.json",
    TestReport: "test-report.json",
    ReviewReport: "review.json",
    CIReport: "ci.json",
}

ATTEMPTS_DIRNAME = "attempts"
_ATTEMPT_DIR_PATTERN = re.compile(r"^\d+$")


class FileRunStore:
    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir).expanduser()
        self._runs_dir = self._data_dir / "runs"
        self._runs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def run_dir(self, run_id: str) -> Path:
        """Return (creating if needed) the directory holding one run's state.

        Public because deterministic governance evidence (``RepositoryVerifier``
        command logs) is written beside the run's artifacts.
        """
        return self._run_dir(run_id)

    def save_run(self, run: FactoryRun) -> Path:
        destination = self._run_dir(run.id) / "run.json"
        self._write_model(destination, run)
        return destination

    def load_run(self, run_id: str) -> FactoryRun:
        path = self._run_dir(run_id) / "run.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = payload.get("schema_version")
        if schema_version != 1:
            raise ValueError(f"Unsupported FactoryRun schema_version: {schema_version}")
        return FactoryRun.model_validate(payload)

    def list_runs(self) -> list[FactoryRun]:
        runs = [self.load_run(path.parent.name) for path in self._runs_dir.glob("*/run.json")]
        return sorted(runs, key=lambda run: (run.created_at, run.id))

    def save_artifact(
        self,
        run_id: str,
        artifact: ArtifactModel,
        filename: str | None = None,
        *,
        attempt: int | None = None,
    ) -> Path:
        """Persist ``artifact`` as the run's latest snapshot.

        When ``attempt`` is supplied an immutable per-attempt copy is also
        written under ``attempts/NN/`` so a bounded repair history can be
        inspected afterwards. ``NN`` is the run-global attempt index (the
        position in ``FactoryRun.attempt_records``), not a per-budget attempt
        number, so a post-PR CI repair can never overwrite the pre-PR
        attempt's evidence. The returned path is always the top-level latest
        snapshot, so existing callers are unaffected.
        """
        destination = self._artifact_path(run_id, type(artifact), filename)
        self._write_model(destination, artifact)
        if attempt is not None:
            self._write_model(
                self.attempt_dir(run_id, attempt) / destination.name,
                artifact,
            )
        return destination

    def load_artifact(
        self,
        run_id: str,
        artifact_type: type[ArtifactModel],
        filename: str | None = None,
        *,
        attempt: int | None = None,
    ) -> ArtifactModel:
        path = self._artifact_path(run_id, artifact_type, filename, attempt=attempt)
        return artifact_type.model_validate_json(path.read_text(encoding="utf-8"))

    def save_patch(
        self,
        run_id: str,
        patch_text: str,
        filename: str = "patch.diff",
        *,
        attempt: int | None = None,
    ) -> Path:
        """Persist the controller-derived patch as the run's latest snapshot
        (and, with ``attempt``, an additional per-attempt copy)."""
        safe_name = self._validated_filename(filename)
        destination = self._run_dir(run_id) / safe_name
        self._write_text_atomic(destination, patch_text)
        if attempt is not None:
            self._write_text_atomic(self.attempt_dir(run_id, attempt) / safe_name, patch_text)
        return destination

    def load_patch(
        self,
        run_id: str,
        filename: str = "patch.diff",
        *,
        attempt: int | None = None,
    ) -> str:
        safe_name = self._validated_filename(filename)
        if attempt is None:
            path = self._run_dir(run_id) / safe_name
        else:
            path = self.attempt_dir(run_id, attempt) / safe_name
        return path.read_text(encoding="utf-8")

    def attempt_dir(self, run_id: str, attempt: int) -> Path:
        """Return (creating if needed) the snapshot directory for ``attempt``."""
        attempt_dir = self._run_dir(run_id) / ATTEMPTS_DIRNAME / self._attempt_key(attempt)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def list_attempts(self, run_id: str) -> list[int]:
        """Return the attempt numbers that have persisted snapshots."""
        attempts_root = self._runs_dir / run_id / ATTEMPTS_DIRNAME
        if not attempts_root.is_dir():
            return []
        return sorted(
            int(child.name)
            for child in attempts_root.iterdir()
            if child.is_dir() and _ATTEMPT_DIR_PATTERN.match(child.name)
        )

    @staticmethod
    def _attempt_key(attempt: int) -> str:
        if attempt < 1:
            raise ValueError("attempt must be 1 or greater")
        return f"{attempt:02d}"

    def _run_dir(self, run_id: str) -> Path:
        if not run_id:
            raise ValueError("run_id must not be empty")
        run_dir = self._runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _artifact_path(
        self,
        run_id: str,
        artifact_type: type[VersionedModel],
        filename: str | None,
        *,
        attempt: int | None = None,
    ) -> Path:
        artifact_name = self._artifact_filename(artifact_type, filename)
        if attempt is None:
            return self._run_dir(run_id) / artifact_name
        return self.attempt_dir(run_id, attempt) / artifact_name

    def _artifact_filename(
        self,
        artifact_type: type[VersionedModel],
        filename: str | None,
    ) -> str:
        if filename is not None:
            return self._validated_filename(filename)

        try:
            return ARTIFACT_FILENAMES[artifact_type]
        except KeyError as exc:
            raise ValueError(
                f"No default filename is registered for artifact type {artifact_type.__name__}"
            ) from exc

    def _validated_filename(self, filename: str) -> str:
        candidate = Path(filename)
        if not filename or candidate.is_absolute() or candidate.name != filename:
            raise ValueError("filename must be a simple relative file name")
        return filename

    def _write_model(self, destination: Path, model: VersionedModel) -> None:
        self._write_text_atomic(destination, f"{model.model_dump_json(indent=2)}\n")

    def _write_text_atomic(self, destination: Path, content: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        try:
            os.replace(temp_path, destination)
        except OSError:
            if temp_path.exists():
                temp_path.unlink()
            raise
