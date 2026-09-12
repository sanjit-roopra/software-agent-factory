"""Phase 15.5 local monitoring and health (``PLAN.md`` Phase 15.5).

Per ``docs/architecture.md`` ("Health and metrics"), persisted run artifacts
are the source of truth: health and metrics are *derived on demand* from the
run store rather than accumulated in a separate counter store or time-series
database. There is deliberately no second metrics truth source here -- this
module only reads what :class:`~software_agent_factory.store.FileRunStore`
already persists (``FactoryRun``, ``WorkItem``, ``TriageResult``). It has no
import-time dependency on ``workflow.py`` (the orchestration/mutation
authority): a run's terminal/finished status is a small, stable domain fact
duplicated locally (see ``_is_run_finished`` below) precisely so this
read-only monitoring path -- and the read-only Phase 15.11 dashboard route
that will call it -- never transitively pulls in
``WorkflowController``/agents/publishing/routing/workspace code merely to
classify a run.

Two public surfaces are provided:

- :func:`build_monitoring_snapshot` -- a pure, read-only function that turns
  a run store into a typed, JSON-serializable :class:`MonitoringSnapshot`:
  paginated run summaries, store-wide counts (succeeded, escalated, failed,
  active, stale-active), attempt tallies by role/model, and aggregate
  metrics (attempts per run, first-pass success rate, scope replans, CI
  repair attempts, completed-run duration). Rendered by ``factory status``
  and by the Phase 15.11 read-only dashboard -- neither of which this module
  imports or depends on.
- :func:`build_run_detail` -- the single-run counterpart, returning a
  :class:`RunDetail` (summary fields, completion facts and the attempt
  history) or ``None`` for a run that does not exist or cannot be read.
- :func:`build_operational_health` -- a pure, read-only function reporting
  three distinct, actionable operational findings: stale non-terminal runs,
  stale (holder-less) workspace lock files under ``data_dir/locks``, and
  orphaned workspace directories under ``data_dir/workspaces`` not
  referenced by any scanned run. Strictly non-destructive: a lock probe
  releases immediately and never deletes the lock file, and an orphaned
  workspace is only named, never removed.
- :func:`configure_factory_logging` -- a lightweight, stdlib-only structured
  JSON logging setup with a bounded ``RotatingFileHandler`` under the
  configured data directory, so operators get durable, size-bounded logs
  while the LaunchAgent sends stdout/stderr to ``/dev/null`` instead of an
  unbounded launchd output file (``PLAN.md`` Phase 15.2).

Health is always derived at read time and never persisted as a boolean: a
run's "stale" or "finished" status is recomputed from its timestamps against
the caller-supplied ``now``/``stale_after`` on every call, and
``MonitoringSnapshot.degraded`` reflects only what was actually unreadable
during *this* call. Nothing here mutates a run, a workspace, or
configuration.

Bounded work per request: run *directory* enumeration (a filesystem
``iterdir``/``stat`` pass) may be broad -- it is only ever cheap metadata,
never a JSON parse -- but the number of ``run.json`` files actually opened
and parsed per call is hard-capped by ``max_scanned_runs`` (default
:data:`DEFAULT_MAX_SCANNED_RUNS`). When a store holds more run directories
than the cap, the newest ones (by ``run.json`` modification time, falling
back to directory modification time) are preferred, and the snapshot says so
honestly via ``scanned_runs``/``scan_truncated``/``degraded_reasons`` rather
than silently reporting store-wide counts that quietly exclude older runs.

Runtime-reported usage is summarized only from persisted
``InvocationRecord`` values. Missing fields stay unknown, and no value is
reconstructed from a price table or converted to USD.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from pydantic import Field, ValidationError

from .models import (
    AgentRole,
    AttemptBudget,
    AttemptTrigger,
    Complexity,
    ContextTier,
    FactoryRun,
    InvocationRecord,
    ModelBase,
    PerformanceRecord,
    ReviewFindingCategory,
    ReviewImpasse,
    Risk,
    TriageResult,
    UsageMetrics,
    UtcDateTime,
    VersionedModel,
    WorkflowState,
    WorkItem,
    utc_now,
)
from .verification import redact_secrets

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux only per AGENTS.md
    fcntl = None  # type: ignore[assignment]

#: Whether this platform supports the ``fcntl.flock`` liveness probe
#: :func:`build_operational_health` uses to detect a stale (holder-less)
#: lock file. Windows has no ``fcntl``; on such a platform lock staleness
#: checks are skipped explicitly (``lock_check_supported=False``) rather
#: than raising or silently reporting zero stale locks as if they were
#: checked.
_FCNTL_AVAILABLE = fcntl is not None

__all__ = [
    "RunStoreProtocol",
    "RunScanResult",
    "scan_readable_runs",
    "RunScanCache",
    "DEFAULT_SCAN_CACHE_TTL",
    "PageMeta",
    "RunStateCounts",
    "RunSummary",
    "FirstPassSuccessMetric",
    "DurationSummary",
    "ReworkMetrics",
    "PerformanceSummary",
    "AggregateMetrics",
    "MonitoringSnapshot",
    "RunAttemptSummary",
    "RunInvocationSummary",
    "UsageSummary",
    "RunDetail",
    "RunGuidance",
    "StaleRunFinding",
    "StaleLockFinding",
    "OrphanedWorkspaceFinding",
    "OperationalHealthReport",
    "DEFAULT_STALE_AFTER",
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_MAX_SCANNED_RUNS",
    "PRUNE_LOCK_PREFIX",
    "build_monitoring_snapshot",
    "build_operational_health",
    "build_run_detail",
    "configure_factory_logging",
    "log_run_event",
]

#: Matches ``SchedulerConfig.stall_timeout_seconds``'s own default (15
#: minutes): a reasonable default definition of "no heartbeat/activity in a
#: while" for a run that a scheduler is not actively supervising (e.g. one
#: started by a bare ``factory run`` invocation, with no daemon involved).
#: Callers with their own configured stall timeout should pass it explicitly
#: instead of relying on this default.
DEFAULT_STALE_AFTER = timedelta(seconds=900)

DEFAULT_PAGE_LIMIT = 100

#: Conservative hard cap on how many ``run.json`` files one call to
#: :func:`build_monitoring_snapshot` will open and parse, independent of
#: ``limit``/``offset``. Directory enumeration to *find* candidates is cheap
#: (filesystem metadata only) and stays unbounded; this cap only bounds the
#: expensive part (JSON parsing + Pydantic validation), so a store with an
#: unbounded number of historical runs can never turn one dashboard/CLI
#: request into unbounded work.
DEFAULT_MAX_SCANNED_RUNS = 1000

LOG_DIRNAME = "logs"
LOG_FILENAME = "factory.log"
DEFAULT_LOG_MAX_BYTES = 5_000_000
DEFAULT_LOG_BACKUP_COUNT = 5

#: Prefix of the durable per-source-repository worktree administration lock
#: created by ``workspace.GitWorktreeWorkspace._prune_lock``. Duplicated as a
#: literal (rather than imported) for the same reason ``_TERMINAL_STATES`` is:
#: this read-only module must not pull in the workspace/orchestration module
#: tree. See :func:`_find_stale_locks` for why these files are never reported
#: as stale.
PRUNE_LOCK_PREFIX = "prune-"

#: Marks a handler this module attached, so :func:`configure_factory_logging`
#: can be called repeatedly (once per entry point) without stacking
#: duplicate handlers on the same logger.
_HANDLER_MARKER = "_software_agent_factory_structured_handler"

#: Terminal ``WorkflowState`` values, duplicated from ``workflow.py``'s
#: ``TERMINAL_STATES`` rather than imported (see module docstring). Kept as
#: a small, explicit constant instead of any dynamic derivation so a change
#: to the canonical set in ``workflow.py`` is a conscious, grep-able,
#: two-place edit rather than a silent drift.
_TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.DONE, WorkflowState.NEEDS_HUMAN, WorkflowState.FAILED}
)


def _is_run_finished(run: FactoryRun) -> bool:
    """Local, dependency-free copy of ``workflow.is_run_finished``.

    True when a persisted run needs no further factory work: every
    ``_TERMINAL_STATES`` member always qualifies, and a ``PR_READY`` run
    qualifies only once the controller has explicitly finalized it
    (``completed_at`` stamped) -- see ``workflow.py``'s own docstring for why
    an un-finalized ``PR_READY`` is deliberately *not* finished. Duplicated
    here (instead of importing ``workflow.is_run_finished``) so this
    read-only monitoring module never transitively imports the orchestration
    module tree that pulls in agents/config/github/governance/publishing/
    routing/workspace.
    """
    if run.state in _TERMINAL_STATES:
        return True
    return run.state is WorkflowState.PR_READY and run.completed_at is not None


def _classify_run(run: FactoryRun) -> str:
    """One of ``"escalated"``, ``"failed"``, ``"succeeded"``, ``"active"``.

    The single three-way branch :class:`RunStateCounts`, aggregate metrics,
    and stale-run health findings all build on, factored out once so every
    consumer agrees on exactly the same classification rule.
    """
    if run.state is WorkflowState.NEEDS_HUMAN:
        return "escalated"
    if run.state is WorkflowState.FAILED:
        return "failed"
    if _is_run_finished(run):
        return "succeeded"
    return "active"


# ---------------------------------------------------------------------------
# Store surface
# ---------------------------------------------------------------------------


class RunStoreProtocol(Protocol):
    """The narrow, read-only slice of :class:`FileRunStore` this module
    needs.

    Deliberately smaller than the full store API (no ``save_run``,
    ``save_artifact``, or patch access) so tests can supply a lightweight
    in-memory fake instead of exercising the filesystem store, and so this
    module has no way to accidentally write through a store it was only
    handed to observe. Any object satisfying this shape -- including the
    real ``FileRunStore`` -- works with :func:`build_monitoring_snapshot`.
    """

    @property
    def runs_dir(self) -> Path: ...

    def load_run(self, run_id: str) -> FactoryRun: ...

    def load_artifact(
        self,
        run_id: str,
        artifact_type: type[Any],
        filename: str | None = None,
        *,
        attempt: int | None = None,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Snapshot models
# ---------------------------------------------------------------------------


class PageMeta(ModelBase):
    """Pagination metadata for the ``runs`` slice of a snapshot."""

    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    returned: int = Field(ge=0)
    total: int = Field(ge=0)
    has_more: bool


class RunStateCounts(ModelBase):
    """Store-wide run counts, computed over every *scanned* run.

    ``succeeded`` covers both terminal ``DONE`` runs and ``PR_READY`` runs
    the controller has explicitly finalized (``completed_at`` stamped) --
    see the local ``_is_run_finished`` predicate above. ``active`` is every
    scanned run that is not yet finished and not escalated/failed;
    ``stale_active`` is the subset of ``active`` whose most recent
    heartbeat/activity/update timestamp is older than ``stale_after``. When
    ``MonitoringSnapshot.scan_truncated`` is true these counts reflect only
    the scanned subset, not the whole store.
    """

    succeeded: int = Field(ge=0)
    escalated: int = Field(ge=0)
    failed: int = Field(ge=0)
    active: int = Field(ge=0)
    stale_active: int = Field(ge=0)


class RunSummary(ModelBase):
    """One dashboard-safe summary of a persisted run.

    Deliberately excludes anything that could carry repository content,
    prompts, tool output, diffs, or tokens/secrets: no command logs, no
    patch text, no agent reasoning, no raw artifact bodies. ``title`` is
    redacted with the same credential patterns applied to captured command
    output (``verification.redact_secrets``) as defense in depth against an
    accidentally-pasted secret in a work item title.
    """

    run_id: str
    work_item_id: str
    title: str | None
    state: WorkflowState
    complexity: Complexity | None
    risk: Risk | None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    age_seconds: float = Field(ge=0.0)
    idle_seconds: float = Field(ge=0.0)
    attempt_count: int = Field(ge=0)
    invocation_count: int = Field(ge=0)
    implementation_attempts: int = Field(ge=0)
    ci_repair_attempts: int = Field(ge=0)
    usage: UsageSummary
    is_finished: bool
    is_stale: bool
    review_status: str | None = None
    performance: PerformanceRecord | None = None


class RunAttemptSummary(ModelBase):
    """One attempt, reduced to fields that are safe to render anywhere.

    Deliberately excludes ``reasoning`` and ``failure_reason``: both are
    unbounded free text produced by (or about) an agent, so either could
    quote repository content, and nothing downstream can vet them. The
    dashboard's own allowlist (``dashboard.sanitize.ATTEMPT_FIELDS``) drops
    them a second time; this model makes sure they are never carried that
    far in the first place.
    """

    attempt_number: int = Field(ge=1)
    role: AgentRole
    model: str
    budget: AttemptBudget
    triggered_by: AttemptTrigger
    outcome: str
    started_at: UtcDateTime
    completed_at: UtcDateTime


class UsageSummary(ModelBase):
    """Safe totals of explicitly reported invocation usage."""

    invocation_count: int = Field(ge=0)
    reported_invocations: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    premium_requests: float | None = Field(default=None, ge=0.0)
    premium_request_cost: float | None = Field(default=None, ge=0.0)
    total_nano_aiu: int | None = Field(default=None, ge=0)


class RunInvocationSummary(ModelBase):
    """One dashboard-safe invocation with typed runtime-reported usage."""

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: str
    model: str
    context_tier: ContextTier
    success: bool
    started_at: UtcDateTime
    completed_at: UtcDateTime
    attempt_number: int | None = Field(default=None, ge=1)
    usage: UsageMetrics | None = None
    performance: PerformanceRecord | None = None


class ActiveInvocationSummary(ModelBase):
    """Dashboard-safe status for an invocation that has not completed."""

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: str
    model: str
    context_tier: ContextTier
    status: str
    started_at: UtcDateTime
    attempt_number: int | None = Field(default=None, ge=1)


class RunGuidance(ModelBase):
    status: str
    reason_code: str
    summary: str
    next_action: str
    artifact: str | None = None
    finding_count: int = Field(default=0, ge=0)
    finding_ids: list[str] = Field(default_factory=list, max_length=12)
    category_counts: dict[ReviewFindingCategory, int] = Field(default_factory=dict)


class RunDetail(ModelBase):
    """One run's read-only detail view: a :class:`RunSummary` plus completion
    facts and the attempt history.

    Same data-minimization rule as :class:`RunSummary`, applied to a single
    run: no command logs, no patch text, no prompts, no agent reasoning, no
    raw artifact bodies and no ``failure_reason``. ``commit_sha`` and
    ``pull_request_url`` are controller-produced identifiers, not repository
    content, so both are included.
    """

    run_id: str
    work_item_id: str
    title: str | None
    state: WorkflowState
    complexity: Complexity | None
    risk: Risk | None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    completed_at: UtcDateTime | None
    age_seconds: float = Field(ge=0.0)
    idle_seconds: float = Field(ge=0.0)
    attempt_count: int = Field(ge=0)
    invocation_count: int = Field(ge=0)
    implementation_attempts: int = Field(ge=0)
    ci_repair_attempts: int = Field(ge=0)
    usage: UsageSummary
    is_finished: bool
    is_stale: bool
    review_status: str | None = None
    performance: PerformanceRecord | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None
    attempts: list[RunAttemptSummary] = Field(default_factory=list)
    invocations: list[RunInvocationSummary] = Field(default_factory=list)
    active_invocation: ActiveInvocationSummary | None = None
    guidance: RunGuidance | None = None


class FirstPassSuccessMetric(ModelBase):
    """Deterministic first-pass success measurement.

    A succeeded run (``_classify_run(run) == "succeeded"``: ``DONE`` or a
    finalized ``PR_READY``) counts as a first-pass success when it needed
    exactly one non-polish ``AttemptBudget.IMPLEMENTATION`` attempt to reach
    ``PR_READY`` -- i.e. the initial implementer attempt was never repaired
    for an implementer failure, a deterministic verification failure, an
    independent-reviewer rejection, or a bounded scope-drift replan. Every
    one of those repair paths in ``WorkflowController._drive_to_pr_ready``
    re-invokes the implementer under the same ``IMPLEMENTATION`` budget
    (``workflow.py``). The optional, planned ``POLISH`` attempt is excluded
    because it is not a recovery from first-pass failure.

    ``denominator`` is every succeeded run in the scanned set; ``rate`` is
    ``None`` (never ``0.0``) when the denominator is zero, since an
    undefined rate must never be reported as "0% first-pass success".
    """

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0.0, le=1.0)


class DurationSummary(ModelBase):
    """Summary of a duration distribution, in seconds. Every ``*_seconds``
    field is ``None`` (never ``0.0``) when ``count == 0``: an empty
    distribution has no minimum/maximum/average, and reporting zero would
    misrepresent "no data" as "instantaneous"."""

    count: int = Field(ge=0)
    min_seconds: float | None = Field(default=None, ge=0.0)
    max_seconds: float | None = Field(default=None, ge=0.0)
    average_seconds: float | None = Field(default=None, ge=0.0)


class ReworkMetrics(ModelBase):
    """Aggregate gate failure and rework metrics across scanned runs."""

    total_gate_failures: int = Field(default=0, ge=0)
    verification_gate_failures: int = Field(default=0, ge=0)
    review_gate_failures: int = Field(default=0, ge=0)
    total_rework_attempts: int = Field(default=0, ge=0)
    runs_with_rework: int = Field(default=0, ge=0)
    rework_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class PerformanceSummary(ModelBase):
    """Aggregate performance telemetry across scanned runs."""

    stage_durations: dict[str, DurationSummary] = Field(default_factory=dict)
    operation_durations: dict[str, DurationSummary] = Field(default_factory=dict)
    counters: dict[str, int] = Field(default_factory=dict)
    total_prompt_chars: int = Field(default=0, ge=0)
    total_response_chars: int = Field(default=0, ge=0)
    rework: ReworkMetrics = Field(default_factory=ReworkMetrics)


class AggregateMetrics(ModelBase):
    """Store-wide (scanned-subset) aggregate metrics: pure functions of
    persisted ``FactoryRun``/``AttemptRecord``/``InvocationRecord`` data.
    Usage totals include only explicitly reported values; missing telemetry
    remains unknown and is never reconstructed from a price table (ADR-017).

    ``completed_run_durations`` covers every *finished* run in the scanned
    set (``DONE``, ``NEEDS_HUMAN``, ``FAILED``, or a finalized ``PR_READY``
    -- i.e. every run for which ``completed_at`` is stamped), measuring
    ``completed_at - created_at``; it is not restricted to successes, since
    overall cycle time is useful regardless of outcome.
    """

    total_attempts: int = Field(ge=0)
    average_attempts_per_run: float | None = Field(default=None, ge=0.0)
    implementation_attempts: int = Field(ge=0)
    ci_repair_attempts: int = Field(ge=0)
    scope_replans: int = Field(ge=0)
    first_pass_success: FirstPassSuccessMetric
    completed_run_durations: DurationSummary
    usage: UsageSummary
    performance: PerformanceSummary = Field(default_factory=PerformanceSummary)


class MonitoringSnapshot(VersionedModel):
    """Typed, JSON-serializable snapshot for later CLI/dashboard use.

    ``total_runs`` is the number of run *directories* discovered under
    ``store.runs_dir`` (cheap filesystem enumeration, never JSON-parsed).
    ``scanned_runs`` is how many of those directories actually had their
    ``run.json`` opened and parsed this call, bounded by ``max_scanned_runs``
    passed to :func:`build_monitoring_snapshot`; ``scan_truncated`` is true
    whenever ``total_runs > scanned_runs`` because the store exceeded that
    cap, in which case ``counts``/``attempts_by_*``/``metrics``/``page`` all
    reflect only the newest scanned subset. ``unreadable_runs`` counts,
    among the scanned subset, directories whose ``run.json`` was missing or
    failed to parse. ``page`` describes only the paginated ``runs`` slice
    actually returned.

    ``degraded`` is true whenever at least one scanned run directory could
    not be read, or the scan itself was truncated -- an honest signal, never
    suppressed to claim a healthy/complete store when something on disk is
    actually corrupt, missing, or simply too large to fully scan in one call.
    """

    generated_at: UtcDateTime
    stale_after_seconds: float = Field(gt=0.0)
    max_scanned_runs: int = Field(ge=1)
    total_runs: int = Field(ge=0)
    scanned_runs: int = Field(ge=0)
    scan_truncated: bool
    unreadable_runs: int = Field(ge=0)
    degraded: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    counts: RunStateCounts
    attempts_by_role: dict[str, int] = Field(default_factory=dict)
    attempts_by_model: dict[str, int] = Field(default_factory=dict)
    metrics: AggregateMetrics
    page: PageMeta
    runs: list[RunSummary] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def build_monitoring_snapshot(
    store: RunStoreProtocol,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
    scan: RunScanResult | None = None,
) -> MonitoringSnapshot:
    """Derive a paginated, read-only monitoring snapshot from ``store``.

    Pure and recomputable: the same store contents and the same
    ``now``/``stale_after``/``limit``/``offset``/``max_scanned_runs`` always
    yield the same snapshot.

    Every run *directory* under ``store.runs_dir`` is discovered via cheap
    filesystem metadata (never a JSON parse), so directory discovery itself
    stays unbounded. Actually opening and parsing ``run.json`` -- the
    expensive step -- is capped at ``max_scanned_runs``: when more
    directories exist than the cap, the newest ones (by ``run.json``
    modification time, falling back to directory modification time when
    ``run.json`` itself is missing) are selected, and
    ``MonitoringSnapshot.scan_truncated`` is set so a caller can never
    mistake a partial scan for a complete one. Store-wide counts and attempt
    tallies are computed only over the scanned subset (bounded, fixed-schema
    data -- not logs, diffs, or tool output); the heavier per-run artifact
    lookups used for ``title``/``complexity``/``risk`` are only performed for
    the page actually returned, so a request never has to parse an unbounded
    number of artifacts to satisfy pagination.

    A directory whose ``run.json`` is missing, unreadable, or fails to parse
    is *not* silently dropped: it is skipped from the readable run list (and
    therefore from every count) but tallied into
    ``unreadable_runs``/``degraded_reasons`` so the caller gets an honest
    degraded indicator instead of either an unhandled exception or a false
    "healthy" result.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be > 0")
    if max_scanned_runs <= 0:
        raise ValueError("max_scanned_runs must be > 0")

    reference_time = _normalize_now(now)
    if scan is None:
        scan = scan_readable_runs(store, max_scanned_runs)

    counts = _compute_state_counts(scan.readable_runs, reference_time, stale_after)
    attempts_by_role, attempts_by_model = _compute_attempt_tallies(scan.readable_runs)
    metrics = _compute_aggregate_metrics(scan.readable_runs)

    total_readable = len(scan.readable_runs)
    page_runs = scan.readable_runs[offset : offset + limit]
    summaries = [_build_run_summary(store, run, reference_time, stale_after) for run in page_runs]

    return MonitoringSnapshot(
        generated_at=reference_time,
        stale_after_seconds=stale_after.total_seconds(),
        max_scanned_runs=max_scanned_runs,
        total_runs=scan.total_directories,
        scanned_runs=scan.scanned_runs,
        scan_truncated=scan.scan_truncated,
        unreadable_runs=scan.unreadable_runs,
        degraded=scan.degraded,
        degraded_reasons=scan.degraded_reasons(max_scanned_runs=max_scanned_runs),
        counts=counts,
        attempts_by_role=attempts_by_role,
        attempts_by_model=attempts_by_model,
        metrics=metrics,
        page=PageMeta(
            limit=limit,
            offset=offset,
            returned=len(summaries),
            total=total_readable,
            has_more=(offset + len(summaries)) < total_readable,
        ),
        runs=summaries,
    )


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _discover_run_directories(store: RunStoreProtocol) -> list[Path]:
    """Every run directory under ``store.runs_dir`` (cheap ``iterdir``,
    never a JSON parse), regardless of whether it contains a ``run.json``.

    Enumerating directories rather than existing ``run.json`` files is what
    lets a directory whose ``run.json`` is missing (e.g. an interrupted
    first save) surface as an unreadable/degraded run instead of being
    invisible to the snapshot entirely.
    """
    runs_dir = store.runs_dir
    if not runs_dir.is_dir():
        return []
    return [entry for entry in runs_dir.iterdir() if entry.is_dir()]


