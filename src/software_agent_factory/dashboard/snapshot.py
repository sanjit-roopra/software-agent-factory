"""Injectable snapshot/detail providers, decoupled from any concrete store.

The dashboard never imports ``software_agent_factory.observability`` (which
does not exist yet) or ``software_agent_factory.store`` directly. Instead it
depends on two small callables supplied by whoever wires the dashboard up:

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
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Protocol

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
    return bool(_RUN_ID_PATTERN.match(candidate))


class SnapshotProvider(Protocol):
    def __call__(self, *, limit: int, offset: int) -> Any: ...


class RunDetailProvider(Protocol):
    def __call__(self, run_id: str) -> Any | None: ...


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
