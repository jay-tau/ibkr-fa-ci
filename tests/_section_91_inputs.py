"""Synthetic section 91 inputs (#1177), shared by the section 91 compute,
pipeline and CLI tests (#1887).

- ``_SYMBOL``, ``_BM_ISIN``, ``_KY_ISIN``, ``_VG_ISIN``, ``_US_ISIN``,
  ``_LONG_GROSS`` and ``_DESCRIPTION``: the privacy markers; ``_ON``: the
  payment date; ``_MARKERS``: the marker texts no refusal may echo;
- ``_PREFIX``: the refusals' fixed head; ``_PROOF``, ``_PREFLIGHT_ZERO``,
  ``_PREFLIGHT_PORTAL``, ``_PREFLIGHT_OTHER``, ``_EXACT_ZERO``,
  ``_EXACT_PORTAL`` and ``_EXACT_OTHER``: each refusal's fixed detail;
- ``_ISIN_BY_ISO``: each country's synthetic ISIN;
- ``_Country``: one country's synthetic payment; ``_ORDINARY``, ``_PRODUCT``
  and ``_READINESS``: the three payments the tests share;
- ``_wht``: a country's withholding row; ``_net_zero_us``: a US payment and
  its same-year refund;
- ``_oracle``: HALF_UP to whole rupees of a non-negative rational;
  ``_exact_credit``: a country's exact section 91 credit;
- ``_assert_refusal``: a refusal's complete fixed message, with no stored
  context, no chain and no marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Isin, Ticker
from tests._crash_reports import render_without_frames

# The issue's privacy markers. Withholding rows and dividend lines have no
# source or reason field, so those ride in the description. The long numeric
# marker also rides there when it isn't the gross amount.
_SYMBOL = "QZ03SEC91"
_BM_ISIN = Isin("BM99970Q3001")
_KY_ISIN = Isin("KY99970Q3002")
_VG_ISIN = Isin("VG99970Q3003")
_US_ISIN = Isin("US99970Q3004")
_LONG_GROSS = "5.999999999999999999999999999"
_DESCRIPTION = (
    "synthetic-section91-description synthetic-section91-source synthetic-section91-reason "
    + _LONG_GROSS
)
_ON = date(2025, 12, 15)
_MARKERS = (
    _SYMBOL,
    str(_BM_ISIN),
    "synthetic-section91-description",
    "synthetic-section91-source",
    "synthetic-section91-reason",
    _ON.isoformat(),
    _LONG_GROSS,
)

_PREFIX = "InvariantError: Form 67 section 91, country input row"
_CREDIT = "field section_91_credit_inr"
_PROOF = (
    "fields section_91_credit_inr and total_credit_inr: exact filed credit cannot be "
    "guaranteed within Decimal precision"
)
_PREFLIGHT_ZERO = (
    f"{_CREDIT}: the credit as filed today rounds to zero, and a filed credit must be positive"
)
_PREFLIGHT_PORTAL = (
    f"{_CREDIT}: the relief as filed today isn't the lower of the filed foreign tax and the "
    "filed Indian tax"
)
_PREFLIGHT_OTHER = f"{_CREDIT}: the credit as filed today fails an existing Form 67 credit check"
_EXACT_ZERO = f"{_CREDIT}: the exact credit rounds to zero, and a filed credit must be positive"
_EXACT_PORTAL = (
    f"{_CREDIT}: the exact relief isn't the lower of the filed foreign tax and the filed Indian tax"
)
_EXACT_OTHER = f"{_CREDIT}: the exact credit fails an existing Form 67 credit check"

_ISIN_BY_ISO = {"BM": _BM_ISIN, "KY": _KY_ISIN, "VG": _VG_ISIN, "US": _US_ISIN}


@dataclass(frozen=True)
class _Country:
    """One country's synthetic payment: a dividend and its withholding, in USD."""

    iso: str = "BM"
    gross: str = "0.20"
    wht: str = "-0.03"
    payable: str | None = "3"


_ORDINARY = _Country()
_PRODUCT = _Country(gross=_LONG_GROSS, wht="-3", payable="2")
_READINESS = _Country(gross="6", wht="-1", payable="1")


def _wht(country: _Country) -> WithholdingTax:
    return WithholdingTax(
        symbol=Ticker(_SYMBOL),
        isin=_ISIN_BY_ISO[country.iso],
        payment_date=_ON,
        amount=Decimal(country.wht),
        currency="USD",
        description=_DESCRIPTION,
    )


def _oracle(value: Fraction) -> int:
    """HALF_UP to whole rupees of a non-negative rational."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _exact_credit(country: _Country, *, tax: str, income: str, fx: str) -> Fraction:
    gross = Fraction(Decimal(country.gross)) * Fraction(Decimal(fx))
    foreign_tax = -Fraction(Decimal(country.wht)) * Fraction(Decimal(fx))
    return min(foreign_tax, Fraction(Decimal(tax)) * gross / Fraction(Decimal(income)))


def _assert_refusal(
    raised: pytest.ExceptionInfo[InvariantError],
    position: int,
    detail: str,
    markers: Sequence[str] = (),
) -> None:
    """The complete fixed message, no stored context, no chain and no marker."""
    error = raised.value
    assert type(error) is InvariantError
    assert str(error) == f"{_PREFIX} {position}, {detail}"
    assert error.context == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is False
    rendered = render_without_frames(error)
    for marker in markers:
        assert marker not in rendered, marker
        assert marker not in repr(error), marker


def _net_zero_us() -> list[WithholdingTax]:
    """A US payment and its same-year refund: netted to zero, so skipped."""
    return [
        WithholdingTax(
            symbol=Ticker(_SYMBOL),
            isin=_US_ISIN,
            payment_date=date(2025, 6, 2),
            amount=Decimal("-1"),
            currency="USD",
            description=_DESCRIPTION,
        ),
        WithholdingTax(
            symbol=Ticker(_SYMBOL),
            isin=_US_ISIN,
            payment_date=date(2025, 6, 9),
            amount=Decimal("1"),
            currency="USD",
            description=_DESCRIPTION,
        ),
    ]
