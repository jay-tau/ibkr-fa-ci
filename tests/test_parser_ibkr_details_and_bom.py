"""IBKR details-on statements and garbled byte-order marks refuse with a clear message (#1195).

Parser-level regressions: the Activity Statement splitter, the direct Trades
parser, the Flex Query splitter and the format detector. Each new refusal is
checked field by field and for the input's :data:`tests._ibkr_details.MARKERS`; near
matches and inputs the new checks don't apply to keep today's exact result.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import (
    FinancialInstrument,
    KnownActivitySection,
    build_instrument_lookup,
    parse_activity_statement_trades,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.multi_section import FILE_AN_ISSUE
from ibkr_fa.parser.format_detect import IbkrCsvFormat, detect_ibkr_csv_format
from ibkr_fa.parser.multi_section import split_sections
from ibkr_fa.types import Ticker
from tests._change_in_nav_checks import RE_EXPORT_HINT
from tests._ibkr_details import (
    ACCOUNT_INFORMATION,
    DETAIL_KINDS,
    DETAILS_OPEN_POSITIONS,
    FLEX_QUERY,
    GARBLED_BOM,
    GARBLED_TEXT,
    HOLDER,
    IDENT,
    MARKERS,
    NUMBER,
    OPEN_POSITIONS_COLUMNS,
    ORDER_CELLS,
    REAL_BOM,
    STATEMENT,
    SYMBOL,
    TRADES_COLUMNS,
    TRADES_HEADER,
    activity_statement,
    assert_garbled_bom_refusal,
    assert_open_positions_refusal,
    assert_trades_detail_refusal,
    detail_cells,
    standard_statement,
    trades_row,
)
from tests._ibkr_format_refusals import no_sentinel_refusal as _no_sentinel
from tests._malformed_csv import malformed_message
from tests._statement_period import ALL_DATES
from tests._trades_totals import with_trades_totals

_UNEXPECTED = f"Refusing rather than dropping it. {FILE_AN_ISSUE}"
_UNKNOWN_KIND = (
    "unexpected DataDiscriminator; v1 only handles 'Order' rows. The splitter checks the "
    "'SubTotal' and 'Total' rows and never passes them on."
)


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    return tmp_path / "statement.csv"


def _split_error(path: Path, text: str) -> ParseError:
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(path)
    return caught.value


def _assert_existing(
    error: ParseError, *, section: str, row_index: int, column: str | None, message: str
) -> None:
    """``error`` is today's exact refusal, not one of #1195's."""
    assert type(error) is ParseError
    assert (error.section, error.row_index, error.column, error.message, error.raw_value) == (
        section,
        row_index,
        column,
        message,
        "",
    )


_Expected = tuple[str, int, str | None, str]


def _assert_is(error: ParseError, expected: _Expected) -> None:
    """``error`` is today's refusal ``(section, row_index, column, message)``."""
    section, row_index, column, message = expected
    _assert_existing(error, section=section, row_index=row_index, column=column, message=message)


# ---- Open Positions header ------------------------------------------------


@pytest.mark.parametrize(
    ("before", "header_line"),
    [
        pytest.param("", 3, id="after-statement"),
        pytest.param(
            with_trades_totals(TRADES_HEADER + trades_row(ORDER_CELLS)), 7, id="after-trades"
        ),
    ],
)
def test_open_positions_open_column_refuses_before_its_shape(
    csv_path: Path, before: str, header_line: int
) -> None:
    """A header with the ``Open`` column, a Summary row and two Lot rows refuses
    at the header's line, before the declared-shape check would."""
    text = STATEMENT + before + DETAILS_OPEN_POSITIONS + ACCOUNT_INFORMATION
    assert_open_positions_refusal(_split_error(csv_path, text), header_line, source=text)


