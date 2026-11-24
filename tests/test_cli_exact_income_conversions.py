"""Exact income, foreign-tax and Form 67 arithmetic through the real CLI (#1312).

#1312 keeps every digit at the shared compute sites on IBKR's filed paths:
the Schedule FA per-symbol dividend and Table A2 interest conversions at the
Dec-31 TTBR (StockPlan's Table A3 dividend shares the first), each withholding
row's INR conversion (Schedule FSI and TR tax paid, Form 67 column 5), Form
67's treaty-rate test and cap (columns 11 and 13), the
withholding reversal pairing's negation, and the Schedule OS and FSI
conversions and sums, which now work at ``EXACT_SUM_DIGITS`` digits instead
of the active 28-digit precision. Unsnapped column 6 files the exact rate's hundredths when its
context keeps a third decimal place, while preserving ordinary audit rates.
Each regression is an in-limit amount whose 28-digit
result lands on the other side of a filed rounding, or of an equality, from
its exact value; main filed or refused that 28-digit result. Each
control is a nearby input whose 28-digit result files, pairs or refuses as
its exact value does, so main and #1312 agree.

Owner rule (#1166), applied to these sites by the owner's 2026-11-12
decision: exact, never round, except the final HALF_UP to the filed whole
rupee or two-place rate. Newly passing cases tested here are those the issue
lists: an equal-and-opposite withholding pair needing more than 28 digits now
pairs; in-limit income whose Schedule OS conversion or OS/FSI sum needs more
than 28 digits now files; and a ₹1 flip at Schedule FSI's portal-relief check,
either way. A reversal that equals its original only at 28 digits now refuses.

Every statement is synthetic: the FY 2025-26 withholding fixture with its
dividend and tax edited, the Schedule FA statement of
:mod:`tests._ibkr_fa_sums_inputs`, or the StockPlan exports of
:mod:`tests._stockplan_fa_exports`, all at the vendored SBI TTBR.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from functools import partial
from pathlib import Path
from typing import Final

import openpyxl
import pytest

from ibkr_fa.cli import main
from ibkr_fa.compute.wht_reversal_pairing import UnprovenWhtReversalError
from ibkr_fa.fx import Rule115Policy
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.pipeline_form67 import run_form67
from ibkr_fa.pipeline_fsi import run_schedule_fsi
from ibkr_fa.pipeline_stockplan_fa import run_stockplan_fa
from tests._crash_reports import assert_no_markers, assert_replaced_unchained
from tests._ftc_warnings import PORTAL_RELIEF_REFUSAL as _PORTAL_RELIEF_REFUSAL
from tests._ftc_warnings import fractional_rate_warning
from tests._ibkr_fa_sums_inputs import A2_AMOUNT_COLUMN as _A2_AMOUNT
from tests._ibkr_fa_sums_inputs import A2_NATURE_COLUMN as _A2_NATURE
from tests._ibkr_fa_sums_inputs import A3_GROSS_COLUMN as _A3_GROSS
from tests._ibkr_fa_sums_inputs import IBKR_FA_SUMS_CONFIG as _FA_CONFIG
from tests._ibkr_fa_sums_inputs import XQBB_DIVIDEND as _XQBB_DIVIDEND
from tests._ibkr_fa_sums_inputs import ibkr_fa_sums_statement as _statement
from tests._ibkr_fa_sums_inputs import read_csv_dict_rows as _rows
from tests._ibkr_fa_sums_inputs import run_schedule_fa_cy2025 as _schedule_fa
from tests._ibkr_fa_sums_inputs import write_fa_sums_inputs as _write_inputs
from tests._ibkr_form67_runs import F67_RATE as _F67_RATE
from tests._ibkr_form67_runs import FSI_TAX as _FSI_TAX
from tests._ibkr_form67_runs import TR_RELIEF as _TR_RELIEF
from tests._ibkr_form67_runs import US_TAX_PAYABLE as _TAX_PAYABLE
from tests._ibkr_form67_runs import fsi_other_sources as _fsi_other_sources
from tests._ibkr_form67_runs import one_row as _one_row
from tests._ibkr_form67_runs import run_command as _run
from tests._ibkr_form67_runs import wht_inputs as _wht_inputs
from tests._ibkr_run_all import FY_2025_26_INPUT as _FY_2025_26_INPUT
from tests._ibkr_run_all import run_all_argv as _run_all_argv
from tests._ibkr_user_toml import US_FOREIGN_TIN_TOML as _TIN
from tests._stockplan_fa_exports import STOCKPLAN_PRICE_NOW as _PRICE_NOW
from tests._stockplan_fa_exports import setup_stockplan_exports as stockplan_exports
from tests._stockplan_fa_exports import stub_fetcher
from tests._stockplan_fa_exports import write_stockplan_holdings as _write_holdings

# ---- shared inputs and readers ---------------------------------------------------

_CUSTODIAN: Final = _FA_CONFIG[_FA_CONFIG.index("[account.custodian]") :]

_FSI_INCOME: Final = "Income from outside India (included in PART B-TI)"
_TR_TAX: Final = (
    "Total taxes paid outside India (total of (c) of Schedule FSI in respect of each country)"
)
_F67_INCOME: Final = "Income from outside India"  # column 4
_F67_TAX: Final = "Tax paid outside India - Amount"  # column 5
_F67_SECTION_90: Final = "Credit claimed under section 90/90A - Amount"  # column 11
_F67_TOTAL_CREDIT: Final = "Total foreign tax credit claimed"  # column 13

# Example A's refusal on main, word for word (Example B's on the branch).
_NO_ORIGINAL: Final = (
    "withholding-tax reversal(s) in FY 2025-04-01 → 2026-03-31 cancel tax of an "
    "unproven year: a reversal with no original. A reversal pairs only with an "
    "equal-and-opposite withholding row of the same source account, ISIN and currency "
    "on or before its date. With none, or with candidates in more than one financial "
    "year, the year of the tax it cancels is unknown, so the run refuses rather than "
    "guess. If the statement starts after the original withholding, use one that "
    "covers it."
)


# ---- Schedule FA at the Dec-31 TTBR: Example 1 and StockPlan -------------------

_EXAMPLE_1: Final = "1000031.75924891024924555717"
_EX1_AMOUNTS = pytest.mark.parametrize(
    ("amount", "filed"),
    [
        pytest.param(_EXAMPLE_1, "89472841", id="exact-below-half"),
        pytest.param("1000031.75924891024924555718", "89472842", id="control"),
    ],
)
_EX1_KINDS = pytest.mark.parametrize("kind", ["Dividend", "Interest"])


def _fa_statement(kind: str, amount: str) -> str:
    """Example 1's statement: ``amount`` as one XQBB dividend or one interest credit."""
    if kind == "Dividend":
        return _statement(dividends=[(date(2025, 6, 16), _XQBB_DIVIDEND, amount)])
    return _statement(interest=[(date(2025, 6, 30), "USD Credit Interest for Jun-2025", amount)])


