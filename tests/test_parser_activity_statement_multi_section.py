"""Tests for :mod:`ibkr_fa.parser.activity_statement.multi_section`."""

from __future__ import annotations

import csv
import errno
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError, PathRefusalError
from ibkr_fa.models.source import DividendEvidence
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    Section,
    dividend_evidence,
    multi_section,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.dividends import _REQUIRED_DIVIDENDS_ROW_KEYS
from ibkr_fa.parser.activity_statement.interest import _REQUIRED_INTEREST_ROW_KEYS
from ibkr_fa.parser.activity_statement.multi_section import (
    _AGGREGATION_DROPPED_SECTIONS,
    _HEADERLESS_SECTIONS,
    _IGNORED_SECTIONS,
    _REFUSE_SECTIONS,
    _SECTION_SHAPES,
    _BlockShape,
)
from ibkr_fa.parser.activity_statement.trades import _REQUIRED_TRADES_ROW_KEYS
from ibkr_fa.parser.activity_statement.withholding_tax import _REQUIRED_WHT_ROW_KEYS
from tests._activity_statement_rows import write_statement_lines as write
from tests._malformed_csv import HOLDER, MARKER, UNCLOSED, assert_malformed, oversized_cell
from tests._private_checks import private_test
from tests._private_data import private_dataset
from tests._text_edits import replace_once
from tests._trades_totals import with_trades_totals

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)


# Minimal valid two-section input — exercises both the per-field
# (Field Name/Field Value) shape and the column-tabular shape that
# the same splitter must handle uniformly.
_MINIMAL_VALID = [
    "Statement,Header,Field Name,Field Value",
    "Statement,Data,BrokerName,Interactive Brokers LLC",
    "Statement,Data,Title,Activity Statement",
    "Account Information,Header,Field Name,Field Value",
    'Account Information,Data,Name,"TAX PAYER"',
    "Account Information,Data,Account,U*******",
]


# ---- happy path ------------------------------------------------------------


def test_splits_minimal_valid_input(tmp_path: Path) -> None:
    sections = split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID))
    assert set(sections) == {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
    }
    [stmt_block] = sections[KnownActivitySection.STATEMENT]
    assert stmt_block.columns == ("Field Name", "Field Value")
    assert stmt_block.rows == [
        {"Field Name": "BrokerName", "Field Value": "Interactive Brokers LLC"},
        {"Field Name": "Title", "Field Value": "Activity Statement"},
    ]


def test_section_dict_value_is_list_of_blocks(tmp_path: Path) -> None:
    """The return type is always a list because some sections (NAV)
    can emit multiple header blocks under one section name."""
    sections = split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID))
    for blocks in sections.values():
        assert isinstance(blocks, list)
        assert all(isinstance(b, Section) for b in blocks)


def test_input_order_preserved_within_block(tmp_path: Path) -> None:
    sections = split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID))
    [stmt_block] = sections[KnownActivitySection.STATEMENT]
    field_names = [r["Field Name"] for r in stmt_block.rows]
    assert field_names == ["BrokerName", "Title"]


# ---- multi-header sections (Net Asset Value style) -------------------------


def test_multiple_header_blocks_under_one_section_name(tmp_path: Path) -> None:
    """Real-world case: IBKR's Net Asset Value section emits a
    second Header row mid-section to introduce a Rate-of-Return
    summary table. The splitter must produce two separate Section
    blocks, each with its own columns + rows."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Net Asset Value,Header,Asset Class,Prior Total,Current Long",
            "Net Asset Value,Data,Cash,0,25.50",
            "Net Asset Value,Data,Stock,0,84.25",
            "Net Asset Value,Header,Time Weighted Rate of Return",
            "Net Asset Value,Data,-2.35%",
        )
    )
    blocks = sections[KnownActivitySection.NET_ASSET_VALUE]
    assert len(blocks) == 2
    assert blocks[0].columns == ("Asset Class", "Prior Total", "Current Long")
    assert len(blocks[0].rows) == 2
    assert blocks[1].columns == ("Time Weighted Rate of Return",)
    assert blocks[1].rows == [{"Time Weighted Rate of Return": "-2.35%"}]


# ---- SubTotal / Total rows -------------------------------------------------


def test_subtotal_and_total_rows_are_checked_and_never_returned(tmp_path: Path) -> None:
    """Trades section interleaves Data + SubTotal (one per symbol)
    + final Total. SubTotal/Total rows have different column
    populations from Data rows and would corrupt downstream
    aggregation, so they never enter the block's rows. #1090: the
    splitter checks them against the rows instead of dropping them."""
    order = 'Trades,Data,Order,Stocks,USD,{},"2025-03-03, 10:00:00",1,10,10,-10,0,10,0,0,O'
    text = with_trades_totals(
        "\n".join(
            (
                "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,"
                "Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code",
                order.format("XQAA"),
                order.format("XQAA"),
                order.format("XQAY"),
            )
        )
    )
    assert (text.count("Trades,SubTotal,"), text.count("Trades,Total,")) == (2, 1)
    [trades] = split_activity_statement_sections(write(tmp_path, *text.splitlines()))[
        KnownActivitySection.TRADES
    ]
    # The 3 Order rows on lines 2, 3 and 5; the SubTotal rows on lines 4
    # and 6 and the Total on line 7 are checked, not returned.
    assert [(row["DataDiscriminator"], row["Symbol"]) for row in trades.rows] == [
        ("Order", "XQAA"),
        ("Order", "XQAA"),
        ("Order", "XQAY"),
    ]
    assert trades.row_lines == [2, 3, 5]


# ---- unrecognised sections + ignored rows ---------------------------------


def test_ignored_section_is_dropped_and_an_unlisted_one_refuses(tmp_path: Path) -> None:
    """#913: ``Codes`` is on the ignored list, so its rows are dropped
    unread. A section on no list refuses, naming its line, even
    header-only: nothing says whether it changes a filed figure. Since
    #1040 it reports under ``<file>``, not the unknown name, which is a
    cell."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value",
            "Statement,Data,BrokerName,IBKR",
            "Codes,Header,Code,Meaning",
            "Codes,Data,A,Assignment",
            "Codes,Data,P,Partial Fill",
        )
    )
    assert set(sections) == {KnownActivitySection.STATEMENT}

    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,BrokerName,IBKR",
                "Transfers,Header,Symbol,Quantity",
            )
        )
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "<file>",
        3,
        None,
        "",
    )
    assert error.message.startswith(
        "line 3 belongs to a section this tool neither parses nor lists as safe to ignore"
    )
    assert "Transfers" not in str(error)


