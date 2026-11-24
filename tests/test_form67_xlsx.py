"""Tests for :mod:`ibkr_fa.output.form67_xlsx`.

Mirrors :mod:`tests.test_output_form67_review_csv` plus XLSX-specific
concerns (native cell types, bold + frozen header, writer-contract on
string cells, valid-XLSX round-trip).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from openpyxl.workbook import Workbook

import ibkr_fa.output.form67_xlsx as xlsx_writer_mod
from ibkr_fa.compute.form67 import (
    Form67ComputeResult,
    Form67Entry,
    Form67SourceOfIncome,
    Form67TreatyRateStatus,
)
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
from ibkr_fa.output.form67_review_csv import _AUDIT_HEADERS
from ibkr_fa.output.form67_xlsx import (
    _COLUMN_COUNT,
    _SHEET_NAME_FORM67,
    Form67XlsxWriter,
)
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_FORM67,
    UnknownAssessmentYearError,
)


def _make_entry(
    *,
    serial_number: int = 1,
    cbdt_country_code: str = "2",
    country_name_portal: str = "UNITED STATES OF AMERICA",
    iso_alpha2: str = "US",
    source_of_income: Form67SourceOfIncome = Form67SourceOfIncome.DIVIDEND,
    income_inr: Decimal = Decimal("10000.50"),
    foreign_tax_inr: Decimal = Decimal("1500.75"),
    foreign_tax_rate_percent: Decimal | None = None,
    tax_paid_rate_percent: Decimal | None = None,
    event_count: int = 3,
    tax_payable_normal_inr: Decimal | None = None,
    tax_payable_special_inr: Decimal | None = None,
    section_90_dtaa_article: str | None = None,
    section_90_dtaa_rate_percent: Decimal | None = None,
    section_90_credit_inr: Decimal | None = None,
    section_91_credit_inr: Decimal | None = None,
    total_credit_inr: Decimal | None = None,
) -> Form67Entry:
    if foreign_tax_rate_percent is None:
        foreign_tax_rate_percent = (foreign_tax_inr / income_inr) * Decimal("100")
    treaty_rate_status = None
    if section_90_dtaa_rate_percent is not None:
        # A DTAA entry has a source rate; by default it is the INR ratio.
        if tax_paid_rate_percent is None:
            tax_paid_rate_percent = foreign_tax_rate_percent
        treaty_rate_status = (
            Form67TreatyRateStatus.WITHIN
            if tax_paid_rate_percent <= section_90_dtaa_rate_percent
            else Form67TreatyRateStatus.ABOVE
        )
    return Form67Entry(
        serial_number=serial_number,
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country_name_portal,
        iso_alpha2=iso_alpha2,
        source_of_income=source_of_income,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        foreign_tax_rate_percent=foreign_tax_rate_percent,
        event_count=event_count,
        tax_payable_normal_inr=tax_payable_normal_inr,
        tax_payable_special_inr=tax_payable_special_inr,
        section_90_dtaa_article=section_90_dtaa_article,
        section_90_dtaa_rate_percent=section_90_dtaa_rate_percent,
        eligible_foreign_tax_inr=(
            foreign_tax_inr if section_90_dtaa_rate_percent is not None else None
        ),
        section_90_credit_inr=section_90_credit_inr,
        section_91_credit_inr=section_91_credit_inr,
        total_credit_inr=total_credit_inr,
        tax_paid_rate_percent=tax_paid_rate_percent,
        highest_payment_rate_percent=(
            None if treaty_rate_status is None else tax_paid_rate_percent
        ),
        treaty_rate_status=treaty_rate_status,
    )


def _open(path: Path) -> Workbook:
    return load_workbook(filename=path)


# ---- Registry ------------------------------------------------------------


def test_writer_registered_under_xlsx_format_id() -> None:
    assert output_binding(Subcommand.FORM67, "xlsx") == OutputBinding(
        OutputKey(Destination.WORKBOOK, Artifact.FORM67_XLSX), Form67XlsxWriter
    )


def test_format_id_is_xlsx() -> None:
    assert Form67XlsxWriter.format_id == "xlsx"


def test_available_form67_formats_includes_portal_review_and_xlsx() -> None:
    formats = set(available_formats(Subcommand.FORM67))
    assert "portal-csv" in formats
    assert "review-csv" in formats
    assert "xlsx" in formats


# ---- Header + column-count contract --------------------------------------


def test_column_count_matches_csv_writer() -> None:
    """XLSX writer must use the same 17-cell column layout as the CSV
    writer for cross-format parity."""
    assert _COLUMN_COUNT == 17


def test_sheet_name_is_form_67(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=()), path)
    wb = _open(path)
    assert wb.active is not None
    assert wb.active.title == _SHEET_NAME_FORM67


def test_header_row_uses_portal_column_headers_form67_plus_audit(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=()), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    header_row = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    assert len(header_row) == _COLUMN_COUNT
    assert tuple(header_row[:13]) == PORTAL_COLUMN_HEADERS_FORM67
    assert tuple(header_row[13:]) == _AUDIT_HEADERS


def test_header_only_with_no_entries(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=()), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    # Header row only — second row is empty (no detail rows).
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) == 1


# ---- Detail rows: cols 7-13 None → empty cells ---------------------------


def test_single_entry_round_trip_leaves_none_cols_7_to_13_empty(tmp_path: Path) -> None:
    """Cols 7-13 left None → truly empty Excel cells (not 0). Portal
    cells 1-6 are populated by the compute layer; this fixture sets
    none of cols 7-13.
    """
    entry = _make_entry()
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) == 2  # header + 1 detail
    _header, detail = rows
    assert len(detail) == _COLUMN_COUNT
    # Portal cells (13):
    assert detail[0] == 1  # Sl. No. — int
    assert detail[1] == "2-UNITED STATES OF AMERICA"  # country dropdown
    assert detail[2] == "Dividend"  # source of income (enum value)
    assert detail[3] == 10001  # income — HALF_UP whole rupees as int
    assert detail[4] == 1501  # foreign tax — HALF_UP whole rupees as int
    # Rate is a Decimal coerced to float for Excel.
    assert detail[5] == pytest.approx(15.0067, rel=1e-3)
    # Cols 7-13 (optional) — None (truly empty cells).
    assert detail[6] is None  # tax_payable_normal_inr
    assert detail[7] is None  # tax_payable_special_inr
    assert detail[8] is None  # §90/90A - DTAA Article No.
    assert detail[9] is None  # §90/90A - DTAA Rate
    assert detail[10] is None  # §90/90A - Amount
    assert detail[11] is None  # §91 - Amount
    assert detail[12] is None  # Total foreign tax credit claimed
    # Audit cells (3):
    assert detail[13] == "US"
    assert detail[14] == pytest.approx(1500.75, rel=1e-6)  # full-precision foreign tax
    assert detail[15] == 3  # event count


# ---- "Computed zero" vs "not computed" distinction (cols 7/8/11/12/13) ---


def test_computed_zero_section_90_credit_renders_as_numeric_zero_not_empty(
    tmp_path: Path,
) -> None:
    """Mirrors PR #215 T3 r1 P2 #1: Decimal("0") (computed but worked
    out to zero) renders as numeric ``0``; distinct from None (not
    computed) which renders as truly-empty Excel cell."""
    entry = _make_entry(
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("15"),
        section_90_credit_inr=Decimal("0"),
        total_credit_inr=Decimal("0"),
    )
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    detail = list(ws.iter_rows(values_only=True))[1]
    assert detail[10] == 0  # §90 credit — numeric, NOT None
    assert detail[12] == 0  # total credit — numeric, NOT None


def test_populated_optional_columns_render_as_native_types(tmp_path: Path) -> None:
    """Populated cols 7-13 render INR cells as HALF_UP int, the DTAA
    rate as float, and the DTAA article as a string."""
    entry = _make_entry(
        income_inr=Decimal("10000"),
        foreign_tax_inr=Decimal("1500"),
        tax_payable_normal_inr=Decimal("3000.40"),
        tax_payable_special_inr=Decimal("2000.60"),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("12.5"),
        section_90_credit_inr=Decimal("1500"),
        total_credit_inr=Decimal("1500"),
    )
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    detail = list(ws.iter_rows(values_only=True))[1]
    assert detail[6] == 3000  # tax_payable_normal — HALF_UP int
    assert detail[7] == 2001  # tax_payable_special — HALF_UP int (2000.60 → 2001)
    assert detail[8] == "Article 10(2)(b)"  # DTAA article verbatim
    assert detail[9] == pytest.approx(12.5, rel=1e-6)  # DTAA rate as float
    assert detail[10] == 1500  # §90 credit — int
    assert detail[11] is None  # §91 credit — still None
    assert detail[12] == 1500  # total credit — int


def test_rate_cells_pre_quantize_half_up_to_match_csv(tmp_path: Path) -> None:
    """T3 r0 P2 of PR #229: the foreign-tax-rate cell (col 6) and the
    DTAA-rate cell (col 10) must hold the HALF_UP two-decimal rate,
    matching the CSV writer's filed rates. Since #898 the cells are compute's filed
    rates (:attr:`Form67Entry.filed`), so the writer doesn't round.

    A tie: Decimal("3.465") under ROUND_HALF_UP → Decimal("3.47").
    ROUND_HALF_EVEN would give Decimal("3.46") instead.

    Verify the stored numeric values match the filed 3.47 within the
    asserted tolerance, rather than the unquantized 3.465.
    """
    entry = _make_entry(
        # Construct income / tax so the model's rate-invariant accepts
        # foreign_tax_rate_percent = 3.465 exactly:
        # tax / income * 100 = 34.65 / 1000 * 100 = 3.465.
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("34.65"),
        foreign_tax_rate_percent=Decimal("3.465"),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("3.465"),
    )
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    detail = list(ws.iter_rows(values_only=True))[1]
    assert entry.filed.tax_paid_rate_percent == entry.filed.section_90_dtaa_rate_percent
    assert entry.filed.tax_paid_rate_percent == Decimal("3.47")
    # Col 6 (foreign-tax-rate) — the filed Decimal("3.47"), coerced to
    # float(3.47), checked as a numeric cell value.
    assert detail[5] == pytest.approx(3.47, rel=1e-6)
    # Col 10 (DTAA-rate) — same treatment.
    assert detail[9] == pytest.approx(3.47, rel=1e-6)
    # Anti-regression: the raw 3.465 float would not match 3.47 to
    # this tolerance.
    assert detail[5] != pytest.approx(3.465, rel=1e-6)


def test_xlsx_rate_uses_source_currency_rate_when_available(tmp_path: Path) -> None:
    """Synthetic rates: the INR ratio (25.40 / 100 * 100 = 25.40%) is a
    little above the 25% DTAA cap, 0.45 above the source-currency rate
    (24.95%). Column 6 holds the source-currency rate."""
    entry = _make_entry(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25.40"),
        foreign_tax_rate_percent=Decimal("25.40"),
        tax_paid_rate_percent=Decimal("24.95"),
    )
    path = tmp_path / "form67.xlsx"

    Form67XlsxWriter().write(Form67ComputeResult(entries=(entry,)), path)

    ws = _open(path).active
    assert ws is not None
    detail = list(ws.iter_rows(values_only=True))[1]
    assert detail[5] == pytest.approx(24.95, rel=1e-6)


# ---- Sort order preserved + No TOTAL row ---------------------------------


def test_multi_entry_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    """Compute layer sorts by cbdt_country_code lexicographically;
    writer preserves order via the audit ISO column."""
    entry_gb = _make_entry(
        serial_number=1,
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    entry_ag = _make_entry(
        serial_number=2,
        cbdt_country_code="1268",
        iso_alpha2="AG",
        country_name_portal="ANTIGUA AND BARBUDA",
    )
    entry_us = _make_entry(serial_number=3, cbdt_country_code="2", iso_alpha2="US")
    result = Form67ComputeResult(entries=(entry_gb, entry_ag, entry_us))

    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(result, path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    detail_rows = list(ws.iter_rows(min_row=2, values_only=True))
    iso_alpha2_col = [r[13] for r in detail_rows]
    assert iso_alpha2_col == ["GB", "AG", "US"]
    # Serial numbers preserved in tuple order.
    serial_col = [r[0] for r in detail_rows]
    assert serial_col == [1, 2, 3]


def test_no_total_row_appended(tmp_path: Path) -> None:
    """Form 67 has NO portal-TOTAL row — XLSX writer ends after the
    last detail row (same contract as CSV)."""
    entry_us = _make_entry(serial_number=1, cbdt_country_code="2", iso_alpha2="US")
    entry_gb = _make_entry(serial_number=2, cbdt_country_code="44", iso_alpha2="GB")
    result = Form67ComputeResult(entries=(entry_us, entry_gb))

    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(result, path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    rows = list(ws.iter_rows(values_only=True))
    assert len(rows) == 3  # header + 2 detail; NO totals row


# ---- Bold + frozen header ------------------------------------------------


def test_header_row_is_bold(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    for cell in next(ws.iter_rows(min_row=1, max_row=1)):
        assert cell.font.bold is True


def test_header_row_is_frozen(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    assert ws.freeze_panes == "A2"


# ---- Writer-contract safety: NUL bytes + formula injection --------------


def test_nul_byte_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model invariants block bad strings at construction; exercise
    the writer's defence-in-depth contract guard via monkeypatch."""

    def nul_detail_row(_entry: Form67Entry) -> list[object]:
        return [
            1,
            "2-UNITED\x00STATES OF AMERICA",  # NUL in country dropdown
            "Dividend",
            10000,
            1500,
            Decimal("15.0"),
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "US",
            Decimal("1500.75"),
            3,
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", nul_detail_row)
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_string_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    def malicious_detail_row(_entry: Form67Entry) -> list[object]:
        return [
            1,
            "2-UNITED STATES OF AMERICA",
            "Dividend",
            10000,
            1500,
            Decimal("15.0"),
            None,
            None,
            f"{trigger}HYPERLINK",  # formula trigger in DTAA article slot
            None,
            None,
            None,
            None,
            "US",
            Decimal("1500.75"),
            3,
        ]

    monkeypatch.setattr(xlsx_writer_mod, "_detail_row", malicious_detail_row)
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    with pytest.raises(WriterContractError):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)


# ---- Per-AY portal-labels selection --------------------------------------


def test_default_assessment_year_uses_current_ay_labels(tmp_path: Path) -> None:
    writer = Form67XlsxWriter()
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=()), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    header_row = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    assert tuple(header_row[:13]) == PORTAL_COLUMN_HEADERS_FORM67


def test_explicit_assessment_year_2026_resolves_to_2026_27_snapshot(
    tmp_path: Path,
) -> None:
    writer = Form67XlsxWriter(assessment_year_start=2026)
    path = tmp_path / "form67.xlsx"
    writer.write(Form67ComputeResult(entries=()), path)
    wb = _open(path)
    ws = wb.active
    assert ws is not None
    header_row = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
    assert tuple(header_row[:13]) == PORTAL_COLUMN_HEADERS_FORM67


def test_explicit_unregistered_assessment_year_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownAssessmentYearError):
        Form67XlsxWriter(assessment_year_start=2020)
