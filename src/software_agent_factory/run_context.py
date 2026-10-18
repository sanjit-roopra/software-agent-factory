"""Types the workflow controller and its parts share: the per-run context, the halt
signal, the transition error and the delivery policy fingerprint."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import FactoryConfig
from .governance import ScopeAssessment
from .models import (
    ExecutionPlan,
    ExecutionRoute,
    FactoryRun,
    RepositoryProfile,
    ReviewReport,
    RouteDecision,
    Specification,
    TestReport,
    TriageResult,
    VerificationReport,
    WorkItem,
)
from .workspace import GitWorktreeWorkspace, WorkspaceEvidence

#: Routes that synthesize the specification and the plan instead of calling the planner.
SYNTHESIZED_ROUTES = frozenset({ExecutionRoute.SINGLE, ExecutionRoute.CRITIQUE})


class Halt(Exception):
    """Internal control-flow signal: the run has already reached a terminal
    state and been persisted; unwind to the caller of ``run()``."""

    def __init__(self, run: FactoryRun) -> None:
        super().__init__(run.state)
        self.run = run


class RunContext:
    """Mutable per-run orchestration context.

    Holds only what the controller needs to keep passing to agents. It never
    holds workflow state -- that lives exclusively on the persisted
    ``FactoryRun``.
    """

    def __init__(
        self,
        *,
        work_item: WorkItem,
        triage_result: TriageResult,
        specification: Specification,
        execution_plan: ExecutionPlan,
        repository_profile: RepositoryProfile,
        workspace: GitWorktreeWorkspace,
        source_repo: Path,
        route_decision: RouteDecision | None = None,
        effective_route: ExecutionRoute = ExecutionRoute.FULL,
        original_synthesized_scope: tuple[str, ...] | None = None,
    ) -> None:
        self.work_item = work_item
        self.triage_result = triage_result
        self.specification = specification
        self.execution_plan = execution_plan
        self.repository_profile = repository_profile
        self.route_decision = route_decision
        self.effective_route = (
            route_decision.effective_route if route_decision is not None else effective_route
        )
        self.original_synthesized_scope = original_synthesized_scope
        self.polish_attempted = False
        self.workspace = workspace
        self.source_repo = source_repo
        self.latest_evidence: WorkspaceEvidence | None = None
        self.latest_verification: VerificationReport | None = None
        self.latest_test_report: TestReport | None = None
        self.latest_review: ReviewReport | None = None
        #: ``(attempt, signature)`` of each failure a repair attempt was given.
        self.repair_signatures: list[tuple[int, str]] = []


def describe_scope(scope: ScopeAssessment) -> str:
    if not scope.findings:
        return "no findings recorded"
    return "; ".join(finding.message for finding in scope.findings)


class TransitionError(Exception):
    """Raised when a caller attempts a workflow state transition that is
    not present in ``ALLOWED_TRANSITIONS``."""


def delivery_policy_fingerprint(config: FactoryConfig) -> str:
    """Bind recovery to the human policy under which the run was started."""
    payload = config.model_dump(
        mode="json",
        include={"repository", "pull_request", "ci", "merge", "risk", "scope_drift"},
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
