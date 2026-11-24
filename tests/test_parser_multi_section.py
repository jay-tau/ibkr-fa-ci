"""Tests for the multi-section Flex CSV splitter."""

from __future__ import annotations

import csv
import errno
import os
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError, PathRefusalError
from ibkr_fa.parser import KnownSection, Section, split_sections
from ibkr_fa.parser.multi_section import _classify_row
from tests._flex_section_rows import (
    FLEX_SECTION_KINDS,
    OPEN_POSITIONS_HEADER,
    OPEN_POSITIONS_XQAW,
    TRADES_CLOSED_LOT_XQAT,
    TRADES_EXECUTION_XQAW,
    TRADES_LOT_DETAIL_HEADER,
)
from tests._flex_section_rows import FLEX_TRADES_HEADER as TRADES_HEADER
from tests._malformed_csv import (
    AFTER_QUOTE,
    HOLDER,
    MARKER,
    UNCLOSED,
    assert_malformed,
    oversized_cell,
)
from tests._private_checks import private_test
from tests._private_data import private_dataset

_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)

# Synthetic simple-schema Trades row: an XQAP opening buy of 16 shares at
# 128.75 (TradeMoney 2060.00), with a 1.00 commission (NetCash -2061.00).
TRADES_XQAP = (
    '"USD","1","STK","COMMON","XQAP","SAMPLE ISSUER AP CORP","99990F100",'
    '"US99990F1003","NASDAQ","US","20260512","20260512","20260513","ExchTrade",'
    '"16","128.75","2060.00","-2060.00","0","-1","USD","-2061.00","130.20","O",'
    '"2061.00","0","BUY"'
)


def write(tmp_path: Path, *lines: str) -> Path:
    p = tmp_path / "flex.csv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_recognises_open_positions_lot_section(tmp_path: Path) -> None:
    sections = split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW))
    assert KnownSection.OPEN_POSITIONS_LOT in sections
    section = sections[KnownSection.OPEN_POSITIONS_LOT]
    assert section.kind is KnownSection.OPEN_POSITIONS_LOT
    assert len(section.rows) == 1
    assert section.rows[0]["Symbol"] == "XQAW"
    assert section.rows[0]["LevelOfDetail"] == "LOT"
    assert section.rows[0]["ISIN"] == "US99990E1038"


def test_recognises_trades_section(tmp_path: Path) -> None:
    sections = split_sections(write(tmp_path, TRADES_HEADER, TRADES_XQAP))
    assert KnownSection.TRADES in sections
    section = sections[KnownSection.TRADES]
    assert section.kind is KnownSection.TRADES
    assert section.rows[0]["Symbol"] == "XQAP"
    assert section.rows[0]["Buy/Sell"] == "BUY"


def test_recognises_multiple_sections_in_one_file(tmp_path: Path) -> None:
    sections = split_sections(
        write(
            tmp_path,
            OPEN_POSITIONS_HEADER,
            OPEN_POSITIONS_XQAW,
            "",
            TRADES_HEADER,
            TRADES_XQAP,
        )
    )
    assert {KnownSection.OPEN_POSITIONS_LOT, KnownSection.TRADES} <= set(sections)
    assert sections[KnownSection.OPEN_POSITIONS_LOT].rows[0]["Symbol"] == "XQAW"
    assert sections[KnownSection.TRADES].rows[0]["Symbol"] == "XQAP"


def test_rows_preserve_string_values_verbatim(tmp_path: Path) -> None:
    """Multi-section parser is dumb — it does not Decimal-parse, date-parse,
    or otherwise mutate values. Per-section parsers handle typing."""
    sections = split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW))
    row = sections[KnownSection.OPEN_POSITIONS_LOT].rows[0]
    assert row["Quantity"] == "25"
    assert row["MarkPrice"] == "45.10"
    assert row["CostBasisMoney"] == "969.75"


def test_blank_lines_between_sections_skipped(tmp_path: Path) -> None:
    sections = split_sections(
        write(
            tmp_path,
            OPEN_POSITIONS_HEADER,
            OPEN_POSITIONS_XQAW,
            "",
            "",
            "   ",  # whitespace-only line
            "",
            TRADES_HEADER,
            TRADES_XQAP,
        )
    )
    assert KnownSection.OPEN_POSITIONS_LOT in sections
    assert KnownSection.TRADES in sections


