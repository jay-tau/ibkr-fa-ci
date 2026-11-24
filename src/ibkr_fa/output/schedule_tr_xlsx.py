"""XLSX writer for Schedule TR (Summary of Tax Relief Claimed).

Sister to :mod:`ibkr_fa.output.schedule_tr_review_csv`: same column
layout (9 cells per row: 5 portal + 4 audit), but emitted as an Excel
workbook so the user can review the values in Excel / LibreOffice /
Google Sheets BEFORE entering them into the e-filing portal's
Schedule TR form (one row per source country).

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — INR amounts (total tax paid + tax
  relief when populated) are written as Excel numbers (compute's
  whole-rupee totals for portal cells, #900; the full-precision value
  for the audit cell), so
  the user can verify TOTALs sum with ``=SUM(...)`` directly on the
  spreadsheet.
* **Header row is bold + frozen** — first-row freeze pane keeps
  column headers visible while scrolling.

Section order (mirrors the CSV writer, single-sheet layout):

1. Header row (bold + frozen).
2. One detail row per :class:`~ibkr_fa.models.ScheduleTrRow`, in
   the compute layer's sort order (``cbdt_country_code`` ascending;
   lexicographic).

Like Schedule FSI, the e-filing portal's Schedule TR accepts ONE
row per source country and computes its own DTAA-vs-non-DTAA
sub-totals server-side; the writer ends after the last detail row
(NO portal-TOTAL row).

Fill status per column matches the CSV writer: TIN, Tax relief
available and Section under which relief claimed are EMPTY cells
(``None``) when the compute row leaves them ``None``, signalling
"not computed; user must fill". A computed-zero relief renders as
numeric ``0`` (NOT empty); see PR #215 T3 r1 P2 #1 for the
"computed zero" vs "not computed" distinction.

Writer-contract safety (shared with the CSV sister):

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string
  cell rejected (binary-contamination guard).
* :func:`_assert_no_xlsx_formula_injection` — Excel auto-executes
  formula-prefixed cells on open. The OWASP-extended trigger set
  used by the CSV writer rejects ``= + - @`` + C0 whitespace +
  Unicode full-width homoglyphs on every string cell.
* :func:`_assert_no_illegal_xlsx_character` — last, on the row as
  appended: a string cell holding a control character a workbook
  cell can't store (openpyxl's set) is rejected, naming only the
  row and cell (#1357).

Atomicity: saved through :func:`~ibkr_fa.output._atomic.atomic_output_path`,
so a failed save leaves ``path`` untouched.

Per-AY portal labels (issue #65): same constructor signature as
the CSV writer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_fsi import require_fsi_relief_matches_portal
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow
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
from ibkr_fa.output.schedule_tr_review_csv import _AUDIT_HEADERS

_SHEET_NAME_TR: Final[str] = "Schedule TR"

# 5 portal columns + 4 audit columns. Mirrors the CSV writer's
# _COLUMN_COUNT for cross-format parity.
_COLUMN_COUNT: Final[int] = 9

# Excel number-format strings (mirror the sister FA-A3 / OS / CG /
# FSI XLSX writers' vocabulary).
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_DECIMAL_INR_FULL_PRECISION_FORMAT: Final[str] = "#,##0.00000000"
_INTEGER_COUNT_FORMAT: Final[str] = "0"

# Per-column number-format strings. ``None`` = default cell format
# (text / no format applied). Index aligns with the 9-cell column
# position in (PORTAL_COLUMN_HEADERS_TR, *_AUDIT_HEADERS).
_COLUMN_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Country Code
    None,  # Taxpayer Identification Number
    _INTEGER_RUPEE_FORMAT,  # Total tax paid outside India
    _INTEGER_RUPEE_FORMAT,  # Total tax relief available in India
    None,  # Section under which relief claimed
    None,  # _audit: ISO alpha-2
    None,  # _audit: Country name (portal)
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Total tax paid INR (full precision)
    _INTEGER_COUNT_FORMAT,  # _audit: FSI rows aggregated count
)


def _header_row(headers: tuple[str, ...]) -> list[object]:
    """Build the 9-cell header row: 5 portal headers + 4 audit headers."""
    return [*headers, *_AUDIT_HEADERS]


def _detail_row(row: ScheduleTrRow) -> list[object]:
    """A single 9-cell detail row with native Excel types.

    Portal cells (5): Country Code stays a string; TIN is the
    per-country TIN string carried from Schedule FSI, or None
    (truly empty Excel cell) when the country has none; Total tax
    paid is the row's whole-rupee total as it is (#900); Total tax
    relief is the whole-rupee total when computed (``0`` for a
    computed zero) or None when left for manual fill (the
    ``cell.value is None`` skip in the format loop leaves it as
    Excel's default "General" format); Section under which
    relief claimed is its StrEnum value when populated or None.

    Audit cells (4): ISO alpha-2 + Country name stay strings;
    Total tax paid full-precision rides as :class:`Decimal`
    (coerced to ``float`` for spreadsheet sum formulas); FSI rows
    aggregated count is an ``int``.
    """
    section_cell: str | None
    if row.section_under_which_claimed is None:
        section_cell = None
    else:
        section_cell = str(row.section_under_which_claimed)
    full_precision_total = (
        row.foreign_tax_inr_total_full_precision
        if row.foreign_tax_inr_total_full_precision is not None
        else row.foreign_tax_inr_total
    )
    return [
        row.cbdt_country_code,
        row.taxpayer_id_number,  # Taxpayer Identification Number (foreign TIN); None → empty
        row.foreign_tax_inr_total,
        row.tax_relief_inr_total,  # None → truly empty for "not computed"
        section_cell,
        row.iso_alpha2,
        row.country_name_portal,
        full_precision_total,
        row.event_count,
    ]


def _iter_rows(
    result: ScheduleTrComputeResult,
    *,
    portal_headers: tuple[str, ...],
) -> Iterator[list[object]]:
    """Yield the header row followed by one detail row per result row."""
    yield _header_row(portal_headers)
    for row in result.rows:
        yield _detail_row(row)


@register(OutputKey(Destination.WORKBOOK, Artifact.TR_XLSX))
class ScheduleTrXlsxWriter:
    """XLSX writer for Schedule TR — sister to :class:`ScheduleTrReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``schedule-tr`` subcommand.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(
        self, *, assessment_year_start: int | None = None, itr_form: ItrForm = "ITR-2"
    ) -> None:
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)
        self._portal_headers = self._labels.PORTAL_COLUMN_HEADERS_TR_BY_ITR_FORM[itr_form]

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY and ITR form (ITR-2 headers when ``None``)."""
        if ctx.itr_form is None:
            return cls(assessment_year_start=ctx.assessment_year_start)
        return cls(assessment_year_start=ctx.assessment_year_start, itr_form=ctx.itr_form)

    def write(self, result: ScheduleTrComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file."""
        require_fsi_relief_matches_portal(result.fsi_rows_with_overwritten_relief)
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_TR

        header_font = Font(bold=True)

        for row_index, row in enumerate(_iter_rows(result, portal_headers=self._portal_headers)):
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


__all__ = ["ScheduleTrXlsxWriter"]
