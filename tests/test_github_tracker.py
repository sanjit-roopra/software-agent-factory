"""Tests for software_agent_factory.github_tracker.

All ``gh`` calls are fully faked through an injected argument-list runner.
These tests never touch the network or require the GitHub CLI to be
authenticated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from software_agent_factory.github import GitHubCommandError
from software_agent_factory.github_tracker import GitHubIssueProvider


class FakeCompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess[str]."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Scripted runner that records every call for assertions."""

    def __init__(self, responses: list[FakeCompletedProcess] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.default = FakeCompletedProcess(returncode=0, stdout="[]")

    def __call__(self, args, cwd=None, env=None):  # noqa: ANN001 - test double
        self.calls.append((list(args), cwd, dict(env) if env else None))
        if self.responses:
            return self.responses.pop(0)
        return self.default


def _issue_payload(
    number: int,
    *,
    repo: str = "acme/widgets",
    title: str | None = None,
    body: str = "Issue body",
    state: str = "OPEN",
    labels: tuple[str, ...] = ("agent-ready",),
    created_at: str = "2026-01-02T03:04:05Z",
    url: str | None = None,
) -> dict[str, object]:
    issue_url = url or f"https://github.com/{repo}/issues/{number}"
    return {
        "id": f"I_{number}",
        "number": number,
        "title": title or f"Issue {number}",
        "body": body,
        "state": state,
        "labels": [{"name": label} for label in labels],
        "createdAt": created_at,
        "url": issue_url,
    }


def test_fetch_candidates_uses_expected_command_and_normalizes_items(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                stdout=json.dumps(
                    [
                        _issue_payload(42, labels=("agent-ready", "priority:high", "bug")),
                        _issue_payload(43, labels=("agent-ready", "p0")),
                    ]
                )
            )
        ]
    )
    provider = GitHubIssueProvider(
        "acme/widgets",
        "agent-ready",
        tmp_path,
        gh_path="gh-bin",
        runner=runner,
    )

    items = provider.fetch_candidates()

    assert [item.opaque_id for item in items] == ["acme/widgets#42", "acme/widgets#43"]
    assert [item.identifier for item in items] == ["acme/widgets#42", "acme/widgets#43"]
    assert items[0].description == "Issue body"
    assert items[0].state == "OPEN"
    assert items[0].labels == ("agent-ready", "priority:high", "bug")
    assert items[0].priority == "P1"
    assert items[0].dispatchable is True
    assert items[0].repository_path == str(tmp_path)
    assert items[0].created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert items[1].priority == "P0"

    assert runner.calls == [
        (
            [
                "gh-bin",
                "issue",
                "list",
                "--repo",
                "acme/widgets",
                "--state",
                "open",
                "--label",
                "agent-ready",
                "--limit",
                "1000",
                "--json",
                "id,number,title,body,state,labels,createdAt,url",
            ],
            tmp_path,
            None,
        )
    ]


def test_fetch_candidates_excludes_pull_request_shaped_records(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                stdout=json.dumps(
                    [
                        _issue_payload(1),
                        _issue_payload(2, url="https://github.com/acme/widgets/pull/2"),
                    ]
                )
            )
        ]
    )
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=runner)

    items = provider.fetch_candidates()

    assert [item.opaque_id for item in items] == ["acme/widgets#1"]


def test_fetch_candidates_returns_items_beyond_default_gh_limit_and_uses_configured_argv(
    tmp_path: Path,
) -> None:
    issues = [_issue_payload(number) for number in range(1, 36)]
    runner = FakeRunner([FakeCompletedProcess(stdout=json.dumps(issues))])
    provider = GitHubIssueProvider(
        "acme/widgets",
        "agent-ready",
        tmp_path,
        runner=runner,
        candidate_limit=35,
    )

    items = provider.fetch_candidates()

    assert len(items) == 35
    assert items[0].opaque_id == "acme/widgets#1"
    assert items[-1].opaque_id == "acme/widgets#35"
    assert runner.calls == [
        (
            [
                "gh",
                "issue",
                "list",
                "--repo",
                "acme/widgets",
                "--state",
                "open",
                "--label",
                "agent-ready",
                "--limit",
                "35",
                "--json",
                "id,number,title,body,state,labels,createdAt,url",
            ],
            tmp_path,
            None,
        )
    ]


