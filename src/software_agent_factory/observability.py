"""Phase 15.5 local monitoring and health (``PLAN.md`` Phase 15.5).

Per ``docs/architecture.md`` ("Health and metrics"), persisted run artifacts
are the source of truth: health and metrics are *derived on demand* from the
run store rather than accumulated in a separate counter store or time-series
database. There is deliberately no second metrics truth source here -- this
module only reads what :class:`~software_agent_factory.store.FileRunStore`
already persists (``FactoryRun``, ``WorkItem``, ``TriageResult``). It has no
import-time dependency on ``workflow.py`` (the orchestration/mutation
authority): it classifies a run with :mod:`software_agent_factory.lifecycle`,
which imports only the models, so this read-only path never pulls in
``WorkflowController``/agents/publishing/routing/workspace code.

Public surfaces:

- :func:`build_monitoring_snapshot` -- a pure, read-only function that turns
  a run store into a typed, JSON-serializable :class:`MonitoringSnapshot`:
  paginated run summaries, store-wide counts (succeeded, escalated, failed,
  active, stale-active), attempt tallies by role/model, and aggregate
  metrics (attempts per run, first-pass success rate, scope replans, CI
  repair attempts, completed-run duration). Rendered by ``factory status``
  and by the Phase 15.11 dashboard -- neither of which this module
  imports or depends on.
- :func:`build_run_detail` -- the single-run counterpart: a :class:`RunDetail` (summary,
  completion facts, attempts and state moves) or ``None`` for an unreadable run.
- Operational health lives in :mod:`.operational_health`, structured logging in
  :mod:`.factory_logging`, and run classification, usage totals and aggregate metrics
  in :mod:`.run_metrics`.

Health is always derived at read time and never persisted as a boolean: a
run's "stale" or "finished" status is recomputed from its timestamps against
the caller-supplied ``now``/``stale_after`` on every call, and
``MonitoringSnapshot.degraded`` reflects only what was actually unreadable
during *this* call. Nothing here mutates a run, a workspace, or
configuration.

Bounded work per request: listing run directories reads only metadata, but at most
``max_scanned_runs`` ``run.json`` files are parsed per call, newest first. A capped scan
says so via ``scanned_runs``/``scan_truncated``/``degraded_reasons``.

Usage is summed only from persisted ``InvocationRecord`` values; a missing field stays
unknown and nothing is priced or converted to USD here.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import Field, ValidationError, model_serializer

from .attempt_summary import RECENT_ATTEMPTS, RunAttemptSummary, attempt_summaries
from .escalation_protocol import (
    ANSWER_COMMAND_PATTERN,
    ReplyClosedCause,
    ReplyPolicy,
    halt_code_for_reason,
    reply_closed_cause,
)
from .lifecycle import is_run_finished
from .models import (
    HALT_REASON_COPY,
    MAX_GUIDANCE_FINDINGS,
    UNRESOLVED_DECISIONS_HALT_REASON,
    UNRESOLVED_DECISIONS_REPLACE_ACTION,
    ActiveInvocationStatus,
    AgentRole,
    AttemptBudget,
    Complexity,
    ContextTier,
    EscalationRecord,
    EscalationStatus,
    ExecutionPlan,
    ExecutionRoute,
    FactoryRun,
    HaltReasonCode,
    InvocationRecord,
    ModelBase,
    OperatorEscalation,
    PerformanceRecord,
    ResumeClassification,
    ResumeRefusal,
    ReviewFindingCategory,
    ReviewImpasse,
    Risk,
    RunListFilter,
    RunSteerNote,
    RunStopRequest,
    StateTransition,
    TriageResult,
    UsageMetrics,
    UtcDateTime,
    VerificationReport,
    VersionedModel,
    WorkflowState,
    WorkItem,
    unresolved_decisions_count,
    unresolved_decisions_summary,
    utc_now,
)
from .now_running import ActiveCallBrief, active_call_briefs
from .redaction import redact_secrets
from .resume import current_context_fingerprint, escalation_route, resume_refusal_within
from .run_metrics import (
    AggregateMetrics,
    UsageSummary,
    _classify_run,
    _compute_aggregate_metrics,
    _compute_attempt_tallies,
    _daily_usage,
    _failed_since,
    _tokens_since,
    resolve_usage,
    summarize_usage,
)
from .run_sort import sort_runs
from .store import ARTIFACT_FILENAMES, OPERATOR_ESCALATION_FILENAME
from .transcripts import read_call_tool_calls

#: The two review statuses the run view and the run guidance report.
_ACTION_REQUIRED = "ACTION_REQUIRED"
_ACCEPTED_WITH_FINDINGS = "ACCEPTED_WITH_FINDINGS"
_GITHUB_EXTERNAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")

__all__ = [
    "RunStoreProtocol",
    "RunScanResult",
    "scan_readable_runs",
    "PageMeta",
    "RunStateCounts",
    "RunSummary",
    "MonitoringSnapshot",
    "RunAttemptSummary",
    "RunInvocationSummary",
    "RunDetail",
    "RunGuidance",
    "DEFAULT_STALE_AFTER",
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_MAX_SCANNED_RUNS",
    "build_monitoring_snapshot",
    "build_run_detail",
]

#: Matches ``SchedulerConfig.stall_timeout_seconds``'s own default (15
#: minutes): a reasonable default definition of "no heartbeat/activity in a
#: while" for a run that a scheduler is not actively supervising (e.g. one
#: started by a bare ``factory run`` invocation, with no daemon involved).
#: Callers with their own configured stall timeout should pass it explicitly
#: instead of relying on this default.
DEFAULT_STALE_AFTER = timedelta(seconds=900)

DEFAULT_PAGE_LIMIT = 100

#: How far back the "last 24 hours" key figures look, measured from the
#: snapshot's own ``now`` so a fixed clock gives a fixed answer.
KEY_FIGURE_WINDOW = timedelta(hours=24)

#: Conservative hard cap on how many ``run.json`` files one call to
#: :func:`build_monitoring_snapshot` will open and parse, independent of
#: ``limit``/``offset``. Directory enumeration to *find* candidates is cheap
#: (filesystem metadata only) and stays unbounded; this cap only bounds the
#: expensive part (JSON parsing + Pydantic validation), so a store with an
#: unbounded number of historical runs can never turn one dashboard/CLI
#: request into unbounded work.
DEFAULT_MAX_SCANNED_RUNS = 1000


# Store surface


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


# Snapshot models


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
    see ``lifecycle.is_run_finished``. ``active`` is every
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


class RunCallBrief(ModelBase):
    """One call reduced to what the run list shows: its role, model and length."""

    role: AgentRole
    model: str
    duration_ms: int = Field(ge=0)


class RunSummary(ModelBase):
    """One dashboard-safe summary of a persisted run.

    Deliberately excludes anything that could carry repository content,
    prompts, tool output, diffs, or tokens/secrets: no command logs, no
    patch text, no agent reasoning, no raw artifact bodies. ``title`` is
    redacted with the same credential patterns applied to captured command
    output (``redaction.redact_secrets``) as defense in depth against an
    accidentally-pasted secret in a work item title.

    ``failure_reason`` is the raw stored text, like the one on
    :class:`RunDetail`: the dashboard sanitizer redacts and bounds it before it
    leaves the process. ``calls`` lists each finished call's role, model and
    length in call order, so the run list can show the models and the duration.
    ``active_role`` is the role of the call in progress, when one is.
    ``resume_classification`` is how a run that waits for a person can continue, so the
    run list can say what it needs without loading the run detail.
    """

    run_id: str
    work_item_id: str
    source_external_id: str | None = None
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
    risk_assessment_enabled: bool = True
    initial_route: ExecutionRoute | None = None
    effective_route: ExecutionRoute | None = None
    waiting_for_human: bool = False
    performance: PerformanceRecord | None = None
    failure_reason: str | None = None
    calls: list[RunCallBrief] = Field(default_factory=list)
    state_started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    pull_request_url: str | None = None
    merge_commit_sha: str | None = None
    unattended: bool = False
    needs_look: list[str] = Field(default_factory=list)
    restart_recoveries: int = Field(default=0, ge=0)
    #: The last ``factory escalate`` of the run and how it went (ADR-055).
    operator_escalation: OperatorEscalation | None = None
    active_role: AgentRole | None = None
    resume_classification: ResumeClassification | None = None
    #: The last attempts of an unfinished run, so the run list can flag a run that repeats.
    recent_attempts: list[RunAttemptSummary] = Field(default_factory=list)


class RunInvocationSummary(ModelBase):
    """One invocation with typed runtime-reported usage.

    ``reasoning`` is the reasoning level the call ran at (for example
    ``high``). ``failure_reason`` is the raw stored text, redacted and bounded
    by ``dashboard.sanitize`` before display.
    """

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: str
    model: str
    reasoning: str
    context_tier: ContextTier
    success: bool
    started_at: UtcDateTime
    completed_at: UtcDateTime
    attempt_number: int | None = Field(default=None, ge=1)
    failure_reason: str | None = None
    usage: UsageMetrics | None = None
    performance: PerformanceRecord | None = None
    runtime: str | None = None
    """The runtime that served the call, from its usage; ``None`` when unknown."""
    fallback_reason: str | None = None
    """Raw reason the configured runtime could not serve the call (ADR-045)."""
    tool_calls: list[dict[str, str]] = Field(default_factory=list)
    """Tools the call used, read from the small file stored with its transcript."""


class ActiveInvocationSummary(ModelBase):
    """Dashboard-safe status for an invocation that has not completed."""

    invocation_number: int = Field(ge=1)
    role: AgentRole
    purpose: str
    model: str
    reasoning: str
    context_tier: ContextTier
    status: ActiveInvocationStatus
    started_at: UtcDateTime
    attempt_number: int | None = Field(default=None, ge=1)
    tool_calls: list[dict[str, str]] = Field(default_factory=list)
    """The latest tool calls the call started, oldest first (dashboard X6)."""
    tool_call_count: int = Field(default=0, ge=0)
    """Every tool call the call started so far."""
    last_output_at: UtcDateTime | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)


class RunGuidance(ModelBase):
    status: str
    reason_code: HaltReasonCode
    summary: str
    next_action: str
    artifact: str | None = None
    finding_count: int = Field(default=0, ge=0)
    decision_count: int | None = Field(default=None, ge=0)
    finding_ids: list[str] = Field(default_factory=list, max_length=MAX_GUIDANCE_FINDINGS)
    category_counts: dict[ReviewFindingCategory, int] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = dict(handler(self))
        if self.decision_count is None:
            data.pop("decision_count", None)
        if self.reason_code is HaltReasonCode.UNRESOLVED_DECISIONS:
            data.pop("finding_count", None)
            data.pop("finding_ids", None)
            data.pop("category_counts", None)
        return data


class VerificationSummary(ModelBase):
    passed: bool
    check_count: int = Field(ge=0)
    failed_check_count: int = Field(ge=0)
    coverage_change: float | None = None


class ApprovalScopeSummary(ModelBase):
    """What approving a risk halt asks for, allows, excludes and keeps in force.

    Free text carried raw from the stored approval context; the dashboard
    sanitizer redacts it before display.
    """

    decision_requested: str
    authorized_actions: list[str]
    unauthorized_actions: list[str]
    conditions_in_force: list[str]


class EscalationSummary(ModelBase):
    status: EscalationStatus
    target_type: str | None = None
    comment_url: str | None = None
    reason_code: str
    resume_classification: str
    waiting_for_human: bool
    waiting_since: UtcDateTime | None = None
    episode_number: int = Field(ge=1)
    reopen_count: int = Field(ge=0)
    accepted_reply_count: int = Field(ge=0)
    last_responder: str | None = None
    last_action: Literal["ANSWER", "RESUME"] | None = None
    last_response_at: UtcDateTime | None = None
    is_resumed: bool = False
    resumed_at: UtcDateTime | None = None
    episode_id: str | None = None
    context_fingerprint: str | None = None
    max_reopens: int = Field(ge=1)
    #: Why a GitHub reply is not read now, or ``None`` when it is. No default: an omitted
    #: cause must not read as an open reply.
    reply_closed_cause: ReplyClosedCause | None
    #: Why the dashboard could not queue a resume now, or ``None`` when it can. Unlike
    #: ``reply_closed_cause`` this ignores the GitHub-only gates.
    dashboard_action_refusal: ResumeRefusal | None = None
    approval_scope: ApprovalScopeSummary | None = None
    decisions: list[str] = Field(default_factory=list)
    scope_paths: list[str] = Field(default_factory=list)


class RunDetail(ModelBase):
    """One run's read-only detail view: a :class:`RunSummary` plus completion
    facts and the attempt history.

    Same data-minimization rule as :class:`RunSummary`, applied to a single
    run: no command logs, no patch text, no prompts, no agent reasoning text
    and no raw artifact bodies. ``failure_reason`` (here and on each attempt
    and invocation) is a free-text exception: it is carried raw so the
    dashboard sanitizer can redact and bound it, so this model must not be
    shown to a user without that step. The escalation's approval scope, plan
    decision questions and each state move's reason follow the same rule.
    ``commit_sha`` and ``pull_request_url`` are controller identifiers, so both show.
    """

    run_id: str
    work_item_id: str
    source_external_id: str | None = None
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
    risk_assessment_enabled: bool = True
    initial_route: ExecutionRoute | None = None
    effective_route: ExecutionRoute | None = None
    waiting_for_human: bool = False
    performance: PerformanceRecord | None = None
    commit_sha: str | None = None
    pull_request_url: str | None = None
    merge_commit_sha: str | None = None
    verification: VerificationSummary | None = None
    artifacts: list[str] = Field(default_factory=list)
    escalation: EscalationSummary | None = None
    attempts: list[RunAttemptSummary] = Field(default_factory=list)
    invocations: list[RunInvocationSummary] = Field(default_factory=list)
    active_invocation: ActiveInvocationSummary | None = None
    guidance: RunGuidance | None = None
    failure_reason: str | None = None
    state_started_at: UtcDateTime | None = None
    unattended: bool = False
    needs_look: list[str] = Field(default_factory=list)
    restart_recoveries: int = Field(default=0, ge=0)
    #: The route ``factory escalate`` would move the run to, or ``None`` (ADR-055).
    escalation_route: ExecutionRoute | None = None
    operator_escalation: OperatorEscalation | None = None
    #: The waiting stop request and steer note; the dashboard adds them (ADR-049, ADR-051).
    stop_request: RunStopRequest | None = None
    steer_note: RunSteerNote | None = None
    transitions: list[StateTransition] = Field(default_factory=list)


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

    The key figures are derived from ``generated_at`` on every call, never
    stored (ADR-017). ``needs_human_count`` is the same count as ``counts.escalated``:
    the runs in ``NEEDS_HUMAN``, which the dashboard shows as "Needs you".
    ``failed_last_24h`` counts ``FAILED`` runs whose ``completed_at`` is strictly
    after ``generated_at`` minus 24 hours. ``tokens_last_24h`` adds the reported
    tokens of every invocation whose ``completed_at`` is strictly after that
    cutoff; a value a call did not report adds nothing. It is ``None`` when calls
    completed after the cutoff and none reported a counted token class, and ``0`` when
    no call did (unknown is never zero). All three cover only the scanned subset.
    ``daily_usage`` sums the usage of the calls that completed on each of the last
    ``COST_TREND_DAYS`` 24-hour periods before ``generated_at``, oldest first. ``active_calls``
    lists the calls in progress over the scanned runs (:mod:`.now_running`).
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
    needs_human_count: int = Field(default=0, ge=0)
    failed_last_24h: int = Field(default=0, ge=0)
    tokens_last_24h: int | None = Field(default=0, ge=0)
    daily_usage: list[UsageSummary] = Field(default_factory=list)
    attempts_by_role: dict[str, int] = Field(default_factory=dict)
    attempts_by_model: dict[str, int] = Field(default_factory=dict)
    metrics: AggregateMetrics
    page: PageMeta
    runs: list[RunSummary] = Field(default_factory=list)
    active_calls: list[ActiveCallBrief] = Field(default_factory=list)


# Snapshot construction


def build_monitoring_snapshot(
    store: RunStoreProtocol,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    limit: int = DEFAULT_PAGE_LIMIT,
    offset: int = 0,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
    scan: RunScanResult | None = None,
    reply_policy: ReplyPolicy | None = None,
    run_filter: RunListFilter | None = None,
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

    With ``reply_policy``, a waiting run's ``resume_classification`` is kept only when the
    dashboard would take a request for it now (see :func:`.resume.resume_refusal_within`).

    ``run_filter`` narrows and orders the page and its ``page.total`` only. The counts and
    metrics still cover every scanned run.
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
    cutoff = reference_time - KEY_FIGURE_WINDOW
    attempts_by_role, attempts_by_model = _compute_attempt_tallies(scan.readable_runs)
    metrics = _compute_aggregate_metrics(scan.readable_runs)

    listed = _listed_runs(store, scan, run_filter, reference_time, stale_after)
    total_readable = len(listed)
    page_runs = listed[offset : offset + limit]
    summaries = [
        _build_run_summary(
            run,
            reference_time,
            stale_after,
            _memoized_artifact_facts(store, run.id, scan.artifact_facts),
            reply_policy,
            load_operator_escalation(store, run.id),
        )
        for run in page_runs
    ]

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
        needs_human_count=counts.escalated,
        failed_last_24h=_failed_since(scan.readable_runs, cutoff),
        tokens_last_24h=_tokens_since(scan.readable_runs, cutoff),
        daily_usage=_daily_usage(scan.readable_runs, reference_time),
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
        active_calls=active_call_briefs(scan.readable_runs, lambda i: _title_of(store, i, scan)),
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
class _ArtifactFacts:
    """What a run summary takes from the work item and triage artifacts, already resolved."""

    title: str | None
    source_external_id: str | None
    complexity: Complexity | None
    risk: Risk | None
    project_id: str | None = None


@dataclass(frozen=True)
class RunScanResult:
    """The one bounded run-directory scan shared by
    :func:`build_monitoring_snapshot` and :func:`build_operational_health`,
    so both agree on exactly the same readable runs, scan cap, and degraded
    reasons for a given call.

    ``artifact_facts`` memoizes the per-run artifact reads of the summaries built from this
    scan, so builds that share the scan (and its copies) read each artifact file once.
    """

    readable_runs: list[FactoryRun]
    total_directories: int
    scanned_runs: int
    scan_truncated: bool
    unreadable_reasons: dict[str, int]
    artifact_facts: dict[str, _ArtifactFacts] = field(
        default_factory=dict, compare=False, repr=False
    )

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


def scan_readable_runs(
    store: RunStoreProtocol,
    max_scanned_runs: int = DEFAULT_MAX_SCANNED_RUNS,
    load_run: Callable[[str], FactoryRun] | None = None,
) -> RunScanResult:
    """Discover run directories (broad, cheap), then open and parse at most
    ``max_scanned_runs`` of the newest ones (bounded, expensive), returning
    every readable ``FactoryRun`` sorted newest-first plus honest scan
    metadata. A directory whose ``run.json`` is missing or fails to parse is
    tallied into ``unreadable_reasons`` rather than raised or dropped
    silently. ``load_run`` replaces ``store.load_run`` (a cache passes its own).
    """
    if max_scanned_runs <= 0:
        raise ValueError("max_scanned_runs must be > 0")
    candidate_run_ids, total_directories, scan_truncated = _select_scan_candidates(
        store, max_scanned_runs
    )

    load = store.load_run if load_run is None else load_run
    readable_runs: list[FactoryRun] = []
    unreadable_reasons: dict[str, int] = {}
    for run_id in candidate_run_ids:
        try:
            readable_runs.append(load(run_id))
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


def _review_status(run: FactoryRun) -> str | None:
    """``action_required`` for a halted run, ``accepted_with_findings`` after an acceptance."""
    if run.state is WorkflowState.NEEDS_HUMAN:
        return _ACTION_REQUIRED
    if run.review_acceptance is not None:
        return _ACCEPTED_WITH_FINDINGS
    return None


def _is_stale(run: FactoryRun, now: datetime, stale_after: timedelta) -> bool:
    signal = _last_signal_at(run) or run.created_at
    return (now - signal) > stale_after


def _list_states(run: FactoryRun, now: datetime, stale_after: timedelta) -> list[str]:
    """The run list states ``run`` is in: its class, and ``stuck`` too when active and stale."""
    states = [_classify_run(run)]
    if states[0] == "active" and _is_stale(run, now, stale_after):
        states.append("stuck")
    return states


def _compute_state_counts(
    runs: list[FactoryRun],
    now: datetime,
    stale_after: timedelta,
) -> RunStateCounts:
    tally = Counter(state for run in runs for state in _list_states(run, now, stale_after))
    return RunStateCounts(
        succeeded=tally["succeeded"],
        escalated=tally["escalated"],
        failed=tally["failed"],
        active=tally["active"],
        stale_active=tally["stuck"],
    )


def _load_optional_artifact(
    store: RunStoreProtocol,
    run_id: str,
    artifact_type: type[Any],
    attempt: int | None = None,
) -> Any | None:
    try:
        if attempt is None:
            return store.load_artifact(run_id, artifact_type)
        return store.load_artifact(run_id, artifact_type, attempt=attempt)
    except (OSError, ValueError):
        return None


def load_operator_escalation(store: RunStoreProtocol, run_id: str) -> OperatorEscalation | None:
    """The last ``factory escalate`` of the run, or ``None`` when there is none or it is bad.

    A ``running`` record whose process is gone (killed, crashed, the host rebooted) reads as
    ``failed``, so it never blocks the run or hides the error.
    """
    try:
        loaded: OperatorEscalation = store.load_artifact(
            run_id, OperatorEscalation, OPERATOR_ESCALATION_FILENAME
        )
    except (OSError, ValueError):
        return None
    if loaded.status == "running" and not process_alive(loaded.pid):
        return loaded.model_copy(
            update={"status": "failed", "error": "the escalation stopped before it finished"}
        )
    return loaded


def process_alive(pid: int | None) -> bool:
    """Return true when a process with ``pid`` exists on this host."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_external_id(work_item: WorkItem | None) -> str | None:
    if work_item is None or work_item.external_id is None:
        return None
    external_id = work_item.external_id
    if len(external_id) <= 200 and _GITHUB_EXTERNAL_ID_PATTERN.fullmatch(external_id):
        return external_id
    return None


