"""Tests for the resume rules shared by the GitHub poller and dashboard requests."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from factory_testing import REPLY_POLICY

from software_agent_factory import resume, resume_writes
from software_agent_factory.config import FactoryConfig, load_config
from software_agent_factory.escalation import parse_plan_decision_answers
from software_agent_factory.github import parse_comment_payload
from software_agent_factory.models import (
    DASHBOARD_USER_LOGIN,
    Complexity,
    DashboardResumeRequest,
    DeliveryRetryContext,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.resume import (
    MAX_PLAN_DECISION_ANSWER_CHARS,
    build_plan_answers,
    clean_plan_answer,
    compute_approval_context_fingerprint,
    compute_delivery_retry_context_fingerprint,
    compute_plan_decision_context_fingerprint,
    is_valid_plan_decision_answers,
    request_mismatch,
    resume_refusal,
    resume_refusal_within,
)
from software_agent_factory.resume_writes import (
    ReplyIdentity,
    accept_resume,
    ingest_dashboard_request,
)
from software_agent_factory.store import FileRunStore

# -- imports ------------------------------------------------------------------


def _imported_modules(module: ModuleType) -> set[str]:
    # The package __init__ imports subprocess, so a sys.modules check cannot tell.
    # Inspect the module's own imports instead.
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == _PACKAGE:
                # ``from software_agent_factory import x`` names the module ``...x``
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif node.level == 0:
                imported.add(node.module or "")
            elif node.module:
                imported.add(f".{node.module}")
            else:  # ``from . import x`` names the module ``.x``
                imported.update(f".{alias.name}" for alias in node.names)
    return imported


# The rules module docstring promises this exact list of package imports.
RULES_ALLOWED_PACKAGE_IMPORTS = {".models", ".config", ".escalation_protocol", ".redaction"}


_PACKAGE = "software_agent_factory"


def _package_modules(module: ModuleType) -> set[str]:
    """The module's package imports, relative or absolute, each written as ``.name``."""
    names: set[str] = set()
    for name in _imported_modules(module):
        if name.startswith("."):
            names.add(name)
        elif name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            names.add(name.removeprefix(_PACKAGE) or ".")
    return names


def test_the_rules_module_imports_only_its_documented_package_modules() -> None:
    assert _package_modules(resume) <= RULES_ALLOWED_PACKAGE_IMPORTS


def test_the_rules_module_imports_no_subprocess() -> None:
    assert "subprocess" not in _imported_modules(resume)


def test_the_writers_module_imports_no_github_subprocess_workflow_or_service() -> None:
    imported = _imported_modules(resume_writes) | _package_modules(resume_writes)

    forbidden = {
        "subprocess",
        ".github",
        ".workflow",
        ".service",
        ".escalation",
        ".store",
        ".dashboard",
        ".publishing",
        ".workspace",
        ".agents",
    }
    named = {
        name for name in imported if any(name == f or name.startswith(f + ".") for f in forbidden)
    }
    assert named == set()


def test_the_rules_module_does_not_import_the_writers() -> None:
    assert ".resume_writes" not in _package_modules(resume)


# -- answer rules ------------------------------------------------------------


def test_answer_at_the_character_limit_is_accepted_and_one_over_is_not() -> None:
    assert MAX_PLAN_DECISION_ANSWER_CHARS == 500
    assert clean_plan_answer("a" * 500) == "a" * 500
    assert clean_plan_answer("a" * 501) is None


def test_answer_is_trimmed_before_it_is_counted() -> None:
    assert clean_plan_answer("  " + "a" * 500 + "  ") == "a" * 500


@pytest.mark.parametrize("text", ["", "   ", "line one\nline two", "line one\rline two"])
def test_blank_and_multi_line_answers_are_rejected(text: str) -> None:
    assert clean_plan_answer(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "see https://example.com/x",
        "edit /etc/passwd",
        "token ghp_" + "a" * 20,
        "key sk-ant-" + "a" * 20,
        "clone ssh://bob:hunter22@example.com/repo",
        "set secret_key=abcdefgh1234",
    ],
)
def test_unsafe_answers_are_rejected(text: str) -> None:
    assert clean_plan_answer(text) is None


def test_build_numbers_one_answer_per_decision_in_order() -> None:
    answers = build_plan_answers(["Use JSON.", "Keep it local."], decision_count=2)

    assert answers is not None
    assert [(a.decision_number, a.answer) for a in answers] == [
        (1, "Use JSON."),
        (2, "Keep it local."),
    ]


@pytest.mark.parametrize("texts", [[], ["only one"], ["one", "two", "three"]])
def test_build_needs_exactly_one_answer_per_decision(texts: list[str]) -> None:
    assert build_plan_answers(texts, decision_count=2) is None


@pytest.mark.parametrize("count", [0, 25])
def test_build_rejects_a_decision_count_outside_the_protocol_range(count: int) -> None:
    assert build_plan_answers(["x"] * count, decision_count=count) is None


