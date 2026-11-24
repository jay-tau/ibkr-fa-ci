"""Tests for OpenLot domain model."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from decimal import Context, Decimal, localcontext
from fractions import Fraction

import pytest

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL


def make_lot(**overrides: object) -> OpenLot:
    """Factory with sensible defaults; overrides supplied via kwargs.

    Defaults are a synthetic XQAW lot: 25 shares bought at 38.75 with a
    1.00 commission (cost 969.75), marked at 45.10.
    """
    defaults: dict[str, object] = {
        "currency_primary": "USD",
        "symbol": Ticker("XQAW"),
        "description": "SAMPLE ISSUER AW INC",
        "isin": Isin("US99990E1038"),
        "issuer_country_code": CountryCode("US"),
        "report_date": date(2026, 12, 31),
        "quantity": Decimal("25"),
        "mark_price": Decimal("45.10"),
        "cost_basis_money": Decimal("969.75"),
        "side": Side.LONG,
        "open_date_time": date(2026, 6, 16),
    }
    defaults.update(overrides)
    return OpenLot(**defaults)  # ty: ignore[invalid-argument-type]


def test_open_lot_constructs_with_sample_xqaw_row() -> None:
    lot = make_lot()
    assert lot.symbol == "XQAW"
    assert lot.isin == "US99990E1038"
    assert lot.quantity == Decimal("25")
    assert lot.cost_basis_money == Decimal("969.75")
    assert lot.side is Side.LONG


def test_open_lot_field_set_is_pinned() -> None:
    """#629 dropped the fields nothing read; adding one back is a decision."""
    assert [f.name for f in fields(OpenLot)] == [
        "currency_primary",
        "symbol",
        "description",
        "isin",
        "issuer_country_code",
        "report_date",
        "quantity",
        "mark_price",
        "cost_basis_money",
        "side",
        "open_date_time",
        "closing_total_usd",
        "cost_basis_money_exact",
        "closing_total_usd_exact",
        "quantity_exact",
    ]


def test_open_lot_is_frozen() -> None:
    lot = make_lot()
    with pytest.raises(AttributeError):
        lot.quantity = Decimal("26")  # ty: ignore[invalid-assignment]


def test_open_lot_uses_slots() -> None:
    lot = make_lot()
    assert not hasattr(lot, "__dict__")


def test_side_enum_values_match_ibkr_strings() -> None:
    assert Side.LONG.value == "Long"
    assert Side.SHORT.value == "Short"
    # StrEnum: instance compares equal to its underlying string.
    assert Side.LONG == "Long"


def test_open_lot_decimals_preserve_ibkr_precision() -> None:
    """A two-decimal CostBasisMoney must round-trip exactly."""
    lot = make_lot()
    assert str(lot.cost_basis_money) == "969.75"


def test_xqad_lot_has_dutch_issuer_country_despite_nasdaq_listing() -> None:
    """Issuer country != listing exchange country for a synthetic Dutch
    issuer listed on NASDAQ (XQAD)."""
    lot = make_lot(
        symbol=Ticker("XQAD"),
        description="SAMPLE ISSUER AD NV",
        isin=Isin("NL99971G1013"),
        issuer_country_code=CountryCode("NL"),
        quantity=Decimal("14"),
        mark_price=Decimal("62.35"),
        cost_basis_money=Decimal("812.58"),
        open_date_time=date(2026, 6, 23),
    )
    assert lot.issuer_country_code == "NL"


# ============================================================================
# Construction-time invariants (PR #122, issue #116)
# ============================================================================
# Per-invariant negative tests. Each verifies that the indicated bad
# input raises InvariantError with the expected `what`. Positive
# coverage of all invariants is provided by every other test in this
# file (and across the codebase) that calls make_lot() with defaults.


def test_invariant_empty_symbol_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(symbol=Ticker(""))
    assert "symbol must be non-empty" in exc.value.what


