"""Pin every Schedule OS output format (#614).

The digests were taken at 07a20f7, before the dividend period split, the
whole-rupee allocations and the 1e totals moved from the writers into
compute. #1033 repinned the hand-built set after re-deriving each figure its
changed dividend moves. #1032 repinned the fixture and hand-built sets after
renaming their securities to synthetic ones and moving two hand-built payment
dates within their periods; no figure changed. #1070 repinned the hand-built
set after renaming its remaining real securities; no figure changed. #1074
repinned every review-csv and xlsx digest after both formats gained the 1b(ix)
interest total row; nothing else moved, and the filing pins didn't change.
#1310 dropped the ``with_non_ordinary`` fixture's pins and its baseline
record: its in-FY Return Of Capital now refuses.
The interest tests at the end check that 1b(ix) total in every format.
CSV pins hash the file bytes.
XLSX pins hash every cell's value, Excel data type and number format, because
openpyxl writes the save time into docProps. The portal filing workbook's
complete raw bytes are also compared, under a fixed clock, with the #1199
baseline in ``tests/fixtures/portal_parity_baseline.json``.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.schedule_os import ScheduleOsComputeResult
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output import (
    RenderContext,
    Subcommand,
    output_binding,
)
from ibkr_fa.output.portal_labels import (
    PORTAL_FIELD_LABEL_OS_OTHER_INTEREST,
    PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_os import run_schedule_os
from tests._ibkr_user_toml import ACCOUNT_AND_FX_USER_TOML as _USER_TOML
from tests._no_echo import assert_no_markers
from tests._portal_parity import assert_workbook_matches, baseline_cases, expected, fix_xlsx_clock
from tests._schedule_os_hand_built import HAND_BUILT_OS_PINS as _HAND_BUILT_PINS
from tests._schedule_os_hand_built import foreign_dividend_line as _foreign
from tests._schedule_os_hand_built import hand_built_os_result as _hand_built_result
from tests._schedule_os_hand_built import other_income_line as _other

_FIXTURES = Path(__file__).parent / "fixtures"
_FORMATS = ("filing-csv", "filing-xlsx", "review-csv", "xlsx")

_FIXTURE_PINS = {
    "iso_dates": {
        "filing-csv": "d75b8a22b7532642b5ac058e696f2958c26f846c5c253be7dfce90b5b4735ca4",
        "filing-xlsx": "f8c04085f8e4fb86bc4f9d0a5185f7b8c7cf6540cc72d98036e91ac745030203",
        "review-csv": "a768498291d221d281226bbcbad96aeda37bcedf242221398c3d054184602c4c",
        "xlsx": "7492c53ab45fb25bf97699d7e76f4c161e47053bf7c9cbb35228bc125f64b13e",
    },
    "with_dividends": {
        "filing-csv": "b621fd4ce21cfd3874445dc8057abf6e661fb5257225e057f066483ba71e628c",
        "filing-xlsx": "b2f297207fb09480f4ed035da1e1414479e41133b69a111e20001dffdae4cdbb",
        "review-csv": "e8ebc96fd7eda53cf2b94ea0b29c9c82624e6126c6414f93e659d3d8b6f830b1",
        "xlsx": "f4728ce73cdf1440476ab14211cb1025d4330d0fa58b2e4c4c9a79f3cdccab38",
    },
    "with_indian_source": {
        "filing-csv": "053cd4c110dfb7a741a53c450abf86a08b4e392f09adefc8ec61fce06135e8a5",
        "filing-xlsx": "c4f956e6c23a9265554e72a23e9bc7ccdb2c0bee200d231df223c79edca9ad79",
        "review-csv": "429a9ce6531cebdcc610f1a1d9c3b62fcca68b69fef064b70386154178dc00c6",
        "xlsx": "bdaa10a22b307b7718fb929c1910a91f506e1b7385881df9e4e247ea5fad0f88",
    },
    "with_interest": {
        "filing-csv": "165b2adeb1825156f5330a949eb9af7a58fdc7aba3924b95314142fe203d1f99",
        "filing-xlsx": "956b11e2d6af557853f98df7ddc6cbb5dd797cd998e060d9c65f3476fa7f605b",
        "review-csv": "cf60fe5e35490e151b08b7465690b1c10fd4f43c0484f0bc9c874fadaba54695",
        "xlsx": "14a46d2db7f092268285210628fd578044461ea7b59bc5b62fef329615410b12",
    },
    "zero_dividends": {
        "filing-csv": "4583ad9c537c469473b7834e7ddb3349218e6b24800795353f6b0238f39d26ec",
        "filing-xlsx": "bf5c0b1d1f1d94fda335c2495dee5dd9ca8d82efab9817ee35cdd84e886a384a",
        "review-csv": "bd9b5e3b5bf893d837e1f9f418c9027bcd8bc8445800e67efa7d292bf4566cb5",
        "xlsx": "a2a782bfd44ec0e3b873a07871fb369ef9e803f596b797f124fb168473ff8537",
    },
}

_FILING_WORKBOOK_CASES = (*sorted(_FIXTURE_PINS), "hand-built")


def _digest(path: Path) -> str:
    if path.suffix != ".xlsx":
        return hashlib.sha256(path.read_bytes()).hexdigest()
    workbook = load_workbook(path)
    try:
        dump = [
            (
                sheet.title,
                sheet.dimensions,
                [
                    (cell.coordinate, cell.data_type, repr(cell.value), cell.number_format)
                    for row in sheet.iter_rows()
                    for cell in row
                ],
            )
            for sheet in workbook.worksheets
        ]
    finally:
        workbook.close()
    return hashlib.sha256(repr(dump).encode()).hexdigest()


def _output_path(tmp_path: Path, format_id: str) -> Path:
    return tmp_path / ("schedule-os.xlsx" if format_id.endswith("xlsx") else "schedule-os.csv")


def _fixture_output(tmp_path: Path, fixture: str, format_id: str) -> Path:
    config = tmp_path / "user.toml"
    config.write_text(_USER_TOML, encoding="utf-8")
    output = _output_path(tmp_path, format_id)
    run_schedule_os(
        input_path=_FIXTURES / f"e2e_activity_statement_schedule_os_{fixture}_fy2025_26.csv",
        config_path=config,
        output_path=output,
        financial_year=FinancialYear(start_year=2025),
        format_id=format_id,
    )
    return output


def _fixture_digest(tmp_path: Path, fixture: str, format_id: str) -> str:
    return _digest(_fixture_output(tmp_path, fixture, format_id))


def _write(tmp_path: Path, format_id: str, result: ScheduleOsComputeResult) -> Path:
    output = _output_path(tmp_path, format_id)
    output_binding(Subcommand.OS, format_id).writer(
        RenderContext(
            assessment_year_start=2026,
            source_label="Robinhood",
            account_labels={0: "Individual", 1: "Joint"},
        )
    ).write(result, output)
    return output


def _hand_built_digest(tmp_path: Path, format_id: str) -> str:
    return _digest(_write(tmp_path, format_id, _hand_built_result()))


@pytest.mark.parametrize("format_id", _FORMATS)
@pytest.mark.parametrize("fixture", sorted(_FIXTURE_PINS))
def test_fixture_outputs_match_pins(tmp_path: Path, fixture: str, format_id: str) -> None:
    assert _fixture_digest(tmp_path, fixture, format_id) == _FIXTURE_PINS[fixture][format_id]


@pytest.mark.parametrize("format_id", _FORMATS)
def test_hand_built_outputs_match_pins(tmp_path: Path, format_id: str) -> None:
    assert _hand_built_digest(tmp_path, format_id) == _HAND_BUILT_PINS[format_id]


@pytest.mark.parametrize("case", _FILING_WORKBOOK_CASES)
def test_filing_workbook_bytes_match_the_portal_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every byte of the filing workbook, docProps/core.xml included (#1199)."""
    fix_xlsx_clock(monkeypatch)
    output = (
        _write(tmp_path, "filing-xlsx", _hand_built_result())
        if case == "hand-built"
        else _fixture_output(tmp_path, case, "filing-xlsx")
    )
    assert_workbook_matches(output.read_bytes(), expected("schedule-os-filing-xlsx", case))