def test_open_positions_open_column_refuses_before_an_undeclared_column(csv_path: Path) -> None:
    """``Open`` wins over later columns no shape declares, and the refusal doesn't
    name the undeclared columns (their names are markers)."""
    header = "Open Positions,Header," + ",".join((*OPEN_POSITIONS_COLUMNS, *MARKERS)) + "\n"
    text = STATEMENT + header + ACCOUNT_INFORMATION
    assert_open_positions_refusal(_split_error(csv_path, text), 3, source=text)


def _open_positions_header(*columns: str) -> str:
    return "Open Positions,Header," + ",".join(columns) + "\n"


_WITH_OPEN = list(OPEN_POSITIONS_COLUMNS)


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        pytest.param(
            [*_WITH_OPEN[:4], "", *_WITH_OPEN[4:]],
            "section header's column name 5 is empty or only whitespace (the first name after "
            "'Header' is 1), so the values under it belong to no column. The file is malformed; "
            "re-export the Activity Statement.",
            id="interior-empty-name",
        ),
        pytest.param(
            [*_WITH_OPEN[:4], " ", *_WITH_OPEN[4:]],
            "section header's column name 5 is empty or only whitespace (the first name after "
            "'Header' is 1), so the values under it belong to no column. The file is malformed; "
            "re-export the Activity Statement.",
            id="whitespace-name",
        ),
        pytest.param(
            [*_WITH_OPEN, "Symbol"],
            "section header's column name 14 repeats column name 4 (the first name after "
            "'Header' is 1); refusing because dict(zip(...)) on data rows would silently "
            "overwrite the earlier value. Either the file is malformed or IBKR has changed this "
            "section's schema.",
            id="duplicate-symbol",
        ),
        pytest.param(
            [*_WITH_OPEN, "Open"],
            "section header's column name 14 repeats column name 5 (the first name after "
            "'Header' is 1); refusing because dict(zip(...)) on data rows would silently "
            "overwrite the earlier value. Either the file is malformed or IBKR has changed this "
            "section's schema.",
            id="duplicate-open",
        ),
    ],
)
def test_open_positions_structural_errors_win_over_open(
    csv_path: Path, columns: list[str], message: str
) -> None:
    """An interior blank or whitespace name, or a repeated name, keeps today's refusal."""
    text = STATEMENT + _open_positions_header(*columns) + ACCOUNT_INFORMATION
    _assert_existing(
        _split_error(csv_path, text),
        section="Open Positions",
        row_index=3,
        column=None,
        message=message,
    )


@pytest.mark.parametrize("name", ["open", "Open ", " Open", "OpeN"])
def test_open_positions_near_match_keeps_the_shape_refusal(csv_path: Path, name: str) -> None:
    """The name must be exactly ``Open``: no trimming or case folding."""
    columns = [*_WITH_OPEN[:4], name, *_WITH_OPEN[5:]]
    text = STATEMENT + _open_positions_header(*columns) + ACCOUNT_INFORMATION
    _assert_existing(
        _split_error(csv_path, text),
        section="Open Positions",
        row_index=3,
        column=None,
        message=(
            "the header on line 3 has a column name 5 (the first name after 'Header' is 1) "
            "that this block shape neither reads nor lists as safe to ignore. The header must "
            "fit one declared block shape, so its values could change a filed figure unseen. "
            f"{_UNEXPECTED}"
        ),
    )


# ---- Trades rows, through the splitter -------------------------------------


