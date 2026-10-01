"""Static and script-level validation for release workflow hardening."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
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
ALLOWED_ACTIONS = frozenset(
    {
        "actions/attest",
        "actions/checkout",
        "actions/configure-pages",
        "actions/dependency-review-action",
        "actions/deploy-pages",
        "actions/download-artifact",
        "actions/setup-node",
        "actions/setup-python",
        "actions/upload-artifact",
        "actions/upload-pages-artifact",
        "astral-sh/setup-uv",
        "github/codeql-action/analyze",
        "github/codeql-action/init",
    }
)


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
            action, _sha, _tag = match.groups()
            assert action in ALLOWED_ACTIONS


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
    assert "uv sync --locked --no-build --no-default-groups --group quality" in text
    assert "uv sync --locked --no-build --no-default-groups --group test" in text
    assert (
        "uv sync --locked --no-build --no-default-groups --group distribution --group native"
        in text
    )
    assert "uv sync --locked --no-build --no-default-groups --group native --group test" in text
    assert "uv lock --check" not in text
    assert "uv run --no-sync --no-build ruff format --check ." in text
    assert "uv run --no-sync --no-build ruff check --output-format=github ." in text
    assert (
        "uv run --no-sync --no-build mypy src/software_agent_factory scripts/docs scripts/release"
        in text
    )
    assert "--cov=src/software_agent_factory" in text
    assert '"3.13"' in text
    assert '"3.14"' in text
    assert "uv build --no-sources" in text
    assert "uv run --no-sync --no-build twine check dist/*" in text
    assert "uv run --no-sync --no-build check-wheel-contents dist/*.whl" in text
    assert "uv run --no-sync --no-build mkdocs build --strict" in text
    assert "uv run --no-sync --no-build python scripts/docs/check_simple_english.py" in text
    assert "python scripts/docs/check_rendered_links.py site" in text
    assert workflow["jobs"]["ci-gate"]["needs"] == [
        "quality",
        "tests",
        "package",
        "docs",
        "sonar-new-issues",
    ]
    assert "scripts/release/prepare_frozen_bundle.py" in text
    assert "scripts/release/smoke_factory.py" in text
    assert '--archive "$ARCHIVE"' in text
    assert '--expect-architecture "arm64"' in text
    assert '--expect-architecture "x86_64"' in text
    assert "COPYFILE_DISABLE=1 tar" in text
    assert "packaging/venvs/wheel-smoke/bin/pip install dist/*.whl" in text
    assert (
        'packaging/venvs/wheel-smoke/bin/python -c "import software_agent_factory.dashboard.assets"'
        in text
    )
    assert "packaging/venvs/sdist-smoke/bin/pip install dist/*.tar.gz" in text
    assert "VERSION=$(PYTHONPATH=src uv run --no-sync --no-build python" in text
    assert workflow["env"]["UV_VERSION"] == "0.12.19"
    for job in workflow["jobs"].values():
        assert "timeout-minutes" in job


def _sonar_job_steps() -> dict[str, str]:
    _text, workflow = _load_workflow("ci.yml")
    steps = workflow["jobs"]["sonar-new-issues"]["steps"]
    return {step["name"]: step["run"] for step in steps}


def _run_step(
    script: str,
    tmp_path: Path,
    env: dict[str, str],
    curl_body: str | None,
    curl_failures: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Execute a workflow ``run:`` script with ``curl`` and ``sleep`` stubbed.

    The ``curl`` stub fails like ``curl -f`` on an HTTP error for its first
    ``curl_failures`` calls, and always when ``curl_body`` is ``None``;
    otherwise it prints ``curl_body``. Calls are counted in ``curl-calls``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body_file = tmp_path / "curl-body"
    calls_file = tmp_path / "curl-calls"
    calls_file.write_text("0", encoding="utf-8")
    if curl_body is None:
        curl_failures = 1_000_000
    else:
        body_file.write_text(curl_body, encoding="utf-8")
    curl = (
        "#!/bin/sh\n"
        f"n=$(($(cat '{calls_file}') + 1)); echo $n > '{calls_file}'\n"
        f"[ $n -le {curl_failures} ] && exit 22\n"
        f"cat '{body_file}'\n"
    )
    for name, content in (("curl", curl), ("sleep", "#!/bin/sh\nexit 0\n")):
        stub = bin_dir / name
        stub.write_text(content, encoding="utf-8")
        stub.chmod(0o755)
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
    return subprocess.run(
        # GitHub runs a run: step with no shell: set as ``bash -e {0}``.
        ["bash", "-e", "-c", script],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
SONAR_ENV = {"PROJECT_KEY": "proj", "PR_NUMBER": "7", "HEAD_SHA": "abc123"}


def _pr_list(sha: str) -> str:
    return json.dumps({"pullRequests": [{"key": "7", "commit": {"sha": sha}}]})


def _issues(total: object, issues: list[dict[str, object]] | None = None) -> str:
    return json.dumps({"paging": {"total": total}, "issues": issues or []})


def test_ci_sonar_job_runs_only_on_pull_requests_without_permissions() -> None:
    _text, workflow = _load_workflow("ci.yml")
    job = workflow["jobs"]["sonar-new-issues"]

    assert job["if"] == "github.event_name == 'pull_request'"
    assert job["permissions"] == {}


@requires_jq
def test_ci_sonar_wait_passes_once_head_commit_is_analysed(tmp_path: Path) -> None:
    wait = _sonar_job_steps()["Wait for SonarCloud to analyse the PR head commit"]

    result = _run_step(wait, tmp_path, SONAR_ENV, _pr_list("abc123"))

    assert result.returncode == 0


@requires_jq
def test_ci_sonar_wait_recovers_after_transient_sonarcloud_errors(tmp_path: Path) -> None:
    wait = _sonar_job_steps()["Wait for SonarCloud to analyse the PR head commit"]

    result = _run_step(wait, tmp_path, SONAR_ENV, _pr_list("abc123"), curl_failures=2)

    assert result.returncode == 0
    assert (tmp_path / "curl-calls").read_text(encoding="utf-8").strip() == "3"


@requires_jq
def test_ci_sonar_wait_fails_when_only_an_older_commit_is_analysed(tmp_path: Path) -> None:
    wait = _sonar_job_steps()["Wait for SonarCloud to analyse the PR head commit"]

    result = _run_step(wait, tmp_path, SONAR_ENV, _pr_list("old999"))

    assert result.returncode == 1
    assert "did not analyse abc123" in result.stdout


@requires_jq
def test_ci_sonar_wait_fails_when_sonarcloud_is_unreachable(tmp_path: Path) -> None:
    wait = _sonar_job_steps()["Wait for SonarCloud to analyse the PR head commit"]

    result = _run_step(wait, tmp_path, SONAR_ENV, None)

    assert result.returncode == 1


@requires_jq
def test_ci_sonar_check_passes_with_zero_open_issues(tmp_path: Path) -> None:
    check = _sonar_job_steps()["Require zero open SonarCloud issues on this PR"]

    result = _run_step(check, tmp_path, SONAR_ENV, _issues(0))

    assert result.returncode == 0


@requires_jq
def test_ci_sonar_check_fails_with_open_issues(tmp_path: Path) -> None:
    check = _sonar_job_steps()["Require zero open SonarCloud issues on this PR"]
    issue = {"component": "proj:src/a.py", "line": 3, "rule": "python:S1", "message": "m"}

    result = _run_step(check, tmp_path, SONAR_ENV, _issues(1, [issue]))

    assert result.returncode == 1
    assert "::error file=src/a.py,line=3::python:S1 m" in result.stdout


@requires_jq
@pytest.mark.parametrize("total", [None, "many"])
def test_ci_sonar_check_fails_on_unexpected_response(tmp_path: Path, total: object) -> None:
    check = _sonar_job_steps()["Require zero open SonarCloud issues on this PR"]

    result = _run_step(check, tmp_path, SONAR_ENV, _issues(total))

    assert result.returncode != 0


@requires_jq
def test_ci_sonar_check_escapes_injected_workflow_commands(tmp_path: Path) -> None:
    check = _sonar_job_steps()["Require zero open SonarCloud issues on this PR"]
    issue = {"component": "proj:a,b.py", "rule": "r", "message": "x\n::add-mask::y"}

    result = _run_step(check, tmp_path, SONAR_ENV, _issues(1, [issue]))

    assert "::error file=a%2Cb.py,line=1::r x%0A::add-mask::y" in result.stdout
    assert not any(line.startswith("::add-mask::") for line in result.stdout.splitlines())


def _ci_gate_script() -> str:
    _text, workflow = _load_workflow("ci.yml")
    script: str = workflow["jobs"]["ci-gate"]["steps"][0]["run"]
    for job in ("quality", "tests", "package", "docs"):
        script = script.replace(f"${{{{ needs.{job}.result }}}}", "success")
    return script


@pytest.mark.parametrize(
    ("ref", "sonar_result", "expected_code"),
    [
        ("refs/pull/7/merge", "success", 0),
        ("refs/pull/7/merge", "failure", 1),
        ("refs/heads/feature", "skipped", 1),
        ("refs/heads/main", "skipped", 0),
    ],
)
def test_ci_gate_requires_sonar_job_on_every_ref_except_main(
    tmp_path: Path, ref: str, sonar_result: str, expected_code: int
) -> None:
    env = {"GITHUB_REF": ref, "SONAR_RESULT": sonar_result}

    result = _run_step(_ci_gate_script(), tmp_path, env, "")

    assert result.returncode == expected_code


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


def test_security_workflow_has_pull_request_audit_codeql_and_locked_audit() -> None:
    text, workflow = _load_workflow("security.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["pull_request"] in ("", None)
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["schedule"]
    assert "pull_request_target" not in text
    assert "actions/dependency-review-action@" in text
    assert "github.event.repository.visibility == 'public'" in text
    dependency_review = workflow["jobs"]["dependency-review"]
    assert dependency_review["if"] == "github.event_name == 'pull_request'"
    assert "uv sync --locked --no-build --no-default-groups --group security" in str(
        dependency_review
    )
    assert "uv run --no-sync --no-build pip-audit --skip-editable" in str(dependency_review)
    assert {"python", "actions"} == set(
        workflow["jobs"]["codeql"]["strategy"]["matrix"]["language"]
    )
    assert workflow["jobs"]["codeql"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "security-events": "write",
    }
    assert "github.event.repository.visibility == 'public' && 'always' || 'never'" in text
    assert "upload-database: false" in text
    assert "Enforce CodeQL findings" in text
    assert "Upload CodeQL SARIF" in text
    assert "uv run --no-sync --no-build pip-audit --skip-editable" in text


def test_docs_workflow_builds_strictly_and_deploys_only_from_main() -> None:
    text, workflow = _load_workflow("docs.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["concurrency"] == {
        "group": "pages",
        "cancel-in-progress": "false",
    }
    assert "pull_request_target" not in text
    assert "persist-credentials: false" in text
    assert "uv sync --locked --no-build --no-default-groups --group docs" in text
    assert "uv run --no-sync --no-build python scripts/docs/check_simple_english.py" in text
    assert "uv run --no-sync --no-build mkdocs build --strict" in text
    assert "python scripts/docs/check_rendered_links.py site" in text
    assert "scripts/docs/**" in workflow["on"]["push"]["paths"]
    assert "README.md" in workflow["on"]["push"]["paths"]
    assert "actions/configure-pages@" in text
    assert "actions/upload-pages-artifact@" in text
    assert workflow["jobs"]["build"]["permissions"] == {
        "contents": "read",
        "pages": "read",
    }
    assert workflow["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["jobs"]["deploy"]["permissions"] == {
        "id-token": "write",
        "pages": "write",
    }
    assert "actions/deploy-pages@" in text


def test_dependabot_updates_uv_dependencies_and_actions() -> None:
    config = yaml.load((ROOT / ".github" / "dependabot.yml").read_text(), Loader=yaml.BaseLoader)
    ecosystems = {entry["package-ecosystem"] for entry in config["updates"]}
    assert ecosystems == {"uv", "github-actions"}
    for entry in config["updates"]:
        group = next(iter(entry["groups"].values()))
        assert group["patterns"] == ["*"]
        assert group["update-types"] == ["minor", "patch"]


def test_release_workflow_has_safe_publish_shape() -> None:
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
    assert "--generate-notes" in text
    assert "--verify-tag" in text
    assert "gh release view" in text
    assert "uv run --no-sync --no-build ruff format --check ." in text
    assert "uv run --no-sync --no-build ruff check ." in text
    assert (
        "uv run --no-sync --no-build mypy src/software_agent_factory scripts/docs scripts/release"
        in text
    )
    assert (
        "uv run --no-sync --no-build pytest -q --cov=src/software_agent_factory --cov-branch"
        in text
    )
    assert "uv run --no-sync --no-build pip-audit --skip-editable" in text
    assert "uv run --no-sync --no-build python scripts/docs/check_simple_english.py" in text
    assert "uv run --no-sync --no-build mkdocs build --strict" in text
    assert "python scripts/docs/check_rendered_links.py site" in text
    assert "uv run --no-sync --no-build twine check dist/*" in text
    assert "uv run --no-sync --no-build check-wheel-contents dist/*.whl" in text
    assert (
        "PYTHONPATH=src uv run --no-sync --no-build python scripts/release/generate_build_info.py"
        in text
    )
    assert "VERSION=$(PYTHONPATH=src uv run --no-sync --no-build python" in text
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
    assert (
        "packaging/venvs/release-wheel-smoke/bin/python "
        '-c "import software_agent_factory.dashboard.assets"'
    ) in text


def test_release_python_distributions_do_not_require_pyinstaller() -> None:
    _text, workflow = _load_workflow("release.yml")
    distribution_job = workflow["jobs"]["python-distributions"]
    build_step = next(
        step for step in distribution_job["steps"] if step["name"] == "Build wheel and sdist"
    )

    assert "PyInstaller" not in build_step["run"]
    assert "--pyinstaller-version" not in build_step["run"]


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


@pytest.fixture
def combine_build_info_in_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, Path]:
    module = _load_script_module("combine_build_info", "scripts/release/combine_build_info.py")
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return module, workdir


def test_combine_build_info_refuses_absolute_path_outside_working_directory(
    combine_build_info_in_workdir: tuple[ModuleType, Path],
) -> None:
    module, workdir = combine_build_info_in_workdir
    outside = workdir.parent / "build-info.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the working directory"):
        module._load(outside)


def test_combine_build_info_refuses_relative_traversal(
    combine_build_info_in_workdir: tuple[ModuleType, Path],
) -> None:
    module, workdir = combine_build_info_in_workdir
    (workdir.parent / "build-info.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the working directory"):
        module._load(Path("../build-info.json"))


def test_combine_build_info_refuses_sibling_sharing_cwd_prefix(
    combine_build_info_in_workdir: tuple[ModuleType, Path],
) -> None:
    module, workdir = combine_build_info_in_workdir
    sibling = workdir.parent / "work-evil"
    sibling.mkdir()
    (sibling / "build-info.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="outside the working directory"):
        module._load(sibling / "build-info.json")


def test_combine_build_info_refuses_symlink_escaping_working_directory(
    combine_build_info_in_workdir: tuple[ModuleType, Path],
) -> None:
    module, workdir = combine_build_info_in_workdir
    outside = workdir.parent / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    (workdir / "link.json").symlink_to(outside)

    with pytest.raises(ValueError, match="outside the working directory"):
        module._load(Path("link.json"))


def test_combine_build_info_loads_path_within_working_directory(
    combine_build_info_in_workdir: tuple[ModuleType, Path],
) -> None:
    module, workdir = combine_build_info_in_workdir
    (workdir / "ok.json").write_text('{"project": "x"}', encoding="utf-8")

    assert module._load(Path("ok.json")) == {"project": "x"}


def test_combine_build_info_accepts_any_path_when_cwd_is_filesystem_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module("combine_build_info", "scripts/release/combine_build_info.py")
    monkeypatch.chdir("/")

    assert module._resolve_within_cwd(Path("/usr")) == Path(os.path.realpath("/usr"))


def test_combine_build_info_main_refuses_output_outside_working_directory(
    combine_build_info_in_workdir: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, workdir = combine_build_info_in_workdir
    entry = {"project": "p", "version": "1", "tag": "v1", "commit_sha": "abc"}
    (workdir / "info.json").write_text(json.dumps(entry), encoding="utf-8")
    outside = workdir.parent / "build-info.json"
    monkeypatch.setattr(
        sys, "argv", ["combine_build_info.py", "--output", str(outside), "info.json"]
    )

    with pytest.raises(ValueError, match="outside the working directory"):
        module.main()
    assert not outside.exists()


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
        "_smoke_dashboard_assets",
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


_DASHBOARD_STUB = """#!{python}
import http.server, os, pathlib, sys

