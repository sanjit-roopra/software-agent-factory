"""Tests for software_agent_factory.agents.

Covers the typed AgentRequest/AgentResult boundary and the deterministic
FakeAgentRuntime default behaviors plus scripted-hook overrides used
throughout tests/test_workflow.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.models import (
    AgentRole,
    AttemptTrigger,
    ChangeSet,
    Complexity,
    ExecutionPlan,
    RepairContext,
    ResearchReport,
    ReviewReport,
    Risk,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)


def _work_item(**overrides: object) -> WorkItem:
    defaults: dict[str, object] = {
        "id": "WI-1",
        "title": "Reject empty customer names",
        "description": "Return HTTP 400 for empty or whitespace-only names.",
    }
    defaults.update(overrides)
    return WorkItem(**defaults)  # type: ignore[arg-type]


def test_agent_result_requires_failure_reason_when_not_success() -> None:
    with pytest.raises(ValueError, match="failure_reason"):
        AgentResult(role=AgentRole.TRIAGE, success=False)


def test_agent_result_allows_success_without_failure_reason() -> None:
    result = AgentResult(role=AgentRole.TRIAGE, success=True)
    assert result.failure_reason is None


def test_default_triage_is_eligible_l1_r1_no_research() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.TRIAGE,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert result.triage_result is not None
    assert result.triage_result.factory_eligible is True
    assert result.triage_result.complexity is Complexity.L1
    assert result.triage_result.risk is Risk.R1
    assert result.triage_result.needs_research is False


def test_default_refiner_produces_specification_from_work_item() -> None:
    runtime = FakeAgentRuntime()
    work_item = _work_item(acceptance_criteria=["Reject empty strings"])
    request = AgentRequest(
        role=AgentRole.REFINER,
        model="claude-opus-5",
        reasoning="high",
        work_item=work_item,
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert isinstance(result.specification, Specification)
    assert result.specification.problem == work_item.description
    assert result.specification.acceptance_criteria == ["Reject empty strings"]


def test_default_planner_produces_execution_plan() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.PLANNER,
        model="claude-opus-5",
        reasoning="high",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert isinstance(result.execution_plan, ExecutionPlan)
    assert len(result.execution_plan.steps) >= 1


def test_default_implementer_creates_new_file_inside_workspace_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        workspace_path=str(workspace),
        attempt_number=1,
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert result.change_set is not None
    created_files = list(workspace.iterdir())
    assert len(created_files) == 1
    assert created_files[0].name in result.change_set.changed_files
    assert "WI-1" in created_files[0].read_text()
    # Nothing was written outside the assigned workspace directory.
    assert list(tmp_path.iterdir()) == [workspace]


def test_default_implementer_requires_workspace_path() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        attempt_number=1,
        timeout_seconds=60,
    )

    with pytest.raises(ValueError, match="workspace_path"):
        runtime.run(request)


def test_default_reviewer_approves() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.REVIEWER,
        model="gpt-5.6-sol",
        reasoning="high",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert isinstance(result.review_report, ReviewReport)
    assert result.review_report.approved is True


def test_default_tester_returns_independent_test_report(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.TESTER,
        model="claude-sonnet-5",
        reasoning="high",
        work_item=_work_item(),
        diff="diff --git a/src/app.py b/src/app.py\n",
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert isinstance(result.test_report, TestReport)
    assert result.test_report.passed is True
    # Deterministic evidence stays a separate artifact: the tester never
    # produces a VerificationReport.
    assert result.verification_report is None


def test_default_researcher_returns_research_report() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.RESEARCHER,
        model="gpt-5.6-sol",
        reasoning="high",
        work_item=_work_item(),
        specification=Specification(problem="How does validation work?", confidence=0.5),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is True
    assert isinstance(result.research_report, ResearchReport)
    assert result.research_report.question == "How does validation work?"


def test_researcher_hook_overrides_default() -> None:
    def scripted_researcher(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.RESEARCHER,
            success=False,
            failure_reason="research unavailable",
        )

    runtime = FakeAgentRuntime(researcher=scripted_researcher)
    request = AgentRequest(
        role=AgentRole.RESEARCHER,
        model="gpt-5.6-sol",
        reasoning="high",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "research unavailable"


def test_reviewer_consumes_test_report_and_diff_contract() -> None:
    runtime = FakeAgentRuntime()
    request = AgentRequest(
        role=AgentRole.REVIEWER,
        model="gpt-5.6-sol",
        reasoning="high",
        work_item=_work_item(),
        diff="diff --git a/src/app.py b/src/app.py\n",
        verification_report=VerificationReport(passed=True, confidence=1.0),
        test_report=TestReport(
            passed=False,
            findings=["Whitespace-only names are still accepted."],
            suggested_tests=["Add a whitespace-only regression test."],
            confidence=0.6,
        ),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.review_report is not None
    assert result.review_report.approved is False
    assert result.review_report.findings == ["Whitespace-only names are still accepted."]
    assert result.review_report.suggested_changes == [
        "Add a whitespace-only regression test."
    ]


def test_repair_context_accepts_typed_model_or_plain_text() -> None:
    typed_request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        workspace_path="/tmp-not-used",
        attempt_number=2,
        repair_context=RepairContext(
            trigger=AttemptTrigger.CI,
            summary="CI test job failed",
            failures=["tests/test_app.py::test_reject_empty failed"],
            log_excerpt="AssertionError",
        ),
        timeout_seconds=60,
    )
    text_request = typed_request.model_copy(update={"repair_context": "verification failed"})

    assert isinstance(typed_request.repair_context, RepairContext)
    assert typed_request.repair_context.trigger is AttemptTrigger.CI
    assert text_request.repair_context == "verification failed"
    assert AgentRequest.model_validate_json(typed_request.model_dump_json()) == typed_request


def test_triage_hook_overrides_default_for_scripted_tests() -> None:
    def scripted_triage(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=False,
                complexity=Complexity.L2,
                risk=Risk.R2,
                requirements_quality="vague",
                needs_research=True,
                dependencies=[],
                unknowns=["unclear scope"],
                confidence=0.2,
            ),
        )

    runtime = FakeAgentRuntime(triage=scripted_triage)
    request = AgentRequest(
        role=AgentRole.TRIAGE,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.triage_result is not None
    assert result.triage_result.factory_eligible is False
    assert result.triage_result.risk is Risk.R2
    assert result.triage_result.needs_research is True


def test_implementer_hook_can_script_failure_then_success() -> None:
    def scripted_implementer(request: AgentRequest) -> AgentResult:
        assert request.attempt_number is not None
        if request.attempt_number < 3:
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                failure_reason=f"scripted failure on attempt {request.attempt_number}",
            )
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="finally succeeded", changed_files=["ignored.txt"]),
        )

    runtime = FakeAgentRuntime(implementer=scripted_implementer)

    for attempt in (1, 2):
        request = AgentRequest(
            role=AgentRole.IMPLEMENTER,
            model="claude-sonnet-5",
            reasoning="medium",
            work_item=_work_item(),
            attempt_number=attempt,
            timeout_seconds=60,
        )
        result = runtime.run(request)
        assert result.success is False
        assert result.failure_reason is not None

    request = AgentRequest(
        role=AgentRole.IMPLEMENTER,
        model="claude-sonnet-5",
        reasoning="medium",
        work_item=_work_item(),
        attempt_number=3,
        timeout_seconds=60,
    )
    result = runtime.run(request)
    assert result.success is True


def test_reviewer_hook_can_reject() -> None:
    def rejecting_reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=False, findings=["needs more tests"]),
        )

    runtime = FakeAgentRuntime(reviewer=rejecting_reviewer)
    request = AgentRequest(
        role=AgentRole.REVIEWER,
        model="gpt-5.6-sol",
        reasoning="high",
        work_item=_work_item(),
        timeout_seconds=60,
    )

    result = runtime.run(request)

    assert result.review_report is not None
    assert result.review_report.approved is False
