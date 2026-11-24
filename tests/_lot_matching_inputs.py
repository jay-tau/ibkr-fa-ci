"""Synthetic Activity Statement executions for the FIFO matcher, shared by the
lot matching, exact CG line and source coverage tests, and the broker-basis
refusal text, shared by the lot matching, lot overlap, Schedule CG and closed
lots tests (#1883) and the pipeline tests.

- ``MatchInstrument``: the four instrument fields an ``ExecutionEvent``
  copies; ``SAMPLE_XQBB`` is the default instrument (a synthetic ISIN).
- ``open_execution`` and ``close_execution``: an opening and a closing
  ``ExecutionEvent`` for an instrument.
- ``match_lots``: ``match_fifo`` on the events and Dec 31 close prices, at a
  fixed report date.
- ``BROKER_BASIS_MESSAGE``: the fixed text of ``require_indian_basis``'s
  refusal of a broker-matched lot whose holding-period date moved (#599).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.models import ClosedLot, ExecutionEvent, ExecutionKind, OpenLot, Side
from ibkr_fa.types import CountryCode, Isin, Ticker


@dataclass(frozen=True, slots=True)
class MatchInstrument:
    symbol: str
    isin: str
    description: str = "SAMPLE ISSUER BB INC"
    listing_exchange: str = "NASDAQ"


SAMPLE_XQBB = MatchInstrument("XQBB", "US99995B1044")
_REPORT_DATE = date(2025, 12, 31)


def _event(
    kind: ExecutionKind,
    row_index: int,
    when: str,
    quantity: str,
    *,
    instrument: MatchInstrument,
    side: Side,
    acquisition_cost: str | None = None,
    net_proceeds: str | None = None,
    trade_price: str | None = None,
) -> ExecutionEvent:
    return ExecutionEvent(
        currency_primary="USD",
        symbol=Ticker(instrument.symbol),
        description=instrument.description,
        isin=Isin(instrument.isin),
        listing_exchange=instrument.listing_exchange,
        issuer_country_code=CountryCode(instrument.isin[:2]),
        kind=kind,
        side=side,
        trade_date_time=datetime.fromisoformat(when),
        row_index=row_index,
        quantity=Decimal(quantity),
        acquisition_cost=None if acquisition_cost is None else Decimal(acquisition_cost),
        net_proceeds=None if net_proceeds is None else Decimal(net_proceeds),
        trade_price=None if trade_price is None else Decimal(trade_price),
    )


def open_execution(
    row_index: int,
    when: str,
    quantity: str,
    cost: str,
    *,
    instrument: MatchInstrument = SAMPLE_XQBB,
    side: Side = Side.LONG,
) -> ExecutionEvent:
    return _event(
        ExecutionKind.OPEN,
        row_index,
        when,
        quantity,
        instrument=instrument,
        side=side,
        acquisition_cost=cost,
    )


def close_execution(
    row_index: int,
    when: str,
    quantity: str,
    net_proceeds: str,
    *,
    trade_price: str = "86.50",
    instrument: MatchInstrument = SAMPLE_XQBB,
    side: Side = Side.LONG,
) -> ExecutionEvent:
    """A sell, or with ``side=Side.SHORT`` a closing BUY; ``net_proceeds``
    is the parser's ``|Basis| + Realized P/L``."""
    return _event(
        ExecutionKind.CLOSE,
        row_index,
        when,
        quantity,
        instrument=instrument,
        side=side,
        net_proceeds=net_proceeds,
        trade_price=trade_price,
    )


def match_lots(
    events: list[ExecutionEvent], close_prices: dict[Ticker, Decimal]
) -> tuple[list[OpenLot], list[ClosedLot]]:
    return match_fifo(events, dec31_close_prices=close_prices, report_date=_REPORT_DATE)


# Fixed text (#1047): no symbol, ISIN or dates.
BROKER_BASIS_MESSAGE = (
    "a broker-matched Flex Query closed lot has a HoldingPeriodDateTime on a "
    "different day from its OpenDateTime. IBKR documents a shifted holding-period "
    "date for US wash sales, so CostBasis may include a US wash-sale adjustment "
    "that the Indian return does not recognise. Re-run schedule-fa with an IBKR "
    "Activity Statement that covers this lot's acquisition; it uses the actual "
    "opening-trade cost. LD-coded (wash-sale) Activity Statement rows are still "
    "refused there (https://github.com/jay-tau/ibkr-fa/issues/265)."
)
