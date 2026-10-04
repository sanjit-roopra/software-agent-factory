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


def _unavailable_result(request: AgentRequest) -> AgentResult:
    return AgentResult(
        role=request.role,
        success=False,
        failure_reason="claude reported an error (error): usage limit",
        runtime_unavailable=True,
    )


def _wrong_result(request: AgentRequest) -> AgentResult:
    return AgentResult(role=request.role, success=False, failure_reason="bad artifact")


class _Recorder:
    """Fake runtimes keyed by name. Each records every request it serves.

    ``hooks`` overrides how one runtime answers; the others answer like the
    fake runtime and report one model usage entry.
    """

    def __init__(self, hooks: dict[RuntimeName, Hook]) -> None:
        self.served: list[tuple[RuntimeName, AgentRequest]] = []
        self._hooks = hooks

    def router(self) -> RoutingAgentRuntime:
        factories: dict[RuntimeName, Callable[[], AgentRuntime]] = {
            name: (lambda name=name: self._runtime(name)) for name in RuntimeName
        }
        return RoutingAgentRuntime(RuntimeName.COPILOT, factories)

    def _runtime(self, name: RuntimeName) -> FakeAgentRuntime:
        hook = self._hooks.get(name)

        def serve(request: AgentRequest) -> AgentResult:
            self.served.append((name, request))
            if hook is not None:
                return hook(request)
            usage = UsageMetrics(model_usage=(ModelUsage(model=request.model),))
            return FakeAgentRuntime().run(request).model_copy(update={"usage": usage})

        return FakeAgentRuntime(
            triage=serve, planner=serve, implementer=serve, tester=serve, reviewer=serve
        )


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


def _served_after_fallback(
    request: AgentRequest,
) -> tuple[AgentResult, list[RuntimeName], AgentRequest]:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable_result})
    result = recorder.router().run(request)
    return result, [name for name, _ in recorder.served], recorder.served[-1][1]


def test_unavailable_runtime_hands_the_call_to_its_fallback_route() -> None:
    result, runtimes, served = _served_after_fallback(_request())

    assert result.success is True
    assert runtimes == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]
    assert (served.model, served.reasoning, served.context_tier) == (
        PI_FALLBACK.model,
        PI_FALLBACK.reasoning,
        PI_FALLBACK.context_tier,
    )
    assert served.fallback is None


def test_fallback_keeps_every_other_request_field_and_the_attempt_number() -> None:
    request = _request()

    _, _, served = _served_after_fallback(request)

    routing_fields = {"runtime", "model", "reasoning", "context_tier", "fallback"}
    assert served.model_dump(exclude=routing_fields) == request.model_dump(exclude=routing_fields)
    assert served.attempt_number == 2


def test_fallback_result_carries_the_first_failure_and_the_serving_usage() -> None:
    result, _, _ = _served_after_fallback(_request())

    assert result.fallback_reason == "claude reported an error (error): usage limit"
    assert result.usage is not None
    assert [(u.runtime, u.model) for u in result.usage.model_usage] == [
        (RuntimeName.PI, PI_FALLBACK.model)
    ]


def test_fallback_is_logged_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable_result})
    router = recorder.router()

    router.run(_request())

    for fact in ("TRIAGE", "claude-code", "pi", PI_FALLBACK.model, "usage limit"):
        assert fact in caplog.text


@pytest.mark.parametrize(
    ("first", "fallback"),
    [(_wrong_result, PI_FALLBACK), (_unavailable_result, None)],
    ids=["model-failure", "no-fallback"],
)
def test_result_is_returned_as_is_without_unavailability_or_fallback(
    first: Hook, fallback: RuntimeFallback | None
) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: first})
    router = recorder.router()

    request = _request(fallback=fallback)

    result = router.run(request)

    assert result == first(request)
    assert [name for name, _ in recorder.served] == [RuntimeName.CLAUDE_CODE]


