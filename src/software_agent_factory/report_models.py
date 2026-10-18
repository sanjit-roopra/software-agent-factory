"""Change set, verification, test, review and CI report models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from software_agent_factory.escalation_models import ReviewFindingDisposition, ReviewFindingDraft
from software_agent_factory.model_base import (
    MAX_OPEN_REVIEW_FINDINGS,
    AttemptTrigger,
    ModelBase,
    NonBlankStr,
    ReviewImpasseKind,
    UtcDateTime,
    VersionedModel,
    utc_now,
)


class ChangeSet(VersionedModel):
    summary: NonBlankStr = Field(min_length=1)
    changed_files: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)


class CommandResult(ModelBase):
    command: str = Field(min_length=1)
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = Field(ge=0.0)
    timed_out: bool = False


class VerificationReport(VersionedModel):
    """Deterministic, factory-produced evidence only.

    Every field here is derived from executing repository-configured
    commands (``verification.DeterministicVerifier``); nothing in this model
    is an agent claim. Independent AI tester judgement lives in
    :class:`TestReport`.
    """

    passed: bool
    deterministic_checks: list[CommandResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    coverage_change: float | None = None
    test_findings: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class TestReport(VersionedModel):
    """Independent AI tester output.

    Kept distinct from :class:`VerificationReport` so a model's judgement is
    never mistaken for deterministic evidence ("a model does not approve its
    own work"). ``passed`` is advisory: the workflow controller still relies
    on the deterministic report for gating.
    """

    passed: bool
    findings: list[NonBlankStr] = Field(default_factory=list)
    suggested_tests: list[NonBlankStr] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    skipped: bool = False
    provenance: Literal["AGENT", "SKIPPED"] = "AGENT"
    skip_reason: str | None = None

    # Not a pytest test class despite the ``Test`` prefix.
    __test__ = False


class RepairContext(ModelBase):
    """Minimal, bounded context handed to a repair attempt.

    Deliberately small: only why the repair is happening plus the relevant
    excerpt, so historical failure context is never dumped wholesale into a
    prompt (``PLAN.md`` Phase 12). The *current* diff is not carried here --
    it travels as controller-derived Git evidence on ``AgentRequest.diff``.
    ``previous_attempts`` holds at most three one-line failure signatures from
    earlier attempts of this run, so a stateless runtime can see it is looping.
    """

    trigger: AttemptTrigger
    summary: str = Field(min_length=1)
    failures: list[str] = Field(default_factory=list)
    log_excerpt: str | None = None
    previous_attempts: list[str] = Field(default_factory=list, max_length=3)


class ReviewReport(VersionedModel):
    approved: bool
    findings: list[NonBlankStr] = Field(default_factory=list)
    scope_concerns: list[NonBlankStr] = Field(default_factory=list)
    security_concerns: list[NonBlankStr] = Field(default_factory=list)
    compatibility_concerns: list[NonBlankStr] = Field(default_factory=list)
    suggested_changes: list[NonBlankStr] = Field(default_factory=list)
    blocking_findings: list[ReviewFindingDraft] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    prior_finding_dispositions: list[ReviewFindingDisposition] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    repair_regressions: list[ReviewFindingDraft] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    skipped: bool = False
    provenance: Literal["AGENT", "SKIPPED"] = "AGENT"
    skip_reason: str | None = None


class ReviewImpasse(VersionedModel):
    snapshot: int = Field(ge=1)
    kind: ReviewImpasseKind = ReviewImpasseKind.UNKNOWN
    reason: str = Field(min_length=1)
    paths: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class CICheckEvidence(ModelBase):
    """One normalized CI check outcome.

    Deliberately expressed with plain strings rather than the enums declared
    in :mod:`software_agent_factory.github`: this module is the domain layer
    and must not depend on the ``gh`` adapter. The controller normalizes a
    ``github.CIStatus`` into this shape before persisting it.
    """

    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    description: str = ""
    details_url: str = ""
    failure_category: str | None = None
    log_excerpt: str = ""


class CIReport(VersionedModel):
    """Persisted, normalized CI evidence for one observation cycle.

    Controller-produced deterministic evidence (``PLAN.md`` Phase 11): it
    records what GitHub Actions reported, never a model's opinion of it.
    """

    overall: str = Field(min_length=1)
    checks: list[CICheckEvidence] = Field(default_factory=list)
    observed_at: UtcDateTime = Field(default_factory=utc_now)
    repair_attempts_used: int = Field(ge=0, default=0)
    timed_out: bool = False

    @property
    def failed_checks(self) -> list[CICheckEvidence]:
        return [check for check in self.checks if check.status == "FAIL"]
