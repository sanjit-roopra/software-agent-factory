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
from .models import MAX_COMMAND_NOTES, MAX_COMMAND_TEXT_LENGTH, SetupState, ToolchainSetupPlan
from .publishing import PublishResult
from .repository_files import (
    RepositoryFile,
    RepositoryFileError,
    RepositoryFilesPlan,
    is_file_note,
    plan_repository_files,
    write_repository_files,
)
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
            plan, _files = plan_setup(worktree)
            outcome, plan = apply_toolchain_setup(plan, command_runner, worktree, limits)
    except WorkspaceLockError as exc:
        raise SetupError(f"{LOCKED}: {exc}") from exc
    except (WorkspaceError, RepositoryFileError, OSError) as exc:
        raise SetupError(str(exc)) from exc
    return SetupRunResult(plan, worktree, workspace.branch_name, outcome, head_commit)


def plan_setup(root: Path) -> tuple[ToolchainSetupPlan, tuple[RepositoryFile, ...]]:
    """Plan the add commands and the repository files for the tree at ``root``."""
    profile = profile_repository(root)
    inventory = inventory_toolchain(root, profile)
    files = plan_repository_files(root, inventory, profile)
    plan = plan_toolchain_setup(inventory, profile)
    return _with_files(plan, files), files.files


def apply_toolchain_setup(
    plan: ToolchainSetupPlan,
    command_runner: CommandRunner,
    worktree: Path,
    limits: ProbeLimits,
) -> tuple[SetupOutcome, ToolchainSetupPlan]:
    """Run the plan's add commands, write the repository files and record the plan.

    The files are planned again after the commands, so ``AGENTS.md`` and the
    ``pr-gate`` skill name the checks of the tools that the setup just added.
    That second plan decides the files, and the returned plan lists them.
    An empty plan runs nothing and writes nothing. A failed command stops the
    setup, writes no file and no record, and leaves the worktree for inspection.
    """

    if plan.is_empty:
        return SetupOutcome(applied=()), plan
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
            outcome = SetupOutcome(
                tuple(applied),
                failed_command=command,
                failure_reason=command_failure_reason(report, "during setup"),
            )
            return outcome, plan
        applied.append(command)
    # An add command may change only the manifest and the lockfile. Anything
    # else, such as AGENTS.md written by a build backend, stops the setup.
    unexpected = [path for path in changed_paths(worktree) if path not in SETUP_ALLOWED_PATHS]
    if unexpected:
        raise SetupError(f"an add command changed an unexpected path: {unexpected[0]}")
    profile = profile_repository(worktree)
    files = plan_repository_files(worktree, inventory_toolchain(worktree, profile), profile)
    write_repository_files(worktree, files.files)
    final = _with_files(plan, files)
    write_setup_record(worktree, final)
    return SetupOutcome(tuple(applied)), final


def _with_files(plan: ToolchainSetupPlan, files: RepositoryFilesPlan) -> ToolchainSetupPlan:
    return ToolchainSetupPlan.model_validate(
        {
            **plan.model_dump(),
            "files": files.paths,
            # Replace the notes of an earlier file plan, and stay within the limit.
            "notes": tuple(
                dict.fromkeys(
                    (*(note for note in plan.notes if not is_file_note(note)), *files.notes)
                )
            )[:MAX_COMMAND_NOTES],
        }
    )


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


#: The manifests and lockfiles a setup pull request may change, besides the
#: setup record and the repository files that the plan lists.
SETUP_ALLOWED_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        f"{SETUP_RECORD_DIR}/{SETUP_RECORD_NAME}",
    }
)

SETUP_TITLE = "Set up development tools and agent skills"
SETUP_COMMIT_SUBJECT = "chore: set up development tools and agent skills"
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
        path
        for path in changed_paths(result.worktree)
        if path not in SETUP_ALLOWED_PATHS and path not in result.plan.files
    ]
    if unexpected:
        raise SetupError(f"unexpected change in the setup worktree: {unexpected[0]}")
    body_lines = [
        "The factory set up development tools and agent skills for this repository (ADR-034).",
    ]
    if result.plan.packages:
        body_lines += [
            "",
            "Added development dependencies:",
            *(f"- `{package}`" for package in result.plan.packages),
        ]
    if result.plan.files:
        body_lines += [
            "",
            "Added agent instructions and skills:",
            *(f"- `{path}`" for path in result.plan.files),
        ]
    if result.plan.notes:
        body_lines += ["", "Notes:", *(f"- {note}" for note in result.plan.notes)]
    body_lines += [
        "",
        "The factory checked the changed paths: only the files above, the manifest,",
        "the lockfile and `.factory/setup.json` change.",
        "The factory does not merge this pull request. Review the dependency changes, then merge.",
    ]
    return publisher.publish(
        workspace_path=result.worktree,
        branch_name=result.branch,
        base_branch=publisher.resolve_base_branch(source_repo),
        commit_message=_commit_message(result.plan),
        title=SETUP_TITLE,
        body="\n".join(body_lines),
    )


class SetupTrigger:
    """Open a setup pull request when the source repository misses tools (ADR-034).

    The trigger plans again only when the source HEAD changes. It plans from
    the checkout first, so most ticks create no worktree. It never opens a
    second pull request for the commands and files it already proposed, and a failed
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
            plan, _files = plan_setup(self._source_repo)
            if plan.is_empty:
                return self._save(SetupState(head_commit=head))
            if previous is not None and _already_proposed(previous, plan):
                return self._save(previous.model_copy(update={"head_commit": head, "note": None}))
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
        if previous is not None and _already_proposed(previous, result.plan):
            return self._save(previous.model_copy(update={"head_commit": head, "note": None}))
        if not result.outcome.succeeded:
            note = f"setup command failed: {result.outcome.failed_command}"
            return self._record_failure(previous, head, note)
        try:
            published = publish_setup(result, self._publisher, self._source_repo)
        except SetupError as exc:
            return self._record_failure(previous, head, f"setup publish refused: {exc}")
        except Exception as exc:  # noqa: BLE001 - record any publish failure and never retry this HEAD
            return self._record_failure(
                previous, head, f"setup publish failed: {type(exc).__name__}"
            )
        return self._save(
            SetupState(
                head_commit=head,
                commands=result.plan.commands,
                files=result.plan.files,
                pull_request_url=published.pull_request_url,
            )
        )

    def _record_failure(self, previous: SetupState | None, head: str, note: str) -> SetupState:
        """Record a failure for ``head`` and keep the last proposal for dedupe."""
        base = previous or SetupState(head_commit=head)
        return self._save(
            SetupState.model_validate(
                {
                    **base.model_dump(),
                    "head_commit": head,
                    "note": note[:MAX_COMMAND_TEXT_LENGTH],
                }
            )
        )

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


def _already_proposed(previous: SetupState, plan: ToolchainSetupPlan) -> bool:
    return (
        bool(previous.pull_request_url)
        and previous.commands == plan.commands
        and previous.files == plan.files
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


def _commit_message(plan: ToolchainSetupPlan) -> str:
    lines = [SETUP_COMMIT_SUBJECT, ""]
    if plan.packages:
        lines.append(f"Development dependencies: {', '.join(plan.packages)}.")
    if plan.files:
        lines.append(f"Agent instructions and skills: {', '.join(plan.files)}.")
    return "\n".join(lines)
