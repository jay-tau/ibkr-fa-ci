"""The Table F block of the four Schedule CG review writers (#1361).

Each writer, STCG and LTCG in CSV and XLSX, ends its rows with a blank row,
the ``_table_f:`` marker, the five Table F date bands of its section's
``table_f`` row, and the fixed note, just before its audit TOTAL and portal
TOTAL rows. Lines here use ₹1/$ (``tests._cg_lines``), so a line's INR gain
is its proceeds less its cost.
"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.compliance.itr_form_selection import ItrForm
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_cg_table_f import ScheduleCgTableFCase, ScheduleCgTableFRow
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.models.schedule_cg_ltcg_row import ScheduleCgLtcgLine
from ibkr_fa.models.schedule_cg_row import ScheduleCgStcgLine
from ibkr_fa.output import Subcommand
from ibkr_fa.output.portal_labels import for_ay
from ibkr_fa.output.schedule_cg_review_csv import (
    TABLE_F_MARKER,
    TABLE_F_NOTE,
    table_f_rows,
)
from ibkr_fa.output.schedules.cg_filing import TABLE_F_BLANK_BASIS
from tests._cg_lines import (
    loss_across_bands_ltcg_result,
    loss_across_bands_stcg_result,
    ltcg_line,
    sold_on,
    stcg_line,
)
from tests._writer_stubs import writer_class

_LABELS = for_ay(2026)
_WRITERS = [
    pytest.param(Subcommand.CG, "review-csv", id="stcg-csv"),
    pytest.param(Subcommand.CG, "xlsx", id="stcg-xlsx"),
    pytest.param(Subcommand.CG_LTCG, "review-csv", id="ltcg-csv"),
    pytest.param(Subcommand.CG_LTCG, "xlsx", id="ltcg-xlsx"),
]
# A sale date in bands (i) Upto 15/6 and (iii) 16/9 to 15/12.
_BAND_I, _BAND_III = date(2025, 6, 10), date(2025, 10, 15)
_INTEGER_RUPEE_FORMAT = "#,##0;-#,##0"


def _line(
    subcommand: Subcommand, sold: date, proceeds_inr: str, cost_inr: str
) -> ScheduleCgStcgLine | ScheduleCgLtcgLine:
    """A line of ``subcommand``'s section sold on ``sold``, at ₹1/$."""
    if subcommand is Subcommand.CG:
        return sold_on(stcg_line(proceeds_inr, cost_inr), sold)
    return sold_on(ltcg_line(proceeds_inr, cost_inr), sold)


def _result(
    subcommand: Subcommand, *sales: tuple[date, str, str]
) -> ScheduleCgStcgComputeResult | ScheduleCgLtcgComputeResult:
    lines = [_line(subcommand, *sale) for sale in sales]
    if subcommand is Subcommand.CG:
        return ScheduleCgStcgComputeResult(
            lines,  # ty: ignore[invalid-argument-type]
            [],
            [],
            [],
        )
    return ScheduleCgLtcgComputeResult(
        lines,  # ty: ignore[invalid-argument-type]
        [],
        [],
        [],
        [],
    )