def test_unknown_section_silently_skipped(tmp_path: Path) -> None:
    """IBKR may emit sections we haven't modelled. The parser drops them
    so v1 doesn't choke on a future Flex Query feature."""
    unknown_header = '"CurrencyPrimary","FuturisticColumnX","FuturisticColumnY"'
    unknown_row = '"USD","abc","def"'
    sections = split_sections(
        write(
            tmp_path,
            OPEN_POSITIONS_HEADER,
            OPEN_POSITIONS_XQAW,
            unknown_header,
            unknown_row,
            TRADES_HEADER,
            TRADES_XQAP,
        )
    )
    assert KnownSection.OPEN_POSITIONS_LOT in sections
    assert KnownSection.TRADES in sections
    # The unknown section appears as a None-kind nowhere; it's just absent.
    assert all(s.kind in KnownSection for s in sections.values())


def test_first_non_blank_row_must_be_a_header(tmp_path: Path) -> None:
    """Garbage at the top of the file is a hard error."""
    with pytest.raises(ParseError) as exc_info:
        split_sections(write(tmp_path, "this is not a CSV header", "neither is this"))
    assert exc_info.value.section == "<file>"
    assert "header" in exc_info.value.message


def test_row_with_wrong_field_count_rejected(tmp_path: Path) -> None:
    """A data row that doesn't match the section header's column count is
    a malformed file — refuse to silently truncate or pad."""
    short_row = '"USD","1","STK","COMMON","XQAW"'
    with pytest.raises(ParseError) as exc_info:
        split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, short_row))
    assert exc_info.value.section == KnownSection.OPEN_POSITIONS_LOT.value
    assert "5 field" in exc_info.value.message
    assert "20" in exc_info.value.message  # declared 20 columns


def test_first_non_blank_row_does_not_leak_account_id_via_raw_value(
    tmp_path: Path,
) -> None:
    """T1-r10 quality P2 regression: when ``split_sections`` rejects a
    Flex file whose first non-blank row is not a recognised section
    header, the row content must NOT leak into ``ParseError.raw_value``
    (which ``ParseError.__str__`` renders verbatim into exception
    strings reproduced in bug reports / stderr).

    Setup: a file whose first row is a stray Account Information Data
    row (no preceding header) — the IBKR account number is the row's
    first cell in Flex format. The splitter rejects with "first
    non-blank row is not a known section header"; the raw account ID
    must not survive into ``raw_value``.
    """
    with pytest.raises(ParseError) as exc:
        split_sections(write(tmp_path, '"U12345678","Test","Individual","USD","IBLLC-US"'))
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert "U12345678" not in exc.value.raw_value
    assert exc.value.raw_value == ""


def test_row_with_wrong_field_count_does_not_leak_account_id_via_raw_value(
    tmp_path: Path,
) -> None:
    """T1-r10 quality P2 regression: when ``split_sections`` rejects a
    row whose width doesn't match the current section header (e.g., a
    malformed Account Information data row), the raw account ID in the
    row MUST NOT leak into ``ParseError.raw_value``.

    Setup: a valid Account Information header followed by a Data row
    with an extra (mismatched) trailing cell. The data row carries the
    raw IBKR account ID in its ``ClientAccountID`` position.
    """
    header = '"ClientAccountID","Name","AccountType","CurrencyPrimary","IBEntity"'
    bad_row = '"U12345678","Test Holder","Individual","USD","IBLLC-US","EXTRA-CELL"'
    with pytest.raises(ParseError) as exc:
        split_sections(write(tmp_path, header, bad_row))
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert "U12345678" not in exc.value.raw_value
    assert exc.value.raw_value == ""
    assert "6 field" in exc.value.message  # error context preserved (6 cells vs 5)


def test_duplicate_recognised_section_rejected(tmp_path: Path) -> None:
    """If IBKR ever splits a section by date and re-emits the header, we
    don't know how to merge — refuse loudly."""
    with pytest.raises(ParseError) as exc_info:
        split_sections(
            write(
                tmp_path,
                OPEN_POSITIONS_HEADER,
                OPEN_POSITIONS_XQAW,
                OPEN_POSITIONS_HEADER,  # second header for the same section
                OPEN_POSITIONS_XQAW,
            )
        )
    assert exc_info.value.section == KnownSection.OPEN_POSITIONS_LOT.value
    assert "more than once" in exc_info.value.message
    # row_index contract: zero-based, never negative.
    assert exc_info.value.row_index >= 0