def test_fallback_is_tried_once_and_its_result_is_returned() -> None:
    def pi_unavailable(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=request.role,
            success=False,
            failure_reason="pi could not be started (FileNotFoundError): pi",
            runtime_unavailable=True,
        )

    recorder = _Recorder(
        {RuntimeName.CLAUDE_CODE: _unavailable_result, RuntimeName.PI: pi_unavailable}
    )

    result = recorder.router().run(_request())

    assert result.failure_reason == "pi could not be started (FileNotFoundError): pi"
    assert [name for name, _ in recorder.served] == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]


def _config_with_triage_fallback(data_dir: Path) -> FactoryConfig:
    payload = build_config(data_dir).model_dump(mode="json")
    payload["models"]["triage"].update(
        runtime=RuntimeName.CLAUDE_CODE.value,
        reasoning="high",
        fallback=PI_FALLBACK.model_dump(mode="json"),
    )
    return FactoryConfig.model_validate(payload)


def test_a_run_moves_a_usage_limited_call_to_its_fallback(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    recorder = _Recorder({RuntimeName.CLAUDE_CODE: _unavailable_result})
    runtime = recorder.router()
    config = _config_with_triage_fallback(factory_data_dir)

    run = WorkflowController(config, FileRunStore(factory_data_dir), runtime).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    triage_runtimes = [name for name, req in recorder.served if req.role is AgentRole.TRIAGE]
    assert triage_runtimes == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]
    # The record keeps the configured route; its usage names what served the call.
    triage = next(r for r in run.invocation_records if r.role is AgentRole.TRIAGE)
    assert triage.model == config.models.triage.model
    assert triage.fallback_reason == "claude reported an error (error): usage limit"
    assert triage.usage is not None
    assert [(u.runtime, u.model) for u in triage.usage.model_usage] == [
        (RuntimeName.PI, PI_FALLBACK.model)
    ]


def test_an_implementer_on_an_unavailable_runtime_uses_its_tier_fallback(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    def implementer_unavailable(request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER:
            return _unavailable_result(request)
        return FakeAgentRuntime().run(request)

    recorder = _Recorder({RuntimeName.CLAUDE_CODE: implementer_unavailable})
    payload = build_config(factory_data_dir).model_dump(mode="json")
    for worker in payload["models"]["workers"].values():
        worker.update(
            runtime=RuntimeName.CLAUDE_CODE.value,
            reasoning="high",
            # Workers must stay outside the reviewer's family, so not PI_FALLBACK.
            fallback={"runtime": "pi", "model": "mai-code-1.1-flash", "reasoning": "high"},
        )
    config = FactoryConfig.model_validate(payload)

    run = WorkflowController(config, FileRunStore(factory_data_dir), recorder.router()).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    served = [name for name, req in recorder.served if req.role is AgentRole.IMPLEMENTER]
    assert served == [RuntimeName.CLAUDE_CODE, RuntimeName.PI]
    assert [record.attempt_number for record in run.attempt_records] == [1]


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
    nested = {**PI_FALLBACK.model_dump(mode="json"), "fallback": PI_FALLBACK.model_dump()}

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        RuntimeFallback.model_validate(nested)


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


def test_reviewer_fallback_must_not_share_a_worker_model_family(factory_data_dir: Path) -> None:
    payload = build_config(factory_data_dir).model_dump(mode="json")
    payload["models"]["reviewer"]["fallback"] = {
        "runtime": "pi",
        "model": payload["models"]["workers"]["L1"]["model"],
        "reasoning": "high",
    }

    with pytest.raises(ValueError, match="reviewer model family"):
        FactoryConfig.model_validate(payload)


def test_a_reviewer_fallback_from_its_own_family_is_accepted(factory_data_dir: Path) -> None:
    payload = build_config(factory_data_dir).model_dump(mode="json")
    payload["models"]["reviewer"]["fallback"] = {
        "runtime": "pi",
        "model": payload["models"]["reviewer"]["model"],
        "reasoning": "high",
    }

    config = FactoryConfig.model_validate(payload)

    assert config.models.reviewer.fallback is not None


def test_a_successful_result_cannot_be_unavailable() -> None:
    with pytest.raises(ValueError, match="runtime_unavailable requires success False"):
        AgentResult(role=AgentRole.TRIAGE, success=True, runtime_unavailable=True)
