"""Deterministic repository command runner.

Implements the Phase 1 "deterministic verification" step described in
``docs/architecture.md`` ("Local verification") and ``PLAN.md``
("deterministic repository command runner"):

- Repository configuration supplies plain shell command strings (e.g.
  ``bun run lint``). This module does not know about YAML/config loading;
  callers (the ``config`` module's ``RepositoryConfig``) pass the resolved
  command list, timeout, environment allowlist and capture limit in.
- Commands run through ``/bin/sh -lc`` since they are user-authored
  repository configuration, not attacker-controlled input, and may rely on
  shell features (``&&``, globs, PATH lookups via login shell profile).
- Execution stops after the first failing (non-zero exit or timed out)
  command. An empty command list passes trivially.
- A timeout produces a failed ``CommandResult`` with ``timed_out=True``; it
  is never allowed to propagate as an uncaught ``TimeoutExpired``.

Environment isolation
---------------------
Repository commands are executed by (and on behalf of) an LLM-driven
implementation attempt, so the process environment is built from an explicit
allowlist instead of inheriting the operator's shell. Only
``PATH``/``HOME``/``LANG``/``TERM`` plus the names a repository explicitly
configures via ``repository.env_passthrough`` are forwarded; credentials such
as ``GH_TOKEN``, ``GITHUB_TOKEN`` or ``AWS_*`` are never passed implicitly.

Output bounding and redaction
-----------------------------
``stdout``/``stderr`` are redacted for well-known credential shapes (GitHub
tokens, AWS keys, PEM private keys, ``Authorization`` headers) and then
bounded to ``repository.log_capture_bytes`` before being returned, so
everything downstream -- persisted command logs, prompts, PR descriptions --
receives sanitized, size-bounded text. Redaction runs before truncation so a
secret can never be split across the elided middle and survive.

Only ``subprocess.TimeoutExpired`` is treated as an expected, structured
failure mode. Any other exception (missing ``cwd``, permission errors, etc.)
propagates unchanged so real infrastructure problems are not hidden as a
"failed command".

``CommandResult`` and ``VerificationReport`` are defined in the sibling
``software_agent_factory.models`` module. ``VerificationReport.failures`` is a
list of human-readable failure descriptions (not ``CommandResult`` objects);
the full per-command detail lives in ``deterministic_checks``. Fields
describing AI tester output (``coverage_change``, ``test_findings``) are
intentionally left at their model defaults since only the deterministic
runner executes here -- independent tester judgement is a separate
``TestReport``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .models import CommandResult, VerificationReport

#: Environment variables always provided to repository commands. Anything
#: else must be named explicitly by repository configuration.
BASE_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "TERM")

#: Default cap on retained stdout/stderr bytes per command.
DEFAULT_CAPTURE_BYTES = 32768

REDACTION_PLACEHOLDER = "[REDACTED]"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub personal access / app / OAuth tokens.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # AWS access key ids and secret access keys.
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\baws_secret_access_key\b\s*[:=]\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?"),
    # PEM-encoded private keys (any flavor), including the body.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Authorization headers / bearer tokens.
    re.compile(r"(?i)\b(?:authorization|bearer)\b\s*[:=]?\s*[A-Za-z0-9._\-/+=]{20,}"),
    # Explicit token/secret/password assignments.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY))\b\s*[:=]\s*"
        r"[\"']?[^\s\"']{8,}[\"']?"
    ),
)


def redact_secrets(text: str) -> str:
    """Replace well-known credential shapes with ``[REDACTED]``."""
    if not text:
        return text
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTION_PLACEHOLDER, redacted)
    return redacted


def bound_output(text: str, limit: int) -> str:
    """Bound ``text`` to ``limit`` bytes, keeping the head and the tail.

    Failures are usually explained at the end of a log while the beginning
    identifies what ran, so both ends are preserved and the middle is elided
    with an explicit marker naming the number of omitted bytes.
    """
    if limit < 1:
        raise ValueError("limit must be 1 or greater")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text

    head_bytes = limit // 2
    tail_bytes = limit - head_bytes
    omitted = len(encoded) - limit
    head = encoded[:head_bytes].decode("utf-8", errors="replace")
    tail = encoded[len(encoded) - tail_bytes :].decode("utf-8", errors="replace")
    return f"{head}\n...[truncated {omitted} bytes]...\n{tail}"


def sanitize_output(text: str, limit: int) -> str:
    """Redact credentials, then bound the result to ``limit`` bytes."""
    return bound_output(redact_secrets(text), limit)


def build_command_env(env_passthrough: Sequence[str] = ()) -> dict[str, str]:
    """Build the explicit environment for repository commands.

    Never inherits the ambient environment: only the base allowlist plus the
    explicitly configured names are forwarded, and only when actually set.
    """
    allowed: list[str] = [*BASE_ENV_ALLOWLIST, *env_passthrough]
    env: dict[str, str] = {}
    for name in allowed:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env.setdefault("PATH", os.defpath)
    return env


def _decode(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class DeterministicVerifier:
    """Runs a sequence of repository-configured shell commands and returns
    a controller-usable ``VerificationReport``."""

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = DEFAULT_CAPTURE_BYTES,
    ) -> VerificationReport:
        env = build_command_env(env_passthrough)
        results: list[CommandResult] = []
        passed = True

        for command in commands:
            started = time.monotonic()
            timed_out = False
            process = subprocess.Popen(
                ["/bin/sh", "-lc", command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = -1
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                stdout = stdout or _decode(exc.stdout)
                stderr = stderr or _decode(exc.stderr)

            duration = time.monotonic() - started
            result = CommandResult(
                command=command,
                exit_code=exit_code,
                stdout=sanitize_output(stdout, capture_bytes),
                stderr=sanitize_output(stderr, capture_bytes),
                duration_seconds=duration,
                timed_out=timed_out,
            )
            results.append(result)

            if timed_out or exit_code != 0:
                passed = False
                break

        failures = [_describe_failure(r) for r in results if r.timed_out or r.exit_code != 0]

        return VerificationReport(
            passed=passed,
            deterministic_checks=results,
            failures=failures,
            confidence=1.0 if passed else 0.0,
        )


def _describe_failure(result: "CommandResult") -> str:
    if result.timed_out:
        return f"{result.command!r} timed out after {result.duration_seconds:.1f}s"
    return f"{result.command!r} exited with code {result.exit_code}"
