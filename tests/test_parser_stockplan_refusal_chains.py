"""StockPlan title-date and workbook refusals keep no input in their exception
graph (#1226).

Each title-date or openpyxl refusal is raised after its handler has ended,
from a content-free stand-in for the error it replaced. Before, that error stayed as the refusal's
suppressed context and kept the input: a title's month-key error names the
month token and, on Python 3.14, its calendar error names the year; openpyxl's
error at open names a manifest member the ZIP lacks, and its error reading the
sheet quotes a numeric cell's text. The direct refusal tests run one of the four StockPlan
readers on a real synthetic workbook, pin the unchanged refusal, then check
that no input marker is anywhere in its exception graph (suppressed links
included), then that its only link is the stand-in naming the replaced error's
class. A warning replacement instead keeps that sanitized refusal as its
suppressed context. A calendar marker can only be printed on 3.14, so the
stand-in check guards those sites on older runtimes.

The markers are module constants because a rendered traceback prints the line
that calls the reader. No year can match a traceback line number: each is past
9000.
"""

from __future__ import annotations

import datetime as _dt
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import pytest

import ibkr_fa.parser.stockplan._xlsx as xlsx_mod
import ibkr_fa.xlsx_checks
from ibkr_fa.cli import _STOCKPLAN_CONTRACT_POINTER
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from ibkr_fa.parser.stockplan.holdings import parse_stockplan_holdings
from tests._crash_reports import (
    assert_prints_no_marker,
    assert_replaced_unchained,
    assert_unchained_refusal,
)
from tests._stockplan_fa_exports import run_stockplan_fa_cli as _main_fa
from tests._stockplan_fa_exports import setup_stockplan_exports as _setup
from tests._stockplan_holdings import quote_history_rows
from tests._stockplan_no_echo_books import (
    BOOK_ISIN,
    BOOK_PERIOD,
    BOOK_RUN_ON,
    BOOK_SYMBOL,
    CAPITAL_GAINS_FILE,
    CAPITAL_GAINS_SHEET,
    DIVIDEND_FILE,
    DIVIDEND_SHEET,
    HOLDINGS_FILE,
    HOLDINGS_SHEET,
    RELEASES_SHEET,
    STOCKPLAN_PARSERS,
    book_withholding_row,
    capital_gains_book,
    cash_holding_row,
    dividend_book,
    dividend_row,
    dividend_title,
    holdings_book,
    refusal_without_escaped_warnings,
    sale_row,
    stock_holding_row,
    vest_row,
)
from tests._xlsx_package_edits import (
    date_styled_overflow,
    edit_sheets,
    replace_once_in_sheet,
    set_cells,
    store_row_out_of_order,
    worksheets,
)


def _dividend_report(tmp_path: Path, run_on: str, period: str = BOOK_PERIOD) -> Path:
    return dividend_book(
        tmp_path,
        dividend_row(),
        book_withholding_row(),
        title=dividend_title(run_on=run_on, period=period),
    )


def _holdings_report(
    tmp_path: Path, run_on: str, quote_date: _dt.date = _dt.date(2026, 4, 2)
) -> Path:
    """A Holdings workbook run on ``run_on``, as of ``quote_date``: by default
    the day of the earliest valid run date these tests use."""
    title = f"Synthetic Issuer - Holdings Report (report run on {run_on})"
    quotes = quote_history_rows("XQCB - NASDAQ", quote_date=quote_date)
    return holdings_book(
        tmp_path, cash_holding_row(), stock_holding_row(), title=title, quotes=quotes
    )


def _releases_report(tmp_path: Path, run_on: str) -> Path:
    title = f"Synthetic Issuer - Releases Report (report run on {run_on})"
    return capital_gains_book(
        tmp_path, vests=(vest_row(),), sales=(sale_row(),), releases_title=title
    )


