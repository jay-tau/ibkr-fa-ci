"""One opening or closing trade, before any lot matching.

The Activity Statement Trades parser emits one :class:`ExecutionEvent`
per Trades row. :func:`ibkr_fa.compute.lot_matching.match_fifo` turns
the events into :class:`~ibkr_fa.models.OpenLot` and
:class:`~ibkr_fa.models.ClosedLot` records (#640).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from ibkr_fa.invariants import require
from ibkr_fa.models.open_lot import Side
from ibkr_fa.types import CountryCode, Isin, Ticker


class ExecutionKind(StrEnum):
    """Whether a trade opens or closes a position (the ``O``/``C`` code token)."""

    OPEN = "O"
    CLOSE = "C"


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """One Trades row's instrument and trade facts.

    An event holds no matched state and no mark price: how much of an
    open a later close consumes, and what the open is worth at year end,
    are the matcher's job.

    Attributes:
        currency_primary: ISO 4217 trade currency.
        symbol: Exchange-traded symbol.
        description: Human-readable security name.
        isin: ISO 6166 ISIN.
        listing_exchange: Exchange the security trades on.
        issuer_country_code: ISO 3166-1 alpha-2 code from the ISIN prefix.
        kind: Whether the row opens or closes a position.
        side: Side of the position the row opens or closes, from the sign
            of ``Quantity``: a short sale opens a ``SHORT`` and a closing
            BUY closes one, which the matcher refuses (#109).
        trade_date_time: Trade date and time; the matcher orders by it.
        row_index: Zero-based index of the source Trades row; breaks
            same-time ties and locates refusals.
        quantity: Absolute number of shares traded.
        acquisition_cost: Opens only: the actual cost ``|Basis|``.
        net_proceeds: Closes only: ``|Basis| + Realized P/L``.
        trade_price: Closes only: the per-share sale price ``T. Price``.
    """

    currency_primary: str
    symbol: Ticker
    description: str
    isin: Isin
    listing_exchange: str
    issuer_country_code: CountryCode
    kind: ExecutionKind
    side: Side
    trade_date_time: datetime
    row_index: int
    quantity: Decimal
    acquisition_cost: Decimal | None
    net_proceeds: Decimal | None
    trade_price: Decimal | None

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(self.symbol != "", what="ExecutionEvent.symbol must be non-empty")
        require(
            self.row_index >= 0,
            what="ExecutionEvent.row_index must be non-negative",
            row_index=self.row_index,
        )
        require(
            self.quantity.is_finite() and self.quantity > 0,
            what="ExecutionEvent.quantity must be positive and finite",
            quantity=self.quantity,
        )
        require(
            isinstance(self.kind, ExecutionKind),
            what="ExecutionEvent.kind must be an ExecutionKind enum member",
            kind=self.kind,
        )
        require(
            isinstance(self.side, Side),
            what="ExecutionEvent.side must be a Side enum member",
            side=self.side,
        )
        if self.kind is ExecutionKind.OPEN:
            require(
                self.acquisition_cost is not None
                and self.acquisition_cost.is_finite()
                and self.acquisition_cost >= 0,
                what="an OPEN ExecutionEvent needs a non-negative finite acquisition_cost",
                acquisition_cost=self.acquisition_cost,
            )
            require(
                self.net_proceeds is None and self.trade_price is None,
                what="an OPEN ExecutionEvent carries no net_proceeds or trade_price",
                net_proceeds=self.net_proceeds,
                trade_price=self.trade_price,
            )
            return
        require(
            self.acquisition_cost is None,
            what="a CLOSE ExecutionEvent carries no acquisition_cost",
            acquisition_cost=self.acquisition_cost,
        )
        require(
            self.net_proceeds is not None and self.net_proceeds.is_finite(),
            what="a CLOSE ExecutionEvent needs a finite net_proceeds",
            net_proceeds=self.net_proceeds,
        )
        require(
            self.trade_price is not None and self.trade_price.is_finite() and self.trade_price >= 0,
            what="a CLOSE ExecutionEvent needs a non-negative finite trade_price",
            trade_price=self.trade_price,
        )
