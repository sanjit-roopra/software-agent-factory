from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from software_agent_factory.models import (
    COST_UNIT_FIELDS,
    MAX_GUIDANCE_FINDINGS,
    MAX_PLAN_DECISIONS,
    TOKEN_CLASS_FIELDS,
    TOTAL_TOKEN_FIELDS,
    AgentRole,
    AttemptBudget,
    AttemptRecord,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    Complexity,
    ContextTier,
    DashboardResumeRequest,
    DependencyEcosystem,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    InvocationRecord,
    ModelBase,
    ModelUsage,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
    PlanStep,
    ProjectBrief,
    ProjectPlan,
    ProjectTask,
    RejectedCommand,
    RepairContext,
    RepositoryCommandsPlan,
    RepositoryCommandsSource,
    RepositoryDependency,
    RepositoryProfile,
    RepositoryTechnology,
    ReviewReport,
    Risk,
    RunLease,
    Specification,
    TestReport,
    TriageResult,
    UsageMetrics,
    VerificationReport,
    WorkflowState,
    WorkItem,
)


def test_project_plan_accepts_one_smallest_sufficient_task() -> None:
    brief = ProjectBrief(
        id="project-1",
        title="Add customer validation",
        description="Reject blank customer names.",
        repository_path="/repo",
    )
    plan = ProjectPlan(
        project_id=brief.id,
        summary="One coherent validation change.",
        delivery_approach="Use one task because implementation and tests form one outcome.",
        tasks=(
            ProjectTask(
                id=1,
                title="Reject blank customer names",
                description="Add validation and focused tests.",
                acceptance_criteria=("Blank names return HTTP 400.",),
            ),
        ),
    )

    assert plan.tasks[0].id == 1


@pytest.mark.parametrize(
    "modules",
    [
        ["application source"],
        ["*.py"],
        [" src"],
        ["src", "src"],
    ],
)
def test_expected_scope_rejects_invalid_top_level_paths(modules: list[str]) -> None:
    with pytest.raises(ValueError, match="expected_scope.modules"):
        ExpectedScope(
            modules=modules,
            estimated_files_min=1,
            estimated_files_max=2,
        )


def test_expected_scope_accepts_nested_repository_path_prefix() -> None:
    scope = ExpectedScope(
        modules=["src/software_agent_factory"],
        estimated_files_min=1,
        estimated_files_max=2,
    )

    assert scope.modules == ["src/software_agent_factory"]


