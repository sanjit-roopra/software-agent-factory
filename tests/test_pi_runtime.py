from __future__ import annotations

from pathlib import Path

import pytest

from software_agent_factory.agents import AgentRequest
from software_agent_factory.config import PiConfig
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    RepositoryProfile,
    WorkItem,
)
from software_agent_factory.pi_runtime import PiAgentRuntime


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
        "reasoning": "medium",
        "work_item": _work_item(),
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


def _correction_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.CORRECT_CHANGE_SET,
        "change_set": ChangeSet(summary="Fix output shape"),
        "workspace_path": "/workspaces/wi-1",
    }
    defaults.update(overrides)
    return _request(AgentRole.IMPLEMENTER, **defaults)


def _skill_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.GENERATE_REPOSITORY_SKILL,
        "repository_profile": RepositoryProfile(
            manifest_fingerprint="a" * 64,
            dependency_fingerprint="b" * 64,
        ),
        "official_documentation_origins": ["https://react.dev"],
        "practice_reference_urls": ["https://example.com/review.md"],
        "workspace_path": "/runs/RUN-1",
    }
    defaults.update(overrides)
    return _request(AgentRole.RESEARCHER, **defaults)


def _runtime(**overrides: object) -> PiAgentRuntime:
    config = PiConfig(**overrides)
    return PiAgentRuntime(config, data_dir=Path("/data"))


# ---------------------------------------------------------------------------
# _build_command: tool allowlists per role/purpose
# ---------------------------------------------------------------------------


def test_build_command_implementer_gets_write_tools() -> None:
    runtime = _runtime()
    request = _request(AgentRole.IMPLEMENTER, workspace_path="/workspaces/wi-1")

    command = runtime._build_command(request, session_arg=["--no-session"])

    assert command == [
        "pi",
        "--mode",
        "rpc",
        "--provider",
        "github-copilot",
        "--model",
        "claude-sonnet-5",
        "--thinking",
        "medium",
        "--tools",
        "read,bash,edit,write,grep,find,ls",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--no-session",
    ]


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
def test_build_command_read_only_roles_get_read_only_tools(role: AgentRole) -> None:
    runtime = _runtime()
    request = _request(role)

    command = runtime._build_command(request, session_arg=["--no-session"])

    assert "--tools" in command
    tools_index = command.index("--tools")
    assert command[tools_index + 1] == "read,grep,find,ls"
    assert "--no-tools" not in command


def test_build_command_change_set_correction_gets_no_tools() -> None:
    runtime = _runtime()
    request = _correction_request()

    command = runtime._build_command(request, session_arg=["--no-session"])

    assert "--no-tools" in command
    assert "--tools" not in command


def test_build_command_rejects_skill_generation() -> None:
    runtime = _runtime()
    request = _skill_request()

    with pytest.raises(ValueError, match="not supported on pi"):
        runtime._build_command(request, session_arg=["--no-session"])


def test_build_command_uses_configured_executable_and_provider() -> None:
    runtime = _runtime(executable="pi-beta", provider="anthropic")
    request = _request(AgentRole.TRIAGE)

    command = runtime._build_command(request, session_arg=["--no-session"])

    assert command[0] == "pi-beta"
    assert command[command.index("--provider") + 1] == "anthropic"


def test_build_command_appends_session_arg_for_resumed_session() -> None:
    runtime = _runtime()
    request = _request(AgentRole.IMPLEMENTER, workspace_path="/workspaces/wi-1")

    command = runtime._build_command(
        request, session_arg=["--session", "/data/pi-sessions/WI-1/IMPLEMENTER.jsonl"]
    )

    assert command[-2:] == ["--session", "/data/pi-sessions/WI-1/IMPLEMENTER.jsonl"]


# ---------------------------------------------------------------------------
# _cwd_for: same rule as the Copilot runtime
# ---------------------------------------------------------------------------


def test_cwd_for_uses_workspace_path_when_supplied(tmp_path: Path) -> None:
    runtime = _runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    assert runtime._cwd_for(request) == workspace.resolve()


def test_cwd_for_falls_back_to_process_cwd_for_read_only_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = _runtime()

    assert runtime._cwd_for(_request(AgentRole.TRIAGE)) == tmp_path.resolve()


def test_cwd_for_change_set_correction_requires_workspace_path() -> None:
    runtime = _runtime()
    request = _correction_request(workspace_path=None)

    with pytest.raises(ValueError, match="workspace_path"):
        runtime._cwd_for(request)


def test_cwd_for_skill_generation_requires_workspace_path() -> None:
    runtime = _runtime()
    request = _skill_request(workspace_path=None)

    with pytest.raises(ValueError, match="neutral run directory"):
        runtime._cwd_for(request)


# ---------------------------------------------------------------------------
# _child_env: GitHub credential scrub, COPILOT_GITHUB_TOKEN kept, cache retention
# ---------------------------------------------------------------------------


def test_child_env_scrubs_github_credential_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_other_secret")
    runtime = _runtime()

    env = runtime._child_env()

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_child_env_keeps_copilot_github_token_for_headless_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    runtime = _runtime()

    env = runtime._child_env()

    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-pat"
    assert "GITHUB_TOKEN" not in env


def test_child_env_sets_pi_cache_retention_from_config() -> None:
    runtime = _runtime(cache_retention="short")

    env = runtime._child_env()

    assert env["PI_CACHE_RETENTION"] == "short"


def test_child_env_defaults_pi_cache_retention_to_long() -> None:
    runtime = _runtime()

    env = runtime._child_env()

    assert env["PI_CACHE_RETENTION"] == "long"
