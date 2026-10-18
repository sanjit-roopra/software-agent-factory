"""Controller-owned git commit and push (PLAN.md Phase 10).

Push safety guards (``GitPublisher``):

- The target branch must start with a configured prefix and must not equal
  the base branch; unsafe or malformed branch names are rejected before any
  subprocess runs (see ``_validate_branch_name``).
- Push always uses an explicit ``HEAD:refs/heads/<branch>`` refspec with no
  ``--force``/``-f`` flag; ``git merge`` is never invoked.
- The remote's host must be in ``allowed_hosts`` (default ``{"github.com"}``);
  the remote's URL is read via the sole permitted, read-only
  ``git remote get-url <remote>`` call, parsed as either an HTTPS or an
  SSH/scp-style URL, and checked before any commit is created. Anything
  outside the allowlist raises :class:`UnsafeRemoteError`.
- ``git config`` is never invoked, and no ``git remote`` subcommand other
  than ``get-url`` is permitted -- this adapter must not mutate the
  repository's remotes or configuration. ``_run_git`` refuses those
  subcommands defensively even though nothing here constructs them.
- Files matching a protected-secret glob (``.env*``, ``*.pem``, ``id_*``,
  ``*.p12``, ``credentials*``) are never committed; staging such a file
  aborts the commit with :class:`ProtectedFileError`.
- An excessive number of changed files (beyond ``max_changed_files``) aborts
  the commit with :class:`ExcessiveChangeScopeError` rather than silently
  publishing a huge, unreviewed diff.

Git evidence (``changed_files``/diff) is derived by ``workspace.py`` from
``git diff --cached`` against the workspace's recorded base commit. Once
``GitPublisher`` commits, the index is no longer "ahead of base" in the same
way, so any evidence collected *after* a commit must be recomputed relative
to the base commit (e.g. ``git diff <base_commit>..HEAD``) rather than
``--cached``; that recomputation is workspace.py's responsibility, not this
module's.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

from .git_safety import (
    GitCommandError,
    GitPublishError,
    GitTimeoutError,
    NoChangesToCommitError,
    UnauthorizedHistoryError,
    UnexpectedRepositoryError,
    UnreviewedContentError,
    UnsafeRemoteError,
    _build_commit_message,
    _is_transient_git_push_error,
    _parse_raw_diff_changed_files,
    _validate_branch_name,
    _validate_change_scope,
)
from .github import (
    SHA_PATTERN,
    CommandRunner,
    RepositoryRef,
    _extract_remote_host,
    default_command_runner,
    parse_remote_repository,
    parse_remote_repository_for_api,
)

COPILOT_CO_AUTHOR_TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
MAX_GIT_PUSH_ATTEMPTS = 2
GIT_PUSH_RETRY_DELAY_SECONDS = 2.0
GIT_PUSH_FINAL_RECONCILE_DELAY_SECONDS = 5.0

# git subcommands this adapter must never invoke: it may commit and push a
# controller-owned branch, but it must never mutate the repository's own
# configuration or remote definitions. ``git remote get-url`` is the sole
# permitted ``remote`` invocation -- it only reads the existing remote URL
# so the host allowlist can be enforced; add/set-url/remove/rename etc. stay
# forbidden.
_FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"config"})
_ALLOWED_REMOTE_SUBCOMMANDS = frozenset({"get-url"})

# Default host allowlist for GitPublisher.allowed_hosts.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({"github.com"})

DEFAULT_MAX_CHANGED_FILES = 200


@dataclass(frozen=True)
class RemoteTarget:
    """A validated remote identity together with the exact URL it came from.

    Writes use ``url`` rather than the remote *alias*: an alias is mutable
    local configuration, so re-resolving it at push time would let a
    concurrent ``git remote set-url`` redirect an authorized push to another
    repository after the identity check passed.
    """

    url: str
    reference: RepositoryRef


@dataclass
class GitPublisher:
    """Controller-owned commit/push adapter.

    Never force-pushes and never merges: the push arguments are built
    entirely by this class (never accepting caller-supplied flags), and no
    method here ever invokes ``git merge``/``git push --force``. It also
    never invokes ``git config`` and never mutates remotes -- the only
    permitted ``git remote`` invocation is the read-only
    ``git remote get-url`` used to enforce ``allowed_hosts`` (see
    ``_FORBIDDEN_GIT_SUBCOMMANDS``/``_ALLOWED_REMOTE_SUBCOMMANDS``). It
    never commits a file matching ``PROTECTED_FILE_GLOBS``, refuses to
    commit more than ``max_changed_files`` files in one go, and refuses to
    push to a remote whose host is not in ``allowed_hosts``.
    """

    runner: CommandRunner = default_command_runner
    remote: str = "origin"
    co_author_trailer: str = COPILOT_CO_AUTHOR_TRAILER
    branch_prefix: str = "factory/"
    base_branch: str = "main"
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES
    allowed_hosts: frozenset[str] = field(default_factory=lambda: DEFAULT_ALLOWED_HOSTS)
    sleeper: Callable[[float], None] = time.sleep

    def _run_git(
        self, workspace_path: Path, args: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args:
            head = args[0]
            if head in _FORBIDDEN_GIT_SUBCOMMANDS:
                raise GitPublishError(
                    f"refusing to run git {head!r}: this adapter must not mutate "
                    "repository configuration or remotes"
                )
            if head == "remote" and (len(args) < 2 or args[1] not in _ALLOWED_REMOTE_SUBCOMMANDS):
                raise GitPublishError(
                    "refusing to run git remote "
                    f"{' '.join(args[1:]) or '<none>'!r}: only "
                    "'git remote get-url' is permitted (adapter must not mutate remotes)"
                )
        full_args = ["git", "-C", str(workspace_path), *args]
        try:
            result = self.runner(full_args)
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(full_args, exc.timeout) from None
        if check and result.returncode != 0:
            raise GitCommandError(full_args, result.returncode, result.stderr)
        return result

    def _resolve_remote_host(self, workspace_path: Path) -> str:
        """Read-only lookup of ``self.remote``'s configured URL and host,
        via the sole permitted ``git remote`` invocation. Never mutates
        ``.git/config``."""
        result = self._run_git(workspace_path, ["remote", "get-url", self.remote])
        url = result.stdout.strip()
        if not url:
            raise UnsafeRemoteError(f"remote {self.remote!r} has no URL configured")
        try:
            return _extract_remote_host(url)
        except ValueError as exc:
            raise UnsafeRemoteError(
                f"could not determine host for remote {self.remote!r} ({url!r}): {exc}"
            ) from exc

    def _validate_remote_host(self, workspace_path: Path) -> None:
        host = self._resolve_remote_host(workspace_path)
        allowed = {allowed_host.lower() for allowed_host in self.allowed_hosts}
        if host not in allowed:
            raise UnsafeRemoteError(
                f"remote {self.remote!r} host {host!r} is not in the allowed hosts "
                f"{sorted(allowed)}"
            )

    def resolve_remote_target(self, workspace_path: Path) -> RemoteTarget:
        """Read the remote once and return both its URL and its exact identity.

        Callers that go on to write (push) must reuse the returned URL so the
        thing that was validated is the thing that is written to.
        """
        result = self._run_git(workspace_path, ["remote", "get-url", self.remote])
        url = result.stdout.strip()
        if not url:
            raise UnsafeRemoteError(f"remote {self.remote!r} has no URL configured")
        if url.startswith("-"):
            raise UnsafeRemoteError(f"refusing an option-like remote URL {url!r}")
        try:
            reference = parse_remote_repository(url)
            destination = url
        except ValueError as exc:
            parsed = urlparse(url)
            if (
                parsed.scheme.lower() != "https"
                or (parsed.username is None and parsed.password is None)
                or parsed.query
                or parsed.fragment
            ):
                raise UnsafeRemoteError(
                    f"could not determine an exact repository for remote {self.remote!r}: {exc}"
                ) from exc
            try:
                reference = parse_remote_repository_for_api(url)
                port = f":{parsed.port}" if parsed.port is not None else ""
            except (ValueError, TypeError) as credential_error:
                raise UnsafeRemoteError(
                    f"could not determine an exact repository for remote {self.remote!r}"
                ) from credential_error
            destination = f"https://{reference.host}{port}/{reference.owner}/{reference.name}.git"
        allowed = {allowed_host.lower() for allowed_host in self.allowed_hosts}
        if reference.host not in allowed:
            raise UnsafeRemoteError(
                f"remote {self.remote!r} host {reference.host!r} is not in the allowed hosts "
                f"{sorted(allowed)}"
            )
        return RemoteTarget(url=destination, reference=reference)

    def resolve_remote_repository(self, workspace_path: Path) -> RepositoryRef:
        """Read-only, strict ``host`` + ``owner/name`` identity of
        ``self.remote``, enforcing ``allowed_hosts``.

        Stricter than :meth:`_validate_remote_host`: the remote URL must also
        resolve to an unambiguous ``owner/name``. Used by the merge boundary,
        which must know exactly which repository it is about to merge into.
        """
        return self.resolve_remote_target(workspace_path).reference

    def resolve_remote_repository_for_api(self, workspace_path: Path) -> RepositoryRef:
        """Resolve host and ``OWNER/REPO`` for explicit GitHub CLI targeting.

        This keeps credentials out of the returned value while allowing common
        credential-bearing HTTPS remotes in pull-request-only mode.
        """
        result = self._run_git(workspace_path, ["remote", "get-url", self.remote])
        url = result.stdout.strip()
        if not url:
            raise UnsafeRemoteError(f"remote {self.remote!r} has no URL configured")
        if url.startswith("-"):
            raise UnsafeRemoteError(f"refusing an option-like remote URL {url!r}")
        try:
            reference = parse_remote_repository_for_api(url)
        except ValueError as exc:
            raise UnsafeRemoteError(
                f"could not determine a repository for remote {self.remote!r}: {exc}"
            ) from exc
        allowed = {allowed_host.lower() for allowed_host in self.allowed_hosts}
        if reference.host not in allowed:
            raise UnsafeRemoteError(
                f"remote {self.remote!r} host {reference.host!r} is not in the allowed hosts "
                f"{sorted(allowed)}"
            )
        return reference

    def remote_branch_sha(
        self, workspace_path: Path, branch_name: str, *, destination: str | None = None
    ) -> str:
        """Read-only lookup of ``branch_name``'s current remote tip, or ``""``
        when the remote branch does not exist.

        ``destination`` pins the lookup to an already-validated URL so it
        cannot describe a different repository than the following push.
        """
        result = self._run_git(
            workspace_path,
            ["ls-remote", "--heads", destination or self.remote, f"refs/heads/{branch_name}"],
        )
        expected_ref = f"refs/heads/{branch_name}"
        for line in result.stdout.splitlines():
            parts = line.split()
            sha = parts[0] if parts else ""
            remote_ref = parts[1] if len(parts) > 1 else ""
            if remote_ref == expected_ref and SHA_PATTERN.fullmatch(sha):
                return sha
        return ""

    def verify_identity(
        self,
        workspace_path: Path,
        *,
        expected_repository: str | None,
        expected_host: str | None,
    ) -> RemoteTarget:
        """Confirm the remote still resolves to the authorized repository/host.

        Read-only (``git remote get-url``). Rejects a checkout whose remote was
        repointed after the run was authorized, before anything is pushed, and
        returns the exact URL that later writes must be pinned to.
        """
        target = self.resolve_remote_target(workspace_path)
        reference = target.reference
        if (
            expected_repository is not None
            and reference.full_name.casefold() != expected_repository.casefold()
        ):
            raise UnexpectedRepositoryError(
                f"remote repository {reference.full_name!r} is not the authorized delivery "
                f"repository {expected_repository!r}"
            )
        if expected_host is not None and reference.host.casefold() != expected_host.casefold():
            raise UnexpectedRepositoryError(
                f"remote host {reference.host!r} is not the authorized delivery host "
                f"{expected_host!r}"
            )
        return target

    def _verify_tree(
        self,
        workspace_path: Path,
        expected_tree_sha: str | None,
        *,
        args: Sequence[str],
    ) -> None:
        """Compare the workspace tree with the tree authorized after review.

        The extra ``git`` call is only made when a reviewed tree was supplied,
        so publications without delivery binding keep their command shape.
        """
        if expected_tree_sha is None:
            return
        if SHA_PATTERN.fullmatch(expected_tree_sha) is None:
            raise UnreviewedContentError(
                f"expected tree {expected_tree_sha!r} is not a full 40-character object name"
            )
        tree = self._run_git(workspace_path, list(args)).stdout.strip()
        if tree != expected_tree_sha:
            raise UnreviewedContentError(
                f"workspace tree {tree or 'unknown'!r} is not the reviewed tree "
                f"{expected_tree_sha!r}; refusing to publish unreviewed content"
            )

    def ensure_pushed(
        self,
        workspace_path: Path,
        branch_name: str,
        *,
        expected_tree_sha: str | None = None,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> str:
        """Recovery path for a crash between commit/push and persistence.

        Publishes the *existing* ``HEAD`` commit without creating a new one:
        used when there is nothing left to commit because a previous attempt
        already committed (and possibly pushed) the same work. Never rewrites
        history and never force-pushes -- if the remote branch has diverged,
        the plain push fails and the error is surfaced.
        """
        _validate_branch_name(
            branch_name, branch_prefix=self.branch_prefix, base_branch=self.base_branch
        )
        destination = self._push_destination(
            workspace_path,
            expected_repository=expected_repository,
            expected_host=expected_host,
        )
        head = self._run_git(workspace_path, ["rev-parse", "HEAD"]).stdout.strip()
        if SHA_PATTERN.fullmatch(head) is None:
            raise GitPublishError(f"could not resolve HEAD in {workspace_path}")
        self._verify_tree(workspace_path, expected_tree_sha, args=["rev-parse", f"{head}^{{tree}}"])
        if self.remote_branch_sha(workspace_path, branch_name, destination=destination) == head:
            return head
        self._push_ref(
            workspace_path,
            destination=destination,
            branch_name=branch_name,
            commit_sha=head,
        )
        return head

    def publish_bound_commit(
        self,
        workspace_path: Path,
        branch_name: str,
        message: str,
        *,
        expected_tree_sha: str,
        expected_parent_sha: str,
        record_commit: Callable[[str], None],
        prepared_commit_sha: str | None = None,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> str:
        """Publish exactly the approved tree on exactly the approved parent.

        The commit object is built with ``git commit-tree`` from the immutable
        reviewed tree and the approved parent, so its identity never depends on
        the mutable index or on whatever ``HEAD`` happens to be. The resulting
        SHA is handed to ``record_commit`` -- which persists it -- *before* the
        branch is advanced and before anything is pushed, so a crash can always
        be resolved to one commit rather than guessed from the worktree.

        The branch is advanced with a compare-and-swap ``git update-ref`` from
        the approved parent, and only that exact commit is pushed. Any commit
        the controller did not create (including one with an identical tree)
        leaves ``HEAD`` off the approved parent and blocks publication.

        ``prepared_commit_sha`` resumes a publication whose receipt was already
        persisted: the recorded commit is re-verified against the same tree and
        parent, and ``HEAD`` may only be the approved parent (crash before the
        ref moved) or the recorded commit itself (crash after).
        """
        _validate_branch_name(
            branch_name, branch_prefix=self.branch_prefix, base_branch=self.base_branch
        )
        full_message = _build_commit_message(message, self.co_author_trailer)
        tree = self._require_object_name(expected_tree_sha, "reviewed tree")
        parent = self._require_object_name(expected_parent_sha, "expected parent commit")
        prepared = (
            self._require_object_name(prepared_commit_sha, "recorded publication commit")
            if prepared_commit_sha is not None
            else None
        )

        destination = self._push_destination(
            workspace_path,
            expected_repository=expected_repository,
            expected_host=expected_host,
        )

        branch = self._run_git(
            workspace_path, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False
        ).stdout.strip()
        if branch != branch_name:
            raise UnauthorizedHistoryError(
                f"workspace is on {branch or 'a detached HEAD'!r}, not the publication branch "
                f"{branch_name!r}"
            )
        head = self._run_git(workspace_path, ["rev-parse", "HEAD"]).stdout.strip()

        if prepared is None:
            if head != parent:
                raise UnauthorizedHistoryError(
                    f"HEAD is {head or 'unknown'!r}, not the approved parent commit {parent!r}: "
                    "the branch carries a commit the controller did not record"
                )
            raw_diff = self._run_git(
                workspace_path,
                ["diff", "--raw", "-z", "--find-copies=1%", "--find-copies-harder", parent, tree],
            ).stdout
            changed_files = _parse_raw_diff_changed_files(raw_diff)
            if not changed_files:
                raise UnauthorizedHistoryError(
                    f"the reviewed tree {tree!r} is identical to the approved parent {parent!r}: "
                    "there is nothing to publish"
                )
            # The same protected-path and change-scope rules as an ordinary
            # publication, applied to the reviewed tree itself.
            _validate_change_scope(changed_files, max_changed_files=self.max_changed_files)
            commit = self._run_git(
                workspace_path, ["commit-tree", tree, "-p", parent, "-m", full_message]
            ).stdout.strip()
            commit = self._require_object_name(commit, "new publication commit")
            self._verify_commit_shape(workspace_path, commit, tree=tree, parent=parent)
            # Durable receipt first: after this returns, the controller knows
            # exactly which commit may be published, even if the next call
            # never happens.
            record_commit(commit)
        else:
            commit = prepared
            self._verify_commit_shape(workspace_path, commit, tree=tree, parent=parent)
            if head not in {parent, commit}:
                raise UnauthorizedHistoryError(
                    f"HEAD is {head or 'unknown'!r}, which is neither the approved parent "
                    f"{parent!r} nor the recorded publication commit {commit!r}"
                )

        if head != commit:
            self._run_git(
                workspace_path,
                ["update-ref", f"refs/heads/{branch_name}", commit, parent],
            )

        if self.remote_branch_sha(workspace_path, branch_name, destination=destination) != commit:
            self._push_ref(
                workspace_path,
                destination=destination,
                branch_name=branch_name,
                commit_sha=commit,
            )
        return commit

    @staticmethod
    def _require_object_name(value: str | None, label: str) -> str:
        name = (value or "").strip().lower()
        if SHA_PATTERN.fullmatch(name) is None:
            raise GitPublishError(f"{label} {value!r} is not a full 40-character object name")
        return name

    def _verify_commit_shape(
        self, workspace_path: Path, commit: str, *, tree: str, parent: str
    ) -> None:
        """Confirm a commit object really is the approved tree on the approved
        parent, with no second parent (no merge, no grafted history)."""
        actual_tree = self._run_git(
            workspace_path, ["rev-parse", "--verify", f"{commit}^{{tree}}"]
        ).stdout.strip()
        if actual_tree != tree:
            raise UnreviewedContentError(
                f"commit {commit!r} carries tree {actual_tree or 'unknown'!r}, not the reviewed "
                f"tree {tree!r}"
            )
        lineage = self._run_git(
            workspace_path, ["rev-list", "--parents", "-n", "1", commit]
        ).stdout.split()
        if lineage[:1] != [commit] or lineage[1:] != [parent]:
            raise UnauthorizedHistoryError(
                f"commit {commit!r} does not have exactly one parent {parent!r} "
                f"(got {' '.join(lineage[1:]) or 'none'})"
            )

    def _push_destination(
        self,
        workspace_path: Path,
        *,
        expected_repository: str | None,
        expected_host: str | None,
    ) -> str:
        """Resolve where a push may go, pinning the *URL* when the run carries
        an authorized delivery identity.

        Without a pinned URL the push would name a remote alias, which git
        re-resolves at push time: an alias rewritten after validation would
        redirect the push. With it, the validated URL is written to directly.
        """
        if expected_repository is None and expected_host is None:
            self._validate_remote_host(workspace_path)
            return self.remote
        return self.verify_identity(
            workspace_path,
            expected_repository=expected_repository,
            expected_host=expected_host,
        ).url

    def _push_ref(
        self,
        workspace_path: Path,
        *,
        destination: str,
        branch_name: str,
        commit_sha: str,
    ) -> None:
        """Push one exact commit with one bounded transient retry.

        A failed push may still have updated the remote before its response was
        lost. Reconcile the exact branch tip before every retry and after the
        final failure; only the expected commit is accepted as success.
        """
        push_args = ["push", "--", destination, f"{commit_sha}:refs/heads/{branch_name}"]
        for attempt in range(MAX_GIT_PUSH_ATTEMPTS):
            try:
                self._run_git(workspace_path, push_args)
                return
            except (GitCommandError, GitTimeoutError) as exc:
                if not _is_transient_git_push_error(exc):
                    raise
                final_attempt = attempt + 1 == MAX_GIT_PUSH_ATTEMPTS
                if self._push_landed_after_failure(
                    workspace_path,
                    destination=destination,
                    branch_name=branch_name,
                    commit_sha=commit_sha,
                    final_attempt=final_attempt,
                ):
                    return
                if final_attempt:
                    raise

    def _push_landed_after_failure(
        self,
        workspace_path: Path,
        *,
        destination: str,
        branch_name: str,
        commit_sha: str,
        final_attempt: bool,
    ) -> bool:
        """Check the remote tip, wait, and check again after a failed push."""
        if self._remote_tip_is(workspace_path, destination, branch_name, commit_sha):
            return True
        self.sleeper(
            GIT_PUSH_FINAL_RECONCILE_DELAY_SECONDS
            if final_attempt
            else GIT_PUSH_RETRY_DELAY_SECONDS
        )
        return self._remote_tip_is(workspace_path, destination, branch_name, commit_sha)

    def _remote_tip_is(
        self, workspace_path: Path, destination: str, branch_name: str, commit_sha: str
    ) -> bool:
        try:
            remote_sha = self.remote_branch_sha(
                workspace_path,
                branch_name,
                destination=destination,
            )
        except (GitCommandError, GitTimeoutError):
            remote_sha = ""
        return remote_sha == commit_sha

    def commit_and_push(
        self,
        workspace_path: Path,
        branch_name: str,
        message: str,
        *,
        expected_tree_sha: str | None = None,
        expected_repository: str | None = None,
        expected_host: str | None = None,
    ) -> str:
        """Stage, commit with the Copilot co-author trailer, and push
        ``branch_name`` to ``self.remote``. Returns the new commit SHA.

        Raises :class:`UnsafeBranchNameError` if ``branch_name`` does not
        start with ``self.branch_prefix`` or equals ``self.base_branch``;
        :class:`UnsafeRemoteError` if ``self.remote``'s URL cannot be
        resolved/parsed or its host is not in ``self.allowed_hosts``;
        :class:`NoChangesToCommitError` if there is nothing staged or
        changed after staging; :class:`ProtectedFileError` if a staged file
        matches ``PROTECTED_FILE_GLOBS``; and
        :class:`ExcessiveChangeScopeError` if more than
        ``self.max_changed_files`` files changed. All of these are checked
        before any commit is created.
        """
        _validate_branch_name(
            branch_name, branch_prefix=self.branch_prefix, base_branch=self.base_branch
        )
        # Pure/local validations happen before any subprocess call so a bad
        # branch name or empty message never even reads the remote.
        full_message = _build_commit_message(message, self.co_author_trailer)
        destination = self._push_destination(
            workspace_path,
            expected_repository=expected_repository,
            expected_host=expected_host,
        )

        self._run_git(workspace_path, ["add", "-A"])
        staged = self._run_git(
            workspace_path,
            ["diff", "--cached", "--raw", "-z", "--find-copies=1%", "--find-copies-harder"],
        )
        changed_files = _parse_raw_diff_changed_files(staged.stdout)
        if not changed_files:
            raise NoChangesToCommitError(
                f"no staged or working changes to commit in {workspace_path}"
            )
        _validate_change_scope(changed_files, max_changed_files=self.max_changed_files)
        self._verify_tree(workspace_path, expected_tree_sha, args=["write-tree"])

        self._run_git(workspace_path, ["commit", "-m", full_message])
        sha = self._run_git(workspace_path, ["rev-parse", "HEAD"]).stdout.strip()
        if not sha:
            raise GitPublishError(f"could not resolve the new commit in {workspace_path}")
        # The commit itself is re-verified: staging was checked before the
        # commit, and nothing is re-staged in between, so a commit whose tree
        # is not the reviewed tree can never be pushed.
        self._verify_tree(workspace_path, expected_tree_sha, args=["rev-parse", f"{sha}^{{tree}}"])
        # Explicit refspec by SHA, no --force: pushes exactly this commit onto
        # the controller-owned branch and nothing else.
        self._push_ref(
            workspace_path,
            destination=destination,
            branch_name=branch_name,
            commit_sha=sha,
        )
        return sha
