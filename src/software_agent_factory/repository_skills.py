"""Repository-scoped persistence for generated skills and the human overlay.

Layout under the configured data directory:

```text
<data_dir>/repository-skills/v1/<repository-key>/repository.json
<data_dir>/repository-skills/v1/<repository-key>/repository-skill-overlay.yaml
<data_dir>/repository-skills/v1/<repository-key>/generated/<dependency-fingerprint>.json
```

Three rules shape this module.

**The machine owns generated evidence; a human owns prose.** A
:class:`~software_agent_factory.models.RepositorySkill` stays strict,
source-grounded and bound to one dependency fingerprint. A
:class:`~software_agent_factory.models.RepositorySkillOverlay` is repository
scoped, carries guidance prose only, and cannot claim targets, sources,
fingerprints or generator provenance. When dependencies change, a *different*
generated file is selected while the same overlay keeps applying.

**The factory never writes the overlay.** ``repository-skill-overlay.yaml``
is read, never created, normalized, reformatted or deleted. A missing overlay
is normal. An unusable one (unsafe, oversized, malformed, schema-invalid, or
impossible to merge within the effective model's bounds) never blocks a run
and is never silently dropped either: :meth:`RepositorySkillManager.select`
and :meth:`RepositorySkillManager.reuse` fall back to the generated guidance
alone and report a typed, actionable :class:`RepositorySkillOverlayError` or
:class:`RepositorySkillMergeError` plus a warning, with the human's bytes left
exactly as written. ``read_overlay().require()`` stays available for callers
that want overlay problems to raise (an explicit validate command, say).

**Reads never write.** Constructing a manager, resolving paths, listing or
loading a generated skill and reading the overlay never create a directory --
not even the data directory. Only :meth:`RepositorySkillManager.create_generated`,
:meth:`RepositorySkillManager.refresh_generated` and
:meth:`RepositorySkillManager.select` create anything, and normal creation is
no-clobber: an existing generated file is never overwritten except through
the explicit :meth:`RepositorySkillManager.refresh_generated` API.

Repository identity is the canonical local Git *common* directory, so the
main checkout and every linked worktree of the same repository share one
skill directory. No remote URL is consulted, nothing is written inside the
repository or a worktree, and there is no TTL: a stored skill stays valid
until its dependency fingerprint stops matching or it is explicitly
refreshed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import ConfigDict, Field, ValidationError

from .models import (
    GENERIC_PRACTICE_VERSION_SCOPE,
    GENERIC_SKILL_TARGET,
    REPOSITORY_KEY_PATTERN,
    REQUIRED_SKILL_TARGET_NAMES,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillOverlay,
    RepositorySkillUse,
    SkillGuidance,
    SkillOverlayMode,
    SkillSelectionSource,
    UtcDateTime,
    VersionedModel,
    utc_now,
)

STORAGE_ROOT_DIRNAME = "repository-skills"
STORAGE_LAYOUT_VERSION = "v1"
GENERATED_DIRNAME = "generated"
OVERLAY_FILENAME = "repository-skill-overlay.yaml"
METADATA_FILENAME = "repository.json"

#: Bounded reads: a stored skill or a hand-written overlay that exceeds these
#: sizes is rejected instead of being parsed, so a huge or hostile file can
#: never be loaded into memory wholesale.
MAX_GENERATED_SKILL_BYTES = 262_144
MAX_OVERLAY_BYTES = 65_536
MAX_METADATA_BYTES = 16_384

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_KEY_RE = re.compile(REPOSITORY_KEY_PATTERN)
_SANITIZE_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_SANITIZE_COLLAPSE = re.compile(r"-{2,}")
_MAX_LABEL_LENGTH = 60
_KEY_DIGEST_LENGTH = 16
_GIT_TIMEOUT_SECONDS = 30
_GUIDANCE_LIST_FIELDS: tuple[str, ...] = ("guidance", "avoid", "validation")


class RepositorySkillError(Exception):
    """Base error for repository-skill persistence."""


class RepositorySkillStorageError(RepositorySkillError):
    """Raised when stored skill state is unusable or unsafe to read/write."""


class RepositorySkillMergeError(RepositorySkillError):
    """Raised when generated guidance and an overlay cannot be combined."""


class RepositorySkillOverlayError(RepositorySkillError):
    """Raised when the human-owned overlay file cannot be used.

    Carries the offending path and one problem string per schema violation so
    the message tells a human exactly what to fix. The factory never repairs
    or rewrites the file itself.
    """

    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        self.path = Path(path)
        self.problems: tuple[str, ...] = tuple(problems)
        detail = "; ".join(self.problems) if self.problems else "unknown problem"
        super().__init__(
            f"invalid repository skill overlay at {self.path}: {detail}. "
            "The factory only reads this file; edit it by hand or remove it."
        )


class RepositoryMetadata(VersionedModel):
    """Storage-local note describing which repository a skill directory serves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_key: str = Field(pattern=REPOSITORY_KEY_PATTERN)
    git_common_dir: str = Field(min_length=1, max_length=4096)
    created_at: UtcDateTime = Field(default_factory=utc_now)


