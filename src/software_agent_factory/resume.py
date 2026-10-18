"""Resume rules shared by the GitHub reply poller and the dashboard request path.

Both paths end in the same place: a receipt, a reopen count, and ``REOPENED``. The rules
that decide whether a run may resume, and what a valid answer looks like, live here once, so
the two paths cannot drift apart.

This module is pure. It reads runs and requests and returns answers. It never writes, and it
imports only :mod:`.models`, :mod:`.config`, :mod:`.escalation_protocol`, :mod:`.redaction`,
:mod:`.resume_classes` and the standard library: no store, no GitHub client, no subprocess,
no workflow and no service. The writes live in :mod:`.resume_writes`, and only the service
and the reply poller call those. The dashboard imports this module and never that one. Tests
check both.

The per-class context checks live in :mod:`.resume_classes`; the names below re-export them
for the callers that import the resume rules from here.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, TypeIs, get_args

from .config import FactoryConfig
from .escalation_protocol import MAX_PLAN_DECISIONS, ReplyPolicy
from .models import (
    MAX_STEER_NOTE_CHARS,
    AcceptedReplyReceipt,
    DashboardResumeRequest,
    EscalationRecord,
    EscalationStatus,
    ExecutionRoute,
    FactoryRun,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
    ResumeClassification,
    ResumeRefusal,
    RiskApprovalContext,
    WorkflowState,
)
from .redaction import REDACTION_PLACEHOLDER, contains_secret
from .resume_classes import RESUME_CLASSES
from .resume_classes import (
    compute_approval_context_fingerprint as compute_approval_context_fingerprint,
)
from .resume_classes import (
    compute_delivery_retry_context_fingerprint as compute_delivery_retry_context_fingerprint,
)
from .resume_classes import (
    compute_plan_decision_context_fingerprint as compute_plan_decision_context_fingerprint,
)
from .resume_classes import contains_unsafe_content as contains_unsafe_content
from .resume_classes import is_valid_delivery_retry_context as is_valid_delivery_retry_context
from .resume_classes import is_valid_plan_decision_context as is_valid_plan_decision_context
from .resume_classes import is_valid_risk_approval_context as is_valid_risk_approval_context

MAX_PLAN_DECISION_ANSWER_CHARS = 500


def clean_plan_answer(text: str) -> str | None:
    """The answer to one plan decision once trimmed, or ``None`` when it breaks a rule.

    An answer is one line of 1 to :data:`MAX_PLAN_DECISION_ANSWER_CHARS` characters with no
    path, URL, credential or diagnostic text. An answer that holds the redaction placeholder is
    refused too: a GitHub comment body is redacted before it is parsed, so the placeholder shows
    that a credential shape was there.
    """
    answer = text.strip()
    if not answer or len(answer) > MAX_PLAN_DECISION_ANSWER_CHARS:
        return None
    if "\r" in answer or "\n" in answer:
        return None
    if REDACTION_PLACEHOLDER in answer or contains_unsafe_content(answer)[0]:
        return None
    return answer


def clean_steer_note(text: str) -> str | None:
    """A steer note once trimmed, or ``None`` when it is empty, too long or holds a secret.

    A note may span lines and name repository paths: only credentials are refused, and the
    redaction placeholder, which shows that a credential shape was there (ADR-047).
    """
    note = text.strip()
    if not note or len(note) > MAX_STEER_NOTE_CHARS:
        return None
    if REDACTION_PLACEHOLDER in note or contains_secret(note):
        return None
    return note


#: States ``factory escalate`` takes a run out of, when nothing works on it (ADR-055).
_ESCALATABLE_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.PR_READY,
        WorkflowState.PR_CREATED,
        WorkflowState.CI_RUNNING,
        WorkflowState.CI_DIAGNOSIS,
        WorkflowState.NEEDS_HUMAN,
        WorkflowState.DONE,
    }
)
#: The route an operator escalation moves a run to (ADR-055). ``FULL`` stays ``FULL``.
_ESCALATION_ROUTES: dict[ExecutionRoute, ExecutionRoute] = {
    ExecutionRoute.SINGLE: ExecutionRoute.CRITIQUE,
    ExecutionRoute.CRITIQUE: ExecutionRoute.FULL,
    ExecutionRoute.FULL_REVIEW: ExecutionRoute.FULL,
    ExecutionRoute.MANUAL_TRIAGE: ExecutionRoute.FULL,
    ExecutionRoute.FULL: ExecutionRoute.FULL,
}


def escalation_route(run: FactoryRun) -> ExecutionRoute | None:
    """The route ``factory escalate`` moves ``run`` to, or ``None`` when it cannot escalate.

    It does not check for a live process: the workspace lock does that (ADR-055).
    """
    if (
        run.state not in _ESCALATABLE_STATES
        or run.merge_commit_sha is not None
        or run.route_decision is None
    ):
        return None
    return _ESCALATION_ROUTES[run.route_decision.effective_route]


def build_plan_answers(
    texts: Sequence[str], *, decision_count: int
) -> list[PlanDecisionAnswer] | None:
    """One numbered answer for each of ``decision_count`` decisions, in order, or ``None``.

    ``None`` means a wrong count, an unsafe answer or one that breaks :func:`clean_plan_answer`.
    """
    if not 1 <= decision_count <= MAX_PLAN_DECISIONS or len(texts) != decision_count:
        return None
    answers: list[PlanDecisionAnswer] = []
    for number, text in enumerate(texts, start=1):
        answer = clean_plan_answer(text)
        if answer is None:
            return None
        answers.append(PlanDecisionAnswer(decision_number=number, answer=answer))
    return answers


#: Escalation states in which a run still waits for a human.
WAITING_STATUSES = frozenset(
    {
        EscalationStatus.PENDING_NOTIFICATION,
        EscalationStatus.NOTIFIED,
        EscalationStatus.NOTIFICATION_FAILED,
    }
)


def has_valid_resume_context(run: FactoryRun) -> bool:
    """Whether the stored decision context of ``run`` is the one its escalation asks about."""
    escalation = run.escalation
    if escalation is None:
        return False
    spec = RESUME_CLASSES.get(escalation.resume_classification)
    return spec is not None and spec.is_valid_context(
        spec.context(escalation), run.id, escalation.episode_id
    )


def awaits_human(run: FactoryRun) -> bool:
    """Whether ``run`` waits in ``NEEDS_HUMAN`` for a human to reply to its escalation."""
    escalation = run.escalation
    return (
        run.state is WorkflowState.NEEDS_HUMAN
        and escalation is not None
        and escalation.status in WAITING_STATUSES
    )


def resume_refusal(run: FactoryRun, config: FactoryConfig, now: datetime) -> ResumeRefusal | None:
    """Why ``run`` cannot accept a resume at ``now``, or ``None`` when it can.

    The run must wait in ``NEEDS_HUMAN`` with an escalation that is pending, notified or
    failed to notify, with a valid decision context, inside its reply window and with a
    reopen left. The result is the stale reason code a dashboard request gets. ``now`` is
    the moment the reply window is judged: a GitHub reply is judged when it is read, a
    dashboard request when its human made it. ``remote_resume_enabled`` is not part of this
    check: it covers GitHub replies only.
    """
    return resume_refusal_within(run, ReplyPolicy.from_config(config.escalation), now)


def resume_refusal_within(
    run: FactoryRun, policy: ReplyPolicy, now: datetime
) -> ResumeRefusal | None:
    """:func:`resume_refusal` for a caller that holds the policy, not the whole config.

    It reads the window and the reopen limit only. The switch and the hosts of ``policy``
    are for GitHub replies, as in :func:`.escalation_protocol.reply_closed_cause`.
    """
    escalation = run.escalation
    if escalation is None or not awaits_human(run):
        return "state_changed"
    if not has_valid_resume_context(run):
        return "context_changed"
    if policy.window_passed(escalation, now):
        return "expired"
    if not policy.reopens_left(escalation):
        return "reopen_limit"
    return None


def current_context_fingerprint(escalation: EscalationRecord) -> str | None:
    """The fingerprint of the context ``escalation`` asks a human to decide, or ``None``."""
    spec = RESUME_CLASSES.get(escalation.resume_classification)
    return spec.fingerprint(escalation) if spec is not None else None


#: What a request names that is not what the run asks now: its episode, its context, its action.
RequestMismatch = Literal["episode", "fingerprint", "action"]


def request_mismatch(
    escalation: EscalationRecord,
    episode_id: str,
    fingerprint: str | None,
    action: ResumeClassification,
) -> RequestMismatch | None:
    """The first way a request differs from what ``escalation`` asks now, or ``None``.

    The order is the episode, then the context fingerprint, then the action. It is the
    second half of :func:`request_refusal`. Ingest also calls it directly to settle the
    requests of an older context, so those go stale as ``context_changed`` even when the
    run has stopped waiting.
    """
    if episode_id != escalation.episode_id:
        return "episode"
    if fingerprint != current_context_fingerprint(escalation):
        return "fingerprint"
    if action is not escalation.resume_classification:
        return "action"
    return None


def _is_mismatch(reason: ResumeRefusal | RequestMismatch) -> TypeIs[RequestMismatch]:
    return reason in get_args(RequestMismatch)


def request_refusal(
    run: FactoryRun,
    *,
    episode_id: str,
    fingerprint: str | None,
    action: ResumeClassification,
    policy: ReplyPolicy,
    now: datetime,
) -> ResumeRefusal | RequestMismatch | None:
    """The one reason a request for ``run`` is not taken at ``now``, or ``None``.

    The order is the service's: :func:`resume_refusal_within` first (state, context, window,
    reopens), then :func:`request_mismatch` (episode, fingerprint, action). The dashboard
    asks it of a request it is about to store and the service of the request for the
    current context, so both give the same reason for that request. The stamp of a stored
    request is judged by the service alone, after the refusals and before the mismatch.
    """
    refusal = resume_refusal_within(run, policy, now)
    if refusal is not None:
        return refusal
    escalation = run.escalation
    assert escalation is not None  # resume_refusal_within returned None
    return request_mismatch(escalation, episode_id, fingerprint, action)


def dashboard_already_accepted(escalation: EscalationRecord, fingerprint: str) -> bool:
    """Whether ``escalation`` holds a dashboard receipt of this episode for ``fingerprint``."""
    return any(
        receipt.source == "dashboard"
        and receipt.episode_id == escalation.episode_id
        and fingerprint
        in (
            receipt.approval_context_fingerprint,
            receipt.plan_decision_context_fingerprint,
            receipt.delivery_retry_context_fingerprint,
        )
        for receipt in escalation.accepted_replies
    )


def unsettled_requests(
    run: FactoryRun, requests: Sequence[DashboardResumeRequest]
) -> list[DashboardResumeRequest]:
    """The ``requests`` ingest can still settle: not already accepted by ``run`` itself.

    A request the run accepted stays pending by design, so ingest has nothing left to do
    for it. ``run`` is the snapshot the caller read.
    """
    escalation = run.escalation
    if escalation is None:
        return list(requests)
    return [
        request
        for request in requests
        if not dashboard_already_accepted(escalation, request.context_fingerprint)
    ]


def dashboard_request_refusal(
    run: FactoryRun, request: DashboardResumeRequest, config: FactoryConfig, now: datetime
) -> tuple[ResumeRefusal | None, list[PlanDecisionAnswer] | None]:
    """The stale reason for ``request`` (or ``None``), and its re-validated plan answers."""
    # The window is judged when the human made the request, not when the service reads it.
    refusal = request_refusal(
        run,
        episode_id=request.episode_id,
        fingerprint=request.context_fingerprint,
        action=request.action,
        policy=ReplyPolicy.from_config(config.escalation),
        now=request.created_at,
    )
    if refusal is not None and not _is_mismatch(refusal):
        return refusal, None
    escalation = run.escalation
    assert escalation is not None  # request_refusal found a waiting run
    # The stamp decides the window, so a stamp the episode or the clock cannot have is refused.
    if request.created_at < escalation.created_at or request.created_at > now:
        return "expired", None
    if request.run_id != run.id or refusal is not None:
        return "context_changed", None
    if request.action is not ResumeClassification.PLAN_DECISION:
        return None, None
    context = escalation.plan_decision_context
    assert context is not None  # a valid plan decision context
    answers = build_plan_answers(
        [answer.answer for answer in request.answers], decision_count=len(context.decisions)
    )
    return ("context_changed" if answers is None else None), answers


def receipt_approves_risk_context(
    receipt: AcceptedReplyReceipt, context: RiskApprovalContext
) -> bool:
    """Whether ``receipt`` carries the fingerprint of exactly this approval ``context``."""
    return receipt.approval_context_fingerprint is not None and secrets.compare_digest(
        receipt.approval_context_fingerprint, context.context_fingerprint
    )


def is_valid_plan_decision_answers(
    answers: PlanDecisionAnswers | None,
    context: PlanDecisionContext,
    *,
    run_id: str,
    episode_id: str,
    receipt: AcceptedReplyReceipt,
) -> bool:
    """Verify persisted human answers still bind to the active decision episode."""
    if not isinstance(answers, PlanDecisionAnswers):
        return False
    if (
        answers.run_id != run_id
        or answers.episode_id != episode_id
        or answers.source != receipt.source
        or answers.comment_id != receipt.comment_id
        or answers.user_login != receipt.user_login
        or answers.user_id != receipt.user_id
        or answers.author_association != receipt.author_association
    ):
        return False
    if not (
        secrets.compare_digest(answers.plan_fingerprint, context.plan_fingerprint)
        and secrets.compare_digest(answers.context_fingerprint, context.context_fingerprint)
        and receipt.plan_decision_context_fingerprint is not None
        and secrets.compare_digest(
            receipt.plan_decision_context_fingerprint, context.context_fingerprint
        )
    ):
        return False
    rebuilt = build_plan_answers(
        [answer.answer for answer in answers.answers], decision_count=len(context.decisions)
    )
    return rebuilt == answers.answers
