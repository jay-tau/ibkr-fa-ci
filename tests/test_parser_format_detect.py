"""Tests for :mod:`ibkr_fa.parser.format_detect`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.format_detect import IbkrCsvFormat, detect_ibkr_csv_format
from tests._malformed_csv import (
    AFTER_QUOTE,
    HOLDER,
    MARKER,
    UNCLOSED,
    assert_malformed,
    oversized_cell,
)


@pytest.fixture
def tmp_csv(tmp_path: Path) -> Path:
    return tmp_path / "input.csv"


# ---- Activity Statement detection ----------------------------------------


def test_activity_statement_canonical_first_line(tmp_csv: Path) -> None:
    tmp_csv.write_text(
        "Statement,Header,Field Name,Field Value,,,,,,,,,,,,,\n"
        'Statement,Data,Title,"Activity Statement",,,,,,,,,,,,,\n'
    )
    assert detect_ibkr_csv_format(tmp_csv) == IbkrCsvFormat.ACTIVITY_STATEMENT


def test_activity_statement_bom_stripped(tmp_csv: Path) -> None:
    """IBKR's Activity Statement exports sometimes carry a UTF-8 BOM;
    the file encoding 'utf-8-sig' strips it. Without that, the first
    cell would be '\\ufeffStatement' and detection would fail."""
    tmp_csv.write_bytes(
        b"\xef\xbb\xbfStatement,Header,Field Name,Field Value\n"
        b"Statement,Data,Title,Activity Statement\n"
    )
    assert detect_ibkr_csv_format(tmp_csv) == IbkrCsvFormat.ACTIVITY_STATEMENT


def test_activity_statement_after_blank_lines(tmp_csv: Path) -> None:
    """First-non-blank semantics: leading blank lines are skipped."""
    tmp_csv.write_text("\n   \n,,,\nStatement,Header,Field Name,Field Value,,,,,,,,,,,,,\n")
    assert detect_ibkr_csv_format(tmp_csv) == IbkrCsvFormat.ACTIVITY_STATEMENT


# ---- Flex Query detection ------------------------------------------------


def test_flex_query_currency_primary_sentinel(tmp_csv: Path) -> None:
    tmp_csv.write_text(
        '"CurrencyPrimary","FXRateToBase","AssetClass","Symbol"\n"USD","1","STK","XQAQ"\n'
    )
    assert detect_ibkr_csv_format(tmp_csv) == IbkrCsvFormat.FLEX_QUERY


def test_flex_query_client_account_id_sentinel(tmp_csv: Path) -> None:
    """Account Information section can be the first section in a Flex
    export when the user only enabled that section; its header begins
    with ``ClientAccountID``."""
    tmp_csv.write_text('"ClientAccountID","AccountAlias","AccountID"\n"U123","",""\n')
    assert detect_ibkr_csv_format(tmp_csv) == IbkrCsvFormat.FLEX_QUERY


# ---- Rejection paths -----------------------------------------------------


def test_empty_file_rejected(tmp_csv: Path) -> None:
    tmp_csv.write_text("")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    assert "empty" in exc.value.message
    assert exc.value.section == "<file>"


def test_only_blank_lines_rejected(tmp_csv: Path) -> None:
    tmp_csv.write_text("\n\n   \n,,,\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    assert "empty" in exc.value.message or "blank" in exc.value.message


def test_unknown_first_line_rejected(tmp_csv: Path) -> None:
    """A CSV from some other tool — neither sentinel matches."""
    tmp_csv.write_text("Date,Ticker,Quantity,Price\n2025-01-15,XQAQ,10,150.00\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    # Error message names both expected formats so the user knows
    # what to compare against.
    assert "Activity Statement" in exc.value.message
    assert "Flex Query" in exc.value.message
    # #1038: no cell is echoed, not even an innocuous-looking first cell.
    assert "Date" not in exc.value.message
    assert exc.value.raw_value == ""


def test_unknown_first_line_does_not_leak_account_id_via_raw_value(
    tmp_csv: Path,
) -> None:
    """T1-r10 quality P2 regression: when format-detection fails on a
    file whose first non-blank row happens to be an Activity Statement
    ``Account Information`` Data row, the raw IBKR account ID at
    ``first_row[3]`` must NOT leak into ``ParseError.raw_value``
    (which ``ParseError.__str__`` renders verbatim into exception
    strings reproduced in bug reports and stderr).

    This row shape is rejected by detection because Activity Statement
    files always begin with the ``Statement`` section's Header row, not
    an ``Account Information`` Data row. So in practice this fires on
    a corrupted/edited statement where the leading rows were stripped.
    """
    tmp_csv.write_text("Account Information,Data,Account,U12345678\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    rendered = str(exc.value)
    assert "U12345678" not in rendered
    assert "U12345678" not in exc.value.raw_value
    assert exc.value.raw_value == ""


@pytest.mark.parametrize(
    ("first_cell", "parts"),
    [
        pytest.param("U12345678", ("U12345678", "5678"), id="account-id"),
        pytest.param("F1234567", ("F1234567", "4567"), id="advisor-account-id"),
        pytest.param(
            "U1234567-WMAB-MMCD", ("U1234567", "4567", "WMAB", "MMCD"), id="virtual-account"
        ),
        pytest.param("MYALIAS-WMAB-MMCD", ("MYALIAS", "WMAB", "MMCD"), id="alias-virtual-account"),
    ],
)
def test_unknown_first_line_does_not_leak_account_id_via_message(
    tmp_csv: Path, first_cell: str, parts: tuple[str, ...]
) -> None:
    """#1038: an account ID, virtual account number or alias in the first
    cell appears in no form, not even masked: the refusal prints no cell."""
    tmp_csv.write_text(f"{first_cell},USD,Test Holder,Individual,IBLLC-US\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    rendered = str(exc.value)
    for part in parts:
        assert part not in rendered
    assert "***" not in rendered
    assert exc.value.raw_value == ""


def test_statement_without_header_token_rejected(tmp_csv: Path) -> None:
    """Edge case: the literal word 'Statement' could appear in a
    non-IBKR file's first cell. The detector requires BOTH cells
    ('Statement', 'Header') to match, not just the first one."""
    tmp_csv.write_text("Statement,Data,Some,Other\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    assert "doesn't match a known" in exc.value.message


def test_statement_single_cell_rejected(tmp_csv: Path) -> None:
    """One-cell first row that says 'Statement' isn't an Activity
    Statement — the second cell ('Header') is what makes the match
    unambiguous."""
    tmp_csv.write_text("Statement\nfoo\n")
    with pytest.raises(ParseError) as exc:
        detect_ibkr_csv_format(tmp_csv)
    assert "doesn't match a known" in exc.value.message


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    """Detector doesn't shadow filesystem errors — let the OS raise."""
    with pytest.raises(FileNotFoundError):
        detect_ibkr_csv_format(tmp_path / "does-not-exist.csv")