def _directory_recency(directory: Path) -> float:
    """Best-effort recency signal for ``directory``, used only to choose
    which runs to scan first when a store exceeds ``max_scanned_runs``.

    Prefers ``run.json``'s own modification time (updated on every
    ``save_run``, so it tracks a run's last activity) and falls back to the
    run directory's own modification time when ``run.json`` does not exist.
    Never raises: an unreadable timestamp sorts as the oldest possible.
    """
    for candidate in (directory / "run.json", directory):
        try:
            return candidate.stat().st_mtime
        except OSError:
            continue
    return float("-inf")


def _select_scan_candidates(
    store: RunStoreProtocol,
    max_scanned_runs: int,
) -> tuple[list[str], int, bool]:
    """Return ``(run_ids_to_scan, total_directories_discovered, truncated)``.

    ``run_ids_to_scan`` is capped at ``max_scanned_runs`` and, when the store
    holds more directories than that, prefers the newest ones by
    :func:`_directory_recency` so a truncated scan still reflects the most
    operationally relevant runs rather than an arbitrary filesystem order.
    """
    directories = _discover_run_directories(store)
    total_directories = len(directories)
    directories.sort(key=lambda entry: (-_directory_recency(entry), entry.name))
    truncated = total_directories > max_scanned_runs
    selected = directories[:max_scanned_runs]
    return [entry.name for entry in selected], total_directories, truncated


