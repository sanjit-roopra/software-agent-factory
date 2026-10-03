"""Deterministic toolchain registry and inventory (ADR-034).

The registry is data: each lane has slots, and each slot has an ordered list of
recognized providers plus the default provider to add when none is found. The
inventory binds the first provider with evidence. An existing tool is kept and
never replaced by the default.

Evidence comes from the repository profile's dependency declarations and from
root-level configuration files. Like the profiler, the inventory never runs a
command, imports target code or contacts the network.
"""

from __future__ import annotations

import configparser
import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .models import (
    RepositoryProfile,
    RepositoryTechnology,
    RepositoryTestTool,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
    ToolchainSlotBinding,
)

MAX_CONFIG_BYTES = 1_048_576


@dataclass(frozen=True)
class SlotSpec:
    """The ordered providers for one slot. The first provider with evidence wins."""

    providers: tuple[ToolchainProvider, ...]
    default: ToolchainProvider


@dataclass(frozen=True)
class ProviderSignals:
    """Evidence that a provider is configured in the repository."""

    dependencies: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()
    pyproject_tools: frozenset[str] = frozenset()
    ini_sections: frozenset[str] = frozenset()
    package_json_keys: frozenset[str] = frozenset()
    test_tool: RepositoryTestTool | None = None


def _signals(
    *,
    dependencies: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    pyproject_tools: tuple[str, ...] = (),
    ini_sections: tuple[str, ...] = (),
    package_json_keys: tuple[str, ...] = (),
    test_tool: RepositoryTestTool | None = None,
) -> ProviderSignals:
    return ProviderSignals(
        dependencies=frozenset(dependencies),
        files=frozenset(files),
        pyproject_tools=frozenset(pyproject_tools),
        ini_sections=frozenset(ini_sections),
        package_json_keys=frozenset(package_json_keys),
        test_tool=test_tool,
    )


_JS_CONFIG_EXTENSIONS = ("js", "mjs", "cjs", "ts", "mts", "cts")

LANE_SLOTS: Mapping[ToolchainLane, Mapping[ToolchainSlot, SlotSpec]] = {
    ToolchainLane.PYTHON: {
        ToolchainSlot.FORMAT: SlotSpec(
            (ToolchainProvider.BLACK, ToolchainProvider.RUFF), ToolchainProvider.RUFF
        ),
        ToolchainSlot.LINT: SlotSpec(
            (ToolchainProvider.RUFF, ToolchainProvider.FLAKE8, ToolchainProvider.PYLINT),
            ToolchainProvider.RUFF,
        ),
        ToolchainSlot.TYPECHECK: SlotSpec(
            (ToolchainProvider.MYPY, ToolchainProvider.PYRIGHT), ToolchainProvider.MYPY
        ),
        ToolchainSlot.TEST: SlotSpec((ToolchainProvider.PYTEST,), ToolchainProvider.PYTEST),
    },
    ToolchainLane.JAVASCRIPT: {
        ToolchainSlot.FORMAT: SlotSpec(
            (ToolchainProvider.BIOME, ToolchainProvider.PRETTIER), ToolchainProvider.PRETTIER
        ),
        ToolchainSlot.LINT: SlotSpec(
            (ToolchainProvider.ESLINT, ToolchainProvider.BIOME, ToolchainProvider.OXLINT),
            ToolchainProvider.OXLINT,
        ),
        ToolchainSlot.TYPECHECK: SlotSpec((ToolchainProvider.TSC,), ToolchainProvider.TSC),
        ToolchainSlot.TEST: SlotSpec(
            (ToolchainProvider.VITEST, ToolchainProvider.JEST), ToolchainProvider.VITEST
        ),
    },
}
"""The lanes, their slots and the ordered providers for each slot."""

