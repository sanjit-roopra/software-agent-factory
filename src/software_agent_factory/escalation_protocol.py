"""The escalation reply protocol: grammar and the rule for when a reply is read.

A leaf: it imports only the standard library and :mod:`.models`. The
escalation controller, which pulls in the GitHub client, and the read-only
dashboard both import it, so they cannot drift apart on what a reply looks
like or when the factory accepts one.
"""

from __future__ import annotations

import re

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
