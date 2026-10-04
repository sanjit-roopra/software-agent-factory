"""Runtime-neutral subprocess helpers shared by agent runtimes.

Process-group termination, the GitHub-credential env-var scrub set, and the
credential-hygiene helpers built on it (:func:`build_child_env`,
:func:`redact_with_scrubbed_values`, :func:`sanitize_output`) are used by more than one
``AgentRuntime`` implementation (Copilot, pi from Slice 3 of
``plans/pi-agent-runtime.md``, and the pi cache probe script), so no single
runtime owns them. Tolerant dotted-version parsing is shared by the runtime
prerequisite checks (Step 2.3 of ``plans/pi-agent-runtime.md``).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from datetime import datetime
from json import JSONDecodeError
from typing import TYPE_CHECKING, Protocol

from . import redaction

if TYPE_CHECKING:
    from .models import AgentRole

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


def redact_with_scrubbed_values(text: str, scrubbed_values: set[str]) -> str:
    """Redact scrubbed credential values and secret shapes from text.

    Secret shapes come from :func:`software_agent_factory.redaction.redact_secrets`.
    Does not collapse whitespace or truncate, so a caller can redact a whole
    buffer *before* truncating it: a secret cut in half by the truncation
    would otherwise escape exact-value redaction. Values shorter than four
    characters are ignored (too short to be a credential, too likely to hit
    unrelated text).
    """
    redacted = text
    for value in sorted(scrubbed_values, key=len, reverse=True):
        if len(value) >= 4:
            redacted = redacted.replace(value, redaction.REDACTION_PLACEHOLDER)
    return redaction.redact_secrets(redacted)


def sanitize_output(text: str, scrubbed_values: set[str]) -> str:
    """Redact scrubbed credential values and secret shapes from text.

    Collapses whitespace and truncates to 600 characters, matching the
    excerpt length used in failure-reason and log messages.
    """
    sanitized = " ".join(redact_with_scrubbed_values(text, scrubbed_values).split())
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


def extract_first_event_ms(stdout: str, started_at_dt: datetime) -> float | None:
    """Best-effort first-event latency from JSONL events that carry a ``timestamp``.

    Copilot and Claude Code ``stream-json`` events both carry one.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        raw_ts = event.get("timestamp")
        if isinstance(raw_ts, str):
            try:
                ts_str = raw_ts.replace("Z", "+00:00")
                event_dt = datetime.fromisoformat(ts_str)
                delta_ms = (event_dt - started_at_dt).total_seconds() * 1000.0
                if delta_ms >= 0:
                    return delta_ms
            except (ValueError, TypeError):
                continue
        elif isinstance(raw_ts, (int, float)) and raw_ts > 0:
            event_sec = raw_ts if raw_ts < 1e11 else raw_ts / 1000.0
            delta_ms = (event_sec - started_at_dt.timestamp()) * 1000.0
            if delta_ms >= 0:
                return delta_ms
    return None


def _decode_timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def merge_timeout_output(previous: object, final: str) -> str:
    prefix = _decode_timeout_text(previous)
    if not prefix or final.startswith(prefix):
        return final
    return f"{prefix}{final}"


def format_failure_reason(
    *,
    role: AgentRole,
    message: str,
    stdout: str,
    stderr: str,
    scrubbed_values: set[str],
    limit: int,
) -> str:
    sections: list[str] = [f"{role.value}: {message}."]
    cleaned_stdout = sanitize_output(stdout, scrubbed_values)
    cleaned_stderr = sanitize_output(stderr, scrubbed_values)
    if cleaned_stdout:
        sections.append(f"stdout={cleaned_stdout}")
    if cleaned_stderr:
        sections.append(f"stderr={cleaned_stderr}")
    combined = " ".join(sections)
    if len(combined) <= limit:
        return combined
    return f"{combined[: limit - 12].rstrip()}...[truncated]"
