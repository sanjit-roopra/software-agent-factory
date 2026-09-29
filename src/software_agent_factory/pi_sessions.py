"""Per work item pi session files: which one a call continues, or starts.

Pi keeps a persisted JSONL conversation per work item and role. This module
decides, from a sidecar record of the previous call, whether the next call may
resume that file (:class:`Continue`) or must start a new one (:class:`Fresh`).
It never runs pi and holds no subprocess code; the runtime passes the returned
path to pi and reports the outcome back through :meth:`PiSessionStore.record`.
"""

from __future__ import annotations

import hashlib
import os
import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .models import AgentRole, ModelBase, UtcDateTime, utc_now

#: Only these roles continue a session. Every other role runs without one.
_SESSION_ROLES = frozenset({AgentRole.IMPLEMENTER, AgentRole.REVIEWER})


@dataclass(frozen=True)
class SessionSettings:
    """The call settings a session is bound to. A change of any starts a new session."""

    model: str
    provider: str
    reasoning: str


@dataclass(frozen=True)
class Continue:
    """Resume the existing session file at ``path`` with the continuation prompt."""

    path: Path


@dataclass(frozen=True)
class Fresh:
    """Start a new session file at ``path`` with the full prompt."""

    path: Path


type SessionDecision = Continue | Fresh


def persists_session(role: AgentRole) -> bool:
    """Return whether ``role`` keeps a persisted session. The single place this is decided."""
    return role in _SESSION_ROLES


class _SessionRecord(ModelBase):
    """Sidecar: what the last call on a role's current session file looked like."""

    session_file: str
    model: str
    provider: str
    reasoning: str
    last_ended_at: UtcDateTime
    last_success: bool


class PiSessionStore:
    """Decides which pi session file a call uses, under ``root``.

    Layout, per work item and role (``root`` is ``<factory.data_dir>/pi-sessions``)::

        <root>/<work item>/<role>.jsonl        first session file
        <root>/<work item>/<role>-<n>.jsonl    n-th replacement, n >= 2
        <root>/<work item>/<role>.meta.json    sidecar naming the current file

    A replacement never deletes the file it replaces. The sidecar always names
    the file of the latest recorded call, so only that file can be continued.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_age_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root
        self._max_age_seconds = max_age_seconds
        self._clock = clock or utc_now

    def resolve(
        self, work_item_id: str, role: AgentRole, settings: SessionSettings
    ) -> SessionDecision:
        """Continue the role's session when every reuse condition holds, else start a new one."""
        directory = self._directory(work_item_id, role)
        record = self._read_record(directory, role)
        if record is not None and self._is_reusable(record, settings, directory):
            return Continue(directory / record.session_file)
        return Fresh(_next_session_path(directory, role))

    def record(
        self,
        work_item_id: str,
        role: AgentRole,
        path: Path,
        settings: SessionSettings,
        *,
        success: bool,
        ended_at: datetime | None = None,
    ) -> None:
        """Remember how the call that used ``path`` ended. A failed call is never continued."""
        directory = self._directory(work_item_id, role)
        if path.parent != directory or not _is_session_file_name(path.name, role):
            raise ValueError(f"{path} is not a session file of this work item and role")
        record = _SessionRecord(
            session_file=path.name,
            model=settings.model,
            provider=settings.provider,
            reasoning=settings.reasoning,
            last_ended_at=ended_at or self._clock(),
            last_success=success,
        )
        _write_text_atomic(_record_path(directory, role), f"{record.model_dump_json(indent=2)}\n")

    def _directory(self, work_item_id: str, role: AgentRole) -> Path:
        if not persists_session(role):
            raise ValueError(f"role {role.value} does not keep a pi session")
        return self._root / _directory_name(work_item_id)

    def _is_reusable(
        self, record: _SessionRecord, settings: SessionSettings, directory: Path
    ) -> bool:
        if not record.last_success or _settings_of(record) != settings:
            return False
        age_seconds = (self._clock() - record.last_ended_at).total_seconds()
        if not 0 <= age_seconds < self._max_age_seconds:
            return False
        return _is_readable_file(directory / record.session_file)

    @staticmethod
    def _read_record(directory: Path, role: AgentRole) -> _SessionRecord | None:
        try:
            text = _record_path(directory, role).read_text(encoding="utf-8")
            record = _SessionRecord.model_validate_json(text)
        except (OSError, ValueError):
            return None
        if not _is_session_file_name(record.session_file, role):
            return None
        return record


_SESSION_SUFFIX = ".jsonl"
_PLAIN_CHARACTERS = frozenset(string.ascii_lowercase + string.digits + "-_")
_MAX_PLAIN_LENGTH = 60
_DIGEST_LENGTH = 32
_MAX_DIRECTORY_NAME_LENGTH = _MAX_PLAIN_LENGTH + 1 + _DIGEST_LENGTH


def _directory_name(work_item_id: str) -> str:
    """Map a work item id to one safe directory name: stable, and distinct per id.

    Lowercase letters, digits, ``-`` and ``_`` stay as they are. An uppercase
    letter becomes ``+`` and its lowercase letter, so ids that differ only in
    case do not share a directory on a case-insensitive file system. A ``.`` stays
    unless it comes first. Every other character becomes ``%XX`` for each of its
    UTF-8 bytes, so ``/``, ``\\``, NUL, ``%`` and ``+`` never appear raw and
    the result cannot leave the root. An encoding longer than the limit is cut
    and ends in ``~`` plus a digest of the whole id; ``~`` is always escaped in a
    plain name, so a cut name cannot equal one.
    """
    if not work_item_id:
        raise ValueError("work item id must not be empty")
    encoded = "".join(
        _encode_character(character, first=index == 0)
        for index, character in enumerate(work_item_id)
    )
    if len(encoded) <= _MAX_DIRECTORY_NAME_LENGTH:
        return encoded
    digest = hashlib.sha256(work_item_id.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{encoded[:_MAX_PLAIN_LENGTH]}~{digest[:_DIGEST_LENGTH]}"


def _encode_character(character: str, *, first: bool) -> str:
    if character in _PLAIN_CHARACTERS or (character == "." and not first):
        return character
    if character in string.ascii_uppercase:
        return f"+{character.lower()}"
    return "".join(f"%{byte:02X}" for byte in character.encode("utf-8", "surrogatepass"))


def _settings_of(record: _SessionRecord) -> SessionSettings:
    return SessionSettings(model=record.model, provider=record.provider, reasoning=record.reasoning)


def _record_path(directory: Path, role: AgentRole) -> Path:
    return directory / f"{role.value.lower()}.meta.json"


def _is_session_file_name(name: str, role: AgentRole) -> bool:
    """Return whether ``name`` is ``<role>.jsonl`` or ``<role>-<n>.jsonl`` with n >= 2."""
    stem = re.escape(role.value.lower())
    return (
        re.fullmatch(rf"{stem}(-([2-9]|[1-9][0-9]+))?{re.escape(_SESSION_SUFFIX)}", name)
        is not None
    )


def _next_session_path(directory: Path, role: AgentRole) -> Path:
    stem = role.value.lower()
    candidate = directory / f"{stem}{_SESSION_SUFFIX}"
    number = 1
    while candidate.exists():
        number += 1
        candidate = directory / f"{stem}-{number}{_SESSION_SUFFIX}"
    return candidate


def _is_readable_file(path: Path) -> bool:
    try:
        with path.open("rb") as session_file:
            session_file.read(1)
    except OSError:
        return False
    return True


def _write_text_atomic(destination: Path, content: str) -> None:
    """Write ``content`` to a temp file beside ``destination``, then ``os.replace`` it in."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, destination)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
