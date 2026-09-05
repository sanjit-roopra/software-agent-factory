from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from software_agent_factory import repository_profile as repository_profile_module
from software_agent_factory.models import (
    DependencyEcosystem,
    RepositoryPackageManager,
    RepositoryProfile,
    RepositoryTechnology,
    RepositoryTestTool,
)
from software_agent_factory.repository_profile import (
    generic_repository_profile,
    profile_repository,
)


def _repository(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _warning_matching(profile: RepositoryProfile, fragment: str) -> str:
    matches = [warning for warning in profile.warnings if fragment in warning]
    assert matches, f"expected a warning containing {fragment!r}, got {profile.warnings}"
    return matches[0]


def test_unknown_repository_has_a_stable_empty_profile(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# example\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == ()
    assert profile.test_tools == ()
    assert profile.dependencies == ()
    assert profile.version_files == ()
    assert len(profile.manifest_fingerprint) == 64


def test_python_profile_retains_declared_versions_and_groups(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
requires-python = ">=3.13"
dependencies = ["pydantic>=2.9,<3"]

[dependency-groups]
dev = ["pytest>=9", "ruff>=0.12"]

[tool.pytest.ini_options]
addopts = "-q"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pytest"\nversion = "9.1.1"\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == (RepositoryTechnology.PYTHON,)
    assert profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert profile.package_managers == (RepositoryPackageManager.UV,)
    assert set(profile.version_files) == {"pyproject.toml", "uv.lock"}
    assert {
        (item.ecosystem, item.name, item.declared_version, item.group)
        for item in profile.dependencies
    } == {
        (DependencyEcosystem.PYTHON, "python", ">=3.13", "runtime"),
        (
            DependencyEcosystem.PYTHON,
            "pydantic",
            ">=2.9,<3",
            "project.dependencies",
        ),
        (DependencyEcosystem.PYTHON, "pytest", ">=9", "dependency-groups.dev"),
        (DependencyEcosystem.PYTHON, "ruff", ">=0.12", "dependency-groups.dev"),
    }
    assert profile.warnings == ()
    pytest_dependency = next(item for item in profile.dependencies if item.name == "pytest")
    assert pytest_dependency.resolved_version == "9.1.1"
    assert pytest_dependency.resolution_path == "uv.lock"


def test_nested_react_vite_profile_retains_declared_versions(
    tmp_path: Path,
) -> None:
    app = tmp_path / "apps" / "web"
    app.mkdir(parents=True)
    (app / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.0.0",
                "dependencies": {"react": "^19.1.0", "react-dom": "^19.1.0"},
                "devDependencies": {
                    "typescript": "5.9.0",
                    "vite": "7.0.0",
                    "vitest": "3.0.0",
                },
            }
        ),
        encoding="utf-8",
    )
    (app / "pnpm-lock.yaml").write_text(
        """
lockfileVersion: '9.0'
importers:
  .:
    dependencies:
      react:
        specifier: ^19.1.0
        version: 19.1.1
      react-dom:
        specifier: ^19.1.0
        version: 19.1.1(react@19.1.1)
    devDependencies:
      vite:
        specifier: 7.0.0
        version: 7.0.0
""".lstrip(),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.technologies == (
        RepositoryTechnology.JAVASCRIPT,
        RepositoryTechnology.REACT,
        RepositoryTechnology.TYPESCRIPT,
        RepositoryTechnology.VITE,
    )
    assert profile.test_tools == (RepositoryTestTool.VITEST,)
    assert profile.package_managers == (RepositoryPackageManager.PNPM,)
    assert set(profile.version_files) == {
        "apps/web/package.json",
        "apps/web/pnpm-lock.yaml",
    }
    versions = {item.name: item.declared_version for item in profile.dependencies}
    assert versions == {
        "react": "^19.1.0",
        "react-dom": "^19.1.0",
        "pnpm": "10.0.0",
        "typescript": "5.9.0",
        "vite": "7.0.0",
        "vitest": "3.0.0",
    }
    resolved = {item.name: item.resolved_version for item in profile.dependencies}
    assert resolved["react"] == "19.1.1"
    assert resolved["react-dom"] == "19.1.1"
    assert resolved["vite"] == "7.0.0"


def test_package_lock_supplies_exact_npm_version(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19.0.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"react": "^19.0.0"}},
                    "node_modules/react": {"version": "19.1.1"},
                },
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    react = next(item for item in profile.dependencies if item.name == "react")
    assert react.declared_version == "^19.0.0"
    assert react.resolved_version == "19.1.1"
    assert react.resolution_path == "package-lock.json"


