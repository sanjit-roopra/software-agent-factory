from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from software_agent_factory.cli import CONFIG_ERROR_EXIT_CODE, app
from software_agent_factory.command_probe import ProbeLimits
from software_agent_factory.models import (
    CommandResult,
    SetupState,
    ToolchainSetupPlan,
    VerificationReport,
)
from software_agent_factory.publishing import PublishResult
from software_agent_factory.setup_run import (
    SETUP_RECORD_DIR,
    SETUP_RECORD_NAME,
    SetupError,
    SetupOutcome,
    SetupRunResult,
    SetupTrigger,
    apply_toolchain_setup,
    changed_paths,
    publish_setup,
    run_toolchain_setup,
    write_setup_record,
)
from software_agent_factory.workspace import GitWorktreeWorkspace

_FINGERPRINT = "a" * 64
_LIMITS = ProbeLimits(timeout_seconds=7, env_passthrough=("NPM_CONFIG_CACHE",), capture_bytes=99)
_OUTPUT_THAT_MUST_NOT_LEAK = "secret output"
_UV_ADD = "uv add --dev --no-sync ruff mypy pytest mutmut"
_TWO_LANES = ToolchainSetupPlan(
    manifest_fingerprint=_FINGERPRINT,
    commands=("uv add --dev --no-sync ruff", "npm install --save-dev oxlint"),
    packages=("ruff", "oxlint"),
)
cli = CliRunner()


# double-waiver: B1 — the real runner spawns package-manager subprocesses.
class _Runner:
    def __init__(
        self,
        failing: frozenset[str] = frozenset(),
        timing_out: frozenset[str] = frozenset(),
        edits: str | None = None,
    ) -> None:
        self.failing = failing
        self.timing_out = timing_out
        self.edits = edits
        self.calls: list[tuple[str, Path, int, tuple[str, ...], int]] = []

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 0,
    ) -> VerificationReport:
        command = commands[0]
        self.calls.append((command, cwd, timeout_seconds, tuple(env_passthrough), capture_bytes))
        if self.edits is not None:
            (cwd / self.edits).write_text("edited\n", encoding="utf-8")
        timed_out = command in self.timing_out
        exit_code = -1 if timed_out else 1 if command in self.failing else 0
        return VerificationReport(
            passed=exit_code == 0,
            deterministic_checks=[
                CommandResult(
                    command=command,
                    exit_code=exit_code,
                    stdout=_OUTPUT_THAT_MUST_NOT_LEAK,
                    stderr=_OUTPUT_THAT_MUST_NOT_LEAK,
                    duration_seconds=0.0,
                    timed_out=timed_out,
                )
            ],
            failures=[] if exit_code == 0 else ["failed"],
            confidence=1.0,
        )


def _read_record(worktree: Path) -> ToolchainSetupPlan:
    path = worktree / SETUP_RECORD_DIR / SETUP_RECORD_NAME
    return ToolchainSetupPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_apply_runs_every_command_in_the_worktree_and_records_the_plan(tmp_path: Path) -> None:
    runner = _Runner()

    outcome = apply_toolchain_setup(_TWO_LANES, runner, tmp_path, _LIMITS)

    assert outcome == SetupOutcome(applied=_TWO_LANES.commands)
    assert runner.calls == [
        (command, tmp_path, 7, ("NPM_CONFIG_CACHE",), 99) for command in _TWO_LANES.commands
    ]
    assert _read_record(tmp_path) == _TWO_LANES


def test_apply_stops_at_a_failure_and_writes_no_record(tmp_path: Path) -> None:
    runner = _Runner(failing=frozenset({"npm install --save-dev oxlint"}))

    outcome = apply_toolchain_setup(_TWO_LANES, runner, tmp_path, _LIMITS)

    assert outcome == SetupOutcome(
        applied=("uv add --dev --no-sync ruff",),
        failed_command="npm install --save-dev oxlint",
        failure_reason="failed during setup with exit code 1",
    )
    assert _OUTPUT_THAT_MUST_NOT_LEAK not in (outcome.failure_reason or "")
    assert not (tmp_path / SETUP_RECORD_DIR).exists()


