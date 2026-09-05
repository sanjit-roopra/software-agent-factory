"""Deterministic repository profiling and built-in skill selection.

The profiler reads only repository paths and a small allowlist of manifests.
It never imports target code, executes commands, contacts the network, or
loads repository-provided skill definitions.
"""

from __future__ import annotations

import configparser
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    AgentRole,
    RepositoryPackageManager,
    RepositoryProfile,
    RepositoryTechnology,
    RepositoryTestTool,
    SelectedSkill,
    SkillId,
)

MAX_SCANNED_FILES = 20_000
MAX_MANIFEST_BYTES = 1_048_576
MAX_SKILL_EVIDENCE = 5

_PRUNED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        ".next",
        "coverage",
        "out",
        "site",
        "target",
        "__pycache__",
    }
)
_VITE_CONFIGS = frozenset(
    {
        "vite.config.js",
        "vite.config.mjs",
        "vite.config.cjs",
        "vite.config.ts",
        "vite.config.mts",
        "vite.config.cts",
    }
)
_VITEST_CONFIGS = frozenset(
    {
        "vitest.config.js",
        "vitest.config.mjs",
        "vitest.config.cjs",
        "vitest.config.ts",
        "vitest.config.mts",
        "vitest.config.cts",
    }
)
_NODE_LOCKFILES = {
    "package-lock.json": RepositoryPackageManager.NPM,
    "pnpm-lock.yaml": RepositoryPackageManager.PNPM,
    "yarn.lock": RepositoryPackageManager.YARN,
    "bun.lock": RepositoryPackageManager.BUN,
    "bun.lockb": RepositoryPackageManager.BUN,
}


@dataclass(frozen=True)
class _SkillSpec:
    id: SkillId
    roles: tuple[AgentRole, ...]
    summary: str
    guidance: tuple[str, ...]


_CATALOG: dict[SkillId, _SkillSpec] = {
    SkillId.PLAN_QUALITY: _SkillSpec(
        id=SkillId.PLAN_QUALITY,
        roles=(AgentRole.PLANNER,),
        summary="Build a repository-grounded, minimal execution plan.",
        guidance=(
            "Name real files or modules supported by repository evidence.",
            "Trace acceptance criteria to implementation and validation steps.",
            "Prefer the smallest coherent change and make uncertainty explicit.",
        ),
    ),
    SkillId.SIMPLIFICATION: _SkillSpec(
        id=SkillId.SIMPLIFICATION,
        roles=(AgentRole.PLANNER, AgentRole.IMPLEMENTER, AgentRole.REVIEWER),
        summary="Remove accidental complexity without changing required behavior.",
        guidance=(
            "Prefer direct control flow and existing abstractions over speculative layers.",
            "Remove duplication only when the duplicated rule would change together.",
            "Do not remove validation, security checks, error handling, or required behavior.",
        ),
    ),
    SkillId.PYTHON_QUALITY: _SkillSpec(
        id=SkillId.PYTHON_QUALITY,
        roles=(
            AgentRole.PLANNER,
            AgentRole.IMPLEMENTER,
            AgentRole.TESTER,
            AgentRole.REVIEWER,
        ),
        summary="Apply modern, typed Python quality practices.",
        guidance=(
            "Follow the repository's Python version, typing, formatting, and test conventions.",
            "Prefer precise exceptions, explicit boundaries, and standard-library solutions.",
            "Avoid dynamic typing escapes and unnecessary framework abstractions.",
        ),
    ),
    SkillId.VITE_QUALITY: _SkillSpec(
        id=SkillId.VITE_QUALITY,
        roles=(
            AgentRole.PLANNER,
            AgentRole.IMPLEMENTER,
            AgentRole.TESTER,
            AgentRole.REVIEWER,
        ),
        summary="Respect the repository's Vite build and module conventions.",
        guidance=(
            "Preserve existing Vite entry points, aliases, environment handling, and scripts.",
            "Do not replace repository tooling or add dependencies without a demonstrated need.",
            "Validate build-facing changes with the repository's configured commands.",
        ),
    ),
    SkillId.REACT_QUALITY: _SkillSpec(
        id=SkillId.REACT_QUALITY,
        roles=(
            AgentRole.PLANNER,
            AgentRole.IMPLEMENTER,
            AgentRole.TESTER,
            AgentRole.REVIEWER,
        ),
        summary="Apply React component, accessibility, and state-boundary practices.",
        guidance=(
            "Prefer accessible, composable components and user-visible behavior.",
            "Keep state local unless sharing is necessary and preserve existing component APIs.",
            "Avoid effects for values that can be derived during rendering.",
        ),
    ),
    SkillId.REACT_REACTIVITY: _SkillSpec(
        id=SkillId.REACT_REACTIVITY,
        roles=(AgentRole.IMPLEMENTER, AgentRole.REVIEWER),
        summary="Check React effects, closures, dependencies, and cleanup.",
        guidance=(
            "Check effect dependencies and stale closures.",
            "Clean up subscriptions, timers, and external resources.",
            "Avoid unstable references that cause unnecessary effects or renders.",
        ),
    ),
    SkillId.REACT_TESTING: _SkillSpec(
        id=SkillId.REACT_TESTING,
        roles=(
            AgentRole.PLANNER,
            AgentRole.IMPLEMENTER,
            AgentRole.TESTER,
            AgentRole.REVIEWER,
        ),
        summary="Test React through observable user behavior.",
        guidance=(
            "Prefer accessible queries and user-visible outcomes over implementation details.",
            "Cover loading, error, empty, and interaction states where relevant.",
            "Avoid snapshot-only coverage and assertions on internal component state.",
        ),
    ),
    SkillId.TESTING_QUALITY: _SkillSpec(
        id=SkillId.TESTING_QUALITY,
        roles=(
            AgentRole.PLANNER,
            AgentRole.IMPLEMENTER,
            AgentRole.TESTER,
            AgentRole.REVIEWER,
        ),
        summary="Use focused regression tests and preserve deterministic test boundaries.",
        guidance=(
            "Add the smallest test that proves the requested behavior and likely regressions.",
            "Prefer stable public behavior over implementation-detail assertions.",
            "Keep tests offline and deterministic unless the repository explicitly "
            "requires otherwise.",
        ),
    ),
}


