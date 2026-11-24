"""Synthetic Flex Query ``CLOSED_LOT`` rows and their closing ``EXECUTION``
rows, and one Open Positions ``LOT`` row, shared by the closed lots, IBKR
number limit, currency refusal, Flex no-echo, IBKR exact and IBKR refusal
chain parser tests.

- ``make_closed_lot_row``: a ``CLOSED_LOT`` row for 12 synthetic shares
  opened and closed on one day; keyword arguments override its cells.
- ``closing_execution_for``: the closing ``EXECUTION`` row of a
  ``CLOSED_LOT`` row.
- ``with_closing_executions``: each ``CLOSED_LOT`` row after its closing
  ``EXECUTION`` row, as a Flex Query Trades section lists them (#1286).
- ``FLEX_OPEN_LOT_XQAW``: an Open Positions ``LOT`` row for 37 synthetic
  XQAW shares, reported on 31 Dec 2025.
- ``FLEX_CLOSED_LOT_XQAT``: a ``CLOSED_LOT`` row for 14 synthetic XQAT
  shares opened on 11 Mar 2025 and closed on 21 Jul 2025.
"""

from __future__ import annotations


def make_closed_lot_row(**overrides: str) -> dict[str, str]:
    """Synthetic CLOSED_LOT row: 12 XQAT shares opened and closed on the
    same day, with CostBasis 12 * 41.35 = 496.20 and FifoPnlRealized
    -23.40, so proceeds are 496.20 - 23.40 = 472.80.

    Tests override individual fields to exercise validation paths.
    """
    base = {
        "CurrencyPrimary": "USD",
        "FXRateToBase": "1",
        "AssetClass": "STK",
        "SubCategory": "COMMON",
        "Symbol": "XQAT",
        "Description": "SAMPLE ISSUER AT INC",
        "CUSIP": "99990G108",
        "ISIN": "US99990G1085",
        "ListingExchange": "NASDAQ",
        "IssuerCountryCode": "US",
        "ReportDate": "20260721",
        "TradeDate": "20260721",
        "SettleDateTarget": "",
        "TransactionType": "",
        "Quantity": "12",
        "TradePrice": "41.35",
        "TradeMoney": "",
        "Proceeds": "",
        "Taxes": "",
        "IBCommission": "",
        "IBCommissionCurrency": "",
        "NetCash": "",
        "ClosePrice": "",
        "Open/CloseIndicator": "C",
        "CostBasis": "496.20",
        "FifoPnlRealized": "-23.40",
        "Buy/Sell": "SELL",
        "LevelOfDetail": "CLOSED_LOT",
        "OpenDateTime": "20260721;101500",
        "HoldingPeriodDateTime": "20260721;101500",
    }
    base.update(overrides)
    return base


def closing_execution_for(lot_row: dict[str, str]) -> dict[str, str]:
    """The closing EXECUTION row of a CLOSED_LOT row: its security and
    trade date, its quantity as a sale, and no lot dates. The parser never
    reads an execution's cash-flow cells, so they are left as the lot's."""
    return {
        **lot_row,
        "Quantity": f"-{lot_row['Quantity']}",
        "LevelOfDetail": "EXECUTION",
        "OpenDateTime": "",
        "HoldingPeriodDateTime": "",
    }


def with_closing_executions(*lot_rows: dict[str, str]) -> list[dict[str, str]]:
    """Each CLOSED_LOT row after its closing execution, as a Flex Query
    Trades section interleaves them (#1286)."""
    return [row for lot_row in lot_rows for row in (closing_execution_for(lot_row), lot_row)]


# 37 * 41.25 = 1526.25 and 37 * 38.40 = 1420.80.
FLEX_OPEN_LOT_XQAW = {
    "CurrencyPrimary": "USD",
    "FXRateToBase": "1",
    "AssetClass": "STK",
    "SubCategory": "COMMON",
    "Symbol": "XQAW",
    "Description": "SAMPLE ISSUER AW INC",
    "CUSIP": "99990E103",
    "ISIN": "US99990E1038",
    "ListingExchange": "NASDAQ",
    "IssuerCountryCode": "US",
    "ReportDate": "20251231",
    "Quantity": "37",
    "MarkPrice": "41.25",
    "PositionValue": "1526.25",
    "CostBasisPrice": "38.40",
    "CostBasisMoney": "1420.80",
    "Side": "Long",
    "LevelOfDetail": "LOT",
    "OpenDateTime": "20250312",
    "HoldingPeriodDateTime": "20250312",
}
# 14 shares at 41.35 = 578.90 basis; proceeds 578.90 - 23.40 = 555.50.
FLEX_CLOSED_LOT_XQAT = {
    "CurrencyPrimary": "USD",
    "FXRateToBase": "1",
    "AssetClass": "STK",
    "SubCategory": "COMMON",
    "Symbol": "XQAT",
    "Description": "SAMPLE ISSUER AT INC",
    "CUSIP": "99990G108",
    "ISIN": "US99990G1085",
    "ListingExchange": "NASDAQ",
    "IssuerCountryCode": "US",
    "ReportDate": "20250721",
    "TradeDate": "20250721",
    "SettleDateTarget": "",
    "TransactionType": "",
    "Quantity": "14",
    "TradePrice": "41.35",
    "TradeMoney": "",
    "Proceeds": "",
    "Taxes": "",
    "IBCommission": "",
    "IBCommissionCurrency": "",
    "NetCash": "",
    "ClosePrice": "",
    "Open/CloseIndicator": "C",
    "CostBasis": "578.90",
    "FifoPnlRealized": "-23.40",
    "Buy/Sell": "SELL",
    "LevelOfDetail": "CLOSED_LOT",
    "OpenDateTime": "20250311;101500",
    "HoldingPeriodDateTime": "20250311;101500",
}
