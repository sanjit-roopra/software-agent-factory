from __future__ import annotations

import hashlib
from pathlib import Path

from software_agent_factory.agents import AgentResult, is_retryable_typed_artifact_failure
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ResearchReport,
    TriageResult,
)
from software_agent_factory.writing_policy import (
    SIMPLE_ENGLISH_REVISION,
    apply_agent_result_writing_policy,
    validate_artifact_writing,
    validate_publication_text,
)

ROOT = Path(__file__).resolve().parents[1]


def test_policy_uses_the_pinned_simpleenglish_revision_and_data() -> None:
    slop = ROOT / "src/software_agent_factory/_vendor/simple_english/slop.tsv"
    license_path = ROOT / "src/software_agent_factory/_vendor/simple_english/LICENSE"

    assert SIMPLE_ENGLISH_REVISION == "61ee200efbd423050aab982eed94226229891ae0"
    assert hashlib.sha256(slop.read_bytes()).hexdigest() == (
        "1e65bf53d00c1495781a70c562e91296b3481c6bc0b37feb2cc6ea275b77e4dc"
    )
    assert "MIT License" in license_path.read_text(encoding="utf-8")


def test_policy_detects_long_sentences_and_filler() -> None:
    text = (
        "This robust and comprehensive implementation uses many unnecessary words "
        "to explain one small fact to the reader and then continues with more words "
        "that do not add useful information; it is crucial — e.g. this phrase."
    )

    findings = validate_publication_text("text", text, max_words=100)

    assert any("sentence_over_limit" in finding for finding in findings)
    assert any("slop_word" in finding for finding in findings)
    assert any("semicolon" in finding for finding in findings)
    assert any("em_dash" in finding for finding in findings)
    assert any("latin_abbrev" in finding for finding in findings)


def test_policy_preserves_uncertainty_modals() -> None:
    report = ResearchReport(
        question="Which behavior applies?",
        findings=["The API may return an empty result."],
        uncertainty=["The dependency might change this behavior."],
    )

    assert validate_artifact_writing(report) == ()


def test_agent_result_writing_failure_is_retryable() -> None:
    plan = ExecutionPlan(
        summary="Use a robust and comprehensive implementation.",
        steps=[PlanStep(id="one", goal="Change the parser.")],
        expected_scope=ExpectedScope(
            modules=["src"],
            estimated_files_min=1,
            estimated_files_max=1,
        ),
    )
    result = AgentResult(
        role=AgentRole.PLANNER,
        success=True,
        execution_plan=plan,
    )

    checked = apply_agent_result_writing_policy(result, AgentPurpose.STANDARD)

    assert checked.success is False
    assert checked.failure_reason is not None
    assert "ExecutionPlan did not satisfy writing policy" in checked.failure_reason
    assert is_retryable_typed_artifact_failure(checked, ExecutionPlan)
    assert checked.execution_plan == plan


def test_artifact_word_budget_limits_total_filler() -> None:
    plan = ExecutionPlan(
        summary=" ".join(["word"] * 26),
        steps=[PlanStep(id="one", goal="Change the parser.")],
        expected_scope=ExpectedScope(
            modules=["src"],
            estimated_files_min=1,
            estimated_files_max=1,
        ),
    )

    findings = validate_artifact_writing(plan)

    assert "summary has 26 words. The limit is 25." in findings


def test_policy_preserves_exact_technical_text() -> None:
    protected_spans = (
        "`robust; output — e.g. unchanged`",
        '"robust; output — e.g. unchanged"',
        "https://example.com/robust;e.g.",
        "\n    robust; output — e.g. unchanged",
        "<!-- project=e.g. robust -->",
    )

    for protected in protected_spans:
        text = f"One two three four five. {protected}"
        assert validate_publication_text("text", text, max_words=5) == ()


def test_policy_rejects_empty_publication_text() -> None:
    assert validate_publication_text("title", "  ", max_words=5) == ("title is empty.",)


def test_dependency_identifiers_are_not_linted_as_prose() -> None:
    result = TriageResult(
        factory_eligible=True,
        complexity="L1",
        risk="R1",
        requirements_quality="Requirements are clear.",
        needs_research=False,
        confidence=0.9,
        dependencies=["delve"],
    )

    assert validate_artifact_writing(result) == ()


def test_paragraph_boundaries_end_sentences() -> None:
    sentence = " ".join(["word"] * 25)

    assert (
        validate_publication_text(
            "body",
            f"{sentence}\n\n{sentence}",
            max_words=50,
        )
        == ()
    )
