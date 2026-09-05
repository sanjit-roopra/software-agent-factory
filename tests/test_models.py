from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from software_agent_factory.models import (
    GENERIC_PRACTICE_VERSION_SCOPE,
    GENERIC_SKILL_TARGET,
    REQUIRED_SKILL_TARGET_NAMES,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    Complexity,
    DependencyEcosystem,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanStep,
    RepairContext,
    RepositoryDependency,
    RepositoryProfile,
    RepositorySkill,
    RepositorySkillOverlay,
    RepositorySkillUse,
    RepositoryTechnology,
    ResearchReport,
    ReviewReport,
    Risk,
    RunLease,
    SkillGuidance,
    SkillOverlayMode,
    SkillSelectionSource,
    SkillSource,
    SkillTarget,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkflowState,
    WorkItem,
)


def test_domain_models_round_trip_and_normalize_utc_datetimes() -> None:
    started_at = datetime(2026, 9, 4, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    completed_at = started_at + timedelta(minutes=5)

    work_item = WorkItem(
        id="WI-123",
        source="MANUAL",
        title="Reject empty names",
        description="Add validation for empty customer names.",
        acceptance_criteria=["Reject empty strings", "Reject whitespace-only strings"],
        constraints=["Do not alter unrelated APIs"],
        labels=["validation"],
        priority="P1",
        created_at=started_at,
    )
    attempt = AttemptRecord(
        attempt_number=1,
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        started_at=started_at,
        completed_at=completed_at,
        outcome="failed",
        failure_reason="pytest failed",
    )
    factory_run = FactoryRun(
        id="RUN-123",
        work_item_id=work_item.id,
        state=WorkflowState.IMPLEMENTING,
        attempt_records=[attempt],
        workspace_path="/workspace/TASK-123",
        branch_name="factory/task-123",
        created_at=started_at,
        updated_at=completed_at,
    )
    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R1,
        requirements_quality="clear",
        needs_research=False,
        dependencies=["pytest"],
        unknowns=["none"],
        confidence=0.8,
    )
    specification = Specification(
        problem="Customer names should not be empty.",
        acceptance_criteria=["Reject empty strings", "Reject whitespace-only strings"],
        constraints=["Keep the existing API shape"],
        assumptions=["Whitespace can be trimmed"],
        unknowns=[],
        dependencies=["customer validator"],
        risk_flags=["input validation"],
        confidence=0.75,
    )
    research = ResearchReport(
        question="How is customer validation handled today?",
        findings=["Validation happens in the API layer."],
        evidence=["src/api/customers.py"],
        implications=["Add a guard before persistence."],
        uncertainty=["No shared validator exists yet."],
    )
    execution_plan = ExecutionPlan(
        summary="Add input validation and tests.",
        steps=[
            PlanStep(
                id="update-validator",
                goal="Reject empty customer names.",
                likely_files=["src/app.py", "tests/test_app.py"],
                validation=["Run targeted pytest"],
            )
        ],
        expected_scope=ExpectedScope(
            modules=["src", "tests"],
            estimated_files_min=1,
            estimated_files_max=3,
        ),
        test_strategy=["Run targeted pytest"],
        risks=["Validation could affect existing requests."],
    )
    change_set = ChangeSet(
        summary="Added validation and tests.",
        changed_files=["src/app.py", "tests/test_app.py"],
        tests_added=["tests/test_app.py"],
        commands_run=["pytest tests/test_app.py"],
    )
    verification = VerificationReport(
        passed=True,
        deterministic_checks=[
            CommandResult(
                command="pytest tests/test_app.py",
                exit_code=0,
                stdout="1 passed",
                duration_seconds=1.2,
            )
        ],
        failures=[],
        coverage_change=0.0,
        test_findings=["No regressions observed."],
        confidence=0.9,
    )
    review = ReviewReport(
        approved=True,
        findings=[],
        scope_concerns=[],
        security_concerns=[],
        compatibility_concerns=[],
        suggested_changes=[],
    )
    repository_profile = RepositoryProfile(
        manifest_fingerprint="a" * 64,
        dependency_fingerprint="b" * 64,
        markers=("pyproject.toml",),
        version_files=("pyproject.toml",),
        technologies=(RepositoryTechnology.PYTHON,),
        dependencies=(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.PYTHON,
                name="python",
                declared_version=">=3.13",
                manifest_path="pyproject.toml",
                group="runtime",
            ),
        ),
    )
    repository_skill = RepositorySkill(
        dependency_fingerprint=repository_profile.dependency_fingerprint,
        targets=(
            SkillTarget(
                ecosystem=DependencyEcosystem.PYTHON,
                name="python",
                declared_version=">=3.13",
                resolved_version="3.13.7",
                evidence=("pyproject.toml",),
            ),
        ),
        official_sources=(
            SkillSource(
                title="Python 3.13 documentation",
                url="https://docs.python.org/3.13/",
                version_scope="3.13",
                applies_to=("python",),
            ),
        ),
        simplify=SkillGuidance(
            summary="Use Python 3.13 simplifications.",
            guidance=("Prefer direct typed code.",),
        ),
        polish=SkillGuidance(
            summary="Polish for Python 3.13.",
            guidance=("Use supported Python 3.13 APIs.",),
        ),
    )

    assert work_item.created_at.tzinfo is UTC
    assert attempt.started_at.tzinfo is UTC
    assert factory_run.updated_at.tzinfo is UTC

    for model in (
        work_item,
        factory_run,
        triage,
        specification,
        research,
        repository_profile,
        repository_skill,
        execution_plan,
        change_set,
        verification,
        review,
    ):
        round_tripped = type(model).model_validate_json(model.model_dump_json())
        assert round_tripped == model
        assert round_tripped.schema_version == 1

    factory_run_dump = factory_run.model_dump(mode="json")
    assert factory_run_dump["state"] == WorkflowState.IMPLEMENTING.value
    assert factory_run_dump["attempt_records"][0]["role"] == AgentRole.IMPLEMENTER.value
    assert factory_run_dump["created_at"].endswith("Z")


