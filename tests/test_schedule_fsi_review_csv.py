"""Tests for :mod:`ibkr_fa.output.schedule_fsi_review_csv`."""

from __future__ import annotations

import csv
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import ibkr_fa.output.schedule_fsi_review_csv as csv_writer_mod
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
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
    PORTAL_COLUMN_HEADERS_FSI,
    UnknownAssessmentYearError,
)
from ibkr_fa.output.schedule_fsi_review_csv import (
    _AUDIT_HEADERS,
    _COLUMN_COUNT,
    ScheduleFsiReviewCsvWriter,
)
from tests._ftc_warnings import MISSING_INDIAN_TAX_WARNING

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_row(
    *,
    cbdt_country_code: str = "2",
    country_name_portal: str = "UNITED STATES OF AMERICA",
    iso_alpha2: str = "US",
    head_of_income: HeadOfIncome = HeadOfIncome.OTHER_SOURCES,
    income_inr: Decimal = Decimal("12345.6789"),
    foreign_tax_inr: Decimal = Decimal("1851.234"),
    event_count: int = 3,
    taxpayer_id_number: str | None = None,
) -> ScheduleFsiRow:
    return ScheduleFsiRow(
        cbdt_country_code=cbdt_country_code,
        country_name_portal=country_name_portal,
        iso_alpha2=iso_alpha2,
        head_of_income=head_of_income,
        income_inr=income_inr,
        foreign_tax_inr=foreign_tax_inr,
        event_count=event_count,
        taxpayer_id_number=taxpayer_id_number,
    )


def _writer(format_id: str) -> Any:
    """The FSI ``format_id`` slot's writer for AY 2026-27, built as a run builds it."""
    return output_binding(Subcommand.FSI, format_id).writer(RenderContext(2026))


def _read(path: Path) -> list[list[str]]:
    """Read a CSV file emitted by the writer back into rows."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_writer_is_registered_under_review_csv_format_id() -> None:
    assert output_binding(Subcommand.FSI, "review-csv") == OutputBinding(
        OutputKey(Destination.REVIEW, Artifact.FSI_CSV), ScheduleFsiReviewCsvWriter
    )


def test_writer_class_attribute_format_id_is_review_csv() -> None:
    assert ScheduleFsiReviewCsvWriter.format_id == "review-csv"


def test_filing_csv_is_vertical_and_drops_audit_columns(tmp_path: Path) -> None:
    """The portal's Schedule FSI is a stacked form, so the file follows it
    field-by-field rather than spreading one entry across eight columns."""
    path = tmp_path / "fsi-filing.csv"
    result = ScheduleFsiComputeResult(rows=[_make_row(taxpayer_id_number="123456789")])
    _writer("filing-csv").write(result, path)

    rows = _read(path)
    assert rows == [
        [PORTAL_COLUMN_HEADERS_FSI[0], "2-UNITED STATES OF AMERICA"],
        [PORTAL_COLUMN_HEADERS_FSI[1], "123456789"],
        ["", ""],
        [PORTAL_COLUMN_HEADERS_FSI[2], "Other Sources"],
        [PORTAL_COLUMN_HEADERS_FSI[3], "12346"],
        [PORTAL_COLUMN_HEADERS_FSI[4], "1851"],
        [PORTAL_COLUMN_HEADERS_FSI[5], ""],
        ["Manual fill guidance", MISSING_INDIAN_TAX_WARNING],
        [PORTAL_COLUMN_HEADERS_FSI[7], ""],
    ]
    assert "_audit" not in path.read_text(encoding="utf-8-sig")
    assert PORTAL_COLUMN_HEADERS_FSI[6] not in path.read_text(encoding="utf-8-sig")


def test_filing_csv_groups_heads_under_one_country(tmp_path: Path) -> None:
    """Country code and TIN appear once; each head gets its own block."""
    path = tmp_path / "fsi-filing.csv"
    result = ScheduleFsiComputeResult(
        rows=[
            # rows must be sorted by (country code, head) per the compute
            # layer's invariant; "Capital Gains" sorts before "Other Sources".
            _make_row(
                taxpayer_id_number="123456789",
                head_of_income=HeadOfIncome.CAPITAL_GAINS,
            ),
            _make_row(taxpayer_id_number="123456789"),
        ]
    )
    _writer("filing-csv").write(result, path)

    rows = _read(path)
    assert [row[0] for row in rows].count(PORTAL_COLUMN_HEADERS_FSI[0]) == 1
    heads = [row[1] for row in rows if row[0] == PORTAL_COLUMN_HEADERS_FSI[2]]
    assert heads == ["Capital Gains", "Other Sources"]
    assert all(len(row) == 2 for row in rows)


@pytest.mark.parametrize("format_id", ["filing-csv", "review-csv", "xlsx"])
def test_every_writer_refuses_relief_that_portal_cannot_reconcile(
    tmp_path: Path, format_id: str
) -> None:
    """#900: each writer reads the compute flag and refuses before writing."""
    path = tmp_path / "fsi-output"
    row = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("1000"),
        foreign_tax_inr=Decimal("400"),
        event_count=1,
        tax_payable_inr=Decimal("300"),
        tax_relief_inr=Decimal("250"),
        dtaa_article="Article 25(2)(a)",
    )

    assert row.filed.portal_overwrites_relief

    with pytest.raises(InvariantError, match="read-only relief"):
        _writer(format_id).write(ScheduleFsiComputeResult(rows=[row]), path)

    assert not path.exists()


