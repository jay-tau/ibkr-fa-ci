"""Values a model's finite-Decimal amount check refuses (#936)."""

from __future__ import annotations

from decimal import Decimal

NOT_A_FINITE_DECIMAL: tuple[object, ...] = (
    Decimal("NaN"),
    Decimal("sNaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
    1,
    1.0,
    "1",
)
"""Non-finite Decimals and values that aren't a Decimal. ``None`` is left to
each test, because an optional amount accepts it."""