@dataclass(frozen=True)
class RepositoryIdentity:
    """Canonical local identity of one source repository.

    ``git_common_dir`` is what ``git rev-parse --git-common-dir`` reports, so
    a linked worktree resolves to the same value as its main checkout and
    both share ``key``.
    """

    key: str
    git_common_dir: Path


@dataclass(frozen=True)
class OverlayRead:
    """Outcome of reading the human-owned overlay file.

    ``present`` distinguishes "no overlay, which is fine" from "an overlay
    exists but is unusable". ``raw_text`` is the human's bytes exactly as
    decoded (``None`` when they could not be read at all), because the
    factory reports overlay problems without ever rewriting the file.
    """

    path: Path
    present: bool
    overlay: RepositorySkillOverlay | None = None
    error: RepositorySkillOverlayError | None = None
    raw_text: str | None = None

    @property
    def valid(self) -> bool:
        return self.error is None

    def require(self) -> RepositorySkillOverlay | None:
        """Return the parsed overlay (``None`` when absent), raising on error."""
        if self.error is not None:
            raise self.error
        return self.overlay


@dataclass(frozen=True)
class GeneratedSkillRecord:
    """A generated skill as it exists on disk after a create/refresh call."""

    path: Path
    skill: RepositorySkill
    created: bool


@dataclass(frozen=True)
class RepositorySkillSelection:
    """The skill a run should use, plus the audit record explaining it.

    A run is never blocked by a human's overlay. When the overlay cannot be
    read, cannot be parsed, or cannot be merged within the bounds a
    :class:`RepositorySkill` allows, ``effective_skill`` falls back to the
    generated guidance alone and the problem is reported here: as
    ``overlay_error`` (typed and actionable) and as a ``warnings`` entry the
    caller can surface. The overlay's bytes are left exactly as written.

    ``overlay`` is the *parsed* overlay when one could be parsed, which is not
    the same as "was applied": a valid overlay whose merge would exceed the
    effective model's bounds is reported instead of applied. Use
    :attr:`overlay_applied` (or ``use.overlay_applied``) for that question.
    """

    effective_skill: RepositorySkill
    generated_skill: RepositorySkill
    overlay: RepositorySkillOverlay | None
    use: RepositorySkillUse
    generated_path: Path
    overlay_error: RepositorySkillError | None = None
    warnings: tuple[str, ...] = ()

    @property
    def overlay_applied(self) -> bool:
        return self.use.overlay_applied


# -- hashing ----------------------------------------------------------------


def content_hash(model: VersionedModel) -> str:
    """Return the SHA-256 of a canonical JSON rendering of ``model``.

    Canonical means sorted keys and no incidental whitespace, so the digest
    depends on content only -- never on field ordering or formatting.
    """
    payload = model.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text_content_hash(canonical)


