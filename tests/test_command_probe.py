from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from software_agent_factory.command_probe import (
    CHANGED_TREE,
    INSTALL_FAILED,
    NOT_AT_BASE_NOTE,
    ProbeLimits,
    baseline_failure_reason,
    probe_candidates,
)
from software_agent_factory.models import (
    CommandResult,
    RejectedCommand,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    ToolchainLane,
    VerificationReport,
)
from software_agent_factory.toolchain_commands import CandidateCommands, LaneCommands

_LIMITS = ProbeLimits(timeout_seconds=60, env_passthrough=(), capture_bytes=1024)
_PY = LaneCommands(ToolchainLane.PYTHON, ("uv sync --locked",), ("CI=true ruff", "CI=true pytest"))
_JS = LaneCommands(ToolchainLane.JAVASCRIPT, ("npm ci",), ("CI=true npm run test",))


# double-waiver: B1 — the real runner spawns subprocesses; this records commands instead.
class _Runner:
    def __init__(
        self,
        failing: frozenset[str] = frozenset(),
        dirty_after: frozenset[str] = frozenset(),
        raises: frozenset[str] = frozenset(),
        workspace: _Workspace | None = None,
    ) -> None:
        self.failing = failing
        self.dirty_after = dirty_after
        self.raises = raises
        self.workspace = workspace
        self.commands: list[str] = []
        self.calls: list[tuple[Path, int, tuple[str, ...], int]] = []

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 0,
    ) -> VerificationReport:
        self.calls.append((cwd, timeout_seconds, tuple(env_passthrough), capture_bytes))
        results: list[CommandResult] = []
        for command in commands:
            self.commands.append(command)
            if command in self.raises:
                raise OSError("spawn failed")
            if command in self.dirty_after and self.workspace is not None:
                self.workspace.dirty = True
            failed = command in self.failing
            results.append(
                CommandResult(
                    command=command,
                    exit_code=2 if failed else 0,
                    stdout="secret output",
                    stderr="secret output",
                    duration_seconds=0.0,
                )
            )
            if failed:
                break
        passed = all(result.exit_code == 0 for result in results)
        return VerificationReport(
            passed=passed,
            deterministic_checks=results,
            failures=[] if passed else ["x"],
            confidence=1.0 if passed else 0.0,
        )


# double-waiver: B1 — the real workspace runs git subprocesses on a worktree.
class _Workspace:
    def __init__(self, *, at_clean_base: bool = True) -> None:
        self.path = Path("/unused")
        self.at_clean_base = at_clean_base
        self.dirty = False
        self.discards = 0

    def is_at_clean_base(self) -> bool:
        return self.at_clean_base and not self.dirty

    def discard_changes(self) -> None:
        self.dirty = False
        self.discards += 1


def _probe(runner: _Runner, workspace: _Workspace, *lanes: LaneCommands) -> RepositoryCommandsPlan:
    return probe_candidates(runner, workspace, CandidateCommands(lanes, ()), _LIMITS)


def test_commands_that_pass_on_the_base_commit_are_kept() -> None:
    runner = _Runner(failing=frozenset({"CI=true ruff"}))

    plan = _probe(runner, _Workspace(), _PY)

    assert plan == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.DERIVED,
        install=("uv sync --locked",),
        verify=("CI=true pytest",),
        rejected=(
            RejectedCommand(
                command="CI=true ruff", reason="failed on the base commit with exit code 2"
            ),
        ),
    )
    assert runner.commands == ["uv sync --locked", "CI=true ruff", "CI=true pytest"]


def test_failed_install_rejects_only_its_own_lane() -> None:
    runner = _Runner(failing=frozenset({"uv sync --locked"}))

    plan = _probe(runner, _Workspace(), _PY, _JS)

    assert plan.source is RepositoryCommandsSource.DERIVED
    assert plan.install == ("npm ci",)
    assert plan.verify == ("CI=true npm run test",)
    assert plan.rejected == tuple(
        RejectedCommand(command=command, reason=INSTALL_FAILED)
        for command in ("uv sync --locked", "CI=true ruff", "CI=true pytest")
    )


def test_lane_that_changes_the_tree_is_discarded_and_rejected() -> None:
    workspace = _Workspace()
    runner = _Runner(dirty_after=frozenset({"CI=true pytest"}), workspace=workspace)

    plan = _probe(runner, workspace, _PY, _JS)

    assert workspace.discards == 1
    assert plan.verify == ("CI=true npm run test",)
    assert [(r.command, r.reason) for r in plan.rejected] == [
        ("CI=true ruff", CHANGED_TREE),
        ("CI=true pytest", CHANGED_TREE),
    ]


def test_failed_install_that_changes_the_tree_is_still_discarded() -> None:
    workspace = _Workspace()
    runner = _Runner(
        failing=frozenset({"npm ci"}), dirty_after=frozenset({"npm ci"}), workspace=workspace
    )

    plan = _probe(runner, workspace, _JS)

    assert workspace.discards == 1
    assert plan.source is RepositoryCommandsSource.NONE
    assert {r.reason for r in plan.rejected} == {INSTALL_FAILED}


def test_worktree_not_clean_at_its_base_runs_nothing_and_deletes_nothing() -> None:
    workspace = _Workspace(at_clean_base=False)
    runner = _Runner()

    plan = _probe(runner, workspace, _PY)

    assert plan == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.NONE, notes=(NOT_AT_BASE_NOTE,)
    )
    assert runner.commands == []
    assert workspace.discards == 0


def test_runner_error_discards_probe_changes_and_propagates() -> None:
    workspace = _Workspace()
    runner = _Runner(
        dirty_after=frozenset({"uv sync --locked"}),
        raises=frozenset({"CI=true ruff"}),
        workspace=workspace,
    )

    with pytest.raises(OSError, match="spawn failed"):
        _probe(runner, workspace, _PY)

    assert workspace.discards == 1


def test_no_candidates_keeps_the_notes() -> None:
    plan = probe_candidates(
        _Runner(), _Workspace(), CandidateCommands((), ("no supported language lane",)), _LIMITS
    )

    assert plan == RepositoryCommandsPlan(
        source=RepositoryCommandsSource.NONE, notes=("no supported language lane",)
    )


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (None, "failed on the base commit"),
        (
            CommandResult(
                command="pytest",
                exit_code=-1,
                stdout="secret output",
                stderr="secret output",
                duration_seconds=1.0,
                timed_out=True,
            ),
            "timed out on the base commit",
        ),
        (
            CommandResult(
                command="pytest",
                exit_code=3,
                stdout="secret output",
                stderr="secret output",
                duration_seconds=1.0,
            ),
            "failed on the base commit with exit code 3",
        ),
    ],
)
def test_baseline_failure_reason_never_quotes_output(
    result: CommandResult | None, reason: str
) -> None:
    report = VerificationReport(
        passed=False,
        deterministic_checks=[] if result is None else [result],
        failures=["x"],
        confidence=0.0,
    )

    assert baseline_failure_reason(report) == reason
    assert "secret" not in baseline_failure_reason(report)


def test_probe_passes_the_worktree_and_limits_to_the_runner() -> None:
    runner = _Runner()
    workspace = _Workspace()
    workspace.path = Path("/worktree")
    limits = ProbeLimits(timeout_seconds=7, env_passthrough=("NPM_CONFIG_CACHE",), capture_bytes=99)

    probe_candidates(runner, workspace, CandidateCommands((_JS,), ()), limits)

    assert set(runner.calls) == {(Path("/worktree"), 7, ("NPM_CONFIG_CACHE",), 99)}
