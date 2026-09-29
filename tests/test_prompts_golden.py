"""Golden tests: ``build_prompt`` output stays byte-identical for representative requests.

Each case renders one request and compares it with a file in
``tests/golden/prompts``. The JSON Schema and the top-level field list of the
output contract come from the artifact models, so they are masked. Everything
else, including section order, titles and separators, must match exactly.

Set ``UPDATE_PROMPT_GOLDENS=1`` to rewrite the files after a deliberate prompt
change. Review the diff before you commit it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ProjectBrief,
    RepairContext,
    RepositoryProfile,
    RepositorySkill,
    ResearchReport,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingOrigin,
    ReviewSourceLocation,
    SkillGuidance,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)
from software_agent_factory.prompts import build_prompt

GOLDEN_DIRECTORY = Path(__file__).parent / "golden" / "prompts"
UPDATE_ENVIRONMENT_VARIABLE = "UPDATE_PROMPT_GOLDENS"

CHANGED_FILE = "src/app.py"
DIFF = "diff --git a/src/app.py b/src/app.py\n+    if not name.strip():\n"
REPAIR_DIFF = "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

_SCHEMA_LINE = re.compile(r"(JSON Schema:\n)\{.*\}")
_TOP_LEVEL_FIELDS = re.compile(r"Top-level fields: [^.\n]*\.")


def _mask_model_derived_text(prompt: str) -> str:
    masked = _SCHEMA_LINE.sub(r"\1<json schema>", prompt)
    return _TOP_LEVEL_FIELDS.sub("Top-level fields: <fields>.", masked)


def _work_item() -> WorkItem:
    return WorkItem(
        id="WI-1",
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
        acceptance_criteria=["Blank names return HTTP 400."],
        constraints=["Do not change the public API."],
        created_at=FIXED_TIME,
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
    return AgentRequest(**payload)


def _specification() -> Specification:
    return Specification(problem="Names must not be blank.", confidence=0.9)


def _triage() -> TriageResult:
    return TriageResult(
        factory_eligible=True,
        complexity="L2",
        risk="R1",
        requirements_quality="vague",
        needs_research=True,
        confidence=0.4,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="Add a guard clause.",
        steps=[PlanStep(id="s1", goal="Validate the name")],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
    )


def _verification() -> VerificationReport:
    return VerificationReport(
        passed=True,
        deterministic_checks=[
            CommandResult(command="pytest -q", exit_code=0, duration_seconds=1.0, stdout="1 passed")
        ],
        confidence=1.0,
    )


def _repair_context() -> RepairContext:
    return RepairContext(
        trigger=AttemptTrigger.CI,
        summary="Continuous integration reported a failing check.",
        failures=["unit-tests: TEST_FAILURE"],
        log_excerpt="AssertionError: expected 400",
    )


def _finding(finding_id: str, category: ReviewFindingCategory, path: str) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        category=category,
        message=f"Finding {finding_id}.",
        locations=[ReviewSourceLocation(path=path, start_line=1, end_line=2)],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )


def _skill() -> RepositorySkill:
    return RepositorySkill(
        dependency_fingerprint="a" * 64,
        generated_at=FIXED_TIME,
        simplify=SkillGuidance(summary="Simplify.", guidance=("Drop dead code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Prefer modern APIs.",)),
        uncertainties=("Fixture skill has no external sources.",),
    )


def _profile() -> RepositoryProfile:
    return RepositoryProfile(manifest_fingerprint="a" * 64, dependency_fingerprint="b" * 64)


def _reviewer_round(**overrides: object) -> AgentRequest:
    return _request(
        AgentRole.REVIEWER,
        specification=_specification(),
        execution_plan=_plan(),
        diff=DIFF,
        changed_files=[CHANGED_FILE],
        verification_report=_verification(),
        test_report=TestReport(passed=False, findings=["Whitespace is accepted."], confidence=0.5),
        attempt_number=3,
        **overrides,
    )


CASES: dict[str, Callable[[], AgentRequest]] = {
    "triage": lambda: _request(AgentRole.TRIAGE),
    "refiner": lambda: _request(AgentRole.REFINER, triage_result=_triage()),
    "researcher": lambda: _request(
        AgentRole.RESEARCHER, triage_result=_triage(), specification=_specification()
    ),
    "planner_replan": lambda: _request(
        AgentRole.PLANNER,
        specification=_specification(),
        research_report=ResearchReport(question="Which validator?", findings=["Use strip()."]),
        repair_context=_repair_context(),
        diff=DIFF,
        changed_files=[CHANGED_FILE],
    ),
    "implementer_first": lambda: _request(
        AgentRole.IMPLEMENTER,
        specification=_specification(),
        execution_plan=_plan(),
        repository_skill=_skill(),
        workspace_path="/w",
        attempt_number=1,
    ),
    "implementer_repair": lambda: _request(
        AgentRole.IMPLEMENTER,
        specification=_specification(),
        execution_plan=_plan(),
        workspace_path="/w",
        attempt_number=2,
        diff=DIFF,
        repair_context=_repair_context(),
    ),
    "tester": lambda: _request(
        AgentRole.TESTER,
        specification=_specification(),
        execution_plan=_plan(),
        repair_context="The previous output was not valid JSON.",
        diff=DIFF,
        changed_files=[CHANGED_FILE],
        verification_report=_verification(),
    ),
    "reviewer_first": lambda: _reviewer_round(),
    "reviewer_rereview": lambda: _reviewer_round(
        prior_review_findings=[_finding("review-1", ReviewFindingCategory.CORRECTNESS, "src/a.py")],
        accepted_review_findings=[
            _finding("review-2", ReviewFindingCategory.COMPATIBILITY, "src/b.py")
        ],
        repair_diff=REPAIR_DIFF,
    ),
    "reviewer_rejection": lambda: _reviewer_round(
        repair_context="The previous output listed no disposition."
    ),
    "correct_change_set": lambda: _request(
        AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.CORRECT_CHANGE_SET,
        change_set=ChangeSet(summary="Fix output shape"),
        workspace_path="/w",
        repair_context=_repair_context(),
    ),
    "decompose_project": lambda: _request(
        AgentRole.PLANNER,
        purpose=AgentPurpose.DECOMPOSE_PROJECT,
        project_brief=ProjectBrief(
            id="project-1",
            title="Build customer validation",
            description="Reject blank customer names.",
            repository_path="/repo",
            created_at=FIXED_TIME,
        ),
        repository_profile=_profile(),
        repair_context="The first plan packed too many outcomes into one task.",
    ),
    "generate_repository_skill": lambda: _request(
        AgentRole.RESEARCHER,
        purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
        repository_profile=_profile(),
        official_documentation_origins=["https://react.dev"],
        practice_reference_urls=["https://example.com/review.md"],
        repair_context="Practice sources must use the general scope.",
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_build_prompt_matches_its_golden_file(case: str) -> None:
    prompt = _mask_model_derived_text(build_prompt(CASES[case]()))
    golden = GOLDEN_DIRECTORY / f"{case}.txt"
    if os.environ.get(UPDATE_ENVIRONMENT_VARIABLE):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(prompt, encoding="utf-8")

    assert prompt == golden.read_text(encoding="utf-8")