# ---- Malformed first record (#1098) ----------------------------------------

_REEXPORT = "re-export it from IBKR."
_TITLE = "Statement,Data,Title,Activity Statement\n"
# One blank record (a whitespace cell) spanning lines 0 and 1.
_BLANK_TWO_LINE_RECORD = '" \n "\n'


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param(UNCLOSED, id="quote-left-open"),
        pytest.param(AFTER_QUOTE, id="text-after-quote"),
        pytest.param(oversized_cell(), id="oversized-cell"),
    ],
)
def test_malformed_first_record_refuses(tmp_csv: Path, cell: str) -> None:
    """#1098: the reader used to repair bad quoting, and a cell over the CSV
    field-size limit raised a raw ``csv.Error``. Each now refuses at the
    0-based line the record starts on."""
    tmp_csv.write_text(f"Statement,Header,{cell}\n{_TITLE}", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(tmp_csv)
    assert_malformed(caught.value, section="<file>", row_index=0, line=1, reexport=_REEXPORT)


@pytest.mark.parametrize(
    ("before", "row_index"),
    [
        pytest.param("\n  \n,,\n", 3, id="after-blank-lines"),
        pytest.param(_BLANK_TWO_LINE_RECORD, 2, id="after-a-two-line-cell"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(f"Statement,Header,{UNCLOSED}\n", id="quote-left-open"),
        pytest.param(f'Statement,Header,"{HOLDER}\n{MARKER}"x\n', id="text-after-a-two-line-quote"),
    ],
)
def test_malformed_first_record_reports_the_line_it_starts_on(
    tmp_csv: Path, before: str, row_index: int, bad: str
) -> None:
    """#1098: the refusal names the first non-blank record's first line,
    counting blank lines and every line a blank quoted cell spans, though
    the reader has read past it, to the end of the file for a quote left
    open."""
    tmp_csv.write_text(before + bad + _TITLE, encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(tmp_csv)
    assert_malformed(
        caught.value, section="<file>", row_index=row_index, line=row_index + 1, reexport=_REEXPORT
    )


def test_unknown_first_record_reports_the_line_it_starts_on(tmp_csv: Path) -> None:
    """#1098: the sentinel refusal names the 0-based line the record starts
    on, not its count, which a blank quoted cell spanning two lines puts one
    short."""
    tmp_csv.write_text(_BLANK_TWO_LINE_RECORD + "Statement,Data,Some,Other\n", encoding="utf-8")
    with pytest.raises(ParseError) as caught:
        detect_ibkr_csv_format(tmp_csv)
    assert (caught.value.section, caught.value.row_index) == ("<file>", 2)
    assert "doesn't match a known" in caught.value.message
