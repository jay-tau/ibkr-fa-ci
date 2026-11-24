"""Residual foreign income reported in Schedule OS row 1e."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_iso_alpha2, is_iso_currency_code
from ibkr_fa.types import CountryCode


@dataclass(frozen=True, slots=True)
class OtherSourceIncome:
    """One exact foreign-currency receipt with an evidenced OS nature."""

    nature: str
    payment_date: date
    amount_gross: Decimal
    currency: str
    description: str
    source_row_index: int | None = None
    source_country_iso2: CountryCode | None = None

    def __post_init__(self) -> None:
        require(self.nature.strip() != "", what="OtherSourceIncome.nature must be non-empty")
        require(
            self.amount_gross.is_finite() and self.amount_gross > 0,
            what="OtherSourceIncome.amount_gross must be finite and positive",
        )
        require(
            is_iso_currency_code(self.currency),
            what="OtherSourceIncome.currency must be uppercase ISO 4217",
        )
        require(
            self.description != "",
            what="OtherSourceIncome.description must preserve its audit text",
        )
        require(
            self.source_row_index is None or self.source_row_index >= 0,
            what="OtherSourceIncome.source_row_index must be non-negative",
        )
        require(
            self.source_country_iso2 is None
            or (is_iso_alpha2(self.source_country_iso2) and self.source_country_iso2 != "IN"),
            what="OtherSourceIncome.source_country_iso2 must be foreign uppercase ISO alpha-2",
        )
