"""Git worktree workspace management.

Provides ``GitWorktreeWorkspace``, the Phase 1 implementation of the
``WorkspaceProvider`` concept described in ``docs/architecture.md``.

Design notes (see docs/architecture.md "Workspace abstraction" and
docs/symphony-alignment.md "Deterministic per-task workspaces" /
"Workspace lifecycle"):

- Each work item gets a stable, sanitized, root-contained workspace under
  ``data_dir/workspaces`` backed by a Git worktree.
- Workspaces are preserved by default; ``cleanup()`` is explicit and is not
  called by the default workflow.
- An advisory ``fcntl.flock`` held on an open descriptor under
  ``data_dir/locks`` prevents two runs from operating on the same work item
  concurrently. Because the kernel drops the lock when the owning process
  dies, a crashed run never leaves an un-acquirable workspace; acquirers
  validate the inode they locked so a clean release can also unlink the
  file.
- ``git worktree add``/``prune`` rewrite administrative metadata shared by
  every worktree of a repository, so the whole ``prepare()`` sequence is
  serialized under a per-source-repo flock. That flock is acquired with a
  blocking (not polling) ``fcntl.flock`` call and has no timeout: a slow
  checkout by another run is a normal, bounded wait, not a failure, and a
  blocking flock wakes immediately (no busy-waiting) the instant the kernel
  drops the lock, including when the holder crashes.
- ``prepare()`` is idempotent: re-running it against an already registered,
  present worktree is a no-op that returns the same path and base commit.
- The controller (not this module) is responsible for turning collected
  evidence into a typed ``ChangeSet``; this module only returns raw changed
  file paths and a ``git diff --cached <base_commit>`` text blob so it has no
  dependency on artifact models that may not exist yet.

This module intentionally performs no network access and never deletes or
mutates the source repository beyond read-only ``git`` inspection commands
and the (opt-in) ``git worktree remove`` in ``cleanup()``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence


class WorkspaceError(Exception):
    """Base error for workspace operations."""


class WorkspaceLockError(WorkspaceError):
    """Raised when an exclusive workspace lock cannot be acquired."""


class WorkspaceSafetyError(WorkspaceError):
    """Raised when an operation would be unsafe (e.g. path escapes root,
    or an existing directory is not the expected registered worktree)."""


_SANITIZE_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]")
_SANITIZE_COLLAPSE = re.compile(r"-{2,}")
_MAX_KEY_LEN = 80
_HASH_LEN = 10

#: Bounded retries for the (rare) "lock file was replaced" race.
_LOCK_ACQUIRE_RETRIES = 5


def sanitize_work_item_id(work_item_id: str) -> str:
    """Derive a sanitized, collision-resistant workspace key.

    The key only contains characters safe for filesystem paths and Git
    branch names (``[A-Za-z0-9._-]``). If sanitization changes the input
    (including truncation), a stable short hash of the original id is
    appended so distinct raw ids cannot collide on the same sanitized key.
    """
    if work_item_id is None or not work_item_id.strip():
        raise ValueError("work_item_id must be a non-empty string")

    sanitized = _SANITIZE_DISALLOWED.sub("-", work_item_id)
    sanitized = _SANITIZE_COLLAPSE.sub("-", sanitized).strip("-.")

    digest = hashlib.sha256(work_item_id.encode("utf-8")).hexdigest()[:_HASH_LEN]

    if not sanitized:
        return f"item-{digest}"

    if sanitized != work_item_id or len(sanitized) > _MAX_KEY_LEN:
        truncated = sanitized[:_MAX_KEY_LEN].rstrip("-.")
        return f"{truncated}-{digest}"

    return sanitized


def _ensure_strictly_within(path: Path, root: Path) -> Path:
    """Resolve ``path`` and ``root``, and ensure ``path`` is a proper
    descendant of ``root`` (not equal to it)."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceSafetyError(
            f"path {resolved_path} is not contained within root {resolved_root}"
        ) from exc
    if str(relative) == ".":
        raise WorkspaceSafetyError(
            f"path {resolved_path} must be strictly below root {resolved_root}"
        )
    return resolved_path


