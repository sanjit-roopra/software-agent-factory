"""Read-only local dashboard (Phase 15.11, ADR-016).

An explicitly bounded exception to the "no web dashboard" rule in
``AGENTS.md``: loopback-only, token-protected, ``GET``-only, standard-library
only, disabled unless something explicitly starts it. See
``docs/architecture.md`` ("Local dashboard") and ``docs/decisions.md``
(ADR-016) for the constraints this package must satisfy.

This package is intentionally self-contained. It does not import
``workflow``, ``service``, ``publishing``, GitHub mutation helpers,
``subprocess`` or any shell helper, and it never starts a server as a side
effect of import -- callers must construct a :class:`DashboardConfig` and
call :func:`create_server` explicitly.
"""

from .handler import DashboardRequestHandler
from .sanitize import (
    ATTEMPT_FIELDS,
    RUN_DETAIL_FIELDS,
    RUN_SUMMARY_FIELDS,
    sanitize_attempt,
    sanitize_run_detail,
    sanitize_run_summary,
)
from .security import (
    LOOPBACK_HOST,
    TOKEN_HEADER,
    TOKEN_QUERY_PARAM,
    InvalidBindHostError,
    expected_origin,
    generate_token,
    host_header_is_valid,
    origin_header_is_valid,
    token_matches,
    validate_bind_host,
)
from .server import DashboardConfig, DashboardServer, create_server
from .snapshot import (
    HealthProvider,
    RunDetailProvider,
    SnapshotProvider,
    is_valid_run_id,
    to_json_safe,
)

__all__ = [
    "ATTEMPT_FIELDS",
    "DashboardConfig",
    "DashboardRequestHandler",
    "DashboardServer",
    "HealthProvider",
    "InvalidBindHostError",
    "LOOPBACK_HOST",
    "RUN_DETAIL_FIELDS",
    "RUN_SUMMARY_FIELDS",
    "RunDetailProvider",
    "SnapshotProvider",
    "TOKEN_HEADER",
    "TOKEN_QUERY_PARAM",
    "create_server",
    "expected_origin",
    "generate_token",
    "host_header_is_valid",
    "is_valid_run_id",
    "origin_header_is_valid",
    "sanitize_attempt",
    "sanitize_run_detail",
    "sanitize_run_summary",
    "to_json_safe",
    "token_matches",
    "validate_bind_host",
]
