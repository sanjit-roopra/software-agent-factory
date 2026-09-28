#!/usr/bin/env python3
"""Offline-testable probe for whether a pi provider reports prompt-cache counts.

Drives pi (``@earendil-works/pi-coding-agent``) over its JSONL RPC mode
(``pi --mode rpc ...``) and reports whether the provider's assistant messages
carry prompt-cache token counts (``cacheRead``/``cacheWrite`` on ``usage``).

Two probe modes:

* ``same-process`` -- one pi process, two prompts sharing a prefix, then
  ``get_messages`` to inspect per-message usage.
* ``resume`` -- one pi process answers a prompt into a session file and
  exits; a second process resumes that file with a follow-up prompt sharing
  the first prompt's prefix, then ``get_messages`` on the second process.

The verdict is ``available`` when any assistant message's ``usage`` carries a
``cacheRead`` field (a reported ``0`` still counts as available); it is
``unavailable`` when no message carries the field at all. This mirrors the
Copilot-runtime convention that "unknown" and zero are distinct.

The process transport is injectable (``ProcessFactory``) so tests can drive
the whole two-process orchestration against a fake process, with no real pi
subprocess involved.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from software_agent_factory.subprocess_utils import build_child_env, kill_process_group

#: Bound on how long :meth:`_SubprocessPiProcess.close` waits for the child
#: to exit on its own before escalating to :func:`kill_process_group`.
_CLOSE_TIMEOUT_SECONDS = 10.0

#: Providers only cache prompts above a minimum length (about 1,024 tokens
#: for Anthropic and OpenAI models), so the shared prefix is padded well past
#: that floor. A short prefix would report zero cache reads even when caching
#: works.
_CACHE_PAD = " ".join(f"Probe padding line {index}." for index in range(900))
#: Shared prefix used for both prompts in a probe run. The second prompt
#: extends this prefix so a provider with prompt caching has a matching
#: prefix to serve from cache.
_FIRST_PROMPT = f"{_CACHE_PAD}\n\nIgnore the padding above. Reply with the single word: ack."
_SECOND_PROMPT_SUFFIX = " Then reply with the single word: ack-again."

_BASE_RPC_FLAGS = (
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-context-files",
    "--no-approve",
    "--no-tools",
)


class PiExecutableNotFoundError(RuntimeError):
    """Raised when the configured pi executable cannot be launched."""

    def __init__(self, executable: str) -> None:
        super().__init__(f"pi executable not found: {executable!r}")
        self.executable = executable


class PiProtocolError(RuntimeError):
    """Raised when pi's JSONL stream cannot be parsed as expected."""


@dataclass(frozen=True)
class CacheVerdict:
    """Result of inspecting assistant-message usage for prompt-cache fields."""

    cache: Literal["available", "unavailable"]
    cache_read_tokens: int
    cache_write_tokens: int
    input_tokens: int


def verdict_from_messages(messages: Sequence[Mapping[str, Any]]) -> CacheVerdict:
    """Pure function: derive a :class:`CacheVerdict` from ``get_messages`` data.

    Only messages carrying a ``usage`` mapping are assistant messages with
    reportable usage; other messages are ignored. Availability is driven by
    whether ``cacheRead`` appears at all, not by its value.
    """
    usages = [
        message["usage"]
        for message in messages
        if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping)
    ]
    available = any("cacheRead" in usage for usage in usages)
    cache_read_tokens = sum(int(usage["cacheRead"]) for usage in usages if "cacheRead" in usage)
    cache_write_tokens = sum(int(usage["cacheWrite"]) for usage in usages if "cacheWrite" in usage)
    input_tokens = sum(int(usage["input"]) for usage in usages if "input" in usage)
    return CacheVerdict(
        cache="available" if available else "unavailable",
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        input_tokens=input_tokens,
    )


class PiProcessProtocol(Protocol):
    """Minimal transport a pi RPC process (real or faked) must provide."""

    def send(self, command: Mapping[str, Any]) -> None: ...

    def read_line(self, deadline: float) -> str | None:
        """Return the next stdout line, or ``None`` at EOF.

        ``deadline`` is an absolute ``time.monotonic()`` value; implementations
        must raise :class:`TimeoutError` if no line arrives before it.
        """
        ...

    def close(self) -> None:
        """Close stdin and reap the process, killing it if it will not exit."""
        ...


ProcessFactory = Callable[[Sequence[str]], PiProcessProtocol]


class _SubprocessPiProcess:
    """Wraps a real ``pi --mode rpc`` subprocess as a :class:`PiProcessProtocol`."""

    def __init__(self, popen: subprocess.Popen[str]) -> None:
        self._popen = popen
        #: Raw bytes read from the stdout fd but not yet returned as a line.
        #: ``select()`` reports readiness at the OS pipe level, but the
        #: buffered ``TextIOWrapper`` can pull more than one line into its
        #: own internal buffer on a single read; polling ``select()`` again
        #: for a line already sitting in that buffer would spuriously time
        #: out. Reading the raw fd directly and keeping our own buffer
        #: avoids that: we only block on ``select()`` when our buffer holds
        #: no complete line yet.
        self._buffer = b""

    def send(self, command: Mapping[str, Any]) -> None:
        assert self._popen.stdin is not None
        self._popen.stdin.write(json.dumps(command) + "\n")
        self._popen.stdin.flush()

    def read_line(self, deadline: float) -> str | None:
        assert self._popen.stdout is not None
        fd = self._popen.stdout.fileno()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for pi output")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise TimeoutError("timed out waiting for pi output")
            chunk = os.read(fd, 65536)
            if not chunk:
                if self._buffer:
                    line, self._buffer = self._buffer, b""
                    return line.decode("utf-8")
                return None
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line.decode("utf-8") + "\n"

    def close(self, *, timeout: float = _CLOSE_TIMEOUT_SECONDS) -> None:
        """Close stdin and reap the child, killing its process group if it hangs."""
        if self._popen.stdin is not None and not self._popen.stdin.closed:
            self._popen.stdin.close()
        try:
            self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_group(self._popen)