def _verification_summary(report: VerificationReport | None) -> VerificationSummary | None:
    if report is None:
        return None
    failed = sum(
        1 for check in report.deterministic_checks if check.exit_code != 0 or check.timed_out
    )
    return VerificationSummary(
        passed=report.passed,
        check_count=len(report.deterministic_checks),
        failed_check_count=failed,
        coverage_change=report.coverage_change,
    )


def _artifact_inventory(store: RunStoreProtocol, run_id: str) -> list[str]:
    run_dir = store.runs_dir / run_id
    if not run_dir.is_dir():
        return []
    return sorted(
        filename for filename in set(ARTIFACT_FILENAMES.values()) if (run_dir / filename).is_file()
    )


def _escalation_summary(
    run: FactoryRun,
    escalation: EscalationRecord | None,
    now: datetime,
    policy: ReplyPolicy,
) -> EscalationSummary | None:
    if escalation is None:
        return None
    approval, plan = escalation.approval_context, escalation.plan_decision_context
    scope = escalation.scope_approval_context
    last_reply = escalation.accepted_replies[-1] if escalation.accepted_replies else None
    is_resumed = escalation.status in {EscalationStatus.REOPENED, EscalationStatus.RESUMED}
    return EscalationSummary(
        status=escalation.status,
        target_type=escalation.target_type.value if escalation.target_type is not None else None,
        comment_url=escalation.comment_url,
        reason_code=escalation.reason_code,
        resume_classification=escalation.resume_classification.value,
        waiting_for_human=run.state is WorkflowState.NEEDS_HUMAN,
        waiting_since=(escalation.created_at if run.state is WorkflowState.NEEDS_HUMAN else None),
        episode_number=escalation.episode_number,
        reopen_count=escalation.reopen_count,
        accepted_reply_count=len(escalation.accepted_replies),
        last_responder=last_reply.user_login if last_reply is not None else None,
        last_action=(
            "ANSWER"
            if last_reply is not None
            and ANSWER_COMMAND_PATTERN.fullmatch(last_reply.command.split("\n", 1)[0])
            else "RESUME"
            if last_reply is not None
            else None
        ),
        last_response_at=last_reply.created_at if last_reply is not None else None,
        is_resumed=is_resumed,
        resumed_at=escalation.updated_at if is_resumed else None,
        episode_id=escalation.episode_id,
        context_fingerprint=current_context_fingerprint(escalation),
        max_reopens=policy.max_reopens,
        reply_closed_cause=reply_closed_cause(escalation, policy, now),
        dashboard_action_refusal=resume_refusal_within(run, policy, now),
        approval_scope=(
            ApprovalScopeSummary(
                decision_requested=approval.decision_requested,
                authorized_actions=approval.authorized_actions,
                unauthorized_actions=approval.unauthorized_actions,
                conditions_in_force=approval.conditions_in_force,
            )
            if approval is not None
            else None
        ),
        decisions=list(plan.decisions) if plan is not None else [],
        scope_paths=list(scope.paths) if scope is not None else [],
    )


