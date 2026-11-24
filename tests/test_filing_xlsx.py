"""Tests for the shared filing-workbook renderer's row checks (#1205, #1323, #1357)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.filing_xlsx import EntrySheet, FilingXlsxWriter, FormSheet, TableSheet
from tests._xlsx_control_characters import (
    REFUSED,
    STORED,
    assert_refused,
    sheet_xml,
    text_with,
)

# Excel's cell limit, counted in UTF-16 code units (#1323).
_LIMIT = 32_767
# A character outside the Basic Multilingual Plane: one code point, two UTF-16 units.
_ASTRAL = "\U0001f600"
_TOO_LONG = (
    "WriterContractError: row {row} cell at index {cell} is longer than a workbook cell"
    " can hold (32,767 characters)"
)


def _entry_note(text: str) -> EntrySheet:
    return EntrySheet(
        title="Entry",
        instruction="Instruction.",
        warning=text,
        footnote="Footnote.",
        header=("Field", "Amount"),
        rows=(),
        widths=(20, 20),
    )


def _entry_row(text: str) -> EntrySheet:
    return EntrySheet(
        title="Entry",
        instruction="Instruction.",
        warning="Warning.",
        footnote="Footnote.",
        header=("Field", "Amount"),
        rows=(("Field one", text),),
        widths=(20, 20),
    )


def _table_row(text: str) -> TableSheet:
    return TableSheet(
        title="Table",
        note="Note.",
        header=("Field", "Amount"),
        rows=(("Field one", text),),
        widths=(20, 20),
    )


def _form_row(text: str) -> FormSheet:
    return FormSheet(
        title="Form",
        rows=(("Heading", ""), ("Field one", text)),
        widths=(20, 20),
        merge="A1:B1",
        heading_prefix="Heading",
        column_headers=frozenset(),
        note_prefix="Note",
    )


def _table_cells(first: str, second: str) -> TableSheet:
    return replace(_table_row(second), rows=((first, second),))


def _form_cells(first: str, second: str) -> FormSheet:
    return replace(_form_row(second), rows=(("Heading", ""), (first, second)))


type _Sheet = EntrySheet | TableSheet | FormSheet
type _MakeSheet = Callable[[str], _Sheet]

# Each site: the sheet holding ``text`` there, the row and cell index a refusal
# names, and the cell the text is written to.
_SITES: dict[str, tuple[_MakeSheet, tuple[int, int], str]] = {
    "entry-note": (_entry_note, (1, 0), "A2"),
    "entry-row": (_entry_row, (5, 1), "B6"),
    "table-row": (_table_row, (2, 1), "B3"),
    "form-row": (_form_row, (1, 1), "B2"),
}
_POSITIONS = [(make, position) for make, position, _coordinate in _SITES.values()]
_COORDINATES = [(make, coordinate) for make, _position, coordinate in _SITES.values()]


def _write(sheet: _Sheet, path: Path) -> None:
    FilingXlsxWriter(lambda _value: [sheet]).write(None, path)


@pytest.mark.parametrize(
    "text",
    ["x" * (_LIMIT + 1), "x" * (_LIMIT - 1) + _ASTRAL],
    ids=["32768-characters", "32767-characters-32768-utf16-units"],
)
@pytest.mark.parametrize(("make", "position"), _POSITIONS, ids=_SITES.keys())
def test_cell_longer_than_an_excel_cell_refuses_without_a_workbook(
    tmp_path: Path, make: _MakeSheet, position: tuple[int, int], text: str
) -> None:
    """A cell over Excel's UTF-16 limit refuses before its row is appended."""
    path = tmp_path / "out.xlsx"

    with pytest.raises(WriterContractError) as raised:
        _write(make(text), path)

    error = raised.value
    assert (error.violation, (error.row_index, error.cell_index)) == ("text_too_long", position)
    assert str(error) == _TOO_LONG.format(row=position[0], cell=position[1])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "text",
    ["x" * _LIMIT, "x" * (_LIMIT - 2) + _ASTRAL],
    ids=["32767-characters", "32767-utf16-units"],
)
@pytest.mark.parametrize(("make", "coordinate"), _COORDINATES, ids=_SITES.keys())
def test_cell_of_exactly_an_excel_cell_is_written_whole(
    tmp_path: Path, make: _MakeSheet, coordinate: str, text: str
) -> None:
    path = tmp_path / "out.xlsx"

    _write(make(text), path)

    workbook = load_workbook(path)
    try:
        assert workbook.active[coordinate].value == text
    finally:
        workbook.close()


