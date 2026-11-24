"""Form-level Form 1042-S checks shared by every issuer's reconciler.

Each issuer keeps its own rules (expected records, Box 6, per-code rates)
and its own matching of source events to form buckets.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from enum import StrEnum
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models.form_1042s import Form1042SAmount, Form1042SCopy

_MAX_CENT_DIGITS: Final[int] = 16
_CENT_EXPONENT: Final[int] = -2
_CENTS_PER_DOLLAR: Final[int] = 100
_ROUND_UP_CENTS: Final[int] = 50


class Form1042SAmountError(IbkrFaError):
    """A source amount is not a bounded, non-negative, whole-cent USD value."""


class Form1042SViolation(StrEnum):
    """A form-level check that one Form 1042-S copy fails."""

    GROSS_INCOME = "box-2-not-whole-dollars"
    FEDERAL_TAX_WITHHELD = "box-7a-not-whole-dollars"
    TOTAL_WITHHOLDING_CREDIT = "box-10-not-whole-dollars"
    CREDIT_DIFFERS_FROM_WITHHELD = "box-10-differs-from-box-7a"
    WITHHOLDING_ALLOWANCE = "box-5-nonzero"
    TAX_WITHHELD_OTHER_AGENTS = "box-8-nonzero"
    OVERWITHHELD_TAX_REPAID = "box-9-nonzero"
    TAX_PAID_BY_AGENT = "box-11-nonzero"
    CHAPTER4_TAX_RATE = "box-4b-nonzero"
    AMENDED = "amended"
    CHAPTER_INDICATOR = "box-3-not-chapter-3"
    FEDERAL_TAX_NOT_DEPOSITED = "box-7b-checked"
    SUBSEQUENT_YEAR_WITHHOLDING = "box-7c-checked"
    QUALIFIED_INTERMEDIARY = "box-7d-checked"
    PRO_RATA_REPORTING = "box-15-checked"


def _is_whole_dollars(amount: Form1042SAmount) -> bool:
    value = amount.value
    return value is not None and value == value.to_integral_value()


def _is_zero_or_blank(amount: Form1042SAmount) -> bool:
    return amount.value is None or amount.value == 0


def form_1042s_violations(copy: Form1042SCopy) -> tuple[Form1042SViolation, ...]:
    """Return every form-level check ``copy`` fails; ``()`` means it passes.

    Boxes 2, 7a and 10 must be printed whole dollars with Box 10 equal to
    Box 7a, Boxes 5, 8, 9 and 11 must be zero or blank, and the chapter 4
    rate must be zero. Amendment, the Box 3 chapter indicator, Boxes 7b-7d
    and the Box 15 pro-rata check are tested only when the issuer's parser
    read them (not ``None``).
    """
    checks = (
        (Form1042SViolation.GROSS_INCOME, not _is_whole_dollars(copy.gross_income)),
        (Form1042SViolation.FEDERAL_TAX_WITHHELD, not _is_whole_dollars(copy.federal_tax_withheld)),
        (
            Form1042SViolation.TOTAL_WITHHOLDING_CREDIT,
            not _is_whole_dollars(copy.total_withholding_credit),
        ),
        (
            Form1042SViolation.CREDIT_DIFFERS_FROM_WITHHELD,
            copy.total_withholding_credit.value != copy.federal_tax_withheld.value,
        ),
        (
            Form1042SViolation.WITHHOLDING_ALLOWANCE,
            not _is_zero_or_blank(copy.withholding_allowance),
        ),
        (
            Form1042SViolation.TAX_WITHHELD_OTHER_AGENTS,
            not _is_zero_or_blank(copy.tax_withheld_other_agents),
        ),
        (
            Form1042SViolation.OVERWITHHELD_TAX_REPAID,
            not _is_zero_or_blank(copy.overwithheld_tax_repaid),
        ),
        (Form1042SViolation.TAX_PAID_BY_AGENT, not _is_zero_or_blank(copy.tax_paid_by_agent)),
        (
            Form1042SViolation.CHAPTER4_TAX_RATE,
            not copy.chapter4_tax_rate_percent.is_finite() or copy.chapter4_tax_rate_percent != 0,
        ),
        (Form1042SViolation.AMENDED, copy.amended or copy.amendment_number is not None),
        (Form1042SViolation.CHAPTER_INDICATOR, copy.chapter_indicator not in {None, "3"}),
        (Form1042SViolation.FEDERAL_TAX_NOT_DEPOSITED, copy.federal_tax_not_deposited is True),
        (Form1042SViolation.SUBSEQUENT_YEAR_WITHHOLDING, copy.subsequent_year_withholding is True),
        (Form1042SViolation.QUALIFIED_INTERMEDIARY, copy.qualified_intermediary is True),
        (Form1042SViolation.PRO_RATA_REPORTING, copy.pro_rata_reporting is True),
    )
    return tuple(violation for violation, failed in checks if failed)


def whole_cents(value: Decimal) -> int:
    """Return a non-negative, bounded, whole-cent USD amount as integer cents.

    More than two decimal places is refused even when the extra places are
    zeros (``1.000``), as is anything over 16 cent digits.
    """
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if (
        not value.is_finite()
        or decimal_tuple.sign
        or not isinstance(exponent, int)
        or exponent < _CENT_EXPONENT
    ):
        raise Form1042SAmountError("amount is not a bounded whole-cent USD amount")
    shift = exponent - _CENT_EXPONENT
    if len(decimal_tuple.digits) + shift > _MAX_CENT_DIGITS:
        raise Form1042SAmountError("amount is not a bounded whole-cent USD amount")
    coefficient = 0
    for digit in decimal_tuple.digits:
        coefficient = coefficient * 10 + digit
    return coefficient * (10**shift)


def whole_dollars(amounts: Iterable[Decimal]) -> int:
    """Sum exact source amounts as integer cents, then round half up once.

    IRS Publication 1187 (https://www.irs.gov/pub/irs-pdf/p1187.pdf) has
    Form 1042-S amounts reported in whole dollars. Summing the exact source
    cents first and rounding half up once per printed box is this project's
    reconciliation convention (ADR 0006,
    ``docs/decisions/0006-robinhood-multi-account-evidence.md``).

    Every amount must pass :func:`whole_cents`: an amount with more than two
    decimal places, even ``1.000``, raises :class:`Form1042SAmountError`
    rather than being rounded. Integer arithmetic keeps the result
    independent of the ambient :mod:`decimal` context.
    """
    cents = sum((whole_cents(amount) for amount in amounts), 0)
    dollars, remainder = divmod(cents, _CENTS_PER_DOLLAR)
    return dollars + (remainder >= _ROUND_UP_CENTS)
