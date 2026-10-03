from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from software_agent_factory.cli import app
from software_agent_factory.command_probe import ProbeLimits
from software_agent_factory.models import (
    CommandResult,
    DependencyEcosystem,
    RepositoryDependency,
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSetupPlan,
    ToolchainSlot,
    ToolchainSlotBinding,
    VerificationReport,
)
from software_agent_factory.toolchain import LANE_SLOTS
from software_agent_factory.toolchain_setup import (
    SETUP_RECORD_PATH,
    apply_toolchain_setup,
    plan_toolchain_setup,
)

_PY = ToolchainLane.PYTHON
_JS = ToolchainLane.JAVASCRIPT
_FINGERPRINT = "a" * 64
_LIMITS = ProbeLimits(timeout_seconds=60, env_passthrough=(), capture_bytes=1024)
cli = CliRunner()


def _profile(*version_files: str, dependencies: tuple[str, ...] = ()) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint=_FINGERPRINT,
        dependency_fingerprint="0" * 64,
        version_files=version_files,
        dependencies=tuple(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.PYTHON,
                name=name,
                declared_version="*",
                manifest_path="pyproject.toml",
                group="dev",
            )
            for name in dependencies
        ),
    )


def _binding(
    lane: ToolchainLane, slot: ToolchainSlot, provider: ToolchainProvider | None = None
) -> ToolchainSlotBinding:
    return ToolchainSlotBinding(
        lane=lane,
        slot=slot,
        provider=provider,
        default_provider=LANE_SLOTS[lane][slot].default_provider,
        evidence=(f"dependency:{provider}",) if provider else (),
    )


def _all_missing(lane: ToolchainLane) -> tuple[ToolchainSlotBinding, ...]:
    return tuple(_binding(lane, slot) for slot in LANE_SLOTS[lane])


def test_bare_uv_repository_gets_every_default_tool_and_mutmut() -> None:
    inventory = ToolchainInventory(lanes=(_PY,), bindings=_all_missing(_PY))

    plan = plan_toolchain_setup(inventory, _profile("uv.lock"))

    assert plan == ToolchainSetupPlan(
        manifest_fingerprint=_FINGERPRINT,
        commands=("uv add --dev ruff mypy pytest mutmut",),
        packages=("ruff", "mypy", "pytest", "mutmut"),
    )


def test_existing_tools_are_kept_and_not_added_again() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(
            _binding(_PY, ToolchainSlot.FORMAT, ToolchainProvider.BLACK),
            _binding(_PY, ToolchainSlot.LINT, ToolchainProvider.FLAKE8),
            _binding(_PY, ToolchainSlot.TYPECHECK),
            _binding(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
        ),
    )

    plan = plan_toolchain_setup(inventory, _profile("poetry.lock", dependencies=("mutmut",)))

    assert plan.commands == ("poetry add --group dev mypy",)


def test_fully_tooled_repository_needs_nothing() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=tuple(
            _binding(_PY, slot, LANE_SLOTS[_PY][slot].default_provider) for slot in LANE_SLOTS[_PY]
        ),
    )

    plan = plan_toolchain_setup(inventory, _profile("uv.lock", dependencies=("mutmut",)))

    assert plan.is_empty
    assert plan.packages == ()


@pytest.mark.parametrize(
    ("test_provider", "runner_plugin"),
    [
        (None, "@stryker-mutator/vitest-runner"),
        (ToolchainProvider.JEST, "@stryker-mutator/jest-runner"),
    ],
)
def test_javascript_lane_gets_stryker_with_the_matching_runner(
    test_provider: ToolchainProvider | None, runner_plugin: str
) -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(
            _binding(_JS, ToolchainSlot.FORMAT),
            _binding(_JS, ToolchainSlot.LINT),
            _binding(_JS, ToolchainSlot.TEST, test_provider),
        ),
    )

    plan = plan_toolchain_setup(inventory, _profile("package-lock.json"))

    expected_test = () if test_provider else ("vitest",)
    assert plan.commands == (
        "npm install --save-dev "
        + " ".join(("prettier", "oxlint", *expected_test, "@stryker-mutator/core", runner_plugin)),
    )


def test_typescript_lane_adds_typescript() -> None:
    inventory = ToolchainInventory(lanes=(_JS,), bindings=(_binding(_JS, ToolchainSlot.TYPECHECK),))

    plan = plan_toolchain_setup(inventory, _profile("pnpm-lock.yaml"))

    assert plan.commands == ("pnpm add --save-dev typescript @stryker-mutator/core",)


