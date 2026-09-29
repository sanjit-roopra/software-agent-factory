"""Shared, non-collected request fixtures for the prompt and pi runtime tests.

Deliberately named without a ``test_`` prefix so pytest imports it as a plain
module rather than collecting it. The golden files pin the exact values of
these fixtures, so change a value here only together with the golden files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentRole,
    AttemptTrigger,
    CommandResult,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    RepairContext,
    RepositoryProfile,
    RepositorySkill,
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

FIXED_TIME = datetime(2026, 1, 1, tzinfo=UTC)

CHANGED_FILE = "src/app.py"
DIFF = "diff --git a/src/app.py b/src/app.py\n+    if not name.strip():\n"
REPAIR_DIFF = "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"

SPECIFICATION_PROBLEM = "Names must not be blank."
PLAN_SUMMARY = "Add a guard clause."


def work_item(work_item_id: str = "WI-1") -> WorkItem:
    return WorkItem(
        id=work_item_id,
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
        acceptance_criteria=["Blank names return HTTP 400."],
        constraints=["Do not change the public API."],
        created_at=FIXED_TIME,
    )


def make_request(role: AgentRole, **overrides: object) -> AgentRequest:
    payload: dict[str, object] = {
        "role": role,
        "model": "claude-sonnet-5",
        "reasoning": "high",
        "work_item": work_item(),
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return AgentRequest(**payload)


def specification() -> Specification:
    return Specification(problem=SPECIFICATION_PROBLEM, confidence=0.9)


def triage() -> TriageResult:
    return TriageResult(
        factory_eligible=True,
        complexity="L2",
        risk="R1",
        requirements_quality="vague",
        needs_research=True,
        confidence=0.4,
    )


def plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary=PLAN_SUMMARY,
        steps=[PlanStep(id="s1", goal="Validate the name")],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
    )


def verification(*, stdout: str = "1 passed") -> VerificationReport:
    return VerificationReport(
        passed=True,
        deterministic_checks=[
            CommandResult(command="pytest -q", exit_code=0, duration_seconds=1.0, stdout=stdout)
        ],
        confidence=1.0,
    )


def failing_test_report(finding: str = "Whitespace is accepted.") -> TestReport:
    return TestReport(passed=False, findings=[finding], confidence=0.5)


def repair_context(
    trigger: AttemptTrigger = AttemptTrigger.CI,
    summary: str = "Continuous integration reported a failing check.",
    failures: list[str] | None = None,
    log_excerpt: str | None = "AssertionError: expected 400",
) -> RepairContext:
    return RepairContext(
        trigger=trigger,
        summary=summary,
        failures=["unit-tests: TEST_FAILURE"] if failures is None else failures,
        log_excerpt=log_excerpt,
    )


def review_finding(
    finding_id: str,
    *,
    message: str | None = None,
    category: ReviewFindingCategory = ReviewFindingCategory.CORRECTNESS,
    path: str = "src/a.py",
) -> ReviewFinding:
    return ReviewFinding(
        id=finding_id,
        category=category,
        message=message or f"Finding {finding_id}.",
        locations=[ReviewSourceLocation(path=path, start_line=1, end_line=2)],
        origin=ReviewFindingOrigin.INITIAL,
        first_seen_snapshot=1,
    )


def repository_skill(
    guidance: str = "Prefer modern APIs.", fingerprint: str = "a" * 64
) -> RepositorySkill:
    return RepositorySkill(
        dependency_fingerprint=fingerprint,
        generated_at=FIXED_TIME,
        simplify=SkillGuidance(summary="Simplify.", guidance=("Drop dead code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=(guidance,)),
        uncertainties=("Fixture skill has no external sources.",),
    )


def repository_profile() -> RepositoryProfile:
    return RepositoryProfile(manifest_fingerprint="a" * 64, dependency_fingerprint="b" * 64)
