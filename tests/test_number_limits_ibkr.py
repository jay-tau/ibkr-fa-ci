"""Read-time digit limits on the numbers the IBKR readers take (#1276).

Each Activity Statement reader refuses a number past its kind's limits when it
reads it, with the kind's one fixed message (:mod:`tests._number_limits`), its
section, row and column, and no value, cause or context. The readers are the
splitter's Trades totals check, the Trades and Open Positions readers, the
Dividends, Withholding Tax and Interest readers with their printed totals, Net
Asset Value and Change in NAV, and the Realized & Unrealized Performance
Summary's ``Cost Adj.`` check. The Flex Query's closed-lot and open-position
readers refuse the same way. The splitter checks every Trades amount before the
Trades parser runs, so in a statement a Trades ``Quantity``, ``Basis`` or
``Realized P/L`` past its limit refuses at its file line; ``T. Price``, which
only the Trades parser reads, refuses at the row's index in its block.

New read-limit refusal inputs include numbers one digit or place past each kind's
limit and larger values. They carry the digit run ``6131`` inside the breaching
number; no refusal may print it. A ``Cost Adj.`` zero such as ``0E-21`` has no
digit to mark, so it is checked
against its own text. Each limit is also pinned from the other side: a value
exactly at it is read with every digit (or reaches the check that follows), and
a value that an earlier check refuses keeps that check's refusal.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import MAX_PREC, Decimal, localcontext
from pathlib import Path
from typing import Final

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import (
    ClosedLot,
    DistributionKind,
    Dividend,
    ExecutionEvent,
    OpenLot,
    StatementPeriod,
)
from ibkr_fa.models.net_asset_value import NavRefusalReason
from ibkr_fa.parser import parse_closed_lots, parse_open_positions
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    Section,
    build_dec31_close_prices,
    build_instrument_lookup,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_trades,
    parse_activity_statement_withholding_tax,
    split_activity_statement_sections,
    validate_reconciliation,
)
from ibkr_fa.parser.activity_statement.change_in_nav_income import (
    ChangeInNavIncomeError,
    check_income_against_change_in_nav,
)
from ibkr_fa.parser.activity_statement.net_asset_value import (
    parse_change_in_nav_fields,
    parse_net_asset_value,
)
from ibkr_fa.parser.activity_statement.performance_summary import check_cost_adjustments
from ibkr_fa.pipeline_dispatch import _dispatch_activity_statement
from ibkr_fa.types import Ticker
from tests._activity_statement_blocks import TRADES_HEADER_RECORD as _HEADER
from tests._activity_statement_blocks import change_in_nav_block as _change_block
from tests._activity_statement_blocks import csv_records_text as _csv_text
from tests._activity_statement_blocks import edit_trades_block as _edit
from tests._activity_statement_blocks import nav_block as _nav_block
from tests._activity_statement_blocks import nav_row as _nav_row
from tests._activity_statement_blocks import performance_summary_block as _summary_block
from tests._activity_statement_blocks import trades_order_record as _order
from tests._activity_statement_blocks import twr_block as _twr_block
from tests._activity_statement_rows import ZERO_QUANTITY_REFUSAL as _ZERO_QUANTITY
from tests._activity_statement_rows import fifo_open_lots as _open_lots
from tests._activity_statement_rows import (
    make_dividend_row,
    make_fii_row,
    make_interest_row,
    make_trade_row,
    make_wht_row,
)
from tests._activity_statement_rows import open_position_close_row as _make_op_summary_row
from tests._activity_statement_rows import open_position_lot_row as _lot
from tests._activity_statement_rows import open_position_row as _make_op_row
from tests._change_in_nav_checks import RE_EXPORT_HINT as _RE_EXPORT
from tests._crash_reports import (
    assert_no_markers,
    assert_prints_no_marker,
    assert_unchained_refusal,
)
from tests._flex_lot_rows import (
    closing_execution_for,
    make_closed_lot_row,
    with_closing_executions,
)
from tests._flex_section_rows import make_open_position_row
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _run_schedule_fa
from tests._ibkr_pipeline_inputs import PIPELINE_ACCOUNT_INFO_SECTION as _ACCOUNT_INFO_SECTION
from tests._ibkr_pipeline_inputs import (
    PIPELINE_AS_OPEN_POSITIONS_SECTION as _AS_OPEN_POSITIONS_SECTION,
)
from tests._ibkr_pipeline_inputs import PIPELINE_AS_TRADES_SECTION as _AS_TRADES_SECTION
from tests._ibkr_pipeline_inputs import PIPELINE_OPEN_POSITIONS_SECTION as _OPEN_POSITIONS_SECTION
from tests._ibkr_pipeline_inputs import (
    PIPELINE_TRADES_WITH_CLOSED_LOT as _TRADES_SECTION_WITH_CLOSED_LOT,
)
from tests._ibkr_pipeline_inputs import write_activity_statement_csv
from tests._ibkr_pipeline_inputs import write_pipeline_flex_csv as write_flex_csv
from tests._ibkr_pipeline_inputs import write_pipeline_user_toml as write_user_toml
from tests._income_blocks import HEADER_LINE, block, summary_row, total_row
from tests._number_limits import MONEY, PRICE, QUANTITY
from tests._statement_period import ALL_DATES
from tests._trades_totals import with_trades_totals

_MARKER: Final = "6131"
_MARKERS: Final = (_MARKER,)
_LIMITS: Final = (QUANTITY, MONEY, PRICE)
_FIXTURES: Final = Path(__file__).parent / "fixtures"


def _whole(digits: int, *, sign: str = "") -> str:
    """A whole number of ``digits`` digits that starts with the marker."""
    return sign + _MARKER + "0" * (digits - len(_MARKER))


def _places(places: int, *, whole: str = "0", sign: str = "") -> str:
    """``whole`` with ``places`` decimal places, the marker last."""
    return f"{sign}{whole}." + "0" * (places - len(_MARKER)) + _MARKER


def _size(text: str) -> tuple[int, int]:
    """``text``'s whole digits and decimal places, as the limits count them."""
    value = Decimal(text)
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return max(value.adjusted() + 1, 0), max(-exponent, 0)


def _refusal(section: str, row: int, column: str, message: str) -> ParseError:
    return ParseError(section=section, row_index=row, column=column, message=message, raw_value="")


def _text(error: ParseError) -> str:
    where = f"row {error.row_index}"
    if error.column is not None:
        where += f", column {error.column!r}"
    return f"ParseError in section {error.section!r} at {where}: {error.message}"


def _assert_refused(
    error: BaseException, expected: ParseError, *, markers: tuple[str, ...] = _MARKERS
) -> None:
    """``error`` is ``expected`` and prints exactly its text, with no marker,
    cause or context."""
    assert_unchained_refusal(error, expected, _text(expected), markers=markers)


def _assert_same_refusal(error: BaseException, twin: BaseException) -> None:
    """``error`` is ``twin``, the refusal of the same input with an in-limit
    value, and prints no marker: the earlier check still refuses first."""
    assert isinstance(twin, ParseError)
    assert twin.message not in _LIMITS
    assert type(error) is type(twin)
    assert error == twin
    assert str(error) == str(twin)
    assert_prints_no_marker(error, markers=_MARKERS)


def _raised(call: Callable[[], object]) -> ParseError:
    with pytest.raises(ParseError) as raised:
        call()
    return raised.value


@pytest.mark.parametrize(
    ("text", "size"),
    [
        (_whole(13), (13, 0)),
        (_whole(16, sign="-"), (16, 0)),
        (_whole(10), (10, 0)),
        (_places(21), (0, 21)),
        (_places(21, whole="4188", sign="-"), (4, 21)),
        (_whole(12), (12, 0)),
        (_whole(15), (15, 0)),
        (_whole(9), (9, 0)),
        (_places(20, whole="5"), (1, 20)),
        ("0E-" + _MARKER, (0, 6131)),
        ("0E+" + _MARKER, (6132, 0)),
        ("0E-21", (0, 21)),
        ("0E+15", (16, 0)),
    ],
)
def test_the_test_numbers_have_the_sizes_the_cases_name(text: str, size: tuple[int, int]) -> None:
    assert _size(text) == size


# --- The Trades parser ----------------------------------------------------------

_TRADES: Final = "Trades"
_INSTRUMENTS: Final = build_instrument_lookup([make_fii_row()])
# A closing sale of 5 of the opening row's shares.
_CLOSE: Final = {"Code": "C", "Quantity": "-5", "Basis": "-206.25", "Realized P/L": "9.50"}


def _trade(cells: dict[str, str], *, closing: bool = False) -> dict[str, str]:
    """:func:`make_trade_row`'s opening buy of 10 shares, or a closing sale of
    5, with ``cells`` replaced."""
    return {**make_trade_row(), **(_CLOSE if closing else {}), **cells}


def _parse_trades(row: dict[str, str]) -> list[ExecutionEvent]:
    return parse_activity_statement_trades([row], instruments=_INSTRUMENTS, period=ALL_DATES)


