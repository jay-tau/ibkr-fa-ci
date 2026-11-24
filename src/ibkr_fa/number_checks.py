"""Read-time digit limits on the numbers an input holds (#1275).

Readers pass each number they take through :func:`number_limit_breach`,
right after their own shape, finite and sign checks. Robinhood, NetBenefits,
Fidelity CMA statements, ``user.toml`` decimal fields and the CLI USD flag
use it since #1275; IBKR, StockPlan, TTBR and price readers since #1276.
Each kind of number has two limits, digits before the decimal point
and digits after it, set well above anything a real input prints, so an
absurd number refuses when it's read, with one fixed message. The exact
arithmetic downstream keeps its 1,000-digit cap
(:data:`ibkr_fa.compute.exact_inr.EXACT_SUM_DIGITS`) as the backstop for
derived values.

This module imports nothing from :mod:`ibkr_fa`, so every layer can use it.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum


class NumberKind(Enum):
    """A kind of number an input holds, with its noun and its two limits:
    digits before the decimal point and digits after it."""

    # Shares, fund units, contracts, shares lent.
    QUANTITY = ("a quantity", 12, 20)
    # USD, or INR in ``user.toml``.
    MONEY = ("a money amount", 15, 20)
    # Also option strikes, dividends per share, fetched closes and highs.
    PRICE = ("a unit price", 9, 20)
    # A rate or a percent.
    RATE = ("a rate", 3, 6)
    # A TTBR.
    FX_RATE = ("an FX rate", 3, 4)

    def __init__(self, noun: str, whole_digits: int, decimal_places: int) -> None:
        self.whole_digits = whole_digits
        self.decimal_places = decimal_places
        # The one fixed refusal text for this kind; it names no value.
        self.refusal = (
            f"{noun} may have at most {whole_digits} digits before the decimal point "
            f"and {decimal_places} after it"
        )


def number_limit_breach(value: Decimal, kind: NumberKind) -> str | None:
    """``kind.refusal`` when ``value`` has more digits than ``kind`` allows,
    else ``None``.

    Both counts come from the parsed value as it stands, sign ignored: its
    whole digits are ``adjusted() + 1`` (``1E+30`` has 31, ``0E+5`` has 6)
    and its decimal places ``-exponent`` (``1.000`` has 3, ``1E-61`` has 61).
    Nothing is rounded, normalised or computed. A non-finite value never
    fits.
    """
    exponent = value.as_tuple().exponent
    if (
        isinstance(exponent, int)
        and value.adjusted() < kind.whole_digits
        and -exponent <= kind.decimal_places
    ):
        return None
    return kind.refusal
