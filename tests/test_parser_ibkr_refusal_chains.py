"""IBKR parser refusals keep no input in their exception graph (#1226).

Each refusal here is raised after its handler has ended. One that replaced a
caught error chains only a content-free ``ReplacedCrashError`` stand-in naming
that error's class, so ``IBKR_FA_TRACEBACK=1`` still re-raises it; the
issuer-country refusal chains nothing. On 2c018e3 these refusals kept the
caught error, which held input: an unknown section name, Side text, a Period
month, an issuer country code and, on Python 3.14, the year of an invalid date
or the hour of an invalid time.

Each direct refusal test checks, in order: the unchanged class, fields and
complete message; no input marker anywhere in the exception graph (fields,
args, ``str``, ``repr``, rendered traces, every cause and context, suppressed
or not); then no context and, at most, the stand-in as its cause. A calendar
or clock marker can only be printed on 3.14, so the structural check guards those
sites on older runtimes. The tests also assert empty args.

The inputs are module constants because a rendered traceback prints the line
that calls the parser. A calendar marker is a year from 9201 up, which no
traceback line number can be; a clock marker is the whole time cell plus 3.14's
``not <hour>`` rendering, since a two-digit hour alone is too short to search.
Every row here is synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import (
    Dividend,
    InterestCredit,
    InterestNonCredit,
    StatementPeriod,
    WithholdingTax,
)
from ibkr_fa.parser import parse_closed_lots, parse_open_positions
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    build_instrument_lookup,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_trades,
    parse_activity_statement_withholding_tax,
    parse_statement_period,
    split_activity_statement_sections,
)
from tests._activity_statement_rows import (
    make_dividend_row,
    make_fii_row,
    make_interest_row,
    make_trade_row,
    make_wht_row,
)
from tests._activity_statement_rows import write_statement_lines as write
from tests._crash_reports import assert_unchained_refusal
from tests._flex_lot_rows import FLEX_CLOSED_LOT_XQAT as _CLOSED_LOT
from tests._flex_lot_rows import FLEX_OPEN_LOT_XQAW as _OPEN_LOT
from tests._flex_lot_rows import with_closing_executions
from tests._ibkr_flex_cli_inputs import CLI_OPEN_POSITIONS_SECTION as _OPEN_POSITIONS_SECTION
from tests._ibkr_pipeline_inputs import PIPELINE_ACCOUNT_INFO_SECTION as _ACCOUNT_INFO_SECTION
from tests._ibkr_pipeline_inputs import write_pipeline_flex_csv as write_flex_csv
from tests._ibkr_pipeline_inputs import write_pipeline_user_toml as write_user_toml
from tests._income_blocks import block, closed
from tests._issuer_country_refusal import UNMAPPED_COUNTRY_RULE as _UNMAPPED
from tests._statement_period import statement_field_row as make_row

_OPEN = "Open Positions (LOT)"
_CLOSED = "Trades"
_CALENDAR = "must be a real calendar date"
_FLEX_CLOCK = "must have a real wall-clock time in HHMMSS"
_SIDE = "must be one of: Long, Short"

type Rows = list[dict[str, str]]

_OPEN_ROW = "ParseError in section 'Open Positions (LOT)' at row 1, column "
_CLOSED_ROW = "ParseError in section 'Trades' at row 1, column "
_PERIOD_2028 = StatementPeriod(start_date=date(2028, 1, 1), end_date=date(2028, 12, 31))
_TRADES_2025 = StatementPeriod(start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))


# --- Flex dates: parser/_dates.py ----------------------------------------------

_OPEN_REPORT_YEAR = "9201"
_OPEN_OPENED_YEAR = "9202"
_CLOSED_TRADE_YEAR = "9203"
_CLOSED_OPENED_YEAR = "9204"
_CLOSED_BOTH_YEAR = "9205"
_FIRST_ROW_YEAR = "9206"
_SECOND_ROW_YEAR = "9207"
# Clock markers: the whole time cell and 3.14's rendering of its hour.
_FLEX_TIME = "874559"
_FLEX_HOUR = "not 87"
_FLEX_BOTH_TIME = "884559"


def _open_rows(*cells: dict[str, str]) -> Rows:
    """A valid lot, then one lot per ``cells`` with those cells changed."""
    return [_OPEN_LOT, *({**_OPEN_LOT, **changed} for changed in cells)]


def _closed_rows(*cells: dict[str, str]) -> Rows:
    """A valid closed lot, then one per ``cells`` with those cells changed."""
    return [_CLOSED_LOT, *({**_CLOSED_LOT, **changed} for changed in cells)]


_FLEX_DATE_REFUSALS = [
    pytest.param(
        parse_open_positions,
        _open_rows({"ReportDate": f"{_OPEN_REPORT_YEAR}0230"}),
        ParseError(
            section=_OPEN, row_index=1, column="ReportDate", message=_CALENDAR, raw_value=""
        ),
        f"{_OPEN_ROW}'ReportDate': {_CALENDAR}",
        (_OPEN_REPORT_YEAR,),
        id="open-report-date-calendar",
    ),
    pytest.param(
        parse_open_positions,
        _open_rows({"OpenDateTime": f"{_OPEN_OPENED_YEAR}0230"}),
        ParseError(
            section=_OPEN, row_index=1, column="OpenDateTime", message=_CALENDAR, raw_value=""
        ),
        f"{_OPEN_ROW}'OpenDateTime': {_CALENDAR}",
        (_OPEN_OPENED_YEAR,),
        id="open-open-date-calendar",
    ),
    pytest.param(
        parse_closed_lots,
        _closed_rows({"TradeDate": f"{_CLOSED_TRADE_YEAR}0230"}),
        ParseError(
            section=_CLOSED, row_index=1, column="TradeDate", message=_CALENDAR, raw_value=""
        ),
        f"{_CLOSED_ROW}'TradeDate': {_CALENDAR}",
        (_CLOSED_TRADE_YEAR,),
        id="closed-trade-date-calendar",
    ),
    pytest.param(
        parse_closed_lots,
        _closed_rows({"OpenDateTime": f"{_CLOSED_OPENED_YEAR}0230;101500"}),
        ParseError(
            section=_CLOSED, row_index=1, column="OpenDateTime", message=_CALENDAR, raw_value=""
        ),
        f"{_CLOSED_ROW}'OpenDateTime': {_CALENDAR}",
        (_CLOSED_OPENED_YEAR,),
        id="closed-open-date-time-calendar",
    ),
    pytest.param(
        parse_closed_lots,
        _closed_rows({"OpenDateTime": f"20250311;{_FLEX_TIME}"}),
        ParseError(
            section=_CLOSED, row_index=1, column="OpenDateTime", message=_FLEX_CLOCK, raw_value=""
        ),
        f"{_CLOSED_ROW}'OpenDateTime': {_FLEX_CLOCK}",
        (_FLEX_TIME, _FLEX_HOUR),
        id="closed-open-date-time-clock",
    ),
    # Priority: the date part refuses before the time part of the same cell.
    pytest.param(
        parse_closed_lots,
        _closed_rows({"OpenDateTime": f"{_CLOSED_BOTH_YEAR}0230;{_FLEX_BOTH_TIME}"}),
        ParseError(
            section=_CLOSED, row_index=1, column="OpenDateTime", message=_CALENDAR, raw_value=""
        ),
        f"{_CLOSED_ROW}'OpenDateTime': {_CALENDAR}",
        (_CLOSED_BOTH_YEAR,),
        id="closed-bad-date-and-time-refuses-on-the-date",
    ),
    # Priority: the first bad row refuses before a later one.
    pytest.param(
        parse_open_positions,
        _open_rows(
            {"ReportDate": f"{_FIRST_ROW_YEAR}0230"}, {"ReportDate": f"{_SECOND_ROW_YEAR}0230"}
        ),
        ParseError(
            section=_OPEN, row_index=1, column="ReportDate", message=_CALENDAR, raw_value=""
        ),
        f"{_OPEN_ROW}'ReportDate': {_CALENDAR}",
        (_FIRST_ROW_YEAR,),
        id="open-two-bad-rows-refuse-on-the-first",
    ),
]


@pytest.mark.parametrize(("parse", "rows", "expected", "text", "markers"), _FLEX_DATE_REFUSALS)
def test_flex_date_refusal_chains_only_a_stand_in(
    parse: Callable[[Rows], object],
    rows: Rows,
    expected: ParseError,
    text: str,
    markers: tuple[str, ...],
) -> None:
    with pytest.raises(ParseError) as caught:
        parse(rows)
    assert_unchained_refusal(caught.value, expected, text=text, markers=markers, crash=ValueError)
    assert caught.value.args == ()


_OPEN_LEAP_DAY = {
    **_OPEN_LOT,
    "ReportDate": "20280229",
    "OpenDateTime": "20280229",
    "HoldingPeriodDateTime": "20280229",
}
# The clock's first and last second, on a leap day.
_CLOSED_LEAP_DAY = {
    **_CLOSED_LOT,
    "TradeDate": "20280229",
    "OpenDateTime": "20280229;000000",
    "HoldingPeriodDateTime": "20280229;235959",
}


def test_flex_leap_day_and_clock_boundaries_parse() -> None:
    [lot] = parse_open_positions([_OPEN_LEAP_DAY])
    assert (lot.report_date, lot.open_date_time) == (date(2028, 2, 29), date(2028, 2, 29))
    [closed_lot] = parse_closed_lots(with_closing_executions(_CLOSED_LEAP_DAY))
    assert (
        closed_lot.open_date_time,
        closed_lot.holding_period_date_time,
        closed_lot.close_date_time,
    ) == (date(2028, 2, 29), date(2028, 2, 29), date(2028, 2, 29))


# --- Issuer country: parser/_scope_guards.py --------------------------------------

# Flex markers fit the cell, which is stripped and upper-cased. An Activity
# Statement lot's country is its ISIN's prefix: ``QZ`` is user-assigned in ISO
# 3166, so it names no country, and the rest is the default instrument's CUSIP.
_OPEN_COUNTRY = "QZOPENCOUNTRY"
_CLOSED_COUNTRY = "QZCLOSEDCOUNTRY"
_PREFIX_COUNTRY = "QZ"
_PREFIX_ISIN = "QZ99990E1031"
_PREFIX_INSTRUMENTS = [make_fii_row(security_id=_PREFIX_ISIN)]
_PREFIX_TRADES = [make_trade_row()]


def _trades_with_unmapped_prefix(rows: Rows) -> object:
    """Parse ``rows`` against an instrument whose ISIN prefix isn't mapped."""
    assert _PREFIX_ISIN.startswith(_PREFIX_COUNTRY)
    instruments = build_instrument_lookup(_PREFIX_INSTRUMENTS)
    return parse_activity_statement_trades(rows, instruments=instruments, period=_TRADES_2025)


