"""Send each agent request to the runtime its role names (ADR-045).

A role without ``runtime`` uses the ``--runtime`` default. Each runtime is
built the first time a request needs it, so a run that never calls pi never
needs pi. The router stamps the serving runtime on every reported
:class:`~software_agent_factory.models.ModelUsage`.

When the first runtime reports itself unavailable and the request names a
fallback, the router sends the same request to the fallback once. A wrong
result is not unavailability, so it never reaches the fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import RuntimeName

__all__ = ["RoutingAgentRuntime"]

logger = logging.getLogger(__name__)


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
        result = self._serve(name, request)
        fallback = request.fallback
        if not result.runtime_unavailable or fallback is None:
            return result
        logger.warning(
            "%s: runtime %s is unavailable; falling back to %s %s: %s",
            request.role.value,
            name.value,
            fallback.runtime.value,
            fallback.model,
            result.failure_reason,
        )
        fallback_request = request.model_copy(
            update={
                "runtime": fallback.runtime,
                "model": fallback.model,
                "reasoning": fallback.reasoning,
                "context_tier": fallback.context_tier,
                "fallback": None,
            }
        )
        return self._serve(fallback.runtime, fallback_request)

    def _serve(self, name: RuntimeName, request: AgentRequest) -> AgentResult:
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
