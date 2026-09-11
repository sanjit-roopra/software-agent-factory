"""Controller-side Git and GitHub adapters (PLAN.md Phases 10-11).

Implements the "Git ownership" section of ``docs/architecture.md``:

    Agents edit files. Controller owns: worktree creation, branch creation,
    commit, push, PR creation. Agents must not directly push protected
    branches.

and the non-optional halves of:

- Phase 10 ("Pull request creation"): commit, push, and open a PR with a
  description built from the run's typed artifacts. Never merges.
- Phase 11 ("GitHub Actions observation"): poll PR check status with a
  bounded loop (no webhook), normalize check state, and deterministically
  classify failures.

- ADR-022 ("Opt-in autonomous project delivery"): the low-level pieces the
  controller-owned merge boundary in ``merging.py`` needs -- exact repository
  identity parsing, pull request state (including head-bound
  ``statusCheckRollup`` evidence), idempotent pull request discovery, and a
  *normal* ``gh pr merge`` guarded by ``--match-head-commit``. ``--admin`` and
  any other branch-protection bypass are deliberately unreachable from here,
  and no code path pushes to a base branch.

Phase 12 ("CI repair") is explicitly out of scope here -- this module only
observes and classifies; it does not decide retries or drive a repair loop.
Whether a merge is even attempted is a policy decision owned by configuration
and the workflow controller, never by this module.

PR creation and CI observation are strictly opt-in from the caller's
perspective: nothing in this module runs on its own, and whether the
workflow controller invokes :class:`GitHubClient` at all is governed by
integration configuration owned elsewhere (outside this module's ownership
boundary). This module never decides *whether* to publish -- only *how*, and
only when explicitly asked to.

Every external process is invoked through argument lists (never
``shell=True``) via an injectable ``CommandRunner``, mirroring the
subprocess style already used by ``workspace.py`` and ``verification.py``.
Tests fully fake the runner; no real ``git``/``gh``/network call is made.

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

Credential handling: this module is meant to run only inside the workflow
controller process, never inside an agent sandbox. ``copilot_runtime.py``
already strips ``GH_TOKEN``/``GITHUB_TOKEN``/etc. from Copilot agent
subprocess environments precisely so that only controller-owned code such as
this module ever supplies GitHub credentials to a subprocess. Any token
handed to :class:`GitHubClient` is passed to the ``gh`` subprocess via the
environment only (never as a CLI argument), is excluded from ``repr()``, and
is never interpolated into an exception message or log excerpt -- those are
additionally scrubbed with the same token patterns ``copilot_runtime`` uses.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import quote, urlparse

from pydantic import Field

from .copilot_runtime import TOKEN_PATTERNS
from .models import (
    ExecutionPlan,
    ModelBase,
    ReviewAcceptance,
    ReviewReport,
    Specification,
    TestReport,
    VerificationReport,
    WorkItem,
)

COPILOT_CO_AUTHOR_TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"

DEFAULT_MAX_LOG_CHARS = 4000
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_POLLS = 40
MAX_GIT_PUSH_ATTEMPTS = 2
GIT_PUSH_RETRY_DELAY_SECONDS = 2.0
GIT_PUSH_FINAL_RECONCILE_DELAY_SECONDS = 5.0


# --------------------------------------------------------------------------
# Command execution
# --------------------------------------------------------------------------


class CommandRunner(Protocol):
    """Injectable process runner. Real and fake implementations share this
    shape so tests never spawn a real ``git``/``gh`` process."""

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


#: Wall-clock ceiling for one real ``git``/``gh`` invocation. Every remote call
#: this module makes (push, ``pr view``/``list``/``edit``/``merge``, check
#: polling) is a network operation that can otherwise hang forever on a stalled
#: connection, so the default runner always bounds it. Exceeding the ceiling
#: raises a typed, redacted timeout error rather than blocking a run.
DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 120.0


def default_command_runner(
    args: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argument list; never through a shell, never
    unbounded.

    ``timeout`` defaults to :data:`DEFAULT_COMMAND_TIMEOUT_SECONDS`; exceeding
    it raises ``subprocess.TimeoutExpired``, which the adapters translate into
    :class:`GitTimeoutError` / :class:`GitHubTimeoutError`.
    """
    merged_env = None if env is None else {**os.environ, **env}
    return subprocess.run(
        list(args),
        cwd=cwd,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _redact(text: str) -> str:
    """Defensively scrub anything resembling a GitHub token before it can
    reach an exception message, log excerpt, or return value."""
    redacted = text
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class GitPublishError(Exception):
    """Base error for :class:`GitPublisher` operations."""


class NoChangesToCommitError(GitPublishError):
    """Raised when a workspace has nothing staged or changed to commit."""


class UnsafeBranchNameError(GitPublishError):
    """Raised when the target branch fails a push safety check: it must
    start with the configured prefix, must differ from the base branch, and
    must not look like a flag/force-push attempt."""


class ProtectedFileError(GitPublishError):
    """Raised when staged changes include a file matching a protected
    secret-like glob (``.env*``, ``*.pem``, ``id_*``, ``*.p12``,
    ``credentials*``). The commit is aborted before it is created."""


class ExcessiveChangeScopeError(GitPublishError):
    """Raised when the number of staged/changed files exceeds
    ``GitPublisher.max_changed_files``. The commit is aborted before it is
    created rather than silently publishing an oversized diff."""


class UnauthorizedHistoryError(GitPublishError):
    """Raised when the branch to be published carries history the controller
    never recorded: an extra agent commit, a rewritten parent, or a ``HEAD``
    that is neither the approved parent nor the recorded publication commit.

    A tree comparison alone cannot catch this. An agent can add a secret in one
    commit and remove it in the next, or commit an empty change, and still end
    up with exactly the reviewed tree, so the *parent* and the recorded commit
    identity are bound as well."""


class UnreviewedContentError(GitPublishError):
    """Raised when the workspace content about to be published is not the exact
    tree the controller authorized after independent review. Nothing is committed
    or pushed."""


class UnexpectedRepositoryError(GitPublishError):
    """Raised when the remote identity resolved at publish time is not the
    repository/host the run was authorized against."""


class UnsafeRemoteError(GitPublishError):
    """Raised when ``self.remote`` cannot be resolved to a URL, the URL
    cannot be parsed, or its host is not in ``GitPublisher.allowed_hosts``.
    The commit is aborted before it is created."""


class GitTimeoutError(GitPublishError):
    """Raised when a ``git`` invocation exceeded its wall-clock budget. The
    command arguments are recorded; any output is redacted."""

    def __init__(self, args: Sequence[str], timeout: float | None) -> None:
        self.command_args = tuple(args)
        self.timeout = timeout
        joined = " ".join(str(part) for part in self.command_args)
        super().__init__(f"git command timed out after {timeout}s: {_redact(joined)}")


class GitCommandError(GitPublishError):
    """Raised when an underlying ``git`` invocation fails."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.command_args = tuple(args)
        self.returncode = returncode
        self.stderr = _redact(stderr)
        joined = " ".join(self.command_args)
        super().__init__(f"git {joined} failed with exit code {returncode}: {self.stderr.strip()}")


_TRANSIENT_GIT_PUSH_ERROR_MARKERS = (
    "bad gateway",
    "broken pipe",
    "connection refused",
    "connection reset",
    "connection timed out",
    "fatal error in commit_refs",
    "gateway timeout",
    "internal server error",
    "operation timed out",
    "remote end hung up unexpectedly",
    "rpc failed",
    "service unavailable",
    "temporary failure",
    "tls handshake timeout",
    "unexpected eof",
)

_NON_RETRYABLE_GIT_PUSH_ERROR_MARKERS = (
    "access denied",
    "authentication failed",
    "could not read username",
    "fetch first",
    "non-fast-forward",
    "not authorized",
    "permission to ",
    "pre-receive hook declined",
    "protected branch",
    "repository not found",
    "repository rule violation",
)

_GIT_PUSH_HTTP_STATUS_PATTERN = re.compile(r"(?:rpc failed;\s*http|returned error:)\s+(4\d{2})\b")


def _is_transient_git_push_error(error: GitCommandError | GitTimeoutError) -> bool:
    if isinstance(error, GitTimeoutError):
        return True
    stderr = error.stderr.casefold()
    http_status = _GIT_PUSH_HTTP_STATUS_PATTERN.search(stderr)
    if http_status is not None and int(http_status.group(1)) not in {408, 429}:
        return False
    if any(marker in stderr for marker in _NON_RETRYABLE_GIT_PUSH_ERROR_MARKERS):
        return False
    return any(marker in stderr for marker in _TRANSIENT_GIT_PUSH_ERROR_MARKERS)


class GitHubError(Exception):
    """Base error for :class:`GitHubClient` operations."""


class GitHubCommandError(GitHubError):
    """Raised when an underlying ``gh`` invocation fails or its output
    cannot be parsed."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.command_args = tuple(args)
        self.returncode = returncode
        self.stderr = _redact(stderr)
        joined = " ".join(str(part) for part in self.command_args)
        super().__init__(f"gh {joined} failed with exit code {returncode}: {self.stderr.strip()}")


class GitHubTimeoutError(GitHubError):
    """Raised when a ``gh`` invocation exceeded its wall-clock budget. The
    command arguments are recorded; any output is redacted."""

    def __init__(self, args: Sequence[str], timeout: float | None) -> None:
        self.command_args = tuple(args)
        self.timeout = timeout
        joined = " ".join(str(part) for part in self.command_args)
        super().__init__(f"gh command timed out after {timeout}s: {_redact(joined)}")


class CIPollTimeoutError(GitHubError):
    """Raised when checks are still pending after the poll budget is spent."""

    def __init__(self, message: str, last_status: "CIStatus | None" = None) -> None:
        self.last_status = last_status
        super().__init__(message)


class MergeNotAllowedError(GitHubError):
    """Raised when a pull request is not eligible for a controller-owned
    merge: wrong repository/host, a fork head, an unexpected head revision or
    base branch, a draft, a missing/failed/pending required check, a
    conflicting or outdated base, a rejected review, or any other state the
    merge policy fails closed on. The merge is never requested."""


class UnknownMergeOutcomeError(GitHubError):
    """Raised when a merge was requested but its outcome could not be
    confirmed (``gh`` failed and the pull request is neither merged at the
    expected head nor demonstrably unmerged, or the merged commit could not
    be read back). Never treated as success."""


# --------------------------------------------------------------------------
# Git publishing (Phase 10: commit + push)
# --------------------------------------------------------------------------

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

# Glob patterns (matched against the file's basename) that must never be
# committed by the factory, regardless of what an implementer staged.
PROTECTED_FILE_GLOBS: tuple[str, ...] = (".env*", "*.pem", "id_*", "*.p12", "credentials*")

DEFAULT_MAX_CHANGED_FILES = 200

#: A full, unabbreviated Git object name. The merge boundary never accepts an
#: abbreviated revision: an expected head must be exact.
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA_PATTERN = SHA_PATTERN

#: Environment variables the controller (and only the controller) may read a
#: GitHub token from, in priority order.
TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")


def resolve_github_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Read a GitHub token from the controller's own environment."""
    source = os.environ if environ is None else environ
    for name in TOKEN_ENV_VARS:
        value = source.get(name)
        if value:
            return value
    return None


# scp-like SSH syntax: [user@]host:path -- but not a "scheme://" URL (the
# negative lookahead excludes ssh://... which urlparse already handles).
_SCP_LIKE_HOST_PATTERN = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?!//)")


def _extract_remote_host(url: str) -> str:
    """Parse the host out of an HTTPS or SSH/scp-style Git remote URL.

    Supports ``https://[user@]host[:port]/path``,
    ``ssh://[user@]host[:port]/path``, and scp-like ``[user@]host:path``.
    Raises ``ValueError`` if no host can be determined.
    """
    candidate = url.strip()
    if not candidate:
        raise ValueError("remote URL is empty")

    scp_match = _SCP_LIKE_HOST_PATTERN.match(candidate)
    if scp_match:
        return scp_match.group("host").lower()

    parsed = urlparse(candidate)
    if parsed.hostname:
        return parsed.hostname.lower()

    raise ValueError(f"could not parse a host from remote URL {url!r}")


_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RepositoryRef:
    """Exact remote repository identity: host plus ``owner/name``."""

    host: str
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def same_repository(self, other: "RepositoryRef") -> bool:
        return (
            self.host.casefold() == other.host.casefold()
            and self.owner.casefold() == other.owner.casefold()
            and self.name.casefold() == other.name.casefold()
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.host}/{self.full_name}"


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


def _repository_path_parts(path: str) -> tuple[str, str]:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"expected an OWNER/REPO path, got {path!r}")
    owner, name = parts[0], parts[1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if _OWNER_PATTERN.fullmatch(owner) is None or _NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"{owner}/{name} is not a valid repository name")
    if name in {".", ".."}:
        raise ValueError(f"{owner}/{name} is not a valid repository name")
    return owner, name


