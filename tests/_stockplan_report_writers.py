"""Synthetic StockPlan ``IN - Capital Gain/Loss Report`` and ``IN - Dividend
Report`` workbooks and their rows, shared by the StockPlan capital gain/loss
and dividend report parser tests and the StockPlan number limit tests. The
other StockPlan test helpers write these reports' header, section and footer
rows from here too.

- ``STOCKPLAN_REPORT_SYMBOL``: the stock's ticker.
- ``STOCKPLAN_DIVIDEND_ISIN``: the stock's ISIN, which the dividend report
  parser takes.
- ``RELEASES_HEADER`` and ``CAPITAL_GAINS_HEADER``: the Releases Report and
  Capital Gains Report sheets' column header rows.
- ``CAPITAL_GAINS_SECTION_ROW``: the Capital Gains Report's ``Sale Details`` /
  ``Acquisition Details`` row above its header.
- ``DIVIDEND_REPORT_HEADER``: the dividend report's column header row.
- ``STOCKPLAN_USD_FOOTER``: the reports' US-dollar currency footer.
- ``release_vest_row``: a Releases Report vest row.
- ``write_releases_and_gains_report``: save a capital gain/loss report
  workbook with a Releases Report and a Capital Gains Report sheet.
- ``write_dividend_rows_report``: save a dividend report workbook with data
  rows under ``DIVIDEND_REPORT_HEADER``.
- ``dividend_paid_row``: a ``Dividend Paid`` row.
- ``dividend_withholding_row``: an ``IRS Nonresident Alien Withholding`` row.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import openpyxl

from ibkr_fa.types import Isin, Ticker

STOCKPLAN_REPORT_SYMBOL = Ticker("XQCB")
STOCKPLAN_DIVIDEND_ISIN = Isin("US99971P1012")
RELEASES_HEADER = [
    "Vest Date",
    "Order Number",
    "Plan",
    "Type",
    "Status",
    "Price",
    "Quantity",
    "Net Cash Proceeds",
    "Net Share Proceeds",
    "Tax Payment Method",
]
CAPITAL_GAINS_HEADER = [
    "Sale Date",
    "Sale Price Per Share",
    "Number of Shares Sold",
    "Share Class",
    "Total Gross Proceeds",
    "Acquisition Date",
]
CAPITAL_GAINS_SECTION_ROW = ["Sale Details", None, None, None, None, "Acquisition Details"]
DIVIDEND_REPORT_HEADER = [
    "Entry Date",
    "Activity",
    "Cash",
    "Symbol",
    "Number of Units or Shares",
    "Unit or Share Price",
    "Book Value",
]
STOCKPLAN_USD_FOOTER = "All currencies are in US Dollars unless specified."


def release_vest_row(
    date: str, order: str, fmv: float, gsus: float, deposited: float
) -> list[object]:
    return [
        _dt.datetime.fromisoformat(date),
        order,
        "XQCB",
        "Release",
        "Complete",
        fmv,
        gsus,
        0.0,
        deposited,
        "Fractional Shares",
    ]


def write_releases_and_gains_report(
    tmp_path: Path,
    *,
    vest_rows: list[list[object]] | None = None,
    cg_rows: list[list[object]] | None = None,
    releases_footer: bool = True,
    cg_footer: bool = True,
    releases_sheet: str = "Releases Report",
    cg_run_on: str = "22-Jun-2026",
    cg_period: str = "02-Jun-2025 to 04-May-2026",
) -> Path:
    wb = openpyxl.Workbook()
    rel = wb.active
    rel.title = releases_sheet
    rel.append(["SAMPLE ISSUER CB, Inc. - Releases Report (report run on 22-Jun-2026)"])
    rel.append(RELEASES_HEADER)
    for row in vest_rows or []:
        rel.append(row)
    if releases_footer:
        rel.append([STOCKPLAN_USD_FOOTER])

    cg = wb.create_sheet("Capital Gains Report")
    cg.append(
        [
            f"SAMPLE ISSUER CB, Inc. - Capital Gains Report (report run on {cg_run_on} "
            f"for reporting period {cg_period})"
        ]
    )
    cg.append(CAPITAL_GAINS_SECTION_ROW)
    cg.append(CAPITAL_GAINS_HEADER)
    for row in cg_rows if cg_rows is not None else [["No records found"]]:
        cg.append(row)
    if cg_footer:
        cg.append([STOCKPLAN_USD_FOOTER])

    path = tmp_path / "IN - Capital Gain_Loss Report.xlsx"
    wb.save(path)
    return path


def write_dividend_rows_report(
    tmp_path: Path,
    data_rows: list[list[object]],
    *,
    sheet: str = "IN - Dividend Report",
    prefix: str = "SAMPLE ISSUER CB, Inc.",
    run_on: str = "22-Jun-2026",
    period: str = "reporting period 01-Jan-1900 to 21-Jun-2026",
    usd_footer: bool = True,
    currency_footer: str = STOCKPLAN_USD_FOOTER,
) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append([f"{prefix} - IN - Dividend Report (report run on {run_on} for {period})"])
    ws.append(DIVIDEND_REPORT_HEADER)
    for row in data_rows:
        ws.append(row)
    if usd_footer:
        ws.append([currency_footer])
    path = tmp_path / "IN - Dividend Report.xlsx"
    wb.save(path)
    return path


def dividend_paid_row(date: str, cash: float) -> list[object]:
    return [_dt.datetime.fromisoformat(date), "Dividend Paid", cash, "XQCB", None, None, 0.0]


def dividend_withholding_row(date: str, cash: float) -> list[object]:
    return [
        _dt.datetime.fromisoformat(date),
        "IRS Nonresident Alien Withholding",
        cash,
        "XQCB",
        None,
        None,
        0.0,
    ]
