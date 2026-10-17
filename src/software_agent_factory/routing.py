from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from .config import FactoryConfig, RoleModelConfig, RoutingOptionConfig
from .governance import find_protected_matches
from .models import (
    AgentRole,
    Complexity,
    ExecutionRoute,
    RepositoryProfile,
    Risk,
    RouteDecision,
    RouteOption,
    TriageResult,
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
        seen_configs: set[tuple[str, str, str, str | None, str | None]] = set()

        for complexity in COMPLEXITY_ORDER[start_index:]:
            config = models.workers[complexity]
            key = (
                config.model,
                config.reasoning,
                str(config.context_tier),
                config.runtime,
                config.provider,
            )
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


def _fits_triage(option: RoutingOptionConfig, triage: TriageResult) -> bool:
    """Whether ``option`` is at least as strong as the triage complexity and risk."""
    if option.complexity is not None and COMPLEXITY_ORDER.index(
        option.complexity
    ) < COMPLEXITY_ORDER.index(triage.complexity):
        return False
    return option.risk is None or RISK_ORDER.index(option.risk) >= RISK_ORDER.index(triage.risk)


def _text_floors(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
) -> tuple[bool, bool]:
    """Return whether the work item text blocks short routes, and whether it blocks SINGLE."""
    named_paths = derive_named_paths(work_item)
    labels = {lbl.casefold().strip() for lbl in work_item.labels}
    version_files = set(repository_profile.version_files)
    manifest_names = {"pyproject.toml", "package.json", "requirements.txt", "uv.lock"}
    full_text = " ".join(
        [
            work_item.title,
            work_item.description,
            *work_item.acceptance_criteria,
            *work_item.constraints,
            *work_item.labels,
        ]
    )
    blocks_short = (
        not work_item.acceptance_criteria
        or not config.repository.commands.verify
        or bool(labels & {"research-required", "research", "needs-research", "full-only", "full"})
        or bool(find_protected_matches(named_paths, config.repository.protected_file_patterns))
        or any(
            path in version_files or PurePosixPath(path).name in manifest_names
            for path in named_paths
        )
        or any(
            re.search(r"\b" + re.escape(term) + r"\b", full_text, re.IGNORECASE)
            for term in config.routing.full_only_terms
        )
    )
    blocks_single = not named_paths or bool(labels & {"no-single", "critique-or-full"})
    return blocks_short, blocks_single


def assess_safety_floors(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
    triage: TriageResult,
) -> set[str]:
    """Return the configured option IDs that the triage result and the safety floors allow.

    - An option is legal only if its complexity and risk are not lower than the triage's.
    - Triage risk that needs human approval disallows SINGLE and CRITIQUE.
    - Missing acceptance criteria or verify commands, research or full-only labels,
      protected or manifest files and ``full_only_terms`` disallow SINGLE and CRITIQUE.
    - No named path or a no-single label disallows SINGLE.
    - MANUAL_TRIAGE is always legal.
    """
    blocks_short, blocks_single = _text_floors(work_item, repository_profile, config)
    blocks_short = blocks_short or config.requires_human_approval(triage.risk)
    legal_ids: set[str] = set()
    for opt in config.routing.options:
        if opt.route is ExecutionRoute.MANUAL_TRIAGE:
            legal_ids.add(opt.id)
        elif not _fits_triage(opt, triage):
            continue
        elif opt.route is ExecutionRoute.FULL:
            legal_ids.add(opt.id)
        elif not (
            blocks_short
            or (opt.risk is not None and config.requires_human_approval(opt.risk))
            or (opt.route is ExecutionRoute.SINGLE and blocks_single)
        ):
            legal_ids.add(opt.id)
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


def _route_decision(
    work_item: WorkItem,
    config: FactoryConfig,
    offered: list[RouteOption],
    chosen: RouteOption | None,
    source: str,
    fallback_reason: str | None = None,
) -> RouteDecision:
    route = chosen.route if chosen is not None else ExecutionRoute.MANUAL_TRIAGE
    # A disabled decision takes FULL without applying the option's model profile.
    detail = chosen if source != "disabled" else None
    return RouteDecision(
        work_item_id=work_item.id,
        offered_options=offered,
        selected_option=chosen.id if chosen is not None else "manual_triage",
        initial_route=route,
        effective_route=route,
        selected_model_profile=detail.model_profile if detail is not None else None,
        source=source,
        confidence=1.0,
        protocol_version=config.routing.rubric_version,
        latency_ms=0.0,
        request_hash=hashlib.sha256(
            f"{work_item.id}:{work_item.title}:{work_item.description}".encode("utf-8")
        ).hexdigest(),
        fallback_reason=fallback_reason,
    )


def determine_route(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
    triage: TriageResult,
) -> RouteDecision:
    """Pick the lightest route the triage result and the safety floors allow (ADR-037)."""
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
    if not config.routing.enabled:
        fallback = find_legal_full_fallback(all_options)
        reason = (
            "routing is disabled by configuration"
            if fallback is not None
            else "routing is disabled and no FULL option is configured"
        )
        return _route_decision(work_item, config, all_options, fallback, "disabled", reason)
    legal_ids = assess_safety_floors(work_item, repository_profile, config, triage)
    offered_options = [opt for opt in all_options if opt.id in legal_ids]
    if not offered_options:
        return _route_decision(
            work_item,
            config,
            all_options,
            None,
            "safety_floor",
            "no legal options satisfied safety floors",
        )
    return _route_decision(
        work_item, config, offered_options, select_lightest_option(offered_options), "triage"
    )
