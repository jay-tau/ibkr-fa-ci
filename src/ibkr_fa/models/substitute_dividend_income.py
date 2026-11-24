"""Foreign substitute-dividend income kept separate from ordinary dividends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import (
    is_iso_alpha2,
    is_iso_currency_code,
    require_isin_shape,
)
from ibkr_fa.types import CountryCode, Isin, Ticker

_ISIN_LENGTH = 12


@dataclass(frozen=True, slots=True)
class SubstituteDividendIncome:
    """One substitute dividend with distinct issuer and income-source countries."""

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: CountryCode
    source_country_iso2: CountryCode
    payment_date: date
    amount_gross: Decimal
    currency: str
    description: str
    source_row_index: int | None = None

    def __post_init__(self) -> None:
        require(self.symbol != "", what="SubstituteDividendIncome.symbol must be non-empty")
        require(
            len(self.isin) == _ISIN_LENGTH and self.isin[:2] == self.issuer_country_iso2,
            what="SubstituteDividendIncome ISIN must agree with its issuer country",
            isin=self.isin,
            issuer_country_iso2=self.issuer_country_iso2,
        )
        require_isin_shape("SubstituteDividendIncome", self.isin)
        require(
            is_iso_alpha2(self.source_country_iso2) and self.source_country_iso2 != "IN",
            what="SubstituteDividendIncome must be foreign-source",
            source_country_iso2=self.source_country_iso2,
        )
        require(
            self.amount_gross.is_finite() and self.amount_gross > 0,
            what="SubstituteDividendIncome.amount_gross must be finite and positive",
            amount_gross=self.amount_gross,
        )
        require(
            is_iso_currency_code(self.currency),
            what="SubstituteDividendIncome.currency must be uppercase ISO 4217",
            currency=self.currency,
        )
        require(
            self.description != "",
            what="SubstituteDividendIncome.description must preserve its audit text",
        )
        require(
            self.source_row_index is None or self.source_row_index >= 0,
            what="SubstituteDividendIncome.source_row_index must be non-negative",
            source_row_index=self.source_row_index,
        )
