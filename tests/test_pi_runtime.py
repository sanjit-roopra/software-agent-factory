from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from factory_testing import FakePiClock, FakePiProcess

from software_agent_factory.agent_artifact import parse_agent_artifact
from software_agent_factory.agents import (
    RUNTIME_FAILURE_REASON_LIMIT,
    AgentRequest,
    is_retryable_typed_artifact_failure,
)
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
from software_agent_factory.pi_runtime import (
    _BEST_EFFORT_USAGE_DEADLINE_SECONDS,
    PiAgentRuntime,
    ProcessFactory,
    _default_process_factory,
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


class StdinEofExitProcess(FakePiProcess):
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
) -> FakePiProcess:
    """A ``FakePiProcess`` that answers ``prompt``, ``get_messages``, then
    ``get_last_assistant_text``.

    Matches the exchange :meth:`PiAgentRuntime.run` drives: a ``prompt``
    command (id ``c1``), settling via one ``agent_settled`` event, a
    ``get_messages`` command (id ``c2``) whose final message carries
    ``stop_reason``/``usage``/``model`` (all ``None`` by default -- a
    normally-settled call with no usage to report), then a
    ``get_last_assistant_text`` command (id ``c3``) answering with
    ``final_assistant_text``.
    """
    process = FakePiProcess()
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
# run: child environment (credential scrub, provider key allowlist, cache retention)
# ---------------------------------------------------------------------------


#: Provider -> the API-key environment variable pi authenticates it from.
_PROVIDER_KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

#: Deliberately not ``ghp_``-shaped, so only exact-value redaction can hide it.
_PLAIN_SECRET = "plainsecretvalue1234"


class _Launch(NamedTuple):
    command: list[str]
    cwd: Path
    env: dict[str, str]


@pytest.fixture(autouse=True)
def killpg_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record ``os.killpg`` calls instead of signalling the fake pid's process group.

    ``FakePiProcess.pid`` is not a real process group: without this, a
    timeout test would send SIGTERM to whatever group has that id. The fake
    never "dies", so ``PiRpcClient.close`` repeats the kill after the runtime's
    own: tests compare ``set(killpg_calls)``, not the list.
    """
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )
    return calls


@pytest.fixture(autouse=True)
def _isolated_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no credential variable from the developer's shell."""
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "JEV_API_KEY",
        *_PROVIDER_KEY_ENV_VARS.values(),
    ):
        monkeypatch.delenv(name, raising=False)