def _run_git(
    cwd: Path, args: Sequence[str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed in {cwd}: {completed.stderr.strip()}")
    return completed


@dataclass(frozen=True)
class WorkspaceEvidence:
    """Raw Git evidence collected from a workspace.

    The controller derives the typed ``ChangeSet`` artifact from this data;
    this module deliberately stays independent of that model.
    """

    changed_files: list[str] = field(default_factory=list)
    diff: str = ""


def _parse_worktree_list(output: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain`` output into per-worktree dicts."""
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if " " in line:
            key, _, value = line.partition(" ")
        else:
            key, value = line, "true"
        current[key] = value
    if current:
        entries.append(current)
    return entries


class GitWorktreeWorkspace:
    """A deterministic, per-work-item Git worktree workspace."""

    def __init__(
        self,
        data_dir: Path,
        source_repo: Path,
        work_item_id: str,
        branch_prefix: str = "factory/",
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.source_repo = self._validate_source_repo(Path(source_repo))
        self.work_item_id = work_item_id
        self.branch_prefix = branch_prefix

        self.workspace_root = self.data_dir / "workspaces"
        self.locks_root = self.data_dir / "locks"
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.locks_root.mkdir(parents=True, exist_ok=True)

        self.key = sanitize_work_item_id(work_item_id)
        self.branch_name = f"{branch_prefix}{self.key}"

        self.path = _ensure_strictly_within(self.workspace_root / self.key, self.workspace_root)
        self.lock_path = _ensure_strictly_within(
            self.locks_root / f"{self.key}.lock", self.locks_root
        )
        self._meta_path = self.workspace_root / f"{self.key}.meta.json"

        common_dir = self._git_common_dir()
        source_digest = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:_HASH_LEN]
        self.prune_lock_path = _ensure_strictly_within(
            self.locks_root / f"prune-{source_digest}.lock", self.locks_root
        )

        self._lock_fd: int | None = None
        self.base_commit: str | None = None

    def _git_common_dir(self) -> Path:
        completed = subprocess.run(
            ["git", "-C", str(self.source_repo), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise WorkspaceError(
                f"could not resolve Git common directory for {self.source_repo}: "
                f"{completed.stderr.strip()}"
            )
        common_dir = Path(completed.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = self.source_repo / common_dir
        return common_dir.resolve()

    @staticmethod
    def _validate_source_repo(source_repo: Path) -> Path:
        resolved = source_repo.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"source repo {resolved} is not a directory")
        completed = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or completed.stdout.strip() != "true":
            raise WorkspaceError(f"{resolved} is not a Git working tree")
        return resolved

    # -- locking -----------------------------------------------------

    def acquire_lock(self) -> None:
        """Acquire an exclusive advisory lock for this workspace.

        The lock is an ``fcntl.flock`` held on an open descriptor rather than
        an ``O_EXCL`` marker file, so the kernel releases it if the owning
        process dies (crash, ``SIGKILL``, power loss) and a run can never be
        blocked by a stale marker. The file's contents (the owner pid) exist
        purely for human debugging.

        After locking, the descriptor's inode is compared against the inode
        currently at ``lock_path``: a previous owner unlinking the file during
        its own release would otherwise let two processes hold flocks on two
        different inodes for the same workspace. A mismatch simply means the
        file was replaced, so the acquisition is retried against the current
        one.

        Raises ``WorkspaceLockError`` if another live owner holds the lock.
        """
        if self._lock_fd is not None:
            return

        for _ in range(_LOCK_ACQUIRE_RETRIES):
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                raise WorkspaceLockError(
                    f"workspace {self.key} is already locked at {self.lock_path}"
                ) from exc

            if not self._fd_matches_lock_path(fd):
                # The file we locked was unlinked/replaced by its previous
                # owner; lock the file that is there now instead.
                self._release_fd(fd)
                continue

            try:
                os.truncate(fd, 0)
                os.write(fd, str(os.getpid()).encode("utf-8"))
            except OSError:
                self._release_fd(fd)
                raise
            self._lock_fd = fd
            return

        raise WorkspaceLockError(
            f"could not acquire a stable lock for workspace {self.key} at {self.lock_path}"
        )

    def _fd_matches_lock_path(self, fd: int) -> bool:
        try:
            path_stat = os.stat(str(self.lock_path))
        except FileNotFoundError:
            return False
        fd_stat = os.fstat(fd)
        return (path_stat.st_dev, path_stat.st_ino) == (fd_stat.st_dev, fd_stat.st_ino)

    def release_lock(self) -> None:
        """Release the advisory lock, if held.

        The lock file is unlinked *while the lock is still held* so no
        leftover file remains after a clean release; acquirers validate the
        inode they locked, so the unlink cannot hand ownership to two
        processes at once.
        """
        fd = self._lock_fd
        self._lock_fd = None
        if fd is None:
            return
        if self._fd_matches_lock_path(fd):
            with contextlib.suppress(OSError):
                self.lock_path.unlink()
        self._release_fd(fd)

    @staticmethod
    def _release_fd(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    @property
    def lock_held(self) -> bool:
        return self._lock_fd is not None

    def __enter__(self) -> "GitWorktreeWorkspace":
        self.acquire_lock()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release_lock()

    @contextlib.contextmanager
    def _prune_lock(self) -> Iterator[None]:
        """Serialize repository-global worktree administration per source repo.

        ``git worktree add``/``prune`` both inspect and rewrite shared
        administrative metadata for *all* worktrees of a repository, so two
        runs administering worktrees concurrently can remove or clobber
        metadata for a worktree the other run just created. An exclusive
        flock keyed on the source repository removes that race, which is
        what makes ``scheduler.max_concurrent_tasks = 2`` safe against the
        same source repository.

        Acquisition blocks the kernel's ``flock(2)`` wait queue rather than
        polling against a short deadline: at this small supported
        concurrency, a slow checkout held by the other run is an ordinary
        wait, not a failure, and turning it into a ``WorkspaceLockError``
        would terminally fail a task for no reason a retry could fix. The
        blocking wait is still crash-safe and cannot hang forever on a dead
        holder: the kernel drops an ``flock`` the instant its owning process
        exits (including via ``SIGKILL``), which immediately wakes this call
        rather than requiring it to poll.
        """
        fd = os.open(str(self.prune_lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            raise
        try:
            yield
        finally:
            self._release_fd(fd)

    def _prune_worktrees(self) -> None:
        with self._prune_lock():
            self._prune_worktrees_unlocked()

    def _prune_worktrees_unlocked(self) -> None:
        """``git worktree prune`` without acquiring the admin lock.

        Only safe to call from code that already holds ``_prune_lock``;
        ``flock`` is per-descriptor, so re-entering would deadlock.
        """
        _run_git(self.source_repo, ["worktree", "prune"])

    # -- worktree lifecycle -------------------------------------------

    def _find_registered_worktree(self) -> dict[str, str] | None:
        completed = _run_git(self.source_repo, ["worktree", "list", "--porcelain"])
        for entry in _parse_worktree_list(completed.stdout):
            worktree_path = entry.get("worktree")
            if worktree_path is None:
                continue
            if Path(worktree_path).resolve() == self.path:
                return entry
        return None

    def _create_worktree(self) -> None:
        base_ref = _run_git(self.source_repo, ["rev-parse", "HEAD"]).stdout.strip()
        result = _run_git(
            self.source_repo,
            ["worktree", "add", "-b", self.branch_name, str(self.path), base_ref],
            check=False,
        )
        if result.returncode != 0:
            # The branch may already exist from a previous partial attempt;
            # fall back to attaching the worktree to the existing branch.
            retry = _run_git(
                self.source_repo,
                ["worktree", "add", str(self.path), self.branch_name],
                check=False,
            )
            if retry.returncode != 0:
                raise WorkspaceError(
                    "failed to create worktree for "
                    f"{self.work_item_id!r}: {result.stderr.strip()} / {retry.stderr.strip()}"
                )

    def _load_or_compute_base_commit(self) -> None:
        """Resolve this workspace's base commit, repairing stale metadata.

        A recorded base commit can become unresolvable (e.g. the source repo
        was re-created, history rewritten, or the object pruned). Rather than
        producing a silently wrong diff later, the stored value is verified
        and recomputed when invalid.
        """
        if self._meta_path.exists():
            try:
                data = json.loads(self._meta_path.read_text())
                recorded = data["base_commit"]
            except (json.JSONDecodeError, KeyError, TypeError):
                recorded = None
            if isinstance(recorded, str) and self._commit_exists(recorded):
                self.base_commit = recorded
                return

        base = _run_git(self.source_repo, ["merge-base", "HEAD", self.branch_name]).stdout.strip()
        if not self._commit_exists(base):
            raise WorkspaceError(
                f"computed base commit {base!r} for {self.branch_name} does not resolve "
                f"in {self.source_repo}"
            )
        self._meta_path.write_text(json.dumps({"base_commit": base, "branch": self.branch_name}))
        self.base_commit = base

    def _commit_exists(self, commit: str) -> bool:
        if not commit:
            return False
        completed = _run_git(
            self.source_repo,
            ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
            check=False,
        )
        return completed.returncode == 0

    def prepare(self) -> Path:
        """Create or restore the worktree for this work item.

        Idempotent: calling this repeatedly against an already registered
        and present worktree returns the same path without side effects
        beyond recording the base commit once.

        The whole inspect/prune/create sequence runs under the per-source-repo
        worktree administration lock, so two concurrently dispatched runs
        against the same repository can never interleave repository-global
        ``git worktree`` metadata updates.
        """
        with self._prune_lock():
            return self._prepare_locked()

    def _prepare_locked(self) -> Path:
        registered = self._find_registered_worktree()

        if registered is not None:
            if self.path.exists():
                self._load_or_compute_base_commit()
                return self.path
            # Registered administratively but missing on disk: prune stale
            # metadata and recreate safely.
            self._prune_worktrees_unlocked()
        elif self.path.exists():
            raise WorkspaceSafetyError(
                f"{self.path} exists but is not a registered Git worktree for "
                f"{self.source_repo}; refusing to overwrite it"
            )

        self._create_worktree()
        self._load_or_compute_base_commit()
        return self.path

    def collect_evidence(self) -> WorkspaceEvidence:
        """Stage all changes (including untracked files) and diff the staged
        tree against this workspace's recorded ``base_commit``.

        Diffing against the base commit (rather than the workspace ``HEAD``)
        keeps changes that a previous attempt already committed inside the
        worktree visible alongside uncommitted repair edits, so controller
        evidence always describes the *whole* change since the run started.
        Never commits.
        """
        if self.base_commit is None:
            self._load_or_compute_base_commit()
        assert self.base_commit is not None

        _run_git(self.path, ["add", "-A"])
        diff = _run_git(self.path, ["diff", "--cached", self.base_commit]).stdout
        names_output = _run_git(
            self.path, ["diff", "--cached", "--name-only", self.base_commit]
        ).stdout
        changed_files = [line for line in names_output.splitlines() if line]
        return WorkspaceEvidence(changed_files=changed_files, diff=diff)

    def cleanup(self, force: bool = False) -> None:
        """Remove the worktree. Only called explicitly; never part of the
        default workflow. Refuses to act on paths outside the workspace
        root and never touches the source repository itself."""
        _ensure_strictly_within(self.path, self.workspace_root)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(self.path))
        _run_git(self.source_repo, args)
        if self._meta_path.exists():
            self._meta_path.unlink()
