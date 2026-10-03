"""Plan and write the repository skills of a setup run (ADR-034).

The setup pull request gives the repository what a local agent needs without
the factory: a managed block in ``AGENTS.md`` with the repository's own
checks, shared skills in ``.agents/skills/``, per-skill links in
``.claude/skills/`` and, for Python, a review agent in ``.claude/agents/``.
``CLAUDE.md`` becomes a link to ``AGENTS.md`` when it does not exist.

All content comes from fixed templates. No model writes it. A file that
already exists is never replaced, except the block between the factory
markers in ``AGENTS.md``. Text outside the markers is kept as written.
"""

from __future__ import annotations

import os
import stat
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
MAX_AGENTS_FILE_BYTES = 1_048_576


class RepositoryFileError(Exception):
    """A repository file could not be read or written safely."""


@dataclass(frozen=True)
class RepositoryFile:
    """One file the setup run writes: text content or a relative symbolic link."""

    path: str
    content: str | None = None
    link_target: str | None = None

    def __post_init__(self) -> None:
        if (self.content is None) == (self.link_target is None):
            raise ValueError("a repository file has either content or a link target")


def plan_repository_files(
    root: Path, inventory: ToolchainInventory, profile: RepositoryProfile
) -> tuple[RepositoryFile, ...]:
    """Return the files to write under ``root``. Nothing a person wrote is replaced."""

    verify = _verify_text(inventory, profile)
    files: list[RepositoryFile] = []
    agents = _agents_file(root, verify)
    if agents is not None:
        files.append(agents)
    if not _exists(root / CLAUDE_FILE):
        files.append(RepositoryFile(CLAUDE_FILE, link_target=AGENTS_FILE))
    for name in SKILL_NAMES:
        skill = f"{SHARED_SKILLS_DIR}/{name}/SKILL.md"
        if not _exists(root / skill):
            text = _template(f"skills/{name}/SKILL.md").replace(VERIFY_PLACEHOLDER, verify)
            files.append(RepositoryFile(skill, content=text))
        link = f"{CLAUDE_SKILLS_DIR}/{name}"
        if not _exists(root / link):
            files.append(RepositoryFile(link, link_target=f"../../{SHARED_SKILLS_DIR}/{name}"))
    for lane in inventory.lanes:
        agent = LANE_AGENTS.get(lane)
        if agent is not None and not _exists(root / CLAUDE_AGENTS_DIR / agent):
            files.append(
                RepositoryFile(f"{CLAUDE_AGENTS_DIR}/{agent}", content=_template(f"agents/{agent}"))
            )
    return tuple(files)


def write_repository_files(root: Path, files: tuple[RepositoryFile, ...]) -> None:
    """Write ``files`` under ``root`` without following a symbolic link the repository holds."""

    for repository_file in files:
        target = root / repository_file.path
        _make_real_parents(root, target.parent)
        if repository_file.link_target is not None:
            try:
                os.symlink(repository_file.link_target, target)
            except OSError as exc:
                raise RepositoryFileError(f"cannot link {repository_file.path}: {exc}") from exc
            continue
        assert repository_file.content is not None
        _write_text(target, repository_file.path, repository_file.content)


def _verify_text(inventory: ToolchainInventory, profile: RepositoryProfile) -> str:
    prefix = f"{CHECK_ENVIRONMENT} "
    commands = [
        command.removeprefix(prefix)
        for lane in candidate_commands(inventory, profile).lanes
        for command in (*lane.install, *lane.verify)
    ]
    return "\n".join(commands) or NO_CHECKS


def _agents_file(root: Path, verify: str) -> RepositoryFile | None:
    block = _template("agents_block.md").replace(VERIFY_PLACEHOLDER, verify).rstrip("\n")
    path = root / AGENTS_FILE
    if not _exists(path):
        return RepositoryFile(AGENTS_FILE, content=f"# Agent instructions\n\n{block}\n")
    current = _read_regular_text(path)
    if current is None:
        return None
    begin = current.find(BLOCK_BEGIN)
    end = current.find(BLOCK_END, begin)
    if begin == -1 or end == -1:
        separator = "" if current.endswith("\n\n") else "\n" if current.endswith("\n") else "\n\n"
        return RepositoryFile(AGENTS_FILE, content=f"{current}{separator}{block}\n")
    updated = current[:begin] + block + current[end + len(BLOCK_END) :]
    return None if updated == current else RepositoryFile(AGENTS_FILE, content=updated)


def _template(name: str) -> str:
    return (
        resources.files("software_agent_factory")
        .joinpath(f"repo_templates/{name}")
        .read_text(encoding="utf-8")
    )


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _read_regular_text(path: Path) -> str | None:
    """Read a regular file without following a link. Return ``None`` for anything else."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        raw = handle.read(MAX_AGENTS_FILE_BYTES + 1)
    if len(raw) > MAX_AGENTS_FILE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _make_real_parents(root: Path, directory: Path) -> None:
    """Create ``directory`` under ``root``, refusing any part that is a symbolic link."""
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RepositoryFileError(f"refusing to write through a symbolic link: {current}")
        current.mkdir(exist_ok=True)


def _write_text(target: Path, name: str, content: str) -> None:
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        raise RepositoryFileError(f"cannot write {name}: {exc}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RepositoryFileError(f"not a regular file: {name}")
        handle.write(content)
