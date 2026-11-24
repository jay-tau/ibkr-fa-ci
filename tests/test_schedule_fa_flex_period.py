"""Schedule FA reads a Flex Query only if its ``BOF`` record covers the calendar year (#1313).

A Flex Query CSV states its period only in IBKR's header and trailer
records, so a query run over part of the year could leave out a sale and
Schedule FA a holding held during the year. Every file here is synthetic: the
issue's examples 1 to 3, the period bounds, the ``CLOSED_LOT`` dates and the
existing refusals, through ``run_fa_a3`` and the ``schedule-fa`` CLI.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.compute.lot_matching import BrokerBasisError
from ibkr_fa.compute.source_coverage import require_flex_calendar_year_period
from ibkr_fa.exceptions import ConfigError, IbkrFaError, ParseError
from ibkr_fa.models.source import FlexPeriod
from ibkr_fa.pipeline_fa import run_fa_a3
from tests._crash_reports import assert_no_markers, assert_unchained_refusal
from tests._flex_records import BOF_DATE_RULE as _BOF_DATE
from tests._flex_records import FLEX_RECORD_MARKERS as _MARKERS
from tests._flex_records import (
    bof_record,
    eof_record,
    late_non_utf8_records,
    marked_bof,
    marked_eof,
    utf8_refusal,
)
from tests._flex_records import flex_layout_refusal as _layout_refusal
from tests._flex_section_rows import OPEN_POSITIONS_HEADER, TRADES_LOT_DETAIL_HEADER
from tests._ibkr_pipeline_inputs import PIPELINE_ACCOUNT_INFO_SECTION, write_pipeline_user_toml

_OPEN_HEADER = OPEN_POSITIONS_HEADER + "\n"
_TRADES_HEADER = TRADES_LOT_DETAIL_HEADER + "\n"


def _lot(report_date: str = "20251231", opened: str = "20250210") -> str:
    """The open ``XQAA`` lot: 5 shares bought at $30, marked at $40."""
    return (
        '"USD","1","STK","COMMON","XQAA","SAMPLE ISSUER AA INC","99990A309","US99990A3095",'
        f'"NASDAQ","US","{report_date}","5","40","200","30","150","Long","LOT",'
        f'"{opened}","{opened}"\n'
    )


def _sale(*, opened: str, sold: str, closed: str = "6", side: str = "SELL") -> str:
    """A closing execution of 6 ``XQBB`` shares at $50 on ``sold`` and its
    CLOSED_LOT row, bought on ``opened`` for $270 (``closed`` shares)."""
    return (
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044",'
        f'"NASDAQ","US","{sold}","{sold}","{sold}","ExchTrade","-6","50","-300","300","0",'
        f'"-1","USD","299","50","C","-270","29","SELL","EXECUTION","",""\n'
        '"USD","1","STK","COMMON","XQBB","SAMPLE ISSUER BB INC","99995B104","US99995B1044",'
        f'"NASDAQ","US","{sold}","{sold}","","","{closed}","45","","","","","","","","C",'
        f'"270","29","{side}","CLOSED_LOT","{opened};101500","{opened};101500"\n'
    )


# Example 1: one open XQAA lot and a Trades section with a header and no rows.
_EXAMPLE_1 = PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _OPEN_HEADER + _lot() + "\n" + _TRADES_HEADER
# Example 1 with XQBB sold on 2025-03-14: two Table A3 rows.
_SOLD_IN_MARCH = _sale(opened="20241104", sold="20250314")
_EXAMPLE_1_SALE = _EXAMPLE_1 + _SOLD_IN_MARCH
# Example 2: nothing open, and XQBB bought 2025-03-03 and sold 2026-01-15.
_EXAMPLE_2 = (
    PIPELINE_ACCOUNT_INFO_SECTION
    + "\n"
    + _OPEN_HEADER
    + "\n"
    + _TRADES_HEADER
    + _sale(opened="20250303", sold="20260115")
)

_FORMATS = {
    "review-csv": ("csv", ()),
    "filing-csv": ("csv", ()),
    "xlsx": ("xlsx", ()),
    "cbdt-json-fragment": ("json", ("--itr-form", "ITR-2")),
}

# Main's (f4acf4c) review and filing CSVs for example 1 with the sale, without
# records: example 3 must write the same bytes.
_MAIN_EXAMPLE_1_SALE_SHA256 = {
    "review-csv": "6572dff35c7ddc810e035657d2f7e35a37bb79e0313bc54f4fd5618facd10a7b",
    "filing-csv": "3deeebf26bd5dbe601774f2be3cd3f7c791ac2301999a66238900f2c1d4a1b25",
}
# Main's (f4acf4c) CSVs for tests/fixtures/e2e_year_end_2025.csv without its
# records, which the fixture now has: the same bytes.
_E2E_FIXTURE = Path(__file__).parent / "fixtures" / "e2e_year_end_2025.csv"
_MAIN_E2E_FIXTURE_SHA256 = {
    "review-csv": "cdfba44661eafa2144a02235f3ffba1228217e42aef062160a61d3fe16255196",
    "filing-csv": "50a7ea647c3616e0e54c333e7904ee4531b735584112276eb9d41a932612effc",
}


def _records(text: str, start: str = "20250101", end: str = "20251231") -> str:
    return bof_record(start, end) + text + eof_record()


def _write(tmp_path: Path, text: str | bytes, *, account: str = "U12345678") -> tuple[Path, Path]:
    flex = tmp_path / "flex.csv"
    if isinstance(text, bytes):
        flex.write_bytes(text)
    else:
        flex.write_text(text, encoding="utf-8")
    config = write_pipeline_user_toml(tmp_path, account_number=account)
    return flex, config


def _run(
    tmp_path: Path, text: str | bytes, *, calendar_year: int = 2025, account: str = "U12345678"
):
    flex, config = _write(tmp_path, text, account=account)
    return run_fa_a3(
        input_path=flex,
        config_path=config,
        output_path=tmp_path / "review.csv",
        calendar_year=calendar_year,
        fetch_prices=False,
    )


def _cli(tmp_path: Path, text: str | bytes, fmt: str = "review-csv") -> tuple[int, Path]:
    flex, config = _write(tmp_path, text)
    suffix, extra = _FORMATS[fmt]
    output = tmp_path / f"schedule-fa.{suffix}"
    code = main(
        [
            "schedule-fa",
            *("--input", str(flex), "--config", str(config), "--output", str(output)),
            *("--calendar-year", "2025", "--format", fmt, "--no-fetch-prices", *extra),
        ]
    )
    return code, output


def _no_records(calendar_year: int = 2025) -> ParseError:
    return ParseError(
        section="<file>",
        row_index=0,
        column=None,
        message=(
            "this Flex Query has no header and trailer records, so the dates it covers can't "
            "be checked, and Schedule FA could leave out a holding sold outside them. Turn on "
            "'Include header and trailer records' in the Flex Query's delivery settings and "
            f"run it from January 1 to December 31, {calendar_year}, or use an Activity "
            "Statement for the year instead."
        ),
        raw_value="",
    )


def _period(calendar_year: int = 2025, line: int = 0) -> ParseError:
    return ParseError(
        section="<file>",
        row_index=line,
        column=None,
        message=(
            f"the Flex Query's header record must show a period starting on or before Jan 1, "
            f"{calendar_year} and ending on Dec 31, {calendar_year}. Schedule FA lists every "
            "holding held during the calendar year; re-run the Flex Query over that period."
        ),
        raw_value="",
    )


def _trade_date(row_index: int) -> ParseError:
    return ParseError(
        section="Trades",
        row_index=row_index,
        column="TradeDate",
        message=(
            "this CLOSED_LOT row's TradeDate is outside the period in the Flex Query's header "
            "record; re-export the Flex Query."
        ),
        raw_value="",
    )


def _refuses(tmp_path: Path, text: str, expected: ParseError, **kwargs: object) -> None:
    with pytest.raises(ParseError) as raised:
        _run(tmp_path, text, **kwargs)  # ty: ignore[invalid-argument-type]
    assert_unchained_refusal(raised.value, expected, str(expected), markers=())
    assert not (tmp_path / "review.csv").exists()


def _a3_symbols(tmp_path: Path) -> list[str]:
    text = (tmp_path / "review.csv").read_text(encoding="utf-8")
    return [symbol for symbol in ("XQAA", "XQBB") if symbol in text]


# ---- examples 1 and 2: no records ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [_EXAMPLE_1, _EXAMPLE_1_SALE, _EXAMPLE_2],
    ids=["example-1", "example-1-sale", "example-2"],
)
def test_a_flex_query_without_records_refuses(tmp_path: Path, text: str) -> None:
    _refuses(tmp_path, text, _no_records())


@pytest.mark.parametrize("fmt", list(_FORMATS))
@pytest.mark.parametrize("text", [_EXAMPLE_1_SALE, _EXAMPLE_2], ids=["example-1", "example-2"])
def test_schedule_fa_refuses_a_flex_query_without_records_in_every_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str, fmt: str
) -> None:
    code, output = _cli(tmp_path, text, fmt)

    assert code == 4
    assert capsys.readouterr().err == f"error: {_no_records()}\n"
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["flex.csv", "user.toml"]


# ---- records with a period that isn't the calendar year --------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [("20250701", "20251231"), ("20250201", "20260131")],
    ids=["from-july", "february-to-january"],
)
@pytest.mark.parametrize("text", [_EXAMPLE_1_SALE, _EXAMPLE_2], ids=["example-1", "example-2"])
def test_a_period_short_of_the_year_refuses(
    tmp_path: Path, text: str, start: str, end: str
) -> None:
    _refuses(tmp_path, _records(text, start, end), _period())


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param("20250102", "20251231", id="from-jan-2"),
        pytest.param("20250101", "20251230", id="to-dec-30"),
        pytest.param("20250101", "20260101", id="to-next-jan-1"),
        pytest.param("20240101", "20241231", id="the-year-before"),
    ],
)
def test_a_period_bound_outside_the_rule_refuses(tmp_path: Path, start: str, end: str) -> None:
    _refuses(tmp_path, _records(_EXAMPLE_1_SALE, start, end), _period())


def test_the_period_refusal_reports_the_bof_line(tmp_path: Path) -> None:
    _refuses(tmp_path, "\n\n" + _records(_EXAMPLE_1_SALE, "20250701"), _period(line=2))


def test_a_leap_year_flex_query_of_365_days_refuses(tmp_path: Path) -> None:
    """IBKR's Flex Web Service caps a date override at 365 days, one short of 2024."""
    text = (
        PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + _OPEN_HEADER
        + _lot("20241231", "20240212")
        + "\n"
        + _TRADES_HEADER
    )
    _refuses(tmp_path, _records(text, "20240102", "20241231"), _period(2024), calendar_year=2024)


