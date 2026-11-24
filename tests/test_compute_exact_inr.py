"""Exact Schedule OS and FSI income conversions and sums (#1136), exact shares (#1241),
and their review amounts and TTBR products (#1243).

All values are synthetic. ``_LONG`` plus any ``_TINY`` needs 29 digits.
For ``_SIX`` (``_LONG`` plus five ``_TINY`` values), the default 28-digit
context files ₹1 instead of the exact total's ₹2. Since #1312 the sums and
conversions run at ``EXACT_SUM_DIGITS`` digits, so they keep every digit up
to that cap. Past it they still refuse: ``_PAST_CAP`` (1 and ``_CAP_TINY``)
adds to 1,001 digits, and ``_CAP_LONG`` x 80 to 1,001 ending in a non-zero
digit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import (
    ROUND_CEILING,
    Context,
    Decimal,
    Inexact,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    getcontext,
    localcontext,
)
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ibkr_fa.compute.exact_inr import (
    EXACT_SUM_DIGITS,
    checked_exact_product,
    exact_decimal,
    exact_fraction,
    exact_inr_conversion,
    exact_inr_sum,
    exact_share,
    exact_signed_inr_sum,
    exact_whole_rupees,
    is_exact_decimal,
    review_amount,
)
from ibkr_fa.compute.income_engine import _fsi_relief_other_income_by_country
from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    _merge_results,
    compute_schedule_fsi_from_dividends,
    compute_schedule_fsi_from_interest,
    compute_schedule_fsi_from_other_income,
    compute_schedule_fsi_from_substitute_dividends,
)
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    compute_schedule_os_dividend_lines,
)
from ibkr_fa.compute.schedule_os_interest import (
    ScheduleOsInterestComputeResult,
    ScheduleOsInterestLine,
    compute_schedule_os_interest_lines,
)
from ibkr_fa.compute.schedule_os_other_income import (
    ScheduleOsOtherIncomeComputeResult,
    ScheduleOsOtherIncomeLine,
    compute_schedule_os_other_income_lines,
    grouped_other_income_totals,
)
from ibkr_fa.compute.schedule_os_substitute_dividend import (
    ScheduleOsSubstituteDividendComputeResult,
    ScheduleOsSubstituteDividendLine,
    compute_schedule_os_substitute_dividend_lines,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import (
    DistributionKind,
    Dividend,
    InterestCredit,
    OtherSourceIncome,
    SubstituteDividendIncome,
)
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM
from tests._fx_stubs import AuditedFxStub

_DESCRIPTION = "SYNTHDESC-2290"
_NATURE = "Synthetic nature QX"
_ISIN = "XS999900QX04"
_LONG = "1.499999999999999999999999998"
_TINY = "4E-28"
_SIX = (Decimal(_LONG), *(Decimal(_TINY),) * 5)
# ``_SIX`` added exactly, in either order, keeps the trailing zero: 29 digits.
_EXACT_SIX = Decimal("1.5000000000000000000000000000")
# x 80 needs 30 digits; 28 round it to 1.500...: ₹2 instead of the exact ₹1.
_INEXACT = "0.018749999999999999999999999999"
_INEXACT_INR = Decimal("1.499999999999999999999999999920")
# x 80 drops only trailing zeros: exactly _LONG.
_ZEROS_ONLY = "0.018749999999999999999999999975"
_RATE = "80"
# 1 plus this needs 1,001 digits, past ``EXACT_SUM_DIGITS``.
_CAP_TINY = "1E-1000"
_PAST_CAP = (Decimal("1"), Decimal(_CAP_TINY))
# 1,000 nines: x 80 is 79.99...92, 1,001 digits ending in a non-zero one.
_CAP_LONG = "0." + "9" * EXACT_SUM_DIGITS
_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)
_PAID = date(2025, 6, 2)

_SUM = f"INR {INEXACT_SUM}"
_CONVERSION = (
    "INR amount (column Amount \N{MULTIPLICATION SIGN} FX rate) must be exact within "
    "Decimal precision"
)


def _refusal(raised: pytest.ExceptionInfo[InvariantError], expected: str, *inputs: str) -> None:
    message = str(raised.value)
    for value in inputs:
        assert value not in message, value
    assert message == f"InvariantError: {expected}"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _fx(rate: str) -> AuditedFxStub:
    return AuditedFxStub(Decimal(rate), specified_date=_FY_END)


def _credit(amount: Decimal | str, row: int | None = None) -> InterestCredit:
    return InterestCredit(
        credit_date=_PAID,
        amount_gross=Decimal(amount),
        currency="USD",
        description=_DESCRIPTION,
        source_row_index=row,
    )


def _other(amount: Decimal | str, row: int | None = None, nature: str = _NATURE):
    return OtherSourceIncome(
        nature=nature,
        payment_date=_PAID,
        amount_gross=Decimal(amount),
        currency="USD",
        description=_DESCRIPTION,
        source_row_index=row,
        source_country_iso2=CountryCode("US"),
    )


def _substitute(amount: str, row: int | None = None) -> SubstituteDividendIncome:
    return SubstituteDividendIncome(
        symbol=Ticker("XQSB"),
        isin=Isin(_ISIN),
        issuer_country_iso2=CountryCode("XS"),
        source_country_iso2=CountryCode("US"),
        payment_date=_PAID,
        amount_gross=Decimal(amount),
        currency="USD",
        description=_DESCRIPTION,
        source_row_index=row,
    )


def _dividend(amount: str, row: int | None = None, isin: str = _ISIN) -> Dividend:
    return Dividend(
        symbol=Ticker("XQSB"),
        isin=Isin(isin),
        payment_date=_PAID,
        amount_gross=Decimal(amount),
        currency="USD",
        description=_DESCRIPTION,
        source_row_index=row,
        kind=DistributionKind.CASH_DIVIDEND,
        type_slice="Cash Dividend USD 0.25 per Share",
    )


# ---- totals ----------------------------------------------------------------


def _interest(amounts: Sequence[Decimal | str], rate: str = "1") -> ScheduleOsInterestComputeResult:
    return compute_schedule_os_interest_lines(
        [_credit(amount) for amount in amounts],
        fx_policy=_fx(rate),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


def _other_income(amounts: Sequence[Decimal]) -> ScheduleOsOtherIncomeComputeResult:
    return compute_schedule_os_other_income_lines(
        [_other(amount) for amount in amounts],
        fx_policy=_fx("1"),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_interest_total_keeps_every_digit(reverse: bool) -> None:
    """#1312: ``_SIX`` totals exactly 1.50 in either order and files ₹2; main refused it."""
    result = _interest(_SIX[::-1] if reverse else _SIX)
    assert result.total_inr_full.compare_total(_EXACT_SIX) == 0
    assert result.total_inr_rounded == Decimal("2")


