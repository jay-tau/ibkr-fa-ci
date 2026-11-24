"""Flex Query section header lines, synthetic data rows under them, a map
from each header's columns to its section, and an Open Positions LOT row
builder, shared by the multi-section splitter, closed lots, Open Positions
and exact parser tests and the IBKR number limit tests.

- ``OPEN_POSITIONS_HEADER``: the Open Positions (LOT) header line.
- ``FLEX_TRADES_HEADER``: the simple (27-column) Trades header line; not the
  Activity Statement ``TRADES_HEADER`` in ``tests/_ibkr_details.py``.
- ``OPEN_POSITIONS_XQAW``: an Open Positions LOT row for the XQAW buy in
  ``TRADES_EXECUTION_XQAW``.
- ``TRADES_LOT_DETAIL_HEADER``: the 30-column LOT-detail Trades header line.
- ``FLEX_SECTION_KINDS``: the column set of each of those three headers, as
  a ``frozenset``, mapped to its ``KnownSection``.
- ``TRADES_EXECUTION_XQAW``: a LOT-detail Trades ``EXECUTION`` row, the
  XQAW opening buy.
- ``TRADES_CLOSED_LOT_XQAT``: a LOT-detail Trades ``CLOSED_LOT`` row for
  XQAT.
- ``make_open_position_row``: an Open Positions LOT row for synthetic XQAW,
  as a dict of column to cell; keyword arguments override cells.
"""

from __future__ import annotations

import csv

from ibkr_fa.parser import KnownSection

# IBKR's Flex Query column names for the Open Positions (LOT) and simple
# (27-column) Trades sections.
OPEN_POSITIONS_HEADER = (
    '"CurrencyPrimary","FXRateToBase","AssetClass","SubCategory","Symbol",'
    '"Description","CUSIP","ISIN","ListingExchange","IssuerCountryCode",'
    '"ReportDate","Quantity","MarkPrice","PositionValue","CostBasisPrice",'
    '"CostBasisMoney","Side","LevelOfDetail","OpenDateTime","HoldingPeriodDateTime"'
)

FLEX_TRADES_HEADER = (
    '"CurrencyPrimary","FXRateToBase","AssetClass","SubCategory","Symbol",'
    '"Description","CUSIP","ISIN","ListingExchange","IssuerCountryCode",'
    '"ReportDate","TradeDate","SettleDateTarget","TransactionType","Quantity",'
    '"TradePrice","TradeMoney","Proceeds","Taxes","IBCommission",'
    '"IBCommissionCurrency","NetCash","ClosePrice","Open/CloseIndicator",'
    '"CostBasis","FifoPnlRealized","Buy/Sell"'
)


# Synthetic Open Positions LOT row: the XQAW buy in TRADES_EXECUTION_XQAW
# below (cost 969.75, so CostBasisPrice 969.75 / 25 = 38.79), marked at 45.10
# (PositionValue 25 * 45.10 = 1127.50).
OPEN_POSITIONS_XQAW = (
    '"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103",'
    '"US99990E1038","NASDAQ","US","20260617","25","45.10","1127.5","38.79",'
    '"969.75","Long","LOT","20260616","20260616"'
)


TRADES_LOT_DETAIL_HEADER = (
    '"CurrencyPrimary","FXRateToBase","AssetClass","SubCategory","Symbol",'
    '"Description","CUSIP","ISIN","ListingExchange","IssuerCountryCode",'
    '"ReportDate","TradeDate","SettleDateTarget","TransactionType","Quantity",'
    '"TradePrice","TradeMoney","Proceeds","Taxes","IBCommission",'
    '"IBCommissionCurrency","NetCash","ClosePrice","Open/CloseIndicator",'
    '"CostBasis","FifoPnlRealized","Buy/Sell","LevelOfDetail",'
    '"OpenDateTime","HoldingPeriodDateTime"'
)

FLEX_SECTION_KINDS = {
    frozenset(next(csv.reader([header]))): kind
    for header, kind in (
        (OPEN_POSITIONS_HEADER, KnownSection.OPEN_POSITIONS_LOT),
        (FLEX_TRADES_HEADER, KnownSection.TRADES),
        (TRADES_LOT_DETAIL_HEADER, KnownSection.TRADES),
    )
}

# Synthetic EXECUTION row: an XQAW opening buy of 25 shares at 38.75
# (TradeMoney 968.75), with a 1.00 commission (NetCash -969.75).
TRADES_EXECUTION_XQAW = (
    '"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103",'
    '"US99990E1038","NASDAQ","US","20260616","20260616","20260617","ExchTrade",'
    '"25","38.75","968.75","-968.75","0","-1","USD","-969.75","39.60","O",'
    '"969.75","0","BUY","EXECUTION","",""'
)
# Synthetic CLOSED_LOT row: 12 XQAT shares with CostBasis 12 * 41.35 = 496.20
# and a HoldingPeriodDateTime before its OpenDateTime.
TRADES_CLOSED_LOT_XQAT = (
    '"USD","1","STK","COMMON","XQAT","SAMPLE ISSUER AT INC","99990G108",'
    '"US99990G1085","NASDAQ","US","20260721","20260721","","","12","41.35",'
    '"","","","","","","","C","496.20","-23.40","SELL","CLOSED_LOT",'
    '"20260721;101500","20260526;110000"'
)


# Synthetic XQAW lot: 25 shares bought at 38.75 with a 1.00 commission, so
# CostBasisMoney is 969.75 and CostBasisPrice 969.75 / 25 = 38.79. Marked at
# 45.10, PositionValue is 25 * 45.10 = 1127.50. ReportDate is the year-end the
# Schedule FA coverage check requires.
def make_open_position_row(**overrides: str) -> dict[str, str]:
    base = {
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
        "ReportDate": "20261231",
        "Quantity": "25",
        "MarkPrice": "45.10",
        "PositionValue": "1127.5",
        "CostBasisPrice": "38.79",
        "CostBasisMoney": "969.75",
        "Side": "Long",
        "LevelOfDetail": "LOT",
        "OpenDateTime": "20260616",
        "HoldingPeriodDateTime": "20260616",
    }
    base.update(overrides)
    return base
