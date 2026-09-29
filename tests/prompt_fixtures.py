"""Shared, non-collected request fixtures for the prompt and pi runtime tests.

Deliberately named without a ``test_`` prefix so pytest imports it as a plain
module rather than collecting it. The golden files pin the exact values of
these fixtures, so change a value here only together with the golden files.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from software_agent_factory.prompts import (
    build_continuation_prompt,
    build_prompt_sections,
    section_hashes,
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


# ---------------------------------------------------------------------------
# Requests shaped like the ones workflow.py builds for the roles that keep a
# session: ``_invoke_implementer`` for the implementer, ``_run_reviewer`` for the
# reviewer, each round on the same work item.
# ---------------------------------------------------------------------------

RESEARCH_QUESTION = "Which validator rejects blank names?"
SKILL_GUIDANCE = "Prefer str.strip() over manual loops."
POLISH_SUMMARY = (
    "Deterministic verification passed. Apply a final bounded polish and simplification "
    "pass using the reusable repository guidance supplied with this request."
)
VERIFICATION_FAILURE = "unit-tests: TEST_FAILURE"
VERIFICATION_LOG_EXCERPT = "AssertionError: expected HTTP 400"
FIRST_REVIEW_TESTER_FINDING = "Whitespace slips through validation."
RE_REVIEW_TESTER_FINDING = "Tabs still slip through validation."
PRIOR_FINDING_MESSAGE = "A blank name skips normalization."
ACCEPTED_FINDING_MESSAGE = "A legacy response stays accepted as review debt."
REPAIRED_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n+    if not name.strip() or name.isspace():\n"
)
DEBT_DIFF = "diff --git a/src/app.py b/src/app.py\n+    return name.strip()\n"
OUTPUT_REJECTION = (
    "Your previous response failed deterministic schema validation.\n"
    "1 validation error for ReviewReport\n"
    "Correct only the output shape. Return one complete ReviewReport JSON object. "
    "Do not add markdown or text outside the JSON."
)

#: What only the first call of a session carries.
BRIEF_TEXT = ("Reject empty customer names", "Return HTTP 400 for empty or whitespace-only names.")
OPENING_TEXT = ("You are the Software Agent Factory", "Use concise technical English")
FIRST_CALL_TEXT = (*BRIEF_TEXT, SPECIFICATION_PROBLEM, PLAN_SUMMARY, *OPENING_TEXT)


def _payload(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    return {**base, **overrides}


def first_implementer_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The first implementation attempt: no repair, no diff, no repository skill yet."""
    return make_request(
        AgentRole.IMPLEMENTER,
        **_payload(
            {
                "work_item": work_item(work_item_id),
                "specification": specification(),
                "research_report": ResearchReport(question=RESEARCH_QUESTION),
                "execution_plan": plan(),
                "workspace_path": "/w",
                "attempt_number": 1,
            },
            overrides,
        ),
    )


def verification_repair_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The attempt after a failed deterministic check."""
    return first_implementer_request(
        work_item_id,
        **_payload(
            {
                "attempt_number": 2,
                "diff": DIFF,
                "changed_files": [CHANGED_FILE],
                "repair_context": repair_context(
                    AttemptTrigger.VERIFICATION,
                    "Deterministic verify failed (test failure).",
                    [VERIFICATION_FAILURE],
                    VERIFICATION_LOG_EXCERPT,
                ),
            },
            overrides,
        ),
    )


def polish_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The bounded polish attempt: the repository skill exists now, and the diff is green."""
    return first_implementer_request(
        work_item_id,
        **_payload(
            {
                "attempt_number": 2,
                "repository_skill": repository_skill(SKILL_GUIDANCE),
                "diff": DIFF,
                "changed_files": [CHANGED_FILE],
                "repair_context": repair_context(
                    AttemptTrigger.POLISH, POLISH_SUMMARY, [], log_excerpt=None
                ),
            },
            overrides,
        ),
    )


def change_set_correction_request(
    base: AgentRequest, summary: str = "Fix the output shape."
) -> AgentRequest:
    """A prose-only ChangeSet correction for ``base``.

    The controller no longer starts one. The runtimes still support the purpose.
    """
    return base.model_copy(
        update={
            "purpose": AgentPurpose.CORRECT_CHANGE_SET,
            "change_set": ChangeSet(summary=summary),
            "diff": None,
            "changed_files": [CHANGED_FILE],
            "repair_context": repair_context(
                AttemptTrigger.IMPLEMENTER_FAILURE,
                "The implementation is not rejected. Correct only the ChangeSet prose.",
                ["The ChangeSet summary must describe the diff."],
                log_excerpt=None,
            ),
        }
    )


def first_review_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The first review round: tester report and verification, no findings yet."""
    return make_request(
        AgentRole.REVIEWER,
        **_payload(
            {
                "work_item": work_item(work_item_id),
                "specification": specification(),
                "execution_plan": plan(),
                "diff": DIFF,
                "changed_files": [CHANGED_FILE],
                "verification_report": verification(),
                "test_report": failing_test_report(FIRST_REVIEW_TESTER_FINDING),
                "workspace_path": "/w",
                "attempt_number": 1,
            },
            overrides,
        ),
    )


def re_review_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The round after a repair: prior findings, a new snapshot and fresh evidence."""
    return first_review_request(
        work_item_id,
        **_payload(
            {
                "attempt_number": 2,
                "diff": REPAIRED_DIFF,
                "repair_diff": REPAIR_DIFF,
                "verification_report": verification(stdout="2 passed"),
                "test_report": failing_test_report(RE_REVIEW_TESTER_FINDING),
                "prior_review_findings": [
                    review_finding("review-1", message=PRIOR_FINDING_MESSAGE, path=CHANGED_FILE)
                ],
            },
            overrides,
        ),
    )


def accepted_debt_review_request(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    """The round after a review acceptance: accepted debt, no open finding, no repair diff."""
    return first_review_request(
        work_item_id,
        **_payload(
            {
                "attempt_number": 3,
                "diff": DEBT_DIFF,
                "verification_report": verification(stdout="3 passed"),
                "accepted_review_findings": [
                    review_finding(
                        "review-2",
                        message=ACCEPTED_FINDING_MESSAGE,
                        category=ReviewFindingCategory.COMPATIBILITY,
                        path=CHANGED_FILE,
                    )
                ],
            },
            overrides,
        ),
    )


def with_output_rejection(request: AgentRequest, reason: str = OUTPUT_REJECTION) -> AgentRequest:
    """The retry inside one round: the same request plus why the last output was rejected."""
    return request.model_copy(update={"repair_context": reason})


def seen_after(*requests: AgentRequest) -> dict[str, str]:
    """What a session holds after ``requests``: the first sent in full, the others continued."""
    seen = section_hashes(build_prompt_sections(requests[0]))
    for request in requests[1:]:
        continuation = build_continuation_prompt(request, seen)
        assert continuation is not None
        seen = dict(continuation.sections_seen)
    return seen
