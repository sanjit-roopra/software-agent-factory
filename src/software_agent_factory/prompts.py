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

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources

from .agents import AgentRequest
from .models import (
    AgentPurpose,
    AgentRole,
    AttemptTrigger,
    ChangeSet,
    CommandResult,
    ModelBase,
    PlanningResult,
    ProjectPlan,
    RepairContext,
    ReviewFinding,
    ReviewReport,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)
from .review_lenses import ReviewLens, render_review_lenses, select_review_lenses
from .writing_policy import writing_limits_text

type RoleName = AgentRole | str

#: Maximum characters of controller-derived diff placed in a prompt. Bounded
#: so a large change never produces an unbounded prompt (``AGENTS.md``
#: explicitly discourages "enormous prompts").
MAX_DIFF_CHARS = 20000

_WORK_ITEM_TITLE = "Work item"
_SPECIFICATION_TITLE = "Specification"
_EXECUTION_PLAN_TITLE = "Execution plan"
_TRIAGE_RESULT_TITLE = "Triage result"
_DIFF_TITLE = "Diff"
_VERIFICATION_TITLE = "Deterministic verification"
_CHANGED_FILES_TITLE = "Changed files"
_REVIEW_LENSES_TITLE = "Review lenses for the changed files"
_POLISH_GUIDANCE_TITLE = "Simplify and polish guidance"
_REPAIR_CONTEXT_TITLE = "Repair context"
_CURRENT_DIFF_TITLE = "Current diff"
_OUTPUT_REJECTION_TITLE = "Previous output rejection"
_PRIOR_FINDINGS_TITLE = "Previously reported blocking issues from this run"
_ACCEPTED_DEBT_TITLE = "Controller-accepted review debt"
_ACCEPTED_DEBT_RULE_TITLE = "Accepted-debt review rule"
_REPAIR_DIFF_TITLE = "Changes since the previous review"

_OPENING_TITLE = "Opening"
_WRITING_RULES_TITLE = "Writing rules"
_ROLE_INSTRUCTIONS_TITLE = "Role instructions"
_OUTPUT_CONTRACT_TITLE = "Output contract"
_NO_LONGER_APPLIES_TITLE = "No longer applies"

_ARTIFACT_MODELS: dict[str, type[ModelBase]] = {
    "TRIAGE": TriageResult,
    "PLANNER": PlanningResult,
    "IMPLEMENTER": ChangeSet,
    "TESTER": TestReport,
    "REVIEWER": ReviewReport,
}

_WRITING_RULES = """Writing rules for every JSON text field:
- Use concise technical English in the spirit of ASD-STE100.
- Use active voice and simple sentences.
- Put one action or fact in each sentence.
- Put a condition before the action that depends on it.
- Use at most 20 words for an instruction sentence.
- Use at most 25 words for a descriptive sentence.
- Do not use filler, semicolons, em dashes, or Latin abbreviations.
- Preserve facts, uncertainty, identifiers, paths, commands, URLs, and quoted errors exactly."""


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


@dataclass(frozen=True)
class PromptSection:
    """One titled part of a prompt.

    ``body`` is the content. ``build_prompt`` prints ``<title>:`` before an
    artifact section (``titled``) and prints the body of the opening, the writing
    rules, the role instructions and the output contract as they are.
    ``labelled_text`` always carries the title, so a section that is sent alone
    still says what it is.
    """

    title: str
    body: str
    titled: bool = True

    @property
    def labelled_text(self) -> str:
        return f"{self.title}:\n{self.body}"

    @property
    def text(self) -> str:
        """The section exactly as ``build_prompt`` prints it."""
        return self.labelled_text if self.titled else self.body

    @property
    def digest(self) -> str:
        """SHA-256 of the body: equal digests under one title mean equal content."""
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def build_prompt(request: AgentRequest) -> str:
    """Build the prompt for an ``AgentRequest`` supported by the runtime."""

    return render_prompt(build_prompt_sections(request))


