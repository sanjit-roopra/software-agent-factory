"""Runtime-neutral parsing of final assistant text into a typed artifact.

Every ``AgentRuntime`` implementation (Copilot today, pi from Slice 3 of
``plans/pi-agent-runtime.md``) ends a call with one blob of "final assistant
text". This module turns that text into the single typed artifact the
requested role/purpose expects (:class:`~.models.TriageResult`,
:class:`~.models.ChangeSet`, etc.), validating it and raising the same
:class:`ValueError` wording regardless of which runtime produced the text.

Runtime-specific stdout/event parsing (e.g. Copilot's JSONL event stream in
``copilot_runtime.extract_assistant_text``) happens before this module is
reached, not inside it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Literal, TypeAlias

from pydantic import ValidationError

from .models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ModelBase,
    ProjectPlan,
    RepositorySkill,
)
from .prompts import RoleName, artifact_model_for_role, normalize_role

ResultField: TypeAlias = Literal[
    "triage_result",
    "specification",
    "research_report",
    "execution_plan",
    "change_set",
    "test_report",
    "review_report",
    "repository_skill",
    "project_plan",
]


@dataclass(frozen=True)
class _ArtifactSpec:
    model_class: type[ModelBase]
    result_field: ResultField


ARTIFACT_SPECS: dict[str, _ArtifactSpec] = {
    "TRIAGE": _ArtifactSpec(
        model_class=artifact_model_for_role("TRIAGE"),
        result_field="triage_result",
    ),
    "REFINER": _ArtifactSpec(
        model_class=artifact_model_for_role("REFINER"),
        result_field="specification",
    ),
    "RESEARCHER": _ArtifactSpec(
        model_class=artifact_model_for_role("RESEARCHER"),
        result_field="research_report",
    ),
    "PLANNER": _ArtifactSpec(
        model_class=artifact_model_for_role("PLANNER"),
        result_field="execution_plan",
    ),
    "IMPLEMENTER": _ArtifactSpec(
        model_class=artifact_model_for_role("IMPLEMENTER"),
        result_field="change_set",
    ),
    # The independent tester produces a TestReport (AI judgement), never a
    # VerificationReport -- deterministic evidence is factory-produced.
    "TESTER": _ArtifactSpec(
        model_class=artifact_model_for_role("TESTER"),
        result_field="test_report",
    ),
    "REVIEWER": _ArtifactSpec(
        model_class=artifact_model_for_role("REVIEWER"),
        result_field="review_report",
    ),
}


def artifact_spec(
    role: RoleName,
    purpose: AgentPurpose = AgentPurpose.STANDARD,
) -> _ArtifactSpec:
    if purpose is AgentPurpose.DECOMPOSE_PROJECT:
        if normalize_role(role) != AgentRole.PLANNER.value:
            raise ValueError("project decomposition requires the PLANNER role")
        return _ArtifactSpec(
            model_class=ProjectPlan,
            result_field="project_plan",
        )
    if purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
        if normalize_role(role) != AgentRole.RESEARCHER.value:
            raise ValueError("repository skill generation requires the RESEARCHER role")
        return _ArtifactSpec(
            model_class=RepositorySkill,
            result_field="repository_skill",
        )
    if purpose is AgentPurpose.CORRECT_CHANGE_SET:
        if normalize_role(role) != AgentRole.IMPLEMENTER.value:
            raise ValueError("ChangeSet correction requires the IMPLEMENTER role")
        return _ArtifactSpec(
            model_class=ChangeSet,
            result_field="change_set",
        )
    normalized_role = normalize_role(role)
    try:
        return ARTIFACT_SPECS[normalized_role]
    except KeyError as exc:  # pragma: no cover - defensive programmer error
        raise ValueError(f"unsupported agent role: {role!r}") from exc


def candidate_texts(assistant_text: str) -> list[str]:
    text = assistant_text.strip()
    return [text] if text else []


@dataclass
class ScanStats:
    """Deterministic work counter for JSON candidate scanning."""

    chars_scanned: int = 0
    candidates_tested: int = 0


def _scan_matching_brace(
    text: str,
    start: int,
    length: int,
    *,
    matched_pairs: dict[int, int],
    stats: ScanStats,
    work_limit: int,
) -> tuple[int, list[int]]:
    """Scan forward from an opening ``{`` at ``start`` for its matching ``}``.

    Tracks nesting depth (ignoring braces inside strings) and records every
    open-brace index whose matching close is discovered along the way into
    ``matched_pairs``, so a caller scanning an enclosing object can skip
    re-scanning braces already resolved here. Returns ``(found_end,
    open_stack)``: ``found_end`` is the index of the matching ``}``, or
    ``-1`` if the scan ran out of text or hit ``work_limit`` first;
    ``open_stack`` holds any braces still open when the scan stopped (only
    meaningful when ``found_end`` is ``-1``).
    """
    depth = 0
    in_string = False
    escape = False
    k = start
    found_end = -1
    open_stack: list[int] = []

    while k < length:
        stats.chars_scanned += 1
        if stats.chars_scanned >= work_limit:
            break

        char = text[k]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
                open_stack.append(k)
            elif char == "}":
                if open_stack:
                    popped = open_stack.pop()
                    matched_pairs[popped] = k
                depth -= 1
                if depth == 0:
                    found_end = k
                    break
        k += 1

    return found_end, open_stack


def _iter_json_objects(
    text: str,
    *,
    scan_stats: ScanStats | None = None,
    max_scan_work: int | None = None,
    max_nested_failures: int = 8,
) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, object]] = []
    length = len(text)
    stats = scan_stats if scan_stats is not None else ScanStats()
    work_limit = max_scan_work if max_scan_work is not None else max(100_000, 10 * length)

    unclosed_starts: set[int] = set()
    matched_pairs: dict[int, int] = {}
    enclosing_failed_end = -1
    nested_failures = 0
    i = 0

    while i < length:
        if stats.chars_scanned >= work_limit:
            break

        start = text.find("{", i)
        if start == -1:
            break

        if start in unclosed_starts:
            i = start + 1
            continue

        # Check if next non-whitespace character after '{' is '"' or '}'
        j = start + 1
        while j < length and text[j].isspace():
            j += 1
        if j >= length or text[j] not in ('"', "}"):
            i = start + 1
            continue

        # Check if matching brace was already discovered in an earlier enclosing scan
        if start in matched_pairs:
            found_end = matched_pairs[start]
        else:
            found_end, open_stack = _scan_matching_brace(
                text,
                start,
                length,
                matched_pairs=matched_pairs,
                stats=stats,
                work_limit=work_limit,
            )

            if stats.chars_scanned >= work_limit:
                break

            if found_end == -1:
                unclosed_starts.update(open_stack)
                i = start + 1
                continue

        stats.candidates_tested += 1
        try:
            payload, end_idx = decoder.raw_decode(text, idx=start)
        except JSONDecodeError:
            if enclosing_failed_end != -1 and found_end <= enclosing_failed_end:
                nested_failures += 1
                if nested_failures >= max_nested_failures:
                    i = enclosing_failed_end + 1
                    enclosing_failed_end = -1
                    nested_failures = 0
                    continue
            else:
                enclosing_failed_end = found_end
                nested_failures = 1
            i = start + 1
            continue

        if isinstance(payload, dict):
            objects.append(payload)
            i = max(end_idx, found_end + 1)
            enclosing_failed_end = -1
            nested_failures = 0
        else:
            i = start + 1

    return objects


def _queue_nested_candidates(
    values: Iterable[object],
    *,
    candidates: list[dict[str, object]],
    queue: list[object],
    seen_ids: set[int],
) -> None:
    """Add each not-yet-seen dict in ``values`` to ``candidates``/``queue``.

    A bare list value is queued for its own later traversal without being a
    candidate itself (only dicts are). Shared by both the dict-values and
    list-items branches of :func:`_iter_nested_dicts`.
    """
    for value in values:
        if isinstance(value, dict):
            if id(value) not in seen_ids:
                seen_ids.add(id(value))
                candidates.append(value)
                queue.append(value)
        elif isinstance(value, list):
            queue.append(value)


def _iter_nested_dicts(payload: dict[str, object]) -> list[dict[str, object]]:
    """Yield payload first, then any nested dictionaries breadth-first."""
    candidates: list[dict[str, object]] = [payload]
    queue: list[object] = [payload]
    seen_ids: set[int] = {id(payload)}

    while queue:
        current = queue.pop(0)
        if isinstance(current, dict):
            _queue_nested_candidates(
                current.values(), candidates=candidates, queue=queue, seen_ids=seen_ids
            )
        elif isinstance(current, list):
            _queue_nested_candidates(current, candidates=candidates, queue=queue, seen_ids=seen_ids)

    return candidates


#: Cap on how many individual field errors a failed-validation message
#: lists before summarizing the rest as a count, so one artifact with many
#: missing fields doesn't produce an unbounded failure message.
_MAX_VALIDATION_ERROR_SUMMARIES = 8


def _summarize_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    if not details:
        return str(error)
    summaries: list[str] = []
    for detail in details[:_MAX_VALIDATION_ERROR_SUMMARIES]:
        location = ".".join(str(part) for part in detail.get("loc", ()))
        message = str(detail.get("msg", "validation error"))
        summaries.append(f"{location}: {message}" if location else message)
    if len(details) > len(summaries):
        summaries.append(f"{len(details) - len(summaries)} more validation error(s)")
    return "; ".join(summaries)


def parse_artifact_from_candidates(
    role: RoleName,
    candidates: list[str],
    *,
    purpose: AgentPurpose,
) -> ModelBase:
    """Shared validation core: try each candidate text for a matching artifact.

    Both :func:`parse_agent_artifact` (one candidate: the final assistant
    text) and ``copilot_runtime.parse_copilot_artifact`` (several candidates
    drawn from Copilot's JSONL event stream) funnel through this so both
    raise byte-identical failure wording.
    """

    spec = artifact_spec(role, purpose)

    found_object = False
    first_validation_error: ValidationError | None = None
    for candidate in candidates:
        for payload in _iter_json_objects(candidate):
            found_object = True
            for dict_candidate in _iter_nested_dicts(payload):
                try:
                    return spec.model_class.model_validate(dict_candidate)
                except ValidationError as exc:
                    if first_validation_error is None or (
                        "schema_version" in dict_candidate and "schema_version" not in payload
                    ):
                        first_validation_error = exc

    role_name = normalize_role(role)
    if first_validation_error is not None:
        raise ValueError(
            f"{role_name} response did not validate as {spec.model_class.__name__}: "
            f"{_summarize_validation_error(first_validation_error)}"
        )
    if not found_object:
        raise ValueError(
            f"{role_name} response did not contain a parseable JSON object for "
            f"{spec.model_class.__name__}"
        )
    raise ValueError(f"{role_name} response did not contain a valid {spec.model_class.__name__}")


def parse_agent_artifact(
    role: RoleName,
    *,
    text: str,
    purpose: AgentPurpose = AgentPurpose.STANDARD,
) -> ModelBase:
    """Extract and validate a single typed artifact from final assistant text.

    ``text`` is the already-extracted final assistant message text for one
    agent call, independent of which runtime produced it.
    """

    return parse_artifact_from_candidates(role, candidate_texts(text), purpose=purpose)
