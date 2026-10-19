"""Call prompts: stored only behind ``factory.store_prompts``, served by the dashboard (ADR-070)."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

import pytest
from factory_testing import build_config, work_item

from software_agent_factory import transcripts
from software_agent_factory.agents import FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DashboardConfig, create_server
from software_agent_factory.dashboard.transcript_route import transcript_answer
from software_agent_factory.models import WorkflowState
from software_agent_factory.store import FileRunStore
from software_agent_factory.transcripts import (
    PROMPT_CUT_MARKER,
    call_prompt_reader,
    cap_prompt,
    prompt_name,
)
from software_agent_factory.workflow import WorkflowController

SECRET = "ghp_" + "a" * 36


def _config(data_dir: Path, *, store_prompts: bool) -> FactoryConfig:
    config = build_config(data_dir)
    factory = config.factory.model_copy(update={"store_prompts": store_prompts})
    return config.model_copy(update={"factory": factory})


def _secret_item() -> Any:
    item = work_item()
    return item.model_copy(update={"description": f"{item.description} token {SECRET}"})


def test_the_flag_is_off_by_default_and_then_no_prompt_is_written(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    store = FileRunStore(factory_data_dir)
    config = build_config(factory_data_dir)
    assert config.factory.store_prompts is False

    run = WorkflowController(config, store, FakeAgentRuntime()).run(
        _secret_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    assert list((store.runs_dir / run.id).rglob("*.prompt.md")) == []


def test_with_the_flag_on_each_call_writes_its_prompt_redacted(
    factory_source_repo: Path, factory_data_dir: Path
) -> None:
    store = FileRunStore(factory_data_dir)
    config = _config(factory_data_dir, store_prompts=True)

    run = WorkflowController(config, store, FakeAgentRuntime()).run(
        _secret_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    run_dir = store.runs_dir / run.id
    numbers = [record.invocation_number for record in run.invocation_records]
    assert numbers
    for number in numbers:
        text = (run_dir / prompt_name(number)).read_text("utf-8")
        assert SECRET not in text
        assert "Reject empty customer names" in text
    assert (run_dir / prompt_name(numbers[0])).stat().st_mode & 0o777 == 0o600


def test_a_prompt_write_failure_never_fails_the_run(
    factory_source_repo: Path, factory_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(transcripts, "write_text_atomic", fail)
    store = FileRunStore(factory_data_dir)
    config = _config(factory_data_dir, store_prompts=True)

    run = WorkflowController(config, store, FakeAgentRuntime()).run(
        work_item(), factory_source_repo
    )

    assert run.state is WorkflowState.PR_READY
    assert list((store.runs_dir / run.id).rglob("*.prompt.md")) == []


def test_a_long_prompt_keeps_its_start_and_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcripts, "PROMPT_MAX_CHARS", 20)

    capped = cap_prompt("a" * 30 + SECRET + "b" * 30)

    assert capped == "a" * 10 + PROMPT_CUT_MARKER + "b" * 10
    assert cap_prompt(f"short {SECRET}") == "short [REDACTED]"


# -- route -------------------------------------------------------------------


def _get(runs_dir: Path | None, path: str) -> tuple[int, Any]:
    server = create_server(
        DashboardConfig(
            snapshot_provider=lambda *, limit, offset: {"runs": []},
            run_detail_provider=lambda run_id: None,
            run_prompt_provider=None if runs_dir is None else call_prompt_reader(runs_dir),
        )
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        headers = {"Host": f"{host}:{port}", TOKEN_HEADER: server.token}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"null")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_the_route_serves_a_stored_prompt_redacted_again(tmp_path: Path) -> None:
    path = tmp_path / "run-001" / prompt_name(2)
    path.parent.mkdir(parents=True)
    path.write_text(f"# Role\nkey {SECRET}\n", encoding="utf-8")

    status, payload = _get(tmp_path, "/api/runs/run-001/calls/2/prompt")

    assert status == 200
    assert payload == {
        "run_id": "run-001",
        "call": 2,
        "text": "# Role\nkey [REDACTED]\n",
        "truncated": False,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/run-001/calls/3/prompt",
        "/api/runs/run-404/calls/2/prompt",
        "/api/runs/run-001/calls/02/prompt",
        "/api/runs/run-001/calls/2/prompt/x",
    ],
)
def test_the_route_is_404_when_no_prompt_was_stored(tmp_path: Path, path: str) -> None:
    (tmp_path / "run-001" / "calls").mkdir(parents=True)
    (tmp_path / "run-001" / prompt_name(2)).write_text("x", encoding="utf-8")

    assert _get(tmp_path, path)[0] == 404


def test_the_route_is_404_without_a_provider() -> None:
    assert _get(None, "/api/runs/run-001/calls/2/prompt")[0] == 404


def test_a_failing_provider_is_503_without_details() -> None:
    def broken(run_id: str, number: int) -> str | None:
        raise OSError(SECRET)

    server = create_server(
        DashboardConfig(
            snapshot_provider=lambda *, limit, offset: {"runs": []},
            run_detail_provider=lambda run_id: None,
            run_prompt_provider=broken,
        )
    )
    status, payload = transcript_answer(server, "/api/runs/run-001/calls/2/prompt")
    server.server_close()

    assert (status, payload) == (503, {"error": "prompt unavailable"})