STATUS, BODY = {status}, {body!r}
pathlib.Path({pid_file!r}).write_text(str(os.getpid()))
if {bad_first_line}:
    print("boom: no url here", flush=True)
    print("stub stderr text", file=sys.stderr, flush=True)
    sys.exit(1)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(STATUS)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(f"dashboard: http://127.0.0.1:{{server.server_port}}/?token=t", flush=True)
server.serve_forever()
"""


def _dashboard_stub(
    tmp_path: Path, *, status: int = 200, body: bytes = b"asset", bad_first_line: bool = False
) -> tuple[Path, Path]:
    """A stub ``factory`` that serves every path on a free loopback port.

    Returns the executable and the file the stub writes its pid to.
    """
    pid_file = tmp_path / "stub.pid"
    stub = tmp_path / "factory"
    stub.write_text(
        _DASHBOARD_STUB.format(
            python=sys.executable,
            status=status,
            body=body,
            pid_file=str(pid_file),
            bad_first_line=bad_first_line,
        ),
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub, pid_file


def _assert_process_gone(pid_file: Path) -> None:
    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_smoke_dashboard_assets_accepts_a_dashboard_that_serves_both_assets(
    tmp_path: Path,
) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    stub, pid_file = _dashboard_stub(tmp_path)

    module._smoke_dashboard_assets(stub, tmp_path)

    _assert_process_gone(pid_file)


def test_smoke_dashboard_assets_rejects_a_first_line_without_the_url(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    stub, pid_file = _dashboard_stub(tmp_path, bad_first_line=True)

    with pytest.raises(SystemExit, match="did not start:\nboom: no url here\nstub stderr text"):
        module._smoke_dashboard_assets(stub, tmp_path)

    _assert_process_gone(pid_file)


def test_smoke_dashboard_assets_rejects_a_status_other_than_200(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    stub, pid_file = _dashboard_stub(tmp_path, status=202, body=b"asset")

    with pytest.raises(SystemExit, match="did not serve /assets/app.js"):
        module._smoke_dashboard_assets(stub, tmp_path)

    _assert_process_gone(pid_file)


def test_smoke_dashboard_assets_rejects_an_empty_asset(tmp_path: Path) -> None:
    module = _load_script_module("smoke_factory", "scripts/release/smoke_factory.py")
    stub, pid_file = _dashboard_stub(tmp_path, body=b"")

    with pytest.raises(SystemExit, match="did not serve /assets/app.js"):
        module._smoke_dashboard_assets(stub, tmp_path)

    _assert_process_gone(pid_file)


def test_pyinstaller_spec_bundles_config_and_build_info() -> None:
    spec_text = PACKAGING_SPEC.read_text(encoding="utf-8")

    assert '"default_config.yaml"' in spec_text
    assert '"simple_english" / "slop.tsv"' in spec_text
    assert '"simple_english" / "LICENSE"' in spec_text
    assert 'project_root / "NOTICE.md"' in spec_text
    assert 'build_info_path = package_root / "build-info.json"' in spec_text
    assert 'collect_submodules("software_agent_factory")' in spec_text
    assert "__main__.py" in spec_text

    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'factory = "software_agent_factory.__main__:main"' in pyproject_text


def test_pi_command_filter_is_packaged() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["software_agent_factory"]
    spec_text = PACKAGING_SPEC.read_text(encoding="utf-8")

    assert "pi_extensions/command_filter.mjs" in package_data
    assert '"pi_extensions" / "command_filter.mjs"' in spec_text
    assert '"software_agent_factory/pi_extensions"' in spec_text


def test_dashboard_static_assets_are_packaged() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["software_agent_factory"]
    spec_text = PACKAGING_SPEC.read_text(encoding="utf-8")

    for name in ("app.js", "style.css"):
        assert f"dashboard/static/{name}" in package_data
        assert f'"dashboard" / "static" / "{name}"' in spec_text
        assert (ROOT / "src/software_agent_factory/dashboard/static" / name).is_file()
    assert '"software_agent_factory/dashboard/static"' in spec_text


def _jobs_running_the_full_test_suite() -> dict[tuple[str, str], list[dict[str, str]]]:
    """Map (workflow, job) to its steps for every job with a step that runs all of pytest."""
    jobs: dict[tuple[str, str], list[dict[str, str]]] = {}
    for name in ("ci.yml", "release.yml"):
        _, workflow = _load_workflow(name)
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            if any(
                "pytest -q" in step.get("run", "") and "tests/" not in step["run"] for step in steps
            ):
                jobs[(name, job_name)] = steps
    return jobs


def test_jobs_that_run_the_full_test_suite_set_up_node_for_the_pi_command_filter_tests() -> None:
    jobs = _jobs_running_the_full_test_suite()

    assert set(jobs) == {("ci.yml", "tests"), ("release.yml", "validate-tag")}
    for (workflow_name, job_name), steps in jobs.items():
        assert any(step.get("uses", "").startswith("actions/setup-node@") for step in steps), (
            f"{workflow_name} job {job_name} runs the full test suite without setting up Node.js"
        )
