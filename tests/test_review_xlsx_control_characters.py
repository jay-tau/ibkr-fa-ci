"""Each review ``--format xlsx`` writer refuses a control character a workbook
cell can't store, naming only the row and cell (#1357).

The FA, OS and CG workbooks get the text, and a number, through model fields.
The FSI, TR and Form 67 models refuse such text, so their detail row is patched
to carry it, as their NUL-byte tests do.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import pytest

from ibkr_fa.compute.form67 import Form67ComputeResult
from ibkr_fa.compute.schedule_cg import DeferredLtcgRow
from ibkr_fa.compute.schedule_cg_ltcg import DeferredStcgRow
from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.compute.schedule_tr import ScheduleTrComputeResult
from ibkr_fa.output import form67_xlsx, schedule_fsi_xlsx, schedule_tr_xlsx
from ibkr_fa.output._writer_contract import WriterContractError
from ibkr_fa.output.schedule_cg_ltcg_xlsx import ScheduleCgLtcgXlsxWriter
from ibkr_fa.output.schedule_cg_xlsx import ScheduleCgXlsxWriter
from ibkr_fa.output.schedule_fa_a2_xlsx import ScheduleFaA2XlsxWriter
from ibkr_fa.output.schedule_os_xlsx import ScheduleOsXlsxWriter
from ibkr_fa.output.xlsx import XlsxWriter
from tests import test_form67_xlsx as form67_tests
from tests import test_output_fa_a2 as fa_a2_tests
from tests import test_output_xlsx as fa_a3_tests
from tests import test_schedule_cg_ltcg_xlsx as cg_ltcg_tests
from tests import test_schedule_cg_xlsx as cg_tests
from tests import test_schedule_fsi_xlsx as fsi_tests
from tests import test_schedule_os_xlsx as os_tests
from tests import test_schedule_tr_xlsx as tr_tests
from tests._cbdt_fragment_inputs import unchecked_row as _unchecked
from tests._xlsx_control_characters import (
    MARKER,
    REFUSED,
    STORED,
    assert_refused,
    sheet_xml,
    text_with,
)

# A number no workbook cell can store: it reads back as infinite (#832).
_UNSTORABLE = Decimal("1E400")


class _Write(Protocol):
    """Writes a review workbook with ``text`` in one string cell and, when
    ``number`` is given, that number in one number cell of the same row."""

    def __call__(
        self,
        text: str,
        path: Path,
        monkeypatch: pytest.MonkeyPatch,
        number: Decimal | None = None,
        /,
    ) -> None: ...


def _given(field: str, number: Decimal | None) -> dict[str, Any]:
    return {} if number is None else {field: number}


def _fa_a3(
    text: str, path: Path, _monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    # The row refuses a control character itself (#1367); this pins the writer.
    row = _unchecked(fa_a3_tests.make_row(**_given("cost_usd", number)), name_of_entity=text)
    XlsxWriter().write([row], path)


def _fa_a2(
    text: str, path: Path, _monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    row = fa_a2_tests._row(
        institution_name=text, **_given("audit_closing_balance_base_ccy", number)
    )
    ScheduleFaA2XlsxWriter().write([row], path)


def _os(
    text: str, path: Path, _monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    line = os_tests.make_line(symbol=text, **_given("amount_gross_usd", number))
    ScheduleOsXlsxWriter().write(os_tests.make_result(lines=[line]), path)


def _cg(
    text: str, path: Path, _monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    """The lot's statement description, in the excluded LTCG block."""
    lot = cg_tests._make_closed_lot(description=text, **_given("quantity", number))
    deferred = DeferredLtcgRow(closed_lot=lot, holding_period_days=1430)
    ScheduleCgXlsxWriter().write(cg_tests._result_with(excluded_ltcg_deferred=[deferred]), path)


