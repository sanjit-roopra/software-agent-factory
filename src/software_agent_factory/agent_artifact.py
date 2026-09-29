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
from typing import Literal

from pydantic import ValidationError

from .agents import AgentResult
from .models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    ModelBase,
    PerformanceRecord,
    ProjectPlan,
    RepositorySkill,
    UsageMetrics,
)
from .prompts import RoleName, artifact_model_for_role, normalize_role

type ResultField = Literal[
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


def _advance_string_state(char: str, escape: bool) -> tuple[bool, bool]:
    """Return ``(in_string, escape)`` after consuming ``char`` inside a JSON string."""
    if escape:
        return True, False
    if char == "\\":
        return True, True
    return char != '"', False


class _JsonObjectScanner:
    """Bounded-work scan for the ``}`` that closes each ``{`` in ``text``.

    Holds the state shared across the scans of one text: the work counter and
    its limit, every open-brace index whose matching close an earlier scan
    already found (``matched_pairs``, so a scan of an enclosing object skips
    re-scanning braces resolved here), and the braces known to be unclosed
    (``unclosed_starts``).
    """

    def __init__(
        self,
        text: str,
        *,
        stats: ScanStats | None = None,
        max_scan_work: int | None = None,
    ) -> None:
        self.text = text
        self.stats = stats if stats is not None else ScanStats()
        self.work_limit = (
            max_scan_work if max_scan_work is not None else max(100_000, 10 * len(text))
        )
        self.matched_pairs: dict[int, int] = {}
        self.unclosed_starts: set[int] = set()

    def work_exhausted(self) -> bool:
        return self.stats.chars_scanned >= self.work_limit

    def is_scannable_start(self, start: int) -> bool:
        """True when the ``{`` at ``start`` may open a JSON object worth scanning.

        Rejects braces already known to be unclosed and braces whose next
        non-whitespace character is neither ``"`` nor ``}``.
        """
        if start in self.unclosed_starts:
            return False
        length = len(self.text)
        j = start + 1
        while j < length and self.text[j].isspace():
            j += 1
        return j < length and self.text[j] in ('"', "}")

    def find_object_end(self, start: int) -> int:
        """Return the index of the ``}`` matching the ``{`` at ``start``, or ``-1``.

        Reuses a match discovered by an earlier enclosing scan when available.
        ``-1`` means the brace is not a scannable start, the object is unclosed
        (its still-open braces are recorded in ``unclosed_starts``), or the scan
        hit the work limit first.
        """
        if not self.is_scannable_start(start):
            return -1
        if start in self.matched_pairs:
            return self.matched_pairs[start]
        found_end, open_stack = self._scan_matching_brace(start)
        if self.work_exhausted():
            return -1
        if found_end == -1:
            self.unclosed_starts.update(open_stack)
        return found_end

    def _scan_matching_brace(self, start: int) -> tuple[int, list[int]]:
        """Scan forward from an opening ``{`` at ``start`` for its matching ``}``.

        Tracks nesting (ignoring braces inside strings) and records every
        open-brace index whose matching close is discovered along the way into
        ``matched_pairs``. Returns ``(found_end, open_stack)``: ``found_end`` is
        the index of the matching ``}``, or ``-1`` if the scan ran out of text
        or hit the work limit first; ``open_stack`` holds any braces still open
        when the scan stopped (only meaningful when ``found_end`` is ``-1``).
        """
        in_string = False
        escape = False
        open_stack: list[int] = []

        for k in range(start, len(self.text)):
            self.stats.chars_scanned += 1
            if self.work_exhausted():
                break

            char = self.text[k]
            if in_string:
                in_string, escape = _advance_string_state(char, escape)
            elif char == '"':
                in_string = True
            elif char == "{":
                open_stack.append(k)
            elif char == "}" and self._close_brace(k, open_stack):
                return k, open_stack

        return -1, open_stack

    def _close_brace(self, index: int, open_stack: list[int]) -> bool:
        """Pair the ``}`` at ``index`` with its ``{``; True when that closes the outermost one.

        The scan starts on a ``{``, so ``open_stack`` is never empty at a ``}``.
        """
        self.matched_pairs[open_stack.pop()] = index
        return not open_stack


@dataclass
class _NestedFailureTracker:
    """Bounds retries on objects nested inside an enclosing object that failed to decode."""

    max_failures: int
    enclosing_failed_end: int = -1
    failures: int = 0

    def reset(self) -> None:
        self.enclosing_failed_end = -1
        self.failures = 0

    def record_failure(self, start: int, found_end: int) -> int:
        """Record a decode failure of the object at ``start``; return where to resume."""
        if self.enclosing_failed_end != -1 and found_end <= self.enclosing_failed_end:
            self.failures += 1
            if self.failures >= self.max_failures:
                resume = self.enclosing_failed_end + 1
                self.reset()
                return resume
        else:
            self.enclosing_failed_end = found_end
            self.failures = 1
        return start + 1


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
    scanner = _JsonObjectScanner(text, stats=scan_stats, max_scan_work=max_scan_work)
    failures = _NestedFailureTracker(max_failures=max_nested_failures)
    i = 0

    while i < length and not scanner.work_exhausted():
        start = text.find("{", i)
        if start == -1:
            break

        found_end = scanner.find_object_end(start)
        if found_end == -1:
            i = start + 1
            continue

        scanner.stats.candidates_tested += 1
        try:
            payload, end_idx = decoder.raw_decode(text, idx=start)
        except JSONDecodeError:
            i = failures.record_failure(start, found_end)
            continue

        if isinstance(payload, dict):
            objects.append(payload)
            i = max(end_idx, found_end + 1)
            failures.reset()
            continue
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


def _validate_payload(
    model_class: type[ModelBase],
    payload: dict[str, object],
    first_validation_error: ValidationError | None,
) -> tuple[ModelBase | None, ValidationError | None]:
    """Validate ``payload`` or a dict nested in it as ``model_class``.

    Returns ``(artifact, first_validation_error)``. The error carried across
    payloads is the first one seen, replaced by a nested candidate's error
    when that candidate carries ``schema_version`` but ``payload`` does not.
    """
    for dict_candidate in _iter_nested_dicts(payload):
        try:
            return model_class.model_validate(dict_candidate), first_validation_error
        except ValidationError as exc:
            if first_validation_error is None or (
                "schema_version" in dict_candidate and "schema_version" not in payload
            ):
                first_validation_error = exc
    return None, first_validation_error


def _no_artifact_error(
    role: RoleName,
    model_class: type[ModelBase],
    *,
    found_object: bool,
    first_validation_error: ValidationError | None,
) -> ValueError:
    role_name = normalize_role(role)
    if first_validation_error is not None:
        return ValueError(
            f"{role_name} response did not validate as {model_class.__name__}: "
            f"{_summarize_validation_error(first_validation_error)}"
        )
    if not found_object:
        return ValueError(
            f"{role_name} response did not contain a parseable JSON object for "
            f"{model_class.__name__}"
        )
    return ValueError(f"{role_name} response did not contain a valid {model_class.__name__}")


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
            artifact, first_validation_error = _validate_payload(
                spec.model_class, payload, first_validation_error
            )
            if artifact is not None:
                return artifact

    raise _no_artifact_error(
        role,
        spec.model_class,
        found_object=found_object,
        first_validation_error=first_validation_error,
    )


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


def build_success_result(
    role: RoleName,
    *,
    purpose: AgentPurpose,
    artifact: ModelBase,
    usage: UsageMetrics | None = None,
    performance: PerformanceRecord | None = None,
) -> AgentResult:
    """Build the success :class:`AgentResult` carrying ``artifact`` in its typed field.

    ``artifact_spec(role, purpose).result_field`` names which ``AgentResult``
    field (``change_set``, ``triage_result``, ...) holds the artifact for
    this role/purpose. Shared by every ``AgentRuntime`` (Copilot, pi) so this
    mapping lives in one place instead of being duplicated per runtime.
    """

    result_field = artifact_spec(role, purpose).result_field
    return AgentResult.model_validate(
        {
            "role": role,
            "success": True,
            "usage": usage,
            "performance": performance,
            result_field: artifact,
        }
    )