@pytest.mark.parametrize(
    "project_fields",
    [
        {"project_task_id": 1},
        {"depends_on": [1]},
        {"project_id": "project-1", "project_task_id": 2, "depends_on": [2]},
        {"project_id": "project-1", "project_task_id": 2, "depends_on": [3]},
    ],
)
def test_work_item_rejects_invalid_project_linkage(
    project_fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkItem(
            id="WI-project",
            title="Invalid project linkage",
            description="Reject invalid project metadata.",
            **project_fields,
        )


@pytest.mark.parametrize(
    "tasks",
    [
        (
            ProjectTask(
                id=2,
                title="Second",
                description="Out of order.",
                acceptance_criteria=("It works.",),
            ),
        ),
        (
            {
                "id": 1,
                "title": "First",
                "description": "Invalid dependency.",
                "acceptance_criteria": ["It works."],
                "dependencies": [1],
            },
        ),
        (
            ProjectTask(
                id=1,
                title="Duplicate",
                description="First.",
                acceptance_criteria=("First works.",),
            ),
            ProjectTask(
                id=2,
                title="duplicate",
                description="Second.",
                acceptance_criteria=("Second works.",),
                dependencies=(1,),
            ),
        ),
    ],
)
def test_project_plan_rejects_invalid_task_graph(tasks: tuple[object, ...]) -> None:
    with pytest.raises(ValidationError):
        ProjectPlan(
            project_id="project-1",
            summary="Invalid plan.",
            delivery_approach="This should be rejected.",
            tasks=tasks,
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
    invocation = InvocationRecord(
        invocation_number=1,
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        context_tier=ContextTier.LONG_CONTEXT,
        started_at=started_at,
        completed_at=completed_at,
        success=True,
        attempt_number=1,
        usage=UsageMetrics(
            total_nano_aiu=123,
            model_usage=(
                ModelUsage(
                    model="claude-sonnet-5",
                    input_tokens=100,
                    output_tokens=20,
                    reasoning_tokens=5,
                ),
            ),
        ),
    )
    factory_run = FactoryRun(
        id="RUN-123",
        work_item_id=work_item.id,
        state=WorkflowState.IMPLEMENTING,
        attempt_records=[attempt],
        invocation_records=[invocation],
        workspace_path="/workspace/TASK-123",
        branch_name="factory/task-123",
        created_at=started_at,
        updated_at=completed_at,
    )
    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R1,
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

    assert work_item.created_at.tzinfo is UTC
    assert attempt.started_at.tzinfo is UTC
    assert factory_run.updated_at.tzinfo is UTC

    for model in (
        work_item,
        factory_run,
        triage,
        specification,
        repository_profile,
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
    assert factory_run_dump["invocation_records"][0]["context_tier"] == "long_context"
    assert factory_run_dump["invocation_records"][0]["usage"]["total_nano_aiu"] == 123
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
    assert attempt.context_tier is ContextTier.DEFAULT
    assert attempt.invocation_number is None


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
    # Kept so old run records load (ADR-035).
    assert {WorkflowState.REFINING, WorkflowState.RESEARCHING} <= set(WorkflowState)
    assert {AgentRole.REFINER, AgentRole.RESEARCHER} <= set(AgentRole)
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
    assert run.invocation_records == []
    assert run.schema_version == 1


def test_list_price_estimate_round_trips_on_model_usage_and_usage_metrics() -> None:
    usage = UsageMetrics(
        total_nano_aiu=123,
        list_price_estimate_usd=0.42,
        model_usage=(
            ModelUsage(
                model="claude-sonnet-5",
                input_tokens=100,
                output_tokens=20,
                list_price_estimate_usd=0.42,
            ),
        ),
    )

    round_tripped = UsageMetrics.model_validate_json(usage.model_dump_json())

    assert round_tripped == usage
    assert round_tripped.list_price_estimate_usd == 0.42
    assert round_tripped.model_usage[0].list_price_estimate_usd == 0.42


def test_list_price_estimate_defaults_to_unknown_for_legacy_usage_json() -> None:
    """Runs persisted before this field existed load with an unknown estimate."""
    legacy_model_usage_json = '{"model": "claude-sonnet-5", "input_tokens": 100}'
    legacy_usage_metrics_json = '{"total_nano_aiu": 123}'

    model_usage = ModelUsage.model_validate_json(legacy_model_usage_json)
    usage_metrics = UsageMetrics.model_validate_json(legacy_usage_metrics_json)

    assert model_usage.list_price_estimate_usd is None
    assert usage_metrics.list_price_estimate_usd is None


def test_list_price_estimate_rejects_negative_value() -> None:
    with pytest.raises(ValidationError, match="list_price_estimate_usd"):
        ModelUsage(model="claude-sonnet-5", list_price_estimate_usd=-0.01)

    with pytest.raises(ValidationError, match="list_price_estimate_usd"):
        UsageMetrics(list_price_estimate_usd=-0.01)


def test_invocation_record_requires_valid_timestamps_and_failure_reason() -> None:
    with pytest.raises(ValidationError, match="completed_at"):
        InvocationRecord(
            invocation_number=1,
            role=AgentRole.TRIAGE,
            model="gpt-5.6-terra",
            reasoning="medium",
            started_at=datetime(2026, 9, 4, 10, 5, tzinfo=UTC),
            completed_at=datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
            success=True,
        )

    with pytest.raises(ValidationError, match="failure_reason"):
        InvocationRecord(
            invocation_number=1,
            role=AgentRole.TRIAGE,
            model="gpt-5.6-terra",
            reasoning="medium",
            started_at=datetime(2026, 9, 4, 10, 0, tzinfo=UTC),
            completed_at=datetime(2026, 9, 4, 10, 1, tzinfo=UTC),
            success=False,
        )


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


def test_expected_scope_requires_at_least_one_path_prefix() -> None:
    with pytest.raises(ValidationError, match="modules"):
        ExpectedScope.model_validate(
            {
                "estimated_files_min": 1,
                "estimated_files_max": 3,
            }
        )


def test_repair_context_is_small_and_typed() -> None:
    context = RepairContext(
        trigger=AttemptTrigger.VERIFICATION,
        summary="pytest failed",
        failures=["'uv run pytest' exited with code 1"],
        log_excerpt="AssertionError: expected 2",
    )

    assert RepairContext.model_validate_json(context.model_dump_json()) == context
    assert set(context.model_dump()) == {"trigger", "summary", "failures", "log_excerpt"}


def test_legacy_execution_plan_payload_validates_without_unresolved_decisions() -> None:
    legacy_json = (
        '{"schema_version": 1, "summary": "Implement feature", "steps": [], '
        '"expected_scope": {"modules": ["src"], "estimated_files_min": 1, '
        '"estimated_files_max": 2}, "test_strategy": ["pytest"], "risks": ["none"]}'
    )
    plan = ExecutionPlan.model_validate_json(legacy_json)
    assert plan.unresolved_decisions == []
    assert plan.is_ready is True
    assert plan.ready is True


def test_execution_plan_derived_readiness_behavior() -> None:
    ready_plan = ExecutionPlan(
        summary="Implement feature",
        steps=[],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=[],
    )
    assert ready_plan.is_ready is True
    assert ready_plan.ready is True

    unready_plan = ExecutionPlan(
        summary="Implement feature",
        steps=[],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=["Need choice between sqlite and postgres."],
    )
    assert unready_plan.is_ready is False
    assert unready_plan.ready is False

    # Readiness is a derived property and must not appear in serialized payloads.
    dumped = ready_plan.model_dump()
    assert "is_ready" not in dumped
    assert "ready" not in dumped
    assert "is_ready" not in ready_plan.model_dump_json()
    assert "ready" not in ready_plan.model_dump_json()


def _invocation_payload() -> dict[str, object]:
    return {
        "invocation_number": 1,
        "role": "TRIAGE",
        "model": "m",
        "reasoning": "low",
        "started_at": "2026-09-29T10:00:00Z",
        "completed_at": "2026-09-29T10:00:01Z",
        "success": True,
    }


def test_invocation_record_ignores_the_removed_writing_findings() -> None:
    """ADR-036: old invocation records may still carry advisory writing findings."""
    payload = {**_invocation_payload(), "writing_findings": ["summary has 26 words."]}

    record = InvocationRecord.model_validate(payload)

    assert "writing_findings" not in record.model_dump()


def test_factory_run_ignores_the_removed_performance_mode_fields() -> None:
    """ADR-036: old run records may still carry the fast performance mode fields."""
    legacy_json = (
        '{"schema_version": 1, "id": "RUN-1", "work_item_id": "WI-1", "state": "DONE",'
        ' "created_at": "2026-09-04T10:00:00Z", "updated_at": "2026-09-04T10:00:00Z",'
        ' "requested_performance_mode": "fast", "effective_performance_mode": "standard",'
        ' "performance_model_profile": "economy",'
        ' "performance_fallback_reason": "scope includes protected files: README.md",'
        ' "invocation_records": [{"invocation_number": 1, "role": "TRIAGE", "model": "m",'
        ' "reasoning": "low", "started_at": "2026-09-04T10:00:00Z",'
        ' "completed_at": "2026-09-04T10:00:01Z", "success": true,'
        ' "writing_findings": ["unknowns[0] has 2 slop_word finding(s)."]}]}'
    )

    run = FactoryRun.model_validate_json(legacy_json)

    assert run.state is WorkflowState.DONE
    assert len(run.invocation_records) == 1
    dumped = run.model_dump()
    assert "effective_performance_mode" not in dumped
    assert "performance_fallback_reason" not in dumped


def test_triage_result_no_longer_asks_the_model_for_requirements_quality() -> None:
    schema = TriageResult.model_json_schema()

    assert "requirements_quality" not in schema["properties"]
    assert "requirements_quality" not in schema["required"]


def test_old_triage_json_with_requirements_quality_still_loads() -> None:
    old = (
        '{"schema_version":1,"factory_eligible":true,"complexity":"L1","risk":"R1",'
        '"requirements_quality":"clear","needs_research":false,"confidence":0.9}'
    )

    result = TriageResult.model_validate_json(old)

    assert result.complexity is Complexity.L1
    assert "requirements_quality" not in result.model_dump_json()


def test_old_triage_json_without_needs_research_loads_and_new_triage_defaults_it() -> None:
    old = '{"schema_version":1,"factory_eligible":true,"complexity":"L1","risk":"R1",'
    old += '"needs_research":true,"confidence":0.9}'

    assert TriageResult.model_validate_json(old).needs_research is True
    assert (
        TriageResult(
            factory_eligible=True, complexity=Complexity.L1, risk=Risk.R1, confidence=0.9
        ).needs_research
        is False
    )


_SCOPE = {"modules": ["src"], "estimated_files_min": 1, "estimated_files_max": 1}
_LOCATION = {"path": "src/a.py", "start_line": 1, "end_line": 1}
_TASK = {
    "id": 1,
    "title": "Add the guard",
    "description": "Add the guard.",
    "acceptance_criteria": ["The guard works."],
}
_BLANK_PROSE_CASES: list[tuple[type[ModelBase], dict[str, object]]] = [
    (TriageResult, {"unknowns": [" "]}),
    (TriageResult, {"dependencies": [""]}),
    (Specification, {"problem": " "}),
    (Specification, {"acceptance_criteria": ["\t"]}),
    (Specification, {"risk_flags": [" "]}),
    (PlanStep, {"goal": " "}),
    (PlanStep, {"validation": [" "]}),
    (ExecutionPlan, {"summary": " "}),
    (ExecutionPlan, {"test_strategy": [" "]}),
    (ExecutionPlan, {"unresolved_decisions": [" "]}),
    (ChangeSet, {"summary": "  "}),
    (TestReport, {"findings": [" "]}),
    (TestReport, {"suggested_tests": [" "]}),
    (ReviewReport, {"findings": [" "]}),
    (ReviewReport, {"suggested_changes": [" "]}),
    (
        ReviewReport,
        {
            "blocking_findings": [
                {"category": "CORRECTNESS", "message": " ", "locations": [_LOCATION]}
            ]
        },
    ),
    (
        ReviewReport,
        {
            "prior_finding_dispositions": [
                {"finding_id": "F1", "status": "RESOLVED", "rationale": " "}
            ]
        },
    ),
    (ProjectTask, {"title": " "}),
    (ProjectTask, {"acceptance_criteria": [" "]}),
    (ProjectPlan, {"summary": " "}),
    (ProjectPlan, {"delivery_approach": " "}),
]
_VALID_BASES: dict[type[ModelBase], dict[str, object]] = {
    TriageResult: {
        "factory_eligible": True,
        "complexity": "L1",
        "risk": "R1",
        "needs_research": False,
        "confidence": 0.9,
    },
    Specification: {"problem": "Fix it.", "confidence": 0.9},
    PlanStep: {"id": "s1", "goal": "Change it."},
    ExecutionPlan: {"summary": "Change it.", "expected_scope": _SCOPE},
    ChangeSet: {"summary": "Changed it."},
    TestReport: {"passed": True, "confidence": 0.9},
    ReviewReport: {"approved": True},
    ProjectTask: _TASK,
    ProjectPlan: {
        "project_id": "proj-1",
        "summary": "One task.",
        "delivery_approach": "One task.",
        "tasks": [_TASK],
    },
}


@pytest.mark.parametrize(("model", "override"), _BLANK_PROSE_CASES)
def test_blank_agent_prose_fails_model_validation(
    model: type[ModelBase], override: dict[str, object]
) -> None:
    payload = {**_VALID_BASES[model], **override}

    with pytest.raises(ValidationError, match="must not be blank"):
        model.model_validate(payload)


@pytest.mark.parametrize("model", list(_VALID_BASES))
def test_valid_agent_prose_still_validates(model: type[ModelBase]) -> None:
    assert model.model_validate(_VALID_BASES[model]) is not None


def test_non_blank_prose_keeps_its_original_whitespace() -> None:
    plan = ExecutionPlan.model_validate(
        {**_VALID_BASES[ExecutionPlan], "summary": "  Change it.  "}
    )

    assert plan.summary == "  Change it.  "


def test_work_item_drops_blank_criteria_and_constraints_but_keeps_the_rest() -> None:
    item = WorkItem(
        id="WI-1",
        title="Reject blanks",
        description="Reject blank names.",
        acceptance_criteria=["", "  Blank names fail.  ", "\t"],
        constraints=[" ", "Keep the API."],
    )

    assert item.acceptance_criteria == ["  Blank names fail.  "]
    assert item.constraints == ["Keep the API."]


def test_project_brief_drops_blank_criteria_and_constraints() -> None:
    brief = ProjectBrief(
        id="project-1",
        title="Validate",
        description="Validate names.",
        repository_path="/repo",
        acceptance_criteria=[" ", "Names are validated."],
        constraints=[""],
    )

    assert brief.acceptance_criteria == ["Names are validated."]
    assert brief.constraints == []


@pytest.mark.parametrize("field", ["title", "description"])
def test_work_item_rejects_a_blank_required_scalar(field: str) -> None:
    payload = {"id": "WI-1", "title": "Title", "description": "Description", field: "  "}

    with pytest.raises(ValidationError, match="must not be blank"):
        WorkItem.model_validate(payload)


@pytest.mark.parametrize("field", ["title", "description"])
def test_project_brief_rejects_a_blank_required_scalar(field: str) -> None:
    payload = {
        "id": "project-1",
        "title": "Title",
        "description": "Description",
        "repository_path": "/repo",
        field: "  ",
    }

    with pytest.raises(ValidationError, match="must not be blank"):
        ProjectBrief.model_validate(payload)


_DIGEST = "a" * 64


def _answers(count: int) -> list[PlanDecisionAnswer]:
    return [PlanDecisionAnswer(decision_number=n, answer="yes") for n in range(1, count + 1)]


def _plan_decision_context(count: int) -> PlanDecisionContext:
    return PlanDecisionContext(
        plan_fingerprint=_DIGEST,
        decisions=[f"Question {n}?" for n in range(1, count + 1)],
        context_fingerprint=_DIGEST,
    )


def _plan_decision_answers(count: int) -> PlanDecisionAnswers:
    return PlanDecisionAnswers(
        run_id="run-1",
        episode_id="episode-1",
        plan_fingerprint=_DIGEST,
        context_fingerprint=_DIGEST,
        comment_id=1,
        user_login="octocat",
        answers=_answers(count),
    )


def _dashboard_request(count: int) -> DashboardResumeRequest:
    return DashboardResumeRequest(
        run_id="run-1",
        episode_id="episode-1",
        context_fingerprint=_DIGEST,
        action="PLAN_DECISION",
        answers=_answers(count),
    )


def test_the_wire_limits_do_not_change() -> None:
    # Stored run files and the dashboard payload hold lists of these sizes. Changing a limit
    # makes an old file fail to load or a new file fail to read in an old dashboard.
    assert (MAX_PLAN_DECISIONS, MAX_GUIDANCE_FINDINGS) == (24, 12)


_PLAN_DECISION_BUILDERS: list[Callable[[int], Any]] = [
    _plan_decision_context,
    _plan_decision_answers,
    _dashboard_request,
]


@pytest.mark.parametrize("build", _PLAN_DECISION_BUILDERS)
def test_a_plan_decision_model_accepts_exactly_max_plan_decisions(
    build: Callable[[int], Any],
) -> None:
    assert build(MAX_PLAN_DECISIONS) is not None


@pytest.mark.parametrize("build", _PLAN_DECISION_BUILDERS)
def test_a_plan_decision_model_rejects_one_more_than_max_plan_decisions(
    build: Callable[[int], Any],
) -> None:
    with pytest.raises(ValidationError):
        build(MAX_PLAN_DECISIONS + 1)


def test_a_decision_number_above_max_plan_decisions_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanDecisionAnswer(decision_number=MAX_PLAN_DECISIONS + 1, answer="yes")


def test_the_last_decision_number_is_accepted() -> None:
    assert PlanDecisionAnswer(decision_number=MAX_PLAN_DECISIONS, answer="yes").answer == "yes"


def test_every_token_class_is_a_usage_metrics_field() -> None:
    assert set(TOKEN_CLASS_FIELDS) <= set(UsageMetrics.model_fields)


def test_every_cost_unit_but_the_derived_one_is_a_usage_metrics_field() -> None:
    assert set(COST_UNIT_FIELDS) - set(UsageMetrics.model_fields) == {"usage_value_usd"}


def test_the_total_leaves_out_only_the_reasoning_tokens() -> None:
    assert set(TOKEN_CLASS_FIELDS) - set(TOTAL_TOKEN_FIELDS) == {"reasoning_tokens"}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"source": RepositoryCommandsSource.NONE, "verify": ("x",)}, "without a source"),
        ({"source": RepositoryCommandsSource.DERIVED}, "derived plan"),
        (
            {"source": RepositoryCommandsSource.DERIVED, "verify": ("x",), "build": ("y",)},
            "derived plan",
        ),
        (
            {
                "source": RepositoryCommandsSource.CONFIG,
                "rejected": (RejectedCommand(command="x", reason="y"),),
            },
            "configured plan",
        ),
    ],
)
def test_plan_rejects_commands_that_contradict_its_source(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        RepositoryCommandsPlan.model_validate(fields)
