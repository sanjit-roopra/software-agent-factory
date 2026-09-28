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
from software_agent_factory.pi_rpc import PiRpcClient
from software_agent_factory.pi_runtime import (
    PiAgentRuntime,
    ProcessFactory,
    usage_from_pi_messages,
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

    def write_raw_stdout(self, text: str) -> None:
        self._stdout_write.write(text)
        self._stdout_write.flush()

    def write_stderr(self, text: str) -> None:
        assert self._stderr_write is not None
        self._stderr_write.write(text)
        self._stderr_write.flush()

    def close_stdout(self) -> None:
        self._stdout_write.close()

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

    def sent_commands(self) -> list[dict[str, Any]]:
        """Read back what ``PiRpcClient`` wrote to stdin so far (non-blocking)."""
        os.set_blocking(self._stdin_read.fileno(), False)
        commands: list[dict[str, Any]] = []
        try:
            for line in self._stdin_read:
                stripped = line.strip()
                if stripped:
                    commands.append(json.loads(stripped))
        except BlockingIOError:
            pass
        return commands


class StdinEofExitProcess(FakeProcess):
    """A pi-like fake that exits when its stdin is closed, not in response to abort.

    Real ``pi`` shuts down on stdin EOF, independent of whether it ever
    receives or acts on an RPC ``abort`` command. Wraps ``self.stdin``'s
    ``close`` so closing it (as :meth:`PiAgentRuntime._abort_and_kill` now
    does) flips this fake to "exited", the same way a real pi process would
    without needing to model actual EOF detection over the pipe.
    """

    def __init__(self) -> None:
        super().__init__()
        assert self.stdin is not None
        real_close = self.stdin.close

        def _close_and_exit() -> None:
            real_close()
            self.exit(0)

        self.stdin.close = _close_and_exit  # type: ignore[method-assign]


def _get_messages_response(
    stop_reason: str | None = None,
    error_message: str | None = None,
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """The ``get_messages`` response :meth:`PiAgentRuntime.run` reads for ``c2``.

    ``stop_reason=None`` mirrors a message that settled normally (no
    ``stopReason`` field carried at all). ``usage``/``model`` attach a
    per-message ``usage`` mapping and a ``model`` id, the shape
    :func:`~software_agent_factory.pi_runtime.usage_from_pi_messages` reads;
    passing either includes the message in the response even when
    ``stop_reason`` is ``None`` (a settled call still has a message pi
    reports usage against).
    """
    message: dict[str, Any] = {"role": "assistant"}
    if stop_reason is not None:
        message["stopReason"] = stop_reason
    if error_message is not None:
        message["errorMessage"] = error_message
    if usage is not None:
        message["usage"] = usage
    if model is not None:
        message["model"] = model
    include_message = stop_reason is not None or usage is not None
    return {
        "type": "response",
        "id": "c2",
        "success": True,
        "data": {"messages": [message] if include_message else []},
    }


def _scripted_process(
    final_assistant_text: str,
    *,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> FakeProcess:
    """A ``FakeProcess`` that answers ``prompt``, ``get_messages``, then
    ``get_last_assistant_text``.

    Matches the exchange :meth:`PiAgentRuntime.run` drives: a ``prompt``
    command (id ``c1``), settling via one ``agent_settled`` event, a
    ``get_messages`` command (id ``c2``) whose final message carries
    ``stop_reason``/``usage``/``model`` (all ``None`` by default -- a
    normally-settled call with no usage to report), then a
    ``get_last_assistant_text`` command (id ``c3``) answering with
    ``final_assistant_text``.
    """
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason=stop_reason, usage=usage, model=model),
        {
            "type": "response",
            "id": "c3",
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


def test_child_env_scrubs_copilot_github_token_for_non_copilot_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")
    runtime = _runtime(provider="anthropic")

    env = runtime._child_env()

    assert "COPILOT_GITHUB_TOKEN" not in env


def test_child_env_and_scrubbed_adds_copilot_github_token_value_when_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat-secret")
    runtime = _runtime()  # default provider: github-copilot

    env, scrubbed_values = runtime._child_env_and_scrubbed()

    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-pat-secret"
    assert "copilot-pat-secret" in scrubbed_values


def test_child_env_and_scrubbed_adds_copilot_github_token_value_when_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat-secret")
    runtime = _runtime(provider="anthropic")

    env, scrubbed_values = runtime._child_env_and_scrubbed()

    assert "COPILOT_GITHUB_TOKEN" not in env
    assert "copilot-pat-secret" in scrubbed_values


def test_child_env_and_scrubbed_retains_and_scrubs_provider_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-fake-key")
    runtime = _runtime()

    env, scrubbed_values = runtime._child_env_and_scrubbed()

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-fake-key"
    assert env["OPENAI_API_KEY"] == "sk-openai-fake-key"
    assert "sk-ant-fake-key" in scrubbed_values
    assert "sk-openai-fake-key" in scrubbed_values


# ---------------------------------------------------------------------------
# run: request validation shared with Copilot (validate_runtime_request)
# ---------------------------------------------------------------------------


def test_run_implementer_without_workspace_path_raises() -> None:
    runtime = _runtime()
    request = _request(AgentRole.IMPLEMENTER)

    with pytest.raises(ValueError, match="IMPLEMENTER requests require workspace_path"):
        runtime.run(request)


def test_run_timeout_seconds_below_one_raises() -> None:
    runtime = _runtime()
    request = _request(AgentRole.TRIAGE, timeout_seconds=0)

    with pytest.raises(ValueError, match="timeout_seconds must be at least 1"):
        runtime.run(request)


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


# ---------------------------------------------------------------------------
# run: pi failures yield sanitized failed results (Step 3.4)
# ---------------------------------------------------------------------------


def test_run_assistant_error_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="error", error_message="boom"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant error: boom"


def test_run_assistant_error_without_error_message_uses_default_wording() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="error"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant error: (no error message)"


def test_run_assistant_aborted_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="aborted"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant call was aborted"


def test_run_process_exits_before_settling_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.close_stdout()
    process.exit(7)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "7" in result.failure_reason


def test_run_invalid_protocol_line_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_raw_stdout("not json at all\n")
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "invalid protocol record" in result.failure_reason


def test_run_command_error_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {"type": "response", "id": "c2", "success": False, "error": "not supported"},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "get_messages" in result.failure_reason
    assert "not supported" in result.failure_reason


def test_run_get_messages_non_mapping_data_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {"type": "response", "id": "c2", "success": True, "data": ["oops"]},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi returned an unexpected response for get_messages"


def test_run_get_last_assistant_text_non_mapping_data_yields_failed_result() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(),
        {"type": "response", "id": "c3", "success": True, "data": "not-a-mapping"},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi returned an unexpected response for get_last_assistant_text"


def test_run_get_last_assistant_text_null_text_is_treated_as_empty() -> None:
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(),
        {"type": "response", "id": "c3", "success": True, "data": {"text": None}},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    # A ``null`` text must be treated as empty text (``response_chars == 0``),
    # not stringified into the four-character literal "None".
    assert result.success is False
    assert result.performance is not None
    assert result.performance.response_chars == 0


def test_run_missing_executable_yields_failed_result() -> None:
    def factory(command: list[str], cwd: Path, env: dict[str, str]) -> FakeProcess:
        raise FileNotFoundError(command[0])

    runtime = _runtime(process_factory=factory, executable="pi-missing")
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "pi-missing" in result.failure_reason


def test_run_sanitizes_credential_value_from_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_supersecrettoken1234")
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr("auth failed for ghp_supersecrettoken1234")
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "ghp_supersecrettoken1234" not in result.failure_reason


def test_run_sanitizes_provider_api_key_from_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake1234567890")
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr("auth failed for sk-ant-fake1234567890")
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason is not None
    assert "sk-ant-fake1234567890" not in result.failure_reason


# ---------------------------------------------------------------------------
# run: timeout aborts, then kills if still alive (Step 3.4)
# ---------------------------------------------------------------------------


def test_run_timeout_sends_abort_then_kills_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    # No agent_settled event is ever written, and the process never exits --
    # simulates pi not settling within the request timeout.
    killed: list[Any] = []
    monkeypatch.setattr(
        "software_agent_factory.pi_runtime.kill_process_group",
        lambda proc: killed.append(proc),
    )
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    sent_types = [command.get("type") for command in process.sent_commands()]
    assert "abort" in sent_types
    assert killed == [process]
    # No get_messages call ever completed (wait_for_settled itself never
    # settled), so the best-effort get_messages retry (Step 4.2) is attempted
    # but also gets nothing back -- usage stays unknown, not zeroed out.
    assert "get_messages" in sent_types
    assert result.usage is None


def test_abort_and_kill_does_not_kill_a_process_that_exits_on_stdin_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi shuts down on stdin EOF, not necessarily in response to ``abort``.

    :meth:`PiAgentRuntime._abort_and_kill` must close stdin after sending the
    best-effort ``abort`` -- a fake that only exits once its stdin is closed
    (:class:`StdinEofExitProcess`) must not be escalated to
    ``kill_process_group``.
    """
    process = StdinEofExitProcess()
    client = PiRpcClient(process)
    killed: list[Any] = []
    monkeypatch.setattr(
        "software_agent_factory.pi_runtime.kill_process_group",
        lambda proc: killed.append(proc),
    )
    runtime = _runtime()

    runtime._abort_and_kill(client, process)

    assert killed == []
    assert process.poll() == 0


def test_run_process_exits_before_settling_reports_unknown_usage() -> None:
    process = FakeProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.close_stdout()
    process.exit(7)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.usage is None


# ---------------------------------------------------------------------------
# run: usage recorded per model (Step 4.2)
# ---------------------------------------------------------------------------


def test_run_success_carries_usage_from_get_messages(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    final_text = json.dumps({"summary": "Reject empty customer names."})
    process = _scripted_process(
        final_text,
        usage={
            "input": 100,
            "output": 20,
            "cacheRead": 80,
            "cacheWrite": 5,
            "cost": {"total": 0.05},
        },
        model="claude-sonnet-5",
    )
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is True
    assert result.usage is not None
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 80
    assert result.usage.cache_write_tokens == 5
    assert result.usage.list_price_estimate_usd == pytest.approx(0.05)
    assert result.usage.total_user_requests == 1
    assert result.usage.current_model == "claude-sonnet-5"


def test_run_timeout_after_settling_keeps_partial_usage() -> None:
    """AC6: a timeout after the call settled still records usage read so far.

    The settled call's own ``get_messages`` read already captured usage
    before ``get_last_assistant_text`` stalls past the timeout -- no
    best-effort retry is needed (or attempted) here.
    """
    process = FakeProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": 40, "output": 10}, model="claude-sonnet-5"),
    )
    # No response ever arrives for get_last_assistant_text (c3).
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 10


# ---------------------------------------------------------------------------
# usage_from_pi_messages: pure per-model usage mapping (Step 4.2)
# ---------------------------------------------------------------------------


def _assistant_message(usage: dict[str, Any], *, model: str = "claude-sonnet-5") -> dict[str, Any]:
    return {"role": "assistant", "model": model, "usage": usage}


def test_usage_from_pi_messages_sums_per_model() -> None:
    messages = [
        _assistant_message(
            {"input": 100, "output": 20, "cacheRead": 80, "cacheWrite": 5, "cost": {"total": 0.03}}
        ),
        _assistant_message(
            {"input": 50, "output": 10, "cacheRead": 40, "cacheWrite": 2, "cost": {"total": 0.02}}
        ),
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 2
    assert usage.input_tokens == 150
    assert usage.output_tokens == 30
    assert usage.cache_read_tokens == 120
    assert usage.cache_write_tokens == 7
    assert usage.list_price_estimate_usd == pytest.approx(0.05)
    assert usage.current_model == "claude-sonnet-5"
    assert len(usage.model_usage) == 1
    model_usage = usage.model_usage[0]
    assert model_usage.model == "claude-sonnet-5"
    assert model_usage.requests == 2
    assert model_usage.input_tokens == 150
    assert model_usage.output_tokens == 30
    assert model_usage.cache_read_tokens == 120
    assert model_usage.cache_write_tokens == 7
    assert model_usage.list_price_estimate_usd == pytest.approx(0.05)


def test_usage_from_pi_messages_unreported_fields_stay_unknown() -> None:
    messages = [_assistant_message({"input": 100, "output": 20})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_write_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_usage_from_pi_messages_reported_zero_stays_zero_not_unknown() -> None:
    messages = [_assistant_message({"input": 10, "cacheRead": 0})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_read_tokens == 0


def test_usage_from_pi_messages_sums_cache_write_1h_with_cache_write() -> None:
    messages = [_assistant_message({"input": 10, "cacheWrite": 3, "cacheWrite1h": 4})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_write_tokens == 7


def test_usage_from_pi_messages_copilot_only_units_stay_unknown() -> None:
    messages = [_assistant_message({"input": 10, "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_premium_request_cost is None
    assert usage.total_nano_aiu is None
    assert usage.model_usage[0].premium_request_cost is None
    assert usage.model_usage[0].total_nano_aiu is None


def test_usage_from_pi_messages_no_assistant_messages_reports_zero_requests() -> None:
    usage = usage_from_pi_messages([])

    assert usage is not None
    assert usage.total_user_requests == 0
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.list_price_estimate_usd is None
    assert usage.current_model is None
    assert usage.model_usage == ()


def test_usage_from_pi_messages_ignores_non_assistant_messages() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "usage": "not-a-mapping"},
        _assistant_message({"input": 5}),
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 1
    assert usage.input_tokens == 5


def test_usage_from_pi_messages_message_without_model_skips_model_usage_entry() -> None:
    """A modelless assistant message counts toward the aggregate but gets no
    ``model_usage`` entry -- this pure function has no request-supplied model
    to fall back on (see its docstring)."""
    messages = [{"role": "assistant", "usage": {"input": 5}}]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 1
    assert usage.input_tokens == 5
    assert usage.current_model is None
    assert usage.model_usage == ()


def test_usage_from_pi_messages_non_numeric_field_value_stays_unknown() -> None:
    """A ``usage`` field carrying a non-numeric value (a malformed record) is
    treated as not reported, not coerced or crashed on."""
    messages = [_assistant_message({"input": "not-a-number", "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5