def test_apply_reports_a_timeout(tmp_path: Path) -> None:
    runner = _Runner(timing_out=frozenset({"uv add --dev --no-sync ruff"}))

    outcome = apply_toolchain_setup(_TWO_LANES, runner, tmp_path, _LIMITS)

    assert outcome.failure_reason == "timed out during setup"


def test_empty_plan_runs_nothing_and_writes_nothing(tmp_path: Path) -> None:
    runner = _Runner()

    outcome = apply_toolchain_setup(
        ToolchainSetupPlan(manifest_fingerprint=_FINGERPRINT), runner, tmp_path, _LIMITS
    )

    assert outcome == SetupOutcome(applied=())
    assert runner.calls == []
    assert not (tmp_path / SETUP_RECORD_DIR).exists()


@pytest.mark.parametrize(
    "fields",
    [{"failure_reason": "failed"}, {"failed_command": "uv add --dev x"}],
)
def test_outcome_needs_a_failed_command_and_its_reason_together(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="come together"):
        SetupOutcome(applied=(), **fields)


def test_record_is_not_written_when_dot_factory_is_a_file(tmp_path: Path) -> None:
    (tmp_path / SETUP_RECORD_DIR).write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(SetupError, match="cannot use .factory"):
        write_setup_record(tmp_path, _TWO_LANES)


def test_record_is_not_written_through_a_symlinked_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / SETUP_RECORD_DIR).symlink_to(outside, target_is_directory=True)

    with pytest.raises(SetupError, match="symbolic link"):
        write_setup_record(worktree, _TWO_LANES)

    assert list(outside.iterdir()) == []


