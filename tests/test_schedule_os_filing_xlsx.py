"""Tests for the readable Schedule OS filing workbook."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import DistributionKind
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output.filing_xlsx import FilingXlsx
from ibkr_fa.output.schedules.os import filing_workbook_sheets
from ibkr_fa.types import Isin, Ticker


def _write(
    result: ScheduleOsComputeResult,
    output: Path,
    *,
    source_label: str | None = None,
    account_labels: dict[int, str] | None = None,
) -> None:
    output_binding(Subcommand.OS, "filing-xlsx").writer(
        RenderContext(2026, source_label=source_label, account_labels=account_labels)
    ).write(result, output)


def _result() -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(
        lines=[
            ScheduleOsDividendLine(
                symbol=Ticker("XQCA"),
                isin=Isin("US99972B1017"),
                issuer_country_iso2="US",
                payment_date=date(2026, 3, 5),
                fx_specified_date=date(2026, 2, 28),
                fx_rate_publication_date=date(2026, 2, 27),
                fx_rate_inr_per_usd=Decimal("90.56"),
                amount_gross_usd=Decimal("1.39"),
                amount_gross_inr=Decimal("125.8784"),  # 1.39 x 90.56
                description="XQCA(US99972B1017) Cash Dividend USD 0.40 per Share",
                kind=DistributionKind.CASH_DIVIDEND,
                source_account_index=0,
            )
        ],
        domestic_lines=[],
        interest_total_inr_rounded=Decimal("280"),
        other_income_lines=(
            ScheduleOsOtherIncomeLine(
                nature="Securities lending fee",
                payment_date=date(2025, 6, 1),
                fx_specified_date=date(2026, 3, 31),
                fx_rate_publication_date=date(2026, 3, 31),
                fx_rate_inr_per_usd=Decimal("80"),
                amount_gross_usd=Decimal("0.11"),
                amount_gross_inr=Decimal("8.80"),
                description="Stock Lending",
                source_row_index=1,
            ),
        ),
    )


def test_filing_xlsx_is_registered_and_readable(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.xlsx"
    _write(_result(), output, source_label="Robinhood", account_labels={0: "Individual"})

    assert output_binding(Subcommand.OS, "filing-xlsx").target == FilingXlsx(filing_workbook_sheets)
    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == [
        "ITR Schedule OS",
        "Quicko by Account",
        "Quicko by Security",
    ]

    itr = workbook["ITR Schedule OS"]
    assert itr["A3"].value == "1ai. Dividend income [other than (ii) and (iii)]"
    assert itr["B3"].value == 126
    assert itr["B4"].value == 280
    assert itr["B5"].value == 9
    assert itr["A7"].value.startswith("Schedule OS / 10.")
    assert [itr.cell(10, column).value for column in range(3, 8)] == [0, 0, 0, 126, 0]
    assert itr.column_dimensions["A"].width == 62
    assert "B1:G1" in {str(item) for item in itr.merged_cells.ranges}
    assert itr["A7"].alignment.wrap_text
    assert itr.freeze_panes == "A2"
    assert itr.page_setup.fitToWidth == 1
    assert itr.sheet_properties.pageSetUpPr.fitToPage

    account = workbook["Quicko by Account"]
    assert account.max_row == 3
    assert [account.cell(3, column).value for column in range(1, 12)] == [
        "Individual",
        "Foreign Dividend Income",
        "United States of America",
        "Robinhood Individual account",
        0,
        0,
        0,
        126,
        0,
        126,
        None,
    ]
    assert account.freeze_panes == "A3"
    assert account.auto_filter.ref == "A2:K3"
    assert "never both" in account["A1"].value

    security = workbook["Quicko by Security"]
    assert security.max_row == 3
    assert [security.cell(3, column).value for column in range(1, 11)] == [
        "Foreign Dividend Income",
        "United States of America",
        "XQCA - Robinhood",
        0,
        0,
        0,
        126,
        0,
        126,
        None,
    ]
    assert security.freeze_panes == "A3"
    assert security.auto_filter.ref == "A2:J3"
    assert security.page_setup.fitToWidth == 1
    assert security.sheet_properties.pageSetUpPr.fitToPage
    assert "never both" in security["A1"].value
    workbook.close()
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_filing_xlsx_keeps_domestic_quicko_expense_instruction(
    tmp_path: Path,
) -> None:
    domestic = ScheduleOsDomesticDividendLine(
        symbol=Ticker("XQCD"),
        isin=Isin("IN99970W1077"),
        issuer_country_iso2="IN",
        payment_date=date(2025, 7, 1),
        fx_specified_date=date(2025, 6, 30),
        fx_rate_publication_date=date(2025, 6, 30),
        fx_rate_inr_per_usd=Decimal("83.50"),
        amount_gross_usd=Decimal("12.34"),
        amount_gross_inr=Decimal("1030.39"),
        description="XQCD(IN99970W1077) Cash Dividend USD 0.21 per Share",
        kind=DistributionKind.CASH_DIVIDEND,
        source_account_index=0,
    )
    output = tmp_path / "schedule-os.xlsx"
    _write(replace(_result(), domestic_lines=[domestic]), output)

    workbook = load_workbook(output, data_only=True)
    worksheet = workbook["Quicko by Security"]
    domestic_row = next(
        row
        for row in worksheet.iter_rows(min_row=2, values_only=True)
        if row[0] == "Dividend Income (Indian-source only)"
    )
    assert domestic_row[-2:] == (1030, "not computed")
    account = workbook["Quicko by Account"]
    assert account["A3"].value == "Account index 0"
    assert account["D3"].value == "Source account index 0"
    workbook.close()


def test_filing_xlsx_groups_accounts_by_country_and_reconciles_both_views(
    tmp_path: Path,
) -> None:
    base = _result()
    us_line = base.lines[0]
    result = replace(
        base,
        lines=[
            us_line,
            replace(us_line, source_account_index=1),
            replace(
                us_line,
                symbol=Ticker("XQDL"),
                isin=Isin("IE99970T1048"),
                issuer_country_iso2="IE",
            ),
        ],
    )
    output = tmp_path / "schedule-os.xlsx"
    _write(result, output, account_labels={0: "Individual", 1: "Managed Individual"})

    workbook = load_workbook(output, data_only=True)
    account = workbook["Quicko by Account"]
    security = workbook["Quicko by Security"]
    assert [
        (account.cell(row, 1).value, account.cell(row, 3).value)
        for row in range(3, account.max_row + 1)
    ] == [
        ("Individual", "Ireland"),
        ("Individual", "United States of America"),
        ("Managed Individual", "United States of America"),
    ]
    assert account.max_row - 2 == 3
    assert security.max_row - 2 == 2
    # Three lines of 125.8784 = 377.6352 -> 378.
    assert [
        sum(account.cell(row, column).value for row in range(3, 6)) for column in range(5, 10)
    ] == [
        0,
        0,
        0,
        378,
        0,
    ]
    assert [
        sum(security.cell(row, column).value for row in range(3, 5)) for column in range(4, 9)
    ] == [0, 0, 0, 378, 0]
    assert sum(account.cell(row, 10).value for row in range(3, 6)) == 378
    assert sum(security.cell(row, 9).value for row in range(3, 5)) == 378
    workbook.close()


def test_filing_xlsx_disambiguates_duplicate_account_types(tmp_path: Path) -> None:
    base = _result()
    result = replace(
        base,
        lines=[base.lines[0], replace(base.lines[0], source_account_index=1)],
    )
    output = tmp_path / "schedule-os.xlsx"
    _write(
        result, output, source_label="Robinhood", account_labels={0: "Individual", 1: "Individual"}
    )

    workbook = load_workbook(output, data_only=True)
    account = workbook["Quicko by Account"]
    assert [account.cell(row, 1).value for row in range(3, 5)] == [
        "Individual (index 0)",
        "Individual (index 1)",
    ]
    assert [account.cell(row, 4).value for row in range(3, 5)] == [
        "Robinhood Individual account (index 0)",
        "Robinhood Individual account (index 1)",
    ]
    workbook.close()


def test_filing_xlsx_omits_account_view_without_account_metadata(tmp_path: Path) -> None:
    result = _result()
    result = replace(
        result,
        lines=[replace(result.lines[0], source_account_index=None)],
    )
    output = tmp_path / "schedule-os.xlsx"
    _write(result, output)

    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == ["ITR Schedule OS", "Quicko by Security"]
    assert "never both" not in workbook["Quicko by Security"]["A1"].value
    assert "reconcile" in workbook["Quicko by Security"]["A1"].value
    workbook.close()


def test_filing_xlsx_rejects_partial_account_metadata_without_output(
    tmp_path: Path,
) -> None:
    result = _result()
    result = replace(
        result,
        lines=[*result.lines, replace(result.lines[0], source_account_index=None)],
    )
    output = tmp_path / "schedule-os.xlsx"

    with pytest.raises(InvariantError, match="account metadata on every dividend line"):
        _write(result, output)
    assert not output.exists()


def test_filing_xlsx_rejects_uncomputed_interest_without_output(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.xlsx"
    with pytest.raises(InvariantError, match="separately computed interest total"):
        _write(replace(_result(), interest_total_inr_rounded=None), output)
    assert not output.exists()