@pytest.mark.parametrize(
    "bad_isin",
    [
        "",  # empty
        "US12345",  # too short
        "US99971T103512",  # too long
        "us99990e1038",  # lowercase
        "1S99990E1038",  # first char numeric
        "US-9990E1038",  # invalid char
        "US99990E103A",  # last char not numeric
    ],
)
def test_invariant_malformed_isin_rejected(bad_isin: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(isin=Isin(bad_isin))
    assert "isin must match ISO 6166" in exc.value.what


def test_malformed_isin_refusal_names_the_key_not_the_value() -> None:
    """A shifted cell can put a name in the ISIN; the message names only the
    ``isin`` key (#1046), and ``context`` keeps the key."""
    with pytest.raises(InvariantError) as exc:
        make_lot(isin=Isin("Synthetic Holder Two"))
    assert str(exc.value) == (
        "InvariantError: OpenLot.isin must match ISO 6166 (12 chars: 2 uppercase "
        "letters + 9 alphanumerics + 1 digit) (context keys: isin)"
    )
    assert exc.value.context == (("isin", None),)


@pytest.mark.parametrize("bad_currency", ["", "us", "USDT", "us1"])
def test_invariant_malformed_currency_rejected(bad_currency: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(currency_primary=bad_currency)
    assert "currency_primary" in exc.value.what


@pytest.mark.parametrize("bad_cc", ["", "USA", "u1", "us"])
def test_invariant_malformed_issuer_country_code_rejected(bad_cc: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(issuer_country_code=CountryCode(bad_cc))
    assert "issuer_country_code" in exc.value.what


@pytest.mark.parametrize("bad_qty", [Decimal("0"), Decimal("-1"), Decimal("-10.5")])
def test_invariant_non_positive_quantity_rejected(bad_qty: Decimal) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(quantity=bad_qty)
    assert "quantity must be positive" in exc.value.what


def test_invariant_negative_mark_price_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(mark_price=Decimal("-1"))
    assert "mark_price must be non-negative" in exc.value.what


def test_invariant_negative_cost_basis_money_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(cost_basis_money=Decimal("-100"))
    assert "cost_basis_money" in exc.value.what


@pytest.mark.parametrize("bad_year", [1989, 2100])
def test_invariant_implausible_report_year_rejected(bad_year: int) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(
            report_date=date(bad_year, 12, 31),
            open_date_time=date(bad_year, 6, 16),
        )
    assert "report_date.year must be in" in exc.value.what


def test_invariant_open_date_after_report_date_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(
            report_date=date(2025, 12, 31),
            open_date_time=date(2026, 1, 5),
        )
    assert "open_date_time must be on or before report_date" in exc.value.what


def test_invariant_open_date_equal_to_report_date_accepted() -> None:
    """Boundary case: a lot opened on Dec 31 of the report year is
    valid (it overlapped CY for one day)."""
    lot = make_lot(
        report_date=date(2025, 12, 31),
        open_date_time=date(2025, 12, 31),
    )
    assert lot.open_date_time == lot.report_date


def test_invariant_short_side_with_positive_quantity_accepted() -> None:
    """The Open Positions snapshot uses unsigned quantity with Side
    tracked separately. A SHORT lot with positive quantity must NOT
    be rejected (it's how IBKR represents shorts in this section);
    the downstream filter in lot_overlap drops shorts from FA output."""
    lot = make_lot(side=Side.SHORT)
    assert lot.side is Side.SHORT
    assert lot.quantity == Decimal("25")


@pytest.mark.parametrize("field", ["quantity", "mark_price", "cost_basis_money"])
@pytest.mark.parametrize("value", [*NOT_A_FINITE_DECIMAL, None], ids=repr)
def test_amount_must_be_a_finite_decimal(field: str, value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(make_lot(), **{field: value})
    assert exc.value.what == f"OpenLot.{field} must be a finite Decimal"


def test_infinite_quantity_refused_when_the_lot_is_built() -> None:
    """#936: the A3 closing value would multiply it by the price and raise
    a raw decimal error far from the lot."""
    with pytest.raises(InvariantError) as exc:
        replace(make_lot(), quantity=Decimal("Infinity"), mark_price=Decimal("0"))
    assert exc.value.what == "OpenLot.quantity must be a finite Decimal"


def test_zero_mark_price_accepted() -> None:
    assert replace(make_lot(), mark_price=Decimal("0")).mark_price == 0


def test_closing_total_defaults_to_none_and_accepts_zero() -> None:
    """#1242: only a lot holding a whole statement total sets it."""
    assert make_lot().closing_total_usd is None
    assert make_lot(closing_total_usd=Decimal("0")).closing_total_usd == 0


@pytest.mark.parametrize("value", NOT_A_FINITE_DECIMAL, ids=repr)
def test_closing_total_must_be_a_finite_decimal_or_none(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(closing_total_usd=value)
    assert (
        str(exc.value)
        == "InvariantError: OpenLot.closing_total_usd must be a finite Decimal or None"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_negative_closing_total_refused_without_its_value() -> None:
    with pytest.raises(InvariantError) as exc:
        make_lot(closing_total_usd=Decimal("-1.25"))
    assert (
        str(exc.value) == "InvariantError: OpenLot.closing_total_usd must be None or non-negative"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


# ---- exact amounts (#1243) --------------------------------------------------

_EXACT_AMOUNTS = ["cost_basis_money", "closing_total_usd"]


def test_exact_amounts_default_to_none() -> None:
    """#1243: a lot whose Decimal amounts are exact carries no rationals."""
    lot = make_lot(closing_total_usd=Decimal("1127.50"))
    assert (lot.cost_basis_money_exact, lot.closing_total_usd_exact) == (None, None)


@pytest.mark.parametrize("field", _EXACT_AMOUNTS)
def test_exact_amount_is_accepted_beside_its_cut(field: str) -> None:
    """#1243: a third of $10.00 is kept exactly beside the Decimal it cuts to."""
    lot = make_lot(**{field: exact_decimal(Fraction(10, 3)), f"{field}_exact": Fraction(10, 3)})
    assert getattr(lot, field) == Decimal("3.333333333333333333333333333")
    assert getattr(lot, f"{field}_exact") == Fraction(10, 3)


@pytest.mark.parametrize("field", _EXACT_AMOUNTS)
@pytest.mark.parametrize(
    ("amount", "exact"),
    [
        pytest.param("3.334", Fraction(10, 3), id="rounded-up"),
        pytest.param("3.332", Fraction(10, 3), id="short-by-a-unit"),
        pytest.param("3.5", Decimal("3.5"), id="decimal"),
        pytest.param("3", 3, id="int"),
        pytest.param("3.5", 3.5, id="float"),
    ],
)
def test_exact_amount_must_be_the_rational_its_decimal_cuts(
    field: str, amount: str, exact: object
) -> None:
    """#1243: the exact field must be a ``Fraction`` its Decimal is a cut of."""
    with pytest.raises(InvariantError) as exc:
        make_lot(**{field: Decimal(amount), f"{field}_exact": exact})
    assert str(exc.value) == (
        f"InvariantError: OpenLot.{field}_exact must be the exact amount {field} rounds"
    )
    assert exc.value.context == ()


def test_exact_closing_total_needs_a_closing_total() -> None:
    """#1243: a share of a statement total is only kept beside the total it cuts to."""
    with pytest.raises(InvariantError) as exc:
        make_lot(closing_total_usd_exact=Fraction(10, 3))
    assert str(exc.value) == (
        "InvariantError: OpenLot.closing_total_usd_exact must be the exact amount "
        "closing_total_usd rounds"
    )
    assert exc.value.context == ()


# ---- exact quantity (#1263) -------------------------------------------------


def test_exact_quantity_defaults_to_none() -> None:
    """#1263: a lot whose Decimal quantity is exact carries no rational quantity."""
    assert make_lot().quantity_exact is None


def test_exact_quantity_is_accepted_beside_its_cut() -> None:
    """#1263: two thirds of a share, left by a same-day pro-rata sale, is kept exactly
    beside the 60-digit Decimal it cuts to."""
    with localcontext(Context(prec=60)):
        quantity = exact_decimal(Fraction(2, 3))
    lot = make_lot(quantity=quantity, quantity_exact=Fraction(2, 3))
    assert lot.quantity == Decimal("0." + "6" * 60)
    assert lot.quantity_exact == Fraction(2, 3)


@pytest.mark.parametrize(
    ("quantity", "exact"),
    [
        pytest.param("0.66666666666666666667", Fraction(2, 3), id="rounded-up"),
        pytest.param("0.5", Decimal("0.5"), id="decimal"),
        pytest.param("2", 2, id="int"),
        pytest.param("0.5", 0.5, id="float"),
    ],
)
def test_exact_quantity_must_be_the_rational_its_decimal_cuts(quantity: str, exact: object) -> None:
    """#1263: the exact quantity must be a ``Fraction`` the Decimal quantity is a cut of."""
    with pytest.raises(InvariantError) as exc:
        make_lot(quantity=Decimal(quantity), quantity_exact=exact)
    assert str(exc.value) == (
        "InvariantError: OpenLot.quantity_exact must be the exact amount quantity rounds"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


_NOT_A_SIDE = [None, *(side.value for side in Side)]


@pytest.mark.parametrize("value", _NOT_A_SIDE, ids=repr)
def test_side_must_be_a_side_member(value: object) -> None:
    """#938: a raw ``"Long"`` equals ``Side.LONG`` but isn't one."""
    with pytest.raises(InvariantError) as exc:
        make_lot(side=value)
    assert exc.value.what == "OpenLot.side must be a Side enum member"
    assert exc.value.context == (("side", None),)


@pytest.mark.parametrize("value", _NOT_A_SIDE, ids=repr)
def test_side_must_be_a_side_member_through_replace(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(make_lot(), side=value)
    assert exc.value.what == "OpenLot.side must be a Side enum member"


@pytest.mark.parametrize("side", list(Side))
def test_every_side_constructs(side: Side) -> None:
    assert make_lot(side=side).side is side
