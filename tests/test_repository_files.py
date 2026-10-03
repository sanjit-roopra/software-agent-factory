from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from software_agent_factory.repository_files import (
    BLOCK_BEGIN,
    BLOCK_END,
    MAX_AGENTS_FILE_BYTES,
    NO_CHECKS,
    UNCLEAR_BLOCK_NOTE,
    UNREADABLE_AGENTS_NOTE,
    RepositoryFile,
    RepositoryFileError,
    RepositoryFilesPlan,
    plan_repository_files,
    write_repository_files,
)
from software_agent_factory.repository_profile import profile_repository
from software_agent_factory.toolchain import inventory_toolchain
from software_agent_factory.toolchain_commands import CHECK_ENVIRONMENT

_ALL_PYTHON_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".agents/skills/pr-gate/SKILL.md",
    ".claude/skills/pr-gate",
    ".agents/skills/simplify/SKILL.md",
    ".claude/skills/simplify",
    ".agents/skills/polish/SKILL.md",
    ".claude/skills/polish",
    ".claude/agents/python-quality.md",
)


def _plan(root: Path) -> RepositoryFilesPlan:
    profile = profile_repository(root)
    return plan_repository_files(root, inventory_toolchain(root, profile), profile)


def _file(plan: RepositoryFilesPlan, path: str) -> RepositoryFile:
    return next(repository_file for repository_file in plan.files if repository_file.path == path)


def _agents(root: Path) -> str:
    return _file(_plan(root), "AGENTS.md").content or ""


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
    plan = _plan(python_repo)

    assert plan.paths == _ALL_PYTHON_FILES
    assert plan.notes == ()
    assert _file(plan, "CLAUDE.md").link_target == "AGENTS.md"
    assert _file(plan, ".claude/skills/simplify").link_target == "../../.agents/skills/simplify"


def test_agents_block_names_the_install_command_and_checks(python_repo: Path) -> None:
    agents = _agents(python_repo)

    assert agents.startswith("# Agent instructions\n\n<!-- factory:begin")
    assert "uv sync --locked\nuv run --no-sync ruff format --check .\n" in agents
    assert "uv run --no-sync pytest -q\n```" in agents
    assert CHECK_ENVIRONMENT not in agents
    assert "{{verify_commands}}" not in agents


def test_pr_gate_skill_names_the_same_checks(python_repo: Path) -> None:
    skill = _file(_plan(python_repo), ".agents/skills/pr-gate/SKILL.md").content or ""

    assert skill.startswith("---\nname: pr-gate\n")
    assert "uv run --no-sync ruff check --no-fix ." in skill


def test_repository_without_checks_says_so(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")

    assert NO_CHECKS in _agents(tmp_path)


def test_javascript_repository_gets_no_python_agent(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")

    assert ".claude/agents/python-quality.md" not in _plan(tmp_path).paths


def test_existing_files_and_dangling_links_are_never_replaced(python_repo: Path) -> None:
    (python_repo / "CLAUDE.md").symlink_to("missing-target.md")
    skill = python_repo / ".agents/skills/pr-gate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine\n", encoding="utf-8")
    (python_repo / ".claude/skills").mkdir(parents=True)
    (python_repo / ".claude/skills/simplify").symlink_to("nowhere")
    (python_repo / ".claude/agents").mkdir(parents=True)
    (python_repo / ".claude/agents/python-quality.md").write_text("mine\n", encoding="utf-8")

    paths = _plan(python_repo).paths

    assert paths == (
        "AGENTS.md",
        ".claude/skills/pr-gate",
        ".agents/skills/simplify/SKILL.md",
        ".agents/skills/polish/SKILL.md",
        ".claude/skills/polish",
    )


@pytest.mark.parametrize(
    ("existing", "expected_prefix"),
    [
        ("# Rules", "# Rules\n\n<!-- factory:begin"),
        ("# Rules\n", "# Rules\n\n<!-- factory:begin"),
        ("# Rules\n\n", "# Rules\n\n<!-- factory:begin"),
    ],
)
def test_existing_agents_file_gets_the_block_after_one_blank_line(
    python_repo: Path, existing: str, expected_prefix: str
) -> None:
    (python_repo / "AGENTS.md").write_text(existing, encoding="utf-8")

    agents = _file(_plan(python_repo), "AGENTS.md")

    assert agents.replaces is True
    assert (agents.content or "").startswith(expected_prefix)
    assert (agents.content or "").endswith(f"{BLOCK_END}\n")


def test_only_the_block_inside_the_markers_is_replaced(python_repo: Path) -> None:
    (python_repo / "AGENTS.md").write_text(
        f"# Rules\n\n{BLOCK_BEGIN} old -->\nstale\n{BLOCK_END}\n\nKeep this.\n", encoding="utf-8"
    )

    agents = _agents(python_repo)

    assert agents.startswith("# Rules\n\n<!-- factory:begin (managed")
    assert "stale" not in agents
    assert agents.endswith(f"{BLOCK_END}\n\nKeep this.\n")


def test_crlf_agents_file_keeps_its_line_endings(python_repo: Path) -> None:
    (python_repo / "AGENTS.md").write_bytes(b"# Rules\r\n")

    agents = _agents(python_repo)

    assert agents.startswith("# Rules\r\n\r\n<!-- factory:begin")
    assert "\n" not in agents.replace("\r\n", "")


def test_crlf_agents_file_with_a_current_block_is_left_alone(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo).files)
    text = (python_repo / "AGENTS.md").read_text(encoding="utf-8")
    (python_repo / "AGENTS.md").write_bytes(text.replace("\n", "\r\n").encode("utf-8"))

    assert "AGENTS.md" not in _plan(python_repo).paths


