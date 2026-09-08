import subprocess
from pathlib import Path

import pytest
from factory_testing import build_config, git

from software_agent_factory.config import FactoryConfig
from software_agent_factory.delivery import fetch_delivery_target
from software_agent_factory.github import GitTimeoutError, MergeNotAllowedError
from software_agent_factory.workspace import GitWorktreeWorkspace, WorkspaceError


def _config(tmp_path: Path) -> FactoryConfig:
    payload = build_config(
        tmp_path / "data",
        verify=["true"],
        pull_request={"enabled": True, "draft": False, "base_branch": "main"},
        ci={"enabled": True},
    ).model_dump(mode="json")
    payload["merge"] = {
        "enabled": True,
        "allowed_repositories": ["acme/repo", "acme/other"],
        "required_checks": ["quality"],
    }
    return FactoryConfig.model_validate(payload)


class LocalFetchRunner:
    """Map a validated GitHub transport to a throwaway local bare repository."""

    def __init__(self, remote: Path, url: str = "https://github.com/acme/repo.git") -> None:
        self.remote = remote
        self.url = url
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, env=None):
        args = list(args)
        self.calls.append(args)
        if args[3:5] == ["remote", "get-url"]:
            return subprocess.CompletedProcess(args, 0, self.url + "\n", "")
        if "fetch" in args:
            assert args[args.index("--") + 1] == self.url
            args[args.index("--") + 1] = str(self.remote)
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)


@pytest.fixture
def bare_remote(tmp_path: Path, factory_source_repo: Path) -> Path:
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(factory_source_repo), str(remote))
    return remote


def test_fetch_pins_authorized_url_and_does_not_move_source(
    tmp_path: Path, factory_source_repo: Path, bare_remote: Path
) -> None:
    original = git(factory_source_repo, "rev-parse", "HEAD").strip()
    (factory_source_repo / "local-only.txt").write_text("never reviewed")
    git(factory_source_repo, "add", "-A")
    git(factory_source_repo, "commit", "-m", "unpushed work")
    source_head = git(factory_source_repo, "rev-parse", "HEAD").strip()
    runner = LocalFetchRunner(bare_remote)
    target = fetch_delivery_target(
        _config(tmp_path), factory_source_repo, "acme/repo", runner=runner
    )
    assert target.commit_sha == original
    assert target.repository == "acme/repo"
    assert target.host == "github.com"
    assert git(factory_source_repo, "rev-parse", "HEAD").strip() == source_head
    assert git(factory_source_repo, "for-each-ref", "refs/software-agent-factory/fetch") == ""
    assert "--no-write-fetch-head" in next(call for call in runner.calls if "fetch" in call)


@pytest.mark.parametrize(
    ("url", "expected", "host"),
    [
        ("https://github.com/acme/other.git", "acme/repo", None),
        ("https://elsewhere.invalid/acme/repo.git", "acme/repo", None),
        ("https://github.com/acme/repo.git", "acme/repo", "enterprise.invalid"),
        ("not-a-repository", "acme/repo", None),
    ],
)
def test_fetch_rejects_identity_drift_before_network(
    tmp_path: Path,
    factory_source_repo: Path,
    bare_remote: Path,
    url: str,
    expected: str,
    host: str | None,
) -> None:
    runner = LocalFetchRunner(bare_remote, url)
    with pytest.raises(MergeNotAllowedError):
        fetch_delivery_target(
            _config(tmp_path), factory_source_repo, expected, expected_host=host, runner=runner
        )
    assert not any("fetch" in call for call in runner.calls)


def test_fetch_timeout_is_a_typed_error_without_captured_output(
    tmp_path: Path, factory_source_repo: Path, bare_remote: Path
) -> None:
    class TimeoutRunner(LocalFetchRunner):
        def __call__(self, args, cwd=None, env=None):
            if "fetch" in args:
                raise subprocess.TimeoutExpired(args, 1, output="sensitive captured output")
            return super().__call__(args, cwd, env)

    with pytest.raises(GitTimeoutError) as error:
        fetch_delivery_target(
            _config(tmp_path),
            factory_source_repo,
            "acme/repo",
            runner=TimeoutRunner(bare_remote),
        )
    assert "sensitive captured output" not in str(error.value)
    assert git(factory_source_repo, "for-each-ref", "refs/software-agent-factory/fetch") == ""


def test_workspace_explicit_base_excludes_unreviewed_local_commits(
    tmp_path: Path, factory_source_repo: Path
) -> None:
    target = git(factory_source_repo, "rev-parse", "HEAD").strip()
    (factory_source_repo / "unreviewed.txt").write_text("local work")
    git(factory_source_repo, "add", "-A")
    git(factory_source_repo, "commit", "-m", "unpushed work")
    source_head = git(factory_source_repo, "rev-parse", "HEAD").strip()
    with GitWorktreeWorkspace(
        tmp_path / "data", factory_source_repo, "task", base_ref=target
    ) as workspace:
        workspace.prepare()
        assert workspace.base_commit == target
        assert git(workspace.path, "rev-parse", "HEAD").strip() == target
        assert not (workspace.path / "unreviewed.txt").exists()
    assert git(factory_source_repo, "rev-parse", "HEAD").strip() == source_head


def test_existing_ahead_workspace_cannot_be_reused_for_delivery(
    tmp_path: Path, factory_source_repo: Path
) -> None:
    target = git(factory_source_repo, "rev-parse", "HEAD").strip()
    with GitWorktreeWorkspace(tmp_path / "data", factory_source_repo, "task") as workspace:
        workspace.prepare()
        (workspace.path / "unreviewed.txt").write_text("not reviewed")
        git(workspace.path, "add", "-A")
        git(workspace.path, "commit", "-m", "unpushed work")
        with pytest.raises(WorkspaceError, match="fetched target"):
            workspace.prepare(base_ref=target)


def test_evidence_diff_and_tree_are_one_immutable_snapshot(
    tmp_path: Path, factory_source_repo: Path
) -> None:
    with GitWorktreeWorkspace(tmp_path / "data", factory_source_repo, "task") as workspace:
        workspace.prepare()
        path = workspace.path / "README.md"
        path.write_text("reviewed text\n")
        evidence = workspace.collect_evidence()
        path.write_text("later unreviewed text\n")
        git(workspace.path, "add", "-A")
        assert evidence.tree_sha != git(workspace.path, "write-tree").strip()
        assert git(workspace.path, "show", f"{evidence.tree_sha}:README.md") == "reviewed text\n"
        assert evidence.diff == git(
            workspace.path, "diff", workspace.base_commit, evidence.tree_sha
        )
