"""Grant one more attempt after the implementation budget ran out (ADR-058)."""

from collections.abc import Callable
from pathlib import Path

import pytest
from factory_testing import build_config, work_item

from software_agent_factory import run_reopen
from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.governance import RepositoryVerificationResult
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    DashboardResumeRequest,
    EscalationStatus,
    FactoryRun,
    ResumeClassification,
    VerificationReport,
    WorkflowState,
    utc_now,
)
from software_agent_factory.resume_classes import is_valid_attempt_grant_context
from software_agent_factory.resume_writes import ingest_dashboard_request
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

GRANT = ResumeClassification.ATTEMPT_GRANT


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


class SwitchVerifier:
    """Verification that fails until a test lets it pass."""

    def __init__(self) -> None:
        self.passed = False

    def run(self, *args: object, **kwargs: object) -> RepositoryVerificationResult:
        failures = [] if self.passed else ["unit tests failed"]
        return RepositoryVerificationResult(
            report=VerificationReport(passed=self.passed, failures=failures, confidence=1.0),
            command_logs=(),
            failure_kind=None,
            failed_phase=None,
            failed_command=None,
        )


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.config: FactoryConfig = build_config(
            tmp_path / "data", same_model_attempts=1, max_total_attempts=2
        )
        self.store = FileRunStore(self.config.data_dir)
        self.verifier = SwitchVerifier()
        self.requests: list[AgentRequest] = []
        default = FakeAgentRuntime()

        def implementer(request: AgentRequest) -> AgentResult:
            self.requests.append(request)
            return default.run(request)

        self.controller = WorkflowController(
            self.config,
            self.store,
            FakeAgentRuntime(implementer=implementer),
            repository_verifier=self.verifier,
        )

    def halt(self, source_repo: Path) -> FactoryRun:
        return self.controller.run(work_item(), source_repo)

    def grant(self, run: FactoryRun, fingerprint: str | None = None) -> None:
        assert run.escalation is not None
        context = run.escalation.attempt_grant_context
        assert context is not None
        request = DashboardResumeRequest(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            context_fingerprint=fingerprint or context.context_fingerprint,
            action=GRANT,
        )
        assert self.store.create_dashboard_request(run.id, request)
        ingest_dashboard_request(run, self.store, self.config, utc_now())


def _implementer_attempts(run: FactoryRun) -> int:
    return sum(1 for attempt in run.attempt_records if attempt.role is AgentRole.IMPLEMENTER)


def test_an_implementation_budget_halt_offers_one_more_attempt(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path)

    halted = rig.halt(source_repo)

    assert halted.state is WorkflowState.NEEDS_HUMAN
    assert (halted.failure_reason or "").startswith("implementation attempt budget exhausted")
    assert halted.escalation is not None
    assert halted.escalation.resume_classification is GRANT
    assert halted.escalation.remote_resume_enabled is False
    context = halted.escalation.attempt_grant_context
    assert context is not None
    assert context.budget is AttemptBudget.IMPLEMENTATION
    assert context.attempts_used == 2
    assert context.last_failure.startswith("Deterministic")
    assert is_valid_attempt_grant_context(context, halted.id, halted.escalation.episode_id)
    assert halted.exhausted_repair is not None
    assert halted.exhausted_repair.failures == ["unit tests failed"]


def test_a_granted_run_makes_exactly_one_attempt_and_can_finish(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.verifier.passed = True
    rig.grant(halted)

    granted = rig.controller.reopen(halted.id, source_repo)

    assert granted.state is WorkflowState.PR_READY, granted.failure_reason
    assert _implementer_attempts(granted) == 3
    assert granted.attempt_grants == {AttemptBudget.IMPLEMENTATION: 1}
    assert granted.exhausted_repair is None
    assert granted.escalation is not None
    assert granted.escalation.status is EscalationStatus.RESUMED
    assert granted.escalation.reopen_count == 1
    assert granted.attempt_records[-1].triggered_by is AttemptTrigger.OPERATOR
    repair = rig.requests[-1].repair_context
    assert repair is not None
    assert repair.trigger is AttemptTrigger.OPERATOR
    assert "unit tests failed" in repair.failures


def test_a_granted_attempt_that_fails_halts_again_for_another_grant(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.grant(halted)

    again = rig.controller.reopen(halted.id, source_repo)

    assert again.state is WorkflowState.NEEDS_HUMAN
    assert _implementer_attempts(again) == 3
    assert again.failure_reason == "implementation attempt budget exhausted after 3 attempt(s)"
    assert again.escalation is not None
    assert again.escalation.resume_classification is GRANT
    assert again.escalation.episode_number == 2
    assert again.escalation.attempt_grant_context is not None
    assert again.escalation.attempt_grant_context.attempts_used == 3


def test_a_grant_for_an_old_context_goes_stale_and_reopens_nothing(
    tmp_path: Path, source_repo: Path
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)

    rig.grant(halted, fingerprint="f" * 64)

    run = rig.store.load_run(halted.id)
    assert run.escalation is not None
    assert run.escalation.status is not EscalationStatus.REOPENED
    (request,) = rig.store.list_dashboard_requests(run.id, run.escalation.episode_id)
    assert (request.status, request.reason) == ("stale", "context_changed")


def _with_escalation(run: FactoryRun, **update: object) -> FactoryRun:
    assert run.escalation is not None
    return run.model_copy(update={"escalation": run.escalation.model_copy(update=update)})


def _without_context(run: FactoryRun) -> FactoryRun:
    return _with_escalation(run, attempt_grant_context=None)


def _with_other_receipt_fingerprint(run: FactoryRun) -> FactoryRun:
    assert run.escalation is not None
    receipts = [
        receipt.model_copy(update={"attempt_grant_context_fingerprint": "d" * 64})
        for receipt in run.escalation.accepted_replies
    ]
    return _with_escalation(run, accepted_replies=receipts)


def _with_fewer_attempts(run: FactoryRun) -> FactoryRun:
    return run.model_copy(update={"attempt_records": run.attempt_records[:-1]})


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (_without_context, "missing or invalid attempt grant context"),
        (_with_other_receipt_fingerprint, "receipt fingerprint does not match"),
        (_with_fewer_attempts, "no longer matches its attempt grant context"),
    ],
    ids=["no-context", "other-receipt", "other-attempts"],
)
def test_a_grant_that_no_longer_matches_its_context_cannot_reopen_the_run(
    tmp_path: Path,
    source_repo: Path,
    tamper: Callable[[FactoryRun], FactoryRun],
    message: str,
) -> None:
    rig = Rig(tmp_path)
    halted = rig.halt(source_repo)
    rig.grant(halted)
    reopening = tamper(rig.store.load_run(halted.id))

    with pytest.raises(ValueError, match=message):
        run_reopen.transition_reopened(rig.controller, reopening)


@pytest.mark.parametrize(
    "reason",
    ["stalled: no progress within the stall timeout", "workspace identity changed"],
)
def test_other_halts_are_not_attempt_grants(reason: str) -> None:
    run = FactoryRun(
        id="run-x", work_item_id="task-x", state=WorkflowState.NEEDS_HUMAN, failure_reason=reason
    )

    assert classify_halt_reason(run)[0] is ResumeClassification.NOT_RESUMABLE
