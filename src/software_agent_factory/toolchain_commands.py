"""Derive repository commands from the toolchain inventory (ADR-034).

The derivation is pure data mapping. It only names commands for providers the
repository already has. It never names a default provider, because a tool
that the repository does not install cannot run. Every verify command checks
and never writes, so verification cannot change the Git tree.

The controller then runs the candidates on the unchanged base commit and keeps
only the commands that pass there.
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

#: Verify commands run in this slot order: fast static checks before tests.
SLOT_ORDER = (
    ToolchainSlot.FORMAT,
    ToolchainSlot.LINT,
    ToolchainSlot.TYPECHECK,
    ToolchainSlot.TEST,
)


@dataclass(frozen=True)
class Runner:
    """How to install dependencies and run a tool for one package manager."""

    lockfile: str
    install: str
    exec_prefix: str
    script_prefix: str | None = None


PYTHON_RUNNERS: tuple[Runner, ...] = (
    Runner(lockfile="uv.lock", install="uv sync --locked", exec_prefix="uv run --no-sync"),
    Runner(
        lockfile="poetry.lock",
        install="poetry install --no-interaction",
        exec_prefix="poetry run",
    ),
)

JAVASCRIPT_RUNNERS: tuple[Runner, ...] = (
    Runner(
        lockfile="package-lock.json",
        install="npm ci",
        exec_prefix="npx --no-install",
        script_prefix="npm run",
    ),
    Runner(
        lockfile="pnpm-lock.yaml",
        install="pnpm install --frozen-lockfile",
        exec_prefix="pnpm exec",
        script_prefix="pnpm run",
    ),
)

LANE_RUNNERS: Mapping[ToolchainLane, tuple[Runner, ...]] = {
    ToolchainLane.PYTHON: PYTHON_RUNNERS,
    ToolchainLane.JAVASCRIPT: JAVASCRIPT_RUNNERS,
}

#: Check-only commands for each provider and slot. A provider with no entry
#: for a slot gets no command. For example, pylint needs a target to check.
PROVIDER_COMMANDS: Mapping[tuple[ToolchainProvider, ToolchainSlot], str] = {
    (ToolchainProvider.RUFF, ToolchainSlot.FORMAT): "ruff format --check .",
    (ToolchainProvider.BLACK, ToolchainSlot.FORMAT): "black --check .",
    (ToolchainProvider.RUFF, ToolchainSlot.LINT): "ruff check .",
    (ToolchainProvider.FLAKE8, ToolchainSlot.LINT): "flake8",
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
    (ToolchainProvider.JEST, ToolchainSlot.TEST): "jest",
}

#: ``package.json`` scripts that replace the provider command for a slot,
#: in order of preference. A script is the command the repository chose.
SLOT_SCRIPTS: Mapping[ToolchainSlot, tuple[str, ...]] = {
    ToolchainSlot.FORMAT: ("format:check", "check:format"),
    ToolchainSlot.LINT: ("lint",),
    ToolchainSlot.TYPECHECK: ("typecheck", "type-check"),
    ToolchainSlot.TEST: ("test",),
}

#: mypy with its own configuration checks the files the configuration names.
_MYPY_CONFIG_EVIDENCE = (
    "pyproject.toml:tool.mypy",
    "file:mypy.ini",
    "file:.mypy.ini",
    "setup.cfg:[mypy]",
)


@dataclass(frozen=True)
class CandidateCommands:
    """Commands to try on the base commit, plus notes on what was left out."""

    install: tuple[str, ...]
    verify: tuple[str, ...]
    notes: tuple[str, ...]


def candidate_commands(
    inventory: ToolchainInventory, profile: RepositoryProfile
) -> CandidateCommands:
    """Map bound providers to check-only commands, one runner per lane."""

    if not inventory.complete:
        return CandidateCommands((), (), ("toolchain inventory is incomplete",))
    root_lockfiles = {path for path in profile.version_files if "/" not in path}
    install: list[str] = []
    verify: list[str] = []
    notes: list[str] = []
    for lane in inventory.lanes:
        runners = [r for r in LANE_RUNNERS[lane] if r.lockfile in root_lockfiles]
        if len(runners) != 1:
            reason = "no supported lockfile" if not runners else "more than one lockfile"
            notes.append(f"{lane} lane skipped: {reason} at the repository root")
            continue
        runner = runners[0]
        lane_verify = _lane_verify_commands(lane, runner, inventory)
        if not lane_verify:
            notes.append(f"{lane} lane skipped: no bound provider has a check command")
            continue
        install.append(runner.install)
        verify.extend(lane_verify)
    return CandidateCommands(tuple(install), tuple(verify), tuple(notes))


def _lane_verify_commands(
    lane: ToolchainLane, runner: Runner, inventory: ToolchainInventory
) -> list[str]:
    commands: list[str] = []
    for slot in SLOT_ORDER:
        script = _script_for(slot, runner, inventory)
        if script is not None:
            commands.append(script)
            continue
        binding = inventory.binding(lane, slot)
        if binding is None or binding.provider is None:
            continue
        command = PROVIDER_COMMANDS.get((binding.provider, slot))
        if command is None:
            continue
        if binding.provider is ToolchainProvider.MYPY and any(
            item in _MYPY_CONFIG_EVIDENCE for item in binding.evidence
        ):
            command = "mypy"
        commands.append(f"{runner.exec_prefix} {command}")
    return commands


def _script_for(slot: ToolchainSlot, runner: Runner, inventory: ToolchainInventory) -> str | None:
    if runner.script_prefix is None:
        return None
    for name in SLOT_SCRIPTS[slot]:
        if name in inventory.package_json_scripts:
            return f"{runner.script_prefix} {name}"
    return None