def parse_remote_repository(url: str) -> RepositoryRef:
    """Parse full ``host`` + ``owner/name`` identity from a Git remote URL.

    Supports the same HTTPS/SSH/scp-style shapes as :func:`_extract_remote_host`.
    Raises ``ValueError`` when the identity is not unambiguous.
    """
    candidate = url.strip()
    parsed = urlparse(candidate)
    if (
        parsed.query
        or parsed.fragment
        or parsed.password is not None
        or (parsed.scheme == "https" and parsed.username is not None)
    ):
        raise ValueError("remote URL must not carry credentials, query parameters or fragments")
    host = _extract_remote_host(candidate)
    scp_match = _SCP_LIKE_HOST_PATTERN.match(candidate)
    path = candidate[scp_match.end() :] if scp_match else urlparse(candidate).path
    owner, name = _repository_path_parts(path)
    return RepositoryRef(host=host, owner=owner, name=name)


def parse_remote_repository_for_api(url: str) -> RepositoryRef:
    """Parse remote identity for an explicit ``gh --repo OWNER/REPO`` value.

    Unlike :func:`parse_remote_repository`, this may read an HTTPS URL carrying
    credentials because it returns only validated host/owner/name metadata and
    never returns or logs the credential-bearing URL.
    """
    candidate = url.strip()
    parsed = urlparse(candidate)
    if parsed.query or parsed.fragment:
        raise ValueError("remote URL must not carry query parameters or fragments")
    host = _extract_remote_host(candidate)
    scp_match = _SCP_LIKE_HOST_PATTERN.match(candidate)
    path = candidate[scp_match.end() :] if scp_match else parsed.path
    owner, name = _repository_path_parts(path)
    return RepositoryRef(host=host, owner=owner, name=name)


