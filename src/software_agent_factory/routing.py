from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from .config import FactoryConfig, RoleModelConfig
from .governance import find_protected_matches
from .models import (
    AgentRole,
    Complexity,
    ExecutionRoute,
    RepositoryProfile,
    Risk,
    RouteDecision,
    RouteOption,
    WorkItem,
)

COMPLEXITY_ORDER = [Complexity.L0, Complexity.L1, Complexity.L2, Complexity.L3]
RISK_ORDER = [Risk.R0, Risk.R1, Risk.R2, Risk.R3]


class ModelRouter:
    def __init__(self, config: FactoryConfig):
        self._config = config

    def model_for_role(self, role: AgentRole) -> RoleModelConfig:
        if role is AgentRole.IMPLEMENTER:
            raise ValueError("Implementer routing requires complexity and attempt_number")

        models = self._config.models
        if role is AgentRole.TRIAGE:
            return models.triage
        if role is AgentRole.PLANNER:
            return models.planner
        if role is AgentRole.TESTER:
            return models.tester
        if role is AgentRole.REVIEWER:
            return models.reviewer
        raise ValueError(f"No fixed model is configured for role {role}")

    def model_for_implementer(
        self,
        starting_complexity: Complexity,
        attempt_number: int,
        *,
        model_profile: str | None = None,
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

        distinct_models = self._distinct_worker_models(
            starting_complexity, model_profile=model_profile
        )
        model_index = (attempt_number - 1) // self._config.retries.same_model_attempts
        return distinct_models[min(model_index, len(distinct_models) - 1)]

    def requires_human_approval(self, risk: Risk) -> bool:
        return self._config.requires_human_approval(risk)

    def _distinct_worker_models(
        self,
        starting_complexity: Complexity,
        *,
        model_profile: str | None = None,
    ) -> list[RoleModelConfig]:
        models = (
            self._config.models
            if model_profile is None
            else self._config.model_profiles[model_profile]
        )
        start_index = COMPLEXITY_ORDER.index(starting_complexity)
        ordered_configs = []
        seen_configs: set[tuple[str, str, str]] = set()

        for complexity in COMPLEXITY_ORDER[start_index:]:
            config = models.workers[complexity]
            key = (config.model, config.reasoning, str(config.context_tier))
            if key in seen_configs:
                continue
            seen_configs.add(key)
            ordered_configs.append(config)

        return ordered_configs


def derive_named_paths(work_item: WorkItem) -> list[str]:
    """Derive validated repository-relative paths explicitly named in the work item."""
    prose_sources = [
        work_item.title,
        work_item.description,
        *work_item.acceptance_criteria,
        *work_item.constraints,
    ]
    full_text = " ".join(prose_sources)

    # 1. Backtick and quote tokens: `path/to/file` or "path/to/file" or 'path/to/file'
    quoted = re.findall(r"[`'\"]([^`'\"]+)[`'\"]", full_text)

    # 2. Whitespace-separated tokens
    words = full_text.split()

    known_extensions = (
        ".py",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".sh",
        ".rs",
        ".go",
        ".c",
        ".h",
        ".cpp",
        ".sql",
        ".lock",
    )

    seen: set[str] = set()
    candidates: list[str] = []
    for raw in [*quoted, *words]:
        token = raw.strip("`'\",:;()[]{}<>").strip()
        if not token:
            continue
        if token.startswith("/") or "://" in token or token.startswith("-"):
            continue
        if any(ch in token for ch in "*?[]{}") or any(ch.isspace() for ch in token):
            continue
        is_dotfile = (
            token.startswith(".")
            and len(token) > 1
            and not token.startswith("..")
            and not any(ch in token for ch in "/\\")
        )
        if (
            "/" in token
            or token.lower().endswith(known_extensions)
            or is_dotfile
            or token.upper() in {"README", "MAKEFILE", "DOCKERFILE", "LICENSE"}
        ):
            parts = PurePosixPath(token).parts
            if not parts or any(p in {".", ".."} for p in parts):
                continue
            normalized = str(PurePosixPath(token))
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

    return candidates


def find_legal_full_fallback(
    legal_options: list[RouteOption],
) -> RouteOption | None:
    """Select the first legal option with route=FULL, or None if none survived."""
    for opt in legal_options:
        if opt.route is ExecutionRoute.FULL:
            return opt
    return None


def assess_safety_floors(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
) -> set[str]:
    """Return the subset of configured option IDs that satisfy safety floors.

    Constraints:
    - Explicit WorkItem risk requiring human approval (R2, R3): disallow SINGLE and CRITIQUE.
    - Explicit WorkItem complexity (L2, L3): disallow SINGLE. Also disallow options
      whose worker complexity is strictly lower than work_item.complexity.
    - Missing acceptance criteria: disallow SINGLE.
    - Missing verification commands: disallow SINGLE.
    - Research-required labels: disallow SINGLE and CRITIQUE (FULL or MANUAL only).
    - Forbidden labels (e.g. 'full-only', 'no-single'): filter accordingly.
    - Protected/sensitive/manifest/version patterns mentioned in task prose:
      disallow SINGLE and CRITIQUE.
    """
    legal_ids: set[str] = set()

    requires_human = False
    if work_item.risk is not None:
        requires_human = config.requires_human_approval(work_item.risk)

    has_acceptance_criteria = bool(work_item.acceptance_criteria)
    has_verify_commands = bool(config.repository.commands.verify)
    named_paths = derive_named_paths(work_item)
    has_narrow_scope = bool(named_paths)

    normalized_labels = {lbl.casefold().strip() for lbl in work_item.labels}
    research_required = bool(
        normalized_labels.intersection({"research-required", "research", "needs-research"})
    )
    full_only = bool(normalized_labels.intersection({"full-only", "full"}))
    no_single = bool(normalized_labels.intersection({"no-single", "critique-or-full"}))

    touches_protected = bool(
        find_protected_matches(named_paths, config.repository.protected_file_patterns)
    )

    version_files = set(repository_profile.version_files)
    manifest_names = {"pyproject.toml", "package.json", "requirements.txt", "uv.lock"}
    touches_manifest_or_version = any(
        path in version_files or PurePosixPath(path).name in manifest_names for path in named_paths
    )

    sensitive_found = touches_protected or touches_manifest_or_version

    # Check governance-sensitive terms across all prose and labels
    prose_sources = [
        work_item.title,
        work_item.description,
        *work_item.acceptance_criteria,
        *work_item.constraints,
        *work_item.labels,
    ]
    full_text = " ".join(prose_sources)
    governance_sensitive = any(
        re.search(r"\b" + re.escape(term) + r"\b", full_text, re.IGNORECASE)
        for term in config.routing.full_only_terms
    )

    for opt_cfg in config.routing.options:
        route = opt_cfg.route

        # MANUAL_TRIAGE is always a safe floor option
        if route is ExecutionRoute.MANUAL_TRIAGE:
            legal_ids.add(opt_cfg.id)
            continue

        # Check complexity lower-bound floor
        if work_item.complexity is not None and opt_cfg.complexity is not None:
            if COMPLEXITY_ORDER.index(opt_cfg.complexity) < COMPLEXITY_ORDER.index(
                work_item.complexity
            ):
                continue

        # Check risk lower-bound floor: explicit WorkItem risk is a lower floor
        if work_item.risk is not None and opt_cfg.risk is not None:
            if RISK_ORDER.index(opt_cfg.risk) < RISK_ORDER.index(work_item.risk):
                continue

        # Configured risk policy decides which routes are legal:
        # An option whose risk requires human approval cannot run via SINGLE or CRITIQUE
        option_requires_human = opt_cfg.risk is not None and config.requires_human_approval(
            opt_cfg.risk
        )

        # FULL route is allowed unless constrained by complexity above
        if route is ExecutionRoute.FULL:
            legal_ids.add(opt_cfg.id)
            continue

        # CRITIQUE route checks
        if route is ExecutionRoute.CRITIQUE:
            if (
                requires_human
                or option_requires_human
                or research_required
                or full_only
                or sensitive_found
                or governance_sensitive
            ):
                continue
            if not has_acceptance_criteria or not has_verify_commands:
                continue
            legal_ids.add(opt_cfg.id)
            continue

        # SINGLE route checks
        if route is ExecutionRoute.SINGLE:
            if not has_narrow_scope:
                continue
            if requires_human or option_requires_human:
                continue
            if not has_acceptance_criteria:
                continue
            if not has_verify_commands:
                continue
            if (
                research_required
                or full_only
                or no_single
                or sensitive_found
                or governance_sensitive
            ):
                continue
            if work_item.complexity in {Complexity.L2, Complexity.L3}:
                continue
            legal_ids.add(opt_cfg.id)
            continue

    return legal_ids


_ROUTE_WEIGHT = {
    ExecutionRoute.SINGLE: 0,
    ExecutionRoute.CRITIQUE: 1,
    ExecutionRoute.FULL: 2,
    ExecutionRoute.MANUAL_TRIAGE: 3,
}


def select_lightest_option(legal_options: list[RouteOption]) -> RouteOption:
    """Return the lightest legal option; ties keep configuration order (ADR-037)."""
    return min(legal_options, key=lambda opt: _ROUTE_WEIGHT[opt.route])


def determine_route(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
) -> RouteDecision:
    """Apply the safety floors and pick a route with a fixed rule (ADR-037)."""
    request_hash = hashlib.sha256(
        f"{work_item.id}:{work_item.title}:{work_item.description}".encode("utf-8")
    ).hexdigest()

    all_options = [
        RouteOption(
            id=opt.id,
            route=opt.route,
            complexity=opt.complexity,
            risk=opt.risk,
            model_profile=opt.model_profile,
            description=opt.description,
        )
        for opt in config.routing.options
    ]

    legal_ids = assess_safety_floors(work_item, repository_profile, config)
    offered_options = [opt for opt in all_options if opt.id in legal_ids]

    # 1. When routing is disabled or not configured
    if not config.routing.enabled:
        fallback_option = find_legal_full_fallback(offered_options)
        if fallback_option is None:
            return RouteDecision(
                work_item_id=work_item.id,
                offered_options=all_options,
                selected_option="manual_triage",
                initial_route=ExecutionRoute.MANUAL_TRIAGE,
                effective_route=ExecutionRoute.MANUAL_TRIAGE,
                selected_worker_complexity=None,
                selected_risk=None,
                selected_model_profile=None,
                source="disabled",
                confidence=1.0,
                probabilities=None,
                model_id=None,
                protocol_version=config.routing.rubric_version,
                latency_ms=0.0,
                request_hash=request_hash,
                fallback_reason=(
                    "routing is disabled and no legal FULL option survived safety floors"
                ),
            )
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=all_options,
            selected_option=fallback_option.id,
            initial_route=fallback_option.route,
            effective_route=fallback_option.route,
            selected_worker_complexity=None,
            selected_risk=None,
            selected_model_profile=None,
            source="disabled",
            confidence=1.0,
            probabilities=None,
            model_id=None,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
            fallback_reason="routing is disabled by configuration",
        )

    # 2. When routing is enabled:
    # If no legal options exist, return MANUAL_TRIAGE
    if not offered_options:
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=all_options,
            selected_option="manual_triage",
            initial_route=ExecutionRoute.MANUAL_TRIAGE,
            effective_route=ExecutionRoute.MANUAL_TRIAGE,
            selected_worker_complexity=None,
            selected_risk=None,
            selected_model_profile=None,
            source="safety_floor",
            confidence=1.0,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
            fallback_reason="no legal options satisfied safety floors",
        )

    # If only one legal option exists, it is the route.
    if len(offered_options) == 1:
        chosen = offered_options[0]
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=offered_options,
            selected_option=chosen.id,
            initial_route=chosen.route,
            effective_route=chosen.route,
            selected_worker_complexity=chosen.complexity,
            selected_risk=chosen.risk,
            selected_model_profile=chosen.model_profile,
            source="single_option",
            confidence=1.0,
            probabilities={chosen.id: 1.0},
            model_id=None,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
        )

    # Several legal options: the safety floors already removed every unsafe
    # route, so take the lightest one that is left.
    chosen = select_lightest_option(offered_options)
    return RouteDecision(
        work_item_id=work_item.id,
        offered_options=offered_options,
        selected_option=chosen.id,
        initial_route=chosen.route,
        effective_route=chosen.route,
        selected_worker_complexity=chosen.complexity,
        selected_risk=chosen.risk,
        selected_model_profile=chosen.model_profile,
        source="rule",
        confidence=1.0,
        probabilities=None,
        model_id=None,
        protocol_version=config.routing.rubric_version,
        latency_ms=0.0,
        request_hash=request_hash,
    )
