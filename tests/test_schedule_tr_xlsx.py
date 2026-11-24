"""Tests for :mod:`ibkr_fa.output.schedule_tr_xlsx`.

Mirrors :mod:`tests.test_schedule_tr_review_csv` plus XLSX-specific
concerns (native cell types, bold + frozen header, writer-contract
on string cells, valid-XLSX round-trip).
"""

from __future__ import annotations

import csv as csvmod
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from openpyxl.workbook import Workbook

import ibkr_fa.output.schedule_tr_xlsx as xlsx_writer_mod
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import (
    Artifact,
    Destination,
    OutputBinding,
    OutputKey,
    Subcommand,
    available_formats,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_TR,
    UnknownAssessmentYearError,
)
from ibkr_fa.output.schedule_tr_review_csv import (
    _AUDIT_HEADERS,
    ScheduleTrReviewCsvWriter,
)
from ibkr_fa.output.schedule_tr_xlsx import (
    _COLUMN_COUNT,
    _SHEET_NAME_TR,
    ScheduleTrXlsxWriter,
)


def _make_row(
    *,
    cbdt_country_code: str = "2",
    country_name_portal: str = "UNITED STATES OF AMERICA",
    iso_alpha2: str = "US",
    foreign_tax_inr_total: Decimal = Decimal("1851"),
    event_count: int = 3,
    foreign_tax_inr_total_full_precision: Decimal | None = None,
    tax_relief_inr_total: Decimal | None = None,
    section_under_which_claimed: TaxReliefSection | None = None,
) -> ScheduleTrRow:
    return ScheduleTrRow(
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country_name_portal,
        iso_alpha2=iso_alpha2,
        foreign_tax_inr_total=foreign_tax_inr_total,
        event_count=event_count,
        foreign_tax_inr_total_full_precision=foreign_tax_inr_total_full_precision,
        tax_relief_inr_total=tax_relief_inr_total,
        section_under_which_claimed=section_under_which_claimed,
    )


def _open(path: Path) -> Workbook:
    return load_workbook(filename=path)


# ---- Registry ------------------------------------------------------------


def test_writer_registered_under_xlsx_format_id() -> None:
    assert output_binding(Subcommand.TR, "xlsx") == OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.TR_XLSX), ScheduleTrXlsxWriter
    )


def test_format_id_is_xlsx() -> None:
    assert ScheduleTrXlsxWriter.format_id == "xlsx"


def test_available_tr_formats_contains_both_writers() -> None:
    formats = available_formats(Subcommand.TR)
    assert "review-csv" in formats
    assert "xlsx" in formats


# ---- Sheet structure -----------------------------------------------------


