"""XLSX writer for Schedule FA Table A3 (issue #42).

Sister to :mod:`ibkr_fa.output.review_csv`: same row contract, same
column block layout (11 portal cells + 9 audit cells), but emitted as
a multi-sheet Excel workbook so a user can review the values in
Excel / LibreOffice / Google Sheets BEFORE copy-pasting into the
e-filing portal's Schedule FA Table A3 form.

What this writer adds over the CSV equivalent:

* **Numeric cells stay numeric** — portal INR amounts (Initial
  Value, Peak Value, Closing Balance, Gross Amount Paid /
  Credited, Gross Proceeds) and audit USD amounts are written as
  Excel numbers (not strings), so the user can sum / subtract /
  spot-check them with native spreadsheet formulas. Portal cells
  hold the model's filed whole-rupee values, the same ones the CSV
  writer renders.
* **Dates stay dates** — both the portal-facing date (DD/MM/YYYY)
  and the audit ISO-8601 dates are written as Excel ``datetime``
  cells with the appropriate number format string applied, so
  spreadsheet date arithmetic works.
* **Header row is bold + frozen** — the first row's freeze pane
  keeps the column headers visible while scrolling long lot
  lists; bold formatting visually separates the header from data.

The XLSX writer enforces the SAME writer-safety contract as the CSV
sister:

* :func:`_assert_no_nul_in_xlsx_row` — NUL bytes in any string cell are
  rejected (binary-contamination guard).
* :func:`_assert_no_xlsx_formula_injection` — Excel auto-executes
  formula-prefixed cells on open. The OWASP-extended trigger set
  (``= + - @`` + C0 whitespace + Unicode full-width homoglyphs) is
  rejected on every string cell. Numeric / datetime cells are
  safe by construction — Excel won't interpret a numeric cell's
  value as a formula.
* :func:`_assert_no_illegal_xlsx_character` — last, on the row as
  appended: a string cell holding a control character a workbook
  cell can't store (openpyxl's set) is rejected, naming only the
  row and cell (#1357).

Multi-sheet structure (forward-compatible with FA Tables A1/A2/A4+):

* Sheet 1: ``"Table A3"`` — the FA-A3 portal + audit columns.
* Future PRs add additional sheets (one per FA table) without
  changing this writer's API: instantiate, call :meth:`write`,
  get one workbook with one sheet per implemented table.

Per-AY portal labels (issue #65): mirrors the CSV writer's
constructor signature. ``assessment_year_start`` defaults to the
package-level current AY; passing a different AY routes to the
matching per-AY snapshot via
:func:`ibkr_fa.output.portal_labels.for_ay`.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.models import FaA3Row
from ibkr_fa.output import portal_labels as _default_labels
from ibkr_fa.output import register
from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._xlsx_contract import (
    _assert_no_illegal_xlsx_character,
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _decimal_to_float,
    _whole_rupee_cell,
)
from ibkr_fa.output.base import Artifact, Destination, OutputKey, RenderContext
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.review_csv import _AUDIT_COLUMN_HEADERS

_SHEET_NAME_FA_A3: Final[str] = "Table A3"

# Excel number-format strings.
_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_DECIMAL_USD_FORMAT: Final[str] = "#,##0.00000000"
_DECIMAL_TTBR_FORMAT: Final[str] = "#,##0.0000"
_PORTAL_DATE_EXCEL_FORMAT: Final[str] = "DD/MM/YYYY"
_AUDIT_DATE_EXCEL_FORMAT: Final[str] = "YYYY-MM-DD"


def _portal_row_cells(row: FaA3Row, *, labels: ModuleType) -> list[object]:
    """The 11 portal-facing cells as native Python types for openpyxl.

    Strings stay strings, dates become :class:`datetime.date`, and
    INR amounts are the row's filed whole-rupee values as integers (or
    :data:`None` for unfilled cells).
    """
    filed = row.filed
    return [
        labels.format_country_for_portal(row.source_country_iso2),
        row.name_of_entity,
        row.address_of_entity,
        row.zip_code,
        row.nature_of_entity,
        row.date_of_acquiring,
        _whole_rupee_cell(filed.initial_value_inr, field="Schedule FA A3 initial value"),
        _whole_rupee_cell(filed.peak_value_inr, field="Schedule FA A3 peak value"),
        _whole_rupee_cell(filed.closing_value_inr, field="Schedule FA A3 closing balance"),
        _whole_rupee_cell(
            filed.gross_amount_paid_credited_inr, field="Schedule FA A3 gross amount"
        ),
        _whole_rupee_cell(
            filed.gross_proceeds_from_sale_inr, field="Schedule FA A3 gross proceeds"
        ),
    ]


def _audit_row_cells(row: FaA3Row) -> list[object]:
    """The 9 audit cells as native Python types for openpyxl.

    Mirrors :func:`ibkr_fa.output.review_csv._audit_cells` but
    preserves :class:`Decimal` / :class:`date` types instead of
    stringifying them, so Excel formulas work on the audit values.
    """
    return [
        row.source_isin,
        row.source_ticker,
        row.source_country_iso2,
        row.audit_cost_basis_usd,
        row.audit_acquisition_date,
        row.audit_close_price_usd,
        row.audit_ttbr_acquisition,
        row.audit_ttbr_dec31,
        row.audit_disposal_date,
    ]


# Per-column number-format strings for the audit block, indexed
# starting at portal-column-count to land on the right worksheet
# column. Strings columns (ISIN, ticker, country) take None (default
# Excel cell format).
_AUDIT_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # _audit: Source ISIN
    None,  # _audit: Source ticker
    None,  # _audit: Source country
    _DECIMAL_USD_FORMAT,  # _audit: Cost basis (USD)
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: Acquisition date
    _DECIMAL_USD_FORMAT,  # _audit: Closing price on Dec 31 (USD)
    _DECIMAL_TTBR_FORMAT,  # _audit: TTBR on acquisition date
    _DECIMAL_TTBR_FORMAT,  # _audit: TTBR on Dec 31
    _AUDIT_DATE_EXCEL_FORMAT,  # _audit: Disposal date
)

# Per-column number-format strings for the 11 portal cells.
_PORTAL_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Country Name and Code (string)
    None,  # Name of entity (string)
    None,  # Address of entity (string)
    None,  # ZIP Code (string)
    None,  # Nature of entity (string)
    _PORTAL_DATE_EXCEL_FORMAT,  # Date of acquiring
    _INTEGER_RUPEE_FORMAT,  # Initial value
    _INTEGER_RUPEE_FORMAT,  # Peak value
    _INTEGER_RUPEE_FORMAT,  # Closing balance
    _INTEGER_RUPEE_FORMAT,  # Gross amount paid / credited
    _INTEGER_RUPEE_FORMAT,  # Gross proceeds from sale
)


@register(OutputKey(Destination.WORKBOOK, Artifact.FA_A3_XLSX))
class XlsxWriter:
    """XLSX writer for Schedule FA Table A3 — sister to :class:`ReviewCsvWriter`.

    Registered under :attr:`format_id` ``"xlsx"`` — selected via the
    CLI's ``--format xlsx`` flag.

    The output workbook contains one sheet (``"Table A3"``) with the
    same column layout as the review CSV (11 portal cells + 9 audit
    cells), but values are written as native Excel types so
    spreadsheet formulas (SUM, etc.) work directly on the data.

    Per-AY portal labels are threaded the same way the CSV writer
    threads them (constructor ``assessment_year_start`` kwarg);
    see :class:`~ibkr_fa.output.review_csv.ReviewCsvWriter` for
    details.
    """

    format_id: ClassVar[str] = "xlsx"

    def __init__(self, *, assessment_year_start: int | None = None) -> None:
        """Build a writer pinned to the snapshot for ``assessment_year_start``.

        Args:
            assessment_year_start: 4-digit AY start year. ``None``
                (the default) falls back to the package-level
                current-AY snapshot — preserves pre-#65 behaviour
                for callers that don't yet thread the AY.

        Raises:
            UnknownAssessmentYearError: If ``assessment_year_start``
                is not registered and is more than one year past
                the latest registered snapshot.
        """
        if assessment_year_start is None:
            self._labels: ModuleType = _default_labels
        else:
            self._labels = for_ay(assessment_year_start)

    @classmethod
    def from_context(cls, ctx: RenderContext) -> Self:
        """Build the writer for ``ctx``'s AY."""
        return cls(assessment_year_start=ctx.assessment_year_start)

    def write(self, rows: Iterable[FaA3Row], path: Path) -> None:
        """Serialise ``rows`` to ``path`` as an XLSX workbook.

        Per the :class:`~ibkr_fa.output.base.FaA3Writer` Protocol
        contract:

        * Overwrites ``path`` if it exists.
        * Does NOT create parent directories — fails loud on a
          typo'd path so the user notices before assuming the file
          was written.
        * ``rows`` is materialised exactly once.

        Args:
            rows: The :class:`FaA3Row` records to serialise. Order
                is preserved.
            path: Destination ``.xlsx`` file path. Must reside in a
                directory that already exists.

        Raises:
            WriterContractError: If any string cell contains a NUL
                byte or starts with a CSV-formula-injection trigger
                (Excel auto-executes formula-prefixed cells; the
                same OWASP-extended trigger set as the CSV sister
                writer is enforced).
            OSError: If ``path``'s parent directory doesn't exist
                or isn't writable.
        """
        materialised = list(rows)
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_FA_A3

        header_cells: list[str] = list(self._labels.PORTAL_COLUMN_HEADERS_FA_A3) + list(
            _AUDIT_COLUMN_HEADERS
        )
        _assert_no_nul_in_xlsx_row(header_cells, row_index=0)
        _assert_no_xlsx_formula_injection(header_cells, row_index=0)
        _assert_no_illegal_xlsx_character(header_cells, row_index=0)
        worksheet.append(header_cells)
        # Bold the header row + freeze it so it stays visible while
        # scrolling long lot lists.
        header_font = Font(bold=True)
        for column_index in range(1, len(header_cells) + 1):
            worksheet.cell(row=1, column=column_index).font = header_font
        worksheet.freeze_panes = "A2"

        for row_index, row in enumerate(materialised, start=1):
            portal_cells = _portal_row_cells(row, labels=self._labels)
            audit_cells = _audit_row_cells(row)
            all_cells: list[object] = portal_cells + audit_cells
            _assert_no_nul_in_xlsx_row(all_cells, row_index=row_index)
            _assert_no_xlsx_formula_injection(all_cells, row_index=row_index)
            cells = _decimal_to_float(all_cells, row_index=row_index)
            _assert_no_illegal_xlsx_character(cells, row_index=row_index)
            worksheet.append(cells)
            # Apply per-column number formats. openpyxl rows are
            # 1-indexed; row_index+1 lands on the data row we just
            # appended (row 1 is the header).
            data_row_number = row_index + 1
            for col_offset, fmt in enumerate(_PORTAL_NUMBER_FORMATS + _AUDIT_NUMBER_FORMATS):
                if fmt is None:
                    continue
                worksheet.cell(row=data_row_number, column=col_offset + 1).number_format = fmt

        with atomic_output_path(path) as tmp_path:
            workbook.save(tmp_path)


__all__ = ["XlsxWriter"]