def test_attempt_record_defaults_keep_existing_json_valid() -> None:
    """Phase 1 attempt records were persisted without budget/trigger fields."""
    legacy_json = (
        '{"attempt_number": 1, "role": "IMPLEMENTER", "model": "claude-sonnet-5",'
        ' "reasoning": "medium", "started_at": "2026-09-04T10:00:00Z",'
        ' "completed_at": "2026-09-04T10:05:00Z", "outcome": "succeeded"}'
    )

    attempt = AttemptRecord.model_validate_json(legacy_json)

    assert attempt.budget is AttemptBudget.IMPLEMENTATION
    assert attempt.triggered_by is AttemptTrigger.INITIAL


def test_attempt_record_records_explicit_budget_and_trigger() -> None:
    started_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    attempt = AttemptRecord(
        attempt_number=2,
        role=AgentRole.IMPLEMENTER,
        model="claude-opus-5",
        reasoning="high",
        started_at=started_at,
        completed_at=started_at,
        outcome="failed",
        failure_reason="CI test job failed",
        budget=AttemptBudget.CI_REPAIR,
        triggered_by=AttemptTrigger.CI,
    )

    round_tripped = AttemptRecord.model_validate_json(attempt.model_dump_json())

    assert round_tripped == attempt
    assert round_tripped.budget is AttemptBudget.CI_REPAIR
    assert round_tripped.triggered_by is AttemptTrigger.CI


def test_polish_attempt_trigger_round_trips() -> None:
    started_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    attempt = AttemptRecord(
        attempt_number=2,
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        started_at=started_at,
        completed_at=started_at,
        outcome="succeeded",
        triggered_by=AttemptTrigger.POLISH,
    )

    assert AttemptRecord.model_validate_json(attempt.model_dump_json()) == attempt


def test_extended_workflow_states_and_roles_exist() -> None:
    assert {
        WorkflowState.RESEARCHING,
        WorkflowState.PR_CREATED,
        WorkflowState.CI_RUNNING,
        WorkflowState.CI_DIAGNOSIS,
        WorkflowState.DONE,
    } <= set(WorkflowState)
    assert AgentRole.RESEARCHER in set(AgentRole)
    # States deliberately not introduced (see the task's scope constraints).
    assert not {"REPAIRING", "PLAN_READY", "BLOCKED"} & {state.value for state in WorkflowState}


def test_factory_run_additive_fields_default_to_none_for_schema_version_1() -> None:
    legacy_json = (
        '{"schema_version": 1, "id": "RUN-1", "work_item_id": "WI-1", "state": "CREATED",'
        ' "created_at": "2026-09-04T10:00:00Z", "updated_at": "2026-09-04T10:00:00Z"}'
    )

    run = FactoryRun.model_validate_json(legacy_json)

    assert run.last_activity_at is None
    assert run.lease is None
    assert run.commit_sha is None
    assert run.schema_version == 1