@pytest.mark.parametrize(
    "open_positions", [False, True], ids=["no-open-positions", "then-open-positions"]
)
@pytest.mark.parametrize("totals", ["none", "order", "all"])
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_trades_detail_row_refuses_before_totals(
    csv_path: Path, kind: str, totals: str, open_positions: bool
) -> None:
    """Order then a detail row on line 5 refuses there, whatever the totals say:
    none, the Order row's alone, or both rows'. ``ClosedLot`` rows carry blank
    amounts, which the totals check would refuse first. A details-on Open
    Positions header after Trades comes later in the file."""
    text = activity_statement(
        kind, totals=totals, open_positions=open_positions, blank_amounts=kind != "Trade"
    )
    assert_trades_detail_refusal(_split_error(csv_path, text), 5, source=text)


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_trades_detail_row_after_the_total_row_refuses_as_a_detail(
    csv_path: Path, kind: str
) -> None:
    """The detail check doesn't wait for the block to end, so a detail row after
    the block's Total row (line 7) gets the detail refusal."""
    order_block = with_trades_totals(TRADES_HEADER + trades_row(ORDER_CELLS))
    detail = trades_row(detail_cells(kind, blank_amounts=True))
    text = STATEMENT + order_block + detail + ACCOUNT_INFORMATION
    assert_trades_detail_refusal(_split_error(csv_path, text), 7, source=text)


def _trades_without(column: str, rows: list[dict[str, str]], *, totals: bool) -> str:
    columns = [name for name in TRADES_COLUMNS if name != column]
    block = "Trades,Header," + ",".join(columns) + "\n"
    for cells in rows:
        block += (
            "Trades,Data," + ",".join(_csv_cell(cells.get(name, "")) for name in columns) + "\n"
        )
    return STATEMENT + (with_trades_totals(block) if totals else block) + ACCOUNT_INFORMATION


def _csv_cell(value: str) -> str:
    return f'"{value}"' if "," in value else value


@pytest.mark.parametrize("totals", [False, True], ids=["no-totals", "totals"])
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_trades_detail_row_refuses_in_a_declared_subset_without_basis(
    csv_path: Path, kind: str, totals: bool
) -> None:
    """A header that declares ``DataDiscriminator`` but not ``Basis`` is a shape
    the splitter accepts; a width-valid detail row refuses before the block's
    totals check."""
    rows = [ORDER_CELLS, detail_cells(kind, blank_amounts=False)]
    text = _trades_without("Basis", rows, totals=totals)
    assert_trades_detail_refusal(_split_error(csv_path, text), 5, source=text)


@pytest.mark.parametrize(
    ("totals", "row_index", "column", "message"),
    [
        pytest.param(
            False,
            5,
            None,
            f"the rows on lines 4-5 end without a 'SubTotal' row. {RE_EXPORT_HINT}",
            id="no-totals",
        ),
        pytest.param(
            True,
            6,
            "Asset Category",
            "the 'SubTotal' row on line 6 names a different symbol, asset category or "
            f"currency from the rows above it. {RE_EXPORT_HINT}",
            id="totals",
        ),
    ],
)
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_trades_without_data_discriminator_keeps_todays_refusal(
    csv_path: Path, kind: str, totals: bool, row_index: int, column: str | None, message: str
) -> None:
    """Without a ``DataDiscriminator`` column no row kind is inferred, even when a
    detail kind's text sits in the row's first cell."""
    rows = [ORDER_CELLS, {**ORDER_CELLS, "Asset Category": kind}]
    text = _trades_without("DataDiscriminator", rows, totals=totals)
    _assert_existing(
        _split_error(csv_path, text),
        section="Trades",
        row_index=row_index,
        column=column,
        message=message,
    )


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_short_detail_row_keeps_the_width_refusal(csv_path: Path, kind: str) -> None:
    detail = trades_row(detail_cells(kind, blank_amounts=True)).removesuffix(f",{IDENT}\n") + "\n"
    text = STATEMENT + TRADES_HEADER + trades_row(ORDER_CELLS) + detail + ACCOUNT_INFORMATION
    _assert_existing(
        _split_error(csv_path, text),
        section="Trades",
        row_index=5,
        column=None,
        message="data row has 13 value cell(s); the section header declared 14 column(s).",
    )


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_detail_row_with_populated_overflow_keeps_the_overflow_refusal(
    csv_path: Path, kind: str
) -> None:
    detail = trades_row(detail_cells(kind, blank_amounts=True), extra=("", HOLDER))
    text = STATEMENT + TRADES_HEADER + trades_row(ORDER_CELLS) + detail + ACCOUNT_INFORMATION
    error = _split_error(csv_path, text)
    _assert_existing(
        error,
        section="Trades",
        row_index=5,
        column=None,
        message=(
            "data row has 16 value cell(s) with non-empty content past the 14 column(s) the "
            "header declared; refusing to drop populated overflow cells. Either the file is "
            "malformed or IBKR has added columns to this section's schema."
        ),
    )
    assert HOLDER not in str(error)


