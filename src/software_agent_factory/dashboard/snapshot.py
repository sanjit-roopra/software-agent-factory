"""Injectable snapshot/detail providers, decoupled from any concrete store.

The dashboard never imports ``software_agent_factory.observability`` (which
does not exist yet) or ``software_agent_factory.store`` directly. Instead it
depends on small callables supplied by whoever wires the dashboard up:

``SnapshotProvider``
    Called for ``/api/summary`` and ``/api/runs``. Expected to mirror the
    planned ``observability.build_monitoring_snapshot(store, *, now=None,
    stale_after=..., limit=..., offset=...)`` signature closely enough that a
    thin wrapper (``lambda **kw: build_monitoring_snapshot(store, **kw)``) can
    be passed straight in. May return a Pydantic model (``model_dump``), a
    dataclass instance, or a plain ``dict`` -- ``to_json_safe`` normalizes any
    of the three into JSON-serializable data.

``RunDetailProvider``
    Called for ``/api/runs/{run_id}`` with an already-validated run id. Must
    return ``None`` when the run does not exist (rendered as 404) and may
    return a Pydantic model, dataclass or dict otherwise.

Neither provider is invoked with anything the dashboard has not already
validated, and neither is expected to perform writes; the dashboard only ever
calls them from `GET`/`HEAD` handling.

``ResumeRunReader`` and ``ResumeRequester`` serve the approve, answer and retry
routes (ADR-033, ADR-042). The reader only reads. The requester makes the one write the
dashboard asks for, a create-only request file; it never changes a run.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ..models import PROJECT_ID_PATTERN, RUN_LIST_SORTS, RUN_LIST_STATES, RunListFilter

if TYPE_CHECKING:
    from ..models import DashboardResumeRequest, FactoryRun

#: Hard ceiling on requested page size, independent of what any caller asks
#: for. Keeps one client from forcing an unbounded read/serialize.
MAX_PAGE_LIMIT = 100

#: Sensible default when a caller does not specify a page size.
DEFAULT_PAGE_LIMIT = 20

#: Hard ceiling on the requested offset. Large offsets are almost certainly
#: not a legitimate use of a local dashboard and are rejected outright.
MAX_PAGE_OFFSET = 1_000_000

#: Run identifiers are generated internally as ``uuid4().hex``/``str(uuid4())``
#: style tokens. This allowlist is intentionally strict: no ``.``, ``/`` or
#: ``\\`` can ever match, so a path-traversal attempt is rejected by shape
#: alone, before any provider ever sees the value.
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def is_valid_run_id(candidate: str) -> bool:
    """Return whether ``candidate`` is shaped like a real run id.

    This check happens independently of, and before, any provider call so a
    traversal-shaped id (``..``, ``/etc/passwd``, percent-encoded variants
    once decoded, embedded null bytes, etc.) is rejected without ever
    reaching a detail provider or store.
    """
    return bool(_RUN_ID_PATTERN.fullmatch(candidate))


class SnapshotProvider(Protocol):
    """``filters`` is passed only when the run list asks for a search, a state, a project
    or the oldest runs first, so a provider without it still serves every other request."""

    def __call__(self, *, limit: int, offset: int, filters: RunListFilter | None = None) -> Any: ...


class RunDetailProvider(Protocol):
    def __call__(self, run_id: str) -> Any | None: ...


class RunDiffProvider(Protocol):
    """Called for ``/api/runs/{run_id}/diff`` with a validated id and attempt index."""

    def __call__(self, run_id: str, attempt: int | None) -> Any | None: ...


class RunTranscriptProvider(Protocol):
    """Called for ``/api/runs/{run_id}/calls/{n}/transcript`` with a validated id and number.

    Returns the stored transcript text, or ``None`` when the run or the file is missing.
    """

    def __call__(self, run_id: str, invocation_number: int, /) -> str | None: ...


class HealthProvider(Protocol):
    """Optional injectable for operational health findings (data directory
    writable, git/gh/copilot present, launchd service state, and so on --
    see ``docs/architecture.md`` "Health and metrics"). Deliberately separate
    from ``SnapshotProvider``: ``build_monitoring_snapshot`` only derives run
    counts/metrics from the run store and has no opinion on host
    prerequisites, and a real health provider (e.g. wrapping
    ``doctor.run_doctor``) legitimately needs to shell out to check
    ``git``/``gh``/``copilot`` -- something this package must never do
    itself. May return a Pydantic model, dataclass or dict, normalized the
    same way as ``SnapshotProvider`` via ``to_json_safe``.
    """

    def __call__(self) -> Any: ...


class ProjectProvider(Protocol):
    """Optional provider for bounded, dashboard-safe project summaries."""

    def __call__(self) -> Any: ...


class ProjectDetailProvider(Protocol):
    """Optional provider of one project with its calls, or ``None`` for an unknown id.

    Called with a project id the dashboard already validated.
    """

    def __call__(self, project_id: str) -> Any | None: ...


class ResumeRequestReader(Protocol):
    """Optional provider of the dashboard resume requests of one episode.

    Called for ``/api/runs/{run_id}`` with a run id and an episode id the
    dashboard already validated. Read only: it returns the requests a person
    queued from the page, each a dict (or model) with ``status``, ``reason``,
    ``action``, ``context_fingerprint`` and ``created_at``. The dashboard reads
    nothing else from a request, so an adapter should leave the answers out.
    """

    def __call__(self, run_id: str, episode_id: str) -> Iterable[Any]: ...


#: What the requester reports for one request: it was stored, one for the same episode and
#: context already exists, or the run is gone.
ResumeRequestResult = Literal["created", "exists", "run_missing"]


class ResumeRunReader(Protocol):
    """Provider of the stored run an approval or answer is checked against.

    Returns ``None`` when the run does not exist or cannot be read. Read only.
    """

    def __call__(self, run_id: str) -> FactoryRun | None: ...


class ResumeRequester(Protocol):
    """Provider that stores one dashboard resume request, create-only.

    The only write the dashboard asks for. It never changes a run: the factory
    service reads the request later and decides.
    """

    def __call__(self, run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult: ...


#: What a stop request comes to: stored, one already waiting, the run is gone, or the run
#: has already finished and has nothing to stop.
StopRequestResult = Literal["created", "exists", "run_missing", "finished"]


class StopRequester(Protocol):
    """Provider that stores one stop request for a run, create-only (ADR-047).

    It never changes a run: the controller that drives the run reads the request.
    """

    def __call__(self, run_id: str, created_at: datetime) -> StopRequestResult: ...


#: What a steer note comes to: stored, stored over a waiting one, the run is gone, or the
#: run has finished and has no next prompt.
SteerRequestResult = Literal["created", "replaced", "run_missing", "finished"]


class SteerRequester(Protocol):
    """Provider that stores the steer note of a run, over any waiting one (ADR-047, ADR-051).

    It never changes a run: the controller adds the note to the next agent prompt.
    """

    def __call__(self, run_id: str, note: str, created_at: datetime) -> SteerRequestResult: ...


#: What an escalation request comes to: started, the run is gone, or it cannot escalate.
EscalateRequestResult = Literal["started", "run_missing", "not_escalatable"]


class EscalateRequester(Protocol):
    """Provider that starts ``factory escalate`` for a run with a note (ADR-055).

    It never changes a run itself: the started command does, under the workspace lock.
    """

    def __call__(self, run_id: str, note: str) -> EscalateRequestResult: ...


#: The real ``build_monitoring_snapshot`` (``observability.py``) rejects
#: ``limit <= 0``, so ``/api/summary`` -- which only needs counts/health, not
#: the run page -- must still request at least one row rather than zero.
MIN_SNAPSHOT_LIMIT = 1


def to_json_safe(value: Any) -> Any:
    """Normalize a provider's return value into JSON-serializable data.

    Accepts, in order of preference: an object with ``model_dump`` (Pydantic
    v2), a dataclass instance, a plain ``dict``/``list``/scalar, or ``None``.
    Raises ``TypeError`` for anything else so a misconfigured provider fails
    loudly during development rather than serializing an opaque repr.
    """
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict | list | str | int | float | bool):
        return value
    raise TypeError(f"Cannot serialize snapshot value of type {type(value).__name__}")


def clamp_pagination(raw_limit: str | None, raw_offset: str | None) -> tuple[int, int] | None:
    """Parse and bound ``limit``/``offset`` query parameters.

    Returns ``(limit, offset)`` with ``limit`` silently capped to
    ``MAX_PAGE_LIMIT`` (a generous request is not an error, just bounded), or
    ``None`` if either parameter is present but not a valid non-negative
    integer, or if ``offset`` exceeds ``MAX_PAGE_OFFSET`` -- callers should
    treat ``None`` as a 400 Bad Request.
    """
    if raw_limit is None or raw_limit == "":
        limit = DEFAULT_PAGE_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            return None
        if limit < 1:
            return None
        limit = min(limit, MAX_PAGE_LIMIT)

    if raw_offset is None or raw_offset == "":
        offset = 0
    else:
        try:
            offset = int(raw_offset)
        except ValueError:
            return None
        if offset < 0 or offset > MAX_PAGE_OFFSET:
            return None

    return limit, offset


#: The longest search text ``/api/runs`` takes in ``q``.
MAX_SEARCH_LENGTH = 100

_PROJECT_ID = re.compile(PROJECT_ID_PATTERN)


def _first(query: dict[str, list[str]], name: str) -> str | None:
    """The first value of ``name``, stripped, or ``None`` when it is missing or blank."""
    value = query.get(name, [""])[0].strip()
    return value or None


def parse_run_filter(query: dict[str, list[str]]) -> RunListFilter | None:
    """Read ``q``, ``state``, ``project`` and ``sort`` from a ``/api/runs`` query.

    Returns ``None`` when a value is not one the run list takes; callers answer 400. A
    query without them gives the default filter, which keeps every run.
    """
    text = _first(query, "q")
    state = _first(query, "state")
    project = _first(query, "project")
    sort = _first(query, "sort") or "newest"
    if text is not None and len(text) > MAX_SEARCH_LENGTH:
        return None
    if state is not None and state not in RUN_LIST_STATES:
        return None
    if project is not None and not _PROJECT_ID.fullmatch(project):
        return None
    if sort not in RUN_LIST_SORTS:
        return None
    return RunListFilter(text, state, project, sort)
