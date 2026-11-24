"""Flex Query header and trailer records in the splitter and format detection (#1313).

IBKR's Reporting Reference ("Delivery Configuration and General
Configuration") lays out the records: a ``BOF`` row (``BOF``, account ID,
section count, from date, to date, generation date and time, two build
numbers), an ``EOF`` row (``EOF``, account ID), and around each section a
``BOS`` row (section code, ``BOS``, description) and an ``EOS`` row (section
code, ``EOS``, record and amount checksums). It names ``BOA`` and ``EOA``
without a layout. Every file here is synthetic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models.source import FlexPeriod
from ibkr_fa.parser import KnownSection, split_flex_query, split_sections
from ibkr_fa.parser.format_detect import IbkrCsvFormat, detect_ibkr_csv_format
from tests._crash_reports import assert_unchained_refusal
from tests._flex_records import (
    _BAD_DATE,
    _row,
    bof_record,
    eof_record,
    late_non_utf8_records,
    marked_bof,
    marked_eof,
    utf8_refusal,
)
from tests._flex_records import BOF_DATE_RULE as _BOF_DATE
from tests._flex_records import FLEX_RECORD_MARKERS as _MARKERS
from tests._flex_records import flex_layout_refusal as _layout_refusal
from tests._flex_section_rows import OPEN_POSITIONS_HEADER

_ACCOUNT_ROW = '"U12345678","USD","Test Holder","Individual","IBLLC-US"\n'
_ACCOUNT = '"ClientAccountID","CurrencyPrimary","Name","AccountType","IBEntity"\n' + _ACCOUNT_ROW
_LOT_ROW = (
    '"USD","1","STK","COMMON","XQAA","SAMPLE ISSUER AA INC","99990A309","US99990A3095",'
    '"NASDAQ","US","20251231","5","40","200","30","150","Long","LOT","20250210","20250210"\n'
)
_OPEN_POSITIONS = OPEN_POSITIONS_HEADER + "\n" + _LOT_ROW
_SECTIONS = _ACCOUNT + _OPEN_POSITIONS

# The record lines, as IBKR's page lays them out.
_BOF = bof_record()
_EOF = eof_record()
_BOA = '"BOA","U12345678"\n'
_EOA = '"EOA","U12345678"\n'
_BOS_ACCT = '"ACCT","BOS","Account Information"\n'
_EOS_ACCT = '"ACCT","EOS","1","0"\n'
_BOS_POST = '"POST","BOS","Positions; trade date basis"\n'
_EOS_POST = '"POST","EOS","1","200"\n'


def _write(tmp_path: Path, text: str | bytes) -> Path:
    path = tmp_path / "flex.csv"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


# ---- records in their documented places ------------------------------------


def test_a_file_without_records_has_no_period(tmp_path: Path) -> None:
    flex = split_flex_query(_write(tmp_path, _SECTIONS))

    assert flex.period is None
    assert flex.sections == split_sections(_write(tmp_path, _SECTIONS))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_BOF + _SECTIONS + _EOF, id="bof-eof"),
        pytest.param(
            _BOF
            + _BOS_ACCT
            + _ACCOUNT
            + _EOS_ACCT
            + _BOS_POST
            + _OPEN_POSITIONS
            + _EOS_POST
            + _EOF,
            id="every-section-wrapped",
        ),
        pytest.param(
            _BOF
            + _BOA
            + _BOS_ACCT
            + _ACCOUNT
            + _EOS_ACCT
            + "\n"
            + _BOS_POST
            + _OPEN_POSITIONS
            + _EOS_POST
            + _EOA
            + _EOF,
            id="account-records-and-blank-line",
        ),
    ],
)
def test_records_in_their_documented_places_pass(tmp_path: Path, text: str) -> None:
    """The ``BOF`` period is read; the sections are those of the same file
    without records."""
    flex = split_flex_query(_write(tmp_path, text))

    assert flex.period == FlexPeriod(date(2025, 1, 1), date(2025, 12, 31), line=0)
    without = split_sections(_write(tmp_path, _SECTIONS))
    assert flex.sections == without
    assert set(flex.sections) == {
        KnownSection.ACCOUNT_INFORMATION,
        KnownSection.OPEN_POSITIONS_LOT,
    }


def test_a_bof_record_after_blank_lines_is_the_first_record(tmp_path: Path) -> None:
    flex = split_flex_query(_write(tmp_path, "\n\n" + _BOF + _SECTIONS + _EOF))

    assert flex.period == FlexPeriod(date(2025, 1, 1), date(2025, 12, 31), line=2)


def test_the_period_may_start_before_its_year(tmp_path: Path) -> None:
    flex = split_flex_query(_write(tmp_path, bof_record("20200101") + _SECTIONS + _EOF))

    assert flex.period == FlexPeriod(date(2020, 1, 1), date(2025, 12, 31), line=0)


def test_a_one_day_period_passes(tmp_path: Path) -> None:
    flex = split_flex_query(_write(tmp_path, bof_record("20251231") + _SECTIONS + _EOF))

    assert flex.period == FlexPeriod(date(2025, 12, 31), date(2025, 12, 31), line=0)


# ---- record-layout refusals -------------------------------------------------

_BOF_NOT_FIRST = "the BOF record isn't the first record"
_BOF_REPEATED = "there's more than one BOF record"
_EOF_NOT_LAST = "the EOF record is missing or isn't the last record"
_BOF_FIELDS = "the BOF record doesn't have 8 fields"
_BOF_REVERSED = "the BOF from date is after its to date"
_ACCOUNT_RECORD = "a BOA or EOA record is outside its documented place"
_UNNAMED = "a record type IBKR's page doesn't name"
_BOF_8 = ("BOF", "U12345678", "3", "20250101", "20251231", "20260105;101500", "100", "100")


_LAYOUT_CASES = [
    # (id, file text, refusal line, rule)
    ("bof-after-a-section", _ACCOUNT + _BOF + _OPEN_POSITIONS + _EOF, 2, _BOF_NOT_FIRST),
    ("bof-last", _SECTIONS + _BOF, 4, _BOF_NOT_FIRST),
    ("eof-without-bof", _SECTIONS + _EOF, 4, _BOF_NOT_FIRST),
    ("bos-without-bof", _BOS_ACCT + _SECTIONS, 0, _BOF_NOT_FIRST),
    ("eos-without-bof", _ACCOUNT + _EOS_ACCT + _OPEN_POSITIONS, 2, _BOF_NOT_FIRST),
    ("two-bof", _BOF + _BOF + _SECTIONS + _EOF, 1, _BOF_REPEATED),
    ("second-bof-later", _BOF + _ACCOUNT + _BOF + _OPEN_POSITIONS + _EOF, 3, _BOF_REPEATED),
    ("eof-missing", _BOF + _SECTIONS, 4, _EOF_NOT_LAST),
    ("eof-missing-after-eos", _BOF + _BOS_ACCT + _ACCOUNT + _EOS_ACCT, 4, _EOF_NOT_LAST),
    ("eof-then-section", _BOF + _ACCOUNT + _EOF + _OPEN_POSITIONS, 4, _EOF_NOT_LAST),
    ("eof-then-data-row", _BOF + _SECTIONS + _EOF + _LOT_ROW, 6, _EOF_NOT_LAST),
    ("eof-then-bos", _BOF + _SECTIONS + _EOF + _BOS_POST, 6, _EOF_NOT_LAST),
    ("two-eof", _BOF + _SECTIONS + _EOF + _EOF, 6, _EOF_NOT_LAST),
    ("eof-alone", _BOF + _SECTIONS + _row("EOF"), 5, _EOF_NOT_LAST),
    ("eof-3-fields", _BOF + _SECTIONS + _row("EOF", "U12345678", "x"), 5, _EOF_NOT_LAST),
    ("eof-trailing-blank", _BOF + _SECTIONS + _row("EOF", "U12345678", ""), 5, _EOF_NOT_LAST),
    ("bof-7-fields", _row(*_BOF_8[:7]) + _SECTIONS + _EOF, 0, _BOF_FIELDS),
    ("bof-9-fields", _row(*_BOF_8, "100") + _SECTIONS + _EOF, 0, _BOF_FIELDS),
    ("bof-9-fields-trailing-blank", _row(*_BOF_8, "") + _SECTIONS + _EOF, 0, _BOF_FIELDS),
    ("bof-alone", _row("BOF") + _SECTIONS + _EOF, 0, _BOF_FIELDS),
    ("from-iso", bof_record("2025-01-01") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("to-iso", bof_record(end="2025-12-31") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-us", bof_record("01/01/2025") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("to-month-name", bof_record(end="31-Dec-25") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-with-time", bof_record("20250101;000000") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-7-digits", bof_record("2025011") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("to-9-digits", bof_record(end="202512310") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-not-a-day", bof_record("20250230") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("to-month-13", bof_record(end="20251331") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-year-zero", bof_record("00000101") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-blank", bof_record("") + _SECTIONS + _EOF, 0, _BOF_DATE),
    ("from-padded", bof_record(" 20250101") + _SECTIONS + _EOF, 0, _BOF_DATE),
    (
        "from-wide-digits",
        bof_record("".join(chr(0xFF10 + int(digit)) for digit in "20250101")) + _SECTIONS + _EOF,
        0,
        _BOF_DATE,
    ),
    (
        "from-arabic-indic",
        bof_record("".join(chr(0x0660 + int(digit)) for digit in "20250101")) + _SECTIONS + _EOF,
        0,
        _BOF_DATE,
    ),
    ("from-after-to", bof_record("20251231", "20250101") + _SECTIONS + _EOF, 0, _BOF_REVERSED),
    ("boa-without-bof", _BOA + _SECTIONS, 0, _ACCOUNT_RECORD),
    ("eoa-without-bof", _SECTIONS + _EOA, 4, _ACCOUNT_RECORD),
    ("boa-after-eof", _BOF + _SECTIONS + _EOF + _BOA, 6, _ACCOUNT_RECORD),
    ("eoa-after-eof", _BOF + _SECTIONS + _EOF + _EOA, 6, _ACCOUNT_RECORD),
    ("unnamed-record-after-bof", _BOF + _row("XOF", "x") + _SECTIONS + _EOF, 1, _UNNAMED),
    ("data-row-after-eos", _BOF + _ACCOUNT + _EOS_ACCT + _ACCOUNT_ROW + _EOF, 4, _UNNAMED),
    ("data-row-after-bos", _BOF + _BOS_ACCT + _ACCOUNT_ROW + _EOF, 2, _UNNAMED),
    ("data-row-after-boa", _BOF + _ACCOUNT + _BOA + _ACCOUNT_ROW + _EOF, 4, _UNNAMED),
    (
        "unlisted-section-code",
        _BOF + _row("ZZZZ", "BOS", "Sample") + _SECTIONS + _EOF,
        1,
        _UNNAMED,
    ),
    (
        "line-descriptor-first",
        _BOF + _row("BOS", "ACCT", "Account") + _SECTIONS + _EOF,
        1,
        _UNNAMED,
    ),
    (
        "section-code-prefixed-header",
        _BOF + _BOS_ACCT + _row("ACCT", "HEADER", "ClientAccountID") + _EOF,
        2,
        _UNNAMED,
    ),
    ("bos-first-in-known-section", _BOF + _ACCOUNT + _row("BOS", "ACCT") + _EOF, 3, _UNNAMED),
    ("eos-first-in-known-section", _BOF + _ACCOUNT + _row("EOS", "ACCT") + _EOF, 3, _UNNAMED),
    *[
        (
            f"{case}-in-unknown-section",
            _BOF + _SECTIONS + _row("CurrencyPrimary", "SampleField") + record + _EOF,
            6,
            _UNNAMED,
        )
        for case, record in (
            ("bos-first", _row("BOS", "ACCT")),
            ("eos-first", _row("EOS", "ACCT")),
            ("unlisted-bos", _row("ZZZZ", "BOS")),
            ("unlisted-eos", _row("ZZZZ", "EOS")),
        )
    ],
]


@pytest.mark.parametrize(
    ("text", "line", "rule"),
    [pytest.param(text, line, rule, id=case) for case, text, line, rule in _LAYOUT_CASES],
)
def test_a_record_that_breaks_the_layout_refuses(
    tmp_path: Path, text: str, line: int, rule: str
) -> None:
    with pytest.raises(ParseError) as raised:
        split_flex_query(_write(tmp_path, text))

    expected = _layout_refusal(line, rule)
    assert_unchained_refusal(raised.value, expected, str(expected), markers=())


@pytest.mark.parametrize(("case", "text", "line", "rule"), _LAYOUT_CASES[:3])
def test_split_sections_refuses_the_same_layout(
    tmp_path: Path, case: str, text: str, line: int, rule: str
) -> None:
    """``split_sections`` reads the records too; it only drops the period."""
    with pytest.raises(ParseError) as raised:
        split_sections(_write(tmp_path, text))

    assert raised.value == _layout_refusal(line, rule), case


def test_a_record_inside_a_section_ends_it(tmp_path: Path) -> None:
    """``EOA`` in the middle of a section: the next data row is outside one."""
    text = _BOF + _OPEN_POSITIONS + _EOA + _LOT_ROW + _EOF

    with pytest.raises(ParseError) as raised:
        split_flex_query(_write(tmp_path, text))

    assert raised.value == _layout_refusal(4, _UNNAMED)


def test_an_existing_splitter_refusal_comes_before_a_later_record(tmp_path: Path) -> None:
    """The records are checked as they are read: a near-miss header earlier in
    the file keeps its refusal."""
    near_miss = _OPEN_POSITIONS.replace('"MarkPrice",', "", 1)
    with pytest.raises(ParseError) as raised:
        split_flex_query(_write(tmp_path, _ACCOUNT + near_miss + _BOF))

    assert raised.value.section == "<file>"
    assert raised.value.row_index == 2
    assert "looks like a changed 'Open Positions (LOT)' section header" in raised.value.message


# ---- format detection --------------------------------------------------------


def test_a_file_starting_with_a_bof_record_is_a_flex_query(tmp_path: Path) -> None:
    assert detect_ibkr_csv_format(_write(tmp_path, _BOF + _SECTIONS + _EOF)) is (
        IbkrCsvFormat.FLEX_QUERY
    )


@pytest.mark.parametrize("text", [_ACCOUNT, _OPEN_POSITIONS], ids=["account", "currency"])
def test_a_flex_query_without_records_is_still_detected(tmp_path: Path, text: str) -> None:
    assert detect_ibkr_csv_format(_write(tmp_path, text)) is IbkrCsvFormat.FLEX_QUERY


@pytest.mark.parametrize("first", [_BOA, _EOF, _BOS_ACCT], ids=["boa", "eof", "bos"])
def test_a_file_starting_with_another_record_is_not_detected(tmp_path: Path, first: str) -> None:
    path = _write(tmp_path, first + _SECTIONS)
    with pytest.raises(ParseError) as raised:
        detect_ibkr_csv_format(path)

    assert raised.value.message == (
        f"input CSV {path}'s first non-blank record doesn't match a known IBKR export "
        "format. Expected one of:\n  * Activity Statement: first two cells ('Statement', "
        "'Header'); or\n  * Flex Query: first cell in ['BOF', 'ClientAccountID', "
        "'CurrencyPrimary']."
    )


# ---- no echo ---------------------------------------------------------------


def test_late_non_utf8_text_keeps_no_record_cell(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as raised:
        split_flex_query(_write(tmp_path, late_non_utf8_records(_SECTIONS)))

    expected = utf8_refusal()
    assert_unchained_refusal(raised.value, expected, str(expected), markers=_MARKERS)


@pytest.mark.parametrize(
    ("text", "line", "rule"),
    [
        pytest.param(marked_bof(_BAD_DATE) + _SECTIONS + marked_eof(), 0, _BOF_DATE, id="from"),
        pytest.param(marked_bof(end=_BAD_DATE) + _SECTIONS + marked_eof(), 0, _BOF_DATE, id="to"),
        pytest.param(
            marked_bof() + marked_bof() + _SECTIONS + marked_eof(), 1, _BOF_REPEATED, id="twice"
        ),
        pytest.param(
            marked_bof() + _SECTIONS + marked_eof() + marked_eof(), 6, _EOF_NOT_LAST, id="eof"
        ),
        pytest.param(
            marked_bof().replace('"\n', '","1"\n') + _SECTIONS + marked_eof(),
            0,
            _BOF_FIELDS,
            id="fields",
        ),
    ],
)
def test_a_layout_refusal_prints_no_record_cell(
    tmp_path: Path, text: str, line: int, rule: str
) -> None:
    with pytest.raises(ParseError) as raised:
        split_flex_query(_write(tmp_path, text))

    expected = _layout_refusal(line, rule)
    assert_unchained_refusal(raised.value, expected, str(expected), markers=_MARKERS)


def test_the_period_keeps_no_record_cell(tmp_path: Path) -> None:
    flex = split_flex_query(_write(tmp_path, marked_bof() + _SECTIONS + marked_eof()))

    assert flex.period == FlexPeriod(date(2025, 1, 1), date(2025, 12, 31), line=0)
    for marker in _MARKERS:
        assert marker not in repr(flex)
