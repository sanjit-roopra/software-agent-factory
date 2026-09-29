"""Shared atomic text write: temp file beside the target, then ``os.replace``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from software_agent_factory.atomic_write import write_text_atomic

DISK_FULL = "disk full"


def test_writes_the_content_and_creates_missing_parent_directories(tmp_path: Path) -> None:
    destination = tmp_path / "a" / "b" / "file.txt"

    write_text_atomic(destination, "café\n")

    assert destination.read_text(encoding="utf-8") == "café\n"


def test_replaces_an_existing_file_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    destination = tmp_path / "file.txt"
    destination.write_text("old", encoding="utf-8")

    write_text_atomic(destination, "new")

    assert destination.read_text(encoding="utf-8") == "new"
    assert [entry.name for entry in tmp_path.iterdir()] == ["file.txt"]


def test_an_explicit_mode_limits_the_file_permissions(tmp_path: Path) -> None:
    destination = tmp_path / "secret.json"

    write_text_atomic(destination, "{}", mode=0o600)

    assert destination.stat().st_mode & 0o077 == 0


def test_a_failed_publish_keeps_the_old_file_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "file.txt"
    destination.write_text("old", encoding="utf-8")

    def refuse(_source: object, _destination: object) -> None:
        raise OSError(DISK_FULL)

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match=DISK_FULL):
        write_text_atomic(destination, "new")
    monkeypatch.undo()

    assert destination.read_text(encoding="utf-8") == "old"
    assert [entry.name for entry in tmp_path.iterdir()] == ["file.txt"]


def test_a_failed_temporary_file_write_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_after_create(descriptor: int, *_args: object, **_options: object) -> None:
        os.close(descriptor)
        raise OSError(DISK_FULL)

    monkeypatch.setattr(os, "fdopen", fail_after_create)
    with pytest.raises(OSError, match=DISK_FULL):
        write_text_atomic(tmp_path / "file.txt", "content")
    monkeypatch.undo()

    assert list(tmp_path.iterdir()) == []