def test_build_rejects_the_whole_set_when_one_answer_breaks_a_rule() -> None:
    assert build_plan_answers(["fine", "a" * 501], decision_count=2) is None
    assert build_plan_answers(["fine", "two\nlines"], decision_count=2) is None


def _reply(*answers: str) -> str:
    lines = [f"{n}. {answer}" for n, answer in enumerate(answers, start=1)]
    return "\n".join(["@factory answer v1 run=r1 episode=ep-1", *lines])


def test_github_reply_follows_the_same_answer_rules() -> None:
    assert parse_plan_decision_answers(_reply("a" * 500), decision_count=1) is not None
    assert parse_plan_decision_answers(_reply("a" * 501), decision_count=1) is None


CREDENTIAL_ANSWERS = ["Authorization: admins only", "API_KEY=abcdefgh12345", "keep [REDACTED] here"]


@pytest.mark.parametrize("answer", CREDENTIAL_ANSWERS)
def test_dashboard_answer_with_a_credential_shape_is_refused(answer: str) -> None:
    assert clean_plan_answer(answer) is None


@pytest.mark.parametrize("answer", CREDENTIAL_ANSWERS)
def test_github_comment_with_a_credential_shape_is_refused_after_redaction(answer: str) -> None:
    comment = parse_comment_payload(
        {"id": 7, "body": _reply(answer), "created_at": "2026-10-01T12:00:00Z"}
    )

    assert parse_plan_decision_answers(comment.body, decision_count=1) is None


def test_github_reply_with_a_bare_carriage_return_is_ignored_not_a_crash() -> None:
    body = "@factory answer v1 run=r1 episode=ep-1\n1. first\rsecond"

    assert parse_plan_decision_answers(body, decision_count=1) is None


# -- fixtures ----------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RUN_ID = "run-resume"
EPISODE = "ep-resume"
OTHER_EPISODE = "ep-earlier"
OLD_EPISODE = "ep-old"
WORK_ITEM_ID = "task-1"
PLAN_FINGERPRINT = "a" * 64
DECISIONS = ["Pick a storage format.", "Pick a cache size."]


def _config(*, max_reopens: int = 3, window_hours: int = 24) -> FactoryConfig:
    config = load_config()
    return config.model_copy(
        update={
            "escalation": config.escalation.model_copy(
                update={"max_reopens": max_reopens, "reply_window_hours": window_hours}
            )
        }
    )


def _risk_context(run_id: str = RUN_ID, episode_id: str = EPISODE) -> RiskApprovalContext:
    rationale = RiskRationale(
        intended_outcome="Update the schema safely.",
        sensitive_boundary="Production database.",
        necessity="The work item migrates customer records.",
        credible_scenario="A bad migration could corrupt accounts.",
        known_mitigations=["Run the migration in one transaction."],
        residual_risk="A short table lock.",
    )
    decision = f"Approve advancing run {run_id} to REFINING."
    authorized = ["Move the run from NEEDS_HUMAN to REFINING."]
    unauthorized = ["Approval does not change the task scope."]
    conditions = ["Quality gates must pass before a pull request."]
    return RiskApprovalContext(
        risk=Risk.R2,
        complexity=Complexity.L1,
        work_item_id=WORK_ITEM_ID,
        work_item_title="Task",
        risk_rationale=rationale,
        decision_requested=decision,
        next_state=WorkflowState.REFINING,
        authorized_actions=authorized,
        unauthorized_actions=unauthorized,
        conditions_in_force=conditions,
        context_fingerprint=compute_approval_context_fingerprint(
            run_id=run_id,
            episode_id=episode_id,
            work_item_id=WORK_ITEM_ID,
            work_item_title="Task",
            risk=Risk.R2.value,
            complexity=Complexity.L1.value,
            rationale=rationale,
            decision_requested=decision,
            next_state=WorkflowState.REFINING.value,
            authorized_actions=authorized,
            unauthorized_actions=unauthorized,
            conditions_in_force=conditions,
        ),
    )


def _plan_context(run_id: str = RUN_ID, episode_id: str = EPISODE) -> PlanDecisionContext:
    return PlanDecisionContext(
        plan_fingerprint=PLAN_FINGERPRINT,
        decisions=DECISIONS,
        context_fingerprint=compute_plan_decision_context_fingerprint(
            run_id=run_id,
            episode_id=episode_id,
            plan_fingerprint=PLAN_FINGERPRINT,
            decisions=DECISIONS,
        ),
    )


def _retry_context(run_id: str = RUN_ID, episode_id: str = EPISODE) -> DeliveryRetryContext:
    return DeliveryRetryContext(
        reviewed_tree_sha="a" * 40,
        base_commit_sha="b" * 40,
        branch_name="factory/task",
        context_fingerprint=compute_delivery_retry_context_fingerprint(
            run_id=run_id,
            episode_id=episode_id,
            reviewed_tree_sha="a" * 40,
            base_commit_sha="b" * 40,
            branch_name="factory/task",
        ),
    )


