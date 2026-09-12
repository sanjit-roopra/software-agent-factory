"""Tests for software_agent_factory.workspace.GitWorktreeWorkspace.

No pyproject.toml / installed package exists yet for this repository, so we
add ``src/`` to ``sys.path`` directly in this file rather than depending on
a conftest.py (out of scope for this ownership boundary).

Test repositories are created fresh under pytest's ``tmp_path`` with local
(not global) Git identity configuration, ``commit.gpgsign`` disabled, and
global/system Git config suppressed via environment variables so these tests
never depend on the developer machine's global Git configuration, commit
signing setup, or hooks.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent
from typing import Sequence

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from software_agent_factory import workspace as workspace_module  # noqa: E402
from software_agent_factory.workspace import (  # noqa: E402
    GitWorktreeWorkspace,
    WorkspaceError,
    WorkspaceLockError,
    WorkspaceSafetyError,
    sanitize_work_item_id,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never depend on global Git config, signing or hooks."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


# -- sanitize_work_item_id -------------------------------------------------


def test_sanitize_preserves_already_safe_ids() -> None:
    assert sanitize_work_item_id("WORK-123") == "WORK-123"
    assert sanitize_work_item_id("issue_42.retry") == "issue_42.retry"


def test_sanitize_appends_stable_hash_when_input_changes() -> None:
    result = sanitize_work_item_id("Fix bug #123!")
    assert result != "Fix bug #123!"
    assert result.startswith("Fix-bug-123")
    # Deterministic: same input always sanitizes identically.
    assert result == sanitize_work_item_id("Fix bug #123!")


def test_sanitize_is_collision_resistant_for_same_unsafe_prefix() -> None:
    a = sanitize_work_item_id("task!!!")
    b = sanitize_work_item_id("task???")
    assert a != b


def test_sanitize_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        sanitize_work_item_id("   ")


# -- prepare() --------------------------------------------------------------


def test_prepare_creates_worktree_and_preserves_source_repo(
    source_repo: Path, data_dir: Path
) -> None:
    head_before = _git(source_repo, "rev-parse", "HEAD").strip()

    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-1")
    path = ws.prepare()

    assert path.exists()
    assert (path / "README.md").exists()
    assert ws.base_commit == head_before

    # Source repo must remain untouched.
    assert _git(source_repo, "status", "--porcelain") == ""
    head_after = _git(source_repo, "rev-parse", "HEAD").strip()
    assert head_after == head_before


def test_prepare_is_idempotent(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-2")
    first_path = ws.prepare()
    first_base = ws.base_commit

    second_path = ws.prepare()

    assert second_path == first_path
    assert ws.base_commit == first_base

    listing = _git(source_repo, "worktree", "list", "--porcelain")
    assert listing.count(str(first_path.resolve())) == 1


def test_prepare_rejects_unsafe_existing_directory(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-3")
    ws.path.mkdir(parents=True)
    (ws.path / "leftover.txt").write_text("do not touch\n")

    with pytest.raises(WorkspaceSafetyError):
        ws.prepare()

    # The unsafe directory must not have been deleted.
    assert ws.path.exists()
    assert (ws.path / "leftover.txt").read_text() == "do not touch\n"


def test_prepare_recovers_from_stale_missing_worktree(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-4")
    path = ws.prepare()
    base_commit = ws.base_commit

    # Simulate a crash that deleted the worktree directory without telling
    # Git, leaving stale administrative metadata behind.
    shutil.rmtree(path)
    assert not path.exists()

    ws2 = GitWorktreeWorkspace(data_dir, source_repo, "WORK-4")
    recovered_path = ws2.prepare()

    assert recovered_path == path
    assert recovered_path.exists()
    assert ws2.base_commit == base_commit


# -- collect_evidence() ------------------------------------------------------


def test_collect_evidence_includes_untracked_and_modified_files(
    source_repo: Path, data_dir: Path
) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-5")
    path = ws.prepare()

    log_before = _git(path, "log", "--oneline")

    (path / "README.md").write_text("hello\nmodified\n")
    (path / "new_file.txt").write_text("brand new\n")

    evidence = ws.collect_evidence()

    assert "README.md" in evidence.changed_files
    assert "new_file.txt" in evidence.changed_files
    assert "new_file.txt" in evidence.diff
    assert "modified" in evidence.diff

    # collect_evidence must never commit.
    log_after = _git(path, "log", "--oneline")
    assert log_after == log_before


def test_collect_evidence_includes_committed_and_uncommitted_changes(
    source_repo: Path, data_dir: Path
) -> None:
    """A repair attempt must not lose evidence of an earlier committed change.

    Diffing the index against the workspace HEAD would hide anything a
    previous attempt already committed inside the worktree, so evidence is
    always taken against the recorded base commit.
    """
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-EVIDENCE")
    path = ws.prepare()

    (path / "committed.txt").write_text("from the first attempt\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "first attempt")

    (path / "uncommitted.txt").write_text("from the repair attempt\n")

    evidence = ws.collect_evidence()

    assert sorted(evidence.changed_files) == ["committed.txt", "uncommitted.txt"]
    assert "from the first attempt" in evidence.diff
    assert "from the repair attempt" in evidence.diff


def test_collect_evidence_recovers_when_stored_base_commit_is_unresolvable(
    source_repo: Path, data_dir: Path
) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BASE")
    path = ws.prepare()
    meta_path = data_dir.resolve() / "workspaces" / f"{ws.key}.meta.json"
    meta_path.write_text(json.dumps({"base_commit": "0" * 40, "branch": ws.branch_name}))

    recovered = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BASE")
    recovered.prepare()

    assert recovered.base_commit == _git(source_repo, "rev-parse", "HEAD").strip()
    assert json.loads(meta_path.read_text())["base_commit"] == recovered.base_commit

    (path / "later.txt").write_text("later\n")
    assert "later.txt" in recovered.collect_evidence().changed_files


def test_file_line_count_handles_non_utf8_files(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BINARY")
    path = ws.prepare()
    (path / "asset.bin").write_bytes(b"\x89PNG\r\n\x1a\n\xff\x00")

    evidence = ws.collect_evidence()

    assert evidence.tree_sha is not None
    assert ws.file_line_count(evidence.tree_sha, "asset.bin") == 3


def test_collect_evidence_subprocess_calls_reduced(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """collect_evidence must execute exactly 3 git subprocess calls:
    add -A, write-tree, and a combined diff --patch-with-raw -z,
    eliminating the redundant diff --name-only call.
    """
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-CALLS")
    path = ws.prepare()
    (path / "file.txt").write_text("content\n")

    executed_commands: list[list[str]] = []
    original_run_git = workspace_module._run_git

    def recording_run_git(
        cwd: Path, args: Sequence[str], check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        executed_commands.append(list(args))
        return original_run_git(cwd, args, check)

    monkeypatch.setattr(workspace_module, "_run_git", recording_run_git)

    evidence = ws.collect_evidence()

    assert len(executed_commands) == 3
    assert executed_commands[0] == ["add", "-A"]
    assert executed_commands[1] == ["write-tree"]
    assert executed_commands[2] == [
        "diff",
        "--patch-with-raw",
        "-z",
        "--find-copies=1%",
        "--find-copies-harder",
        ws.base_commit,
        evidence.tree_sha,
    ]
    assert evidence.changed_files == ["file.txt"]
    assert "content" in evidence.diff


def test_collect_evidence_handles_spaces_in_paths(source_repo: Path, data_dir: Path) -> None:
    """File paths with spaces must be preserved without quoting or splitting."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-SPACES")
    path = ws.prepare()

    spaced_dir = path / "spaced directory"
    spaced_dir.mkdir()
    (spaced_dir / "spaced file.txt").write_text("hello in sub\n")
    (path / "another space file.txt").write_text("root spaced\n")

    evidence = ws.collect_evidence()

    assert "another space file.txt" in evidence.changed_files
    assert "spaced directory/spaced file.txt" in evidence.changed_files
    assert "another space file.txt" in evidence.diff
    assert "spaced directory/spaced file.txt" in evidence.diff


