"""Checks for public project metadata and documentation."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _load_docs_link_checker() -> ModuleType:
    path = ROOT / "scripts" / "docs" / "check_rendered_links.py"
    spec = importlib.util.spec_from_file_location("check_rendered_links", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_project_files_exist() -> None:
    required = [
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "GOVERNANCE.md",
        "LICENSE",
        "MAINTAINERS.md",
        "SECURITY.md",
        "SUPPORT.md",
        ".github/CODEOWNERS",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
        ".github/release.yml",
        "mkdocs.yml",
        "docs/llms.txt",
    ]

    missing = [path for path in required if not (ROOT / path).is_file()]

    assert missing == []


def test_package_metadata_links_to_public_project_resources() -> None:
    project = _load_pyproject()["project"]

    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert project["requires-python"] == ">=3.13"
    assert project["maintainers"] == [{"name": "Sanjit Roopra"}]
    assert set(project["urls"]) == {
        "Documentation",
        "Repository",
        "Issues",
        "Changelog",
        "Security",
        "Releases",
    }
    assert all("<owner>" not in url for url in project["urls"].values())


def test_documentation_configuration_uses_material_and_strict_validation() -> None:
    config = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert config["site_url"] == "https://sanjit-roopra.github.io/software-agent-factory/"
    assert config["repo_url"] == "https://github.com/sanjit-roopra/software-agent-factory"
    assert config["strict"] == "true"
    assert config["theme"]["name"] == "material"
    assert config["theme"]["font"] == "false"
    assert config["plugins"] == ["search"]


def test_documentation_dependencies_are_isolated() -> None:
    groups = _load_pyproject()["dependency-groups"]

    assert groups["docs"] == ["mkdocs-material>=9.7,<10.0"]
    assert {"include-group": "docs"} in groups["dev"]


def test_readme_has_no_placeholder_repository_urls() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "https://github.com/sanjit-roopra/software-agent-factory" in readme
    assert "https://sanjit-roopra.github.io/software-agent-factory/" in readme
    assert "github.com/<owner>" not in readme


def test_security_policy_links_to_the_published_safety_page() -> None:
    policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "https://sanjit-roopra.github.io/software-agent-factory/reference/safety/" in policy
    assert "/concepts/safety/" not in policy


def test_rendered_link_checker_finds_visible_markdown_links(tmp_path: Path) -> None:
    module = _load_docs_link_checker()
    page = tmp_path / "get-started" / "index.html"
    page.parent.mkdir()
    page.write_text(
        """
        <main><p>[Install](install.md)</p></main>
        <pre><code>[Example](example.md)</code></pre>
        """,
        encoding="utf-8",
    )

    assert module.find_literal_markdown_links(tmp_path) == [
        "get-started/index.html: [Install](install.md)"
    ]