def _categorize_load_error(exc: Exception) -> str:
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, ValidationError):
        return "validation_error"
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, OSError):
        return "io_error"
    return "invalid_run_data"


@dataclass(frozen=True)
class RunScanResult:
    """The one bounded run-directory scan shared by
    :func:`build_monitoring_snapshot` and :func:`build_operational_health`,
    so both agree on exactly the same readable runs, scan cap, and degraded
    reasons for a given call."""

    readable_runs: list[FactoryRun]
    total_directories: int
    scanned_runs: int
    scan_truncated: bool
    unreadable_reasons: dict[str, int]

    @property
    def unreadable_runs(self) -> int:
        return sum(self.unreadable_reasons.values())

    @property
    def degraded(self) -> bool:
        return bool(self.unreadable_reasons) or self.scan_truncated

    def degraded_reasons(self, *, max_scanned_runs: int) -> list[str]:
        reasons = [
            f"{count} run(s) unreadable ({reason})"
            for reason, count in sorted(self.unreadable_reasons.items())
        ]
        if self.scan_truncated:
            reasons.append(
                f"scan capped at {max_scanned_runs} of {self.total_directories} "
                "discovered run directory(ies); computed results reflect only "
                "the newest scanned runs"
            )
        return reasons


_RunScanResult = RunScanResult