def test_collect_evidence_handles_renames(source_repo: Path, data_dir: Path) -> None:
    """Renamed files must be tracked by their destination path and diff must record the rename."""
    # Commit the initial file into source_repo so it exists in base_commit
    (source_repo / "file_to_rename.txt").write_text("content to be renamed\n" * 10)
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-m", "add file to rename in base")

    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-RENAMES")
    path = ws.prepare()

    _git(path, "mv", "file_to_rename.txt", "renamed_destination.txt")

    evidence = ws.collect_evidence()

    assert evidence.changed_files == ["file_to_rename.txt", "renamed_destination.txt"]
    assert "rename from file_to_rename.txt" in evidence.diff
    assert "rename to renamed_destination.txt" in evidence.diff

    # Rename with spaces
    _git(path, "mv", "renamed_destination.txt", "renamed with spaces.txt")
    evidence_spaces = ws.collect_evidence()

    assert evidence_spaces.changed_files == ["file_to_rename.txt", "renamed with spaces.txt"]
    assert "rename to renamed with spaces.txt" in evidence_spaces.diff


def test_collect_evidence_tracks_protected_source_rename(source_repo: Path, data_dir: Path) -> None:
    """Renaming a protected source file such as .env to an allowed destination
    must include the protected source path in changed_files."""
    (source_repo / ".env").write_text("SECRET=123\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-m", "add .env in base")

    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PROTECTED-RENAME")
    path = ws.prepare()

    _git(path, "mv", ".env", "safe.txt")

    evidence = ws.collect_evidence()
    assert evidence.changed_files == [".env", "safe.txt"]
    assert "rename from .env" in evidence.diff
    assert "rename to safe.txt" in evidence.diff