def test_filing_csv_accepts_rule128_tax_when_source_rate_is_treaty_rate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fsi-filing.csv"
    row = ScheduleFsiRow(
        cbdt_country_code="2",
        country_name_portal="UNITED STATES OF AMERICA",
        iso_alpha2="US",
        head_of_income=HeadOfIncome.OTHER_SOURCES,
        income_inr=Decimal("2764.3157"),
        foreign_tax_inr=Decimal("641.2283"),
        event_count=1,
        # 2764.3157 x 0.312 = 862.4664984
        tax_payable_inr=Decimal("862.4664984"),
        tax_relief_inr=Decimal("641.2283"),
        dtaa_article="Article 25(2)(a)",
    )

    _writer("filing-csv").write(ScheduleFsiComputeResult(rows=[row]), path)

    rows = _read(path)
    assert [PORTAL_COLUMN_HEADERS_FSI[4], "641"] in rows
    assert [PORTAL_COLUMN_HEADERS_FSI[5], "862"] in rows
    assert not any(row[0] == "Manual fill guidance" for row in rows)


@pytest.mark.parametrize(
    ("foreign_tax", "indian_tax", "portal_foreign_tax", "portal_indian_tax", "needs_guidance"),
    [
        (Decimal("0"), None, "0", "", False),
        (Decimal("0.49"), None, "0", "", True),
        (Decimal("0.50"), None, "1", "", True),
        (Decimal("1851.234"), None, "1851", "", True),
        (Decimal("1851.234"), Decimal("0"), "1851", "0", False),
        (Decimal("1851.234"), Decimal("0.49"), "1851", "0", False),
        (Decimal("1851.234"), Decimal("1000"), "1851", "1000", False),
    ],
)
def test_filing_csv_tax_guidance_uses_unrounded_tax_and_missing_indian_tax(
    tmp_path: Path,
    foreign_tax: Decimal,
    indian_tax: Decimal | None,
    portal_foreign_tax: str,
    portal_indian_tax: str,
    needs_guidance: bool,
) -> None:
    path = tmp_path / "fsi-filing.csv"
    row = replace(_make_row(foreign_tax_inr=foreign_tax), tax_payable_inr=indian_tax)

    _writer("filing-csv").write(ScheduleFsiComputeResult(rows=[row]), path)

    rows = _read(path)
    assert [PORTAL_COLUMN_HEADERS_FSI[4], portal_foreign_tax] in rows
    tax_index = rows.index([PORTAL_COLUMN_HEADERS_FSI[5], portal_indian_tax])
    guidance = ["Manual fill guidance", MISSING_INDIAN_TAX_WARNING]
    assert (guidance in rows) == needs_guidance
    if needs_guidance:
        assert rows[tax_index + 1] == guidance