def _written(
    subcommand: Subcommand,
    format_id: str,
    result: ScheduleCgStcgComputeResult | ScheduleCgLtcgComputeResult,
    tmp_path: Path,
    *,
    itr_form: ItrForm = "ITR-2",
) -> list[list[object]]:
    """Every row the writer writes; a blank cell is ``""`` in both formats."""
    path = tmp_path / ("out.xlsx" if format_id == "xlsx" else "out.csv")
    writer_class(subcommand, format_id)(assessment_year_start=2026, itr_form=itr_form).write(
        result, path
    )
    if format_id == "xlsx":
        sheet = load_workbook(path).active
        assert sheet is not None
        return [
            ["" if value is None else value for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [list[object](row) for row in csv.reader(handle)]


def _band_labels(subcommand: Subcommand, itr_form: ItrForm) -> list[str]:
    row, row_labels = (
        (3, _LABELS.PORTAL_ROW_LABEL_CG_TABLE_F_STCG_BY_ITR_FORM)
        if subcommand is Subcommand.CG
        else (5, _LABELS.PORTAL_ROW_LABEL_CG_TABLE_F_LTCG_BY_ITR_FORM)
    )
    return [
        f"Schedule CG / F / {row}({column}) {heading}: {row_labels[itr_form]}"
        for column, heading in zip(
            ("i", "ii", "iii", "iv", "v"), _LABELS.PORTAL_COLUMN_HEADERS_CG_TABLE_F, strict=True
        )
    ]


def _bands(rows: list[list[object]]) -> list[tuple[str, object, object]]:
    """Each band row's label, second cell and amount, after checking the block's shape."""
    blank, marker, *band_rows, note = rows[-10:-2]
    assert blank == [""] * 16
    assert marker == [TABLE_F_MARKER, *[""] * 15]
    assert note == [TABLE_F_NOTE, *[""] * 15]
    assert len(band_rows) == 5
    for row in band_rows:
        assert len(row) == 16
        assert row[2:15] == [""] * 13
    return [(str(row[0]), row[1], row[15]) for row in band_rows]


def _amount(cell: object) -> Decimal:
    """A CSV or XLSX amount cell; the CSV escapes a loss's sign as ``'-``."""
    return Decimal(str(cell).removeprefix("'"))


@pytest.mark.parametrize(("subcommand", "format_id"), _WRITERS)
@pytest.mark.parametrize("itr_form", ["ITR-2", "ITR-3"])
def test_bands_split_the_total_before_both_totals(
    tmp_path: Path, subcommand: Subcommand, format_id: str, itr_form: ItrForm
) -> None:
    """Gains of ₹100.4 in band (i) and ₹200.4 in band (iii) file a total of
    ₹301 (₹800.8 consideration rounds to ₹801, less ₹500 cost). The nets'
    floors give ₹300, and the spare rupee goes to the earlier band on the
    tied remainder: (101, 0, 200, 0, 0)."""
    result = _result(subcommand, (_BAND_I, "300.4", "200"), (_BAND_III, "500.4", "300"))
    rows = _written(subcommand, format_id, result, tmp_path, itr_form=itr_form)

    bands = _bands(rows)
    assert [label for label, _, _ in bands] == _band_labels(subcommand, itr_form)
    assert [second for _, second, _ in bands] == [""] * 5
    assert [_amount(amount) for _, _, amount in bands] == [101, 0, 200, 0, 0]
    assert rows[-2][0] == "_audit: TOTAL Realized gain INR (full precision)"
    assert _amount(rows[-1][15]) == 301 == sum(_amount(amount) for _, _, amount in bands)
    assert {len(row) for row in rows} == {16}


@pytest.mark.parametrize("subcommand", [Subcommand.CG, Subcommand.CG_LTCG])
def test_xlsx_bands_are_whole_rupee_numbers(tmp_path: Path, subcommand: Subcommand) -> None:
    path = tmp_path / "out.xlsx"
    writer_class(subcommand, "xlsx")().write(
        _result(subcommand, (_BAND_I, "300.4", "200"), (_BAND_III, "500.4", "300")), path
    )
    sheet = load_workbook(path).active
    assert sheet is not None
    # The five bands sit above the note, audit TOTAL and portal TOTAL rows.
    cells = [sheet.cell(row=row, column=16) for row in range(sheet.max_row - 7, sheet.max_row - 2)]
    assert [(cell.value, cell.data_type) for cell in cells] == [
        (101, "n"),
        (0, "n"),
        (200, "n"),
        (0, "n"),
        (0, "n"),
    ]
    assert {cell.number_format for cell in cells} == {_INTEGER_RUPEE_FORMAT}


@pytest.mark.parametrize(("subcommand", "format_id"), _WRITERS)
@pytest.mark.parametrize(
    "sales",
    [
        pytest.param((), id="no-line"),
        pytest.param(((_BAND_I, "100", "200"), (_BAND_III, "250", "200")), id="net-loss"),
    ],
)
def test_no_positive_total_gives_five_zeros(
    tmp_path: Path, subcommand: Subcommand, format_id: str, sales: tuple[tuple[date, str, str], ...]
) -> None:
    """With no in-FY line, or a net loss, the file adds nothing to Table F."""
    rows = _written(subcommand, format_id, _result(subcommand, *sales), tmp_path)

    assert [(second, _amount(amount)) for _, second, amount in _bands(rows)] == [("", 0)] * 5
    assert _amount(rows[-1][15]) <= 0


@pytest.mark.parametrize(("subcommand", "format_id"), _WRITERS)
def test_loss_across_bands_leaves_the_bands_blank(
    tmp_path: Path, subcommand: Subcommand, format_id: str
) -> None:
    """Gains in bands (i) and (iv) beside a loss in band (iii), netting ₹150:
    no band is chosen for the loss, so all five are blank and say why."""
    result = (
        loss_across_bands_stcg_result()
        if subcommand is Subcommand.CG
        else loss_across_bands_ltcg_result()
    )
    assert result.table_f.case is ScheduleCgTableFCase.LOSS_ACROSS_BANDS
    rows = _written(subcommand, format_id, result, tmp_path)

    reason = TABLE_F_BLANK_BASIS[ScheduleCgTableFCase.LOSS_ACROSS_BANDS]
    assert [(second, amount) for _, second, amount in _bands(rows)] == [(reason, "")] * 5
    assert _amount(rows[-1][15]) == 150


def test_a_manual_section_row_refuses() -> None:
    """#1383: no Schedule CG section is entered by hand any more, so a
    MANUAL_SECTION row has no reason; one reaching the Table F block all four
    review writers share refuses with fixed text, not a KeyError or a
    computed row's note. No engine result builds such a row, so the block is
    built directly."""
    manual = ScheduleCgTableFRow(ScheduleCgTableFCase.MANUAL_SECTION, None)

    with pytest.raises(InvariantError) as raised:
        list(table_f_rows(manual, tuple(_band_labels(Subcommand.CG, "ITR-2"))))

    assert str(raised.value) == (
        "InvariantError: a Schedule CG Table F row is MANUAL_SECTION, which no Schedule CG "
        "section builds"
    )
    assert raised.value.context == ()
