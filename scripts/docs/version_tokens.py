"""Render documentation version tokens from authoritative package metadata."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

VERSION_TOKEN = "{{ factory_version }}"
RELEASE_TAG_TOKEN = "{{ factory_release_tag }}"


@lru_cache(maxsize=1)
def _project_version(config_file_path: str) -> str:
    repository_root = Path(config_file_path).resolve().parent
    payload = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml must define a [project] table")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml must define a non-empty project.version")
    return version


def on_page_markdown(
    markdown: str,
    page: Any,
    config: Any,
    files: Any,
) -> str:
    """Replace release tokens before MkDocs renders each page."""
    del page, files
    version = _project_version(str(config.config_file_path))
    return markdown.replace(VERSION_TOKEN, version).replace(
        RELEASE_TAG_TOKEN,
        f"v{version}",
    )