def test_filing_csv_places_tax_guidance_under_its_own_head(tmp_path: Path) -> None:
    path = tmp_path / "fsi-filing.csv"
    result = ScheduleFsiComputeResult(
        rows=[
            _make_row(head_of_income=HeadOfIncome.CAPITAL_GAINS, foreign_tax_inr=Decimal("0")),
            _make_row(),
        ]
    )

    _writer("filing-csv").write(result, path)

    assert _read(path) == [
        [PORTAL_COLUMN_HEADERS_FSI[0], "2-UNITED STATES OF AMERICA"],
        [PORTAL_COLUMN_HEADERS_FSI[1], ""],
        ["", ""],
        [PORTAL_COLUMN_HEADERS_FSI[2], "Capital Gains"],
        [PORTAL_COLUMN_HEADERS_FSI[3], "12346"],
        [PORTAL_COLUMN_HEADERS_FSI[4], "0"],
        [PORTAL_COLUMN_HEADERS_FSI[5], ""],
        [PORTAL_COLUMN_HEADERS_FSI[7], ""],
        ["", ""],
        [PORTAL_COLUMN_HEADERS_FSI[2], "Other Sources"],
        [PORTAL_COLUMN_HEADERS_FSI[3], "12346"],
        [PORTAL_COLUMN_HEADERS_FSI[4], "1851"],
        [PORTAL_COLUMN_HEADERS_FSI[5], ""],
        ["Manual fill guidance", MISSING_INDIAN_TAX_WARNING],
        [PORTAL_COLUMN_HEADERS_FSI[7], ""],
    ]


@pytest.mark.parametrize("taxed_country", ["111", "2"])
def test_filing_csv_places_tax_guidance_under_its_own_country(
    tmp_path: Path, taxed_country: str
) -> None:
    path = tmp_path / "fsi-filing.csv"
    result = ScheduleFsiComputeResult(
        rows=[
            _make_row(
                cbdt_country_code=code,
                country_name_portal=name,
                iso_alpha2=iso,
                foreign_tax_inr=Decimal("1") if code == taxed_country else Decimal("0"),
            )
            for code, name, iso in [
                ("111", "UNITED KINGDOM", "GB"),
                ("2", "UNITED STATES OF AMERICA", "US"),
            ]
        ]
    )

    _writer("filing-csv").write(result, path)

    country = ""
    guided_countries = []
    for label, value in _read(path):
        if label == PORTAL_COLUMN_HEADERS_FSI[0]:
            country = value.split("-", 1)[0]
        elif label == "Manual fill guidance":
            guided_countries.append(country)
    assert guided_countries == [taxed_country]


def test_filing_csv_uses_numeric_dtaa_article(tmp_path: Path) -> None:
    path = tmp_path / "fsi-filing.csv"
    row = replace(
        _make_row(taxpayer_id_number="123456789"),
        dtaa_article="Article 25(2)(a)",
    )

    _writer("filing-csv").write(ScheduleFsiComputeResult(rows=[row]), path)

    rows = _read(path)
    assert [PORTAL_COLUMN_HEADERS_FSI[7], "25"] in rows


# ---------------------------------------------------------------------------
# Header + column-count contract
# ---------------------------------------------------------------------------


def test_column_count_is_thirteen() -> None:
    assert _COLUMN_COUNT == 13


def test_header_is_thirteen_cells_and_starts_with_portal_columns(
    tmp_path: Path,
) -> None:
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)

    rows = _read(path)
    assert len(rows) == 1
    assert len(rows[0]) == _COLUMN_COUNT
    assert tuple(rows[0][:8]) == PORTAL_COLUMN_HEADERS_FSI
    assert tuple(rows[0][8:]) == _AUDIT_HEADERS


def test_header_only_with_no_rows(tmp_path: Path) -> None:
    """Empty compute result emits ONLY the header row (no padding)."""
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    rows = _read(path)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Detail rows
# ---------------------------------------------------------------------------


