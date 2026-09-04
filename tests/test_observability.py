"""Tests for software_agent_factory.observability.

Covers the Phase 15.5 local monitoring/health contract:
- build_monitoring_snapshot() aggregation, pagination, staleness, safe
  per-run summaries, and aggregate metrics (attempts, first-pass success,
  scope replans, CI repair attempts, completed-run durations), against both
  a real FileRunStore and a narrow in-memory fake implementing
  RunStoreProtocol.
- corrupt/missing run file handling (degraded, never silently "healthy").
- build_operational_health() stale-run/stale-lock/orphaned-workspace
  findings, strictly read-only.
- configure_factory_logging()/log_run_event() structured JSON logging.

No network, no model calls, no clock dependence beyond timestamps the tests
construct themselves.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    Complexity,
    FactoryRun,
    Risk,
    RunLease,
    TriageResult,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.observability import (
    DEFAULT_MAX_SCANNED_RUNS,
    DEFAULT_STALE_AFTER,
    MonitoringSnapshot,
    OperationalHealthReport,
    OrphanedWorkspaceFinding,
    RunStoreProtocol,
    RunSummary,
    StaleLockFinding,
    StaleRunFinding,
    build_monitoring_snapshot,
    build_operational_health,
    configure_factory_logging,
    log_run_event,
)
from software_agent_factory.store import FileRunStore

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux only per AGENTS.md
    fcntl = None

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _run(
    run_id: str,
    *,
    state: WorkflowState = WorkflowState.CREATED,
    created_at: datetime = T0,
    updated_at: datetime | None = None,
    last_activity_at: datetime | None = None,
    completed_at: datetime | None = None,
    lease: RunLease | None = None,
    attempt_records: list[AttemptRecord] | None = None,
    workspace_path: str | None = None,
) -> FactoryRun:
    return FactoryRun(
        id=run_id,
        work_item_id=f"WI-{run_id}",
        state=state,
        created_at=created_at,
        updated_at=updated_at or created_at,
        last_activity_at=last_activity_at,
        completed_at=completed_at,
        lease=lease,
        attempt_records=attempt_records or [],
        workspace_path=workspace_path,
    )


def _attempt(
    number: int,
    *,
    role: AgentRole = AgentRole.IMPLEMENTER,
    model: str = "claude-sonnet-5",
    budget: AttemptBudget = AttemptBudget.IMPLEMENTATION,
    trigger: AttemptTrigger = AttemptTrigger.INITIAL,
    started_at: datetime = T0,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=number,
        role=role,
        model=model,
        reasoning="medium",
        started_at=started_at,
        completed_at=started_at + timedelta(minutes=1),
        outcome="ok",
        budget=budget,
        triggered_by=trigger,
    )


# ---------------------------------------------------------------------------
# A narrow fake satisfying RunStoreProtocol, decoupled from real file
# serialization -- used to prove the module works against a fake, not just
# the real FileRunStore.
# ---------------------------------------------------------------------------


@dataclass
class FakeRunStore:
    runs_dir: Path
    _runs: dict[str, FactoryRun] = field(default_factory=dict)
    _errors: dict[str, Exception] = field(default_factory=dict)
    _artifacts: dict[tuple[str, type], object] = field(default_factory=dict)

    def add_run(self, run: FactoryRun) -> None:
        self._runs[run.id] = run
        self._touch_marker(run.id)

    def add_broken(self, run_id: str, exc: Exception) -> None:
        self._errors[run_id] = exc
        self._touch_marker(run_id)

    def add_bare_directory(self, run_id: str) -> None:
        """Create a run directory with no ``run.json`` at all, e.g. an
        interrupted first save. ``load_run`` for it must behave exactly like
        the real ``FileRunStore`` would: raise ``FileNotFoundError``."""
        (self.runs_dir / run_id).mkdir(parents=True, exist_ok=True)

    def add_artifact(self, run_id: str, artifact: object) -> None:
        self._artifacts[(run_id, type(artifact))] = artifact

    def marker_path(self, run_id: str) -> Path:
        return self.runs_dir / run_id / "run.json"

    def _touch_marker(self, run_id: str) -> None:
        marker = self.marker_path(run_id)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")

    def load_run(self, run_id: str) -> FactoryRun:
        if run_id in self._errors:
            raise self._errors[run_id]
        try:
            return self._runs[run_id]
        except KeyError:
            raise FileNotFoundError(f"no run.json for {run_id}") from None

    def load_artifact(self, run_id, artifact_type, filename=None, *, attempt=None):
        try:
            return self._artifacts[(run_id, artifact_type)]
        except KeyError:
            raise FileNotFoundError(f"no {artifact_type.__name__} for {run_id}") from None


def _fake_store(tmp_path: Path) -> FakeRunStore:
    return FakeRunStore(runs_dir=tmp_path / "runs")


# ---------------------------------------------------------------------------
# build_monitoring_snapshot: empty store
# ---------------------------------------------------------------------------


def test_empty_store_yields_zeroed_snapshot(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert isinstance(snapshot, MonitoringSnapshot)
    assert snapshot.total_runs == 0
    assert snapshot.unreadable_runs == 0
    assert snapshot.degraded is False
    assert snapshot.degraded_reasons == []
    assert snapshot.counts.succeeded == 0
    assert snapshot.counts.active == 0
    assert snapshot.page.total == 0
    assert snapshot.page.returned == 0
    assert snapshot.page.has_more is False
    assert snapshot.runs == []


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------


def test_counts_classify_every_terminal_and_active_state(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("done", state=WorkflowState.DONE, completed_at=T0))
    store.add_run(
        _run(
            "pr-ready-finalized",
            state=WorkflowState.PR_READY,
            completed_at=T0,
        )
    )
    store.add_run(
        _run(
            "pr-ready-unfinalized",
            state=WorkflowState.PR_READY,
            completed_at=None,
            updated_at=T0,
        )
    )
    store.add_run(_run("needs-human", state=WorkflowState.NEEDS_HUMAN))
    store.add_run(_run("failed", state=WorkflowState.FAILED))
    store.add_run(_run("implementing", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0, limit=100)

    # DONE and the *finalized* PR_READY run both count as succeeded; the
    # interrupted (unfinalized) PR_READY run counts as active instead, per
    # workflow.is_run_finished.
    assert snapshot.counts.succeeded == 2
    assert snapshot.counts.escalated == 1
    assert snapshot.counts.failed == 1
    assert snapshot.counts.active == 2  # pr-ready-unfinalized + implementing
    assert snapshot.total_runs == 6

    by_id = {run.run_id: run for run in snapshot.runs}
    assert by_id["pr-ready-finalized"].is_finished is True
    assert by_id["pr-ready-unfinalized"].is_finished is False


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_stale_active_run_detected_from_heartbeat_age(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    stale_after = timedelta(minutes=15)
    old_heartbeat = T0 - timedelta(hours=1)
    fresh_heartbeat = T0 - timedelta(minutes=1)

    store.add_run(
        _run(
            "stale",
            state=WorkflowState.IMPLEMENTING,
            created_at=old_heartbeat,
            updated_at=old_heartbeat,
            lease=RunLease(host="mac", pid=1, heartbeat_at=old_heartbeat),
        )
    )
    store.add_run(
        _run(
            "fresh",
            state=WorkflowState.IMPLEMENTING,
            created_at=fresh_heartbeat,
            updated_at=fresh_heartbeat,
            lease=RunLease(host="mac", pid=2, heartbeat_at=fresh_heartbeat),
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0, stale_after=stale_after)

    assert snapshot.counts.active == 2
    assert snapshot.counts.stale_active == 1
    by_id = {run.run_id: run for run in snapshot.runs}
    assert by_id["stale"].is_stale is True
    assert by_id["fresh"].is_stale is False


def test_finished_runs_are_never_flagged_stale_even_if_old(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "old-done",
            state=WorkflowState.DONE,
            created_at=T0 - timedelta(days=30),
            updated_at=T0 - timedelta(days=30),
            completed_at=T0 - timedelta(days=30),
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0, stale_after=timedelta(minutes=1))

    assert snapshot.counts.stale_active == 0
    assert snapshot.runs[0].is_stale is False


def test_run_with_no_heartbeat_falls_back_to_created_at_for_staleness(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "never-touched",
            state=WorkflowState.CREATED,
            created_at=T0 - timedelta(hours=2),
            updated_at=T0 - timedelta(hours=2),
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0, stale_after=timedelta(minutes=15))

    assert snapshot.runs[0].is_stale is True


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_sorts_newest_first_and_reports_page_metadata(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    for i in range(5):
        moment = T0 + timedelta(minutes=i)
        store.add_run(_run(f"run-{i}", created_at=moment, updated_at=moment))

    later = T0 + timedelta(hours=1)
    first_page = build_monitoring_snapshot(store, now=later, limit=2, offset=0)
    assert [r.run_id for r in first_page.runs] == ["run-4", "run-3"]
    assert first_page.page.limit == 2
    assert first_page.page.offset == 0
    assert first_page.page.returned == 2
    assert first_page.page.total == 5
    assert first_page.page.has_more is True

    second_page = build_monitoring_snapshot(store, now=later, limit=2, offset=2)
    assert [r.run_id for r in second_page.runs] == ["run-2", "run-1"]
    assert second_page.page.has_more is True

    last_page = build_monitoring_snapshot(store, now=later, limit=2, offset=4)
    assert [r.run_id for r in last_page.runs] == ["run-0"]
    assert last_page.page.has_more is False
    assert last_page.page.returned == 1


def test_offset_past_end_returns_empty_page_not_an_error(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("only-run"))

    snapshot = build_monitoring_snapshot(store, now=T0, limit=10, offset=50)

    assert snapshot.runs == []
    assert snapshot.page.total == 1
    assert snapshot.page.returned == 0
    assert snapshot.page.has_more is False


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": -1}, "limit"),
        ({"offset": -1}, "offset"),
        ({"stale_after": timedelta(0)}, "stale_after"),
        ({"stale_after": timedelta(seconds=-1)}, "stale_after"),
        ({"max_scanned_runs": 0}, "max_scanned_runs"),
        ({"max_scanned_runs": -1}, "max_scanned_runs"),
    ],
)
def test_invalid_arguments_raise_value_error(tmp_path: Path, kwargs: dict, match: str) -> None:
    store = _fake_store(tmp_path)
    with pytest.raises(ValueError, match=match):
        build_monitoring_snapshot(store, now=T0, **kwargs)


def test_naive_now_is_rejected(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_monitoring_snapshot(store, now=datetime(2026, 9, 1, 12, 0))  # noqa: DTZ001


def test_default_stale_after_is_positive() -> None:
    assert DEFAULT_STALE_AFTER > timedelta(0)


def test_default_max_scanned_runs_is_conservative() -> None:
    assert DEFAULT_MAX_SCANNED_RUNS == 1000


def test_snapshot_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("a", state=WorkflowState.DONE, completed_at=T0))
    store.add_run(_run("b", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    first = build_monitoring_snapshot(store, now=T0)
    second = build_monitoring_snapshot(store, now=T0)

    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
        exclude={"generated_at"}
    )


# ---------------------------------------------------------------------------
# Attempt tallies
# ---------------------------------------------------------------------------


def test_attempt_tallies_by_role_and_model_and_budget(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "run-1",
            state=WorkflowState.VERIFYING,
            updated_at=T0,
            attempt_records=[
                _attempt(1, role=AgentRole.IMPLEMENTER, model="claude-sonnet-5"),
                _attempt(
                    2,
                    role=AgentRole.TESTER,
                    model="claude-sonnet-5",
                    budget=AttemptBudget.CI_REPAIR,
                ),
                _attempt(3, role=AgentRole.IMPLEMENTER, model="gpt-5.6-sol"),
            ],
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.attempts_by_role == {"IMPLEMENTER": 2, "TESTER": 1}
    assert snapshot.attempts_by_model == {"claude-sonnet-5": 2, "gpt-5.6-sol": 1}
    run_summary = snapshot.runs[0]
    assert run_summary.attempt_count == 3
    assert run_summary.implementation_attempts == 2
    assert run_summary.ci_repair_attempts == 1


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def test_aggregate_metrics_total_and_average_attempts(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "run-1",
            state=WorkflowState.DONE,
            completed_at=T0,
            attempt_records=[_attempt(1), _attempt(2)],
        )
    )
    store.add_run(_run("run-2", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.metrics.total_attempts == 2
    assert snapshot.metrics.average_attempts_per_run == pytest.approx(1.0)


def test_aggregate_metrics_average_attempts_is_none_for_empty_store(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.metrics.total_attempts == 0
    assert snapshot.metrics.average_attempts_per_run is None


def test_aggregate_metrics_implementation_and_ci_repair_totals(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "run-1",
            state=WorkflowState.DONE,
            completed_at=T0,
            attempt_records=[
                _attempt(1, budget=AttemptBudget.IMPLEMENTATION),
                _attempt(2, budget=AttemptBudget.CI_REPAIR),
                _attempt(3, budget=AttemptBudget.CI_REPAIR),
            ],
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.metrics.implementation_attempts == 1
    assert snapshot.metrics.ci_repair_attempts == 2


def test_aggregate_metrics_scope_replans_counts_scope_triggered_attempts(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "run-1",
            state=WorkflowState.PLANNING,
            updated_at=T0,
            attempt_records=[
                _attempt(1, trigger=AttemptTrigger.INITIAL),
                _attempt(2, trigger=AttemptTrigger.SCOPE),
            ],
        )
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.metrics.scope_replans == 1


def test_first_pass_success_counts_single_implementation_attempt_succeeded_runs(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    # First-pass success: exactly one IMPLEMENTATION attempt, then DONE.
    store.add_run(
        _run(
            "first-pass",
            state=WorkflowState.DONE,
            completed_at=T0,
            attempt_records=[_attempt(1, budget=AttemptBudget.IMPLEMENTATION)],
        )
    )
    # Succeeded, but needed a retry -- not first-pass.
    store.add_run(
        _run(
            "retried",
            state=WorkflowState.DONE,
            completed_at=T0,
            attempt_records=[
                _attempt(1, budget=AttemptBudget.IMPLEMENTATION),
                _attempt(2, budget=AttemptBudget.IMPLEMENTATION, trigger=AttemptTrigger.REVIEW),
            ],
        )
    )
    # Escalated: excluded from both numerator and denominator.
    store.add_run(_run("escalated", state=WorkflowState.NEEDS_HUMAN, completed_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0)

    first_pass = snapshot.metrics.first_pass_success
    assert first_pass.denominator == 2
    assert first_pass.numerator == 1
    assert first_pass.rate == pytest.approx(0.5)


def test_first_pass_success_rate_is_none_when_no_succeeded_runs(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("failed", state=WorkflowState.FAILED, completed_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0)

    first_pass = snapshot.metrics.first_pass_success
    assert first_pass.denominator == 0
    assert first_pass.numerator == 0
    assert first_pass.rate is None


def test_completed_run_durations_summary_spans_every_finished_state(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "done",
            state=WorkflowState.DONE,
            created_at=T0,
            completed_at=T0 + timedelta(minutes=10),
        )
    )
    store.add_run(
        _run(
            "failed",
            state=WorkflowState.FAILED,
            created_at=T0,
            completed_at=T0 + timedelta(minutes=30),
        )
    )
    # Active (not finished): excluded from the duration summary entirely.
    store.add_run(_run("active", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0 + timedelta(hours=1))

    durations = snapshot.metrics.completed_run_durations
    assert durations.count == 2
    assert durations.min_seconds == pytest.approx(600.0)
    assert durations.max_seconds == pytest.approx(1800.0)
    assert durations.average_seconds == pytest.approx(1200.0)


def test_completed_run_durations_is_none_when_no_finished_runs(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("active", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0)

    durations = snapshot.metrics.completed_run_durations
    assert durations.count == 0
    assert durations.min_seconds is None
    assert durations.max_seconds is None
    assert durations.average_seconds is None


def test_no_run_ever_reports_a_cost_field(tmp_path: Path) -> None:
    """AttemptRecord persists no token/cost data; the snapshot must not
    fabricate one (ADR-017: unknown stays unknown, never zero)."""
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1", attempt_records=[_attempt(1)]))

    snapshot = build_monitoring_snapshot(store, now=T0)

    dumped = json.dumps(snapshot.model_dump(mode="json"))
    assert "cost" not in dumped
    assert "token" not in dumped


# ---------------------------------------------------------------------------
# Safe per-run summaries: title/complexity/risk resolution
# ---------------------------------------------------------------------------


def test_title_is_populated_from_work_item_and_redacted(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1"))
    store.add_artifact(
        "run-1",
        WorkItem(
            id="WI-run-1",
            title="Fix bug using token ghp_abcdefghijklmnopqrstuvwxyz012345",
            description="d",
        ),
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    title = snapshot.runs[0].title
    assert title is not None
    assert "ghp_" not in title
    assert "[REDACTED]" in title


def test_missing_work_item_yields_none_title_and_no_crash(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1"))

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.runs[0].title is None
    assert snapshot.runs[0].complexity is None
    assert snapshot.runs[0].risk is None
    assert snapshot.degraded is False  # a missing *artifact* is not a corrupt run.json


def test_complexity_and_risk_prefer_triage_result_over_work_item(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1"))
    store.add_artifact(
        "run-1",
        WorkItem(
            id="WI-run-1",
            title="t",
            description="d",
            complexity=Complexity.L0,
            risk=Risk.R0,
        ),
    )
    store.add_artifact(
        "run-1",
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="good",
            needs_research=False,
            confidence=0.9,
        ),
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.runs[0].complexity is Complexity.L2
    assert snapshot.runs[0].risk is Risk.R2


def test_complexity_and_risk_fall_back_to_work_item_without_triage(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1"))
    store.add_artifact(
        "run-1",
        WorkItem(
            id="WI-run-1",
            title="t",
            description="d",
            complexity=Complexity.L1,
            risk=Risk.R1,
        ),
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.runs[0].complexity is Complexity.L1
    assert snapshot.runs[0].risk is Risk.R1


# ---------------------------------------------------------------------------
# Corrupt/missing run.json handling (real FileRunStore)
# ---------------------------------------------------------------------------


def test_corrupt_run_file_is_reported_as_degraded_not_raised(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    good_run = _run("good", state=WorkflowState.DONE, completed_at=T0)
    store.save_run(good_run)

    corrupt_dir = store.runs_dir / "corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "run.json").write_text("{not valid json", encoding="utf-8")

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.degraded is True
    assert snapshot.unreadable_runs == 1
    assert snapshot.total_runs == 2
    assert any("invalid_json" in reason for reason in snapshot.degraded_reasons)
    # the readable run is still reported, not dropped silently alongside the
    # corrupt one.
    assert snapshot.page.total == 1
    assert snapshot.runs[0].run_id == "good"


def test_run_file_failing_schema_validation_is_reported_as_degraded(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    broken_dir = store.runs_dir / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "run.json").write_text(
        json.dumps({"schema_version": 1, "id": "broken"}), encoding="utf-8"
    )  # missing required fields (work_item_id, state)

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.degraded is True
    assert snapshot.unreadable_runs == 1
    assert any("validation_error" in reason for reason in snapshot.degraded_reasons)


def test_run_file_with_unsupported_schema_version_is_reported_as_degraded(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    broken_dir = store.runs_dir / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "run.json").write_text(
        json.dumps({"schema_version": 2, "id": "broken"}), encoding="utf-8"
    )

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.degraded is True
    assert snapshot.unreadable_runs == 1


def test_fake_broken_run_categorizes_missing_file_error(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_broken("gone", FileNotFoundError("run.json"))

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.degraded is True
    assert any("missing" in reason for reason in snapshot.degraded_reasons)


def test_bare_run_directory_without_run_json_is_unreadable_not_invisible(
    tmp_path: Path,
) -> None:
    """A run directory can exist (e.g. ``store.run_dir`` was created for
    governance evidence, or ``save_run`` was interrupted before writing
    ``run.json``) without ever containing a ``run.json``. Enumerating
    *directories* rather than existing ``run.json`` files must still surface
    it as an unreadable/degraded run rather than skipping it silently."""
    store = _fake_store(tmp_path)
    store.add_run(_run("good", state=WorkflowState.DONE, completed_at=T0))
    store.add_bare_directory("no-run-json-yet")

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.total_runs == 2
    assert snapshot.scanned_runs == 2
    assert snapshot.unreadable_runs == 1
    assert snapshot.degraded is True
    assert any("missing" in reason for reason in snapshot.degraded_reasons)
    assert snapshot.page.total == 1
    assert snapshot.runs[0].run_id == "good"


def test_real_store_directory_missing_run_json_is_reported_as_degraded(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    store.save_run(_run("good", state=WorkflowState.DONE, completed_at=T0))
    # A directory with no run.json at all, distinct from an invalid/corrupt
    # one: e.g. store.run_dir(...) was called for evidence but save_run
    # never completed.
    (store.runs_dir / "empty-dir").mkdir(parents=True)

    snapshot = build_monitoring_snapshot(store, now=T0)

    assert snapshot.total_runs == 2
    assert snapshot.unreadable_runs == 1
    assert snapshot.degraded is True
    assert any("missing" in reason for reason in snapshot.degraded_reasons)
    assert snapshot.page.total == 1


# ---------------------------------------------------------------------------
# Bounded scan: directory enumeration is broad, JSON parsing is capped
# ---------------------------------------------------------------------------


def _set_mtime(path: Path, when: datetime) -> None:
    timestamp = when.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_scan_within_cap_is_never_truncated(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    for i in range(3):
        store.add_run(_run(f"run-{i}", created_at=T0, updated_at=T0))

    snapshot = build_monitoring_snapshot(store, now=T0, max_scanned_runs=10)

    assert snapshot.total_runs == 3
    assert snapshot.scanned_runs == 3
    assert snapshot.scan_truncated is False
    assert snapshot.degraded is False


def test_scan_cap_selects_newest_directories_by_mtime_and_reports_truncation(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    for i in range(5):
        run_id = f"run-{i}"
        store.add_run(_run(run_id, created_at=T0, updated_at=T0))
        # Stagger marker mtimes so run-4 is newest, run-0 is oldest,
        # independent of FactoryRun.created_at (which is identical for all
        # five here) -- the scan cap orders by filesystem recency, not by
        # parsed run content, since ordering must happen *before* any
        # parsing occurs.
        _set_mtime(store.marker_path(run_id), T0 + timedelta(seconds=i))

    snapshot = build_monitoring_snapshot(store, now=T0, max_scanned_runs=3, limit=100)

    assert snapshot.total_runs == 5
    assert snapshot.scanned_runs == 3
    assert snapshot.scan_truncated is True
    assert snapshot.degraded is True
    assert any("capped at 3 of 5" in reason for reason in snapshot.degraded_reasons)
    scanned_ids = {run.run_id for run in snapshot.runs}
    assert scanned_ids == {"run-4", "run-3", "run-2"}


def test_scan_truncation_limits_counts_to_scanned_subset(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    for i in range(4):
        run_id = f"run-{i}"
        store.add_run(_run(run_id, state=WorkflowState.DONE, created_at=T0, completed_at=T0))
        _set_mtime(store.marker_path(run_id), T0 + timedelta(seconds=i))

    snapshot = build_monitoring_snapshot(store, now=T0, max_scanned_runs=2)

    # Only the 2 newest-by-mtime runs were parsed, so counts reflect just
    # those 2, not all 4 -- this must be honest, not silently store-wide.
    assert snapshot.counts.succeeded == 2
    assert snapshot.page.total == 2


# ---------------------------------------------------------------------------
# Snapshot never exposes prohibited content by construction
# ---------------------------------------------------------------------------


def test_snapshot_schema_excludes_prohibited_fields() -> None:
    prohibited_substrings = ("prompt", "diff", "log", "token", "stdout", "stderr", "secret")
    all_field_names = list(MonitoringSnapshot.model_fields) + list(RunSummary.model_fields)
    lowered = " ".join(all_field_names).lower()
    for substring in prohibited_substrings:
        assert substring not in lowered


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


def test_configure_factory_logging_writes_bounded_json_lines(tmp_path: Path) -> None:
    logger_name = "software_agent_factory.test.logging"
    logger = configure_factory_logging(tmp_path, logger_name=logger_name)
    try:
        log_run_event(
            logger,
            "run started",
            run_id="RUN-1",
            state=WorkflowState.TRIAGING,
        )

        log_path = tmp_path / "logs" / "factory.log"
        assert log_path.is_file()
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)

        assert payload["message"] == "run started"
        assert payload["run_id"] == "RUN-1"
        assert payload["state"] == "TRIAGING"
        assert payload["logger"] == logger_name
        assert payload["level"] == "INFO"
        assert "timestamp" in payload
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_configure_factory_logging_is_idempotent(tmp_path: Path) -> None:
    logger_name = "software_agent_factory.test.idempotent"
    logger1 = configure_factory_logging(tmp_path, logger_name=logger_name)
    logger2 = configure_factory_logging(tmp_path, logger_name=logger_name)
    try:
        assert logger1 is logger2
        assert len(logger1.handlers) == 1
    finally:
        for handler in list(logger1.handlers):
            logger1.removeHandler(handler)
            handler.close()


def test_log_message_redacts_obvious_secrets(tmp_path: Path) -> None:
    logger_name = "software_agent_factory.test.redaction"
    logger = configure_factory_logging(tmp_path, logger_name=logger_name)
    try:
        log_run_event(logger, "token leaked: ghp_abcdefghijklmnopqrstuvwxyz012345")

        log_path = tmp_path / "logs" / "factory.log"
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)

        assert "ghp_" not in payload["message"]
        assert "[REDACTED]" in payload["message"]
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_log_event_without_run_id_or_state_omits_those_keys(tmp_path: Path) -> None:
    logger_name = "software_agent_factory.test.no_extra"
    logger = configure_factory_logging(tmp_path, logger_name=logger_name)
    try:
        log_run_event(logger, "plain message")

        log_path = tmp_path / "logs" / "factory.log"
        line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        payload = json.loads(line)

        assert "run_id" not in payload
        assert "state" not in payload
        assert payload["message"] == "plain message"
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_log_file_stays_under_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "factory-data"
    logger_name = "software_agent_factory.test.location"
    logger = configure_factory_logging(data_dir, logger_name=logger_name)
    try:
        log_run_event(logger, "hello")
        log_path = data_dir / "logs" / "factory.log"
        assert log_path.is_file()
        assert log_path.resolve().is_relative_to(data_dir.resolve())
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_log_handler_bounds_file_size_with_rotation(tmp_path: Path) -> None:
    logger_name = "software_agent_factory.test.rotation"
    logger = configure_factory_logging(
        tmp_path, logger_name=logger_name, max_bytes=500, backup_count=2
    )
    try:
        for i in range(200):
            log_run_event(logger, f"message number {i} padded with filler text")

        logs_dir = tmp_path / "logs"
        rotated = list(logs_dir.glob("factory.log*"))
        # at least one rotation must have occurred, and never more than
        # backup_count + 1 files.
        assert len(rotated) >= 2
        assert len(rotated) <= 3
        for path in rotated:
            assert path.stat().st_size <= 500 + 4096  # small slack for one record
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_configure_factory_logging_returns_standard_logger(tmp_path: Path) -> None:
    logger = configure_factory_logging(tmp_path, logger_name="software_agent_factory.test.type")
    try:
        assert isinstance(logger, logging.Logger)
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------------------
# build_operational_health(): stale runs, stale locks, orphaned workspaces
# ---------------------------------------------------------------------------


def test_operational_health_on_empty_store_is_clean(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)

    report = build_operational_health(store, now=T0)

    assert isinstance(report, OperationalHealthReport)
    assert report.degraded is False
    assert report.stale_runs == []
    assert report.stale_locks == []
    assert report.orphaned_workspaces == []
    assert report.locks_checked == 0
    assert report.workspaces_checked == 0


def test_stale_run_finding_reports_idle_active_run(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    old_heartbeat = T0 - timedelta(hours=2)
    store.add_run(
        _run(
            "stalled",
            state=WorkflowState.IMPLEMENTING,
            created_at=old_heartbeat,
            updated_at=old_heartbeat,
            lease=RunLease(host="mac", pid=123, heartbeat_at=old_heartbeat),
            workspace_path="/data/workspaces/stalled-key",
        )
    )

    report = build_operational_health(store, now=T0, stale_after=timedelta(minutes=15))

    assert len(report.stale_runs) == 1
    finding = report.stale_runs[0]
    assert isinstance(finding, StaleRunFinding)
    assert finding.run_id == "stalled"
    assert finding.work_item_id == "WI-stalled"
    assert finding.state is WorkflowState.IMPLEMENTING
    assert finding.workspace_path == "/data/workspaces/stalled-key"
    assert finding.idle_seconds == pytest.approx(7200.0)


def test_finished_runs_never_appear_as_stale_run_findings(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    old = T0 - timedelta(days=10)
    store.add_run(_run("old-done", state=WorkflowState.DONE, created_at=old, completed_at=old))
    store.add_run(
        _run("old-failed", state=WorkflowState.FAILED, created_at=old, completed_at=old)
    )
    store.add_run(
        _run("old-escalated", state=WorkflowState.NEEDS_HUMAN, created_at=old, completed_at=old)
    )

    report = build_operational_health(store, now=T0, stale_after=timedelta(minutes=1))

    assert report.stale_runs == []


def test_fresh_active_run_is_not_a_stale_run_finding(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("fresh", state=WorkflowState.IMPLEMENTING, updated_at=T0))

    report = build_operational_health(store, now=T0, stale_after=timedelta(minutes=15))

    assert report.stale_runs == []


def test_orphaned_workspace_directory_is_reported(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1", workspace_path=str(tmp_path / "workspaces" / "run-1-key")))
    (tmp_path / "workspaces" / "run-1-key").mkdir(parents=True)
    (tmp_path / "workspaces" / "orphan-key").mkdir(parents=True)

    report = build_operational_health(store, now=T0)

    assert report.workspaces_checked == 2
    assert [f.workspace_name for f in report.orphaned_workspaces] == ["orphan-key"]
    assert isinstance(report.orphaned_workspaces[0], OrphanedWorkspaceFinding)
    # never deleted
    assert (tmp_path / "workspaces" / "orphan-key").is_dir()


def test_workspace_referenced_by_run_is_not_orphaned(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    workspace_dir = tmp_path / "workspaces" / "referenced-key"
    workspace_dir.mkdir(parents=True)
    store.add_run(_run("run-1", workspace_path=str(workspace_dir)))

    report = build_operational_health(store, now=T0)

    assert report.orphaned_workspaces == []
    assert report.workspaces_checked == 1


def test_no_workspaces_or_locks_directory_yields_zero_checked_not_a_crash(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    store.add_run(_run("run-1"))

    report = build_operational_health(store, now=T0)

    assert report.locks_checked == 0
    assert report.workspaces_checked == 0
    assert report.degraded is False


@pytest.mark.skipif(fcntl is None, reason="requires fcntl (macOS/Linux)")
def test_stale_lock_is_detected_released_and_left_on_disk(tmp_path: Path) -> None:
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    lock_path = locks_dir / "abandoned-key.lock"
    lock_path.write_text("99999999", encoding="utf-8")

    store = _fake_store(tmp_path)
    report = build_operational_health(store, now=T0)

    assert report.lock_check_supported is True
    assert report.locks_checked == 1
    assert len(report.stale_locks) == 1
    finding = report.stale_locks[0]
    assert isinstance(finding, StaleLockFinding)
    assert finding.lock_name == "abandoned-key.lock"
    assert finding.modified_at is not None
    # never deleted, never rewritten
    assert lock_path.is_file()
    assert lock_path.read_text(encoding="utf-8") == "99999999"

    # a second, independent probe still finds it (proves the first probe's
    # own lock was actually released, not merely reported as released).
    second_report = build_operational_health(store, now=T0)
    assert len(second_report.stale_locks) == 1


@pytest.mark.skipif(fcntl is None, reason="requires fcntl (macOS/Linux)")
def test_actively_held_lock_is_not_reported_as_stale(tmp_path: Path) -> None:
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    lock_path = locks_dir / "active-key.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    # A distinct open-file-description on the same path holds an
    # independent flock from any fd this module opens internally, faithfully
    # simulating a live external holder without needing a second process.
    holder_fd = os.open(str(lock_path), os.O_RDWR)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        store = _fake_store(tmp_path)
        report = build_operational_health(store, now=T0)

        assert report.locks_checked == 1
        assert report.stale_locks == []
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_lock_health_probe_never_acquires_the_workspace_flock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import software_agent_factory.observability as observability_module

    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / "active-key.lock").write_text(str(os.getpid()), encoding="utf-8")

    def forbidden_flock(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("monitoring must not acquire a workspace flock")

    monkeypatch.setattr(observability_module.fcntl, "flock", forbidden_flock)

    report = build_operational_health(_fake_store(tmp_path), now=T0)

    assert report.stale_locks == []
    assert report.degraded is False


def test_non_lock_files_under_locks_dir_are_ignored(tmp_path: Path) -> None:
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / "not-a-lock.txt").write_text("", encoding="utf-8")

    store = _fake_store(tmp_path)
    report = build_operational_health(store, now=T0)

    assert report.locks_checked == 0
    assert report.stale_locks == []


def test_lock_check_unsupported_platform_is_reported_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import software_agent_factory.observability as observability_module

    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / "some-key.lock").write_text(str(os.getpid()), encoding="utf-8")

    monkeypatch.setattr(observability_module, "_FCNTL_AVAILABLE", False)
    store = _fake_store(tmp_path)

    report = build_operational_health(store, now=T0)

    assert report.lock_check_supported is False
    assert report.stale_locks == []
    assert report.locks_checked == 0
    assert report.degraded is True
    assert any("unsupported" in reason for reason in report.degraded_reasons)


def test_operational_health_explicit_data_dir_overrides_store_default(tmp_path: Path) -> None:
    other_root = tmp_path / "elsewhere"
    (other_root / "workspaces" / "an-orphan").mkdir(parents=True)
    store = _fake_store(tmp_path)  # runs_dir under tmp_path, unrelated to other_root

    report = build_operational_health(store, data_dir=other_root, now=T0)

    assert [f.workspace_name for f in report.orphaned_workspaces] == ["an-orphan"]


def test_operational_health_scan_cap_propagates_truncation_and_degraded(
    tmp_path: Path,
) -> None:
    store = _fake_store(tmp_path)
    for i in range(4):
        run_id = f"run-{i}"
        store.add_run(_run(run_id, created_at=T0, updated_at=T0))
        timestamp = (T0 + timedelta(seconds=i)).timestamp()
        os.utime(store.marker_path(run_id), (timestamp, timestamp))

    report = build_operational_health(store, now=T0, max_scanned_runs=2)

    assert report.total_runs == 4
    assert report.scanned_runs == 2
    assert report.scan_truncated is True
    assert report.degraded is True
    assert any("capped at 2 of 4" in reason for reason in report.degraded_reasons)
    assert any("false positive" in reason for reason in report.degraded_reasons)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"stale_after": timedelta(0)}, "stale_after"),
        ({"stale_after": timedelta(seconds=-1)}, "stale_after"),
        ({"max_scanned_runs": 0}, "max_scanned_runs"),
        ({"max_scanned_runs": -1}, "max_scanned_runs"),
    ],
)
def test_operational_health_invalid_arguments_raise_value_error(
    tmp_path: Path, kwargs: dict, match: str
) -> None:
    store = _fake_store(tmp_path)
    with pytest.raises(ValueError, match=match):
        build_operational_health(store, now=T0, **kwargs)


def test_operational_health_never_writes_to_data_dir(tmp_path: Path) -> None:
    """A read-only health check must never create, modify, or delete
    anything under data_dir -- not the runs it scans, not lock files, not
    workspace directories."""
    store = _fake_store(tmp_path)
    store.add_run(
        _run("run-1", workspace_path=str(tmp_path / "workspaces" / "run-1-key"))
    )
    (tmp_path / "workspaces" / "run-1-key").mkdir(parents=True)
    (tmp_path / "workspaces" / "orphan").mkdir(parents=True)
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / "some.lock").write_text("", encoding="utf-8")

    def snapshot_tree() -> set[str]:
        return {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    before = snapshot_tree()
    build_operational_health(store, now=T0)
    after = snapshot_tree()

    assert before == after


def test_operational_health_report_has_no_prohibited_fields() -> None:
    prohibited_substrings = ("prompt", "diff", "token", "stdout", "stderr", "secret")
    all_field_names = (
        list(OperationalHealthReport.model_fields)
        + list(StaleRunFinding.model_fields)
        + list(StaleLockFinding.model_fields)
        + list(OrphanedWorkspaceFinding.model_fields)
    )
    lowered = " ".join(all_field_names).lower()
    for substring in prohibited_substrings:
        assert substring not in lowered


# ---------------------------------------------------------------------------
# Protocol conformance sanity check
# ---------------------------------------------------------------------------


def test_file_run_store_satisfies_run_store_protocol(tmp_path: Path) -> None:
    store: RunStoreProtocol = FileRunStore(tmp_path / "data")
    assert isinstance(store.runs_dir, Path)


def test_fake_run_store_satisfies_run_store_protocol(tmp_path: Path) -> None:
    store: RunStoreProtocol = _fake_store(tmp_path)
    assert isinstance(store.runs_dir, Path)


# ---------------------------------------------------------------------------
# build_run_detail
# ---------------------------------------------------------------------------


def test_build_run_detail_returns_summary_fields_plus_attempts(tmp_path: Path) -> None:
    from software_agent_factory.observability import RunDetail, build_run_detail

    store = _fake_store(tmp_path)
    run = _run(
        "run-detail",
        state=WorkflowState.PR_READY,
        completed_at=T0 + timedelta(minutes=5),
        attempt_records=[_attempt(1), _attempt(2, budget=AttemptBudget.CI_REPAIR)],
    )
    store.add_run(run)
    store.add_artifact("run-detail", WorkItem(id="WI-1", title="Title", description="D"))

    detail = build_run_detail(store, "run-detail", now=T0 + timedelta(minutes=10))

    assert isinstance(detail, RunDetail)
    assert detail.run_id == "run-detail"
    assert detail.title == "Title"
    assert detail.state is WorkflowState.PR_READY
    assert detail.completed_at == T0 + timedelta(minutes=5)
    assert detail.attempt_count == 2
    assert detail.implementation_attempts == 1
    assert detail.ci_repair_attempts == 1
    assert [attempt.attempt_number for attempt in detail.attempts] == [1, 2]
    assert detail.attempts[0].role is AgentRole.IMPLEMENTER


def test_build_run_detail_returns_none_for_missing_or_unreadable_runs(
    tmp_path: Path,
) -> None:
    from software_agent_factory.observability import build_run_detail

    store = _fake_store(tmp_path)
    store.add_broken("run-corrupt", ValueError("bad json"))

    assert build_run_detail(store, "run-absent") is None
    assert build_run_detail(store, "run-corrupt") is None


def test_build_run_detail_rejects_a_hostile_run_id_without_touching_disk(
    tmp_path: Path,
) -> None:
    """The real store validates the id before any path is built, so a
    traversal-shaped id from an HTTP client resolves to ``None``."""
    from software_agent_factory.observability import build_run_detail

    store = FileRunStore(tmp_path / "data")

    assert build_run_detail(store, "../../etc/passwd") is None
    assert not (tmp_path / "data").exists()


def test_build_run_detail_omits_free_text_and_raw_artifacts(tmp_path: Path) -> None:
    """No ``failure_reason``, no attempt ``reasoning``: neither can be vetted
    for repository content, so neither is carried into a detail view."""
    from software_agent_factory.observability import build_run_detail

    store = _fake_store(tmp_path)
    store.add_run(
        _run(
            "run-safe",
            state=WorkflowState.NEEDS_HUMAN,
            completed_at=T0,
            attempt_records=[_attempt(1)],
        )
    )

    payload = build_run_detail(store, "run-safe").model_dump(mode="json")

    assert "failure_reason" not in payload
    assert all("reasoning" not in attempt for attempt in payload["attempts"])
    assert all("failure_reason" not in attempt for attempt in payload["attempts"])


def test_build_run_detail_is_read_only(tmp_path: Path) -> None:
    from software_agent_factory.observability import build_run_detail

    data_dir = tmp_path / "data"
    store = FileRunStore(data_dir)
    store.save_run(_run("run-readonly", state=WorkflowState.DONE, completed_at=T0))
    before = sorted(path.name for path in (data_dir / "runs" / "run-readonly").iterdir())

    assert build_run_detail(store, "run-readonly") is not None

    after = sorted(path.name for path in (data_dir / "runs" / "run-readonly").iterdir())
    assert after == before


# ---------------------------------------------------------------------------
# Durable worktree-administration locks are not stale-lock findings
# ---------------------------------------------------------------------------


def test_prune_administration_locks_are_never_reported_as_stale(tmp_path: Path) -> None:
    """``workspace._prune_lock`` deliberately never unlinks its file, so a
    holder-less ``prune-*.lock`` is the normal healthy state and must not be
    reported after every successful run."""
    from software_agent_factory.observability import PRUNE_LOCK_PREFIX

    data_dir = tmp_path / "data"
    locks_dir = data_dir / "locks"
    locks_dir.mkdir(parents=True)
    (locks_dir / f"{PRUNE_LOCK_PREFIX}8c5b637090.lock").write_text("", encoding="utf-8")
    (locks_dir / "workspace-key.lock").write_text("99999999", encoding="utf-8")

    report = build_operational_health(FileRunStore(data_dir), data_dir=data_dir, now=T0)

    assert report.locks_checked == 1
    assert [finding.lock_name for finding in report.stale_locks] == ["workspace-key.lock"]