def _artifact_facts(store: RunStoreProtocol, run_id: str) -> _ArtifactFacts:
    """Read the work item and triage of one run; a missing or unreadable one counts as absent."""
    work_item = _load_optional_artifact(store, run_id, WorkItem)
    triage = _load_optional_artifact(store, run_id, TriageResult)

    title = redact_secrets(work_item.title) if work_item is not None else None
    return _ArtifactFacts(
        title=title,
        source_external_id=_safe_external_id(work_item),
        complexity=triage.complexity if triage is not None else None,
        risk=triage.risk if triage is not None else None,
        project_id=work_item.project_id if work_item is not None else None,
    )


def _listed_runs(
    store: RunStoreProtocol,
    scan: RunScanResult,
    run_filter: RunListFilter | None,
    now: datetime,
    stale_after: timedelta,
) -> list[FactoryRun]:
    """The scanned runs ``run_filter`` keeps, in its order. Newest first by default."""
    if run_filter is None:
        return scan.readable_runs
    kept = [
        run
        for run in scan.readable_runs
        if (run_filter.state is None or run_filter.state in _list_states(run, now, stale_after))
        and _run_matches(store, run, run_filter, scan.artifact_facts)
    ]
    return sort_runs(kept, run_filter.sort, now, lambda run_id: _title_of(store, run_id, scan))


