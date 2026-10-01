"""Pure secret redaction and bounded reason text.

This module imports only ``re``. It must stay free of ``subprocess`` and of
every other factory module, so a read-only surface such as the dashboard can
redact text without loading process-spawning code.

Redaction always runs before any cut. A secret then cannot be split by the
cut and leave a readable fragment.
"""

from __future__ import annotations

import re

REDACTION_PLACEHOLDER = "[REDACTED]"

#: Default cap, in characters, for a reason shown to an operator.
REASON_LIMIT = 500

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
    # Authorization and proxy-authorization headers (all schemes: Basic, Bearer, Digest, etc.).
    re.compile(r"(?i)\b(?:authorization|proxy[_-]?authorization)\b\s*[:=]\s*[^\r\n]+"),
    re.compile(r"(?i)\bbearer\b\s*(?:[:=]\s*)?[a-z0-9._\-/+=]{20,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/]{8,}={1,2}(?!\S)"),
    # Cookie and Set-Cookie headers.
    re.compile(r"(?i)\b(?:cookie|set[_-]?cookie|set[_-]?cookie2)\b\s*[:=]\s*[^\r\n]+"),
    # Standalone JWTs (JSON Web Tokens).
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # Explicit token/secret/password/session assignments.
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|SESSION[_-]?ID|SESSION[_-]?KEY|SESSION[_-]?TOKEN|JSESSIONID|PHPSESSID))\b\s*[:=]\s*"
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


def bounded_reason(
    text: str, limit: int = REASON_LIMIT, run_id: str | None = None
) -> tuple[str, bool]:
    """Redact ``text``, then cut it to at most ``limit`` characters.

    Returns ``(text, truncated)``. A cut keeps the start and the end and puts
    a marker between them. The marker names ``factory show <run_id>`` when
    ``run_id`` is given, and ``factory show <run>`` otherwise. The result,
    marker included, is never longer than ``limit``.
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