@pytest.mark.parametrize(
    ("closing", "cells", "column", "message"),
    [
        pytest.param(False, {"Quantity": _whole(13)}, "Quantity", QUANTITY, id="quantity-13-whole"),
        pytest.param(
            False, {"Quantity": _places(21)}, "Quantity", QUANTITY, id="quantity-21-places"
        ),
        pytest.param(
            True,
            {"Quantity": _whole(13, sign="-")},
            "Quantity",
            QUANTITY,
            id="closing-quantity-minus-13-whole",
        ),
        pytest.param(
            True,
            {"Quantity": _places(21, whole="5", sign="-")},
            "Quantity",
            QUANTITY,
            id="closing-quantity-minus-21-places",
        ),
        # The limit check is part of reading the number, so an absurd zero
        # refuses there, before the zero-quantity check.
        pytest.param(
            False, {"Quantity": "0E-" + _MARKER}, "Quantity", QUANTITY, id="zero-quantity-places"
        ),
        pytest.param(False, {"T. Price": _whole(10)}, "T. Price", PRICE, id="price-10-whole"),
        pytest.param(
            False,
            {"T. Price": _places(21, whole="41")},
            "T. Price",
            PRICE,
            id="price-21-places",
        ),
        pytest.param(
            True, {"T. Price": _whole(10)}, "T. Price", PRICE, id="closing-price-10-whole"
        ),
        # Read before the closing trade's negative-price check.
        pytest.param(
            True,
            {"T. Price": _whole(10, sign="-")},
            "T. Price",
            PRICE,
            id="closing-price-minus-10-whole",
        ),
        pytest.param(False, {"Basis": _whole(16)}, "Basis", MONEY, id="basis-16-whole"),
        pytest.param(
            False, {"Basis": _places(21, whole="412")}, "Basis", MONEY, id="basis-21-places"
        ),
        pytest.param(
            True,
            {"Basis": _whole(16, sign="-")},
            "Basis",
            MONEY,
            id="closing-basis-minus-16-whole",
        ),
        pytest.param(
            True,
            {"Basis": _places(21, whole="206", sign="-")},
            "Basis",
            MONEY,
            id="closing-basis-minus-21-places",
        ),
        # Read before the sign check, so a long row's negative basis refuses
        # at its limit.
        pytest.param(
            False,
            {"Basis": _whole(16, sign="-")},
            "Basis",
            MONEY,
            id="basis-sign-mismatch-16-whole",
        ),
        pytest.param(
            True,
            {"Realized P/L": _whole(16)},
            "Realized P/L",
            MONEY,
            id="realized-16-whole",
        ),
        pytest.param(
            True,
            {"Realized P/L": _places(21, whole="9", sign="-")},
            "Realized P/L",
            MONEY,
            id="realized-minus-21-places",
        ),
    ],
)
def test_trades_parser_refuses_a_number_past_its_limits(
    closing: bool, cells: dict[str, str], column: str, message: str
) -> None:
    """Trades numbers past their limits refuse at their row's index in the
    block, including the first refusing sizes. Main read each one with every
    digit; the zero, the negative closing price and the sign mismatch then met the checks that
    follow, which they now never reach."""
    row = _trade(cells, closing=closing)

    error = _raised(lambda: _parse_trades(row))

    _assert_refused(error, _refusal(_TRADES, 0, column, message))


@pytest.mark.parametrize(
    ("closing", "cells", "field", "expected"),
    [
        pytest.param(False, {"Quantity": _whole(12)}, "quantity", _whole(12), id="quantity-12"),
        pytest.param(
            False, {"Quantity": _places(20)}, "quantity", _places(20), id="quantity-20-places"
        ),
        pytest.param(
            True,
            {"Quantity": _whole(12, sign="-")},
            "quantity",
            _whole(12),
            id="closing-quantity-12",
        ),
        pytest.param(
            True,
            {"Quantity": _places(20, whole="5", sign="-")},
            "quantity",
            _places(20, whole="5"),
            id="closing-quantity-20-places",
        ),
        pytest.param(True, {"T. Price": _whole(9)}, "trade_price", _whole(9), id="price-9"),
        pytest.param(
            True,
            {"T. Price": _places(20, whole="41")},
            "trade_price",
            _places(20, whole="41"),
            id="price-20-places",
        ),
        pytest.param(False, {"Basis": _whole(15)}, "acquisition_cost", _whole(15), id="basis-15"),
        pytest.param(
            False,
            {"Basis": _places(20, whole="412")},
            "acquisition_cost",
            _places(20, whole="412"),
            id="basis-20-places",
        ),
        # A closing row's net proceeds are its relieved Basis plus its
        # Realized P/L (206.25 and 9.50 by default).
        pytest.param(
            True,
            {"Basis": _whole(15, sign="-")},
            "net_proceeds",
            "613100000000009.50",
            id="closing-basis-15",
        ),
        pytest.param(
            True,
            {"Realized P/L": _whole(15)},
            "net_proceeds",
            "613100000000206.25",
            id="realized-15",
        ),
        pytest.param(
            True,
            {"Realized P/L": _places(20, whole="9")},
            "net_proceeds",
            "215.25000000000000006131",
            id="realized-20-places",
        ),
    ],
)
def test_trades_parser_reads_a_number_at_its_limits(
    closing: bool, cells: dict[str, str], field: str, expected: str
) -> None:
    """A quantity of 12 whole digits or 20 places, a price of 9 or 20 and an
    amount of 15 or 20 read with every digit."""
    [event] = _parse_trades(_trade(cells, closing=closing))

    assert getattr(event, field).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"Code": "X"}, {"Quantity": _whole(13)}, id="code-before-quantity"),
        pytest.param({"Currency": "EUR"}, {"T. Price": _whole(10)}, id="currency-before-price"),
        pytest.param({"Quantity": "NaN"}, {"Basis": _whole(16)}, id="non-finite-before-basis"),
        pytest.param({"Quantity": "1x"}, {"T. Price": _whole(10)}, id="malformed-before-price"),
        pytest.param({"Quantity": "0.00"}, {"Basis": _whole(16)}, id="zero-before-basis"),
        pytest.param({"Basis": "Infinity"}, {"T. Price": _whole(10)}, id="basis-before-price"),
        pytest.param(
            {"Quantity": "1x"},
            {"Quantity": _whole(13) + "x"},
            id="malformed-quantity-stays-malformed",
        ),
        # A NaN may carry digits; it is still not finite.
        pytest.param(
            {"Quantity": "NaN"},
            {"Quantity": "NaN" + _whole(13)},
            id="non-finite-quantity-stays-non-finite",
        ),
    ],
)
def test_trades_parser_keeps_a_refusal_that_comes_first(
    earlier: dict[str, str], absurd: dict[str, str]
) -> None:
    """A row an earlier check refuses keeps that refusal, whatever later or
    malformed number it holds."""
    twin = _raised(lambda: _parse_trades(_trade(earlier)))

    error = _raised(lambda: _parse_trades(_trade({**earlier, **absurd})))

    _assert_same_refusal(error, twin)


def test_an_in_limit_zero_trades_quantity_keeps_its_refusal() -> None:
    quantity = "0.0000"
    error = _raised(lambda: _parse_trades(_trade({"Quantity": quantity})))

    _assert_refused(error, _refusal(_TRADES, 0, "Quantity", _ZERO_QUANTITY), markers=("0000",))


# --- The splitter's Trades totals check -----------------------------------------

# The lines of tests._activity_statement_blocks._BLOCK: 1 the header, 2-3
# XQAA's buy and sell, 4 their SubTotal, 5 XQAB's buy, 6 its SubTotal, 7 the
# Total.
_DATA_LINES: Final = frozenset({2, 3, 5})


def _split_rows(tmp_path: Path, text: str) -> list[dict[str, str]]:
    path = tmp_path / "statement.csv"
    path.write_text(text, encoding="utf-8")
    sections = split_activity_statement_sections(path)
    return [row for block in sections[KnownActivitySection.TRADES] for row in block.rows]


def _split_refusal(tmp_path: Path, text: str) -> ParseError:
    path = tmp_path / "statement.csv"
    path.write_text(text, encoding="utf-8")
    return _raised(lambda: split_activity_statement_sections(path))


@pytest.mark.parametrize(
    ("line", "column", "value", "message"),
    [
        pytest.param(2, "Quantity", _whole(13), QUANTITY, id="quantity-13-whole"),
        pytest.param(2, "Quantity", _places(21, whole="37"), QUANTITY, id="quantity-21-places"),
        pytest.param(
            3, "Quantity", _whole(13, sign="-"), QUANTITY, id="closing-quantity-minus-13-whole"
        ),
        pytest.param(
            3,
            "Quantity",
            _places(21, whole="12", sign="-"),
            QUANTITY,
            id="closing-quantity-minus-21-places",
        ),
        pytest.param(2, "Quantity", "0E-" + _MARKER, QUANTITY, id="zero-quantity-places"),
        pytest.param(2, "Proceeds", _whole(16, sign="-"), MONEY, id="proceeds-minus-16-whole"),
        pytest.param(
            2,
            "Comm/Fee",
            _places(21, whole="1", sign="-"),
            MONEY,
            id="commission-minus-21-places",
        ),
        pytest.param(2, "Basis", _whole(16), MONEY, id="basis-16-whole"),
        pytest.param(2, "Basis", _places(21, whole="4188"), MONEY, id="basis-21-places"),
        pytest.param(3, "Basis", _whole(16, sign="-"), MONEY, id="closing-basis-minus-16-whole"),
        pytest.param(3, "Realized P/L", _whole(16), MONEY, id="realized-16-whole"),
        pytest.param(3, "Realized P/L", _places(21, whole="98"), MONEY, id="realized-21-places"),
        pytest.param(3, "MTM P/L", _whole(16, sign="-"), MONEY, id="mtm-minus-16-whole"),
        pytest.param(4, "Quantity", _whole(13), QUANTITY, id="subtotal-quantity-13-whole"),
        pytest.param(
            4,
            "Quantity",
            _places(21, whole="25"),
            QUANTITY,
            id="subtotal-quantity-21-places",
        ),
        pytest.param(4, "Basis", _whole(16), MONEY, id="subtotal-basis-16-whole"),
        pytest.param(
            6,
            "Proceeds",
            _places(21, whole="946", sign="-"),
            MONEY,
            id="subtotal-proceeds-minus-21-places",
        ),
        pytest.param(7, "Basis", _whole(16), MONEY, id="total-basis-16-whole"),
        pytest.param(
            7, "Realized P/L", _places(21, whole="98"), MONEY, id="total-realized-21-places"
        ),
        pytest.param(
            7, "Comm/Fee", _whole(16, sign="-"), MONEY, id="total-commission-minus-16-whole"
        ),
    ],
)
def test_trades_totals_check_refuses_a_number_past_its_limits(
    tmp_path: Path, line: int, column: str, value: str, message: str
) -> None:
    """The splitter reads every Trades data, SubTotal and Total amount for its
    totals check, so each refuses past its limit at its file line, before any
    sum is compared. A data row is re-totalled, so main's splitter passed it;
    a SubTotal or Total no longer adds up, which main refused as a mismatch."""
    text = _edit(line, column, value)
    if line in _DATA_LINES:
        text = with_trades_totals(text)

    error = _split_refusal(tmp_path, text)

    _assert_refused(error, _refusal(_TRADES, line, column, message))


