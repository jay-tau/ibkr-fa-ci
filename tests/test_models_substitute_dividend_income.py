"""Tests for :class:`ibkr_fa.models.substitute_dividend_income.SubstituteDividendIncome`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import SubstituteDividendIncome
from ibkr_fa.types import CountryCode, Isin, Ticker


def _make(**overrides: object) -> SubstituteDividendIncome:
    defaults: dict[str, object] = {
        "symbol": Ticker("TEST"),
        "isin": Isin("BM99970C1019"),
        "issuer_country_iso2": CountryCode("BM"),
        "source_country_iso2": CountryCode("US"),
        "payment_date": date(2025, 6, 1),
        "amount_gross": Decimal("1.00"),
        "currency": "USD",
        "description": "synthetic substitute dividend",
    }
    defaults.update(overrides)
    return SubstituteDividendIncome(**defaults)  # ty: ignore[invalid-argument-type]


def test_happy_path_constructs() -> None:
    income = _make()
    assert income.isin == "BM99970C1019"
    assert income.issuer_country_iso2 == "BM"


def test_isin_must_agree_with_its_issuer_country() -> None:
    with pytest.raises(InvariantError) as exc:
        _make(issuer_country_iso2=CountryCode("US"))
    assert exc.value.what == "SubstituteDividendIncome ISIN must agree with its issuer country"
    assert exc.value.context == (("isin", None), ("issuer_country_iso2", None))


@pytest.mark.parametrize("isin", ["US99992A109\n", "US99992A1!97", "ÜS99992A1097"])
def test_isin_must_match_the_whole_iso_6166_shape(isin: str) -> None:
    """#854: the issuer country agrees with the first two characters, so only
    the shape check can refuse the rest."""
    with pytest.raises(InvariantError) as exc:
        _make(isin=Isin(isin), issuer_country_iso2=CountryCode(isin[:2]))
    assert exc.value.what == (
        "SubstituteDividendIncome.isin must match ISO 6166 (12 chars: 2 uppercase "
        "letters + 9 alphanumerics + 1 digit)"
    )
    assert exc.value.context == (("isin", None),)


@pytest.mark.parametrize(
    ("overrides", "what", "context"),
    [
        (
            {"source_country_iso2": CountryCode("ÜS")},
            "SubstituteDividendIncome must be foreign-source",
            (("source_country_iso2", None),),
        ),
        (
            {"currency": "ÜSD"},
            "SubstituteDividendIncome.currency must be uppercase ISO 4217",
            (("currency", None),),
        ),
    ],
    ids=["source-country", "currency"],
)
def test_letter_code_with_a_non_ascii_capital_rejected(
    overrides: dict[str, object], what: str, context: tuple[tuple[str, None], ...]
) -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO codes are ASCII."""
    with pytest.raises(InvariantError) as exc:
        _make(**overrides)
    assert exc.value.what == what
    assert exc.value.context == context
