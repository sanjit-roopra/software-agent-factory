"""Per-call transcripts: stored redacted and capped, read back as tool calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from factory_testing import REPLY_POLICY, build_config, work_item

from software_agent_factory import transcripts
from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.dashboard.sanitize import sanitize_invocation
from software_agent_factory.models import (
    AgentRole,
    ModelUsage,
    RuntimeName,
    UsageMetrics,
    WorkflowState,
)
from software_agent_factory.observability import build_run_detail
from software_agent_factory.store import FileRunStore
from software_agent_factory.transcripts import (
    TRANSCRIPT_CUT_MARKER,
    read_call_tool_calls,
    tool_calls,
    write_call_transcript,
)
from software_agent_factory.workflow import WorkflowController

SECRET = "ghp_" + "a" * 36


def _claude_tool_event(name: str, tool_input: Any) -> str:
    content = [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "name": name, "input": tool_input},
    ]
    return json.dumps({"type": "assistant", "message": {"content": content}})


def _copilot_tool_event(name: str, arguments: Any) -> str:
    return json.dumps(
        {"type": "tool.execution_start", "data": {"toolName": name, "arguments": arguments}}
    )


def test_write_redacts_secrets_and_returns_the_run_relative_path(tmp_path: Path) -> None:
    name = write_call_transcript(tmp_path, 3, f'{{"token": "{SECRET}"}}\n')

    assert name == "calls/0003.jsonl"
    stored = (tmp_path / name).read_text(encoding="utf-8")
    assert SECRET not in stored
    assert "[REDACTED]" in stored


def test_write_cuts_a_long_transcript_at_a_line_and_marks_the_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_CHARS", 10)

    write_call_transcript(tmp_path, 1, "aaaa\nbbbb\ncccc\n")

    assert (tmp_path / "calls/0001.jsonl").read_text(
        encoding="utf-8"
    ) == "aaaa\nbbbb\n" + TRANSCRIPT_CUT_MARKER


def test_write_cuts_mid_line_when_the_first_line_is_too_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "TRANSCRIPT_MAX_CHARS", 4)

    write_call_transcript(tmp_path, 1, "abcdefgh")

    assert (tmp_path / "calls/0001.jsonl").read_text(
        encoding="utf-8"
    ) == "abcd" + TRANSCRIPT_CUT_MARKER


def test_write_stores_the_tool_calls_beside_the_transcript(tmp_path: Path) -> None:
    text = _claude_tool_event("Bash", {"command": f"echo {SECRET}"}) + "\n"

    write_call_transcript(tmp_path, 2, text)

    assert read_call_tool_calls(tmp_path, 2) == [{"name": "Bash", "preview": "echo [REDACTED]"}]


@pytest.mark.parametrize(
    "content",
    ["not json", '{"name": "Bash"}', '[{"name": 3}, {"name": "Read", "preview": 5}, "x"]'],
)
def test_read_tool_calls_skips_a_bad_file(tmp_path: Path, content: str) -> None:
    (tmp_path / "calls").mkdir()
    (tmp_path / "calls" / "0001.tools.json").write_text(content, encoding="utf-8")

    assert read_call_tool_calls(tmp_path, 1) == []
    assert read_call_tool_calls(tmp_path, 9) == []


def test_tool_calls_read_claude_and_copilot_events_and_skip_other_lines() -> None:
    text = "\n".join(
        [
            "not json",
            json.dumps(["a list"]),
            json.dumps({"type": "assistant", "message": "no content"}),
            json.dumps({"type": "tool.execution_start", "data": {"toolName": 7}}),
            json.dumps({"type": "result"}),
            _claude_tool_event("Bash", {"command": "uv run   pytest\n -q"}),
            _claude_tool_event("Read", {"file_path": "src/app.py"}),
            _claude_tool_event("TodoWrite", {"todos": []}),
            _copilot_tool_event("shell", "ls -la"),
            _copilot_tool_event("view", None),
        ]
    )

    assert tool_calls(text) == [
        {"name": "Bash", "preview": "uv run pytest -q"},
        {"name": "Read", "preview": "src/app.py"},
        {"name": "TodoWrite", "preview": ""},
        {"name": "shell", "preview": "ls -la"},
        {"name": "view", "preview": ""},
    ]


def test_tool_calls_stop_at_the_limit_and_cut_long_previews() -> None:
    text = "\n".join(_claude_tool_event("Bash", {"command": "x" * 500}) for _ in range(5))

    calls = tool_calls(text, limit=2)

    assert len(calls) == 2
    assert len(calls[0]["preview"]) == transcripts.PREVIEW_CHARS
    assert calls[0]["preview"].endswith("…")


def _raw_call(**overrides: Any) -> dict[str, Any]:
    call: dict[str, Any] = {
        "invocation_number": 1,
        "role": "IMPLEMENTER",
        "model": "m",
        "reasoning": "high",
        "success": True,
        "started_at": "2024-01-01T00:00:00+00:00",
        "completed_at": "2024-01-01T00:00:01+00:00",
    }
    return {**call, **overrides}


def test_sanitized_call_keeps_a_known_runtime_and_a_redacted_fallback_reason() -> None:
    call = sanitize_invocation(
        _raw_call(runtime="pi", fallback_reason=f"claude limit {SECRET}"), "run-1"
    )

    assert call["runtime"] == "pi"
    assert call["fallback_reason"] is not None
    assert SECRET not in call["fallback_reason"]


def test_sanitized_call_drops_an_unknown_runtime_and_an_empty_reason() -> None:
    call = sanitize_invocation(_raw_call(runtime="<script>", fallback_reason=""))

    assert call["runtime"] is None
    assert call["fallback_reason"] is None
    assert call["tool_calls"] == []


def test_sanitized_tool_calls_are_redacted_bounded_and_need_a_name() -> None:
    raw = [
        {"name": "Bash", "preview": f"echo {SECRET}"},
        {"name": "", "preview": "no name"},
        "not a dict",
        {"name": "Read", "preview": 7},
        {"name": "Bash", "preview": "y" * 500},
    ]

    calls = sanitize_invocation(_raw_call(tool_calls=raw))["tool_calls"]

    assert [call["name"] for call in calls] == ["Bash", "Read", "Bash"]
    assert SECRET not in calls[0]["preview"]
    assert calls[1]["preview"] == ""
    assert len(calls[2]["preview"]) == 160


def test_sanitized_tool_calls_make_absolute_paths_relative() -> None:
    raw = [
        {"name": "Read", "preview": "/Users/me/repo/.claude/worktrees/run-1/src/x.py"},
        {"name": "Bash", "preview": "cat /etc/hosts ~/notes/todo.md && ls src/a"},
        {"name": "Fetch", "preview": "https://example.com/a/b"},
    ]

    calls = sanitize_invocation(_raw_call(tool_calls=raw))["tool_calls"]

    assert [call["preview"] for call in calls] == [
        "src/x.py",
        "cat hosts todo.md && ls src/a",
        "https://example.com/a/b",
    ]


def test_sanitized_tool_calls_ignore_a_value_that_is_not_a_list() -> None:
    assert sanitize_invocation(_raw_call(tool_calls={"name": "Bash"}))["tool_calls"] == []


class _TranscriptRuntime:
    """The fake runtime, with a tool-call transcript on every result."""

    def __init__(self) -> None:
        self._fake = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        result = self._fake.run(request)
        transcript = _claude_tool_event("Bash", {"command": f"echo {SECRET}"}) + "\n"
        usage = UsageMetrics(model_usage=(ModelUsage(model="m", runtime=RuntimeName.PI),))
        return result.model_copy(update={"transcript": transcript, "usage": usage})


def test_a_run_stores_each_call_transcript_and_the_detail_lists_its_tool_calls(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    store = FileRunStore(factory_data_dir)
    config = build_config(factory_data_dir)

    run = WorkflowController(config, store, _TranscriptRuntime()).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    first = run.invocation_records[0]
    assert first.transcript_path == "calls/0001.jsonl"
    stored = (store.runs_dir / run.id / first.transcript_path).read_text(encoding="utf-8")
    assert SECRET not in stored
    detail = build_run_detail(store, run.id, reply_policy=REPLY_POLICY)
    assert detail is not None
    call = detail.invocations[0]
    assert call.runtime == "pi"
    assert call.tool_calls == [{"name": "Bash", "preview": "echo [REDACTED]"}]


def test_a_transcript_write_failure_never_fails_the_run(
    factory_source_repo: Path, factory_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr("software_agent_factory.workflow.write_call_transcript", fail)
    store = FileRunStore(factory_data_dir)

    run = WorkflowController(build_config(factory_data_dir), store, _TranscriptRuntime()).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    assert all(record.transcript_path is None for record in run.invocation_records)


def test_a_call_without_a_transcript_lists_no_tool_calls(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    store = FileRunStore(factory_data_dir)
    config = build_config(factory_data_dir)

    run = WorkflowController(config, store, FakeAgentRuntime()).run(
        work_item(), factory_source_repo
    )

    detail = build_run_detail(store, run.id, reply_policy=REPLY_POLICY)
    assert detail is not None
    assert all(call.tool_calls == [] for call in detail.invocations)
    assert all(call.runtime is None for call in detail.invocations)
    assert AgentRole.TRIAGE in {call.role for call in detail.invocations}
