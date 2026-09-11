"""Tests for software_agent_factory.github.

Most ``git``/``gh`` invocations are faked via a scripted ``CommandRunner``;
no real subprocess, filesystem Git repo, or network call is made in those
cases. One test (``test_commit_and_push_leaves_source_git_config_untouched``)
uses a real local Git repository and the real ``default_command_runner`` to
assert push safety end-to-end against actual ``git`` behavior -- it still
never touches the network (the "remote" is a local bare repo under
``tmp_path``), mirroring the real-``git`` style already used by
``tests/test_workspace.py``.

Coverage:

- ``GitPublisher``: staged/working change detection, commit trailer,
  no-changes error, push safety (no ``--force``, no merge, correct branch
  refspec, branch prefix/base-branch guards), protected-file and
  excessive-scope rejection, and the ``git config``/``git remote`` guard.
- ``GitHubClient.create_pr``: argument construction and PR body plumbing.
- ``build_pr_body``: pure PR description assembly from typed artifacts.
- ``GitHubClient.get_pr_checks`` / ``poll_checks``: bucket normalization,
  aggregation, bounded polling transitions, timeout, and log/description
  redaction and bounding.
- ``classify_failure``: deterministic failure category heuristics.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

from software_agent_factory.github import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    PULL_REQUEST_VIEW_FIELDS,
    CheckResult,
    CheckStatus,
    CIPollTimeoutError,
    CIStatus,
    ExcessiveChangeScopeError,
    FailureCategory,
    GitCommandError,
    GitHubClient,
    GitHubCommandError,
    GitHubError,
    GitHubTimeoutError,
    GitPublisher,
    GitPublishError,
    GitTimeoutError,
    NoChangesToCommitError,
    ProtectedFileError,
    UnexpectedRepositoryError,
    UnknownMergeOutcomeError,
    UnreviewedContentError,
    UnsafeBranchNameError,
    UnsafeRemoteError,
    build_pr_body,
    classify_failure,
    default_command_runner,
    is_safe_ref_name,
    normalize_status_check_rollup,
    parse_pull_request_url,
    parse_remote_repository,
    parse_remote_repository_for_api,
)
from software_agent_factory.models import (
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ReviewAcceptance,
    ReviewAcceptanceReason,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingOrigin,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
    Specification,
    VerificationReport,
    WorkItem,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the one real-git test never depends on global Git config,
    signing, or hooks (mirrors tests/test_workspace.py)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


class FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess[str]."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Scripted CommandRunner: pops canned responses in call order and
    records every invocation (args, cwd, env) for assertions."""

    def __init__(self, responses: list[FakeCompletedProcess] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.default = FakeCompletedProcess(returncode=0, stdout="")

    def __call__(self, args, cwd=None, env=None):  # noqa: ANN001 - test double
        self.calls.append((list(args), cwd, dict(env) if env else None))
        if self.responses:
            return self.responses.pop(0)
        return self.default


def _remote_url_response(url: str = "https://github.com/acme/repo.git") -> FakeCompletedProcess:
    """Canned response for the ``git remote get-url <remote>`` call that
    ``GitPublisher.commit_and_push`` issues before staging anything."""
    return FakeCompletedProcess(returncode=0, stdout=f"{url}\n")


# --------------------------------------------------------------------------
# GitPublisher
# --------------------------------------------------------------------------


def test_commit_and_push_raises_when_nothing_staged(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),  # remote get-url
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(returncode=0, stdout=""),  # diff --cached --name-only
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(NoChangesToCommitError):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    # Nothing should be committed or pushed once no changes were detected.
    commands = [call[0] for call in runner.calls]
    assert not any("commit" in cmd for cmd in commands)
    assert not any("push" in cmd for cmd in commands)


def test_has_changes_reports_false_when_nothing_staged(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(returncode=0, stdout=""),  # diff --cached --name-only
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.has_changes(tmp_path) is False


def test_has_changes_reports_true_when_files_staged(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.has_changes(tmp_path) is True


def test_commit_and_push_appends_copilot_trailer_and_returns_sha(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),  # remote get-url
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),  # diff --cached --name-only
            FakeCompletedProcess(returncode=0),  # commit
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),  # rev-parse HEAD
            FakeCompletedProcess(returncode=0),  # push
        ]
    )
    publisher = GitPublisher(runner=runner, remote="upstream")

    sha = publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    assert sha == "abc123"
    commit_call = next(call for call in runner.calls if "commit" in call[0])
    message = commit_call[0][commit_call[0].index("-m") + 1]
    assert message.startswith("Implement feature")
    assert "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>" in message


def test_commit_and_push_does_not_duplicate_trailer_if_already_present(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),
            FakeCompletedProcess(returncode=0),
        ]
    )
    publisher = GitPublisher(runner=runner)
    message_with_trailer = (
        "Implement feature\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
    )

    publisher.commit_and_push(tmp_path, "factory/wi-1", message_with_trailer)

    commit_call = next(call for call in runner.calls if "commit" in call[0])
    message = commit_call[0][commit_call[0].index("-m") + 1]
    assert message.count("Co-authored-by: Copilot") == 1


def test_commit_and_push_never_forces_and_pushes_explicit_branch_refspec(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),
            FakeCompletedProcess(returncode=0),
        ]
    )
    publisher = GitPublisher(runner=runner, remote="origin")

    publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    push_call = next(call for call in runner.calls if "push" in call[0])
    assert "--force" not in push_call[0]
    assert "-f" not in push_call[0]
    assert push_call[0][-3:] == ["--", "origin", "abc123:refs/heads/factory/wi-1"]
    # Never merges anything, and never mutates remotes beyond the one
    # permitted read-only "remote get-url" lookup.
    assert not any("merge" in call[0] for call in runner.calls)
    remote_calls = [call[0] for call in runner.calls if "remote" in call[0]]
    assert all(call[call.index("remote") + 1] == "get-url" for call in remote_calls)


def test_commit_and_push_raises_typed_error_on_git_failure(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=1, stderr="fatal: could not commit"),
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(GitCommandError, match="could not commit"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")


def test_commit_and_push_rejects_empty_message(tmp_path: Path) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner)

    with pytest.raises(ValueError, match="empty"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "   ")

    # Empty message is rejected before any git subprocess is invoked.
    assert runner.calls == []


def test_commit_and_push_rejects_branch_not_starting_with_prefix(tmp_path: Path) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner, branch_prefix="factory/")

    with pytest.raises(UnsafeBranchNameError, match="prefix"):
        publisher.commit_and_push(tmp_path, "not-a-factory-branch", "Implement feature")

    # Rejected before any git subprocess is invoked -- nothing was staged,
    # committed, or pushed.
    assert runner.calls == []


def test_commit_and_push_rejects_branch_equal_to_base_branch(tmp_path: Path) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner, branch_prefix="factory/", base_branch="factory/main")

    with pytest.raises(UnsafeBranchNameError, match="base branch"):
        publisher.commit_and_push(tmp_path, "factory/main", "Implement feature")

    assert runner.calls == []


def test_commit_and_push_rejects_branch_names_that_look_like_flags(tmp_path: Path) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner, branch_prefix="")

    with pytest.raises(UnsafeBranchNameError):
        publisher.commit_and_push(tmp_path, "--force", "Implement feature")

    assert runner.calls == []


