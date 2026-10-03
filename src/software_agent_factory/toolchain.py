"""Deterministic toolchain registry and inventory (ADR-034).

The registry is data: each lane has slots, and each slot has an ordered list of
recognized providers, the default provider to add when none is found and an
optional technology the slot requires. The inventory binds the first provider
with evidence. An existing tool is kept and never replaced by the default.

Evidence comes from the repository profile's dependency declarations and from
root-level configuration files. Like the profiler, the inventory never runs a
command, imports target code or contacts the network. Configuration parsing
failures become warnings, never exceptions.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from .repository_profile import MAX_MANIFEST_BYTES

#: Profile warnings that mean dependency or technology evidence is incomplete.
#: A missing binding is then not proof that the repository lacks the tool.
INCOMPLETE_PROFILE_WARNING_PREFIXES = (
    "scan limit reached",
    "dependency evidence limited",
    "repository profiling degraded",
    "ignored dependency declaration outside profile limits",
    "could not read",
    "invalid manifest",
    "invalid config",
    "invalid requirements file",
    "skipped oversized manifest",
)

#: Lockfiles add versions, not dependency names, so a lockfile that cannot be
#: read leaves the evidence complete. The profiler reads lockfiles with the
#: same reader as manifests, so its warnings use the same prefixes.
_LOCKFILE_NAMES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "pylock.toml",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
    }
)
_WARNING_PATH = re.compile(r"^(?:could not read |skipped oversized manifest: )(?P<path>[^:]+)")


@dataclass(frozen=True)
class SlotSpec:
    """The ordered providers for one slot. The first provider with evidence wins."""

    providers: tuple[ToolchainProvider, ...]
    default_provider: ToolchainProvider
    requires: RepositoryTechnology | None = None


@dataclass(frozen=True)
class ProviderSignals:
    """Evidence that a provider is configured in the repository."""

    dependencies: frozenset[str] = frozenset()
    files: frozenset[str] = frozenset()
    pyproject_tools: frozenset[str] = frozenset()
    ini_sections: frozenset[str] = frozenset()
    package_json_keys: frozenset[str] = frozenset()
    test_tool: RepositoryTestTool | None = None


def _provider_signals(
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
_INI_FILES = ("setup.cfg", "tox.ini")

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
        ToolchainSlot.TYPECHECK: SlotSpec(
            (ToolchainProvider.TSC,),
            ToolchainProvider.TSC,
            requires=RepositoryTechnology.TYPESCRIPT,
        ),
        ToolchainSlot.TEST: SlotSpec(
            (ToolchainProvider.VITEST, ToolchainProvider.JEST), ToolchainProvider.VITEST
        ),
    },
}
"""The lanes, their slots and the ordered providers for each slot."""

PROVIDER_SIGNALS: Mapping[ToolchainProvider, ProviderSignals] = {
    ToolchainProvider.RUFF: _provider_signals(
        dependencies=("ruff",), files=("ruff.toml", ".ruff.toml"), pyproject_tools=("ruff",)
    ),
    ToolchainProvider.BLACK: _provider_signals(dependencies=("black",), pyproject_tools=("black",)),
    ToolchainProvider.FLAKE8: _provider_signals(
        dependencies=("flake8",), files=(".flake8",), ini_sections=("flake8",)
    ),
    ToolchainProvider.PYLINT: _provider_signals(
        dependencies=("pylint",), files=(".pylintrc", "pylintrc"), pyproject_tools=("pylint",)
    ),
    ToolchainProvider.MYPY: _provider_signals(
        dependencies=("mypy",),
        files=("mypy.ini", ".mypy.ini"),
        pyproject_tools=("mypy",),
        ini_sections=("mypy",),
    ),
    ToolchainProvider.PYRIGHT: _provider_signals(
        dependencies=("pyright",), files=("pyrightconfig.json",), pyproject_tools=("pyright",)
    ),
    ToolchainProvider.PYTEST: _provider_signals(
        dependencies=("pytest",), test_tool=RepositoryTestTool.PYTEST
    ),
    ToolchainProvider.PRETTIER: _provider_signals(
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
    ToolchainProvider.BIOME: _provider_signals(
        dependencies=("@biomejs/biome",), files=("biome.json", "biome.jsonc")
    ),
    ToolchainProvider.ESLINT: _provider_signals(
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
    ToolchainProvider.OXLINT: _provider_signals(
        dependencies=("oxlint",), files=(".oxlintrc.json", "oxlint.json")
    ),
    ToolchainProvider.TSC: _provider_signals(
        dependencies=("typescript",), files=("tsconfig.json",)
    ),
    ToolchainProvider.VITEST: _provider_signals(
        dependencies=("vitest",), test_tool=RepositoryTestTool.VITEST
    ),
    ToolchainProvider.JEST: _provider_signals(
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
    #: ``(file name, section)`` pairs from ``setup.cfg`` and ``tox.ini``.
    ini_sections: set[tuple[str, str]] = field(default_factory=set)
    package_json_keys: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _RepositoryFacts:
    dependency_names: frozenset[str]
    test_tools: frozenset[RepositoryTestTool]
    root_evidence: _RootEvidence


def inventory_toolchain(repository_root: Path, profile: RepositoryProfile) -> ToolchainInventory:
    """Bind each lane slot to an existing provider or mark it missing."""

    lanes = _detect_lanes(profile)
    root = repository_root.resolve()
    facts = _RepositoryFacts(
        dependency_names=frozenset(dependency.name.lower() for dependency in profile.dependencies),
        test_tools=frozenset(profile.test_tools),
        root_evidence=_read_root_evidence(root) if root.is_dir() else _RootEvidence(),
    )
    bindings = [
        _bind(lane, slot, spec, facts)
        for lane in lanes
        for slot, spec in LANE_SLOTS[lane].items()
        if spec.requires is None or spec.requires in profile.technologies
    ]
    # Copy only the fixed prefix. A profile warning can quote repository text.
    evidence_warnings = [w for w in profile.warnings if not _names_a_lockfile(w)]
    incomplete = [
        prefix
        for prefix in INCOMPLETE_PROFILE_WARNING_PREFIXES
        if any(warning.startswith(prefix) for warning in evidence_warnings)
    ]
    return ToolchainInventory(
        lanes=lanes,
        bindings=tuple(bindings),
        complete=not incomplete and not facts.root_evidence.warnings,
        warnings=(*incomplete, *facts.root_evidence.warnings),
    )


def _names_a_lockfile(warning: str) -> bool:
    match = _WARNING_PATH.match(warning)
    return match is not None and Path(match["path"]).name.lower() in _LOCKFILE_NAMES


def degraded_toolchain_inventory(warning: str) -> ToolchainInventory:
    """Return an empty, incomplete inventory when the inventory cannot be built."""

    return ToolchainInventory(complete=False, warnings=(warning,))


def _detect_lanes(profile: RepositoryProfile) -> tuple[ToolchainLane, ...]:
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


def _bind(
    lane: ToolchainLane,
    slot: ToolchainSlot,
    spec: SlotSpec,
    facts: _RepositoryFacts,
) -> ToolchainSlotBinding:
    for provider in spec.providers:
        evidence = _provider_evidence(PROVIDER_SIGNALS[provider], facts)
        if evidence:
            return ToolchainSlotBinding(
                lane=lane,
                slot=slot,
                provider=provider,
                default_provider=spec.default_provider,
                evidence=evidence,
            )
    return ToolchainSlotBinding(lane=lane, slot=slot, default_provider=spec.default_provider)


def _provider_evidence(signals: ProviderSignals, facts: _RepositoryFacts) -> tuple[str, ...]:
    root = facts.root_evidence
    evidence = [
        *(f"dependency:{name}" for name in sorted(signals.dependencies & facts.dependency_names)),
        *(f"file:{name}" for name in sorted(signals.files & root.files)),
        *(
            f"pyproject.toml:tool.{name}"
            for name in sorted(signals.pyproject_tools & root.pyproject_tools)
        ),
        *(
            f"{file_name}:[{section}]"
            for file_name, section in sorted(root.ini_sections)
            if section in signals.ini_sections
        ),
        *(
            f"package.json:{key}"
            for key in sorted(signals.package_json_keys & root.package_json_keys)
        ),
    ]
    if signals.test_tool is not None and signals.test_tool in facts.test_tools:
        evidence.append(f"test-tool:{signals.test_tool}")
    return tuple(evidence)


def _read_root_evidence(root: Path) -> _RootEvidence:
    try:
        names = frozenset(
            entry.name for entry in root.iterdir() if entry.is_file(follow_symlinks=False)
        )
    except OSError as exc:
        return _RootEvidence(warnings=[f"could not list repository root: {type(exc).__name__}"])
    evidence = _RootEvidence(files=names)
    if "pyproject.toml" in names:
        payload = _parse_config(root / "pyproject.toml", tomllib.loads, evidence)
        tool = payload.get("tool") if isinstance(payload, dict) else None
        if isinstance(tool, dict):
            evidence.pyproject_tools.update(str(name) for name in tool)
    for ini_name in _INI_FILES:
        if ini_name in names:
            sections = _parse_config(root / ini_name, _ini_sections, evidence)
            if sections is not None:
                evidence.ini_sections.update((ini_name, section) for section in sections)
    if "package.json" in names:
        payload = _parse_config(root / "package.json", json.loads, evidence)
        if isinstance(payload, dict):
            evidence.package_json_keys.update(str(key) for key in payload)
    return evidence


def _ini_sections(text: str) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser.sections()


def _parse_config(path: Path, parse: Callable[[str], Any], evidence: _RootEvidence) -> Any | None:
    """Parse one untrusted configuration file, or record a warning and return ``None``.

    The warning names the exception type only. Parser messages can quote file
    content, and that content is untrusted.
    """

    text = _read_bounded_text(path, evidence)
    if text is None:
        return None
    try:
        return parse(text)
    except (ValueError, RecursionError, configparser.Error) as exc:
        # TOMLDecodeError and JSONDecodeError are ValueError subclasses. A plain
        # ValueError also covers integers past the interpreter's digit limit.
        evidence.warnings.append(f"invalid {path.name}: {type(exc).__name__}")
        return None


def _read_bounded_text(path: Path, evidence: _RootEvidence) -> str | None:
    """Read a regular file without following a symbolic link and within the size limit."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        evidence.warnings.append(f"could not read {path.name}: {type(exc).__name__}")
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            evidence.warnings.append(f"skipped non-regular config: {path.name}")
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        evidence.warnings.append(f"could not read {path.name}: {type(exc).__name__}")
        return None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_MANIFEST_BYTES:
        evidence.warnings.append(f"skipped oversized config: {path.name}")
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        evidence.warnings.append(f"could not read {path.name}: UnicodeDecodeError")
        return None