def test_collect_evidence_tracks_protected_source_copy(source_repo: Path, data_dir: Path) -> None:
    """Copying a protected source file such as .env to an allowed destination
    must include both the protected source path and destination path in changed_files."""
    (source_repo / ".env").write_text("SECRET=123\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-m", "add .env in base")

    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PROTECTED-COPY")
    path = ws.prepare()

    shutil.copy(path / ".env", path / "safe.txt")

    evidence = ws.collect_evidence()
    assert evidence.changed_files == [".env", "safe.txt"]
    assert "copy from .env" in evidence.diff
    assert "copy to safe.txt" in evidence.diff


def test_collect_evidence_tracks_padded_and_modified_protected_copy(
    source_repo: Path, data_dir: Path
) -> None:
    """Copying a protected source file (.env) to an allowed destination with
    modest modifications and padding must still detect the copy and include
    the protected source path in changed_files."""
    (source_repo / ".env").write_text("SECRET=123\nAPI_KEY=xyz\nTOKEN=abc\n")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-m", "add .env in base")

    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PROTECTED-PADDED-COPY")
    path = ws.prepare()

    (path / "safe.txt").write_text(
        "# Header comments\n" * 10
        + "SECRET=123\nAPI_KEY=xyz_mod\nTOKEN=abc\n"
        + "# Footer comments\n" * 10
    )

    evidence = ws.collect_evidence()
    assert ".env" in evidence.changed_files
    assert "safe.txt" in evidence.changed_files
    assert "copy from .env" in evidence.diff
    assert "copy to safe.txt" in evidence.diff