def _capital_gains_report(tmp_path: Path, run_on: str, period: str = BOOK_PERIOD) -> Path:
    return capital_gains_book(
        tmp_path, vests=(vest_row(),), sales=(sale_row(),), run_on=run_on, period=period
    )


@dataclass(frozen=True, slots=True)
class _Caller:
    """One StockPlan reader, the sheet it reads, and this module's markers for it."""

    id: str
    sheet: str
    file: str
    # The report's name in its malformed-title-date refusal.
    report: str
    # A workbook whose sheet's title run date is the given token.
    build: Callable[[Path, str], Path]
    # The sheet's worksheet part in the ZIP.
    member: str
    # A numeric cell on the sheet, its stored text, and the data row's first
    # cell, stored before it.
    numeric: str
    numeric_text: str
    first: str
    # Markers: a title month token, a title year, a manifest member's name and
    # a numeric cell's text.
    month: str
    year: str
    part: str
    number: str

    def parse(self, path: Path) -> object:
        return STOCKPLAN_PARSERS[self.sheet](path)


_DIVIDEND = _Caller(
    id="dividend",
    sheet=DIVIDEND_SHEET,
    file=DIVIDEND_FILE,
    report="Dividend Report",
    build=_dividend_report,
    member="xl/worksheets/sheet1.xml",
    numeric="G3",
    numeric_text="0",
    first="A3",
    month="Qzd",
    year="9241",
    part="qzmemberdiv",
    number="QZNUMBERDIV",
)
_HOLDINGS = _Caller(
    id="holdings",
    sheet=HOLDINGS_SHEET,
    file=HOLDINGS_FILE,
    report="Holdings Report",
    build=_holdings_report,
    member="xl/worksheets/sheet1.xml",
    numeric="D3",
    numeric_text="4.18",
    first="A3",
    month="Qzh",
    year="9242",
    part="qzmemberhold",
    number="QZNUMBERHOLD",
)
_RELEASES = _Caller(
    id="releases",
    sheet=RELEASES_SHEET,
    file=CAPITAL_GAINS_FILE,
    report="Releases Report",
    build=_releases_report,
    member="xl/worksheets/sheet1.xml",
    numeric="F3",
    numeric_text="196.42",
    first="A3",
    month="Qzr",
    year="9243",
    part="qzmemberrel",
    number="QZNUMBERREL",
)
_CAPITAL_GAINS = _Caller(
    id="capital-gains",
    sheet=CAPITAL_GAINS_SHEET,
    file=CAPITAL_GAINS_FILE,
    report="Capital Gains report",
    build=_capital_gains_report,
    member="xl/worksheets/sheet2.xml",
    numeric="B4",
    numeric_text="190.25",
    first="A4",
    month="Qzc",
    year="9244",
    part="qzmembercg",
    number="QZNUMBERCG",
)
_CALLERS = (_DIVIDEND, _HOLDINGS, _RELEASES, _CAPITAL_GAINS)
_EACH_CALLER = pytest.mark.parametrize("caller", _CALLERS, ids=[c.id for c in _CALLERS])


def _title_message(caller: _Caller) -> str:
    return f"malformed {caller.report} date in {caller.file}"


def _open_message(caller: _Caller) -> str:
    return f"could not open {caller.file} as an .xlsx workbook"


def _read_message(caller: _Caller) -> str:
    return f"could not read the {caller.sheet!r} sheet's rows from {caller.file}"


def _expected(caller: _Caller, message: str) -> ParseError:
    return ParseError(section=caller.sheet, row_index=0, column=None, message=message, raw_value="")


def _assert_refusal(
    error: BaseException,
    caller: _Caller,
    message: str,
    *,
    markers: tuple[str, ...],
    crash: type[BaseException],
) -> None:
    """``error`` is ``caller``'s row-0 refusal with ``message``, with no marker
    anywhere in its graph and only the stand-in for ``crash`` as its cause."""
    assert_unchained_refusal(
        error,
        _expected(caller, message),
        text=f"ParseError in section {caller.sheet!r} at row 0: {message}",
        markers=markers,
        crash=crash,
    )
    assert error.args == ()


