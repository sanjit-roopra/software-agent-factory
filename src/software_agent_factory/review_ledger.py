"""Review ledger rules: which findings stay open, when review stops converging,
and which findings the review policy lets a run accept.

Every function here is pure: it takes the stored ledger and a review report and
returns new values. The controller in :mod:`software_agent_factory.workflow`
saves them and decides what to do next; this module never reads or changes a run.
"""

from __future__ import annotations

import hashlib
import re

from .config import ReviewConfig
from .models import (
    MAX_GUIDANCE_FINDINGS,
    MAX_OPEN_REVIEW_FINDINGS,
    ReviewDispositionStatus,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingDraft,
    ReviewFindingOrigin,
    ReviewImpasse,
    ReviewImpasseKind,
    ReviewLedger,
    ReviewReport,
    ReviewSourceLocation,
    Risk,
)

MAX_LATE_REVIEW_ADOPTION_ROUNDS = 1
MAX_CONSECUTIVE_BLOCKING_REVIEWS_PER_PATH = 3
MAX_CONSECUTIVE_UNRESOLVED_REVIEWS = 3
MAX_CONSECUTIVE_REPLACEMENT_REVIEWS = 2
#: Origins an attended run may accept; repair regressions always need a person.
ACCEPTABLE_ORIGINS = frozenset({ReviewFindingOrigin.INITIAL, ReviewFindingOrigin.LATE_ADOPTED})

_DIFF_HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def apply_review(
    ledger: ReviewLedger,
    review: ReviewReport,
    *,
    snapshot: int,
    tree_sha: str,
    repair_diff: str | None,
    blocked_categories: list[ReviewFindingCategory],
) -> tuple[ReviewLedger, ReviewReport, ReviewImpasse | None]:
    """Fold one review into the ledger.

    Returns the new ledger, the review with ``approved`` set from the open
    findings, and the impasse when review stopped converging.
    """
    if not ledger.open_findings:
        return _apply_first_review(ledger, review, snapshot, tree_sha)
    return _apply_repair_review(ledger, review, snapshot, tree_sha, repair_diff, blocked_categories)


def _apply_first_review(
    ledger: ReviewLedger,
    review: ReviewReport,
    snapshot: int,
    tree_sha: str,
) -> tuple[ReviewLedger, ReviewReport, ReviewImpasse | None]:
    new_drafts = [
        draft
        for draft in review.blocking_findings
        if not any(_draft_matches_finding(draft, finding) for finding in ledger.accepted_findings)
    ]
    open_findings = mint_findings(new_drafts, ReviewFindingOrigin.INITIAL, snapshot)
    ledger = ledger.model_copy(
        update={
            "open_findings": open_findings,
            "last_reviewed_tree_sha": tree_sha if open_findings else None,
            "late_adoption_rounds": 0,
            "consecutive_replacement_rounds": 0,
            "path_streaks": dict.fromkeys(finding_paths(open_findings), 1),
            "unresolved_streaks": {finding.id: 1 for finding in open_findings},
        }
    )
    return ledger, review.model_copy(update={"approved": not open_findings}), None


