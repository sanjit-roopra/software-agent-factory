"""The escalation reply protocol: the shared reply grammar and a mirror of the poller's gate.

A leaf: it imports only the standard library and :mod:`.models`. The
escalation controller, which pulls in the GitHub client, and the read-only
dashboard both import the grammar, so they cannot drift apart on what a reply
looks like.

The reply poller takes its context, window and reopen checks from
:func:`.resume.resume_refusal`. :func:`reply_closed_cause` mirrors that gate for the
dashboard, and a parity test keeps the two in line. The stored-context validity check
(``is_valid_risk_approval_context`` and ``is_valid_plan_decision_context``)
lives in :mod:`.resume` and stays out of this mirror.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from .models import EPISODE_ID_PATTERN as EPISODE_ID_PATTERN
from .models import EscalationRecord, EscalationStatus

#: Characters allowed in a run id or an episode id inside a reply command.
_ID_CHARS = "A-Za-z0-9._-"

#: The most numbered decisions one plan-decision reply can answer.
MAX_PLAN_DECISIONS = 24

# ``EPISODE_ID_PATTERN`` (an episode id as a whole) lives in ``models`` and is re-exported
# here. The command patterns below do not cap the length.

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


_ESCALATION_OFF = "escalation replies are turned off"
_NO_INSTRUCTIONS = "the notice has no reply instructions"
_CURSOR_CLOSED = "the factory stopped reading replies"
_WINDOW_EXPIRED = "the reply window expired"
_REOPEN_LIMIT = "the reopen limit is reached"
_HOST_NOT_ALLOWED = "the notice host is no longer allowed"
_STATUS_UNKNOWN = "the notice status is not known"
_ALREADY_RESUMED = "the run already resumed from a reply"

#: Why a reply cannot reach a run, by escalation status. The factory reads
#: replies only while the status is ``NOTIFIED``.
_STATUS_CAUSES: dict[EscalationStatus, str] = {
    EscalationStatus.PENDING_NOTIFICATION: "the notice is not sent yet",
    EscalationStatus.NOTIFICATION_FAILED: "the notice was not sent",
    EscalationStatus.EXPIRED: _WINDOW_EXPIRED,
    EscalationStatus.REOPENED: _ALREADY_RESUMED,
    EscalationStatus.RESUMED: _ALREADY_RESUMED,
}

#: Every phrase :func:`reply_closed_cause` can return.
REPLY_CLOSED_CAUSES: frozenset[str] = frozenset(
    {
        *_STATUS_CAUSES.values(),
        _ESCALATION_OFF,
        _NO_INSTRUCTIONS,
        _CURSOR_CLOSED,
        _WINDOW_EXPIRED,
        _REOPEN_LIMIT,
        _HOST_NOT_ALLOWED,
        _STATUS_UNKNOWN,
    }
)


def notice_host(record: EscalationRecord, allowed_hosts: Sequence[str]) -> str:
    """The host the poller would read replies from: the stored one, else the first allowed."""
    return record.target_host or (allowed_hosts[0] if allowed_hosts else "github.com")


def reply_closed_cause(
    record: EscalationRecord,
    *,
    max_reopens: int | None,
    reply_window_hours: float | None,
    enabled: bool | None,
    allowed_hosts: Sequence[str] | None,
    now: datetime,
) -> str | None:
    """Why the reply poller would ignore a reply to ``record`` at ``now``, or ``None``.

    This mirrors the accept checks of ``poll_escalation_reply`` and
    ``validate_reply_candidate`` in :mod:`software_agent_factory.escalation`: a reply is
    read only while escalation is enabled, the status is ``NOTIFIED``, the notice carries
    reply instructions, the reply cursor is open, the reply window has not passed, a reopen
    is left and the notice host is still allowed. A parity test keeps the mirror in line.
    The poller also checks the stored decision context; this predicate does not.
    A config value that is ``None`` is unknown and does not close the reply.
    The result is one short plain-English phrase from :data:`REPLY_CLOSED_CAUSES`.
    """
    if enabled is False:
        return _ESCALATION_OFF
    if record.status is not EscalationStatus.NOTIFIED:
        return _STATUS_CAUSES.get(record.status, _STATUS_UNKNOWN)
    if not record.remote_resume_enabled:
        return _NO_INSTRUCTIONS
    if record.reply_cursor == "closed":
        return _CURSOR_CLOSED
    if reply_window_hours is not None and now > record.created_at + timedelta(
        hours=reply_window_hours
    ):
        return _WINDOW_EXPIRED
    if max_reopens is not None and record.reopen_count >= max_reopens:
        return _REOPEN_LIMIT
    if allowed_hosts is not None and notice_host(record, allowed_hosts).casefold() not in {
        host.casefold() for host in allowed_hosts
    }:
        return _HOST_NOT_ALLOWED
    return None
