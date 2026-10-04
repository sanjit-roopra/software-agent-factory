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

import os
from pathlib import Path
from typing import Callable, Protocol

from pydantic import Field, model_validator

from .models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    Complexity,
    ContextTier,
    ExecutionPlan,
    ExpectedScope,
    ModelBase,
    PerformanceRecord,
    PlanStep,
    ProjectBrief,
    ProjectPlan,
    ProjectTask,
    RepairContext,
    RepositoryProfile,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingDraft,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
    RuntimeName,
    Specification,
    TestReport,
    TriageResult,
    UsageMetrics,
    VerificationReport,
    WorkItem,
)

RUNTIME_FAILURE_REASON_LIMIT = 4000


def runtime_exception_failure_reason(exc: Exception) -> str:
    """Return a bounded diagnostic for a runtime boundary exception."""
    return f"{type(exc).__name__}: {exc}"[:RUNTIME_FAILURE_REASON_LIMIT]


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
    contract adds ``test_report``. Neither receives the implementer's
    ``ChangeSet``: deliberately no implementer self-justification.
    """

    role: AgentRole
    purpose: AgentPurpose = AgentPurpose.STANDARD
    model: str
    reasoning: str
    context_tier: ContextTier = ContextTier.DEFAULT
    runtime: RuntimeName | None = None
    """The runtime that serves this call; ``None`` uses ``--runtime`` (ADR-045)."""
    work_item: WorkItem
    triage_result: TriageResult | None = None
    specification: Specification | None = None
    execution_plan: ExecutionPlan | None = None
    diff: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    verification_report: VerificationReport | None = None
    test_report: TestReport | None = None
    prior_review_findings: list[ReviewFinding] = Field(default_factory=list)
    accepted_review_findings: list[ReviewFinding] = Field(default_factory=list)
    repair_diff: str | None = None
    repair_context: RepairContext | str | None = None
    repository_profile: RepositoryProfile | None = None
    #: Dependency names the repository declares. They select stack review lenses.
    dependency_names: tuple[str, ...] = ()
    workspace_path: str | None = None
    attempt_number: int | None = None
    timeout_seconds: int
    project_brief: ProjectBrief | None = None
    #: False when ``risk_assessment.enabled`` is off: the TRIAGE prompt then stops
    #: asking for a ``risk_rationale``.
    risk_assessment_enabled: bool = True

    @model_validator(mode="after")
    def _validate_purpose(self) -> AgentRequest:
        if self.purpose is AgentPurpose.DECOMPOSE_PROJECT:
            if self.role is not AgentRole.PLANNER:
                raise ValueError("project decomposition requires the PLANNER role")
            if self.project_brief is None:
                raise ValueError("project decomposition requires project_brief")
        return self


def workspace_cwd(request: AgentRequest) -> Path:
    """Resolve the working directory one agent runtime should run in.

    Shared by :class:`~software_agent_factory.copilot_runtime.CopilotAgentRuntime`,
    :class:`~software_agent_factory.pi_runtime.PiAgentRuntime` and
    :class:`~software_agent_factory.claude_code_runtime.ClaudeCodeAgentRuntime`
    so all apply the exact same rule: the request's workspace when supplied,
    otherwise the process's current working directory.
    """
    if request.workspace_path:
        return Path(request.workspace_path).expanduser().resolve()
    return Path(os.getcwd()).expanduser().resolve()


def validate_runtime_request(request: AgentRequest) -> None:
    """Reject a request no ``AgentRuntime`` should ever start a process for.

    Shared by :class:`~software_agent_factory.copilot_runtime.CopilotAgentRuntime`,
    :class:`~software_agent_factory.pi_runtime.PiAgentRuntime` and
    :class:`~software_agent_factory.claude_code_runtime.ClaudeCodeAgentRuntime`
    so all enforce the same runtime-neutral checks, in the same order, before
    either builds a command or starts a subprocess: an
    :attr:`AgentRole.IMPLEMENTER` request always needs a ``workspace_path``
    (there is no "current directory" an implementer should ever write to),
    and ``timeout_seconds`` must be positive (a runtime cannot bound a
    subprocess call against a non-positive deadline).
    """
    if request.role is AgentRole.IMPLEMENTER and not request.workspace_path:
        raise ValueError("IMPLEMENTER requests require workspace_path")
    if request.timeout_seconds < 1:
        raise ValueError("timeout_seconds must be at least 1")


class AgentResult(ModelBase):
    """An explicit success/failure outcome carrying at most one artifact.

    The one exception is the standard ``PLANNER`` call. It carries both
    ``specification`` and ``execution_plan`` from one ``PlanningResult``.

    ``success is False`` always requires ``failure_reason`` so the controller
    (and any persisted ``AttemptRecord``) has a human-readable explanation; it
    never has to guess why an agent failed.
    """

    role: AgentRole
    success: bool
    failure_reason: str | None = None
    triage_result: TriageResult | None = None
    specification: Specification | None = None
    project_plan: ProjectPlan | None = None
    execution_plan: ExecutionPlan | None = None
    change_set: ChangeSet | None = None
    verification_report: VerificationReport | None = None
    test_report: TestReport | None = None
    review_report: ReviewReport | None = None
    usage: UsageMetrics | None = None
    performance: PerformanceRecord | None = None

    def model_post_init(self, __context: object) -> None:
        if not self.success and not self.failure_reason:
            raise ValueError("failure_reason is required when success is False")


def is_retryable_typed_artifact_failure(
    result: AgentResult, artifact_type: type[ModelBase]
) -> bool:
    """Return whether one correction prompt can fix a typed-output failure."""
    if result.success or result.failure_reason is None:
        return False
    artifact_name = artifact_type.__name__
    return any(
        marker in result.failure_reason
        for marker in (
            f"did not validate as {artifact_name}",
            f"did not contain a parseable JSON object for {artifact_name}",
            f"did not contain a valid {artifact_name}",
        )
    )


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
        planner: AgentHook | None = None,
        implementer: AgentHook | None = None,
        tester: AgentHook | None = None,
        reviewer: AgentHook | None = None,
    ) -> None:
        self._hooks: dict[AgentRole, AgentHook] = {}
        if triage is not None:
            self._hooks[AgentRole.TRIAGE] = triage
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
        if hook is None:
            return self._default(request)
        result = hook(request)
        if (
            request.role is AgentRole.PLANNER
            and request.purpose is AgentPurpose.STANDARD
            and result.execution_plan is not None
            and result.specification is None
        ):
            # A planner hook may script only the plan. The fake adds the
            # specification that the real planner returns with it.
            result = result.model_copy(update={"specification": self._specification(request)})
        return result

    def _default(self, request: AgentRequest) -> AgentResult:
        if request.purpose is AgentPurpose.DECOMPOSE_PROJECT:
            return self._default_project_plan(request)
        if request.role is AgentRole.TRIAGE:
            return self._default_triage(request)
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
            dependencies=[],
            unknowns=[],
            confidence=0.8,
        )
        return AgentResult(role=AgentRole.TRIAGE, success=True, triage_result=triage_result)

    def _specification(self, request: AgentRequest) -> Specification:
        if request.specification is not None:
            return request.specification
        work_item = request.work_item
        return Specification(
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

    def _default_planner(self, request: AgentRequest) -> AgentResult:
        specification = self._specification(request)
        goal = specification.problem
        planned_files = request.changed_files or ["FACTORY_NOTES.md"]
        execution_plan = ExecutionPlan(
            summary=f"Implement: {request.work_item.title}",
            steps=[
                PlanStep(
                    id="implement",
                    goal=goal,
                    likely_files=planned_files,
                    validation=["Run the repository's configured verification commands."],
                )
            ],
            expected_scope=ExpectedScope(
                modules=planned_files,
                estimated_files_min=1,
                estimated_files_max=3,
            ),
            test_strategy=["Run the repository's configured verification commands."],
            risks=[],
            unresolved_decisions=[],
        )
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            specification=specification,
            execution_plan=execution_plan,
        )

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
            path = request.changed_files[0] if request.changed_files else "FACTORY_NOTES.md"
            review_report = ReviewReport(
                approved=False,
                blocking_findings=[
                    ReviewFindingDraft(
                        category=ReviewFindingCategory.CORRECTNESS,
                        message=(
                            test_report.findings[0]
                            if test_report.findings
                            else "The independent tester reported a failure."
                        ),
                        locations=[ReviewSourceLocation(path=path, start_line=1, end_line=1)],
                    )
                ],
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