_COUNTRY_REFUSALS = [
    pytest.param(
        parse_open_positions,
        _open_rows({"IssuerCountryCode": _OPEN_COUNTRY}),
        ParseError(
            section=_OPEN, row_index=1, column="IssuerCountryCode", message=_UNMAPPED, raw_value=""
        ),
        f"{_OPEN_ROW}'IssuerCountryCode': {_UNMAPPED}",
        (_OPEN_COUNTRY,),
        id="flex-open-positions",
    ),
    pytest.param(
        parse_closed_lots,
        _closed_rows({"IssuerCountryCode": _CLOSED_COUNTRY}),
        ParseError(
            section=_CLOSED,
            row_index=1,
            column="IssuerCountryCode",
            message=_UNMAPPED,
            raw_value="",
        ),
        f"{_CLOSED_ROW}'IssuerCountryCode': {_UNMAPPED}",
        (_CLOSED_COUNTRY,),
        id="flex-closed-lots",
    ),
    pytest.param(
        _trades_with_unmapped_prefix,
        _PREFIX_TRADES,
        ParseError(section="Trades", row_index=0, column="Symbol", message=_UNMAPPED, raw_value=""),
        f"ParseError in section 'Trades' at row 0, column 'Symbol': {_UNMAPPED}",
        (_PREFIX_COUNTRY,),
        id="activity-statement-trades-isin-prefix",
    ),
]


