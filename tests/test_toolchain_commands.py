from __future__ import annotations

import pytest

from software_agent_factory.models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
    ToolchainSlotBinding,
)
from software_agent_factory.toolchain import LANE_SLOTS
from software_agent_factory.toolchain_commands import (
    CandidateCommands,
    LaneCommands,
    candidate_commands,
)

_PY = ToolchainLane.PYTHON
_JS = ToolchainLane.JAVASCRIPT


def _profile(*version_files: str) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint="0" * 64,
        dependency_fingerprint="0" * 64,
        version_files=version_files,
    )


def _bound(
    lane: ToolchainLane, slot: ToolchainSlot, provider: ToolchainProvider
) -> ToolchainSlotBinding:
    return ToolchainSlotBinding(
        lane=lane,
        slot=slot,
        provider=provider,
        default_provider=LANE_SLOTS[lane][slot].default_provider,
        evidence=(f"dependency:{provider}",),
    )


def _missing(lane: ToolchainLane, slot: ToolchainSlot) -> ToolchainSlotBinding:
    return ToolchainSlotBinding(
        lane=lane, slot=slot, default_provider=LANE_SLOTS[lane][slot].default_provider
    )


def _lane(candidates: CandidateCommands, lane: ToolchainLane) -> LaneCommands:
    matches = [item for item in candidates.lanes if item.lane is lane]
    assert len(matches) == 1, candidates
    return matches[0]


def test_uv_python_lane_runs_bound_tools_in_slot_order() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(
            _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
            _bound(_PY, ToolchainSlot.LINT, ToolchainProvider.RUFF),
            _bound(_PY, ToolchainSlot.FORMAT, ToolchainProvider.RUFF),
            _bound(_PY, ToolchainSlot.TYPECHECK, ToolchainProvider.MYPY),
        ),
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates == CandidateCommands(
        lanes=(
            LaneCommands(
                lane=_PY,
                install=("uv sync --locked",),
                verify=(
                    "CI=true uv run --no-sync ruff format --check .",
                    "CI=true uv run --no-sync ruff check --no-fix .",
                    "CI=true uv run --no-sync mypy .",
                    "CI=true uv run --no-sync pytest -q",
                ),
            ),
        ),
        notes=(),
    )


@pytest.mark.parametrize(
    ("lane", "slot", "provider", "lockfile", "expected"),
    [
        (
            _PY,
            ToolchainSlot.FORMAT,
            ToolchainProvider.BLACK,
            "uv.lock",
            "uv run --no-sync black --check .",
        ),
        (
            _PY,
            ToolchainSlot.LINT,
            ToolchainProvider.FLAKE8,
            "poetry.lock",
            "poetry run flake8 --extend-exclude .venv",
        ),
        (
            _PY,
            ToolchainSlot.TYPECHECK,
            ToolchainProvider.PYRIGHT,
            "uv.lock",
            "uv run --no-sync pyright",
        ),
        (
            _JS,
            ToolchainSlot.FORMAT,
            ToolchainProvider.BIOME,
            "pnpm-lock.yaml",
            "pnpm exec biome format .",
        ),
        (
            _JS,
            ToolchainSlot.LINT,
            ToolchainProvider.BIOME,
            "pnpm-lock.yaml",
            "pnpm exec biome lint .",
        ),
        (
            _JS,
            ToolchainSlot.LINT,
            ToolchainProvider.ESLINT,
            "package-lock.json",
            "npx --no-install eslint .",
        ),
        (
            _JS,
            ToolchainSlot.TYPECHECK,
            ToolchainProvider.TSC,
            "package-lock.json",
            "npx --no-install tsc --noEmit",
        ),
        (
            _JS,
            ToolchainSlot.TEST,
            ToolchainProvider.VITEST,
            "package-lock.json",
            "npx --no-install vitest run",
        ),
        (
            _JS,
            ToolchainSlot.TEST,
            ToolchainProvider.JEST,
            "package-lock.json",
            "npx --no-install jest --ci",
        ),
    ],
)
def test_each_provider_has_a_check_only_command(
    lane: ToolchainLane,
    slot: ToolchainSlot,
    provider: ToolchainProvider,
    lockfile: str,
    expected: str,
) -> None:
    inventory = ToolchainInventory(lanes=(lane,), bindings=(_bound(lane, slot, provider),))

    candidates = candidate_commands(inventory, _profile(lockfile))

    assert _lane(candidates, lane).verify == (f"CI=true {expected}",)