@pytest.mark.parametrize(
    "period",
    [
        FlexPeriod(date(2025, 1, 1), date(2025, 12, 31), line=0),
        FlexPeriod(date(2024, 12, 31), date(2025, 12, 31), line=0),
        FlexPeriod(date(2020, 1, 1), date(2025, 12, 31), line=0),
        FlexPeriod(date(2024, 1, 1), date(2024, 12, 31), line=0),
    ],
    ids=["jan-1", "dec-31-before", "years-before", "leap-year"],
)
def test_the_period_check_accepts_a_start_on_or_before_jan_1(period: FlexPeriod) -> None:
    require_flex_calendar_year_period(period, calendar_year=period.end_date.year)


# ---- example 3: the documented records -------------------------------------


@pytest.mark.parametrize("start", ["20250101", "20241231", "20200101"])
def test_records_covering_the_year_file_both_rows(tmp_path: Path, start: str) -> None:
    report = _run(tmp_path, _records(_EXAMPLE_1_SALE, start))

    assert report.rows_written == 2
    assert _a3_symbols(tmp_path) == ["XQAA", "XQBB"]


@pytest.mark.parametrize("fmt", list(_MAIN_EXAMPLE_1_SALE_SHA256))
def test_example_3_writes_the_bytes_main_writes_without_records(tmp_path: Path, fmt: str) -> None:
    code, output = _cli(tmp_path, _records(_EXAMPLE_1_SALE), fmt)

    assert code == 3
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert digest == _MAIN_EXAMPLE_1_SALE_SHA256[fmt]


