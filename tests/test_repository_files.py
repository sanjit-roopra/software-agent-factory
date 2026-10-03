from __future__ import annotations

import json
from pathlib import Path

import pytest

from software_agent_factory.repository_files import (
    BLOCK_BEGIN,
    BLOCK_END,
    NO_CHECKS,
    RepositoryFile,
    RepositoryFileError,
    plan_repository_files,
    write_repository_files,
)
from software_agent_factory.repository_profile import profile_repository
from software_agent_factory.toolchain import inventory_toolchain


def _plan(root: Path) -> tuple[RepositoryFile, ...]:
    profile = profile_repository(root)
    return plan_repository_files(root, inventory_toolchain(root, profile), profile)


def _paths(files: tuple[RepositoryFile, ...]) -> list[str]:
    return [repository_file.path for repository_file in files]


def _by_path(files: tuple[RepositoryFile, ...], path: str) -> RepositoryFile:
    return next(repository_file for repository_file in files if repository_file.path == path)


@pytest.fixture
def python_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[dependency-groups]\ndev = ["ruff", "pytest"]\n[tool.ruff]\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return repo


def test_bare_python_repository_gets_every_file(python_repo: Path) -> None:
    files = _plan(python_repo)

    assert _paths(files) == [
        "AGENTS.md",
        "CLAUDE.md",
        ".agents/skills/pr-gate/SKILL.md",
        ".claude/skills/pr-gate",
        ".agents/skills/simplify/SKILL.md",
        ".claude/skills/simplify",
        ".agents/skills/polish/SKILL.md",
        ".claude/skills/polish",
        ".claude/agents/python-quality.md",
    ]
    assert _by_path(files, "CLAUDE.md").link_target == "AGENTS.md"
    assert _by_path(files, ".claude/skills/simplify").link_target == "../../.agents/skills/simplify"


def test_agents_block_names_the_repository_checks(python_repo: Path) -> None:
    agents = _by_path(_plan(python_repo), "AGENTS.md").content or ""

    assert agents.startswith("# Agent instructions\n\n<!-- factory:begin")
    assert "uv sync --locked\nuv run --no-sync ruff format --check .\n" in agents
    assert "uv run --no-sync pytest -q\n```" in agents
    assert "CI=true" not in agents
    assert "{{verify_commands}}" not in agents


def test_pr_gate_skill_names_the_same_checks(python_repo: Path) -> None:
    skill = _by_path(_plan(python_repo), ".agents/skills/pr-gate/SKILL.md").content or ""

    assert skill.startswith("---\nname: pr-gate\n")
    assert "uv run --no-sync ruff check --no-fix ." in skill


def test_repository_without_checks_says_so(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")

    agents = _by_path(_plan(tmp_path), "AGENTS.md").content or ""

    assert NO_CHECKS in agents
    assert ".claude/agents/python-quality.md" not in _paths(_plan(tmp_path))


def test_javascript_repository_gets_no_python_agent(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")

    assert ".claude/agents/python-quality.md" not in _paths(_plan(tmp_path))


def test_existing_files_are_never_replaced(python_repo: Path) -> None:
    (python_repo / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
    skill = python_repo / ".agents/skills/pr-gate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine\n", encoding="utf-8")
    (python_repo / ".claude/skills").mkdir(parents=True)
    (python_repo / ".claude/skills/simplify").write_text("mine\n", encoding="utf-8")

    paths = _paths(_plan(python_repo))

    assert "CLAUDE.md" not in paths
    assert ".agents/skills/pr-gate/SKILL.md" not in paths
    assert ".claude/skills/simplify" not in paths
    assert ".claude/skills/pr-gate" in paths


def test_existing_agents_file_gets_the_block_appended(python_repo: Path) -> None:
    (python_repo / "AGENTS.md").write_text("# House rules\n\nUse tabs.\n", encoding="utf-8")

    agents = _by_path(_plan(python_repo), "AGENTS.md").content or ""

    assert agents.startswith("# House rules\n\nUse tabs.\n\n<!-- factory:begin")
    assert agents.endswith(f"{BLOCK_END}\n")


def test_only_the_block_inside_the_markers_is_replaced(python_repo: Path) -> None:
    (python_repo / "AGENTS.md").write_text(
        f"# Rules\n\n{BLOCK_BEGIN} old -->\nstale\n{BLOCK_END}\n\nKeep this.\n", encoding="utf-8"
    )

    agents = _by_path(_plan(python_repo), "AGENTS.md").content or ""

    assert agents.startswith("# Rules\n\n<!-- factory:begin (managed")
    assert "stale" not in agents
    assert agents.endswith(f"{BLOCK_END}\n\nKeep this.\n")


def test_a_current_block_is_left_alone(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo))

    assert _plan(python_repo) == ()


def test_a_symlinked_agents_file_is_left_alone(python_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-agents.md"
    outside.write_text("elsewhere\n", encoding="utf-8")
    (python_repo / "AGENTS.md").symlink_to(outside)

    assert "AGENTS.md" not in _paths(_plan(python_repo))


def test_writing_creates_files_and_relative_links(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo))

    assert (python_repo / "CLAUDE.md").read_text() == (python_repo / "AGENTS.md").read_text()
    assert (
        (python_repo / ".claude/skills/polish/SKILL.md")
        .read_text()
        .startswith("---\nname: polish\n")
    )
    assert (python_repo / ".claude/agents/python-quality.md").read_text().count("MIT") == 1


def test_writing_refuses_a_symlinked_parent_directory(python_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-claude"
    outside.mkdir()
    (python_repo / ".claude").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepositoryFileError, match="symbolic link"):
        write_repository_files(python_repo, _plan(python_repo))

    assert list(outside.iterdir()) == []


def test_a_file_has_content_or_a_link_target() -> None:
    with pytest.raises(ValueError, match="either content or a link target"):
        RepositoryFile("x")