def test_interest_total_of_six_exact_products_keeps_every_digit() -> None:
    """#1136's six credits: since #1312 each product keeps its trailing zeros, and their
    sum is exactly 1.50 with all of them, filed as ₹2; main refused the sum."""
    result = _interest([_ZEROS_ONLY, *("5E-30" for _ in range(5))], rate=_RATE)
    assert result.total_inr_full.compare_total(Decimal("1.500000000000000000000000000000")) == 0
    assert result.total_inr_rounded == Decimal("2")


@pytest.mark.parametrize(
    ("rate", "markers"),
    [
        pytest.param("1", (_DESCRIPTION, _CAP_TINY), id="amounts"),
        pytest.param(_RATE, (_DESCRIPTION, _CAP_TINY, _RATE), id="products"),
    ],
)
def test_interest_total_past_the_digit_cap_refuses(rate: str, markers: tuple[str, ...]) -> None:
    """#1312: a total that needs 1,001 digits still refuses, with no input value."""
    with pytest.raises(InvariantError) as raised:
        _interest(_PAST_CAP, rate=rate)
    _refusal(raised, f"Schedule OS 1b(ix) interest {_SUM}", *markers)


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_other_income_nature_total_keeps_every_digit(reverse: bool) -> None:
    """#1312: one nature's ``_SIX`` totals exactly 1.50 in either order and files ₹2."""
    result = _other_income(_SIX[::-1] if reverse else _SIX)
    [(nature, total, whole_rupees)] = grouped_other_income_totals(result.lines)
    assert (nature, whole_rupees) == (_NATURE, Decimal("2"))
    assert total.compare_total(_EXACT_SIX) == 0


def test_other_income_nature_total_past_the_digit_cap_refuses() -> None:
    """#1312: a nature total that needs 1,001 digits still refuses, naming no nature."""
    result = _other_income(_PAST_CAP)
    with pytest.raises(InvariantError) as raised:
        grouped_other_income_totals(result.lines)
    _refusal(raised, f"Schedule OS 1e other income {_SUM}", _DESCRIPTION, _NATURE, _CAP_TINY)


def _dividend_line(inr: Decimal) -> ScheduleOsDividendLine:
    return ScheduleOsDividendLine(
        symbol=Ticker("XQSB"),
        isin=Isin("US99972B1017"),
        issuer_country_iso2="US",
        payment_date=_PAID,
        fx_specified_date=date(2025, 5, 31),
        fx_rate_publication_date=date(2025, 5, 31),
        fx_rate_inr_per_usd=Decimal("1"),
        amount_gross_usd=inr,
        amount_gross_inr=inr,
        description=_DESCRIPTION,
        kind=DistributionKind.CASH_DIVIDEND,
    )


def _interest_line(inr: Decimal) -> ScheduleOsInterestLine:
    return ScheduleOsInterestLine(
        credit_date=_PAID,
        fx_specified_date=_FY_END,
        fx_rate_publication_date=_FY_END,
        fx_rate_inr_per_usd=Decimal("1"),
        amount_gross_usd=inr,
        amount_gross_inr=inr,
        description=_DESCRIPTION,
    )