def test_duplicate_section_with_no_data_rows_still_has_non_negative_row_index(
    tmp_path: Path,
) -> None:
    """Edge case: two identical headers with no intervening data rows.
    section_row_index is still its -1 sentinel when _flush fires, so
    row_index must be clamped to >= 0 to honour the ParseError contract."""
    with pytest.raises(ParseError) as exc_info:
        split_sections(
            write(
                tmp_path,
                OPEN_POSITIONS_HEADER,
                OPEN_POSITIONS_HEADER,
            )
        )
    assert exc_info.value.section == KnownSection.OPEN_POSITIONS_LOT.value
    assert exc_info.value.row_index >= 0


def test_empty_section_kept_with_zero_rows(tmp_path: Path) -> None:
    """A section header followed by no data rows is a valid empty section
    (e.g., a user with no closed lots in the period)."""
    sections = split_sections(write(tmp_path, OPEN_POSITIONS_HEADER))
    assert sections[KnownSection.OPEN_POSITIONS_LOT].rows == []


def test_section_columns_preserve_file_order(tmp_path: Path) -> None:
    """Section.columns is an ordered tuple of header columns."""
    sections = split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW))
    cols = sections[KnownSection.OPEN_POSITIONS_LOT].columns
    assert cols[0] == "CurrencyPrimary"
    assert cols[-1] == "HoldingPeriodDateTime"
    assert "ISIN" in cols


