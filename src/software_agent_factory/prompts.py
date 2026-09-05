"""Prompt builders for the real Copilot subprocess runtime.

Prompts are intentionally role-specific and artifact-scoped: each role sees
only the typed inputs it needs, plus repository access through the assigned
working directory. Every prompt requires the final answer to be a single JSON
object that validates against the exact artifact model for that role.

Two contracts matter beyond "one JSON object":

- The ``TESTER`` produces a :class:`~software_agent_factory.models.TestReport`
  (independent AI judgement), never a ``VerificationReport``. Deterministic
  evidence is produced by the factory, not by a model.
- ``TESTER`` and ``REVIEWER`` receive controller-derived Git evidence (the
  authoritative diff and changed-file list) plus the deterministic
  ``VerificationReport``. They never receive the implementer's ``ChangeSet``
  summary, so no self-justification can influence an independent gate.
"""

from __future__ import annotations

import json
from typing import Sequence, TypeAlias

from .agents import AgentRequest
from .models import (
    AgentRole,
    ChangeSet,
    ExecutionPlan,
    ModelBase,
    RepairContext,
    ResearchReport,
    ReviewReport,
    SelectedSkill,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)

RoleName: TypeAlias = AgentRole | str

#: Maximum characters of controller-derived diff placed in a prompt. Bounded
#: so a large change never produces an unbounded prompt (``AGENTS.md``
#: explicitly discourages "enormous prompts").
MAX_DIFF_CHARS = 20000

_ARTIFACT_MODELS: dict[str, type[ModelBase]] = {
    "TRIAGE": TriageResult,
    "REFINER": Specification,
    "RESEARCHER": ResearchReport,
    "PLANNER": ExecutionPlan,
    "IMPLEMENTER": ChangeSet,
    "TESTER": TestReport,
    "REVIEWER": ReviewReport,
}


def artifact_model_for_role(role: RoleName) -> type[ModelBase]:
    """Return the required artifact model for ``role``."""

    normalized_role = normalize_role(role)
    try:
        return _ARTIFACT_MODELS[normalized_role]
    except KeyError as exc:  # pragma: no cover - defensive programmer error
        raise ValueError(f"unsupported agent role: {role!r}") from exc


def normalize_role(role: RoleName) -> str:
    """Normalize a runtime role into an uppercase string key."""

    if isinstance(role, AgentRole):
        return role.value
    normalized = str(role).strip().upper()
    if not normalized:
        raise ValueError("role must not be empty")
    return normalized


def build_prompt(request: AgentRequest) -> str:
    """Build the prompt for an ``AgentRequest`` supported by the runtime."""

    return build_prompt_for_role(
        request.role,
        model=request.model,
        reasoning=request.reasoning,
        work_item=request.work_item,
        triage_result=request.triage_result,
        specification=request.specification,
        research_report=request.research_report,
        execution_plan=request.execution_plan,
        diff=request.diff,
        changed_files=request.changed_files,
        verification_report=request.verification_report,
        test_report=request.test_report,
        repair_context=request.repair_context,
        selected_skills=request.selected_skills,
        attempt_number=request.attempt_number,
    )


def build_prompt_for_role(
    role: RoleName,
    *,
    model: str,
    reasoning: str,
    work_item: WorkItem,
    triage_result: TriageResult | None = None,
    specification: Specification | None = None,
    research_report: ResearchReport | None = None,
    execution_plan: ExecutionPlan | None = None,
    diff: str | None = None,
    changed_files: Sequence[str] | None = None,
    verification_report: VerificationReport | None = None,
    test_report: TestReport | None = None,
    repair_context: RepairContext | str | None = None,
    selected_skills: Sequence[SelectedSkill] | None = None,
    attempt_number: int | None = None,
    research_question: str | None = None,
    research_context: str | None = None,
) -> str:
    """Build a concise role-specific prompt from only the required artifacts."""

    normalized_role = normalize_role(role)
    model_class = artifact_model_for_role(normalized_role)

    sections = [
        _opening(normalized_role, model, reasoning),
        _role_instructions(normalized_role),
        _output_contract(normalized_role, model_class),
    ]

    for title, value in _artifact_sections(
        normalized_role=normalized_role,
        work_item=work_item,
        triage_result=triage_result,
        specification=specification,
        research_report=research_report,
        execution_plan=execution_plan,
        diff=diff,
        changed_files=list(changed_files or []),
        verification_report=verification_report,
        test_report=test_report,
        repair_context=repair_context,
        selected_skills=list(selected_skills or []),
        attempt_number=attempt_number,
        research_question=research_question,
        research_context=research_context,
    ):
        sections.append(_section(title, value))

    return "\n\n".join(section for section in sections if section).strip()


