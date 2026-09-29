"""Per work item pi session files: which one a call continues, or starts.

Pi keeps a persisted JSONL conversation per work item and role. This module
decides, from a sidecar record of the previous call, whether the next call may
resume that file (:class:`Continue`) or must start a new one (:class:`Fresh`).
It never runs pi and holds no subprocess code; the runtime passes the returned
path to pi and reports the outcome back through :meth:`PiSessionStore.record`.
"""

from __future__ import annotations

import hashlib
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .atomic_write import write_text_atomic
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
    """Resume the existing session file at ``path`` with the continuation prompt.

    ``sent_sections`` maps each prompt section title to the content hash of what
    the session has received so far, so the caller can send only what is new.
    """

    path: Path
    sent_sections: Mapping[str, str]


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
    #: Section title -> content hash of what the session has received. Required:
    #: a sidecar written before this field existed does not validate, so its
    #: session is not continued. Without the map the factory cannot tell what the
    #: session holds, and it would send every section again into a long history.
    sent_sections: dict[str, str]


class PiSessionStore:
    """Decides which pi session file a call uses, under ``root``.

    Layout, per work item and role (``root`` is ``<factory.data_dir>/pi-sessions``)::

        <root>/<work item>/<role>.jsonl        first session file
        <root>/<work item>/<role>-<n>.jsonl    n-th replacement, n >= 2
        <root>/<work item>/<role>.meta.json    sidecar naming the current file

    The sidecar always names the file of the latest recorded call, so only that
    file can be continued. It also holds ``sent_sections``, the content hash of
    each prompt section the session has received. A sidecar without that map
    (written by an older version) is not reusable: the next call starts a new
    session. Starting a new session deletes the role's older files
    that were last written ``max_age_seconds`` or longer ago; younger ones stay.
    Everything is owner-only: directories 0700, files 0600.
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
        """Continue the role's session when every reuse condition holds, else start a new one.

        Also makes sure the private directories exist before pi runs, so pi never
        creates them with the umask's permissions.
        """
        directory = self._prepare_directory(work_item_id, role)
        record = self._read_record(directory, role)
        if record is not None and self._is_reusable(record, settings, directory):
            session_path = directory / record.session_file
            _restrict_to_owner(session_path)
            return Continue(session_path, record.sent_sections)
        return self._start_session(directory, role)

    def fresh(self, work_item_id: str, role: AgentRole) -> Fresh:
        """Start a new session file, although the role's current one could be continued.

        For a call that has nothing new to add to a continued session, so it needs
        the full prompt in a new one. The current file stays: only a file that
        expired is removed, as for any new session.
        """
        return self._start_session(self._prepare_directory(work_item_id, role), role)

    def record(
        self,
        work_item_id: str,
        role: AgentRole,
        path: Path,
        settings: SessionSettings,
        *,
        success: bool,
        sent_sections: Mapping[str, str],
        ended_at: datetime | None = None,
    ) -> None:
        """Remember how the call that used ``path`` ended. A failed call is never continued.

        ``sent_sections`` is the map of the sections that apply to the session,
        including this call. It is stored only for a call that settled: a failed
        call makes the next one start a new session, so its map is never read.

        Call it after every pi call: it also makes the session file readable by
        the owner only, because pi creates that file with the umask's permissions.
        """
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
            sent_sections=dict(sent_sections) if success else {},
        )
        _restrict_to_owner(path)
        write_text_atomic(
            _record_path(directory, role),
            f"{record.model_dump_json(indent=2)}\n",
            mode=_OWNER_FILE_MODE,
        )

    def _prepare_directory(self, work_item_id: str, role: AgentRole) -> Path:
        """Return the role's work item directory, made private before pi runs in it."""
        directory = self._directory(work_item_id, role)
        _ensure_private_directory(self._root)
        _ensure_private_directory(directory)
        return directory

    def _start_session(self, directory: Path, role: AgentRole) -> Fresh:
        fresh = Fresh(_next_session_path(directory, role))
        self._prune_expired_files(directory, role)
        return fresh

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

    def _prune_expired_files(self, directory: Path, role: AgentRole) -> None:
        """Delete the role's session files last written ``max_age`` or longer ago.

        Such a file can never be continued, and it holds a full transcript. Only
        a new session prunes, so the file a call continues is never touched. A
        file that cannot be removed is skipped: pruning must not fail a call.
        """
        cutoff = self._clock().timestamp() - self._max_age_seconds
        try:
            entries = [
                entry for entry in directory.iterdir() if _is_session_file_name(entry.name, role)
            ]
        except OSError:
            return
        for entry in entries:
            try:
                if entry.stat().st_mtime <= cutoff:
                    entry.unlink()
            except OSError:
                continue

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
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700
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


def _role_stem(role: AgentRole) -> str:
    return role.value.lower()


def _record_path(directory: Path, role: AgentRole) -> Path:
    return directory / f"{_role_stem(role)}.meta.json"


def _session_file_name(role: AgentRole, number: int) -> str:
    """The one definition of the file name: ``<stem>.jsonl``, then ``<stem>-<n>.jsonl``."""
    stem = _role_stem(role)
    if number == 1:
        return f"{stem}{_SESSION_SUFFIX}"
    return f"{stem}-{number}{_SESSION_SUFFIX}"


def _is_session_file_name(name: str, role: AgentRole) -> bool:
    """Return whether ``name`` is what :func:`_session_file_name` makes for ``role``.

    The number is read from the name and the name is built again from it, so
    the parser cannot drift from the formatter. Numbers of more than nine digits
    are not session numbers.
    """
    pattern = rf"{re.escape(_role_stem(role))}(?:-([0-9]{{1,9}}))?{re.escape(_SESSION_SUFFIX)}"
    match = re.fullmatch(pattern, name)
    if match is None:
        return False
    number = 1 if match.group(1) is None else int(match.group(1))
    return number >= 1 and name == _session_file_name(role, number)


def _next_session_path(directory: Path, role: AgentRole) -> Path:
    number = 1
    while (directory / _session_file_name(role, number)).exists():
        number += 1
    return directory / _session_file_name(role, number)


def _is_readable_file(path: Path) -> bool:
    try:
        with path.open("rb") as session_file:
            session_file.read(1)
    except OSError:
        return False
    return True


def _ensure_private_directory(path: Path) -> None:
    """Create ``path`` if needed and make it owner-only, whatever the umask says."""
    path.mkdir(mode=_OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(_OWNER_DIRECTORY_MODE)


def _restrict_to_owner(path: Path) -> None:
    """Make ``path`` owner-only. A file that does not exist (pi wrote none) is left alone."""
    try:
        path.chmod(_OWNER_FILE_MODE)
    except FileNotFoundError:
        pass