def profile_repository(repository_root: Path) -> RepositoryProfile:
    """Build a deterministic profile from repository-local evidence."""

    root = repository_root.resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {repository_root}")

    markers: set[str] = set()
    technologies: set[RepositoryTechnology] = set()
    test_tools: set[RepositoryTestTool] = set()
    package_managers: set[RepositoryPackageManager] = set()
    evidence: dict[SkillId, set[str]] = {skill_id: set() for skill_id in SkillId}
    warnings: list[str] = []
    scanned_files = 0
    first_python_source: str | None = None

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in _PRUNED_DIRECTORIES and not (Path(directory) / name).is_symlink()
        )
        for file_name in sorted(file_names):
            scanned_files += 1
            if scanned_files > MAX_SCANNED_FILES:
                warnings.append(f"scan limit reached after {MAX_SCANNED_FILES} files")
                directory_names[:] = []
                break

            path = Path(directory) / file_name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            lower_name = file_name.lower()

            if lower_name.endswith(".py"):
                technologies.add(RepositoryTechnology.PYTHON)
                if first_python_source is None:
                    first_python_source = relative
            if lower_name.endswith((".ts", ".tsx")):
                technologies.add(RepositoryTechnology.TYPESCRIPT)
            if _looks_like_test(relative, lower_name):
                _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)

            if _is_python_marker(lower_name):
                markers.add(relative)
                technologies.add(RepositoryTechnology.PYTHON)
                _record_evidence(evidence, SkillId.PYTHON_QUALITY, relative)
            if lower_name == "uv.lock":
                markers.add(relative)
                package_managers.add(RepositoryPackageManager.UV)
            if lower_name in _NODE_LOCKFILES:
                markers.add(relative)
                package_managers.add(_NODE_LOCKFILES[lower_name])
            if lower_name in _VITE_CONFIGS:
                markers.add(relative)
                technologies.update({RepositoryTechnology.JAVASCRIPT, RepositoryTechnology.VITE})
                _record_evidence(evidence, SkillId.VITE_QUALITY, relative)
            if lower_name in _VITEST_CONFIGS:
                markers.add(relative)
                technologies.add(RepositoryTechnology.JAVASCRIPT)
                test_tools.add(RepositoryTestTool.VITEST)
                _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)
            if lower_name in {"pytest.ini", "conftest.py"}:
                markers.add(relative)
                technologies.add(RepositoryTechnology.PYTHON)
                test_tools.add(RepositoryTestTool.PYTEST)
                _record_evidence(evidence, SkillId.PYTHON_QUALITY, relative)
                _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)

            if lower_name == "pyproject.toml":
                _inspect_pyproject(
                    path,
                    relative,
                    markers,
                    technologies,
                    test_tools,
                    evidence,
                    warnings,
                )
            elif lower_name in {"setup.cfg", "tox.ini"}:
                _inspect_python_ini(
                    path,
                    relative,
                    technologies,
                    test_tools,
                    evidence,
                    warnings,
                )
            elif lower_name == "package.json":
                _inspect_package_json(
                    path,
                    relative,
                    markers,
                    technologies,
                    test_tools,
                    package_managers,
                    evidence,
                    warnings,
                )

        if scanned_files > MAX_SCANNED_FILES:
            break

    if (
        RepositoryTechnology.PYTHON in technologies
        and not evidence[SkillId.PYTHON_QUALITY]
        and first_python_source is not None
    ):
        _record_evidence(evidence, SkillId.PYTHON_QUALITY, first_python_source)
    selected_skills = _select_skills(technologies, test_tools, evidence)
    return RepositoryProfile(
        markers=tuple(sorted(markers)),
        technologies=tuple(sorted(technologies, key=str)),
        test_tools=tuple(sorted(test_tools, key=str)),
        package_managers=tuple(sorted(package_managers, key=str)),
        selected_skills=selected_skills,
        warnings=tuple(warnings),
    )