def _default_process_factory(cmd: Sequence[str]) -> PiProcessProtocol:
    # pi's github-copilot auth is read from ~/.pi/agent/auth.json, not these
    # env vars, so dropping them cannot break auth -- it only keeps a GitHub
    # credential in the factory's own environment from leaking into the
    # child's environment or transcript.
    env, _ = build_child_env()
    try:
        popen = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise PiExecutableNotFoundError(cmd[0]) from exc
    return _SubprocessPiProcess(popen)


class _PiConversation:
    """Sequential JSONL command/response exchange with one pi RPC process."""

    def __init__(self, process: PiProcessProtocol, deadline: float) -> None:
        self._process = process
        self._deadline = deadline
        self._next_id = itertools.count(1)

    def send_prompt_and_wait(self, message: str) -> None:
        command_id = next(self._next_id)
        self._process.send({"id": command_id, "type": "prompt", "message": message})
        while True:
            record = self._read_record()
            if record.get("type") == "agent_settled":
                return

    def get_messages(self) -> list[Mapping[str, Any]]:
        command_id = next(self._next_id)
        self._process.send({"id": command_id, "type": "get_messages"})
        while True:
            record = self._read_record()
            if record.get("type") == "response" and record.get("id") == command_id:
                if not record.get("success", False):
                    raise PiProtocolError(f"get_messages failed: {record.get('error')!r}")
                data = record.get("data") or {}
                messages = data.get("messages", [])
                return list(messages)

    def _read_record(self) -> dict[str, Any]:
        while True:
            line = self._process.read_line(self._deadline)
            if line is None:
                raise PiProtocolError("pi process closed its output before responding")
            stripped = line.strip()
            if stripped:
                break
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise PiProtocolError(f"pi wrote a non-JSON line: {stripped!r}") from exc
        if not isinstance(record, dict):
            raise PiProtocolError(f"pi wrote a JSON line that is not an object: {stripped!r}")
        return record


def _build_command(
    executable: str, provider: str, model: str, session_args: Sequence[str]
) -> list[str]:
    return [
        executable,
        "--mode",
        "rpc",
        "--provider",
        provider,
        "--model",
        model,
        *session_args,
        *_BASE_RPC_FLAGS,
    ]


def run_same_process(
    *,
    executable: str,
    provider: str,
    model: str,
    timeout: float,
    process_factory: ProcessFactory,
) -> CacheVerdict:
    """Send two prefix-sharing prompts to one pi process, then read usage."""
    deadline = time.monotonic() + timeout
    cmd = _build_command(executable, provider, model, ("--no-session",))
    process = process_factory(cmd)
    try:
        conversation = _PiConversation(process, deadline)
        conversation.send_prompt_and_wait(_FIRST_PROMPT)
        conversation.send_prompt_and_wait(_FIRST_PROMPT + _SECOND_PROMPT_SUFFIX)
        messages = conversation.get_messages()
    finally:
        process.close()
    return verdict_from_messages(messages)


def run_resume(
    *,
    executable: str,
    provider: str,
    model: str,
    timeout: float,
    process_factory: ProcessFactory,
    session_path: Path,
) -> CacheVerdict:
    """Answer one prompt, exit, then resume the same session file for a follow-up.

    Reports the verdict from the second process's messages only.
    """
    deadline = time.monotonic() + timeout
    session_args = ("--session", str(session_path))

    first_cmd = _build_command(executable, provider, model, session_args)
    first_process = process_factory(first_cmd)
    try:
        _PiConversation(first_process, deadline).send_prompt_and_wait(_FIRST_PROMPT)
    finally:
        first_process.close()

    second_cmd = _build_command(executable, provider, model, session_args)
    second_process = process_factory(second_cmd)
    try:
        conversation = _PiConversation(second_process, deadline)
        conversation.send_prompt_and_wait(_FIRST_PROMPT + _SECOND_PROMPT_SUFFIX)
        messages = conversation.get_messages()
    finally:
        second_process.close()
    return verdict_from_messages(messages)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe whether a pi provider reports prompt-cache token counts."
    )
    parser.add_argument("--provider", required=True, help="pi provider id, e.g. github-copilot")
    parser.add_argument("--model", required=True, help="model id to probe")
    parser.add_argument(
        "--mode",
        required=True,
        choices=("same-process", "resume"),
        help="same-process: two prompts in one pi process; "
        "resume: a second process resumes the first's session file",
    )
    parser.add_argument("--executable", default="pi", help="pi executable name or path")
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for pi to settle"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.mode == "same-process":
            verdict = run_same_process(
                executable=args.executable,
                provider=args.provider,
                model=args.model,
                timeout=args.timeout,
                process_factory=_default_process_factory,
            )
        else:
            with tempfile.TemporaryDirectory(prefix="pi_cache_probe_") as tmp_dir:
                session_path = Path(tmp_dir) / "session.jsonl"
                verdict = run_resume(
                    executable=args.executable,
                    provider=args.provider,
                    model=args.model,
                    timeout=args.timeout,
                    process_factory=_default_process_factory,
                    session_path=session_path,
                )
    except PiExecutableNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "provider": args.provider,
                "model": args.model,
                "mode": args.mode,
                "cache": verdict.cache,
                "cache_read_tokens": verdict.cache_read_tokens,
                "cache_write_tokens": verdict.cache_write_tokens,
                "input_tokens": verdict.input_tokens,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
