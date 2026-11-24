"""Tests for the Morgan Stanley StockPlan "IN - Capital Gain/Loss Report" parser
(Releases Report vests + Capital Gains Report sales) — #365.

Uses synthetic openpyxl workbooks in the report's layout.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.stockplan.capital_gain_loss import (
    StockplanSale,
    StockplanVest,
    parse_stockplan_capital_gains_report,
    parse_stockplan_releases,
)
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_no_markers
from tests._stockplan_fa_cases import FUTURE_VEST_REFUSAL
from tests._stockplan_report_writers import CAPITAL_GAINS_HEADER as _CG_HEADER
from tests._stockplan_report_writers import CAPITAL_GAINS_SECTION_ROW
from tests._stockplan_report_writers import RELEASES_HEADER as _REL_HEADER
from tests._stockplan_report_writers import STOCKPLAN_REPORT_SYMBOL as _SYMBOL
from tests._stockplan_report_writers import STOCKPLAN_USD_FOOTER as _USD_FOOTER
from tests._stockplan_report_writers import release_vest_row as _vest_row
from tests._stockplan_report_writers import write_releases_and_gains_report as _write


def _parse_cg(path: Path) -> list[StockplanSale]:
    return parse_stockplan_capital_gains_report(path, symbol=_SYMBOL)[0]


def test_parses_releases_vests(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        vest_rows=[
            _vest_row("2025-08-14", "RB0000A1", 196.42, 9.012, 6.214),
            _vest_row("2025-11-14", "RB0000B2", 224.63, 4.506, 3.108),
        ],
    )
    vests, run_on = parse_stockplan_releases(path, symbol=_SYMBOL)
    assert run_on == _dt.date(2026, 6, 22)
    assert [
        (v.vest_date.isoformat(), str(v.fmv_per_share), str(v.shares_deposited)) for v in vests
    ] == [
        ("2025-08-14", "196.42", "6.214"),
        ("2025-11-14", "224.63", "3.108"),
    ]
    assert vests[0].order_number == "RB0000A1"


def test_non_release_type_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)
    row[3] = "Sale"  # Type
    with pytest.raises(ParseError, match="only 'Release'"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_incomplete_status_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)
    row[4] = "Pending"  # Status
    with pytest.raises(ParseError, match="not 'Complete'"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_wrong_plan_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)
    row[2] = "XCLS"  # Plan
    with pytest.raises(ParseError, match="row Plan does not match the configured"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_duplicate_order_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        vest_rows=[
            _vest_row("2025-08-14", "DUP", 196.42, 10.0, 6.214),
            _vest_row("2025-09-15", "DUP", 203.18, 5.0, 3.107),
        ],
    )
    with pytest.raises(ParseError, match="duplicate vest Order Number"):
        parse_stockplan_releases(path, symbol=_SYMBOL)


def test_blank_order_number_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "", 196.42, 10.0, 6.214)
    with pytest.raises(ParseError, match="blank Order Number"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_nonzero_release_cash_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)
    row[7] = 1.25
    with pytest.raises(ParseError, match="non-zero release cash"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_unknown_tax_payment_method_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)
    row[9] = "Cash"
    with pytest.raises(ParseError, match="unexpected Tax Payment Method"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_nonpositive_fmv_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 0.0, 10.0, 6.214)
    with pytest.raises(ParseError, match="must be positive"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_releases_non_finite_number_refuses(tmp_path: Path, text: str) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 9.012, 6.214)
    row[5] = text
    with pytest.raises(ParseError, match="non-finite Price") as excinfo:
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Releases Report",
        2,
        "Price",
        "",
    )


def test_deposited_exceeds_gsus_refuses(tmp_path: Path) -> None:
    row = _vest_row("2025-08-14", "X1", 196.42, 5.0, 6.214)  # deposited > gsus
    with pytest.raises(ParseError, match="must be > 0 and <="):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_releases_missing_usd_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        vest_rows=[_vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)],
        releases_footer=False,
    )
    with pytest.raises(ParseError, match="denominated in US Dollars"):
        parse_stockplan_releases(path, symbol=_SYMBOL)


def test_missing_releases_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, vest_rows=[], releases_sheet="Wrong Sheet")
    with pytest.raises(ParseError, match="has no 'Releases Report' sheet"):
        parse_stockplan_releases(path, symbol=_SYMBOL)


def test_malformed_release_row_refuses(tmp_path: Path) -> None:
    # A data-looking row (populated past col 0) missing its Type must REFUSE,
    # not be silently skipped (locks the is_data_row content-based classification).
    row: list[object] = [
        _dt.datetime(2025, 8, 14),
        "X1",
        "XQCB",
        "",
        "Complete",
        196.42,
        10.0,
        0.0,
        6.214,
        "",
    ]
    with pytest.raises(ParseError, match="only 'Release'"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_date_only_release_row_refuses(tmp_path: Path) -> None:
    # A row with ONLY a vest date in col 0 (a truncated transaction) must REFUSE,
    # not be silently skipped — a bare date is a data-row signal.
    row: list[object] = [
        _dt.datetime(2025, 8, 14),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    with pytest.raises(ParseError, match="blank Order Number"):
        parse_stockplan_releases(_write(tmp_path, vest_rows=[row]), symbol=_SYMBOL)


def test_extra_releases_header_column_refuses(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        vest_rows=[_vest_row("2025-08-14", "X1", 196.42, 10.0, 6.214)],
    )
    wb = openpyxl.load_workbook(path)
    wb["Releases Report"].cell(row=2, column=11, value="New Column")
    wb.save(path)
    with pytest.raises(ParseError, match="expected exact header"):
        parse_stockplan_releases(path, symbol=_SYMBOL)


def test_unknown_releases_text_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, vest_rows=[["New releases section"]])
    with pytest.raises(ParseError, match="unknown non-data row") as excinfo:
        parse_stockplan_releases(path, symbol=_SYMBOL)
    assert excinfo.value.row_index == 2
    assert "New releases section" not in str(excinfo.value)


def test_unexpected_workbook_sheet_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path)
    wb = openpyxl.load_workbook(path)
    wb.create_sheet("New Transactions")
    wb.save(path)
    with pytest.raises(ParseError, match="unexpected sheet"):
        parse_stockplan_releases(path, symbol=_SYMBOL)


def test_no_sentinel_no_sales_refuses(tmp_path: Path) -> None:
    # A Capital Gains sheet with neither sale rows nor the "No records found"
    # sentinel (e.g. truncated) cannot confirm zero sales — refuse.
    path = _write(tmp_path, vest_rows=[], cg_rows=[])
    with pytest.raises(ParseError, match="cannot confirm zero sales"):
        _parse_cg(path)


def test_capital_gains_no_records(tmp_path: Path) -> None:
    path = _write(tmp_path, vest_rows=[], cg_rows=[["No records found"]])
    assert _parse_cg(path) == []


@pytest.mark.parametrize("run_on", ["01-Feb-2026", "31-Mar-2026"])
def test_capital_gains_run_on_or_before_coverage_end_refuses(tmp_path: Path, run_on: str) -> None:
    # A report run before its period ends, or on its last day, can miss later sales,
    # so its "No records found" cannot prove zero sales for the whole period (#680).
    path = _write(tmp_path, cg_run_on=run_on, cg_period="01-Apr-2025 to 31-Mar-2026")
    with pytest.raises(ParseError) as excinfo:
        _parse_cg(path)
    assert excinfo.value.message == (
        "IN - Capital Gain_Loss Report.xlsx was not run strictly after its reporting period "
        "ended, so it can miss later sales; re-export the report after the period ends"
    )


def test_capital_gains_run_after_coverage_end_accepted(tmp_path: Path) -> None:
    path = _write(tmp_path, cg_run_on="01-Apr-2026", cg_period="01-Apr-2025 to 31-Mar-2026")
    assert parse_stockplan_capital_gains_report(path, symbol=_SYMBOL) == (
        [],
        (_dt.date(2025, 4, 1), _dt.date(2026, 3, 31)),
    )


@pytest.mark.parametrize("run_on", ["31-Feb-2026", "01-Foo-2026"])
def test_capital_gains_malformed_run_date_refuses(tmp_path: Path, run_on: str) -> None:
    path = _write(tmp_path, cg_run_on=run_on, cg_period="01-Apr-2025 to 31-Mar-2026")
    with pytest.raises(ParseError, match="malformed Capital Gains report date in"):
        _parse_cg(path)


def test_capital_gains_parses_sale(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XQCB",
        380.0,
        _dt.datetime(2025, 8, 14),
    ]
    path = _write(tmp_path, vest_rows=[], cg_rows=[sale])
    sales = _parse_cg(path)
    assert len(sales) == 1
    assert sales[0].shares_sold == Decimal("2")
    assert sales[0].gross_proceeds == Decimal("380")
    assert sales[0].acquisition_date == _dt.date(2025, 8, 14)


def test_share_class_mismatch_refuses(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XCLS",
        380.0,
        _dt.datetime(2025, 8, 14),
    ]
    with pytest.raises(ParseError, match="row Share Class does not match the configured"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))


def test_negative_shares_sold_refuses(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        -2.0,
        "XQCB",
        380.0,
        _dt.datetime(2025, 8, 14),
    ]
    with pytest.raises(ParseError, match="Number of Shares Sold must be positive"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))


def test_negative_gross_proceeds_refuses(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XQCB",
        -380.0,
        _dt.datetime(2025, 8, 14),
    ]
    with pytest.raises(ParseError, match="Total Gross Proceeds must not be negative"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_capital_gains_non_finite_number_refuses(tmp_path: Path, text: str) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XQCB",
        text,
        _dt.datetime(2025, 8, 14),
    ]
    with pytest.raises(ParseError, match="non-finite Total Gross Proceeds") as excinfo:
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))
    error = excinfo.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Capital Gains Report",
        3,
        "Total Gross Proceeds",
        "",
    )


def test_non_date_sale_date_refuses(tmp_path: Path) -> None:
    sale: list[object] = ["2026-02-10", 190.0, 2.0, "XQCB", 380.0, _dt.datetime(2025, 8, 14)]
    with pytest.raises(ParseError, match="non-date Sale Date"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))


def test_extra_capital_gains_data_column_refuses(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XQCB",
        380.0,
        _dt.datetime(2025, 8, 14),
        "new value",
    ]
    with pytest.raises(ParseError, match="unexpected populated column"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[sale]))


def test_unknown_capital_gains_text_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path, cg_rows=[["New sale section"]])
    with pytest.raises(ParseError, match="unknown non-data row") as excinfo:
        _parse_cg(path)
    assert excinfo.value.row_index == 3
    assert "New sale section" not in str(excinfo.value)


def test_changed_capital_gains_section_row_refuses(tmp_path: Path) -> None:
    path = _write(tmp_path)
    wb = openpyxl.load_workbook(path)
    wb["Capital Gains Report"].cell(row=2, column=1, value="Sales")
    wb.save(path)
    with pytest.raises(ParseError, match="section row"):
        _parse_cg(path)


def test_sentinel_with_sale_refuses(tmp_path: Path) -> None:
    sale: list[object] = [
        _dt.datetime(2026, 2, 10),
        190.0,
        2.0,
        "XQCB",
        380.0,
        _dt.datetime(2025, 8, 14),
    ]
    path = _write(tmp_path, vest_rows=[], cg_rows=[sale, ["No records found"]])
    with pytest.raises(ParseError, match="BOTH a 'No records found' sentinel and sale"):
        _parse_cg(path)


def test_dirty_sentinel_row_refuses(tmp_path: Path) -> None:
    # A "No records found" row that also carries sale figures beyond col 0 is
    # corrupt and must refuse, not be treated as the clean no-sales sentinel.
    dirty: list[object] = [
        "No records found",
        190.0,
        2.0,
        "XQCB",
        380.0,
        _dt.datetime(2025, 8, 14),
    ]
    with pytest.raises(ParseError, match="also carries sale data"):
        _parse_cg(_write(tmp_path, vest_rows=[], cg_rows=[dirty]))


# --- Events dated after their sheet's run date (#1176) --------------------------

_SP_SYMBOL = Ticker("QXSP")
_SP_TITLE = "Synthetic Title SP01"
_SP_ORDER = "SYNTH-ORDER-SP01"
_SP_PLAN = "Synthetic Plan SP01"
_SP_AMOUNT = 137.2468
_SP_FILE = f"IN - Capital Gain_Loss Report - {_SP_TITLE}.xlsx"
_SP_RUN_ON = "22-Jun-2026"
_SP_ENDED_PERIOD = "01-Jan-2025 to 31-Dec-2025"
_SP_FUTURE = _dt.datetime(2026, 6, 23)
_SP_SAME_DAY = _dt.datetime(2026, 6, 22)
_SP_EARLIER = _dt.datetime(2026, 6, 1)
_SP_ACQUIRED = _dt.datetime(2025, 6, 2)
# Synthetic values shared by every future-event workbook; none may reach a refusal.
_SP_MARKERS = (
    _SP_TITLE,
    "QXSP",
    "137.2468",
    _SP_RUN_ON,
    "2026-06-22",
    "23-Jun-2026",
    "2026-06-23",
    "01-Jan-2025",
    "31-Dec-2025",
)
_SALE_AFTER_RUN = (
    "ParseError in section 'Capital Gains Report' at row {row}, column 'Sale Date': "
    "sale date must not be after this sheet's report run date"
)
_ACQUISITION_AFTER_RUN = (
    "ParseError in section 'Capital Gains Report' at row {row}, column 'Acquisition Date': "
    "acquisition date must not be after this sheet's report run date"
)


def _sp_vest(day: _dt.datetime, order: str = _SP_ORDER) -> list[object]:
    return [
        day,
        order,
        "QXSP",
        "Release",
        "Complete",
        _SP_AMOUNT,
        2.0,
        0.0,
        1.0,
        "Fractional Shares",
    ]


def _sp_sale(sold: _dt.datetime, acquired: _dt.datetime = _SP_ACQUIRED) -> list[object]:
    return [sold, _SP_AMOUNT, 1.0, "QXSP", _SP_AMOUNT, acquired]


def _sp_book(
    tmp_path: Path,
    *,
    vests: tuple[list[object], ...] = (),
    sales: tuple[list[object], ...] = (["No records found"],),
    period: str = _SP_ENDED_PERIOD,
    releases_footer: bool = True,
    cg_footer: bool = True,
) -> Path:
    """A synthetic workbook whose two titles are both run on ``_SP_RUN_ON``."""
    wb = openpyxl.Workbook()
    rel = wb.active
    rel.title = "Releases Report"
    rel.append([f"{_SP_TITLE} - Releases Report (report run on {_SP_RUN_ON})"])
    rel.append(_REL_HEADER)
    for row in vests:
        rel.append(row)
    if releases_footer:
        rel.append([_USD_FOOTER])
    cg = wb.create_sheet("Capital Gains Report")
    cg.append(
        [
            f"{_SP_TITLE} - Capital Gains Report (report run on {_SP_RUN_ON} "
            f"for reporting period {period})"
        ]
    )
    cg.append(CAPITAL_GAINS_SECTION_ROW)
    cg.append(_CG_HEADER)
    for row in sales:
        cg.append(row)
    if cg_footer:
        cg.append([_USD_FOOTER])
    path = tmp_path / _SP_FILE
    wb.save(path)
    return path


def _assert_new_refusal(error: ParseError, rendered: str, *markers: str) -> None:
    """``error`` is one of #1176's fixed refusals: complete text, no value, no chain."""
    assert str(error) == rendered
    assert error.raw_value == ""
    assert error.__cause__ is None
    assert error.__context__ is None
    assert_no_markers(str(error), *_SP_MARKERS, *markers, _SP_FILE)


