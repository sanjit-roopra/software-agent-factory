"""Review ledger, halt reason, reply and escalation models."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from software_agent_factory.model_base import (
    MAX_OPEN_REVIEW_FINDINGS,
    Complexity,
    ExecutionRoute,
    ModelBase,
    NonBlankStr,
    ReviewAcceptanceReason,
    ReviewDispositionStatus,
    ReviewFindingCategory,
    ReviewFindingOrigin,
    Risk,
    UtcDateTime,
    VersionedModel,
    WorkflowState,
    utc_now,
)

if TYPE_CHECKING:
    from software_agent_factory.models import ExecutionPlan


class ReviewSourceLocation(ModelBase):
    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if (
            not value
            or value == "."
            or "\0" in value
            or "\n" in value
            or "\r" in value
            or "\\" in value
        ):
            raise ValueError("path must be a normalized repository-relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or value != path.as_posix() or ".." in path.parts:
            raise ValueError("path must be a normalized repository-relative path")
        return value

    @model_validator(mode="after")
    def _validate_lines(self) -> ReviewSourceLocation:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ReviewFindingDraft(ModelBase):
    category: ReviewFindingCategory
    message: NonBlankStr = Field(min_length=1, max_length=1000)
    locations: list[ReviewSourceLocation] = Field(min_length=1, max_length=8)


class ReviewFinding(ReviewFindingDraft):
    id: str = Field(min_length=1, max_length=80)
    origin: ReviewFindingOrigin
    first_seen_snapshot: int = Field(ge=1)


class ReviewFindingDisposition(ModelBase):
    finding_id: str = Field(min_length=1, max_length=80)
    status: ReviewDispositionStatus
    rationale: NonBlankStr = Field(min_length=1, max_length=1000)


class ReviewLedger(ModelBase):
    open_findings: list[ReviewFinding] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    accepted_findings: list[ReviewFinding] = Field(
        default_factory=list,
        max_length=MAX_OPEN_REVIEW_FINDINGS,
    )
    last_reviewed_tree_sha: str | None = None
    late_adoption_rounds: int = Field(default=0, ge=0)
    consecutive_replacement_rounds: int = Field(default=0, ge=0)
    path_streaks: dict[str, int] = Field(default_factory=dict)
    unresolved_streaks: dict[str, int] = Field(default_factory=dict)


class ReviewAcceptance(VersionedModel):
    snapshot: int = Field(ge=1)
    reason: ReviewAcceptanceReason
    risk: Risk
    review_rounds: int = Field(ge=1)
    reviewed_tree_sha: str = Field(min_length=40, max_length=64)
    findings: list[ReviewFinding] = Field(min_length=1, max_length=MAX_OPEN_REVIEW_FINDINGS)


class EscalationStatus(StrEnum):
    PENDING_NOTIFICATION = "PENDING_NOTIFICATION"
    NOTIFIED = "NOTIFIED"
    NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
    REOPENED = "REOPENED"
    RESUMED = "RESUMED"
    EXPIRED = "EXPIRED"


class ResumeClassification(StrEnum):
    RISK_APPROVAL = "RISK_APPROVAL"
    PLAN_DECISION = "PLAN_DECISION"
    DELIVERY_RETRY = "DELIVERY_RETRY"
    NOT_RESUMABLE = "NOT_RESUMABLE"


class HaltReasonCode(StrEnum):
    """Why a run stopped, or why its guidance reads as it does.

    The values are stored in run files and sent to the dashboard, so they never change.
    Every copy table keys on this enum: the escalation classifier, the run guidance,
    the dashboard guidance copy and the dashboard halt sentences.
    """

    BOUNDED_REVIEW_ACCEPTANCE = "BOUNDED_REVIEW_ACCEPTANCE"
    REVIEW_IMPASSE = "REVIEW_IMPASSE"
    UNRESOLVED_DECISIONS = "UNRESOLVED_DECISIONS"
    RISK_APPROVAL = "RISK_APPROVAL"
    SCOPE_REVIEW = "SCOPE_REVIEW"
    ATTEMPT_BUDGET_EXHAUSTED = "ATTEMPT_BUDGET_EXHAUSTED"
    CI_INTERVENTION = "CI_INTERVENTION"
    DELIVERY_INTERVENTION = "DELIVERY_INTERVENTION"
    RECOVERY_INTERVENTION = "RECOVERY_INTERVENTION"
    MANUAL_INSPECTION = "MANUAL_INSPECTION"
    STALLED = "STALLED"

    @classmethod
    def parse(cls, value: object) -> HaltReasonCode | None:
        """The code a stored or sent value names, or ``None`` for anything else."""
        if not isinstance(value, str):
            return None
        try:
            return cls(value)
        except ValueError:
            return None


class HaltReasonCopy(NamedTuple):
    """The words for one halt reason: what happened and what a person does next."""

    summary: str
    next_action: str


#: The one copy source for halt reasons. The run guidance and the dashboard guidance read
#: it as it is. The GitHub notice reads it too, except the plan-decision next action, which
#: says "this GitHub thread" because the notice is posted there. For UNRESOLVED_DECISIONS
#: the summary is the form without a count and the next action is the one for a reply.
HALT_REASON_COPY: dict[HaltReasonCode, HaltReasonCopy] = {
    HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE: HaltReasonCopy(
        "The controller continued after the bounded review limit.",
        "Review the accepted findings in the pull request before merging.",
    ),
    HaltReasonCode.REVIEW_IMPASSE: HaltReasonCopy(
        "Independent review did not converge within the safe automatic policy.",
        "Inspect review-impasse.json, resolve or accept the listed findings, then retry.",
    ),
    HaltReasonCode.UNRESOLVED_DECISIONS: HaltReasonCopy(
        "The execution plan has unresolved architectural decisions.",
        "Reply with complete numbered decisions on the escalation thread.",
    ),
    HaltReasonCode.RISK_APPROVAL: HaltReasonCopy(
        "The run requires approval under the configured risk policy.",
        "Review the work item risk and approve or change the policy before retrying.",
    ),
    HaltReasonCode.SCOPE_REVIEW: HaltReasonCopy(
        "The proposed changes exceeded the approved scope.",
        "Review the planned and changed files, then update the scope or retry.",
    ),
    HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED: HaltReasonCopy(
        "The run exhausted a bounded retry budget.",
        "Inspect the run artifacts, correct the underlying issue, then retry.",
    ),
    HaltReasonCode.CI_INTERVENTION: HaltReasonCopy(
        "CI could not be completed or repaired automatically.",
        "Inspect the pull request checks, fix the failing check, then retry delivery.",
    ),
    HaltReasonCode.DELIVERY_INTERVENTION: HaltReasonCopy(
        "The controller could not complete pull request delivery.",
        "Check repository permissions and delivery settings, then retry delivery.",
    ),
    HaltReasonCode.RECOVERY_INTERVENTION: HaltReasonCopy(
        "The run could not safely recover its persisted workspace.",
        "Inspect the run and workspace metadata before starting a replacement run.",
    ),
    HaltReasonCode.MANUAL_INSPECTION: HaltReasonCopy(
        "The controller stopped at a manual decision boundary.",
        "Inspect the typed run artifacts and decide whether to retry or replace the run.",
    ),
    HaltReasonCode.STALLED: HaltReasonCopy(
        "The run made no progress within the stall timeout, so the factory stopped its agent.",
        "Inspect the last invocation and the workspace, then start a replacement run.",
    ),
}

#: The start of the halt reason of a run the service stopped because it made no progress.
STALLED_HALT_REASON = "stalled: no progress within the stall timeout"

#: The halt reason of a run that stopped on unresolved plan decisions. A count can follow
#: it, so the classifiers match it as a prefix.
UNRESOLVED_DECISIONS_HALT_REASON = "execution plan has unresolved decisions"

#: The start of the halt reason of a run whose publish step failed. The error follows it.
PUBLISH_FAILED_HALT_REASON = "could not publish the pull request:"

#: The next action when no reply can resume a run that stopped on unresolved decisions.
UNRESOLVED_DECISIONS_REPLACE_ACTION = (
    "Inspect execution-plan.json, resolve the decisions, then start a replacement run."
)


def unresolved_decisions_count(plan: ExecutionPlan | None, reason: str) -> int | None:
    """How many decisions a halt left open: the plan's own count, else a count in ``reason``."""
    if plan is not None and plan.unresolved_decisions:
        return len(plan.unresolved_decisions)
    match = re.search(r"\b(\d+)\s+unresolved", reason) or re.search(r"\((\d+)\)", reason)
    return int(match.group(1)) if match else None


