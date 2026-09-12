from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from software_agent_factory.agents import AgentRequest
from software_agent_factory.copilot_runtime import (
    CopilotAgentRuntime,
    ScanStats,
    _iter_json_objects,
    parse_copilot_artifact,
    parse_copilot_usage,
)
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ContextTier,
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
    return AgentRequest(**defaults)


class _FakePopen:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timeout: subprocess.TimeoutExpired | None = None,
        timeout_calls: int = 1,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self._timeout_calls = timeout_calls
        self._communicate_calls = 0
        self.returncode = returncode
        self.pid = 43210

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        self._communicate_calls += 1
        if self._timeout is not None and self._communicate_calls <= self._timeout_calls:
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
    assert command[command.index("--context") + 1] == "default"
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


def test_build_command_passes_context_tier_and_usage_output_path(tmp_path: Path) -> None:
    runtime = CopilotAgentRuntime()
    request = _request(AgentRole.TRIAGE, context_tier=ContextTier.LONG_CONTEXT)
    usage_path = tmp_path / "usage.json"

    command = runtime._build_command(
        request,
        prompt="triage",
        cwd=Path("/repo"),
        usage_output_path=usage_path,
    )

    assert command[command.index("--context") + 1] == "long_context"
    assert command[command.index("--usage-output-file") + 1] == str(usage_path)


def test_parse_copilot_usage_preserves_only_reported_values() -> None:
    usage = parse_copilot_usage(
        {
            "totalPremiumRequestCost": 1.0,
            "totalUserRequests": 1,
            "totalNanoAiu": 292470077500,
            "totalApiDurationMs": 890,
            "currentModel": "gpt-5.6-sol",
            "lastCallInputTokens": 1195,
            "lastCallOutputTokens": 59,
            "tokenDetails": {
                "input": {"tokenCount": 1195},
                "output": {"tokenCount": 59},
                "cache_read": {"tokenCount": 47104},
                "cache_write": {"tokenCount": 0},
            },
            "unknown": {"secret": "ignored"},
            "modelMetrics": {
                "gpt-5.6-sol": {
                    "requests": {"count": 1, "cost": 1.0},
                    "usage": {
                        "inputTokens": 1195,
                        "outputTokens": 59,
                        "cacheReadTokens": 47104,
                        "cacheWriteTokens": 0,
                        "reasoningTokens": 18,
                    },
                    "totalNanoAiu": 292470077500,
                }
            },
        }
    )

    assert usage is not None
    assert usage.total_premium_request_cost == 1.0
    assert usage.total_nano_aiu == 292470077500
    assert usage.input_tokens == 1195
    assert usage.cache_write_tokens == 0
    assert usage.model_usage[0].input_tokens == 1195
    assert usage.model_usage[0].reasoning_tokens == 18
    assert not hasattr(usage, "unknown")


@pytest.mark.parametrize("payload", [None, [], {"totalNanoAiu": -1}, {"totalUserRequests": True}])
def test_parse_copilot_usage_returns_none_without_valid_reported_values(payload: object) -> None:
    assert parse_copilot_usage(payload) is None


def test_parse_copilot_usage_accepts_whole_float_counters_and_rejects_non_finite_costs() -> None:
    usage = parse_copilot_usage(
        {
            "totalNanoAiu": 1e9,
            "totalUserRequests": 2.0,
            "totalPremiumRequestCost": float("nan"),
            "tokenDetails": {"input": {"tokenCount": 1e4}},
        }
    )

    assert usage is not None
    assert usage.total_nano_aiu == 1_000_000_000
    assert usage.total_user_requests == 2
    assert usage.input_tokens == 10_000
    assert usage.total_premium_request_cost is None


