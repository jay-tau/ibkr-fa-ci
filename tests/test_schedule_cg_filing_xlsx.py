"""Tests for the shared portal-shaped Schedule CG filing workbook (#642, #1205)."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa import pipeline_robinhood_reporting
from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compliance.schedule_cg_filing_position import ScheduleCgFilingPosition
from ibkr_fa.compute.robinhood_capital_gains import (
    _FILING_BASIS,
    RobinhoodCapitalGainLot,
    RobinhoodCapitalGainsResult,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionCapitalGainBucket,
    RobinhoodOptionCapitalGainLine,
)
from ibkr_fa.compute.schedule_cg_filing import (
    ScheduleCgFilingBasis,
    ScheduleCgFilingSection,
    build_schedule_cg_filing_result,
)
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import UnknownAssessmentYearError, for_ay
from ibkr_fa.output.robinhood_options_csv import OPTIONS_FILENAMES
from ibkr_fa.output.schedules import cg_filing
from tests._robinhood_cg import (
    ACCOUNT_LABELS,
    CONSIDERATION_BASIS,
    CONSIDERATION_POSITION,
    FILING_CLASS_KIND,
    FILING_LTCG_KIND,
    FILING_LTCG_ROUTE,
    FILING_VENUE_KIND,
    FILING_VENUE_ROUTE,
    NO_OPTIONS,
    TRANSFER_EXPENDITURE_BASIS,
    cg_result,
    option_lines,
    sale,
)
from tests._schedule_cg_filing_workbook import robinhood_ltcg_result as _ltcg_result
from tests._schedule_cg_filing_workbook import robinhood_stcg_result as _result
from tests._schedule_cg_filing_workbook import values_by_field as _values_by_field
from tests._schedule_cg_filing_workbook import write_filing_workbook as _write_filing
from tests._schedule_cg_filing_workbook import write_result_workbook as _write


def _bands(row: int, value: int | None) -> dict[str, int | None]:
    """Table F row ``row``'s five band cells, each ``value`` (#1360)."""
    return {f"F{row}({column})": value for column in ("i", "ii", "iii", "iv", "v")}