def _buy(symbol: str, quantity: str, basis: str) -> list[str]:
    return _order(symbol, quantity, "1", "1", "-1", "0", basis, "0", "0", "O")


@pytest.mark.parametrize(
    ("rows", "line", "column", "message"),
    [
        # Two buys whose 13-digit SubTotal Quantity is on line 4.
        pytest.param(
            (
                _buy("XQAA", "961310000000", "961310000000"),
                _buy("XQAA", "100000000000", "100000000000"),
            ),
            4,
            "Quantity",
            QUANTITY,
            id="subtotal-quantity-13-whole",
        ),
        # Two symbols whose SubTotals add up to a 16-digit Total Basis on line 6.
        pytest.param(
            (_buy("XQAA", "1", "961310000000000"), _buy("XQAB", "1", "100000000000000")),
            6,
            "Basis",
            MONEY,
            id="total-basis-16-whole",
        ),
    ],
)
def test_a_printed_trades_total_past_its_limit_refuses_though_its_rows_are_within(
    tmp_path: Path, rows: tuple[list[str], ...], line: int, column: str, message: str
) -> None:
    """The printed total adds its rows up exactly, as main checked and
    passed, but has a digit more than its kind allows."""
    text = with_trades_totals(_csv_text(_HEADER, *rows))
    assert "10613100000" in text

    error = _split_refusal(tmp_path, text)

    _assert_refused(error, _refusal(_TRADES, line, column, message))


@pytest.mark.parametrize(
    ("line", "column", "value"),
    [
        pytest.param(2, "Quantity", _whole(12), id="quantity-12"),
        pytest.param(2, "Quantity", _places(20, whole="37"), id="quantity-20-places"),
        pytest.param(3, "Quantity", _whole(12, sign="-"), id="closing-quantity-minus-12"),
        pytest.param(2, "Basis", _whole(15), id="basis-15"),
        pytest.param(2, "Basis", _places(20, whole="4188"), id="basis-20-places"),
        pytest.param(3, "Realized P/L", _whole(15), id="realized-15"),
        pytest.param(2, "MTM P/L", _places(20, whole="41"), id="mtm-20-places"),
        # Only the Trades parser reads T. Price.
        pytest.param(2, "T. Price", _whole(10), id="price-not-read"),
    ],
)
def test_trades_totals_check_reads_a_number_at_its_limits(
    tmp_path: Path, line: int, column: str, value: str
) -> None:
    """A data row at its limit, re-totalled, passes the totals check: its
    SubTotal and Total stay within theirs."""
    rows = _split_rows(tmp_path, with_trades_totals(_edit(line, column, value)))

    assert [row[column] for row in rows][line - 2] == value


@pytest.mark.parametrize(
    ("line", "column", "value", "message"),
    [
        pytest.param(
            7,
            "Quantity",
            _whole(13),
            "the 'Total' row on line 7 has a value in its 'Quantity' column, which must "
            f"be empty. {_RE_EXPORT}",
            id="total-quantity-must-be-empty",
        ),
        pytest.param(
            4,
            "T. Price",
            _whole(10),
            "the 'SubTotal' row on line 4 has a value in its 'T. Price' column, which must "
            f"be empty. {_RE_EXPORT}",
            id="subtotal-price-must-be-empty",
        ),
        pytest.param(
            2,
            "Quantity",
            _whole(13) + "x",
            "the data row on line 2 needs a finite decimal in its 'Quantity' column.",
            id="malformed-data-quantity",
        ),
        pytest.param(
            4,
            "Basis",
            _whole(16) + "x",
            "the 'SubTotal' row on line 4 needs a finite decimal in its 'Basis' column.",
            id="malformed-subtotal-basis",
        ),
    ],
)
def test_trades_totals_check_keeps_a_refusal_that_comes_first(
    tmp_path: Path, line: int, column: str, value: str, message: str
) -> None:
    """A cell that must be empty, or isn't a finite decimal, keeps its refusal
    whatever number it holds."""
    error = _split_refusal(tmp_path, _edit(line, column, value))

    _assert_refused(error, _refusal(_TRADES, line, column, message))


# --- A whole Activity Statement -------------------------------------------------

_STATEMENT_TRADE: Final = {
    "Quantity": "1",
    "T. Price": "212",
    "C. Price": "212.60",
    "Proceeds": "-212",
    "Comm/Fee": "-0.35",
    "Basis": "212.35",
    "Realized P/L": "0",
    "MTM P/L": "0.60",
    "Code": "O",
}
_STATEMENT_POSITION: Final = "XQBK,1,1,212.35,212.35,212.60,212.60,"
# The statement's one trade row is on line 13 of the file.
_TRADE_LINE: Final = 13


def _statement_trades(cells: dict[str, str]) -> str:
    """tests._ibkr_pipeline_inputs's Trades section with ``cells`` of its row replaced."""
    old = "," + ",".join(_STATEMENT_TRADE.values()) + "\n"
    assert _AS_TRADES_SECTION.count(old) == 1
    new = "," + ",".join({**_STATEMENT_TRADE, **cells}.values()) + "\n"
    return _AS_TRADES_SECTION.replace(old, new)


def _statement_position(*, quantity: str = "1", close: str = "212.60") -> str:
    """tests._ibkr_pipeline_inputs's Open Positions section with its Summary row's
    Quantity and Close Price replaced."""
    assert _AS_OPEN_POSITIONS_SECTION.count(_STATEMENT_POSITION) == 1
    new = f"XQBK,{quantity},1,212.35,212.35,{close},212.60,"
    return _AS_OPEN_POSITIONS_SECTION.replace(_STATEMENT_POSITION, new)


def _any_period(_period: StatementPeriod) -> None:
    return None


@pytest.mark.parametrize(
    ("trades", "position", "expected"),
    [
        # The Summary row holds the same quantity, so main reconciled it.
        pytest.param(
            {"Quantity": _whole(13)},
            {"quantity": _whole(13)},
            _refusal(_TRADES, _TRADE_LINE, "Quantity", QUANTITY),
            id="quantity-13-whole",
        ),
        pytest.param(
            {"Quantity": _places(21, whole="1")},
            {},
            _refusal(_TRADES, _TRADE_LINE, "Quantity", QUANTITY),
            id="quantity-21-places",
        ),
        pytest.param(
            {"Basis": _whole(16)},
            {},
            _refusal(_TRADES, _TRADE_LINE, "Basis", MONEY),
            id="basis-16-whole",
        ),
        pytest.param(
            {"Realized P/L": _places(21)},
            {},
            _refusal(_TRADES, _TRADE_LINE, "Realized P/L", MONEY),
            id="realized-21-places",
        ),
        pytest.param(
            {"T. Price": _whole(10)},
            {},
            _refusal(_TRADES, 0, "T. Price", PRICE),
            id="price-10-whole",
        ),
        pytest.param(
            {"T. Price": _places(21, whole="212")},
            {},
            _refusal(_TRADES, 0, "T. Price", PRICE),
            id="price-21-places",
        ),
        pytest.param(
            {},
            {"close": _whole(10)},
            _refusal("Open Positions", 0, "Close Price", PRICE),
            id="close-price-10-whole",
        ),
        pytest.param(
            {},
            {"close": _places(21, whole="212")},
            _refusal("Open Positions", 0, "Close Price", PRICE),
            id="close-price-21-places",
        ),
        pytest.param(
            {},
            {"quantity": _whole(13)},
            _refusal("Open Positions", 0, "Quantity", QUANTITY),
            id="position-quantity-13-whole",
        ),
    ],
)
def test_a_statement_number_past_its_limits_refuses_at_its_first_reader(
    tmp_path: Path, trades: dict[str, str], position: dict[str, str], expected: ParseError
) -> None:
    """Read through the pipeline's Activity Statement entry: the totals check
    refuses a Trades Quantity, Basis or Realized P/L at its file line, the
    Trades parser a T. Price at its row, and the Open Positions readers a
    Summary row's Close Price or Quantity. Main read each one; it refused only
    the 13-digit Summary Quantity, later, as a reconciliation mismatch (no
    trade row within its limit can reach it)."""
    path = write_activity_statement_csv(
        tmp_path,
        trades_section=_statement_trades(trades),
        open_positions_section=_statement_position(**position),
    )

    error = _raised(lambda: _dispatch_activity_statement(path, period_validator=_any_period))

    _assert_refused(error, expected)


def test_the_statement_cases_read_without_a_change() -> None:
    """The control for the two builders above."""
    assert _statement_trades({}) == _AS_TRADES_SECTION
    assert _statement_position() == _AS_OPEN_POSITIONS_SECTION


# --- Open Positions: Summary Close Price and Quantity, Lot Quantity -------------

_OPEN_POSITIONS: Final = "Open Positions"


