"""Tests for the filing-only Schedule OS CSV."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
    ScheduleOsDomesticDividendLine,
)
from ibkr_fa.compute.schedule_os_other_income import ScheduleOsOtherIncomeLine
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models import DistributionKind
from ibkr_fa.output import RenderContext, Subcommand, output_binding
from ibkr_fa.output.filing_csv import FILING_WORKSHEET, PortalCsv
from ibkr_fa.output.schedules.os import filing_csv_rows
from ibkr_fa.types import Isin, Ticker


def _write(result: ScheduleOsComputeResult, output: Path, source_label: str | None = None) -> None:
    output_binding(Subcommand.OS, "filing-csv").writer(
        RenderContext(2026, source_label=source_label)
    ).write(result, output)


def _result() -> ScheduleOsComputeResult:
    return ScheduleOsComputeResult(
        lines=[
            ScheduleOsDividendLine(
                symbol=Ticker("XESP"),
                isin=Isin("US99999E1047"),
                issuer_country_iso2="US",
                payment_date=date(2026, 3, 5),
                fx_specified_date=date(2026, 2, 28),
                fx_rate_publication_date=date(2026, 2, 27),
                fx_rate_inr_per_usd=Decimal("90.56"),
                amount_gross_usd=Decimal("1.39"),
                amount_gross_inr=Decimal("125.8784"),  # 1.39 x 90.56
                description="XESP(US99999E1047) Cash Dividend USD 0.40 per Share",
                kind=DistributionKind.CASH_DIVIDEND,
            )
        ],
        domestic_lines=[],
        interest_total_inr_rounded=Decimal("0"),
    )


def test_filing_csv_matches_itr_and_quicko_fields(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.csv"
    _write(_result(), output, "Fidelity NetBenefits stock plan")
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    assert rows == [
        [
            "Schedule OS / 1. Gross income chargeable to tax at normal applicable rates "
            "(1a+ 1b+ 1c+ 1d + 1e)",
            "IMPORTANT: ADD every ITR amount in this file to existing Schedule OS values "
            "from Indian income, other accounts, or other sources; do not replace totals.",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Field",
            "Amount to ADD",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "1ai. Dividend income [other than (ii) and (iii)]",
            "126",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "1b(ix). Others including interest from Companies, NBFCs & HFCs",
            "0",
            "",
            "",
            "",
            "",
            "",
        ],
        ["", "", "", "", "", "", ""],
        [
            "Schedule OS / 10. Information about accrual/receipt of income from Other Sources",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "IMPORTANT: ADD these gross period amounts to any existing/prefilled "
            "Schedule OS values, then subtract any row 2f DTAA dividend and section "
            "57(1) interest expenditure; neither subtraction is computed here.",
            "",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Sl No.",
            "Other Source Income",
            "Upto 15/6",
            "From 16/6 to 15/9",
            "From 16/9 to 15/12",
            "From 16/12 to 15/3",
            "From 16/3 to 31/3",
        ],
        [
            "3a.",
            "Dividend Income referred in Sl.no. 1a(i)",
            "0",
            "0",
            "0",
            "126",
            "0",
        ],
        ["", "", "", "", "", "", ""],
        ["Quicko / Add Foreign Dividend Income", "", "", "", "", "", ""],
        ["Country", "United States of America", "", "", "", "", ""],
        [
            "Description",
            "XESP - Fidelity NetBenefits stock plan",
            "",
            "",
            "",
            "",
            "",
        ],
        ["Period 1: 01/04/2025 to 15/06/2025", "0", "", "", "", "", ""],
        ["Period 2: 16/06/2025 to 15/09/2025", "0", "", "", "", "", ""],
        ["Period 3: 16/09/2025 to 15/12/2025", "0", "", "", "", "", ""],
        ["Period 4: 16/12/2025 to 15/03/2026", "126", "", "", "", "", ""],
        ["Period 5: 16/03/2026 to 31/03/2026", "0", "", "", "", "", ""],
    ]
    assert rows[2][1] == str(sum(int(amount) for amount in rows[8][2:])) == "126"
    # A foreign dividend must not also appear under Quicko's domestic screen,
    # which carries no country and so would yield no FSI/TR row.
    assert not any(row[0] == "Quicko / Add Dividend Income (Indian-source only)" for row in rows)
    assert "_audit" not in output.read_text(encoding="utf-8-sig")


def test_filing_csv_emits_schedule_os_1e_nature_and_amount(tmp_path: Path) -> None:
    result = replace(
        _result(),
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
    output = tmp_path / "schedule-os.csv"

    _write(result, output)

    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    assert [
        "1e. Any other income (please specify nature) / Nature: Securities lending fee",
        "9",
        "",
        "",
        "",
        "",
        "",
    ] in rows


def test_filing_csv_registered() -> None:
    assert output_binding(Subcommand.OS, "filing-csv").target == PortalCsv(
        filing_csv_rows, FILING_WORKSHEET
    )


def _domestic_line() -> ScheduleOsDomesticDividendLine:
    return ScheduleOsDomesticDividendLine(
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
    )


def test_domestic_dividends_use_quickos_indian_source_screen(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.csv"
    result = ScheduleOsComputeResult(
        lines=_result().lines,
        domestic_lines=[_domestic_line()],
        interest_total_inr_rounded=Decimal("0"),
    )
    _write(result, output, "Fidelity NetBenefits stock plan")
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    # ITR 1a(i) carries BOTH sources; Quicko splits them across two screens.
    assert rows[2][1] == "1156"  # 126 foreign + 1030 domestic
    foreign = next(
        i for i, row in enumerate(rows) if row[0] == "Quicko / Add Foreign Dividend Income"
    )
    assert rows[foreign + 1] == ["Country", "United States of America", "", "", "", "", ""]
    assert rows[foreign + 2] == [
        "Description",
        "XESP - Fidelity NetBenefits stock plan",
        "",
        "",
        "",
        "",
        "",
    ]
    assert rows[foreign + 6] == ["Period 4: 16/12/2025 to 15/03/2026", "126", "", "", "", "", ""]
    domestic = next(
        i
        for i, row in enumerate(rows)
        if row[0] == "Quicko / Add Dividend Income (Indian-source only)"
    )
    assert rows[domestic + 1] == ["Period 1: 01/04/2025 to 15/06/2025", "0", "", "", "", "", ""]
    assert rows[domestic + 2] == ["Period 2: 16/06/2025 to 15/09/2025", "1030", "", "", "", "", ""]
    assert rows[-2] == ["Total", "1030", "", "", "", "", ""]
    assert rows[-1] == ["Expense (per period)", "not computed", "", "", "", "", ""]
    assert all(len(row) == 7 for row in rows)


def test_quicko_description_falls_back_to_ticker_without_a_source(tmp_path: Path) -> None:
    """A run whose context names no source still gets valid output."""
    output = tmp_path / "schedule-os.csv"
    _write(_result(), output)
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    assert ["Description", "XESP", "", "", "", "", ""] in rows


def test_interest_total_is_emitted_as_its_own_1b_ix_row(tmp_path: Path) -> None:
    """1b(ix) interest must reach the filing CSV, not just a CLI advisory.

    Schedule FSI reports the COMBINED foreign income, so a worksheet carrying
    only the 1a(i) dividend leaves the two CSVs irreconcilable and the interest
    silently unfiled. 1b(ix) is a separate portal cell, so it is a separate row
    rather than being folded into the dividend total.
    """
    output = tmp_path / "schedule-os.csv"
    result = replace(_result(), interest_total_inr_rounded=Decimal("1916"))
    _write(result, output)
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    dividend = ["1ai. Dividend income [other than (ii) and (iii)]", "126", "", "", "", "", ""]
    interest = [
        "1b(ix). Others including interest from Companies, NBFCs & HFCs",
        "1916",
        *([""] * 5),
    ]
    assert dividend in rows
    assert interest in rows
    # Separate cells: the dividend total must NOT absorb the interest.
    assert rows.index(interest) == rows.index(dividend) + 1
    assert all(len(row) == 7 for row in rows)


def test_zero_interest_is_explicit_in_the_filing_row(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.csv"
    _write(_result(), output)
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    assert [
        "1b(ix). Others including interest from Companies, NBFCs & HFCs",
        "0",
        *([""] * 5),
    ] in rows


def test_quicko_entries_reconcile_sub_rupees_and_distinguish_isins(tmp_path: Path) -> None:
    first = replace(
        _result().lines[0],
        symbol=Ticker("DUP"),
        isin=Isin("US99993A1088"),
        fx_rate_inr_per_usd=Decimal("100"),
        amount_gross_usd=Decimal("0.0049"),
        amount_gross_inr=Decimal("0.49"),
        description="DUP(US99993A1088) Cash Dividend USD 0.0049 per Share",
    )
    second = replace(
        first,
        isin=Isin("US99972B1017"),
        description="DUP(US99972B1017) Cash Dividend USD 0.0049 per Share",
    )
    result = ScheduleOsComputeResult(
        lines=[first, second],
        domestic_lines=[],
        interest_total_inr_rounded=Decimal("0"),
    )
    output = tmp_path / "schedule-os.csv"
    _write(result, output)
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))

    # Same country and symbol, so the ISIN orders the entries: the second line
    # comes first and takes the tied rupee.
    descriptions = [row[1] for row in rows if row[0] == "Description"]
    assert descriptions == ["DUP (US99972B1017)", "DUP (US99993A1088)"]
    period_4_amounts = [
        int(row[1]) for row in rows if row[0] == "Period 4: 16/12/2025 to 15/03/2026"
    ]
    itr_period_4 = next(row for row in rows if row[0] == "3a.")[5]
    assert period_4_amounts == [1, 0]
    assert sum(period_4_amounts) == int(itr_period_4) == int(rows[2][1]) == 1


def test_filing_csv_refuses_an_uncomputed_interest_total(tmp_path: Path) -> None:
    output = tmp_path / "schedule-os.csv"
    with pytest.raises(InvariantError, match="separately computed interest total"):
        _write(replace(_result(), interest_total_inr_rounded=None), output)
    assert not output.exists()
