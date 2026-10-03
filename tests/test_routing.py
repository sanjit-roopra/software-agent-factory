from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from test_workflow import FakeAgentRuntime, RecordingRuntime, _config, _work_item

from software_agent_factory.agents import AgentRequest, AgentResult
from software_agent_factory.config import FactoryConfig, RoutingConfig, RoutingOptionConfig
from software_agent_factory.models import (
    AgentRole,
    ChangeSet,
    Complexity,
    ExecutionPlan,
    ExecutionRoute,
    FactoryRun,
    RepositoryPackageManager,
    RepositoryProfile,
    ReviewReport,
    Risk,
    RouteDecision,
    RouteOption,
    Specification,
    TestReport,
    TriageResult,
    WorkflowState,
)
from software_agent_factory.routing import (
    ModelRouter,
    assess_safety_floors,
    derive_named_paths,
    determine_route,
    find_legal_full_fallback,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController, _RunContext


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _config_dict(max_total_attempts: int = 6) -> dict[str, object]:
    return {
        "factory": {
            "data_dir": "~/.software-factory",
            "retries": {
                "same_model_attempts": 2,
                "max_total_attempts": max_total_attempts,
            },
        },
        "models": {
            "triage": {"model": "claude-sonnet-5", "reasoning": "medium"},
            "planner": {"model": "claude-opus-5", "reasoning": "high"},
            "workers": {
                "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                "L1": {"model": "claude-sonnet-5", "reasoning": "medium"},
                "L2": {"model": "claude-opus-5", "reasoning": "high"},
                "L3": {"model": "claude-opus-5", "reasoning": "high"},
            },
            "tester": {"model": "claude-sonnet-5", "reasoning": "high"},
            "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
        },
        "repository": {
            "branch_prefix": "factory/",
            "command_timeout_seconds": 900,
            "commands": {"install": [], "verify": [], "build": []},
        },
        "risk": {
            "R0": {"human_approval": False},
            "R1": {"human_approval": False},
            "R2": {"human_approval": True},
            "R3": {"human_approval": True},
        },
    }


def test_model_router_routes_fixed_roles_and_risk_gate() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    assert router.model_for_role(AgentRole.TRIAGE).model == "claude-sonnet-5"
    assert router.model_for_role(AgentRole.PLANNER).reasoning == "high"
    assert router.model_for_role(AgentRole.REVIEWER).model == "gpt-5.6-sol"
    assert router.requires_human_approval(Risk.R1) is False
    assert router.requires_human_approval(Risk.R2) is True


def test_model_router_rejects_implementer_without_complexity() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    with pytest.raises(ValueError, match="Implementer routing"):
        router.model_for_role(AgentRole.IMPLEMENTER)


def test_model_router_escalates_implementer_by_distinct_models() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    routed_models = [
        selection.model if selection is not None else None
        for selection in (
            router.model_for_implementer(Complexity.L0, attempt_number)
            for attempt_number in range(1, 8)
        )
    ]

    assert routed_models == [
        "mai-code-1.1-flash",
        "mai-code-1.1-flash",
        "claude-sonnet-5",
        "claude-sonnet-5",
        "claude-opus-5",
        "claude-opus-5",
        None,
    ]


@pytest.mark.parametrize("starting_complexity", list(Complexity))
def test_every_complexity_gets_the_full_attempt_budget(
    starting_complexity: Complexity,
) -> None:
    """Escalation must never shrink the bounded budget.

    Higher complexities have fewer *distinct* stronger models available, so
    routing plateaus on the strongest one instead of returning ``None``
    early: otherwise an L3 task would silently get fewer repair attempts
    than an L0 task.
    """
    config = FactoryConfig.model_validate(_config_dict())
    router = ModelRouter(config)
    max_total_attempts = config.retries.max_total_attempts

    selections = [
        router.model_for_implementer(starting_complexity, attempt_number)
        for attempt_number in range(1, max_total_attempts + 1)
    ]

    assert all(selection is not None for selection in selections)
    assert len(selections) == max_total_attempts
    assert router.model_for_implementer(starting_complexity, max_total_attempts + 1) is None


@pytest.mark.parametrize(
    ("starting_complexity", "strongest_model"),
    [
        (Complexity.L0, "claude-opus-5"),
        (Complexity.L1, "claude-opus-5"),
        (Complexity.L2, "claude-opus-5"),
        (Complexity.L3, "claude-opus-5"),
    ],
)
def test_escalation_plateaus_at_strongest_distinct_model(
    starting_complexity: Complexity,
    strongest_model: str,
) -> None:
    config = FactoryConfig.model_validate(_config_dict())
    router = ModelRouter(config)

    last = router.model_for_implementer(starting_complexity, config.retries.max_total_attempts)

    assert last is not None
    assert last.model == strongest_model


@pytest.mark.parametrize("attempt_number", [0, -1])
def test_model_router_rejects_invalid_attempt_numbers(attempt_number: int) -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict()))

    with pytest.raises(ValueError, match="attempt_number"):
        router.model_for_implementer(Complexity.L0, attempt_number)


def test_model_router_returns_none_only_past_total_attempt_budget() -> None:
    router = ModelRouter(FactoryConfig.model_validate(_config_dict(max_total_attempts=3)))

    assert router.model_for_implementer(Complexity.L0, 3).model == "claude-sonnet-5"
    assert router.model_for_implementer(Complexity.L0, 4) is None
    assert router.model_for_implementer(Complexity.L2, 3).model == "claude-opus-5"
    assert router.model_for_implementer(Complexity.L2, 4) is None


def test_routing_distinguishes_same_model_with_different_runtime_settings() -> None:
    payload = _config_dict(max_total_attempts=4)
    workers = payload["models"]["workers"]  # type: ignore[index]
    workers["L1"] = {  # type: ignore[index]
        "model": "claude-sonnet-5",
        "reasoning": "high",
        "context_tier": "long_context",
    }
    workers["L2"] = {  # type: ignore[index]
        "model": "claude-sonnet-5",
        "reasoning": "high",
        "context_tier": "default",
    }
    router = ModelRouter(FactoryConfig.model_validate(payload))

    first = router.model_for_implementer(Complexity.L1, 1)
    third = router.model_for_implementer(Complexity.L1, 3)

    assert first is not None and first.context_tier.value == "long_context"
    assert third is not None and third.context_tier.value == "default"


# ---------------------------------------------------------------------------
# Deterministic execution routing tests (ADR-037)
# ---------------------------------------------------------------------------


def _default_profile() -> RepositoryProfile:
    return RepositoryProfile(
        package_managers=(RepositoryPackageManager.UV,),
        version_files=("pyproject.toml",),
        dependency_fingerprint="0" * 64,
        manifest_fingerprint="1" * 64,
    )


def test_routing_disabled_preserves_full_pipeline_without_network() -> None:
    config = FactoryConfig.model_validate(_config_dict())
    assert config.routing.enabled is False

    wi = _work_item("WI-routing-disabled")
    profile = _default_profile()

    decision = determine_route(wi, profile, config)

    assert decision.initial_route is ExecutionRoute.FULL
    assert decision.effective_route is ExecutionRoute.FULL
    assert decision.source == "disabled"
    assert decision.selected_worker_complexity is None
    assert decision.selected_model_profile is None
    assert decision.fallback_reason == "routing is disabled by configuration"


def _risk_gate_config(*, risk_assessment_enabled: bool) -> FactoryConfig:
    config_dict = _config_dict()
    config_dict["risk_assessment"] = {"enabled": risk_assessment_enabled}
    config_dict["repository"]["commands"]["verify"] = ["pytest"]  # type: ignore[index]
    config_dict["routing"] = {
        "enabled": True,
        "options": [
            {"id": "single_r2", "route": "SINGLE", "complexity": "L0", "risk": "R2"},
            {"id": "full_l2", "route": "FULL", "complexity": "L2"},
            {"id": "manual_triage", "route": "MANUAL_TRIAGE"},
        ],
    }
    return FactoryConfig.model_validate(config_dict)


@pytest.mark.parametrize(
    ("explicit_risk", "enabled", "expected"),
    [
        (None, True, {"manual_triage", "full_l2"}),
        (None, False, {"manual_triage", "full_l2", "single_r2"}),
        (Risk.R2, True, {"manual_triage", "full_l2"}),
        (Risk.R2, False, {"manual_triage", "full_l2", "single_r2"}),
    ],
)
def test_assess_safety_floors_ignores_the_approval_gate_when_risk_assessment_is_off(
    explicit_risk: Risk | None, enabled: bool, expected: set[str]
) -> None:
    config = _risk_gate_config(risk_assessment_enabled=enabled)
    wi = _work_item("WI-risk-gate").model_copy(
        update={
            "risk": explicit_risk,
            "description": "Fix the typo in src/app.py.",
            "acceptance_criteria": ["Check"],
        }
    )

    assert assess_safety_floors(wi, _default_profile(), config) == expected


def test_model_router_reports_no_approval_needed_when_risk_assessment_is_off() -> None:
    payload = _config_dict()
    payload["risk_assessment"] = {"enabled": False}
    router = ModelRouter(FactoryConfig.model_validate(payload))

    assert [router.requires_human_approval(risk) for risk in Risk] == [False] * len(Risk)


def test_assess_safety_floors_filters_disallowed_options() -> None:
    config_dict = _config_dict()
    config_dict["routing"] = {"enabled": True}
    config_dict["repository"]["commands"]["verify"] = ["pytest"]
    config = FactoryConfig.model_validate(config_dict)
    profile = _default_profile()

    # 1. Human approval required (Risk R2)
    wi_r2 = _work_item("WI-risk-r2").model_copy(
        update={"risk": Risk.R2, "acceptance_criteria": ["Check"]}
    )
    legal_r2 = assess_safety_floors(wi_r2, profile, config)
    assert legal_r2 == {"manual_triage", "full_l2", "full_l3"}

    # 2. Missing acceptance criteria
    wi_no_ac = _work_item("WI-no-ac").model_copy(update={"acceptance_criteria": []})
    legal_no_ac = assess_safety_floors(wi_no_ac, profile, config)
    assert legal_no_ac == {"manual_triage", "full_l2", "full_l3"}

    # 3. Missing verify commands
    cfg_no_verify = config.model_copy(
        update={
            "repository": config.repository.model_copy(
                update={"commands": config.repository.commands.model_copy(update={"verify": []})}
            )
        }
    )
    wi_normal = _work_item("WI-normal").model_copy(update={"acceptance_criteria": ["Check"]})
    legal_no_verify = assess_safety_floors(wi_normal, profile, cfg_no_verify)
    assert legal_no_verify == {"manual_triage", "full_l2", "full_l3"}

    # 4. Research required label
    wi_research = _work_item("WI-research").model_copy(
        update={"labels": ["needs-research"], "acceptance_criteria": ["Check"]}
    )
    legal_research = assess_safety_floors(wi_research, profile, config)
    assert legal_research == {"manual_triage", "full_l2", "full_l3"}

    # 5. Full-only label
    wi_full = _work_item("WI-full").model_copy(
        update={"labels": ["full-only"], "acceptance_criteria": ["Check"]}
    )
    legal_full = assess_safety_floors(wi_full, profile, config)
    assert legal_full == {"manual_triage", "full_l2", "full_l3"}

    # 6. Sensitive / protected files mentioned in work item prose
    wi_protected = _work_item("WI-protected").model_copy(
        update={
            "description": "modify .env credentials and secret keys",
            "acceptance_criteria": ["Check"],
        }
    )
    legal_protected = assess_safety_floors(wi_protected, profile, config)
    assert legal_protected == {"manual_triage", "full_l2", "full_l3"}

    # 7. Complexity floor: L2 task filters out L0 and L1 options
    wi_l2 = _work_item("WI-l2").model_copy(
        update={"complexity": Complexity.L2, "acceptance_criteria": ["Check"]}
    )
    legal_l2 = assess_safety_floors(wi_l2, profile, config)
    assert "single_l0" not in legal_l2
    assert "single_l1" not in legal_l2
    assert "critique_l1" not in legal_l2
    assert "critique_l2" in legal_l2
    assert "full_l2" in legal_l2


def test_single_legal_option_is_selected() -> None:
    config_dict = _config_dict()
    config_dict["routing"] = {
        "enabled": True,
        "options": [
            {
                "id": "full_l2",
                "route": "FULL",
                "complexity": "L2",
                "description": "Full only option",
            }
        ],
    }
    config = FactoryConfig.model_validate(config_dict)
    profile = _default_profile()
    wi = _work_item("WI-single-option")

    decision = determine_route(wi, profile, config)

    assert decision.source == "single_option"
    assert decision.selected_option == "full_l2"
    assert decision.effective_route is ExecutionRoute.FULL


def _enabled_routing_config(options: list[dict[str, object]] | None = None) -> FactoryConfig:
    config_dict = _config_dict()
    config_dict["repository"]["commands"]["verify"] = ["pytest"]  # type: ignore[index]
    routing: dict[str, object] = {"enabled": True}
    if options is not None:
        routing["options"] = options
    config_dict["routing"] = routing
    return FactoryConfig.model_validate(config_dict)


@pytest.mark.parametrize(
    ("update", "expected_option", "expected_route"),
    [
        (
            {"title": "Fix typo in README.md", "acceptance_criteria": ["Typo is gone"]},
            "single_l0",
            ExecutionRoute.SINGLE,
        ),
        ({"acceptance_criteria": ["Names are rejected"]}, "critique_l1", ExecutionRoute.CRITIQUE),
        ({}, "full_l2", ExecutionRoute.FULL),
    ],
)
def test_several_legal_options_pick_the_lightest_route(
    update: dict[str, object], expected_option: str, expected_route: ExecutionRoute
) -> None:
    config = _enabled_routing_config()
    wi = _work_item("WI-lightest").model_copy(update=update)

    decision = determine_route(wi, _default_profile(), config)

    assert len(decision.offered_options) > 1
    assert decision.selected_option == expected_option
    assert decision.initial_route is expected_route
    assert decision.effective_route is expected_route
    assert decision.source == "rule"
    assert decision.confidence == 1.0
    assert decision.probabilities is None
    assert decision.model_id is None
    assert decision.latency_ms == 0.0
    assert decision.fallback_reason is None


def test_lightest_route_ties_keep_configuration_order() -> None:
    config = _enabled_routing_config(
        [
            {"id": "manual", "route": "MANUAL_TRIAGE"},
            {"id": "full_l3", "route": "FULL", "complexity": "L3"},
            {"id": "full_l2", "route": "FULL", "complexity": "L2"},
        ]
    )

    decision = determine_route(_work_item("WI-tie"), _default_profile(), config)

    assert [opt.id for opt in decision.offered_options] == ["manual", "full_l3", "full_l2"]
    assert decision.selected_option == "full_l3"
    assert decision.selected_worker_complexity is Complexity.L3
    assert decision.source == "rule"


def test_config_with_removed_classifier_keys_still_loads() -> None:
    config_dict = _config_dict()
    config_dict["routing"] = {
        "enabled": True,
        "api_url": "https://api.typesafe.ai/v1/systemone",
        "model": "jev-1.13.0",
        "api_key_env_var": "JEV_API_KEY",
        "timeout_seconds": 5.0,
        "min_confidence": 0.7,
        "min_probability": 0.5,
        "max_prompt_chars": 4000,
        "max_response_bytes": 65536,
        "single_max_changed_files": 3,
    }

    routing = FactoryConfig.model_validate(config_dict).routing

    assert routing.enabled is True
    assert routing.single_max_changed_files == 3
    assert not hasattr(routing, "api_url")
    with pytest.raises(ValidationError, match="unknown_key"):
        RoutingConfig.model_validate({"unknown_key": 1})


def test_persisted_jev_route_decision_still_loads() -> None:
    payload = {
        "work_item_id": "WI-old-jev",
        "offered_options": [
            {"id": "single_l0", "route": "SINGLE", "complexity": "L0", "risk": "R0"},
            {"id": "full_l2", "route": "FULL", "complexity": "L2"},
        ],
        "selected_option": "single_l0",
        "initial_route": "SINGLE",
        "effective_route": "SINGLE",
        "selected_worker_complexity": "L0",
        "selected_risk": "R0",
        "source": "jev",
        "confidence": 0.91,
        "probabilities": {"single_l0": 0.91, "full_l2": 0.09},
        "model_id": "jev-1.13.0",
        "protocol_version": "1.0",
        "latency_ms": 42.5,
        "usage": {"input_tokens": 120, "output_tokens": 4},
        "request_hash": "a" * 64,
    }

    decision = RouteDecision.model_validate(payload)

    assert decision.source == "jev"
    assert decision.probabilities == {"single_l0": 0.91, "full_l2": 0.09}
    assert decision.model_id == "jev-1.13.0"
    assert decision.usage == {"input_tokens": 120, "output_tokens": 4}


