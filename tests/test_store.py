from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_factory.models import (
    ChangeSet,
    FactoryRun,
    RepositoryProfile,
    RepositorySkill,
    SkillGuidance,
    Specification,
    TestReport,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.store import ATTEMPTS_DIRNAME, FileRunStore


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


def test_repository_profile_has_a_registered_run_level_filename(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    profile = RepositoryProfile(
        manifest_fingerprint="0" * 64,
        dependency_fingerprint="1" * 64,
    )

    path = store.save_artifact(run.id, profile)

    assert path.name == "repository-profile.json"
    assert store.load_artifact(run.id, RepositoryProfile) == profile
    assert store.list_attempts(run.id) == []


def test_repository_skill_has_a_registered_run_level_filename(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    skill = RepositorySkill(
        dependency_fingerprint="a" * 64,
        simplify=SkillGuidance(summary="Simplify.", guidance=("Keep behavior.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Use exact versions.",)),
        uncertainties=("No external research in this fixture.",),
    )

    path = store.save_artifact(run.id, skill)

    assert path.name == "repository-skill.json"
    assert store.load_artifact(run.id, RepositorySkill) == skill


def test_listing_runs_ignores_attempt_directories(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    store.save_artifact(run.id, ChangeSet(summary="s", changed_files=[]), attempt=1)

    assert store.list_runs() == [run]


# ---------------------------------------------------------------------------
# run_id safety (PLAN.md Phase 15 core safety foundation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_run_id",
    [
        "",
        ".",
        "..",
        "../escape",
        "..\\escape",
        "a/../../etc/passwd",
        "nested/run",
        "nested\\run",
        "/absolute",
        "run\x00id",
        "run id",
        "a" * 129,
    ],
)
def test_every_public_method_rejects_unsafe_run_ids_before_touching_the_filesystem(
    tmp_path: Path, bad_run_id: str
) -> None:
    store = FileRunStore(tmp_path / "data")
    work_item = WorkItem(id="WI-1", title="t", description="d")

    with pytest.raises(ValueError):
        store.run_dir(bad_run_id)
    with pytest.raises(ValueError):
        store.save_run(_sample_run().model_copy(update={"id": bad_run_id}))
    with pytest.raises(ValueError):
        store.load_run(bad_run_id)
    with pytest.raises(ValueError):
        store.save_artifact(bad_run_id, work_item)
    with pytest.raises(ValueError):
        store.load_artifact(bad_run_id, WorkItem)
    with pytest.raises(ValueError):
        store.save_patch(bad_run_id, "diff\n")
    with pytest.raises(ValueError):
        store.load_patch(bad_run_id)
    with pytest.raises(ValueError):
        store.attempt_dir(bad_run_id, 1)
    with pytest.raises(ValueError):
        store.list_attempts(bad_run_id)

    # Nothing traversal-shaped ever reached the filesystem: not even the
    # top-level runs directory was created (every rejection happened before
    # any filesystem access).
    assert not store.runs_dir.exists()


def test_run_id_accepts_the_full_safe_charset_and_max_length(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run_id = "run-ABC.123_" + ("x" * (128 - len("run-ABC.123_")))
    assert len(run_id) == 128
    run = _sample_run().model_copy(update={"id": run_id})

    store.save_run(run)

    assert store.load_run(run_id) == run


# ---------------------------------------------------------------------------
# Read paths never create filesystem artifacts (PLAN.md Phase 15)
# ---------------------------------------------------------------------------


def test_loading_a_missing_run_leaves_no_filesystem_artifacts(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        store.load_run("does-not-exist")

    assert not store.runs_dir.exists()


def test_loading_a_missing_artifact_leaves_no_filesystem_artifacts(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        store.load_artifact("does-not-exist", WorkItem)

    assert not store.runs_dir.exists()


def test_loading_a_missing_artifact_for_an_existing_run_creates_nothing_new(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    before = {path.name for path in (store.runs_dir / run.id).iterdir()}

    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, WorkItem)

    after = {path.name for path in (store.runs_dir / run.id).iterdir()}
    assert after == before
    assert not (store.runs_dir / run.id / ATTEMPTS_DIRNAME).exists()


def test_loading_a_missing_patch_leaves_no_filesystem_artifacts(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        store.load_patch("does-not-exist")

    assert not store.runs_dir.exists()


def test_loading_a_missing_attempt_snapshot_leaves_no_filesystem_artifacts(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    with pytest.raises(FileNotFoundError):
        store.load_artifact(run.id, WorkItem, attempt=1)
    with pytest.raises(FileNotFoundError):
        store.load_patch(run.id, attempt=1)

    assert not (store.runs_dir / run.id / ATTEMPTS_DIRNAME).exists()


def test_listing_attempts_for_a_missing_run_leaves_no_filesystem_artifacts(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")

    assert store.list_attempts("does-not-exist") == []

    assert not store.runs_dir.exists()


def test_run_dir_is_a_write_path_that_creates_the_directory(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    created = store.run_dir("brand-new-run")

    assert created.is_dir()
    assert created == store.runs_dir / "brand-new-run"


# ---------------------------------------------------------------------------
# Lazy root/run directory creation (constructing a store must not mutate
# disk; only a write path may create the runs root)
# ---------------------------------------------------------------------------


def test_constructing_a_store_creates_no_directories(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    store = FileRunStore(data_dir)

    assert not data_dir.exists()
    assert not store.runs_dir.exists()


def test_listing_runs_against_a_missing_root_returns_empty_without_creating_it(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")

    assert store.list_runs() == []

    assert not store.runs_dir.exists()


def test_a_write_after_construction_creates_the_root_lazily(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = FileRunStore(data_dir)
    assert not store.runs_dir.exists()

    store.save_run(_sample_run())

    assert store.runs_dir.is_dir()
    assert data_dir.is_dir()


# ---------------------------------------------------------------------------
# Filename hardening (PLAN.md Phase 15 core safety foundation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_filename",
    [
        "",
        ".",
        "..",
        "a/b",
        "a\\b",
        "../escape.json",
        "/absolute.json",
        "run\x00id.json",
    ],
)
def test_every_filename_accepting_method_rejects_unsafe_filenames(
    tmp_path: Path, bad_filename: str
) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    work_item = WorkItem(id="WI-1", title="t", description="d")

    with pytest.raises(ValueError):
        store.save_artifact(run.id, work_item, bad_filename)
    with pytest.raises(ValueError):
        store.load_artifact(run.id, WorkItem, bad_filename)
    with pytest.raises(ValueError):
        store.save_patch(run.id, "diff\n", bad_filename)
    with pytest.raises(ValueError):
        store.load_patch(run.id, bad_filename)

    # Nothing traversal-shaped was written anywhere under the run directory
    # (nor, for an absolute/parent-escaping filename, outside it).
    assert {path.name for path in (store.runs_dir / run.id).iterdir()} == {"run.json"}
    assert not (tmp_path / "data" / "escape.json").exists()
    assert not (tmp_path / "escape.json").exists()


def test_double_dot_filename_cannot_escape_into_the_parent_run_directory(
    tmp_path: Path,
) -> None:
    """Regression test: ``Path("..").name == ".."``, so a naive ``candidate.name
    != filename`` check alone does not reject ``".."`` -- it must be rejected
    explicitly."""
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    with pytest.raises(ValueError):
        store.save_patch(run.id, "hostile\n", "..")

    # The run's parent (the runs directory itself) must not have been
    # written to.
    assert {path.name for path in store.runs_dir.iterdir()} == {run.id}


# ---------------------------------------------------------------------------
# Invalid attempt numbers never partially mutate storage
# ---------------------------------------------------------------------------


def test_invalid_attempt_does_not_partially_write_the_top_level_artifact_snapshot(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    with pytest.raises(ValueError, match="attempt must be 1 or greater"):
        store.save_artifact(run.id, ChangeSet(summary="bad", changed_files=[]), attempt=0)

    # No top-level change-set.json was written by the failed call.
    assert {path.name for path in (store.runs_dir / run.id).iterdir()} == {"run.json"}


def test_invalid_attempt_does_not_partially_write_the_top_level_patch(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)

    with pytest.raises(ValueError, match="attempt must be 1 or greater"):
        store.save_patch(run.id, "diff\n", attempt=0)

    assert {path.name for path in (store.runs_dir / run.id).iterdir()} == {"run.json"}


def test_invalid_attempt_does_not_create_a_run_directory_for_a_brand_new_run(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")

    with pytest.raises(ValueError, match="attempt must be 1 or greater"):
        store.attempt_dir("brand-new-run", 0)

    assert not store.runs_dir.exists()
