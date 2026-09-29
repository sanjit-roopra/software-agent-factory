from __future__ import annotations

import pytest

from software_agent_factory.usage_values import non_negative_float, non_negative_int


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (7, 7), (7.0, 7), (0.0, 0)],
)
def test_non_negative_int_accepts_whole_non_negative_numbers(value: object, expected: int) -> None:
    assert non_negative_int(value) == expected


@pytest.mark.parametrize(
    "value",
    [-1, -0.5, 10.5, float("nan"), float("inf"), True, False, "5", None, [1], {"a": 1}],
)
def test_non_negative_int_rejects_everything_else(value: object) -> None:
    assert non_negative_int(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, 0.0), (3, 3.0), (0.25, 0.25)])
def test_non_negative_float_accepts_finite_non_negative_numbers(
    value: object, expected: float
) -> None:
    result = non_negative_float(value)

    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize(
    "value", [-1, -0.01, float("nan"), float("inf"), float("-inf"), True, "1.5", None]
)
def test_non_negative_float_rejects_everything_else(value: object) -> None:
    assert non_negative_float(value) is None
