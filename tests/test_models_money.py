"""Tests for :mod:`ibkr_fa.models.money` (#615, #830)."""

from __future__ import annotations

from decimal import Decimal, Inexact, localcontext

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.money import require_filed_amount, to_whole_rupees


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.5", "1"),
        ("-0.5", "-1"),
        ("2.4999", "2"),
        ("1000.4", "1000"),
        ("1200.6", "1201"),
    ],
)
def test_rounds_half_up_to_whole_rupees(value: str, expected: str) -> None:
    rounded = to_whole_rupees(Decimal(value))

    assert rounded == Decimal(expected)
    assert rounded.as_tuple().exponent == 0


@pytest.mark.parametrize("value", ["-0.1", "-0.4999", "-0", "-0.00"])
def test_never_returns_signed_zero(value: str) -> None:
    rounded = to_whole_rupees(Decimal(value))

    assert rounded == 0
    assert not rounded.is_signed()
    assert str(rounded) == "0"


@pytest.mark.parametrize("value", ["0", "7", "-7", "123456789012345"])
def test_whole_values_are_unchanged(value: str) -> None:
    assert str(to_whole_rupees(Decimal(value))) == value


@pytest.mark.parametrize("value", ["1E+2", "5.00"])
def test_whole_values_get_exponent_zero(value: str) -> None:
    rounded = to_whole_rupees(Decimal(value))

    assert rounded == Decimal(value)
    assert rounded.as_tuple().exponent == 0


# ---- the CBDT 14-digit bound (#830) ----------------------------------------


@pytest.mark.parametrize(
    "value", ["99999999999999", "99999999999999.4", "-99999999999999.4", "0", "1E-100"]
)
def test_filed_amount_within_the_cbdt_bound_accepted(value: str) -> None:
    require_filed_amount(Decimal(value), what="Schedule OS 1a(i) dividend total")


@pytest.mark.parametrize(
    "value", ["99999999999999.5", "-99999999999999.5", "100000000000000", "1E100", "-1E100"]
)
def test_filed_amount_beyond_the_cbdt_bound_rejected(value: str) -> None:
    with pytest.raises(InvariantError) as exc:
        require_filed_amount(Decimal(value), what="Schedule OS 1a(i) dividend total", period=2)

    assert exc.value.what == (
        "Schedule OS 1a(i) dividend total must round to a whole-rupee amount within "
        "±99,999,999,999,999 (the CBDT 14-digit bound)"
    )
    assert exc.value.context == (("value", None), ("period", None))


def test_filed_amount_bound_is_independent_of_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 14
        context.traps[Inexact] = True
        require_filed_amount(Decimal("-99999999999999.4"), what="total")
        with pytest.raises(InvariantError, match="total must round"):
            require_filed_amount(Decimal("-99999999999999.5"), what="total")
