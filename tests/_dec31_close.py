"""The NetBenefits Dec-31 closing-price resolver's fixed refusal texts, the
synthetic markers no refusal may echo, and the check that they don't, which
several test modules share.
"""

from __future__ import annotations

from tests._crash_reports import assert_prints_no_marker

# The resolver's complete fixed session refusals (#1165). Only the validated
# reporting year varies, in the unsupported-year message.
MARKET_SESSION_REFUSAL = (
    "Dec-31 market close: the price series has no usable Close for the required US market session"
)
OVERRIDE_SESSION_REFUSAL = "Dec-31 price override: price_date is not the required US market session"


def unsupported_year_refusal(year: int) -> str:
    return f"Dec-31 per-share pricing, field calendar_year: calendar year {year} is unsupported"


# Distinct synthetic markers (#1165): none may appear in a refusal, its
# cause/context chain or captured stderr.
MARKER_SYMBOL = "QZ01IDENT"
MARKER_DESCRIPTION = "synthetic-name-01"
MARKER_PRICE = "137.2468"
MARKER_SOURCE = "synthetic-source-01"
MARKER_REASON = "synthetic-reason-01"
MARKER_SOURCE_DATE = "2025-12-30"
# The resolver has no description field; caller tests cover that marker.


def assert_markers_absent(exc: BaseException, *markers: str) -> None:
    """No marker anywhere in ``exc`` or any link of its chain, suppressed or
    not (:func:`assert_prints_no_marker`)."""
    assert_prints_no_marker(exc, markers=markers)


def assert_absent(message: str, *values: str) -> None:
    for value in values:
        assert value not in message, value
