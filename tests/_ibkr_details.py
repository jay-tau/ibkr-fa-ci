"""Synthetic IBKR details-on and garbled-BOM inputs, and their refusals (#1195).

IBKR's custom Activity Statement adds an ``Open`` column to Open Positions and
``Trade``/``ClosedLot``/``ClosedLot*`` rows to Trades when "Hide Details for
Positions, Trades and Client Fees Sections?" is No. A garbled byte-order mark is
a UTF-8 BOM read as Latin-1 and saved again as UTF-8. Every refusal of either is
a :class:`ParseError` with an empty ``raw_value`` that prints none of the
input's :data:`MARKERS`.

The Activity Statements put the Statement block on lines 1-2 and the Trades
header on line 3, its ``Order`` row on line 4 and a detail row on line 5.
"""

from __future__ import annotations

from typing import Final

from ibkr_fa.exceptions import ParseError
from tests._crash_reports import assert_prints_no_marker
from tests._trades_totals import with_trades_totals

SYMBOL: Final = "SYNTHSYM-DETAILS"
DESCRIPTION: Final = "SYNTHDESC-DETAILS"
HOLDER: Final = "SYNTHHOLDER-DETAILS"
ACCOUNT: Final = "SYNTHACCOUNT-DETAILS"
IDENT: Final = "SYNTHIDENT-DETAILS"
DATE: Final = "2026-03-04"
NUMBER: Final = "731.2468"
MARKERS: Final = (SYMBOL, DESCRIPTION, HOLDER, ACCOUNT, IDENT, DATE, NUMBER)

GARBLED_BOM: Final = b"\xc3\xaf\xc2\xbb\xc2\xbf"
GARBLED_TEXT: Final = GARBLED_BOM.decode()
REAL_BOM: Final = b"\xef\xbb\xbf"
DETAIL_KINDS: Final = ("Trade", "ClosedLot", "ClosedLot*")

_HIDE: Final = "'Hide Details for Positions, Trades and Client Fees Sections?' set to Yes."
OPEN_POSITIONS_MESSAGE: Final = (
    "this Activity Statement was exported with position and trade details shown (Open "
    "Positions has an 'Open' column). This tool reads the standard layout: re-export it "
    f"with {_HIDE}"
)
TRADES_DETAIL_MESSAGE: Final = (
    "this row is one of IBKR's trade details, which this tool doesn't read. Re-export the "
    f"Activity Statement with {_HIDE}"
)
GARBLED_BOM_MESSAGE: Final = (
    "the file starts with a garbled byte-order mark, so it was re-encoded after download and "
    "may be damaged elsewhere. Download the CSV from IBKR again and use it unedited."
)


def open_positions_text(line: int) -> str:
    """The complete refusal of a details-on Open Positions header on ``line``."""
    return f"ParseError in section 'Open Positions' at row {line}: {OPEN_POSITIONS_MESSAGE}"


def trades_detail_text(row: int) -> str:
    """The complete refusal of a Trades detail row at ``row``."""
    return (
        f"ParseError in section 'Trades' at row {row}, column 'DataDiscriminator': "
        f"{TRADES_DETAIL_MESSAGE}"
    )


def garbled_bom_text(row: int) -> str:
    """The complete refusal of a garbled byte-order mark reported at ``row``."""
    return f"ParseError in section '<file>' at row {row}: {GARBLED_BOM_MESSAGE}"


def assert_refusal(
    error: BaseException,
    *,
    source: str,
    section: str,
    row_index: int,
    column: str | None,
    message: str,
    text: str,
) -> None:
    """``error`` is exactly this :class:`ParseError`, and prints no marker."""
    assert type(error) is ParseError
    assert isinstance(error, ParseError)
    assert (error.section, error.row_index, error.column, error.raw_value, error.message) == (
        section,
        row_index,
        column,
        "",
        message,
    )
    assert str(error) == text
    assert_prints_no_marker(error, markers=tuple(marker for marker in MARKERS if marker in source))


def assert_open_positions_refusal(error: BaseException, line: int, *, source: str) -> None:
    """``error`` is the details-on Open Positions refusal of the header on ``line``."""
    assert_refusal(
        error,
        source=source,
        section="Open Positions",
        row_index=line,
        column=None,
        message=OPEN_POSITIONS_MESSAGE,
        text=open_positions_text(line),
    )


def assert_trades_detail_refusal(error: BaseException, row: int, *, source: str) -> None:
    """``error`` is the Trades detail refusal at ``row``."""
    assert_refusal(
        error,
        source=source,
        section="Trades",
        row_index=row,
        column="DataDiscriminator",
        message=TRADES_DETAIL_MESSAGE,
        text=trades_detail_text(row),
    )


def assert_garbled_bom_refusal(error: BaseException, row: int, *, source: str) -> None:
    """``error`` is the garbled byte-order mark refusal at ``row``."""
    assert_refusal(
        error,
        source=source,
        section="<file>",
        row_index=row,
        column=None,
        message=GARBLED_BOM_MESSAGE,
        text=garbled_bom_text(row),
    )


# ---- Activity Statements -------------------------------------------------

