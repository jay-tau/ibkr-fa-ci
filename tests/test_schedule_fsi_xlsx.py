"""Tests for :mod:`ibkr_fa.output.schedule_fsi_xlsx`.

Mirrors the row-shape and per-AY threading tests in
``test_schedule_fsi_review_csv.py``, plus XLSX-specific concerns:

* numeric cells stay typed (not stringified)
* header row is bold + frozen
* writer-contract still rejects NUL + formula-injection triggers on
  string cells
* the file is a valid XLSX (round-trips through openpyxl on read)
"""

from __future__ import annotations

import csv as csvmod
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

import ibkr_fa.output.schedule_fsi_xlsx as xlsx_writer_mod
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
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
    PORTAL_COLUMN_HEADERS_FSI,
    UnknownAssessmentYearError,
)
from ibkr_fa.output.schedule_fsi_review_csv import (
    _AUDIT_HEADERS,
    ScheduleFsiReviewCsvWriter,
)
from ibkr_fa.output.schedule_fsi_xlsx import (
    _COLUMN_COUNT,
    _SHEET_NAME_FSI,
    ScheduleFsiXlsxWriter,
)


def _make_row(
    *,
    cbdt_country_code: str = "2",
    country_name_portal: str = "UNITED STATES OF AMERICA",
    iso_alpha2: str = "US",
    head_of_income: HeadOfIncome = HeadOfIncome.OTHER_SOURCES,
    income_inr: Decimal = Decimal("12345.6789"),
    foreign_tax_inr: Decimal = Decimal("1851.234"),
    event_count: int = 3,
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country_name_portal,
        iso_alpha2=iso_alpha2,
        head_of_income=head_of_income,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        event_count=event_count,
    )


def _open(path: Path):
    """Open an XLSX file written by the writer for assertions."""
    return load_workbook(filename=path)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_writer_registered_under_xlsx_format_id() -> None:
    assert output_binding(Subcommand.FSI, "xlsx") == OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FSI_XLSX), ScheduleFsiXlsxWriter
    )


def test_format_id_is_xlsx() -> None:
    assert ScheduleFsiXlsxWriter.format_id == "xlsx"


def test_available_fsi_formats_contains_both_writers() -> None:
    formats = available_formats(Subcommand.FSI)
    assert "review-csv" in formats
    assert "xlsx" in formats


# ---------------------------------------------------------------------------
# Sheet structure
# ---------------------------------------------------------------------------