def test_missing_file_raises_path_refusal(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.csv"
    with pytest.raises(PathRefusalError) as raised:
        split_sections(path)
    assert raised.value == PathRefusalError("Flex Query CSV not found", path)


@_PERMISSIONS_ENFORCED
def test_csv_in_an_unsearchable_folder_raises_eacces(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """#1238: the CSV existence check raises EACCES instead of calling the CSV missing."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    path = write(blocked, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    with pytest.raises(PermissionError) as raised:
        split_sections(path)
    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(path)!r}"


def test_section_dataclass_is_frozen(tmp_path: Path) -> None:
    sections = split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW))
    section = sections[KnownSection.OPEN_POSITIONS_LOT]
    assert isinstance(section, Section)
    with pytest.raises(AttributeError):
        section.kind = None  # ty: ignore[invalid-assignment]


def test_known_section_header_recognised_when_columns_reordered(tmp_path: Path) -> None:
    """Header detection is order-independent for KNOWN sections: even if
    IBKR rearranges columns, the frozenset signature still matches."""
    # Reorder the columns: put ISIN first instead of CurrencyPrimary.
    cols = [
        "ISIN",
        "CurrencyPrimary",
        "FXRateToBase",
        "AssetClass",
        "SubCategory",
        "Symbol",
        "Description",
        "CUSIP",
        "ListingExchange",
        "IssuerCountryCode",
        "ReportDate",
        "Quantity",
        "MarkPrice",
        "PositionValue",
        "CostBasisPrice",
        "CostBasisMoney",
        "Side",
        "LevelOfDetail",
        "OpenDateTime",
        "HoldingPeriodDateTime",
    ]
    header = ",".join(f'"{c}"' for c in cols)
    # Shuffle the data row to match the new column order.
    values = {
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
        "ReportDate": "20260617",
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
    data = ",".join(f'"{values[c]}"' for c in cols)
    sections = split_sections(write(tmp_path, header, data))
    assert KnownSection.OPEN_POSITIONS_LOT in sections
    section = sections[KnownSection.OPEN_POSITIONS_LOT]
    # Column tuple preserves the file's order — not the canonical order.
    assert section.columns[0] == "ISIN"
    # But the data row is keyed correctly because dict zip uses columns.
    assert section.rows[0]["Symbol"] == "XQAW"
    assert section.rows[0]["ISIN"] == "US99990E1038"


# A record whose first cell is one of these starts a new section.
_SECTION_FIRST_CELLS = frozenset({"CurrencyPrimary", "ClientAccountID"})


def _csv_section_kinds(path: Path) -> set[KnownSection]:
    """Recognized headers read from the file without the splitter."""
    kinds: set[KnownSection] = set()
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.reader(source):
            if row and row[0] == "ClientAccountID":
                kinds.add(KnownSection.ACCOUNT_INFORMATION)
            elif (kind := FLEX_SECTION_KINDS.get(frozenset(row))) is not None:
                kinds.add(kind)
    return kinds


def _csv_section_rows(
    path: Path, columns: tuple[str, ...], headers: set[tuple[str, ...]]
) -> list[dict[str, str]]:
    """The data rows under the header ``columns``, read with ``csv``
    rather than the splitter: the non-blank records after that header, up
    to the next record that starts a section (its first cell is
    ``CurrencyPrimary`` or ``ClientAccountID``, or it is one of
    ``headers``)."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        records = [r for r in csv.reader(f) if any(cell.strip() for cell in r)]
    [start] = [i for i, record in enumerate(records) if tuple(record) == columns]
    rows: list[dict[str, str]] = []
    for record in records[start + 1 :]:
        if record[0] in _SECTION_FIRST_CELLS or tuple(record) in headers:
            break
        rows.append(dict(zip(columns, record, strict=True)))
    return rows


@pytest.mark.parametrize("dataset", ["ibkr/flex-query.csv", "ibkr/flex-query-closed-lots.csv"])
@private_test
def test_sections_equal_the_files_own_csv_records(dataset: str) -> None:
    """End-to-end against a Flex Query CSV dataset (skips when absent),
    checking only properties relative to the file itself: it splits, and
    every section kept holds exactly the records under its header, read
    with ``csv``, column for column. The synthetic tests in this file pin
    both Trades schemas."""
    [sample] = private_dataset(dataset)
    sections = split_sections(sample)
    assert set(sections) == _csv_section_kinds(sample)
    headers = {section.columns for section in sections.values()}
    for section in sections.values():
        assert section.rows == _csv_section_rows(sample, section.columns, headers)


def test_section_audit_detects_dropped_sections(tmp_path: Path, monkeypatch) -> None:
    sample = write(tmp_path, OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW)
    monkeypatch.setattr(__name__ + ".private_dataset", lambda _: (sample,))
    monkeypatch.setattr(__name__ + ".split_sections", lambda _: {})
    with pytest.raises(pytest.fail.Exception, match="private file-relative check failed"):
        test_sections_equal_the_files_own_csv_records("sample.csv")


def test_lot_detail_trades_signature_recognised(tmp_path: Path) -> None:
    """The 30-col LOT-detail Trades schema (emitted when Closed Lots
    is enabled in the Flex Query) must be recognised as
    KnownSection.TRADES, with EXECUTION + CLOSED_LOT data rows
    intermixed in the section's rows list."""
    sections = split_sections(
        write(
            tmp_path,
            TRADES_LOT_DETAIL_HEADER,
            TRADES_EXECUTION_XQAW,
            TRADES_CLOSED_LOT_XQAT,
        )
    )
    assert KnownSection.TRADES in sections
    [exec_row, closed_row] = sections[KnownSection.TRADES].rows
    assert exec_row["LevelOfDetail"] == "EXECUTION"
    assert exec_row["OpenDateTime"] == ""
    assert closed_row["LevelOfDetail"] == "CLOSED_LOT"
    assert closed_row["OpenDateTime"] == "20260721;101500"
    assert closed_row["CostBasis"] == "496.20"


def test_simple_and_lot_detail_trades_both_map_to_same_known_section(
    tmp_path: Path,
) -> None:
    """Sanity: the 27-col simple Trades and the 30-col LOT-detail
    Trades both produce KnownSection.TRADES, so downstream callers
    don't need to branch on schema version when querying the
    sections dict — they just need to detect column presence."""
    simple_dir = tmp_path / "simple"
    simple_dir.mkdir()
    detailed_dir = tmp_path / "detailed"
    detailed_dir.mkdir()
    simple = split_sections(write(simple_dir, TRADES_HEADER, TRADES_XQAP))
    detailed = split_sections(write(detailed_dir, TRADES_LOT_DETAIL_HEADER, TRADES_EXECUTION_XQAW))
    assert KnownSection.TRADES in simple
    assert KnownSection.TRADES in detailed
    assert "LevelOfDetail" not in simple[KnownSection.TRADES].rows[0]
    assert "LevelOfDetail" in detailed[KnownSection.TRADES].rows[0]


def test_utf8_bom_at_start_of_file_accepted(tmp_path: Path) -> None:
    """Desktop CSV exports often add a UTF-8 BOM. utf-8-sig strips it
    transparently so the first header cell is 'CurrencyPrimary', not
    '\\ufeffCurrencyPrimary'. Without this, _is_header_row() would
    reject the whole file."""
    p = tmp_path / "flex_with_bom.csv"
    bom = "\ufeff"
    p.write_text(
        bom + OPEN_POSITIONS_HEADER + "\n" + OPEN_POSITIONS_XQAW + "\n",
        encoding="utf-8",
    )
    sections = split_sections(p)
    assert KnownSection.OPEN_POSITIONS_LOT in sections
    assert sections[KnownSection.OPEN_POSITIONS_LOT].rows[0]["Symbol"] == "XQAW"


def test_parse_error_row_index_is_section_relative(tmp_path: Path) -> None:
    """ParseError.row_index is documented as zero-based within a section.
    A bad row 5 lines into the second section should report row_index=0
    (it's the first data row of THAT section), not the file-level
    line number."""
    short_row = '"USD","1","STK","COMMON","XQAW"'  # 5 fields, header has 20
    p = tmp_path / "flex.csv"
    p.write_text(
        "\n".join(
            [
                OPEN_POSITIONS_HEADER,
                OPEN_POSITIONS_XQAW,
                "",
                "",
                TRADES_HEADER,
                short_row,  # first data row of TRADES, but file row 5
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ParseError) as exc_info:
        split_sections(p)
    assert exc_info.value.section == KnownSection.TRADES.value
    # Section-relative: this is the FIRST data row in the Trades section.
    assert exc_info.value.row_index == 0


def test_classify_row_empty_row_is_not_a_header() -> None:
    """Defensive: _classify_row called with an empty row returns
    (False, None). The splitter skips blank rows before calling, but
    the function is exposed as a helper so it should be safe to call
    independently."""
    assert _classify_row([], file_row_index=0) == (False, None)


@pytest.mark.parametrize(
    ("bad_header", "section"),
    [
        pytest.param(
            OPEN_POSITIONS_HEADER.replace('"Description"', '"Symbol"', 1),
            KnownSection.OPEN_POSITIONS_LOT,
            id="substituted-near-miss",
        ),
        pytest.param(TRADES_HEADER + ',"Symbol"', KnownSection.TRADES, id="added-exact-match"),
    ],
)
def test_header_with_duplicate_column_name_refuses(
    tmp_path: Path, bad_header: str, section: KnownSection
) -> None:
    """#904: a header that LOOKS like a known section but repeats a
    column name (e.g., 'Symbol' twice) refuses, naming the repeat:
    matching it would let dict(zip(...)) on data rows silently overwrite
    the earlier 'Symbol' value, and dropping it as an unknown section
    would silently lose the section. Without the repeat, one header is a
    near miss of Open Positions and the other is exactly Trades."""
    # Sanity-check the test data: it really does contain a duplicate.
    assert bad_header.count('"Symbol"') == 2
    with pytest.raises(ParseError) as exc:
        split_sections(write(tmp_path, bad_header))
    assert exc.value.section == "<file>"
    assert exc.value.row_index == 0
    assert repr(section.value) in exc.value.message
    assert "repeats column(s) ['Symbol']" in exc.value.message
    assert exc.value.raw_value == ""


def test_header_with_duplicate_column_but_no_marker_treated_as_data() -> None:
    """An unrecognised duplicate-cell row with neither a header marker
    nor section resemblance stays a data row."""
    is_header, kind = _classify_row(["X", "Y", "X"], file_row_index=0)
    assert is_header is False
    assert kind is None


def test_account_information_identified_by_first_column_hint(tmp_path: Path) -> None:
    """Account Information has variable column composition by Flex
    Query config, so it isn't identified by signature. Instead it's
    identified by ``row[0] == "ClientAccountID"``. Verify the section
    is recognised across two different column sets (one minimal, one
    extended) — both should yield ``KnownSection.ACCOUNT_INFORMATION``."""
    minimal_header = '"ClientAccountID","Name","AccountType","CurrencyPrimary","IBEntity"'
    minimal_row = '"U1234567","Test Holder","Individual","USD","IBLLC-US"'
    sections = split_sections(write(tmp_path, minimal_header, minimal_row))
    assert KnownSection.ACCOUNT_INFORMATION in sections
    assert sections[KnownSection.ACCOUNT_INFORMATION].rows == [
        {
            "ClientAccountID": "U1234567",
            "Name": "Test Holder",
            "AccountType": "Individual",
            "CurrencyPrimary": "USD",
            "IBEntity": "IBLLC-US",
        }
    ]


def test_account_information_identified_with_extra_columns(tmp_path: Path) -> None:
    extended_header = (
        '"ClientAccountID","AccountAlias","Model","CurrencyPrimary","Name",'
        '"AccountType","IBEntity","DateOpened","PrimaryEmail"'
    )
    extended_row = '"U7654321","","","USD","Other Holder","Joint","IBKR-IE","20200101",""'
    sections = split_sections(write(tmp_path, extended_header, extended_row))
    assert KnownSection.ACCOUNT_INFORMATION in sections
    [row] = sections[KnownSection.ACCOUNT_INFORMATION].rows
    assert row["ClientAccountID"] == "U7654321"
    assert row["AccountType"] == "Joint"
    assert row["IBEntity"] == "IBKR-IE"


def test_first_column_hint_classify_row_direct() -> None:
    is_header, kind = _classify_row(
        ["ClientAccountID", "Name", "AccountType", "CurrencyPrimary", "IBEntity"],
        file_row_index=0,
    )
    assert is_header is True
    assert kind == KnownSection.ACCOUNT_INFORMATION


def test_account_information_with_duplicate_column_refuses(
    tmp_path: Path,
) -> None:
    """#904: a malformed Account Information header with a duplicate
    column must NOT be classified as the known section (dict(zip(...))
    would overwrite the duplicate's earlier value), nor dropped as an
    unknown section: it refuses, counting the repeat (#1038: 'Name' is in
    no signature table, so it isn't named)."""
    bad_header = '"ClientAccountID","Name","AccountType","CurrencyPrimary","IBEntity","Name"'
    bad_row = '"U1","Holder","Individual","USD","IBLLC-US","DUP"'
    with pytest.raises(ParseError) as exc:
        split_sections(
            write(
                tmp_path,
                OPEN_POSITIONS_HEADER,
                _SYNTHETIC_OPEN_POSITIONS_ROW,
                "",
                bad_header,
                bad_row,
            )
        )
    assert exc.value.section == "<file>"
    assert exc.value.row_index == 3
    assert "'Account Information'" in exc.value.message
    assert "repeats 1 column(s) not in any known section;" in exc.value.message
    assert exc.value.raw_value == ""


def test_account_information_can_appear_first(tmp_path: Path) -> None:
    """Regression: a file that opens with Account Information (no
    leading section) must be parsed cleanly — the first-column hint
    fires before any other section has been opened."""
    header = '"ClientAccountID","Name","AccountType","CurrencyPrimary","IBEntity"'
    row = '"U1","Test","Individual","USD","IBLLC-US"'
    sections = split_sections(
        write(tmp_path, header, row, "", OPEN_POSITIONS_HEADER, OPEN_POSITIONS_XQAW)
    )
    assert KnownSection.ACCOUNT_INFORMATION in sections
    assert KnownSection.OPEN_POSITIONS_LOT in sections


def test_duplicate_account_information_section_raises(tmp_path: Path) -> None:
    """The existing duplicate-section check must apply to sections
    identified by first-column hint, not just signature-matched ones."""
    header = '"ClientAccountID","Name","AccountType","CurrencyPrimary","IBEntity"'
    row1 = '"U1","Holder1","Individual","USD","IBLLC-US"'
    row2 = '"U2","Holder2","Joint","USD","IBKR-IE"'
    with pytest.raises(ParseError) as exc:
        split_sections(write(tmp_path, header, row1, "", header, row2))
    assert exc.value.section == KnownSection.ACCOUNT_INFORMATION.value
    assert "more than once" in exc.value.message


# ============================================================================
# Section postconditions (PR #124, issue #118)
# ============================================================================


def test_split_sections_post_columns_are_non_empty_and_unique(tmp_path: Path) -> None:
    """The post-loop assertions in split_sections verify that every
    kept section's columns tuple is non-empty and has unique entries.
    This test exercises the happy path; the invariant fires only on
    a future refactor that weakens _classify_row's duplicate-column
    rejection."""
    flex = tmp_path / "valid.csv"
    flex.write_text(
        OPEN_POSITIONS_HEADER
        + "\n"
        + '"USD","1","STK","COMMON","XQAW","SAMPLE ISSUER AW INC","99990E103","US99990E1038","NASDAQ","US","20251231","25","45.10","1127.5","38.79","969.75","Long","LOT","20250616","20250616"\n',  # noqa: E501
        encoding="utf-8",
    )
    sections = split_sections(flex)
    for section in sections.values():
        assert len(section.columns) > 0
        assert len(set(section.columns)) == len(section.columns)


# ============================================================================
# Near-miss headers (#904)
# ============================================================================


def _columns(header: str) -> list[str]:
    return header.replace('"', "").split(",")


def _header(columns: list[str]) -> str:
    return ",".join(f'"{column}"' for column in columns)


_OPEN_POSITIONS_COLUMNS = _columns(OPEN_POSITIONS_HEADER)
_TRADES_COLUMNS = _columns(TRADES_HEADER)
_SYNTHETIC_OPEN_POSITIONS_ROW = ",".join("x" for _ in _OPEN_POSITIONS_COLUMNS)
_ONE_UNKNOWN = "1 column(s) not in any known section"


@pytest.mark.parametrize(
    ("header", "section", "extra", "missing"),
    [
        pytest.param(
            TRADES_LOT_DETAIL_HEADER + ',"OrderTime"',
            KnownSection.TRADES,
            _ONE_UNKNOWN,
            [],
            id="30-column-trades-plus-one",
        ),
        pytest.param(
            TRADES_HEADER + ',"OrderTime"',
            KnownSection.TRADES,
            _ONE_UNKNOWN,
            [],
            id="27-column-trades-plus-one",
        ),
        pytest.param(
            TRADES_HEADER.replace('"TradeDate"', '"Trade Date"'),
            KnownSection.TRADES,
            _ONE_UNKNOWN,
            ["TradeDate"],
            id="trades-column-renamed",
        ),
        pytest.param(
            TRADES_HEADER.replace(',"Taxes"', ""),
            KnownSection.TRADES,
            "column(s) []",
            ["Taxes"],
            id="trades-column-removed",
        ),
        pytest.param(
            OPEN_POSITIONS_HEADER + ',"Multiplier"',
            KnownSection.OPEN_POSITIONS_LOT,
            _ONE_UNKNOWN,
            [],
            id="open-positions-plus-one",
        ),
        pytest.param(
            OPEN_POSITIONS_HEADER + ',"Proceeds","Multiplier"',
            KnownSection.OPEN_POSITIONS_LOT,
            "column(s) ['Proceeds'] and 1 other column(s) not in any known section",
            [],
            id="open-positions-plus-a-known-and-an-unknown-column",
        ),
        pytest.param(
            _header([*_OPEN_POSITIONS_COLUMNS[:10], "MarkPrice"]),
            KnownSection.OPEN_POSITIONS_LOT,
            "column(s) []",
            sorted(_OPEN_POSITIONS_COLUMNS[10:12] + _OPEN_POSITIONS_COLUMNS[13:]),
            id="eleven-of-twenty-open-positions-columns",
        ),
    ],
)
def test_near_miss_header_refuses(
    tmp_path: Path, header: str, section: KnownSection, extra: str, missing: list[str]
) -> None:
    """#904: a header that is plainly a changed Trades or Open Positions
    header refuses instead of being dropped as an unknown section, naming
    the section and the extra and missing columns against the nearest
    registered signature. #1038: an extra column no signature table holds
    is counted, not named."""
    with pytest.raises(ParseError) as exc:
        split_sections(
            write(tmp_path, OPEN_POSITIONS_HEADER, _SYNTHETIC_OPEN_POSITIONS_ROW, "", header)
        )
    assert exc.value.section == "<file>"
    assert exc.value.row_index == 3
    assert f"changed {section.value!r} section header" in exc.value.message
    assert f"extra {extra} and lacks column(s) {missing!r}" in exc.value.message
    assert exc.value.raw_value == ""


def test_reordered_near_miss_header_refuses_after_an_unknown_section(tmp_path: Path) -> None:
    """#904: a changed Trades header whose first column isn't
    ``CurrencyPrimary`` refuses even inside an unknown section, where it
    would otherwise be read as one of that section's (dropped) data rows."""
    changed = [*_TRADES_COLUMNS, "OrderTime"]
    changed.remove("Symbol")
    with pytest.raises(ParseError) as exc:
        split_sections(
            write(
                tmp_path,
                OPEN_POSITIONS_HEADER,
                _SYNTHETIC_OPEN_POSITIONS_ROW,
                '"CurrencyPrimary","FuturisticColumnX","FuturisticColumnY"',
                '"USD","abc","def"',
                _header(["Symbol", *changed]),
            )
        )
    assert exc.value.row_index == 4
    assert "changed 'Trades' section header" in exc.value.message
    assert f"extra {_ONE_UNKNOWN} and lacks column(s) []" in exc.value.message


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param([*_OPEN_POSITIONS_COLUMNS[:9], "MarkPrice"], id="half-of-open-positions"),
        pytest.param(
            [
                *_TRADES_COLUMNS[:11],
                *("Quantity", "LevelOfDetail", "OpenDateTime", "HoldingPeriodDateTime"),
            ],
            id="no-open-positions-only-column",
        ),
    ],
)
def test_header_short_of_a_near_miss_is_skipped(tmp_path: Path, columns: list[str]) -> None:
    """#904: a header needs more than half of a signature's columns and
    at least one column only that section has to refuse. Short of
    either, it is an unknown section and is dropped as before. The
    second header holds 15 of Open Positions' 20 columns, but every one
    of them is shared with Trades."""
    sections = split_sections(
        write(
            tmp_path,
            OPEN_POSITIONS_HEADER,
            _SYNTHETIC_OPEN_POSITIONS_ROW,
            _header(columns),
            ",".join('"x"' for _ in columns),
        )
    )
    assert set(sections) == {KnownSection.OPEN_POSITIONS_LOT}
    assert len(sections[KnownSection.OPEN_POSITIONS_LOT].rows) == 1


def test_data_row_with_a_trades_column_name_as_a_value_still_parses(tmp_path: Path) -> None:
    """#904: the near-miss rule reads the whole row, so one cell that
    equals a Trades-only column name doesn't turn a data row into a
    header."""
    extended_header = (
        '"ClientAccountID","AccountAlias","Model","CurrencyPrimary","Name",'
        '"AccountType","IBEntity","DateOpened","PrimaryEmail"'
    )
    row = '"U7654321","Proceeds","","USD","Other Holder","Joint","IBKR-IE","20200101",""'
    sections = split_sections(write(tmp_path, extended_header, row))
    [parsed] = sections[KnownSection.ACCOUNT_INFORMATION].rows
    assert parsed["AccountAlias"] == "Proceeds"


# ============================================================================
# Malformed CSV records (#1098)
# ============================================================================

_REEXPORT = "re-export the Flex Query."
_XQAW_NAME = '"SAMPLE ISSUER AW INC"'
# The XQAW row with its Description spanning two lines (rows 1-2 after the header).
_TWO_LINE_XQAW = OPEN_POSITIONS_XQAW.replace(_XQAW_NAME, '"SAMPLE ISSUER\nAW INC"')


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(UNCLOSED, id="quote-left-open"),
        pytest.param(AFTER_QUOTE, id="text-after-quote"),
        pytest.param(oversized_cell(), id="oversized-cell"),
    ],
)
def test_malformed_record_refuses(tmp_path: Path, cell: str) -> None:
    """#1098: the reader used to repair bad quoting, and a cell over the CSV
    field-size limit raised a raw ``csv.Error``. Each now refuses at the
    0-based line the bad record starts on."""
    row = OPEN_POSITIONS_XQAW.replace(_XQAW_NAME, cell)
    with pytest.raises(ParseError) as caught:
        split_sections(write(tmp_path, OPEN_POSITIONS_HEADER, row, TRADES_HEADER, TRADES_XQAP))
    assert_malformed(caught.value, section="<file>", row_index=1, line=2, reexport=_REEXPORT)


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(("", "  ", ",,"), id="after-blank-lines"),
        pytest.param((OPEN_POSITIONS_HEADER, _TWO_LINE_XQAW), id="after-a-two-line-cell"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param((f'"USD",{UNCLOSED}',), id="quote-left-open"),
        pytest.param((f'"USD","{HOLDER}', f'{MARKER}"x'), id="text-after-a-two-line-quote"),
    ],
)
def test_malformed_record_reports_the_line_it_starts_on(
    tmp_path: Path, before: tuple[str, ...], bad: tuple[str, ...]
) -> None:
    """#1098: the refusal names the bad record's first line, counting blank
    lines and every line an earlier quoted cell spans, though the reader has
    read past it, to the end of the file for a quote left open."""
    with pytest.raises(ParseError) as caught:
        split_sections(write(tmp_path, *before, *bad, TRADES_HEADER))
    assert_malformed(caught.value, section="<file>", row_index=3, line=4, reexport=_REEXPORT)