def test_single_row_round_trip(tmp_path: Path) -> None:
    row = _make_row()
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)

    rows = _read(path)
    assert len(rows) == 2

    _header, data = rows
    assert len(data) == _COLUMN_COUNT
    # Portal cells (8):
    assert data[0] == "2"  # CBDT code
    assert data[1] == ""  # TIN — _make_row sets none
    assert data[2] == "Other Sources"  # HeadOfIncome.value
    assert data[3] == "12346"  # income, HALF_UP whole rupee
    assert data[4] == "1851"  # tax, HALF_UP whole rupee
    assert data[5] == ""  # Tax payable in India — _make_row sets none
    assert data[6] == ""  # Tax relief in India — _make_row sets none
    assert data[7] == ""  # DTAA article — _make_row sets none
    # Audit cells (5):
    assert data[8] == "US"
    assert data[9] == "UNITED STATES OF AMERICA"
    assert data[10] == "12345.6789"
    assert data[11] == "1851.234"
    assert data[12] == "3"


def test_unfilled_portal_cells_are_empty_strings_not_zero(
    tmp_path: Path,
) -> None:
    """Unset TIN / Tax payable / Tax relief / DTAA fields render as
    EMPTY cells (NOT the string ``"0"``), signalling 'not computed;
    fill by hand' rather than 'computed and equals zero'. The test
    builds the row directly with ``_make_row``, which leaves all four
    ``None``. Locks the review-artifact contract."""
    row = _make_row()
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[1] == ""  # TIN
    assert data[5] == ""  # Tax payable
    assert data[6] == ""  # Tax relief
    assert data[7] == ""  # DTAA


def test_taxpayer_id_number_rendered_when_present(tmp_path: Path) -> None:
    """When the pipeline stamped a foreign TIN onto the row (from
    ``[account.foreign_tins]``), the writer renders it in the TIN column."""
    row = _make_row(taxpayer_id_number="123456789")
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    _, data = _read(path)
    assert data[1] == "123456789"  # TIN column


def test_zero_foreign_tax_row_emits_literal_zero(tmp_path: Path) -> None:
    """A row with ``foreign_tax_inr == 0`` (legitimate for a CG-only
    bucket with no foreign withholding) emits ``"0"`` in the tax cell
    — not an empty cell. The empty cell would conflict with the
    contract that EMPTY means 'not computed', whereas this is a
    computed-zero outcome."""
    row = _make_row(foreign_tax_inr=Decimal("0"))
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[4] == "0"
    assert data[11] == "0"  # audit cell preserves full-precision str


def test_capital_gains_head_renders_portal_label_verbatim(
    tmp_path: Path,
) -> None:
    row = _make_row(head_of_income=HeadOfIncome.CAPITAL_GAINS)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    assert data[2] == "Capital Gains"


# ---------------------------------------------------------------------------
# Sort order (preserved from compute layer; writer does not re-sort)
# ---------------------------------------------------------------------------


def test_multi_row_preserves_compute_layer_sort_order(tmp_path: Path) -> None:
    """Compute layer sorts by (cbdt_country_code, head_of_income.value)
    LEXICOGRAPHICALLY. Writer preserves that order — no re-sort. Pins
    the human-surprising lexicographic ordering (CBDT '1268' before '2')
    so a future refactor doesn't silently switch to numeric sort."""
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
    # Construct in compute-sorted order (lexicographic on CBDT code).
    expected_order = ["111", "1268", "2"]
    result = ScheduleFsiComputeResult(rows=[row_gb, row_ag, row_us])

    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(result, path)
    rows = _read(path)
    detail_cbdt = [r[0] for r in rows[1:]]
    assert detail_cbdt == expected_order


def test_same_country_different_heads_emits_two_rows(tmp_path: Path) -> None:
    """A single country with both OTHER_SOURCES and CAPITAL_GAINS heads
    appears as TWO rows, sorted by head value ('Capital Gains' before
    'Other Sources' alphabetically)."""
    row_cg = _make_row(head_of_income=HeadOfIncome.CAPITAL_GAINS)
    row_os = _make_row(head_of_income=HeadOfIncome.OTHER_SOURCES)
    result = ScheduleFsiComputeResult(rows=[row_cg, row_os])

    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(result, path)
    rows = _read(path)
    assert len(rows) == 3
    assert rows[1][2] == "Capital Gains"
    assert rows[2][2] == "Other Sources"


