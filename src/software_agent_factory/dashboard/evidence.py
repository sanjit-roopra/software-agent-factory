"""Field allowlists for a run's check output and review text (ADR-047).

``GET /api/runs/<id>/checks`` and ``GET /api/runs/<id>/review`` pass the
provider's raw evidence through these functions. Each one builds its answer key
by key, so a provider field outside the allowlist never leaves the process.

Every text runs through ``redact_secrets`` before any cut. Command output and a
CI log excerpt keep their end, since a failure is near the end: the last
:data:`OUTPUT_LINE_LIMIT` lines and at most :data:`OUTPUT_CHAR_LIMIT`
characters, with a marker that names the cut. Review text is cut to
:data:`REVIEW_TEXT_LIMIT` characters per field, and a list keeps its first
:data:`MAX_EVIDENCE_ITEMS` items.
"""

from __future__ import annotations

from typing import Any

from ..models import ReviewFindingCategory
from ..redaction import bounded_reason, redact_secrets
from .snapshot import to_json_safe
from .validators import is_number, is_positive_int, is_safe_https_url, run_id_of

OUTPUT_LINE_LIMIT = 80
OUTPUT_CHAR_LIMIT = 8_000
REVIEW_TEXT_LIMIT = 2_000
MAX_EVIDENCE_ITEMS = 200
#: A command line or a check name is short; a longer one is cut.
_LABEL_LIMIT = 500
#: A finding names at most 8 source locations (``ReviewFindingDraft.locations``).
_MAX_LOCATIONS = 8
OUTPUT_CUT_MARKER = "...[earlier output cut]...\n"

_CATEGORIES = frozenset(category.value for category in ReviewFindingCategory)
_REVIEW_TEXT_LISTS = (
    "findings",
    "scope_concerns",
    "security_concerns",
    "compatibility_concerns",
    "suggested_changes",
)
_REVIEW_FINDING_LISTS = ("blocking_findings", "repair_regressions")


def output_tail(text: str) -> tuple[str, bool]:
    """The redacted end of ``text`` and whether anything before it was cut."""
    lines = redact_secrets(text).splitlines()
    cut = len(lines) > OUTPUT_LINE_LIMIT
    tail = "\n".join(lines[-OUTPUT_LINE_LIMIT:])
    if len(tail) > OUTPUT_CHAR_LIMIT:
        cut = True
    if cut:
        tail = OUTPUT_CUT_MARKER + tail[-(OUTPUT_CHAR_LIMIT - len(OUTPUT_CUT_MARKER)) :]
    return tail, cut


def _text(value: Any, run_id: str | None, limit: int = REVIEW_TEXT_LIMIT) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return bounded_reason(value, limit, run_id)[0]


def _items(value: Any) -> list[Any]:
    return value[:MAX_EVIDENCE_ITEMS] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in _items(value) if isinstance(item, dict)]


def _text_items(value: Any, run_id: str | None) -> list[str]:
    texts = (_text(item, run_id) for item in _items(value))
    return [text for text in texts if text is not None]


def _output_fields(*parts: Any) -> dict[str, Any]:
    text = "\n".join(part for part in parts if isinstance(part, str) and part)
    output, cut = output_tail(text)
    return {"output": output, "output_truncated": cut}


def _sanitize_check(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    exit_code = data.get("exit_code")
    duration = data.get("duration_seconds")
    timed_out = data.get("timed_out") is True
    valid_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    return {
        "command": _text(data.get("command"), run_id, _LABEL_LIMIT),
        "exit_code": exit_code if valid_exit else None,
        "duration_seconds": duration if is_number(duration) and duration >= 0 else None,
        "timed_out": timed_out,
        "passed": valid_exit and exit_code == 0 and not timed_out,
        **_output_fields(data.get("stdout"), data.get("stderr")),
    }


def _short(value: Any) -> str | None:
    """A short status or category word, such as ``FAIL``; anything else is ``None``."""
    if isinstance(value, str) and 0 < len(value) <= 64 and value.replace("_", "").isalnum():
        return value
    return None


def _sanitize_ci_check(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    url = data.get("details_url")
    return {
        "name": _text(data.get("name"), run_id, _LABEL_LIMIT),
        "status": _short(data.get("status")),
        "description": _text(data.get("description"), run_id, _LABEL_LIMIT),
        "details_url": url if is_safe_https_url(url) else None,
        "failure_category": _short(data.get("failure_category")),
        **_output_fields(data.get("log_excerpt")),
    }


def sanitize_run_checks(raw: Any) -> dict[str, Any]:
    """The verification and CI checks of a run, each with its redacted output tail."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run evidence must serialize to a JSON object")
    run_id = run_id_of(data)
    return {
        "checks": [_sanitize_check(item, run_id) for item in _dicts(data.get("checks"))],
        "ci_checks": [_sanitize_ci_check(item, run_id) for item in _dicts(data.get("ci_checks"))],
    }


def _location(data: dict[str, Any], run_id: str | None) -> dict[str, Any] | None:
    path = _text(data.get("path"), run_id, _LABEL_LIMIT)
    start, end = data.get("start_line"), data.get("end_line")
    if path is None or not (is_positive_int(start) and is_positive_int(end)):
        return None
    return {"path": path, "start_line": start, "end_line": end}


def _finding(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    category = data.get("category")
    locations = (_location(item, run_id) for item in _dicts(data.get("locations")))
    finding: dict[str, Any] = {
        "category": category if category in _CATEGORIES else None,
        "message": _text(data.get("message"), run_id),
        "locations": [item for item in locations if item is not None][:_MAX_LOCATIONS],
    }
    if "id" in data:
        finding["id"] = _text(data.get("id"), run_id, 80)
    return finding


def _findings(value: Any, run_id: str | None) -> list[dict[str, Any]]:
    return [_finding(item, run_id) for item in _dicts(value)]


def _disposition(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    return {
        "finding_id": _text(data.get("finding_id"), run_id, 80),
        "status": _short(data.get("status")),
        "rationale": _text(data.get("rationale"), run_id),
    }


def _round(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    attempt = data.get("attempt_number")
    review: dict[str, Any] = {
        "attempt_number": attempt if is_positive_int(attempt) else None,
        "approved": data.get("approved") is True,
        "skipped": data.get("skipped") is True,
        "skip_reason": _text(data.get("skip_reason"), run_id),
    }
    for key in _REVIEW_TEXT_LISTS:
        review[key] = _text_items(data.get(key), run_id)
    for key in _REVIEW_FINDING_LISTS:
        review[key] = _findings(data.get(key), run_id)
    review["prior_finding_dispositions"] = [
        _disposition(item, run_id) for item in _dicts(data.get("prior_finding_dispositions"))
    ]
    return review


def sanitize_run_review(raw: Any) -> dict[str, Any]:
    """The review rounds of a run and its open and accepted findings, text redacted and cut."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError("run evidence must serialize to a JSON object")
    run_id = run_id_of(data)
    return {
        "rounds": [_round(item, run_id) for item in _dicts(data.get("review_rounds"))],
        "open_findings": _findings(data.get("open_findings"), run_id),
        "accepted_findings": _findings(data.get("accepted_findings"), run_id),
    }


__all__ = [
    "MAX_EVIDENCE_ITEMS",
    "OUTPUT_CHAR_LIMIT",
    "OUTPUT_LINE_LIMIT",
    "REVIEW_TEXT_LIMIT",
    "output_tail",
    "sanitize_run_checks",
    "sanitize_run_review",
]
