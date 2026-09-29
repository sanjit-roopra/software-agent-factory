from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from software_agent_factory._vendor.simple_english.lint import lint
from software_agent_factory.agents import AgentResult
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
    check_publication_text,
    result_writing_findings,
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


def _wordy_plan() -> ExecutionPlan:
    return ExecutionPlan(
        summary="Use a robust and comprehensive implementation.",
        steps=[PlanStep(id="one", goal="Change the parser.")],
        expected_scope=ExpectedScope(
            modules=["src"],
            estimated_files_min=1,
            estimated_files_max=1,
        ),
    )


def test_agent_result_writing_findings_are_returned_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = AgentResult(role=AgentRole.PLANNER, success=True, execution_plan=_wordy_plan())

    with caplog.at_level(logging.WARNING, logger="software_agent_factory.writing_policy"):
        findings = result_writing_findings(result, AgentPurpose.STANDARD, source="run RUN-1")

    assert "summary has 2 slop_word finding(s)." in findings
    assert "source=run RUN-1" in caplog.text
    assert "artifact=ExecutionPlan" in caplog.text
    assert result.success is True
    assert result.failure_reason is None


def test_clean_or_failed_results_have_no_writing_findings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clean = AgentResult(
        role=AgentRole.PLANNER,
        success=True,
        execution_plan=_wordy_plan().model_copy(update={"summary": "Change the parser."}),
    )
    failed = AgentResult(role=AgentRole.PLANNER, success=False, failure_reason="boom")
    no_artifact = AgentResult(role=AgentRole.PLANNER, success=True)

    with caplog.at_level(logging.WARNING, logger="software_agent_factory.writing_policy"):
        for result in (clean, failed, no_artifact):
            assert result_writing_findings(result, AgentPurpose.STANDARD, source="x") == ()

    assert caplog.text == ""


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


def test_clean_unresolved_decision_prose_passes_writing_policy() -> None:
    plan = ExecutionPlan(
        summary="Implement required interface changes.",
        steps=[PlanStep(id="step-1", goal="Update parser.", likely_files=["src/parser.py"])],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        test_strategy=["Run existing unit tests."],
        risks=["The change may affect parser performance."],
        unresolved_decisions=[
            "The data layer requires a human choice between SQLite and PostgreSQL.",
        ],
    )
    assert validate_artifact_writing(plan) == ()


def test_invalid_unresolved_decision_prose_fails_writing_policy() -> None:
    # Test word count limit (>30 words)
    long_decision = " ".join(["word"] * 31)
    plan_long = ExecutionPlan(
        summary="Implement required interface changes.",
        steps=[PlanStep(id="step-1", goal="Update parser.", likely_files=["src/parser.py"])],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=[long_decision],
    )
    findings_long = validate_artifact_writing(plan_long)
    assert any("unresolved_decisions[0] has 31 words. The limit is 30." in f for f in findings_long)

    # Test prohibited style (e.g. semicolon, latin abbrev, filler)
    plan_bad_style = ExecutionPlan(
        summary="Implement required interface changes.",
        steps=[PlanStep(id="step-1", goal="Update parser.", likely_files=["src/parser.py"])],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=["We need a robust solution; e.g. for gRPC."],
    )
    findings_bad = validate_artifact_writing(plan_bad_style)
    assert len(findings_bad) > 0


def test_lint_reads_each_list_item_as_one_sentence() -> None:
    report = lint(
        "- Run the unit tests first\n"
        "1. Check the log output!\n"
        "* Open the report page\n"
        "Not a list line here.",
        "procedural",
    )

    assert report["sentences"] == 4
    assert report["longest_sentence_words"] == len("Run the unit tests first.".split())


def test_publication_text_findings_are_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="software_agent_factory.writing_policy"):
        findings = check_publication_text("issue body", "A robust plan.", max_words=100)

    assert findings == ("issue body has 1 slop_word finding(s).",)
    assert "publication text findings field=issue body count=1" in caplog.text


def test_clean_publication_text_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="software_agent_factory.writing_policy"):
        assert check_publication_text("issue body", "Add the parser.", max_words=100) == ()

    assert caplog.text == ""


def test_blank_publication_text_still_raises() -> None:
    with pytest.raises(ValueError, match="commit message is empty"):
        check_publication_text("commit message", "  ", max_words=20)
