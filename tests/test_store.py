from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_factory.models import (
    ChangeSet,
    FactoryRun,
    Specification,
    TestReport,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.store import FileRunStore


def _sample_run(state: WorkflowState = WorkflowState.CREATED) -> FactoryRun:
    timestamp = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    return FactoryRun(
        id="RUN-123",
        work_item_id="WI-123",
        state=state,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_file_run_store_round_trips_run_artifact_and_patch(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    work_item = WorkItem(
        id="WI-123",
        title="Add validation",
        description="Validate empty names.",
    )
    specification = Specification(
        problem="Names should not be empty.",
        acceptance_criteria=["Reject empty names"],
        constraints=[],
        assumptions=[],
        unknowns=[],
        dependencies=[],
        risk_flags=[],
        confidence=0.8,
    )

    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, specification)
    patch_path = store.save_patch(run.id, "diff --git a/a.py b/a.py\n")

    loaded_run = store.load_run(run.id)
    loaded_work_item = store.load_artifact(run.id, WorkItem)
    loaded_specification = store.load_artifact(run.id, Specification)
    listed_runs = store.list_runs()

    assert loaded_run == run
    assert loaded_work_item == work_item
    assert loaded_specification == specification
    assert listed_runs == [run]
    assert patch_path.read_text(encoding="utf-8") == "diff --git a/a.py b/a.py\n"


def test_file_run_store_preserves_existing_run_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileRunStore(tmp_path / "data")
    original_run = _sample_run(WorkflowState.CREATED)
    updated_run = original_run.model_copy(update={"state": WorkflowState.TRIAGING})
    store.save_run(original_run)

    original_replace = os.replace

    def failing_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        if Path(dst).name == "run.json":
            raise OSError("simulated replace failure")
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store.save_run(updated_run)

    assert store.load_run(original_run.id).state is WorkflowState.CREATED


def test_file_run_store_rejects_unknown_factory_run_schema(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run_dir = store.runs_dir / "RUN-123"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"schema_version": 99, "id": "RUN-123", "work_item_id": "WI-123", "state": "CREATED"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported FactoryRun schema_version: 99"):
        store.load_run("RUN-123")


def test_attempt_snapshots_are_written_alongside_latest_snapshot(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    first = ChangeSet(summary="first attempt", changed_files=["a.py"])
    second = ChangeSet(summary="second attempt", changed_files=["a.py", "b.py"])

    latest_path = store.save_artifact(run.id, first, attempt=1)
    store.save_patch(run.id, "diff --git a/a.py b/a.py\n", attempt=1)
    store.save_artifact(run.id, second, attempt=2)
    store.save_patch(run.id, "diff --git a/b.py b/b.py\n", attempt=2)

    assert latest_path == store.runs_dir / run.id / "change-set.json"
    # Top-level snapshot always holds the latest values.
    assert store.load_artifact(run.id, ChangeSet) == second
    assert store.load_patch(run.id) == "diff --git a/b.py b/b.py\n"
    # Per-attempt history is preserved.
    assert store.load_artifact(run.id, ChangeSet, attempt=1) == first
    assert store.load_artifact(run.id, ChangeSet, attempt=2) == second
    assert store.load_patch(run.id, attempt=1) == "diff --git a/a.py b/a.py\n"
    assert store.list_attempts(run.id) == [1, 2]
    assert (store.runs_dir / run.id / "attempts" / "01" / "change-set.json").exists()


def test_saving_without_attempt_keeps_phase_1_layout(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    store.save_artifact(run.id, ChangeSet(summary="only", changed_files=[]))
    store.save_patch(run.id, "diff\n")

    assert store.list_attempts(run.id) == []
    assert not (store.runs_dir / run.id / "attempts").exists()
    assert {path.name for path in (store.runs_dir / run.id).iterdir()} == {
        "run.json",
        "change-set.json",
        "patch.diff",
    }


def test_attempt_snapshots_reject_invalid_attempt_numbers(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    with pytest.raises(ValueError, match="attempt must be 1 or greater"):
        store.save_artifact(run.id, ChangeSet(summary="bad", changed_files=[]), attempt=0)


def test_test_report_has_a_registered_default_filename(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    report = TestReport(passed=True, findings=[], suggested_tests=[], confidence=0.9)

    path = store.save_artifact(run.id, report, attempt=1)

    assert path.name == "test-report.json"
    assert store.load_artifact(run.id, TestReport, attempt=1) == report


def test_listing_runs_ignores_attempt_directories(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    store.save_artifact(run.id, ChangeSet(summary="s", changed_files=[]), attempt=1)

    assert store.list_runs() == [run]
