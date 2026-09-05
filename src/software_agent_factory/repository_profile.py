"""Deterministic repository profiling for version-aware skill research.

The profiler reads only repository paths and a small allowlist of manifests.
It never imports target code, executes commands, contacts the network, or
loads repository-provided skill definitions.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import (
    DependencyEcosystem,
    RepositoryDependency,
    RepositoryPackageManager,
    RepositoryProfile,
    RepositoryTechnology,
    RepositoryTestTool,
)

MAX_SCANNED_FILES = 20_000
MAX_MANIFEST_BYTES = 1_048_576
MAX_FINGERPRINT_BYTES = 16_777_216
MAX_DECLARED_DEPENDENCIES = 200
MAX_PROFILE_PATHS = 100
MAX_TEST_MARKERS = 5
_DEPENDENCY_NAME_PATTERN = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*$")

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
_VERSION_FILES = frozenset(
    {
        "bun.lock",
        "bun.lockb",
        "package-lock.json",
        "package.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pylock.toml",
        "pyproject.toml",
        "uv.lock",
        "yarn.lock",
    }
)


def profile_repository(repository_root: Path) -> RepositoryProfile:
    """Build a deterministic profile from repository-local evidence."""

    root = repository_root.resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {repository_root}")

    markers: set[str] = set()
    technologies: set[RepositoryTechnology] = set()
    test_tools: set[RepositoryTestTool] = set()
    package_managers: set[RepositoryPackageManager] = set()
    dependencies: list[RepositoryDependency] = []
    resolutions: dict[
        tuple[DependencyEcosystem, str],
        set[tuple[str, str]],
    ] = {}
    version_digests: dict[str, str] = {}
    warnings: list[str] = []
    scanned_files = 0
    test_markers = 0

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

            if lower_name in _VERSION_FILES or _is_python_marker(lower_name):
                digest = _fingerprint_file(path, relative, warnings)
                if digest is not None:
                    version_digests[relative] = digest

            if lower_name.endswith(".py"):
                technologies.add(RepositoryTechnology.PYTHON)
            if lower_name.endswith((".ts", ".tsx")):
                technologies.add(RepositoryTechnology.TYPESCRIPT)
            if _looks_like_test(relative, lower_name) and test_markers < MAX_TEST_MARKERS:
                markers.add(relative)
                test_markers += 1

            if _is_python_marker(lower_name):
                markers.add(relative)
                technologies.add(RepositoryTechnology.PYTHON)
            if lower_name == "uv.lock":
                markers.add(relative)
                package_managers.add(RepositoryPackageManager.UV)
                _inspect_uv_lock(path, relative, resolutions, warnings)
            if lower_name == "poetry.lock":
                markers.add(relative)
                package_managers.add(RepositoryPackageManager.POETRY)
            if lower_name in _NODE_LOCKFILES:
                markers.add(relative)
                package_managers.add(_NODE_LOCKFILES[lower_name])
                if lower_name == "package-lock.json":
                    _inspect_package_lock(path, relative, resolutions, warnings)
                elif lower_name == "pnpm-lock.yaml":
                    _inspect_pnpm_lock(path, relative, resolutions, warnings)
            if lower_name in _VITE_CONFIGS:
                markers.add(relative)
                technologies.update({RepositoryTechnology.JAVASCRIPT, RepositoryTechnology.VITE})
            if lower_name in _VITEST_CONFIGS:
                markers.add(relative)
                technologies.add(RepositoryTechnology.JAVASCRIPT)
                test_tools.add(RepositoryTestTool.VITEST)
            if lower_name in {"pytest.ini", "conftest.py"}:
                markers.add(relative)
                technologies.add(RepositoryTechnology.PYTHON)
                test_tools.add(RepositoryTestTool.PYTEST)

            if lower_name == "pyproject.toml":
                _inspect_pyproject(
                    path,
                    relative,
                    markers,
                    technologies,
                    test_tools,
                    package_managers,
                    dependencies,
                    warnings,
                )
            elif _is_requirements_file(lower_name):
                package_managers.add(RepositoryPackageManager.PIP)
                _inspect_requirements_file(
                    path,
                    relative,
                    technologies,
                    test_tools,
                    dependencies,
                    warnings,
                )
            elif lower_name in {"setup.cfg", "tox.ini"}:
                _inspect_python_ini(
                    path,
                    relative,
                    technologies,
                    test_tools,
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
                    dependencies,
                    warnings,
                )

        if scanned_files > MAX_SCANNED_FILES:
            break

    resolved_dependencies = _apply_resolutions(dependencies, resolutions, warnings)
    normalized_dependencies = tuple(
        sorted(
            resolved_dependencies[:MAX_DECLARED_DEPENDENCIES],
            key=lambda item: (
                item.ecosystem,
                item.manifest_path,
                item.group,
                item.name,
                item.declared_version,
            ),
        )
    )
    if len(dependencies) > MAX_DECLARED_DEPENDENCIES:
        warnings.append(f"dependency evidence limited to {MAX_DECLARED_DEPENDENCIES} declarations")
    version_files = _bounded_version_files(version_digests, normalized_dependencies)
    if len(version_digests) > len(version_files):
        warnings.append(f"version file paths limited to {MAX_PROFILE_PATHS} entries")
    if len(markers) > MAX_PROFILE_PATHS:
        warnings.append(f"repository markers limited to {MAX_PROFILE_PATHS} entries")
    manifest_fingerprint = _manifest_fingerprint(version_digests)
    dependency_fingerprint = _dependency_fingerprint(
        technologies,
        test_tools,
        package_managers,
        normalized_dependencies,
    )
    return RepositoryProfile(
        manifest_fingerprint=manifest_fingerprint,
        dependency_fingerprint=dependency_fingerprint,
        markers=tuple(sorted(markers)[:MAX_PROFILE_PATHS]),
        version_files=version_files,
        technologies=tuple(sorted(technologies, key=str)),
        test_tools=tuple(sorted(test_tools, key=str)),
        package_managers=tuple(sorted(package_managers, key=str)),
        dependencies=normalized_dependencies,
        warnings=tuple(warnings),
    )


def _bounded_version_files(
    version_digests: dict[str, str],
    dependencies: tuple[RepositoryDependency, ...],
) -> tuple[str, ...]:
    relevant_paths = {dependency.manifest_path for dependency in dependencies} | {
        dependency.resolution_path
        for dependency in dependencies
        if dependency.resolution_path is not None
    }
    ordered = [
        *sorted(path for path in relevant_paths if path in version_digests),
        *sorted(path for path in version_digests if path not in relevant_paths),
    ]
    return tuple(ordered[:MAX_PROFILE_PATHS])


def generic_repository_profile(*, warning: str | None = None) -> RepositoryProfile:
    """Return a safe profile when repository detection degrades."""

    return RepositoryProfile(
        manifest_fingerprint=_manifest_fingerprint({}),
        dependency_fingerprint=_dependency_fingerprint(set(), set(), set(), ()),
        warnings=(warning,) if warning else (),
    )


def _manifest_fingerprint(version_digests: dict[str, str]) -> str:
    canonical = json.dumps(
        sorted(version_digests.items()),
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"repository-manifests-v1\0" + canonical).hexdigest()


def _dependency_fingerprint(
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    package_managers: set[RepositoryPackageManager],
    dependencies: tuple[RepositoryDependency, ...],
) -> str:
    payload = {
        "detector_version": 2,
        "technologies": sorted(str(item) for item in technologies),
        "test_tools": sorted(str(item) for item in test_tools),
        "package_managers": sorted(str(item) for item in package_managers),
        "dependencies": [dependency.model_dump(mode="json") for dependency in dependencies],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"repository-dependencies-v1\0" + canonical).hexdigest()


def _fingerprint_file(path: Path, relative: str, warnings: list[str]) -> str | None:
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            if size <= MAX_FINGERPRINT_BYTES:
                while chunk := handle.read(65_536):
                    digest.update(chunk)
            else:
                digest.update(handle.read(65_536))
                handle.seek(-65_536, os.SEEK_END)
                digest.update(handle.read(65_536))
                warnings.append(
                    f"fingerprinted only the edges of oversized version file: {relative}"
                )
        return digest.hexdigest()
    except OSError as exc:
        warnings.append(f"could not fingerprint {relative}: {exc}")
        return None


def _append_dependency(
    dependencies: list[RepositoryDependency],
    *,
    ecosystem: DependencyEcosystem,
    name: str,
    declared_version: str,
    manifest_path: str,
    group: str,
    warnings: list[str],
) -> None:
    if len(dependencies) >= MAX_DECLARED_DEPENDENCIES + 1:
        return
    normalized_name = name.strip().lower().replace("_", "-")
    normalized_version = declared_version.strip()
    if not normalized_name or not normalized_version:
        return
    if not _DEPENDENCY_NAME_PATTERN.fullmatch(normalized_name):
        warnings.append(f"ignored invalid dependency name in {manifest_path}: {name!r}")
        return
    try:
        dependency = RepositoryDependency(
            ecosystem=ecosystem,
            name=normalized_name,
            declared_version=normalized_version,
            manifest_path=manifest_path,
            group=group,
        )
    except ValidationError:
        warnings.append(
            f"ignored dependency declaration outside profile limits in {manifest_path}: "
            f"{normalized_name!r}"
        )
        return
    dependencies.append(dependency)


def _record_resolution(
    resolutions: dict[tuple[DependencyEcosystem, str], set[tuple[str, str]]],
    *,
    ecosystem: DependencyEcosystem,
    name: str,
    version: str,
    path: str,
) -> None:
    normalized_name = name.strip().lower().replace("_", "-")
    normalized_version = version.strip()
    if (
        not normalized_name
        or not normalized_version
        or not _DEPENDENCY_NAME_PATTERN.fullmatch(normalized_name)
        or len(normalized_name) > 200
        or len(normalized_version) > 200
        or len(path) > 1000
    ):
        return
    resolutions.setdefault((ecosystem, normalized_name), set()).add((normalized_version, path))


def _apply_resolutions(
    dependencies: list[RepositoryDependency],
    resolutions: dict[tuple[DependencyEcosystem, str], set[tuple[str, str]]],
    warnings: list[str],
) -> list[RepositoryDependency]:
    resolved: list[RepositoryDependency] = []
    warned: set[tuple[DependencyEcosystem, str]] = set()
    for dependency in dependencies:
        candidates = resolutions.get((dependency.ecosystem, dependency.name), set())
        versions = {version for version, _ in candidates}
        if len(versions) == 1:
            version = next(iter(versions))
            resolution_path = sorted(path for _, path in candidates)[0]
            resolved.append(
                dependency.model_copy(
                    update={
                        "resolved_version": version,
                        "resolution_path": resolution_path,
                    }
                )
            )
            continue
        if len(versions) > 1 and (dependency.ecosystem, dependency.name) not in warned:
            warnings.append(
                f"multiple locked versions found for {dependency.ecosystem}:{dependency.name}"
            )
            warned.add((dependency.ecosystem, dependency.name))
        resolved.append(dependency)
    return resolved


def _is_python_marker(file_name: str) -> bool:
    return (
        file_name in {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"}
        or file_name.startswith("requirements-")
        and file_name.endswith(".txt")
    )


def _is_requirements_file(file_name: str) -> bool:
    return file_name == "requirements.txt" or (
        file_name.startswith("requirements-") and file_name.endswith(".txt")
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


def _inspect_uv_lock(
    path: Path,
    relative: str,
    resolutions: dict[tuple[DependencyEcosystem, str], set[tuple[str, str]]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        payload = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"invalid lockfile {relative}: {exc}")
        return
    packages = payload.get("package")
    if not isinstance(packages, list):
        return
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            _record_resolution(
                resolutions,
                ecosystem=DependencyEcosystem.PYTHON,
                name=name,
                version=version,
                path=relative,
            )


def _inspect_package_lock(
    path: Path,
    relative: str,
    resolutions: dict[tuple[DependencyEcosystem, str], set[tuple[str, str]]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"invalid lockfile {relative}: {exc}")
        return
    if not isinstance(payload, dict):
        warnings.append(f"invalid lockfile {relative}: expected an object")
        return

    packages = payload.get("packages")
    if isinstance(packages, dict):
        for package_path, package in packages.items():
            if not isinstance(package_path, str) or "node_modules/" not in package_path:
                continue
            if package_path.count("node_modules/") != 1:
                continue
            if not isinstance(package, dict):
                continue
            version = package.get("version")
            if not isinstance(version, str):
                continue
            name = package_path.rsplit("node_modules/", 1)[-1]
            _record_resolution(
                resolutions,
                ecosystem=DependencyEcosystem.NPM,
                name=name,
                version=version,
                path=relative,
            )
        return

    legacy_dependencies = payload.get("dependencies")
    if isinstance(legacy_dependencies, dict):
        for name, package in legacy_dependencies.items():
            if not isinstance(name, str) or not isinstance(package, dict):
                continue
            version = package.get("version")
            if isinstance(version, str):
                _record_resolution(
                    resolutions,
                    ecosystem=DependencyEcosystem.NPM,
                    name=name,
                    version=version,
                    path=relative,
                )


def _inspect_pnpm_lock(
    path: Path,
    relative: str,
    resolutions: dict[tuple[DependencyEcosystem, str], set[tuple[str, str]]],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        documents = list(yaml.safe_load_all(raw.decode("utf-8")))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        warnings.append(f"invalid lockfile {relative}: {exc}")
        return
    payload = next(
        (document for document in reversed(documents) if isinstance(document, dict)),
        None,
    )
    if payload is None:
        return
    importers = payload.get("importers")
    if not isinstance(importers, dict):
        return
    for importer in importers.values():
        if not isinstance(importer, dict):
            continue
        for group in ("dependencies", "devDependencies", "optionalDependencies"):
            table = importer.get(group)
            if not isinstance(table, dict):
                continue
            for name, entry in table.items():
                if not isinstance(name, str) or not isinstance(entry, dict):
                    continue
                version = entry.get("version")
                if not isinstance(version, str):
                    continue
                normalized_version = version.split("(", 1)[0]
                if normalized_version.startswith(("link:", "file:", "workspace:")):
                    continue
                _record_resolution(
                    resolutions,
                    ecosystem=DependencyEcosystem.NPM,
                    name=name,
                    version=normalized_version,
                    path=relative,
                )


def _inspect_pyproject(
    path: Path,
    relative: str,
    markers: set[str],
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    package_managers: set[RepositoryPackageManager],
    dependencies: list[RepositoryDependency],
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
    project = payload.get("project")
    if isinstance(project, dict):
        requires_python = project.get("requires-python")
        if isinstance(requires_python, str):
            _append_dependency(
                dependencies,
                ecosystem=DependencyEcosystem.PYTHON,
                name="python",
                declared_version=requires_python,
                manifest_path=relative,
                group="runtime",
                warnings=warnings,
            )
    dependency_names = _pyproject_dependencies(
        payload,
        relative,
        dependencies,
        warnings,
    )
    tool = payload.get("tool")
    tool_table = tool if isinstance(tool, dict) else {}
    poetry = tool_table.get("poetry")
    if isinstance(poetry, dict):
        package_managers.add(RepositoryPackageManager.POETRY)
        dependency_names.update(_poetry_dependencies(poetry, relative, dependencies, warnings))
    if "pytest" in dependency_names or "pytest" in tool_table:
        test_tools.add(RepositoryTestTool.PYTEST)


def _pyproject_dependencies(
    payload: dict[str, Any],
    relative: str,
    dependencies: list[RepositoryDependency],
    warnings: list[str],
) -> set[str]:
    names: set[str] = set()
    project = payload.get("project")
    if isinstance(project, dict):
        runtime_dependencies = project.get("dependencies")
        if isinstance(runtime_dependencies, list):
            _record_python_requirements(
                runtime_dependencies,
                "project.dependencies",
                relative,
                dependencies,
                names,
                warnings,
            )
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group_name, group in optional.items():
                if isinstance(group, list):
                    _record_python_requirements(
                        group,
                        f"project.optional-dependencies.{group_name}",
                        relative,
                        dependencies,
                        names,
                        warnings,
                    )
    dependency_groups = payload.get("dependency-groups")
    if isinstance(dependency_groups, dict):
        for group_name, group in dependency_groups.items():
            if isinstance(group, list):
                _record_python_requirements(
                    group,
                    f"dependency-groups.{group_name}",
                    relative,
                    dependencies,
                    names,
                    warnings,
                )
    return names


def _record_python_requirements(
    raw_requirements: list[Any],
    group: str,
    relative: str,
    dependencies: list[RepositoryDependency],
    names: set[str],
    warnings: list[str],
) -> None:
    for item in raw_requirements:
        if not isinstance(item, str):
            continue
        name = _requirement_name(item)
        if not name:
            continue
        names.add(name)
        declared = item.strip()[len(name) :].strip() or "*"
        _append_dependency(
            dependencies,
            ecosystem=DependencyEcosystem.PYTHON,
            name=name,
            declared_version=declared,
            manifest_path=relative,
            group=group,
            warnings=warnings,
        )


def _poetry_dependencies(
    poetry: dict[str, Any],
    relative: str,
    dependencies: list[RepositoryDependency],
    warnings: list[str],
) -> set[str]:
    names: set[str] = set()
    tables: list[tuple[str, Any]] = [
        ("tool.poetry.dependencies", poetry.get("dependencies")),
        ("tool.poetry.dev-dependencies", poetry.get("dev-dependencies")),
    ]
    groups = poetry.get("group")
    if isinstance(groups, dict):
        for group_name, group in groups.items():
            if isinstance(group, dict):
                tables.append(
                    (
                        f"tool.poetry.group.{group_name}.dependencies",
                        group.get("dependencies"),
                    )
                )
    for group, table in tables:
        if not isinstance(table, dict):
            continue
        for raw_name, declaration in table.items():
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip().lower().replace("_", "-")
            if isinstance(declaration, str):
                declared_version = declaration
            elif isinstance(declaration, dict) and isinstance(declaration.get("version"), str):
                declared_version = declaration["version"]
            else:
                continue
            names.add(name)
            _append_dependency(
                dependencies,
                ecosystem=DependencyEcosystem.PYTHON,
                name=name,
                declared_version=declared_version,
                manifest_path=relative,
                group=group,
                warnings=warnings,
            )
    return names


def _inspect_requirements_file(
    path: Path,
    relative: str,
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    dependencies: list[RepositoryDependency],
    warnings: list[str],
) -> None:
    raw = _read_manifest(path, relative, warnings)
    if raw is None:
        return
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        warnings.append(f"invalid requirements file {relative}: {exc}")
        return
    technologies.add(RepositoryTechnology.PYTHON)
    names: set[str] = set()
    for line in lines:
        requirement = line.split("#", 1)[0].strip()
        if not requirement or requirement.startswith(("-", ".")):
            continue
        name = _requirement_name(requirement)
        if not name:
            continue
        names.add(name)
        declared = requirement[len(name) :].strip() or "*"
        _append_dependency(
            dependencies,
            ecosystem=DependencyEcosystem.PYTHON,
            name=name,
            declared_version=declared,
            manifest_path=relative,
            group="requirements",
            warnings=warnings,
        )
    if "pytest" in names:
        test_tools.add(RepositoryTestTool.PYTEST)


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
    if parser.has_section("pytest") or parser.has_section("tool:pytest"):
        test_tools.add(RepositoryTestTool.PYTEST)


def _inspect_package_json(
    path: Path,
    relative: str,
    markers: set[str],
    technologies: set[RepositoryTechnology],
    test_tools: set[RepositoryTestTool],
    package_managers: set[RepositoryPackageManager],
    dependencies: list[RepositoryDependency],
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
    dependency_names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        table = payload.get(key)
        if isinstance(table, dict):
            for name, declared_version in table.items():
                normalized_name = str(name).lower()
                dependency_names.add(normalized_name)
                if isinstance(declared_version, str):
                    _append_dependency(
                        dependencies,
                        ecosystem=DependencyEcosystem.NPM,
                        name=normalized_name,
                        declared_version=declared_version,
                        manifest_path=relative,
                        group=key,
                        warnings=warnings,
                    )

    if "typescript" in dependency_names:
        technologies.add(RepositoryTechnology.TYPESCRIPT)
    if "react" in dependency_names or "react-dom" in dependency_names:
        technologies.add(RepositoryTechnology.REACT)
    if "vite" in dependency_names:
        technologies.add(RepositoryTechnology.VITE)
    if "vitest" in dependency_names:
        test_tools.add(RepositoryTestTool.VITEST)

    declared_manager = payload.get("packageManager")
    if isinstance(declared_manager, str):
        manager, separator, version = declared_manager.partition("@")
        manager = manager.lower()
        try:
            package_managers.add(RepositoryPackageManager(manager))
        except ValueError:
            pass
        else:
            if separator and version:
                _append_dependency(
                    dependencies,
                    ecosystem=DependencyEcosystem.NPM,
                    name=manager,
                    declared_version=version,
                    manifest_path=relative,
                    group="packageManager",
                    warnings=warnings,
                )