def test_every_section_is_on_exactly_one_list() -> None:
    """#913: each section name is parsed, ignored or refused, never two
    of these. Every parsed section declares its header shapes, every
    ignored section and ignored column says why, and no column is both
    read and ignored. ``Change in Dividend Accruals`` is the one enum
    member that is ignored: it is kept only as dividend evidence."""
    evidence = KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS
    parsed = {section.value for section in KnownActivitySection} - {evidence.value}
    assert set(_SECTION_SHAPES) == set(KnownActivitySection) - {evidence}
    assert evidence.value in _IGNORED_SECTIONS
    assert not parsed & _IGNORED_SECTIONS.keys()
    assert not parsed & _REFUSE_SECTIONS
    assert not _IGNORED_SECTIONS.keys() & _REFUSE_SECTIONS
    assert _IGNORED_SECTIONS.keys() >= _HEADERLESS_SECTIONS
    assert all(reason.strip() for reason in _IGNORED_SECTIONS.values())
    for shapes in _SECTION_SHAPES.values():
        for shape in shapes:
            assert not shape.read & shape.ignored.keys()
            assert all(reason.strip() for reason in shape.ignored.values())


@pytest.mark.parametrize(
    ("section", "required"),
    [
        (KnownActivitySection.TRADES, _REQUIRED_TRADES_ROW_KEYS),
        (KnownActivitySection.DIVIDENDS, _REQUIRED_DIVIDENDS_ROW_KEYS),
        (KnownActivitySection.WITHHOLDING_TAX, _REQUIRED_WHT_ROW_KEYS),
        (KnownActivitySection.INTEREST, _REQUIRED_INTEREST_ROW_KEYS),
    ],
)
def test_every_column_a_parser_requires_is_declared(
    section: KnownActivitySection, required: tuple[str, ...]
) -> None:
    """#913: the header check never refuses a column a parser needs."""
    assert any(set(required) <= shape.columns for shape in _SECTION_SHAPES[section])


@pytest.mark.parametrize("name", sorted(_IGNORED_SECTIONS.keys() - _HEADERLESS_SECTIONS))
def test_ignored_section_rows_are_not_read(tmp_path: Path, name: str) -> None:
    """#913: an ignored section's rows are dropped unread, whatever
    their row type, width or header, since its reason holds for all of
    them."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            *_MINIMAL_VALID,
            f"{name},Header,,Code,,Code",
            f"{name},Data,x",
            f"{name},Notes,free text",
            f"{name},Total,,,,,,,9",
            f"{name},Adjustment,1,2,3,4,5,6,7,8",
        )
    )
    evidence = {KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS} & {name}
    assert set(sections) == {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
        *evidence,
    }


@pytest.mark.parametrize(
    "row",
    ['Notes,"Free text, with a comma"', "Total P/L for Statement Period,,1.23"],
)
def test_headerless_ignored_section_needs_no_header(tmp_path: Path, row: str) -> None:
    """#913: IBKR prints these two ignored sections as one row with no
    header row. They're exempt from the header-first check by name."""
    sections = split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID, row))
    assert set(sections) == {
        KnownActivitySection.STATEMENT,
        KnownActivitySection.ACCOUNT_INFORMATION,
    }


@pytest.mark.parametrize(
    ("lines", "section", "line"),
    [
        pytest.param(("Fees,Data,Fee,USD",), "Fees", 1, id="no-header"),
        pytest.param(
            ("Fees,Header,Subtitle", "Codes,Header,Code", "Fees,Data,Fee"),
            "Fees",
            3,
            id="after-another-section",
        ),
        pytest.param(
            ("Codes,Header,Code", "Notes,free text", "Codes,Data,A"),
            "Codes",
            3,
            id="after-a-headerless-row",
        ),
    ],
)
def test_ignored_section_row_before_its_header_refuses(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int
) -> None:
    """#913: only the named headerless sections may skip their header.
    Any other ignored section's rows must follow its own header."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines))
    error = exc.value
    assert (error.section, error.row_index, error.column) == (section, line, None)
    assert "row appeared before any 'Header' row" in error.message


def test_change_in_dividend_accruals_is_ignored_but_counts_as_dividend_evidence(
    tmp_path: Path,
) -> None:
    """#913: Change in Dividend Accruals' rows aren't read, so it maps to
    no blocks, but its presence still means dividend activity exists."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            *_MINIMAL_VALID,
            "Change in Dividend Accruals,Header,Asset Category,Currency,Symbol,Date,"
            "Ex Date,Pay Date,Quantity,Tax,Fee,Gross Rate,Gross Amount,Net Amount,Code",
            "Change in Dividend Accruals,Data,Stocks,USD,ABC,2025-12-20,2025-12-19,"
            "2026-01-05,10,0,0,0.10,1.00,1.00,Po",
        )
    )
    assert sections[KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS] == []
    assert dividend_evidence(sections) is DividendEvidence.PRESENT


def test_corporate_actions_section_with_data_raises(tmp_path: Path) -> None:
    """A ``Corporate Actions`` section carrying data can't be dropped
    like a benign unknown section: the split / merger / spin-off it
    records silently reshapes lot quantity and cost basis, so the
    splitter refuses fail-closed rather than file a wrong Schedule FA
    figure (#106 / #60)."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,BrokerName,IBKR",
                "Corporate Actions,Header,Report Date,Description,Quantity",
                "Corporate Actions,Data,2025-06-01,XQAQ(US99992A1097) Split 4 for 1,300",
            )
        )
    assert "Corporate Actions" in str(exc.value)
    assert "issues/106" in str(exc.value)


def test_corporate_actions_header_only_without_data_is_dropped(
    tmp_path: Path,
) -> None:
    """An empty ``Corporate Actions`` section (header, no data rows)
    means no corporate action occurred — it must be dropped like any
    other unknown section, NOT refused. Guards the fail-closed check
    against over-eager refusal on a clean statement."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value",
            "Statement,Data,BrokerName,IBKR",
            "Corporate Actions,Header,Report Date,Description,Quantity",
        )
    )
    assert KnownActivitySection.STATEMENT in sections
    assert all("Corporate Actions" not in k.value for k in sections)


