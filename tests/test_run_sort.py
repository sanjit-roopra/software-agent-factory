"""Server-side run list sort: each column both ways, ties kept newest first."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from software_agent_factory.models import (
    RUN_LIST_SORTS,
    AgentRole,
    FactoryRun,
    InvocationRecord,
    UsageMetrics,
    WorkflowState,
)
from software_agent_factory.run_sort import sort_runs

T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=20)


def _call(usage: UsageMetrics) -> InvocationRecord:
    return InvocationRecord(
        invocation_number=1,
        role=AgentRole.IMPLEMENTER,
        model="m",
        reasoning="medium",
        started_at=T0,
        completed_at=T0 + timedelta(seconds=1),
        success=True,
        usage=usage,
    )


def _run(
    run_id: str,
    state: WorkflowState,
    started_minutes_ago: int,
    stopped_after: int | None,
    usage: UsageMetrics | None,
) -> FactoryRun:
    created = T0 - timedelta(minutes=started_minutes_ago)
    stopped = created + timedelta(minutes=stopped_after or 0)
    return FactoryRun(
        id=run_id,
        work_item_id=f"WI-{run_id}",
        state=state,
        created_at=created,
        updated_at=stopped,
        completed_at=stopped if state is WorkflowState.DONE else None,
        invocation_records=[] if usage is None else [_call(usage)],
    )


# Newest first, as the scan gives them. r1, r2 and r4 ran 10 minutes; r3 still runs (22).
# r1 reports $2 as nano-AIU and r3 $2 as a list price, so they tie on cost.
RUNS = [
    _run("r1", WorkflowState.DONE, 0, 10, UsageMetrics(total_nano_aiu=200_000_000_000)),
    _run("r2", WorkflowState.FAILED, 1, 10, None),
    _run("r3", WorkflowState.IMPLEMENTING, 2, None, UsageMetrics(list_price_estimate_usd=2.0)),
    _run("r4", WorkflowState.DONE, 3, 10, UsageMetrics(list_price_estimate_usd=1.0)),
]
TITLES = {"r1": "beta", "r3": "Alpha", "r4": "gamma"}


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("newest", ["r1", "r2", "r3", "r4"]),
        ("oldest", ["r4", "r3", "r2", "r1"]),
        ("duration", ["r1", "r2", "r4", "r3"]),
        ("-duration", ["r3", "r1", "r2", "r4"]),
        ("cost", ["r4", "r1", "r3", "r2"]),
        ("-cost", ["r1", "r3", "r4", "r2"]),
        ("state", ["r3", "r1", "r4", "r2"]),
        ("-state", ["r2", "r1", "r4", "r3"]),
        ("title", ["r3", "r1", "r4", "r2"]),
        ("-title", ["r4", "r1", "r3", "r2"]),
    ],
)
def test_each_sort_orders_the_runs_and_ties_keep_newest_first(
    sort: str, expected: list[str]
) -> None:
    assert sort in RUN_LIST_SORTS
    ordered = sort_runs(RUNS, sort, NOW, TITLES.get)
    assert [run.id for run in ordered] == expected


def test_every_sort_is_covered() -> None:
    assert len(RUN_LIST_SORTS) == 10