@pytest.mark.parametrize(
    "kind", ["trade", "Trade ", " Trade", "ClosedLot**", "closedlot", "SynthKind"]
)
def test_near_match_kind_passes_the_splitter_and_refuses_as_unknown(
    csv_path: Path, kind: str
) -> None:
    """A kind that isn't exactly a detail kind is kept as written, and with valid
    totals reaches the Trades parser's unknown-kind refusal."""
    csv_path.write_text(activity_statement(kind, totals="all"), encoding="utf-8")
    [block] = split_activity_statement_sections(csv_path)[KnownActivitySection.TRADES]
    assert [row["DataDiscriminator"] for row in block.rows] == ["Order", kind]
    with pytest.raises(ParseError) as caught:
        parse_activity_statement_trades(block.rows, instruments=_instruments(), period=ALL_DATES)
    _assert_existing(
        caught.value,
        section="Trades",
        row_index=1,
        column="DataDiscriminator",
        message=_UNKNOWN_KIND,
    )


# ---- Precedence in the splitter ---------------------------------------------


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_detail_before_its_header_keeps_the_header_seen_refusal(csv_path: Path, kind: str) -> None:
    text = STATEMENT + trades_row(detail_cells(kind, blank_amounts=True))
    _assert_existing(
        _split_error(csv_path, text),
        section="Trades",
        row_index=3,
        column=None,
        message=(
            "row appeared before any 'Header' row was seen for section 'Trades'; "
            "the Activity Statement format requires the Header row to precede Data rows "
            "in each section."
        ),
    )


@pytest.mark.parametrize("kind", ["Open", *DETAIL_KINDS])
def test_details_report_physical_lines_after_multiline_records(csv_path: Path, kind: str) -> None:
    prefix = STATEMENT + f'Statement,Data,Title,"{HOLDER}\n{IDENT}"\n\n'
    if kind == "Open":
        text = prefix + DETAILS_OPEN_POSITIONS
        assert_open_positions_refusal(_split_error(csv_path, text), 6, source=text)
    else:
        cells = {**ORDER_CELLS, "DataDiscriminator": kind, "Symbol": f"{SYMBOL},\n{IDENT}"}
        text = prefix + TRADES_HEADER + trades_row(ORDER_CELLS) + trades_row(cells)
        assert_trades_detail_refusal(_split_error(csv_path, text), 8, source=text)


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_an_earlier_unknown_section_wins_over_a_later_detail(csv_path: Path, kind: str) -> None:
    text = (
        STATEMENT
        + f"{IDENT},Header,Field Name\n"
        + activity_statement(kind, totals="none")[len(STATEMENT) :]
    )
    _assert_existing(
        _split_error(csv_path, text),
        section="<file>",
        row_index=3,
        column=None,
        message=(
            "line 3 belongs to a section this tool neither parses nor lists as safe to "
            "ignore, so it can't tell whether the section changes a filed figure. "
            f"{_UNEXPECTED}"
        ),
    )


def test_an_earlier_trades_block_without_totals_wins_over_a_later_open_column(
    csv_path: Path,
) -> None:
    """The Trades block's totals are checked when the Open Positions header ends
    it, before that header is read."""
    text = STATEMENT + TRADES_HEADER + trades_row(ORDER_CELLS) + DETAILS_OPEN_POSITIONS
    _assert_existing(
        _split_error(csv_path, text),
        section="Trades",
        row_index=4,
        column=None,
        message=f"the rows on line 4 end without a 'SubTotal' row. {RE_EXPORT_HINT}",
    )


