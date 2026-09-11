"""Controller-owned rules for concise technical prose.

The policy uses selected mechanical checks from SimpleEnglish. It applies
ASD-STE100 principles but does not claim formal compliance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


def artifact_passages(artifact: ModelBase) -> list[WritingPassage]:
    """Return the agent-authored prose fields for one typed artifact."""

    if isinstance(artifact, TriageResult):
        return [
            _passage("requirements_quality", artifact.requirements_quality, max_words=12),
            *_items("dependencies", artifact.dependencies, max_words=25, lint_prose=False),
            *_items("unknowns", artifact.unknowns, max_words=25),
        ]
    if isinstance(artifact, Specification):
        return [
            _passage("problem", artifact.problem, max_words=80),
            *_items(
                "acceptance_criteria",
                artifact.acceptance_criteria,
                text_type="procedural",
                max_words=25,
            ),
            *_items("constraints", artifact.constraints, max_words=30),
            *_items("assumptions", artifact.assumptions, max_words=30),
            *_items("unknowns", artifact.unknowns, max_words=30),
            *_items("dependencies", artifact.dependencies, max_words=30, lint_prose=False),
            *_items("risk_flags", artifact.risk_flags, max_words=30),
        ]
    if isinstance(artifact, ResearchReport):
        return [
            _passage("question", artifact.question, max_words=80),
            *_items("findings", artifact.findings, max_words=50),
            *_items("evidence", artifact.evidence, max_words=50),
            *_items("implications", artifact.implications, max_words=35),
            *_items("uncertainty", artifact.uncertainty, max_words=35),
        ]
    if isinstance(artifact, ExecutionPlan):
        passages = [
            _passage("summary", artifact.summary, max_words=25),
            *_items(
                "test_strategy",
                artifact.test_strategy,
                text_type="procedural",
                max_words=20,
            ),
            *_items("risks", artifact.risks, max_words=30),
        ]
        for index, step in enumerate(artifact.steps):
            passages.append(
                _passage(
                    f"steps[{index}].goal",
                    step.goal,
                    text_type="procedural",
                    max_words=20,
                )
            )
            passages.extend(
                _items(
                    f"steps[{index}].validation",
                    step.validation,
                    text_type="procedural",
                    max_words=20,
                )
            )
        return passages
    if isinstance(artifact, ChangeSet):
        return [_passage("summary", artifact.summary, max_words=40)]
    if isinstance(artifact, TestReport):
        return [
            *_items("findings", artifact.findings, max_words=50),
            *_items(
                "suggested_tests",
                artifact.suggested_tests,
                text_type="procedural",
                max_words=20,
            ),
        ]
    if isinstance(artifact, ReviewReport):
        passages = [
            *_items("findings", artifact.findings, max_words=50),
            *_items("scope_concerns", artifact.scope_concerns, max_words=50),
            *_items("security_concerns", artifact.security_concerns, max_words=50),
            *_items("compatibility_concerns", artifact.compatibility_concerns, max_words=50),
            *_items(
                "suggested_changes",
                artifact.suggested_changes,
                text_type="procedural",
                max_words=20,
            ),
        ]
        for field, findings in (
            ("blocking_findings", artifact.blocking_findings),
            ("repair_regressions", artifact.repair_regressions),
        ):
            passages.extend(
                _passage(f"{field}[{index}].message", finding.message, max_words=50)
                for index, finding in enumerate(findings)
            )
        passages.extend(
            _passage(
                f"prior_finding_dispositions[{index}].rationale",
                disposition.rationale,
                max_words=50,
            )
            for index, disposition in enumerate(artifact.prior_finding_dispositions)
        )
        return passages
    if isinstance(artifact, ProjectPlan):
        passages = [
            _passage("summary", artifact.summary, max_words=40),
            _passage("delivery_approach", artifact.delivery_approach, max_words=120),
        ]
        for index, task in enumerate(artifact.tasks):
            passages.extend(
                [
                    _passage(f"tasks[{index}].title", task.title, max_words=15),
                    _passage(f"tasks[{index}].description", task.description, max_words=100),
                    *_items(
                        f"tasks[{index}].acceptance_criteria",
                        task.acceptance_criteria,
                        text_type="procedural",
                        max_words=25,
                    ),
                    *_items(
                        f"tasks[{index}].constraints",
                        task.constraints,
                        max_words=30,
                    ),
                ]
            )
        return passages
    if isinstance(artifact, RepositorySkill):
        return [
            _passage("simplify.summary", artifact.simplify.summary, max_words=40),
            *_items(
                "simplify.guidance",
                artifact.simplify.guidance,
                text_type="procedural",
                max_words=25,
            ),
            *_items(
                "simplify.avoid",
                artifact.simplify.avoid,
                text_type="procedural",
                max_words=25,
            ),
            *_items(
                "simplify.validation",
                artifact.simplify.validation,
                text_type="procedural",
                max_words=20,
            ),
            _passage("polish.summary", artifact.polish.summary, max_words=40),
            *_items(
                "polish.guidance",
                artifact.polish.guidance,
                text_type="procedural",
                max_words=25,
            ),
            *_items(
                "polish.avoid",
                artifact.polish.avoid,
                text_type="procedural",
                max_words=25,
            ),
            *_items(
                "polish.validation",
                artifact.polish.validation,
                text_type="procedural",
                max_words=20,
            ),
            *_items("uncertainties", artifact.uncertainties, max_words=35),
        ]
    return []


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


def apply_agent_result_writing_policy(
    result: AgentResult,
    purpose: AgentPurpose,
) -> AgentResult:
    """Convert a non-conforming successful result into a retryable failure."""

    if not result.success:
        return result
    artifact = _result_artifact(result, purpose)
    if artifact is None:
        return result
    findings = validate_artifact_writing(artifact)
    if not findings:
        return result
    artifact_name = type(artifact).__name__
    return result.model_copy(
        update={
            "success": False,
            "failure_reason": (
                f"{artifact_name} did not satisfy writing policy:\n" + "\n".join(findings)
            ),
        }
    )


def writing_policy_correction_context(
    failure_reason: str,
    artifact: ModelBase,
) -> str:
    """Build one correction prompt with the rejected artifact."""

    return (
        f"{failure_reason}\n"
        "Rewrite only the prose fields that failed. Preserve all facts, uncertainty, "
        "identifiers, paths, commands, quoted errors, and other fields. Return one complete "
        f"{type(artifact).__name__} JSON object.\n\n"
        "Previous rejected artifact:\n"
        f"{artifact.model_dump_json(indent=2)}"
    )


def validate_publication_text(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
) -> tuple[str, ...]:
    """Validate one factory-authored publication field."""

    return validate_passages([_passage(field, text, text_type=text_type, max_words=max_words)])


def require_publication_text(
    field: str,
    text: str,
    *,
    text_type: WritingType = "descriptive",
    max_words: int,
) -> None:
    """Raise before mutation when factory-authored publication text is invalid."""

    findings = validate_publication_text(
        field,
        text,
        text_type=text_type,
        max_words=max_words,
    )
    if findings:
        raise ValueError(f"{field} did not satisfy writing policy:\n" + "\n".join(findings))
