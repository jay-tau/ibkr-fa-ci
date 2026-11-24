"""Tests for :mod:`ibkr_fa.output.schedule_tr_review_csv`."""

from __future__ import annotations

import csv
import os
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ibkr_fa.output.schedule_tr_review_csv as csv_writer_mod
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection
from ibkr_fa.output import (
    Artifact,
    Destination,
    OutputBinding,
    OutputKey,
    Subcommand,
    output_binding,
)
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.base import RenderContext
from ibkr_fa.output.portal_labels import (
    PORTAL_COLUMN_HEADERS_TR,
    UnknownAssessmentYearError,
)
from ibkr_fa.output.schedule_tr_review_csv import (
    _AUDIT_HEADERS,
    _COLUMN_COUNT,
    ScheduleTrReviewCsvWriter,
)

# ---- shared fixtures -----------------------------------------------------


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
    taxpayer_id_number: str | None = None,
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
        taxpayer_id_number=taxpayer_id_number,
    )


def _writer(format_id: str) -> Any:
    """The TR ``format_id`` slot's writer for AY 2026-27, built as a run builds it."""
    return output_binding(Subcommand.TR, format_id).writer(RenderContext(2026))


def _read(path: Path) -> list[list[str]]:
    """Read a CSV file emitted by the writer back into rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


# ---- Registry ------------------------------------------------------------


def test_writer_is_registered_under_review_csv_format_id() -> None:
    assert output_binding(Subcommand.TR, "review-csv") == OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.TR_CSV), ScheduleTrReviewCsvWriter
    )


def test_writer_class_attribute_format_id_is_review_csv() -> None:
    assert ScheduleTrReviewCsvWriter.format_id == "review-csv"


def test_filing_csv_contains_only_portal_columns(tmp_path: Path) -> None:
    path = tmp_path / "tr-filing.csv"
    result = ScheduleTrComputeResult(
        rows=[
            _make_row(
                taxpayer_id_number="123456789",
                tax_relief_inr_total=Decimal("750"),
                section_under_which_claimed=TaxReliefSection.SECTION_90,
            )
        ]
    )
    _writer("filing-csv").write(result, path)

    rows = _read(path)
    assert rows[0] == list(PORTAL_COLUMN_HEADERS_TR)
    assert rows[1] == [
        "2-UNITED STATES OF AMERICA",
        "123456789",
        "1851",
        "750",
        "90",
    ]
    assert all(len(row) == 5 for row in rows)
    assert "_audit" not in path.read_text(encoding="utf-8-sig")


# ---- Header + column-count contract --------------------------------------


def test_column_count_is_nine() -> None:
    assert _COLUMN_COUNT == 9


def test_header_is_nine_cells_and_starts_with_portal_columns(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[]), path)

    rows = _read(path)
    assert len(rows) == 1
    assert len(rows[0]) == _COLUMN_COUNT
    assert tuple(rows[0][:5]) == PORTAL_COLUMN_HEADERS_TR
    assert tuple(rows[0][5:]) == _AUDIT_HEADERS


def test_header_only_with_no_rows(tmp_path: Path) -> None:
    """Empty compute result emits ONLY the header row."""
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    rows = _read(path)
    assert len(rows) == 1


# ---- Detail rows ---------------------------------------------------------


def test_single_row_round_trip_leaves_unset_optional_cells_empty(tmp_path: Path) -> None:
    """A row with no TIN, relief or section (all None, as when the
    country has no TIN and its relief is left for manual fill) renders
    those cells EMPTY."""
    row = _make_row(foreign_tax_inr_total_full_precision=Decimal("1851.234"))
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)

    rows = _read(path)
    assert len(rows) == 2

    _header, data = rows
    assert len(data) == _COLUMN_COUNT
    # Portal cells (5):
    assert data[0] == "2"  # CBDT code
    assert data[1] == ""  # TIN — EMPTY (None)
    assert data[2] == "1851"  # foreign tax total, whole rupees
    assert data[3] == ""  # tax_relief — EMPTY (None)
    assert data[4] == ""  # section — EMPTY (None)
    # Audit cells (4):
    assert data[5] == "US"
    assert data[6] == "UNITED STATES OF AMERICA"
    assert data[7] == "1851.234"  # full precision
    assert data[8] == "3"


def test_computed_zero_relief_renders_as_literal_zero_not_empty(tmp_path: Path) -> None:
    """T3 r1 P2 #1 fix flowed through to writer: tax_relief=Decimal("0")
    (computed but worked out to zero) renders as literal '0', NOT
    empty. Distinct from None (not computed). Pins the writer-layer
    half of the "computed zero" vs "not computed" semantic.

    PR #222: the symmetric coupling invariant requires
    section_under_which_claimed to accompany tax_relief_inr_total,
    so the test fixture now supplies a section alongside the zero
    relief amount."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1000"),
        tax_relief_inr_total=Decimal("0"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[3] == "0"  # NOT empty


def test_populated_relief_and_section_render_correctly(tmp_path: Path) -> None:
    """Populated relief and section render as HALF_UP whole rupees +
    StrEnum value verbatim."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1000"),
        tax_relief_inr_total=Decimal("750"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[3] == "750"
    assert data[4] == "90"


def test_unfilled_portal_cells_are_empty_strings_not_zero(tmp_path: Path) -> None:
    """A row with no TIN, relief or section renders those cells as
    EMPTY strings, distinct from the 'computed zero' case. Locks the
    review-artifact contract."""
    row = _make_row()
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[1] == ""  # TIN
    assert data[3] == ""  # tax_relief None = "not computed"
    assert data[4] == ""  # section None = "not computed"


def test_taxpayer_id_number_rendered_when_present(tmp_path: Path) -> None:
    """A TIN copied from the FSI rows (stamped from [account.foreign_tins])
    renders in the TR TIN column."""
    row = _make_row(taxpayer_id_number="123456789")
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    _, data = _read(path)
    assert data[1] == "123456789"  # TIN column


# ---- Sort order (preserved from compute layer) ---------------------------


def test_multi_row_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    """Compute layer sorts by cbdt_country_code lexicographically.
    Writer preserves that order — no re-sort. Locks the lexicographic
    convention ('1268' before '2') against a future numeric-sort
    refactor."""
    row_us = _make_row(
        cbdt_country_code="2",
        iso_alpha2="US",
        country_name_portal="UNITED STATES OF AMERICA",
    )
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
    # Compute-sorted order (lexicographic):
    expected_order = ["111", "1268", "2"]
    result = ScheduleTrComputeResult(rows=[row_gb, row_ag, row_us])

    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(result, path)
    rows = _read(path)
    detail_cbdt = [r[0] for r in rows[1:]]
    assert detail_cbdt == expected_order


# ---- No TOTAL row (TR-specific design choice) ----------------------------


def test_no_summary_or_total_row_appended(tmp_path: Path) -> None:
    """TR has NO TOTAL row — the portal accepts one row per source
    country and computes its own DTAA-vs-non-DTAA sub-totals server-
    side. Locks the contract so a future refactor doesn't add a
    phantom totals row that the e-filing portal would treat as an
    additional country."""
    row1 = _make_row()
    row2 = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    result = ScheduleTrComputeResult(rows=[row2, row1])

    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(result, path)
    rows = _read(path)
    assert len(rows) == 3  # header + 2 detail rows; NO TOTAL row
    # Last row is a normal detail row (US sorted last lexicographically).
    last = rows[-1]
    assert last[0] == "2"


# ---- Encoding + uniform width --------------------------------------------


def test_utf8_sig_bom_present(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    with path.open("rb") as fh:
        head = fh.read(3)
    assert head == b"\xef\xbb\xbf"  # UTF-8 BOM


def test_every_row_has_nine_cells(tmp_path: Path) -> None:
    row_a = _make_row()
    row_b = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    result = ScheduleTrComputeResult(rows=[row_b, row_a])

    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
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

    def nul_detail_row(row: ScheduleTrRow, **_kwargs: object) -> list[str]:
        return [
            "2",
            "",
            "1000",
            "",
            "",
            "US",
            "UNITED STATES\x00OF AMERICA",  # NUL in country name audit cell
            "1000.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)


def test_nul_byte_violation_does_not_leave_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nul_detail_row(row: ScheduleTrRow, **_kwargs: object) -> list[str]:
        return [
            "2",
            "",
            "1000",
            "",
            "",
            "US",
            "UNITED STATES\x00OF AMERICA",
            "1000.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    assert not path.exists()


# ---- Writer-contract safety: formula injection rejection -----------------


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    def malicious_detail_row(row: ScheduleTrRow, **_kwargs: object) -> list[str]:
        return [
            "2",
            "",
            "1000",
            "",
            "",
            "US",
            f"{trigger}HYPERLINK",  # formula trigger in audit slot
            "1000.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)


# ---- Atomic-write contract -----------------------------------------------


def test_atomic_write_no_temp_file_remains_on_success(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    assert path.exists()
    temp_files = [p.name for p in tmp_path.iterdir() if p.name != "tr.csv"]
    assert temp_files == []


def test_mkstemp_failure_surfaces_unmasked(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    with (
        patch("tempfile.mkstemp", side_effect=PermissionError("denied")),
        pytest.raises(PermissionError, match="denied"),
    ):
        writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    assert not path.exists()


def test_overwrites_existing_file(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    path.write_text("stale content", encoding="utf-8")
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    rows = _read(path)
    assert len(rows) == 2
    assert rows[1][0] == "2"


# ---- Per-AY label threading ----------------------------------------------


def test_writer_defaults_to_current_ay_labels(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    rows = _read(path)
    assert tuple(rows[0][:5]) == PORTAL_COLUMN_HEADERS_TR


def test_writer_accepts_explicit_assessment_year_start(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter(assessment_year_start=2026)
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)
    rows = _read(path)
    assert tuple(rows[0][:5]) == PORTAL_COLUMN_HEADERS_TR


def test_itr3_writer_uses_itr3_section_header(tmp_path: Path) -> None:
    writer = ScheduleTrReviewCsvWriter(assessment_year_start=2026, itr_form="ITR-3")
    path = tmp_path / "itr3.csv"
    writer.write(ScheduleTrComputeResult(rows=[]), path)
    assert _read(path)[0][4] == "Section under which relief claimed (specify 90, 90A or 91)"


def test_writer_with_unknown_ay_raises(tmp_path: Path) -> None:
    with pytest.raises(UnknownAssessmentYearError):
        ScheduleTrReviewCsvWriter(assessment_year_start=2000)


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv"])
def test_amount_cells_are_the_compute_whole_rupee_totals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    whole_rupees_without_rounding,
    format_id: str,
) -> None:
    """#900: the detail row writes the TR row's whole-rupee totals as they are.

    Compute sums the FSI rows' filed values, so the writer neither rounds nor
    calls the per-AY ``format_amount_for_portal`` (which replaced the #227
    per-AY rounding check for this writer). The header still comes from the
    writer's AY labels.
    """
    from ibkr_fa.output import portal_labels  # noqa: PLC0415

    def refuse(_value: object) -> str:
        raise AssertionError("a Schedule TR CSV rounded an amount")

    monkeypatch.setattr(portal_labels, "format_amount_for_portal", refuse)
    monkeypatch.setattr(
        portal_labels._SNAPSHOTS_BY_AY_START[portal_labels.CURRENT_AY_START],
        "format_amount_for_portal",
        refuse,
    )
    row = _make_row(
        tax_relief_inr_total=Decimal("500"),
        section_under_which_claimed=TaxReliefSection.SECTION_90,
    )
    # Guard the already validated totals against another rounding pass.
    for field in ("foreign_tax_inr_total", "tax_relief_inr_total"):
        object.__setattr__(row, field, whole_rupees_without_rounding(getattr(row, field)))
    path = tmp_path / "tr.csv"
    _writer(format_id).write(ScheduleTrComputeResult(rows=[row]), path)

    rows = _read(path)
    assert tuple(rows[0][:5]) == PORTAL_COLUMN_HEADERS_TR
    assert rows[1][2:4] == ["1851", "500"]


# ---- Audit cell precision contract --------------------------------------


def test_audit_columns_preserve_full_precision_decimals(tmp_path: Path) -> None:
    """Portal cells render compute's whole-rupee total as it is (#900; the
    model refuses a fractional one); audit cells preserve full precision via
    str(Decimal)."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1"), foreign_tax_inr_total_full_precision=Decimal("1.499")
    )
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[2] == "1"  # the compute total, as it is
    assert data[7] == "1.499"  # full precision


