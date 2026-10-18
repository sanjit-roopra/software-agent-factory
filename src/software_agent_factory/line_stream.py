"""Read an agent CLI's stdout line by line while it runs (ADR-052).

Claude Code and Copilot print one JSON event per line. Reading each line as it
arrives, not all of stdout at exit, lets every tool call reach the dashboard's
live tail while the call still runs (:func:`~.transcripts.report_event_line`).
The lines are read on the calling thread, so the workflow's listener, held in a
context variable, sees them. A side thread drains stderr, another writes the
prompt to stdin, and a timer stops the process group at the deadline.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import IO

from .transcripts import report_event_line


@dataclass(frozen=True)
class StreamedOutput:
    """All a finished process printed, and whether the deadline stopped it."""

    stdout: str
    stderr: str
    timed_out: bool


def stream_process(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
    prompt: str | None = None,
    grace_seconds: float = 1.0,
) -> StreamedOutput:
    """Feed ``prompt`` to stdin, report each stdout line, and wait for the process to end.

    At the deadline the process group gets SIGTERM, then SIGKILL after
    ``grace_seconds``. ``stdout`` is every line exactly as printed, so it is the
    same transcript ``communicate()`` would return. Any exception stops the
    process group before it propagates.
    """
    deadline = time.monotonic() + timeout_seconds
    stop = _Stopper(process, grace_seconds)
    stderr_parts: list[str] = []
    helpers = [_start(_drain, process.stderr, stderr_parts.append)]
    if prompt is not None:
        helpers.append(_start(_feed, process.stdin, prompt))
    timer = threading.Timer(timeout_seconds, stop.expire)
    timer.daemon = True
    timer.start()
    lines: list[str] = []
    try:
        for line in process.stdout or ():
            lines.append(line)
            report_event_line(line)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stop.expire()
    except BaseException:
        stop.stop()
        raise
    finally:
        timer.cancel()
        timer.join()
        for helper in helpers:
            helper.join()
    return StreamedOutput("".join(lines), "".join(stderr_parts), stop.timed_out)


class _Stopper:
    """Stops the process group once, from the timer or the reading thread."""

    def __init__(self, process: subprocess.Popen[str], grace_seconds: float) -> None:
        self._process = process
        self._grace_seconds = grace_seconds
        self._lock = threading.Lock()
        self._stopped = False
        self.timed_out = False

    def expire(self) -> None:
        if self._process.returncode is not None:
            return
        self.timed_out = True
        self.stop()

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        # macOS answers EPERM, not ESRCH, when only a zombie is left in the group.
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            self._process.wait(timeout=self._grace_seconds)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGKILL)
            self._process.wait()


def _start(target: Callable[..., None], *args: object) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def _drain(stream: IO[str] | None, keep: Callable[[str], None]) -> None:
    if stream is not None:
        keep(stream.read())


def _feed(stream: IO[str] | None, prompt: str) -> None:
    """Write the prompt and close stdin; a child that exits early may close the pipe first."""
    if stream is None:
        return
    with suppress(BrokenPipeError, OSError):
        stream.write(prompt)
    with suppress(BrokenPipeError, OSError):
        stream.close()
