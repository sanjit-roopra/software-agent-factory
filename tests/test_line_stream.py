"""Claude Code and Copilot stream their stdout lines to the live tail (ADR-052)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_claude_code_runtime import TRIAGE_JSON, _request, _result_event

from software_agent_factory import line_stream
from software_agent_factory.claude_code_runtime import ClaudeCodeAgentRuntime
from software_agent_factory.line_stream import stream_process
from software_agent_factory.models import ActiveInvocation, AgentRole, FactoryRun, WorkflowState
from software_agent_factory.run_watch import live_activity
from software_agent_factory.transcripts import live_tool_calls, report_event_line


def _tool_use(name: str, **arguments: str) -> str:
    content = [{"type": "tool_use", "name": name, "input": arguments}]
    return json.dumps({"type": "assistant", "message": {"content": content}})


def _copilot_start(name: str, path: str) -> str:
    return json.dumps(
        {"type": "tool.execution_start", "data": {"toolName": name, "arguments": {"path": path}}}
    )


def _child(body: str) -> subprocess.Popen[str]:
    """A real child process in its own group, running ``body`` as Python."""
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(body)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _printer(lines: list[str], *, pause: float = 0.0, tail: str = "") -> str:
    return f"""
        import sys, time
        for line in {lines!r}:
            print(line, flush=True)
            time.sleep({pause})
        sys.stdout.write({tail!r})
        print("to stderr", file=sys.stderr)
    """


def test_each_tool_call_is_reported_in_order_while_the_process_runs() -> None:
    lines = [_tool_use("Read", file_path="src/a.py"), _copilot_start("bash", "ls")]
    process = _child(_printer(lines, pause=0.3))
    seen: list[tuple[str, int | None]] = []

    with live_tool_calls(lambda call: seen.append((call["name"], process.poll()))):
        output = stream_process(process, timeout_seconds=30)

    assert seen == [("Read", None), ("bash", None)]
    assert output.timed_out is False
    assert process.returncode == 0


def test_the_transcript_is_the_whole_stdout_exactly() -> None:
    lines = [_tool_use("Grep", pattern="x"), "not json", ""]
    process = _child(_printer(lines, tail='{"type":"result"}'))

    output = stream_process(process, timeout_seconds=30)

    assert output.stdout == "\n".join(lines) + '\n{"type":"result"}'
    assert output.stderr == "to stderr\n"


def test_the_prompt_reaches_stdin_and_stdin_is_closed() -> None:
    prompt = "line one\n" + "x" * 200_000
    process = _child("import sys; print(len(sys.stdin.read()))")

    output = stream_process(process, timeout_seconds=30, prompt=prompt)

    assert output.stdout == f"{len(prompt)}\n"


def test_the_deadline_stops_the_process_group_and_keeps_partial_output() -> None:
    process = _child('import time; print("started", flush=True); time.sleep(60)')
    started = time.monotonic()

    output = stream_process(process, timeout_seconds=1)

    assert output.timed_out is True
    assert output.stdout == "started\n"
    assert time.monotonic() - started < 10
    assert process.returncode is not None
    assert process.returncode < 0


def test_a_process_that_ignores_sigterm_is_killed() -> None:
    process = _child(
        """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("ready", flush=True)
        time.sleep(60)
        """
    )

    output = stream_process(process, timeout_seconds=1, grace_seconds=0.2)

    assert output.timed_out is True
    assert process.returncode == -9


def test_an_error_while_reading_stops_the_process() -> None:
    line = _tool_use("Read", file_path="a")
    process = _child(f"import time; print({line!r}, flush=True); time.sleep(60)")

    def fail(_call: dict[str, str]) -> None:
        raise RuntimeError("listener broke")

    with live_tool_calls(fail), pytest.raises(RuntimeError, match="listener broke"):
        stream_process(process, timeout_seconds=30)

    assert process.returncode is not None


def test_each_line_stamps_the_last_output_time_on_the_active_call() -> None:
    run = FactoryRun(id="run-out", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    run.active_invocation = ActiveInvocation(
        invocation_number=1,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="r",
        started_at=started,
    )
    process = _child(_printer(["plain text", _tool_use("Edit", file_path="b.py")]))

    with live_activity(run) as changed:
        stream_process(process, timeout_seconds=30)

    active = run.active_invocation
    assert active is not None
    assert active.last_output_at is not None
    assert active.last_output_at > started
    assert active.recent_tool_calls == [{"name": "Edit", "preview": "b.py"}]
    assert changed.is_set()


def test_a_claude_code_call_reports_its_tools_before_it_ends(tmp_path: Path) -> None:
    """A stand-in ``claude`` reads the prompt from stdin and streams real event shapes."""
    events = [
        _tool_use("Read", file_path="src/app.py"),
        _tool_use("Bash", command="pytest -q"),
        json.dumps(_result_event(TRIAGE_JSON)),
    ]
    script = tmp_path / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "prompt = sys.stdin.read()\n"
        "assert 'Reject blanks' in prompt\n"
        f"for line in {events!r}:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    script.chmod(0o755)
    seen: list[dict[str, str]] = []

    with live_tool_calls(seen.append):
        result = ClaudeCodeAgentRuntime(executable=str(script)).run(_request(AgentRole.TRIAGE))

    assert result.success is True, result.failure_reason
    assert seen == [
        {"name": "Read", "preview": "src/app.py"},
        {"name": "Bash", "preview": "pytest -q"},
    ]
    assert result.transcript == "".join(f"{line}\n" for line in events)


def test_a_line_past_the_integer_digit_limit_is_not_a_tool_call() -> None:
    seen: list[dict[str, str]] = []

    with live_tool_calls(seen.append):
        report_event_line('{"n": ' + "1" * 5000 + "}\n")

    assert seen == []


def test_a_group_left_with_only_a_zombie_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS answers EPERM when a group holds only a zombie; the stop must not raise."""
    process = _child("pass")
    time.sleep(0.5)

    def eperm(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(line_stream.os, "killpg", eperm)

    line_stream._Stopper(process, 0.1).expire()
    process.wait(timeout=5)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()
