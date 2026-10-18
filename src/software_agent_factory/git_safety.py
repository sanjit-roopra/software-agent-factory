"""Pure git publishing checks and errors used by ``git_publish.GitPublisher``.

Nothing here runs a process. These are the deterministic guards: branch
name safety, protected-file and change-scope limits, raw diff parsing, and
transient push error classification.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Sequence

from .github import is_safe_ref_name
from .redaction import redact_secrets


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
        super().__init__(f"git command timed out after {timeout}s: {redact_secrets(joined)}")


class GitCommandError(GitPublishError):
    """Raised when an underlying ``git`` invocation fails."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.command_args = tuple(args)
        self.returncode = returncode
        self.stderr = redact_secrets(stderr)
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


# Glob patterns (matched against the file's basename) that must never be
# committed by the factory, regardless of what an implementer staged.
PROTECTED_FILE_GLOBS: tuple[str, ...] = (".env*", "*.pem", "id_*", "*.p12", "credentials*")


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


_STATUS_TOKEN_PATTERN = re.compile(r"^[ACDMRTUX][0-9]*$")


def _parse_raw_diff_changed_files(output: str) -> list[str]:
    """Extract changed file paths from NUL-delimited raw git diff (``--raw -z``).

    For rename and copy records, both source and destination paths are
    included, without duplicates and preserving deterministic order.
    File names that match status codes (e.g. 'M', 'A0', 'R100') are never
    treated as status tokens because every record unambiguously starts with
    a ':' header.
    """
    if not output:
        return []

    changed_files: list[str] = []
    seen: set[str] = set()

    def add_path(p: str) -> None:
        if p and p not in seen:
            seen.add(p)
            changed_files.append(p)

    parts = output.rstrip("\0").split("\0")
    if not parts or parts == [""]:
        return []

    idx = 0
    while idx < len(parts):
        token = parts[idx]
        if not token:
            idx += 1
            continue
        if not token.startswith(":"):
            raise GitPublishError(f"malformed raw diff header: {token!r}")
        header_fields = token.split()
        status = header_fields[-1] if header_fields else ""
        if status.startswith(("R", "C")):
            if idx + 2 >= len(parts):
                raise GitPublishError(f"truncated raw diff record for rename/copy: {token!r}")
            add_path(parts[idx + 1])
            add_path(parts[idx + 2])
            idx += 3
        else:
            if idx + 1 >= len(parts):
                raise GitPublishError(f"truncated raw diff record: {token!r}")
            add_path(parts[idx + 1])
            idx += 2

    return changed_files


def _validate_change_scope(changed_files: Sequence[str], *, max_changed_files: int) -> None:
    protected = sorted(f for f in changed_files if _is_protected_path(f))
    if protected:
        raise ProtectedFileError(f"refusing to commit protected file(s): {', '.join(protected)}")
    if len(changed_files) > max_changed_files:
        raise ExcessiveChangeScopeError(
            f"refusing to commit {len(changed_files)} changed files "
            f"(exceeds max_changed_files={max_changed_files})"
        )
