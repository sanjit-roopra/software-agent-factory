"""Read-only operational health: stale runs, stale locks, orphaned workspaces.

Split out of :mod:`software_agent_factory.observability`.
:func:`build_operational_health` reuses the same bounded run scan as the
monitoring snapshot. It is strictly non-destructive: a lock probe never
deletes the lock file, and an orphaned workspace is only named, never removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import Field

from .models import ModelBase, UtcDateTime, VersionedModel, WorkflowState
from .observability import (
    DEFAULT_MAX_SCANNED_RUNS,
    DEFAULT_STALE_AFTER,
    RunScanResult,
    RunStoreProtocol,
    _is_stale,
    _last_signal_at,
    _normalize_now,
    process_alive,
    scan_readable_runs,
)
from .run_metrics import _classify_run
from .setup_run import SETUP_WORKTREE_NAME

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux only per AGENTS.md
    fcntl = None  # type: ignore[assignment]

#: Whether this platform supports the ``fcntl.flock`` liveness probe
#: :func:`build_operational_health` uses to detect a stale (holder-less)
#: lock file. Windows has no ``fcntl``; on such a platform lock staleness
#: checks are skipped explicitly (``lock_check_supported=False``) rather
#: than raising or silently reporting zero stale locks as if they were
#: checked.
_FCNTL_AVAILABLE = fcntl is not None

#: Prefix of the durable per-source-repository worktree administration lock
#: created by ``workspace.GitWorktreeWorkspace._prune_lock``. Duplicated as a
#: literal (rather than imported) because
#: this read-only module must not pull in the workspace/orchestration module
#: tree. See :func:`_find_stale_locks` for why these files are never reported
#: as stale.
PRUNE_LOCK_PREFIX = "prune-"


class StaleRunFinding(ModelBase):
    """One non-terminal run whose most recent heartbeat/activity/update
    timestamp is older than ``stale_after`` -- the same staleness rule
    ``RunSummary.is_stale``/``RunStateCounts.stale_active`` use, surfaced
    here as an individually actionable health finding rather than folded
    into an aggregate count."""

    run_id: str
    work_item_id: str
    state: WorkflowState
    idle_seconds: float = Field(ge=0.0)
    workspace_path: str | None = None


class StaleLockFinding(ModelBase):
    """One workspace lock file under ``data_dir/locks`` that exists but has
    no live holder: a non-blocking ``flock`` probe against it succeeded, was
    released immediately, and the file itself was left untouched on disk --
    this module never deletes a lock file. ``lock_name`` is just the file
    name (e.g. ``<workspace-key>.lock``); no repository content or path
    outside ``data_dir`` is exposed. The durable ``prune-*.lock`` worktree
    administration mutex is excluded by design (see
    :func:`_find_stale_locks`)."""

    lock_name: str
    modified_at: UtcDateTime | None = None


class OrphanedWorkspaceFinding(ModelBase):
    """One directory under ``data_dir/workspaces`` not referenced by any
    scanned run's ``FactoryRun.workspace_path``. May include false
    positives for a workspace whose owning run could not be scanned this
    call (see ``OperationalHealthReport.degraded_reasons``); it is only
    named here, never removed or otherwise acted upon."""

    workspace_name: str
    modified_at: UtcDateTime | None = None


class OperationalHealthReport(VersionedModel):
    """Typed, read-only operational health findings.

    Derived at read time from the same bounded run scan
    :func:`build_monitoring_snapshot` uses (see ``scanned_runs``/
    ``scan_truncated``/``degraded`` -- identical meaning here), plus a
    cheap, strictly non-destructive inspection of ``data_dir/locks`` and
    ``data_dir/workspaces``. Nothing here mutates, repairs, or deletes
    anything: a stale lock probe releases its own non-blocking flock
    immediately and never unlinks the file; an orphaned workspace is only
    named, never removed.

    ``lock_check_supported`` is ``False`` on a platform with no ``fcntl``
    (e.g. Windows); in that case ``stale_locks`` is always empty and
    ``degraded_reasons`` says so explicitly rather than implying zero stale
    locks were found.
    """

    generated_at: UtcDateTime
    stale_after_seconds: float = Field(gt=0.0)
    max_scanned_runs: int = Field(ge=1)
    total_runs: int = Field(ge=0)
    scanned_runs: int = Field(ge=0)
    scan_truncated: bool
    unreadable_runs: int = Field(ge=0)
    degraded: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    lock_check_supported: bool
    locks_checked: int = Field(ge=0)
    workspaces_checked: int = Field(ge=0)
    stale_runs: list[StaleRunFinding] = Field(default_factory=list)
    stale_locks: list[StaleLockFinding] = Field(default_factory=list)
    orphaned_workspaces: list[OrphanedWorkspaceFinding] = Field(default_factory=list)


def build_operational_health(
    store: RunStoreProtocol,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
    scan: RunScanResult | None = None,
) -> OperationalHealthReport:
    """Derive a read-only operational health report.

    Pure and recomputable (aside from lock liveness, which necessarily
    reflects the instant it is probed): the same store/filesystem contents
    and the same ``now``/``stale_after``/``max_scanned_runs`` yield the same
    findings, modulo whether another process happens to be holding a lock
    at the moment of the call.

    ``data_dir`` defaults to ``store.runs_dir.parent``, matching the
    conventional layout ``FileRunStore``/``GitWorktreeWorkspace`` both use
    (``data_dir/runs``, ``data_dir/workspaces``, ``data_dir/locks``); pass it
    explicitly if a caller's store does not follow that convention.

    Reuses the exact same bounded run scan as :func:`build_monitoring_snapshot`
    (:func:`scan_readable_runs`, capped at ``max_scanned_runs``) for both
    stale-run detection and the set of workspace directories considered
    "referenced", so orphan/staleness findings and the metrics snapshot are
    always consistent with each other for the same inputs.

    Never writes, deletes, or repairs anything: a stale lock probe uses a
    non-blocking ``flock`` that is released the instant it succeeds and
    never unlinks the file, and an orphaned workspace directory is only
    named in the returned report.
    """
    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be > 0")
    if max_scanned_runs <= 0:
        raise ValueError("max_scanned_runs must be > 0")

    reference_time = _normalize_now(now)
    root = Path(data_dir).expanduser() if data_dir is not None else store.runs_dir.parent

    if scan is None:
        scan = scan_readable_runs(store, max_scanned_runs)

    stale_runs = [
        StaleRunFinding(
            run_id=run.id,
            work_item_id=run.work_item_id,
            state=run.state,
            idle_seconds=max(
                (reference_time - (_last_signal_at(run) or run.created_at)).total_seconds(),
                0.0,
            ),
            workspace_path=run.workspace_path,
        )
        for run in scan.readable_runs
        if _classify_run(run) == "active" and _is_stale(run, reference_time, stale_after)
    ]

    referenced_workspace_names = {
        Path(run.workspace_path).name for run in scan.readable_runs if run.workspace_path
    }

    stale_locks, locks_checked, lock_issues = _find_stale_locks(root / "locks")
    orphaned_workspaces, workspaces_checked = _find_orphaned_workspaces(
        root / "workspaces", referenced_workspace_names
    )

    degraded_reasons = [
        *scan.degraded_reasons(max_scanned_runs=max_scanned_runs),
        *lock_issues,
    ]
    if scan.scan_truncated:
        degraded_reasons.append(
            "orphaned-workspace findings may include false positives for workspaces "
            "owned by runs the scan cap excluded"
        )

    return OperationalHealthReport(
        generated_at=reference_time,
        stale_after_seconds=stale_after.total_seconds(),
        max_scanned_runs=max_scanned_runs,
        total_runs=scan.total_directories,
        scanned_runs=scan.scanned_runs,
        scan_truncated=scan.scan_truncated,
        unreadable_runs=scan.unreadable_runs,
        degraded=scan.degraded or bool(lock_issues),
        degraded_reasons=degraded_reasons,
        lock_check_supported=_FCNTL_AVAILABLE,
        locks_checked=locks_checked,
        workspaces_checked=workspaces_checked,
        stale_runs=stale_runs,
        stale_locks=stale_locks,
        orphaned_workspaces=orphaned_workspaces,
    )


def _find_stale_locks(locks_dir: Path) -> tuple[list[StaleLockFinding], int, list[str]]:
    """Inspect every workspace ``*.lock`` file directly under ``locks_dir``.

    Returns ``(findings, files_checked, issues)``; ``issues`` are
    human-readable degraded-reason strings (unsupported platform, malformed
    owner data, or a per-file probe failure count), never raised.

    ``prune-<digest>.lock`` files are deliberately skipped and not counted:
    unlike a workspace lock -- which ``GitWorktreeWorkspace.release_lock``
    unlinks while still held, so a *leftover* file genuinely means an
    abandoned holder -- the per-source-repository worktree administration
    lock (``workspace._prune_lock``) is a durable mutex file that is only
    ever flocked and released, never unlinked. Reporting it would make every
    healthy factory that has ever run once report a permanent, unactionable
    stale lock.
    """
    if not _FCNTL_AVAILABLE:
        return [], 0, ["lock staleness checks unsupported on this platform (no fcntl)"]
    if not locks_dir.is_dir():
        return [], 0, []

    findings: list[StaleLockFinding] = []
    checked = 0
    failed_checks = 0
    for entry in sorted(locks_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_file() or not entry.name.endswith(".lock"):
            continue
        if entry.name.startswith(PRUNE_LOCK_PREFIX):
            continue
        checked += 1
        try:
            is_stale, modified_at = _probe_lock_staleness(entry)
        except (OSError, ValueError):
            failed_checks += 1
            continue
        if is_stale:
            findings.append(StaleLockFinding(lock_name=entry.name, modified_at=modified_at))

    issues = (
        [f"{failed_checks} lock file(s) could not be probed for staleness"] if failed_checks else []
    )
    return findings, checked, issues


def _probe_lock_staleness(lock_path: Path) -> tuple[bool, datetime | None]:
    """Non-destructively determine whether a lock's recorded owner is dead.

    Workspace locks persist their owning PID after acquiring the real
    ``flock``. Monitoring must not acquire that same exclusive lock even
    briefly: the workspace protocol uses ``LOCK_NB``, so an observer holding
    it could make a legitimate run fail with ``WorkspaceLockError``. Instead
    this read-only probe checks whether the recorded PID still exists.

    PID reuse can conservatively produce a false negative (an abandoned file
    appears live), but never a false lock conflict. Empty or malformed owner
    data is reported as a degraded probe rather than guessed to be stale.
    """
    try:
        owner_pid = int(lock_path.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        raise ValueError(f"lock file has an invalid owner PID: {lock_path}") from exc
    if owner_pid <= 0:
        raise ValueError(f"lock file has an invalid owner PID: {lock_path}")

    is_stale = not process_alive(owner_pid)

    try:
        modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        modified_at = None
    return is_stale, modified_at


def _find_orphaned_workspaces(
    workspaces_dir: Path,
    referenced_names: set[str],
) -> tuple[list[OrphanedWorkspaceFinding], int]:
    """Every directory directly under ``workspaces_dir`` whose name is not
    in ``referenced_names`` and is not a setup worktree. Returns
    ``(findings, directories_checked)``.
    Never descends into a workspace's own contents (no file listing, no
    diff, no repository content is read)."""
    if not workspaces_dir.is_dir():
        return [], 0

    findings: list[OrphanedWorkspaceFinding] = []
    checked = 0
    for entry in sorted(workspaces_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_dir():
            continue
        checked += 1
        # Setup worktrees (ADR-034) belong to no run. They are kept on purpose.
        if entry.name in referenced_names or SETUP_WORKTREE_NAME.match(entry.name):
            continue
        try:
            modified_at = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        except OSError:
            modified_at = None
        findings.append(
            OrphanedWorkspaceFinding(workspace_name=entry.name, modified_at=modified_at)
        )
    return findings, checked
