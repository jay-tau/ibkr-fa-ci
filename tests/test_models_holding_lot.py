"""Tests for the valuation-free HoldingLot model (#627)."""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import Side
from ibkr_fa.models.holding_lot import HoldingLot
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL


def make_holding_lot(**overrides: object) -> HoldingLot:
    defaults: dict[str, object] = {
        "currency": "USD",
        "symbol": Ticker("XQCB"),
        "description": "SAMPLE ISSUER CB Inc.",
        "isin": Isin("US99971P1012"),
        "issuer_country_code": CountryCode("US"),
        "quantity": Decimal("6.214"),
        "cost_basis_money": Decimal("1220.55388"),
        "acquisition_date": date(2025, 8, 14),
        "side": Side.LONG,
    }
    defaults.update(overrides)
    return HoldingLot(**defaults)  # type: ignore[arg-type]


def test_has_no_valuation_fields() -> None:
    assert {f.name for f in dataclasses.fields(HoldingLot)} == {
        "currency",
        "symbol",
        "description",
        "isin",
        "issuer_country_code",
        "quantity",
        "cost_basis_money",
        "acquisition_date",
        "side",
    }


def test_is_frozen_and_slotted() -> None:
    lot = make_holding_lot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        lot.quantity = Decimal("1")  # ty: ignore[invalid-assignment]
    assert not hasattr(lot, "__dict__")


def test_zero_cost_basis_accepted() -> None:
    assert make_holding_lot(cost_basis_money=Decimal("0")).cost_basis_money == 0


@pytest.mark.parametrize(
    ("overrides", "what"),
    [
        ({"symbol": Ticker("")}, "HoldingLot.symbol must be non-empty"),
        ({"isin": Isin("us99971p1012")}, "HoldingLot.isin must match ISO 6166"),
        ({"isin": Isin("US99971P101X")}, "HoldingLot.isin must match ISO 6166"),
        ({"currency": "usd"}, "HoldingLot.currency must be 3 uppercase ASCII letters"),
        ({"currency": "USDT"}, "HoldingLot.currency must be 3 uppercase ASCII letters"),
        # ``$`` also matches just before a final line break (#835).
        ({"currency": "USD\n"}, "HoldingLot.currency must be 3 uppercase ASCII letters"),
        ({"issuer_country_code": CountryCode("USA")}, "HoldingLot.issuer_country_code must be"),
        ({"quantity": Decimal("0")}, "HoldingLot.quantity must be positive"),
        ({"quantity": Decimal("-1")}, "HoldingLot.quantity must be positive"),
        ({"cost_basis_money": Decimal("-0.01")}, "HoldingLot.cost_basis_money must be"),
    ],
)
def test_invariant_rejected(overrides: dict[str, object], what: str) -> None:
    with pytest.raises(InvariantError) as exc:
        make_holding_lot(**overrides)
    assert what in exc.value.what


@pytest.mark.parametrize("field", ["quantity", "cost_basis_money"])
@pytest.mark.parametrize("value", [*NOT_A_FINITE_DECIMAL, None], ids=repr)
def test_amount_must_be_a_finite_decimal(field: str, value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        dataclasses.replace(make_holding_lot(), **{field: value})
    assert exc.value.what == f"HoldingLot.{field} must be a finite Decimal"


_NOT_A_SIDE = [None, *(side.value for side in Side)]


@pytest.mark.parametrize("value", _NOT_A_SIDE, ids=repr)
def test_side_must_be_a_side_member(value: object) -> None:
    """#938: a raw ``"Long"`` equals ``Side.LONG`` but isn't one."""
    with pytest.raises(InvariantError) as exc:
        make_holding_lot(side=value)
    assert exc.value.what == "HoldingLot.side must be a Side enum member"
    assert exc.value.context == (("side", None),)


@pytest.mark.parametrize("value", _NOT_A_SIDE, ids=repr)
def test_side_must_be_a_side_member_through_replace(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        dataclasses.replace(make_holding_lot(), side=value)
    assert exc.value.what == "HoldingLot.side must be a Side enum member"


@pytest.mark.parametrize("side", list(Side))
def test_every_side_constructs(side: Side) -> None:
    assert make_holding_lot(side=side).side is side