def test_package_lock_prefers_top_level_version_over_nested_copy(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19.0.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/react": {"version": "19.1.1"},
                    "node_modules/legacy/node_modules/react": {"version": "18.3.1"},
                },
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    react = next(item for item in profile.dependencies if item.name == "react")
    assert react.resolved_version == "19.1.1"
    assert not any("multiple locked versions" in warning for warning in profile.warnings)


def test_requirements_file_retains_dependency_versions(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "pytest==8.4.2\nrequests>=2.32\n-r requirements-dev.txt\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert profile.package_managers == (RepositoryPackageManager.PIP,)
    assert {(item.name, item.declared_version, item.group) for item in profile.dependencies} == {
        ("pytest", "==8.4.2", "requirements"),
        ("requests", ">=2.32", "requirements"),
    }


def test_poetry_tables_retain_dependency_versions(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry.dependencies]
python = "^3.13"
pydantic = "^2.11"

[tool.poetry.group.test.dependencies]
pytest = { version = "^8.4", extras = ["testing"] }
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert profile.package_managers == (RepositoryPackageManager.POETRY,)
    assert {(item.name, item.declared_version, item.group) for item in profile.dependencies} == {
        ("python", "^3.13", "tool.poetry.dependencies"),
        ("pydantic", "^2.11", "tool.poetry.dependencies"),
        ("pytest", "^8.4", "tool.poetry.group.test.dependencies"),
    }


def test_oversized_dependency_declaration_is_skipped_with_warning(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "x" * 501}}),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert any("outside profile limits" in warning for warning in profile.warnings)


def test_manifest_formatting_changes_only_the_manifest_fingerprint(tmp_path: Path) -> None:
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}}),
        encoding="utf-8",
    )
    before = profile_repository(tmp_path)

    package_json.write_text(
        '{\n  "dependencies": {\n    "react": "19.0.0"\n  }\n}\n',
        encoding="utf-8",
    )
    after = profile_repository(tmp_path)

    assert before.manifest_fingerprint != after.manifest_fingerprint
    assert before.dependency_fingerprint == after.dependency_fingerprint


def test_dependency_version_change_changes_dependency_fingerprint(tmp_path: Path) -> None:
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}}),
        encoding="utf-8",
    )
    before = profile_repository(tmp_path)

    package_json.write_text(
        json.dumps({"dependencies": {"react": "19.1.0"}}),
        encoding="utf-8",
    )
    after = profile_repository(tmp_path)

    assert before.manifest_fingerprint != after.manifest_fingerprint
    assert before.dependency_fingerprint != after.dependency_fingerprint
    assert before.dependencies[0].declared_version == "19.0.0"
    assert after.dependencies[0].declared_version == "19.1.0"


