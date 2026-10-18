"""The run page shows a waiting stop request and steer note (ADR-049, ADR-051)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dashboard_js import JsCall, function_source, run_functions
from factory_testing import REPLY_POLICY

from software_agent_factory.dashboard import assets as dashboard_assets
from software_agent_factory.dashboard.pending import sanitize_pending, with_pending
from software_agent_factory.dashboard.view import run_detail_view
from software_agent_factory.models import (
    FactoryRun,
    RunSteerNote,
    RunStopRequest,
    WorkflowState,
)
from software_agent_factory.observability import build_run_detail
from software_agent_factory.redaction import REDACTION_PLACEHOLDER
from software_agent_factory.store import FileRunStore

RUN_ID = "run-pending"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
NOW_ISO = "2026-10-18T12:00:00Z"
NOW_MS = int(NOW.timestamp() * 1000)
TOKEN = "ghp_" + "a" * 36
_JS = dashboard_assets.APP_JS


def _store(tmp_path: Path) -> FileRunStore:
    store = FileRunStore(tmp_path)
    store.save_run(FactoryRun(id=RUN_ID, work_item_id="w", state=WorkflowState.IMPLEMENTING))
    return store


def _detail(store: FileRunStore) -> dict[str, object]:
    detail = build_run_detail(store, RUN_ID, reply_policy=REPLY_POLICY)
    assert detail is not None
    return run_detail_view(with_pending(store, RUN_ID, detail))


def test_a_run_with_nothing_waiting_has_no_pending_fields(tmp_path: Path) -> None:
    detail = _detail(_store(tmp_path))

    assert "stop_request" not in detail
    assert "steer_note" not in detail


def test_the_run_detail_carries_the_waiting_stop_and_note(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_stop_request(RunStopRequest(run_id=RUN_ID, source="cli", created_at=NOW))
    store.create_steer_note(
        RunSteerNote(run_id=RUN_ID, source="dashboard", note="Keep the API.", created_at=NOW)
    )

    detail = _detail(store)

    assert detail["stop_request"] == {"source": "cli", "created_at": NOW_ISO}
    assert detail["steer_note"] == {
        "source": "dashboard",
        "created_at": NOW_ISO,
        "note": "Keep the API.",
    }


def test_the_sanitizer_redacts_the_note_and_drops_bad_entries() -> None:
    raw = {
        "stop_request": {"source": "elsewhere", "created_at": NOW_ISO},
        "steer_note": {
            "source": "cli",
            "created_at": NOW_ISO,
            "note": f"use {TOKEN} " + "x" * 3000,
            "run_id": RUN_ID,
        },
    }

    sanitized = sanitize_pending(raw)

    assert "stop_request" not in sanitized
    note = sanitized["steer_note"]["note"]
    assert TOKEN not in note
    assert REDACTION_PLACEHOLDER in note
    assert len(note) == 2000
    assert set(sanitized["steer_note"]) == {"source", "created_at", "note"}
    assert sanitize_pending({"stop_request": {"source": "cli", "created_at": "soon"}}) == {}
    assert sanitize_pending({"steer_note": {"source": "cli", "created_at": NOW_ISO}}) == {}
    assert sanitize_pending("not a detail") == {}


def test_the_page_shows_a_waiting_stop_and_note() -> None:
    sent = "2026-10-18T11:55:00Z"
    calls = [
        JsCall("stopRequestedText", ({"created_at": sent}, NOW_MS)),
        JsCall("stopRequestedText", (None, NOW_MS)),
        JsCall("steerWaitingView", ({"created_at": sent, "note": "Keep the API."}, NOW_MS)),
        JsCall("steerWaitingView", (None, NOW_MS)),
    ]

    values = [result.value for result in run_functions(_JS, calls)]

    assert values == [
        "Stop requested 5 min ago",
        None,
        {
            "waiting": "Waiting for the next agent call, sent 5 min ago: Keep the API.",
            "sendLabel": "Replace note",
        },
        {"waiting": "", "sendLabel": "Send note"},
    ]


def test_a_waiting_stop_keeps_the_button_disabled_and_the_note_shows_in_the_box() -> None:
    stop = function_source(_JS, "renderStopControl")
    assert "stopRequestedText(detail.stop_request, Date.now())" in stop
    assert "setDisabled([button, cancel], true);" in stop
    steer = function_source(_JS, "renderSteerControl")
    assert "steerWaitingView(detail.steer_note, Date.now())" in steer
    assert "controls.send.textContent = view.sendLabel;" in steer
    html = dashboard_assets.render_index_html(token="t")
    assert '<p id="run-steer-waiting" class="steer-waiting" role="status" hidden></p>' in html
    assert ".steer-waiting { white-space: pre-wrap;" in dashboard_assets.STYLE_CSS
