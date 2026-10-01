"""Resume rules shared by the GitHub reply poller and the dashboard request path.

Both paths end in the same place: a receipt, a reopen count, and ``REOPENED``. The rules
that decide whether a run may resume, and what a valid answer looks like, live here once, so
the two paths cannot drift apart.

This module does no I/O of its own. It imports only :mod:`.models`, :mod:`.config`,
:mod:`.escalation_protocol` and the standard library: no GitHub client, no subprocess, no
workflow and no service. It writes only through the :class:`ResumeStore` it is given, and
only the service calls the write functions (:func:`accept_resume` and
:func:`ingest_dashboard_request`). The read-only dashboard never does. Tests check both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .config import FactoryConfig
from .escalation_protocol import MAX_PLAN_DECISIONS, format_answer_command, format_resume_command
from .models import (
    DASHBOARD_USER_LOGIN,
    REPLY_CURSOR_CLOSED,
    AcceptedReplyReceipt,
    DashboardResumeRequest,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
    ReplySource,
    ResumeClassification,
    ResumeRefusal,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)

logger = logging.getLogger(__name__)

MAX_PLAN_DECISION_ANSWER_CHARS = 500

# Absolute, network, and system file system paths
_ABSOLUTE_OR_NETWORK_PATH_PATTERN = re.compile(
    r"(?i)"
    r"(?:(?<![A-Za-z0-9.~/@\\<])(?<!&lt;)/(?:[A-Za-z0-9_.-]+)[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])~[\\/][^\s\"'`>)]+)"
    r"|(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])\\\\[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.:])//[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
)

# Credentials embedded in URLs
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@[^\s/]+"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s@]+@[^\s/]+"
)

# Tokens, API keys, credentials, and private keys
_TOKEN_AND_KEY_PATTERN = re.compile(
    r"(?i)\bxox[baprse]-[0-9A-Za-z-]{10,}\b"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,})\b"
    r"|\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"
    r"|\bsk-(?:proj-|ant-)?[0-9a-zA-Z_-]{20,}\b"
    r"|\b(?:authorization|proxy[_-]?authorization)\s*[:=]\s*[^\r\n]+"
    r"|\b(?:cookie|set[_-]?cookie|set[_-]?cookie2)\s*[:=]\s*[^\r\n]+"
    r"|\bBearer\s+[A-Za-z0-9_.\-/+=]{20,}"
    r"|\bBasic\s+[A-Za-z0-9+/]{8,}={1,2}(?!\S)"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|session[_-]?id|session[_-]?token|session[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_.-]{8,}"
    r"|-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"
)

# External URLs (http, https, ftp) and bare www. domains
_EXTERNAL_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|ftp)://[^\s\"'`<>)]+"
    r"|\bwww\.[A-Za-z0-9_.-]+\.[A-Za-z]{2,}[^\s\"'`<>)]*"
)

# Raw diagnostics / stack traces / diff output
_RAW_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(?:traceback \(most recent call last\)|subprocess\.calledprocesserror|"
    r"file \"[^\"]+\", line \d+|diff --git|@@ -\d+,\d+ \+\d+,\d+ @@|\+[A-Z0-9_]+=[^\s]+)"
)


def contains_unsafe_content(text: str) -> tuple[bool, str]:
    """Check whether text contains paths, embedded credentials, tokens, URLs, or diagnostics."""
    if not text:
        return False, ""
    if _URL_CREDENTIAL_PATTERN.search(text):
        return True, "contains URL-embedded credentials"
    if _EXTERNAL_URL_PATTERN.search(text):
        return True, "contains external URL or link"
    if _TOKEN_AND_KEY_PATTERN.search(text):
        return True, "contains token or credential"
    if _ABSOLUTE_OR_NETWORK_PATH_PATTERN.search(text):
        return True, "contains local or network file system path"
    if _RAW_DIAGNOSTIC_PATTERN.search(text):
        return True, "contains raw diagnostic or diff output"
    return False, ""


def clean_plan_answer(text: str) -> str | None:
    """The answer to one plan decision once trimmed, or ``None`` when it breaks a rule.

    An answer is one line of 1 to :data:`MAX_PLAN_DECISION_ANSWER_CHARS` characters with no
    path, URL, credential or diagnostic text.
    """
    answer = text.strip()
    if not answer or len(answer) > MAX_PLAN_DECISION_ANSWER_CHARS:
        return None
    if "\r" in answer or "\n" in answer:
        return None
    if contains_unsafe_content(answer)[0]:
        return None
    return answer


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


def _fingerprint_dict(data: dict[str, object]) -> str:
    """SHA-256 of ``data`` as canonical JSON: sorted keys, no spaces."""
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_approval_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    work_item_id: str,
    work_item_title: str,
    risk: str,
    complexity: str,
    rationale: RiskRationale,
    decision_requested: str,
    next_state: str,
    authorized_actions: Sequence[str],
    unauthorized_actions: Sequence[str],
    conditions_in_force: Sequence[str],
) -> str:
    """Compute deterministic SHA-256 binding displayed and authority fields to episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "work_item_id": work_item_id,
            "work_item_title": work_item_title,
            "risk": risk,
            "complexity": complexity,
            "intended_outcome": rationale.intended_outcome,
            "sensitive_boundary": rationale.sensitive_boundary,
            "necessity": rationale.necessity,
            "credible_scenario": rationale.credible_scenario,
            "known_mitigations": list(rationale.known_mitigations),
            "residual_risk": rationale.residual_risk,
            "decision_requested": decision_requested,
            "next_state": next_state,
            "authorized_actions": list(authorized_actions),
            "unauthorized_actions": list(unauthorized_actions),
            "conditions_in_force": list(conditions_in_force),
        }
    )


