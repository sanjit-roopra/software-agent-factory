"""Tests for the controller-owned, fail-closed merge boundary (ADR-022).

Hermetic: every ``git``/``gh`` invocation goes through a fake
``CommandRunner``. No network call, no real subprocess, no model call.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import pytest
from factory_testing import build_config

from software_agent_factory.config import FactoryConfig
from software_agent_factory.github import (
    GitHubCommandError,
    GitHubTimeoutError,
    MergeNotAllowedError,
    UnknownMergeOutcomeError,
    UnsafeRemoteError,
)
from software_agent_factory.merging import MergeResult, PullRequestMerger

HEAD = "a" * 40
MERGE_COMMIT = "b" * 40
PR_URL = "https://github.com/acme/repo/pull/42"


def pr_payload(
    *,
    state: str = "OPEN",
    head_oid: str = HEAD,
    base: str = "main",
    head_branch: str = "factory/WI-1",
    checks: Sequence[Mapping[str, object]] | None = None,
    mergeable: str = "MERGEABLE",
    merge_state: str = "CLEAN",
    review_decision: str = "APPROVED",
    merge_commit: str | None = None,
    cross_repository: bool = False,
    head_repository: str = "acme/repo",
    draft: bool = False,
    url: str = PR_URL,
    number: int = 42,
    body: str = "Run ID: `run-1`",
) -> dict[str, object]:
    owner, _, name = head_repository.partition("/")
    return {
        "number": number,
        "url": url,
        "state": state,
        "isDraft": draft,
        "isCrossRepository": cross_repository,
        "headRefName": head_branch,
        "headRefOid": head_oid,
        "baseRefName": base,
        "headRepository": {"name": name},
        "headRepositoryOwner": {"login": owner},
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": review_decision,
        "mergeCommit": {"oid": merge_commit} if merge_commit else None,
        "statusCheckRollup": list(
            checks if checks is not None else [check_run("quality", "SUCCESS")]
        ),
        "body": body,
    }


def check_run(name: str, conclusion: str, *, status: str = "COMPLETED") -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://github.com/acme/repo/actions/runs/1",
    }


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeGitHub:
    """Fake ``git``/``gh`` runner scripted by pull request payloads.

    Models the REST surface the merger actually uses: ``gh pr view --json``,
    ``gh api repos/.../rules/branches/...`` (or classic protection) and the
    synchronous ``gh api --method PUT repos/.../pulls/N/merge``.
    """

    views: list[dict[str, object]] = field(default_factory=list)
    remote_url: str = "https://github.com/acme/repo.git"
    merge_returncode: int = 0
    merge_stderr: str = ""
    merge_response: dict[str, object] | None = None
    view_after_merge: dict[str, object] | None = None
    view_error: bool = False
    policy_contexts: list[str] | None = None
    policy_strict: bool = True
    policy_requires_pull_request: bool = True
    policy_readable: bool = True
    calls: list[list[str]] = field(default_factory=list)
    merged: bool = False

    def _policy_payload(self) -> list[dict[str, object]]:
        contexts = self.policy_contexts if self.policy_contexts is not None else ["quality"]
        rules: list[dict[str, object]] = [
            {
                "ruleset_id": 1,
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [{"context": name} for name in contexts],
                    "strict_required_status_checks_policy": self.policy_strict,
                },
            }
        ]
        if self.policy_requires_pull_request:
            rules.append({"ruleset_id": 1, "type": "pull_request", "parameters": {}})
        return rules

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> FakeCompleted:
        argv = list(args)
        self.calls.append(argv)
        if argv[0] == "git":
            tail = argv[3:] if argv[1:2] == ["-C"] else argv[1:]
            if tail[:2] == ["remote", "get-url"]:
                return FakeCompleted(stdout=f"{self.remote_url}\n")
            return FakeCompleted()
        if argv[1:3] == ["pr", "view"]:
            if self.view_error:
                return FakeCompleted(returncode=1, stderr="gh: not found")
            if self.merged and self.view_after_merge is not None:
                return FakeCompleted(stdout=json.dumps(self.view_after_merge))
            payload = self.views[0] if len(self.views) == 1 else self.views.pop(0)
            return FakeCompleted(stdout=json.dumps(payload))
        if argv[1:2] == ["api"] and any("/rules/branches/" in part for part in argv):
            if not self.policy_readable:
                return FakeCompleted(returncode=1, stderr="HTTP 403")
            return FakeCompleted(stdout=json.dumps(self._policy_payload()))
        if argv[1:2] == ["api"] and any(part.endswith("/protection") for part in argv):
            return FakeCompleted(returncode=1, stderr="HTTP 404")
        if argv[1:2] == ["api"] and any(part.endswith("/rulesets/1") for part in argv):
            return FakeCompleted(stdout=json.dumps({"enforcement": "active", "bypass_actors": []}))
        if argv[1:2] == ["api"] and any(part.endswith("/merge") for part in argv):
            self.merged = self.merge_returncode == 0
            payload = self.merge_response
            if payload is None:
                payload = (
                    {"merged": True, "sha": MERGE_COMMIT, "message": "Pull Request merged"}
                    if self.merge_returncode == 0
                    else {"message": self.merge_stderr or "not mergeable"}
                )
            return FakeCompleted(
                returncode=self.merge_returncode,
                stdout=json.dumps(payload),
                stderr=self.merge_stderr,
            )
        return FakeCompleted()

    def gh_commands(self) -> list[list[str]]:
        return [argv for argv in self.calls if argv and argv[0].endswith("gh")]

    def merge_commands(self) -> list[list[str]]:
        return [
            argv
            for argv in self.gh_commands()
            if argv[1:2] == ["api"] and any(part.endswith("/merge") for part in argv)
        ]

    def policy_commands(self) -> list[list[str]]:
        return [
            argv
            for argv in self.gh_commands()
            if argv[1:2] == ["api"] and any("/rules/branches/" in part for part in argv)
        ]


def merge_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    method: str = "squash",
    repositories: list[str] | None = None,
    required_checks: list[str] | None = None,
    base_branch: str = "main",
) -> FactoryConfig:
    payload = build_config(
        tmp_path / "data",
        verify=["true"],
        pull_request={"enabled": True, "draft": False, "base_branch": base_branch},
        ci={"enabled": True},
    ).model_dump(mode="json")
    payload["merge"] = {
        "enabled": enabled,
        "method": method,
        "allowed_repositories": repositories if repositories is not None else ["acme/repo"],
        "required_checks": required_checks if required_checks is not None else ["quality"],
    }
    return FactoryConfig.model_validate(payload)


def build_merger(tmp_path: Path, runner: FakeGitHub, **kwargs) -> PullRequestMerger:
    return PullRequestMerger(merge_config(tmp_path, **kwargs), runner=runner, token=None)


def do_merge(merger: PullRequestMerger, tmp_path: Path, **overrides) -> MergeResult:
    kwargs: dict[str, object] = {
        "repo_path": tmp_path,
        "pull_request_url": PR_URL,
        "expected_head_sha": HEAD,
        "base_branch": "main",
    }
    kwargs.update(overrides)
    return merger.merge(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Repository authorization
# ---------------------------------------------------------------------------


def test_validate_repository_returns_the_allowlisted_repository(tmp_path: Path) -> None:
    runner = FakeGitHub()
    merger = build_merger(tmp_path, runner)

    assert merger.validate_repository(tmp_path) == "acme/repo"


def test_github_response_cannot_switch_hosts(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(url="https://elsewhere.invalid/acme/repo/pull/42")])
    with pytest.raises(MergeNotAllowedError, match="returned pull request"):
        do_merge(build_merger(tmp_path, runner), tmp_path)
    assert runner.merge_commands() == []


def test_merge_and_policy_calls_pin_the_requested_host(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    do_merge(build_merger(tmp_path, runner), tmp_path)
    for args in runner.gh_commands():
        if args[1] == "api":
            assert args[args.index("--hostname") + 1] == "github.com"
        if args[1:3] == ["pr", "view"]:
            assert args[args.index("--repo") + 1] == "github.com/acme/repo"


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@github.com:acme/repo.git",
        "https://github.com/Acme/Repo",
        "ssh://git@github.com/acme/repo.git",
    ],
)
def test_validate_repository_accepts_equivalent_remote_url_shapes(
    tmp_path: Path, remote_url: str
) -> None:
    merger = build_merger(tmp_path, FakeGitHub(remote_url=remote_url))

    assert merger.validate_repository(tmp_path) == "acme/repo"


def test_validate_repository_rejects_a_host_outside_the_allowlist(tmp_path: Path) -> None:
    merger = build_merger(tmp_path, FakeGitHub(remote_url="https://evil.example/acme/repo.git"))

    with pytest.raises(UnsafeRemoteError):
        merger.validate_repository(tmp_path)


def test_validate_repository_rejects_a_repository_outside_the_allowlist(tmp_path: Path) -> None:
    merger = build_merger(tmp_path, FakeGitHub(remote_url="https://github.com/other/repo.git"))

    with pytest.raises(MergeNotAllowedError, match="allowed_repositories"):
        merger.validate_repository(tmp_path)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_merge_requests_a_normal_merge_and_confirms_the_merge_commit(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner)

    result = do_merge(merger, tmp_path)

    assert result == MergeResult(commit_sha=MERGE_COMMIT, pull_request_url=PR_URL)
    merge_command = runner.merge_commands()[0]
    assert merge_command[1:] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "PUT",
        "-H",
        "Accept: application/vnd.github+json",
        "repos/acme/repo/pulls/42/merge",
        "-f",
        f"sha={HEAD}",
        "-f",
        "merge_method=squash",
    ]
    assert "--admin" not in merge_command
    assert "--auto" not in merge_command
    # Never the porcelain command, which can enqueue or defer a merge.
    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.gh_commands())
    # Head is inspected before *and* after the checks are read, and once more
    # to confirm the outcome: three bounded reads, one merge, no retry loop.
    views = [argv for argv in runner.gh_commands() if argv[1:3] == ["pr", "view"]]
    assert len(views) == 3
    assert len(runner.merge_commands()) == 1


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_configured_merge_method_is_sent_to_the_rest_endpoint(tmp_path: Path, method: str) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner, method=method)

    do_merge(merger, tmp_path)

    assert f"merge_method={method}" in runner.merge_commands()[0]


def test_no_git_push_or_admin_command_is_ever_issued(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner)

    do_merge(merger, tmp_path)

    tokens = [token for argv in runner.calls for token in argv]
    assert "push" not in tokens
    assert "--admin" not in tokens
    assert "--auto" not in tokens
    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.gh_commands())
    # Only the merge endpoint and read-only policy/state reads are used.
    assert not any(argv[1:2] == ["api"] and "graphql" in argv for argv in runner.gh_commands())


# ---------------------------------------------------------------------------
# Identity, revision and base guards
# ---------------------------------------------------------------------------


def test_merge_is_disabled_unless_configured(tmp_path: Path) -> None:
    merger = build_merger(tmp_path, FakeGitHub(), enabled=False)

    with pytest.raises(MergeNotAllowedError, match="disabled"):
        do_merge(merger, tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/acme/repo/pull/42",
        "https://github.com/other/repo/pull/42",
        "http://github.com/acme/repo/pull/42",
        "https://github.com/acme/repo/pulls/42",
        "not-a-url",
    ],
)
def test_merge_rejects_a_pull_request_url_outside_the_allowlist(tmp_path: Path, url: str) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path, pull_request_url=url)

    assert runner.merge_commands() == []


def test_merge_rejects_a_pull_request_from_another_repository(tmp_path: Path) -> None:
    runner = FakeGitHub(remote_url="https://github.com/acme/other.git", views=[pr_payload()])
    merger = build_merger(tmp_path, runner, repositories=["acme/repo", "acme/other"])

    with pytest.raises(MergeNotAllowedError, match="local remote"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_rejects_a_pull_request_gh_resolved_to_a_different_number(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(url="https://github.com/acme/repo/pull/7", number=7)],
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="returned pull request"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_rejects_a_fork_head(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(cross_repository=True, head_repository="attacker/repo")],
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="head repository"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


@pytest.mark.parametrize("sha", ["", "abc", "z" * 40, HEAD[:39], f"{HEAD} --admin"])
def test_merge_requires_a_full_expected_head_sha(tmp_path: Path, sha: str) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="40-character"):
        do_merge(merger, tmp_path, expected_head_sha=sha)

    assert runner.gh_commands() == []


def test_merge_refuses_a_head_that_moved_after_review(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(head_oid="c" * 40)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="not the reviewed commit"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_when_the_head_moves_between_the_two_reads(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(), pr_payload(head_oid="c" * 40)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="not the reviewed commit"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_a_base_branch_other_than_the_configured_one(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="pull_request.base_branch"):
        do_merge(merger, tmp_path, base_branch="release")

    assert runner.gh_commands() == []


def test_merge_refuses_a_pull_request_targeting_another_branch(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(base="production")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="intended base branch"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_a_head_branch_without_the_factory_prefix(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(head_branch="hotfix/manual")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="branch prefix"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_a_draft_pull_request(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(draft=True)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="draft"):
        do_merge(merger, tmp_path)


def test_merge_refuses_a_closed_pull_request(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(state="CLOSED")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="not OPEN"):
        do_merge(merger, tmp_path)


# ---------------------------------------------------------------------------
# Mergeability and review guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN", ""])
def test_merge_fails_closed_on_conflicts_or_unknown_mergeability(
    tmp_path: Path, mergeable: str
) -> None:
    runner = FakeGitHub(views=[pr_payload(mergeable=mergeable)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="mergeable"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


@pytest.mark.parametrize("merge_state", ["BLOCKED", "BEHIND", "DIRTY", "UNSTABLE", "UNKNOWN", ""])
def test_merge_fails_closed_on_a_blocked_or_outdated_merge_state(
    tmp_path: Path, merge_state: str
) -> None:
    runner = FakeGitHub(views=[pr_payload(merge_state=merge_state)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="mergeStateStatus"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_when_a_reviewer_requested_changes(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(review_decision="CHANGES_REQUESTED")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="requested changes"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


# ---------------------------------------------------------------------------
# Required checks
# ---------------------------------------------------------------------------


def test_missing_required_check_never_authorizes_a_merge(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(checks=[check_run("other", "SUCCESS")])])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="not present"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL", "CANCELLED", "FAILURE", "STALE"])
def test_a_required_check_must_actually_pass(tmp_path: Path, conclusion: str) -> None:
    runner = FakeGitHub(views=[pr_payload(checks=[check_run("quality", conclusion)])])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_a_pending_required_check_never_authorizes_a_merge(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(checks=[check_run("quality", "", status="IN_PROGRESS")])])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path)


def test_no_checks_at_all_never_authorizes_a_merge(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(checks=[])])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="not present"):
        do_merge(merger, tmp_path)


def test_other_failing_or_pending_checks_fail_closed(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[
            pr_payload(
                checks=[check_run("quality", "SUCCESS"), check_run("smoke", "FAILURE")],
            )
        ]
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="other check"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_a_skipped_non_required_check_does_not_block_a_merge(tmp_path: Path) -> None:
    checks = [check_run("quality", "SUCCESS"), check_run("optional", "SKIPPED")]
    runner = FakeGitHub(
        views=[pr_payload(checks=checks), pr_payload(checks=checks)],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner)

    assert do_merge(merger, tmp_path).commit_sha == MERGE_COMMIT


def test_commit_status_contexts_are_honored_as_checks(tmp_path: Path) -> None:
    contexts = [
        {"__typename": "StatusContext", "context": "quality", "state": "SUCCESS"},
        {"__typename": "StatusContext", "context": "legacy", "state": "PENDING"},
    ]
    runner = FakeGitHub(views=[pr_payload(checks=contexts)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="legacy=PENDING"):
        do_merge(merger, tmp_path)


# ---------------------------------------------------------------------------
# Outcome confirmation and idempotent recovery
# ---------------------------------------------------------------------------


def test_an_already_merged_matching_pull_request_is_idempotent_success(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(state="MERGED", merge_commit=MERGE_COMMIT)])
    merger = build_merger(tmp_path, runner)

    result = do_merge(merger, tmp_path)

    assert result == MergeResult(commit_sha=MERGE_COMMIT, pull_request_url=PR_URL)
    assert runner.merge_commands() == []


def test_an_already_merged_pull_request_at_another_head_is_rejected(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(state="MERGED", head_oid="c" * 40, merge_commit=MERGE_COMMIT)]
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="already merged"):
        do_merge(merger, tmp_path)


def test_a_merged_pull_request_without_a_merge_commit_is_an_unknown_outcome(
    tmp_path: Path,
) -> None:
    runner = FakeGitHub(views=[pr_payload(state="MERGED")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(UnknownMergeOutcomeError):
        do_merge(merger, tmp_path)


def test_a_refused_merge_is_an_explicit_failure_not_a_retry_loop(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload(), pr_payload()],
        merge_returncode=1,
        merge_stderr="Pull request is not mergeable: the head has changed",
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="refused to merge"):
        do_merge(merger, tmp_path)

    assert len(runner.merge_commands()) == 1


def test_a_successful_command_that_did_not_merge_is_an_unknown_outcome(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(), pr_payload(), pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(UnknownMergeOutcomeError, match="still OPEN"):
        do_merge(merger, tmp_path)


def test_an_unreadable_confirmation_is_an_unknown_outcome_not_success(tmp_path: Path) -> None:
    class FailingConfirm(FakeGitHub):
        def __call__(self, args, cwd=None, env=None):  # noqa: ANN001 - test double
            result = super().__call__(args, cwd, env)
            if self.merged and list(args)[1:3] == ["pr", "view"]:
                return FakeCompleted(returncode=1, stderr="network unreachable")
            return result

    runner = FailingConfirm(views=[pr_payload(), pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(UnknownMergeOutcomeError, match="could not be confirmed"):
        do_merge(merger, tmp_path)


def test_an_unreadable_pull_request_before_merging_surfaces_the_gh_error(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload()], view_error=True)
    merger = build_merger(tmp_path, runner)

    with pytest.raises(GitHubCommandError):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merging_never_submits_a_github_review(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner)

    do_merge(merger, tmp_path)

    # The independent Reviewer's approval is evidence in the PR body, never a
    # GitHub review submitted with the factory's credentials.
    assert not any(argv[1:3] == ["pr", "review"] for argv in runner.gh_commands())
    assert not any(argv[1:3] == ["pr", "edit"] for argv in runner.gh_commands())


# ---------------------------------------------------------------------------
# Stale or injected refs, and bounded commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base", ["main..evil", "-main", "refs/heads/main", "main:evil", "main~1", "main@{1}"]
)
def test_merge_rejects_an_unsafe_base_branch_before_any_command(tmp_path: Path, base: str) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner, base_branch="main")

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path, base_branch=base)

    assert runner.gh_commands() == []


@pytest.mark.parametrize("ref", ["factory/WI-1..evil", "refs/heads/factory/WI-1", "-factory/WI-1"])
def test_merge_rejects_an_injected_head_ref_reported_by_github(tmp_path: Path, ref: str) -> None:
    runner = FakeGitHub(views=[pr_payload(head_branch=ref)])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_rejects_an_injected_base_ref_reported_by_github(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload(base="main:evil")])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="unsafe head/base ref"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_an_already_merged_pull_request_with_an_unsafe_base_is_rejected(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(state="MERGED", base="main..evil", merge_commit=MERGE_COMMIT)]
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path)


def test_a_hanging_github_call_fails_the_merge_instead_of_blocking(tmp_path: Path) -> None:
    class Hanging(FakeGitHub):
        def __call__(self, args, cwd=None, env=None):  # noqa: ANN001 - test double
            argv = list(args)
            if argv[0].endswith("gh"):
                self.calls.append(argv)
                raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)
            return super().__call__(args, cwd, env)

    merger = build_merger(tmp_path, Hanging(views=[pr_payload()]))

    with pytest.raises(GitHubTimeoutError):
        do_merge(merger, tmp_path)


# ---------------------------------------------------------------------------
# Binding to the identity authorized at dispatch time
# ---------------------------------------------------------------------------


def test_merge_accepts_the_repository_identity_authorized_for_the_run(tmp_path: Path) -> None:
    runner = FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
    )
    merger = build_merger(tmp_path, runner)

    result = do_merge(
        merger,
        tmp_path,
        expected_repository="acme/repo",
        expected_host="github.com",
    )

    assert result.commit_sha == MERGE_COMMIT


def test_merge_refuses_a_pull_request_in_another_authorized_repository(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner, repositories=["acme/repo", "acme/other"])

    with pytest.raises(MergeNotAllowedError, match="authorized delivery repository"):
        do_merge(merger, tmp_path, expected_repository="acme/other")

    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.calls)


def test_merge_refuses_a_host_that_differs_from_the_authorized_host(tmp_path: Path) -> None:
    runner = FakeGitHub(views=[pr_payload()])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="authorized delivery host"):
        do_merge(merger, tmp_path, expected_host="ghe.example.com")

    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.calls)


def test_merge_refuses_when_the_local_remote_was_repointed_after_authorization(
    tmp_path: Path,
) -> None:
    runner = FakeGitHub(remote_url="https://github.com/acme/other.git")
    merger = build_merger(tmp_path, runner, repositories=["acme/repo", "acme/other"])

    with pytest.raises(MergeNotAllowedError):
        do_merge(merger, tmp_path, expected_repository="acme/repo", expected_host="github.com")

    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.calls)


# ---------------------------------------------------------------------------
# Server-enforced protection (the merge gate GitHub applies atomically)
# ---------------------------------------------------------------------------


def _green_runner(**kwargs) -> FakeGitHub:
    return FakeGitHub(
        views=[pr_payload(), pr_payload()],
        view_after_merge=pr_payload(state="MERGED", merge_commit=MERGE_COMMIT),
        **kwargs,
    )


def test_the_base_branch_policy_is_read_before_merging(tmp_path: Path) -> None:
    runner = _green_runner()
    merger = build_merger(tmp_path, runner)

    do_merge(merger, tmp_path)

    policy_calls = runner.policy_commands()
    assert policy_calls, "expected the enforced branch policy to be read"
    assert "repos/acme/repo/rules/branches/main" in policy_calls[0]
    # Read before the merge request, never after it.
    assert runner.calls.index(policy_calls[0]) < runner.calls.index(runner.merge_commands()[0])


def test_merge_refuses_when_github_does_not_require_the_configured_checks(
    tmp_path: Path,
) -> None:
    runner = _green_runner(policy_contexts=["something-else"])
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="does not require"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_when_github_does_not_require_up_to_date_branches(tmp_path: Path) -> None:
    runner = _green_runner(policy_strict=False)
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="up to date"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_merge_refuses_when_github_does_not_require_a_pull_request(tmp_path: Path) -> None:
    runner = _green_runner(policy_requires_pull_request=False)
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="reviewed pull request"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_an_unreadable_branch_policy_fails_closed(tmp_path: Path) -> None:
    runner = _green_runner(policy_readable=False)
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="could not be read"):
        do_merge(merger, tmp_path)

    assert runner.merge_commands() == []


def test_a_merge_queue_response_is_refused_rather_than_left_pending(tmp_path: Path) -> None:
    runner = _green_runner(
        merge_returncode=1,
        merge_response={"message": "Changes must be made through the merge queue"},
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(MergeNotAllowedError, match="merge queue"):
        do_merge(merger, tmp_path)


def test_a_merge_commit_disagreement_is_an_unknown_outcome(tmp_path: Path) -> None:
    runner = _green_runner(
        merge_response={"merged": True, "sha": "9" * 40, "message": "merged"},
    )
    merger = build_merger(tmp_path, runner)

    with pytest.raises(UnknownMergeOutcomeError):
        do_merge(merger, tmp_path)


def test_every_pull_request_read_names_the_repository_explicitly(tmp_path: Path) -> None:
    runner = _green_runner()
    merger = build_merger(tmp_path, runner)

    do_merge(merger, tmp_path)

    views = [argv for argv in runner.gh_commands() if argv[1:3] == ["pr", "view"]]
    assert views
    for argv in views:
        assert "--repo" in argv
        assert argv[argv.index("--repo") + 1] == "github.com/acme/repo"
