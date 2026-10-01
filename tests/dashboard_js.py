"""Read the dashboard script as text.

No JavaScript runner is available in the test suite (ADR-016), so asset tests
pin the wiring in the source. These helpers cut a function out of the script so
a test can assert on that function alone instead of on the whole file.
"""

from __future__ import annotations

import re
from functools import cache

_QUOTES = "\"'"


def normalized(text: str) -> str:
    """Collapse every run of whitespace to one space."""
    return " ".join(text.split())


def _skip_string(js: str, start: int) -> int:
    """Index just past the string literal that opens at ``start``."""
    quote = js[start]
    index = start + 1
    while index < len(js):
        if js[index] == "\\":
            index += 2
        elif js[index] == quote:
            return index + 1
        else:
            index += 1
    raise AssertionError("unterminated string literal in the dashboard script")


@cache
def strip_comments(js: str) -> str:
    """The script without ``//`` and ``/* */`` comments; string literals stay intact."""
    kept: list[str] = []
    index = 0
    while index < len(js):
        if js.startswith("//", index):
            end = js.find("\n", index)
            index = len(js) if end == -1 else end
        elif js.startswith("/*", index):
            end = js.find("*/", index)
            if end == -1:
                raise AssertionError("unterminated block comment in the dashboard script")
            index = end + 2
        elif js[index] in _QUOTES:
            end = _skip_string(js, index)
            kept.append(js[index:end])
            index = end
        else:
            kept.append(js[index])
            index += 1
    return "".join(kept)


def _balanced_source(code: str, start: int, open_index: int, label: str) -> str:
    """Normalized ``code[start:close]`` where ``close`` ends the block opened at ``open_index``."""
    depth = 0
    index = open_index
    while index < len(code):
        char = code[index]
        if char in _QUOTES:
            index = _skip_string(code, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return normalized(code[start : index + 1])
        index += 1
    raise AssertionError(f"{label} has unbalanced braces")


def function_source(js: str, name: str) -> str:
    """Normalized source of ``function name(...) { ... }``, comments removed.

    The closing brace is found by balancing braces, so nested blocks and
    callbacks are included. Raises when the function is missing or its braces
    do not balance.
    """
    code = strip_comments(js)
    start = re.search(rf"function {re.escape(name)}\(", code)
    if start is None:
        raise AssertionError(f"function {name} not found in the dashboard script")
    index = code.find("{", code.index(")", start.end()))
    if index == -1:
        raise AssertionError(f"function {name} has no body")
    return _balanced_source(code, start.start(), index, f"function {name}")


def object_literal_source(js: str, name: str) -> str:
    """Normalized source of ``const name = { ... };``, comments removed.

    Like ``function_source``: the closing brace is found by balancing braces.
    Raises when the constant is missing or its braces do not balance.
    """
    code = strip_comments(js)
    start = re.search(rf"const {re.escape(name)} = \{{", code)
    if start is None:
        raise AssertionError(f"object {name} not found in the dashboard script")
    return _balanced_source(code, start.start(), start.end() - 1, f"object {name}")


def listener_source(js: str, function: str, target: str, event: str) -> str:
    """Normalized source of the callback of ``target.addEventListener("event", ...)``.

    The listener is looked up inside ``function``. The result runs from the call to the brace
    that closes the callback, found by balancing braces, so a test can assert on what that
    listener does and on nothing else. Raises when the listener is missing.
    """
    body = function_source(js, function)
    call = f'{target}.addEventListener("{event}"'
    start = body.find(call)
    if start == -1:
        raise AssertionError(f"no {event} listener on {target} in function {function}")
    open_index = body.find("{", body.index("function", start))
    return _balanced_source(body, start, open_index, f"{event} listener on {target}")
