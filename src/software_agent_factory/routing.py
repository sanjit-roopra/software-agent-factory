from __future__ import annotations

from .config import FactoryConfig, RoleModelConfig
from .models import AgentRole, Complexity, Risk

COMPLEXITY_ORDER = [Complexity.L0, Complexity.L1, Complexity.L2, Complexity.L3]


class ModelRouter:
    def __init__(self, config: FactoryConfig):
        self._config = config

    def model_for_role(self, role: AgentRole) -> RoleModelConfig:
        if role is AgentRole.IMPLEMENTER:
            raise ValueError("Implementer routing requires complexity and attempt_number")

        if role is AgentRole.TRIAGE:
            return self._config.models.triage
        if role is AgentRole.REFINER:
            return self._config.models.refiner
        if role is AgentRole.RESEARCHER:
            return self._config.models.researcher
        if role is AgentRole.PLANNER:
            return self._config.models.planner
        if role is AgentRole.TESTER:
            return self._config.models.tester
        if role is AgentRole.REVIEWER:
            return self._config.models.reviewer
        raise ValueError(f"No fixed model is configured for role {role}")

    def model_for_researcher(self) -> RoleModelConfig:
        return self._config.models.researcher

    def model_for_implementer(
        self,
        starting_complexity: Complexity,
        attempt_number: int,
    ) -> RoleModelConfig | None:
        """Select the worker model for one implementation/repair attempt.

        Escalation walks the distinct configured worker models from
        ``starting_complexity`` upwards, spending ``same_model_attempts`` on
        each. Once the strongest distinct configured model is reached the
        selection plateaus there, so every complexity yields exactly
        ``max_total_attempts`` usable attempts; ``None`` means the global
        budget itself is exhausted, never that routing ran out of models.
        """
        if attempt_number < 1:
            raise ValueError("attempt_number must be 1 or greater")
        if attempt_number > self._config.retries.max_total_attempts:
            return None

        distinct_models = self._distinct_worker_models(starting_complexity)
        model_index = (attempt_number - 1) // self._config.retries.same_model_attempts
        return distinct_models[min(model_index, len(distinct_models) - 1)]

    def requires_human_approval(self, risk: Risk) -> bool:
        return self._config.risk[risk].human_approval

    def _distinct_worker_models(self, starting_complexity: Complexity) -> list[RoleModelConfig]:
        start_index = COMPLEXITY_ORDER.index(starting_complexity)
        ordered_configs = []
        seen_models: set[str] = set()

        for complexity in COMPLEXITY_ORDER[start_index:]:
            config = self._config.models.workers[complexity]
            if config.model in seen_models:
                continue
            seen_models.add(config.model)
            ordered_configs.append(config)

        return ordered_configs
