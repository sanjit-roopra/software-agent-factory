"""Static and script-level validation for release workflow hardening."""

from __future__ import annotations

import importlib.util
import stat
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PACKAGING_SPEC = ROOT / "packaging" / "pyinstaller.spec"
ACTION_LINE = (
    r"^\s*uses:\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)"
    r"@([0-9a-f]{40})\s+#\s+(v[^\s]+)\s*$"
)
EXPECTED_ACTIONS = {
    "actions/attest": ("1e69f48acb82d1966a394da916b4c1698aa569d6", "v4.2.2"),
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/dependency-review-action": (
        "a1d282b36b6f3519aa1f3fc636f609c47dddb294",
        "v5.0.0",
    ),
    "actions/setup-python": ("5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    "astral-sh/setup-uv": ("20cfd1bf945f4377ade1205e4dbc17946fc9a30d", "v10.0.1"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7.0.1"),
    "actions/download-artifact": ("3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", "v8.0.1"),
    "github/codeql-action/analyze": (
        "cdf488f595d80d6e07e03d4674febd5ab45fa938",
        "v4.37.9",
    ),
    "github/codeql-action/init": (
        "cdf488f595d80d6e07e03d4674febd5ab45fa938",
        "v4.37.9",
    ),
}


def _load_workflow(name: str) -> tuple[str, dict[str, Any]]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return text, yaml.load(text, Loader=yaml.BaseLoader)


def _load_script_module(name: str, relative_path: str) -> ModuleType:
    script_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_third_party_actions_are_pinned_to_full_shas_with_release_comments() -> None:
    import re

    action_line = re.compile(ACTION_LINE)
    for workflow_path in sorted(WORKFLOWS.glob("*.yml")):
        workflow_name = workflow_path.name
        text, _ = _load_workflow(workflow_name)
        for line in text.splitlines():
            if "uses:" not in line or "./" in line:
                continue
            match = action_line.match(line)
            assert match, f"Unpinned or uncommented action reference: {workflow_name}: {line}"
            action, sha, tag = match.groups()
            assert action in EXPECTED_ACTIONS
            assert (sha, tag) == EXPECTED_ACTIONS[action]


def test_pyinstaller_spec_resolves_repo_root_from_the_packaging_directory() -> None:
    spec_text = PACKAGING_SPEC.read_text(encoding="utf-8")
    assert "SPECPATH" in spec_text
    assert "project_root = Path(SPECPATH).resolve().parent" in spec_text
    assert 'package_root = project_root / "src" / "software_agent_factory"' in spec_text
    assert "exclude_binaries=True" in spec_text