def parse_pull_request_url(url: str) -> tuple[RepositoryRef, int]:
    """Parse ``https://<host>/<owner>/<name>/pull/<number>`` into its exact
    repository identity and PR number. Raises ``ValueError`` otherwise."""
    parsed = urlparse(url.strip())
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise ValueError(f"pull request URL must be an https URL, got {url!r}")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 4 or parts[2] != "pull":
        raise ValueError(f"could not parse a pull request URL from {url!r}")
    owner, name = _repository_path_parts("/".join(parts[:2]))
    if not parts[3].isdigit() or int(parts[3]) <= 0:
        raise ValueError(f"could not parse a pull request number from {url!r}")
    return RepositoryRef(host=parsed.hostname.lower(), owner=owner, name=name), int(parts[3])


def _is_protected_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in PROTECTED_FILE_GLOBS)


def _build_commit_message(message: str, trailer: str) -> str:
    body = message.strip()
    if not body:
        raise ValueError("commit message must not be empty")
    if trailer in body:
        return body
    return f"{body}\n\n{trailer}"


#: Characters and shapes a branch name must never contain. Anything else is a
#: malformed, injected or otherwise unusable ref: a leading ``-`` looks like a
#: flag, ``..``/``@{``/``~``/``^``/``:`` are revision syntax rather than a
#: branch, and control characters cannot appear in a real ref.
_UNSAFE_REF_CHARACTERS = frozenset(" \t\\~^:?*[")


def is_safe_ref_name(name: str) -> bool:
    """Deterministic branch-name safety check (no I/O).

    Accepts only a plain, relative branch name. Used to reject a stale,
    malformed or injected base/head ref *before* it can reach a ``git``/``gh``
    argument list or authorize a merge.
    """
    if not name or name != name.strip():
        return False
    if name.startswith("-") or name.startswith("/") or name.endswith("/"):
        return False
    if name.startswith("refs/") or name.endswith(".lock") or name.endswith("."):
        return False
    if ".." in name or "@{" in name or "//" in name or name == "@":
        return False
    return not any(
        character in _UNSAFE_REF_CHARACTERS or ord(character) < 32 or ord(character) == 127
        for character in name
    )


def _validate_branch_name(branch_name: str, *, branch_prefix: str, base_branch: str) -> None:
    if not branch_name or not branch_name.strip():
        raise UnsafeBranchNameError("branch_name must not be empty")
    if not is_safe_ref_name(branch_name):
        raise UnsafeBranchNameError(f"branch name {branch_name!r} is not a safe ref name")
    if not branch_name.startswith(branch_prefix):
        raise UnsafeBranchNameError(
            f"branch {branch_name!r} does not start with the configured prefix {branch_prefix!r}"
        )
    if branch_name == base_branch:
        raise UnsafeBranchNameError(
            f"branch {branch_name!r} must not be the same as the base branch {base_branch!r}"
        )


def _validate_change_scope(changed_files: Sequence[str], *, max_changed_files: int) -> None:
    protected = sorted(f for f in changed_files if _is_protected_path(f))
    if protected:
        raise ProtectedFileError(f"refusing to commit protected file(s): {', '.join(protected)}")
    if len(changed_files) > max_changed_files:
        raise ExcessiveChangeScopeError(
            f"refusing to commit {len(changed_files)} changed files "
            f"(exceeds max_changed_files={max_changed_files})"
        )


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
            if remote_ref == expected_ref and _SHA_PATTERN.fullmatch(sha):
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
        if _SHA_PATTERN.fullmatch(head) is None:
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
            changed_files = [
                line
                for line in self._run_git(
                    workspace_path, ["diff", "--name-only", parent, tree]
                ).stdout.splitlines()
                if line
            ]
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
        if _SHA_PATTERN.fullmatch(name) is None:
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
                try:
                    remote_sha = self.remote_branch_sha(
                        workspace_path,
                        branch_name,
                        destination=destination,
                    )
                except (GitCommandError, GitTimeoutError):
                    remote_sha = ""
                if remote_sha == commit_sha:
                    return
                final_attempt = attempt + 1 == MAX_GIT_PUSH_ATTEMPTS
                delay = (
                    GIT_PUSH_FINAL_RECONCILE_DELAY_SECONDS
                    if final_attempt
                    else GIT_PUSH_RETRY_DELAY_SECONDS
                )
                self.sleeper(delay)
                try:
                    remote_sha = self.remote_branch_sha(
                        workspace_path,
                        branch_name,
                        destination=destination,
                    )
                except (GitCommandError, GitTimeoutError):
                    remote_sha = ""
                if remote_sha == commit_sha:
                    return
                if final_attempt:
                    raise

    def has_changes(self, workspace_path: Path) -> bool:
        """Stage everything (including untracked files) and report whether
        anything is now staged. Never commits."""
        self._run_git(workspace_path, ["add", "-A"])
        staged = self._run_git(workspace_path, ["diff", "--cached", "--name-only"])
        return any(line for line in staged.stdout.splitlines())

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
        staged = self._run_git(workspace_path, ["diff", "--cached", "--name-only"])
        changed_files = [line for line in staged.stdout.splitlines() if line]
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


# --------------------------------------------------------------------------
# CI status model (Phase 11)
# --------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PENDING = "PENDING"
    PASS = "PASS"
    FAIL = "FAIL"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class FailureCategory(StrEnum):
    CODE_FAILURE = "CODE_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    FLAKY_TEST = "FLAKY_TEST"
    INFRA_FAILURE = "INFRA_FAILURE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    UNKNOWN = "UNKNOWN"


class CheckResult(ModelBase):
    name: str
    status: CheckStatus
    description: str = ""
    details_url: str = ""
    # Bounded, only populated for FAIL checks -- see GitHubClient.fetch_check_log.
    log_excerpt: str = ""
    failure_category: FailureCategory | None = None


class CIStatus(ModelBase):
    overall: CheckStatus
    checks: list[CheckResult] = Field(default_factory=list)


_BUCKET_TO_STATUS: dict[str, CheckStatus] = {
    "pass": CheckStatus.PASS,
    "fail": CheckStatus.FAIL,
    "pending": CheckStatus.PENDING,
    "skipping": CheckStatus.SKIPPED,
    "cancel": CheckStatus.CANCELLED,
}


def _normalize_bucket(value: str) -> CheckStatus:
    normalized = _BUCKET_TO_STATUS.get(value.strip().lower())
    if normalized is not None:
        return normalized
    # Unrecognized gh output: treat conservatively as still pending rather
    # than silently reporting success.
    return CheckStatus.PENDING


def _aggregate_status(checks: Sequence[CheckResult]) -> CheckStatus:
    if not checks:
        return CheckStatus.PENDING
    statuses = {check.status for check in checks}
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL
    if CheckStatus.PENDING in statuses:
        return CheckStatus.PENDING
    if CheckStatus.CANCELLED in statuses:
        return CheckStatus.CANCELLED
    return CheckStatus.PASS


# --------------------------------------------------------------------------
# Pull request state (ADR-022 merge boundary)
# --------------------------------------------------------------------------

