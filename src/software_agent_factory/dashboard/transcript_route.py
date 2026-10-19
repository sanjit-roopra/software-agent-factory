"""The ``GET /api/runs/<id>/calls/<n>/transcript`` and ``.../prompt`` routes (ADR-047, ADR-050).

It serves one agent call's stored transcript: a one-line preview of each of the last
:data:`MAX_EVENTS` events, and the last :data:`TAIL_CHARS` characters of the raw text.
The text goes through ``redact_secrets`` again before any cut, and only the last
:data:`MAX_TRANSCRIPT_CHARS` characters are kept. ``truncated`` says that events were
left out. The text comes from a :class:`~.snapshot.RunTranscriptProvider`, never from a
store this package reads itself.

The prompt route serves the prompt stored when ``factory.store_prompts`` is on (ADR-070):
the text, redacted again and cut to :data:`~..transcripts.PROMPT_MAX_CHARS`. It is ``404``
when no prompt was stored.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Any, Protocol

from ..redaction import redact_secrets
from ..transcripts import PROMPT_CUT_MARKER, TRANSCRIPT_CUT_MARKER, cap_prompt, event_preview
from .snapshot import RunTranscriptProvider, is_valid_run_id

_logger = logging.getLogger("software_agent_factory.dashboard")

_CALL_FILE_PATTERN = re.compile(r"^/api/runs/([^/]+)/calls/([^/]+)/(transcript|prompt)$")
#: A call number: 1 to 9999, no sign or leading zero, as in ``calls/<n>.jsonl``.
_CALL_PATTERN = re.compile(r"^[1-9]\d{0,3}$", re.ASCII)
MAX_TRANSCRIPT_CHARS = 1_000_000
MAX_EVENTS = 500
TAIL_CHARS = 16_000

_NOT_FOUND: tuple[HTTPStatus, dict[str, Any]] = (HTTPStatus.NOT_FOUND, {"error": "not found"})


def _log_failure_type(run_id: str, exc: BaseException) -> None:
    """Log a provider failure by its exception type only, never its message or traceback."""
    _logger.error("Transcript provider failed for run %s: %s", run_id, type(exc).__name__)


class CallFileProviders(Protocol):
    """The server attributes that read a call's stored transcript and prompt."""

    run_transcript_provider: RunTranscriptProvider | None
    run_prompt_provider: RunTranscriptProvider | None


def transcript_answer(providers: CallFileProviders, path: str) -> tuple[HTTPStatus, dict[str, Any]]:
    """The status and JSON body for ``path``: ``404`` when it is not a call file route."""
    match = _CALL_FILE_PATTERN.match(path)
    if match is None:
        return _NOT_FOUND
    run_id, raw_number, kind = match.groups()
    prompt = kind == "prompt"
    provider = providers.run_prompt_provider if prompt else providers.run_transcript_provider
    if provider is None or not is_valid_run_id(run_id) or not _CALL_PATTERN.match(raw_number):
        return _NOT_FOUND
    number = int(raw_number)
    try:
        text = provider(run_id, number)
    except Exception as exc:  # noqa: BLE001 - the type only: the message can quote run data
        _log_failure_type(run_id, exc)
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{kind} unavailable"}
    if text is None:
        return _NOT_FOUND
    body = sanitize_prompt(text) if prompt else sanitize_transcript(text)
    return HTTPStatus.OK, {"run_id": run_id, "call": number, **body}


def sanitize_prompt(text: str) -> dict[str, Any]:
    """One stored prompt, redacted again and capped; ``truncated`` when it was cut."""
    capped = cap_prompt(text)
    return {"text": capped, "truncated": PROMPT_CUT_MARKER in capped}


def sanitize_transcript(text: str) -> dict[str, Any]:
    """Event previews and a raw tail of one transcript, redacted and capped."""
    redacted = redact_secrets(text)
    truncated = TRANSCRIPT_CUT_MARKER in redacted
    if len(redacted) > MAX_TRANSCRIPT_CHARS:
        truncated = True
        start = len(redacted) - MAX_TRANSCRIPT_CHARS
        line_start = redacted.find("\n", start)
        redacted = redacted[line_start + 1 if line_start >= 0 else start :]
    lines = [line for line in redacted.splitlines() if line.strip()]
    if len(lines) > MAX_EVENTS:
        truncated = True
        lines = lines[-MAX_EVENTS:]
    return {
        "events": [event_preview(line) for line in lines],
        "tail": redacted[-TAIL_CHARS:],
        "truncated": truncated,
    }