def _rewrite(
    path: Path, member: str, edit: Callable[[str], str], *, add: dict[str, str] | None = None
) -> Path:
    """Replace ``path``'s ZIP ``member`` with ``edit`` of its text, and add ``add``'s members."""
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    payloads[member] = edit(payloads[member].decode("utf-8")).encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
        for name, text in (add or {}).items():
            package.writestr(name, text)
    return path


_SHARED_STRINGS = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
_EMPTY_SHARED_STRINGS = (
    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" '
    'uniqueCount="0" />'
)


def _shared_strings_part(path: Path, name: str, *, present: bool) -> Path:
    """Name ``xl/<name>.xml`` as the workbook's shared-strings part in its manifest.

    openpyxl opens that part while it loads the workbook, so a ZIP without it
    fails at open with a ``KeyError`` naming it. With ``present``, the ZIP
    holds an empty table there; the sheets store inline strings, so it's unused.
    """
    member = f"xl/{name}.xml"
    override = f'<Override PartName="/{member}" ContentType="{_SHARED_STRINGS}" />'

    def edit(types: str) -> str:
        assert types.count("</Types>") == 1
        assert _SHARED_STRINGS not in types
        return types.replace("</Types>", f"{override}</Types>")

    add = {member: _EMPTY_SHARED_STRINGS} if present else None
    return _rewrite(path, "[Content_Types].xml", edit, add=add)


def _numeric_cell_text(path: Path, caller: _Caller, text: str) -> Path:
    """Store ``text`` as ``caller``'s numeric cell's value, keeping its ``t="n"`` type."""
    cell = re.compile(rf'(<c r="{caller.numeric}"[^>]*\bt="n"[^>]*><v>)[^<]*(</v>)')

    def edit(sheet: str) -> str:
        edited, count = cell.subn(lambda match: f"{match[1]}{text}{match[2]}", sheet)
        assert count == 1
        return edited

    return _rewrite(path, caller.member, edit)


def _malformed_number(tmp_path: Path, caller: _Caller) -> Path:
    """``caller``'s workbook, its numeric cell holding the number marker."""
    return _numeric_cell_text(caller.build(tmp_path, BOOK_RUN_ON), caller, caller.number)


# --- _xlsx.title_date: month lookup and calendar ------------------------------


@_EACH_CALLER
def test_unknown_title_month_keeps_no_month(tmp_path: Path, caller: _Caller) -> None:
    path = caller.build(tmp_path, f"02-{caller.month}-2026")
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    _assert_refusal(
        caught.value, caller, _title_message(caller), markers=(caller.month,), crash=KeyError
    )


@_EACH_CALLER
def test_calendar_invalid_title_day_keeps_no_date_parts(tmp_path: Path, caller: _Caller) -> None:
    path = caller.build(tmp_path, f"30-Feb-{caller.year}")
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    _assert_refusal(
        caught.value, caller, _title_message(caller), markers=(caller.year,), crash=ValueError
    )


def _title_order(
    caller: _Caller,
    build: Callable[[Path, str, str], Path],
    month: str,
    year: str,
    *,
    month_first: bool,
) -> object:
    """A title with an unknown ``month`` and 30 February of ``year``: one in the
    run date, the other in the period end. The first refuses, so the stand-in
    names its error's class."""
    bad_month, bad_day = f"{month}-2026", f"Feb-{year}"
    if month_first:
        run_on, end, crash = f"02-{bad_month}", f"30-{bad_day}", KeyError
    else:
        run_on, end, crash = f"30-{bad_day}", f"31-{bad_month}", ValueError
    order = "bad-month-before-bad-day" if month_first else "bad-day-before-bad-month"
    return pytest.param(
        caller,
        build,
        run_on,
        f"01-Apr-2025 to {end}",
        crash,
        (month,) if month_first else (year,),
        id=f"{caller.id}-{order}",
    )


