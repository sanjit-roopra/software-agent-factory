"""The refusals a dashboard write answers with: an HTTP status and a JSON body.

``handler.py`` raises them for transport faults (host, origin, token, content type, length,
JSON) and :mod:`.actions` raises them for the faults of the request itself (a bad field, an
unknown run, a conflict). Neither module imports the other's errors: both come from here, and
the handler turns any of them into the response and the audit event. Standard library only.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Literal

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


class WriteRejected(Exception):  # noqa: N818 - a response, not an error condition
    """A write the dashboard refuses. ``payload`` is the JSON body of the response."""

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