@pytest.mark.parametrize(
    "existing",
    [
        f"{BLOCK_BEGIN} -->\nno end marker\n",
        f"{BLOCK_END}\n{BLOCK_BEGIN} -->\n",
        f"{BLOCK_BEGIN} -->\na\n{BLOCK_END}\n{BLOCK_BEGIN} -->\nb\n{BLOCK_END}\n",
    ],
)
def test_unclear_markers_leave_agents_file_alone_with_a_note(
    python_repo: Path, existing: str
) -> None:
    (python_repo / "AGENTS.md").write_text(existing, encoding="utf-8")

    plan = _plan(python_repo)

    assert "AGENTS.md" not in plan.paths
    assert plan.notes == (UNCLEAR_BLOCK_NOTE,)


@pytest.mark.parametrize("kind", ["symlink", "oversized", "not-utf8", "directory"])
def test_unreadable_agents_file_is_left_alone_with_a_note(
    python_repo: Path, tmp_path: Path, kind: str
) -> None:
    agents = python_repo / "AGENTS.md"
    if kind == "symlink":
        outside = tmp_path / "outside-agents.md"
        outside.write_text("elsewhere\n", encoding="utf-8")
        agents.symlink_to(outside)
    elif kind == "oversized":
        agents.write_text("#" * (MAX_AGENTS_FILE_BYTES + 1), encoding="utf-8")
    elif kind == "not-utf8":
        agents.write_bytes(b"\xff\xfe")
    else:
        agents.mkdir()

    plan = _plan(python_repo)

    assert "AGENTS.md" not in plan.paths
    assert plan.notes == (UNREADABLE_AGENTS_NOTE,)


def test_ignored_paths_are_skipped_with_a_note(python_repo: Path) -> None:
    subprocess.run(["git", "-C", str(python_repo), "init", "-q"], check=True)
    (python_repo / ".gitignore").write_text("CLAUDE.md\n.claude/\n", encoding="utf-8")

    plan = _plan(python_repo)

    assert plan.paths == (
        "AGENTS.md",
        ".agents/skills/pr-gate/SKILL.md",
        ".agents/skills/simplify/SKILL.md",
        ".agents/skills/polish/SKILL.md",
    )
    assert plan.notes == (
        ".claude/agents/python-quality.md skipped: the repository ignores it",
        ".claude/skills/polish skipped: the repository ignores it",
        ".claude/skills/pr-gate skipped: the repository ignores it",
        ".claude/skills/simplify skipped: the repository ignores it",
        "CLAUDE.md skipped: the repository ignores it",
    )


def test_a_current_block_is_left_alone(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo).files)

    assert _plan(python_repo) == RepositoryFilesPlan(files=())


def test_writing_creates_relative_links(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo).files)

    assert os.readlink(python_repo / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(python_repo / ".claude/skills/polish") == "../../.agents/skills/polish"
    assert (
        (python_repo / ".claude/skills/polish/SKILL.md")
        .read_text()
        .startswith("---\nname: polish\n")
    )


def test_python_agent_carries_its_attribution(python_repo: Path) -> None:
    write_repository_files(python_repo, _plan(python_repo).files)

    agent = (python_repo / ".claude/agents/python-quality.md").read_text(encoding="utf-8")

    assert "Adapted from the dev-team plugin" in agent


def test_writing_refuses_a_symlinked_parent_directory(python_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-claude"
    outside.mkdir()
    (python_repo / ".claude").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RepositoryFileError, match="cannot write"):
        write_repository_files(python_repo, _plan(python_repo).files)

    assert list(outside.iterdir()) == []


def test_writing_never_replaces_a_file_that_appeared_after_planning(python_repo: Path) -> None:
    plan = _plan(python_repo)
    skill = python_repo / ".agents/skills/pr-gate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("mine\n", encoding="utf-8")

    with pytest.raises(RepositoryFileError, match="cannot write .agents/skills/pr-gate/SKILL.md"):
        write_repository_files(python_repo, plan.files)

    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "mine\n"


def test_writing_never_follows_a_link_that_appeared_after_planning(
    python_repo: Path, tmp_path: Path
) -> None:
    plan = _plan(python_repo)
    victim = tmp_path / "victim.md"
    victim.write_text("keep\n", encoding="utf-8")
    (python_repo / "AGENTS.md").symlink_to(victim)

    with pytest.raises(RepositoryFileError, match="cannot write AGENTS.md"):
        write_repository_files(python_repo, plan.files)

    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_writing_refuses_a_parent_that_is_a_file(python_repo: Path) -> None:
    (python_repo / ".agents").write_text("not a directory\n", encoding="utf-8")
    plan = RepositoryFilesPlan(files=(RepositoryFile(".agents/skills/x/SKILL.md", content="x\n"),))

    with pytest.raises(RepositoryFileError, match="cannot write"):
        write_repository_files(python_repo, plan.files)


def test_a_file_has_content_or_a_link_target() -> None:
    with pytest.raises(ValueError, match="either content or a link target"):
        RepositoryFile("x")
