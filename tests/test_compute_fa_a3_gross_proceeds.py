"""Tests for :mod:`ibkr_fa.compute.fa_a3_gross_proceeds`.

Issue #16: closed-lot proceeds → INR via the reporting calendar
year's December 31 TTBR; open lots short-circuit to :class:`Decimal` 0.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.compute.fa_a3_gross_proceeds import (
    gross_proceeds_inr,
    gross_proceeds_inr_for_lot,
)
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fx_stubs import CalendarPolicy


def _make_closed_lot(
    *,
    proceeds: Decimal = Decimal("261.75"),
    close_date: date = date(2025, 6, 15),
    open_date: date = date(2024, 6, 15),
    cost: Decimal = Decimal("274.60"),
    gross: Decimal | None = None,
) -> ClosedLot:
    """Build a default XQAT closed lot."""
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQAT"),
        description="SAMPLE ISSUER AT INC",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("5"),
        cost_basis_money=cost,
        proceeds_money=proceeds,
        open_date_time=open_date,
        close_date_time=close_date,
        gross_proceeds_money=gross,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


def _make_open_lot(
    *,
    open_date: date = date(2024, 6, 15),
    cost: Decimal = Decimal("386.45"),
    quantity: Decimal = Decimal("15"),
    mark_price: Decimal = Decimal("32.70"),
) -> OpenLot:
    """Build a default XQAW open lot."""
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQAW"),
        description="SAMPLE ISSUER AW INC",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=quantity,
        mark_price=mark_price,
        cost_basis_money=cost,
        side=Side.LONG,
        open_date_time=open_date,
    )


# ---- gross_proceeds_inr primitive -----------------------------------------


def test_gross_proceeds_inr_multiplies_proceeds_by_rate() -> None:
    """Happy path: ``proceeds * rate`` exactly, full precision."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    result = gross_proceeds_inr(Decimal("261.75"), 2025, fx_policy=fx)
    # 261.75 * 85.75 = 22445.0625 exactly (no rounding here;
    # the A3 row's filed value rounds it).
    assert result == Decimal("22445.0625")


def test_gross_proceeds_inr_returns_full_precision_decimal() -> None:
    """No quantization — the A3 row's filed value rounds it. The
    Decimal returned here must preserve every digit IBKR + SBI
    published, so audit columns can independently reproduce the
    portal figure."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    result = gross_proceeds_inr(Decimal("123.456789"), 2025, fx_policy=fx)
    # Multiplication of two Decimals preserves all significant digits.
    assert result == Decimal("123.456789") * Decimal("85.75")
    assert isinstance(result, Decimal)


def test_gross_proceeds_inr_zero_proceeds_returns_zero() -> None:
    """Zero proceeds times any rate = zero. The invariant allows
    it (>= 0); the multiplication yields exact zero."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    result = gross_proceeds_inr(Decimal("0"), 2025, fx_policy=fx)
    assert result == Decimal("0")


def test_gross_proceeds_inr_negative_proceeds_passes_through() -> None:
    """The conversion primitive preserves the input sign."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    result = gross_proceeds_inr(Decimal("-1.50"), 2025, fx_policy=fx)
    # -1.50 * 85.75 = -128.625 (the sign passes through)
    assert result == Decimal("-1.50") * Decimal("85.75")
    assert result < 0


def test_gross_proceeds_inr_missing_dec31_fx_rate_propagates() -> None:
    """A missing reporting-year closing rate propagates unchanged."""
    fx = CalendarPolicy({})  # no rates
    with pytest.raises(MissingFxRateError):
        gross_proceeds_inr(Decimal("100"), 2025, fx_policy=fx)
    assert fx.requested_dates[-1] == date(2025, 12, 31)


# #1250: a 32-digit gross at ₹81.92/$ is ₹4096.4999… to every digit, which files
# ₹4096. A 28-digit product rounds it to ₹4096.50, which files ₹4097.
_LONG_GROSS = Decimal("50.006103515624999999999999999999")
_LONG_GROSS_INR = Decimal("4096.49999999999999999999999999991808")
_DEC31_8192 = {date(2025, 12, 31): Decimal("81.92")}
_GROSS_REFUSAL = f"InvariantError: Schedule FA A3 gross proceeds, INR: {INEXACT_PRODUCT}"


def test_gross_proceeds_inr_keeps_every_digit_of_a_long_product() -> None:
    """#1250: proceeds x rate keeps all 36 digits. Main multiplied at 28 digits
    and got ₹4096.500000000000000000000000."""
    result = gross_proceeds_inr(_LONG_GROSS, 2025, fx_policy=CalendarPolicy(_DEC31_8192))
    assert result.compare_total(_LONG_GROSS_INR) == 0
    assert to_whole_rupees(result) == Decimal("4096")


@pytest.mark.parametrize(
    "proceeds",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)
def test_gross_proceeds_inr_refuses_when_it_cannot_be_exact(proceeds: str) -> None:
    """#1250: past the exponent or the 1000-digit cap the product refuses,
    naming no value. Main raised a raw Overflow or returned a rounded product."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("82.51")})
    with pytest.raises(InvariantError) as exc:
        gross_proceeds_inr(Decimal(proceeds), 2025, fx_policy=fx)
    assert str(exc.value) == _GROSS_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


# ---- gross_proceeds_inr_for_lot wrapper -----------------------------------


def test_open_lot_returns_zero() -> None:
    """Open lots have no disposal — no FX lookup, just 0."""
    fx = CalendarPolicy({})  # NO rates — proves we don't call rate_for
    lot = _make_open_lot()
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result == Decimal("0")


