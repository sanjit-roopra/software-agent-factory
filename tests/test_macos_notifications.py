from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from software_agent_factory.config import NotificationsConfig, load_config
from software_agent_factory.macos_notifications import (
    NOTIFICATION_TEXT_LIMIT,
    NOTIFICATION_TITLE,
    OSASCRIPT,
    notify_run_outcome,
)
from software_agent_factory.models import FactoryRun, WorkflowState
from software_agent_factory.store import FileRunStore

ON = NotificationsConfig(macos=True)


class FakeRunner:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.error = error

    def __call__(self, args: list[str], **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error


def _store(tmp_path: Path, state: WorkflowState, reason: str | None = None) -> FileRunStore:
    store = FileRunStore(tmp_path)
    store.save_run(
        FactoryRun(id="run-1", work_item_id="github:o/r#7", state=state, failure_reason=reason)
    )
    return store


def test_needs_human_posts_a_redacted_bounded_notice(tmp_path: Path) -> None:
    secret = "ghp_" + "a" * 36
    reason = f"token={secret} " + "x" * 1000
    runner = FakeRunner()
    notify_run_outcome(
        ON,
        _store(tmp_path, WorkflowState.NEEDS_HUMAN, reason),
        "run-1",
        runner=runner,
        platform="darwin",
    )
    [(args, kwargs)] = runner.calls
    assert args[0] == OSASCRIPT
    assert args[-1] == NOTIFICATION_TITLE
    text = args[-2]
    assert text.startswith("github:o/r#7 needs you: ")
    assert secret not in text
    assert len(text) <= NOTIFICATION_TEXT_LIMIT
    # The text is an argument to the script, never part of it.
    assert all(text not in part for part in args[1:-2])
    assert kwargs["timeout"] > 0


def test_finished_run_posts_its_state(tmp_path: Path) -> None:
    runner = FakeRunner()
    store = _store(tmp_path, WorkflowState.DONE)
    notify_run_outcome(ON, store, "run-1", runner=runner, platform="darwin")
    assert runner.calls[0][0][-2] == "github:o/r#7 finished: DONE"


def test_unfinished_run_posts_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    store = _store(tmp_path, WorkflowState.IMPLEMENTING)
    notify_run_outcome(ON, store, "run-1", runner=runner, platform="darwin")
    assert runner.calls == []


def test_other_platforms_and_flag_off_post_nothing(tmp_path: Path) -> None:
    runner = FakeRunner()
    store = _store(tmp_path, WorkflowState.DONE)
    notify_run_outcome(ON, store, "run-1", runner=runner, platform="linux")
    notify_run_outcome(NotificationsConfig(), store, "run-1", runner=runner, platform="darwin")
    assert runner.calls == []


def test_failures_are_logged_not_raised(tmp_path: Path, caplog: Any) -> None:
    runner = FakeRunner(subprocess.TimeoutExpired(OSASCRIPT, 10))
    store = _store(tmp_path, WorkflowState.DONE)
    notify_run_outcome(ON, store, "run-1", runner=runner, platform="darwin")
    notify_run_outcome(ON, store, "missing-run", runner=FakeRunner(), platform="darwin")
    assert caplog.text.count("could not post a macOS notification") == 2


def test_default_config_keeps_notifications_off() -> None:
    assert load_config().notifications.macos is False
