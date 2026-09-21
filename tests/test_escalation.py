"""Offline tests for GitHub escalation notices and authorized human reply loop.

Tests cover:
- Notification destination (open PR preferred, issue fallback, host validation, no arbitrary search)
- Body data minimization (no raw failure_reason, paths, issue body, diffs, logs)
- Fixed guidance fields only in notification comment
- Host/repository validation
- Persistence and retry (failures don't prevent NEEDS_HUMAN; bounded delivery attempts)
- Exact command parsing (@factory resume v1 run=<id> episode=<id>; no free-form args)
- Authorization (allowed identities, allowed associations)
- Bot and self rejection
- Wrong target / stale episode / replay / edited comment rejection
- Bounded polls / window / reopens
- Same-run resume & preserved attempt budget (RISK_APPROVAL -> REFINING)
- No reply text in agent prompts
- Service integration (concurrency, daily quota, AlreadyRunFilter)
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig, load_config
from software_agent_factory.escalation import (
    UNRESOLVED_DECISIONS_HALT_PREFIX,
    UNRESOLVED_DECISIONS_REASON_CODE,
    ValidationResult,
    build_escalation_comment,
    build_plan_decision_context,
    classify_halt_reason,
    deliver_escalation_notification,
    format_escalation_marker,
    is_authorized_author,
    parse_plan_decision_answers,
    parse_resume_command,
    poll_escalation_reply,
    resolve_escalation_target,
    validate_reply_candidate,
)
from software_agent_factory.github import (
    GitHubClient,
    GitHubComment,
    GitHubError,
)
from software_agent_factory.models import (
    AcceptedReplyReceipt,
    AgentRole,
    Complexity,
    EscalationRecord,
    EscalationStatus,
    EscalationTargetType,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    PlanDecisionAnswers,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    TriageResult,
    WorkflowState,
    WorkItem,
    utc_now,
)
from software_agent_factory.scheduler import TrackerItem
from software_agent_factory.service import AlreadyRunFilter, FactoryService
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import TransitionError, WorkflowController


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, responses: list[FakeCompletedProcess] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.default = FakeCompletedProcess(returncode=0, stdout="")

    def __call__(
        self, args: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None
    ):
        self.calls.append((list(args), cwd, dict(env) if env else None))
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            return FakeCompletedProcess(0, json.dumps({"id": 99999, "login": "factory-bot"}))
        if self.responses:
            return self.responses.pop(0)
        return self.default


def _make_config(
    data_dir: Path,
    *,
    escalation_enabled: bool = True,
    authorized_identities: list[str] | None = None,
    max_reopens: int = 3,
    reply_window_hours: int = 168,
    max_reply_polls_per_tick: int = 10,
    max_notification_attempts: int = 3,
    max_runs_per_day: int | None = 20,
    max_concurrent_tasks: int = 1,
) -> FactoryConfig:
    config = load_config()
    return config.model_copy(
        update={
            "factory": config.factory.model_copy(update={"data_dir": data_dir}),
            "escalation": config.escalation.model_copy(
                update={
                    "enabled": escalation_enabled,
                    "authorized_identities": authorized_identities or ["lead-dev", "reviewer-1"],
                    "max_reopens": max_reopens,
                    "reply_window_hours": reply_window_hours,
                    "max_reply_polls_per_tick": max_reply_polls_per_tick,
                    "max_notification_attempts": max_notification_attempts,
                }
            ),
            "scheduler": config.scheduler.model_copy(
                update={
                    "enabled": True,
                    "max_concurrent_tasks": max_concurrent_tasks,
                    "max_runs_per_day": max_runs_per_day,
                }
            ),
        }
    )


def _make_comment_payload(
    comment_id: int,
    body: str,
    login: str = "lead-dev",
    user_id: int = 1001,
    user_type: str = "User",
    author_association: str = "MEMBER",
    created_at: str | datetime | None = None,
    updated_at: str | datetime | None = None,
) -> dict:
    if created_at is None:
        created_at_str = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    elif isinstance(created_at, datetime):
        created_at_str = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        created_at_str = created_at

    if updated_at is None:
        updated_at_str = created_at_str
    elif isinstance(updated_at, datetime):
        updated_at_str = updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        updated_at_str = updated_at

    return {
        "id": comment_id,
        "url": f"https://api.github.com/repos/owner/repo/issues/comments/{comment_id}",
        "html_url": f"https://github.com/owner/repo/issues/1#issuecomment-{comment_id}",
        "body": body,
        "user": {
            "login": login,
            "id": user_id,
            "type": user_type,
        },
        "created_at": created_at_str,
        "updated_at": updated_at_str,
        "author_association": author_association,
    }


def _make_approval_context(
    run_id: str = "run-1",
    episode_id: str = "ep-1",
    work_item_id: str = "task-1",
    work_item_title: str = "Task",
    risk: Risk = Risk.R2,
    complexity: Complexity = Complexity.L1,
) -> RiskApprovalContext:
    from software_agent_factory.escalation import compute_approval_context_fingerprint

    rationale = RiskRationale(
        intended_outcome="Update production database schema safely.",
        sensitive_boundary="Production database trust boundary.",
        necessity="Work item requires migrating production customer records.",
        credible_scenario="Data migration error could corrupt customer accounts.",
        known_mitigations=["Run migration inside atomic transaction."],
        residual_risk="Potential brief transaction lock delay on high-load tables.",
    )
    decision_requested = (
        f"Approve advancing run {run_id} to REFINING under risk policy {risk.value}."
    )
    authorized_actions = [
        "Transition workflow from NEEDS_HUMAN to REFINING.",
        "Refine requirements into an explicit specification.",
        "Plan implementation steps within approved scope.",
        "Execute code changes in an isolated workspace.",
        "Run deterministic verification, tests, and review.",
    ]
    unauthorized_actions = [
        "Approval does not change task scope.",
        "Approval does not increase retry budgets.",
        "Approval does not bypass quality gates.",
        "Approval does not alter credential or permission policy.",
        "Approval does not change deployment policy.",
        "Approval does not override configured merge policy.",
    ]
    conditions_in_force = [
        "The approved scope remains restricted to this task.",
        "Deterministic verification must pass before review.",
        "Independent testing and review remain mandatory.",
        "Quality gates must pass before pull request creation.",
        "Approval resumes the same run at REFINING.",
        "Approval does not reset run history or attempt budgets.",
    ]
    fp = compute_approval_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        work_item_id=work_item_id,
        work_item_title=work_item_title,
        risk=risk.value,
        complexity=complexity.value,
        intended_outcome=rationale.intended_outcome,
        sensitive_boundary=rationale.sensitive_boundary,
        necessity=rationale.necessity,
        credible_scenario=rationale.credible_scenario,
        known_mitigations=rationale.known_mitigations,
        residual_risk=rationale.residual_risk,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING.value,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
    )
    return RiskApprovalContext(
        risk=risk,
        complexity=complexity,
        work_item_id=work_item_id,
        work_item_title=work_item_title,
        risk_rationale=rationale,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
        context_fingerprint=fp,
    )


# ---------------------------------------------------------------------------
# 1. Exact Command Parsing Tests
# ---------------------------------------------------------------------------


def test_parse_resume_command_valid() -> None:
    parsed = parse_resume_command("@factory resume v1 run=run-abc-123 episode=ep-987654")
    assert parsed == ("run-abc-123", "ep-987654")

    # Extra internal whitespace tolerated
    parsed2 = parse_resume_command("@factory   resume   v1   run=run-1   episode=ep-2")
    assert parsed2 == ("run-1", "ep-2")

    # Surrounding newlines / whitespace stripped
    parsed3 = parse_resume_command("\n  @factory resume v1 run=r-1 episode=e-1  \n")
    assert parsed3 == ("r-1", "e-1")


@pytest.mark.parametrize(
    "invalid_cmd",
    [
        "resume v1 run=r1 episode=e1",  # missing @factory
        "@factory resume run=r1 episode=e1",  # missing v1
        "@factory resume v2 run=r1 episode=e1",  # wrong version
        "/factory resume v1 run=r1 episode=e1",  # slash command not allowed
        "@factory resume v1 run=r1",  # missing episode
        "@factory resume v1 episode=e1",  # missing run
        "@factory resume v1 run=r1 episode=e1 extra args",  # extra arguments
        "@factory resume v1 run=r1 episode=e1\nPlease skip tests",  # multi-line guidance
        "Hello @factory resume v1 run=r1 episode=e1",  # leading words
        "",  # empty
    ],
)
def test_parse_resume_command_invalid(invalid_cmd: str) -> None:
    assert parse_resume_command(invalid_cmd) is None


def test_parse_plan_decision_answers_requires_exact_ordered_responses() -> None:
    parsed = parse_plan_decision_answers(
        "@factory answer v1 run=run-1 episode=ep-1\n"
        "1. Use SQLite for the local-first store.\n"
        "2. Keep the current public API.",
        decision_count=2,
    )

    assert parsed is not None
    assert parsed[:2] == ("run-1", "ep-1")
    assert [answer.answer for answer in parsed[2]] == [
        "Use SQLite for the local-first store.",
        "Keep the current public API.",
    ]

    assert (
        parse_plan_decision_answers(
            "@factory answer v1 run=run-1 episode=ep-1\n1. Use SQLite.\n3. Keep the API.",
            decision_count=2,
        )
        is None
    )
    assert (
        parse_plan_decision_answers(
            " @factory answer v1 run=run-1 episode=ep-1\n1. Use SQLite.",
            decision_count=1,
        )
        is None
    )
    assert (
        parse_plan_decision_answers(
            "@factory\tanswer v1 run=run-1 episode=ep-1\n1. Use SQLite.",
            decision_count=1,
        )
        is None
    )
    assert (
        parse_plan_decision_answers(
            "@factory answer v1 run=run-1 episode=ep-1\n1. Use SQLite.",
            decision_count=2,
        )
        is None
    )


# ---------------------------------------------------------------------------
# 2. Halt Category Classification & Data Minimization
# ---------------------------------------------------------------------------


def test_classify_halt_reason_risk_approval() -> None:
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason="risk R2 requires human approval",
    )
    classification, code, summary, action = classify_halt_reason(run)
    assert classification is ResumeClassification.RISK_APPROVAL
    assert code == "RISK_APPROVAL"
    assert "approval" in summary.lower()
    assert "risk" in action.lower()


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("Scope exceeded approved limits for planned files", "SCOPE_REVIEW"),
        (
            "scope drift requires human review: unplanned change src/risk_policy.py",
            "SCOPE_REVIEW",
        ),
        ("exhausted implementation attempt budget", "ATTEMPT_BUDGET_EXHAUSTED"),
        ("CI checks failed on the PR branch", "CI_INTERVENTION"),
        ("could not publish the pull request: permission denied", "DELIVERY_INTERVENTION"),
        ("workspace identity changed or was abandoned", "RECOVERY_INTERVENTION"),
        ("unexpected manual boundary", "MANUAL_INSPECTION"),
    ],
)
def test_classify_halt_reason_not_resumable(reason: str, expected_code: str) -> None:
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason=reason,
    )
    classification, code, summary, action = classify_halt_reason(run)
    assert classification is ResumeClassification.NOT_RESUMABLE
    assert code == expected_code
    assert summary
    assert action


def test_classify_halt_reason_unresolved_decisions_stable_prefix_wins(tmp_path: Path) -> None:
    # Suffixes with "scope" and "merge" must still classify as UNRESOLVED_DECISIONS
    for suffix in (
        "",
        ": scope boundary review needed",
        ": merge conflict with main",
        " (scope and merge choices)",
        ": attempt budget exceeded",
    ):
        run = FactoryRun(
            id="run-test-prefix",
            work_item_id="task-1",
            state=WorkflowState.NEEDS_HUMAN,
            failure_reason=f"{UNRESOLVED_DECISIONS_HALT_PREFIX}{suffix}",
        )
        classification, code, summary, action = classify_halt_reason(run)
        assert classification is ResumeClassification.PLAN_DECISION
        assert code == UNRESOLVED_DECISIONS_REASON_CODE
        assert "Reply with complete numbered decisions" in action


def test_classify_halt_reason_unresolved_decisions_safe_count_handling(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-unresolved-count",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason=UNRESOLVED_DECISIONS_HALT_PREFIX,
    )
    store.save_run(run)

    # 1. Without store or without ExecutionPlan: safe count is omitted, no model prose
    _, code, summary_no_plan, action = classify_halt_reason(run)
    assert code == UNRESOLVED_DECISIONS_REASON_CODE
    assert "Reply with complete numbered decisions" in action
    assert "The execution plan has unresolved architectural decisions." in summary_no_plan

    # 2. With persisted ExecutionPlan containing 2 decisions: safe count is included
    plan = ExecutionPlan(
        summary="Implement feature",
        steps=[],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=[
            "Need choice between SQLite and PostgreSQL.",
            "Need choice between REST and gRPC.",
        ],
    )
    store.save_artifact(run.id, plan)

    _, code, summary_with_plan, action = classify_halt_reason(run, store)
    assert code == UNRESOLVED_DECISIONS_REASON_CODE
    assert "2 unresolved architectural decisions" in summary_with_plan
    assert "Reply with complete numbered decisions" in action
    # Verify no model prose in summary or action
    assert "SQLite" not in summary_with_plan
    assert "PostgreSQL" not in summary_with_plan


def test_build_escalation_comment_data_minimization() -> None:
    sensitive_failure = (
        "Internal stack trace: File /Users/secret/repo/bad.py, line 42; "
        "issue body: 'Secret Customer Data'; git diff: +SECRET_KEY=12345"
    )
    run = FactoryRun(
        id="run-safe-999",
        work_item_id="task-secret",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason=sensitive_failure,
    )
    classification, code, summary, action = classify_halt_reason(run)
    comment = build_escalation_comment(
        run_id=run.id,
        episode_id="ep-secret-token",
        classification=classification,
        reason_code=code,
        summary=summary,
        next_action=action,
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
    )

    # Must contain stable hidden marker
    expected_marker = format_escalation_marker("run-safe-999", "ep-secret-token")
    assert expected_marker in comment

    # Must contain exact command instructions for RISK_APPROVAL
    if classification is ResumeClassification.RISK_APPROVAL:
        assert "@factory resume v1 run=run-safe-999 episode=ep-secret-token" in comment

    # Must NEVER leak sensitive raw failure text, paths, issue body, diffs, or logs
    assert "/Users/secret" not in comment
    assert "Secret Customer Data" not in comment
    assert "SECRET_KEY" not in comment
    assert "bad.py" not in comment
    assert "Internal stack trace" not in comment


# ---------------------------------------------------------------------------
# 3. Target Resolution & Allowed Hosts Validation
# ---------------------------------------------------------------------------


def test_resolve_escalation_target_prefers_open_pr(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-pr",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/my-org/my-repo/pull/42",
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-1", title="Task", description="Desc", external_id="my-org/my-repo#10"),
    )

    pr_payload = {
        "number": 42,
        "url": "https://github.com/my-org/my-repo/pull/42",
        "state": "OPEN",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(pr_payload))])
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is not None
    repo_ref, number, target_type, target_url = target
    assert repo_ref.full_name == "my-org/my-repo"
    assert number == 42
    assert target_type is EscalationTargetType.PULL_REQUEST
    assert target_url == "https://github.com/my-org/my-repo/pull/42"


def test_resolve_escalation_target_falls_back_when_pr_closed(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-pr-closed",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/my-org/my-repo/pull/42",
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-1", title="Task", description="Desc", external_id="my-org/my-repo#10"),
    )

    # PR is CLOSED
    pr_payload = {
        "number": 42,
        "url": "https://github.com/my-org/my-repo/pull/42",
        "state": "CLOSED",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(pr_payload))])
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is not None
    repo_ref, number, target_type, target_url = target
    assert repo_ref.full_name == "my-org/my-repo"
    assert number == 10
    assert target_type is EscalationTargetType.ISSUE


def test_resolve_escalation_target_enforces_allowed_hosts(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    # PR on an unallowed host
    run = FactoryRun(
        id="run-untrusted",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://evil.example.com/org/repo/pull/1",
    )
    store.save_run(run)
    # Issue on an unallowed host as well
    store.save_artifact(
        run.id,
        WorkItem(
            id="task-1",
            title="Task",
            description="Desc",
            external_id="https://evil.example.com/org/repo/issues/1",
        ),
    )
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is None


# ---------------------------------------------------------------------------
# 4. Author Authorization & Bot / Self Rejection
# ---------------------------------------------------------------------------


def test_is_authorized_author_valid() -> None:
    now = utc_now()
    comment = GitHubComment(
        id=1,
        user_login="alice",
        user_id=101,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=r1 episode=e1",
    )
    assert is_authorized_author(
        comment,
        authorized_identities=["alice", "bob"],
        allowed_associations=["OWNER", "MEMBER", "COLLABORATOR"],
    )


def test_is_authorized_author_numeric_id_match() -> None:
    now = utc_now()
    comment = GitHubComment(
        id=1,
        user_login="some-renamed-user",
        user_id=9999,
        user_type="User",
        author_association="COLLABORATOR",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=r1 episode=e1",
    )
    assert is_authorized_author(
        comment,
        authorized_identities=["9999"],
        allowed_associations=["OWNER", "MEMBER", "COLLABORATOR"],
    )


def test_numeric_identity_does_not_authorize_same_numeric_login() -> None:
    now = utc_now()
    comment = GitHubComment(
        id=1,
        user_login="9999",
        user_id=1234,
        user_type="User",
        author_association="COLLABORATOR",
        created_at=now,
        updated_at=now,
    )

    assert not is_authorized_author(
        comment,
        authorized_identities=["9999"],
        allowed_associations=["OWNER", "MEMBER", "COLLABORATOR"],
    )


def test_is_authorized_author_rejects_unauthorized() -> None:
    now = utc_now()
    comment = GitHubComment(
        id=1,
        user_login="mallory",
        user_id=666,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        comment,
        authorized_identities=["alice"],
        allowed_associations=["OWNER", "MEMBER", "COLLABORATOR"],
    )


def test_is_authorized_author_rejects_disallowed_association() -> None:
    now = utc_now()
    comment = GitHubComment(
        id=1,
        user_login="alice",
        user_id=101,
        user_type="User",
        author_association="CONTRIBUTOR",  # Not OWNER/MEMBER/COLLABORATOR
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        comment,
        authorized_identities=["alice"],
        allowed_associations=["OWNER", "MEMBER", "COLLABORATOR"],
    )


def test_is_authorized_author_rejects_bot_and_self() -> None:
    now = utc_now()
    bot_comment = GitHubComment(
        id=1,
        user_login="alice",
        user_id=101,
        user_type="Bot",  # Marked as bot
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        bot_comment,
        authorized_identities=["alice"],
        allowed_associations=["MEMBER"],
    )

    app_bot_comment = GitHubComment(
        id=2,
        user_login="my-app[bot]",
        user_id=102,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        app_bot_comment,
        authorized_identities=["my-app[bot]"],
        allowed_associations=["MEMBER"],
    )

    self_comment = GitHubComment(
        id=3,
        user_login="factory-account",
        user_id=103,
        user_type="User",
        author_association="OWNER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        self_comment,
        authorized_identities=["factory-account"],
        allowed_associations=["OWNER"],
        factory_login="factory-account",
    )


# ---------------------------------------------------------------------------
# 5. Delivery, Persistence and Retry Safety
# ---------------------------------------------------------------------------


def test_deliver_escalation_notification_success(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-deliver",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason="risk R2 requires human approval",
        pull_request_url="https://github.com/owner/repo/pull/1",
    )
    store.save_run(run)
    store.save_artifact(
        run.id, WorkItem(id="task-1", title="Task", description="Desc", external_id="owner/repo#1")
    )

    # get_pull_request -> list comments (empty) -> create comment
    pr_resp = FakeCompletedProcess(
        0, json.dumps({"number": 1, "url": "https://github.com/owner/repo/pull/1", "state": "OPEN"})
    )
    list_resp = FakeCompletedProcess(0, json.dumps([]))
    created_resp = FakeCompletedProcess(
        0,
        json.dumps(_make_comment_payload(1234, "Notice", login="factory[bot]")),
    )
    runner = FakeRunner([pr_resp, list_resp, created_resp])
    client = GitHubClient(runner=runner)

    updated = deliver_escalation_notification(run, store, config, client, tmp_path)
    assert updated.escalation is not None
    assert updated.escalation.status is EscalationStatus.NOTIFIED
    assert updated.escalation.comment_id == 1234
    assert updated.escalation.delivery_attempts == 1
    assert updated.escalation.delivery_error is None

    # Verify persisted in store
    loaded = store.load_run(run.id)
    assert loaded.escalation.status is EscalationStatus.NOTIFIED


def test_deliver_escalation_notification_error_does_not_prevent_needs_human(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-err",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason="risk R2 requires human approval",
        pull_request_url="https://github.com/owner/repo/pull/1",
    )
    store.save_run(run)
    store.save_artifact(
        run.id, WorkItem(id="task-1", title="Task", description="Desc", external_id="owner/repo#1")
    )

    # get_pull_request -> list comments -> create comment FAILS
    pr_resp = FakeCompletedProcess(
        0, json.dumps({"number": 1, "url": "https://github.com/owner/repo/pull/1", "state": "OPEN"})
    )
    list_resp = FakeCompletedProcess(0, json.dumps([]))
    fail_resp = FakeCompletedProcess(1, "", "fatal: connection timeout")
    runner = FakeRunner([pr_resp, list_resp, fail_resp])
    client = GitHubClient(runner=runner)

    updated = deliver_escalation_notification(run, store, config, client, tmp_path)
    # Run MUST remain in NEEDS_HUMAN
    assert updated.state is WorkflowState.NEEDS_HUMAN
    assert updated.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert updated.escalation.delivery_attempts == 1
    assert "timeout" in (updated.escalation.delivery_error or "")


def test_deliver_escalation_notification_caps_attempts(tmp_path: Path) -> None:
    config = _make_config(tmp_path, max_notification_attempts=2)
    store = FileRunStore(tmp_path)
    escalation = EscalationRecord(
        episode_id="ep-1",
        episode_number=1,
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        delivery_attempts=2,  # Already at max attempts
    )
    run = FactoryRun(
        id="run-capped",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    updated = deliver_escalation_notification(run, store, config, client, tmp_path)
    assert updated.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert len(runner.calls) == 0  # No network calls made


# ---------------------------------------------------------------------------
# 6. Candidate Validation, Stale Episode, Replay, and Edit Rejection
# ---------------------------------------------------------------------------


def test_validate_reply_candidate_rejects_stale_episode(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-current",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
    )
    run = FactoryRun(
        id="run-target",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=555,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-target episode=ep-STALE",  # Mismatched episode
    )
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "episode" in reason


def test_validate_reply_candidate_rejects_replay(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    already_accepted = AcceptedReplyReceipt(
        comment_id=777,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=now - timedelta(minutes=10),
        command="@factory resume v1 run=r1 episode=ep-1",
        episode_id="ep-1",
        run_id="r1",
    )
    escalation = EscalationRecord(
        episode_id="ep-1",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        accepted_replies=[already_accepted],
        approval_context=_make_approval_context(run_id="r1", episode_id="ep-1"),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="r1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=777,  # Same comment id!
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=r1 episode=ep-1",
    )
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "already been accepted" in reason


def test_validate_reply_candidate_rejects_edited_comment(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    created_dt = datetime.fromisoformat("2026-09-13T10:00:00+00:00")
    escalation = EscalationRecord(
        episode_id="ep-1",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=created_dt - timedelta(hours=1),
        last_notified_at=created_dt - timedelta(hours=1),
        approval_context=_make_approval_context(run_id="run-1", episode_id="ep-1"),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=888,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=created_dt,
        updated_at=created_dt,
        body="@factory resume v1 run=run-1 episode=ep-1",
    )

    # Re-fetch returns a comment with updated_at != created_at (was edited!)
    re_fetched_payload = _make_comment_payload(
        888,
        "@factory resume v1 run=run-1 episode=ep-1",
        login="lead-dev",
        created_at="2026-09-13T10:00:00Z",
        updated_at="2026-09-13T10:05:00Z",  # Edited!
    )
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(re_fetched_payload))])
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment,
        run=run,
        config=config,
        client=client,
        repo_path=tmp_path,
        now=created_dt,
    )
    assert not is_valid
    assert "edited" in reason


def test_validate_reply_candidate_rejects_expired_window(tmp_path: Path) -> None:
    config = _make_config(tmp_path, reply_window_hours=24)
    now = utc_now()
    # Escalation created 25 hours ago
    escalation = EscalationRecord(
        episode_id="ep-1",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=25),
        last_notified_at=now - timedelta(hours=25),
    )
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=999,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-1 episode=ep-1",
    )
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path, now=now
    )
    assert not is_valid
    assert "expired" in reason


def test_validate_reply_candidate_rejects_non_resumable_halt(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    # Run halted due to SCOPE_REVIEW (not resumable via reply!)
    escalation = EscalationRecord(
        episode_id="ep-1",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.NOT_RESUMABLE,
        reason_code="SCOPE_REVIEW",
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
    )
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=123,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-1 episode=ep-1",
    )
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "not resumable" in reason


# ---------------------------------------------------------------------------
# 7. Reply Polling & Decision Receipt Persistence
# ---------------------------------------------------------------------------


def test_poll_escalation_reply_accepts_valid_comment(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-1234",
        episode_number=1,
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=10,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=_make_approval_context(run_id="run-poll", episode_id="ep-1234"),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-poll",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    comment_body = "@factory resume v1 run=run-poll episode=ep-1234"
    comment_data = _make_comment_payload(
        555,
        comment_body,
        login="lead-dev",
        created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    # First response: list comments. Second response: get comment 555 for re-fetch.
    list_resp = FakeCompletedProcess(0, json.dumps([comment_data]))
    get_resp = FakeCompletedProcess(0, json.dumps(comment_data))
    runner = FakeRunner([list_resp, get_resp])
    client = GitHubClient(runner=runner)

    receipt = poll_escalation_reply(run, store, config, client, tmp_path, now=now)
    assert receipt is not None
    assert receipt.comment_id == 555
    assert receipt.user_login == "lead-dev"
    assert receipt.run_id == "run-poll"
    assert receipt.episode_id == "ep-1234"

    # Verify decision receipt persisted on disk
    loaded = store.load_run(run.id)
    assert loaded.escalation.status is EscalationStatus.REOPENED
    assert loaded.escalation.reopen_count == 1
    assert len(loaded.escalation.accepted_replies) == 1
    persisted_receipt = loaded.escalation.accepted_replies[0]
    assert persisted_receipt.comment_id == 555
    # Verify raw reply body is NEVER stored on disk!
    assert not hasattr(persisted_receipt, "raw_body")
    assert persisted_receipt.command == "@factory resume v1 run=run-poll episode=ep-1234"


def test_poll_escalation_reply_persists_validated_plan_answers(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    now = utc_now()
    run = FactoryRun(
        id="run-plan-poll",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason=UNRESOLVED_DECISIONS_HALT_PREFIX,
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        ExecutionPlan(
            summary="Plan needs a decision.",
            expected_scope=ExpectedScope(
                modules=["src"],
                estimated_files_min=1,
                estimated_files_max=2,
            ),
            unresolved_decisions=["Select the local persistence format."],
        ),
    )
    context = build_plan_decision_context(run, store, episode_id="ep-plan-poll")
    assert context is not None
    escalation = EscalationRecord(
        episode_id="ep-plan-poll",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.PLAN_DECISION,
        target_repository="owner/repo",
        target_number=10,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        plan_decision_context=context,
        remote_resume_enabled=True,
    )
    run = run.model_copy(update={"escalation": escalation})
    store.save_run(run)
    body = (
        "@factory answer v1 run=run-plan-poll episode=ep-plan-poll\n"
        "1. Use JSON files in the configured data directory."
    )
    comment_data = _make_comment_payload(
        556,
        body,
        login="lead-dev",
        created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    client = GitHubClient(
        runner=FakeRunner(
            [
                FakeCompletedProcess(0, json.dumps([comment_data])),
                FakeCompletedProcess(0, json.dumps(comment_data)),
            ]
        )
    )

    receipt = poll_escalation_reply(run, store, config, client, tmp_path, now=now)

    assert receipt is not None
    assert receipt.plan_decision_context_fingerprint == context.context_fingerprint
    persisted = store.load_artifact(run.id, PlanDecisionAnswers)
    assert persisted.comment_id == receipt.comment_id
    assert persisted.context_fingerprint == context.context_fingerprint
    assert persisted.answers[0].answer == "Use JSON files in the configured data directory."


# ---------------------------------------------------------------------------
# 8. Controller Reopen API & Same-Run Resume
# ---------------------------------------------------------------------------


def test_workflow_controller_reopen_risk_approval(source_repo: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    # 1. Run initially with Risk.R2 -> enters NEEDS_HUMAN
    def triage_hook(req: AgentRequest) -> AgentResult:
        result = TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,  # R2 requires human approval
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=RiskRationale(
                intended_outcome="Update production database schema.",
                sensitive_boundary="Production database trust boundary.",
                necessity="Work item requires migrating production customer records.",
                credible_scenario="Data migration error could corrupt customer accounts.",
                known_mitigations=["Run migration inside atomic transaction."],
                residual_risk="Potential brief transaction lock delay on high-load tables.",
            ),
        )
        return AgentResult(role=AgentRole.TRIAGE, success=True, triage_result=result)

    runtime = FakeAgentRuntime(triage=triage_hook)
    controller = WorkflowController(config, store, runtime)

    work_item = WorkItem(id="task-r2", title="High risk task", description="Do something safely")
    run = controller.run(work_item, source_repo, run_id="run-r2-1")

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.escalation is not None
    assert run.escalation.resume_classification is ResumeClassification.RISK_APPROVAL
    assert run.escalation.reason_code == "RISK_APPROVAL"
    assert len(run.attempt_records) == 0  # No attempt spent on triage

    # Attempting normal transition from NEEDS_HUMAN must raise TransitionError!
    with pytest.raises(TransitionError, match="cannot transition from NEEDS_HUMAN"):
        controller.transition(run, WorkflowState.REFINING)

    # Calling reopen without an accepted receipt in REOPENED status must fail!
    with pytest.raises(ValueError, match="REOPENED"):
        controller.reopen(run.id, source_repo)

    # 2. Simulate human approval via accepted reply receipt and controller.reopen
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={run.id} episode={run.escalation.episode_id}",
        episode_id=run.escalation.episode_id,
        run_id=run.id,
        approval_context_fingerprint=(
            run.escalation.approval_context.context_fingerprint
            if run.escalation.approval_context
            else None
        ),
    )
    run = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={
                    "status": EscalationStatus.REOPENED,
                    "accepted_replies": [receipt],
                }
            )
        }
    )
    store.save_run(run)

    reopened = controller.reopen(run.id, source_repo)

    # Reopened run must be the SAME run!
    assert reopened.id == run.id
    assert reopened.work_item_id == run.work_item_id
    assert reopened.state is WorkflowState.PR_READY  # Ran successfully to completion!
    assert len(reopened.attempt_records) >= 1  # Spent normal implementation budget
    assert reopened.escalation.status is EscalationStatus.RESUMED


def test_workflow_controller_reopens_plan_decision_at_planning(
    source_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    planner_contexts: list[str | None] = []

    def planner(request: AgentRequest) -> AgentResult:
        planner_contexts.append(request.repair_context)
        unresolved = ExecutionPlan(
            summary="Plan requires one human decision",
            steps=[],
            expected_scope=ExpectedScope(
                modules=["FACTORY_NOTES.md"],
                estimated_files_min=1,
                estimated_files_max=1,
            ),
            unresolved_decisions=["Choose the local persistence format."],
        )
        if len(planner_contexts) < 3:
            return AgentResult(role=AgentRole.PLANNER, success=True, execution_plan=unresolved)
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            execution_plan=ExecutionPlan(
                summary="Implement the documented decision.",
                steps=[],
                expected_scope=ExpectedScope(
                    modules=["FACTORY_NOTES.md"],
                    estimated_files_min=1,
                    estimated_files_max=1,
                ),
            ),
        )

    controller = WorkflowController(config, store, FakeAgentRuntime(planner=planner))
    run = controller.run(
        WorkItem(id="task-plan-answer", title="Plan answer", description="Use a local store."),
        source_repo,
        run_id="run-plan-answer",
    )

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.escalation is not None
    assert run.escalation.resume_classification is ResumeClassification.PLAN_DECISION
    context = run.escalation.plan_decision_context
    assert context is not None
    parsed = parse_plan_decision_answers(
        f"@factory answer v1 run={run.id} episode={run.escalation.episode_id}\n"
        "1. Use JSON files in the configured data directory.",
        decision_count=1,
    )
    assert parsed is not None
    _, _, answers = parsed
    store.save_artifact(
        run.id,
        PlanDecisionAnswers(
            run_id=run.id,
            episode_id=run.escalation.episode_id,
            plan_fingerprint=context.plan_fingerprint,
            context_fingerprint=context.context_fingerprint,
            comment_id=101,
            user_login="lead-dev",
            user_id=1001,
            author_association="MEMBER",
            answers=answers,
        ),
    )
    receipt = AcceptedReplyReceipt(
        comment_id=101,
        user_login="lead-dev",
        user_id=1001,
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory answer v1 run={run.id} episode={run.escalation.episode_id}",
        episode_id=run.escalation.episode_id,
        run_id=run.id,
        plan_decision_context_fingerprint=context.context_fingerprint,
    )
    run = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={
                    "status": EscalationStatus.REOPENED,
                    "reopen_count": 1,
                    "accepted_replies": [receipt],
                }
            )
        }
    )
    store.save_run(run)

    reopened = controller.reopen(run.id, source_repo)

    assert reopened.state is WorkflowState.PR_READY
    assert reopened.escalation is not None
    assert reopened.escalation.status is EscalationStatus.RESUMED
    assert reopened.attempt_records[0].triggered_by.value == "INITIAL"
    assert len(planner_contexts) == 3
    assert planner_contexts[-1] is not None
    assert "Use JSON files in the configured data directory." in planner_contexts[-1]


def test_workflow_controller_reopen_rejects_non_resumable(
    source_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    # Create run halted in NEEDS_HUMAN with SCOPE_REVIEW
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command="@factory resume v1 run=run-scope episode=ep-scope",
        episode_id="ep-scope",
        run_id="run-scope",
    )
    escalation = EscalationRecord(
        episode_id="ep-scope",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.NOT_RESUMABLE,
        reason_code="SCOPE_REVIEW",
        accepted_replies=[receipt],
    )
    run = FactoryRun(
        id="run-scope",
        work_item_id="task-scope",
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason="Scope exceeded approved limits",
        escalation=escalation,
    )
    store.save_run(run)

    reopened = controller.reopen(run.id, source_repo)
    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reply_cursor == "closed"


def test_workflow_controller_reopen_rejects_exceeded_max_reopens(
    source_repo: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_reopens=2)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command="@factory resume v1 run=run-reopen-cap episode=ep-reopen-cap",
        episode_id="ep-reopen-cap",
        run_id="run-reopen-cap",
    )
    escalation = EscalationRecord(
        episode_id="ep-reopen-cap",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        reopen_count=3,  # Exceeded max_reopens=2
        accepted_replies=[receipt],
    )
    run = FactoryRun(
        id="run-reopen-cap",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    reopened = controller.reopen(run.id, source_repo)
    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
    assert reopened.escalation.reply_cursor == "closed"


# ---------------------------------------------------------------------------
# 9. No Reply Text in Prompts
# ---------------------------------------------------------------------------


def test_no_reply_text_in_agent_prompts(source_repo: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    intercepted_requests: list[AgentRequest] = []

    class InterceptingRuntime:
        def __init__(self, delegate: FakeAgentRuntime) -> None:
            self.delegate = delegate

        def run(self, request: AgentRequest) -> AgentResult:
            intercepted_requests.append(request)
            return self.delegate.run(request)

    def triage_hook(req: AgentRequest) -> AgentResult:
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=Complexity.L1,
                risk=Risk.R2,
                requirements_quality="clear",
                needs_research=False,
                confidence=0.9,
                risk_rationale=RiskRationale(
                    intended_outcome="Update production database schema.",
                    sensitive_boundary="Production database trust boundary.",
                    necessity="Work item requires migrating production customer records.",
                    credible_scenario="Data migration error could corrupt customer accounts.",
                    known_mitigations=["Run migration inside atomic transaction."],
                    residual_risk="Potential brief transaction lock delay on high-load tables.",
                ),
            ),
        )

    base_runtime = FakeAgentRuntime(triage=triage_hook)
    runtime = InterceptingRuntime(base_runtime)
    controller = WorkflowController(config, store, runtime)

    work_item = WorkItem(id="task-r2-prompt", title="High risk task", description="Do something")
    run = controller.run(work_item, source_repo, run_id="run-prompt-test")
    assert run.state is WorkflowState.NEEDS_HUMAN

    # Simulate receipt
    arbitrary_comment_text = f"@factory resume v1 run={run.id} episode={run.escalation.episode_id}"
    receipt = AcceptedReplyReceipt(
        comment_id=999,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=arbitrary_comment_text,
        episode_id=run.escalation.episode_id,
        run_id=run.id,
        approval_context_fingerprint=(
            run.escalation.approval_context.context_fingerprint
            if run.escalation.approval_context
            else None
        ),
    )
    run = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={
                    "status": EscalationStatus.REOPENED,
                    "accepted_replies": [receipt],
                }
            )
        }
    )
    store.save_run(run)

    # Reopen and execute rest of workflow
    reopened = controller.reopen(run.id, source_repo)
    assert reopened.state is WorkflowState.PR_READY

    # Verify that the command / comment text never entered ANY agent request or field
    for req in intercepted_requests:
        req_str = str(req.model_dump())
        assert arbitrary_comment_text not in req_str
        assert run.escalation.episode_id not in req_str


# ---------------------------------------------------------------------------
# 10. Service Integration: Concurrency, Daily Quota, AlreadyRunFilter
# ---------------------------------------------------------------------------


def test_service_reconciles_and_reopens_within_capacity(source_repo: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_concurrent_tasks=1, max_runs_per_day=5)
    store = FileRunStore(data_dir)

    # Prepare halted run in store
    now = utc_now()
    tracker_item = TrackerItem(
        opaque_id="owner/repo#1",
        identifier="owner/repo#1",
        title="Task",
        description="Desc",
        state="OPEN",
        labels=("agent-ready",),
        created_at=now,
        blockers=(),
        dispatchable=True,
        repository_path=str(source_repo),
    )
    from software_agent_factory.scheduler import deterministic_work_item_id

    work_item_id = deterministic_work_item_id(tracker_item)
    from software_agent_factory.workspace import GitWorktreeWorkspace

    ws = GitWorktreeWorkspace(data_dir, source_repo, work_item_id, branch_prefix="factory/")
    ws.acquire_lock()
    ws.prepare()
    ws.release_lock()

    escalation = EscalationRecord(
        episode_id="ep-service-1",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=_make_approval_context(
            run_id="run-svc-1",
            episode_id="ep-service-1",
            work_item_id=work_item_id,
        ),
        remote_resume_enabled=True,
    )

    run = FactoryRun(
        id="run-svc-1",
        work_item_id=work_item_id,
        state=WorkflowState.NEEDS_HUMAN,
        workspace_path=str(ws.path),
        branch_name=ws.branch_name,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id=work_item_id, title="Task", description="Desc", external_id="owner/repo#1"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="good",
            needs_research=False,
            confidence=0.9,
            risk_rationale=escalation.approval_context.risk_rationale,
        ),
    )

    from software_agent_factory.repository_profile import generic_repository_profile

    store.save_artifact(run.id, generic_repository_profile())

    comment_time = escalation.created_at + timedelta(minutes=10)
    comment_data = _make_comment_payload(
        901,
        "@factory resume v1 run=run-svc-1 episode=ep-service-1",
        login="lead-dev",
        created_at=comment_time,
        updated_at=comment_time,
    )
    list_resp = FakeCompletedProcess(0, json.dumps([comment_data]))
    get_resp = FakeCompletedProcess(0, json.dumps(comment_data))
    runner = FakeRunner([list_resp, get_resp])
    client = GitHubClient(runner=runner)

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=client,
    )

    # Reconcile escalation
    service.reconcile_escalation()
    service.drain(timeout_seconds=5.0)

    future = service._handles["run-svc-1"].future
    if future and future.exception():
        raise future.exception()

    # Verify run was reopened and completed
    finished_run = store.load_run("run-svc-1")
    assert finished_run.state is WorkflowState.PR_READY
    assert finished_run.escalation.status is EscalationStatus.RESUMED

    # Verify AlreadyRunFilter continues blocking the issue from fresh dispatch
    tracker_item = TrackerItem(
        opaque_id="owner/repo#1",
        identifier="owner/repo#1",
        title="Task",
        description="Desc",
        state="OPEN",
        labels=("agent-ready",),
        created_at=utc_now(),
        blockers=(),
        dispatchable=True,
        repository_path=str(source_repo),
    )
    # Ensure AlreadyRunFilter hides it:
    filt = AlreadyRunFilter(DummyProvider(), store)
    assert len(filt._filter([tracker_item])) == 0


# ---------------------------------------------------------------------------
# 11. Findings Verification Tests (Findings 1 - 9)
# ---------------------------------------------------------------------------


def test_reopen_requires_durable_reopened_status_and_valid_receipt(
    source_repo: Path, tmp_path: Path
) -> None:
    """Finding 1: Reopen requires REOPENED status and matching accepted receipt."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    # Case A: status is NOTIFIED (not REOPENED)
    escalation_notified = EscalationRecord(
        episode_id="ep-1",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
    )
    run_a = FactoryRun(
        id="run-a",
        work_item_id="task-a",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation_notified,
    )
    store.save_run(run_a)
    with pytest.raises(ValueError, match="REOPENED"):
        controller.reopen(run_a.id, source_repo)

    # Case B: status is REOPENED, but accepted_replies is empty
    escalation_no_receipt = EscalationRecord(
        episode_id="ep-2",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        accepted_replies=[],
    )
    run_b = FactoryRun(
        id="run-b",
        work_item_id="task-b",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation_no_receipt,
    )
    store.save_run(run_b)
    reopened_b = controller.reopen(run_b.id, source_repo)
    assert reopened_b.state is WorkflowState.NEEDS_HUMAN
    assert reopened_b.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened_b.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE

    # Case C: receipt belongs to a different episode
    stale_receipt = AcceptedReplyReceipt(
        comment_id=10,
        user_login="lead-dev",
        created_at=utc_now(),
        command="@factory resume v1 run=run-c episode=ep-old",
        episode_id="ep-old",
        run_id="run-c",
    )
    escalation_mismatched = EscalationRecord(
        episode_id="ep-current",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        accepted_replies=[stale_receipt],
    )
    run_c = FactoryRun(
        id="run-c",
        work_item_id="task-c",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation_mismatched,
    )
    store.save_run(run_c)
    reopened_c = controller.reopen(run_c.id, source_repo)
    assert reopened_c.state is WorkflowState.NEEDS_HUMAN
    assert reopened_c.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened_c.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE


def test_transition_has_no_reopening_bypass(tmp_path: Path) -> None:
    """Finding 1: transition() forbids leaving NEEDS_HUMAN with no reopening bypass."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    run = FactoryRun(
        id="run-halted",
        work_item_id="task-h",
        state=WorkflowState.NEEDS_HUMAN,
    )
    store.save_run(run)

    with pytest.raises(TransitionError, match="cannot transition from NEEDS_HUMAN"):
        controller.transition(run, WorkflowState.REFINING)

    with pytest.raises(TypeError):
        controller.transition(run, WorkflowState.REFINING, reopening=True)  # type: ignore[call-arg]


def test_service_reconciliation_recovers_stranded_reopened_run(
    source_repo: Path, tmp_path: Path
) -> None:
    """Finding 2: Crash after reply persistence strands NEEDS_HUMAN + REOPENED.
    Reconciliation must treat as durable resume-pending work, idempotently dispatch it,
    and mark fully resumed (RESUMED) only after controller leaves NEEDS_HUMAN."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_concurrent_tasks=1)
    store = FileRunStore(data_dir)

    from software_agent_factory.scheduler import deterministic_work_item_id
    from software_agent_factory.workspace import GitWorktreeWorkspace

    now = utc_now()
    tracker_item = TrackerItem(
        opaque_id="owner/repo#5",
        identifier="owner/repo#5",
        title="Task",
        description="Desc",
        state="OPEN",
        labels=("agent-ready",),
        created_at=now,
        blockers=(),
        dispatchable=True,
        repository_path=str(source_repo),
    )
    work_item_id = deterministic_work_item_id(tracker_item)
    ws = GitWorktreeWorkspace(data_dir, source_repo, work_item_id, branch_prefix="factory/")
    ws.acquire_lock()
    ws.prepare()
    ws.release_lock()

    app_ctx = _make_approval_context(
        run_id="run-stranded",
        episode_id="ep-stranded",
        work_item_id=work_item_id,
    )
    receipt = AcceptedReplyReceipt(
        comment_id=777,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=now,
        accepted_at=now,
        command="@factory resume v1 run=run-stranded episode=ep-stranded",
        episode_id="ep-stranded",
        run_id="run-stranded",
        approval_context_fingerprint=app_ctx.context_fingerprint,
    )
    escalation = EscalationRecord(
        episode_id="ep-stranded",
        status=EscalationStatus.REOPENED,  # Stranded before executor dispatch!
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=5,
        accepted_replies=[receipt],
        reopen_count=1,
        approval_context=app_ctx,
        remote_resume_enabled=True,
    )

    run = FactoryRun(
        id="run-stranded",
        work_item_id=work_item_id,
        state=WorkflowState.NEEDS_HUMAN,
        workspace_path=str(ws.path),
        branch_name=ws.branch_name,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(
            id=work_item_id,
            title="Task",
            description="Desc",
            external_id="owner/repo#5",
        ),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="good",
            needs_research=False,
            confidence=0.9,
            risk_rationale=app_ctx.risk_rationale,
        ),
    )
    from software_agent_factory.repository_profile import generic_repository_profile

    store.save_artifact(run.id, generic_repository_profile())

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    client = GitHubClient(runner=FakeRunner())
    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=client,
    )

    # First reconciliation: picks up stranded REOPENED run and executes it
    service.reconcile_escalation()
    service.drain(timeout_seconds=5.0)

    finished = store.load_run("run-stranded")
    assert finished.state is WorkflowState.PR_READY
    assert finished.escalation.status is EscalationStatus.RESUMED

    # Second reconciliation: idempotent, does not dispatch again
    prev_handles_count = len(service._handles)
    service.reconcile_escalation()
    assert len(service._handles) == prev_handles_count