def render_prompt(sections: Sequence[PromptSection]) -> str:
    """Join ``sections`` into the full prompt."""

    return "\n\n".join(section.text for section in sections if section.text).strip()


def build_prompt_sections(request: AgentRequest) -> list[PromptSection]:
    """Return every part of the full prompt for ``request``, in prompt order.

    The opening, the writing rules, the role instructions and the output
    contract come first, then one section for each artifact the role receives.
    Titles are unique within one request.
    """

    normalized_role = normalize_role(request.role)
    model_class = _model_class_for(normalized_role, request.purpose)
    sections = [
        PromptSection(
            _OPENING_TITLE,
            _opening(normalized_role, request.model, request.reasoning),
            titled=False,
        ),
        PromptSection(_WRITING_RULES_TITLE, _WRITING_RULES, titled=False),
        PromptSection(
            _ROLE_INSTRUCTIONS_TITLE,
            _role_instructions(
                normalized_role,
                request.purpose,
                repair_review=bool(request.prior_review_findings),
                risk_assessment=request.risk_assessment_enabled,
            ),
            titled=False,
        ),
        PromptSection(
            _OUTPUT_CONTRACT_TITLE,
            _output_contract(
                normalized_role, model_class, risk_assessment=request.risk_assessment_enabled
            ),
            titled=False,
        ),
    ]
    artifacts = _artifact_sections(normalized_role, request)
    sections.extend(PromptSection(title, _render(value)) for title, value in artifacts)
    return sections


def section_hashes(sections: Sequence[PromptSection]) -> dict[str, str]:
    """Map each section title to the content hash of its body."""

    return {section.title: section.digest for section in sections}


@dataclass(frozen=True)
class ContinuationPrompt:
    """The prompt for a call that continues a session, and what the session then holds.

    ``sections_seen`` is the title to content hash map of the sections that apply
    after this call. The caller stores it for the next call.
    """

    text: str
    sections_seen: Mapping[str, str]


_CONTINUATION_LEAD = (
    "This continues the session. Only new or changed sections follow. "
    "Earlier sections still apply unless a section below replaces them."
)


def build_continuation_prompt(
    request: AgentRequest, seen: Mapping[str, str]
) -> ContinuationPrompt | None:
    """Build the prompt for a call that continues a session, or ``None``.

    ``seen`` maps each section title to the content hash of what the session
    already received. The prompt has a short lead line, then every section that
    is absent from ``seen`` or has another hash, then one "No longer applies"
    section, then always the output contract. A changed section carries its
    title, so it replaces the earlier section of that title. The role
    instructions change this way when a review moves between the first-review
    rules and the re-review rules.

    "No longer applies" names each title in ``seen`` that this request does not
    carry, such as a repair context or an output rejection of an earlier round.
    Without it the lead line would let such a section look current.

    Nothing is sent that the session holds, so a round adds only what it
    changed: the polish guidance, a repair context, a new
    diff, tester report, verification report or snapshot number, prior
    findings or accepted debt, an output rejection.

    Returns ``None`` when there is no new, changed or stale section. A stale
    section counts as a change: the session must learn that it ended. The
    caller then starts a new session and sends the full prompt. Content that
    repeats exactly, such as the same rejection twice, is not new.

    ``sections_seen`` is the section map of this request alone: a stale title
    is dropped, so it is sent again if it comes back.
    """

    sections = build_prompt_sections(request)
    current = section_hashes(sections)
    changed = [
        section
        for section in sections
        if section.title != _OUTPUT_CONTRACT_TITLE and seen.get(section.title) != section.digest
    ]
    stale = sorted(title for title in seen if title not in current)
    if not changed and not stale:
        return None
    contract = next(section for section in sections if section.title == _OUTPUT_CONTRACT_TITLE)
    notices = [PromptSection(_NO_LONGER_APPLIES_TITLE, _stale_notice(stale))] if stale else []
    parts = [
        _CONTINUATION_LEAD,
        *(section.labelled_text for section in (*changed, *notices, contract)),
    ]
    return ContinuationPrompt(text="\n\n".join(parts), sections_seen=current)


