from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest
import typer

from software_agent_factory import cli
from software_agent_factory.agents import AgentRequest, AgentResult
from software_agent_factory.claude_code_runtime import (
    ClaudeCodeAgentRuntime,
    usage_from_result_event,
)
from software_agent_factory.config import FactoryConfig, load_config
from software_agent_factory.models import (
    AgentRole,
    ChangeSet,
    Complexity,
    ModelUsage,
    Risk,
    TriageResult,
    WorkItem,
)

TRIAGE_JSON = (
    '{"factory_eligible":true,"complexity":"L1","risk":"R1","needs_research":false,'
    '"dependencies":[],"unknowns":[],"confidence":0.8}'
)
CHANGE_SET_JSON = (
    '{"summary":"Applied fix","changed_files":["app.py"],"tests_added":[],'
    '"commands_run":["pytest"]}'
)
#: Independent oracle for ``claude --effort`` (Claude Code 2.1.289 ``--help``).
CLAUDE_EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


def _request(role: AgentRole, **overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "role": role,
        "model": "sonnet",
        "reasoning": "high",
        "work_item": WorkItem(id="WI-1", title="Reject blanks", description="Return 400."),
        "timeout_seconds": 30,
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


def _result_event(text: str | None, **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "duration_ms": 1766,
        "duration_api_ms": 1741,
        "total_cost_usd": 0.013876,
        "usage": {
            "input_tokens": 9,
            "cache_creation_input_tokens": 6646,
            "cache_read_input_tokens": 21,
            "output_tokens": 115,
            "output_tokens_details": {"thinking_tokens": 103},
        },
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": 9,
                "outputTokens": 115,
                "cacheReadInputTokens": 21,
                "cacheCreationInputTokens": 6646,
                "thinkingTokens": 103,
                "costUSD": 0.013876,
            }
        },
    }
    event.update(overrides)
    return event


def _stream(*events: dict[str, object]) -> str:
    # A real init event lists every slash command and is well over 600 chars.
    init = {"type": "system", "subtype": "init", "slash_commands": ["x" * 40] * 30}
    return "\n".join(json.dumps(e) for e in (init, *events)) + "\n"


class _FakePopen:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises
        self.returncode = returncode
        self.pid = 43210
        self.stdin_input: str | None = None

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        if input is not None:
            self.stdin_input = input
        if self._raises is not None:
            exc, self._raises = self._raises, None
            raise exc
        return self._stdout, self._stderr


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch, popen: _FakePopen) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> _FakePopen:
        captured["command"] = command
        captured.update(kwargs)
        return popen

    monkeypatch.setattr("software_agent_factory.claude_code_runtime.subprocess.Popen", fake_popen)
    return captured


def _record_killpg(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, signal.Signals]]:
    killed: list[tuple[int, signal.Signals]] = []
    # double-waiver: B1 — os.killpg signals a real process group
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )
    return killed


def _assert_failed(result: AgentResult, *fragments: str) -> str:
    assert result.success is False
    assert result.failure_reason is not None
    for fragment in fragments:
        assert fragment in result.failure_reason
    return result.failure_reason


def _flag_value(command: list[str], flag: str) -> str:
    assert flag in command, f"{flag} missing from {command}"
    return command[command.index(flag) + 1]


def _values_after(command: list[str], flag: str) -> list[str]:
    assert flag in command, f"{flag} missing from {command}"
    return command[command.index(flag) + 1 :]


@pytest.mark.parametrize(
    "role", [AgentRole.TRIAGE, AgentRole.PLANNER, AgentRole.TESTER, AgentRole.REVIEWER]
)
def test_read_only_roles_get_read_tools_and_no_allow_rules(role: AgentRole) -> None:
    command = ClaudeCodeAgentRuntime().build_command(_request(role))

    assert _flag_value(command, "--tools") == "Read,Grep,Glob"
    assert "--allowedTools" not in command
    assert _values_after(command, "--disallowedTools") == ["WebFetch", "WebSearch"]


def test_command_isolates_claude_from_user_setup_and_keeps_files_in_the_worktree() -> None:
    command = ClaudeCodeAgentRuntime().build_command(_request(AgentRole.TRIAGE, reasoning="xhigh"))

    assert command[:2] == ["claude", "-p"]
    assert _flag_value(command, "--model") == "sonnet"
    assert _flag_value(command, "--effort") == "xhigh"
    assert _flag_value(command, "--setting-sources") == ""
    assert _flag_value(command, "--permission-mode") == "acceptEdits"
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command
    assert "--bare" not in command
    assert "bypassPermissions" not in command


