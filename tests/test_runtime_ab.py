"""Tests for the pi vs Copilot A/B benchmark: manifest, report and go bar.

No paid runtime is ever started: reports are built from hand-made
``InvocationRecord`` fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from software_agent_factory.models import (
    AgentRole,
    AttemptRecord,
    AttemptTrigger,
    Complexity,
    FactoryRun,
    InvocationRecord,
    Risk,
    TriageResult,
    UsageMetrics,
    VerificationReport,
    WorkflowState,
)
from software_agent_factory.store import FileRunStore
from software_agent_factory.subprocess_utils import GITHUB_CREDENTIAL_ENV_VARS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _cwd_is_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The script confines --manifest and --out to the working directory."""
    monkeypatch.chdir(tmp_path)


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
    invocations: tuple[InvocationRecord, ...] | None = None,
) -> Any:
    if invocations is None:
        invocations = (_invocation(),)
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


def _copilot_sample() -> Any:
    return _tokens_sample(input=1000, cache_read=1000)


# --- manifest ----------


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
    path = _write_manifest(tmp_path, [])

    with pytest.raises(ab.ManifestError, match="no tasks"):
        ab.load_manifest(path)


def test_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ab.ManifestError, match="not valid JSON"):
        ab.load_manifest(path)


def test_manifest_that_cannot_be_read_is_a_manifest_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(ab.ManifestError, match="could not be read"):
        ab.load_manifest(missing)