def _title_of(store: RunStoreProtocol, run_id: str, scan: RunScanResult) -> str | None:
    return _memoized_artifact_facts(store, run_id, scan.artifact_facts).title


def _run_matches(
    store: RunStoreProtocol,
    run: FactoryRun,
    run_filter: RunListFilter,
    memo: dict[str, _ArtifactFacts],
) -> bool:
    if run_filter.text is None and run_filter.project_id is None:
        return True
    facts = _memoized_artifact_facts(store, run.id, memo)
    if run_filter.project_id is not None and facts.project_id != run_filter.project_id:
        return False
    if run_filter.text is None:
        return True
    needle = run_filter.text.casefold()
    fields = (run.id, run.work_item_id, facts.source_external_id, facts.title)
    return any(needle in field.casefold() for field in fields if field)


def _memoized_artifact_facts(
    store: RunStoreProtocol, run_id: str, memo: dict[str, _ArtifactFacts]
) -> _ArtifactFacts:
    """The facts of ``run_id`` from ``memo``, read on a miss.

    Threads share ``memo`` and only look up or add one key, never iterate it, and
    ``setdefault`` adds atomically: two threads that miss together agree on one value.
    """
    facts = memo.get(run_id)
    if facts is None:
        facts = memo.setdefault(run_id, _artifact_facts(store, run_id))
    return facts