def is_valid_risk_approval_context(
    context: RiskApprovalContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify that an approval context is complete, safe, and bound to this run and episode."""
    if not isinstance(context, RiskApprovalContext):
        return False
    if context.risk not in {Risk.R2, Risk.R3}:
        return False
    if context.next_state is not WorkflowState.REFINING:
        return False

    rationale = context.risk_rationale
    fields = [
        context.work_item_id,
        context.work_item_title,
        context.decision_requested,
        rationale.intended_outcome,
        rationale.sensitive_boundary,
        rationale.necessity,
        rationale.credible_scenario,
        *rationale.known_mitigations,
        rationale.residual_risk,
        *context.authorized_actions,
        *context.unauthorized_actions,
        *context.conditions_in_force,
    ]
    for field_val in fields:
        is_unsafe, _ = contains_unsafe_content(field_val)
        if is_unsafe:
            return False

    expected_fp = compute_approval_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        work_item_id=context.work_item_id,
        work_item_title=context.work_item_title,
        risk=context.risk.value,
        complexity=context.complexity.value,
        rationale=rationale,
        decision_requested=context.decision_requested,
        next_state=context.next_state.value,
        authorized_actions=context.authorized_actions,
        unauthorized_actions=context.unauthorized_actions,
        conditions_in_force=context.conditions_in_force,
    )
    return secrets.compare_digest(context.context_fingerprint, expected_fp)


def compute_plan_decision_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    plan_fingerprint: str,
    decisions: Sequence[str],
) -> str:
    """Bind a numbered decision set to one run and escalation episode."""
    return _fingerprint_dict(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "plan_fingerprint": plan_fingerprint,
            "decisions": list(decisions),
        }
    )


def is_valid_plan_decision_context(
    context: PlanDecisionContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify an answerable decision context is safe and bound to its episode."""
    if not isinstance(context, PlanDecisionContext):
        return False
    if not 1 <= len(context.decisions) <= MAX_PLAN_DECISIONS:
        return False
    if any(not value or contains_unsafe_content(value)[0] for value in context.decisions):
        return False
    expected = compute_plan_decision_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        plan_fingerprint=context.plan_fingerprint,
        decisions=context.decisions,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)


#: Escalation states in which a run still waits for a human.
WAITING_STATUSES = frozenset(
    {
        EscalationStatus.PENDING_NOTIFICATION,
        EscalationStatus.NOTIFIED,
        EscalationStatus.NOTIFICATION_FAILED,
    }
)


def _has_valid_resume_context(run: FactoryRun) -> bool:
    escalation = run.escalation
    if escalation is None:
        return False
    if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
        return escalation.approval_context is not None and is_valid_risk_approval_context(
            escalation.approval_context, run.id, escalation.episode_id
        )
    if escalation.resume_classification is ResumeClassification.PLAN_DECISION:
        return escalation.plan_decision_context is not None and is_valid_plan_decision_context(
            escalation.plan_decision_context, run.id, escalation.episode_id
        )
    return False


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
    return resume_refusal_within(
        run,
        reply_window_hours=config.escalation.reply_window_hours,
        max_reopens=config.escalation.max_reopens,
        now=now,
    )


def resume_refusal_within(
    run: FactoryRun,
    *,
    reply_window_hours: float | None,
    max_reopens: int | None,
    now: datetime,
) -> ResumeRefusal | None:
    """:func:`resume_refusal` for a caller that holds the two limits, not the whole config.

    A limit that is ``None`` is unknown and is not checked, as in
    :func:`.escalation_protocol.reply_closed_cause`.
    """
    escalation = run.escalation
    if escalation is None or not awaits_human(run):
        return "state_changed"
    if not _has_valid_resume_context(run):
        return "context_changed"
    if reply_window_hours is not None and now > escalation.created_at + timedelta(
        hours=reply_window_hours
    ):
        return "expired"
    if max_reopens is not None and escalation.reopen_count >= max_reopens:
        return "reopen_limit"
    return None


