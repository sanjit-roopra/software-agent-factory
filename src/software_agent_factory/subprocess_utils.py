"""Runtime-neutral subprocess helpers shared by agent runtimes.

Process-group termination, the GitHub-credential env-var scrub set, and
tolerant dotted-version parsing are used by more than one ``AgentRuntime``
implementation (Copilot today, pi from Slice 3 of
``plans/pi-agent-runtime.md``), so neither runtime owns them.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess

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


def kill_process_group(
    process: subprocess.Popen[str],
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


def parse_version(text: str) -> tuple[int, ...] | None:
    """Parse a dotted version string into a tuple comparable with ``<``/``>=``.

    Tolerant of a leading ``"v"``/``"V"`` (``"v22.19.0"``) and of trailing
    non-numeric text after the dotted version (``"22.19.0 (arm64)"``).
    Returns ``None`` when no leading dotted-integer version can be found, so
    callers (e.g. ``doctor``) can distinguish "older than required" from
    "version could not be determined".
    """

    stripped = text.strip()
    if stripped[:1] in ("v", "V"):
        stripped = stripped[1:]
    match = _VERSION_PATTERN.match(stripped)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))
