"""Pure secret redaction and bounded reason text.

This module imports only ``re``. It must stay free of ``subprocess`` and of
every other factory module, so a read-only surface such as the dashboard can
redact text without importing a process-spawning module itself. The package
``__init__`` still imports ``verification``, so this does not keep
``subprocess`` out of the running process.

Redaction always runs before any cut. A secret then cannot be split by the
cut and leave a readable fragment.
"""

from __future__ import annotations

import re

REDACTION_PLACEHOLDER = "[REDACTED]"

#: Default cap, in characters, for a reason shown to an operator.
REASON_LIMIT = 500

#: The words before ``PRIVATE KEY`` in a PEM header, such as ``RSA `` or ``EC-P256 ``.
_PEM_LABEL = r"[A-Z0-9_ -]{0,40}"

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub personal access / app / OAuth tokens. Eight characters is the
    # shortest body any caller ever redacted; the class includes ``_``. The guard skips a
    # prefix inside a word or snake_case name, such as ``num_highs_and_lows`` or
    # ``use_ghs_runner_cfg``.
    re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{8,}"),
    # A full-length token is caught even right after a letter, as in ``%3Dghp_...`` or
    # an escaped ``\\nghp_...``. Real tokens have 36 letters and digits.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9_])"),
    re.compile(r"github_pat_\w{20,}"),
    # GitLab personal access tokens, OpenAI and Anthropic keys (``sk-``, ``sk-proj-``,
    # ``sk-ant-``), and Slack tokens.
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?i:xox[baprse])-[0-9A-Za-z-]{10,}"),
    # AWS access key ids and secret access keys.
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\baws_secret_access_key\b[^\S\r\n]*[:=][^\S\r\n]*(?:\r?\n[^\S\r\n]+)?[\"']?[a-z0-9/+=]{40}[\"']?"
    ),
    # Credentials in a URL: ``scheme://user:password@host`` or ``scheme://token@host``.
    # The scheme is at most 32 characters, so a scan from each start stays short. The
    # fixed ``git@`` user of an SSH remote is not a secret.
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://(?!git@)[^/\s@]+@[^\s/]+"),
    # PEM-encoded private keys (any flavor), including the body. The body stops at the
    # next run of five dashes, so a header without an END costs one short scan.
    re.compile(
        rf"-----BEGIN {_PEM_LABEL}PRIVATE KEY-----(?:[^-]|-(?!----))*"
        rf"-----END {_PEM_LABEL}PRIVATE KEY-----"
    ),
    re.compile(rf"-----BEGIN {_PEM_LABEL}PRIVATE KEY-----"),
    # Authorization and proxy-authorization headers (all schemes: Basic, Bearer, Digest, etc.).
    re.compile(
        r"(?i)\b(?:authorization|proxy[_-]?authorization)\b[^\S\r\n]*[:=][^\S\r\n]*(?:\r?\n[^\S\r\n]+)?[^\r\n]+"
    ),
    re.compile(r"(?i)\bbearer\b\s*(?:[:=]\s*)?[a-z0-9._\-/+=]{20,}"),
    re.compile(r"(?i)\bBasic\s+[a-z0-9+/]{8,}={1,2}(?!\S)"),
    # Cookie and Set-Cookie headers.
    re.compile(
        r"(?i)\b(?:cookie|set[_-]?cookie|set[_-]?cookie2)\b[^\S\r\n]*[:=][^\S\r\n]*(?:\r?\n[^\S\r\n]+)?[^\r\n]+"
    ),
    # Standalone JWTs (JSON Web Tokens).
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # Explicit token/secret/password/session assignments.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|SECRET[_-]?KEY|SESSION[_-]?ID|SESSION[_-]?KEY|SESSION[_-]?TOKEN|JSESSIONID|PHPSESSID))\b"
        r"[^\S\r\n]*[:=][^\S\r\n]*(?:\r?\n[^\S\r\n]+)?"
        r"[\"']?[^\s\"';]{8,}[\"']?"
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


def contains_secret(text: str) -> bool:
    """Whether ``text`` holds any credential shape that :func:`redact_secrets` would replace."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def bounded_reason(
    text: str, limit: int = REASON_LIMIT, run_id: str | None = None
) -> tuple[str, bool]:
    """Redact ``text``, then cut it to at most ``limit`` characters.

    Returns ``(text, truncated)``. A cut keeps the start and the end and puts
    a marker between them. The marker names ``factory show <run_id>`` when
    ``run_id`` is given, and ``factory show <run>`` otherwise. The result,
    marker included, is never longer than ``limit``.

    Raises ``ValueError`` when ``limit`` is too small to hold the marker plus
    at least one character of head and one of tail.
    """
    redacted = redact_secrets(text)
    if len(redacted) <= limit:
        return redacted, False
    target = run_id if run_id else "<run>"
    marker = f"\n...[cut; run `factory show {target}` for the full text]...\n"
    keep = limit - len(marker)
    if keep < 2:
        raise ValueError("limit is too small for the truncation marker")
    head = keep - keep // 2
    tail = keep // 2
    return f"{redacted[:head]}{marker}{redacted[len(redacted) - tail :]}", True