def test_sheet_name_is_schedule_tr(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    wb = _open(path)
    assert wb.sheetnames == [_SHEET_NAME_TR]


def test_header_row_is_bold_and_frozen(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    assert ws.freeze_panes == "A2"
    for cell in ws[1]:
        assert cell.font.bold is True


def test_header_cells_match_portal_then_audit(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:5]) == PORTAL_COLUMN_HEADERS_TR
    assert tuple(header_values[5:]) == _AUDIT_HEADERS
    assert len(header_values) == _COLUMN_COUNT


def test_empty_result_emits_only_header(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    assert ws.max_row == 1


# ---- Detail-row cell types and values ------------------------------------


def test_single_row_renders_with_native_types_and_empty_optional_cells(
    tmp_path: Path,
) -> None:
    """A row with no TIN, relief or section (all None) renders those
    cells as None in Excel (truly empty)."""
    row = _make_row(foreign_tax_inr_total_full_precision=Decimal("1851.234"))
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    assert ws.max_row == 2

    data = [c.value for c in ws[2]]
    # Portal cells (5):
    assert data[0] == "2"
    assert data[1] is None  # TIN
    assert data[2] == 1851
    assert isinstance(data[2], int)
    assert data[3] is None  # tax_relief — None (not computed)
    assert data[4] is None  # section — None (not computed)
    # Audit cells (4):
    assert data[5] == "US"
    assert data[6] == "UNITED STATES OF AMERICA"
    assert isinstance(data[7], float)
    assert data[7] == pytest.approx(1851.234)
    assert data[8] == 3
    assert isinstance(data[8], int)


def test_computed_zero_relief_renders_as_numeric_zero_not_none(tmp_path: Path) -> None:
    """T3 r1 P2 #1 fix flowed through to XLSX: tax_relief=Decimal("0")
    (computed but worked out to zero) renders as numeric ``0``, NOT
    None. Distinguishes 'computed zero' from 'not computed' in Excel.

    PR #222: the symmetric coupling invariant requires
    section_under_which_claimed to accompany tax_relief_inr_total."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1000"),
        tax_relief_inr_total=Decimal("0"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    data = [c.value for c in ws[2]]
    assert data[3] == 0
    assert isinstance(data[3], int)


def test_populated_section_renders_strenum_value(tmp_path: Path) -> None:
    row = _make_row(
        foreign_tax_inr_total=Decimal("1000"),
        tax_relief_inr_total=Decimal("750"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    data = [c.value for c in ws[2]]
    assert data[3] == 750
    assert data[4] == "90"


def test_portal_totals_are_the_compute_whole_rupee_values(
    tmp_path: Path, whole_rupees_without_rounding
) -> None:
    """#900: compute sums the FSI rows' filed whole rupees (two rows of
    ₹0.75 file ₹1 + ₹1), and the writer renders that total as it is; the
    model refuses a fractional one, so the writer has nothing to round."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("2"),
        foreign_tax_inr_total_full_precision=Decimal("1.50"),
        tax_relief_inr_total=Decimal("1"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    # Guard the already validated totals against another rounding pass.
    for field in ("foreign_tax_inr_total", "tax_relief_inr_total"):
        object.__setattr__(row, field, whole_rupees_without_rounding(getattr(row, field)))
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    data = [c.value for c in ws[2]]
    assert data[2:4] == [2, 1]
    assert all(isinstance(value, int) for value in data[2:4])
    assert data[7] == pytest.approx(1.50)


def test_audit_total_uses_full_precision_field_when_populated(tmp_path: Path) -> None:
    """T2/T3 r2 fix on PR #216: when compute populates
    ``foreign_tax_inr_total_full_precision`` separately from the
    portal-displayed ``foreign_tax_inr_total``, the audit cell
    MUST render the full-precision value (not the pre-rounded
    portal total). Example: two FSI rows of ₹1.50 each give
    portal=₹4 / full-precision=₹3.00 — the audit cell must
    carry Decimal("3.00"), NOT Decimal("4")."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("4"),  # portal: per-row HALF_UP sum
        foreign_tax_inr_total_full_precision=Decimal("3.00"),  # full-precision sum
    )
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    data = [c.value for c in ws[2]]
    assert data[2] == 4  # portal-displayed integer
    # openpyxl serialises Decimal cells to float/int on round-trip;
    # use pytest.approx like the existing audit-cell tests.
    assert data[7] == pytest.approx(3.00)


def test_audit_total_falls_back_to_portal_when_full_precision_none(tmp_path: Path) -> None:
    """Legacy back-compat: a ScheduleTrRow with
    ``foreign_tax_inr_total_full_precision=None`` falls back to
    the portal-displayed total in the XLSX audit cell. Pre-PR-
    #216 callers and direct-constructor test fixtures rely on
    this."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1234"),
        foreign_tax_inr_total_full_precision=None,
    )
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    data = [c.value for c in ws[2]]
    assert data[7] == 1234  # fallback to portal field


