"""Tests for the JavaScript source readers and the node runner in ``dashboard_js`` (ADR-016)."""

from __future__ import annotations

import math

import pytest
from dashboard_js import (
    UNDEFINED,
    JsCall,
    JsResult,
    function_source,
    listener_source,
    object_literal_source,
    require_node,
    run_functions,
    strip_comments,
)


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


_WIRING = """
function wire(button, form) {
  button.addEventListener("click", function () {
    send("a}");
  });
  form.addEventListener("input", function (event) {
    if (event) { remember(); }
  });
  form.addEventListener("submit", function () { send(); });
}
function other(button) { button.addEventListener("click", function () { other(); }); }
"""


def test_listener_source_returns_only_the_callback_that_was_asked_for() -> None:
    assert listener_source(_WIRING, "wire", "button", "click") == (
        'button.addEventListener("click", function () { send("a}"); }'
    )
    assert listener_source(_WIRING, "wire", "form", "input") == (
        'form.addEventListener("input", function (event) { if (event) { remember(); } }'
    )
    assert "remember" not in listener_source(_WIRING, "wire", "form", "submit")


def test_listener_source_looks_only_inside_the_named_function() -> None:
    assert "other()" in listener_source(_WIRING, "other", "button", "click")
    assert "other()" not in listener_source(_WIRING, "wire", "button", "click")


def test_listener_source_raises_when_the_listener_is_missing() -> None:
    with pytest.raises(AssertionError, match="no keydown listener on button in function wire"):
        listener_source(_WIRING, "wire", "button", "keydown")


_FIND_NODE = "dashboard_js.find_node"
_NO_NODE = "node is not on PATH"
_NODE_PATH = "/usr/local/bin/node"


def test_require_node_returns_the_path_of_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FIND_NODE, lambda: _NODE_PATH)

    assert require_node() == _NODE_PATH


def test_require_node_fails_in_ci_when_node_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FIND_NODE, lambda: None)
    monkeypatch.setenv("CI", "true")

    with pytest.raises(pytest.fail.Exception, match=_NO_NODE):
        require_node()


def test_require_node_skips_outside_ci_when_node_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FIND_NODE, lambda: None)
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(pytest.skip.Exception, match=_NO_NODE):
        require_node()


_PAGE = """
(function () {
  "use strict";
  const GREETING = "hello; {world}";
  const unused = document.getElementById("missing").value;
  function shout(name) {
    return name.toUpperCase() + "!";
  }
  function greet(name) {
    return GREETING + " " + shout(name) + " " + obj.shout;
  }
  function boom() {
    throw new Error("broken helper");
  }
  function dropQuery() {
    globalThis.history.replaceState(null, "", globalThis.location.pathname);
  }
  function makeNode() {
    const node = document.createElement("p");
    node.textContent = "text";
    return node;
  }
  function kindOf(value) {
    return typeof value + ":" + String(value);
  }
  function innerKind(value) {
    return kindOf(value.inner[0]);
  }
  function echo(value) {
    return value;
  }
  function nothing() {}
  function wrap(value) {
    return { inner: value, list: [value] };
  }
  const obj = { shout: 1 };
  start();
})();
"""


def test_run_functions_loads_the_constants_and_helpers_a_function_names() -> None:
    results = run_functions(_PAGE, [JsCall("greet", ("ann",))])

    assert results == [JsResult("greet", "hello; {world} ANN! 1", [])]


def test_run_functions_runs_many_calls_in_order_in_one_process() -> None:
    calls = [JsCall("shout", ("a",)), JsCall("shout", ("b",)), JsCall("greet", ("c",))]

    values = [result.value for result in run_functions(_PAGE, calls)]

    assert values == ["A!", "B!", "hello; {world} C! 1"]


def test_run_functions_gives_a_helper_a_plain_object_for_each_element() -> None:
    results = run_functions(_PAGE, [JsCall("makeNode")])

    assert results[0].value == {"tagName": "p", "className": "", "textContent": "text"}


def test_run_functions_reports_the_address_writes_of_each_call() -> None:
    location = {"pathname": "/page"}

    results = run_functions(_PAGE, [JsCall("dropQuery", (), location), JsCall("shout", ("a",))])

    assert [result.history for result in results] == [[[None, "", "/page"]], []]


def test_run_functions_returns_the_other_cases_when_one_helper_throws() -> None:
    calls = [JsCall("shout", ("a",)), JsCall("boom"), JsCall("shout", ("b",))]

    results = run_functions(_PAGE, calls)

    assert [result.error for result in results] == [None, "Error: broken helper", None]
    assert [results[0].value, results[2].value] == ["A!", "B!"]


def test_reading_the_value_of_a_case_that_threw_raises() -> None:
    [result] = run_functions(_PAGE, [JsCall("boom")])

    with pytest.raises(AssertionError, match="boom threw in node: Error: broken helper"):
        _ = result.value


def test_run_functions_raises_when_the_script_does_not_define_the_helper() -> None:
    with pytest.raises(AssertionError, match="absent is not a top-level definition"):
        run_functions(_PAGE, [JsCall("absent")])


def test_run_functions_raises_when_node_fails_to_load_the_script() -> None:
    js = "  const crash = null.value;\n  function broken() { return crash; }"

    with pytest.raises(AssertionError, match="node failed"):
        run_functions(js, [JsCall("broken")])


def test_run_functions_fails_in_ci_when_node_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FIND_NODE, lambda: None)
    monkeypatch.setenv("CI", "true")

    with pytest.raises(pytest.fail.Exception, match=_NO_NODE):
        run_functions(_PAGE, [JsCall("shout", ("a",))])


def test_run_functions_sends_undefined_nan_and_infinity_as_arguments() -> None:
    values = [math.nan, math.inf, -math.inf, None, UNDEFINED]

    results = run_functions(_PAGE, [JsCall("kindOf", (value,)) for value in values])

    assert [result.value for result in results] == [
        "number:NaN",
        "number:Infinity",
        "number:-Infinity",
        "object:null",
        "undefined:undefined",
    ]


def test_run_functions_sends_non_json_values_nested_in_arguments() -> None:
    results = run_functions(_PAGE, [JsCall("innerKind", ({"inner": [math.inf]},))])

    assert results[0].value == "number:Infinity"


def test_run_functions_returns_undefined_nan_and_infinity() -> None:
    calls = [
        JsCall("echo", (value,)) for value in (math.nan, math.inf, -math.inf, None, UNDEFINED)
    ] + [JsCall("nothing")]

    values = [result.value for result in run_functions(_PAGE, calls)]

    assert [repr(value) for value in values[:3]] == ["nan", "inf", "-inf"]
    assert values[3:] == [None, UNDEFINED, UNDEFINED]


def test_run_functions_returns_undefined_nested_in_a_result() -> None:
    results = run_functions(_PAGE, [JsCall("wrap", (UNDEFINED,))])

    assert results[0].value == {"inner": UNDEFINED, "list": [UNDEFINED]}
