"""Tests for the pure redaction module."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from software_agent_factory import redaction, verification
from software_agent_factory.redaction import (
    _SECRET_PATTERNS,
    REDACTION_PLACEHOLDER,
    bounded_reason,
    redact_secrets,
)

_PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"

#: One sample per entry of ``_SECRET_PATTERNS``, in the same order. Each pair is
#: (sample text, the part that must not survive redaction).
_SAMPLES: tuple[tuple[str, str], ...] = (
    ("ghp_abcdefgh12345678", "abcdefgh12345678"),
    ("github_pat_abcdefghij0123456789", "abcdefghij0123456789"),
    ("AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
    ("aws_secret_access_key=" + "A1b2C3d4E5" * 4, "A1b2C3d4E5"),
    (f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_BODY}\n-----END RSA PRIVATE KEY-----", _PEM_BODY),
    ("-----BEGIN PRIVATE KEY-----", "BEGIN PRIVATE KEY"),
    ("Authorization: Digest username=bob", "username=bob"),
    ("bearer abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("Cookie: sid=abc123def456", "abc123def456"),
    (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "dozjgNryP4J3jVmNHl0w5N",
    ),
    ("MY_API_KEY=abcdefgh12345", "abcdefgh12345"),
)


def test_every_secret_pattern_has_a_sample() -> None:
    assert len(_SAMPLES) == len(_SECRET_PATTERNS)


@pytest.mark.parametrize("index", range(len(_SAMPLES)))
def test_each_secret_pattern_is_redacted(index: int) -> None:
    sample, secret = _SAMPLES[index]
    pattern: re.Pattern[str] = _SECRET_PATTERNS[index]

    assert pattern.search(sample) is not None
    redacted = redact_secrets(f"before {sample} after")

    assert REDACTION_PLACEHOLDER in redacted
    assert secret not in redacted


def test_redact_secrets_leaves_empty_and_safe_text_alone() -> None:
    assert redact_secrets("") == ""
    assert redact_secrets("plain failure text") == "plain failure text"


def test_verification_reuses_the_redaction_functions() -> None:
    assert verification.redact_secrets is redaction.redact_secrets


@pytest.mark.parametrize("start", [200, 245, 490])
def test_secret_across_the_cut_is_fully_redacted(start: int) -> None:
    secret = "GH_TOKEN=ghp_abcdefgh12345678"
    text = "x" * (start - 1) + " " + secret + " " + "y" * 600
    assert text.index(secret) == start

    reason, truncated = bounded_reason(text)

    assert truncated is True
    assert "ghp_" not in reason
    assert "abcdefgh" not in reason
    assert "12345678" not in reason


def test_reason_of_500_characters_is_shown_in_full() -> None:
    text = "a" * 500

    reason, truncated = bounded_reason(text)

    assert reason == text
    assert truncated is False


def test_reason_of_501_characters_is_cut_and_marked() -> None:
    text = "S" * 250 + "M" * 251

    reason, truncated = bounded_reason(text)

    assert truncated is True
    assert len(reason) <= 500
    assert "factory show <run>" in reason
    assert reason.startswith("SSS")
    assert reason.endswith("MMM")


def test_the_length_limit_counts_the_redacted_text() -> None:
    secret = "GH_TOKEN=ghp_abcdefgh12345678"
    text = "z" * 480 + " " + secret

    reason, truncated = bounded_reason(text)

    assert truncated is False
    assert reason == "z" * 480 + " " + REDACTION_PLACEHOLDER


def test_marker_names_the_run_when_it_is_known() -> None:
    reason, _ = bounded_reason("q" * 900, run_id="run-42")

    assert "factory show run-42" in reason
    assert len(reason) <= 500


@pytest.mark.parametrize("limit", [100, 500, 2000])
def test_cut_result_never_exceeds_the_limit(limit: int) -> None:
    reason, truncated = bounded_reason("w" * (limit * 3), limit=limit, run_id="r" * 20)

    assert truncated is True
    assert len(reason) == limit


def test_limit_too_small_for_the_marker_is_rejected() -> None:
    with pytest.raises(ValueError, match="too small"):
        bounded_reason("w" * 100, limit=10)


def test_redaction_module_imports_only_re() -> None:
    # The package __init__ imports subprocess, so a sys.modules check cannot
    # tell. Inspect the module's own imports instead.
    tree = ast.parse(Path(redaction.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports could pull in subprocess"
            imported.add(node.module or "")

    assert imported == {"__future__", "re"}
