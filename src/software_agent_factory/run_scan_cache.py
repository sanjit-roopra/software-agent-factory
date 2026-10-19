"""Cache of readable run scans for the dashboard's poll loop.

Two layers keep a dashboard poll from re-reading the whole run store:

* a short TTL reuses the last scan outright, so the summary, health and runs reads
  of one poll share it;
* after the TTL, a rescan lists and stats every run directory again (cheap) but
  parses only the ``run.json`` files whose modification time, size or inode
  changed.
  Unchanged runs are the same parsed ``FactoryRun`` objects as before.

Parsed runs are shared between callers and must be treated as read-only. The cache
is a read path only and never acts as workflow authority.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .models import FactoryRun
from .observability import (
    DEFAULT_MAX_SCANNED_RUNS,
    RunScanResult,
    RunStoreProtocol,
    scan_readable_runs,
)

__all__ = ["DEFAULT_SCAN_CACHE_TTL", "RunScanCache"]

# Longer than the dashboard's fast poll (POLL_ACTIVE_MS, 2 s in dashboard/static/app.js),
# so back-to-back polls reuse one scan instead of rescanning every tick.
DEFAULT_SCAN_CACHE_TTL: float = 6.0

#: What identifies one version of a ``run.json``: its modification time, size and
#: inode. The store replaces ``run.json`` atomically, so every save gets a new inode;
#: that catches a same-size rewrite inside one coarse mtime tick (Linux timestamps
#: can be a few milliseconds coarse).
_FileKey = tuple[int, int, int]


class RunScanCache:
    """Thread-safe scan cache: TTL reuse, then mtime-keyed re-parsing of changed runs.

    Each caller gets its own run list and reason map; the ``FactoryRun`` objects are
    shared and read-only. Runs that disappear from the store leave the cache on the
    next rescan.
    """

    def __init__(
        self,
        store: RunStoreProtocol,
        *,
        ttl: float = DEFAULT_SCAN_CACHE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_scan: RunScanResult | None = None
        self._cached_max_scanned_runs: int | None = None
        self._cached_at: float = 0.0
        self._parsed: dict[str, tuple[_FileKey, FactoryRun]] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._parses: int = 0

    @property
    def ttl(self) -> float:
        return self._ttl

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    @property
    def parses(self) -> int:
        """How many ``run.json`` files this cache has parsed in total."""
        with self._lock:
            return self._parses

    def get_scan(self, max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS) -> RunScanResult:
        """Return a validated scan, reusing the cached scan if within TTL.

        Returns a shallow copy: the run list and reason map are new, the runs
        themselves are the cached, read-only objects.
        """
        if max_scanned_runs <= 0:
            raise ValueError("max_scanned_runs must be > 0")

        with self._lock:
            now_mono = self._clock()
            if (
                self._cached_scan is not None
                and self._cached_max_scanned_runs == max_scanned_runs
                and (now_mono - self._cached_at) < self._ttl
            ):
                self._hits += 1
                return self._copy_scan(self._cached_scan)

            self._misses += 1
            previous = self._parsed
            self._parsed = {}
            scan = scan_readable_runs(
                self._store,
                max_scanned_runs,
                load_run=lambda run_id: self._load(run_id, previous),
            )
            self._cached_scan = scan
            self._cached_max_scanned_runs = max_scanned_runs
            self._cached_at = now_mono
            return self._copy_scan(scan)

    def _load(self, run_id: str, previous: dict[str, tuple[_FileKey, FactoryRun]]) -> FactoryRun:
        """Reuse the parsed run when its ``run.json`` is unchanged; parse it otherwise.

        The file is stat-ed before it is read, so a write that lands in between is
        stored under the older key and re-parsed on the next rescan.
        """
        try:
            stat = (self._store.runs_dir / run_id / "run.json").stat()
        except OSError:
            return self._store.load_run(run_id)
        key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        cached = previous.get(run_id)
        if cached is not None and cached[0] == key:
            run = cached[1]
        else:
            self._parses += 1
            run = self._store.load_run(run_id)
        self._parsed[run_id] = (key, run)
        return run

    @staticmethod
    def _copy_scan(scan: RunScanResult) -> RunScanResult:
        return RunScanResult(
            readable_runs=list(scan.readable_runs),
            total_directories=scan.total_directories,
            scanned_runs=scan.scanned_runs,
            scan_truncated=scan.scan_truncated,
            unreadable_reasons=dict(scan.unreadable_reasons),
            artifact_facts=scan.artifact_facts,
        )