_TITLE_ORDER = (
    _title_order(_DIVIDEND, _dividend_report, "Qzp", "9245", month_first=True),
    _title_order(_DIVIDEND, _dividend_report, "Qzs", "9246", month_first=False),
    _title_order(_CAPITAL_GAINS, _capital_gains_report, "Qzt", "9247", month_first=True),
    _title_order(_CAPITAL_GAINS, _capital_gains_report, "Qzu", "9248", month_first=False),
)


@pytest.mark.parametrize(("caller", "build", "run_on", "period", "crash", "markers"), _TITLE_ORDER)
def test_first_bad_title_date_refuses(
    tmp_path: Path,
    caller: _Caller,
    build: Callable[[Path, str, str], Path],
    run_on: str,
    period: str,
    crash: type[BaseException],
    markers: tuple[str, ...],
) -> None:
    path = build(tmp_path, run_on, period)
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    _assert_refusal(
        caught.value,
        caller,
        _title_message(caller),
        markers=markers,
        crash=crash,
    )


_LEAP_DAY = _dt.date(2028, 2, 29)


def test_leap_day_title_dates_parse(tmp_path: Path) -> None:
    # Control: a leap day in every title date position each reader has. A
    # report must run strictly after its period ends, so one title can't hold
    # 29-Feb-2028 as both the run date and the period end.
    for name in ("holdings", "releases", "dividend-run", "dividend-end", "cg-run", "cg-end"):
        (tmp_path / name).mkdir()
    # As of the run date itself, which passes only if the run date reads as the leap day.
    holdings = parse_stockplan_holdings(
        _holdings_report(tmp_path / "holdings", "29-Feb-2028", _LEAP_DAY),
        symbol=BOOK_SYMBOL,
        listing_exchange="NASDAQ",
    )
    assert holdings.selected_date == _LEAP_DAY
    vests, run_on = parse_stockplan_releases(
        _releases_report(tmp_path / "releases", "29-Feb-2028"), symbol=BOOK_SYMBOL
    )
    assert (len(vests), run_on) == (1, _LEAP_DAY)

    leap_run = ("29-Feb-2028", "29-Feb-2024 to 28-Feb-2028")
    leap_end = ("01-Mar-2028", "01-Apr-2025 to 29-Feb-2028")
    run_coverage = (_dt.date(2024, 2, 29), _dt.date(2028, 2, 28))
    end_coverage = (_dt.date(2025, 4, 1), _LEAP_DAY)
    for folder, title, coverage in (
        ("dividend-run", leap_run, run_coverage),
        ("dividend-end", leap_end, end_coverage),
    ):
        dividends, withholdings, parsed = parse_stockplan_dividend_report(
            _dividend_report(tmp_path / folder, *title), symbol=BOOK_SYMBOL, isin=BOOK_ISIN
        )
        assert (len(dividends), len(withholdings), parsed) == (1, 1, coverage)
    for folder, title, coverage in (
        ("cg-run", leap_run, run_coverage),
        ("cg-end", leap_end, end_coverage),
    ):
        sales, parsed = parse_stockplan_capital_gains_report(
            _capital_gains_report(tmp_path / folder, *title), symbol=BOOK_SYMBOL
        )
        assert (len(sales), parsed) == (1, coverage)


# --- _xlsx._read_sheet_rows: opening the workbook ----------------------------


@_EACH_CALLER
def test_manifest_member_missing_from_the_zip_keeps_no_member_name(
    tmp_path: Path, caller: _Caller
) -> None:
    path = _shared_strings_part(caller.build(tmp_path, BOOK_RUN_ON), caller.part, present=False)
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    _assert_refusal(
        caught.value, caller, _open_message(caller), markers=(caller.part,), crash=KeyError
    )


