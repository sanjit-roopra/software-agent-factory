"""The escalation reply protocol: grammar and the rule for when a reply is read.

A leaf: it imports only the standard library and :mod:`.models`. The
escalation controller, which pulls in the GitHub client, and the read-only
dashboard both import it, so they cannot drift apart on what a reply looks
like or when the factory accepts one.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .models import EscalationRecord, EscalationStatus

#: Characters allowed in a run id or an episode id inside a reply command.
_ID_CHARS = "A-Za-z0-9._-"

#: The most numbered decisions one plan-decision reply can answer.
MAX_PLAN_DECISIONS = 24

#: An episode id as a whole. The command patterns below do not cap the length.
EPISODE_ID_PATTERN = re.compile(rf"[{_ID_CHARS}]{{1,128}}")

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


#: Why a reply cannot reach a run, by escalation status. The factory reads
#: replies only while the status is ``NOTIFIED``.
_STATUS_CAUSES: dict[EscalationStatus, str] = {
    EscalationStatus.PENDING_NOTIFICATION: "the notice is not sent yet",
    EscalationStatus.NOTIFICATION_FAILED: "the notice was not sent",
    EscalationStatus.EXPIRED: "the reply window expired",
    EscalationStatus.REOPENED: "the run already resumed from a reply",
    EscalationStatus.RESUMED: "the run already resumed from a reply",
}


def reply_closed_cause(
    record: EscalationRecord,
    *,
    max_reopens: int | None,
    reply_window_hours: float | None,
    enabled: bool | None,
    now: datetime,
) -> str | None:
    """Why the factory would ignore a reply to ``record`` at ``now``, or ``None``.

    This is the rule behind ``poll_escalation_reply`` and ``validate_reply_candidate``
    in :mod:`software_agent_factory.escalation`: a reply is read only while escalation
    is enabled, the status is ``NOTIFIED``, the notice carries reply instructions,
    the reply cursor is open, the reply window has not passed and a reopen is left.
    A config value that is ``None`` is unknown and does not close the reply.
    The result is one short plain-English phrase.
    """
    if enabled is False:
        return "escalation replies are turned off"
    if record.status is not EscalationStatus.NOTIFIED:
        return _STATUS_CAUSES.get(record.status, "the notice status is not known")
    if not record.remote_resume_enabled:
        return "the notice has no reply instructions"
    if record.reply_cursor == "closed":
        return "the factory stopped reading replies"
    if reply_window_hours is not None and now > record.created_at + timedelta(
        hours=reply_window_hours
    ):
        return "the reply window expired"
    if max_reopens is not None and record.reopen_count >= max_reopens:
        return "the reopen limit is reached"
    return None