def test_invalid_manifest_is_reported_without_guessing_frameworks(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not-json", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert RepositoryTechnology.REACT not in profile.technologies
    assert RepositoryTechnology.VITE not in profile.technologies
    assert any("invalid manifest package.json" in warning for warning in profile.warnings)
    assert profile.version_files == ("package.json",)


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
    assert profile.version_files == ()


def test_generic_test_structure_is_recorded_as_repository_evidence(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "service_spec.rb").write_text("describe 'service' do\nend\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.markers == ("tests/service_spec.rb",)


def test_profiling_a_non_directory_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text("[project]\nname = 'example'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="repository root is not a directory"):
        profile_repository(manifest)


def test_generic_profile_records_only_the_supplied_warning() -> None:
    degraded = generic_repository_profile(warning="detection failed")
    empty = generic_repository_profile()

    assert degraded.warnings == ("detection failed",)
    assert empty.warnings == ()
    assert degraded.manifest_fingerprint == empty.manifest_fingerprint
    assert degraded.dependency_fingerprint == empty.dependency_fingerprint
    assert degraded.dependencies == ()


def test_scan_limit_stops_the_walk_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_SCANNED_FILES", 1)
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19.0.0"}}),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert _warning_matching(profile, "scan limit reached after 1 files")
    assert profile.dependencies == ()


def test_path_limits_truncate_markers_and_version_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_PROFILE_PATHS", 1)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"name": "example"}), encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert len(profile.markers) == 1
    assert len(profile.version_files) == 1
    assert _warning_matching(profile, "repository markers limited to 1 entries")
    assert _warning_matching(profile, "version file paths limited to 1 entries")


def test_dependency_limit_truncates_declarations_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_DECLARED_DEPENDENCIES", 2)
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "alpha": "1.0.0",
                    "beta": "1.0.0",
                    "gamma": "1.0.0",
                    "delta": "1.0.0",
                    "epsilon": "1.0.0",
                }
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert len(profile.dependencies) == 2
    assert _warning_matching(profile, "dependency evidence limited to 2 declarations")


def test_test_marker_limit_keeps_the_profile_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_TEST_MARKERS", 2)
    tests = tmp_path / "tests"
    tests.mkdir()
    for index in range(4):
        (tests / f"case_{index}.rb").write_text("# case\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.markers == ("tests/case_0.rb", "tests/case_1.rb")


def test_unreadable_version_file_is_reported_and_skipped(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission-based failures are not reproducible as root")
    unreadable = tmp_path / "requirements.txt"
    unreadable.write_text("pytest==8.4.2\n", encoding="utf-8")
    unreadable.chmod(0o000)
    try:
        profile = profile_repository(tmp_path)
    finally:
        unreadable.chmod(0o600)

    assert profile.version_files == ()
    assert profile.dependencies == ()
    assert _warning_matching(profile, "could not fingerprint requirements.txt")
    assert _warning_matching(profile, "could not read requirements.txt")


def test_oversized_version_file_is_fingerprinted_from_its_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_FINGERPRINT_BYTES", 1_024)
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pytest"\nversion = "9.1.1"\n' + "# pad\n" * 20_000,
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.version_files == ("uv.lock",)
    assert _warning_matching(profile, "fingerprinted only the edges of oversized version file")


def test_oversized_manifest_is_skipped_without_dependency_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_MANIFEST_BYTES", 32)
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "19.0.0", "vite": "7.0.0"}}),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert profile.technologies == ()
    assert profile.version_files == ("package.json",)
    assert _warning_matching(profile, "skipped oversized manifest: package.json")


def test_invalid_and_empty_dependency_declarations_are_filtered(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"bad name!": "1.0.0", "empty": "   "},
                "peerDependencies": {"react": "^19.0.0"},
                "optionalDependencies": {"fsevents": 2},
                "devDependencies": "not-a-table",
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert {(item.name, item.group) for item in profile.dependencies} == {
        ("react", "peerDependencies")
    }
    assert RepositoryTechnology.REACT in profile.technologies
    assert _warning_matching(profile, "ignored invalid dependency name in package.json")


def test_package_json_package_manager_variants(tmp_path: Path) -> None:
    unknown = _repository(tmp_path, "unknown")
    (unknown / "package.json").write_text(
        json.dumps({"packageManager": "hermit@1.2.3"}),
        encoding="utf-8",
    )
    versionless = _repository(tmp_path, "versionless")
    (versionless / "package.json").write_text(
        json.dumps({"packageManager": "npm"}),
        encoding="utf-8",
    )

    unknown_profile = profile_repository(unknown)
    versionless_profile = profile_repository(versionless)

    assert unknown_profile.package_managers == ()
    assert unknown_profile.dependencies == ()
    assert versionless_profile.package_managers == (RepositoryPackageManager.NPM,)
    assert versionless_profile.dependencies == ()


def test_package_json_that_is_not_an_object_is_reported(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("[]", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.technologies == ()
    assert profile.markers == ()
    assert _warning_matching(profile, "invalid manifest package.json: expected an object")


def test_config_markers_detect_python_and_javascript_tooling(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("# lock\n", encoding="utf-8")
    (tmp_path / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "vitest.config.mts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert set(profile.technologies) == {
        RepositoryTechnology.JAVASCRIPT,
        RepositoryTechnology.PYTHON,
        RepositoryTechnology.TYPESCRIPT,
        RepositoryTechnology.VITE,
    }
    assert set(profile.test_tools) == {RepositoryTestTool.PYTEST, RepositoryTestTool.VITEST}
    assert profile.package_managers == (RepositoryPackageManager.POETRY,)
    assert "poetry.lock" in profile.markers
    assert "vite.config.ts" in profile.markers
    assert "vitest.config.mts" in profile.markers


def test_setup_cfg_and_tox_ini_pytest_detection(tmp_path: Path) -> None:
    setup_cfg = _repository(tmp_path, "setup-cfg")
    (setup_cfg / "setup.cfg").write_text(
        "[metadata]\nname = example\n\n[tool:pytest]\naddopts = -q\n",
        encoding="utf-8",
    )
    tox_ini = _repository(tmp_path, "tox-ini")
    (tox_ini / "tox.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
    plain = _repository(tmp_path, "plain")
    (plain / "tox.ini").write_text("[tox]\nenvlist = py313\n", encoding="utf-8")

    setup_cfg_profile = profile_repository(setup_cfg)
    tox_profile = profile_repository(tox_ini)
    plain_profile = profile_repository(plain)

    assert setup_cfg_profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert setup_cfg_profile.markers == ("setup.cfg",)
    assert setup_cfg_profile.version_files == ("setup.cfg",)
    assert tox_profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert tox_profile.technologies == (RepositoryTechnology.PYTHON,)
    assert plain_profile.test_tools == ()


def test_unparsable_python_config_is_reported(tmp_path: Path) -> None:
    broken = _repository(tmp_path, "broken")
    (broken / "tox.ini").write_text("[unclosed\nkey = value\n", encoding="utf-8")
    binary = _repository(tmp_path, "binary")
    (binary / "setup.cfg").write_bytes(b"[tool:pytest]\n\xff\xfe\n")

    broken_profile = profile_repository(broken)
    binary_profile = profile_repository(binary)

    assert broken_profile.technologies == ()
    assert _warning_matching(broken_profile, "invalid config tox.ini")
    assert binary_profile.test_tools == ()
    assert _warning_matching(binary_profile, "invalid config setup.cfg")


def test_requirements_parsing_ignores_comments_options_and_nameless_lines(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements-dev.txt").write_text(
        "\n".join(
            [
                "# comment only",
                "",
                "-e .",
                "./local-package",
                "==1.0",
                "ruff  # pinned separately",
                "httpx[http2]>=0.27 ; python_version >= '3.13'",
                "my_package==1.2.3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.package_managers == (RepositoryPackageManager.PIP,)
    assert profile.test_tools == ()
    assert {(item.name, item.declared_version) for item in profile.dependencies} == {
        ("ruff", "*"),
        ("httpx", "[http2]>=0.27 ; python_version >= '3.13'"),
        ("my-package", "==1.2.3"),
    }


def test_requirements_file_with_invalid_encoding_is_reported(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_bytes(b"pytest==8.4.2\n\xff\xfe\n")

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert profile.test_tools == ()
    assert _warning_matching(profile, "invalid requirements file requirements.txt")


def test_pyproject_tolerates_malformed_tables_and_records_optional_groups(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
requires-python = 3.13
dependencies = ["httpx>=0.27", 42, "==1.0"]

[project.optional-dependencies]
docs = ["mkdocs-material>=9.7"]

[dependency-groups]
dev = ["ruff"]

[tool]
poetry = "not-a-table"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.package_managers == ()
    assert profile.test_tools == ()
    assert {(item.name, item.declared_version, item.group) for item in profile.dependencies} == {
        ("httpx", ">=0.27", "project.dependencies"),
        ("mkdocs-material", ">=9.7", "project.optional-dependencies.docs"),
        ("ruff", "*", "dependency-groups.dev"),
    }


def test_pyproject_with_non_table_groups_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
dependencies = "not-a-list"
optional-dependencies = { docs = "not-a-list" }

[dependency-groups]
dev = "not-a-list"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert profile.technologies == (RepositoryTechnology.PYTHON,)
    assert profile.markers == ("pyproject.toml",)


def test_invalid_pyproject_is_reported_without_dependency_evidence(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\nname = 'example'\n", encoding="utf-8")

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert profile.package_managers == ()
    assert profile.test_tools == ()
    assert _warning_matching(profile, "invalid manifest pyproject.toml")


def test_poetry_declaration_variants_are_normalized(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry]
name = "example"

[tool.poetry.dependencies]
my_pkg = "^1.0"
weird = { git = "https://example.invalid/pkg.git" }
broken = 12

[tool.poetry.dev-dependencies]
pytest = "^8.4"

[tool.poetry.group]
malformed = "not-a-table"

[tool.poetry.group.docs]
dependencies = "not-a-table"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.package_managers == (RepositoryPackageManager.POETRY,)
    assert profile.test_tools == (RepositoryTestTool.PYTEST,)
    assert {(item.name, item.declared_version, item.group) for item in profile.dependencies} == {
        ("my-pkg", "^1.0", "tool.poetry.dependencies"),
        ("pytest", "^8.4", "tool.poetry.dev-dependencies"),
    }


def test_uv_lock_edge_shapes_are_tolerated(tmp_path: Path) -> None:
    invalid = _repository(tmp_path, "invalid")
    (invalid / "uv.lock").write_text("version = = 1\n", encoding="utf-8")
    unexpected = _repository(tmp_path, "unexpected")
    (unexpected / "uv.lock").write_text('package = "not-a-list"\n', encoding="utf-8")
    partial = _repository(tmp_path, "partial")
    (partial / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["pydantic>=2.9", "httpx>=0.27"]\n',
        encoding="utf-8",
    )
    (partial / "uv.lock").write_text(
        "\n".join(
            [
                "version = 1",
                'package = [1, { name = "httpx" }, { name = "pydantic", version = "2.11.0" }]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    invalid_profile = profile_repository(invalid)
    unexpected_profile = profile_repository(unexpected)
    partial_profile = profile_repository(partial)

    assert _warning_matching(invalid_profile, "invalid lockfile uv.lock")
    assert unexpected_profile.package_managers == (RepositoryPackageManager.UV,)
    assert unexpected_profile.warnings == ()
    resolved = {item.name: item.resolved_version for item in partial_profile.dependencies}
    assert resolved == {"pydantic": "2.11.0", "httpx": None}


def test_uv_lock_versions_outside_limits_are_not_used_as_resolutions(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["pydantic>=2.9"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        f'version = 1\n\n[[package]]\nname = "pydantic"\nversion = "{"9" * 201}"\n',
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    pydantic = next(item for item in profile.dependencies if item.name == "pydantic")
    assert pydantic.resolved_version is None
    assert pydantic.resolution_path is None
    assert profile.warnings == ()


def test_conflicting_lockfiles_are_reported_once_and_leave_versions_unresolved(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["pydantic>=2.9"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pydantic"\nversion = "2.11.0"\n',
        encoding="utf-8",
    )
    nested = tmp_path / "service"
    nested.mkdir()
    (nested / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "pydantic"\nversion = "2.9.2"\n',
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    pydantic = next(item for item in profile.dependencies if item.name == "pydantic")
    assert pydantic.resolved_version is None
    conflicts = [item for item in profile.warnings if "multiple locked versions" in item]
    assert conflicts == [
        f"multiple locked versions found for {DependencyEcosystem.PYTHON}:pydantic"
    ]


def test_package_lock_malformed_payloads_are_reported(tmp_path: Path) -> None:
    invalid = _repository(tmp_path, "invalid")
    (invalid / "package-lock.json").write_text("{", encoding="utf-8")
    not_an_object = _repository(tmp_path, "not-an-object")
    (not_an_object / "package-lock.json").write_text("[]", encoding="utf-8")
    unexpected_shape = _repository(tmp_path, "unexpected-shape")
    (unexpected_shape / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 1, "dependencies": "not-a-table"}),
        encoding="utf-8",
    )

    invalid_profile = profile_repository(invalid)
    not_an_object_profile = profile_repository(not_an_object)
    unexpected_shape_profile = profile_repository(unexpected_shape)

    assert invalid_profile.package_managers == (RepositoryPackageManager.NPM,)
    assert _warning_matching(invalid_profile, "invalid lockfile package-lock.json")
    assert _warning_matching(
        not_an_object_profile, "invalid lockfile package-lock.json: expected an object"
    )
    assert unexpected_shape_profile.warnings == ()


def test_package_lock_ignores_entries_without_usable_versions(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19.0.0", "vite": "^7.0.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "example"},
                    "node_modules/vite": "not-an-object",
                    "node_modules/react": {"version": 19},
                    "node_modules/left-pad": {"version": "1.3.0"},
                },
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    resolved = {item.name: item.resolved_version for item in profile.dependencies}
    assert resolved == {"react": None, "vite": None}


def test_legacy_package_lock_dependencies_supply_versions(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0", "vite": "^7.0.0"}}),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "react": {"version": "18.3.1"},
                    "vite": {"resolved": "https://registry.invalid/vite"},
                    "left-pad": "not-an-object",
                },
            }
        ),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    resolved = {item.name: item.resolved_version for item in profile.dependencies}
    assert resolved == {"react": "18.3.1", "vite": None}
    assert profile.warnings == ()


def test_pnpm_lock_malformed_documents_are_tolerated(tmp_path: Path) -> None:
    invalid = _repository(tmp_path, "invalid")
    (invalid / "pnpm-lock.yaml").write_text("importers: [unclosed\n", encoding="utf-8")
    sequence = _repository(tmp_path, "sequence")
    (sequence / "pnpm-lock.yaml").write_text("- first\n- second\n", encoding="utf-8")
    scalar_importers = _repository(tmp_path, "scalar-importers")
    (scalar_importers / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters: 3\n", encoding="utf-8"
    )

    invalid_profile = profile_repository(invalid)
    sequence_profile = profile_repository(sequence)
    scalar_profile = profile_repository(scalar_importers)

    assert _warning_matching(invalid_profile, "invalid lockfile pnpm-lock.yaml")
    assert sequence_profile.package_managers == (RepositoryPackageManager.PNPM,)
    assert sequence_profile.warnings == ()
    assert scalar_profile.warnings == ()


def test_pnpm_lock_skips_local_and_malformed_entries(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^19.0.0", "left-pad": "^1.0.0", "zod": "^3.0.0"},
                "devDependencies": {"vite": "^7.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text(
        """
lockfileVersion: '9.0'
importers:
  broken: 3
  .:
    dependencies:
      react:
        specifier: ^19.0.0
        version: link:../react
      left-pad: not-a-mapping
      zod:
        specifier: ^3.0.0
        version: 3
    devDependencies: not-a-table
    optionalDependencies:
      vite:
        specifier: ^7.0.0
        version: 7.0.4(rollup@4.0.0)
""".lstrip(),
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    resolved = {item.name: item.resolved_version for item in profile.dependencies}
    assert resolved == {"react": None, "left-pad": None, "zod": None, "vite": "7.0.4"}
    assert profile.warnings == ()


def test_yarn_and_bun_lockfiles_are_recorded_without_resolution_parsing(
    tmp_path: Path,
) -> None:
    yarn = _repository(tmp_path, "yarn")
    (yarn / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19.0.0"}}),
        encoding="utf-8",
    )
    (yarn / "yarn.lock").write_text('react@^19.0.0:\n  version "19.1.1"\n', encoding="utf-8")
    bun = _repository(tmp_path, "bun")
    (bun / "bun.lockb").write_bytes(b"\x00binary-lock\x00")

    yarn_profile = profile_repository(yarn)
    bun_profile = profile_repository(bun)

    assert yarn_profile.package_managers == (RepositoryPackageManager.YARN,)
    assert yarn_profile.dependencies[0].resolved_version is None
    assert "yarn.lock" in yarn_profile.version_files
    assert bun_profile.package_managers == (RepositoryPackageManager.BUN,)
    assert bun_profile.markers == ("bun.lockb",)
    assert bun_profile.warnings == ()


def test_oversized_manifests_are_skipped_by_every_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository_profile_module, "MAX_MANIFEST_BYTES", 8)
    padding = "# padding padding padding\n"
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\ndependencies = ["httpx>=0.27"]\n' + padding,
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n',
        encoding="utf-8",
    )
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\naddopts = -q\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": {"node_modules/react": {"version": "19.1.1"}}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters: {}\n", encoding="utf-8"
    )

    profile = profile_repository(tmp_path)

    assert profile.dependencies == ()
    assert profile.test_tools == ()
    skipped = {
        warning.removeprefix("skipped oversized manifest: ")
        for warning in profile.warnings
        if warning.startswith("skipped oversized manifest: ")
    }
    assert skipped == {
        "package-lock.json",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "setup.cfg",
        "uv.lock",
    }


def test_poetry_group_table_of_the_wrong_type_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry]
name = "example"
group = "not-a-table"

[tool.poetry.dependencies]
pydantic = "^2.11"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    profile = profile_repository(tmp_path)

    assert profile.package_managers == (RepositoryPackageManager.POETRY,)
    assert {item.name for item in profile.dependencies} == {"pydantic"}
    assert profile.warnings == ()
