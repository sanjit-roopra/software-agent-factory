"""Risk assessment switch: ``risk_assessment.enabled`` turns the approval gate and rationale off."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_workflow import FakeAgentRuntime, RecordingRuntime, _config, _triage_hook, _work_item

from software_agent_factory.agents import AgentRequest, AgentResult
from software_agent_factory.config import FactoryConfig, RiskAssessmentConfig
from software_agent_factory.models import (
    AgentRole,
    Complexity,
    Risk,
    TriageResult,
    WorkflowState,
)
from software_agent_factory.prompts import build_prompt
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _config_with(data_dir: Path, *, enabled: bool) -> FactoryConfig:
    return _config(data_dir).model_copy(
        update={"risk_assessment": RiskAssessmentConfig(enabled=enabled)}
    )


def _triage_without_rationale(risk: Risk):
    def hook(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=Complexity.L1,
                risk=risk,
                needs_research=False,
                confidence=0.8,
            ),
        )

    return hook


@pytest.mark.parametrize("risk", [Risk.R2, Risk.R3])
def test_disabled_run_passes_the_risk_gate_and_reaches_pr_ready(
    risk: Risk, source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config_with(data_dir, enabled=False),
        store,
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, risk)),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.escalation is None
    assert store.load_run(run.id).risk_assessment_enabled is False
    assert store.load_artifact(run.id, TriageResult).risk is risk


@pytest.mark.parametrize("risk", [Risk.R2, Risk.R3])
def test_disabled_run_accepts_triage_without_a_rationale(
    risk: Risk, source_repo: Path, data_dir: Path
) -> None:
    store = FileRunStore(data_dir)
    runtime = RecordingRuntime(FakeAgentRuntime(triage=_triage_without_rationale(risk)))
    controller = WorkflowController(_config_with(data_dir, enabled=False), store, runtime)

    run = controller.run(_work_item(), source_repo)

    triage_requests = [r for r in runtime.requests if r.role is AgentRole.TRIAGE]
    assert run.state is WorkflowState.PR_READY
    assert len(triage_requests) == 1
    assert store.load_artifact(run.id, TriageResult).risk_rationale is None


def test_disabled_run_asks_triage_for_no_rationale(source_repo: Path, data_dir: Path) -> None:
    runtime = RecordingRuntime(FakeAgentRuntime())
    controller = WorkflowController(
        _config_with(data_dir, enabled=False), FileRunStore(data_dir), runtime
    )

    controller.run(_work_item(), source_repo)

    triage_request = next(r for r in runtime.requests if r.role is AgentRole.TRIAGE)
    assert triage_request.risk_assessment_enabled is False
    assert "risk_rationale is required" not in build_prompt(triage_request)


def test_enabled_run_still_stops_at_the_risk_gate(source_repo: Path, data_dir: Path) -> None:
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        _config_with(data_dir, enabled=True),
        store,
        FakeAgentRuntime(triage=_triage_hook(Complexity.L1, Risk.R2)),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason == "risk R2 requires human approval"
    assert run.risk_assessment_enabled is True


def test_enabled_run_retries_then_fails_a_triage_without_a_rationale(
    source_repo: Path, data_dir: Path
) -> None:
    runtime = RecordingRuntime(FakeAgentRuntime(triage=_triage_without_rationale(Risk.R2)))
    controller = WorkflowController(
        _config_with(data_dir, enabled=True), FileRunStore(data_dir), runtime
    )

    run = controller.run(_work_item(), source_repo)

    triage_requests = [r for r in runtime.requests if r.role is AgentRole.TRIAGE]
    assert run.state is WorkflowState.FAILED
    assert "risk_rationale is required when risk is R2" in (run.failure_reason or "")
    assert len(triage_requests) == 2
    assert triage_requests[0].repair_context is None
    assert "risk_rationale is required" in str(triage_requests[1].repair_context)


def test_enabled_run_accepts_a_rationale_supplied_on_the_retry(
    source_repo: Path, data_dir: Path
) -> None:
    with_rationale = _triage_hook(Complexity.L1, Risk.R2)
    without_rationale = _triage_without_rationale(Risk.R2)

    def hook(request: AgentRequest) -> AgentResult:
        return (without_rationale if request.attempt_number == 1 else with_rationale)(request)

    controller = WorkflowController(
        _config_with(data_dir, enabled=True),
        FileRunStore(data_dir),
        FakeAgentRuntime(triage=hook),
    )

    run = controller.run(_work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.failure_reason == "risk R2 requires human approval"