# ---------------------------------------------------------------------------
# No TOTAL row (FSI-specific design choice)
# ---------------------------------------------------------------------------


def test_no_summary_or_total_row_appended(tmp_path: Path) -> None:
    """FSI has NO TOTAL row — unlike Schedule OS / CG. The last row of
    the file is the last detail row (or, when there are no detail rows,
    the header). Locks the contract so a future refactor doesn't add
    a phantom totals row that the e-filing portal would treat as an
    additional FSI row on copy-paste."""
    row1 = _make_row()
    row2 = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    result = ScheduleFsiComputeResult(rows=[row2, row1])

    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(result, path)
    rows = _read(path)
    assert len(rows) == 3  # header + 2 detail rows; NO TOTAL row
    last = rows[-1]
    assert last[0] == "2"  # last is a normal detail row (US, sorted last)


# ---------------------------------------------------------------------------
# Encoding + uniform width
# ---------------------------------------------------------------------------


def test_utf8_sig_bom_present(tmp_path: Path) -> None:
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    with path.open("rb") as fh:
        head = fh.read(3)
    assert head == b"\xef\xbb\xbf"  # UTF-8 BOM


def test_every_row_has_thirteen_cells(tmp_path: Path) -> None:
    row_a = _make_row()
    row_b = _make_row(
        cbdt_country_code="111",
        iso_alpha2="GB",
        country_name_portal="UNITED KINGDOM",
    )
    result = ScheduleFsiComputeResult(rows=[row_b, row_a])

    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(result, path)
    rows = _read(path)
    assert all(len(r) == _COLUMN_COUNT for r in rows)


# ---------------------------------------------------------------------------
# Writer-contract safety: NUL byte rejection
# ---------------------------------------------------------------------------


