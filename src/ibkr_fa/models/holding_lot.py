"""A lot a source says is held, without a valuation (#627).

A source can state what is held and what it cost, but not always what
it was worth on the report date. :class:`HoldingLot` holds only those
facts. The engine values it into an :class:`~ibkr_fa.models.OpenLot`
with :func:`ibkr_fa.compute.holding_valuation.value_holding_lot`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import require_lot_identifiers
from ibkr_fa.models._row_checks import require_amounts
from ibkr_fa.models.open_lot import Side
from ibkr_fa.types import CountryCode, Isin, Ticker


@dataclass(frozen=True, slots=True)
class HoldingLot:
    """One held tax lot: what it is, how much of it, and what it cost.

    Attributes:
        currency: ISO 4217 currency the lot is denominated in.
        symbol: Exchange-traded symbol.
        description: Human-readable security name.
        isin: ISO 6166 ISIN.
        issuer_country_code: ISO 3166-1 alpha-2 country of the issuer,
            not of the listing exchange.
        quantity: Shares in the lot; may be fractional.
        cost_basis_money: Acquisition value in ``currency``: the cash paid
            for a purchase, and the FMV at acquisition for an ESPP purchase
            (ADR-0003 decision 2) or an RSU vest.
        acquisition_date: Date the lot was acquired.
        side: :class:`~ibkr_fa.models.Side` of the position.
    """

    currency: str
    symbol: Ticker
    description: str
    isin: Isin
    issuer_country_code: CountryCode
    quantity: Decimal
    cost_basis_money: Decimal
    acquisition_date: date
    side: Side

    def __post_init__(self) -> None:
        """Refuse a non-finite amount, a non-positive quantity, a negative
        cost, an empty symbol, a malformed identifier and a ``side`` that
        isn't a :class:`~ibkr_fa.models.Side` member (its raw string
        included)."""
        require_amounts(self, "quantity", "cost_basis_money")
        require(
            isinstance(self.side, Side),
            what="HoldingLot.side must be a Side enum member",
            side=self.side,
        )
        require(self.symbol != "", what="HoldingLot.symbol must be non-empty")
        require_lot_identifiers(
            "HoldingLot",
            isin=self.isin,
            currency_field="currency",
            currency=self.currency,
            issuer_country_code=self.issuer_country_code,
        )
        require(
            self.quantity > 0,
            what="HoldingLot.quantity must be positive (side carries the direction)",
            quantity=self.quantity,
            side=self.side,
        )
        require(
            self.cost_basis_money >= 0,
            what="HoldingLot.cost_basis_money must be non-negative",
            cost_basis_money=self.cost_basis_money,
        )
