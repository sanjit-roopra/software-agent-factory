"""Controller-owned rules for concise technical prose.

The policy uses selected mechanical checks from SimpleEnglish. It applies
ASD-STE100 principles but does not claim formal compliance.
"""

from __future__ import annotations

import logging
from typing import Literal

from ._vendor.simple_english import lint, prose_word_count
from .models import (
    ChangeSet,
    ExecutionPlan,
    ModelBase,
    PlanningResult,
    ProjectPlan,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
)

WritingType = Literal["procedural", "descriptive"]
SIMPLE_ENGLISH_REVISION = "61ee200efbd423050aab982eed94226229891ae0"

logger = logging.getLogger(__name__)


#: Word limits for agent-authored prose, keyed by artifact type. A ``[]`` marks a
#: list of items, and each item has the limit. The prompt states them (ADR-036).
_FIELD_LIMITS: dict[type[ModelBase], dict[str, int]] = {
    TriageResult: {
        "dependencies": 25,
        "unknowns": 25,
        "intended_outcome": 30,
        "sensitive_boundary": 30,
        "necessity": 30,
        "credible_scenario": 45,
        "known_mitigations": 25,
        "residual_risk": 30,
    },
    Specification: {
        "problem": 80,
        "acceptance_criteria": 25,
        "constraints": 30,
        "assumptions": 30,
        "unknowns": 30,
        "dependencies": 30,
        "risk_flags": 30,
    },
    ExecutionPlan: {
        "summary": 25,
        "test_strategy": 20,
        "risks": 30,
        "unresolved_decisions": 30,
        "steps[].goal": 20,
        "steps[].validation": 20,
    },
    ChangeSet: {"summary": 40},
    TestReport: {"findings": 50, "suggested_tests": 20},
    ReviewReport: {
        "findings": 50,
        "scope_concerns": 50,
        "security_concerns": 50,
        "compatibility_concerns": 50,
        "suggested_changes": 20,
        "blocking_findings[].message": 50,
        "repair_regressions[].message": 50,
        "prior_finding_dispositions[].rationale": 50,
    },
    ProjectPlan: {
        "summary": 40,
        "delivery_approach": 120,
        "tasks[].title": 15,
        "tasks[].description": 100,
        "tasks[].acceptance_criteria": 25,
        "tasks[].constraints": 30,
    },
}
#: The planner returns both artifacts in one object (ADR-035).
_FIELD_LIMITS[PlanningResult] = {
    f"{part}.{field}": words
    for part, artifact_type in (
        ("specification", Specification),
        ("execution_plan", ExecutionPlan),
    )
    for field, words in _FIELD_LIMITS[artifact_type].items()
}

#: Filler words that the prose check flags. The prompt lists a few of them.
FILLER_EXAMPLES: tuple[str, ...] = (
    "robust",
    "comprehensive",
    "leverage",
    "utilize",
    "crucial",
    "pivotal",
    "seamless",
    "streamline",
    "enhance",
    "furthermore",
    "moreover",
)


def field_word_limits(artifact_type: type[ModelBase]) -> dict[str, int]:
    """Return the word limit of each prose field of ``artifact_type``."""

    return dict(_FIELD_LIMITS.get(artifact_type, {}))


def writing_limits_text(artifact_type: type[ModelBase]) -> str:
    """Return the compact writing limits to show an agent, or an empty string."""

    limits = field_word_limits(artifact_type)
    if not limits:
        return ""
    pairs = ", ".join(f"{field}={words}" for field, words in limits.items())
    return (
        f"Word limits for each string or list item: {pairs}.\n"
        f"Avoid filler words such as {', '.join(FILLER_EXAMPLES)}."
    )


def validate_publication_text(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
) -> tuple[str, ...]:
    """Return the policy findings for one factory-authored publication field.

    Blank text is not a finding. ``check_publication_text`` rejects it.
    """

    findings: list[str] = []
    word_count = prose_word_count(text)
    if word_count > max_words:
        findings.append(f"{field} has {word_count} words. The limit is {max_words}.")
    report = lint(text, text_type)
    findings.extend(
        f"{field} has {count} {rule} finding(s)."
        for rule, count in report["violations"].items()
        if count
    )
    return tuple(findings)


def check_publication_text(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
) -> tuple[str, ...]:
    """Log advisory wording findings for factory-authored publication text.

    Wording never blocks publication. Blank text is a structural error, so it
    still raises before any mutation.
    """

    if not text.strip():
        raise ValueError(f"{field} is empty.")
    findings = validate_publication_text(
        field,
        text,
        text_type=text_type,
        max_words=max_words,
    )
    if findings:
        logger.warning(
            "publication text findings field=%s count=%d: %s",
            field,
            len(findings),
            " | ".join(findings),
        )
    return findings