def test_scheduler_daily_quota_counts_reopens_using_dispatch_time(tmp_path: Path) -> None:
    """Finding 3: Reopens count original run plus every reopen dispatched in current UTC day,
    using accepted_at/dispatched_at timestamp, not GitHub comment created_at."""
    from software_agent_factory.scheduler import Scheduler

    store = FileRunStore(tmp_path)
    now = utc_now()
    yesterday = now - timedelta(days=1)

    # Run 1: created yesterday, reopened once yesterday, reopened once today
    receipt_yesterday = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        created_at=yesterday - timedelta(hours=2),  # comment created yesterday
        accepted_at=yesterday,  # dispatched yesterday
        command="resume",
        episode_id="ep-1",
        run_id="run-1",
    )
    receipt_today = AcceptedReplyReceipt(
        comment_id=2,
        user_login="lead-dev",
        created_at=yesterday,  # comment created yesterday!
        accepted_at=now,  # accepted/dispatched TODAY!
        command="resume",
        episode_id="ep-1",
        run_id="run-1",
    )
    run_1 = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        created_at=yesterday,
        escalation=EscalationRecord(
            episode_id="ep-1",
            status=EscalationStatus.RESUMED,
            accepted_replies=[receipt_yesterday, receipt_today],
        ),
    )

    # Run 2: created today, reopened once today
    receipt_today_2 = AcceptedReplyReceipt(
        comment_id=3,
        user_login="lead-dev",
        created_at=now,
        accepted_at=now,
        command="resume",
        episode_id="ep-2",
        run_id="run-2",
    )
    run_2 = FactoryRun(
        id="run-2",
        work_item_id="task-2",
        state=WorkflowState.NEEDS_HUMAN,
        created_at=now,
        escalation=EscalationRecord(
            episode_id="ep-2",
            status=EscalationStatus.RESUMED,
            accepted_replies=[receipt_today_2],
        ),
    )

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    scheduler = Scheduler(
        provider=DummyProvider(),
        dispatch=lambda item: None,  # type: ignore[return-value]
        store=store,
        max_runs_per_day=10,
        clock=lambda: now,
    )

    # Dispatches counted for today:
    # run_1: created yesterday (0), receipt_yesterday (0), receipt_today (1) -> 1
    # run_2: created today (1), receipt_today_2 (1) -> 2
    # Total today = 3 dispatches. Remaining quota = 10 - 3 = 7.
    remaining = scheduler._remaining_daily_quota([run_1, run_2])
    assert remaining == 7