@pytest.mark.parametrize("descriptor", ["BOS", "EOS"])
def test_a_descriptor_in_an_unread_data_cell_keeps_the_existing_result(
    tmp_path: Path, descriptor: str
) -> None:
    """An unused FXRateToBase cell is data, not a section-code/descriptor record."""
    text = _EXAMPLE_1_SALE.replace('"USD","1","STK"', f'"USD","{descriptor}","STK"')
    assert text != _EXAMPLE_1_SALE
    code, output = _cli(tmp_path, _records(text))

    assert code == 3
    assert (
        hashlib.sha256(output.read_bytes()).hexdigest() == _MAIN_EXAMPLE_1_SALE_SHA256["review-csv"]
    )


@pytest.mark.parametrize("fmt", list(_MAIN_E2E_FIXTURE_SHA256))
def test_the_e2e_fixture_with_records_writes_main_s_bytes(tmp_path: Path, fmt: str) -> None:
    text = _E2E_FIXTURE.read_text(encoding="utf-8")
    assert text.startswith('"BOF",') and text.splitlines()[-1].startswith('"EOF",')
    code, output = _cli(tmp_path, text, fmt)

    assert code == 3
    assert hashlib.sha256(output.read_bytes()).hexdigest() == _MAIN_E2E_FIXTURE_SHA256[fmt]


