"""Tests for :mod:`ibkr_fa.compute.closing_value`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from typing import ClassVar

import pytest

from ibkr_fa.compute import closing_value_inr, closing_value_inr_for_lot
from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fx_stubs import CalendarPolicy


def make_open_lot(
    *,
    quantity: Decimal = Decimal("10"),
    mark_price: Decimal = Decimal("100"),
    closing_total_usd: Decimal | None = None,
) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQAW"),
        description="XQAW (test)",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=quantity,
        mark_price=mark_price,
        cost_basis_money=Decimal("800"),
        side=Side.LONG,
        open_date_time=date(2024, 6, 15),
        closing_total_usd=closing_total_usd,
    )


def make_closed_lot() -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQAT"),
        description="XQAT (test)",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("5"),
        cost_basis_money=Decimal("274.60"),
        proceeds_money=Decimal("261.75"),
        open_date_time=date(2024, 6, 15),
        close_date_time=date(2025, 6, 15),
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


# ---- closing_value_inr (primitive) -----------------------------------------


def test_simple_year_end_conversion() -> None:
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    inr = closing_value_inr(Decimal("1000"), calendar_year=2025, fx_policy=policy)
    assert inr == Decimal("85000.00")


def test_uses_dec_31_of_target_cy_for_fx() -> None:
    """Regression: the FX rate must be looked up for Dec 31 of the
    requested calendar_year — not Jan 1, not the current date, not
    some other heuristic."""
    policy = CalendarPolicy(
        {
            date(2024, 12, 31): Decimal("83.10"),
            date(2025, 12, 31): Decimal("85.20"),
            date(2026, 12, 31): Decimal("86.30"),
        }
    )
    assert closing_value_inr(Decimal("100"), calendar_year=2024, fx_policy=policy) == Decimal(
        "8310.00"
    )
    assert closing_value_inr(Decimal("100"), calendar_year=2025, fx_policy=policy) == Decimal(
        "8520.00"
    )
    assert closing_value_inr(Decimal("100"), calendar_year=2026, fx_policy=policy) == Decimal(
        "8630.00"
    )


def test_preserves_full_decimal_precision() -> None:
    """Same convention as initial_value: no quantization in compute."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("83.4321")})
    inr = closing_value_inr(Decimal("490.5"), calendar_year=2025, fx_policy=policy)
    # 490.5 * 83.4321 = 40923.44505
    assert inr == Decimal("40923.44505")


def test_zero_position_value_returns_zero() -> None:
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    assert closing_value_inr(Decimal("0"), calendar_year=2025, fx_policy=policy) == 0


def test_propagates_missing_fx_rate_error() -> None:
    """If the policy has no rate for Dec 31 of CY, its error
    propagates. The test checks the policy was asked for that date;
    the error itself doesn't name it (#1368)."""
    policy = CalendarPolicy({date(2024, 12, 31): Decimal("83.10")})
    with pytest.raises(MissingFxRateError):
        closing_value_inr(Decimal("100"), calendar_year=2030, fx_policy=policy)
    assert policy.requested_dates[-1] == date(2030, 12, 31)


# ---- closing_value_inr_for_lot ---------------------------------------------


def test_open_lot_uses_quantity_times_mark_price() -> None:
    """OpenLot delegates to the primitive with quantity * mark_price.
    OpenLot.mark_price is the documented source of truth for FA-A3
    closing value (see the field's docstring). Regression catches
    using cost_basis_money or any other field by mistake."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    # quantity=15, mark_price=32.70 → year-end USD position 490.5
    lot = make_open_lot(quantity=Decimal("15"), mark_price=Decimal("32.70"))
    inr = closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=policy)
    assert inr == Decimal("41692.50")  # 490.5 * 85.00


def test_closed_lot_returns_zero() -> None:
    """A disposed lot has no year-end position. Closing value is 0."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    lot = make_closed_lot()
    assert closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=policy) == Decimal("0")


def test_closed_lot_does_not_consult_fx_policy() -> None:
    """The 0-for-disposed convention must short-circuit before any
    FX lookup. Otherwise users with an outdated TTBR archive that
    doesn't cover the target Dec 31 would see a spurious
    MissingFxRateError on closed-lot rows."""

    class _RaisingPolicy:
        currency: ClassVar[str] = "USD"

        def rate_for(self, event_date: date) -> Decimal:
            del event_date
            raise AssertionError("FX policy must not be called for ClosedLot")

    lot = make_closed_lot()
    assert closing_value_inr_for_lot(
        lot, calendar_year=2025, fx_policy=_RaisingPolicy()
    ) == Decimal("0")


def test_open_lot_propagates_missing_fx_rate_error() -> None:
    policy = CalendarPolicy({date(2024, 12, 31): Decimal("83.10")})
    lot = make_open_lot(quantity=Decimal("1"), mark_price=Decimal("100"))
    with pytest.raises(MissingFxRateError):
        closing_value_inr_for_lot(lot, calendar_year=2030, fx_policy=policy)
    assert policy.requested_dates[-1] == date(2030, 12, 31)


