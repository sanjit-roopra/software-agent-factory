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

import os
import subprocess
from pathlib import Path

import pytest

from software_agent_factory.github import (
    CheckResult,
    CheckStatus,
    CIPollTimeoutError,
    CIStatus,
    ExcessiveChangeScopeError,
    FailureCategory,
    GitCommandError,
    GitHubClient,
    GitHubCommandError,
    GitPublisher,
    GitPublishError,
    NoChangesToCommitError,
    ProtectedFileError,
    UnsafeBranchNameError,
    UnsafeRemoteError,
    build_pr_body,
    classify_failure,
    default_command_runner,
)
from software_agent_factory.models import (
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ReviewReport,
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
    assert push_call[0][-2:] == ["origin", "HEAD:refs/heads/factory/wi-1"]
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
        expected_scope=ExpectedScope(estimated_files_min=1, estimated_files_max=2),
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
