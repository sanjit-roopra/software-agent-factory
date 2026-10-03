"""Apply a toolchain setup plan in a factory worktree (ADR-034).

A setup run works in its own worktree, keyed by the source HEAD commit, under
the per-work-item lock. It refuses a worktree that is not clean at its base,
so a kept worktree from a failed run is never built on. The add commands
change only the manifest and the lockfile: they install nothing, and the
JavaScript ones run no package scripts. Python locking can still run the
project's build backend to read package metadata. The plan is recorded in
``.factory/setup.json`` without following a symbolic link that the
repository might hold.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .atomic_write import write_text_atomic
from .command_probe import CommandRunner, ProbeLimits, command_failure_reason
from .models import SetupState, ToolchainSetupPlan
from .publishing import PublishResult
from .repository_profile import profile_repository
from .toolchain import inventory_toolchain
from .toolchain_setup import plan_toolchain_setup
from .workspace import GitWorktreeWorkspace, WorkspaceError, WorkspaceLockError

logger = logging.getLogger(__name__)

SETUP_RECORD_DIR = ".factory"
SETUP_RECORD_NAME = "setup.json"
SETUP_WORK_ITEM_PREFIX = "SETUP-"
#: Setup worktree names: the prefix and the first 12 characters of the HEAD commit.
SETUP_WORKTREE_NAME = re.compile(r"^SETUP-[0-9a-f]{12}$")
NOT_AT_BASE = "setup worktree is not clean at its base commit"
LOCKED = "another setup run holds the lock"
HEAD_MOVED = "the source HEAD moved while the setup worktree was prepared"


class SetupError(Exception):
    """A setup run could not start or could not write its record."""


@dataclass(frozen=True)
class SetupOutcome:
    """What applying a setup plan did."""

    applied: tuple[str, ...]
    failed_command: str | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.failed_command is None) != (self.failure_reason is None):
            raise ValueError("a failed command and its reason come together")

    @property
    def succeeded(self) -> bool:
        return self.failed_command is None


@dataclass(frozen=True)
class SetupRunResult:
    """The plan of a setup run, where it ran and what happened."""

    plan: ToolchainSetupPlan
    worktree: Path
    branch: str
    outcome: SetupOutcome
    base_commit: str


def run_toolchain_setup(
    source_repo: Path,
    data_dir: Path,
    branch_prefix: str,
    command_runner: CommandRunner,
    limits: ProbeLimits,
    head_commit: str,
) -> SetupRunResult:
    """Plan and apply the setup in a worktree at ``head_commit`` of ``source_repo``."""

    try:
        workspace = GitWorktreeWorkspace(
            data_dir,
            source_repo,
            f"{SETUP_WORK_ITEM_PREFIX}{head_commit[:12]}",
            branch_prefix=branch_prefix,
        )
        with workspace:
            worktree = workspace.prepare()
            # Refuse a worktree that a failed run kept, and one created after
            # the source HEAD moved away from the commit this run was asked for.
            if workspace.base_commit != head_commit:
                raise SetupError(f"{HEAD_MOVED}: {worktree}")
            if not workspace.is_at_clean_base():
                raise SetupError(f"{NOT_AT_BASE}: {worktree}")
            profile = profile_repository(worktree)
            plan = plan_toolchain_setup(inventory_toolchain(worktree, profile), profile)
            outcome = apply_toolchain_setup(plan, command_runner, worktree, limits)
    except WorkspaceLockError as exc:
        raise SetupError(f"{LOCKED}: {exc}") from exc
    except (WorkspaceError, OSError) as exc:
        raise SetupError(str(exc)) from exc
    return SetupRunResult(plan, worktree, workspace.branch_name, outcome, head_commit)


def apply_toolchain_setup(
    plan: ToolchainSetupPlan,
    command_runner: CommandRunner,
    worktree: Path,
    limits: ProbeLimits,
) -> SetupOutcome:
    """Run the plan's add commands in order and record the plan in the worktree.

    An empty plan runs nothing and writes nothing. A failed command stops the
    setup, writes no record and leaves the worktree for inspection.
    """

    if plan.is_empty:
        return SetupOutcome(applied=())
    applied: list[str] = []
    for command in plan.commands:
        report = command_runner.run(
            [command],
            worktree,
            limits.timeout_seconds,
            env_passthrough=limits.env_passthrough,
            capture_bytes=limits.capture_bytes,
        )
        if not report.passed:
            return SetupOutcome(
                tuple(applied),
                failed_command=command,
                failure_reason=command_failure_reason(report, "during setup"),
            )
        applied.append(command)
    write_setup_record(worktree, plan)
    return SetupOutcome(tuple(applied))


def write_setup_record(worktree: Path, plan: ToolchainSetupPlan) -> Path:
    """Write ``.factory/setup.json`` without following symbolic links.

    The worktree is a checkout of the target repository, which can hold a
    symbolic link at ``.factory`` or at the record path.
    """

    directory = worktree / SETUP_RECORD_DIR
    if directory.is_symlink():
        raise SetupError(f"refusing to write through a symbolic link: {SETUP_RECORD_DIR}")
    try:
        directory.mkdir(exist_ok=True)
        # Open the directory once and write relative to it, so a later swap of
        # the directory for a symbolic link cannot redirect the write.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise SetupError(f"cannot use {SETUP_RECORD_DIR}: {exc}") from exc
    try:
        descriptor = os.open(
            SETUP_RECORD_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise SetupError(f"cannot write {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}: {exc}") from exc
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SetupError(f"not a regular file: {SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}")
        handle.write(plan.model_dump_json(indent=2) + "\n")
    return directory / SETUP_RECORD_NAME


def source_state(repo: Path) -> tuple[str, bool]:
    """Return the HEAD commit of ``repo`` and whether its checkout has changes."""
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
        status = _git(repo, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SetupError(f"not a Git repository with a commit: {repo}") from exc
    return head, bool(status.strip())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


#: The only paths a setup pull request may change.
SETUP_ALLOWED_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        f"{SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}",
    }
)

SETUP_TITLE = "Add missing development tools"
SETUP_COMMIT_SUBJECT = "chore: add missing development tools"
SETUP_STATE_DIR = "setup-state"


class SetupPublisher(Protocol):
    """The part of ``PullRequestPublisher`` a setup run uses."""

    def resolve_base_branch(self, source_repo: Path) -> str: ...

    def publish(
        self,
        *,
        workspace_path: Path,
        branch_name: str,
        base_branch: str,
        commit_message: str,
        title: str,
        body: str,
    ) -> PublishResult: ...


def publish_setup(
    result: SetupRunResult, publisher: SetupPublisher, source_repo: Path
) -> PublishResult:
    """Commit the setup worktree, push its branch and open a pull request.

    The pull request asks for review. The factory never merges it, because it
    changes dependencies.
    """

    if _git(result.worktree, "rev-parse", "HEAD").strip() != result.base_commit:
        raise SetupError("the setup worktree has a commit that the factory did not make")
    unexpected = [
        path for path in changed_paths(result.worktree) if path not in SETUP_ALLOWED_PATHS
    ]
    if unexpected:
        raise SetupError(f"unexpected change in the setup worktree: {unexpected[0]}")
    packages = ", ".join(result.plan.packages)
    body_lines = [
        "The factory found development tools that this repository does not have (ADR-034).",
        "",
        "Added development dependencies:",
        *(f"- `{package}`" for package in result.plan.packages),
    ]
    if result.plan.notes:
        body_lines += ["", "Notes:", *(f"- {note}" for note in result.plan.notes)]
    body_lines += [
        "",
        "The factory checked the changed paths: only the manifest, the lockfile",
        "and `.factory/setup.json` change.",
        "The factory does not merge this pull request. Review the dependency changes, then merge.",
    ]
    return publisher.publish(
        workspace_path=result.worktree,
        branch_name=result.branch,
        base_branch=publisher.resolve_base_branch(source_repo),
        commit_message=f"{SETUP_COMMIT_SUBJECT}\n\nAdded by factory setup: {packages}.",
        title=SETUP_TITLE,
        body="\n".join(body_lines),
    )


class SetupTrigger:
    """Open a setup pull request when the source repository misses tools (ADR-034).

    The trigger plans again only when the source HEAD changes. It plans from
    the checkout first, so most ticks create no worktree. It never opens a
    second pull request for the commands it already proposed, and a failed
    setup is recorded so it does not retry on every tick.
    """

    def __init__(
        self,
        *,
        source_repo: Path,
        data_dir: Path,
        branch_prefix: str,
        limits: ProbeLimits,
        command_runner: CommandRunner,
        publisher: SetupPublisher,
    ) -> None:
        self._source_repo = source_repo
        self._data_dir = data_dir
        self._branch_prefix = branch_prefix
        self._limits = limits
        self._command_runner = command_runner
        self._publisher = publisher
        key = hashlib.sha256(str(source_repo.resolve()).encode("utf-8")).hexdigest()[:16]
        self._state_path = data_dir / SETUP_STATE_DIR / f"{key}.json"

    def tick(self) -> SetupState | None:
        """Run one check. Return the new state, or ``None`` when there was nothing to decide.

        Only one process checks a repository at a time. Another process that
        holds the check skips this tick.
        """

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._state_path.with_suffix(".lock"), "a+") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            return self._tick_locked()

    def _tick_locked(self) -> SetupState | None:
        head, dirty = source_state(self._source_repo)
        previous = self._load()
        if previous is not None and previous.head_commit == head:
            return None
        if not dirty:
            # A clean checkout is HEAD, so plan there first and skip the
            # worktree when there is nothing new to do.
            profile = profile_repository(self._source_repo)
            plan = plan_toolchain_setup(inventory_toolchain(self._source_repo, profile), profile)
            if plan.is_empty:
                return self._save(SetupState(head_commit=head))
            if previous is not None and _already_proposed(previous, plan.commands):
                return self._save(previous.model_copy(update={"head_commit": head}))
        try:
            result = run_toolchain_setup(
                self._source_repo,
                self._data_dir,
                self._branch_prefix,
                self._command_runner,
                self._limits,
                head,
            )
        except SetupError as exc:
            # Record the refusal, so this HEAD is not tried again on every tick.
            return self._record_failure(previous, head, f"setup could not run: {exc}")
        if result.plan.is_empty:
            return self._save(SetupState(head_commit=head))
        if previous is not None and _already_proposed(previous, result.plan.commands):
            return self._save(previous.model_copy(update={"head_commit": head}))
        if not result.outcome.succeeded:
            note = f"setup command failed: {result.outcome.failed_command}"
            return self._record_failure(previous, head, note)
        try:
            published = publish_setup(result, self._publisher, self._source_repo)
        except SetupError as exc:
            return self._record_failure(previous, head, f"setup publish refused: {exc}")
        except Exception as exc:  # noqa: BLE001 - record any publish failure, never retry this HEAD
            return self._record_failure(
                previous, head, f"setup publish failed: {type(exc).__name__}"
            )
        return self._save(
            SetupState(
                head_commit=head,
                commands=result.plan.commands,
                pull_request_url=published.pull_request_url,
            )
        )

    def _record_failure(self, previous: SetupState | None, head: str, note: str) -> SetupState:
        """Record a failure for ``head`` and keep the last proposal for dedupe."""
        base = previous or SetupState(head_commit=head)
        return self._save(base.model_copy(update={"head_commit": head, "note": note}))

    def _load(self) -> SetupState | None:
        try:
            return SetupState.model_validate_json(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            # A damaged state file only loses the dedupe memory. Start again.
            logger.warning("ignoring an unreadable setup state file: %s", self._state_path)
            return None

    def _save(self, state: SetupState) -> SetupState:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._state_path, state.model_dump_json(indent=2) + "\n")
        return state


def _already_proposed(previous: SetupState | None, commands: tuple[str, ...]) -> bool:
    return (
        previous is not None and bool(previous.pull_request_url) and previous.commands == commands
    )


def changed_paths(worktree: Path) -> tuple[str, ...]:
    """Return every path that differs from HEAD in ``worktree``, untracked files included."""
    output = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    entries = iter(entry for entry in output.split("\0") if entry)
    paths: list[str] = []
    for entry in entries:
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            # A rename or copy is followed by its source path, with no status.
            paths.append(next(entries, ""))
    return tuple(sorted(paths))
