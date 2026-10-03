"""Plan the tools a setup run adds to a repository (ADR-034).

The plan is pure data mapping from the toolchain inventory. For each lane
with exactly one lockfile, it adds the default provider of every missing slot
and the lane's mutation tool. It never replaces a provider or a mutation tool
that the repository already has, and it plans nothing from an incomplete
inventory, because a missing binding is then not proof that a tool is absent.
"""

from __future__ import annotations

from collections.abc import Mapping

from .models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSetupPlan,
    ToolchainSlot,
)
from .toolchain_commands import (
    INCOMPLETE_INVENTORY_NOTE,
    YARN_BERRY_RUNNER,
    PackageRunner,
    root_version_files,
    select_package_runner,
)

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

#: The package that adds each lane's mutation tool.
MUTATION_PACKAGES: Mapping[ToolchainLane, str] = {
    ToolchainLane.PYTHON: "mutmut",
    ToolchainLane.JAVASCRIPT: "@stryker-mutator/core",
}

#: Stryker needs the plugin for the lane's test runner.
STRYKER_TEST_RUNNER_PLUGINS: Mapping[ToolchainProvider, str] = {
    ToolchainProvider.VITEST: "@stryker-mutator/vitest-runner",
    ToolchainProvider.JEST: "@stryker-mutator/jest-runner",
}

#: pnpm refuses to add to the root of a workspace without this flag.
PNPM_WORKSPACE_ROOT_FLAG = "--workspace-root"
#: Yarn 1 refuses to add to the root of a workspace without this flag.
YARN_WORKSPACE_ROOT_FLAG = "-W"


def plan_toolchain_setup(
    inventory: ToolchainInventory, profile: RepositoryProfile
) -> ToolchainSetupPlan:
    """Return the add commands for the missing tools of every supported lane."""

    if not inventory.complete:
        return ToolchainSetupPlan(
            manifest_fingerprint=profile.manifest_fingerprint,
            notes=(INCOMPLETE_INVENTORY_NOTE,),
        )
    root_files = root_version_files(profile)
    commands: list[str] = []
    packages: list[str] = []
    notes: list[str] = []
    for lane in inventory.lanes:
        package_runner, skip_note = select_package_runner(
            lane, root_files, yarn_berry=inventory.yarn_berry
        )
        if package_runner is None:
            notes.append(skip_note)
            continue
        lane_packages = [
            *_missing_provider_packages(lane, inventory),
            *_missing_mutation_packages(lane, inventory),
        ]
        if lane_packages:
            commands.append(_add_command(package_runner, inventory, lane_packages))
            packages.extend(lane_packages)
    return ToolchainSetupPlan(
        manifest_fingerprint=profile.manifest_fingerprint,
        commands=tuple(commands),
        packages=tuple(packages),
        notes=tuple(notes),
    )


def _missing_provider_packages(lane: ToolchainLane, inventory: ToolchainInventory) -> list[str]:
    packages: list[str] = []
    for binding in inventory.bindings:
        if binding.lane is not lane or not binding.is_missing:
            continue
        package = PROVIDER_PACKAGES[binding.default_provider]
        if package not in packages:
            packages.append(package)
    return packages


def _missing_mutation_packages(lane: ToolchainLane, inventory: ToolchainInventory) -> list[str]:
    if lane in inventory.mutation_tool_lanes:
        return []
    packages = [MUTATION_PACKAGES[lane]]
    test = inventory.binding(lane, ToolchainSlot.TEST)
    test_provider = None if test is None else test.provider or test.default_provider
    plugin = STRYKER_TEST_RUNNER_PLUGINS.get(test_provider) if test_provider else None
    if lane is ToolchainLane.JAVASCRIPT and plugin is not None:
        packages.append(plugin)
    return packages


def _add_command(
    package_runner: PackageRunner, inventory: ToolchainInventory, packages: list[str]
) -> str:
    prefix = package_runner.add_dev
    if inventory.pnpm_workspace and package_runner.lockfile == "pnpm-lock.yaml":
        prefix = f"{prefix} {PNPM_WORKSPACE_ROOT_FLAG}"
    yarn_classic = package_runner.lockfile == "yarn.lock" and package_runner != YARN_BERRY_RUNNER
    if inventory.yarn_workspace and yarn_classic:
        prefix = f"{prefix} {YARN_WORKSPACE_ROOT_FLAG}"
    return f"{prefix} {' '.join(packages)}"