@pytest.mark.parametrize(
    ("lines", "section", "row_index"),
    [
        pytest.param(
            ('" ', ' "', "this is not a CSV header"),
            "<file>",
            2,
            id="first-row-not-a-header",
        ),
        pytest.param(
            (OPEN_POSITIONS_HEADER, _TWO_LINE_XQAW, "", TRADES_HEADER + ',"OrderTime"'),
            "<file>",
            4,
            id="near-miss-header",
        ),
        pytest.param(
            (OPEN_POSITIONS_HEADER, _TWO_LINE_XQAW, '"USD","1"'),
            KnownSection.OPEN_POSITIONS_LOT.value,
            1,
            id="column-count-stays-section-relative",
        ),
    ],
)
def test_file_level_refusal_reports_the_line_its_record_starts_on(
    tmp_path: Path, lines: tuple[str, ...], section: str, row_index: int
) -> None:
    """#1098: a refusal under ``<file>`` names the 0-based line its record
    starts on, not the record's count, which falls one short for each extra
    line an earlier quoted cell spans (here a blank cell, and the XQAW
    Description). A row number within a section stays as it was."""
    with pytest.raises(ParseError) as caught:
        split_sections(write(tmp_path, *lines))
    assert (caught.value.section, caught.value.row_index) == (section, row_index)
