"""Controller-owned pull request publishing and CI observation.

Thin, configuration-aware composition over :mod:`software_agent_factory.github`
so :class:`~software_agent_factory.workflow.WorkflowController` stays a state
machine rather than a ``git``/``gh`` driver.

Everything here is strictly opt-in: nothing runs unless
``pull_request.enabled`` (and, for CI, ``ci.enabled``) is set in configuration.
The workflow controller is the only caller.

Credential boundary (``AGENTS.md`` "Agents do NOT control ... production
credentials"): the GitHub token is read from the *controller's* environment
here and handed to :class:`~software_agent_factory.github.GitHubClient`, which
passes it to ``gh`` through the child environment only. It is never placed on
a command line, never written to an artifact, and never reaches an agent -- the
Copilot runtime independently strips ``GH_TOKEN``/``GITHUB_TOKEN`` from every
agent subprocess environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import FactoryConfig
from .github import (
    CheckStatus,
    CIPollTimeoutError,
    CIStatus,
    CommandRunner,
    GitHubClient,
    GitPublisher,
    default_command_runner,
)
from .models import CICheckEvidence, CIReport

#: Environment variables the controller (and only the controller) may read a
#: GitHub token from, in priority order.
TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")

DEFAULT_BASE_BRANCH = "main"


def resolve_github_token(environ: dict[str, str] | None = None) -> str | None:
    """Read a GitHub token from the controller's own environment."""
    source = os.environ if environ is None else environ
    for name in TOKEN_ENV_VARS:
        value = source.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class PublishResult:
    """What one commit/push (+ optional PR creation) produced."""

    commit_sha: str
    base_branch: str
    pull_request_url: str | None
    created_pull_request: bool


class PullRequestPublisher:
    """Commits, pushes and opens a PR for a controller-owned branch.

    Never merges: the only ``gh`` verbs reachable from here are
    ``pr create`` and (via :class:`CIObserver`) ``pr checks`` / ``run view``.
    """

    def __init__(
        self,
        config: FactoryConfig,
        *,
        publisher: GitPublisher | None = None,
        client: GitHubClient | None = None,
        token: str | None = None,
        runner: CommandRunner = default_command_runner,
    ) -> None:
        self._config = config
        self._runner = runner
        resolved_token = token if token is not None else resolve_github_token()
        self._publisher = publisher
        self._client = (
            client if client is not None else GitHubClient(runner=runner, token=resolved_token)
        )

    def resolve_base_branch(self, source_repo: Path) -> str:
        """Configured base branch, else the source repository's current branch."""
        configured = self._config.pull_request.base_branch
        if configured:
            return configured
        result = self._runner(["git", "-C", str(source_repo), "rev-parse", "--abbrev-ref", "HEAD"])
        branch = (result.stdout or "").strip()
        if result.returncode != 0 or not branch or branch == "HEAD":
            return DEFAULT_BASE_BRANCH
        return branch

    def _git_publisher(self, base_branch: str) -> GitPublisher:
        if self._publisher is not None:
            # Keep an injected publisher authoritative for the safety knobs a
            # test/integrator configured, but never let it publish against a
            # base branch the controller did not resolve.
            return self._publisher
        pull_request = self._config.pull_request
        return GitPublisher(
            runner=self._runner,
            remote=pull_request.remote,
            branch_prefix=self._config.repository.branch_prefix,
            base_branch=base_branch,
            max_changed_files=self._config.repository.max_changed_files,
            allowed_hosts=frozenset(pull_request.allowed_hosts),
        )

    def publish(
        self,
        *,
        workspace_path: Path,
        branch_name: str,
        base_branch: str,
        commit_message: str,
        title: str,
        body: str,
        existing_pull_request_url: str | None = None,
    ) -> PublishResult:
        """Commit + push ``branch_name``; open a PR unless one already exists.

        Re-publishing (a CI repair cycle) pushes an additional normal commit
        onto the same branch, which updates the existing PR. No force push, no
        history rewrite, no merge.
        """
        publisher = self._git_publisher(base_branch)
        commit_sha = publisher.commit_and_push(workspace_path, branch_name, commit_message)

        if existing_pull_request_url is not None:
            return PublishResult(
                commit_sha=commit_sha,
                base_branch=base_branch,
                pull_request_url=existing_pull_request_url,
                created_pull_request=False,
            )

        url = self._client.create_pr(
            workspace_path,
            base=base_branch,
            head=branch_name,
            title=title,
            body=body,
            draft=self._config.pull_request.draft,
        )
        return PublishResult(
            commit_sha=commit_sha,
            base_branch=base_branch,
            pull_request_url=url,
            created_pull_request=True,
        )


class CIObserver:
    """Bounded GitHub Actions polling, normalized into persisted evidence."""

    def __init__(
        self,
        config: FactoryConfig,
        *,
        client: GitHubClient | None = None,
        token: str | None = None,
        runner: CommandRunner = default_command_runner,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        resolved_token = token if token is not None else resolve_github_token()
        self._client = (
            client if client is not None else GitHubClient(runner=runner, token=resolved_token)
        )
        self._sleep = sleep

    @property
    def max_polls(self) -> int:
        ci = self._config.ci
        return max(1, ci.max_wait_seconds // ci.poll_interval_seconds)

    def observe(
        self,
        *,
        repo_path: Path,
        pull_request_url: str,
        repair_attempts_used: int = 0,
    ) -> CIReport:
        """Poll until checks settle or the configured budget is spent."""
        ci = self._config.ci
        try:
            if self._sleep is None:
                status = self._client.poll_checks(
                    repo_path,
                    pull_request_url,
                    interval_seconds=float(ci.poll_interval_seconds),
                    max_polls=self.max_polls,
                    max_seconds=float(ci.max_wait_seconds),
                )
            else:
                status = self._client.poll_checks(
                    repo_path,
                    pull_request_url,
                    interval_seconds=float(ci.poll_interval_seconds),
                    max_polls=self.max_polls,
                    max_seconds=float(ci.max_wait_seconds),
                    sleep=self._sleep,
                )
        except CIPollTimeoutError as exc:
            last = exc.last_status
            return normalize_ci_status(
                last if last is not None else CIStatus(overall=CheckStatus.PENDING),
                repair_attempts_used=repair_attempts_used,
                timed_out=True,
            )
        return normalize_ci_status(status, repair_attempts_used=repair_attempts_used)


def normalize_ci_status(
    status: CIStatus,
    *,
    repair_attempts_used: int = 0,
    timed_out: bool = False,
) -> CIReport:
    """Convert adapter-level ``CIStatus`` into persisted domain evidence."""
    return CIReport(
        overall=status.overall.value,
        checks=[
            CICheckEvidence(
                name=check.name,
                status=check.status.value,
                description=check.description,
                details_url=check.details_url,
                failure_category=(
                    check.failure_category.value if check.failure_category is not None else None
                ),
                log_excerpt=check.log_excerpt,
            )
            for check in status.checks
        ],
        repair_attempts_used=repair_attempts_used,
        timed_out=timed_out,
    )