def test_implementer_gets_bash_but_not_commit_push_gh_or_network() -> None:
    command = ClaudeCodeAgentRuntime().build_command(_request(AgentRole.IMPLEMENTER))

    assert _flag_value(command, "--tools") == "Read,Edit,Write,Bash,Grep,Glob"
    assert _flag_value(command, "--allowedTools") == "Bash"
    assert _values_after(command, "--disallowedTools") == [
        "WebFetch",
        "WebSearch",
        "Bash(git commit:*)",
        "Bash(git push:*)",
        "Bash(gh:*)",
        "Bash(curl:*)",
        "Bash(wget:*)",
    ]


def test_run_parses_the_result_text_into_the_role_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(_result_event(CHANGE_SET_JSON))))

    result = ClaudeCodeAgentRuntime().run(
        _request(AgentRole.IMPLEMENTER, workspace_path=str(tmp_path))
    )

    assert result.success is True, result.failure_reason
    assert result.change_set == ChangeSet(
        summary="Applied fix", changed_files=["app.py"], tests_added=[], commands_run=["pytest"]
    )


def test_run_starts_claude_in_the_worktree_with_the_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    popen = _FakePopen(stdout=_stream(_result_event(TRIAGE_JSON)))
    captured = _install_fake_popen(monkeypatch, popen)

    ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE, workspace_path=str(tmp_path)))

    assert captured["cwd"] == tmp_path
    assert captured["stdin"] is subprocess.PIPE
    assert captured["start_new_session"] is True
    assert popen.stdin_input is not None
    assert "Reject blanks" in popen.stdin_input


def test_child_env_has_no_github_or_anthropic_billing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_popen(
        monkeypatch, _FakePopen(stdout=_stream(_result_event(TRIAGE_JSON)))
    )
    for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.setenv(name, f"secret-{name}")
    for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.invalid")

    ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE))

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    for name in (
        "GH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "ANTHROPIC_BASE_URL",
    ):
        assert name not in env


def test_failure_reason_redacts_a_scrubbed_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    _install_fake_popen(
        monkeypatch, _FakePopen(stderr="auth failed for anthropic-secret-value", returncode=1)
    )

    reason = _assert_failed(ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)))

    assert "anthropic-secret-value" not in reason


def test_run_records_usage_and_list_price(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(_result_event(TRIAGE_JSON))))

    result = ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE))

    assert result.usage is not None
    assert result.usage.list_price_estimate_usd == pytest.approx(0.013876)


def test_usage_maps_every_token_field_per_call_and_per_model() -> None:
    usage = usage_from_result_event(_result_event(TRIAGE_JSON))

    assert usage is not None
    assert usage.current_model == "claude-sonnet-5-5"
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (9, 115, 103)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (21, 6646)
    assert (usage.total_api_duration_ms, usage.session_duration_ms) == (1741, 1766)
    assert usage.model_usage == (
        ModelUsage(
            model="claude-sonnet-5-5",
            input_tokens=9,
            output_tokens=115,
            reasoning_tokens=103,
            cache_read_tokens=21,
            cache_write_tokens=6646,
            list_price_estimate_usd=0.013876,
        ),
    )


def test_usage_skips_malformed_model_entries() -> None:
    event = _result_event(
        TRIAGE_JSON,
        modelUsage={
            "": {"inputTokens": 1},
            "   ": {"inputTokens": 1},
            "x": "not-a-dict",
            " claude-haiku ": {"inputTokens": 2},
        },
    )

    usage = usage_from_result_event(event)

    assert usage is not None
    assert [m.model for m in usage.model_usage] == ["claude-haiku"]


def test_usage_names_no_current_model_when_several_models_ran() -> None:
    event = _result_event(
        TRIAGE_JSON,
        modelUsage={"claude-haiku": {"inputTokens": 2}, "claude-sonnet": {"inputTokens": 3}},
    )

    usage = usage_from_result_event(event)

    assert usage is not None
    assert usage.current_model is None
    assert len(usage.model_usage) == 2


def test_usage_is_none_when_the_event_reports_nothing() -> None:
    assert usage_from_result_event({"type": "result"}) is None


def test_triage_artifact_parses_to_the_expected_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(_result_event(TRIAGE_JSON))))

    result = ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE))

    assert result.success is True
    assert result.triage_result is not None
    assert result.triage_result.model_dump(exclude={"provenance"}) == TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R1,
        needs_research=False,
        dependencies=[],
        unknowns=[],
        confidence=0.8,
    ).model_dump(exclude={"provenance"})