def test_two_open_lots_with_same_qty_mark_match_for_same_cy() -> None:
    """Sanity: closing value depends only on quantity * mark_price
    and CY, not on any other lot field."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    lot_a = make_open_lot(quantity=Decimal("5"), mark_price=Decimal("100"))
    lot_b = make_open_lot(quantity=Decimal("5"), mark_price=Decimal("100"))
    assert closing_value_inr_for_lot(
        lot_a, calendar_year=2025, fx_policy=policy
    ) == closing_value_inr_for_lot(lot_b, calendar_year=2025, fx_policy=policy)


# ---- #1242: a statement total feeds the closing value directly -------------

_DEC31_8250 = {date(2025, 12, 31): Decimal("82.50")}
# A $1.00 statement total over 3 shares, as the 28-digit mark a division gives.
_THIRD = Decimal("0.3333333333333333333333333333")
_CLOSING_REFUSAL = f"InvariantError: Schedule FA A3 closing value, INR: {INEXACT_PRODUCT}"


def test_statement_total_files_its_exact_closing_value() -> None:
    """3 shares worth $1.00 at ₹82.50/$: 3 x the mark x the rate is ₹82.4999…,
    which files ₹82; the total itself gives ₹82.50, which files ₹83."""
    policy = CalendarPolicy(_DEC31_8250)
    marked = make_open_lot(quantity=Decimal("3"), mark_price=_THIRD)
    totalled = make_open_lot(
        quantity=Decimal("3"), mark_price=_THIRD, closing_total_usd=Decimal("1.00")
    )
    assert closing_value_inr_for_lot(marked, calendar_year=2025, fx_policy=policy) < Decimal("82.5")
    assert closing_value_inr_for_lot(totalled, calendar_year=2025, fx_policy=policy) == Decimal(
        "82.5"
    )


def test_statement_total_wins_over_the_mark() -> None:
    """The total is the closing value; the mark is only review detail."""
    lot = make_open_lot(
        quantity=Decimal("2"), mark_price=Decimal("15"), closing_total_usd=Decimal("31")
    )
    inr = closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8250))
    assert inr == Decimal("2557.50")


def test_statement_total_product_keeps_every_digit_in_any_context() -> None:
    """A 60-digit total at a hostile 3-digit context, trapping rounding, still
    gives the exact product."""
    total = "0." + "3" * 60
    lot = make_open_lot(quantity=Decimal("3"), mark_price=_THIRD, closing_total_usd=Decimal(total))
    with localcontext(Context(prec=3, traps=[Inexact, Rounded])):
        inr = closing_value_inr_for_lot(
            lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8250)
        )
    # $(1/3 - 1/(3E60)) x 82.50 = ₹(27.5 - 2.75E-59), to the operands' 62 places.
    assert inr.compare_total(Decimal("27.4" + "9" * 57 + "7250")) == 0


@pytest.mark.parametrize(
    "total",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)
def test_statement_total_product_refuses_when_it_cannot_be_exact(total: str) -> None:
    lot = make_open_lot(
        quantity=Decimal("1"), mark_price=Decimal("1"), closing_total_usd=Decimal(total)
    )
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("82.51")})
    with pytest.raises(InvariantError) as exc:
        closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=policy)
    assert str(exc.value) == _CLOSING_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def _third_share() -> OpenLot:
    """One share holding exactly a third of a $10.00 statement total, beside its 28-digit cut."""
    cut = exact_decimal(Fraction(10, 3))
    lot = make_open_lot(quantity=Decimal("1"), mark_price=cut, closing_total_usd=cut)
    return replace(lot, closing_total_usd_exact=Fraction(10, 3))


def test_exact_statement_share_files_its_exact_half_up() -> None:
    """#1243: a third of $10.00 at ₹84.15/$ is exactly ₹280.50, which files ₹281; the exact
    product of its 28-digit cut is ₹280.4999…, which filed ₹280."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("84.15")})
    inr = closing_value_inr_for_lot(_third_share(), calendar_year=2025, fx_policy=policy)
    assert to_whole_rupees(inr) == Decimal("281")
    assert inr.compare_total(Decimal("280.5")) == 0


