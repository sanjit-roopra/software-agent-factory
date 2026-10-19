"""Opt-in macOS notification when a service run needs a person or finishes.

``notifications.macos`` turns it on. Only macOS posts one, through
``/usr/bin/osascript``. The text is redacted and short. A failure is logged and
never raised, so a notification can never change a run.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from .config import NotificationsConfig
from .lifecycle import is_run_finished
from .models import WorkflowState
from .redaction import bounded_reason
from .store import FileRunStore

logger = logging.getLogger(__name__)

OSASCRIPT = "/usr/bin/osascript"
NOTIFICATION_TITLE = "Software factory"
#: The longest body text. macOS shows only a few lines of a notification.
NOTIFICATION_TEXT_LIMIT = 200
NOTIFICATION_TIMEOUT_SECONDS = 10

#: Text and title go in as arguments, never into the script, so no text can change it.
_SCRIPT = (
    "-e",
    "on run argv",
    "-e",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "-e",
    "end run",
)

Runner = Callable[..., Any]


def notify_run_outcome(
    config: NotificationsConfig,
    store: FileRunStore,
    run_id: str,
    *,
    runner: Runner = subprocess.run,
    platform: str = sys.platform,
) -> None:
    """Post a notification when ``run_id`` waits for a person or has finished."""
    if not config.macos or platform != "darwin":
        return
    try:
        run = store.load_run(run_id)
        if not is_run_finished(run):
            return
        if run.state is WorkflowState.NEEDS_HUMAN:
            text = f"{run.work_item_id} needs you: {run.failure_reason or 'no reason given'}"
        else:
            text = f"{run.work_item_id} finished: {run.state.value}"
        text, _ = bounded_reason(text, NOTIFICATION_TEXT_LIMIT, run_id)
        runner(
            [OSASCRIPT, *_SCRIPT, text, NOTIFICATION_TITLE],
            check=True,
            capture_output=True,
            timeout=NOTIFICATION_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("could not post a macOS notification for run %s: %s", run_id, exc)