def test_error_result_keeps_its_text_despite_a_long_init_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _result_event(
        "Claude AI usage limit reached", is_error=True, subtype="error_during_execution"
    )
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(event), returncode=1))

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)),
        "claude reported an error (error_during_execution): Claude AI usage limit reached",
    )


def test_error_result_text_is_redacted_in_the_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret-value")
    event = _result_event("bad key anthropic-secret-value", is_error=True, subtype="error")
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(event), returncode=1))

    reason = _assert_failed(ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)), "bad key")

    assert "anthropic-secret-value" not in reason


def test_missing_result_event_fails_with_exit_code_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout="", stderr="boom", returncode=2))

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)),
        "exited with code 2 and no result event",
        "boom",
    )


def test_nonzero_exit_after_a_success_result_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(
        monkeypatch, _FakePopen(stdout=_stream(_result_event(TRIAGE_JSON)), returncode=1)
    )

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)), "claude exited with code 1"
    )


def test_unparseable_result_text_fails_and_keeps_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(_result_event("no json here"))))

    result = ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE))

    _assert_failed(result, "TriageResult")
    assert result.usage is not None


def test_result_event_without_text_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(_result_event(None))))

    _assert_failed(ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)), "TriageResult")


def test_timeout_kills_the_process_group_and_keeps_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = subprocess.TimeoutExpired(cmd="claude", timeout=30, output="partial output")
    _install_fake_popen(monkeypatch, _FakePopen(raises=timeout))
    killed = _record_killpg(monkeypatch)

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)),
        "claude timed out after 30s",
        "partial output",
    )
    assert killed == [(43210, signal.SIGTERM)]


def test_interrupt_kills_the_process_group_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_popen(monkeypatch, _FakePopen(raises=KeyboardInterrupt()))
    killed = _record_killpg(monkeypatch)

    runtime, request = ClaudeCodeAgentRuntime(), _request(AgentRole.TRIAGE)

    with pytest.raises(KeyboardInterrupt):
        runtime.run(request)

    assert killed == [(43210, signal.SIGTERM)]


def test_missing_executable_fails_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_popen(*_args: object, **_kwargs: object) -> _FakePopen:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(
        "software_agent_factory.claude_code_runtime.subprocess.Popen", raising_popen
    )

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)),
        "claude could not be started (FileNotFoundError)",
    )


def test_unsupported_reasoning_fails_before_starting_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_fake_popen(monkeypatch, _FakePopen())

    _assert_failed(
        ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE, reasoning="extreme")),
        "does not accept reasoning 'extreme'",
    )
    assert captured == {}


def test_cli_builds_the_claude_code_runtime() -> None:
    runtime = cli._build_runtime(cli.RuntimeChoice.CLAUDE_CODE, load_config(None))

    assert isinstance(runtime, ClaudeCodeAgentRuntime)


def test_packaged_claude_profile_uses_effort_levels_only() -> None:
    models = load_config(None, model_profile="claude").models
    reasoning = {
        "triage": models.triage.reasoning,
        "planner": models.planner.reasoning,
        "tester": models.tester.reasoning,
        "reviewer": models.reviewer.reasoning,
        **{tier.value: worker.reasoning for tier, worker in models.workers.items()},
    }

    assert {
        name: level for name, level in reasoning.items() if level not in CLAUDE_EFFORT_LEVELS
    } == {}


def _claude_config(**routing: object) -> FactoryConfig:
    payload = load_config(None, model_profile="claude").model_dump(mode="json")
    payload["model_profiles"]["economy"]["triage"]["reasoning"] = "minimal"
    payload["routing"].update(routing)
    return FactoryConfig.model_validate(payload)


def test_effort_preflight_checks_profiles_that_routing_can_select() -> None:
    options = [
        {"id": "cheap", "route": "SINGLE", "complexity": "L0", "risk": "R0"},
        {"id": "full", "route": "FULL", "complexity": "L3", "risk": "R1"},
    ]
    options[0]["model_profile"] = "economy"
    config = _claude_config(enabled=True, options=options)

    with pytest.raises(typer.Exit):
        cli._require_claude_code_effort(cli.RuntimeChoice.CLAUDE_CODE, config)


def test_effort_preflight_ignores_profiles_when_routing_is_off() -> None:
    config = _claude_config(enabled=False)

    cli._require_claude_code_effort(cli.RuntimeChoice.CLAUDE_CODE, config)


def test_switch_values_are_removed_but_not_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "true")
    event = _result_event("is_error was true", is_error=True, subtype="error")
    _install_fake_popen(monkeypatch, _FakePopen(stdout=_stream(event), returncode=1))

    _assert_failed(ClaudeCodeAgentRuntime().run(_request(AgentRole.TRIAGE)), "is_error was true")
