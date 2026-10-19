"""A run's raw evidence for the dashboard: checks, review rounds, plan and diff (ADR-047).

Read-only, like :func:`~software_agent_factory.observability.build_run_detail`.
The text is raw: command output, CI log excerpts and reviewer findings can quote
repository content and secrets, so the dashboard sanitizer must redact and cut it
before it reaches a client.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .governance import paths_outside_scope
from .models import (
    ChangeSet,
    CIReport,
    ExecutionPlan,
    FactoryRun,
    ModelBase,
    ReviewReport,
    Specification,
    TestReport,
    VerificationReport,
)
from .observability import RunStoreProtocol, _load_optional_artifact
from .review_ledger import finding_id

#: The most attempts the verification and CI histories keep; older ones are dropped.
HISTORY_LIMIT = 10

__all__ = [
    "HISTORY_LIMIT",
    "PatchStoreProtocol",
    "build_run_diff",
    "build_run_evidence",
]


def build_run_evidence(store: RunStoreProtocol, run_id: str) -> dict[str, Any] | None:
    """One run's check output and review text, or ``None`` if the run is not readable.

    The checks are the latest verification and CI reports, plus the per-attempt
    history of each and the tester's verdict per attempt (the last
    :data:`HISTORY_LIMIT` attempts that saved one). The
    review has one round per attempt that saved a review, each finding with its
    ledger id, plus the open and accepted findings of the review ledger. The plan
    part is the latest saved specification, execution plan and change set, each
    ``None`` when absent, and the changed files outside the plan's modules.
    """
    try:
        run = store.load_run(run_id)
    except (OSError, ValueError):
        return None
    verification = _load_optional_artifact(store, run.id, VerificationReport)
    ci = _load_optional_artifact(store, run.id, CIReport)
    ledger = run.review_ledger
    plan = _load_optional_artifact(store, run.id, ExecutionPlan)
    change_set = _load_optional_artifact(store, run.id, ChangeSet)
    return {
        "run_id": run.id,
        "branch_name": run.branch_name,
        "checks": [] if verification is None else _dumped(verification.deterministic_checks),
        "ci_checks": [] if ci is None else _dumped(ci.checks),
        "verification_history": [
            {"attempt": number, "checks": _dumped(report.deterministic_checks)}
            for number, report in _history(store, run, VerificationReport)
        ],
        "ci_history": [
            {"attempt": number, "overall": report.overall, "checks": _dumped(report.checks)}
            for number, report in _history(store, run, CIReport)
        ],
        "tester_history": [
            {"attempt": number, "passed": report.passed, "skipped": report.skipped}
            for number, report in _history(store, run, TestReport)
        ],
        "review_rounds": [
            _review_round(number, review) for number, review in _history(store, run, ReviewReport)
        ],
        "open_findings": _dumped(ledger.open_findings),
        "accepted_findings": _dumped(ledger.accepted_findings),
        "specification": _dumped_optional(store, run.id, Specification),
        "execution_plan": None if plan is None else plan.model_dump(mode="json"),
        "change_set": None if change_set is None else change_set.model_dump(mode="json"),
        "out_of_scope_paths": (
            []
            if plan is None or change_set is None
            else list(paths_outside_scope(plan.expected_scope.modules, change_set.changed_files))
        ),
    }


def _history(store: RunStoreProtocol, run: FactoryRun, artifact_type: type[Any]) -> list[Any]:
    """``(attempt, report)`` for each attempt that saved ``artifact_type``, oldest first.

    ``attempts/NN`` is keyed by the run-global index, not the per-budget attempt_number.
    Review rounds are all kept; the check histories keep the last :data:`HISTORY_LIMIT`.
    """
    found = []
    for number in range(1, len(run.attempt_records) + 1):
        report = _load_optional_artifact(store, run.id, artifact_type, attempt=number)
        if report is not None:
            found.append((number, report))
    return found if artifact_type is ReviewReport else found[-HISTORY_LIMIT:]


def _review_round(number: int, review: ReviewReport) -> dict[str, Any]:
    """One review round, each new finding with the id the ledger gives it, so the
    page can follow a finding across rounds by the dispositions that name it."""
    data = review.model_dump(mode="json")
    for key, drafts in (
        ("blocking_findings", review.blocking_findings),
        ("repair_regressions", review.repair_regressions),
    ):
        data[key] = [{"id": finding_id(draft), **draft.model_dump(mode="json")} for draft in drafts]
    return {"attempt_number": number, **data}


class PatchStoreProtocol(RunStoreProtocol, Protocol):
    """A run store that can also read a saved ``patch.diff`` (ADR-047)."""

    def load_patch(
        self, run_id: str, filename: str = "patch.diff", *, attempt: int | None = None
    ) -> str: ...


def build_run_diff(
    store: PatchStoreProtocol, run_id: str, attempt: int | None = None
) -> dict[str, Any] | None:
    """One run's saved patch, raw, or ``None`` if the run is not readable.

    ``attempt`` picks a per-attempt copy by its run-global index; ``None`` is the
    latest patch. ``attempts`` lists every index with a saved patch, for the
    page's attempt picker. A missing patch is ``patch: None``. The text can quote
    secrets, so the dashboard sanitizer must redact and cut it (ADR-047).
    """
    try:
        run = store.load_run(run_id)
    except (OSError, ValueError):
        return None
    patches: dict[int | None, str] = {}
    for number in [None, *range(1, len(run.attempt_records) + 1)]:
        try:
            patches[number] = store.load_patch(run.id, attempt=number)
        except (OSError, ValueError):
            continue
    return {
        "run_id": run.id,
        "attempt": attempt,
        "attempts": [number for number in patches if number is not None],
        "patch": patches.get(attempt),
    }


def _dumped_optional(
    store: RunStoreProtocol, run_id: str, artifact_type: type[ModelBase]
) -> dict[str, Any] | None:
    artifact = _load_optional_artifact(store, run_id, artifact_type)
    return None if artifact is None else artifact.model_dump(mode="json")


def _dumped(models: Sequence[ModelBase]) -> list[dict[str, Any]]:
    return [model.model_dump(mode="json") for model in models]