@pytest.mark.parametrize(
    ("vests", "row", "markers"),
    [
        pytest.param((_sp_vest(_SP_FUTURE),), 2, (_SP_ORDER,), id="first-row"),
        pytest.param(
            ([], _sp_vest(_SP_EARLIER, "SYNTH-ORDER-SP00"), [], _sp_vest(_SP_FUTURE)),
            5,
            (_SP_ORDER, "SYNTH-ORDER-SP00", "2026-06-01"),
            id="after-blank-rows",
        ),
    ],
)
def test_vest_after_the_run_date_refuses(
    tmp_path: Path, vests: tuple[list[object], ...], row: int, markers: tuple[str, ...]
) -> None:
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_releases(_sp_book(tmp_path, vests=vests), symbol=_SP_SYMBOL)
    _assert_new_refusal(excinfo.value, FUTURE_VEST_REFUSAL.format(row=row), *markers)


@pytest.mark.parametrize(
    ("sales", "rendered", "markers"),
    [
        pytest.param(
            (_sp_sale(_SP_FUTURE),), _SALE_AFTER_RUN.format(row=3), ("2025-06-02",), id="sale"
        ),
        pytest.param(
            (_sp_sale(_SP_EARLIER, _SP_FUTURE),),
            _ACQUISITION_AFTER_RUN.format(row=3),
            ("2026-06-01",),
            id="acquisition",
        ),
        pytest.param(
            (_sp_sale(_SP_FUTURE, _SP_FUTURE),),
            _SALE_AFTER_RUN.format(row=3),
            (),
            id="both-sale-first",
        ),
        pytest.param(
            ([], _sp_sale(_SP_EARLIER), [], _sp_sale(_SP_EARLIER, _SP_FUTURE)),
            _ACQUISITION_AFTER_RUN.format(row=6),
            ("2026-06-01", "2025-06-02"),
            id="after-blank-rows",
        ),
    ],
)
def test_sale_event_after_the_run_date_refuses(
    tmp_path: Path, sales: tuple[list[object], ...], rendered: str, markers: tuple[str, ...]
) -> None:
    # A valid title whose reporting period ended before the run (#1176).
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_capital_gains_report(_sp_book(tmp_path, sales=sales), symbol=_SP_SYMBOL)
    _assert_new_refusal(excinfo.value, rendered, *markers)


