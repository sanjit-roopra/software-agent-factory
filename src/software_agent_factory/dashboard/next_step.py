"""The "Needs you" view model: how an operator continues a halted run.

Pure: no I/O, and it imports only ``models`` and the leaves ``escalation_protocol`` and
``validators`` (no ``escalation``, which pulls in the GitHub client). ``next_step`` reads a
run detail that already went through :mod:`software_agent_factory.dashboard.sanitize`. That
step redacted and bounded every free text, so this module copies text and never redacts it.

Whether the page may offer an action is not decided here. The escalation block carries
``dashboard_action_refusal``, which :func:`software_agent_factory.resume.resume_refusal_within`
computed from the stored run and the config: the rules the service applies to a dashboard
request. A block without it, or with an unknown value, is treated as refused.

Whether a GitHub reply can reach the run is a separate question. The block carries
``reply_closed_cause``, which :func:`software_agent_factory.escalation_protocol.reply_closed_cause`
computed. The copyable reply text and the comment link show only while that is ``None``; the
dashboard action does not depend on it.

A request the dashboard queued is read through the injected ``ResumeRequestReader``. Only its
status, stale reason, action, context fingerprint and time are used; the answers in it are never
read, so they cannot reach the page.

The reply text must match the two parsers in :mod:`software_agent_factory.escalation`
(``parse_resume_command`` and ``parse_plan_decision_answers``). The tests pin
that round trip. A run id, episode id or fingerprint that could not survive the
parser is never put into a reply: the step becomes
``remote_approval_unavailable`` instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ..escalation_protocol import MAX_PLAN_DECISIONS, format_answer_command, format_resume_command
from ..models import HaltReasonCode, ResumeClassification, WorkflowState
from .validators import (
    RESUME_CLASSIFICATIONS,
    is_context_fingerprint,
    is_count,
    is_episode_id,
    is_safe_https_url,
    run_id_of,
)

#: Why a run stopped, one plain sentence per halt reason code.
REASON_SENTENCES: dict[HaltReasonCode, str] = {
    HaltReasonCode.RISK_APPROVAL: (
        "The run stopped because its risk level needs a person to approve it."
    ),
    HaltReasonCode.UNRESOLVED_DECISIONS: (
        "The run stopped because its plan needs decisions from a person."
    ),
    HaltReasonCode.SCOPE_REVIEW: (
        "The run stopped because its changes went beyond the approved scope."
    ),
    HaltReasonCode.REVIEW_IMPASSE: (
        "The run stopped because the review did not settle on an answer."
    ),
    HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED: (
        "The run stopped because it used all of its retry attempts."
    ),
    HaltReasonCode.CI_INTERVENTION: (
        "The run stopped because CI could not pass or be repaired by itself."
    ),
    HaltReasonCode.DELIVERY_INTERVENTION: (
        "The run stopped because it could not deliver the pull request."
    ),
    HaltReasonCode.RECOVERY_INTERVENTION: (
        "The run stopped because it could not safely recover its workspace."
    ),
    HaltReasonCode.MANUAL_INSPECTION: "The run stopped at a point where a person must decide.",
}
FALLBACK_SENTENCE = "The run stopped and needs a person to look at it."
CANNOT_CONTINUE = "This run cannot continue."

ANSWER_PLACEHOLDER = "<answer>"
START_HINT = "If factory start is not running, start it."

#: The cause shown when the escalation block does not say whether a reply is open.
UNKNOWN_REPLY_STATE = "the reply state is not known"
#: The cause shown when the escalation block does not say whether the dashboard may act.
UNKNOWN_ACTION_STATE = "the run state is not known"
#: The cause shown when the reopen count has reached its maximum. It is one of
#: ``escalation_protocol.REPLY_CLOSED_CAUSES``; a test pins that.
REOPEN_LIMIT_CAUSE = "the reopen limit is reached"

#: One sentence per reason the service gave for refusing a dashboard request.
STALE_SENTENCES: dict[str, str] = {
    "expired": "approval expired, approve again",
    "reopen_limit": "reopen limit reached, inspect with factory show",
    "context_changed": "the run changed, review again",
    "state_changed": "the run state changed, review again",
}
#: The same, for a request that carried plan answers: only the expired sentence differs.
ANSWER_STALE_SENTENCES: dict[str, str] = dict(
    STALE_SENTENCES, expired="answers expired, send them again"
)
#: The same, for a request to publish again.
RETRY_STALE_SENTENCES: dict[str, str] = dict(STALE_SENTENCES, expired="retry expired, retry again")
_STALE_SENTENCES_BY_ACTION: dict[str, dict[str, str]] = {
    ResumeClassification.RISK_APPROVAL: STALE_SENTENCES,
    ResumeClassification.PLAN_DECISION: ANSWER_STALE_SENTENCES,
    ResumeClassification.DELIVERY_RETRY: RETRY_STALE_SENTENCES,
}

#: Why the page offers no action, one phrase per refusal code. Two reuse the reply phrases.
REFUSAL_CAUSES: dict[str, str] = {
    "expired": "the reply window expired",
    "reopen_limit": REOPEN_LIMIT_CAUSE,
    "context_changed": "the run changed",
    "state_changed": "the run state changed",
}


class NextStepKind(StrEnum):
    """Every ``kind`` a next step can have: the one list the page and the tests share."""

    NONE = "none"
    CANNOT_CONTINUE = "cannot_continue"
    REMOTE_APPROVAL_UNAVAILABLE = "remote_approval_unavailable"
    APPROVE = "approve"
    ANSWER = "answer"
    RETRY = "retry"
    QUEUED = "queued"


@dataclass(frozen=True)
class _Request:
    """The few facts of a dashboard request the page may use. Never its answers."""

    action: str
    fingerprint: str
    status: str
    reason: str | None
    created_at: datetime


def _count_or_none(value: Any) -> int | None:
    return value if is_count(value) else None


def _empty(kind: NextStepKind) -> dict[str, Any]:
    return {
        "kind": kind,
        "sentence": None,
        "reason_code": None,
        "resume_classification": None,
        "approval_scope": None,
        "decisions": [],
        "reopens_used": None,
        "max_reopens": None,
        "comment_url": None,
        "episode_id": None,
        "context_fingerprint": None,
        "reply_text": None,
        "failure_reason": None,
        "failure_reason_truncated": False,
        "requested_at": None,
        "stale_sentence": None,
    }


def _reason_sentence(escalation: dict[str, Any]) -> str:
    code = HaltReasonCode.parse(escalation.get("reason_code"))
    return REASON_SENTENCES.get(code, FALLBACK_SENTENCE) if code is not None else FALLBACK_SENTENCE


def _comment_url(escalation: dict[str, Any]) -> str | None:
    """The link to the GitHub comment, only while a reply there would be read."""
    url = escalation.get("comment_url")
    return url if _reply_open(escalation) and is_safe_https_url(url) else None


def _halt_step(kind: NextStepKind, sentence: str, escalation: dict[str, Any]) -> dict[str, Any]:
    """A step for a halted run: the facts every kind shows."""
    code = escalation.get("reason_code")
    resume_classification = escalation.get("resume_classification")
    step = _empty(kind)
    step.update(
        sentence=sentence,
        reason_code=code if isinstance(code, str) and code else None,
        resume_classification=(
            resume_classification if resume_classification in RESUME_CLASSIFICATIONS else None
        ),
        reopens_used=_count_or_none(escalation.get("reopen_count")),
        max_reopens=_count_or_none(escalation.get("max_reopens")),
        comment_url=_comment_url(escalation),
    )
    return step


def _cannot_continue(
    run: dict[str, Any], sentence: str, escalation: dict[str, Any]
) -> dict[str, Any]:
    step = _halt_step(NextStepKind.CANNOT_CONTINUE, f"{sentence} {CANNOT_CONTINUE}", escalation)
    reason = run.get("failure_reason")
    if isinstance(reason, str) and reason:
        step["failure_reason"] = reason
        step["failure_reason_truncated"] = run.get("failure_reason_truncated") is True
    return step


def _unavailable(run: dict[str, Any], escalation: dict[str, Any], what: str) -> dict[str, Any]:
    target = run_id_of(run) or "<run>"
    sentence = (
        f"{_reason_sentence(escalation)} {what} Inspect the run with `factory show {target}`."
    )
    return _halt_step(NextStepKind.REMOTE_APPROVAL_UNAVAILABLE, sentence, escalation)


def _reply_open(escalation: dict[str, Any]) -> bool:
    """Whether the factory would read a GitHub reply now.

    The cause comes from :func:`software_agent_factory.escalation_protocol.reply_closed_cause`,
    computed with the config when the run detail is built. Only ``None`` means open. A block
    without that field, or with any other value, is treated as closed.
    """
    return escalation.get("reply_closed_cause", UNKNOWN_REPLY_STATE) is None


def _action_refusal_cause(escalation: dict[str, Any]) -> str | None:
    """Why the page offers no action, or ``None`` when the service would take a request.

    ``dashboard_action_refusal`` must be present: ``None`` means the service would accept,
    one of the four codes means it would not. A missing or unknown value is refused.
    """
    refusal = escalation.get("dashboard_action_refusal", UNKNOWN_ACTION_STATE)
    if refusal is None:
        return None
    if isinstance(refusal, str) and refusal in REFUSAL_CAUSES:
        return REFUSAL_CAUSES[refusal]
    return UNKNOWN_ACTION_STATE


def _reply_ids(run: dict[str, Any], escalation: dict[str, Any]) -> tuple[str, str, str] | None:
    """Run id, episode id and fingerprint, all safe to put in a reply."""
    run_id = run_id_of(run)
    episode_id = escalation.get("episode_id")
    fingerprint = escalation.get("context_fingerprint")
    if run_id is None or not is_episode_id(episode_id) or not is_context_fingerprint(fingerprint):
        return None
    return run_id, episode_id, fingerprint


def _github_reply(
    step: dict[str, Any], escalation: dict[str, Any], reply_text: str
) -> dict[str, Any]:
    """Add the copyable reply when a GitHub reply would be read."""
    if _reply_open(escalation):
        step["reply_text"] = reply_text
    return step


def _approve(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    refused = _action_refusal_cause(escalation)
    if refused is not None:
        return _unavailable(run, escalation, f"Approval is not available because {refused}.")
    ids = _reply_ids(run, escalation)
    scope = escalation.get("approval_scope")
    if ids is None or not isinstance(scope, dict):
        return _unavailable(run, escalation, "Approval is not available.")
    run_id, episode_id, fingerprint = ids
    step = _halt_step(NextStepKind.APPROVE, _reason_sentence(escalation), escalation)
    step.update(approval_scope=scope, episode_id=episode_id, context_fingerprint=fingerprint)
    return _github_reply(step, escalation, format_resume_command(run_id, episode_id))


def _answer(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    refused = _action_refusal_cause(escalation)
    if refused is not None:
        return _unavailable(run, escalation, f"Answers are not available because {refused}.")
    ids = _reply_ids(run, escalation)
    questions = escalation.get("decisions")
    if (
        ids is None
        or not isinstance(questions, list)
        or not 1 <= len(questions) <= MAX_PLAN_DECISIONS
    ):
        return _unavailable(run, escalation, "Answers are not available.")
    run_id, episode_id, fingerprint = ids
    template = [f"{n}. {ANSWER_PLACEHOLDER}" for n in range(1, len(questions) + 1)]
    step = _halt_step(NextStepKind.ANSWER, _reason_sentence(escalation), escalation)
    step.update(
        decisions=[{"number": n, "question": q} for n, q in enumerate(questions, start=1)],
        episode_id=episode_id,
        context_fingerprint=fingerprint,
    )
    reply = "\n".join([format_answer_command(run_id, episode_id), *template])
    return _github_reply(step, escalation, reply)


def _retry(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    refused = _action_refusal_cause(escalation)
    if refused is not None:
        return _unavailable(run, escalation, f"Retry is not available because {refused}.")
    ids = _reply_ids(run, escalation)
    if ids is None:
        return _unavailable(run, escalation, "Retry is not available.")
    _, episode_id, fingerprint = ids
    step = _halt_step(NextStepKind.RETRY, _reason_sentence(escalation), escalation)
    step.update(episode_id=episode_id, context_fingerprint=fingerprint)
    return step


def _parsed_requests(raw: Iterable[Any]) -> list[_Request]:
    """The requests that have the expected shape. Anything else is dropped."""
    requests: list[_Request] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status, reason = item.get("status"), item.get("reason")
        created_at = _parse_time(item.get("created_at"))
        if (
            status not in ("pending", "stale")
            or item.get("action") not in RESUME_CLASSIFICATIONS
            or not is_context_fingerprint(item.get("context_fingerprint"))
            or (status == "stale") != (reason in STALE_SENTENCES)
            or created_at is None
        ):
            continue
        requests.append(
            _Request(
                action=item["action"],
                fingerprint=item["context_fingerprint"],
                status=status,
                reason=reason,
                created_at=created_at,
            )
        )
    return sorted(requests, key=lambda request: request.created_at)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _queued_sentence(request: _Request) -> str:
    moment = request.created_at.strftime("%Y-%m-%d %H:%M UTC")
    if request.action == ResumeClassification.RISK_APPROVAL:
        return f"Approved at {moment}, queued for the factory service. {START_HINT}"
    if request.action == ResumeClassification.DELIVERY_RETRY:
        return f"Retry requested at {moment}, queued for the factory service. {START_HINT}"
    return f"Answers sent at {moment}, queued for the factory service"


def _queued_request(
    requests: list[_Request], classification: Any, fingerprint: str
) -> _Request | None:
    """The pending request for this halt's action and context, if the dashboard queued one."""
    for request in requests:
        if (request.status, request.action, request.fingerprint) == (
            "pending",
            classification,
            fingerprint,
        ):
            return request
    return None


