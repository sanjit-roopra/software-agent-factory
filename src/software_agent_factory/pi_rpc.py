"""JSONL RPC client for the ``pi`` coding agent's ``--mode rpc`` protocol.

Step 3.1 of ``plans/pi-agent-runtime.md``. This module is a pure protocol
client: it imports nothing from ``agents``, ``models`` or ``config``, only
the runtime-neutral :mod:`software_agent_factory.subprocess_utils` helper
used to escalate a hung process on close. Callers (the pi runtime, Step 3.2)
own request/response shaping, tool selection and process construction --
this client only drives an already-started process handle over
stdin/stdout/stderr.
"""

from __future__ import annotations

import itertools
import json
import os
import select
import subprocess
import time
from typing import IO, Any, Protocol

from .subprocess_utils import kill_process_group

#: Bytes retained in the stderr tail exposed via :attr:`PiRpcClient.stderr_tail`
#: -- enough for a useful excerpt in a failure reason without unbounded
#: growth if pi writes a lot to stderr before exiting.
_STDERR_TAIL_BYTES = 4096

_READ_CHUNK_BYTES = 65536

#: Default bound :meth:`PiRpcClient.close` spends waiting for the child to
#: exit on its own after its stdin closes, before escalating to
#: :func:`kill_process_group`.
_DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0

#: Default cap on how large ``_stdout_buffer`` may grow while accumulating a
#: single line with no newline yet. ``get_messages`` responses carry a
#: session's full message history, so this is generous by design -- it is a
#: safety bound against an unbounded read (a protocol break or a runaway
#: response), not a realistic per-line size.
_MAX_LINE_BYTES = 64 * 1024 * 1024


class PiProcessHandle(Protocol):
    """Minimal handle :class:`PiRpcClient` needs over an already-started pi process.

    Matches the surface of a text-mode ``subprocess.Popen`` (``pid``,
    ``stdin``/``stdout``/``stderr``, ``poll``, ``wait``, ``communicate``), so
    a real ``Popen`` and a test double with the same shape are both usable
    without a cast -- including passing the handle straight to
    :func:`software_agent_factory.subprocess_utils.kill_process_group`, which
    expects exactly this ``pid`` + ``communicate`` shape. Constructing the
    real process (``subprocess.Popen(..., stdin=PIPE, stdout=PIPE,
    stderr=PIPE, start_new_session=True)``) is the runtime's job (Step 3.2 of
    ``plans/pi-agent-runtime.md``); this client only consumes the handle.
    """

    @property
    def pid(self) -> int: ...

    @property
    def stdin(self) -> IO[str] | None: ...

    @property
    def stdout(self) -> IO[str] | None: ...

    @property
    def stderr(self) -> IO[str] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]: ...


class PiRpcError(RuntimeError):
    """Base class for errors raised by :class:`PiRpcClient`."""


class PiRpcCommandError(PiRpcError):
    """Raised when pi answers a command with ``"success": false``."""

    def __init__(self, command: dict[str, Any], error: Any) -> None:
        super().__init__(f"pi command {command.get('type')!r} failed: {error!r}")
        self.command = command
        self.error = error


class PiRpcProtocolError(PiRpcError):
    """Raised when a line pi wrote cannot be parsed as a JSON object record."""

    def __init__(self, line_excerpt: str, *, reason: str = "is not a JSON object") -> None:
        super().__init__(f"pi wrote a line that {reason}: {line_excerpt!r}")
        self.line_excerpt = line_excerpt