def test_corporate_actions_summary_row_without_data_raises(
    tmp_path: Path,
) -> None:
    """A line-item-suppressed "summary" statement may emit only a
    ``Total`` / ``SubTotal`` row (no ``Data``) under
    ``Corporate Actions``. Refusing on any NON-header row — not just
    ``Data`` — keeps that from slipping through fail-open."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,BrokerName,IBKR",
                "Corporate Actions,Header,Report Date,Description,Quantity",
                "Corporate Actions,Total,,,300",
            )
        )
    assert "Corporate Actions" in str(exc.value)


def test_corporate_actions_refused_even_if_promoted_to_known_section() -> None:
    """The refuse-guard runs on the pre-enum path, so a refuse-section
    must never ALSO be a :class:`KnownActivitySection`. If #106 later
    adds ``CORPORATE_ACTIONS`` to the enum, this fails until the value
    is dropped from ``_REFUSE_SECTIONS`` — forcing the lift of the
    refusal to be a deliberate act, not an accidental side effect."""
    known = {s.value for s in KnownActivitySection}
    assert not (_REFUSE_SECTIONS & known)


@pytest.mark.parametrize(
    ("lines", "section", "line", "row_type"),
    [
        pytest.param(
            (
                "Trades,Header,DataDiscriminator,Symbol",
                "Trades,Data,Order,XQAA",
                "Trades,Notes,Future-row-type,XQAY",
            ),
            "Trades",
            3,
            "Notes",
            id="trades-notes",
        ),
        pytest.param(
            (
                "Dividends,Header,Currency,Date,Description,Amount",
                'Dividends,Data,USD,2025-06-02,"ABC(US99971J1050) Cash Dividend '
                'USD 0.10 per Share (Ordinary Dividend)",10.00',
                'Dividends,Adjustment,USD,2025-06-02,"ABC(US99971J1050) Cash Dividend '
                'USD 0.10 per Share (Ordinary Dividend)",-3.00',
            ),
            "Dividends",
            3,
            "Adjustment",
            id="dividends-adjustment",
        ),
        pytest.param(
            ("Statement,Header,Field Name,Field Value", "Statement,,BrokerName,IBKR"),
            "Statement",
            2,
            "",
            id="blank-row-type",
        ),
    ],
)
def test_unknown_row_type_in_a_parsed_section_refuses(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int, row_type: str
) -> None:
    """#913: a parsed section's rows are Header, Data, SubTotal or Total.
    A row of any other type could carry a figure (a dividend adjustment)
    that no parser reads, so it refuses instead of being dropped. Since
    #1040 it doesn't print the row type, which is a cell."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines))
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        section,
        line,
        None,
        "",
    )
    assert error.message.startswith(
        f"line {line} has a row type other than 'Header', 'Data', 'SubTotal' or 'Total'"
    )
    if row_type:
        assert row_type not in str(error)


@pytest.mark.parametrize(
    ("lines", "section", "position"),
    [
        pytest.param(
            (
                "Dividends,Header,Currency,Date,Description,Amount,Adjustment",
                'Dividends,Data,USD,2025-06-02,"ABC(US99971J1050) Cash Dividend '
                'USD 0.10 per Share (Ordinary Dividend)",10.00,-3.00',
                "Dividends,Data,Total,,,10.00,",
            ),
            "Dividends",
            5,
            id="dividends-with-rows",
        ),
        pytest.param(
            ("Dividends,Header,Currency,Date,Description,Amount,Adjustment",),
            "Dividends",
            5,
            id="dividends-header-only",
        ),
        pytest.param(
            (
                "Net Asset Value,Header,Asset Class,Prior Total,Current Long,"
                "Current Short,Current Total,Change,Adjustment",
                "Net Asset Value,Data,Total,0,53,0,50,50,-3",
            ),
            "Net Asset Value",
            7,
            id="nav-total-with-adjustment",
        ),
        pytest.param(
            ("Net Asset Value,Header,Asset Class,Current Total,Time Weighted Rate of Return",),
            "Net Asset Value",
            3,
            id="nav-columns-of-two-blocks",
        ),
        pytest.param(
            ("Account Information,Header,Field Name,Field Value,Extra",),
            "Account Information",
            3,
            id="account-information",
        ),
    ],
)
def test_header_outside_a_declared_block_shape_refuses(
    tmp_path: Path, lines: tuple[str, ...], section: str, position: int
) -> None:
    """#913: a header must fit one declared block shape. A column outside
    that shape refuses at the header's line, before any row, so a
    header-only block is checked too. Its values (a -3 adjustment beside
    a closing NAV of 50) could otherwise change a filed figure unseen.
    Since #1040 the column is named by position, not by its header cell."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID, *lines))
    error = exc.value
    line = len(_MINIMAL_VALID) + 1
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        section,
        line,
        None,
        "",
    )
    assert error.message.startswith(
        f"the header on line {line} has a column name {position} (the first name "
        "after 'Header' is 1) that this block shape neither reads nor lists as safe "
        "to ignore."
    )


@pytest.mark.parametrize(
    ("section", "columns"),
    [
        pytest.param(section, sorted(shape.columns), id=f"{section.value}-{index}")
        for section, shapes in _SECTION_SHAPES.items()
        for index, shape in enumerate(shapes)
    ],
)
def test_every_declared_header_shape_is_accepted(
    tmp_path: Path, section: KnownActivitySection, columns: list[str]
) -> None:
    """#913: a header naming every column of one declared shape splits,
    in any order. A shape's columns may also be absent, as Financial
    Instrument Information's ``ISIN`` and ``Underlying`` are in some
    exports."""
    sections = split_activity_statement_sections(
        write(tmp_path, f"{section.value},Header,{','.join(columns)}")
    )
    [block] = sections[section]
    assert block.columns == tuple(columns)


# ---- column-tuple shape ----------------------------------------------------


def test_trailing_empty_cells_in_header_stripped(tmp_path: Path) -> None:
    """IBKR pads every row to the file's max column count. Section
    headers therefore end with many empty cells. Those would
    become empty-string column names polluting the dict keys —
    strip them."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value,,,,",
            "Statement,Data,BrokerName,IBKR,,,,",
        )
    )
    [stmt] = sections[KnownActivitySection.STATEMENT]
    assert stmt.columns == ("Field Name", "Field Value")
    # The data row likewise had trailing padding; the dict keeps
    # only the columns the header declared.
    assert stmt.rows == [{"Field Name": "BrokerName", "Field Value": "IBKR"}]


def test_data_row_with_fewer_cells_than_header_raises(tmp_path: Path) -> None:
    """If a Data row has fewer value cells than the Header declared
    columns, that's a malformed file — raise rather than silently
    return partial dicts."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Trades,Header,Symbol,Quantity,Basis",
                "Trades,Data,only-one",  # 3 expected, 1 given
            )
        )
    assert exc.value.section == "Trades"
    assert "1 value cell" in exc.value.message
    assert "3 column" in exc.value.message


def test_data_row_with_empty_overflow_cells_treated_as_padding(tmp_path: Path) -> None:
    """IBKR pads every row to the file's max column count with
    EMPTY trailing cells. Empty overflow cells past the declared
    column count are padding and are dropped — the dict keeps only
    what the header declared."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value",
            "Statement,Data,x,y,,,",
        )
    )
    [stmt] = sections[KnownActivitySection.STATEMENT]
    assert stmt.rows == [{"Field Name": "x", "Field Value": "y"}]


