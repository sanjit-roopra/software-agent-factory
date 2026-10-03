"""Run derived repository commands on the base commit and keep those that pass (ADR-034).

The probe is the only step before triage that runs repository code. It needs a
worktree that is clean and at its base commit, so a failure on the probe
belongs to the repository and not to earlier work. It never deletes work it
did not create: if the worktree is not clean before the probe, it runs
nothing. Each lane is probed on its own, so one lane's failure does not reject
another lane's commands.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import (
    RejectedCommand,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    VerificationReport,
)
from .toolchain_commands import CandidateCommands, LaneCommands

INSTALL_FAILED = "install failed on the base commit"
CHANGED_TREE = "changed the Git tree on the base commit"
NOT_AT_BASE_NOTE = "worktree is not clean at its base commit; no command was run"


class CommandRunner(Protocol):
    """Runs shell commands in a directory, as ``DeterministicVerifier`` does."""

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = ...,
    ) -> VerificationReport: ...


class ProbeWorkspace(Protocol):
    """The worktree operations the probe needs."""

    path: Path

    def is_at_clean_base(self) -> bool: ...

    def discard_changes(self) -> None: ...


@dataclass(frozen=True)
class ProbeLimits:
    timeout_seconds: int
    env_passthrough: tuple[str, ...]
    capture_bytes: int


def probe_candidates(
    runner: CommandRunner,
    workspace: ProbeWorkspace,
    candidates: CandidateCommands,
    limits: ProbeLimits,
) -> RepositoryCommandsPlan:
    """Return the plan of commands that pass on the unchanged base commit."""

    notes = list(candidates.notes)
    if not candidates.lanes:
        return RepositoryCommandsPlan(source=RepositoryCommandsSource.NONE, notes=tuple(notes))
    if not workspace.is_at_clean_base():
        notes.append(NOT_AT_BASE_NOTE)
        return RepositoryCommandsPlan(source=RepositoryCommandsSource.NONE, notes=tuple(notes))
    install: list[str] = []
    verify: list[str] = []
    rejected: list[RejectedCommand] = []
    for lane in candidates.lanes:
        kept, lane_rejected = _probe_lane(runner, workspace, lane, limits)
        rejected.extend(lane_rejected)
        if kept:
            install.extend(lane.install)
            verify.extend(kept)
    if not verify:
        return RepositoryCommandsPlan(
            source=RepositoryCommandsSource.NONE, rejected=tuple(rejected), notes=tuple(notes)
        )
    return RepositoryCommandsPlan(
        source=RepositoryCommandsSource.DERIVED,
        install=tuple(install),
        verify=tuple(verify),
        rejected=tuple(rejected),
        notes=tuple(notes),
    )


def _probe_lane(
    runner: CommandRunner,
    workspace: ProbeWorkspace,
    lane: LaneCommands,
    limits: ProbeLimits,
) -> tuple[list[str], list[RejectedCommand]]:
    try:
        kept, rejected = _run_lane(runner, workspace, lane, limits)
    except BaseException:
        _discard_probe_changes(workspace)
        raise
    # The worktree was clean at its base before the probe, so any change,
    # including a commit that moved HEAD, is the probe's own.
    if _discard_probe_changes(workspace) and kept:
        return [], [
            RejectedCommand(command=command, reason=CHANGED_TREE) for command in lane.verify
        ]
    return kept, rejected


def _run_lane(
    runner: CommandRunner,
    workspace: ProbeWorkspace,
    lane: LaneCommands,
    limits: ProbeLimits,
) -> tuple[list[str], list[RejectedCommand]]:
    if not _run(runner, workspace, lane.install, limits).passed:
        return [], [
            RejectedCommand(command=command, reason=INSTALL_FAILED)
            for command in (*lane.install, *lane.verify)
        ]
    kept: list[str] = []
    rejected: list[RejectedCommand] = []
    for command in lane.verify:
        report = _run(runner, workspace, (command,), limits)
        if report.passed:
            kept.append(command)
        else:
            rejected.append(
                RejectedCommand(command=command, reason=baseline_failure_reason(report))
            )
    return kept, rejected


def _discard_probe_changes(workspace: ProbeWorkspace) -> bool:
    """Discard changes the probe made. Return whether there were any."""
    if workspace.is_at_clean_base():
        return False
    workspace.discard_changes()
    return True


def _run(
    runner: CommandRunner,
    workspace: ProbeWorkspace,
    commands: Sequence[str],
    limits: ProbeLimits,
) -> VerificationReport:
    return runner.run(
        list(commands),
        workspace.path,
        limits.timeout_seconds,
        env_passthrough=limits.env_passthrough,
        capture_bytes=limits.capture_bytes,
    )


def baseline_failure_reason(report: VerificationReport) -> str:
    """Describe a baseline failure without quoting command output."""
    result = report.deterministic_checks[-1] if report.deterministic_checks else None
    if result is None:
        return "failed on the base commit"
    if result.timed_out:
        return "timed out on the base commit"
    return f"failed on the base commit with exit code {result.exit_code}"