PROVIDER_SIGNALS: Mapping[ToolchainProvider, ProviderSignals] = {
    ToolchainProvider.RUFF: _signals(
        dependencies=("ruff",), files=("ruff.toml", ".ruff.toml"), pyproject_tools=("ruff",)
    ),
    ToolchainProvider.BLACK: _signals(dependencies=("black",), pyproject_tools=("black",)),
    ToolchainProvider.FLAKE8: _signals(
        dependencies=("flake8",), files=(".flake8",), ini_sections=("flake8",)
    ),
    ToolchainProvider.PYLINT: _signals(
        dependencies=("pylint",), files=(".pylintrc", "pylintrc"), pyproject_tools=("pylint",)
    ),
    ToolchainProvider.MYPY: _signals(
        dependencies=("mypy",),
        files=("mypy.ini", ".mypy.ini"),
        pyproject_tools=("mypy",),
        ini_sections=("mypy",),
    ),
    ToolchainProvider.PYRIGHT: _signals(
        dependencies=("pyright",), files=("pyrightconfig.json",), pyproject_tools=("pyright",)
    ),
    ToolchainProvider.PYTEST: _signals(
        dependencies=("pytest",), test_tool=RepositoryTestTool.PYTEST
    ),
    ToolchainProvider.PRETTIER: _signals(
        dependencies=("prettier",),
        files=(
            ".prettierrc",
            ".prettierrc.json",
            ".prettierrc.yaml",
            ".prettierrc.yml",
            ".prettierrc.toml",
            *(f".prettierrc.{ext}" for ext in _JS_CONFIG_EXTENSIONS),
            *(f"prettier.config.{ext}" for ext in _JS_CONFIG_EXTENSIONS),
        ),
        package_json_keys=("prettier",),
    ),
    ToolchainProvider.BIOME: _signals(
        dependencies=("@biomejs/biome",), files=("biome.json", "biome.jsonc")
    ),
    ToolchainProvider.ESLINT: _signals(
        dependencies=("eslint",),
        files=(
            ".eslintrc",
            ".eslintrc.json",
            ".eslintrc.yaml",
            ".eslintrc.yml",
            ".eslintrc.js",
            ".eslintrc.cjs",
            *(f"eslint.config.{ext}" for ext in _JS_CONFIG_EXTENSIONS),
        ),
        package_json_keys=("eslintConfig",),
    ),
    ToolchainProvider.OXLINT: _signals(
        dependencies=("oxlint",), files=(".oxlintrc.json", "oxlint.json")
    ),
    ToolchainProvider.TSC: _signals(dependencies=("typescript",), files=("tsconfig.json",)),
    ToolchainProvider.VITEST: _signals(
        dependencies=("vitest",), test_tool=RepositoryTestTool.VITEST
    ),
    ToolchainProvider.JEST: _signals(
        dependencies=("jest",),
        files=tuple(f"jest.config.{ext}" for ext in (*_JS_CONFIG_EXTENSIONS, "json")),
        package_json_keys=("jest",),
    ),
}
"""How each provider is detected."""


@dataclass
class _RootEvidence:
    files: frozenset[str] = frozenset()
    pyproject_tools: set[str] = field(default_factory=set)
    ini_sections: set[str] = field(default_factory=set)
    package_json_keys: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def inventory_toolchain(repository_root: Path, profile: RepositoryProfile) -> ToolchainInventory:
    """Bind each lane slot to an existing provider or mark it missing."""

    lanes = _lanes(profile)
    root = repository_root.resolve()
    evidence = _read_root_evidence(root) if root.is_dir() else _RootEvidence()
    dependencies = {dependency.name.lower() for dependency in profile.dependencies}
    bindings: list[ToolchainSlotBinding] = []
    for lane in lanes:
        for slot, spec in LANE_SLOTS[lane].items():
            if slot is ToolchainSlot.TYPECHECK and not _needs_typecheck(lane, profile):
                continue
            bindings.append(_bind(lane, slot, spec, profile, dependencies, evidence))
    return ToolchainInventory(
        lanes=lanes,
        bindings=tuple(bindings),
        warnings=tuple(evidence.warnings),
    )


def _lanes(profile: RepositoryProfile) -> tuple[ToolchainLane, ...]:
    technologies = set(profile.technologies)
    lanes: list[ToolchainLane] = []
    if RepositoryTechnology.PYTHON in technologies:
        lanes.append(ToolchainLane.PYTHON)
    if technologies & {
        RepositoryTechnology.JAVASCRIPT,
        RepositoryTechnology.TYPESCRIPT,
        RepositoryTechnology.REACT,
        RepositoryTechnology.VITE,
    }:
        lanes.append(ToolchainLane.JAVASCRIPT)
    return tuple(lanes)