def test_fetch_by_ids_revalidates_each_issue_and_marks_closed_or_label_removed_undispatchable(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                stdout=json.dumps(
                    _issue_payload(42, state="CLOSED", labels=("agent-ready", "p1"))
                )
            ),
            FakeCompletedProcess(stdout=json.dumps(_issue_payload(43, labels=("bug",)))),
        ]
    )
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=runner)

    items = provider.fetch_by_ids(["acme/widgets#42", "acme/widgets#43"])

    assert [item.opaque_id for item in items] == ["acme/widgets#42", "acme/widgets#43"]
    assert items[0].state == "CLOSED"
    assert items[0].priority == "P1"
    assert items[0].dispatchable is False
    assert items[1].state == "OPEN"
    assert items[1].dispatchable is False
    assert runner.calls == [
        (
            [
                "gh",
                "issue",
                "view",
                "42",
                "--repo",
                "acme/widgets",
                "--json",
                "id,number,title,body,state,labels,createdAt,url",
            ],
            tmp_path,
            None,
        ),
        (
            [
                "gh",
                "issue",
                "view",
                "43",
                "--repo",
                "acme/widgets",
                "--json",
                "id,number,title,body,state,labels,createdAt,url",
            ],
            tmp_path,
            None,
        ),
    ]


def test_fetch_by_ids_omits_missing_issues(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stderr="GraphQL: Could not resolve to an issue with the number of 42.",
            )
        ]
    )
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=runner)

    items = provider.fetch_by_ids(["acme/widgets#42"])

    assert items == ()


@pytest.mark.parametrize("candidate_limit", [0, -1, 1001])
def test_provider_rejects_out_of_bounds_candidate_limit(
    tmp_path: Path, candidate_limit: int
) -> None:
    with pytest.raises(ValueError, match="candidate_limit must be between 1 and 1000"):
        GitHubIssueProvider(
            "acme/widgets",
            "agent-ready",
            tmp_path,
            runner=FakeRunner(),
            candidate_limit=candidate_limit,
        )


@pytest.mark.parametrize(
    ("opaque_id", "message"),
    [
        ("other/repo#42", "does not belong to repository"),
        ("acme/widgets#not-a-number", "positive issue number"),
        ("not-even-close", "must use the format"),
    ],
)
def test_fetch_by_ids_rejects_invalid_ids_clearly(
    tmp_path: Path, opaque_id: str, message: str
) -> None:
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=FakeRunner())

    with pytest.raises(ValueError, match=message):
        provider.fetch_by_ids([opaque_id])


def test_token_is_only_passed_via_env_and_errors_are_redacted(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            FakeCompletedProcess(
                returncode=1,
                stderr="authentication failed for ghp_secretsecret123456789",
            )
        ]
    )
    provider = GitHubIssueProvider(
        "acme/widgets",
        "agent-ready",
        tmp_path,
        token="ghp_providersecret987654321",  # noqa: S106
        runner=runner,
    )

    with pytest.raises(GitHubCommandError) as exc_info:
        provider.fetch_candidates()

    args, cwd, env = runner.calls[0]
    assert cwd == tmp_path
    assert "ghp_providersecret987654321" not in args
    assert env == {"GH_TOKEN": "ghp_providersecret987654321"}
    assert "ghp_providersecret987654321" not in repr(provider)
    assert "ghp_secretsecret123456789" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_fetch_candidates_raises_typed_error_on_nonzero_exit(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(returncode=1, stderr="boom")])
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=runner)

    with pytest.raises(GitHubCommandError, match="boom"):
        provider.fetch_candidates()


def test_fetch_candidates_raises_typed_error_on_malformed_json(tmp_path: Path) -> None:
    runner = FakeRunner([FakeCompletedProcess(stdout="{not json}")])
    provider = GitHubIssueProvider("acme/widgets", "agent-ready", tmp_path, runner=runner)

    with pytest.raises(GitHubCommandError, match="invalid JSON"):
        provider.fetch_candidates()