def _assert_fa_files(a3: Path, a2: Path, kind: str, filed: str) -> None:
    """The XQBB A3 row files ``filed`` for a dividend (₹0 for interest), and
    Table A2's one gross-credit row is ``kind`` ``filed``."""
    holding = _one_row(a3)
    assert holding["_audit: Source ticker"] == "XQBB"
    assert holding[_A3_GROSS] == (filed if kind == "Dividend" else "0")
    assert [(row[_A2_NATURE], row[_A2_AMOUNT]) for row in _rows(a2)] == [(kind, filed)]


@_EX1_KINDS
@_EX1_AMOUNTS
def test_schedule_fa_files_the_exact_dec31_conversion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, amount: str, filed: str
) -> None:
    """#1312 Example 1: at the Dec-31 TTBR of 89.47, USD
    1000031.75924891024924555717 is exactly ₹89472841.4999999999999999999999, so
    the XQBB A3 row and A2's Dividend row (as an interest credit, A2's Interest
    row) file ₹89472841. Main's 28-digit 89472841.50000000000000000000
    filed ₹89472842. The control …718 files ₹89472842 both ways."""
    statement, config = _write_inputs(tmp_path, _fa_statement(kind, amount))
    out = tmp_path / "schedule-fa.csv"

    assert _schedule_fa(statement, config, out) == 3

    capsys.readouterr()
    _assert_fa_files(out, tmp_path / "schedule-fa-a2.csv", kind, filed)


