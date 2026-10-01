"""The "Needs you" view model: how an operator continues a halted run.

Pure: no I/O, and it imports only ``models`` and the leaves ``escalation_protocol`` and
``validators`` (no ``escalation``, which pulls in the GitHub client). ``next_step`` reads a
run detail that already went through :mod:`software_agent_factory.dashboard.sanitize`. That
step redacted and bounded every free text, so this module copies text and never redacts it.

Whether a reply can reach the run is not decided here. The escalation block carries
``reply_closed_cause``, which :func:`software_agent_factory.escalation_protocol.reply_closed_cause`
computed from the stored record and the config. A block without it is treated as closed.

The reply text must match the two parsers in :mod:`software_agent_factory.escalation`
(``parse_resume_command`` and ``parse_plan_decision_answers``). The tests pin
that round trip. A run id, episode id or fingerprint that could not survive the
parser is never put into a reply: the step becomes
``remote_approval_unavailable`` instead.
"""

from __future__ import annotations

from typing import Any

from ..escalation_protocol import MAX_PLAN_DECISIONS, format_answer_command, format_resume_command
from ..models import ResumeClassification, WorkflowState
from .validators import (
    RESUME_CLASSIFICATIONS,
    is_context_fingerprint,
    is_count,
    is_episode_id,
    is_safe_https_url,
    run_id_of,
)

#: Why a run stopped, one plain sentence per halt reason code.
REASON_SENTENCES: dict[str, str] = {
    "RISK_APPROVAL": "The run stopped because its risk level needs a person to approve it.",
    "UNRESOLVED_DECISIONS": "The run stopped because its plan needs decisions from a person.",
    "SCOPE_REVIEW": "The run stopped because its changes went beyond the approved scope.",
    "REVIEW_IMPASSE": "The run stopped because the review did not settle on an answer.",
    "ATTEMPT_BUDGET_EXHAUSTED": "The run stopped because it used all of its retry attempts.",
    "CI_INTERVENTION": "The run stopped because CI could not pass or be repaired by itself.",
    "DELIVERY_INTERVENTION": "The run stopped because it could not deliver the pull request.",
    "RECOVERY_INTERVENTION": "The run stopped because it could not safely recover its workspace.",
    "MANUAL_INSPECTION": "The run stopped at a point where a person must decide.",
}
FALLBACK_SENTENCE = "The run stopped and needs a person to look at it."
FAILED_SENTENCE = "The run failed."
CANNOT_CONTINUE = "This run cannot continue."

ANSWER_PLACEHOLDER = "<answer>"

#: The cause shown when the escalation block does not say whether a reply is open.
UNKNOWN_REPLY_STATE = "the reply state is not known"


def _count_or_none(value: Any) -> int | None:
    return value if is_count(value) else None


def _empty(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "sentence": None,
        "reason_code": None,
        "resume_classification": None,
        "approval_scope": None,
        "decisions": [],
        "reopens_used": None,
        "reopens_max": None,
        "comment_url": None,
        "episode_id": None,
        "context_fingerprint": None,
        "reply_text": None,
        "failure_reason": None,
        "failure_reason_truncated": False,
    }


def _reason_sentence(escalation: dict[str, Any]) -> str:
    code = escalation.get("reason_code")
    return (
        REASON_SENTENCES.get(code, FALLBACK_SENTENCE)
        if isinstance(code, str)
        else FALLBACK_SENTENCE
    )


def _halt_step(kind: str, sentence: str, escalation: dict[str, Any]) -> dict[str, Any]:
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
        reopens_max=_count_or_none(escalation.get("reopen_max")),
        comment_url=(
            escalation["comment_url"] if is_safe_https_url(escalation.get("comment_url")) else None
        ),
    )
    return step


def _cannot_continue(
    run: dict[str, Any], sentence: str, escalation: dict[str, Any]
) -> dict[str, Any]:
    step = _halt_step("cannot_continue", f"{sentence} {CANNOT_CONTINUE}", escalation)
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
    return _halt_step("remote_approval_unavailable", sentence, escalation)


def _closed_reply_cause(escalation: dict[str, Any]) -> str | None:
    """Why the factory would ignore a reply now, or ``None`` when it would read it.

    The cause comes from :func:`software_agent_factory.escalation_protocol.reply_closed_cause`,
    computed with the config when the run detail is built. A block without that field
    is treated as closed, because the reply state is then unknown.
    """
    if "reply_closed_cause" not in escalation:
        return UNKNOWN_REPLY_STATE
    cause = escalation["reply_closed_cause"]
    return cause if isinstance(cause, str) and cause else None


def _reply_ids(run: dict[str, Any], escalation: dict[str, Any]) -> tuple[str, str, str] | None:
    """Run id, episode id and fingerprint, all safe to put in a reply."""
    run_id = run_id_of(run)
    episode_id = escalation.get("episode_id")
    fingerprint = escalation.get("context_fingerprint")
    if run_id is None or not is_episode_id(episode_id) or not is_context_fingerprint(fingerprint):
        return None
    return run_id, episode_id, fingerprint


def _approve(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    closed = _closed_reply_cause(escalation)
    if closed is not None:
        return _unavailable(run, escalation, f"Remote approval is not available because {closed}.")
    ids = _reply_ids(run, escalation)
    scope = escalation.get("approval_scope")
    if ids is None or not isinstance(scope, dict):
        return _unavailable(run, escalation, "Remote approval is not available.")
    run_id, episode_id, fingerprint = ids
    step = _halt_step("approve", _reason_sentence(escalation), escalation)
    step.update(
        approval_scope=scope,
        episode_id=episode_id,
        context_fingerprint=fingerprint,
        reply_text=format_resume_command(run_id, episode_id),
    )
    return step


def _answer(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    closed = _closed_reply_cause(escalation)
    if closed is not None:
        return _unavailable(run, escalation, f"Remote answers are not available because {closed}.")
    ids = _reply_ids(run, escalation)
    questions = escalation.get("decisions")
    if (
        ids is None
        or not isinstance(questions, list)
        or not 1 <= len(questions) <= MAX_PLAN_DECISIONS
    ):
        return _unavailable(run, escalation, "Remote answers are not available.")
    run_id, episode_id, fingerprint = ids
    template = [f"{n}. {ANSWER_PLACEHOLDER}" for n in range(1, len(questions) + 1)]
    step = _halt_step("answer", _reason_sentence(escalation), escalation)
    step.update(
        decisions=[{"number": n, "question": q} for n, q in enumerate(questions, start=1)],
        episode_id=episode_id,
        context_fingerprint=fingerprint,
        reply_text="\n".join([format_answer_command(run_id, episode_id), *template]),
    )
    return step


def next_step(run: dict[str, Any]) -> dict[str, Any]:
    """What the operator must do to continue ``run``, or ``kind == "none"``."""
    state = run.get("state")
    raw = run.get("escalation")
    escalation: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if state == WorkflowState.FAILED:
        return _cannot_continue(run, FAILED_SENTENCE, {})
    if state != WorkflowState.NEEDS_HUMAN:
        return _empty("none")
    resume_classification = escalation.get("resume_classification")
    if resume_classification == ResumeClassification.RISK_APPROVAL:
        return _approve(run, escalation)
    if resume_classification == ResumeClassification.PLAN_DECISION:
        return _answer(run, escalation)
    return _cannot_continue(run, _reason_sentence(escalation), escalation)
