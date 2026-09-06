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
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    ProjectBrief,
    ProjectPlan,
    RepositoryProfile,
    RepositorySkill,
    ResearchReport,
    ReviewReport,
    SkillGuidance,
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


def _skill_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.GENERATE_REPOSITORY_SKILL,
        "repository_profile": RepositoryProfile(
            manifest_fingerprint="a" * 64,
            dependency_fingerprint="b" * 64,
        ),
        "official_documentation_origins": ["https://react.dev", "https://vite.dev"],
        "practice_reference_urls": ["https://example.com/review.md"],
        "workspace_path": "/runs/RUN-1",
    }
    defaults.update(overrides)
    return _request(AgentRole.RESEARCHER, **defaults)


def _project_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.DECOMPOSE_PROJECT,
        "project_brief": ProjectBrief(
            id="project-1",
            title="Build validation",
            description="Reject blank names.",
            repository_path="/repo",
        ),
        "workspace_path": "/repo",
    }
    defaults.update(overrides)
    return _request(AgentRole.PLANNER, **defaults)


def test_project_decomposition_parses_project_plan() -> None:
    stdout = """
    {
      "schema_version": 1,
      "project_id": "project-1",
      "summary": "Implement validation.",
      "delivery_approach": "Use one coherent task.",
      "tasks": [{
        "id": 1,
        "title": "Reject blank names",
        "description": "Add validation and tests.",
        "acceptance_criteria": ["Blank names are rejected."],
        "constraints": [],
        "dependencies": [],
        "priority": null,
        "labels": []
      }],
      "created_at": "2026-09-06T08:00:00Z"
    }
    """

    artifact = parse_copilot_artifact(
        AgentRole.PLANNER,
        purpose=AgentPurpose.DECOMPOSE_PROJECT,
        stdout=stdout,
    )

    assert isinstance(artifact, ProjectPlan)
    assert artifact.tasks[0].id == 1


def test_project_decomposition_uses_read_only_repository_tools() -> None:
    runtime = CopilotAgentRuntime()
    request = _project_request()

    command = runtime._build_command(request, prompt="plan", cwd=Path("/repo"))

    assert command[command.index("--available-tools") + 1] == "glob,grep,view"
    assert "--no-custom-instructions" not in command


def test_build_command_for_skill_researcher_allows_read_only_web_access() -> None:
    runtime = CopilotAgentRuntime()
    request = _skill_request()

    command = runtime._build_command(request, prompt="research", cwd=Path("/runs/RUN-1"))

    assert command[command.index("--available-tools") + 1] == "web_fetch"
    assert "--no-custom-instructions" in command
    assert [command[index + 1] for index, item in enumerate(command) if item == "--allow-url"] == [
        "https://react.dev",
        "https://vite.dev",
        "https://example.com/review.md",
    ]
    assert "--allow-all-urls" not in command
    assert "--allow-all-paths" not in command
    assert "--add-dir" not in command
    available_tools = command[command.index("--available-tools") + 1]
    for repository_tool in ("bash", "view", "glob", "grep", "create", "edit"):
        assert repository_tool not in available_tools

    denied = [command[index + 1] for index, item in enumerate(command) if item == "--deny-tool"]
    assert denied == ["shell", "write"]
    assert "url" not in denied


def test_build_command_for_skill_researcher_deduplicates_allowed_urls() -> None:
    runtime = CopilotAgentRuntime()
    request = _skill_request(
        official_documentation_origins=["https://react.dev", "https://react.dev"],
        practice_reference_urls=["https://react.dev", "https://example.com/review.md"],
    )

    command = runtime._build_command(request, prompt="research", cwd=Path("/runs/RUN-1"))

    assert [command[index + 1] for index, item in enumerate(command) if item == "--allow-url"] == [
        "https://react.dev",
        "https://example.com/review.md",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://react.dev",
        "file:///etc/passwd",
        "https://user:token@react.dev/doc.md",
        "https://react.dev/doc .md",
    ],
)
def test_build_command_rejects_unsafe_skill_research_urls(url: str) -> None:
    runtime = CopilotAgentRuntime()
    request = _skill_request(practice_reference_urls=[url])

    with pytest.raises(ValueError, match="repository skill research URL"):
        runtime._build_command(request, prompt="research", cwd=Path("/runs/RUN-1"))


