"""Tests for the JavaScript source readers in ``dashboard_js`` (ADR-016)."""

from __future__ import annotations

import pytest
from dashboard_js import function_source, object_literal_source, strip_comments


def test_function_source_returns_the_balanced_body_without_comments() -> None:
    js = """
    function first(a) {
      // a brace in a comment: }
      const text = "} not a close {";
      if (a) { return 'x'; }
      return text;
    }
    function second() { return 1; }
    """
    assert function_source(js, "first") == (
        "function first(a) { "
        "const text = \"} not a close {\"; if (a) { return 'x'; } return text; }"
    )
    assert function_source(js, "second") == "function second() { return 1; }"


def test_function_source_raises_when_the_function_is_missing() -> None:
    with pytest.raises(AssertionError, match="missing not found"):
        function_source("function other() { return 1; }", "missing")


def test_function_source_raises_when_braces_do_not_balance() -> None:
    with pytest.raises(AssertionError, match="unbalanced"):
        function_source("function broken() { if (a) { return 1; }", "broken")


def test_object_literal_source_returns_the_balanced_literal_without_comments() -> None:
    js = """
    const OTHER = { a: 1 };
    const TABLE = {
      // a brace in a comment: }
      one: function () { return "}"; },
      two: { nested: true }
    };
    """
    assert object_literal_source(js, "TABLE") == (
        'const TABLE = { one: function () { return "}"; }, two: { nested: true } }'
    )


def test_object_literal_source_raises_when_the_object_is_missing() -> None:
    with pytest.raises(AssertionError, match="object MISSING not found"):
        object_literal_source("const OTHER = { a: 1 };", "MISSING")


def test_object_literal_source_raises_when_braces_do_not_balance() -> None:
    with pytest.raises(AssertionError, match="unbalanced"):
        object_literal_source("const BROKEN = { a: { b: 1 };", "BROKEN")


def test_strip_comments_ignores_a_close_brace_inside_a_block_comment() -> None:
    assert strip_comments("a /* } */ b") == "a  b"


def test_strip_comments_keeps_a_string_with_an_escaped_quote() -> None:
    js = r'const text = "say \"// hi\""; // gone'
    assert strip_comments(js) == r'const text = "say \"// hi\""; '


def test_strip_comments_raises_on_an_unterminated_string() -> None:
    with pytest.raises(AssertionError, match="unterminated string"):
        strip_comments('const text = "open')


def test_strip_comments_raises_on_an_unterminated_block_comment() -> None:
    with pytest.raises(AssertionError, match="unterminated block comment"):
        strip_comments("const a = 1; /* open")
