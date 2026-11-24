"""Tests for the FxPolicy Protocol."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

from ibkr_fa.fx import FxPolicy


class _FixedRatePolicy:
    """Minimal FxPolicy implementation used as a structural-typing fixture."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rate: Decimal) -> None:
        self._rate = rate

    def rate_for(self, event_date: date) -> Decimal:
        del event_date  # Always returns the fixed rate.
        return self._rate


def test_protocol_accepts_minimal_implementation() -> None:
    """Anything with `currency: ClassVar[str]` and `rate_for(date) -> Decimal`
    structurally satisfies FxPolicy — no inheritance required."""
    policy: FxPolicy = _FixedRatePolicy(Decimal("83.40"))
    assert policy.currency == "USD"
    assert policy.rate_for(date(2026, 12, 31)) == Decimal("83.40")


def test_rate_for_is_deterministic() -> None:
    policy = _FixedRatePolicy(Decimal("83.40"))
    assert policy.rate_for(date(2026, 1, 1)) == policy.rate_for(date(2026, 1, 1))
