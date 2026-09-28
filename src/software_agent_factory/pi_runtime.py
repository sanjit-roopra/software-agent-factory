"""``pi`` (``@earendil-works/pi-coding-agent``) agent runtime.

Step 2.2 of ``plans/pi-agent-runtime.md`` added only the shape of this
runtime: :class:`PiAgentRuntime` stores its configuration and data
directory, and :meth:`PiAgentRuntime.run` returns a failed
:class:`~software_agent_factory.agents.AgentResult` naming itself as not yet
implemented. Step 3.2 adds the private helpers ``run`` will use once Step 3.3
wires the JSONL RPC call to the ``pi`` executable: the command line
(``_build_command``), the working directory (``_cwd_for``, the same rule the
Copilot runtime uses) and the scrubbed child environment (``_child_env``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_capabilities import AgentCapability, capability_for
from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import AgentPurpose
from .subprocess_utils import build_child_env

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .config import PiConfig

logger = logging.getLogger(__name__)

#: Returned by the Step 2.2 stub. Slice 3 replaces ``run`` with the real
#: pi RPC call, at which point this constant is removed.
_NOT_YET_IMPLEMENTED_REASON = "pi runtime not yet implemented"

#: Pi ``--tools`` value per :class:`AgentCapability`, per the "Role tool
#: allowlists (v1)" table in ``docs/specs/pi-agent-runtime.md``.
#: ``AgentCapability.WEB_RESEARCH`` has no entry: pi has no web-fetch tool,
#: so :meth:`PiAgentRuntime._build_command` raises before this lookup runs.
_TOOL_ARGS: dict[AgentCapability, tuple[str, ...]] = {
    AgentCapability.IMPLEMENTER_WRITE: ("--tools", "read,bash,edit,write,grep,find,ls"),
    AgentCapability.READ_ONLY: ("--tools", "read,grep,find,ls"),
    AgentCapability.NO_TOOLS: ("--no-tools",),
}


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

    def _build_command(
        self,
        request: AgentRequest,
        *,
        session_arg: Sequence[str],
    ) -> list[str]:
        """Build the ``pi --mode rpc`` command line for one agent call.

        ``session_arg`` is ``["--no-session"]`` or ``["--session", <path>]``;
        Slice 6 supplies the persisted-session path, so every caller in this
        slice passes ``["--no-session"]``.
        """
        capability = capability_for(request)
        if capability is AgentCapability.WEB_RESEARCH:
            raise ValueError("repository skill generation is not supported on pi")
        logger.debug(
            "ignoring context_tier=%s for pi request (no --context equivalent)",
            request.context_tier,
        )
        return [
            self._config.executable,
            "--mode",
            "rpc",
            "--provider",
            self._config.provider,
            "--model",
            request.model,
            "--thinking",
            request.reasoning,
            *_TOOL_ARGS[capability],
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            *session_arg,
        ]

    def _cwd_for(self, request: AgentRequest) -> Path:
        """Resolve the working directory for one pi call.

        Same rule as the Copilot runtime's ``_cwd_for``
        (``copilot_runtime.py``): the request's workspace when supplied,
        otherwise the process cwd -- except ``CORRECT_CHANGE_SET`` and
        ``GENERATE_REPOSITORY_SKILL`` always require an explicit workspace.
        """
        if request.workspace_path:
            return Path(request.workspace_path).expanduser().resolve()
        if request.purpose is AgentPurpose.CORRECT_CHANGE_SET:
            raise ValueError("ChangeSet correction requires workspace_path")
        if request.purpose is AgentPurpose.GENERATE_REPOSITORY_SKILL:
            raise ValueError(
                "repository skill generation requires workspace_path (the neutral run directory)"
            )
        return Path(os.getcwd()).expanduser().resolve()

    def _child_env(self) -> dict[str, str]:
        """Build the scrubbed environment pi's child process runs in.

        ``build_child_env`` scrubs :data:`subprocess_utils.GITHUB_CREDENTIAL_ENV_VARS`;
        ``COPILOT_GITHUB_TOKEN`` is not in that set, so it survives when set --
        pi's ``github-copilot`` provider uses ``~/.pi/agent/auth.json`` for an
        interactive login, but authenticates headless from that variable when
        present.
        """
        env, _scrubbed_values = build_child_env()
        env["PI_CACHE_RETENTION"] = self._config.cache_retention
        return env
