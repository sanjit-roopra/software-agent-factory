"""Per-role runtime choice (ADR-045 slice 2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from factory_testing import build_config, work_item

from software_agent_factory import cli
from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig, RoleModelConfig
from software_agent_factory.models import (
    AgentRole,
    Complexity,
    ModelUsage,
    RuntimeName,
    UsageMetrics,
    WorkflowState,
)
from software_agent_factory.routing import ModelRouter
from software_agent_factory.runtime_router import RoutingAgentRuntime
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

Calls = list[tuple[RuntimeName, AgentRole]]


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _recording(name: RuntimeName, calls: Calls, built: list[RuntimeName]) -> FakeAgentRuntime:
    built.append(name)
    delegate = FakeAgentRuntime()

    def record(request: AgentRequest) -> AgentResult:
        calls.append((name, request.role))
        return delegate.run(request)

    return FakeAgentRuntime(
        triage=record, planner=record, implementer=record, tester=record, reviewer=record
    )


def _install_recording_runtimes(monkeypatch: pytest.MonkeyPatch) -> tuple[Calls, list[RuntimeName]]:
    calls: Calls = []
    built: list[RuntimeName] = []
    seams = {
        "CopilotAgentRuntime": RuntimeName.COPILOT,
        "PiAgentRuntime": RuntimeName.PI,
        "ClaudeCodeAgentRuntime": RuntimeName.CLAUDE_CODE,
    }
    for seam, name in seams.items():
        # double-waiver: B1 — real runtimes start paid agent subprocesses
        monkeypatch.setattr(cli, seam, lambda *_args, _name=name: _recording(_name, calls, built))
    return calls, built


def _with_runtimes(config: FactoryConfig, **runtimes: RuntimeName) -> FactoryConfig:
    payload = config.model_dump(mode="json")
    for role, runtime in runtimes.items():
        payload["models"][role]["runtime"] = runtime.value
    return FactoryConfig.model_validate(payload)


def test_each_role_runs_on_the_runtime_it_names(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, data_dir: Path
) -> None:
    calls, built = _install_recording_runtimes(monkeypatch)
    config = _with_runtimes(
        build_config(data_dir), triage=RuntimeName.PI, planner=RuntimeName.CLAUDE_CODE
    )
    runtime = cli._build_runtime(cli.RuntimeChoice.COPILOT, config)

    run = WorkflowController(config, FileRunStore(data_dir), runtime).run(work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    served = {role: name for name, role in calls}
    assert served == {
        AgentRole.TRIAGE: RuntimeName.PI,
        AgentRole.PLANNER: RuntimeName.CLAUDE_CODE,
        AgentRole.IMPLEMENTER: RuntimeName.COPILOT,
        AgentRole.TESTER: RuntimeName.COPILOT,
        AgentRole.REVIEWER: RuntimeName.COPILOT,
    }
    assert len(built) == len(set(built))  # each runtime built once


def test_fake_runtime_ignores_role_runtimes(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, data_dir: Path
) -> None:
    calls, built = _install_recording_runtimes(monkeypatch)
    config = _with_runtimes(build_config(data_dir), triage=RuntimeName.PI)

    runtime = cli._build_runtime(cli.RuntimeChoice.FAKE, config)
    run = WorkflowController(config, FileRunStore(data_dir), runtime).run(work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert (calls, built) == ([], [])


def test_router_builds_only_the_runtimes_requests_name() -> None:
    built: list[RuntimeName] = []
    calls: Calls = []
    router = RoutingAgentRuntime(
        RuntimeName.COPILOT,
        {name: (lambda name=name: _recording(name, calls, built)) for name in RuntimeName},
    )
    request = AgentRequest(
        role=AgentRole.TRIAGE,
        model="m",
        reasoning="high",
        work_item=work_item(),
        timeout_seconds=60,
    )

    router.run(request)
    router.run(request.model_copy(update={"runtime": RuntimeName.PI}))
    router.run(request)

    assert built == [RuntimeName.COPILOT, RuntimeName.PI]
    assert [name for name, _ in calls] == [RuntimeName.COPILOT, RuntimeName.PI, RuntimeName.COPILOT]


def test_router_names_the_serving_runtime_in_model_usage() -> None:
    usage = UsageMetrics(model_usage=(ModelUsage(model="m"),))

    def triage(request: AgentRequest) -> AgentResult:
        return FakeAgentRuntime().run(request).model_copy(update={"usage": usage})

    router = RoutingAgentRuntime(
        RuntimeName.COPILOT, {RuntimeName.PI: lambda: FakeAgentRuntime(triage=triage)}
    )
    request = AgentRequest(
        role=AgentRole.TRIAGE,
        model="m",
        reasoning="high",
        timeout_seconds=60,
        runtime=RuntimeName.PI,
        work_item=work_item(),
    )

    result = router.run(request)

    assert result.usage is not None
    assert [u.runtime for u in result.usage.model_usage] == [RuntimeName.PI]


def test_escalation_treats_a_runtime_change_as_a_new_model(data_dir: Path) -> None:
    payload = build_config(data_dir).model_dump(mode="json")
    payload["models"]["workers"]["L3"]["runtime"] = RuntimeName.CLAUDE_CODE.value
    router = ModelRouter(FactoryConfig.model_validate(payload))

    distinct = router._distinct_worker_models(Complexity.L2)

    assert [config.runtime for config in distinct] == [None, RuntimeName.CLAUDE_CODE]


def test_claude_code_role_rejects_an_unknown_effort() -> None:
    with pytest.raises(ValueError, match="runtime claude-code accepts reasoning"):
        RoleModelConfig(model="claude-opus-5", reasoning="minimal", runtime="claude-code")


def test_doctor_runtimes_cover_roles_and_the_default(data_dir: Path) -> None:
    config = _with_runtimes(build_config(data_dir), triage=RuntimeName.PI)

    assert config.runtimes_for(None) == frozenset()
    assert config.runtimes_for(RuntimeName.COPILOT) == {RuntimeName.COPILOT, RuntimeName.PI}