def test_collect_evidence_handles_status_like_filenames(source_repo: Path, data_dir: Path) -> None:
    """Legal filenames matching status tokens like M, A0, R100 must be properly tracked."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-STATUS-FILENAMES")
    path = ws.prepare()

    (path / "M").write_text("file named M\n")
    (path / "A0").write_text("file named A0\n")
    (path / "R100").write_text("file named R100\n")
    (path / "C100").write_text("file named C100\n")

    evidence = ws.collect_evidence()
    assert set(evidence.changed_files) == {"M", "A0", "R100", "C100"}
    assert len(evidence.changed_files) == 4
    for name in ("M", "A0", "R100", "C100"):
        assert name in evidence.diff


def test_collect_evidence_handles_untracked_files(source_repo: Path, data_dir: Path) -> None:
    """Untracked files created in the worktree must be staged and frozen into the tree."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-UNTRACKED")
    path = ws.prepare()

    (path / "untracked_one.txt").write_text("untracked content 1\n")
    (path / "untracked_two.txt").write_text("untracked content 2\n")

    evidence = ws.collect_evidence()

    assert "untracked_one.txt" in evidence.changed_files
    assert "untracked_two.txt" in evidence.changed_files
    assert "new file mode" in evidence.diff
    assert "untracked content 1" in evidence.diff
    assert "untracked content 2" in evidence.diff
    assert evidence.tree_sha is not None
    assert ws.file_line_count(evidence.tree_sha, "untracked_one.txt") == 1
    assert ws.file_line_count(evidence.tree_sha, "untracked_two.txt") == 1


def test_collect_evidence_handles_empty_diff(source_repo: Path, data_dir: Path) -> None:
    """When no changes exist, collect_evidence returns empty changed_files and empty diff."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-EMPTY")
    ws.prepare()

    evidence = ws.collect_evidence()

    assert evidence.changed_files == []
    assert evidence.diff == ""
    assert evidence.tree_sha is not None


def test_parse_patch_with_raw_unit() -> None:
    """Unit test covering parsing corner cases including malformed tokens."""
    # Empty
    assert workspace_module._parse_patch_with_raw("") == ([], "")

    # Standard modified
    raw_mod = ":100644 100644 1111111 2222222 M\0file.txt\0\0diff --git a/file.txt b/file.txt\n"
    files, diff = workspace_module._parse_patch_with_raw(raw_mod)
    assert files == ["file.txt"]
    assert diff == "diff --git a/file.txt b/file.txt\n"

    # Copy / Rename
    raw_ren = ":100644 100644 1111111 1111111 R100\0src.txt\0dst.txt\0\0diff --git ...\n"
    files, diff = workspace_module._parse_patch_with_raw(raw_ren)
    assert files == ["src.txt", "dst.txt"]

    # Status-like filenames (M, A0, R100, C100) are treated strictly as paths
    raw_status_names = (
        ":000000 100644 0000000 1111111 A\0M\0"
        ":000000 100644 0000000 2222222 A\0A0\0"
        ":000000 100644 0000000 3333333 A\0R100\0"
        ":100644 100644 4444444 5555555 M\0C100\0"
        "\0diff --git ...\n"
    )
    files, diff = workspace_module._parse_patch_with_raw(raw_status_names)
    assert files == ["M", "A0", "R100", "C100"]

    # Deduplication and deterministic order preservation across records
    raw_dup = (
        ":100644 100644 1111111 1111111 R100\0src.txt\0dst.txt\0"
        ":100644 100644 2222222 3333333 M\0dst.txt\0"
        ":100644 100644 1111111 4444444 C100\0src.txt\0copy.txt\0"
        "\0diff --git ...\n"
    )
    files, diff = workspace_module._parse_patch_with_raw(raw_dup)
    assert files == ["src.txt", "dst.txt", "copy.txt"]

    # Malformed headers
    with pytest.raises(workspace_module.WorkspaceError, match="malformed raw diff header"):
        workspace_module._parse_patch_with_raw("bad header\0file.txt\0\0diff")

    # Truncated record
    with pytest.raises(workspace_module.WorkspaceError, match="truncated raw diff record"):
        workspace_module._parse_patch_with_raw(":100644 100644 1111111 2222222 M")

    # Truncated rename record
    with pytest.raises(
        workspace_module.WorkspaceError, match="truncated raw diff record for rename/copy"
    ):
        workspace_module._parse_patch_with_raw(":100644 100644 1111111 2222222 R100\0src.txt")


# -- locking ------------------------------------------------------------


def test_lock_conflict_fails_clearly(source_repo: Path, data_dir: Path) -> None:
    ws1 = GitWorktreeWorkspace(data_dir, source_repo, "WORK-6")
    ws2 = GitWorktreeWorkspace(data_dir, source_repo, "WORK-6")

    ws1.acquire_lock()
    try:
        with pytest.raises(WorkspaceLockError):
            ws2.acquire_lock()
    finally:
        ws1.release_lock()

    # Once released, another owner can acquire it.
    ws2.acquire_lock()
    ws2.release_lock()


def test_context_manager_releases_lock(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-7")
    with ws:
        assert ws.lock_path.exists()
        assert ws.lock_held is True
    assert not ws.lock_path.exists()
    assert ws.lock_held is False


def test_lock_is_reacquirable_after_a_stale_lock_file_is_left_behind(
    source_repo: Path, data_dir: Path
) -> None:
    """A leftover lock *file* must never block a new run: only a live flock
    conveys ownership."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-STALE")
    ws.lock_path.write_text("999999")

    ws.acquire_lock()
    try:
        assert ws.lock_held is True
    finally:
        ws.release_lock()


