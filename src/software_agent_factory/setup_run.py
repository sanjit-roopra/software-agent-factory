"""Apply a toolchain setup plan in a factory worktree (ADR-034).

A setup run works in its own worktree, keyed by the source HEAD commit, under
the per-work-item lock. It refuses a worktree that is not clean at its base,
so a kept worktree from a failed run is never built on. The add commands
change only the manifest and the lockfile: they install nothing and run no
package scripts. The plan is recorded in ``.factory/setup.json`` without
following a symbolic link that the repository might hold.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .command_probe import CommandRunner, ProbeLimits, command_failure_reason
from .models import ToolchainSetupPlan
from .repository_profile import profile_repository
from .toolchain import inventory_toolchain
from .toolchain_setup import plan_toolchain_setup
from .workspace import GitWorktreeWorkspace, WorkspaceError

SETUP_RECORD_DIR = ".factory"
SETUP_RECORD_NAME = "setup.json"
SETUP_WORK_ITEM_PREFIX = "SETUP-"
NOT_AT_BASE = "setup worktree is not clean at its base commit"


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

    workspace = GitWorktreeWorkspace(
        data_dir,
        source_repo,
        f"{SETUP_WORK_ITEM_PREFIX}{head_commit[:12]}",
        branch_prefix=branch_prefix,
    )
    try:
        with workspace:
            worktree = workspace.prepare()
            if not workspace.is_at_clean_base():
                raise SetupError(f"{NOT_AT_BASE}: {worktree}")
            profile = profile_repository(worktree)
            plan = plan_toolchain_setup(inventory_toolchain(worktree, profile), profile)
            outcome = apply_toolchain_setup(plan, command_runner, worktree, limits)
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
    directory.mkdir(exist_ok=True)
    if not directory.is_dir() or directory.is_symlink():
        raise SetupError(f"not a directory: {SETUP_RECORD_DIR}")
    record = directory / SETUP_RECORD_NAME
    try:
        descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        raise SetupError(f"cannot write {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}: {exc}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SetupError(f"not a regular file: {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}")
        handle.write(plan.model_dump_json(indent=2) + "\n")
    return record
