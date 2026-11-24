"""Synthetic IBKR Activity Statement rows, the FIFO open lots of Trades rows,
two Open Positions refusal texts and a statement file writer, shared by the
Activity Statement Trades, Dividends, Interest, Withholding Tax and
multi-section parser tests, the IBKR number limit, refusal chain and exact
parser tests and the Open Positions value tests.

- :func:`make_fii_row`: a Financial Instrument Information row; keyword
  arguments set its symbol, description, security ID, listing exchange and
  asset category.
- :func:`make_trade_row`: a Trades ``Order`` row for USD stocks; keyword
  arguments set its symbol, date and time, quantity, prices, basis and code.
- :func:`fifo_open_lots`: parses Trades rows with a Statement Period that
  holds every date, FIFO-matches them with a default Dec 31 close-price table
  and a 31 Dec 2025 report date, and returns the open lots.
- :func:`open_position_row`: an Open Positions ``Summary`` row with a given
  symbol and quantity.
- :func:`open_position_lot_row`: an :func:`open_position_row` row with
  ``DataDiscriminator`` set to ``Lot``.
- :func:`open_position_close_row`: an Open Positions ``Summary`` row with the
  columns the Dec 31 close-price extractor reads.
- :data:`ZERO_QUANTITY_REFUSAL`: the ``ParseError`` message for a Trades or
  Open Positions row whose Quantity is zero.
- :data:`RECONCILIATION_REFUSAL`: the ``ParseError`` message when the quantity
  reconstructed from Trades doesn't reconcile with an Open Positions
  ``Summary`` row's Quantity.
- :func:`make_dividend_row`, :func:`make_interest_row` and
  :func:`make_wht_row`: a Dividends, Interest or Withholding Tax ``Data`` row;
  keyword arguments set its currency, date, description and amount.
- :func:`write_statement_lines`: writes the lines, each followed by a newline,
  to ``statement.csv`` in a folder as UTF-8 and returns the file's path.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.compute.lot_matching import match_fifo
from ibkr_fa.models import OpenLot
from ibkr_fa.parser.activity_statement import FinancialInstrument, parse_activity_statement_trades
from ibkr_fa.types import Ticker
from tests._statement_period import ALL_DATES

# Default Dec 31 close-price lookup used by every test that doesn't
# need to vary it. Mirrors the default close_price in make_trade_row
# below, so existing assertions about mark_price/position_value
# (which were written against the old C. Price-derived behaviour)
# continue to hold for the default symbol "XQAW". Tests that vary
# the symbol or want a Dec-31 close DIFFERENT from the trade-day
# close (#133's whole point) build their own dict. The default inputs are synthetic.
_DEFAULT_CLOSE_PRICES: dict[Ticker, Decimal] = {Ticker("XQAW"): Decimal("45.60")}


def make_fii_row(
    *,
    symbol: str = "XQAW",
    description: str = "SAMPLE ISSUER AW INC",
    security_id: str = "US99990E1038",
    listing_exch: str = "NASDAQ",
    asset_category: str = "Stocks",
) -> dict[str, str]:
    """Construct a Financial Instrument Information row."""
    return {
        "Asset Category": asset_category,
        "Symbol": symbol,
        "Description": description,
        "Conid": "12345",
        "Security ID": security_id,
        "Underlying": symbol,
        "Listing Exch": listing_exch,
        "Multiplier": "1",
        "Type": "COMMON",
        "Code": "",
    }


def make_trade_row(
    *,
    symbol: str = "XQAW",
    date_time: str = "2025-06-15, 09:30:00",
    quantity: str = "10",
    trade_price: str = "41.25",
    close_price: str = "45.60",
    basis: str = "412.50",
    code: str = "O",
) -> dict[str, str]:
    """Construct one Activity Statement Trades 'Order' row.

    The default ``basis`` is ``quantity * trade_price`` with no commission
    (10 * 41.25 = 412.50), matching ``Comm/Fee`` of 0."""
    return {
        "DataDiscriminator": "Order",
        "Asset Category": "Stocks",
        "Currency": "USD",
        "Symbol": symbol,
        "Date/Time": date_time,
        "Quantity": quantity,
        "T. Price": trade_price,
        "C. Price": close_price,
        "Proceeds": str(-Decimal(quantity) * Decimal(trade_price)),
        "Comm/Fee": "0",
        "Basis": basis,
        "Realized P/L": "0",
        "MTM P/L": "0",
        "Code": code,
    }


def fifo_open_lots(
    rows: list[dict[str, str]], *, instruments: dict[Ticker, FinancialInstrument]
) -> list[OpenLot]:
    """Parse and FIFO-match ``rows``; the open lots feed reconciliation."""
    open_lots, _closed_lots = match_fifo(
        parse_activity_statement_trades(rows, instruments=instruments, period=ALL_DATES),
        dec31_close_prices=_DEFAULT_CLOSE_PRICES,
        report_date=date(2025, 12, 31),
    )
    return open_lots


def open_position_row(*, symbol: str, quantity: str) -> dict[str, str]:
    return {
        "DataDiscriminator": "Summary",
        "Asset Category": "Stocks",
        "Currency": "USD",
        "Symbol": symbol,
        "Quantity": quantity,
        "Mult": "1",
        "Cost Price": "100",
        "Cost Basis": "1000",
        "Close Price": "105",
        "Value": "1050",
        "Unrealized P/L": "50",
        "Code": "",
    }


def open_position_lot_row(*, symbol: str, quantity: str) -> dict[str, str]:
    return {**open_position_row(symbol=symbol, quantity=quantity), "DataDiscriminator": "Lot"}


def open_position_close_row(
    *, symbol: str, close_price: str, quantity: str = "10"
) -> dict[str, str]:
    """Build one Open Positions Summary row with the columns the
    Dec 31 close-price extractor reads."""
    return {
        "DataDiscriminator": "Summary",
        "Asset Category": "Stocks",
        "Currency": "USD",
        "Symbol": symbol,
        "Quantity": quantity,
        "Mult": "1",
        "Cost Price": "0",
        "Cost Basis": "0",
        "Close Price": close_price,
        "Value": "0",
        "Unrealized P/L": "0",
        "Code": "",
    }


ZERO_QUANTITY_REFUSAL = "must not be zero; a row that moved no shares is malformed."
RECONCILIATION_REFUSAL = (
    "reconciliation failed: the total quantity reconstructed from Trades for this "
    "Summary row's Symbol differs from its Quantity by more than 0.0001. If part of the "
    "holding was bought before the statement period, re-export the Activity Statement "
    "starting from your account's first trade. Otherwise, refusing output to avoid "
    "corrupting Schedule FA — please file an issue with a redacted statement."
)


def make_dividend_row(
    *,
    currency: str = "USD",
    payment_date: str = "18-02-2026",
    description: str = "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
    amount: str = "2.80",
) -> dict[str, str]:
    """Build one synthetic Activity Statement Dividends 'Data' row.

    The default amount is 8 shares at the description's 0.35 per share.
    """
    return {
        "DataDiscriminator": "Data",
        "Currency": currency,
        "Date": payment_date,
        "Description": description,
        "Amount": amount,
    }


def make_interest_row(
    *,
    currency: str = "USD",
    credit_date: str = "30-04-2025",
    description: str = "USD Credit Interest for Apr-2025",
    amount: str = "1.23",
) -> dict[str, str]:
    """Build one Activity Statement Interest 'Data' row.

    IBKR's Interest section schema (columns in order)::

      DataDiscriminator, Currency, Date, Description, Amount

    Default values mirror IBKR's typical monthly USD-credit-interest
    payment on idle USD cash above the minimum-balance threshold.
    """
    return {
        "DataDiscriminator": "Data",
        "Currency": currency,
        "Date": credit_date,
        "Description": description,
        "Amount": amount,
    }


def make_wht_row(
    *,
    currency: str = "USD",
    payment_date: str = "18-02-2026",
    description: str = "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax",
    amount: str = "-0.70",
) -> dict[str, str]:
    """Build one synthetic Activity Statement Withholding Tax 'Data' row.

    Default amount is negative because IBKR emits WHT as a debit
    against the cash balance (money leaving). See
    :class:`ibkr_fa.models.WithholdingTax` for the sign convention.
    It is 25% of the matching 2.80 dividend (8 shares at 0.35): 2.80 * 0.25 = 0.70.
    """
    return {
        "DataDiscriminator": "Data",
        "Currency": currency,
        "Date": payment_date,
        "Description": description,
        "Amount": amount,
    }


def write_statement_lines(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "statement.csv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
