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

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from software_agent_factory import workspace as workspace_module  # noqa: E402
from software_agent_factory.workspace import (  # noqa: E402
    GitWorktreeWorkspace,
    WorkspaceLockError,
    WorkspaceSafetyError,
    sanitize_work_item_id,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
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


def test_prepare_rejects_unsafe_existing_directory(
    source_repo: Path, data_dir: Path
) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-3")
    ws.path.mkdir(parents=True)
    (ws.path / "leftover.txt").write_text("do not touch\n")

    with pytest.raises(WorkspaceSafetyError):
        ws.prepare()

    # The unsafe directory must not have been deleted.
    assert ws.path.exists()
    assert (ws.path / "leftover.txt").read_text() == "do not touch\n"


def test_prepare_recovers_from_stale_missing_worktree(
    source_repo: Path, data_dir: Path
) -> None:
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

    def recording_run_git(cwd: Path, args, check: bool = True):
        if list(args)[:2] == ["worktree", "prune"]:
            observed.append(_prune_lock_is_held(ws.prune_lock_path))
        return original_run_git(cwd, args, check)

    monkeypatch.setattr(workspace_module, "_run_git", recording_run_git)

    path = ws.prepare()
    shutil.rmtree(path)
    GitWorktreeWorkspace(data_dir, source_repo, "WORK-PRUNE").prepare()

    assert observed == [True]


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


def test_cleanup_refuses_path_outside_workspace_root(
    source_repo: Path, data_dir: Path
) -> None:
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


def test_workspace_and_lock_paths_are_root_contained(
    source_repo: Path, data_dir: Path
) -> None:
    ws = GitWorktreeWorkspace(data_dir, source_repo, "WORK-10")
    assert ws.path.is_relative_to((data_dir / "workspaces").resolve())
    assert ws.lock_path.is_relative_to((data_dir / "locks").resolve())
