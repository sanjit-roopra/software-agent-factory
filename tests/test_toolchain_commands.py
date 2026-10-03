from __future__ import annotations

from software_agent_factory.models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
    ToolchainSlotBinding,
)
from software_agent_factory.toolchain_commands import candidate_commands


def _profile(*version_files: str) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint="0" * 64,
        dependency_fingerprint="0" * 64,
        version_files=version_files,
    )


def _bound(
    lane: ToolchainLane,
    slot: ToolchainSlot,
    provider: ToolchainProvider,
    *evidence: str,
) -> ToolchainSlotBinding:
    return ToolchainSlotBinding(
        lane=lane,
        slot=slot,
        provider=provider,
        default_provider=provider,
        evidence=evidence or (f"dependency:{provider}",),
    )


def _missing(lane: ToolchainLane, slot: ToolchainSlot) -> ToolchainSlotBinding:
    return ToolchainSlotBinding(lane=lane, slot=slot, default_provider=ToolchainProvider.RUFF)


_PY = ToolchainLane.PYTHON
_JS = ToolchainLane.JAVASCRIPT


def _python_inventory(*bindings: ToolchainSlotBinding) -> ToolchainInventory:
    return ToolchainInventory(lanes=(_PY,), bindings=bindings)


def test_uv_python_lane_runs_bound_tools_in_slot_order() -> None:
    inventory = _python_inventory(
        _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
        _bound(_PY, ToolchainSlot.LINT, ToolchainProvider.RUFF),
        _bound(_PY, ToolchainSlot.FORMAT, ToolchainProvider.RUFF),
        _bound(_PY, ToolchainSlot.TYPECHECK, ToolchainProvider.MYPY),
    )

    candidates = candidate_commands(inventory, _profile("pyproject.toml", "uv.lock"))

    assert candidates.install == ("uv sync --locked",)
    assert candidates.verify == (
        "uv run --no-sync ruff format --check .",
        "uv run --no-sync ruff check .",
        "uv run --no-sync mypy .",
        "uv run --no-sync pytest -q",
    )
    assert candidates.notes == ()


def test_missing_slots_get_no_command_and_no_default_tool() -> None:
    inventory = _python_inventory(
        _missing(_PY, ToolchainSlot.FORMAT),
        _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
    )

    candidates = candidate_commands(inventory, _profile("poetry.lock"))

    assert candidates.install == ("poetry install --no-interaction",)
    assert candidates.verify == ("poetry run pytest -q",)


def test_configured_mypy_uses_its_own_file_list() -> None:
    inventory = _python_inventory(
        _bound(_PY, ToolchainSlot.TYPECHECK, ToolchainProvider.MYPY, "pyproject.toml:tool.mypy")
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates.verify == ("uv run --no-sync mypy",)


def test_pylint_has_no_check_command_so_the_lane_is_skipped() -> None:
    inventory = _python_inventory(_bound(_PY, ToolchainSlot.LINT, ToolchainProvider.PYLINT))

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates.install == ()
    assert candidates.verify == ()
    assert candidates.notes == ("python lane skipped: no bound provider has a check command",)


def test_lane_without_a_root_lockfile_is_skipped() -> None:
    inventory = _python_inventory(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST))

    candidates = candidate_commands(inventory, _profile("pyproject.toml", "service/uv.lock"))

    assert candidates.verify == ()
    assert candidates.notes == (
        "python lane skipped: no supported lockfile at the repository root",
    )


def test_lane_with_two_lockfiles_is_skipped() -> None:
    inventory = _python_inventory(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST))

    candidates = candidate_commands(inventory, _profile("uv.lock", "poetry.lock"))

    assert candidates.notes == (
        "python lane skipped: more than one lockfile at the repository root",
    )


def test_incomplete_inventory_derives_nothing() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),),
        complete=False,
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates.verify == ()
    assert candidates.notes == ("toolchain inventory is incomplete",)


def test_package_scripts_replace_provider_commands() -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(
            _bound(_JS, ToolchainSlot.FORMAT, ToolchainProvider.PRETTIER),
            _bound(_JS, ToolchainSlot.LINT, ToolchainProvider.ESLINT),
            _bound(_JS, ToolchainSlot.TYPECHECK, ToolchainProvider.TSC),
            _bound(_JS, ToolchainSlot.TEST, ToolchainProvider.VITEST),
        ),
        package_json_scripts=("lint", "test", "type-check"),
    )

    candidates = candidate_commands(inventory, _profile("package.json", "package-lock.json"))

    assert candidates.install == ("npm ci",)
    assert candidates.verify == (
        "npx --no-install prettier --check .",
        "npm run lint",
        "npm run type-check",
        "npm run test",
    )


def test_script_without_a_bound_provider_still_runs() -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(_missing(_JS, ToolchainSlot.TEST),),
        package_json_scripts=("test",),
    )

    candidates = candidate_commands(inventory, _profile("pnpm-lock.yaml"))

    assert candidates.install == ("pnpm install --frozen-lockfile",)
    assert candidates.verify == ("pnpm run test",)


def test_mixed_repository_derives_each_lane_with_its_runner() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY, _JS),
        bindings=(
            _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
            _bound(_JS, ToolchainSlot.LINT, ToolchainProvider.OXLINT),
        ),
    )

    candidates = candidate_commands(inventory, _profile("uv.lock", "pnpm-lock.yaml"))

    assert candidates.install == ("uv sync --locked", "pnpm install --frozen-lockfile")
    assert candidates.verify == ("uv run --no-sync pytest -q", "pnpm exec oxlint")