@pytest.mark.parametrize(("parse", "rows", "expected", "text", "markers"), _COUNTRY_REFUSALS)
def test_unmapped_issuer_country_chains_nothing(
    parse: Callable[[Rows], object],
    rows: Rows,
    expected: ParseError,
    text: str,
    markers: tuple[str, ...],
) -> None:
    with pytest.raises(ParseError) as caught:
        parse(rows)
    assert_unchained_refusal(caught.value, expected, text=text, markers=markers)
    assert caught.value.args == ()


# --- Open Positions Side: parser/open_positions.py --------------------------------

_SIDE_TEXT = "QZSIDEMARK"
_UNKNOWN_SIDE = _open_rows({"Side": _SIDE_TEXT})


def test_unknown_side_chains_only_a_stand_in() -> None:
    with pytest.raises(ParseError) as caught:
        parse_open_positions(_UNKNOWN_SIDE)
    assert_unchained_refusal(
        caught.value,
        ParseError(section=_OPEN, row_index=1, column="Side", message=_SIDE, raw_value=""),
        text="ParseError in section 'Open Positions (LOT)' at row 1, column 'Side': " + _SIDE,
        markers=(_SIDE_TEXT,),
        crash=ValueError,
    )
    assert caught.value.args == ()


# --- Activity Statement sections: multi_section.py --------------------------------

