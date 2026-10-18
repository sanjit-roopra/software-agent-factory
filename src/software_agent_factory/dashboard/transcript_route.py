"""The ``GET /api/runs/<id>/calls/<n>/transcript`` route (ADR-047, ADR-050).

It serves one agent call's stored transcript: a one-line preview of each of the last
:data:`MAX_EVENTS` events, and the last :data:`TAIL_CHARS` characters of the raw text.
The text goes through ``redact_secrets`` again before any cut, and only the last
:data:`MAX_TRANSCRIPT_CHARS` characters are read. ``truncated`` says that events were
left out. The text comes from a :class:`~.snapshot.RunTranscriptProvider`, never from a
store this package reads itself.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Any

from ..redaction import redact_secrets
from ..transcripts import TRANSCRIPT_CUT_MARKER, event_preview
from .snapshot import RunTranscriptProvider, is_valid_run_id

_logger = logging.getLogger("software_agent_factory.dashboard")

_TRANSCRIPT_PATTERN = re.compile(r"^/api/runs/([^/]+)/calls/([^/]+)/transcript$")
#: A call number: 1 to 9999, no sign or leading zero, as in ``calls/<n>.jsonl``.
_CALL_PATTERN = re.compile(r"^[1-9]\d{0,3}$", re.ASCII)
MAX_TRANSCRIPT_CHARS = 1_000_000
MAX_EVENTS = 500
TAIL_CHARS = 16_000

_NOT_FOUND: tuple[HTTPStatus, dict[str, Any]] = (HTTPStatus.NOT_FOUND, {"error": "not found"})


def transcript_answer(
    provider: RunTranscriptProvider | None, path: str
) -> tuple[HTTPStatus, dict[str, Any]]:
    """The status and JSON body for ``path``: ``404`` when it is not a transcript route."""
    match = _TRANSCRIPT_PATTERN.match(path)
    if provider is None or match is None:
        return _NOT_FOUND
    run_id, raw_number = match.groups()
    if not is_valid_run_id(run_id) or not _CALL_PATTERN.match(raw_number):
        return _NOT_FOUND
    number = int(raw_number)
    try:
        text = provider(run_id, number)
    except Exception as exc:  # noqa: BLE001 - the type only: the message can quote run data
        _logger.error("Transcript provider failed for run %s: %s", run_id, type(exc).__name__)
        return HTTPStatus.SERVICE_UNAVAILABLE, {"error": "transcript unavailable"}
    if text is None:
        return _NOT_FOUND
    return HTTPStatus.OK, {"run_id": run_id, "call": number, **sanitize_transcript(text)}


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
