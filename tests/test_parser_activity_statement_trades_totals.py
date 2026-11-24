"""Tests for :mod:`ibkr_fa.parser.activity_statement._trades_totals` (#1090).

The splitter checks every Trades block's ``SubTotal`` and ``Total`` rows
against its ``Data`` rows and refuses a block with rows but no totals.
These cases keep totals that expose missing or changed trade rows even
when the ending holdings still reconcile.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement._trades_totals import COLUMNS
from ibkr_fa.parser.activity_statement.multi_section import _SECTION_SHAPES
from tests._activity_statement_blocks import (
    _BLOCK,
    _XQAA_BUY,
    _XQAA_SELL,
    _XQAB_BUY,
    _records,
)
from tests._activity_statement_blocks import TRADES_HEADER_RECORD as _HEADER
from tests._activity_statement_blocks import csv_records_text as _text
from tests._activity_statement_blocks import edit_trades_block as _edit
from tests._activity_statement_blocks import trades_order_record as _order
from tests._change_in_nav_checks import RE_EXPORT_HINT as _RE_EXPORT
from tests._crash_reports import assert_unchained_refusal
from tests._malformed_csv import HOLDER, MARKER
from tests._number_limits import MONEY
from tests._trades_totals import with_trades_totals

_FIXTURES = Path(__file__).parent / "fixtures"


_AMOUNTS = {
    cell
    for row in (_XQAA_BUY, _XQAA_SELL, _XQAB_BUY)
    for cell in row[7:15]
    if len(cell.lstrip("-")) > 2
}


def _drop(*lines: int, text: str = _BLOCK) -> str:
    return _text(*(row for line, row in enumerate(_records(text), 1) if line not in lines))


def _split(tmp_path: Path, text: str) -> list[list[dict[str, str]]]:
    path = tmp_path / "statement.csv"
    path.write_text(text, encoding="utf-8")
    sections = split_activity_statement_sections(path)
    return [block.rows for block in sections[KnownActivitySection.TRADES]]


def _refusal(tmp_path: Path, text: str) -> ParseError:
    path = tmp_path / "statement.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(path)
    return caught.value


def _assert_refusal(
    error: ParseError, line: int, column: str | None, message: str, *markers: str
) -> None:
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        "Trades",
        line,
        column,
        message,
        "",
    )
    for marker in markers:
        assert marker not in str(error)


def test_the_test_block_has_its_totals_where_the_tests_expect() -> None:
    kinds = [(row[1], row[5]) for row in _records(_BLOCK)]
    assert kinds == [
        ("Header", "Symbol"),
        ("Data", "XQAA"),
        ("Data", "XQAA"),
        ("SubTotal", "XQAA"),
        ("Data", "XQAB"),
        ("SubTotal", "XQAB"),
        ("Total", ""),
    ]


def test_every_trades_column_is_checked_as_blank_key_or_sum() -> None:
    [shape] = _SECTION_SHAPES[KnownActivitySection.TRADES]
    assert set(COLUMNS) == shape.columns
    assert len(COLUMNS) == len(set(COLUMNS))


# ---- blocks that parse -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "symbols"),
    [
        pytest.param(_BLOCK, ["XQAA", "XQAA", "XQAB"], id="several-symbols"),
        pytest.param(
            with_trades_totals(_text(_HEADER, _XQAB_BUY)), ["XQAB"], id="one-symbol-one-row"
        ),
        pytest.param(
            with_trades_totals(_text(_HEADER, _XQAA_BUY, _XQAA_SELL)),
            ["XQAA", "XQAA"],
            id="a-buy-and-a-sell-in-one-run",
        ),
        pytest.param(
            _text(
                *(
                    [*row, "", ""] if row[1] in {"SubTotal", "Total"} else row
                    for row in _records(_BLOCK)
                )
            ),
            ["XQAA", "XQAA", "XQAB"],
            id="totals-with-empty-trailing-padding",
        ),
        pytest.param(_edit(7, "Code", " "), ["XQAA", "XQAA", "XQAB"], id="total-code-one-space"),
        pytest.param(_text(_HEADER), [], id="header-only"),
    ],
)
def test_a_checked_block_returns_only_its_order_rows(
    tmp_path: Path, text: str, symbols: list[str]
) -> None:
    """The totals are checked and never returned; MTM P/L cells are
    populated throughout, and a header-only block needs no totals."""
    [rows] = _split(tmp_path, text)
    assert [(row["DataDiscriminator"], row["Symbol"]) for row in rows] == [
        ("Order", symbol) for symbol in symbols
    ]


@pytest.mark.parametrize("line", [4, 7])
@pytest.mark.parametrize("delta", ["0.000001", "-0.000001"])
def test_basis_may_differ_by_the_owner_tolerance(tmp_path: Path, line: int, delta: str) -> None:
    """#1090 owner decision: a Basis total may differ from its exact sum by
    at most 0.000001. The Total is checked against the printed SubTotals."""
    printed = {4: "2830.01", 7: "3777.93"}[line]
    assert _records(_BLOCK)[line - 1][2 + COLUMNS.index("Basis")] == printed
    text = _edit(line, "Basis", str(Decimal(printed) + Decimal(delta)))
    if line == 4:
        # Keep the Total equal to the printed SubTotals.
        text = _edit(7, "Basis", str(Decimal("3777.93") + Decimal(delta)), text)
    assert len(_split(tmp_path, text)[0]) == 3


@pytest.mark.parametrize(
    ("line", "column", "value", "message"),
    [
        # 1E-20 past the tolerance: the smallest step a Basis within the money
        # limit's 20 places can print.
        pytest.param(
            4,
            "Basis",
            "2830.01000100000000000001",
            "the 'SubTotal' row on line 4 doesn't equal the sum of the rows on lines 2-3 in "
            f"its 'Basis' column. A row may be missing or changed. {_RE_EXPORT}",
            id="subtotal-basis-past-the-tolerance",
        ),
        pytest.param(
            7,
            "Basis",
            "3777.92999899999999999999",
            "the 'Total' row on line 7 doesn't equal the sum of the 'SubTotal' rows on lines "
            f"4, 6 in its 'Basis' column. A row may be missing or changed. {_RE_EXPORT}",
            id="total-basis-past-the-tolerance",
        ),
        pytest.param(
            4,
            "Proceeds",
            "-2728.929999",
            "the 'SubTotal' row on line 4 doesn't equal the sum of the rows on lines 2-3 in "
            f"its 'Proceeds' column. A row may be missing or changed. {_RE_EXPORT}",
            id="no-tolerance-outside-basis",
        ),
    ],
)
def test_a_total_past_the_tolerance_refuses(
    tmp_path: Path, line: int, column: str, value: str, message: str
) -> None:
    _assert_refusal(_refusal(tmp_path, _edit(line, column, value)), line, column, message)


@pytest.mark.parametrize(
    ("line", "value", "message"),
    [
        pytest.param(
            4,
            "2830.01000100000000000000000000000000001",
            "the 'SubTotal' row on line 4 doesn't equal the sum of the rows on lines 2-3 in "
            f"its 'Basis' column. A row may be missing or changed. {_RE_EXPORT}",
            id="subtotal",
        ),
        pytest.param(
            7,
            "3777.92999899999999999999999999999999999",
            "the 'Total' row on line 7 doesn't equal the sum of the 'SubTotal' rows on lines "
            f"4, 6 in its 'Basis' column. A row may be missing or changed. {_RE_EXPORT}",
            id="total",
        ),
    ],
)
def test_a_basis_difference_past_the_default_precision_stays_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: int, value: str, message: str
) -> None:
    """A Basis 1E-35 past the tolerance refuses: at Decimal's default 28
    digits its difference from the sum would round to the tolerance. A Basis
    within the money limit (20 places) never differs by that many digits near
    the tolerance, and the splitter refuses this one by its digit limit first
    (:func:`test_an_amount_past_its_digit_limit_refuses_before_the_sums`), so
    this stubs the limit out (#1276) to reach the exact subtraction."""
    monkeypatch.setattr(
        "ibkr_fa.parser.activity_statement._trades_totals.number_limit_breach",
        lambda _value, _kind: None,
        raising=False,
    )
    _assert_refusal(_refusal(tmp_path, _edit(line, "Basis", value)), line, "Basis", message)


def test_sums_past_the_default_precision_stay_exact(tmp_path: Path) -> None:
    """At Decimal's default 28 digits, 1E+14 + 1E-20 (35 digits, each amount
    within the money limit) rounds to 1E+14, so a lost row would go
    unnoticed; the check adds exactly instead."""
    text = with_trades_totals(
        _text(
            _HEADER,
            _order("XQAA", "1", "1", "1", "1E+14", "0", "1", "0", "0", "O"),
            _order("XQAA", "1", "1", "1", "0.00000000000000000001", "0", "1", "0", "0", "O"),
        )
    )
    assert (
        _records(text)[3][2 + COLUMNS.index("Proceeds")] == "100000000000000.00000000000000000001"
    )
    assert len(_split(tmp_path, text)[0]) == 2
    error = _refusal(tmp_path, _edit(4, "Proceeds", "1E+14", _edit(5, "Proceeds", "1E+14", text)))
    _assert_refusal(
        error,
        4,
        "Proceeds",
        "the 'SubTotal' row on line 4 doesn't equal the sum of the rows on lines 2-3 in its "
        f"'Proceeds' column. A row may be missing or changed. {_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    ("line", "label", "rows"),
    [(4, "'SubTotal'", "rows on lines 2-3"), (5, "'Total'", "'SubTotal' rows on line 4")],
)
def test_basis_comparison_past_the_default_precision_stays_exact(
    tmp_path: Path, line: int, label: str, rows: str
) -> None:
    """Rounding the large sum before subtracting would hide a Basis mismatch:
    at Decimal's default 28 digits 1E+14 + 0.00000100000000000001 rounds to
    1E+14 + 1E-6, within the tolerance of the printed 1E+14."""
    text = with_trades_totals(
        _text(
            _HEADER,
            _order("XQAA", "1", "1", "1", "0", "0", "1E+14", "0", "0", "O"),
            _order("XQAA", "1", "1", "1", "0", "0", "0.00000100000000000001", "0", "0", "O"),
        )
    )
    error = _refusal(tmp_path, _edit(line, "Basis", "1E+14", text))
    _assert_refusal(
        error,
        line,
        "Basis",
        f"the {label} row on line {line} doesn't equal the sum of the {rows} in its "
        f"'Basis' column. A row may be missing or changed. {_RE_EXPORT}",
    )


# ---- a lost or changed row -------------------------------------------------


_CLOSURES = _FIXTURES / "e2e_activity_statement_closures_2025.csv"
_CLOSURES_SOLO_LINES = (20, 21)
"""The closures fixture's SOLO buy and sell; its SubTotal follows on line 22."""


def test_a_removed_round_trip_refuses_at_its_orphan_subtotal(tmp_path: Path) -> None:
    """The issue's example: with SOLO's buy and sell removed, the ending
    holdings still reconcile, and the statement used to parse with three
    closed lots and a gain of 320.00 instead of 520.00. SOLO's SubTotal
    now has no rows above it."""
    text = _CLOSURES.read_text(encoding="utf-8")
    records = _records(text)
    assert [records[line - 1][5] for line in (*_CLOSURES_SOLO_LINES, 22)] == ["SOLO"] * 3
    assert records[21][1] == "SubTotal"
    error = _refusal(tmp_path, _drop(*_CLOSURES_SOLO_LINES, text=text))
    _assert_refusal(
        error,
        20,
        None,
        "the 'SubTotal' row on line 20 has no rows above it since the previous 'SubTotal' "
        f"row or the block's header. {_RE_EXPORT}",
    )


def test_a_removed_round_trip_and_subtotal_refuses_at_the_total(tmp_path: Path) -> None:
    """With SOLO's SubTotal removed too, the Total no longer adds up; its
    first mismatch is Proceeds."""
    text = _CLOSURES.read_text(encoding="utf-8")
    error = _refusal(tmp_path, _drop(*_CLOSURES_SOLO_LINES, 22, text=text))
    _assert_refusal(
        error,
        29,
        "Proceeds",
        "the 'Total' row on line 29 doesn't equal the sum of the 'SubTotal' rows on lines "
        f"23, 26, 28 in its 'Proceeds' column. A row may be missing or changed. {_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("Quantity", "38"),
        ("Proceeds", "-4188.29"),
        ("Comm/Fee", "-2.13"),
        ("Basis", "4189.42"),
        ("Realized P/L", "23.57"),
        ("MTM P/L", "42.44"),
    ],
)
def test_a_changed_order_row_refuses_at_its_subtotal(
    tmp_path: Path, column: str, value: str
) -> None:
    error = _refusal(tmp_path, _edit(2, column, value))
    _assert_refusal(
        error,
        4,
        column,
        "the 'SubTotal' row on line 4 doesn't equal the sum of the rows on lines 2-3 in its "
        f"{column!r} column. A row may be missing or changed. {_RE_EXPORT}",
        *(_AMOUNTS - {_XQAA_BUY[2 + COLUMNS.index(column)]}),
        value,
    )


