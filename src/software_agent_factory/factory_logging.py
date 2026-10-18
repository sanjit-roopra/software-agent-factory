"""Structured JSON logging for the factory.

A lightweight, stdlib-only structured JSON logging setup with a bounded
``RotatingFileHandler`` under the configured data directory, so operators get
durable, size-bounded logs while the LaunchAgent sends stdout/stderr to
``/dev/null`` instead of an unbounded launchd output file (``PLAN.md`` Phase
15.2). Split out of :mod:`software_agent_factory.observability`.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

from .models import WorkflowState
from .redaction import redact_secrets

LOG_DIRNAME = "logs"
LOG_FILENAME = "factory.log"
DEFAULT_LOG_MAX_BYTES = 5_000_000
DEFAULT_LOG_BACKUP_COUNT = 5

#: Marks a handler this module attached, so :func:`configure_factory_logging`
#: can be called repeatedly (once per entry point) without stacking
#: duplicate handlers on the same logger.
_HANDLER_MARKER = "_software_agent_factory_structured_handler"


class _RedactingJsonFormatter(logging.Formatter):
    """Renders one JSON object per line: ``timestamp``, ``level``,
    ``logger``, ``message``, plus ``run_id``/``state`` when supplied via
    ``extra``.

    The rendered message is passed through the same credential redaction
    used for captured command output (``redaction.redact_secrets``).
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