def _run(
    kind: ResumeClassification = ResumeClassification.RISK_APPROVAL,
    *,
    state: WorkflowState = WorkflowState.NEEDS_HUMAN,
    **record: object,
) -> FactoryRun:
    fields: dict[str, object] = {
        "episode_id": EPISODE,
        "status": EscalationStatus.NOTIFIED,
        "resume_classification": kind,
        "created_at": NOW - timedelta(hours=1),
        "remote_resume_enabled": True,
    }
    if kind is ResumeClassification.RISK_APPROVAL:
        fields["approval_context"] = _risk_context()
    elif kind is ResumeClassification.PLAN_DECISION:
        fields["plan_decision_context"] = _plan_context()
    elif kind is ResumeClassification.DELIVERY_RETRY:
        fields["delivery_retry_context"] = _retry_context()
    escalation = EscalationRecord.model_validate({**fields, **record})
    return FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=state, escalation=escalation)


def _store(tmp_path: Path, run: FactoryRun) -> FileRunStore:
    store = FileRunStore(tmp_path)
    store.save_run(run)
    return store


def _fingerprint(run: FactoryRun) -> str:
    assert run.escalation is not None
    context = (
        run.escalation.approval_context
        or run.escalation.plan_decision_context
        or run.escalation.delivery_retry_context
    )
    assert context is not None
    return context.context_fingerprint


def _request(run: FactoryRun, **overrides: object) -> DashboardResumeRequest:
    assert run.escalation is not None
    kind = run.escalation.resume_classification
    fields: dict[str, object] = {
        "run_id": run.id,
        "episode_id": run.escalation.episode_id,
        "context_fingerprint": _fingerprint(run),
        "action": kind,
        "answers": (
            [PlanDecisionAnswer(decision_number=n, answer=f"Answer {n}.") for n in (1, 2)]
            if kind is ResumeClassification.PLAN_DECISION
            else []
        ),
        "created_at": NOW - timedelta(minutes=5),
    }
    return DashboardResumeRequest.model_validate({**fields, **overrides})


def _submit(store: FileRunStore, run: FactoryRun, **overrides: object) -> DashboardResumeRequest:
    request = _request(run, **overrides)
    assert store.create_dashboard_request(run.id, request) is True
    return request


def _stored_request(store: FileRunStore, run: FactoryRun) -> DashboardResumeRequest:
    assert run.escalation is not None
    request = store.load_dashboard_request(run.id, run.escalation.episode_id, _fingerprint(run))
    assert request is not None
    return request


# -- resume_refusal ----------------------------------------------------------

PLAN = ResumeClassification.PLAN_DECISION
RISK = ResumeClassification.RISK_APPROVAL
RETRY = ResumeClassification.DELIVERY_RETRY


@pytest.mark.parametrize("kind", [RISK, PLAN, RETRY])
@pytest.mark.parametrize(
    "status",
    [
        EscalationStatus.PENDING_NOTIFICATION,
        EscalationStatus.NOTIFIED,
        EscalationStatus.NOTIFICATION_FAILED,
    ],
)
def test_a_waiting_run_with_a_valid_context_can_accept_a_resume(
    kind: ResumeClassification, status: EscalationStatus
) -> None:
    run = _run(kind, status=status)

    assert resume_refusal(run, _config(), NOW) is None


def test_a_dashboard_resume_does_not_need_remote_resume_enabled() -> None:
    run = _run(remote_resume_enabled=False, reply_cursor="closed")

    assert resume_refusal(run, _config(), NOW) is None


@pytest.mark.parametrize(
    "status", [EscalationStatus.REOPENED, EscalationStatus.RESUMED, EscalationStatus.EXPIRED]
)
def test_a_run_that_no_longer_waits_has_a_changed_state(status: EscalationStatus) -> None:
    assert resume_refusal(_run(status=status), _config(), NOW) == "state_changed"


def test_a_run_outside_needs_human_has_a_changed_state() -> None:
    run = _run(state=WorkflowState.FAILED)

    assert resume_refusal(run, _config(), NOW) == "state_changed"


def test_a_run_without_an_escalation_has_a_changed_state() -> None:
    run = FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=WorkflowState.NEEDS_HUMAN)

    assert resume_refusal(run, _config(), NOW) == "state_changed"


def test_a_context_bound_to_another_episode_is_a_changed_context() -> None:
    run = _run(approval_context=_risk_context(episode_id="ep-other"))

    assert resume_refusal(run, _config(), NOW) == "context_changed"


def test_a_retry_context_bound_to_another_episode_is_a_changed_context() -> None:
    run = _run(RETRY, delivery_retry_context=_retry_context(episode_id="ep-other"))

    assert resume_refusal(run, _config(), NOW) == "context_changed"


def test_a_missing_context_is_a_changed_context() -> None:
    assert resume_refusal(_run(RETRY, delivery_retry_context=None), _config(), NOW) == (
        "context_changed"
    )
    assert resume_refusal(_run(approval_context=None), _config(), NOW) == "context_changed"
    assert resume_refusal(_run(PLAN, plan_decision_context=None), _config(), NOW) == (
        "context_changed"
    )


