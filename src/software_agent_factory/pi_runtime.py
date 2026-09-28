"""``pi`` (``@earendil-works/pi-coding-agent``) agent runtime.

Step 2.2 of ``plans/pi-agent-runtime.md`` adds only the shape of this
runtime: :class:`PiAgentRuntime` stores its configuration and data
directory, and :meth:`PiAgentRuntime.run` returns a failed
:class:`~software_agent_factory.agents.AgentResult` naming itself as not yet
implemented. Slice 3 of the plan replaces the body of ``run`` with the real
JSONL RPC call to the ``pi`` executable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agents import AgentRequest, AgentResult, AgentRuntime

if TYPE_CHECKING:
    from pathlib import Path

    from .config import PiConfig

#: Returned by the Step 2.2 stub. Slice 3 replaces ``run`` with the real
#: pi RPC call, at which point this constant is removed.
_NOT_YET_IMPLEMENTED_REASON = "pi runtime not yet implemented"


class PiAgentRuntime(AgentRuntime):
    """Production ``AgentRuntime`` backed by the ``pi`` CLI.

    ``config`` is the loaded ``pi:`` configuration block
    (:class:`~software_agent_factory.config.PiConfig`); ``data_dir`` is the
    factory's configured data directory, used by later slices for the
    per-work-item session store under ``<data_dir>/pi-sessions``.
    """

    def __init__(self, config: PiConfig, data_dir: Path) -> None:
        self._config = config
        self._data_dir = data_dir

    def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=request.role,
            success=False,
            failure_reason=_NOT_YET_IMPLEMENTED_REASON,
        )
