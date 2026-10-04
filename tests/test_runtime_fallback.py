"""Fallback when a runtime cannot serve a call (ADR-045 slice 3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from factory_testing import build_config, work_item

from software_agent_factory.agents import (
    AgentRequest,
    AgentResult,
    AgentRuntime,
    FakeAgentRuntime,
)
from software_agent_factory.config import FactoryConfig
from software_agent_factory.models import (
    AgentRole,
    ContextTier,
    ModelUsage,
    RuntimeFallback,
    RuntimeName,
    UsageMetrics,
    WorkflowState,
)
from software_agent_factory.runtime_router import RoutingAgentRuntime
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

Hook = Callable[[AgentRequest], AgentResult]

PI_FALLBACK = RuntimeFallback(
    runtime=RuntimeName.PI,
    model="gpt-5.6-terra",
    reasoning="medium",
    context_tier=ContextTier.LONG_CONTEXT,
)


def _unavailable(request: AgentRequest) -> AgentResult:
    return AgentResult(
        role=request.role,
        success=False,
        failure_reason="claude reported an error (error): usage limit",
        runtime_unavailable=True,
    )


def _wrong_result(request: AgentRequest) -> AgentResult:
    return AgentResult(role=request.role, success=False, failure_reason="bad artifact")


class _Recorder:
    """Runtimes keyed by name. Each records the requests it serves."""

    def __init__(self, triage: dict[RuntimeName, Hook]) -> None:
        self.served: list[tuple[RuntimeName, AgentRequest]] = []
        self._triage = triage

    def router(self) -> RoutingAgentRuntime:
        factories: dict[RuntimeName, Callable[[], AgentRuntime]] = {
            name: (lambda name=name: self._runtime(name)) for name in RuntimeName
        }
        return RoutingAgentRuntime(RuntimeName.COPILOT, factories)

    def _runtime(self, name: RuntimeName) -> FakeAgentRuntime:
        delegate = self._triage.get(name)

        def triage(request: AgentRequest) -> AgentResult:
            self.served.append((name, request))
            if delegate is not None:
                return delegate(request)
            usage = UsageMetrics(model_usage=(ModelUsage(model=request.model),))
            return FakeAgentRuntime().run(request).model_copy(update={"usage": usage})

        return FakeAgentRuntime(triage=triage)


def _request(**overrides: object) -> AgentRequest:
    fields: dict[str, object] = {
        "role": AgentRole.TRIAGE,
        "model": "claude-opus-5-5",
        "reasoning": "high",
        "runtime": RuntimeName.CLAUDE_CODE,
        "fallback": PI_FALLBACK,
        "attempt_number": 2,
        "work_item": work_item(),
        "timeout_seconds": 60,
    }
    fields.update(overrides)
    return AgentRequest.model_validate(fields)


def test_unavailable_runtime_hands_the_same_request_to_its_fallback() -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable})
    router = recorder.router()
    request = _request()

    result = router.run(request)

    assert result.success is True
    assert [name for name, _ in recorder.served] == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]
    served = recorder.served[1][1]
    assert (served.model, served.reasoning, served.context_tier) == (
        "gpt-5.6-terra",
        "medium",
        ContextTier.LONG_CONTEXT,
    )
    assert served.fallback is None
    assert served.attempt_number == request.attempt_number
    routing = {"runtime", "model", "reasoning", "context_tier", "fallback"}
    assert served.model_dump(exclude=routing) == request.model_dump(exclude=routing)
    assert result.usage is not None
    assert [(u.runtime, u.model) for u in result.usage.model_usage] == [
        (RuntimeName.PI, "gpt-5.6-terra")
    ]


def test_fallback_is_logged_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable})
    router = recorder.router()

    router.run(_request())

    assert (
        "TRIAGE: runtime claude-code is unavailable; falling back to pi gpt-5.6-terra: "
        "claude reported an error (error): usage limit"
    ) in caplog.text


@pytest.mark.parametrize(
    ("first", "fallback"),
    [(_wrong_result, PI_FALLBACK), (_unavailable, None)],
    ids=["model-failure", "no-fallback"],
)
def test_result_is_returned_as_is_without_unavailability_or_fallback(
    first: Hook, fallback: RuntimeFallback | None
) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: first})
    router = recorder.router()

    result = router.run(_request(fallback=fallback))

    assert result.success is False
    assert [name for name, _ in recorder.served] == [RuntimeName.CLAUDE_CODE]


def test_fallback_is_tried_once() -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable, RuntimeName.PI: _unavailable})
    router = recorder.router()

    result = router.run(_request())

    assert result.runtime_unavailable is True
    assert [name for name, _ in recorder.served] == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]


def _config_with_triage_fallback(data_dir: Path) -> FactoryConfig:
    payload = build_config(data_dir).model_dump(mode="json")
    payload["models"]["triage"].update(
        runtime=RuntimeName.CLAUDE_CODE.value,
        reasoning="high",
        fallback={"runtime": "pi", "model": "gpt-5.6-terra", "reasoning": "high"},
    )
    return FactoryConfig.model_validate(payload)


def test_a_run_moves_a_usage_limited_call_to_its_fallback(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable})
    runtime = recorder.router()
    config = _config_with_triage_fallback(factory_data_dir)

    run = WorkflowController(config, FileRunStore(factory_data_dir), runtime).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    triage_runtimes = [name for name, req in recorder.served if req.role is AgentRole.TRIAGE]
    assert triage_runtimes == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]


def test_fallback_runtime_counts_as_a_runtime_the_run_uses(factory_data_dir: Path) -> None:
    config = _config_with_triage_fallback(factory_data_dir)

    assert config.runtimes_for(RuntimeName.COPILOT) == {
        RuntimeName.COPILOT,
        RuntimeName.CLAUDE_CODE,
        RuntimeName.PI,
    }
    assert config.runtimes_for(None) == frozenset()


def test_claude_code_fallback_rejects_an_unknown_effort() -> None:
    with pytest.raises(ValueError, match="runtime claude-code accepts reasoning"):
        RuntimeFallback(runtime=RuntimeName.CLAUDE_CODE, model="claude-opus-5-5", reasoning="x")


def test_fallback_has_no_nested_fallback() -> None:
    with pytest.raises(ValueError, match="fallback"):
        RuntimeFallback.model_validate(
            {"runtime": "pi", "model": "m", "reasoning": "high", "fallback": {}}
        )


def test_worker_fallback_must_not_share_the_reviewer_model_family(factory_data_dir: Path) -> None:
    payload = build_config(factory_data_dir).model_dump(mode="json")
    reviewer_model = payload["models"]["reviewer"]["model"]
    payload["models"]["workers"]["L3"]["fallback"] = {
        "runtime": "pi",
        "model": reviewer_model,
        "reasoning": "high",
    }

    with pytest.raises(ValueError, match="reviewer model family"):
        FactoryConfig.model_validate(payload)