def test_a_halt_that_cannot_resume_is_a_changed_context() -> None:
    run = _run(ResumeClassification.NOT_RESUMABLE)

    assert resume_refusal(run, _config(), NOW) == "context_changed"


def test_the_reply_window_ends_after_its_last_moment() -> None:
    run = _run(created_at=NOW - timedelta(hours=24))

    assert resume_refusal(run, _config(window_hours=24), NOW) is None
    assert resume_refusal(run, _config(window_hours=24), NOW + timedelta(seconds=1)) == "expired"


@pytest.mark.parametrize(("reopens", "refusal"), [(2, None), (3, "reopen_limit")])
def test_the_reopen_limit_closes_at_its_count(reopens: int, refusal: str | None) -> None:
    run = _run(reopen_count=reopens)

    assert resume_refusal(run, _config(max_reopens=3), NOW) == refusal


def test_an_invalid_context_outranks_an_ended_window() -> None:
    run = _run(approval_context=None, created_at=NOW - timedelta(days=30))

    assert resume_refusal(run, _config(), NOW) == "context_changed"


# -- ingest ------------------------------------------------------------------


def test_a_risk_request_reopens_the_run_with_a_dashboard_receipt(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    request = _submit(store, run)

    receipt = ingest_dashboard_request(run, store, _config(), NOW)

    assert receipt is not None
    assert receipt.source == "dashboard"
    assert receipt.comment_id is None
    assert receipt.user_login == DASHBOARD_USER_LOGIN
    assert receipt.created_at == request.created_at
    assert receipt.accepted_at == NOW
    assert receipt.command == f"@factory resume v1 run={RUN_ID} episode={EPISODE}"
    assert receipt.approval_context_fingerprint == _fingerprint(run)
    saved = store.load_run(RUN_ID)
    assert saved.state is WorkflowState.NEEDS_HUMAN
    assert saved.escalation is not None
    assert saved.escalation.status is EscalationStatus.REOPENED
    assert saved.escalation.reopen_count == 1
    assert saved.escalation.reply_cursor == "closed"
    assert saved.escalation.accepted_replies == [receipt]
    assert saved.attempt_records == run.attempt_records
    assert _stored_request(store, run).status == "pending"


def test_a_retry_request_reopens_the_run_with_a_dashboard_receipt(tmp_path: Path) -> None:
    run = _run(RETRY)
    store = _store(tmp_path, run)
    _submit(store, run)

    receipt = ingest_dashboard_request(run, store, _config(), NOW)

    assert receipt is not None
    assert receipt.source == "dashboard"
    assert receipt.command == f"@factory resume v1 run={RUN_ID} episode={EPISODE}"
    assert receipt.delivery_retry_context_fingerprint == _fingerprint(run)
    assert (receipt.approval_context_fingerprint, receipt.plan_decision_context_fingerprint) == (
        None,
        None,
    )
    saved = store.load_run(RUN_ID)
    assert saved.escalation is not None
    assert saved.escalation.status is EscalationStatus.REOPENED
    assert saved.escalation.reopen_count == 1
    assert saved.escalation.accepted_replies == [receipt]
    assert _stored_request(store, run).status == "pending"


def test_a_retry_request_is_ingested_once(tmp_path: Path) -> None:
    run = _run(RETRY)
    store = _store(tmp_path, run)
    _submit(store, run)
    first = ingest_dashboard_request(run, store, _config(), NOW)

    assert first is not None
    again = store.load_run(RUN_ID)
    assert ingest_dashboard_request(again, store, _config(), NOW) is None
    assert again.escalation is not None
    assert again.escalation.reopen_count == 1


def test_a_plan_request_saves_the_answers_with_the_dashboard_source(tmp_path: Path) -> None:
    run = _run(PLAN)
    store = _store(tmp_path, run)
    _submit(store, run)

    receipt = ingest_dashboard_request(run, store, _config(), NOW)

    assert receipt is not None
    assert receipt.plan_decision_context_fingerprint == _fingerprint(run)
    assert receipt.command == f"@factory answer v1 run={RUN_ID} episode={EPISODE}"
    answers = store.load_artifact(RUN_ID, PlanDecisionAnswers)
    assert answers.source == "dashboard"
    assert answers.comment_id is None
    assert [a.answer for a in answers.answers] == ["Answer 1.", "Answer 2."]
    assert run.escalation is not None
    assert run.escalation.plan_decision_context is not None
    assert is_valid_plan_decision_answers(
        answers,
        run.escalation.plan_decision_context,
        run_id=RUN_ID,
        episode_id=EPISODE,
        receipt=receipt,
    )


def test_a_request_works_without_remote_resume(tmp_path: Path) -> None:
    run = _run(remote_resume_enabled=False, reply_cursor="closed")
    store = _store(tmp_path, run)
    _submit(store, run)

    assert ingest_dashboard_request(run, store, _config(), NOW) is not None


def test_no_request_means_nothing_happens(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None
    assert store.load_run(RUN_ID) == run


def test_a_run_without_an_escalation_is_left_alone(tmp_path: Path) -> None:
    run = FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=WorkflowState.NEEDS_HUMAN)
    store = _store(tmp_path, run)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None


@pytest.mark.parametrize("kind", [RISK, PLAN])
def test_an_accepted_request_stays_pending_and_is_not_ingested_twice(
    tmp_path: Path, kind: ResumeClassification
) -> None:
    run = _run(kind)
    store = _store(tmp_path, run)
    _submit(store, run)
    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    reopened = store.load_run(RUN_ID)

    assert ingest_dashboard_request(reopened, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == reopened
    assert _stored_request(store, run).status == "pending"


@pytest.mark.parametrize("kind", [RISK, PLAN])
def test_a_request_this_run_accepted_stays_pending_when_ingest_reads_a_stale_run(
    tmp_path: Path, kind: ResumeClassification
) -> None:
    run = _run(kind)
    store = _store(tmp_path, run)
    _submit(store, run)
    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    reopened = store.load_run(RUN_ID)

    # ``run`` is the snapshot read before the request was accepted.
    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == reopened
    assert _stored_request(store, run).status == "pending"


@pytest.mark.parametrize("kind", [RISK, PLAN])
def test_an_accepted_request_stays_pending_when_a_stale_run_is_read_after_the_window(
    tmp_path: Path, kind: ResumeClassification
) -> None:
    run = _run(kind)
    store = _store(tmp_path, run)
    _submit(store, run)
    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    reopened = store.load_run(RUN_ID)
    after_window = NOW + timedelta(hours=25)

    # ``run`` is the snapshot read before the request was accepted, now past the window.
    assert ingest_dashboard_request(run, store, _config(), after_window) is None

    assert store.load_run(RUN_ID) == reopened
    assert _stored_request(store, run).status == "pending"


def test_a_github_reply_accepted_first_makes_the_request_stale(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run)
    github = ReplyIdentity(
        source="github",
        comment_id=555,
        user_login="lead-dev",
        user_id=1001,
        author_association="MEMBER",
        created_at=NOW - timedelta(minutes=1),
    )
    accept_resume(run, store, _config(), reply=github, answers=None, now=NOW)
    after_github = store.load_run(RUN_ID)

    assert ingest_dashboard_request(after_github, store, _config(), NOW) is None

    saved = store.load_run(RUN_ID)
    assert saved == after_github
    assert saved.escalation is not None
    assert [r.source for r in saved.escalation.accepted_replies] == ["github"]
    assert saved.escalation.reopen_count == 1
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "state_changed")


def test_a_request_made_after_the_reply_window_ended_is_stale(tmp_path: Path) -> None:
    run = _run(created_at=NOW - timedelta(hours=25))  # the window ended an hour ago
    store = _store(tmp_path, run)
    _submit(store, run, created_at=NOW - timedelta(minutes=59))

    assert ingest_dashboard_request(run, store, _config(window_hours=24), NOW) is None

    assert store.load_run(RUN_ID) == run
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "expired")


