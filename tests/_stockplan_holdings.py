"""Synthetic StockPlan Holdings Report workbooks, their ``Quote History``
rows and the holdings parse call, shared by the StockPlan holdings parser,
no-echo, refusal chain, number limit and FA pipeline tests.

- ``HOLDINGS_SYMBOL`` and ``HOLDINGS_EXCHANGE``: the stock's ticker and listing
  exchange that ``parse_holdings_report`` passes the parser.
- ``HOLDINGS_HEADER``: the report's column header row.
- ``HOLDINGS_FOOTER``: the report's footer text, written in one cell.
- ``QUOTE_DATE`` and ``QUOTE_PRICE``: the synthetic Selected Date and the
  stock's quote on it.
- ``quote_history_rows``: a ``Quote History`` sheet's rows for a fund.
- ``add_quote_history``: append a ``Quote History`` sheet to a workbook.
- ``market_value``: the Market Value that ``QUOTE_PRICE`` gives a number of
  shares.
- ``HOLDINGS_QUOTES``: the default ``Quote History`` rows, for the stock.
- ``write_holdings_report``: save a Holdings Report workbook with data rows and
  an optional ``Quote History`` sheet.
- ``holdings_cash_row``: the report's cash row.
- ``holdings_xqcb_row``: the report's stock row for a number of shares and an
  acquisition value, at ``market_value``.
- ``parse_holdings_report``: ``parse_stockplan_holdings`` on a workbook.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import openpyxl

from ibkr_fa.parser.stockplan.holdings import StockplanHoldings, parse_stockplan_holdings
from ibkr_fa.types import Ticker

HOLDINGS_SYMBOL = Ticker("XQCB")
HOLDINGS_EXCHANGE = "NASDAQ"
HOLDINGS_HEADER = [
    "Employee Number",
    "Savings Plan Name",
    "Fund Name",
    "Number of Shares",
    "Acquisition Value",
    "Market Value",
]
HOLDINGS_FOOTER = (
    "Market Value Definition = Market Price as at Selected Date * Total Held Shares"
    "Acquisition Value Definition = Fair Market Value at Vest/Exercise * Total Held Shares"
    "The numbers on this statement reflect post-split values from stock split on 7/15/2022"
)
# The synthetic Selected Date, before the title's 14-Jul-2026 run date, and the
# stock's quote: its Market Value is this price x its Number of Shares (#1290).
QUOTE_DATE = _dt.date(2026, 7, 10)
QUOTE_PRICE = Decimal("232.06")


def quote_history_rows(
    fund: str, *, quote_date: _dt.date = QUOTE_DATE, price: object = float(QUOTE_PRICE)
) -> list[list[object]]:
    """A synthetic ``Quote History`` sheet: a ``Cash - USD`` quote of 1 and
    ``fund``'s ``price``, both dated ``quote_date``."""
    when = _dt.datetime.combine(quote_date, _dt.time())
    return [
        ["Quote History"],
        ["Fund", "Quote Date", "Price"],
        ["Cash - USD", when, 1.0],
        [fund, when, price],
    ]


def add_quote_history(workbook: openpyxl.Workbook, rows: Sequence[list[object]]) -> None:
    """Append a ``Quote History`` sheet holding ``rows`` to ``workbook``."""
    sheet = workbook.create_sheet("Quote History")
    for row in rows:
        sheet.append(row)


def market_value(shares: object) -> float:
    """The Market Value that :data:`QUOTE_PRICE` gives ``shares``."""
    return float(QUOTE_PRICE * Decimal(str(shares)))


HOLDINGS_QUOTES = tuple(quote_history_rows("XQCB - NASDAQ"))


def write_holdings_report(
    tmp_path: Path,
    data_rows: list[list[object]],
    *,
    sheet: str = "Holdings Report",
    quotes: Sequence[list[object]] | None = HOLDINGS_QUOTES,
) -> Path:
    """A Holdings workbook; ``quotes`` is its ``Quote History`` sheet, or None for none."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(["SAMPLE ISSUER CB, Inc. - Holdings Report (report run on 14-Jul-2026)"])
    ws.append(HOLDINGS_HEADER)
    for row in data_rows:
        ws.append(row)
    ws.append([HOLDINGS_FOOTER])
    if quotes is not None:
        add_quote_history(wb, quotes)
    path = tmp_path / "Vested Share Holdings.xlsx"
    wb.save(path)
    return path


def holdings_cash_row() -> list[object]:
    return [
        "7000001",
        "SAMPLE ISSUER CB, Inc. Long Share Savings Plan",
        "Cash - USD",
        4.18,
        4.18,
        4.18,
    ]


def holdings_xqcb_row(shares: float, acq: float) -> list[object]:
    return [
        "7000001",
        "SAMPLE ISSUER CB, Inc. Long Share Savings Plan",
        "XQCB - NASDAQ",
        shares,
        acq,
        market_value(shares),
    ]


def parse_holdings_report(path: Path) -> StockplanHoldings:
    return parse_stockplan_holdings(
        path, symbol=HOLDINGS_SYMBOL, listing_exchange=HOLDINGS_EXCHANGE
    )