def text_content_hash(text: str) -> str:
    """Return the SHA-256 of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- repository identity ----------------------------------------------------


def resolve_repository_identity(repository_path: str | Path) -> RepositoryIdentity:
    """Resolve the canonical local Git identity of ``repository_path``.

    Uses the Git *common* directory, so the main checkout and any linked
    worktree of the same repository share one identity. Remote URLs are never
    consulted: identity is a local fact, and a repository with no remote (or
    several) must still work.
    """
    path = Path(repository_path).expanduser()
    if not path.is_dir():
        raise RepositorySkillError(f"repository path is not a directory: {path}")
    common_dir = _git_common_dir(path)
    return RepositoryIdentity(key=repository_key(common_dir), git_common_dir=common_dir)


def repository_key(git_common_dir: Path) -> str:
    """Derive a safe, collision-resistant path component for a repository.

    The key is a readable label plus a stable hash of the canonical common
    directory, so distinct repositories with the same directory name never
    share storage and the component is always safe to join as a single path
    segment.
    """
    canonical = str(git_common_dir)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_KEY_DIGEST_LENGTH]
    source = git_common_dir.parent.name if git_common_dir.name == ".git" else git_common_dir.name
    label = _SANITIZE_COLLAPSE.sub("-", _SANITIZE_DISALLOWED.sub("-", source))
    label = label.strip("-._")[:_MAX_LABEL_LENGTH].strip("-._")
    if not label or not label[0].isalnum():
        label = "repository"
    key = f"{label}-{digest}"
    return validate_repository_key(key)


def validate_repository_key(key: str) -> str:
    """Validate ``key`` is safe as exactly one filesystem path component."""
    if not isinstance(key, str) or not _REPOSITORY_KEY_RE.match(key) or key in {".", ".."}:
        raise RepositorySkillError(
            "repository key must be 1-128 ASCII letters, digits, '.', '_' or '-' characters "
            f"starting with a letter or digit; got {key!r}"
        )
    return key


def validate_dependency_fingerprint(fingerprint: str) -> str:
    """Validate a dependency fingerprint before it is used in a path."""
    if not isinstance(fingerprint, str) or not _FINGERPRINT_PATTERN.match(fingerprint):
        raise RepositorySkillError(
            f"dependency fingerprint must be 64 lowercase hex characters; got {fingerprint!r}"
        )
    return fingerprint


def _git_common_dir(path: Path) -> Path:
    attempts = (
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        ("rev-parse", "--git-common-dir"),
    )
    for args in attempts:
        completed = _run_git(path, args)
        if completed.returncode != 0:
            continue
        raw = completed.stdout.strip()
        if not raw:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            # ``git -C <path>`` runs with ``path`` as its working directory,
            # so a relative common dir is relative to ``path``.
            candidate = path / candidate
        return candidate.resolve()
    raise RepositorySkillError(f"{path} is not inside a Git repository")


def _run_git(cwd: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositorySkillError(f"git {' '.join(args)} failed in {cwd}: {exc}") from exc


# -- generated provenance validation -----------------------------------------


def repository_skill_validation_error(
    skill: RepositorySkill,
    profile: RepositoryProfile,
    *,
    official_documentation_origins: Sequence[str],
    practice_reference_urls: Sequence[str],
) -> str | None:
    """Return why ``skill`` is not grounded in ``profile``, or ``None``.

    The single deterministic provenance check for machine-generated
    guidance, shared by the workflow controller and any command that
    refreshes or validates a stored skill, so both enforce exactly the same
    rules. It compares an agent's claims against repository-derived evidence
    only:

    - every target must match a detected dependency version exactly, and cite
      only evidence paths the profile actually recorded;
    - every recognized dependency the profile detected must be targeted;
    - every official source must sit on a configured documentation origin and
      may only claim dependencies the profile detected;
    - every target must be grounded by at least one official source;
    - every practice source must be an exactly configured reference URL,
      scoped to the generic repository target, carrying no version claim.

    Advisory only in the sense that the caller decides what to do with the
    message; it never mutates the skill and never softens a rule.
    """
    if skill.dependency_fingerprint != profile.dependency_fingerprint:
        return "researcher returned repository guidance for a different dependency fingerprint"

    dependencies = {
        (
            dependency.ecosystem,
            dependency.name,
            dependency.declared_version,
            dependency.resolved_version,
        )
        for dependency in profile.dependencies
    }
    evidence_paths = set(profile.version_files)
    evidence_paths.update(dependency.manifest_path for dependency in profile.dependencies)
    evidence_paths.update(
        dependency.resolution_path
        for dependency in profile.dependencies
        if dependency.resolution_path is not None
    )
    for target in skill.targets:
        identity = (
            target.ecosystem,
            target.name,
            target.declared_version,
            target.resolved_version,
        )
        if identity not in dependencies:
            return (
                "researcher returned repository guidance for an unverified dependency "
                f"version: {target.ecosystem}:{target.name}"
            )
        if not set(target.evidence).issubset(evidence_paths):
            return (
                "researcher returned repository guidance with unverified version evidence "
                f"for {target.ecosystem}:{target.name}"
            )

    required_dependencies = {
        (
            dependency.ecosystem,
            dependency.name,
            dependency.declared_version,
            dependency.resolved_version,
        )
        for dependency in profile.dependencies
        if dependency.name in REQUIRED_SKILL_TARGET_NAMES
    }
    target_identities = {
        (
            target.ecosystem,
            target.name,
            target.declared_version,
            target.resolved_version,
        )
        for target in skill.targets
    }
    if missing_targets := sorted(
        required_dependencies - target_identities,
        key=lambda item: (str(item[0]), item[1], item[2], item[3] or ""),
    ):
        return (
            "researcher omitted version targets required by the repository profile: "
            + ", ".join(
                f"{ecosystem}:{name}@{resolved or declared}"
                for ecosystem, name, declared, resolved in missing_targets
            )
        )

    detected_names = {dependency.name for dependency in profile.dependencies}
    target_names = {target.name for target in skill.targets}
    allowed_origins = {_url_origin(url) for url in official_documentation_origins}
    grounded_names: set[str] = set()
    for source in skill.official_sources:
        if _url_origin(source.url) not in allowed_origins:
            return (
                "researcher cited a source outside "
                f"polish.official_documentation_origins: {source.url}"
            )
        if unverified := sorted(set(source.applies_to) - detected_names):
            return (
                "researcher cited an official source for dependencies that are not in "
                f"the repository profile: {source.url} covers " + ", ".join(unverified)
            )
        grounded_names.update(source.applies_to)
    if ungrounded_names := sorted(target_names - grounded_names):
        return (
            "researcher returned version-specific guidance without official source "
            "provenance for: " + ", ".join(ungrounded_names)
        )

    allowed_references = set(practice_reference_urls)
    for source in skill.practice_sources:
        if source.url not in allowed_references:
            return f"researcher cited a source outside polish.practice_reference_urls: {source.url}"
        if source.applies_to != (GENERIC_SKILL_TARGET,):
            return (
                f"researcher cited a practice source outside the generic repository "
                f"scope: {source.url}"
            )
        if source.version_scope.casefold() != GENERIC_PRACTICE_VERSION_SCOPE:
            return (
                "researcher cited a practice source carrying a version claim: "
                f"{source.url} scoped to {source.version_scope}"
            )
    return None


def _url_origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


# -- merging ----------------------------------------------------------------


def merge_repository_skill(
    generated: RepositorySkill,
    overlay: RepositorySkillOverlay | None,
) -> RepositorySkill:
    """Deterministically combine generated guidance with a human overlay.

    ``replace`` swaps out only the sections the overlay actually supplies;
    ``extend`` appends the overlay's prose to the generated prose with
    duplicates removed. Targets, sources, uncertainties, the dependency
    fingerprint and generator provenance always come from the generated skill
    -- an overlay contributes prose and nothing else.

    Raises :class:`RepositorySkillMergeError` when the combined result would
    exceed the bounds the strict :class:`RepositorySkill` model enforces.
    """
    if overlay is None:
        return generated

    payload: dict[str, Any] = generated.model_dump()
    payload["simplify"] = _merge_section(
        "simplify", generated.simplify, overlay.simplify, overlay.mode
    ).model_dump()
    payload["polish"] = _merge_section(
        "polish", generated.polish, overlay.polish, overlay.mode
    ).model_dump()
    try:
        return RepositorySkill.model_validate(payload)
    except ValidationError as exc:  # pragma: no cover - defensive
        raise RepositorySkillMergeError(
            f"the effective repository skill is invalid after merging the overlay: "
            f"{_validation_problems(exc)}"
        ) from exc


def _merge_section(
    name: str,
    generated: SkillGuidance,
    overlay: SkillGuidance | None,
    mode: SkillOverlayMode,
) -> SkillGuidance:
    if overlay is None:
        return generated
    if mode is SkillOverlayMode.REPLACE:
        return overlay

    summary = _merge_summary(name, generated.summary, overlay.summary)
    merged_lists: dict[str, tuple[str, ...]] = {}
    for field_name in _GUIDANCE_LIST_FIELDS:
        merged = _deduplicate(
            (*getattr(generated, field_name), *getattr(overlay, field_name)),
        )
        limit = _guidance_bound(field_name)
        if len(merged) > limit:
            raise RepositorySkillMergeError(
                f"extending {name}.{field_name} would produce {len(merged)} entries, "
                f"exceeding the {limit} a repository skill allows; shorten the overlay "
                f"or use mode '{SkillOverlayMode.REPLACE.value}'"
            )
        merged_lists[field_name] = merged
    try:
        return SkillGuidance(summary=summary, **merged_lists)
    except ValidationError as exc:  # pragma: no cover - defensive
        raise RepositorySkillMergeError(
            f"extended {name} guidance is invalid: {_validation_problems(exc)}"
        ) from exc


def _merge_summary(name: str, generated: str, overlay: str) -> str:
    if generated.strip().casefold() == overlay.strip().casefold():
        return generated
    combined = f"{generated.rstrip()} {overlay.lstrip()}"
    limit = _guidance_bound("summary")
    if len(combined) > limit:
        raise RepositorySkillMergeError(
            f"extending {name}.summary would produce {len(combined)} characters, "
            f"exceeding the {limit} a repository skill allows; shorten the overlay "
            f"summary or use mode '{SkillOverlayMode.REPLACE.value}'"
        )
    return combined


def _deduplicate(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    kept: list[str] = []
    for item in items:
        key = item.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return tuple(kept)


def _guidance_bound(field_name: str) -> int:
    """Read a bound from :class:`SkillGuidance` itself, so merge limits can
    never drift away from the model the merged result must satisfy."""
    for meta in SkillGuidance.model_fields[field_name].metadata:
        maximum = getattr(meta, "max_length", None)
        if isinstance(maximum, int):
            return maximum
    raise RepositorySkillMergeError(  # pragma: no cover - defensive
        f"SkillGuidance.{field_name} declares no maximum length"
    )


def _validation_problems(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        problems.append(f"{location}: {error['msg']}")
    return "; ".join(problems)


def _overlay_warning(error: RepositorySkillError) -> str:
    """Phrase an overlay problem as an actionable, run-safe warning."""
    return f"repository skill overlay was not applied; using generated guidance only: {error}"


# -- filesystem helpers -----------------------------------------------------


def _assert_no_symlinked_components(root: Path, path: Path) -> None:
    """Refuse to traverse a symlink anywhere below ``root``.

    ``root`` itself may legitimately be a symlink (a data directory can live
    behind one); every component the factory owns below it may not, so stored
    state can never redirect reads or writes elsewhere.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RepositorySkillStorageError(f"{path} is not contained within {root}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RepositorySkillStorageError(f"refusing to follow symlink: {current}")


def _read_file_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular file, refusing symlinks, directories and oversized files.

    Opens with ``O_NOFOLLOW`` and validates the *opened* descriptor, so the
    checks cannot be defeated by swapping the path between check and open.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise
    except IsADirectoryError as exc:
        raise RepositorySkillStorageError(f"{path} is a directory, not a file") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise RepositorySkillStorageError(f"refusing to follow symlink: {path}") from exc
        raise RepositorySkillStorageError(f"cannot read {path}: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode):
            raise RepositorySkillStorageError(f"{path} is a directory, not a file")
        if not stat.S_ISREG(info.st_mode):
            raise RepositorySkillStorageError(f"{path} is not a regular file")
        if info.st_size > max_bytes:
            raise RepositorySkillStorageError(
                f"{path} is {info.st_size} bytes, exceeding the {max_bytes} byte limit"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            data = handle.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > max_bytes:
        raise RepositorySkillStorageError(f"{path} exceeds the {max_bytes} byte limit")
    return data


def _write_text_create_only(destination: Path, content: str) -> bool:
    """Publish ``content`` at ``destination`` only if it does not exist yet.

    Returns ``True`` when this call created the file. The content is written
    to a temporary sibling first and published with ``os.link``, which fails
    rather than clobbering, so a reader never sees a partial file and two
    concurrent creators agree on one complete winner.
    """
    temp_path = _write_temp_sibling(destination, content)
    try:
        os.link(temp_path, destination)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        raise RepositorySkillStorageError(f"cannot create {destination}: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def _write_text_atomic(destination: Path, content: str) -> None:
    """Replace ``destination`` atomically (the explicit-refresh write path)."""
    temp_path = _write_temp_sibling(destination, content)
    try:
        os.replace(temp_path, destination)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise RepositorySkillStorageError(f"cannot write {destination}: {exc}") from exc


def _write_temp_sibling(destination: Path, content: str) -> Path:
    temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
        handle.write(content)
    return temp_path


def _model_text(model: VersionedModel) -> str:
    return f"{model.model_dump_json(indent=2)}\n"


class RepositorySkillManager:
    """Filesystem-backed store for one repository's skills and overlay.

    Read APIs never create directories. Normal creation is no-clobber:
    :meth:`create_generated` publishes a generated skill only when none
    exists for that dependency fingerprint, and only the explicit
    :meth:`refresh_generated` replaces one. No API here writes, normalizes or
    deletes the overlay file, and nothing is ever written inside the source
    repository or a worktree.
    """

    def __init__(self, data_dir: str | Path, identity: RepositoryIdentity) -> None:
        self._data_dir = Path(data_dir).expanduser()
        self._identity = RepositoryIdentity(
            key=validate_repository_key(identity.key),
            git_common_dir=Path(identity.git_common_dir),
        )

    @classmethod
    def for_repository(
        cls, data_dir: str | Path, repository_path: str | Path
    ) -> RepositorySkillManager:
        """Build a manager for the repository containing ``repository_path``."""
        return cls(data_dir, resolve_repository_identity(repository_path))

    # -- paths (never created by reads) -----------------------------------

    @property
    def identity(self) -> RepositoryIdentity:
        return self._identity

    @property
    def repository_key(self) -> str:
        return self._identity.key

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def root_dir(self) -> Path:
        return self._data_dir / STORAGE_ROOT_DIRNAME / STORAGE_LAYOUT_VERSION

    @property
    def repository_dir(self) -> Path:
        return self.root_dir / self._identity.key

    @property
    def generated_dir(self) -> Path:
        return self.repository_dir / GENERATED_DIRNAME

    @property
    def overlay_path(self) -> Path:
        return self.repository_dir / OVERLAY_FILENAME

    @property
    def metadata_path(self) -> Path:
        return self.repository_dir / METADATA_FILENAME

    def generated_path(self, dependency_fingerprint: str) -> Path:
        """Path of the generated skill for one dependency fingerprint.

        A dependency change simply selects a different filename here, while
        the repository-scoped overlay keeps applying unchanged.
        """
        return (
            self.generated_dir / f"{validate_dependency_fingerprint(dependency_fingerprint)}.json"
        )

    # -- reads -------------------------------------------------------------

    def list_generated_fingerprints(self) -> tuple[str, ...]:
        """List stored generated fingerprints. Read-only: creates nothing."""
        generated_dir = self.generated_dir
        if not generated_dir.is_dir():
            return ()
        _assert_no_symlinked_components(self._data_dir, generated_dir)
        return tuple(
            sorted(
                child.stem
                for child in generated_dir.iterdir()
                if child.is_file()
                and child.suffix == ".json"
                and _FINGERPRINT_PATTERN.match(child.stem)
            )
        )

    def load_generated(self, dependency_fingerprint: str) -> RepositorySkill | None:
        """Load a stored generated skill, or ``None`` when none exists.

        Read-only: a missing skill never creates the repository or generated
        directory.
        """
        path = self.generated_path(dependency_fingerprint)
        _assert_no_symlinked_components(self._data_dir, path)
        try:
            raw = _read_file_bytes(path, max_bytes=MAX_GENERATED_SKILL_BYTES)
        except FileNotFoundError:
            return None
        return self._parse_generated(path, raw, dependency_fingerprint)

    def read_overlay(self) -> OverlayRead:
        """Read the human-owned overlay without ever writing to it.

        A missing overlay returns ``present=False`` and no error. An
        unusable overlay returns a typed :class:`RepositorySkillOverlayError`
        on the result (raised by :meth:`OverlayRead.require`) with the
        human's bytes untouched.
        """
        path = self.overlay_path
        try:
            _assert_no_symlinked_components(self._data_dir, path)
            raw = _read_file_bytes(path, max_bytes=MAX_OVERLAY_BYTES)
        except FileNotFoundError:
            return OverlayRead(path=path, present=False)
        except RepositorySkillStorageError as exc:
            return OverlayRead(
                path=path,
                present=True,
                error=RepositorySkillOverlayError(path, (str(exc),)),
            )

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return OverlayRead(
                path=path,
                present=True,
                error=RepositorySkillOverlayError(path, (f"file is not valid UTF-8: {exc}",)),
            )

        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return OverlayRead(
                path=path,
                present=True,
                raw_text=text,
                error=RepositorySkillOverlayError(path, (f"file is not valid YAML: {exc}",)),
            )
        except RecursionError:
            # A size-bounded file can still nest deeply enough to exhaust the
            # parser's stack (``[[[[...``), which surfaces as RecursionError
            # rather than a YAMLError. It is still just an unusable overlay,
            # so it degrades like any other instead of unwinding into a
            # caller as an untyped crash.
            return OverlayRead(
                path=path,
                present=True,
                raw_text=text,
                error=RepositorySkillOverlayError(
                    path, ("file is nested too deeply or invalid YAML",)
                ),
            )

        if not isinstance(document, dict):
            described = "an empty document" if document is None else type(document).__name__
            return OverlayRead(
                path=path,
                present=True,
                raw_text=text,
                error=RepositorySkillOverlayError(
                    path, (f"overlay must be a YAML mapping; got {described}",)
                ),
            )

        try:
            overlay = RepositorySkillOverlay.model_validate(document)
        except ValidationError as exc:
            return OverlayRead(
                path=path,
                present=True,
                raw_text=text,
                error=RepositorySkillOverlayError(
                    path, tuple(_validation_problems(exc).split("; "))
                ),
            )
        return OverlayRead(path=path, present=True, overlay=overlay, raw_text=text)

    def load_metadata(self) -> RepositoryMetadata | None:
        """Load the storage-local repository note, or ``None`` when absent."""
        path = self.metadata_path
        _assert_no_symlinked_components(self._data_dir, path)
        try:
            raw = _read_file_bytes(path, max_bytes=MAX_METADATA_BYTES)
        except FileNotFoundError:
            return None
        try:
            return RepositoryMetadata.model_validate_json(raw)
        except ValidationError as exc:
            raise RepositorySkillStorageError(
                f"stored repository metadata at {path} is invalid: {_validation_problems(exc)}"
            ) from exc

    # -- writes ------------------------------------------------------------

    def create_generated(self, skill: RepositorySkill) -> GeneratedSkillRecord:
        """Store ``skill`` if no skill exists for its dependency fingerprint.

        No-clobber and atomic. When another process published a skill for the
        same fingerprint first, that complete winner is loaded and returned
        with ``created=False``; nothing on disk is overwritten.
        """
        path = self.generated_path(skill.dependency_fingerprint)
        self._ensure_generated_dir()
        created = _write_text_create_only(path, _model_text(skill))
        if created:
            return GeneratedSkillRecord(path=path, skill=skill, created=True)
        existing = self.load_generated(skill.dependency_fingerprint)
        if existing is None:  # pragma: no cover - only under concurrent deletion
            raise RepositorySkillStorageError(
                f"generated skill at {path} disappeared while being created"
            )
        return GeneratedSkillRecord(path=path, skill=existing, created=False)

    def refresh_generated(self, skill: RepositorySkill) -> GeneratedSkillRecord:
        """Explicitly replace the stored generated skill, atomically.

        The only API that may overwrite generated guidance. It touches
        nothing else: the overlay file is never read, written or removed
        here, other fingerprints keep their stored skills, and ``skill``'s own
        ``generated_at`` is preserved exactly as supplied rather than being
        restamped, so the refreshed record still says when its guidance was
        produced.
        """
        path = self.generated_path(skill.dependency_fingerprint)
        existed = path.exists()
        self._ensure_generated_dir()
        _write_text_atomic(path, _model_text(skill))
        return GeneratedSkillRecord(path=path, skill=skill, created=not existed)

    # -- selection ---------------------------------------------------------

    def select(self, skill: RepositorySkill) -> RepositorySkillSelection:
        """Store ``skill`` if absent, apply the overlay, and audit the result.

        An overlay problem never blocks a run and is never silently ignored:
        the selection falls back to generated-only guidance and carries a
        typed :attr:`RepositorySkillSelection.overlay_error` plus an
        actionable warning. Callers that want a hard failure (an explicit
        validate command, say) use ``read_overlay().require()``.
        """
        record = self.create_generated(skill)
        source = SkillSelectionSource.GENERATED if record.created else SkillSelectionSource.REUSED
        return self._selection(record, source)

    def reuse(self, dependency_fingerprint: str) -> RepositorySkillSelection | None:
        """Select an already stored skill, or ``None`` when none is stored.

        Pure read path: it never creates a directory or a file, so asking
        whether a skill can be reused cannot alter stored state. Overlay
        problems degrade exactly as they do in :meth:`select`.
        """
        stored = self.load_generated(dependency_fingerprint)
        if stored is None:
            return None
        record = GeneratedSkillRecord(
            path=self.generated_path(dependency_fingerprint), skill=stored, created=False
        )
        return self._selection(record, SkillSelectionSource.REUSED)

    def _selection(
        self, record: GeneratedSkillRecord, source: SkillSelectionSource
    ) -> RepositorySkillSelection:
        read = self.read_overlay()
        overlay = read.overlay
        overlay_error: RepositorySkillError | None = read.error
        effective = record.skill
        applied = False

        if overlay is not None:
            try:
                effective = merge_repository_skill(record.skill, overlay)
                applied = True
            except RepositorySkillMergeError as exc:
                # The overlay parsed but cannot be honoured. Report it and
                # keep the run on valid generated guidance rather than
                # failing, or pretending the human said nothing.
                overlay_error = exc

        use = RepositorySkillUse(
            repository_key=self._identity.key,
            dependency_fingerprint=record.skill.dependency_fingerprint,
            source=source,
            generated_skill_hash=content_hash(record.skill),
            overlay_hash=None if overlay is None else content_hash(overlay),
            overlay_mode=None if overlay is None else overlay.mode,
            overlay_applied=applied,
            effective_skill_hash=content_hash(effective),
        )
        return RepositorySkillSelection(
            effective_skill=effective,
            generated_skill=record.skill,
            overlay=overlay,
            use=use,
            generated_path=record.path,
            overlay_error=overlay_error,
            warnings=() if overlay_error is None else (_overlay_warning(overlay_error),),
        )

    # -- internals ---------------------------------------------------------

    def _ensure_generated_dir(self) -> None:
        generated_dir = self.generated_dir
        _assert_no_symlinked_components(self._data_dir, generated_dir)
        generated_dir.mkdir(parents=True, exist_ok=True)
        self._write_metadata_once()

    def _write_metadata_once(self) -> None:
        metadata = RepositoryMetadata(
            repository_key=self._identity.key,
            git_common_dir=str(self._identity.git_common_dir),
        )
        path = self.metadata_path
        if path.exists():
            return
        _write_text_create_only(path, _model_text(metadata))

    def _parse_generated(
        self, path: Path, raw: bytes, dependency_fingerprint: str
    ) -> RepositorySkill:
        try:
            skill = RepositorySkill.model_validate_json(raw)
        except ValidationError as exc:
            raise RepositorySkillStorageError(
                f"stored repository skill at {path} is invalid: {_validation_problems(exc)}"
            ) from exc
        if skill.dependency_fingerprint != dependency_fingerprint:
            raise RepositorySkillStorageError(
                f"stored repository skill at {path} claims dependency fingerprint "
                f"{skill.dependency_fingerprint}, not {dependency_fingerprint}"
            )
        return skill