def test_build_command_for_implementer_denies_push_and_network() -> None:
    runtime = CopilotAgentRuntime()
    request = _request(AgentRole.IMPLEMENTER, workspace_path="/repo/worktree")

    command = runtime._build_command(request, prompt="implement", cwd=Path("/repo/worktree"))

    assert command[command.index("--available-tools") + 1] == "glob,grep,view,create,edit,bash"
    denied = [command[index + 1] for index, item in enumerate(command) if item == "--deny-tool"]
    assert "url" in denied
    assert "shell(git push)" in denied
    assert "shell(git commit)" in denied
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
    assert captured["kwargs"]["cwd"] == workspace  # type: ignore[index]
    assert captured["kwargs"]["start_new_session"] is True  # type: ignore[index]
    env = captured["kwargs"]["env"]  # type: ignore[index]
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "GIT_ASKPASS" not in env


def test_run_returns_usage_from_temporary_usage_output_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured_usage_path: list[Path] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakePopen:
        usage_path = Path(command[command.index("--usage-output-file") + 1])
        captured_usage_path.append(usage_path)
        usage_path.write_text(
            (
                '{"totalPremiumRequestCost":1,"totalNanoAiu":123,'
                '"modelMetrics":{"claude-sonnet-5":{"usage":'
                '{"inputTokens":100,"outputTokens":20,"reasoningTokens":5}}}}'
            ),
            encoding="utf-8",
        )
        return _FakePopen(
            stdout=(
                '{"summary":"Applied fix","changed_files":["app.py"],'
                '"tests_added":[],"commands_run":["pytest"]}'
            )
        )

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    result = CopilotAgentRuntime().run(
        _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))
    )

    assert result.success is True
    assert result.usage is not None
    assert result.usage.total_premium_request_cost == 1.0
    assert result.usage.model_usage[0].input_tokens == 100
    assert captured_usage_path and not captured_usage_path[0].exists()


def test_run_falls_back_to_result_stream_when_usage_file_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_popen(command: list[str], **_kwargs: object) -> _FakePopen:
        usage_path = Path(command[command.index("--usage-output-file") + 1])
        usage_path.write_text("{not-json", encoding="utf-8")
        return _FakePopen(
            stdout="\n".join(
                [
                    (
                        '{"summary":"Applied fix","changed_files":["app.py"],'
                        '"tests_added":[],"commands_run":["pytest"]}'
                    ),
                    (
                        '{"type":"result","usage":{"premiumRequests":1.5,'
                        '"totalApiDurationMs":250,"sessionDurationMs":1200}}'
                    ),
                ]
            )
        )

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)

    result = CopilotAgentRuntime().run(
        _request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))
    )

    assert result.success is True
    assert result.usage is not None
    assert result.usage.premium_requests == 1.5
    assert result.usage.total_api_duration_ms == 250
    assert result.usage.session_duration_ms == 1200


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