def test_factory_run_lease_and_activity_round_trip() -> None:
    created_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    run = FactoryRun(
        id="RUN-2",
        work_item_id="WI-2",
        state=WorkflowState.CI_RUNNING,
        created_at=created_at,
        updated_at=created_at,
        last_activity_at=created_at + timedelta(minutes=1),
        lease=RunLease(host="macbook.local", pid=4321, heartbeat_at=created_at),
        commit_sha="a" * 40,
        pull_request_url="https://github.com/acme/app/pull/7",
    )

    round_tripped = FactoryRun.model_validate_json(run.model_dump_json())

    assert round_tripped == run
    assert round_tripped.lease is not None
    assert round_tripped.lease.pid == 4321
    assert round_tripped.schema_version == 1


def test_test_report_is_distinct_from_deterministic_verification_report() -> None:
    test_report = TestReport(
        passed=False,
        findings=["Whitespace-only names are still accepted."],
        suggested_tests=["Add a whitespace-only regression test."],
        confidence=0.6,
    )

    round_tripped = TestReport.model_validate_json(test_report.model_dump_json())

    assert round_tripped == test_report
    assert round_tripped.schema_version == 1
    # A TestReport carries no deterministic evidence fields.
    assert "deterministic_checks" not in test_report.model_dump()
    assert "deterministic_checks" in VerificationReport(passed=True, confidence=1.0).model_dump()


def test_repair_context_is_small_and_typed() -> None:
    context = RepairContext(
        trigger=AttemptTrigger.VERIFICATION,
        summary="pytest failed",
        failures=["'uv run pytest' exited with code 1"],
        log_excerpt="AssertionError: expected 2",
    )

    assert RepairContext.model_validate_json(context.model_dump_json()) == context
    assert set(context.model_dump()) == {"trigger", "summary", "failures", "log_excerpt"}


def _guidance(summary: str) -> SkillGuidance:
    return SkillGuidance(summary=summary, guidance=("Prefer the simplest supported form.",))


def test_skill_source_requires_bounded_distinct_applicability() -> None:
    with pytest.raises(ValidationError):
        SkillSource(  # type: ignore[call-arg]
            title="React documentation",
            url="https://react.dev/reference/react",
            version_scope="19.1.0",
        )

    with pytest.raises(ValidationError):
        SkillSource(
            title="React documentation",
            url="https://react.dev/reference/react",
            version_scope="19.1.0",
            applies_to=(),
        )

    with pytest.raises(ValidationError, match="distinct"):
        SkillSource(
            title="React documentation",
            url="https://react.dev/reference/react",
            version_scope="19.1.0",
            applies_to=("react", "react"),
        )

    with pytest.raises(ValidationError, match="cannot be combined"):
        SkillSource(
            title="Quality review heuristics",
            url="https://example.invalid/review.md",
            version_scope="general",
            applies_to=(GENERIC_SKILL_TARGET, "react"),
        )

    shared = SkillSource(
        title="React documentation",
        url="https://react.dev/reference/react",
        version_scope="19.1.0",
        applies_to=("react", "react-dom"),
    )

    assert shared.applies_to == ("react", "react-dom")
    assert SkillSource.model_validate_json(shared.model_dump_json()) == shared


def test_official_sources_may_not_claim_the_generic_repository_target() -> None:
    with pytest.raises(ValidationError, match="official sources must name the dependencies"):
        RepositorySkill(
            dependency_fingerprint="b" * 64,
            official_sources=(
                SkillSource(
                    title="React documentation",
                    url="https://react.dev/reference/react",
                    version_scope="19.1.0",
                    applies_to=(GENERIC_SKILL_TARGET,),
                ),
            ),
            simplify=_guidance("Simplify."),
            polish=_guidance("Polish."),
        )

    skill = RepositorySkill(
        dependency_fingerprint="b" * 64,
        practice_sources=(
            SkillSource(
                title="Quality review heuristics",
                url="https://example.invalid/review.md",
                version_scope="general",
                applies_to=(GENERIC_SKILL_TARGET,),
            ),
        ),
        simplify=_guidance("Simplify."),
        polish=_guidance("Polish."),
        uncertainties=("No official source was consulted.",),
    )

    assert skill.practice_sources[0].applies_to == (GENERIC_SKILL_TARGET,)
    assert RepositorySkill.model_validate_json(skill.model_dump_json()) == skill


def test_practice_sources_must_be_generic_and_version_neutral() -> None:
    with pytest.raises(ValidationError, match="generic repository target"):
        RepositorySkill(
            dependency_fingerprint="b" * 64,
            practice_sources=(
                SkillSource(
                    title="Quality review heuristics",
                    url="https://example.invalid/review.md",
                    version_scope="general",
                    applies_to=("typescript",),
                ),
            ),
            simplify=_guidance("Simplify."),
            polish=_guidance("Polish."),
            uncertainties=("No official source was consulted.",),
        )

    with pytest.raises(ValidationError, match="version scope 'general'"):
        RepositorySkill(
            dependency_fingerprint="b" * 64,
            practice_sources=(
                SkillSource(
                    title="Quality review heuristics",
                    url="https://example.invalid/review.md",
                    version_scope="TypeScript 5.9",
                    applies_to=(GENERIC_SKILL_TARGET,),
                ),
            ),
            simplify=_guidance("Simplify."),
            polish=_guidance("Polish."),
            uncertainties=("No official source was consulted.",),
        )