def _substitute_line(inr: Decimal) -> ScheduleOsSubstituteDividendLine:
    return ScheduleOsSubstituteDividendLine(
        symbol=Ticker("XQSB"),
        isin=Isin(_ISIN),
        issuer_country_iso2=CountryCode("XS"),
        source_country_iso2=CountryCode("US"),
        payment_date=_PAID,
        fx_specified_date=_FY_END,
        fx_rate_publication_date=_FY_END,
        fx_rate_inr_per_usd=Decimal("1"),
        amount_gross_usd=inr,
        amount_gross_inr=inr,
        description=_DESCRIPTION,
    )


def _other_line(inr: Decimal, nature: str = _NATURE) -> ScheduleOsOtherIncomeLine:
    return ScheduleOsOtherIncomeLine(
        nature=nature,
        payment_date=_PAID,
        fx_specified_date=_FY_END,
        fx_rate_publication_date=_FY_END,
        fx_rate_inr_per_usd=Decimal("1"),
        amount_gross_usd=inr,
        amount_gross_inr=inr,
        description=_DESCRIPTION,
        source_row_index=None,
        source_country_iso2=CountryCode("US"),
    )


def _fsi_from_dividends(amounts: Sequence[Decimal]) -> ScheduleFsiComputeResult:
    return compute_schedule_fsi_from_dividends(
        ScheduleOsComputeResult(
            lines=[_dividend_line(amount) for amount in amounts],
            domestic_lines=[],
        )
    )


def _fsi_from_interest(amounts: Sequence[Decimal]) -> ScheduleFsiComputeResult:
    return compute_schedule_fsi_from_interest(
        ScheduleOsInterestComputeResult(lines=[_interest_line(amount) for amount in amounts]),
        broker_domicile_iso2="US",
    )


def _fsi_from_substitute_dividends(amounts: Sequence[Decimal]) -> ScheduleFsiComputeResult:
    return compute_schedule_fsi_from_substitute_dividends(
        ScheduleOsSubstituteDividendComputeResult(
            lines=tuple(_substitute_line(amount) for amount in amounts)
        )
    )


def _fsi_from_other_income(amounts: Sequence[Decimal]) -> ScheduleFsiComputeResult:
    return compute_schedule_fsi_from_other_income(
        ScheduleOsOtherIncomeComputeResult(lines=tuple(_other_line(amount) for amount in amounts))
    )


_FSI_AGGREGATIONS = [
    (_fsi_from_dividends, "Schedule FSI dividend income by country"),
    (_fsi_from_interest, "Schedule FSI interest income"),
    (_fsi_from_substitute_dividends, "Schedule FSI substitute dividend income by country"),
    (_fsi_from_other_income, "Schedule FSI other income by country"),
]


@pytest.mark.parametrize(("aggregate", "label"), _FSI_AGGREGATIONS)
def test_fsi_source_aggregation_keeps_every_digit(aggregate, label: str) -> None:
    """#1312: the country's ``_SIX`` income is exactly 1.50, filed as ₹2; main refused it."""
    [row] = aggregate(_SIX).rows
    assert (row.iso_alpha2, row.filed.income_inr, row.event_count) == ("US", Decimal("2"), 6)
    assert row.income_inr.compare_total(_EXACT_SIX) == 0


@pytest.mark.parametrize(("aggregate", "label"), _FSI_AGGREGATIONS)
def test_fsi_source_aggregation_past_the_digit_cap_refuses(aggregate, label: str) -> None:
    """#1312: a country total that needs 1,001 digits still refuses, with no input value."""
    with pytest.raises(InvariantError) as raised:
        aggregate(_PAST_CAP)
    _refusal(raised, f"{label} {_SUM}", _DESCRIPTION, _CAP_TINY)


def test_fsi_relief_other_income_map_keeps_every_digit() -> None:
    """Two natures in one country: each 1e total is exact, and since #1312 so is the
    country's, which needs 29 digits; main refused it."""
    lines = (_other_line(Decimal(_LONG)), _other_line(Decimal(_TINY), nature="Second nature"))
    assert [total for _, total, _ in grouped_other_income_totals(lines)] == [
        Decimal(_TINY),
        Decimal(_LONG),
    ]
    income_by_iso = _fsi_relief_other_income_by_country((), lines)
    assert list(income_by_iso) == ["US"]
    assert income_by_iso["US"].compare_total(Decimal("1.4999999999999999999999999984")) == 0


def test_fsi_relief_other_income_map_past_the_digit_cap_refuses() -> None:
    """#1312: each 1e total is exact, but the country's needs 1,001 digits and still
    refuses, naming no nature."""
    lines = (_other_line(_PAST_CAP[0]), _other_line(_PAST_CAP[1], nature="Second nature"))
    assert [total for _, total, _ in grouped_other_income_totals(lines)] == list(_PAST_CAP[::-1])
    with pytest.raises(InvariantError) as raised:
        _fsi_relief_other_income_by_country((), lines)
    _refusal(
        raised,
        f"Schedule FSI relief other income by country {_SUM}",
        _DESCRIPTION,
        _NATURE,
        "Second nature",
        _CAP_TINY,
    )


