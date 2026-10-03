"""Plan the tools a setup run adds to a repository (ADR-034).

The plan is pure data mapping from the toolchain inventory. For each lane
with exactly one package runner, it adds the default provider of every
missing slot and the lane's mutation tool. It never replaces a provider that
the repository already has, and it plans nothing from an incomplete
inventory, because a missing binding is then not proof that a tool is absent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .command_probe import CommandRunner, ProbeLimits, baseline_failure_reason
from .models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSetupPlan,
    ToolchainSlot,
)
from .toolchain_commands import lane_runner, root_version_files

#: The development package that installs each default provider.
PROVIDER_PACKAGES: Mapping[ToolchainProvider, str] = {
    ToolchainProvider.RUFF: "ruff",
    ToolchainProvider.MYPY: "mypy",
    ToolchainProvider.PYTEST: "pytest",
    ToolchainProvider.PRETTIER: "prettier",
    ToolchainProvider.OXLINT: "oxlint",
    ToolchainProvider.TSC: "typescript",
    ToolchainProvider.VITEST: "vitest",
}

#: The package that shows a lane already has its mutation tool.
MUTATION_MARKERS: Mapping[ToolchainLane, str] = {
    ToolchainLane.PYTHON: "mutmut",
    ToolchainLane.JAVASCRIPT: "@stryker-mutator/core",
}

#: Stryker needs a runner plugin for the lane's test provider.
STRYKER_RUNNERS: Mapping[ToolchainProvider, str] = {
    ToolchainProvider.VITEST: "@stryker-mutator/vitest-runner",
    ToolchainProvider.JEST: "@stryker-mutator/jest-runner",
}


def plan_toolchain_setup(
    inventory: ToolchainInventory, profile: RepositoryProfile
) -> ToolchainSetupPlan:
    """Return the add commands for the missing tools of every supported lane."""

    if not inventory.complete:
        return ToolchainSetupPlan(
            manifest_fingerprint=profile.manifest_fingerprint,
            notes=("toolchain inventory is incomplete",),
        )
    declared = {dependency.name.lower() for dependency in profile.dependencies}
    root_files = root_version_files(profile)
    commands: list[str] = []
    packages: list[str] = []
    notes: list[str] = []
    for lane in inventory.lanes:
        runner, skip_note = lane_runner(lane, root_files)
        if runner is None:
            notes.append(skip_note)
            continue
        lane_packages = _missing_packages(lane, inventory, declared)
        if lane_packages:
            commands.append(f"{runner.add_dev} {' '.join(lane_packages)}")
            packages.extend(lane_packages)
    return ToolchainSetupPlan(
        manifest_fingerprint=profile.manifest_fingerprint,
        commands=tuple(commands),
        packages=tuple(packages),
        notes=tuple(notes),
    )


def _missing_packages(
    lane: ToolchainLane, inventory: ToolchainInventory, declared: set[str]
) -> list[str]:
    packages: list[str] = []
    test_provider: ToolchainProvider | None = None
    for binding in inventory.bindings:
        if binding.lane is not lane:
            continue
        provider = binding.provider or binding.default_provider
        if binding.slot is ToolchainSlot.TEST:
            test_provider = provider
        if binding.provider is None:
            package = PROVIDER_PACKAGES.get(binding.default_provider)
            if package is not None and package not in packages:
                packages.append(package)
    marker = MUTATION_MARKERS[lane]
    if marker not in declared:
        packages.append(marker)
        runner_plugin = STRYKER_RUNNERS.get(test_provider) if test_provider else None
        if lane is ToolchainLane.JAVASCRIPT and runner_plugin is not None:
            packages.append(runner_plugin)
    return packages


SETUP_RECORD_PATH = Path(".factory") / "setup.json"


@dataclass(frozen=True)
class SetupOutcome:
    """What applying a setup plan did. ``failed_command`` is set when a command failed."""

    applied: tuple[str, ...]
    failed_command: str | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failed_command is None


def apply_toolchain_setup(
    plan: ToolchainSetupPlan,
    runner: CommandRunner,
    worktree: Path,
    limits: ProbeLimits,
) -> SetupOutcome:
    """Run the plan's add commands in order and record the plan in the worktree.

    The record is written only when every command succeeded. A failed command
    stops the setup and leaves the worktree for inspection.
    """

    applied: list[str] = []
    for command in plan.commands:
        report = runner.run(
            [command],
            worktree,
            limits.timeout_seconds,
            env_passthrough=limits.env_passthrough,
            capture_bytes=limits.capture_bytes,
        )
        if not report.passed:
            reason = baseline_failure_reason(report).replace("on the base commit", "during setup")
            return SetupOutcome(tuple(applied), failed_command=command, failure_reason=reason)
        applied.append(command)
    record = worktree / SETUP_RECORD_PATH
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return SetupOutcome(tuple(applied))