_SECTION_NAME = "QZSECTIONMARK"
_UNKNOWN_SECTION_LINES = (
    "Statement,Header,Field Name,Field Value",
    "Statement,Data,BrokerName,Interactive Brokers LLC",
    f"{_SECTION_NAME},Header,Field Name,Field Value",
)
_UNKNOWN_SECTION = (
    "line 3 belongs to a section this tool neither parses nor lists as safe to ignore, so "
    "it can't tell whether the section changes a filed figure. Refusing rather than "
    "dropping it. If IBKR now emits this, please file an issue at "
    "https://github.com/jay-tau/ibkr-fa/issues naming it."
)


def test_unknown_section_chains_only_a_stand_in(tmp_path: Path) -> None:
    path = write(tmp_path, *_UNKNOWN_SECTION_LINES)
    with pytest.raises(ParseError) as caught:
        split_activity_statement_sections(path)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section="<file>", row_index=3, column=None, message=_UNKNOWN_SECTION, raw_value=""
        ),
        text="ParseError in section '<file>' at row 3: " + _UNKNOWN_SECTION,
        markers=(_SECTION_NAME,),
        crash=ValueError,
    )
    assert caught.value.args == ()


# --- Activity Statement dates: activity_statement/_dates.py -----------------------

_DIVIDEND_YEAR = "9211"
_WHT_YEAR = "9212"
_INTEREST_YEAR = "9213"
_LEAP_INTEREST = "USD Credit Interest for Feb-2028"


def _dividends(rows: Rows) -> list[Dividend]:
    """Parse ``rows`` as one Dividends block, closed by their ``Total`` row."""
    return parse_activity_statement_dividends(
        block(KnownActivitySection.DIVIDENDS, closed(rows)), period=_PERIOD_2028
    )


def _withholding_tax(rows: Rows) -> list[WithholdingTax]:
    """Parse ``rows`` as one Withholding Tax block, closed by their ``Total`` row."""
    return parse_activity_statement_withholding_tax(
        block(KnownActivitySection.WITHHOLDING_TAX, closed(rows)), period=_PERIOD_2028
    )


def _interest(rows: Rows) -> tuple[list[InterestCredit], list[InterestNonCredit]]:
    """Parse ``rows`` as one Interest block, closed by their ``Total`` row."""
    return parse_activity_statement_interest(
        block(KnownActivitySection.INTEREST, closed(rows)), period=_PERIOD_2028
    )


_STATEMENT_DATE_REFUSALS = [
    pytest.param(
        _dividends,
        [make_dividend_row(payment_date=f"30-02-{_DIVIDEND_YEAR}")],
        "Dividends",
        (_DIVIDEND_YEAR,),
        id="dividends",
    ),
    pytest.param(
        _withholding_tax,
        [make_wht_row(payment_date=f"{_WHT_YEAR}-02-30")],
        "Withholding Tax",
        (_WHT_YEAR,),
        id="withholding-tax",
    ),
    pytest.param(
        _interest,
        [make_interest_row(credit_date=f"30-02-{_INTEREST_YEAR}")],
        "Interest",
        (_INTEREST_YEAR,),
        id="interest",
    ),
]


@pytest.mark.parametrize(("parse", "rows", "section", "markers"), _STATEMENT_DATE_REFUSALS)
def test_statement_date_refusal_chains_only_a_stand_in(
    parse: Callable[[Rows], object], rows: Rows, section: str, markers: tuple[str, ...]
) -> None:
    with pytest.raises(ParseError) as caught:
        parse(rows)
    assert_unchained_refusal(
        caught.value,
        ParseError(section=section, row_index=0, column="Date", message=_CALENDAR, raw_value=""),
        text=f"ParseError in section {section!r} at row 0, column 'Date': {_CALENDAR}",
        markers=markers,
        crash=ValueError,
    )
    assert caught.value.args == ()