# ---- CLOSED_LOT dates --------------------------------------------------------


def _sold_on(sold: str) -> str:
    return _records(_EXAMPLE_1 + _sale(opened="20241104", sold=sold), "20241201")


@pytest.mark.parametrize("sold", ["20241130", "20260101"], ids=["day-before-from", "day-after-to"])
def test_a_closed_lot_outside_the_period_refuses(tmp_path: Path, sold: str) -> None:
    _refuses(tmp_path, _sold_on(sold), _trade_date(1))


@pytest.mark.parametrize(
    ("sold", "symbols"),
    [("20241201", ["XQAA"]), ("20251231", ["XQAA", "XQBB"])],
    ids=["on-from", "on-to"],
)
def test_a_closed_lot_on_a_period_bound_passes(
    tmp_path: Path, sold: str, symbols: list[str]
) -> None:
    """A lot sold on the from date, before the year, is filtered out as before."""
    _run(tmp_path, _sold_on(sold))

    assert _a3_symbols(tmp_path) == symbols


def test_the_trade_date_refusal_names_the_closed_lot_row(tmp_path: Path) -> None:
    """The second sale's CLOSED_LOT row is row 3 of the Trades section."""
    sales = _sale(opened="20241104", sold="20250314") + _sale(opened="20241104", sold="20260102")
    _refuses(tmp_path, _records(_EXAMPLE_1 + sales), _trade_date(3))


def test_the_period_refusal_precedes_the_trade_date_refusal(tmp_path: Path) -> None:
    _refuses(tmp_path, _records(_EXAMPLE_2, "20250701"), _period())


# ---- existing refusals keep their place ------------------------------------

_NEAR_MISS_HEADER = _OPEN_HEADER.replace('"MarkPrice",', "")
_OPEN_ROW = _lot()

# Inputs main refuses in the parser layer, without records.
_PARSER_REFUSALS = {
    "malformed-record": _EXAMPLE_1_SALE + '"USD","unclosed\n',
    "near-miss-header": (
        PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _NEAR_MISS_HEADER + "\n" + _TRADES_HEADER
    ),
    "near-miss-with-descriptor-cell": PIPELINE_ACCOUNT_INFO_SECTION
    + "\n"
    + _OPEN_HEADER.replace('"FXRateToBase"', '"BOS"')
    + "\n"
    + _TRADES_HEADER,
    "repeated-section": (
        PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + _OPEN_HEADER
        + _OPEN_ROW
    ),
    "repeated-section-then-near-miss": (
        PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + _NEAR_MISS_HEADER
    ),
    "field-count": (
        PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _OPEN_HEADER + _OPEN_ROW.replace("\n", ',"x"\n')
    ),
    "missing-open-positions": PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _TRADES_HEADER,
    "blank-account": (
        PIPELINE_ACCOUNT_INFO_SECTION.replace('"U12345678"', '""') + "\n" + _OPEN_HEADER + _OPEN_ROW
    ),
    "open-lot": (
        PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _OPEN_HEADER + _OPEN_ROW.replace('"USD"', '"EUR"', 1)
    ),
    "snapshot": (
        PIPELINE_ACCOUNT_INFO_SECTION
        + "\n"
        + _OPEN_HEADER
        + _lot("20251130")
        + "\n"
        + _TRADES_HEADER
    ),
    "missing-trades": PIPELINE_ACCOUNT_INFO_SECTION + "\n" + _OPEN_HEADER + _OPEN_ROW,
    "closed-lot": _EXAMPLE_1 + _sale(opened="20241104", sold="20250314", side="BUY"),
    "closing-execution": _EXAMPLE_1 + _sale(opened="20241104", sold="20250314", closed="5"),
}


def _parse_refusal(tmp_path: Path, text: str) -> ParseError:
    with pytest.raises(ParseError) as raised:
        _run(tmp_path, text)
    return raised.value