def _build_run_summary(
    run: FactoryRun,
    now: datetime,
    stale_after: timedelta,
    facts: _ArtifactFacts,
    reply_policy: ReplyPolicy | None = None,
    operator_escalation: OperatorEscalation | None = None,
) -> RunSummary:
    signal = _last_signal_at(run) or run.created_at
    finished = is_run_finished(run)
    implementation_attempts = sum(
        1 for attempt in run.attempt_records if attempt.budget is AttemptBudget.IMPLEMENTATION
    )
    ci_repair_attempts = sum(
        1 for attempt in run.attempt_records if attempt.budget is AttemptBudget.CI_REPAIR
    )

    return RunSummary(
        run_id=run.id,
        work_item_id=run.work_item_id,
        source_external_id=facts.source_external_id,
        title=facts.title,
        state=run.state,
        complexity=facts.complexity,
        risk=facts.risk,
        created_at=run.created_at,
        updated_at=run.updated_at,
        age_seconds=max((now - run.created_at).total_seconds(), 0.0),
        idle_seconds=max((now - signal).total_seconds(), 0.0),
        attempt_count=len(run.attempt_records),
        invocation_count=len(run.invocation_records),
        implementation_attempts=implementation_attempts,
        ci_repair_attempts=ci_repair_attempts,
        usage=summarize_usage(run.invocation_records),
        is_finished=finished,
        is_stale=(not finished) and _is_stale(run, now, stale_after),
        review_status=_review_status(run),
        risk_assessment_enabled=run.risk_assessment_enabled,
        initial_route=run.initial_route,
        effective_route=run.effective_route,
        waiting_for_human=run.state is WorkflowState.NEEDS_HUMAN,
        performance=run.performance,
        failure_reason=run.failure_reason,
        calls=_call_briefs(run.invocation_records),
        state_started_at=run.state_started_at,
        completed_at=run.completed_at,
        pull_request_url=run.pull_request_url,
        merge_commit_sha=run.merge_commit_sha,
        unattended=run.unattended,
        needs_look=list(run.needs_look),
        restart_recoveries=run.restart_recoveries,
        operator_escalation=operator_escalation,
        active_role=(
            run.active_invocation.role if run.active_invocation and not finished else None
        ),
        resume_classification=_page_resume_classification(run, now, reply_policy),
        recent_attempts=[]
        if finished
        else attempt_summaries(run.attempt_records[-RECENT_ATTEMPTS:]),
    )