def _stale_notice(titles: Sequence[str]) -> str:
    return f"These earlier sections no longer apply: {', '.join(titles)}."


#: Reviewer rules that replace the first-review rules when prior findings exist.
_REVIEWER_REPAIR_RULES = """- Review only the targeted repair.
- Return one disposition for each prior finding id.
- Use RESOLVED, UNRESOLVED, or WITHDRAWN with a concrete rationale.
- Put repair defects in repair_regressions.
- Put older newly noticed defects in blocking_findings.
- Use the repair diff as the change evidence.
- Do not omit or rename a prior finding."""


def _model_class_for(normalized_role: str, purpose: AgentPurpose) -> type[ModelBase]:
    if purpose is AgentPurpose.DECOMPOSE_PROJECT:
        return ProjectPlan
    return artifact_model_for_role(normalized_role)


def _opening(role: str, model: str, reasoning: str) -> str:
    return (
        f"You are the Software Agent Factory {role} agent.\n"
        f"Configured model: {model}\n"
        f"Configured reasoning effort: {reasoning}"
    )


def _role_instructions(
    role: str,
    purpose: AgentPurpose,
    *,
    repair_review: bool = False,
    risk_assessment: bool = True,
) -> str:
    if purpose is AgentPurpose.DECOMPOSE_PROJECT:
        return """Create the smallest sufficient DAG of reviewable work items.
- Use one task only for one bounded pull request.
- Split independent outcomes, hard prerequisites, safe parallel work, or excessive scope.
- Keep tests and related documentation with the functional task.
- Keep bootstrap work separate from substantial functional contracts.
- Make each task coherent and deterministically verifiable.
- Add a dependency only when the predecessor must be integrated or merged first.
- Omit dependencies for work that is safe in parallel worktrees.
- Persist or isolate a shared architectural choice only when independent tasks could
  otherwise make incompatible choices.
- Reuse repository capabilities. Do not add speculative work.
- Use contiguous task ids from 1. Reference earlier task ids only.
- Explain the task split, parallel waves, and merge gates in delivery_approach.
- Do not edit the repository."""
    if role == "TRIAGE":
        return _triage_instructions(risk_assessment)
    if role == "PLANNER":
        return """Write an explicit specification, then plan the smallest implementation.
- In the specification, separate facts, assumptions, and unknowns.
- Keep acceptance criteria measurable.
- Do not invent requirements, future features, unrelated refactors, or generic hardening.
- If a specification is given, return it unchanged unless new context changes it.
- Reuse existing code and extension points.
- Do not add speculative abstractions, dependencies, services, or infrastructure.
- Give concrete steps, likely files, validation, risks, and tests.
- Use repository-relative path prefixes in expected_scope.modules.
- Do not use concepts, descriptions, or glob patterns as module paths.
- Treat sibling-task constraints as hard scope boundaries.
- Report an unresolved decision only for a material requirement, interface, data, safety,
  delivery, or architecture choice.
- Report it only when existing artifacts, repository evidence, and constraints cannot
  determine the choice without inventing intent.
- Do not report ordinary implementation choices.
- Leave unresolved_decisions empty when existing evidence is sufficient.
- A replan describes the existing verified diff. It does not change the diff.
- Keep sibling outcomes outside the expected scope.
- File-count estimates are advisory. The controller enforces the hard limit."""
    if role == "IMPLEMENTER":
        return """Make the required changes in the current working directory.
- Inspect files, edit files, and run local commands.
- Do not commit, push, open a PR, or change workflow state.
- Make the narrowest change that meets the acceptance criteria.
- Reuse existing mechanisms. Do not do unrelated cleanup.
- Treat sibling-task constraints as hard scope boundaries.
- Stop when the required behavior and configured checks pass.
- Return ChangeSet metadata only."""
    if role == "TESTER":
        return """Test the implementation independently.
- Use the specification, plan, repository, diff, changed files, and deterministic results.
- Do not use or request an implementer self-assessment.
- Report concrete failures and missing tests.
- Do not require a broader redesign when the requested behavior works."""
    if role == "REVIEWER":
        common = """Review the implementation independently.
- Review correctness, regressions, security, compatibility, and scope.
- Use the diff, deterministic results, tester report, and repository.
- Do not use or request an implementer self-assessment.
- Use the work item acceptance criteria and constraints as the boundary.
- Report only concrete, high-confidence defects in the current change.
- A security finding needs a plausible exploit path.
- Do not require future features, sibling work, redesigns, or generic hardening.
- Report excess scope only when it harms the current change.
- Cite each blocker with exact paths and current line ranges.
- Leave legacy string concern fields empty. Use typed finding fields.
- Put non-blocking improvements only in suggested_changes."""
        if not repair_review:
            return (
                f"{common}\n"
                "- List all blockers in blocking_findings.\n"
                "- Leave prior_finding_dispositions and repair_regressions empty.\n"
                "- Set approved to true only when blocking_findings is empty."
            )
        return f"{common}\n{_REVIEWER_REPAIR_RULES}"
    raise ValueError(f"unsupported agent role: {role!r}")


