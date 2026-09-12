#!/usr/bin/env python3
"""Standard-library benchmark harness for Software Agent Factory.

Measures latency of key offline factory operations:
1. CLI import and startup
2. Run store scanning and monitoring snapshot computation
3. Prompt construction and artifact output parsing
4. Repository profiling on local synthetic fixtures

All benchmarks are deterministic, offline, and require no network or LLM calls.
Emits structured JSON and supports comparison against a saved JSON baseline.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _run_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _percentile(data: list[float], p: float) -> float:
    """Calculate percentile using linear interpolation between closest ranks."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    k = (n - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, n - 1)
    d = k - f
    return sorted_data[f] + d * (sorted_data[c] - sorted_data[f])


def _measure_repeated(
    func: Callable[[], None],
    iterations: int,
    warmup: int,
) -> dict[str, Any]:
    for _ in range(warmup):
        func()

    durations_ms: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        func()
        t1 = time.perf_counter()
        durations_ms.append((t1 - t0) * 1000.0)

    mean_ms = statistics.mean(durations_ms)
    median_ms = statistics.median(durations_ms)
    p50_ms = _percentile(durations_ms, 50.0)
    p95_ms = _percentile(durations_ms, 95.0)
    min_ms = min(durations_ms)
    max_ms = max(durations_ms)
    stddev_ms = statistics.stdev(durations_ms) if len(durations_ms) > 1 else 0.0

    return {
        "iterations": iterations,
        "mean_ms": round(mean_ms, 3),
        "median_ms": round(median_ms, 3),
        "p50_ms": round(p50_ms, 3),
        "p95_ms": round(p95_ms, 3),
        "min_ms": round(min_ms, 3),
        "max_ms": round(max_ms, 3),
        "stddev_ms": round(stddev_ms, 3),
        "samples_ms": [round(d, 3) for d in durations_ms],
    }


# ---------------------------------------------------------------------------
# Benchmark 1: CLI Startup and Import
# ---------------------------------------------------------------------------


def benchmark_cli_import(repo_root: Path, iterations: int, warmup: int) -> dict[str, Any]:
    python_exe = sys.executable
    cmd = [python_exe, "-c", "import software_agent_factory"]
    env = {"PYTHONPATH": str(repo_root / "src")}

    def run_import() -> None:
        _run_subprocess(cmd, cwd=repo_root, env_overrides=env)

    return _measure_repeated(run_import, iterations=iterations, warmup=warmup)


def benchmark_cli_help(repo_root: Path, iterations: int, warmup: int) -> dict[str, Any]:
    python_exe = sys.executable
    cmd = [python_exe, "-m", "software_agent_factory", "--help"]
    env = {"PYTHONPATH": str(repo_root / "src")}

    def run_help() -> None:
        _run_subprocess(cmd, cwd=repo_root, env_overrides=env)

    return _measure_repeated(run_help, iterations=iterations, warmup=warmup)


# ---------------------------------------------------------------------------
# Benchmark 2: Run Store Scan and Snapshot
# ---------------------------------------------------------------------------


def _setup_synthetic_run_store(data_dir: Path, num_runs: int = 100) -> None:
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    states = [
        "PR_READY",
        "DONE",
        "FAILED",
        "NEEDS_HUMAN",
        "IMPLEMENTING",
    ]

    for i in range(num_runs):
        run_id = f"RUN-SYNTH-{i:04d}"
        run_state = states[i % len(states)]
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run_payload = {
            "schema_version": 1,
            "id": run_id,
            "work_item_id": f"WI-SYNTH-{i:04d}",
            "workspace_path": str(data_dir),
            "branch_name": f"factory/WI-SYNTH-{i:04d}",
            "state": run_state,
            "attempt_records": [],
            "invocation_records": [],
            "created_at": "2026-09-12T00:00:00Z",
            "updated_at": "2026-09-12T00:00:00Z",
        }
        (run_dir / "run.json").write_text(json.dumps(run_payload), encoding="utf-8")

        work_item_payload = {
            "schema_version": 1,
            "id": f"WI-SYNTH-{i:04d}",
            "title": f"Synthetic benchmark task {i}",
            "description": "A synthetic workload item used for run store benchmark testing.",
            "risk": "R0",
        }
        (run_dir / "work-item.json").write_text(json.dumps(work_item_payload), encoding="utf-8")