def skills_for_role(
    profile: RepositoryProfile,
    role: AgentRole,
) -> tuple[SelectedSkill, ...]:
    """Return the profile's skills applicable to one existing agent role."""

    return tuple(skill for skill in profile.selected_skills if role in skill.roles)


def generic_repository_profile(*, warning: str | None = None) -> RepositoryProfile:
    """Return a safe profile when advisory repository detection degrades."""

    evidence: dict[SkillId, set[str]] = {skill_id: set() for skill_id in SkillId}
    return RepositoryProfile(
        selected_skills=_select_skills(set(), set(), evidence),
        warnings=(warning,) if warning else (),
    )


def _select_skills(
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    evidence: dict[SkillId, set[str]],
) -> tuple[SelectedSkill, ...]:
    selected_ids = [SkillId.PLAN_QUALITY, SkillId.SIMPLIFICATION]
    if RepositoryTechnology.PYTHON in technologies:
        selected_ids.append(SkillId.PYTHON_QUALITY)
    if RepositoryTechnology.VITE in technologies:
        selected_ids.append(SkillId.VITE_QUALITY)
    if RepositoryTechnology.REACT in technologies:
        selected_ids.extend(
            [SkillId.REACT_QUALITY, SkillId.REACT_REACTIVITY, SkillId.REACT_TESTING]
        )
    if test_tools or evidence[SkillId.TESTING_QUALITY]:
        selected_ids.append(SkillId.TESTING_QUALITY)

    return tuple(
        SelectedSkill(
            id=spec.id,
            version=1,
            summary=spec.summary,
            roles=spec.roles,
            guidance=spec.guidance,
            evidence=tuple(sorted(evidence[skill_id])),
        )
        for skill_id in selected_ids
        for spec in (_CATALOG[skill_id],)
    )


def _record_evidence(
    evidence: dict[SkillId, set[str]],
    skill_id: SkillId,
    relative_path: str,
) -> None:
    if len(evidence[skill_id]) < MAX_SKILL_EVIDENCE:
        evidence[skill_id].add(relative_path)


