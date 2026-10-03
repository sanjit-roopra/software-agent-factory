"""Apply a toolchain setup plan in a factory worktree (ADR-034).

A setup run works in its own worktree, keyed by the source HEAD commit, under
the per-work-item lock. It refuses a worktree that is not clean at its base,
so a kept worktree from a failed run is never built on. The add commands
change only the manifest and the lockfile: they install nothing, and the
JavaScript ones run no package scripts. Python locking can still run the
project's build backend to read package metadata. The plan is recorded in
``.factory/setup.json`` without following a symbolic link that the
repository might hold.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .command_probe import CommandRunner, ProbeLimits, command_failure_reason
from .models import ToolchainSetupPlan
from .repository_profile import profile_repository
from .toolchain import inventory_toolchain
from .toolchain_setup import plan_toolchain_setup
from .workspace import GitWorktreeWorkspace, WorkspaceError, WorkspaceLockError

SETUP_RECORD_DIR = ".factory"
SETUP_RECORD_NAME = "setup.json"
SETUP_WORK_ITEM_PREFIX = "SETUP-"
NOT_AT_BASE = "setup worktree is not clean at its base commit"
LOCKED = "another setup run holds the lock"
HEAD_MOVED = "the source HEAD moved while the setup worktree was prepared"


class SetupError(Exception):
    """A setup run could not start or could not write its record."""


@dataclass(frozen=True)
class SetupOutcome:
    """What applying a setup plan did."""

    applied: tuple[str, ...]
    failed_command: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.failed_command is None) != (self.failure_reason is None):
            raise ValueError("a failed command and its reason come together")

    @property
    def succeeded(self) -> bool:
        return self.failed_command is None


@dataclass(frozen=True)
class SetupRunResult:
    """The plan of a setup run, where it ran and what happened."""

    plan: ToolchainSetupPlan
    worktree: Path
    branch: str
    outcome: SetupOutcome


def run_toolchain_setup(
    source_repo: Path,
    data_dir: Path,
    branch_prefix: str,
    command_runner: CommandRunner,
    limits: ProbeLimits,
    head_commit: str,
) -> SetupRunResult:
    """Plan and apply the setup in a worktree at ``head_commit`` of ``source_repo``."""

    try:
        workspace = GitWorktreeWorkspace(
            data_dir,
            source_repo,
            f"{SETUP_WORK_ITEM_PREFIX}{head_commit[:12]}",
            branch_prefix=branch_prefix,
        )
        with workspace:
            worktree = workspace.prepare()
            # Refuse a worktree that a failed run kept, and one created after
            # the source HEAD moved away from the commit this run was asked for.
            if workspace.base_commit != head_commit:
                raise SetupError(f"{HEAD_MOVED}: {worktree}")
            if not workspace.is_at_clean_base():
                raise SetupError(f"{NOT_AT_BASE}: {worktree}")
            profile = profile_repository(worktree)
            plan = plan_toolchain_setup(inventory_toolchain(worktree, profile), profile)
            outcome = apply_toolchain_setup(plan, command_runner, worktree, limits)
    except WorkspaceLockError as exc:
        raise SetupError(f"{LOCKED}: {exc}") from exc
    except (WorkspaceError, OSError) as exc:
        raise SetupError(str(exc)) from exc
    return SetupRunResult(plan, worktree, workspace.branch_name, outcome)


def apply_toolchain_setup(
    plan: ToolchainSetupPlan,
    command_runner: CommandRunner,
    worktree: Path,
    limits: ProbeLimits,
) -> SetupOutcome:
    """Run the plan's add commands in order and record the plan in the worktree.

    An empty plan runs nothing and writes nothing. A failed command stops the
    setup, writes no record and leaves the worktree for inspection.
    """

    if plan.is_empty:
        return SetupOutcome(applied=())
    applied: list[str] = []
    for command in plan.commands:
        report = command_runner.run(
            [command],
            worktree,
            limits.timeout_seconds,
            env_passthrough=limits.env_passthrough,
            capture_bytes=limits.capture_bytes,
        )
        if not report.passed:
            return SetupOutcome(
                tuple(applied),
                failed_command=command,
                failure_reason=command_failure_reason(report, "during setup"),
            )
        applied.append(command)
    write_setup_record(worktree, plan)
    return SetupOutcome(tuple(applied))


def write_setup_record(worktree: Path, plan: ToolchainSetupPlan) -> Path:
    """Write ``.factory/setup.json`` without following symbolic links.

    The worktree is a checkout of the target repository, which can hold a
    symbolic link at ``.factory`` or at the record path.
    """

    directory = worktree / SETUP_RECORD_DIR
    if directory.is_symlink():
        raise SetupError(f"refusing to write through a symbolic link: {SETUP_RECORD_DIR}")
    try:
        directory.mkdir(exist_ok=True)
        # Open the directory once and write relative to it, so a later swap of
        # the directory for a symbolic link cannot redirect the write.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SetupError(f"cannot use {SETUP_RECORD_DIR}: {exc}") from exc
    try:
        descriptor = os.open(
            SETUP_RECORD_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise SetupError(f"cannot write {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}: {exc}") from exc
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SetupError(f"not a regular file: {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}")
        handle.write(plan.model_dump_json(indent=2) + "\n")
    return directory / SETUP_RECORD_NAME


def source_state(repo: Path) -> tuple[str, bool]:
    """Return the HEAD commit of ``repo`` and whether its checkout has changes."""
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
        status = _git(repo, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(f"not a Git repository with a commit: {repo}") from exc
    return head, bool(status.strip())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout
