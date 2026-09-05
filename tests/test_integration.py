"""End-to-end integration tests for the assembled factory (Phases 2-14).

Every test here is hermetic: no paid model call (``FakeAgentRuntime`` only)
and no live GitHub/network access (every ``git``/``gh`` boundary that talks to
a remote is driven through an injected fake ``CommandRunner``). Real ``git``
is only used against throwaway repositories created under ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from factory_testing import (
    ScriptedRunner,
    build_config,
    build_controller,
    check_payload,
    repair_contexts,
    triage_hook,
    work_item,
)

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.models import (
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    ChangeSet,
    CIReport,
    Complexity,
    ReviewReport,
    Risk,
    TestReport,
    VerificationReport,
    WorkflowState,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def _check(name: str, bucket: str, *, description: str = "", link: str = "") -> dict[str, str]:
    return check_payload(name, bucket, description=description, link=link)


def _repair_contexts(requests: list[AgentRequest]) -> list:
    return repair_contexts(requests)


# ---------------------------------------------------------------------------
# Pull requests disabled (default): PR_READY is the completed endpoint
# ---------------------------------------------------------------------------


def test_pull_requests_disabled_completes_at_pr_ready_without_git_or_gh(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner()
    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert run.completed_at is not None, "PR_READY must be explicitly finalized"
    assert run.lease is None
    assert run.commit_sha is None
    assert run.pull_request_url is None
    # Nothing ever reached the publishing/CI boundary.
    assert runner.calls == []


def test_pull_requests_disabled_persists_every_artifact_including_test_report(
    source_repo: Path, data_dir: Path
) -> None:
    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    run = controller.run(work_item(), source_repo)

    run_dir = store.runs_dir / run.id
    for filename in (
        "run.json",
        "work-item.json",
        "triage.json",
        "specification.json",
        "execution-plan.json",
        "change-set.json",
        "patch.diff",
        "verification.json",
        "test-report.json",
        "review.json",
    ):
        assert (run_dir / filename).exists(), f"missing {filename}"

    # Per-attempt snapshots exist alongside the latest top-level snapshot.
    assert store.list_attempts(run.id) == [1]
    attempt_dir = store.attempt_dir(run.id, 1)
    for filename in (
        "change-set.json",
        "patch.diff",
        "verification.json",
        "test-report.json",
        "review.json",
    ):
        assert (attempt_dir / filename).exists(), f"missing attempt snapshot {filename}"

    test_report = store.load_artifact(run.id, TestReport)
    assert test_report.passed is True


# ---------------------------------------------------------------------------
# Independent gate contracts
# ---------------------------------------------------------------------------


def test_tester_and_reviewer_receive_authoritative_evidence_only(
    source_repo: Path, data_dir: Path
) -> None:
    seen: dict[AgentRole, AgentRequest] = {}

    def record(role: AgentRole):
        def hook(request: AgentRequest) -> AgentResult:
            seen[role] = request
            return FakeAgentRuntime()._default(request)

        return hook

    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(tester=record(AgentRole.TESTER), reviewer=record(AgentRole.REVIEWER)),
    )

    run = controller.run(work_item(), source_repo)
    assert run.state is WorkflowState.PR_READY

    tester = seen[AgentRole.TESTER]
    assert tester.diff is not None and "FACTORY_NOTES.md" in tester.diff
    assert tester.changed_files == ["FACTORY_NOTES.md"]
    assert isinstance(tester.verification_report, VerificationReport)
    assert tester.verification_report.passed is True
    # No implementer self-justification is ever handed to an independent gate.
    assert tester.change_set is None

    reviewer = seen[AgentRole.REVIEWER]
    assert reviewer.diff == tester.diff
    assert reviewer.changed_files == ["FACTORY_NOTES.md"]
    assert isinstance(reviewer.verification_report, VerificationReport)
    assert isinstance(reviewer.test_report, TestReport)
    assert reviewer.change_set is None


def test_broken_deterministic_checks_never_reach_the_tester_or_reviewer(
    source_repo: Path, data_dir: Path
) -> None:
    invoked: list[AgentRole] = []

    def record(role: AgentRole):
        def hook(request: AgentRequest) -> AgentResult:
            invoked.append(role)
            return FakeAgentRuntime()._default(request)

        return hook

    config = build_config(data_dir, verify=["false"], same_model_attempts=1, max_total_attempts=2)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(tester=record(AgentRole.TESTER), reviewer=record(AgentRole.REVIEWER)),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert invoked == []


def test_install_verify_and_build_run_in_order_with_persisted_logs(
    source_repo: Path, data_dir: Path
) -> None:
    config = build_config(
        data_dir,
        install=["echo installing"],
        verify=["echo verifying"],
        build=["echo building"],
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    report = store.load_artifact(run.id, VerificationReport)
    assert [check.command for check in report.deterministic_checks] == [
        "echo installing",
        "echo verifying",
        "echo building",
    ]

    logs = sorted(path.name for path in (store.runs_dir / run.id / "logs").glob("*.log"))
    assert any("install" in name for name in logs)
    assert any("verify" in name for name in logs)
    assert any("build" in name for name in logs)


# ---------------------------------------------------------------------------
# Bounded repair and repair context
# ---------------------------------------------------------------------------


def test_verification_failure_produces_a_verification_repair_context(
    source_repo: Path, data_dir: Path
) -> None:
    requests: list[AgentRequest] = []

    def recording_implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        return FakeAgentRuntime()._default_implementer(request)

    config = build_config(
        data_dir,
        verify=["echo boom-from-the-verify-command >&2; exit 3"],
        same_model_attempts=1,
        max_total_attempts=2,
    )
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config, store, FakeAgentRuntime(implementer=recording_implementer)
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert len(run.attempt_records) == 2
    assert [record.budget for record in run.attempt_records] == [
        AttemptBudget.IMPLEMENTATION,
        AttemptBudget.IMPLEMENTATION,
    ]
    assert [record.triggered_by for record in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.VERIFICATION,
    ]

    contexts = _repair_contexts(requests)
    assert len(contexts) == 1
    assert contexts[0].trigger is AttemptTrigger.VERIFICATION
    assert contexts[0].failures
    assert contexts[0].log_excerpt is not None
    assert "boom-from-the-verify-command" in contexts[0].log_excerpt
    # The repair attempt also sees the current controller-derived diff.
    assert requests[1].diff is not None and "FACTORY_NOTES.md" in requests[1].diff


def test_review_rejection_produces_a_review_repair_context(
    source_repo: Path, data_dir: Path
) -> None:
    requests: list[AgentRequest] = []

    def recording_implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        return FakeAgentRuntime()._default_implementer(request)

    def rejecting_reviewer(request: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.REVIEWER,
            success=True,
            review_report=ReviewReport(
                approved=False,
                findings=["The whitespace-only case is still unhandled."],
                suggested_changes=["Add a guard clause."],
            ),
        )

    config = build_config(data_dir, same_model_attempts=1, max_total_attempts=2)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(implementer=recording_implementer, reviewer=rejecting_reviewer),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert [record.triggered_by for record in run.attempt_records] == [
        AttemptTrigger.INITIAL,
        AttemptTrigger.REVIEW,
    ]
    contexts = _repair_contexts(requests)
    assert contexts[0].trigger is AttemptTrigger.REVIEW
    assert "The whitespace-only case is still unhandled." in contexts[0].failures


def test_implementer_failure_produces_an_implementer_repair_context(
    source_repo: Path, data_dir: Path
) -> None:
    requests: list[AgentRequest] = []

    def failing_then_ok(request: AgentRequest) -> AgentResult:
        requests.append(request)
        if request.attempt_number == 1:
            return AgentResult(
                role=AgentRole.IMPLEMENTER, success=False, failure_reason="tooling exploded"
            )
        return FakeAgentRuntime()._default_implementer(request)

    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime(implementer=failing_then_ok))

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.PR_READY
    assert [record.attempt_number for record in run.attempt_records] == [1, 2]
    assert [record.outcome for record in run.attempt_records] == ["failed", "succeeded"]
    assert run.attempt_records[1].triggered_by is AttemptTrigger.IMPLEMENTER_FAILURE
    contexts = _repair_contexts(requests)
    assert contexts[0].trigger is AttemptTrigger.IMPLEMENTER_FAILURE
    assert contexts[0].failures == ["tooling exploded"]


def test_scope_drift_replan_is_bounded_and_carries_scope_findings(
    source_repo: Path, data_dir: Path
) -> None:
    """A change touching a dependency manifest is sensitive; at R1 the policy
    replans once, then escalates rather than looping forever."""
    implementer_requests: list[AgentRequest] = []
    planner_requests: list[AgentRequest] = []

    def drifting_implementer(request: AgentRequest) -> AgentResult:
        implementer_requests.append(request)
        assert request.workspace_path is not None
        (Path(request.workspace_path) / "package.json").write_text(
            json.dumps({"name": "demo", "attempt": request.attempt_number}) + "\n"
        )
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="touched dependencies"),
        )

    def recording_planner(request: AgentRequest) -> AgentResult:
        planner_requests.append(request)
        return FakeAgentRuntime()._default_planner(request)

    config = build_config(data_dir, max_replans=1)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(implementer=drifting_implementer, planner=recording_planner),
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "scope drift replan budget exhausted" in (run.failure_reason or "")
    # Exactly one replan happened: the initial plan plus one re-plan.
    assert len(planner_requests) == 2
    assert planner_requests[1].repair_context is not None
    assert planner_requests[1].changed_files == ["package.json"]
    scope_attempts = [
        record for record in run.attempt_records if record.triggered_by is AttemptTrigger.SCOPE
    ]
    assert len(scope_attempts) == 1
    contexts = _repair_contexts(implementer_requests)
    assert contexts[0].trigger is AttemptTrigger.SCOPE
    assert any("dependency" in failure for failure in contexts[0].failures)


def test_sensitive_scope_drift_at_high_risk_escalates_immediately(
    source_repo: Path, data_dir: Path
) -> None:
    def migration_implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        migrations = Path(request.workspace_path) / "migrations"
        migrations.mkdir(exist_ok=True)
        (migrations / "001_add_column.sql").write_text("ALTER TABLE customers ADD c TEXT;\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="added a migration"),
        )

    # R2 requires human approval up front, so use R1 triage with an R2-level
    # policy decision driven purely by the sensitive file category.
    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(
        config,
        store,
        FakeAgentRuntime(
            triage=triage_hook(Complexity.L1, Risk.R1), implementer=migration_implementer
        ),
    )

    run = controller.run(work_item(), source_repo)

    # At R1 a sensitive finding replans (bounded), then escalates.
    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "migration" in (run.failure_reason or "")


# ---------------------------------------------------------------------------
# Restart safety / recovery
# ---------------------------------------------------------------------------


def test_recover_abandoned_run_escalates_without_resetting_the_budget(
    source_repo: Path, data_dir: Path
) -> None:
    config = build_config(data_dir, verify=["false"], same_model_attempts=1, max_total_attempts=1)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())
    exhausted = controller.run(work_item("WI-budget"), source_repo)
    assert exhausted.state is WorkflowState.NEEDS_HUMAN
    assert len(exhausted.attempt_records) == 1

    # Simulate a process that died mid-implementation with attempts spent.
    stuck = exhausted.model_copy(
        update={
            "id": "run-abandoned",
            "state": WorkflowState.IMPLEMENTING,
            "completed_at": None,
            "failure_reason": None,
        }
    )
    store.save_run(stuck)

    recovered = controller.recover_abandoned_run(stuck)

    assert recovered.state is WorkflowState.NEEDS_HUMAN
    assert recovered.completed_at is not None
    # The persisted budget is never widened by a restart.
    assert len(recovered.attempt_records) == len(stuck.attempt_records)
    assert recovered.workspace_path == stuck.workspace_path
    assert store.load_run("run-abandoned") == recovered


def test_recover_leaves_finished_runs_untouched(source_repo: Path, data_dir: Path) -> None:
    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())
    finished = controller.run(work_item(), source_repo)

    assert controller.recover_abandoned_run(finished) == finished


def test_lock_contention_reports_already_active_without_persisting_a_run(
    source_repo: Path, data_dir: Path
) -> None:
    from software_agent_factory.workspace import GitWorktreeWorkspace

    config = build_config(data_dir)
    store = FileRunStore(data_dir)
    item = work_item("WI-locked")
    holder = GitWorktreeWorkspace(
        config.data_dir, source_repo, item.id, branch_prefix=config.repository.branch_prefix
    )
    holder.acquire_lock()
    try:
        controller = WorkflowController(config, store, FakeAgentRuntime())
        run = controller.run(item, source_repo)
    finally:
        holder.release_lock()

    assert run.state is WorkflowState.FAILED
    assert "already active" in (run.failure_reason or "")
    # No junk run is persisted for reconciliation to explain later.
    assert store.list_runs() == []


# ---------------------------------------------------------------------------
# Pull request creation (opt-in)
# ---------------------------------------------------------------------------


def test_pull_request_enabled_publishes_and_reaches_done_when_ci_disabled(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner()
    config = build_config(data_dir, pull_request={"enabled": True, "draft": True})
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    assert run.completed_at is not None
    assert run.commit_sha == runner.commit_sha
    assert run.pull_request_url == runner.pr_url

    pushes = [argv for argv in runner.commands("git") if "push" in argv]
    assert len(pushes) == 1
    assert "--force" not in pushes[0]
    assert pushes[0][-1] == "HEAD:refs/heads/factory/WI-1"
    assert not any("merge" in argv for argv in runner.commands("git"))

    create = [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "create"]]
    assert len(create) == 1
    assert "--draft" in create[0]
    assert create[0][create[0].index("--base") + 1] == "main"
    assert create[0][create[0].index("--head") + 1] == "factory/WI-1"
    assert not any(argv[1:3] == ["pr", "merge"] for argv in runner.commands("gh"))


def test_pr_body_contains_every_required_section(source_repo: Path, data_dir: Path) -> None:
    runner = ScriptedRunner()
    config = build_config(data_dir, verify=["echo checking"], pull_request={"enabled": True})
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    body = runner.pr_bodies[0]
    assert "Reject empty customer names" in body
    assert "Empty names are rejected with HTTP 400" in body
    assert "### Specification" in body
    assert "### Plan" in body
    assert "`FACTORY_NOTES.md`" in body
    assert "### Deterministic verification" in body
    assert "echo checking" in body
    assert "### Independent tester" in body
    assert "### Reviewer result" in body
    assert run.id in body


def test_base_branch_is_resolved_from_the_source_repository_when_unset(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner(base_branch="trunk")
    config = build_config(data_dir, pull_request={"enabled": True, "base_branch": None})
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    create = [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "create"]][0]
    assert create[create.index("--base") + 1] == "trunk"


def test_missing_remote_ends_needs_human_without_creating_a_pull_request(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner(remote_missing=True)
    config = build_config(data_dir, pull_request={"enabled": True})
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "could not publish" in (run.failure_reason or "")
    assert run.pull_request_url is None
    assert not any(argv[1:3] == ["pr", "create"] for argv in runner.commands("gh"))
    assert not any("push" in argv for argv in runner.commands("git"))


def test_protected_file_change_is_refused_before_publishing(
    source_repo: Path, data_dir: Path
) -> None:
    def secret_leaking_implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        (Path(request.workspace_path) / ".env").write_text("TOKEN=nope\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="added configuration"),
        )

    runner = ScriptedRunner(changed_files=(".env",))
    config = build_config(data_dir, pull_request={"enabled": True}, max_replans=0)
    store = FileRunStore(data_dir)
    controller = build_controller(
        config, store, FakeAgentRuntime(implementer=secret_leaking_implementer), runner
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert not any("push" in argv for argv in runner.commands("git"))


def test_excessive_changed_files_are_refused_before_publishing(
    source_repo: Path, data_dir: Path
) -> None:
    def sprawling_implementer(request: AgentRequest) -> AgentResult:
        assert request.workspace_path is not None
        workspace = Path(request.workspace_path)
        for index in range(4):
            (workspace / f"file_{index}.txt").write_text(f"{index}\n")
        return AgentResult(
            role=AgentRole.IMPLEMENTER,
            success=True,
            change_set=ChangeSet(summary="touched everything"),
        )

    def permissive_planner(request: AgentRequest) -> AgentResult:
        result = FakeAgentRuntime()._default_planner(request)
        assert result.execution_plan is not None
        plan = result.execution_plan.model_copy(
            update={
                "expected_scope": result.execution_plan.expected_scope.model_copy(
                    update={"estimated_files_max": 10}
                )
            }
        )
        return AgentResult(role=AgentRole.PLANNER, success=True, execution_plan=plan)

    runner = ScriptedRunner()
    config = build_config(data_dir, pull_request={"enabled": True}, max_changed_files=2)
    store = FileRunStore(data_dir)
    controller = build_controller(
        config,
        store,
        FakeAgentRuntime(implementer=sprawling_implementer, planner=permissive_planner),
        runner,
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "max_changed_files" in (run.failure_reason or "")
    assert not any("push" in argv for argv in runner.commands("git"))


# ---------------------------------------------------------------------------
# CI observation and bounded CI repair (opt-in)
# ---------------------------------------------------------------------------


def _ci_config(data_dir: Path, *, repair_attempts: int = 3, **kwargs: object):
    return build_config(
        data_dir,
        pull_request={"enabled": True},
        ci={
            "enabled": True,
            "poll_interval_seconds": 1,
            "max_wait_seconds": 5,
            "repair_attempts": repair_attempts,
        },
        **kwargs,  # type: ignore[arg-type]
    )


def test_ci_pass_reaches_done(source_repo: Path, data_dir: Path) -> None:
    runner = ScriptedRunner(check_responses=[[_check("build", "pass")]])
    config = _ci_config(data_dir)
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    report = store.load_artifact(run.id, CIReport)
    assert report.overall == "PASS"
    assert [check.name for check in report.checks] == ["build"]


def test_repairable_ci_failure_repairs_pushes_again_and_then_passes(
    source_repo: Path, data_dir: Path
) -> None:
    requests: list[AgentRequest] = []

    def recording_implementer(request: AgentRequest) -> AgentResult:
        requests.append(request)
        return FakeAgentRuntime()._default_implementer(request)

    runner = ScriptedRunner(
        check_responses=[
            [_check("unit-tests", "fail", description="AssertionError: expected 400")],
            [_check("unit-tests", "pass")],
        ]
    )
    config = _ci_config(data_dir)
    store = FileRunStore(data_dir)
    controller = build_controller(
        config, store, FakeAgentRuntime(implementer=recording_implementer), runner
    )

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.DONE
    ci_attempts = [
        record for record in run.attempt_records if record.budget is AttemptBudget.CI_REPAIR
    ]
    assert len(ci_attempts) == 1
    assert ci_attempts[0].triggered_by is AttemptTrigger.CI
    assert ci_attempts[0].attempt_number == 1

    contexts = _repair_contexts(requests)
    assert contexts[0].trigger is AttemptTrigger.CI
    assert contexts[0].failures == ["unit-tests: TEST_FAILURE"]

    # Per-attempt evidence is keyed by the run-global attempt index, so the
    # CI repair (whose per-budget attempt_number restarts at 1) does not
    # overwrite the pre-PR attempt's immutable snapshot.
    assert store.list_attempts(run.id) == [1, 2]
    first = store.load_patch(run.id, attempt=1)
    second = store.load_patch(run.id, attempt=2)
    assert first != second
    assert "Repair: none" in first
    assert "Repair: CI" in second

    # An additional normal commit is pushed to the same branch; the PR is
    # updated rather than recreated, and nothing is force-pushed or merged.
    pushes = [argv for argv in runner.commands("git") if "push" in argv]
    assert len(pushes) == 2
    assert all("--force" not in argv for argv in pushes)
    assert all(argv[-1] == "HEAD:refs/heads/factory/WI-1" for argv in pushes)
    creates = [argv for argv in runner.commands("gh") if argv[1:3] == ["pr", "create"]]
    assert len(creates) == 1


def test_non_repairable_ci_failure_escalates_with_evidence(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner(
        check_responses=[
            [_check("deploy", "fail", description="connection reset by peer")],
        ]
    )
    config = _ci_config(data_dir)
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "not repairable" in (run.failure_reason or "")
    assert "deploy=INFRA_FAILURE" in (run.failure_reason or "")
    assert not any(record.budget is AttemptBudget.CI_REPAIR for record in run.attempt_records)
    report = store.load_artifact(run.id, CIReport)
    assert report.overall == "FAIL"
    assert report.failed_checks[0].failure_category == "INFRA_FAILURE"
    # Only one push happened: no repair cycle was attempted.
    assert len([argv for argv in runner.commands("git") if "push" in argv]) == 1


def test_ci_repair_budget_is_bounded_and_separate_from_implementation(
    source_repo: Path, data_dir: Path
) -> None:
    runner = ScriptedRunner(
        check_responses=[[_check("unit-tests", "fail", description="tests failed")]]
    )
    config = _ci_config(data_dir, repair_attempts=2)
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "CI repair budget exhausted" in (run.failure_reason or "")

    implementation = [
        record for record in run.attempt_records if record.budget is AttemptBudget.IMPLEMENTATION
    ]
    ci_repairs = [
        record for record in run.attempt_records if record.budget is AttemptBudget.CI_REPAIR
    ]
    assert len(implementation) == 1, "the pre-PR budget is untouched by CI repair"
    assert len(ci_repairs) == 2
    assert [record.attempt_number for record in ci_repairs] == [1, 2]
    # PR update cycles are hard-capped by the CI repair budget.
    assert len([argv for argv in runner.commands("git") if "push" in argv]) == 3


def test_ci_timeout_escalates_to_needs_human(source_repo: Path, data_dir: Path) -> None:
    runner = ScriptedRunner(check_responses=[[_check("unit-tests", "pending")]])
    config = _ci_config(data_dir)
    store = FileRunStore(data_dir)
    controller = build_controller(config, store, FakeAgentRuntime(), runner)

    run = controller.run(work_item(), source_repo)

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert "still pending" in (run.failure_reason or "")
    report = store.load_artifact(run.id, CIReport)
    assert report.timed_out is True