def test_open_column_before_trades_details_refuses_at_the_header(csv_path: Path) -> None:
    """With Open Positions first, its header refuses before any Trades detail row."""
    trades = activity_statement("Trade", totals="none")[len(STATEMENT) :]
    text = STATEMENT + DETAILS_OPEN_POSITIONS + trades
    assert_open_positions_refusal(_split_error(csv_path, text), 3, source=text)


# ---- Trades rows, through the parser directly --------------------------------


def _instruments() -> dict[Ticker, FinancialInstrument]:
    return build_instrument_lookup(
        [
            {
                "Asset Category": "Stocks",
                "Symbol": SYMBOL,
                "Description": "SAMPLE ISSUER DT INC",
                "Conid": "1",
                "Security ID": "US99990E1038",
                "Underlying": SYMBOL,
                "Listing Exch": "NASDAQ",
                "Multiplier": "1",
                "Type": "COMMON",
                "Code": "",
            }
        ]
    )


def _parse_error(rows: list[dict[str, str]]) -> ParseError:
    with pytest.raises(ParseError) as caught:
        parse_activity_statement_trades(rows, instruments=_instruments(), period=ALL_DATES)
    return caught.value


def test_order_row_parses() -> None:
    """The valid Order row the tests below put first parses on its own."""
    [event] = parse_activity_statement_trades(
        [dict(ORDER_CELLS)], instruments=_instruments(), period=ALL_DATES
    )
    assert str(event.quantity) == NUMBER


@pytest.mark.parametrize("blank_amounts", [False, True], ids=["amounts", "blank-amounts"])
@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_direct_detail_row_refuses_at_its_index(kind: str, blank_amounts: bool) -> None:
    detail = detail_cells(kind, blank_amounts=blank_amounts)
    detail = {column: detail.get(column, "") for column in TRADES_COLUMNS}
    for rows, index in (([detail], 0), ([dict(ORDER_CELLS), detail], 1)):
        assert_trades_detail_refusal(_parse_error(rows), index, source=repr(rows))


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_detail_refusals_do_not_echo_shifted_cells(csv_path: Path, kind: str) -> None:
    cells = {
        **ORDER_CELLS,
        **dict(zip(TRADES_COLUMNS[1:8], MARKERS, strict=True)),
        "DataDiscriminator": kind,
    }
    text = STATEMENT + TRADES_HEADER + trades_row(ORDER_CELLS) + trades_row(cells)
    assert_trades_detail_refusal(_split_error(csv_path, text), 5, source=text)
    for rows, index in (([cells], 0), ([dict(ORDER_CELLS), cells], 1)):
        assert_trades_detail_refusal(_parse_error(rows), index, source=repr(rows))


@pytest.mark.parametrize("kind", DETAIL_KINDS)
def test_direct_detail_row_without_basis_keeps_the_missing_key_refusal(kind: str) -> None:
    """The parser's required-key check comes before the detail check; the
    splitter's order differs (see the declared-subset test above)."""
    detail = {k: v for k, v in detail_cells(kind, blank_amounts=False).items() if k != "Basis"}
    for rows, index in (([detail], 0), ([dict(ORDER_CELLS), detail], 1)):
        _assert_existing(
            _parse_error(rows),
            section="Trades",
            row_index=index,
            column="Basis",
            message=(
                "required column(s) ['Basis'] missing from the Trades row. IBKR may have "
                "changed the section's schema, or the export was generated with a custom "
                "column subset. Re-export with the standard column set."
            ),
        )