def test_a_request_made_inside_the_reply_window_reopens_after_the_window_ended(
    tmp_path: Path,
) -> None:
    run = _run(created_at=NOW - timedelta(hours=25))  # the window ended an hour ago
    store = _store(tmp_path, run)
    _submit(store, run, created_at=NOW - timedelta(hours=2))

    receipt = ingest_dashboard_request(run, store, _config(window_hours=24), NOW)

    assert receipt is not None
    assert receipt.accepted_at == NOW
    assert _stored_request(store, run).status == "pending"


@pytest.mark.parametrize(
    "stamp",
    [
        NOW - timedelta(hours=1, seconds=1),  # before the escalation was made
        NOW + timedelta(seconds=1),  # after the service read it
    ],
)
def test_a_request_stamped_outside_the_episode_and_the_clock_is_stale(
    tmp_path: Path, stamp: datetime
) -> None:
    run = _run()  # the escalation was made one hour before NOW
    store = _store(tmp_path, run)
    _submit(store, run, created_at=stamp)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == run
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "expired")


@pytest.mark.parametrize("stamp", [NOW - timedelta(hours=1), NOW])
def test_a_request_stamped_at_the_edges_of_the_episode_and_the_clock_reopens(
    tmp_path: Path, stamp: datetime
) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run, created_at=stamp)

    assert ingest_dashboard_request(run, store, _config(), NOW) is not None


@pytest.mark.parametrize(
    ("reopens", "reopened"),
    [(2, True), (3, False)],
)
def test_the_reopen_limit_decides_whether_a_request_reopens(
    tmp_path: Path, reopens: int, reopened: bool
) -> None:
    run = _run(reopen_count=reopens)
    store = _store(tmp_path, run)
    _submit(store, run)

    receipt = ingest_dashboard_request(run, store, _config(max_reopens=3), NOW)

    assert (receipt is not None) is reopened
    request = _stored_request(store, run)
    assert request.status == ("pending" if reopened else "stale")
    assert request.reason == (None if reopened else "reopen_limit")
    saved = store.load_run(RUN_ID)
    assert saved.escalation is not None
    assert saved.escalation.reopen_count == (reopens + 1 if reopened else reopens)