def test_every_filing_workbook_case_has_a_baseline_record() -> None:
    assert set(_FILING_WORKBOOK_CASES) == baseline_cases("schedule-os-filing-xlsx")


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_interest_fixture_output_differs_from_zero_dividends(format_id: str) -> None:
    assert _FIXTURE_PINS["with_interest"][format_id] != _FIXTURE_PINS["zero_dividends"][format_id]


def _labelled_cells(path: Path) -> list[tuple[object, object, str | None]]:
    """(first cell of its row, value, number format) for every non-empty cell."""
    if path.suffix != ".xlsx":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [(row[0], value, None) for row in csv.reader(handle) for value in row if value]
    workbook = load_workbook(path)
    try:
        return [
            (row[0].value, cell.value, cell.number_format)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows()
            for cell in row
            if cell.value is not None
        ]
    finally:
        workbook.close()


_INTEREST_CELLS = {
    "filing-csv": (PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST, "4321", None),
    "filing-xlsx": (PORTAL_FIELD_LABEL_OS_SECTION_1_OTHER_INTEREST, 4321, "#,##0"),
    "review-csv": (PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, "4321", None),
    "xlsx": (PORTAL_FIELD_LABEL_OS_OTHER_INTEREST, 4321, "#,##0"),
}


@pytest.mark.parametrize("format_id", _FORMATS)
def test_interest_only_result_shows_its_total_once_under_the_1b_ix_label(
    tmp_path: Path, format_id: str
) -> None:
    result = ScheduleOsComputeResult(
        lines=[],
        domestic_lines=[],
        interest_total_inr_rounded=Decimal("4321"),
    )
    cells = _labelled_cells(_write(tmp_path, format_id, result))
    assert [cell for cell in cells if "4321" in str(cell[1])] == [_INTEREST_CELLS[format_id]]


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing"])
@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
def test_review_formats_refuse_a_result_without_the_interest_total(
    tmp_path: Path, format_id: str, *, existing: bool
) -> None:
    result = ScheduleOsComputeResult(
        lines=[_foreign("XQZA", "US99998A1034", "US", date(2025, 5, 15), "1.21", "83.17", 0)],
        domestic_lines=[],
        other_income_lines=(
            replace(
                _other("Securities lending fee", date(2025, 6, 1), "0.11", 1),
                description="Synthetic Holder Two",
            ),
        ),
    )
    output = _output_path(tmp_path, format_id)
    if existing:
        output.write_bytes(b"sentinel")

    with pytest.raises(InvariantError) as excinfo:
        _write(tmp_path, format_id, result)

    message = str(excinfo.value)
    assert message == (
        "InvariantError: Schedule OS review output requires the separately computed interest total"
    )
    assert excinfo.value.context == ()
    assert_no_markers(message, "XQZA", "Synthetic Holder Two")
    assert list(tmp_path.iterdir()) == ([output] if existing else [])
    if existing:
        assert output.read_bytes() == b"sentinel"
