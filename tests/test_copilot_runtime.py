from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from software_agent_factory.agents import AgentRequest
from software_agent_factory.copilot_runtime import (
    CopilotAgentRuntime,
    parse_copilot_artifact,
)
from software_agent_factory.models import (
    AgentRole,
    ChangeSet,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ResearchReport,
    ReviewReport,
    Specification,
    TestReport,
    TriageResult,
    WorkItem,
)
from software_agent_factory.prompts import build_prompt


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
    return AgentRequest(**defaults)  # type: ignore[arg-type]


class _FakePopen:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: subprocess.TimeoutExpired | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self._communicate_calls = 0
        self.returncode = returncode
        self.pid = 43210

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self._communicate_calls += 1
        if self._timeout is not None and self._communicate_calls == 1:
            raise self._timeout
        return self._stdout, self._stderr


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
def test_build_command_for_read_only_role_uses_exact_read_only_tools(role: AgentRole) -> None:
    runtime = CopilotAgentRuntime()
    request = _request(role)

    command = runtime._build_command(request, prompt="triage", cwd=Path("/repo"))

    assert command[:4] == ["copilot", "-C", "/repo", "--model"]
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert command[command.index("--stream") + 1] == "off"
    assert "--disable-builtin-mcps" in command
    assert "--no-remote-export" in command
    assert "--no-auto-update" in command
    assert "--no-ask-user" in command
    assert "--allow-all-tools" in command
    available_tools = command[command.index("--available-tools") + 1]
    assert available_tools == "glob,grep,view"
    assert "bash" not in available_tools
    assert "create" not in available_tools
    assert "edit" not in available_tools

    denied = [command[index + 1] for index, item in enumerate(command) if item == "--deny-tool"]
    assert denied == ["url"]


def test_build_command_for_implementer_denies_push_and_network() -> None:
    runtime = CopilotAgentRuntime()
    request = _request(AgentRole.IMPLEMENTER, workspace_path="/repo/worktree")

    command = runtime._build_command(request, prompt="implement", cwd=Path("/repo/worktree"))

    assert command[command.index("--available-tools") + 1] == "glob,grep,view,create,edit,bash"
    denied = [command[index + 1] for index, item in enumerate(command) if item == "--deny-tool"]
    assert "url" in denied
    assert "shell(git push)" in denied
    assert "shell(gh:*)" in denied


def test_run_uses_workspace_cwd_and_scrubs_github_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakePopen(
            stdout=(
                '{"summary":"Applied fix","changed_files":["app.py"],'
                '"tests_added":[],"commands_run":["pytest"]}'
            )
        )

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_value")
    monkeypatch.setenv("GH_TOKEN", "gho_another_secret")
    monkeypatch.setenv("GIT_ASKPASS", "secret-helper")
    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace)))

    assert result.success is True
    assert result.change_set == ChangeSet(
        summary="Applied fix",
        changed_files=["app.py"],
        tests_added=[],
        commands_run=["pytest"],
    )
    assert captured["kwargs"]["cwd"] == workspace
    assert captured["kwargs"]["start_new_session"] is True
    env = captured["kwargs"]["env"]
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "GIT_ASKPASS" not in env


@pytest.mark.parametrize(
    ("role", "stdout", "expected_type"),
    [
        (
            AgentRole.TRIAGE,
            (
                '{"type":"assistant.message","message":{"content":['
                '{"type":"output_text","text":"'
                '{"factory_eligible":true,"complexity":"L1","risk":"R1",'
                '"requirements_quality":"clear","needs_research":false,'
                '"dependencies":[],"unknowns":[],"confidence":0.8}'
                '"}]}}'
            ),
            TriageResult,
        ),
        (
            AgentRole.REFINER,
            (
                '{"problem":"Reject blank names","acceptance_criteria":["Reject blanks"],'
                '"constraints":[],"assumptions":["API contract stays the same"],'
                '"unknowns":[],"dependencies":[],"risk_flags":[],"confidence":0.7}'
            ),
            Specification,
        ),
        (
            "RESEARCHER",
            (
                '{"message":{"content":[{"text":"'
                '{"question":"How are names validated?","findings":["No current guard"],'
                '"evidence":["src/api.py"],"implications":["Add request validation"],'
                '"uncertainty":["No integration test found"]}'
                '"}]}}'
            ),
            ResearchReport,
        ),
        (
            AgentRole.PLANNER,
            (
                '{"type":"assistant.message.delta","delta":"```json\\n'
                '{"summary":"Implement validation","steps":[{"id":"edit","goal":"Add guard",'
                '"likely_files":["src/api.py"],"validation":["pytest"]}],"expected_scope":'
                '{"modules":["src"],"estimated_files_min":1,"estimated_files_max":2},'
                '"test_strategy":["pytest"],"risks":["regression"]}'
                '\\n```"}'
            ),
            ExecutionPlan,
        ),
        (
            AgentRole.IMPLEMENTER,
            (
                '{"type":"response.completed","response":{"output":[{"type":"message",'
                '"content":[{"type":"output_text","text":"'
                '{"summary":"Updated validation","changed_files":["src/api.py"],'
                '"tests_added":["tests/test_api.py"],"commands_run":["pytest"]}'
                '"}]}]}}'
            ),
            ChangeSet,
        ),
        (
            AgentRole.TESTER,
            (
                "Verification complete\n"
                '{"passed":true,"findings":["No issues found"],'
                '"suggested_tests":[],"confidence":0.9}'
            ),
            TestReport,
        ),
        (
            AgentRole.REVIEWER,
            (
                '{"assistant":{"content":"{\\"approved\\":true,\\"findings\\":[],'
                '\\"scope_concerns\\":[],\\"security_concerns\\":[],'
                '\\"compatibility_concerns\\":[],\\"suggested_changes\\":[]}"}}'
            ),
            ReviewReport,
        ),
    ],
)
def test_parse_copilot_artifact_for_every_role(
    role: AgentRole | str,
    stdout: str,
    expected_type: type[object],
) -> None:
    artifact = parse_copilot_artifact(role, stdout=stdout)
    assert isinstance(artifact, expected_type)


