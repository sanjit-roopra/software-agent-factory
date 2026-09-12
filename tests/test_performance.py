"""Tests for performance telemetry models, instrumentation, and benchmark harness."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from software_agent_factory.cli_output import render_status_report
from software_agent_factory.dashboard.sanitize import sanitize_performance
from software_agent_factory.models import (
    MAX_PERFORMANCE_MAP_ENTRIES,
    MAX_PERFORMANCE_METRICS,
    MAX_PERFORMANCE_NAME_LENGTH,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    FactoryRun,
    PerformanceMetric,
    PerformanceRecord,
    WorkflowState,
)
from software_agent_factory.observability import (
    MonitoringSnapshot,
    OperationalHealthReport,
    PageMeta,
    RunStateCounts,
    _compute_aggregate_metrics,
)
from software_agent_factory.telemetry import (
    count_operation,
    measure_operation,
    record_gate_failure,
    record_payload_size,
    record_process_timing,
    record_rework,
    record_run_store_scan,
    record_scheduler_event,
    record_subprocess_count,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, relative_path: str) -> ModuleType:
    script_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_bench = _load_script_module("benchmark", "scripts/performance/benchmark.py")
benchmark_prompt_and_parser = _bench.benchmark_prompt_and_parser
benchmark_repository_profiling = _bench.benchmark_repository_profiling
benchmark_run_store_scan = _bench.benchmark_run_store_scan
benchmark_dashboard_monitoring_summary = _bench.benchmark_dashboard_monitoring_summary
benchmark_dashboard_operational_health = _bench.benchmark_dashboard_operational_health
benchmark_dashboard_shared_cache = _bench.benchmark_dashboard_shared_cache
benchmark_dashboard_shared_scan_cold = _bench.benchmark_dashboard_shared_scan_cold
benchmark_dashboard_shared_cache_warm = _bench.benchmark_dashboard_shared_cache_warm
benchmark_scheduler_drain = _bench.benchmark_scheduler_drain
benchmark_git_evidence_collection = _bench.benchmark_git_evidence_collection
run_controller_standard_vs_fast = _bench.run_controller_standard_vs_fast
compare_with_baseline = _bench.compare_with_baseline


def test_performance_metric_validation() -> None:
    metric = PerformanceMetric(
        name="test.op",
        value=123.45,
        unit="ms",
        stage="IMPLEMENTING",
    )
    assert metric.name == "test.op"
    assert metric.value == 123.45
    assert metric.unit == "ms"

    # Negative values are rejected
    with pytest.raises(ValidationError):
        PerformanceMetric(name="test.op", value=-1.0, unit="ms")

    # Empty names are rejected
    with pytest.raises(ValidationError):
        PerformanceMetric(name="", value=10.0, unit="ms")


def test_performance_record_bounds_and_truncation() -> None:
    record = PerformanceRecord()

    # Oversized name gets truncated to MAX_PERFORMANCE_NAME_LENGTH
    long_name = "x" * (MAX_PERFORMANCE_NAME_LENGTH + 50)
    record.record_duration(long_name, 50.0)
    assert len(list(record.durations_ms.keys())[0]) == MAX_PERFORMANCE_NAME_LENGTH

    # Map entries are bounded to MAX_PERFORMANCE_MAP_ENTRIES
    for i in range(MAX_PERFORMANCE_MAP_ENTRIES + 20):
        record.record_counter(f"counter_{i}", 1)
    assert len(record.counters) <= MAX_PERFORMANCE_MAP_ENTRIES

    # Metric list is bounded to MAX_PERFORMANCE_METRICS
    for i in range(MAX_PERFORMANCE_METRICS + 20):
        record.add_metric(f"m_{i}", float(i), "ms")
    assert len(record.metrics) <= MAX_PERFORMANCE_METRICS


def test_performance_record_accumulation() -> None:
    record = PerformanceRecord()
    record.record_duration("stage.IMPLEMENTING", 100.0, accumulate=True)
    record.record_duration("stage.IMPLEMENTING", 200.0, accumulate=True)
    assert record.durations_ms["stage.IMPLEMENTING"] == 300.0

    record.record_duration("stage.VERIFYING", 50.0, accumulate=False)
    record.record_duration("stage.VERIFYING", 60.0, accumulate=False)
    assert record.durations_ms["stage.VERIFYING"] == 60.0

    record.record_counter("rework_total", 1)
    record.record_counter("rework_total", 2)
    assert record.counters["rework_total"] == 3


def test_measure_operation_accumulates_repeated_timings() -> None:
    record = PerformanceRecord()
    ticks = iter((1.0, 1.1, 2.0, 2.2))

    with measure_operation(record, "operation.profile", clock=lambda: next(ticks)):
        pass
    with measure_operation(record, "operation.profile", clock=lambda: next(ticks)):
        pass

    assert record.durations_ms["operation.profile"] == pytest.approx(300.0)


def test_schema_compatibility_with_defaults() -> None:
    # A serialized FactoryRun without performance fields deserializes cleanly with defaults
    raw_run_json = json.dumps(
        {
            "schema_version": 1,
            "id": "RUN-001",
            "work_item_id": "WI-001",
            "state": "PR_READY",
        }
    )
    run = FactoryRun.model_validate_json(raw_run_json)
    assert run.performance is not None
    assert isinstance(run.performance, PerformanceRecord)
    assert run.performance.durations_ms == {}
    assert run.performance.counters == {}
    assert run.performance.metrics == []


def test_telemetry_helper_functions() -> None:
    record = PerformanceRecord()

    with measure_operation(record, "custom_op"):
        pass
    assert "custom_op" in record.durations_ms
    assert record.durations_ms["custom_op"] >= 0.0

    count_operation(record, "items_processed", delta=5)
    assert record.counters["items_processed"] == 5

    record_gate_failure(record, "verification")
    assert record.counters["gate_failures_total"] == 1
    assert record.counters["gate_failure.verification"] == 1

    record_rework(record, "implementation_retry")
    assert record.counters["rework_total"] == 1
    assert record.counters["rework.implementation_retry"] == 1

    record_subprocess_count(record, delta=2)
    assert record.counters["subprocesses_total"] == 2

    record_run_store_scan(record, 15)
    assert record.counters["run_store_scan"] == 15

    record_process_timing(record, boot_ms=45.2, first_event_ms=120.8)
    assert record.process_boot_ms == 45.2
    assert record.first_event_ms == 120.8

    record_payload_size(record, prompt_chars=1200, response_chars=450)
    assert record.prompt_chars == 1200
    assert record.response_chars == 450

    record_scheduler_event(record, "wake", reason="completion", delta=1)
    assert record.counters["scheduler.wake"] == 1
    assert record.counters["scheduler.wake.completion"] == 1


def test_aggregate_metrics_computation() -> None:
    run1 = FactoryRun(
        id="RUN-1",
        work_item_id="WI-1",
        state=WorkflowState.PR_READY,
        performance=PerformanceRecord(
            durations_ms={"stage.IMPLEMENTING": 1000.0, "stage.VERIFYING": 200.0},
            counters={
                "gate_failures_total": 0,
                "rework_total": 0,
            },
            prompt_chars=1000,
            response_chars=300,
        ),
    )
    run2 = FactoryRun(
        id="RUN-2",
        work_item_id="WI-2",
        state=WorkflowState.DONE,
        performance=PerformanceRecord(
            durations_ms={"stage.IMPLEMENTING": 2000.0, "stage.VERIFYING": 400.0},
            counters={
                "gate_failures_total": 1,
                "gate_failure.verification": 1,
                "rework_total": 1,
            },
            prompt_chars=2000,
            response_chars=600,
        ),
    )

    agg = _compute_aggregate_metrics([run1, run2])
    assert agg.performance.total_prompt_chars == 3000
    assert agg.performance.total_response_chars == 900
    assert agg.performance.rework.total_gate_failures == 1
    assert agg.performance.rework.verification_gate_failures == 1
    assert agg.performance.rework.total_rework_attempts == 1
    assert agg.performance.rework.runs_with_rework == 1
    assert agg.performance.rework.rework_rate == 0.5

    assert "IMPLEMENTING" in agg.performance.stage_durations
    impl_summary = agg.performance.stage_durations["IMPLEMENTING"]
    assert impl_summary.count == 2
    assert impl_summary.average_seconds == 1.5


def test_aggregate_metrics_rework_fallback_semantics() -> None:
    now = datetime.now(timezone.utc)
    # 1. Initial + Polish: should have 0 rework attempts even when falling back from attempt_records
    run_polish = FactoryRun(
        id="RUN-POLISH",
        work_item_id="WI-POLISH",
        state=WorkflowState.DONE,
        attempt_records=[
            AttemptRecord(
                attempt_number=1,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.IMPLEMENTATION,
                triggered_by=AttemptTrigger.INITIAL,
                started_at=now,
                completed_at=now,
                outcome="SUCCESS",
            ),
            AttemptRecord(
                attempt_number=2,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.IMPLEMENTATION,
                triggered_by=AttemptTrigger.POLISH,
                started_at=now,
                completed_at=now,
                outcome="SUCCESS",
            ),
        ],
    )
    agg_polish = _compute_aggregate_metrics([run_polish])
    assert agg_polish.performance.rework.total_rework_attempts == 0
    assert agg_polish.performance.rework.runs_with_rework == 0

    # 2. Initial + Verification Repair: should have 1 rework attempt
    run_verify_repair = FactoryRun(
        id="RUN-VERIFY-REPAIR",
        work_item_id="WI-VERIFY-REPAIR",
        state=WorkflowState.DONE,
        attempt_records=[
            AttemptRecord(
                attempt_number=1,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.IMPLEMENTATION,
                triggered_by=AttemptTrigger.INITIAL,
                started_at=now,
                completed_at=now,
                outcome="FAILURE",
            ),
            AttemptRecord(
                attempt_number=2,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.IMPLEMENTATION,
                triggered_by=AttemptTrigger.VERIFICATION,
                started_at=now,
                completed_at=now,
                outcome="SUCCESS",
            ),
        ],
    )
    agg_verify = _compute_aggregate_metrics([run_verify_repair])
    assert agg_verify.performance.rework.total_rework_attempts == 1
    assert agg_verify.performance.rework.runs_with_rework == 1

    # 3. Initial + CI Repair: should have 1 rework attempt counted from CI repair
    run_ci_repair = FactoryRun(
        id="RUN-CI-REPAIR",
        work_item_id="WI-CI-REPAIR",
        state=WorkflowState.DONE,
        attempt_records=[
            AttemptRecord(
                attempt_number=1,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.IMPLEMENTATION,
                triggered_by=AttemptTrigger.INITIAL,
                started_at=now,
                completed_at=now,
                outcome="SUCCESS",
            ),
            AttemptRecord(
                attempt_number=1,
                role=AgentRole.IMPLEMENTER,
                model="claude-sonnet-5",
                reasoning="medium",
                budget=AttemptBudget.CI_REPAIR,
                triggered_by=AttemptTrigger.CI,
                started_at=now,
                completed_at=now,
                outcome="SUCCESS",
            ),
        ],
    )
    agg_ci = _compute_aggregate_metrics([run_ci_repair])
    assert agg_ci.performance.rework.total_rework_attempts == 1
    assert agg_ci.performance.rework.runs_with_rework == 1


def test_dashboard_sanitization_for_performance() -> None:
    raw = {
        "prompt_chars": 500,
        "response_chars": 200,
        "process_boot_ms": 35.5,
        "first_event_ms": 110.2,
        "durations_ms": {"stage.IMPLEMENTING": 500.0, "invalid": -10.0},
        "counters": {"gate_failures_total": 2},
        "sensitive_prompt_text": "secret source code",
        "diff_content": "--- a/foo\n+++ b/foo",
    }
    sanitized = sanitize_performance(raw)
    assert sanitized["prompt_chars"] == 500
    assert sanitized["response_chars"] == 200
    assert sanitized["process_boot_ms"] == 35.5
    assert sanitized["first_event_ms"] == 110.2
    assert "stage.IMPLEMENTING" in sanitized["durations_ms"]
    assert "invalid" not in sanitized["durations_ms"]
    assert sanitized["counters"]["gate_failures_total"] == 2
    # Ensure sensitive fields are completely dropped
    assert "sensitive_prompt_text" not in sanitized
    assert "diff_content" not in sanitized


def test_cli_output_status_report_renders_rework() -> None:
    snapshot = MonitoringSnapshot(
        generated_at=datetime.now(timezone.utc),
        total_runs=2,
        scanned_runs=2,
        unreadable_runs=0,
        scan_truncated=False,
        degraded=False,
        max_scanned_runs=100,
        stale_after_seconds=3600.0,
        counts=RunStateCounts(succeeded=2, escalated=0, failed=0, active=0, stale_active=0),
        metrics=_compute_aggregate_metrics(
            [
                FactoryRun(
                    id="RUN-1",
                    work_item_id="WI-1",
                    state=WorkflowState.DONE,
                    performance=PerformanceRecord(
                        counters={"gate_failures_total": 2, "rework_total": 1}
                    ),
                )
            ]
        ),
        page=PageMeta(total=1, offset=0, limit=20, returned=1, has_more=False),
        runs=[],
    )
    health = OperationalHealthReport(
        generated_at=datetime.now(timezone.utc),
        stale_after_seconds=3600.0,
        max_scanned_runs=100,
        total_runs=2,
        scanned_runs=2,
        scan_truncated=False,
        unreadable_runs=0,
        degraded=False,
        lock_check_supported=True,
        stale_runs=[],
        stale_locks=[],
        locks_checked=0,
        orphaned_workspaces=[],
        workspaces_checked=0,
    )
    lines = render_status_report(snapshot, health)
    rework_lines = [line for line in lines if "rework:" in line]
    assert len(rework_lines) == 1
    assert "2 gate failure(s)" in rework_lines[0]
    assert "1 rework attempt(s)" in rework_lines[0]


def _assert_benchmark_schema(result: dict[str, Any], expected_iterations: int = 1) -> None:
    assert result["iterations"] == expected_iterations
    assert "mean_ms" in result and result["mean_ms"] >= 0.0
    assert "median_ms" in result and result["median_ms"] >= 0.0
    assert "p50_ms" in result and result["p50_ms"] >= 0.0
    assert "p95_ms" in result and result["p95_ms"] >= 0.0
    assert "min_ms" in result and result["min_ms"] >= 0.0
    assert "max_ms" in result and result["max_ms"] >= 0.0
    assert "stddev_ms" in result and result["stddev_ms"] >= 0.0
    assert "samples_ms" in result and isinstance(result["samples_ms"], list)
    assert len(result["samples_ms"]) == expected_iterations


def test_benchmark_harness_components() -> None:
    # 1. Run store benchmark on synthetic runs
    scan_res = benchmark_run_store_scan(num_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(scan_res)

    # 2. Prompt construction and parser benchmark
    prompt_res = benchmark_prompt_and_parser(iterations=1, warmup=0)
    _assert_benchmark_schema(prompt_res)

    # 3. Repository profiling benchmark (small and large)
    repo_small = benchmark_repository_profiling("small", iterations=1, warmup=0)
    _assert_benchmark_schema(repo_small)

    repo_large = benchmark_repository_profiling("large", iterations=1, warmup=0)
    _assert_benchmark_schema(repo_large)

    # 4. Dashboard monitoring summary, health, and shared-scan cache
    dash_sum = benchmark_dashboard_monitoring_summary(store_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(dash_sum)

    dash_health = benchmark_dashboard_operational_health(store_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(dash_health)

    dash_shared = benchmark_dashboard_shared_cache(store_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(dash_shared)

    dash_cold = benchmark_dashboard_shared_scan_cold(store_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(dash_cold)

    dash_warm = benchmark_dashboard_shared_cache_warm(store_runs=10, iterations=1, warmup=0)
    _assert_benchmark_schema(dash_warm)

    # 5. Scheduler backlog drain at concurrency 1 and 2
    sched_c1 = benchmark_scheduler_drain(concurrency=1, num_items=5, iterations=1, warmup=0)
    _assert_benchmark_schema(sched_c1)
    assert sched_c1["items_dispatched"] == 5

    sched_c2 = benchmark_scheduler_drain(concurrency=2, num_items=5, iterations=1, warmup=0)
    _assert_benchmark_schema(sched_c2)
    assert sched_c2["items_dispatched"] == 5

    # 6. Git evidence collection
    git_ev = benchmark_git_evidence_collection(iterations=1, warmup=0)
    _assert_benchmark_schema(git_ev)


def test_scheduler_drain_exact_once_dispatch_and_concurrency() -> None:
    """Requirement 1: Verify each synthetic item is dispatched exactly once and
    concurrency 1 and 2 drain the exact same count."""
    count = 12
    sched_c1 = benchmark_scheduler_drain(concurrency=1, num_items=count, iterations=2, warmup=1)
    sched_c2 = benchmark_scheduler_drain(concurrency=2, num_items=count, iterations=2, warmup=1)

    _assert_benchmark_schema(sched_c1, expected_iterations=2)
    _assert_benchmark_schema(sched_c2, expected_iterations=2)

    assert sched_c1["items_dispatched"] == count
    assert sched_c2["items_dispatched"] == count
    assert sched_c1["items_dispatched"] == sched_c2["items_dispatched"]


def test_dashboard_shared_cache_fallback_on_execution_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 3: Verify baseline compatibility fallback handles TypeError
    at execution time when scan parameter is passed."""
    import software_agent_factory.observability as obs

    orig_snapshot = obs.build_monitoring_snapshot

    def _mock_snapshot(*args: Any, **kwargs: Any) -> Any:
        if "scan" in kwargs:
            raise TypeError("build_monitoring_snapshot() got an unexpected keyword argument 'scan'")
        return orig_snapshot(*args, **kwargs)

    monkeypatch.setattr(obs, "build_monitoring_snapshot", _mock_snapshot)

    # Both cold shared-scan and warm-cache benchmarks must execute cleanly via fallback
    cold_res = benchmark_dashboard_shared_scan_cold(store_runs=5, iterations=1, warmup=0)
    _assert_benchmark_schema(cold_res)

    warm_res = benchmark_dashboard_shared_cache_warm(store_runs=5, iterations=1, warmup=0)
    _assert_benchmark_schema(warm_res)


