"""Tests for repository-scoped skill persistence, overlays and merging."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_factory.models import (
    GENERIC_SKILL_TARGET,
    DependencyEcosystem,
    RepositoryDependency,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillOverlay,
    SkillGuidance,
    SkillOverlayMode,
    SkillSelectionSource,
    SkillSource,
    SkillTarget,
)
from software_agent_factory.repository_skills import (
    MAX_OVERLAY_BYTES,
    OVERLAY_FILENAME,
    RepositoryIdentity,
    RepositorySkillError,
    RepositorySkillManager,
    RepositorySkillMergeError,
    RepositorySkillOverlayError,
    RepositorySkillStorageError,
    content_hash,
    merge_repository_skill,
    repository_key,
    repository_skill_validation_error,
    resolve_repository_identity,
    validate_dependency_fingerprint,
    validate_repository_key,
)

FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
GENERATED_AT = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _guidance(
    summary: str,
    guidance: tuple[str, ...] = ("Prefer the simplest supported form.",),
    avoid: tuple[str, ...] = (),
    validation: tuple[str, ...] = (),
) -> SkillGuidance:
    return SkillGuidance(summary=summary, guidance=guidance, avoid=avoid, validation=validation)


def _skill(
    dependency_fingerprint: str = FINGERPRINT_A,
    *,
    simplify: SkillGuidance | None = None,
    polish: SkillGuidance | None = None,
    generated_at: datetime = GENERATED_AT,
) -> RepositorySkill:
    return RepositorySkill(
        dependency_fingerprint=dependency_fingerprint,
        generated_at=generated_at,
        targets=(
            SkillTarget(
                ecosystem="npm",
                name="react",
                declared_version="^19.1.0",
                resolved_version="19.1.0",
                evidence=("package.json",),
            ),
        ),
        official_sources=(
            SkillSource(
                title="React documentation",
                url="https://react.dev/reference/react",
                version_scope="19.1.0",
                applies_to=("react",),
            ),
        ),
        simplify=simplify or _guidance("Generated simplify."),
        polish=polish or _guidance("Generated polish."),
        uncertainties=("Server component guidance was not verified.",),
    )


def _identity(name: str = "demo") -> RepositoryIdentity:
    common_dir = Path(f"/checkouts/{name}/.git")
    return RepositoryIdentity(key=repository_key(common_dir), git_common_dir=common_dir)


def _manager(data_dir: Path, name: str = "demo") -> RepositorySkillManager:
    return RepositorySkillManager(data_dir, _identity(name))


def _write_overlay(manager: RepositorySkillManager, text: str) -> Path:
    path = manager.overlay_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- identity and path safety ------------------------------------------------


def test_identity_is_shared_by_the_checkout_subdirectories_and_linked_worktrees(
    factory_source_repo: Path, tmp_path: Path
) -> None:
    nested = factory_source_repo / "src" / "pkg"
    nested.mkdir(parents=True)
    worktree = tmp_path / "linked-worktree"
    subprocess.run(
        ["git", "-C", str(factory_source_repo), "worktree", "add", str(worktree), "-b", "topic"],
        capture_output=True,
        text=True,
        check=True,
    )

    main_identity = resolve_repository_identity(factory_source_repo)
    nested_identity = resolve_repository_identity(nested)
    worktree_identity = resolve_repository_identity(worktree)

    assert main_identity == nested_identity == worktree_identity
    assert main_identity.git_common_dir == (factory_source_repo / ".git").resolve()
    assert validate_repository_key(main_identity.key) == main_identity.key
    assert main_identity.key == repository_key(main_identity.git_common_dir)


def test_resolve_repository_identity_rejects_non_repositories(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(RepositorySkillError, match="not inside a Git repository"):
        resolve_repository_identity(plain)

    with pytest.raises(RepositorySkillError, match="not a directory"):
        resolve_repository_identity(tmp_path / "missing")


def test_repository_key_is_a_safe_hashed_path_component() -> None:
    first = repository_key(Path("/one/service/.git"))
    second = repository_key(Path("/two/service/.git"))
    awkward = repository_key(Path("/somewhere/../weird name!/.git"))

    assert first != second
    assert first.startswith("service-")
    assert "/" not in awkward and " " not in awkward
    for key in (first, second, awkward):
        assert validate_repository_key(key) == key

    with pytest.raises(RepositorySkillError):
        validate_repository_key("../escape")
    with pytest.raises(RepositorySkillError):
        validate_repository_key("")


def test_generated_paths_reject_unsafe_fingerprints(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")

    for unsafe in ("../../etc/passwd", "A" * 64, "", "a" * 63, "a" * 64 + "/x"):
        with pytest.raises(RepositorySkillError):
            manager.generated_path(unsafe)

    assert validate_dependency_fingerprint(FINGERPRINT_A) == FINGERPRINT_A
    path = manager.generated_path(FINGERPRINT_A)
    assert path.parent == manager.generated_dir
    assert path.name == f"{FINGERPRINT_A}.json"


def test_reads_never_create_anything(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manager = _manager(data_dir)

    assert manager.load_generated(FINGERPRINT_A) is None
    assert manager.list_generated_fingerprints() == ()
    assert manager.reuse(FINGERPRINT_A) is None
    overlay_read = manager.read_overlay()
    assert manager.load_metadata() is None

    assert overlay_read.present is False
    assert overlay_read.overlay is None
    assert overlay_read.require() is None
    assert not data_dir.exists()


def test_selection_never_writes_into_the_repository(
    factory_source_repo: Path, tmp_path: Path
) -> None:
    manager = RepositorySkillManager.for_repository(tmp_path / "data", factory_source_repo)
    before = sorted(path.name for path in factory_source_repo.iterdir())

    manager.select(_skill())

    status = subprocess.run(
        ["git", "-C", str(factory_source_repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout == ""
    assert sorted(path.name for path in factory_source_repo.iterdir()) == before
    assert manager.load_generated(FINGERPRINT_A) is not None


# -- generated skill storage -------------------------------------------------


def test_create_generated_is_no_clobber_and_records_metadata(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    first = manager.create_generated(_skill())
    replacement = _skill(simplify=_guidance("Rewritten simplify."))

    second = manager.create_generated(replacement)

    assert first.created is True
    assert second.created is False
    assert second.skill == first.skill
    assert second.skill.simplify.summary == "Generated simplify."
    assert manager.load_generated(FINGERPRINT_A) == first.skill
    assert manager.list_generated_fingerprints() == (FINGERPRINT_A,)

    metadata = manager.load_metadata()
    assert metadata is not None
    assert metadata.repository_key == manager.repository_key
    assert metadata.git_common_dir == str(manager.identity.git_common_dir)


def test_concurrent_creation_selects_one_complete_winner(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    candidates = [_skill(simplify=_guidance(f"Simplify {index}.")) for index in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(manager.create_generated, candidates))

    stored = manager.load_generated(FINGERPRINT_A)
    assert stored is not None
    assert sum(record.created for record in records) == 1
    assert all(record.skill == stored for record in records)


def test_refresh_replaces_only_generated_guidance(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    manager.create_generated(_skill())
    manager.create_generated(_skill(FINGERPRINT_B))
    overlay_text = (
        "mode: extend\nsimplify:\n  summary: Keep helpers local.\n"
        "  guidance:\n    - Inline single-use helpers.\n"
    )
    overlay_path = _write_overlay(manager, overlay_text)
    refreshed_at = datetime(2026, 9, 6, 8, 30, tzinfo=UTC)

    record = manager.refresh_generated(
        _skill(simplify=_guidance("Refreshed simplify."), generated_at=refreshed_at)
    )

    stored = manager.load_generated(FINGERPRINT_A)
    assert record.created is False
    assert stored is not None
    assert stored.simplify.summary == "Refreshed simplify."
    assert stored.generated_at == refreshed_at
    other = manager.load_generated(FINGERPRINT_B)
    assert other is not None and other.simplify.summary == "Generated simplify."
    assert overlay_path.read_text(encoding="utf-8") == overlay_text


def test_refresh_creates_when_absent(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")

    record = manager.refresh_generated(_skill())

    assert record.created is True
    assert manager.load_generated(FINGERPRINT_A) == record.skill


def test_generated_reads_reject_symlinks_directories_oversize_and_malformed_json(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path / "data")
    manager.create_generated(_skill(FINGERPRINT_B))
    path = manager.generated_path(FINGERPRINT_A)

    path.symlink_to(manager.generated_path(FINGERPRINT_B))
    with pytest.raises(RepositorySkillStorageError, match="symlink"):
        manager.load_generated(FINGERPRINT_A)
    path.unlink()

    path.mkdir()
    with pytest.raises(RepositorySkillStorageError, match="directory"):
        manager.load_generated(FINGERPRINT_A)
    path.rmdir()

    path.write_text("x" * 300_000, encoding="utf-8")
    with pytest.raises(RepositorySkillStorageError, match="byte limit"):
        manager.load_generated(FINGERPRINT_A)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RepositorySkillStorageError, match="invalid"):
        manager.load_generated(FINGERPRINT_A)

    path.write_text(_skill(FINGERPRINT_B).model_dump_json(), encoding="utf-8")
    with pytest.raises(RepositorySkillStorageError, match="claims dependency fingerprint"):
        manager.load_generated(FINGERPRINT_A)


def test_symlinked_storage_components_are_refused(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    manager = _manager(data_dir)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    manager.root_dir.mkdir(parents=True)
    manager.repository_dir.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(RepositorySkillStorageError, match="symlink"):
        manager.load_generated(FINGERPRINT_A)
    with pytest.raises(RepositorySkillStorageError, match="symlink"):
        manager.create_generated(_skill())
    assert manager.read_overlay().error is not None


# -- overlay reading ---------------------------------------------------------


def test_overlay_is_parsed_and_never_rewritten(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    text = (
        "# hand written\n"
        "mode: replace\n"
        "polish:\n"
        "  summary: House polish rules.\n"
        "  guidance:\n"
        "    - Name tests after behaviour.\n"
        "  avoid:\n"
        "    - Do not add abstractions for one caller.\n"
    )
    path = _write_overlay(manager, text)

    read = manager.read_overlay()
    overlay = read.require()

    assert read.present is True
    assert read.raw_text == text
    assert overlay is not None
    assert overlay.mode is SkillOverlayMode.REPLACE
    assert overlay.simplify is None
    assert overlay.polish is not None
    assert overlay.polish.summary == "House polish rules."
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("mode: extend\ntargets:\n  - name: react\n", "targets"),
        ("mode: extend\ndependency_fingerprint: " + "a" * 64 + "\n", "dependency_fingerprint"),
        ("mode: extend\nofficial_sources: []\n", "official_sources"),
        ("mode: extend\ngenerator_version: 2\n", "generator_version"),
        ("mode: extend\ngenerated_at: 2026-01-01T00:00:00Z\n", "generated_at"),
        ("mode: extend\nschema_version: 2\nsimplify:\n  summary: x\n", "schema_version"),
        ("mode: merge\nsimplify:\n  summary: x\n  guidance: [y]\n", "mode"),
        ("simplify:\n  summary: x\n  guidance: [y]\n  sources: []\n", "sources"),
        ("mode: extend\n", "simplify"),
    ],
)
def test_overlay_schema_rejects_machine_owned_and_unknown_fields(
    tmp_path: Path, document: str, expected: str
) -> None:
    manager = _manager(tmp_path / "data")
    _write_overlay(manager, document)

    read = manager.read_overlay()

    assert read.overlay is None
    assert read.valid is False
    assert read.error is not None
    assert expected in str(read.error)
    with pytest.raises(RepositorySkillOverlayError):
        read.require()


def test_overlay_rejects_malformed_yaml_non_mappings_and_bad_bytes(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")

    broken = "mode: [extend\n"
    _write_overlay(manager, broken)
    read = manager.read_overlay()
    assert read.error is not None and "not valid YAML" in str(read.error)
    assert read.raw_text == broken
    assert manager.overlay_path.read_text(encoding="utf-8") == broken

    _write_overlay(manager, "- extend\n")
    assert "must be a YAML mapping" in str(manager.read_overlay().error)

    _write_overlay(manager, "# only a comment\n")
    assert "empty document" in str(manager.read_overlay().error)

    manager.overlay_path.write_bytes(b"mode: \xff\xfe\n")
    assert "not valid UTF-8" in str(manager.read_overlay().error)


def test_overlay_rejects_symlinks_directories_and_oversized_files(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    target = tmp_path / "external-overlay.yaml"
    target.write_text("mode: extend\nsimplify:\n  summary: x\n  guidance: [y]\n", encoding="utf-8")
    manager.repository_dir.mkdir(parents=True)

    manager.overlay_path.symlink_to(target)
    assert "symlink" in str(manager.read_overlay().error)
    manager.overlay_path.unlink()

    manager.overlay_path.mkdir()
    assert "directory" in str(manager.read_overlay().error)
    manager.overlay_path.rmdir()

    _write_overlay(manager, "# " + "x" * MAX_OVERLAY_BYTES + "\n")
    assert "byte limit" in str(manager.read_overlay().error)


def test_invalid_overlay_degrades_to_generated_guidance_with_a_warning(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    document = "mode: extend\ntargets: []\n"
    _write_overlay(manager, document)
    skill = _skill()

    selection = manager.select(skill)

    assert selection.effective_skill == skill
    assert selection.overlay is None
    assert selection.overlay_applied is False
    assert selection.use.overlay_applied is False
    assert selection.use.overlay_hash is None
    assert selection.use.overlay_mode is None
    assert selection.use.effective_skill_hash == selection.use.generated_skill_hash
    assert isinstance(selection.overlay_error, RepositorySkillOverlayError)
    assert selection.overlay_error.path == manager.overlay_path
    assert selection.overlay_error.problems
    assert len(selection.warnings) == 1
    warning = selection.warnings[0]
    assert "using generated guidance only" in warning
    assert "targets" in warning
    assert "only reads this file" in warning
    assert manager.overlay_path.read_text(encoding="utf-8") == document

    with pytest.raises(RepositorySkillOverlayError):
        manager.read_overlay().require()


def test_unsafe_or_oversized_overlays_degrade_without_touching_the_bytes(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    target = tmp_path / "external-overlay.yaml"
    target.write_text("mode: extend\nsimplify:\n  summary: x\n  guidance: [y]\n", encoding="utf-8")
    manager.repository_dir.mkdir(parents=True)
    manager.overlay_path.symlink_to(target)
    skill = _skill()

    selection = manager.select(skill)

    assert selection.effective_skill == skill
    assert selection.overlay is None
    assert "symlink" in selection.warnings[0]
    assert manager.overlay_path.is_symlink()
    assert manager.overlay_path.readlink() == target

    manager.overlay_path.unlink()
    oversized = "# " + "x" * MAX_OVERLAY_BYTES + "\n"
    _write_overlay(manager, oversized)

    degraded = manager.select(skill)

    assert degraded.effective_skill == skill
    assert "byte limit" in degraded.warnings[0]
    assert manager.overlay_path.read_text(encoding="utf-8") == oversized


def test_unmergeable_overlay_degrades_but_is_recorded_as_present(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    document = "mode: extend\nsimplify:\n  summary: " + "H" * 290 + "\n  guidance:\n    - Extra.\n"
    _write_overlay(manager, document)
    skill = _skill()

    selection = manager.select(skill)

    assert selection.effective_skill == skill
    assert selection.overlay is not None
    assert selection.overlay_applied is False
    assert isinstance(selection.overlay_error, RepositorySkillMergeError)
    assert "simplify.summary" in selection.warnings[0]
    assert "using generated guidance only" in selection.warnings[0]
    assert selection.use.overlay_hash == content_hash(selection.overlay)
    assert selection.use.overlay_mode is SkillOverlayMode.EXTEND
    assert selection.use.overlay_applied is False
    assert selection.use.effective_skill_hash == selection.use.generated_skill_hash
    assert manager.overlay_path.read_text(encoding="utf-8") == document


def test_reuse_degrades_on_overlay_problems_without_writing(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    skill = _skill()
    manager.create_generated(skill)
    _write_overlay(manager, "mode: extend\nofficial_sources: []\n")
    before = sorted(path.name for path in manager.repository_dir.iterdir())

    selection = manager.reuse(FINGERPRINT_A)

    assert selection is not None
    assert selection.effective_skill == skill
    assert selection.use.source is SkillSelectionSource.REUSED
    assert isinstance(selection.overlay_error, RepositorySkillOverlayError)
    assert "official_sources" in selection.warnings[0]
    assert sorted(path.name for path in manager.repository_dir.iterdir()) == before


def test_deeply_nested_overlay_degrades_instead_of_crashing(tmp_path: Path) -> None:
    """A size-bounded overlay can still exhaust the YAML parser's stack."""
    manager = _manager(tmp_path / "data")
    depth = 20_000
    document = "[" * depth + "]" * depth + "\n"
    assert len(document.encode("utf-8")) < MAX_OVERLAY_BYTES
    _write_overlay(manager, document)
    skill = _skill()

    read = manager.read_overlay()

    assert read.overlay is None
    assert read.error is not None
    assert "nested too deeply or invalid YAML" in str(read.error)
    with pytest.raises(RepositorySkillOverlayError, match="nested too deeply"):
        read.require()

    selection = manager.select(skill)
    reused = manager.reuse(FINGERPRINT_A)

    assert selection.effective_skill == skill
    assert selection.use.overlay_applied is False
    assert selection.use.overlay_hash is None
    assert isinstance(selection.overlay_error, RepositorySkillOverlayError)
    assert "nested too deeply or invalid YAML" in selection.warnings[0]
    assert reused is not None
    assert reused.effective_skill == skill
    assert "nested too deeply or invalid YAML" in reused.warnings[0]
    assert manager.overlay_path.read_text(encoding="utf-8") == document


