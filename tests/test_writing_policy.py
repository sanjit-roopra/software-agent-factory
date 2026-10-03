from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from software_agent_factory._vendor.simple_english.lint import lint
from software_agent_factory.models import (
    ChangeSet,
    PlanningResult,
    TriageResult,
    WorkItem,
)
from software_agent_factory.writing_policy import (
    FILLER_EXAMPLES,
    SIMPLE_ENGLISH_REVISION,
    check_publication_text,
    field_word_limits,
    validate_publication_text,
    writing_limits_text,
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
    text = "The API may return an empty result."

    assert validate_publication_text("text", text, max_words=20) == ()


def test_publication_text_over_the_word_limit_is_a_finding() -> None:
    findings = validate_publication_text("title", "One two three four five six.", max_words=5)

    assert findings == ("title has 6 words. The limit is 5.",)


def test_planning_result_limits_name_the_specification_and_plan_parts() -> None:
    assert "specification.problem=80" in writing_limits_text(PlanningResult)
    assert "execution_plan.summary=25" in writing_limits_text(PlanningResult)


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


def test_triage_limits_do_not_include_the_removed_requirements_quality() -> None:
    limits = field_word_limits(TriageResult)

    assert limits["unknowns"] == 25
    assert limits["credible_scenario"] == 45
    assert "requirements_quality" not in limits


def test_field_word_limits_is_a_copy_and_empty_for_an_unlimited_type() -> None:
    field_word_limits(ChangeSet)["summary"] = 1

    assert field_word_limits(ChangeSet) == {"summary": 40}
    assert field_word_limits(WorkItem) == {}


def test_writing_limits_text_lists_each_limit_and_filler_examples() -> None:
    text = writing_limits_text(TriageResult)

    assert text.startswith("Word limits for each string or list item: ")
    assert "unknowns=25" in text
    assert "credible_scenario=45" in text
    assert "Avoid filler words such as robust, comprehensive" in text
    assert writing_limits_text(WorkItem) == ""


def test_filler_examples_are_words_the_prose_check_flags() -> None:
    for word in FILLER_EXAMPLES:
        assert lint(f"The change is {word} today.", "descriptive")["violations"]["slop_word"] == 1
