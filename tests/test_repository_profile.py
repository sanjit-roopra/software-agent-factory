from __future__ import annotations

import json
from pathlib import Path

from software_agent_factory.models import (
    AgentRole,
    RepositoryPackageManager,
    RepositoryTechnology,
    RepositoryTestTool,
    SkillId,
)
from software_agent_factory.repository_profile import profile_repository, skills_for_role


def _skill_ids(root: Path) -> list[SkillId]:
    return [skill.id for skill in profile_repository(root).selected_skills]


def test_unknown_repository_selects_only_universal_skills(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# example\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == ()
    assert profile.test_tools == ()
    assert _skill_ids(tmp_path) == [SkillId.PLAN_QUALITY, SkillId.SIMPLIFICATION]
    assert [skill.id for skill in skills_for_role(profile, AgentRole.TESTER)] == []


def test_python_pytest_repository_selects_python_and_testing_skills(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
dependencies = []

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.12"]

[tool.pytest.ini_options]
addopts = "-q"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == (RepositoryTechnology.PYTHON,)
    assert profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert profile.package_managers == (RepositoryPackageManager.UV,)
    assert [skill.id for skill in profile.selected_skills] == [
        SkillId.PLAN_QUALITY,
        SkillId.SIMPLIFICATION,
        SkillId.PYTHON_QUALITY,
        SkillId.TESTING_QUALITY,
    ]
    assert "pyproject.toml" in profile.markers
    assert profile.warnings == ()


def test_nested_react_vite_vitest_repository_selects_frontend_skills(
    tmp_path: Path,
) -> None:
    app = tmp_path / "apps" / "web"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.0.0",
                "dependencies": {"react": "19.0.0"},
                "devDependencies": {
                    "typescript": "5.9.0",
                    "vite": "7.0.0",
                    "vitest": "3.0.0",
                },
            }
        ),
        encoding="utf-8",
    )
    (app / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == (
        RepositoryTechnology.JAVASCRIPT,
        RepositoryTechnology.REACT,
        RepositoryTechnology.TYPESCRIPT,
        RepositoryTechnology.VITE,
    )
    assert profile.test_tools == (RepositoryTestTool.VITEST,)
    assert profile.package_managers == (RepositoryPackageManager.PNPM,)
    assert [skill.id for skill in profile.selected_skills] == [
        SkillId.PLAN_QUALITY,
        SkillId.SIMPLIFICATION,
        SkillId.VITE_QUALITY,
        SkillId.REACT_QUALITY,
        SkillId.REACT_REACTIVITY,
        SkillId.REACT_TESTING,
        SkillId.TESTING_QUALITY,
    ]
    assert all(
        "apps/web/package.json" in skill.evidence
        for skill in profile.selected_skills
        if skill.id
        in {
            SkillId.VITE_QUALITY,
            SkillId.REACT_QUALITY,
            SkillId.REACT_REACTIVITY,
            SkillId.REACT_TESTING,
            SkillId.TESTING_QUALITY,
        }
    )


def test_invalid_manifest_is_reported_without_guessing_frameworks(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not-json", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert RepositoryTechnology.REACT not in profile.technologies
    assert RepositoryTechnology.VITE not in profile.technologies
    assert any("invalid manifest package.json" in warning for warning in profile.warnings)


def test_ignored_directories_and_symlinks_do_not_contribute_evidence(
    tmp_path: Path,
) -> None:
    ignored = tmp_path / "node_modules" / "react"
    ignored.mkdir(parents=True)
    (ignored / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}}),
        encoding="utf-8",
    )
    target = tmp_path / "external-package.json"
    target.write_text(json.dumps({"dependencies": {"vite": "7.0.0"}}), encoding="utf-8")
    (tmp_path / "package.json").symlink_to(target)

    profile = profile_repository(tmp_path)

    assert profile.technologies == ()
    assert profile.markers == ()


def test_skill_evidence_is_bounded_for_large_source_trees(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    for index in range(20):
        (source / f"module_{index:02}.py").write_text("value = 1\n", encoding="utf-8")

    profile = profile_repository(tmp_path)
    python_skill = next(
        skill for skill in profile.selected_skills if skill.id is SkillId.PYTHON_QUALITY
    )

    assert python_skill.evidence == ("src/module_00.py",)


def test_generic_test_structure_selects_testing_guidance(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "service_spec.rb").write_text("describe 'service' do\nend\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert SkillId.TESTING_QUALITY in [skill.id for skill in profile.selected_skills]