def test_per_column_number_formats_applied_on_data_rows(tmp_path: Path) -> None:
    row = _make_row(
        foreign_tax_inr_total=Decimal("1000"),
        tax_relief_inr_total=Decimal("500"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    # Total tax paid (col 3 = index 2) gets the integer-rupee format.
    assert ws.cell(row=2, column=3).number_format == "#,##0"
    # Total tax relief (col 4 = index 3) gets the integer-rupee format when populated.
    assert ws.cell(row=2, column=4).number_format == "#,##0"
    # Full-precision audit (col 8 = index 7) gets the high-precision format.
    assert ws.cell(row=2, column=8).number_format == "#,##0.00000000"
    # FSI-rows-aggregated count (col 9 = index 8) gets the integer format.
    assert ws.cell(row=2, column=9).number_format == "0"


def test_none_portal_cells_have_default_format(tmp_path: Path) -> None:
    """tax_relief = None (not computed) → cell stays at the default
    'General' format (not the integer-rupee format)."""
    row = _make_row()  # tax_relief defaults to None
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    # Tax relief None → 'General' format (the format loop skipped it)
    assert ws.cell(row=2, column=4).value is None
    assert ws.cell(row=2, column=4).number_format == "General"
    # TIN None → 'General' format too.
    assert ws.cell(row=2, column=2).value is None
    assert ws.cell(row=2, column=2).number_format == "General"


# ---- Sort order ----------------------------------------------------------


def test_writer_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    row_us = _make_row(cbdt_country_code="2", iso_alpha2="US")
    row_gb = _make_row(
        cbdt_country_code="111", iso_alpha2="GB", country_name_portal="UNITED KINGDOM"
    )
    row_ag = _make_row(
        cbdt_country_code="1268",
        iso_alpha2="AG",
        country_name_portal="ANTIGUA AND BARBUDA",
    )
    expected_order = ["111", "1268", "2"]
    result = ScheduleTrComputeResult(rows=[row_gb, row_ag, row_us])

    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(result, path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    cbdt_cells = [ws.cell(row=r, column=1).value for r in range(2, 5)]
    assert cbdt_cells == expected_order


def test_no_summary_or_total_row_appended(tmp_path: Path) -> None:
    """TR has NO TOTAL row (DTAA-vs-non-DTAA sub-totals computed
    server-side). The file ends after the last detail row."""
    row1 = _make_row()
    row2 = _make_row(cbdt_country_code="111", iso_alpha2="GB", country_name_portal="UNITED KINGDOM")
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[row2, row1]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    assert ws.max_row == 3  # header + 2 detail rows
    last_data = [c.value for c in ws[ws.max_row]]
    assert last_data[0] == "2"


# ---- Writer-contract safety (string cells only) --------------------------


def test_nul_byte_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nul_detail_row(row: ScheduleTrRow) -> list[object]:
        return [
            "2",
            None,
            1000,
            None,
            None,
            "US",
            "UNITED STATES\x00OF AMERICA",
            Decimal("1000.00"),
            1,
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    def malicious_detail_row(row: ScheduleTrRow) -> list[object]:
        return [
            "2",
            None,
            1000,
            None,
            None,
            "US",
            f"{trigger}HYPERLINK",
            Decimal("1000.00"),
            1,
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)


# ---- Per-AY label threading ----------------------------------------------


def test_writer_defaults_to_current_ay_labels(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:5]) == PORTAL_COLUMN_HEADERS_TR


def test_writer_accepts_explicit_assessment_year_start(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter(assessment_year_start=2026)
    path = tmp_path / "tr.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:5]) == PORTAL_COLUMN_HEADERS_TR


def test_itr3_writer_uses_itr3_section_header(tmp_path: Path) -> None:
    writer = ScheduleTrXlsxWriter(assessment_year_start=2026, itr_form="ITR-3")
    path = tmp_path / "itr3.xlsx"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    ws = _open(path)[_SHEET_NAME_TR]
    assert ws.cell(row=1, column=5).value == (
        "Section under which relief claimed (specify 90, 90A or 91)"
    )


def test_writer_with_unknown_ay_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownAssessmentYearError):
        ScheduleTrXlsxWriter(assessment_year_start=2000)


# ---- Cross-format parity -------------------------------------------------


def test_csv_and_xlsx_emit_same_country_code_and_name_strings(
    tmp_path: Path,
) -> None:
    """The same compute result rendered through both writers produces
    identical Country Code + Country Name text. Locks the contract."""
    row = _make_row()
    result = ScheduleTrComputeResult(rows=[row])

    csv_path = tmp_path / "tr.csv"
    xlsx_path = tmp_path / "tr.xlsx"
    ScheduleTrReviewCsvWriter().write(result, csv_path)
    ScheduleTrXlsxWriter().write(result, xlsx_path)

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        csv_rows = list(csvmod.reader(fh))
    wb = _open(xlsx_path)
    ws = wb[_SHEET_NAME_TR]
    xlsx_data = [c.value for c in ws[2]]

    csv_data = csv_rows[1]
    assert csv_data[0] == xlsx_data[0]  # CBDT code
    # ISO + country name in audit cells must match.
    assert csv_data[5] == xlsx_data[5]
    assert csv_data[6] == xlsx_data[6]


def test_overwrites_existing_file(tmp_path: Path) -> None:
    """workbook.save overwrites an existing file (mirrors sister
    OS / CG / FSI XLSX writer pattern)."""
    writer = ScheduleTrXlsxWriter()
    path = tmp_path / "tr.xlsx"
    path.write_bytes(b"stale")
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_TR]
    assert ws.max_row == 2