def test_a_valid_overlay_selection_carries_no_warning(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    _write_overlay(
        manager,
        "mode: extend\npolish:\n  summary: House polish.\n  guidance:\n    - Keep names plain.\n",
    )

    selection = manager.select(_skill())

    assert selection.warnings == ()
    assert selection.overlay_error is None
    assert selection.overlay_applied is True


def test_overlay_applies_across_dependency_fingerprints(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    _write_overlay(
        manager,
        "mode: extend\nsimplify:\n  summary: House style.\n  guidance:\n    - Prefer stdlib.\n",
    )

    first = manager.select(_skill(FINGERPRINT_A))
    second = manager.select(_skill(FINGERPRINT_B))

    assert first.generated_path != second.generated_path
    assert first.overlay == second.overlay
    assert first.use.overlay_hash == second.use.overlay_hash
    assert first.use.dependency_fingerprint == FINGERPRINT_A
    assert second.use.dependency_fingerprint == FINGERPRINT_B
    assert "Prefer stdlib." in first.effective_skill.simplify.guidance
    assert "Prefer stdlib." in second.effective_skill.simplify.guidance
    assert sorted(manager.list_generated_fingerprints()) == sorted([FINGERPRINT_A, FINGERPRINT_B])


# -- merging -----------------------------------------------------------------


def test_extend_combines_and_deduplicates_guidance() -> None:
    generated = _skill(
        simplify=_guidance(
            "Generated simplify.",
            guidance=("Prefer the simplest supported form.", "Delete dead code."),
            avoid=("Avoid clever metaprogramming.",),
        )
    )
    overlay = RepositorySkillOverlay(
        mode=SkillOverlayMode.EXTEND,
        simplify=SkillGuidance(
            summary="House style.",
            guidance=("prefer the simplest supported form.", "Keep modules small."),
            avoid=("Avoid clever metaprogramming.",),
            validation=("Run the targeted tests.",),
        ),
    )

    merged = merge_repository_skill(generated, overlay)

    assert merged.simplify.summary == "Generated simplify. House style."
    assert merged.simplify.guidance == (
        "Prefer the simplest supported form.",
        "Delete dead code.",
        "Keep modules small.",
    )
    assert merged.simplify.avoid == ("Avoid clever metaprogramming.",)
    assert merged.simplify.validation == ("Run the targeted tests.",)
    assert merged.polish == generated.polish
    assert merged.targets == generated.targets
    assert merged.official_sources == generated.official_sources
    assert merged.uncertainties == generated.uncertainties
    assert merged.dependency_fingerprint == generated.dependency_fingerprint
    assert merged.generated_at == generated.generated_at


def test_replace_replaces_only_supplied_sections() -> None:
    generated = _skill()
    overlay = RepositorySkillOverlay(
        mode=SkillOverlayMode.REPLACE,
        polish=SkillGuidance(summary="Only polish.", guidance=("Name things plainly.",)),
    )

    merged = merge_repository_skill(generated, overlay)

    assert merged.polish.summary == "Only polish."
    assert merged.polish.guidance == ("Name things plainly.",)
    assert merged.simplify == generated.simplify
    assert merge_repository_skill(generated, None) == generated


def test_extend_fails_clearly_when_effective_bounds_would_be_exceeded() -> None:
    generated = _skill(
        simplify=_guidance(
            "Generated simplify.",
            guidance=tuple(f"Generated rule {index}." for index in range(12)),
        )
    )
    overlay = RepositorySkillOverlay(
        simplify=SkillGuidance(summary="House style.", guidance=("One more rule.",))
    )

    with pytest.raises(RepositorySkillMergeError, match="simplify.guidance"):
        merge_repository_skill(generated, overlay)

    long_overlay = RepositorySkillOverlay(
        simplify=SkillGuidance(summary="H" * 290, guidance=("One more rule.",))
    )
    with pytest.raises(RepositorySkillMergeError, match="simplify.summary"):
        merge_repository_skill(_skill(), long_overlay)


def test_extend_keeps_an_identical_summary_once() -> None:
    generated = _skill(simplify=_guidance("Same summary."))
    overlay = RepositorySkillOverlay(
        simplify=SkillGuidance(summary="same summary.", guidance=("Extra rule.",))
    )

    merged = merge_repository_skill(generated, overlay)

    assert merged.simplify.summary == "Same summary."


# -- hashing and audit -------------------------------------------------------


def test_content_hash_is_stable_and_content_sensitive() -> None:
    skill = _skill()
    reparsed = RepositorySkill.model_validate(json.loads(skill.model_dump_json()))

    assert content_hash(skill) == content_hash(reparsed)
    assert content_hash(skill) != content_hash(_skill(simplify=_guidance("Different.")))
    assert len(content_hash(skill)) == 64


def test_use_audit_records_generation_then_reuse(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    skill = _skill()

    generated = manager.select(skill)
    reused = manager.select(skill)
    reused_without_writing = manager.reuse(FINGERPRINT_A)

    assert generated.use.source is SkillSelectionSource.GENERATED
    assert reused.use.source is SkillSelectionSource.REUSED
    assert reused_without_writing is not None
    assert reused_without_writing.use.source is SkillSelectionSource.REUSED
    assert generated.use.repository_key == manager.repository_key
    assert generated.use.overlay_hash is None
    assert generated.use.overlay_mode is None
    assert generated.use.overlay_applied is False
    assert generated.use.generated_skill_hash == generated.use.effective_skill_hash
    assert generated.effective_skill == skill


def test_use_audit_records_an_applied_overlay(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "data")
    _write_overlay(
        manager,
        "mode: extend\npolish:\n  summary: House polish.\n  guidance:\n    - Keep names plain.\n",
    )

    selection = manager.select(_skill())

    assert selection.overlay is not None
    assert selection.use.overlay_applied is True
    assert selection.use.overlay_mode is SkillOverlayMode.EXTEND
    assert selection.use.overlay_hash == content_hash(selection.overlay)
    assert selection.use.generated_skill_hash == content_hash(selection.generated_skill)
    assert selection.use.effective_skill_hash == content_hash(selection.effective_skill)
    assert selection.use.effective_skill_hash != selection.use.generated_skill_hash
    assert "Keep names plain." in selection.effective_skill.polish.guidance
    assert manager.load_generated(FINGERPRINT_A) == selection.generated_skill


def test_manager_for_repository_uses_the_git_common_dir(
    factory_source_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    manager = RepositorySkillManager.for_repository(data_dir, factory_source_repo)

    manager.create_generated(_skill())

    assert manager.repository_dir.parent == data_dir / "repository-skills" / "v1"
    assert manager.overlay_path.name == OVERLAY_FILENAME
    assert (manager.generated_dir / f"{FINGERPRINT_A}.json").is_file()
    assert os.path.isdir(manager.generated_dir)


# -- generated provenance validation -----------------------------------------

OFFICIAL_ORIGINS = ("https://react.dev", "https://docs.pytest.org")
PRACTICE_URLS = ("https://example.invalid/review.md",)


def _dependency(
    name: str,
    declared_version: str,
    *,
    manifest_path: str = "package.json",
    ecosystem: DependencyEcosystem = DependencyEcosystem.NPM,
) -> RepositoryDependency:
    return RepositoryDependency(
        ecosystem=ecosystem,
        name=name,
        declared_version=declared_version,
        manifest_path=manifest_path,
        group="dependencies",
    )


def _profile(
    *dependencies: RepositoryDependency,
    version_files: tuple[str, ...] = ("package.json",),
) -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint="c" * 64,
        dependency_fingerprint=FINGERPRINT_A,
        version_files=version_files,
        dependencies=dependencies,
    )


def _target(
    name: str,
    declared_version: str,
    *,
    evidence: tuple[str, ...] = ("package.json",),
    ecosystem: DependencyEcosystem = DependencyEcosystem.NPM,
) -> SkillTarget:
    return SkillTarget(
        ecosystem=ecosystem, name=name, declared_version=declared_version, evidence=evidence
    )


def _official(
    url: str = "https://react.dev/reference/react",
    applies_to: tuple[str, ...] = ("react",),
    version_scope: str = "19.1.0",
) -> SkillSource:
    return SkillSource(
        title="Documentation", url=url, version_scope=version_scope, applies_to=applies_to
    )


def _grounded_skill(
    targets: tuple[SkillTarget, ...] = (),
    official_sources: tuple[SkillSource, ...] = (),
    practice_sources: tuple[SkillSource, ...] = (),
    dependency_fingerprint: str = FINGERPRINT_A,
) -> RepositorySkill:
    return RepositorySkill(
        dependency_fingerprint=dependency_fingerprint,
        targets=targets,
        official_sources=official_sources,
        practice_sources=practice_sources,
        simplify=_guidance("Simplify."),
        polish=_guidance("Polish."),
        uncertainties=("Bounded fixture.",),
    )


def _validate(skill: RepositorySkill, profile: RepositoryProfile) -> str | None:
    return repository_skill_validation_error(
        skill,
        profile,
        official_documentation_origins=OFFICIAL_ORIGINS,
        practice_reference_urls=PRACTICE_URLS,
    )


def test_grounded_skill_passes_validation() -> None:
    profile = _profile(_dependency("react", "19.1.0"))
    skill = _grounded_skill(
        targets=(_target("react", "19.1.0"),),
        official_sources=(_official(),),
        practice_sources=(
            SkillSource(
                title="Quality review heuristics",
                url=PRACTICE_URLS[0],
                version_scope="general",
                applies_to=(GENERIC_SKILL_TARGET,),
            ),
        ),
    )

    assert _validate(skill, profile) is None


def test_validation_rejects_a_mismatched_dependency_fingerprint() -> None:
    profile = _profile(_dependency("react", "19.1.0"))
    skill = _grounded_skill(
        targets=(_target("react", "19.1.0"),),
        official_sources=(_official(),),
        dependency_fingerprint=FINGERPRINT_B,
    )

    error = _validate(skill, profile)

    assert error == "researcher returned repository guidance for a different dependency fingerprint"


def test_validation_rejects_unverified_target_versions_and_evidence() -> None:
    profile = _profile(_dependency("react", "19.1.0"))

    wrong_version = _grounded_skill(
        targets=(_target("react", "18.3.1"),), official_sources=(_official(),)
    )
    unverified_evidence = _grounded_skill(
        targets=(_target("react", "19.1.0", evidence=("invented/package.json",)),),
        official_sources=(_official(),),
    )

    version_error = _validate(wrong_version, profile)
    evidence_error = _validate(unverified_evidence, profile)

    assert version_error is not None
    assert "unverified dependency version: npm:react" in version_error
    assert evidence_error is not None
    assert "unverified version evidence for npm:react" in evidence_error


def test_validation_requires_every_recognized_dependency_to_be_targeted() -> None:
    profile = _profile(
        _dependency("react", "19.1.0"),
        _dependency("react", "18.3.1", manifest_path="apps/legacy/package.json"),
        version_files=("package.json", "apps/legacy/package.json"),
    )
    skill = _grounded_skill(targets=(_target("react", "19.1.0"),), official_sources=(_official(),))

    error = _validate(skill, profile)

    assert error is not None
    assert error.startswith("researcher omitted version targets required by the repository profile")
    assert "npm:react@18.3.1" in error


def test_validation_confines_official_sources_to_configured_origins_and_dependencies() -> None:
    profile = _profile(_dependency("react", "19.1.0"))

    off_origin = _grounded_skill(
        targets=(_target("react", "19.1.0"),),
        official_sources=(_official(url="https://blog.invalid/react"),),
    )
    undetected = _grounded_skill(
        targets=(_target("react", "19.1.0"),),
        official_sources=(_official(applies_to=("react", "svelte")),),
    )
    uppercase_host = _grounded_skill(
        targets=(_target("react", "19.1.0"),),
        official_sources=(_official(url="https://REACT.dev/reference/react"),),
    )

    origin_error = _validate(off_origin, profile)
    undetected_error = _validate(undetected, profile)

    assert origin_error is not None
    assert "outside polish.official_documentation_origins" in origin_error
    assert undetected_error is not None
    assert "not in the repository profile" in undetected_error
    assert "svelte" in undetected_error
    assert _validate(uppercase_host, profile) is None


def test_validation_requires_official_provenance_for_every_target() -> None:
    profile = _profile(
        _dependency("left-pad", "1.3.0"),
        _dependency("right-pad", "1.0.0"),
    )
    skill = _grounded_skill(
        targets=(_target("left-pad", "1.3.0"),),
        official_sources=(_official(applies_to=("right-pad",)),),
    )

    error = _validate(skill, profile)

    assert error is not None
    assert "without official source provenance for: left-pad" in error


def test_validation_confines_practice_sources_to_the_configured_allowlist() -> None:
    profile = _profile(_dependency("react", "19.1.0"))
    base = _grounded_skill(targets=(_target("react", "19.1.0"),), official_sources=(_official(),))
    generic_source = SkillSource(
        title="Quality review heuristics",
        url=PRACTICE_URLS[0],
        version_scope="general",
        applies_to=(GENERIC_SKILL_TARGET,),
    )

    off_allowlist = base.model_copy(
        update={
            "practice_sources": (
                generic_source.model_copy(update={"url": "https://elsewhere.invalid/notes.md"}),
            )
        }
    )
    dependency_scoped = base.model_copy(
        update={"practice_sources": (generic_source.model_copy(update={"applies_to": ("react",)}),)}
    )
    version_claiming = base.model_copy(
        update={
            "practice_sources": (generic_source.model_copy(update={"version_scope": "React 19"}),)
        }
    )

    allowlist_error = _validate(off_allowlist, profile)
    scope_error = _validate(dependency_scoped, profile)
    version_error = _validate(version_claiming, profile)

    assert allowlist_error is not None
    assert "outside polish.practice_reference_urls" in allowlist_error
    assert scope_error is not None
    assert "outside the generic repository scope" in scope_error
    assert version_error is not None
    assert "carrying a version claim" in version_error
    assert "React 19" in version_error


def test_validation_accepts_a_profile_with_no_dependencies_and_no_targets() -> None:
    assert _validate(_grounded_skill(), _profile()) is None