def _page_resume_classification(
    run: FactoryRun, now: datetime, policy: ReplyPolicy | None
) -> ResumeClassification | None:
    """How a waiting run can continue, or ``None`` when it does not wait or the page cannot act.

    The run list badge reads it, so it follows the same refusal as the run page's action.
    Without ``policy`` the reply window and the reopen limit are not judged.
    """
    if run.escalation is None or run.state is not WorkflowState.NEEDS_HUMAN:
        return None
    if policy is not None and resume_refusal_within(run, policy, now) is not None:
        return None
    return run.escalation.resume_classification


def _call_briefs(invocations: Iterable[InvocationRecord]) -> list[RunCallBrief]:
    """Each call's role, model and length, in call order."""
    return [
        RunCallBrief(
            role=record.role,
            model=record.model,
            duration_ms=_elapsed_ms(record.started_at, record.completed_at),
        )
        for record in sorted(invocations, key=lambda record: record.invocation_number)
    ]


def _elapsed_ms(started_at: datetime, completed_at: datetime) -> int:
    """Whole milliseconds between the two times of a call, which never ends before it starts."""
    return (completed_at - started_at) // timedelta(milliseconds=1)


def _served_runtime(invocation: InvocationRecord) -> str | None:
    """The runtime its usage says served the call, or ``None`` when no usage names one."""
    model_usage = invocation.usage.model_usage if invocation.usage is not None else ()
    return next((str(usage.runtime) for usage in model_usage if usage.runtime), None)