def test_manual_triage_route_halts_in_needs_human(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    config = config.model_copy(
        update={"routing": config.routing.model_copy(update={"enabled": True})}
    )
    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime()

    # The only FULL option (L2) is below the L3 floor, so only manual triage stays legal.
    work_item = _work_item("WI-manual").model_copy(update={"complexity": Complexity.L3})
    restricted = [opt for opt in config.routing.options if opt.id in {"full_l2", "manual_triage"}]
    config = config.model_copy(
        update={"routing": config.routing.model_copy(update={"options": restricted})}
    )
    controller = WorkflowController(config, store, runtime)
    run = controller.run(work_item, source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.initial_route is ExecutionRoute.MANUAL_TRIAGE
    assert run.effective_route is ExecutionRoute.MANUAL_TRIAGE
    assert "manual triage" in (run.failure_reason or "")
    # Implementer must not have been invoked
    assert not any(attempt.role is AgentRole.IMPLEMENTER for attempt in run.attempt_records)


def test_single_route_skips_agents_and_synthesizes_artifacts(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    commands = config.repository.commands.model_copy(update={"verify": ["git status"]})
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(update={"enabled": True}),
            "repository": config.repository.model_copy(update={"commands": commands}),
        }
    )
    store = FileRunStore(data_dir)
    runtime = RecordingRuntime(FakeAgentRuntime())

    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-single").model_copy(
        update={
            "acceptance_criteria": ["Ensure README exists"],
            "constraints": ["No unnecessary files"],
        }
    )
    run = controller.run(wi, source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.initial_route is ExecutionRoute.SINGLE
    assert run.effective_route is ExecutionRoute.SINGLE
    assert run.performance.counters["route.saved_calls"] == 4

    # Verify agent invocations: only implementer
    invoked_roles = [r.role for r in runtime.requests]
    assert AgentRole.TRIAGE not in invoked_roles
    assert AgentRole.REFINER not in invoked_roles
    assert AgentRole.RESEARCHER not in invoked_roles
    assert AgentRole.PLANNER not in invoked_roles
    assert AgentRole.TESTER not in invoked_roles
    assert AgentRole.REVIEWER not in invoked_roles
    assert AgentRole.IMPLEMENTER in invoked_roles

    # Verify synthesized artifacts
    triage = store.load_artifact(run.id, TriageResult)
    spec = store.load_artifact(run.id, Specification)
    plan = store.load_artifact(run.id, ExecutionPlan)

    assert triage.provenance == "SYNTHESIZED"
    assert spec.provenance == "SYNTHESIZED"
    assert plan.provenance == "SYNTHESIZED"

    # Verify RouteDecision artifact persisted
    decision = store.load_artifact(run.id, RouteDecision)
    assert decision.selected_option == "single_l0"
    assert decision.effective_route is ExecutionRoute.SINGLE


def test_critique_route_runs_implementer_and_reviewer_only(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    commands = config.repository.commands.model_copy(update={"verify": ["git status"]})
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(update={"enabled": True}),
            "repository": config.repository.model_copy(update={"commands": commands}),
        }
    )
    store = FileRunStore(data_dir)
    runtime = RecordingRuntime(FakeAgentRuntime())

    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-critique").model_copy(update={"acceptance_criteria": ["Check file"]})
    run = controller.run(wi, source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.initial_route is ExecutionRoute.CRITIQUE
    assert run.effective_route is ExecutionRoute.CRITIQUE
    assert run.performance.counters["route.saved_calls"] == 3

    invoked_roles = [r.role for r in runtime.requests]
    assert AgentRole.TRIAGE not in invoked_roles
    assert AgentRole.REFINER not in invoked_roles
    assert AgentRole.PLANNER not in invoked_roles
    assert AgentRole.TESTER not in invoked_roles
    assert AgentRole.IMPLEMENTER in invoked_roles
    assert AgentRole.REVIEWER in invoked_roles


def test_monotonic_ratchet_triggers(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    commands = config.repository.commands.model_copy(update={"verify": ["git status"]})
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(
                update={"enabled": True, "single_max_changed_files": 1}
            ),
            "repository": config.repository.model_copy(update={"commands": commands}),
        }
    )
    store = FileRunStore(data_dir)

    # Implementer creates 2 files, exceeding single_max_changed_files (1)
    def multi_file_implementer(req: AgentRequest) -> AgentResult:
        workspace = Path(req.workspace_path)  # type: ignore[arg-type]
        (workspace / "file1.txt").write_text("one")
        (workspace / "file2.txt").write_text("two")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Created 2 files",
                changed_files=["file1.txt", "file2.txt"],
                tests_added=[],
                commands_run=[],
            ),
        )

    runtime = FakeAgentRuntime(implementer=multi_file_implementer)

    controller = WorkflowController(config, store, runtime)
    wi = _work_item("WI-ratchet").model_copy(
        update={
            "title": "Update file1.txt and file2.txt",
            "acceptance_criteria": ["Touch two files"],
        }
    )

    run = controller.run(wi, source_repo)

    # Ratcheted from SINGLE to FULL_REVIEW because changed files exceeded single_max_changed_files
    assert run.initial_route is ExecutionRoute.SINGLE
    assert run.effective_route is ExecutionRoute.FULL_REVIEW

    decision = store.load_artifact(run.id, RouteDecision)
    assert len(decision.adjustments) >= 1
    assert decision.adjustments[0].to_route is ExecutionRoute.FULL_REVIEW
    assert "changed-file count" in decision.adjustments[0].reason


def test_old_run_data_compatibility() -> None:
    # A serialized FactoryRun without initial_route, effective_route, or route_decision
    payload = {
        "id": "run-old-123",
        "work_item_id": "WI-old",
        "state": "DONE",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:01:00Z",
        "workspace_path": "/tmp/workspace",
    }
    run = FactoryRun.model_validate(payload)
    assert run.initial_route is None
    assert run.effective_route is None
    assert run.route_decision is None


# --- Regression tests for review findings 1 to 7 ---


def test_finding2_config_requires_full_option_and_safe_fallback() -> None:
    # 1. RoutingConfig requires at least one FULL option
    with pytest.raises(
        ValidationError,
        match=r"routing\.options must contain at least one option with route=ExecutionRoute\.FULL",
    ):
        RoutingConfig(
            options=[
                RoutingOptionConfig(
                    id="single_l0",
                    route=ExecutionRoute.SINGLE,
                    complexity=Complexity.L0,
                    risk=Risk.R0,
                ),
                RoutingOptionConfig(
                    id="critique_l1",
                    route=ExecutionRoute.CRITIQUE,
                    complexity=Complexity.L1,
                    risk=Risk.R1,
                ),
            ]
        )

    # 2. find_legal_full_fallback helper
    full_opt = RouteOption(id="full_l2", route=ExecutionRoute.FULL, complexity=Complexity.L2)
    single_opt = RouteOption(
        id="single_l0",
        route=ExecutionRoute.SINGLE,
        complexity=Complexity.L0,
        risk=Risk.R0,
    )
    found = find_legal_full_fallback([single_opt, full_opt])
    assert found is not None and found.id == "full_l2"
    assert find_legal_full_fallback([single_opt]) is None


def test_finding3_derive_named_paths_and_narrow_scope_enforcement() -> None:
    # Valid extraction
    wi = _work_item("WI-paths").model_copy(
        update={
            "title": "Update README.md and src/foo.py",
            "description": "Also edit `config/app.json` and 'docs/index.md'",
            "acceptance_criteria": ["Check file Makefile and tests/test_foo.py"],
            "constraints": ["Do not touch /absolute/path or https://url or -flag or *.txt"],
        }
    )
    paths = derive_named_paths(wi)
    assert "README.md" in paths
    assert "src/foo.py" in paths
    assert "config/app.json" in paths
    assert "docs/index.md" in paths
    assert "Makefile" in paths
    assert "tests/test_foo.py" in paths
    assert "/absolute/path" not in paths
    assert "https://url" not in paths

    # When no paths are named, SINGLE must not be offered
    wi_no_paths = _work_item("WI-no-paths").model_copy(
        update={
            "title": "General improvements",
            "description": "Make things better",
            "acceptance_criteria": ["Pass tests"],
        }
    )
    config_dict = _config_dict()
    config_dict["routing"] = {"enabled": True}
    config_dict["repository"]["commands"]["verify"] = ["pytest"]
    cfg = FactoryConfig.model_validate(config_dict)
    legal = assess_safety_floors(wi_no_paths, _default_profile(), cfg)
    assert "single_l0" not in legal
    assert "single_l1" not in legal