@_EACH_CALLER
def test_manifest_member_in_the_zip_still_parses(tmp_path: Path, caller: _Caller) -> None:
    # Control: the same manifest entry, with its member present.
    path = caller.build(tmp_path, BOOK_RUN_ON)
    before = caller.parse(path)
    _shared_strings_part(path, caller.part, present=True)
    assert caller.parse(path) == before


# --- _xlsx._read_sheet_rows: reading the sheet --------------------------------


@_EACH_CALLER
def test_numeric_cell_holding_text_keeps_no_cell_text(tmp_path: Path, caller: _Caller) -> None:
    path = _malformed_number(tmp_path, caller)
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    _assert_refusal(
        caught.value, caller, _read_message(caller), markers=(caller.number,), crash=ValueError
    )


@_EACH_CALLER
def test_numeric_cell_holding_a_number_still_parses(tmp_path: Path, caller: _Caller) -> None:
    # Control: the same cell rewritten with its own number.
    path = caller.build(tmp_path, BOOK_RUN_ON)
    before = caller.parse(path)
    _numeric_cell_text(path, caller, caller.numeric_text)
    assert caller.parse(path) == before


@_EACH_CALLER
def test_workbook_closes_while_the_read_refusal_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: _Caller
) -> None:
    path = _malformed_number(tmp_path, caller)
    load = xlsx_mod.openpyxl.load_workbook
    active_during_close: list[BaseException | None] = []

    def load_spying_on_close(*args: object, **kwargs: object) -> object:
        workbook = load(*args, **kwargs)
        close = workbook.close

        def spy() -> None:
            active_during_close.append(sys.exception())
            close()

        monkeypatch.setattr(workbook, "close", spy)
        return workbook

    monkeypatch.setattr(xlsx_mod.openpyxl, "load_workbook", load_spying_on_close)
    with pytest.raises(ParseError) as caught:
        caller.parse(path)
    [active] = active_during_close
    assert active is caught.value
    _assert_refusal(
        caught.value, caller, _read_message(caller), markers=(caller.number,), crash=ValueError
    )


@_EACH_CALLER
def test_warning_replacement_keeps_no_cell_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caller: _Caller
) -> None:
    # openpyxl warns reading the data row's first cell, a date-styled number
    # out of range for a date, then fails on the numeric cell holding text. The
    # warning's refusal replaces the read refusal, unchanged (#1103), and so
    # keeps it as a suppressed context: that refusal's only link is the stand-in.
    overflow = date_styled_overflow(caller.build(tmp_path, BOOK_RUN_ON), caller.sheet, caller.first)
    path = _numeric_cell_text(overflow, caller, caller.number)

    refusal = refusal_without_escaped_warnings(capsys, lambda: caller.parse(path))

    expected = _expected(caller, _read_message(caller))
    assert type(refusal) is ParseError
    assert refusal == expected
    assert str(refusal) == f"ParseError in section {caller.sheet!r} at row 0: {expected.message}"
    assert refusal.args == ()
    assert_prints_no_marker(refusal, markers=(caller.number,))
    assert refusal.__cause__ is None
    assert refusal.__suppress_context__
    replaced = refusal.__context__
    assert type(replaced) is ParseError
    assert replaced is not refusal
    assert replaced == expected
    assert_replaced_unchained(replaced, ValueError)


# --- Priority: the whole in-order scan runs before the formula and error checks


def _scan_site(code: str) -> str:
    """The stand-in's text for a ``ValueError`` the sheet scan raises on the
    one ``xlsx_checks.py`` line holding ``code``."""
    lines = Path(ibkr_fa.xlsx_checks.__file__).read_text(encoding="utf-8").splitlines()
    [number] = [index for index, line in enumerate(lines, start=1) if code in line]
    return f"ValueError at ibkr_fa/xlsx_checks.py:{number}"


# openpyxl raises reading a cell inside the scan's parser loop; the scan raises
# a row stored out of order itself.
_CELL_UNREADABLE = "in parser.parse():"
_ROW_OUT_OF_ORDER = '"row {row} is stored out of order"'