#: ``gh pr view --json`` fields the merge boundary needs. Every one of these
#: is a documented ``gh pr view`` field; nothing here is inferred.
PULL_REQUEST_VIEW_FIELDS: tuple[str, ...] = (
    "number",
    "url",
    "state",
    "isDraft",
    "isCrossRepository",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "headRepository",
    "headRepositoryOwner",
    "mergeable",
    "mergeStateStatus",
    "reviewDecision",
    "mergeCommit",
    "statusCheckRollup",
    "body",
)

#: ``gh pr list --json`` fields used for idempotent PR discovery.
PULL_REQUEST_LIST_FIELDS: tuple[str, ...] = (
    "number",
    "url",
    "state",
    "isDraft",
    "isCrossRepository",
    "headRefName",
    "headRefOid",
    "baseRefName",
    "headRepository",
    "headRepositoryOwner",
    "body",
)

#: ``CheckRun.conclusion``/``StatusContext.state`` values, normalized. Anything
#: unrecognized stays ``PENDING`` (never silently successful), and NEUTRAL and
#: SKIPPED are deliberately *not* ``PASS`` -- a required check that skipped did
#: not verify anything.
_CONCLUSION_TO_STATUS: dict[str, CheckStatus] = {
    "SUCCESS": CheckStatus.PASS,
    "FAILURE": CheckStatus.FAIL,
    "ERROR": CheckStatus.FAIL,
    "TIMED_OUT": CheckStatus.FAIL,
    "ACTION_REQUIRED": CheckStatus.FAIL,
    "STARTUP_FAILURE": CheckStatus.FAIL,
    "STALE": CheckStatus.FAIL,
    "CANCELLED": CheckStatus.CANCELLED,
    "NEUTRAL": CheckStatus.SKIPPED,
    "SKIPPED": CheckStatus.SKIPPED,
    "PENDING": CheckStatus.PENDING,
    "EXPECTED": CheckStatus.PENDING,
    "QUEUED": CheckStatus.PENDING,
    "IN_PROGRESS": CheckStatus.PENDING,
    "WAITING": CheckStatus.PENDING,
    "REQUESTED": CheckStatus.PENDING,
}


def normalize_status_check_rollup(items: Sequence[Mapping[str, object]]) -> list[CheckResult]:
    """Normalize ``statusCheckRollup`` entries (check runs *and* commit status
    contexts) into :class:`CheckResult` values.

    The rollup is attached to the pull request's current head commit, so --
    unlike a free-standing check listing -- it cannot describe some other
    revision. A completed check run is classified by its ``conclusion``; an
    incomplete one is ``PENDING`` regardless of any stale conclusion field.
    """
    results: list[CheckResult] = []
    for item in items:
        name = str(item.get("name") or item.get("context") or "").strip()
        if not name:
            name = "unknown-check"
        if item.get("context") is not None and item.get("name") is None:
            raw = str(item.get("state") or "")
        else:
            status_value = str(item.get("status") or "").upper()
            conclusion = str(item.get("conclusion") or "")
            raw = conclusion if status_value in {"", "COMPLETED"} else status_value
        status = _CONCLUSION_TO_STATUS.get(raw.strip().upper(), CheckStatus.PENDING)
        results.append(
            CheckResult(
                name=name,
                status=status,
                description=_redact(str(item.get("description") or ""))[:DEFAULT_MAX_LOG_CHARS],
                details_url=str(item.get("detailsUrl") or item.get("targetUrl") or ""),
            )
        )
    return results


class PullRequestState(ModelBase):
    """Normalized ``gh pr view``/``gh pr list`` payload.

    Fields absent from a given ``gh`` query default to empty rather than
    raising, so one model serves both queries; every merge precondition
    treats an empty value as "not proven" and fails closed.
    """

    number: int = 0
    url: str = ""
    state: str = ""
    is_draft: bool = False
    is_cross_repository: bool = False
    head_ref_name: str = ""
    head_ref_oid: str = ""
    base_ref_name: str = ""
    head_repository: str = ""
    mergeable: str = ""
    merge_state_status: str = ""
    review_decision: str = ""
    merge_commit_sha: str = ""
    body: str = ""
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def merged(self) -> bool:
        return self.state.upper() == "MERGED"


def _head_repository_full_name(payload: Mapping[str, object]) -> str:
    owner = payload.get("headRepositoryOwner")
    repository = payload.get("headRepository")
    owner_login = str(owner.get("login") or "") if isinstance(owner, Mapping) else ""
    name = str(repository.get("name") or "") if isinstance(repository, Mapping) else ""
    if owner_login and name:
        return f"{owner_login}/{name}"
    return ""


def parse_pull_request_payload(payload: Mapping[str, object]) -> PullRequestState:
    """Pure projection of one ``gh pr view``/``gh pr list`` JSON object."""
    merge_commit = payload.get("mergeCommit")
    merge_commit_sha = (
        str(merge_commit.get("oid") or "") if isinstance(merge_commit, Mapping) else ""
    )
    rollup = payload.get("statusCheckRollup")
    checks = (
        normalize_status_check_rollup([item for item in rollup if isinstance(item, Mapping)])
        if isinstance(rollup, list)
        else []
    )
    number_value = payload.get("number")
    return PullRequestState(
        number=number_value if isinstance(number_value, int) else 0,
        url=str(payload.get("url") or ""),
        state=str(payload.get("state") or "").upper(),
        is_draft=bool(payload.get("isDraft") or False),
        is_cross_repository=bool(payload.get("isCrossRepository") or False),
        head_ref_name=str(payload.get("headRefName") or ""),
        head_ref_oid=str(payload.get("headRefOid") or "").lower(),
        base_ref_name=str(payload.get("baseRefName") or ""),
        head_repository=_head_repository_full_name(payload),
        mergeable=str(payload.get("mergeable") or "").upper(),
        merge_state_status=str(payload.get("mergeStateStatus") or "").upper(),
        review_decision=str(payload.get("reviewDecision") or "").upper(),
        merge_commit_sha=merge_commit_sha.lower(),
        body=_redact(str(payload.get("body") or "")),
        checks=checks,
    )


# --------------------------------------------------------------------------
# Failure classification heuristics
# --------------------------------------------------------------------------