def _launch(runtime_kwargs: dict[str, object] | None = None, **request_overrides: Any) -> _Launch:
    """Run one triage call and return what the runtime launched pi with."""
    launches: list[_Launch] = []
    process = _scripted_process(json.dumps(_TRIAGE_JSON))

    def factory(command: Sequence[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        launches.append(_Launch(list(command), cwd, env))
        return process

    runtime = _runtime(process_factory=factory, **(runtime_kwargs or {}))
    result = runtime.run(_request(AgentRole.TRIAGE, **request_overrides))
    assert result.success is True
    return launches[0]


def _failure_reason_when_pi_writes(stderr: str, **runtime_kwargs: object) -> str:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr(stderr)
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process, **runtime_kwargs)
    result = runtime.run(_request(AgentRole.TRIAGE))
    assert result.success is False
    assert result.failure_reason is not None
    return result.failure_reason


def test_run_redacts_a_secret_the_stderr_window_would_cut_in_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's 4 KB stderr window cuts inside the secret: without redaction before
    truncation, its last five characters would open the failure reason."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _PLAIN_SECRET)

    reason = _failure_reason_when_pi_writes(
        "A" * 100 + _PLAIN_SECRET + "B" * (4096 - 5), provider="anthropic"
    )

    assert _PLAIN_SECRET[-5:] not in reason


def test_run_child_env_scrubs_github_credential_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_other_secret")

    env = _launch().env

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_run_child_env_keeps_copilot_github_token_for_headless_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    env = _launch().env

    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-pat"
    assert "GITHUB_TOKEN" not in env


def test_run_child_env_sets_pi_cache_retention_from_config() -> None:
    assert _launch({"cache_retention": "short"}).env["PI_CACHE_RETENTION"] == "short"


def test_run_child_env_defaults_pi_cache_retention_to_long() -> None:
    assert _launch().env["PI_CACHE_RETENTION"] == "long"


def test_run_child_env_drops_copilot_github_token_for_non_copilot_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")

    env = _launch({"provider": "anthropic"}).env

    assert "COPILOT_GITHUB_TOKEN" not in env


@pytest.mark.parametrize(("provider", "own_var"), sorted(_PROVIDER_KEY_ENV_VARS.items()))
def test_run_child_env_keeps_only_the_configured_providers_api_key(
    monkeypatch: pytest.MonkeyPatch, provider: str, own_var: str
) -> None:
    for name in _PROVIDER_KEY_ENV_VARS.values():
        monkeypatch.setenv(name, f"value-of-{name}")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")

    env = _launch({"provider": provider}).env

    assert env[own_var] == f"value-of-{own_var}"
    for name in _PROVIDER_KEY_ENV_VARS.values():
        if name != own_var:
            assert name not in env
    assert "COPILOT_GITHUB_TOKEN" not in env


def test_run_child_env_drops_every_provider_api_key_for_github_copilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _PROVIDER_KEY_ENV_VARS.values():
        monkeypatch.setenv(name, f"value-of-{name}")

    env = _launch().env

    assert not set(_PROVIDER_KEY_ENV_VARS.values()) & set(env)


def test_run_child_env_drops_the_default_routing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_API_KEY", "routing-secret")

    assert "JEV_API_KEY" not in _launch().env


def test_run_child_env_drops_the_configured_routing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_ROUTING_KEY", "routing-secret")
    launches: list[_Launch] = []
    process = _scripted_process(json.dumps(_TRIAGE_JSON))

    def factory(command: Sequence[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        launches.append(_Launch(list(command), cwd, env))
        return process

    runtime = PiAgentRuntime(
        PiConfig(),
        data_dir=Path("/data"),
        process_factory=factory,
        routing_api_key_env_var="CUSTOM_ROUTING_KEY",
    )

    runtime.run(_request(AgentRole.TRIAGE))

    assert "CUSTOM_ROUTING_KEY" not in launches[0].env


@pytest.mark.parametrize(
    "env_var",
    [
        "GITHUB_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "JEV_API_KEY",
        *sorted(_PROVIDER_KEY_ENV_VARS.values()),
    ],
)
@pytest.mark.parametrize("provider", ["github-copilot", "anthropic", "openai"])
def test_run_redacts_every_known_credential_value_from_failure_reason(
    monkeypatch: pytest.MonkeyPatch, env_var: str, provider: str
) -> None:
    monkeypatch.setenv(env_var, _PLAIN_SECRET)

    reason = _failure_reason_when_pi_writes(f"auth failed for {_PLAIN_SECRET}", provider=provider)

    assert _PLAIN_SECRET not in reason
    assert "auth failed for [REDACTED]" in reason


# ---------------------------------------------------------------------------
# run: request validation shared with Copilot (validate_runtime_request)
# ---------------------------------------------------------------------------


def test_run_implementer_without_workspace_path_raises() -> None:
    runtime = _runtime()
    request = _request(AgentRole.IMPLEMENTER)

    with pytest.raises(ValueError, match="IMPLEMENTER requests require workspace_path"):
        runtime.run(request)


def test_run_change_set_correction_without_workspace_path_raises_correction_wording() -> None:
    runtime = _runtime()
    request = _correction_request(workspace_path=None)

    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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
    def factory(command: list[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
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
    process = FakePiProcess()
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
    process = FakePiProcess()
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


def test_run_failure_reason_stays_within_shared_runtime_limit() -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr("boom " * 5000)
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(_request(AgentRole.TRIAGE))

    assert result.failure_reason is not None
    assert result.failure_reason.startswith("pi process exited with code 1")
    assert len(result.failure_reason) <= RUNTIME_FAILURE_REASON_LIMIT


# ---------------------------------------------------------------------------
# run: timeout aborts, then kills if still alive (Step 3.4)
# ---------------------------------------------------------------------------


def test_run_timeout_sends_abort_then_kills_process_group(
    pi_fake_clock: FakePiClock, killpg_calls: list[tuple[int, int]]
) -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    # No agent_settled event is ever written, and the process never exits --
    # simulates pi not settling within the request timeout.
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    sent_types = [command.get("type") for command in process.sent_commands()]
    assert "abort" in sent_types
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}
    # No get_messages call ever completed (wait_for_settled itself never
    # settled), so the best-effort get_messages retry (Step 4.2) is attempted
    # but also gets nothing back -- usage stays unknown, not zeroed out.
    assert "get_messages" in sent_types
    assert result.usage is None


def test_run_timeout_gives_best_effort_usage_read_its_own_short_deadline(
    pi_fake_clock: FakePiClock,
) -> None:
    """The request's deadline has already passed when the best-effort
    ``get_messages`` runs, so it gets its own bounded window, not the request's."""
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=1))

    # 1 s request timeout, then the 2 s best-effort window, then nothing more.
    assert pi_fake_clock.now == pytest.approx(1000.0 + 1 + _BEST_EFFORT_USAGE_DEADLINE_SECONDS)


def test_run_timeout_after_malformed_usage_still_aborts_and_kills(
    pi_fake_clock: FakePiClock, killpg_calls: list[tuple[int, int]]
) -> None:
    """A malformed value (e.g. a negative token count) in the settled call's
    own ``get_messages`` read must not raise out of ``run()`` before it
    reaches the timeout path -- it is treated as unreported usage instead,
    and pi is still aborted and killed when the subsequent
    ``get_last_assistant_text`` call times out."""
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": -5}, model="claude-sonnet-5"),
    )
    # No response ever arrives for get_last_assistant_text (c3), and the
    # process never exits on its own.
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = _request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens is None
    sent_types = [command.get("type") for command in process.sent_commands()]
    assert "abort" in sent_types
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}


