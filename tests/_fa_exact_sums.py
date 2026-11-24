"""Shared values for the exact Schedule FA sum tests (#1168).

Owner decision (2026-11-10, on #1166): these sums never round. They add in a
copy of the active context widened to at least ``EXACT_SUM_DIGITS`` and refuse
only a nonfinite amount, a decimal arithmetic failure or an addition that would
still round at that precision.

All values are synthetic. ``SIX`` (``LONG`` then five ``TINY``) totals exactly
1.50, so ₹2 at ₹1/$; the 28-digit context keeps ``LONG`` and files ₹1.
``HUGE`` plus ``MINUTE`` needs more than ``EXACT_SUM_DIGITS`` digits.
"""

from __future__ import annotations

from datetime import date
from typing import Final

from ibkr_fa.exceptions import InvariantError
from tests._crash_reports import render_without_frames
from tests._exact_refusals import INEXACT_SUM as EXACT

LONG: Final = "1.499999999999999999999999998"
TINY: Final = "4E-28"
SIX: Final = (LONG, *(TINY,) * 5)
HUGE: Final = "1E+10"
MINUTE: Final = "1E-995"

# Privacy markers: each may sit on an input, and none may reach a refusal.
SYMBOL: Final = "SYNTHFA2C"
ISIN: Final = "US99999Q2001"
DESCRIPTION: Final = "SYNTHDESC2C"
SOURCE: Final = "SYNTHSOURCE2C"
MARKED_DATE: Final = date(2025, 6, 17)
NUMERIC: Final = "1.2345678901234567890123456789"
MARKERS: Final = (SYMBOL, ISIN, DESCRIPTION, SOURCE, MARKED_DATE.isoformat(), NUMERIC)


def message(at: str, *, source_row_index: int | None = None) -> str:
    """The full rendered refusal for the sum located by ``at``."""
    suffix = (
        "" if source_row_index is None else f" (context keys: source_row_index={source_row_index})"
    )
    return f"InvariantError: {at}: {EXACT}{suffix}"


def assert_refusal(error: InvariantError, expected: str, *, markers: tuple[str, ...]) -> None:
    """``error`` renders exactly ``expected``, chains nothing and names none of ``markers``."""
    assert str(error) == expected
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    for marker in markers:
        assert marker not in rendered, marker