@pytest.mark.parametrize("column", ["Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L"])
def test_a_changed_total_refuses(tmp_path: Path, column: str) -> None:
    error = _refusal(tmp_path, _edit(7, column, "1"))
    _assert_refusal(
        error,
        7,
        column,
        "the 'Total' row on line 7 doesn't equal the sum of the 'SubTotal' rows on lines 4, 6 "
        f"in its {column!r} column. A row may be missing or changed. {_RE_EXPORT}",
        *_AMOUNTS,
    )


def test_a_one_subtotal_total_names_its_line(tmp_path: Path) -> None:
    text = with_trades_totals(_text(_HEADER, _XQAB_BUY))
    error = _refusal(tmp_path, _edit(4, "MTM P/L", "1", text))
    _assert_refusal(
        error,
        4,
        "MTM P/L",
        "the 'Total' row on line 4 doesn't equal the sum of the 'SubTotal' rows on line 3 in "
        f"its 'MTM P/L' column. A row may be missing or changed. {_RE_EXPORT}",
    )


# ---- runs, orphans and the block's end ---------------------------------------


@pytest.mark.parametrize(
    ("dropped", "line", "rows"),
    [
        pytest.param((4,), 3, "lines 2-3", id="before-the-next-symbol"),
        pytest.param((6,), 5, "line 5", id="before-the-total"),
        pytest.param((6, 7), 5, "line 5", id="at-the-block-end"),
    ],
)
def test_a_run_without_a_subtotal_refuses(
    tmp_path: Path, dropped: tuple[int, ...], line: int, rows: str
) -> None:
    error = _refusal(tmp_path, _drop(*dropped))
    _assert_refusal(
        error, line, None, f"the rows on {rows} end without a 'SubTotal' row. {_RE_EXPORT}"
    )


