from __future__ import annotations

import pytest

from software_agent_factory.agent_capabilities import AgentCapability, capability_for
from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentRole,
    WorkItem,
)


def _work_item() -> WorkItem:
    return WorkItem(
        id="WI-1",
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only customer names.",
    )


def _request(role: AgentRole, **overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "role": role,
        "model": "claude-sonnet-5",
        "reasoning": "high",
        "work_item": _work_item(),
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


def test_implementer_role_is_implementer_write() -> None:
    request = _request(AgentRole.IMPLEMENTER)

    assert capability_for(request) is AgentCapability.IMPLEMENTER_WRITE


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
        AgentRole.RESEARCHER,
        AgentRole.PLANNER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ],
)
def test_other_roles_default_to_read_only(role: AgentRole) -> None:
    request = _request(role)

    assert capability_for(request) is AgentCapability.READ_ONLY
