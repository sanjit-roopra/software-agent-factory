"""The "Needs you" view model (slice 2, step 2.4 of #80)."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from software_agent_factory.dashboard import next_step as next_step_module
from software_agent_factory.dashboard import sanitize
from software_agent_factory.dashboard.next_step import (
    FALLBACK_SENTENCE,
    REASON_SENTENCES,
    REOPEN_LIMIT_CAUSE,
    STALE_SENTENCES,
    NextStepKind,
    next_step,
)
from software_agent_factory.dashboard.sanitize import (
    GUIDANCE_COPY,
    MAX_SCOPE_ITEMS,
    sanitize_run_detail,
)
from software_agent_factory.dashboard.view import run_detail_view
from software_agent_factory.escalation import parse_plan_decision_answers, parse_resume_command
from software_agent_factory.escalation_protocol import REPLY_CLOSED_CAUSES
from software_agent_factory.models import (
    Complexity,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.observability import build_run_detail
from software_agent_factory.redaction import REASON_LIMIT
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
            "reply_closed_cause": None,
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


def _step(run: dict[str, Any]) -> dict[str, Any]:
    """The next step as the page gets it: after the sanitizer redacted and bounded the text."""
    step: dict[str, Any] = run_detail_view(run)["next_step"]
    return step


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


def test_cannot_continue_failure_reason_is_the_sanitized_text_and_its_truncated_flag() -> None:
    run = _run("FAILED", resume_classification=None, failure_reason=f"{SECRET} " + "x" * 2000)

    step = _step(run)

    assert step["failure_reason"].startswith("[REDACTED] ")
    assert len(step["failure_reason"]) <= REASON_LIMIT
    assert step["failure_reason_truncated"] is True


def test_next_step_copies_the_failure_reason_it_is_given_without_changing_it() -> None:
    run = _run("FAILED", resume_classification=None, failure_reason="already bounded")
    run["failure_reason_truncated"] = True

    step = next_step(run)

    assert step["failure_reason"] == "already bounded"
    assert step["failure_reason_truncated"] is True


def test_needs_human_without_an_escalation_record_cannot_continue() -> None:
    step = next_step(_run(resume_classification=None, failure_reason="stopped"))

    assert step["kind"] == "cannot_continue"
    assert step["failure_reason"] == "stopped"


@pytest.mark.parametrize("scope", [None, {}, {**SCOPE, "authorized_actions": "not a list"}])
def test_risk_approval_without_a_valid_approval_context_is_unavailable(scope: Any) -> None:
    step = _step(_run(approval_scope=scope))

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
    assert _step(_plan_run(decisions=decisions))["kind"] == "remote_approval_unavailable"


def test_the_most_decisions_the_answer_parser_accepts_are_all_offered() -> None:
    step = _step(_plan_run(decisions=[f"Question {n}?" for n in range(1, 25)]))

    assert step["kind"] == "answer"
    assert [d["number"] for d in step["decisions"]] == list(range(1, 25))


def test_more_decisions_than_the_answer_parser_accepts_are_unavailable() -> None:
    step = _step(_plan_run(decisions=[f"Question {n}?" for n in range(25)]))

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
    "cause",
    [
        "the notice is not sent yet",
        "the notice has no reply instructions",
        "the reply window expired",
    ],
)
def test_a_reply_the_factory_would_ignore_is_unavailable_and_says_why(cause: str) -> None:
    approve = next_step(_risk_run(reply_closed_cause=cause))
    answer = next_step(_plan_run(reply_closed_cause=cause))

    assert approve["kind"] == answer["kind"] == "remote_approval_unavailable"
    assert f"Remote approval is not available because {cause}." in approve["sentence"]
    assert f"Remote answers are not available because {cause}." in answer["sentence"]
    assert approve["reply_text"] is answer["reply_text"] is None


def test_an_escalation_without_a_reply_state_is_treated_as_closed() -> None:
    run = _risk_run()
    del run["escalation"]["reply_closed_cause"]

    step = next_step(run)

    assert step["kind"] == "remote_approval_unavailable"
    assert "because the reply state is not known." in step["sentence"]


@pytest.mark.parametrize("cause", ["", 5, ["closed"], {"a": 1}, False])
def test_a_malformed_reply_state_is_treated_as_closed(cause: Any) -> None:
    approve = next_step(_risk_run(reply_closed_cause=cause))
    answer = next_step(_plan_run(reply_closed_cause=cause))

    assert approve["kind"] == answer["kind"] == "remote_approval_unavailable"
    assert "because the reply state is not known." in approve["sentence"]
    assert "because the reply state is not known." in answer["sentence"]
    assert approve["reply_text"] is answer["reply_text"] is None


def test_the_action_is_hidden_once_the_reopen_limit_is_reached() -> None:
    for used in (3, 5):
        approve = next_step(_risk_run(reopen_count=used, reopen_max=3))
        answer = next_step(_plan_run(reopen_count=used, reopen_max=3))

        assert approve["kind"] == answer["kind"] == "remote_approval_unavailable"
        assert approve["reply_text"] is answer["reply_text"] is None
        assert "because the reopen limit is reached." in approve["sentence"]
        assert "because the reopen limit is reached." in answer["sentence"]
        assert f"factory show {RUN_ID}" in approve["sentence"]


def test_the_action_stays_while_a_reopen_is_left_or_the_numbers_are_unknown() -> None:
    assert next_step(_risk_run(reopen_count=2, reopen_max=3))["kind"] == "approve"
    assert next_step(_plan_run(reopen_count=None, reopen_max=None))["kind"] == "answer"


def test_the_reopen_limit_phrase_is_one_the_factory_reports() -> None:
    assert REOPEN_LIMIT_CAUSE in REPLY_CLOSED_CAUSES


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


def test_no_two_reason_codes_share_a_sentence() -> None:
    assert len(set(REASON_SENTENCES.values())) == len(REASON_SENTENCES)


def test_every_reason_sentence_starts_with_the_run() -> None:
    assert all(sentence.startswith("The run ") for sentence in REASON_SENTENCES.values())


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

    step = _step(_risk_run(approval_scope=scope))

    assert "ghp_abcdefgh12345678" not in repr(step)
    assert step["approval_scope"]["decision_requested"] == "Approve [REDACTED]"
    assert step["approval_scope"]["authorized_actions"] == ["Use [REDACTED]"]
    assert step["approval_scope"]["unauthorized_actions"] == ["No [REDACTED]"]
    assert step["approval_scope"]["conditions_in_force"] == ["Keep [REDACTED]"]


def test_secret_in_a_decision_question_is_redacted() -> None:
    step = _step(_plan_run(decisions=[f"Which {SECRET} ?", "Fine?"]))

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
            "reply_closed_cause": None,
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
            remote_resume_enabled=True,
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

    detail = build_run_detail(
        store, RUN_ID, max_reopens=3, reply_window_hours=168, escalation_enabled=True
    )
    step = run_detail_view(detail)["next_step"]

    assert step["kind"] == "approve"
    assert (step["reopens_used"], step["reopens_max"]) == (1, 3)
    assert step["approval_scope"]["decision_requested"] == "Approve [REDACTED]"
    assert step["comment_url"] == "https://github.com/o/r/pull/1#c-1"
    assert parse_resume_command(step["reply_text"]) == (RUN_ID, EPISODE_ID)


def test_a_stored_notice_with_no_reply_instructions_is_unavailable_end_to_end(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "data")
    now = datetime(2026, 10, 1, tzinfo=UTC)
    store.save_run(
        FactoryRun(
            id=RUN_ID,
            work_item_id="WI-1",
            state=WorkflowState.NEEDS_HUMAN,
            created_at=now,
            updated_at=now,
            escalation=EscalationRecord(
                episode_id=EPISODE_ID,
                status=EscalationStatus.NOTIFIED,
                resume_classification=ResumeClassification.PLAN_DECISION,
                reason_code="UNRESOLVED_DECISIONS",
                remote_resume_enabled=False,
                reply_cursor="closed",
                plan_decision_context=PlanDecisionContext(
                    plan_fingerprint="p" * 64,
                    decisions=["Use SQLite?"],
                    context_fingerprint=FINGERPRINT,
                ),
            ),
        )
    )

    step = run_detail_view(build_run_detail(store, RUN_ID, max_reopens=3))["next_step"]

    assert step["kind"] == "remote_approval_unavailable"
    assert "because the notice has no reply instructions." in step["sentence"]


@pytest.mark.parametrize("cause", [*sorted(REPLY_CLOSED_CAUSES), None])
def test_the_sanitizer_keeps_every_known_reply_closed_cause_and_none(cause: str | None) -> None:
    detail = {"run_id": RUN_ID, "escalation": {"reply_closed_cause": cause}}

    assert sanitize_run_detail(detail)["escalation"]["reply_closed_cause"] == cause


@pytest.mark.parametrize("cause", ["", 5, ["closed"], {"a": 1}, "a phrase the factory never sends"])
def test_the_sanitizer_drops_a_reply_closed_cause_that_is_not_a_known_phrase(cause: Any) -> None:
    detail = {"run_id": RUN_ID, "escalation": {"reply_closed_cause": cause}}

    assert "reply_closed_cause" not in sanitize_run_detail(detail)["escalation"]


def test_each_escalation_text_is_cut_to_the_reason_limit_and_names_the_run() -> None:
    long = "x" * 2000
    scope = {
        "decision_requested": long,
        "authorized_actions": [long],
        "unauthorized_actions": [long],
        "conditions_in_force": [long],
    }

    approve = _step(_risk_run(approval_scope=scope))
    answer = _step(_plan_run(decisions=[long, "Fine?"]))

    texts = [
        approve["approval_scope"]["decision_requested"],
        approve["approval_scope"]["authorized_actions"][0],
        approve["approval_scope"]["unauthorized_actions"][0],
        approve["approval_scope"]["conditions_in_force"][0],
        answer["decisions"][0]["question"],
    ]
    for text in texts:
        assert len(text) <= REASON_LIMIT
        assert f"`factory show {RUN_ID}`" in text


def test_an_approval_scope_list_over_the_cap_is_dropped_whole() -> None:
    items = [f"Allowed {n}." for n in range(MAX_SCOPE_ITEMS)]
    at_cap = _step(_risk_run(approval_scope={**SCOPE, "authorized_actions": items}))
    over_cap = _step(_risk_run(approval_scope={**SCOPE, "authorized_actions": [*items, "More."]}))

    assert at_cap["approval_scope"]["authorized_actions"] == items
    assert over_cap["kind"] == "remote_approval_unavailable"
    assert over_cap["approval_scope"] is None


def test_sanitize_does_not_import_next_step_and_next_step_does_not_redact() -> None:
    def imported(module: Any) -> set[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.add(f"{'.' * node.level}{node.module or ''}")
                names.update(f"{'.' * node.level}{node.module or ''}.{a.name}" for a in node.names)
        return names

    assert not {name for name in imported(sanitize) if "next_step" in name}
    assert not {name for name in imported(next_step_module) if "redaction" in name}


# ---- Dashboard requests: queued and stale ------------------------------------------

REQUESTED_AT = "2026-10-01T09:30:00Z"
QUEUED_AT = "2026-10-01 09:30 UTC"
START_HINT = "If factory start is not running, start it."


def _request(
    action: str = "RISK_APPROVAL",
    *,
    status: str = "pending",
    reason: str | None = None,
    fingerprint: str = FINGERPRINT,
    created_at: str = REQUESTED_AT,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "action": action,
        "context_fingerprint": fingerprint,
        "status": status,
        "reason": reason,
        "created_at": created_at,
        **extra,
    }


def test_a_pending_approval_shows_when_it_was_queued_and_how_to_start_the_service() -> None:
    step = next_step(_risk_run(), [_request()])

    assert step["kind"] == "approved_pending"
    assert (
        step["sentence"] == f"Approved at {QUEUED_AT}, queued for the factory service. {START_HINT}"
    )
    assert step["requested_at"] == "2026-10-01T09:30:00+00:00"
    assert step["stale_sentence"] is None
    assert (step["episode_id"], step["context_fingerprint"]) == (EPISODE_ID, FINGERPRINT)
    assert step["approval_scope"] is None
    assert step["reply_text"] is None


def test_pending_plan_answers_use_their_own_wording() -> None:
    step = next_step(_plan_run(), [_request("PLAN_DECISION")])

    assert step["kind"] == "approved_pending"
    assert step["sentence"] == f"Answers sent at {QUEUED_AT}, queued for the factory service"
    assert step["decisions"] == []


def test_the_queued_time_is_shown_in_utc_whatever_offset_the_store_wrote() -> None:
    step = next_step(_risk_run(), [_request(created_at="2026-10-01T11:30:00+02:00")])

    assert QUEUED_AT in step["sentence"]


def test_a_pending_request_still_shows_when_the_reply_is_closed() -> None:
    run = _risk_run(reply_closed_cause="the reply window expired", reopen_count=3)

    assert next_step(run, [_request()])["kind"] == "approved_pending"


@pytest.mark.parametrize(
    ("reason", "sentence"),
    [
        ("expired", "approval expired, approve again"),
        ("reopen_limit", "reopen limit reached, inspect with factory show"),
        ("context_changed", "the run changed, review again"),
        ("state_changed", "the run state changed, review again"),
    ],
)
def test_a_stale_request_adds_its_reason_to_the_normal_panel(reason: str, sentence: str) -> None:
    step = next_step(_risk_run(), [_request(status="stale", reason=reason)])

    assert step["kind"] == "approve"
    assert step["stale_sentence"] == sentence
    assert step["requested_at"] is None
    assert step["approval_scope"] == SCOPE


def test_every_stale_reason_has_a_sentence() -> None:
    assert set(STALE_SENTENCES) == {"expired", "reopen_limit", "context_changed", "state_changed"}


def test_a_stale_request_for_an_old_context_still_explains_the_new_panel() -> None:
    old = _request(status="stale", reason="context_changed", fingerprint="a" * 64)

    step = next_step(_plan_run(), [{**old, "action": "PLAN_DECISION"}])

    assert step["kind"] == "answer"
    assert step["stale_sentence"] == "the run changed, review again"


def test_the_newest_stale_request_wins() -> None:
    older = _request(status="stale", reason="expired", created_at="2026-10-01T08:00:00Z")
    newer = _request(
        status="stale", reason="context_changed", fingerprint="a" * 64, created_at=REQUESTED_AT
    )

    step = next_step(_risk_run(), [newer, older])

    assert step["stale_sentence"] == STALE_SENTENCES["context_changed"]


def test_a_pending_request_for_another_context_or_action_does_not_count() -> None:
    other_context = _request(fingerprint="a" * 64)
    other_action = _request("PLAN_DECISION")

    for request in (other_context, other_action):
        step = next_step(_risk_run(), [request])

        assert step["kind"] == "approve"
        assert step["stale_sentence"] is None


def test_without_a_request_the_step_has_no_queued_fields() -> None:
    step = next_step(_risk_run())

    assert step["kind"] == "approve"
    assert (step["requested_at"], step["stale_sentence"]) == (None, None)


def test_no_request_matters_without_a_valid_context() -> None:
    step = next_step(_risk_run(context_fingerprint="short"), [_request(fingerprint="short")])

    assert step["kind"] == "remote_approval_unavailable"


def test_a_request_never_changes_a_run_that_is_not_waiting() -> None:
    step = next_step(_run("IMPLEMENTING"), [_request()])

    assert step["kind"] == "none"


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        _request(status="done"),
        _request("OTHER"),
        _request(fingerprint="short"),
        _request(status="stale", reason=None),
        _request(status="stale", reason="bogus"),
        _request(reason="expired"),
        _request(created_at="yesterday"),
        _request(created_at="2026-10-01T09:30:00"),
        _request(created_at=5),
    ],
)
def test_a_malformed_request_is_ignored(bad: Any) -> None:
    step = next_step(_risk_run(), [bad])

    assert step["kind"] == "approve"
    assert step["stale_sentence"] is None


def test_the_answers_of_a_request_never_reach_the_step() -> None:
    answers = [{"decision_number": 1, "answer": "use the secret plan"}]

    step = next_step(_plan_run(), [_request("PLAN_DECISION", answers=answers)])

    assert "secret plan" not in repr(step)


def test_the_view_asks_only_a_waiting_run_with_a_valid_episode_for_requests() -> None:
    asked: list[tuple[str, str]] = []

    def requests_for(run_id: str, episode_id: str) -> list[dict[str, Any]]:
        asked.append((run_id, episode_id))
        return [_request()]

    waiting = run_detail_view(_risk_run(), requests_for)
    active = run_detail_view(_run("IMPLEMENTING"), requests_for)
    no_episode = run_detail_view(_risk_run(episode_id="ep one"), requests_for)

    assert waiting["next_step"]["kind"] == "approved_pending"
    assert active["next_step"]["kind"] == "none"
    assert no_episode["next_step"]["kind"] == "remote_approval_unavailable"
    assert asked == [(RUN_ID, EPISODE_ID)]


def test_the_view_without_a_reader_shows_the_normal_panel() -> None:
    assert run_detail_view(_risk_run())["next_step"]["kind"] == "approve"


def test_the_kinds_are_one_enum_and_serialize_as_plain_strings() -> None:
    assert [kind.value for kind in NextStepKind] == [
        "none",
        "cannot_continue",
        "remote_approval_unavailable",
        "approve",
        "answer",
        "approved_pending",
    ]
    assert json.dumps(next_step(_risk_run())["kind"]) == '"approve"'