STATEMENT: Final = (
    "Statement,Header,Field Name,Field Value\n"
    'Statement,Data,Period,"April 1, 2025 - March 31, 2026"\n'
)
TRADES_COLUMNS: Final = (
    "DataDiscriminator",
    "Asset Category",
    "Currency",
    "Symbol",
    "Date/Time",
    "Quantity",
    "T. Price",
    "C. Price",
    "Proceeds",
    "Comm/Fee",
    "Basis",
    "Realized P/L",
    "MTM P/L",
    "Code",
)
TRADES_HEADER: Final = "Trades,Header," + ",".join(TRADES_COLUMNS) + "\n"
# Each row's cells, keyed by TRADES_COLUMNS, as the splitter maps them.
_AMOUNTS: Final = {
    "Asset Category": "Stocks",
    "Currency": "USD",
    "Symbol": SYMBOL,
    "Date/Time": f"{DATE}, 10:15:00",
    "Quantity": NUMBER,
    "T. Price": "2",
    "C. Price": "2",
    "Proceeds": "-1462.4936",
    "Comm/Fee": "-1",
    "Basis": "1463.4936",
    "Realized P/L": "0",
    "MTM P/L": "0",
    "Code": "O",
}
ORDER_CELLS: Final = {"DataDiscriminator": "Order", **_AMOUNTS}
OPEN_POSITIONS_COLUMNS: Final = (
    "DataDiscriminator",
    "Asset Category",
    "Currency",
    "Symbol",
    "Open",
    "Quantity",
    "Mult",
    "Cost Price",
    "Cost Basis",
    "Close Price",
    "Value",
    "Unrealized P/L",
    "Code",
)
# A details-on Open Positions block: its header, a Summary row and two Lot rows.
DETAILS_OPEN_POSITIONS: Final = (
    "Open Positions,Header," + ",".join(OPEN_POSITIONS_COLUMNS) + "\n"
    f"Open Positions,Data,Summary,Stocks,USD,{SYMBOL},-,{NUMBER},1,2,1462.4936,2,"
    "1462.4936,0,\n"
    f'Open Positions,Data,Lot,Stocks,USD,{SYMBOL},"{DATE}, 10:15:00",365.6234,1,2,'
    f"731.2468,2,731.2468,0,{IDENT}\n"
    f'Open Positions,Data,Lot,Stocks,USD,{SYMBOL},"{DATE}, 10:16:00",365.6234,1,2,'
    f"731.2468,2,731.2468,0,{IDENT}\n"
)
ACCOUNT_INFORMATION: Final = (
    "Account Information,Header,Field Name,Field Value\n"
    f"Account Information,Data,Name,{HOLDER}\n"
    f"Account Information,Data,Account,{ACCOUNT}\n"
    "Account Information,Data,Account Type,Individual\n"
    "Account Information,Data,Base Currency,USD\n"
)
INSTRUMENTS: Final = (
    "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
    "Security ID,Listing Exch,Multiplier,Type,Code\n"
    f"Financial Instrument Information,Data,Stocks,{SYMBOL},{DESCRIPTION},1,{IDENT},"
    "NASDAQ,1,COMMON,\n"
)


def trades_row(cells: dict[str, str], *, extra: tuple[str, ...] = ()) -> str:
    """One Trades ``Data`` row with ``cells`` under :data:`TRADES_COLUMNS`."""
    values = [cells.get(column, "") for column in TRADES_COLUMNS]
    return "Trades,Data," + ",".join(_quoted(value) for value in (*values, *extra)) + "\n"


def detail_cells(kind: str, *, blank_amounts: bool) -> dict[str, str]:
    """A detail row of ``kind``: like the ``Order`` row, or with every amount
    blank (as a ``ClosedLot`` row's Quantity can be)."""
    if blank_amounts:
        return {
            "DataDiscriminator": kind,
            "Asset Category": "Stocks",
            "Currency": "USD",
            "Symbol": SYMBOL,
            "Date/Time": DATE,
            "Code": IDENT,
        }
    return {**ORDER_CELLS, "DataDiscriminator": kind}


def activity_statement(
    kind: str, *, totals: str, open_positions: bool = False, blank_amounts: bool = False
) -> str:
    """A statement whose Trades block (lines 3-5) holds an ``Order`` row and a
    ``kind`` row.

    ``totals`` is ``"none"`` for no ``SubTotal``/``Total`` rows, ``"order"`` for
    totals of the ``Order`` row alone or ``"all"`` for totals of both rows.
    ``open_positions`` adds a details-on Open Positions block after Trades.
    Account Information and Financial Instrument Information follow.
    """
    order = trades_row(ORDER_CELLS)
    detail = trades_row(detail_cells(kind, blank_amounts=blank_amounts))
    if totals == "none":
        trades = TRADES_HEADER + order + detail
    elif totals == "order":
        head, _, tail = with_trades_totals(TRADES_HEADER + order).partition(order)
        trades = head + order + detail + tail
    else:
        assert totals == "all"
        trades = with_trades_totals(TRADES_HEADER + order + detail)
    blocks = (
        STATEMENT,
        trades,
        DETAILS_OPEN_POSITIONS if open_positions else "",
        ACCOUNT_INFORMATION,
        INSTRUMENTS,
    )
    return "".join(blocks)


def standard_statement() -> str:
    """The same statement with only the ``Order`` row and its totals: the
    standard layout, which Schedule OS accepts."""
    return "".join(
        (
            STATEMENT,
            with_trades_totals(TRADES_HEADER + trades_row(ORDER_CELLS)),
            ACCOUNT_INFORMATION,
        )
    )


# ---- Flex Queries --------------------------------------------------------

FLEX_QUERY: Final = (
    '"ClientAccountID","CurrencyPrimary","Name","AccountType","IBEntity"\n'
    f'"{ACCOUNT}","USD","{HOLDER}","Individual","IBLLC-US"\n'
)


def _quoted(value: str) -> str:
    return f'"{value}"' if "," in value else value
