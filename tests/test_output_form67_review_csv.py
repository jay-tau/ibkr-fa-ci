"""Tests for :mod:`ibkr_fa.output.form67_review_csv`."""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

import pytest

import ibkr_fa.output.form67_review_csv as csv_writer_mod
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
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.form67_review_csv import (
    _AUDIT_HEADERS,
    _COLUMN_COUNT,
    Form67ReviewCsvWriter,
)
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_FORM67,
    UnknownAssessmentYearError,
)

# ---- shared fixtures -----------------------------------------------------


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
    """Build a :class:`Form67Entry` with sane defaults that satisfy the
    model invariants (income > 0, rate ≈ tax/income*100 to tolerance).
    """
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


def _read(path: Path) -> list[list[str]]:
    """Read a CSV file emitted by the writer back into rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


# ---- Registry ------------------------------------------------------------


def test_writer_is_registered_under_review_csv_format_id() -> None:
    assert output_binding(Subcommand.FORM67, "review-csv") == OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FORM67_CSV), Form67ReviewCsvWriter
    )


def test_writer_class_attribute_format_id_is_review_csv() -> None:
    assert Form67ReviewCsvWriter.format_id == "review-csv"


# ---- Header + column-count contract --------------------------------------


def test_column_count_is_seventeen() -> None:
    assert _COLUMN_COUNT == 17


def test_audit_headers_are_four_cells() -> None:
    assert len(_AUDIT_HEADERS) == 4


def test_portal_headers_are_thirteen_cells() -> None:
    assert len(PORTAL_COLUMN_HEADERS_FORM67) == 13


def test_header_is_seventeen_cells_and_starts_with_portal_columns(tmp_path: Path) -> None:
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=()), path)

    rows = _read(path)
    assert len(rows) == 1
    assert len(rows[0]) == _COLUMN_COUNT
    assert tuple(rows[0][:13]) == PORTAL_COLUMN_HEADERS_FORM67
    assert tuple(rows[0][13:]) == _AUDIT_HEADERS


def test_header_only_with_no_entries(tmp_path: Path) -> None:
    """Empty compute result emits ONLY the header row."""
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=()), path)
    rows = _read(path)
    assert len(rows) == 1


# ---- Detail rows: cols 7-13 None → EMPTY ---------------------------------


def test_single_entry_round_trip_leaves_none_cols_7_to_13_empty(tmp_path: Path) -> None:
    """Cols 7-13 left None → EMPTY cells.

    Portal cells 1-6 are always populated by the compute layer; 7-13
    are optional, and this fixture sets none of them.
    """
    entry = _make_entry()
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)

    rows = _read(path)
    assert len(rows) == 2

    _header, data = rows
    assert len(data) == _COLUMN_COUNT
    # Portal cells (13):
    assert data[0] == "1"  # Sl. No.
    assert data[1] == "2-UNITED STATES OF AMERICA"  # combined country (format_country_for_portal)
    assert data[2] == "Dividend"  # source of income enum value
    assert data[3] == "10001"  # income — HALF_UP whole rupee
    assert data[4] == "1501"  # foreign tax — HALF_UP whole rupee
    assert data[5] == "15.01"  # rate — two decimal places (1500.75 / 10000.50 * 100)
    assert data[6] == ""  # col 7: tax_payable_normal — EMPTY (None)
    assert data[7] == ""  # col 8: tax_payable_special — EMPTY (None)
    assert data[8] == ""  # col 9: dtaa article — EMPTY (None)
    assert data[9] == ""  # col 10: dtaa rate — EMPTY (None)
    assert data[10] == ""  # col 11: §90 credit — EMPTY (None)
    assert data[11] == ""  # col 12: §91 credit — EMPTY (None)
    assert data[12] == ""  # col 13: total credit — EMPTY (None)
    # Audit cells (4):
    assert data[13] == "US"
    assert data[14] == "1500.75"  # full-precision foreign tax
    assert data[15] == "3"  # event count
    assert data[16] == "true"


# ---- "Computed zero" vs "not computed" distinction (cols 11/12/13) -------


def test_computed_zero_section_90_credit_renders_as_literal_zero_not_empty(
    tmp_path: Path,
) -> None:
    """Mirrors Schedule TR PR #215 T3 r1 P2 #1 fix at the writer
    layer: Decimal("0") (computed but worked out to zero) renders as
    literal '0', NOT empty. Distinct from None (not computed).
    """
    entry = _make_entry(
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("15"),
        section_90_credit_inr=Decimal("0"),
        total_credit_inr=Decimal("0"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[10] == "0"  # §90 credit — NOT empty
    assert data[12] == "0"  # total credit — NOT empty


def test_computed_zero_section_91_credit_renders_as_literal_zero_not_empty(
    tmp_path: Path,
) -> None:
    """Same as §90 but on the §91 branch."""
    entry = _make_entry(
        section_91_credit_inr=Decimal("0"),
        total_credit_inr=Decimal("0"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[11] == "0"  # §91 credit — NOT empty
    assert data[12] == "0"  # total credit — NOT empty


def test_populated_optional_columns_render_correctly(tmp_path: Path) -> None:
    """Populated optional fields render as HALF_UP whole rupees + DTAA
    rate two-decimals."""
    entry = _make_entry(
        income_inr=Decimal("10000"),
        foreign_tax_inr=Decimal("1500"),
        tax_payable_normal_inr=Decimal("3000.40"),
        tax_payable_special_inr=Decimal("2000.60"),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("15"),
        section_90_credit_inr=Decimal("1500"),
        total_credit_inr=Decimal("1500"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[6] == "3000"  # tax_payable_normal — HALF_UP
    assert data[7] == "2001"  # tax_payable_special — HALF_UP (2000.60 → 2001)
    assert data[8] == "Article 10(2)(b)"  # dtaa article verbatim
    assert data[9] == "15.00"  # dtaa rate — two decimal places
    assert data[10] == "1500"  # §90 credit
    assert data[11] == ""  # §91 credit — still None
    assert data[12] == "1500"  # total credit


def test_fractional_dtaa_rate_renders_with_two_decimal_places(tmp_path: Path) -> None:
    """DTAA rate is a Decimal in percentage units; the column (11)
    note in the form67_entry module docstring explicitly notes both
    integer (15) and fractional (12.5) percentages are valid. The
    writer must render fractional rates without truncation."""
    entry = _make_entry(
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("12.5"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[9] == "12.50"


def test_rate_rendering_uses_half_up_not_half_even(tmp_path: Path) -> None:
    """Two-decimal rate cells (cols 6 + 10) must round HALF_UP per
    the CBDT portal convention, not Python's default HALF_EVEN. A
    tie case: Decimal("3.465") must render as "3.47"
    (HALF_UP), not "3.46" (HALF_EVEN). T3 r0 P3 of PR #226."""
    # Construct an entry whose foreign_tax_rate_percent is exactly
    # 3.465 (a HALF_UP-vs-HALF_EVEN tie case) by passing
    # the rate explicitly. The model validates rate ≈ tax/income*100
    # to a tolerance, so use compatible income / tax values:
    # income=1_000_000, tax=34_650 → rate = 3.465%.
    entry = _make_entry(
        income_inr=Decimal("1000000"),
        foreign_tax_inr=Decimal("34650"),
        foreign_tax_rate_percent=Decimal("3.465"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    # HALF_UP: 3.465 → 3.47. Default Python f"{Decimal('3.465'):.2f}"
    # (HALF_EVEN) would produce "3.46".
    assert data[5] == "3.47"


def test_review_rate_uses_source_currency_rate_when_available(tmp_path: Path) -> None:
    """Synthetic rates: the INR ratio (25.40 / 100 * 100 = 25.40%) is a
    little above the 25% DTAA cap, 0.45 above the source-currency rate
    (24.95%). Column 6 renders the source-currency rate."""
    entry = _make_entry(
        income_inr=Decimal("100"),
        foreign_tax_inr=Decimal("25.40"),
        foreign_tax_rate_percent=Decimal("25.40"),
        tax_paid_rate_percent=Decimal("24.95"),
    )
    path = tmp_path / "form67.csv"

    Form67ReviewCsvWriter().write(Form67ComputeResult(entries=(entry,)), path)

    _, data = _read(path)
    assert data[5] == "24.95"


def test_dtaa_rate_rendering_uses_half_up_not_half_even(tmp_path: Path) -> None:
    """Same HALF_UP guarantee as the foreign-tax-rate cell, applied
    to the DTAA rate cell (col 10) via _format_optional_rate."""
    entry = _make_entry(
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("3.465"),
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[9] == "3.47"


# ---- Sort order (preserved from compute layer) ---------------------------


def test_multi_entry_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    """Compute layer sorts by (cbdt_country_code, source_of_income.value)
    lexicographically with 1-indexed serial_number preserved. Writer
    preserves that order — no re-sort.
    """
    # Build entries already in the compute-sorted order so the
    # Form67ComputeResult post-init invariants pass.
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
    entry_us = _make_entry(
        serial_number=3,
        cbdt_country_code="2",
        iso_alpha2="US",
        country_name_portal="UNITED STATES OF AMERICA",
    )
    # Compute layer sorts by entry.cbdt_country_code (the field, not the
    # format-helper lookup). Verify sort preservation via the audit ISO
    # cell which preserves entry order verbatim.
    expected_iso_order = ["GB", "AG", "US"]
    result = Form67ComputeResult(entries=(entry_gb, entry_ag, entry_us))

    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(result, path)
    rows = _read(path)
    detail_iso = [r[13] for r in rows[1:]]  # audit ISO column
    assert detail_iso == expected_iso_order


def test_serial_numbers_preserved_one_indexed_in_emitted_order(tmp_path: Path) -> None:
    """The Sl. No. column reflects the 1-indexed serial number from
    the compute layer, which guarantees 1..N in tuple order."""
    entry_a = _make_entry(serial_number=1, cbdt_country_code="111", iso_alpha2="GB")
    entry_b = _make_entry(serial_number=2, cbdt_country_code="2", iso_alpha2="US")
    result = Form67ComputeResult(entries=(entry_a, entry_b))

    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(result, path)
    rows = _read(path)
    serials = [r[0] for r in rows[1:]]
    assert serials == ["1", "2"]


# ---- No TOTAL row (Form 67 design choice) --------------------------------


def test_no_summary_or_total_row_appended(tmp_path: Path) -> None:
    """Form 67 has NO TOTAL row — the e-filing portal accepts one row
    per (country, source-of-income) aggregate and computes cross-row
    sums server-side. Locks the contract so a future refactor doesn't
    add a phantom totals row.
    """
    entry_a = _make_entry(serial_number=1, cbdt_country_code="111", iso_alpha2="GB")
    entry_b = _make_entry(serial_number=2, cbdt_country_code="2", iso_alpha2="US")
    result = Form67ComputeResult(entries=(entry_a, entry_b))

    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(result, path)
    rows = _read(path)
    assert len(rows) == 3  # header + 2 detail rows; NO TOTAL row


# ---- Encoding + uniform width --------------------------------------------


def test_utf8_sig_bom_present(tmp_path: Path) -> None:
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    with path.open("rb") as fh:
        head = fh.read(3)
    assert head == b"\xef\xbb\xbf"  # UTF-8 BOM


def test_every_row_has_sixteen_cells(tmp_path: Path) -> None:
    entry_a = _make_entry(serial_number=1, cbdt_country_code="111", iso_alpha2="GB")
    entry_b = _make_entry(serial_number=2, cbdt_country_code="2", iso_alpha2="US")
    result = Form67ComputeResult(entries=(entry_a, entry_b))

    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(result, path)
    rows = _read(path)
    assert all(len(r) == _COLUMN_COUNT for r in rows)


# ---- Writer-contract safety: NUL byte rejection --------------------------


def test_nul_byte_in_cell_raises_writer_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model invariants block bad strings at construction; exercise
    the writer's defence-in-depth contract guard via monkeypatch."""

    def nul_detail_row(_entry: Form67Entry, **_kwargs: object) -> list[str]:
        return [
            "1",
            "2-UNITED STATES OF AMERICA",
            "DIVIDEND",
            "10000",
            "1500",
            "15.00",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "US",
            "1500.00\x00",  # NUL in audit cell
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    with pytest.raises(WriterContractError):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)


def test_nul_byte_violation_does_not_leave_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nul_detail_row(_entry: Form67Entry, **_kwargs: object) -> list[str]:
        return [
            "1",
            "2-UNITED STATES OF AMERICA",
            "DIVIDEND",
            "10000",
            "1500",
            "15.00",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "US",
            "1500.00\x00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    with pytest.raises(WriterContractError):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    assert not path.exists()


# ---- Writer-contract safety: formula injection rejection -----------------


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    def malicious_detail_row(_entry: Form67Entry, **_kwargs: object) -> list[str]:
        return [
            "1",
            "2-UNITED STATES OF AMERICA",
            "DIVIDEND",
            "10000",
            "1500",
            "15.00",
            "",
            "",
            f"{trigger}HYPERLINK",  # formula trigger in dtaa-article slot
            "",
            "",
            "",
            "",
            "US",
            "1500.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", malicious_detail_row)
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    with pytest.raises(WriterContractError):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)


# ---- Atomic-write contract -----------------------------------------------


def test_temp_file_is_cleaned_up_on_csv_writer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If csv.writerow raises mid-stream, the temp file must be
    removed and the destination must not exist (atomic guarantee).
    """

    def crashing_detail_row(_entry: Form67Entry, **_kwargs: object) -> list[str]:
        raise RuntimeError("simulated writer crash")

    monkeypatch.setattr(csv_writer_mod, "_detail_row", crashing_detail_row)
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    with pytest.raises(RuntimeError, match="simulated writer crash"):
        writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    assert not path.exists()
    # No leftover .tmp files in the parent directory.
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_overwrites_existing_destination_atomically(tmp_path: Path) -> None:
    path = tmp_path / "form67.csv"
    path.write_text("previous content that must be replaced")
    writer = Form67ReviewCsvWriter()
    writer.write(Form67ComputeResult(entries=(_make_entry(),)), path)
    rows = _read(path)
    # Replaced — first row is the writer's header, not the old content.
    assert rows[0][0] == "Sl. No."


# ---- Per-AY portal-labels selection --------------------------------------


def test_default_assessment_year_uses_current_ay_labels(tmp_path: Path) -> None:
    """When no assessment_year_start is passed, the writer sources
    labels from the package-level default (currently AY 2026-27)."""
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=()), path)
    rows = _read(path)
    assert tuple(rows[0][:13]) == PORTAL_COLUMN_HEADERS_FORM67


def test_explicit_assessment_year_2026_resolves_to_2026_27_snapshot(
    tmp_path: Path,
) -> None:
    """Passing 2026 explicitly should match the default behaviour."""
    writer = Form67ReviewCsvWriter(assessment_year_start=2026)
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=()), path)
    rows = _read(path)
    assert tuple(rows[0][:13]) == PORTAL_COLUMN_HEADERS_FORM67


def test_explicit_unregistered_assessment_year_raises(tmp_path: Path) -> None:
    """Unsupported AYs (older than the earliest registered snapshot)
    must raise UnknownAssessmentYearError at construction time so the
    pipeline can surface an actionable error before writing files."""
    with pytest.raises(UnknownAssessmentYearError):
        Form67ReviewCsvWriter(assessment_year_start=2020)


def test_amount_and_rate_cells_are_the_compute_filed_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#898: the detail row writes :attr:`Form67Entry.filed` as it is.

    Compute rounds every filed amount and rate, so the writer neither rounds
    nor calls the per-AY ``format_amount_for_portal`` (which replaced the
    #227 per-AY rounding check for this writer). The header still comes from
    the writer's AY labels.
    """
    from ibkr_fa.output import portal_labels  # noqa: PLC0415

    def refuse(_value: object) -> str:
        raise AssertionError("the Form 67 review CSV rounded an amount")

    monkeypatch.setattr(portal_labels, "format_amount_for_portal", refuse)
    monkeypatch.setattr(
        portal_labels._SNAPSHOTS_BY_AY_START[portal_labels.CURRENT_AY_START],
        "format_amount_for_portal",
        refuse,
    )
    entry = _make_entry(
        income_inr=Decimal("10000.50"),
        foreign_tax_inr=Decimal("1500.49"),
        tax_payable_normal_inr=Decimal("3000.5"),
        tax_payable_special_inr=Decimal("2000.60"),
        section_90_dtaa_article="Article 10(2)(b)",
        section_90_dtaa_rate_percent=Decimal("24.255"),  # filed HALF_UP as 24.26
        section_90_credit_inr=Decimal("1500.49"),
        total_credit_inr=Decimal("1500.49"),
    )
    path = tmp_path / "form67.csv"
    Form67ReviewCsvWriter().write(Form67ComputeResult(entries=(entry,)), path)

    filed = entry.filed
    detail = _read(path)[1]
    assert detail[3:13] == [
        str(filed.income_inr),
        str(filed.foreign_tax_inr),
        str(filed.tax_paid_rate_percent),
        str(filed.tax_payable_normal_inr),
        str(filed.tax_payable_special_inr),
        "Article 10(2)(b)",
        str(filed.section_90_dtaa_rate_percent),
        str(filed.section_90_credit_inr),
        "",
        str(filed.total_credit_inr),
    ]
    assert detail[3:13] == [
        "10001",
        "1500",
        "15.00",
        "3001",
        "2001",
        "Article 10(2)(b)",
        "24.26",
        "1500",
        "",
        "1500",
    ]


# ---- Country format helper integration -----------------------------------


def test_country_cell_uses_stored_cbdt_and_name_fields(
    tmp_path: Path,
) -> None:
    """Col 2 'Name of the country / specified territory' uses the
    entry's stored ``cbdt_country_code`` and ``country_name_portal``
    fields directly (NOT format_country_for_portal re-derivation from
    iso_alpha2). T3 r0 P2 of PR #226 changed this so directly-
    constructed entries whose CBDT code/name fields differ from the
    ISO-lookup-derived defaults render exactly what the user passed,
    not a silent re-derivation.

    Construct an entry with the standard iso/cbdt match (US → 2 →
    UNITED STATES OF AMERICA) and verify the rendered cell is
    ``<cbdt>-<name>``.
    """
    entry = _make_entry(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    assert data[1] == "2-UNITED STATES OF AMERICA"


def test_country_cell_uses_stored_fields_not_iso_lookup(tmp_path: Path) -> None:
    """Defensive test: construct an entry whose stored cbdt_country_code
    differs from what format_country_for_portal would derive from
    iso_alpha2 (this is allowed by the model — no consistency invariant
    enforces alignment). The writer must render the STORED fields, not
    the iso-lookup-derived defaults. Pins T3 r0 P2 of PR #226."""
    entry = _make_entry(
        cbdt_country_code="9999",
        country_name_portal="DRIFTED COUNTRY NAME",
        iso_alpha2="US",
    )
    writer = Form67ReviewCsvWriter()
    path = tmp_path / "form67.csv"
    writer.write(Form67ComputeResult(entries=(entry,)), path)
    _, data = _read(path)
    # Stored fields rendered verbatim; iso-lookup-derived "2-UNITED
    # STATES OF AMERICA" must NOT appear.
    assert data[1] == "9999-DRIFTED COUNTRY NAME"