def test_direct_row_without_data_discriminator_keeps_the_missing_key_refusal() -> None:
    row = {k: v for k, v in ORDER_CELLS.items() if k != "DataDiscriminator"}
    _assert_existing(
        _parse_error([{**row, "Asset Category": "Trade"}]),
        section="Trades",
        row_index=0,
        column="DataDiscriminator",
        message=(
            "required column(s) ['DataDiscriminator'] missing from the Trades row. IBKR may "
            "have changed the section's schema, or the export was generated with a custom "
            "column subset. Re-export with the standard column set."
        ),
    )


@pytest.mark.parametrize("kind", ["trade", "Trade ", "ClosedLot**", "SynthKind", ""])
def test_direct_near_match_kind_keeps_the_unknown_kind_refusal(kind: str) -> None:
    row = {**ORDER_CELLS, "DataDiscriminator": kind}
    for rows, index in (([row], 0), ([dict(ORDER_CELLS), row], 1)):
        _assert_existing(
            _parse_error(rows),
            section="Trades",
            row_index=index,
            column="DataDiscriminator",
            message=_UNKNOWN_KIND,
        )


# ---- Garbled byte-order mark --------------------------------------------------

_ACTIVITY = (STATEMENT + ACCOUNT_INFORMATION).encode()
_FLEX = FLEX_QUERY.encode()
_FLEX_UNQUOTED = FLEX_QUERY.replace('"', "").encode()
# Each format's file, with its first cell quoted, and with its first record
# spanning two lines (a quoted cell holds a line break).
_GARBLED_INPUTS = {
    "activity": _ACTIVITY,
    "activity-quoted": b'"Statement"' + _ACTIVITY.removeprefix(b"Statement"),
    "activity-multiline": _ACTIVITY.replace(
        b"Field Value\n", f'Field Value,"{HOLDER}\n{IDENT}"\n'.encode(), 1
    ),
    "activity-marker-first": f"{HOLDER},{','.join(MARKERS)}\n".encode() + _ACTIVITY,
    "flex": _FLEX,
    "flex-unquoted": _FLEX_UNQUOTED,
    "flex-multiline": _FLEX.replace(b'"IBEntity"', f'"IBEntity","{HOLDER}\n{IDENT}"'.encode(), 1),
    "flex-marker-first": f"{HOLDER},{','.join(MARKERS)}\n".encode() + _FLEX,
    "activity-details": activity_statement(
        "ClosedLot", totals="none", open_positions=True
    ).encode(),
    "activity-open-positions": (STATEMENT + DETAILS_OPEN_POSITIONS).encode(),
}


def _garbled(data: bytes) -> bytes:
    """Put the prefix inside the first cell, including a quoted one."""
    if data.startswith(b'"'):
        return b'"' + GARBLED_BOM + data[1:]
    return GARBLED_BOM + data


@pytest.mark.parametrize("name", list(_GARBLED_INPUTS))
def test_garbled_bom_refuses_in_the_detector(csv_path: Path, name: str) -> None:
    """The detector refuses at row 0, before its format sentinels, whichever
    format follows and whatever later details the file has."""
    data = _garbled(_GARBLED_INPUTS[name])
    csv_path.write_bytes(data)
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(csv_path)
    assert_garbled_bom_refusal(caught.value, 0, source=data.decode())


@pytest.mark.parametrize("name", [name for name in _GARBLED_INPUTS if name.startswith("activity")])
def test_garbled_bom_refuses_in_the_activity_splitter(csv_path: Path, name: str) -> None:
    """The Activity Statement splitter refuses at row 1, its first line, before
    section classification and before any later detail refusal."""
    data = _garbled(_GARBLED_INPUTS[name])
    csv_path.write_bytes(data)
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(csv_path)
    assert_garbled_bom_refusal(caught.value, 1, source=data.decode())