def can_accept_resume(run: FactoryRun, config: FactoryConfig, now: datetime) -> bool:
    """Whether ``run`` can accept a resume at ``now``. See :func:`resume_refusal` for why not."""
    return resume_refusal(run, config, now) is None


def _current_context_fingerprint(escalation: EscalationRecord) -> str | None:
    if escalation.resume_classification is ResumeClassification.RISK_APPROVAL:
        risk_context = escalation.approval_context
        return risk_context.context_fingerprint if risk_context is not None else None
    if escalation.resume_classification is ResumeClassification.PLAN_DECISION:
        plan_context = escalation.plan_decision_context
        return plan_context.context_fingerprint if plan_context is not None else None
    return None


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
        _current_context_fingerprint(fresh) == _current_context_fingerprint(seen)
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
    reopened = escalation.model_copy(
        update={
            "accepted_replies": [*escalation.accepted_replies, receipt],
            "reopen_count": escalation.reopen_count + 1,
            "status": EscalationStatus.REOPENED,
            "reply_cursor": REPLY_CURSOR_CLOSED,
            "updated_at": now,
        }
    )
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
    is_risk = escalation.resume_classification is ResumeClassification.RISK_APPROVAL
    is_plan = escalation.resume_classification is ResumeClassification.PLAN_DECISION
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
            if is_risk
            else format_answer_command(fresh.id, escalation.episode_id)
        ),
        episode_id=escalation.episode_id,
        run_id=fresh.id,
        approval_context_fingerprint=approval.context_fingerprint if is_risk and approval else None,
        plan_decision_context_fingerprint=plan.context_fingerprint if is_plan and plan else None,
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


def _dashboard_already_accepted(escalation: EscalationRecord, fingerprint: str) -> bool:
    return any(
        receipt.source == "dashboard"
        and receipt.episode_id == escalation.episode_id
        and fingerprint
        in (receipt.approval_context_fingerprint, receipt.plan_decision_context_fingerprint)
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
        if not _dashboard_already_accepted(escalation, request.context_fingerprint)
    ]


def _request_refusal(
    run: FactoryRun, request: DashboardResumeRequest, config: FactoryConfig, now: datetime
) -> tuple[ResumeRefusal | None, list[PlanDecisionAnswer] | None]:
    """The stale reason for ``request`` (or ``None``), and its re-validated plan answers."""
    # The window is judged when the human made the request, not when the service reads it.
    refusal = resume_refusal(run, config, request.created_at)
    if refusal is not None:
        return refusal, None
    escalation = run.escalation
    assert escalation is not None  # resume_refusal returned None
    # The stamp decides the window, so a stamp the episode or the clock cannot have is refused.
    if request.created_at < escalation.created_at or request.created_at > now:
        return "expired", None
    if request.run_id != run.id or request.action is not escalation.resume_classification:
        return "context_changed", None
    if request.action is not ResumeClassification.PLAN_DECISION:
        return None, None
    context = escalation.plan_decision_context
    assert context is not None  # a valid plan decision context
    answers = build_plan_answers(
        [answer.answer for answer in request.answers], decision_count=len(context.decisions)
    )
    return ("context_changed" if answers is None else None), answers


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
    fingerprint: str | None,
    requests: Sequence[DashboardResumeRequest],
) -> DashboardResumeRequest | None:
    """Mark the pending requests for another context stale; return the one for ``fingerprint``."""
    current: DashboardResumeRequest | None = None
    for request in requests:
        if request.status != "pending" or request.episode_id != escalation.episode_id:
            continue
        if request.context_fingerprint == fingerprint:
            current = request
        else:
            _mark_stale(store, run_id, request, "context_changed")
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
    fingerprint = _current_context_fingerprint(escalation)
    if requests is None:
        requests = store.list_dashboard_requests(run.id, escalation.episode_id)
    current = _settle_other_contexts(store, run.id, escalation, fingerprint, requests)
    if current is None or fingerprint is None:
        return None
    if _dashboard_already_accepted(escalation, fingerprint):
        return None
    reason, answers = _request_refusal(run, current, config, now)
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
    if stored is not None and _dashboard_already_accepted(stored, fingerprint):
        return None
    logger.info("dashboard request for run %s is stale: %s", run.id, result)
    _mark_stale(store, run.id, current, result)
    return None