def test_non_resumable_notified_runs_do_not_consume_poll_slots(
    source_repo: Path, tmp_path: Path
) -> None:
    """Finding 4: Non-resumable NOTIFIED runs do not consume reply-poll slots,
    and their reply_cursor is closed deterministically."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_reply_polls_per_tick=1)
    store = FileRunStore(data_dir)

    # Non-resumable run 1 in NOTIFIED
    run_non_resumable = FactoryRun(
        id="run-nr",
        work_item_id="task-nr",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-nr",
            status=EscalationStatus.NOTIFIED,
            resume_classification=ResumeClassification.NOT_RESUMABLE,
            reason_code="SCOPE_REVIEW",
            target_repository="owner/repo",
            target_number=1,
        ),
    )
    store.save_run(run_non_resumable)

    # Resumable run 2 in NOTIFIED
    run_resumable = FactoryRun(
        id="run-res",
        work_item_id="task-res",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-res",
            status=EscalationStatus.NOTIFIED,
            resume_classification=ResumeClassification.RISK_APPROVAL,
            reason_code="RISK_APPROVAL",
            target_repository="owner/repo",
            target_number=2,
        ),
    )
    store.save_run(run_resumable)

    client = GitHubClient(runner=FakeRunner())

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=client,
    )

    service.reconcile_escalation()

    # Verify non-resumable run has reply_cursor marked 'closed'
    updated_nr = store.load_run("run-nr")
    assert updated_nr.escalation.reply_cursor == "closed"


def test_poll_escalation_reply_bounded_pagination_across_ticks(tmp_path: Path) -> None:
    """Finding 5: Bounded pagination across ticks using persisted reply_cursor."""
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    now = utc_now()

    escalation = EscalationRecord(
        episode_id="ep-pages",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=10,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=_make_approval_context(
            run_id="run-pages",
            episode_id="ep-pages",
            work_item_id="task-p",
        ),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-pages",
        work_item_id="task-p",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    base_time = escalation.created_at + timedelta(minutes=1)
    # 100 irrelevant comments on page 1, 100 irrelevant comments on page 2
    page1_comments = [
        _make_comment_payload(
            i,
            f"comment {i}",
            login="other",
            created_at=base_time + timedelta(seconds=i),
            updated_at=base_time + timedelta(seconds=i),
        )
        for i in range(1, 101)
    ]
    page2_comments = [
        _make_comment_payload(
            i,
            f"comment {i}",
            login="other",
            created_at=base_time + timedelta(seconds=i),
            updated_at=base_time + timedelta(seconds=i),
        )
        for i in range(101, 201)
    ]
    # Page 3 has the valid reply
    valid_payload = _make_comment_payload(
        205,
        "@factory resume v1 run=run-pages episode=ep-pages",
        login="lead-dev",
        created_at=base_time + timedelta(seconds=205),
        updated_at=base_time + timedelta(seconds=205),
    )
    page3_comments = [valid_payload]

    # Tick 1: returns page 1 and page 2 (max_pages_per_poll=2)
    resp_p1 = FakeCompletedProcess(0, json.dumps(page1_comments))
    resp_p2 = FakeCompletedProcess(0, json.dumps(page2_comments))
    runner_tick1 = FakeRunner([resp_p1, resp_p2])
    client_tick1 = GitHubClient(runner=runner_tick1)

    receipt_1 = poll_escalation_reply(run, store, config, client_tick1, tmp_path, now=now)
    assert receipt_1 is None

    # Verify cursor saved on disk pointing to next page
    run_after_tick1 = store.load_run(run.id)
    assert run_after_tick1.escalation.reply_cursor is not None
    cursor_data = json.loads(run_after_tick1.escalation.reply_cursor)
    assert cursor_data.get("page") == 3

    # Tick 2: resumes from cursor (page 3), finds valid reply and re-fetches comment 205
    resp_p3 = FakeCompletedProcess(0, json.dumps(page3_comments))
    resp_refetch = FakeCompletedProcess(0, json.dumps(valid_payload))
    runner_tick2 = FakeRunner([resp_p3, resp_refetch])
    client_tick2 = GitHubClient(runner=runner_tick2)

    receipt_2 = poll_escalation_reply(
        run_after_tick1, store, config, client_tick2, tmp_path, now=now
    )
    assert receipt_2 is not None
    assert receipt_2.comment_id == 205
    assert receipt_2.user_login == "lead-dev"


def test_deliver_escalation_notification_reconciles_lost_post_response(tmp_path: Path) -> None:
    """Finding 5: Reconcile lost POST response without duplicate notices."""
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    escalation = EscalationRecord(
        episode_id="ep-lost-post",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.NOT_RESUMABLE,
        reason_code="MANUAL_INSPECTION",
        delivery_attempts=1,  # previous attempt POSTed comment but connection failed before saving
    )
    run = FactoryRun(
        id="run-lost",
        work_item_id="task-l",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/owner/repo/pull/1",
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(
        run.id, WorkItem(id="task-l", title="Task", description="Desc", external_id="owner/repo#1")
    )

    expected_notice = build_escalation_comment(
        run_id="run-lost",
        episode_id="ep-lost-post",
        classification=ResumeClassification.NOT_RESUMABLE,
        reason_code="MANUAL_INSPECTION",
        summary="The controller stopped at a manual decision boundary.",
        next_action=(
            "Inspect the typed run artifacts and decide whether to retry or replace the run."
        ),
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
    )
    existing_comment = _make_comment_payload(
        888,
        str(expected_notice),
        login="factory-bot",
        user_id=99999,
    )

    pr_resp = FakeCompletedProcess(
        0, json.dumps({"number": 1, "url": "https://github.com/owner/repo/pull/1", "state": "OPEN"})
    )
    list_resp = FakeCompletedProcess(0, json.dumps([existing_comment]))
    # No create comment response provided: if create is called, it would fail
    runner = FakeRunner([pr_resp, list_resp])
    client = GitHubClient(runner=runner)

    updated = deliver_escalation_notification(run, store, config, client, tmp_path)
    assert updated.escalation.status is EscalationStatus.NOTIFIED
    assert updated.escalation.comment_id == 888


def test_factory_self_account_rejected_even_if_configured(tmp_path: Path) -> None:
    """Finding 6: Authenticated identity resolved and cached via get_authenticated_user.
    Self comments rejected even if login or user_id is in authorized_identities."""
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    # 1. Verify get_authenticated_user resolution and caching
    user_identity = client.get_authenticated_user(tmp_path)
    assert user_identity.id == 99999
    assert user_identity.login == "factory-bot"

    # Second call should use cache (no extra command)
    calls_before = len(runner.calls)
    cached_ident = client.get_authenticated_user(tmp_path)
    assert cached_ident == user_identity
    assert len(runner.calls) == calls_before

    # 2. Verify self comment rejection even if configured
    now = utc_now()
    self_comment_by_login = GitHubComment(
        id=1,
        user_login="factory-bot",
        user_id=1001,
        user_type="User",
        author_association="OWNER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        self_comment_by_login,
        authorized_identities=["factory-bot"],  # Explicitly configured!
        allowed_associations=["OWNER"],
        factory_login="factory-bot",
        factory_id=99999,
    )

    self_comment_by_id = GitHubComment(
        id=2,
        user_login="renamed-factory",
        user_id=99999,  # Matches factory_id!
        user_type="User",
        author_association="OWNER",
        created_at=now,
        updated_at=now,
    )
    assert not is_authorized_author(
        self_comment_by_id,
        authorized_identities=["99999", "renamed-factory"],
        allowed_associations=["OWNER"],
        factory_login="factory-bot",
        factory_id=99999,
    )


def test_github_host_identity_persisted_and_passed(tmp_path: Path) -> None:
    """Finding 7: Persist target_host, validate it, pass it to PR lookup, comment list/create,
    and exact comment re-fetch. Support Enterprise without silent github.com fallback."""
    enterprise_host = "ghe.internal.corp"
    config = _make_config(tmp_path)
    config = config.model_copy(
        update={
            "escalation": config.escalation.model_copy(update={"allowed_hosts": [enterprise_host]}),
            "pull_request": config.pull_request.model_copy(
                update={"allowed_hosts": [enterprise_host]}
            ),
        }
    )
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-ghe",
        work_item_id="task-ghe",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url=f"https://{enterprise_host}/owner/repo/pull/12",
        delivery_host=enterprise_host,
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(
            id="task-ghe",
            title="Task",
            description="Desc",
            external_id=f"https://{enterprise_host}/owner/repo/issues/12",
        ),
    )

    pr_resp = FakeCompletedProcess(
        0,
        json.dumps(
            {"number": 12, "url": f"https://{enterprise_host}/owner/repo/pull/12", "state": "OPEN"}
        ),
    )
    list_resp = FakeCompletedProcess(0, json.dumps([]))
    created_resp = FakeCompletedProcess(
        0, json.dumps(_make_comment_payload(12, "Notice", login="factory[bot]"))
    )
    runner = FakeRunner([pr_resp, list_resp, created_resp])
    client = GitHubClient(runner=runner, host=enterprise_host)

    updated = deliver_escalation_notification(run, store, config, client, tmp_path)
    assert updated.escalation.target_host == enterprise_host

    # Check calls to ensure enterprise_host was passed, not github.com
    for call_args, _, _ in runner.calls:
        if "--hostname" in call_args:
            idx = call_args.index("--hostname")
            assert call_args[idx + 1] == enterprise_host


def test_missing_or_invalid_targets_consume_attempts_and_stop(tmp_path: Path) -> None:
    """Finding 8: Count each delivery cycle, including deterministic target-resolution failure,
    and stop at configured maximum."""
    config = _make_config(tmp_path, max_notification_attempts=3)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-notarget",
        work_item_id="task-nt",
        state=WorkflowState.NEEDS_HUMAN,
        # No pull_request_url and no work item artifact
    )
    store.save_run(run)
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    # Attempt 1
    run1 = deliver_escalation_notification(run, store, config, client, tmp_path)
    assert run1.escalation.delivery_attempts == 1
    assert run1.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert "no valid escalation target" in (run1.escalation.delivery_error or "")

    # Attempt 2
    run2 = deliver_escalation_notification(run1, store, config, client, tmp_path)
    assert run2.escalation.delivery_attempts == 2

    # Attempt 3 (reaches max_notification_attempts=3)
    run3 = deliver_escalation_notification(run2, store, config, client, tmp_path)
    assert run3.escalation.delivery_attempts == 3

    # Attempt 4 (already at max: should not increment or execute)
    run4 = deliver_escalation_notification(run3, store, config, client, tmp_path)
    assert run4.escalation.delivery_attempts == 3
    assert run4.escalation.status is EscalationStatus.NOTIFICATION_FAILED


def test_unsupported_reopen_grant_fields_removed(tmp_path: Path) -> None:
    """Finding 9: attempts_per_reopen and attempt_grants are removed from config and models."""
    config = load_config()
    assert not hasattr(config.escalation, "attempts_per_reopen")

    record = EscalationRecord(episode_id="ep-1")
    assert not hasattr(record, "attempt_grants")


# ---------------------------------------------------------------------------
# 12. Final Review Findings Regression Tests (Review Findings 1 - 6)
# ---------------------------------------------------------------------------


def test_poll_escalation_reply_fails_closed_when_authenticated_user_fails(tmp_path: Path) -> None:
    """Review Finding 1: Authenticated-self lookup fails closed.
    If authenticated-user identity cannot be resolved, skip reply acceptance for that attempt.
    """
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-self-fail",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=10,
        created_at=now - timedelta(hours=1),
    )
    run = FactoryRun(
        id="run-self-fail",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    comment_time = escalation.created_at + timedelta(minutes=10)
    comment_data = _make_comment_payload(
        555,
        "@factory resume v1 run=run-self-fail episode=ep-self-fail",
        login="lead-dev",
        created_at=comment_time,
        updated_at=comment_time,
    )
    list_resp = FakeCompletedProcess(0, json.dumps([comment_data]))

    class FailingUserRunner:
        def __call__(self, args, cwd=None, env=None):
            if "user" in args and not any("issues" in a or "comments" in a for a in args):
                return FakeCompletedProcess(1, "", "fatal: bad credentials or connection failure")
            return list_resp

    client = GitHubClient(runner=FailingUserRunner())
    receipt = poll_escalation_reply(run, store, config, client, tmp_path, now=now)
    assert receipt is None
    loaded = store.load_run(run.id)
    assert loaded.escalation.status is EscalationStatus.NOTIFIED
    assert len(loaded.escalation.accepted_replies) == 0


def test_reopen_failure_persists_non_resumable_episode_and_breaks_loop(
    source_repo: Path, tmp_path: Path
) -> None:
    """Review Finding 2: A failed reopen must not leave NEEDS_HUMAN + REOPENED dispatchable.
    If workspace check fails, persist a new non-resumable escalation episode,
    closing the resume-pending record and preventing tight redispatch loops.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_concurrent_tasks=1)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    now = utc_now()
    receipt = AcceptedReplyReceipt(
        comment_id=101,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=now,
        accepted_at=now,
        command="@factory resume v1 run=run-failed-reopen episode=ep-1",
        episode_id="ep-1",
        run_id="run-failed-reopen",
    )
    escalation = EscalationRecord(
        episode_id="ep-1",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        accepted_replies=[receipt],
        reopen_count=1,
    )
    run = FactoryRun(
        id="run-failed-reopen",
        work_item_id="task-fr",
        state=WorkflowState.NEEDS_HUMAN,
        workspace_path=str(data_dir / "workspaces" / "non-existent-ws"),
        branch_name="factory/task-fr",
        escalation=escalation,
    )
    store.save_run(run)

    reopened = controller.reopen(run.id, source_repo)

    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.failure_reason == "workspace identity changed or workspace is missing"
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "RECOVERY_INTERVENTION"
    assert reopened.escalation.reply_cursor == "closed"
    assert reopened.escalation.episode_number == 2
    assert reopened.escalation.episode_id != "ep-1"
    assert reopened.lease is None

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=GitHubClient(runner=FakeRunner()),
    )
    service.reconcile_escalation()
    assert len(service._handles) == 0