def _apply_repair_review(
    ledger: ReviewLedger,
    review: ReviewReport,
    snapshot: int,
    tree_sha: str,
    repair_diff: str | None,
    blocked_categories: list[ReviewFindingCategory],
) -> tuple[ReviewLedger, ReviewReport, ReviewImpasse | None]:
    prior = list(ledger.open_findings)
    dispositions = {
        disposition.finding_id: disposition for disposition in review.prior_finding_dispositions
    }
    unresolved_ids = {
        finding.id
        for finding in prior
        if dispositions[finding.id].status is ReviewDispositionStatus.UNRESOLVED
    }
    prior_ids = {finding.id for finding in prior}

    regression_overlaps, regression_drafts = _partition_drafts(prior, review.repair_regressions)
    latent_overlaps, latent_drafts = _partition_drafts(prior, review.blocking_findings)
    unresolved_ids.update(regression_overlaps)
    unresolved_ids.update(latent_overlaps)

    changed_ranges = _changed_line_ranges(repair_diff or "")
    regressions: list[ReviewFinding] = []
    for draft in regression_drafts:
        origin = (
            ReviewFindingOrigin.REPAIR_REGRESSION_DIRECT
            if _draft_intersects_ranges(draft, changed_ranges)
            else ReviewFindingOrigin.REPAIR_REGRESSION_INDIRECT
        )
        regressions.extend(mint_findings([draft], origin, snapshot))

    safety_late_drafts = [draft for draft in latent_drafts if draft.category in blocked_categories]
    adopt_late = bool(latent_drafts) and (
        ledger.late_adoption_rounds < MAX_LATE_REVIEW_ADOPTION_ROUNDS
    )
    adopted_late = mint_findings(
        latent_drafts if adopt_late else safety_late_drafts,
        ReviewFindingOrigin.LATE_ADOPTED,
        snapshot,
    )
    unresolved = [finding for finding in prior if finding.id in unresolved_ids]
    current = deduplicate_findings([*unresolved, *regressions, *adopted_late])
    too_many_findings = len(current) > MAX_OPEN_REVIEW_FINDINGS
    if too_many_findings:
        current = current[:MAX_OPEN_REVIEW_FINDINGS]

    ledger = ledger.model_copy(
        update={
            "open_findings": current,
            "last_reviewed_tree_sha": tree_sha if current else None,
            "late_adoption_rounds": ledger.late_adoption_rounds + int(adopt_late),
            "consecutive_replacement_rounds": (
                ledger.consecutive_replacement_rounds + 1
                if not unresolved and bool(regressions or adopted_late)
                else 0
            ),
            "path_streaks": {
                path: ledger.path_streaks.get(path, 0) + 1 for path in finding_paths(current)
            },
            "unresolved_streaks": {
                finding.id: (
                    ledger.unresolved_streaks.get(finding.id, 0) + 1
                    if finding.id in prior_ids
                    else 1
                )
                for finding in current
            },
        }
    )
    suggestions = list(review.suggested_changes)
    advisory_late_drafts = [draft for draft in latent_drafts if draft not in safety_late_drafts]
    if advisory_late_drafts and not adopt_late:
        suggestions.extend(
            f"Late review finding left advisory after the bounded adoption round: {draft.message}"
            for draft in advisory_late_drafts
        )
    effective = review.model_copy(
        update={"approved": not current, "suggested_changes": suggestions}
    )
    return ledger, effective, _convergence_impasse(ledger, snapshot, too_many_findings)


def _convergence_impasse(
    ledger: ReviewLedger,
    snapshot: int,
    too_many_findings: bool,
) -> ReviewImpasse | None:
    if too_many_findings:
        return impasse(
            ledger,
            snapshot,
            f"more than {MAX_OPEN_REVIEW_FINDINGS} blockers remained open",
            kind=ReviewImpasseKind.TOO_MANY_FINDINGS,
        )
    path_limit = sorted(
        path
        for path, count in ledger.path_streaks.items()
        if count >= MAX_CONSECUTIVE_BLOCKING_REVIEWS_PER_PATH
    )
    if path_limit:
        return impasse(
            ledger,
            snapshot,
            "review blockers kept returning on the same path(s): " + ", ".join(path_limit),
            kind=ReviewImpasseKind.REPEATED_PATH,
        )
    unresolved_limit = sorted(
        finding_id
        for finding_id, count in ledger.unresolved_streaks.items()
        if count >= MAX_CONSECUTIVE_UNRESOLVED_REVIEWS
    )
    if unresolved_limit:
        return impasse(
            ledger,
            snapshot,
            "review findings remained unresolved across repeated repairs: "
            + ", ".join(unresolved_limit),
            kind=ReviewImpasseKind.REPEATED_FINDING,
        )
    if ledger.consecutive_replacement_rounds >= MAX_CONSECUTIVE_REPLACEMENT_REVIEWS:
        return impasse(
            ledger,
            snapshot,
            "consecutive repairs replaced every previous blocker with new blockers",
            kind=ReviewImpasseKind.REPLACEMENT_LOOP,
        )
    return None


def impasse(
    ledger: ReviewLedger,
    snapshot: int,
    summary: str,
    *,
    kind: ReviewImpasseKind,
) -> ReviewImpasse:
    """Describe why review stopped, naming the open findings it stopped on."""
    findings = ledger.open_findings[:MAX_GUIDANCE_FINDINGS]
    paths = sorted(finding_paths(findings))
    reason = f"review failed to converge at snapshot {snapshot}: {summary}"
    if paths:
        reason += "; blocking paths: " + ", ".join(paths)
    return ReviewImpasse(
        snapshot=snapshot,
        kind=kind,
        reason=reason,
        paths=paths,
        finding_ids=[finding.id for finding in findings],
        findings=[f"[{finding.id}] {finding.message}" for finding in findings],
    )