def test_statement_leap_day_controls_parse() -> None:
    [dividend] = _dividends([make_dividend_row(payment_date="29-02-2028")])
    [wht] = _withholding_tax([make_wht_row(payment_date="2028-02-29")])
    interest_credits, non_credits = _interest(
        [make_interest_row(credit_date="29-02-2028", description=_LEAP_INTEREST)]
    )
    assert non_credits == []
    [credit] = interest_credits
    assert (dividend.payment_date, wht.payment_date, credit.credit_date) == (
        date(2028, 2, 29),
        date(2028, 2, 29),
        date(2028, 2, 29),
    )


# --- Statement Period: activity_statement/statement_period.py ----------------------

# The month lookup is case-insensitive and its KeyError holds the month
# lowercased; the marker check is case-insensitive too.
_PERIOD_MONTH = "Qzmonth"
_PRIORITY_MONTH = "Qzstart"
_PERIOD_YEAR = "9221"
_PRIORITY_YEAR = "9222"
_UNKNOWN_MONTH = (
    "the Period contains an unrecognised month name; expected one of ['april', 'august', "
    "'december', 'february', 'january', 'july', 'june', 'march', 'may', 'november', "
    "'october', 'september']."
)
_PERIOD_CALENDAR = "the Period contains a date that isn't a real calendar date."

_PERIOD_REFUSALS = [
    pytest.param(
        [make_row("Period", f"{_PERIOD_MONTH} 1, 2025 - December 31, 2025")],
        _UNKNOWN_MONTH,
        KeyError,
        (_PERIOD_MONTH,),
        id="unknown-month",
    ),
    pytest.param(
        [make_row("Period", f"February 30, {_PERIOD_YEAR} - March 31, {_PERIOD_YEAR}")],
        _PERIOD_CALENDAR,
        ValueError,
        (_PERIOD_YEAR,),
        id="calendar",
    ),
    # Priority: an unknown start month refuses before a bad end day.
    pytest.param(
        [
            make_row(
                "Period", f"{_PRIORITY_MONTH} 1, {_PRIORITY_YEAR} - February 30, {_PRIORITY_YEAR}"
            )
        ],
        _UNKNOWN_MONTH,
        KeyError,
        (_PRIORITY_MONTH,),
        id="unknown-month-and-bad-end-day-refuses-on-the-month",
    ),
]


@pytest.mark.parametrize(("rows", "message", "crash", "markers"), _PERIOD_REFUSALS)
def test_period_refusal_chains_only_a_stand_in(
    rows: Rows, message: str, crash: type[BaseException], markers: tuple[str, ...]
) -> None:
    with pytest.raises(ParseError) as caught:
        parse_statement_period(rows)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section="Statement", row_index=0, column="Period", message=message, raw_value=""
        ),
        text=f"ParseError in section 'Statement' at row 0, column 'Period': {message}",
        markers=markers,
        crash=crash,
    )
    assert caught.value.args == ()


def test_leap_day_period_parses() -> None:
    period = parse_statement_period([make_row("Period", "February 29, 2028 - December 31, 2028")])
    assert period == StatementPeriod(start_date=date(2028, 2, 29), end_date=date(2028, 12, 31))


# --- Trades Date/Time: activity_statement/trades.py --------------------------------

_TRADE_YEAR = "9231"
_TRADE_BOTH_YEAR = "9232"
_TRADE_TIME = "86:45:59"
_TRADE_HOUR = "not 86"
_TRADE_BOTH_TIME = "85:45:59"
_TRADE_CLOCK = "must have a real wall-clock time"

_TRADE_REFUSALS = [
    pytest.param(
        [make_trade_row(date_time=f"{_TRADE_YEAR}-02-30, 10:15:00")],
        _CALENDAR,
        (_TRADE_YEAR,),
        id="calendar",
    ),
    pytest.param(
        [make_trade_row(date_time=f"2025-06-16, {_TRADE_TIME}")],
        _TRADE_CLOCK,
        (_TRADE_TIME, _TRADE_HOUR),
        id="clock",
    ),
    # Priority: the date refuses before the time.
    pytest.param(
        [make_trade_row(date_time=f"{_TRADE_BOTH_YEAR}-02-30, {_TRADE_BOTH_TIME}")],
        _CALENDAR,
        (_TRADE_BOTH_YEAR,),
        id="bad-date-and-time-refuses-on-the-date",
    ),
]


