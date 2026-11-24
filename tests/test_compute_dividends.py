"""Tests for :mod:`ibkr_fa.compute.dividends`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.dividends import (
    us_source_dividend_count,
    us_source_withholding_tax_net_paid,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import Dividend, WithholdingTax
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fx_stubs import DateAwareRatePolicy as _DateAwareRatePolicy
from tests._fx_stubs import FixedRatePolicy as _FixedRatePolicy


def _make_div(
    *,
    symbol: str = "XQAA",
    isin: str = "US99990A3095",
    payment_date: date = date(2025, 6, 15),
    amount: str = "1.00",
) -> Dividend:
    return Dividend(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        payment_date=payment_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=f"{symbol}({isin}) Cash Dividend USD x per Share",
    )


# ---- happy path -----------------------------------------------------------


def test_single_dividend_in_cy_aggregated() -> None:
    """One $1 dividend on Jun 15 2025 at rate 85 -> ₹85."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(amount="1.00")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {Ticker("XQAA"): Decimal("85")}


def test_multiple_payments_use_calendar_year_closing_rate() -> None:
    """Payment dates control CY inclusion; one Dec-31 TTBR converts the total."""
    fx = _DateAwareRatePolicy(
        {
            date(2025, 3, 15): Decimal("83"),
            date(2025, 9, 15): Decimal("87"),
            date(2025, 12, 31): Decimal("89"),
        }
    )
    out = aggregate_dividends_by_symbol_inr(
        [
            _make_div(amount="0.50", payment_date=date(2025, 3, 15)),
            _make_div(amount="0.50", payment_date=date(2025, 9, 15)),
        ],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out[Ticker("XQAA")] == Decimal("89.00")
    assert fx.requested_dates == [date(2025, 12, 31)]


def test_multiple_symbols_grouped_independently() -> None:
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [
            _make_div(symbol="XQAA", isin="US99990A3095", amount="1.00"),
            _make_div(symbol="XQAY", isin="US99990B1098", amount="2.00"),
            _make_div(symbol="XQAA", isin="US99990A3095", amount="3.00"),
        ],
        calendar_year=2025,
        fx_policy=fx,
    )
    # Full-precision sums; writer applies HALF_UP at output time.
    assert out == {
        Ticker("XQAA"): Decimal("340.00"),  # (1+3) * 85
        Ticker("XQAY"): Decimal("170.00"),  # 2 * 85
    }


# ---- CY filter ------------------------------------------------------------


def test_dividends_outside_cy_silently_ignored_before() -> None:
    """A dividend in CY-1 is filtered out (multi-period statements
    can emit dividends from before the target window)."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(payment_date=date(2024, 12, 31), amount="100")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {}


def test_dividends_outside_cy_silently_ignored_after() -> None:
    """A dividend in CY+1 is also filtered out."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(payment_date=date(2026, 1, 1), amount="100")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {}


def test_cy_boundary_inclusive_jan_1() -> None:
    """Jan 1 of CY is included (boundary)."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(payment_date=date(2025, 1, 1), amount="1")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {Ticker("XQAA"): Decimal("85")}


def test_cy_boundary_inclusive_dec_31() -> None:
    """Dec 31 of CY is included (boundary)."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(payment_date=date(2025, 12, 31), amount="1")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {Ticker("XQAA"): Decimal("85")}


