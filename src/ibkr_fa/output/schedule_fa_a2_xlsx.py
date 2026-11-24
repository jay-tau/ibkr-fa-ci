"""XLSX writer for Schedule FA Table A2 (the IBKR custodial account, #18).

Sister to :mod:`ibkr_fa.output.xlsx` (Table A3): same writer-safety contract
and numeric/date typing, emitted as a SEPARATE workbook (e.g.
``schedule-fa-a2.xlsx``) with a single ``"Table A2"`` sheet, so the A3 output
is untouched. One row — the IBKR account itself.

Numeric cells stay numeric (peak / closing / gross amount as whole rupees;
audit NAV + TTBR as decimals) and the opening date stays an Excel date, so a
reviewer can spot-check with native spreadsheet formulas. Peak/closing may be
negative (a margined account's NAV) — they are NOT clamped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import ClassVar, Final, Self

from openpyxl import Workbook
from openpyxl.styles import Font

from ibkr_fa.models.fa_a2_row import FaA2Row
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
from ibkr_fa.output.schedule_fa_a2_review_csv import _AUDIT_HEADERS, _format_components

_SHEET_NAME_FA_A2: Final[str] = "Table A2"

_INTEGER_RUPEE_FORMAT: Final[str] = "#,##0"
_DECIMAL_USD_FORMAT: Final[str] = "#,##0.00000000"
_DECIMAL_TTBR_FORMAT: Final[str] = "#,##0.0000"
_PORTAL_DATE_EXCEL_FORMAT: Final[str] = "DD/MM/YYYY"

_PORTAL_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Country Name and Code
    None,  # Name of institution
    None,  # Address
    None,  # ZIP
    None,  # Account Number
    None,  # Status
    _PORTAL_DATE_EXCEL_FORMAT,  # Account opening date
    _INTEGER_RUPEE_FORMAT,  # Peak Balance
    _INTEGER_RUPEE_FORMAT,  # Closing balance
    None,  # Nature of amount
    _INTEGER_RUPEE_FORMAT,  # Gross amount
)
_AUDIT_NUMBER_FORMATS: Final[tuple[str | None, ...]] = (
    None,  # Base currency
    _DECIMAL_USD_FORMAT,  # Closing balance (base ccy)
    _DECIMAL_TTBR_FORMAT,  # TTBR on Dec 31
    None,  # Account number sourced from config
    None,  # NAV cross-checked
    None,  # Gross credited components
    _DECIMAL_USD_FORMAT,  # Peak lower bound INR
    _PORTAL_DATE_EXCEL_FORMAT,  # Peak lower bound date
    _DECIMAL_USD_FORMAT,  # Peak upper bound INR
    _PORTAL_DATE_EXCEL_FORMAT,  # Peak upper bound date
    None,  # Selected peak bound
    _DECIMAL_USD_FORMAT,  # Selected peak balance (base ccy)
    _DECIMAL_TTBR_FORMAT,  # TTBR on selected peak date
    _DECIMAL_USD_FORMAT,  # Statement account-value floor
    _PORTAL_DATE_EXCEL_FORMAT,  # Statement account-value floor date
    None,  # Peak override source
    None,  # Peak override reason
)


def _portal_row_cells(row: FaA2Row) -> list[object]:
    filed = row.filed
    return [
        f"{row.country_code}-{row.country_name}",
        row.institution_name,
        row.institution_address,
        row.institution_zip,
        row.account_number,
        row.status.value,
        row.account_opening_date,
        _whole_rupee_cell(filed.peak_balance_inr, field="Schedule FA A2 peak balance"),
        _whole_rupee_cell(filed.closing_balance_inr, field="Schedule FA A2 closing balance"),
        "" if row.nature_of_amount is None else row.nature_of_amount.value,
        _whole_rupee_cell(filed.gross_amount_inr, field="Schedule FA A2 gross amount"),
    ]


def _audit_row_cells(row: FaA2Row) -> list[object]:
    components: Mapping[str, Decimal] = row.audit_gross_components
    return [
        row.base_currency,
        row.audit_closing_balance_base_ccy,
        row.audit_ttbr_dec31,
        "yes" if row.audit_account_number_from_config else "no",
        "yes" if row.audit_nav_cross_checked else "no",
        _format_components(components),
        row.audit_peak_balance_lower_inr,
        row.audit_peak_balance_lower_date,
        row.audit_peak_balance_upper_inr,
        row.audit_peak_balance_upper_date,
        row.audit_peak_balance_bound or "",
        row.audit_peak_balance_base_ccy,
        row.audit_peak_ttbr,
        row.audit_statement_peak_floor_base_ccy,
        row.audit_statement_peak_floor_date,
        row.audit_peak_override_source or "",
        row.audit_peak_override_reason or "",
    ]


@register(OutputKey(Destination.WORKBOOK, Artifact.FA_A2_XLSX))
class ScheduleFaA2XlsxWriter:
    """XLSX writer for Schedule FA Table A2 — sister to the CSV writer."""

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

    def write(self, rows: Iterable[FaA2Row], path: Path) -> None:
        """Serialise ``rows`` to ``path`` as a single-sheet XLSX workbook."""
        materialised = list(rows)
        workbook = Workbook()
        worksheet = workbook.active
        if worksheet is None:  # pragma: no cover — openpyxl always creates Sheet
            raise RuntimeError("openpyxl Workbook() did not create a default worksheet")
        worksheet.title = _SHEET_NAME_FA_A2

        header_cells: list[str] = list(self._labels.PORTAL_COLUMN_HEADERS_FA_A2) + list(
            _AUDIT_HEADERS
        )
        _assert_no_nul_in_xlsx_row(header_cells, row_index=0)
        _assert_no_xlsx_formula_injection(header_cells, row_index=0)
        _assert_no_illegal_xlsx_character(header_cells, row_index=0)
        worksheet.append(header_cells)
        header_font = Font(bold=True)
        for column_index in range(1, len(header_cells) + 1):
            worksheet.cell(row=1, column=column_index).font = header_font
        worksheet.freeze_panes = "A2"

        for row_index, row in enumerate(materialised, start=1):
            all_cells: list[object] = _portal_row_cells(row) + _audit_row_cells(row)
            _assert_no_nul_in_xlsx_row(all_cells, row_index=row_index)
            _assert_no_xlsx_formula_injection(all_cells, row_index=row_index)
            cells = _decimal_to_float(all_cells, row_index=row_index)
            _assert_no_illegal_xlsx_character(cells, row_index=row_index)
            worksheet.append(cells)
            data_row_number = row_index + 1
            for col_offset, fmt in enumerate(_PORTAL_NUMBER_FORMATS + _AUDIT_NUMBER_FORMATS):
                if fmt is None:
                    continue
                worksheet.cell(row=data_row_number, column=col_offset + 1).number_format = fmt

        with atomic_output_path(path) as tmp_path:
            workbook.save(tmp_path)