def test_filing_workbook_matches_itr3_a6_and_b9(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        _result(),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == ["ITR-3 A6 STCG"]
    stcg = workbook["ITR-3 A6 STCG"]
    assert "ADD Robinhood" in stcg["A1"].value
    assert (
        "Omitted zero Robinhood fields: A6(a)(i)(a-c), A6(b)(ii), A6(b)(iii), A6(d), "
        "A6(e), A6(f), F3(i), F3(ii), F3(iii), F3(iv), F3(v). " in stcg["A1"].value
    )
    assert "No long-term (B9) Robinhood disposals" in stcg["A1"].value
    assert "Section 50CA prescribed FMV is not computed" in stcg["A2"].value
    assert "not as a standalone XLSX or CSV" in stcg["A3"].value
    assert stcg.freeze_panes == "A6"
    assert stcg.auto_filter.ref == "A5:E7"
    assert stcg["C6"].number_format == "0;[RED]-0"
    assert (
        "Zero-valued editable fields and portal-calculated totals and results "
        "are available only with --debug" in stcg["A1"].value
    )
    workbook.close()

    assert _values_by_field(output, "ITR-3 A6 STCG") == {
        "A6(a)(ii)": 1000,
        "A6(b)(i)": 1200,
    }
    if os.name == "posix":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert (
        "Section 50CA prescribed FMV was not computed"
        in (
            pipeline_robinhood_reporting.capital_gains_warnings(
                _result(),
                filing=True,
            )[0]
        )
    )


def test_filing_workbook_uses_itr2_a5_terminal_field(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        _result(),
        output,
        assessment_year_start=2026,
        itr_form="ITR-2",
        account_labels=ACCOUNT_LABELS,
    )

    values = _values_by_field(output, "ITR-2 A5 STCG")
    assert values == {
        "A5(a)(ii)": 1000,
        "A5(b)(i)": 1200,
    }


def test_filing_workbook_accepts_ltcg_loss(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        _ltcg_result(cost=Decimal("12"), proceeds=Decimal("10")),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == ["ITR-3 B9 LTCG"]
    assert "No short-term (A6) Robinhood disposals" in workbook["ITR-3 B9 LTCG"]["A1"].value
    workbook.close()
    values = _values_by_field(output, "ITR-3 B9 LTCG")
    assert values == {
        "B9(a)(ii)": 1000,
        "B9(b)(i)": 1200,
    }


def test_debug_workbook_includes_portal_calculated_checks(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        _result(),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=True,
    )

    values = _values_by_field(output, "ITR-3 A6 STCG")
    assert values == {
        "A6(a)(ii)": 1000,
        "A6(a)(iii)": 1000,
        "A6(b)(i)": 1200,
        "A6(b)(iii)": 0,
        "A6(b)(iv)": 1200,
        "A6(c)": -200,
        "A6(d)": 0,
        "A6(g)": -200,
        **_bands(3, 0),
    }
    workbook = load_workbook(output, data_only=True)
    worksheet = workbook["ITR-3 A6 STCG"]
    basis = {
        worksheet.cell(row, 1).value: worksheet.cell(row, 5).value
        for row in range(6, worksheet.max_row + 1)
    }
    assert "Robinhood contribution" in basis["A6(a)(iii)"]
    assert "cost-of-improvement contribution" in basis["A6(b)(iv)"]
    assert "Debug view" in worksheet["A1"].value
    workbook.close()


def test_debug_workbook_includes_ltcg_calculated_checks(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        _ltcg_result(cost=Decimal("12"), proceeds=Decimal("10")),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=True,
    )

    assert _values_by_field(output, "ITR-3 B9 LTCG") == {
        "B9(a)(ii)": 1000,
        "B9(a)(iii)": 1000,
        "B9(b)(i)": 1200,
        "B9(b)(iii)": 0,
        "B9(b)(iv)": 1200,
        "B9(c)": -200,
        "B9(e)": -200,
        **_bands(5, 0),
    }
    workbook = load_workbook(output, data_only=True)
    assert "its section 54F deduction is zero" in workbook["ITR-3 B9 LTCG"]["E12"].value
    workbook.close()


def test_filing_workbook_keeps_both_nonempty_sections(tmp_path: Path) -> None:
    stcg = _result()
    ltcg = _ltcg_result(cost=Decimal("12"), proceeds=Decimal("10"))
    result = replace(
        stcg,
        ltcg=ltcg.ltcg,
        attributed_lots=(*stcg.attributed_lots, *ltcg.attributed_lots),
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        result,
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == ["ITR-3 A6 STCG", "ITR-3 B9 LTCG"]
    assert "No long-term" not in workbook["ITR-3 A6 STCG"]["A1"].value
    assert "No short-term" not in workbook["ITR-3 B9 LTCG"]["A1"].value
    workbook.close()


def test_filing_workbook_uses_portal_component_rounding(tmp_path: Path) -> None:
    result = _result()
    line = replace(
        result.stcg.stcg_lines[0],
        cost_basis_inr=Decimal("1200.6"),
        proceeds_usd=Decimal("10.004"),
        proceeds_inr=Decimal("1000.4"),
        realized_gain_inr=Decimal("-200.2"),
    )
    result = replace(result, stcg=replace(result.stcg, stcg_lines=[line]))
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        result,
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=True,
    )

    values = _values_by_field(output, "ITR-3 A6 STCG")
    assert values["A6(a)(iii)"] == 1000
    assert values["A6(b)(iv)"] == 1201
    assert values["A6(g)"] == -201
    workbook = load_workbook(output, data_only=True)
    assert "differs from separately rounding" in workbook["ITR-3 A6 STCG"]["A2"].value
    workbook.close()


def test_filing_workbook_rejects_unsupported_assessment_year(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"

    with pytest.raises(UnknownAssessmentYearError):
        _write(
            _result(),
            output,
            assessment_year_start=2028,
            itr_form="ITR-3",
            account_labels=ACCOUNT_LABELS,
        )
    assert not output.exists()


def test_filing_workbook_explains_when_no_cg_entry_is_required(tmp_path: Path) -> None:
    result = _result()
    result = replace(
        result,
        stcg=replace(result.stcg, stcg_lines=[]),
        attributed_lots=(),
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        result,
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    workbook = load_workbook(output, data_only=True)
    assert workbook.sheetnames == ["ITR-3 Schedule CG"]
    worksheet = workbook.active
    assert "No Robinhood entry is required in A6 or B9" in worksheet["A1"].value
    # The summary has no section, so it doesn't state the section sheets' position (#1383).
    assert worksheet["A2"].value == (
        "No Robinhood capital-gain disposals were attributed to this financial year."
    )
    assert worksheet.max_row == 5
    workbook.close()


def _zero_rounded_result() -> RobinhoodCapitalGainsResult:
    """An STCG sale whose every editable amount rounds to ₹0."""
    result = _result()
    line = replace(
        result.stcg.stcg_lines[0],
        cost_basis_usd=Decimal("0.004"),
        proceeds_usd=Decimal("0.004"),
        cost_basis_inr=Decimal("0.4"),
        proceeds_inr=Decimal("0.4"),
        realized_gain_inr=Decimal("0"),
    )
    item = result.attributed_lots[0]
    closed_lot = replace(
        item.closed_lot,
        cost_basis_money=Decimal("0.004"),
        proceeds_money=Decimal("0.004"),
        gross_proceeds_money=Decimal("0.004"),
    )
    return replace(
        result,
        stcg=replace(result.stcg, stcg_lines=[line]),
        attributed_lots=(
            RobinhoodCapitalGainLot(
                replace(item.source_slice, cost_usd=Decimal("0.004")),
                closed_lot,
            ),
        ),
    )


def test_filing_workbook_explains_zero_rounded_editable_amounts(
    tmp_path: Path,
) -> None:
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        _zero_rounded_result(),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    assert _values_by_field(output, "ITR-3 A6 STCG") == {}
    workbook = load_workbook(output, data_only=True)
    assert "every editable amount rounds to 0" in workbook["ITR-3 A6 STCG"]["A1"].value
    workbook.close()


@pytest.mark.parametrize(
    ("itr_form", "section", "result_field"),
    [("ITR-2", "A5", "A5(e)"), ("ITR-3", "A6", "A6(g)")],
)
def test_writer_renders_the_stcg_sections_amounts(
    tmp_path: Path, itr_form: ItrForm, section: str, result_field: str
) -> None:
    """#1078: every STCG amount cell reads the section; the writer supplies none."""
    filing = cg_result(sale("12", "10")).filing(())
    assert filing is not None
    filing = replace(
        filing,
        stcg_section=ScheduleCgFilingSection(
            schedule="STCG",
            consideration_inr=Decimal("1000"),
            total_consideration_inr=Decimal("1000"),
            cost_of_acquisition_inr=Decimal("1200"),
            transfer_expenditure_inr=Decimal("5"),
            total_deductions_inr=Decimal("1205"),
            balance_inr=Decimal("-205"),
            section_94_7_8_disallowed_loss_inr=Decimal("7"),
            result_inr=Decimal("-198"),
        ),
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write_filing(filing, output, itr_form=itr_form, include_checks=True)

    assert _values_by_field(output, f"{itr_form} {section} STCG") == {
        f"{section}(a)(ii)": 1000,
        f"{section}(a)(iii)": 1000,
        f"{section}(b)(i)": 1200,
        f"{section}(b)(iii)": 5,
        f"{section}(b)(iv)": 1205,
        f"{section}(c)": -205,
        f"{section}(d)": 7,
        result_field: -198,
        **_bands(3, 0),
    }


@pytest.mark.parametrize(("itr_form", "section"), [("ITR-2", "B8"), ("ITR-3", "B9")])
def test_writer_renders_the_ltcg_sections_amounts(
    tmp_path: Path, itr_form: ItrForm, section: str
) -> None:
    """#1078: every LTCG amount cell reads the section; the writer supplies none."""
    filing = cg_result(sale("12", "10", long_term=True)).filing(())
    assert filing is not None
    filing = replace(
        filing,
        ltcg_section=ScheduleCgFilingSection(
            schedule="LTCG",
            consideration_inr=Decimal("1000"),
            total_consideration_inr=Decimal("1000"),
            cost_of_acquisition_inr=Decimal("1200"),
            transfer_expenditure_inr=Decimal("3"),
            total_deductions_inr=Decimal("1203"),
            balance_inr=Decimal("-203"),
            section_94_7_8_disallowed_loss_inr=None,
            result_inr=Decimal("-203"),
        ),
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write_filing(filing, output, itr_form=itr_form, include_checks=True)

    assert _values_by_field(output, f"{itr_form} {section} LTCG") == {
        f"{section}(a)(ii)": 1000,
        f"{section}(a)(iii)": 1000,
        f"{section}(b)(i)": 1200,
        f"{section}(b)(iii)": 3,
        f"{section}(b)(iv)": 1203,
        f"{section}(c)": -203,
        f"{section}(e)": -203,
        **_bands(5, 0),
    }


def _notes(path: Path, sheet_name: str) -> tuple[str, str]:
    """The sheet's scope note (A1) and tax-position note (A2)."""
    workbook = load_workbook(path, data_only=True)
    worksheet = workbook[sheet_name]
    notes = (str(worksheet["A1"].value), str(worksheet["A2"].value))
    workbook.close()
    return notes


def _omitted(scope_note: str) -> list[str]:
    """The fields the scope note names as omitted zeros."""
    listed = scope_note.split("Omitted zero Robinhood fields: ", 1)[1].split(". ", 1)[0]
    return listed.split(", ")


@pytest.mark.parametrize("include_checks", [False, True], ids=["default", "debug"])
@pytest.mark.parametrize(
    ("itr_form", "section", "result_field"),
    [("ITR-3", "A6", "A6(g)"), ("ITR-2", "A5", "A5(e)")],
)
def test_stcg_section_files_each_sales_recorded_amount(
    tmp_path: Path, itr_form: ItrForm, section: str, result_field: str, include_checks: bool
) -> None:
    """#1383: a Robinhood sale's recorded Amount is its full value of
    consideration, with 0 transfer expenditure, so the section is filled and
    its tax position note states that position."""
    output = tmp_path / "schedule-cg.xlsx"
    # ₹1000.4 consideration rounds to a non-zero ₹1000; ₹1200.6 cost to ₹1201,
    # so the section also has a ₹-1 component rounding difference.
    _write(
        cg_result(sale("12.006", "10.004")),
        output,
        assessment_year_start=2026,
        itr_form=itr_form,
        account_labels=ACCOUNT_LABELS,
        include_checks=include_checks,
    )

    sheet = f"{itr_form} {section} STCG"
    expected: dict[str, int | None] = {f"{section}(a)(ii)": 1000, f"{section}(b)(i)": 1201}
    if include_checks:
        expected |= {
            f"{section}(a)(iii)": 1000,
            f"{section}(b)(iii)": 0,
            f"{section}(b)(iv)": 1201,
            f"{section}(c)": -201,
            f"{section}(d)": 0,
            result_field: -201,
            **_bands(3, 0),
        }
    assert _values_by_field(output, sheet) == expected
    rows = _rows(output, sheet)
    assert rows[f"{section}(a)(ii)"][3] == CONSIDERATION_BASIS
    if include_checks:
        assert rows[f"{section}(b)(iii)"][3] == TRANSFER_EXPENDITURE_BASIS
    scope_note, position_note = _notes(output, sheet)
    assert (f"{section}(b)(iii)" in _omitted(scope_note)) is not include_checks
    assert (
        "Zero-valued editable fields and portal-calculated totals and results are "
        "available only with --debug." in scope_note
    ) is not include_checks
    assert position_note == (
        "Tax position: 1 foreign exchange-listed securities on NASDAQ are reported as "
        "assets other than unquoted shares. Section 50CA prescribed FMV is not computed. "
        "No CBDT guidance or binding ruling directly resolves this foreign-exchange "
        "classification. Whole-rupee portal component arithmetic differs from separately "
        "rounding the full-precision gain by STCG -1 and LTCG 0; the workbook uses the "
        f"portal component result. {CONSIDERATION_POSITION}"
    )


@pytest.mark.parametrize("include_checks", [False, True], ids=["default", "debug"])
def test_ltcg_section_files_each_sales_recorded_amount(
    tmp_path: Path, include_checks: bool
) -> None:
    """#1383: the same for an LTCG section, with a loss, since a positive LTCG
    line still waits for the section 54F decision."""
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        cg_result(sale("12", "10", long_term=True)),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=include_checks,
    )

    expected: dict[str, int | None] = {"B9(a)(ii)": 1000, "B9(b)(i)": 1200}
    if include_checks:
        expected |= {
            "B9(a)(iii)": 1000,
            "B9(b)(iii)": 0,
            "B9(b)(iv)": 1200,
            "B9(c)": -200,
            "B9(e)": -200,
            **_bands(5, 0),
        }
    assert _values_by_field(output, "ITR-3 B9 LTCG") == expected
    rows = _rows(output, "ITR-3 B9 LTCG")
    assert rows["B9(a)(ii)"][3] == CONSIDERATION_BASIS
    if include_checks:
        assert rows["B9(b)(iii)"][3] == TRANSFER_EXPENDITURE_BASIS
    scope_note, position_note = _notes(output, "ITR-3 B9 LTCG")
    assert ("B9(b)(iii)" in _omitted(scope_note)) is not include_checks
    assert position_note.endswith(f" {_FILING_BASIS.consideration_position}")


def test_a_section_files_the_sum_of_its_sales_amounts(tmp_path: Path) -> None:
    """#1383: each sale's recorded Amount adds to its section's consideration,
    and the note names no sale."""
    output = tmp_path / "schedule-cg.xlsx"
    result = cg_result(
        sale("12", "10"),
        sale("5", "4", sale_date=date(2025, 7, 1), lot_ordinal=3, sale_ordinal=4),
    )
    _write(
        result,
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=True,
    )

    assert _values_by_field(output, "ITR-3 A6 STCG") == {
        "A6(a)(ii)": 1400,
        "A6(a)(iii)": 1400,
        "A6(b)(i)": 1700,
        "A6(b)(iii)": 0,
        "A6(b)(iv)": 1700,
        "A6(c)": -300,
        "A6(d)": 0,
        "A6(g)": -300,
        **_bands(3, 0),
    }
    _scope_note, position_note = _notes(output, "ITR-3 A6 STCG")
    assert position_note.endswith(f" {_FILING_BASIS.consideration_position}")
    for item in result.attributed_lots:
        lot = item.closed_lot
        for marker in (lot.symbol, item.source_slice.cusip, lot.close_date_time.isoformat()):
            assert marker not in position_note


def test_both_sections_file_their_amounts_and_rounding_differences(tmp_path: Path) -> None:
    """#1383: neither section is left blank, so each sheet's note gives both
    sections' rounding differences, then the position."""
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        cg_result(
            sale("12.006", "10.004"),
            sale("12.006", "10.004", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
    )

    assert _values_by_field(output, "ITR-3 A6 STCG") == {"A6(a)(ii)": 1000, "A6(b)(i)": 1201}
    assert _values_by_field(output, "ITR-3 B9 LTCG") == {"B9(a)(ii)": 1000, "B9(b)(i)": 1201}
    for sheet in ("ITR-3 A6 STCG", "ITR-3 B9 LTCG"):
        _scope_note, position_note = _notes(output, sheet)
        assert "rounding the full-precision gain by STCG -1 and LTCG -1;" in position_note
        assert position_note.endswith(f" {_FILING_BASIS.consideration_position}")


def test_rounded_zero_stcg_balance_files_zero_not_refused(tmp_path: Path) -> None:
    """A ₹0.1 STCG gain whose rounded components cancel files a 0 balance and
    result, and 0 bands (#1383)."""
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        cg_result(sale("10.003", "10.004")),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=True,
    )

    assert _values_by_field(output, "ITR-3 A6 STCG") == {
        "A6(a)(ii)": 1000,
        "A6(a)(iii)": 1000,
        "A6(b)(i)": 1000,
        "A6(b)(iii)": 0,
        "A6(b)(iv)": 1000,
        "A6(c)": 0,
        "A6(d)": 0,
        "A6(g)": 0,
        **_bands(3, 0),
    }
    _scope_note, position_note = _notes(output, "ITR-3 A6 STCG")
    assert position_note.endswith(f" {_FILING_BASIS.consideration_position}")


# The unreviewed exchanges and classes the cases below feed in, as whole tokens.
_UNREVIEWED_VALUE = re.compile(r"(?i)\b(OTC|PREFERRED)\b")


def _blocked_refusal(*kind_texts: str) -> str:
    """The exact refusal of a Robinhood filing blocked on ``kind_texts``, in
    blocker order, each reachable kind's route line under them (#1555)."""
    routes = {
        FILING_VENUE_KIND: f"\n{FILING_VENUE_ROUTE.format(sales='0:2')}",
        FILING_LTCG_KIND: f"\n{FILING_LTCG_ROUTE}",
    }
    return (
        f"InvariantError: Schedule CG filing is blocked: {'; '.join(kind_texts)}"
        f"{''.join(routes.get(text, '') for text in kind_texts)} (context keys: reasons)"
    )


def _on(lot: RobinhoodCapitalGainLot, **fields: object) -> RobinhoodCapitalGainLot:
    return replace(lot, closed_lot=replace(lot.closed_lot, **fields))


def test_writer_refuses_a_blocked_result_with_every_reason(tmp_path: Path) -> None:
    """#642: the writer gates nothing itself; it refuses the builder's blockers,
    naming each one's kind (#1047). Their reasons, which name the unreviewed
    exchange, keep only their key in the context."""
    result = cg_result(sale("10", "12", long_term=True))
    item = result.attributed_lots[0]
    result = replace(
        result,
        attributed_lots=(
            replace(item, closed_lot=replace(item.closed_lot, listing_exchange="OTC")),
        ),
    )
    filing = result.filing(())
    assert filing is not None
    output = tmp_path / "schedule-cg.xlsx"

    with pytest.raises(InvariantError) as raised:
        _write_filing(filing, output, itr_form="ITR-3")
    assert raised.value.context == (("reasons", None),)
    assert str(raised.value) == _blocked_refusal(FILING_VENUE_KIND, FILING_LTCG_KIND)
    assert _UNREVIEWED_VALUE.search(str(raised.value)) is None
    assert not output.exists()


# Each case: a filing blocked on one kind, and that kind's text.
_WRITER_BLOCKERS = {
    "unreviewed-venue": (
        lambda: _on(sale("12", "10"), listing_exchange="OTC"),
        FILING_VENUE_KIND,
    ),
    "unreviewed-security-class": (
        lambda: _on(sale("12", "10"), sub_category="PREFERRED"),
        FILING_CLASS_KIND,
    ),
    "positive-ltcg": (
        lambda: sale("10", "12", long_term=True),
        FILING_LTCG_KIND,
    ),
}


@pytest.mark.parametrize(
    ("make_lot", "kind_text"), _WRITER_BLOCKERS.values(), ids=_WRITER_BLOCKERS.keys()
)
def test_writer_refusal_names_each_blocker_kind(
    tmp_path: Path,
    make_lot: Callable[[], RobinhoodCapitalGainLot],
    kind_text: str,
) -> None:
    """#1047: the writer's refusal names the blocker's kind in fixed text and
    keeps only its reason's key in the context, since the reason can name an
    unreviewed exchange or class."""
    filing = cg_result(make_lot()).filing(())
    assert filing is not None
    output = tmp_path / "schedule-cg.xlsx"

    with pytest.raises(InvariantError) as raised:
        _write_filing(filing, output, itr_form="ITR-3")

    assert str(raised.value) == _blocked_refusal(kind_text)
    assert raised.value.context == (("reasons", None),)
    assert _UNREVIEWED_VALUE.search(str(raised.value)) is None
    assert not output.exists()


def _other_basis() -> ScheduleCgFilingBasis:
    """Another source's evidence wording, each claim naming itself."""
    return ScheduleCgFilingBasis(
        consideration="Example consideration evidence.",
        cost="Example cost evidence.",
        transfer_expenditure="Example transfer-expenditure evidence.",
        section_94_7_8="Example section 94(7)/(8) evidence.",
        schedule_dcg="Example Schedule DCG evidence.",
        consideration_position="Example consideration position.",
        covered_disposals="example covered disposals",
        uncovered_disposals="Example uncovered disposals need entry elsewhere",
        table_f_other_files="example other capital-gains files",
    )


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_writer_takes_every_source_claim_from_its_label_and_basis(
    tmp_path: Path, itr_form: ItrForm
) -> None:
    """A source other than Robinhood renders with no Robinhood wording; its
    position ends each section sheet's tax position note (#1383)."""
    lots = (
        sale("12", "10"),
        sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
    )
    engines = cg_result(*lots)
    filing = build_schedule_cg_filing_result(
        engines.stcg,
        engines.ltcg,
        (item.closed_lot for item in lots),
        position=ScheduleCgFilingPosition(frozenset({"NASDAQ"}), frozenset({"COMMON"})),
        basis=_other_basis(),
        stcg_uncovered_disposals=False,
        ltcg_uncovered_disposals=False,
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write_filing(
        filing, output, itr_form=itr_form, source_label="Example Broker", include_checks=True
    )

    workbook = load_workbook(output, data_only=True)
    text = " ".join(
        str(cell.value)
        for worksheet in workbook.worksheets
        for row in worksheet.iter_rows()
        for cell in row
        if cell.value is not None
    )
    position_notes = [str(worksheet["A2"].value) for worksheet in workbook.worksheets]
    workbook.close()
    assert "Robinhood" not in text
    assert "FINRA" not in text
    for claim in (
        "Example consideration evidence.",
        "Example cost evidence.",
        "Example transfer-expenditure evidence.",
        "Example section 94(7)/(8) evidence.",
        "Example Broker amount",
        "ADD Example Broker input amounts",
    ):
        assert claim in text
    assert ("Example Schedule DCG evidence." in text) is (itr_form == "ITR-3")
    assert len(position_notes) == 2
    for note in position_notes:
        assert note.endswith(" Example consideration position.")


def test_writer_refuses_a_formula_in_the_source_heading(tmp_path: Path) -> None:
    filing = cg_result().filing(())
    assert filing is not None
    output = tmp_path / "schedule-cg.xlsx"

    with pytest.raises(WriterContractError) as raised:
        _write_filing(filing, output, itr_form="ITR-2", source_label="=1")

    assert raised.value.violation == "formula_trigger"
    assert (raised.value.row_index, raised.value.cell_index) == (4, 2)
    assert not output.exists()


def test_sheet_module_names_no_source() -> None:
    source = Path(cg_filing.__file__).read_text(encoding="utf-8")

    assert "robinhood" not in source.lower()


# ---- #1322: sections holding option capital gains ----

_STCG_OPTIONS = option_lines(RobinhoodOptionCapitalGainBucket.STCG_OTHER_ASSETS)
_LTCG_OPTIONS = option_lines(RobinhoodOptionCapitalGainBucket.LTCG_OTHER_ASSETS)
_UNCOVERED = (
    "Robinhood option capital gains are not in this workbook; enter them by hand from "
    f"{OPTIONS_FILENAMES[0]}, which robinhood-options and robinhood-all write."
)

# Each case: the ITR form, the option lines, and the summary's scope note
# after its instruction.
_SUMMARY_CASES = {
    "itr2-stcg": (
        "ITR-2",
        _STCG_OPTIONS,
        "No Robinhood entry is required in B8. Manual entry in A5:",
    ),
    "itr2-ltcg": (
        "ITR-2",
        _LTCG_OPTIONS,
        "No Robinhood entry is required in A5. Manual entry in B8:",
    ),
    "itr3-stcg": (
        "ITR-3",
        _STCG_OPTIONS,
        "No Robinhood entry is required in B9. Manual entry in A6:",
    ),
    "itr3-both": ("ITR-3", (*_LTCG_OPTIONS, *_STCG_OPTIONS), "Manual entry in A6 and B9:"),
}


@pytest.mark.parametrize(
    ("itr_form", "options", "expected"), _SUMMARY_CASES.values(), ids=_SUMMARY_CASES.keys()
)
def test_summary_names_the_sections_option_capital_gains_need(
    tmp_path: Path,
    itr_form: ItrForm,
    options: tuple[RobinhoodOptionCapitalGainLine, ...],
    expected: str,
) -> None:
    """#1322: a section holding option capital gains is never said to need no entry."""
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        cg_result(),
        output,
        assessment_year_start=2026,
        itr_form=itr_form,
        account_labels=ACCOUNT_LABELS,
        options=options,
    )

    scope_note, position_note = _notes(output, f"{itr_form} Schedule CG")
    assert scope_note == (
        "IMPORTANT: ADD Robinhood input amounts to any existing Schedule CG values from "
        f"other sources; do not replace existing totals. {expected} {_UNCOVERED} Preserve "
        "any values from other sources."
    )
    assert position_note == "No Robinhood equity disposals were attributed to this financial year."


@pytest.mark.parametrize(
    ("itr_form", "stcg", "ltcg"), [("ITR-2", "A5", "B8"), ("ITR-3", "A6", "B9")]
)
def test_equity_sheet_names_an_option_only_counterpart(
    tmp_path: Path, itr_form: ItrForm, stcg: str, ltcg: str
) -> None:
    """#1322: an equity STCG sheet beside an option-only LTCG section, and the
    reverse, name the other section for manual entry instead of none."""
    for lot, options, sheet, counterpart, old in (
        (sale("12", "10"), _LTCG_OPTIONS, f"{stcg} STCG", ltcg, "long-term"),
        (
            sale("12", "10", long_term=True),
            _STCG_OPTIONS,
            f"{ltcg} LTCG",
            stcg,
            "short-term",
        ),
    ):
        output = tmp_path / f"{counterpart}.xlsx"
        _write(
            cg_result(lot),
            output,
            assessment_year_start=2026,
            itr_form=itr_form,
            account_labels=ACCOUNT_LABELS,
            options=options,
        )

        scope_note, _position_note = _notes(output, f"{itr_form} {sheet}")
        assert (
            f"Preserve any values from other sources. Manual entry in {counterpart}: {_UNCOVERED} "
            in scope_note
        )
        assert f"No {old} ({counterpart})" not in scope_note
        assert "entry is required" not in scope_note


def test_equity_sheet_names_its_own_section_beside_option_gains(tmp_path: Path) -> None:
    """#1322: the option gains add to the sheet's own section, whose other
    section stays empty and needs no entry."""
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        cg_result(sale("12", "10")),
        output,
        assessment_year_start=2026,
        itr_form="ITR-2",
        account_labels=ACCOUNT_LABELS,
        options=_STCG_OPTIONS,
    )

    scope_note, _position_note = _notes(output, "ITR-2 A5 STCG")
    assert (
        " No long-term (B8) Robinhood disposals; no Robinhood entry is required. Manual entry "
        f"in A5: {_UNCOVERED} "
    ) in scope_note


def test_each_equity_sheet_names_only_its_own_section(tmp_path: Path) -> None:
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        cg_result(
            sale("12", "10"),
            sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
        ),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        options=(*_STCG_OPTIONS, *_LTCG_OPTIONS),
    )

    stcg_note, _position_note = _notes(output, "ITR-3 A6 STCG")
    ltcg_note, _position_note = _notes(output, "ITR-3 B9 LTCG")
    assert f" Manual entry in A6: {_UNCOVERED} " in stcg_note
    assert f" Manual entry in B9: {_UNCOVERED} " in ltcg_note
    assert "B9" not in stcg_note
    assert "A6:" not in ltcg_note


def test_zero_rounded_sheet_beside_option_gains_still_needs_entry(tmp_path: Path) -> None:
    """#1322: the sheet's own amounts round to 0, but its section also has
    option capital-gain lines."""
    output = tmp_path / "schedule-cg.xlsx"

    _write(
        _zero_rounded_result(),
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        options=_STCG_OPTIONS,
    )

    scope_note, _position_note = _notes(output, "ITR-3 A6 STCG")
    assert scope_note.endswith(
        "Robinhood disposals exist, but every editable amount rounds to 0; no numeric "
        "entry from this sheet is required."
    )
    assert f" Manual entry in A6: {_UNCOVERED} " in scope_note
    assert "no numeric Robinhood entry" not in scope_note


def test_writer_takes_the_uncovered_disposal_wording_from_the_basis(tmp_path: Path) -> None:
    """#1322: another source's notes come from its label and basis."""
    engines = cg_result()
    filing = build_schedule_cg_filing_result(
        engines.stcg,
        engines.ltcg,
        (),
        position=ScheduleCgFilingPosition(frozenset({"NASDAQ"}), frozenset({"COMMON"})),
        basis=_other_basis(),
        stcg_uncovered_disposals=False,
        ltcg_uncovered_disposals=True,
    )
    output = tmp_path / "schedule-cg.xlsx"

    _write_filing(filing, output, itr_form="ITR-2", source_label="Example Broker")

    scope_note, position_note = _notes(output, "ITR-2 Schedule CG")
    assert scope_note.endswith(
        "totals. No Example Broker entry is required in A5. Manual entry in B8: Example "
        "uncovered disposals need entry elsewhere. Preserve any values from other sources."
    )
    assert position_note == (
        "No Example Broker example covered disposals were attributed to this financial year."
    )
    assert "Robinhood" not in scope_note + position_note


# ---- #1360: Table F date bands ----

# A sale date in bands (i), (iii) and (iv) of FY 2025-26.
_BAND_1, _BAND_3, _BAND_4 = date(2025, 6, 10), date(2025, 10, 1), date(2026, 1, 15)
# The Table F row descriptions after each band heading, by ITR form.
_F3_DESCRIPTION = {
    "ITR-2": (
        "Short-term capital gains taxable at applicable rates. Enter value from item 3v "
        "of schedule BFLA, if any."
    ),
    "ITR-3": (
        "Short-term capital gains taxable at applicable rates. Enter value from item 5viii "
        "of schedule BFLA, if any."
    ),
}
_F5_DESCRIPTION = {
    "ITR-2": (
        "Long- term capital gains taxable at the rate of 12.5%. Enter value from item 3vii "
        "of schedule BFLA, if any."
    ),
    "ITR-3": (
        "Long- term capital gains taxable at the rate of 12.5%. Enter value from item 5x "
        "of schedule BFLA, if any."
    ),
}
_HEADINGS = ("Upto 15/6", "16/6 to 15/9", "16/9 to 15/12", "16/12 to 15/3", "16/3 to 31/3")
_COMPUTED_BASIS = (
    "Robinhood sales dated in this band, net, before any set-off; the five bands add up "
    "to this section's result, or are 0 when it isn't positive. Add the same band's "
    "amounts from every other capital-gains file: other brokers, robinhood-options.csv's "
    "option gains, and Indian sales. Then lower the bands for any capital loss set off "
    "against them, so the row totals the Schedule BFLA item its description names. The "
    "portal checks only the total."
)
# A computed note's text after its first sentence; a blank note keeps it (#1379).
_GUIDANCE = (
    "Add the same band's amounts from every other capital-gains file: other brokers, "
    "robinhood-options.csv's option gains, and Indian sales. Then lower the bands for any "
    "capital loss set off against them, so the row totals the Schedule BFLA item its "
    "description names. The portal checks only the total."
)
_LOSS_ACROSS_BANDS_REASON = (
    "Blank: a date band nets a loss and two or more bands net gains. Choose how the loss "
    "lowers them: no band below 0, and the five add up to this section's result."
)
_LOSS_ACROSS_BANDS_BASIS = f"{_LOSS_ACROSS_BANDS_REASON} {_GUIDANCE}"


def _rows(path: Path, sheet_name: str) -> dict[str, tuple[object, ...]]:
    """Each row's description, amount, action and Basis, by portal field."""
    workbook = load_workbook(path, data_only=True)
    worksheet = workbook[sheet_name]
    rows = {
        str(worksheet.cell(row, 1).value): tuple(
            worksheet.cell(row, column).value for column in range(2, 6)
        )
        for row in range(6, worksheet.max_row + 1)
    }
    workbook.close()
    return rows


def _write_lots(
    tmp_path: Path, *lots: RobinhoodCapitalGainLot, itr_form: ItrForm, debug: bool
) -> Path:
    """Write the filing workbook of ``lots`` through the builder and the real writer."""
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        cg_result(*lots),
        output,
        assessment_year_start=2026,
        itr_form=itr_form,
        account_labels=ACCOUNT_LABELS,
        include_checks=debug,
    )
    return output


def _bands_of(rows: dict[str, tuple[object, ...]], row: int) -> list[object]:
    return [rows[f"F{row}({column})"][1] for column in ("i", "ii", "iii", "iv", "v")]


@pytest.mark.parametrize(
    ("itr_form", "section", "result_field"),
    [("ITR-2", "A5", "A5(e)"), ("ITR-3", "A6", "A6(g)")],
)
def test_gains_in_two_bands_split_the_section_result(
    tmp_path: Path, itr_form: ItrForm, section: str, result_field: str
) -> None:
    """Gains of ₹200.4 in band (i) and ₹300.3 in band (iii): consideration
    ₹2,500.7 files 2,501 against cost 2,000, a result of 501. The bands are the
    nets' floors, 200 and 300, and the leftover rupee goes to the larger
    remainder, band (i). Each row is "Enter/add" with the official labels."""
    output = _write_lots(
        tmp_path,
        sale("10", "12.004", sale_date=_BAND_1),
        sale("10", "13.003", sale_date=_BAND_3, lot_ordinal=3, sale_ordinal=4),
        itr_form=itr_form,
        debug=True,
    )

    rows = _rows(output, f"{itr_form} {section} STCG")
    assert rows[result_field][1] == 501
    assert _bands_of(rows, 3) == [201, 0, 300, 0, 0]
    assert [rows[f"F3({column})"] for column in ("i", "ii", "iii", "iv", "v")] == [
        (f"{heading}: {_F3_DESCRIPTION[itr_form]}", band, "Enter/add", _COMPUTED_BASIS)
        for heading, band in zip(_HEADINGS, (201, 0, 300, 0, 0), strict=True)
    ]
    assert list(rows)[-5:] == ["F3(i)", "F3(ii)", "F3(iii)", "F3(iv)", "F3(v)"]


def test_one_gain_band_beside_a_loss_band_files_the_result_in_the_gain_band(
    tmp_path: Path,
) -> None:
    """A ₹300 gain in band (i) and a ₹100 loss in band (iii) file A5(e) ₹200,
    all in band (i). The default view shows only that band and names the
    zero ones as omitted."""
    output = _write_lots(
        tmp_path,
        sale("10", "13", sale_date=_BAND_1),
        sale("12", "11", sale_date=_BAND_3, lot_ordinal=3, sale_ordinal=4),
        itr_form="ITR-2",
        debug=False,
    )

    assert _values_by_field(output, "ITR-2 A5 STCG") == {
        "A5(a)(ii)": 2400,
        "A5(b)(i)": 2200,
        "F3(i)": 200,
    }
    scope_note, _ = _notes(output, "ITR-2 A5 STCG")
    assert (
        "Omitted zero Robinhood fields: A5(a)(i)(a-c), A5(b)(ii), A5(b)(iii), A5(d), "
        "F3(ii), F3(iii), F3(iv), F3(v). " in scope_note
    )


@pytest.mark.parametrize("debug", [False, True], ids=["default", "debug"])
def test_a_loss_band_beside_two_gain_bands_leaves_table_f_blank(
    tmp_path: Path, debug: bool
) -> None:
    """Gains of ₹300 in band (i) and ₹100 in band (iv) beside a ₹100 loss in
    band (iii): no source says which gain band the loss lowers, so every band
    is blank, shown in both views, and the run report names Table F row 3."""
    result = cg_result(
        sale("10", "13", sale_date=_BAND_1),
        sale("12", "11", sale_date=_BAND_3, lot_ordinal=3, sale_ordinal=4),
        sale("10", "11", sale_date=_BAND_4, lot_ordinal=5, sale_ordinal=6),
    )
    output = tmp_path / "schedule-cg.xlsx"
    _write(
        result,
        output,
        assessment_year_start=2026,
        itr_form="ITR-3",
        account_labels=ACCOUNT_LABELS,
        include_checks=debug,
    )

    rows = _rows(output, "ITR-3 A6 STCG")
    assert _bands_of(rows, 3) == [None] * 5
    assert {rows[f"F3({column})"][3] for column in ("i", "ii", "iii", "iv", "v")} == {
        _LOSS_ACROSS_BANDS_BASIS
    }
    assert pipeline_robinhood_reporting.cg_manual_reasons(
        result, NO_OPTIONS, writes_options_csv=False
    ) == (
        "Schedule CG Table F row 3 needs its date bands entered by hand: a band nets a "
        "loss and two or more bands net gains; schedule-cg.xlsx leaves them blank",
    )


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_a_blank_rows_note_keeps_the_table_f_guidance(tmp_path: Path, itr_form: ItrForm) -> None:
    """#1379: the filer fills a blank band row by hand, so after its reason
    its note still says to add the other files' bands, lower them for
    set-off losses and total the Schedule BFLA item. A loss sale in each
    section, with both rows set to a loss across bands, which row 5 can't
    reach through the builder while a positive LTCG line blocks the
    workbook."""
    filing = cg_result(
        sale("12", "10"),
        sale("12", "10", long_term=True, lot_ordinal=3, sale_ordinal=4),
    ).filing(())
    assert filing is not None
    blank = ScheduleCgTableFRow(ScheduleCgTableFCase.LOSS_ACROSS_BANDS, None)
    output = tmp_path / "schedule-cg.xlsx"
    _write_filing(
        replace(filing, stcg_table_f=blank, ltcg_table_f=blank), output, itr_form=itr_form
    )

    stcg, ltcg = ("A5", "B8") if itr_form == "ITR-2" else ("A6", "B9")
    for sheet, row in ((f"{itr_form} {stcg} STCG", 3), (f"{itr_form} {ltcg} LTCG", 5)):
        rows = _rows(output, sheet)
        assert _bands_of(rows, row) == [None] * 5
        for column in ("i", "ii", "iii", "iv", "v"):
            note = rows[f"F{row}({column})"][3]
            assert isinstance(note, str)
            assert note.startswith(f"{_LOSS_ACROSS_BANDS_REASON} ")
            assert _GUIDANCE in note


# What a MANUAL_SECTION Table F row reaching a writer raises (#1383).
_MANUAL_SECTION_REFUSAL = (
    "InvariantError: a Schedule CG Table F row is MANUAL_SECTION, which no Schedule CG "
    "section builds"
)


def test_a_manual_section_table_f_row_refuses() -> None:
    """#1383: no Schedule CG section is entered by hand any more, so a
    MANUAL_SECTION row has no Basis; one reaching the workbook refuses with
    fixed text, not a KeyError or a computed row's note.
    ScheduleCgFilingResult refuses such a row itself, so the row helpers are
    called directly."""
    labels = for_ay(2026)
    manual = ScheduleCgTableFRow(ScheduleCgTableFCase.MANUAL_SECTION, None)
    for reach in (
        lambda: cg_filing.table_f_blank_basis(manual.case),
        lambda: cg_filing._table_f_rows(
            3,
            labels.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM["ITR-2"],
            manual,
            headings=labels.PORTAL_COLUMN_HEADERS_CG_TABLE_F,
            source_label="Robinhood",
            basis=_FILING_BASIS,
        ),
    ):
        with pytest.raises(InvariantError) as raised:
            reach()

        assert str(raised.value) == _MANUAL_SECTION_REFUSAL
        assert raised.value.context == ()


@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_an_ltcg_loss_files_zero_bands(tmp_path: Path, itr_form: ItrForm) -> None:
    """A result of zero or less adds nothing to Table F row 5: zeros, with the
    official labels in the debug view."""
    output = _write_lots(tmp_path, sale("12", "10", long_term=True), itr_form=itr_form, debug=True)

    section = "B8" if itr_form == "ITR-2" else "B9"
    rows = _rows(output, f"{itr_form} {section} LTCG")
    assert [rows[f"F5({column})"] for column in ("i", "ii", "iii", "iv", "v")] == [
        (f"{heading}: {_F5_DESCRIPTION[itr_form]}", 0, "Enter/add", _COMPUTED_BASIS)
        for heading in _HEADINGS
    ]


def test_each_sale_lands_in_the_band_of_its_date(tmp_path: Path) -> None:
    """Gains of $1, $2, $4 ... $256 (₹100 to the dollar) sold on the nine
    requested boundary dates: band (i) holds 15/6, (ii) 16/6 and 15/9, (iii)
    16/9 and 15/12, (iv) 16/12 and 15/3, and (v) 16/3 and 31/3."""
    dates = (
        date(2025, 6, 15),
        date(2025, 6, 16),
        date(2025, 9, 15),
        date(2025, 9, 16),
        date(2025, 12, 15),
        date(2025, 12, 16),
        date(2026, 3, 15),
        date(2026, 3, 16),
        date(2026, 3, 31),
    )
    output = _write_lots(
        tmp_path,
        *(
            sale(
                "1",
                str(1 + 2**index),
                sale_date=sale_date,
                lot_ordinal=2 * index + 1,
                sale_ordinal=2 * index + 2,
            )
            for index, sale_date in enumerate(dates)
        ),
        itr_form="ITR-2",
        debug=True,
    )

    rows = _rows(output, "ITR-2 A5 STCG")
    assert rows["A5(e)"][1] == 51100
    assert _bands_of(rows, 3) == [100, 600, 2400, 9600, 38400]