def _triage_instructions(risk_assessment: bool) -> str:
    rationale_rule = (
        "\n- If risk is R2 or R3, provide a case-specific causal risk_rationale."
        if risk_assessment
        else ""
    )
    return f"""Assess the work item.
- Decide whether the factory can do the work.
- Set complexity and risk.{rationale_rule}
- List missing information."""


def _output_contract(
    role: str, model_class: type[ModelBase], *, risk_assessment: bool = True
) -> str:
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
        if risk_assessment:
            contract = f"{contract} When risk is R2 or R3, risk_rationale is required."
    limits = writing_limits_text(model_class)
    if limits:
        contract = f"{contract}\n{limits}"
    return (
        f"{contract}\n{model_class.__name__} JSON Schema:\n"
        f"{json.dumps(model_class.model_json_schema(), separators=(',', ':'), sort_keys=True)}"
    )


def _given(*pairs: tuple[str, object]) -> list[tuple[str, object]]:
    """Keep the ``(title, value)`` pairs whose value is not ``None``."""

    return [(title, value) for title, value in pairs if value is not None]


def _diff_or_none(diff: str | None) -> str | None:
    return _bounded_diff(diff) if diff else None


def _dump_findings(findings: Sequence[ReviewFinding]) -> list[dict[str, object]]:
    return [finding.model_dump(mode="json") for finding in findings]


def _decompose_sections(request: AgentRequest) -> list[tuple[str, object]]:
    return _given(
        ("Project brief", request.project_brief),
        ("Repository profile", request.repository_profile),
        ("Previous decomposition rejection", request.repair_context),
    )


def _triage_sections(request: AgentRequest) -> list[tuple[str, object]]:
    return _given(
        (_WORK_ITEM_TITLE, request.work_item),
        (_OUTPUT_REJECTION_TITLE, request.repair_context),
    )


def _planner_context_title(repair_context: object) -> str:
    if not isinstance(repair_context, str):
        return "Replan context"
    if "unresolved decisions" in repair_context:
        return "Clarification context"
    if repair_context.startswith("An authorized human resolved"):
        return "Human decision context"
    return "Replan context"


def _planner_sections(request: AgentRequest) -> list[tuple[str, object]]:
    return _given(
        (_WORK_ITEM_TITLE, request.work_item),
        (_TRIAGE_RESULT_TITLE, request.triage_result),
        (_SPECIFICATION_TITLE, request.specification),
        (_planner_context_title(request.repair_context), request.repair_context),
        ("Changed files so far", request.changed_files or None),
        (_CURRENT_DIFF_TITLE, _diff_or_none(request.diff)),
    )