def test_earlier_and_same_day_events_parse_as_before(tmp_path: Path) -> None:
    # The titles give no run time, so an event on the run date itself passes.
    path = _sp_book(
        tmp_path,
        vests=(_sp_vest(_SP_EARLIER, "SYNTH-ORDER-SP00"), _sp_vest(_SP_SAME_DAY)),
        sales=(_sp_sale(_SP_EARLIER), _sp_sale(_SP_SAME_DAY, _SP_SAME_DAY)),
    )
    amount = Decimal(str(_SP_AMOUNT))
    assert parse_stockplan_releases(path, symbol=_SP_SYMBOL) == (
        [
            StockplanVest(_SP_EARLIER.date(), "SYNTH-ORDER-SP00", amount, Decimal(2), Decimal(1)),
            StockplanVest(_SP_SAME_DAY.date(), _SP_ORDER, amount, Decimal(2), Decimal(1)),
        ],
        _SP_SAME_DAY.date(),
    )
    assert parse_stockplan_capital_gains_report(path, symbol=_SP_SYMBOL) == (
        [
            StockplanSale(
                _SP_EARLIER.date(), amount, Decimal(1), "QXSP", amount, _SP_ACQUIRED.date()
            ),
            StockplanSale(
                _SP_SAME_DAY.date(), amount, Decimal(1), "QXSP", amount, _SP_SAME_DAY.date()
            ),
        ],
        (_dt.date(2025, 1, 1), _dt.date(2025, 12, 31)),
    )


