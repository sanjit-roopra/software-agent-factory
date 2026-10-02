"""Tests for the pure redaction module."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from software_agent_factory import redaction
from software_agent_factory.redaction import (
    _SECRET_PATTERNS,
    REASON_LIMIT,
    REDACTION_PLACEHOLDER,
    bounded_reason,
    contains_secret,
    redact_secrets,
)

_PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"

#: One sample per secret pattern. Each row is (id, sample text, the part that
#: must not survive redaction).
_SAMPLES: tuple[tuple[str, str, str], ...] = (
    ("github-token", "ghp_abcdefgh12345678", "abcdefgh12345678"),
    ("github-pat", "github_pat_abcdefghij0123456789", "abcdefghij0123456789"),
    ("aws-access-key-id", "AKIAABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOP"),
    ("aws-secret-key", "aws_secret_access_key=" + "A1b2C3d4E5" * 4, "A1b2C3d4E5"),
    (
        "pem-block",
        f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_BODY}\n-----END RSA PRIVATE KEY-----",
        _PEM_BODY,
    ),
    ("pem-header", "-----BEGIN PRIVATE KEY-----", "BEGIN PRIVATE KEY"),
    ("authorization-header", "Authorization: Digest username=bob", "username=bob"),
    ("bearer", "bearer abcdefghijklmnopqrstuvwx", "abcdefghijklmnopqrstuvwx"),
    ("basic", "Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("cookie", "Cookie: sid=abc123def456", "abc123def456"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "dozjgNryP4J3jVmNHl0w5N",
    ),
    ("assignment", "MY_API_KEY=abcdefgh12345", "abcdefgh12345"),
    ("assignment-secret-key", "SECRET_KEY=abcdefgh12345", "abcdefgh12345"),
    ("assignment-hyphen-api-key", "api-key: abcdefgh12345", "abcdefgh12345"),
    ("openai-key", "sk-" + "a1B2c3D4e5" * 3, "a1B2c3D4e5"),
    ("openai-project-key", "sk-proj-" + "a1B2c3D4e5" * 3, "a1B2c3D4e5"),
    ("anthropic-key", "sk-ant-api03-" + "a1B2c3D4e5" * 3, "a1B2c3D4e5"),
    ("gitlab-token", "glpat-" + "a1B2c3D4e5" * 3, "a1B2c3D4e5"),
    ("slack-token", "xoxb-1234567890-abcdef", "1234567890-abcdef"),
    ("url-user-password", "https://bob:hunter22@example.com/repo", "hunter22"),
    ("url-user-only", "ssh://deploytoken@example.com/repo", "deploytoken"),
    ("pem-header-variant", "-----BEGIN EC-P256 PRIVATE KEY-----", "EC-P256"),
)

#: (id, text, redacted?) pairs written by hand around the smallest accepted size
#: of each quantified pattern. Change them with the quantifier when a pattern changes.
_THRESHOLDS: tuple[tuple[str, str, bool], ...] = (
    ("github-token-7", "ghp_" + "a" * 7, False),
    ("github-token-8", "ghp_" + "a" * 8, True),
    ("github-pat-19", "github_pat_" + "a" * 19, False),
    ("github-pat-20", "github_pat_" + "a" * 20, True),
    ("aws-key-body-15", "AKIA" + "A" * 15, False),
    ("aws-key-body-16", "AKIA" + "A" * 16, True),
    ("aws-key-body-17", "AKIA" + "A" * 17, False),
    ("jwt-segment-9", "eyJ" + "a" * 10 + "." + "b" * 9 + "." + "c" * 10, False),
    ("jwt-segment-10", "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10, True),
    ("bearer-19", "bearer " + "a" * 19, False),
    ("bearer-20", "bearer " + "a" * 20, True),
    ("assignment-7", "MY_TOKEN=" + "a" * 7, False),
    ("assignment-8", "MY_TOKEN=" + "a" * 8, True),
    ("secret-key-7", "SECRET_KEY=" + "a" * 7, False),
    ("secret-key-8", "SECRET_KEY=" + "a" * 8, True),
    ("aws-secret-39", "aws_secret_access_key=" + "a" * 39, False),
    ("aws-secret-40", "aws_secret_access_key=" + "a" * 40, True),
    ("basic-7", "Basic " + "a" * 7 + "=", False),
    ("basic-8", "Basic " + "a" * 8 + "=", True),
    ("openai-key-19", "sk-" + "a" * 19, False),
    ("openai-key-20", "sk-" + "a" * 20, True),
    ("gitlab-token-19", "glpat-" + "a" * 19, False),
    ("gitlab-token-20", "glpat-" + "a" * 20, True),
    ("slack-token-9", "xoxb-" + "1" * 9, False),
    ("slack-token-10", "xoxb-" + "1" * 10, True),
    ("slack-token-kind-x", "xoxx-" + "1" * 10, False),
    ("url-without-credentials", "https://example.com/repo", False),
    ("url-with-user", "https://u@example.com/repo", True),
)


@pytest.mark.parametrize(
    ("sample", "secret"), [row[1:] for row in _SAMPLES], ids=[row[0] for row in _SAMPLES]
)
def test_each_secret_shape_is_redacted(sample: str, secret: str) -> None:
    redacted = redact_secrets(f"before {sample} after")

    assert REDACTION_PLACEHOLDER in redacted
    assert secret not in redacted


@pytest.mark.parametrize("pattern", _SECRET_PATTERNS, ids=lambda pattern: pattern.pattern[:40])
def test_every_secret_pattern_matches_some_sample(pattern: re.Pattern[str]) -> None:
    assert any(pattern.search(sample) for _, sample, _ in _SAMPLES)


@pytest.mark.parametrize(
    ("text", "is_redacted"),
    [row[1:] for row in _THRESHOLDS],
    ids=[row[0] for row in _THRESHOLDS],
)
def test_pattern_minimum_length_boundaries(text: str, is_redacted: bool) -> None:
    assert (redact_secrets(f"x {text} y") != f"x {text} y") is is_redacted


@pytest.mark.parametrize(
    ("sample", "secret"), [row[1:] for row in _SAMPLES], ids=[row[0] for row in _SAMPLES]
)
def test_contains_secret_is_true_for_each_secret_shape(sample: str, secret: str) -> None:
    assert contains_secret(f"before {sample} after") is True


@pytest.mark.parametrize("text", ["", "plain failure text", "configure basic authentication"])
def test_contains_secret_is_false_without_a_secret_shape(text: str) -> None:
    assert contains_secret(text) is False


def test_several_secrets_in_one_text_are_all_redacted() -> None:
    text = "a ghp_abcdefgh12345678 b AKIAABCDEFGHIJKLMNOP c MY_TOKEN=abcdefgh12345 d"

    assert redact_secrets(text) == f"a {REDACTION_PLACEHOLDER} b {REDACTION_PLACEHOLDER} c " + (
        f"{REDACTION_PLACEHOLDER} d"
    )


def test_header_redaction_stops_at_the_end_of_the_line() -> None:
    text = "Authorization: Bearer abc\nnext line stays\r\nCookie: sid=1\nlast line stays"

    assert redact_secrets(text) == (
        f"{REDACTION_PLACEHOLDER}\nnext line stays\r\n{REDACTION_PLACEHOLDER}\nlast line stays"
    )


def test_redact_secrets_leaves_empty_and_safe_text_alone() -> None:
    assert redact_secrets("") == ""
    assert redact_secrets("plain failure text") == "plain failure text"


@pytest.mark.parametrize("kind", "pousr")
def test_every_github_token_kind_is_redacted(kind: str) -> None:
    token = f"gh{kind}_abcdefgh"

    assert redact_secrets(f"saw {token} here") == f"saw {REDACTION_PLACEHOLDER} here"


def test_a_github_token_body_with_underscores_is_redacted_whole() -> None:
    assert redact_secrets("x ghp_abc_def_ghi_jkl y") == f"x {REDACTION_PLACEHOLDER} y"


def _cut_shape(limit: int = REASON_LIMIT, run_id: str | None = None) -> tuple[int, str, int]:
    """Read the head size, marker and tail size from a real cut."""
    reason, truncated = bounded_reason("H" * (limit * 2) + "T" * (limit * 2), limit, run_id)
    assert truncated is True
    head = len(reason) - len(reason.lstrip("H"))
    tail = len(reason) - len(reason.rstrip("T"))
    return head, reason[head : len(reason) - tail], tail


_SECRET = "ghp_abcdefgh12345678"
_FRAGMENTS = ("ghp_", "abcdefgh", "12345678")


def _assert_no_fragment(reason: str) -> None:
    for fragment in _FRAGMENTS:
        assert fragment not in reason


def test_secret_in_the_dropped_middle_leaves_no_trace() -> None:
    head, _, _ = _cut_shape()
    text = "x" * REASON_LIMIT + _SECRET + " " + "y" * REASON_LIMIT
    assert head < text.index(_SECRET) < len(text) - REASON_LIMIT

    reason, truncated = bounded_reason(text)

    assert truncated is True
    _assert_no_fragment(reason)
    assert REDACTION_PLACEHOLDER not in reason


def test_secret_straddling_the_head_edge_is_redacted() -> None:
    head, _, _ = _cut_shape()
    start = head - len(_SECRET) // 2
    text = "x" * start + _SECRET + " " + "y" * (REASON_LIMIT * 2)
    assert start < head < start + len(_SECRET)

    reason, truncated = bounded_reason(text)

    assert truncated is True
    _assert_no_fragment(reason)
    assert REDACTION_PLACEHOLDER in reason.split("\n", 1)[0]


def test_secret_straddling_the_tail_edge_is_redacted() -> None:
    _, _, tail = _cut_shape()
    prefix = "x" * (REASON_LIMIT * 2)
    suffix = " " + "y" * (tail - len(_SECRET) // 2 - 1)
    text = prefix + _SECRET + suffix
    boundary = len(text) - tail
    assert text.index(_SECRET) < boundary < text.index(_SECRET) + len(_SECRET)

    reason, truncated = bounded_reason(text)

    assert truncated is True
    _assert_no_fragment(reason)
    assert REDACTION_PLACEHOLDER in reason.rsplit("\n", 1)[1]


def test_reason_at_the_limit_is_shown_in_full() -> None:
    text = "a" * REASON_LIMIT

    reason, truncated = bounded_reason(text)

    assert reason == text
    assert truncated is False


def test_reason_one_over_the_limit_is_cut_and_marked() -> None:
    text = "S" * (REASON_LIMIT // 2) + "M" * (REASON_LIMIT // 2 + 1)

    reason, truncated = bounded_reason(text)

    assert truncated is True
    assert len(reason) <= REASON_LIMIT
    assert "factory show <run>" in reason
    assert reason.startswith("SSS")
    assert reason.endswith("MMM")


def test_the_length_limit_counts_the_redacted_text() -> None:
    secret = "GH_TOKEN=ghp_abcdefgh12345678"
    text = "z" * (REASON_LIMIT - 20) + " " + secret
    assert len(text) > REASON_LIMIT

    reason, truncated = bounded_reason(text)

    assert truncated is False
    assert reason == "z" * (REASON_LIMIT - 20) + " " + REDACTION_PLACEHOLDER


def test_marker_names_the_run_when_it_is_known() -> None:
    reason, _ = bounded_reason("q" * (REASON_LIMIT * 2), run_id="run-42")

    assert "factory show run-42" in reason
    assert len(reason) <= REASON_LIMIT


def test_marker_falls_back_to_the_run_placeholder_for_an_empty_run_id() -> None:
    reason, _ = bounded_reason("q" * (REASON_LIMIT * 2), run_id="")

    assert "factory show <run>" in reason


@pytest.mark.parametrize("limit", [100, REASON_LIMIT, 2000])
def test_cut_result_never_exceeds_the_limit(limit: int) -> None:
    reason, truncated = bounded_reason("w" * (limit * 3), limit=limit, run_id="r" * 20)

    assert truncated is True
    assert len(reason) == limit


def test_limit_too_small_for_the_marker_is_rejected() -> None:
    with pytest.raises(ValueError, match="too small"):
        bounded_reason("w" * 100, limit=10)


def test_limit_one_above_the_marker_is_rejected() -> None:
    _, marker, _ = _cut_shape()

    with pytest.raises(ValueError, match="too small"):
        bounded_reason("H" * 50 + "T" * 50, limit=len(marker) + 1)


def test_limit_two_above_the_marker_keeps_one_head_and_one_tail_character() -> None:
    _, marker, _ = _cut_shape()
    limit = len(marker) + 2

    reason, truncated = bounded_reason("H" * 50 + "T" * 50, limit=limit)

    assert truncated is True
    assert len(reason) == limit
    assert reason == "H" + marker + "T"


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
