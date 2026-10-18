"""The escalation reply protocol: the shared reply grammar and the one reply gate.

A leaf: it imports only the standard library and :mod:`.models` (and the config type, for
annotations). The escalation controller, which pulls in the GitHub client, and the read-only
dashboard both import it, so they cannot drift apart on what a reply looks like, on
whether one is read, or on which code a halt reason selects.

:func:`reply_closed_cause` is the one reply gate. The reply poller and the reply validator in
:mod:`.escalation` call it and act on the cause it returns, and the dashboard shows that
cause. The stored-context validity check (``is_valid_risk_approval_context`` and
``is_valid_plan_decision_context``) lives in :mod:`.resume` and stays out of the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from .models import EPISODE_ID_PATTERN as EPISODE_ID_PATTERN
from .models import MAX_PLAN_DECISIONS as MAX_PLAN_DECISIONS
from .models import (
    PUBLISH_FAILED_HALT_REASON,
    REPLY_CURSOR_CLOSED,
    STALLED_HALT_REASON,
    STOPPED_HALT_REASON,
    UNRESOLVED_DECISIONS_HALT_REASON,
    EscalationRecord,
    EscalationStatus,
    HaltReasonCode,
)

if TYPE_CHECKING:
    from .config import EscalationConfig

#: Characters allowed in a run id or an episode id inside a reply command.
_ID_CHARS = "A-Za-z0-9._-"

# ``EPISODE_ID_PATTERN`` (an episode id as a whole) and ``MAX_PLAN_DECISIONS`` live in
# ``models`` and are re-exported here. The command patterns below do not cap the length.

RESUME_COMMAND_PATTERN = re.compile(
    rf"^@factory\s+resume\s+v1\s+run=(?P<run>[{_ID_CHARS}]+)\s+episode=(?P<episode>[{_ID_CHARS}]+)$"
)
ANSWER_COMMAND_PATTERN = re.compile(
    rf"^@factory answer v1 run=(?P<run>[{_ID_CHARS}]+) episode=(?P<episode>[{_ID_CHARS}]+)$"
)


def format_resume_command(run_id: str, episode_id: str) -> str:
    """The reply that approves a risk halt. :data:`RESUME_COMMAND_PATTERN` matches it."""
    return f"@factory resume v1 run={run_id} episode={episode_id}"


def format_answer_command(run_id: str, episode_id: str) -> str:
    """The first line of a plan-decision reply. :data:`ANSWER_COMMAND_PATTERN` matches it."""
    return f"@factory answer v1 run={run_id} episode={episode_id}"


class ReplyClosedCause(StrEnum):
    """Why a reply cannot reach a run. The value is the phrase the dashboard shows."""

    ESCALATION_OFF = "escalation replies are turned off"
    NOT_SENT_YET = "the notice is not sent yet"
    NOT_SENT = "the notice was not sent"
    NO_INSTRUCTIONS = "the notice has no reply instructions"
    CURSOR_CLOSED = "the factory stopped reading replies"
    WINDOW_EXPIRED = "the reply window expired"
    REOPEN_LIMIT = "the reopen limit is reached"
    HOST_NOT_ALLOWED = "the notice host is no longer allowed"
    STATUS_UNKNOWN = "the notice status is not known"
    ALREADY_RESUMED = "the run already resumed from a reply"


#: Why a reply cannot reach a run, by escalation status. The factory reads
#: replies only while the status is ``NOTIFIED``.
_STATUS_CAUSES: dict[EscalationStatus, ReplyClosedCause] = {
    EscalationStatus.PENDING_NOTIFICATION: ReplyClosedCause.NOT_SENT_YET,
    EscalationStatus.NOTIFICATION_FAILED: ReplyClosedCause.NOT_SENT,
    EscalationStatus.EXPIRED: ReplyClosedCause.WINDOW_EXPIRED,
    EscalationStatus.REOPENED: ReplyClosedCause.ALREADY_RESUMED,
    EscalationStatus.RESUMED: ReplyClosedCause.ALREADY_RESUMED,
}

#: Every phrase :func:`reply_closed_cause` can return.
REPLY_CLOSED_CAUSES: frozenset[ReplyClosedCause] = frozenset(ReplyClosedCause)


@dataclass(frozen=True, kw_only=True)
class ReplyPolicy:
    """The configured limits that decide whether a reply is read.

    One required value for every caller of :func:`reply_closed_cause` and
    :func:`.resume.resume_refusal_within`. There is no unknown limit: build it with
    :meth:`from_config`.
    """

    max_reopens: int
    reply_window_hours: int
    escalation_enabled: bool
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_config(cls, escalation: EscalationConfig) -> Self:
        """The policy of an ``escalation`` config. The one place a policy is built."""
        return cls(
            max_reopens=escalation.max_reopens,
            reply_window_hours=escalation.reply_window_hours,
            escalation_enabled=escalation.enabled,
            allowed_hosts=tuple(escalation.allowed_hosts),
        )

    def window_passed(self, record: EscalationRecord, now: datetime) -> bool:
        """Whether ``now`` is after the last moment the reply window of ``record`` is open."""
        return now > record.created_at + timedelta(hours=self.reply_window_hours)

    def reopens_left(self, record: EscalationRecord) -> bool:
        """Whether ``record`` can still be reopened: its count is below the limit."""
        return record.reopen_count < self.max_reopens

    def notice_host(self, record: EscalationRecord) -> str:
        """The host a reply is read from: the stored one, else the first allowed one."""
        return record.target_host or (self.allowed_hosts[0] if self.allowed_hosts else "github.com")

    def allows_host(self, host: str) -> bool:
        """Whether ``host`` is one of the allowed hosts, compared without regard to case."""
        return host.casefold() in {allowed.casefold() for allowed in self.allowed_hosts}


def reply_closed_cause(
    record: EscalationRecord, policy: ReplyPolicy, now: datetime
) -> ReplyClosedCause | None:
    """Why a reply to ``record`` is not read at ``now``, or ``None`` when it is.

    A reply is read only while escalation is enabled, the status is ``NOTIFIED``, the notice
    carries reply instructions, the reply cursor is open, the reply window has not passed,
    a reopen is left and the notice host is still allowed. The first check that fails names
    the cause, in that order. The stored decision context is not part of the gate.
    """
    if not policy.escalation_enabled:
        return ReplyClosedCause.ESCALATION_OFF
    if record.status is not EscalationStatus.NOTIFIED:
        return _STATUS_CAUSES.get(record.status, ReplyClosedCause.STATUS_UNKNOWN)
    if not record.remote_resume_enabled:
        return ReplyClosedCause.NO_INSTRUCTIONS
    if record.reply_cursor == REPLY_CURSOR_CLOSED:
        return ReplyClosedCause.CURSOR_CLOSED
    if policy.window_passed(record, now):
        return ReplyClosedCause.WINDOW_EXPIRED
    if not policy.reopens_left(record):
        return ReplyClosedCause.REOPEN_LIMIT
    if not policy.allows_host(policy.notice_host(record)):
        return ReplyClosedCause.HOST_NOT_ALLOWED
    return None


#: Halt reasons the controller writes with a fixed prefix, and the code each maps to. An
#: exact prefix comes before the word checks: a publish error text can hold "scope" or
#: "attempt".
_PREFIX_CODES = (
    (STALLED_HALT_REASON, HaltReasonCode.STALLED),
    (STOPPED_HALT_REASON, HaltReasonCode.MANUAL_INSPECTION),
    (UNRESOLVED_DECISIONS_HALT_REASON, HaltReasonCode.UNRESOLVED_DECISIONS),
    (PUBLISH_FAILED_HALT_REASON, HaltReasonCode.DELIVERY_INTERVENTION),
)

#: Words in a free-text halt reason, and the code they select, checked in order.
_WORD_CODES = (
    (("scope",), HaltReasonCode.SCOPE_REVIEW),
    (("budget", "attempt"), HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED),
    (("ci ",), HaltReasonCode.CI_INTERVENTION),
    (("publish", "pull request", "merge", "permission"), HaltReasonCode.DELIVERY_INTERVENTION),
    (("abandon", "interrupt", "workspace"), HaltReasonCode.RECOVERY_INTERVENTION),
)

_RISK_APPROVAL_REASON = re.compile(r"risk r[23] requires human approval")


def halt_code_for_reason(reason: str) -> HaltReasonCode:
    """The code a halt reason selects from its text alone, compared without regard to case.

    The one classifier of reason text: the GitHub notice and the dashboard both read it.
    ``REVIEW_IMPASSE`` comes from a stored artifact, so it is not selected here.
    """
    reason = reason.lower()
    prefix_code = next((code for prefix, code in _PREFIX_CODES if reason.startswith(prefix)), None)
    if prefix_code is not None:
        return prefix_code
    if _RISK_APPROVAL_REASON.fullmatch(reason):
        return HaltReasonCode.RISK_APPROVAL
    for words, code in _WORD_CODES:
        if any(word in reason for word in words) or (
            code is HaltReasonCode.CI_INTERVENTION and reason.startswith("ci")
        ):
            return code
    return HaltReasonCode.MANUAL_INSPECTION