def test_run_controller_standard_vs_fast_schema() -> None:
    res = run_controller_standard_vs_fast()
    assert "summary" in res
    assert "standard" in res
    assert "fast" in res

    summary = res["summary"]
    assert summary["gates_preserved"] is True
    assert "speedup_pct" in summary
    assert summary["standard_duration_ms"] >= 0.0
    assert summary["fast_duration_ms"] >= 0.0

    # Requirement 2: Verify gates_preserved proves deterministic verification,
    # Tester, and Reviewer ran
    assert "gate_facts" in summary
    gate_facts = summary["gate_facts"]
    assert gate_facts["standard_gates_satisfied"] is True
    assert gate_facts["fast_gates_satisfied"] is True
    assert gate_facts["deterministic_verification_passed"] is True
    assert gate_facts["tester_verified"] is True
    assert gate_facts["reviewer_verified"] is True

    standard = res["standard"]
    assert standard["state"] == "PR_READY"
    assert standard["mode"] == "standard"
    assert standard["effective_performance_mode"] == "standard"
    assert standard["polish_attempts"] == 1
    assert "gate_facts" in standard
    std_gates = standard["gate_facts"]
    assert std_gates["gates_satisfied"] is True
    assert std_gates["deterministic_verification_ran"] is True
    assert std_gates["deterministic_verification_passed"] is True
    assert std_gates["tester_invoked"] is True
    assert std_gates["tester_success"] is True
    assert std_gates["test_report_persisted"] is True
    assert std_gates["reviewer_invoked"] is True
    assert std_gates["reviewer_success"] is True
    assert std_gates["review_report_persisted"] is True
    assert std_gates["review_approved"] is True

    fast = res["fast"]
    assert fast["state"] == "PR_READY"
    assert fast["mode"] == "fast"
    assert fast["effective_performance_mode"] == "fast"
    assert fast["performance_model_profile"] == "economy"
    assert fast["polish_attempts"] == 0
    assert "gate_facts" in fast
    fast_gates = fast["gate_facts"]
    assert fast_gates["gates_satisfied"] is True
    assert fast_gates["deterministic_verification_ran"] is True
    assert fast_gates["deterministic_verification_passed"] is True
    assert fast_gates["tester_invoked"] is True
    assert fast_gates["tester_success"] is True
    assert fast_gates["test_report_persisted"] is True
    assert fast_gates["reviewer_invoked"] is True
    assert fast_gates["reviewer_success"] is True
    assert fast_gates["review_report_persisted"] is True
    assert fast_gates["review_approved"] is True


def test_benchmark_baseline_comparison() -> None:
    current = {
        "benchmarks": {
            "cli_import_ms": {"mean_ms": 100.0, "p50_ms": 100.0},
            "run_store_scan_ms": {"mean_ms": 150.0, "p50_ms": 150.0},
            "prompt_and_parser_ms": {"mean_ms": 10.0, "p50_ms": 10.0},
        }
    }
    baseline = {
        "benchmarks": {
            "cli_import_ms": {"mean_ms": 100.0, "p50_ms": 100.0},
            "run_store_scan_ms": {"mean_ms": 100.0, "p50_ms": 100.0},  # +50% regression
            "prompt_and_parser_ms": {"mean_ms": 20.0, "p50_ms": 20.0},  # -50% improvement
        }
    }

    lines, has_regression = compare_with_baseline(current, baseline, threshold_pct=20.0)
    assert has_regression is True
    comparison_text = "\n".join(lines)
    assert "run_store_scan_ms" in comparison_text
    assert "REGRESSED" in comparison_text
    assert "IMPROVED" in comparison_text
    assert "OK" in comparison_text