def test_a_request_for_an_old_context_goes_stale_and_the_new_one_reopens(
    tmp_path: Path,
) -> None:
    run = _run()
    store = _store(tmp_path, run)
    old = _submit(store, run, context_fingerprint="f" * 64)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None
    stale = store.load_dashboard_request(RUN_ID, EPISODE, old.context_fingerprint)
    assert stale is not None
    assert (stale.status, stale.reason) == ("stale", "context_changed")
    assert store.load_run(RUN_ID) == run

    _submit(store, run)

    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    kept = store.load_dashboard_request(RUN_ID, EPISODE, old.context_fingerprint)
    assert kept is not None
    assert kept.status == "stale"


def test_a_request_file_whose_name_does_not_match_its_content_is_ignored(
    tmp_path: Path,
) -> None:
    run = _run()
    store = _store(tmp_path, run)
    forged = _request(run, context_fingerprint="f" * 64)
    path = store.runs_dir / RUN_ID / f"dashboard-approval-{EPISODE}-{_fingerprint(run)[:16]}.json"
    path.write_text(forged.model_dump_json(), encoding="utf-8")

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == run
    assert path.read_text(encoding="utf-8") == forged.model_dump_json()


def test_a_request_for_another_run_is_stale(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    foreign = _request(run, run_id="run-other")
    path = store.runs_dir / RUN_ID / f"dashboard-approval-{EPISODE}-{_fingerprint(run)[:16]}.json"
    path.write_text(foreign.model_dump_json(), encoding="utf-8")

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == run
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "context_changed")


def test_a_request_for_the_other_action_is_stale(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(
        store,
        run,
        action=PLAN,
        answers=[PlanDecisionAnswer(decision_number=1, answer="x")],
    )

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "context_changed")


def test_a_closed_window_outranks_a_request_for_the_other_action(tmp_path: Path) -> None:
    run = _run(created_at=NOW - timedelta(hours=25))
    store = _store(tmp_path, run)
    _submit(
        store,
        run,
        action=PLAN,
        answers=[PlanDecisionAnswer(decision_number=1, answer="x")],
    )

    assert ingest_dashboard_request(run, store, _config(window_hours=24), NOW) is None

    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "expired")


def test_every_request_of_a_run_that_cannot_resume_goes_stale(tmp_path: Path) -> None:
    run = _run(ResumeClassification.NOT_RESUMABLE)
    store = _store(tmp_path, run)
    request = _submit(store, _run(), context_fingerprint="e" * 64)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    stale = store.load_dashboard_request(RUN_ID, EPISODE, request.context_fingerprint)
    assert stale is not None
    assert (stale.status, stale.reason) == ("stale", "context_changed")
    assert store.load_run(RUN_ID) == run


def test_a_stale_request_is_never_read_again(tmp_path: Path) -> None:
    run = _run(created_at=NOW - timedelta(hours=25))
    store = _store(tmp_path, run)
    _submit(store, run, created_at=NOW - timedelta(minutes=30))
    assert ingest_dashboard_request(run, store, _config(window_hours=24), NOW) is None
    stale = _stored_request(store, run)

    assert ingest_dashboard_request(run, store, _config(window_hours=1000), NOW) is None

    assert _stored_request(store, run) == stale


@pytest.mark.parametrize(
    "answers",
    [
        [PlanDecisionAnswer(decision_number=1, answer="Only one.")],
        [
            PlanDecisionAnswer(decision_number=1, answer="Fine."),
            PlanDecisionAnswer(decision_number=2, answer="See https://example.com/x"),
        ],
    ],
    ids=["missing-answer", "unsafe-answer"],
)
def test_plan_answers_are_checked_again_before_a_run_reopens(
    tmp_path: Path, answers: list[PlanDecisionAnswer]
) -> None:
    run = _run(PLAN)
    store = _store(tmp_path, run)
    _submit(store, run, answers=answers)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == run
    with pytest.raises(FileNotFoundError):
        store.load_artifact(RUN_ID, PlanDecisionAnswers)
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "context_changed")


def test_a_pending_request_for_a_run_that_moved_on_goes_stale(tmp_path: Path) -> None:
    run = _run(state=WorkflowState.FAILED)
    store = _store(tmp_path, run)
    _submit(store, run)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "state_changed")


def test_a_request_of_another_episode_is_not_read(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    other = _request(run, episode_id=OTHER_EPISODE)
    store.create_dashboard_request(RUN_ID, other)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_dashboard_request(RUN_ID, OTHER_EPISODE, _fingerprint(run)) == other


def test_accept_resume_needs_an_escalation() -> None:
    run = FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=WorkflowState.NEEDS_HUMAN)
    reply = ReplyIdentity("github", 1, "lead-dev", None, "", NOW)
    store = FileRunStore(Path("unused"))
    config = _config()

    with pytest.raises(ValueError, match="no escalation"):
        accept_resume(run, store, config, reply=reply, answers=None, now=NOW)