def test_record_is_not_written_through_a_symlinked_file(tmp_path: Path) -> None:
    target = tmp_path / "victim.txt"
    target.write_text("keep\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    (worktree / SETUP_RECORD_DIR).mkdir(parents=True)
    (worktree / SETUP_RECORD_DIR / SETUP_RECORD_NAME).symlink_to(target)

    with pytest.raises(SetupError, match="cannot write"):
        write_setup_record(worktree, _TWO_LANES)

    assert target.read_text(encoding="utf-8") == "keep\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )


@pytest.fixture
def bare_uv_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _commit(repo, "init")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _setup_worktree(data_dir: Path, repo: Path) -> Path:
    return (data_dir / "workspaces" / f"SETUP-{_head(repo)[:12]}").resolve()


def test_setup_run_changes_only_its_worktree(bare_uv_repo: Path, tmp_path: Path) -> None:
    runner = _Runner(edits="pyproject.toml")
    data_dir = tmp_path / "data"

    result = run_toolchain_setup(
        bare_uv_repo, data_dir, "factory/", runner, _LIMITS, _head(bare_uv_repo)
    )

    assert result.outcome == SetupOutcome(applied=(_UV_ADD,))
    assert result.branch == f"factory/SETUP-{_head(bare_uv_repo)[:12]}"
    assert [call[:2] for call in runner.calls] == [(_UV_ADD, result.worktree)]
    assert _read_record(result.worktree).commands == (_UV_ADD,)
    assert (result.worktree / "pyproject.toml").read_text() == "edited\n"
    assert _git(bare_uv_repo, "status", "--porcelain") == ""


def test_setup_run_refuses_a_worktree_left_dirty_by_an_earlier_run(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    head = _head(bare_uv_repo)
    first = run_toolchain_setup(
        bare_uv_repo, data_dir, "factory/", _Runner(edits="pyproject.toml"), _LIMITS, head
    )
    runner = _Runner()

    with pytest.raises(SetupError, match="not clean at its base"):
        run_toolchain_setup(bare_uv_repo, data_dir, "factory/", runner, _LIMITS, head)

    assert runner.calls == []
    assert (first.worktree / "pyproject.toml").read_text() == "edited\n"


def test_setup_run_refuses_while_another_run_holds_the_lock(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    head = _head(bare_uv_repo)
    holder = GitWorktreeWorkspace(data_dir, bare_uv_repo, f"SETUP-{head[:12]}")
    runner = _Runner()

    with holder, pytest.raises(SetupError, match="another setup run holds the lock"):
        run_toolchain_setup(bare_uv_repo, data_dir, "factory/", runner, _LIMITS, head)

    assert runner.calls == []


def _path_with(tmp_path: Path, scripts: dict[str, str]) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    os.symlink(git, bin_dir / "git")
    for name, body in scripts.items():
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(bin_dir)


def test_cli_dry_run_prints_the_plan_and_creates_no_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"

    result = cli.invoke(
        app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert result.output == f"add: {_UV_ADD}\n"
    assert _git(bare_uv_repo, "worktree", "list").count("\n") == 1


def test_cli_dry_run_notes_uncommitted_changes(bare_uv_repo: Path, tmp_path: Path) -> None:
    (bare_uv_repo / "app.py").write_text("x = 2\n", encoding="utf-8")

    result = cli.invoke(
        app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(tmp_path), "--dry-run"]
    )

    assert "note: uncommitted changes in the checkout; a setup run uses HEAD" in result.output


def test_cli_setup_success_reports_the_worktree_and_branch(
    bare_uv_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_with(tmp_path, {"uv": "echo '# added' >> pyproject.toml"}))
    data_dir = tmp_path / "data"

    result = cli.invoke(app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    worktree = _setup_worktree(data_dir, bare_uv_repo)
    assert result.output == (
        f"add: {_UV_ADD}\nworktree: {worktree}\nbranch: factory/SETUP-{_head(bare_uv_repo)[:12]}\n"
    )
    assert _read_record(worktree).commands == (_UV_ADD,)
    assert (worktree / "pyproject.toml").read_text().endswith("# added\n")
    assert _git(bare_uv_repo, "status", "--porcelain") == ""


def test_cli_setup_failure_keeps_the_worktree_and_leaves_the_source_alone(
    bare_uv_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", _path_with(tmp_path, {"uv": "exit 3"}))
    data_dir = tmp_path / "data"

    result = cli.invoke(app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir)])

    assert result.exit_code == 1, result.output
    assert (
        f"setup command failed: {_UV_ADD} (failed during setup with exit code 3)" in result.output
    )
    worktree = _setup_worktree(data_dir, bare_uv_repo)
    assert f"worktree kept at {worktree}" in result.output
    assert worktree.is_dir()
    assert not (worktree / SETUP_RECORD_DIR).exists()
    assert _git(bare_uv_repo, "status", "--porcelain") == ""


def test_cli_setup_outside_a_repository_fails_cleanly(tmp_path: Path) -> None:
    result = cli.invoke(app, ["setup", "--repo", str(tmp_path), "--data-dir", str(tmp_path)])

    assert result.exit_code == CONFIG_ERROR_EXIT_CODE
    assert "not a Git repository with a commit" in result.output


def test_cli_setup_reports_a_refused_run(bare_uv_repo: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    head = _head(bare_uv_repo)
    holder = GitWorktreeWorkspace(data_dir, bare_uv_repo, f"SETUP-{head[:12]}")

    with holder:
        result = cli.invoke(
            app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir)]
        )

    assert result.exit_code == 1
    assert result.output.startswith("setup could not run: another setup run holds the lock")


def test_cli_setup_with_nothing_to_add_prints_no_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    (bare_uv_repo / "uv.lock").unlink()
    _commit(bare_uv_repo, "drop the lockfile")

    result = cli.invoke(
        app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(tmp_path / "data")]
    )

    assert result.exit_code == 0, result.output
    assert result.output == (
        "note: python lane skipped: no supported lockfile at the repository root\nnothing to add\n"
    )


def test_setup_run_with_an_unusable_data_dir_fails_cleanly(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.write_text("a file, not a directory\n", encoding="utf-8")
    head = _head(bare_uv_repo)
    runner = _Runner()

    with pytest.raises(SetupError):
        run_toolchain_setup(bare_uv_repo, data_dir, "factory/", runner, _LIMITS, head)


def test_setup_run_refuses_when_the_source_head_moved(bare_uv_repo: Path, tmp_path: Path) -> None:
    requested = _head(bare_uv_repo)
    (bare_uv_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    _commit(bare_uv_repo, "move HEAD")
    runner = _Runner()

    with pytest.raises(SetupError, match="source HEAD moved"):
        run_toolchain_setup(bare_uv_repo, tmp_path / "data", "factory/", runner, _LIMITS, requested)

    assert runner.calls == []


# double-waiver: B1 — the real publisher pushes to a Git remote and calls the GitHub API.
class _Publisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def resolve_base_branch(self, source_repo: Path) -> str:
        return "main"

    def publish(
        self,
        *,
        workspace_path: Path,
        branch_name: str,
        base_branch: str,
        commit_message: str,
        title: str,
        body: str,
    ) -> PublishResult:
        self.calls.append(
            {
                "workspace_path": workspace_path,
                "branch_name": branch_name,
                "base_branch": base_branch,
                "commit_message": commit_message,
                "title": title,
                "body": body,
            }
        )
        return PublishResult(
            commit_sha="c" * 40,
            base_branch=base_branch,
            pull_request_url=f"https://github.com/o/r/pull/{len(self.calls)}",
            created_pull_request=True,
        )


def _trigger(repo: Path, data_dir: Path, runner: _Runner, publisher: _Publisher) -> SetupTrigger:
    return SetupTrigger(
        source_repo=repo,
        data_dir=data_dir,
        branch_prefix="factory/",
        limits=_LIMITS,
        command_runner=runner,
        publisher=publisher,
    )


def test_publish_setup_opens_a_review_pull_request_for_the_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    result = run_toolchain_setup(
        bare_uv_repo, tmp_path / "data", "factory/", _Runner(), _LIMITS, _head(bare_uv_repo)
    )
    publisher = _Publisher()

    published = publish_setup(result, publisher, bare_uv_repo)

    assert published.pull_request_url == "https://github.com/o/r/pull/1"
    call = publisher.calls[0]
    assert call["workspace_path"] == result.worktree
    assert call["branch_name"] == result.branch
    assert call["base_branch"] == "main"
    assert call["title"] == "Add missing development tools"
    assert call["commit_message"] == (
        "chore: add missing development tools\n\n"
        "Added by factory setup: ruff, mypy, pytest, mutmut."
    )
    body = str(call["body"])
    assert "- `ruff`" in body and "- `mutmut`" in body
    assert "The factory does not merge this pull request." in body


def test_trigger_opens_a_pull_request_for_a_new_plan(bare_uv_repo: Path, tmp_path: Path) -> None:
    runner = _Runner()
    publisher = _Publisher()

    state = _trigger(bare_uv_repo, tmp_path / "data", runner, publisher).tick()

    assert state == SetupState(
        head_commit=_head(bare_uv_repo),
        commands=(_UV_ADD,),
        pull_request_url="https://github.com/o/r/pull/1",
    )
    assert [call[0] for call in runner.calls] == [_UV_ADD]
    assert len(publisher.calls) == 1


def test_trigger_does_nothing_while_head_stays_the_same(bare_uv_repo: Path, tmp_path: Path) -> None:
    runner = _Runner()
    publisher = _Publisher()
    trigger = _trigger(bare_uv_repo, tmp_path / "data", runner, publisher)
    trigger.tick()

    assert trigger.tick() is None
    assert len(runner.calls) == 1
    assert len(publisher.calls) == 1


def test_trigger_keeps_the_pull_request_when_a_new_head_has_the_same_plan(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    publisher = _Publisher()
    trigger = _trigger(bare_uv_repo, tmp_path / "data", _Runner(), publisher)
    trigger.tick()
    (bare_uv_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    _commit(bare_uv_repo, "unrelated change")

    state = trigger.tick()

    assert state == SetupState(
        head_commit=_head(bare_uv_repo),
        commands=(_UV_ADD,),
        pull_request_url="https://github.com/o/r/pull/1",
    )
    assert len(publisher.calls) == 1


def test_trigger_opens_a_second_pull_request_for_a_changed_plan(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    publisher = _Publisher()
    trigger = _trigger(bare_uv_repo, tmp_path / "data", _Runner(), publisher)
    trigger.tick()
    (bare_uv_repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[tool.ruff]\n', encoding="utf-8"
    )
    _commit(bare_uv_repo, "configure ruff")

    state = trigger.tick()

    assert state is not None
    assert state.commands == ("uv add --dev --no-sync mypy pytest mutmut",)
    assert state.pull_request_url == "https://github.com/o/r/pull/2"


def test_trigger_with_nothing_to_add_creates_no_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    (bare_uv_repo / "uv.lock").unlink()
    _commit(bare_uv_repo, "drop the lockfile")
    data_dir = tmp_path / "data"
    publisher = _Publisher()

    state = _trigger(bare_uv_repo, data_dir, _Runner(), publisher).tick()

    assert state == SetupState(head_commit=_head(bare_uv_repo))
    assert publisher.calls == []
    assert not (data_dir / "workspaces").exists()


def test_trigger_records_a_failed_setup_and_does_not_retry_it(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    runner = _Runner(failing=frozenset({_UV_ADD}))
    publisher = _Publisher()
    trigger = _trigger(bare_uv_repo, tmp_path / "data", runner, publisher)

    state = trigger.tick()
    again = trigger.tick()

    assert state == SetupState(
        head_commit=_head(bare_uv_repo), note=f"setup command failed: {_UV_ADD}"
    )
    assert again is None
    assert publisher.calls == []
    assert len(runner.calls) == 1


def test_cli_publish_needs_pull_requests_enabled(bare_uv_repo: Path, tmp_path: Path) -> None:
    result = cli.invoke(
        app,
        ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(tmp_path), "--publish"],
    )

    assert result.exit_code == CONFIG_ERROR_EXIT_CODE
    assert "--publish needs pull_request.enabled" in result.output


class _FailingPublisher(_Publisher):
    def publish(self, **kwargs: object) -> PublishResult:  # type: ignore[override]
        super().publish(**kwargs)  # type: ignore[arg-type]
        raise OSError("push rejected")


def test_trigger_records_a_failed_publication_and_does_not_retry_it(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    runner = _Runner()
    publisher = _FailingPublisher()
    trigger = _trigger(bare_uv_repo, tmp_path / "data", runner, publisher)

    state = trigger.tick()
    again = trigger.tick()

    assert state == SetupState(
        head_commit=_head(bare_uv_repo), note="setup publish failed: OSError"
    )
    assert again is None
    assert len(publisher.calls) == 1
    assert len(runner.calls) == 1


def test_trigger_records_a_refused_setup_and_does_not_retry_it(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    head = _head(bare_uv_repo)
    holder = GitWorktreeWorkspace(data_dir, bare_uv_repo, f"SETUP-{head[:12]}")
    trigger = _trigger(bare_uv_repo, data_dir, _Runner(), _Publisher())

    with holder:
        state = trigger.tick()

    assert state is not None
    assert state.note is not None
    assert state.note.startswith("setup could not run: another setup run holds the lock")
    assert trigger.tick() is None


def test_trigger_skips_a_tick_while_another_process_checks(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    runner = _Runner()
    trigger = _trigger(bare_uv_repo, data_dir, runner, _Publisher())
    lock_path = trigger._state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True)

    with open(lock_path, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert trigger.tick() is None

    assert runner.calls == []


def test_trigger_ignores_a_damaged_state_file(bare_uv_repo: Path, tmp_path: Path) -> None:
    trigger = _trigger(bare_uv_repo, tmp_path / "data", _Runner(), _Publisher())
    trigger._state_path.parent.mkdir(parents=True)
    trigger._state_path.write_text("{not json", encoding="utf-8")

    state = trigger.tick()

    assert state is not None
    assert state.pull_request_url == "https://github.com/o/r/pull/1"


def test_trigger_with_a_dirty_checkout_plans_in_the_head_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    # The uncommitted edit would hide the missing tools from a checkout plan.
    (bare_uv_repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[dependency-groups]\n'
        'dev = ["ruff", "mypy", "pytest", "mutmut"]\n',
        encoding="utf-8",
    )
    runner = _Runner()

    state = _trigger(bare_uv_repo, tmp_path / "data", runner, _Publisher()).tick()

    assert state is not None
    assert state.commands == (_UV_ADD,)
    assert [call[0] for call in runner.calls] == [_UV_ADD]


def test_publish_refuses_a_change_outside_the_manifest_and_lockfile(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    result = run_toolchain_setup(
        bare_uv_repo,
        tmp_path / "data",
        "factory/",
        _Runner(edits="build-output.txt"),
        _LIMITS,
        _head(bare_uv_repo),
    )
    publisher = _Publisher()

    with pytest.raises(
        SetupError, match="unexpected change in the setup worktree: build-output.txt"
    ):
        publish_setup(result, publisher, bare_uv_repo)

    assert publisher.calls == []


def test_publish_lists_plan_notes_in_the_body(bare_uv_repo: Path, tmp_path: Path) -> None:
    result = run_toolchain_setup(
        bare_uv_repo, tmp_path / "data", "factory/", _Runner(), _LIMITS, _head(bare_uv_repo)
    )
    noted = SetupRunResult(
        plan=result.plan.model_copy(update={"notes": ("javascript lane skipped: x",)}),
        worktree=result.worktree,
        branch=result.branch,
        outcome=result.outcome,
        base_commit=result.base_commit,
    )
    publisher = _Publisher()

    publish_setup(noted, publisher, bare_uv_repo)

    assert "Notes:\n- javascript lane skipped: x" in str(publisher.calls[0]["body"])


def test_changed_paths_lists_both_sides_of_a_rename(bare_uv_repo: Path) -> None:
    _git(bare_uv_repo, "mv", "app.py", "uv.lock.new")

    assert changed_paths(bare_uv_repo) == ("app.py", "uv.lock.new")


def test_a_failure_keeps_the_open_proposal_so_no_duplicate_opens(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    publisher = _Publisher()
    trigger = _trigger(bare_uv_repo, data_dir, _Runner(), publisher)
    trigger.tick()
    (bare_uv_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    _commit(bare_uv_repo, "second head")
    (bare_uv_repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
    holder = GitWorktreeWorkspace(data_dir, bare_uv_repo, f"SETUP-{_head(bare_uv_repo)[:12]}")
    with holder:
        refused = trigger.tick()
    (bare_uv_repo / "notes.txt").unlink()
    (bare_uv_repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    _commit(bare_uv_repo, "third head")

    after = trigger.tick()

    assert refused is not None and refused.pull_request_url == "https://github.com/o/r/pull/1"
    assert after is not None and after.pull_request_url == "https://github.com/o/r/pull/1"
    assert len(publisher.calls) == 1


def test_publish_refuses_a_commit_made_inside_the_setup_worktree(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    result = run_toolchain_setup(
        bare_uv_repo, tmp_path / "data", "factory/", _Runner(), _LIMITS, _head(bare_uv_repo)
    )
    (result.worktree / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _commit(result.worktree, "commit from a build backend")
    publisher = _Publisher()

    with pytest.raises(SetupError, match="commit that the factory did not make"):
        publish_setup(result, publisher, bare_uv_repo)

    assert publisher.calls == []