def test_nul_byte_in_cell_raises_writer_contract_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's ALL-CAPS / numeric-only invariants block NUL bytes
    from landing in `cbdt_country_code` / `country_name_portal` at
    construction time, so the writer's defence-in-depth contract guard
    is exercised by patching `_detail_row` to emit a NUL-bearing cell.
    Locks the wiring of `_assert_no_nul_in_row`."""

    def nul_detail_row(row, **_kwargs):
        return [
            "2",  # Country Code
            "",  # TIN
            "Other Sources",  # Head of Income
            "100",  # Income
            "10",  # Tax paid
            "",  # Tax payable
            "",  # Tax relief
            "",  # DTAA
            "US",  # _audit: ISO alpha-2
            "UNITED STATES\x00OF AMERICA",  # _audit: Country name (portal)
            "100.00",  # _audit: Income INR
            "10.00",  # _audit: Tax INR
            "1",  # _audit: Event count
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)


def test_nul_byte_violation_does_not_leave_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic-write contract: a writer-contract violation must NOT
    leave the final path on disk (the staging temp file is unlinked)."""

    def nul_detail_row(row, **_kwargs):
        return [
            "2",  # Country Code
            "",  # TIN
            "Other Sources",  # Head of Income
            "100",  # Income
            "10",  # Tax paid
            "",  # Tax payable
            "",  # Tax relief
            "",  # DTAA
            "US",  # _audit: ISO alpha-2
            "UNITED STATES\x00OF AMERICA",  # _audit: Country name (portal)
            "100.00",  # _audit: Income INR
            "10.00",  # _audit: Tax INR
            "1",  # _audit: Event count
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", nul_detail_row)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    assert not path.exists()


# ---------------------------------------------------------------------------
# Writer-contract safety: formula injection rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["=", "+", "-", "@"])
def test_formula_injection_in_cell_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    """The model's ALL-CAPS / numeric-only invariants block formula
    triggers from landing in `cbdt_country_code` / `country_name_portal`
    at construction time, so the writer's defence-in-depth contract
    guard is exercised by patching `_detail_row` to emit a malicious
    cell. Locks the wiring of `_assert_no_csv_formula_injection`."""

    def malicious_detail_row(row, **_kwargs):
        return [
            "2",
            f'{trigger}HYPERLINK("http://evil")',
            "",
            "Other Sources",
            "100",
            "10",
            "",
            "",
            "",
            "US",
            "100.00",
            "10.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)


def test_formula_injection_violation_does_not_leave_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic-write contract: a writer-contract violation must NOT
    leave the final path on disk (the staging temp file is unlinked)."""

    def malicious_detail_row(row, **_kwargs):
        return [
            "2",
            "",
            "Other Sources",
            "100",
            "10",
            "",
            "",
            "",
            "US",
            "=evil",  # formula trigger in audit slot
            "100.00",
            "10.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    assert not path.exists()


# ---------------------------------------------------------------------------
# Atomic-write contract
# ---------------------------------------------------------------------------


def test_atomic_write_no_temp_file_remains_on_success(tmp_path: Path) -> None:
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    assert path.exists()
    temp_files = [p.name for p in tmp_path.iterdir() if p.name != "fsi.csv"]
    assert temp_files == []


def test_mkstemp_failure_surfaces_unmasked(tmp_path: Path) -> None:
    """If mkstemp itself raises (e.g., PermissionError on parent dir),
    the exception MUST surface unmasked — the staging-path guard
    (``tmp_path is not None`` precondition on the cleanup branch)
    means we don't try to unlink a path we never allocated."""
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with (
        patch("tempfile.mkstemp", side_effect=PermissionError("denied")),
        pytest.raises(PermissionError, match="denied"),
    ):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    assert not path.exists()


def test_failure_after_mkstemp_cleans_up_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If mkstemp succeeds but a subsequent step (e.g., writerow)
    raises, the except branch MUST unlink the staging temp file so
    we don't leave litter on disk."""

    def malicious_detail_row(row, **_kwargs):
        return [
            "2",
            "",
            "Other Sources",
            "100",
            "10",
            "",
            "",
            "",
            "US",
            "=evil",  # formula trigger in audit slot
            "100.00",
            "10.00",
            "1",
        ]

    monkeypatch.setattr(csv_writer_mod, "_detail_row", malicious_detail_row)
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    with pytest.raises(WriterContractError):
        writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    leftover = [p.name for p in tmp_path.iterdir()]
    assert leftover == []


def test_overwrites_existing_file(tmp_path: Path) -> None:
    """Writer overwrites an existing path (atomic os.replace semantics)."""
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    path.write_text("stale content", encoding="utf-8")
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    rows = _read(path)
    assert len(rows) == 2
    assert rows[1][0] == "2"


# ---------------------------------------------------------------------------
# Per-AY label threading
# ---------------------------------------------------------------------------


def test_writer_defaults_to_current_ay_labels(tmp_path: Path) -> None:
    """Default constructor uses the package-level current AY snapshot."""
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[]), path)
    rows = _read(path)
    assert tuple(rows[0][:8]) == PORTAL_COLUMN_HEADERS_FSI


def test_writer_accepts_explicit_assessment_year_start(tmp_path: Path) -> None:
    """Constructor `assessment_year_start` routes through `for_ay`."""
    writer = ScheduleFsiReviewCsvWriter(assessment_year_start=2026)
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    rows = _read(path)
    assert tuple(rows[0][:8]) == PORTAL_COLUMN_HEADERS_FSI


def test_writer_with_unknown_ay_raises(tmp_path: Path) -> None:
    """A constructor AY too far away to satisfy the +1-fallback rule
    raises at construction time (NOT at write time)."""

    with pytest.raises(UnknownAssessmentYearError):
        ScheduleFsiReviewCsvWriter(assessment_year_start=2000)