def test_lock_is_released_when_the_owning_process_is_sigkilled(
    source_repo: Path, data_dir: Path
) -> None:
    """The kernel drops an flock when its owner dies, so a crashed run
    (SIGKILL, power loss) leaves a recoverable workspace."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-CRASH")
    ready_marker = data_dir / "child-ready"

    child_source = dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(_SRC)!r})
        from pathlib import Path
        from software_agent_factory.workspace import GitWorktreeWorkspace

        workspace = GitWorktreeWorkspace(
            Path({str(data_dir)!r}), Path({str(source_repo)!r}), "WORK-CRASH"
        )
        workspace.acquire_lock()
        Path({str(ready_marker)!r}).write_text("locked")
        time.sleep(60)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", child_source])
    try:
        deadline = time.monotonic() + 20
        while not ready_marker.exists():
            assert child.poll() is None, "lock-holding child exited early"
            assert time.monotonic() < deadline, "child never acquired the lock"
            time.sleep(0.05)

        with pytest.raises(WorkspaceLockError):
            ws.acquire_lock()

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=20)

        # No graceful release ran, yet the workspace must be usable again.
        ws.acquire_lock()
        ws.release_lock()
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=20)


def test_prune_is_serialized_per_source_repository(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent ``git worktree prune`` runs would race on shared metadata,
    so pruning must happen under an exclusive per-source-repo lock."""
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PRUNE")
    other = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PRUNE-OTHER")
    assert ws.prune_lock_path == other.prune_lock_path

    observed: list[bool] = []
    original_run_git = workspace_module._run_git

    def recording_run_git(
        cwd: Path, args: Sequence[str], check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if list(args)[:2] == ["worktree", "prune"]:
            observed.append(_prune_lock_is_held(ws.prune_lock_path))
        return original_run_git(cwd, args, check)

    monkeypatch.setattr(workspace_module, "_run_git", recording_run_git)

    path = ws.prepare()
    shutil.rmtree(path)
    GitWorktreeWorkspace(data_dir, source_repo, "WORK-PRUNE").prepare()

    assert observed == [True]


def test_prune_lock_is_shared_by_main_checkout_and_linked_worktree(
    source_repo: Path,
    data_dir: Path,
) -> None:
    project = GitWorktreeWorkspace(data_dir, source_repo, "PROJECT-LOCK")
    project_path = project.prepare()
    child = GitWorktreeWorkspace(data_dir, project_path, "CHILD-LOCK")

    assert project.prune_lock_path == child.prune_lock_path


def test_prune_lock_is_shared_across_factory_data_directories(
    source_repo: Path,
    tmp_path: Path,
) -> None:
    first = GitWorktreeWorkspace(tmp_path / "first-data", source_repo, "FIRST")
    second = GitWorktreeWorkspace(tmp_path / "second-data", source_repo, "SECOND")

    assert first.prune_lock_path == second.prune_lock_path
    common_dir = Path(_git(source_repo, "rev-parse", "--git-common-dir").strip())
    if not common_dir.is_absolute():
        common_dir = source_repo / common_dir
    assert first.prune_lock_path.is_relative_to(common_dir.resolve())


def test_prune_lock_waits_for_a_slow_holder_instead_of_failing(
    source_repo: Path, data_dir: Path
) -> None:
    """Regression test for the concurrency=2 failure mode this module used
    to have: a slow concurrent worktree admin op (e.g. a slow checkout)
    holding the per-source-repo prune lock past the old hard-coded 10s
    timeout must never terminally fail *this* run. The lock now blocks
    (uninterruptibly, no polling deadline) until the other holder releases
    it, then proceeds normally.

    Coordination uses ``threading.Event`` rather than a real ``sleep(10)``
    so the regression is exercised deterministically and fast: the waiter
    is proven to still be blocked (not failed) while the lock is held, then
    proven to succeed the instant it is released.
    """
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-PRUNE-WAIT")

    locked = threading.Event()
    release = threading.Event()
    blocker_fd = os.open(str(ws.prune_lock_path), os.O_CREAT | os.O_RDWR, 0o644)

    def hold_lock() -> None:
        fcntl.flock(blocker_fd, fcntl.LOCK_EX)
        locked.set()
        # Stands in for a checkout slower than the old 10s timeout, without
        # the test itself ever sleeping that long: the waiter thread below
        # only unblocks once told to via `release`.
        release.wait(timeout=5)
        fcntl.flock(blocker_fd, fcntl.LOCK_UN)
        os.close(blocker_fd)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert locked.wait(timeout=5), "test setup: blocker never acquired the lock"

    waiter_done = threading.Event()
    waiter_errors: list[BaseException] = []

    def wait_for_lock() -> None:
        try:
            with ws._prune_lock():
                pass
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            waiter_errors.append(exc)
        finally:
            waiter_done.set()

    waiter = threading.Thread(target=wait_for_lock)
    waiter.start()
    try:
        # While the other holder is still active, the waiter must remain
        # blocked rather than raising WorkspaceLockError from a timeout.
        assert not waiter_done.wait(timeout=0.3)

        release.set()
        holder.join(timeout=5)
        assert waiter_done.wait(timeout=5), "waiter never acquired the lock after release"
    finally:
        waiter.join(timeout=5)

    assert waiter_errors == []


def _prune_lock_is_held(lock_path: Path) -> bool:
    """True when some other process/owner currently holds the prune flock."""
    probe = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(probe, fcntl.LOCK_UN)
        return False
    finally:
        os.close(probe)


# -- cleanup() ------------------------------------------------------------


def test_cleanup_removes_worktree(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-8")
    path = ws.prepare()
    assert path.exists()

    ws.cleanup(force=True)

    assert not path.exists()
    listing = _git(source_repo, "worktree", "list", "--porcelain")
    assert str(path.resolve()) not in listing


def test_cleanup_refuses_path_outside_workspace_root(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-9")
    ws.prepare()

    # Simulate a defect/attempted misuse that points ``path`` outside the
    # workspace root; cleanup must refuse rather than touching source_repo.
    ws.path = source_repo

    with pytest.raises(WorkspaceSafetyError):
        ws.cleanup(force=True)

    # Source repo must remain completely intact.
    assert source_repo.exists()
    assert (source_repo / "README.md").exists()
    assert _git(source_repo, "status", "--porcelain") == ""


def test_workspace_and_lock_paths_are_root_contained(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-10")
    assert ws.path.is_relative_to((data_dir / "workspaces").resolve())
    assert ws.lock_path.is_relative_to((data_dir / "locks").resolve())


# -- batch line counts ----------------------------------------------------


def test_file_line_counts_batch_correctness(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BATCH-CORRECTNESS")
    path = ws.prepare()

    (path / "one.txt").write_text("no newline at end")
    (path / "two.txt").write_text("line 1\nline 2\n")
    (path / "empty.txt").write_text("")
    (path / "with space.txt").write_text("a\nb\nc\n")
    sub = path / "subdir"
    sub.mkdir()
    (sub / "nested.py").write_text("1\n2\n3\n4\n5\n")
    (path / "binary.bin").write_bytes(b"\x00\x01\n\x02\n\x03")

    evidence = ws.collect_evidence()
    assert evidence.tree_sha is not None

    paths = [
        "one.txt",
        "two.txt",
        "empty.txt",
        "with space.txt",
        "subdir/nested.py",
        "binary.bin",
        "missing.txt",
        "subdir",
    ]

    counts = ws.file_line_counts(evidence.tree_sha, paths)
    assert counts == {
        "one.txt": 1,
        "two.txt": 2,
        "empty.txt": 0,
        "with space.txt": 3,
        "subdir/nested.py": 5,
        "binary.bin": 3,
        "missing.txt": None,
        "subdir": None,
    }
    # batch_file_line_counts alias returns identical result
    assert ws.batch_file_line_counts(evidence.tree_sha, paths) == counts


def test_file_line_counts_subprocess_calls_bounded_and_deduplicated(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BATCH-SUBPROCESS")
    path = ws.prepare()

    (path / "file_a.txt").write_text("alpha 1\nalpha 2\n")
    (path / "file_b.txt").write_text("beta 1\nbeta 2\nbeta 3\n")

    evidence = ws.collect_evidence()
    assert evidence.tree_sha is not None

    executed_commands: list[list[str]] = []
    original_run_git_bytes = workspace_module._run_git_bytes

    def recording_run_git_bytes(
        cwd: Path,
        args: Sequence[str],
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        executed_commands.append(list(args))
        return original_run_git_bytes(cwd, args, check=check, input_bytes=input_bytes)

    monkeypatch.setattr(workspace_module, "_run_git_bytes", recording_run_git_bytes)

    # Empty paths must execute 0 git subprocesses
    assert ws.file_line_counts(evidence.tree_sha, []) == {}
    assert len(executed_commands) == 0

    # Query with multiple files, duplicates, and missing file: exactly 1 git subprocess call
    query_paths = [
        "file_a.txt",
        "file_b.txt",
        "file_a.txt",
        "file_b.txt",
        "missing.txt",
    ]
    counts = ws.file_line_counts(evidence.tree_sha, query_paths)

    assert counts == {
        "file_a.txt": 2,
        "file_b.txt": 3,
        "missing.txt": None,
    }
    assert len(executed_commands) == 1
    assert executed_commands[0] == ["cat-file", "--batch", "-z"]


def test_file_line_counts_safety_and_validation(source_repo: Path, data_dir: Path) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-BATCH-SAFETY")
    ws.prepare()
    evidence = ws.collect_evidence()
    assert evidence.tree_sha is not None

    # Invalid tree object format
    with pytest.raises(WorkspaceError, match="invalid Git tree object"):
        ws.file_line_counts("not-a-sha", ["file.txt"])

    # Path traversal and invalid relative path forms
    invalid_paths = [
        "/etc/passwd",
        "../escape.txt",
        "subdir/../escape.txt",
        r"win\path.txt",
        "",
        ".",
        "file\nwith\nnewline.txt",
        "file\0with\0null.txt",
    ]
    for invalid_path in invalid_paths:
        with pytest.raises(WorkspaceError, match="invalid repository-relative path"):
            ws.file_line_counts(evidence.tree_sha, [invalid_path])