def _stale_sentence(request: _Request) -> str:
    return _STALE_SENTENCES_BY_ACTION[request.action][str(request.reason)]


def _resume_step(
    run: dict[str, Any], escalation: dict[str, Any], requests: list[_Request]
) -> dict[str, Any]:
    """The step for a resumable halt, with any dashboard request."""
    classification = escalation.get("resume_classification")
    ids = _reply_ids(run, escalation)
    queued = _queued_request(requests, classification, ids[2]) if ids is not None else None
    if queued is not None:
        step = _halt_step(NextStepKind.QUEUED, _queued_sentence(queued), escalation)
        step.update(
            episode_id=escalation.get("episode_id"),
            context_fingerprint=queued.fingerprint,
            requested_at=queued.created_at.isoformat(),
        )
        return step
    if classification == ResumeClassification.RISK_APPROVAL:
        step = _approve(run, escalation)
    elif classification == ResumeClassification.DELIVERY_RETRY:
        step = _retry(run, escalation)
    else:
        step = _answer(run, escalation)
    stale = [r for r in requests if r.status == "stale" and r.action == classification]
    if stale:
        step["stale_sentence"] = _stale_sentence(stale[-1])
    return step


def next_step(run: dict[str, Any], requests: Iterable[Any] = ()) -> dict[str, Any]:
    """What the operator must do to continue ``run``, or ``kind == "none"``.

    ``requests`` are the dashboard requests of the run's current episode, oldest first. A
    failed run needs nothing from the operator: the page shows its failure reason in the run
    outcome, so its step is ``none``.
    """
    state = run.get("state")
    raw = run.get("escalation")
    escalation: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if state != WorkflowState.NEEDS_HUMAN:
        return _empty(NextStepKind.NONE)
    if escalation.get("resume_classification") in (
        ResumeClassification.RISK_APPROVAL,
        ResumeClassification.PLAN_DECISION,
        ResumeClassification.DELIVERY_RETRY,
    ):
        return _resume_step(run, escalation, _parsed_requests(requests))
    return _cannot_continue(run, _reason_sentence(escalation), escalation)