@pytest.mark.parametrize(("rows", "message", "markers"), _TRADE_REFUSALS)
def test_trade_date_time_refusal_chains_only_a_stand_in(
    rows: Rows, message: str, markers: tuple[str, ...]
) -> None:
    instruments = build_instrument_lookup([make_fii_row()])
    with pytest.raises(ParseError) as caught:
        parse_activity_statement_trades(rows, instruments=instruments, period=_TRADES_2025)
    assert_unchained_refusal(
        caught.value,
        ParseError(
            section="Trades", row_index=0, column="Date/Time", message=message, raw_value=""
        ),
        text=f"ParseError in section 'Trades' at row 0, column 'Date/Time': {message}",
        markers=markers,
        crash=ValueError,
    )
    assert caught.value.args == ()


def test_trade_leap_day_and_clock_boundaries_parse() -> None:
    rows = [
        make_trade_row(date_time="2028-02-29, 00:00:00"),
        make_trade_row(date_time="2028-02-29, 23:59:59"),
    ]
    events = parse_activity_statement_trades(
        rows, instruments=build_instrument_lookup([make_fii_row()]), period=_PERIOD_2028
    )
    assert [event.trade_date_time for event in events] == [
        datetime(2028, 2, 29, 0, 0, 0),
        datetime(2028, 2, 29, 23, 59, 59),
    ]


# --- CLI crash classification ------------------------------------------------------

_TRACEBACK = "IBKR_FA_TRACEBACK"
_CLI_SIDE = "QZCLISIDE"
_CLI_COUNTRY = "QZCLICOUNTRY"


def _flex_lot(old: str, new: str) -> str:
    """A Flex Query export whose one lot has ``old`` replaced by ``new``."""
    assert _OPEN_POSITIONS_SECTION.count(old) == 1
    return "\n".join((_ACCOUNT_INFO_SECTION, _OPEN_POSITIONS_SECTION.replace(old, new)))


_CLASSIFIED = [
    pytest.param(
        _flex_lot('"Long","LOT"', f'"{_CLI_SIDE}","LOT"'),
        ParseError(section=_OPEN, row_index=0, column="Side", message=_SIDE, raw_value=""),
        f"ParseError in section 'Open Positions (LOT)' at row 0, column 'Side': {_SIDE}",
        ValueError,
        (_CLI_SIDE,),
        id="crash-classified",
    ),
    pytest.param(
        _flex_lot('"NASDAQ","US"', f'"NASDAQ","{_CLI_COUNTRY}"'),
        ParseError(
            section=_OPEN, row_index=0, column="IssuerCountryCode", message=_UNMAPPED, raw_value=""
        ),
        f"ParseError in section 'Open Positions (LOT)' at row 0, column 'IssuerCountryCode': "
        f"{_UNMAPPED}",
        None,
        (_CLI_COUNTRY,),
        id="domain-only",
    ),
]


@pytest.mark.parametrize("traceback_flag", [False, True], ids=["default", "traceback-flag"])
@pytest.mark.parametrize(("flex", "refusal", "text", "crash", "markers"), _CLASSIFIED)
def test_cli_still_classifies_a_parser_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flex: str,
    refusal: ParseError,
    text: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
    traceback_flag: bool,
) -> None:
    """Under ``IBKR_FA_TRACEBACK=1`` a refusal that replaced a crash still
    re-raises, through its stand-in; the country refusal, which replaced none,
    still prints. Without the flag both print the unchanged refusal. Either way
    an earlier run's outputs are removed and none is written."""
    if traceback_flag:
        monkeypatch.setenv(_TRACEBACK, "1")
    else:
        monkeypatch.delenv(_TRACEBACK, raising=False)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "schedule-fa.csv"
    for stale in (out, out_dir / "schedule-fa-a2.csv"):
        stale.write_text("stale", encoding="utf-8")
    argv = [
        *("schedule-fa", "--input", str(write_flex_csv(tmp_path, flex))),
        *("--config", str(write_user_toml(tmp_path)), "--output", str(out)),
        *("--calendar-year", "2025", "--no-fetch-prices"),
    ]

    if traceback_flag and crash is not None:
        with pytest.raises(ParseError) as raised:
            main(argv)
        assert_unchained_refusal(raised.value, refusal, text=text, markers=markers, crash=crash)
        assert raised.value.args == ()
        assert capsys.readouterr() == ("", "")
    else:
        assert main(argv) == 4
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"error: {text}\n"
    assert list(out_dir.iterdir()) == []
