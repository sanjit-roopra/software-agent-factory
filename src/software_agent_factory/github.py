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

Phase 12 ("CI repair") is explicitly out of scope here -- this module only
observes and classifies; it does not decide retries or drive a repair loop.

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
from urllib.parse import urlparse

from pydantic import Field

from .copilot_runtime import TOKEN_PATTERNS
from .models import (
    ExecutionPlan,
    ModelBase,
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


def default_command_runner(
    args: Sequence[str],
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command as an argument list; never through a shell."""
    merged_env = None if env is None else {**os.environ, **env}
    return subprocess.run(list(args), cwd=cwd, env=merged_env, capture_output=True, text=True)


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


class UnsafeRemoteError(GitPublishError):
    """Raised when ``self.remote`` cannot be resolved to a URL, the URL
    cannot be parsed, or its host is not in ``GitPublisher.allowed_hosts``.
    The commit is aborted before it is created."""


class GitCommandError(GitPublishError):
    """Raised when an underlying ``git`` invocation fails."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.command_args = tuple(args)
        self.returncode = returncode
        self.stderr = _redact(stderr)
        joined = " ".join(self.command_args)
        super().__init__(f"git {joined} failed with exit code {returncode}: {self.stderr.strip()}")


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


class CIPollTimeoutError(GitHubError):
    """Raised when checks are still pending after the poll budget is spent."""

    def __init__(self, message: str, last_status: "CIStatus | None" = None) -> None:
        self.last_status = last_status
        super().__init__(message)


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


def _validate_branch_name(branch_name: str, *, branch_prefix: str, base_branch: str) -> None:
    if not branch_name or not branch_name.strip():
        raise UnsafeBranchNameError("branch_name must not be empty")
    if branch_name.startswith("-"):
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

    def _run_git(
        self, workspace_path: Path, args: Sequence[str]
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
        result = self.runner(full_args)
        if result.returncode != 0:
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

    def has_changes(self, workspace_path: Path) -> bool:
        """Stage everything (including untracked files) and report whether
        anything is now staged. Never commits."""
        self._run_git(workspace_path, ["add", "-A"])
        staged = self._run_git(workspace_path, ["diff", "--cached", "--name-only"])
        return any(line for line in staged.stdout.splitlines())

    def commit_and_push(self, workspace_path: Path, branch_name: str, message: str) -> str:
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
        self._validate_remote_host(workspace_path)

        self._run_git(workspace_path, ["add", "-A"])
        staged = self._run_git(workspace_path, ["diff", "--cached", "--name-only"])
        changed_files = [line for line in staged.stdout.splitlines() if line]
        if not changed_files:
            raise NoChangesToCommitError(
                f"no staged or working changes to commit in {workspace_path}"
            )
        _validate_change_scope(changed_files, max_changed_files=self.max_changed_files)

        self._run_git(workspace_path, ["commit", "-m", full_message])
        sha = self._run_git(workspace_path, ["rev-parse", "HEAD"]).stdout.strip()
        # Explicit refspec, no --force: pushes exactly this commit onto the
        # controller-owned branch and nothing else.
        self._run_git(
            workspace_path,
            ["push", self.remote, f"HEAD:refs/heads/{branch_name}"],
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

    def _env(self) -> Mapping[str, str] | None:
        return {"GH_TOKEN": self.token} if self.token else None

    def _run(
        self,
        args: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = self.runner([self.gh_path, *args], cwd, self._env())
        if check and result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        return result

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

    def create_pr(
        self,
        repo_path: Path,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> str:
        """Open a PR with ``gh pr create`` and return its URL. Never
        merges; this method has no code path that invokes ``gh pr merge``."""
        if not title.strip():
            raise ValueError("PR title must not be empty")
        args = [
            "pr",
            "create",
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

    # -- CI observation --------------------------------------------------

    def get_pr_checks(
        self,
        repo_path: Path,
        pr: str,
        *,
        fetch_logs: bool = True,
        max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
    ) -> CIStatus:
        """Fetch and normalize check status via ``gh pr checks --json``.

        ``gh pr checks`` intentionally exits non-zero while checks are
        pending or failing (see ``gh help exit-codes``), so the exit code is
        only treated as an error when no JSON body was produced at all.
        """
        args = ["pr", "checks", pr, "--json", "name,bucket,state,link,description"]
        result = self._run(args, repo_path, check=False)
        stdout = result.stdout.strip()
        if not stdout:
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
                repo_path, pr, fetch_logs=fetch_logs, max_log_chars=max_log_chars
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
    run_id: str,
) -> str:
    """Pure function assembling a PR description from typed artifacts.

    No I/O, no network: callers pass whatever artifacts the run has
    produced so far. Never merges or claims approval on the caller's
    behalf -- it only renders what it is given.
    """
    lines: list[str] = [f"## {work_item.title}", "", work_item.description.strip(), ""]

    if work_item.acceptance_criteria:
        lines.append("### Acceptance criteria")
        lines.extend(f"- {item}" for item in work_item.acceptance_criteria)
        lines.append("")

    if specification is not None:
        lines.append("### Specification")
        lines.append(specification.problem.strip())
        if specification.acceptance_criteria:
            lines.append("")
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in specification.acceptance_criteria)
        lines.append("")

    if plan is not None:
        lines.append("### Plan")
        lines.append(plan.summary.strip())
        if plan.steps:
            lines.append("")
            lines.extend(f"- {step.goal}" for step in plan.steps)
        lines.append("")

    lines.append("### Changed files")
    if changed_files:
        lines.extend(f"- `{changed_file}`" for changed_file in changed_files)
    else:
        lines.append("_no changed files recorded_")
    lines.append("")

    if verification is not None:
        lines.append("### Deterministic verification")
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
            lines.append("AI tester findings:")
            lines.extend(f"- {finding}" for finding in verification.test_findings)
        lines.append("")

    if test_report is not None:
        lines.append("### Independent tester")
        lines.append(f"Passed: {test_report.passed}")
        if test_report.findings:
            lines.extend(f"- {finding}" for finding in test_report.findings)
        if test_report.suggested_tests:
            lines.append("")
            lines.append("Suggested tests:")
            lines.extend(f"- {suggested}" for suggested in test_report.suggested_tests)
        lines.append("")

    if review is not None:
        lines.append("### Reviewer result")
        lines.append(f"Approved: {review.approved}")
        if review.findings:
            lines.extend(f"- {finding}" for finding in review.findings)
        lines.append("")

    lines.append("### Run")
    lines.append(f"Run ID: `{run_id}`")

    return "\n".join(lines).strip() + "\n"
