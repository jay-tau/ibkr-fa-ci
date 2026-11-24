"""The shared portal filing-workbook renderer (#1202, #1205).

A catalog binding's :class:`FilingXlsx` holds a schedule's sheet function.
Building the run's writer passes it the run's context, so a context refusal
comes before any workbook exists. Writing calls the function with the computed
result before the workbook is created, then builds each sheet the function
yields: it checks each row for a NUL byte, then a formula trigger, then a text
cell longer than an Excel cell can hold (32,767 characters, #1323), then a
control character a workbook cell can't store (#1357), appends it, and lays the
sheet out. No cell is cut, wrapped or moved to fit. The workbook
is saved to a staging file and published only after every sheet is built, and
it is closed either way.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from ibkr_fa.output._atomic import atomic_output_path
from ibkr_fa.output._xlsx_contract import (
    _assert_no_illegal_xlsx_character,
    _assert_no_nul_in_xlsx_row,
    _assert_no_xlsx_formula_injection,
    _assert_xlsx_text_fits,
)
from ibkr_fa.output.base import RenderContext

_HEADER_FILL: Final[PatternFill] = PatternFill("solid", fgColor="1F4E78")
_SECTION_FILL: Final[PatternFill] = PatternFill("solid", fgColor="D9EAF7")
_NOTE_FILL: Final[PatternFill] = PatternFill("solid", fgColor="FFF2CC")
_WARNING_FILL: Final[PatternFill] = PatternFill("solid", fgColor="F4CCCC")
_WHITE_BOLD: Final[Font] = Font(bold=True, color="FFFFFF")
_BOLD: Final[Font] = Font(bold=True)
_WRAP: Final[Alignment] = Alignment(wrap_text=True, vertical="top")
_INTEGER_FORMAT: Final[str] = "#,##0"
_RED_NEGATIVE_FORMAT: Final[str] = "0;[RED]-0"


def _check_row(row: Sequence[object], *, row_index: int) -> None:
    """Check ``row`` for a NUL byte, a formula trigger, text too long for a cell
    and a control character a cell can't store, in that order (every filing sheet)."""
    _assert_no_nul_in_xlsx_row(row, row_index=row_index)
    _assert_no_xlsx_formula_injection(row, row_index=row_index)
    _assert_xlsx_text_fits(row, row_index=row_index)
    _assert_no_illegal_xlsx_character(row, row_index=row_index)


def _append_checked(worksheet: Worksheet, row: Sequence[object], *, row_index: int) -> None:
    _check_row(row, row_index=row_index)
    worksheet.append(row)


def _text_cell(value: str) -> str | int | None:
    if value == "":
        return None
    return int(value) if value.lstrip("-").isdecimal() else value