@pytest.mark.parametrize(
    "close",
    [
        pytest.param(_whole(10), id="10-whole"),
        pytest.param(_places(21, whole="45"), id="21-places"),
    ],
)
def test_close_price_refuses_past_its_limits(close: str) -> None:
    rows = [_make_op_summary_row(symbol="XQAW", close_price=close)]

    error = _raised(lambda: build_dec31_close_prices(rows))

    _assert_refused(error, _refusal(_OPEN_POSITIONS, 0, "Close Price", PRICE))


@pytest.mark.parametrize("close", [_whole(9), _places(20, whole="45")], ids=["9", "20-places"])
def test_close_price_reads_at_its_limits(close: str) -> None:
    prices = build_dec31_close_prices([_make_op_summary_row(symbol="XQAW", close_price=close)])

    assert prices[Ticker("XQAW")].as_tuple() == Decimal(close).as_tuple()


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"Currency": "EUR"}, _whole(10), id="currency"),
        pytest.param({"Close Price": "1x"}, _whole(10) + "x", id="malformed"),
        pytest.param({"Close Price": "NaN"}, "NaN" + _whole(10), id="non-finite"),
    ],
)
def test_close_price_keeps_a_refusal_that_comes_first(earlier: dict[str, str], absurd: str) -> None:
    row = {**_make_op_summary_row(symbol="XQAW", close_price="45.60"), **earlier}
    twin = _raised(lambda: build_dec31_close_prices([row]))

    error = _raised(lambda: build_dec31_close_prices([{**row, "Close Price": absurd}]))

    _assert_same_refusal(error, twin)


def test_a_duplicate_summary_row_refuses_before_its_close_price() -> None:
    rows = [
        _make_op_summary_row(symbol="XQAW", close_price="45.60"),
        _make_op_summary_row(symbol="XQAW", close_price="45.60"),
    ]
    twin = _raised(lambda: build_dec31_close_prices(rows))
    rows[1] = _make_op_summary_row(symbol="XQAW", close_price=_whole(10))

    error = _raised(lambda: build_dec31_close_prices(rows))

    _assert_same_refusal(error, twin)


def _lots(quantity: str, basis: str = "412.50") -> list[OpenLot]:
    """The open lots of one opening trade of ``quantity`` shares."""
    instruments = build_instrument_lookup([make_fii_row()])
    return _open_lots([make_trade_row(quantity=quantity, basis=basis)], instruments=instruments)


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(_whole(13), id="13-whole"),
        pytest.param(_places(21, whole="10"), id="21-places"),
        pytest.param(_whole(13, sign="-"), id="short-minus-13-whole"),
    ],
)
def test_summary_quantity_refuses_past_its_limits(quantity: str) -> None:
    """Read before it is compared with the reconstructed lots of 10 shares:
    main's tolerance passed the 21-place Quantity and refused the others as a
    reconciliation mismatch."""
    rows = [_make_op_row(symbol="XQAW", quantity=quantity)]

    error = _raised(lambda: validate_reconciliation(_lots("10"), rows))

    _assert_refused(error, _refusal(_OPEN_POSITIONS, 0, "Quantity", QUANTITY))


@pytest.mark.parametrize(
    ("quantity", "basis"),
    [
        pytest.param(_whole(12), "412.50", id="12"),
        pytest.param(_places(20, whole="10"), "412.50", id="20-places"),
        pytest.param(_whole(12, sign="-"), "-412.50", id="short-minus-12"),
    ],
)
def test_summary_and_lot_quantities_read_at_their_limits(quantity: str, basis: str) -> None:
    """A Summary row and its one Lot row at the limit reconcile with the lots
    of a trade of that many shares."""
    rows = [
        _make_op_row(symbol="XQAW", quantity=quantity),
        _lot(symbol="XQAW", quantity=quantity),
    ]

    validate_reconciliation(_lots(quantity, basis), rows)


def test_summary_quantity_at_its_limit_reaches_the_lot_match() -> None:
    """With no lots, a 12-digit Summary Quantity reaches the next check."""
    rows = [_make_op_row(symbol="XQAW", quantity=_whole(12))]
    twin = _raised(
        lambda: validate_reconciliation([], [_make_op_row(symbol="XQAW", quantity="10")])
    )

    error = _raised(lambda: validate_reconciliation([], rows))

    _assert_same_refusal(error, twin)
    assert (error.row_index, error.column) == (0, "Symbol")


@pytest.mark.parametrize(
    "quantities",
    [
        pytest.param((_whole(13),), id="13-whole"),
        # Two Lot rows that split the Summary row's 10 exactly, as main passed.
        pytest.param((_places(21, whole="5"), "4.999999999999999993869"), id="21-places-adding-up"),
        pytest.param((_whole(13, sign="-"),), id="minus-13-whole"),
        # Read before the zero check.
        pytest.param(("0E-" + _MARKER,), id="zero-places"),
    ],
)
def test_lot_quantity_refuses_past_its_limits(quantities: tuple[str, ...]) -> None:
    """Pass 1 reads each Lot row's Quantity before the rows are tied to their
    Summary row: main passed the two 21-place rows and refused the others as
    not adding up, of the wrong sign or zero."""
    rows = [
        _make_op_row(symbol="XQAW", quantity="10"),
        *(_lot(symbol="XQAW", quantity=quantity) for quantity in quantities),
    ]

    error = _raised(lambda: validate_reconciliation(_lots("10"), rows))

    _assert_refused(error, _refusal(_OPEN_POSITIONS, 1, "Quantity", QUANTITY))


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"Symbol": ""}, _whole(13), id="blank-symbol"),
        pytest.param({"Currency": ""}, _whole(13), id="blank-currency"),
        pytest.param({"Quantity": "NaN"}, "NaN" + _whole(13), id="non-finite"),
        pytest.param({"Quantity": "1x"}, _whole(13) + "x", id="malformed"),
    ],
)
def test_lot_quantity_keeps_a_refusal_that_comes_first(
    earlier: dict[str, str], absurd: str
) -> None:
    lot = {**_lot(symbol="XQAW", quantity="10"), **earlier}
    summary = _make_op_row(symbol="XQAW", quantity="10")
    twin = _raised(lambda: validate_reconciliation(_lots("10"), [summary, lot]))

    error = _raised(
        lambda: validate_reconciliation(_lots("10"), [summary, {**lot, "Quantity": absurd}])
    )

    _assert_same_refusal(error, twin)


def test_an_in_limit_zero_lot_quantity_keeps_its_refusal() -> None:
    rows = [_make_op_row(symbol="XQAW", quantity="10"), _lot(symbol="XQAW", quantity="0.0000")]

    error = _raised(lambda: validate_reconciliation(_lots("10"), rows))

    _assert_refused(
        error, _refusal(_OPEN_POSITIONS, 1, "Quantity", _ZERO_QUANTITY), markers=("0000",)
    )


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"Quantity": "NaN"}, "NaN" + _whole(13), id="non-finite"),
        pytest.param({"Quantity": "1x"}, _whole(13) + "x", id="malformed"),
    ],
)
def test_summary_quantity_keeps_a_refusal_that_comes_first(
    earlier: dict[str, str], absurd: str
) -> None:
    row = {**_make_op_row(symbol="XQAW", quantity="10"), **earlier}
    twin = _raised(lambda: validate_reconciliation(_lots("10"), [row]))

    error = _raised(lambda: validate_reconciliation(_lots("10"), [{**row, "Quantity": absurd}]))

    _assert_same_refusal(error, twin)


# --- Dividends, Withholding Tax and Interest ------------------------------------

_DIVIDENDS: Final = KnownActivitySection.DIVIDENDS
_WITHHOLDING: Final = KnownActivitySection.WITHHOLDING_TAX
_INTEREST: Final = KnownActivitySection.INTEREST
_INCOME_KINDS: Final = pytest.mark.parametrize(
    "kind", [_DIVIDENDS, _WITHHOLDING, _INTEREST], ids=lambda kind: kind.value
)


def _income_row(kind: KnownActivitySection, amount: str | None = None) -> dict[str, str]:
    """The section test module's default row of ``kind``, with ``amount``; a
    negative Interest amount is debit interest."""
    if kind is _DIVIDENDS:
        row = make_dividend_row()
    elif kind is _WITHHOLDING:
        row = make_wht_row()
    elif amount is not None and amount.startswith("-"):
        row = make_interest_row(description="USD Debit Interest for Apr-2025")
    else:
        row = make_interest_row()
    return row if amount is None else {**row, "Amount": amount}


def _income_amounts(kind: KnownActivitySection, rows: list[dict[str, str]]) -> list[Decimal]:
    """Parse ``rows`` as one block of ``kind``; return each record's amount."""
    section = block(kind, rows)
    if kind is _DIVIDENDS:
        return [
            row.amount_gross
            for row in parse_activity_statement_dividends(section, period=ALL_DATES)
        ]
    if kind is _WITHHOLDING:
        records = parse_activity_statement_withholding_tax(section, period=ALL_DATES)
        return [row.amount for row in records]
    interest_credits, non_credits = parse_activity_statement_interest(section, period=ALL_DATES)
    return [row.amount_gross for row in interest_credits] + [row.amount for row in non_credits]