def unresolved_decisions_summary(count: int | None) -> str:
    """The halt summary for a plan with ``count`` unresolved decisions, or none counted."""
    if count is None:
        return HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS].summary
    label = "decision" if count == 1 else "decisions"
    return f"The execution plan has {count} unresolved architectural {label}."


class EscalationTargetType(StrEnum):
    PULL_REQUEST = "PULL_REQUEST"
    ISSUE = "ISSUE"


DASHBOARD_USER_LOGIN = "dashboard-local"

#: The most numbered decisions one plan-decision reply can answer. Lives here so the
#: models below can bound their lists with it; ``escalation_protocol`` re-exports it.
MAX_PLAN_DECISIONS = 24

#: ``EscalationRecord.reply_cursor`` once no further reply can resume the run.
REPLY_CURSOR_CLOSED = "closed"

#: An episode id as a whole. Lives here so the request model can check it without
#: importing ``escalation_protocol``, which imports this module.
EPISODE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")

#: A context fingerprint is a full SHA-256 digest in lowercase hex.
CONTEXT_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")

ReplySource = Literal["github", "dashboard"]

#: How alive the call recorded as active looks. The run view reports it and the dashboard
#: shows it; the page names an outcome for each one.
ActiveInvocationStatus = Literal["running", "stale", "crashed", "abandoned"]