def test_data_row_with_populated_overflow_cells_raises(tmp_path: Path) -> None:
    """Codex P2 on PR #102: silently dropping NON-empty overflow
    cells could corrupt downstream tax calculations (e.g., losing
    a Code at the end of a Trades row). A populated cell past the
    declared columns means either a malformed file or an IBKR
    schema change — raise rather than silently drop."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,x,y,POPULATED-OVERFLOW,",
            )
        )
    assert exc.value.section == "Statement"
    assert "populated overflow" in exc.value.message.lower()
    assert "2 column" in exc.value.message  # header declared 2


def test_data_row_with_mixed_overflow_populated_and_empty_raises(
    tmp_path: Path,
) -> None:
    """Regression for the off-by-one case: trailing empty cell is
    still fine, but a populated cell somewhere in the overflow
    region must trip the check."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,x,y,,POPULATED,",
            )
        )
    assert "populated overflow" in exc.value.message.lower()


def test_data_row_too_few_cells_does_not_leak_account_id_via_raw_value(
    tmp_path: Path,
) -> None:
    """T1-r10 quality P2 regression: when an Account Information Data
    row has fewer value cells than the header declared (the "data row
    has N value cell(s)" branch in ``_row_cells``), the raw
    ``Account`` field value (the IBKR account number) must NOT leak
    into ``ParseError.raw_value``, which ``ParseError.__str__`` renders
    verbatim into exception strings (bug reports, stderr).

    Setup: the 2-value-column Account Information header (``Field Name``,
    ``Field Value``) but a Data row with only 1 value cell
    (``U12345678``) — one short of the 2 the header declared,
    exercising the "too few cells" branch. The account ID
    is present in the raw row that the splitter sees (and would
    previously have leaked via the old ``",".join(row)`` rendering of
    the full row), but must not appear in any user-visible surface.
    """
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,U12345678",
            )
        )
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert "U12345678" not in exc.value.raw_value
    assert exc.value.raw_value == ""
    assert "value cell" in exc.value.message  # error context preserved


def test_data_row_populated_overflow_does_not_leak_account_id_via_raw_value(
    tmp_path: Path,
) -> None:
    """T1-r10 quality P2 regression: the "populated overflow cells"
    branch in ``_row_cells`` (silently dropping non-empty
    overflow would corrupt downstream tax math). If the offending
    overflow cell is an account ID (e.g., a malformed Account
    Information row with extra trailing data), the raw ID must NOT
    leak via ``ParseError.raw_value``.
    """
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Account Information,Header,Field Name",
                "Account Information,Data,Account,U12345678",
            )
        )
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert "U12345678" not in exc.value.raw_value
    assert exc.value.raw_value == ""
    assert "populated overflow" in exc.value.message.lower()


def test_header_with_duplicate_column_names_raises(tmp_path: Path) -> None:
    """Codex P2 on PR #102: Activity Statement headers are
    identified by section name only (unlike the Flex Query
    signature-based identification), so duplicate column names
    don't get caught by signature mismatch. Without this guard,
    ``dict(zip(...))`` on data rows would silently overwrite the
    earlier value for any repeated column — losing data."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,A,A",
                "Statement,Data,x,y",
            )
        )
    assert (exc.value.section, exc.value.row_index, exc.value.column) == ("Statement", 1, None)
    assert exc.value.message.startswith(
        "section header's column name 2 repeats column name 1 (the first name after 'Header' is 1)"
    )
    assert exc.value.raw_value == ""


def test_header_duplicate_column_check_skips_empty_padding(
    tmp_path: Path,
) -> None:
    """Trailing empty cells are stripped BEFORE the dedup check
    runs, so a normal IBKR header with lots of trailing commas
    doesn't trip the check."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value,,,,",
            "Statement,Data,x,y,,,,",
        )
    )
    [stmt] = sections[KnownActivitySection.STATEMENT]
    assert stmt.columns == ("Field Name", "Field Value")
    assert stmt.rows == [{"Field Name": "x", "Field Value": "y"}]


def test_header_with_three_repeated_column_names_reports_the_first_repeat(
    tmp_path: Path,
) -> None:
    """A column repeated 3 times refuses at its first repeat, naming the
    positions of the repeat and of the name it repeats (#1040)."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Header,B,A,C,A,A",
                "Statement,Data,x,y,z,w,v",
            )
        )
    assert exc.value.message.startswith("section header's column name 4 repeats column name 2 ")


@pytest.mark.parametrize(
    ("lines", "section", "line"),
    [
        pytest.param(("Statement,Header",), "Statement", 1, id="bare"),
        pytest.param(("Statement,Header,,,,",), "Statement", 1, id="padded"),
        pytest.param(
            ("Trades,Header", "Trades,Data,,,", "Trades,Data,,,"),
            "Trades",
            1,
            id="all-empty-data-rows",
        ),
        pytest.param(
            (
                "Open Positions,Header,Symbol,Quantity",
                "Open Positions,Data,XQAA,1",
                "Trades,Header,,,",
            ),
            "Trades",
            3,
            id="after-a-valid-block",
        ),
        pytest.param(
            ("Trades,Header,,,", "Trades,Data,XQAA,1,"),
            "Trades",
            1,
            id="before-a-populated-data-row",
        ),
    ],
)
def test_header_with_no_column_names_raises(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int
) -> None:
    """#812: a known section's header that names no columns, once
    trailing empty cells are stripped, is refused at its line. The
    splitter used to drop such a block, so the section (or one of its
    blocks) looked absent, and a populated data row under it was only
    refused later, as overflow at the data row's line."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines))
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        section,
        line,
        None,
        "",
    )
    assert error.message == (
        "section header declares no column names, so its rows can't be "
        "read. The file is malformed; re-export the Activity Statement."
    )


def test_header_only_known_section_is_an_empty_block(tmp_path: Path) -> None:
    """A known section whose header names its columns but has no data
    rows is a legitimately empty section, kept as a block with no rows."""
    sections = split_activity_statement_sections(
        write(tmp_path, "Trades,Header,Symbol,Quantity,,,")
    )
    assert sections == {
        KnownActivitySection.TRADES: [
            Section(kind=KnownActivitySection.TRADES, columns=("Symbol", "Quantity"), header_line=1)
        ]
    }