def test_a_total_without_a_subtotal_refuses_even_at_zero(tmp_path: Path) -> None:
    """The block's only run was a fee-free, break-even buy and sell; with
    its Order rows and SubTotal removed, the Total's amounts are all zero."""
    text = with_trades_totals(
        _text(
            _HEADER,
            _order("XQAA", "5", "20", "20", "-100", "0", "100", "0", "0", "O"),
            _order("XQAA", "-5", "20", "20", "100", "0", "-100", "0", "0", "C"),
        )
    )
    [header, total] = _records(_drop(2, 3, 4, text=text))
    assert header == _HEADER
    assert [total[2 + COLUMNS.index(column)] for column in ("Proceeds", "Basis")] == ["0", "0"]
    error = _refusal(tmp_path, _drop(2, 3, 4, text=text))
    _assert_refusal(
        error,
        2,
        None,
        f"the 'Total' row on line 2 has no 'SubTotal' row above it in its block. {_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    ("text", "line", "header"),
    [
        pytest.param(_drop(7), 6, 1, id="one-block"),
        pytest.param(_BLOCK + _drop(7), 13, 8, id="second-block"),
    ],
)
def test_a_block_without_a_total_refuses(tmp_path: Path, text: str, line: int, header: int) -> None:
    """Each header-led block needs its own Total."""
    _assert_refusal(
        _refusal(tmp_path, text),
        line,
        None,
        f"the Trades block whose header is on line {header} ends without a 'Total' row. "
        f"{_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    "after",
    [
        pytest.param(_records(_BLOCK)[6], id="second-total"),
        pytest.param(_XQAB_BUY, id="order-row"),
        pytest.param(_records(_BLOCK)[5], id="subtotal-row"),
    ],
)
def test_a_row_after_the_total_refuses(tmp_path: Path, after: list[str]) -> None:
    error = _refusal(tmp_path, _BLOCK + _text(after))
    _assert_refusal(
        error,
        8,
        None,
        "line 8 comes after the Trades block's 'Total' row on line 7, which must be the "
        f"block's last row. {_RE_EXPORT}",
    )