def _fsi_row(income: Decimal, foreign_tax: Decimal) -> ScheduleFsiComputeResult:
    return ScheduleFsiComputeResult(
        rows=[
            ScheduleFsiRow(
                cbdt_country_code="2",
                country_name_portal="UNITED STATES OF AMERICA",
                iso_alpha2="US",
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr=income,
                foreign_tax_inr=foreign_tax,
                event_count=1,
            )
        ]
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_fsi_merge_income_keeps_every_digit(reverse: bool) -> None:
    """#1312: six merged incomes total exactly 1.50 in either order, filed as ₹2."""
    amounts = _SIX[::-1] if reverse else _SIX
    [row] = _merge_results(*(_fsi_row(amount, Decimal("0")) for amount in amounts)).rows
    assert (row.filed.income_inr, row.event_count) == (Decimal("2"), 6)
    assert row.income_inr.compare_total(_EXACT_SIX) == 0


def test_fsi_merge_income_past_the_digit_cap_refuses() -> None:
    """#1312: a merged income that needs 1,001 digits still refuses."""
    with pytest.raises(InvariantError) as raised:
        _merge_results(*(_fsi_row(amount, Decimal("0")) for amount in _PAST_CAP))
    _refusal(raised, f"Schedule FSI income by country and head {_SUM}", _CAP_TINY)


@pytest.mark.parametrize("reverse", [False, True], ids=["six", "reversed"])
def test_fsi_merge_foreign_tax_keeps_every_digit(reverse: bool) -> None:
    """#1312: six merged foreign taxes total exactly 1.50 in either order, filed as ₹2."""
    amounts = _SIX[::-1] if reverse else _SIX
    [row] = _merge_results(*(_fsi_row(Decimal("2"), amount) for amount in amounts)).rows
    assert (row.income_inr, row.filed.foreign_tax_inr, row.event_count) == (
        Decimal("12"),
        Decimal("2"),
        6,
    )
    assert row.foreign_tax_inr.compare_total(_EXACT_SIX) == 0


def test_fsi_merge_foreign_tax_past_the_digit_cap_refuses() -> None:
    """#1312: a merged foreign tax that needs 1,001 digits still refuses."""
    with pytest.raises(InvariantError) as raised:
        _merge_results(*(_fsi_row(Decimal("2"), amount) for amount in _PAST_CAP))
    _refusal(raised, f"Schedule FSI foreign tax by country and head {_SUM}", _CAP_TINY)


# ---- conversions -----------------------------------------------------------


def _convert_interest(amount: str, row: int | None) -> Decimal:
    result = compute_schedule_os_interest_lines(
        [_credit(amount, row)],
        fx_policy=_fx(_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    return result.lines[0].amount_gross_inr


def _convert_other_income(amount: str, row: int | None) -> Decimal:
    result = compute_schedule_os_other_income_lines(
        [_other(amount, row)],
        fx_policy=_fx(_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    return result.lines[0].amount_gross_inr


def _convert_substitute_dividend(amount: str, row: int | None) -> Decimal:
    result = compute_schedule_os_substitute_dividend_lines(
        [_substitute(amount, row)],
        fx_policy=_fx(_RATE),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    return result.lines[0].amount_gross_inr


def _convert_dividend(amount: str, row: int | None, isin: str = _ISIN) -> Decimal:
    result = compute_schedule_os_dividend_lines(
        [_dividend(amount, row, isin)],
        fx_policy=AuditedFxStub(Decimal(_RATE)),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
    )
    return result.lines[0].amount_gross_inr


_CONVERSIONS = [
    (_convert_interest, 11, "Schedule OS 1b(ix) interest"),
    (_convert_other_income, 12, "Schedule OS 1e other income"),
    (_convert_substitute_dividend, 13, "Schedule OS 1e other income"),
    (_convert_dividend, 14, "Schedule OS dividend"),
]


def _conversion_markers(convert, amount: str) -> tuple[str, ...]:
    """The input values a refusal of ``convert(amount, row)`` must not name."""
    markers = (_DESCRIPTION, amount, _RATE)
    if convert is _convert_other_income:
        return (*markers, _NATURE)
    if convert in (_convert_substitute_dividend, _convert_dividend):
        return (*markers, _ISIN)
    return markers


@pytest.mark.parametrize(("convert", "row", "label"), _CONVERSIONS)
def test_conversion_keeps_every_digit(convert, row: int, label: str) -> None:
    """#1312: ``_INEXACT`` x 80 keeps all 30 digits, so its HALF_UP is the exact ₹1;
    main refused it."""
    inr = convert(_INEXACT, row)
    assert inr.compare_total(_INEXACT_INR) == 0
    assert to_whole_rupees(inr) == Decimal("1")


@pytest.mark.parametrize(("convert", "row", "label"), _CONVERSIONS)
def test_conversion_past_the_digit_cap_refuses(convert, row: int, label: str) -> None:
    """#1312: a product that would still lose a non-zero digit at 1,000 digits refuses,
    naming its source row and no input value."""
    with pytest.raises(InvariantError) as raised:
        convert(_CAP_LONG, row)
    _refusal(
        raised,
        f"{label} {_CONVERSION} (context keys: source_row_index={row})",
        *_conversion_markers(convert, _CAP_LONG),
    )


def test_dividend_conversion_without_a_source_row_keeps_every_digit() -> None:
    """#1312: without a source row too, the product keeps all 30 digits."""
    assert _convert_dividend(_INEXACT, None).compare_total(_INEXACT_INR) == 0


def test_dividend_conversion_past_the_digit_cap_refuses_without_context() -> None:
    """#1312: past the cap, a dividend without a source row refuses with no context keys."""
    with pytest.raises(InvariantError) as raised:
        _convert_dividend(_CAP_LONG, None)
    _refusal(
        raised,
        f"Schedule OS dividend {_CONVERSION}",
        *_conversion_markers(_convert_dividend, _CAP_LONG),
    )


@pytest.mark.parametrize(("convert", "row", "label"), _CONVERSIONS)
def test_conversion_that_drops_only_zeros_passes(convert, row: int, label: str) -> None:
    kwargs = {"isin": "US99972B1017"} if convert is _convert_dividend else {}
    assert convert(_ZEROS_ONLY, row, **kwargs) == Decimal(_LONG)


@pytest.mark.parametrize(("convert", "row", "label"), _CONVERSIONS)
def test_conversion_past_the_digit_cap_that_drops_only_zeros_passes(
    convert, row: int, label: str
) -> None:
    """#1312: 1,000 digits ending in 5, x 80, needs 1,002 digits, but past the 1,000th
    they are zeros, so the product keeps its exact value; main refused it."""
    amount = "0." + "9" * (EXACT_SUM_DIGITS - 1) + "5"
    with localcontext(Context(prec=2 * EXACT_SUM_DIGITS)):
        exact = Decimal(amount) * Decimal(_RATE)
    assert convert(amount, row) == exact


@pytest.mark.parametrize(("convert", "row", "label"), _CONVERSIONS)
def test_conversion_overflow_refuses(convert, row: int, label: str) -> None:
    with pytest.raises(InvariantError) as raised:
        convert("1E999999", row)
    _refusal(
        raised,
        f"{label} {_CONVERSION} (context keys: source_row_index={row})",
        "1E999999",
        _RATE,
    )


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_conversion_refuses(amount: str) -> None:
    with pytest.raises(InvariantError) as raised:
        exact_inr_conversion(
            Decimal(amount), Decimal(_RATE), label="Schedule OS dividend", source_row_index=None
        )
    _refusal(raised, f"Schedule OS dividend {_CONVERSION}", amount, _RATE)


@pytest.mark.parametrize("sum_inr", [exact_inr_sum, exact_signed_inr_sum])
@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity", "9E999999"])
def test_nonfinite_or_overflowing_sum_refuses(sum_inr, amount: str) -> None:
    with pytest.raises(InvariantError) as raised:
        sum_inr([Decimal(amount), Decimal(amount)], label="Schedule OS dividend")
    _refusal(raised, f"Schedule OS dividend {_SUM}", amount)


# ---- signs -----------------------------------------------------------------


def test_signed_other_income_nature_total_is_unchanged() -> None:
    lines = (_other_line(Decimal("5.00")), _other_line(Decimal("-1.25")))
    assert grouped_other_income_totals(lines) == ((_NATURE, Decimal("3.75"), Decimal("4")),)


def test_signed_other_income_nature_total_keeps_every_digit() -> None:
    """#1312: ``_LONG`` less ``_TINY`` is exactly 1.4999999999999999999999999976, 29 digits,
    filed as ₹1; main refused it."""
    lines = (_other_line(Decimal(_LONG)), _other_line(Decimal("-" + _TINY)))
    [(nature, total, whole_rupees)] = grouped_other_income_totals(lines)
    assert (nature, whole_rupees) == (_NATURE, Decimal("1"))
    assert total.compare_total(Decimal("1.4999999999999999999999999976")) == 0


def test_signed_other_income_nature_total_past_the_digit_cap_refuses() -> None:
    """#1312: 2 less ``_CAP_TINY`` needs 1,001 digits and still refuses, naming no nature."""
    lines = (_other_line(Decimal("2")), _other_line(Decimal("-" + _CAP_TINY)))
    with pytest.raises(InvariantError) as raised:
        grouped_other_income_totals(lines)
    _refusal(raised, f"Schedule OS 1e other income {_SUM}", _DESCRIPTION, _NATURE, _CAP_TINY)


def test_negative_dividend_amount_still_refuses() -> None:
    with pytest.raises(InvariantError) as raised:
        exact_inr_sum([Decimal("1"), Decimal("-0.5")], label="Schedule OS dividend")
    _refusal(raised, "Schedule OS dividend INR amounts must not be negative", "0.5")


# ---- exact shares (#1241) --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("NaN", id="nan"),
        pytest.param("sNaN", id="snan"),
        pytest.param("-Infinity", id="infinity"),
        pytest.param("1" * (EXACT_SUM_DIGITS + 1), id="digits"),
        pytest.param(f"1E-{EXACT_SUM_DIGITS + 1}", id="exponent"),
        pytest.param(f"1E+{EXACT_SUM_DIGITS + 1}", id="adjusted"),
    ],
)
def test_exact_fraction_refuses_nonfinite_or_past_the_digit_cap(value: str) -> None:
    """#1241: a nonfinite value, or one past ``EXACT_SUM_DIGITS`` digits or places, has none."""
    assert exact_fraction(Decimal(value)) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("84.15", Fraction(1683, 20), id="rate"),
        pytest.param("-0.00", Fraction(0), id="negative-zero"),
        pytest.param("1" * EXACT_SUM_DIGITS, Fraction(int("1" * EXACT_SUM_DIGITS)), id="digits"),
        pytest.param(f"1E-{EXACT_SUM_DIGITS}", Fraction(1, 10**EXACT_SUM_DIGITS), id="exponent"),
        pytest.param(f"1E+{EXACT_SUM_DIGITS}", Fraction(10**EXACT_SUM_DIGITS), id="adjusted"),
    ],
)
def test_exact_fraction_is_exact_up_to_the_digit_cap(value: str, expected: Fraction) -> None:
    """#1241: the cap is inclusive, as for ``checked_addition``."""
    assert exact_fraction(Decimal(value)) == expected


def test_exact_share_keeps_a_third() -> None:
    """#1241: one of three shares bought for $10.00 costs exactly $10/3, which no Decimal holds."""
    assert exact_share(Decimal("10.00"), Decimal("1"), Decimal("3")) == Fraction(10, 3)


def test_exact_share_takes_an_earlier_share_as_is() -> None:
    """#1241: a remainder's share splits the remainder's exact rational."""
    assert exact_share(Fraction(20, 3), Decimal("1"), Decimal("2")) == Fraction(10, 3)


@pytest.mark.parametrize(
    ("amount", "part", "whole"),
    [
        pytest.param("10", "1", "0", id="zero-whole"),
        pytest.param("10", "1", "-0.00", id="negative-zero-whole"),
        pytest.param("NaN", "1", "3", id="amount"),
        pytest.param("10", f"1E-{EXACT_SUM_DIGITS + 1}", "3", id="part"),
        pytest.param("10", "1", "Infinity", id="whole"),
    ],
)
def test_exact_share_without_an_exact_operand_is_none(amount: str, part: str, whole: str) -> None:
    """#1241: a zero whole, or an operand ``exact_fraction`` refuses, gives no share."""
    assert exact_share(Decimal(amount), Decimal(part), Decimal(whole)) is None


@pytest.mark.parametrize(
    ("amount", "part", "whole", "expected"),
    [
        # A sale's $10.00 cash for 2 shares, split by a lot's exact 2/3 share.
        pytest.param(Decimal("10.00"), Fraction(2, 3), Decimal("2"), Fraction(10, 3), id="part"),
        # A $3.40 Dec-31 total of exactly 2 shares, split by a lot's exact 2/3.
        pytest.param(Decimal("3.40"), Fraction(2, 3), Fraction(2), Fraction(17, 15), id="both"),
        # Half a share of a fragment holding exactly 2/3 share for $20/3.
        pytest.param(Fraction(20, 3), Decimal("0.5"), Fraction(2, 3), Fraction(5), id="whole"),
    ],
)
def test_exact_share_takes_an_exact_quantity_as_is(
    amount: Decimal | Fraction,
    part: Decimal | Fraction,
    whole: Decimal | Fraction,
    expected: Fraction,
) -> None:
    """#1263: a ``Fraction`` part or whole, such as a lot's exact quantity after a
    pro-rata sale, is taken as is, so the share stays exact."""
    assert exact_share(amount, part, whole) == expected


@pytest.mark.parametrize(
    ("part", "whole"),
    [
        pytest.param(Decimal(f"1E-{EXACT_SUM_DIGITS + 1}"), Fraction(2, 3), id="part"),
        pytest.param(Fraction(1, 3), Fraction(0), id="zero-whole"),
    ],
)
def test_exact_share_beside_an_exact_quantity_without_an_exact_operand_is_none(
    part: Decimal | Fraction, whole: Fraction
) -> None:
    """#1263: beside a ``Fraction`` whole, a Decimal part past the digit cap still
    gives no share, and so does a zero ``Fraction`` whole."""
    assert exact_share(Decimal("10"), part, whole) is None


@pytest.mark.parametrize(
    ("prec", "value", "expected"),
    [
        pytest.param(28, Fraction(561, 2), "280.5", id="terminating"),
        pytest.param(28, Fraction(-561, 2), "-280.5", id="negative-terminating"),
        pytest.param(28, Fraction(10, 3), "3.333333333333333333333333333", id="third"),
        pytest.param(28, Fraction(-10, 3), "-3.333333333333333333333333333", id="negative-third"),
        pytest.param(28, Fraction(20, 3), "6.666666666666666666666666666", id="cut-not-rounded"),
        pytest.param(3, Fraction(123456789, 2), "61728394.5", id="widened-terminating"),
        pytest.param(3, Fraction(123456790, 3), "41152263.3", id="widened-third"),
    ],
)
def test_exact_decimal_is_one_division_cut_toward_zero(
    prec: int, value: Fraction, expected: str
) -> None:
    """#1241: exact where it fits the caller's precision, else cut toward zero there, widened
    to keep a tenth."""
    with localcontext(Context(prec=prec)):
        result = exact_decimal(value)
    assert result.compare_total(Decimal(expected)) == 0


def test_exact_decimal_ignores_a_hostile_context_and_leaves_it_unchanged() -> None:
    """#1241: a 3-digit caller rounding up and trapping ``Inexact`` and ``Rounded`` still gets
    the cut, and keeps its own flags."""
    with localcontext(Context(prec=3, rounding=ROUND_CEILING, traps=[Inexact, Rounded])):
        before = repr(getcontext())
        results = [exact_decimal(Fraction(10, 3)), exact_decimal(Fraction(-20, 3))]
        after = repr(getcontext())
    assert [str(result) for result in results] == ["3.33", "-6.66"]
    assert after == before


def test_exact_decimal_ignores_the_callers_exponent_limits() -> None:
    """#1241: a caller whose ``Emax`` and ``Emin`` can't hold the value, with overflow
    and underflow trapped, still gets the division, not a bare ``decimal`` exception."""
    with localcontext(Context(Emax=2, Emin=-2, traps=[Overflow, Underflow, Subnormal])):
        results = [exact_decimal(Fraction(10**5, 3)), exact_decimal(Fraction(1, 3 * 10**6))]
    assert [str(result) for result in results] == [
        "33333.33333333333333333333333",
        "3.333333333333333333333333333E-7",
    ]


@given(
    whole=st.integers(min_value=-(10**12), max_value=10**12),
    offset=st.integers(min_value=-3, max_value=3),
    denominator=st.integers(min_value=1, max_value=10**30),
    prec=st.integers(min_value=1, max_value=30),
)
def test_exact_decimal_rounds_to_the_exact_whole_rupees(
    whole: int, offset: int, denominator: int, prec: int
) -> None:
    """#1241: on a half-rupee tie or just either side, at any precision, one division's HALF_UP
    whole rupees are the exact value's, and it is the value cut toward zero."""
    value = whole + Fraction(1, 2) + Fraction(offset, denominator)
    with localcontext(Context(prec=prec)):
        result = exact_decimal(value)
    assert to_whole_rupees(result) == exact_whole_rupees(value)
    assert is_exact_decimal(result, value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Fraction(1, 2), "1", id="half"),
        pytest.param(Fraction(-1, 2), "-1", id="negative-half"),
        pytest.param(Fraction(561, 2), "281", id="tie"),
        pytest.param(Fraction(-2805, 10), "-281", id="negative-tie"),
        pytest.param(Fraction(561, 2) - Fraction(1, 10**30), "280", id="below-tie"),
        pytest.param(Fraction(-561, 2) + Fraction(1, 10**30), "-280", id="negative-below-tie"),
        pytest.param(Fraction(-1, 3), "0", id="negative-to-zero"),
        pytest.param(Fraction(0), "0", id="zero"),
        pytest.param(Fraction(841), "841", id="whole"),
    ],
)
def test_exact_whole_rupees_rounds_half_away_from_zero(value: Fraction, expected: str) -> None:
    """#1241: as ``to_whole_rupees`` rounds a Decimal: HALF_UP, exponent 0, never -0."""
    assert exact_whole_rupees(value).compare_total(Decimal(expected)) == 0