def test_parse_copilot_artifact_handles_pretty_printed_json() -> None:
    artifact = parse_copilot_artifact(
        AgentRole.IMPLEMENTER,
        stdout=json.dumps(
            {
                "summary": "Updated validation",
                "changed_files": ["src/api.py"],
                "tests_added": ["tests/test_api.py"],
                "commands_run": ["pytest"],
            },
            indent=2,
        ),
    )

    assert artifact == ChangeSet(
        summary="Updated validation",
        changed_files=["src/api.py"],
        tests_added=["tests/test_api.py"],
        commands_run=["pytest"],
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


def test_parse_copilot_artifact_ignores_lifecycle_events_before_direct_json() -> None:
    artifact = parse_copilot_artifact(
        AgentRole.TESTER,
        stdout="\n".join(
            [
                (
                    '{"type":"session.mcp_server_status_changed","data":{"name":"github"},'
                    '"ephemeral":true,"id":"event-1","timestamp":"2026-09-09T22:29:32Z",'
                    '"parentId":null}'
                ),
                (
                    '{"passed":true,"findings":["No issues found"],'
                    '"suggested_tests":[],"confidence":0.9}'
                ),
            ]
        ),
    )

    assert artifact == TestReport(
        passed=True,
        findings=["No issues found"],
        suggested_tests=[],
        confidence=0.9,
    )


def test_parse_copilot_artifact_keeps_non_event_logs_before_direct_json() -> None:
    artifact = parse_copilot_artifact(
        AgentRole.TESTER,
        stdout="\n".join(
            [
                '{"level":"info","message":"starting session"}',
                (
                    '{"passed":true,"findings":["No issues found"],'
                    '"suggested_tests":[],"confidence":0.9}'
                ),
            ]
        ),
    )

    assert isinstance(artifact, TestReport)
    assert artifact.passed is True


def test_parse_copilot_artifact_does_not_validate_lifecycle_event_as_artifact() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_copilot_artifact(
            AgentRole.TESTER,
            stdout=(
                '{"type":"session.mcp_server_status_changed","data":{"name":"github"},'
                '"ephemeral":true,"id":"event-1","timestamp":"2026-09-09T22:29:32Z",'
                '"parentId":null}'
            ),
        )

    message = str(exc_info.value)
    assert "did not contain a parseable JSON object for TestReport" in message
    assert "type: Extra inputs are not permitted" not in message


def test_parse_copilot_artifact_ignores_result_event_without_artifact() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_copilot_artifact(
            AgentRole.TESTER,
            stdout='{"type":"result","usage":{"premiumRequests":1}}',
        )

    message = str(exc_info.value)
    assert "did not contain a parseable JSON object for TestReport" in message
    assert "type: Extra inputs are not permitted" not in message


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


def test_parse_copilot_artifact_reports_root_object_validation_errors() -> None:
    malformed_plan = {
        "schema_version": 1,
        "summary": "Implement validation",
        "steps": [
            {
                "id": "edit",
                "description": "Add guard",
                "files": ["src/api.py"],
                "validation": ["pytest"],
            }
        ],
        "expected_scope": ["src"],
        "test_strategy": ["pytest"],
        "risks": [],
    }

    with pytest.raises(ValueError) as exc_info:
        parse_copilot_artifact(
            AgentRole.PLANNER,
            stdout=json.dumps(malformed_plan),
        )

    message = str(exc_info.value)
    assert "steps.0.goal: Field required" in message
    assert "steps.0.description: Extra inputs are not permitted" in message
    assert "expected_scope: Input should be a valid dictionary" in message
    assert "summary: Field required" not in message


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
    killed: list[tuple[int, signal.Signals]] = []
    timeout = subprocess.TimeoutExpired(
        cmd=["copilot"],
        timeout=5,
        output=(
            '{"type":"session.usage_checkpoint","data":'
            '{"totalNanoAiu":22456000,"totalPremiumRequests":1}}\n'
            "partial stdout"
        ),
        stderr="partial stderr",
    )

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        return _FakePopen(stdout="", stderr="", timeout=timeout)

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr("software_agent_factory.copilot_runtime.os.killpg", fake_killpg)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=5))

    assert result.success is False
    assert result.failure_reason is not None
    assert "timed out after 5s" in result.failure_reason
    assert result.usage is not None
    assert result.usage.premium_requests == 1.0
    assert result.usage.total_nano_aiu == 22456000
    assert killed == [(43210, signal.SIGTERM)]


def test_timeout_uncooperative_process_escalates_to_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    timeout = subprocess.TimeoutExpired(
        cmd=["copilot"],
        timeout=5,
        output="partial stdout",
        stderr="partial stderr",
    )

    def fake_popen(*args: object, **kwargs: object) -> _FakePopen:
        return _FakePopen(stdout="", stderr="", timeout=timeout, timeout_calls=2)

    def fake_killpg(pid: int, sig: signal.Signals) -> None:
        killed.append((pid, sig))

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr("software_agent_factory.copilot_runtime.os.killpg", fake_killpg)

    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE, timeout_seconds=5))

    assert result.success is False
    assert result.failure_reason is not None
    assert "timed out after 5s" in result.failure_reason
    assert killed == [(43210, signal.SIGTERM), (43210, signal.SIGKILL)]