# ---- symbol, asset category and currency -----------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [("Symbol", "XQAC"), ("Asset Category", "Equity and Index Options"), ("Currency", "EUR")],
)
def test_a_subtotal_naming_other_rows_refuses(tmp_path: Path, column: str, value: str) -> None:
    error = _refusal(tmp_path, _edit(4, column, value))
    _assert_refusal(
        error,
        4,
        column,
        "the 'SubTotal' row on line 4 names a different symbol, asset category or currency "
        f"from the rows above it. {_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    ("column", "value"), [("Asset Category", "Equity and Index Options"), ("Currency", "EUR")]
)
def test_a_total_naming_other_rows_refuses(tmp_path: Path, column: str, value: str) -> None:
    error = _refusal(tmp_path, _edit(7, column, value))
    _assert_refusal(
        error,
        7,
        column,
        "the 'Total' row on line 7 names a different asset category or currency from the "
        f"rows it covers. {_RE_EXPORT}",
    )


# ---- cell shapes -----------------------------------------------------------

_ROWS = pytest.mark.parametrize(
    ("line", "label"), [(4, "'SubTotal' row"), (7, "'Total' row")], ids=["subtotal", "total"]
)


@_ROWS
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("Code", "O"),
        ("Date/Time", "2025-03-03, 10:00:00"),
        ("T. Price", "113.17"),
        ("DataDiscriminator", "Order"),
        ("C. Price", "114.29"),
        ("Code", "  "),
    ],
)
def test_a_populated_blank_cell_refuses(
    tmp_path: Path, line: int, label: str, column: str, value: str
) -> None:
    """Only the Total's Code may hold one space (#1090 owner decision)."""
    error = _refusal(tmp_path, _edit(line, column, value))
    _assert_refusal(
        error,
        line,
        column,
        f"the {label} on line {line} has a value in its {column!r} column, which must be "
        f"empty. {_RE_EXPORT}",
        *_AMOUNTS,
    )


