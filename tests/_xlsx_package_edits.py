"""Edits to a saved XLSX workbook, shared by the StockPlan no-echo and refusal chain tests.

Two edits reopen the workbook with openpyxl and save it: one sets cells, the
other stores a date-styled number too large for a date. The other four work on
the worksheet XML inside the ZIP package: one reads each worksheet, one
rewrites each worksheet with an edit, one builds an edit that replaces a text
found at most once per sheet, and one uses both to store a row again after the
sheet's last row, failing when no worksheet changed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from zipfile import ZipFile

import openpyxl


def date_styled_overflow(path: Path, sheet: str, *coordinates: str) -> Path:
    """Store a date-styled number too large for a date at each of ``coordinates``."""
    workbook = openpyxl.load_workbook(path)
    for coordinate in coordinates:
        cell = workbook[sheet][coordinate]
        cell.value = 1e100
        cell.number_format = "yyyy-mm-dd"
    workbook.save(path)
    return path


def set_cells(path: Path, sheet: str, **cells: object) -> Path:
    workbook = openpyxl.load_workbook(path)
    for coordinate, value in cells.items():
        workbook[sheet][coordinate] = value
    workbook.save(path)
    return path


def worksheets(path: Path) -> dict[str, str]:
    """Each worksheet's XML in ``path``, by member name."""
    with ZipFile(path) as package:
        return {
            name: package.read(name).decode("utf-8")
            for name in package.namelist()
            if name.startswith("xl/worksheets/sheet")
        }


def edit_sheets(path: Path, edit: Callable[[str, str], str]) -> Path:
    """Replace each worksheet's XML in ``path`` with ``edit(member name, XML)``."""
    with ZipFile(path) as source:
        infos = source.infolist()
        payloads = {info.filename: source.read(info.filename) for info in infos}
    for name, payload in payloads.items():
        if name.startswith("xl/worksheets/sheet"):
            payloads[name] = edit(name, payload.decode("utf-8")).encode("utf-8")
    with ZipFile(path, "w") as package:
        for info in infos:
            package.writestr(info, payloads[info.filename])
    return path


def replace_once_in_sheet(old: str, new: str) -> Callable[[str, str], str]:
    def edit(_name: str, sheet: str) -> str:
        assert sheet.count(old) <= 1
        return sheet.replace(old, new)

    return edit


def store_row_out_of_order(path: Path) -> Path:
    """Store an empty row 2 again after the sheet's last row."""
    before = worksheets(path)
    edit_sheets(path, replace_once_in_sheet("</sheetData>", '<row r="2" /></sheetData>'))
    assert worksheets(path) != before
    return path
