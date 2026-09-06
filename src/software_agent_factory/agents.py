"""Agent boundary: typed requests/results and the ``AgentRuntime`` protocol.

Implements the Phase 1 "Agent runtime abstraction" described in
``docs/architecture.md``:

```text
AgentRuntime.run(request) -> AgentResult
```

``AgentRequest`` carries the role, the configured model/reasoning (selected by
``routing.ModelRouter``, never chosen by the agent itself), whatever typed
context/artifacts are relevant to that role, an optional workspace path for
the implementer, and a timeout. ``AgentResult`` is an explicit success/failure
outcome carrying at most one typed artifact.

Per ``AGENTS.md`` ("A model does not approve its own work") and
``docs/architecture.md`` ("ChangeSet ... are not trusted agent claims"), the
``changed_files`` an implementer reports on its ``ChangeSet`` is informational
only. The workflow controller always re-derives ``changed_files`` (and the
patch) from ``workspace.collect_evidence()`` and overwrites whatever the agent
claimed; nothing in this module should be treated as authoritative evidence.

``FakeAgentRuntime`` is the Phase 1 test double described in
``docs/architecture.md`` ("Fake agents"). It is deterministic, does no
network/LLM access, and lets tests script failures, review rejection and
triage overrides by supplying a small hook callable per role. Any role
without a supplied hook falls back to a simple, deterministic default so
happy-path tests do not need to configure every role.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from pydantic import Field, model_validator

from .models import (
    REQUIRED_SKILL_TARGET_NAMES,
    AgentPurpose,
    AgentRole,
    ChangeSet,
    Complexity,
    ExecutionPlan,
    ExpectedScope,
    ModelBase,
    PlanStep,
    ProjectBrief,
    ProjectPlan,
    ProjectTask,
    RepairContext,
    RepositoryProfile,
    RepositorySkill,
    ResearchReport,
    ReviewReport,
    Risk,
    SkillGuidance,
    SkillSource,
    SkillTarget,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)


class AgentRequest(ModelBase):
    """Everything an agent invocation needs, and nothing more.

    Only the fields relevant to ``role`` are expected to be populated by
    callers; the rest stay ``None``. The controller is responsible for
    supplying ``model``/``reasoning`` from ``routing.ModelRouter`` -- agents
    never choose their own model.

    ``diff`` carries controller-derived Git evidence (never an agent's own
    description of its change), ``changed_files`` the authoritative,
    controller-derived file list, ``test_report`` the independent tester's
    judgement, and ``repair_context`` the bounded reason a repair attempt was
    started. The tester contract is ``specification`` + ``execution_plan`` +
    ``diff`` + ``changed_files`` + ``verification_report``; the reviewer
    contract adds ``test_report``. Neither receives ``change_set``:
    deliberately no implementer self-justification.
    """

    role: AgentRole
    purpose: AgentPurpose = AgentPurpose.STANDARD
    model: str
    reasoning: str
    work_item: WorkItem
    triage_result: TriageResult | None = None
    specification: Specification | None = None
    research_report: ResearchReport | None = None
    execution_plan: ExecutionPlan | None = None
    change_set: ChangeSet | None = None
    diff: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    verification_report: VerificationReport | None = None
    test_report: TestReport | None = None
    repair_context: RepairContext | str | None = None
    repository_profile: RepositoryProfile | None = None
    repository_skill: RepositorySkill | None = None
    official_documentation_origins: list[str] = Field(default_factory=list)
    practice_reference_urls: list[str] = Field(default_factory=list)
    workspace_path: str | None = None
    attempt_number: int | None = None
    timeout_seconds: int
    project_brief: ProjectBrief | None = None

    @model_validator(mode="after")
    def _validate_purpose(self) -> AgentRequest:
        if self.purpose is AgentPurpose.DECOMPOSE_PROJECT:
            if self.role is not AgentRole.PLANNER:
                raise ValueError("project decomposition requires the PLANNER role")
            if self.project_brief is None:
                raise ValueError("project decomposition requires project_brief")
        elif self.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            if self.role is not AgentRole.RESEARCHER:
                raise ValueError("repository skill generation requires the RESEARCHER role")
            if self.repository_profile is None:
                raise ValueError("repository skill generation requires repository_profile")
        if (
            self.purpose is not AgentPurpose.GENERATE_REPOSITORY_SKILL
            and (self.official_documentation_origins or self.practice_reference_urls)
        ):
            raise ValueError(
                "research URL configuration is only valid for repository skill generation"
            )
        return self


class AgentResult(ModelBase):
    """An explicit success/failure outcome carrying at most one artifact.

    ``success is False`` always requires ``failure_reason`` so the controller
    (and any persisted ``AttemptRecord``) has a human-readable explanation; it
    never has to guess why an agent failed.
    """

    role: AgentRole
    success: bool
    failure_reason: str | None = None
    triage_result: TriageResult | None = None
    specification: Specification | None = None
    research_report: ResearchReport | None = None
    repository_skill: RepositorySkill | None = None
    project_plan: ProjectPlan | None = None
    execution_plan: ExecutionPlan | None = None
    change_set: ChangeSet | None = None
    verification_report: VerificationReport | None = None
    test_report: TestReport | None = None
    review_report: ReviewReport | None = None

    def model_post_init(self, __context: object) -> None:
        if not self.success and not self.failure_reason:
            raise ValueError("failure_reason is required when success is False")


class AgentRuntime(Protocol):
    """The only boundary between the factory and model inference.

    ``docs/architecture.md`` requires the domain/workflow layers to depend
    only on this protocol, never on Copilot-specific SDK objects. The
    production implementation (``CopilotAgentRuntime``) arrives in Phase 2;
    Phase 1 tests use ``FakeAgentRuntime`` exclusively.
    """

    def run(self, request: AgentRequest) -> AgentResult: ...


AgentHook = Callable[[AgentRequest], AgentResult]


class FakeAgentRuntime:
    """Deterministic test double for :class:`AgentRuntime`.

    Every role has a simple, deterministic default behavior. Tests that need
    to script failures, reviewer rejection or triage overrides supply a hook
    callable for the relevant role; the hook receives the full
    :class:`AgentRequest` (including ``attempt_number``) and returns the
    :class:`AgentResult` to use instead of the default, so tests can vary
    behavior by attempt (e.g. fail twice, then succeed) without a bespoke
    scripting DSL.
    """

    def __init__(
        self,
        *,
        triage: AgentHook | None = None,
        refiner: AgentHook | None = None,
        researcher: AgentHook | None = None,
        planner: AgentHook | None = None,
        implementer: AgentHook | None = None,
        tester: AgentHook | None = None,
        reviewer: AgentHook | None = None,
    ) -> None:
        self._hooks: dict[AgentRole, AgentHook] = {}
        if triage is not None:
            self._hooks[AgentRole.TRIAGE] = triage
        if refiner is not None:
            self._hooks[AgentRole.REFINER] = refiner
        if researcher is not None:
            self._hooks[AgentRole.RESEARCHER] = researcher
        if planner is not None:
            self._hooks[AgentRole.PLANNER] = planner
        if implementer is not None:
            self._hooks[AgentRole.IMPLEMENTER] = implementer
        if tester is not None:
            self._hooks[AgentRole.TESTER] = tester
        if reviewer is not None:
            self._hooks[AgentRole.REVIEWER] = reviewer

    def run(self, request: AgentRequest) -> AgentResult:
        hook = self._hooks.get(request.role)
        if hook is not None:
            return hook(request)
        return self._default(request)

    def _default(self, request: AgentRequest) -> AgentResult:
        if request.purpose is AgentPurpose.DECOMPOSE_PROJECT:
            return self._default_project_plan(request)
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            return self._default_repository_skill(request)
        if request.role is AgentRole.TRIAGE:
            return self._default_triage(request)
        if request.role is AgentRole.REFINER:
            return self._default_refiner(request)
        if request.role is AgentRole.RESEARCHER:
            return self._default_researcher(request)
        if request.role is AgentRole.PLANNER:
            return self._default_planner(request)
        if request.role is AgentRole.IMPLEMENTER:
            return self._default_implementer(request)
        if request.role is AgentRole.TESTER:
            return self._default_tester(request)
        return self._default_reviewer(request)

    # -- defaults ----------------------------------------------------

    def _default_project_plan(self, request: AgentRequest) -> AgentResult:
        brief = request.project_brief
        if brief is None:  # pragma: no cover - guarded by AgentRequest
            raise ValueError("project decomposition requires project_brief")
        project_plan = ProjectPlan(
            project_id=brief.id,
            summary=f"Deliver: {brief.title}",
            delivery_approach=(
                "Use one coherent work item because the fake runtime has no evidence that "
                "separate delivery or dependency boundaries are required."
            ),
            tasks=(
                ProjectTask(
                    id=1,
                    title=brief.title,
                    description=brief.description,
                    acceptance_criteria=tuple(brief.acceptance_criteria)
                    or ("The project description is fully implemented.",),
                    constraints=tuple(brief.constraints),
                ),
            ),
        )
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=project_plan,
        )

    def _default_triage(self, request: AgentRequest) -> AgentResult:
        triage_result = TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R1,
            requirements_quality="clear",
            needs_research=False,
            dependencies=[],
            unknowns=[],
            confidence=0.8,
        )
        return AgentResult(role=AgentRole.TRIAGE, success=True, triage_result=triage_result)

    def _default_refiner(self, request: AgentRequest) -> AgentResult:
        work_item = request.work_item
        specification = Specification(
            problem=work_item.description,
            acceptance_criteria=list(work_item.acceptance_criteria)
            or ["The implementation satisfies the work item description."],
            constraints=list(work_item.constraints),
            assumptions=["The repository's existing behavior outside this task remains valid."],
            unknowns=[],
            dependencies=[],
            risk_flags=[],
            confidence=0.8,
        )
        return AgentResult(role=AgentRole.REFINER, success=True, specification=specification)

    def _default_researcher(self, request: AgentRequest) -> AgentResult:
        specification = request.specification
        question = (
            specification.problem if specification is not None else request.work_item.description
        )
        research_report = ResearchReport(
            question=question,
            findings=["No external research was required for this deterministic fake run."],
            evidence=[],
            implications=["Proceed with planning using the existing specification."],
            uncertainty=[],
        )
        return AgentResult(role=AgentRole.RESEARCHER, success=True, research_report=research_report)

    def _default_repository_skill(self, request: AgentRequest) -> AgentResult:
        profile = request.repository_profile
        if profile is None:
            return AgentResult(
                role=AgentRole.RESEARCHER,
                success=False,
                failure_reason="repository skill generation requires a repository profile",
            )
        important_names = set(REQUIRED_SKILL_TARGET_NAMES)
        source_candidates = {
            "python": ("https://docs.python.org", "Official Python documentation"),
            "pytest": ("https://docs.pytest.org", "Official pytest documentation"),
            "react": ("https://react.dev", "Official React documentation"),
            "react-dom": ("https://react.dev", "Official React documentation"),
            "vite": ("https://vite.dev", "Official Vite documentation"),
            "vitest": ("https://vitest.dev", "Official Vitest documentation"),
        }
        groundable_names = {
            name
            for name, (url, _) in source_candidates.items()
            if url in request.official_documentation_origins
        }
        target_dependencies = sorted(
            (
                dependency
                for dependency in profile.dependencies
                if dependency.name in groundable_names
            ),
            key=lambda dependency: (
                dependency.name not in important_names,
                dependency.ecosystem,
                dependency.name,
                dependency.manifest_path,
            ),
        )[:24]
        targets = tuple(
            SkillTarget(
                ecosystem=dependency.ecosystem,
                name=dependency.name,
                declared_version=dependency.declared_version,
                resolved_version=dependency.resolved_version,
                evidence=tuple(
                    path
                    for path in (dependency.manifest_path, dependency.resolution_path)
                    if path is not None
                ),
            )
            for dependency in target_dependencies
        )
        sources: list[SkillSource] = []
        grouped: dict[str, tuple[str, list[str], list[str]]] = {}
        for target in targets:
            candidate = source_candidates.get(target.name)
            if candidate is None:
                continue
            url, title = candidate
            if url not in request.official_documentation_origins:
                continue
            _, names, scopes = grouped.setdefault(url, (title, [], []))
            if target.name not in names:
                names.append(target.name)
            scope = target.resolved_version or target.declared_version
            if scope not in scopes:
                scopes.append(scope)
        for url, (title, names, scopes) in grouped.items():
            sources.append(
                SkillSource(
                    title=title,
                    url=url,
                    version_scope=", ".join(scopes)[:200],
                    applies_to=tuple(names),
                )
            )
        repository_skill = RepositorySkill(
            dependency_fingerprint=profile.dependency_fingerprint,
            targets=targets,
            official_sources=tuple(sources),
            simplify=SkillGuidance(
                summary="Simplify the changed code while preserving behavior.",
                guidance=(
                    "Prefer direct control flow and existing repository abstractions.",
                    "Remove only complexity that is unnecessary for the requested behavior.",
                ),
                avoid=("Do not change tests, public interfaces, dependencies, or behavior.",),
                validation=("Use the repository's configured deterministic checks.",),
            ),
            polish=SkillGuidance(
                summary="Polish the changed code for the detected dependency versions.",
                guidance=(
                    "Apply only practices compatible with the detected dependency declarations.",
                    "Prefer a no-op over an unsupported version-specific assumption.",
                ),
                avoid=("Do not add dependencies or expand the requested scope.",),
                validation=("Use the repository's configured deterministic checks.",),
            ),
            uncertainties=("The fake runtime uses deterministic official-source fixtures.",),
        )
        return AgentResult(
            role=AgentRole.RESEARCHER,
            success=True,
            repository_skill=repository_skill,
        )

    def _default_planner(self, request: AgentRequest) -> AgentResult:
        specification = request.specification
        goal = specification.problem if specification is not None else request.work_item.description
        execution_plan = ExecutionPlan(
            summary=f"Implement: {request.work_item.title}",
            steps=[
                PlanStep(
                    id="implement",
                    goal=goal,
                    likely_files=[],
                    validation=["Run the repository's configured verification commands."],
                )
            ],
            expected_scope=ExpectedScope(modules=[], estimated_files_min=1, estimated_files_max=3),
            test_strategy=["Run the repository's configured verification commands."],
            risks=[],
        )
        return AgentResult(role=AgentRole.PLANNER, success=True, execution_plan=execution_plan)

    def _default_implementer(self, request: AgentRequest) -> AgentResult:
        if request.workspace_path is None:
            raise ValueError("IMPLEMENTER requests require a workspace_path")

        workspace_path = Path(request.workspace_path)
        attempt_number = request.attempt_number or 1
        repair_context = request.repair_context
        if isinstance(repair_context, RepairContext):
            repair_note = f"{repair_context.trigger.value}: {repair_context.summary}"
        else:
            repair_note = repair_context or "none"
        note_path = workspace_path / "FACTORY_NOTES.md"
        note_path.write_text(
            "# Factory change\n\n"
            f"Work item: {request.work_item.id}\n"
            f"Attempt: {attempt_number}\n"
            f"Model: {request.model}\n"
            f"Repair: {repair_note}\n",
            encoding="utf-8",
        )
        change_set = ChangeSet(
            summary=f"Recorded a deterministic change for {request.work_item.id}.",
            changed_files=[note_path.name],
            tests_added=[],
            commands_run=[],
        )
        return AgentResult(role=AgentRole.IMPLEMENTER, success=True, change_set=change_set)

    def _default_tester(self, request: AgentRequest) -> AgentResult:
        test_report = TestReport(
            passed=True,
            findings=["No issues found."],
            suggested_tests=[],
            confidence=0.9,
        )
        return AgentResult(role=AgentRole.TESTER, success=True, test_report=test_report)

    def _default_reviewer(self, request: AgentRequest) -> AgentResult:
        test_report = request.test_report
        if test_report is not None and not test_report.passed:
            review_report = ReviewReport(
                approved=False,
                findings=list(test_report.findings)
                or ["The independent tester reported a failure."],
                suggested_changes=list(test_report.suggested_tests),
            )
            return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=review_report)

        review_report = ReviewReport(
            approved=True,
            findings=[],
            scope_concerns=[],
            security_concerns=[],
            compatibility_concerns=[],
            suggested_changes=[],
        )
        return AgentResult(role=AgentRole.REVIEWER, success=True, review_report=review_report)
