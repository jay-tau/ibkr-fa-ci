"""Tests for :class:`ibkr_fa.models.dividend.Dividend`."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import Dividend
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.types import Isin, Ticker
from tests._amount_checks import NOT_A_FINITE_DECIMAL


def _make(**overrides: object) -> Dividend:
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAA"),
        "isin": Isin("US99990A3095"),
        "payment_date": date(2026, 2, 18),
        "amount_gross": Decimal("2.80"),
        "currency": "USD",
        "description": "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
    }
    defaults.update(overrides)
    return Dividend(**defaults)  # ty: ignore[invalid-argument-type]


def test_happy_path_constructs() -> None:
    div = _make()
    assert div.symbol == "XQAA"
    assert div.isin == "US99990A3095"
    assert div.payment_date == date(2026, 2, 18)
    assert div.amount_gross == Decimal("2.80")
    assert div.currency == "USD"


def test_dataclass_is_frozen() -> None:
    """Mirror the invariant pattern of OpenLot / ClosedLot."""
    div = _make()
    with pytest.raises(AttributeError):
        div.amount_gross = Decimal("99")  # ty: ignore[invalid-assignment]


def test_empty_symbol_rejected() -> None:
    with pytest.raises(InvariantError) as exc:
        _make(symbol=Ticker(""))
    assert "symbol" in exc.value.what


def test_malformed_isin_rejected_too_short() -> None:
    with pytest.raises(InvariantError):
        _make(isin=Isin("US02"))


def test_malformed_isin_rejected_no_country_prefix() -> None:
    with pytest.raises(InvariantError):
        _make(isin=Isin("123456789012"))


def test_malformed_isin_rejected_lowercase_prefix() -> None:
    with pytest.raises(InvariantError):
        _make(isin=Isin("us99990A3095"))


@pytest.mark.parametrize("isin", ["US99992A109\n", "US99992A1!97", "ÜS99992A1097"])
def test_isin_must_match_the_whole_iso_6166_shape(isin: str) -> None:
    """#854: two capitals then any ten characters isn't an ISIN."""
    with pytest.raises(InvariantError) as exc:
        _make(isin=Isin(isin))
    assert exc.value.what == (
        "Dividend.isin must match ISO 6166 (12 chars, leading 2-letter country code)"
    )
    assert exc.value.context == (("isin", None),)


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-0.01"), Decimal("-100")])
def test_non_positive_amount_rejected(bad_amount: Decimal) -> None:
    """Dividends section is positive-only; reversals go through
    Change in Dividend Accruals. The model invariant enforces this
    at construction so a downstream sum can't be silently corrupted."""
    with pytest.raises(InvariantError) as exc:
        _make(amount_gross=bad_amount)
    assert "positive" in exc.value.what.lower()


def test_malformed_currency_rejected_lowercase() -> None:
    with pytest.raises(InvariantError):
        _make(currency="usd")


def test_malformed_currency_rejected_wrong_length() -> None:
    with pytest.raises(InvariantError):
        _make(currency="US")


def test_malformed_currency_rejected_non_alpha() -> None:
    with pytest.raises(InvariantError):
        _make(currency="US1")


def test_malformed_currency_rejected_non_ascii_capital() -> None:
    """#854: ``isupper`` and ``isalpha`` accept ``"Ü"``; ISO 4217 is ASCII."""
    with pytest.raises(InvariantError) as exc:
        _make(currency="ÜSD")
    assert exc.value.what == "Dividend.currency must be a 3-letter uppercase ISO 4217 code"
    assert exc.value.context == (("currency", None),)


def test_empty_description_rejected() -> None:
    """Description is the audit trail; an empty one would lose the
    'why was this row constructed' provenance link."""
    with pytest.raises(InvariantError):
        _make(description="")


# ---- #154 source_row_index field ----------------------------------------


def test_source_row_index_defaults_to_none() -> None:
    """#154: backwards-compat — fixture callers that don't supply
    source_row_index still construct cleanly."""
    div = _make()
    assert div.source_row_index is None


def test_source_row_index_accepts_zero() -> None:
    """#154: 0-based index — the first row IS index 0, must accept."""
    div = _make(source_row_index=0)
    assert div.source_row_index == 0


def test_source_row_index_accepts_positive_int() -> None:
    div = _make(source_row_index=7)
    assert div.source_row_index == 7


def test_source_row_index_rejects_negative() -> None:
    """#154: a negative index would mean the parser threaded a
    bogus value — fail-fast invariant."""
    with pytest.raises(InvariantError) as exc:
        _make(source_row_index=-1)
    assert "source_row_index" in exc.value.what
    assert "non-negative" in exc.value.what


# ---- #623 holding_date field ----------------------------------------------


def test_holding_date_defaults_to_none() -> None:
    assert _make().holding_date is None


def test_positional_construction_still_ends_at_source_account_index() -> None:
    div = Dividend(
        Ticker("XQAA"),
        Isin("US99990A3095"),
        date(2026, 2, 18),
        Decimal("2.80"),
        "USD",
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
        7,
        DistributionKind.CASH_DIVIDEND,
        "Cash Dividend USD",
        2,
    )
    assert div.source_row_index == 7
    assert div.source_account_index == 2
    assert div.holding_date is None


@pytest.mark.parametrize("value", [*NOT_A_FINITE_DECIMAL, None], ids=repr)
def test_amount_gross_must_be_a_finite_decimal(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_make(), amount_gross=value)
    assert exc.value.what == "Dividend.amount_gross must be a finite Decimal"


_NOT_A_KIND = [None, *(kind.value for kind in DistributionKind)]


@pytest.mark.parametrize("value", _NOT_A_KIND, ids=repr)
def test_kind_must_be_a_distribution_kind_member(value: object) -> None:
    """#938: a raw ``"Cash Dividend"`` equals its member and would pass
    Schedule OS's allow-list, then crash the writers on ``kind.value``."""
    with pytest.raises(InvariantError) as exc:
        _make(kind=value)
    assert exc.value.what == "Dividend.kind must be a DistributionKind enum member"
    assert exc.value.context == (("kind", None),)


@pytest.mark.parametrize("value", _NOT_A_KIND, ids=repr)
def test_kind_must_be_a_member_through_replace(value: object) -> None:
    with pytest.raises(InvariantError) as exc:
        replace(_make(), kind=value)
    assert exc.value.what == "Dividend.kind must be a DistributionKind enum member"


@pytest.mark.parametrize("kind", list(DistributionKind))
def test_every_distribution_kind_constructs(kind: DistributionKind) -> None:
    """The UNRECOGNISED default and the MALFORMED marker included: Schedule
    OS refuses those later, not the model."""
    assert _make(kind=kind).kind is kind