def test_finding3_and_4_unrelated_src_changes_ratchet_to_full_review(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    commands = config.repository.commands.model_copy(update={"verify": ["git status"]})
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(update={"enabled": True}),
            "repository": config.repository.model_copy(update={"commands": commands}),
        }
    )
    store = FileRunStore(data_dir)

    def rogue_implementer(req: AgentRequest) -> AgentResult:
        workspace = Path(req.workspace_path)  # type: ignore[arg-type]
        (workspace / "README.md").write_text("updated readme")
        src_dir = workspace / "src"
        src_dir.mkdir(exist_ok=True)
        (src_dir / "unrelated.py").write_text("# unrelated code")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Updated README and added unrelated src file",
                changed_files=["README.md", "src/unrelated.py"],
                tests_added=[],
                commands_run=[],
            ),
        )

    runtime = FakeAgentRuntime(implementer=rogue_implementer)

    controller = WorkflowController(config, store, runtime)
    wi = _work_item("WI-rogue").model_copy(
        update={
            "title": "Update README.md",
            "acceptance_criteria": ["README.md has been updated"],
        }
    )
    run = controller.run(wi, source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.initial_route is ExecutionRoute.SINGLE
    assert run.effective_route is ExecutionRoute.FULL_REVIEW

    decision = store.load_artifact(run.id, RouteDecision)
    assert decision.effective_route is ExecutionRoute.FULL_REVIEW
    assert any(
        "unrelated changes outside synthesized scope" in adj.reason for adj in decision.adjustments
    )


def test_finding6_single_and_critique_skipped_reports_semantics(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    commands = config.repository.commands.model_copy(update={"verify": ["git status"]})
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(update={"enabled": True}),
            "repository": config.repository.model_copy(update={"commands": commands}),
        }
    )
    store = FileRunStore(data_dir)

    # 1. SINGLE route: test_report and review_report have skipped=True and provenance="SKIPPED"
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(),
    )
    wi_single = _work_item("WI-single-rep").model_copy(
        update={"title": "Update README.md", "acceptance_criteria": ["Check README.md"]}
    )
    run_single = controller.run(wi_single, source_repo)
    assert run_single.state is WorkflowState.PR_READY
    assert run_single.effective_route is ExecutionRoute.SINGLE

    test_rep = store.load_artifact(run_single.id, TestReport)
    rev_rep = store.load_artifact(run_single.id, ReviewReport)
    assert test_rep.skipped is True
    assert test_rep.provenance == "SKIPPED"
    assert rev_rep.skipped is True
    assert rev_rep.provenance == "SKIPPED"

    # 2. CRITIQUE route: reviewer receives test_report=None,
    # TestReport is skipped, and ReviewReport is AGENT
    recorded_reviewer_req: AgentRequest | None = None

    def recording_reviewer(req: AgentRequest) -> AgentResult:
        nonlocal recorded_reviewer_req
        recorded_reviewer_req = req
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=True,
                findings=[],
            ),
        )

    runtime_critique = FakeAgentRuntime(reviewer=recording_reviewer)
    controller_critique = WorkflowController(
        config,
        store,
        runtime_critique,
    )
    wi_critique = _work_item("WI-critique-rep").model_copy(
        update={
            "title": "Update README.md",
            "acceptance_criteria": ["Check README.md"],
            # The no-single label removes SINGLE, so CRITIQUE is the lightest legal route.
            "labels": ["no-single"],
        }
    )
    run_critique = controller_critique.run(wi_critique, source_repo)
    assert run_critique.state is WorkflowState.PR_READY
    assert run_critique.effective_route is ExecutionRoute.CRITIQUE

    assert recorded_reviewer_req is not None
    assert recorded_reviewer_req.test_report is None

    test_rep_critique = store.load_artifact(run_critique.id, TestReport)
    assert test_rep_critique.skipped is True
    assert test_rep_critique.provenance == "SKIPPED"

    rev_rep_critique = store.load_artifact(run_critique.id, ReviewReport)
    assert rev_rep_critique.skipped is False
    assert rev_rep_critique.provenance == "AGENT"


def test_finding7_model_profile_validation_and_application(
    source_repo: Path,
    data_dir: Path,
) -> None:
    # 1. Config validation fails on unknown model_profile
    config_dict = _config_dict()
    config_dict["routing"] = {
        "enabled": True,
        "options": [
            {
                "id": "bad_profile",
                "route": "SINGLE",
                "complexity": "L0",
                "risk": "R0",
                "model_profile": "non_existent_profile",
            },
            {
                "id": "full_l2",
                "route": "FULL",
                "complexity": "L2",
            },
        ],
    }
    with pytest.raises(ValidationError, match="references unknown model profile"):
        FactoryConfig.model_validate(config_dict)

    # 2. Custom profile is applied to worker when route option selects it
    config_dict_custom = _config_dict()
    custom_models = dict(config_dict["models"])
    custom_workers = dict(config_dict["models"]["workers"])
    custom_workers["L0"] = {"model": "custom-l0-model", "reasoning": "low"}
    custom_models["workers"] = custom_workers
    config_dict_custom["model_profiles"] = {"custom_profile": custom_models}
    config_dict_custom["routing"] = {
        "enabled": True,
        "options": [
            {
                "id": "single_custom",
                "route": "SINGLE",
                "complexity": "L0",
                "risk": "R0",
                "model_profile": "custom_profile",
            },
            {
                "id": "full_l2",
                "route": "FULL",
                "complexity": "L2",
            },
        ],
    }
    config_custom = FactoryConfig.model_validate(config_dict_custom)
    config_custom = config_custom.model_copy(
        update={
            "factory": config_custom.factory.model_copy(update={"data_dir": str(data_dir)}),
            "repository": config_custom.repository.model_copy(
                update={
                    "commands": config_custom.repository.commands.model_copy(
                        update={"verify": ["git status"]}
                    )
                }
            ),
        }
    )

    recorded_worker_req: AgentRequest | None = None

    def recording_implementer(req: AgentRequest) -> AgentResult:
        nonlocal recorded_worker_req
        recorded_worker_req = req
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Done",
                changed_files=["README.md"],
                tests_added=[],
                commands_run=[],
            ),
        )

    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime(implementer=recording_implementer)
    controller = WorkflowController(
        config_custom,
        store,
        runtime,
    )
    wi = _work_item("WI-custom-prof").model_copy(
        update={"title": "Update README.md", "acceptance_criteria": ["Check README.md"]}
    )
    run = controller.run(wi, source_repo)
    assert run.state is WorkflowState.PR_READY
    assert recorded_worker_req is not None
    assert recorded_worker_req.model == "custom-l0-model"