def test_missing_slots_get_no_command_and_no_default_tool() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(
            _missing(_PY, ToolchainSlot.FORMAT),
            _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
        ),
    )

    candidates = candidate_commands(inventory, _profile("poetry.lock"))

    assert _lane(candidates, _PY) == LaneCommands(
        lane=_PY,
        install=("poetry install --no-interaction",),
        verify=("CI=true poetry run pytest -q",),
    )


def test_mypy_with_its_own_file_list_runs_without_a_target() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(_bound(_PY, ToolchainSlot.TYPECHECK, ToolchainProvider.MYPY),),
        self_targeting_providers=(ToolchainProvider.MYPY,),
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert _lane(candidates, _PY).verify == ("CI=true uv run --no-sync mypy",)


def test_pylint_has_no_check_command_so_the_lane_is_skipped() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,), bindings=(_bound(_PY, ToolchainSlot.LINT, ToolchainProvider.PYLINT),)
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates == CandidateCommands(
        lanes=(), notes=("python lane skipped: no bound provider has a check command",)
    )


@pytest.mark.parametrize(
    ("version_files", "note"),
    [
        (("service/uv.lock",), "python lane skipped: no supported lockfile at the repository root"),
        (
            ("uv.lock", "poetry.lock"),
            "python lane skipped: more than one lockfile at the repository root",
        ),
    ],
)
def test_lane_needs_exactly_one_root_lockfile(version_files: tuple[str, ...], note: str) -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,), bindings=(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),)
    )

    candidates = candidate_commands(inventory, _profile(*version_files))

    assert candidates == CandidateCommands(lanes=(), notes=(note,))


def test_incomplete_inventory_derives_nothing() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),),
        complete=False,
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert candidates == CandidateCommands(lanes=(), notes=("toolchain inventory is incomplete",))


def test_repository_without_lanes_says_so() -> None:
    candidates = candidate_commands(ToolchainInventory(), _profile())

    assert candidates == CandidateCommands(lanes=(), notes=("no supported language lane",))


def test_package_script_replaces_the_provider_command() -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(_bound(_JS, ToolchainSlot.LINT, ToolchainProvider.ESLINT),),
        package_json_scripts=("lint",),
    )

    candidates = candidate_commands(inventory, _profile("package-lock.json"))

    assert _lane(candidates, _JS) == LaneCommands(
        lane=_JS, install=("npm ci",), verify=("CI=true npm run lint",)
    )


def test_provider_command_is_used_when_no_script_matches_its_slot() -> None:
    inventory = ToolchainInventory(
        lanes=(_JS,),
        bindings=(_bound(_JS, ToolchainSlot.FORMAT, ToolchainProvider.PRETTIER),),
        package_json_scripts=("lint",),
    )

    candidates = candidate_commands(inventory, _profile("package-lock.json"))

    assert _lane(candidates, _JS).verify == (
        "CI=true npx --no-install prettier --check .",
        "CI=true npm run lint",
    )


@pytest.mark.parametrize(
    ("scripts", "expected"),
    [
        (("check:format", "format:check"), "CI=true pnpm run format:check"),
        (("type-check", "typecheck"), "CI=true pnpm run typecheck"),
    ],
)
def test_script_preference_order(scripts: tuple[str, ...], expected: str) -> None:
    inventory = ToolchainInventory(lanes=(_JS,), package_json_scripts=scripts)

    candidates = candidate_commands(inventory, _profile("pnpm-lock.yaml"))

    assert _lane(candidates, _JS).verify == (expected,)


def test_python_lane_ignores_package_scripts() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY,),
        bindings=(_bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),),
        package_json_scripts=("test",),
    )

    candidates = candidate_commands(inventory, _profile("uv.lock"))

    assert _lane(candidates, _PY).verify == ("CI=true uv run --no-sync pytest -q",)


def test_mixed_repository_derives_each_lane_with_its_runner() -> None:
    inventory = ToolchainInventory(
        lanes=(_PY, _JS),
        bindings=(
            _bound(_PY, ToolchainSlot.TEST, ToolchainProvider.PYTEST),
            _bound(_JS, ToolchainSlot.LINT, ToolchainProvider.OXLINT),
        ),
    )

    candidates = candidate_commands(inventory, _profile("uv.lock", "pnpm-lock.yaml"))

    assert candidates.lanes == (
        LaneCommands(_PY, ("uv sync --locked",), ("CI=true uv run --no-sync pytest -q",)),
        LaneCommands(_JS, ("pnpm install --frozen-lockfile",), ("CI=true pnpm exec oxlint",)),
    )