def clear_open_findings(ledger: ReviewLedger) -> ReviewLedger:
    """Drop the open findings and their streaks once review has passed."""
    return ledger.model_copy(
        update={
            "open_findings": [],
            "last_reviewed_tree_sha": None,
            "consecutive_replacement_rounds": 0,
            "path_streaks": {},
            "unresolved_streaks": {},
        }
    )


def within_policy(
    policy: ReviewConfig,
    risk: Risk,
    findings: list[ReviewFinding],
    review: ReviewReport,
) -> bool:
    """Whether the configured review policy allows an attended run to accept
    ``findings`` at ``risk``. Callers add their own origin or risk checks."""
    return (
        risk in policy.accepted_risks
        and 0 < len(findings) <= policy.max_accepted_findings
        and not review.repair_regressions
        and not any(
            finding.category in policy.blocked_categories
            for finding in [*findings, *review.blocking_findings]
        )
    )


def has_acceptable_origins(findings: list[ReviewFinding]) -> bool:
    return all(finding.origin in ACCEPTABLE_ORIGINS for finding in findings)


def mint_findings(
    drafts: list[ReviewFindingDraft],
    origin: ReviewFindingOrigin,
    snapshot: int,
) -> list[ReviewFinding]:
    findings = [
        ReviewFinding(
            id=_finding_id(draft),
            category=draft.category,
            message=draft.message,
            locations=draft.locations,
            origin=origin,
            first_seen_snapshot=snapshot,
        )
        for draft in drafts
    ]
    return deduplicate_findings(findings)


def deduplicate_findings(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    deduplicated: dict[str, ReviewFinding] = {}
    for finding in findings:
        deduplicated.setdefault(finding.id, finding)
    return list(deduplicated.values())


def finding_paths(findings: list[ReviewFinding]) -> set[str]:
    return {location.path for finding in findings for location in finding.locations}


def _finding_id(draft: ReviewFindingDraft) -> str:
    locations = ",".join(
        f"{location.path}:{location.start_line}-{location.end_line}"
        for location in sorted(
            draft.locations,
            key=lambda item: (item.path, item.start_line, item.end_line),
        )
    )
    digest = hashlib.sha256(f"{draft.category.value}:{locations}".encode("utf-8")).hexdigest()[:16]
    return f"review-{draft.category.value.lower()}-{digest}"


def _locations_overlap(left: ReviewSourceLocation, right: ReviewSourceLocation) -> bool:
    return (
        left.path == right.path
        and left.start_line <= right.end_line
        and right.start_line <= left.end_line
    )


def _draft_overlaps_finding(draft: ReviewFindingDraft, finding: ReviewFinding) -> bool:
    return any(
        _locations_overlap(draft_location, finding_location)
        for draft_location in draft.locations
        for finding_location in finding.locations
    )


def _draft_matches_finding(draft: ReviewFindingDraft, finding: ReviewFinding) -> bool:
    return (
        draft.category is finding.category
        and draft.message == finding.message
        and draft.locations == finding.locations
    )


def _partition_drafts(
    prior: list[ReviewFinding],
    drafts: list[ReviewFindingDraft],
) -> tuple[set[str], list[ReviewFindingDraft]]:
    overlapping_ids: set[str] = set()
    new_drafts: list[ReviewFindingDraft] = []
    for draft in drafts:
        overlapping = {finding.id for finding in prior if _draft_overlaps_finding(draft, finding)}
        if overlapping:
            overlapping_ids.update(overlapping)
        else:
            new_drafts.append(draft)
    return overlapping_ids, new_drafts


def _changed_line_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            ranges.setdefault(current_path, [])
            continue
        if current_path is None:
            continue
        match = _DIFF_HUNK_PATTERN.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        end = start + max(count, 1) - 1
        ranges[current_path].append((start, end))
    return ranges


def _draft_intersects_ranges(
    draft: ReviewFindingDraft,
    changed_ranges: dict[str, list[tuple[int, int]]],
) -> bool:
    return any(
        location.start_line <= end and start <= location.end_line
        for location in draft.locations
        for start, end in changed_ranges.get(location.path, [])
    )