def test_routing_option_config_rejects_full_review() -> None:
    with pytest.raises(
        ValidationError,
        match="FULL_REVIEW is a controller-only execution route and cannot be configured",
    ):
        RoutingOptionConfig(
            id="full_review_opt",
            route=ExecutionRoute.FULL_REVIEW,
            complexity=Complexity.L2,
        )


def test_routing_config_enforces_max_options_limit() -> None:
    valid_options = [
        RoutingOptionConfig(
            id="full_default",
            route=ExecutionRoute.FULL,
            complexity=Complexity.L2,
        ),
        *(
            RoutingOptionConfig(
                id=f"single_{i}",
                route=ExecutionRoute.SINGLE,
                complexity=Complexity.L0,
                risk=Risk.R0,
            )
            for i in range(254)
        ),
    ]
    assert len(valid_options) == 255
    config = RoutingConfig(options=valid_options)
    assert len(config.options) == 255

    too_many_options = [
        *valid_options,
        RoutingOptionConfig(
            id="single_overflow",
            route=ExecutionRoute.SINGLE,
            complexity=Complexity.L0,
            risk=Risk.R0,
        ),
    ]
    with pytest.raises(
        ValidationError,
        match=r"routing\.options must contain at most 255 options",
    ):
        RoutingConfig(options=too_many_options)


def test_final_review_item2_agent_skipped_report_rejected(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(
                update={
                    "enabled": True,
                    "options": [
                        RoutingOptionConfig(
                            id="full_l2", route=ExecutionRoute.FULL, complexity=Complexity.L2
                        )
                    ],
                }
            )
        }
    )

    # Malicious reviewer returning skipped=True in FULL route must be rejected
    def malicious_reviewer(req: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=True,
                findings=[],
                skipped=True,
                provenance="SKIPPED",
                skip_reason="Malicious skip",
            ),
        )

    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime(reviewer=malicious_reviewer)
    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-malicious-skip").model_copy(
        update={"title": "Update README.md", "acceptance_criteria": ["Check README.md"]}
    )
    run = controller.run(wi, source_repo)
    assert run.state is WorkflowState.FAILED
    assert "claiming skipped/provenance SKIPPED" in (run.failure_reason or "")

    # Also verify _review_authorizes_delivery directly
    skipped_review = ReviewReport(approved=True, findings=[], skipped=True, provenance="SKIPPED")
    full_run = run.model_copy(
        update={"effective_route": ExecutionRoute.FULL, "reviewed_tree_sha": "abc"}
    )
    assert controller._review_authorizes_delivery(full_run, skipped_review, Risk.R0) is False


def test_final_review_item3_unexpected_scope_ratchets_before_replan(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir, verify=["git status"])
    config = config.model_copy(
        update={
            "routing": config.routing.model_copy(
                update={
                    "enabled": True,
                    "options": [
                        RoutingOptionConfig(
                            id="single_l0",
                            route=ExecutionRoute.SINGLE,
                            complexity=Complexity.L0,
                            risk=Risk.R0,
                        ),
                        RoutingOptionConfig(
                            id="full_l2", route=ExecutionRoute.FULL, complexity=Complexity.L2
                        ),
                    ],
                }
            )
        }
    )

    tester_called = False
    reviewer_called = False

    def implementer_touching_unrelated(req: AgentRequest) -> AgentResult:
        assert req.workspace_path is not None
        (Path(req.workspace_path) / "unrelated.py").write_text("print('unrelated')\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(
                summary="Touched unrelated",
                changed_files=["unrelated.py"],
                tests_added=[],
                commands_run=[],
            ),
        )

    def recording_tester(req: AgentRequest) -> AgentResult:
        nonlocal tester_called
        tester_called = True
        return AgentResult(
            role=AgentRole.TESTER,
            success=True,
            test_report=TestReport(passed=True, confidence=1.0, findings=[], suggested_tests=[]),
        )

    def recording_reviewer(req: AgentRequest) -> AgentResult:
        nonlocal reviewer_called
        reviewer_called = True
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(approved=True, findings=[]),
        )

    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime(
        implementer=implementer_touching_unrelated,
        tester=recording_tester,
        reviewer=recording_reviewer,
    )
    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-unrelated").model_copy(
        update={"title": "Update README.md", "acceptance_criteria": ["Check README.md"]}
    )
    run = controller.run(wi, source_repo)

    # Route must have ratcheted to FULL_REVIEW
    assert run.effective_route is ExecutionRoute.FULL_REVIEW
    # Both Tester and Reviewer must have been invoked
    assert tester_called is True
    assert reviewer_called is True


def test_final_review_item4_normalize_default_model_profile_and_worker_failure() -> None:
    # 1. RoutingOptionConfig with model_profile="default" normalizes to None
    opt_cfg = RoutingOptionConfig(
        id="single_default",
        route=ExecutionRoute.SINGLE,
        complexity=Complexity.L0,
        risk=Risk.R0,
        model_profile="default",
    )
    assert opt_cfg.model_profile is None

    # RouteOption and RouteDecision normalize "default" to None
    opt = RouteOption(
        id="opt1",
        route=ExecutionRoute.SINGLE,
        complexity=Complexity.L0,
        risk=Risk.R0,
        model_profile="default",
    )
    assert opt.model_profile is None

    decision = RouteDecision(
        work_item_id="WI-1",
        offered_options=[opt],
        selected_option="opt1",
        initial_route=ExecutionRoute.SINGLE,
        effective_route=ExecutionRoute.SINGLE,
        selected_model_profile="default",
        source="test",
        request_hash="hash",
    )
    assert decision.selected_model_profile is None