def test_crash_recovered_reopened_dispatch_does_not_require_extra_quota_slot(
    source_repo: Path, tmp_path: Path
) -> None:
    """Review Finding 3: Crash-recovered REOPENED receipt is already a durable quota reservation.
    Dispatching that pending reopen must not require another quota slot.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_concurrent_tasks=1, max_runs_per_day=1)
    store = FileRunStore(data_dir)

    now = utc_now()
    receipt = AcceptedReplyReceipt(
        comment_id=505,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=now,
        accepted_at=now,
        command="@factory resume v1 run=run-crash-rec episode=ep-crash-rec",
        episode_id="ep-crash-rec",
        run_id="run-crash-rec",
    )
    from software_agent_factory.scheduler import deterministic_work_item_id
    from software_agent_factory.workspace import GitWorktreeWorkspace

    tracker_item = TrackerItem(
        opaque_id="owner/repo#1",
        identifier="owner/repo#1",
        title="Task",
        description="Desc",
        state="OPEN",
        labels=("agent-ready",),
        created_at=now,
        blockers=(),
        dispatchable=True,
        repository_path=str(source_repo),
    )
    work_item_id = deterministic_work_item_id(tracker_item)
    ws = GitWorktreeWorkspace(data_dir, source_repo, work_item_id, branch_prefix="factory/")
    ws.acquire_lock()
    ws.prepare()
    ws.release_lock()

    app_ctx = _make_approval_context(
        run_id="run-crash-rec",
        episode_id="ep-crash-rec",
        work_item_id=work_item_id,
    )
    receipt = AcceptedReplyReceipt(
        comment_id=505,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=now,
        accepted_at=now,
        command="@factory resume v1 run=run-crash-rec episode=ep-crash-rec",
        episode_id="ep-crash-rec",
        run_id="run-crash-rec",
        approval_context_fingerprint=app_ctx.context_fingerprint,
    )
    escalation = EscalationRecord(
        episode_id="ep-crash-rec",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        accepted_replies=[receipt],
        reopen_count=1,
        approval_context=app_ctx,
        remote_resume_enabled=True,
    )

    run = FactoryRun(
        id="run-crash-rec",
        work_item_id=work_item_id,
        state=WorkflowState.NEEDS_HUMAN,
        created_at=now,
        workspace_path=str(ws.path),
        branch_name=ws.branch_name,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id=work_item_id, title="Task", description="Desc", external_id="owner/repo#1"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="good",
            needs_research=False,
            confidence=0.9,
            risk_rationale=app_ctx.risk_rationale,
        ),
    )
    from software_agent_factory.repository_profile import generic_repository_profile

    store.save_artifact(run.id, generic_repository_profile())

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=GitHubClient(runner=FakeRunner()),
    )

    assert service.scheduler._remaining_daily_quota(store.list_runs()) == 0

    service.reconcile_escalation()
    service.drain(timeout_seconds=5.0)

    finished = store.load_run("run-crash-rec")
    assert finished.state is WorkflowState.PR_READY
    assert finished.escalation.status is EscalationStatus.RESUMED


def test_reply_polling_starvation_prevention_with_rotating_cursor(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Finding 4: Prevent starvation via rotating cursor across ticks and restarts."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir, max_reply_polls_per_tick=2)
    store = FileRunStore(data_dir)
    now = utc_now()

    polled_runs: list[str] = []

    def mock_poll(run, *args, **kwargs):
        polled_runs.append(run.id)
        return None

    import software_agent_factory.escalation as esc_mod

    monkeypatch.setattr(esc_mod, "poll_escalation_reply", mock_poll)

    for i in range(1, 5):
        run = FactoryRun(
            id=f"run-{i}",
            work_item_id=f"task-{i}",
            state=WorkflowState.NEEDS_HUMAN,
            escalation=EscalationRecord(
                episode_id=f"ep-{i}",
                status=EscalationStatus.NOTIFIED,
                resume_classification=ResumeClassification.RISK_APPROVAL,
                target_repository="owner/repo",
                target_number=i,
                created_at=now + timedelta(seconds=i),
            ),
        )
        store.save_run(run)

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=GitHubClient(runner=FakeRunner()),
    )

    # Tick 1: polls runs 1 and 2
    service.reconcile_escalation()
    assert polled_runs == ["run-1", "run-2"]

    # Tick 2: polls runs 3 and 4
    polled_runs.clear()
    service.reconcile_escalation()
    assert polled_runs == ["run-3", "run-4"]

    # Tick 3: wraps around and polls runs 1 and 2
    polled_runs.clear()
    service.reconcile_escalation()
    assert polled_runs == ["run-1", "run-2"]

    # Durability across restart: create fresh service reading data_dir
    service2 = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        github_client=GitHubClient(runner=FakeRunner()),
    )
    polled_runs.clear()
    service2.reconcile_escalation()
    assert polled_runs == ["run-3", "run-4"]


def test_resolve_escalation_target_binds_to_authoritative_repository(tmp_path: Path) -> None:
    """Review Finding 6: PR target must match authoritative repository identity."""
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-valid-pr",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/my-org/my-repo/pull/42",
        delivery_repository="my-org/my-repo",
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-1", title="Task", description="Desc", external_id="my-org/my-repo#10"),
    )

    pr_payload = {
        "number": 42,
        "url": "https://github.com/my-org/my-repo/pull/42",
        "state": "OPEN",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(pr_payload))])
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is not None
    repo_ref, number, target_type, target_url = target
    assert repo_ref.full_name == "my-org/my-repo"
    assert number == 42
    assert target_type is EscalationTargetType.PULL_REQUEST


def test_resolve_escalation_target_rejects_cross_repository_pr_and_falls_back(
    tmp_path: Path,
) -> None:
    """Review Finding 6: Cross-repository PR URL must not be queried; safely fall back."""
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-cross-pr",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/evil-org/other-repo/pull/99",
        delivery_repository="my-org/my-repo",
    )
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-1", title="Task", description="Desc", external_id="my-org/my-repo#10"),
    )

    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is not None
    repo_ref, number, target_type, target_url = target
    assert repo_ref.full_name == "my-org/my-repo"
    assert number == 10
    assert target_type is EscalationTargetType.ISSUE
    assert "issues/10" in (target_url or "")
    assert len(runner.calls) == 0


def test_resolve_escalation_target_cross_repo_without_source_issue_fails_safely(
    tmp_path: Path,
) -> None:
    """Review Finding 6: Cross-repository PR with no valid source issue fails without writing."""
    config = _make_config(tmp_path)
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-cross-no-issue",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/evil-org/other-repo/pull/99",
        delivery_repository="my-org/my-repo",
    )
    store.save_run(run)

    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    target = resolve_escalation_target(run, store, config, client, tmp_path)
    assert target is None


# ---------------------------------------------------------------------------
# 13. Regression Tests for Release-Readiness Defects
# ---------------------------------------------------------------------------


def test_resolve_escalation_target_binds_pr_host_and_source_host(tmp_path: Path) -> None:
    """Defect 1: PR target must match complete repository identity including host.

    Cross-host PRs (e.g. PR on github.com while issue is on GHE) must be rejected
    and fall back safely to the source issue. Matching GHE PRs must be accepted.
    """
    config = _make_config(tmp_path)
    config = config.model_copy(
        update={
            "escalation": config.escalation.model_copy(
                update={"allowed_hosts": ["github.com", "ghe.corp.internal"]}
            )
        }
    )
    store = FileRunStore(tmp_path)

    # Case A: Source issue on GHE, but PR on github.com -> Must reject PR and fall back to issue
    run_cross = FactoryRun(
        id="run-cross-host",
        work_item_id="task-ghe",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://github.com/my-org/my-repo/pull/42",
        delivery_repository="my-org/my-repo",
        delivery_host="ghe.corp.internal",
    )
    store.save_run(run_cross)
    store.save_artifact(
        run_cross.id,
        WorkItem(
            id="task-ghe",
            title="Task",
            description="Desc",
            external_id="https://ghe.corp.internal/my-org/my-repo/issues/15",
        ),
    )

    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    target_cross = resolve_escalation_target(run_cross, store, config, client, tmp_path)
    assert target_cross is not None
    repo_ref, number, target_type, target_url = target_cross
    assert repo_ref.host == "ghe.corp.internal"
    assert repo_ref.full_name == "my-org/my-repo"
    assert number == 15
    assert target_type is EscalationTargetType.ISSUE
    assert len(runner.calls) == 0  # GitHub was never queried for the cross-host PR

    # Case B: GHE issue and matching GHE PR -> Accepted
    run_ghe = FactoryRun(
        id="run-valid-ghe",
        work_item_id="task-ghe",
        state=WorkflowState.NEEDS_HUMAN,
        pull_request_url="https://ghe.corp.internal/my-org/my-repo/pull/42",
        delivery_repository="my-org/my-repo",
        delivery_host="ghe.corp.internal",
    )
    store.save_run(run_ghe)

    pr_payload = {
        "number": 42,
        "url": "https://ghe.corp.internal/my-org/my-repo/pull/42",
        "state": "OPEN",
    }
    runner_ghe = FakeRunner([FakeCompletedProcess(0, json.dumps(pr_payload))])
    client_ghe = GitHubClient(runner=runner_ghe)

    target_ghe = resolve_escalation_target(run_ghe, store, config, client_ghe, tmp_path)
    assert target_ghe is not None
    ref_ghe, num_ghe, type_ghe, url_ghe = target_ghe
    assert ref_ghe.host == "ghe.corp.internal"
    assert ref_ghe.full_name == "my-org/my-repo"
    assert num_ghe == 42
    assert type_ghe is EscalationTargetType.PULL_REQUEST
    assert url_ghe == "https://ghe.corp.internal/my-org/my-repo/pull/42"


def test_validate_reply_candidate_distinguishes_transient_failure(tmp_path: Path) -> None:
    """Defect 2: Distinguish retryable validation failure from permanent rejection."""
    config = _make_config(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-1",
        status=EscalationStatus.NOTIFIED,
        target_repository="my-org/my-repo",
        target_number=10,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        created_at=now - timedelta(minutes=5),
        last_notified_at=now - timedelta(minutes=5),
        approval_context=_make_approval_context(run_id="run-1", episode_id="ep-1"),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )

    valid_comment = GitHubComment(
        id=200,
        user_login="lead-dev",
        user_id=10,
        user_type="User",
        author_association="MEMBER",
        body="@factory resume v1 run=run-1 episode=ep-1",
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )

    # 1. Transient failure during authoritative comment re-fetch -> retryable=True
    runner_err = FakeRunner([FakeCompletedProcess(1, "", "502 Bad Gateway")])
    client_err = GitHubClient(runner=runner_err)
    res_err = validate_reply_candidate(
        valid_comment, run=run, config=config, client=client_err, repo_path=tmp_path
    )
    assert isinstance(res_err, ValidationResult)
    assert not res_err.is_valid
    assert res_err.retryable is True
    assert "could not re-fetch comment 200" in res_err.reason

    # 2. Permanent failure: invalid grammar -> retryable=False
    bad_grammar_comment = valid_comment.model_copy(update={"body": "hello please resume"})
    runner_ok = FakeRunner()
    client_ok = GitHubClient(runner=runner_ok)
    res_perm = validate_reply_candidate(
        bad_grammar_comment, run=run, config=config, client=client_ok, repo_path=tmp_path
    )
    assert not res_perm.is_valid
    assert res_perm.retryable is False

    # 3. Permanent failure: edited comment -> retryable=False
    re_fetched_edited = {
        "id": 200,
        "body": "@factory resume v1 run=run-1 episode=ep-1",
        "user": {"login": "lead-dev", "id": 10, "type": "User"},
        "author_association": "MEMBER",
        "created_at": (now - timedelta(minutes=2)).isoformat(),
        "updated_at": (now - timedelta(minutes=1)).isoformat(),  # Edited!
    }
    runner_edited = FakeRunner([FakeCompletedProcess(0, json.dumps(re_fetched_edited))])
    client_edited = GitHubClient(runner=runner_edited)
    res_edited = validate_reply_candidate(
        valid_comment, run=run, config=config, client=client_edited, repo_path=tmp_path
    )
    assert not res_edited.is_valid
    assert res_edited.retryable is False
    assert "edited" in res_edited.reason


def test_poll_escalation_reply_does_not_advance_cursor_on_transient_failure(
    tmp_path: Path,
) -> None:
    """Defect 2: Do not advance durable reply cursor past a valid-looking command whose
    re-fetch fails transiently, so a later poll can accept it."""
    config = _make_config(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-1",
        status=EscalationStatus.NOTIFIED,
        target_repository="my-org/my-repo",
        target_number=10,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        created_at=now - timedelta(minutes=10),
        last_notified_at=now - timedelta(minutes=10),
        approval_context=_make_approval_context(run_id="run-cursor-retry", episode_id="ep-1"),
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-cursor-retry",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store = FileRunStore(tmp_path)
    store.save_run(run)

    # Comments returned by list_issue_comments:
    # 1. Non-command comment (id=10)
    # 2. Valid resume command (id=20)
    comment_chat = _make_comment_payload(
        comment_id=10,
        login="random-user",
        user_type="User",
        author_association="NONE",
        body="Just checking in on this run",
        created_at=(now - timedelta(minutes=8)).isoformat(),
        updated_at=(now - timedelta(minutes=8)).isoformat(),
    )
    comment_resume = _make_comment_payload(
        comment_id=20,
        login="lead-dev",
        user_type="User",
        author_association="MEMBER",
        body="@factory resume v1 run=run-cursor-retry episode=ep-1",
        created_at=(now - timedelta(minutes=5)).isoformat(),
        updated_at=(now - timedelta(minutes=5)).isoformat(),
    )

    list_payload = [comment_chat, comment_resume]

    # Poll 1: list succeeds, but re-fetch of comment 20 fails with transient error (500)
    runner_poll1 = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps(list_payload)),  # list_issue_comments
            FakeCompletedProcess(1, "", "500 Internal Server Error"),  # get_issue_comment 20
        ]
    )
    client_poll1 = GitHubClient(runner=runner_poll1)

    receipt1 = poll_escalation_reply(run, store, config, client_poll1, tmp_path, now=now)
    assert receipt1 is None

    # Verify the stored cursor was NOT advanced past comment 20!
    updated_run = store.load_run(run.id)
    assert updated_run.escalation.status is EscalationStatus.NOTIFIED
    assert updated_run.escalation.reply_cursor is not None
    cursor_data = json.loads(updated_run.escalation.reply_cursor)
    assert cursor_data.get("last_id") == 10  # Stopped after comment 10, did NOT advance past 20!

    # Poll 2: Transient failure resolved; re-fetch succeeds
    runner_poll2 = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([comment_resume])),  # list_issue_comments
            FakeCompletedProcess(0, json.dumps(comment_resume)),  # get_issue_comment 20 succeeds!
        ]
    )
    client_poll2 = GitHubClient(runner=runner_poll2)

    receipt2 = poll_escalation_reply(updated_run, store, config, client_poll2, tmp_path, now=now)
    assert receipt2 is not None
    assert receipt2.comment_id == 20
    assert receipt2.user_login == "lead-dev"

    final_run = store.load_run(run.id)
    assert final_run.escalation.status is EscalationStatus.REOPENED
    assert final_run.escalation.reply_cursor == "closed"


def test_reconcile_escalation_handles_reduced_max_reopens_no_loop(
    source_repo: Path, tmp_path: Path
) -> None:
    """Defect 3: When max_reopens is reduced after persistence, REOPENED run must be changed
    to non-dispatchable non-resumable state without entering a repeated dispatch loop."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Initial config allowed 3 reopens
    store = FileRunStore(data_dir)

    receipt = AcceptedReplyReceipt(
        comment_id=5,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command="@factory resume v1 run=run-loop episode=ep-1",
        episode_id="ep-1",
        run_id="run-loop",
    )
    escalation = EscalationRecord(
        episode_id="ep-1",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        accepted_replies=[receipt],
        reopen_count=2,  # Already reopened twice
    )
    run = FactoryRun(
        id="run-loop",
        work_item_id="task-loop",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)

    # Now factory restarts or reconfigures with max_reopens=1 (< reopen_count=2)
    config = _make_config(data_dir, max_reopens=1)

    class DummyProvider:
        def fetch_candidates(self):
            return ()

        def fetch_by_ids(self, ids):
            return ()

    controller = WorkflowController(config, store, FakeAgentRuntime())
    service = FactoryService(
        config=config,
        store=store,
        runtime=FakeAgentRuntime(),
        source_repo=source_repo,
        github_repo="owner/repo",
        provider=DummyProvider(),
        controller=controller,
        github_client=GitHubClient(runner=FakeRunner()),
    )

    # First reconcile tick
    service.reconcile_escalation()
    assert len(service._handles) == 0  # Was NOT dispatched

    # Verify run changed to safe, non-dispatchable, non-resumable escalation state
    stored_run = store.load_run(run.id)
    assert stored_run.state is WorkflowState.NEEDS_HUMAN
    assert stored_run.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert stored_run.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert stored_run.escalation.reason_code == "ATTEMPT_BUDGET_EXHAUSTED"
    assert stored_run.escalation.reply_cursor == "closed"

    # Second reconcile tick: must NOT dispatch or loop
    service.reconcile_escalation()
    assert len(service._handles) == 0


