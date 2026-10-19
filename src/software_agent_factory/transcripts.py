"""Keep each agent call's event stream beside its run, and read tool calls back from it.

A runtime hands back the raw lines its CLI printed (``AgentResult.transcript``).
The workflow writes them to ``runs/<id>/calls/<n>.jsonl`` after
:func:`~.redaction.redact_secrets`, cut to :data:`TRANSCRIPT_MAX_CHARS`. At the
same time it writes the tools the call used, one short line each, to a small
``calls/<n>.tools.json`` beside it. A dashboard poll reads only that small file.
The whole transcript is read only when a person opens it (:func:`read_call_transcript`).

Every runtime also reports each tool call as it starts, through :func:`report_tool_call`
(dashboard X6). pi reports from its RPC events; Claude Code and Copilot report from each
stdout line as it arrives (:func:`report_event_line`). The workflow listens with
:func:`live_tool_calls` and keeps the latest ones on the active call, so the dashboard can
show a live tail.
pi prints no stream of its own on stdout, so its runtime keeps the RPC events in an
:class:`EventLog` and returns those lines as the transcript.

``factory project`` stores its decomposition calls the same way, under
``projects/<id>/calls/<n>.jsonl``, through :func:`store_call_transcript`.

With ``factory.store_prompts`` on, the workflow also writes the prompt of each run call to
``calls/<n>.prompt.md`` (:func:`store_call_prompt`), redacted and cut to
:data:`PROMPT_MAX_CHARS`. It is off by default: a prompt holds the most repository text.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from json import JSONDecodeError
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .atomic_write import write_text_atomic
from .prompts import build_prompt
from .redaction import redact_secrets

if TYPE_CHECKING:
    from .agents import AgentRequest
    from .config import FactorySettings
    from .models import FactoryRun

logger = logging.getLogger(__name__)

CALLS_DIRNAME = "calls"
TRANSCRIPT_MAX_CHARS = 2_000_000
TRANSCRIPT_CUT_MARKER = '{"type":"factory.transcript_cut"}\n'
MAX_TOOL_CALLS = 40
PREVIEW_CHARS = 120
#: The longest one-line preview of a transcript event the dashboard shows (ADR-047).
EVENT_PREVIEW_CHARS = 400
#: The longest stored prompt (ADR-047, ADR-070). A cut keeps the start and the end.
PROMPT_MAX_CHARS = 100_000
PROMPT_CUT_MARKER = "\n\n[factory: the middle of this prompt was cut]\n\n"

# Input keys that best say what a tool call did, in order of preference.
_PREVIEW_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")


#: Receives one ``{"name", "preview"}`` dict per tool call a running call starts.
ToolCallSink = Callable[[dict[str, str]], None]

_tool_call_sink: ContextVar[ToolCallSink | None] = ContextVar("tool_call_sink", default=None)
_output_sink: ContextVar[Callable[[], None] | None] = ContextVar("output_sink", default=None)


@contextmanager
def live_tool_calls(
    sink: ToolCallSink, on_output: Callable[[], None] | None = None
) -> Iterator[None]:
    """Send each tool call reported in this context to ``sink``; call ``on_output`` per line."""
    token = _tool_call_sink.set(sink)
    output_token = _output_sink.set(on_output)
    try:
        yield
    finally:
        _output_sink.reset(output_token)
        _tool_call_sink.reset(token)


def report_tool_call(name: str, arguments: Any) -> None:
    """Tell the listener, if any, that a tool call started. ``arguments`` gives the preview."""
    sink = _tool_call_sink.get()
    if sink is not None:
        sink({"name": _one_line(redact_secrets(name)), "preview": _preview(arguments)})


def report_output() -> None:
    """Tell the listener, if any, that the running call printed something (ADR-048)."""
    sink = _output_sink.get()
    if sink is not None:
        sink()


def report_event_line(line: str) -> None:
    """Report one line a streaming CLI printed: output arrived, and each tool call it starts."""
    report_output()
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):  # not JSON, or a number past Python's digit limit
        return
    for name, arguments in _tool_uses(event):
        report_tool_call(name, arguments)


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


def prompt_name(invocation_number: int) -> str:
    """The run-relative path of one call's stored prompt."""
    return f"{CALLS_DIRNAME}/{invocation_number:04d}.prompt.md"


