"""The live tool-call tail of the running call (dashboard X6)."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dashboard_js import JsCall, run_functions
from factory_testing import FakePiProcess, build_config
from prompt_fixtures import make_request
from test_stall_cancel import _work_item

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import PiConfig
from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.sanitize import sanitize_active_invocation
from software_agent_factory.models import (
    ActiveInvocation,
    AgentRole,
    FactoryRun,
    WorkflowState,
)
from software_agent_factory.observability import build_active_invocation_summary
from software_agent_factory.pi_rpc import PiRpcClient
from software_agent_factory.pi_runtime import PiAgentRuntime, _report_tool_start
from software_agent_factory.run_watch import LIVE_TOOL_CALLS, note_tool_call
from software_agent_factory.store import FileRunStore
from software_agent_factory.transcripts import live_tool_calls, report_tool_call
from software_agent_factory.workflow import WorkflowController

_STARTED = datetime(2026, 1, 1, tzinfo=UTC)


def _active(**overrides: Any) -> ActiveInvocation:
    fields: dict[str, Any] = {
        "invocation_number": 1,
        "role": AgentRole.IMPLEMENTER,
        "model": "m",
        "reasoning": "medium",
        "started_at": _STARTED,
    }
    return ActiveInvocation(**{**fields, **overrides})


# -- report_tool_call ---------------------------------------------------------------


def test_a_reported_tool_call_reaches_the_sink_as_a_redacted_one_line_preview() -> None:
    seen: list[dict[str, str]] = []

    with live_tool_calls(seen.append):
        report_tool_call("bash", {"command": "echo ghp_" + "a" * 36 + "\n  done"})
    report_tool_call("bash", {"command": "after the context"})

    assert len(seen) == 1
    assert seen[0]["name"] == "bash"
    assert "ghp_" not in seen[0]["preview"]
    assert "\n" not in seen[0]["preview"]


def test_a_tool_call_without_a_listener_is_dropped() -> None:
    report_tool_call("read", {"path": "a.py"})


# -- pi: the event hook and the tool start event ------------------------------------


def test_the_rpc_client_shows_every_event_record_to_on_event() -> None:
    process = FakePiProcess()
    seen: list[dict[str, Any]] = []
    client = PiRpcClient(process, on_event=seen.append)
    process.write_records(
        {"type": "tool_execution_start", "toolName": "read"},
        {"type": "response", "id": "c1", "success": True},
        {"type": "turn_start"},
        {"type": "agent_settled"},
    )

    client.request({"type": "prompt", "message": "hi"}, deadline=time.monotonic() + 5)
    client.wait_for_settled(deadline=time.monotonic() + 5)

    assert [record["type"] for record in seen] == [
        "tool_execution_start",
        "turn_start",
        "agent_settled",
    ]


def test_only_a_tool_start_with_a_name_is_reported_and_its_args_are_redacted() -> None:
    seen: list[dict[str, str]] = []

    def redact(text: str) -> str:
        return text.replace("hunter2", "[REDACTED]")

    with live_tool_calls(seen.append):
        _report_tool_start({"type": "tool_execution_end", "toolName": "bash"}, redact)
        _report_tool_start({"type": "tool_execution_start", "toolName": 7}, redact)
        _report_tool_start(
            {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "x hunter2"}},
            redact,
        )
        _report_tool_start(
            {"type": "tool_execution_start", "toolName": "t", "args": "hunter2"}, redact
        )
        _report_tool_start({"type": "tool_execution_start", "toolName": "u", "args": 3}, redact)

    assert seen == [
        {"name": "bash", "preview": "x [REDACTED]"},
        {"name": "t", "preview": "[REDACTED]"},
        {"name": "u", "preview": ""},
    ]


def test_a_pi_call_reports_its_tool_calls_while_it_runs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "tool_execution_start", "toolName": "read", "args": {"path": "src/a.py"}},
        {"type": "agent_settled"},
        {"type": "response", "id": "c2", "success": True, "data": {"messages": []}},
        {
            "type": "response",
            "id": "c3",
            "success": True,
            "data": {"text": json.dumps({"summary": "Done."})},
        },
    )
    process.exit(0)
    runtime = PiAgentRuntime(
        PiConfig(), data_dir=tmp_path, process_factory=lambda command, cwd, env: process
    )
    seen: list[dict[str, str]] = []
    outputs: list[None] = []

    with live_tool_calls(seen.append, lambda: outputs.append(None)):
        result = runtime.run(make_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace)))

    assert result.success is True
    assert seen == [{"name": "read", "preview": "src/a.py"}]
    assert len(outputs) >= 2  # each pi event counts as output for the silence check


# -- the workflow keeps the latest tool calls on the active call --------------------


def test_the_active_call_keeps_the_latest_tool_calls_and_counts_them_all(tmp_path: Path) -> None:

    run = FactoryRun(id="run-tail", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    run.active_invocation = _active()
    changed = threading.Event()

    for number in range(LIVE_TOOL_CALLS + 5):
        note_tool_call(run, {"name": f"t{number}", "preview": ""}, changed)

    assert changed.is_set()
    assert run.active_invocation is not None
    assert run.active_invocation.tool_call_count == LIVE_TOOL_CALLS + 5
    names = [call["name"] for call in run.active_invocation.recent_tool_calls]
    assert names == [f"t{number}" for number in range(5, LIVE_TOOL_CALLS + 5)]


def test_a_tool_call_after_the_call_ended_is_ignored() -> None:

    run = FactoryRun(id="run-done", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    changed = threading.Event()

    note_tool_call(run, {"name": "late", "preview": ""}, changed)

    assert run.active_invocation is None
    assert not changed.is_set()


class ToolReportingRuntime:
    """Reports tool calls in the Implementer call, then waits until the run file shows them."""

    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self.stored: ActiveInvocation | None = None
        self._delegate = FakeAgentRuntime()

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER and self.stored is None:
            report_tool_call("bash", {"command": "pytest -q"})
            report_tool_call("read", {"path": "src/app.py"})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.stored is None:
                active = self.store.list_runs()[0].active_invocation
                if active is not None and active.tool_call_count == 2:
                    self.stored = active
                time.sleep(0.01)
        return self._delegate.run(request)


def test_the_run_file_shows_the_tool_calls_before_the_call_ends(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    store = FileRunStore(factory_data_dir)
    runtime = ToolReportingRuntime(store)
    controller = WorkflowController(build_config(factory_data_dir), store, runtime)
    controller.heartbeat_interval_seconds = 0.01

    run = controller.run(_work_item(factory_source_repo), factory_source_repo, run_id="run-live")

    assert run.state is WorkflowState.PR_READY
    assert runtime.stored is not None
    assert runtime.stored.recent_tool_calls == [
        {"name": "bash", "preview": "pytest -q"},
        {"name": "read", "preview": "src/app.py"},
    ]


# -- what the dashboard sends ----------------------------------------------------


def test_the_summary_and_the_sanitized_call_carry_the_tail() -> None:
    run = FactoryRun(id="run-sum", work_item_id="w", state=WorkflowState.IMPLEMENTING)
    tail = [{"name": "bash", "preview": "/Users/me/repo/x.sh ghp_" + "b" * 36}]
    run.active_invocation = _active(recent_tool_calls=tail, tool_call_count=9)

    summary = build_active_invocation_summary(run, now=_STARTED)
    assert summary is not None
    assert summary.tool_calls == tail
    assert summary.tool_call_count == 9

    call = sanitize_active_invocation(summary.model_dump(mode="json"))
    assert call["tool_call_count"] == 9
    assert call["tool_calls"][0]["name"] == "bash"
    assert "ghp_" not in call["tool_calls"][0]["preview"]
    assert "/Users/me" not in call["tool_calls"][0]["preview"]


def test_a_bad_count_falls_back_to_the_number_of_tool_calls() -> None:
    call = sanitize_active_invocation(
        {"tool_calls": [{"name": "a", "preview": ""}], "tool_call_count": "lots"}
    )

    assert call["tool_call_count"] == 1


def test_the_active_call_line_names_the_last_tool() -> None:
    active = {
        "role": "IMPLEMENTER",
        "model": "m",
        "status": "running",
        "started_at": "t",
        "tool_calls": [{"name": "read", "preview": "a.py"}, {"name": "bash", "preview": ""}],
    }
    results = run_functions(
        dashboard_assets.APP_JS,
        (
            JsCall("activeInvocationText", ({**active, "tool_calls": []},)),
            JsCall("activeInvocationText", (active,)),
        ),
    )

    assert [result.value for result in results] == [
        "IMPLEMENTER (m) — running since t",
        "IMPLEMENTER (m) — running since t — last tool: bash",
    ]


def test_the_status_line_shows_the_tool_count_and_the_last_tool() -> None:
    tools = [{"name": "read", "preview": "a.py"}, {"name": "bash", "preview": "ls"}]
    results = run_functions(
        dashboard_assets.APP_JS,
        (
            JsCall("liveToolPart", ({"tool_calls": []},)),
            JsCall("liveToolPart", ({"tool_calls": tools[:1]},)),
            JsCall("liveToolPart", ({"tool_calls": tools, "tool_call_count": 27},)),
        ),
    )

    assert [result.value for result in results] == [
        None,
        {"text": "1 tool · last: read  a.py", "title": "read  a.py", "className": "live-tool"},
        {
            "text": "27 tools · last: bash  ls",
            "title": "read  a.py\nbash  ls",
            "className": "live-tool",
        },
    ]