@pytest.mark.parametrize("column", ["Symbol", "Quantity"])
def test_a_total_with_a_symbol_or_quantity_refuses(tmp_path: Path, column: str) -> None:
    error = _refusal(tmp_path, _edit(7, column, "XQAA" if column == "Symbol" else "43"))
    _assert_refusal(
        error,
        7,
        column,
        f"the 'Total' row on line 7 has a value in its {column!r} column, which must be "
        f"empty. {_RE_EXPORT}",
    )


def test_a_subtotal_code_of_one_space_refuses(tmp_path: Path) -> None:
    error = _refusal(tmp_path, _edit(4, "Code", " "))
    _assert_refusal(
        error,
        4,
        "Code",
        "the 'SubTotal' row on line 4 has a value in its 'Code' column, which must be "
        f"empty. {_RE_EXPORT}",
    )


@_ROWS
@pytest.mark.parametrize("value", ["", "NaN", "sNaN", "Infinity", "-Infinity", "1.2.3", "1,0"])
def test_an_amount_that_isnt_a_finite_decimal_refuses(
    tmp_path: Path, line: int, label: str, value: str
) -> None:
    error = _refusal(tmp_path, _edit(line, "Basis", value))
    _assert_refusal(
        error,
        line,
        "Basis",
        f"the {label} on line {line} needs a finite decimal in its 'Basis' column.",
    )


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "abc"])
def test_a_data_row_amount_that_isnt_a_finite_decimal_refuses(tmp_path: Path, value: str) -> None:
    """The splitter adds every summed cell before the Trades parser reads the row."""
    error = _refusal(tmp_path, _edit(3, "MTM P/L", value))
    _assert_refusal(
        error,
        3,
        "MTM P/L",
        "the data row on line 3 needs a finite decimal in its 'MTM P/L' column.",
    )


def test_a_header_without_a_summed_column_refuses_at_the_subtotal(tmp_path: Path) -> None:
    keep = [index for index, column in enumerate(_HEADER) if column != "MTM P/L"]
    records = [[row[index] for index in keep] for row in _records(_BLOCK)]
    error = _refusal(tmp_path, _text(*records))
    _assert_refusal(
        error, 4, "MTM P/L", "the 'SubTotal' row on line 4 is missing its 'MTM P/L' cell."
    )


