"""Version and build metadata helpers for installed and frozen builds.

``PLAN.md`` Phase 15.1/15.2: one authoritative version resolution shared by
every entry point -- the installed ``factory`` console script, ``python -m
software_agent_factory`` and the Typer application's own ``--version``
option -- so all three always print the same line
(:func:`format_version_line`), whether the process is a source checkout, an
installed wheel or a frozen PyInstaller bundle.
"""

from __future__ import annotations

import json
import platform
import sys
import tomllib
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any

PROJECT_NAME = "software-agent-factory"
BUILD_INFO_FILENAME = "build-info.json"


@lru_cache(maxsize=1)
def get_build_info() -> dict[str, Any]:
    """Return bundled build metadata when available."""
    for candidate in _build_info_candidates():
        if not candidate.is_file():
            continue
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Build info must be a JSON object: {candidate}")
        return payload
    return {}


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the single authoritative package version."""
    build_info = get_build_info()
    bundled_version = build_info.get("version")
    if isinstance(bundled_version, str) and bundled_version:
        return bundled_version

    try:
        return distribution_version(PROJECT_NAME)
    except PackageNotFoundError:
        return _read_pyproject_version()


@lru_cache(maxsize=1)
def get_version_source() -> str:
    """Describe where :func:`get_version` resolved the version from."""
    build_info = get_build_info()
    bundled_version = build_info.get("version")
    if isinstance(bundled_version, str) and bundled_version:
        return "build-info"

    try:
        distribution_version(PROJECT_NAME)
    except PackageNotFoundError:
        return "pyproject"
    return "installed-metadata"


@lru_cache(maxsize=1)
def get_runtime_details() -> dict[str, str]:
    """Expose lightweight runtime details for diagnostics and build scripts."""
    return {
        "version": get_version(),
        "version_source": get_version_source(),
        "python_version": platform.python_version(),
        "frozen": "true" if getattr(sys, "frozen", False) else "false",
    }


def get_program_name() -> str:
    """The program name printed by ``--version``.

    A frozen bundle is invoked through its own executable (whose name the
    operator chose when extracting the archive), so that name is used
    verbatim; everything else is invoked as the ``factory`` console script
    or ``python -m software_agent_factory`` and reports ``factory``.
    Deliberately not cached: ``sys.frozen``/``sys.executable`` are patched by
    tests to exercise the frozen path.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return "factory"


def format_version_line() -> str:
    """The single ``--version`` line every entry point prints."""
    return f"{get_program_name()} {get_version()}"


@lru_cache(maxsize=1)
def _read_pyproject_version() -> str:
    for candidate in _pyproject_candidates():
        if not candidate.is_file():
            continue
        payload = tomllib.loads(candidate.read_text(encoding="utf-8"))
        project = payload.get("project")
        if not isinstance(project, dict):
            continue
        version = project.get("version")
        if isinstance(version, str) and version:
            return version
    raise RuntimeError(
        "Unable to determine project version from installed metadata or pyproject.toml"
    )


def _build_info_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    package_dir = Path(__file__).resolve().parent

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            bundle_root = Path(str(meipass))
            candidates.extend(
                (
                    bundle_root / "software_agent_factory" / BUILD_INFO_FILENAME,
                    bundle_root / BUILD_INFO_FILENAME,
                )
            )
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            (
                executable_dir / "software_agent_factory" / BUILD_INFO_FILENAME,
                executable_dir / BUILD_INFO_FILENAME,
            )
        )

    candidates.extend(
        (
            package_dir / BUILD_INFO_FILENAME,
            package_dir.parent / BUILD_INFO_FILENAME,
        )
    )
    return tuple(dict.fromkeys(candidates))


def _pyproject_candidates() -> tuple[Path, ...]:
    package_dir = Path(__file__).resolve().parent
    return tuple(parent / "pyproject.toml" for parent in package_dir.parents)
