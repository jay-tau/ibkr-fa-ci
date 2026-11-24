"""XLSX writer for Schedule FSI (Foreign Source Income).

Sister to :mod:`ibkr_fa.output.schedule_fsi_review_csv`: same column
layout (13 cells per row, mixing portal cells with full-precision
audit cells), but emitted as an Excel workbook so the user can
review the values in Excel / LibreOffice / Google Sheets BEFORE
entering them into the e-filing portal's Schedule FSI form (one
row per (country, head)).

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — INR amounts (income + tax) and
  the audit event count are written as Excel numbers (not strings),
  so the user can verify the audit TOTALs sum to the portal TOTALs
  with ``=SUM(...)`` directly on the spreadsheet. The portal-facing
  cells are compute's filed whole-rupee values (#900), as in the CSV
  writer.
* **Header row is bold + frozen** — the first row's freeze pane
  keeps the column headers visible while scrolling long FSI lists;
  bold formatting visually separates the header from data.

Section order (mirrors the CSV writer, single-sheet layout):

1. Header row (bold + frozen).
2. One detail row per :class:`~ibkr_fa.models.ScheduleFsiRow`, in
   the compute layer's sort order (``(cbdt_country_code,
   head_of_income.value)`` ascending; lexicographic on the CBDT code).

Unlike Schedule OS / CG, FSI has **no portal-TOTAL row** — the
e-filing portal accepts one row per (country, head) and computes
its own totals server-side. The XLSX file therefore ends after the
last detail row; users who want a running grand-total can drop
``=SUM(...)`` over the ``_audit: Income INR (full precision)``
column themselves.

Fill status per column matches the CSV writer (rules in
:mod:`ibkr_fa.compute.schedule_fsi_relief`). The TIN is stamped from
``[account.foreign_tins]``. A row with no foreign tax gets a computed
relief of ``0``. An ``Other Sources`` row with a Form 67 dividend
credit gets that credit as relief, the Form 67 entry's column 7 (plus
the resolved Indian tax on any other income it combines) as tax
payable, and the treaty's relief article. Any other TIN or FTC cell
is EMPTY (NOT zero), meaning "not computed; fill by hand before
pasting into the portal", except a blank DTAA article beside a §91
credit, which is correct because §91 has no treaty article. A
computed Form 67 credit that can't be reconciled with its FSI row
refuses the run before writing instead of leaving the row blank.

Writer-contract safety (shared with the CSV sister):

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string
  cell are rejected (binary-contamination guard). Preserves the
  ORIGINAL worksheet column index in :class:`WriterContractError`
  so the user lands on the right cell.
* :func:`_assert_no_xlsx_formula_injection` — Excel auto-executes
  formula-prefixed cells on open. The same OWASP-extended trigger
  set used by the CSV writer (``= + - @`` + C0 whitespace +
  Unicode full-width homoglyphs) is rejected on every string
  cell. Numeric / datetime cells are safe by construction.
* :func:`_assert_no_illegal_xlsx_character` — last, on the row as
  appended: a string cell holding a control character a workbook
  cell can't store (openpyxl's set) is rejected, naming only the
  row and cell (#1357).

Atomicity: saved through :func:`~ibkr_fa.output._atomic.atomic_output_path`,
so a failed save leaves ``path`` untouched.

Per-AY portal labels (issue #65): mirrors the CSV writer's
constructor signature. ``assessment_year_start`` defaults to the
package-level current AY; passing a different AY routes to the
matching per-AY snapshot via
:func:`ibkr_fa.output.portal_labels.for_ay`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.compute.schedule_fsi import (
    ScheduleFsiComputeResult,
    require_fsi_relief_matches_portal,
)
from ibkr_fa.models.schedule_fsi_row import ScheduleFsiRow
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._xlsx_contract import (
    _assert_no_illegal_xlsx_character,
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _decimal_to_float,
)
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.schedule_fsi_review_csv import _AUDIT_HEADERS

_SHEET_NAME_FSI: Final[str] = "Schedule FSI"

# 8 portal columns + 5 audit columns — mirrors the CSV writer's
# _COLUMN_COUNT for cross-format parity. Note: Schedule FSI has NO
# separate "Country Name" portal column; the vendored country_name_portal
# is preserved in an audit column.
_COLUMN_COUNT: Final[int] = 13

# Excel number-format strings. Mirror the FA-A3 / OS / CG XLSX
# writers' vocabulary so a user opening multiple files sees
# consistent formatting.
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_DECIMAL_INR_FULL_PRECISION_FORMAT: Final[str] = "#,##0.00000000"
_INTEGER_COUNT_FORMAT: Final[str] = "0"

# Per-column number-format strings. ``None`` = default cell format
# (text / no format applied). Index aligns with the 13-cell column
# position in (PORTAL_COLUMN_HEADERS_FSI, *_AUDIT_HEADERS).
_COLUMN_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Country Code
    None,  # Taxpayer Identification Number
    None,  # Head of Income
    _INTEGER_RUPEE_FORMAT,  # Income from outside India
    _INTEGER_RUPEE_FORMAT,  # Tax paid outside India
    _INTEGER_RUPEE_FORMAT,  # Tax payable in India
    _INTEGER_RUPEE_FORMAT,  # Tax relief available in India
    None,  # Relevant article of DTAA
    None,  # _audit: ISO alpha-2
    None,  # _audit: Country name (portal)
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Income INR (full precision)
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Tax INR (full precision)
    _INTEGER_COUNT_FORMAT,  # _audit: Event count
)


def _header_row(labels: ModuleType) -> list[object]:
    """Build the 13-cell header row: 8 portal headers + 5 audit headers."""
    return [*labels.PORTAL_COLUMN_HEADERS_FSI, *_AUDIT_HEADERS]


def _detail_row(row: ScheduleFsiRow) -> list[object]:
    """A single 13-cell detail row with native Excel types.

    Portal cells (8): Country Code / Head of Income stay strings;
    Income, Tax, Tax payable and Tax relief are the
    :attr:`ScheduleFsiRow.filed` whole-rupee values compute rounded
    (#900). TIN, Tax payable, Tax relief, and DTAA article are
    :data:`None` (truly empty Excel cells — not zero strings) where
    the row leaves them unset; the module docstring says what an empty
    cell means.

    Audit cells (5): ISO alpha-2 + Country name (portal) stay strings;
    Income + Tax full-precision amounts ride as :class:`Decimal`
    (coerced to ``float`` for spreadsheet sum formulas); event count
    is an ``int``.
    """
    filed = row.filed
    return [
        row.cbdt_country_code,
        row.taxpayer_id_number,  # Taxpayer Identification Number (foreign TIN); None → empty
        row.head_of_income.value,
        filed.income_inr,
        filed.foreign_tax_inr,
        filed.tax_payable_inr,  # Tax payable in India (#36/#37); None → empty
        filed.tax_relief_inr,  # Tax relief available in India (#36/#37); None → empty
        row.dtaa_article,  # Relevant article of DTAA (#36/#37); None → empty cell
        row.iso_alpha2,
        row.country_name_portal,
        row.income_inr,
        row.foreign_tax_inr,
        row.event_count,
    ]


def _iter_rows(
    result: ScheduleFsiComputeResult,
    *,
    labels: ModuleType,
) -> Iterator[list[object]]:
    """Yield the header row followed by one detail row per result row.

    The compute layer guarantees ``result.rows`` is already sorted
    by ``(cbdt_country_code, head_of_income.value)``; the writer
    preserves that order (no re-sort).
    """
    yield _header_row(labels)
    for row in result.rows:
        yield _detail_row(row)


@register(OutputKey(Destination.WORKBOOK, Artifact.FSI_XLSX))
class ScheduleFsiXlsxWriter:
    """XLSX writer for Schedule FSI — sister to :class:`ScheduleFsiReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``schedule-fsi`` subcommand.

    The output workbook contains one sheet (``"Schedule FSI"``) with
    the same column layout as the review CSV (13 cells per row), but
    values are written as native Excel types so spreadsheet formulas
    (SUM, etc.) work directly on the data.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them (constructor ``assessment_year_start`` kwarg); see
    :class:`~ibkr_fa.output.schedule_fsi_review_csv.ScheduleFsiReviewCsvWriter`
    for details.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        See :class:`ibkr_fa.output.schedule_fsi_review_csv.ScheduleFsiReviewCsvWriter.__init__`
        for the contract.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, result: ScheduleFsiComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file."""
        require_fsi_relief_matches_portal(result.rows)
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_FSI

        header_font = Font(bold=True)

        for row_index, row in enumerate(_iter_rows(result, labels=self._labels)):
            _assert_no_nul_in_xlsx_row(row, row_index=row_index)
            _assert_no_xlsx_formula_injection(row, row_index=row_index)
            cells = _decimal_to_float(row, row_index=row_index)
            _assert_no_illegal_xlsx_character(cells, row_index=row_index)
            worksheet.append(cells)
            data_row_number = row_index + 1  # 1-indexed
            if row_index == 0:
                for column_index in range(1, len(row) + 1):
                    worksheet.cell(row=data_row_number, column=column_index).font = header_font
                continue
            for col_offset, fmt in enumerate(_COLUMN_NUMBER_FORMATS):
                if fmt is None:
                    continue
                cell = worksheet.cell(row=data_row_number, column=col_offset + 1)
                if cell.value is None:
                    continue
                cell.number_format = fmt

        worksheet.freeze_panes = "A2"

        with atomic_output_path(path) as tmp_path:
            workbook.save(tmp_path)


__all__ = ["ScheduleFsiXlsxWriter"]
