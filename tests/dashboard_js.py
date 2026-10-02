"""Read the dashboard script as text, and run its pure helpers in ``node``.

Asset tests pin DOM wiring and security rules in the source. The readers cut a
function out of the script so a test can assert on that function alone instead
of on the whole file. ``run_functions`` loads selected helpers, with the
constants and helpers they use, into one ``node`` process and calls them with
JSON arguments. It needs plain ``node`` only: no npm and no bundler (ADR-016).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Final, NamedTuple

import pytest

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


# --------------------------------------------------------------------------
# Running pure helpers in node
# --------------------------------------------------------------------------

_DEFINITION = re.compile(r"^  ((?:function (\w+)\(|(?:const|let) (\w+) =))", re.MULTILINE)
_STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_IDENTIFIER = re.compile(r"(?<![\w.$])[A-Za-z_]\w*")
_NODE_TIMEOUT_SECONDS = 30
_SENTINEL_KEY = "$js"


class _Undefined:
    def __repr__(self) -> str:
        return "UNDEFINED"


# JSON cannot carry these JavaScript values. Each crosses the boundary as
# ``{"$js": name}``: in arguments and in results, nested anywhere.
UNDEFINED: Final = _Undefined()
_FROM_NAME: Final[dict[str, object]] = {
    "undefined": UNDEFINED,
    "NaN": math.nan,
    "Infinity": math.inf,
    "-Infinity": -math.inf,
}

# The page needs a browser. A call gets the few globals the helpers read: a fake
# ``document`` whose elements are plain objects, and ``location`` and ``history``
# set per call, so a test can see what a helper wrote to the address bar.
_PRELUDE = """\
"use strict";
const NON_JSON = {
  "undefined": undefined, "NaN": NaN, "Infinity": Infinity, "-Infinity": -Infinity
};
function reviveValue(value) {
  if (Array.isArray(value)) {
    return value.map(reviveValue);
  }
  if (value === null || typeof value !== "object") {
    return value;
  }
  const keys = Object.keys(value);
  if (keys.length === 1 && keys[0] === "$js") {
    return NON_JSON[value.$js];
  }
  return Object.fromEntries(keys.map((key) => [key, reviveValue(value[key])]));
}
function replaceValue(key, value) {
  if (value === undefined || (typeof value === "number" && !Number.isFinite(value))) {
    return { $js: String(value) };
  }
  return value;
}
const calls = reviveValue(JSON.parse(require("fs").readFileSync(0, "utf8")));
const document = {
  createElement: (tagName) => ({ tagName: tagName, className: "", textContent: "" })
};
"""
_RUNNER = """\
const results = calls.map((call) => {
  const entries = [];
  globalThis.location = call.location;
  globalThis.history = { replaceState: (...entry) => entries.push(entry) };
  try {
    return { value: api[call.function](...call.args), history: entries };
  } catch (error) {
    return { error: String(error), history: entries };
  }
});
process.stdout.write(JSON.stringify(results, replaceValue));
"""


class JsCall(NamedTuple):
    """One call of a top-level helper with JSON arguments and an optional ``location``."""

    function: str
    args: tuple[object, ...] = ()
    location: Mapping[str, str] | None = None


@dataclass(frozen=True)
class JsResult:
    """What one call returned as JSON and the ``history.replaceState`` calls it made.

    When the helper threw, ``error`` holds the message and reading ``value`` raises, so one
    bad case fails only the tests that read it.
    """

    function: str
    returned: object
    history: list[list[object]]
    error: str | None = None

    @property
    def value(self) -> object:
        if self.error is not None:
            raise AssertionError(f"{self.function} threw in node: {self.error}")
        return self.returned


def find_node() -> str | None:
    """Path of ``node`` on ``PATH``, or ``None`` when it is not installed."""
    return shutil.which("node")


def require_node() -> str:
    """Path of ``node``. When it is missing, fail in CI and skip on a developer machine."""
    node = find_node()
    if node is not None:
        return node
    message = "node is not on PATH"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def _definition_end(code: str, start: int) -> int:
    """Index just past the top-level ``function`` or ``const`` that starts at ``start``."""
    is_function = code.startswith("function", start)
    depth = 0
    index = start
    while index < len(code):
        char = code[index]
        if char in _QUOTES:
            index = _skip_string(code, index)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0 and char == "}" and is_function:
                return index + 1
        elif char == ";" and depth == 0:
            return index + 1
        index += 1
    raise AssertionError(f"declaration at offset {start} is not closed")


def _names_used(source: str) -> set[str]:
    """Identifiers in ``source`` outside string literals and property accesses."""
    return set(_IDENTIFIER.findall(_STRING.sub('""', source)))


def _definitions_for(js: str, wanted: Iterable[str]) -> list[str]:
    """Source of the top-level definitions ``wanted`` needs, in script order."""
    code = strip_comments(js)
    starts = {m.group(2) or m.group(3): m.start(1) for m in _DEFINITION.finditer(code)}
    chosen: dict[str, str] = {}
    pending = list(wanted)
    while pending:
        name = pending.pop()
        if name in chosen:
            continue
        if name not in starts:
            raise AssertionError(f"{name} is not a top-level definition in the dashboard script")
        chosen[name] = code[starts[name] : _definition_end(code, starts[name])]
        pending.extend(used for used in _names_used(chosen[name]) if used in starts)
    return [chosen[name] for name in sorted(chosen, key=starts.__getitem__)]


def _encode(value: object) -> object:
    """``value`` with the JavaScript-only values replaced by their ``{"$js": name}`` form."""
    if value is UNDEFINED:
        return {_SENTINEL_KEY: "undefined"}
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return {_SENTINEL_KEY: "NaN"}
        return {_SENTINEL_KEY: "Infinity" if value > 0 else "-Infinity"}
    if isinstance(value, Mapping):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_encode(item) for item in value]
    return value


def _decode(value: object) -> object:
    """Reverse of ``_encode`` on a parsed JSON result."""
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, dict):
        if value.keys() == {_SENTINEL_KEY}:
            return _FROM_NAME[value[_SENTINEL_KEY]]
        return {key: _decode(item) for key, item in value.items()}
    return value


def _call_payload(call: JsCall) -> dict[str, object]:
    return dict(function=call.function, args=_encode(call.args), location=call.location)


def _parse_result(call: JsCall, outcome: dict[str, object]) -> JsResult:
    history = _decode(outcome["history"])
    assert isinstance(history, list)
    error = outcome.get("error")
    assert error is None or isinstance(error, str)
    return JsResult(call.function, _decode(outcome.get("value")), history, error)


def run_functions(js: str, calls: Sequence[JsCall]) -> list[JsResult]:
    """Call top-level helpers of the script in one ``node`` process.

    The script is not run as a whole. Each called helper is loaded with the
    constants and helpers it names, so no DOM is needed. Raises when a helper
    is missing or when ``node`` fails. A helper that throws gives a result whose
    ``error`` is set. Without ``node`` it fails in CI and skips elsewhere
    (``require_node``).
    """
    node = require_node()
    names = sorted({call.function for call in calls})
    script = "\n".join(
        [_PRELUDE, *_definitions_for(js, names), f"const api = {{ {', '.join(names)} }};", _RUNNER]
    )
    completed = subprocess.run(
        [node, "-e", script],
        input=json.dumps([_call_payload(call) for call in calls]),
        capture_output=True,
        text=True,
        timeout=_NODE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed ({completed.returncode}): {completed.stderr.strip()}")
    outcomes = json.loads(completed.stdout)
    return [_parse_result(call, outcome) for call, outcome in zip(calls, outcomes, strict=True)]