def _income_refusal(kind: KnownActivitySection, rows: list[dict[str, str]]) -> ParseError:
    return _raised(lambda: _income_amounts(kind, rows))


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        pytest.param(_DIVIDENDS, _whole(16), id="dividend-16-whole"),
        pytest.param(_DIVIDENDS, _places(21, whole="2"), id="dividend-21-places"),
        pytest.param(_WITHHOLDING, _whole(16, sign="-"), id="withholding-minus-16-whole"),
        pytest.param(_WITHHOLDING, _places(21, sign="-"), id="withholding-minus-21-places"),
        pytest.param(_WITHHOLDING, _whole(16), id="withholding-reversal-16-whole"),
        # Read before the zero-tax check.
        pytest.param(_WITHHOLDING, "-0E-" + _MARKER, id="withholding-zero-places"),
        pytest.param(_INTEREST, _whole(16), id="interest-16-whole"),
        pytest.param(_INTEREST, _places(21, whole="1"), id="interest-21-places"),
        pytest.param(_INTEREST, _whole(16, sign="-"), id="debit-interest-minus-16-whole"),
    ],
)
def test_income_amount_refuses_past_its_limits(kind: KnownActivitySection, amount: str) -> None:
    """A detail row's Amount refuses past the money limit at its row's index
    in the block, before the printed total is compared. Main read each one;
    the zero tax then met the zero-tax check, which it now never reaches."""
    rows = [_income_row(kind, amount), total_row(amount)]

    error = _income_refusal(kind, rows)

    _assert_refused(error, _refusal(kind.value, 0, "Amount", MONEY))


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        pytest.param(_DIVIDENDS, _whole(15), id="dividend-15"),
        pytest.param(_DIVIDENDS, _places(20, whole="2"), id="dividend-20-places"),
        pytest.param(_WITHHOLDING, _whole(15, sign="-"), id="withholding-minus-15"),
        pytest.param(_WITHHOLDING, _places(20, sign="-"), id="withholding-minus-20-places"),
        pytest.param(_INTEREST, _whole(15), id="interest-15"),
        pytest.param(_INTEREST, _places(20, whole="1"), id="interest-20-places"),
        pytest.param(_INTEREST, _whole(15, sign="-"), id="debit-interest-minus-15"),
    ],
)
def test_income_amount_reads_at_its_limits(kind: KnownActivitySection, amount: str) -> None:
    """Its printed Total and summary at the limit too."""
    rows = [_income_row(kind, amount), total_row(amount), summary_row(kind.value, "USD", amount)]

    [read] = _income_amounts(kind, rows)

    assert read.as_tuple() == Decimal(amount).as_tuple()


@pytest.mark.parametrize(
    ("kind", "earlier", "absurd"),
    [
        pytest.param(_DIVIDENDS, {"Currency": "EUR"}, _whole(16), id="dividend-currency"),
        pytest.param(_DIVIDENDS, {"Date": "31-02-2026"}, _whole(16), id="dividend-date"),
        pytest.param(
            _DIVIDENDS, {"Description": "no security"}, _whole(16), id="dividend-description"
        ),
        pytest.param(_WITHHOLDING, {"Currency": "EUR"}, _whole(16), id="withholding-currency"),
        pytest.param(
            _WITHHOLDING,
            {"Description": "no security"},
            _whole(16),
            id="withholding-description",
        ),
        pytest.param(_INTEREST, {"Currency": "usd"}, _whole(16), id="interest-currency"),
        pytest.param(_DIVIDENDS, {"Amount": "1x"}, _whole(16) + "x", id="dividend-malformed"),
        pytest.param(
            _WITHHOLDING, {"Amount": "NaN"}, "NaN" + _whole(16), id="withholding-non-finite"
        ),
        pytest.param(_INTEREST, {"Amount": "1x"}, _whole(16) + "x", id="interest-malformed"),
    ],
)
def test_income_amount_keeps_a_refusal_that_comes_first(
    kind: KnownActivitySection, earlier: dict[str, str], absurd: str
) -> None:
    """A row an earlier check refuses keeps that refusal; a malformed or
    non-finite Amount keeps its own, however many digits it has."""
    row = {**_income_row(kind), **earlier}
    twin = _income_refusal(kind, [row])

    error = _income_refusal(kind, [{**row, "Amount": absurd}])

    _assert_same_refusal(error, twin)


def test_an_in_limit_zero_withholding_tax_keeps_its_refusal() -> None:
    amount = "-0.0000"
    error = _income_refusal(_WITHHOLDING, [_income_row(_WITHHOLDING, amount)])

    _assert_refused(
        error,
        _refusal(
            _WITHHOLDING.value,
            0,
            "Amount",
            "withholding-tax amount must be non-zero (got 0). IBKR does not emit zero-tax "
            "rows; this suggests a malformed or truncated row. Negative amounts represent "
            "tax paid (normal); positive amounts represent reversals/refunds (both accepted).",
        ),
        markers=("0000",),
    )


# The default dividend row's ``SYMBOL(ISIN)`` prefix.
_SECURITY: Final = make_dividend_row()["Description"].split(" ", 1)[0]


@pytest.mark.parametrize(
    ("type_slice", "amount", "message"),
    [
        pytest.param(
            f"Cash Dividend USD {_whole(10)} per Share (Ordinary Dividend)",
            "2.80",
            PRICE,
            id="per-share-10-whole",
        ),
        pytest.param(
            f"Cash Dividend USD {_places(21)} per Share", "2.80", PRICE, id="per-share-21-places"
        ),
        pytest.param(
            f"Return Of Capital USD {_whole(10)} per Share", "2.80", PRICE, id="return-of-capital"
        ),
        pytest.param(
            f"Payment In Lieu Of Dividend USD {_places(21, whole='1')} per Share",
            "2.80",
            PRICE,
            id="payment-in-lieu-21-places",
        ),
        # The label is matched ignoring case.
        pytest.param(
            f"cash dividend USD {_whole(10)} per Share", "2.80", PRICE, id="label-any-case"
        ),
        pytest.param(f"Cash in Lieu USD {_whole(16)}", "2.80", MONEY, id="cash-in-lieu-16-whole"),
        pytest.param(
            f"Cash in Lieu USD {_places(21, whole='3')}",
            "2.80",
            MONEY,
            id="cash-in-lieu-21-places",
        ),
        pytest.param(
            f"Cash in Lieu USD {_whole(10)} per Share",
            "2.80",
            PRICE,
            id="cash-in-lieu-per-share-10-whole",
        ),
        # The description is checked before the Amount is read.
        pytest.param(
            f"Cash Dividend USD {_whole(10)} per Share",
            _whole(16),
            PRICE,
            id="before-the-amount",
        ),
    ],
)
def test_dividend_description_amount_refuses_past_its_limits(
    type_slice: str, amount: str, message: str
) -> None:
    """A known kind's description amount is matched by shape and never kept,
    but still refuses past its limit: a per-share rate as a unit price, a
    Cash in Lieu amount as money. Main read the row."""
    rows = [
        make_dividend_row(description=f"{_SECURITY} {type_slice}", amount=amount),
        total_row(amount),
    ]

    error = _income_refusal(_DIVIDENDS, rows)

    _assert_refused(error, _refusal(_DIVIDENDS.value, 0, "Description", message))


def _dividends(rows: list[dict[str, str]]) -> list[Dividend]:
    return parse_activity_statement_dividends(block(_DIVIDENDS, rows), period=ALL_DATES)


@pytest.mark.parametrize(
    ("type_slice", "kind"),
    [
        pytest.param(
            f"Cash Dividend USD {_whole(9)} per Share (Ordinary Dividend)",
            DistributionKind.CASH_DIVIDEND,
            id="per-share-9",
        ),
        pytest.param(
            f"Cash Dividend USD {_places(20)} per Share",
            DistributionKind.CASH_DIVIDEND,
            id="per-share-20-places",
        ),
        # Fifteen whole digits: a money amount, not a unit price.
        pytest.param(
            f"Cash in Lieu USD {_whole(15)}", DistributionKind.CASH_IN_LIEU, id="cash-in-lieu-15"
        ),
        pytest.param(
            f"Cash in Lieu USD {_places(20)}",
            DistributionKind.CASH_IN_LIEU,
            id="cash-in-lieu-20-places",
        ),
        # Not matched by shape, so not checked: these classify as today.
        pytest.param(
            f"Mystery Distribution USD {_whole(30)} per Share",
            DistributionKind.UNRECOGNISED,
            id="unrecognised",
        ),
        pytest.param(
            f"Cash Dividend USD {_whole(30)}",
            DistributionKind.MALFORMED,
            id="malformed-no-per-share",
        ),
        pytest.param(
            f"Cash Dividend USD {_places(30)} per Share - Extra",
            DistributionKind.MALFORMED,
            id="malformed-suffix",
        ),
        pytest.param(
            f"Cash Dividend  USD {_whole(30)} per Share",
            DistributionKind.MALFORMED,
            id="malformed-label-spacing",
        ),
        pytest.param(
            f"Cash Dividend USD -{_whole(30)} per Share",
            DistributionKind.MALFORMED,
            id="malformed-signed",
        ),
        pytest.param(
            f"Cash in Lieu USD {_whole(30)} Extra",
            DistributionKind.MALFORMED,
            id="malformed-cash-in-lieu",
        ),
        pytest.param(
            f"Cash Dividend {_whole(30)} per Share",
            DistributionKind.MALFORMED,
            id="malformed-no-currency",
        ),
    ],
)
def test_dividend_description_reads_at_its_limits_or_when_unmatched(
    type_slice: str, kind: DistributionKind
) -> None:
    """A description amount at its limit reads, and an unrecognised or
    malformed description, which isn't matched by shape, isn't checked."""
    description = f"{_SECURITY} {type_slice}"

    [dividend] = _dividends([make_dividend_row(description=description), total_row("2.80")])

    assert (dividend.kind, dividend.description, dividend.amount_gross) == (
        kind,
        description,
        Decimal("2.80"),
    )


def test_dividend_currency_refuses_before_the_description_amount() -> None:
    description = f"{_SECURITY} Cash Dividend USD {_whole(10)} per Share"
    twin = _income_refusal(_DIVIDENDS, [make_dividend_row(currency="EUR")])

    error = _income_refusal(
        _DIVIDENDS, [make_dividend_row(currency="EUR", description=description)]
    )

    _assert_same_refusal(error, twin)


# --- Printed income totals ------------------------------------------------------