def _is_python_marker(file_name: str) -> bool:
    return (
        file_name in {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"}
        or file_name.startswith("requirements-")
        and file_name.endswith(".txt")
    )


def _looks_like_test(relative_path: str, file_name: str) -> bool:
    parts = Path(relative_path).parts
    return (
        any(part in {"test", "tests", "__tests__"} for part in parts[:-1])
        or file_name.startswith("test_")
        or ".test." in file_name
        or ".spec." in file_name
    )


def _read_manifest(path: Path, relative: str, warnings: list[str]) -> bytes | None:
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            warnings.append(f"skipped oversized manifest: {relative}")
            return None
        return path.read_bytes()
    except OSError as exc:
        warnings.append(f"could not read {relative}: {exc}")
        return None


def _inspect_pyproject(
    path: Path,
    relative: str,
    markers: set[str],
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    evidence: dict[SkillId, set[str]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"invalid manifest {relative}: {exc}")
        return

    markers.add(relative)
    technologies.add(RepositoryTechnology.PYTHON)
    _record_evidence(evidence, SkillId.PYTHON_QUALITY, relative)
    dependencies = _pyproject_dependencies(payload)
    tool = payload.get("tool")
    tool_table = tool if isinstance(tool, dict) else {}
    if "pytest" in dependencies or "pytest" in tool_table:
        test_tools.add(RepositoryTestTool.PYTEST)
        _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)


def _pyproject_dependencies(payload: dict[str, Any]) -> set[str]:
    raw_dependencies: list[str] = []
    project = payload.get("project")
    if isinstance(project, dict):
        dependencies = project.get("dependencies")
        if isinstance(dependencies, list):
            raw_dependencies.extend(str(item) for item in dependencies)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    raw_dependencies.extend(str(item) for item in group)
    dependency_groups = payload.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for group in dependency_groups.values():
            if isinstance(group, list):
                raw_dependencies.extend(str(item) for item in group)
    return {_requirement_name(item) for item in raw_dependencies}


def _requirement_name(requirement: str) -> str:
    normalized = requirement.strip().lower()
    for separator in ("[", " ", "<", ">", "=", "!", "~", ";", "@"):
        normalized = normalized.split(separator, 1)[0]
    return normalized.replace("_", "-")


def _inspect_python_ini(
    path: Path,
    relative: str,
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    evidence: dict[SkillId, set[str]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    parser = configparser.ConfigParser()
    try:
        parser.read_string(raw.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        warnings.append(f"invalid config {relative}: {exc}")
        return
    technologies.add(RepositoryTechnology.PYTHON)
    _record_evidence(evidence, SkillId.PYTHON_QUALITY, relative)
    if parser.has_section("pytest") or parser.has_section("tool:pytest"):
        test_tools.add(RepositoryTestTool.PYTEST)
        _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)


def _inspect_package_json(
    path: Path,
    relative: str,
    markers: set[str],
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    package_managers: set[RepositoryPackageManager],
    evidence: dict[SkillId, set[str]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"invalid manifest {relative}: {exc}")
        return
    if not isinstance(payload, dict):
        warnings.append(f"invalid manifest {relative}: expected an object")
        return

    markers.add(relative)
    technologies.add(RepositoryTechnology.JAVASCRIPT)
    dependencies: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        table = payload.get(key)
        if isinstance(table, dict):
            dependencies.update(str(name).lower() for name in table)

    if "typescript" in dependencies:
        technologies.add(RepositoryTechnology.TYPESCRIPT)
    if "react" in dependencies or "react-dom" in dependencies:
        technologies.add(RepositoryTechnology.REACT)
        for skill_id in (
            SkillId.REACT_QUALITY,
            SkillId.REACT_REACTIVITY,
            SkillId.REACT_TESTING,
        ):
            _record_evidence(evidence, skill_id, relative)
    if "vite" in dependencies:
        technologies.add(RepositoryTechnology.VITE)
        _record_evidence(evidence, SkillId.VITE_QUALITY, relative)
    if "vitest" in dependencies:
        test_tools.add(RepositoryTestTool.VITEST)
        _record_evidence(evidence, SkillId.TESTING_QUALITY, relative)

    declared_manager = payload.get("packageManager")
    if isinstance(declared_manager, str):
        manager = declared_manager.split("@", 1)[0].lower()
        try:
            package_managers.add(RepositoryPackageManager(manager))
        except ValueError:
            pass