@pytest.mark.parametrize(
    "protected_file",
    [
        ".env",
        ".env.production",
        "config/.env.local",
        "secrets/id_rsa",
        "id_ed25519",
        "keys/server.pem",
        "certs/client.p12",
        "credentials.json",
        "app/credentials-prod.yaml",
    ],
)
def test_commit_and_push_rejects_protected_files(tmp_path: Path, protected_file: str) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),  # remote get-url
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(  # diff --cached --name-only
                returncode=0, stdout=f"src/app.py\n{protected_file}\n"
            ),
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(ProtectedFileError, match="protected file"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    commands = [call[0] for call in runner.calls]
    assert not any("commit" in cmd for cmd in commands)
    assert not any("push" in cmd for cmd in commands)


def test_commit_and_push_rejects_excessive_changed_files(tmp_path: Path) -> None:
    many_files = "\n".join(f"file{i}.py" for i in range(250)) + "\n"
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout=many_files),
        ]
    )
    publisher = GitPublisher(runner=runner, max_changed_files=200)

    with pytest.raises(ExcessiveChangeScopeError, match="250"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    commands = [call[0] for call in runner.calls]
    assert not any("commit" in cmd for cmd in commands)
    assert not any("push" in cmd for cmd in commands)


def test_commit_and_push_allows_changed_files_within_the_bound(tmp_path: Path) -> None:
    files = "\n".join(f"file{i}.py" for i in range(150)) + "\n"
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout=files),
            FakeCompletedProcess(returncode=0),  # commit
            FakeCompletedProcess(returncode=0, stdout="deadbeef\n"),  # rev-parse HEAD
            FakeCompletedProcess(returncode=0),  # push
        ]
    )
    publisher = GitPublisher(runner=runner, max_changed_files=200)

    sha = publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    assert sha == "deadbeef"


def test_run_git_refuses_to_mutate_remotes_or_config(tmp_path: Path) -> None:
    publisher = GitPublisher(runner=FakeRunner())

    with pytest.raises(GitPublishError, match="remote"):
        publisher._run_git(tmp_path, ["remote", "set-url", "origin", "https://evil.example"])

    with pytest.raises(GitPublishError, match="remote"):
        publisher._run_git(tmp_path, ["remote", "add", "origin", "https://evil.example"])

    with pytest.raises(GitPublishError, match="remote"):
        publisher._run_git(tmp_path, ["remote", "remove", "origin"])

    with pytest.raises(GitPublishError, match="config"):
        publisher._run_git(tmp_path, ["config", "user.email", "evil@example.com"])

    # The one permitted remote invocation must still work.
    publisher._run_git(tmp_path, ["remote", "get-url", "origin"])


# --------------------------------------------------------------------------
# GitPublisher: allowed_hosts enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/repo.git",
        "https://x-access-token@github.com/acme/repo.git",
        "https://github.com/acme/repo",
        "git@github.com:acme/repo.git",
        "ssh://git@github.com/acme/repo.git",
        "ssh://git@github.com:22/acme/repo.git",
    ],
)
def test_commit_and_push_accepts_github_com_https_and_ssh_remotes(tmp_path: Path, url: str) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(url),
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),  # diff --cached
            FakeCompletedProcess(returncode=0),  # commit
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),  # rev-parse HEAD
            FakeCompletedProcess(returncode=0),  # push
        ]
    )
    publisher = GitPublisher(runner=runner)

    sha = publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    assert sha == "abc123"


def test_commit_and_push_rejects_remote_host_outside_allowlist(tmp_path: Path) -> None:
    runner = FakeRunner([_remote_url_response("https://evil.example.com/acme/repo.git")])
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnsafeRemoteError, match="evil.example.com"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    # Rejected right after the read-only remote lookup: no staging, commit,
    # or push ever happened.
    commands = [call[0] for call in runner.calls]
    assert not any("add" in cmd for cmd in commands)
    assert not any("commit" in cmd for cmd in commands)
    assert not any("push" in cmd for cmd in commands)


def test_commit_and_push_allows_custom_allowed_hosts(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response("https://git.internal.example/acme/repo.git"),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),
            FakeCompletedProcess(returncode=0),
        ]
    )
    publisher = GitPublisher(runner=runner, allowed_hosts=frozenset({"git.internal.example"}))

    sha = publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    assert sha == "abc123"


def test_commit_and_push_raises_clear_error_when_remote_url_is_empty(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0, stdout="")])
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnsafeRemoteError, match="no URL configured"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")


def test_commit_and_push_raises_clear_error_when_remote_url_is_malformed(tmp_path: Path) -> None:
    runner = FakeRunner([_remote_url_response("not-a-valid-remote-url")])
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnsafeRemoteError, match="could not determine host"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")