def _footers(kind: KnownActivitySection, footer: str, value: str) -> list[dict[str, str]]:
    """The default row of ``kind`` and its printed ``Total`` row, then for
    ``footer == "summary"`` the ``Total <Section> in USD`` row; the last
    printed row holds ``value``."""
    detail = _income_row(kind)
    if footer == "total":
        return [detail, total_row(value)]
    return [detail, total_row(detail["Amount"]), summary_row(kind.value, "USD", value)]


@_INCOME_KINDS
@pytest.mark.parametrize(
    ("footer", "value"),
    [
        pytest.param("total", _whole(16), id="total-16-whole"),
        pytest.param("total", _places(21, whole="2"), id="total-21-places"),
        pytest.param("total", _whole(16, sign="-"), id="total-minus-16-whole"),
        pytest.param("summary", _whole(16), id="summary-16-whole"),
        pytest.param("summary", _places(21, sign="-"), id="summary-minus-21-places"),
    ],
)
def test_printed_total_refuses_past_its_limits(
    kind: KnownActivitySection, footer: str, value: str
) -> None:
    """A Total or summary row's Amount refuses past the money limit at its
    file line, before it is compared with the rows it adds up (main refused
    these as a mismatch)."""
    rows = _footers(kind, footer, value)

    error = _income_refusal(kind, rows)

    _assert_refused(error, _refusal(kind.value, HEADER_LINE + len(rows), "Amount", MONEY))


# Two in-limit amounts whose sum has 16 whole digits.
_ADDENDS: Final = ("961310000000000", "100000000000000")
_SUM: Final = "1061310000000000"


@_INCOME_KINDS
@pytest.mark.parametrize("footer", ["total", "summary"])
def test_printed_total_past_its_limit_refuses_though_its_rows_are_within(
    kind: KnownActivitySection, footer: str
) -> None:
    """The printed Total (of two rows) or summary (of two runs' Totals) adds
    them up exactly, as main checked and passed, but has a sixteenth whole
    digit."""
    sign = "-" if kind is _WITHHOLDING else ""
    first, second = (_income_row(kind, sign + amount) for amount in _ADDENDS)
    if footer == "total":
        rows = [first, second, total_row(sign + _SUM)]
    else:
        rows = [
            first,
            total_row(first["Amount"]),
            second,
            total_row(second["Amount"]),
            summary_row(kind.value, "USD", sign + _SUM),
        ]

    error = _income_refusal(kind, rows)

    _assert_refused(error, _refusal(kind.value, HEADER_LINE + len(rows), "Amount", MONEY))


@_INCOME_KINDS
@pytest.mark.parametrize("size", ["15", "20-places"])
def test_printed_totals_read_at_their_limits(kind: KnownActivitySection, size: str) -> None:
    sign = "-" if kind is _WITHHOLDING else ""
    amount = _whole(15, sign=sign) if size == "15" else _places(20, whole="1", sign=sign)
    rows = [_income_row(kind, amount), total_row(amount), summary_row(kind.value, "USD", amount)]

    assert _income_amounts(kind, rows) == [Decimal(amount)]


@_INCOME_KINDS
@pytest.mark.parametrize(
    ("footer", "edit", "absurd"),
    [
        pytest.param("total", {"Currency": "TOTAL"}, _whole(16), id="total-currency-not-exact"),
        pytest.param("total", {"Date": "2025-06-30"}, _whole(16), id="total-cell-not-empty"),
        pytest.param("summary", {"Currency": "EUR"}, _whole(16), id="summary-currency-differs"),
        pytest.param("total", {"Amount": "1x"}, _whole(16) + "x", id="total-malformed"),
        pytest.param("summary", {"Amount": "NaN"}, "NaN" + _whole(16), id="summary-non-finite"),
    ],
)
def test_printed_total_keeps_a_refusal_that_comes_first(
    kind: KnownActivitySection, footer: str, edit: dict[str, str], absurd: str
) -> None:
    *rows, last = _footers(kind, footer, "0")
    twin = _income_refusal(kind, [*rows, {**last, **edit}])

    error = _income_refusal(kind, [*rows, {**last, **edit, "Amount": absurd}])

    _assert_same_refusal(error, twin)


# --- Net Asset Value and Change in NAV ------------------------------------------

_NAV: Final = "Net Asset Value"
_CHANGE: Final = "Change in NAV"


def _nav(*rows: tuple[str, str]) -> list[Section]:
    return [_nav_block([_nav_row(name, total) for name, total in rows]), _twr_block()]


@pytest.mark.parametrize(
    ("nav", "change", "expected"),
    [
        pytest.param(
            (("Cash ", "1.00"), ("Total", _whole(16))),
            None,
            _refusal(_NAV, 1, "Current Total", MONEY),
            id="total-row-16-whole",
        ),
        pytest.param(
            (("Cash ", "1.00"), ("Total", _places(21, whole="1"))),
            None,
            _refusal(_NAV, 1, "Current Total", MONEY),
            id="total-row-21-places",
        ),
        pytest.param(
            (("Cash ", "1.00"), ("Total", _whole(16, sign="-"))),
            None,
            _refusal(_NAV, 1, "Current Total", MONEY),
            id="total-row-minus-16-whole",
        ),
        pytest.param(
            (("Cash ", _whole(16)), ("Total", "1.00")),
            None,
            _refusal(_NAV, 0, "Current Total", MONEY),
            id="asset-class-row-16-whole",
        ),
        pytest.param(
            (("Cash ", _places(21, whole="1")), ("Stock", "1.00")),
            (("Ending Value", "2.00"),),
            _refusal(_NAV, 0, "Current Total", MONEY),
            id="asset-class-row-without-total-21-places",
        ),
        pytest.param(
            (("Total", "1.00"),),
            (("Starting Value", "0"), ("Dividends", _whole(16)), ("Ending Value", "1.00")),
            _refusal(_CHANGE, 1, "Field Value", MONEY),
            id="change-field-16-whole",
        ),
        pytest.param(
            (("Total", "1.00"),),
            (("Starting Value", "0"), ("Commissions", _places(21, sign="-"))),
            _refusal(_CHANGE, 1, "Field Value", MONEY),
            id="change-field-minus-21-places",
        ),
        pytest.param(
            (("Total", "1.00"),),
            (("Starting Value", "0"), ("Ending Value", _whole(16))),
            _refusal(_CHANGE, 1, "Field Value", MONEY),
            id="ending-value-16-whole",
        ),
    ],
)
def test_net_asset_value_refuses_a_number_past_its_limits(
    nav: tuple[tuple[str, str], ...],
    change: tuple[tuple[str, str], ...] | None,
    expected: ParseError,
) -> None:
    """Every Current Total and Change in NAV Field Value it reads refuses past
    the money limit at its row's index in its block. Main read each one."""
    changes = None if change is None else [_change_block(list(change))]

    error = _raised(lambda: parse_net_asset_value(_nav(*nav), changes))

    _assert_refused(error, expected)


@pytest.mark.parametrize("value", [_whole(15), _places(20, whole="1")], ids=["15", "20-places"])
def test_net_asset_value_reads_at_its_limits(value: str) -> None:
    summary = parse_net_asset_value(
        _nav(("Cash ", value), ("Total", value)),
        [_change_block([("Starting Value", value), ("Ending Value", value)])],
    )

    assert summary.closing_total is not None
    assert summary.closing_total.as_tuple() == Decimal(value).as_tuple()
    assert summary.cross_checked
    assert summary.asset_class_current_totals["Cash"].as_tuple() == Decimal(value).as_tuple()


def test_a_blank_nav_total_keeps_its_soft_refusal_before_the_asset_class_rows() -> None:
    """The Total row is read first: a blank one is today's soft refusal, and
    the asset-class rows go unread."""
    summary = parse_net_asset_value(_nav(("Cash ", _whole(16)), ("Total", "")), None)

    assert (summary.closing_total, summary.refusal_reason) == (None, NavRefusalReason.BLANK_TOTAL)


# Commissions isn't an income field, so main's income check passed it.
_CHANGE_FIELDS: Final = [("Starting Value", "0"), ("Commissions", _whole(16, sign="-"))]


def test_change_in_nav_fields_refuse_past_their_limits() -> None:
    error = _raised(lambda: parse_change_in_nav_fields([_change_block(_CHANGE_FIELDS)]))

    _assert_refused(error, _refusal(_CHANGE, 1, "Field Value", MONEY))


def test_income_check_against_change_in_nav_refuses_a_field_past_its_limits() -> None:
    """The income commands' Change in NAV check reads the fields the same way (#1279)."""
    sections = {KnownActivitySection.CHANGE_IN_NAV: [_change_block(_CHANGE_FIELDS)]}

    error = _raised(
        lambda: check_income_against_change_in_nav(
            sections,
            base_currency="USD",
            dividends=[],
            withholding_tax=[],
            interest_credits=[],
            interest_non_credits=[],
        )
    )

    _assert_refused(error, _refusal(_CHANGE, 1, "Field Value", MONEY))


