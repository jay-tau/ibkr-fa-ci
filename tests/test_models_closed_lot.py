"""Tests for ClosedLot domain model."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
from decimal import Context, Decimal, localcontext
from fractions import Fraction

import pytest

from ibkr_fa.compute.exact_inr import exact_decimal
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL


def make_closed_lot(**overrides: object) -> ClosedLot:
    """Factory with sensible defaults; overrides supplied via kwargs.

    Defaults are a synthetic XQAP round trip, opened and closed on the
    same day: 16 shares bought at 128.75 with a 1.00 commission (cost
    2061.00), sold at 128.25 less a 1.00 commission (proceeds 2051.00).
    The Closed Lots section schema used here is the IBKR Flex Reference
    best-guess pending gate G3 verification.
    """
    defaults: dict[str, object] = {
        "currency_primary": "USD",
        "sub_category": "COMMON",
        "symbol": Ticker("XQAP"),
        "description": "SAMPLE ISSUER AP CORP",
        "isin": Isin("US99990F1003"),
        "listing_exchange": "NASDAQ",
        "issuer_country_code": CountryCode("US"),
        "quantity": Decimal("16"),
        "cost_basis_money": Decimal("2061.00"),
        "proceeds_money": Decimal("2051.00"),
        "open_date_time": date(2026, 5, 12),
        "close_date_time": date(2026, 5, 12),
        "basis_provenance": BasisProvenance.ENGINE_FIFO,
    }
    defaults.update(overrides)
    return ClosedLot(**defaults)  # ty: ignore[invalid-argument-type]


def test_closed_lot_constructs_with_round_trip_xqap() -> None:
    lot = make_closed_lot()
    assert lot.symbol == "XQAP"
    assert lot.quantity == Decimal("16")
    assert lot.cost_basis_money == Decimal("2061.00")
    assert lot.proceeds_money == Decimal("2051.00")
    assert lot.open_date_time == date(2026, 5, 12)
    assert lot.close_date_time == date(2026, 5, 12)


def test_closed_lot_field_set_is_pinned() -> None:
    """#629 dropped the fields nothing read; adding one back is a decision."""
    assert [f.name for f in fields(ClosedLot)] == [
        "currency_primary",
        "sub_category",
        "symbol",
        "description",
        "isin",
        "listing_exchange",
        "issuer_country_code",
        "quantity",
        "cost_basis_money",
        "proceeds_money",
        "open_date_time",
        "close_date_time",
        "gross_proceeds_money",
        "basis_provenance",
        "holding_period_date_time",
        "cost_basis_inr_override",
        "cost_basis_money_exact",
        "proceeds_money_exact",
        "quantity_exact",
        "gross_proceeds_money_exact",
    ]


def test_source_without_exchange_or_security_class_states_none() -> None:
    lot = make_closed_lot(sub_category=None, listing_exchange=None)
    assert lot.sub_category is None
    assert lot.listing_exchange is None


def test_closed_lot_is_frozen() -> None:
    lot = make_closed_lot()
    with pytest.raises(AttributeError):
        lot.quantity = Decimal("17")  # ty: ignore[invalid-assignment]


def test_closed_lot_uses_slots() -> None:
    lot = make_closed_lot()
    assert not hasattr(lot, "__dict__")


def test_closed_lot_quantity_is_unsigned() -> None:
    """Closed-lot quantity is the disposed amount; sign is implied by section."""
    lot = make_closed_lot(quantity=Decimal("16"))
    assert lot.quantity > 0


def test_closed_lot_decimals_round_trip_exactly() -> None:
    lot = make_closed_lot(
        cost_basis_money=Decimal("2187.45"),
        proceeds_money=Decimal("2051.00"),
    )
    # 2051.00 - 2187.45 = -136.45
    assert lot.proceeds_money - lot.cost_basis_money == Decimal("-136.45")


def test_closed_lot_supports_cross_year_holding() -> None:
    """Lot opened in prior CY, closed in target CY — common for Schedule FA."""
    lot = make_closed_lot(
        open_date_time=date(2025, 11, 15),
        close_date_time=date(2026, 3, 1),
    )
    assert lot.open_date_time.year == 2025
    assert lot.close_date_time.year == 2026
    assert lot.close_date_time > lot.open_date_time


def test_closed_lot_supports_dutch_issuer_listed_in_us() -> None:
    """Issuer country != listing-exchange country (a synthetic Dutch
    issuer on NASDAQ)."""
    lot = make_closed_lot(
        symbol=Ticker("XQAD"),
        description="SAMPLE ISSUER AD NV",
        isin=Isin("NL99971G1013"),
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("NL"),
    )
    assert lot.issuer_country_code == "NL"
    assert lot.listing_exchange == "NASDAQ"


# ============================================================================
# Construction-time invariants (PR #122, issue #116)
# ============================================================================


def test_invariant_empty_symbol_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(symbol=Ticker(""))
    assert "symbol must be non-empty" in exc.value.what


