"""Order the run list by one of its columns.

The dashboard run list pages on the server, so the server sorts before it pages. The runs
come in newest first. Python's sort is stable, so runs with the same value keep that
order in both directions, and a run without a value goes last in both directions.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from .lifecycle import is_run_finished
from .models import FactoryRun, WorkflowState
from .run_metrics import summarize_usage
from .usage_models import NANO_AIU_PER_USD

#: Reads the title of a run by its id, or ``None`` when it has none.
TitleOf = Callable[[str], str | None]


def _duration_seconds(run: FactoryRun, now: datetime) -> float:
    """Wall-clock time from the start, as the run list shows it: up to now while it runs."""
    if is_run_finished(run) or run.state is WorkflowState.NEEDS_HUMAN:
        end = run.completed_at or run.updated_at
    else:
        end = now
    return max((end - run.created_at).total_seconds(), 0.0)


def _cost_usd(run: FactoryRun) -> float | None:
    """The USD figure the run list shows: the usage value when reported, else the list price."""
    usage = summarize_usage(run.invocation_records)
    if usage.total_nano_aiu is not None:
        return usage.total_nano_aiu / NANO_AIU_PER_USD
    return usage.list_price_estimate_usd


def _title(run: FactoryRun, title_of: TitleOf) -> str | None:
    title = title_of(run.id)
    return title.casefold() if title else None


def _sort_value(
    key: str, run: FactoryRun, now: datetime, title_of: TitleOf
) -> tuple[float, str] | None:
    """The value of ``key`` for ``run``: a number, or text after a zero. ``None`` goes last."""
    if key == "duration":
        return (_duration_seconds(run, now), "")
    if key == "cost":
        cost = _cost_usd(run)
        return None if cost is None else (cost, "")
    if key == "state":
        return (0.0, run.state.value)
    title = _title(run, title_of)
    return None if title is None else (0.0, title)


def sort_runs(
    runs: list[FactoryRun], sort: str, now: datetime, title_of: TitleOf
) -> list[FactoryRun]:
    """``runs``, newest first, in the order ``sort`` names (see ``RUN_LIST_SORTS``)."""
    if sort == "newest":
        return runs
    if sort == "oldest":
        return runs[::-1]
    descending = sort.startswith("-")
    key = sort.removeprefix("-")
    present: list[tuple[tuple[float, str], FactoryRun]] = []
    missing: list[FactoryRun] = []
    for run in runs:
        value = _sort_value(key, run, now, title_of)
        if value is None:
            missing.append(run)
        else:
            present.append((value, run))
    present.sort(key=lambda pair: pair[0], reverse=descending)
    return [run for _, run in present] + missing