@pytest.mark.parametrize(
    ("value", "exact", "expected"),
    [
        pytest.param("280.5", Fraction(561, 2), True, id="exact"),
        pytest.param("280.50", Fraction(561, 2), True, id="exact-padded"),
        pytest.param("3.333333333333333333333333333", Fraction(10, 3), True, id="cut"),
        pytest.param("-3.333", Fraction(-10, 3), True, id="negative-cut"),
        pytest.param("6.667", Fraction(20, 3), False, id="half-up-overshoots"),
        pytest.param("-3.333", Fraction(10, 3), False, id="wrong-sign"),
        pytest.param("0", Fraction(1, 3), False, id="zero"),
        pytest.param("0.000", Fraction(1, 3000), False, id="zero-cut"),
        pytest.param("3.332", Fraction(10, 3), False, id="short-by-a-unit"),
        pytest.param("280", Fraction(561, 2), False, id="cut-without-a-tenth"),
        pytest.param("2E+2", Fraction(561, 2), False, id="cut-above-the-units"),
        pytest.param("NaN", Fraction(1), False, id="nonfinite"),
        pytest.param("1E-999999999", Fraction(1, 3), False, id="extreme-small-cut"),
        pytest.param("1E+999999999", Fraction(1, 3), False, id="extreme-large-cut"),
    ],
)
def test_is_exact_decimal(value: str, exact: Fraction, expected: bool) -> None:
    """#1241: the exact value itself, or it cut toward zero at the Decimal's last digit,
    keeping a tenth as :func:`exact_decimal` does, so its whole rupees are exact."""
    assert is_exact_decimal(Decimal(value), exact) is expected