def test_mix_of_in_cy_and_out_of_cy() -> None:
    """Realistic multi-period statement: only in-CY dividends count."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [
            _make_div(payment_date=date(2024, 12, 15), amount="100"),  # out
            _make_div(payment_date=date(2025, 6, 15), amount="1"),  # in
            _make_div(payment_date=date(2026, 1, 15), amount="100"),  # out
        ],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {Ticker("XQAA"): Decimal("85")}


# ---- empty input ----------------------------------------------------------


def test_empty_dividend_list_returns_empty_dict() -> None:
    """Buy-and-hold accounts with no dividend ex-dates in CY produce
    no entries — caller should treat missing keys as zero (the same
    behaviour build_fa_a3_rows now uses to gate the manual-fill
    warning)."""
    fx = _DateAwareRatePolicy({})
    assert aggregate_dividends_by_symbol_inr([], calendar_year=2025, fx_policy=fx) == {}
    assert fx.requested_dates == []


# ---- rounding -------------------------------------------------------------


def test_full_precision_preserved_through_compute_layer() -> None:
    """PR #137 Tier-1 Finding 3 (correctness): the compute layer
    returns full-precision Decimals matching the convention of
    initial_value.py / closing_value.py. The writer applies the
    documented HALF_UP rounding to the rupee at serialisation
    time. Sub-rupee values must NOT be lost here."""
    fx = _FixedRatePolicy(Decimal("100"))
    # Tiny fractional dividend that would round to 0 if we
    # quantized per payment (0.001 * 100 = 0.1, rounds to 0).
    # Full precision preserves it; the writer's HALF_UP later
    # decides the user-visible rounding.
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(amount="0.001")],
        calendar_year=2025,
        fx_policy=fx,
    )
    assert out == {Ticker("XQAA"): Decimal("0.100")}


def test_single_payment_compute_layer_preserves_subrupee_precision() -> None:
    """Synthetic fractional-share dividend: a 0.02 share holding of
    XQAA receiving a $0.35/share dividend yields $0.007 gross, which
    at TTBR 85 is ₹0.595. The compute layer preserves all
    significant digits; the writer's HALF_UP rounds to ₹1 at
    output time."""
    fx = _FixedRatePolicy(Decimal("85"))
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(amount="0.007")],
        calendar_year=2025,
        fx_policy=fx,
    )
    # 0.02 * 0.35 = 0.007; 0.007 * 85 = 0.595, full precision preserved.
    assert out == {Ticker("XQAA"): Decimal("0.595")}


# #1312: a 27-digit dividend at the Dec-31 TTBR of 89.47 is exactly
# ₹89472841.4999…, which files ₹89472841. Main's 28-digit product,
# ₹89472841.50000000000000000000, filed ₹89472842.
_LONG_DIVIDEND = "1000031.75924891024924555717"
_LONG_DIVIDEND_INR = Decimal("89472841.4999999999999999999999")


def test_symbol_total_is_the_exact_product_at_the_dec31_rate() -> None:
    """#1312: the INR total keeps every digit of the product and files ₹89472841."""
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(amount=_LONG_DIVIDEND)],
        calendar_year=2025,
        fx_policy=_FixedRatePolicy(Decimal("89.47")),
    )
    [(symbol, total)] = out.items()
    assert symbol == Ticker("XQAA")
    assert total.compare_total(_LONG_DIVIDEND_INR) == 0
    assert to_whole_rupees(total) == Decimal("89472841")


def test_symbol_total_just_above_the_half_files_the_rupee_above() -> None:
    """Control: one more digit at the end is ₹89472841.5000…0008946, which
    files ₹89472842, as main's 28-digit product does."""
    out = aggregate_dividends_by_symbol_inr(
        [_make_div(amount="1000031.75924891024924555718")],
        calendar_year=2025,
        fx_policy=_FixedRatePolicy(Decimal("89.47")),
    )
    assert to_whole_rupees(out[Ticker("XQAA")]) == Decimal("89472842")


def test_symbol_total_past_1000_digits_refuses_at_its_position() -> None:
    """#1312: a rate of 1,000 significant digits keeps the first symbol's
    product exact, but the second needs 1,002 digits, so it refuses naming the
    symbol total's position, not the symbol or a value. Main rounded it."""
    rate = Decimal("8." + "3" * 999)
    dividends = [
        _make_div(symbol="XQAA", amount="1"),
        _make_div(symbol="XQCB", isin="US99990B1062", amount="1.5"),
    ]
    with pytest.raises(InvariantError) as caught:
        aggregate_dividends_by_symbol_inr(
            dividends, calendar_year=2025, fx_policy=_FixedRatePolicy(rate)
        )
    error = caught.value
    assert str(error) == (
        f"InvariantError: Schedule FA per-symbol dividends, symbol total 2, INR: {INEXACT_PRODUCT}"
    )
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    for marker in ("XQCB", "US99990B1062", "1.5", "3" * 30):
        assert marker not in repr(error), marker


# ---- US-source advisory inputs (#145, #626) --------------------------------


def _wht(amount: str, isin: str = "US99990E1038") -> WithholdingTax:
    return WithholdingTax(
        symbol=Ticker("XQAW"),
        isin=Isin(isin),
        payment_date=date(2025, 6, 15),
        amount=Decimal(amount),
        currency="USD",
        description="XQAW(US99990E1038) Cash Dividend USD 0.50 per Share - US Tax",
    )


def test_us_source_dividend_count_uses_the_isin_prefix() -> None:
    dividends = [
        _make_div(symbol="XQAW", isin="US99990E1038"),
        _make_div(symbol="XQCX", isin="GB99970P1026"),
        _make_div(symbol="XQAW", isin="US99990E1038", payment_date=date(2025, 9, 15)),
    ]
    assert us_source_dividend_count(dividends) == 2


@pytest.mark.parametrize(
    ("amounts", "expected"),
    [
        (("-0.45", "-0.30"), Decimal("0.75")),  # paid only: -(-0.45 - 0.30)
        (("0.45",), Decimal("0")),  # reversal only
        (("-0.45", "0.45"), Decimal("0.00")),  # paid + reversal that cancel
        ((), Decimal("0")),  # no rows
    ],
)
def test_us_source_withholding_tax_net_paid(amounts: tuple[str, ...], expected: Decimal) -> None:
    net = us_source_withholding_tax_net_paid([_wht(amount) for amount in amounts])
    assert net.as_tuple() == expected.as_tuple()


def test_us_source_withholding_tax_ignores_non_us_isins() -> None:
    assert us_source_withholding_tax_net_paid([_wht("-0.50", isin="GB99970P1026")]) == 0