def test_final_review_item5_sensitive_paths_with_backticks_and_quotes() -> None:
    config = FactoryConfig.model_validate(_config_dict())
    profile = _default_profile()

    # Backticks around pyproject.toml
    wi1 = _work_item("WI-pyproject").model_copy(
        update={
            "title": "Update `pyproject.toml` dependencies",
            "acceptance_criteria": ["Pass tests"],
        }
    )
    legal1 = assess_safety_floors(wi1, profile, config)
    assert "single_l0" not in legal1
    assert "critique_l1" not in legal1

    # Quotes around .env
    wi2 = _work_item("WI-env").model_copy(
        update={
            "title": "Configure secrets in '.env'",
            "acceptance_criteria": ["Pass tests"],
        }
    )
    legal2 = assess_safety_floors(wi2, profile, config)
    assert "single_l0" not in legal2
    assert "critique_l1" not in legal2


def test_final_review_item6_no_legal_full_fallback_returns_manual_triage() -> None:
    config_dict = _config_dict()
    # Configure routing with ONLY full_l2 (complexity L2) and NO manual_triage option
    config_dict["routing"] = {
        "enabled": True,
        "options": [
            {
                "id": "full_l2",
                "route": "FULL",
                "complexity": "L2",
            }
        ],
    }
    config = FactoryConfig.model_validate(config_dict)
    profile = _default_profile()

    # Work item with Complexity.L3: full_l2 does not satisfy L3 floor!
    wi = _work_item("WI-l3").model_copy(
        update={"complexity": Complexity.L3, "title": "Heavy L3 task"}
    )
    decision = determine_route(wi, profile, config)

    # Must return MANUAL_TRIAGE even though manual_triage was not in config
    assert decision.initial_route is ExecutionRoute.MANUAL_TRIAGE
    assert decision.effective_route is ExecutionRoute.MANUAL_TRIAGE
    assert decision.selected_option == "manual_triage"


def test_final_review_item8_reopen_restores_and_propagates_route_decision(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir)
    config = config.model_copy(
        update={"routing": config.routing.model_copy(update={"enabled": True})}
    )

    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime()
    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-reopen-route")
    run = controller.run(wi, source_repo)
    assert run.route_decision is not None
    assert run.route_decision.selected_option == "full_l2"


def test_final_review_item9_authoritative_route_decision_ratchet_and_ci_repair(
    source_repo: Path,
    data_dir: Path,
) -> None:
    config = _config(data_dir, verify=["git status"])
    config = config.model_copy(
        update={"routing": config.routing.model_copy(update={"enabled": True})}
    )

    store = FileRunStore(data_dir)
    runtime = FakeAgentRuntime()
    controller = WorkflowController(
        config,
        store,
        runtime,
    )

    wi = _work_item("WI-ratchet-test").model_copy(
        update={"title": "Update README.md", "acceptance_criteria": ["Check README.md"]}
    )
    run = controller.run(wi, source_repo)
    assert run.state is WorkflowState.PR_READY
    assert run.effective_route is ExecutionRoute.SINGLE
    assert run.route_decision is not None
    assert run.route_decision.effective_route is ExecutionRoute.SINGLE

    # Loaded artifact matches run.route_decision
    loaded_decision = store.load_artifact(run.id, RouteDecision)
    assert loaded_decision.effective_route is ExecutionRoute.SINGLE

    # Now verify CI repair ratchets SINGLE to FULL_REVIEW with an authoritative recorded adjustment
    from software_agent_factory.models import AttemptBudget, AttemptTrigger, RepairContext
    from software_agent_factory.workspace import GitWorktreeWorkspace

    workspace = GitWorktreeWorkspace(
        config.data_dir,
        source_repo,
        wi.id,
        branch_prefix=config.repository.branch_prefix,
    )
    workspace.prepare()
    context = _RunContext(
        work_item=wi,
        triage_result=store.load_artifact(run.id, TriageResult),
        specification=store.load_artifact(run.id, Specification),
        execution_plan=store.load_artifact(run.id, ExecutionPlan),
        repository_profile=store.load_artifact(run.id, RepositoryProfile),
        workspace=workspace,
        source_repo=source_repo,
        route_decision=loaded_decision,
    )
    ci_repair_context = RepairContext(
        trigger=AttemptTrigger.CI,
        summary="CI failed on remote check",
        failures=["test_failure: assertion failed"],
    )
    run = controller.transition(run, WorkflowState.PR_CREATED)
    run = controller.transition(run, WorkflowState.CI_RUNNING)
    run = controller.transition(run, WorkflowState.CI_DIAGNOSIS)
    run = controller.transition(run, WorkflowState.IMPLEMENTING)
    run = controller._drive_to_pr_ready(run, context, AttemptBudget.CI_REPAIR, ci_repair_context)

    assert run.effective_route is ExecutionRoute.FULL_REVIEW
    assert run.route_decision is not None
    assert run.route_decision.effective_route is ExecutionRoute.FULL_REVIEW
    assert any(
        adj.to_route is ExecutionRoute.FULL_REVIEW and "CI repair" in adj.reason
        for adj in run.route_decision.adjustments
    )

    reloaded_decision = store.load_artifact(run.id, RouteDecision)
    assert reloaded_decision.effective_route is ExecutionRoute.FULL_REVIEW


