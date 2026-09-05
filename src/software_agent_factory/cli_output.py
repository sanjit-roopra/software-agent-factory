"""Human-readable rendering for the ``factory`` CLI (``PLAN.md`` Phase 15).

Every ``factory`` command that reports structured state offers two shapes:
``--json`` (the typed model, dumped verbatim) and a default human-readable
form produced here. Keeping the human form in pure, side-effect-free
functions that take a typed report and return lines means:

- the CLI command bodies stay about argument parsing and wiring;
- output is unit-testable without invoking a command, a store or a server;
- no rendering path can accidentally mutate a run, a workspace or
  configuration, because none of these functions receive anything mutable.

Nothing here loads configuration, touches the filesystem or starts a process,
and nothing here renders a dashboard token, a command log, a diff or an agent
prompt.
"""

from __future__ import annotations

from .doctor import CheckStatus, DoctorReport
from .observability import MonitoringSnapshot, OperationalHealthReport
from .service_install import ServiceStatus

__all__ = [
    "STATUS_SYMBOLS",
    "render_doctor_report",
    "render_service_status",
    "render_status_report",
]

#: One stable ASCII marker per check status. ASCII rather than emoji or box
#: drawing so output stays readable in a launchd log, a pipe and a plain
#: terminal alike.
STATUS_SYMBOLS: dict[CheckStatus, str] = {
    CheckStatus.OK: "ok  ",
    CheckStatus.WARNING: "warn",
    CheckStatus.ERROR: "FAIL",
}


def render_doctor_report(report: DoctorReport) -> list[str]:
    """Render a :class:`DoctorReport` as aligned ``status name  message``
    lines, followed by remediation for anything that is not ``OK`` and a
    one-line verdict."""
    lines: list[str] = []
    width = max((len(check.name) for check in report.checks), default=0)
    for check in report.checks:
        lines.append(f"{STATUS_SYMBOLS[check.status]}  {check.name.ljust(width)}  {check.message}")
        if check.remediation is not None and check.status is not CheckStatus.OK:
            lines.append(f"{' ' * 6}{' ' * width}  -> {check.remediation}")

    errors = sum(1 for check in report.checks if check.status is CheckStatus.ERROR)
    warnings = sum(1 for check in report.checks if check.status is CheckStatus.WARNING)
    lines.append("")
    verdict = "ok" if report.success else "failed"
    lines.append(f"doctor: {verdict} ({errors} error(s), {warnings} warning(s))")
    return lines


def _format_rate(value: float | None) -> str:
    return "unknown" if value is None else f"{value * 100:.0f}%"


def _format_seconds(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0f}s"


def render_status_report(
    snapshot: MonitoringSnapshot, health: OperationalHealthReport
) -> list[str]:
    """Render the read-only monitoring snapshot and operational health as a
    compact operator summary.

    Degraded and truncated scans are reported explicitly and first, never
    smoothed over: a partial scan that silently looked like a complete one
    would be worse than no summary at all (``ADR-017``).
    """
    counts = snapshot.counts
    metrics = snapshot.metrics
    lines = [
        f"generated at: {snapshot.generated_at.isoformat()}",
        (
            f"runs: {snapshot.total_runs} total, {snapshot.scanned_runs} scanned"
            f"{' (scan truncated)' if snapshot.scan_truncated else ''}"
        ),
        (
            f"states: {counts.succeeded} succeeded, {counts.escalated} escalated, "
            f"{counts.failed} failed, {counts.active} active "
            f"({counts.stale_active} stale)"
        ),
        (
            f"attempts: {metrics.total_attempts} total, "
            f"{metrics.implementation_attempts} implementation, "
            f"{metrics.ci_repair_attempts} CI repair, "
            f"{metrics.scope_replans} scope replan(s)"
        ),
        (
            "first-pass success: "
            f"{_format_rate(metrics.first_pass_success.rate)} "
            f"({metrics.first_pass_success.numerator}/"
            f"{metrics.first_pass_success.denominator})"
        ),
        (
            f"completed run duration: {metrics.completed_run_durations.count} run(s), "
            f"avg {_format_seconds(metrics.completed_run_durations.average_seconds)}, "
            f"max {_format_seconds(metrics.completed_run_durations.max_seconds)}"
        ),
        f"stale threshold: {snapshot.stale_after_seconds:.0f}s",
    ]

    if snapshot.unreadable_runs:
        lines.append(f"unreadable runs: {snapshot.unreadable_runs}")

    lines.append("")
    lines.append("health:")
    lines.append(f"  stale runs: {len(health.stale_runs)}")
    for finding in health.stale_runs:
        lines.append(f"    {finding.run_id}  {finding.state}  idle {finding.idle_seconds:.0f}s")
    if health.lock_check_supported:
        lines.append(
            f"  stale locks: {len(health.stale_locks)} (of {health.locks_checked} checked)"
        )
        for lock in health.stale_locks:
            lines.append(f"    {lock.lock_name}")
    else:
        lines.append("  stale locks: unsupported on this platform")
    lines.append(
        f"  orphaned workspaces: {len(health.orphaned_workspaces)} "
        f"(of {health.workspaces_checked} checked)"
    )
    for workspace in health.orphaned_workspaces:
        lines.append(f"    {workspace.workspace_name}")

    degraded_reasons = list(dict.fromkeys([*snapshot.degraded_reasons, *health.degraded_reasons]))
    if snapshot.degraded or health.degraded:
        lines.append("")
        lines.append("status: DEGRADED (this report is partial)")
        for reason in degraded_reasons:
            lines.append(f"  - {reason}")
    else:
        lines.append("")
        lines.append("status: complete")

    if snapshot.runs:
        lines.append("")
        lines.append(
            f"runs (showing {snapshot.page.returned} of {snapshot.page.total}, "
            f"offset {snapshot.page.offset}):"
        )
        for run in snapshot.runs:
            marker = " STALE" if run.is_stale else ""
            lines.append(
                f"  {run.run_id}  {run.state}  {run.work_item_id}  "
                f"attempts {run.attempt_count}  idle {run.idle_seconds:.0f}s{marker}"
            )
        if snapshot.page.has_more:
            lines.append("  ... more runs available (use --offset)")
    return lines


def render_service_status(status: ServiceStatus) -> list[str]:
    """Render one :class:`ServiceStatus` for ``factory service status``."""
    lines = [
        f"label: {status.label}",
        f"plist: {status.plist_path}",
        f"installed: {'yes' if status.installed else 'no'}",
        f"loaded: {'yes' if status.loaded else 'no'}",
    ]
    if not status.installed:
        lines.append("detail: no LaunchAgent plist found for this label")
    elif not status.loaded and status.detail:
        lines.append(f"detail: {status.detail.splitlines()[0]}")
    return lines