def test_kill_process_group_handles_process_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from software_agent_factory.copilot_runtime import _kill_process_group

    class _DyingPopen:
        def __init__(self) -> None:
            self.pid = 99999

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "out", "err"

    def fail_killpg(pid: int, sig: signal.Signals) -> None:
        raise ProcessLookupError("No such process")

    monkeypatch.setattr("software_agent_factory.copilot_runtime.os.killpg", fail_killpg)
    stdout, stderr = _kill_process_group(_DyingPopen())  # type: ignore[arg-type]
    assert stdout == "out"
    assert stderr == "err"


def test_iter_json_objects_adversarial_unclosed_braces_linear_speed() -> None:
    from software_agent_factory.copilot_runtime import _iter_json_objects

    text = "{" * 50000

    objects = _iter_json_objects(text)
    assert objects == []


def test_iter_json_objects_adversarial_unclosed_objects_speed() -> None:
    from software_agent_factory.copilot_runtime import _iter_json_objects

    text = '{"key":' * 5000

    objects = _iter_json_objects(text)
    assert objects == []


def test_iter_json_objects_handles_braces_in_strings_and_escapes() -> None:
    from software_agent_factory.copilot_runtime import _iter_json_objects

    text = (
        'prose before {"summary": "has {braces} and \\"escaped\\" quotes", '
        '"changed_files": ["foo.py"], "tests_added": [], "commands_run": []} prose after'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 1
    assert objects[0]["summary"] == 'has {braces} and "escaped" quotes'


def test_iter_json_objects_handles_multiple_valid_objects() -> None:
    from software_agent_factory.copilot_runtime import _iter_json_objects

    text = '{"a": 1}\nsome text\n{"b": 2}\n{"c": {"nested": true}}'
    objects = _iter_json_objects(text)
    assert len(objects) == 3
    assert objects[0] == {"a": 1}
    assert objects[1] == {"b": 2}
    assert objects[2] == {"c": {"nested": True}}


def test_iter_json_objects_ignores_non_string_keys() -> None:
    from software_agent_factory.copilot_runtime import _iter_json_objects

    text = '{123: "invalid json"} {"valid": true}'
    objects = _iter_json_objects(text)
    assert len(objects) == 1
    assert objects[0] == {"valid": True}


def test_correct_change_set_permissions_and_command() -> None:
    runtime = CopilotAgentRuntime()
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
    command = runtime._build_command(request, prompt="correct", cwd=Path("/repo"))

    # No available tools
    available_tools_idx = command.index("--available-tools")
    assert command[available_tools_idx + 1] == ""

    # Denied shell, write, url
    denied = [command[i + 1] for i, arg in enumerate(command) if arg == "--deny-tool"]
    assert "shell" in denied
    assert "write" in denied
    assert "url" in denied
    assert "--allow-url" not in command


def test_correct_change_set_artifact_validation() -> None:
    stdout = json.dumps(
        {
            "schema_version": 1,
            "summary": "Updated summary explaining fix",
            "changed_files": ["app.py"],
            "tests_added": [],
            "commands_run": [],
        }
    )
    artifact = parse_copilot_artifact(
        AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.CORRECT_CHANGE_SET,
        stdout=stdout,
    )
    assert isinstance(artifact, ChangeSet)
    assert artifact.summary == "Updated summary explaining fix"


def test_correct_change_set_requires_implementer_role() -> None:
    with pytest.raises(ValueError, match="ChangeSet correction requires the IMPLEMENTER role"):
        parse_copilot_artifact(
            AgentRole.PLANNER,
            purpose=AgentPurpose.CORRECT_CHANGE_SET,
            stdout='{"summary": "foo"}',
        )


def test_correct_change_set_requires_workspace_path() -> None:
    runtime = CopilotAgentRuntime()
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
        workspace_path=None,
    )

    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
        runtime.run(request)

    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
        runtime._cwd_for(request)

    request_empty = _request(
        AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.CORRECT_CHANGE_SET,
        change_set=change_set,
        workspace_path="",
    )
    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
        runtime.run(request_empty)

    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
        runtime._cwd_for(request_empty)

    request_valid = _request(
        AgentRole.IMPLEMENTER,
        purpose=AgentPurpose.CORRECT_CHANGE_SET,
        change_set=change_set,
        workspace_path="/tmp/explicit-workspace",
    )
    assert runtime._cwd_for(request_valid) == Path("/tmp/explicit-workspace").resolve()