def _implementer_sections(request: AgentRequest) -> list[tuple[str, object]]:
    return [
        *_given(
            (_WORK_ITEM_TITLE, _work_item_brief(request.work_item)),
            (_SPECIFICATION_TITLE, request.specification),
            (_EXECUTION_PLAN_TITLE, request.execution_plan),
            ("Attempt number", request.attempt_number),
            (_REPAIR_CONTEXT_TITLE, request.repair_context),
            (_CURRENT_DIFF_TITLE, _diff_or_none(request.diff)),
        ),
        *_polish_guidance_sections(request),
    ]


@cache
def _skill_template_body(name: str) -> str:
    """Return a factory skill template without its YAML frontmatter."""
    text = (
        resources.files("software_agent_factory")
        .joinpath(f"repo_templates/skills/{name}/SKILL.md")
        .read_text(encoding="utf-8")
    )
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    return text.strip()


def _polish_guidance_sections(request: AgentRequest) -> list[tuple[str, object]]:
    """The fixed guidance for the polish attempt (ADR-034). No model writes or picks it."""
    repair_context = request.repair_context
    if not (
        isinstance(repair_context, RepairContext)
        and repair_context.trigger is AttemptTrigger.POLISH
    ):
        return []
    guidance = "\n\n".join(
        (
            "The factory runs verification after this attempt. "
            "You do not need to run the pr-gate checks.",
            _skill_template_body("simplify"),
            _skill_template_body("polish"),
        )
    )
    return [
        (_POLISH_GUIDANCE_TITLE, guidance),
        *_review_lens_sections(
            request,
            rule="Fix an item only when it is a concrete defect in the current change.",
        ),
    ]


def _selected_lenses(request: AgentRequest) -> tuple[ReviewLens, ...]:
    return select_review_lenses(request.changed_files, frozenset(request.dependency_names))


def _evidence_sections(request: AgentRequest) -> list[tuple[str, object]]:
    """The sections the independent TESTER and REVIEWER both receive."""

    return _given(
        (_WORK_ITEM_TITLE, _work_item_brief(request.work_item)),
        (_SPECIFICATION_TITLE, request.specification),
        (_EXECUTION_PLAN_TITLE, request.execution_plan),
        (_OUTPUT_REJECTION_TITLE, request.repair_context),
        (_CHANGED_FILES_TITLE, request.changed_files or None),
        (_DIFF_TITLE, _diff_or_none(request.diff)),
        (_VERIFICATION_TITLE, request.verification_report),
    )


def _prior_findings_sections(request: AgentRequest) -> list[tuple[str, object]]:
    if not request.prior_review_findings:
        return []
    return [(_PRIOR_FINDINGS_TITLE, _dump_findings(request.prior_review_findings))]


def _accepted_debt_sections(request: AgentRequest) -> list[tuple[str, object]]:
    if not request.accepted_review_findings:
        return []
    return [
        (_ACCEPTED_DEBT_TITLE, _dump_findings(request.accepted_review_findings)),
        (
            _ACCEPTED_DEBT_RULE_TITLE,
            (
                "Do not report an unchanged accepted finding again. If the current "
                "repair changed its cited path and the defect remains, report it as a "
                "new blocking finding with current locations."
            ),
        ),
    ]


_REVIEWER_LENS_RULE = (
    "Check the change against each checklist. Report an item only as a "
    "concrete defect in the current change."
)


def _review_lens_sections(
    request: AgentRequest, rule: str = _REVIEWER_LENS_RULE
) -> list[tuple[str, object]]:
    """The checklists that apply to the changed files (ADR-034). No model selects them."""
    lenses = _selected_lenses(request)
    if not lenses:
        return []
    return [
        (
            _REVIEW_LENSES_TITLE,
            {
                "rule": rule,
                "lenses": render_review_lenses(lenses),
            },
        )
    ]