@pytest.mark.parametrize(
    ("lines", "section", "line", "position"),
    [
        pytest.param(
            ("Trades,Header,A,,B", "Trades,Data,x,LOST,y"), "Trades", 1, 2, id="empty-between"
        ),
        pytest.param(("Trades,Header,,A",), "Trades", 1, 1, id="leading-empty"),
        pytest.param(("Trades,Header,A, ,B",), "Trades", 1, 2, id="whitespace"),
        pytest.param(("Trades,Header,A,B, ",), "Trades", 1, 3, id="trailing-whitespace"),
        pytest.param(("Statement,Header, ",), "Statement", 1, 1, id="whitespace-only"),
        pytest.param(("Trades,Header,A,,,B",), "Trades", 1, 2, id="two-empty-names"),
        pytest.param(
            (
                "Open Positions,Header,Symbol,Quantity",
                "Open Positions,Data,x,y",
                "Trades,Header,A,,B",
            ),
            "Trades",
            3,
            2,
            id="after-a-valid-block",
        ),
    ],
)
def test_header_with_blank_column_name_raises(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int, position: int
) -> None:
    """#819: a known section's header with an empty or whitespace-only
    column name, once trailing empty cells are stripped, is refused at
    its line. The splitter used to key the value under it by ``""`` or
    ``" "``, which no section parser reads, so it was dropped without an
    error. Two empty names get this error, not the duplicate-name one."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines))
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        section,
        line,
        None,
        "",
    )
    assert error.message == (
        f"section header's column name {position} is empty or only whitespace "
        "(the first name after 'Header' is 1), so the values under it belong to "
        "no column. The file is malformed; re-export the Activity Statement."
    )


def test_header_column_name_with_surrounding_spaces_is_not_stripped(tmp_path: Path) -> None:
    """#819: only a name that is empty or only whitespace is blank. A
    name with spaces around real text is neither stripped nor renamed,
    so it isn't the column the section reads and refuses as unlisted
    (#913)."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(tmp_path, "Trades,Header, Symbol ,Quantity", "Trades,Data,x,y")
        )
    assert (exc.value.section, exc.value.row_index, exc.value.column) == ("Trades", 1, None)
    assert exc.value.message.startswith("the header on line 1 has a column name 1 ")


# ---- header-required invariant ---------------------------------------------


def test_data_row_before_any_header_raises(tmp_path: Path) -> None:
    """A Data row for a recognised section that appears before its
    Header is a malformed file — raise. The error names the section
    and the line number so the user can find it in their editor."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                "Statement,Data,too-early,IBKR",
            )
        )
    assert exc.value.section == "Statement"
    assert exc.value.row_index == 1  # 1-based line number


def test_blank_lines_between_sections_skipped(tmp_path: Path) -> None:
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Statement,Header,Field Name,Field Value",
            "Statement,Data,BrokerName,IBKR",
            "",
            "Account Information,Header,Field Name,Field Value",
            "Account Information,Data,Name,Test",
        )
    )
    assert KnownActivitySection.STATEMENT in sections
    assert KnownActivitySection.ACCOUNT_INFORMATION in sections


# ---- reported line numbers --------------------------------------------------

# The quoted ``Title`` value spans lines 2 and 3, so the next row is the
# file's third CSV record but starts on line 4.
_TWO_LINE_CELL = (
    "Statement,Header,Field Name,Field Value",
    'Statement,Data,Title,"Activity',
    'Statement"',
)


@pytest.mark.parametrize(
    ("lines", "section", "line", "message"),
    [
        pytest.param(
            (*_TWO_LINE_CELL, "Trades,Data,XQAA,1"),
            "Trades",
            4,
            "row appeared before any 'Header' row",
            id="data-before-header",
        ),
        pytest.param(
            (
                "Notes/Legal Notes,Header,Type,Note",
                'Notes/Legal Notes,Data,Legal,"first',
                "second",
                'third"',
                "Trades,Header,A,A",
            ),
            "Trades",
            5,
            "column name 2 repeats column name 1",
            id="duplicate-name-after-unknown-section",
        ),
        pytest.param(
            (
                *_TWO_LINE_CELL,
                "Corporate Actions,Header,Report Date,Description",
                "Corporate Actions,Data,2025-06-01,Split",
            ),
            "Corporate Actions",
            5,
            "corporate-action rows",
            id="corporate-actions",
        ),
        pytest.param(
            (*_TWO_LINE_CELL, "Statement,Data,only-one"),
            "Statement",
            4,
            "data row has 1 value cell(s); the section header declared 2",
            id="too-few-cells",
        ),
        pytest.param(
            (*_TWO_LINE_CELL, "Trades,Header"),
            "Trades",
            4,
            "declares no column names",
            id="no-column-names",
        ),
        pytest.param(
            (*_TWO_LINE_CELL, "Trades,Header,A,,B"),
            "Trades",
            4,
            "column name 2 is empty",
            id="blank-column-name",
        ),
        pytest.param(
            ("", "Statement,Header,Field Name,Field Value", "", "Statement,Data,only-one"),
            "Statement",
            4,
            "data row has 1 value cell(s); the section header declared 2",
            id="blank-lines-unchanged",
        ),
        pytest.param(
            ("Trades,Header,Symbol", 'Trades,Data,"x', 'y",OVERFLOW'),
            "Trades",
            2,
            "populated overflow cells",
            id="row-spanning-lines-reports-its-first-line",
        ),
    ],
)
def test_refusal_reports_the_line_its_row_starts_on(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int, message: str
) -> None:
    """#824: a quoted cell can hold line breaks, so one CSV record can
    span several lines. A refusal reports the line on which its row
    starts, as a text editor numbers it. The splitter used to report the
    row's CSV record number, which falls one short for every extra line
    of an earlier record. Blank lines were always counted, and a row that
    spans lines itself still reports its first line."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines))
    error = exc.value
    assert (error.section, error.row_index, error.column) == (section, line, None)
    assert message in error.message


