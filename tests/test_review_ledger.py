"""The shared review policy predicate used at acceptance and at delivery."""

from __future__ import annotations

import pytest

from software_agent_factory import review_ledger
from software_agent_factory.config import ReviewConfig
from software_agent_factory.models import (
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingDraft,
    ReviewFindingOrigin,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
)


def _draft(
    category: ReviewFindingCategory = ReviewFindingCategory.CORRECTNESS,
) -> ReviewFindingDraft:
    return ReviewFindingDraft(
        category=category,
        message="needs a fix",
        locations=[ReviewSourceLocation(path="a.py", start_line=1, end_line=1)],
    )


def _finding(
    category: ReviewFindingCategory = ReviewFindingCategory.CORRECTNESS,
    origin: ReviewFindingOrigin = ReviewFindingOrigin.INITIAL,
) -> list[ReviewFinding]:
    return review_ledger.mint_findings([_draft(category)], origin, snapshot=1)


def test_within_policy_accepts_an_eligible_finding() -> None:
    assert review_ledger.within_policy(
        ReviewConfig(), Risk.R0, _finding(), ReviewReport(approved=False)
    )


@pytest.mark.parametrize(
    ("risk", "findings", "review"),
    [
        (Risk.R3, _finding(), ReviewReport(approved=False)),
        (Risk.R0, [], ReviewReport(approved=False)),
        (Risk.R0, _finding(ReviewFindingCategory.SECURITY), ReviewReport(approved=False)),
        (Risk.R0, _finding(), ReviewReport(approved=False, repair_regressions=[_draft()])),
        (
            Risk.R0,
            _finding(),
            ReviewReport(approved=False, blocking_findings=[_draft(ReviewFindingCategory.SCOPE)]),
        ),
    ],
)
def test_within_policy_rejects_each_policy_breach(
    risk: Risk, findings: list[ReviewFinding], review: ReviewReport
) -> None:
    assert not review_ledger.within_policy(ReviewConfig(), risk, findings, review)


def test_repair_regressions_are_not_acceptable_origins() -> None:
    assert review_ledger.has_acceptable_origins(_finding(origin=ReviewFindingOrigin.LATE_ADOPTED))
    assert not review_ledger.has_acceptable_origins(
        _finding(origin=ReviewFindingOrigin.REPAIR_REGRESSION_DIRECT)
    )
