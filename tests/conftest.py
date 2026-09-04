"""Shared pytest fixtures.

Git-related fixtures create throwaway repositories under ``tmp_path`` with
local (not global) identity configuration, ``commit.gpgsign`` disabled, and
global/system Git config suppressed, so tests never depend on the developer
machine's Git configuration, commit signing setup, or hooks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from factory_testing import git


@pytest.fixture
def factory_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # No controller-side GitHub credentials may leak in from the developer's
    # shell: tests must never be able to reach a real GitHub account.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def factory_source_repo(tmp_path: Path, factory_git_env: None) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "factory-test@example.invalid")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def factory_data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory
