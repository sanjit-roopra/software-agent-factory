"""Tests for :mod:`software_agent_factory.pi_rpc`.

Most tests use ``FakePiProcess`` (``tests/factory_testing.py``), which wraps ``os.pipe()`` pairs so
``PiRpcClient``'s raw-fd ``select``/``os.read`` loop runs against real,
deterministic file descriptors -- the test writes scripted JSONL records
into the read end ``PiRpcClient`` consumes, exactly as a real pi process
would. One test drives a real short-lived Python child instead, to prove
the stderr-draining ``select()`` loop can't deadlock against a full OS pipe
buffer -- a scenario ``FakePiProcess``'s unbounded pipes can't reproduce.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from factory_testing import FakePiClock, FakePiProcess

from software_agent_factory.pi_rpc import (
    _STDERR_REDACTION_MARGIN_CHARS,
    _STDERR_TAIL_CHARS,
    PiRpcClient,
    PiRpcCommandError,
    PiRpcProcessExited,
    PiRpcProtocolError,
    PiRpcTimeout,
    messages_since_last_prompt,
)
from software_agent_factory.subprocess_utils import redact_secrets

_DEADLINE = 5.0


def _deadline(seconds: float = _DEADLINE) -> float:
    return time.monotonic() + seconds


# ---------------------------------------------------------------------------
# send: unique incrementing ids
# ---------------------------------------------------------------------------


def test_send_assigns_incrementing_ids() -> None:
    process = FakePiProcess()
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
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "event", "name": "tool_call_started"},
        {"type": "response", "id": "c1", "success": True, "data": {"ok": True}},
    )

    result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert result == {"type": "response", "id": "c1", "success": True, "data": {"ok": True}}


def test_request_raises_command_error_on_success_false() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_records({"type": "response", "id": "c1", "success": False, "error": "boom"})

    deadline = _deadline()
    with pytest.raises(PiRpcCommandError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert excinfo.value.error == "boom"
    assert excinfo.value.command == {"type": "prompt", "message": "hi", "id": "c1"}


def test_request_raises_protocol_error_on_invalid_json_line() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_raw_stdout("not valid json\n")

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "not valid json" in excinfo.value.line_excerpt


def test_request_raises_protocol_error_on_valid_json_that_is_not_an_object() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_raw_stdout("[1, 2, 3]\n")

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "[1, 2, 3]" in excinfo.value.line_excerpt


def test_request_raises_timeout_when_deadline_passes(pi_fake_clock: FakePiClock) -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    deadline = pi_fake_clock.deadline(0.2)

    with pytest.raises(PiRpcTimeout):
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert pi_fake_clock.now == deadline


def test_request_raises_process_exited_on_eof_with_stderr_tail() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(1)
    process.write_stderr("fatal: credential missing")
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert excinfo.value.returncode == 1
    assert "fatal: credential missing" in excinfo.value.stderr_tail


def test_process_exited_returncode_falls_back_to_wait_when_not_yet_polled() -> None:
    """When ``poll()`` has not yet observed the exit (returns ``None``), the
    returncode reported on EOF comes from reaping via ``wait()`` instead."""
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit_pending_reap(3)
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert excinfo.value.returncode == 3


def test_process_exited_returncode_is_none_when_process_still_running() -> None:
    """EOF on stdout with ``poll()`` reporting ``None`` and ``wait()`` timing
    out (the process is genuinely still running) reports ``returncode`` as
    ``None`` rather than raising or hanging."""
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.close_stdout()  # EOF, but exit()/exit_pending_reap() never called

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert excinfo.value.returncode is None


def test_stderr_tail_keeps_trailing_window_when_it_exceeds_the_cap() -> None:
    """``stderr_tail`` is bounded to the last ~4 K characters (``_STDERR_TAIL_CHARS``)
    -- the trailing window, not the front, since the most recent output is
    what's useful in a failure reason."""
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(1)
    process.write_stderr(("a" * 5000) + "TAIL_MARKER")
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    tail = excinfo.value.stderr_tail
    assert "TAIL_MARKER" in tail
    assert len(tail) <= 4096
    assert "a" * 5000 not in tail


_SECRET = "plainsecretvalue1234"


def _redact_secret(text: str) -> str:
    return text.replace(_SECRET, "[REDACTED]")


def test_stderr_tail_redacts_a_secret_before_the_window_cuts_it() -> None:
    """A secret whose front falls outside the 4 KB window must not survive as
    a suffix: redaction runs on the untruncated stream, then the window cuts."""
    process = FakePiProcess()
    client = PiRpcClient(process, redact=_redact_secret)
    process.exit(1)
    process.write_stderr("A" * 100 + _SECRET + "B" * (4096 - 5))
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    tail = excinfo.value.stderr_tail
    assert len(tail) <= 4096
    assert "e1234" not in tail
    assert tail.endswith("B" * 4000)


def test_stderr_tail_keeps_a_full_window_when_the_raw_window_was_trimmed() -> None:
    """Dropping the margin that guards the front edge must not shorten a tail
    that has nothing redacted."""
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(1)
    process.write_stderr("a" * (_STDERR_TAIL_CHARS + _STDERR_REDACTION_MARGIN_CHARS + 500) + "END")
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    tail = excinfo.value.stderr_tail
    assert len(tail) == _STDERR_TAIL_CHARS
    assert tail.endswith("aEND")


def test_stderr_tail_hides_a_long_secret_cut_by_the_raw_window_when_redaction_shrinks_it() -> None:
    """A secret longer than 256 chars whose front the raw window cuts is not
    redactable whole; the redacted tokens after it shrink the text and would
    slide the leftover fragment into the exposed tail."""
    secret = "sk-" + "".join(f"{i:04d}" for i in range(250))
    window = _STDERR_TAIL_CHARS + _STDERR_REDACTION_MARGIN_CHARS
    token = "ghp_" + "A" * 36
    kept_of_secret = 600
    after = " ".join([token] * ((window - (len(secret) - kept_of_secret)) // (len(token) + 1)))
    process = FakePiProcess()
    client = PiRpcClient(process, redact=lambda text: redact_secrets(text, {secret}))
    process.exit(1)
    process.write_stderr("P" * 100 + secret + " " + after)
    process.close_stdout()

    deadline = _deadline()
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    tail = excinfo.value.stderr_tail
    assert not any(char.isdigit() for char in tail)
    assert "ghp_" not in tail


def test_stderr_tail_redacts_a_secret_split_across_reads(pi_fake_clock: FakePiClock) -> None:
    process = FakePiProcess()
    client = PiRpcClient(process, redact=_redact_secret)
    process.exit(1)
    process.write_stderr("auth failed for " + _SECRET[:8])
    deadline = pi_fake_clock.deadline(0.05)
    with pytest.raises(PiRpcTimeout):
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)
    process.write_stderr(_SECRET[8:] + "\n")
    process.close_stdout()

    deadline = pi_fake_clock.deadline(5.0)
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert _SECRET not in excinfo.value.stderr_tail
    assert "auth failed for [REDACTED]" in excinfo.value.stderr_tail


def test_stderr_tail_redacts_a_token_shaped_secret_split_across_reads(
    pi_fake_clock: FakePiClock,
) -> None:
    """A GitHub token's first half already matches ``TOKEN_PATTERNS`` (``\\b`` fires
    at the end of the text), so redacting per read would leak the second half."""
    token = "ghp_" + "A" * 8 + "SECONDHALF12345"
    process = FakePiProcess()
    client = PiRpcClient(process, redact=lambda text: redact_secrets(text, set()))
    process.exit(1)
    process.write_stderr("auth failed for " + token[:12])
    deadline = pi_fake_clock.deadline(0.05)
    with pytest.raises(PiRpcTimeout):
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)
    process.write_stderr(token[12:] + "\n")
    process.close_stdout()

    deadline = pi_fake_clock.deadline(5.0)
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "SECONDHALF12345" not in excinfo.value.stderr_tail
    assert "auth failed for [REDACTED]" in excinfo.value.stderr_tail


def test_stderr_tail_keeps_multibyte_characters_split_across_reads(
    pi_fake_clock: FakePiClock,
) -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(1)
    encoded = "caf\u00e9".encode()
    process.write_stderr_bytes(encoded[:4])
    deadline = pi_fake_clock.deadline(0.05)
    with pytest.raises(PiRpcTimeout):
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)
    process.write_stderr_bytes(encoded[4:])
    process.close_stdout()

    deadline = pi_fake_clock.deadline(5.0)
    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert excinfo.value.stderr_tail == "caf\u00e9"


def test_protocol_error_excerpt_redacts_a_secret_before_truncating_to_200_chars() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process, redact=_redact_secret)
    process.write_raw_stdout("x" * 195 + _SECRET + " not json\n")

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert len(excinfo.value.line_excerpt) <= 200
    assert "plain" not in excinfo.value.line_excerpt


def test_oversized_line_excerpt_redacts_a_secret_before_truncating_to_200_chars() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process, max_line_bytes=300, redact=_redact_secret)
    process.write_raw_stdout("x" * 195 + _SECRET + "y" * 200)

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "plain" not in excinfo.value.line_excerpt


def test_invalid_utf8_line_excerpt_redacts_a_secret_before_truncating() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process, redact=_redact_secret)
    process.write_stdout_bytes(b"x" * 195 + _SECRET.encode() + b"\xff\n")

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "plain" not in excinfo.value.line_excerpt


def test_request_succeeds_when_stderr_handle_is_none() -> None:
    """A process constructed like a real ``Popen(stderr=subprocess.DEVNULL)``
    handle (``.stderr is None``) must not be selected on -- ``PiRpcClient``
    reads stdout alone rather than raising on the missing fd."""
    process = FakePiProcess(stderr=False)
    client = PiRpcClient(process)
    assert process.stderr is None

    process.write_records({"type": "response", "id": "c1", "success": True})

    result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    assert result["id"] == "c1"


def test_read_line_raises_protocol_error_when_line_exceeds_max_line_bytes() -> None:
    """A line that keeps growing without ever completing with a newline must
    fail fast once it exceeds the configured byte cap, naming the limit,
    rather than letting ``_stdout_buffer`` grow without bound."""
    process = FakePiProcess()
    client = PiRpcClient(process, max_line_bytes=16)
    process.write_raw_stdout("x" * 17)  # no newline: buffer grows past the 16-byte cap

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "exceeded 16 bytes without a newline" in str(excinfo.value)
    assert excinfo.value.line_excerpt.startswith("x")


def test_two_lines_in_one_write_are_both_read_without_timeout() -> None:
    """Both lines land in one flush, then the write end closes -- no further
    data ever arrives. A correct implementation reads both already-buffered
    lines without a second ``select()`` wait; one that relies on a buffered
    text stream's own readline buffering could otherwise miss the second
    line and time out waiting for data that already arrived."""
    process = FakePiProcess()
    client = PiRpcClient(process)

    # Ids line up with what the two request() calls below will generate
    # (a fresh client's first send is "c1", second is "c2").
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "response", "id": "c2", "success": True},
    )
    process.close_stdout()

    first = client.request({"type": "prompt"}, deadline=_deadline(1.0))
    second = client.request({"type": "prompt"}, deadline=_deadline(1.0))

    assert first["id"] == "c1"
    assert second["id"] == "c2"


# ---------------------------------------------------------------------------
# wait_for_settled: collects events, including ones buffered by request()
# ---------------------------------------------------------------------------


def test_wait_for_settled_collects_events_until_agent_settled() -> None:
    process = FakePiProcess()
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
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "agent_settled"},
        {"type": "response", "id": "c1", "success": True},
    )
    client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    events = client.wait_for_settled(deadline=_deadline())

    assert events == [{"type": "agent_settled"}]


def test_wait_for_settled_keeps_records_after_the_settle_for_the_next_wait() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    process.write_records(
        {"type": "agent_settled"},
        {"type": "event", "name": "next_turn_started"},
        {"type": "agent_settled"},
        {"type": "response", "id": "c1", "success": True},
    )
    client.request({"type": "prompt", "message": "hi"}, deadline=_deadline())

    first = client.wait_for_settled(deadline=_deadline())
    second = client.wait_for_settled(deadline=_deadline(0.5))

    assert first == [{"type": "agent_settled"}]
    assert second == [{"type": "event", "name": "next_turn_started"}, {"type": "agent_settled"}]


def test_request_raises_protocol_error_on_invalid_utf8_line() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)

    os.write(process._stdout_write.fileno(), b"\xff\xfe not utf-8\n")

    deadline = _deadline()
    with pytest.raises(PiRpcProtocolError) as excinfo:
        client.request({"type": "prompt", "message": "hi"}, deadline=deadline)

    assert "not valid UTF-8" in str(excinfo.value)


def test_send_raises_process_exited_when_pi_stdin_is_gone() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(3)
    process._stdin_read.close()

    with pytest.raises(PiRpcProcessExited) as excinfo:
        client.send({"type": "prompt", "message": "hi"})

    assert excinfo.value.returncode == 3


# ---------------------------------------------------------------------------
# close: stdin closed, process reaped, kill on timeout
# ---------------------------------------------------------------------------


def test_close_closes_stdin_and_waits_for_exit() -> None:
    process = FakePiProcess()
    client = PiRpcClient(process)
    process.exit(0)

    client.close(timeout=1.0)

    assert process.stdin is not None
    assert process.stdin.closed


def test_close_kills_process_group_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakePiProcess()
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

    assert process.stdin is not None
    assert process.stdin.closed
    assert killed == [(process.pid, signal.SIGTERM)]


def test_close_survives_undecodable_output_left_by_a_killed_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kill_process_group`` reads the dying process's leftover output; a
    text-mode ``Popen`` raises ``UnicodeDecodeError`` on a split UTF-8
    character there. Cleanup must not raise."""
    process = FakePiProcess()
    process.communicate_error = UnicodeDecodeError("utf-8", b"\xe2\x82", 0, 2, "unexpected end")
    client = PiRpcClient(process)
    monkeypatch.setattr("software_agent_factory.subprocess_utils.os.killpg", lambda pid, sig: None)

    client.close(timeout=0.05)

    assert process.stdin is not None
    assert process.stdin.closed


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
        result = client.request({"type": "prompt", "message": "hi"}, deadline=_deadline(5.0))
    finally:
        client.close(timeout=1.0)

    assert result["success"] is True


# ---------------------------------------------------------------------------
# messages_since_last_prompt: what one call added to a resumed session
# ---------------------------------------------------------------------------

_PROMPT = "round 2 prompt"


def _message(role: str, label: str, *, content: object = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "label": label}
    if content is not None:
        message["content"] = content
    return message


def _user(prompt: str) -> dict[str, Any]:
    return _message("user", prompt, content=prompt)


def _labels(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    return [message["label"] for message in messages]


def test_messages_since_last_prompt_drops_the_rounds_before_the_calls_own_prompt() -> None:
    session = [
        _user("round 1 prompt"),
        _message("assistant", "round 1 tool call"),
        _message("toolResult", "round 1 tool output"),
        _message("assistant", "round 1 answer"),
        _user(_PROMPT),
        _message("assistant", "round 2 tool call"),
        _message("toolResult", "round 2 tool output"),
        _message("assistant", "round 2 answer"),
    ]

    result = messages_since_last_prompt(session, _PROMPT)

    assert _labels(result) == ["round 2 tool call", "round 2 tool output", "round 2 answer"]


def test_messages_since_last_prompt_is_empty_when_the_prompt_is_not_in_the_session() -> None:
    """pi timed out before it recorded this call's prompt: the assistant messages are older."""
    session = [_user("round 1 prompt"), _message("assistant", "round 1 answer")]

    assert messages_since_last_prompt(session, _PROMPT) == []


def test_messages_since_last_prompt_matches_a_prompt_held_in_text_content_blocks() -> None:
    blocks = [{"type": "text", "text": "round "}, {"type": "text", "text": "2 prompt"}]
    session = [
        _user("round 1 prompt"),
        _message("assistant", "round 1 answer"),
        _message("user", "round 2", content=blocks),
        _message("assistant", "round 2 answer"),
    ]

    assert _labels(messages_since_last_prompt(session, _PROMPT)) == ["round 2 answer"]


def test_messages_since_last_prompt_starts_after_the_last_copy_of_the_prompt() -> None:
    session = [
        _user(_PROMPT),
        _message("assistant", "earlier answer"),
        _user(_PROMPT),
        _message("assistant", "answer"),
    ]

    assert _labels(messages_since_last_prompt(session, _PROMPT)) == ["answer"]


def test_messages_since_last_prompt_keeps_a_list_that_has_no_user_message() -> None:
    """No round can precede the prompt in such a list, so nothing is dropped."""
    session = [_message("assistant", "answer"), _message("toolResult", "output")]

    assert messages_since_last_prompt(session, _PROMPT) == session


def test_messages_since_last_prompt_is_empty_for_no_messages() -> None:
    assert messages_since_last_prompt([], _PROMPT) == []


def test_messages_since_last_prompt_is_empty_when_the_prompt_got_no_answer() -> None:
    session = [_message("assistant", "answer"), _user(_PROMPT)]

    assert messages_since_last_prompt(session, _PROMPT) == []
