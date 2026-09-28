from __future__ import annotations

import pytest

from software_agent_factory.agent_capabilities import AgentCapability, capability_for
from software_agent_factory.agents import AgentRequest
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    RepositoryProfile,
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


def test_correct_change_set_purpose_is_no_tools() -> None:
    """CORRECT_CHANGE_SET always targets IMPLEMENTER, so this also covers the
    purpose check winning over the role (which alone would mean
    IMPLEMENTER_WRITE); AgentRequest validation forbids pairing this purpose
    with any other role, so that case cannot be exercised separately."""
    change_set = ChangeSet(
        summary="Initial summary",
        changed_files=["app.py"],
        tests_added=[],
        commands_run=[],
    )
    request = _request(
        AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.CORRECT_CHANGE_SET,
        change_set=change_set,
        workspace_path="/repo",
    )

    assert capability_for(request) is AgentCapability.NO_TOOLS


def test_generate_repository_skill_purpose_is_web_research() -> None:
    request = _request(
        AgentRole.RESEARCHER,
        purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
        repository_profile=RepositoryProfile(
            manifest_fingerprint="a" * 64,
            dependency_fingerprint="b" * 64,
        ),
        official_documentation_origins=["https://react.dev"],
        workspace_path="/runs/RUN-1",
    )

    assert capability_for(request) is AgentCapability.WEB_RESEARCH


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