@pytest.mark.parametrize("value", [_whole(15), _places(20, whole="1")], ids=["15", "20-places"])
def test_change_in_nav_field_at_its_limits_reaches_the_income_check(value: str) -> None:
    """Read with every digit, then compared: with no Dividends section, a
    nonzero Dividends field refuses as #1279 does."""
    change = [_change_block([("Starting Value", "0"), ("Dividends", value)])]
    fields = parse_change_in_nav_fields(change)
    assert fields["Dividends"].as_tuple() == Decimal(value).as_tuple()

    with pytest.raises(ChangeInNavIncomeError) as raised:
        check_income_against_change_in_nav(
            {KnownActivitySection.CHANGE_IN_NAV: change},
            base_currency="USD",
            dividends=[],
            withholding_tax=[],
            interest_credits=[],
            interest_non_credits=[],
        )

    assert str(raised.value) == (
        "the Activity Statement's Change in NAV shows a nonzero 'Dividends' amount, but the "
        "statement has no 'Dividends' section. Re-export the Activity Statement with the "
        "'Dividends' section included; the run refuses rather than treat the section as empty."
    )


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param(
            [("Starting Value", "0"), ("", "1")],
            [("Starting Value", "0"), ("", _whole(16))],
            id="blank-field-name",
        ),
        pytest.param(
            [("Dividends", "1"), ("Dividends", "1")],
            [("Dividends", "1"), ("Dividends", _whole(16))],
            id="repeated-field-name",
        ),
        pytest.param(
            [("Dividends", "1x")], [("Dividends", _whole(16) + "x")], id="malformed-value"
        ),
    ],
)
def test_change_in_nav_keeps_a_refusal_that_comes_first(
    earlier: list[tuple[str, str]], absurd: list[tuple[str, str]]
) -> None:
    twin = _raised(lambda: parse_change_in_nav_fields([_change_block(earlier)]))

    error = _raised(lambda: parse_change_in_nav_fields([_change_block(absurd)]))

    _assert_same_refusal(error, twin)


# --- Realized & Unrealized Performance Summary ----------------------------------

_PERFORMANCE: Final = KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY.value


@pytest.mark.parametrize(
    ("value", "markers"),
    [
        pytest.param("0E-21", ("0E-21",), id="zero-21-places"),
        pytest.param("-0E-21", ("0E-21",), id="minus-zero-21-places"),
        pytest.param("0." + "0" * 21, ("0." + "0" * 21,), id="zero-21-places-written-out"),
        pytest.param("0E+15", ("0E+15",), id="zero-16-whole"),
        pytest.param("0E-" + _MARKER, _MARKERS, id="zero-6131-places"),
        pytest.param("0E+" + _MARKER, _MARKERS, id="zero-6132-whole"),
    ],
)
def test_a_zero_cost_adjustment_refuses_past_the_money_limits(
    value: str, markers: tuple[str, ...]
) -> None:
    """A zero passes the blank-or-zero check, but its digits still count, at
    the row's file line. Main accepted each one."""
    error = _raised(lambda: check_cost_adjustments([_summary_block("0", value)]))

    _assert_refused(error, _refusal(_PERFORMANCE, 6, "Cost Adj.", MONEY), markers=markers)


@pytest.mark.parametrize("value", ["0E-20", "-0E-20", "0." + "0" * 20, "0E+14", "0" * 15])
def test_a_zero_cost_adjustment_at_the_money_limits_passes(value: str) -> None:
    check_cost_adjustments([_summary_block("0", value)])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_whole(30), id="30-whole"),
        pytest.param(_places(30), id="30-places"),
        pytest.param(_whole(16, sign="-"), id="minus-16-whole"),
        pytest.param(_whole(16) + "x", id="malformed"),
    ],
)
def test_a_nonzero_cost_adjustment_keeps_its_refusal(value: str) -> None:
    """Any nonzero adjustment, however long, keeps the blank-or-zero refusal."""
    twin = _raised(lambda: check_cost_adjustments([_summary_block("0", "5")]))

    error = _raised(lambda: check_cost_adjustments([_summary_block("0", value)]))

    _assert_same_refusal(error, twin)
    assert error.message == (
        "line 6 has a 'Cost Adj.' cell that isn't blank or a zero decimal. A non-zero "
        "adjustment changes a holding's cost basis, which the lots built from the Trades "
        "section may not reflect, so the run refuses rather than file an unverified cost."
    )


# --- The Flex Query's closed lots and open positions ----------------------------

_FLEX_TRADES: Final = "Trades"
_FLEX_POSITIONS: Final = "Open Positions (LOT)"


def _closed_lot(cells: dict[str, str]) -> dict[str, str]:
    return {**make_closed_lot_row(), **cells}


@pytest.mark.parametrize(
    ("cells", "column", "message"),
    [
        pytest.param({"CostBasis": _whole(16)}, "CostBasis", MONEY, id="cost-16-whole"),
        pytest.param(
            {"CostBasis": _places(21, whole="496")}, "CostBasis", MONEY, id="cost-21-places"
        ),
        pytest.param({"FifoPnlRealized": _whole(16)}, "FifoPnlRealized", MONEY, id="pnl-16-whole"),
        pytest.param(
            {"FifoPnlRealized": _places(21, whole="23", sign="-")},
            "FifoPnlRealized",
            MONEY,
            id="pnl-minus-21-places",
        ),
        pytest.param({"Quantity": _whole(13)}, "Quantity", QUANTITY, id="quantity-13-whole"),
        pytest.param(
            {"Quantity": _places(21, whole="12")}, "Quantity", QUANTITY, id="quantity-21-places"
        ),
    ],
)
def test_closed_lot_refuses_a_number_past_its_limits(
    cells: dict[str, str], column: str, message: str
) -> None:
    row = _closed_lot(cells)

    error = _raised(lambda: parse_closed_lots([row]))

    _assert_refused(error, _refusal(_FLEX_TRADES, 0, column, message))


@pytest.mark.parametrize(
    "quantity",
    [
        pytest.param(_whole(13), id="13-whole"),
        pytest.param(_places(21, whole="12"), id="21-places"),
    ],
)
def test_closing_execution_refuses_a_quantity_past_its_limits(quantity: str) -> None:
    """#1286 reads a closing execution's Quantity, so the quantity limit
    applies to it too; the closed lot after it is within the limits."""
    execution = {**closing_execution_for(_closed_lot({})), "Quantity": f"-{quantity}"}

    error = _raised(lambda: parse_closed_lots([execution, _closed_lot({})]))

    _assert_refused(error, _refusal(_FLEX_TRADES, 0, "Quantity", QUANTITY))


@pytest.mark.parametrize(
    ("cells", "field", "expected"),
    [
        pytest.param({"CostBasis": _whole(15)}, "cost_basis_money", _whole(15), id="cost-15"),
        pytest.param(
            {"CostBasis": _places(20, whole="496")},
            "cost_basis_money",
            _places(20, whole="496"),
            id="cost-20-places",
        ),
        # Proceeds are CostBasis (496.20) plus FifoPnlRealized.
        pytest.param(
            {"FifoPnlRealized": _whole(15)},
            "proceeds_money",
            "613100000000496.20",
            id="pnl-15",
        ),
        pytest.param(
            {"FifoPnlRealized": _places(20, whole="23", sign="-")},
            "proceeds_money",
            "473.19999999999999993869",
            id="pnl-minus-20-places",
        ),
        pytest.param({"Quantity": _whole(12)}, "quantity", _whole(12), id="quantity-12"),
        pytest.param(
            {"Quantity": _places(20, whole="12")},
            "quantity",
            _places(20, whole="12"),
            id="quantity-20-places",
        ),
    ],
)
def test_closed_lot_reads_a_number_at_its_limits(
    cells: dict[str, str], field: str, expected: str
) -> None:
    lots: list[ClosedLot] = parse_closed_lots(with_closing_executions(_closed_lot(cells)))

    assert getattr(lots[0], field).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"Buy/Sell": "BUY"}, {"CostBasis": _whole(16)}, id="short-cover"),
        pytest.param({"CurrencyPrimary": "EUR"}, {"Quantity": _whole(13)}, id="currency"),
        pytest.param({"CostBasis": ""}, {"FifoPnlRealized": _whole(16)}, id="blank-cost"),
        pytest.param({"CostBasis": "NaN"}, {"Quantity": _whole(13)}, id="non-finite-cost"),
        pytest.param({"Quantity": "1x"}, {"Quantity": _whole(13) + "x"}, id="malformed"),
    ],
)
def test_closed_lot_keeps_a_refusal_that_comes_first(
    earlier: dict[str, str], absurd: dict[str, str]
) -> None:
    twin = _raised(lambda: parse_closed_lots([_closed_lot(earlier)]))

    error = _raised(lambda: parse_closed_lots([_closed_lot({**earlier, **absurd})]))

    _assert_same_refusal(error, twin)


@pytest.mark.parametrize(
    ("cells", "column", "message"),
    [
        # Each PositionValue is Quantity * MarkPrice to the cent, as main checked.
        pytest.param(
            {"Quantity": _whole(13), "MarkPrice": "1", "PositionValue": _whole(13)},
            "Quantity",
            QUANTITY,
            id="quantity-13-whole",
        ),
        pytest.param(
            {"Quantity": _places(21, whole="25")}, "Quantity", QUANTITY, id="quantity-21-places"
        ),
        pytest.param(
            {"Quantity": "1", "MarkPrice": _whole(10), "PositionValue": _whole(10)},
            "MarkPrice",
            PRICE,
            id="mark-10-whole",
        ),
        pytest.param(
            {"MarkPrice": _places(21, whole="45"), "PositionValue": "1125"},
            "MarkPrice",
            PRICE,
            id="mark-21-places",
        ),
        pytest.param(
            {"Quantity": "61310000", "MarkPrice": "100000000", "PositionValue": _whole(16)},
            "PositionValue",
            MONEY,
            id="position-value-16-whole",
        ),
        pytest.param(
            {"MarkPrice": "45.08", "PositionValue": _places(21, whole="1127")},
            "PositionValue",
            MONEY,
            id="position-value-21-places",
        ),
        pytest.param(
            {"CostBasisPrice": _whole(10)}, "CostBasisPrice", PRICE, id="cost-price-10-whole"
        ),
        pytest.param(
            {"CostBasisPrice": _places(21, whole="38")},
            "CostBasisPrice",
            PRICE,
            id="cost-price-21-places",
        ),
        # Read before the non-negative check.
        pytest.param(
            {"CostBasisPrice": _whole(10, sign="-")},
            "CostBasisPrice",
            PRICE,
            id="cost-price-minus-10-whole",
        ),
        pytest.param(
            {"CostBasisMoney": _whole(16)}, "CostBasisMoney", MONEY, id="cost-money-16-whole"
        ),
        pytest.param(
            {"CostBasisMoney": _places(21, whole="969")},
            "CostBasisMoney",
            MONEY,
            id="cost-money-21-places",
        ),
    ],
)
def test_open_position_refuses_a_number_past_its_limits(
    cells: dict[str, str], column: str, message: str
) -> None:
    """Each number refuses past its limit, a Quantity or MarkPrice before the
    PositionValue is compared with their product. Main read each one; the
    negative CostBasisPrice then met the non-negative check, which it now
    never reaches."""
    row = make_open_position_row(**cells)

    error = _raised(lambda: parse_open_positions([row]))

    _assert_refused(error, _refusal(_FLEX_POSITIONS, 0, column, message))