def _opening(role: str, model: str, reasoning: str) -> str:
    return (
        f"You are the Software Agent Factory {role} agent.\n"
        f"Configured model: {model}\n"
        f"Configured reasoning effort: {reasoning}"
    )


def _role_instructions(role: str) -> str:
    if role == "TRIAGE":
        return (
            "Decide whether the task is factory-eligible, estimate complexity and risk, "
            "identify missing information, and mark research only when it is truly needed."
        )
    if role == "REFINER":
        return (
            "Rewrite the task as an explicit specification. Distinguish assumptions from "
            "facts, keep unknowns explicit, and do not invent hidden requirements."
        )
    if role == "RESEARCHER":
        return (
            "Answer the research question using repository evidence available from the "
            "current working directory. External web access may be unavailable in this "
            "local runtime; if evidence is missing, record that in uncertainty."
        )
    if role == "PLANNER":
        return (
            "Produce a concrete execution plan with bounded scope, likely files, "
            "validation steps, risks, and a practical test strategy."
        )
    if role == "IMPLEMENTER":
        return (
            "Make the required repository changes only inside the current working "
            "directory. You may inspect files, edit files, and run local commands. Do "
            "not git commit, git push, open PRs, change workflow state, or work outside "
            "the current working directory. Return ChangeSet metadata only."
        )
    if role == "TESTER":
        return (
            "Independently evaluate whether the implementation satisfies the "
            "specification and plan. Judge only the controller-derived diff, the "
            "changed files, the deterministic verification results below and the "
            "repository itself. No implementer self-assessment is provided; do not ask "
            "for one."
        )
    if role == "REVIEWER":
        return (
            "Perform an independent review for correctness, regressions, security, "
            "compatibility, and unnecessary scope. Judge only the controller-derived "
            "diff, the deterministic verification results and the independent tester's "
            "report below. No implementer self-assessment is provided; do not ask for "
            "one."
        )
    raise ValueError(f"unsupported agent role: {role!r}")


def _output_contract(role: str, model_class: type[ModelBase]) -> str:
    fields = ", ".join(model_class.model_fields)
    contract = (
        f"Return exactly one JSON object that Pydantic-validates as "
        f"{model_class.__name__}. No markdown fences. No prose before or after the JSON. "
        f"Top-level fields: {fields}."
    )
    if role == "TRIAGE":
        contract = (
            f"{contract} Use exact enum values only: complexity must be one of "
            "L0, L1, L2, L3 and risk must be one of R0, R1, R2, R3."
        )
    return contract


