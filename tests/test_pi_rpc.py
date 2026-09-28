"""Tests for :mod:`software_agent_factory.pi_rpc`.

Most tests use ``FakeProcess``, which wraps ``os.pipe()`` pairs so
``PiRpcClient``'s raw-fd ``select``/``os.read`` loop runs against real,
deterministic file descriptors -- the test writes scripted JSONL records
into the read end ``PiRpcClient`` consumes, exactly as a real pi process
would. One test drives a real short-lived Python child instead, to prove
the stderr-draining ``select()`` loop can't deadlock against a full OS pipe
buffer -- a scenario ``FakeProcess``'s unbounded pipes can't reproduce.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import IO, Any

import pytest

from software_agent_factory.pi_rpc import (
    PiRpcClient,
    PiRpcCommandError,
    PiRpcProcessExited,
    PiRpcProtocolError,
    PiRpcTimeout,
)

_DEADLINE = 5.0


# double-waiver: B1 — out-of-process pi subprocess handle
class FakeProcess:
    """``PiProcessHandle``-shaped double backed by real ``os.pipe()`` fds.

    ``stdin``/``stdout``/``stderr`` are the ends ``PiRpcClient`` reads from
    and writes to; the ``_*_write``/``_*_read`` counterparts are the test's
    handle on the other end, used to script pi's output and to close stdout
    to simulate the process exiting.
    """

    def __init__(self, *, stderr: bool = True) -> None:
        stdin_read_fd, stdin_write_fd = os.pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()

        self.stdin: IO[str] | None = os.fdopen(stdin_write_fd, "w")
        self._stdin_read = os.fdopen(stdin_read_fd, "r")
        self.stdout: IO[str] | None = os.fdopen(stdout_read_fd, "r")
        self._stdout_write = os.fdopen(stdout_write_fd, "w")

        if stderr:
            stderr_read_fd, stderr_write_fd = os.pipe()
            self.stderr: IO[str] | None = os.fdopen(stderr_read_fd, "r")
            self._stderr_write: IO[str] | None = os.fdopen(stderr_write_fd, "w")
        else:
            # Mirrors a real ``Popen(stderr=subprocess.DEVNULL)`` handle, as
            # used by ``scripts/performance/pi_cache_probe.py``'s process
            # factory: ``.stderr`` is ``None`` rather than an open pipe.
            self.stderr = None
            self._stderr_write = None

        self.pid = 999_999
        self._returncode: int | None = None
        self._wait_returncode: int | None = None

    def write_records(self, *records: dict[str, Any]) -> None:
        for record in records:
            self._stdout_write.write(json.dumps(record) + "\n")
        self._stdout_write.flush()

    def write_raw_stdout(self, text: str) -> None:
        self._stdout_write.write(text)
        self._stdout_write.flush()

    def write_stderr(self, text: str) -> None:
        assert self._stderr_write is not None
        self._stderr_write.write(text)
        self._stderr_write.flush()

    def close_stdout(self) -> None:
        self._stdout_write.close()

    def exit(self, returncode: int) -> None:
        self._returncode = returncode
        self._wait_returncode = returncode

    def exit_pending_reap(self, returncode: int) -> None:
        """Simulate a process that has exited but not yet been reaped by ``poll()``.

        ``poll()`` still reports ``None`` (not yet observed), while ``wait()``
        successfully reaps it and returns ``returncode`` -- exercising
        ``_returncode``'s fallback from ``poll()`` to ``wait()``.
        """
        self._wait_returncode = returncode

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake-pi", timeout=timeout or 0)
        return self._wait_returncode

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
        return ("", "")

    def sent_commands(self) -> list[dict[str, Any]]:
        """Read back what ``PiRpcClient`` wrote to stdin so far (non-blocking)."""
        os.set_blocking(self._stdin_read.fileno(), False)
        commands: list[dict[str, Any]] = []
        try:
            for line in self._stdin_read:
                stripped = line.strip()
                if stripped:
                    commands.append(json.loads(stripped))
        except BlockingIOError:
            pass
        return commands


def _deadline(seconds: float = _DEADLINE) -> float:
    return time.monotonic() + seconds


# ---------------------------------------------------------------------------
# send: unique incrementing ids
# ---------------------------------------------------------------------------


def test_send_assigns_incrementing_ids() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    first_id = client.send({"type": "prompt", "message": "hi"})
    second_id = client.send({"type": "get_messages"})

    assert first_id == "c1"
    assert second_id == "c2"
    sent = process.sent_commands()
    assert sent == [
        {"type": "prompt", "message": "hi", "id": "c1"},
        {"type": "get_messages", "id": "c2"},
    ]


# ---------------------------------------------------------------------------
# request: response matching by id, interleaved events, command errors
# ---------------------------------------------------------------------------


def test_request_matches_response_by_id_and_buffers_interleaved_events() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "event", "name": "tool_call_started"},
        {"type": "response", "id": "c1", "success": True, "data": {"ok": True}},
    )

    result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert result == {"type": "response", "id": "c1", "success": True, "data": {"ok": True}}


def test_request_raises_command_error_on_success_false() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_records({"type": "response", "id": "c1", "success": False, "error": "boom"})

    with pytest.raises(PiRpcCommandError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert excinfo.value.error == "boom"
    assert excinfo.value.command == {"type": "prompt", "message": "hi", "id": "c1"}


def test_request_raises_protocol_error_on_invalid_json_line() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_raw_stdout("not valid json\n")

    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert "not valid json" in excinfo.value.line_excerpt


def test_request_raises_protocol_error_on_valid_json_that_is_not_an_object() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_raw_stdout("[1, 2, 3]\n")

    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert "[1, 2, 3]" in excinfo.value.line_excerpt


def test_request_raises_timeout_when_deadline_passes() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    started = time.monotonic()
    with pytest.raises(PiRpcTimeout):
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline(0.2))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0


def test_request_raises_process_exited_on_eof_with_stderr_tail() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)
    process.exit(1)
    process.write_stderr("fatal: credential missing")
    process.close_stdout()

    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert excinfo.value.returncode == 1
    assert "fatal: credential missing" in excinfo.value.stderr_tail


def test_process_exited_returncode_falls_back_to_wait_when_not_yet_polled() -> None:
    """When ``poll()`` has not yet observed the exit (returns ``None``), the
    returncode reported on EOF comes from reaping via ``wait()`` instead."""
    process = FakeProcess()
    client = PiRpcClient(process)
    process.exit_pending_reap(3)
    process.close_stdout()

    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert excinfo.value.returncode == 3


def test_process_exited_returncode_is_none_when_process_still_running() -> None:
    """EOF on stdout with ``poll()`` reporting ``None`` and ``wait()`` timing
    out (the process is genuinely still running) reports ``returncode`` as
    ``None`` rather than raising or hanging."""
    process = FakeProcess()
    client = PiRpcClient(process)
    process.close_stdout()  # EOF, but exit()/exit_pending_reap() never called

    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert excinfo.value.returncode is None


def test_stderr_tail_keeps_trailing_window_when_it_exceeds_the_cap() -> None:
    """``stderr_tail`` is bounded to the last ~4 KB (``_STDERR_TAIL_BYTES``)
    -- the trailing window, not the front, since the most recent output is
    what's useful in a failure reason."""
    process = FakeProcess()
    client = PiRpcClient(process)
    process.exit(1)
    process.write_stderr(("a" * 5000) + "TAIL_MARKER")
    process.close_stdout()

    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    tail = excinfo.value.stderr_tail
    assert "TAIL_MARKER" in tail
    assert len(tail.encode("utf-8")) <= 4096
    assert "a" * 5000 not in tail


def test_request_succeeds_when_stderr_handle_is_none() -> None:
    """A process constructed like a real ``Popen(stderr=subprocess.DEVNULL)``
    handle (``.stderr is None``) must not be selected on -- ``PiRpcClient``
    reads stdout alone rather than raising on the missing fd."""
    process = FakeProcess(stderr=False)
    client = PiRpcClient(process)
    assert process.stderr is None

    process.write_records({"type": "response", "id": "c1", "success": True})

    result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert result["id"] == "c1"


def test_read_line_raises_protocol_error_when_line_exceeds_max_line_bytes() -> None:
    """A line that keeps growing without ever completing with a newline must
    fail fast once it exceeds the configured byte cap, naming the limit,
    rather than letting ``_stdout_buffer`` grow without bound."""
    process = FakeProcess()
    client = PiRpcClient(process, max_line_bytes=16)
    process.write_raw_stdout("x" * 17)  # no newline: buffer grows past the 16-byte cap

    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert "16" in str(excinfo.value)


def test_two_lines_in_one_write_are_both_read_without_timeout() -> None:
    """Both lines land in one flush, then the write end closes -- no further
    data ever arrives. A correct implementation reads both already-buffered
    lines without a second ``select()`` wait; one that relies on a buffered
    text stream's own readline buffering could otherwise miss the second
    line and time out waiting for data that already arrived."""
    process = FakeProcess()
    client = PiRpcClient(process)

    # Ids line up with what the two request() calls below will generate
    # (a fresh client's first send is "c1", second is "c2").
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "response", "id": "c2", "success": True},
    )
    process.close_stdout()

    started = time.monotonic()
    first = client.request({"type": "prompt"}, deadline=_deadline(1.0))
    second = client.request({"type": "prompt"}, deadline=_deadline(1.0))
    elapsed = time.monotonic() - started

    assert first["id"] == "c1"
    assert second["id"] == "c2"
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# wait_for_settled: collects events, including ones buffered by request()
# ---------------------------------------------------------------------------


def test_wait_for_settled_collects_events_until_agent_settled() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "event", "name": "tool_call_started"},
        {"type": "response", "id": "c1", "success": True},
    )
    client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    process.write_records(
        {"type": "event", "name": "tool_call_finished"},
        {"type": "agent_settled"},
    )
    events = client.wait_for_settled(deadline=_deadline())

    assert events == [
        {"type": "event", "name": "tool_call_started"},
        {"type": "event", "name": "tool_call_finished"},
        {"type": "agent_settled"},
    ]


def test_wait_for_settled_returns_immediately_if_already_buffered() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "agent_settled"},
        {"type": "response", "id": "c1", "success": True},
    )
    client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    events = client.wait_for_settled(deadline=_deadline())

    assert events == [{"type": "agent_settled"}]


# ---------------------------------------------------------------------------
# close: stdin closed, process reaped, kill on timeout
# ---------------------------------------------------------------------------


def test_close_closes_stdin_and_waits_for_exit() -> None:
    process = FakeProcess()
    client = PiRpcClient(process)
    process.exit(0)

    client.close(timeout=1.0)

    assert process.stdin is not None and process.stdin.closed


def test_close_kills_process_group_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    client = PiRpcClient(process)
    # process.exit() never called: wait() always raises TimeoutExpired, so
    # close() must escalate to kill_process_group rather than hang or raise.
    # os.killpg is faked (matching tests/test_subprocess_utils.py convention)
    # rather than sent against the fake pid, which is not a real process group.
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    client.close(timeout=0.05)

    assert process.stdin is not None and process.stdin.closed
    assert killed == [(process.pid, signal.SIGTERM)]


# ---------------------------------------------------------------------------
# stderr draining: a full stderr pipe must never block a stdout-only wait
# ---------------------------------------------------------------------------


def test_read_line_drains_stderr_while_waiting_for_stdout_avoiding_deadlock() -> None:
    """A real child that writes well past the OS pipe buffer to stderr
    *before* its single stdout response line must not deadlock ``request()``.

    If the ``select()`` loop only drained stderr because it was jointly
    ready with stdout (rather than including the stderr fd on every
    iteration), the child's ``stderr.write`` would block once the pipe
    buffer fills, it would never reach the stdout write, and ``request()``
    would hang until the deadline instead of returning promptly.
    """
    script = (
        "import json, sys\n"
        "command = json.loads(sys.stdin.readline())\n"
        "sys.stderr.write('e' * 200_000)\n"  # far past any OS pipe buffer size
        "sys.stderr.flush()\n"
        "sys.stdout.write(json.dumps({'type': 'response', 'id': command['id'], "
        "'success': True}) + chr(10))\n"
        "sys.stdout.flush()\n"
    )
    popen = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    client = PiRpcClient(popen)
    try:
        started = time.monotonic()
        result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline(5.0))
        elapsed = time.monotonic() - started
    finally:
        client.close(timeout=1.0)

    assert result["success"] is True
    assert elapsed < 5.0