def test_parse_copilot_artifact_recovers_nested_envelope_object() -> None:
    inner = {
        "schema_version": 1,
        "factory_eligible": True,
        "complexity": "L0",
        "risk": "R0",
        "requirements_quality": "clear",
        "needs_research": False,
        "dependencies": [],
        "unknowns": [],
        "confidence": 1.0,
    }
    stdout = json.dumps({"status": "success", "result": inner})
    artifact = parse_copilot_artifact(AgentRole.TRIAGE, stdout=stdout)
    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L0"
    assert artifact.risk == "R0"


def test_parse_copilot_artifact_recovers_deeply_nested_envelope_object() -> None:
    inner = {
        "schema_version": 1,
        "factory_eligible": True,
        "complexity": "L1",
        "risk": "R1",
        "requirements_quality": "clear",
        "needs_research": False,
        "dependencies": [],
        "unknowns": [],
        "confidence": 0.9,
    }
    stdout = json.dumps({"envelope": {"response": {"output": {"payload": inner}}}})
    artifact = parse_copilot_artifact(AgentRole.TRIAGE, stdout=stdout)
    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L1"
    assert artifact.confidence == 0.9


def test_parse_copilot_artifact_recovers_nested_envelope_in_list() -> None:
    inner = {
        "schema_version": 1,
        "factory_eligible": True,
        "complexity": "L2",
        "risk": "R0",
        "requirements_quality": "clear",
        "needs_research": False,
        "dependencies": [],
        "unknowns": [],
        "confidence": 0.85,
    }
    stdout = json.dumps({"candidates": [{"content": inner}]})
    artifact = parse_copilot_artifact(AgentRole.TRIAGE, stdout=stdout)
    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L2"


def test_parse_copilot_artifact_malformed_prose_followed_by_valid_object() -> None:
    valid_json = json.dumps(
        {
            "schema_version": 1,
            "factory_eligible": True,
            "complexity": "L0",
            "risk": "R0",
            "requirements_quality": "clear",
            "needs_research": False,
            "dependencies": [],
            "unknowns": [],
            "confidence": 1.0,
        }
    )
    stdout = (
        "I looked at the code: function parse() { return { broken: json; }; }\n"
        'Here is an unclosed quote: { "unclosed: text that does not end\n'
        'And broken syntax: { "foo": [1, 2, } }\n'
        "Finally, here is the typed artifact:\n"
        f"{valid_json}\n"
    )
    artifact = parse_copilot_artifact(AgentRole.TRIAGE, stdout=stdout)
    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L0"


