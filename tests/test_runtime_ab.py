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
    with pytest.raises(ab.ManifestError, match="no tasks"):
        ab.load_manifest(_write_manifest(tmp_path, []))


def test_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ab.ManifestError, match="not valid JSON"):
        ab.load_manifest(path)


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


# --- go bar ----------


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


# --- rendering ----------


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

    assert [(r.entry.issue, r.runtime) for r in runner.requests] == [
        (10, ab.Runtime.COPILOT),
        (10, ab.Runtime.PI),
        (11, ab.Runtime.COPILOT),
        (11, ab.Runtime.PI),
        (12, ab.Runtime.COPILOT),
        (12, ab.Runtime.PI),
    ]
    assert all(set(outcome.samples) == set(ab.Runtime) for outcome in outcomes)
    first, second = runner.requests[:2]
    assert (first.title, first.description) == (second.title, second.description)
    assert (first.title, first.description) == ("Issue 10", "Body of 10")


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

    with pytest.raises(ab.IssueFetchError):
        _run_benchmark(runner, budget=ab.Budget(max_wall_seconds=1e9), fetch=fetch)

    assert runner.requests == []


def test_budget_needs_at_least_one_positive_limit() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ab.Budget()
    with pytest.raises(ValueError, match="greater than 0"):
        ab.Budget(max_pi_usd=0.0)


# --- driver: local-only guard --------------------------------------------------------------------


def test_default_config_is_local_only() -> None:
    from software_agent_factory.config import load_config

    ab.ensure_local_only(load_config(None))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("pull_request", "enabled"),
        ("merge", "enabled"),
        ("escalation", "enabled"),
        ("scheduler", "enabled"),
    ],
)
def test_config_that_can_reach_github_is_refused(section: str, field: str) -> None:
    from software_agent_factory.config import load_config

    config = load_config(None)
    changed = getattr(config, section).model_copy(update={field: True})
    unsafe = config.model_copy(update={section: changed})

    with pytest.raises(ab.UnsafeConfigError, match=section):
        ab.ensure_local_only(unsafe)


# --- driver: real runner over an injected command runner ----------


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _is_factory_run(args: list[str]) -> bool:
    return args[1:4] == ["-m", "software_agent_factory", "run"]


class FakeCommands:
    """Injected command runner: no git, no gh and no factory process is started."""

    def __init__(self, *, state: WorkflowState | None = WorkflowState.PR_READY) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._state = state

    def __call__(self, args: Any, cwd: Path) -> Any:
        import subprocess

        argv = list(args)
        self.calls.append((argv, cwd))
        if _is_factory_run(argv) and self._state is not None:
            FileRunStore(_option(argv, "--data-dir")).save_run(
                _run(self._state, invocation_records=[_invocation(1, premium_requests=2.0)])
            )
        return subprocess.CompletedProcess(argv, 0, "", "boom on stderr")

    def factory_runs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls if _is_factory_run(argv)]


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


def test_runner_replays_at_base_sha_in_an_isolated_worktree_and_data_dir(tmp_path: Path) -> None:
    commands = FakeCommands()

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    added, *_, removed = [argv for argv, _ in commands.calls if argv[0] == "git"]
    worktree = tmp_path / "work" / "pi" / "issue-10" / "repo"
    assert added == [
        "git",
        "-C",
        str(tmp_path / "source"),
        "worktree",
        "add",
        "--detach",
        str(worktree),
        SHA_B,
    ]
    assert removed[4:6] == ["remove", "--force"]
    (factory_run,) = commands.factory_runs()
    assert _option(factory_run, "--repo") == str(worktree)
    assert _option(factory_run, "--data-dir") == str(tmp_path / "work" / "pi" / "issue-10" / "data")
    assert _option(factory_run, "--runtime") == "pi"
    assert (_option(factory_run, "--title"), _option(factory_run, "--description")) == ("T", "D")
    assert sample.passed is True
    assert sample.invocations[0].usage.premium_requests == 2.0


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
    assert _option(copilot_run, "--work-item-id") != _option(pi_run, "--work-item-id")


def test_runner_reports_failure_when_no_run_was_stored(tmp_path: Path) -> None:
    commands = FakeCommands(state=None)

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.COPILOT))

    assert sample.passed is False
    assert sample.invocations == ()
    assert sample.error is not None
    assert "boom on stderr" in sample.error


def test_runner_reports_a_failed_worktree_add_without_running_factory(tmp_path: Path) -> None:
    import subprocess

    class GitFails(FakeCommands):
        def __call__(self, args: Any, cwd: Path) -> Any:
            super().__call__(args, cwd)
            return subprocess.CompletedProcess(list(args), 128, "", "bad object")

    commands = GitFails()

    sample = _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    assert commands.factory_runs() == []
    assert sample.passed is False
    assert sample.error is not None
    assert "bad object" in sample.error


def test_runner_removes_the_worktree_even_when_factory_run_raises(tmp_path: Path) -> None:
    class Explodes(FakeCommands):
        def __call__(self, args: Any, cwd: Path) -> Any:
            if _is_factory_run(list(args)):
                raise RuntimeError("kaboom")
            return super().__call__(args, cwd)

    commands = Explodes()

    with pytest.raises(RuntimeError, match="kaboom"):
        _runner(tmp_path, commands)(_request(ab.Runtime.PI))

    assert any(argv[4:6] == ["remove", "--force"] for argv, _ in commands.calls)


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

    with pytest.raises(ab.IssueFetchError, match="issue 42"):
        ab.GhIssueFetcher(tmp_path, commands)(42)


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
    assert payload["verdict"]["recommendation"] in {"recommended", "experimental"}
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
        def __call__(self, args: Any, cwd: Path) -> Any:
            super().__call__(args, cwd)
            return subprocess.CompletedProcess(list(args), 1, "", "")

    code, commands = _cli(tmp_path, "--max-wall-seconds", "10", commands=NoCommits())

    assert code == 2
    assert commands.factory_runs() == []
    assert "issue 1" in capsys.readouterr().err


# --- shipped manifest ----------------------------------------------------------------------------


MANIFEST_PATH = ROOT / "scripts" / "performance" / "runtime_ab_manifest.json"
PI_WORK_NUMBERS = range(60, 73)


def test_shipped_manifest_validates_and_spans_every_level() -> None:
    manifest = ab.load_manifest(MANIFEST_PATH)

    assert 8 <= len(manifest.tasks) <= 12
    assert {entry.level for entry in manifest.tasks} == {"L0", "L1", "L2", "L3"}
    assert all(entry.title for entry in manifest.tasks)


def test_shipped_manifest_leaves_out_the_pi_work_itself() -> None:
    numbers = {entry.issue for entry in ab.load_manifest(MANIFEST_PATH).tasks}

    assert numbers.isdisjoint(PI_WORK_NUMBERS)