def _needs_typecheck(lane: ToolchainLane, profile: RepositoryProfile) -> bool:
    # Plain JavaScript has no type checker to add. TypeScript does.
    return (
        lane is not ToolchainLane.JAVASCRIPT
        or RepositoryTechnology.TYPESCRIPT in profile.technologies
    )


def _bind(
    lane: ToolchainLane,
    slot: ToolchainSlot,
    spec: SlotSpec,
    profile: RepositoryProfile,
    dependencies: set[str],
    evidence: _RootEvidence,
) -> ToolchainSlotBinding:
    for provider in spec.providers:
        found = _provider_evidence(PROVIDER_SIGNALS[provider], profile, dependencies, evidence)
        if found:
            return ToolchainSlotBinding(
                lane=lane,
                slot=slot,
                provider=provider,
                default_provider=spec.default,
                evidence=found,
            )
    return ToolchainSlotBinding(lane=lane, slot=slot, default_provider=spec.default)


def _provider_evidence(
    signals: ProviderSignals,
    profile: RepositoryProfile,
    dependencies: set[str],
    evidence: _RootEvidence,
) -> tuple[str, ...]:
    found = [
        *(f"dependency:{name}" for name in sorted(signals.dependencies & dependencies)),
        *(f"file:{name}" for name in sorted(signals.files & evidence.files)),
        *(
            f"pyproject:tool.{name}"
            for name in sorted(signals.pyproject_tools & evidence.pyproject_tools)
        ),
        *(f"ini:[{name}]" for name in sorted(signals.ini_sections & evidence.ini_sections)),
        *(
            f"package.json:{key}"
            for key in sorted(signals.package_json_keys & evidence.package_json_keys)
        ),
    ]
    if signals.test_tool is not None and signals.test_tool in profile.test_tools:
        found.append(f"test-tool:{signals.test_tool}")
    return tuple(found)


def _read_root_evidence(root: Path) -> _RootEvidence:
    try:
        names = frozenset(entry.name for entry in root.iterdir() if entry.is_file())
    except OSError as exc:
        return _RootEvidence(warnings=[f"could not list repository root: {exc}"])
    evidence = _RootEvidence(files=names)
    if "pyproject.toml" in names:
        _read_pyproject_tools(root / "pyproject.toml", evidence)
    for ini_name in ("setup.cfg", "tox.ini"):
        if ini_name in names:
            _read_ini_sections(root / ini_name, evidence)
    if "package.json" in names:
        _read_package_json_keys(root / "package.json", evidence)
    return evidence


def _read_text(path: Path, evidence: _RootEvidence) -> str | None:
    try:
        if path.is_symlink():
            return None
        if path.stat().st_size > MAX_CONFIG_BYTES:
            evidence.warnings.append(f"skipped oversized config: {path.name}")
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        evidence.warnings.append(f"could not read {path.name}: {exc}")
        return None


def _read_pyproject_tools(path: Path, evidence: _RootEvidence) -> None:
    text = _read_text(path, evidence)
    if text is None:
        return
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        evidence.warnings.append(f"invalid {path.name}: {exc}")
        return
    tool = payload.get("tool")
    if isinstance(tool, dict):
        evidence.pyproject_tools.update(str(name) for name in tool)


def _read_ini_sections(path: Path, evidence: _RootEvidence) -> None:
    text = _read_text(path, evidence)
    if text is None:
        return
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        evidence.warnings.append(f"invalid {path.name}: {exc}")
        return
    evidence.ini_sections.update(parser.sections())


def _read_package_json_keys(path: Path, evidence: _RootEvidence) -> None:
    text = _read_text(path, evidence)
    if text is None:
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        evidence.warnings.append(f"invalid {path.name}: {exc}")
        return
    if isinstance(payload, dict):
        evidence.package_json_keys.update(str(key) for key in payload)
