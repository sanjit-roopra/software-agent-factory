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
subprocess involved. The JSONL protocol exchange itself (line buffering,
response matching, event collection) is delegated to
:class:`software_agent_factory.pi_rpc.PiRpcClient` rather than reimplemented
here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from software_agent_factory.pi_rpc import PiProcessHandle, PiRpcClient
from software_agent_factory.subprocess_utils import build_child_env

#: Bound on how long :meth:`~software_agent_factory.pi_rpc.PiRpcClient.close`
#: waits for a probe child to exit on its own before escalating to killing
#: its process group.
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


ProcessFactory = Callable[[Sequence[str]], PiProcessHandle]


def _default_process_factory(cmd: Sequence[str]) -> PiProcessHandle:
    # pi's github-copilot auth is read from ~/.pi/agent/auth.json, not these
    # env vars, so dropping them cannot break auth -- it only keeps a GitHub
    # credential in the factory's own environment from leaking into the
    # child's environment or transcript.
    env, _ = build_child_env()
    try:
        return subprocess.Popen(
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


class _PiConversation:
    """Sequential JSONL command/response exchange with one pi RPC client."""

    def __init__(self, client: PiRpcClient, deadline: float) -> None:
        self._client = client
        self._deadline = deadline

    def send_prompt_and_wait(self, message: str) -> None:
        self._client.request({"type": "prompt", "message": message}, deadline=self._deadline)
        self._client.wait_for_settled(deadline=self._deadline)

    def get_messages(self) -> list[Mapping[str, Any]]:
        record = self._client.request({"type": "get_messages"}, deadline=self._deadline)
        data = record.get("data") or {}
        return list(data.get("messages", []))


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
    client = PiRpcClient(process)
    try:
        conversation = _PiConversation(client, deadline)
        conversation.send_prompt_and_wait(_FIRST_PROMPT)
        conversation.send_prompt_and_wait(_FIRST_PROMPT + _SECOND_PROMPT_SUFFIX)
        messages = conversation.get_messages()
    finally:
        client.close(timeout=_CLOSE_TIMEOUT_SECONDS)
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
    first_client = PiRpcClient(first_process)
    try:
        _PiConversation(first_client, deadline).send_prompt_and_wait(_FIRST_PROMPT)
    finally:
        first_client.close(timeout=_CLOSE_TIMEOUT_SECONDS)

    second_cmd = _build_command(executable, provider, model, session_args)
    second_process = process_factory(second_cmd)
    second_client = PiRpcClient(second_process)
    try:
        conversation = _PiConversation(second_client, deadline)
        conversation.send_prompt_and_wait(_FIRST_PROMPT + _SECOND_PROMPT_SUFFIX)
        messages = conversation.get_messages()
    finally:
        second_client.close(timeout=_CLOSE_TIMEOUT_SECONDS)
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