def test_accept_resume_rejects_answers_without_a_plan_context(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    reply = ReplyIdentity("github", 1, "lead-dev", None, "", NOW)
    answers = [PlanDecisionAnswer(decision_number=1, answer="x")]
    config = _config()

    with pytest.raises(ValueError, match="no plan decision context"):
        accept_resume(run, store, config, reply=reply, answers=answers, now=NOW)

    assert store.load_run(RUN_ID) == run


# -- one reopen per episode, even from a stale snapshot ----------------------


def _github_reply() -> ReplyIdentity:
    return ReplyIdentity(
        source="github",
        comment_id=555,
        user_login="lead-dev",
        user_id=1001,
        author_association="MEMBER",
        created_at=NOW - timedelta(minutes=1),
    )


def test_a_github_reply_on_a_stale_snapshot_does_not_overwrite_an_ingested_request(
    tmp_path: Path,
) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run)
    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    after_dashboard = store.load_run(RUN_ID)

    # ``run`` is the snapshot read before the request was ingested.
    receipt = accept_resume(run, store, _config(), reply=_github_reply(), answers=None, now=NOW)

    assert receipt is None
    assert store.load_run(RUN_ID) == after_dashboard
    assert after_dashboard.escalation is not None
    assert [r.source for r in after_dashboard.escalation.accepted_replies] == ["dashboard"]
    assert after_dashboard.escalation.reopen_count == 1


def test_a_request_on_a_stale_snapshot_goes_stale_after_a_github_reply(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run)
    github_receipt = accept_resume(
        run, store, _config(), reply=_github_reply(), answers=None, now=NOW
    )
    assert github_receipt is not None
    after_github = store.load_run(RUN_ID)

    assert ingest_dashboard_request(run, store, _config(), NOW) is None

    assert store.load_run(RUN_ID) == after_github
    assert after_github.escalation is not None
    assert after_github.escalation.accepted_replies == [github_receipt]
    assert after_github.escalation.reopen_count == 1
    stale = _stored_request(store, run)
    assert (stale.status, stale.reason) == ("stale", "state_changed")


def test_a_github_reply_makes_every_pending_request_of_its_episode_stale(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run)
    _submit(store, run, context_fingerprint="f" * 64)
    elsewhere = _request(run, episode_id=OTHER_EPISODE)
    store.create_dashboard_request(RUN_ID, elsewhere)

    assert accept_resume(run, store, _config(), reply=_github_reply(), answers=None, now=NOW)

    assert [(r.status, r.reason) for r in store.list_dashboard_requests(RUN_ID, EPISODE)] == [
        ("stale", "state_changed")
    ] * 2
    assert store.load_dashboard_request(RUN_ID, OTHER_EPISODE, _fingerprint(run)) == elsewhere


def test_a_github_reply_that_is_refused_leaves_the_request_pending(tmp_path: Path) -> None:
    run = _run(created_at=NOW - timedelta(hours=25))
    store = _store(tmp_path, run)
    request = _submit(store, run, created_at=NOW - timedelta(hours=2))

    receipt = accept_resume(run, store, _config(), reply=_github_reply(), answers=None, now=NOW)

    assert receipt is None
    assert _stored_request(store, run) == request


def test_ingest_uses_the_listing_it_is_given(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    _submit(store, run)

    assert ingest_dashboard_request(run, store, _config(), NOW, requests=[]) is None
    assert store.load_run(RUN_ID) == run
    assert _stored_request(store, run).status == "pending"

    pending = store.list_dashboard_requests(RUN_ID, EPISODE)

    assert ingest_dashboard_request(run, store, _config(), NOW, requests=pending) is not None


def test_ingest_ignores_listed_requests_of_another_episode(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, run)
    earlier = _request(run, episode_id=OTHER_EPISODE)

    # Not on disk: marking it stale would raise, so it must not be touched.
    assert ingest_dashboard_request(run, store, _config(), NOW, requests=[earlier]) is None

    assert store.load_run(RUN_ID) == run


def test_plan_answers_of_a_stale_snapshot_do_not_replace_the_accepted_ones(
    tmp_path: Path,
) -> None:
    run = _run(PLAN)
    store = _store(tmp_path, run)
    _submit(store, run)
    assert ingest_dashboard_request(run, store, _config(), NOW) is not None
    late = [PlanDecisionAnswer(decision_number=n, answer=f"Late {n}.") for n in (1, 2)]

    receipt = accept_resume(run, store, _config(), reply=_github_reply(), answers=late, now=NOW)

    assert receipt is None
    saved = store.load_artifact(RUN_ID, PlanDecisionAnswers)
    assert saved.source == "dashboard"
    assert [a.answer for a in saved.answers] == ["Answer 1.", "Answer 2."]


def test_a_reply_checked_against_another_context_is_not_accepted(tmp_path: Path) -> None:
    seen = _run(PLAN)
    decisions = ["Pick another thing.", "Pick one more."]
    newer = _plan_context().model_copy(
        update={
            "decisions": decisions,
            "context_fingerprint": compute_plan_decision_context_fingerprint(
                run_id=RUN_ID,
                episode_id=EPISODE,
                plan_fingerprint=PLAN_FINGERPRINT,
                decisions=decisions,
            ),
        }
    )
    stored = _run(PLAN, plan_decision_context=newer)
    store = _store(tmp_path, stored)
    answers = [PlanDecisionAnswer(decision_number=n, answer=f"Answer {n}.") for n in (1, 2)]

    receipt = accept_resume(seen, store, _config(), reply=_github_reply(), answers=answers, now=NOW)

    assert receipt is None
    assert store.load_run(RUN_ID) == stored
    with pytest.raises(FileNotFoundError):
        store.load_artifact(RUN_ID, PlanDecisionAnswers)


# -- only the service writes -------------------------------------------------

_WRITE_FUNCTIONS = {
    "accept_resume",
    "ingest_dashboard_request",
    "replace_dashboard_request",
    "save_artifact",
    "save_run",
}


def test_the_dashboard_package_never_names_a_resume_write_function() -> None:
    dashboard = Path(resume.__file__).parent / "dashboard"
    sources = sorted(dashboard.rglob("*.py"))
    assert sources, "the dashboard package moved; update this test"
    named: dict[str, set[str]] = {}
    for path in sources:
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.alias):
                found.add(node.name.rsplit(".", maxsplit=1)[-1])
        if found & _WRITE_FUNCTIONS:
            named[path.name] = found & _WRITE_FUNCTIONS
    assert named == {}


