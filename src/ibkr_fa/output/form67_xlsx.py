"""XLSX writer for Form 67 (Statement of income from a country or
specified territory outside India and Foreign Tax Credit).

Sister to :mod:`ibkr_fa.output.form67_review_csv`: same column layout
(17 cells per row: 13 portal + 4 audit), but emitted as an Excel
workbook so the user can review the values in Excel / LibreOffice /
Google Sheets BEFORE entering them into the e-filing portal's Form 67
service (one row per (country, source-of-income) aggregate).

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — INR amounts (income, foreign tax,
  optional tax-payable / DTAA-rate / credit columns when populated) are
  written as Excel numbers (compute's filed whole-rupee and two-place
  values for portal cells, #898; the full-precision value for the
  audit cell), so the user can verify per-(country, source) totals sum
  with ``=SUM(...)`` directly on the spreadsheet.
* **Header row is bold + frozen** — first-row freeze pane keeps column
  headers visible while scrolling.

Section order (mirrors the CSV writer, single-sheet layout):

1. Header row (bold + frozen).
2. One detail row per :class:`~ibkr_fa.compute.form67.Form67Entry`, in
   the compute layer's sort order (``(cbdt_country_code,
   source_of_income.value)`` ascending; 1-indexed
   :attr:`Form67Entry.serial_number` preserved).

Like Schedule FSI / TR, Form 67's e-filing portal service accepts ONE
row per (country, source-of-income) aggregate and computes cross-row
sums server-side; the writer ends after the last detail row (NO
portal-TOTAL row).

Fill status per column matches the CSV writer (see
:mod:`ibkr_fa.output.form67_review_csv`; :mod:`ibkr_fa.compute.form67`
owns the fill rules): each of cols 7-13 (tax-payable normal/special,
DTAA article/rate, §90/§91 credits, total credit) is an EMPTY cell
(``None``) when the entry leaves it ``None``, signalling "not
computed; user must fill". A computed-zero amount renders as numeric
``0`` (NOT empty); see PR #215 T3 r1 P2 #1 for the "computed zero"
vs "not computed" distinction.

Writer-contract safety (shared with the CSV sister):

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string cell
  rejected (binary-contamination guard).
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

Per-AY portal labels (issue #65): same constructor signature as the
CSV writer.

PR (post-#228 ship) step 2.5/N of #37 introduces this writer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.compute.form67 import Form67ComputeResult, Form67Entry
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
from ibkr_fa.output.form67_review_csv import _AUDIT_HEADERS
from ibkr_fa.output.portal_labels import for_ay

_SHEET_NAME_FORM67: Final[str] = "Form 67"

# 13 portal columns + 3 audit columns. Mirrors the CSV writer's
# _COLUMN_COUNT for cross-format parity.
_COLUMN_COUNT: Final[int] = 17

# Excel number-format strings (mirror the sister TR / FSI / OS / CG
# XLSX writers' vocabulary).
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_TWO_DECIMAL_PERCENT_FORMAT: Final[str] = "0.00"
_DECIMAL_INR_FULL_PRECISION_FORMAT: Final[str] = "#,##0.00000000"
_INTEGER_COUNT_FORMAT: Final[str] = "0"

# Per-column number-format strings. ``None`` = default cell format
# (text / no format applied). Index aligns with the 17-cell column
# position in (PORTAL_COLUMN_HEADERS_FORM67, *_AUDIT_HEADERS).
_COLUMN_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    _INTEGER_COUNT_FORMAT,  # (1) Sl. No.
    None,  # (2) Name of the country/specified territory
    None,  # (3) Source of income
    _INTEGER_RUPEE_FORMAT,  # (4) Income from outside India
    _INTEGER_RUPEE_FORMAT,  # (5) Tax paid outside India - Amount
    _TWO_DECIMAL_PERCENT_FORMAT,  # (6) Tax paid outside India - Rate
    _INTEGER_RUPEE_FORMAT,  # (7) Tax payable normal provisions
    _INTEGER_RUPEE_FORMAT,  # (8) Tax payable 115JB/JC
    None,  # (9) §90/90A - DTAA Article No.
    _TWO_DECIMAL_PERCENT_FORMAT,  # (10) §90/90A - DTAA Rate
    _INTEGER_RUPEE_FORMAT,  # (11) §90/90A - Amount
    _INTEGER_RUPEE_FORMAT,  # (12) §91 - Amount
    _INTEGER_RUPEE_FORMAT,  # (13) Total foreign tax credit claimed
    None,  # _audit: ISO alpha-2
    _DECIMAL_INR_FULL_PRECISION_FORMAT,  # _audit: Foreign tax INR (full precision)
    _INTEGER_COUNT_FORMAT,  # _audit: Underlying WHT events aggregated count
    None,  # _audit: Indian tax payable requires manual calculation
)


def _header_row(labels: ModuleType) -> list[object]:
    """Build the 17-cell header row: 13 portal headers + 4 audit headers."""
    return [*labels.PORTAL_COLUMN_HEADERS_FORM67, *_AUDIT_HEADERS]


def _detail_row(entry: Form67Entry) -> list[object]:
    """A single 17-cell detail row with native Excel types.

    Portal cells (13): Sl. No. is an :class:`int`; country dropdown
    (``"<cbdt_country_code>-<country_name_portal>"``) and source-of-
    income enum value stay strings; amounts and rates are the
    :attr:`Form67Entry.filed` values compute rounded (#898), already
    whole rupees and two places, so the stored value matches the CSV
    writers even where Excel's "0.00" format would round a tie
    differently (coerced to ``float`` for Excel number-format
    consumption); optional cols 7-13 render ``None`` as a truly empty
    Excel cell, with the same "computed zero" distinction as the CSV
    writer (Decimal(0) → numeric ``0``; None → empty).

    Audit cells (4): ISO alpha-2 stays a string; foreign-tax INR
    full-precision rides as :class:`Decimal` (coerced to ``float``
    for spreadsheet sum formulas); event_count is an ``int``.
    """
    filed = entry.filed
    return [
        entry.serial_number,
        f"{entry.cbdt_country_code}-{entry.country_name_portal}",
        entry.source_of_income.value,
        filed.income_inr,
        filed.foreign_tax_inr,
        filed.tax_paid_rate_percent,
        filed.tax_payable_normal_inr,
        filed.tax_payable_special_inr,
        entry.section_90_dtaa_article,
        filed.section_90_dtaa_rate_percent,
        filed.section_90_credit_inr,
        filed.section_91_credit_inr,
        filed.total_credit_inr,
        entry.iso_alpha2,
        entry.foreign_tax_inr,
        entry.event_count,
        entry.tax_payable_normal_inr is None,
    ]


def _iter_rows(
    result: Form67ComputeResult,
    *,
    labels: ModuleType,
) -> Iterator[list[object]]:
    """Yield the header row followed by one detail row per result entry."""
    yield _header_row(labels)
    for entry in result.entries:
        yield _detail_row(entry)


@register(OutputKey(Destination.WORKBOOK, Artifact.FORM67_XLSX))
class Form67XlsxWriter:
    """XLSX writer for Form 67 — sister to :class:`Form67ReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag on the ``form-67`` subcommand, whose
    choices come from :func:`~ibkr_fa.output.available_formats`.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, result: Form67ComputeResult, path: Path) -> None:
        """Serialise ``result`` to ``path`` as a single-sheet XLSX file."""
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_FORM67

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


__all__ = ["Form67XlsxWriter"]