def test_commit_and_push_raises_clear_error_when_remote_is_missing(tmp_path: Path) -> None:
    runner = FakeRunner(
        [FakeCompletedProcess(returncode=2, stderr="fatal: No such remote 'origin'")]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(GitCommandError, match="No such remote"):
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")


def test_commit_and_push_leaves_source_git_config_untouched(tmp_path: Path) -> None:
    """End-to-end with the real ``git`` binary (no network): pushing to a
    local bare remote over a ``file://localhost`` URL (so the allowed-hosts
    check can be exercised without any real network access) must not
    mutate the source repo's or the remote's ``.git/config`` in any way."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")

    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    remote_url = f"file://localhost{remote}"
    _git(repo, "remote", "add", "origin", remote_url)
    _git(repo, "push", "origin", "main")

    _git(repo, "checkout", "-b", "factory/wi-1")
    (repo / "app.py").write_text("print('hi')\n")

    source_config_path = repo / ".git" / "config"
    remote_config_path = remote / "config"
    source_config_before = source_config_path.read_bytes()
    remote_config_before = remote_config_path.read_bytes()

    publisher = GitPublisher(
        runner=default_command_runner,
        remote="origin",
        allowed_hosts=frozenset({"localhost"}),
    )
    sha = publisher.commit_and_push(repo, "factory/wi-1", "Implement feature")

    assert len(sha) == 40
    assert source_config_path.read_bytes() == source_config_before
    assert remote_config_path.read_bytes() == remote_config_before
    # The branch actually landed on the remote, without a force push.
    # Bare repos need --git-dir (not -C) under strict safe.bareRepository.
    pushed_sha = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "factory/wi-1"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert pushed_sha == sha


# --------------------------------------------------------------------------
# GitHubClient.create_pr
# --------------------------------------------------------------------------


def test_create_issue_builds_expected_args_and_returns_url(tmp_path: Path) -> None:
    runner = FakeRunner(
        [FakeCompletedProcess(returncode=0, stdout="https://github.com/acme/repo/issues/7\n")]
    )
    client = GitHubClient(runner=runner)

    url = client.create_issue(
        tmp_path,
        repository="acme/repo",
        title="Implement validation",
        body="body text",
        labels=("project",),
    )

    assert url == "https://github.com/acme/repo/issues/7"
    args, cwd, _env = runner.calls[0]
    assert args == [
        "gh",
        "issue",
        "create",
        "--repo",
        "acme/repo",
        "--title",
        "Implement validation",
        "--body",
        "body text",
        "--label",
        "project",
    ]
    assert cwd == tmp_path


def test_close_issue_marks_it_completed(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0)])
    client = GitHubClient(runner=runner)

    client.close_issue(
        tmp_path,
        repository="acme/repo",
        issue="https://github.com/acme/repo/issues/7",
    )

    assert runner.calls[0][0] == [
        "gh",
        "issue",
        "close",
        "https://github.com/acme/repo/issues/7",
        "--repo",
        "acme/repo",
        "--reason",
        "completed",
    ]


def test_create_pr_builds_expected_args_and_returns_url(tmp_path: Path) -> None:
    runner = FakeRunner(
        [FakeCompletedProcess(returncode=0, stdout="https://github.com/acme/repo/pull/42\n")]
    )
    client = GitHubClient(runner=runner)

    url = client.create_pr(
        tmp_path,
        base="main",
        head="factory/wi-1",
        title="Implement feature",
        body="body text",
    )

    assert url == "https://github.com/acme/repo/pull/42"
    args, cwd, _env = runner.calls[0]
    assert args[0] == "gh"
    assert args[1:] == [
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        "factory/wi-1",
        "--title",
        "Implement feature",
        "--body",
        "body text",
    ]
    assert cwd == tmp_path


def test_create_pr_rejects_empty_title(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner())

    with pytest.raises(ValueError, match="title"):
        client.create_pr(tmp_path, base="main", head="factory/wi-1", title="  ", body="x")


def test_create_pr_raises_typed_error_when_gh_fails(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=1, stderr="not authenticated")])
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubCommandError, match="not authenticated"):
        client.create_pr(tmp_path, base="main", head="factory/wi-1", title="Title", body="body")


def test_create_pr_raises_when_output_is_not_a_url(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0, stdout="no url here\n")])
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubCommandError, match="could not parse"):
        client.create_pr(tmp_path, base="main", head="factory/wi-1", title="Title", body="body")


def test_github_client_token_is_passed_via_env_not_args_or_repr(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0, stdout="https://x/pull/1\n")])
    client = GitHubClient(runner=runner, token="ghp_supersecrettoken1234")  # noqa: S106

    client.create_pr(tmp_path, base="main", head="h", title="t", body="b")

    args, _cwd, env = runner.calls[0]
    assert "ghp_supersecrettoken1234" not in args
    assert env == {"GH_TOKEN": "ghp_supersecrettoken1234"}
    assert "ghp_supersecrettoken1234" not in repr(client)


# --------------------------------------------------------------------------
# build_pr_body
# --------------------------------------------------------------------------


def _work_item() -> WorkItem:
    return WorkItem(
        id="WI-1",
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
        acceptance_criteria=["Empty names are rejected with 400"],
    )


def test_build_pr_body_includes_all_supplied_sections() -> None:
    work_item = _work_item()
    specification = Specification(
        problem="Names should not be empty.",
        acceptance_criteria=["Reject empty names"],
        confidence=0.9,
    )
    plan = ExecutionPlan(
        summary="Add validation to the customer creation endpoint.",
        steps=[PlanStep(id="s1", goal="Add input validation")],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
    )
    verification = VerificationReport(passed=True, confidence=1.0, test_findings=["all green"])
    review = ReviewReport(approved=True, findings=["looks good"])

    body = build_pr_body(
        work_item=work_item,
        specification=specification,
        plan=plan,
        changed_files=["src/app.py", "tests/test_app.py"],
        verification=verification,
        review=review,
        run_id="RUN-1",
    )

    assert "Reject empty customer names" in body
    assert "Return HTTP 400" in body
    assert "Names should not be empty." in body
    assert "Add validation to the customer creation endpoint." in body
    assert "`src/app.py`" in body
    assert "`tests/test_app.py`" in body
    assert "Passed: True" in body
    assert "all green" in body
    assert "Approved: True" in body
    assert "looks good" in body
    assert "RUN-1" in body


def test_build_pr_body_handles_missing_optional_artifacts() -> None:
    body = build_pr_body(
        work_item=_work_item(),
        specification=None,
        plan=None,
        changed_files=[],
        verification=None,
        review=None,
        run_id="RUN-2",
    )

    assert "no changed files recorded" in body
    assert "RUN-2" in body
    # No stray section headers for artifacts that were not supplied.
    assert "### Specification" not in body
    assert "### Plan" not in body
    assert "### Deterministic verification" not in body
    assert "### Reviewer result" not in body


def test_build_pr_body_discloses_controller_accepted_findings() -> None:
    acceptance = ReviewAcceptance(
        snapshot=3,
        reason=ReviewAcceptanceReason.REVIEW_ROUND_LIMIT,
        risk=Risk.R1,
        review_rounds=3,
        reviewed_tree_sha="a" * 40,
        findings=[
            ReviewFinding(
                id="review-correctness-1234",
                category=ReviewFindingCategory.CORRECTNESS,
                message="A bounded edge case remains.",
                locations=[ReviewSourceLocation(path="src/app.py", start_line=1, end_line=1)],
                origin=ReviewFindingOrigin.INITIAL,
                first_seen_snapshot=1,
            )
        ],
    )
    body = build_pr_body(
        work_item=_work_item(),
        specification=None,
        plan=None,
        changed_files=["src/app.py"],
        verification=VerificationReport(passed=True, confidence=1.0),
        review=ReviewReport(approved=False),
        review_acceptance=acceptance,
        run_id="RUN-accepted",
    )

    assert "### Accepted with findings" in body
    assert "this is not Reviewer approval" in body
    assert "review-correctness-1234" in body
    assert "`" + ("a" * 40) + "`" in body


# --------------------------------------------------------------------------
# CI status: bucket normalization + aggregation
# --------------------------------------------------------------------------


def _checks_response(*items: dict[str, str]) -> FakeCompletedProcess:
    import json

    return FakeCompletedProcess(returncode=0, stdout=json.dumps(list(items)))


def test_get_pr_checks_all_pass(tmp_path: Path) -> None:
    runner = FakeRunner(
        [_checks_response({"name": "build", "bucket": "pass", "link": "", "description": ""})]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42")

    assert status.overall == CheckStatus.PASS
    assert status.checks[0].status == CheckStatus.PASS
    assert status.checks[0].failure_category is None


def test_get_pr_checks_pending_takes_priority_over_pass() -> None:
    from software_agent_factory.github import _aggregate_status

    checks = [
        CheckResult(name="build", status=CheckStatus.PASS),
        CheckResult(name="test", status=CheckStatus.PENDING),
    ]
    assert _aggregate_status(checks) == CheckStatus.PENDING


def test_get_pr_checks_fail_takes_priority_over_pending() -> None:
    from software_agent_factory.github import _aggregate_status

    checks = [
        CheckResult(name="build", status=CheckStatus.FAIL),
        CheckResult(name="test", status=CheckStatus.PENDING),
    ]
    assert _aggregate_status(checks) == CheckStatus.FAIL


def test_get_pr_checks_classifies_failures_and_fetches_bounded_log(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _checks_response(
                {
                    "name": "pytest-suite",
                    "bucket": "fail",
                    "link": "https://github.com/acme/repo/actions/runs/999/job/1",
                    "description": "Process completed with exit code 1.",
                }
            ),
            FakeCompletedProcess(
                returncode=0,
                stdout="setting up environment\npytest-suite: AssertionError: test failed\n",
            ),
        ]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42")

    assert status.overall == CheckStatus.FAIL
    failed = status.checks[0]
    assert failed.failure_category == FailureCategory.TEST_FAILURE
    assert "AssertionError" in failed.log_excerpt
    assert len(failed.log_excerpt) <= 4000

    log_call = runner.calls[1]
    assert log_call[0][:4] == ["gh", "run", "view", "999"]
    assert "--log-failed" in log_call[0]


def test_get_pr_checks_skips_log_fetch_when_disabled(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _checks_response(
                {
                    "name": "build",
                    "bucket": "fail",
                    "link": "https://github.com/acme/repo/actions/runs/1/job/1",
                    "description": "",
                }
            )
        ]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42", fetch_logs=False)

    assert status.checks[0].log_excerpt == ""
    assert len(runner.calls) == 1


def test_get_pr_checks_raises_when_no_output_and_gh_failed(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=1, stdout="", stderr="no pull requests")])
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubCommandError, match="no pull requests"):
        client.get_pr_checks(tmp_path, "42")


def test_get_pr_checks_treats_unregistered_checks_as_pending(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stdout="",
                stderr="no checks reported on the 'factory/task-1' branch",
            )
        ]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42")

    assert status == CIStatus(overall=CheckStatus.PENDING)


def test_get_pr_checks_tolerates_nonzero_exit_when_json_present(tmp_path: Path) -> None:
    # gh pr checks exits non-zero while checks are pending; JSON body is
    # still authoritative and must be parsed, not treated as an error.
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=8,
                stdout='[{"name": "build", "bucket": "pending", "link": "", "description": ""}]',
            )
        ]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42")

    assert status.overall == CheckStatus.PENDING


def test_poll_checks_waits_for_checks_to_register(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stdout="",
                stderr="no checks reported on the 'factory/task-1' branch",
            ),
            _checks_response({"name": "build", "bucket": "pending", "link": "", "description": ""}),
            _checks_response({"name": "build", "bucket": "pass", "link": "", "description": ""}),
        ]
    )
    client = GitHubClient(runner=runner)
    sleeps: list[float] = []

    status = client.poll_checks(
        tmp_path,
        "42",
        interval_seconds=5,
        max_polls=3,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )

    assert status.overall == CheckStatus.PASS
    assert sleeps == [5, 5]


def test_get_pr_checks_redacts_token_like_text_in_description(tmp_path: Path) -> None:
    token = "ghp_1234567890abcdef"
    runner = FakeRunner(
        [
            _checks_response(
                {
                    "name": "build",
                    "bucket": "pass",
                    "link": "",
                    "description": f"completed using {token}",
                }
            )
        ]
    )
    client = GitHubClient(runner=runner)

    status = client.get_pr_checks(tmp_path, "42")

    assert token not in status.checks[0].description
    assert "[REDACTED]" in status.checks[0].description


def test_fetch_check_log_redacts_token_like_strings(tmp_path: Path) -> None:
    token = "ghp_1234567890abcdef"
    runner = FakeRunner(
        [FakeCompletedProcess(returncode=0, stdout=f"error: unauthorized using token {token}\n")]
    )
    client = GitHubClient(runner=runner)
    check = CheckResult(
        name="deploy",
        status=CheckStatus.FAIL,
        details_url="https://github.com/acme/repo/actions/runs/42/job/1",
    )

    excerpt = client.fetch_check_log(tmp_path, check)

    assert token not in excerpt
    assert "[REDACTED]" in excerpt


def test_fetch_check_log_is_bounded_by_max_chars(tmp_path: Path) -> None:
    long_log = "line without a name match\n" * 500
    runner = FakeRunner([FakeCompletedProcess(returncode=0, stdout=long_log)])
    client = GitHubClient(runner=runner)
    check = CheckResult(
        name="deploy",
        status=CheckStatus.FAIL,
        details_url="https://github.com/acme/repo/actions/runs/7/job/1",
    )

    excerpt = client.fetch_check_log(tmp_path, check, max_chars=50)

    assert len(excerpt) <= 50


# --------------------------------------------------------------------------
# Bounded polling
# --------------------------------------------------------------------------


def test_poll_checks_returns_as_soon_as_no_longer_pending(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _checks_response({"name": "build", "bucket": "pending", "link": "", "description": ""}),
            _checks_response({"name": "build", "bucket": "pending", "link": "", "description": ""}),
            _checks_response({"name": "build", "bucket": "pass", "link": "", "description": ""}),
        ]
    )
    client = GitHubClient(runner=runner)
    sleeps: list[float] = []

    status = client.poll_checks(
        tmp_path,
        "42",
        interval_seconds=5,
        max_polls=10,
        sleep=sleeps.append,
        clock=iter([0.0, 1.0, 2.0]).__next__,
    )

    assert status.overall == CheckStatus.PASS
    assert sleeps == [5, 5]  # slept between poll 1->2 and poll 2->3, not after success


def test_poll_checks_raises_timeout_when_still_pending_after_max_polls(tmp_path: Path) -> None:
    pending = _checks_response(
        {"name": "build", "bucket": "pending", "link": "", "description": ""}
    )
    runner = FakeRunner([pending, pending, pending])
    client = GitHubClient(runner=runner)

    with pytest.raises(CIPollTimeoutError) as exc_info:
        client.poll_checks(
            tmp_path,
            "42",
            interval_seconds=1,
            max_polls=3,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
        )

    assert exc_info.value.last_status is not None
    assert exc_info.value.last_status.overall == CheckStatus.PENDING
    assert len(runner.calls) == 3


def test_poll_checks_stops_early_when_max_seconds_elapsed(tmp_path: Path) -> None:
    pending = _checks_response(
        {"name": "build", "bucket": "pending", "link": "", "description": ""}
    )
    runner = FakeRunner([pending, pending, pending, pending, pending])
    client = GitHubClient(runner=runner)
    clock_values = iter([0.0, 100.0, 200.0])

    with pytest.raises(CIPollTimeoutError):
        client.poll_checks(
            tmp_path,
            "42",
            interval_seconds=1,
            max_polls=50,
            max_seconds=50,
            sleep=lambda _seconds: None,
            clock=lambda: next(clock_values),
        )

    # Stopped well before the max_polls budget because max_seconds elapsed.
    assert len(runner.calls) < 50


def test_poll_checks_rejects_invalid_bounds(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner())

    with pytest.raises(ValueError, match="interval_seconds"):
        client.poll_checks(tmp_path, "42", interval_seconds=0)

    with pytest.raises(ValueError, match="max_polls"):
        client.poll_checks(tmp_path, "42", max_polls=0)


# --------------------------------------------------------------------------
# classify_failure heuristics
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "log", "expected"),
    [
        ("pytest", "AssertionError: expected 400 got 200", FailureCategory.TEST_FAILURE),
        ("unit-tests", "2 tests failed", FailureCategory.TEST_FAILURE),
        ("build", "", FailureCategory.CODE_FAILURE),
        ("lint", "", FailureCategory.CODE_FAILURE),
        (
            "install",
            "npm ERR! could not resolve dependency tree",
            FailureCategory.DEPENDENCY_FAILURE,
        ),
        (
            "build",
            "ModuleNotFoundError: No module named 'requests'",
            FailureCategory.DEPENDENCY_FAILURE,
        ),
        ("integration", "connection reset by peer", FailureCategory.INFRA_FAILURE),
        ("test", "runner has received a shutdown signal", FailureCategory.INFRA_FAILURE),
        ("flaky-test", "known flaky, retry succeeded", FailureCategory.FLAKY_TEST),
        ("mystery-check", "", FailureCategory.UNKNOWN),
    ],
)
def test_classify_failure_heuristics(name: str, log: str, expected: FailureCategory) -> None:
    assert classify_failure(name, log) == expected


def test_classify_failure_prefers_infra_signal_over_test_name() -> None:
    # A test-named check that actually failed due to an infra blip must not
    # be misclassified as an ordinary test failure.
    result = classify_failure("integration-tests", "connection refused by runner")
    assert result == FailureCategory.INFRA_FAILURE


def test_ci_status_is_a_plain_typed_model() -> None:
    status = CIStatus(overall=CheckStatus.PASS, checks=[])
    assert status.overall == CheckStatus.PASS
    assert status.checks == []


# --------------------------------------------------------------------------
# Repository identity parsing (ADR-022 merge boundary)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/repo.git",
        "https://github.com/acme/repo",
        "git@github.com:acme/repo.git",
        "ssh://git@github.com/acme/repo.git",
        "https://github.com:443/acme/repo.git",
    ],
)
def test_parse_remote_repository_extracts_exact_identity(url: str) -> None:
    reference = parse_remote_repository(url)

    assert reference.host == "github.com"
    assert reference.full_name == "acme/repo"


@pytest.mark.parametrize(
    "url",
    ["", "https://github.com/acme", "https://github.com/", "git@github.com:acme", "acme/repo"],
)
def test_parse_remote_repository_rejects_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_remote_repository(url)


def test_parse_remote_repository_is_case_insensitive_on_identity() -> None:
    first = parse_remote_repository("https://GitHub.com/Acme/Repo.git")
    second = parse_remote_repository("git@github.com:acme/repo.git")

    assert first.same_repository(second)


def test_api_remote_parser_discards_https_credentials() -> None:
    reference = parse_remote_repository_for_api(
        "https://oauth2:secret-token@github.com/acme/repo.git"
    )

    assert reference.host == "github.com"
    assert reference.full_name == "acme/repo"


def test_active_host_uses_the_current_gh_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GH_HOST", raising=False)
    payload = {
        "hosts": {
            "github.com": [
                {"active": True, "host": "github.com", "state": "success"},
            ]
        }
    }
    runner = FakeRunner([FakeCompletedProcess(stdout=json.dumps(payload))])

    assert GitHubClient(runner=runner).active_host(tmp_path) == "github.com"
    assert runner.calls[0][0] == [
        "gh",
        "auth",
        "status",
        "--active",
        "--json",
        "hosts",
    ]


def test_active_host_honors_and_pins_gh_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    payload = {
        "hosts": {
            "github.com": [
                {"active": True, "host": "github.com", "state": "success"},
            ],
            "ghe.example.com": [
                {"active": True, "host": "ghe.example.com", "state": "success"},
            ],
        }
    }
    runner = FakeRunner([FakeCompletedProcess(stdout=json.dumps(payload))])
    client = GitHubClient(runner=runner)

    assert client.active_host(tmp_path) == "ghe.example.com"
    client.find_pull_requests(
        tmp_path,
        head="factory/WI-1",
        base="main",
        repository="acme/repo",
    )
    assert runner.calls[-1][2] == {"GH_HOST": "ghe.example.com"}


def test_active_host_defaults_to_github_with_multiple_authenticated_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GH_HOST", raising=False)
    payload = {
        "hosts": {
            "github.com": [
                {"active": True, "host": "github.com", "state": "success"},
            ],
            "ghe.example.com": [
                {"active": True, "host": "ghe.example.com", "state": "success"},
            ],
        }
    }

    assert (
        GitHubClient(
            runner=FakeRunner([FakeCompletedProcess(stdout=json.dumps(payload))])
        ).active_host(tmp_path)
        == "github.com"
    )


def test_parse_pull_request_url_returns_repository_and_number() -> None:
    reference, number = parse_pull_request_url("https://github.com/acme/repo/pull/42")

    assert reference.full_name == "acme/repo"
    assert reference.host == "github.com"
    assert number == 42


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/repo/pull/42",
        "https://github.com/acme/repo/pulls/42",
        "https://github.com/acme/repo/pull/0",
        "https://github.com/acme/repo/pull/abc",
        "https://github.com/acme/repo/pull/42/files",
        "gh://acme/repo/pull/42",
    ],
)
def test_parse_pull_request_url_rejects_anything_else(url: str) -> None:
    with pytest.raises(ValueError):
        parse_pull_request_url(url)


def test_resolve_remote_repository_enforces_the_host_allowlist(tmp_path: Path) -> None:
    publisher = GitPublisher(runner=FakeRunner([_remote_url_response()]))

    assert publisher.resolve_remote_repository(tmp_path).full_name == "acme/repo"

    hostile = GitPublisher(
        runner=FakeRunner([_remote_url_response("https://evil.example/acme/repo.git")])
    )
    with pytest.raises(UnsafeRemoteError):
        hostile.resolve_remote_repository(tmp_path)


def test_resolve_remote_repository_rejects_a_non_repository_remote(tmp_path: Path) -> None:
    publisher = GitPublisher(runner=FakeRunner([_remote_url_response("https://github.com/acme")]))

    with pytest.raises(UnsafeRemoteError):
        publisher.resolve_remote_repository(tmp_path)


# --------------------------------------------------------------------------
# GitPublisher.ensure_pushed (crash recovery, no new commit)
# --------------------------------------------------------------------------


def test_ensure_pushed_is_a_no_op_when_the_remote_already_has_head(tmp_path: Path) -> None:
    sha = "a" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),  # remote get-url
            FakeCompletedProcess(stdout=f"{sha}\n"),  # rev-parse HEAD
            FakeCompletedProcess(stdout=f"{sha}\trefs/heads/factory/wi-1\n"),  # ls-remote
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.ensure_pushed(tmp_path, "factory/wi-1") == sha
    assert not any("push" in call[0] for call in runner.calls)
    assert not any("commit" in call[0] for call in runner.calls)


def test_remote_branch_sha_requires_the_exact_returned_ref(tmp_path: Path) -> None:
    expected = "a" * 40
    actual = "b" * 40
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                stdout=(
                    f"{expected}\trefs/heads/decoy/refs/heads/factory/wi-1\n"
                    f"{actual}\trefs/heads/factory/wi-1\n"
                )
            )
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.remote_branch_sha(tmp_path, "factory/wi-1") == actual


def test_ensure_pushed_pushes_an_existing_commit_without_creating_one(tmp_path: Path) -> None:
    sha = "a" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(stdout=f"{sha}\n"),
            FakeCompletedProcess(stdout=""),  # branch missing on the remote
            FakeCompletedProcess(),  # push
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.ensure_pushed(tmp_path, "factory/wi-1") == sha
    push = [call[0] for call in runner.calls if "push" in call[0]][0]
    assert push[-3:] == ["--", "origin", f"{sha}:refs/heads/factory/wi-1"]
    assert "--force" not in push and "-f" not in push
    assert not any("commit" in call[0] for call in runner.calls)


def test_ensure_pushed_retries_a_transient_commit_refs_failure(tmp_path: Path) -> None:
    sha = "a" * 40
    delays: list[float] = []
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(stdout=f"{sha}\n"),
            FakeCompletedProcess(stdout=""),  # branch initially missing
            FakeCompletedProcess(
                returncode=1,
                stderr=(
                    "remote: fatal error in commit_refs\n"
                    "! [remote rejected] HEAD -> factory/wi-1 (failure)\n"
                ),
            ),
            FakeCompletedProcess(stdout=""),  # first push did not land
            FakeCompletedProcess(stdout=""),  # still missing after the retry delay
            FakeCompletedProcess(),  # one retry succeeds
        ]
    )
    publisher = GitPublisher(runner=runner, sleeper=delays.append)

    assert publisher.ensure_pushed(tmp_path, "factory/wi-1") == sha
    pushes = [call[0] for call in runner.calls if "push" in call[0]]
    assert len(pushes) == 2
    assert delays == [2.0]
    assert all("--force" not in push and "-f" not in push for push in pushes)


def test_ensure_pushed_recovers_when_a_transient_push_response_was_lost(
    tmp_path: Path,
) -> None:
    sha = "a" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(stdout=f"{sha}\n"),
            FakeCompletedProcess(stdout=""),  # branch initially missing
            FakeCompletedProcess(returncode=1, stderr="fatal: unexpected EOF"),
            FakeCompletedProcess(stdout=f"{sha}\trefs/heads/factory/wi-1\n"),
        ]
    )
    publisher = GitPublisher(runner=runner)

    assert publisher.ensure_pushed(tmp_path, "factory/wi-1") == sha
    assert len([call for call in runner.calls if "push" in call[0]]) == 1


def test_ensure_pushed_reconciles_after_the_final_transient_failure(tmp_path: Path) -> None:
    sha = "a" * 40
    delays: list[float] = []
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(stdout=f"{sha}\n"),
            FakeCompletedProcess(stdout=""),  # branch initially missing
            FakeCompletedProcess(returncode=1, stderr="fatal: unexpected EOF"),
            FakeCompletedProcess(stdout=""),  # first push did not land
            FakeCompletedProcess(stdout=""),  # still missing after the retry delay
            FakeCompletedProcess(returncode=1, stderr="fatal: unexpected EOF"),
            FakeCompletedProcess(stdout=""),  # final immediate reconciliation
            FakeCompletedProcess(
                stdout=f"{sha}\trefs/heads/factory/wi-1\n"
            ),  # visible after propagation delay
        ]
    )
    publisher = GitPublisher(runner=runner, sleeper=delays.append)

    assert publisher.ensure_pushed(tmp_path, "factory/wi-1") == sha
    assert len([call for call in runner.calls if "push" in call[0]]) == 2
    assert delays == [2.0, 5.0]


@pytest.mark.parametrize(
    "stderr",
    [
        "! [rejected] HEAD -> factory/wi-1 (non-fast-forward)",
        "remote: error: GH007: protected branch update failed\n"
        "remote: error: pre-receive hook declined",
        "remote: Permission to acme/repo.git denied to user.",
        "fatal: Authentication failed for 'https://github.com/acme/repo.git/'",
        "error: RPC failed; HTTP 403 curl 22 The requested URL returned error: 403\n"
        "fatal: the remote end hung up unexpectedly",
    ],
)
def test_ensure_pushed_does_not_retry_non_transient_rejections(tmp_path: Path, stderr: str) -> None:
    sha = "a" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(stdout=f"{sha}\n"),
            FakeCompletedProcess(stdout=""),  # branch initially missing
            FakeCompletedProcess(returncode=1, stderr=stderr),
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(GitCommandError):
        publisher.ensure_pushed(tmp_path, "factory/wi-1")

    assert len([call for call in runner.calls if "push" in call[0]]) == 1
    assert len([call for call in runner.calls if "ls-remote" in call[0]]) == 1


def test_ensure_pushed_enforces_the_branch_prefix(tmp_path: Path) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnsafeBranchNameError):
        publisher.ensure_pushed(tmp_path, "main")

    assert runner.calls == []


# --------------------------------------------------------------------------
# Status check rollup normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("SUCCESS", CheckStatus.PASS),
        ("FAILURE", CheckStatus.FAIL),
        ("TIMED_OUT", CheckStatus.FAIL),
        ("ACTION_REQUIRED", CheckStatus.FAIL),
        ("STALE", CheckStatus.FAIL),
        ("CANCELLED", CheckStatus.CANCELLED),
        ("SKIPPED", CheckStatus.SKIPPED),
        ("NEUTRAL", CheckStatus.SKIPPED),
        ("SOMETHING_NEW", CheckStatus.PENDING),
    ],
)
def test_rollup_conclusions_normalize_conservatively(
    conclusion: str, expected: CheckStatus
) -> None:
    [check] = normalize_status_check_rollup(
        [
            {
                "__typename": "CheckRun",
                "name": "quality",
                "status": "COMPLETED",
                "conclusion": conclusion,
            }
        ]
    )

    assert check.status is expected


def test_an_incomplete_check_run_is_pending_regardless_of_its_conclusion() -> None:
    [check] = normalize_status_check_rollup(
        [
            {
                "__typename": "CheckRun",
                "name": "quality",
                "status": "IN_PROGRESS",
                "conclusion": "SUCCESS",
            }
        ]
    )

    assert check.status is CheckStatus.PENDING


def test_commit_status_contexts_are_normalized_by_state() -> None:
    checks = normalize_status_check_rollup(
        [
            {"__typename": "StatusContext", "context": "legacy", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "other", "state": "ERROR"},
        ]
    )

    assert [(check.name, check.status) for check in checks] == [
        ("legacy", CheckStatus.PASS),
        ("other", CheckStatus.FAIL),
    ]


# --------------------------------------------------------------------------
# GitHubClient pull request state and merging
# --------------------------------------------------------------------------


def _pr_view_payload() -> str:
    return json.dumps(
        {
            "number": 42,
            "url": "https://github.com/acme/repo/pull/42",
            "state": "OPEN",
            "isDraft": False,
            "isCrossRepository": False,
            "headRefName": "factory/WI-1",
            "headRefOid": "A" * 40,
            "baseRefName": "main",
            "headRepository": {"name": "repo"},
            "headRepositoryOwner": {"login": "acme"},
            "mergeable": "mergeable",
            "mergeStateStatus": "clean",
            "reviewDecision": "approved",
            "mergeCommit": None,
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "quality",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ],
            "body": "Run ID: `run-1`",
        }
    )


def test_get_pull_request_normalizes_the_documented_fields(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(stdout=_pr_view_payload())])
    client = GitHubClient(runner=runner)

    state = client.get_pull_request(tmp_path, "https://github.com/acme/repo/pull/42")

    assert state.number == 42
    assert state.state == "OPEN"
    assert state.head_ref_oid == "a" * 40
    assert state.head_repository == "acme/repo"
    assert state.mergeable == "MERGEABLE"
    assert state.merge_state_status == "CLEAN"
    assert state.review_decision == "APPROVED"
    assert state.merged is False
    assert [check.name for check in state.checks] == ["quality"]

    args = runner.calls[0][0]
    assert args[:4] == ["gh", "pr", "view", "https://github.com/acme/repo/pull/42"]
    requested = args[args.index("--json") + 1].split(",")
    assert set(requested) == set(PULL_REQUEST_VIEW_FIELDS)


def test_get_pull_request_raises_on_unusable_output(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner([FakeCompletedProcess(stdout="not json")]))

    with pytest.raises(GitHubCommandError):
        client.get_pull_request(tmp_path, "https://github.com/acme/repo/pull/42")


def test_merged_pull_request_exposes_its_merge_commit(tmp_path: Path) -> None:
    payload = json.loads(_pr_view_payload())
    payload["state"] = "MERGED"
    payload["mergeCommit"] = {"oid": "B" * 40}
    client = GitHubClient(runner=FakeRunner([FakeCompletedProcess(stdout=json.dumps(payload))]))

    state = client.get_pull_request(tmp_path, "https://github.com/acme/repo/pull/42")

    assert state.merged is True
    assert state.merge_commit_sha == "b" * 40


def test_find_pull_requests_queries_an_exact_head_and_base(tmp_path: Path) -> None:
    payload = json.dumps([json.loads(_pr_view_payload())])
    runner = FakeRunner([FakeCompletedProcess(stdout=payload)])
    client = GitHubClient(runner=runner)

    [found] = client.find_pull_requests(tmp_path, head="factory/WI-1", base="main")

    assert found.url == "https://github.com/acme/repo/pull/42"
    args = runner.calls[0][0]
    assert args[:3] == ["gh", "pr", "list"]
    assert args[args.index("--head") + 1] == "factory/WI-1"
    assert args[args.index("--base") + 1] == "main"
    assert args[args.index("--state") + 1] == "open"


def test_find_pull_requests_returns_nothing_when_gh_prints_nothing(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner([FakeCompletedProcess(stdout="")]))

    assert client.find_pull_requests(tmp_path, head="factory/WI-1", base="main") == []


def test_merge_pull_request_uses_a_synchronous_rest_merge_bound_to_the_head(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=0,
                stdout=json.dumps({"merged": True, "sha": "b" * 40, "message": "merged"}),
            )
        ]
    )
    client = GitHubClient(runner=runner, token="ghp_supersecrettoken1234")  # noqa: S106

    outcome = client.merge_pull_request(
        tmp_path,
        repository="acme/repo",
        number=42,
        method="squash",
        expected_head_sha="a" * 40,
    )

    args, _cwd, env = runner.calls[0]
    assert args == [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PUT",
        "-H",
        "Accept: application/vnd.github+json",
        "repos/acme/repo/pulls/42/merge",
        "-f",
        f"sha={'a' * 40}",
        "-f",
        "merge_method=squash",
    ]
    assert outcome.merged is True
    assert outcome.commit_sha == "b" * 40
    assert env == {"GH_TOKEN": "ghp_supersecrettoken1234"}


def test_merge_pull_request_never_uses_gh_pr_merge_which_could_enqueue(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0, stdout="{}")])
    client = GitHubClient(runner=runner)

    client.merge_pull_request(
        tmp_path, repository="acme/repo", number=1, method="merge", expected_head_sha="a" * 40
    )

    for args, _cwd, _env in runner.calls:
        assert args[1:3] != ["pr", "merge"]
        assert "--auto" not in args
        assert "--admin" not in args


def test_gh_pr_merge_and_auto_merge_are_refused_outright(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner())

    with pytest.raises(GitHubError):
        client._run(["pr", "merge", "42", "--squash"], tmp_path)
    with pytest.raises(GitHubError):
        client._run(["pr", "edit", "42", "--auto"], tmp_path)


def test_merge_pull_request_reports_a_refusal_instead_of_raising(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stdout=json.dumps({"message": "Pull Request is not mergeable"}),
                stderr="gh: HTTP 405",
            )
        ]
    )
    client = GitHubClient(runner=runner)

    outcome = client.merge_pull_request(
        tmp_path, repository="acme/repo", number=42, method="merge", expected_head_sha="a" * 40
    )

    assert outcome.merged is False
    assert outcome.returncode == 1
    assert "not mergeable" in outcome.message
    assert outcome.queue_requested is False


def test_a_merge_queue_response_is_recognized(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stdout=json.dumps(
                    {"message": "Changes must be made through a merge queue"},
                ),
            )
        ]
    )
    client = GitHubClient(runner=runner)

    outcome = client.merge_pull_request(
        tmp_path, repository="acme/repo", number=42, method="squash", expected_head_sha="a" * 40
    )

    assert outcome.merged is False
    assert outcome.queue_requested is True


def test_a_merged_response_without_a_commit_is_an_unknown_outcome(tmp_path: Path) -> None:
    runner = FakeRunner(
        [FakeCompletedProcess(returncode=0, stdout=json.dumps({"merged": True, "sha": ""}))]
    )
    client = GitHubClient(runner=runner)

    with pytest.raises(UnknownMergeOutcomeError):
        client.merge_pull_request(
            tmp_path, repository="acme/repo", number=42, method="squash", expected_head_sha="a" * 40
        )


@pytest.mark.parametrize(
    ("method", "sha", "number", "repository"),
    [
        ("admin", "a" * 40, 42, "acme/repo"),
        ("squash", "abc", 42, "acme/repo"),
        ("squash", "", 42, "acme/repo"),
        ("", "a" * 40, 42, "acme/repo"),
        ("squash", "a" * 40, 0, "acme/repo"),
        ("squash", "a" * 40, 42, "repo"),
    ],
)
def test_merge_pull_request_rejects_unsupported_arguments(
    tmp_path: Path, method: str, sha: str, number: int, repository: str
) -> None:
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    with pytest.raises(ValueError):
        client.merge_pull_request(
            tmp_path,
            repository=repository,
            number=number,
            method=method,
            expected_head_sha=sha,
        )

    assert runner.calls == []


# --------------------------------------------------------------------------
# Server-enforced branch policy
# --------------------------------------------------------------------------


def _ruleset_response(*, contexts: list[str], strict: bool = True, pr_rule: bool = True) -> str:
    rules: list[dict] = [
        {
            "ruleset_id": 1,
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [{"context": name} for name in contexts],
                "strict_required_status_checks_policy": strict,
            },
        }
    ]
    if pr_rule:
        rules.append({"ruleset_id": 1, "type": "pull_request", "parameters": {}})
    return json.dumps(rules)


def test_branch_policy_is_read_from_rulesets(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=0, stdout=_ruleset_response(contexts=["quality"])),
            FakeCompletedProcess(
                returncode=0,
                stdout=json.dumps({"enforcement": "active", "bypass_actors": []}),
            ),
            FakeCompletedProcess(returncode=1, stderr="Not Found"),
        ]
    )
    client = GitHubClient(runner=runner)

    policy = client.get_branch_policy(tmp_path, repository="acme/repo", branch="main")

    assert policy.required_contexts == frozenset({"quality"})
    assert policy.strict is True
    assert policy.requires_pull_request is True
    assert policy.sources == ("ruleset",)
    assert runner.calls[0][0] == [
        "gh",
        "api",
        "--hostname",
        "github.com",
        "-H",
        "Accept: application/vnd.github+json",
        "repos/acme/repo/rules/branches/main",
    ]


def test_branch_policy_falls_back_to_classic_protection(tmp_path: Path) -> None:
    protection = {
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"strict": True, "contexts": ["quality"]},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="Not Found"),
            FakeCompletedProcess(returncode=0, stdout=json.dumps(protection)),
        ]
    )
    client = GitHubClient(runner=runner)

    policy = client.get_branch_policy(tmp_path, repository="acme/repo", branch="main")

    assert policy.required_contexts == frozenset({"quality"})
    assert policy.strict is True
    assert policy.requires_pull_request is True
    assert policy.sources == ("branch-protection",)


def test_an_unreadable_branch_policy_is_an_error_not_an_empty_policy(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="HTTP 403"),
            FakeCompletedProcess(returncode=1, stderr="HTTP 403"),
        ]
    )
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubError):
        client.get_branch_policy(tmp_path, repository="acme/repo", branch="main")


@pytest.mark.parametrize(
    "metadata",
    [
        {"enforcement": "evaluate", "bypass_actors": []},
        {"enforcement": "active", "bypass_actors": [{"actor_id": 1, "bypass_mode": "always"}]},
        {"enforcement": "active"},
    ],
)
def test_ruleset_must_be_active_without_bypass(tmp_path: Path, metadata: dict) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(stdout=_ruleset_response(contexts=["quality"])),
            FakeCompletedProcess(stdout=json.dumps(metadata)),
            FakeCompletedProcess(returncode=1, stderr="HTTP 404"),
        ]
    )
    with pytest.raises(GitHubError):
        GitHubClient(runner=runner).get_branch_policy(
            tmp_path, repository="acme/repo", branch="main"
        )


@pytest.mark.parametrize("enforced", [False, None])
def test_classic_protection_cannot_exempt_administrators(
    tmp_path: Path, enforced: bool | None
) -> None:
    protection = {
        "enforce_admins": {"enabled": enforced},
        "required_status_checks": {"strict": True, "contexts": ["quality"]},
        "required_pull_request_reviews": {
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="HTTP 404"),
            FakeCompletedProcess(stdout=json.dumps(protection)),
        ]
    )
    with pytest.raises(GitHubError):
        GitHubClient(runner=runner).get_branch_policy(
            tmp_path, repository="acme/repo", branch="main"
        )


def test_branch_policy_pins_host_and_encodes_branch(tmp_path: Path) -> None:
    protection = {
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"strict": True, "contexts": ["quality"]},
        "required_pull_request_reviews": {
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []},
        },
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="HTTP 404"),
            FakeCompletedProcess(stdout=json.dumps(protection)),
        ]
    )
    GitHubClient(runner=runner).get_branch_policy(
        tmp_path, repository="acme/repo", branch="release/next", hostname="git.example.com"
    )
    for args, *_ in runner.calls:
        assert args[args.index("--hostname") + 1] == "git.example.com"
        assert "release%2Fnext" in args[-1]


def test_branch_policy_rejects_an_unsafe_branch_or_repository(tmp_path: Path) -> None:
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    with pytest.raises(ValueError):
        client.get_branch_policy(tmp_path, repository="acme/repo", branch="../evil")
    with pytest.raises(ValueError):
        client.get_branch_policy(tmp_path, repository="evil", branch="main")

    assert runner.calls == []


# --------------------------------------------------------------------------
# Reviewer evidence: update, never impersonate
# --------------------------------------------------------------------------


def test_update_pr_edits_only_the_description(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0)])
    client = GitHubClient(runner=runner)

    client.update_pr(
        tmp_path, "https://github.com/acme/repo/pull/42", body="new body", title="new title"
    )

    args, _cwd, _env = runner.calls[0]
    assert args == [
        "gh",
        "pr",
        "edit",
        "https://github.com/acme/repo/pull/42",
        "--body",
        "new body",
        "--title",
        "new title",
    ]


def test_update_pr_can_refresh_the_body_alone(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=0)])
    client = GitHubClient(runner=runner)

    client.update_pr(tmp_path, "https://github.com/acme/repo/pull/42", body="new body")

    assert "--title" not in runner.calls[0][0]


def test_update_pr_rejects_empty_content(tmp_path: Path) -> None:
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    with pytest.raises(ValueError):
        client.update_pr(tmp_path, "https://github.com/acme/repo/pull/42", body="  ")
    with pytest.raises(ValueError):
        client.update_pr(tmp_path, "https://github.com/acme/repo/pull/42", body="b", title=" ")

    assert runner.calls == []


def test_update_pr_failure_is_surfaced_not_swallowed(tmp_path: Path) -> None:
    client = GitHubClient(runner=FakeRunner([FakeCompletedProcess(returncode=1, stderr="nope")]))

    with pytest.raises(GitHubCommandError):
        client.update_pr(tmp_path, "https://github.com/acme/repo/pull/42", body="body")


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "review", "https://github.com/acme/repo/pull/42", "--approve"],
        ["pr", "ready", "https://github.com/acme/repo/pull/42"],
        ["api", "graphql", "-f", "query=..."],
        ["pr", "merge", "42", "--squash", "--admin"],
    ],
)
def test_client_refuses_to_review_or_bypass_protection(tmp_path: Path, args: list[str]) -> None:
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubError):
        client._run(args, tmp_path)

    assert runner.calls == []


# --------------------------------------------------------------------------
# Bounded commands: no remote call may hang forever
# --------------------------------------------------------------------------


def test_default_command_runner_bounds_a_hanging_command(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        default_command_runner(["sleep", "5"], tmp_path, None, 0.2)


def test_default_command_runner_has_a_bounded_default_timeout() -> None:
    signature = inspect.signature(default_command_runner)
    assert signature.parameters["timeout"].default == DEFAULT_COMMAND_TIMEOUT_SECONDS
    assert 0 < DEFAULT_COMMAND_TIMEOUT_SECONDS <= 600


class TimingOutRunner:
    """Runner whose every invocation exceeds its wall-clock budget."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd=None, env=None):  # noqa: ANN001 - test double
        self.calls.append(list(args))
        raise subprocess.TimeoutExpired(
            cmd=list(args), timeout=1.0, output="ghp_supersecrettoken1234"
        )


def test_git_timeouts_become_typed_redacted_errors(tmp_path: Path) -> None:
    publisher = GitPublisher(runner=TimingOutRunner())

    with pytest.raises(GitTimeoutError) as caught:
        publisher.commit_and_push(tmp_path, "factory/wi-1", "Implement feature")

    assert isinstance(caught.value, GitPublishError)
    assert "timed out" in str(caught.value)
    assert "ghp_supersecrettoken1234" not in str(caught.value)


def test_gh_timeouts_become_typed_redacted_errors(tmp_path: Path) -> None:
    client = GitHubClient(runner=TimingOutRunner(), token="ghp_supersecrettoken1234")  # noqa: S106

    with pytest.raises(GitHubTimeoutError) as caught:
        client.get_pull_request(tmp_path, "https://github.com/acme/repo/pull/42")

    assert isinstance(caught.value, GitHubError)
    assert "ghp_supersecrettoken1234" not in str(caught.value)

    with pytest.raises(GitHubTimeoutError):
        client.merge_pull_request(
            tmp_path,
            repository="acme/repo",
            number=42,
            method="squash",
            expected_head_sha="a" * 40,
        )

    with pytest.raises(GitHubTimeoutError):
        client.get_branch_policy(tmp_path, repository="acme/repo", branch="main")


def test_poll_checks_surfaces_a_timeout_instead_of_looping(tmp_path: Path) -> None:
    client = GitHubClient(runner=TimingOutRunner())

    with pytest.raises(GitHubTimeoutError):
        client.poll_checks(
            tmp_path,
            "https://github.com/acme/repo/pull/42",
            interval_seconds=0.01,
            max_polls=3,
            sleep=lambda _seconds: None,
        )


# --------------------------------------------------------------------------
# Ref name safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["main", "factory/WI-1", "release-2.0", "a_b.c"])
def test_plain_branch_names_are_accepted(name: str) -> None:
    assert is_safe_ref_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        " main",
        "main ",
        "-force",
        "refs/heads/main",
        "main..other",
        "main@{upstream}",
        "main:evil",
        "main~1",
        "main^",
        "main?",
        "main*",
        "main[1]",
        "main\\evil",
        "main.lock",
        "a//b",
        "/main",
        "main/",
        "@",
        "main\nother",
    ],
)
def test_unsafe_or_injected_ref_names_are_rejected(name: str) -> None:
    assert is_safe_ref_name(name) is False


