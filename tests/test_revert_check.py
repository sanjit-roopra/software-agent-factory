from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from software_agent_factory.command_probe import ProbeLimits
from software_agent_factory.models import CommandResult, VerificationReport
from software_agent_factory.revert_check import (
    REVERT_CHECK_COMMAND,
    TESTS_PASS_WITHOUT_CHANGE,
    revert_check_result,
    revert_check_sources,
    run_revert_check,
)
from software_agent_factory.workspace import GitWorktreeWorkspace, WorkspaceError

_LIMITS = ProbeLimits(timeout_seconds=60, env_passthrough=(), capture_bytes=65536)
_SOURCES = ("app.py", "new.py", "old.py")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitWorktreeWorkspace:
    """A worktree that changes app.py, adds new.py, deletes old.py and adds a test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "old.py").write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    data = tmp_path / "data"
    data.mkdir()
    ws = GitWorktreeWorkspace(data, repo, "WI-revert")
    path = ws.prepare()
    (path / "app.py").write_text("x = 2\n", encoding="utf-8")
    (path / "new.py").write_text("y = 3\n", encoding="utf-8")
    (path / "old.py").unlink()
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test(): pass\n", encoding="utf-8")
    return ws


class _Runner:
    """Record what the worktree holds when the verify commands run."""

    def __init__(self, exit_code: int = 1, timed_out: bool = False) -> None:
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.seen: list[tuple[str, bool, bool]] = []

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 32768,
    ) -> VerificationReport:
        self.seen.append(
            ((cwd / "app.py").read_text(), (cwd / "new.py").exists(), (cwd / "old.py").exists())
        )
        check = CommandResult(
            command=commands[0],
            exit_code=self.exit_code,
            duration_seconds=0.0,
            timed_out=self.timed_out,
        )
        passed = self.exit_code == 0
        return VerificationReport(
            passed=passed, deterministic_checks=[check], confidence=1.0 if passed else 0.0
        )


def test_sources_are_the_non_test_files_of_a_change_with_tests() -> None:
    assert revert_check_sources(["src/a.py", "tests/test_a.py"]) == ("src/a.py",)
    assert revert_check_sources(["src/a.ts", "src/a.test.ts", "__tests__/b.js"]) == ("src/a.ts",)
    assert revert_check_sources(["src/a.py"]) == ()
    assert revert_check_sources(["tests/test_a.py"]) == ()


@pytest.mark.parametrize(
    ("runner", "verdict"),
    [
        (_Runner(), "passed: pytest fails without the production change"),
        (_Runner(exit_code=0), TESTS_PASS_WITHOUT_CHANGE),
        (_Runner(exit_code=-1, timed_out=True), "skipped: pytest timed out"),
    ],
)
def test_check_runs_verify_at_the_base_and_restores_the_change(
    workspace: GitWorktreeWorkspace, runner: _Runner, verdict: str
) -> None:
    tree = workspace.collect_evidence().tree_sha
    assert tree is not None

    result = run_revert_check(runner, workspace, tree, ["pytest"], _SOURCES, _LIMITS)

    assert result == verdict
    assert runner.seen == [("x = 1\n", False, True)]
    assert workspace.collect_evidence().tree_sha == tree


def test_check_skips_and_restores_the_change_when_verify_raises(
    workspace: GitWorktreeWorkspace,
) -> None:
    tree = workspace.collect_evidence().tree_sha
    assert tree is not None

    class _Raising(_Runner):
        def run(self, *args: object, **kwargs: object) -> VerificationReport:
            raise OSError("spawn failed")

    verdict = run_revert_check(_Raising(), workspace, tree, ["pytest"], _SOURCES, _LIMITS)

    assert verdict == "skipped: revert check error: OSError"
    assert workspace.collect_evidence().tree_sha == tree


def test_result_is_one_passing_check_that_lists_the_reverted_files() -> None:
    result = revert_check_result(TESTS_PASS_WITHOUT_CHANGE, ("app.py",), 1.0)

    assert result.command == REVERT_CHECK_COMMAND
    assert result.exit_code == 0
    assert result.stdout == f"revert check: {TESTS_PASS_WITHOUT_CHANGE}\n" + (
        "reverted to the base commit:\n- app.py"
    )


def test_failure_to_restore_the_change_is_raised(
    workspace: GitWorktreeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    def _restore(paths: Sequence[str], tree: str | None = None) -> None:
        calls.append(tree)
        if tree is not None:
            raise WorkspaceError("restore failed")

    monkeypatch.setattr(workspace, "restore_paths", _restore)

    with pytest.raises(WorkspaceError):
        run_revert_check(_Runner(), workspace, "abc", ["pytest"], _SOURCES, _LIMITS)
    assert calls == [None, "abc"]