@_EX1_KINDS
@_EX1_AMOUNTS
def test_run_all_fa_leg_files_the_exact_dec31_conversion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, amount: str, filed: str
) -> None:
    """#1312: run-all with Example 1's statement as ``--cy-input`` (the FY
    2025-26 fixture as ``--fy-input``, and Example 1's custodian added to the
    config so Table A2 is built) writes the A3 and A2 figures standalone
    schedule-fa does: ₹89472841, main ₹89472842."""
    cy_input = tmp_path / "cy.csv"
    cy_input.write_text(_fa_statement(kind, amount), encoding="utf-8")
    argv = [*_run_all_argv(tmp_path, _FY_2025_26_INPUT), "--cy-input", str(cy_input)]
    with Path(argv[argv.index("--config") + 1]).open("a", encoding="utf-8") as config:
        config.write(_CUSTODIAN)

    assert main(argv) == 3

    capsys.readouterr()
    out = tmp_path / "out"
    _assert_fa_files(out / "schedule-fa.csv", out / "schedule-fa-a2.csv", kind, filed)


@pytest.mark.parametrize(
    ("amount", "filed", "printed"),
    [
        pytest.param(
            "12431.75924891024924555717",
            "1112269",
            "1112269.4999999999999999999999",
            id="exact-below-half",
        ),
        pytest.param(
            "12431.759248910249245557", "1112269", "1112269.49999999999999998479", id="control"
        ),
    ],
)
def test_stockplan_fa_files_and_prints_the_exact_dividend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    amount: str,
    filed: str,
    printed: str,
) -> None:
    """#1312: one 2-share vest at USD 100 (Holdings: 2 shares, USD 200) and
    one Dividend Paid of USD 12431.75924891024924555717 on 2025-12-10, a text
    cell since a float can't hold its digits. At the Dec-31 TTBR of 89.47 it is
    exactly ₹1112269.4999999999999999999999, so ``stockplan-fa`` files ₹1112269
    in the A3 row and prints that INR. Main filed ₹1112270 and printed
    1112269.500000000000000000000. The control's product fits in 28 digits and
    files ₹1112269 both ways. The price fetcher is the offline stub."""
    input_dir, config, out = stockplan_exports(tmp_path, vests=[("2025-08-14", "V1", 100, 2, 2)])
    _write_holdings(input_dir / "Vested Share Holdings.xlsx", shares=Decimal(2), acq=Decimal(200))
    report = input_dir / "IN - Dividend Report.xlsx"
    workbook = openpyxl.load_workbook(report)
    sheet = workbook.active
    assert sheet is not None
    assert [sheet["B3"].value, sheet["B4"].value] == [
        "Dividend Paid",
        "IRS Nonresident Alien Withholding",
    ]
    sheet["C3"] = amount
    sheet.delete_rows(4)  # drop the withholding row: the dividend alone
    workbook.save(report)
    monkeypatch.setattr(
        "ibkr_fa.cli.run_stockplan_fa",
        partial(run_stockplan_fa, price_fetcher=stub_fetcher, price_now=_PRICE_NOW),
    )

    rc = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config)),
            *("--output", str(out), "--calendar-year", "2025"),
        ]
    )

    assert rc == 3
    stdout = capsys.readouterr().out.splitlines()
    assert f"  Gross amount credited during the period (dividends): INR {printed}." in stdout
    assert [row[_A3_GROSS] for row in _rows(out)] == [filed]