def test_iter_json_objects_malformed_prose_followed_by_valid_objects() -> None:
    text = (
        'Some text with { code: block } and { "bad": syntax, [1, } '
        'and unclosed { "string: unclosed '
        'and then valid: {"first": 123} and {"second": {"nested": 456}}'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 2
    assert objects[0] == {"first": 123}
    assert objects[1] == {"second": {"nested": 456}}


def test_iter_json_objects_deeply_nested_malformed_bounded_work() -> None:
    depth = 1000
    text = '{"a": ' * depth + "broken_payload" + "}" * depth
    stats = ScanStats()
    objects = _iter_json_objects(text, scan_stats=stats)

    assert objects == []
    # Verify deterministic near-linear work: characters scanned is bounded by 2 * len(text)
    assert stats.chars_scanned <= 2 * len(text)
    # Consecutive nested failures bound ensures only a small number of candidate decodes occur
    assert stats.candidates_tested <= 10


def test_iter_json_objects_unclosed_nested_braces_bounded_work() -> None:
    depth = 1000
    text = '{"a": ' * depth + "}"
    stats = ScanStats()
    objects = _iter_json_objects(text, scan_stats=stats)

    assert objects == []
    # Unclosed braces are tracked statefully so redundant scans are skipped in O(1)
    assert stats.chars_scanned <= 2 * len(text)
    assert stats.candidates_tested <= 2


def test_iter_json_objects_enforces_max_scan_work_limit() -> None:
    depth = 500
    text = '{"a": ' * depth + "}" * depth
    stats = ScanStats()
    limit = 250
    objects = _iter_json_objects(text, scan_stats=stats, max_scan_work=limit)

    assert objects == []
    assert stats.chars_scanned <= limit + 10


def test_iter_json_objects_skips_non_json_braces_without_suffix_scans() -> None:
    text = "{x" * 1000 + "}"
    stats = ScanStats()

    objects = _iter_json_objects(text, scan_stats=stats, max_scan_work=1)

    assert objects == []
    assert stats.chars_scanned == 0
    assert stats.candidates_tested == 0


def test_parse_copilot_artifact_recovers_valid_after_deeply_nested_prefix() -> None:
    valid_json = json.dumps(
        {
            "schema_version": 1,
            "factory_eligible": True,
            "complexity": "L0",
            "risk": "R0",
            "requirements_quality": "clear",
            "needs_research": False,
            "dependencies": [],
            "unknowns": [],
            "confidence": 1.0,
        }
    )
    malformed_prefix = '{"a": ' * 500 + "unparsable" + "}" * 500
    stdout = f"Prefix noise: {malformed_prefix}\nResult:\n{valid_json}\n"

    artifact = parse_copilot_artifact(AgentRole.TRIAGE, stdout=stdout)
    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L0"


def test_iter_json_objects_nested_envelope_with_broken_outer_recovers_inner() -> None:
    text = '{ broken: syntax, "result": {"valid": 123} }'
    objects = _iter_json_objects(text)
    assert objects == [{"valid": 123}]


def test_iter_json_objects_preserves_multiple_objects_and_escapes() -> None:
    text = (
        '{"first": "escaped \\" { and } braces", "val": 1} '
        '{"second": {"nested": "str \\\\ with \\" quote"}}'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 2
    assert objects[0] == {"first": 'escaped " { and } braces', "val": 1}
    assert objects[1] == {"second": {"nested": 'str \\ with " quote'}}


def test_compatibility_non_streaming_fallback_and_no_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_command: list[str] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakePopen:
        captured_command.extend(command)
        return _FakePopen(
            stdout=(
                '{"schema_version":1,"factory_eligible":true,"complexity":"L0",'
                '"risk":"R0","requirements_quality":"clear","needs_research":false,'
                '"dependencies":[],"unknowns":[],"confidence":1.0}'
            )
        )

    monkeypatch.setattr("software_agent_factory.copilot_runtime.subprocess.Popen", fake_popen)
    runtime = CopilotAgentRuntime()
    result = runtime.run(_request(AgentRole.TRIAGE))

    assert result.success is True
    assert result.triage_result is not None
    assert result.triage_result.complexity == "L0"
    # Never persistent sessions: --resume must not appear
    assert "--resume" not in captured_command
    assert "--stream" in captured_command
    assert captured_command[captured_command.index("--stream") + 1] == "off"
    # Telemetry preserved
    assert result.performance is not None
    assert result.performance.prompt_chars is not None
    assert result.performance.prompt_chars > 0
    assert result.performance.response_chars is not None
    assert result.performance.response_chars > 0
    assert result.performance.process_boot_ms is not None


def _json_string(value: str) -> str:
    import json

    return json.dumps(value)