def test_commit_and_push_rejects_an_unsafe_branch_name_before_any_command(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnsafeBranchNameError):
        publisher.commit_and_push(tmp_path, "factory/wi-1..evil", "Implement feature")

    assert runner.calls == []


# --------------------------------------------------------------------------
# Reviewed-tree and authorized-identity binding
# --------------------------------------------------------------------------


def test_commit_and_push_verifies_the_staged_tree_before_committing(tmp_path: Path) -> None:
    reviewed = "a" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),  # add -A
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=0, stdout=f"{reviewed}\n"),  # write-tree
            FakeCompletedProcess(returncode=0),  # commit
            FakeCompletedProcess(returncode=0, stdout="abc123\n"),  # rev-parse HEAD
            FakeCompletedProcess(returncode=0, stdout=f"{reviewed}\n"),  # commit tree
            FakeCompletedProcess(returncode=0),  # push
        ]
    )
    publisher = GitPublisher(runner=runner)

    sha = publisher.commit_and_push(
        tmp_path, "factory/wi-1", "Implement feature", expected_tree_sha=reviewed
    )

    assert sha == "abc123"
    assert ["git", "-C", str(tmp_path), "write-tree"] in [call[0] for call in runner.calls]


def test_commit_and_push_refuses_a_tree_the_reviewer_did_not_approve(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0),
            FakeCompletedProcess(returncode=0, stdout="src/app.py\n"),
            FakeCompletedProcess(returncode=0, stdout=f"{'b' * 40}\n"),
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnreviewedContentError):
        publisher.commit_and_push(
            tmp_path, "factory/wi-1", "Implement feature", expected_tree_sha="a" * 40
        )

    commands = [call[0] for call in runner.calls]
    assert not any("commit" in argv for argv in commands)
    assert not any("push" in argv for argv in commands)


