"""Golden tests: ``build_prompt`` output stays byte-identical for representative requests.

Each case renders one request and compares it with a file in
``tests/golden/prompts``. The JSON Schema and the top-level field list of the
output contract come from the artifact models, so they are masked. Everything
else, including section order, titles and separators, must match exactly.
``test_masked_model_text_is_the_models_own_schema_and_fields`` proves the two
masked parts equal what the models produce, so the mask hides nothing else.

Set ``UPDATE_PROMPT_GOLDENS=1`` to rewrite the files after a deliberate prompt
change. The run then fails on purpose, so a refresh never passes as a green
run. Review the diff, then run the tests again without the variable. The
refresh refuses to run when the ``CI`` variable is set.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from prompt_fixtures import (
    CHANGED_FILE,
    DIFF,
    FIXED_TIME,
    REPAIR_DIFF,
    change_set_correction_request,
    failing_test_report,
    first_implementer_request,
    first_review_request,
    make_request,
    plan,
    polish_request,
    re_review_request,
    repair_context,
    repository_profile,
    repository_skill,
    review_finding,
    seen_after,
    specification,
    triage,
    verification,
    verification_repair_request,
    with_output_rejection,
)

from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ExecutionPlan,
    ModelBase,
    ProjectBrief,
    ProjectPlan,
    RepositorySkill,
    ResearchReport,
    ReviewFindingCategory,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
)
from software_agent_factory.prompts import (
    build_continuation_prompt,
    build_prompt,
    build_prompt_sections,
)

GOLDEN_DIRECTORY = Path(__file__).parent / "golden" / "prompts"
UPDATE_ENVIRONMENT_VARIABLE = "UPDATE_PROMPT_GOLDENS"
CI_ENVIRONMENT_VARIABLE = "CI"

_SCHEMA_LINE = re.compile(r"(JSON Schema:\n)(\{.*\})")
_TOP_LEVEL_FIELDS = re.compile(r"Top-level fields: ([^.\n]*)\.")


def _mask_model_derived_text(prompt: str) -> str:
    masked = _SCHEMA_LINE.sub(r"\1<json schema>", prompt)
    return _TOP_LEVEL_FIELDS.sub("Top-level fields: <fields>.", masked)


def assert_matches_golden(name: str, text: str) -> None:
    """Compare ``text`` with ``tests/golden/prompts/<name>.txt``, or rewrite it on request."""
    golden = GOLDEN_DIRECTORY / f"{name}.txt"
    if os.environ.get(UPDATE_ENVIRONMENT_VARIABLE) == "1":
        if os.environ.get(CI_ENVIRONMENT_VARIABLE):
            pytest.fail(f"{UPDATE_ENVIRONMENT_VARIABLE} must not rewrite golden files in CI")
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(text, encoding="utf-8")
        pytest.fail(f"Rewrote {golden.name}. Review the diff, then run the tests without it.")

    assert text == golden.read_text(encoding="utf-8")


def _request(role: AgentRole, **overrides: object) -> AgentRequest:
    return make_request(role, **overrides)


def _reviewer_round(**overrides: object) -> AgentRequest:
    return _request(
        AgentRole.REVIEWER,
        specification=specification(),
        execution_plan=plan(),
        diff=DIFF,
        changed_files=[CHANGED_FILE],
        verification_report=verification(),
        test_report=failing_test_report(),
        attempt_number=3,
        **overrides,
    )


CASES: dict[str, Callable[[], AgentRequest]] = {
    "triage": lambda: _request(AgentRole.TRIAGE),
    "refiner": lambda: _request(AgentRole.REFINER, triage_result=triage()),
    "researcher": lambda: _request(
        AgentRole.RESEARCHER, triage_result=triage(), specification=specification()
    ),
    "planner_replan": lambda: _request(
        AgentRole.PLANNER,
        specification=specification(),
        research_report=ResearchReport(question="Which validator?", findings=["Use strip()."]),
        repair_context=repair_context(),
        diff=DIFF,
        changed_files=[CHANGED_FILE],
    ),
    "implementer_first": lambda: _request(
        AgentRole.IMPLEMENTER,
        specification=specification(),
        execution_plan=plan(),
        repository_skill=repository_skill(),
        workspace_path="/w",
        attempt_number=1,
    ),
    "implementer_repair": lambda: _request(
        AgentRole.IMPLEMENTER,
        specification=specification(),
        execution_plan=plan(),
        workspace_path="/w",
        attempt_number=2,
        diff=DIFF,
        repair_context=repair_context(),
    ),
    "tester": lambda: _request(
        AgentRole.TESTER,
        specification=specification(),
        execution_plan=plan(),
        repair_context="The previous output was not valid JSON.",
        diff=DIFF,
        changed_files=[CHANGED_FILE],
        verification_report=verification(),
    ),
    "reviewer_first": lambda: _reviewer_round(),
    "reviewer_rereview": lambda: _reviewer_round(
        prior_review_findings=[review_finding("review-1")],
        accepted_review_findings=[
            review_finding(
                "review-2", category=ReviewFindingCategory.COMPATIBILITY, path="src/b.py"
            )
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
        repair_context=repair_context(),
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
        repository_profile=repository_profile(),
        repair_context="The first plan packed too many outcomes into one task.",
    ),
    "generate_repository_skill": lambda: _request(
        AgentRole.RESEARCHER,
        purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
        repository_profile=repository_profile(),
        official_documentation_origins=["https://react.dev"],
        practice_reference_urls=["https://example.com/review.md"],
        repair_context="Practice sources must use the general scope.",
    ),
}


EXPECTED_MODELS: dict[str, type[ModelBase]] = {
    "triage": TriageResult,
    "refiner": Specification,
    "researcher": ResearchReport,
    "planner_replan": ExecutionPlan,
    "implementer_first": ChangeSet,
    "implementer_repair": ChangeSet,
    "tester": TestReport,
    "reviewer_first": ReviewReport,
    "reviewer_rereview": ReviewReport,
    "reviewer_rejection": ReviewReport,
    "correct_change_set": ChangeSet,
    "decompose_project": ProjectPlan,
    "generate_repository_skill": RepositorySkill,
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_build_prompt_matches_its_golden_file(case: str) -> None:
    assert_matches_golden(case, _mask_model_derived_text(build_prompt(CASES[case]())))


def test_every_case_names_its_expected_model() -> None:
    assert sorted(EXPECTED_MODELS) == sorted(CASES)


@pytest.mark.parametrize("case", sorted(CASES))
def test_masked_model_text_is_the_models_own_schema_and_fields(case: str) -> None:
    prompt = build_prompt(CASES[case]())
    model = EXPECTED_MODELS[case]

    schema_match = _SCHEMA_LINE.search(prompt)
    fields_match = _TOP_LEVEL_FIELDS.search(prompt)

    assert schema_match is not None
    assert fields_match is not None
    assert schema_match.group(2) == json.dumps(
        model.model_json_schema(), separators=(",", ":"), sort_keys=True
    )
    assert fields_match.group(1) == ", ".join(model.model_fields)
    assert "<json schema>" in _mask_model_derived_text(prompt)


@pytest.mark.parametrize("case", sorted(CASES))
def test_prompt_sections_cover_every_part_of_the_full_prompt(case: str) -> None:
    request = CASES[case]()

    sections = build_prompt_sections(request)

    assert "\n\n".join(section.text for section in sections).strip() == build_prompt(request)


@pytest.mark.parametrize("case", sorted(CASES))
def test_prompt_section_titles_are_unique(case: str) -> None:
    titles = [section.title for section in build_prompt_sections(CASES[case]())]

    assert sorted(titles) == sorted(set(titles))


#: Each case is the calls a session already received, then the call being continued.
CONTINUATION_CASES: dict[str, Callable[[], tuple[AgentRequest, ...]]] = {
    "continuation_implementer_repair": lambda: (
        first_implementer_request(),
        verification_repair_request(),
    ),
    "continuation_polish_with_repository_skill": lambda: (
        first_implementer_request(),
        polish_request(),
    ),
    "continuation_change_set_correction": lambda: (
        first_implementer_request(),
        change_set_correction_request(first_implementer_request()),
    ),
    "continuation_reviewer_rereview": lambda: (first_review_request(), re_review_request()),
    "continuation_reviewer_rejection_retry": lambda: (
        first_review_request(),
        with_output_rejection(first_review_request()),
    ),
}


def _continuation_text(case: str) -> str:
    *earlier, request = CONTINUATION_CASES[case]()
    continuation = build_continuation_prompt(request, seen_after(*earlier))
    assert continuation is not None
    return continuation.text


@pytest.mark.parametrize("case", sorted(CONTINUATION_CASES))
def test_build_continuation_prompt_matches_its_golden_file(case: str) -> None:
    assert_matches_golden(case, _mask_model_derived_text(_continuation_text(case)))
