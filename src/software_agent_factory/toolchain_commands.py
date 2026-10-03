"""Derive repository commands from the toolchain inventory (ADR-034).

The derivation is pure data mapping. It only names commands for providers the
repository already has. It never names a default provider, because a tool
that the repository does not install cannot run. Every verify command is
meant to check and never write: fixes, snapshot updates and other writes are
turned off with flags and with ``CI=true``.

The controller then runs the candidates of each lane on the unchanged base
commit and keeps only the commands that pass there.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import (
    RepositoryProfile,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
)
from .toolchain import SLOT_SCRIPTS

#: Verify commands run in this slot order: fast static checks before tests.
SLOT_ORDER = (
    ToolchainSlot.FORMAT,
    ToolchainSlot.LINT,
    ToolchainSlot.TYPECHECK,
    ToolchainSlot.TEST,
)

#: Many tools stop writing snapshots or prompting when ``CI`` is set. The
#: factory's command environment does not pass ``CI`` through, so each derived
#: verify command sets it.
CHECK_ENVIRONMENT = "CI=true"


@dataclass(frozen=True)
class PackageRunner:
    """How to install dependencies and run a tool for one package manager."""

    lockfile: str
    install: str
    exec_prefix: str
    script_prefix: str | None = None


PYTHON_RUNNERS: tuple[PackageRunner, ...] = (
    PackageRunner(lockfile="uv.lock", install="uv sync --locked", exec_prefix="uv run --no-sync"),
    PackageRunner(
        lockfile="poetry.lock",
        install="poetry install --no-interaction",
        exec_prefix="poetry run",
    ),
)

JAVASCRIPT_RUNNERS: tuple[PackageRunner, ...] = (
    PackageRunner(
        lockfile="package-lock.json",
        install="npm ci",
        exec_prefix="npx --no-install",
        script_prefix="npm run",
    ),
    PackageRunner(
        lockfile="pnpm-lock.yaml",
        install="pnpm install --frozen-lockfile",
        exec_prefix="pnpm exec",
        script_prefix="pnpm run",
    ),
)

LANE_RUNNERS: Mapping[ToolchainLane, tuple[PackageRunner, ...]] = {
    ToolchainLane.PYTHON: PYTHON_RUNNERS,
    ToolchainLane.JAVASCRIPT: JAVASCRIPT_RUNNERS,
}

#: Check-only commands for each provider and slot. A provider with no entry
#: for a slot gets no command. For example, pylint needs a target to check.
PROVIDER_COMMANDS: Mapping[tuple[ToolchainProvider, ToolchainSlot], str] = {
    (ToolchainProvider.RUFF, ToolchainSlot.FORMAT): "ruff format --check .",
    (ToolchainProvider.BLACK, ToolchainSlot.FORMAT): "black --check .",
    (ToolchainProvider.RUFF, ToolchainSlot.LINT): "ruff check --no-fix .",
    # The install step creates .venv in the worktree. flake8 does not skip it.
    (ToolchainProvider.FLAKE8, ToolchainSlot.LINT): "flake8 --extend-exclude .venv",
    (ToolchainProvider.MYPY, ToolchainSlot.TYPECHECK): "mypy .",
    (ToolchainProvider.PYRIGHT, ToolchainSlot.TYPECHECK): "pyright",
    (ToolchainProvider.PYTEST, ToolchainSlot.TEST): "pytest -q",
    (ToolchainProvider.PRETTIER, ToolchainSlot.FORMAT): "prettier --check .",
    (ToolchainProvider.BIOME, ToolchainSlot.FORMAT): "biome format .",
    (ToolchainProvider.ESLINT, ToolchainSlot.LINT): "eslint .",
    (ToolchainProvider.BIOME, ToolchainSlot.LINT): "biome lint .",
    (ToolchainProvider.OXLINT, ToolchainSlot.LINT): "oxlint",
    (ToolchainProvider.TSC, ToolchainSlot.TYPECHECK): "tsc --noEmit",
    (ToolchainProvider.VITEST, ToolchainSlot.TEST): "vitest run",
    (ToolchainProvider.JEST, ToolchainSlot.TEST): "jest --ci",
}

#: Commands for a provider whose own configuration names the files to check.
SELF_TARGETING_COMMANDS: Mapping[tuple[ToolchainProvider, ToolchainSlot], str] = {
    (ToolchainProvider.MYPY, ToolchainSlot.TYPECHECK): "mypy",
}

INCOMPLETE_INVENTORY_NOTE = "toolchain inventory is incomplete"


@dataclass(frozen=True)
class LaneCommands:
    """The install and verify candidates for one lane."""

    lane: ToolchainLane
    install: tuple[str, ...]
    verify: tuple[str, ...]


@dataclass(frozen=True)
class CandidateCommands:
    """Candidates per lane, plus notes on the lanes that were left out."""

    lanes: tuple[LaneCommands, ...]
    notes: tuple[str, ...]


def candidate_commands(
    inventory: ToolchainInventory, profile: RepositoryProfile
) -> CandidateCommands:
    """Map bound providers to check-only commands, one package runner per lane."""

    if not inventory.complete:
        return CandidateCommands((), (INCOMPLETE_INVENTORY_NOTE,))
    root_version_files = {path for path in profile.version_files if "/" not in path}
    lanes: list[LaneCommands] = []
    notes: list[str] = []
    for lane in inventory.lanes:
        runners = [r for r in LANE_RUNNERS[lane] if r.lockfile in root_version_files]
        if len(runners) != 1:
            reason = "no supported lockfile" if not runners else "more than one lockfile"
            notes.append(f"{lane} lane skipped: {reason} at the repository root")
            continue
        runner = runners[0]
        verify = _lane_verify_commands(lane, runner, inventory)
        if not verify:
            notes.append(f"{lane} lane skipped: no bound provider has a check command")
            continue
        lanes.append(LaneCommands(lane=lane, install=(runner.install,), verify=verify))
    if not inventory.lanes:
        notes.append("no supported language lane")
    return CandidateCommands(tuple(lanes), tuple(notes))


def _lane_verify_commands(
    lane: ToolchainLane, runner: PackageRunner, inventory: ToolchainInventory
) -> tuple[str, ...]:
    commands: list[str] = []
    for slot in SLOT_ORDER:
        command = _script_command(slot, runner, inventory) or _provider_command(
            lane, slot, runner, inventory
        )
        if command is not None:
            commands.append(f"{CHECK_ENVIRONMENT} {command}")
    return tuple(commands)


def _provider_command(
    lane: ToolchainLane,
    slot: ToolchainSlot,
    runner: PackageRunner,
    inventory: ToolchainInventory,
) -> str | None:
    binding = inventory.binding(lane, slot)
    if binding is None or binding.provider is None:
        return None
    key = (binding.provider, slot)
    command = PROVIDER_COMMANDS.get(key)
    if binding.provider in inventory.self_targeting_providers:
        command = SELF_TARGETING_COMMANDS.get(key, command)
    return None if command is None else f"{runner.exec_prefix} {command}"


def _script_command(
    slot: ToolchainSlot, runner: PackageRunner, inventory: ToolchainInventory
) -> str | None:
    if runner.script_prefix is None:
        return None
    for name in SLOT_SCRIPTS[slot]:
        if name in inventory.package_json_scripts:
            return f"{runner.script_prefix} {name}"
    return None
