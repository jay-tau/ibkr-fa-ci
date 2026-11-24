"""Every Schedule CG output files the same Policy A total (#615).

Proceeds ₹1000.4 and cost ₹1200.6 round to 1000 and 1201, so the
component balance is -201 while the rounded gain sum is -200.
"""

from __future__ import annotations

import csv
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook

from ibkr_fa.compute.robinhood_capital_gains import RobinhoodCapitalGainsResult
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.output import (
    Subcommand,
)
from tests._cg_lines import ltcg_result, stcg_result
from tests._robinhood_cg import ACCOUNT_LABELS
from tests._schedule_cg_filing_workbook import robinhood_ltcg_result as _ltcg_result
from tests._schedule_cg_filing_workbook import robinhood_stcg_result as _result
from tests._schedule_cg_filing_workbook import values_by_field as _values_by_field
from tests._schedule_cg_filing_workbook import write_result_workbook as _write
from tests._writer_stubs import writer_class


def _robinhood_stcg(proceeds: str, cost: str) -> RobinhoodCapitalGainsResult:
    result = _result()
    line = replace(
        result.stcg.stcg_lines[0],
        proceeds_usd=Decimal(proceeds) / 100,
        proceeds_inr=Decimal(proceeds),
        cost_basis_inr=Decimal(cost),
        realized_gain_inr=Decimal(proceeds) - Decimal(cost),
    )
    return replace(result, stcg=replace(result.stcg, stcg_lines=[line]))


def _portal_cell(path: Path) -> object:
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))[-1][-1]
    workbook = load_workbook(path, data_only=True)
    worksheet = workbook.active
    assert worksheet is not None
    value = worksheet.cell(worksheet.max_row, worksheet.max_column).value
    workbook.close()
    return value


@pytest.mark.parametrize(("format_id", "suffix"), [("review-csv", ".csv"), ("xlsx", ".xlsx")])
def test_generic_writers_file_the_component_total(
    tmp_path: Path, format_id: str, suffix: str
) -> None:
    stcg_out = tmp_path / f"stcg{suffix}"
    ltcg_out = tmp_path / f"ltcg{suffix}"

    writer_class(Subcommand.CG, format_id)().write(stcg_result("1000.4", "1200.6"), stcg_out)
    writer_class(Subcommand.CG_LTCG, format_id)().write(ltcg_result("1000.4", "1200.6"), ltcg_out)

    expected = "'-201" if suffix == ".csv" else -201
    assert _portal_cell(stcg_out) == expected
    assert _portal_cell(ltcg_out) == expected


def test_robinhood_workbook_files_the_component_total(tmp_path: Path) -> None:
    stcg = _robinhood_stcg("1000.4", "1200.6")
    ltcg = _ltcg_result(cost=Decimal("12.006"), proceeds=Decimal("10.004"))
    stcg_out, ltcg_out = tmp_path / "stcg.xlsx", tmp_path / "ltcg.xlsx"

    for result, output in ((stcg, stcg_out), (ltcg, ltcg_out)):
        _write(
            result,
            output,
            assessment_year_start=2026,
            itr_form="ITR-3",
            account_labels=ACCOUNT_LABELS,
            include_checks=True,
        )

    assert _values_by_field(stcg_out, "ITR-3 A6 STCG")["A6(g)"] == -201
    assert _values_by_field(ltcg_out, "ITR-3 B9 LTCG")["B9(e)"] == -201


@pytest.mark.parametrize("format_id", ["review-csv", "xlsx"])
@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_generic_writers_refuse_before_writing(
    tmp_path: Path, format_id: str, schedule: str
) -> None:
    output = tmp_path / "out"
    if schedule == "STCG":
        writer, result = (
            writer_class(Subcommand.CG, format_id)(),
            stcg_result("0.40", "0.60"),
        )
    else:
        writer, result = (
            writer_class(Subcommand.CG_LTCG, format_id)(),
            ltcg_result("0.40", "0.60"),
        )

    with pytest.raises(InvariantError) as excinfo:
        writer.write(result, output)  # type: ignore[arg-type]

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("schedule", ["STCG", "LTCG"])
def test_robinhood_workbook_refuses_before_writing(tmp_path: Path, schedule: str) -> None:
    if schedule == "STCG":
        result = _robinhood_stcg("0.40", "0.60")
    else:
        result = _ltcg_result(cost=Decimal("0.006"), proceeds=Decimal("0.004"))
    output = tmp_path / "schedule-cg.xlsx"

    with pytest.raises(InvariantError) as excinfo:
        _write(
            result,
            output,
            assessment_year_start=2026,
            itr_form="ITR-3",
            account_labels=ACCOUNT_LABELS,
        )

    assert dict(excinfo.value.context) == {
        "schedule": None,
        "consideration_inr": None,
        "cost_of_acquisition_inr": None,
    }
    assert list(tmp_path.iterdir()) == []