def _reviewer_sections(request: AgentRequest) -> list[tuple[str, object]]:
    return [
        *_evidence_sections(request),
        *_review_lens_sections(request),
        *_given(
            ("Independent tester report", request.test_report),
            ("Implementation snapshot under review", request.attempt_number),
        ),
        *_prior_findings_sections(request),
        *_accepted_debt_sections(request),
        *_given((_REPAIR_DIFF_TITLE, _diff_or_none(request.repair_diff))),
    ]


_PURPOSE_SECTIONS: dict[AgentPurpose, Callable[[AgentRequest], list[tuple[str, object]]]] = {
    AgentPurpose.DECOMPOSE_PROJECT: _decompose_sections,
}

_ROLE_SECTIONS: dict[str, Callable[[AgentRequest], list[tuple[str, object]]]] = {
    "TRIAGE": _triage_sections,
    "PLANNER": _planner_sections,
    "IMPLEMENTER": _implementer_sections,
    "TESTER": _evidence_sections,
    "REVIEWER": _reviewer_sections,
}


def _artifact_sections(normalized_role: str, request: AgentRequest) -> list[tuple[str, object]]:
    purpose_builder = _PURPOSE_SECTIONS.get(request.purpose)
    if purpose_builder is not None:
        return purpose_builder(request)
    role_builder = _ROLE_SECTIONS.get(normalized_role)
    if role_builder is None:
        raise ValueError(f"unsupported agent role: {normalized_role!r}")
    return role_builder(request)


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


def parse_test_counts(*outputs: str) -> dict[str, int]:
    combined = "\n".join(output for output in outputs if output)
    if not combined:
        return {}
    counts: dict[str, int] = {}

    passed_matches = re.findall(r"\b(\d+)\s+passed\b", combined, re.IGNORECASE)
    if passed_matches:
        counts["passed"] = int(passed_matches[-1])

    failed_matches = re.findall(r"\b(\d+)\s+failed\b", combined, re.IGNORECASE)
    if failed_matches:
        counts["failed"] = int(failed_matches[-1])

    skipped_matches = re.findall(r"\b(\d+)\s+skipped\b", combined, re.IGNORECASE)
    if skipped_matches:
        counts["skipped"] = int(skipped_matches[-1])

    xfailed_matches = re.findall(r"\b(\d+)\s+xfailed\b", combined, re.IGNORECASE)
    if xfailed_matches:
        counts["xfailed"] = int(xfailed_matches[-1])

    xpassed_matches = re.findall(r"\b(\d+)\s+xpassed\b", combined, re.IGNORECASE)
    if xpassed_matches:
        counts["xpassed"] = int(xpassed_matches[-1])

    error_matches = re.findall(r"\b(\d+)\s+error(?:s)?\b", combined, re.IGNORECASE)
    if error_matches:
        counts["errors"] = int(error_matches[-1])

    collected_matches = re.findall(r"\bcollected\s+(\d+)\s+items?\b", combined, re.IGNORECASE)
    if collected_matches:
        counts["collected"] = int(collected_matches[-1])

    cargo_match = re.search(
        r"test result: \w+\.\s+(\d+)\s+passed;\s+(\d+)\s+failed;\s+(\d+)\s+ignored",
        combined,
        re.IGNORECASE,
    )
    if cargo_match:
        counts["passed"] = int(cargo_match.group(1))
        counts["failed"] = int(cargo_match.group(2))
        counts["skipped"] = int(cargo_match.group(3))

    return counts


MAX_COLLECTION_ERRORS: int = 10
MAX_COLLECTION_ERROR_CHARS: int = 300


