"""Runtime-neutral subprocess helpers shared by agent runtimes.

Process-group termination, the GitHub-credential env-var scrub set, and the
credential-hygiene helpers built on it (:data:`TOKEN_PATTERNS`,
:func:`build_child_env`, :func:`sanitize_output`) are used by more than one
``AgentRuntime`` implementation (Copilot, pi from Slice 3 of
``plans/pi-agent-runtime.md``, and the pi cache probe script), so no single
runtime owns them. Tolerant dotted-version parsing is shared by the runtime
prerequisite checks (Step 2.3 of ``plans/pi-agent-runtime.md``).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from typing import Protocol

#: Environment variables scrubbed from a child agent process's environment so
#: a GitHub credential in the factory's own environment cannot leak into a
#: subprocess transcript or be used implicitly by tools the agent runs.
GITHUB_CREDENTIAL_ENV_VARS = frozenset(
    {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GIT_ASKPASS",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_PAT",
        "GITHUB_TOKEN",
    }
)

#: Patterns matching GitHub token literals, used to scrub tokens that reach a
#: child process's output even when they were not sourced from one of
#: :data:`GITHUB_CREDENTIAL_ENV_VARS` (e.g. embedded in a URL or error body).
TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)

_VERSION_PATTERN = re.compile(r"(\d+(?:\.\d+)*)")


class TerminableProcess(Protocol):
    """The minimal process handle :func:`kill_process_group` needs.

    Narrower than ``subprocess.Popen`` so any process wrapper exposing a
    ``pid`` and a ``communicate`` -- real ``Popen``, a test double, or a
    runtime-specific wrapper -- can be passed without an unsafe cast.
    """

    @property
    def pid(self) -> int: ...

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]: ...


def kill_process_group(
    process: TerminableProcess,
    *,
    grace_seconds: float = 1.0,
) -> tuple[str, str]:
    """Terminate a process group, escalating to SIGKILL if it outlives the grace period."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return process.communicate()

    try:
        return process.communicate(timeout=grace_seconds)
    except (subprocess.TimeoutExpired, TimeoutError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def build_child_env() -> tuple[dict[str, str], set[str]]:
    """Copy the current environment with GitHub credentials scrubbed.

    Returns the scrubbed environment mapping alongside the set of credential
    values removed, so callers can also redact those values from process
    output (see :func:`sanitize_output`).
    """
    env = dict(os.environ)
    scrubbed_values: set[str] = set()
    for name in GITHUB_CREDENTIAL_ENV_VARS:
        value = env.pop(name, None)
        if value:
            scrubbed_values.add(value)
    return env, scrubbed_values


def sanitize_output(text: str, scrubbed_values: set[str]) -> str:
    """Redact scrubbed credential values and token-shaped substrings from text.

    Collapses whitespace and truncates to 600 characters, matching the
    excerpt length used in failure-reason and log messages.
    """
    sanitized = text
    for value in sorted(scrubbed_values, key=len, reverse=True):
        if len(value) >= 4:
            sanitized = sanitized.replace(value, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) <= 600:
        return sanitized
    return f"{sanitized[:597].rstrip()}..."


def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse a dotted version string into a tuple comparable with ``<``/``>=``.

    Tolerant of a leading ``"v"``/``"V"`` (``"v22.19.0"``) and of trailing
    non-numeric text after the dotted version (``"22.19.0 (arm64)"``).
    Returns ``None`` when no leading dotted-integer version can be found, so
    callers can distinguish "older than required" from "version could not
    be determined".
    """

    stripped = text.strip()
    if stripped[:1] in ("v", "V"):
        stripped = stripped[1:]
    match = _VERSION_PATTERN.match(stripped)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))
