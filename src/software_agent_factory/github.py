"""Controller-side Git and GitHub adapters (PLAN.md Phases 10-11).

Implements the "Git ownership" section of ``docs/architecture.md``:

    Agents edit files. Controller owns: worktree creation, branch creation,
    commit, push, PR creation. Agents must not directly push protected
    branches.

and the non-optional halves of:

- Phase 10 ("Pull request creation"): open a PR with a description built
  from the run's typed artifacts. Never merges. The commit and push half
  lives in ``git_publish.py``, with its pure checks in ``git_safety.py``.
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

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import quote, urlparse

from pydantic import Field

from .governance import paths_outside_scope
from .models import (
    ExecutionPlan,
    ModelBase,
    RejectedCommand,
    ReviewAcceptance,
    ReviewReport,
    Specification,
    TestReport,
    UtcDateTime,
    VerificationReport,
    WorkItem,
)
from .redaction import redact_secrets

DEFAULT_MAX_LOG_CHARS = 4000
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_POLLS = 40
DEFAULT_AUTH_CACHE_TTL_SECONDS: float = 60.0


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


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class GitHubError(Exception):
    """Base error for :class:`GitHubClient` operations."""


class GitHubCommandError(GitHubError):
    """Raised when an underlying ``gh`` invocation fails or its output
    cannot be parsed."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.command_args = tuple(args)
        self.returncode = returncode
        self.stderr = redact_secrets(stderr)
        joined = " ".join(str(part) for part in self.command_args)
        super().__init__(f"gh {joined} failed with exit code {returncode}: {self.stderr.strip()}")


class GitHubTimeoutError(GitHubError):
    """Raised when a ``gh`` invocation exceeded its wall-clock budget. The
    command arguments are recorded; any output is redacted."""

    def __init__(self, args: Sequence[str], timeout: float | None) -> None:
        self.command_args = tuple(args)
        self.timeout = timeout
        joined = " ".join(str(part) for part in self.command_args)
        super().__init__(f"gh command timed out after {timeout}s: {redact_secrets(joined)}")


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
# Repository identity and refs
# --------------------------------------------------------------------------

#: A full, unabbreviated Git object name. The merge boundary never accepts an
#: abbreviated revision: an expected head must be exact.
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
GITHUB_HOST = "github.com"

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


def parse_issue_reference(ref: str, default_host: str = GITHUB_HOST) -> tuple[RepositoryRef, int]:
    """Parse an issue reference (URL or OWNER/NAME#NUMBER) into its exact
    repository identity and issue number. Raises ``ValueError`` otherwise."""
    candidate = ref.strip()
    if candidate.startswith("https://") or candidate.startswith("http://"):
        parsed = urlparse(candidate)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError(f"issue reference URL must be an https URL, got {ref!r}")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != 4 or parts[2] not in {"issues", "pull"}:
            raise ValueError(f"could not parse an issue reference from URL {ref!r}")
        owner, name = _repository_path_parts("/".join(parts[:2]))
        if not parts[3].isdigit() or int(parts[3]) <= 0:
            raise ValueError(f"could not parse an issue number from URL {ref!r}")
        return RepositoryRef(host=parsed.hostname.lower(), owner=owner, name=name), int(parts[3])

    if "#" in candidate:
        repo_part, number_part = candidate.split("#", 1)
        if not number_part.isdigit() or int(number_part) <= 0:
            raise ValueError(f"could not parse an issue number from {ref!r}")
        repo_parts = repo_part.split("/")
        if len(repo_parts) == 2:
            owner, name = _repository_path_parts(repo_part)
            host = default_host
        elif len(repo_parts) == 3:
            host, owner, name = repo_parts
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
                raise ValueError(f"invalid host in issue reference {ref!r}")
            owner, name = _repository_path_parts(f"{owner}/{name}")
        else:
            raise ValueError(f"invalid repository format in issue reference {ref!r}")
        return RepositoryRef(host=host.lower(), owner=owner, name=name), int(number_part)

    raise ValueError(f"could not parse an issue reference from {ref!r}")


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
                description=redact_secrets(str(item.get("description") or ""))[
                    :DEFAULT_MAX_LOG_CHARS
                ],
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
        body=redact_secrets(str(payload.get("body") or "")),
        checks=checks,
    )


