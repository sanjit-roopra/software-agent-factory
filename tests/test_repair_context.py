"""Golden test: the repair and correction text sent to agents stays byte-identical.

``tests/golden/repair_context.json`` holds the output of every builder for
fixed inputs. Set ``UPDATE_PROMPT_GOLDENS=1`` to rewrite it after a deliberate
text change; the run then fails on purpose. Review the diff, then run again.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from prompt_fixtures import failing_test_report, plan, review_finding, verification

from software_agent_factory import repair_context
from software_agent_factory.governance import (
    CheckPhase,
    RepositoryVerificationResult,
    VerificationFailureKind,
)
from software_agent_factory.models import (
    AgentRole,
    AttemptRecord,
    AttemptTrigger,
    CICheckEvidence,
    CIReport,
    PlanDecisionAnswer,
    PlanDecisionAnswers,
    RepairContext,
)
from software_agent_factory.workspace import WorkspaceEvidence

GOLDEN = Path(__file__).parent / "golden" / "repair_context.json"
_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _attempt(trigger: AttemptTrigger) -> AttemptRecord:
    return AttemptRecord(
        attempt_number=1,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="r",
        started_at=_TIME,
        completed_at=_TIME,
        outcome="FAILED",
        failure_reason=None if trigger is AttemptTrigger.POLISH else "runtime crashed",
        triggered_by=trigger,
    )


def _failed_verification() -> RepositoryVerificationResult:
    report = verification(stdout="E   AssertionError: expected 400")
    check = report.deterministic_checks[0].model_copy(update={"exit_code": 1, "stderr": "boom"})
    report = report.model_copy(
        update={"passed": False, "deterministic_checks": [check], "failures": ["pytest -q: 1"]}
    )
    return RepositoryVerificationResult(
        report=report,
        command_logs=(),
        failure_kind=VerificationFailureKind.TEST,
        failed_phase=CheckPhase.VERIFY,
        failed_command=None,
    )


def _rendered() -> dict[str, object]:
    rc = repair_context
    decided = plan().model_copy(update={"unresolved_decisions": ["Keep v1?", "Rename it?"]})
    answers = PlanDecisionAnswers(
        run_id="run-1",
        episode_id="ep-1",
        plan_fingerprint="a" * 64,
        context_fingerprint="b" * 64,
        user_login="octocat",
        comment_id=1,
        answers=[
            PlanDecisionAnswer(decision_number=1, answer="No"),
            PlanDecisionAnswer(decision_number=2, answer="Yes"),
        ],
        accepted_at=_TIME,
    )
    prior = RepairContext(trigger=AttemptTrigger.CI, summary="CI failed.", failures=["lint"])
    ci = CIReport(
        overall="FAIL",
        observed_at=_TIME,
        checks=[
            CICheckEvidence(name="lint", status="FAIL", log_excerpt="E501 line too long"),
            CICheckEvidence(name="tests", status="FAIL", failure_category="TEST_FAILURE"),
            CICheckEvidence(name="docs", status="PASS"),
        ],
    )
    contexts = {
        "implementer_failure": rc.implementer_failure_context(_attempt(AttemptTrigger.INITIAL)),
        "implementer_failure_polish": rc.implementer_failure_context(
            _attempt(AttemptTrigger.POLISH)
        ),
        "polish": rc.polish_context(),
        "verification": rc.verification_repair_context(_failed_verification()),
        "verification_mutation": rc.verification_mutation_repair_context(
            WorkspaceEvidence(changed_files=["a.py", "b.py"]),
            WorkspaceEvidence(changed_files=["a.py", "coverage.xml"]),
        ),
        "verification_mutation_same_files": rc.verification_mutation_repair_context(
            WorkspaceEvidence(changed_files=["a.py"]), WorkspaceEvidence(changed_files=["a.py"])
        ),
        "verification_artifact": rc.verification_artifact_repair_context(["dist/", "x.log"]),
        "review": rc.review_repair_context([review_finding("F1")], failing_test_report()),
        "ci": rc.ci_repair_context(ci),
    }
    return {
        "planner_clarification": rc.planner_clarification_context(["Keep v1?", "Rename it?"]),
        "planner_human_decision": rc.planner_human_decision_context(decided, answers),
        "typed_artifact": rc.typed_artifact_repair_context("bad json stdout=xyz", "ReviewReport"),
        "typed_artifact_after_text": rc.typed_artifact_repair_context("bad", "Plan", "Earlier."),
        "typed_artifact_after_context": rc.typed_artifact_repair_context("bad", "Plan", prior),
        "review_contract": rc.review_contract_repair_context("missing disposition"),
        **{name: context.model_dump(mode="json") for name, context in contexts.items()},
    }


def test_repair_context_text_matches_golden() -> None:
    rendered = _rendered()
    if os.environ.get("UPDATE_PROMPT_GOLDENS") == "1":
        GOLDEN.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
        raise AssertionError(f"rewrote {GOLDEN}; review the diff and run again")
    assert rendered == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_bounded_keeps_the_tail() -> None:
    assert repair_context.bounded("  abcdef  ", limit=3) == "def"
    assert repair_context.bounded(" ab ", limit=3) == "ab"


def test_repair_memory_lists_earlier_failures_and_flags_a_repeat() -> None:
    earlier: list[tuple[int, str]] = []

    def failing(code: int, error: str) -> RepairContext:
        return RepairContext(
            trigger=AttemptTrigger.VERIFICATION,
            summary="Deterministic verify failed (test failure).",
            failures=[f"'pytest' exited with code {code}"],
            log_excerpt=f"$ pytest\ncollected 3 items\n{error}",
        )

    remember = repair_context.with_repair_memory
    first = remember(earlier, failing(1, "tests/test_a.py:10: AssertionError"), 1)
    other = remember(earlier, failing(2, "E   ImportError: no module"), 2)
    repeat = remember(earlier, failing(1, "tests/test_a.py:99: AssertionError"), 3)

    assert first is not None
    assert first.previous_attempts == []
    assert other is not None
    assert not other.summary.startswith("Same failure")
    assert repeat is not None
    assert repeat.summary.startswith("Same failure as attempt 1; try a different approach.")
    assert repeat.previous_attempts == [
        "attempt 1: 'pytest' exited with code # | tests/test_a.py:#: AssertionError",
        "attempt 2: 'pytest' exited with code # | E ImportError: no module",
    ]
    polish = RepairContext(trigger=AttemptTrigger.POLISH, summary="polish")
    assert remember(earlier, polish, 4) is polish
    assert remember(earlier, None, 4) is None
    assert len(earlier) == 3
