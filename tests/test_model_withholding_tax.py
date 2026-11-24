"""Tests for :class:`ibkr_fa.models.withholding_tax.WithholdingTax`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import WithholdingTax
from ibkr_fa.types import Isin, Ticker


def _make(**overrides: object) -> WithholdingTax:
    defaults: dict[str, object] = {
        "symbol": Ticker("XQAA"),
        "isin": Isin("US99990A3095"),
        "payment_date": date(2026, 2, 18),
        "amount": Decimal("-0.70"),
        "currency": "USD",
        "description": "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax",
    }
    defaults.update(overrides)
    return WithholdingTax(**defaults)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_happy_path_constructs() -> None:
    wht = _make()
    assert wht.symbol == "XQAA"
    assert wht.isin == "US99990A3095"
    assert wht.payment_date == date(2026, 2, 18)
    assert wht.amount == Decimal("-0.70")
    assert wht.currency == "USD"


def test_dataclass_is_frozen() -> None:
    """Mirror the invariant pattern of Dividend / OpenLot / ClosedLot."""
    wht = _make()
    with pytest.raises(AttributeError):
        wht.amount = Decimal("99")  # ty: ignore[invalid-assignment]


def test_positive_amount_accepted_as_reversal() -> None:
    """Key divergence from :class:`Dividend`: WHT amounts are
    SIGNED. Positive = reversal/refund of a previously-paid WHT
    (IBKR convention). Both signs must round-trip cleanly."""
    wht = _make(amount=Decimal("0.70"))
    assert wht.amount == Decimal("0.70")


def test_negative_amount_accepted_as_tax_paid() -> None:
    """The normal case: IBKR emits WHT as a debit (money leaving
    the cash balance) with a negative sign."""
    wht = _make(amount=Decimal("-0.70"))
    assert wht.amount == Decimal("-0.70")


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
        "WithholdingTax.isin must match ISO 6166 (12 chars, leading 2-letter country code)"
    )
    assert exc.value.context == (("isin", None),)


def test_zero_amount_rejected() -> None:
    """IBKR doesn't emit zero-tax rows. A zero would corrupt
    downstream Schedule FSI / Form 67 totals if silently allowed."""
    with pytest.raises(InvariantError) as exc:
        _make(amount=Decimal("0"))
    assert "non-zero" in exc.value.what.lower()


@pytest.mark.parametrize(
    "bad_amount",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_non_finite_amount_rejected(bad_amount: Decimal) -> None:
    """NaN/Infinity would propagate into per-payment INR conversion
    and corrupt totals — rejected at construction."""
    with pytest.raises(InvariantError):
        _make(amount=bad_amount)


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
    assert exc.value.what == "WithholdingTax.currency must be a 3-letter uppercase ISO 4217 code"
    assert exc.value.context == (("currency", None),)


def test_empty_description_rejected() -> None:
    """Description is the audit-trail link to the originating
    Dividend row's description on the same payment date."""
    with pytest.raises(InvariantError):
        _make(description="")


def test_source_account_index_must_be_non_negative() -> None:
    """#890: Form 67 keys payments on the account, as Dividend does."""
    assert _make().source_account_index is None
    assert _make(source_account_index=0).source_account_index == 0
    with pytest.raises(InvariantError, match="source_account_index must be non-negative"):
        _make(source_account_index=-1)


@pytest.mark.parametrize(
    "index", ["0", [], Decimal("NaN"), Decimal("sNaN"), Decimal("0.5"), 0.5, True, False]
)
def test_source_account_index_must_be_an_integer(index: object) -> None:
    with pytest.raises(InvariantError, match="source_account_index"):
        _make(source_account_index=index)


def test_source_account_index_is_keyword_only() -> None:
    """#1141: a seventh positional argument refuses, as it did while it filled
    the removed field's slot on a paid-tax row."""
    with pytest.raises(TypeError):
        WithholdingTax(
            Ticker("XQAA"),
            Isin("US99990A3095"),
            date(2026, 2, 18),
            Decimal("-0.70"),
            "USD",
            "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax",
            0,  # ty: ignore[too-many-positional-arguments]
        )


def test_tax_country_and_file_line_default_to_none() -> None:
    """#1282: only the IBKR Activity Statement parser sets them."""
    wht = _make()
    assert wht.tax_country_iso2 is None
    assert wht.file_line is None
    wht = _make(tax_country_iso2="NL", file_line=12)
    assert (wht.tax_country_iso2, wht.file_line) == ("NL", 12)
    assert _make(file_line=1).file_line == 1


@pytest.mark.parametrize(
    "country", ["nl", "N", "NLD", "N1", "", "\u00d1L", "\uff2e\uff2c", 0, b"NL"]
)
def test_tax_country_must_be_two_uppercase_ascii_letters(country: object) -> None:
    with pytest.raises(InvariantError, match="tax_country_iso2 must be two uppercase"):
        _make(tax_country_iso2=country, file_line=12)


def test_tax_country_needs_a_file_line() -> None:
    """#1282: a refusal names the row by its file line."""
    with pytest.raises(InvariantError, match="file_line must be set when tax_country_iso2 is"):
        _make(tax_country_iso2="NL")


@pytest.mark.parametrize("line", [0, -1, True, 1.0, "12", Decimal(12)])
def test_file_line_must_be_a_one_based_integer(line: object) -> None:
    with pytest.raises(InvariantError, match="file_line must be a 1-based line number"):
        _make(file_line=line)
