"""Plan and write the repository skills of a setup run (ADR-034).

The setup pull request gives the repository what a local agent needs without
the factory: a managed block in ``AGENTS.md`` with the repository's own
checks, shared skills in ``.agents/skills/``, per-skill links in
``.claude/skills/`` and, for Python, a review agent in ``.claude/agents/``.
``CLAUDE.md`` becomes a link to ``AGENTS.md`` when it does not exist.

All content comes from fixed templates. No model writes it. A file that
already exists is never replaced, except the one block between the factory
markers in ``AGENTS.md``. Text outside the markers is kept as written. A path
that the repository ignores is skipped, because it would never be committed.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .models import RepositoryProfile, ToolchainInventory, ToolchainLane
from .toolchain_commands import CHECK_ENVIRONMENT, candidate_commands

AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
SHARED_SKILLS_DIR = ".agents/skills"
CLAUDE_SKILLS_DIR = ".claude/skills"
CLAUDE_AGENTS_DIR = ".claude/agents"
SKILL_NAMES = ("pr-gate", "simplify", "polish")
LANE_AGENTS: dict[ToolchainLane, str] = {ToolchainLane.PYTHON: "python-quality.md"}
BLOCK_BEGIN = "<!-- factory:begin"
BLOCK_END = "<!-- factory:end -->"
VERIFY_PLACEHOLDER = "{{verify_commands}}"
NO_CHECKS = "# The factory found no checks. Add the repository's lint and test commands here."
UNCLEAR_BLOCK_NOTE = "AGENTS.md left alone: it needs exactly one factory block with both markers"
UNREADABLE_AGENTS_NOTE = "AGENTS.md left alone: it is not a readable UTF-8 file"
MIXED_NEWLINES_NOTE = "AGENTS.md left alone: it mixes line endings"
IGNORED_NOTE_SUFFIX = " skipped: the repository ignores it"
MAX_AGENTS_FILE_BYTES = 1_048_576


class RepositoryFileError(Exception):
    """A repository file could not be read or written safely."""


@dataclass(frozen=True)
class RepositoryFile:
    """One file the setup run writes: text content or a relative symbolic link.

    ``replaces`` is true only for an ``AGENTS.md`` that exists. Every other
    file is created and must not exist when it is written.
    """

    path: str
    content: str | None = None
    link_target: str | None = None
    replaces: bool = False

    def __post_init__(self) -> None:
        if (self.content is None) == (self.link_target is None):
            raise ValueError("a repository file has either content or a link target")


@dataclass(frozen=True)
class RepositoryFilesPlan:
    files: tuple[RepositoryFile, ...]
    notes: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(repository_file.path for repository_file in self.files)


def plan_repository_files(
    root: Path, inventory: ToolchainInventory, profile: RepositoryProfile
) -> RepositoryFilesPlan:
    """Return the files to write under ``root``. Nothing a person wrote is replaced."""

    verify = _verify_text(inventory, profile)
    candidates: list[RepositoryFile] = []
    notes: list[str] = []
    agents, agents_note = _agents_file(root, verify)
    if agents is not None:
        candidates.append(agents)
    if agents_note is not None:
        notes.append(agents_note)
    if not _exists(root / CLAUDE_FILE):
        candidates.append(RepositoryFile(CLAUDE_FILE, link_target=AGENTS_FILE))
    for name in SKILL_NAMES:
        skill = f"{SHARED_SKILLS_DIR}/{name}/SKILL.md"
        if not _exists(root / skill):
            text = _template(f"skills/{name}/SKILL.md").replace(VERIFY_PLACEHOLDER, verify)
            candidates.append(RepositoryFile(skill, content=text))
        link = f"{CLAUDE_SKILLS_DIR}/{name}"
        if not _exists(root / link):
            candidates.append(RepositoryFile(link, link_target=f"../../{SHARED_SKILLS_DIR}/{name}"))
    for lane in inventory.lanes:
        agent = LANE_AGENTS.get(lane)
        if agent is not None and not _exists(root / CLAUDE_AGENTS_DIR / agent):
            candidates.append(
                RepositoryFile(f"{CLAUDE_AGENTS_DIR}/{agent}", content=_template(f"agents/{agent}"))
            )
    ignored = _ignored_paths(root, [candidate.path for candidate in candidates])
    notes.extend(f"{path}{IGNORED_NOTE_SUFFIX}" for path in sorted(ignored))
    files = tuple(candidate for candidate in candidates if candidate.path not in ignored)
    return RepositoryFilesPlan(files, tuple(notes))


def write_repository_files(root: Path, files: tuple[RepositoryFile, ...]) -> None:
    """Write ``files`` under ``root`` without following a symbolic link the repository holds.

    Each parent directory is opened relative to the one before it with
    ``O_NOFOLLOW``, so a link that appears during the write is never followed.
    """

    for repository_file in files:
        parts = Path(repository_file.path).parts
        try:
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise RepositoryFileError(f"cannot open {root}: {exc}") from exc
        try:
            for part in parts[:-1]:
                try:
                    os.mkdir(part, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                os.close(directory_fd)
                directory_fd = child_fd
            _write_entry(directory_fd, parts[-1], repository_file)
        except OSError as exc:
            raise RepositoryFileError(f"cannot write {repository_file.path}: {exc}") from exc
        finally:
            os.close(directory_fd)


def _write_entry(directory_fd: int, name: str, repository_file: RepositoryFile) -> None:
    if repository_file.link_target is not None:
        os.symlink(repository_file.link_target, name, dir_fd=directory_fd)
        return
    assert repository_file.content is not None
    create = os.O_TRUNC if repository_file.replaces else os.O_EXCL
    # O_NONBLOCK makes a FIFO fail at once instead of hanging the setup.
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | create,
        0o644,
        dir_fd=directory_fd,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RepositoryFileError(f"not a regular file: {repository_file.path}")
        handle.write(repository_file.content)


def is_file_note(note: str) -> bool:
    """Return whether ``note`` comes from a repository file plan."""
    return note.startswith("AGENTS.md left alone") or note.endswith(IGNORED_NOTE_SUFFIX)


def _verify_text(inventory: ToolchainInventory, profile: RepositoryProfile) -> str:
    prefix = f"{CHECK_ENVIRONMENT} "
    commands = [
        command.removeprefix(prefix)
        for lane in candidate_commands(inventory, profile).lanes
        for command in (*lane.install, *lane.verify)
    ]
    return "\n".join(commands) or NO_CHECKS


def _agents_file(root: Path, verify: str) -> tuple[RepositoryFile | None, str | None]:
    block = _template("agents_block.md").replace(VERIFY_PLACEHOLDER, verify).rstrip("\n")
    path = root / AGENTS_FILE
    if not _exists(path):
        return RepositoryFile(AGENTS_FILE, content=f"# Agent instructions\n\n{block}\n"), None
    current = _read_regular_text(path)
    if current is None:
        return None, UNREADABLE_AGENTS_NOTE
    # Work with "\n" and write back in the file's own line ending style.
    crlf = current.count("\r\n")
    if crlf and crlf != current.count("\n"):
        return None, MIXED_NEWLINES_NOTE
    text = current.replace("\r\n", "\n")
    updated = _merge_block(text, block)
    if updated is None:
        return None, UNCLEAR_BLOCK_NOTE
    if updated == text:
        return None, None
    newline = "\r\n" if crlf else "\n"
    return RepositoryFile(AGENTS_FILE, content=updated.replace("\n", newline), replaces=True), None


def _merge_block(text: str, block: str) -> str | None:
    """Put ``block`` into ``text``, or return ``None`` when the markers are unclear."""
    begins = text.count(BLOCK_BEGIN)
    ends = text.count(BLOCK_END)
    if begins == 0 and ends == 0:
        return f"{text}{_separator(text)}{block}\n"
    begin = text.find(BLOCK_BEGIN)
    end = text.find(BLOCK_END)
    if begins != 1 or ends != 1 or begin > end:
        return None
    return text[:begin] + block + text[end + len(BLOCK_END) :]


def _separator(text: str) -> str:
    """Return what keeps exactly one blank line between ``text`` and an appended block."""
    if text.endswith("\n\n"):
        return ""
    if text.endswith("\n"):
        return "\n"
    return "\n\n"


def _ignored_paths(root: Path, paths: list[str]) -> set[str]:
    """Return the paths that the repository at ``root`` ignores. Outside Git, none."""
    if not paths:
        return set()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
            input="\0".join(paths) + "\0",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode not in (0, 1):
        return set()
    return {path for path in result.stdout.split("\0") if path}


def _template(name: str) -> str:
    return (
        resources.files("software_agent_factory")
        .joinpath(f"repo_templates/{name}")
        .read_text(encoding="utf-8")
    )


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _read_regular_text(path: Path) -> str | None:
    """Read a regular file without following a link or blocking. ``None`` for anything else."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MAX_AGENTS_FILE_BYTES + 1)
    if len(raw) > MAX_AGENTS_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
