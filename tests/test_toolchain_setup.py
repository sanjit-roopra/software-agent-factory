from __future__ import annotations

import pytest

from software_agent_factory.models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSetupPlan,
    ToolchainSlot,
    ToolchainSlotBinding,
)
from software_agent_factory.toolchain import LANE_SLOTS
from software_agent_factory.toolchain_setup import PROVIDER_PACKAGES, plan_toolchain_setup

_PY = ToolchainLane.PYTHON
_JS = ToolchainLane.JAVASCRIPT
_FINGERPRINT = "a" * 64


def _profile(*version_files: str) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint=_FINGERPRINT,
        dependency_fingerprint="0" * 64,
        version_files=version_files,
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


@pytest.mark.parametrize(
    "provider",
    sorted({spec.default_provider for slots in LANE_SLOTS.values() for spec in slots.values()}),
)
def test_every_default_provider_has_a_package(provider: ToolchainProvider) -> None:
    assert PROVIDER_PACKAGES[provider]


def test_bare_uv_repository_gets_every_default_tool_and_mutmut() -> None:
    inventory = ToolchainInventory(lanes=(_PY,), bindings=_all_missing(_PY))

    plan = plan_toolchain_setup(inventory, _profile("uv.lock"))

    assert plan == ToolchainSetupPlan(
        manifest_fingerprint=_FINGERPRINT,
        commands=("uv add --dev --no-sync ruff mypy pytest mutmut",),
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
        mutation_tool_lanes=(_PY,),
    )

    plan = plan_toolchain_setup(inventory, _profile("poetry.lock"))

    assert plan.commands == ("poetry add --group dev --lock mypy",)


def test_fully_tooled_repository_needs_nothing() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=tuple(
            _binding(_PY, slot, LANE_SLOTS[_PY][slot].default_provider) for slot in LANE_SLOTS[_PY]
        ),
        mutation_tool_lanes=(_PY,),
    )

    plan = plan_toolchain_setup(inventory, _profile("uv.lock"))

    assert plan == ToolchainSetupPlan(manifest_fingerprint=_FINGERPRINT)


@pytest.mark.parametrize(
    ("test_binding", "expected"),
    [
        (
            _binding(_JS, ToolchainSlot.TEST),
            "npm install --save-dev --package-lock-only --ignore-scripts "
            "prettier oxlint vitest @stryker-mutator/core @stryker-mutator/vitest-runner",
        ),
        (
            _binding(_JS, ToolchainSlot.TEST, ToolchainProvider.JEST),
            "npm install --save-dev --package-lock-only --ignore-scripts "
            "prettier oxlint @stryker-mutator/core @stryker-mutator/jest-runner",
        ),
    ],
)
def test_javascript_lane_gets_stryker_with_the_matching_runner(
    test_binding: ToolchainSlotBinding, expected: str
) -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(
            _binding(_JS, ToolchainSlot.FORMAT),
            _binding(_JS, ToolchainSlot.LINT),
            test_binding,
        ),
    )

    plan = plan_toolchain_setup(inventory, _profile("package-lock.json"))

    assert plan.commands == (expected,)


def test_configured_stryker_is_not_added_again() -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,), bindings=(_binding(_JS, ToolchainSlot.LINT),), mutation_tool_lanes=(_JS,)
    )

    plan = plan_toolchain_setup(inventory, _profile("package-lock.json"))

    assert plan.commands == ("npm install --save-dev --package-lock-only --ignore-scripts oxlint",)


@pytest.mark.parametrize(
    ("pnpm_workspace", "expected"),
    [
        (
            False,
            "pnpm add --save-dev --lockfile-only --ignore-scripts typescript @stryker-mutator/core",
        ),
        (
            True,
            "pnpm add --save-dev --lockfile-only --ignore-scripts --workspace-root "
            "typescript @stryker-mutator/core",
        ),
    ],
)
def test_typescript_lane_adds_typescript(pnpm_workspace: bool, expected: str) -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(_binding(_JS, ToolchainSlot.TYPECHECK),),
        pnpm_workspace=pnpm_workspace,
    )

    plan = plan_toolchain_setup(inventory, _profile("pnpm-lock.yaml"))

    assert plan.commands == (expected,)


def test_mixed_repository_adds_tools_per_lane_in_lane_order() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY, _JS),
        bindings=(_binding(_PY, ToolchainSlot.LINT), _binding(_JS, ToolchainSlot.LINT)),
        mutation_tool_lanes=(_PY, _JS),
    )

    plan = plan_toolchain_setup(inventory, _profile("uv.lock", "package-lock.json"))

    assert plan == ToolchainSetupPlan(
        manifest_fingerprint=_FINGERPRINT,
        commands=(
            "uv add --dev --no-sync ruff",
            "npm install --save-dev --package-lock-only --ignore-scripts oxlint",
        ),
        packages=("ruff", "oxlint"),
    )


def test_incomplete_inventory_plans_nothing() -> None:
    inventory = ToolchainInventory(lanes=(_PY,), bindings=_all_missing(_PY), complete=False)

    plan = plan_toolchain_setup(inventory, _profile("uv.lock"))

    assert plan == ToolchainSetupPlan(
        manifest_fingerprint=_FINGERPRINT, notes=("toolchain inventory is incomplete",)
    )


@pytest.mark.parametrize(
    ("lane", "version_files", "note"),
    [
        (
            _PY,
            ("pyproject.toml",),
            "python lane skipped: no supported lockfile at the repository root",
        ),
        (
            _PY,
            ("uv.lock", "Pipfile.lock"),
            "python lane skipped: more than one lockfile at the repository root",
        ),
        (
            _JS,
            ("package-lock.json", "yarn.lock"),
            "javascript lane skipped: more than one lockfile at the repository root",
        ),
        (
            _JS,
            ("yarn.lock",),
            "javascript lane skipped: no supported lockfile at the repository root",
        ),
    ],
)
def test_lane_needs_exactly_one_supported_root_lockfile(
    lane: ToolchainLane, version_files: tuple[str, ...], note: str
) -> None:
    inventory = ToolchainInventory(lanes=(lane,), bindings=_all_missing(lane))

    plan = plan_toolchain_setup(inventory, _profile(*version_files))

    assert plan == ToolchainSetupPlan(manifest_fingerprint=_FINGERPRINT, notes=(note,))


def test_plan_rejects_commands_without_packages() -> None:
    with pytest.raises(ValueError, match="packages exactly when"):
        ToolchainSetupPlan(manifest_fingerprint=_FINGERPRINT, commands=("uv add --dev x",))


def test_javascript_lane_without_a_test_slot_gets_stryker_without_a_runner_plugin() -> None:
    inventory = ToolchainInventory(lanes=(_JS,), bindings=(_binding(_JS, ToolchainSlot.LINT),))

    plan = plan_toolchain_setup(inventory, _profile("package-lock.json"))

    assert plan.packages == ("oxlint", "@stryker-mutator/core")


def test_skipped_lane_does_not_stop_the_other_lane() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY, _JS),
        bindings=(_binding(_PY, ToolchainSlot.LINT), _binding(_JS, ToolchainSlot.LINT)),
        mutation_tool_lanes=(_JS,),
    )

    plan = plan_toolchain_setup(inventory, _profile("uv.lock", "package-lock.json", "yarn.lock"))

    assert plan == ToolchainSetupPlan(
        manifest_fingerprint=_FINGERPRINT,
        commands=("uv add --dev --no-sync ruff mutmut",),
        packages=("ruff", "mutmut"),
        notes=("javascript lane skipped: more than one lockfile at the repository root",),
    )