def _check_reply_source(source: ReplySource, comment_id: int | None, user_login: str) -> None:
    """Shared rule: a GitHub reply has a comment id, a dashboard reply has the fixed login."""
    if source == "github" and comment_id is None:
        raise ValueError("a github reply needs a comment_id")
    if source == "dashboard" and comment_id is not None:
        raise ValueError("a dashboard reply has no GitHub comment_id")
    if source == "dashboard" and user_login != DASHBOARD_USER_LOGIN:
        raise ValueError(f"a dashboard reply must use user_login {DASHBOARD_USER_LOGIN!r}")


def _without_github_source(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``source`` when it is ``github``, so a GitHub reply is saved as it was before.

    Code from before the field existed forbids unknown keys. Leaving the key out of GitHub
    replies means only a dashboard reply changes the file, and a rollback fails only for those.
    """
    if data.get("source") == "github":
        data.pop("source")
    return data


class AcceptedReplyReceipt(ModelBase):
    source: ReplySource = "github"
    comment_id: int | None = Field(default=None, ge=1)
    user_login: str = Field(min_length=1)
    user_id: int | None = None
    author_association: str = ""
    created_at: UtcDateTime
    accepted_at: UtcDateTime = Field(default_factory=utc_now)
    dispatched_at: UtcDateTime | None = None
    command: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    approval_context_fingerprint: str | None = None
    plan_decision_context_fingerprint: str | None = None
    delivery_retry_context_fingerprint: str | None = None

    @model_validator(mode="after")
    def _require_reply_source_fields(self) -> AcceptedReplyReceipt:
        _check_reply_source(self.source, self.comment_id, self.user_login)
        return self

    @model_serializer(mode="wrap")
    def _omit_github_source(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without_github_source(handler(self))


class RiskRationale(ModelBase):
    intended_outcome: str = Field(min_length=1, max_length=300)
    sensitive_boundary: str = Field(min_length=1, max_length=300)
    necessity: str = Field(min_length=1, max_length=300)
    credible_scenario: str = Field(min_length=1, max_length=400)
    known_mitigations: list[str] = Field(min_length=1, max_length=10)
    residual_risk: str = Field(min_length=1, max_length=300)

    @field_validator(
        "intended_outcome",
        "sensitive_boundary",
        "necessity",
        "credible_scenario",
        "residual_risk",
    )
    @classmethod
    def _validate_prose(cls, value: str) -> str:
        s = value.strip()
        if not s:
            raise ValueError("field must not be blank")
        return s

    @field_validator("known_mitigations")
    @classmethod
    def _validate_known_mitigations(cls, items: list[str]) -> list[str]:
        if not items:
            raise ValueError("known_mitigations must contain at least one mitigation")
        cleaned: list[str] = []
        for item in items:
            s = item.strip()
            if not s:
                raise ValueError("known_mitigations entries must not be blank")
            if len(s) > 200:
                raise ValueError("known_mitigations entries must be 200 characters or fewer")
            cleaned.append(s)
        return cleaned


class RiskApprovalContext(ModelBase):
    risk: Risk
    complexity: Complexity
    work_item_id: str = Field(min_length=1, max_length=128)
    work_item_title: str = Field(min_length=1, max_length=256)
    risk_rationale: RiskRationale
    decision_requested: str = Field(min_length=1, max_length=300)
    #: Contexts written before ADR-035 name REFINING. New contexts set PLANNING.
    next_state: WorkflowState = WorkflowState.REFINING
    authorized_actions: list[str] = Field(min_length=1, max_length=10)
    unauthorized_actions: list[str] = Field(min_length=1, max_length=10)
    conditions_in_force: list[str] = Field(min_length=1, max_length=10)
    context_fingerprint: str = Field(min_length=64, max_length=64)


class PlanDecisionContext(ModelBase):
    """Immutable decision questions that an authorized human may answer."""

    plan_fingerprint: str = Field(min_length=64, max_length=64)
    decisions: list[str] = Field(min_length=1, max_length=MAX_PLAN_DECISIONS)
    context_fingerprint: str = Field(min_length=64, max_length=64)

    @field_validator("decisions")
    @classmethod
    def _validate_decisions(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("decision questions must not be blank")
        if any(len(value) > 500 for value in cleaned):
            raise ValueError("decision questions must be 500 characters or fewer")
        return cleaned


class DeliveryRetryContext(ModelBase):
    """The reviewed work a person may publish again after the pull request failed to open."""

    reviewed_tree_sha: str = Field(min_length=40, max_length=64)
    base_commit_sha: str = Field(min_length=40, max_length=64)
    branch_name: str = Field(min_length=1)
    context_fingerprint: str = Field(min_length=64, max_length=64)


class PlanDecisionAnswer(ModelBase):
    """One validated answer to a numbered plan decision."""

    decision_number: int = Field(ge=1, le=MAX_PLAN_DECISIONS)
    answer: str = Field(min_length=1, max_length=500)

    @field_validator("answer")
    @classmethod
    def _validate_answer(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("answer must not be blank")
        if "\r" in cleaned or "\n" in cleaned:
            raise ValueError("answer must be a single line")
        return cleaned


class PlanDecisionAnswers(VersionedModel):
    """Durable authorized answers bound to one unresolved-plan escalation."""

    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    context_fingerprint: str = Field(min_length=64, max_length=64)
    source: ReplySource = "github"
    comment_id: int | None = Field(default=None, ge=1)
    user_login: str = Field(min_length=1)
    user_id: int | None = None
    author_association: str = ""
    answers: list[PlanDecisionAnswer] = Field(min_length=1, max_length=MAX_PLAN_DECISIONS)
    accepted_at: UtcDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _require_reply_source_fields(self) -> PlanDecisionAnswers:
        _check_reply_source(self.source, self.comment_id, self.user_login)
        return self

    @model_serializer(mode="wrap")
    def _omit_github_source(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _without_github_source(handler(self))

    @model_validator(mode="after")
    def _require_ordered_answers(self) -> PlanDecisionAnswers:
        _check_ordered_answers(self.answers)
        return self


def _check_ordered_answers(answers: list[PlanDecisionAnswer]) -> None:
    expected = list(range(1, len(answers) + 1))
    if [answer.decision_number for answer in answers] != expected:
        raise ValueError("answers must use contiguous decision numbers in order")


DashboardRequestAction = Literal[
    ResumeClassification.RISK_APPROVAL,
    ResumeClassification.PLAN_DECISION,
    ResumeClassification.DELIVERY_RETRY,
]
DashboardRequestStatus = Literal["pending", "stale"]
#: Why a run cannot take a resume. A dashboard request is marked stale with the same code.
ResumeRefusal = Literal["expired", "reopen_limit", "context_changed", "state_changed"]
#: A new ``ResumeRefusal`` value changes the stored request schema: older code rejects it.
DashboardRequestStaleReason = ResumeRefusal


class DashboardResumeRequest(VersionedModel):
    """A local approval or plan answer waiting for the service to read it.

    The dashboard creates it as ``pending``. Only the service marks it ``stale``,
    with the reason it refused it.
    """

    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    context_fingerprint: str
    action: DashboardRequestAction
    answers: list[PlanDecisionAnswer] = Field(default_factory=list, max_length=MAX_PLAN_DECISIONS)
    created_at: UtcDateTime = Field(default_factory=utc_now)
    status: DashboardRequestStatus = "pending"
    reason: DashboardRequestStaleReason | None = None

    @field_validator("episode_id")
    @classmethod
    def _validate_episode_id(cls, value: str) -> str:
        if EPISODE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("episode_id must be 1-128 letters, digits, '.', '_' or '-'")
        return value

    @field_validator("context_fingerprint")
    @classmethod
    def _validate_context_fingerprint(cls, value: str) -> str:
        if CONTEXT_FINGERPRINT_PATTERN.fullmatch(value) is None:
            raise ValueError("context_fingerprint must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _require_consistent_fields(self) -> DashboardResumeRequest:
        if self.action == ResumeClassification.PLAN_DECISION:
            if not self.answers:
                raise ValueError("a plan answer request needs answers")
            _check_ordered_answers(self.answers)
        elif self.answers:
            raise ValueError("a risk approval request has no answers")
        if (self.status == "stale") != (self.reason is not None):
            raise ValueError("a stale request needs a reason and a pending one has none")
        return self

    def marked_stale(self, reason: DashboardRequestStaleReason) -> DashboardResumeRequest:
        """This request as the service leaves it when it refuses it, validated again."""
        return DashboardResumeRequest.model_validate(
            {**self.model_dump(), "status": "stale", "reason": reason}
        )


#: The start of the halt reason of a run a person stopped with Stop or ``factory cancel``.
STOPPED_HALT_REASON = "stopped on request"


class RunStopRequest(VersionedModel):
    """A person's request to stop a run (ADR-047).

    The dashboard or ``factory cancel`` creates it once, and never changes it. The
    controller that drives the run reads it, kills the live agent call, halts the run in
    ``NEEDS_HUMAN`` and deletes the request.
    """

    run_id: str = Field(min_length=1)
    source: Literal["dashboard", "cli"]
    created_at: UtcDateTime = Field(default_factory=utc_now)


#: The longest operator note a person can send to steer a run (ADR-047).
MAX_STEER_NOTE_CHARS = 2000


class RunSteerNote(VersionedModel):
    """A person's note to the next agent call of a run (ADR-047).

    The dashboard or ``factory steer`` creates it once, and never changes it. The
    controller adds it to the next agent prompt as an operator section and deletes it.
    """

    run_id: str = Field(min_length=1)
    source: Literal["dashboard", "cli"]
    note: str = Field(min_length=1, max_length=MAX_STEER_NOTE_CHARS)
    created_at: UtcDateTime = Field(default_factory=utc_now)


class OperatorEscalation(VersionedModel):
    """The last ``factory escalate`` of a run and how it went, for the dashboard (ADR-055).

    The command writes ``running`` when it starts, then ``done`` or ``failed`` with the error.
    ``to_route`` is ``None`` when the run could not escalate at all. ``pid`` is the process
    that runs it, so a ``running`` record whose process is gone reads as failed.
    """

    from_route: ExecutionRoute | None = None
    to_route: ExecutionRoute | None = None
    note: str = Field(min_length=1, max_length=MAX_STEER_NOTE_CHARS)
    started_at: UtcDateTime = Field(default_factory=utc_now)
    status: Literal["running", "done", "failed"] = "running"
    error: str | None = None
    pid: int | None = None


class EscalationRecord(ModelBase):
    episode_id: str = Field(min_length=1)
    episode_number: int = Field(default=1, ge=1)
    status: EscalationStatus = EscalationStatus.PENDING_NOTIFICATION
    resume_classification: ResumeClassification = ResumeClassification.NOT_RESUMABLE
    target_type: EscalationTargetType | None = None
    target_host: str | None = None
    target_repository: str | None = None
    target_number: int | None = Field(default=None, ge=1)
    target_url: str | None = None
    comment_id: int | None = Field(default=None, ge=1)
    comment_url: str | None = None
    #: A ``HaltReasonCode`` value. It stays ``str`` so a ``run.json`` that holds a code
    #: this version does not know still loads. Readers use ``HaltReasonCode.parse``.
    reason_code: str = ""
    delivery_attempts: int = Field(default=0, ge=0)
    delivery_error: str | None = None
    last_notified_at: UtcDateTime | None = None
    reply_cursor: str | None = None
    accepted_replies: list[AcceptedReplyReceipt] = Field(default_factory=list)
    reopen_count: int = Field(default=0, ge=0)
    approval_context: RiskApprovalContext | None = None
    plan_decision_context: PlanDecisionContext | None = None
    delivery_retry_context: DeliveryRetryContext | None = None
    remote_resume_enabled: bool = False
    created_at: UtcDateTime = Field(default_factory=utc_now)
    updated_at: UtcDateTime = Field(default_factory=utc_now)

    def advanced_cursor(self, cursor: str, now: datetime) -> Self:
        """This record with the reply cursor at ``cursor``, stamped ``now``."""
        return self.model_copy(update={"reply_cursor": cursor, "updated_at": now})

    def closed_to_replies(self, now: datetime) -> Self:
        """This record with the cursor closed and remote resume off, stamped ``now``."""
        return self.model_copy(
            update={
                "remote_resume_enabled": False,
                "reply_cursor": REPLY_CURSOR_CLOSED,
                "updated_at": now,
            }
        )

    def expired(self, now: datetime) -> Self:
        """:meth:`closed_to_replies`, with the status ``EXPIRED``."""
        return self.closed_to_replies(now).model_copy(update={"status": EscalationStatus.EXPIRED})

    def reopened(self, receipt: AcceptedReplyReceipt, now: datetime) -> Self:
        """This record after ``receipt`` reopened the run: one more reopen, the cursor closed."""
        return self.model_copy(
            update={
                "accepted_replies": [*self.accepted_replies, receipt],
                "reopen_count": self.reopen_count + 1,
                "status": EscalationStatus.REOPENED,
                "reply_cursor": REPLY_CURSOR_CLOSED,
                "updated_at": now,
            }
        )