def test_refusal_within_reads_the_window_and_the_reopen_limit_of_the_policy() -> None:
    run = _run(created_at=NOW - timedelta(hours=500), reopen_count=9)
    reopens_only = replace(REPLY_POLICY, reply_window_hours=1000)

    assert resume_refusal_within(run, REPLY_POLICY, NOW) == "expired"
    assert resume_refusal_within(run, reopens_only, NOW) == "reopen_limit"
    assert resume_refusal_within(run, replace(reopens_only, max_reopens=10), NOW) is None


@pytest.mark.parametrize(
    ("other_episode", "other_fingerprint", "other_action", "expected"),
    [
        pytest.param(False, False, False, None, id="all three match"),
        pytest.param(True, False, False, "episode", id="another episode"),
        pytest.param(False, True, False, "fingerprint", id="another fingerprint"),
        pytest.param(False, False, True, "action", id="another action"),
        pytest.param(True, True, False, "episode", id="episode before fingerprint"),
        pytest.param(False, True, True, "fingerprint", id="fingerprint before action"),
        pytest.param(True, True, True, "episode", id="episode before the rest"),
    ],
)
def test_a_request_mismatch_names_the_first_difference_in_the_documented_order(
    other_episode: bool, other_fingerprint: bool, other_action: bool, expected: str | None
) -> None:
    run = _run(RISK)
    assert run.escalation is not None

    mismatch = request_mismatch(
        run.escalation,
        OLD_EPISODE if other_episode else EPISODE,
        "b" * 64 if other_fingerprint else _fingerprint(run),
        PLAN if other_action else RISK,
    )

    assert mismatch == expected


def _request_refusal_of(run: FactoryRun, **overrides: Any) -> str | None:
    fields: dict[str, Any] = {
        "episode_id": EPISODE,
        "action": RISK,
        "policy": REPLY_POLICY,
        "now": NOW,
        **overrides,
    }
    if "fingerprint" not in fields:
        fields["fingerprint"] = _fingerprint(run)
    return resume.request_refusal(run, **fields)


@pytest.mark.parametrize(
    ("run", "overrides", "expected"),
    [
        pytest.param(_run(RISK), {}, None, id="a request the service would take"),
        pytest.param(_run(RISK, status=EscalationStatus.REOPENED), {}, "state_changed", id="state"),
        pytest.param(
            _run(RISK, state=WorkflowState.IMPLEMENTING),
            {"episode_id": OLD_EPISODE},
            "state_changed",
            id="state before the episode",
        ),
        pytest.param(
            _run(RISK, approval_context=None),
            {"fingerprint": "b" * 64},
            "context_changed",
            id="context before the fingerprint",
        ),
        pytest.param(
            _run(RISK, created_at=NOW - timedelta(hours=25), reopen_count=3),
            {},
            "expired",
            id="window before the reopen limit",
        ),
        pytest.param(
            _run(RISK, created_at=NOW - timedelta(hours=25)),
            {"episode_id": OLD_EPISODE},
            "expired",
            id="window before the episode",
        ),
        pytest.param(
            _run(RISK, reopen_count=3),
            {"action": PLAN},
            "reopen_limit",
            id="reopen limit before the action",
        ),
        pytest.param(_run(RISK), {"episode_id": OLD_EPISODE}, "episode", id="episode"),
        pytest.param(_run(RISK), {"fingerprint": "b" * 64}, "fingerprint", id="fingerprint"),
        pytest.param(_run(RISK), {"action": PLAN}, "action", id="action"),
    ],
)
def test_a_request_refusal_names_one_reason_in_the_service_order(
    run: FactoryRun, overrides: dict[str, Any], expected: str | None
) -> None:
    assert _request_refusal_of(run, **overrides) == expected
