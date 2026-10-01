"""The two writes the dashboard asks for: approve a risk approval, answer plan decisions.

``POST /api/runs/<id>/approve`` and ``/answer`` end in one create-only request file that
the factory service reads later. This module checks the body and the run, and hands the
request to the injected :class:`~software_agent_factory.dashboard.snapshot.ResumeRequester`.
It never changes a run. It reads the run through the same rules the service uses
(:func:`software_agent_factory.resume.resume_refusal_within` and the answer rules), so a
request that passes here is one the service would accept now. It imports only the read
functions of ``resume``: the write functions stay with the service, and a test checks it.

Transport checks (route, ``Host``, ``Origin``, token, content type, length, JSON) live in
``handler.py``. Everything here starts with a decoded JSON body.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Literal

from ..models import (
    CONTEXT_FINGERPRINT_PATTERN,
    DashboardResumeRequest,
    EscalationRecord,
    FactoryRun,
    PlanDecisionAnswer,
    ResumeClassification,
    ResumeRefusal,
    utc_now,
)
from ..resume import (
    RequestMismatch,
    awaits_human,
    build_plan_answers,
    clean_plan_answer,
    request_mismatch,
    resume_refusal_within,
)
from .snapshot import ResumeRequester, ResumeRunReader, is_valid_run_id
from .validators import is_episode_id

#: Why a well-formed request is refused with ``409``. The page maps each code to a sentence.
ConflictReason = Literal[
    "not_waiting",
    "stale_episode",
    "stale_fingerprint",
    "wrong_action",
    "reopen_limit",
    "expired",
    "existing_request",
]

#: What a request names wrongly, as the code the page gets.
_MISMATCH_REASONS: dict[RequestMismatch, ConflictReason] = {
    "episode": "stale_episode",
    "fingerprint": "stale_fingerprint",
    "action": "wrong_action",
}

#: The service's refusal codes as the codes the page gets. ``state_changed`` and
#: ``context_changed`` are the service's words for a run that stopped waiting and a context
#: that no longer matches.
_REFUSAL_REASONS: dict[ResumeRefusal, ConflictReason] = {
    "state_changed": "not_waiting",
    "context_changed": "stale_fingerprint",
    "expired": "expired",
    "reopen_limit": "reopen_limit",
}


@dataclass(frozen=True, kw_only=True)
class ResumeActions:
    """What the approve and answer routes need. Without it they are not routed.

    ``reply_window_hours`` and ``max_reopens`` are the configured limits, with ``None``
    for a limit that is unknown. ``clock`` stamps ``created_at``: a request never carries
    a time the client chose.
    """

    run_reader: ResumeRunReader
    requester: ResumeRequester
    reply_window_hours: float | None = None
    max_reopens: int | None = None
    clock: Callable[[], datetime] = utc_now


class ActionRejected(Exception):  # noqa: N818 - a response, not an error condition
    """A request the dashboard refuses. ``payload`` is the JSON body of the response."""

    def __init__(
        self,
        status: HTTPStatus,
        error: str,
        *,
        reason: ConflictReason | None = None,
        decision: int | None = None,
    ) -> None:
        super().__init__(error)
        self.status = status
        #: The status and, for a ``409``, its reason: what the audit event records.
        self.result = f"{status.value} {reason}" if reason is not None else str(status.value)
        self.payload: dict[str, object] = {"error": error}
        if reason is not None:
            self.payload["reason"] = reason
        if decision is not None:
            self.payload["decision"] = decision


def _bad_request(error: str, *, decision: int | None = None) -> ActionRejected:
    return ActionRejected(HTTPStatus.BAD_REQUEST, error, decision=decision)


def _conflict(reason: ConflictReason) -> ActionRejected:
    return ActionRejected(HTTPStatus.CONFLICT, "conflict", reason=reason)


@dataclass(frozen=True)
class _Fields:
    episode_id: str
    context_fingerprint: str
    answers: tuple[str, ...]


def _answer_texts(raw: object) -> tuple[str, ...]:
    """The answers of a body, each one checked on its own so a failure names its decision."""
    if not isinstance(raw, list) or not raw:
        raise _bad_request("answers must be a list with one answer per decision")
    for number, text in enumerate(raw, start=1):
        if not isinstance(text, str) or clean_plan_answer(text) is None:
            raise _bad_request(f"the answer for decision {number} is not valid", decision=number)
    return tuple(raw)


def _parse_fields(kind: ResumeClassification, body: object) -> _Fields:
    if not isinstance(body, dict):
        raise _bad_request("the body must be a JSON object")
    episode_id = body.get("episode_id")
    fingerprint = body.get("context_fingerprint")
    if not is_episode_id(episode_id):
        raise _bad_request("episode_id is missing or not valid")
    # The stored shape (64 lowercase hex), not the wider one the page accepts for display.
    if (
        not isinstance(fingerprint, str)
        or CONTEXT_FINGERPRINT_PATTERN.fullmatch(fingerprint) is None
    ):
        raise _bad_request("context_fingerprint is missing or not valid")
    answers = (
        _answer_texts(body.get("answers")) if kind is ResumeClassification.PLAN_DECISION else ()
    )
    return _Fields(episode_id, fingerprint, answers)


def _conflict_reason(
    actions: ResumeActions,
    run: FactoryRun,
    kind: ResumeClassification,
    fields: _Fields,
    now: datetime,
) -> ConflictReason | None:
    """Why the service would not take this request now, in the order the page explains it."""
    escalation = run.escalation
    if escalation is None or not awaits_human(run):
        return "not_waiting"
    mismatch = request_mismatch(escalation, fields.episode_id, fields.context_fingerprint, kind)
    if mismatch is not None:
        return _MISMATCH_REASONS[mismatch]
    refusal = resume_refusal_within(
        run,
        reply_window_hours=actions.reply_window_hours,
        max_reopens=actions.max_reopens,
        now=now,
    )
    return None if refusal is None else _REFUSAL_REASONS[refusal]


def _plan_answers(escalation: EscalationRecord, texts: Sequence[str]) -> list[PlanDecisionAnswer]:
    """The numbered answers, built by the rule the service applies, or ``400``.

    The service checks them again, and a request it cannot read goes stale, so a bad count
    is refused here.
    """
    context = escalation.plan_decision_context
    answers = (
        None
        if context is None
        else build_plan_answers(texts, decision_count=len(context.decisions))
    )
    if answers is None:
        raise _bad_request("the answers must cover every decision")
    return answers


def accept_action(
    actions: ResumeActions, kind: ResumeClassification, raw_run_id: str, body: object
) -> dict[str, object]:
    """Check one approve or answer request and store it. Returns the ``202`` body.

    Raises :class:`ActionRejected` for every refusal, and nothing has been written then.
    The order is the contract: the body (``400``), the run id (``400``) and the run
    (``404``), then the conflicts (``409``) in :func:`_conflict_reason` order, and last
    the create-only write, where an existing request is a ``409`` too.
    """
    fields = _parse_fields(kind, body)
    if not is_valid_run_id(raw_run_id):
        raise _bad_request("the run id is not valid")
    run = actions.run_reader(raw_run_id)
    if run is None:
        raise ActionRejected(HTTPStatus.NOT_FOUND, "not found")
    now = actions.clock()
    reason = _conflict_reason(actions, run, kind, fields, now)
    if reason is not None:
        raise _conflict(reason)
    escalation = run.escalation
    assert escalation is not None  # a run that awaits a human has one
    answers = (
        _plan_answers(escalation, fields.answers)
        if kind is ResumeClassification.PLAN_DECISION
        else []
    )
    request = DashboardResumeRequest(
        run_id=raw_run_id,
        episode_id=fields.episode_id,
        context_fingerprint=fields.context_fingerprint,
        action=kind,
        answers=answers,
        created_at=now,
    )
    result = actions.requester(raw_run_id, request)
    if result == "run_missing":
        raise ActionRejected(HTTPStatus.NOT_FOUND, "not found")
    if result == "exists":
        raise _conflict("existing_request")
    return {"status": "accepted", "requested_at": now.isoformat()}
