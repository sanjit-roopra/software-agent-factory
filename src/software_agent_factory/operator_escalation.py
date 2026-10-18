"""The start record of an operator escalation (ADR-055).

``factory escalate`` and the dashboard's escalate route both record here that a process now
escalates a run, so the two cannot write the record two ways.
"""

from __future__ import annotations

from .models import OperatorEscalation
from .observability import load_operator_escalation
from .resume import escalation_route
from .store import OPERATOR_ESCALATION_FILENAME, FileRunStore


def save_escalation_start(
    store: FileRunStore, run_id: str, note: str, pid: int
) -> OperatorEscalation | None:
    """Record that process ``pid`` escalates ``run_id`` now, and return the record.

    Returns ``None`` and writes nothing when another process still escalates the run. When
    ``pid`` already wrote its record, that record comes back unchanged: the dashboard records
    its child after starting it, and a child that already finished must keep its outcome. A
    missing run raises ``FileNotFoundError``.
    """
    run = store.load_run(run_id)
    last = load_operator_escalation(store, run_id)
    if last is not None and last.pid == pid:
        return last
    if last is not None and last.status == "running":
        return None
    record = OperatorEscalation(
        from_route=None if run.route_decision is None else run.route_decision.effective_route,
        to_route=escalation_route(run),
        note=note,
        pid=pid,
    )
    store.save_artifact(run_id, record, OPERATOR_ESCALATION_FILENAME)
    return record