def test_run_timeout_with_undecodable_leftover_output_keeps_partial_usage(
    pi_fake_clock: FakePiClock, killpg_calls: list[tuple[int, int]]
) -> None:
    """AC6: killing a wedged pi reads its leftover output, and a text-mode
    ``Popen`` raises ``UnicodeDecodeError`` on a split UTF-8 character there.
    That must not replace the failed result or lose the usage read so far."""
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": 40, "output": 10}, model="claude-sonnet-5"),
    )
    process.communicate_error = UnicodeDecodeError("utf-8", b"\xe2\x82", 0, 2, "unexpected end")
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=1))

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens == 40
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}


def test_default_process_factory_tolerates_a_split_utf8_character_when_killed() -> None:
    """The real ``Popen`` the runtime starts must decode leniently: a child
    killed mid-character leaves bytes ``communicate()`` cannot strictly decode."""
    script = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xe2\\x82')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.write('ready\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(60)\n"
    )
    process = _default_process_factory([sys.executable, "-c", script], Path.cwd(), dict(os.environ))
    assert process.stderr is not None
    assert process.stderr.readline() == "ready\n"

    process.kill()
    stdout, _stderr = process.communicate()

    assert stdout == "\ufffd"


def test_run_does_not_kill_a_pi_that_exits_on_stdin_eof_after_a_timeout(
    pi_fake_clock: FakePiClock, killpg_calls: list[tuple[int, int]]
) -> None:
    """pi shuts down on stdin EOF, not necessarily in response to ``abort``.

    On timeout the runtime must close stdin after sending the best-effort
    ``abort``: a pi that only exits once its stdin closes
    (:class:`StdinEofExitProcess`) must not be escalated to a process-group kill.
    """
    process = StdinEofExitProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=1))

    assert result.failure_reason == "pi timed out after 1 seconds"
    assert "abort" in [command.get("type") for command in process.sent_commands()]
    assert process.poll() == 0
    assert killpg_calls == []


def test_run_process_exits_before_settling_reports_unknown_usage() -> None:
    process = FakePiProcess()
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


def test_run_timeout_after_settling_keeps_partial_usage(pi_fake_clock: FakePiClock) -> None:
    """AC6: a timeout after the call settled still records usage read so far.

    The settled call's own ``get_messages`` read already captured usage
    before ``get_last_assistant_text`` stalls past the timeout -- no
    best-effort retry is needed (or attempted) here.
    """
    process = FakePiProcess()
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
    # Usage was already read, so no best-effort retry extends the wait.
    assert pi_fake_clock.now == pytest.approx(1000.0 + 1)


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


def test_usage_from_pi_messages_fractional_token_count_stays_unknown() -> None:
    """Token counts are whole numbers -- a fractional one is malformed, as in
    the Copilot runtime, and is not truncated or rounded."""
    messages = [_assistant_message({"input": 10.5, "output": 5.0, "cacheWrite": 2.5})]

    usage = usage_from_pi_messages(messages)

    assert usage.input_tokens is None
    assert usage.output_tokens == 5
    assert usage.cache_write_tokens is None
    assert usage.model_usage[0].input_tokens is None


def test_usage_from_pi_messages_negative_field_value_stays_unknown() -> None:
    """A negative token count is malformed -- pi never legitimately reports
    one, and :class:`~software_agent_factory.models.ModelUsage`/
    :class:`~software_agent_factory.models.UsageMetrics` reject it outright
    -- so it must be treated as not reported, not crash the mapping."""
    messages = [_assistant_message({"input": -5, "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_nan_field_value_stays_unknown() -> None:
    messages = [_assistant_message({"input": float("nan"), "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_infinite_field_value_stays_unknown() -> None:
    messages = [_assistant_message({"input": float("inf"), "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_negative_cost_stays_unknown() -> None:
    messages = [_assistant_message({"input": 5, "cost": {"total": -0.5}})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.list_price_estimate_usd is None
    assert usage.input_tokens == 5


def test_usage_from_pi_messages_current_model_is_last_assistant_messages_own_model() -> None:
    """``current_model`` is the *last* assistant message's own ``model`` --
    ``None`` when that specific message did not report one, even when an
    earlier assistant message did (matches the corrected docstring; the
    prior implementation incorrectly kept the earlier model)."""
    messages = [
        _assistant_message({"input": 100}, model="claude-sonnet-5"),
        {"role": "assistant", "usage": {"input": 5}},
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.current_model is None
