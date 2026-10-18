"""Builders for the repair and correction text the controller sends to agents.

Every function here is pure: it turns stored evidence into a
:class:`~software_agent_factory.models.RepairContext` or a correction string.
The controller in :mod:`software_agent_factory.workflow` decides when to call
them; this module never reads or changes a run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from .governance import RepositoryVerificationResult
from .models import (
    AttemptRecord,
    AttemptTrigger,
    CIReport,
    ExecutionPlan,
    PlanDecisionAnswers,
    RepairContext,
    ReviewFinding,
    TestReport,
)
from .workspace import WorkspaceEvidence

#: Bound on how much failure text is copied into a repair prompt.
MAX_REPAIR_EXCERPT_CHARS = 4000
MAX_REPAIR_FAILURES = 10

PLANNING_RESULT_REQUEST = (
    "Return a complete PlanningResult JSON object with the specification and the execution plan."
)

_NO_MEMORY_TRIGGERS = frozenset({AttemptTrigger.POLISH, AttemptTrigger.OPERATOR})
_ERROR_LINE = re.compile(r"error|fail|assert|exception", re.IGNORECASE)


def planner_clarification_context(unresolved_decisions: Sequence[str]) -> str:
    decisions_list = "\n".join(f"- {decision}" for decision in unresolved_decisions)
    return (
        "The previous execution plan listed these unresolved decisions:\n"
        f"{decisions_list}\n\n"
        "Resolve any item that repository evidence or existing constraints answer. "
        "Retain only genuinely human-owned choices. " + PLANNING_RESULT_REQUEST
    )


def planner_human_decision_context(
    previous_plan: ExecutionPlan,
    decision_answers: PlanDecisionAnswers,
) -> str:
    """Format only durable, validated answers for the replacement planning call."""
    decisions = [
        {
            "decision_number": answer.decision_number,
            "question": previous_plan.unresolved_decisions[answer.decision_number - 1],
            "answer": answer.answer,
        }
        for answer in decision_answers.answers
    ]
    return (
        "An authorized human resolved these previous plan decisions. "
        "Treat each answer as a hard planning constraint. Do not change scope, policy, "
        "budgets, or delivery settings.\n\n"
        f"{json.dumps(decisions, ensure_ascii=True)}\n\n" + PLANNING_RESULT_REQUEST
    )


def typed_artifact_repair_context(
    failure_reason: str,
    artifact_name: str,
    prior_context: RepairContext | str | None = None,
) -> str:
    validation_error = failure_reason.split(" stdout=", 1)[0].strip()
    correction = (
        "Your previous response failed deterministic schema validation.\n"
        f"{validation_error}\n"
        f"Correct only the output shape. Return one complete {artifact_name} JSON object. "
        "Do not add markdown or text outside the JSON."
    )
    if prior_context is None:
        return correction
    if isinstance(prior_context, str):
        return f"{prior_context}\n\n{correction}"
    return f"{prior_context.model_dump_json()}\n\n{correction}"


def review_contract_repair_context(failure_reason: str) -> str:
    return (
        "Your previous ReviewReport passed JSON schema validation but violated the deterministic "
        f"review contract. Contract error: {failure_reason}. Correct the complete ReviewReport "
        "using the typed blocker, disposition, and regression fields exactly as instructed. "
        "Do not change repository files."
    )


def implementer_failure_context(last: AttemptRecord) -> RepairContext:
    if last.triggered_by is AttemptTrigger.POLISH:
        return RepairContext(
            trigger=AttemptTrigger.IMPLEMENTER_FAILURE,
            summary=(
                "The optional polish attempt did not complete. Restore or preserve "
                "the last verified behavior and resolve any partial polish edits."
            ),
            failures=[last.failure_reason or "polish implementer reported failure"],
            log_excerpt=None,
        )
    return RepairContext(
        trigger=AttemptTrigger.IMPLEMENTER_FAILURE,
        summary=(
            "The previous implementation attempt did not complete. The working tree "
            "may contain partial, unverified edits from that attempt; inspect and "
            "reconcile them before continuing."
        ),
        failures=[last.failure_reason or "implementer reported failure"],
        log_excerpt=None,
    )


def polish_context() -> RepairContext:
    return RepairContext(
        trigger=AttemptTrigger.POLISH,
        summary=(
            "Deterministic verification passed. Apply a final bounded polish and "
            "simplification pass using the fixed simplify and polish guidance supplied "
            "with this request. Simplify first, then polish. "
            "Preserve required behavior, public interfaces, scope, dependencies, "
            "security checks, and verification policy. Make no edit when no safe "
            "improvement exists."
        ),
        failures=[],
        log_excerpt=None,
    )


def verification_repair_context(verification: RepositoryVerificationResult) -> RepairContext:
    report = verification.report
    failed = [
        check for check in report.deterministic_checks if check.timed_out or check.exit_code != 0
    ]
    excerpt = None
    if failed:
        last = failed[-1]
        excerpt = bounded(f"$ {last.command}\n{last.stdout}\n{last.stderr}")
    phase = verification.failed_phase.value if verification.failed_phase else "verify"
    kind = verification.failure_kind.value if verification.failure_kind else "unknown"
    return RepairContext(
        trigger=AttemptTrigger.VERIFICATION,
        summary=f"Deterministic {phase} failed ({kind} failure).",
        failures=list(report.failures)[:MAX_REPAIR_FAILURES],
        log_excerpt=excerpt,
    )


def verification_mutation_repair_context(
    before: WorkspaceEvidence,
    after: WorkspaceEvidence,
) -> RepairContext:
    before_files = set(before.changed_files)
    after_files = set(after.changed_files)
    added = sorted(after_files - before_files)
    removed = sorted(before_files - after_files)
    details = [
        "Repository verification commands changed the Git tree after implementation "
        "evidence was captured. Verification must leave source contents unchanged. "
        "Remove generated artifacts from the worktree and Git index, then add appropriate "
        "ignore rules so verification cannot stage them again."
    ]
    if added:
        details.append(f"Newly tracked after verification: {', '.join(added)}")
    if removed:
        details.append(f"No longer tracked after verification: {', '.join(removed)}")
    if not added and not removed:
        details.append("One or more already-changed files were modified during verification.")
    return RepairContext(
        trigger=AttemptTrigger.VERIFICATION,
        summary="Deterministic verification modified the repository.",
        failures=details[:MAX_REPAIR_FAILURES],
        log_excerpt=None,
    )


def verification_artifact_repair_context(retained_paths: list[str]) -> RepairContext:
    return RepairContext(
        trigger=AttemptTrigger.VERIFICATION,
        summary="Verification-generated artifacts are still part of the proposed change.",
        failures=[
            "Remove these generated paths from the worktree and Git index before retrying: "
            + ", ".join(retained_paths)
        ],
        log_excerpt=None,
    )


def review_repair_context(
    findings: list[ReviewFinding],
    test_report: TestReport,
) -> RepairContext:
    excerpt = None
    if test_report.findings:
        excerpt = bounded("\n".join(test_report.findings))
    return RepairContext(
        trigger=AttemptTrigger.REVIEW,
        summary="The independent reviewer rejected the change.",
        failures=[
            f"[{finding.id}] {finding.message}" for finding in findings[:MAX_REPAIR_FAILURES]
        ],
        log_excerpt=excerpt,
    )


def ci_repair_context(report: CIReport) -> RepairContext:
    failed = report.failed_checks
    excerpts = [
        check.log_excerpt or check.description
        for check in failed
        if check.log_excerpt or check.description
    ]
    return RepairContext(
        trigger=AttemptTrigger.CI,
        summary="Continuous integration reported a failing check.",
        failures=[f"{check.name}: {check.failure_category or 'UNKNOWN'}" for check in failed][
            :MAX_REPAIR_FAILURES
        ],
        log_excerpt=bounded("\n\n".join(excerpts)) if excerpts else None,
    )


def _repair_signature(repair_context: RepairContext) -> str:
    """One line naming a failure: the first failure plus the first error line of its log.

    Digits are masked so line numbers, counts and durations do not hide a repeat.
    """
    head = repair_context.failures[0] if repair_context.failures else repair_context.summary
    lines = (repair_context.log_excerpt or "").splitlines()
    error = next((line for line in lines if _ERROR_LINE.search(line)), "")
    text = " | ".join(part for part in (head, error) if part)
    return re.sub(r"\d+", "#", " ".join(text.split()))[:200]


def with_repair_memory(
    earlier: list[tuple[int, str]],
    repair_context: RepairContext | None,
    failed_attempt: int,
) -> RepairContext | None:
    """Add earlier failure signatures, and a fixed warning when this failure repeats one."""
    if repair_context is None or repair_context.trigger in _NO_MEMORY_TRIGGERS:
        return repair_context
    signature = _repair_signature(repair_context)
    repeat = next((n for n, seen in reversed(earlier) if seen == signature), None)
    summary = repair_context.summary
    if repeat is not None:
        summary = f"Same failure as attempt {repeat}; try a different approach. {summary}"
    previous = [f"attempt {n}: {seen}" for n, seen in earlier[-3:]]
    earlier.append((failed_attempt, signature))
    return repair_context.model_copy(update={"summary": summary, "previous_attempts": previous})


def bounded(text: str, limit: int = MAX_REPAIR_EXCERPT_CHARS) -> str:
    """Keep only the tail of a failure excerpt: the end explains the failure."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[-limit:]