class PiRpcProcessExited(PiRpcError):
    """Raised when pi's stdout hits EOF before the awaited record arrived."""

    def __init__(self, returncode: int | None, stderr_tail: str) -> None:
        super().__init__(f"pi process exited (code={returncode}): {stderr_tail}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class PiRpcTimeout(PiRpcError):
    """Raised when the deadline passed before the awaited record arrived."""


class PiRpcClient:
    """Drives one ``pi --mode rpc`` process over JSONL stdin/stdout.

    ``process`` must already be started; construction and cwd/argv are the
    runtime's concern (see :class:`PiProcessHandle`).
    """

    def __init__(self, process: PiProcessHandle, *, max_line_bytes: int = _MAX_LINE_BYTES) -> None:
        self._process = process
        self._max_line_bytes = max_line_bytes
        self._next_id = itertools.count(1)
        self._stdout_buffer = b""
        self._stdout_eof = False
        self._stderr_tail = b""
        self._stderr_eof = process.stderr is None
        #: Records seen by :meth:`request` that did not match the response it
        #: was waiting for. Drained by the next :meth:`wait_for_settled` call.
        self._pending_events: list[dict[str, Any]] = []

    @property
    def stderr_tail(self) -> str:
        """Last ~4 KB of pi's stderr read so far, decoded for display in a failure reason."""
        return self._stderr_tail.decode("utf-8", errors="replace")

    def send(self, command: dict[str, Any]) -> str:
        """Write one JSON command line (plus ``"\\n"``), assigning a unique ``id``.

        Returns the assigned id (``"c1"``, ``"c2"``, ...).
        """
        command_id = f"c{next(self._next_id)}"
        payload = dict(command)
        payload["id"] = command_id
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(json.dumps(payload) + "\n")
            self._process.stdin.flush()
        except OSError as exc:
            # pi already exited: report it like the read path does, with the
            # return code and stderr tail, instead of a bare BrokenPipeError.
            raise PiRpcProcessExited(self._returncode(), self.stderr_tail) from exc
        return command_id

    def request(self, command: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        """Send ``command`` and read records until its matching response arrives.

        Records seen while waiting that are not the matching response are
        kept as events -- returned later by :meth:`wait_for_settled`. Raises
        :class:`PiRpcCommandError` when the response's ``"success"`` is
        ``False``.
        """
        command_id = self.send(command)
        sent_command = {**command, "id": command_id}
        while True:
            record = self._read_record(deadline)
            if record.get("type") == "response" and record.get("id") == command_id:
                if record.get("success") is False:
                    raise PiRpcCommandError(sent_command, record.get("error"))
                return record
            self._pending_events.append(record)

    def wait_for_settled(self, *, deadline: float) -> list[dict[str, Any]]:
        """Collect event records until one with ``type == "agent_settled"`` arrives.

        Returns every event collected, including events buffered earlier by
        :meth:`request` while it waited on a different command's response.
        """
        events = self._pending_events
        self._pending_events = []
        for index, event in enumerate(events):
            if event.get("type") == "agent_settled":
                # Records buffered after this settle belong to the next wait.
                self._pending_events = events[index + 1 :]
                return events[: index + 1]
        while True:
            record = self._read_record(deadline)
            events.append(record)
            if record.get("type") == "agent_settled":
                return events

    def close(self, timeout: float = _DEFAULT_CLOSE_TIMEOUT_SECONDS) -> None:
        """Close stdin and reap the process, killing its process group on timeout."""
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(self._process)

    def _read_record(self, deadline: float) -> dict[str, Any]:
        line = self._read_line(deadline)
        stripped = line.strip()
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PiRpcProtocolError(stripped[:200]) from exc
        if not isinstance(record, dict):
            raise PiRpcProtocolError(stripped[:200])
        return record

    def _read_line(self, deadline: float) -> str:
        """Return the next complete stdout line, blocking on the deadline if needed.

        Keeps an internal bytes buffer and only ``select()``s the raw fd when
        the buffer holds no complete line yet. ``select()`` on a buffered
        text stream can miss a line already pulled into that stream's own
        internal buffer by an earlier read; reading the raw fd directly with
        ``os.read`` and tracking our own buffer avoids that.
        """
        assert self._process.stdout is not None
        stdout_fd = self._process.stdout.fileno()
        stderr_fd = self._process.stderr.fileno() if self._process.stderr is not None else None

        while b"\n" not in self._stdout_buffer:
            if self._stdout_eof:
                raise PiRpcProcessExited(self._returncode(), self.stderr_tail)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PiRpcTimeout("timed out waiting for pi output")
            readers = [stdout_fd]
            if stderr_fd is not None and not self._stderr_eof:
                readers.append(stderr_fd)
            ready, _, _ = select.select(readers, [], [], remaining)
            if stderr_fd is not None and stderr_fd in ready:
                chunk = os.read(stderr_fd, _READ_CHUNK_BYTES)
                if chunk:
                    self._stderr_tail = (self._stderr_tail + chunk)[-_STDERR_TAIL_BYTES:]
                else:
                    self._stderr_eof = True
            if stdout_fd in ready:
                chunk = os.read(stdout_fd, _READ_CHUNK_BYTES)
                if chunk:
                    self._stdout_buffer += chunk
                    if (
                        len(self._stdout_buffer) > self._max_line_bytes
                        and b"\n" not in self._stdout_buffer
                    ):
                        raise PiRpcProtocolError(
                            self._stdout_buffer[:200].decode("utf-8", errors="replace"),
                            reason=f"exceeded {self._max_line_bytes} bytes without a newline",
                        )
                else:
                    self._stdout_eof = True

        line, _, self._stdout_buffer = self._stdout_buffer.partition(b"\n")
        try:
            return line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PiRpcProtocolError(
                line[:200].decode("utf-8", errors="replace"), reason="is not valid UTF-8"
            ) from exc

    def _returncode(self) -> int | None:
        code = self._process.poll()
        if code is not None:
            return code
        try:
            return self._process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return None