def _call_tool_calls(
    store: RunStoreProtocol, run_id: str, invocation: InvocationRecord
) -> list[dict[str, str]]:
    """The tool calls stored with the call's transcript; none when it has no transcript."""
    if invocation.transcript_path is None:
        return []
    return read_call_tool_calls(store.runs_dir / run_id, invocation.invocation_number)


def build_run_detail(
    store: RunStoreProtocol,
    run_id: str,
    *,
    now: datetime | None = None,
    stale_after: timedelta = DEFAULT_STALE_AFTER,
    reply_policy: ReplyPolicy,
) -> RunDetail | None:
    """Derive one run's read-only detail view, or ``None`` if it is not readable.

    Read-only like :func:`build_monitoring_snapshot`: it never creates, mutates or
    repairs anything on disk. ``None``, never an exception, is returned for a run id
    that does not exist, is malformed or hostile, or whose ``run.json`` does not
    parse, so the dashboard maps it straight onto a 404.
    """
    try:
        run = store.load_run(run_id)
    except (OSError, ValueError):  # ValidationError is a ValueError in Pydantic v2
        return None

    observed_at = _normalize_now(now)
    operator_escalation = load_operator_escalation(store, run.id)
    summary = _build_run_summary(
        run, observed_at, stale_after, _artifact_facts(store, run.id), None, operator_escalation
    )
    verification = _load_optional_artifact(store, run.id, VerificationReport)
    return RunDetail(
        **summary.model_dump(
            exclude={
                "calls",
                "failure_reason",
                "active_role",
                "resume_classification",
                "recent_attempts",
            }
        ),
        commit_sha=run.commit_sha,
        verification=_verification_summary(verification),
        artifacts=_artifact_inventory(store, run.id),
        escalation=_escalation_summary(run, run.escalation, observed_at, reply_policy),
        # One escalation at a time: the page offers none while one runs.
        escalation_route=(
            None
            if operator_escalation is not None and operator_escalation.status == "running"
            else escalation_route(run)
        ),
        attempts=attempt_summaries(run.attempt_records),
        invocations=[
            RunInvocationSummary(
                invocation_number=invocation.invocation_number,
                role=invocation.role,
                purpose=str(invocation.purpose),
                model=invocation.model,
                reasoning=invocation.reasoning,
                context_tier=invocation.context_tier,
                success=invocation.success,
                started_at=invocation.started_at,
                completed_at=invocation.completed_at,
                attempt_number=invocation.attempt_number,
                failure_reason=invocation.failure_reason,
                usage=resolve_usage(invocation.usage) if invocation.usage is not None else None,
                performance=invocation.performance,
                runtime=_served_runtime(invocation),
                fallback_reason=invocation.fallback_reason,
                tool_calls=_call_tool_calls(store, run.id, invocation),
            )
            for invocation in run.invocation_records
        ],
        active_invocation=build_active_invocation_summary(
            run,
            now=_normalize_now(now),
            stale_after=stale_after,
        ),
        guidance=_build_run_guidance(store, run),
        failure_reason=run.failure_reason,
        transitions=run.transitions,
    )


