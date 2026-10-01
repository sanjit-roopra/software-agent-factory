"""Run totals by token class and cost unit (slice 2, step 2.3 of #80)."""

from __future__ import annotations

from typing import Any

import pytest

from software_agent_factory.dashboard.aggregate import (
    COST_UNIT_FIELDS,
    TOKEN_CLASS_FIELDS,
    run_totals,
)
from software_agent_factory.dashboard.sanitize import USAGE_FIELDS


def _call(
    status: str | None = "SUCCESS", duration_ms: int | None = 1000, **usage: Any
) -> dict[str, Any]:
    return {"status": status, "duration_ms": duration_ms, "usage": usage}


def _figure(total: float | int | None, reported_count: int) -> dict[str, Any]:
    return {"total": total, "reported_count": reported_count}


def test_mixed_cost_units_stay_three_separate_figures() -> None:
    totals = run_totals(
        [
            _call(total_premium_request_cost=1, usage_value_usd=0.5),
            _call(total_premium_request_cost=2, usage_value_usd=1.0),
            _call(list_price_estimate_usd=0.2),
        ]
    )

    assert totals["costs"] == {
        "total_premium_request_cost": _figure(3, 2),
        "usage_value_usd": _figure(1.5, 2),
        "list_price_estimate_usd": _figure(pytest.approx(0.2), 1),
    }


def test_tokens_sum_per_class() -> None:
    totals = run_totals(
        [
            _call(input_tokens=100, output_tokens=10, cache_read_tokens=40),
            _call(input_tokens=50, output_tokens=5),
        ]
    )

    assert totals["tokens"] == {
        "input_tokens": _figure(150, 2),
        "output_tokens": _figure(15, 2),
        "reasoning_tokens": _figure(None, 0),
        "cache_read_tokens": _figure(40, 1),
        "cache_write_tokens": _figure(None, 0),
    }


def test_no_calls_gives_every_figure_null_with_zero_reported() -> None:
    totals = run_totals([])

    assert totals["calls"] == 0
    assert totals["failed_calls"] == _figure(None, 0)
    assert totals["duration_ms"] == _figure(None, 0)
    for field in TOKEN_CLASS_FIELDS:
        assert totals["tokens"][field] == _figure(None, 0)
    for field in COST_UNIT_FIELDS:
        assert totals["costs"][field] == _figure(None, 0)


def test_partial_reporting_counts_two_of_three() -> None:
    totals = run_totals(
        [
            _call(cache_read_tokens=30),
            _call(cache_read_tokens=70),
            _call(cache_read_tokens=None),
        ]
    )

    assert totals["calls"] == 3
    assert totals["tokens"]["cache_read_tokens"] == _figure(100, 2)


def test_a_reported_zero_counts_as_reported() -> None:
    totals = run_totals([_call(cache_read_tokens=0, usage_value_usd=0.0), _call()])

    assert totals["tokens"]["cache_read_tokens"] == _figure(0, 1)
    assert totals["costs"]["usage_value_usd"] == _figure(0.0, 1)


def test_failed_calls_and_duration_count_only_calls_that_reported_them() -> None:
    totals = run_totals(
        [
            _call("SUCCESS", 1500),
            _call("FAILED", 2500),
            _call("FAILED", None),
            _call(None, None),
        ]
    )

    assert totals["calls"] == 4
    assert totals["failed_calls"] == _figure(2, 3)
    assert totals["duration_ms"] == _figure(4000, 2)


def test_unreported_status_is_not_counted_as_failed_or_succeeded() -> None:
    totals = run_totals([_call(None, None)])

    assert totals["failed_calls"] == _figure(None, 0)


@pytest.mark.parametrize("bad", [True, "12", [1], {"a": 1}])
def test_non_numeric_values_are_unreported(bad: Any) -> None:
    totals = run_totals([_call(input_tokens=bad)])

    assert totals["tokens"]["input_tokens"] == _figure(None, 0)


def test_a_call_without_usage_or_with_a_non_object_usage_reports_nothing() -> None:
    totals = run_totals([{"status": "SUCCESS"}, {"usage": "oops"}])

    assert totals["calls"] == 2
    assert totals["tokens"]["input_tokens"] == _figure(None, 0)


def test_field_names_come_from_the_usage_allowlist() -> None:
    # usage_value_usd is derived from nano AIU, never read from the provider.
    assert set(TOKEN_CLASS_FIELDS) <= USAGE_FIELDS
    assert set(COST_UNIT_FIELDS) - {"usage_value_usd"} <= USAGE_FIELDS
    assert "usage_value_usd" not in USAGE_FIELDS