def test_a_sum_past_the_exponent_range_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 9E+999999 rows can't be added exactly. Since #1276 the splitter
    refuses an amount that size by its digit limit first
    (:func:`test_an_amount_past_its_digit_limit_refuses_before_the_sums`), so
    this stubs the limit out to reach the sum."""
    monkeypatch.setattr(
        "ibkr_fa.parser.activity_statement._trades_totals.number_limit_breach",
        lambda _value, _kind: None,
        raising=False,
    )
    text = _edit(2, "Proceeds", "9E+999999", _edit(3, "Proceeds", "9E+999999"))
    _assert_refusal(
        _refusal(tmp_path, text),
        3,
        "Proceeds",
        "the amounts up to line 3 are too large or too small to add up exactly, so the "
        f"printed total can't be checked. {_RE_EXPORT}",
    )


@pytest.mark.parametrize(
    ("edits", "line", "column"),
    [
        pytest.param(
            ((2, "Proceeds", "7242" + "0" * 27),), 2, "Proceeds", id="data-row-31-whole-digits"
        ),
        pytest.param(((2, "Basis", "7242" + "0" * 27),), 2, "Basis", id="basis-31-whole-digits"),
        pytest.param(
            ((2, "Proceeds", "7242E+999999"), (3, "Proceeds", "7242E+999999")),
            2,
            "Proceeds",
            id="past-the-exponent-range",
        ),
        pytest.param(
            ((4, "Basis", "2830.010001" + "0" * 25 + "7242"),),
            4,
            "Basis",
            id="subtotal-35-places",
        ),
        pytest.param(
            ((7, "Basis", "3777.929998" + "9" * 25 + "7242"),), 7, "Basis", id="total-35-places"
        ),
    ],
)
def test_an_amount_past_its_digit_limit_refuses_before_the_sums(
    tmp_path: Path, edits: tuple[tuple[int, str, str], ...], line: int, column: str
) -> None:
    """#1276: the amounts the exact-sum and tolerance tests above once read
    refuse when their row is read, with the money limit and no value, before
    any sum or comparison."""
    text = _BLOCK
    for edit_line, edit_column, value in edits:
        text = _edit(edit_line, edit_column, value, text)
    error = _refusal(tmp_path, text)
    assert_unchained_refusal(
        error,
        ParseError(section="Trades", row_index=line, column=column, message=MONEY, raw_value=""),
        f"ParseError in section 'Trades' at row {line}, column {column!r}: {MONEY}",
        markers=("7242",),
    )


# ---- width ----------------------------------------------------------------


@_ROWS
def test_a_short_total_refuses_without_its_last_cell(tmp_path: Path, line: int, label: str) -> None:
    records = _records(_BLOCK)
    records[line - 1] = [*records[line - 1][:-2], MARKER]
    error = _refusal(tmp_path, _text(*records))
    _assert_refusal(
        error,
        line,
        None,
        f"{label} has 13 value cell(s); the section header declared 14 column(s).",
        MARKER,
    )


@_ROWS
def test_a_total_with_a_populated_cell_past_the_header_refuses(
    tmp_path: Path, line: int, label: str
) -> None:
    records = _records(_BLOCK)
    records[line - 1] = [*records[line - 1], HOLDER]
    error = _refusal(tmp_path, _text(*records))
    _assert_refusal(
        error,
        line,
        None,
        f"{label} has 15 value cell(s) with non-empty content past the 14 column(s) the "
        "header declared; refusing to drop populated overflow cells. Either the file is "
        "malformed or IBKR has added columns to this section's schema.",
        HOLDER,
    )


# ---- privacy ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        pytest.param(
            "Code",
            HOLDER,
            "the 'SubTotal' row on line 4 has a value in its 'Code' column, which must be "
            f"empty. {_RE_EXPORT}",
            id="name-in-a-blank-cell",
        ),
        pytest.param(
            "Symbol",
            MARKER,
            "the 'SubTotal' row on line 4 names a different symbol, asset category or "
            f"currency from the rows above it. {_RE_EXPORT}",
            id="marker-in-the-symbol",
        ),
    ],
)
def test_a_refusal_prints_no_cell_or_amount(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    error = _refusal(tmp_path, _edit(4, column, value))
    _assert_refusal(error, 4, column, message, value, *_AMOUNTS)
