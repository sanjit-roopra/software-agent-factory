"""Field allowlists for a run's check output, review text, plan and diff (ADR-047).

``GET /api/runs/<id>/checks``, ``/review``, ``/plan`` and ``/diff`` pass the
provider's raw evidence through these functions. Each one builds its answer key
by key, so a provider field outside the allowlist never leaves the process.

Every text runs through ``redact_secrets`` before any cut. Command output and a
CI log excerpt keep their end, since a failure is near the end: the last
:data:`OUTPUT_LINE_LIMIT` lines and at most :data:`OUTPUT_CHAR_LIMIT`
characters, with a marker that names the cut. Review text is cut to
:data:`REVIEW_TEXT_LIMIT` characters per field, and a list keeps its first
:data:`MAX_EVIDENCE_ITEMS` items. Plan and change set text follow the review
limits. A diff is split per file with its added and removed line counts; file
text is kept in order until :data:`DIFF_CHAR_LIMIT` characters, and a file past
that keeps its counts but loses its text.
"""

from __future__ import annotations

from typing import Any

from ..models import ReviewFindingCategory
from ..redaction import bounded_reason, redact_secrets
from .snapshot import to_json_safe
from .validators import is_number, is_positive_int, is_safe_https_url, run_id_of

_NOT_AN_OBJECT = "run evidence must serialize to a JSON object"

OUTPUT_LINE_LIMIT = 80
OUTPUT_CHAR_LIMIT = 8_000
REVIEW_TEXT_LIMIT = 2_000
MAX_EVIDENCE_ITEMS = 200
#: A command line or a check name is short; a longer one is cut.
_LABEL_LIMIT = 500
#: A finding names at most 8 source locations (``ReviewFindingDraft.locations``).
_MAX_LOCATIONS = 8
OUTPUT_CUT_MARKER = "...[earlier output cut]...\n"
#: The most diff text one answer carries: 256 KB per attempt (ADR-047).
DIFF_CHAR_LIMIT = 256_000
_DIFF_FILE_HEADER = "diff --git "
#: The most attempts a check history carries, newest kept; matches ``run_evidence.HISTORY_LIMIT``.
MAX_HISTORY = 10

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


def _history(value: Any, run_id: str | None, ci: bool) -> list[dict[str, Any]]:
    """Per-attempt checks; an entry without a valid attempt number is dropped."""
    entries = []
    for item in _dicts(value)[-MAX_HISTORY:]:
        attempt = item.get("attempt")
        if not is_positive_int(attempt):
            continue
        checks = _dicts(item.get("checks"))
        entry: dict[str, Any] = {"attempt": attempt}
        if ci:
            entry["overall"] = _short(item.get("overall"))
            entry["checks"] = [_sanitize_ci_check(check, run_id) for check in checks]
        else:
            entry["checks"] = [_sanitize_check(check, run_id) for check in checks]
        entries.append(entry)
    return entries


def sanitize_run_checks(raw: Any) -> dict[str, Any]:
    """The verification and CI checks of a run, each with its redacted output tail,
    and the same per attempt for the attempt picker."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError(_NOT_AN_OBJECT)
    run_id = run_id_of(data)
    return {
        "checks": [_sanitize_check(item, run_id) for item in _dicts(data.get("checks"))],
        "ci_checks": [_sanitize_ci_check(item, run_id) for item in _dicts(data.get("ci_checks"))],
        "verification_history": _history(data.get("verification_history"), run_id, ci=False),
        "ci_history": _history(data.get("ci_history"), run_id, ci=True),
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
        raise TypeError(_NOT_AN_OBJECT)
    run_id = run_id_of(data)
    return {
        "rounds": [_round(item, run_id) for item in _dicts(data.get("review_rounds"))],
        "open_findings": _findings(data.get("open_findings"), run_id),
        "accepted_findings": _findings(data.get("accepted_findings"), run_id),
    }


_SPEC_TEXT_LISTS = ("acceptance_criteria", "constraints", "assumptions", "unknowns")
_PLAN_TEXT_LISTS = ("test_strategy", "risks", "unresolved_decisions")
_CHANGE_SET_LISTS = ("changed_files", "tests_added", "commands_run")


def _text_lists(data: dict[str, Any], keys: tuple[str, ...], run_id: str | None) -> dict[str, Any]:
    return {key: _text_items(data.get(key), run_id) for key in keys}


def _specification(data: Any, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    return {
        "problem": _text(data.get("problem"), run_id),
        **_text_lists(data, _SPEC_TEXT_LISTS, run_id),
    }


def _plan_step(data: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    return {
        "id": _text(data.get("id"), run_id, 80),
        "goal": _text(data.get("goal"), run_id),
        "likely_files": _text_items(data.get("likely_files"), run_id),
        "validation": _text_items(data.get("validation"), run_id),
    }


def _expected_scope(data: Any, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    low, high = data.get("estimated_files_min"), data.get("estimated_files_max")
    valid = type(low) is int and type(high) is int and 0 <= low <= high
    return {
        "modules": _text_items(data.get("modules"), run_id),
        "estimated_files_min": low if valid else None,
        "estimated_files_max": high if valid else None,
    }


def _execution_plan(data: Any, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    return {
        "summary": _text(data.get("summary"), run_id),
        "steps": [_plan_step(item, run_id) for item in _dicts(data.get("steps"))],
        "expected_scope": _expected_scope(data.get("expected_scope"), run_id),
        **_text_lists(data, _PLAN_TEXT_LISTS, run_id),
    }


def _change_set(data: Any, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    return {
        "summary": _text(data.get("summary"), run_id),
        **_text_lists(data, _CHANGE_SET_LISTS, run_id),
    }


def sanitize_run_plan(raw: Any) -> dict[str, Any]:
    """The run's specification, plan, change set and out-of-scope paths, text redacted and cut."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError(_NOT_AN_OBJECT)
    run_id = run_id_of(data)
    return {
        "branch_name": _text(data.get("branch_name"), run_id, _LABEL_LIMIT),
        "specification": _specification(data.get("specification"), run_id),
        "execution_plan": _execution_plan(data.get("execution_plan"), run_id),
        "change_set": _change_set(data.get("change_set"), run_id),
        "out_of_scope_paths": _text_items(data.get("out_of_scope_paths"), run_id),
    }


