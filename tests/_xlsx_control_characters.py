"""Shared cases for the workbook control-character refusal (#1357).

openpyxl can't store U+0000 to U+0008, U+000B, U+000C or U+000E to U+001F in a
cell, and its own error holds the whole cell text. Every workbook writer
refuses such a cell first, naming only its row and cell index.
"""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from ibkr_fa.output._writer_contract import WriterContractError
from tests._crash_reports import assert_unchained_refusal

# A digit run standing for a value a cell could hold; no refusal may print it.
MARKER = "473812"

# Characters a workbook cell can't store. None is first in the cell, where the
# formula check takes a U+000B.
REFUSED = pytest.mark.parametrize(
    "character",
    ["\x01", "\x08", "\x0c", "\x0e", "\x1f", "\x0b"],
    ids=["U+0001", "U+0008", "U+000C", "U+000E", "U+001F", "U+000B"],
)
# Control characters a workbook cell stores. None is first in the cell, where
# the formula check takes tab, line feed and carriage return.
STORED = pytest.mark.parametrize(
    "character",
    ["\t", "\n", "\r", "\x7f"],
    ids=["tab", "line-feed", "carriage-return", "U+007F"],
)


def text_with(character: str) -> str:
    """Cell text holding ``character`` and :data:`MARKER`, neither of them first."""
    return f"SAMPLE{character}ISSUER {MARKER}"


def refusal_text(row: int, cell: int) -> str:
    return (
        f"WriterContractError: row {row} cell at index {cell} holds a control character"
        " a workbook cell can't store"
    )


def assert_refused(error: BaseException, *, row: int, cell: int, directory: Path) -> None:
    """``error`` is the control-character refusal at ``row`` and ``cell``; it prints
    no :data:`MARKER` and chains nothing, and ``directory`` is left empty."""
    expected = WriterContractError(
        violation="illegal_character", row_index=row, cell_index=cell, trigger=""
    )
    assert_unchained_refusal(error, expected, refusal_text(row, cell), markers=(MARKER,))
    assert list(directory.iterdir()) == []


def sheet_xml(path: Path) -> bytes:
    """The workbook's only worksheet XML, as saved."""
    with ZipFile(path) as archive:
        return archive.read("xl/worksheets/sheet1.xml")
