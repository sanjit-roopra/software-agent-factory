"""The "Needs you" view model: how an operator continues a halted run.

Pure: no I/O, and it imports only ``models`` and ``redaction`` (no
``escalation``, which pulls in the GitHub client). ``next_step`` reads a run
detail whose escalation block already went through the sanitizer allowlist.
It still redacts every free-text field itself, so it is safe on its own.

The reply text must match the two parsers in :mod:`software_agent_factory.escalation`
(``parse_resume_command`` and ``parse_plan_decision_answers``). The tests pin
that round trip. A run id, episode id or fingerprint that could not survive the
parser is never put into a reply: the step becomes
``remote_approval_unavailable`` instead.
"""

from __future__ import annotations

import re
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from ..models import EscalationStatus, ResumeClassification, WorkflowState
from ..redaction import bounded_reason, redact_secrets

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

#: The most numbered decisions ``parse_plan_decision_answers`` accepts.
MAX_DECISIONS = 24
ANSWER_PLACEHOLDER = "<answer>"

_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
_FINGERPRINT_PATTERN = re.compile(r"[A-Za-z0-9]{64}")
_RESUME_CLASSES = frozenset(item.value for item in ResumeClassification)

#: Why a reply cannot reach a halted run, by escalation status. The reply poller
#: (``escalation.poll_escalation_reply``) reads replies only while the status is
#: ``NOTIFIED``.
_CLOSED_REPLY_CAUSES: dict[str, str] = {
    EscalationStatus.PENDING_NOTIFICATION: "the notice is not sent yet",
    EscalationStatus.NOTIFICATION_FAILED: "the notice was not sent",
    EscalationStatus.EXPIRED: "the reply window expired",
    EscalationStatus.REOPENED: "the run already resumed from a reply",
    EscalationStatus.RESUMED: "the run already resumed from a reply",
}


def is_safe_https_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def is_opaque_id(value: Any) -> TypeGuard[str]:
    """A run or episode id the reply parsers accept."""
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def is_context_fingerprint(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _FINGERPRINT_PATTERN.fullmatch(value) is not None


def _text_list(value: Any) -> list[str] | None:
    """Redacted copy of a non-empty list of strings, else ``None``."""
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return [redact_secrets(item) for item in value]


def clean_approval_scope(value: Any) -> dict[str, Any] | None:
    """The approval scope with every text redacted, or ``None`` when it is malformed."""
    if not isinstance(value, dict):
        return None
    requested = value.get("decision_requested")
    lists = {
        key: _text_list(value.get(key))
        for key in ("authorized_actions", "unauthorized_actions", "conditions_in_force")
    }
    if not isinstance(requested, str) or not requested or None in lists.values():
        return None
    return {"decision_requested": redact_secrets(requested), **lists}


def clean_decisions(value: Any) -> list[str]:
    """The decision questions, redacted. Malformed input gives an empty list."""
    return _text_list(value) or []


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _empty(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "sentence": None,
        "reason_code": None,
        "resume_class": None,
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
    resume_class = escalation.get("resume_classification")
    step = _empty(kind)
    step.update(
        sentence=sentence,
        reason_code=code if isinstance(code, str) and code else None,
        resume_class=resume_class if resume_class in _RESUME_CLASSES else None,
        reopens_used=_int_or_none(escalation.get("reopen_count")),
        reopens_max=_int_or_none(escalation.get("reopen_max")),
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
        text, cut = bounded_reason(reason, run_id=_run_id(run))
        step["failure_reason"] = text
        step["failure_reason_truncated"] = cut or run.get("failure_reason_truncated") is True
    return step


def _unavailable(run: dict[str, Any], escalation: dict[str, Any], what: str) -> dict[str, Any]:
    target = _run_id(run) or "<run>"
    sentence = (
        f"{_reason_sentence(escalation)} {what} Inspect the run with `factory show {target}`."
    )
    return _halt_step("remote_approval_unavailable", sentence, escalation)


def _run_id(run: dict[str, Any]) -> str | None:
    candidate = run.get("run_id", run.get("id"))
    return candidate if is_opaque_id(candidate) else None


def _closed_reply_cause(escalation: dict[str, Any]) -> str | None:
    """Why the poller would ignore a reply now, or ``None`` when it would read it.

    The poller stops at a status other than ``NOTIFIED`` and at the reopen limit
    (``reopen_count >= max_reopens``). A reopen count or limit that is unknown
    does not close the reply.
    """
    status = escalation.get("status")
    if status != EscalationStatus.NOTIFIED:
        cause = _CLOSED_REPLY_CAUSES.get(status) if isinstance(status, str) else None
        return cause or "the notice status is not known"
    used = _int_or_none(escalation.get("reopen_count"))
    limit = _int_or_none(escalation.get("reopen_max"))
    if used is not None and limit is not None and used >= limit:
        return "the reopen limit is reached"
    return None


def _reply_ids(run: dict[str, Any], escalation: dict[str, Any]) -> tuple[str, str, str] | None:
    """Run id, episode id and fingerprint, all safe to put in a reply."""
    run_id = _run_id(run)
    episode_id = escalation.get("episode_id")
    fingerprint = escalation.get("context_fingerprint")
    if run_id is None or not is_opaque_id(episode_id) or not is_context_fingerprint(fingerprint):
        return None
    return run_id, episode_id, fingerprint


def _approve(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    closed = _closed_reply_cause(escalation)
    if closed is not None:
        return _unavailable(run, escalation, f"Remote approval is not available because {closed}.")
    ids = _reply_ids(run, escalation)
    scope = clean_approval_scope(escalation.get("approval_scope"))
    if ids is None or scope is None:
        return _unavailable(run, escalation, "Remote approval is not available.")
    run_id, episode_id, fingerprint = ids
    step = _halt_step("approve", _reason_sentence(escalation), escalation)
    step.update(
        approval_scope={
            "decision_requested": scope["decision_requested"],
            "authorized_actions": scope["authorized_actions"],
            "excluded_actions": scope["unauthorized_actions"],
            "conditions_in_force": scope["conditions_in_force"],
        },
        episode_id=episode_id,
        context_fingerprint=fingerprint,
        reply_text=f"@factory resume v1 run={run_id} episode={episode_id}",
    )
    return step


def _answer(run: dict[str, Any], escalation: dict[str, Any]) -> dict[str, Any]:
    closed = _closed_reply_cause(escalation)
    if closed is not None:
        return _unavailable(run, escalation, f"Remote answers are not available because {closed}.")
    ids = _reply_ids(run, escalation)
    questions = clean_decisions(escalation.get("decisions"))
    if ids is None or not 1 <= len(questions) <= MAX_DECISIONS:
        return _unavailable(run, escalation, "Remote answers are not available.")
    run_id, episode_id, fingerprint = ids
    template = [f"{n}. {ANSWER_PLACEHOLDER}" for n in range(1, len(questions) + 1)]
    step = _halt_step("answer", _reason_sentence(escalation), escalation)
    step.update(
        decisions=[{"number": n, "question": q} for n, q in enumerate(questions, start=1)],
        episode_id=episode_id,
        context_fingerprint=fingerprint,
        reply_text="\n".join([f"@factory answer v1 run={run_id} episode={episode_id}", *template]),
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
    resume_class = escalation.get("resume_classification")
    if resume_class == ResumeClassification.RISK_APPROVAL:
        return _approve(run, escalation)
    if resume_class == ResumeClassification.PLAN_DECISION:
        return _answer(run, escalation)
    return _cannot_continue(run, _reason_sentence(escalation), escalation)