def test_sheet_name_is_schedule_fsi(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    wb = _open(path)
    assert wb.sheetnames == [_SHEET_NAME_FSI]


def test_header_row_is_bold_and_frozen(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    # Frozen pane at A2 → row 1 is frozen.
    assert ws.freeze_panes == "A2"
    # Header row is bold.
    for cell in ws[1]:
        assert cell.font.bold is True


def test_header_cells_match_portal_then_audit(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:8]) == PORTAL_COLUMN_HEADERS_FSI
    assert tuple(header_values[8:]) == _AUDIT_HEADERS
    assert len(header_values) == _COLUMN_COUNT


def test_empty_result_emits_only_header(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    assert ws.max_row == 1


# ---------------------------------------------------------------------------
# Detail-row cell types and values
# ---------------------------------------------------------------------------


def test_single_row_renders_with_native_types(tmp_path: Path) -> None:
    row = _make_row()
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    assert ws.max_row == 2

    data = [c.value for c in ws[2]]
    # Portal cells (8):
    assert data[0] == "2"
    assert data[1] is None  # TIN
    assert data[2] == "Other Sources"
    # Whole-rupee integers (HALF_UP):
    assert data[3] == 12346
    assert isinstance(data[3], int)
    assert data[4] == 1851
    assert isinstance(data[4], int)
    assert data[5] is None  # Tax payable
    assert data[6] is None  # Tax relief
    assert data[7] is None  # DTAA
    # Audit cells (5):
    assert data[8] == "US"
    assert data[9] == "UNITED STATES OF AMERICA"
    # Full-precision INR amounts coerced to float for Excel.
    assert isinstance(data[10], float)
    assert data[10] == pytest.approx(12345.6789)
    assert isinstance(data[11], float)
    assert data[11] == pytest.approx(1851.234)
    assert data[12] == 3
    assert isinstance(data[12], int)


def test_zero_foreign_tax_row_emits_numeric_zero(tmp_path: Path) -> None:
    """CG-only bucket with no foreign withholding → tax cell is
    numeric ``0`` (not ``None`` / not the string ``"0"``)."""
    row = _make_row(foreign_tax_inr=Decimal("0"))
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    data = [c.value for c in ws[2]]
    assert data[4] == 0  # tax portal cell (column 5)
    assert isinstance(data[4], int)
    # openpyxl reads float(0.0) back as int(0) — the round-trip
    # collapses whole-number floats. The cell was WRITTEN as a
    # numeric (float coerced from Decimal); the type on read is
    # int because openpyxl optimises whole-number floats to int.
    assert data[11] == 0  # tax audit cell (column 12)
    assert isinstance(data[11], int)


def test_capital_gains_head_renders_portal_label(tmp_path: Path) -> None:
    row = _make_row(head_of_income=HeadOfIncome.CAPITAL_GAINS)
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    data = [c.value for c in ws[2]]
    assert data[2] == "Capital Gains"


def test_rounding_is_half_up(tmp_path: Path) -> None:
    """1.500 rounds to 2 (HALF_UP); 1.499 rounds to 1."""
    row = _make_row(
        income_inr=Decimal("1.499"),
        foreign_tax_inr=Decimal("1.500"),
    )
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    data = [c.value for c in ws[2]]
    assert data[3] == 1  # income (8-col portal block)
    assert data[4] == 2  # tax


def test_portal_amounts_are_the_compute_filed_values(
    tmp_path: Path, filed_without_rounding
) -> None:
    """#900: the portal amount cells are :attr:`ScheduleFsiRow.filed` as it
    is, not another rounding of the raw amounts; the audit cells keep the raw
    amounts."""
    row = replace(
        _make_row(),
        tax_payable_inr=Decimal("1999.6"),
        tax_relief_inr=Decimal("1851.234"),
        dtaa_article="Article 25(2)(a)",
    )
    filed_without_rounding(
        row,
        income_inr=Decimal("12001"),
        foreign_tax_inr=Decimal("1801"),
        tax_payable_inr=Decimal("1901"),
        tax_relief_inr=Decimal("1801"),
    )
    path = tmp_path / "fsi.xlsx"
    ScheduleFsiXlsxWriter().write(ScheduleFsiComputeResult(rows=[row]), path)

    data = [c.value for c in _open(path)[_SHEET_NAME_FSI][2]]
    assert data[3:7] == [12001, 1801, 1901, 1801]
    assert all(isinstance(value, int) for value in data[3:7])
    assert data[10:12] == [pytest.approx(12345.6789), pytest.approx(1851.234)]


def test_per_column_number_formats_applied_on_data_rows(tmp_path: Path) -> None:
    row = _make_row()
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    # Income + Tax-paid portal cells (columns 4 + 5 in 1-indexed) get
    # the integer-rupee format.
    assert ws.cell(row=2, column=4).number_format == "#,##0"
    assert ws.cell(row=2, column=5).number_format == "#,##0"
    # Full-precision audit cells (columns 11 + 12) get the high-
    # precision format.
    assert ws.cell(row=2, column=11).number_format == "#,##0.00000000"
    assert ws.cell(row=2, column=12).number_format == "#,##0.00000000"
    # Event count (column 13) gets the integer format.
    assert ws.cell(row=2, column=13).number_format == "0"


def test_none_portal_cells_have_no_number_format_override(
    tmp_path: Path,
) -> None:
    """For empty (None) portal cells (TIN, Tax payable, etc.) the
    writer skips applying the per-column format — the cell stays as
    the default General number format. Locks the behaviour so future
    refactors don't accidentally apply a numeric format to a None
    cell (which would render as the default General which is fine,
    but masks the "not computed" semantics)."""
    row = _make_row()
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    # column 6 = Tax payable (None: _make_row sets none) — its format
    # must NOT have been set to the rupee format.
    assert ws.cell(row=2, column=6).value is None
    # The default cell format is 'General', not the integer-rupee format.
    assert ws.cell(row=2, column=6).number_format == "General"
    # column 7 = Tax relief available in India (also None) — same
    # `cell.value is None` skip path. Locks the skip for both rupee
    # columns this fixture leaves empty.
    assert ws.cell(row=2, column=7).value is None
    assert ws.cell(row=2, column=7).number_format == "General"


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


def test_writer_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    row_us = _make_row(cbdt_country_code="2", iso_alpha2="US")
    row_gb = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    row_ag = _make_row(
        cbdt_country_code="1268",
        iso_alpha2="AG",
        country_name_portal="ANTIGUA AND BARBUDA",
    )
    # Lexicographic order on CBDT code:
    expected_order = ["111", "1268", "2"]
    result = ScheduleFsiComputeResult(rows=[row_gb, row_ag, row_us])

    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(result, path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    cbdt_cells = [ws.cell(row=r, column=1).value for r in range(2, 5)]
    assert cbdt_cells == expected_order


def test_no_summary_or_total_row_appended(tmp_path: Path) -> None:
    """FSI has NO TOTAL row. The file ends after the last detail row."""
    row1 = _make_row()
    row2 = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[row2, row1]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    assert ws.max_row == 3  # header + 2 detail rows
    # Last row is a normal detail row (US sorted last lexicographically).
    last_data = [c.value for c in ws[ws.max_row]]
    assert last_data[0] == "2"
    # And the rounded-income cell (column 4 in 1-indexed = data[3])
    # is the integer rupee — NOT a totals label.
    assert isinstance(last_data[3], int)


# ---------------------------------------------------------------------------
# Writer-contract safety (string cells only)
# ---------------------------------------------------------------------------


def test_nul_byte_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's ALL-CAPS / numeric-only invariants block NUL bytes
    from landing in `cbdt_country_code` / `country_name_portal` at
    construction time, so the writer's defence-in-depth contract guard
    is exercised by patching `_detail_row` to emit a NUL-bearing cell."""

    def nul_detail_row(row):
        return [
            "2",  # Country Code
            None,  # TIN
            "Other Sources",  # Head of Income
            100,  # Income (rounded)
            10,  # Tax paid (rounded)
            None,  # Tax payable
            None,  # Tax relief
            None,  # DTAA
            "US",  # _audit: ISO alpha-2
            "UNITED STATES\x00OF AMERICA",  # _audit: Country name (portal)
            Decimal("100.00"),  # _audit: Income INR
            Decimal("10.00"),  # _audit: Tax INR
            1,  # _audit: Event count
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    """Same defence-in-depth wiring as the CSV writer's analog. The
    XLSX writer's guard fires on string cells regardless of which
    string cell carries the trigger."""

    def malicious_detail_row(row):
        return [
            "2",
            None,
            "Other Sources",
            100,
            10,
            None,
            None,
            None,
            "US",
            f"{trigger}HYPERLINK",  # formula trigger in audit slot
            Decimal("100.00"),
            Decimal("10.00"),
            1,
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)


# ---------------------------------------------------------------------------
# Per-AY label threading
# ---------------------------------------------------------------------------


def test_writer_defaults_to_current_ay_labels(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:8]) == PORTAL_COLUMN_HEADERS_FSI


def test_writer_accepts_explicit_assessment_year_start(tmp_path: Path) -> None:
    writer = ScheduleFsiXlsxWriter(assessment_year_start=2026)
    path = tmp_path / "fsi.xlsx"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    header_values = [c.value for c in ws[1]]
    assert tuple(header_values[:8]) == PORTAL_COLUMN_HEADERS_FSI


def test_writer_with_unknown_ay_raises(tmp_path: Path) -> None:

    with pytest.raises(UnknownAssessmentYearError):
        ScheduleFsiXlsxWriter(assessment_year_start=2000)


# ---------------------------------------------------------------------------
# Output-format-registry parity
# ---------------------------------------------------------------------------


def test_csv_and_xlsx_emit_same_country_name_and_head_strings(
    tmp_path: Path,
) -> None:
    """Cross-format parity: the same compute result rendered through
    both the CSV and the XLSX writer must produce identical Country
    Name + Head of Income text. Locks the contract that XLSX is not
    silently transforming portal strings."""

    row = _make_row(head_of_income=HeadOfIncome.CAPITAL_GAINS)
    result = ScheduleFsiComputeResult(rows=[row])

    csv_path = tmp_path / "fsi.csv"
    xlsx_path = tmp_path / "fsi.xlsx"
    ScheduleFsiReviewCsvWriter().write(result, csv_path)
    ScheduleFsiXlsxWriter().write(result, xlsx_path)

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        csv_rows = list(csvmod.reader(fh))
    wb = _open(xlsx_path)
    ws = wb[_SHEET_NAME_FSI]
    xlsx_data = [c.value for c in ws[2]]

    csv_data = csv_rows[1]
    # Country Code + Head of Income strings must match exactly.
    assert csv_data[0] == xlsx_data[0]
    assert csv_data[2] == xlsx_data[2]
    # ISO alpha-2 + Country Name audit cells must match.
    assert csv_data[8] == xlsx_data[8]
    assert csv_data[9] == xlsx_data[9]


def test_overwrites_existing_file(tmp_path: Path) -> None:
    """The writer replaces an existing file at path (saved atomically
    through ``atomic_output_path``)."""
    writer = ScheduleFsiXlsxWriter()
    path = tmp_path / "fsi.xlsx"
    path.write_bytes(b"stale")
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    wb = _open(path)
    ws = wb[_SHEET_NAME_FSI]
    assert ws.max_row == 2
