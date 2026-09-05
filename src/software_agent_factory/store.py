"""Filesystem run store: the factory's only source of truth.

Layout under the configured data directory:

```text
<data_dir>/runs/<run_id>/run.json          the FactoryRun itself
<data_dir>/runs/<run_id>/<artifact>.json   latest typed artifact snapshots
<data_dir>/runs/<run_id>/attempts/NN/      per-attempt snapshots + evidence
```

Two properties matter beyond plain persistence, because the Phase 15
read-only surfaces (``factory status``, ``factory show`` and the dashboard)
depend on them:

- **Reads never write.** Constructing a store, listing runs, loading a run,
  an artifact or a patch never creates a directory -- not even the data
  directory itself -- so observing a factory cannot alter what it observes.
  Only the explicit write paths (``save_*``, ``run_dir``, ``attempt_dir``)
  create anything.
- **Run ids are validated before any path is built**
  (:func:`validate_run_id`), so an id that arrived from an untrusted source
  (an HTTP path parameter, say) can never escape the runs directory.
"""

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
    RepositoryProfile,
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
    RepositoryProfile: "repository-profile.json",
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

#: A run_id is always used as exactly one filesystem path component (never
#: joined as a nested path), so this charset intentionally excludes ``/`` and
#: ``\`` outright rather than relying only on the ``.``/``..`` traversal
#: check below.
MAX_RUN_ID_LENGTH = 128
_RUN_ID_PATTERN = re.compile(rf"^[A-Za-z0-9._-]{{1,{MAX_RUN_ID_LENGTH}}}$")
_RESERVED_PATH_COMPONENTS = frozenset({".", ".."})


class InvalidRunIdError(ValueError):
    """Raised when a run_id fails safety validation before any filesystem
    access, so a hostile or malformed run_id (e.g. from an HTTP path
    parameter) can never reach ``Path`` construction."""


def validate_run_id(run_id: str) -> str:
    """Validate ``run_id`` is safe to use as a single filesystem path
    component and return it unchanged.

    Allowed: ASCII letters, digits, ``.``, ``_`` and ``-``, 1-128 characters.
    Rejected: empty strings, anything containing ``/`` or ``\\``, and the
    reserved traversal names ``.``/``..``. Raises :class:`InvalidRunIdError`
    (a ``ValueError`` subclass) instead of touching the filesystem, so every
    public :class:`FileRunStore` method can call this first and safely reject
    a hostile run_id (for instance one supplied verbatim by an HTTP client)
    before any path is resolved.

    Generated ids are ``run-<uuid4 hex>`` (``workflow.py``), which the
    dashboard's own, deliberately stricter allowlist
    (``dashboard.snapshot.is_valid_run_id``, which additionally forbids
    ``.``) also accepts. Widening the generated id format to include a ``.``
    would therefore need that allowlist widened too, or the dashboard's run
    detail route would answer 404 for a perfectly valid run.
    """
    if not isinstance(run_id, str) or not _RUN_ID_PATTERN.match(run_id):
        raise InvalidRunIdError(
            "run_id must be 1-128 ASCII letters, digits, '.', '_' or '-' characters; "
            f"got {run_id!r}"
        )
    if run_id in _RESERVED_PATH_COMPONENTS:
        raise InvalidRunIdError(f"run_id must not be a path traversal token: {run_id!r}")
    return run_id


