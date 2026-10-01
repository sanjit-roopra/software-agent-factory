"""The "Needs you" view model (slice 2, step 2.4 of #80)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from software_agent_factory.dashboard.next_step import (
    FALLBACK_SENTENCE,
    REASON_SENTENCES,
    next_step,
)
from software_agent_factory.dashboard.sanitize import GUIDANCE_COPY, sanitize_run_detail
from software_agent_factory.dashboard.view import run_detail_view
from software_agent_factory.escalation import parse_plan_decision_answers, parse_resume_command
from software_agent_factory.models import (
    Complexity,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.observability import build_run_detail
from software_agent_factory.store import FileRunStore

RUN_ID = "run-20261001-abc"
EPISODE_ID = "ep-0123456789abcdef01234567"
FINGERPRINT = "f" * 64
SECRET = "GH_TOKEN=ghp_abcdefgh12345678"

SCOPE: dict[str, Any] = {
    "decision_requested": "Approve advancing the run to REFINING.",
    "authorized_actions": ["Refine the requirements.", "Run agents in a workspace."],
    "unauthorized_actions": ["Approval does not change scope."],
    "conditions_in_force": ["Quality gates must pass."],
}


def _run(
    state: str = "NEEDS_HUMAN",
    *,
    reason_code: str | None = "RISK_APPROVAL",
    resume_classification: str | None = "RISK_APPROVAL",
    failure_reason: str | None = None,
    **escalation: Any,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "run_id": RUN_ID,
        "state": state,
        "failure_reason": failure_reason,
        "failure_reason_truncated": False,
    }
    if resume_classification is not None:
        run["escalation"] = {
            "status": "NOTIFIED",
            "reason_code": reason_code,
            "resume_classification": resume_classification,
            "episode_id": EPISODE_ID,
            "context_fingerprint": FINGERPRINT,
            "reopen_count": 0,
            "reopen_max": 3,
            **escalation,
        }
    return run


def _risk_run(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("approval_scope", SCOPE)
    return _run(**kwargs)


def _plan_run(decisions: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("reason_code", "UNRESOLVED_DECISIONS")
    kwargs.setdefault(
        "decisions", ["Use SQLite?", "Keep the old API?"] if decisions is None else decisions
    )
    return _run(resume_classification="PLAN_DECISION", **kwargs)


def test_risk_approval_halt_gives_approve_with_scope_reopens_and_reply() -> None:
    step = next_step(_risk_run())

    assert step["kind"] == "approve"
    assert step["sentence"] == REASON_SENTENCES["RISK_APPROVAL"]
    assert step["resume_classification"] == "RISK_APPROVAL"
    assert step["approval_scope"] == {
        "decision_requested": "Approve advancing the run to REFINING.",
        "authorized_actions": ["Refine the requirements.", "Run agents in a workspace."],
        "unauthorized_actions": ["Approval does not change scope."],
        "conditions_in_force": ["Quality gates must pass."],
    }
    assert step["decisions"] == []
    assert (step["reopens_used"], step["reopens_max"]) == (0, 3)
    assert step["episode_id"] == EPISODE_ID
    assert step["context_fingerprint"] == FINGERPRINT
    assert step["reply_text"] == f"@factory resume v1 run={RUN_ID} episode={EPISODE_ID}"


def test_approve_reply_text_round_trips_through_the_resume_parser() -> None:
    reply = next_step(_risk_run())["reply_text"]

    assert parse_resume_command(reply) == (RUN_ID, EPISODE_ID)


def test_plan_decision_halt_gives_answer_with_numbered_decisions() -> None:
    step = next_step(_plan_run())

    assert step["kind"] == "answer"
    assert step["sentence"] == REASON_SENTENCES["UNRESOLVED_DECISIONS"]
    assert step["resume_classification"] == "PLAN_DECISION"
    assert step["decisions"] == [
        {"number": 1, "question": "Use SQLite?"},
        {"number": 2, "question": "Keep the old API?"},
    ]
    assert step["approval_scope"] is None
    assert step["reply_text"] == (
        f"@factory answer v1 run={RUN_ID} episode={EPISODE_ID}\n1. <answer>\n2. <answer>"
    )


def test_answer_template_round_trips_through_the_answer_parser() -> None:
    reply = next_step(_plan_run())["reply_text"]

    parsed = parse_plan_decision_answers(reply, decision_count=2)

    assert parsed is not None
    run_id, episode_id, answers = parsed
    assert (run_id, episode_id) == (RUN_ID, EPISODE_ID)
    assert [(a.decision_number, a.answer) for a in answers] == [(1, "<answer>"), (2, "<answer>")]


@pytest.mark.parametrize(
    ("state", "resume_classification", "reason_code"),
    [
        ("NEEDS_HUMAN", "NOT_RESUMABLE", "SCOPE_REVIEW"),
        ("FAILED", None, None),
    ],
)
def test_runs_that_cannot_continue_say_so_and_show_the_failure_reason(
    state: str, resume_classification: str | None, reason_code: str | None
) -> None:
    step = next_step(
        _run(
            state,
            reason_code=reason_code,
            resume_classification=resume_classification,
            failure_reason="scope drift in src/app.py",
        )
    )

    assert step["kind"] == "cannot_continue"
    assert step["sentence"].endswith("This run cannot continue.")
    assert step["failure_reason"] == "scope drift in src/app.py"
    assert step["failure_reason_truncated"] is False
    assert step["reply_text"] is None


def test_failed_run_sentence_names_the_failure() -> None:
    step = next_step(_run("FAILED", resume_classification=None, failure_reason="boom"))

    assert step["sentence"] == "The run failed. This run cannot continue."
    assert step["resume_classification"] is None


def test_cannot_continue_failure_reason_is_redacted_and_keeps_the_truncated_flag() -> None:
    run = _run("FAILED", resume_classification=None, failure_reason=f"x {SECRET}")
    run["failure_reason_truncated"] = True

    step = next_step(run)

    assert step["failure_reason"] == "x [REDACTED]"
    assert step["failure_reason_truncated"] is True


def test_needs_human_without_an_escalation_record_cannot_continue() -> None:
    step = next_step(_run(resume_classification=None, failure_reason="stopped"))

    assert step["kind"] == "cannot_continue"
    assert step["failure_reason"] == "stopped"


@pytest.mark.parametrize("scope", [None, {}, {**SCOPE, "authorized_actions": "not a list"}])
def test_risk_approval_without_a_valid_approval_context_is_unavailable(scope: Any) -> None:
    step = next_step(_run(approval_scope=scope))

    assert step["kind"] == "remote_approval_unavailable"
    assert step["sentence"] == (
        f"{REASON_SENTENCES['RISK_APPROVAL']} Remote approval is not available."
        f" Inspect the run with `factory show {RUN_ID}`."
    )
    assert step["reply_text"] is None
    assert step["approval_scope"] is None


def test_plan_decision_without_decisions_is_unavailable() -> None:
    step = next_step(_plan_run(decisions=[]))

    assert step["kind"] == "remote_approval_unavailable"
    assert "Remote answers are not available." in step["sentence"]
    assert f"`factory show {RUN_ID}`" in step["sentence"]
    assert step["decisions"] == []


@pytest.mark.parametrize("decisions", ["not a list", [7], ["fine", ""]])
def test_malformed_decisions_are_unavailable(decisions: Any) -> None:
    assert next_step(_plan_run(decisions=decisions))["kind"] == "remote_approval_unavailable"


def test_more_decisions_than_the_answer_parser_accepts_are_unavailable() -> None:
    step = next_step(_plan_run(decisions=[f"Question {n}?" for n in range(25)]))

    assert step["kind"] == "remote_approval_unavailable"


@pytest.mark.parametrize(
    "override",
    [
        {"episode_id": None},
        {"episode_id": "ep one"},
        {"context_fingerprint": None},
        {"context_fingerprint": "short"},
    ],
)
def test_an_unsafe_or_missing_episode_or_fingerprint_makes_the_reply_unavailable(
    override: dict[str, Any],
) -> None:
    assert next_step(_risk_run(**override))["kind"] == "remote_approval_unavailable"
    assert next_step(_plan_run(**override))["kind"] == "remote_approval_unavailable"


@pytest.mark.parametrize("run_id", ["run one", "run.one"])
def test_a_run_id_the_dashboard_route_would_reject_makes_the_reply_unavailable(
    run_id: str,
) -> None:
    run = _risk_run()
    run["run_id"] = run_id

    step = next_step(run)

    assert step["kind"] == "remote_approval_unavailable"
    assert "`factory show <run>`" in step["sentence"]


@pytest.mark.parametrize(
    ("override", "cause"),
    [
        ({"status": "PENDING_NOTIFICATION"}, "the notice is not sent yet"),
        ({"status": "NOTIFICATION_FAILED"}, "the notice was not sent"),
        ({"status": "EXPIRED"}, "the reply window expired"),
        ({"status": "REOPENED"}, "the run already resumed from a reply"),
        ({"status": "RESUMED"}, "the run already resumed from a reply"),
        ({"status": None}, "the notice status is not known"),
        ({"status": ["NOTIFIED"]}, "the notice status is not known"),
        ({"reopen_count": 3, "reopen_max": 3}, "the reopen limit is reached"),
        ({"reopen_count": 4, "reopen_max": 3}, "the reopen limit is reached"),
    ],
    ids=[
        "pending",
        "notification-failed",
        "expired",
        "reopened",
        "resumed",
        "no-status",
        "malformed-status",
        "reopen-limit-reached",
        "reopen-limit-passed",
    ],
)
def test_a_reply_the_poller_would_ignore_is_unavailable_and_says_why(
    override: dict[str, Any], cause: str
) -> None:
    approve = next_step(_risk_run(**override))
    answer = next_step(_plan_run(**override))

    assert approve["kind"] == answer["kind"] == "remote_approval_unavailable"
    assert f"Remote approval is not available because {cause}." in approve["sentence"]
    assert f"Remote answers are not available because {cause}." in answer["sentence"]
    assert approve["reply_text"] is answer["reply_text"] is None


@pytest.mark.parametrize(
    "override",
    [
        {"reopen_count": 2, "reopen_max": 3},
        {"reopen_count": None, "reopen_max": 3},
        {"reopen_count": 5, "reopen_max": None},
    ],
    ids=["reopens-left", "no-reopen-count", "no-reopen-limit"],
)
def test_a_notified_run_with_reopens_left_or_unknown_can_still_be_replied_to(
    override: dict[str, Any],
) -> None:
    assert next_step(_risk_run(**override))["kind"] == "approve"
    assert next_step(_plan_run(**override))["kind"] == "answer"


@pytest.mark.parametrize(
    "state", ["CREATED", "IMPLEMENTING", "REFINING", "PR_READY", "DONE", "CANCELLED"]
)
def test_active_and_finished_runs_have_no_next_step(state: str) -> None:
    step = next_step(_run(state))

    assert step["kind"] == "none"
    assert step["sentence"] is None
    assert step["reply_text"] is None
    assert step["decisions"] == []


def test_a_run_without_a_state_has_no_next_step() -> None:
    assert next_step({})["kind"] == "none"


@pytest.mark.parametrize("code", sorted(REASON_SENTENCES))
def test_each_reason_code_has_its_own_plain_sentence(code: str) -> None:
    step = next_step(_run(reason_code=code, resume_classification="NOT_RESUMABLE"))

    assert step["sentence"] == f"{REASON_SENTENCES[code]} This run cannot continue."
    assert REASON_SENTENCES[code].startswith("The run ")
    assert len(set(REASON_SENTENCES.values())) == len(REASON_SENTENCES)


def test_every_reason_sentence_belongs_to_a_known_reason_code() -> None:
    assert set(REASON_SENTENCES) <= set(GUIDANCE_COPY)
    assert set(GUIDANCE_COPY) - set(REASON_SENTENCES) == {"BOUNDED_REVIEW_ACCEPTANCE"}


@pytest.mark.parametrize("code", ["SOMETHING_NEW", "", None])
def test_an_unknown_reason_code_gets_the_fallback_sentence(code: str | None) -> None:
    step = next_step(_run(reason_code=code, resume_classification="NOT_RESUMABLE"))

    assert step["sentence"] == f"{FALLBACK_SENTENCE} This run cannot continue."


def test_secret_in_the_approval_scope_is_redacted() -> None:
    scope = {
        "decision_requested": f"Approve {SECRET}",
        "authorized_actions": [f"Use {SECRET}"],
        "unauthorized_actions": [f"No {SECRET}"],
        "conditions_in_force": [f"Keep {SECRET}"],
    }

    step = next_step(_risk_run(approval_scope=scope))

    assert "ghp_abcdefgh12345678" not in repr(step)
    assert step["approval_scope"]["decision_requested"] == "Approve [REDACTED]"
    assert step["approval_scope"]["authorized_actions"] == ["Use [REDACTED]"]
    assert step["approval_scope"]["unauthorized_actions"] == ["No [REDACTED]"]
    assert step["approval_scope"]["conditions_in_force"] == ["Keep [REDACTED]"]


def test_secret_in_a_decision_question_is_redacted() -> None:
    step = next_step(_plan_run(decisions=[f"Which {SECRET} ?", "Fine?"]))

    assert "ghp_abcdefgh12345678" not in repr(step)
    assert step["decisions"][0] == {"number": 1, "question": "Which [REDACTED] ?"}


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://github.com/o/r/pull/1#c-1", True),
        (None, False),
        ("http://github.com/o/r/pull/1#c-1", False),
        ("javascript:alert(1)", False),
        ("https://user:pw@github.com/o/r", False),
        ("https://github.com:notaport/o/r", False),
    ],
)
def test_comment_link_is_kept_only_for_https(url: str | None, kept: bool) -> None:
    step = next_step(_risk_run(comment_url=url))

    assert step["comment_url"] == (url if kept else None)


@pytest.mark.parametrize(("used", "maximum"), [(0, 3), (2, 3), (3, 3)])
def test_reopens_show_used_and_max(used: int, maximum: int) -> None:
    step = next_step(_risk_run(reopen_count=used, reopen_max=maximum))

    assert (step["reopens_used"], step["reopens_max"]) == (used, maximum)


def test_unknown_reopen_numbers_stay_none() -> None:
    step = next_step(_risk_run(reopen_count=None, reopen_max="three"))

    assert (step["reopens_used"], step["reopens_max"]) == (None, None)


def test_sanitized_run_detail_carries_the_next_step_and_redacts_the_escalation() -> None:
    detail = {
        "run_id": RUN_ID,
        "state": "NEEDS_HUMAN",
        "escalation": {
            "status": "NOTIFIED",
            "reason_code": "RISK_APPROVAL",
            "resume_classification": "RISK_APPROVAL",
            "episode_id": EPISODE_ID,
            "context_fingerprint": FINGERPRINT,
            "reopen_count": 1,
            "reopen_max": 3,
            "comment_url": "https://github.com/o/r/pull/1#c-1",
            "approval_scope": {**SCOPE, "decision_requested": f"Approve {SECRET}"},
            "raw_prompt": "dropped",
        },
    }

    sanitized = run_detail_view(detail)

    assert sanitized["next_step"]["kind"] == "approve"
    assert sanitized["next_step"]["reopens_used"] == 1
    assert sanitized["next_step"]["comment_url"] == "https://github.com/o/r/pull/1#c-1"
    assert "raw_prompt" not in sanitized["escalation"]
    assert sanitized["escalation"]["approval_scope"]["decision_requested"] == "Approve [REDACTED]"
    assert "ghp_abcdefgh12345678" not in repr(sanitized)


def test_sanitized_run_detail_drops_malformed_escalation_context() -> None:
    detail = {
        "run_id": RUN_ID,
        "state": "NEEDS_HUMAN",
        "escalation": {
            "reason_code": "RISK_APPROVAL",
            "resume_classification": "RISK_APPROVAL",
            "episode_id": "ep one",
            "context_fingerprint": "short",
            "reopen_max": -1,
            "approval_scope": {"decision_requested": 5},
            "decisions": "not a list",
        },
    }

    escalation = sanitize_run_detail(detail)["escalation"]

    for key in ("episode_id", "context_fingerprint", "reopen_max", "approval_scope", "decisions"):
        assert key not in escalation


def test_sanitized_run_detail_for_an_active_run_has_no_next_step() -> None:
    sanitized = run_detail_view({"run_id": RUN_ID, "state": "IMPLEMENTING"})

    assert sanitized["next_step"]["kind"] == "none"


def test_stored_run_goes_from_the_provider_through_the_sanitizer_to_a_valid_reply(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    scope = RiskRationale(
        intended_outcome="o",
        sensitive_boundary="b",
        necessity="n",
        credible_scenario="c",
        known_mitigations=["m"],
        residual_risk="r",
    )
    now = datetime(2026, 10, 1, tzinfo=UTC)
    run = FactoryRun(
        id=RUN_ID,
        work_item_id="WI-1",
        state=WorkflowState.NEEDS_HUMAN,
        created_at=now,
        updated_at=now,
        escalation=EscalationRecord(
            episode_id=EPISODE_ID,
            status=EscalationStatus.NOTIFIED,
            resume_classification=ResumeClassification.RISK_APPROVAL,
            reason_code="RISK_APPROVAL",
            reopen_count=1,
            comment_url="https://github.com/o/r/pull/1#c-1",
            approval_context=RiskApprovalContext(
                risk=Risk.R2,
                complexity=Complexity.L1,
                work_item_id="WI-1",
                work_item_title="Task",
                risk_rationale=scope,
                decision_requested=f"Approve {SECRET}",
                authorized_actions=["Run agents."],
                unauthorized_actions=["Change scope."],
                conditions_in_force=["Gates stay on."],
                context_fingerprint=FINGERPRINT,
            ),
        ),
    )
    store.save_run(run)

    detail = build_run_detail(store, RUN_ID, max_reopens=3)
    step = run_detail_view(detail)["next_step"]

    assert step["kind"] == "approve"
    assert (step["reopens_used"], step["reopens_max"]) == (1, 3)
    assert step["approval_scope"]["decision_requested"] == "Approve [REDACTED]"
    assert step["comment_url"] == "https://github.com/o/r/pull/1#c-1"
    assert parse_resume_command(step["reply_text"]) == (RUN_ID, EPISODE_ID)