class GitHubIdentity(ModelBase):
    """Normalized authenticated GitHub user identity."""

    id: int = Field(ge=1)
    login: str = Field(min_length=1)


class GitHubComment(ModelBase):
    """Normalized issue/PR comment payload."""

    id: int = Field(ge=1)
    url: str = ""
    html_url: str = ""
    body: str = ""
    user_login: str = ""
    user_id: int | None = None
    user_type: str = ""
    created_at: UtcDateTime
    updated_at: UtcDateTime
    author_association: str = ""


def parse_comment_payload(payload: Mapping[str, object]) -> GitHubComment:
    """Parse one GitHub issue/PR comment payload."""
    raw_id = payload.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        raise GitHubError(f"invalid comment id in payload: {raw_id!r}")
    user = payload.get("user")
    user_map = user if isinstance(user, Mapping) else {}
    raw_user_id = user_map.get("id")
    user_id = (
        raw_user_id
        if isinstance(raw_user_id, int) and not isinstance(raw_user_id, bool) and raw_user_id > 0
        else None
    )
    user_login = str(user_map.get("login") or "").strip()
    user_type = str(user_map.get("type") or "").strip()

    raw_created = str(payload.get("created_at") or payload.get("createdAt") or "").strip()
    raw_updated = str(payload.get("updated_at") or payload.get("updatedAt") or "").strip()
    if not raw_created:
        raise GitHubError("comment payload missing created_at")
    try:
        created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubError(f"invalid created_at in comment payload: {raw_created!r}") from exc
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    if raw_updated:
        try:
            updated_at = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitHubError(f"invalid updated_at in comment payload: {raw_updated!r}") from exc
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        else:
            updated_at = updated_at.astimezone(timezone.utc)
    else:
        updated_at = created_at

    return GitHubComment(
        id=raw_id,
        url=str(payload.get("url") or ""),
        html_url=str(payload.get("html_url") or payload.get("htmlUrl") or ""),
        body=redact_secrets(str(payload.get("body") or "")),
        user_login=user_login,
        user_id=user_id,
        user_type=user_type,
        created_at=created_at,
        updated_at=updated_at,
        author_association=str(
            payload.get("author_association") or payload.get("authorAssociation") or ""
        ).upper(),
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
    _authenticated_identities: dict[str, tuple[GitHubIdentity, float, str]] = field(
        default_factory=dict, init=False, repr=False
    )

    def _active_credential_key(self, hostname: str) -> str:
        token = self.token or resolve_github_token()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
            return f"token:{digest}"
        gh_config_dir = os.environ.get("GH_CONFIG_DIR")
        config_path = (
            Path(gh_config_dir) / "hosts.yml"
            if gh_config_dir
            else Path.home() / ".config" / "gh" / "hosts.yml"
        )
        try:
            if config_path.is_file():
                mtime = config_path.stat().st_mtime_ns
                return f"ambient-file:{mtime}"
        except OSError:
            pass
        return "ambient-none"

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
            next(iter(host_names)) if len(host_names) == 1 else GITHUB_HOST
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

    def create_issue_comment(
        self,
        repo_path: Path,
        *,
        repository: str,
        issue_number: int,
        body: str,
        hostname: str = GITHUB_HOST,
    ) -> GitHubComment:
        """Post a comment on an issue or pull request conversation."""
        target = _validate_repository_name(repository)
        if issue_number <= 0:
            raise ValueError(f"invalid issue or pull request number: {issue_number}")
        if not body.strip():
            raise ValueError("comment body must not be empty")
        args = [
            "api",
            "--hostname",
            hostname,
            "--method",
            "POST",
            "-H",
            _GITHUB_API_ACCEPT,
            f"repos/{target}/issues/{issue_number}/comments",
            "-f",
            f"body={body}",
        ]
        result = self._run(args, repo_path, check=False)
        if result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        payload = self._parse_json(args, result)
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON object from gh api comment creation",
            )
        return parse_comment_payload(payload)

    def list_issue_comments(
        self,
        repo_path: Path,
        *,
        repository: str,
        issue_number: int,
        since: datetime | None = None,
        page: int = 1,
        per_page: int = 100,
        hostname: str = GITHUB_HOST,
    ) -> list[GitHubComment]:
        """List comments for an issue or pull request conversation with bounded pagination."""
        target = _validate_repository_name(repository)
        if issue_number <= 0:
            raise ValueError(f"invalid issue or pull request number: {issue_number}")
        bounded_page = max(1, page)
        bounded_per_page = max(1, min(per_page, 100))
        path = (
            f"repos/{target}/issues/{issue_number}/comments"
            f"?page={bounded_page}&per_page={bounded_per_page}"
        )
        if since is not None:
            since_utc = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            path += f"&since={since_utc}"
        args = [
            "api",
            "--hostname",
            hostname,
            "-H",
            _GITHUB_API_ACCEPT,
            path,
        ]
        result = self._run(args, repo_path, check=False)
        if result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        payload = self._parse_json(args, result)
        if not isinstance(payload, list):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON array from gh api comment listing",
            )
        return [parse_comment_payload(item) for item in payload if isinstance(item, dict)]

    def get_issue_comment(
        self,
        repo_path: Path,
        *,
        repository: str,
        comment_id: int,
        hostname: str = GITHUB_HOST,
    ) -> GitHubComment:
        """Fetch and revalidate a single issue or pull request comment."""
        target = _validate_repository_name(repository)
        if comment_id <= 0:
            raise ValueError(f"invalid comment id: {comment_id}")
        args = [
            "api",
            "--hostname",
            hostname,
            "-H",
            _GITHUB_API_ACCEPT,
            f"repos/{target}/issues/comments/{comment_id}",
        ]
        result = self._run(args, repo_path, check=False)
        if result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        payload = self._parse_json(args, result)
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON object from gh api comment retrieval",
            )
        return parse_comment_payload(payload)

    def get_authenticated_user(
        self,
        repo_path: Path,
        *,
        hostname: str = GITHUB_HOST,
        force_refresh: bool = False,
        max_age_seconds: float = DEFAULT_AUTH_CACHE_TTL_SECONDS,
    ) -> GitHubIdentity:
        """Resolve and cache the authenticated GitHub user identity for a host.

        Maintains a safe, bounded cache tied to the active credential and TTL
        to ensure changes to ambient or configured authentication are detected
        during long-running daemon operations.
        """
        now = time.monotonic()
        cred_key = self._active_credential_key(hostname)
        if not force_refresh and hostname in self._authenticated_identities:
            cached_identity, cached_at, cached_key = self._authenticated_identities[hostname]
            if (now - cached_at) < max_age_seconds and cached_key == cred_key:
                return cached_identity

        args = [
            "api",
            "--hostname",
            hostname,
            "-H",
            _GITHUB_API_ACCEPT,
            "user",
        ]
        result = self._run(args, repo_path, check=False)
        if result.returncode != 0:
            raise GitHubCommandError((self.gh_path, *args), result.returncode, result.stderr)
        payload = self._parse_json(args, result)
        if not isinstance(payload, dict):
            raise GitHubCommandError(
                (self.gh_path, *args),
                result.returncode,
                "expected a JSON object from gh api user",
            )
        raw_id = payload.get("id")
        raw_login = payload.get("login")
        if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
            raise GitHubError(f"invalid user id in api user payload: {raw_id!r}")
        if not isinstance(raw_login, str) or not raw_login.strip():
            raise GitHubError(f"invalid login in api user payload: {raw_login!r}")
        identity = GitHubIdentity(id=raw_id, login=raw_login.strip())
        self._authenticated_identities[hostname] = (identity, now, cred_key)
        return identity

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

    def flag_pr(
        self,
        repo_path: Path,
        pr: str,
        *,
        label: str,
        comment: str,
        repository: str | None = None,
    ) -> None:
        """Add ``label`` (creating it when missing) and post ``comment`` on a
        pull request. Never merges and never submits a review."""
        repo_args = self._repo_args(repository)
        self._run(["label", "create", label, *repo_args, "--force"], repo_path)
        self._run(["pr", "edit", pr, *repo_args, "--add-label", label], repo_path)
        self._run(["pr", "comment", pr, *repo_args, "--body", comment], repo_path)

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
        self,
        repo_path: Path,
        pr: str,
        *,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> PullRequestState:
        """Read one pull request's current state via ``gh pr view --json``.

        Read-only: this never changes the pull request. The returned
        ``checks`` come from ``statusCheckRollup``, which GitHub attaches to
        the pull request's *current head commit*, so check evidence cannot
        silently describe an older revision.
        """
        target_repo = repository
        if hostname and target_repo and len(target_repo.split("/")) == 2:
            target_repo = f"{hostname}/{target_repo}"
        args = [
            "pr",
            "view",
            pr,
            *self._repo_args(target_repo),
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
        hostname: str = GITHUB_HOST,
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
        if SHA_PATTERN.fullmatch(expected_head_sha or "") is None:
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
        if merged and SHA_PATTERN.fullmatch(commit_sha) is None:
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
        hostname: str = GITHUB_HOST,
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

    def _api_json(self, repo_path: Path, path: str, *, hostname: str = GITHUB_HOST) -> object:
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
            description = redact_secrets(str(item.get("description") or ""))[:DEFAULT_MAX_LOG_CHARS]
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
        log_text = redact_secrets(result.stdout or "")
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
    suppressions: Sequence[str] = (),
    verification: VerificationReport | None,
    test_report: TestReport | None = None,
    review: ReviewReport | None,
    review_acceptance: ReviewAcceptance | None = None,
    rejected_commands: Sequence[RejectedCommand] = (),
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
    if plan is not None:
        lines.extend(_scope_growth(plan, changed_files))
    lines.extend(_bulleted_section("Suppressions and skips added", suppressions))

    if verification is not None:
        lines.extend(["## Verification", f"Passed: {verification.passed}"])
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

    rejected_lines = [f"`{item.command}`: {item.reason}" for item in rejected_commands]
    lines.extend(_bulleted_section("Checks not run", rejected_lines))
    lines.extend(
        _bulleted_section("Assumptions and open questions", _open_items(specification, plan))
    )

    if test_report is not None:
        lines.append("## Independent tester")
        if test_report.skipped:
            lines.append(f"Skipped: {test_report.skip_reason or 'Tester skipped'}")
        else:
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
        if review.skipped:
            lines.append(f"Skipped: {review.skip_reason or 'Reviewer skipped'}")
        else:
            lines.append(f"Approved: {review.approved}")
            if review.findings:
                lines.extend(f"- {finding}" for finding in review.findings)
            if review.suggested_changes:
                lines.append("")
                lines.append("Suggested changes:")
                lines.extend(f"- {suggested}" for suggested in review.suggested_changes)
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

    lines.extend(["## Run", f"Run ID: `{run_id}`"])

    return "\n".join(lines).strip() + "\n"


def _scope_growth(plan: ExecutionPlan, changed_files: Sequence[str]) -> list[str]:
    """How far the change grew past its plan; a growth sign, not a gate."""
    scope = plan.expected_scope
    lines = [
        f"{len(changed_files)} files changed; the plan estimated "
        f"{scope.estimated_files_min}-{scope.estimated_files_max}.",
        "",
    ]
    outside = paths_outside_scope(scope.modules, changed_files)
    return lines + _bulleted_section(
        "Outside the planned modules", [f"`{path}`" for path in outside]
    )


def _bulleted_section(title: str, items: Sequence[str]) -> list[str]:
    """Render a PR body section, or nothing when there is nothing to disclose."""
    if not items:
        return []
    return [f"## {title}", *(f"- {item}" for item in items), ""]


def _open_items(specification: Specification | None, plan: ExecutionPlan | None) -> list[str]:
    assumptions = specification.assumptions if specification is not None else []
    decisions = plan.unresolved_decisions if plan is not None else []
    return [*(f"Assumption: {item}" for item in assumptions), *decisions]