def test_incomplete_inventory_plans_nothing() -> None:
    inventory = ToolchainInventory(lanes=(_PY,), bindings=_all_missing(_PY), complete=False)

    plan = plan_toolchain_setup(inventory, _profile("uv.lock"))

    assert plan.is_empty
    assert plan.notes == ("toolchain inventory is incomplete",)


def test_lane_without_a_root_lockfile_is_skipped_with_a_note() -> None:
    inventory = ToolchainInventory(lanes=(_PY,), bindings=_all_missing(_PY))

    plan = plan_toolchain_setup(inventory, _profile("pyproject.toml"))

    assert plan.is_empty
    assert plan.notes == ("python lane skipped: no supported lockfile at the repository root",)


# double-waiver: B1 — the real runner spawns package-manager subprocesses.
class _Runner:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.commands: list[str] = []

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
        self.commands.append(command)
        exit_code = 1 if command in self.failing else 0
        return VerificationReport(
            passed=exit_code == 0,
            deterministic_checks=[
                CommandResult(
                    command=command,
                    exit_code=exit_code,
                    stdout="secret",
                    stderr="secret",
                    duration_seconds=0.0,
                )
            ],
            failures=[] if exit_code == 0 else ["x"],
            confidence=1.0,
        )


_TWO_LANES = ToolchainSetupPlan(
    manifest_fingerprint=_FINGERPRINT,
    commands=("uv add --dev ruff", "npm install --save-dev oxlint"),
    packages=("ruff", "oxlint"),
)


def test_apply_runs_every_command_and_records_the_plan(tmp_path: Path) -> None:
    runner = _Runner()

    outcome = apply_toolchain_setup(_TWO_LANES, runner, tmp_path, _LIMITS)

    assert outcome.succeeded
    assert runner.commands == list(_TWO_LANES.commands)
    record = json.loads((tmp_path / SETUP_RECORD_PATH).read_text(encoding="utf-8"))
    assert ToolchainSetupPlan.model_validate(record) == _TWO_LANES


def test_apply_stops_at_the_first_failure_and_writes_no_record(tmp_path: Path) -> None:
    runner = _Runner(failing=frozenset({"uv add --dev ruff"}))

    outcome = apply_toolchain_setup(_TWO_LANES, runner, tmp_path, _LIMITS)

    assert not outcome.succeeded
    assert outcome.failed_command == "uv add --dev ruff"
    assert outcome.failure_reason == "failed during setup with exit code 1"
    assert runner.commands == ["uv add --dev ruff"]
    assert not (tmp_path / SETUP_RECORD_PATH).exists()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def bare_uv_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
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
        "-m",
        "init",
    )
    return repo


def test_cli_dry_run_prints_the_plan_and_changes_nothing(
    bare_uv_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"

    result = cli.invoke(
        app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert result.output == "add: uv add --dev ruff mypy pytest mutmut\n"
    assert not (data_dir / "workspaces").exists()


def test_cli_setup_failure_keeps_the_worktree_and_leaves_the_source_alone(
    bare_uv_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only git is on PATH, so the real package-manager command fails fast.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = shutil.which("git")
    assert git is not None
    os.symlink(git, bin_dir / "git")
    monkeypatch.setenv("PATH", str(bin_dir))
    data_dir = tmp_path / "data"

    result = cli.invoke(app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir)])

    assert result.exit_code == 1, result.output
    assert "setup command failed: uv add --dev ruff mypy pytest mutmut" in result.output
    assert "worktree kept at" in result.output
    assert (bare_uv_repo / "pyproject.toml").read_text() == '[project]\nname = "demo"\n'


def test_cli_setup_with_nothing_to_add_runs_no_command(bare_uv_repo: Path, tmp_path: Path) -> None:
    (bare_uv_repo / "uv.lock").unlink()
    _git(
        bare_uv_repo,
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "user.name=t",
        "commit",
        "-qam",
        "drop lockfile",
    )
    data_dir = tmp_path / "data"

    result = cli.invoke(app, ["setup", "--repo", str(bare_uv_repo), "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert result.output == (
        "note: python lane skipped: no supported lockfile at the repository root\nnothing to add\n"
    )