# ---- foreign tax and the Form 67 credit ------------------------------------------


@pytest.mark.parametrize(
    "tax",
    [
        pytest.param("-912201.23717217787913340935", id="exact-below-half"),
        pytest.param("-912201.23717217787913340934", id="control"),
    ],
)
def test_foreign_tax_files_the_exact_conversion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tax: str
) -> None:
    """#1312 Example 2: a USD 9122012.37 dividend with USD
    912201.23717217787913340935 of US tax, at the Rule 128 rate of 87.70, is
    exactly ₹80000048.499999999999999999995 of tax, so schedule-fsi's tax paid,
    schedule-tr's total taxes paid and Form 67 column 5 file ₹80000048. Main's
    28-digit 80000048.50000000000000000000 filed ₹80000049 in all
    three. The control …934 (₹80000048.499999999999999999118, 28 digits
    80000048.49999999999999999912) files ₹80000048 both ways."""
    statement, config = _wht_inputs(tmp_path, "9122012.37", tax)
    fsi, tr, form67 = (tmp_path / f"{name}.csv" for name in ("fsi", "tr", "form-67"))

    assert _run("schedule-fsi", statement, config, fsi) == 3
    assert _run("schedule-tr", statement, config, tr) == 3
    assert _run("form-67", statement, config, form67, "review-csv") == 0

    capsys.readouterr()
    assert _fsi_other_sources(fsi)[_FSI_TAX] == "80000048"
    assert _one_row(tr)[_TR_TAX] == "80000048"
    assert _one_row(form67)[_F67_TAX] == "80000048"


@pytest.mark.parametrize(
    "tax",
    [
        pytest.param("-114031.23717217787913340935", id="exact-below-half"),
        pytest.param("-114031.23717217787913340934", id="control"),
    ],
)
def test_credit_files_the_exact_foreign_tax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tax: str
) -> None:
    """#1312 "the credit, not only the tax": a USD 760208.25 dividend with USD
    114031.23717217787913340935 of US tax (15%, within the treaty rate), a US
    TIN and a US DIVIDEND tax payable override of ₹99999999, so the credit is
    filed. The tax is exactly ₹10000539.499999999999999999995, so Form 67
    columns 5, 11 and 13, FSI's tax paid and TR's total taxes paid and total
    relief all file ₹10000539; main filed ₹10000540 in all six, every
    command exiting 0, so no cross-file check caught it. The issue calls this
    Example 3's withholding, but Example 3's is above the treaty rate and base
    refuses it in FSI and TR at the portal-relief check; this is audit round
    7's within-rate example, the nearest shape that reconciles. The control …934
    files ₹10000539 both ways."""
    statement, config = _wht_inputs(tmp_path, "760208.25", tax, config=_TIN + _TAX_PAYABLE)
    fsi, tr, form67 = (tmp_path / f"{name}.csv" for name in ("fsi", "tr", "form-67"))

    assert _run("schedule-fsi", statement, config, fsi) == 0
    assert _run("schedule-tr", statement, config, tr) == 0
    assert _run("form-67", statement, config, form67, "review-csv") == 0

    capsys.readouterr()
    assert _fsi_other_sources(fsi)[_FSI_TAX] == "10000539"
    tr_row = _one_row(tr)
    assert (tr_row[_TR_TAX], tr_row[_TR_RELIEF]) == ("10000539", "10000539")
    entry = _one_row(form67)
    assert (entry[_F67_TAX], entry[_F67_SECTION_90], entry[_F67_TOTAL_CREDIT]) == (
        "10000539",
        "10000539",
        "10000539",
    )


