"""Check Fidelity CMA monthly statements against the year-end report."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.normalize.form_1042s import Form1042SAmountError, whole_cents
from ibkr_fa.parser.fidelity_cma import (
    CmaIncomeKind,
    CmaStatement,
    CmaYearEndIncomeSummary,
)


class CmaYearEndError(IbkrFaError):
    """The monthly statements and the year-end report do not agree."""


def to_cents(value: Decimal, what: str) -> int:
    """Return a non-negative, bounded, whole-cent USD amount as integer cents."""
    try:
        return whole_cents(value)
    except Form1042SAmountError as exc:
        raise CmaYearEndError(f"{what} is not a bounded whole-cent USD amount") from exc


def require_year_end_income_match(
    statements: Iterable[CmaStatement],
    year_end: CmaYearEndIncomeSummary,
    *,
    tax_year: int,
) -> None:
    """Require the tax year's statement income totals to equal the year-end report.

    Every row is converted to whole cents before it is summed. Withholding
    is compared as magnitudes, after checking that every statement row is
    negative and the year-end total is not positive.
    """
    if year_end.tax_year != tax_year:
        raise CmaYearEndError("year-end report is for a different tax year")
    year_end_withholding = to_cents(year_end.withholding_usd.copy_abs(), "year-end withholding")
    if year_end.withholding_usd > 0:
        raise CmaYearEndError("year-end withholding must not be positive")
    dividends = 0
    interest = 0
    withholding = 0
    for statement in statements:
        for credit in statement.income_credits:
            if credit.date.year != tax_year:
                continue
            if credit.kind is CmaIncomeKind.SPAXX_DIVIDEND:
                dividends += to_cents(credit.amount_usd, "statement dividend")
            elif credit.kind is CmaIncomeKind.CREDIT_INTEREST:
                interest += to_cents(credit.amount_usd, "statement interest")
            else:
                raise CmaYearEndError("unsupported CMA income classification")
        for item in statement.withholdings:
            if item.date.year != tax_year:
                continue
            cents = to_cents(item.amount_usd.copy_abs(), "statement withholding")
            if item.amount_usd >= 0:
                raise CmaYearEndError("statement withholding must be negative")
            withholding += cents
    if (
        interest != to_cents(year_end.interest_usd, "year-end interest")
        or dividends != to_cents(year_end.dividends_usd, "year-end dividends")
        or withholding != year_end_withholding
    ):
        raise CmaYearEndError("monthly statement totals do not match the year-end report")


def require_year_end_match(
    statements: Iterable[CmaStatement],
    december: CmaStatement,
    year_end: CmaYearEndIncomeSummary | None,
    *,
    calendar_year: int,
) -> None:
    """Require year-end evidence that matches the December statement's
    account and holdings, and the calendar year's statement income."""
    if year_end is None or year_end.position is None or year_end.tax_year != calendar_year:
        raise CmaYearEndError("matching CMA year-end holdings evidence is missing")
    if (
        year_end.account_id != december.account_id
        or year_end.account_holder != december.account_holder
    ):
        raise CmaYearEndError("CMA year-end account identity does not match the monthly statements")
    position = year_end.position
    if (
        position.quantity != december.position.quantity
        or position.price_per_unit_usd != december.position.price_per_unit_usd
        or position.market_value_usd != december.position.ending_market_value_usd
    ):
        raise CmaYearEndError("CMA year-end holdings do not match the December monthly statement")
    require_year_end_income_match(statements, year_end, tax_year=calendar_year)
