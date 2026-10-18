"""Keep each agent call's event stream beside its run, and read tool calls back from it.

A runtime hands back the raw lines its CLI printed (``AgentResult.transcript``).
The workflow writes them to ``runs/<id>/calls/<n>.jsonl`` after
:func:`~.redaction.redact_secrets`, cut to :data:`TRANSCRIPT_MAX_CHARS`. At the
same time it writes the tools the call used, one short line each, to a small
``calls/<n>.tools.json`` beside it. The dashboard reads only that small file,
so a poll never parses a whole transcript.

A runtime that streams its events (pi, dashboard X6) also reports each tool call as it
starts, through :func:`report_tool_call`. The workflow listens with :func:`live_tool_calls`
and keeps the latest ones on the active call, so the dashboard can show a live tail.
pi prints no stream of its own on stdout, so its runtime keeps the RPC events in an
:class:`EventLog` and returns those lines as the transcript.

``factory project`` stores its decomposition calls the same way, under
``projects/<id>/calls/<n>.jsonl``, through :func:`store_call_transcript`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .atomic_write import write_text_atomic
from .redaction import redact_secrets

logger = logging.getLogger(__name__)

CALLS_DIRNAME = "calls"
TRANSCRIPT_MAX_CHARS = 2_000_000
TRANSCRIPT_CUT_MARKER = '{"type":"factory.transcript_cut"}\n'
MAX_TOOL_CALLS = 40
PREVIEW_CHARS = 120

# Input keys that best say what a tool call did, in order of preference.
_PREVIEW_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")


#: Receives one ``{"name", "preview"}`` dict per tool call a running call starts.
ToolCallSink = Callable[[dict[str, str]], None]

_tool_call_sink: ContextVar[ToolCallSink | None] = ContextVar("tool_call_sink", default=None)


@contextmanager
def live_tool_calls(sink: ToolCallSink) -> Iterator[None]:
    """Send each tool call reported in this context to ``sink``."""
    token = _tool_call_sink.set(sink)
    try:
        yield
    finally:
        _tool_call_sink.reset(token)


def report_tool_call(name: str, arguments: Any) -> None:
    """Tell the listener, if any, that a tool call started. ``arguments`` gives the preview."""
    sink = _tool_call_sink.get()
    if sink is not None:
        sink({"name": _one_line(redact_secrets(name)), "preview": _preview(arguments)})


def transcript_name(invocation_number: int) -> str:
    """The run-relative path of one call's transcript."""
    return f"{CALLS_DIRNAME}/{invocation_number:04d}.jsonl"


def tool_calls_name(invocation_number: int) -> str:
    """The run-relative path of the tool calls read from one call's transcript."""
    return f"{CALLS_DIRNAME}/{invocation_number:04d}.tools.json"


def write_call_transcript(run_dir: Path, invocation_number: int, text: str) -> str:
    """Write one call's redacted, capped transcript; return its run-relative path."""
    redacted = redact_secrets(text)
    if len(redacted) > TRANSCRIPT_MAX_CHARS:
        cut = redacted.rfind("\n", 0, TRANSCRIPT_MAX_CHARS)
        redacted = redacted[: cut + 1 if cut >= 0 else TRANSCRIPT_MAX_CHARS] + TRANSCRIPT_CUT_MARKER
    name = transcript_name(invocation_number)
    write_text_atomic(run_dir / name, redacted, mode=0o600)
    tools = json.dumps(tool_calls(redacted), ensure_ascii=False)
    write_text_atomic(run_dir / tool_calls_name(invocation_number), tools, mode=0o600)
    return name


def store_call_transcript(directory: Path, invocation_number: int, text: str | None) -> str | None:
    """Write one call's transcript when there is one; a write failure is only logged."""
    if not text:
        return None
    try:
        return write_call_transcript(directory, invocation_number, text)
    except OSError:
        logger.warning("could not store the transcript of call %d", invocation_number)
        return None


# pi repeats the whole partial message in each streaming update; ``message_end`` keeps it once.
_SKIPPED_EVENTS = frozenset({"message_update"})


class EventLog:
    """Keep a streaming runtime's event records as JSON lines, cut near the transcript cap."""

    def __init__(self, redact: Callable[[str], str] = str) -> None:
        self._redact = redact
        self._lines: list[str] = []
        self._chars = 0

    def add(self, record: dict[str, Any]) -> None:
        if record.get("type") in _SKIPPED_EVENTS or self._chars > TRANSCRIPT_MAX_CHARS:
            return
        line = self._redact(json.dumps(record, ensure_ascii=False))
        self._lines.append(line)
        self._chars += len(line) + 1

    def text(self) -> str | None:
        """The kept lines, or ``None`` when no event arrived."""
        return "".join(f"{line}\n" for line in self._lines) or None


def read_call_tool_calls(run_dir: Path, invocation_number: int) -> list[dict[str, str]]:
    """The tool calls stored for one call; none when the file is missing or unreadable."""
    try:
        raw = json.loads((run_dir / tool_calls_name(invocation_number)).read_text("utf-8"))
    except (OSError, UnicodeDecodeError, JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [
        {"name": item["name"], "preview": item.get("preview", "")}
        for item in raw[:MAX_TOOL_CALLS]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("preview", ""), str)
    ]


def _one_line(value: str) -> str:
    line = " ".join(value.split())
    return line if len(line) <= PREVIEW_CHARS else line[: PREVIEW_CHARS - 1] + "…"


def _preview(arguments: Any) -> str:
    if isinstance(arguments, str):
        return _one_line(redact_secrets(arguments))
    if not isinstance(arguments, dict):
        return ""
    for key in _PREVIEW_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(redact_secrets(value))
    return ""


def _claude_tool_uses(event: dict[str, Any]) -> list[tuple[str, Any]]:
    """Tool uses in a Claude Code ``assistant`` event."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        (block["name"], block.get("input"))
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_use"
        and isinstance(block.get("name"), str)
    ]


def _copilot_tool_uses(event: dict[str, Any]) -> list[tuple[str, Any]]:
    """The tool a Copilot ``tool.execution_start`` event starts."""
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("toolName"), str):
        return []
    return [(data["toolName"], data.get("arguments"))]


def _pi_tool_uses(event: dict[str, Any]) -> list[tuple[str, Any]]:
    """The tool a pi ``tool_execution_start`` event starts."""
    name = event.get("toolName")
    return [(name, event.get("args"))] if isinstance(name, str) else []


def _tool_uses(event: Any) -> list[tuple[str, Any]]:
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    if kind == "assistant":
        return _claude_tool_uses(event)
    if kind == "tool.execution_start":
        return _copilot_tool_uses(event)
    if kind == "tool_execution_start":
        return _pi_tool_uses(event)
    return []


def tool_calls(text: str, limit: int = MAX_TOOL_CALLS) -> list[dict[str, str]]:
    """The first ``limit`` tool calls in a transcript: name and a one-line preview."""
    calls: list[dict[str, str]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        for name, arguments in _tool_uses(event):
            calls.append({"name": _one_line(name), "preview": _preview(arguments)})
            if len(calls) >= limit:
                return calls
    return calls