@pytest.mark.parametrize(
    ("dividend", "credit"),
    [
        pytest.param("456124.9486887115165336374", "10000539", id="exact-below-half"),
        pytest.param("456124.9486887115165336375", "10000540", id="control"),
    ],
)
def test_form67_caps_the_credit_at_the_exact_treaty_tax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dividend: str, credit: str
) -> None:
    """#1312 Example 3: USD 136837.49 of US tax (30%) on a USD
    456124.9486887115165336374 dividend, with a US DIVIDEND tax payable
    override of ₹99999999. The gross is ₹40002157.99999999999999999998, so the
    25% treaty cap is exactly ₹10000539.4999999999999999999950 and Form 67
    columns 11 and 13 file ₹10000539; main filed ₹10000540. Column 5,
    the tax paid, stays ₹12000648. The control …375 files ₹10000540 both
    ways."""
    statement, config = _wht_inputs(tmp_path, dividend, "-136837.49", config=_TAX_PAYABLE)
    out = tmp_path / "form-67.csv"

    assert _run("form-67", statement, config, out, "review-csv") == 0

    capsys.readouterr()
    entry = _one_row(out)
    assert (entry[_F67_TAX], entry[_F67_SECTION_90], entry[_F67_TOTAL_CREDIT]) == (
        "12000648",
        credit,
        credit,
    )


@pytest.mark.parametrize(
    ("tax", "rate"),
    [
        pytest.param("-123450000.00000000000000012344", "12.34", id="exact-below-half"),
        pytest.param("-123450000.00000000000000012345", "12.35", id="control"),
    ],
)
def test_form67_files_the_exact_tax_paid_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tax: str, rate: str
) -> None:
    """#1312 Example 4: USD 123450000.00000000000000012344 of tax on a USD
    1000000000.000000000000001 dividend is just below 12.345%, so Form 67
    column 6 files 12.34 and stderr says to enter 12.34 by hand. Main's 28-digit
    quotient was 0.1234500000000000000000000000 and filed
    12.35. The control …12345 is exactly 12.345% and files 12.35 both ways."""
    statement, config = _wht_inputs(tmp_path, "1000000000.000000000000001", tax)
    out = tmp_path / "form-67.csv"

    assert _run("form-67", statement, config, out, "review-csv") == 0

    err = capsys.readouterr().err.splitlines()
    assert _one_row(out)[_F67_RATE] == rate
    assert fractional_rate_warning(rate, "2") in err


# ---- withholding reversals: Examples A and B -------------------------------------


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("123456789.12345678901234567891", id="past-28-digits"),
        pytest.param("1.23", id="control"),
    ],
)
def test_equal_and_opposite_reversal_pairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], amount: str
) -> None:
    """#1312 Example A (newly passing, sanctioned): USD
    123456789.12345678901234567891 of tax and its equal reversal pair, so
    schedule-fsi files tax paid ₹0 and form-67 writes no entry (no foreign
    tax paid), both exiting 0. Main rounded the negated reversal to 28
    digits, found no original and exited 4. The control USD 1.23 pairs both
    ways."""
    statement, config = _wht_inputs(tmp_path, "999999999.99", f"-{amount}", reversal=amount)
    fsi, form67 = tmp_path / "fsi.csv", tmp_path / "form-67.csv"

    assert _run("schedule-fsi", statement, config, fsi) == 0
    assert _run("form-67", statement, config, form67, "review-csv") == 0

    stdout = capsys.readouterr().out.splitlines()
    assert _fsi_other_sources(fsi)[_FSI_TAX] == "0"
    assert _rows(form67) == []
    assert "  Total foreign tax paid (portal-displayed): INR 0" in stdout


# Example B: the reversal is USD 1E-20 more than the tax. Main's unary
# minus rounded it onto the tax and paired them. The marker is a digit run
# contiguous in both cells.
_MARKER: Final = "736250901"
_EXAMPLE_B_TAX: Final = "-736250901.1234567890123456789"
_EXAMPLE_B_REVERSAL: Final = "736250901.12345678901234567891"


