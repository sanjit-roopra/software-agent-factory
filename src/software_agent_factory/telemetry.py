"""Minimal, bounded performance telemetry instrumentation hooks.

This module provides instrumentation utilities to record operation and stage
timing, payload size attribution, process timings, subprocess counts, and
rework measures into :class:`~software_agent_factory.models.PerformanceRecord`.

Telemetry records contain numeric measurements, standard labels, and counts
only: repository content, prompts, diffs, secrets, and raw command logs are
strictly excluded.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .models import PerformanceRecord

__all__ = [
    "count_operation",
    "measure_operation",
    "record_gate_failure",
    "record_payload_size",
    "record_process_timing",
    "record_rework",
    "record_run_store_scan",
    "record_scheduler_event",
    "record_subprocess_count",
]


@contextmanager
def measure_operation(
    record: PerformanceRecord | None,
    name: str,
    *,
    stage: str | None = None,
    operation: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> Iterator[dict[str, Any]]:
    """Context manager to measure the wall-clock duration of an operation."""
    start = clock()
    info: dict[str, Any] = {}
    try:
        yield info
    finally:
        elapsed_ms = (clock() - start) * 1000.0
        if record is not None:
            record.record_duration(
                name,
                elapsed_ms,
                stage=stage,
                operation=operation,
                accumulate=True,
            )


def count_operation(
    record: PerformanceRecord | None,
    name: str,
    delta: int = 1,
    *,
    stage: str | None = None,
    operation: str | None = None,
) -> None:
    """Record an increment to a named counter on ``record``."""
    if record is not None and delta > 0:
        record.record_counter(name, delta=delta, stage=stage, operation=operation)


def record_gate_failure(
    record: PerformanceRecord | None,
    gate: str,
    *,
    stage: str | None = None,
) -> None:
    """Record a deterministic or review gate rejection event."""
    if record is not None:
        record.record_counter(f"gate_failure.{gate}", 1, stage=stage, operation="gate")
        record.record_counter("gate_failures_total", 1, stage=stage, operation="gate")


def record_rework(
    record: PerformanceRecord | None,
    rework_type: str,
    *,
    stage: str | None = None,
) -> None:
    """Record a workflow rework attempt or replan."""
    if record is not None:
        record.record_counter(f"rework.{rework_type}", 1, stage=stage, operation="rework")
        record.record_counter("rework_total", 1, stage=stage, operation="rework")


def record_subprocess_count(
    record: PerformanceRecord | None,
    kind: str = "general",
    delta: int = 1,
) -> None:
    """Record subprocess execution counts."""
    if record is not None and delta > 0:
        record.record_counter(f"subprocess.{kind}", delta, operation="subprocess")
        record.record_counter("subprocesses_total", delta, operation="subprocess")


def record_run_store_scan(
    record: PerformanceRecord | None,
    count: int = 1,
) -> None:
    """Record a run-store scan operation."""
    if record is not None and count > 0:
        record.record_counter("run_store_scan", count, operation="store")


def record_process_timing(
    record: PerformanceRecord | None,
    *,
    boot_ms: float | None = None,
    first_event_ms: float | None = None,
) -> None:
    """Record process boot and first-event latency where available."""
    if record is not None:
        if boot_ms is not None:
            record.process_boot_ms = max(0.0, float(boot_ms))
        if first_event_ms is not None:
            record.first_event_ms = max(0.0, float(first_event_ms))


def record_payload_size(
    record: PerformanceRecord | None,
    *,
    prompt_chars: int | None = None,
    response_chars: int | None = None,
) -> None:
    """Record prompt and response size attribution in character count."""
    if record is not None:
        record.record_size(prompt_chars=prompt_chars, response_chars=response_chars)


def record_scheduler_event(
    record: PerformanceRecord | None,
    event_type: str = "wake",
    *,
    reason: str | None = None,
    delta: int = 1,
) -> None:
    """Record generic scheduler events such as wakeups, polls, and completions."""
    if record is not None and delta > 0:
        clean_event = event_type.strip().lower()
        record.record_counter(f"scheduler.{clean_event}", delta, operation="scheduler")
        if reason:
            clean_reason = reason.strip().lower()
            record.record_counter(
                f"scheduler.{clean_event}.{clean_reason}", delta, operation="scheduler"
            )