def _future_vest_with(column: int, value: object) -> list[object]:
    row = _sp_vest(_SP_FUTURE)
    row[column] = value
    return row


@pytest.mark.parametrize(
    ("book", "rendered"),
    [
        pytest.param(
            {"vests": (_future_vest_with(3, "Sale"),)},
            "ParseError in section 'Releases Report' at row 2, column 'Type': "
            "unexpected row Type; only 'Release' rows are supported",
            id="type",
        ),
        pytest.param(
            {"vests": (_future_vest_with(4, "Pending"),)},
            "ParseError in section 'Releases Report' at row 2, column 'Status': "
            "vest Status is not 'Complete'",
            id="status",
        ),
        pytest.param(
            {"vests": (_future_vest_with(2, _SP_PLAN),)},
            "ParseError in section 'Releases Report' at row 2, column 'Plan': "
            "row Plan does not match the configured [stockplan.security] symbol",
            id="identity",
        ),
        pytest.param(
            {"vests": (_future_vest_with(5, -_SP_AMOUNT),)},
            "ParseError in section 'Releases Report' at row 2, column 'Price': "
            "Fair Market Value (Price) must be positive",
            id="figure",
        ),
        pytest.param(
            {"vests": (_sp_vest(_SP_FUTURE), _future_vest_with(3, "Sale"))},
            "ParseError in section 'Releases Report' at row 3, column 'Type': "
            "unexpected row Type; only 'Release' rows are supported",
            id="later-row",
        ),
        pytest.param(
            {"vests": (_sp_vest(_SP_FUTURE),), "releases_footer": False},
            "ParseError in section 'Releases Report' at row 0: could not uniquely confirm "
            f"{_SP_FILE} is denominated in US Dollars (expected exactly one "
            f"{_USD_FOOTER!r} statement, found 0)",
            id="currency",
        ),
    ],
)
def test_earlier_releases_refusals_win_over_a_vest_after_the_run_date(
    tmp_path: Path, book: dict[str, Any], rendered: str
) -> None:
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_releases(_sp_book(tmp_path, **book), symbol=_SP_SYMBOL)
    assert str(excinfo.value) == rendered