def _example_b(tmp_path: Path) -> tuple[Path, Path]:
    statement, config = _wht_inputs(
        tmp_path, "999999999.99", _EXAMPLE_B_TAX, reversal=_EXAMPLE_B_REVERSAL
    )
    text = statement.read_text(encoding="utf-8")
    for cell in (_EXAMPLE_B_TAX, _EXAMPLE_B_REVERSAL):
        assert f",{cell}," in text
    assert text.count(_MARKER) == 2
    return statement, config


@pytest.mark.parametrize(
    ("command", "format_id"), [("schedule-fsi", "filing-csv"), ("form-67", "review-csv")]
)
def test_reversal_equal_only_at_28_digits_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str, format_id: str
) -> None:
    """#1312 Example B (newly refused): exit 4 with Example A's main message
    alone, no output, and no amount (the digit-run marker) printed. Main
    paired the rows and exited 0."""
    statement, config = _example_b(tmp_path)
    out = tmp_path / f"{command}.csv"

    rc = _run(command, statement, config, out, format_id)

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == f"error: {_NO_ORIGINAL}\n"
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, _MARKER)
    assert not out.exists()


@pytest.mark.parametrize("run", [run_schedule_fsi, run_form67], ids=["fsi", "form-67"])
def test_reversal_refusal_keeps_no_amount(tmp_path: Path, run: Callable[..., object]) -> None:
    """#1312 Example B: the pipeline raises the reversal refusal itself, with
    no cause or context and no amount (the marker) in its ``str``, ``repr``,
    fields or traceback. Main raised nothing."""
    statement, config = _example_b(tmp_path)

    with pytest.raises(UnprovenWhtReversalError) as raised:
        run(
            input_path=statement,
            config_path=config,
            output_path=tmp_path / "out.csv",
            financial_year=FinancialYear(2025),
        )

    assert str(raised.value) == _NO_ORIGINAL
    assert_replaced_unchained(raised.value, markers=(_MARKER,))


# ---- Schedule OS and FSI at 28+ digits: item 6 -------------------------------------


