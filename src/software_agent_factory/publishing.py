"""Controller-owned pull request publishing and CI observation.

Thin, configuration-aware composition over :mod:`software_agent_factory.github`
so :class:`~software_agent_factory.workflow.WorkflowController` stays a state
machine rather than a ``git``/``gh`` driver.

Everything here is strictly opt-in: nothing runs unless
``pull_request.enabled`` (and, for CI, ``ci.enabled``) is set in configuration.
The workflow controller is the only caller.

Merging lives in :mod:`software_agent_factory.merging` and is re-exported here
(:class:`PullRequestMerger`, :class:`MergeResult`) so the controller has one
delivery import. It is separately gated by ``merge.enabled``: publishing a pull
request never implies merging it.

Credential boundary (``AGENTS.md`` "Agents do NOT control ... production
credentials"): the GitHub token is read from the *controller's* environment
here and handed to :class:`~software_agent_factory.github.GitHubClient`, which
passes it to ``gh`` through the child environment only. It is never placed on
a command line, never written to an artifact, and never reaches an agent -- the
Copilot runtime independently strips ``GH_TOKEN``/``GITHUB_TOKEN`` from every
agent subprocess environment.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import FactoryConfig
from .github import (
    TOKEN_ENV_VARS,
    CheckStatus,
    CIPollTimeoutError,
    CIStatus,
    CommandRunner,
    GitHubClient,
    GitPublisher,
    GitTimeoutError,
    NoChangesToCommitError,
    PullRequestState,
    UnexpectedRepositoryError,
    default_command_runner,
    parse_pull_request_url,
    resolve_github_token,
)
from .merging import MergeResult, PullRequestMerger
from .models import CICheckEvidence, CIReport

__all__ = [
    "TOKEN_ENV_VARS",
    "CIObserver",
    "MergeResult",
    "PublishResult",
    "PullRequestMerger",
    "PullRequestPublisher",
    "normalize_ci_status",
    "resolve_github_token",
]

DEFAULT_BASE_BRANCH = "main"

#: Stable, run-scoped marker rendered into every factory PR body by
#: :func:`software_agent_factory.github.build_pr_body`. Recovering publishers
#: use it to prove a discovered pull request belongs to *this* run rather than
#: merely sharing a branch name.
_RUN_MARKER_PATTERN = re.compile(r"^Run ID: `([^`]+)`$", re.MULTILINE)


def _run_marker(body: str) -> str | None:
    match = _RUN_MARKER_PATTERN.search(body or "")
    return match.group(1) if match else None


@dataclass(frozen=True)
class PublishResult:
    """What one commit/push (+ optional PR creation/update) produced."""

    commit_sha: str
    base_branch: str
    pull_request_url: str | None
    created_pull_request: bool
    updated_pull_request: bool = False


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
        args = ["git", "-C", str(source_repo), "rev-parse", "--abbrev-ref", "HEAD"]
        try:
            result = self._runner(args)
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(args, exc.timeout) from None
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
        expected_tree_sha: str | None = None,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> PublishResult:
        """Commit + push ``branch_name``; open a PR unless one already exists.

        Re-publishing (a CI repair cycle) pushes an additional normal commit
        onto the same branch, which updates the existing PR. No force push, no
        history rewrite, no merge.

        ``expected_tree_sha`` binds the publication to the exact Git tree the
        independent Reviewer approved, and ``expected_repository``/
        ``expected_host`` bind it to the repository the run was authorized
        against; a mismatch aborts before anything is committed or pushed.

        Both halves are idempotent, so a crash between pushing and persisting
        the result never duplicates or discards work:

        - when there is nothing left to commit because a previous attempt
          already committed the same work, the existing ``HEAD`` is published
          as-is (:meth:`GitPublisher.ensure_pushed`) instead of failing or
          rewriting history;
        - before creating a pull request, an existing open pull request for
          the same repository, head branch and base branch is looked up and
          reused. A discovered pull request is only accepted when it is not a
          fork head and -- when the body carries a run marker -- when it names
          the same run, so an unrelated pull request can never be adopted.
        """
        api_repository = (
            f"{expected_host}/{expected_repository}"
            if expected_host is not None and expected_repository is not None
            else expected_repository
        )
        if existing_pull_request_url is not None and expected_repository is not None:
            try:
                identity, _ = parse_pull_request_url(existing_pull_request_url)
            except ValueError as exc:
                raise UnexpectedRepositoryError("invalid persisted pull request URL") from exc
            if identity.full_name.casefold() != expected_repository.casefold() or (
                expected_host is not None and identity.host.casefold() != expected_host.casefold()
            ):
                raise UnexpectedRepositoryError(
                    "persisted pull request belongs to another repository"
                )
        publisher = self._git_publisher(base_branch)
        try:
            commit_sha = publisher.commit_and_push(
                workspace_path,
                branch_name,
                commit_message,
                expected_tree_sha=expected_tree_sha,
                expected_repository=expected_repository,
                expected_host=expected_host,
            )
        except NoChangesToCommitError:
            commit_sha = publisher.ensure_pushed(
                workspace_path,
                branch_name,
                expected_tree_sha=expected_tree_sha,
                expected_repository=expected_repository,
                expected_host=expected_host,
            )

        if existing_pull_request_url is not None:
            self._refresh_pull_request(
                workspace_path,
                existing_pull_request_url,
                title=title,
                body=body,
                repository=api_repository,
            )
            return PublishResult(
                commit_sha=commit_sha,
                base_branch=base_branch,
                pull_request_url=existing_pull_request_url,
                created_pull_request=False,
                updated_pull_request=True,
            )

        discovered = self._discover_pull_request(
            workspace_path,
            branch_name=branch_name,
            base_branch=base_branch,
            body=body,
            repository=api_repository,
        )
        if discovered is not None:
            self._refresh_pull_request(
                workspace_path, discovered, title=title, body=body, repository=api_repository
            )
            return PublishResult(
                commit_sha=commit_sha,
                base_branch=base_branch,
                pull_request_url=discovered,
                created_pull_request=False,
                updated_pull_request=True,
            )

        url = self._client.create_pr(
            workspace_path,
            base=base_branch,
            head=branch_name,
            title=title,
            body=body,
            draft=self._config.pull_request.draft,
            repository=api_repository,
        )
        return PublishResult(
            commit_sha=commit_sha,
            base_branch=base_branch,
            pull_request_url=url,
            created_pull_request=True,
        )

    def _refresh_pull_request(
        self,
        workspace_path: Path,
        pull_request_url: str,
        *,
        title: str,
        body: str,
        repository: str | None = None,
    ) -> None:
        """Republish the description so GitHub shows the *current* revision's
        independent Reviewer outcome.

        Every published revision -- including each CI repair commit -- is
        approved by the independent Reviewer before it is pushed, and the body
        is rebuilt from that run's artifacts. Leaving the original body in
        place would advertise a stale approval for code that has since
        changed, so a failed refresh is an error rather than a silent
        best-effort: the caller (the workflow controller) decides what happens
        next.

        This never submits a GitHub review with the factory's credentials --
        the model Reviewer is evidence in the description, not an approving
        GitHub user, and the repository's own review requirements still apply.
        """
        self._client.update_pr(
            workspace_path, pull_request_url, body=body, title=title, repository=repository
        )

    def _discover_pull_request(
        self,
        workspace_path: Path,
        *,
        branch_name: str,
        base_branch: str,
        body: str,
        repository: str | None = None,
    ) -> str | None:
        """Find an already-open pull request for this exact head/base pair.

        Returns ``None`` when there is no unambiguous match, in which case a
        new pull request is created. Never adopts a fork head, a pull request
        for a different run, or an ambiguous set of candidates.
        """
        candidates = self._client.find_pull_requests(
            workspace_path,
            head=branch_name,
            base=base_branch,
            state="open",
            repository=repository,
        )
        marker = _run_marker(body)
        matches = [
            candidate
            for candidate in candidates
            if self._is_same_pull_request(candidate, branch_name, base_branch, marker)
        ]
        if len(matches) != 1:
            return None
        return matches[0].url or None

    @staticmethod
    def _is_same_pull_request(
        candidate: PullRequestState,
        branch_name: str,
        base_branch: str,
        marker: str | None,
    ) -> bool:
        if candidate.is_cross_repository:
            return False
        if candidate.head_ref_name != branch_name or candidate.base_ref_name != base_branch:
            return False
        if marker is not None and _run_marker(candidate.body) != marker:
            return False
        return bool(candidate.url)


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