def test_open_lot_returns_decimal_zero_not_int_zero() -> None:
    """The return type must be Decimal (the FA-A3 row invariant
    accepts Decimal | None; an int 0 would fail downstream
    type-checking and the model's >= 0 invariant check on a
    Decimal)."""
    fx = CalendarPolicy({})
    lot = _make_open_lot()
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert isinstance(result, Decimal)


def test_closed_lot_uses_dec31_for_fx_lookup() -> None:
    """The reporting-year closing rate wins over acquisition and disposal rates."""
    fx = CalendarPolicy(
        {
            date(2024, 6, 15): Decimal("83.50"),  # acquisition rate
            date(2025, 6, 15): Decimal("84.25"),  # disposal rate
            date(2025, 12, 31): Decimal("85.75"),  # FA gross-credit rate
        }
    )
    lot = _make_closed_lot(gross=Decimal("263"))
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result == Decimal("263") * Decimal("85.75")
    assert result != Decimal("263") * Decimal("83.50")
    assert result != Decimal("263") * Decimal("84.25")


def test_closed_lot_propagates_missing_dec31_fx_rate() -> None:
    """Acquisition and disposal rates cannot substitute for the Dec-31 rate."""
    fx = CalendarPolicy(
        {
            date(2024, 6, 15): Decimal("83.50"),
            date(2025, 6, 15): Decimal("84.25"),
        }
    )
    lot = _make_closed_lot(gross=Decimal("263"))
    with pytest.raises(MissingFxRateError):
        gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert fx.requested_dates[-1] == date(2025, 12, 31)


def test_closed_lot_zero_proceeds_returns_zero() -> None:
    """An edge case: a lot disposed at exactly its cost basis (no
    P&L, with proceeds_money = 0 is also impossible per the
    ClosedLot identity unless cost is 0 too). A zero-cost lot
    disposed at zero proceeds = 0 INR proceeds. Realistic for
    fully-worthless dispositions (rare but valid)."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    lot = _make_closed_lot(
        proceeds=Decimal("0"),
        cost=Decimal("0"),
        gross=Decimal("0"),
    )
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result == Decimal("0")


def test_closed_lot_zero_proceeds_still_need_dec31_rate() -> None:
    """#735: proven zero proceeds convert at Dec-31 like any other proceeds."""
    fx = CalendarPolicy({date(2025, 1, 15): Decimal("85.50")})
    lot = _make_closed_lot(
        proceeds=Decimal("0"),
        open_date=date(2025, 1, 15),
        close_date=date(2025, 6, 16),
        gross=Decimal("0"),
    )
    with pytest.raises(MissingFxRateError):
        gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert fx.requested_dates[-1] == date(2025, 12, 31)


def test_closed_lot_without_gross_returns_none_without_fx_lookup() -> None:
    """Net cash cannot substitute for the portal's gross consideration."""
    fx = CalendarPolicy({})
    lot = _make_closed_lot(
        cost=Decimal("1.00"),
        proceeds=Decimal("-0.50"),
    )
    assert lot.proceeds_money == Decimal("-0.50")
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result is None


def test_closed_lot_uses_gross_proceeds_money_when_present() -> None:
    """#348: when the parser supplies the raw gross (quantity * T.Price),
    the wrapper files THAT — not the net ``proceeds_money`` — and applies
    no clamp (gross is non-negative by construction)."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    # Net proceeds 261.75, but the shares really sold for a gross 263.00
    # (the difference is the IBKR commission the net folds in).
    lot = _make_closed_lot(gross=Decimal("263.00"))
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result == Decimal("263.00") * Decimal("85.75")
    # ...and it is NOT the net-derived figure.
    assert result != lot.proceeds_money * Decimal("85.75")


def test_closed_lot_long_gross_converts_exactly_in_any_context() -> None:
    """#1250: the lot wrapper's product is exact, even at a hostile 3-digit
    context that traps rounding. Main raised the context's Inexact trap."""
    lot = _make_closed_lot(gross=_LONG_GROSS)
    with localcontext(Context(prec=3, traps=[Inexact, Rounded])):
        result = gross_proceeds_inr_for_lot(
            lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8192)
        )
    assert result is not None
    assert result.compare_total(_LONG_GROSS_INR) == 0


def test_closed_lot_converts_its_exact_gross_proceeds() -> None:
    """#1383: a lot carrying ``gross_proceeds_money_exact``, such as a third of
    a $100.00 Robinhood sale, converts that rational: $100/3 x ₹90 is exactly
    ₹3,000. Its 28-digit Decimal cut would give ₹2,999.99…97."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("90")})
    third = Fraction(100, 3)
    lot = replace(_make_closed_lot(gross=exact_decimal(third)), gross_proceeds_money_exact=third)
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result == Decimal("3000")


def test_closed_lot_deep_loss_with_gross_files_positive_not_clamped() -> None:
    """A positive proven gross is filed even when net cash is negative."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    lot = _make_closed_lot(
        cost=Decimal("1.00"),
        proceeds=Decimal("-0.50"),  # net negative (same as the clamp test)
        gross=Decimal("0.50"),  # but the shares really sold for $0.50
    )
    result = gross_proceeds_inr_for_lot(lot, calendar_year=2025, fx_policy=fx)
    assert result is not None
    assert result == Decimal("0.50") * Decimal("85.75")
    assert result > 0  # NOT clamped to zero


def test_closed_lot_deep_loss_primitive_passes_negative_through() -> None:
    """The lower-level primitive retains its signed multiplication contract."""
    fx = CalendarPolicy({date(2025, 12, 31): Decimal("85.75")})
    result = gross_proceeds_inr(Decimal("-0.50"), 2025, fx_policy=fx)
    assert result == Decimal("-0.50") * Decimal("85.75")
    assert result < 0