def cap_prompt(text: str) -> str:
    """``text`` redacted, with its middle cut when it is longer than :data:`PROMPT_MAX_CHARS`."""
    redacted = redact_secrets(text)
    if len(redacted) <= PROMPT_MAX_CHARS:
        return redacted
    half = PROMPT_MAX_CHARS // 2
    return redacted[:half] + PROMPT_CUT_MARKER + redacted[-half:]


def store_call_prompt(
    settings: FactorySettings, run_dir: Path, run: FactoryRun, request: AgentRequest
) -> str | None:
    """Write the running call's prompt when ``factory.store_prompts`` is on; only log a failure."""
    active = run.active_invocation
    if not settings.store_prompts or active is None:
        return None
    name = prompt_name(active.invocation_number)
    try:
        write_text_atomic(run_dir / name, cap_prompt(build_prompt(request)), mode=0o600)
    except OSError:
        logger.warning("could not store the prompt of call %d", active.invocation_number)
        return None
    return name


def call_prompt_reader(runs_dir: Path) -> Callable[[str, int], str | None]:
    """Reads one call's stored prompt by run id and call number, for the dashboard."""

    def read(run_id: str, number: int) -> str | None:
        try:
            return (runs_dir / run_id / prompt_name(number)).read_text("utf-8", errors="replace")
        except OSError:
            return None

    return read


# pi repeats everything so far in each streaming update; the matching ``*_end`` event keeps it once.
_SKIPPED_EVENTS = frozenset({"message_update", "tool_execution_update"})


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


def read_call_transcript(run_dir: Path, invocation_number: int) -> str | None:
    """One call's stored transcript, or ``None`` when it is missing or unreadable."""
    try:
        return (run_dir / transcript_name(invocation_number)).read_text("utf-8", errors="replace")
    except OSError:
        return None


def call_transcript_reader(runs_dir: Path) -> Callable[[str, int], str | None]:
    """Reads one call's transcript by run id and call number, for the dashboard."""
    return lambda run_id, number: read_call_transcript(runs_dir / run_id, number)


def event_preview(line: str, limit: int = EVENT_PREVIEW_CHARS) -> dict[str, str]:
    """One transcript line as its event type and one line of what it says or does."""
    try:
        event = json.loads(line)
    except JSONDecodeError:
        event = None
    if not isinstance(event, dict):
        return {"type": "text", "text": _one_line(line, limit)}
    kind = event.get("type")
    uses = _tool_uses(event)
    text = "; ".join(f"{name} {_preview(arguments)}".strip() for name, arguments in uses)
    return {
        "type": _one_line(kind, 60) if isinstance(kind, str) else "event",
        "text": _one_line(text or _event_text(event), limit),
    }


def _event_text(event: dict[str, Any]) -> str:
    """The first text an event carries, at its top level, in ``data`` or in ``message``."""
    for source in (event, event.get("data"), event.get("message")):
        if not isinstance(source, dict):
            continue
        for key in ("result", "content", "text", "message"):
            text = _text_of(source.get(key))
            if text:
                return text
    return ""


def _text_of(value: Any) -> str:
    """A non-blank string, or the joined ``text`` of a list of content blocks."""
    if isinstance(value, str):
        return value if value.strip() else ""
    if not isinstance(value, list):
        return ""
    return " ".join(
        b["text"] for b in value if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def _one_line(value: str, limit: int = PREVIEW_CHARS) -> str:
    line = " ".join(value.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


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
