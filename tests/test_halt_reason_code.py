"""One HaltReasonCode enum ties the halt-reason copy tables together (#88)."""

from __future__ import annotations

import pytest

from software_agent_factory.dashboard.next_step import REASON_SENTENCES
from software_agent_factory.dashboard.sanitize import GUIDANCE_COPY
from software_agent_factory.escalation import classify_halt_reason
from software_agent_factory.models import FactoryRun, HaltReasonCode, WorkflowState
from software_agent_factory.observability import RunGuidance

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

HALT_REASONS = [
    "scope exceeded",
    "risk r2 requires human approval",
    "attempt budget exhausted",
    "CI checks failed",
    "could not publish the pull request",
    "workspace was abandoned",
    "something else",
]


def test_the_wire_values_do_not_change() -> None:
    assert {code.value for code in HaltReasonCode} == WIRE_VALUES


def test_every_code_has_guidance_copy_and_no_other_key_exists() -> None:
    assert set(GUIDANCE_COPY) == set(HaltReasonCode)


def test_every_halt_code_has_a_sentence_and_no_other_key_exists() -> None:
    assert set(REASON_SENTENCES) == set(HaltReasonCode) - NOT_A_HALT


@pytest.mark.parametrize("reason", HALT_REASONS)
def test_classify_halt_reason_returns_a_halt_reason_code(reason: str) -> None:
    run = FactoryRun(
        id="run-1", work_item_id="task-1", state=WorkflowState.NEEDS_HUMAN, failure_reason=reason
    )

    _, code, _, _ = classify_halt_reason(run)

    assert code in set(HaltReasonCode) - NOT_A_HALT
    assert type(code) is HaltReasonCode


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