_FLAKY_MARKERS = (
    "flaky",
    "known flaky",
    "retry succeeded",
    "passed on retry",
    "re-run may pass",
)
_INFRA_MARKERS = (
    "connection reset",
    "connection refused",
    "could not resolve host",
    "network is unreachable",
    "runner has received a shutdown signal",
    "lost communication with the server",
    "no space left on device",
    "rate limit",
    "502 bad gateway",
    "503 service unavailable",
    "timed out",
    "timeout",
)
_DEPENDENCY_MARKERS = (
    "no matching distribution found",
    "could not resolve dependency",
    "resolution failed",
    "checksum mismatch",
    "cannot find module",
    "module not found",
    "no module named",
    "missing dependency",
    "dependency conflict",
    "peer dependency",
    "npm err!",
    "enoent",
)
_TEST_MARKERS = (
    "assertionerror",
    "test failed",
    "tests failed",
    "failed:",
    "expected:",
)
_TEST_NAME_MARKERS = ("test", "pytest", "jest", "vitest", "unit", "integration")
_CODE_NAME_MARKERS = ("lint", "build", "compile", "typecheck", "type-check", "format")


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def classify_failure(check_name: str, log_excerpt: str = "") -> FailureCategory:
    """Deterministically classify a failed check from its name and a
    bounded excerpt of its log. Pure function: no I/O.

    Precedence mirrors ``governance.RepositoryVerifier.classify_failure``:
    infrastructure/environment signals first (since they can otherwise look
    like any other failure type), then flaky/dependency signals, then
    name-based test/code heuristics, falling back to ``UNKNOWN`` when there
    is no evidence either way.
    """
    name = check_name.lower()
    text = log_excerpt.lower()

    if _contains_any(text, _INFRA_MARKERS):
        return FailureCategory.INFRA_FAILURE
    if _contains_any(text, _FLAKY_MARKERS):
        return FailureCategory.FLAKY_TEST
    if _contains_any(text, _DEPENDENCY_MARKERS) or "dependency" in name or "install" in name:
        return FailureCategory.DEPENDENCY_FAILURE
    if _contains_any(text, _TEST_MARKERS) or _contains_any(name, _TEST_NAME_MARKERS):
        return FailureCategory.TEST_FAILURE
    if _contains_any(name, _CODE_NAME_MARKERS):
        return FailureCategory.CODE_FAILURE
    return FailureCategory.UNKNOWN


# --------------------------------------------------------------------------
# GitHub client (Phase 10 PR creation, Phase 11 CI observation)
# --------------------------------------------------------------------------

_RUN_ID_PATTERN = re.compile(r"/actions/runs/(\d+)")

#: Configured merge method -> the ``gh pr merge`` flag that requests it.
#: ``--admin`` is deliberately absent: this adapter has no code path that can
#: bypass branch protection.
#: ``gh`` verbs this adapter must never invoke. Submitting a review with the
#: factory's credentials would impersonate a human approval; requesting one is
#: equally out of bounds for an unattended controller.
_FORBIDDEN_GH_COMMANDS: frozenset[tuple[str, str]] = frozenset(
    {("pr", "review"), ("pr", "ready"), ("api", "graphql"), ("pr", "merge")}
)

#: ``gh`` flags that would make a request asynchronous or bypass protection.
#: ``--auto`` enables auto-merge, which can merge *later*, after the controller
#: has already given up and handed the run to a human.
_FORBIDDEN_GH_FLAGS: frozenset[str] = frozenset({"--admin", "--auto"})

#: REST merge methods. The REST merge endpoint merges synchronously or fails;
#: unlike ``gh pr merge`` it can never enqueue a merge queue entry or enable
#: auto-merge.
_MERGE_METHODS: frozenset[str] = frozenset({"squash", "merge", "rebase"})

_GITHUB_API_ACCEPT = "Accept: application/vnd.github+json"

#: Substrings in a GitHub merge response that mean "this repository wants the
#: merge to go through a merge queue, or the merge is being deferred". ADR-022
#: deliberately does not model merge queues, so these fail closed instead of
#: leaving a merge pending after the controller stops watching.
_QUEUE_RESPONSE_MARKERS: tuple[str, ...] = (
    "merge queue",
    "merge_queue",
    "queued",
    "auto-merge",
    "auto merge",
    "enqueue",
)


@dataclass(frozen=True)
class MergeOutcome:
    """Parsed response of one synchronous REST merge request."""

    merged: bool
    commit_sha: str
    message: str
    returncode: int

    @property
    def queue_requested(self) -> bool:
        lowered = self.message.casefold()
        return any(marker in lowered for marker in _QUEUE_RESPONSE_MARKERS)


@dataclass(frozen=True)
class BranchPolicy:
    """What the *server* enforces on a base branch, read read-only.

    The factory never enforces required checks on GitHub's behalf: a check can
    be re-run or a new commit can land between our inspection and the merge, so
    only server-side enforcement is atomic with the merge itself.
    """

    required_contexts: frozenset[str]
    strict: bool
    requires_pull_request: bool
    sources: tuple[str, ...]


def _classic_protection_authorizes(protection: Mapping[str, object]) -> bool:
    """Whether classic branch protection can authorize an unattended merge.

    ``enforce_admins`` alone is not enough: ``required_pull_request_reviews``
    may carry ``bypass_pull_request_allowances``, which lets listed users,
    teams or apps merge without the approvals the policy appears to require.
    Since the factory merges with its own credentials, a non-empty (or absent,
    and therefore unknown) allowance means the policy cannot prove that human
    approval was actually enforced. All three allowance arrays must be present
    and explicitly empty.
    """
    admins = protection.get("enforce_admins")
    if not isinstance(admins, Mapping) or admins.get("enabled") is not True:
        return False
    reviews = protection.get("required_pull_request_reviews")
    if not isinstance(reviews, Mapping):
        return False
    allowances = reviews.get("bypass_pull_request_allowances")
    if not isinstance(allowances, Mapping):
        # Absent means "not reported", which cannot be read as "nobody may
        # bypass": GitHub omits the key for some token scopes.
        return False
    for key in ("users", "teams", "apps"):
        actors = allowances.get(key)
        if not isinstance(actors, list) or actors:
            return False
    return True


def _validate_repository_name(repository: str) -> str:
    reference = _repository_path_parts(repository)
    return f"{reference[0]}/{reference[1]}"