def test_parse_copilot_artifact_handles_fenced_json_plain_text_fallback() -> None:
    artifact = parse_copilot_artifact(
        AgentRole.PLANNER,
        stdout=(
            "Here is the plan:\n```json\n"
            '{"summary":"Implement validation","steps":[{"id":"edit","goal":"Add guard",'
            '"likely_files":["src/api.py"],"validation":["pytest"]}],"expected_scope":'
            '{"modules":["src"],"estimated_files_min":1,"estimated_files_max":2},'
            '"test_strategy":["pytest"],"risks":[]}\n```'
        ),
    )

    assert artifact == ExecutionPlan(
        summary="Implement validation",
        steps=[
            PlanStep(
                id="edit",
                goal="Add guard",
                likely_files=["src/api.py"],
                validation=["pytest"],
            )
        ],
        expected_scope=ExpectedScope(
            modules=["src"],
            estimated_files_min=1,
            estimated_files_max=2,
        ),
        test_strategy=["pytest"],
        risks=[],
    )


def test_parse_copilot_artifact_reads_actual_assistant_message_data_shape() -> None:
    artifact = parse_copilot_artifact(
        AgentRole.REVIEWER,
        stdout=(
            '{"type":"assistant.message","data":{"messageId":"msg_123","model":"gpt-5.6-sol",'
            '"content":"{\\"approved\\":true,\\"findings\\":[],\\"scope_concerns\\":[],'
            '\\"security_concerns\\":[],\\"compatibility_concerns\\":[],'
            '\\"suggested_changes\\":[]}","role":"assistant"}}'
        ),
    )

    assert artifact == ReviewReport(
        approved=True,
        findings=[],
        scope_concerns=[],
        security_concerns=[],
        compatibility_concerns=[],
        suggested_changes=[],
    )


def test_parse_copilot_artifact_prefers_final_assistant_content_over_prompt_echo() -> None:
    prompt_with_embedded_json = (
        'Return exactly one JSON object. Work item: {"id":"WI-1","title":"Smoke",'
        '"description":"Prompt echo should be ignored."}'
    )
    final_content = (
        '{"schema_version":1,"factory_eligible":true,"complexity":"L1","risk":"R1",'
        '"requirements_quality":"clear","needs_research":false,'
        '"dependencies":[],"unknowns":[],"confidence":0.9}'
    )

    artifact = parse_copilot_artifact(
        AgentRole.TRIAGE,
        stdout="\n".join(
            [
                '{"type":"user.message","data":{"content":'
                + _json_string(prompt_with_embedded_json)
                + "}}",
                '{"type":"assistant.message","data":{"messageId":"msg_123","model":"claude-sonnet-5","content":'
                + _json_string(final_content)
                + ',"role":"assistant"}}',
                '{"type":"model.response","data":{"response":{"role":"assistant","content":'
                + _json_string(final_content)
                + ',"refusal":null}}}',
            ]
        ),
    )

    assert artifact == TriageResult(
        factory_eligible=True,
        complexity="L1",
        risk="R1",
        requirements_quality="clear",
        needs_research=False,
        dependencies=[],
        unknowns=[],
        confidence=0.9,
    )


def test_build_prompt_for_triage_requires_exact_enum_values() -> None:
    prompt = build_prompt(_request(AgentRole.TRIAGE))
    assert "complexity must be one of L0, L1, L2, L3" in prompt
    assert "risk must be one of R0, R1, R2, R3" in prompt


def test_malformed_output_returns_failure_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        return _FakePopen(stdout='{"not":"a triage result"}')

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE))

    assert result.success is False
    assert result.failure_reason is not None
    assert "TRIAGE response did not validate as TriageResult" in result.failure_reason


def test_nonzero_exit_returns_failure_and_redacts_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ghp_secret_token_value"

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        return _FakePopen(
            stdout="",
            stderr=f"fatal: bad auth {secret}",
            returncode=23,
        )

    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE))

    assert result.success is False
    assert result.failure_reason is not None
    assert "code 23" in result.failure_reason
    assert secret not in result.failure_reason
    assert "[REDACTED]" in result.failure_reason


def test_timeout_kills_process_group_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: dict[str, object] = {}
    timeout = subprocess.TimeoutExpired(
        cmd=["copilot"],
        timeout=5,
        output="partial stdout",
        stderr="partial stderr",
    )

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        return _FakePopen(stdout="", stderr="", timeout=timeout)

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        killed["pid"] = pid
        killed["sig"] = sig

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr("software_agent_factory.copilot_runtime.os.killpg", fake_killpg)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=5))

    assert result.success is False
    assert result.failure_reason is not None
    assert "timed out after 5s" in result.failure_reason
    assert killed == {"pid": 43210, "sig": signal.SIGKILL}


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)
