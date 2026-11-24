"""Tests for :class:`ibkr_fa.models.other_source_income.OtherSourceIncome`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import OtherSourceIncome
from ibkr_fa.types import CountryCode


def _make(**overrides: object) -> OtherSourceIncome:
    defaults: dict[str, object] = {
        "nature": "Securities lending fee",
        "payment_date": date(2025, 6, 1),
        "amount_gross": Decimal("0.11"),
        "currency": "USD",
        "description": "Stock Lending",
        "source_country_iso2": CountryCode("US"),
    }
    defaults.update(overrides)
    return OtherSourceIncome(**defaults)  # ty: ignore[invalid-argument-type]


def test_happy_path_constructs() -> None:
    income = _make()
    assert income.currency == "USD"
    assert income.source_country_iso2 == "US"


@pytest.mark.parametrize(
    ("overrides", "what"),
    [
        ({"currency": "ÜSD"}, "OtherSourceIncome.currency must be uppercase ISO 4217"),
        (
            {"source_country_iso2": CountryCode("ÜS")},
            "OtherSourceIncome.source_country_iso2 must be foreign uppercase ISO alpha-2",
        ),
    ],
    ids=["currency", "source-country"],
)
def test_letter_code_with_a_non_ascii_capital_rejected(
    overrides: dict[str, object], what: str
) -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO codes are ASCII."""
    with pytest.raises(InvariantError) as exc:
        _make(**overrides)
    assert exc.value.what == what
    assert exc.value.context == ()