@pytest.mark.parametrize(
    ("other", "violation"),
    [("a\x00b", "nul_byte"), ("=1", "formula_trigger")],
    ids=["nul-byte", "formula-trigger"],
)
def test_length_is_checked_after_the_nul_and_formula_checks(
    tmp_path: Path, other: str, violation: str
) -> None:
    """A too-long first cell still reports a later cell's NUL byte or formula trigger."""
    path = tmp_path / "out.xlsx"
    sheet = TableSheet(
        title="Table",
        note="Note.",
        header=("Field", "Amount"),
        rows=(("x" * (_LIMIT + 1), other),),
        widths=(20, 20),
    )

    with pytest.raises(WriterContractError) as raised:
        _write(sheet, path)

    assert (raised.value.violation, raised.value.cell_index) == (violation, 1)
    assert list(tmp_path.iterdir()) == []


@REFUSED
@pytest.mark.parametrize(("make", "position"), _POSITIONS, ids=_SITES.keys())
def test_control_character_refuses_without_a_workbook(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    make: _MakeSheet,
    position: tuple[int, int],
    character: str,
) -> None:
    """A character a workbook cell can't store refuses before its row is appended,
    naming only the row and cell (#1357)."""
    with pytest.raises(WriterContractError) as raised:
        _write(make(text_with(character)), tmp_path / "out.xlsx")

    assert_refused(raised.value, row=position[0], cell=position[1], directory=tmp_path)
    assert capsys.readouterr() == ("", "")


@STORED
@pytest.mark.parametrize("make", [make for make, _position in _POSITIONS], ids=_SITES.keys())
def test_control_character_a_cell_stores_is_written_unchanged(
    tmp_path: Path, make: _MakeSheet, character: str
) -> None:
    path = tmp_path / "out.xlsx"

    _write(make(text_with(character)), path)

    assert text_with(character).encode() in sheet_xml(path)


@pytest.mark.parametrize(
    ("other", "violation"),
    [
        ("a\x00b", "nul_byte"),
        ("=1", "formula_trigger"),
        ("\vx", "formula_trigger"),
        ("x" * (_LIMIT + 1), "text_too_long"),
    ],
    ids=["nul-byte", "formula-trigger", "leading-u+000b", "too-long"],
)
@pytest.mark.parametrize(
    ("make", "position"),
    [(_table_cells, (2, 1)), (_form_cells, (1, 1))],
    ids=["table-row", "form-row"],
)
def test_control_character_is_checked_after_every_other_check(
    tmp_path: Path,
    make: Callable[[str, str], _Sheet],
    position: tuple[int, int],
    other: str,
    violation: str,
) -> None:
    """A control character in the first cell still lets the next cell's NUL byte,
    formula trigger or too-long text refuse with its own violation (#1357)."""
    with pytest.raises(WriterContractError) as raised:
        _write(make(text_with("\x01"), other), tmp_path / "out.xlsx")

    error = raised.value
    assert (error.violation, (error.row_index, error.cell_index)) == (violation, position)
    assert list(tmp_path.iterdir()) == []


def test_control_character_past_the_length_limit_refuses_as_too_long(tmp_path: Path) -> None:
    """A too-long cell whose only control character comes after its first 32,767
    characters refuses for its length, as on main."""
    with pytest.raises(WriterContractError) as raised:
        _write(_table_row("x" * _LIMIT + "\x01"), tmp_path / "out.xlsx")

    assert (raised.value.violation, raised.value.cell_index) == ("text_too_long", 1)
    assert list(tmp_path.iterdir()) == []
