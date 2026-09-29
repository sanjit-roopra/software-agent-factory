"""Role/purpose -> tool-capability mapping shared by agent runtimes.

Every ``AgentRuntime`` decides what one agent call is allowed to do (read
only, implementer writes, no tools at all, or web research) from the same
request shape. This module names that decision once; each runtime then maps
the resulting :class:`AgentCapability` onto its own tool-name vocabulary
(Copilot's ``_permission_profile`` today, pi's tool allowlist from Slice 3 of
``plans/pi-agent-runtime.md``).
"""

from __future__ import annotations

from enum import Enum

from .agents import AgentRequest
from .models import AgentPurpose, AgentRole


class AgentCapability(str, Enum):
    """What an agent call may do, independent of a runtime's tool-name syntax."""

    READ_ONLY = "READ_ONLY"
    IMPLEMENTER_WRITE = "IMPLEMENTER_WRITE"
    NO_TOOLS = "NO_TOOLS"
    WEB_RESEARCH = "WEB_RESEARCH"


def capability_for(request: AgentRequest) -> AgentCapability:
    """Return the capability a request is entitled to, independent of runtime."""
    if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
        return AgentCapability.NO_TOOLS
    if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
        return AgentCapability.WEB_RESEARCH
    if request.role is AgentRole.IMPLEMENTER:
        return AgentCapability.IMPLEMENTER_WRITE
    return AgentCapability.READ_ONLY
