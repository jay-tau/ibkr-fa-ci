"""IBKR's missing Indian tax warning gives each blank Form 67 row's advice (#1474).

``form-67``, ``schedule-fsi``, ``schedule-tr`` and ``run-all`` run their real
pipelines on the FY 2025-26 end-to-end statement, with its one security's ISIN
set per case and its withholding's ``- XX Tax`` suffix to match. The warning
offers an exact [[form67.tax_payable_overrides]] amount only for a row one
clears, as #1469 made the other brokers' warning do: a no-DTAA row's advice
names the [form67.section_91] inputs too, and a row no override clears gets its
gap instead. A run whose only blank row is a DTAA row an override alone clears
prints today's text. Following the no-DTAA advice files a section 91 credit, and
an override for the row the advice says it won't clear refuses.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._ftc_warnings import CH_GAP as _CH_GAP
from tests._ftc_warnings import (
    KY_SECTION_91_NEED,
    MISSING_INDIAN_TAX_WARNING,
    override_sentence,
    wont_clear,
)
from tests._ftc_warnings import blank_row_warning as _warning
from tests._ibkr_form67_runs import CH_ISIN as _CH
from tests._ibkr_form67_runs import CH_OVERRIDE as _CH_OVERRIDE
from tests._ibkr_form67_runs import FIXTURE_ISIN as _FIXTURE_ISIN
from tests._ibkr_form67_runs import KY_ISIN as _KY
from tests._ibkr_form67_runs import KY_OVERRIDE as _KY_OVERRIDE
from tests._ibkr_form67_runs import SECTION_91_INPUTS as _SECTION_91
from tests._ibkr_form67_runs import config_with as _config
from tests._ibkr_form67_runs import ibkr_output_dir as _output_dir
from tests._ibkr_form67_runs import missing_tax_warnings as _warnings
from tests._ibkr_form67_runs import statement_with_isin as _statement

# Per case: the security's ISIN and the run's one missing Indian tax warning.
_CASES = {
    "clearable": (_FIXTURE_ISIN, MISSING_INDIAN_TAX_WARNING),
    "no-dtaa": (_KY, _warning(override_sentence("KY"), KY_SECTION_91_NEED)),
    "unvendored": (_CH, _warning(wont_clear(_CH_GAP, "CH/DIVIDEND"))),
}


# Each command's argv for (statement, config, output dir).
_Argv = Callable[[Path, Path, Path], list[str]]


def _schedule(name: str, *extra: str) -> _Argv:
    def argv(statement: Path, config: Path, out: Path) -> list[str]:
        return [
            name,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / f"{name}.csv"), "--financial-year", "2025-26", *extra),
        ]

    return argv


def _run_all(statement: Path, config: Path, out: Path) -> list[str]:
    return [
        "run-all",
        *("--fy-input", str(statement), "--config", str(config), "--output-dir", str(out)),
        *("--financial-year", "2025-26", "--itr-form", "ITR-2"),
    ]


_FORM67 = _schedule("form-67")
_COMMANDS = pytest.mark.parametrize(
    "argv",
    [
        pytest.param(_FORM67, id="form-67"),
        pytest.param(_schedule("schedule-fsi"), id="schedule-fsi"),
        pytest.param(_schedule("schedule-tr", "--itr-form", "ITR-2"), id="schedule-tr"),
        pytest.param(_run_all, id="run-all"),
    ],
)
# The commands that write form-67.csv.
_WRITES_FORM67 = pytest.mark.parametrize(
    "argv", [pytest.param(_FORM67, id="form-67"), pytest.param(_run_all, id="run-all")]
)


@_COMMANDS
@pytest.mark.parametrize("case", list(_CASES))
def test_blank_row_advice_offers_an_override_only_where_one_clears(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: _Argv, case: str
) -> None:
    """The run prints the warning once, with the case's per-row advice. run-all
    prints it once although its Schedule FSI, TR and Form 67 reports all carry
    that advice. The clearable case's line is today's text, byte for byte."""
    isin, warning = _CASES[case]

    rc = main(argv(_statement(tmp_path, isin), _config(tmp_path), _output_dir(tmp_path)))

    assert rc == 3
    assert _warnings(capsys) == [warning]


_KY_FOLLOWED = _KY_OVERRIDE + _SECTION_91


@_COMMANDS
def test_following_the_no_dtaa_advice_leaves_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: _Argv
) -> None:
    """With exactly the KY override and both [form67.section_91] inputs the
    advice names, the run no longer prints the warning."""
    rc = main(
        argv(_statement(tmp_path, _KY), _config(tmp_path, _KY_FOLLOWED), _output_dir(tmp_path))
    )

    assert rc != 4
    assert _warnings(capsys) == []


@_WRITES_FORM67
def test_following_the_no_dtaa_advice_files_a_section_91_credit(
    tmp_path: Path, argv: _Argv
) -> None:
    """Form 67 files KY's section 91 credit: the lower of the 34.203 withheld
    and 30% of the 228.02 dividend, rounded to whole rupees."""
    out = _output_dir(tmp_path)

    main(argv(_statement(tmp_path, _KY), _config(tmp_path, _KY_FOLLOWED), out))

    with (out / "form-67.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[1:] == [
        ["1", "1345", "", "7", "", "228", "34", "15.00", "3000", "", "", "", "", "34", "34"]
    ]


@_WRITES_FORM67
def test_the_override_the_advice_says_wont_clear_a_row_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: _Argv
) -> None:
    """As before #1474: with a CH override, Form 67 still can't compute CH's
    credit, so the run refuses naming the gap the CH advice gave."""
    out = _output_dir(tmp_path)

    rc = main(argv(_statement(tmp_path, _CH), _config(tmp_path, _CH_OVERRIDE), out))

    assert rc == 4
    assert f"Form 67 cannot be produced: {_CH_GAP}" in capsys.readouterr().err
    assert list(out.iterdir()) == []
