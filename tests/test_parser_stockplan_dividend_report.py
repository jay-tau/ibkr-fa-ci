"""Tests for the Morgan Stanley StockPlan "IN - Dividend Report" parser (#365).

Uses a synthetic openpyxl workbook in the report's layout (title row, header,
paired Dividend Paid / IRS NRA Withholding rows, a quote-history error line, and
the currency footer).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from xml.etree.ElementTree import ParseError as XMLParseError
from zipfile import ZipFile

import openpyxl
import pytest

import ibkr_fa.parser.stockplan._xlsx as xlsx_mod
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.dividend_report import parse_stockplan_dividend_report
from tests._stockplan_report_writers import STOCKPLAN_DIVIDEND_ISIN as _ISIN
from tests._stockplan_report_writers import STOCKPLAN_REPORT_SYMBOL as _SYMBOL
from tests._stockplan_report_writers import STOCKPLAN_USD_FOOTER
from tests._stockplan_report_writers import dividend_paid_row as _div
from tests._stockplan_report_writers import dividend_withholding_row as _wht
from tests._stockplan_report_writers import write_dividend_rows_report as _write


def test_parses_paired_dividend_and_withholding(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _div("2025-12-10", 2.60),
            _wht("2025-12-10", -0.65),
            _div("2026-03-11", 4.12),
            _wht("2026-03-11", -1.03),
            ["Unable to get quote as at 1899/12/31, funds = (ilxable symbol = [.XQCB.NASDAQ.]), "],
            [
                "Unable to retrieve market price for fund: [.XQCB.NASDAQ.] "
                "on date: 1899/12/31. Returned $0.00 USD instead."
            ],
        ],
    )
    divs, whts, coverage = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    # Run on 22-Jun-2026, one day after the coverage ends, so it is accepted (#682).
    assert coverage == (_dt.date(1900, 1, 1), _dt.date(2026, 6, 21))
    assert [(d.payment_date.isoformat(), d.amount_gross) for d in divs] == [
        ("2025-12-10", Decimal("2.60")),
        ("2026-03-11", Decimal("4.12")),
    ]
    assert [(w.payment_date.isoformat(), w.amount) for w in whts] == [
        ("2025-12-10", Decimal("-0.65")),
        ("2026-03-11", Decimal("-1.03")),
    ]
    assert all(d.currency == "USD" and d.isin == _ISIN for d in divs)


def test_unrecognised_activity_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [[_dt.datetime(2025, 12, 10), "Dividend Reinvested", 2.60, "XQCB", None, None, 0.0]],
    )
    with pytest.raises(ParseError, match="unrecognised dividend-report Activity"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_positive_withholding_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_wht("2025-12-10", 0.65)])  # positive -> refuse
    with pytest.raises(ParseError, match="must be a negative"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_nonpositive_dividend_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 0.0)])
    with pytest.raises(ParseError, match="must be a positive"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_symbol_mismatch_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [[_dt.datetime(2025, 12, 10), "Dividend Paid", 2.60, "XQBN", None, None, 0.0]],
    )
    with pytest.raises(ParseError, match="does not match the configured"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_populated_units_refuses(tmp_path: Path) -> None:
    row = _div("2025-12-10", 2.60)
    row[4] = 1.0
    path = _write(tmp_path, [row])
    with pytest.raises(ParseError, match="Number of Units or Shares is populated"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_populated_unit_price_refuses(tmp_path: Path) -> None:
    row = _div("2025-12-10", 2.60)
    row[5] = 2.60
    path = _write(tmp_path, [row])
    with pytest.raises(ParseError, match="Unit or Share Price is populated"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_nonzero_book_value_refuses(tmp_path: Path) -> None:
    row = _div("2025-12-10", 2.60)
    row[6] = 2.60
    path = _write(tmp_path, [row])
    with pytest.raises(ParseError, match="Book Value must be zero"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_blank_book_value_refuses(tmp_path: Path) -> None:
    row = _div("2025-12-10", 2.60)
    row[6] = None
    path = _write(tmp_path, [row])
    with pytest.raises(ParseError, match="non-numeric Book Value"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_number_refuses(tmp_path: Path, text: str) -> None:
    row = _div("2025-12-10", 2.60)
    row[2] = text
    path = _write(tmp_path, [row])
    with pytest.raises(ParseError, match="non-finite Cash") as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "IN - Dividend Report",
        2,
        "Cash",
        "",
    )


def test_duplicate_dividend_same_date_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60), _div("2025-12-10", 2.60)],  # same-date duplicate
    )
    with pytest.raises(ParseError, match="duplicate 'Dividend Paid'"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_duplicate_withholding_same_date_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60), _wht("2025-12-10", -0.65), _wht("2025-12-10", -0.65)],
    )
    with pytest.raises(ParseError, match="duplicate 'IRS Nonresident Alien Withholding'"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_data_row_with_non_date_entry_date_refuses(tmp_path: Path) -> None:
    # A data-looking row (Activity populated) whose Entry Date is a string must
    # REFUSE, not be silently skipped (which would under-report).
    path = _write(
        tmp_path,
        [["2025-12-10", "Dividend Paid", 2.60, "XQCB", None, None, 0.0]],
    )
    with pytest.raises(ParseError, match="non-date Entry Date"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_malformed_coverage_date_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60)],
        period="reporting period 31-Apr-2025 to 31-Mar-2026",  # 31 April doesn't exist
    )
    with pytest.raises(ParseError, match="malformed Dividend Report date in"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


@pytest.mark.parametrize("run_on", ["01-Feb-2026", "31-Mar-2026"])
def test_run_on_or_before_coverage_end_refuses(tmp_path: Path, run_on: str) -> None:
    # A report run before its period ends, or on its last day, can miss later
    # dividends, so its claimed coverage cannot prove none is missing (#682).
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60)],
        run_on=run_on,
        period="reporting period 01-Jan-1900 to 31-Mar-2026",
    )
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert excinfo.value.message == (
        "IN - Dividend Report.xlsx was not run strictly after its reporting period ended, so "
        "it can miss later dividends; re-export the report after the period ends"
    )


_FY_RUN_ON = "02-Apr-2026"
_FY_PERIOD = "reporting period 01-Apr-2025 to 31-Mar-2026"


def test_rows_before_coverage_refuse(tmp_path: Path) -> None:
    # The title says FY 2025-26 but the rows are from March 2025: compute would
    # drop them as out-of-FY and file zero income. Refuse instead (#912).
    path = _write(
        tmp_path,
        [_div("2025-03-15", 10.00), _wht("2025-03-15", -2.50)],
        run_on=_FY_RUN_ON,
        period=_FY_PERIOD,
    )
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "IN - Dividend Report",
        2,
        "Entry Date",
        "",
    )
    assert error.message == "'Dividend Paid' is dated outside the report's stated reporting period"


def test_rows_on_coverage_boundaries_parse(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_div("2025-04-01", 2.60), _wht("2025-04-01", -0.65), _div("2026-03-31", 4.12)],
        run_on=_FY_RUN_ON,
        period=_FY_PERIOD,
    )
    divs, whts, _ = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert [d.payment_date for d in divs] == [_dt.date(2025, 4, 1), _dt.date(2026, 3, 31)]
    assert [w.payment_date for w in whts] == [_dt.date(2025, 4, 1)]


def test_withholding_alone_outside_coverage_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [_div("2026-03-31", 4.12), _wht("2026-04-01", -1.03)],
        run_on=_FY_RUN_ON,
        period=_FY_PERIOD,
    )
    with pytest.raises(
        ParseError,
        match=r"row 3, column 'Entry Date': 'IRS Nonresident Alien Withholding' is dated "
        r"outside the report's stated reporting period$",
    ):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


@pytest.mark.parametrize("run_on", ["31-Feb-2026", "01-Foo-2026"])
def test_malformed_run_date_refuses(tmp_path: Path, run_on: str) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)], run_on=run_on)
    with pytest.raises(ParseError, match="malformed Dividend Report date in"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


# A title prefix holding its own whole clause, which the parser once read
# instead of the closing one (#1071).
_PREFIX_WITH_CLAUSE = (
    "SAMPLE ISSUER CB, Inc. (report run on 05-Apr-2026 "
    "for reporting period 01-Jan-2020 to 31-Mar-2026)"
)


def test_title_prefix_clause_never_supplies_the_coverage(tmp_path: Path) -> None:
    # February-only coverage, so stockplan-income's FY span check then refuses.
    path = _write(
        tmp_path,
        [_div("2026-02-10", 2.60), _wht("2026-02-10", -0.39)],
        prefix=_PREFIX_WITH_CLAUSE,
        run_on="05-Mar-2026",
        period="reporting period 01-Feb-2026 to 28-Feb-2026",
    )
    _divs, _whts, coverage = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert coverage == (_dt.date(2026, 2, 1), _dt.date(2026, 2, 28))


@pytest.mark.parametrize(
    ("run_on", "period", "message"),
    [
        pytest.param(
            "05-Apr-2026",
            "reporting period 01-Jan-2026 to 31-Feb-2026",
            "malformed Dividend Report date in IN - Dividend Report.xlsx",
            id="closing-date-not-real",
        ),
        pytest.param(
            "15-Mar-2026",
            "reporting period 01-Jan-2026 to 31-Mar-2026",
            "IN - Dividend Report.xlsx was not run strictly after its reporting period "
            "ended, so it can miss later dividends; re-export the report after the period ends",
            id="closing-run-date-before-period-end",
        ),
    ],
)
def test_title_prefix_clause_never_hides_a_bad_closing_clause(
    tmp_path: Path, run_on: str, period: str, message: str
) -> None:
    path = _write(
        tmp_path,
        [_div("2026-02-10", 2.60), _wht("2026-02-10", -0.39)],
        prefix=_PREFIX_WITH_CLAUSE,
        run_on=run_on,
        period=period,
    )
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    error = excinfo.value
    assert (error.section, error.row_index, error.column) == ("IN - Dividend Report", 0, None)
    assert (error.message, error.raw_value) == (message, "")
    for date in ("Apr-2026", "Mar-2026", "Feb-2026", "Jan-2026", "Jan-2020", "2020"):
        assert date not in str(error)


def test_negated_usd_footer_refuses(tmp_path: Path) -> None:
    # A negated currency note must NOT satisfy the USD confirmation.
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60)],
        currency_footer="All currencies are not in US Dollars unless specified.",
    )
    with pytest.raises(ParseError, match="unknown non-data row"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_malformed_period_year_refuses(tmp_path: Path) -> None:
    # A trailing-digit year (20260) must not truncate to 2026 — refuse.
    path = _write(
        tmp_path,
        [_div("2025-12-10", 2.60)],
        period="reporting period 01-Apr-2025 to 31-Mar-20260",
    )
    with pytest.raises(ParseError, match="reporting period"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_missing_usd_currency_footer_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)], usd_footer=False)
    with pytest.raises(ParseError, match="denominated in US Dollars"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_missing_coverage_line_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)], period="(no period stated)")
    with pytest.raises(ParseError, match="reporting period"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_clean_no_records_sentinel_proves_zero_activity(tmp_path: Path) -> None:
    path = _write(tmp_path, [["No records found"]])
    dividends, withholdings, _ = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert dividends == []
    assert withholdings == []


def test_empty_report_without_sentinel_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [])
    with pytest.raises(ParseError, match="cannot prove zero activity"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_dirty_no_records_sentinel_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [["No records found", "hidden value"]])
    with pytest.raises(ParseError, match="also carries data"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_no_records_sentinel_with_dividend_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [["No records found"], _div("2025-12-10", 2.60)])
    with pytest.raises(ParseError, match="both 'No records found' and dividend"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_corrupt_xlsx_refuses(tmp_path: Path) -> None:
    # A corrupt/non-.xlsx file must yield a controlled ParseError, not a traceback.
    path = tmp_path / "IN - Dividend Report.xlsx"
    path.write_bytes(b"this is not a real xlsx workbook")
    with pytest.raises(ParseError, match="could not open"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_corrupt_worksheet_xml_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A valid zip with corrupt worksheet XML raises xml ...ParseError from openpyxl;
    # it must be wrapped as a controlled ParseError, not escape as a traceback.
    path = _write(tmp_path, [_div("2025-12-10", 2.60)])

    def _boom(*_a: object, **_k: object):
        raise XMLParseError("mismatched tag")

    monkeypatch.setattr(xlsx_mod.openpyxl, "load_workbook", _boom)
    with pytest.raises(ParseError, match="could not open"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_missing_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)], sheet="Wrong Sheet Name")
    with pytest.raises(ParseError, match="no 'IN - Dividend Report' sheet"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_missing_header_refuses(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "IN - Dividend Report"
    ws.append(
        [
            "SAMPLE ISSUER CB, Inc. - IN - Dividend Report (report run on 22-Jun-2026 for "
            "reporting period 01-Jan-1900 to 21-Jun-2026)"
        ]
    )
    ws.append([_dt.datetime(2025, 12, 10), "Dividend Paid", 2.60, "XQCB"])
    path = tmp_path / "IN - Dividend Report.xlsx"
    wb.save(path)
    with pytest.raises(ParseError, match="expected exact header"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_extra_header_column_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)])
    wb = openpyxl.load_workbook(path)
    wb["IN - Dividend Report"].cell(row=2, column=8, value="New Column")
    wb.save(path)
    with pytest.raises(ParseError, match="expected exact header"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_extra_data_column_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [[*_div("2025-12-10", 2.60), "new value"]])
    with pytest.raises(ParseError, match="unexpected populated column"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def _edit_sheet(path: Path, edit: Callable[[str], str]) -> None:
    """Rewrite the report's sheet XML, keeping the rest of the package."""
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    sheet = payloads["xl/worksheets/sheet1.xml"].decode("utf-8")
    edited = edit(sheet)
    assert edited != sheet
    payloads["xl/worksheets/sheet1.xml"] = edited.encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])


