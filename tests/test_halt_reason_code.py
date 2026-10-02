"""One HaltReasonCode enum ties the halt-reason copy tables together (#88)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from software_agent_factory.dashboard.next_step import REASON_SENTENCES
from software_agent_factory.dashboard.sanitize import sanitize_run_detail
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.models import (
    HALT_REASON_COPY,
    UNRESOLVED_DECISIONS_HALT_REASON,
    UNRESOLVED_DECISIONS_REPLACE_ACTION,
    EscalationRecord,
    EscalationStatus,
    ExecutionPlan,
    ExpectedScope,
    FactoryRun,
    HaltReasonCode,
    ResumeClassification,
    ReviewImpasse,
    WorkflowState,
    unresolved_decisions_count,
    unresolved_decisions_summary,
)
from software_agent_factory.observability import RunGuidance, _build_run_guidance
from software_agent_factory.store import FileRunStore

#: A run that is not halted still carries guidance, so this code has no halt sentence.
NOT_A_HALT = {HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE}

#: The strings stored in run files and sent to the page. A rename here breaks both.
WIRE_VALUES = {
    "BOUNDED_REVIEW_ACCEPTANCE",
    "REVIEW_IMPASSE",
    "UNRESOLVED_DECISIONS",
    "RISK_APPROVAL",
    "SCOPE_REVIEW",
    "ATTEMPT_BUDGET_EXHAUSTED",
    "CI_INTERVENTION",
    "DELIVERY_INTERVENTION",
    "RECOVERY_INTERVENTION",
    "MANUAL_INSPECTION",
}

#: One failure reason for each code a failure reason alone selects, and the code it selects.
#: REVIEW_IMPASSE comes from a stored artifact and BOUNDED_REVIEW_ACCEPTANCE is not a halt.
CLASSIFIED_REASONS = [
    (UNRESOLVED_DECISIONS_HALT_REASON, HaltReasonCode.UNRESOLVED_DECISIONS),
    ("scope exceeded", HaltReasonCode.SCOPE_REVIEW),
    ("risk r2 requires human approval", HaltReasonCode.RISK_APPROVAL),
    ("attempt budget exhausted", HaltReasonCode.ATTEMPT_BUDGET_EXHAUSTED),
    ("CI checks failed", HaltReasonCode.CI_INTERVENTION),
    ("could not publish the pull request", HaltReasonCode.DELIVERY_INTERVENTION),
    ("workspace was abandoned", HaltReasonCode.RECOVERY_INTERVENTION),
    ("something else", HaltReasonCode.MANUAL_INSPECTION),
]
COPIED_REASONS = [
    case for case in CLASSIFIED_REASONS if case[1] is not HaltReasonCode.UNRESOLVED_DECISIONS
]

#: The GitHub notice is posted on the thread it asks people to reply on, so it says
#: "this GitHub thread". The dashboard and the run view say "the escalation thread".
GITHUB_REPLY_ACTION = "Reply with complete numbered decisions on this GitHub thread."


def _halted(reason: str) -> FactoryRun:
    return FactoryRun(
        id="run-1", work_item_id="task-1", state=WorkflowState.NEEDS_HUMAN, failure_reason=reason
    )


def _impasse() -> ReviewImpasse:
    return ReviewImpasse(snapshot=1, reason="no progress", finding_ids=["review-correctness-1"])


def test_the_wire_values_do_not_change() -> None:
    assert {code.value for code in HaltReasonCode} == WIRE_VALUES


def test_every_code_has_copy_and_no_other_key_exists() -> None:
    assert set(HALT_REASON_COPY) == set(HaltReasonCode)


def test_every_halt_code_has_a_sentence_and_no_other_key_exists() -> None:
    assert set(REASON_SENTENCES) == set(HaltReasonCode) - NOT_A_HALT


def test_a_failure_reason_and_a_stored_impasse_select_every_halt_code() -> None:
    reason_codes = {code for _, code in CLASSIFIED_REASONS}

    assert reason_codes | {HaltReasonCode.REVIEW_IMPASSE} == set(HaltReasonCode) - NOT_A_HALT


@pytest.mark.parametrize(("reason", "code"), CLASSIFIED_REASONS)
def test_classify_halt_reason_returns_the_exact_code(reason: str, code: HaltReasonCode) -> None:
    _, actual, _, _ = classify_halt_reason(_halted(reason))

    assert actual is code


@pytest.mark.parametrize(("reason", "code"), COPIED_REASONS)
def test_classify_halt_reason_reads_the_copy_table(reason: str, code: HaltReasonCode) -> None:
    _, _, summary, next_action = classify_halt_reason(_halted(reason))

    actual = (summary, next_action)
    assert actual == HALT_REASON_COPY[code]


def test_classify_halt_reason_names_the_github_thread_for_plan_decisions() -> None:
    classification, _, summary, next_action = classify_halt_reason(
        _halted(UNRESOLVED_DECISIONS_HALT_REASON)
    )

    assert classification is ResumeClassification.PLAN_DECISION
    assert summary == HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS].summary
    assert next_action == GITHUB_REPLY_ACTION


def test_classify_halt_reason_reports_a_stored_review_impasse(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _halted("review failed to converge")
    store.save_run(run)
    store.save_artifact(run.id, _impasse())

    classification, code, summary, next_action = classify_halt_reason(run, store)

    assert classification is ResumeClassification.NOT_RESUMABLE
    assert code is HaltReasonCode.REVIEW_IMPASSE
    actual = (summary, next_action)
    assert actual == HALT_REASON_COPY[HaltReasonCode.REVIEW_IMPASSE]


@pytest.mark.parametrize(("reason", "code"), COPIED_REASONS)
def test_run_guidance_reads_the_copy_table(
    tmp_path: Path, reason: str, code: HaltReasonCode
) -> None:
    guidance = _build_run_guidance(FileRunStore(tmp_path), _halted(reason))

    assert guidance is not None
    actual = (guidance.reason_code, guidance.summary, guidance.next_action)
    assert actual == (code, *HALT_REASON_COPY[code])


def test_run_guidance_reads_the_copy_table_for_a_stored_review_impasse(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _halted("review failed to converge")
    store.save_run(run)
    store.save_artifact(run.id, _impasse())

    guidance = _build_run_guidance(store, run)

    assert guidance is not None
    actual = (guidance.summary, guidance.next_action)
    assert actual == HALT_REASON_COPY[HaltReasonCode.REVIEW_IMPASSE]


def test_run_guidance_offers_a_replacement_run_when_no_reply_can_resume(tmp_path: Path) -> None:
    guidance = _build_run_guidance(
        FileRunStore(tmp_path), _halted(UNRESOLVED_DECISIONS_HALT_REASON)
    )

    assert guidance is not None
    assert guidance.summary == HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS].summary
    assert guidance.next_action == UNRESOLVED_DECISIONS_REPLACE_ACTION


def test_run_guidance_asks_for_a_reply_when_a_reply_can_resume_the_run(tmp_path: Path) -> None:
    run = _halted(UNRESOLVED_DECISIONS_HALT_REASON).model_copy(
        update={
            "escalation": EscalationRecord(
                episode_id="ep-1",
                status=EscalationStatus.NOTIFIED,
                resume_classification=ResumeClassification.PLAN_DECISION,
                remote_resume_enabled=True,
            )
        }
    )

    guidance = _build_run_guidance(FileRunStore(tmp_path), run)

    assert guidance is not None
    assert guidance.next_action == HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS].next_action


def _plan(*decisions: str) -> ExecutionPlan:
    return ExecutionPlan(
        summary="s",
        steps=[],
        expected_scope=ExpectedScope(modules=["src"], estimated_files_min=1, estimated_files_max=2),
        unresolved_decisions=list(decisions),
    )


@pytest.mark.parametrize(
    ("plan", "reason", "expected"),
    [
        (_plan("a?", "b?"), "execution plan has unresolved decisions (7)", 2),
        (_plan(), "execution plan has 3 unresolved decisions", 3),
        (None, "execution plan has unresolved decisions (4)", 4),
        (None, "execution plan has unresolved decisions", None),
    ],
)
def test_the_unresolved_count_is_the_plans_own_else_the_one_in_the_reason(
    plan: ExecutionPlan | None, reason: str, expected: int | None
) -> None:
    assert unresolved_decisions_count(plan, reason) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (None, "The execution plan has unresolved architectural decisions."),
        (1, "The execution plan has 1 unresolved architectural decision."),
        (2, "The execution plan has 2 unresolved architectural decisions."),
    ],
)
def test_the_unresolved_summary_counts_when_it_can(count: int | None, expected: str) -> None:
    assert unresolved_decisions_summary(count) == expected


def _sanitized_guidance(**fields: object) -> Any:
    return sanitize_run_detail({"run_id": "run-1", "guidance": fields})["guidance"]


@pytest.mark.parametrize(
    "code", sorted(set(HaltReasonCode) - {HaltReasonCode.UNRESOLVED_DECISIONS})
)
def test_dashboard_guidance_reads_the_copy_table(code: HaltReasonCode) -> None:
    guidance = _sanitized_guidance(reason_code=code.value)

    actual = (guidance["summary"], guidance["next_action"])
    assert actual == HALT_REASON_COPY[code]


def test_dashboard_guidance_keeps_the_reply_action_the_table_holds() -> None:
    table = HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS]

    guidance = _sanitized_guidance(
        reason_code="UNRESOLVED_DECISIONS", next_action=table.next_action
    )

    actual = (guidance["summary"], guidance["next_action"])
    assert actual == table


@pytest.mark.parametrize("sent", [None, "anything else", UNRESOLVED_DECISIONS_REPLACE_ACTION])
def test_dashboard_guidance_offers_a_replacement_run_for_any_other_action(
    sent: str | None,
) -> None:
    guidance = _sanitized_guidance(reason_code="UNRESOLVED_DECISIONS", next_action=sent)

    assert guidance["next_action"] == UNRESOLVED_DECISIONS_REPLACE_ACTION


def test_guidance_serializes_its_code_as_the_plain_string() -> None:
    guidance = RunGuidance(
        status="ACTION_REQUIRED",
        reason_code=HaltReasonCode.CI_INTERVENTION,
        summary="s",
        next_action="a",
    )

    assert guidance.model_dump_json().count('"reason_code":"CI_INTERVENTION"') == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("RISK_APPROVAL", HaltReasonCode.RISK_APPROVAL),
        ("risk_approval", None),
        ("", None),
        (None, None),
        (["RISK_APPROVAL"], None),
    ],
)
def test_parse_returns_the_code_or_none(value: object, expected: HaltReasonCode | None) -> None:
    assert HaltReasonCode.parse(value) is expected
