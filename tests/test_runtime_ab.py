"""Tests for the pi vs Copilot A/B benchmark: manifest, report and go bar.

No paid runtime is ever started: reports are built from hand-made
``InvocationRecord`` fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from software_agent_factory.models import (
    AgentRole,
    AttemptRecord,
    AttemptTrigger,
    FactoryRun,
    InvocationRecord,
    UsageMetrics,
    VerificationReport,
    WorkflowState,
)
from software_agent_factory.store import FileRunStore

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, relative_path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ab = _load_script_module("runtime_ab", "scripts/performance/runtime_ab.py")

SHA_A = "a" * 40
SHA_B = "b" * 40
T0 = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def _invocation(number: int = 1, **usage: Any) -> InvocationRecord:
    return InvocationRecord(
        invocation_number=number,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="high",
        started_at=T0,
        completed_at=T0 + timedelta(seconds=5),
        success=True,
        usage=UsageMetrics(**usage) if usage else None,
    )


def _sample(
    *,
    passed: bool = True,
    wall: float = 10.0,
    repairs: int = 0,
    invocations: tuple[InvocationRecord, ...] = (),
) -> Any:
    return ab.RunSample(
        passed=passed, wall_seconds=wall, repair_rounds=repairs, invocations=invocations
    )


def _entry(issue: int = 1, level: str | None = None) -> Any:
    return ab.ManifestEntry(issue=issue, base_sha=SHA_A, level=level)


def _outcome(issue: int, copilot: Any, pi: Any) -> Any:
    return ab.TaskOutcome(
        entry=_entry(issue),
        samples={ab.Runtime.COPILOT: copilot, ab.Runtime.PI: pi},
    )


def _tokens_sample(
    *, input: int, cache_read: int | None, cache_write: int | None = 0, passed: bool = True
) -> Any:
    usage: dict[str, Any] = {"input_tokens": input, "output_tokens": 50}
    if cache_read is not None:
        usage["cache_read_tokens"] = cache_read
    if cache_write is not None:
        usage["cache_write_tokens"] = cache_write
    return _sample(passed=passed, invocations=(_invocation(**usage),))


def _bar_report(copilot: Any, pi: Any) -> Any:
    return ab.build_report([_outcome(1, copilot, pi)])


def _criteria(report: Any) -> dict[str, bool]:
    return {item.name: item.met for item in report.verdict.criteria}


COPILOT_BASE = dict(input=1000, cache_read=1000)


# --- manifest -----------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, tasks: list[dict[str, Any]]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return path


def test_manifest_loads_entries_with_optional_level_and_title(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path,
        [
            {"issue": 12, "base_sha": SHA_A},
            {"issue": 13, "base_sha": SHA_B, "level": "L3", "title": "Big one"},
        ],
    )

    manifest = ab.load_manifest(path)

    assert [entry.issue for entry in manifest.tasks] == [12, 13]
    assert manifest.tasks[0].level is None
    assert (manifest.tasks[1].level, manifest.tasks[1].title) == ("L3", "Big one")


def test_manifest_entry_without_base_sha_names_the_entry(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, [{"issue": 12, "base_sha": SHA_A}, {"issue": 42}])

    with pytest.raises(ab.ManifestError) as excinfo:
        ab.load_manifest(path)

    message = str(excinfo.value)
    assert "entry 2" in message
    assert "issue 42" in message
    assert "base_sha" in message


@pytest.mark.parametrize(
    "bad",
    [
        {"issue": 5, "base_sha": "abc123"},
        {"issue": 5, "base_sha": SHA_A, "level": "L9"},
        {"issue": 0, "base_sha": SHA_A},
        {"issue": 5, "base_sha": SHA_A, "surprise": 1},
    ],
)
def test_manifest_rejects_invalid_entry(tmp_path: Path, bad: dict[str, Any]) -> None:
    path = _write_manifest(tmp_path, [bad])

    with pytest.raises(ab.ManifestError, match="entry 1"):
        ab.load_manifest(path)


def test_manifest_rejects_duplicate_issue(tmp_path: Path) -> None:
    path = _write_manifest(
        tmp_path, [{"issue": 7, "base_sha": SHA_A}, {"issue": 7, "base_sha": SHA_B}]
    )

    with pytest.raises(ab.ManifestError, match="duplicate issue 7"):
        ab.load_manifest(path)


def test_manifest_rejects_empty_task_list(tmp_path: Path) -> None:
    with pytest.raises(ab.ManifestError, match="no tasks"):
        ab.load_manifest(_write_manifest(tmp_path, []))


def test_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ab.ManifestError, match="not valid JSON"):
        ab.load_manifest(path)


# --- report: per task and total -------------------------------------------------------------


def test_report_shows_every_metric_per_task_and_in_total() -> None:
    copilot_1 = _sample(
        wall=100.0,
        repairs=1,
        invocations=(
            _invocation(
                1,
                input_tokens=1000,
                output_tokens=200,
                cache_read_tokens=500,
                cache_write_tokens=100,
                premium_requests=3.0,
                total_nano_aiu=7,
            ),
        ),
    )
    copilot_2 = _sample(
        passed=False,
        wall=50.0,
        repairs=2,
        invocations=(
            _invocation(
                1,
                input_tokens=400,
                output_tokens=100,
                cache_read_tokens=100,
                cache_write_tokens=0,
                premium_requests=1.0,
                total_nano_aiu=3,
            ),
        ),
    )
    pi_1 = _sample(
        wall=80.0,
        invocations=(
            _invocation(
                1,
                input_tokens=600,
                output_tokens=150,
                cache_read_tokens=900,
                cache_write_tokens=50,
                list_price_estimate_usd=0.25,
            ),
        ),
    )
    pi_2 = _sample(
        wall=40.0,
        repairs=1,
        invocations=(
            _invocation(
                1,
                input_tokens=300,
                output_tokens=60,
                cache_read_tokens=300,
                cache_write_tokens=0,
                list_price_estimate_usd=0.10,
            ),
        ),
    )

    report = ab.build_report([_outcome(1, copilot_1, pi_1), _outcome(2, copilot_2, pi_2)])

    first = report.tasks[0].runtimes[ab.Runtime.COPILOT]
    assert (first.runs, first.passes) == (1, 1)
    assert first.tokens.model_dump() == {
        "input": 1000,
        "output": 200,
        "cache_read": 500,
        "cache_write": 100,
    }
    assert first.cache_read_share == pytest.approx(500 / 1600)
    assert (first.cost.premium_requests, first.cost.total_nano_aiu) == (3.0, 7)
    assert first.cost.list_price_estimate_usd is None
    assert (first.wall_seconds, first.repair_rounds) == (100.0, 1)

    copilot_total = report.totals[ab.Runtime.COPILOT]
    assert (copilot_total.runs, copilot_total.passes) == (2, 1)
    assert copilot_total.tokens.input == 1400
    assert copilot_total.tokens.cache_read == 600
    assert copilot_total.cache_read_share == pytest.approx(600 / (1400 + 600 + 100))
    assert copilot_total.cost.premium_requests == 4.0
    assert (copilot_total.wall_seconds, copilot_total.repair_rounds) == (150.0, 3)

    pi_total = report.totals[ab.Runtime.PI]
    assert pi_total.cost.list_price_estimate_usd == pytest.approx(0.35)
    assert pi_total.cost.premium_requests is None
    assert pi_total.cost.total_nano_aiu is None


def test_cost_units_are_kept_apart_per_runtime() -> None:
    both = dict(premium_requests=2.0, total_nano_aiu=9, list_price_estimate_usd=1.5)
    sample = _sample(invocations=(_invocation(**both),))

    report = _bar_report(sample, sample)

    copilot_cost = report.totals[ab.Runtime.COPILOT].cost
    pi_cost = report.totals[ab.Runtime.PI].cost
    assert copilot_cost.model_dump() == {
        "premium_requests": 2.0,
        "total_nano_aiu": 9,
        "list_price_estimate_usd": None,
    }
    assert pi_cost.model_dump() == {
        "premium_requests": None,
        "total_nano_aiu": None,
        "list_price_estimate_usd": 1.5,
    }


def test_cache_read_share_is_unavailable_when_no_cache_counts_reported() -> None:
    copilot = _tokens_sample(input=1000, cache_read=None, cache_write=None)
    pi = _tokens_sample(input=500, cache_read=400)

    report = _bar_report(copilot, pi)

    assert report.tasks[0].runtimes[ab.Runtime.COPILOT].cache_read_share is None
    assert report.totals[ab.Runtime.COPILOT].cache_read_share is None
    assert report.totals[ab.Runtime.COPILOT].tokens.cache_read is None
    assert report.totals[ab.Runtime.PI].cache_read_share is not None


def test_cache_read_share_is_zero_when_reported_and_zero() -> None:
    copilot = _tokens_sample(input=1000, cache_read=0, cache_write=0)
    pi = _tokens_sample(input=500, cache_read=400)

    report = _bar_report(copilot, pi)

    assert report.tasks[0].runtimes[ab.Runtime.COPILOT].cache_read_share == 0.0
    assert report.totals[ab.Runtime.COPILOT].cache_read_share == 0.0
    assert report.totals[ab.Runtime.COPILOT].tokens.cache_read == 0


def test_task_without_usage_reports_unavailable_everything() -> None:
    report = _bar_report(_sample(), _sample())

    metrics = report.totals[ab.Runtime.COPILOT]
    assert metrics.tokens.model_dump() == {
        "input": None,
        "output": None,
        "cache_read": None,
        "cache_write": None,
    }
    assert metrics.cache_read_share is None
    assert metrics.cost.premium_requests is None


def test_cache_unavailable_in_one_task_only_does_not_hide_the_total() -> None:
    silent = _tokens_sample(input=100, cache_read=None, cache_write=None)
    loud = _tokens_sample(input=100, cache_read=100)
    pi = _tokens_sample(input=100, cache_read=100)

    report = ab.build_report([_outcome(1, silent, pi), _outcome(2, loud, pi)])

    assert report.tasks[0].runtimes[ab.Runtime.COPILOT].cache_read_share is None
    assert report.totals[ab.Runtime.COPILOT].cache_read_share == pytest.approx(100 / 300)


def test_skipped_task_is_marked_and_excluded_from_totals() -> None:
    ran = _outcome(
        1, _tokens_sample(input=10, cache_read=0), _tokens_sample(input=10, cache_read=0)
    )
    skipped = ab.TaskOutcome(entry=_entry(2), samples={})

    report = ab.build_report([ran, skipped])

    assert [task.skipped for task in report.tasks] == [False, True]
    assert report.tasks[1].runtimes == {}
    assert report.totals[ab.Runtime.COPILOT].runs == 1


def test_task_report_carries_level_title_and_error() -> None:
    entry = ab.ManifestEntry(issue=9, base_sha=SHA_A, level="L2", title="Fix thing")
    broken = ab.RunSample(passed=False, wall_seconds=0.0, repair_rounds=0, error="no run stored")
    outcome = ab.TaskOutcome(
        entry=entry, samples={ab.Runtime.COPILOT: broken, ab.Runtime.PI: _sample()}
    )

    task = ab.build_report([outcome]).tasks[0]

    assert (task.issue, task.level, task.title) == (9, "L2", "Fix thing")
    assert task.errors == {ab.Runtime.COPILOT: "no run stored"}


# --- go bar ---------------------------------------------------------------------------------


def test_go_bar_met() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=700, cache_read=3000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is True
    assert _criteria(report) == {"pass_count": True, "cache_read_share": True, "tokens": True}


def test_go_bar_pass_count_fails_alone() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=700, cache_read=3000, passed=False)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": False, "cache_read_share": True, "tokens": True}


def test_go_bar_cache_share_fails_alone() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=700, cache_read=1000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": True, "cache_read_share": False, "tokens": True}


def test_go_bar_tokens_fail_alone() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=900, cache_read=3000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": True, "cache_read_share": True, "tokens": False}


def test_go_bar_share_needs_fifteen_points_over_copilot() -> None:
    copilot = _tokens_sample(input=500, cache_read=500)
    exactly = _tokens_sample(input=175, cache_read=325)
    just_short = _tokens_sample(input=176, cache_read=325)

    assert _criteria(_bar_report(copilot, exactly))["cache_read_share"] is True
    assert _criteria(_bar_report(copilot, just_short))["cache_read_share"] is False


def test_go_bar_token_ceiling_counts_input_plus_cache_write() -> None:
    copilot = _tokens_sample(input=1000, cache_read=1000, cache_write=1000)
    at_ceiling = _tokens_sample(input=1000, cache_read=9000, cache_write=600)
    over_ceiling = _tokens_sample(input=1000, cache_read=9000, cache_write=601)

    assert _criteria(_bar_report(copilot, at_ceiling))["tokens"] is True
    assert _criteria(_bar_report(copilot, over_ceiling))["tokens"] is False


def test_go_bar_pi_passes_only_at_70_percent_when_copilot_share_unavailable() -> None:
    copilot = _tokens_sample(input=1000, cache_read=None, cache_write=None)
    at_bar = _tokens_sample(input=300, cache_read=700)
    below_bar = _tokens_sample(input=301, cache_read=700)

    met = _bar_report(copilot, at_bar)
    unmet = _bar_report(copilot, below_bar)

    assert _criteria(met)["cache_read_share"] is True
    assert met.verdict.meets is True
    assert _criteria(unmet)["cache_read_share"] is False
    assert unmet.verdict.meets is False


def test_go_bar_fails_when_pi_reports_no_cache_counts() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=100, cache_read=None, cache_write=None)

    report = _bar_report(copilot, pi)

    assert _criteria(report)["cache_read_share"] is False
    detail = {item.name: item.detail for item in report.verdict.criteria}["cache_read_share"]
    assert "unavailable" in detail


def test_go_bar_token_criterion_fails_when_input_tokens_unreported() -> None:
    copilot = _sample()
    pi = _tokens_sample(input=1, cache_read=3000)

    report = _bar_report(copilot, pi)

    assert _criteria(report)["tokens"] is False


def test_go_bar_compares_only_tasks_that_ran() -> None:
    copilot = _tokens_sample(**COPILOT_BASE)
    pi = _tokens_sample(input=700, cache_read=3000)
    skipped = ab.TaskOutcome(entry=_entry(2), samples={})

    report = ab.build_report([_outcome(1, copilot, pi), skipped])

    assert report.verdict.meets is True


def test_go_bar_is_not_met_with_no_completed_tasks() -> None:
    report = ab.build_report([ab.TaskOutcome(entry=_entry(1), samples={})])

    assert report.verdict.meets is False


# --- stored runs ------------------------------------------------------------------------------


def _attempt(number: int, trigger: AttemptTrigger, role: AgentRole = AgentRole.IMPLEMENTER) -> Any:
    return AttemptRecord(
        attempt_number=number,
        role=role,
        model="m",
        reasoning="high",
        started_at=T0,
        completed_at=T0,
        outcome="done",
        triggered_by=trigger,
    )


def _run(state: WorkflowState, **overrides: Any) -> FactoryRun:
    fields: dict[str, Any] = {
        "id": "run-1",
        "work_item_id": "wi-1",
        "state": state,
        "created_at": T0,
        "updated_at": T0 + timedelta(seconds=30),
        "completed_at": T0 + timedelta(seconds=90),
        "invocation_records": [_invocation(1, input_tokens=5)],
        "attempt_records": [
            _attempt(1, AttemptTrigger.INITIAL),
            _attempt(2, AttemptTrigger.VERIFICATION),
            _attempt(3, AttemptTrigger.REVIEW),
            _attempt(4, AttemptTrigger.REVIEW, role=AgentRole.REVIEWER),
        ],
    }
    fields.update(overrides)
    return FactoryRun(**fields)


def test_sample_from_run_reads_pass_wall_time_and_repair_rounds() -> None:
    sample = ab.sample_from_run(_run(WorkflowState.PR_READY), verification_passed=True)

    assert sample.passed is True
    assert sample.wall_seconds == 90.0
    assert sample.repair_rounds == 2
    assert len(sample.invocations) == 1


@pytest.mark.parametrize("state", [WorkflowState.FAILED, WorkflowState.NEEDS_HUMAN])
def test_sample_from_run_fails_a_run_that_did_not_reach_pr_ready(state: WorkflowState) -> None:
    assert ab.sample_from_run(_run(state), verification_passed=True).passed is False


def test_sample_from_run_fails_when_verification_was_red() -> None:
    sample = ab.sample_from_run(_run(WorkflowState.DONE), verification_passed=False)

    assert sample.passed is False


def test_sample_from_run_falls_back_to_updated_at_for_wall_time() -> None:
    run = _run(WorkflowState.FAILED, completed_at=None)

    assert ab.sample_from_run(run, verification_passed=None).wall_seconds == 30.0


def test_load_sample_reads_run_and_verification_from_store(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))
    store.save_artifact("run-1", VerificationReport(passed=False, confidence=0.5))

    sample = ab.load_sample(store, "run-1")

    assert sample.passed is False


def test_load_sample_treats_missing_verification_artifact_as_unknown(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))

    assert ab.load_sample(store, "run-1").passed is True


# --- rendering ----------------------------------------------------------------------------------


def test_markdown_shows_unavailable_zero_and_verdict() -> None:
    copilot = _tokens_sample(input=1000, cache_read=None, cache_write=None)
    zero = _tokens_sample(input=500, cache_read=0)

    unavailable_md = ab.render_markdown(_bar_report(copilot, zero))
    zero_md = ab.render_markdown(_bar_report(zero, zero))

    assert "unavailable" in unavailable_md
    assert "0.0%" in zero_md
    assert "Go bar" in unavailable_md
    assert "experimental" in unavailable_md


def test_markdown_states_recommended_when_go_bar_met() -> None:
    report = _bar_report(_tokens_sample(**COPILOT_BASE), _tokens_sample(input=700, cache_read=3000))

    markdown = ab.render_markdown(report)

    assert "recommended" in markdown
    assert "list-price estimate" in markdown


def test_markdown_lists_skipped_tasks() -> None:
    report = ab.build_report([ab.TaskOutcome(entry=_entry(77), samples={})])

    assert "skipped" in ab.render_markdown(report)