@pytest.mark.parametrize("prec", [28, 60])
def test_generated_decimal_cut_can_extend_past_the_operand_place_cap(prec: int) -> None:
    exact = Fraction(1, 3 * 10**950)
    with localcontext(Context(prec=prec)):
        cut = exact_decimal(exact)
    assert is_exact_decimal(cut, exact)


# ---- review amounts and TTBR products (#1243) ------------------------------

_AT = "Synthetic A3 column, INR"


# A third of $10.00, cut to the 20 digits main's lot allocation kept.
_THIRD_OF_TEN = "3.3333333333333333333"


@pytest.mark.parametrize(
    ("prec", "amount", "exact", "expected", "kept"),
    [
        pytest.param(28, "10.00", None, "10.00", None, id="no-rational"),
        pytest.param(
            28, _THIRD_OF_TEN, Fraction(10, 3), "3." + "3" * 27, Fraction(10, 3), id="third"
        ),
        pytest.param(
            60, _THIRD_OF_TEN, Fraction(10, 3), "3." + "3" * 59, Fraction(10, 3), id="caller-prec"
        ),
        pytest.param(28, "280.50", Fraction(561, 2), "280.5", None, id="terminating"),
        pytest.param(28, "5.00", Fraction(5), "5", None, id="whole"),
    ],
)
def test_review_amount_cuts_the_rational_and_keeps_it_only_where_the_cut_drops_it(
    prec: int, amount: str, exact: Fraction | None, expected: str, kept: Fraction | None
) -> None:
    """#1243: with no rational the amount is kept as is; else it is one division of the
    rational at the caller's precision, and the rational is kept only where that can't hold it."""
    with localcontext(Context(prec=prec)):
        review, rational = review_amount(Decimal(amount), exact)
    assert review.compare_total(Decimal(expected)) == 0
    assert rational == kept