@pytest.mark.parametrize("payable", ["filed", "blank"])
def test_amount_cells_are_the_compute_filed_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, filed_without_rounding, payable: str
) -> None:
    """#900: both CSVs write :attr:`ScheduleFsiRow.filed` as it is.

    Compute rounds every filed amount, so the writers neither round nor call
    the per-AY ``format_amount_for_portal`` (which replaced the #227 per-AY
    rounding check for these writers). The header still comes from the
    writer's AY labels.
    """
    from ibkr_fa.output import portal_labels  # noqa: PLC0415

    def refuse(_value: object) -> str:
        raise AssertionError("a Schedule FSI CSV rounded an amount")

    monkeypatch.setattr(portal_labels, "format_amount_for_portal", refuse)
    monkeypatch.setattr(
        portal_labels._SNAPSHOTS_BY_AY_START[portal_labels.CURRENT_AY_START],
        "format_amount_for_portal",
        refuse,
    )
    row = replace(
        _make_row(),
        tax_payable_inr=None if payable == "blank" else Decimal("1999.6"),
        tax_relief_inr=Decimal("1851.234"),
        dtaa_article="Article 25(2)(a)",
    )
    filed_without_rounding(
        row,
        income_inr=Decimal("12001"),
        foreign_tax_inr=Decimal("1801"),
        tax_payable_inr=None if payable == "blank" else Decimal("1901"),
        tax_relief_inr=Decimal("1801"),
    )
    result = ScheduleFsiComputeResult(rows=[row])
    review_path = tmp_path / "fsi.csv"
    filing_path = tmp_path / "fsi-filing.csv"

    ScheduleFsiReviewCsvWriter().write(result, review_path)
    _writer("filing-csv").write(result, filing_path)

    payable_cell = "" if payable == "blank" else "1901"
    review = _read(review_path)
    assert tuple(review[0][:8]) == PORTAL_COLUMN_HEADERS_FSI
    assert review[1][3:7] == ["12001", "1801", payable_cell, "1801"]
    assert review[1][10:12] == ["12345.6789", "1851.234"]  # audit: raw amounts
    filing = dict(row for row in _read(filing_path) if len(row) == 2 and row[0])
    assert [filing[PORTAL_COLUMN_HEADERS_FSI[index]] for index in (3, 4, 5)] == [
        "12001",
        "1801",
        payable_cell,
    ]


# ---------------------------------------------------------------------------
# Output-format-registry parity
# ---------------------------------------------------------------------------


def test_writer_overwrites_via_os_replace(tmp_path: Path) -> None:
    """os.replace semantics: even if a file at `path` is held open by
    another process when we call write, the staging-tmp mkstemp +
    os.replace pattern overwrites atomically (the existing file's
    inode is replaced)."""
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    path.write_bytes(b"old")
    inode_before = path.stat().st_ino
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)
    inode_after = path.stat().st_ino
    assert inode_before != inode_after  # atomic replace swapped inodes


def test_audit_columns_preserve_full_precision_decimals(tmp_path: Path) -> None:
    """Portal cells round to whole rupees (HALF_UP); audit cells
    preserve the full Decimal precision via str(Decimal). Pins the
    reconciliation contract: a user who SUMs the audit column in
    Excel gets a higher-precision figure than the column-of-rounded-
    rupees would yield."""
    row = _make_row(
        income_inr=Decimal("1.499"),  # rounds DOWN to 1
        foreign_tax_inr=Decimal("1.500"),  # rounds UP to 2 (HALF_UP)
    )
    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[row]), path)
    rows = _read(path)
    _, data = rows
    # Portal cells: HALF_UP rounding (8-column portal block)
    assert data[3] == "1"  # income
    assert data[4] == "2"  # tax
    # Audit cells: verbatim Decimal stringification
    assert data[10] == "1.499"
    assert data[11] == "1.500"


def test_atomic_via_temp_then_replace(tmp_path: Path, monkeypatch) -> None:
    """The write path goes through mkstemp() in tmp_path then
    os.replace() to commit. Verify by spying on both calls."""
    real_mkstemp = __import__("tempfile").mkstemp
    real_replace = os.replace

    mkstemp_calls = []
    replace_calls = []

    def spy_mkstemp(*args, **kwargs):
        mkstemp_calls.append((args, kwargs))
        return real_mkstemp(*args, **kwargs)

    def spy_replace(src, dst):
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr("ibkr_fa.output._atomic.tempfile.mkstemp", spy_mkstemp)
    monkeypatch.setattr("ibkr_fa.output._atomic.os.replace", spy_replace)

    writer = ScheduleFsiReviewCsvWriter()
    path = tmp_path / "fsi.csv"
    writer.write(ScheduleFsiComputeResult(rows=[_make_row()]), path)

    assert len(mkstemp_calls) == 1
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == path
