"""The agent calls in progress across every run, for the dashboard's "Now running" strip.

:func:`software_agent_factory.observability.build_monitoring_snapshot` lists them from
the runs it already scanned, so the strip costs no extra store read beyond a title.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .lifecycle import is_run_finished
from .models import AgentRole, FactoryRun, ModelBase, UtcDateTime

#: The most calls the strip lists; one run has at most one call in progress.
MAX_ACTIVE_CALLS = 20


class ActiveCallBrief(ModelBase):
    """One call in progress: its run, the run title, its role and model, and when it began."""

    run_id: str
    title: str | None
    role: AgentRole
    model: str
    started_at: UtcDateTime


def active_call_briefs(
    runs: Iterable[FactoryRun], title_of: Callable[[str], str | None]
) -> list[ActiveCallBrief]:
    """The call in progress of each unfinished run, the newest call first.

    ``title_of`` reads a run's title; it is called only for the runs listed.
    """
    calls = [
        (run.id, call)
        for run in runs
        if (call := run.active_invocation) is not None and not is_run_finished(run)
    ]
    calls.sort(key=lambda pair: pair[1].started_at, reverse=True)
    return [
        ActiveCallBrief(
            run_id=run_id,
            title=title_of(run_id),
            role=call.role,
            model=call.model,
            started_at=call.started_at,
        )
        for run_id, call in calls[:MAX_ACTIVE_CALLS]
    ]