def test_exact_statement_share_refuses_a_rate_without_an_exact_rational() -> None:
    """#1243: a rate 1001 places past the units digit multiplies the cut exactly, but has
    no exact rational for the share."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("1E-1001")})
    with pytest.raises(InvariantError) as exc:
        closing_value_inr_for_lot(_third_share(), calendar_year=2025, fx_policy=policy)
    assert str(exc.value) == _CLOSING_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_statement_total_propagates_missing_fx_rate_error() -> None:
    lot = make_open_lot(quantity=Decimal("3"), mark_price=_THIRD, closing_total_usd=Decimal("1.00"))
    policy = CalendarPolicy(_DEC31_8250)
    with pytest.raises(MissingFxRateError):
        closing_value_inr_for_lot(lot, calendar_year=2030, fx_policy=policy)
    assert policy.requested_dates[-1] == date(2030, 12, 31)


# ---- #1250: quantity x mark and the INR value are exact or refuse ----------

_DEC31_8192 = {date(2025, 12, 31): Decimal("81.92")}
# A 33-digit mark: 3 shares at it are worth $835.174560546874999999999999999998,
# which a 28-digit product rounds to $835.174560546875.
_LONG_MARK = Decimal("278.391520182291666666666666666666")
_LONG_POSITION_USD = Decimal("835.174560546874999999999999999998")
# That position at ₹81.92/$, to every digit. It files ₹68417; a 28-digit product
# rounds it to ₹68417.50, which files ₹68418.
_LONG_POSITION_INR = Decimal("68417.49999999999999999999999999983616")
_POSITION_REFUSAL = f"InvariantError: Schedule FA A3 closing value, position USD: {INEXACT_PRODUCT}"
_PAST_THE_CAP = pytest.mark.parametrize(
    "operand",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)


def _assert_bare_refusal(error: InvariantError, message: str) -> None:
    assert str(error) == message
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None


def test_marked_lot_files_its_exact_closing_value() -> None:
    """3 x a 33-digit mark x ₹81.92/$ keeps every digit and files ₹68417. Main
    multiplied at 28 digits, rounded the position to $835.174560546875 and filed
    ₹68418."""
    lot = make_open_lot(quantity=Decimal("3"), mark_price=_LONG_MARK)
    inr = closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8192))
    assert inr.compare_total(_LONG_POSITION_INR) == 0
    assert to_whole_rupees(inr) == Decimal("68417")


def test_marked_lot_closing_value_keeps_every_digit_in_any_context() -> None:
    """Both products are exact at a hostile 3-digit context that traps
    rounding. Main's quantity x mark raised the context's Inexact trap."""
    lot = make_open_lot(quantity=Decimal("3"), mark_price=_LONG_MARK)
    with localcontext(Context(prec=3, traps=[Inexact, Rounded])):
        inr = closing_value_inr_for_lot(
            lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8192)
        )
    assert inr.compare_total(_LONG_POSITION_INR) == 0


def test_position_value_converts_to_its_exact_inr_value() -> None:
    """The primitive's position x rate keeps every digit. Main rounded the
    37-digit product to ₹68417.50000000000000000000000."""
    inr = closing_value_inr(
        _LONG_POSITION_USD, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8192)
    )
    assert inr.compare_total(_LONG_POSITION_INR) == 0


@_PAST_THE_CAP
def test_marked_position_refuses_when_it_cannot_be_exact(operand: str) -> None:
    """quantity x mark refuses past the exponent or the 1000-digit cap. Main
    rounded the 999-digit quantity's product and raised a raw Overflow for the
    other."""
    lot = make_open_lot(quantity=Decimal(operand), mark_price=Decimal("82.51"))
    with pytest.raises(InvariantError) as exc:
        closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=CalendarPolicy(_DEC31_8250))
    _assert_bare_refusal(exc.value, _POSITION_REFUSAL)


@_PAST_THE_CAP
def test_position_value_refuses_when_its_inr_value_cannot_be_exact(operand: str) -> None:
    """The primitive's position x rate refuses past the cap. Main rounded the
    999-digit product and raised a raw Overflow for the other."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("82.51")})
    with pytest.raises(InvariantError) as exc:
        closing_value_inr(Decimal(operand), calendar_year=2025, fx_policy=policy)
    _assert_bare_refusal(exc.value, _CLOSING_REFUSAL)


def test_marked_lot_refuses_when_its_inr_value_cannot_be_exact() -> None:
    """A 999-digit quantity at a $1 mark is an exact position, but x ₹82.51/$
    needs 1002 digits, so the INR product refuses. Main returned a rounded value."""
    lot = make_open_lot(quantity=Decimal("1" * 999), mark_price=Decimal("1"))
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("82.51")})
    with pytest.raises(InvariantError) as exc:
        closing_value_inr_for_lot(lot, calendar_year=2025, fx_policy=policy)
    _assert_bare_refusal(exc.value, _CLOSING_REFUSAL)


# ============================================================================
# Construction-time invariants (PR #123, issue #117)
# ============================================================================


def test_invariant_negative_position_value_rejected() -> None:
    """The bare helper rejects negative position value at parse time
    rather than silently propagating a wrong number into the
    pipeline. The OpenLot model already blocks the construction
    upstream (PR #122)."""
    policy = CalendarPolicy({date(2025, 12, 31): Decimal("85.00")})
    with pytest.raises(InvariantError) as exc:
        closing_value_inr(Decimal("-1000"), calendar_year=2025, fx_policy=policy)
    assert "position_value_usd must be non-negative" in exc.value.what