@pytest.mark.parametrize(
    ("prec", "amount", "multiplier", "expected"),
    [
        pytest.param(28, Fraction(10, 3), "84.15", "280.5", id="tie"),
        pytest.param(28, Fraction(1, 3), "82.51", "27.50333333333333333333333333", id="cut"),
        pytest.param(3, Fraction(1, 3), "82.51", "27.5", id="cut-keeps-a-tenth"),
        pytest.param(28, Fraction(1), f"1E-{EXACT_SUM_DIGITS}", "1E-1000", id="cap"),
    ],
)
def test_checked_exact_product_is_one_division_of_the_exact_product(
    prec: int, amount: Fraction, multiplier: str, expected: str
) -> None:
    """#1243: one division of the exact product at the caller's precision, keeping a tenth, so
    it files the exact HALF_UP: a third of $10.00 at ₹84.15/$ is exactly ₹280.50, ₹281."""
    with localcontext(Context(prec=prec)):
        product = checked_exact_product(amount, Decimal(multiplier), at=_AT)
    assert product.compare_total(Decimal(expected)) == 0
    assert to_whole_rupees(product) == exact_whole_rupees(amount * Fraction(multiplier))


@pytest.mark.parametrize(
    "multiplier",
    [
        pytest.param("84." + "1" * EXACT_SUM_DIGITS, id="digits"),
        pytest.param(f"1E-{EXACT_SUM_DIGITS + 1}", id="exponent"),
        pytest.param(f"1E+{EXACT_SUM_DIGITS + 1}", id="adjusted"),
        pytest.param("NaN", id="nan"),
        pytest.param("sNaN", id="snan"),
        pytest.param("-Infinity", id="infinity"),
    ],
)
def test_checked_exact_product_refuses_a_multiplier_without_an_exact_rational(
    multiplier: str,
) -> None:
    """#1243: a multiplier ``exact_fraction`` refuses has no exact product; the refusal is
    ``checked_product``'s, with no context and no chain."""
    with pytest.raises(InvariantError) as raised:
        checked_exact_product(Fraction(10, 3), Decimal(multiplier), at=_AT)
    _refusal(raised, f"{_AT}: {INEXACT_PRODUCT}")
    assert raised.value.context == ()
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
