"""Tests for role-scoped prompt construction.

These assert the *contracts* the architecture depends on: which artifact model
each role must return, and which inputs each role is (and is not) allowed to
see. Nothing here calls a model.
"""

from __future__ import annotations

import pytest

from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentRole,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    RepairContext,
    ResearchReport,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)
from software_agent_factory.prompts import (
    MAX_DIFF_CHARS,
    artifact_model_for_role,
    build_prompt,
    normalize_role,
)

DIFF = "diff --git a/src/app.py b/src/app.py\n+    if not name.strip():\n"


def _work_item() -> WorkItem:
    return WorkItem(
        id="WI-1",
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
    )


def _specification() -> Specification:
    return Specification(problem="Names must not be blank.", confidence=0.9)


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="Add a guard clause.",
        steps=[PlanStep(id="s1", goal="Validate the name")],
        expected_scope=ExpectedScope(estimated_files_min=1, estimated_files_max=2),
    )


def _verification() -> VerificationReport:
    return VerificationReport(
        passed=True,
        deterministic_checks=[
            CommandResult(command="pytest -q", exit_code=0, duration_seconds=1.0)
        ],
        confidence=1.0,
    )


def _request(role: AgentRole, **overrides: object) -> AgentRequest:
    payload: dict[str, object] = {
        "role": role,
        "model": "claude-sonnet-5",
        "reasoning": "high",
        "work_item": _work_item(),
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return AgentRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Artifact contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (AgentRole.TRIAGE, TriageResult),
        (AgentRole.REFINER, Specification),
        (AgentRole.RESEARCHER, ResearchReport),
        (AgentRole.PLANNER, ExecutionPlan),
        (AgentRole.IMPLEMENTER, ChangeSet),
        (AgentRole.TESTER, TestReport),
        (AgentRole.REVIEWER, ReviewReport),
    ],
)
def test_every_role_maps_to_its_artifact_model(role: AgentRole, expected: type[object]) -> None:
    assert artifact_model_for_role(role) is expected


def test_tester_returns_a_test_report_not_a_verification_report() -> None:
    """Deterministic evidence is factory-produced; a model never emits it."""
    assert artifact_model_for_role(AgentRole.TESTER) is TestReport
    assert artifact_model_for_role(AgentRole.TESTER) is not VerificationReport


def test_researcher_role_enum_is_supported_directly() -> None:
    assert normalize_role(AgentRole.RESEARCHER) == "RESEARCHER"
    assert artifact_model_for_role(AgentRole.RESEARCHER) is ResearchReport
    assert artifact_model_for_role("researcher") is ResearchReport


def test_unsupported_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported agent role"):
        artifact_model_for_role("DEPLOYER")
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_role("   ")


# ---------------------------------------------------------------------------
# Role input contracts
# ---------------------------------------------------------------------------


def test_researcher_prompt_includes_the_specification_and_triage_result() -> None:
    prompt = build_prompt(
        _request(
            AgentRole.RESEARCHER,
            specification=_specification(),
            triage_result=TriageResult(
                factory_eligible=True,
                complexity="L2",
                risk="R1",
                requirements_quality="vague",
                needs_research=True,
                confidence=0.4,
            ),
        )
    )

    assert "RESEARCHER agent" in prompt
    assert "ResearchReport" in prompt
    assert "Names must not be blank." in prompt
    assert "Triage result" in prompt


def test_tester_prompt_carries_diff_changed_files_and_deterministic_results() -> None:
    prompt = build_prompt(
        _request(
            AgentRole.TESTER,
            specification=_specification(),
            execution_plan=_plan(),
            diff=DIFF,
            changed_files=["src/app.py"],
            verification_report=_verification(),
        )
    )

    assert "TestReport" in prompt
    assert "Changed files" in prompt
    assert "src/app.py" in prompt
    assert DIFF.strip() in prompt
    assert "Deterministic verification" in prompt
    assert "pytest -q" in prompt
    assert "No implementer self-assessment is provided" in prompt


def test_reviewer_prompt_carries_the_tester_report_and_never_a_change_set() -> None:
    prompt = build_prompt(
        _request(
            AgentRole.REVIEWER,
            specification=_specification(),
            execution_plan=_plan(),
            diff=DIFF,
            changed_files=["src/app.py"],
            verification_report=_verification(),
            test_report=TestReport(
                passed=False, findings=["Whitespace is still accepted."], confidence=0.5
            ),
            change_set=ChangeSet(summary="I did a great job and everything works."),
        )
    )

    assert "ReviewReport" in prompt
    assert "Independent tester report" in prompt
    assert "Whitespace is still accepted." in prompt
    # The implementer's self-justification never reaches an independent gate.
    assert "I did a great job" not in prompt


def test_implementer_prompt_carries_repair_context_and_current_diff() -> None:
    prompt = build_prompt(
        _request(
            AgentRole.IMPLEMENTER,
            specification=_specification(),
            execution_plan=_plan(),
            workspace_path="/tmp-not-used",
            attempt_number=3,
            diff=DIFF,
            repair_context=RepairContext(
                trigger=AttemptTrigger.CI,
                summary="Continuous integration reported a failing check.",
                failures=["unit-tests: TEST_FAILURE"],
                log_excerpt="AssertionError: expected 400",
            ),
        )
    )

    assert "ChangeSet" in prompt
    assert "Repair context" in prompt
    assert "unit-tests: TEST_FAILURE" in prompt
    assert "AssertionError: expected 400" in prompt
    assert "Attempt number" in prompt
    assert DIFF.strip() in prompt


def test_triage_and_refiner_prompts_stay_minimal() -> None:
    triage = build_prompt(_request(AgentRole.TRIAGE, diff=DIFF, changed_files=["a.py"]))
    assert "TriageResult" in triage
    assert DIFF.strip() not in triage

    refiner = build_prompt(_request(AgentRole.REFINER, diff=DIFF))
    assert "Specification" in refiner
    assert DIFF.strip() not in refiner


def test_diff_is_bounded_in_prompts() -> None:
    huge = "x" * (MAX_DIFF_CHARS + 500)
    prompt = build_prompt(_request(AgentRole.TESTER, diff=huge, changed_files=["a.py"]))

    assert "truncated 500 characters" in prompt
    assert len(prompt) < len(huge) + 5000
