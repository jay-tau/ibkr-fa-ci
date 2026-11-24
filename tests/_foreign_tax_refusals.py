"""The foreign tax refusal texts the tests expect, and the check that a
refusal prints none of the withholding rows' values (#1754)."""

from __future__ import annotations

import re
from datetime import date

from ibkr_fa.models.withholding_tax import WithholdingTax

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)

# How UnprovenWhtReversalError names each reversal (#1047).
NO_ORIGINAL = "a reversal with no original"
TWO_FY_CANDIDATES = "a reversal with candidate originals in more than one financial year"


def unproven_reversal_message(
    *named: str, fy_start: date = _FY_START, fy_end: date = _FY_END
) -> str:
    """The whole UnprovenWhtReversalError text naming ``named`` (#1047).

    It names each reversal by its source account index (when set) and why its
    original's year is unproven, never its ISIN, amount, currency or dates.
    """
    return (
        f"withholding-tax reversal(s) in FY {fy_start.isoformat()} → "
        f"{fy_end.isoformat()} cancel tax of an unproven year: "
        + "; ".join(named)
        + ". A reversal pairs only with an equal-and-opposite withholding row "
        "of the same source account, ISIN and currency on or before its date. "
        "With none, or with candidates in more than one financial year, the "
        "year of the tax it cancels is unknown, so the run refuses rather than "
        "guess. If the statement starts after the original withholding, use "
        "one that covers it."
    )


def orphan_wht_message(count: int, *, fy_start: date = _FY_START, fy_end: date = _FY_END) -> str:
    """The whole :func:`require_no_orphan_wht` text for ``count`` orphan events.

    It counts them and prints the FY bounds, never their ISINs, dates or
    source accounts (#1047).
    """
    return (
        "Schedule FSI: foreign withholding tax was deducted on "
        f"{count} (ISIN, payment_date) event(s) in FY {fy_start.isoformat()} → "
        f"{fy_end.isoformat()}, but no matching in-FY foreign-source ordinary "
        "dividend with the same ISIN AND payment_date (and source account, when "
        "set) was found in the Schedule OS foreign-dividend aggregation "
        "(`os_result.lines` — foreign-source ordinary dividends only; "
        "Indian-source dividends route to `domestic_lines`). Per §90/§91 + Rule "
        "128(5), the foreign tax credit must be claimed in the FY in which the "
        "underlying foreign-income event is offered to Indian tax. The FSI WHT "
        "aggregator keys layers on (country, OTHER_SOURCES) — even within a "
        "country, an orphan WHT can be silently absorbed by an unrelated "
        "same-country dividend / interest row, inflating the credit claim "
        "incorrectly. This check requires a paid (negative-amount) WHT row to "
        "carry the same payment_date as its underlying dividend; a paid row "
        "with no in-FY dividend on that date is treated as orphaned (typically "
        "a late adjustment for a prior-FY dividend). Positive-amount (reversal) "
        "WHT rows carry the reversal booking date and are skipped — they reduce "
        "credit, not inflate it. This version can't carry withholding booked in "
        "this FY back to a dividend paid in an earlier one, and a run for that "
        "earlier FY reads only withholding booked inside it, so it would leave "
        "this withholding out without a warning. Claim its credit by hand in the "
        "return for the FY in which the dividend was offered to tax, and complete "
        "what this command would have written for this FY by hand. A follow-up issue "
        "tracks tagging the WHT model with source-kind so the layering can be "
        "made source-aware."
    )


def stated_country_message(*lines: int) -> str:
    """The whole StatedTaxCountryMismatchError text naming ``lines`` (#1282).

    It names each row by its statement line, never its symbol, ISIN,
    countries, date or amount.
    """
    return (
        "the Withholding Tax row(s) on statement line(s) "
        + ", ".join(str(line) for line in lines)
        + " state a tax country (the description's ' - XX Tax' suffix) other "
        "than the country of the row's ISIN. Foreign tax is filed and credited "
        "under the ISIN's country, so the run refuses rather than credit tax the "
        "statement says another country took."
    )


def assert_no_wht_echo(message: str, rows: list[WithholdingTax]) -> None:
    """``message`` prints no row's ISIN, payment date, amount or currency (#1047).

    Compared case-insensitively; the currency as a whole token.
    """
    for row in rows:
        for value in (row.isin, row.payment_date.isoformat(), str(abs(row.amount))):
            assert value.lower() not in message.lower(), value
        assert re.search(rf"(?i)\b{re.escape(row.currency)}\b", message) is None
