"""Tests for the offline-testable pi prompt-cache probe.

No real ``pi`` subprocess is spawned: the process transport is faked via
``FakePiProcess`` (``tests/factory_testing.py``), a ``PiProcessHandle``-shaped
double backed by real ``os.pipe()`` fds, since the probe drives its process
through :class:`software_agent_factory.pi_rpc.PiRpcClient`. The
missing-executable scenario relies on ``subprocess.Popen`` raising
``FileNotFoundError`` for a nonexistent path without ever starting a process.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from factory_testing import FakePiProcess

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, relative_path: str) -> ModuleType:
    script_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_script_module("pi_cache_probe", "scripts/performance/pi_cache_probe.py")


def _settled_response(command_id: str) -> list[dict[str, Any]]:
    return [
        {"id": command_id, "type": "response", "success": True},
        {"type": "agent_settled"},
    ]


def _get_messages_response(command_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": command_id,
        "type": "response",
        "success": True,
        "data": {"messages": messages},
    }


def _fake_process(records: Sequence[Mapping[str, Any]]) -> FakePiProcess:
    """A process with no stderr pipe (as the probe's own factory builds one)
    that has already written ``records`` and closed its stdout."""
    process = FakePiProcess(stderr=False)
    process.write_records(*records)
    process.close_stdout()
    process.exit(0)
    return process


# double-waiver: B1 — out-of-process pi subprocess handle
class RecordingFactory:
    """Process factory that records every command it was called with."""

    def __init__(self, processes: Sequence[FakePiProcess]) -> None:
        self._processes = list(processes)
        self.commands: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> FakePiProcess:
        self.commands.append(list(cmd))
        return self._processes.pop(0)


# ---------------------------------------------------------------------------
# verdict_from_messages: pure stats-to-verdict function
# ---------------------------------------------------------------------------


def test_verdict_available_when_cache_read_present_and_positive() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "usage": {"input": 100, "output": 10, "cacheRead": 80, "cacheWrite": 5},
        },
    ]

    verdict = probe.verdict_from_messages(messages)

    assert verdict.cache == "available"
    assert verdict.cache_read_tokens == 80
    assert verdict.cache_write_tokens == 5
    assert verdict.input_tokens == 100


def test_verdict_unavailable_when_no_message_carries_cache_fields() -> None:
    messages = [
        {"role": "assistant", "usage": {"input": 50, "output": 5}},
    ]

    verdict = probe.verdict_from_messages(messages)

    assert verdict.cache == "unavailable"
    assert verdict.cache_read_tokens == 0
    assert verdict.cache_write_tokens == 0


def test_verdict_available_with_zero_cache_reads_is_not_unavailable() -> None:
    messages = [
        {"role": "assistant", "usage": {"input": 50, "output": 5, "cacheRead": 0}},
    ]

    verdict = probe.verdict_from_messages(messages)

    assert verdict.cache == "available"
    assert verdict.cache_read_tokens == 0


def test_verdict_unavailable_with_no_assistant_messages() -> None:
    verdict = probe.verdict_from_messages([])

    assert verdict.cache == "unavailable"
    assert verdict.cache_read_tokens == 0
    assert verdict.cache_write_tokens == 0
    assert verdict.input_tokens == 0


def test_verdict_sums_across_multiple_assistant_messages() -> None:
    messages = [
        {"role": "assistant", "usage": {"input": 100, "cacheRead": 20}},
        {"role": "assistant", "usage": {"input": 50, "cacheRead": 30}},
    ]

    verdict = probe.verdict_from_messages(messages)

    assert verdict.cache == "available"
    assert verdict.cache_read_tokens == 50
    assert verdict.input_tokens == 150


# ---------------------------------------------------------------------------
# run_same_process: single-process, two-prompt orchestration
# ---------------------------------------------------------------------------


def test_run_same_process_reports_verdict_from_get_messages() -> None:
    records = [
        *_settled_response("c1"),
        *_settled_response("c2"),
        _get_messages_response(
            "c3",
            [{"role": "assistant", "usage": {"input": 10, "cacheRead": 7}}],
        ),
    ]
    process = _fake_process(records)
    factory = RecordingFactory([process])

    verdict = probe.run_same_process(
        executable="pi",
        provider="github-copilot",
        model="claude-sonnet-5",
        timeout=5.0,
        process_factory=factory,
    )

    assert verdict.cache == "available"
    assert verdict.cache_read_tokens == 7
    assert process.stdin is not None
    assert process.stdin.closed
    assert len(factory.commands) == 1
    cmd = factory.commands[0]
    assert cmd[0] == "pi"
    assert "--no-session" in cmd
    assert "--provider" in cmd
    assert "github-copilot" in cmd
    assert "--model" in cmd
    assert "claude-sonnet-5" in cmd
    # Two prompt commands sent, second sharing the first's prefix.
    prompt_commands = [c for c in process.sent_commands() if c.get("type") == "prompt"]
    assert len(prompt_commands) == 2
    assert prompt_commands[1]["message"].startswith(prompt_commands[0]["message"])


# ---------------------------------------------------------------------------
# run_resume: two-process orchestration over one session file
# ---------------------------------------------------------------------------


def test_run_resume_second_process_uses_same_session_file(tmp_path: Path) -> None:
    session_path = tmp_path / "session.jsonl"
    first_process = _fake_process(_settled_response("c1"))
    second_process = _fake_process(
        [
            *_settled_response("c1"),
            _get_messages_response(
                "c2",
                [{"role": "assistant", "usage": {"input": 5, "cacheRead": 4}}],
            ),
        ]
    )
    factory = RecordingFactory([first_process, second_process])

    verdict = probe.run_resume(
        executable="pi",
        provider="github-copilot",
        model="gpt-5",
        timeout=5.0,
        process_factory=factory,
        session_path=session_path,
    )

    assert verdict.cache == "available"
    assert verdict.cache_read_tokens == 4
    assert first_process.stdin is not None
    assert first_process.stdin.closed
    assert second_process.stdin is not None
    assert second_process.stdin.closed

    assert len(factory.commands) == 2
    first_cmd, second_cmd = factory.commands
    assert "--session" in first_cmd
    assert "--session" in second_cmd
    first_session_arg = first_cmd[first_cmd.index("--session") + 1]
    second_session_arg = second_cmd[second_cmd.index("--session") + 1]
    assert first_session_arg == second_session_arg == str(session_path)

    # Only the second process was asked for messages; verdict reflects it alone.
    assert not any(c.get("type") == "get_messages" for c in first_process.sent_commands())
    assert any(c.get("type") == "get_messages" for c in second_process.sent_commands())


def test_run_resume_counts_only_the_second_prompts_messages(tmp_path: Path) -> None:
    """The resumed session's ``get_messages`` also holds the first prompt's round."""
    first_process = _fake_process(_settled_response("c1"))
    second_process = _fake_process(
        [
            *_settled_response("c1"),
            _get_messages_response(
                "c2",
                [
                    {"role": "user", "content": "first prompt"},
                    {
                        "role": "assistant",
                        "usage": {"input": 900, "cacheRead": 0, "cacheWrite": 800},
                    },
                    {"role": "user", "content": "second prompt"},
                    {"role": "assistant", "usage": {"input": 5, "cacheRead": 4, "cacheWrite": 1}},
                ],
            ),
        ]
    )

    verdict = probe.run_resume(
        executable="pi",
        provider="github-copilot",
        model="gpt-5",
        timeout=5.0,
        process_factory=RecordingFactory([first_process, second_process]),
        session_path=tmp_path / "session.jsonl",
    )

    assert (verdict.input_tokens, verdict.cache_read_tokens, verdict.cache_write_tokens) == (
        5,
        4,
        1,
    )


def test_run_resume_reports_unavailable_when_second_process_lacks_cache_fields(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session.jsonl"
    first_process = _fake_process(_settled_response("c1"))
    second_process = _fake_process(
        [
            *_settled_response("c1"),
            _get_messages_response("c2", [{"role": "assistant", "usage": {"input": 5}}]),
        ]
    )
    factory = RecordingFactory([first_process, second_process])

    verdict = probe.run_resume(
        executable="pi",
        provider="github-copilot",
        model="gpt-5",
        timeout=5.0,
        process_factory=factory,
        session_path=session_path,
    )

    assert verdict.cache == "unavailable"


# ---------------------------------------------------------------------------
# Missing executable
# ---------------------------------------------------------------------------


def test_main_exits_non_zero_naming_missing_executable(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = probe.main(
        [
            "--provider",
            "github-copilot",
            "--model",
            "claude-sonnet-5",
            "--mode",
            "same-process",
            "--executable",
            "definitely-not-a-real-pi-executable",
        ]
    )

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "definitely-not-a-real-pi-executable" in captured.err


def test_first_prompt_exceeds_provider_cache_minimum() -> None:
    # Providers cache only prefixes of about 1,024 tokens or more; roughly four
    # characters per token means the prompt must be well above 4,096 characters.
    assert len(probe._FIRST_PROMPT) > 8_000
