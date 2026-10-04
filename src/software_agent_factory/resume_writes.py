"""Resume writers: the functions that save an accepted reply through the run store.

:mod:`.resume` holds the rules and does no writes. This module applies them: it saves the
receipt, the plan answers and the reopen of a run (:func:`accept_resume`), and settles the
dashboard requests of an episode (:func:`ingest_dashboard_request`). Only the service and the
GitHub reply poller call these. The dashboard imports :mod:`.resume` only, and a test checks
that.

Like :mod:`.resume`, it imports no GitHub client, no subprocess, no workflow and no service.
It writes only through the :class:`ResumeStore` it is given.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .config import FactoryConfig
from .escalation_protocol import format_answer_command, format_resume_command
from .models import (
    DASHBOARD_USER_LOGIN,
    AcceptedReplyReceipt,
    DashboardResumeRequest,
    EscalationRecord,
    FactoryRun,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    ReplySource,
    ResumeClassification,
    ResumeRefusal,
)
from .resume import (
    current_context_fingerprint,
    dashboard_already_accepted,
    dashboard_request_refusal,
    request_mismatch,
    resume_refusal,
)

logger = logging.getLogger(__name__)


class ResumeStore(Protocol):
    """The run store calls resume needs. ``FileRunStore`` satisfies it."""

    def load_run(self, run_id: str) -> FactoryRun: ...

    def save_run(self, run: FactoryRun) -> Path: ...

    def save_artifact(self, run_id: str, artifact: PlanDecisionAnswers) -> Path: ...

    def list_dashboard_requests(
        self, run_id: str, episode_id: str
    ) -> list[DashboardResumeRequest]: ...

    def replace_dashboard_request(self, run_id: str, request: DashboardResumeRequest) -> None: ...


@dataclass(frozen=True)
class ReplyIdentity:
    """Who gave an accepted reply and when. A dashboard reply has no comment id."""

    source: ReplySource
    comment_id: int | None
    user_login: str
    user_id: int | None
    author_association: str
    created_at: datetime


def _same_context(fresh: EscalationRecord, seen: EscalationRecord) -> bool:
    """Whether ``fresh`` is still the episode and context the reply was checked against."""
    return fresh.episode_id == seen.episode_id and (
        current_context_fingerprint(fresh) == current_context_fingerprint(seen)
    )


def _accept(
    run: FactoryRun,
    store: ResumeStore,
    config: FactoryConfig,
    *,
    reply: ReplyIdentity,
    answers: list[PlanDecisionAnswer] | None,
    now: datetime,
) -> AcceptedReplyReceipt | ResumeRefusal:
    seen = run.escalation
    if seen is None:
        raise ValueError(f"run {run.id} has no escalation to resume")
    # The caller's run may be older than the stored one: the other path can have accepted a
    # reply since it was read. Check and save the stored run, never the snapshot.
    fresh = store.load_run(run.id)
    # A dashboard request is judged when its human made it, so waiting for a free slot or the
    # daily quota cannot end its window. A GitHub reply is judged when the poller reads it.
    window_at = reply.created_at if reply.source == "dashboard" else now
    refusal = resume_refusal(fresh, config, window_at)
    if refusal is not None:
        return refusal
    escalation = fresh.escalation
    assert escalation is not None  # resume_refusal returned None
    if not _same_context(escalation, seen):
        return "context_changed"
    receipt = _build_receipt(fresh, escalation, reply, now)
    if answers is not None:
        _save_plan_answers(store, fresh, escalation, reply, answers, now)
    reopened = escalation.reopened(receipt, now)
    store.save_run(fresh.model_copy(update={"escalation": reopened}))
    if reply.source == "github":
        # The run is reopened, so a dashboard request made for this episode can no longer
        # be applied. A request this run accepted itself stays pending.
        for request in store.list_dashboard_requests(fresh.id, escalation.episode_id):
            if request.status == "pending":
                _mark_stale(store, fresh.id, request, "state_changed")
    return receipt


def _build_receipt(
    fresh: FactoryRun,
    escalation: EscalationRecord,
    reply: ReplyIdentity,
    now: datetime,
) -> AcceptedReplyReceipt:
    """The receipt for ``reply``, carrying the fingerprint of the context the human saw."""
    approval = escalation.approval_context
    plan = escalation.plan_decision_context
    delivery = escalation.delivery_retry_context
    is_risk = escalation.resume_classification is ResumeClassification.RISK_APPROVAL
    is_plan = escalation.resume_classification is ResumeClassification.PLAN_DECISION
    is_retry = escalation.resume_classification is ResumeClassification.DELIVERY_RETRY
    return AcceptedReplyReceipt(
        source=reply.source,
        comment_id=reply.comment_id,
        user_login=reply.user_login,
        user_id=reply.user_id,
        author_association=reply.author_association,
        created_at=reply.created_at,
        accepted_at=now,
        command=(
            format_resume_command(fresh.id, escalation.episode_id)
            if is_risk or is_retry
            else format_answer_command(fresh.id, escalation.episode_id)
        ),
        episode_id=escalation.episode_id,
        run_id=fresh.id,
        approval_context_fingerprint=approval.context_fingerprint if is_risk and approval else None,
        plan_decision_context_fingerprint=plan.context_fingerprint if is_plan and plan else None,
        delivery_retry_context_fingerprint=(
            delivery.context_fingerprint if is_retry and delivery else None
        ),
    )


def _save_plan_answers(
    store: ResumeStore,
    fresh: FactoryRun,
    escalation: EscalationRecord,
    reply: ReplyIdentity,
    answers: list[PlanDecisionAnswer],
    now: datetime,
) -> None:
    plan = escalation.plan_decision_context
    if plan is None:
        raise ValueError(f"run {fresh.id} has no plan decision context for the answers")
    store.save_artifact(
        fresh.id,
        PlanDecisionAnswers(
            run_id=fresh.id,
            episode_id=escalation.episode_id,
            plan_fingerprint=plan.plan_fingerprint,
            context_fingerprint=plan.context_fingerprint,
            source=reply.source,
            comment_id=reply.comment_id,
            user_login=reply.user_login,
            user_id=reply.user_id,
            author_association=reply.author_association,
            answers=answers,
            accepted_at=now,
        ),
    )


def accept_resume(
    run: FactoryRun,
    store: ResumeStore,
    config: FactoryConfig,
    *,
    reply: ReplyIdentity,
    answers: list[PlanDecisionAnswer] | None,
    now: datetime,
) -> AcceptedReplyReceipt | None:
    """Record an accepted reply: the receipt, the plan answers, a reopen and a closed cursor.

    The caller has already checked the reply. ``run`` may be a snapshot read earlier, so this
    reloads the stored run and checks :func:`resume_refusal` again, and that the episode and
    context are still the ones ``run`` shows. If any check fails, nothing is saved and the
    result is ``None``. Otherwise the stored run is saved, not ``run``. The receipt carries
    the fingerprint of the context the human saw. Plan answers are saved before the run, so
    a run never reopens without them.
    """
    result = _accept(run, store, config, reply=reply, answers=answers, now=now)
    return result if isinstance(result, AcceptedReplyReceipt) else None


def _mark_stale(
    store: ResumeStore,
    run_id: str,
    request: DashboardResumeRequest,
    reason: ResumeRefusal,
) -> None:
    store.replace_dashboard_request(run_id, request.marked_stale(reason))


def _settle_other_contexts(
    store: ResumeStore,
    run_id: str,
    escalation: EscalationRecord,
    requests: Sequence[DashboardResumeRequest],
) -> DashboardResumeRequest | None:
    """Mark the pending requests for another context stale; return the one for this context.

    A request for this context but another action is returned too:
    :func:`.resume.dashboard_request_refusal` judges it, so a closed window or a changed state
    is still the reason it gets.
    """
    current: DashboardResumeRequest | None = None
    for request in requests:
        if request.status != "pending":
            continue
        mismatch = request_mismatch(
            escalation, request.episode_id, request.context_fingerprint, request.action
        )
        if mismatch == "episode":
            continue
        if mismatch == "fingerprint":
            _mark_stale(store, run_id, request, "context_changed")
        else:
            current = request
    return current


def ingest_dashboard_request(
    run: FactoryRun,
    store: ResumeStore,
    config: FactoryConfig,
    now: datetime,
    *,
    requests: Sequence[DashboardResumeRequest] | None = None,
) -> AcceptedReplyReceipt | None:
    """Turn the pending dashboard request for ``run``'s current context into a reopen.

    Only the service calls this. It reads the requests of the current episode, checks the
    one for the current context again, and either records it like an accepted GitHub reply
    (source ``dashboard``) and returns the receipt, or marks it stale with the reason and
    returns ``None``. A pending request for any other context of the episode goes stale as
    ``context_changed``. A request the run already accepted stays pending, and so does a
    request nobody has read yet: capacity and quota never make a request stale, because the
    reply window is judged when the request was made, not at ``now``.
    A request stamped before its escalation or after ``now`` is stale as ``expired``.

    A caller that already read the requests of the episode can pass them as ``requests``, to
    save a second directory listing. Requests of another episode in it are ignored.
    """
    # Work from the stored run, never the caller's snapshot: an older snapshot could miss
    # this request's own acceptance and mark it stale.
    run = store.load_run(run.id)
    escalation = run.escalation
    if escalation is None:
        return None
    fingerprint = current_context_fingerprint(escalation)
    if requests is None:
        requests = store.list_dashboard_requests(run.id, escalation.episode_id)
    current = _settle_other_contexts(store, run.id, escalation, requests)
    if current is None or fingerprint is None:
        return None
    if dashboard_already_accepted(escalation, fingerprint):
        return None
    reason, answers = dashboard_request_refusal(run, current, config, now)
    if reason is not None:
        logger.info("dashboard request for run %s is stale: %s", run.id, reason)
        _mark_stale(store, run.id, current, reason)
        return None
    result = _accept(
        run,
        store,
        config,
        reply=ReplyIdentity(
            source="dashboard",
            comment_id=None,
            user_login=DASHBOARD_USER_LOGIN,
            user_id=None,
            author_association="",
            created_at=current.created_at,
        ),
        answers=answers,
        now=now,
    )
    if isinstance(result, AcceptedReplyReceipt):
        return result
    # ``run`` may be older than the stored run, which can hold this request's own acceptance.
    stored = store.load_run(run.id).escalation
    if stored is not None and dashboard_already_accepted(stored, fingerprint):
        return None
    logger.info("dashboard request for run %s is stale: %s", run.id, result)
    _mark_stale(store, run.id, current, result)
    return None