_TWO_PAIRS = [
    _div("2025-12-10", 2.60),
    _wht("2025-12-10", -0.65),
    _div("2026-03-11", 4.12),
    _wht("2026-03-11", -1.03),
]


def test_rows_stored_after_a_higher_row_refuse(tmp_path: Path) -> None:
    # Read-only openpyxl skips a row stored after a higher-numbered row, so the
    # second dividend and its withholding would be dropped silently (#961).
    path = _write(tmp_path, _TWO_PAIRS)

    def store_rows_5_and_6_after_the_footer(sheet: str) -> str:
        start, footer, end = (
            sheet.index(tag) for tag in ('<row r="5">', '<row r="7">', "</sheetData>")
        )
        return sheet[:start] + sheet[footer:end] + sheet[start:footer] + sheet[end:]

    _edit_sheet(path, store_rows_5_and_6_after_the_footer)
    with pytest.raises(ParseError, match="could not read the 'IN - Dividend Report' sheet's rows"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_cell_stored_before_a_lower_column_refuses(tmp_path: Path) -> None:
    # Without a <dimension>, read-only openpyxl sizes a row by its last cell in
    # file order, so an H3 stored first would hide from the column check (#961).
    path = _write(tmp_path, [[*_div("2025-12-10", 2.60), "new value"]])
    extra = '<c r="H3" t="inlineStr"><is><t>new value</t></is></c>'
    _edit_sheet(
        path,
        lambda sheet: (
            re.sub(r"<dimension [^>]*/>", "", sheet)
            .replace(extra, "", 1)
            .replace('<row r="3">', f'<row r="3">{extra}', 1)
        ),
    )
    with pytest.raises(ParseError, match="could not read the 'IN - Dividend Report' sheet's rows"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_row_past_the_declared_dimension_is_parsed(tmp_path: Path) -> None:
    # The sheet's <dimension> tag ends at the footer row 7; the dividend in row 8
    # is still read (#961).
    path = _write(
        tmp_path,
        [
            *_TWO_PAIRS,
            [STOCKPLAN_USD_FOOTER],
            _div("2026-05-08", 9.99),
        ],
        usd_footer=False,
    )
    _edit_sheet(
        path, lambda sheet: sheet.replace('<dimension ref="A1:G8" />', '<dimension ref="A1:G7" />')
    )
    dividends, _, _ = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert [d.amount_gross for d in dividends] == [
        Decimal("2.60"),
        Decimal("4.12"),
        Decimal("9.99"),
    ]


def test_column_past_the_declared_dimension_refuses(tmp_path: Path) -> None:
    # The sheet's <dimension> tag ends at column G; the populated H3 is still read (#961).
    path = _write(tmp_path, [[*_div("2025-12-10", 2.60), "new value"]])
    _edit_sheet(
        path, lambda sheet: sheet.replace('<dimension ref="A1:H4" />', '<dimension ref="A1:G4" />')
    )
    with pytest.raises(ParseError, match="unexpected populated column"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


@pytest.mark.parametrize(
    "malformed_cell",
    [
        pytest.param(
            '<c r="A9" t="inlineStr"><is bogus="x"><t>ignored</t></is></c>',
            id="invalid-string-attribute",
        ),
        pytest.param('<c r="A9" t="n"><v>x</v></c>', id="non-numeric-number"),
    ],
)
def test_malformed_content_past_the_declared_dimension_refuses(
    tmp_path: Path, malformed_cell: str
) -> None:
    path = _write(tmp_path, _TWO_PAIRS)
    # The old dimension-limited read stops at empty row 8, leaving malformed row 9 unread.
    _edit_sheet(
        path,
        lambda sheet: sheet.replace(
            "</sheetData>",
            f'<row r="8"/><row r="9">{malformed_cell}</row></sheetData>',
        ),
    )
    with pytest.raises(ParseError, match="could not read"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_shared_string_index_past_the_table_refuses(tmp_path: Path) -> None:
    # A cell naming a shared string the workbook doesn't have refuses as a ParseError,
    # not a raw IndexError.
    path = _write(tmp_path, [_div("2025-12-10", 2.60)])
    _edit_sheet(
        path,
        lambda sheet: sheet.replace(
            '<c r="G3" t="n"><v>0</v></c>', '<c r="G3" t="s"><v>99</v></c>', 1
        ),
    )
    with pytest.raises(ParseError, match="could not read"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_unknown_text_row_refuses_without_its_content(tmp_path: Path) -> None:
    path = _write(tmp_path, [["New report section"]])
    with pytest.raises(ParseError, match="unknown non-data row") as excinfo:
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert excinfo.value.row_index == 2
    assert "New report section" not in str(excinfo.value)


def test_unexpected_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)])
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("New Activity")
    wb.save(path)
    with pytest.raises(ParseError, match="unexpected sheet"):
        parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)


def test_quote_history_sheet_is_allowed(tmp_path: Path) -> None:
    path = _write(tmp_path, [_div("2025-12-10", 2.60)])
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("Quote History").append(["unconsumed source data"])
    wb.save(path)
    dividends, _, _ = parse_stockplan_dividend_report(path, symbol=_SYMBOL, isin=_ISIN)
    assert len(dividends) == 1