def test_github_client_identity_bounded_cache_and_expiration(tmp_path: Path) -> None:
    """Defect 5: GitHubClient maintains a bounded identity cache tied to active credentials
    and TTL; fails closed on resolution error."""
    runner = FakeRunner()
    client = GitHubClient(runner=runner)

    # 1. Initial call resolves identity
    ident1 = client.get_authenticated_user(tmp_path)
    assert ident1.login == "factory-bot"
    assert len(runner.calls) == 1

    # 2. Immediate second call uses bounded cache
    ident2 = client.get_authenticated_user(tmp_path)
    assert ident2 == ident1
    assert len(runner.calls) == 1

    # 3. force_refresh=True re-queries gh
    ident3 = client.get_authenticated_user(tmp_path, force_refresh=True)
    assert ident3 == ident1
    assert len(runner.calls) == 2

    # 4. max_age_seconds=0 expires cache and re-queries gh
    ident4 = client.get_authenticated_user(tmp_path, max_age_seconds=0)
    assert ident4 == ident1
    assert len(runner.calls) == 3

    # 5. Credential change invalidates cache and re-queries gh
    client.token = "new-distinct-token-value"
    ident5 = client.get_authenticated_user(tmp_path)
    assert ident5 == ident1
    assert len(runner.calls) == 4

    # 6. Failure to resolve fails closed
    def fail_runner(args, cwd=None, env=None):
        return FakeCompletedProcess(1, "", "gh: auth token revoked")

    fail_client = GitHubClient(runner=fail_runner)
    with pytest.raises(GitHubError):
        fail_client.get_authenticated_user(tmp_path)