def _print_setup(worksheet: Worksheet, widths: tuple[int, ...]) -> None:
    for index, width in enumerate(widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width
    worksheet.sheet_view.showGridLines = False
    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def _append_note(
    worksheet: Worksheet, note: str, *, row: int, width: int, fill: PatternFill, height: int
) -> None:
    """Append ``note`` as row ``row``, merged across ``width`` columns, shaded and bold."""
    _append_checked(worksheet, [note, *([None] * (width - 1))], row_index=row - 1)
    worksheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    worksheet.cell(row, 1).fill = fill
    worksheet.cell(row, 1).font = _BOLD
    worksheet.cell(row, 1).alignment = _WRAP
    worksheet.row_dimensions[row].height = height


def _lay_out_table(
    worksheet: Worksheet,
    widths: tuple[int, ...],
    *,
    header_row: int,
    width: int,
    number_format: str,
) -> None:
    """Style the header row and the rows below it, freeze and filter the header,
    and print it on every page."""
    for cell in worksheet[header_row]:
        cell.fill = _HEADER_FILL
        cell.font = _WHITE_BOLD
        cell.alignment = _WRAP
    for row in worksheet.iter_rows(min_row=header_row + 1):
        for cell in row:
            cell.alignment = _WRAP
            if isinstance(cell.value, int):
                cell.number_format = number_format
    _print_setup(worksheet, widths)
    worksheet.freeze_panes = f"A{header_row + 1}"
    worksheet.auto_filter.ref = f"A{header_row}:{get_column_letter(width)}{worksheet.max_row}"
    worksheet.print_title_rows = f"{header_row}:{header_row}"


@dataclass(frozen=True)
class FormSheet:
    """A sheet of the same text rows a filing CSV writes, read one at a time.

    Each row is checked as text, then its ``""`` cells become empty cells and
    its whole-number text becomes numbers. The first row is frozen and
    ``merge`` merged. A row is styled by its first cell: a ``heading_prefix``
    heading is shaded, bold and tall, a ``column_headers`` row is a dark header,
    and a ``note_prefix`` note is tall.
    """

    title: str
    rows: Iterable[Sequence[str]]
    widths: tuple[int, ...]
    merge: str
    heading_prefix: str
    column_headers: frozenset[str]
    note_prefix: str

    def build(self, worksheet: Worksheet) -> None:
        for row_index, row in enumerate(self.rows):
            _check_row(row, row_index=row_index)
            worksheet.append([_text_cell(value) for value in row])
        worksheet.merge_cells(self.merge)
        worksheet.freeze_panes = "A2"
        _print_setup(worksheet, self.widths)
        for row in worksheet.iter_rows():
            for cell in row:
                cell.alignment = _WRAP
                if isinstance(cell.value, int):
                    cell.number_format = _INTEGER_FORMAT
        for row_number in range(1, worksheet.max_row + 1):
            first = worksheet.cell(row_number, 1).value
            if isinstance(first, str) and first.startswith(self.heading_prefix):
                for cell in worksheet[row_number]:
                    cell.fill = _SECTION_FILL
                    cell.font = _BOLD
                worksheet.row_dimensions[row_number].height = 45
            elif first in self.column_headers:
                for cell in worksheet[row_number]:
                    cell.fill = _HEADER_FILL
                    cell.font = _WHITE_BOLD
            elif isinstance(first, str) and first.startswith(self.note_prefix):
                worksheet.row_dimensions[row_number].height = 60


@dataclass(frozen=True)
class TableSheet:
    """A note merged across the table, a column header, then the rows.

    The header is frozen, filtered and printed on every page.
    """

    title: str
    note: str
    header: Sequence[str]
    rows: Sequence[Sequence[object]]
    widths: tuple[int, ...]

    def build(self, worksheet: Worksheet) -> None:
        width = len(self.header)
        _append_note(worksheet, self.note, row=1, width=width, fill=_NOTE_FILL, height=30)
        _append_checked(worksheet, self.header, row_index=1)
        for row_index, row in enumerate(self.rows, start=2):
            _append_checked(worksheet, row, row_index=row_index)
        worksheet.row_dimensions[2].height = 45
        _lay_out_table(
            worksheet, self.widths, header_row=2, width=width, number_format=_INTEGER_FORMAT
        )


@dataclass(frozen=True)
class EntrySheet:
    """An instruction, a warning and a footnote, each merged across the table,
    then a blank row, a column header and the rows of fields to enter.

    The header is frozen, filtered and printed on every page, and a whole
    number shows a negative value in red.
    """

    title: str
    instruction: str
    warning: str
    footnote: str
    header: Sequence[str]
    rows: Sequence[Sequence[object]]
    widths: tuple[int, ...]

    def build(self, worksheet: Worksheet) -> None:
        width = len(self.header)
        for row, note, fill, height in (
            (1, self.instruction, _NOTE_FILL, 75),
            (2, self.warning, _WARNING_FILL, 75),
            (3, self.footnote, _NOTE_FILL, 45),
        ):
            _append_note(worksheet, note, row=row, width=width, fill=fill, height=height)
        worksheet.append([])
        _append_checked(worksheet, self.header, row_index=4)
        for row_index, row in enumerate(self.rows, start=5):
            _append_checked(worksheet, row, row_index=row_index)
        _lay_out_table(
            worksheet, self.widths, header_row=5, width=width, number_format=_RED_NEGATIVE_FORMAT
        )


type Sheets = Callable[[Any], Iterable[FormSheet | TableSheet | EntrySheet]]


@dataclass(frozen=True)
class FilingXlsxWriter:
    """A run's writer, built by :meth:`FilingXlsx.from_context`."""

    sheets: Sheets

    def write(self, value: object, path: Path) -> None:
        sheets = self.sheets(value)
        workbook = Workbook()
        try:
            for index, sheet in enumerate(sheets):
                worksheet = workbook.create_sheet() if index else workbook.active
                worksheet.title = sheet.title
                sheet.build(worksheet)
            with atomic_output_path(path) as tmp_path:
                workbook.save(tmp_path)
        finally:
            workbook.close()


@dataclass(frozen=True)
class FilingXlsx:
    """A catalog target; ``sheets`` takes a run's context and returns its sheets."""

    sheets: Callable[[RenderContext], Sheets]

    def from_context(self, ctx: RenderContext) -> FilingXlsxWriter:
        return FilingXlsxWriter(self.sheets(ctx))