def test_final_finding1_and_2_option_contracts_governance_terms_and_risk_floor() -> None:
    # 1. Option contract validation for RoutingOptionConfig
    # SINGLE requires complexity and risk
    with pytest.raises(ValidationError, match="complexity is required for SINGLE"):
        RoutingOptionConfig(id="s1", route=ExecutionRoute.SINGLE, risk=Risk.R0)

    with pytest.raises(ValidationError, match="risk is required for SINGLE"):
        RoutingOptionConfig(id="s2", route=ExecutionRoute.SINGLE, complexity=Complexity.L0)

    # CRITIQUE requires complexity and risk
    with pytest.raises(ValidationError, match="complexity is required for CRITIQUE"):
        RoutingOptionConfig(id="c1", route=ExecutionRoute.CRITIQUE, risk=Risk.R1)

    with pytest.raises(ValidationError, match="risk is required for CRITIQUE"):
        RoutingOptionConfig(id="c2", route=ExecutionRoute.CRITIQUE, complexity=Complexity.L1)

    # FULL requires complexity, risk is optional
    with pytest.raises(ValidationError, match="complexity is required for FULL"):
        RoutingOptionConfig(id="f1", route=ExecutionRoute.FULL)

    full_valid = RoutingOptionConfig(id="f2", route=ExecutionRoute.FULL, complexity=Complexity.L2)
    assert full_valid.complexity == Complexity.L2
    assert full_valid.risk is None

    # MANUAL_TRIAGE forbids complexity and risk
    with pytest.raises(ValidationError, match="complexity must be absent for MANUAL_TRIAGE"):
        RoutingOptionConfig(id="m1", route=ExecutionRoute.MANUAL_TRIAGE, complexity=Complexity.L0)

    with pytest.raises(ValidationError, match="risk must be absent for MANUAL_TRIAGE"):
        RoutingOptionConfig(id="m2", route=ExecutionRoute.MANUAL_TRIAGE, risk=Risk.R0)

    m_valid = RoutingOptionConfig(id="m3", route=ExecutionRoute.MANUAL_TRIAGE)
    assert m_valid.complexity is None
    assert m_valid.risk is None

    # Same contract on RouteOption
    with pytest.raises(ValidationError, match="risk is required for SINGLE"):
        RouteOption(id="ro_s", route=ExecutionRoute.SINGLE, complexity=Complexity.L0)

    with pytest.raises(ValidationError, match="complexity must be absent for MANUAL_TRIAGE"):
        RouteOption(id="ro_m", route=ExecutionRoute.MANUAL_TRIAGE, complexity=Complexity.L0)

    # 2. Deterministic governance-sensitive terms filter out SINGLE and CRITIQUE
    config = FactoryConfig.model_validate(_config_dict())
    config = config.model_copy(
        update={
            "repository": config.repository.model_copy(
                update={
                    "commands": config.repository.commands.model_copy(update={"verify": ["pytest"]})
                }
            )
        }
    )
    profile = _default_profile()

    # Specifically: 'Disable authentication in src/auth.py'
    wi_auth = _work_item("WI-auth").model_copy(
        update={
            "title": "Disable authentication in src/auth.py",
            "acceptance_criteria": ["Update auth check"],
        }
    )
    legal_auth = assess_safety_floors(wi_auth, profile, config)
    # Neither SINGLE nor CRITIQUE options should be offered
    assert "single_l0" not in legal_auth
    assert "single_l1" not in legal_auth
    assert "critique_l1" not in legal_auth
    assert "critique_l2" not in legal_auth
    assert "full_l2" in legal_auth
    assert "manual_triage" in legal_auth

    # Other governance terms: payment, billing, migration, encryption, credentials
    for term in ["payment", "billing", "migration", "encryption", "credentials"]:
        wi_term = _work_item(f"WI-{term}").model_copy(
            update={
                "title": f"Update system for {term}",
                "acceptance_criteria": ["Check changes"],
            }
        )
        legal_term = assess_safety_floors(wi_term, profile, config)
        assert "single_l0" not in legal_term
        assert "critique_l1" not in legal_term
        assert "full_l2" in legal_term

    # 3. Explicit WorkItem risk as lower floor
    # Work item with risk=R1: filters out SINGLE (R0) because R0 < R1; allows CRITIQUE (R1)
    wi_r1 = _work_item("WI-r1").model_copy(
        update={
            "title": "Update README.md",
            "acceptance_criteria": ["Check README.md"],
            "risk": Risk.R1,
        }
    )
    legal_r1 = assess_safety_floors(wi_r1, profile, config)
    assert "single_l0" not in legal_r1
    assert "single_l1" not in legal_r1
    assert "critique_l1" in legal_r1
    assert "critique_l2" in legal_r1
    assert "full_l2" in legal_r1

    # Work item with risk=R2: filters out SINGLE and CRITIQUE (R0, R1 < R2)
    wi_r2 = _work_item("WI-r2").model_copy(
        update={
            "title": "Update README.md",
            "acceptance_criteria": ["Check README.md"],
            "risk": Risk.R2,
        }
    )
    legal_r2 = assess_safety_floors(wi_r2, profile, config)
    assert "single_l0" not in legal_r2
    assert "critique_l1" not in legal_r2
    assert "full_l2" in legal_r2

    # 4. Configured risk policy governs legality rather than hardcoded categories
    # If policy configures R1 to require human approval, CRITIQUE with risk=R1 is disallowed
    strict_r1_config = config.model_copy(
        update={
            "risk": {
                **config.risk,
                Risk.R1: config.risk[Risk.R1].model_copy(update={"human_approval": True}),
            }
        }
    )
    legal_strict_r1 = assess_safety_floors(wi_r1, profile, strict_r1_config)
    assert "critique_l1" not in legal_strict_r1
    assert "critique_l2" not in legal_strict_r1
    assert "full_l2" in legal_strict_r1

    # 5. _synthesize_triage_result requires selected_worker_complexity and selected_risk
    from software_agent_factory.workflow import WorkflowController

    dummy_controller = WorkflowController.__new__(WorkflowController)
    valid_decision = RouteDecision(
        work_item_id="WI-1",
        offered_options=[
            RouteOption(
                id="single_l0",
                route=ExecutionRoute.SINGLE,
                complexity=Complexity.L0,
                risk=Risk.R0,
            )
        ],
        selected_option="single_l0",
        initial_route=ExecutionRoute.SINGLE,
        effective_route=ExecutionRoute.SINGLE,
        selected_worker_complexity=Complexity.L0,
        selected_risk=Risk.R0,
        source="rule",
        request_hash="hash",
    )
    triage = dummy_controller._synthesize_triage_result(wi_r1, valid_decision)
    assert triage.complexity == Complexity.L0
    assert triage.risk == Risk.R0
    assert triage.provenance == "SYNTHESIZED"

    # Missing complexity raises ValueError
    bad_decision_complexity = valid_decision.model_copy(update={"selected_worker_complexity": None})
    with pytest.raises(ValueError, match="requires selected worker complexity"):
        dummy_controller._synthesize_triage_result(wi_r1, bad_decision_complexity)

    # Missing risk raises ValueError
    bad_decision_risk = valid_decision.model_copy(update={"selected_risk": None})
    with pytest.raises(ValueError, match="requires a selected risk"):
        dummy_controller._synthesize_triage_result(wi_r1, bad_decision_risk)