@pytest.mark.parametrize("name", [name for name in _GARBLED_INPUTS if name.startswith("flex")])
def test_garbled_bom_refuses_in_the_flex_splitter(csv_path: Path, name: str) -> None:
    """The Flex Query splitter refuses at row 0, before header classification."""
    data = _garbled(_GARBLED_INPUTS[name])
    csv_path.write_bytes(data)
    with pytest.raises(ParseError) as caught:
        split_sections(csv_path)
    assert_garbled_bom_refusal(caught.value, 0, source=data.decode())


@pytest.mark.parametrize(
    "name", [name for name, data in _GARBLED_INPUTS.items() if data.startswith(b'"')]
)
def test_garbled_bom_before_an_opening_quote_refuses(csv_path: Path, name: str) -> None:
    """A re-encoded file whose first cell is quoted has the mark before the quote.
    The reader then keeps the quote in an unquoted first cell, which still starts
    with the mark, so the detector and the format's splitter refuse."""
    data = GARBLED_BOM + _GARBLED_INPUTS[name]
    csv_path.write_bytes(data)
    split, row = (
        (split_activity_statement_sections, 1)
        if name.startswith("activity")
        else (split_sections, 0)
    )
    for reader, row_index in ((detect_ibkr_csv_format, 0), (split, row)):
        with pytest.raises(ParseError) as caught:
            reader(csv_path)
        assert_garbled_bom_refusal(caught.value, row_index, source=data.decode())


def test_garbled_bom_after_a_real_bom_refuses(csv_path: Path) -> None:
    """A real mark is stripped as before; the garbled one after it is the file's start."""
    data = REAL_BOM + GARBLED_BOM + _ACTIVITY
    csv_path.write_bytes(data)
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(csv_path)
    assert_garbled_bom_refusal(caught.value, 0, source=data.decode())
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(csv_path)
    assert_garbled_bom_refusal(caught.value, 1, source=data.decode())