def test_schedule_os_fsi_and_form67_file_the_exact_dividend(
    tmp_path: Path,
) -> None:
    """#1312 item 6 (newly passing, sanctioned): Example 1's USD
    1000031.75924891024924555717 as the fixture's dividend (tax USD 0.39). Its
    Schedule OS conversion needs more than 28 digits, so main refused
    every command (exit 4) with the Schedule OS dividend conversion's "must be
    exact within Decimal precision" refusal. Now each runs to the
    fixture's usual exit, and Schedule OS 1a(i), FSI's Other Sources income
    and Form 67 column 4 file the exact product's HALF_UP at the Rule 115
    rate."""
    statement, config = _wht_inputs(tmp_path, _EXAMPLE_1, "-0.39")
    rate = Rule115Policy().rate_for(date(2025, 9, 12))
    with localcontext(Context(prec=1200)):
        income = str((Decimal(_EXAMPLE_1) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    runs = {
        "schedule-os": "filing-csv",
        "schedule-fsi": "filing-csv",
        "schedule-tr": "filing-csv",
        "form-67": "review-csv",
    }

    codes = {
        command: _run(command, statement, config, tmp_path / f"{command}.csv", format_id)
        for command, format_id in runs.items()
    }

    assert codes == {"schedule-os": 0, "schedule-fsi": 3, "schedule-tr": 3, "form-67": 0}
    with (tmp_path / "schedule-os.csv").open(encoding="utf-8-sig", newline="") as handle:
        os_cells = {row[0]: row[1] for row in csv.reader(handle) if row}
    assert os_cells["1ai. Dividend income [other than (ii) and (iii)]"] == income
    assert _fsi_other_sources(tmp_path / "schedule-fsi.csv")[_FSI_INCOME] == income
    assert _one_row(tmp_path / "form-67.csv")[_F67_INCOME] == income
    assert _one_row(tmp_path / "schedule-tr.csv")[_TR_TAX] == "34"


# ---- later check: the portal-relief check follows the exact values, both ways ----


@pytest.mark.parametrize(
    "tax",
    [
        pytest.param("-912201.23717217787913340935", id="exact-below-half"),
        pytest.param("-912201.23717217787913340934", id="control"),
    ],
)
def test_portal_relief_check_passes_on_the_exact_tax(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tax: str
) -> None:
    """#1312 later check, refused before and filed now (sanctioned ₹1 flip):
    Example 2's US tax on a USD 3648804.92 dividend is about USD 0.0072 above
    the 25% treaty tax, with a US TIN and a ₹99999999 tax payable override.
    The tax is exactly ₹80000048.499999999999999999995 and the cap
    ₹80000047.871, both filing ₹80000048, so the relief equals the portal's
    min(tax paid, tax payable): schedule-fsi and schedule-tr exit 0 with tax
    paid and relief ₹80000048, as Form 67 columns 5, 11 and 13 file. Main's
    28-digit tax filed ₹80000049 over the ₹80000048 relief, so both
    refused with the portal-relief message. The control …934 files ₹80000048
    both ways."""
    statement, config = _wht_inputs(tmp_path, "3648804.92", tax, config=_TIN + _TAX_PAYABLE)
    fsi, tr, form67 = (tmp_path / f"{name}.csv" for name in ("fsi", "tr", "form-67"))

    assert _run("schedule-fsi", statement, config, fsi) == 0
    assert _run("schedule-tr", statement, config, tr) == 0
    assert _run("form-67", statement, config, form67, "review-csv") == 0

    capsys.readouterr()
    assert _fsi_other_sources(fsi)[_FSI_TAX] == "80000048"
    tr_row = _one_row(tr)
    assert (tr_row[_TR_TAX], tr_row[_TR_RELIEF]) == ("80000048", "80000048")
    entry = _one_row(form67)
    assert (entry[_F67_TAX], entry[_F67_SECTION_90], entry[_F67_TOTAL_CREDIT]) == (
        "80000048",
        "80000048",
        "80000048",
    )


@pytest.mark.parametrize(
    ("dividend", "credit", "refused"),
    [
        pytest.param("456124.9486887115165336374", "10000539", True, id="exact-below-half"),
        pytest.param("456124.9486887115165336375", "10000540", False, id="control"),
    ],
)
def test_portal_relief_check_refuses_on_the_exact_cap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dividend: str,
    credit: str,
    refused: bool,
) -> None:
    """#1312 later check, filed before and refused now (sanctioned ₹1 flip):
    USD 114031.2425 of US tax on Example 3's dividend is USD 0.0053 above the
    25% treaty tax, with a US TIN and a ₹99999999 tax payable override. The
    tax files ₹10000540 and the exact cap ₹10000539.4999999999999999999950
    files ₹10000539, so the relief no longer equals the portal's min(tax paid,
    tax payable): schedule-fsi and schedule-tr refuse with the portal-relief
    message, and form-67 files the ₹10000539 credit. Main let it
    through with a ₹1-too-high cap, ₹10000540. The control …375 caps at
    ₹10000540 and files both ways."""
    statement, config = _wht_inputs(tmp_path, dividend, "-114031.2425", config=_TIN + _TAX_PAYABLE)

    for command in ("schedule-fsi", "schedule-tr"):
        out = tmp_path / f"{command}.csv"
        rc = _run(command, statement, config, out)
        err = capsys.readouterr().err
        if refused:
            assert (rc, err) == (4, f"error: {_PORTAL_RELIEF_REFUSAL}\n"), command
            assert not out.exists()
        else:
            assert rc == 0, command
    assert _run("form-67", statement, config, tmp_path / "form-67.csv", "review-csv") == 0

    capsys.readouterr()
    entry = _one_row(tmp_path / "form-67.csv")
    assert (entry[_F67_TAX], entry[_F67_SECTION_90], entry[_F67_TOTAL_CREDIT]) == (
        "10000540",
        credit,
        credit,
    )
    if not refused:
        assert _one_row(tmp_path / "schedule-tr.csv")[_TR_RELIEF] == credit
