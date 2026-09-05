"""Tests for version resolution across source, installed, and frozen execution."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from software_agent_factory import version
from software_agent_factory.__main__ import main


def _clear_version_caches() -> None:
    version.get_build_info.cache_clear()
    version.get_version.cache_clear()
    version.get_version_source.cache_clear()
    version.get_runtime_details.cache_clear()
    version._read_pyproject_version.cache_clear()


def test_get_version_falls_back_to_pyproject_when_metadata_is_missing(monkeypatch) -> None:
    _clear_version_caches()

    def missing_distribution(_name: str) -> str:
        raise version.PackageNotFoundError

    monkeypatch.setattr(version, "distribution_version", missing_distribution)

    assert version.get_version() == "0.2.0"
    assert version.get_version_source() == "pyproject"


def test_get_version_prefers_bundled_build_info_for_frozen_build(
    tmp_path: Path, monkeypatch
) -> None:
    build_info = tmp_path / "software_agent_factory" / "build-info.json"
    build_info.parent.mkdir(parents=True)
    build_info.write_text(json.dumps({"version": "9.9.9", "tag": "v9.9.9"}), encoding="utf-8")

    _clear_version_caches()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "factory"), raising=False)
    monkeypatch.setattr(version, "distribution_version", lambda _name: "0.1.0")

    assert version.get_build_info()["tag"] == "v9.9.9"
    assert version.get_version() == "9.9.9"
    assert version.get_version_source() == "build-info"


def test_main_prints_version_without_loading_the_cli(monkeypatch) -> None:
    _clear_version_caches()
    monkeypatch.setattr("software_agent_factory.__main__.get_version", lambda: "3.2.1")
    captured = io.StringIO()

    with redirect_stdout(captured):
        exit_code = main(["--version"])

    assert exit_code == 0
    assert captured.getvalue().strip() == "factory 3.2.1"
