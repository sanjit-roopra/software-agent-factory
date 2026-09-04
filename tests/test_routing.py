from __future__ import annotations

import pytest

from software_agent_factory.config import FactoryConfig
from software_agent_factory.models import AgentRole, Complexity, Risk
from software_agent_factory.routing import ModelRouter


def _config_dict(max_total_attempts: int = 6) -> dict[str, object]:
    return {
        "factory": {
            "data_dir": "~/.software-factory",
            "retries": {
                "same_model_attempts": 2,
                "max_total_attempts": max_total_attempts,
            },
        },
        "models": {
            "triage": {"model": "claude-sonnet-5", "reasoning": "medium"},
            "refiner": {"model": "claude-opus-5", "reasoning": "high"},
            "researcher": {"model": "gpt-5.6-sol", "reasoning": "high"},
            "planner": {"model": "claude-opus-5", "reasoning": "high"},
            "workers": {
                "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                "L1": {"model": "claude-sonnet-5", "reasoning": "medium"},
                "L2": {"model": "claude-opus-5", "reasoning": "high"},
                "L3": {"model": "claude-opus-5", "reasoning": "high"},
            },
            "tester": {"model": "claude-sonnet-5", "reasoning": "high"},
            "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
        },
        "repository": {
            "branch_prefix": "factory/",
            "command_timeout_seconds": 900,
            "commands": {"install": [], "verify": [], "build": []},
        },
        "risk": {
            "R0": {"human_approval": False},
            "R1": {"human_approval": False},
            "R2": {"human_approval": True},
            "R3": {"human_approval": True},
        },
    }


def test_model_router_routes_fixed_roles_and_risk_gate() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    assert router.model_for_role(AgentRole.TRIAGE).model == "claude-sonnet-5"
    assert router.model_for_role(AgentRole.PLANNER).reasoning == "high"
    assert router.model_for_role(AgentRole.RESEARCHER).model == "gpt-5.6-sol"
    assert router.model_for_role(AgentRole.REVIEWER).model == "gpt-5.6-sol"
    assert router.model_for_researcher().model == "gpt-5.6-sol"
    assert router.requires_human_approval(Risk.R1) is False
    assert router.requires_human_approval(Risk.R2) is True


def test_model_router_rejects_implementer_without_complexity() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    with pytest.raises(ValueError, match="Implementer routing"):
        router.model_for_role(AgentRole.IMPLEMENTER)


def test_model_router_escalates_implementer_by_distinct_models() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    routed_models = [
        selection.model if selection is not None else None
        for selection in (
            router.model_for_implementer(Complexity.L0, attempt_number)
            for attempt_number in range(1, 8)
        )
    ]

    assert routed_models == [
        "mai-code-1.1-flash",
        "mai-code-1.1-flash",
        "claude-sonnet-5",
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-5",
        None,
    ]


@pytest.mark.parametrize("starting_complexity", list(Complexity))
def test_every_complexity_gets_the_full_attempt_budget(
    starting_complexity: Complexity,
) -> None:
    """Escalation must never shrink the bounded budget.

    Higher complexities have fewer *distinct* stronger models available, so
    routing plateaus on the strongest one instead of returning ``None``
    early: otherwise an L3 task would silently get fewer repair attempts
    than an L0 task.
    """
    config = FactoryConfig.model_validate(_config_dict())
    router = ModelRouter(config)
    max_total_attempts = config.retries.max_total_attempts

    selections = [
        router.model_for_implementer(starting_complexity, attempt_number)
        for attempt_number in range(1, max_total_attempts + 1)
    ]

    assert all(selection is not None for selection in selections)
    assert len(selections) == max_total_attempts
    assert router.model_for_implementer(starting_complexity, max_total_attempts + 1) is None


@pytest.mark.parametrize(
    ("starting_complexity", "strongest_model"),
    [
        (Complexity.L0, "claude-opus-5"),
        (Complexity.L1, "claude-opus-5"),
        (Complexity.L2, "claude-opus-5"),
        (Complexity.L3, "claude-opus-5"),
    ],
)
def test_escalation_plateaus_at_strongest_distinct_model(
    starting_complexity: Complexity,
    strongest_model: str,
) -> None:
    config = FactoryConfig.model_validate(_config_dict())
    router = ModelRouter(config)

    last = router.model_for_implementer(
        starting_complexity, config.retries.max_total_attempts
    )

    assert last is not None
    assert last.model == strongest_model


@pytest.mark.parametrize("attempt_number", [0, -1])
def test_model_router_rejects_invalid_attempt_numbers(attempt_number: int) -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    with pytest.raises(ValueError, match="attempt_number"):
        router.model_for_implementer(Complexity.L0, attempt_number)


def test_model_router_returns_none_only_past_total_attempt_budget() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict(max_total_attempts=3)))

    assert router.model_for_implementer(Complexity.L0, 3).model == "claude-sonnet-5"
    assert router.model_for_implementer(Complexity.L0, 4) is None
    assert router.model_for_implementer(Complexity.L2, 3).model == "claude-opus-5"
    assert router.model_for_implementer(Complexity.L2, 4) is None