def _split_files(patch: str) -> list[str]:
    """The per-file parts of a git patch; text before the first header is dropped."""
    parts = patch.split("\n" + _DIFF_FILE_HEADER)
    first = parts[0]
    files = [_DIFF_FILE_HEADER + part for part in parts[1:]]
    if first.startswith(_DIFF_FILE_HEADER):
        files.insert(0, first)
    return files


def _file_path(text: str) -> str:
    """The new path of a file part, or the old one for a deleted file."""
    for line in text.splitlines()[:8]:
        if line.startswith("+++ b/"):
            return line[len("+++ b/") :]
        if line.startswith("rename to "):
            return line[len("rename to ") :]
    header = text.splitlines()[0][len(_DIFF_FILE_HEADER) :]
    return header.rsplit(" b/", 1)[-1] if " b/" in header else header


def _file_status(text: str) -> str:
    head = text[:400]
    for marker, status in (
        ("\nnew file mode", "added"),
        ("\ndeleted file mode", "deleted"),
        ("\nrename to ", "renamed"),
    ):
        if marker in head:
            return status
    return "modified"


def _line_counts(text: str) -> tuple[int, int]:
    """Counts +/- lines after the first hunk, so a removed '-- x' is not a header."""
    added = removed = 0
    in_hunks = False
    for line in text.splitlines():
        in_hunks = in_hunks or line.startswith("@@")
        if in_hunks and line.startswith("+"):
            added += 1
        elif in_hunks and line.startswith("-"):
            removed += 1
    return added, removed


def _diff_files(patch: str) -> tuple[list[dict[str, Any]], bool]:
    files: list[dict[str, Any]] = []
    budget = DIFF_CHAR_LIMIT
    parts = _split_files(patch)
    cut = len(parts) > MAX_EVIDENCE_ITEMS
    for text in parts[:MAX_EVIDENCE_ITEMS]:
        added, removed = _line_counts(text)
        fits = len(text) <= budget
        budget -= len(text) if fits else 0
        cut = cut or not fits
        files.append(
            {
                "path": _file_path(text)[:_LABEL_LIMIT],
                "status": _file_status(text),
                "additions": added,
                "deletions": removed,
                "patch": text if fits else None,
            }
        )
    return files, cut


def sanitize_run_diff(raw: Any) -> dict[str, Any]:
    """One saved patch of a run, redacted, split per file and capped (ADR-047)."""
    data = to_json_safe(raw)
    if not isinstance(data, dict):
        raise TypeError(_NOT_AN_OBJECT)
    attempt = data.get("attempt")
    attempts = [item for item in _items(data.get("attempts")) if is_positive_int(item)]
    patch = data.get("patch")
    files, cut = _diff_files(redact_secrets(patch)) if isinstance(patch, str) else ([], False)
    return {
        "attempt": attempt if is_positive_int(attempt) else None,
        "attempts": attempts,
        "has_patch": isinstance(patch, str),
        "files": files,
        "additions": sum(item["additions"] for item in files),
        "deletions": sum(item["deletions"] for item in files),
        "truncated": cut,
    }


__all__ = [
    "DIFF_CHAR_LIMIT",
    "MAX_EVIDENCE_ITEMS",
    "MAX_HISTORY",
    "OUTPUT_CHAR_LIMIT",
    "OUTPUT_LINE_LIMIT",
    "REVIEW_TEXT_LIMIT",
    "output_tail",
    "sanitize_run_checks",
    "sanitize_run_diff",
    "sanitize_run_plan",
    "sanitize_run_review",
]