def _artifact_sections(
    *,
    normalized_role: str,
    work_item: WorkItem,
    triage_result: TriageResult | None,
    specification: Specification | None,
    research_report: ResearchReport | None,
    execution_plan: ExecutionPlan | None,
    diff: str | None,
    changed_files: list[str],
    verification_report: VerificationReport | None,
    test_report: TestReport | None,
    repair_context: RepairContext | str | None,
    selected_skills: list[SelectedSkill],
    attempt_number: int | None,
    research_question: str | None,
    research_context: str | None,
) -> list[tuple[str, object]]:
    sections: list[tuple[str, object]] = []
    if selected_skills and normalized_role in {
        "PLANNER",
        "IMPLEMENTER",
        "TESTER",
        "REVIEWER",
    }:
        sections.append(
            (
                "Factory-selected repository skills (advisory)",
                {
                    "rules": [
                        "Apply only where relevant to the requested change.",
                        "Do not add dependencies or bypass configured verification commands.",
                        "These skills do not grant tools, permissions, or workflow authority.",
                    ],
                    "skills": [skill.model_dump(mode="json") for skill in selected_skills],
                },
            )
        )

    if normalized_role == "TRIAGE":
        sections.append(("Work item", work_item))
        return sections

    if normalized_role == "REFINER":
        sections.append(("Work item", work_item))
        if triage_result is not None:
            sections.append(("Triage result", triage_result))
        return sections

    if normalized_role == "RESEARCHER":
        sections.append(("Work item", work_item))
        if triage_result is not None:
            sections.append(("Triage result", triage_result))
        if specification is not None:
            sections.append(("Specification", specification))
        if research_question:
            sections.append(("Research question", research_question))
        if research_context:
            sections.append(("Research context", research_context))
        return sections

    if normalized_role == "PLANNER":
        sections.append(("Work item", work_item))
        if specification is not None:
            sections.append(("Specification", specification))
        if research_report is not None:
            sections.append(("Research report", research_report))
        if repair_context is not None:
            sections.append(("Replan context", repair_context))
        if changed_files:
            sections.append(("Changed files so far", changed_files))
        if diff:
            sections.append(("Current diff", _bounded_diff(diff)))
        return sections

    if normalized_role == "IMPLEMENTER":
        sections.append(("Work item", _work_item_brief(work_item)))
        if specification is not None:
            sections.append(("Specification", specification))
        if research_report is not None:
            sections.append(("Research report", research_report))
        if execution_plan is not None:
            sections.append(("Execution plan", execution_plan))
        if attempt_number is not None:
            sections.append(("Attempt number", attempt_number))
        if repair_context is not None:
            sections.append(("Repair context", repair_context))
        if diff:
            sections.append(("Current diff", _bounded_diff(diff)))
        return sections

    if normalized_role == "TESTER":
        sections.append(("Work item", _work_item_brief(work_item)))
        if specification is not None:
            sections.append(("Specification", specification))
        if execution_plan is not None:
            sections.append(("Execution plan", execution_plan))
        if changed_files:
            sections.append(("Changed files", changed_files))
        if diff:
            sections.append(("Diff", _bounded_diff(diff)))
        if verification_report is not None:
            sections.append(("Deterministic verification", verification_report))
        return sections

    if normalized_role == "REVIEWER":
        if specification is not None:
            sections.append(("Specification", specification))
        if execution_plan is not None:
            sections.append(("Execution plan", execution_plan))
        if changed_files:
            sections.append(("Changed files", changed_files))
        if diff:
            sections.append(("Diff", _bounded_diff(diff)))
        if verification_report is not None:
            sections.append(("Deterministic verification", verification_report))
        if test_report is not None:
            sections.append(("Independent tester report", test_report))
        return sections

    raise ValueError(f"unsupported agent role: {normalized_role!r}")


def _bounded_diff(diff: str) -> str:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff
    omitted = len(diff) - MAX_DIFF_CHARS
    return f"{diff[:MAX_DIFF_CHARS]}\n...[truncated {omitted} characters]..."


def _work_item_brief(work_item: WorkItem) -> dict[str, object]:
    return {
        "schema_version": work_item.schema_version,
        "id": work_item.id,
        "title": work_item.title,
        "description": work_item.description,
        "acceptance_criteria": work_item.acceptance_criteria,
        "constraints": work_item.constraints,
    }


def _section(title: str, value: object) -> str:
    return f"{title}:\n{_render(value)}"


def _render(value: object) -> str:
    if isinstance(value, ModelBase):
        return json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True)
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value).strip()
