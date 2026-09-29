"""Controller-owned rules for concise technical prose.

The policy uses selected mechanical checks from SimpleEnglish. It applies
ASD-STE100 principles but does not claim formal compliance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ._vendor.simple_english import lint, prose_word_count
from .agents import AgentResult
from .models import (
    AgentPurpose,
    ChangeSet,
    ExecutionPlan,
    ModelBase,
    ProjectPlan,
    RepositorySkill,
    ResearchReport,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
)

WritingType = Literal["procedural", "descriptive"]
POLICY_NAME = "controlled technical English"
POLICY_VERSION = 1
SIMPLE_ENGLISH_REVISION = "61ee200efbd423050aab982eed94226229891ae0"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WritingPassage:
    field: str
    text: str
    text_type: WritingType
    max_words: int
    lint_prose: bool = True


def _passage(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
    lint_prose: bool = True,
) -> WritingPassage:
    return WritingPassage(
        field=field,
        text=text,
        text_type=text_type,
        max_words=max_words,
        lint_prose=lint_prose,
    )


def _items(
    field: str,
    values: list[str] | tuple[str, ...],
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
    lint_prose: bool = True,
) -> list[WritingPassage]:
    return [
        _passage(
            f"{field}[{index}]",
            value,
            text_type=text_type,
            max_words=max_words,
            lint_prose=lint_prose,
        )
        for index, value in enumerate(values)
    ]


#: Word limits for agent-authored prose, keyed by artifact type. A ``[]`` marks a
#: list of items, and each item has the limit. This table is the only place that
#: holds a limit: ``artifact_passages`` checks them and the prompt states them.
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
    ResearchReport: {
        "question": 80,
        "findings": 50,
        "evidence": 50,
        "implications": 35,
        "uncertainty": 35,
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
    RepositorySkill: {
        "simplify.summary": 40,
        "simplify.guidance": 25,
        "simplify.avoid": 25,
        "simplify.validation": 20,
        "polish.summary": 40,
        "polish.guidance": 25,
        "polish.avoid": 25,
        "polish.validation": 20,
        "uncertainties": 35,
    },
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


def _triage_passages(artifact: TriageResult, limit: dict[str, int]) -> list[WritingPassage]:
    passages = [
        *_items(
            "dependencies",
            artifact.dependencies,
            max_words=limit["dependencies"],
            lint_prose=False,
        ),
        *_items("unknowns", artifact.unknowns, max_words=limit["unknowns"]),
    ]
    if artifact.risk_rationale is not None:
        rationale = artifact.risk_rationale
        passages.extend(
            [
                _passage(name, getattr(rationale, name), max_words=limit[name])
                for name in (
                    "intended_outcome",
                    "sensitive_boundary",
                    "necessity",
                    "credible_scenario",
                )
            ]
        )
        passages.extend(
            _items(
                "known_mitigations",
                rationale.known_mitigations,
                max_words=limit["known_mitigations"],
            )
        )
        passages.append(
            _passage("residual_risk", rationale.residual_risk, max_words=limit["residual_risk"])
        )
    return passages


def _specification_passages(artifact: Specification, limit: dict[str, int]) -> list[WritingPassage]:
    return [
        _passage("problem", artifact.problem, max_words=limit["problem"]),
        *_items(
            "acceptance_criteria",
            artifact.acceptance_criteria,
            text_type="procedural",
            max_words=limit["acceptance_criteria"],
        ),
        *_items("constraints", artifact.constraints, max_words=limit["constraints"]),
        *_items("assumptions", artifact.assumptions, max_words=limit["assumptions"]),
        *_items("unknowns", artifact.unknowns, max_words=limit["unknowns"]),
        *_items(
            "dependencies",
            artifact.dependencies,
            max_words=limit["dependencies"],
            lint_prose=False,
        ),
        *_items("risk_flags", artifact.risk_flags, max_words=limit["risk_flags"]),
    ]


def _research_passages(artifact: ResearchReport, limit: dict[str, int]) -> list[WritingPassage]:
    return [
        _passage("question", artifact.question, max_words=limit["question"]),
        *_items("findings", artifact.findings, max_words=limit["findings"]),
        *_items("evidence", artifact.evidence, max_words=limit["evidence"]),
        *_items("implications", artifact.implications, max_words=limit["implications"]),
        *_items("uncertainty", artifact.uncertainty, max_words=limit["uncertainty"]),
    ]


def _plan_passages(artifact: ExecutionPlan, limit: dict[str, int]) -> list[WritingPassage]:
    passages = [
        _passage("summary", artifact.summary, max_words=limit["summary"]),
        *_items(
            "test_strategy",
            artifact.test_strategy,
            text_type="procedural",
            max_words=limit["test_strategy"],
        ),
        *_items("risks", artifact.risks, max_words=limit["risks"]),
        *_items(
            "unresolved_decisions",
            artifact.unresolved_decisions,
            max_words=limit["unresolved_decisions"],
        ),
    ]
    for index, step in enumerate(artifact.steps):
        passages.append(
            _passage(
                f"steps[{index}].goal",
                step.goal,
                text_type="procedural",
                max_words=limit["steps[].goal"],
            )
        )
        passages.extend(
            _items(
                f"steps[{index}].validation",
                step.validation,
                text_type="procedural",
                max_words=limit["steps[].validation"],
            )
        )
    return passages


def _change_set_passages(artifact: ChangeSet, limit: dict[str, int]) -> list[WritingPassage]:
    return [_passage("summary", artifact.summary, max_words=limit["summary"])]


def _test_report_passages(artifact: TestReport, limit: dict[str, int]) -> list[WritingPassage]:
    return [
        *_items("findings", artifact.findings, max_words=limit["findings"]),
        *_items(
            "suggested_tests",
            artifact.suggested_tests,
            text_type="procedural",
            max_words=limit["suggested_tests"],
        ),
    ]


def _review_passages(artifact: ReviewReport, limit: dict[str, int]) -> list[WritingPassage]:
    passages = [
        *_items("findings", artifact.findings, max_words=limit["findings"]),
        *_items("scope_concerns", artifact.scope_concerns, max_words=limit["scope_concerns"]),
        *_items(
            "security_concerns",
            artifact.security_concerns,
            max_words=limit["security_concerns"],
        ),
        *_items(
            "compatibility_concerns",
            artifact.compatibility_concerns,
            max_words=limit["compatibility_concerns"],
        ),
        *_items(
            "suggested_changes",
            artifact.suggested_changes,
            text_type="procedural",
            max_words=limit["suggested_changes"],
        ),
    ]
    for field, findings in (
        ("blocking_findings", artifact.blocking_findings),
        ("repair_regressions", artifact.repair_regressions),
    ):
        passages.extend(
            _passage(
                f"{field}[{index}].message",
                finding.message,
                max_words=limit[f"{field}[].message"],
            )
            for index, finding in enumerate(findings)
        )
    passages.extend(
        _passage(
            f"prior_finding_dispositions[{index}].rationale",
            disposition.rationale,
            max_words=limit["prior_finding_dispositions[].rationale"],
        )
        for index, disposition in enumerate(artifact.prior_finding_dispositions)
    )
    return passages


def _project_plan_passages(artifact: ProjectPlan, limit: dict[str, int]) -> list[WritingPassage]:
    passages = [
        _passage("summary", artifact.summary, max_words=limit["summary"]),
        _passage(
            "delivery_approach",
            artifact.delivery_approach,
            max_words=limit["delivery_approach"],
        ),
    ]
    for index, task in enumerate(artifact.tasks):
        passages.extend(
            [
                _passage(f"tasks[{index}].title", task.title, max_words=limit["tasks[].title"]),
                _passage(
                    f"tasks[{index}].description",
                    task.description,
                    max_words=limit["tasks[].description"],
                ),
                *_items(
                    f"tasks[{index}].acceptance_criteria",
                    task.acceptance_criteria,
                    text_type="procedural",
                    max_words=limit["tasks[].acceptance_criteria"],
                ),
                *_items(
                    f"tasks[{index}].constraints",
                    task.constraints,
                    max_words=limit["tasks[].constraints"],
                ),
            ]
        )
    return passages


def _skill_passages(skill: RepositorySkill, limit: dict[str, int]) -> list[WritingPassage]:
    passages: list[WritingPassage] = []
    for name, guidance in (("simplify", skill.simplify), ("polish", skill.polish)):
        passages.append(
            _passage(f"{name}.summary", guidance.summary, max_words=limit[f"{name}.summary"])
        )
        for kind, values in (
            ("guidance", guidance.guidance),
            ("avoid", guidance.avoid),
            ("validation", guidance.validation),
        ):
            passages.extend(
                _items(
                    f"{name}.{kind}",
                    values,
                    text_type="procedural",
                    max_words=limit[f"{name}.{kind}"],
                )
            )
    passages.extend(_items("uncertainties", skill.uncertainties, max_words=limit["uncertainties"]))
    return passages


type _PassageBuilder = Callable[[Any, dict[str, int]], list[WritingPassage]]

_PASSAGE_BUILDERS: dict[type[ModelBase], _PassageBuilder] = {
    TriageResult: _triage_passages,
    Specification: _specification_passages,
    ResearchReport: _research_passages,
    ExecutionPlan: _plan_passages,
    ChangeSet: _change_set_passages,
    TestReport: _test_report_passages,
    ReviewReport: _review_passages,
    ProjectPlan: _project_plan_passages,
    RepositorySkill: _skill_passages,
}


def artifact_passages(artifact: ModelBase) -> list[WritingPassage]:
    """Return the agent-authored prose fields for one typed artifact."""

    builder = _PASSAGE_BUILDERS.get(type(artifact))
    if builder is None:
        return []
    return builder(artifact, _FIELD_LIMITS[type(artifact)])


def validate_passages(passages: list[WritingPassage]) -> tuple[str, ...]:
    """Return bounded policy findings for authored prose."""

    findings: list[str] = []
    for passage in passages:
        if not passage.text.strip():
            findings.append(f"{passage.field} is empty.")
        else:
            word_count = prose_word_count(passage.text)
            if word_count > passage.max_words:
                findings.append(
                    f"{passage.field} has {word_count} words. The limit is {passage.max_words}."
                )
            if passage.lint_prose:
                report = lint(passage.text, passage.text_type)
                for rule, count in report["violations"].items():
                    if count:
                        findings.append(f"{passage.field} has {count} {rule} finding(s).")
        if len(findings) >= 12:
            findings.append("More writing findings were omitted.")
            break
    return tuple(findings)


def validate_artifact_writing(artifact: ModelBase) -> tuple[str, ...]:
    """Validate all selected prose fields in one agent artifact."""

    return validate_passages(artifact_passages(artifact))


def _result_artifact(result: AgentResult, purpose: AgentPurpose) -> ModelBase | None:
    if purpose is AgentPurpose.DECOMPOSE_PROJECT:
        return result.project_plan
    if purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
        return result.repository_skill
    return {
        "TRIAGE": result.triage_result,
        "REFINER": result.specification,
        "RESEARCHER": result.research_report,
        "PLANNER": result.execution_plan,
        "IMPLEMENTER": result.change_set,
        "TESTER": result.test_report,
        "REVIEWER": result.review_report,
    }[result.role.value]


def result_writing_findings(
    result: AgentResult,
    purpose: AgentPurpose,
    *,
    source: str,
) -> tuple[str, ...]:
    """Return and log the advisory writing findings for a successful result.

    Writing rules never fail a result and never trigger a retry. The caller
    stores the findings on the invocation record.
    """

    if not result.success:
        return ()
    artifact = _result_artifact(result, purpose)
    if artifact is None:
        return ()
    findings = validate_artifact_writing(artifact)
    if findings:
        logger.warning(
            "writing findings source=%s artifact=%s count=%d: %s",
            source,
            type(artifact).__name__,
            len(findings),
            " | ".join(findings),
        )
    return findings


def validate_publication_text(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
) -> tuple[str, ...]:
    """Validate one factory-authored publication field."""

    return validate_passages([_passage(field, text, text_type=text_type, max_words=max_words)])


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