@dataclass
class GitHubClient:
    """Controller-owned ``gh`` adapter: PR creation and CI check polling.

    ``token``, when supplied, is only ever passed to the ``gh`` subprocess
    through its environment (as ``GH_TOKEN``) -- never as a CLI argument,
    never included in ``repr()`` (``field(repr=False)``), and never
    interpolated into an error message.
    """

    runner: CommandRunner = default_command_runner
    gh_path: str = "gh"
    token: str | None = field(default=None, repr=False)
    host: str | None = None

    def _env(self) -> Mapping[str, str] | None:
        values: dict[str, str] = {}
        if self.token:
            values["GH_TOKEN"] = self.token
        if self.host:
            values["GH_HOST"] = self.host
        return values or None

    def _run(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self._reject_forbidden(args)
        try:
            result = self.runner([self.gh_path, *args], cwd, self._env())
        except subprocess.TimeoutExpired as exc:
            # ``exc`` can carry captured output; never re-raise it directly.
            raise GitHubTimeoutError((self.gh_path, *args), exc.timeout) from None
        if check and result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        return result

    def active_host(self, repo_path: Path) -> str:
        """Resolve and pin the host selected by the current ``gh`` environment."""
        if self.host is not None:
            return self.host
        args = ["auth", "status", "--active", "--json", "hosts"]
        result = self._run(args, repo_path)
        payload = self._parse_json(args, result)
        if not isinstance(payload, dict) or not isinstance(payload.get("hosts"), dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected gh auth status to return a hosts object",
            )
        hosts = payload["hosts"]
        configured_host = os.environ.get("GH_HOST", "").strip().lower()
        host_names = {
            str(host).strip().lower()
            for host in hosts
            if isinstance(host, str) and str(host).strip()
        }
        selected = configured_host or (
            next(iter(host_names)) if len(host_names) == 1 else "github.com"
        )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", selected) is None:
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "gh selected an invalid host",
            )
        entries = next(
            (
                value
                for key, value in hosts.items()
                if isinstance(key, str) and key.casefold() == selected.casefold()
            ),
            None,
        )
        if not isinstance(entries, list) or not any(
            isinstance(entry, dict)
            and entry.get("state") == "success"
            and isinstance(entry.get("host"), str)
            and entry["host"].casefold() == selected.casefold()
            for entry in entries
        ):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"gh host {selected!r} is not authenticated",
            )
        self.host = selected
        return selected

    @staticmethod
    def _repo_args(repository: str | None) -> list[str]:
        """Name the repository explicitly instead of letting ``gh`` infer it
        from the working directory's remotes, which are mutable local state."""
        if repository is None:
            return []
        parts = repository.split("/")
        if len(parts) == 3:
            host, owner, name = parts
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
                raise GitHubError("invalid explicit GitHub hostname")
            return ["--repo", f"{host}/{_validate_repository_name(f'{owner}/{name}')}"]
        return ["--repo", _validate_repository_name(repository)]

    @staticmethod
    def _reject_forbidden(args: Sequence[str]) -> None:
        """Defensive guard: this adapter must never submit a GitHub review and
        must never bypass branch protection.

        The factory's independent Reviewer is a model, not a GitHub user. Its
        outcome belongs in the pull request *body* as deterministic evidence.
        Submitting ``gh pr review --approve`` with the factory's credentials
        would impersonate a human approval and could satisfy a repository's
        required-review rule that a human is supposed to satisfy, so no code
        path may construct it. ``--admin`` is refused for the same reason.
        """
        argv = [str(part) for part in args]
        if tuple(argv[:2]) in _FORBIDDEN_GH_COMMANDS:
            raise GitHubError(
                f"refusing to run gh {' '.join(argv[:2])}: the factory's independent "
                "Reviewer must not impersonate a GitHub review, and repository review "
                "requirements must keep applying"
            )
        for flag in _FORBIDDEN_GH_FLAGS:
            if flag in argv:
                raise GitHubError(
                    f"refusing to run gh with {flag}: branch protection is never bypassed and "
                    "no merge may complete asynchronously after the controller stops watching"
                )

    # -- PR creation ---------------------------------------------------

    def create_issue(
        self,
        repo_path: Path,
        *,
        repository: str,
        title: str,
        body: str,
        labels: Sequence[str] = (),
    ) -> str:
        """Create one GitHub issue and return its URL.

        Project-created issues are intentionally not given the scheduler's
        ``agent-ready`` label automatically; the local project plan remains
        the authoritative source so one task cannot be dispatched twice.
        """
        if not title.strip():
            raise ValueError("issue title must not be empty")
        if not repository.strip():
            raise ValueError("GitHub repository must not be empty")
        args = [
            "issue",
            "create",
            "--repo",
            repository,
            "--title",
            title,
            "--body",
            body,
        ]
        for label in labels:
            args.extend(["--label", label])
        result = self._run(args, repo_path)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        url = lines[-1] if lines else ""
        if not url.startswith("http"):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"could not parse an issue URL from gh output: {result.stdout!r}",
            )
        return url

    def close_issue(self, repo_path: Path, *, repository: str, issue: str) -> None:
        """Close a project-created issue after its task is integrated."""
        if not issue.strip():
            raise ValueError("issue identifier must not be empty")
        self._run(
            ["issue", "close", issue, "--repo", repository, "--reason", "completed"],
            repo_path,
        )

    def update_pr(
        self,
        repo_path: Path,
        pr: str,
        *,
        body: str,
        title: str | None = None,
        repository: str | None = None,
    ) -> None:
        """Refresh an existing pull request's description (and optionally its
        title) with ``gh pr edit``.

        Used after every re-publish (each CI repair pushes a new commit) so the
        pull request always shows the *current* revision's independent Reviewer
        outcome instead of a stale initial approval. This only edits the pull
        request's own description: it never submits or requests a GitHub review
        and never changes the repository's review requirements.
        """
        if not body.strip():
            raise ValueError("PR body must not be empty")
        args = ["pr", "edit", pr, *self._repo_args(repository), "--body", body]
        if title is not None:
            if not title.strip():
                raise ValueError("PR title must not be empty")
            args.extend(["--title", title])
        self._run(args, repo_path)

    def create_pr(
        self,
        repo_path: Path,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        draft: bool = False,
        repository: str | None = None,
    ) -> str:
        """Open a PR with ``gh pr create`` and return its URL. Never
        merges; this method has no code path that invokes ``gh pr merge``."""
        if not title.strip():
            raise ValueError("PR title must not be empty")
        args = [
            "pr",
            "create",
            *self._repo_args(repository),
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            args.append("--draft")
        result = self._run(args, repo_path)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        url = lines[-1] if lines else ""
        if not url.startswith("http"):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"could not parse a PR URL from gh output: {result.stdout!r}",
            )
        return url

    # -- Pull request state and merging (ADR-022) ------------------------

    def get_pull_request(
        self, repo_path: Path, pr: str, *, repository: str | None = None
    ) -> PullRequestState:
        """Read one pull request's current state via ``gh pr view --json``.

        Read-only: this never changes the pull request. The returned
        ``checks`` come from ``statusCheckRollup``, which GitHub attaches to
        the pull request's *current head commit*, so check evidence cannot
        silently describe an older revision.
        """
        args = [
            "pr",
            "view",
            pr,
            *self._repo_args(repository),
            "--json",
            ",".join(PULL_REQUEST_VIEW_FIELDS),
        ]
        result = self._run(args, repo_path)
        payload = self._parse_json(args, result)
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON object from gh pr view",
            )
        return parse_pull_request_payload(payload)

    def find_pull_requests(
        self,
        repo_path: Path,
        *,
        head: str,
        base: str,
        state: str = "open",
        limit: int = 20,
        repository: str | None = None,
    ) -> list[PullRequestState]:
        """List pull requests for an exact head/base branch pair.

        Used for idempotent PR discovery after a crash between creating a PR
        and persisting its URL. Read-only; the caller is responsible for
        deciding whether a discovered PR is genuinely this run's.
        """
        if not head.strip() or not base.strip():
            raise ValueError("head and base branch names must not be empty")
        if state not in {"open", "closed", "merged", "all"}:
            raise ValueError(f"unsupported pull request state filter {state!r}")
        args = [
            "pr",
            "list",
            *self._repo_args(repository),
            "--head",
            head,
            "--base",
            base,
            "--state",
            state,
            "--limit",
            str(max(1, min(limit, 100))),
            "--json",
            ",".join(PULL_REQUEST_LIST_FIELDS),
        ]
        result = self._run(args, repo_path)
        if not (result.stdout or "").strip():
            # ``gh`` exited successfully with no body: there is nothing to
            # adopt. Returning an empty list keeps discovery fail-safe (a new
            # pull request is created) rather than fail-open (adopting an
            # unverified one).
            return []
        payload = self._parse_json(args, result)
        if not isinstance(payload, list):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON array from gh pr list",
            )
        return [parse_pull_request_payload(item) for item in payload if isinstance(item, dict)]

    def merge_pull_request(
        self,
        repo_path: Path,
        *,
        repository: str,
        number: int,
        method: str,
        expected_head_sha: str,
        hostname: str = "github.com",
    ) -> MergeOutcome:
        """Merge ``number`` synchronously at exactly ``expected_head_sha``.

        Uses the REST endpoint ``PUT /repos/{owner}/{repo}/pulls/{n}/merge``
        rather than ``gh pr merge``. That matters for safety, not style:
        ``gh pr merge`` may silently enable auto-merge or add the pull request
        to a repository's merge queue, in which case the merge would complete
        *later*, possibly after the controller has already reported failure and
        handed the run to a human. The REST call either merges now or fails.

        ``sha`` is sent so GitHub refuses the merge if the head moved after the
        controller inspected it, and the repository is named explicitly instead
        of being inferred from the working directory. Branch protection and
        repository rules still apply and are never bypassed.
        """
        target = _validate_repository_name(repository)
        if method not in _MERGE_METHODS:
            raise ValueError(f"unsupported merge method {method!r}")
        if _SHA_PATTERN.fullmatch(expected_head_sha or "") is None:
            raise ValueError("expected_head_sha must be a full 40-character commit SHA")
        if number <= 0:
            raise ValueError(f"invalid pull request number {number!r}")
        args = [
            "api",
            "--hostname",
            hostname,
            "--method",
            "PUT",
            "-H",
            _GITHUB_API_ACCEPT,
            f"repos/{target}/pulls/{number}/merge",
            "-f",
            f"sha={expected_head_sha}",
            "-f",
            f"merge_method={method}",
        ]
        result = self._run(args, repo_path, check=False)
        payload = self._loads(result.stdout) or self._loads(result.stderr)
        merged = False
        commit_sha = ""
        message = (result.stderr or "").strip()
        if isinstance(payload, dict):
            merged = payload.get("merged") is True
            raw_sha = payload.get("sha")
            commit_sha = raw_sha.strip().lower() if isinstance(raw_sha, str) else ""
            raw_message = payload.get("message")
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
        if merged and _SHA_PATTERN.fullmatch(commit_sha) is None:
            raise UnknownMergeOutcomeError(
                f"GitHub reported pull request {target}#{number} as merged without a usable "
                "merge commit"
            )
        return MergeOutcome(
            merged=merged,
            commit_sha=commit_sha,
            message=message[:500],
            returncode=result.returncode,
        )

    def get_branch_policy(
        self,
        repo_path: Path,
        *,
        repository: str,
        branch: str,
        hostname: str = "github.com",
    ) -> BranchPolicy:
        """Read what the server enforces on ``branch`` (read-only).

        Both the rulesets view and classic branch protection are consulted;
        whichever is readable contributes. Nothing here changes a rule, and an
        unreadable policy is an error, so the caller fails closed rather than
        assuming the server will block a failing check.
        """
        target = _validate_repository_name(repository)
        if not is_safe_ref_name(branch):
            raise ValueError(f"unsafe branch name {branch!r}")
        encoded_branch = quote(branch, safe="")
        contexts: set[str] = set()
        strict = False
        requires_pull_request = False
        sources: list[str] = []
        errors: list[str] = []

        try:
            rules = self._api_json(
                repo_path, f"repos/{target}/rules/branches/{encoded_branch}", hostname=hostname
            )
        except GitHubCommandError as exc:
            rules = None
            errors.append(f"rulesets: {exc}")
        if isinstance(rules, list):
            found = False
            enforcement: dict[str, bool] = {}
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                ruleset_id = rule.get("ruleset_id")
                if (
                    not isinstance(ruleset_id, int)
                    or isinstance(ruleset_id, bool)
                    or ruleset_id <= 0
                ):
                    continue
                source_type = rule.get("ruleset_source_type", "Repository")
                source = rule.get("ruleset_source", target)
                if (
                    source_type == "Organization"
                    and isinstance(source, str)
                    and source.casefold() == target.split("/")[0].casefold()
                ):
                    ruleset_path = f"orgs/{source}/rulesets/{ruleset_id}"
                elif (
                    source_type == "Repository"
                    and isinstance(source, str)
                    and source.casefold() == target.casefold()
                ):
                    ruleset_path = f"repos/{target}/rulesets/{ruleset_id}"
                else:
                    continue
                if ruleset_path not in enforcement:
                    try:
                        metadata = self._api_json(repo_path, ruleset_path, hostname=hostname)
                    except GitHubCommandError as exc:
                        metadata = None
                        errors.append(f"ruleset enforcement: {exc}")
                    enforcement[ruleset_path] = (
                        isinstance(metadata, dict)
                        and metadata.get("enforcement") == "active"
                        and metadata.get("bypass_actors") == []
                    )
                if not enforcement[ruleset_path]:
                    continue
                kind = rule.get("type")
                parameters = rule.get("parameters")
                parameters = parameters if isinstance(parameters, dict) else {}
                if kind == "pull_request":
                    requires_pull_request = True
                    found = True
                if kind == "required_status_checks":
                    found = True
                    for check in parameters.get("required_status_checks") or []:
                        if isinstance(check, dict) and isinstance(check.get("context"), str):
                            contexts.add(check["context"])
                    strict = (
                        strict or parameters.get("strict_required_status_checks_policy") is True
                    )
            if found:
                sources.append("ruleset")

        try:
            protection = self._api_json(
                repo_path, f"repos/{target}/branches/{encoded_branch}/protection", hostname=hostname
            )
        except GitHubCommandError as exc:
            protection = None
            errors.append(f"branch protection: {exc}")
        if isinstance(protection, dict) and _classic_protection_authorizes(protection):
            sources.append("branch-protection")
            checks = protection.get("required_status_checks")
            if isinstance(checks, dict):
                strict = strict or checks.get("strict") is True
                for context in checks.get("contexts") or []:
                    if isinstance(context, str):
                        contexts.add(context)
                for check in checks.get("checks") or []:
                    if isinstance(check, dict) and isinstance(check.get("context"), str):
                        contexts.add(check["context"])
            if isinstance(protection.get("required_pull_request_reviews"), dict):
                requires_pull_request = True
        elif isinstance(protection, dict):
            errors.append(
                "branch protection: enforce_admins is disabled or a pull request review bypass "
                "allowance exists, so it cannot authorize an unattended merge"
            )

        if not sources:
            raise GitHubError(
                f"could not read the enforced branch policy for {target}#{branch}: "
                + "; ".join(errors or ["no policy returned"])
            )
        return BranchPolicy(
            required_contexts=frozenset(contexts),
            strict=strict,
            requires_pull_request=requires_pull_request,
            sources=tuple(sources),
        )

    def _api_json(self, repo_path: Path, path: str, *, hostname: str = "github.com") -> object:
        args = ["api", "--hostname", hostname, "-H", _GITHUB_API_ACCEPT, path]
        result = self._run(args, repo_path, check=False)
        if result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        return self._parse_json(args, result)

    @staticmethod
    def _loads(raw: str | None) -> object:
        try:
            return json.loads((raw or "").strip())
        except (json.JSONDecodeError, TypeError):
            return None

    def _parse_json(self, args: Sequence[str], result: subprocess.CompletedProcess[str]) -> object:
        stdout = (result.stdout or "").strip()
        if not stdout:
            raise GitHubCommandError(
                (self.gh_path, *args), result.returncode, result.stderr or "no output from gh"
            )
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubCommandError(
                (self.gh_path, *args), result.returncode, f"invalid JSON from gh: {exc}"
            ) from exc

    # -- CI observation --------------------------------------------------

    def get_pr_checks(
        self,
        repo_path: Path,
        pr: str,
        *,
        repository: str | None = None,
        fetch_logs: bool = True,
        max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
    ) -> CIStatus:
        """Fetch and normalize check status via ``gh pr checks --json``.

        ``gh pr checks`` intentionally exits non-zero while checks are
        pending or failing (see ``gh help exit-codes``), so the exit code is
        only treated as an error when no JSON body was produced at all.
        """
        args = [
            "pr",
            "checks",
            pr,
            *self._repo_args(repository),
            "--json",
            "name,bucket,state,link,description",
        ]
        result = self._run(args, repo_path, check=False)
        stdout = result.stdout.strip()
        if not stdout:
            if "no checks reported on the " in result.stderr.casefold():
                return CIStatus(overall=CheckStatus.PENDING)
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        try:
            raw_checks = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                f"invalid JSON from gh pr checks: {exc}",
            ) from exc

        checks: list[CheckResult] = []
        for item in raw_checks:
            status = _normalize_bucket(str(item.get("bucket") or item.get("state") or ""))
            description = _redact(str(item.get("description") or ""))[:DEFAULT_MAX_LOG_CHARS]
            check = CheckResult(
                name=str(item.get("name") or "unknown-check"),
                status=status,
                description=description,
                details_url=str(item.get("link") or ""),
            )
            if status is CheckStatus.FAIL:
                log_excerpt = (
                    self.fetch_check_log(repo_path, check, max_chars=max_log_chars)
                    if fetch_logs
                    else ""
                )
                # The check's own summary line is real CI evidence too: when a
                # log cannot be fetched (no run id, logs disabled) it is often
                # the only signal available for classification.
                evidence = f"{log_excerpt}\n{description}".strip()
                check = check.model_copy(
                    update={
                        "log_excerpt": log_excerpt,
                        "failure_category": classify_failure(check.name, evidence),
                    }
                )
            checks.append(check)

        return CIStatus(overall=_aggregate_status(checks), checks=checks)

    def fetch_check_log(
        self,
        repo_path: Path,
        check: CheckResult,
        *,
        max_chars: int = DEFAULT_MAX_LOG_CHARS,
    ) -> str:
        """Bounded, explicit helper returning only the failed-log excerpt
        relevant to ``check`` (never the full historical CI log). Returns
        ``""`` when the run cannot be identified from ``check.details_url``.
        """
        match = _RUN_ID_PATTERN.search(check.details_url or "")
        if match is None:
            return ""
        run_id = match.group(1)
        result = self._run(["run", "view", run_id, "--log-failed"], repo_path, check=False)
        log_text = _redact(result.stdout or "")
        if not log_text:
            return ""
        relevant_lines = [
            line for line in log_text.splitlines() if check.name.lower() in line.lower()
        ]
        excerpt_source = "\n".join(relevant_lines) if relevant_lines else log_text
        return excerpt_source[-max_chars:]

    def poll_checks(
        self,
        repo_path: Path,
        pr: str,
        *,
        repository: str | None = None,
        interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_polls: int = DEFAULT_MAX_POLLS,
        max_seconds: float | None = None,
        fetch_logs: bool = True,
        max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> CIStatus:
        """Bounded poll loop; no webhook. Stops as soon as ``overall`` is no
        longer ``PENDING``. Raises :class:`CIPollTimeoutError` if ``max_polls``
        (and, when given, ``max_seconds``) are exhausted while still pending.
        """
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_polls < 1:
            raise ValueError("max_polls must be at least 1")

        start = clock()
        last_status: CIStatus | None = None
        for attempt in range(1, max_polls + 1):
            last_status = self.get_pr_checks(
                repo_path,
                pr,
                repository=repository,
                fetch_logs=fetch_logs,
                max_log_chars=max_log_chars,
            )
            if last_status.overall is not CheckStatus.PENDING:
                return last_status

            elapsed = clock() - start
            if max_seconds is not None and elapsed >= max_seconds:
                break
            if attempt < max_polls:
                sleep(interval_seconds)

        raise CIPollTimeoutError(
            f"CI checks for PR {pr!r} still pending after {attempt} poll(s)",
            last_status=last_status,
        )


# --------------------------------------------------------------------------
# PR body (pure helper, Phase 10)
# --------------------------------------------------------------------------


def build_pr_body(
    *,
    work_item: WorkItem,
    specification: Specification | None,
    plan: ExecutionPlan | None,
    changed_files: Sequence[str],
    verification: VerificationReport | None,
    test_report: TestReport | None = None,
    review: ReviewReport | None,
    review_acceptance: ReviewAcceptance | None = None,
    run_id: str,
) -> str:
    """Pure function assembling a PR description from typed artifacts.

    No I/O, no network: callers pass whatever artifacts the run has
    produced so far. Never merges or claims approval on the caller's
    behalf -- it only renders what it is given.
    """
    lines: list[str] = ["## Summary", ""]

    if specification is not None:
        lines.append(specification.problem.strip())
        if specification.acceptance_criteria:
            lines.append("")
            lines.append("## Acceptance criteria")
            lines.extend(f"- {item}" for item in specification.acceptance_criteria)
        lines.append("")
    else:
        lines.append(f"Complete work item `{work_item.id}`.")
        lines.append("")

    if plan is not None:
        lines.append("## Plan")
        if plan.steps:
            lines.extend(f"- {step.goal}" for step in plan.steps)
        lines.append("")

    lines.append("## Changed files")
    if changed_files:
        lines.extend(f"- `{changed_file}`" for changed_file in changed_files)
    else:
        lines.append("_no changed files recorded_")
    lines.append("")

    if verification is not None:
        lines.append("## Verification")
        lines.append(f"Passed: {verification.passed}")
        if verification.failures:
            lines.extend(f"- {failure}" for failure in verification.failures)
        if verification.deterministic_checks:
            lines.append("")
            lines.append("Commands:")
            lines.extend(
                f"- `{check.command}` exited {check.exit_code}"
                for check in verification.deterministic_checks
            )
        if verification.test_findings:
            lines.append("")
            lines.append("Tester findings:")
            lines.extend(f"- {finding}" for finding in verification.test_findings)
        lines.append("")

    if test_report is not None:
        lines.append("## Independent tester")
        lines.append(f"Passed: {test_report.passed}")
        if test_report.findings:
            lines.extend(f"- {finding}" for finding in test_report.findings)
        if test_report.suggested_tests:
            lines.append("")
            lines.append("Suggested tests:")
            lines.extend(f"- {suggested}" for suggested in test_report.suggested_tests)
        lines.append("")

    if review is not None:
        lines.append("## Review")
        lines.append(f"Approved: {review.approved}")
        if review.findings:
            lines.extend(f"- {finding}" for finding in review.findings)
        lines.append("")

    if review_acceptance is not None:
        lines.append("## Accepted findings")
        lines.append("The controller accepted these findings under the bounded review policy.")
        lines.append("The Reviewer did not approve this result.")
        lines.append(f"Reviewed tree: `{review_acceptance.reviewed_tree_sha}`")
        lines.append(f"Review rounds: {review_acceptance.review_rounds}")
        lines.extend(
            f"- `{finding.id}` ({finding.category.value}): {finding.message}"
            for finding in review_acceptance.findings
        )
        lines.append("")

    lines.append("## Run")
    lines.append(f"Run ID: `{run_id}`")

    return "\n".join(lines).strip() + "\n"
