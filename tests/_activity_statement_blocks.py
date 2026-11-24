"""Synthetic Activity Statement blocks and Trades records shared by the Net
Asset Value, Performance Summary and Trades totals parser tests and the
IBKR number limit tests.

- ``nav_row``: one asset-class row of the Net Asset Value table.
- ``nav_block``: the Net Asset Value table block of those rows.
- ``twr_block``: the Time-Weighted Rate of Return block.
- ``change_in_nav_block``: a Change in NAV block of field/value pairs.
- ``performance_summary_block``: one Realized & Unrealized Performance
  Summary block.
- ``TRADES_HEADER_RECORD``: the Trades header record.
- ``trades_order_record``: a Trades ``Order`` record.
- ``csv_records_text``: the CSV text of records.
- ``edit_trades_block``: a Trades block with one cell changed, by default
  ``_BLOCK``: the header and three ``Order`` records (``_XQAA_BUY``,
  ``_XQAA_SELL``, ``_XQAB_BUY``) with their totals.
"""

from __future__ import annotations

import csv
import io

from ibkr_fa.parser.activity_statement import KnownActivitySection, Section
from ibkr_fa.parser.activity_statement._trades_totals import COLUMNS
from tests._trades_totals import with_trades_totals

_NAV_COLS = (
    "Asset Class",
    "Prior Total",
    "Current Long",
    "Current Short",
    "Current Total",
    "Change",
)
_CHANGE_COLS = ("Field Name", "Field Value")


def nav_row(asset_class: str, current_total: str) -> dict[str, str]:
    return {
        "Asset Class": asset_class,
        "Prior Total": "0",
        "Current Long": current_total,
        "Current Short": "0",
        "Current Total": current_total,
        "Change": current_total,
    }


def nav_block(rows: list[dict[str, str]]) -> Section:
    return Section(
        kind=KnownActivitySection.NET_ASSET_VALUE, columns=_NAV_COLS, header_line=1, rows=rows
    )


def twr_block() -> Section:
    """The second header-led block IBKR emits under 'Net Asset Value'."""
    return Section(
        kind=KnownActivitySection.NET_ASSET_VALUE,
        columns=("Time Weighted Rate of Return",),
        header_line=1,
        rows=[{"Time Weighted Rate of Return": "-1.05%"}],
    )


def change_in_nav_block(pairs: list[tuple[str, str]]) -> Section:
    return Section(
        kind=KnownActivitySection.CHANGE_IN_NAV,
        columns=_CHANGE_COLS,
        header_line=1,
        rows=[{"Field Name": n, "Field Value": v} for n, v in pairs],
    )


_SECTION = KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY
_COLUMNS = ("Asset Category", "Symbol", "Cost Adj.", "Realized Total")


def performance_summary_block(
    *cost_adjustments: str, columns: tuple[str, ...] = _COLUMNS
) -> Section:
    """One summary block, its header on line 4 and a row per value after it."""
    rows = [
        dict(zip(columns, ("Stocks", "ABC", value, "1.00")[: len(columns)], strict=True))
        for value in cost_adjustments
    ]
    return Section(
        kind=_SECTION,
        columns=columns,
        header_line=4,
        rows=rows,
        row_lines=list(range(5, 5 + len(rows))),
    )


TRADES_HEADER_RECORD = ["Trades", "Header", *COLUMNS]


def trades_order_record(symbol: str, *cells: str) -> list[str]:
    """An ``Order`` row: Quantity, T. Price, C. Price, Proceeds, Comm/Fee,
    Basis, Realized P/L, MTM P/L, Code."""
    quantity, price, close, proceeds, fee, basis, realized, mtm, code = cells
    return [
        *("Trades", "Data", "Order", "Stocks", "USD", symbol, "2025-03-03, 10:00:00"),
        *(quantity, price, close, proceeds, fee, basis, realized, mtm, code),
    ]


# A buy and a sell of XQAA, then a buy of XQAB, with varied amounts for
# privacy checks. With its totals:
#   1 header, 2-3 XQAA, 4 SubTotal, 5 XQAB, 6 SubTotal, 7 Total.
_XQAA_BUY = trades_order_record(
    "XQAA", "37", "113.17", "114.29", "-4187.29", "-1.13", "4188.42", "0", "41.44", "O"
)
_XQAA_SELL = trades_order_record(
    "XQAA", "-12", "121.53", "114.29", "1458.36", "-1.07", "-1358.41", "98.88", "-86.88", "C"
)
_XQAB_BUY = trades_order_record(
    "XQAB", "18", "52.61", "53.07", "-946.98", "-0.94", "947.92", "0", "8.28", "O"
)


def csv_records_text(*records: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(records)
    return buffer.getvalue()


def _records(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


_BLOCK = with_trades_totals(
    csv_records_text(TRADES_HEADER_RECORD, _XQAA_BUY, _XQAA_SELL, _XQAB_BUY)
)


def edit_trades_block(line: int, column: str, value: str, text: str = _BLOCK) -> str:
    """``text`` with ``column`` of the record on ``line`` set to ``value``."""
    records = _records(text)
    records[line - 1][2 + COLUMNS.index(column)] = value
    return csv_records_text(*records)