@pytest.mark.parametrize(
    ("cells", "field", "expected"),
    [
        pytest.param(
            {"Quantity": _whole(12), "MarkPrice": "1", "PositionValue": _whole(12)},
            "quantity",
            _whole(12),
            id="quantity-12",
        ),
        pytest.param(
            {"Quantity": _places(20, whole="25")},
            "quantity",
            _places(20, whole="25"),
            id="quantity-20-places",
        ),
        pytest.param(
            {"Quantity": "1", "MarkPrice": _whole(9), "PositionValue": _whole(9)},
            "mark_price",
            _whole(9),
            id="mark-9",
        ),
        pytest.param(
            {"MarkPrice": _places(20, whole="45"), "PositionValue": "1125"},
            "mark_price",
            _places(20, whole="45"),
            id="mark-20-places",
        ),
        pytest.param(
            {"Quantity": "6131000", "MarkPrice": "100000000", "PositionValue": _whole(15)},
            "quantity",
            "6131000",
            id="position-value-15",
        ),
        pytest.param(
            {"MarkPrice": "45.08", "PositionValue": _places(20, whole="1127")},
            "mark_price",
            "45.08",
            id="position-value-20-places",
        ),
        pytest.param({"CostBasisPrice": _whole(9)}, "quantity", "25", id="cost-price-9"),
        pytest.param(
            {"CostBasisPrice": _places(20, whole="38")},
            "quantity",
            "25",
            id="cost-price-20-places",
        ),
        pytest.param(
            {"CostBasisMoney": _whole(15)}, "cost_basis_money", _whole(15), id="cost-money-15"
        ),
        pytest.param(
            {"CostBasisMoney": _places(20, whole="969")},
            "cost_basis_money",
            _places(20, whole="969"),
            id="cost-money-20-places",
        ),
    ],
)
def test_open_position_reads_a_number_at_its_limits(
    cells: dict[str, str], field: str, expected: str
) -> None:
    """Each number at its limit reads (CostBasisPrice and PositionValue are
    only checked, not kept)."""
    [lot] = parse_open_positions([make_open_position_row(**cells)])

    assert getattr(lot, field).as_tuple() == Decimal(expected).as_tuple()


@pytest.mark.parametrize(
    ("earlier", "absurd"),
    [
        pytest.param({"CurrencyPrimary": "EUR"}, {"Quantity": _whole(13)}, id="currency"),
        pytest.param({"MarkPrice": "NaN"}, {"PositionValue": _whole(16)}, id="non-finite-mark"),
        pytest.param({"PositionValue": "1200"}, {"CostBasisPrice": _whole(10)}, id="mismatch"),
        pytest.param({"CostBasisPrice": "-1"}, {"CostBasisMoney": _whole(16)}, id="negative"),
        pytest.param({"Quantity": "1x"}, {"Quantity": _whole(13) + "x"}, id="malformed"),
        pytest.param({"MarkPrice": "NaN"}, {"MarkPrice": "NaN" + _whole(10)}, id="non-finite"),
    ],
)
def test_open_position_keeps_a_refusal_that_comes_first(
    earlier: dict[str, str], absurd: dict[str, str]
) -> None:
    twin = _raised(lambda: parse_open_positions([make_open_position_row(**earlier)]))

    error = _raised(lambda: parse_open_positions([make_open_position_row(**{**earlier, **absurd})]))

    _assert_same_refusal(error, twin)


# --- Through the CLI --------------------------------------------------------------


def _statement_input(
    tmp_path: Path, trades: dict[str, str], position: dict[str, str] | None = None
) -> Path:
    return write_activity_statement_csv(
        tmp_path,
        trades_section=_statement_trades(trades),
        open_positions_section=_statement_position(**(position or {})),
    )


def _dividend_input(tmp_path: Path, amount: str) -> Path:
    """The dividends fixture with its first dividend's Amount replaced, and
    its printed Total still their sum."""
    text = (_FIXTURES / "e2e_activity_statement_with_dividends_2025.csv").read_text(
        encoding="utf-8"
    )
    old, total = "(Ordinary Dividend),2.10,", "Dividends,Data,Total,,,4.20,"
    assert text.count(old) == 2
    assert text.count(total) == 1
    with localcontext(prec=MAX_PREC):
        new_total = format(Decimal(amount) + Decimal("2.10"), "f")
    text = text.replace(old, f"(Ordinary Dividend),{amount},", 1)
    path = tmp_path / "statement.csv"
    path.write_text(text.replace(total, f"Dividends,Data,Total,,,{new_total},"), encoding="utf-8")
    return path


def _flex_input(
    tmp_path: Path, *, position: tuple[str, str, str] = ("10", "95.4", "954"), cost: str = "260"
) -> Path:
    """tests._ibkr_pipeline_inputs's Flex Query with its open lot's Quantity,
    MarkPrice and PositionValue, and its closed lot's CostBasis, replaced."""
    old_position, old_cost = '"10","95.4","954"', '"260"'
    assert _OPEN_POSITIONS_SECTION.count(old_position) == 1
    assert _TRADES_SECTION_WITH_CLOSED_LOT.count(old_cost) == 1
    new_position = ",".join(f'"{cell}"' for cell in position)
    positions = _OPEN_POSITIONS_SECTION.replace(old_position, new_position)
    closed = _TRADES_SECTION_WITH_CLOSED_LOT.replace(old_cost, f'"{cost}"')
    return write_flex_csv(tmp_path, _ACCOUNT_INFO_SECTION, positions, closed)


_CLI_CASES: Final = [
    pytest.param(
        lambda path: _statement_input(path, {"Quantity": _whole(13)}, {"quantity": _whole(13)}),
        _refusal(_TRADES, _TRADE_LINE, "Quantity", QUANTITY),
        id="statement-quantity-13-whole",
    ),
    pytest.param(
        lambda path: _statement_input(path, {"Quantity": _places(21, whole="1")}),
        _refusal(_TRADES, _TRADE_LINE, "Quantity", QUANTITY),
        id="statement-quantity-21-places",
    ),
    pytest.param(
        lambda path: _statement_input(path, {"T. Price": _whole(10)}),
        _refusal(_TRADES, 0, "T. Price", PRICE),
        id="statement-price-10-whole",
    ),
    pytest.param(
        lambda path: _statement_input(path, {"T. Price": _places(21, whole="212")}),
        _refusal(_TRADES, 0, "T. Price", PRICE),
        id="statement-price-21-places",
    ),
    pytest.param(
        lambda path: _dividend_input(path, _whole(16)),
        _refusal(_DIVIDENDS.value, 0, "Amount", MONEY),
        id="statement-dividend-16-whole",
    ),
    pytest.param(
        lambda path: _dividend_input(path, _places(21, whole="2")),
        _refusal(_DIVIDENDS.value, 0, "Amount", MONEY),
        id="statement-dividend-21-places",
    ),
    pytest.param(
        lambda path: _flex_input(path, position=(_whole(13), "1", _whole(13))),
        _refusal(_FLEX_POSITIONS, 0, "Quantity", QUANTITY),
        id="flex-quantity-13-whole",
    ),
    pytest.param(
        lambda path: _flex_input(path, position=(_places(21, whole="10"), "95.4", "954")),
        _refusal(_FLEX_POSITIONS, 0, "Quantity", QUANTITY),
        id="flex-quantity-21-places",
    ),
    pytest.param(
        lambda path: _flex_input(path, cost=_whole(16)),
        _refusal(_FLEX_TRADES, 1, "CostBasis", MONEY),
        id="flex-cost-16-whole",
    ),
    pytest.param(
        lambda path: _flex_input(path, cost=_places(21, whole="260")),
        _refusal(_FLEX_TRADES, 1, "CostBasis", MONEY),
        id="flex-cost-21-places",
    ),
]


@pytest.mark.parametrize(("write", "expected"), _CLI_CASES)
def test_a_past_limit_number_stops_schedule_fa(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    write: Callable[[Path], Path],
    expected: ParseError,
) -> None:
    """``schedule-fa`` exits 4 with the reader's located refusal alone, and
    the stale A3 and A2 files at its targets are gone. Main read each number:
    it filed the decimal-place cases and the whole-digit opening price (exit
    3), and refused the other whole-digit cases only later, at the CBDT
    14-digit bound on the rupee values."""
    statement = write(tmp_path)
    config = write_user_toml(tmp_path)
    assert _MARKER in statement.read_text(encoding="utf-8")
    inputs = sorted(tmp_path.iterdir())
    out = tmp_path / "schedule-fa.csv"
    out.write_text("stale", encoding="utf-8")
    (tmp_path / "schedule-fa-a2.csv").write_text("stale", encoding="utf-8")

    assert _run_schedule_fa(statement, config, out) == 4

    captured = capsys.readouterr()
    assert captured.err == f"error: {_text(expected)}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, _MARKER)
    assert sorted(tmp_path.iterdir()) == inputs