@pytest.mark.parametrize(
    ("book", "rendered"),
    [
        pytest.param(
            {"sales": ([*_sp_sale(_SP_FUTURE)[:3], _SP_PLAN, *_sp_sale(_SP_FUTURE)[4:]],)},
            "ParseError in section 'Capital Gains Report' at row 3, column 'Share Class': "
            "row Share Class does not match the configured [stockplan.security] symbol",
            id="identity",
        ),
        pytest.param(
            {"sales": ([*_sp_sale(_SP_FUTURE)[:2], -1.0, *_sp_sale(_SP_FUTURE)[3:]],)},
            "ParseError in section 'Capital Gains Report' at row 3, "
            "column 'Number of Shares Sold': Number of Shares Sold must be positive",
            id="figure",
        ),
        pytest.param(
            {"sales": (_sp_sale(_SP_FUTURE), ["No records found"])},
            "ParseError in section 'Capital Gains Report' at row 2: "
            f"{_SP_FILE} has BOTH a 'No records found' sentinel and sale row(s) — "
            "ambiguous/corrupt; refusing rather than risk under-reporting sales",
            id="zero-sales",
        ),
        pytest.param(
            {"sales": (_sp_sale(_SP_FUTURE),), "cg_footer": False},
            "ParseError in section 'Capital Gains Report' at row 0: could not uniquely "
            f"confirm {_SP_FILE} is denominated in US Dollars (expected exactly one "
            f"{_USD_FOOTER!r} statement, found 0)",
            id="currency",
        ),
        pytest.param(
            {"sales": (_sp_sale(_SP_FUTURE),), "period": "01-Jan-2025 to 22-Jun-2026"},
            "ParseError in section 'Capital Gains Report' at row 0: "
            f"{_SP_FILE} was not run strictly after its reporting period ended, so it can "
            "miss later sales; re-export the report after the period ends",
            id="coverage",
        ),
        pytest.param(
            {"sales": (_sp_sale(_SP_FUTURE),), "period": "31-Dec-2025 to 01-Jan-2025"},
            "ParseError in section 'Capital Gains Report' at row 0: "
            "Capital Gains reporting period ends before it starts",
            id="inverted-period",
        ),
    ],
)
def test_earlier_capital_gains_refusals_win_over_a_sale_after_the_run_date(
    tmp_path: Path, book: dict[str, Any], rendered: str
) -> None:
    with pytest.raises(ParseError) as excinfo:
        parse_stockplan_capital_gains_report(_sp_book(tmp_path, **book), symbol=_SP_SYMBOL)
    assert str(excinfo.value) == rendered