def test_ci_workflow_has_secure_triggers_permissions_and_archive_smokes() -> None:
    text, workflow = _load_workflow("ci.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert "tags" not in workflow["on"]["push"]
    assert "pull_request_target" not in text
    assert "persist-credentials: false" in text
    assert "uv sync --locked --no-default-groups --group quality" in text
    assert "uv sync --locked --no-default-groups --group test" in text
    assert "uv sync --locked --no-default-groups --group distribution" in text
    assert "uv sync --locked --no-default-groups --group native --group test" in text
    assert "uv lock --check" not in text
    assert "uv run --no-sync ruff format --check ." in text
    assert "uv run --no-sync ruff check --output-format=github ." in text
    assert "uv run --no-sync mypy src/software_agent_factory scripts/release" in text
    assert "--cov=software_agent_factory" in text
    assert '"3.13"' in text
    assert '"3.14"' in text
    assert "uv build --no-sources" in text
    assert "uv run --no-sync twine check dist/*" in text
    assert "uv run --no-sync check-wheel-contents dist/*.whl" in text
    assert "scripts/release/prepare_frozen_bundle.py" in text
    assert "scripts/release/smoke_factory.py" in text
    assert '--archive "$ARCHIVE"' in text
    assert '--expect-architecture "arm64"' in text
    assert '--expect-architecture "x86_64"' in text
    assert "COPYFILE_DISABLE=1 tar" in text
    assert "packaging/venvs/wheel-smoke/bin/pip install dist/*.whl" in text
    assert "packaging/venvs/sdist-smoke/bin/pip install dist/*.tar.gz" in text
    assert "VERSION=$(PYTHONPATH=src uv run --no-sync python" in text
    assert workflow["env"]["UV_VERSION"] == "0.9.17"
    for job in workflow["jobs"].values():
        assert "timeout-minutes" in job


def test_ci_workflow_limits_native_macos_to_main_and_manual_dispatch() -> None:
    _text, workflow = _load_workflow("ci.yml")
    jobs = {
        "macos-arm64": "macos-15",
        "macos-x86_64": "macos-15-intel",
    }
    for job_name, runner in jobs.items():
        macos_job = workflow["jobs"][job_name]
        assert macos_job["runs-on"] == runner
        condition = macos_job["if"]
        assert "workflow_dispatch" in condition
        assert "refs/heads/main" in condition
        assert "refs/tags/" not in condition


def test_security_workflow_has_dependency_review_codeql_and_locked_audit() -> None:
    text, workflow = _load_workflow("security.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["pull_request"] in ("", None)
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["schedule"]
    assert "pull_request_target" not in text
    assert "actions/dependency-review-action@" in text
    assert "fail-on-severity: moderate" in text
    assert {"python", "actions"} == set(
        workflow["jobs"]["codeql"]["strategy"]["matrix"]["language"]
    )
    assert workflow["jobs"]["codeql"]["permissions"] == {
        "actions": "read",
        "contents": "read",
    }
    assert "upload: never" in text
    assert "upload-database: false" in text
    assert "Enforce CodeQL findings" in text
    assert "Upload CodeQL SARIF" in text
    assert "uv run --no-sync pip-audit --skip-editable" in text


def test_future_python_compatibility_runs_only_on_schedule_or_manual_dispatch() -> None:
    text, workflow = _load_workflow("compatibility.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["jobs"]["python-prerelease"]["name"] == "Python 3.15 prerelease"
    assert 'python-version: "3.15"' in text
    assert "allow-prereleases: true" in text


def test_dependabot_updates_uv_dependencies_and_actions() -> None:
    config = yaml.load((ROOT / ".github" / "dependabot.yml").read_text(), Loader=yaml.BaseLoader)
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert ecosystems == {"uv", "github-actions"}


def test_release_workflow_has_immutable_publish_shape() -> None:
    text, workflow = _load_workflow("release.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["tag"]["required"] == "true"
    assert "persist-credentials: false" in text
    for job_name, job in workflow["jobs"].items():
        if job_name == "publish":
            continue
        permissions = job.get("permissions")
        assert permissions in (None, {"contents": "read"})
    assert "pull_request_target" not in text
    assert "write-all" not in text
    assert "gh release create" in text
    assert "gh release view" in text
    assert "uv run --no-sync ruff format --check ." in text
    assert "uv run --no-sync ruff check ." in text
    assert "uv run --no-sync mypy src/software_agent_factory scripts/release" in text
    assert "uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch" in text
    assert "uv run --no-sync pip-audit --skip-editable" in text
    assert "uv run --no-sync twine check dist/*" in text
    assert "uv run --no-sync check-wheel-contents dist/*.whl" in text
    assert "PYTHONPATH=src uv run --no-sync python scripts/release/generate_build_info.py" in text
    assert "VERSION=$(PYTHONPATH=src uv run --no-sync python" in text
    assert "shasum -a 256 -c SHA256SUMS" in text
    assert "macos-15" in text
    assert "macos-15-intel" in text
    assert "--validate-tag-match" in text
    assert '--commit-sha "$(git rev-parse HEAD)"' in text
    assert '--commit-sha "$GITHUB_SHA"' not in text
    assert "path: packaging/python-distributions/" in text
    assert "packaging/build-info-py3-none-any.json" in text
    assert text.count("if-no-files-found: error") == 3
    assert "scripts/release/prepare_frozen_bundle.py" in text
    assert "scripts/release/smoke_factory.py" in text
    assert '--archive "$ARCHIVE"' in text
    assert '--expect-architecture "arm64"' in text
    assert '--expect-architecture "x86_64"' in text
    assert "COPYFILE_DISABLE=1 tar" in text
    assert workflow["jobs"]["publish"]["permissions"] == {
        "artifact-metadata": "write",
        "attestations": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert "actions/attest@" in text
    assert "github.event.repository.visibility == 'public'" in text
    assert "packaging/venvs/release-sdist-smoke" in text


def test_combine_build_info_requires_consistent_release_identity() -> None:
    module = _load_script_module("combine_build_info", "scripts/release/combine_build_info.py")
    entries = [
        {
            "project": "software-agent-factory",
            "version": "1.2.3",
            "tag": "v1.2.3",
            "commit_sha": "abc123",
        },
        {
            "project": "software-agent-factory",
            "version": "1.2.3",
            "tag": "v1.2.3",
            "commit_sha": "def456",
        },
    ]

    assert module._consistent_value(entries, "version") == "1.2.3"
    with pytest.raises(SystemExit, match="commit_sha"):
        module._consistent_value(entries, "commit_sha")


def test_prepare_frozen_bundle_writes_install_instructions_and_optional_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "software-agent-factory"
    bundle.mkdir()
    notices_root = tmp_path / "notices"
    notices_root.mkdir()
    (notices_root / "LICENSE").write_text("example license\n", encoding="utf-8")

    module = _load_script_module(
        "prepare_frozen_bundle", "scripts/release/prepare_frozen_bundle.py"
    )
    monkeypatch.setattr(module, "ROOT", notices_root)

    module.prepare_bundle(bundle, version="1.2.3", architecture="arm64")

    instructions = (bundle / module.INSTALL_FILENAME).read_text(encoding="utf-8")
    assert "macOS arm64" in instructions
    assert "./factory --version" in instructions
    assert "./factory doctor" in instructions
    assert "./factory service install" in instructions
    assert "required: git" in instructions
    assert "scheduler.enabled" in instructions
    assert "optional: copilot" in instructions
    assert "unsigned or ad-hoc signed" in instructions
    assert (bundle / "LICENSE").read_text(encoding="utf-8") == "example license\n"


def test_smoke_workspace_parent_rejects_protected_and_unsafe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    smoke_home = tmp_path / "smoke-home"
    smoke_home.mkdir()
    smoke_repo = tmp_path / "smoke-repo"
    smoke_repo.mkdir()
    monkeypatch.setattr(module.Path, "home", lambda: smoke_home)
    monkeypatch.setattr(module, "REPO_ROOT", smoke_repo)

    with pytest.raises(SystemExit, match="protected workspace root"):
        module._validate_workspace_parent(Path("/"))
    with pytest.raises(SystemExit, match="protected workspace root"):
        module._validate_workspace_parent(smoke_home)
    with pytest.raises(SystemExit, match="protected workspace root"):
        module._validate_workspace_parent(smoke_repo)
    plain_parent = tmp_path / "plain-parent"
    plain_parent.mkdir()
    with pytest.raises(SystemExit, match="non-distinctive workspace root"):
        module._validate_workspace_parent(plain_parent / "ordinary-root")


def test_smoke_workspace_parent_rejects_existing_non_smoke_content(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    parent = tmp_path / "release-smoke"
    parent.mkdir()
    (parent / "keep.txt").write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="non-marker/non-smoke"):
        module._validate_workspace_parent(parent)


def test_smoke_workspace_parent_rejects_symlink_ambiguity(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    target = tmp_path / "smoke-target"
    target.mkdir()
    symlink_root = tmp_path / "smoke-link"
    symlink_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(SystemExit, match="symlinked smoke path"):
        module._validate_workspace_parent(symlink_root)


def test_smoke_script_checks_archive_instructions_and_executable_mode(tmp_path: Path) -> None:
    prepare_module = _load_script_module(
        "prepare_frozen_bundle", "scripts/release/prepare_frozen_bundle.py"
    )
    smoke_module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")

    bundle = tmp_path / "bundle" / "software-agent-factory"
    bundle.mkdir(parents=True)
    executable = bundle / "factory"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    prepare_module.prepare_bundle(bundle, version="1.2.3", architecture="arm64")

    archive = tmp_path / "software-agent-factory-1.2.3-macos-arm64.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(bundle, arcname=bundle.name)

    parent = smoke_module._validate_workspace_parent(tmp_path / "archive-smoke")
    workspace = smoke_module._create_workspace(parent)
    try:
        extracted_bundle = smoke_module._load_bundle_from_archive(
            archive,
            workspace,
            expect_architecture="arm64",
        )
        smoke_module._assert_executable_mode(extracted_bundle / "factory")
        assert (extracted_bundle / prepare_module.INSTALL_FILENAME).is_file()
    finally:
        smoke_module._cleanup_workspace(workspace)


def test_smoke_script_rejects_non_executable_bundle_binary(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    executable = tmp_path / "factory"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(SystemExit, match="not marked executable"):
        module._assert_executable_mode(executable)


def test_smoke_script_exercises_doctor_status_and_the_prerequisite_failure() -> None:
    """The release smoke must prove the *shipped* commands work offline, not
    just that the binary starts."""
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")

    for name in (
        "_smoke_cli",
        "_smoke_doctor",
        "_smoke_status",
        "_smoke_service_status_is_read_only",
        "_smoke_missing_git_prerequisite",
        "_smoke_fake_run",
    ):
        assert callable(getattr(module, name)), f"smoke script is missing {name}"

    source = (ROOT / "scripts" / "release" / "smoke_factory.py").read_text(encoding="utf-8")
    assert module.PREREQUISITE_EXIT_CODE == 2
    # The service is only ever *queried*: a smoke run must not install, load
    # or remove a LaunchAgent on the machine running it. (The archive's
    # INSTALL.txt still documents 'factory service install' for humans, which
    # is why this checks the invoked argv shape rather than the word.)
    assert '"service", "status"' in source
    assert '"service", "install"' not in source
    assert '"service", "uninstall"' not in source


def test_smoke_missing_git_prerequisite_uses_a_controlled_path(tmp_path: Path) -> None:
    """Runs the real check against a stub executable: with an empty ``PATH``
    the CLI must exit 2 with an explicit message and no traceback."""
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")

    stub = tmp_path / "factory"
    stub.write_text(
        '#!/bin/sh\necho "missing required executable(s) on PATH: git" >&2\nexit 2\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    repo.mkdir()

    module._smoke_missing_git_prerequisite(stub, repo, tmp_path)


def test_smoke_missing_git_prerequisite_rejects_a_traceback(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")

    stub = tmp_path / "factory"
    stub.write_text(
        '#!/bin/sh\necho "Traceback (most recent call last): git" >&2\nexit 2\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(SystemExit, match="explicit git prerequisite error"):
        module._smoke_missing_git_prerequisite(stub, repo, tmp_path)


def test_pyinstaller_spec_bundles_config_and_build_info_without_dashboard_assets() -> None:
    """The dashboard is asset-free (HTML/CSS/JS are Python constants), so the
    spec must not reference a static directory that does not exist."""
    spec_text = PACKAGING_SPEC.read_text(encoding="utf-8")

    assert '"default_config.yaml"' in spec_text
    assert 'build_info_path = package_root / "build-info.json"' in spec_text
    assert 'collect_submodules("software_agent_factory")' in spec_text
    assert "dashboard/static" not in spec_text
    assert "__main__.py" in spec_text

    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dashboard/static" not in pyproject_text
    assert 'factory = "software_agent_factory.__main__:main"' in pyproject_text