def test_every_data_row_keeps_the_line_it_starts_on(tmp_path: Path) -> None:
    """#905, #908: each block records the file line of its header and of
    each of its data rows, in row order, counting lines a quoted cell
    spans and skipping the SubTotal/Total rows the splitter drops (in
    Open Positions; #1090 checks Trades' instead). A second header
    starts a new block with its own lines."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            *_TWO_LINE_CELL,
            "",
            "Open Positions,Header,Symbol,Quantity",
            'Open Positions,Data,"XA',
            'LA",1',
            "Open Positions,SubTotal,,1",
            "Open Positions,Data,XQAY,2",
            "Interest,Header,Currency,Date,Description,Amount",
            "Interest,Data,USD,2025-06-30,USD Debit Interest for Jun-2025,-0.50",
            "Interest,Header,Currency,Date,Description,Amount",
            "Interest,Data,USD,2025-07-31,USD Debit Interest for Jul-2025,-0.25",
        )
    )
    [statement] = sections[KnownActivitySection.STATEMENT]
    assert (statement.header_line, statement.row_lines) == (1, [2])
    [positions] = sections[KnownActivitySection.OPEN_POSITIONS]
    assert (positions.header_line, positions.row_lines) == (5, [6, 9])
    assert [row["Symbol"] for row in positions.rows] == ["XA\nLA", "XQAY"]
    first, second = sections[KnownActivitySection.INTEREST]
    assert (first.header_line, first.row_lines) == (10, [11])
    assert (second.header_line, second.row_lines) == (12, [13])


# ---- malformed CSV records (#1098) -------------------------------------------

_DIVIDENDS_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_with_dividends_2025.csv"
)
_REEXPORT = "re-export the Activity Statement."
_DIVIDENDS_HEADER = "Dividends,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"
_FIRST_DIVIDEND = (
    "Dividends,Data,USD,16-03-2025,XQBB(US99995B1044) Cash Dividend USD 0.21 per Share "
    "(Ordinary Dividend),2.10,,,,,,,,,,,\n"
)
_DIVIDENDS_TOTAL = "Dividends,Data,Total,,,4.20,,,,,,,,,,,\n"


@pytest.mark.parametrize(
    ("edit", "line"),
    [
        pytest.param(
            lambda text: (
                text[: text.index(_DIVIDENDS_TOTAL)]
                + f'Dividends,Data,Total,{HOLDER},{MARKER},"4.20\n'
            ),
            21,
            id="cut-short-total",
        ),
        pytest.param(
            lambda text: replace_once(
                text,
                _FIRST_DIVIDEND,
                f'Dividends,Data,USD,16-03-2025,{HOLDER} {MARKER},"100"7.00,,,,,,,,,,,\n',
            ),
            19,
            id="text-after-quote",
        ),
        pytest.param(
            lambda text: replace_once(
                text,
                _DIVIDENDS_HEADER,
                "Deposits & Withdrawals,Header,Currency,Settle Date,Description,Amount\n"
                f"Deposits & Withdrawals,Data,USD,2025-02-03,{UNCLOSED},100.00\n"
                + _DIVIDENDS_HEADER,
            ),
            19,
            id="quote-in-an-ignored-section-left-open",
        ),
        pytest.param(
            lambda text: replace_once(
                text,
                _FIRST_DIVIDEND,
                f"Dividends,Data,USD,16-03-2025,{oversized_cell()},2.10,,,,,,,,,,,\n",
            ),
            19,
            id="oversized-cell",
        ),
    ],
)
def test_malformed_record_refuses(tmp_path: Path, edit: Callable[[str], str], line: int) -> None:
    """#1098: the reader used to repair bad quoting. A cut-short total kept
    its amount, ``"100"7.00`` read as 1007.00, and a quote left open in an
    ignored section swallowed the Dividends section, which then read as
    absent. A cell over the CSV field-size limit raised a raw ``csv.Error``.
    Each now refuses at the line the bad record starts on."""
    path = tmp_path / "statement.csv"
    path.write_text(edit(_DIVIDENDS_FIXTURE.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(path)
    assert_malformed(caught.value, section="<file>", row_index=line, line=line, reexport=_REEXPORT)


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(("", "  ", ",,"), id="after-blank-lines"),
        pytest.param(_TWO_LINE_CELL, id="after-a-two-line-cell"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param((f"Statement,Data,Name,{UNCLOSED}",), id="quote-left-open"),
        pytest.param(
            (f'Statement,Data,Name,"{HOLDER}', f'{MARKER}"x'), id="text-after-a-two-line-quote"
        ),
    ],
)
def test_malformed_record_reports_the_line_it_starts_on(
    tmp_path: Path, before: tuple[str, ...], bad: tuple[str, ...]
) -> None:
    """#1098: the refusal names the bad record's first line, counting blank
    lines and every line an earlier quoted cell spans, though the reader has
    read past it, to the end of the file for a quote left open."""
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(
            write(tmp_path, *before, *bad, "Statement,Data,Title,Activity Statement")
        )
    assert_malformed(caught.value, section="<file>", row_index=4, line=4, reexport=_REEXPORT)


@pytest.mark.parametrize("section", ["Dividends", "Withholding Tax", "Interest"])
@pytest.mark.parametrize("discriminator", ["SubTotal", "Total"])
def test_aggregation_row_in_an_income_section_is_refused(
    tmp_path: Path, section: str, discriminator: str
) -> None:
    """#908: these sections print their totals as Data rows, which their
    parsers check against the rows above them. A SubTotal- or Total-type
    row there can't be checked, so the splitter refuses it instead of
    dropping it as it does in Trades."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                *_TWO_LINE_CELL,
                f"{section},Header,Currency,Date,Description,Amount",
                f"{section},Data,USD,2025-04-30,USD Credit Interest for Apr-2025,1.23",
                f"{section},{discriminator},,,,1.23",
            )
        )
    error = exc.value
    assert (error.section, error.row_index, error.column) == (section, 6, None)
    assert f"line 6 is a {discriminator!r} row" in error.message


@pytest.mark.parametrize("discriminator", ["SubTotal", "Total"])
def test_aggregation_row_in_the_realized_unrealized_summary_is_refused(
    tmp_path: Path, discriminator: str
) -> None:
    """#913: this summary prints its totals as Data rows, and every row's
    ``Cost Adj.`` is checked, so a SubTotal- or Total-type row would be
    dropped unchecked and refuses instead."""
    section = KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY.value
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(
                tmp_path,
                f"{section},Header,Asset Category,Symbol,Cost Adj.,Realized Total",
                f"{section},Data,Stocks,ABC,0,1.00",
                f"{section},{discriminator},Stocks,,5,1.00",
            )
        )
    error = exc.value
    assert (error.section, error.row_index, error.column) == (section, 3, None)
    assert f"line 3 is a {discriminator!r} row" in error.message


# #1012: one block of each parsed section that defines no SubTotal or Total
# rows, as its header and one Data row.
_NAV_HEADER = (
    "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change"
)
_NO_AGGREGATION_BLOCKS = {
    "statement": (
        "Statement,Header,Field Name,Field Value",
        "Statement,Data,BrokerName,Interactive Brokers LLC",
    ),
    "account-information": (
        "Account Information,Header,Field Name,Field Value",
        "Account Information,Data,Account,U12345678",
    ),
    "nav-asset-classes": (_NAV_HEADER, "Net Asset Value,Data,Cash,0,30,0,30,30"),
    "nav-rate-of-return": (
        "Net Asset Value,Header,Time Weighted Rate of Return",
        "Net Asset Value,Data,-2.35%",
    ),
    "change-in-nav": (
        "Change in NAV,Header,Field Name,Field Value",
        "Change in NAV,Data,Ending Value,30",
    ),
    "financial-instrument-information": (
        "Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,"
        "Security ID,Listing Exch,Multiplier,Type,Code",
        "Financial Instrument Information,Data,Stocks,ABC,ABC INC,1,US99971J1050,NYSE,1,COMMON,",
    ),
}


