"""Tests for :mod:`ibkr_fa.compute.initial_value`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest

from ibkr_fa.compute import initial_value_inr, initial_value_inr_for_lot
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models import BasisProvenance, ClosedLot, OpenLot, Side
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fx_stubs import CalendarPolicy, FixedRatePolicy


def make_open_lot(*, cost: Decimal, opened: date) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQAW"),
        description="XQAW (test)",
        isin=Isin("US99990E1038"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        cost_basis_money=cost,
        side=Side.LONG,
        open_date_time=opened,
    )


def make_closed_lot(*, cost: Decimal, opened: date, closed: date) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker("XQAT"),
        description="XQAT (test)",
        isin=Isin("US99990G1085"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("5"),
        cost_basis_money=cost,
        proceeds_money=cost + Decimal("10"),
        open_date_time=opened,
        close_date_time=closed,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


# ---- initial_value_inr (primitive) -----------------------------------------


def test_simple_conversion() -> None:
    policy = FixedRatePolicy(Decimal("83.40"))
    inr = initial_value_inr(Decimal("100"), date(2025, 6, 15), fx_policy=policy)
    assert inr == Decimal("8340.00")


def test_preserves_full_decimal_precision() -> None:
    """No quantization in the compute layer — the A3 row's filed value
    rounds it. cost_basis * rate must round-trip every digit."""
    policy = FixedRatePolicy(Decimal("83.4321"))
    inr = initial_value_inr(Decimal("386.45"), date(2025, 6, 15), fx_policy=policy)
    # 386.45 * 83.4321 = 32242.335045 (full Decimal precision)
    assert inr == Decimal("32242.335045")


def test_zero_cost_basis_returns_zero() -> None:
    """Defensive: a zero-cost-basis lot (rare but possible after some
    corporate actions) should compute to zero, not raise."""
    policy = FixedRatePolicy(Decimal("83.40"))
    assert initial_value_inr(Decimal("0"), date(2025, 6, 15), fx_policy=policy) == 0


def test_negative_cost_basis_passes_through() -> None:
    """PR #123 (correctness invariants): negative cost basis is now
    a fail-fast InvariantError rather than silently passing through.
    OpenLot/ClosedLot already prohibit negative cost_basis_money at
    construction (PR #122); this test verifies the bare helper has
    the same posture so a direct caller can't bypass the lot-level
    check."""
    policy = FixedRatePolicy(Decimal("83.40"))
    with pytest.raises(InvariantError) as exc:
        initial_value_inr(Decimal("-100"), date(2025, 6, 15), fx_policy=policy)
    assert "cost_basis_money must be non-negative" in exc.value.what


def test_uses_acquired_on_for_fx_lookup() -> None:
    """Regression: the FX rate must be sourced for the acquisition
    date specifically — not some other date the function might
    accidentally use."""
    policy = CalendarPolicy(
        {
            date(2024, 1, 15): Decimal("83.10"),
            date(2025, 1, 15): Decimal("84.20"),
        }
    )
    inr = initial_value_inr(Decimal("100"), date(2024, 1, 15), fx_policy=policy)
    assert inr == Decimal("8310.00")
    inr2 = initial_value_inr(Decimal("100"), date(2025, 1, 15), fx_policy=policy)
    assert inr2 == Decimal("8420.00")


def test_propagates_missing_fx_rate_error() -> None:
    """MissingFxRateError from the policy bubbles up unchanged so the
    pipeline can render it with full structured context."""
    policy = CalendarPolicy({date(2025, 1, 15): Decimal("84.20")})
    with pytest.raises(MissingFxRateError) as exc:
        initial_value_inr(Decimal("100"), date(2030, 1, 1), fx_policy=policy)
    assert exc.value.currency == "USD"
    assert policy.requested_dates[-1] == date(2030, 1, 1)


# ---- initial_value_inr_for_lot (convenience wrapper) -----------------------


def test_for_open_lot_uses_open_date_time_and_cost_basis_money() -> None:
    policy = CalendarPolicy({date(2024, 6, 15): Decimal("83.50")})
    lot = make_open_lot(cost=Decimal("386.45"), opened=date(2024, 6, 15))
    # 386.45 * 83.50 = 32268.575
    assert initial_value_inr_for_lot(lot, fx_policy=policy) == Decimal("32268.575")


def test_for_closed_lot_uses_open_date_time_not_close_date() -> None:
    """A closed lot's INITIAL value is sourced from its OPEN date —
    NOT close_date_time. Regression for the obvious mistake."""
    policy = CalendarPolicy(
        {
            date(2024, 1, 15): Decimal("83.10"),  # acquired on this date
            date(2025, 6, 1): Decimal("84.50"),  # closed on this date — NOT used
        }
    )
    lot = make_closed_lot(cost=Decimal("100"), opened=date(2024, 1, 15), closed=date(2025, 6, 1))
    assert initial_value_inr_for_lot(lot, fx_policy=policy) == Decimal("8310.00")


def test_for_lot_propagates_missing_fx_rate_error() -> None:
    """If the lot's open_date_time has no FX rate, the policy's error
    propagates. The test checks the policy was asked for that date;
    the error itself doesn't name it (#1368)."""
    policy = CalendarPolicy({date(2025, 1, 15): Decimal("84.20")})
    lot = make_open_lot(cost=Decimal("100"), opened=date(2025, 7, 15))
    with pytest.raises(MissingFxRateError):
        initial_value_inr_for_lot(lot, fx_policy=policy)
    assert policy.requested_dates[-1] == date(2025, 7, 15)


def test_for_open_and_closed_with_same_open_date_compute_equal() -> None:
    """An OpenLot and a ClosedLot acquired on the same date with the
    same cost_basis_money compute to the same INR value — the union
    type's two arms are interchangeable from this function's
    perspective."""
    policy = FixedRatePolicy(Decimal("83.40"))
    same_open = date(2024, 6, 15)
    same_cost = Decimal("386.45")
    open_lot = make_open_lot(cost=same_cost, opened=same_open)
    closed_lot = make_closed_lot(cost=same_cost, opened=same_open, closed=date(2025, 3, 1))
    assert initial_value_inr_for_lot(open_lot, fx_policy=policy) == initial_value_inr_for_lot(
        closed_lot, fx_policy=policy
    )


# ---- #1243: an exact cost converts its rational ----------------------------

# A third of $10.00, cut to the 20 digits main's lot allocation kept.
_THIRD_OF_TEN = Decimal("3.3333333333333333333")
_OPENED = date(2025, 1, 15)

_INITIAL_REFUSAL = f"InvariantError: Schedule FA A3 initial value, INR: {INEXACT_PRODUCT}"


def _third_lot(kind: str, *, exact: Fraction | None = Fraction(10, 3)) -> OpenLot | ClosedLot:
    """A lot of kind ``open`` or ``closed`` costing ``_THIRD_OF_TEN``, with ``exact``."""
    lot = (
        make_open_lot(cost=_THIRD_OF_TEN, opened=_OPENED)
        if kind == "open"
        else make_closed_lot(cost=_THIRD_OF_TEN, opened=_OPENED, closed=date(2025, 6, 2))
    )
    return lot if exact is None else replace(lot, cost_basis_money_exact=exact)


@pytest.mark.parametrize("kind", ["open", "closed"])
def test_exact_cost_files_its_exact_half_up(kind: str) -> None:
    """#1243: a third of $10.00 at ₹84.15/$ is exactly ₹280.50, which files ₹281; the
    20-digit cost gives ₹280.499999999999999997195, which filed ₹280."""
    lot = _third_lot(kind)
    inr = initial_value_inr_for_lot(lot, fx_policy=FixedRatePolicy(Decimal("84.15")))
    assert to_whole_rupees(inr) == Decimal("281")
    assert inr.compare_total(Decimal("280.5")) == 0


@pytest.mark.parametrize("kind", ["open", "closed"])
@pytest.mark.parametrize(
    "rate",
    [
        pytest.param("84." + "1" * 1000, id="past-1000-digits"),
        pytest.param("1E-1001", id="past-1000-places"),
    ],
)
def test_exact_cost_refuses_a_rate_without_an_exact_rational(kind: str, rate: str) -> None:
    """#1243: the exact product has no rational for such a rate, so it refuses.
    Since #1250 a 1002-digit rate's Decimal product refuses first, with the same
    message (see the controls below)."""
    with pytest.raises(InvariantError) as exc:
        initial_value_inr_for_lot(_third_lot(kind), fx_policy=FixedRatePolicy(Decimal(rate)))
    assert str(exc.value) == _INITIAL_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


# ---- #1250: cost x rate is exact or refuses ---------------------------------

# A 33-digit cost: at ₹80.00/$ it is ₹10000.4999… to every digit, which files
# ₹10000. A 28-digit product rounds it to ₹10000.50, which files ₹10001.
_LONG_COST = Decimal("125.006249999999999999999999999999")
_LONG_COST_INR = Decimal("10000.49999999999999999999999999992000")


def test_long_cost_converts_to_its_exact_inr_value() -> None:
    """The primitive keeps all 37 digits of cost x rate. Main multiplied at 28
    digits and got ₹10000.50000000000000000000000."""
    inr = initial_value_inr(
        _LONG_COST, date(2025, 6, 15), fx_policy=FixedRatePolicy(Decimal("80.00"))
    )
    assert inr.compare_total(_LONG_COST_INR) == 0
    assert to_whole_rupees(inr) == Decimal("10000")


def test_for_lot_converts_a_long_cost_exactly_in_any_context() -> None:
    """The lot wrapper's product is exact, even at a hostile 3-digit context
    that traps rounding. Main raised the context's Inexact trap."""
    lot = make_open_lot(cost=_LONG_COST, opened=date(2024, 6, 15))
    policy = CalendarPolicy({date(2024, 6, 15): Decimal("80.00")})
    with localcontext(Context(prec=3, traps=[Inexact, Rounded])):
        inr = initial_value_inr_for_lot(lot, fx_policy=policy)
    assert inr.compare_total(_LONG_COST_INR) == 0


@pytest.mark.parametrize(
    "cost",
    ["9E+999999", "1" * 999],
    ids=["past-the-largest-exponent", "past-1000-digits"],
)
def test_product_refuses_when_it_cannot_be_exact(cost: str) -> None:
    """Past the exponent or the 1000-digit cap the product refuses, naming no
    value. Main raised a raw Overflow or returned a rounded product."""
    with pytest.raises(InvariantError) as exc:
        initial_value_inr(
            Decimal(cost), date(2025, 6, 15), fx_policy=FixedRatePolicy(Decimal("82.51"))
        )
    assert str(exc.value) == _INITIAL_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.parametrize("kind", ["open", "closed"])
def test_cost_without_a_rational_keeps_the_decimal_product(kind: str) -> None:
    """#1243 control: a lot with no exact cost converts its Decimal as before,
    with no new refusal where the product fits."""
    lot = _third_lot(kind, exact=None)
    inr = initial_value_inr_for_lot(lot, fx_policy=FixedRatePolicy(Decimal("84.15")))
    assert inr.compare_total(Decimal("280.499999999999999997195")) == 0


@pytest.mark.parametrize("kind", ["open", "closed"])
def test_cost_without_a_rational_refuses_a_product_past_the_cap(kind: str) -> None:
    """#1250: a cost with no exact rational times a 1002-digit rate needs over
    1000 digits, so the product refuses rather than round to 28 digits.
    #1243 pinned main's rounded ₹280.3703703703703703675666667."""
    lot = _third_lot(kind, exact=None)
    with pytest.raises(InvariantError) as exc:
        initial_value_inr_for_lot(lot, fx_policy=FixedRatePolicy(Decimal("84." + "1" * 1000)))
    assert str(exc.value) == _INITIAL_REFUSAL
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