def test_build_command_requires_at_least_one_allowed_skill_research_url() -> None:
    runtime = CopilotAgentRuntime()
    request = _skill_request(official_documentation_origins=[], practice_reference_urls=[])

    with pytest.raises(ValueError, match="at least one allowed URL"):
        runtime._build_command(request, prompt="research", cwd=Path("/runs/RUN-1"))


def test_cwd_for_skill_request_uses_neutral_run_directory_not_operator_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "RUN-1"
    run_dir.mkdir(parents=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.chdir(repository)

    runtime = CopilotAgentRuntime()
    cwd = runtime._cwd_for(_skill_request(workspace_path=str(run_dir)))

    assert cwd == run_dir.resolve()
    assert cwd != repository.resolve()


def test_cwd_for_skill_request_refuses_to_fall_back_to_process_cwd() -> None:
    runtime = CopilotAgentRuntime()

    with pytest.raises(ValueError, match="neutral run directory"):
        runtime._cwd_for(_skill_request(workspace_path=None))


def test_cwd_for_read_only_role_still_falls_back_to_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = CopilotAgentRuntime()

    assert runtime._cwd_for(_request(AgentRole.TRIAGE)) == tmp_path.resolve()


def test_run_for_skill_request_uses_run_directory_and_neutral_permissions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "RUN-1"
    run_dir.mkdir(parents=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.chdir(repository)
    captured: dict[str, object] = {}

    stdout = RepositorySkill(
        dependency_fingerprint="b" * 64,
        simplify=SkillGuidance(summary="Simplify.", guidance=("Delete dead code.",)),
        polish=SkillGuidance(summary="Polish.", guidance=("Follow the docs.",)),
        uncertainties=("Fixture skill cites no external sources.",),
    ).model_dump_json()

    def fake_popen(command: list[str], **kwargs: object) -> _FakePopen:
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return _FakePopen(stdout=stdout)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_skill_request(workspace_path=str(run_dir)))

    assert result.success is True
    assert result.repository_skill is not None
    assert captured["cwd"] == run_dir.resolve()
    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("-C") + 1] == str(run_dir.resolve())
    assert command[command.index("--available-tools") + 1] == "web_fetch"
    assert "--no-custom-instructions" in command


def test_run_for_skill_request_without_run_directory_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_popen(command: list[str], **kwargs: object) -> _FakePopen:  # pragma: no cover
        raise AssertionError("copilot must not be launched without a run directory")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    runtime = CopilotAgentRuntime()

    with pytest.raises(ValueError, match="neutral run directory"):
        runtime.run(_skill_request(workspace_path=None))


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


def test_parse_repository_skill_artifact_for_research_purpose() -> None:
    stdout = RepositorySkill(
        dependency_fingerprint="a" * 64,
        simplify=SkillGuidance(
            summary="Simplify first.",
            guidance=("Remove unnecessary indirection.",),
        ),
        polish=SkillGuidance(
            summary="Polish second.",
            guidance=("Use the detected framework version.",),
        ),
        uncertainties=("Fixture skill has no external sources.",),
    ).model_dump_json()

    artifact = parse_copilot_artifact(
        AgentRole.RESEARCHER,
        purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
        stdout=stdout,
    )

    assert isinstance(artifact, RepositorySkill)


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


def test_missing_executable_returns_failure_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        raise FileNotFoundError(2, "No such file or directory: 'copilot'")

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE))

    assert result.success is False
    assert result.triage_result is None
    assert result.failure_reason is not None
    assert result.failure_reason.startswith("TRIAGE: copilot could not be started")
    assert "FileNotFoundError" in result.failure_reason
    assert "No such file or directory" in result.failure_reason


def test_launch_oserror_failure_reason_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ghp_secret_token_value"

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        raise PermissionError(13, f"Permission denied while using {secret}")

    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.REVIEWER))

    assert result.success is False
    assert result.failure_reason is not None
    assert "copilot could not be started (PermissionError)" in result.failure_reason
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
