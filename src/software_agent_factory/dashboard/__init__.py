"""Local dashboard (Phase 15.11, ADR-016, ADR-033).

An explicitly bounded exception to the "no web dashboard" rule in
``AGENTS.md``: loopback-only, token-protected, standard-library only, disabled
unless something explicitly starts it. It reads with ``GET``. Its only writes are
two ``POST`` routes that queue an approval or plan answers for the factory service
(ADR-033); every other write method is ``405``. See
``docs/architecture.md`` ("Local dashboard") and ``docs/decisions.md``
(ADR-016) for the constraints this package must satisfy.

This package is intentionally self-contained. It does not import
``workflow``, ``service``, ``publishing``, GitHub mutation helpers,
``subprocess`` or any shell helper, and it never starts a server as a side
effect of import -- callers must construct a :class:`DashboardConfig` and
call :func:`create_server` explicitly.

Wire and storage names keep ``invocation`` (``invocations``, ``invocation_count``,
``active_invocation``). The page calls the same thing a "call".
"""

from .actions import ConflictReason, ResumeActions
from .handler import DashboardRequestHandler
from .sanitize import (
    ATTEMPT_FIELDS,
    PROJECT_FIELDS,
    PROJECT_MODEL_FIELDS,
    PROJECT_TASK_FIELDS,
    RUN_DETAIL_FIELDS,
    RUN_SUMMARY_FIELDS,
    sanitize_attempt,
    sanitize_project,
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
    header_token_matches,
    host_header_is_valid,
    origin_header_is_valid,
    required_origin_is_valid,
    token_matches,
    validate_bind_host,
)
from .server import DashboardConfig, DashboardServer, create_server
from .snapshot import (
    HealthProvider,
    ProjectProvider,
    ResumeRequester,
    ResumeRequestReader,
    ResumeRequestResult,
    ResumeRunReader,
    RunDetailProvider,
    SnapshotProvider,
    is_valid_run_id,
    to_json_safe,
)

__all__ = [
    "ATTEMPT_FIELDS",
    "ConflictReason",
    "DashboardConfig",
    "DashboardRequestHandler",
    "DashboardServer",
    "HealthProvider",
    "InvalidBindHostError",
    "LOOPBACK_HOST",
    "PROJECT_FIELDS",
    "PROJECT_MODEL_FIELDS",
    "PROJECT_TASK_FIELDS",
    "ProjectProvider",
    "RUN_DETAIL_FIELDS",
    "RUN_SUMMARY_FIELDS",
    "ResumeActions",
    "ResumeRequestReader",
    "ResumeRequestResult",
    "ResumeRequester",
    "ResumeRunReader",
    "RunDetailProvider",
    "SnapshotProvider",
    "TOKEN_HEADER",
    "TOKEN_QUERY_PARAM",
    "create_server",
    "expected_origin",
    "generate_token",
    "header_token_matches",
    "host_header_is_valid",
    "is_valid_run_id",
    "origin_header_is_valid",
    "required_origin_is_valid",
    "sanitize_attempt",
    "sanitize_project",
    "sanitize_run_detail",
    "sanitize_run_summary",
    "to_json_safe",
    "token_matches",
    "validate_bind_host",
]