def _cg_ltcg(
    text: str, path: Path, _monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    """The lot's statement description, in the excluded STCG block."""
    lot = cg_ltcg_tests._make_closed_lot(description=text, **_given("quantity", number))
    deferred = DeferredStcgRow(closed_lot=lot, holding_period_days=400)
    ScheduleCgLtcgXlsxWriter().write(cg_ltcg_tests._result_with(excluded_stcg=[deferred]), path)


def _patch_detail_row(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    text: tuple[int, str],
    number: tuple[int, Decimal | None],
) -> None:
    """Make ``module``'s detail rows carry ``text``, and ``number`` unless it is
    :data:`None`, at their cell indexes."""
    original = module._detail_row
    cells = dict([text] if number[1] is None else [text, number])

    def detail_row(row: object) -> list[object]:
        patched = list(original(row))
        for index, value in cells.items():
            patched[index] = value
        return patched

    monkeypatch.setattr(module, "_detail_row", detail_row)


# Each patched writer's text cell and a Decimal cell.
_FSI_TEXT, _FSI_NUMBER = 9, 10  # _audit: Country name (portal), _audit: Income INR
_TR_TEXT, _TR_NUMBER = 6, 7  # _audit: Country name, _audit: foreign tax INR
_FORM67_TEXT, _FORM67_NUMBER = 1, 14  # country dropdown, _audit: foreign tax INR


def _fsi(
    text: str, path: Path, monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    _patch_detail_row(
        monkeypatch, schedule_fsi_xlsx, text=(_FSI_TEXT, text), number=(_FSI_NUMBER, number)
    )
    schedule_fsi_xlsx.ScheduleFsiXlsxWriter().write(
        ScheduleFsiComputeResult(rows=[fsi_tests._make_row()]), path
    )


def _tr(
    text: str, path: Path, monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    _patch_detail_row(
        monkeypatch, schedule_tr_xlsx, text=(_TR_TEXT, text), number=(_TR_NUMBER, number)
    )
    schedule_tr_xlsx.ScheduleTrXlsxWriter().write(
        ScheduleTrComputeResult(rows=[tr_tests._make_row()]), path
    )


def _form67(
    text: str, path: Path, monkeypatch: pytest.MonkeyPatch, number: Decimal | None = None
) -> None:
    _patch_detail_row(
        monkeypatch, form67_xlsx, text=(_FORM67_TEXT, text), number=(_FORM67_NUMBER, number)
    )
    form67_xlsx.Form67XlsxWriter().write(
        Form67ComputeResult(entries=(form67_tests._make_entry(),)), path
    )


# Each writer, the row and cell index its text lands in, and the cell index its
# number lands in, in the same row.
_WRITERS: dict[str, tuple[_Write, tuple[int, int], int]] = {
    "fa-a3": (_fa_a3, (1, 1), 14),
    "fa-a2": (_fa_a2, (1, 1), 12),
    "os": (_os, (1, 0), 5),
    "cg": (_cg, (3, 11), 8),
    "cg-ltcg": (_cg_ltcg, (3, 11), 8),
    "fsi": (_fsi, (1, _FSI_TEXT), _FSI_NUMBER),
    "tr": (_tr, (1, _TR_TEXT), _TR_NUMBER),
    "form67": (_form67, (1, _FORM67_TEXT), _FORM67_NUMBER),
}
_CASES = [(write, position) for write, position, _number_cell in _WRITERS.values()]


@REFUSED
@pytest.mark.parametrize(("write", "position"), _CASES, ids=_WRITERS.keys())
def test_control_character_refuses_without_a_workbook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write: _Write,
    position: tuple[int, int],
    character: str,
) -> None:
    with pytest.raises(WriterContractError) as raised:
        write(text_with(character), tmp_path / "out.xlsx", monkeypatch)

    assert_refused(raised.value, row=position[0], cell=position[1], directory=tmp_path)
    assert capsys.readouterr() == ("", "")


@STORED
@pytest.mark.parametrize("write", [write for write, _position in _CASES], ids=_WRITERS.keys())
def test_control_character_a_cell_stores_is_written_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write: _Write, character: str
) -> None:
    path = tmp_path / "out.xlsx"

    write(text_with(character), path, monkeypatch)

    assert text_with(character).encode() in sheet_xml(path)


@pytest.mark.parametrize(
    ("text", "violation"),
    [
        (text_with("\x01") + "\x00", "nul_byte"),
        ("=" + text_with("\x01"), "formula_trigger"),
        ("\x0b" + text_with("\x01"), "formula_trigger"),
    ],
    ids=["nul-byte", "formula-trigger", "leading-u+000b"],
)
@pytest.mark.parametrize(("write", "position"), _CASES, ids=_WRITERS.keys())
def test_nul_and_formula_checks_still_refuse_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    position: tuple[int, int],
    text: str,
    violation: str,
) -> None:
    """A cell holding a U+0001 and a NUL byte or a leading formula trigger keeps
    that older refusal."""
    with pytest.raises(WriterContractError) as raised:
        write(text, tmp_path / "out.xlsx", monkeypatch)

    error = raised.value
    assert (error.violation, (error.row_index, error.cell_index)) == (violation, position)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("write", "position", "number_cell"), _WRITERS.values(), ids=_WRITERS.keys()
)
def test_unrepresentable_number_still_refuses_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: _Write,
    position: tuple[int, int],
    number_cell: int,
) -> None:
    """A number the workbook can't store, in the row of a cell holding a U+0001,
    keeps its own refusal."""
    with pytest.raises(WriterContractError) as raised:
        write(text_with("\x01"), tmp_path / "out.xlsx", monkeypatch, _UNSTORABLE)

    error = raised.value
    assert (error.violation, (error.row_index, error.cell_index)) == (
        "unrepresentable_number",
        (position[0], number_cell),
    )
    assert list(tmp_path.iterdir()) == []


def test_control_character_past_openpyxl_cut_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """openpyxl keeps a cell's first 32,767 characters and checks only those; a
    review cell whose only U+0001 comes after them now refuses (#1357)."""
    with pytest.raises(WriterContractError) as raised:
        _cg(MARKER + "x" * 32_767 + "\x01", tmp_path / "out.xlsx", monkeypatch)

    assert_refused(raised.value, row=3, cell=11, directory=tmp_path)