def benchmark_run_store_scan(
    num_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    from software_agent_factory.observability import build_monitoring_snapshot
    from software_agent_factory.store import FileRunStore

    with tempfile.TemporaryDirectory(prefix="benchmark_store_") as temp_dir:
        store_path = Path(temp_dir)
        _setup_synthetic_run_store(store_path, num_runs=num_runs)
        store = FileRunStore(store_path)

        def run_scan() -> None:
            snapshot = build_monitoring_snapshot(store, max_scanned_runs=num_runs)
            assert snapshot.scanned_runs == num_runs

        return _measure_repeated(run_scan, iterations=iterations, warmup=warmup)


# ---------------------------------------------------------------------------
# Benchmark 3: Dashboard Monitoring Summary & Operational Health Reads
# ---------------------------------------------------------------------------


def benchmark_dashboard_monitoring_summary(
    store_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    from software_agent_factory.observability import build_monitoring_snapshot
    from software_agent_factory.store import FileRunStore

    with tempfile.TemporaryDirectory(prefix="benchmark_dash_sum_") as temp_dir:
        store_path = Path(temp_dir)
        _setup_synthetic_run_store(store_path, num_runs=store_runs)
        store = FileRunStore(store_path)

        def run_summary() -> None:
            snapshot = build_monitoring_snapshot(store, max_scanned_runs=store_runs)
            assert snapshot.scanned_runs == store_runs

        return _measure_repeated(run_summary, iterations=iterations, warmup=warmup)


def benchmark_dashboard_operational_health(
    store_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    from software_agent_factory.observability import build_operational_health
    from software_agent_factory.store import FileRunStore

    with tempfile.TemporaryDirectory(prefix="benchmark_dash_health_") as temp_dir:
        store_path = Path(temp_dir)
        _setup_synthetic_run_store(store_path, num_runs=store_runs)
        store = FileRunStore(store_path)

        def run_health() -> None:
            health = build_operational_health(
                store, data_dir=store_path, max_scanned_runs=store_runs
            )
            assert health.scanned_runs == store_runs

        return _measure_repeated(run_health, iterations=iterations, warmup=warmup)


def _probe_scan_support(store: Any, store_path: Path, store_runs: int) -> tuple[bool, type | None]:
    """Probe whether RunScanCache and the scan= parameter are supported at execution time."""
    try:
        from software_agent_factory.observability import (
            RunScanCache,
            build_monitoring_snapshot,
            build_operational_health,
        )

        cache = RunScanCache(store)
        probe_scan = cache.get_scan(store_runs)
        snap = build_monitoring_snapshot(store, max_scanned_runs=store_runs, scan=probe_scan)
        health = build_operational_health(
            store, data_dir=store_path, max_scanned_runs=store_runs, scan=probe_scan
        )
        if snap.scanned_runs == store_runs and health.scanned_runs == store_runs:
            return True, RunScanCache
    except (ImportError, TypeError, AttributeError):
        pass
    return False, None


def benchmark_dashboard_shared_scan_cold(
    store_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    """Benchmark cold shared scan: one store scan is executed from disk and shared
    between monitoring snapshot and operational health.

    Falls back to unshared cold scans on baseline commits lacking scan parameter support,
    gracefully handling TypeError at execution time.
    """
    from software_agent_factory.observability import (
        build_monitoring_snapshot,
        build_operational_health,
    )
    from software_agent_factory.store import FileRunStore

    with tempfile.TemporaryDirectory(prefix="benchmark_dash_cold_") as temp_dir:
        store_path = Path(temp_dir)
        _setup_synthetic_run_store(store_path, num_runs=store_runs)
        store = FileRunStore(store_path)
        supports_scan, cache_cls = _probe_scan_support(store, store_path, store_runs)

        def run_cold() -> None:
            if supports_scan and cache_cls is not None:
                try:
                    cache = cache_cls(store)
                    scan = cache.get_scan(store_runs)
                    snapshot = build_monitoring_snapshot(
                        store, max_scanned_runs=store_runs, scan=scan
                    )
                    health = build_operational_health(
                        store,
                        data_dir=store_path,
                        max_scanned_runs=store_runs,
                        scan=scan,
                    )
                    assert snapshot.scanned_runs == store_runs
                    assert health.scanned_runs == store_runs
                    return
                except TypeError:
                    pass
            # Fallback for baseline commit compatibility without shared scan support
            snapshot = build_monitoring_snapshot(store, max_scanned_runs=store_runs)
            health = build_operational_health(
                store, data_dir=store_path, max_scanned_runs=store_runs
            )
            assert snapshot.scanned_runs == store_runs
            assert health.scanned_runs == store_runs

        return _measure_repeated(run_cold, iterations=iterations, warmup=warmup)


def benchmark_dashboard_shared_cache_warm(
    store_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    """Benchmark in-memory warm cache lookup: reads from an already-populated RunScanCache.

    Note: This measures in-memory cache hit latency and does NOT represent a cold store refresh.
    Falls back gracefully if caching is unsupported on baseline commits, handling TypeError
    at execution time.
    """
    from software_agent_factory.observability import (
        build_monitoring_snapshot,
        build_operational_health,
    )
    from software_agent_factory.store import FileRunStore

    with tempfile.TemporaryDirectory(prefix="benchmark_dash_warm_") as temp_dir:
        store_path = Path(temp_dir)
        _setup_synthetic_run_store(store_path, num_runs=store_runs)
        store = FileRunStore(store_path)
        supports_scan, cache_cls = _probe_scan_support(store, store_path, store_runs)

        warm_cache = cache_cls(store) if supports_scan and cache_cls is not None else None
        if warm_cache is not None:
            try:
                # Pre-populate cache so iterations measure warm cache hits only
                _ = warm_cache.get_scan(store_runs)
            except Exception:
                warm_cache = None

        def run_warm() -> None:
            if warm_cache is not None:
                try:
                    scan = warm_cache.get_scan(store_runs)
                    snapshot = build_monitoring_snapshot(
                        store, max_scanned_runs=store_runs, scan=scan
                    )
                    health = build_operational_health(
                        store,
                        data_dir=store_path,
                        max_scanned_runs=store_runs,
                        scan=scan,
                    )
                    assert snapshot.scanned_runs == store_runs
                    assert health.scanned_runs == store_runs
                    return
                except TypeError:
                    pass
            # Fallback for baseline commit compatibility
            snapshot = build_monitoring_snapshot(store, max_scanned_runs=store_runs)
            health = build_operational_health(
                store, data_dir=store_path, max_scanned_runs=store_runs
            )
            assert snapshot.scanned_runs == store_runs
            assert health.scanned_runs == store_runs

        return _measure_repeated(run_warm, iterations=iterations, warmup=warmup)


def benchmark_dashboard_shared_cache(
    store_runs: int = 100,
    iterations: int = 5,
    warmup: int = 1,
    *,
    cache_state: str = "cold",
) -> dict[str, Any]:
    """Measure dashboard operations using shared scan cache.

    Default is cold shared-scan (1 scan for summary and health) to ensure fair comparison
    against baseline store scans. Pass cache_state='warm' for in-memory hit latency.
    """
    if cache_state == "warm":
        return benchmark_dashboard_shared_cache_warm(store_runs, iterations, warmup)
    return benchmark_dashboard_shared_scan_cold(store_runs, iterations, warmup)


# ---------------------------------------------------------------------------
# Benchmark 4: Scheduler Backlog Drain
# ---------------------------------------------------------------------------


def benchmark_scheduler_drain(
    concurrency: int = 1,
    num_items: int = 20,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    from software_agent_factory.scheduler import DispatchOutcome, Scheduler, TrackerItem

    class _BenchHandle:
        def is_done(self) -> bool:
            return True

        def outcome(self) -> DispatchOutcome:
            return DispatchOutcome.SUCCEEDED

        def last_activity_at(self) -> datetime:
            return datetime.now(timezone.utc)

        def cancel(self) -> None:
            pass

    class _BenchProvider:
        def __init__(self, count: int) -> None:
            self._items: dict[str, TrackerItem] = {
                f"item-{i:04d}": TrackerItem(
                    opaque_id=f"item-{i:04d}",
                    identifier=f"item-{i:04d}",
                    title=f"Synthetic task {i}",
                    description="Scheduler benchmark workload",
                    state="open",
                    labels=(),
                    priority=None,
                    created_at=datetime.now(timezone.utc),
                    blockers=(),
                    dispatchable=True,
                    repository_path="/repo",
                )
                for i in range(count)
            }

        @property
        def remaining(self) -> list[TrackerItem]:
            return list(self._items.values())

        def fetch_candidates(self) -> list[TrackerItem]:
            return list(self._items.values())

        def fetch_by_ids(self, opaque_ids: Any) -> list[TrackerItem]:
            return [self._items[oid] for oid in opaque_ids if oid in self._items]

        def claim(self, opaque_id: str) -> None:
            """Remove item from backlog at claim/dispatch time so provider state
            accurately reflects that the item is no longer pending candidate discovery."""
            self._items.pop(opaque_id, None)

    dispatched_counts: list[int] = []

    def run_drain() -> None:
        provider = _BenchProvider(num_items)
        dispatched_ids: list[str] = []

        def dispatch(item: TrackerItem) -> _BenchHandle:
            dispatched_ids.append(item.opaque_id)
            provider.claim(item.opaque_id)
            return _BenchHandle()

        scheduler = Scheduler(
            provider,
            dispatch,
            max_concurrent_tasks=concurrency,
        )
        ticks = 0
        while provider.remaining or scheduler.active_count > 0:
            scheduler.tick()
            ticks += 1
            if ticks > num_items * 2 + 20:
                raise RuntimeError("Scheduler backlog drain stalled")

        # Deterministic assertions for exact-once dispatch:
        assert scheduler.active_count == 0, f"Expected 0 active tasks, got {scheduler.active_count}"
        assert len(provider.remaining) == 0, (
            f"Expected 0 remaining items, got {len(provider.remaining)}"
        )
        assert len(dispatched_ids) == num_items, (
            f"Expected {num_items} dispatches, got {len(dispatched_ids)}"
        )
        assert len(set(dispatched_ids)) == num_items, (
            f"Duplicate dispatches detected: {len(dispatched_ids)} vs {len(set(dispatched_ids))}"
        )
        dispatched_counts.append(len(dispatched_ids))

    res = _measure_repeated(run_drain, iterations=iterations, warmup=warmup)
    res["items_dispatched"] = num_items
    res["dispatched_count"] = num_items
    return res


# ---------------------------------------------------------------------------
# Benchmark 5: Repository Profiling (Small & Large)
# ---------------------------------------------------------------------------


def _setup_synthetic_repo(repo_dir: Path, tree_size: str = "small") -> None:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = "Benchmark Runner"
    env["GIT_AUTHOR_EMAIL"] = "benchmark@example.invalid"
    env["GIT_COMMITTER_NAME"] = "Benchmark Runner"
    env["GIT_COMMITTER_EMAIL"] = "benchmark@example.invalid"

    _run_subprocess(["git", "init", "-b", "main"], cwd=repo_dir, env_overrides=env)
    _run_subprocess(
        ["git", "config", "user.name", "Benchmark Runner"],
        cwd=repo_dir,
        env_overrides=env,
    )
    _run_subprocess(
        ["git", "config", "user.email", "benchmark@example.invalid"],
        cwd=repo_dir,
        env_overrides=env,
    )
    _run_subprocess(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_dir,
        env_overrides=env,
    )

    pyproject = repo_dir / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "synthetic-bench-app"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "pydantic>=2.0.0",
    "typer>=0.12.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "ruff>=0.5.0",
]
""",
        encoding="utf-8",
    )

    package_json = repo_dir / "package.json"
    package_json.write_text(
        """{
  "name": "synthetic-frontend",
  "version": "1.0.0",
  "dependencies": {
    "react": "^19.0.0"
  }
}
""",
        encoding="utf-8",
    )

    readme = repo_dir / "README.md"
    readme.write_text("# Synthetic Bench App\n", encoding="utf-8")

    src_dir = repo_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "app.py").write_text("print('hello world')\n", encoding="utf-8")

    if tree_size == "large":
        lock_file = repo_dir / "uv.lock"
        lock_file.write_text("version = 1\nrevision = 1\n", encoding="utf-8")
        for d in range(10):
            sub_dir = repo_dir / f"pkg_{d}"
            sub_dir.mkdir(parents=True, exist_ok=True)
            for f in range(25):
                (sub_dir / f"mod_{f}.py").write_text(
                    f"# Synthetic module {d}.{f}\nx = {f}\n", encoding="utf-8"
                )

    _run_subprocess(["git", "add", "."], cwd=repo_dir, env_overrides=env)
    _run_subprocess(
        ["git", "commit", "-m", "Initial synthetic commit"],
        cwd=repo_dir,
        env_overrides=env,
    )


def benchmark_repository_profiling(
    tree_size_or_iterations: str | int = "small",
    iterations: int = 5,
    warmup: int = 1,
    *,
    tree_size: str = "small",
) -> dict[str, Any]:
    if isinstance(tree_size_or_iterations, str):
        actual_tree_size = tree_size_or_iterations
        actual_iterations = iterations
        actual_warmup = warmup
    else:
        actual_tree_size = tree_size
        actual_iterations = tree_size_or_iterations
        actual_warmup = iterations

    from software_agent_factory.repository_profile import profile_repository

    with tempfile.TemporaryDirectory(prefix="benchmark_repo_") as temp_dir:
        repo_path = Path(temp_dir)
        _setup_synthetic_repo(repo_path, tree_size=actual_tree_size)

        def run_profile() -> None:
            profile = profile_repository(repo_path)
            assert len(profile.dependencies) >= 3

        return _measure_repeated(run_profile, iterations=actual_iterations, warmup=actual_warmup)


# ---------------------------------------------------------------------------
# Benchmark 6: Git Evidence Collection
# ---------------------------------------------------------------------------


def benchmark_git_evidence_collection(
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    from software_agent_factory.workspace import GitWorktreeWorkspace

    with tempfile.TemporaryDirectory(prefix="benchmark_git_") as temp_dir:
        root = Path(temp_dir)
        source_repo = root / "source"
        data_dir = root / "data"
        source_repo.mkdir()
        data_dir.mkdir()

        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_AUTHOR_NAME"] = "Benchmark Runner"
        env["GIT_AUTHOR_EMAIL"] = "benchmark@example.invalid"
        env["GIT_COMMITTER_NAME"] = "Benchmark Runner"
        env["GIT_COMMITTER_EMAIL"] = "benchmark@example.invalid"

        _run_subprocess(["git", "init", "-b", "main"], cwd=source_repo, env_overrides=env)
        _run_subprocess(
            ["git", "config", "user.name", "Benchmark Runner"],
            cwd=source_repo,
            env_overrides=env,
        )
        _run_subprocess(
            ["git", "config", "user.email", "benchmark@example.invalid"],
            cwd=source_repo,
            env_overrides=env,
        )
        _run_subprocess(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=source_repo,
            env_overrides=env,
        )

        (source_repo / "README.md").write_text("# Bench Repo\n", encoding="utf-8")
        (source_repo / "app.py").write_text("print('init')\n", encoding="utf-8")
        _run_subprocess(["git", "add", "."], cwd=source_repo, env_overrides=env)
        _run_subprocess(
            ["git", "commit", "-m", "Initial commit"],
            cwd=source_repo,
            env_overrides=env,
        )

        ws = GitWorktreeWorkspace(data_dir, source_repo, "WI-BENCH-EVIDENCE")
        ws.prepare()

        counter = 0

        def run_collect() -> None:
            nonlocal counter
            counter += 1
            (ws.path / f"change_{counter}.py").write_text(
                f"# Change {counter}\nx = {counter}\n", encoding="utf-8"
            )
            ev = ws.collect_evidence()
            assert ev.tree_sha is not None
            assert len(ev.changed_files) >= 1

        return _measure_repeated(run_collect, iterations=iterations, warmup=warmup)


# ---------------------------------------------------------------------------
# Benchmark 7: Prompt Construction and Output Parser
# ---------------------------------------------------------------------------


def benchmark_prompt_and_parser(iterations: int = 5, warmup: int = 1) -> dict[str, Any]:
    from software_agent_factory.agents import AgentRequest
    from software_agent_factory.copilot_runtime import parse_copilot_artifact
    from software_agent_factory.models import (
        AgentPurpose,
        AgentRole,
        Risk,
        WorkItem,
    )
    from software_agent_factory.prompts import build_prompt

    work_item = WorkItem(
        id="WI-BENCH-001",
        title="Implement benchmark test feature",
        description="Construct synthetic prompts and parse typed responses.",
        risk=Risk.R0,
    )
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.STANDARD,
        model="bench-model",
        reasoning="Standard implementation request",
        timeout_seconds=300,
        work_item=work_item,
        diff="--- a/src/test.py\n+++ b/src/test.py\n@@ -1,3 +1,4 @@\n+print('bench')\n",
        changed_files=["src/test.py"],
    )

    synthetic_response = json.dumps(
        {
            "schema_version": 1,
            "summary": "Implemented synthetic benchmark test feature",
            "changed_files": ["src/test.py"],
            "tests_added": ["tests/test_foo.py"],
            "commands_run": ["pytest tests/test_foo.py"],
        }
    )

    def run_prompt_and_parser() -> None:
        prompt = build_prompt(request)
        assert len(prompt) > 50
        artifact = parse_copilot_artifact(
            AgentRole.IMPLEMENTER,
            stdout=synthetic_response,
            purpose=AgentPurpose.STANDARD,
        )
        assert artifact is not None

    return _measure_repeated(run_prompt_and_parser, iterations=iterations, warmup=warmup)


# ---------------------------------------------------------------------------
# Standard vs Fast Fake-Runtime Controller Comparison
# ---------------------------------------------------------------------------


def run_controller_standard_vs_fast(
    repo_root: Path | None = None,
    iterations: int = 3,
    warmup: int = 1,
) -> dict[str, Any]:
    """Execute a deterministic offline controller bakeoff comparing standard
    versus fast performance mode under fake-runtime boundaries.

    Verifies latency, attempts, invocations, and that quality gates remain
    authoritative and unweakened.
    """
    from software_agent_factory.agents import FakeAgentRuntime
    from software_agent_factory.config import FactoryConfig
    from software_agent_factory.models import (
        AgentRole,
        AttemptTrigger,
        ReviewReport,
        TestReport,
        VerificationReport,
        WorkItem,
    )
    from software_agent_factory.store import FileRunStore
    from software_agent_factory.workflow import WorkflowController

    def _execute_run(mode: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="benchmark_ctrl_") as temp_dir:
            root = Path(temp_dir)
            source_repo = root / "source"
            data_dir = root / "data"
            source_repo.mkdir()
            data_dir.mkdir()

            env = os.environ.copy()
            env["GIT_CONFIG_GLOBAL"] = os.devnull
            env["GIT_CONFIG_SYSTEM"] = os.devnull
            env["GIT_AUTHOR_NAME"] = "Benchmark Runner"
            env["GIT_AUTHOR_EMAIL"] = "benchmark@example.invalid"
            env["GIT_COMMITTER_NAME"] = "Benchmark Runner"
            env["GIT_COMMITTER_EMAIL"] = "benchmark@example.invalid"

            _run_subprocess(["git", "init", "-b", "main"], cwd=source_repo, env_overrides=env)
            _run_subprocess(
                ["git", "config", "user.name", "Benchmark Runner"],
                cwd=source_repo,
                env_overrides=env,
            )
            _run_subprocess(
                ["git", "config", "user.email", "benchmark@example.invalid"],
                cwd=source_repo,
                env_overrides=env,
            )
            _run_subprocess(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=source_repo,
                env_overrides=env,
            )

            (source_repo / "app.py").write_text("def run(): return 42\n", encoding="utf-8")
            _run_subprocess(["git", "add", "."], cwd=source_repo, env_overrides=env)
            _run_subprocess(
                ["git", "commit", "-m", "Initial commit"],
                cwd=source_repo,
                env_overrides=env,
            )

            config = FactoryConfig.model_validate(
                {
                    "factory": {
                        "data_dir": str(data_dir),
                        "retries": {"same_model_attempts": 2, "max_total_attempts": 6},
                    },
                    "models": {
                        "triage": {"model": "claude-sonnet-5", "reasoning": "medium"},
                        "refiner": {"model": "claude-opus-5", "reasoning": "high"},
                        "researcher": {"model": "gpt-5.6-sol", "reasoning": "high"},
                        "planner": {"model": "claude-opus-5", "reasoning": "high"},
                        "workers": {
                            "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                            "L1": {"model": "claude-sonnet-5", "reasoning": "medium"},
                            "L2": {"model": "claude-opus-5", "reasoning": "high"},
                            "L3": {"model": "claude-opus-5", "reasoning": "high"},
                        },
                        "tester": {"model": "claude-sonnet-5", "reasoning": "high"},
                        "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
                    },
                    "model_profiles": {
                        "economy": {
                            "triage": {"model": "gpt-5.6-luna", "reasoning": "medium"},
                            "refiner": {"model": "gpt-5.6-terra", "reasoning": "high"},
                            "researcher": {"model": "gemini-3.8-flash", "reasoning": "medium"},
                            "planner": {"model": "gpt-5.6-terra", "reasoning": "high"},
                            "workers": {
                                "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                                "L1": {"model": "gemini-3.8-flash", "reasoning": "medium"},
                                "L2": {"model": "claude-sonnet-5", "reasoning": "high"},
                                "L3": {"model": "claude-opus-5", "reasoning": "high"},
                            },
                            "tester": {"model": "gemini-3.8-flash", "reasoning": "high"},
                            "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
                        }
                    },
                    "performance": {
                        "mode": mode,
                        "fast_model_profile": "economy",
                    },
                    "repository": {
                        "branch_prefix": "factory/",
                        "command_timeout_seconds": 30,
                        "commands": {"install": [], "verify": [], "build": []},
                    },
                    "polish": {"enabled": True},
                    "risk": {
                        "R0": {"human_approval": False},
                        "R1": {"human_approval": False},
                        "R2": {"human_approval": True},
                        "R3": {"human_approval": True},
                    },
                }
            )

            store = FileRunStore(data_dir)
            runtime = FakeAgentRuntime()
            controller = WorkflowController(config, store, runtime)
            work_item = WorkItem(
                id=f"WI-BENCH-{mode.upper()}",
                title=f"Benchmark Controller Task ({mode})",
                description="Controller comparison execution item",
            )

            t0 = time.perf_counter()
            run = controller.run(work_item, source_repo)
            t1 = time.perf_counter()

            polish_attempts = [
                att for att in run.attempt_records if att.triggered_by == AttemptTrigger.POLISH
            ]

            # Requirement 2: Inspect invocation records and persisted verification/review evidence
            tester_invocations = [
                inv for inv in run.invocation_records if inv.role == AgentRole.TESTER
            ]
            reviewer_invocations = [
                inv for inv in run.invocation_records if inv.role == AgentRole.REVIEWER
            ]
            tester_ran = len(tester_invocations) > 0 and all(
                inv.success for inv in tester_invocations
            )
            reviewer_ran = len(reviewer_invocations) > 0 and all(
                inv.success for inv in reviewer_invocations
            )

            verification_report = None
            try:
                verification_report = store.load_artifact(run.id, VerificationReport)
            except Exception:
                pass
            verification_passed = (
                verification_report is not None and verification_report.passed is True
            )

            test_report = None
            try:
                test_report = store.load_artifact(run.id, TestReport)
            except Exception:
                pass

            review_report = None
            try:
                review_report = store.load_artifact(run.id, ReviewReport)
            except Exception:
                pass
            review_approved = review_report is not None and review_report.approved is True

            gates_satisfied = bool(
                run.state.value == "PR_READY"
                and verification_passed
                and tester_ran
                and test_report is not None
                and reviewer_ran
                and review_approved
            )

            gate_facts = {
                "state": run.state.value,
                "deterministic_verification_ran": verification_report is not None,
                "deterministic_verification_passed": verification_passed,
                "tester_invoked": len(tester_invocations) > 0,
                "tester_invocations_count": len(tester_invocations),
                "tester_success": tester_ran,
                "test_report_persisted": test_report is not None,
                "reviewer_invoked": len(reviewer_invocations) > 0,
                "reviewer_invocations_count": len(reviewer_invocations),
                "reviewer_success": reviewer_ran,
                "review_report_persisted": review_report is not None,
                "review_approved": review_approved,
                "gates_satisfied": gates_satisfied,
            }

            return {
                "mode": mode,
                "state": run.state.value,
                "duration_ms": round((t1 - t0) * 1000.0, 2),
                "attempts_total": len(run.attempt_records),
                "invocations_total": len(run.invocation_records),
                "polish_attempts": len(polish_attempts),
                "effective_performance_mode": getattr(
                    run, "effective_performance_mode", "standard"
                ),
                "performance_model_profile": getattr(run, "performance_model_profile", None),
                "gate_facts": gate_facts,
                "models_by_role": {inv.role.value: inv.model for inv in run.invocation_records},
            }

    for _ in range(warmup):
        _execute_run("standard")
        _execute_run("fast")

    std_durations: list[float] = []
    standard_summary: dict[str, Any] = {}
    for _ in range(iterations):
        standard_summary = _execute_run("standard")
        std_durations.append(standard_summary["duration_ms"])

    fast_durations: list[float] = []
    fast_summary: dict[str, Any] = {}
    for _ in range(iterations):
        fast_summary = _execute_run("fast")
        fast_durations.append(fast_summary["duration_ms"])

    std_median = round(statistics.median(std_durations), 2)
    fast_median = round(statistics.median(fast_durations), 2)
    standard_summary["duration_ms"] = std_median
    fast_summary["duration_ms"] = fast_median
    standard_summary["samples_ms"] = std_durations
    fast_summary["samples_ms"] = fast_durations

    std_gate_facts = standard_summary.get("gate_facts", {})
    fast_gate_facts = fast_summary.get("gate_facts", {})

    gates_preserved = bool(
        std_gate_facts.get("gates_satisfied", False)
        and fast_gate_facts.get("gates_satisfied", False)
    )

    summary_gate_facts = {
        "standard_gates_satisfied": std_gate_facts.get("gates_satisfied", False),
        "fast_gates_satisfied": fast_gate_facts.get("gates_satisfied", False),
        "deterministic_verification_passed": bool(
            std_gate_facts.get("deterministic_verification_passed", False)
            and fast_gate_facts.get("deterministic_verification_passed", False)
        ),
        "tester_verified": bool(
            std_gate_facts.get("tester_success", False)
            and fast_gate_facts.get("tester_success", False)
            and std_gate_facts.get("test_report_persisted", False)
            and fast_gate_facts.get("test_report_persisted", False)
        ),
        "reviewer_verified": bool(
            std_gate_facts.get("reviewer_success", False)
            and fast_gate_facts.get("reviewer_success", False)
            and std_gate_facts.get("review_approved", False)
            and fast_gate_facts.get("review_approved", False)
        ),
    }

    speedup_pct = (
        round(
            (std_median - fast_median) / std_median * 100.0,
            2,
        )
        if std_median > 0
        else 0.0
    )

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "standard_duration_ms": std_median,
            "fast_duration_ms": fast_median,
            "speedup_pct": speedup_pct,
            "gates_preserved": gates_preserved,
            "gate_facts": summary_gate_facts,
        },
        "standard": standard_summary,
        "fast": fast_summary,
    }


# ---------------------------------------------------------------------------
# Harness Runner and Baseline Comparison
# ---------------------------------------------------------------------------


def run_all_benchmarks(
    repo_root: Path,
    iterations: int = 5,
    warmup: int = 1,
) -> dict[str, Any]:
    # Configure sys.path so in-process benchmarks import from repo_root
    src_dir = str(repo_root / "src")
    if src_dir in sys.path:
        sys.path.remove(src_dir)
    sys.path.insert(0, src_dir)

    # Invalidate cached software_agent_factory modules if switching roots
    for mod in list(sys.modules):
        if mod == "software_agent_factory" or mod.startswith("software_agent_factory."):
            del sys.modules[mod]

    results: dict[str, Any] = {}

    print("Running Benchmark 1/7: CLI Import & Startup...")
    results["cli_import_ms"] = benchmark_cli_import(repo_root, iterations, warmup)
    results["cli_help_invocation_ms"] = benchmark_cli_help(repo_root, iterations, warmup)

    print("Running Benchmark 2/7: Run Store Scans (100, 500, 1000 runs)...")
    results["run_store_scan_100_ms"] = benchmark_run_store_scan(100, iterations, warmup)
    results["run_store_scan_500_ms"] = benchmark_run_store_scan(500, iterations, warmup)
    results["run_store_scan_1000_ms"] = benchmark_run_store_scan(1000, iterations, warmup)
    results["run_store_scan_ms"] = results["run_store_scan_100_ms"]

    print("Running Benchmark 3/7: Dashboard Monitoring Summary & Health Reads...")
    results["dashboard_monitoring_summary_ms"] = benchmark_dashboard_monitoring_summary(
        100, iterations, warmup
    )
    results["dashboard_operational_health_ms"] = benchmark_dashboard_operational_health(
        100, iterations, warmup
    )
    results["dashboard_shared_scan_cold_ms"] = benchmark_dashboard_shared_scan_cold(
        100, iterations, warmup
    )
    results["dashboard_shared_cache_warm_ms"] = benchmark_dashboard_shared_cache_warm(
        100, iterations, warmup
    )
    results["dashboard_shared_cache_ms"] = results["dashboard_shared_scan_cold_ms"]

    print("Running Benchmark 4/7: Scheduler Backlog Drain (concurrency 1 & 2)...")
    results["scheduler_drain_c1_ms"] = benchmark_scheduler_drain(
        concurrency=1, num_items=20, iterations=iterations, warmup=warmup
    )
    results["scheduler_drain_c2_ms"] = benchmark_scheduler_drain(
        concurrency=2, num_items=20, iterations=iterations, warmup=warmup
    )

    print("Running Benchmark 5/7: Repository Profiling (Small & Large)...")
    results["repository_profile_small_ms"] = benchmark_repository_profiling(
        tree_size="small", iterations=iterations, warmup=warmup
    )
    results["repository_profile_large_ms"] = benchmark_repository_profiling(
        tree_size="large", iterations=iterations, warmup=warmup
    )
    results["repository_profile_ms"] = results["repository_profile_small_ms"]

    print("Running Benchmark 6/7: Git Evidence Collection...")
    results["git_evidence_collection_ms"] = benchmark_git_evidence_collection(
        iterations=iterations, warmup=warmup
    )

    print("Running Benchmark 7/7: Prompt Construction & Output Parser...")
    results["prompt_and_parser_ms"] = benchmark_prompt_and_parser(iterations, warmup)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "repo_root": str(repo_root),
        },
        "benchmarks": results,
    }


def compare_with_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    threshold_pct: float = 20.0,
) -> tuple[list[str], bool]:
    lines: list[str] = []
    has_regression = False

    header = (
        f"{'Benchmark':<34} | {'Cur p50':<11} | {'Cur Mean':<11} | {'Base Mean':<11} | "
        f"{'Diff (ms)':<11} | {'Diff (%)':<10} | {'Status'}"
    )
    separator = "-" * len(header)
    lines.append(header)
    lines.append(separator)

    current_benchmarks = current.get("benchmarks", {})
    baseline_benchmarks = baseline.get("benchmarks", {})

    for name, cur_data in sorted(current_benchmarks.items()):
        base_data = baseline_benchmarks.get(name)
        c_p50 = float(cur_data.get("p50_ms", cur_data.get("median_ms", 0.0)))
        c_mean = float(cur_data.get("mean_ms", 0.0))

        if not base_data or "mean_ms" not in base_data:
            lines.append(
                f"{name:<34} | {c_p50:>8.3f} ms | {c_mean:>8.3f} ms | {'N/A':<11} | "
                f"{'N/A':<11} | {'N/A':<10} | NEW"
            )
            continue

        b_mean = float(base_data["mean_ms"])
        diff_ms = c_mean - b_mean
        diff_pct = (diff_ms / b_mean * 100.0) if b_mean > 0 else 0.0

        if diff_pct > threshold_pct:
            status = "REGRESSED"
            has_regression = True
        elif diff_pct < -threshold_pct:
            status = "IMPROVED"
        else:
            status = "OK"

        lines.append(
            f"{name:<34} | {c_p50:>8.3f} ms | {c_mean:>8.3f} ms | {b_mean:>8.3f} ms | "
            f"{diff_ms:>+10.3f} ms | {diff_pct:>+9.2f}% | {status}"
        )

    return lines, has_regression


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run local standard-library performance benchmarks for Software Agent Factory."
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Number of measurement iterations per benchmark (default: 5).",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of warmup iterations per benchmark (default: 1).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to write the output JSON benchmark report.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Path to a baseline JSON benchmark report to compare against.",
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=20.0,
        help="Maximum regression percentage allowed before flagging as REGRESSED (default: 20.0).",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit with code 1 if any benchmark regressed beyond threshold.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Path to target repository root to benchmark (default: current repository root).",
    )
    parser.add_argument(
        "--controller-comparison",
        action="store_true",
        help="Run standard vs fast fake-runtime controller comparison.",
    )
    parser.add_argument(
        "--controller-output",
        type=Path,
        help="Path to write the controller comparison JSON report.",
    )

    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve() if args.repo_root else Path(__file__).resolve().parents[2]

    print(
        f"Starting performance benchmarks on {repo_root} "
        f"(warmup={args.warmup}, iterations={args.iterations})..."
    )
    current_results = run_all_benchmarks(
        repo_root=repo_root,
        iterations=args.iterations,
        warmup=args.warmup,
    )

    print("\nBenchmark Results:")
    for name, data in sorted(current_results["benchmarks"].items()):
        p50 = data.get("p50_ms", data.get("median_ms", 0.0))
        p95 = data.get("p95_ms", 0.0)
        print(
            f"  {name:<34} p50={p50:>8.3f} ms p95={p95:>8.3f} ms mean={data['mean_ms']:>8.3f} ms "
            f"(min={data['min_ms']:>8.3f}, max={data['max_ms']:>8.3f}, "
            f"stddev={data['stddev_ms']:>6.3f})"
        )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(current_results, indent=2), encoding="utf-8")
        print(f"\nSaved benchmark results to {args.output}")

    if args.controller_comparison or args.controller_output:
        print("\nRunning Standard vs Fast Controller Comparison...")
        ctrl_results = run_controller_standard_vs_fast(repo_root)
        std_sum = ctrl_results["standard"]
        fast_sum = ctrl_results["fast"]
        print(
            f"  Standard Duration: {ctrl_results['summary']['standard_duration_ms']:.2f} ms "
            f"({std_sum['attempts_total']} attempts, {std_sum['invocations_total']} invocations)"
        )
        print(
            f"  Fast Duration:     {ctrl_results['summary']['fast_duration_ms']:.2f} ms "
            f"({fast_sum['attempts_total']} attempts, {fast_sum['invocations_total']} invocations)"
        )
        print(
            f"  Speedup:           {ctrl_results['summary']['speedup_pct']:+.2f}% "
            f"(Gates Preserved: {ctrl_results['summary']['gates_preserved']})"
        )
        gf = ctrl_results["summary"].get("gate_facts", {})
        print(
            f"  Gates Breakdown:   verification={gf.get('deterministic_verification_passed')} "
            f"tester={gf.get('tester_verified')} reviewer={gf.get('reviewer_verified')}"
        )
        if args.controller_output:
            args.controller_output.parent.mkdir(parents=True, exist_ok=True)
            args.controller_output.write_text(json.dumps(ctrl_results, indent=2), encoding="utf-8")
            print(f"Saved controller comparison to {args.controller_output}")

    has_regression = False
    if args.baseline:
        if not args.baseline.exists():
            print(f"\nError: baseline file not found: {args.baseline}", file=sys.stderr)
            return 2
        baseline_data = json.loads(args.baseline.read_text(encoding="utf-8"))
        print("\nComparison with Baseline:")
        comparison_lines, has_regression = compare_with_baseline(
            current=current_results,
            baseline=baseline_data,
            threshold_pct=args.threshold_pct,
        )
        for line in comparison_lines:
            print(line)

    if args.fail_on_regression and has_regression:
        print(
            "\nBenchmark failed due to performance regression beyond threshold.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
