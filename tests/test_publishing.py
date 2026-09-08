"""Tests for the controller-owned publishing/CI boundary.

No network access: every ``git``/``gh`` invocation goes through a fake
``CommandRunner``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from factory_testing import FakeCompleted, ScriptedRunner, build_config, check_payload

from software_agent_factory.github import (
    CheckStatus,
    CIStatus,
    GitHubClient,
    GitHubCommandError,
    GitPublisher,
    UnexpectedRepositoryError,
    UnreviewedContentError,
)
from software_agent_factory.publishing import (
    CIObserver,
    PullRequestPublisher,
    normalize_ci_status,
    resolve_github_token,
)

# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def test_token_is_read_from_the_controller_environment_in_priority_order() -> None:
    assert resolve_github_token({"GH_TOKEN": "a", "GITHUB_TOKEN": "b"}) == "a"
    assert resolve_github_token({"GITHUB_TOKEN": "b"}) == "b"
    assert resolve_github_token({}) is None
    assert resolve_github_token({"GH_TOKEN": ""}) is None


def test_token_reaches_gh_only_through_the_child_environment(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    config = build_config(tmp_path, pull_request={"enabled": True})
    publisher = PullRequestPublisher(
        config,
        publisher=GitPublisher(runner=runner, base_branch="main"),
        client=GitHubClient(runner=runner, token="ghp_supersecrettoken1234"),  # noqa: S106
        token="ghp_supersecrettoken1234",  # noqa: S106
        runner=runner,
    )

    publisher.publish(
        workspace_path=tmp_path,
        branch_name="factory/WI-1",
        base_branch="main",
        commit_message="Do the thing",
        title="Do the thing",
        body="body",
    )

    gh_calls = [call for call in runner.calls if call[0][0].endswith("gh")]
    assert gh_calls, "expected a gh invocation"
    for argv, _cwd, env in gh_calls:
        assert "ghp_supersecrettoken1234" not in " ".join(argv)
        assert env == {"GH_TOKEN": "ghp_supersecrettoken1234"}

    # git never receives the token at all.
    for argv, _cwd, env in runner.calls:
        if argv[0] == "git":
            assert env is None


# ---------------------------------------------------------------------------
# Base branch resolution
# ---------------------------------------------------------------------------


def test_configured_base_branch_wins_over_the_repository_branch(tmp_path: Path) -> None:
    runner = ScriptedRunner(base_branch="trunk")
    config = build_config(tmp_path, pull_request={"enabled": True, "base_branch": "release"})
    publisher = PullRequestPublisher(config, runner=runner)

    assert publisher.resolve_base_branch(tmp_path) == "release"
    assert runner.calls == []


def test_base_branch_falls_back_to_main_for_a_detached_head(tmp_path: Path) -> None:
    runner = ScriptedRunner(base_branch="HEAD")
    config = build_config(tmp_path, pull_request={"enabled": True})
    publisher = PullRequestPublisher(config, runner=runner)

    assert publisher.resolve_base_branch(tmp_path) == "main"


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publish_creates_a_draft_pull_request_when_configured(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    config = build_config(tmp_path, pull_request={"enabled": True, "draft": True})
    publisher = PullRequestPublisher(
        config,
        publisher=GitPublisher(runner=runner, base_branch="main"),
        client=GitHubClient(runner=runner),
        token=None,
        runner=runner,
    )

    result = publisher.publish(
        workspace_path=tmp_path,
        branch_name="factory/WI-1",
        base_branch="main",
        commit_message="Do the thing",
        title="Do the thing",
        body="body",
    )

    assert result.created_pull_request is True
    assert result.pull_request_url == runner.pr_url
    assert result.commit_sha == runner.commit_sha
    create = [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "create"]][0]
    assert "--draft" in create


def test_publish_updates_an_existing_pull_request_without_recreating_it(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner()
    config = build_config(tmp_path, pull_request={"enabled": True})
    publisher = PullRequestPublisher(
        config,
        publisher=GitPublisher(runner=runner, base_branch="main"),
        client=GitHubClient(runner=runner),
        token=None,
        runner=runner,
    )

    result = publisher.publish(
        workspace_path=tmp_path,
        branch_name="factory/WI-1",
        base_branch="main",
        commit_message="Repair the thing",
        title="Do the thing",
        body="body",
        existing_pull_request_url="https://github.com/acme/repo/pull/7",
    )

    assert result.created_pull_request is False
    assert result.pull_request_url == "https://github.com/acme/repo/pull/7"
    assert not any(argv[1:3] == ["pr", "create"] for argv in runner.commands("gh"))
    assert runner.pushes()


# ---------------------------------------------------------------------------
# CI observation
# ---------------------------------------------------------------------------


def test_poll_budget_is_derived_from_the_configured_wait_window(tmp_path: Path) -> None:
    config = build_config(
        tmp_path,
        pull_request={"enabled": True},
        ci={
            "enabled": True,
            "poll_interval_seconds": 10,
            "max_wait_seconds": 95,
            "repair_attempts": 3,
        },
    )
    observer = CIObserver(config, client=GitHubClient(runner=ScriptedRunner()))

    assert observer.max_polls == 9


def test_observe_normalizes_a_passing_status(tmp_path: Path) -> None:
    runner = ScriptedRunner(check_responses=[[check_payload("build", "pass")]])
    config = build_config(
        tmp_path,
        pull_request={"enabled": True},
        ci={
            "enabled": True,
            "poll_interval_seconds": 1,
            "max_wait_seconds": 3,
            "repair_attempts": 1,
        },
    )
    observer = CIObserver(config, client=GitHubClient(runner=runner), sleep=lambda _s: None)

    report = observer.observe(
        repo_path=tmp_path, pull_request_url="https://github.com/acme/repo/pull/1"
    )

    assert report.overall == "PASS"
    assert report.timed_out is False
    assert report.failed_checks == []


def test_observe_reports_a_timeout_with_the_last_known_status(tmp_path: Path) -> None:
    runner = ScriptedRunner(check_responses=[[check_payload("build", "pending")]])
    config = build_config(
        tmp_path,
        pull_request={"enabled": True},
        ci={
            "enabled": True,
            "poll_interval_seconds": 1,
            "max_wait_seconds": 2,
            "repair_attempts": 1,
        },
    )
    observer = CIObserver(config, client=GitHubClient(runner=runner), sleep=lambda _s: None)

    report = observer.observe(
        repo_path=tmp_path, pull_request_url="https://github.com/acme/repo/pull/1"
    )

    assert report.timed_out is True
    assert report.overall == "PENDING"
    assert [check.name for check in report.checks] == ["build"]


def test_normalize_ci_status_is_a_pure_domain_projection() -> None:
    report = normalize_ci_status(CIStatus(overall=CheckStatus.PASS), repair_attempts_used=2)

    assert report.schema_version == 1
    assert report.overall == "PASS"
    assert report.repair_attempts_used == 2
    # Round-trips through persistence-shaped JSON.
    assert report.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize("bucket", ["pass", "fail", "pending", "cancel", "skipping"])
def test_every_bucket_normalizes_to_a_plain_string(tmp_path: Path, bucket: str) -> None:
    runner = ScriptedRunner(check_responses=[[check_payload("check", bucket)]])
    config = build_config(
        tmp_path,
        pull_request={"enabled": True},
        ci={
            "enabled": True,
            "poll_interval_seconds": 1,
            "max_wait_seconds": 2,
            "repair_attempts": 1,
        },
    )
    observer = CIObserver(config, client=GitHubClient(runner=runner), sleep=lambda _s: None)

    report = observer.observe(
        repo_path=tmp_path, pull_request_url="https://github.com/acme/repo/pull/1"
    )

    assert isinstance(report.overall, str)
    assert all(isinstance(check.status, str) for check in report.checks)


# ---------------------------------------------------------------------------
# Idempotent recovery (ADR-022): crash between push/create and persistence
# ---------------------------------------------------------------------------

BODY = "## Title\n\n### Run\nRun ID: `run-1`\n"


class RecoveryRunner(ScriptedRunner):
    """``ScriptedRunner`` with scriptable ``gh pr list`` output and an
    optionally clean working tree (nothing left to commit)."""

    def __init__(self, *, listed: list[dict] | None = None, clean: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.listed = listed or []
        self.clean = clean
        self.remote_branch_sha = ""

    def _git(self, argv):  # noqa: ANN001 - test double
        tail = argv[3:] if argv[1:2] == ["-C"] else argv[1:]
        if self.clean and tail[:3] == ["diff", "--cached", "--name-only"]:
            return FakeCompleted(stdout="")
        if tail[:1] == ["ls-remote"]:
            if not self.remote_branch_sha:
                return FakeCompleted(stdout="")
            return FakeCompleted(stdout=f"{self.remote_branch_sha}\trefs/heads/factory/WI-1\n")
        return super()._git(argv)

    def _gh(self, argv):  # noqa: ANN001 - test double
        if argv[1:3] == ["pr", "list"]:
            return FakeCompleted(stdout=json.dumps(self.listed))
        return super()._gh(argv)


def listed_pr(
    *,
    url: str = "https://github.com/acme/repo/pull/42",
    head: str = "factory/WI-1",
    base: str = "main",
    body: str = "Run ID: `run-1`",
    cross_repository: bool = False,
) -> dict:
    return {
        "number": 42,
        "url": url,
        "state": "OPEN",
        "isDraft": False,
        "isCrossRepository": cross_repository,
        "headRefName": head,
        "headRefOid": "a" * 40,
        "baseRefName": base,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "acme"},
        "body": body,
    }


def _publisher(tmp_path: Path, runner: ScriptedRunner) -> PullRequestPublisher:
    config = build_config(tmp_path, pull_request={"enabled": True, "draft": False})
    return PullRequestPublisher(
        config,
        publisher=GitPublisher(runner=runner, base_branch="main", branch_prefix="factory/"),
        client=GitHubClient(runner=runner),
        token=None,
        runner=runner,
    )


def _publish(publisher: PullRequestPublisher, tmp_path: Path, **overrides):
    kwargs = {
        "workspace_path": tmp_path,
        "branch_name": "factory/WI-1",
        "base_branch": "main",
        "commit_message": "Do the thing",
        "title": "Do the thing",
        "body": BODY,
    }
    kwargs.update(overrides)
    return publisher.publish(**kwargs)


def test_publish_reuses_an_existing_pull_request_for_the_same_head_and_run(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[listed_pr()])
    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.created_pull_request is False
    assert result.pull_request_url == "https://github.com/acme/repo/pull/42"
    assert not any(argv[1:3] == ["pr", "create"] for argv in runner.commands("gh"))


def test_publish_creates_a_pull_request_when_none_exists(tmp_path: Path) -> None:
    runner = RecoveryRunner(listed=[])
    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.created_pull_request is True
    assert result.pull_request_url == runner.pr_url


@pytest.mark.parametrize(
    "candidate",
    [
        listed_pr(body="Run ID: `other-run`"),
        listed_pr(cross_repository=True),
        listed_pr(head="factory/WI-2"),
        listed_pr(base="release"),
        listed_pr(url=""),
    ],
)
def test_publish_never_adopts_an_unrelated_pull_request(tmp_path: Path, candidate: dict) -> None:
    runner = RecoveryRunner(listed=[candidate])
    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.created_pull_request is True
    assert result.pull_request_url == runner.pr_url


def test_publish_creates_a_pull_request_when_discovery_is_ambiguous(tmp_path: Path) -> None:
    runner = RecoveryRunner(
        listed=[listed_pr(), listed_pr(url="https://github.com/acme/repo/pull/43")]
    )
    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.created_pull_request is True


def test_publish_recovers_when_the_work_was_already_committed_and_pushed(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(clean=True, listed=[listed_pr()])
    runner.remote_branch_sha = runner.commit_sha

    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.commit_sha == runner.commit_sha
    assert result.created_pull_request is False
    # Nothing new is committed, nothing is discarded, nothing is force pushed.
    assert not any("commit" in argv for argv in runner.commands("git"))
    assert runner.pushes() == []


def test_publish_recovers_when_the_work_was_committed_but_not_pushed(tmp_path: Path) -> None:
    runner = RecoveryRunner(clean=True, listed=[])

    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.commit_sha == runner.commit_sha
    assert not any("commit" in argv for argv in runner.commands("git"))
    push = runner.pushes()[0]
    assert push[-3:] == ["--", "origin", f"{runner.commit_sha}:refs/heads/factory/WI-1"]
    assert "--force" not in push


def test_publish_skips_discovery_entirely_for_a_known_pull_request(tmp_path: Path) -> None:
    runner = RecoveryRunner(listed=[listed_pr()])

    result = _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        existing_pull_request_url="https://github.com/acme/repo/pull/7",
    )

    assert result.pull_request_url == "https://github.com/acme/repo/pull/7"
    assert not any(argv[1:3] == ["pr", "list"] for argv in runner.commands("gh"))


# ---------------------------------------------------------------------------
# Reviewer evidence stays current on every published revision
# ---------------------------------------------------------------------------


def _pr_edits(runner: ScriptedRunner) -> list[list[str]]:
    return [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "edit"]]


def test_republishing_refreshes_the_body_with_the_latest_reviewer_evidence(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[])
    repaired = "## Title\n\n### Reviewer result\nApproved: True\n\n### Run\nRun ID: `run-1`\n"

    result = _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        body=repaired,
        existing_pull_request_url="https://github.com/acme/repo/pull/7",
    )

    assert result.created_pull_request is False
    assert result.updated_pull_request is True
    [edit] = _pr_edits(runner)
    assert edit[3] == "https://github.com/acme/repo/pull/7"
    assert edit[edit.index("--body") + 1] == repaired
    assert edit[edit.index("--title") + 1] == "Do the thing"


def test_a_discovered_pull_request_is_also_refreshed(tmp_path: Path) -> None:
    runner = RecoveryRunner(listed=[listed_pr()])

    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.updated_pull_request is True
    [edit] = _pr_edits(runner)
    assert edit[3] == "https://github.com/acme/repo/pull/42"


def test_a_newly_created_pull_request_is_not_edited_again(tmp_path: Path) -> None:
    runner = RecoveryRunner(listed=[])

    result = _publish(_publisher(tmp_path, runner), tmp_path)

    assert result.created_pull_request is True
    assert result.updated_pull_request is False
    assert _pr_edits(runner) == []


def test_a_failed_body_refresh_is_not_silently_ignored(tmp_path: Path) -> None:
    class FailingEdit(RecoveryRunner):
        def _gh(self, argv):  # noqa: ANN001 - test double
            if argv[1:3] == ["pr", "edit"]:
                return FakeCompleted(returncode=1, stderr="could not update pull request")
            return super()._gh(argv)

    runner = FailingEdit(listed=[])

    with pytest.raises(GitHubCommandError):
        _publish(
            _publisher(tmp_path, runner),
            tmp_path,
            existing_pull_request_url="https://github.com/acme/repo/pull/7",
        )


def test_publishing_never_submits_a_github_review_or_bypasses_protection(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[listed_pr()])

    _publish(_publisher(tmp_path, runner), tmp_path)

    gh_commands = runner.commands("gh")
    assert not any(argv[1:3] == ["pr", "review"] for argv in gh_commands)
    assert not any(argv[1:3] == ["pr", "merge"] for argv in gh_commands)
    assert not any("--admin" in argv for argv in gh_commands)


# ---------------------------------------------------------------------------
# Binding a publication to the reviewed tree and authorized repository
# ---------------------------------------------------------------------------

REVIEWED_TREE = ScriptedRunner.commit_sha


def test_publish_accepts_the_reviewed_tree_and_authorized_identity(tmp_path: Path) -> None:
    runner = ScriptedRunner()

    result = _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        expected_tree_sha=REVIEWED_TREE,
        expected_repository="acme/repo",
        expected_host="github.com",
    )

    assert result.commit_sha == ScriptedRunner.commit_sha
    assert any(argv[-1] == "write-tree" for argv, _cwd, _env in runner.calls)


def test_publish_refuses_content_that_is_not_the_reviewed_tree(tmp_path: Path) -> None:
    runner = ScriptedRunner()

    with pytest.raises(UnreviewedContentError):
        _publish(
            _publisher(tmp_path, runner),
            tmp_path,
            expected_tree_sha="f" * 40,
        )

    tokens = [argv for argv, _cwd, _env in runner.calls]
    assert not any("commit" in argv for argv in tokens)
    assert not any("push" in argv for argv in tokens)


def test_publish_refuses_a_tree_that_is_not_a_full_object_name(tmp_path: Path) -> None:
    runner = ScriptedRunner()

    with pytest.raises(UnreviewedContentError):
        _publish(_publisher(tmp_path, runner), tmp_path, expected_tree_sha="abc")


def test_publish_refuses_a_remote_repointed_after_authorization(tmp_path: Path) -> None:
    runner = ScriptedRunner(remote_url="https://github.com/acme/other.git")

    with pytest.raises(UnexpectedRepositoryError):
        _publish(
            _publisher(tmp_path, runner),
            tmp_path,
            expected_repository="acme/repo",
            expected_host="github.com",
        )

    assert not any("push" in argv for argv, _cwd, _env in runner.calls)


def test_publish_refuses_a_remote_on_another_host(tmp_path: Path) -> None:
    runner = ScriptedRunner(remote_url="https://github.com/acme/repo.git")

    with pytest.raises(UnexpectedRepositoryError):
        _publish(
            _publisher(tmp_path, runner),
            tmp_path,
            expected_repository="acme/repo",
            expected_host="ghe.example.com",
        )


def test_recovering_an_already_committed_workspace_still_binds_the_reviewed_tree(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[listed_pr()], clean=True)

    with pytest.raises(UnreviewedContentError):
        _publish(_publisher(tmp_path, runner), tmp_path, expected_tree_sha="a" * 40)

    assert not any("push" in argv for argv, _cwd, _env in runner.calls)


def test_recovering_an_already_committed_workspace_accepts_the_reviewed_tree(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[listed_pr()], clean=True)

    result = _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        expected_tree_sha=REVIEWED_TREE,
        expected_repository="acme/repo",
        expected_host="github.com",
    )

    assert result.created_pull_request is False


def test_an_authorized_publish_pushes_to_the_validated_url_not_a_mutable_alias(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner()

    _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        expected_repository="acme/repo",
        expected_host="github.com",
    )

    push = [argv for argv, _cwd, _env in runner.calls if "push" in argv][0]
    assert push[-2] == "https://github.com/acme/repo.git"
    assert "origin" not in push
    assert "--force" not in push


def test_pull_request_commands_name_the_authorized_repository(tmp_path: Path) -> None:
    runner = RecoveryRunner(listed=[])

    _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        expected_repository="acme/repo",
        expected_host="github.com",
    )

    gh_commands = runner.commands("gh")
    assert gh_commands
    for argv in gh_commands:
        assert "--repo" in argv
        assert argv[argv.index("--repo") + 1] == "github.com/acme/repo"


def test_an_existing_pull_request_is_refreshed_against_the_authorized_repository(
    tmp_path: Path,
) -> None:
    runner = RecoveryRunner(listed=[])

    _publish(
        _publisher(tmp_path, runner),
        tmp_path,
        existing_pull_request_url="https://github.com/acme/repo/pull/7",
        expected_repository="acme/repo",
    )

    edits = [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "edit"]]
    assert edits
    assert edits[0][edits[0].index("--repo") + 1] == "acme/repo"