def _formula_before(path: Path) -> Path:
    return set_cells(path, DIVIDEND_SHEET, F3="=1+1")


def _error_value_before(path: Path) -> Path:
    return set_cells(path, DIVIDEND_SHEET, F3="#N/A")


def _row_out_of_order_before(path: Path) -> Path:
    """Store an empty row 1 again between rows 2 and 3."""
    before = worksheets(path)
    edit_sheets(path, replace_once_in_sheet('<row r="3">', '<row r="1" /><row r="3">'))
    assert worksheets(path) != before
    return path


# Each case adds a fault to the Dividend Report and then stores the number marker
# in G3; the scan stops at the first failure in stored order.
_SCAN_ORDER = (
    pytest.param(_formula_before, _CELL_UNREADABLE, True, id="formula-before-the-cell"),
    pytest.param(_error_value_before, _CELL_UNREADABLE, True, id="error-value-before-the-cell"),
    pytest.param(
        store_row_out_of_order, _CELL_UNREADABLE, True, id="row-out-of-order-after-the-cell"
    ),
    # The scan stops before the cell, so its text can't reach any graph.
    pytest.param(
        _row_out_of_order_before, _ROW_OUT_OF_ORDER, False, id="row-out-of-order-before-the-cell"
    ),
)


@pytest.mark.parametrize(("fault", "site", "cell_read"), _SCAN_ORDER)
def test_unreadable_scan_refusal_wins(
    tmp_path: Path, fault: Callable[[Path], Path], site: str, cell_read: bool
) -> None:
    path = _numeric_cell_text(
        fault(_DIVIDEND.build(tmp_path, BOOK_RUN_ON)), _DIVIDEND, _DIVIDEND.number
    )
    with pytest.raises(ParseError) as caught:
        _DIVIDEND.parse(path)
    _assert_refusal(
        caught.value,
        _DIVIDEND,
        _read_message(_DIVIDEND),
        markers=(_DIVIDEND.number,) if cell_read else (),
        crash=ValueError,
    )
    assert str(caught.value.__cause__) == _scan_site(site)


# --- CLI classification --------------------------------------------------------

_CLI_MONTH = "Qzk"
_CLI_RUN_ON = f"02-{_CLI_MONTH}-2026"
_CLI_REFUSAL = ParseError(
    section="Releases Report",
    row_index=0,
    column=None,
    message="malformed Releases Report date in IN - Capital Gain_Loss Report.xlsx",
    raw_value="",
)
_CLI_TEXT = (
    "ParseError in section 'Releases Report' at row 0: malformed Releases Report date in "
    "IN - Capital Gain_Loss Report.xlsx"
)


@pytest.mark.parametrize("traceback_flag", [False, True], ids=["default", "traceback-flag"])
def test_cli_still_classifies_a_title_month_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    traceback_flag: bool,
) -> None:
    """Under ``IBKR_FA_TRACEBACK=1`` the refusal still re-raises, through its
    stand-in for the month-key error; without the flag it prints unchanged,
    with the StockPlan contract pointer. Either way the run's output is gone."""
    input_dir, cfg, out = _setup(tmp_path, releases_run_on=_CLI_RUN_ON)
    out.write_text("stale")
    inputs = sorted(path for path in tmp_path.rglob("*") if path != out)

    if traceback_flag:
        monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")
        with pytest.raises(ParseError) as raised:
            _main_fa(input_dir, cfg, out)
        assert sorted(tmp_path.rglob("*")) == inputs
        assert capsys.readouterr() == ("", "")
        assert_unchained_refusal(
            raised.value, _CLI_REFUSAL, text=_CLI_TEXT, markers=(_CLI_MONTH,), crash=KeyError
        )
        assert raised.value.args == ()
        return
    assert _main_fa(input_dir, cfg, out) == 4
    assert sorted(tmp_path.rglob("*")) == inputs
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_CLI_TEXT}\n{_STOCKPLAN_CONTRACT_POINTER}\n"
