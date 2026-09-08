"""Controller-owned, fail-closed pull request merging (ADR-022).

The user explicitly authorized autonomous merging of reviewed, CI-green work
into a configured target branch. This module is the *only* place that may ask
GitHub to merge, and it is disabled unless ``merge.enabled`` is configured.

Everything here is deliberately deterministic and conservative:

- **Exact identity.** The pull request URL's host must be in
  ``pull_request.allowed_hosts``, its ``OWNER/REPO`` must be in
  ``merge.allowed_repositories``, and it must be the *same* repository the
  local remote points at. A fork head (``isCrossRepository``, or a head
  repository different from the base repository) is never merged.
- **Exact revision.** The head must equal the reviewed, published commit the
  controller passes in, and the head branch must carry the factory branch
  prefix. The merge request itself carries ``--match-head-commit`` so GitHub
  refuses the merge if the head moved in between.
- **Fresh, head-bound evidence.** Check state is read from the pull request's
  ``statusCheckRollup`` (which GitHub attaches to the current head), and the
  head is re-read immediately before merging. An earlier ``CIReport`` from the
  workflow's polling loop is never sufficient on its own.
- **Required means required.** Every configured ``merge.required_checks`` entry
  must be *present* and ``PASS``. Missing, skipped, neutral, cancelled or
  pending required checks cannot authorize a merge; any other failing, pending
  or cancelled check also fails closed.
- **Server-enforced gates.** Reading a green rollup is not enough: a required
  check can be re-run, or a new commit can land, between our read and the
  merge. Before merging, the base branch's *server* policy is read read-only
  and must itself require every configured check, require a pull request, and
  require the branch to be up to date (strict). If the policy is unreadable or
  does not cover them, the merge fails closed.
- **Never a bypass, never deferred.** The merge is a synchronous REST
  ``PUT .../pulls/{n}/merge`` with the configured method: never ``--admin``,
  never a protection override, never a direct push to the base branch, and
  never ``gh pr merge``, which may enable auto-merge or enqueue a merge queue
  entry that completes long after the controller stopped watching. GitHub's own
  branch protection and repository rules still apply and may still refuse.
- **Confirmed outcome.** ``DONE`` requires reading back a genuinely merged pull
  request and its actual merge commit. A merge whose outcome cannot be
  determined raises :class:`~software_agent_factory.github.UnknownMergeOutcomeError`
  rather than being reported as success. An already-merged pull request that
  matches this run exactly is an idempotent success, so a network or process
  interruption after the merge request can be recovered without merging twice.

Bounded work: one merge request and at most three read-only pull request
queries. There is no retry loop here -- a blocked, outdated or conflicting pull
request fails with an explicit reason and the workflow controller decides what
happens next. Merge queues are deliberately not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import FactoryConfig
from .github import (
    SHA_PATTERN,
    BranchPolicy,
    CheckResult,
    CheckStatus,
    CommandRunner,
    GitHubClient,
    GitHubError,
    GitPublisher,
    MergeNotAllowedError,
    PullRequestState,
    RepositoryRef,
    UnknownMergeOutcomeError,
    default_command_runner,
    is_safe_ref_name,
    parse_pull_request_url,
    resolve_github_token,
)

#: ``mergeStateStatus`` values that permit a normal, unattended merge. Anything
#: else (``BLOCKED``, ``BEHIND``, ``DIRTY``, ``DRAFT``, ``UNSTABLE``,
#: ``UNKNOWN``) fails closed with an explicit reason.
MERGEABLE_STATE_STATUSES: frozenset[str] = frozenset({"CLEAN", "HAS_HOOKS"})


@dataclass(frozen=True)
class MergeResult:
    """Confirmed evidence that a pull request actually merged."""

    commit_sha: str
    pull_request_url: str


def _describe_check(check: CheckResult) -> str:
    return f"{check.name}={check.status.value}"


class PullRequestMerger:
    """Verifies and merges one reviewed, CI-green pull request."""

    def __init__(
        self,
        config: FactoryConfig,
        *,
        client: GitHubClient | None = None,
        publisher: GitPublisher | None = None,
        token: str | None = None,
        runner: CommandRunner = default_command_runner,
    ) -> None:
        self._config = config
        self._runner = runner
        resolved_token = token if token is not None else resolve_github_token()
        self._client = (
            client if client is not None else GitHubClient(runner=runner, token=resolved_token)
        )
        self._publisher = publisher or GitPublisher(
            runner=runner,
            remote=config.pull_request.remote,
            branch_prefix=config.repository.branch_prefix,
            base_branch=config.pull_request.base_branch or "main",
            max_changed_files=config.repository.max_changed_files,
            allowed_hosts=frozenset(config.pull_request.allowed_hosts),
        )

    # -- repository authorization ---------------------------------------

    def validate_repository(self, repo_path: Path) -> str:
        """Resolve and authorize the exact repository behind ``repo_path``.

        Returns the canonical ``OWNER/REPO`` entry from
        ``merge.allowed_repositories``. Raises
        :class:`~software_agent_factory.github.UnsafeRemoteError` when the
        remote host is not allowed or the remote is not an unambiguous
        repository, and :class:`MergeNotAllowedError` when the repository is
        not on the allowlist.
        """
        reference = self._publisher.resolve_remote_repository(repo_path)
        return self._authorized_repository(reference)

    def _authorized_repository(self, reference: RepositoryRef) -> str:
        allowed_hosts = {host.casefold() for host in self._config.pull_request.allowed_hosts}
        if reference.host.casefold() not in allowed_hosts:
            raise MergeNotAllowedError(
                f"host {reference.host!r} is not in pull_request.allowed_hosts "
                f"{sorted(allowed_hosts)}"
            )
        for entry in self._config.merge.allowed_repositories:
            if entry.casefold() == reference.full_name.casefold():
                return entry
        raise MergeNotAllowedError(
            f"repository {reference.full_name!r} is not in merge.allowed_repositories"
        )

    # -- merging ---------------------------------------------------------

    def merge(
        self,
        *,
        repo_path: Path,
        pull_request_url: str,
        expected_head_sha: str,
        base_branch: str,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> MergeResult:
        """Verify every merge precondition, then request a normal merge and
        confirm the resulting merge commit.

        ``expected_repository``/``expected_host``, when supplied, bind the merge
        to the exact repository identity the run was authorized against at
        dispatch time, on top of the configured allowlists: the pull request
        URL, the local remote and the persisted authorization must all agree.
        """
        merge_config = self._config.merge
        if not merge_config.enabled:
            raise MergeNotAllowedError("merging is disabled (merge.enabled is false)")

        head_sha = (expected_head_sha or "").strip().lower()
        if SHA_PATTERN.fullmatch(head_sha) is None:
            raise MergeNotAllowedError("expected_head_sha must be a full 40-character commit SHA")
        target = (base_branch or "").strip()
        configured_base = (self._config.pull_request.base_branch or "").strip()
        if not target:
            raise MergeNotAllowedError("an explicit base branch is required to merge")
        if not is_safe_ref_name(target):
            raise MergeNotAllowedError(f"base branch {target!r} is not a safe branch name")
        if configured_base != target:
            raise MergeNotAllowedError(
                f"base branch {target!r} does not match the configured "
                f"pull_request.base_branch {configured_base!r}"
            )

        repository = self._resolve_identity(
            repo_path,
            pull_request_url,
            expected_repository=expected_repository,
            expected_host=expected_host,
        )

        reference, number = parse_pull_request_url(pull_request_url)

        # First read: full eligibility, including head-bound check evidence.
        state = self._read_pull_request(repo_path, pull_request_url, repository)
        self._validate_identity(state, repository, pull_request_url)
        if state.merged:
            # Recovery after an interruption: this exact pull request already
            # merged at the reviewed head, so report the recorded merge commit
            # instead of merging anything again.
            return self._confirm_merged(
                state, head_sha, target, pull_request_url, require_check_evidence=True
            )
        self._validate_mergeable(state, head_sha, target)

        # The gate that actually matters: GitHub itself must be enforcing the
        # required checks on this base branch, atomically with the merge.
        self._validate_server_enforcement(repo_path, repository, target, hostname=reference.host)

        # Second read immediately before merging: proves the checks inspected
        # above still describe the current head, never a stale revision.
        recheck = self._read_pull_request(repo_path, pull_request_url, repository)
        self._validate_identity(recheck, repository, pull_request_url)
        if recheck.merged:
            return self._confirm_merged(
                recheck, head_sha, target, pull_request_url, require_check_evidence=True
            )
        self._validate_mergeable(recheck, head_sha, target)

        outcome = self._client.merge_pull_request(
            repo_path,
            repository=repository,
            number=number,
            method=merge_config.method,
            expected_head_sha=head_sha,
            hostname=reference.host,
        )
        if not outcome.merged and outcome.queue_requested:
            raise MergeNotAllowedError(
                f"GitHub did not merge {pull_request_url} synchronously; it requires a merge "
                f"queue or deferred merge, which this factory does not use: {outcome.message}"
            )

        try:
            final = self._read_pull_request(repo_path, pull_request_url, repository)
        except (GitHubError, OSError) as exc:
            raise UnknownMergeOutcomeError(
                f"merge of {pull_request_url} was requested but its outcome could not be "
                f"confirmed: {exc}"
            ) from exc

        if final.merged:
            self._validate_identity(final, repository, pull_request_url)
            result = self._confirm_merged(
                final, head_sha, target, pull_request_url, require_check_evidence=False
            )
            if outcome.merged and outcome.commit_sha and outcome.commit_sha != result.commit_sha:
                raise UnknownMergeOutcomeError(
                    f"merge of {pull_request_url} produced commit {outcome.commit_sha!r} but the "
                    f"pull request reports {result.commit_sha!r}"
                )
            return result
        if outcome.returncode != 0 or not outcome.merged:
            raise MergeNotAllowedError(
                f"GitHub refused to merge {pull_request_url}: {outcome.message or 'no reason'}"
            )
        raise UnknownMergeOutcomeError(
            f"merge of {pull_request_url} was requested but the pull request is still "
            f"{final.state or 'UNKNOWN'}"
        )

    # -- validation helpers ----------------------------------------------

    def _read_pull_request(
        self, repo_path: Path, pull_request_url: str, repository: str
    ) -> PullRequestState:
        """Read the pull request naming the repository explicitly, so the
        answer can never come from whatever repository the working directory
        happens to point at."""
        _, number = parse_pull_request_url(pull_request_url)
        return self._client.get_pull_request(repo_path, str(number), repository=repository)

    def _validate_server_enforcement(
        self, repo_path: Path, repository: str, base_branch: str, *, hostname: str
    ) -> None:
        """Require GitHub itself to enforce this run's merge preconditions.

        The controller's own check inspection is inherently racy: a required
        workflow can be re-run, expire or start again between the last read and
        the merge request. Only the server can refuse a merge atomically, so a
        merge is allowed only when the target branch's policy already requires
        every configured check, requires a pull request, and requires the
        branch to be up to date with its base.
        """
        try:
            policy: BranchPolicy = self._client.get_branch_policy(
                repo_path, repository=repository, branch=base_branch, hostname=hostname
            )
        except GitHubError as exc:
            raise MergeNotAllowedError(
                f"refusing to merge into {repository}#{base_branch}: its enforced branch policy "
                f"could not be read ({exc})"
            ) from exc

        enforced = policy.required_contexts
        missing = sorted(
            required for required in self._config.merge.required_checks if required not in enforced
        )
        if missing:
            raise MergeNotAllowedError(
                f"refusing to merge into {repository}#{base_branch}: GitHub does not require "
                f"check(s) {', '.join(missing)} there, so a failing or re-run check would not "
                "block the merge server-side"
            )
        if not policy.requires_pull_request:
            raise MergeNotAllowedError(
                f"refusing to merge into {repository}#{base_branch}: GitHub does not require "
                "changes to arrive through a reviewed pull request there"
            )
        if not policy.strict:
            raise MergeNotAllowedError(
                f"refusing to merge into {repository}#{base_branch}: GitHub does not require "
                "branches to be up to date with the base before merging, so checks could have "
                "passed against a stale target"
            )

    def _resolve_identity(
        self,
        repo_path: Path,
        pull_request_url: str,
        *,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> str:
        try:
            reference, _number = parse_pull_request_url(pull_request_url)
        except ValueError as exc:
            raise MergeNotAllowedError(str(exc)) from exc
        active_host = self._client.active_host(repo_path)
        if reference.host.casefold() != active_host.casefold():
            raise MergeNotAllowedError(
                f"pull request host {reference.host!r} is not the active authenticated gh "
                f"host {active_host!r}"
            )
        repository = self._authorized_repository(reference)
        if (
            expected_repository is not None
            and repository.casefold() != expected_repository.casefold()
        ):
            raise MergeNotAllowedError(
                f"pull request repository {repository!r} is not the authorized delivery "
                f"repository {expected_repository!r}"
            )
        local_reference = self._publisher.resolve_remote_repository(repo_path)
        if (
            expected_host is not None
            and local_reference.host.casefold() != expected_host.casefold()
        ):
            raise MergeNotAllowedError(
                f"remote host {local_reference.host!r} is not the authorized delivery host "
                f"{expected_host!r}"
            )
        local = self._authorized_repository(local_reference)
        if local.casefold() != repository.casefold():
            raise MergeNotAllowedError(
                f"pull request repository {repository!r} does not match the local remote "
                f"repository {local!r}"
            )
        return repository

    def _validate_identity(
        self, state: PullRequestState, repository: str, pull_request_url: str
    ) -> None:
        try:
            reference, number = parse_pull_request_url(state.url)
        except ValueError as exc:
            raise MergeNotAllowedError(
                f"could not verify the identity of {pull_request_url}: {exc}"
            ) from exc
        requested_reference, requested_number = parse_pull_request_url(pull_request_url)
        if (
            reference.full_name.casefold() != repository.casefold()
            or not reference.same_repository(requested_reference)
            or number != requested_number
            or (state.number and state.number != requested_number)
        ):
            raise MergeNotAllowedError(
                f"GitHub returned pull request {state.url!r} for {pull_request_url!r}"
            )
        if state.is_cross_repository or state.head_repository.casefold() != repository.casefold():
            raise MergeNotAllowedError(
                f"refusing to merge a pull request whose head repository is "
                f"{state.head_repository or 'unknown'!r} rather than {repository!r}"
            )

    def _validate_mergeable(self, state: PullRequestState, head_sha: str, base_branch: str) -> None:
        if state.state != "OPEN":
            raise MergeNotAllowedError(f"pull request is {state.state or 'UNKNOWN'}, not OPEN")
        if state.is_draft:
            raise MergeNotAllowedError("refusing to merge a draft pull request")
        if not is_safe_ref_name(state.head_ref_name) or not is_safe_ref_name(state.base_ref_name):
            raise MergeNotAllowedError(
                f"refusing a pull request with an unsafe head/base ref "
                f"({state.head_ref_name!r} -> {state.base_ref_name!r})"
            )
        prefix = self._config.repository.branch_prefix
        if not state.head_ref_name.startswith(prefix):
            raise MergeNotAllowedError(
                f"head branch {state.head_ref_name!r} does not start with the factory "
                f"branch prefix {prefix!r}"
            )
        if state.head_ref_oid != head_sha:
            raise MergeNotAllowedError(
                f"head commit {state.head_ref_oid or 'unknown'!r} is not the reviewed commit "
                f"{head_sha!r}"
            )
        if state.base_ref_name != base_branch:
            raise MergeNotAllowedError(
                f"pull request targets {state.base_ref_name!r}, not the intended base branch "
                f"{base_branch!r}"
            )
        if state.review_decision == "CHANGES_REQUESTED":
            raise MergeNotAllowedError("a reviewer requested changes on this pull request")
        if state.review_decision == "REVIEW_REQUIRED":
            raise MergeNotAllowedError(
                "this pull request still requires a review that has not been given; the "
                "factory never merges around an outstanding human approval"
            )
        if state.mergeable != "MERGEABLE":
            raise MergeNotAllowedError(
                f"GitHub reports mergeable={state.mergeable or 'UNKNOWN'!r} "
                "(conflicts or an unresolved merge state)"
            )
        if state.merge_state_status not in MERGEABLE_STATE_STATUSES:
            raise MergeNotAllowedError(
                f"GitHub reports mergeStateStatus="
                f"{state.merge_state_status or 'UNKNOWN'!r}; the pull request is blocked, "
                "outdated or otherwise not cleanly mergeable"
            )
        self._validate_checks(state.checks)

    def _validate_checks(self, checks: list[CheckResult]) -> None:
        self._validate_required_checks(checks, already_merged=False)
        blocking = sorted(
            _describe_check(check)
            for check in checks
            if check.status in {CheckStatus.FAIL, CheckStatus.PENDING, CheckStatus.CANCELLED}
        )
        if blocking:
            raise MergeNotAllowedError(
                f"other check(s) are failing, pending or cancelled: {', '.join(blocking)}"
            )

    def _validate_required_checks(self, checks: list[CheckResult], *, already_merged: bool) -> None:
        """Every configured required check must be present and ``PASS`` on this
        head. Missing, skipped, neutral, cancelled and pending never satisfy a
        required check, whether the pull request is open or already merged."""
        context = "the merged head" if already_merged else "the current head"
        by_name: dict[str, list[CheckResult]] = {}
        for check in checks:
            by_name.setdefault(check.name, []).append(check)

        missing: list[str] = []
        unsatisfied: list[str] = []
        for required in self._config.merge.required_checks:
            matches = by_name.get(required)
            if not matches:
                missing.append(required)
                continue
            if any(match.status is not CheckStatus.PASS for match in matches):
                unsatisfied.extend(_describe_check(match) for match in matches)
        if missing:
            raise MergeNotAllowedError(
                f"required check(s) are not present on {context}: {', '.join(missing)}"
            )
        if unsatisfied:
            raise MergeNotAllowedError(
                f"required check(s) did not pass on {context}: {', '.join(sorted(unsatisfied))}"
            )

    def _confirm_merged(
        self,
        state: PullRequestState,
        head_sha: str,
        base_branch: str,
        pull_request_url: str,
        *,
        require_check_evidence: bool,
    ) -> MergeResult:
        """Accept an already-merged pull request as this run's delivery.

        ``require_check_evidence`` distinguishes the two ways this is reached:

        - ``True`` -- the pull request was *found* merged before this process
          requested anything (recovery after an interruption, or a merge
          performed outside the factory). Nothing in this process verified the
          checks, so the merged head must still carry evidence that every
          configured required check passed. A manual merge that bypassed,
          skipped or never ran a required check is refused with an explicit
          reason rather than being accepted as delivery.
        - ``False`` -- this process performed the full preflight (identity,
          revision, mergeability and required checks, twice, on this exact
          head) and then requested the merge. Re-imposing the check gate on the
          post-merge read would be self-defeating: a rollup entry can legitimately
          be re-run or expire after the merge, and refusing then would leave a
          genuinely merged pull request recorded as a failure.

        Either way the merge commit, head, base and repository identity are
        bound exactly, and an ambiguous response never triggers a fresh merge.
        """
        if state.head_ref_oid != head_sha:
            raise MergeNotAllowedError(
                f"{pull_request_url} is already merged, but at head "
                f"{state.head_ref_oid or 'unknown'!r} rather than the reviewed commit "
                f"{head_sha!r}"
            )
        if not is_safe_ref_name(state.base_ref_name) or state.base_ref_name != base_branch:
            raise MergeNotAllowedError(
                f"{pull_request_url} is already merged into {state.base_ref_name!r} rather "
                f"than the intended base branch {base_branch!r}"
            )
        if SHA_PATTERN.fullmatch(state.merge_commit_sha or "") is None:
            raise UnknownMergeOutcomeError(
                f"{pull_request_url} reports MERGED but no merge commit could be read back"
            )
        if require_check_evidence:
            if not state.checks:
                raise MergeNotAllowedError(
                    f"{pull_request_url} is already merged, but its head {head_sha!r} carries no "
                    "check evidence, so it cannot prove the required checks "
                    f"({', '.join(self._config.merge.required_checks)}) passed"
                )
            self._validate_required_checks(state.checks, already_merged=True)
        return MergeResult(commit_sha=state.merge_commit_sha, pull_request_url=state.url)