def _outside_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make a sibling directory the cwd and return a directory outside it."""
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.chdir(inside)
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside


def test_manifest_outside_the_working_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside_cwd(tmp_path, monkeypatch)
    path = _write_manifest(outside, [{"issue": 1, "base_sha": SHA_A}])

    with pytest.raises(ab.ManifestError, match="outside the working directory"):
        ab.load_manifest(path)


def test_manifest_symlink_out_of_the_working_directory_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside_cwd(tmp_path, monkeypatch)
    target = _write_manifest(outside, [{"issue": 1, "base_sha": SHA_A}])
    link = Path("link.json")
    link.symlink_to(target)

    with pytest.raises(ab.ManifestError, match="outside the working directory"):
        ab.load_manifest(link)


# --- report: per task and total ----------


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

    assert report.tasks[0].runtimes == {
        ab.Runtime.COPILOT: ab.RuntimeMetrics(
            runs=1,
            passes=1,
            tokens=ab.TokenCounts(input=1000, output=200, cache_read=500, cache_write=100),
            cache_read_share=500 / 1600,
            cost=ab.RuntimeCost(premium_requests=3.0, total_nano_aiu=7),
            wall_seconds=100.0,
            repair_rounds=1,
        ),
        ab.Runtime.PI: ab.RuntimeMetrics(
            runs=1,
            passes=1,
            tokens=ab.TokenCounts(input=600, output=150, cache_read=900, cache_write=50),
            cache_read_share=900 / 1550,
            cost=ab.RuntimeCost(list_price_estimate_usd=0.25),
            wall_seconds=80.0,
            repair_rounds=0,
        ),
    }
    assert report.tasks[1].runtimes == {
        ab.Runtime.COPILOT: ab.RuntimeMetrics(
            runs=1,
            passes=0,
            tokens=ab.TokenCounts(input=400, output=100, cache_read=100, cache_write=0),
            cache_read_share=100 / 500,
            cost=ab.RuntimeCost(premium_requests=1.0, total_nano_aiu=3),
            wall_seconds=50.0,
            repair_rounds=2,
        ),
        ab.Runtime.PI: ab.RuntimeMetrics(
            runs=1,
            passes=1,
            tokens=ab.TokenCounts(input=300, output=60, cache_read=300, cache_write=0),
            cache_read_share=300 / 600,
            cost=ab.RuntimeCost(list_price_estimate_usd=0.10),
            wall_seconds=40.0,
            repair_rounds=1,
        ),
    }
    assert report.totals == {
        ab.Runtime.COPILOT: ab.RuntimeMetrics(
            runs=2,
            passes=1,
            tokens=ab.TokenCounts(input=1400, output=300, cache_read=600, cache_write=100),
            cache_read_share=600 / 2100,
            cost=ab.RuntimeCost(premium_requests=4.0, total_nano_aiu=10),
            wall_seconds=150.0,
            repair_rounds=3,
        ),
        ab.Runtime.PI: ab.RuntimeMetrics(
            runs=2,
            passes=2,
            tokens=ab.TokenCounts(input=900, output=210, cache_read=1200, cache_write=50),
            cache_read_share=1200 / 2150,
            cost=ab.RuntimeCost(list_price_estimate_usd=0.25 + 0.10),
            wall_seconds=120.0,
            repair_rounds=1,
        ),
    }


def test_cost_units_are_kept_apart_per_runtime() -> None:
    sample = _sample(
        invocations=(
            _invocation(premium_requests=2.0, total_nano_aiu=9, list_price_estimate_usd=1.5),
        )
    )

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


def _failed(message: str | None = "no run stored") -> Any:
    return ab.RunSample(passed=False, wall_seconds=0.0, repair_rounds=0, error=message)


def test_task_where_one_runtime_failed_is_dropped_from_both_totals() -> None:
    fine = _tokens_sample(input=100, cache_read=100)
    dropped = _outcome(2, _failed(), _tokens_sample(input=9000, cache_read=1))

    report = ab.build_report([_outcome(1, fine, fine), dropped])

    assert [task.excluded for task in report.tasks] == [False, True]
    assert [task.skipped for task in report.tasks] == [False, False]
    for runtime in ab.Runtime:
        assert report.totals[runtime].runs == 1
        assert report.totals[runtime].tokens.input == 100


def test_sample_without_invocations_excludes_the_task() -> None:
    silent = ab.RunSample(passed=True, wall_seconds=1.0, repair_rounds=0)

    report = ab.build_report([_outcome(1, silent, _sample())])

    assert report.tasks[0].excluded is True
    assert report.totals[ab.Runtime.PI].runs == 0


def test_task_that_ran_cleanly_is_not_excluded() -> None:
    report = ab.build_report([_outcome(1, _sample(), _sample())])

    assert report.tasks[0].excluded is False


def test_task_report_carries_level_title_and_error() -> None:
    entry = ab.ManifestEntry(issue=9, base_sha=SHA_A, level="L2", title="Fix thing")
    broken = ab.RunSample(passed=False, wall_seconds=0.0, repair_rounds=0, error="no run stored")
    outcome = ab.TaskOutcome(
        entry=entry, samples={ab.Runtime.COPILOT: broken, ab.Runtime.PI: _sample()}
    )

    task = ab.build_report([outcome]).tasks[0]

    assert (task.issue, task.level, task.title) == (9, "L2", "Fix thing")
    assert task.errors == {ab.Runtime.COPILOT: "no run stored"}


# --- go bar ----------


def test_go_bar_met() -> None:
    copilot = _copilot_sample()
    pi = _tokens_sample(input=700, cache_read=3000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is True
    assert _criteria(report) == {"pass_count": True, "cache_read_share": True, "tokens": True}


def test_go_bar_pass_count_fails_alone() -> None:
    copilot = _copilot_sample()
    pi = _tokens_sample(input=700, cache_read=3000, passed=False)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": False, "cache_read_share": True, "tokens": True}


def test_go_bar_pass_count_needs_at_least_one_pi_pass() -> None:
    copilot = _tokens_sample(input=1000, cache_read=1000, passed=False)
    pi = _tokens_sample(input=700, cache_read=3000, passed=False)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": False, "cache_read_share": True, "tokens": True}


def test_go_bar_cache_share_fails_alone() -> None:
    copilot = _copilot_sample()
    pi = _tokens_sample(input=700, cache_read=1000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": True, "cache_read_share": False, "tokens": True}


def test_go_bar_tokens_fail_alone() -> None:
    copilot = _copilot_sample()
    pi = _tokens_sample(input=900, cache_read=3000)

    report = _bar_report(copilot, pi)

    assert report.verdict.meets is False
    assert _criteria(report) == {"pass_count": True, "cache_read_share": True, "tokens": False}


@pytest.mark.parametrize(
    ("pi_input", "expected"),
    [(175, True), (176, False)],
    ids=["exactly 15 points over", "just short of 15 points"],
)
def test_go_bar_share_needs_fifteen_points_over_copilot(pi_input: int, expected: bool) -> None:
    copilot = _tokens_sample(input=500, cache_read=500)
    pi = _tokens_sample(input=pi_input, cache_read=325)

    assert _criteria(_bar_report(copilot, pi))["cache_read_share"] is expected


@pytest.mark.parametrize(
    ("pi_cache_write", "expected"), [(600, True), (601, False)], ids=["at ceiling", "over ceiling"]
)
def test_go_bar_token_ceiling_counts_input_plus_cache_write(
    pi_cache_write: int, expected: bool
) -> None:
    copilot = _tokens_sample(input=1000, cache_read=1000, cache_write=1000)
    pi = _tokens_sample(input=1000, cache_read=9000, cache_write=pi_cache_write)

    assert _criteria(_bar_report(copilot, pi))["tokens"] is expected


@pytest.mark.parametrize(
    ("pi_input", "expected"), [(300, True), (301, False)], ids=["at 70 percent", "below 70 percent"]
)
def test_go_bar_pi_needs_70_percent_share_when_copilot_share_unavailable(
    pi_input: int, expected: bool
) -> None:
    copilot = _tokens_sample(input=1000, cache_read=None, cache_write=None)
    pi = _tokens_sample(input=pi_input, cache_read=700)

    report = _bar_report(copilot, pi)

    assert _criteria(report)["cache_read_share"] is expected
    assert report.verdict.meets is expected


def test_go_bar_fails_when_pi_reports_no_cache_counts() -> None:
    copilot = _copilot_sample()
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
    copilot = _copilot_sample()
    pi = _tokens_sample(input=700, cache_read=3000)
    skipped = ab.TaskOutcome(entry=_entry(2), samples={})

    report = ab.build_report([_outcome(1, copilot, pi), skipped])

    assert report.verdict.meets is True


def test_go_bar_is_not_met_with_no_completed_tasks() -> None:
    report = ab.build_report([ab.TaskOutcome(entry=_entry(1), samples={})])

    assert report.verdict.meets is False


def _bar_metrics(*, runs: int, input: int, cache_read: int) -> Any:
    return ab.RuntimeMetrics(
        runs=runs,
        passes=1,
        tokens=ab.TokenCounts(input=input, output=10, cache_read=cache_read, cache_write=0),
        cache_read_share=cache_read / (input + cache_read),
        cost=ab.RuntimeCost(),
        wall_seconds=1.0,
        repair_rounds=0,
    )


@pytest.mark.parametrize(("pi_runs", "expected"), [(0, False), (1, True)])
def test_go_bar_needs_at_least_one_pi_run_even_when_every_criterion_is_met(
    pi_runs: int, expected: bool
) -> None:
    copilot = _bar_metrics(runs=1, input=1000, cache_read=100)
    pi = _bar_metrics(runs=pi_runs, input=100, cache_read=900)

    verdict = ab.evaluate_go_bar(copilot, pi)

    assert all(item.met for item in verdict.criteria)
    assert verdict.meets is expected
    assert verdict.recommendation == ("recommended" if expected else "experimental")


# --- stored runs ----------


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


@pytest.mark.parametrize(
    ("verification_passed", "expected"), [(True, True), (False, False), (None, False)]
)
def test_sample_passes_only_with_green_verification(
    verification_passed: bool | None, expected: bool
) -> None:
    run = _run(WorkflowState.DONE)

    sample = ab.sample_from_run(run, verification_passed=verification_passed)

    assert sample.passed is expected


def test_sample_from_run_falls_back_to_updated_at_for_wall_time() -> None:
    run = _run(WorkflowState.FAILED, completed_at=None)

    assert ab.sample_from_run(run, verification_passed=None).wall_seconds == 30.0


def test_load_sample_reads_run_and_verification_from_store(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))
    store.save_artifact("run-1", VerificationReport(passed=False, confidence=0.5))

    sample = ab.load_sample(store, "run-1")

    assert sample.passed is False


def _triage(complexity: Complexity, risk: Risk) -> TriageResult:
    return TriageResult(
        factory_eligible=True,
        complexity=complexity,
        risk=risk,
        needs_research=False,
        confidence=0.9,
    )


def test_load_sample_reads_the_triage_level_and_risk(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))
    store.save_artifact("run-1", _triage(Complexity.L1, Risk.R2))

    sample = ab.load_sample(store, "run-1")

    assert sample.triage == ab.TriageLevel(complexity=Complexity.L1, risk=Risk.R2)


def test_load_sample_without_triage_has_no_triage_level(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))

    assert ab.load_sample(store, "run-1").triage is None


def test_load_sample_does_not_pass_a_run_without_a_verification_artifact(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run(WorkflowState.PR_READY))

    assert ab.load_sample(store, "run-1").passed is False


# --- rendering ----------


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _total_cell(markdown: str, runtime: Any, column: str) -> str:
    lines = markdown.splitlines()
    columns = _cells(next(line for line in lines if line.startswith("| Task |")))
    row = next(line for line in lines if line.startswith(f"| total | {runtime.value} |"))
    return _cells(row)[columns.index(column)]


def test_markdown_total_cache_share_is_unavailable_when_runtime_reported_none() -> None:
    copilot = _tokens_sample(input=1000, cache_read=None, cache_write=None)
    pi = _tokens_sample(input=500, cache_read=0)

    markdown = ab.render_markdown(_bar_report(copilot, pi))

    assert _total_cell(markdown, ab.Runtime.COPILOT, "Cache share") == "unavailable"
    assert _total_cell(markdown, ab.Runtime.PI, "Cache share") == "0.0%"


def test_markdown_total_cache_share_is_zero_percent_when_reported_zero() -> None:
    zero = _tokens_sample(input=500, cache_read=0)

    markdown = ab.render_markdown(_bar_report(zero, zero))

    assert _total_cell(markdown, ab.Runtime.COPILOT, "Cache share") == "0.0%"


def test_markdown_states_experimental_when_go_bar_unmet() -> None:
    zero = _tokens_sample(input=500, cache_read=0)

    lines = ab.render_markdown(_bar_report(zero, zero)).splitlines()

    assert "Go bar: pi does not meet the go bar: experimental, not recommended" in lines


def test_markdown_states_recommended_when_go_bar_met() -> None:
    report = _bar_report(_copilot_sample(), _tokens_sample(input=700, cache_read=3000))

    markdown = ab.render_markdown(report)

    assert "Go bar: pi meets the go bar: recommended" in markdown.splitlines()
    assert "not recommended" not in markdown
    assert "list-price estimate" in markdown


def test_markdown_marks_excluded_tasks_and_lists_their_errors_and_costs() -> None:
    clean = _outcome(
        1,
        _sample(
            wall=100.0,
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
        ),
        _sample(
            wall=80.0,
            invocations=(_invocation(1, input_tokens=600, list_price_estimate_usd=0.25),),
        ),
    )
    broken = ab.TaskOutcome(
        entry=ab.ManifestEntry(issue=9, base_sha=SHA_A, level="L2"),
        samples={
            ab.Runtime.COPILOT: _failed("no run stored (exit 1): boom"),
            ab.Runtime.PI: _sample(),
        },
    )

    lines = ab.render_markdown(ab.build_report([clean, broken])).splitlines()

    assert (
        "| #1 | copilot | 1/1 | 1000 | 200 | 500 | 100 | 31.2% "
        "| 3 premium requests / 7 nano-AIU | 100.0 | 0 |" in lines
    )
    assert (
        "| #1 | pi | 1/1 | 600 | unavailable | unavailable | unavailable | unavailable "
        "| $0.2500 list-price estimate | 80.0 | 0 |" in lines
    )
    assert (
        "| #9 L2 (excluded) | copilot | 0/1 | unavailable | unavailable | unavailable "
        "| unavailable | unavailable | unavailable | 0.0 | 0 |" in lines
    )
    assert lines[-3:] == ["## Errors", "", "- #9 L2 copilot: no run stored (exit 1): boom"]


def test_markdown_lists_skipped_tasks() -> None:
    report = ab.build_report([ab.TaskOutcome(entry=_entry(77), samples={})])

    assert "skipped" in ab.render_markdown(report)


# --- per-role breakdown ----------

_UNSUPPORTED = "not supported on this runtime"


def _role_call(
    role: AgentRole, model: str, *, success: bool = True, **usage: Any
) -> InvocationRecord:
    return _invocation(**usage).model_copy(
        update={
            "role": role,
            "model": model,
            "success": success,
            "failure_reason": None if success else _UNSUPPORTED,
        }
    )


def _role_rows(report: Any, runtime: Any) -> dict[tuple[AgentRole, str], Any]:
    return {(row.role, row.model): row for row in report.roles if row.runtime is runtime}


def _role_report(
    copilot_calls: tuple[InvocationRecord, ...], pi_calls: tuple[InvocationRecord, ...]
) -> Any:
    return ab.build_report(
        [_outcome(1, _sample(invocations=copilot_calls), _sample(invocations=pi_calls))]
    )


def test_role_breakdown_sums_calls_and_tokens_per_role_and_model() -> None:
    report = _role_report(
        (
            _role_call(
                AgentRole.IMPLEMENTER,
                "flash",
                input_tokens=100,
                output_tokens=5,
                cache_read_tokens=900,
                cache_write_tokens=0,
            ),
            _role_call(
                AgentRole.IMPLEMENTER,
                "flash",
                input_tokens=40,
                output_tokens=1,
                cache_read_tokens=100,
                cache_write_tokens=20,
            ),
        ),
        (_role_call(AgentRole.IMPLEMENTER, "flash", input_tokens=7, output_tokens=2),),
    )

    row = _role_rows(report, ab.Runtime.COPILOT)[(AgentRole.IMPLEMENTER, "flash")]
    assert row.calls == 2
    assert row.tokens == ab.TokenCounts(input=140, output=6, cache_read=1000, cache_write=20)


def test_role_breakdown_keeps_two_models_of_one_role_apart() -> None:
    report = _role_report(
        (
            _role_call(AgentRole.IMPLEMENTER, "flash", input_tokens=100),
            _role_call(AgentRole.IMPLEMENTER, "opus", input_tokens=30),
        ),
        (_role_call(AgentRole.IMPLEMENTER, "flash"),),
    )

    rows = _role_rows(report, ab.Runtime.COPILOT)
    assert rows[(AgentRole.IMPLEMENTER, "flash")].tokens.input == 100
    assert rows[(AgentRole.IMPLEMENTER, "opus")].tokens.input == 30


def test_role_breakdown_counts_failed_calls_without_usage_as_unavailable() -> None:
    report = _role_report(
        (_role_call(AgentRole.PLANNER, "opus", input_tokens=10),),
        (
            _role_call(AgentRole.RESEARCHER, "opus", success=False),
            _role_call(AgentRole.RESEARCHER, "opus", success=False),
        ),
    )

    researcher = _role_rows(report, ab.Runtime.PI)[(AgentRole.RESEARCHER, "opus")]
    assert (researcher.calls, researcher.failed_calls) == (2, 2)
    assert researcher.tokens == ab.TokenCounts()
    assert (AgentRole.RESEARCHER, "opus") not in _role_rows(report, ab.Runtime.COPILOT)


def test_role_breakdown_keeps_each_runtime_cost_unit() -> None:
    report = _role_report(
        (_role_call(AgentRole.REVIEWER, "sol", premium_requests=3.0, total_nano_aiu=500),),
        (_role_call(AgentRole.REVIEWER, "sol", list_price_estimate_usd=0.25),),
    )

    copilot = _role_rows(report, ab.Runtime.COPILOT)[(AgentRole.REVIEWER, "sol")]
    pi = _role_rows(report, ab.Runtime.PI)[(AgentRole.REVIEWER, "sol")]
    assert copilot.cost == ab.RuntimeCost(premium_requests=3.0, total_nano_aiu=500)
    assert pi.cost == ab.RuntimeCost(list_price_estimate_usd=0.25)


def test_role_breakdown_follows_workflow_order_then_model_then_copilot_first() -> None:
    report = _role_report(
        (
            _role_call(AgentRole.REVIEWER, "sol"),
            _role_call(AgentRole.IMPLEMENTER, "opus"),
            _role_call(AgentRole.IMPLEMENTER, "flash"),
        ),
        (_role_call(AgentRole.REVIEWER, "sol"), _role_call(AgentRole.TRIAGE, "terra")),
    )

    assert [(row.role, row.model, row.runtime) for row in report.roles] == [
        (AgentRole.TRIAGE, "terra", ab.Runtime.PI),
        (AgentRole.IMPLEMENTER, "flash", ab.Runtime.COPILOT),
        (AgentRole.IMPLEMENTER, "opus", ab.Runtime.COPILOT),
        (AgentRole.REVIEWER, "sol", ab.Runtime.COPILOT),
        (AgentRole.REVIEWER, "sol", ab.Runtime.PI),
    ]


def test_role_breakdown_leaves_out_excluded_tasks() -> None:
    clean = _outcome(1, _sample(), _sample())
    broken = _outcome(2, _sample(), _failed())

    report = ab.build_report([clean, broken])

    (row,) = _role_rows(report, ab.Runtime.COPILOT).values()
    assert row.calls == 1


def test_role_breakdown_is_empty_when_every_task_is_excluded() -> None:
    report = ab.build_report([_outcome(1, _sample(), _failed())])

    assert report.roles == ()
    assert "## By role" in ab.render_markdown(report)


def _role_cell(markdown: str, role: str, runtime: Any, column: str) -> str:
    lines = markdown.splitlines()
    section = lines[lines.index("## By role") :]
    columns = _cells(next(line for line in section if line.startswith("| Role |")))
    for line in section:
        cells = _cells(line)
        if len(cells) == len(columns) and (
            cells[columns.index("Role")],
            cells[columns.index("Runtime")],
        ) == (role, runtime.value):
            return cells[columns.index(column)]
    raise AssertionError(f"no By role row for {role} {runtime.value}")


def test_markdown_lists_each_role_with_model_tokens_and_cost_per_runtime() -> None:
    report = _role_report(
        (
            _role_call(
                AgentRole.PLANNER,
                "opus",
                input_tokens=10,
                output_tokens=2,
                cache_read_tokens=30,
                cache_write_tokens=4,
                premium_requests=15.0,
                total_nano_aiu=99,
            ),
        ),
        (_role_call(AgentRole.PLANNER, "opus", success=False),),
    )

    markdown = ab.render_markdown(report)

    copilot = {
        column: _role_cell(markdown, "PLANNER", ab.Runtime.COPILOT, column)
        for column in (
            "Model",
            "Calls",
            "Failed",
            "Input",
            "Output",
            "Cache read",
            "Cache write",
            "Cost",
        )
    }
    assert copilot == {
        "Model": "opus",
        "Calls": "1",
        "Failed": "0",
        "Input": "10",
        "Output": "2",
        "Cache read": "30",
        "Cache write": "4",
        "Cost": "15 premium requests / 99 nano-AIU",
    }
    assert _role_cell(markdown, "PLANNER", ab.Runtime.PI, "Failed") == "1"
    assert _role_cell(markdown, "PLANNER", ab.Runtime.PI, "Input") == "unavailable"
    assert _role_cell(markdown, "PLANNER", ab.Runtime.PI, "Cost") == "unavailable"


# --- triage agreement ----------


def _triaged(complexity: Complexity, risk: Risk) -> ab.RunSample:
    return _sample().model_copy(update={"triage": ab.TriageLevel(complexity=complexity, risk=risk)})


def test_task_triage_matches_when_both_runtimes_agree() -> None:
    report = ab.build_report(
        [_outcome(1, _triaged(Complexity.L1, Risk.R1), _triaged(Complexity.L1, Risk.R1))]
    )

    (task,) = report.tasks
    assert task.triage_mismatch is False


@pytest.mark.parametrize(
    ("pi_complexity", "pi_risk"), [(Complexity.L2, Risk.R1), (Complexity.L1, Risk.R2)]
)
def test_task_triage_mismatch_when_level_or_risk_differs(
    pi_complexity: Complexity, pi_risk: Risk
) -> None:
    report = ab.build_report(
        [_outcome(1, _triaged(Complexity.L1, Risk.R1), _triaged(pi_complexity, pi_risk))]
    )

    (task,) = report.tasks
    assert task.triage_mismatch is True
    assert task.triage[ab.Runtime.PI] == ab.TriageLevel(complexity=pi_complexity, risk=pi_risk)


@pytest.mark.parametrize(
    ("copilot", "pi", "expected"),
    [
        ((Complexity.L1, Risk.R1), (Complexity.L1, Risk.R1), False),
        ((Complexity.L1, Risk.R1), None, True),
        (None, (Complexity.L1, Risk.R1), True),
        (None, None, False),
    ],
)
def test_task_triage_mismatch_only_when_the_runtimes_differ(
    copilot: tuple[Complexity, Risk] | None,
    pi: tuple[Complexity, Risk] | None,
    expected: bool,
) -> None:
    def sample(level: tuple[Complexity, Risk] | None) -> Any:
        return _sample() if level is None else _triaged(*level)

    report = ab.build_report([_outcome(1, sample(copilot), sample(pi))])

    assert report.tasks[0].triage_mismatch is expected


def test_markdown_triage_leaves_out_skipped_tasks() -> None:
    report = ab.build_report(
        [
            _outcome(1, _triaged(Complexity.L0, Risk.R0), _triaged(Complexity.L0, Risk.R0)),
            ab.TaskOutcome(entry=_entry(99), samples={}),
        ]
    )

    lines = ab.render_markdown(report).splitlines()
    triage = lines[lines.index("## Triage") : lines.index("## Per task")]

    assert "| #1 | L0/R0 | L0/R0 | yes |" in triage
    assert not any(line.startswith("| #99 ") for line in triage)


def test_markdown_lists_triage_per_runtime_and_marks_a_mismatch() -> None:
    report = ab.build_report(
        [_outcome(22, _triaged(Complexity.L2, Risk.R2), _triaged(Complexity.L1, Risk.R1))]
    )

    lines = ab.render_markdown(report).splitlines()

    start = lines.index("## Triage")
    assert "| #22 | L2/R2 | L1/R1 | no |" in lines[start:]


def test_markdown_triage_is_unavailable_when_a_runtime_stored_none() -> None:
    report = ab.build_report([_outcome(5, _triaged(Complexity.L0, Risk.R0), _sample())])

    lines = ab.render_markdown(report).splitlines()

    assert "| #5 | L0/R0 | unavailable | no |" in lines[lines.index("## Triage") :]


# --- driver: budget and replay ----------


def _manifest(count: int = 3) -> Any:
    return ab.Manifest(
        tasks=tuple(
            ab.ManifestEntry(issue=10 + index, base_sha=SHA_A, level="L1") for index in range(count)
        )
    )


def _issue_text(issue: int) -> Any:
    return ab.IssueText(title=f"Issue {issue}", body=f"Body of {issue}")


class RecordingRunner:
    """Fake replay runner: returns a canned sample per runtime and records requests."""

    def __init__(self, samples: dict[Any, Any] | None = None) -> None:
        self.requests: list[Any] = []
        self._samples = samples or {}

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self._samples.get(request.runtime, _sample())


def _run_benchmark(
    runner: Any, *, budget: Any, manifest: Any | None = None, fetch: Any = _issue_text
) -> Any:
    return ab.run_benchmark(
        manifest or _manifest(), budget=budget, runner=runner, fetch_issue=fetch
    )


def test_every_task_runs_once_per_runtime_with_the_same_issue_text() -> None:
    runner = RecordingRunner()

    outcomes = _run_benchmark(runner, budget=ab.Budget(max_wall_seconds=1e9))

    # Both runtimes of one task run at the same time, so only the task order is fixed.
    assert sorted((r.entry.issue, r.runtime.value) for r in runner.requests) == [
        (10, "copilot"),
        (10, "pi"),
        (11, "copilot"),
        (11, "pi"),
        (12, "copilot"),
        (12, "pi"),
    ]
    assert [r.entry.issue for r in runner.requests] == [10, 10, 11, 11, 12, 12]
    assert all(set(outcome.samples) == set(ab.Runtime) for outcome in outcomes)
    first, second = runner.requests[:2]
    assert (first.title, first.description) == (second.title, second.description)
    assert (first.title, first.description) == ("Issue 10", "Body of 10")


def test_both_runtimes_of_a_task_run_at_the_same_time() -> None:
    both_started = threading.Barrier(len(ab.Runtime), timeout=5)

    def runner(request: Any) -> Any:
        # Sequential runs would wait here alone until the barrier times out.
        both_started.wait()
        return _sample()

    outcomes = _run_benchmark(
        runner, budget=ab.Budget(max_wall_seconds=1e9), manifest=_manifest(count=1)
    )

    assert set(outcomes[0].samples) == set(ab.Runtime)


def test_a_runner_that_raises_keeps_the_other_runtime_sample() -> None:
    def runner(request: Any) -> Any:
        if request.runtime is ab.Runtime.PI:
            raise RuntimeError("pi crashed")
        return _sample(wall=7.0)

    (outcome,) = _run_benchmark(
        runner, budget=ab.Budget(max_wall_seconds=1e9), manifest=_manifest(count=1)
    )

    assert outcome.samples[ab.Runtime.COPILOT].wall_seconds == 7.0
    assert outcome.samples[ab.Runtime.PI].error == "runner raised RuntimeError: pi crashed"
    assert outcome.excluded is True


def test_manifest_title_overrides_fetched_title() -> None:
    manifest = ab.Manifest(
        tasks=(ab.ManifestEntry(issue=5, base_sha=SHA_A, title="Manifest title"),)
    )
    runner = RecordingRunner()

    _run_benchmark(runner, budget=ab.Budget(max_wall_seconds=1e9), manifest=manifest)

    assert runner.requests[0].title == "Manifest title"
    assert runner.requests[0].description == "Body of 5"


def test_empty_issue_body_falls_back_to_the_title() -> None:
    runner = RecordingRunner()

    _run_benchmark(
        runner,
        budget=ab.Budget(max_wall_seconds=1e9),
        manifest=_manifest(1),
        fetch=lambda issue: ab.IssueText(title="Only title", body="  "),
    )

    assert runner.requests[0].description == "Only title"


def test_budget_reached_stops_new_tasks_and_marks_the_rest_skipped() -> None:
    expensive = _sample(invocations=(_invocation(premium_requests=3.0),))
    runner = RecordingRunner({ab.Runtime.COPILOT: expensive})

    outcomes = _run_benchmark(runner, budget=ab.Budget(max_copilot_premium_requests=3.0))

    assert [request.entry.issue for request in runner.requests] == [10, 10]
    assert [bool(outcome.samples) for outcome in outcomes] == [True, False, False]
    assert ab.build_report(outcomes).tasks[2].skipped is True


def test_pi_list_price_budget_stops_new_tasks() -> None:
    pricey = _sample(invocations=(_invocation(list_price_estimate_usd=0.6),))
    runner = RecordingRunner({ab.Runtime.PI: pricey})

    outcomes = _run_benchmark(runner, budget=ab.Budget(max_pi_usd=1.0))

    assert [bool(outcome.samples) for outcome in outcomes] == [True, True, False]


def test_wall_time_budget_sums_both_runtimes() -> None:
    runner = RecordingRunner(
        {ab.Runtime.COPILOT: _sample(wall=40.0), ab.Runtime.PI: _sample(wall=20.0)}
    )

    outcomes = _run_benchmark(runner, budget=ab.Budget(max_wall_seconds=100.0))

    assert [bool(outcome.samples) for outcome in outcomes] == [True, True, False]


def test_unreported_cost_never_trips_a_cost_budget() -> None:
    runner = RecordingRunner()

    outcomes = _run_benchmark(runner, budget=ab.Budget(max_copilot_premium_requests=0.1))

    assert all(outcome.samples for outcome in outcomes)


def test_issue_text_is_fetched_for_every_task_before_the_first_run() -> None:
    events: list[str] = []

    def fetch(issue: int) -> Any:
        events.append(f"fetch {issue}")
        return _issue_text(issue)

    def runner(request: Any) -> Any:
        events.append(f"run {request.entry.issue}")
        return _sample()

    _run_benchmark(runner, budget=ab.Budget(max_wall_seconds=1e9), fetch=fetch)

    assert events[:3] == ["fetch 10", "fetch 11", "fetch 12"]


def test_failed_issue_fetch_aborts_before_any_run() -> None:
    def fetch(issue: int) -> Any:
        raise ab.IssueFetchError(f"cannot fetch {issue}")

    runner = RecordingRunner()
    budget = ab.Budget(max_wall_seconds=1e9)

    with pytest.raises(ab.IssueFetchError):
        _run_benchmark(runner, budget=budget, fetch=fetch)

    assert runner.requests == []


def test_budget_needs_at_least_one_limit() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ab.Budget()


def test_budget_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        ab.Budget(max_pi_usd=0.0)


# --- driver: local-only guard --------------------------------------------------------------------


def test_default_config_is_local_only() -> None:
    from software_agent_factory.config import load_config

    ab.ensure_local_only(load_config(None))


@pytest.mark.parametrize("section", ab._GITHUB_SECTIONS)
def test_config_that_can_reach_github_is_refused(section: str) -> None:
    from software_agent_factory.config import load_config

    config = load_config(None)
    changed = getattr(config, section).model_copy(update={"enabled": True})
    unsafe = config.model_copy(update={section: changed})

    with pytest.raises(ab.UnsafeConfigError, match=section):
        ab.ensure_local_only(unsafe)


# --- driver: real runner over an injected command runner ----------


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _is_factory_run(args: list[str]) -> bool:
    return args[1:4] == ["-m", "software_agent_factory", "run"]


class FakeCommands:
    """Injected command runner: no git, no gh and no factory process is started.

    ``git clone`` is mimicked by creating the target directory, so cleanup can be checked.
    """

    def __init__(self, *, state: WorkflowState | None = WorkflowState.PR_READY) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self.factory_envs: list[dict[str, str] | None] = []
        self.gh_config_listings: list[list[str]] = []
        self._state = state

    def __call__(self, args: Any, cwd: Path, env: Any = None) -> Any:
        import subprocess

        argv = list(args)
        self.calls.append((argv, cwd))
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        if _is_factory_run(argv):
            self._record_factory_env(env)
            self._store_run(argv)
        return subprocess.CompletedProcess(argv, 0, "", "boom on stderr")

    def _record_factory_env(self, env: Any) -> None:
        self.factory_envs.append(None if env is None else dict(env))
        if env is not None:
            self.gh_config_listings.append(sorted(os.listdir(env["GH_CONFIG_DIR"])))

    def _store_run(self, argv: list[str]) -> None:
        if self._state is None:
            return
        store = FileRunStore(_option(argv, "--data-dir"))
        store.save_run(_run(self._state, invocation_records=[_invocation(1, premium_requests=2.0)]))
        store.save_artifact("run-1", VerificationReport(passed=True, confidence=0.9))

    def factory_runs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if _is_factory_run(argv)]

    def git_calls(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if argv[0] == "git"]


def _runner(tmp_path: Path, commands: Any, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "repo": tmp_path / "source",
        "workdir": tmp_path / "work",
        "model_profile": "default",
        "config": None,
        "run_command": commands,
    }
    options.update(overrides)
    return ab.CliReplayRunner(**options)


def _request(runtime: Any, issue: int = 10) -> Any:
    return ab.ReplayRequest(
        entry=ab.ManifestEntry(issue=issue, base_sha=SHA_B),
        runtime=runtime,
        title="T",
        description="D",
    )


def _task_dir(tmp_path: Path, runtime: str = "pi", issue: int = 10) -> Path:
    return tmp_path / "work" / runtime / f"issue-{issue}"


def test_runner_resolves_relative_repo_and_workdir_before_cloning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    commands = FakeCommands()

    _runner(tmp_path, commands, repo=Path("source"), workdir=Path("work"))(_request(ab.Runtime.PI))

    clone_call = commands.git_calls()[0]
    assert clone_call[-2:] == [str(tmp_path / "source"), str(_task_dir(tmp_path) / "repo")]


def test_runner_passes_a_relative_config_as_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    commands = FakeCommands()

    _runner(tmp_path, commands, config=Path("local.yaml"))(_request(ab.Runtime.PI))

    assert _option(commands.factory_runs()[0], "--config") == str(tmp_path / "local.yaml")


def test_runner_replays_at_base_sha_in_a_throwaway_shared_clone(tmp_path: Path) -> None:
    commands = FakeCommands()

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    clone = _task_dir(tmp_path) / "repo"
    assert commands.git_calls() == [
        ["git", "clone", "--shared", "--no-checkout", str(tmp_path / "source"), str(clone)],
        ["git", "-C", str(clone), "checkout", "--detach", SHA_B],
        ["git", "-C", str(clone), "remote", "set-url", "--push", "origin", "no-push://disabled"],
    ]
    (factory_run,) = commands.factory_runs()
    assert _option(factory_run, "--repo") == str(clone)
    assert _option(factory_run, "--data-dir") == str(_task_dir(tmp_path) / "data")
    assert _option(factory_run, "--runtime") == "pi"
    assert (_option(factory_run, "--title"), _option(factory_run, "--description")) == ("T", "D")
    assert sample.passed is True
    assert sample.invocations[0].usage.premium_requests == 2.0


def test_runner_never_gives_the_user_repo_to_worktree_or_branch_commands(tmp_path: Path) -> None:
    commands = FakeCommands()
    source = str(tmp_path / "source")

    _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    for argv in commands.git_calls():
        is_clone = argv[1] == "clone"
        assert (source in argv) is is_clone
        assert "worktree" not in argv
        assert "-b" not in argv
        assert "branch" not in argv
    (factory_run,) = commands.factory_runs()
    assert source not in factory_run
    assert all(cwd != tmp_path / "source" for _, cwd in commands.calls)


def test_runner_removes_the_clone_after_a_run(tmp_path: Path) -> None:
    commands = FakeCommands()

    _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    assert not (_task_dir(tmp_path) / "repo").exists()
    assert (_task_dir(tmp_path) / "data").exists()


@pytest.mark.parametrize("failure", [RuntimeError("kaboom"), KeyboardInterrupt()])
def test_runner_removes_the_clone_even_when_factory_run_is_interrupted(
    tmp_path: Path, failure: BaseException
) -> None:
    class Explodes(FakeCommands):
        def __call__(self, args: Any, cwd: Path, env: Any = None) -> Any:
            if _is_factory_run(list(args)):
                raise failure
            return super().__call__(args, cwd, env)

    run = _runner(tmp_path, Explodes())
    request = _request(ab.Runtime.PI)

    with pytest.raises(type(failure)):
        run(request)

    assert not (_task_dir(tmp_path) / "repo").exists()


@pytest.mark.parametrize("failing_step", ["clone", "checkout", "remote"])
def test_runner_reports_a_failed_clone_step_without_running_factory(
    tmp_path: Path, failing_step: str
) -> None:
    import subprocess

    class StepFails(FakeCommands):
        def __call__(self, args: Any, cwd: Path, env: Any = None) -> Any:
            done = super().__call__(args, cwd, env)
            if failing_step in list(args):
                return subprocess.CompletedProcess(list(args), 128, "", "bad object")
            return done

    commands = StepFails()

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    assert commands.factory_runs() == []
    assert sample.passed is False
    assert sample.error is not None
    assert "bad object" in sample.error
    assert not (_task_dir(tmp_path) / "repo").exists()


def test_runner_refuses_a_non_empty_task_directory(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    task_dir.mkdir(parents=True)
    (task_dir / "old-run.json").write_text("{}", encoding="utf-8")
    commands = FakeCommands()

    run = _runner(tmp_path, commands)
    request = _request(ab.Runtime.PI)

    with pytest.raises(ab.WorkdirError, match="issue-10"):
        run(request)

    assert commands.calls == []
    assert (task_dir / "old-run.json").exists()


def test_runner_gives_both_runtimes_the_same_models_and_isolated_state(tmp_path: Path) -> None:
    commands = FakeCommands()
    runner = _runner(tmp_path, commands, model_profile="economy", config=tmp_path / "c.yaml")

    runner(_request(ab.Runtime.COPILOT))
    runner(_request(ab.Runtime.PI))

    copilot_run, pi_run = commands.factory_runs()
    for option in ("--model-profile", "--config", "--title", "--description"):
        assert _option(copilot_run, option) == _option(pi_run, option)
    assert _option(copilot_run, "--model-profile") == "economy"
    assert _option(copilot_run, "--runtime") == "copilot"
    assert _option(copilot_run, "--data-dir") != _option(pi_run, "--data-dir")
    assert _option(copilot_run, "--repo") != _option(pi_run, "--repo")
    assert _option(copilot_run, "--work-item-id") != _option(pi_run, "--work-item-id")


def test_work_item_ids_differ_between_benchmark_invocations(tmp_path: Path) -> None:
    ids = []
    for index in range(2):
        commands = FakeCommands()
        runner = _runner(tmp_path / f"invocation-{index}", commands)
        runner(_request(ab.Runtime.PI))
        ids.append(_option(commands.factory_runs()[0], "--work-item-id"))

    assert ids[0] != ids[1]
    assert all(item.endswith("-pi-issue-10") for item in ids)


def test_work_item_id_carries_the_given_invocation_id(tmp_path: Path) -> None:
    commands = FakeCommands()

    _runner(tmp_path, commands, invocation_id="abc123")(_request(ab.Runtime.PI))

    assert _option(commands.factory_runs()[0], "--work-item-id") == (
        "runtime-ab-abc123-pi-issue-10"
    )


def test_runner_reports_failure_when_no_run_was_stored(tmp_path: Path) -> None:
    commands = FakeCommands(state=None)

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.COPILOT))

    assert sample.passed is False
    assert sample.invocations == ()
    assert sample.error is not None
    assert "boom on stderr" in sample.error


GITHUB_CREDENTIAL_VARS = sorted(GITHUB_CREDENTIAL_ENV_VARS | {"GH_TOKEN", "GH_ENTERPRISE_TOKEN"})


@pytest.mark.parametrize("name", GITHUB_CREDENTIAL_VARS)
def test_factory_run_env_has_no_github_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "secret-value")
    commands = FakeCommands()

    _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    (env,) = commands.factory_envs
    assert env is not None
    assert name not in env


def test_factory_run_env_keeps_copilot_token_and_uses_an_empty_gh_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_gh_config = tmp_path / "real-gh"
    real_gh_config.mkdir()
    (real_gh_config / "hosts.yml").write_text("token: x", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(real_gh_config))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    commands = FakeCommands()

    _runner(tmp_path, commands)(_request(ab.Runtime.COPILOT))

    (env,) = commands.factory_envs
    assert env is not None
    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-token"
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "pi-agent")
    assert env["GH_CONFIG_DIR"] != str(real_gh_config)
    assert commands.gh_config_listings == [[]]


def test_default_config_option_is_omitted_when_not_given(tmp_path: Path) -> None:
    commands = FakeCommands()

    _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    assert "--config" not in commands.factory_runs()[0]


def test_verify_base_commits_names_the_missing_commit(tmp_path: Path) -> None:
    import subprocess

    def commands(args: Any, cwd: Path) -> Any:
        missing = str(args[-1]).startswith(SHA_B)
        return subprocess.CompletedProcess(list(args), 1 if missing else 0, "", "")

    manifest = ab.Manifest(
        tasks=(
            ab.ManifestEntry(issue=1, base_sha=SHA_A),
            ab.ManifestEntry(issue=2, base_sha=SHA_B),
        )
    )

    with pytest.raises(ab.ManifestError, match="issue 2"):
        ab.verify_base_commits(manifest, tmp_path, commands)


def test_issue_fetcher_reads_title_and_body_with_gh(tmp_path: Path) -> None:
    import subprocess

    seen: list[list[str]] = []

    def commands(args: Any, cwd: Path) -> Any:
        seen.append(list(args))
        payload = json.dumps({"title": "T", "body": "B"})
        return subprocess.CompletedProcess(list(args), 0, payload, "")

    text = ab.GhIssueFetcher(tmp_path, commands)(42)

    assert (text.title, text.body) == ("T", "B")
    assert seen == [["gh", "issue", "view", "42", "--json", "title,body"]]


def test_issue_fetcher_raises_when_gh_fails(tmp_path: Path) -> None:
    import subprocess

    def commands(args: Any, cwd: Path) -> Any:
        return subprocess.CompletedProcess(list(args), 1, "", "not found")

    fetcher = ab.GhIssueFetcher(tmp_path, commands)

    with pytest.raises(ab.IssueFetchError, match="issue 42"):
        fetcher(42)


def test_issue_fetcher_names_the_issue_when_gh_prints_bad_json(tmp_path: Path) -> None:
    import subprocess

    def commands(args: Any, cwd: Path, env: Any = None) -> Any:
        return subprocess.CompletedProcess(list(args), 0, "not json", "")

    fetcher = ab.GhIssueFetcher(tmp_path, commands)

    with pytest.raises(ab.IssueFetchError, match="issue 42: unexpected gh output"):
        fetcher(42)


def test_tail_keeps_the_last_500_characters_without_surrounding_whitespace() -> None:
    text = "  head" + "x" * 600 + "end \n"

    tail = ab._tail(text)

    assert len(tail) == 500
    assert tail.endswith("xxxend")
    assert "head" not in tail


# --- real command runner ----------


def _patch_subprocess_run(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> list[dict[str, Any]]:
    import subprocess

    calls: list[dict[str, Any]] = []

    def fake_run(args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, **kwargs})
        return behaviour(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_command_runner_turns_a_timeout_into_exit_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def times_out(args: Any) -> Any:
        raise subprocess.TimeoutExpired(args, 5)

    _patch_subprocess_run(monkeypatch, times_out)

    result = ab.subprocess_command_runner(5)(["sleep", "9"], tmp_path)

    assert result.returncode == 124
    assert result.stderr == "timed out after 5s"


def test_command_runner_turns_a_missing_program_into_exit_127(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(args: Any) -> Any:
        raise FileNotFoundError("no such program: nope")

    _patch_subprocess_run(monkeypatch, missing)

    result = ab.subprocess_command_runner(5)(["nope"], tmp_path)

    assert result.returncode == 127
    assert "no such program: nope" in result.stderr


def test_command_runner_passes_cwd_timeout_and_env_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def fine(args: Any) -> Any:
        return subprocess.CompletedProcess(args, 0, "", "")

    calls = _patch_subprocess_run(monkeypatch, fine)
    run = ab.subprocess_command_runner(7)

    run(["a"], tmp_path)
    run(["b"], tmp_path, {"ONLY": "this"})

    inherited, replaced = calls
    assert (inherited["cwd"], inherited["timeout"], inherited["env"]) == (tmp_path, 7, None)
    assert replaced["env"] == {"ONLY": "this"}


# --- command line ----------


def _cli(
    tmp_path: Path,
    *extra: str,
    commands: Any | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> tuple[int, Any]:
    manifest = _write_manifest(
        tmp_path, tasks or [{"issue": 1, "base_sha": SHA_A}, {"issue": 2, "base_sha": SHA_B}]
    )
    fake = commands or FakeCommands()
    argv = [
        "--manifest", str(manifest),
        "--repo", str(tmp_path / "source"),
        "--workdir", str(tmp_path / "work"),
        "--out", str(tmp_path / "out" / "report.json"),
        *extra,
    ]  # fmt: skip
    return ab.main(argv, run_command=fake, fetch_issue=_issue_text), fake


def test_cli_writes_json_and_markdown_reports(tmp_path: Path) -> None:
    code, commands = _cli(tmp_path, "--max-wall-seconds", "1000000")

    assert code == 0
    payload = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert [task["issue"] for task in payload["tasks"]] == [1, 2]
    assert payload["verdict"]["recommendation"] == "experimental"
    assert payload["verdict"]["meets"] is False
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "Go bar" in markdown
    assert len(commands.factory_runs()) == 4


def test_cli_marks_remaining_tasks_skipped_when_budget_is_reached(tmp_path: Path) -> None:
    code, commands = _cli(tmp_path, "--max-copilot-premium-requests", "2")

    assert code == 0
    payload = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert [task["skipped"] for task in payload["tasks"]] == [False, True]
    assert len(commands.factory_runs()) == 2


def test_cli_invalid_manifest_fails_before_any_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, commands = _cli(tmp_path, "--max-wall-seconds", "10", tasks=[{"issue": 42}])

    assert code == 2
    assert commands.calls == []
    assert "issue 42" in capsys.readouterr().err


def test_cli_refuses_a_used_workdir_before_any_paid_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    used = tmp_path / "work" / "pi" / "issue-2"
    used.mkdir(parents=True)
    (used / "data").mkdir()

    code, commands = _cli(tmp_path, "--max-wall-seconds", "10")

    assert code == 2
    assert commands.factory_runs() == []
    assert "issue-2" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content", [None, "- a\n- list\n", "a: [unclosed"], ids=["missing", "not a mapping", "bad yaml"]
)
def test_cli_refuses_a_config_that_cannot_be_loaded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    config = tmp_path / "broken.yaml"
    if content is not None:
        config.write_text(content, encoding="utf-8")

    code, commands = _cli(tmp_path, "--max-wall-seconds", "10", "--config", str(config))

    assert code == 2
    assert commands.calls == []
    assert "config could not be loaded" in capsys.readouterr().err


def test_cli_refuses_a_manifest_outside_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = _outside_cwd(tmp_path, monkeypatch)
    commands = FakeCommands()
    argv = [
        "--manifest", str(_write_manifest(outside, [{"issue": 1, "base_sha": SHA_A}])),
        "--out", "report.json",
        "--max-wall-seconds", "10",
    ]  # fmt: skip

    code = ab.main(argv, run_command=commands, fetch_issue=_issue_text)

    assert code == 2
    assert commands.calls == []
    assert "outside the working directory" in capsys.readouterr().err


def test_cli_refuses_an_output_outside_the_working_directory_before_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = _outside_cwd(tmp_path, monkeypatch)
    commands = FakeCommands()
    argv = [
        "--manifest", str(_write_manifest(Path("."), [{"issue": 1, "base_sha": SHA_A}])),
        "--out", str(outside / "report.json"),
        "--max-wall-seconds", "10",
    ]  # fmt: skip

    code = ab.main(argv, run_command=commands, fetch_issue=_issue_text)

    assert code == 2
    assert commands.calls == []
    assert list(outside.iterdir()) == []
    assert "outside the working directory" in capsys.readouterr().err


def test_write_reports_refuses_a_path_outside_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _outside_cwd(tmp_path, monkeypatch)
    report = ab.build_report([])

    with pytest.raises(ValueError, match="outside the working directory"):
        ab._write_reports(report, outside / "report.json")

    assert list(outside.iterdir()) == []


def test_cli_requires_a_budget(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _cli(tmp_path)

    assert excinfo.value.code == 2


def test_cli_refuses_a_config_that_can_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "publish.yaml"
    config.write_text("pull_request:\n  enabled: true\n", encoding="utf-8")

    code, commands = _cli(tmp_path, "--max-wall-seconds", "10", "--config", str(config))

    assert code == 2
    assert commands.calls == []
    assert "pull_request" in capsys.readouterr().err


def test_cli_stops_when_a_base_commit_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    class NoCommits(FakeCommands):
        def __call__(self, args: Any, cwd: Path, env: Any = None) -> Any:
            super().__call__(args, cwd, env)
            return subprocess.CompletedProcess(list(args), 1, "", "")

    code, commands = _cli(tmp_path, "--max-wall-seconds", "10", commands=NoCommits())

    assert code == 2
    assert commands.factory_runs() == []
    assert "issue 1" in capsys.readouterr().err


# --- shipped manifest ----------------------------------------------------------------------------


MANIFEST_PATH = ROOT / "scripts" / "performance" / "runtime_ab_manifest.json"
PI_WORK_NUMBERS = range(60, 73)


def test_shipped_manifest_validates_and_spans_every_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    manifest = ab.load_manifest(MANIFEST_PATH)

    assert 8 <= len(manifest.tasks) <= 12
    assert {entry.level for entry in manifest.tasks} == {"L0", "L1", "L2", "L3"}
    assert all(entry.title for entry in manifest.tasks)


def test_shipped_manifest_leaves_out_the_pi_work_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    numbers = {entry.issue for entry in ab.load_manifest(MANIFEST_PATH).tasks}

    assert numbers.isdisjoint(PI_WORK_NUMBERS)
