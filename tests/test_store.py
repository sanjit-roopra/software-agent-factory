from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from software_agent_factory.models import (
    ChangeSet,
    DashboardRequestStaleReason,
    DashboardResumeRequest,
    FactoryRun,
    PlanDecisionAnswer,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillOverlay,
    RepositorySkillUse,
    ResumeClassification,
    SkillGuidance,
    SkillOverlayMode,
    SkillSelectionSource,
    Specification,
    TestReport,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.store import (
    ATTEMPTS_DIRNAME,
    FileRunStore,
    ImmutableArtifactConflictError,
)


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


def test_file_run_store_rejects_missing_factory_run_schema(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run_dir = store.runs_dir / "RUN-123"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"id": "RUN-123", "work_item_id": "WI-123", "state": "CREATED"}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported FactoryRun schema_version: None"):
        store.load_run("RUN-123")


def test_file_run_store_rejects_corrupt_json_with_json_decode_error(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run_dir = store.runs_dir / "RUN-CORRUPT"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        '{"schema_version": 1, "id": "RUN-CORRUPT", corrupt...',
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        store.load_run("RUN-CORRUPT")


def _store_with_one_good_and_two_unreadable_runs(tmp_path: Path) -> tuple[FileRunStore, str]:
    store = FileRunStore(tmp_path / "data")
    good = _sample_run()
    store.save_run(good)
    legacy = json.loads(store.load_run(good.id).model_dump_json())
    legacy["id"] = "RUN-LEGACY"
    legacy["review_ledger"] = {
        "open_findings": [
            {
                "id": "F1",
                "category": "CORRECTNESS",
                "message": "  ",
                "locations": [{"path": "a.py", "start_line": 1, "end_line": 1}],
                "origin": "INITIAL",
                "first_seen_snapshot": 1,
            }
        ]
    }
    legacy_dir = store.runs_dir / "RUN-LEGACY"
    legacy_dir.mkdir()
    (legacy_dir / "run.json").write_text(json.dumps(legacy), encoding="utf-8")
    corrupt_dir = store.runs_dir / "RUN-CORRUPT"
    corrupt_dir.mkdir()
    (corrupt_dir / "run.json").write_text("{corrupt", encoding="utf-8")
    return store, good.id


def test_list_runs_is_strict_by_default(tmp_path: Path) -> None:
    store, _good_id = _store_with_one_good_and_two_unreadable_runs(tmp_path)

    with pytest.raises(ValueError):  # noqa: PT011 - both a bad value and bad JSON count
        store.list_runs()


def test_list_runs_can_skip_and_log_runs_that_fail_validation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store, good_id = _store_with_one_good_and_two_unreadable_runs(tmp_path)

    with caplog.at_level(logging.WARNING, logger="software_agent_factory.store"):
        runs = store.list_runs(skip_invalid=True)

    assert [run.id for run in runs] == [good_id]
    assert "skipped run RUN-LEGACY" in caplog.text
    assert "text must not be blank" in caplog.text
    assert "skipped run RUN-CORRUPT" in caplog.text


def test_file_run_store_save_artifact_reuses_single_serialization_for_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    artifact = ChangeSet(summary="Optimized artifact", changed_files=["src/main.py"])

    model_text_calls = 0
    original_model_text = store._model_text

    def counting_model_text(model: object) -> str:
        nonlocal model_text_calls
        model_text_calls += 1
        return original_model_text(model)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_model_text", counting_model_text)

    dest = store.save_artifact(run.id, artifact, attempt=1)
    attempt_path = store.attempt_dir(run.id, 1) / "change-set.json"

    # Must be serialized exactly once despite writing both destination and attempt snapshot
    assert model_text_calls == 1
    assert dest.exists()
    assert attempt_path.exists()
    assert dest != attempt_path
    # Both independent files must have identical content
    assert dest.read_text(encoding="utf-8") == attempt_path.read_text(encoding="utf-8")
    assert store.load_artifact(run.id, ChangeSet, attempt=1) == artifact


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


def _skill_use(**overrides: object) -> RepositorySkillUse:
    payload: dict[str, object] = {
        "repository_key": "demo-0123456789abcdef",
        "dependency_fingerprint": "a" * 64,
        "selected_at": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        "source": SkillSelectionSource.GENERATED,
        "generated_skill_hash": "b" * 64,
        "effective_skill_hash": "b" * 64,
    }
    payload.update(overrides)
    return RepositorySkillUse.model_validate(payload)


def test_repository_skill_audit_artifacts_have_registered_filenames(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    use = _skill_use()
    overlay = RepositorySkillOverlay(
        mode=SkillOverlayMode.EXTEND,
        simplify=SkillGuidance(summary="House style.", guidance=("Prefer stdlib.",)),
    )

    use_path = store.save_artifact_once(run.id, use)
    overlay_path = store.save_artifact_once(run.id, overlay)

    assert use_path.name == "repository-skill-use.json"
    assert overlay_path.name == "repository-skill-overlay.json"
    assert store.load_artifact(run.id, RepositorySkillUse) == use
    assert store.load_artifact(run.id, RepositorySkillOverlay) == overlay


def test_create_once_artifacts_are_idempotent_but_immutable(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run)
    use = _skill_use()

    first = store.save_artifact_once(run.id, use)
    again = store.save_artifact_once(run.id, use)

    assert first == again
    assert store.load_artifact(run.id, RepositorySkillUse) == use

    with pytest.raises(ImmutableArtifactConflictError, match="create-once"):
        store.save_artifact_once(run.id, _skill_use(source=SkillSelectionSource.REUSED))

    assert store.load_artifact(run.id, RepositorySkillUse) == use
    assert not [path for path in (store.runs_dir / run.id).iterdir() if path.suffix == ".tmp"]


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def _request(**overrides: object) -> DashboardResumeRequest:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "episode_id": "episode-1",
        "context_fingerprint": FINGERPRINT_A,
        "action": ResumeClassification.RISK_APPROVAL,
    }
    return DashboardResumeRequest.model_validate({**fields, **overrides})


def _store_with_run(tmp_path: Path) -> FileRunStore:
    store = FileRunStore(tmp_path / "data")
    run = _sample_run()
    store.save_run(run.model_copy(update={"id": "run-1"}))
    return store


def _request_files(store: FileRunStore) -> list[Path]:
    return sorted((store.runs_dir / "run-1").glob("dashboard-approval-*"))


def test_dashboard_request_is_created_once_per_episode_and_fingerprint(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)

    assert store.create_dashboard_request("run-1", _request()) is True
    [path] = _request_files(store)
    assert path.name == f"dashboard-approval-episode-1-{FINGERPRINT_A[:16]}.json"
    before = path.read_text(encoding="utf-8")

    second = _request(created_at=datetime(2030, 1, 1, tzinfo=UTC))
    assert store.create_dashboard_request("run-1", second) is False

    assert _request_files(store) == [path]
    assert path.read_text(encoding="utf-8") == before


def test_dashboard_request_with_new_fingerprint_gets_its_own_file(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)

    assert store.create_dashboard_request("run-1", _request()) is True
    assert store.create_dashboard_request("run-1", _request(context_fingerprint=FINGERPRINT_B))

    assert len(_request_files(store)) == 2


def test_concurrent_dashboard_requests_create_exactly_one_file(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[bool] = []

    def create() -> None:
        barrier.wait()
        results.append(store.create_dashboard_request("run-1", _request()))

    threads = [threading.Thread(target=create) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False] * (workers - 1) + [True]
    assert len(_request_files(store)) == 1
    assert not list((store.runs_dir / "run-1").glob("*.tmp"))
    assert not list((store.runs_dir / "run-1").glob(".*.tmp"))


def test_dashboard_request_for_a_missing_run_raises_and_creates_nothing(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "data")

    with pytest.raises(FileNotFoundError):
        store.create_dashboard_request("run-1", _request())

    assert not (store.runs_dir / "run-1").exists()


def test_dashboard_request_for_another_run_is_rejected(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)

    with pytest.raises(ValueError, match="not run-1"):
        store.create_dashboard_request("run-1", _request(run_id="run-2"))

    assert _request_files(store) == []


def test_dashboard_request_round_trips(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    request = _request(
        action=ResumeClassification.PLAN_DECISION,
        answers=[PlanDecisionAnswer(decision_number=1, answer="Use SQLite.")],
    )
    store.create_dashboard_request("run-1", request)

    assert store.load_dashboard_request("run-1", "episode-1", FINGERPRINT_A) == request


def test_load_of_an_absent_dashboard_request_is_none(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    store.create_dashboard_request("run-1", _request())

    assert store.load_dashboard_request("run-1", "episode-1", FINGERPRINT_B) is None
    assert store.load_dashboard_request("run-1", "other-episode", FINGERPRINT_A) is None
    assert (
        FileRunStore(tmp_path / "empty").load_dashboard_request("run-1", "episode-1", FINGERPRINT_A)
        is None
    )
    assert not (tmp_path / "empty").exists()


def test_dashboard_requests_of_one_episode_are_listed_oldest_first(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    older = _request(created_at="2026-10-01T10:00:00Z")
    newer = _request(context_fingerprint=FINGERPRINT_B, created_at="2026-10-01T11:00:00Z")
    store.create_dashboard_request("run-1", newer)
    store.create_dashboard_request("run-1", older)
    store.create_dashboard_request("run-1", _request(episode_id="episode-2"))

    assert store.list_dashboard_requests("run-1", "episode-1") == [older, newer]
    assert store.list_dashboard_requests("run-1", "episode-3") == []


def test_listing_dashboard_requests_skips_a_damaged_file(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    good = _request(context_fingerprint=FINGERPRINT_B)
    store.create_dashboard_request("run-1", good)
    damaged = store.runs_dir / "run-1" / f"dashboard-approval-episode-1-{FINGERPRINT_A[:16]}.json"
    damaged.write_text("{not json", encoding="utf-8")

    assert store.list_dashboard_requests("run-1", "episode-1") == [good]


def test_listing_dashboard_requests_skips_a_file_that_does_not_match_its_name(
    tmp_path: Path,
) -> None:
    store = _store_with_run(tmp_path)
    good = _request(context_fingerprint=FINGERPRINT_B)
    store.create_dashboard_request("run-1", good)
    run_dir = store.runs_dir / "run-1"
    mislabeled = run_dir / f"dashboard-approval-episode-1-{FINGERPRINT_A[:16]}.json"
    mislabeled.write_text(good.model_dump_json(), encoding="utf-8")
    other_episode = _request(episode_id="episode-1-x")
    store.create_dashboard_request("run-1", other_episode)

    assert store.list_dashboard_requests("run-1", "episode-1") == [good]


def test_listing_dashboard_requests_of_a_missing_run_is_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "empty")

    assert store.list_dashboard_requests("run-1", "episode-1") == []
    assert not (tmp_path / "empty").exists()


def test_listing_dashboard_requests_rejects_an_unsafe_episode_id(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)

    with pytest.raises(ValueError, match="episode id"):
        store.list_dashboard_requests("run-1", "../x")


@pytest.mark.parametrize(
    "update",
    [{"status": "stale", "reason": "expired"}, {"reason": "expired"}],
    ids=["stale", "pending-with-a-reason"],
)
def test_a_dashboard_request_is_created_pending_and_without_a_reason(
    tmp_path: Path, update: dict[str, object]
) -> None:
    store = _store_with_run(tmp_path)
    # model_copy skips validation, as a caller that bypassed the model would.
    request = _request().model_copy(update=update)

    with pytest.raises(ValueError, match="pending"):
        store.create_dashboard_request("run-1", request)

    assert _request_files(store) == []


def test_replacing_a_dashboard_request_overwrites_it_in_place(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)
    request = _request()
    store.create_dashboard_request("run-1", request)
    stale = request.model_copy(update={"status": "stale", "reason": "expired"})

    store.replace_dashboard_request("run-1", stale)

    assert store.load_dashboard_request("run-1", "episode-1", FINGERPRINT_A) == stale
    assert len(_request_files(store)) == 1
    assert store.create_dashboard_request("run-1", request) is False


def test_replacing_a_dashboard_request_that_does_not_exist_raises(tmp_path: Path) -> None:
    store = _store_with_run(tmp_path)

    with pytest.raises(FileNotFoundError, match="dashboard-approval-episode-1"):
        store.replace_dashboard_request("run-1", _request())

    assert _request_files(store) == []


@pytest.mark.parametrize("episode", ["", "a/b", "..\\x", "x" * 129, "a b"])
def test_dashboard_request_lookup_rejects_unsafe_episode_ids(tmp_path: Path, episode: str) -> None:
    store = _store_with_run(tmp_path)

    with pytest.raises(ValueError, match="episode id"):
        store.load_dashboard_request("run-1", episode, FINGERPRINT_A)


@pytest.mark.parametrize("fingerprint", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 65])
def test_dashboard_request_lookup_rejects_bad_fingerprints(
    tmp_path: Path, fingerprint: str
) -> None:
    store = _store_with_run(tmp_path)

    with pytest.raises(ValueError, match="fingerprint"):
        store.load_dashboard_request("run-1", "episode-1", fingerprint)


_PLAN_ACTION = {"action": ResumeClassification.PLAN_DECISION}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"episode_id": ""}, id="empty-episode"),
        pytest.param({"episode_id": "a/b"}, id="episode-with-slash"),
        pytest.param({"episode_id": "x" * 129}, id="episode-too-long"),
        pytest.param({"context_fingerprint": "a" * 63}, id="fingerprint-too-short"),
        pytest.param({"context_fingerprint": "A" * 64}, id="fingerprint-uppercase"),
        pytest.param({"action": ResumeClassification.NOT_RESUMABLE}, id="action-not-resumable"),
        pytest.param({"action": "DELETE_RUN"}, id="action-unknown"),
        pytest.param({"answers": [{"decision_number": 1, "answer": "x"}]}, id="risk-with-answers"),
        pytest.param(_PLAN_ACTION, id="plan-without-answers"),
        pytest.param(
            {**_PLAN_ACTION, "answers": [{"decision_number": 2, "answer": "x"}]},
            id="answers-start-at-two",
        ),
        pytest.param(
            {**_PLAN_ACTION, "answers": [{"decision_number": 1, "answer": "two\nlines"}]},
            id="answer-with-two-lines",
        ),
        pytest.param(
            {**_PLAN_ACTION, "answers": [{"decision_number": 1, "answer": "x" * 501}]},
            id="answer-too-long",
        ),
        pytest.param(
            {
                **_PLAN_ACTION,
                "answers": [{"decision_number": n, "answer": "x"} for n in range(1, 26)],
            },
            id="too-many-answers",
        ),
        pytest.param({"status": "stale"}, id="stale-without-reason"),
        pytest.param({"reason": "expired"}, id="pending-with-reason"),
        pytest.param({"status": "stale", "reason": "because"}, id="unknown-reason"),
        pytest.param({"status": "done"}, id="unknown-status"),
    ],
)
def test_dashboard_request_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _request(**overrides)


@pytest.mark.parametrize("reason", get_args(DashboardRequestStaleReason))
def test_dashboard_request_accepts_a_stale_state_with_each_reason(reason: str) -> None:
    assert _request(status="stale", reason=reason).reason == reason


def test_dashboard_request_accepts_the_string_form_of_an_action() -> None:
    assert _request(action="RISK_APPROVAL").action is ResumeClassification.RISK_APPROVAL
    assert (
        _request(action="PLAN_DECISION", answers=[{"decision_number": 1, "answer": "x"}]).action
        is ResumeClassification.PLAN_DECISION
    )