def _build_run_guidance(store: RunStoreProtocol, run: FactoryRun) -> RunGuidance | None:
    if run.state is not WorkflowState.NEEDS_HUMAN and run.review_acceptance is not None:
        copy = HALT_REASON_COPY[HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE]
        return RunGuidance(
            status=_ACCEPTED_WITH_FINDINGS,
            reason_code=HaltReasonCode.BOUNDED_REVIEW_ACCEPTANCE,
            summary=copy.summary,
            next_action=copy.next_action,
            artifact="review-acceptance.json",
            finding_count=len(run.review_acceptance.findings),
            finding_ids=[finding.id for finding in run.review_acceptance.findings][
                :MAX_GUIDANCE_FINDINGS
            ],
            category_counts=_review_category_counts(run.review_acceptance.findings),
        )
    if run.state is not WorkflowState.NEEDS_HUMAN:
        return None

    impasse = _load_optional_artifact(store, run.id, ReviewImpasse)
    if impasse is not None:
        copy = HALT_REASON_COPY[HaltReasonCode.REVIEW_IMPASSE]
        return RunGuidance(
            status=_ACTION_REQUIRED,
            reason_code=HaltReasonCode.REVIEW_IMPASSE,
            summary=copy.summary,
            next_action=copy.next_action,
            artifact="review-impasse.json",
            finding_count=len(impasse.finding_ids),
            finding_ids=impasse.finding_ids[:MAX_GUIDANCE_FINDINGS],
            category_counts=_review_category_counts(run.review_ledger.open_findings),
        )

    reason = (run.failure_reason or "").lower()
    if reason.startswith(UNRESOLVED_DECISIONS_HALT_REASON):
        return _unresolved_decisions_guidance(store, run, reason)
    # The code the escalation recorded at the halt, else the one the reason text selects.
    code = (
        HaltReasonCode.parse(run.escalation.reason_code) if run.escalation else None
    ) or halt_code_for_reason(reason)
    copy = HALT_REASON_COPY[code]
    return RunGuidance(
        status=_ACTION_REQUIRED,
        reason_code=code,
        summary=copy.summary,
        next_action=copy.next_action,
    )


def _unresolved_decisions_guidance(
    store: RunStoreProtocol, run: FactoryRun, reason: str
) -> RunGuidance:
    plan = _load_optional_artifact(store, run.id, ExecutionPlan)
    unresolved_count = unresolved_decisions_count(plan, reason)
    reply_enabled = (
        run.escalation is not None
        and run.escalation.resume_classification is ResumeClassification.PLAN_DECISION
        and run.escalation.status is EscalationStatus.NOTIFIED
        and run.escalation.remote_resume_enabled
    )
    action = (
        HALT_REASON_COPY[HaltReasonCode.UNRESOLVED_DECISIONS].next_action
        if reply_enabled
        else UNRESOLVED_DECISIONS_REPLACE_ACTION
    )
    return RunGuidance(
        status=_ACTION_REQUIRED,
        reason_code=HaltReasonCode.UNRESOLVED_DECISIONS,
        summary=unresolved_decisions_summary(unresolved_count),
        next_action=action,
        artifact="execution-plan.json",
        decision_count=unresolved_count,
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
    if active is None or is_run_finished(run):
        return None
    status = _active_invocation_status(run, _normalize_now(now), stale_after)
    return ActiveInvocationSummary(
        invocation_number=active.invocation_number,
        role=active.role,
        purpose=str(active.purpose),
        model=active.model,
        reasoning=active.reasoning,
        context_tier=active.context_tier,
        status=status,
        started_at=active.started_at,
        attempt_number=active.attempt_number,
        tool_calls=list(active.recent_tool_calls),
        tool_call_count=active.tool_call_count,
        last_output_at=active.last_output_at,
        timeout_seconds=active.timeout_seconds,
    )


def _active_invocation_status(
    run: FactoryRun, now: datetime, stale_after: timedelta
) -> ActiveInvocationStatus:
    lease = run.lease
    if lease is None:
        return "abandoned"
    if lease.host == socket.gethostname():
        return "running" if process_alive(lease.pid) else "crashed"
    if _is_stale(run, now, stale_after):
        return "stale"
    return "running"
