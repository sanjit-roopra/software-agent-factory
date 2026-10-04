"""Send each agent request to the runtime its role names (ADR-045).

A role without ``runtime`` uses the ``--runtime`` default. Each runtime is
built the first time a request needs it, so a run that never calls pi never
needs pi. The router stamps the serving runtime on every reported
:class:`~software_agent_factory.models.ModelUsage`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import RuntimeName

__all__ = ["RoutingAgentRuntime"]


class RoutingAgentRuntime:
    """An :class:`AgentRuntime` that delegates to one runtime per request."""

    def __init__(
        self,
        default: RuntimeName,
        factories: Mapping[RuntimeName, Callable[[], AgentRuntime]],
    ) -> None:
        self._default = default
        self._factories = factories
        self._built: dict[RuntimeName, AgentRuntime] = {}

    def run(self, request: AgentRequest) -> AgentResult:
        name = request.runtime or self._default
        runtime = self._built.get(name)
        if runtime is None:
            runtime = self._built[name] = self._factories[name]()
        result = runtime.run(request)
        if result.usage is None or not result.usage.model_usage:
            return result
        stamped = tuple(
            usage.model_copy(update={"runtime": name}) for usage in result.usage.model_usage
        )
        return result.model_copy(
            update={"usage": result.usage.model_copy(update={"model_usage": stamped})}
        )