@pytest.mark.parametrize("tail", ["", '"x'], ids=["unclosed-quote", "text-after-quote"])
@pytest.mark.parametrize(
    ("reader", "row_index", "reexport"),
    [
        (split_activity_statement_sections, 1, "re-export the Activity Statement."),
        (split_sections, 0, "re-export the Flex Query."),
        (detect_ibkr_csv_format, 0, "re-export it from IBKR."),
    ],
    ids=["activity", "flex", "detector"],
)
def test_malformed_first_record_wins_over_garbled_bom(
    csv_path: Path, reader: Callable[[Path], object], row_index: int, reexport: str, tail: str
) -> None:
    csv_path.write_text(f'"{GARBLED_TEXT}Statement",Header,"{HOLDER}{tail}', encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        reader(csv_path)
    _assert_existing(
        caught.value,
        section="<file>",
        row_index=row_index,
        column=None,
        message=malformed_message(1, reexport),
    )


@pytest.mark.parametrize("bom", [b"", REAL_BOM], ids=["no-bom", "real-bom"])
def test_real_and_absent_bom_keep_todays_results(tmp_path: Path, bom: bytes) -> None:
    """A real mark or none gives the same result as the plain file through each
    format's splitter and the detector."""
    plain, marked = tmp_path / "plain.csv", tmp_path / "marked.csv"
    plain.write_text(standard_statement(), encoding="utf-8")
    marked.write_bytes(bom + standard_statement().encode())
    assert split_activity_statement_sections(marked) == split_activity_statement_sections(plain)
    assert detect_ibkr_csv_format(marked) is IbkrCsvFormat.ACTIVITY_STATEMENT
    plain.write_bytes(_FLEX)
    marked.write_bytes(bom + _FLEX)
    assert split_sections(marked) == split_sections(plain)
    assert detect_ibkr_csv_format(marked) is IbkrCsvFormat.FLEX_QUERY


def _unknown_section(line: int) -> _Expected:
    return (
        "<file>",
        line,
        None,
        f"line {line} belongs to a section this tool neither parses nor lists as safe to "
        f"ignore, so it can't tell whether the section changes a filed figure. {_UNEXPECTED}",
    )


def _no_flex_header(row: int) -> _Expected:
    return (
        "<file>",
        row,
        None,
        "first non-blank row is not a known section header (expected first column "
        "'CurrencyPrimary' or a row matching a known section signature)",
    )


def _boundary(body: bytes, where: str) -> bytes:
    """``body`` with the garbled mark, or a partial or lookalike one, placed
    where the special refusal must not fire."""
    first, _, rest = body.partition(b"\n")
    return {
        "after-blank-line": b"\n" + GARBLED_BOM + body,
        "after-blank-lines": b"\n  \n,,,\n" + GARBLED_BOM + body,
        "later-record": first + b"\n" + GARBLED_BOM + rest,
        "later-cell": first.replace(b",", b"," + GARBLED_BOM, 1) + b"\n" + rest,
        "midway-first-cell": first[:3] + GARBLED_BOM + first[3:] + b"\n" + rest,
        "partial-1": b"\xc3\xaf" + body,
        "partial-2": b"\xc3\xaf\xc2\xbb" + body,
        "lookalike-x": b"\xc3\xaf\xc2\xbbX" + body,
        "lookalike-quote": b"\xc3\xaf\xe2\x80\xba\xc2\xbf" + body,
    }[where]


_FIRST_LINE_CASES = (
    "midway-first-cell",
    "partial-1",
    "partial-2",
    "lookalike-x",
    "lookalike-quote",
)
_BOUNDARY_CASES = (
    "after-blank-line",
    "after-blank-lines",
    "later-record",
    "later-cell",
    *_FIRST_LINE_CASES,
)


@pytest.mark.parametrize("where", _BOUNDARY_CASES)
def test_activity_bom_boundary_keeps_todays_results(csv_path: Path, where: str) -> None:
    csv_path.write_bytes(_boundary(_ACTIVITY, where))
    split_expected = {
        "after-blank-line": _unknown_section(2),
        "after-blank-lines": _unknown_section(4),
        "later-record": _unknown_section(2),
        "later-cell": (
            "Statement",
            1,
            None,
            "line 1 has a row type other than 'Header', 'Data', 'SubTotal' or 'Total', the "
            "only ones this section's rows may have, so the row could hold a figure no parser "
            f"reads. {_UNEXPECTED}",
        ),
    }.get(where, _unknown_section(1))
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(csv_path)
    _assert_is(caught.value, split_expected)
    if where == "later-record":
        assert detect_ibkr_csv_format(csv_path) is IbkrCsvFormat.ACTIVITY_STATEMENT
        return
    detect_row = {"after-blank-line": 1, "after-blank-lines": 3}.get(where, 0)
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(csv_path)
    _assert_is(caught.value, _no_sentinel(detect_row, csv_path))


@pytest.mark.parametrize("where", _BOUNDARY_CASES)
def test_flex_bom_boundary_keeps_todays_results(csv_path: Path, where: str) -> None:
    csv_path.write_bytes(_boundary(_FLEX, where))
    if where in {"later-record", "later-cell"}:
        # Today both are accepted: the mark is part of a value, or of a column
        # name in a section whose first column identifies it.
        assert detect_ibkr_csv_format(csv_path) is IbkrCsvFormat.FLEX_QUERY
        [section] = split_sections(csv_path).values()
        [row] = section.rows
        marked = [key for key, value in row.items() if GARBLED_TEXT in key + value]
        assert marked == (
            ["ClientAccountID"] if where == "later-record" else [f'{GARBLED_TEXT}"CurrencyPrimary"']
        )
        return
    row = {"after-blank-line": 1, "after-blank-lines": 3}.get(where, 0)
    with pytest.raises(ParseError) as caught:
        split_sections(csv_path)
    _assert_is(caught.value, _no_flex_header(row))
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(csv_path)
    _assert_is(caught.value, _no_sentinel(row, csv_path))