def test_invariant_malformed_isin_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(isin=Isin("not-an-isin"))
    assert "isin must match ISO 6166" in exc.value.what


def test_invariant_malformed_currency_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(currency_primary="usd")
    assert "currency_primary" in exc.value.what


def test_invariant_malformed_country_code_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(issuer_country_code=CountryCode("USA"))
    assert "issuer_country_code" in exc.value.what


_ISIN_WHAT = (
    "ClosedLot.isin must match ISO 6166 (12 chars: 2 uppercase letters + 9 alphanumerics + 1 digit)"
)


@pytest.mark.parametrize(
    ("field", "value", "what"),
    [
        ("isin", "BADISIN", _ISIN_WHAT),
        # ``$`` also matches just before a final line break (#835).
        ("isin", "US99990F1003\n", _ISIN_WHAT),
        (
            "currency_primary",
            "USD\n",
            "ClosedLot.currency_primary must be 3 uppercase ASCII letters (ISO 4217)",
        ),
        (
            "issuer_country_code",
            "US\n",
            "ClosedLot.issuer_country_code must be 2 uppercase ASCII letters (ISO 3166-1 alpha-2)",
        ),
    ],
)
def test_invariant_malformed_identifier_error(field: str, value: str, what: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(**{field: value})
    assert exc.value.what == what
    assert exc.value.context == ((field, None),)


@pytest.mark.parametrize("bad_qty", [Decimal("0"), Decimal("-1")])
def test_invariant_non_positive_quantity_rejected(bad_qty: Decimal) -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(
            quantity=bad_qty,
            cost_basis_money=Decimal("100"),
            proceeds_money=Decimal("90"),
        )
    assert "quantity must be positive" in exc.value.what


def test_invariant_negative_cost_basis_money_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(
            cost_basis_money=Decimal("-100"),
            proceeds_money=Decimal("-100"),
        )
    assert "cost_basis_money must be non-negative" in exc.value.what


def test_invariant_negative_gross_proceeds_money_rejected() -> None:
    """#348: gross_proceeds_money (quantity * T.Price) is non-negative
    by construction; a negative value signals a buggy parser."""
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(gross_proceeds_money=Decimal("-1"))
    assert "gross_proceeds_money must be non-negative" in exc.value.what


def test_gross_proceeds_money_defaults_to_none() -> None:
    """Sources that can't supply the raw price (Flex / NetBenefits)
    omit gross_proceeds_money; FA-A3 then falls back to proceeds_money."""
    assert make_closed_lot().gross_proceeds_money is None


def test_invariant_close_before_open_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(
            open_date_time=date(2025, 6, 15),
            close_date_time=date(2025, 6, 14),
        )
    assert "close_date_time must be on or after open_date_time" in exc.value.what


def test_invariant_close_equals_open_accepted() -> None:
    """Day-trade boundary: open and close on the same day is valid."""
    lot = make_closed_lot(
        open_date_time=date(2025, 6, 15),
        close_date_time=date(2025, 6, 15),
    )
    assert lot.open_date_time == lot.close_date_time


# ---- basis provenance (#640) ------------------------------------------------


def test_broker_matched_lot_carries_its_holding_period_date() -> None:
    lot = make_closed_lot(
        basis_provenance=BasisProvenance.BROKER_MATCHED,
        holding_period_date_time=date(2026, 4, 21),
    )
    assert lot.holding_period_date_time == date(2026, 4, 21)


@pytest.mark.parametrize(
    ("provenance", "holding"),
    [
        (BasisProvenance.BROKER_MATCHED, None),
        (BasisProvenance.ENGINE_FIFO, date(2026, 5, 12)),
        (BasisProvenance.S49_2AA, date(2026, 5, 12)),
    ],
)
def test_invariant_holding_period_date_only_on_broker_matched_lots(
    provenance: BasisProvenance, holding: date | None
) -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(basis_provenance=provenance, holding_period_date_time=holding)
    assert "required for, and only for, BROKER_MATCHED lots" in exc.value.what


def test_invariant_basis_provenance_must_be_an_enum_member() -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(basis_provenance="engine_fifo")
    assert "basis_provenance must be a BasisProvenance enum member" in exc.value.what


# ---- statutory INR cost override (#633) -------------------------------------

_S49_COST = StatutoryInrCostBasis(amount_inr=Decimal("24000"), source="perquisite worksheet")


def test_existing_constructors_default_to_no_override() -> None:
    assert make_closed_lot().cost_basis_inr_override is None


def test_s49_2aa_lot_carries_its_statutory_inr_cost() -> None:
    lot = make_closed_lot(
        basis_provenance=BasisProvenance.S49_2AA, cost_basis_inr_override=_S49_COST
    )
    assert lot.cost_basis_inr_override == _S49_COST


def test_statutory_inr_cost_may_be_zero() -> None:
    assert StatutoryInrCostBasis(amount_inr=Decimal("0"), source="x").amount_inr == 0


@pytest.mark.parametrize("amount", ["-0.01", "NaN", "Infinity"])
def test_statutory_inr_cost_must_be_finite_and_non_negative(amount: str) -> None:
    with pytest.raises(InvariantError) as exc:
        StatutoryInrCostBasis(amount_inr=Decimal(amount), source="perquisite worksheet")
    assert "amount_inr must be finite and non-negative" in exc.value.what


@pytest.mark.parametrize("source", ["", "  \t"])
def test_statutory_inr_cost_source_must_be_non_blank(source: str) -> None:
    with pytest.raises(InvariantError) as exc:
        StatutoryInrCostBasis(amount_inr=Decimal("24000"), source=source)
    assert "source must be non-blank" in exc.value.what


@pytest.mark.parametrize(
    ("provenance", "holding", "override"),
    [
        (BasisProvenance.S49_2AA, None, None),
        (BasisProvenance.ENGINE_FIFO, None, _S49_COST),
        (BasisProvenance.BROKER_MATCHED, date(2026, 5, 12), _S49_COST),
    ],
)
def test_invariant_override_only_on_s49_2aa_lots(
    provenance: BasisProvenance,
    holding: date | None,
    override: StatutoryInrCostBasis | None,
) -> None:
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(
            basis_provenance=provenance,
            holding_period_date_time=holding,
            cost_basis_inr_override=override,
        )
    assert "cost_basis_inr_override is required for, and only for, S49_2AA lots" in exc.value.what


@pytest.mark.parametrize("field", ["quantity", "cost_basis_money", "proceeds_money"])
@pytest.mark.parametrize("value", [*NOT_A_FINITE_DECIMAL, None], ids=repr)
def test_amount_must_be_a_finite_decimal(field: str, value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(make_closed_lot(), **{field: value})
    assert exc.value.what == f"ClosedLot.{field} must be a finite Decimal"


@pytest.mark.parametrize("value", NOT_A_FINITE_DECIMAL, ids=repr)
def test_gross_proceeds_money_must_be_a_finite_decimal_or_none(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(make_closed_lot(), gross_proceeds_money=value)
    assert exc.value.what == "ClosedLot.gross_proceeds_money must be a finite Decimal or None"


def test_gross_proceeds_money_may_be_none() -> None:
    lot = make_closed_lot(gross_proceeds_money=Decimal("2052.00"))
    assert replace(lot, gross_proceeds_money=None).gross_proceeds_money is None


def test_negative_proceeds_money_accepted() -> None:
    """Net proceeds go negative when fees exceed the sale price."""
    assert replace(make_closed_lot(), proceeds_money=Decimal("-5")).proceeds_money < 0


# ---- exact amounts (#1241; gross proceeds #1383) ----------------------------

_EXACT_AMOUNTS = ["cost_basis_money", "proceeds_money", "gross_proceeds_money"]


def test_exact_amounts_default_to_none() -> None:
    """#1241: a lot whose Decimal amounts are exact carries no rationals."""
    lot = make_closed_lot()
    assert (lot.cost_basis_money_exact, lot.proceeds_money_exact) == (None, None)


@pytest.mark.parametrize("field", _EXACT_AMOUNTS)
def test_exact_amount_is_accepted_beside_its_cut(field: str) -> None:
    """#1241: a third of $10.00 is kept exactly beside the Decimal it cuts to."""
    lot = make_closed_lot(
        **{field: exact_decimal(Fraction(10, 3)), f"{field}_exact": Fraction(10, 3)}
    )
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
    """#1241: the exact field must be a ``Fraction`` its Decimal is a cut of."""
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(**{field: Decimal(amount), f"{field}_exact": exact})
    assert str(exc.value) == (
        f"InvariantError: ClosedLot.{field}_exact must be the exact amount {field} rounds"
    )


def test_exact_gross_proceeds_needs_its_decimal() -> None:
    """#1383: an exact gross proceeds beside no Decimal gross refuses."""
    with pytest.raises(InvariantError) as exc:
        make_closed_lot(gross_proceeds_money_exact=Fraction(10, 3))
    assert str(exc.value) == (
        "InvariantError: ClosedLot.gross_proceeds_money_exact must be the exact amount "
        "gross_proceeds_money rounds"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


# ---- exact quantity (#1263) -------------------------------------------------


def test_exact_quantity_defaults_to_none() -> None:
    """#1263: a lot whose Decimal quantity is exact carries no rational quantity."""
    assert make_closed_lot().quantity_exact is None


def test_exact_quantity_is_accepted_beside_its_cut() -> None:
    """#1263: two thirds of a share, given up in a same-day pro-rata sale, is kept exactly
    beside the 60-digit Decimal it cuts to."""
    with localcontext(Context(prec=60)):
        quantity = exact_decimal(Fraction(2, 3))
    lot = make_closed_lot(quantity=quantity, quantity_exact=Fraction(2, 3))
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
        make_closed_lot(quantity=Decimal(quantity), quantity_exact=exact)
    assert str(exc.value) == (
        "InvariantError: ClosedLot.quantity_exact must be the exact amount quantity rounds"
    )
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