def parse_collection_errors(
    *outputs: str,
    limit: int = MAX_COLLECTION_ERRORS,
    max_line_chars: int = MAX_COLLECTION_ERROR_CHARS,
) -> list[str]:
    combined = "\n".join(output for output in outputs if output)
    if not combined or limit <= 0:
        return []
    errors: list[str] = []
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("ERROR collecting")
            or "CollectionError" in stripped
            or "collection error" in stripped.casefold()
            or (stripped.startswith("ERROR ") and "test" in stripped.casefold())
        ):
            bounded = stripped[:max_line_chars].rstrip()
            if bounded not in errors:
                errors.append(bounded)
                if len(errors) >= limit:
                    break
    return errors


def _bound_text(text: str, limit: int = 2000) -> str:
    if len(text) <= limit:
        return text
    tail_len = int(limit * 0.75)
    head_len = limit - tail_len
    omitted = len(text) - limit
    head = text[:head_len]
    tail = text[len(text) - tail_len :]
    return f"{head}\n...[truncated {omitted} characters]...\n{tail}"


def summarize_command_result(
    check: CommandResult,
    *,
    max_failure_chars: int = 2000,
    max_collection_errors: int = MAX_COLLECTION_ERRORS,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "command": check.command,
        "exit_code": check.exit_code,
        "duration_seconds": check.duration_seconds,
    }
    is_success = check.exit_code == 0 and not check.timed_out
    test_counts = parse_test_counts(check.stdout, check.stderr)
    if test_counts:
        summary["test_counts"] = test_counts
    collection_errors = parse_collection_errors(
        check.stdout,
        check.stderr,
        limit=max_collection_errors,
    )
    if collection_errors:
        summary["collection_errors"] = collection_errors

    if is_success:
        if check.stdout:
            stdout_hash = hashlib.sha256(check.stdout.encode("utf-8")).hexdigest()
            summary["stdout"] = f"[omitted: sha256={stdout_hash}]"
            summary["stdout_hash"] = stdout_hash
        else:
            summary["stdout"] = ""
        if check.stderr:
            stderr_hash = hashlib.sha256(check.stderr.encode("utf-8")).hexdigest()
            summary["stderr"] = f"[omitted: sha256={stderr_hash}]"
            summary["stderr_hash"] = stderr_hash
        else:
            summary["stderr"] = ""
    else:
        summary["timed_out"] = check.timed_out
        if check.stdout:
            summary["stdout"] = _bound_text(check.stdout, limit=max_failure_chars)
        else:
            summary["stdout"] = ""
        if check.stderr:
            summary["stderr"] = _bound_text(check.stderr, limit=max_failure_chars)
        else:
            summary["stderr"] = ""

    return summary


def summarize_verification_report(
    report: VerificationReport,
    *,
    max_failure_chars: int = 2000,
    max_collection_errors: int = MAX_COLLECTION_ERRORS,
) -> dict[str, object]:
    checks = [
        summarize_command_result(
            check,
            max_failure_chars=max_failure_chars,
            max_collection_errors=max_collection_errors,
        )
        for check in report.deterministic_checks
    ]
    summary: dict[str, object] = {
        "passed": report.passed,
        "confidence": report.confidence,
        "deterministic_checks": checks,
    }
    if report.failures:
        summary["failures"] = report.failures
    if report.coverage_change is not None:
        summary["coverage_change"] = report.coverage_change
    if report.test_findings:
        summary["test_findings"] = report.test_findings
    return summary


def _render(value: object) -> str:
    if isinstance(value, VerificationReport):
        return json.dumps(
            summarize_verification_report(value),
            separators=(",", ":"),
            sort_keys=True,
        )
    if isinstance(value, CommandResult):
        return json.dumps(
            summarize_command_result(value),
            separators=(",", ":"),
            sort_keys=True,
        )
    if isinstance(value, ModelBase):
        return json.dumps(
            value.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        )
    if isinstance(value, (dict, list)):
        return json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
        )
    return str(value).strip()