def _with_records(text: str, *, wrap_sections: bool) -> tuple[str, list[int]]:
    """``text`` between ``BOF`` and ``EOF`` records and, with ``wrap_sections``,
    each blank-line-separated block between ``BOS`` and ``EOS`` records; and
    the new zero-based line of each of ``text``'s lines."""
    out = [bof_record()]
    lines: list[int] = []
    in_block = False
    for line in text.splitlines(keepends=True):
        if wrap_sections and line.strip() and not in_block:
            out.append('"TRNT","BOS","Trades"\n')
        elif wrap_sections and not line.strip() and in_block:
            out.append('"TRNT","EOS","0","0"\n')
        in_block = bool(line.strip())
        lines.append(len(out))
        out.append(line)
    if wrap_sections and in_block:
        out.append('"TRNT","EOS","0","0"\n')
    return "".join(out) + eof_record(), lines


@pytest.mark.parametrize("wrap_sections", [False, True], ids=["bof-eof", "every-record"])
@pytest.mark.parametrize("text", list(_PARSER_REFUSALS.values()), ids=list(_PARSER_REFUSALS))
def test_a_parser_refusal_is_the_same_with_records(
    tmp_path: Path, text: str, wrap_sections: bool
) -> None:
    """With records added, the parser refuses as without them; a ``<file>``
    refusal names the same record on its new line."""
    without = _parse_refusal(tmp_path, text)
    assert without != _no_records()
    records, lines = _with_records(text, wrap_sections=wrap_sections)
    with_records = _parse_refusal(tmp_path, records)

    if without.section == "<file>":
        line = without.row_index
        without = dataclasses.replace(
            without,
            row_index=lines[line],
            message=without.message.replace(f"line {line + 1} ", f"line {lines[line] + 1} "),
        )
    assert with_records == without
    assert str(with_records) == str(without)


_SHIFTED = _EXAMPLE_1 + _sale(opened="20241104", sold="20250314").replace(
    '"20241104;101500"\n', '"20241010;101500"\n'
)


@pytest.mark.parametrize(
    ("text", "account", "error"),
    [
        pytest.param(_EXAMPLE_1_SALE, "U99999999", ConfigError, id="account-number"),
        pytest.param(_SHIFTED, "U12345678", BrokerBasisError, id="shifted-holding-date"),
    ],
)
def test_a_later_refusal_needs_the_records_first(
    tmp_path: Path, text: str, account: str, error: type[IbkrFaError]
) -> None:
    """Without records the input refuses for them; with them it refuses as before."""
    _refuses(tmp_path, text, _no_records(), account=account)
    with pytest.raises(error):
        _run(tmp_path, _records(text), account=account)


# ---- no echo ----------------------------------------------------------------


def test_late_non_utf8_record_cells_never_reach_the_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = late_non_utf8_records(_EXAMPLE_1_SALE)
    expected = utf8_refusal()
    with pytest.raises(ParseError) as raised:
        _run(tmp_path, text)
    assert_unchained_refusal(raised.value, expected, str(expected), markers=_MARKERS)

    code, output = _cli(tmp_path, text)
    assert code == 4
    assert not output.exists()
    captured = capsys.readouterr()
    assert captured == ("", f"error: {expected}\n")
    assert_no_markers(captured.out + captured.err, *_MARKERS)


def test_record_cells_never_reach_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = _cli(tmp_path, marked_bof() + _EXAMPLE_1_SALE + marked_eof())

    assert code == 3
    captured = capsys.readouterr()
    for text in (captured.out, captured.err, output.read_text(encoding="utf-8")):
        assert_no_markers(text, *_MARKERS)


@pytest.mark.parametrize(
    ("bof", "expected"),
    [
        pytest.param(marked_bof("47182936"), _layout_refusal(0, _BOF_DATE), id="malformed-date"),
        pytest.param(marked_bof("20250701"), _period(), id="short-period"),
    ],
)
def test_a_refusal_prints_no_record_cell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bof: str, expected: ParseError
) -> None:
    text = bof + _EXAMPLE_1_SALE + marked_eof()
    with pytest.raises(ParseError) as raised:
        _run(tmp_path, text)
    assert_unchained_refusal(raised.value, expected, str(expected), markers=_MARKERS)

    code, _output = _cli(tmp_path, text)
    assert code == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {expected}\n"
    assert_no_markers(captured.out + captured.err, *_MARKERS)