def test_practice_source_version_scope_comparison_is_case_insensitive() -> None:
    skill = RepositorySkill(
        dependency_fingerprint="b" * 64,
        practice_sources=(
            SkillSource(
                title="Quality review heuristics",
                url="https://example.invalid/review.md",
                version_scope="General",
                applies_to=(GENERIC_SKILL_TARGET,),
            ),
        ),
        simplify=_guidance("Simplify."),
        polish=_guidance("Polish."),
        uncertainties=("No official source was consulted.",),
    )

    assert skill.practice_sources[0].version_scope.casefold() == GENERIC_PRACTICE_VERSION_SCOPE
    assert RepositorySkill.model_validate_json(skill.model_dump_json()) == skill

    with pytest.raises(ValidationError, match="version scope 'general'"):
        RepositorySkill.model_validate_json(
            skill.model_dump_json().replace('"General"', '"react 19"')
        )


def test_required_skill_target_names_are_the_recognized_version_targets() -> None:
    assert REQUIRED_SKILL_TARGET_NAMES == (
        "python",
        "pytest",
        "react",
        "react-dom",
        "vite",
        "vitest",
    )
    assert GENERIC_SKILL_TARGET not in REQUIRED_SKILL_TARGET_NAMES


def test_repository_skill_overlay_is_prose_only_and_strict() -> None:
    overlay = RepositorySkillOverlay(simplify=_guidance("House simplify style."))

    assert overlay.schema_version == 1
    assert overlay.mode is SkillOverlayMode.EXTEND
    assert overlay.polish is None
    assert RepositorySkillOverlay.model_validate_json(overlay.model_dump_json()) == overlay

    with pytest.raises(ValidationError, match="simplify"):
        RepositorySkillOverlay(mode=SkillOverlayMode.REPLACE)

    machine_owned = {
        "targets": [],
        "official_sources": [],
        "practice_sources": [],
        "uncertainties": [],
        "dependency_fingerprint": "a" * 64,
        "generator_version": 1,
        "generated_at": "2026-01-01T00:00:00Z",
        "detector_version": 2,
    }
    for field, value in machine_owned.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            RepositorySkillOverlay.model_validate(
                {"mode": "extend", "simplify": overlay.simplify, field: value}
            )

    with pytest.raises(ValidationError, match="schema_version"):
        RepositorySkillOverlay.model_validate({"schema_version": 2, "simplify": overlay.simplify})
    with pytest.raises(ValidationError, match="mode"):
        RepositorySkillOverlay.model_validate({"mode": "merge", "simplify": overlay.simplify})


def _use(**overrides: object) -> RepositorySkillUse:
    payload: dict[str, object] = {
        "repository_key": "demo-0123456789abcdef",
        "dependency_fingerprint": "a" * 64,
        "source": SkillSelectionSource.GENERATED,
        "generated_skill_hash": "b" * 64,
        "effective_skill_hash": "b" * 64,
    }
    payload.update(overrides)
    return RepositorySkillUse.model_validate(payload)


def test_repository_skill_use_records_bounded_consistent_provenance() -> None:
    use = _use()

    assert use.overlay_applied is False
    assert use.selected_at.tzinfo is not None
    assert RepositorySkillUse.model_validate_json(use.model_dump_json()) == use

    applied = _use(
        source=SkillSelectionSource.REUSED,
        overlay_hash="c" * 64,
        overlay_mode=SkillOverlayMode.EXTEND,
        overlay_applied=True,
        effective_skill_hash="d" * 64,
    )
    assert applied.overlay_mode is SkillOverlayMode.EXTEND

    with pytest.raises(ValidationError, match="must record its hash"):
        _use(overlay_applied=True)
    with pytest.raises(ValidationError, match="must record its mode"):
        _use(overlay_hash="c" * 64)
    with pytest.raises(ValidationError, match="must equal the generated skill"):
        _use(effective_skill_hash="e" * 64)
    with pytest.raises(ValidationError, match="repository_key"):
        _use(repository_key="../escape")
    with pytest.raises(ValidationError, match="generated_skill_hash"):
        _use(generated_skill_hash="not-a-hash", effective_skill_hash="not-a-hash")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _use(guidance="prose")