DEFAULT_SCAN_CACHE_TTL: float = 2.0


class RunScanCache:
    """Thread-safe, bounded, monotonic-TTL cache for readable run scans.

    Designed for read-only observability and dashboard refresh cycles so
    /api/summary, its health data, and nearby /api/runs reads share a single
    validated run scan rather than deserializing the same run set multiple
    times. Uses monotonic time expiry and defensive copies to prevent mutation
    contamination. Never used as workflow authority.
    """

    def __init__(
        self,
        store: RunStoreProtocol,
        *,
        ttl: float = DEFAULT_SCAN_CACHE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cached_scan: RunScanResult | None = None
        self._cached_max_scanned_runs: int | None = None
        self._cached_at: float = 0.0
        self._hits: int = 0
        self._misses: int = 0

    @property
    def ttl(self) -> float:
        return self._ttl

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    def invalidate(self) -> None:
        """Clear cached scan immediately."""
        with self._lock:
            self._cached_scan = None
            self._cached_max_scanned_runs = None
            self._cached_at = 0.0

    def get_scan(self, max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS) -> RunScanResult:
        """Return a validated scan, reusing the cached scan if within TTL.

        Returns a defensive copy of the scan result and its runs so callers
        cannot mutate the cached state.
        """
        if max_scanned_runs <= 0:
            raise ValueError("max_scanned_runs must be > 0")

        with self._lock:
            now_mono = self._clock()
            if (
                self._cached_scan is not None
                and self._cached_max_scanned_runs == max_scanned_runs
                and (now_mono - self._cached_at) < self._ttl
            ):
                self._hits += 1
                return self._copy_scan(self._cached_scan)

            self._misses += 1
            scan = scan_readable_runs(self._store, max_scanned_runs)
            self._cached_scan = scan
            self._cached_max_scanned_runs = max_scanned_runs
            self._cached_at = now_mono
            return self._copy_scan(scan)

    @staticmethod
    def _copy_scan(scan: RunScanResult) -> RunScanResult:
        return RunScanResult(
            readable_runs=[run.model_copy(deep=True) for run in scan.readable_runs],
            total_directories=scan.total_directories,
            scanned_runs=scan.scanned_runs,
            scan_truncated=scan.scan_truncated,
            unreadable_reasons=dict(scan.unreadable_reasons),
        )


def scan_readable_runs(
    store: RunStoreProtocol,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
) -> RunScanResult:
    """Discover run directories (broad, cheap), then open and parse at most
    ``max_scanned_runs`` of the newest ones (bounded, expensive), returning
    every readable ``FactoryRun`` sorted newest-first plus honest scan
    metadata. A directory whose ``run.json`` is missing or fails to parse is
    tallied into ``unreadable_reasons`` rather than raised or dropped
    silently.
    """
    if max_scanned_runs <= 0:
        raise ValueError("max_scanned_runs must be > 0")
    candidate_run_ids, total_directories, scan_truncated = _select_scan_candidates(
        store, max_scanned_runs
    )

    readable_runs: list[FactoryRun] = []
    unreadable_reasons: dict[str, int] = {}
    for run_id in candidate_run_ids:
        try:
            readable_runs.append(store.load_run(run_id))
        except (OSError, ValueError) as exc:
            reason = _categorize_load_error(exc)
            unreadable_reasons[reason] = unreadable_reasons.get(reason, 0) + 1

    readable_runs.sort(key=lambda run: (run.created_at, run.id), reverse=True)

    return RunScanResult(
        readable_runs=readable_runs,
        total_directories=total_directories,
        scanned_runs=len(candidate_run_ids),
        scan_truncated=scan_truncated,
        unreadable_reasons=unreadable_reasons,
    )


_scan_readable_runs = scan_readable_runs


def _last_signal_at(run: FactoryRun) -> datetime | None:
    """Most recent liveness signal persisted for ``run``.

    Prefers the lease heartbeat (the scheduler's own liveness mechanism --
    see ``scheduler.py``'s "Lease/heartbeat liveness" note), then
    ``last_activity_at``, then ``updated_at``. Returns ``None`` only when
    none of these were ever recorded, e.g. a freshly created run.
    """
    candidates = [
        timestamp
        for timestamp in (
            run.lease.heartbeat_at if run.lease is not None else None,
            run.last_activity_at,
            run.updated_at,
        )
        if timestamp is not None
    ]
    return max(candidates) if candidates else None


def _is_stale(run: FactoryRun, now: datetime, stale_after: timedelta) -> bool:
    signal = _last_signal_at(run) or run.created_at
    return (now - signal) > stale_after


def _compute_state_counts(
    runs: list[FactoryRun],
    now: datetime,
    stale_after: timedelta,
) -> RunStateCounts:
    succeeded = escalated = failed = active = stale_active = 0
    for run in runs:
        classification = _classify_run(run)
        if classification == "escalated":
            escalated += 1
        elif classification == "failed":
            failed += 1
        elif classification == "succeeded":
            succeeded += 1
        else:
            active += 1
            if _is_stale(run, now, stale_after):
                stale_active += 1
    return RunStateCounts(
        succeeded=succeeded,
        escalated=escalated,
        failed=failed,
        active=active,
        stale_active=stale_active,
    )


def _compute_attempt_tallies(
    runs: list[FactoryRun],
) -> tuple[dict[str, int], dict[str, int]]:
    by_role: dict[str, int] = {}
    by_model: dict[str, int] = {}
    for run in runs:
        for attempt in run.attempt_records:
            by_role[attempt.role.value] = by_role.get(attempt.role.value, 0) + 1
            by_model[attempt.model] = by_model.get(attempt.model, 0) + 1
    return by_role, by_model


def _duration_summary(durations_seconds: list[float]) -> DurationSummary:
    if not durations_seconds:
        return DurationSummary(count=0, min_seconds=None, max_seconds=None, average_seconds=None)
    return DurationSummary(
        count=len(durations_seconds),
        min_seconds=min(durations_seconds),
        max_seconds=max(durations_seconds),
        average_seconds=sum(durations_seconds) / len(durations_seconds),
    )


def _compute_aggregate_metrics(runs: list[FactoryRun]) -> AggregateMetrics:
    """See :class:`AggregateMetrics` for the exact, deterministic definition
    of each field. Pure function of ``runs`` (the scanned readable subset);
    no wall-clock/timezone dependence beyond the persisted timestamps
    themselves."""
    total_attempts = 0
    implementation_attempts = 0
    ci_repair_attempts = 0
    scope_replans = 0
    succeeded_total = 0
    first_pass_numerator = 0
    durations: list[float] = []

    stage_duration_samples: dict[str, list[float]] = {}
    operation_duration_samples: dict[str, list[float]] = {}
    aggregated_counters: dict[str, int] = {}
    total_prompt_chars = 0
    total_response_chars = 0
    total_gate_failures = 0
    verification_gate_failures = 0
    review_gate_failures = 0
    total_rework_attempts = 0
    runs_with_rework = 0

    for run in runs:
        run_implementation_attempts = 0
        run_ci_repair_attempts = 0
        legacy_scope_replans = 0
        for attempt in run.attempt_records:
            total_attempts += 1
            if attempt.budget is AttemptBudget.IMPLEMENTATION:
                implementation_attempts += 1
                if attempt.triggered_by is not AttemptTrigger.POLISH:
                    run_implementation_attempts += 1
            elif attempt.budget is AttemptBudget.CI_REPAIR:
                ci_repair_attempts += 1
                run_ci_repair_attempts += 1
            if attempt.triggered_by is AttemptTrigger.SCOPE:
                legacy_scope_replans += 1
        scope_replans += max(run.scope_replans, legacy_scope_replans)

        if run.completed_at is not None and _is_run_finished(run):
            durations.append(max((run.completed_at - run.created_at).total_seconds(), 0.0))

        if _classify_run(run) == "succeeded":
            succeeded_total += 1
            if run_implementation_attempts == 1:
                first_pass_numerator += 1

        for k, v in run.performance.durations_ms.items():
            if k.startswith("stage."):
                stage_name = k.removeprefix("stage.")
                stage_duration_samples.setdefault(stage_name, []).append(v / 1000.0)
            elif k.startswith("operation."):
                op_name = k.removeprefix("operation.")
                operation_duration_samples.setdefault(op_name, []).append(v / 1000.0)

        for k, v in run.performance.counters.items():
            aggregated_counters[k] = aggregated_counters.get(k, 0) + v

        total_prompt_chars += run.performance.prompt_chars or 0
        total_response_chars += run.performance.response_chars or 0

        gate_failures = run.performance.counters.get("gate_failures_total", 0)
        total_gate_failures += gate_failures
        verification_gate_failures += run.performance.counters.get(
            "gate_failure.verification", 0
        ) + run.performance.counters.get("gate_failure.VERIFYING", 0)
        review_gate_failures += run.performance.counters.get(
            "gate_failure.review", 0
        ) + run.performance.counters.get("gate_failure.REVIEWING", 0)
        fallback_rework = (
            max(0, run_implementation_attempts - 1)
            + run_ci_repair_attempts
            + max(run.scope_replans, legacy_scope_replans)
        )
        run_rework = run.performance.counters.get("rework_total", 0) or fallback_rework
        total_rework_attempts += run_rework
        if run_rework > 0:
            runs_with_rework += 1

    average_attempts_per_run = (total_attempts / len(runs)) if runs else None
    first_pass_success = FirstPassSuccessMetric(
        numerator=first_pass_numerator,
        denominator=succeeded_total,
        rate=(first_pass_numerator / succeeded_total) if succeeded_total > 0 else None,
    )
    completed_run_durations = _duration_summary(durations)
    stage_durations = {
        stage: _duration_summary(samples)
        for stage, samples in sorted(stage_duration_samples.items())
    }
    operation_durations = {
        op: _duration_summary(samples) for op, samples in sorted(operation_duration_samples.items())
    }
    rework = ReworkMetrics(
        total_gate_failures=total_gate_failures,
        verification_gate_failures=verification_gate_failures,
        review_gate_failures=review_gate_failures,
        total_rework_attempts=total_rework_attempts,
        runs_with_rework=runs_with_rework,
        rework_rate=(runs_with_rework / len(runs)) if runs else None,
    )
    performance = PerformanceSummary(
        stage_durations=stage_durations,
        operation_durations=operation_durations,
        counters=aggregated_counters,
        total_prompt_chars=total_prompt_chars,
        total_response_chars=total_response_chars,
        rework=rework,
    )

    return AggregateMetrics(
        total_attempts=total_attempts,
        average_attempts_per_run=average_attempts_per_run,
        implementation_attempts=implementation_attempts,
        ci_repair_attempts=ci_repair_attempts,
        scope_replans=scope_replans,
        first_pass_success=first_pass_success,
        completed_run_durations=completed_run_durations,
        usage=_usage_summary(invocation for run in runs for invocation in run.invocation_records),
        performance=performance,
    )


def _usage_summary(invocations: Iterable[InvocationRecord]) -> UsageSummary:
    records = list(invocations)
    reported = [record for record in records if record.usage is not None]

    input_tokens: list[int] = []
    output_tokens: list[int] = []
    reasoning_tokens: list[int] = []
    cache_read_tokens: list[int] = []
    cache_write_tokens: list[int] = []
    premium_requests: list[float] = []
    premium_request_costs: list[float] = []
    total_nano_aiu: list[int] = []

    for record in reported:
        usage = record.usage
        assert usage is not None
        if usage.premium_requests is not None:
            premium_requests.append(usage.premium_requests)
        if usage.total_premium_request_cost is not None:
            premium_request_costs.append(usage.total_premium_request_cost)
        if usage.total_nano_aiu is not None:
            total_nano_aiu.append(usage.total_nano_aiu)
        elif model_nano := [
            item.total_nano_aiu for item in usage.model_usage if item.total_nano_aiu is not None
        ]:
            total_nano_aiu.append(sum(model_nano))

        model_input = [
            item.input_tokens for item in usage.model_usage if item.input_tokens is not None
        ]
        model_output = [
            item.output_tokens for item in usage.model_usage if item.output_tokens is not None
        ]
        model_reasoning = [
            item.reasoning_tokens for item in usage.model_usage if item.reasoning_tokens is not None
        ]
        model_cache_read = [
            item.cache_read_tokens
            for item in usage.model_usage
            if item.cache_read_tokens is not None
        ]
        model_cache_write = [
            item.cache_write_tokens
            for item in usage.model_usage
            if item.cache_write_tokens is not None
        ]
        if usage.input_tokens is not None:
            input_tokens.append(usage.input_tokens)
        elif model_input:
            input_tokens.append(sum(model_input))
        if usage.output_tokens is not None:
            output_tokens.append(usage.output_tokens)
        elif model_output:
            output_tokens.append(sum(model_output))
        if usage.reasoning_tokens is not None:
            reasoning_tokens.append(usage.reasoning_tokens)
        elif model_reasoning:
            reasoning_tokens.append(sum(model_reasoning))
        if usage.cache_read_tokens is not None:
            cache_read_tokens.append(usage.cache_read_tokens)
        elif model_cache_read:
            cache_read_tokens.append(sum(model_cache_read))
        if usage.cache_write_tokens is not None:
            cache_write_tokens.append(usage.cache_write_tokens)
        elif model_cache_write:
            cache_write_tokens.append(sum(model_cache_write))

        for item in usage.model_usage:
            if usage.total_premium_request_cost is None and item.premium_request_cost is not None:
                premium_request_costs.append(item.premium_request_cost)

    return UsageSummary(
        invocation_count=len(records),
        reported_invocations=len(reported),
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
        reasoning_tokens=sum(reasoning_tokens) if reasoning_tokens else None,
        cache_read_tokens=sum(cache_read_tokens) if cache_read_tokens else None,
        cache_write_tokens=sum(cache_write_tokens) if cache_write_tokens else None,
        premium_requests=sum(premium_requests) if premium_requests else None,
        premium_request_cost=(sum(premium_request_costs) if premium_request_costs else None),
        total_nano_aiu=sum(total_nano_aiu) if total_nano_aiu else None,
    )


def _load_optional_artifact(
    store: RunStoreProtocol,
    run_id: str,
    artifact_type: type[Any],
) -> Any | None:
    try:
        return store.load_artifact(run_id, artifact_type)
    except (OSError, ValueError):
        return None


def _build_run_summary(
    store: RunStoreProtocol,
    run: FactoryRun,
    now: datetime,
    stale_after: timedelta,
) -> RunSummary:
    work_item = _load_optional_artifact(store, run.id, WorkItem)
    triage = _load_optional_artifact(store, run.id, TriageResult)

    title = redact_secrets(work_item.title) if work_item is not None else None
    if triage is not None:
        complexity, risk = triage.complexity, triage.risk
    elif work_item is not None:
        complexity, risk = work_item.complexity, work_item.risk
    else:
        complexity, risk = None, None

    signal = _last_signal_at(run) or run.created_at
    finished = _is_run_finished(run)
    implementation_attempts = sum(
        1 for attempt in run.attempt_records if attempt.budget is AttemptBudget.IMPLEMENTATION
    )
    ci_repair_attempts = sum(
        1 for attempt in run.attempt_records if attempt.budget is AttemptBudget.CI_REPAIR
    )

    return RunSummary(
        run_id=run.id,
        work_item_id=run.work_item_id,
        title=title,
        state=run.state,
        complexity=complexity,
        risk=risk,
        created_at=run.created_at,
        updated_at=run.updated_at,
        age_seconds=max((now - run.created_at).total_seconds(), 0.0),
        idle_seconds=max((now - signal).total_seconds(), 0.0),
        attempt_count=len(run.attempt_records),
        invocation_count=len(run.invocation_records),
        implementation_attempts=implementation_attempts,
        ci_repair_attempts=ci_repair_attempts,
        usage=_usage_summary(run.invocation_records),
        is_finished=finished,
        is_stale=(not finished) and _is_stale(run, now, stale_after),
        review_status=(
            "ACTION_REQUIRED"
            if run.state is WorkflowState.NEEDS_HUMAN
            else "ACCEPTED_WITH_FINDINGS"
            if run.review_acceptance is not None
            else None
        ),
        performance=run.performance,
    )


def build_run_detail(
    store: RunStoreProtocol,
    run_id: str,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> RunDetail | None:
    """Derive one run's read-only detail view, or ``None`` if it is not
    readable.

    Pure and read-only in exactly the same sense as
    :func:`build_monitoring_snapshot`: it loads one persisted run (plus the
    same two small artifacts the summary uses for ``title``/``complexity``/
    ``risk``) and never creates, mutates or repairs anything on disk --
    :class:`~software_agent_factory.store.FileRunStore` read paths do not
    create a run directory for a missing run.

    ``None`` -- never an exception -- is returned for a run id that does not
    exist, is malformed/hostile, or whose ``run.json`` cannot be parsed. That
    is what lets an untrusted caller (the Phase 15.11 dashboard's detail
    route) map "no such run" straight onto a 404 without distinguishing
    "absent" from "corrupt" to the client.
    """
    try:
        run = store.load_run(run_id)
    except (OSError, ValueError):  # ValidationError is a ValueError in Pydantic v2
        return None

    summary = _build_run_summary(store, run, _normalize_now(now), stale_after)
    return RunDetail(
        **summary.model_dump(),
        completed_at=run.completed_at,
        commit_sha=run.commit_sha,
        pull_request_url=run.pull_request_url,
        attempts=[
            RunAttemptSummary(
                attempt_number=attempt.attempt_number,
                role=attempt.role,
                model=attempt.model,
                budget=attempt.budget,
                triggered_by=attempt.triggered_by,
                outcome=attempt.outcome,
                started_at=attempt.started_at,
                completed_at=attempt.completed_at,
            )
            for attempt in run.attempt_records
        ],
        invocations=[
            RunInvocationSummary(
                invocation_number=invocation.invocation_number,
                role=invocation.role,
                purpose=str(invocation.purpose),
                model=invocation.model,
                context_tier=invocation.context_tier,
                success=invocation.success,
                started_at=invocation.started_at,
                completed_at=invocation.completed_at,
                attempt_number=invocation.attempt_number,
                usage=invocation.usage,
                performance=invocation.performance,
            )
            for invocation in run.invocation_records
        ],
        active_invocation=build_active_invocation_summary(
            run,
            now=_normalize_now(now),
            stale_after=stale_after,
        ),
        guidance=_build_run_guidance(store, run),
    )


def _build_run_guidance(store: RunStoreProtocol, run: FactoryRun) -> RunGuidance | None:
    if run.state is not WorkflowState.NEEDS_HUMAN and run.review_acceptance is not None:
        return RunGuidance(
            status="ACCEPTED_WITH_FINDINGS",
            reason_code="BOUNDED_REVIEW_ACCEPTANCE",
            summary="The controller continued after the bounded review limit.",
            next_action="Review the accepted findings in the pull request before merging.",
            artifact="review-acceptance.json",
            finding_count=len(run.review_acceptance.findings),
            finding_ids=[finding.id for finding in run.review_acceptance.findings],
            category_counts=_review_category_counts(run.review_acceptance.findings),
        )
    if run.state is not WorkflowState.NEEDS_HUMAN:
        return None

    impasse = _load_optional_artifact(store, run.id, ReviewImpasse)
    if impasse is not None:
        return RunGuidance(
            status="ACTION_REQUIRED",
            reason_code="REVIEW_IMPASSE",
            summary="Independent review did not converge within the safe automatic policy.",
            next_action=(
                "Inspect review-impasse.json, resolve or accept the listed findings, then retry."
            ),
            artifact="review-impasse.json",
            finding_count=len(impasse.finding_ids),
            finding_ids=impasse.finding_ids,
            category_counts=_review_category_counts(run.review_ledger.open_findings),
        )

    reason = (run.failure_reason or "").lower()
    if "risk" in reason or "approval" in reason:
        code = "RISK_APPROVAL"
        summary = "The run requires approval under the configured risk policy."
        action = "Review the work item risk and approve or change the policy before retrying."
    elif "scope" in reason:
        code = "SCOPE_REVIEW"
        summary = "The proposed changes exceeded the approved scope."
        action = "Review the planned and changed files, then update the scope or retry."
    elif "budget" in reason or "attempt" in reason:
        code = "ATTEMPT_BUDGET_EXHAUSTED"
        summary = "The run exhausted a bounded retry budget."
        action = "Inspect the run artifacts, correct the underlying issue, then retry."
    elif "ci " in reason or reason.startswith("ci"):
        code = "CI_INTERVENTION"
        summary = "CI could not be completed or repaired automatically."
        action = "Inspect the pull request checks, fix the failing check, then retry delivery."
    elif any(term in reason for term in ("publish", "pull request", "merge", "permission")):
        code = "DELIVERY_INTERVENTION"
        summary = "The controller could not complete pull request delivery."
        action = "Check repository permissions and delivery settings, then retry delivery."
    elif any(term in reason for term in ("abandon", "interrupt", "workspace")):
        code = "RECOVERY_INTERVENTION"
        summary = "The run could not safely recover its persisted workspace."
        action = "Inspect the run and workspace metadata before starting a replacement run."
    else:
        code = "MANUAL_INSPECTION"
        summary = "The controller stopped at a manual decision boundary."
        action = "Inspect the typed run artifacts and decide whether to retry or replace the run."
    return RunGuidance(
        status="ACTION_REQUIRED",
        reason_code=code,
        summary=summary,
        next_action=action,
    )


def _review_category_counts(findings: Iterable[Any]) -> dict[ReviewFindingCategory, int]:
    counts: dict[ReviewFindingCategory, int] = {}
    for finding in findings:
        counts[finding.category] = counts.get(finding.category, 0) + 1
    return counts


def build_active_invocation_summary(
    run: FactoryRun,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
) -> ActiveInvocationSummary | None:
    """Return safe liveness details for the invocation persisted as active."""

    active = run.active_invocation
    if active is None or _is_run_finished(run):
        return None
    current_time = _normalize_now(now)
    status = _active_invocation_status(run, current_time, stale_after)
    return ActiveInvocationSummary(
        invocation_number=active.invocation_number,
        role=active.role,
        purpose=str(active.purpose),
        model=active.model,
        context_tier=active.context_tier,
        status=status,
        started_at=active.started_at,
        attempt_number=active.attempt_number,
    )


def _active_invocation_status(
    run: FactoryRun,
    now: datetime,
    stale_after: timedelta,
) -> str:
    lease = run.lease
    if lease is None:
        return "abandoned"
    if lease.host == socket.gethostname():
        try:
            os.kill(lease.pid, 0)
        except ProcessLookupError:
            return "crashed"
        except PermissionError:
            return "running"
        return "running"
    if _is_stale(run, now, stale_after):
        return "stale"
    return "running"


# ---------------------------------------------------------------------------
# Operational health: stale runs, stale locks, orphaned workspaces
# ---------------------------------------------------------------------------


class StaleRunFinding(ModelBase):
    """One non-terminal run whose most recent heartbeat/activity/update
    timestamp is older than ``stale_after`` -- the same staleness rule
    ``RunSummary.is_stale``/``RunStateCounts.stale_active`` use, surfaced
    here as an individually actionable health finding rather than folded
    into an aggregate count."""

    run_id: str
    work_item_id: str
    state: WorkflowState
    idle_seconds: float = Field(ge=0.0)
    workspace_path: str | None = None


class StaleLockFinding(ModelBase):
    """One workspace lock file under ``data_dir/locks`` that exists but has
    no live holder: a non-blocking ``flock`` probe against it succeeded, was
    released immediately, and the file itself was left untouched on disk --
    this module never deletes a lock file. ``lock_name`` is just the file
    name (e.g. ``<workspace-key>.lock``); no repository content or path
    outside ``data_dir`` is exposed. The durable ``prune-*.lock`` worktree
    administration mutex is excluded by design (see
    :func:`_find_stale_locks`)."""

    lock_name: str
    modified_at: UtcDateTime | None = None


class OrphanedWorkspaceFinding(ModelBase):
    """One directory under ``data_dir/workspaces`` not referenced by any
    scanned run's ``FactoryRun.workspace_path``. May include false
    positives for a workspace whose owning run could not be scanned this
    call (see ``OperationalHealthReport.degraded_reasons``); it is only
    named here, never removed or otherwise acted upon."""

    workspace_name: str
    modified_at: UtcDateTime | None = None


class OperationalHealthReport(VersionedModel):
    """Typed, read-only operational health findings.

    Derived at read time from the same bounded run scan
    :func:`build_monitoring_snapshot` uses (see ``scanned_runs``/
    ``scan_truncated``/``degraded`` -- identical meaning here), plus a
    cheap, strictly non-destructive inspection of ``data_dir/locks`` and
    ``data_dir/workspaces``. Nothing here mutates, repairs, or deletes
    anything: a stale lock probe releases its own non-blocking flock
    immediately and never unlinks the file; an orphaned workspace is only
    named, never removed.

    ``lock_check_supported`` is ``False`` on a platform with no ``fcntl``
    (e.g. Windows); in that case ``stale_locks`` is always empty and
    ``degraded_reasons`` says so explicitly rather than implying zero stale
    locks were found.
    """

    generated_at: UtcDateTime
    stale_after_seconds: float = Field(gt=0.0)
    max_scanned_runs: int = Field(ge=1)
    total_runs: int = Field(ge=0)
    scanned_runs: int = Field(ge=0)
    scan_truncated: bool
    unreadable_runs: int = Field(ge=0)
    degraded: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    lock_check_supported: bool
    locks_checked: int = Field(ge=0)
    workspaces_checked: int = Field(ge=0)
    stale_runs: list[StaleRunFinding] = Field(default_factory=list)
    stale_locks: list[StaleLockFinding] = Field(default_factory=list)
    orphaned_workspaces: list[OrphanedWorkspaceFinding] = Field(default_factory=list)


def build_operational_health(
    store: RunStoreProtocol,
    *,
    data_dir: str | Path | None = None,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
    scan: RunScanResult | None = None,
) -> OperationalHealthReport:
    """Derive a read-only operational health report.

    Pure and recomputable (aside from lock liveness, which necessarily
    reflects the instant it is probed): the same store/filesystem contents
    and the same ``now``/``stale_after``/``max_scanned_runs`` yield the same
    findings, modulo whether another process happens to be holding a lock
    at the moment of the call.

    ``data_dir`` defaults to ``store.runs_dir.parent``, matching the
    conventional layout ``FileRunStore``/``GitWorktreeWorkspace`` both use
    (``data_dir/runs``, ``data_dir/workspaces``, ``data_dir/locks``); pass it
    explicitly if a caller's store does not follow that convention.

    Reuses the exact same bounded run scan as :func:`build_monitoring_snapshot`
    (:func:`scan_readable_runs`, capped at ``max_scanned_runs``) for both
    stale-run detection and the set of workspace directories considered
    "referenced", so orphan/staleness findings and the metrics snapshot are
    always consistent with each other for the same inputs.

    Never writes, deletes, or repairs anything: a stale lock probe uses a
    non-blocking ``flock`` that is released the instant it succeeds and
    never unlinks the file, and an orphaned workspace directory is only
    named in the returned report.
    """
    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be > 0")
    if max_scanned_runs <= 0:
        raise ValueError("max_scanned_runs must be > 0")

    reference_time = _normalize_now(now)
    root = Path(data_dir).expanduser() if data_dir is not None else store.runs_dir.parent

    if scan is None:
        scan = scan_readable_runs(store, max_scanned_runs)

    stale_runs = [
        StaleRunFinding(
            run_id=run.id,
            work_item_id=run.work_item_id,
            state=run.state,
            idle_seconds=max(
                (reference_time - (_last_signal_at(run) or run.created_at)).total_seconds(),
                0.0,
            ),
            workspace_path=run.workspace_path,
        )
        for run in scan.readable_runs
        if _classify_run(run) == "active" and _is_stale(run, reference_time, stale_after)
    ]

    referenced_workspace_names = {
        Path(run.workspace_path).name for run in scan.readable_runs if run.workspace_path
    }

    stale_locks, locks_checked, lock_issues = _find_stale_locks(root / "locks")
    orphaned_workspaces, workspaces_checked = _find_orphaned_workspaces(
        root / "workspaces", referenced_workspace_names
    )

    degraded_reasons = [
        *scan.degraded_reasons(max_scanned_runs=max_scanned_runs),
        *lock_issues,
    ]
    if scan.scan_truncated:
        degraded_reasons.append(
            "orphaned-workspace findings may include false positives for workspaces "
            "owned by runs the scan cap excluded"
        )

    return OperationalHealthReport(
        generated_at=reference_time,
        stale_after_seconds=stale_after.total_seconds(),
        max_scanned_runs=max_scanned_runs,
        total_runs=scan.total_directories,
        scanned_runs=scan.scanned_runs,
        scan_truncated=scan.scan_truncated,
        unreadable_runs=scan.unreadable_runs,
        degraded=scan.degraded or bool(lock_issues),
        degraded_reasons=degraded_reasons,
        lock_check_supported=_FCNTL_AVAILABLE,
        locks_checked=locks_checked,
        workspaces_checked=workspaces_checked,
        stale_runs=stale_runs,
        stale_locks=stale_locks,
        orphaned_workspaces=orphaned_workspaces,
    )


def _find_stale_locks(locks_dir: Path) -> tuple[list[StaleLockFinding], int, list[str]]:
    """Inspect every workspace ``*.lock`` file directly under ``locks_dir``.

    Returns ``(findings, files_checked, issues)``; ``issues`` are
    human-readable degraded-reason strings (unsupported platform, malformed
    owner data, or a per-file probe failure count), never raised.

    ``prune-<digest>.lock`` files are deliberately skipped and not counted:
    unlike a workspace lock -- which ``GitWorktreeWorkspace.release_lock``
    unlinks while still held, so a *leftover* file genuinely means an
    abandoned holder -- the per-source-repository worktree administration
    lock (``workspace._prune_lock``) is a durable mutex file that is only
    ever flocked and released, never unlinked. Reporting it would make every
    healthy factory that has ever run once report a permanent, unactionable
    stale lock.
    """
    if not _FCNTL_AVAILABLE:
        return [], 0, ["lock staleness checks unsupported on this platform (no fcntl)"]
    if not locks_dir.is_dir():
        return [], 0, []

    findings: list[StaleLockFinding] = []
    checked = 0
    failed_checks = 0
    for entry in sorted(locks_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_file() or not entry.name.endswith(".lock"):
            continue
        if entry.name.startswith(PRUNE_LOCK_PREFIX):
            continue
        checked += 1
        try:
            is_stale, modified_at = _probe_lock_staleness(entry)
        except (OSError, ValueError):
            failed_checks += 1
            continue
        if is_stale:
            findings.append(StaleLockFinding(lock_name=entry.name, modified_at=modified_at))

    issues = (
        [f"{failed_checks} lock file(s) could not be probed for staleness"] if failed_checks else []
    )
    return findings, checked, issues


def _probe_lock_staleness(lock_path: Path) -> tuple[bool, datetime | None]:
    """Non-destructively determine whether a lock's recorded owner is dead.

    Workspace locks persist their owning PID after acquiring the real
    ``flock``. Monitoring must not acquire that same exclusive lock even
    briefly: the workspace protocol uses ``LOCK_NB``, so an observer holding
    it could make a legitimate run fail with ``WorkspaceLockError``. Instead
    this read-only probe checks whether the recorded PID still exists.

    PID reuse can conservatively produce a false negative (an abandoned file
    appears live), but never a false lock conflict. Empty or malformed owner
    data is reported as a degraded probe rather than guessed to be stale.
    """
    try:
        owner_pid = int(lock_path.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        raise ValueError(f"lock file has an invalid owner PID: {lock_path}") from exc
    if owner_pid <= 0:
        raise ValueError(f"lock file has an invalid owner PID: {lock_path}")

    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        is_stale = True
    except PermissionError:
        is_stale = False
    else:
        is_stale = False

    try:
        modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        modified_at = None
    return is_stale, modified_at


def _find_orphaned_workspaces(
    workspaces_dir: Path,
    referenced_names: set[str],
) -> tuple[list[OrphanedWorkspaceFinding], int]:
    """Every directory directly under ``workspaces_dir`` whose name is not
    in ``referenced_names``. Returns ``(findings, directories_checked)``.
    Never descends into a workspace's own contents (no file listing, no
    diff, no repository content is read)."""
    if not workspaces_dir.is_dir():
        return [], 0

    findings: list[OrphanedWorkspaceFinding] = []
    checked = 0
    for entry in sorted(workspaces_dir.iterdir(), key=lambda path: path.name):
        if not entry.is_dir():
            continue
        checked += 1
        if entry.name in referenced_names:
            continue
        try:
            modified_at = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        except OSError:
            modified_at = None
        findings.append(
            OrphanedWorkspaceFinding(workspace_name=entry.name, modified_at=modified_at)
        )
    return findings, checked


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class _RedactingJsonFormatter(logging.Formatter):
    """Renders one JSON object per line: ``timestamp``, ``level``,
    ``logger``, ``message``, plus ``run_id``/``state`` when supplied via
    ``extra``.

    The rendered message is passed through the same credential redaction
    used for captured command output (``verification.redact_secrets``).
    Exception tracebacks are intentionally never rendered: this module has
    no way to know whether a caller logged an exception that happened to
    wrap repository content, so it stays out of the log line entirely
    rather than risk leaking it.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            payload["run_id"] = run_id
        state = getattr(record, "state", None)
        if state is not None:
            payload["state"] = str(state)
        return json.dumps(payload, sort_keys=True)


def configure_factory_logging(
    data_dir: str | Path,
    *,
    logger_name: str = "software_agent_factory",
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> logging.Logger:
    """Attach a bounded, structured-JSON rotating file handler under
    ``<data_dir>/logs/factory.log`` and return the configured logger.

    Idempotent: calling this again for the same ``logger_name`` (e.g. from
    another entry point) reuses the existing handler instead of stacking a
    duplicate one. Deliberately does not attach a console/stream handler:
    the LaunchAgent sends stdout/stderr to ``/dev/null`` and relies on this
    bounded, on-disk structured log instead of an unbounded launchd output
    file. Log files never leave the configured data directory.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    for handler in logger.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            return logger

    log_dir = Path(data_dir).expanduser() / LOG_DIRNAME
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / LOG_FILENAME,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(_RedactingJsonFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log_run_event(
    logger: logging.Logger,
    message: str,
    *,
    run_id: str | None = None,
    state: WorkflowState | str | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one structured log record, optionally tagged with ``run_id``/
    ``state`` for correlation, without callers having to remember the
    ``extra=`` mapping shape ``_RedactingJsonFormatter`` expects."""
    extra: dict[str, object] = {}
    if run_id is not None:
        extra["run_id"] = run_id
    if state is not None:
        extra["state"] = state
    logger.log(level, message, extra=extra)