@pytest.mark.parametrize("discriminator", ["SubTotal", "Total"])
@pytest.mark.parametrize("block", _NO_AGGREGATION_BLOCKS)
def test_aggregation_row_in_a_section_that_defines_none_is_refused(
    tmp_path: Path, block: str, discriminator: str
) -> None:
    """#1012: these sections define no SubTotal or Total rows, so their
    parsers never read one, and the splitter used to drop it. It now
    refuses, naming the section, the row type and the line, and none of
    the row's cells."""
    header, data = _NO_AGGREGATION_BLOCKS[block]
    section = header.split(",", 1)[0]
    width = len(header.split(",")) - 2
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(tmp_path, header, data, f"{section},{discriminator}" + ",U99999999" * width)
        )
    error = exc.value
    assert (error.section, error.row_index, error.column) == (section, 3, None)
    assert error.message.startswith(
        f"line 3 is a {discriminator!r} row, but the {section!r} section defines "
        f"no 'SubTotal' or 'Total' rows"
    )
    assert error.raw_value == ""
    assert "U99999999" not in str(error)


_NAV_CASH_AND_STOCK = (
    _NAV_HEADER,
    "Net Asset Value,Data,Cash,0,30,0,30,30",
    "Net Asset Value,Data,Stock,0,70,0,70,70",
)
_NAV_DATA_TOTAL = "Net Asset Value,Data,Total,0,100,0,100,100"
_CHANGE_IN_NAV_100 = (
    "Change in NAV,Header,Field Name,Field Value",
    "Change in NAV,Data,Ending Value,100",
)
_FII_HEADER = _NO_AGGREGATION_BLOCKS["financial-instrument-information"][0]


@pytest.mark.parametrize("discriminator", ["SubTotal", "Total"])
@pytest.mark.parametrize(
    ("lines", "section", "line"),
    [
        pytest.param(
            (*_NAV_CASH_AND_STOCK, _NAV_DATA_TOTAL, "Net Asset Value,{},Total,0,150,0,150,150"),
            "Net Asset Value",
            5,
            id="second-nav-total",
        ),
        pytest.param(
            (
                *_NAV_CASH_AND_STOCK,
                "Net Asset Value,{},Total,0,150,0,150,150",
                *_CHANGE_IN_NAV_100,
            ),
            "Net Asset Value",
            4,
            id="nav-total-against-ending-value",
        ),
        pytest.param(
            (
                *_NAV_CASH_AND_STOCK,
                _NAV_DATA_TOTAL,
                *_CHANGE_IN_NAV_100,
                "Change in NAV,{},Ending Value,150",
            ),
            "Change in NAV",
            7,
            id="second-ending-value",
        ),
        pytest.param(
            (
                *_NAV_CASH_AND_STOCK,
                _NAV_DATA_TOTAL,
                "Net Asset Value,{},Total,0,150,0,150,150,9",
                *_CHANGE_IN_NAV_100,
            ),
            "Net Asset Value",
            5,
            id="nav-total-past-the-header",
        ),
        pytest.param(
            (
                "Account Information,Header,Field Name,Field Value",
                "Account Information,Data,Name,Test Holder",
                "Account Information,{},Account,U99999999",
            ),
            "Account Information",
            3,
            id="another-account",
        ),
        pytest.param(
            (
                "Account Information,Header,Field Name,Field Value",
                "Account Information,{},Name,Someone Else",
                "Account Information,Data,Account,U12345678",
            ),
            "Account Information",
            2,
            id="holder-name",
        ),
        pytest.param(
            (
                "Statement,Header,Field Name,Field Value",
                "Statement,Data,BrokerName,Interactive Brokers LLC",
                "Statement,{},BrokerName,Interactive Brokers (U.K.) Limited",
            ),
            "Statement",
            3,
            id="second-broker",
        ),
        pytest.param(
            (
                _FII_HEADER,
                "Financial Instrument Information,Data,Stocks,ABC,ABC INC,1,US99971J1050,"
                "NYSE,1,COMMON,",
                "Financial Instrument Information,{},Stocks,ABC,ABC INC,2,US99971J1051,"
                "NYSE,1,COMMON,",
            ),
            "Financial Instrument Information",
            3,
            id="second-instrument-for-a-symbol",
        ),
    ],
)
def test_aggregation_row_that_its_data_form_would_refuse_is_refused(
    tmp_path: Path, lines: tuple[str, ...], section: str, line: int, discriminator: str
) -> None:
    """#1012: each row, typed Data, is read: a second NAV total or Ending
    Value, a NAV total that disagrees with the Ending Value or overflows
    its header, another account number, a second broker or a second ISIN
    for one symbol refuses, and a holder name is the statement's own.
    Typed SubTotal or Total, it was dropped and the run went on, filing the
    other figure or the configured account. The splitter now refuses it."""
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(
            write(tmp_path, *(text.format(discriminator) for text in lines))
        )
    error = exc.value
    assert (error.section, error.row_index) == (section, line)
    assert f"line {line} is a {discriminator!r} row, but the {section!r} section" in error.message
    assert "U99999999" not in str(error)


@pytest.mark.parametrize("discriminator", ["SubTotal", "Total"])
def test_aggregation_row_in_open_positions_is_dropped(tmp_path: Path, discriminator: str) -> None:
    """#1012: Open Positions stays on the allow-list, so its SubTotal and
    Total rows are dropped and exactly its Data rows are kept."""
    sections = split_activity_statement_sections(
        write(
            tmp_path,
            "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,"
            "Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code",
            "Open Positions,Data,Summary,Stocks,USD,ABC,10,1,5,50,6,60,10,",
            "Open Positions,Data,Summary,Stocks,USD,XQKB,4,1,10,40,9,36,-4,",
            f"Open Positions,{discriminator},,Stocks,USD,,,,,90,,96,6,",
        )
    )
    [positions] = sections[KnownActivitySection.OPEN_POSITIONS]
    assert [row["Symbol"] for row in positions.rows] == ["ABC", "XQKB"]
    assert positions.row_lines == [2, 3]


def test_only_open_positions_drops_aggregation_rows() -> None:
    """#1012: the allow-list is the one place a SubTotal or Total row may be
    dropped, each section with its reason. #1090 took Trades off it: the
    splitter checks Trades' totals instead."""
    assert _AGGREGATION_DROPPED_SECTIONS.keys() == {KnownActivitySection.OPEN_POSITIONS}
    assert all(reason.strip() for reason in _AGGREGATION_DROPPED_SECTIONS.values())