def test_audit_total_uses_full_precision_field_when_populated(tmp_path: Path) -> None:
    """T2/T3 r2 fix on PR #216: when compute populates
    ``foreign_tax_inr_total_full_precision`` separately from the
    portal-displayed ``foreign_tax_inr_total``, the audit cell
    MUST render the full-precision value (not the portal-
    displayed pre-rounded total). Example: two FSI rows of ₹1.50
    each give portal=₹4 / full-precision=₹3.00 — the audit cell
    must say "3.00", NOT "4"."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("4"),  # portal: per-row HALF_UP sum
        foreign_tax_inr_total_full_precision=Decimal("3.00"),  # FSI full-precision sum
    )
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    _, data = _read(path)
    assert data[2] == "4"  # portal-displayed
    assert data[7] == "3.00"  # full-precision audit cell


def test_audit_total_falls_back_to_portal_when_full_precision_none(tmp_path: Path) -> None:
    """Legacy back-compat: a ScheduleTrRow constructed without a
    full-precision value (``foreign_tax_inr_total_full_precision=None``)
    falls back to rendering the portal-displayed total in the
    audit cell. Pre-PR-#216 callers and direct-constructor test
    fixtures rely on this."""
    row = _make_row(
        foreign_tax_inr_total=Decimal("1234"),
        foreign_tax_inr_total_full_precision=None,
    )
    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[row]), path)
    _, data = _read(path)
    assert data[7] == "1234"  # fallback to portal field


def test_atomic_via_temp_then_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy mkstemp + os.replace to confirm the atomic-write path."""
    import tempfile as _tempfile  # noqa: PLC0415  (local for spy)

    real_mkstemp = _tempfile.mkstemp
    real_replace = os.replace

    mkstemp_calls: list[tuple[Any, ...]] = []
    replace_calls: list[tuple[Any, Any]] = []

    def spy_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        mkstemp_calls.append((args, kwargs))
        return real_mkstemp(*args, **kwargs)

    def spy_replace(src: Any, dst: Any) -> None:
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("ibkr_fa.output._atomic.tempfile.mkstemp", spy_mkstemp)
    monkeypatch.setattr("ibkr_fa.output._atomic.os.replace", spy_replace)

    writer = ScheduleTrReviewCsvWriter()
    path = tmp_path / "tr.csv"
    writer.write(ScheduleTrComputeResult(rows=[_make_row()]), path)

    assert len(mkstemp_calls) == 1
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == path