def test_commit_and_push_refuses_a_repository_other_than_the_authorized_one(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([_remote_url_response("https://github.com/acme/other.git")])
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnexpectedRepositoryError):
        publisher.commit_and_push(
            tmp_path,
            "factory/wi-1",
            "Implement feature",
            expected_repository="acme/repo",
        )

    assert len(runner.calls) == 1


def test_verify_identity_is_case_insensitive_and_read_only(tmp_path: Path) -> None:
    runner = FakeRunner([_remote_url_response("https://github.com/Acme/Repo.git")])
    publisher = GitPublisher(runner=runner)

    target = publisher.verify_identity(
        tmp_path, expected_repository="acme/repo", expected_host="GitHub.com"
    )

    assert target.reference.full_name.casefold() == "acme/repo"
    assert target.url == "https://github.com/Acme/Repo.git"
    assert [call[0][3:5] for call in runner.calls] == [["remote", "get-url"]]


def test_verify_identity_rejects_an_unexpected_host(tmp_path: Path) -> None:
    runner = FakeRunner([_remote_url_response("https://github.com/acme/repo.git")])
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnexpectedRepositoryError):
        publisher.verify_identity(
            tmp_path, expected_repository="acme/repo", expected_host="ghe.example.com"
        )


def test_ensure_pushed_verifies_the_committed_tree(tmp_path: Path) -> None:
    reviewed = "c" * 40
    runner = FakeRunner(
        [
            _remote_url_response(),
            FakeCompletedProcess(returncode=0, stdout=f"{'0' * 40}\n"),  # rev-parse HEAD
            FakeCompletedProcess(returncode=0, stdout=f"{'d' * 40}\n"),  # rev-parse HEAD^{tree}
        ]
    )
    publisher = GitPublisher(runner=runner)

    with pytest.raises(UnreviewedContentError):
        publisher.ensure_pushed(tmp_path, "factory/wi-1", expected_tree_sha=reviewed)

    assert not any("push" in call[0] for call in runner.calls)