def test_a_section_parsed_later_refuses_aggregation_rows_until_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1012: a section moved into the parsed set but not onto the
    allow-list refuses a Total row rather than dropping it."""
    name = KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS
    monkeypatch.setattr(
        multi_section,
        "_IGNORED_SECTIONS",
        {key: reason for key, reason in _IGNORED_SECTIONS.items() if key != name},
    )
    monkeypatch.setattr(
        multi_section,
        "_SECTION_SHAPES",
        {**_SECTION_SHAPES, name: (_BlockShape(read=frozenset({"Symbol", "Amount"}), ignored={}),)},
    )
    lines = (f"{name},Header,Symbol,Amount", f"{name},Data,ABC,1")

    [block] = split_activity_statement_sections(write(tmp_path, *lines))[name]
    assert block.rows == [{"Symbol": "ABC", "Amount": "1"}]
    with pytest.raises(ParseError) as exc:
        split_activity_statement_sections(write(tmp_path, *lines, f"{name},Total,,1"))
    assert exc.value.message.startswith(
        f"line 3 is a 'Total' row, but the {name.value!r} section defines no"
    )


# ---- file-level behaviour -------------------------------------------------


def test_missing_file_raises_path_refusal(tmp_path: Path) -> None:
    path = tmp_path / "nope.csv"
    with pytest.raises(PathRefusalError) as raised:
        split_activity_statement_sections(path)
    assert raised.value == PathRefusalError("Activity Statement CSV not found", path)


@_PERMISSIONS_ENFORCED
def test_csv_in_an_unsearchable_folder_raises_eacces(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """#1238: the CSV existence check raises EACCES instead of calling the CSV missing."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    path = write(blocked, *_MINIMAL_VALID)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        split_activity_statement_sections(path)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(path)!r}"


def test_utf8_bom_at_start_of_file_accepted(tmp_path: Path) -> None:
    """IBKR's Activity Statement CSVs sometimes ship with a UTF-8
    BOM. The reader uses ``utf-8-sig`` to strip it transparently.
    Without that, the first section name would literally be
    '\\ufeffStatement' and the section wouldn't be recognised."""
    p = tmp_path / "with_bom.csv"
    bom = "\ufeff"
    p.write_text(
        bom + "Statement,Header,Field Name,Field Value\n" + "Statement,Data,BrokerName,IBKR\n",
        encoding="utf-8",
    )
    sections = split_activity_statement_sections(p)
    assert KnownActivitySection.STATEMENT in sections


def test_section_dataclass_is_frozen(tmp_path: Path) -> None:
    """Sections shouldn't be mutated by downstream consumers."""
    sections = split_activity_statement_sections(write(tmp_path, *_MINIMAL_VALID))
    [stmt] = sections[KnownActivitySection.STATEMENT]
    with pytest.raises((AttributeError, TypeError)):
        stmt.columns = ()  # ty: ignore[invalid-assignment]


# ---- end-to-end against private statements --------------------------------
#
# These read files kept outside git (``tests/_private_data.py``) and skip
# when they are absent. They check only relations inside each file.

# Every section but this one is parsed; its rows aren't read (#913).
_ACCRUALS = KnownActivitySection.CHANGE_IN_DIVIDEND_ACCRUALS


def _csv_parsed_section_blocks(
    sample: Path,
) -> dict[str, list[tuple[tuple[str, ...], list[dict[str, str]]]]]:
    """Each parsed section's header-led blocks, read with ``csv`` straight
    from the file: per block, its header's column names (trailing empty
    cells dropped) and its ``Data`` rows keyed by them."""
    parsed = {kind.value for kind in KnownActivitySection} - {_ACCRUALS.value}
    blocks: dict[str, list[tuple[tuple[str, ...], list[dict[str, str]]]]] = {}
    with sample.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.reader(source):
            if len(row) < 2 or row[0] not in parsed:
                continue
            name, row_type, cells = row[0], row[1], row[2:]
            if row_type == "Header":
                while cells and cells[-1] == "":
                    cells.pop()
                blocks.setdefault(name, []).append((tuple(cells), []))
            elif row_type == "Data":
                columns, rows = blocks[name][-1]
                rows.append(dict(zip(columns, cells, strict=False)))
    return blocks


def _check_private_statement_sections(sample: Path) -> None:
    """The splitter returns exactly the known sections named in the file's
    first column, and each parsed section's blocks have the columns and
    ``Data`` rows that ``csv`` reads straight from the file. The ignored
    ``Change in Dividend Accruals`` maps to no blocks."""
    sections = split_activity_statement_sections(sample)
    with sample.open(newline="", encoding="utf-8-sig") as source:
        names = {row[0] for row in csv.reader(source) if row}
    known = {kind.value for kind in KnownActivitySection}
    assert set(sections) == {KnownActivitySection(name) for name in names & known}
    expected = _csv_parsed_section_blocks(sample)
    for kind, blocks in sections.items():
        if kind is _ACCRUALS:
            assert blocks == []
        else:
            assert [(block.columns, block.rows) for block in blocks] == expected[kind.value]


@private_test
def test_works_against_private_fy2025_26_statement() -> None:
    """End-to-end against the private FY 2025-26 statement: the splitter's
    sections, blocks and rows match the file read with ``csv``."""
    [sample] = private_dataset("ibkr/activity-statement-fy2025-26.csv")
    _check_private_statement_sections(sample)


@private_test
def test_works_against_private_cy2025_statement() -> None:
    """End-to-end against the private CY 2025 statement, as above."""
    [sample] = private_dataset("ibkr/activity-statement-cy2025.csv")
    _check_private_statement_sections(sample)


# ============================================================================
# Section postconditions (PR #124, issue #118)
# ============================================================================


def test_split_activity_statement_post_columns_are_non_empty_and_unique(
    tmp_path: Path,
) -> None:
    """The post-loop assertions in split_activity_statement_sections
    verify every kept section block's columns tuple is non-empty
    and has unique entries. The existing _validate_header_columns_unique
    raises at header parse time; the post-loop assertion is
    defence-in-depth against any future refactor that re-routes
    around the inline check."""
    csv = tmp_path / "as.csv"
    csv.write_text(
        "Statement,Header,Field Name,Field Value,,,,,,,,,,,,,\n"
        "Statement,Data,Title,Activity Statement,,,,,,,,,,,,,\n",
        encoding="utf-8",
    )
    sections = split_activity_statement_sections(csv)
    for section_list in sections.values():
        for section in section_list:
            assert len(section.columns) > 0
            assert len(set(section.columns)) == len(section.columns)
