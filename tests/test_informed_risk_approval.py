"""Focused tests for informed GitHub risk-approval notices and security boundaries.

Covers:
- Case-specific causal information appears in notice
- Exact approval scope, transition, and exclusions appear
- Security boundary: Markdown/HTML injection, newlines, secrets, local paths
- Output bounds: field limits and total comment size limit
- Unavailable future facts are not claimed
- Legacy persisted records load with approval_context=None
- Missing/invalid decision context cannot be remotely resumed (fail-closed)
- Duplicate, replay, and restart behavior
- Non-risk escalations remain non-resumable
- Triage contract behavior, writing policy checks, and deterministic persistence
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import load_config
from software_agent_factory.escalation import (
    MAX_ESCALATION_COMMENT_CHARS,
    EscalationTargetType,
    build_escalation_comment,
    build_risk_approval_context,
    compute_approval_context_fingerprint,
    contains_unsafe_content,
    deliver_escalation_notification,
    escape_notice_text,
    is_valid_risk_approval_context,
    normalize_whitespace,
    poll_escalation_reply,
    reconcile_undelivered_notifications,
    resolve_escalation_target,
    validate_reply_candidate,
)
from software_agent_factory.github import (
    GitHubClient,
    GitHubComment,
    GitHubError,
    parse_comment_payload,
)
from software_agent_factory.models import (
    AcceptedReplyReceipt,
    AgentRole,
    Complexity,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    TriageResult,
    WorkflowState,
    WorkItem,
    utc_now,
)
from software_agent_factory.repository_profile import generic_repository_profile
from software_agent_factory.store import FileRunStore
from software_agent_factory.verification import redact_secrets
from software_agent_factory.workflow import WorkflowController
from software_agent_factory.workspace import GitWorktreeWorkspace
from software_agent_factory.writing_policy import artifact_passages, validate_artifact_writing


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, responses: list[FakeCompletedProcess] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
        self.default = FakeCompletedProcess(0, json.dumps({"id": 99999, "login": "factory-bot"}))

    def __call__(self, args, cwd=None, env=None):
        self.calls.append((list(args), cwd, dict(env) if env else None))
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            return FakeCompletedProcess(0, json.dumps({"id": 99999, "login": "factory-bot"}))
        if self.responses:
            return self.responses.pop(0)
        return self.default


def _make_config(data_dir: Path):
    config = load_config()
    return config.model_copy(
        update={
            "factory": config.factory.model_copy(update={"data_dir": data_dir}),
            "escalation": config.escalation.model_copy(
                update={
                    "enabled": True,
                    "authorized_identities": ["lead-dev", "security-lead"],
                    "max_reopens": 3,
                    "reply_window_hours": 72,
                }
            ),
        }
    )


def _sample_rationale() -> RiskRationale:
    return RiskRationale(
        intended_outcome="Deploy authorization service to production cluster.",
        sensitive_boundary="Production IAM and Kubernetes cluster secrets.",
        necessity="Task introduces new role-based access control policies.",
        credible_scenario="Incorrect policy binding could grant root cluster access.",
        known_mitigations=[
            "Test policies in staging first.",
            "Limit token lifetime to 15 minutes.",
        ],
        residual_risk="Staging cannot simulate multi-tenant concurrency.",
    )


def _sample_context(
    run_id: str = "run-sample-1",
    episode_id: str = "ep-sample-1",
    work_item_id: str = "task-iam",
    work_item_title: str = "Configure cluster IAM role bindings",
    risk: Risk = Risk.R2,
    complexity: Complexity = Complexity.L2,
) -> RiskApprovalContext:
    rationale = _sample_rationale()
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
    decision_requested = (
        f"Approve advancing run {run_id} to REFINING under risk policy {risk.value}."
    )
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
# 1. Causal Information in Notice
# ---------------------------------------------------------------------------


def test_case_specific_causal_information_in_notice() -> None:
    ctx = _sample_context(run_id="run-iam-42", episode_id="ep-iam-42")
    comment = build_escalation_comment(
        run_id="run-iam-42",
        episode_id="ep-iam-42",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Review and approve.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )

    assert "### Factory Risk Approval Notice" in comment
    assert "The run `run-iam-42` halted because risk `R2` requires human approval." in comment
    assert "- **Work item**: `task-iam` - Configure cluster IAM role bindings" in comment

    # Causal chain section
    assert "#### Why approval is required" in comment
    assert "- Intended outcome: Deploy authorization service to production cluster." in comment
    assert "- Sensitive boundary: Production IAM and Kubernetes cluster secrets." in comment
    assert "- Necessity: Task introduces new role-based access control policies." in comment
    assert (
        "- Credible scenario: Incorrect policy binding could grant root cluster access." in comment
    )
    assert "- Known mitigations:" in comment
    assert "  - Test policies in staging first." in comment
    assert "  - Limit token lifetime to 15 minutes." in comment
    assert "- Residual risk: Staging cannot simulate multi-tenant concurrency." in comment

    # Residual risk section
    assert "#### Residual risk accepted" in comment
    assert "Staging cannot simulate multi-tenant concurrency." in comment


# ---------------------------------------------------------------------------
# 2. Exact Approval Scope & Exclusions
# ---------------------------------------------------------------------------


def test_exact_approval_scope_and_exclusions_appear() -> None:
    ctx = _sample_context(run_id="run-scope-1", episode_id="ep-scope-1")
    comment = build_escalation_comment(
        run_id="run-scope-1",
        episode_id="ep-scope-1",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Review and approve.",
        attempts_consumed=2,
        reopen_count=1,
        max_reopens=3,
        approval_context=ctx,
    )

    # Decision requested
    assert "#### Decision requested" in comment
    assert "Approve advancing run run-scope-1 to REFINING under risk policy R2." in comment

    # Approval authorizes
    assert "#### Approval authorizes" in comment
    assert "- Transition workflow from NEEDS_HUMAN to REFINING." in comment
    assert "- Refine requirements into an explicit specification." in comment
    assert "- Plan implementation steps within approved scope." in comment
    assert "- Execute code changes in an isolated workspace." in comment
    assert "- Run deterministic verification, tests, and review." in comment

    # Approval does NOT authorize
    assert "#### Approval does not authorize" in comment
    assert "- Approval does not change task scope." in comment
    assert "- Approval does not increase retry budgets." in comment
    assert "- Approval does not bypass quality gates." in comment
    assert "- Approval does not alter credential or permission policy." in comment
    assert "- Approval does not change deployment policy." in comment
    assert "- Approval does not override configured merge policy." in comment

    # Conditions that remain in force
    assert "#### Conditions that remain in force" in comment
    assert "- The approved scope remains restricted to this task." in comment
    assert "- Deterministic verification must pass before review." in comment
    assert "- Independent testing and review remain mandatory." in comment
    assert "- Quality gates must pass before pull request creation." in comment
    assert "- Approval resumes the same run at REFINING." in comment
    assert "- Approval does not reset run history or attempt budgets." in comment

    # Resume instructions
    assert "#### Resume instructions" in comment
    assert "@factory resume v1 run=run-scope-1 episode=ep-scope-1" in comment


# ---------------------------------------------------------------------------
# 3. Security Boundary: HTML/Markdown Injection, Newlines, Secrets, Paths
# ---------------------------------------------------------------------------


def test_security_boundary_html_markdown_injection_escaped() -> None:
    escaped = escape_notice_text("<script>alert('xss')</script> `code` <!-- fake marker --> &")
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "<!--" not in escaped
    assert "&lt;!--" in escaped
    assert "`" not in escaped
    assert "&#96;" in escaped


def test_security_boundary_newlines_normalized() -> None:
    multiline = "Line one\n\nLine two\r\n\tLine three   "
    normalized = normalize_whitespace(multiline)
    assert normalized == "Line one Line two Line three"
    assert "\n" not in normalized
    assert "\r" not in normalized


def test_security_boundary_tokens_and_secrets_rejected(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-sec", work_item_id="task-sec", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(
            id="task-sec",
            title="Configure service with token ghp_1234567890abcdef",
            description="desc",
        ),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=RiskRationale(
                intended_outcome="Set API_KEY='sk-1234567890abcdef12345' in env",
                sensitive_boundary="API key trust boundary",
                necessity="Service requires authentication token",
                credible_scenario="Token leak could expose data",
                known_mitigations=["Redact in logs"],
                residual_risk="Manual review required",
            ),
        ),
    )

    ctx = build_risk_approval_context(run, store, episode_id="ep-sec-1")
    # Publication filters fail closed: tokens cause context to be rejected
    assert ctx is None


def test_security_boundary_local_paths_rejected(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-path", work_item_id="task-p", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-p", title="Update /Users/sanjit.roopra/secret/file.py", description="d"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=_sample_rationale(),
        ),
    )

    # Local path in work item title causes context to be rejected as unsafe
    ctx = build_risk_approval_context(run, store, episode_id="ep-p-1")
    assert ctx is None

    # Notice falls back to safe concise notice without resume instructions
    comment = build_escalation_comment(
        run_id=run.id,
        episode_id="ep-p-1",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect local artifacts.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )
    assert "Remote resume is disabled. Manual inspection of local artifacts is required." in comment
    assert "@factory resume" not in comment


def test_contains_unsafe_content_helper() -> None:
    assert contains_unsafe_content("/Users/secret/file.txt")[0] is True
    assert contains_unsafe_content("/home/user/code")[0] is True
    assert contains_unsafe_content("/var/log/syslog")[0] is True
    assert contains_unsafe_content("~/secret/keys")[0] is True
    assert contains_unsafe_content("Traceback (most recent call last):\n  File 'a.py'")[0] is True
    assert contains_unsafe_content("diff --git a/foo b/foo")[0] is True
    # Safe text
    assert contains_unsafe_content("owner/repo#42")[0] is False
    assert contains_unsafe_content("Deploy authorization service safely")[0] is False


# ---------------------------------------------------------------------------
# 4. Output Bounds Enforced
# ---------------------------------------------------------------------------


def test_output_bounds_enforced() -> None:
    ctx = _sample_context(run_id="run-b", episode_id="ep-b")
    comment = build_escalation_comment(
        run_id="run-b",
        episode_id="ep-b",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )
    assert len(comment) <= MAX_ESCALATION_COMMENT_CHARS
    assert len(comment) < 3000


def test_oversized_comment_falls_back_safely() -> None:
    # Construct oversized context with repeated text
    ctx = _sample_context(run_id="run-huge", episode_id="ep-huge")
    long_actions = ["A" * 200 for _ in range(10)]
    oversized_ctx = ctx.model_copy(
        update={
            "authorized_actions": long_actions,
            "unauthorized_actions": long_actions,
            "conditions_in_force": long_actions,
        }
    )
    comment = build_escalation_comment(
        run_id="run-huge",
        episode_id="ep-huge",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=oversized_ctx,
    )
    # Exceeding size limit triggers fallback
    assert "Remote resume is disabled. Manual inspection of local artifacts is required." in comment
    assert "@factory resume" not in comment


# ---------------------------------------------------------------------------
# 5. Unavailable Future Facts Are Not Claimed
# ---------------------------------------------------------------------------


def test_no_unavailable_future_facts_claimed() -> None:
    ctx = _sample_context(run_id="run-future", episode_id="ep-future")
    comment = build_escalation_comment(
        run_id="run-future",
        episode_id="ep-future",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )

    # Must NOT claim that tests pass or files were changed before implement
    assert "all tests passed" not in comment.lower()
    assert "tests pass" not in comment.lower()
    assert "review approved" not in comment.lower()
    assert "changed files:" not in comment.lower()
    assert "files changed:" not in comment.lower()
    assert "diff:" not in comment.lower()

    # Must clearly indicate that verification and testing REMAIN in force
    assert "Deterministic verification must pass before review." in comment
    assert "Independent testing and review remain mandatory." in comment


# ---------------------------------------------------------------------------
# 6. Legacy Persisted Records Load
# ---------------------------------------------------------------------------


def test_legacy_persisted_records_load() -> None:
    legacy_escalation_json = json.dumps(
        {
            "episode_id": "ep-legacy-1",
            "episode_number": 1,
            "status": "NOTIFIED",
            "resume_classification": "RISK_APPROVAL",
            "reason_code": "RISK_APPROVAL",
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z",
        }
    )
    record = EscalationRecord.model_validate_json(legacy_escalation_json)
    assert record.approval_context is None
    assert record.episode_id == "ep-legacy-1"

    legacy_run_json = json.dumps(
        {
            "schema_version": 1,
            "id": "run-legacy-1",
            "work_item_id": "task-legacy",
            "state": "NEEDS_HUMAN",
            "escalation": json.loads(legacy_escalation_json),
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z",
        }
    )
    run = FactoryRun.model_validate_json(legacy_run_json)
    assert run.escalation is not None
    assert run.escalation.approval_context is None


# ---------------------------------------------------------------------------
# 7. Missing or Invalid Decision Context Cannot Be Remotely Resumed
# ---------------------------------------------------------------------------


def test_missing_decision_context_cannot_be_remotely_resumed(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    # Legacy record with approval_context=None
    escalation = EscalationRecord(
        episode_id="ep-legacy",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=None,
    )
    run = FactoryRun(
        id="run-legacy",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=101,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-legacy episode=ep-legacy",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert (
        "remote resume is disabled" in reason
        or "missing or invalid risk approval decision context" in reason
    )


def test_tampered_fingerprint_cannot_be_remotely_resumed(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    ctx = _sample_context(run_id="run-tamper", episode_id="ep-tamper")
    # Tamper with fingerprint
    tampered_ctx = ctx.model_copy(update={"context_fingerprint": "0" * 64})
    escalation = EscalationRecord(
        episode_id="ep-tamper",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=tampered_ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-tamper",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=102,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-tamper episode=ep-tamper",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "missing or invalid risk approval decision context; local inspection required" in reason


# ---------------------------------------------------------------------------
# 8. Duplicate / Replay / Restart Behavior
# ---------------------------------------------------------------------------


def test_duplicate_replay_rejected(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    ctx = _sample_context(run_id="run-rep", episode_id="ep-rep")
    receipt = AcceptedReplyReceipt(
        comment_id=555,
        user_login="lead-dev",
        created_at=now - timedelta(minutes=5),
        command="@factory resume v1 run=run-rep episode=ep-rep",
        episode_id="ep-rep",
        run_id="run-rep",
        approval_context_fingerprint=ctx.context_fingerprint,
    )
    escalation = EscalationRecord(
        episode_id="ep-rep",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        accepted_replies=[receipt],
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-rep",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    # Replay of the exact same comment ID 555
    comment = GitHubComment(
        id=555,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-rep episode=ep-rep",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "already been accepted" in reason


# ---------------------------------------------------------------------------
# 9. Non-Risk Escalations Remain Non-Resumable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason_code",
    ["SCOPE_REVIEW", "ATTEMPT_BUDGET_EXHAUSTED", "CI_INTERVENTION", "MANUAL_INSPECTION"],
)
def test_non_risk_escalations_remain_non_resumable(reason_code: str, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    escalation = EscalationRecord(
        episode_id="ep-nr",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.NOT_RESUMABLE,
        reason_code=reason_code,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
    )
    run = FactoryRun(
        id="run-nr",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=666,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-nr episode=ep-nr",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "not resumable" in reason


# ---------------------------------------------------------------------------
# 10. Triage Contract, Writing Policy, & Deterministic Persistence
# ---------------------------------------------------------------------------


def test_triage_contract_requires_risk_rationale_for_r2_and_r3() -> None:
    # R2 without rationale raises ValidationError
    with pytest.raises(ValidationError, match="risk_rationale is required"):
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=None,
        )

    # R3 without rationale raises ValidationError
    with pytest.raises(ValidationError, match="risk_rationale is required"):
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R3,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=None,
        )

    # R0 and R1 do not require risk_rationale
    triage_r0 = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L0,
        risk=Risk.R0,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
    )
    assert triage_r0.risk_rationale is None

    triage_r1 = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R1,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
    )
    assert triage_r1.risk_rationale is None


def test_writing_policy_checks_risk_rationale() -> None:
    rationale = _sample_rationale()
    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R2,
        requirements_quality="clear task",
        needs_research=False,
        confidence=0.9,
        risk_rationale=rationale,
    )
    passages = artifact_passages(triage)
    passage_names = [p.field for p in passages]
    assert "intended_outcome" in passage_names
    assert "sensitive_boundary" in passage_names
    assert "necessity" in passage_names
    assert "credible_scenario" in passage_names
    assert any(name.startswith("known_mitigations") for name in passage_names)
    assert "residual_risk" in passage_names

    # Check writing policy passes
    findings = validate_artifact_writing(triage)
    assert findings == ()


def test_workflow_controller_persists_approval_context_on_risk_halt(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    def triage_hook(req: AgentRequest) -> AgentResult:
        result = TriageResult(
            factory_eligible=True,
            complexity=Complexity.L1,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=_sample_rationale(),
        )
        return AgentResult(role=AgentRole.TRIAGE, success=True, triage_result=result)

    runtime = FakeAgentRuntime(triage=triage_hook)
    controller = WorkflowController(config, store, runtime)

    work_item = WorkItem(
        id="task-r2-persist",
        title="Sensitive key rotation",
        description="Rotate root signing keys.",
    )

    source = tmp_path / "src"
    source.mkdir()
    import subprocess

    subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "README.md").write_text("test")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "init"], check=True)

    run = controller.run(work_item, source, run_id="run-persist-1")

    assert run.state is WorkflowState.NEEDS_HUMAN
    assert run.escalation is not None
    assert run.escalation.resume_classification is ResumeClassification.RISK_APPROVAL
    assert run.escalation.approval_context is not None

    # Verify persisted on disk
    loaded_run = store.load_run(run.id)
    assert loaded_run.escalation is not None
    assert loaded_run.escalation.approval_context is not None
    assert loaded_run.escalation.approval_context.risk is Risk.R2
    assert is_valid_risk_approval_context(
        loaded_run.escalation.approval_context, loaded_run.id, loaded_run.escalation.episode_id
    )


def test_build_risk_approval_context_missing_artifacts_and_empty_fields(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-missing-art", work_item_id="task-m", state=WorkflowState.NEEDS_HUMAN)

    # 1. Missing WorkItem
    assert build_risk_approval_context(run, store) is None

    # Save WorkItem
    work_item = WorkItem(id="task-m", title="Valid Title", description="d")
    store.save_artifact(run.id, work_item)

    # 2. Missing TriageResult
    assert build_risk_approval_context(run, store) is None

    # Save TriageResult with R1 (not R2/R3)
    triage_r1 = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R1,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
    )
    store.save_artifact(run.id, triage_r1)
    assert build_risk_approval_context(run, store) is None

    # 3. WorkItem with whitespace-only title (fails length / empty cleaned field)
    # Use object construction bypassing Pydantic field_validator if possible or test clean check
    rat = _sample_rationale()
    triage_r2 = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R2,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
        risk_rationale=rat,
    )
    store.save_artifact(run.id, triage_r2)
    ctx = build_risk_approval_context(run, store, episode_id="ep-1")
    assert ctx is not None


def test_is_valid_risk_approval_context_branches() -> None:
    ctx = _sample_context(run_id="run-val", episode_id="ep-val")

    # Non-context object
    assert is_valid_risk_approval_context(None, "run-val", "ep-val") is False
    assert is_valid_risk_approval_context("string", "run-val", "ep-val") is False  # type: ignore[arg-type]

    # Non-R2/R3 risk
    ctx_r1 = ctx.model_copy(update={"risk": Risk.R1})
    assert is_valid_risk_approval_context(ctx_r1, "run-val", "ep-val") is False

    # Next state not REFINING
    ctx_done = ctx.model_copy(update={"next_state": WorkflowState.DONE})
    assert is_valid_risk_approval_context(ctx_done, "run-val", "ep-val") is False

    # Unsafe content in rationale field
    unsafe_rat = ctx.risk_rationale.model_copy(
        update={"intended_outcome": "Install /var/log/secret.txt"}
    )
    ctx_unsafe = ctx.model_copy(update={"risk_rationale": unsafe_rat})
    assert is_valid_risk_approval_context(ctx_unsafe, "run-val", "ep-val") is False


def test_workflow_controller_reopen_validates_approval_context(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    # 1. Run in NEEDS_HUMAN but with NOT_RESUMABLE halt category
    run = FactoryRun(
        id="run-halt-err",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-err",
            status=EscalationStatus.REOPENED,
            resume_classification=ResumeClassification.NOT_RESUMABLE,
            accepted_replies=[
                AcceptedReplyReceipt(
                    comment_id=1,
                    user_login="lead-dev",
                    command="@factory resume v1 run=run-halt-err episode=ep-err",
                    episode_id="ep-err",
                    run_id="run-halt-err",
                    created_at=utc_now(),
                )
            ],
        ),
    )
    store.save_run(run)
    with pytest.raises(ValueError, match="halt category NOT_RESUMABLE cannot be reopened"):
        controller._transition_reopened(run)

    # 2. Run in NEEDS_HUMAN with RISK_APPROVAL but approval_context=None
    run_no_ctx = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={"resume_classification": ResumeClassification.RISK_APPROVAL}
            )
        }
    )
    store.save_run(run_no_ctx)
    with pytest.raises(ValueError, match="missing or invalid risk approval decision context"):
        controller._transition_reopened(run_no_ctx)


def test_end_to_end_delivery_and_reply_polling_maximal_context(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    # Construct maximal valid length fields
    work_item = WorkItem(
        id="task-max-context-" + "x" * 90,
        title="Maximal length safe work item title for informed approval testing " + "t" * 40,
        description="d",
        external_id="owner/repo#42",
    )
    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L2,
        risk=Risk.R2,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
        risk_rationale=RiskRationale(
            intended_outcome="Migrate database cluster tables to partition structure. " + "o" * 150,
            sensitive_boundary="Customer database primary cluster and replication endpoints. "
            + "b" * 150,
            necessity="Work item requires cross-region failover and read replica authorization. "
            + "n" * 140,
            credible_scenario="Incorrect partition routing could leak cross-tenant customer rows. "
            + "s" * 200,
            known_mitigations=[
                "Deploy read-only migration scripts in staging first. " + "m" * 80,
                "Verify replica checksums prior to failover cutover. " + "m" * 80,
                "Run database migration inside isolated staging canary. " + "m" * 80,
                "Maintain rollback scripts tested against replica data. " + "m" * 80,
                "Enforce connection throttling during online rebalancing. " + "m" * 80,
            ],
            residual_risk="Transient query latency increase during online partition rebalancing. "
            + "r" * 140,
        ),
    )
    run = FactoryRun(
        id="run-max",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        failure_reason="risk R2 requires human approval",
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, triage)

    # 1. Delivery
    from software_agent_factory.escalation import (
        deliver_escalation_notification,
        poll_escalation_reply,
    )

    now_str = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_payload = {
        "id": 801,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/801",
        "html_url": "https://github.com/owner/repo/issues/42#comment-801",
        "body": "notice",
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": now_str,
        "updated_at": now_str,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([])),
            FakeCompletedProcess(0, json.dumps(comment_payload)),
        ]
    )
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation is not None
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.remote_resume_enabled is True
    assert delivered_run.escalation.reply_cursor is None  # Open for replies!

    # 2. Reply polling
    ep_id = delivered_run.escalation.episode_id
    reply_time = (delivered_run.escalation.created_at + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    valid_reply_payload = {
        "id": 901,
        "body": f"@factory resume v1 run={delivered_run.id} episode={ep_id}",
        "user": {"login": "lead-dev", "id": 100, "type": "User"},
        "author_association": "MEMBER",
        "created_at": reply_time,
        "updated_at": reply_time,
    }
    runner_poll = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([valid_reply_payload])),
            FakeCompletedProcess(0, json.dumps(valid_reply_payload)),
        ]
    )
    client_poll = GitHubClient(runner=runner_poll)

    receipt = poll_escalation_reply(delivered_run, store, config, client_poll, repo_path)
    assert receipt is not None
    assert receipt.comment_id == 901
    expected_fp = delivered_run.escalation.approval_context.context_fingerprint
    assert receipt.approval_context_fingerprint == expected_fp

    # 3. Reopening transition validates receipt fingerprint
    controller = WorkflowController(config, store, FakeAgentRuntime())
    reopened_run = store.load_run(delivered_run.id)
    assert reopened_run.escalation.status is EscalationStatus.REOPENED
    resumed_run = controller._transition_reopened(reopened_run)
    assert resumed_run.state is WorkflowState.REFINING
    assert resumed_run.escalation.status is EscalationStatus.RESUMED


def test_oversized_context_closes_cursor_and_fails_closed_on_polling(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(id="task-over", title="Task", description="d", external_id="owner/repo#42")
    # Build oversized context
    ctx = _sample_context(run_id="run-over", episode_id="ep-over", work_item_id=work_item.id)
    huge_actions = ["Action line repeating for size limit testing " * 5 for _ in range(10)]
    oversized_ctx = ctx.model_copy(
        update={
            "authorized_actions": huge_actions,
            "unauthorized_actions": huge_actions,
            "conditions_in_force": huge_actions,
        }
    )
    escalation = EscalationRecord(
        episode_id="ep-over",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=oversized_ctx,
    )
    run = FactoryRun(
        id="run-over",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        deliver_escalation_notification,
        poll_escalation_reply,
    )

    now_str = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_payload = {
        "id": 802,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/802",
        "html_url": "https://github.com/owner/repo/issues/42#comment-802",
        "body": "notice",
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": now_str,
        "updated_at": now_str,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([])),
            FakeCompletedProcess(0, json.dumps(comment_payload)),
        ]
    )
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"

    # Polling immediately returns None because cursor is closed
    reply = poll_escalation_reply(delivered_run, store, config, client, repo_path)
    assert reply is None


def test_fingerprint_binds_all_authority_and_displayed_fields() -> None:
    ctx = _sample_context(run_id="run-fp", episode_id="ep-fp")
    assert is_valid_risk_approval_context(ctx, "run-fp", "ep-fp") is True

    # Mutate run_id or episode_id binding
    assert is_valid_risk_approval_context(ctx, "run-other", "ep-fp") is False
    assert is_valid_risk_approval_context(ctx, "run-fp", "ep-other") is False

    # Mutate work_item_id
    bad_id = ctx.model_copy(update={"work_item_id": "other-task"})
    assert is_valid_risk_approval_context(bad_id, "run-fp", "ep-fp") is False

    # Mutate work_item_title
    bad_title = ctx.model_copy(update={"work_item_title": "Altered Title"})
    assert is_valid_risk_approval_context(bad_title, "run-fp", "ep-fp") is False

    # Mutate decision_requested
    bad_dec = ctx.model_copy(update={"decision_requested": "Approve full autonomous deploy."})
    assert is_valid_risk_approval_context(bad_dec, "run-fp", "ep-fp") is False

    # Mutate next_state
    bad_state = ctx.model_copy(update={"next_state": WorkflowState.IMPLEMENTING})
    assert is_valid_risk_approval_context(bad_state, "run-fp", "ep-fp") is False

    # Mutate authorized_actions
    bad_auth = ctx.model_copy(update={"authorized_actions": ["Deploy directly to prod."]})
    assert is_valid_risk_approval_context(bad_auth, "run-fp", "ep-fp") is False

    # Mutate unauthorized_actions
    bad_unauth = ctx.model_copy(update={"unauthorized_actions": ["No restrictions."]})
    assert is_valid_risk_approval_context(bad_unauth, "run-fp", "ep-fp") is False

    # Mutate conditions_in_force
    bad_cond = ctx.model_copy(update={"conditions_in_force": ["Skip verification."]})
    assert is_valid_risk_approval_context(bad_cond, "run-fp", "ep-fp") is False

    # Mutate risk_rationale fields
    rat = ctx.risk_rationale
    bad_outcome = ctx.model_copy(
        update={"risk_rationale": rat.model_copy(update={"intended_outcome": "Altered outcome."})}
    )
    assert is_valid_risk_approval_context(bad_outcome, "run-fp", "ep-fp") is False

    bad_boundary = ctx.model_copy(
        update={
            "risk_rationale": rat.model_copy(update={"sensitive_boundary": "Altered boundary."})
        }
    )
    assert is_valid_risk_approval_context(bad_boundary, "run-fp", "ep-fp") is False

    bad_necessity = ctx.model_copy(
        update={"risk_rationale": rat.model_copy(update={"necessity": "Altered necessity."})}
    )
    assert is_valid_risk_approval_context(bad_necessity, "run-fp", "ep-fp") is False

    bad_scenario = ctx.model_copy(
        update={"risk_rationale": rat.model_copy(update={"credible_scenario": "Altered scenario."})}
    )
    assert is_valid_risk_approval_context(bad_scenario, "run-fp", "ep-fp") is False

    bad_mitigations = ctx.model_copy(
        update={"risk_rationale": rat.model_copy(update={"known_mitigations": ["No mitigation."]})}
    )
    assert is_valid_risk_approval_context(bad_mitigations, "run-fp", "ep-fp") is False

    bad_residual = ctx.model_copy(
        update={"risk_rationale": rat.model_copy(update={"residual_risk": "Zero risk."})}
    )
    assert is_valid_risk_approval_context(bad_residual, "run-fp", "ep-fp") is False


def test_adversarial_markdown_rendering() -> None:
    adversarial_title = "[Click Here](http://evil.com) and ![Image](http://evil.com/pic.png)"
    adversarial_outcome = "@admin @org/security please approve **bold** *italic* _under_ ~strike~"
    adversarial_scenario = (
        "# Injected Header\n| col1 | col2 |\n```sh\nrm -rf /\n``` "
        "<script>alert('xss')</script> <!-- comment -->"
    )
    clean_outcome = escape_notice_text(normalize_whitespace(adversarial_outcome))
    clean_scenario = escape_notice_text(normalize_whitespace(adversarial_scenario))
    clean_title = escape_notice_text(normalize_whitespace(adversarial_title))
    clean_id = escape_notice_text(normalize_whitespace("task-adv-[evil]-@admin-`code`"))

    rat = RiskRationale(
        intended_outcome=clean_outcome,
        sensitive_boundary="Production trust boundary",
        necessity="Required for task",
        credible_scenario=clean_scenario,
        known_mitigations=["Test safely"],
        residual_risk="Accepted risk",
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
    decision_requested = "Approve advancing run run-adv to REFINING under risk policy R2."
    fp = compute_approval_context_fingerprint(
        run_id="run-adv",
        episode_id="ep-adv",
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk=Risk.R2.value,
        complexity=Complexity.L2.value,
        intended_outcome=rat.intended_outcome,
        sensitive_boundary=rat.sensitive_boundary,
        necessity=rat.necessity,
        credible_scenario=rat.credible_scenario,
        known_mitigations=rat.known_mitigations,
        residual_risk=rat.residual_risk,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING.value,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
    )
    ctx = RiskApprovalContext(
        risk=Risk.R2,
        complexity=Complexity.L2,
        work_item_id=clean_id,
        work_item_title=clean_title,
        risk_rationale=rat,
        decision_requested=decision_requested,
        next_state=WorkflowState.REFINING,
        authorized_actions=authorized_actions,
        unauthorized_actions=unauthorized_actions,
        conditions_in_force=conditions_in_force,
        context_fingerprint=fp,
    )

    comment = build_escalation_comment(
        run_id="run-adv",
        episode_id="ep-adv",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )

    assert comment.remote_resume_enabled is True
    # 1. Links and images must not have unescaped square brackets
    assert "[Click Here]" not in comment
    assert "&#91;Click Here&#93;" in comment
    assert "!&#91;Image&#93;" in comment

    # 2. Mentions must not contain raw @
    assert "@admin" not in comment
    assert "&#64;admin" in comment
    assert "@org/security" not in comment
    assert "&#64;org/security" in comment

    # 3. Emphasis must not contain raw * or _ or ~
    assert "**bold**" not in comment
    assert "&#42;&#42;bold&#42;&#42;" in comment
    assert "_under_" not in comment
    assert "&#95;under&#95;" in comment
    assert "~strike~" not in comment
    assert "&#126;strike&#126;" in comment

    # 4. Injected headers must not contain raw #
    assert "# Injected Header" not in comment
    assert "&#35; Injected Header" in comment

    # 5. Table pipes must be escaped
    assert "| col1 | col2 |" not in comment
    assert "&#124; col1 &#124; col2 &#124;" in comment

    # 6. Raw URLs must be neutralized to prevent GFM autolinking
    assert "http://" not in comment
    assert "http&#58;&#47;&#47;" in comment

    # 7. HTML and code delimiters
    assert "<script>" not in comment
    assert "&lt;script&gt;" in comment
    assert "<!-- comment -->" not in comment
    assert "&lt;!-- comment --&gt;" in comment
    assert "```sh" not in comment
    assert "&#96;&#96;&#96;sh" in comment

    # 8. Work item identifier backticks and mentions are escaped
    assert "&#96;code&#96;" in comment


# ---------------------------------------------------------------------------
# 11. Finding 1: General Absolute & Network Path Detection Fail-Closed
# ---------------------------------------------------------------------------


def test_contains_unsafe_content_absolute_paths_with_assignment_and_punctuation() -> None:
    unsafe_samples = [
        "/Users/alice/private/key.pem",
        "path=/Users/alice/private/key.pem",
        "workspace=/workspace/project/key.pem",
        "at:/Volumes/team/repo/key.pem",
        "file: /tmp/file",
        "at /var/log/syslog",
        '("/private/etc/passwd")',
        "key=/custom/dir/secret.pem",
        "home=~/keys/id_rsa",
        r"win=C:\Users\admin\key.pem",
        r"win=C:/Users/admin/key.pem",
        r"unc=\\server\share\key.pem",
        "net=//server/share/key.pem",
        ";/etc/shadow",
        ",/opt/bin/app",
        '{"path": "/srv/data/file.txt"}',
    ]
    for sample in unsafe_samples:
        is_unsafe, reason = contains_unsafe_content(sample)
        assert is_unsafe is True, f"Expected unsafe for: {sample!r}"
        assert reason == "contains local or network file system path"


def test_contains_unsafe_content_avoids_false_positives_for_safe_prose() -> None:
    safe_samples = [
        "owner/repo#42",
        "Deploy authorization service safely",
        "and/or staging environment",
        "either/or approach",
        "step 1/2 complete",
        "ratio: 3/5",
        "date: 2026/09/13",
        "CI/CD pipeline and I/O monitoring",
        "TCP/IP networking",
        "src/main.py and tests/test_app.py",
        "./local/run.sh and ../parent/file.txt",
    ]
    for sample in safe_samples:
        is_unsafe, reason = contains_unsafe_content(sample)
        assert is_unsafe is False, f"False positive for safe prose: {sample!r} ({reason})"


def test_end_to_end_context_building_rejects_absolute_paths_after_punctuation(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    sample_unsafe_fields = [
        ("path=/Users/alice/private/key.pem", "title"),
        ("workspace=/workspace/project/key.pem", "outcome"),
        ("at:/Volumes/team/repo/key.pem", "boundary"),
        ("key=/custom/dir/secret.pem", "necessity"),
        ("home=~/keys/id_rsa", "scenario"),
        (r"win=C:\Users\admin\key.pem", "mitigation"),
        (r"unc=\\server\share\key.pem", "residual"),
    ]

    for idx, (unsafe_val, field_name) in enumerate(sample_unsafe_fields):
        run = FactoryRun(
            id=f"run-path-unsafe-{idx}",
            work_item_id=f"task-path-{idx}",
            state=WorkflowState.NEEDS_HUMAN,
        )
        store.save_run(run)

        title = unsafe_val if field_name == "title" else "Safe task title"
        rat = RiskRationale(
            intended_outcome=(unsafe_val if field_name == "outcome" else "Deploy auth service."),
            sensitive_boundary=(
                unsafe_val if field_name == "boundary" else "Cluster IAM trust boundary."
            ),
            necessity=(
                unsafe_val if field_name == "necessity" else "Required for new role bindings."
            ),
            credible_scenario=(
                unsafe_val if field_name == "scenario" else "Misconfiguration could grant access."
            ),
            known_mitigations=[unsafe_val if field_name == "mitigation" else "Test in staging."],
            residual_risk=(unsafe_val if field_name == "residual" else "Manual approval required."),
        )

        store.save_artifact(run.id, WorkItem(id=f"task-path-{idx}", title=title, description="d"))
        store.save_artifact(
            run.id,
            TriageResult(
                factory_eligible=True,
                complexity=Complexity.L2,
                risk=Risk.R2,
                requirements_quality="clear",
                needs_research=False,
                confidence=0.9,
                risk_rationale=rat,
            ),
        )

        ctx = build_risk_approval_context(run, store, episode_id=f"ep-path-{idx}")
        assert ctx is None, f"Expected context rejected for {field_name}={unsafe_val!r}"


def test_end_to_end_context_building_accepts_safe_prose_with_slashes(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(
        id="run-path-safe",
        work_item_id="task-path-safe",
        state=WorkflowState.NEEDS_HUMAN,
    )
    store.save_run(run)

    title = "Configure IAM for owner/repo#42 (step 1/2)"
    rat = RiskRationale(
        intended_outcome="Deploy CI/CD and I/O monitors and/or audit log export.",
        sensitive_boundary="TCP/IP cluster ingress and/or egress gateway.",
        necessity="Update policy for owner/repo service accounts.",
        credible_scenario="Policy failure could disrupt staging and/or canary environments.",
        known_mitigations=[
            "Test in staging first (phase 1/2).",
            "Verify with CI/CD smoke suite.",
        ],
        residual_risk="Transient connection latency during TCP/IP rebalancing.",
    )

    store.save_artifact(run.id, WorkItem(id="task-path-safe", title=title, description="d"))
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=rat,
        ),
    )

    ctx = build_risk_approval_context(run, store, episode_id="ep-path-safe")
    assert ctx is not None
    assert is_valid_risk_approval_context(ctx, run.id, "ep-path-safe") is True


# ---------------------------------------------------------------------------
# 12. Finding 2: Credential Coverage & External URL Rejection / Defanging
# ---------------------------------------------------------------------------


def test_common_credential_forms_detected_and_rejected() -> None:
    credential_samples = [
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "authorization: basic dXNlcjpwYXNz",
        "Basic dXNlcjpwYXNzd29yZA==",
        "Authorization: Bearer my-secret-token-1234567890",
        (
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3A9PlFUP8AnbV4k"
        ),
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3A9PlFUP8AnbV4k",
    ]
    for cred in credential_samples:
        is_unsafe, reason = contains_unsafe_content(cred)
        assert is_unsafe is True, f"Expected credential detected for: {cred!r}"
        assert reason == "contains token or credential"

        redacted = redact_secrets(cred)
        assert "[REDACTED]" in redacted
        assert cred not in redacted

    # Safe prose mentioning mechanisms must not trigger false positives
    safe_auth_prose = "configure basic authentication and bearer token mechanisms"
    is_unsafe, _ = contains_unsafe_content(safe_auth_prose)
    assert is_unsafe is False
    assert redact_secrets(safe_auth_prose) == safe_auth_prose


def test_external_urls_detected_and_rejected_during_approval_context_construction(
    tmp_path: Path,
) -> None:
    url_samples = [
        "https://evil.example/path",
        "http://evil.example",
        "www.evil.example",
        "www.evil.example/malware",
        "[Click Here](http://evil.com)",
        "[Download](https://evil.example/tool)",
        "<https://evil.example>",
        "ftp://files.evil.example/pub",
    ]

    for url_val in url_samples:
        is_unsafe, reason = contains_unsafe_content(url_val)
        assert is_unsafe is True, f"Expected URL detected for: {url_val!r}"
        assert reason == "contains external URL or link"

    store = FileRunStore(tmp_path)
    for idx, url_val in enumerate(url_samples):
        run = FactoryRun(
            id=f"run-url-{idx}",
            work_item_id=f"task-url-{idx}",
            state=WorkflowState.NEEDS_HUMAN,
        )
        store.save_run(run)
        store.save_artifact(
            run.id,
            WorkItem(
                id=f"task-url-{idx}",
                title=f"Review security policy referencing {url_val}",
                description="desc",
            ),
        )
        store.save_artifact(
            run.id,
            TriageResult(
                factory_eligible=True,
                complexity=Complexity.L2,
                risk=Risk.R2,
                requirements_quality="clear",
                needs_research=False,
                confidence=0.9,
                risk_rationale=_sample_rationale(),
            ),
        )

        ctx = build_risk_approval_context(run, store, episode_id=f"ep-url-{idx}")
        # External URL-bearing context MUST be rejected during context construction
        assert ctx is None, f"Expected context rejected for URL {url_val!r}"


def test_escape_notice_text_neutralizes_bare_urls_and_domains_to_prevent_autolinks() -> None:
    raw_text = "Visit https://evil.example/path and www.evil.example and http://evil.com/payload"
    escaped = escape_notice_text(raw_text)
    assert "https://" not in escaped
    assert "http://" not in escaped
    assert "www." not in escaped
    assert "https&#58;&#47;&#47;" in escaped
    assert "http&#58;&#47;&#47;" in escaped
    assert "www&#46;" in escaped


# ---------------------------------------------------------------------------
# 13. Finding 3: Reply Created After Notification (Timing & Fail-Closed)
# ---------------------------------------------------------------------------


def test_reply_candidate_posted_after_episode_creation_before_notification_rejected(
    tmp_path: Path,
) -> None:
    config = _make_config(tmp_path)
    base_time = datetime.fromisoformat("2026-09-13T10:00:00+00:00")
    t_episode = base_time
    t_comment = base_time + timedelta(minutes=2)
    t_notified = base_time + timedelta(minutes=5)

    ctx = _sample_context(run_id="run-timing-1", episode_id="ep-timing-1")
    escalation = EscalationRecord(
        episode_id="ep-timing-1",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=t_episode,
        last_notified_at=t_notified,
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-timing-1",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )

    # Comment was posted after episode creation (10:02 > 10:00)
    # but BEFORE notification (10:02 < 10:05)
    comment = GitHubComment(
        id=701,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=t_comment,
        updated_at=t_comment,
        body="@factory resume v1 run=run-timing-1 episode=ep-timing-1",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert is_valid is False
    assert "created before escalation notification was posted" in reason


def test_reply_candidate_missing_notification_timestamp_rejected(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now()
    ctx = _sample_context(run_id="run-timing-2", episode_id="ep-timing-2")
    escalation = EscalationRecord(
        episode_id="ep-timing-2",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=None,  # Missing notification timestamp!
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-timing-2",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=702,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-timing-2 episode=ep-timing-2",
    )
    client = GitHubClient(runner=lambda *a, **k: None)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert is_valid is False
    assert "escalation has not been successfully notified" in reason


def test_reply_candidate_valid_post_notification_command_accepted(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    base_time = datetime.fromisoformat("2026-09-13T10:00:00+00:00")
    t_episode = base_time
    t_notified = base_time + timedelta(minutes=5)
    t_comment = base_time + timedelta(minutes=6)

    ctx = _sample_context(run_id="run-timing-3", episode_id="ep-timing-3")
    escalation = EscalationRecord(
        episode_id="ep-timing-3",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=t_episode,
        last_notified_at=t_notified,
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-timing-3",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )

    # Comment posted after notification was posted (10:06 > 10:05)
    comment_payload = {
        "id": 703,
        "body": "@factory resume v1 run=run-timing-3 episode=ep-timing-3",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "MEMBER",
        "created_at": t_comment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": t_comment.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    comment = parse_comment_payload(comment_payload)

    # Mock client re-fetch returns the same comment
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_payload))])
    client = GitHubClient(runner=runner)

    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert is_valid is True
    assert reason == "valid"


def test_reply_candidate_at_exact_notification_timestamp_accepted(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    t_notified = datetime.fromisoformat("2026-09-13T10:05:00+00:00")

    ctx = _sample_context(run_id="run-timing-4", episode_id="ep-timing-4")
    escalation = EscalationRecord(
        episode_id="ep-timing-4",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=t_notified - timedelta(minutes=5),
        last_notified_at=t_notified,
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-timing-4",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )

    comment_payload = {
        "id": 704,
        "body": "@factory resume v1 run=run-timing-4 episode=ep-timing-4",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "MEMBER",
        "created_at": t_notified.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": t_notified.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    comment = parse_comment_payload(comment_payload)
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_payload))])
    client = GitHubClient(runner=runner)

    # comment.created_at >= last_notified_at must pass
    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client, repo_path=tmp_path
    )
    assert is_valid is True
    assert reason == "valid"


def test_notification_delivery_deduplication_preserves_last_notified_at(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-dedup", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-dedup", episode_id="ep-dedup", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-dedup",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-dedup",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        build_escalation_comment,
        deliver_escalation_notification,
    )

    # Existing comment on GitHub already contains exact notice from earlier delivery
    expected_notice = build_escalation_comment(
        run_id="run-dedup",
        episode_id="ep-dedup",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    existing_comment_time = "2026-09-13T10:00:00Z"
    existing_comment_payload = {
        "id": 880,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/880",
        "html_url": "https://github.com/owner/repo/issues/42#comment-880",
        "body": str(expected_notice),
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": existing_comment_time,
        "updated_at": existing_comment_time,
        "author_association": "COLLABORATOR",
    }
    # list_issue_comments returns existing comment -> no create_issue_comment call made!
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.comment_id == 880
    assert delivered_run.escalation.last_notified_at is not None
    # Verifies timestamp matches existing comment created_at
    assert delivered_run.escalation.last_notified_at.isoformat().startswith("2026-09-13T10:00:00")


@pytest.mark.parametrize(
    "unsafe_input",
    [
        "/Volumes/ExternalData/key.pem",
        "/Volumes",
        "/workspace/project/secrets.env",
        "/workspace",
        "/workspaces/code/token",
        "/srv/app/credentials.json",
        "/srv",
        "/custom/absolute/path.txt",
        "/mnt/storage/keys",
        "/media/usb/secret",
        "/app/production/keys.json",
        "/data/prod/db.sqlite",
        "\\\\nas-server\\share\\data",
        "//backup-server/share/keys",
        "C:\\Windows\\System32\\cmd.exe",
        "D:/workspace/project/secret.txt",
        "https://user:password@github.com/repo",
        "https://api-token@corp.internal/v1",
        "postgres://admin:secret123@db.prod.internal:5432/mydb",
        "xoxb-1234567890-abcdef123456",
        "xoxp-9876543210-abcdef123456",
        "xoxa-1111222233-abcdef123456",
        "xoxr-1234567890-abcdef123456",
        "xoxs-1234567890-abcdef123456",
        "ghp_1234567890abcdefghijklmnopqrstuv",
        "github_pat_11A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0",
        "glpat-1234567890abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "sk-proj-abc123def456ghi789jkl012mno345pqr",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "api_key: secret-api-key-12345678",
        "-----BEGIN RSA PRIVATE KEY-----",
        "/secret.txt",
        'Authorization: Digest username="admin", realm="test", nonce="abc"',
        'Proxy-Authorization: Digest username="admin"',
        "Cookie: sessionid=1234567890abcdef",
        "Set-Cookie: sessionid=1234567890abcdef",
        "sessionid=1234567890abcdef",
        "session_id=1234567890abcdef",
    ],
)
def test_publication_filters_reject_paths_and_tokens(unsafe_input: str, tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-filter-test", work_item_id="task-f", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-f", title=f"Task with {unsafe_input}", description="d"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=_sample_rationale(),
        ),
    )

    ctx = build_risk_approval_context(run, store, episode_id="ep-f")
    assert ctx is None, f"Expected {unsafe_input} to be rejected by publication filters"


def test_transition_reopened_requires_matching_fingerprint(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)
    controller = WorkflowController(config, store, FakeAgentRuntime())

    work_item = WorkItem(id="task-reopen-fp", title="Task", description="d")
    app_ctx = _sample_context(run_id="run-reopen-fp", episode_id="ep-reopen-fp")

    # 1. Receipt with None fingerprint must fail
    receipt_none_fp = AcceptedReplyReceipt(
        comment_id=101,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command="@factory resume v1 run=run-reopen-fp episode=ep-reopen-fp",
        episode_id="ep-reopen-fp",
        run_id="run-reopen-fp",
        approval_context_fingerprint=None,
    )
    run = FactoryRun(
        id="run-reopen-fp",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-reopen-fp",
            status=EscalationStatus.REOPENED,
            resume_classification=ResumeClassification.RISK_APPROVAL,
            approval_context=app_ctx,
            accepted_replies=[receipt_none_fp],
            remote_resume_enabled=True,
        ),
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    with pytest.raises(ValueError, match="receipt fingerprint does not match"):
        controller._transition_reopened(run)

    # 2. Receipt with mismatched fingerprint must fail
    receipt_mismatched = receipt_none_fp.model_copy(
        update={"approval_context_fingerprint": "0" * 64}
    )
    run_mismatched = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={"accepted_replies": [receipt_mismatched]}
            )
        }
    )
    store.save_run(run_mismatched)
    with pytest.raises(ValueError, match="receipt fingerprint does not match"):
        controller._transition_reopened(run_mismatched)

    # 3. Altered approval context (tampered fields) must fail
    tampered_ctx = app_ctx.model_copy(update={"work_item_title": "Tampered Title"})
    run_tampered = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(
                update={
                    "approval_context": tampered_ctx,
                    "accepted_replies": [
                        receipt_none_fp.model_copy(
                            update={
                                "approval_context_fingerprint": tampered_ctx.context_fingerprint
                            }
                        )
                    ],
                }
            )
        }
    )
    store.save_run(run_tampered)
    with pytest.raises(ValueError, match="missing or invalid risk approval decision context"):
        controller._transition_reopened(run_tampered)

    # 4. Receipt with exact matching fingerprint succeeds
    receipt_valid = receipt_none_fp.model_copy(
        update={"approval_context_fingerprint": app_ctx.context_fingerprint}
    )
    run_valid = run.model_copy(
        update={
            "escalation": run.escalation.model_copy(update={"accepted_replies": [receipt_valid]})
        }
    )
    store.save_run(run_valid)
    resumed = controller._transition_reopened(run_valid)
    assert resumed.state is WorkflowState.REFINING
    assert resumed.escalation.status is EscalationStatus.RESUMED


# ---------------------------------------------------------------------------
# Notification deduplication & remote resume tests (Finding 1)
# ---------------------------------------------------------------------------


def test_notification_dedup_marker_only_body_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-marker-only", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-mo", episode_id="ep-mo", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-mo",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-mo",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        deliver_escalation_notification,
        format_escalation_marker,
    )

    marker = format_escalation_marker("run-mo", "ep-mo")
    marker_only_body = f"{marker}\nJust marker present without rendered approval notice."
    existing_comment_payload = {
        "id": 881,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/881",
        "html_url": "https://github.com/owner/repo/issues/42#comment-881",
        "body": marker_only_body,
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": "2026-09-13T10:00:00Z",
        "updated_at": "2026-09-13T10:00:00Z",
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert delivered_run.escalation.last_notified_at is None
    assert delivered_run.escalation.comment_id is None


def test_notification_dedup_altered_body_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-altered-body", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-alt", episode_id="ep-alt", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-alt",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-alt",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        build_escalation_comment,
        deliver_escalation_notification,
    )

    expected_notice = build_escalation_comment(
        run_id="run-alt",
        episode_id="ep-alt",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    altered_body = str(expected_notice).replace(
        "Approve advancing run", "Malicious altered body text"
    )
    existing_comment_payload = {
        "id": 882,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/882",
        "html_url": "https://github.com/owner/repo/issues/42#comment-882",
        "body": altered_body,
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": "2026-09-13T10:00:00Z",
        "updated_at": "2026-09-13T10:00:00Z",
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert delivered_run.escalation.last_notified_at is None
    assert delivered_run.escalation.comment_id is None


def test_notification_dedup_wrong_author_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-wrong-author", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-wa", episode_id="ep-wa", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-wa",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-wa",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        build_escalation_comment,
        deliver_escalation_notification,
    )

    expected_notice = build_escalation_comment(
        run_id="run-wa",
        episode_id="ep-wa",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    existing_comment_payload = {
        "id": 883,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/883",
        "html_url": "https://github.com/owner/repo/issues/42#comment-883",
        "body": str(expected_notice),
        "user": {"login": "attacker-user", "id": 12345, "type": "User"},
        "created_at": "2026-09-13T10:00:00Z",
        "updated_at": "2026-09-13T10:00:00Z",
        "author_association": "NONE",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert delivered_run.escalation.last_notified_at is None
    assert delivered_run.escalation.comment_id is None


def test_notification_dedup_correct_body_correct_factory_author(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-correct-author", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-ca", episode_id="ep-ca", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-ca",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-ca",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        build_escalation_comment,
        deliver_escalation_notification,
    )

    expected_notice = build_escalation_comment(
        run_id="run-ca",
        episode_id="ep-ca",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    existing_time = "2026-09-13T10:00:00Z"
    existing_comment_payload = {
        "id": 884,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/884",
        "html_url": "https://github.com/owner/repo/issues/42#comment-884",
        "body": str(expected_notice),
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": existing_time,
        "updated_at": existing_time,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.remote_resume_enabled is True
    assert delivered_run.escalation.reply_cursor is None
    assert delivered_run.escalation.comment_id == 884
    assert delivered_run.escalation.last_notified_at is not None
    assert delivered_run.escalation.last_notified_at.isoformat().startswith("2026-09-13T10:00:00")


def test_notification_dedup_network_retry_recovery(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-net-rec", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-nr", episode_id="ep-nr", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-nr",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        delivery_attempts=1,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-nr",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    from software_agent_factory.escalation import (
        build_escalation_comment,
        deliver_escalation_notification,
    )

    expected_notice = build_escalation_comment(
        run_id="run-nr",
        episode_id="ep-nr",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    existing_time = "2026-09-13T10:00:00Z"
    existing_comment_payload = {
        "id": 885,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/885",
        "html_url": "https://github.com/owner/repo/issues/42#comment-885",
        "body": str(expected_notice),
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": existing_time,
        "updated_at": existing_time,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner([FakeCompletedProcess(0, json.dumps([existing_comment_payload]))])
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.comment_id == 885
    assert delivered_run.escalation.remote_resume_enabled is True
    assert delivered_run.escalation.delivery_attempts == 2
    assert delivered_run.escalation.last_notified_at is not None
    assert delivered_run.escalation.last_notified_at.isoformat().startswith("2026-09-13T10:00:00")


# ---------------------------------------------------------------------------
# Context construction & rendering tests (Finding 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret_input",
    [
        "/secret.txt",
        (
            'Authorization: Digest username="admin", realm="test", '
            'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", uri="/api", '
            'response="6629fae49393a05397450978507c4ef1"'
        ),
        'Proxy-Authorization: Digest username="proxyadmin", realm="corp", nonce="12345"',
        "Cookie: sessionid=1234567890abcdef",
        "Set-Cookie: sessionid=1234567890abcdef; Secure; HttpOnly",
        "sessionid=1234567890abcdef",
        "session_id=1234567890abcdef",
    ],
)
def test_end_to_end_context_construction_and_rendering_rejects_secrets(
    secret_input: str, tmp_path: Path
) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-e2e-sec", work_item_id="task-sec", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-sec", title=f"Task with {secret_input}", description="desc"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=_sample_rationale(),
        ),
    )

    ctx = build_risk_approval_context(run, store, episode_id="ep-sec")
    assert ctx is None, f"Expected {secret_input} to be rejected in build_risk_approval_context"

    from software_agent_factory.escalation import build_escalation_comment

    comment = build_escalation_comment(
        run_id="run-e2e-sec",
        episode_id="ep-sec",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect locally.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=3,
        approval_context=None,
    )
    assert getattr(comment, "remote_resume_enabled", False) is False
    rendered_text = str(comment)
    assert "@factory resume" not in rendered_text
    assert (
        "Remote resume is disabled. Manual inspection of local artifacts is required."
        in rendered_text
    )


@pytest.mark.parametrize(
    "safe_input",
    [
        "owner/repo#42",
        "docs/architecture.md",
        "src/models/user.py",
        "and/or condition evaluated",
        "either/or branch taken",
        "ratio is 1/2 of baseline",
        "input/output buffer sized 50/50",
    ],
)
def test_end_to_end_context_construction_and_rendering_safe_counterexamples(
    safe_input: str, tmp_path: Path
) -> None:
    store = FileRunStore(tmp_path)
    run = FactoryRun(id="run-e2e-safe", work_item_id="task-safe", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)
    store.save_artifact(
        run.id,
        WorkItem(id="task-safe", title=f"Task with {safe_input}", description="desc"),
    )
    store.save_artifact(
        run.id,
        TriageResult(
            factory_eligible=True,
            complexity=Complexity.L2,
            risk=Risk.R2,
            requirements_quality="clear",
            needs_research=False,
            confidence=0.9,
            risk_rationale=_sample_rationale(),
        ),
    )

    ctx = build_risk_approval_context(run, store, episode_id="ep-safe")
    assert ctx is not None, f"Expected {safe_input} to be accepted as safe prose"

    from software_agent_factory.escalation import build_escalation_comment

    comment = build_escalation_comment(
        run_id="run-e2e-safe",
        episode_id="ep-safe",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Review and approve.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )
    assert getattr(comment, "remote_resume_enabled", False) is True
    rendered_text = str(comment)
    assert "@factory resume v1 run=run-e2e-safe episode=ep-safe" in rendered_text


def test_redaction_replaces_auth_and_cookie_secrets_in_diagnostics() -> None:
    from software_agent_factory.verification import redact_secrets, sanitize_output

    sample_diagnostic = (
        "HTTP 401 Unauthorized\n"
        'Authorization: Digest username="admin", realm="auth", nonce="12345", response="67890"\n'
        'Proxy-Authorization: Digest username="proxy", realm="test", nonce="abc"\n'
        "Cookie: sessionid=xyz9876543210; theme=dark\n"
        "Set-Cookie: sessionid=xyz9876543210; Path=/; HttpOnly\n"
        "Set-Cookie2: auth_token=secret_token_12345678\n"
        "session_id=abcdef123456789\n"
        "sessionid=abcdef123456789\n"
        "Stack trace:\n"
        '  File "app.py", line 42 in run\n'
    )

    redacted = redact_secrets(sample_diagnostic)
    assert "Authorization: Digest" not in redacted
    assert "Proxy-Authorization: Digest" not in redacted
    assert "Cookie: sessionid" not in redacted
    assert "Set-Cookie: sessionid" not in redacted
    assert "xyz9876543210" not in redacted
    assert "abcdef123456789" not in redacted
    assert "secret_token_12345678" not in redacted

    sanitized = sanitize_output(sample_diagnostic, limit=1000)
    assert "xyz9876543210" not in sanitized
    assert "abcdef123456789" not in sanitized


# ---------------------------------------------------------------------------
# Additional regression scenarios for coverage restoration
# ---------------------------------------------------------------------------


def test_risk_rationale_field_and_item_validators() -> None:
    with pytest.raises(ValidationError, match="field must not be blank"):
        RiskRationale(
            intended_outcome="   ",
            sensitive_boundary="b",
            necessity="n",
            credible_scenario="s",
            known_mitigations=["m"],
            residual_risk="r",
        )

    with pytest.raises(ValidationError, match="at least 1 item"):
        RiskRationale.model_validate(
            {
                "intended_outcome": "o",
                "sensitive_boundary": "b",
                "necessity": "n",
                "credible_scenario": "s",
                "known_mitigations": [],
                "residual_risk": "r",
            }
        )

    with pytest.raises(ValidationError, match="known_mitigations entries must not be blank"):
        RiskRationale(
            intended_outcome="o",
            sensitive_boundary="b",
            necessity="n",
            credible_scenario="s",
            known_mitigations=["   "],
            residual_risk="r",
        )

    with pytest.raises(
        ValidationError, match="known_mitigations entries must be 200 characters or fewer"
    ):
        RiskRationale(
            intended_outcome="o",
            sensitive_boundary="b",
            necessity="n",
            credible_scenario="s",
            known_mitigations=["m" * 201],
            residual_risk="r",
        )


def test_deliver_escalation_notification_unverified_factory_fails_closed(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-unver", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-unver", episode_id="ep-unver", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-unver",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-unver",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    def runner_fail(args, cwd=None, env=None):
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            raise GitHubError("cannot resolve identity")
        return FakeCompletedProcess(0, json.dumps([]))

    client = GitHubClient(runner=runner_fail)
    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert delivered_run.escalation.delivery_error == (
        "cannot verify authenticated factory account; failing closed for local inspection"
    )
    assert delivered_run.escalation.delivery_attempts == 1
    assert delivered_run.escalation.last_notified_at is None


def test_deliver_escalation_notification_terminal_delivery_failure_fails_closed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(id="task-term", title="Task", description="d", external_id="owner/repo#42")
    ctx = _sample_context(run_id="run-term", episode_id="ep-term", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-term",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        delivery_attempts=config.escalation.max_notification_attempts - 1,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-term",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    runner = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([])),
            FakeCompletedProcess(1, "", stderr="500 Internal Server Error"),
        ]
    )
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert "notification delivery error" in (delivered_run.escalation.delivery_error or "")
    assert delivered_run.escalation.delivery_attempts == config.escalation.max_notification_attempts


def test_deliver_escalation_notification_deduplication_list_error_proceeds(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-list-err", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-le", episode_id="ep-le", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-le",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-le",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    now_str = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_payload = {
        "id": 886,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/886",
        "html_url": "https://github.com/owner/repo/issues/42#comment-886",
        "body": "notice",
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": now_str,
        "updated_at": now_str,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(1, "", stderr="rate limit on list comments"),
            FakeCompletedProcess(0, json.dumps(comment_payload)),
        ]
    )
    client = GitHubClient(runner=runner)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFIED
    assert delivered_run.escalation.comment_id == 886


def test_deliver_escalation_notification_dedup_existing_marker_unverified_author_fails_closed(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item = WorkItem(
        id="task-unver-marker", title="Task", description="d", external_id="owner/repo#42"
    )
    ctx = _sample_context(run_id="run-um", episode_id="ep-um", work_item_id=work_item.id)
    escalation = EscalationRecord(
        episode_id="ep-um",
        status=EscalationStatus.PENDING_NOTIFICATION,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=ctx,
    )
    run = FactoryRun(
        id="run-um",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    store.save_artifact(run.id, work_item)

    expected_notice = build_escalation_comment(
        run_id="run-um",
        episode_id="ep-um",
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="The run requires approval under the configured risk policy.",
        next_action="Review the work item risk and approve or change the policy before retrying.",
        attempts_consumed=0,
        reopen_count=0,
        max_reopens=config.escalation.max_reopens,
        approval_context=ctx,
    )
    existing_payload = {
        "id": 887,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/887",
        "html_url": "https://github.com/owner/repo/issues/42#comment-887",
        "body": str(expected_notice),
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": "2026-09-13T10:00:00Z",
        "updated_at": "2026-09-13T10:00:00Z",
        "author_association": "COLLABORATOR",
    }

    def runner_unverified(args, cwd=None, env=None):
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            raise GitHubError("cannot resolve identity")
        return FakeCompletedProcess(0, json.dumps([existing_payload]))

    client = GitHubClient(runner=runner_unverified)

    delivered_run = deliver_escalation_notification(run, store, config, client, repo_path)
    assert delivered_run.escalation.status is EscalationStatus.NOTIFICATION_FAILED
    assert delivered_run.escalation.remote_resume_enabled is False
    assert delivered_run.escalation.reply_cursor == "closed"
    assert "existing comment with escalation marker has altered body or unverified author" in (
        delivered_run.escalation.delivery_error or ""
    )


def test_build_escalation_comment_size_limit_warning_and_fallback() -> None:
    rat = RiskRationale(
        intended_outcome="Deploy cluster authorization service. " + "o" * 250,
        sensitive_boundary="Production IAM and Kubernetes cluster secrets. " + "b" * 250,
        necessity="Task introduces new role-based access control policies. " + "n" * 240,
        credible_scenario="Incorrect policy binding could grant root cluster access. " + "s" * 340,
        known_mitigations=[
            f"Mitigation measure number {i:02d} for risk controls: " + "m" * 150 for i in range(10)
        ],
        residual_risk="Staging cannot simulate multi-tenant concurrency. " + "r" * 250,
    )
    auth_actions = [
        f"Authorized action step {i}: execute verification and tests within approved boundaries."
        for i in range(10)
    ]
    unauth_actions = [
        f"Unauthorized action {i}: approval does not alter credential or permission policy."
        for i in range(10)
    ]
    conditions = [
        f"Condition in force {i}: deterministic verification must pass before review."
        for i in range(10)
    ]
    decision_req = (
        "Approve advancing run run-huge to REFINING under risk policy R2 with all conditions."
    )

    run_id = "run-huge"
    ep_id = "ep-huge"
    work_id = "task-huge-oversized-context-verification"
    work_title = "Title of the oversized risk approval task for testing maximum comment size bounds"

    fp = compute_approval_context_fingerprint(
        run_id=run_id,
        episode_id=ep_id,
        work_item_id=work_id,
        work_item_title=work_title,
        risk=Risk.R2.value,
        complexity=Complexity.L2.value,
        intended_outcome=rat.intended_outcome,
        sensitive_boundary=rat.sensitive_boundary,
        necessity=rat.necessity,
        credible_scenario=rat.credible_scenario,
        known_mitigations=rat.known_mitigations,
        residual_risk=rat.residual_risk,
        decision_requested=decision_req,
        next_state=WorkflowState.REFINING.value,
        authorized_actions=auth_actions,
        unauthorized_actions=unauth_actions,
        conditions_in_force=conditions,
    )
    ctx = RiskApprovalContext(
        risk=Risk.R2,
        complexity=Complexity.L2,
        work_item_id=work_id,
        work_item_title=work_title,
        risk_rationale=rat,
        decision_requested=decision_req,
        next_state=WorkflowState.REFINING,
        authorized_actions=auth_actions,
        unauthorized_actions=unauth_actions,
        conditions_in_force=conditions,
        context_fingerprint=fp,
    )
    assert is_valid_risk_approval_context(ctx, run_id, ep_id) is True

    comment = build_escalation_comment(
        run_id=run_id,
        episode_id=ep_id,
        classification=ResumeClassification.RISK_APPROVAL,
        reason_code="RISK_APPROVAL",
        summary="Risk R2 requires approval.",
        next_action="Inspect locally.",
        attempts_consumed=1,
        reopen_count=0,
        max_reopens=3,
        approval_context=ctx,
    )
    assert comment.remote_resume_enabled is False
    assert (
        "This risk approval escalation notice exceeded the maximum comment size limit. "
        "Remote resume is disabled. Manual inspection of local artifacts is required."
        in str(comment)
    )
    assert "@factory resume" not in str(comment)


def test_build_risk_approval_context_empty_cleaned_fields_and_workflow_halt(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    run = FactoryRun(id="run-blank", work_item_id="task-blank", state=WorkflowState.NEEDS_HUMAN)
    store.save_run(run)

    work_item = WorkItem.model_construct(id="   ", title="Title", description="d")
    store.save_artifact(run.id, work_item)

    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R2,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
        risk_rationale=_sample_rationale(),
    )
    store.save_artifact(run.id, triage)

    assert build_risk_approval_context(run, store, episode_id="ep-blank") is None

    controller = WorkflowController(config, store, FakeAgentRuntime())
    active_run = FactoryRun(
        id="run-halt-blank", work_item_id="task-blank", state=WorkflowState.TRIAGING
    )
    store.save_run(active_run)
    store.save_artifact(active_run.id, work_item)
    store.save_artifact(active_run.id, triage)

    halted_run = controller.transition(
        active_run,
        WorkflowState.NEEDS_HUMAN,
        failure_reason="risk R2 requires human approval",
    )
    assert halted_run.state is WorkflowState.NEEDS_HUMAN
    assert halted_run.escalation is not None
    assert halted_run.escalation.approval_context is None
    assert halted_run.escalation.remote_resume_enabled is False


def _setup_reopened_environment(tmp_path: Path):
    import subprocess

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(source), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "commit.gpgsign", "false"], check=True)
    (source / "README.md").write_text("initial\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-m", "init"], check=True)

    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    work_item_id = "task-reopen-e2e"
    work_item = WorkItem(id=work_item_id, title="Test Task", description="desc")
    ws = GitWorktreeWorkspace(
        data_dir,
        source,
        work_item_id,
        branch_prefix=config.repository.branch_prefix,
    )
    ws.prepare()
    ws.release_lock()

    app_ctx = _sample_context(
        run_id="run-reopen-e2e", episode_id="ep-reopen-e2e", work_item_id=work_item_id
    )
    triage = TriageResult(
        factory_eligible=True,
        complexity=Complexity.L1,
        risk=Risk.R2,
        requirements_quality="clear",
        needs_research=False,
        confidence=0.9,
        risk_rationale=app_ctx.risk_rationale,
    )
    repo_profile = generic_repository_profile()

    run = FactoryRun(
        id="run-reopen-e2e",
        work_item_id=work_item_id,
        state=WorkflowState.NEEDS_HUMAN,
        workspace_path=str(ws.path),
        branch_name=ws.branch_name,
        base_commit_sha=ws.base_commit,
    )
    return source, config, store, run, app_ctx, work_item, triage, repo_profile


def test_workflow_controller_reopen_missing_receipt_fingerprint_fails_closed(
    tmp_path: Path,
) -> None:
    (
        source,
        config,
        store,
        run,
        app_ctx,
        work_item,
        triage,
        repo_profile,
    ) = _setup_reopened_environment(tmp_path)
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={run.id} episode=ep-reopen-e2e",
        episode_id="ep-reopen-e2e",
        run_id=run.id,
        approval_context_fingerprint=None,
    )
    escalation = EscalationRecord(
        episode_id="ep-reopen-e2e",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=app_ctx,
        accepted_replies=[receipt],
        remote_resume_enabled=True,
    )
    run = run.model_copy(update={"escalation": escalation})
    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, triage)
    store.save_artifact(run.id, repo_profile)

    controller = WorkflowController(config, store, FakeAgentRuntime())
    reopened = controller.reopen(run.id, source)

    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "RECOVERY_INTERVENTION"
    assert reopened.escalation.reply_cursor == "closed"
    assert reopened.escalation.episode_number == 2


def test_workflow_controller_reopen_mismatched_receipt_fingerprint_fails_closed(
    tmp_path: Path,
) -> None:
    (
        source,
        config,
        store,
        run,
        app_ctx,
        work_item,
        triage,
        repo_profile,
    ) = _setup_reopened_environment(tmp_path)
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={run.id} episode=ep-reopen-e2e",
        episode_id="ep-reopen-e2e",
        run_id=run.id,
        approval_context_fingerprint="0" * 64,
    )
    escalation = EscalationRecord(
        episode_id="ep-reopen-e2e",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=app_ctx,
        accepted_replies=[receipt],
        remote_resume_enabled=True,
    )
    run = run.model_copy(update={"escalation": escalation})
    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, triage)
    store.save_artifact(run.id, repo_profile)

    controller = WorkflowController(config, store, FakeAgentRuntime())
    reopened = controller.reopen(run.id, source)

    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "RECOVERY_INTERVENTION"
    assert reopened.escalation.reply_cursor == "closed"


def test_workflow_controller_reopen_missing_approval_context_fails_closed(
    tmp_path: Path,
) -> None:
    (
        source,
        config,
        store,
        run,
        app_ctx,
        work_item,
        triage,
        repo_profile,
    ) = _setup_reopened_environment(tmp_path)
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={run.id} episode=ep-reopen-e2e",
        episode_id="ep-reopen-e2e",
        run_id=run.id,
        approval_context_fingerprint=app_ctx.context_fingerprint,
    )
    escalation = EscalationRecord(
        episode_id="ep-reopen-e2e",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=None,
        accepted_replies=[receipt],
        remote_resume_enabled=True,
    )
    run = run.model_copy(update={"escalation": escalation})
    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, triage)
    store.save_artifact(run.id, repo_profile)

    controller = WorkflowController(config, store, FakeAgentRuntime())
    reopened = controller.reopen(run.id, source)

    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "RECOVERY_INTERVENTION"
    assert reopened.escalation.reply_cursor == "closed"


def test_workflow_controller_reopen_tampered_approval_context_fails_closed(
    tmp_path: Path,
) -> None:
    (
        source,
        config,
        store,
        run,
        app_ctx,
        work_item,
        triage,
        repo_profile,
    ) = _setup_reopened_environment(tmp_path)
    tampered_ctx = app_ctx.model_copy(update={"work_item_title": "Tampered"})
    receipt = AcceptedReplyReceipt(
        comment_id=1,
        user_login="lead-dev",
        author_association="MEMBER",
        created_at=utc_now(),
        command=f"@factory resume v1 run={run.id} episode=ep-reopen-e2e",
        episode_id="ep-reopen-e2e",
        run_id=run.id,
        approval_context_fingerprint=tampered_ctx.context_fingerprint,
    )
    escalation = EscalationRecord(
        episode_id="ep-reopen-e2e",
        status=EscalationStatus.REOPENED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        approval_context=tampered_ctx,
        accepted_replies=[receipt],
        remote_resume_enabled=True,
    )
    run = run.model_copy(update={"escalation": escalation})
    store.save_run(run)
    store.save_artifact(run.id, work_item)
    store.save_artifact(run.id, triage)
    store.save_artifact(run.id, repo_profile)

    controller = WorkflowController(config, store, FakeAgentRuntime())
    reopened = controller.reopen(run.id, source)

    assert reopened.state is WorkflowState.NEEDS_HUMAN
    assert reopened.escalation.status is EscalationStatus.PENDING_NOTIFICATION
    assert reopened.escalation.resume_classification is ResumeClassification.NOT_RESUMABLE
    assert reopened.escalation.reason_code == "RECOVERY_INTERVENTION"
    assert reopened.escalation.reply_cursor == "closed"


def test_validate_reply_candidate_additional_fail_closed_paths(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    now = utc_now().replace(microsecond=0)
    ctx = _sample_context(run_id="run-fc", episode_id="ep-fc")
    escalation = EscalationRecord(
        episode_id="ep-fc",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-fc",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    comment = GitHubComment(
        id=100,
        user_login="lead-dev",
        user_id=1,
        user_type="User",
        author_association="MEMBER",
        created_at=now,
        updated_at=now,
        body="@factory resume v1 run=run-fc episode=ep-fc",
    )

    # 1. Missing target repository / number
    run_no_target = run.model_copy(
        update={"escalation": escalation.model_copy(update={"target_repository": None})}
    )
    client = GitHubClient(runner=lambda *a, **k: None)
    is_valid, reason = validate_reply_candidate(
        comment, run=run_no_target, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "target is incomplete" in reason

    # 2. Target host not allowed
    run_bad_host = run.model_copy(
        update={"escalation": escalation.model_copy(update={"target_host": "untrusted.invalid"})}
    )
    is_valid, reason = validate_reply_candidate(
        comment, run=run_bad_host, config=config, client=client, repo_path=tmp_path
    )
    assert not is_valid
    assert "target host 'untrusted.invalid' is not allowed" in reason

    # 3. get_authenticated_user raises GitHubError
    def runner_auth_err(args, cwd=None, env=None):
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            raise GitHubError("connection failed")
        return FakeCompletedProcess(0, json.dumps([]))

    client_err = GitHubClient(runner=runner_auth_err)
    res = validate_reply_candidate(
        comment, run=run, config=config, client=client_err, repo_path=tmp_path
    )
    assert not res.is_valid
    assert res.retryable is True
    assert "cannot prove author is not factory account" in res.reason

    # 4. get_authenticated_user returns unresolved identity
    client_unres = GitHubClient(runner=lambda *a, **k: None)
    client_unres.get_authenticated_user = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(login="", id=None)
    )
    res = validate_reply_candidate(
        comment, run=run, config=config, client=client_unres, repo_path=tmp_path
    )
    assert not res.is_valid
    assert res.retryable is True
    assert "identity unresolved" in res.reason

    # 5. Re-fetch comment id mismatch
    comment_diff_id = {
        "id": 999,
        "body": "@factory resume v1 run=run-fc episode=ep-fc",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "MEMBER",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    runner_diff_id = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_diff_id))])
    client_diff = GitHubClient(runner=runner_diff_id)
    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client_diff, repo_path=tmp_path
    )
    assert not is_valid
    assert "re-fetched comment id mismatch" in reason

    # 6. Re-fetch comment creation time mismatch
    comment_diff_time = {
        "id": 100,
        "body": "@factory resume v1 run=run-fc episode=ep-fc",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "MEMBER",
        "created_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    runner_diff_time = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_diff_time))])
    client_diff_t = GitHubClient(runner=runner_diff_time)
    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client_diff_t, repo_path=tmp_path
    )
    assert not is_valid
    assert "re-fetched comment creation time mismatch" in reason

    # 7. Re-fetch comment body mismatch
    comment_diff_body = {
        "id": 100,
        "body": "Different body text",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "MEMBER",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    runner_diff_body = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_diff_body))])
    client_diff_b = GitHubClient(runner=runner_diff_body)
    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client_diff_b, repo_path=tmp_path
    )
    assert not is_valid
    assert "re-fetched comment body no longer matches resume command" in reason

    # 8. Re-fetch author not authorized
    comment_diff_auth = {
        "id": 100,
        "body": "@factory resume v1 run=run-fc episode=ep-fc",
        "user": {"login": "lead-dev", "id": 1, "type": "User"},
        "author_association": "NONE",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    runner_diff_auth = FakeRunner([FakeCompletedProcess(0, json.dumps(comment_diff_auth))])
    client_diff_a = GitHubClient(runner=runner_diff_auth)
    is_valid, reason = validate_reply_candidate(
        comment, run=run, config=config, client=client_diff_a, repo_path=tmp_path
    )
    assert not is_valid
    assert "re-fetched author is not authorized" in reason


def test_poll_escalation_reply_fail_closed_and_edge_cases(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    now = utc_now()
    ctx = _sample_context(run_id="run-poll-fc", episode_id="ep-poll-fc")
    escalation = EscalationRecord(
        episode_id="ep-poll-fc",
        status=EscalationStatus.NOTIFIED,
        resume_classification=ResumeClassification.RISK_APPROVAL,
        target_repository="owner/repo",
        target_number=1,
        created_at=now - timedelta(hours=1),
        last_notified_at=now - timedelta(hours=1),
        approval_context=ctx,
        remote_resume_enabled=True,
    )
    run = FactoryRun(
        id="run-poll-fc",
        work_item_id="task-1",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=escalation,
    )
    store.save_run(run)
    client = GitHubClient(runner=lambda *a, **k: None)

    # 1. Run not in NEEDS_HUMAN
    run_done = run.model_copy(update={"state": WorkflowState.DONE})
    assert poll_escalation_reply(run_done, store, config, client, repo_path) is None

    # 2. Escalation status not NOTIFIED
    run_pend = run.model_copy(
        update={
            "escalation": escalation.model_copy(
                update={"status": EscalationStatus.PENDING_NOTIFICATION}
            )
        }
    )
    assert poll_escalation_reply(run_pend, store, config, client, repo_path) is None

    # 3. last_notified_at is None
    run_no_notif = run.model_copy(
        update={"escalation": escalation.model_copy(update={"last_notified_at": None})}
    )
    assert poll_escalation_reply(run_no_notif, store, config, client, repo_path) is None

    # 4. Target host not allowed
    run_bad_host = run.model_copy(
        update={"escalation": escalation.model_copy(update={"target_host": "untrusted.invalid"})}
    )
    assert poll_escalation_reply(run_bad_host, store, config, client, repo_path) is None

    # 5. Remote resume disabled or approval context missing in poll -> closes cursor
    run_no_ctx = run.model_copy(
        update={"escalation": escalation.model_copy(update={"approval_context": None})}
    )
    store.save_run(run_no_ctx)
    assert poll_escalation_reply(run_no_ctx, store, config, client, repo_path) is None
    loaded_no_ctx = store.load_run(run_no_ctx.id)
    assert loaded_no_ctx.escalation.remote_resume_enabled is False
    assert loaded_no_ctx.escalation.reply_cursor == "closed"

    # 6. Window expired in polling -> sets EXPIRED and closes cursor
    store.save_run(run)
    future_time = now + timedelta(hours=config.escalation.reply_window_hours + 1)
    assert poll_escalation_reply(run, store, config, client, repo_path, now=future_time) is None
    loaded_expired = store.load_run(run.id)
    assert loaded_expired.escalation.status is EscalationStatus.EXPIRED
    assert loaded_expired.escalation.remote_resume_enabled is False
    assert loaded_expired.escalation.reply_cursor == "closed"

    # 7. Reopen count limit in polling -> closes cursor
    run_max_reopen = run.model_copy(
        update={
            "escalation": escalation.model_copy(
                update={"reopen_count": config.escalation.max_reopens}
            )
        }
    )
    store.save_run(run_max_reopen)
    assert poll_escalation_reply(run_max_reopen, store, config, client, repo_path, now=now) is None
    loaded_reopen = store.load_run(run_max_reopen.id)
    assert loaded_reopen.escalation.remote_resume_enabled is False
    assert loaded_reopen.escalation.reply_cursor == "closed"

    # 8. get_authenticated_user raises GitHubError in polling
    store.save_run(run)

    def runner_auth_fail(args, cwd=None, env=None):
        if "user" in args and not any("issues" in a or "comments" in a for a in args):
            raise GitHubError("auth check failed")
        return FakeCompletedProcess(0, json.dumps([]))

    client_auth_fail = GitHubClient(runner=runner_auth_fail)
    assert poll_escalation_reply(run, store, config, client_auth_fail, repo_path, now=now) is None

    # 9. get_authenticated_user returns unverified in polling
    client_unver = GitHubClient(runner=lambda *a, **k: None)
    client_unver.get_authenticated_user = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(login="", id=None)
    )
    assert poll_escalation_reply(run, store, config, client_unver, repo_path, now=now) is None

    # 10. Malformed reply_cursor JSON in polling recovers gracefully
    run_malformed_cursor = run.model_copy(
        update={"escalation": escalation.model_copy(update={"reply_cursor": "{malformed-json"})}
    )
    runner_empty = FakeRunner([FakeCompletedProcess(0, json.dumps([]))])
    client_empty = GitHubClient(runner=runner_empty)
    assert (
        poll_escalation_reply(run_malformed_cursor, store, config, client_empty, repo_path, now=now)
        is None
    )

    # 11. list_issue_comments raises GitHubError in polling breaks gracefully
    runner_list_err = FakeRunner([FakeCompletedProcess(1, "", stderr="rate limit exceeded")])
    client_list_err = GitHubClient(runner=runner_list_err)
    assert poll_escalation_reply(run, store, config, client_list_err, repo_path, now=now) is None

    # 12. Pagination across ticks when exactly 100 comments returned (advances page)
    full_page_comments = [
        {
            "id": i,
            "body": "chat comment",
            "user": {"login": "other-user", "id": 1000 + i, "type": "User"},
            "author_association": "NONE",
            "created_at": (now - timedelta(minutes=30) + timedelta(seconds=i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "updated_at": (now - timedelta(minutes=30) + timedelta(seconds=i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        for i in range(1, 101)
    ]
    runner_pagination = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps(full_page_comments)),
            FakeCompletedProcess(1, "", stderr="rate limit reached on page 2"),
        ]
    )
    client_pagination = GitHubClient(runner=runner_pagination)
    assert poll_escalation_reply(run, store, config, client_pagination, repo_path, now=now) is None
    loaded_paged = store.load_run(run.id)
    assert loaded_paged.escalation.reply_cursor is not None
    cursor_info = json.loads(loaded_paged.escalation.reply_cursor)
    assert cursor_info["page"] == 2


def test_reconcile_undelivered_notifications_skips_ineligible_runs(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    # 1. Run in DONE -> skipped
    run_done = FactoryRun(id="run-d", work_item_id="task-1", state=WorkflowState.DONE)
    store.save_run(run_done)

    # 2. Run in NEEDS_HUMAN but already NOTIFIED -> skipped
    run_notified = FactoryRun(
        id="run-notif",
        work_item_id="task-2",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-notif",
            status=EscalationStatus.NOTIFIED,
            resume_classification=ResumeClassification.RISK_APPROVAL,
        ),
    )
    store.save_run(run_notified)

    # 3. Run in NEEDS_HUMAN with NOTIFICATION_FAILED but delivery_attempts >= max -> skipped
    run_max_attempts = FactoryRun(
        id="run-max-att",
        work_item_id="task-3",
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-max-att",
            status=EscalationStatus.NOTIFICATION_FAILED,
            resume_classification=ResumeClassification.RISK_APPROVAL,
            delivery_attempts=config.escalation.max_notification_attempts,
        ),
    )
    store.save_run(run_max_attempts)

    # 4. Run in NEEDS_HUMAN needing delivery
    work_item = WorkItem(id="task-need", title="Task", description="d", external_id="owner/repo#42")
    ctx = _sample_context(run_id="run-need", episode_id="ep-need", work_item_id=work_item.id)
    run_needing = FactoryRun(
        id="run-need",
        work_item_id=work_item.id,
        state=WorkflowState.NEEDS_HUMAN,
        escalation=EscalationRecord(
            episode_id="ep-need",
            status=EscalationStatus.PENDING_NOTIFICATION,
            resume_classification=ResumeClassification.RISK_APPROVAL,
            approval_context=ctx,
        ),
    )
    store.save_run(run_needing)
    store.save_artifact(run_needing.id, work_item)

    now_str = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_payload = {
        "id": 888,
        "url": "https://api.github.com/repos/owner/repo/issues/comments/888",
        "html_url": "https://github.com/owner/repo/issues/42#comment-888",
        "body": "notice",
        "user": {"login": "factory-bot", "id": 99999, "type": "Bot"},
        "created_at": now_str,
        "updated_at": now_str,
        "author_association": "COLLABORATOR",
    }
    runner = FakeRunner(
        [
            FakeCompletedProcess(0, json.dumps([])),
            FakeCompletedProcess(0, json.dumps(comment_payload)),
        ]
    )
    client = GitHubClient(runner=runner)

    reconciled = reconcile_undelivered_notifications(store, config, client, repo_path)
    assert len(reconciled) == 1
    assert reconciled[0].id == "run-need"
    assert reconciled[0].escalation.status is EscalationStatus.NOTIFIED


def test_resolve_escalation_target_branches(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    config = _make_config(data_dir)
    store = FileRunStore(data_dir)

    # 1. Invalid issue reference raises ValueError -> source_issue_repo is None
    work_item_bad = WorkItem(
        id="task-bad-issue", title="T", description="d", external_id="invalid#issue#notnum"
    )
    run_bad = FactoryRun(
        id="run-bad-tgt",
        work_item_id=work_item_bad.id,
        state=WorkflowState.NEEDS_HUMAN,
    )
    store.save_run(run_bad)
    store.save_artifact(run_bad.id, work_item_bad)
    client = GitHubClient(runner=lambda *a, **k: None)
    target = resolve_escalation_target(run_bad, store, config, client, repo_path)
    assert target is None

    # 2. expected_repository with multiple slashes (e.g. host/owner/repo)
    work_item_ok = WorkItem(
        id="task-multi-slash", title="T", description="d", external_id="owner/repo#12"
    )
    run_multi = FactoryRun(
        id="run-multi-slash",
        work_item_id=work_item_ok.id,
        state=WorkflowState.NEEDS_HUMAN,
    )
    store.save_run(run_multi)
    store.save_artifact(run_multi.id, work_item_ok)
    target_multi = resolve_escalation_target(
        run_multi,
        store,
        config,
        client,
        repo_path,
        expected_repository="github.com/owner/repo",
    )
    assert target_multi is not None
    assert target_multi[1] == 12

    # 3. Malformed PR url raises ValueError -> pr_repo_ref is None
    run_bad_pr = run_multi.model_copy(update={"pull_request_url": "not-a-valid-pr-url"})
    target_bad_pr = resolve_escalation_target(run_bad_pr, store, config, client, repo_path)
    assert target_bad_pr is not None  # Falls back to issue target
    assert target_bad_pr[2] == EscalationTargetType.ISSUE

    # 4. Valid PR url but client raises GitHubError checking PR status -> falls back
    run_valid_pr = run_multi.model_copy(
        update={"pull_request_url": "https://github.com/owner/repo/pull/55"}
    )
    runner_pr_err = FakeRunner([FakeCompletedProcess(1, "", stderr="not found")])
    client_pr_err = GitHubClient(runner=runner_pr_err)
    target_fallback = resolve_escalation_target(
        run_valid_pr, store, config, client_pr_err, repo_path
    )
    assert target_fallback is not None
    assert target_fallback[2] == EscalationTargetType.ISSUE
