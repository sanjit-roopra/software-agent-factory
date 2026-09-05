"""Tests for the controller-owned publishing/CI boundary.

No network access: every ``git``/``gh`` invocation goes through a fake
``CommandRunner``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from factory_testing import ScriptedRunner, build_config, check_payload

from software_agent_factory.github import CheckStatus, CIStatus, GitHubClient, GitPublisher
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