# --------------------------------------------------------------------------
# Classic protection cannot authorize a merge if anyone may bypass review
# --------------------------------------------------------------------------


def _classic_protection(allowances: object) -> dict:
    reviews: dict = {"required_approving_review_count": 1}
    if allowances is not None:
        reviews["bypass_pull_request_allowances"] = allowances
    return {
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"strict": True, "contexts": ["quality"]},
        "required_pull_request_reviews": reviews,
    }


@pytest.mark.parametrize(
    "allowances",
    [
        None,
        {"users": [{"login": "someone"}], "teams": [], "apps": []},
        {"users": [], "teams": [{"slug": "ops"}], "apps": []},
        {"users": [], "teams": [], "apps": [{"slug": "bot"}]},
        {"users": [], "teams": []},
        {},
    ],
)
def test_classic_protection_with_a_review_bypass_cannot_authorize(
    tmp_path: Path, allowances: object
) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="Not Found"),
            FakeCompletedProcess(returncode=0, stdout=json.dumps(_classic_protection(allowances))),
        ]
    )

    with pytest.raises(GitHubError):
        GitHubClient(runner=runner).get_branch_policy(
            tmp_path, repository="acme/repo", branch="main"
        )


def test_classic_protection_with_explicitly_empty_allowances_authorizes(tmp_path: Path) -> None:
    protection = _classic_protection({"users": [], "teams": [], "apps": []})
    runner = FakeRunner(
        [
            FakeCompletedProcess(returncode=1, stderr="Not Found"),
            FakeCompletedProcess(returncode=0, stdout=json.dumps(protection)),
        ]
    )

    policy = GitHubClient(runner=runner).get_branch_policy(
        tmp_path, repository="acme/repo", branch="main"
    )

    assert policy.sources == ("branch-protection",)
    assert policy.requires_pull_request is True
