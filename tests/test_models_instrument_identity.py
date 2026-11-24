"""Tests for the InstrumentIdentity domain model (#628)."""

from __future__ import annotations

from dataclasses import fields

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import InstrumentIdentity
from ibkr_fa.types import CountryCode, Isin, Ticker


def make_identity(**overrides: str) -> InstrumentIdentity:
    values = {
        "symbol": "XQAQ",
        "isin": "US99992A1097",
        "listing_exchange": "NASDAQ",
        "issuer_country_code": "US",
        **overrides,
    }
    return InstrumentIdentity(
        symbol=Ticker(values["symbol"]),
        isin=Isin(values["isin"]),
        listing_exchange=values["listing_exchange"],
        issuer_country_code=CountryCode(values["issuer_country_code"]),
    )


def test_field_set_is_the_shared_security_identity() -> None:
    assert [item.name for item in fields(InstrumentIdentity)] == [
        "symbol",
        "isin",
        "listing_exchange",
        "issuer_country_code",
    ]


def test_valid_us_identity() -> None:
    identity = make_identity()
    assert identity.symbol == "XQAQ"
    assert identity.isin == "US99992A1097"
    assert identity.listing_exchange == "NASDAQ"
    assert identity.issuer_country_code == "US"


def test_indian_issuer_is_allowed() -> None:
    # Robinhood configs may name an IN issuer; NetBenefits and StockPlan
    # refuse it in their own parsers.
    assert make_identity(issuer_country_code="IN").issuer_country_code == "IN"


def test_repr_does_not_disclose_the_holding() -> None:
    text = repr(make_identity())
    assert "XQAQ" not in text
    assert "US99992A1097" not in text


@pytest.mark.parametrize(
    "isin",
    [
        "US99992A109",  # too short
        "US99992A10977",  # too long
        "us99992A1097",  # lowercase country
        "US99992A109X",  # non-digit check character
        "US99992A1097\n",  # trailing newline
        "",
    ],
)
def test_isin_shape_is_refused(isin: str) -> None:
    with pytest.raises(InvariantError, match=r"InstrumentIdentity\.isin must match ISO 6166"):
        make_identity(isin=isin)


@pytest.mark.parametrize("isin", ["US99992A1098", "US99971T1035"])
def test_isin_check_digit_is_refused(isin: str) -> None:
    with pytest.raises(InvariantError, match=r"InstrumentIdentity\.isin check digit is invalid"):
        make_identity(isin=isin)


@pytest.mark.parametrize("field", ["symbol", "listing_exchange"])
def test_empty_descriptor_is_refused(field: str) -> None:
    with pytest.raises(InvariantError, match=rf"InstrumentIdentity\.{field} must be non-empty"):
        make_identity(**{field: ""})


@pytest.mark.parametrize("country", ["USA", "us", "U", "", "US\n"])
def test_issuer_country_must_be_iso_alpha2(country: str) -> None:
    with pytest.raises(InvariantError, match=r"InstrumentIdentity\.issuer_country_code must be 2"):
        make_identity(issuer_country_code=country)