class FileRunStore:
    def __init__(self, data_dir: str | Path):
        self._data_dir = Path(data_dir).expanduser()
        self._runs_dir = self._data_dir / "runs"
        # Deliberately lazy: constructing a store must never mutate disk, so
        # a purely read-only caller (e.g. the Phase 15.11 dashboard, or the
        # Phase 15.5 metrics scan) can point at a data directory that does
        # not exist yet without creating it. Every write path below creates
        # its own parents (``mkdir(parents=True, ...)``) on demand instead.

    @property
    def runs_dir(self) -> Path:
        return self._runs_dir

    def run_dir(self, run_id: str) -> Path:
        """Return (creating if needed) the directory holding one run's state.

        Public because deterministic governance evidence (``RepositoryVerifier``
        command logs) is written beside the run's artifacts. This is a write
        path: it creates the run directory. Use :meth:`load_run` or
        :meth:`list_runs` for read-only access that must never create one.
        """
        return self._run_dir_for_write(run_id)

    def save_run(self, run: FactoryRun) -> Path:
        destination = self._run_dir_for_write(run.id) / "run.json"
        self._write_model(destination, run)
        return destination

    def load_run(self, run_id: str) -> FactoryRun:
        """Load a persisted run. Read-only: a missing run raises
        ``FileNotFoundError`` and never creates the run directory."""
        path = self._run_dir_readonly(run_id) / "run.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_version = payload.get("schema_version")
        if schema_version != 1:
            raise ValueError(f"Unsupported FactoryRun schema_version: {schema_version}")
        return FactoryRun.model_validate(payload)

    def list_runs(self) -> list[FactoryRun]:
        """List every persisted run. Read-only: never creates a run or
        attempt directory."""
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
        if attempt is not None:
            # Validate before any write: an invalid attempt must never leave
            # the top-level latest snapshot partially updated.
            self._attempt_key(attempt)
        destination = self._artifact_path(run_id, type(artifact), filename, create=True)
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
        """Load a persisted artifact snapshot. Read-only: a missing artifact
        or run raises ``FileNotFoundError`` and never creates the run or
        attempt directory."""
        path = self._artifact_path(run_id, artifact_type, filename, attempt=attempt, create=False)
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
        if attempt is not None:
            # Validate before any write: an invalid attempt must never leave
            # the top-level latest snapshot partially updated.
            self._attempt_key(attempt)
        safe_name = self._validated_filename(filename)
        destination = self._run_dir_for_write(run_id) / safe_name
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
        """Load a persisted patch. Read-only: a missing patch or run raises
        ``FileNotFoundError`` and never creates the run or attempt
        directory."""
        safe_name = self._validated_filename(filename)
        if attempt is None:
            path = self._run_dir_readonly(run_id) / safe_name
        else:
            path = self._attempt_dir_readonly(run_id, attempt) / safe_name
        return path.read_text(encoding="utf-8")

    def attempt_dir(self, run_id: str, attempt: int) -> Path:
        """Return (creating if needed) the snapshot directory for ``attempt``.

        Write path: use it only to persist a new attempt snapshot. Reading an
        existing attempt snapshot goes through :meth:`load_artifact` /
        :meth:`load_patch`, which never create a directory for a missing
        attempt.
        """
        # Validate before creating anything: an invalid attempt must not
        # create the run directory as a side effect of evaluating the path.
        attempt_key = self._attempt_key(attempt)
        attempt_dir = self._run_dir_for_write(run_id) / ATTEMPTS_DIRNAME / attempt_key
        attempt_dir.mkdir(parents=True, exist_ok=True)
        return attempt_dir

    def list_attempts(self, run_id: str) -> list[int]:
        """Return the attempt numbers that have persisted snapshots.
        Read-only: never creates a run or attempt directory."""
        attempts_root = self._run_dir_readonly(run_id) / ATTEMPTS_DIRNAME
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

    def _run_dir_for_write(self, run_id: str) -> Path:
        run_dir = self._runs_dir / validate_run_id(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _run_dir_readonly(self, run_id: str) -> Path:
        """Validate ``run_id`` and return its directory path without ever
        creating it (or any parent), so loading a missing run leaves no
        filesystem artifacts behind."""
        return self._runs_dir / validate_run_id(run_id)

    def _attempt_dir_readonly(self, run_id: str, attempt: int) -> Path:
        return self._run_dir_readonly(run_id) / ATTEMPTS_DIRNAME / self._attempt_key(attempt)

    def _artifact_path(
        self,
        run_id: str,
        artifact_type: type[VersionedModel],
        filename: str | None,
        *,
        attempt: int | None = None,
        create: bool,
    ) -> Path:
        artifact_name = self._artifact_filename(artifact_type, filename)
        if attempt is None:
            run_dir = self._run_dir_for_write(run_id) if create else self._run_dir_readonly(run_id)
            return run_dir / artifact_name
        attempt_dir = (
            self.attempt_dir(run_id, attempt)
            if create
            else self._attempt_dir_readonly(run_id, attempt)
        )
        return attempt_dir / artifact_name

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
        if (
            not isinstance(filename, str)
            or not filename
            or "\x00" in filename
            or "/" in filename
            or "\\" in filename
            or filename in _RESERVED_PATH_COMPONENTS  # rejects "." and ".."
        ):
            raise ValueError(
                "filename must be a simple relative file name (no '/', '\\', NUL byte, "
                f"'.', or '..'); got {filename!r}"
            )
        candidate = Path(filename)
        if candidate.is_absolute() or candidate.name != filename:
            raise ValueError(f"filename must be a simple relative file name; got {filename!r}")
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
