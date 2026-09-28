from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import IO, Any

import pytest

from software_agent_factory.agent_artifact import parse_agent_artifact
from software_agent_factory.agents import AgentRequest, is_retryable_typed_artifact_failure
from software_agent_factory.config import PiConfig
from software_agent_factory.copilot_runtime import parse_copilot_artifact
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    RepositoryProfile,
    TriageResult,
    WorkItem,
)
from software_agent_factory.pi_runtime import PiAgentRuntime, ProcessFactory


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


def _runtime(
    *, process_factory: ProcessFactory | None = None, **overrides: object
) -> PiAgentRuntime:
    config = PiConfig(**overrides)
    kwargs: dict[str, object] = {}
    if process_factory is not None:
        kwargs["process_factory"] = process_factory
    return PiAgentRuntime(config, data_dir=Path("/data"), **kwargs)


# double-waiver: B1 — out-of-process pi subprocess handle
class FakeProcess:
    """``PiProcessHandle``-shaped double backed by real ``os.pipe()`` fds.

    Same approach as ``tests/test_pi_rpc.py``'s ``FakeProcess``: real pipe
    fds so ``PiRpcClient``'s raw-fd ``select``/``os.read`` loop runs against
    real, deterministic file descriptors -- the test writes scripted JSONL
    records into the read end ``PiRpcClient`` consumes, exactly as a real
    ``pi`` process would.
    """

    def __init__(self) -> None:
        stdin_read_fd, stdin_write_fd = os.pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stderr_read_fd, stderr_write_fd = os.pipe()

        self.stdin: IO[str] | None = os.fdopen(stdin_write_fd, "w")
        self._stdin_read = os.fdopen(stdin_read_fd, "r")
        self.stdout: IO[str] | None = os.fdopen(stdout_read_fd, "r")
        self._stdout_write = os.fdopen(stdout_write_fd, "w")
        self.stderr: IO[str] | None = os.fdopen(stderr_read_fd, "r")
        self._stderr_write: IO[str] | None = os.fdopen(stderr_write_fd, "w")

        self.pid = 999_999
        self._returncode: int | None = None
        self._wait_returncode: int | None = None

    def write_records(self, *records: dict[str, Any]) -> None:
        for record in records:
            self._stdout_write.write(json.dumps(record) + "\n")
        self._stdout_write.flush()

    def exit(self, returncode: int) -> None:
        self._returncode = returncode
        self._wait_returncode = returncode

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake-pi", timeout=timeout or 0)
        return self._wait_returncode

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
        return ("", "")


def _scripted_process(final_assistant_text: str) -> FakeProcess:
    """A ``FakeProcess`` that answers ``prompt`` then ``get_last_assistant_text``.

    Matches the one exchange :meth:`PiAgentRuntime.run` drives: a ``prompt``
    command (id ``c1``), settling via one ``agent_settled`` event, then a
    ``get_last_assistant_text`` command (id ``c2``) answering with
    ``final_assistant_text``.
    """
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {
            "type": "response",
            "id": "c2",
            "success": True,
            "data": {"text": final_assistant_text},
        },
    )
    process.exit(0)
    return process


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


# ---------------------------------------------------------------------------
# run: happy path against a scripted process (Step 3.3)
# ---------------------------------------------------------------------------


_TRIAGE_JSON = {
    "factory_eligible": True,
    "complexity": "L1",
    "risk": "R1",
    "requirements_quality": "clear",
    "needs_research": False,
    "confidence": 0.9,
}


def test_run_implementer_happy_path_returns_change_set(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    final_text = json.dumps({"summary": "Reject empty customer names."})
    process = _scripted_process(final_text)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is True
    assert result.role is AgentRole.IMPLEMENTER
    assert result.change_set == ChangeSet(summary="Reject empty customer names.")
    assert result.performance is not None
    assert result.performance.response_chars == len(final_text)


def test_run_read_only_role_happy_path_returns_triage_result() -> None:
    final_text = json.dumps(_TRIAGE_JSON)
    process = _scripted_process(final_text)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is True
    assert result.role is AgentRole.TRIAGE
    assert result.triage_result == TriageResult(**_TRIAGE_JSON)


def test_run_sends_prompt_built_from_the_request() -> None:
    process = _scripted_process(json.dumps(_TRIAGE_JSON))
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    runtime.run(request)

    sent = process._stdin_read.readline()
    command = json.loads(sent)
    assert command["type"] == "prompt"
    assert request.work_item.title in command["message"]


# ---------------------------------------------------------------------------
# run: output parity with Copilot (AC4)
# ---------------------------------------------------------------------------


def test_output_parity_with_copilot_for_identical_final_text() -> None:
    final_text = json.dumps(_TRIAGE_JSON)

    pi_artifact = parse_agent_artifact(
        AgentRole.TRIAGE, text=final_text, purpose=AgentPurpose.STANDARD
    )
    copilot_artifact = parse_copilot_artifact(
        AgentRole.TRIAGE, stdout=final_text, purpose=AgentPurpose.STANDARD
    )

    assert pi_artifact == copilot_artifact == TriageResult(**_TRIAGE_JSON)


# ---------------------------------------------------------------------------
# run: malformed output is retryable, same wording as Copilot
# ---------------------------------------------------------------------------


def test_run_malformed_output_is_retryable_like_copilot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = _scripted_process("not a JSON object at all")
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is False
    assert is_retryable_typed_artifact_failure(result, ChangeSet) is True
