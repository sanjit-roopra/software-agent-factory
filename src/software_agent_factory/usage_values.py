"""Runtime-neutral validation of numeric usage fields an agent CLI reports.

Shared by :mod:`~software_agent_factory.copilot_runtime` and
:mod:`~software_agent_factory.pi_runtime` so both accept and reject the same
values. A rejected value is treated as "not reported" (``None``), never
coerced, because :class:`~software_agent_factory.models.UsageMetrics` and
:class:`~software_agent_factory.models.ModelUsage` refuse negative values.
"""

from __future__ import annotations

import math


def non_negative_int(value: object) -> int | None:
    """Return ``value`` as an ``int`` when it is a whole, finite, non-negative number.

    ``bool`` is excluded even though it is an ``int`` subclass. A float is
    accepted only when it has no fractional part (``7.0`` becomes ``7``); a
    token count of ``10.5`` is malformed, not rounded.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def non_negative_float(value: object) -> float | None:
    """Return ``value`` as a ``float`` when it is a finite, non-negative number.

    An ``int`` too large to fit a ``float`` is rejected like any other
    non-finite value instead of raising ``OverflowError``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None
