"""Refuse conflicting stated countries after the mapped Indian-ADR skip (#1282).

A Withholding Tax description ending ``" - XX Tax"`` names the country that
took the tax. Foreign tax is filed and credited under the ISIN's country, so a
row naming another country would be credited as the wrong country's tax.
The check follows the mapped Indian-ADR skip under ``underlying-issuer`` routing.
``schedule-fsi``, ``schedule-tr``, ``form-67`` and ``run-all`` now exit 4 with
fixed text naming the row's statement line and leave no output, for a paid row
and for a reversal. A suffix naming the ISIN's country, and a row without one,
file as before. Every symbol, ISIN, date and amount here is synthetic, and no
refusal prints one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._foreign_tax_refusals import stated_country_message
from tests._income_blocks import with_totals

_FY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
)
_CONFIG = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true

[account.foreign_tins]
US = "123456789"

[fx]
max_carry_forward_days = 4

[[form67.tax_payable_overrides]]
country = "US"
source = "DIVIDEND"
tax_payable_normal_inr = "10000"
"""
_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")

_SYMBOL = "XQTC"
_ISIN = "US99995T1028"
_PAID_DATE = "12-09-2025"
_REVERSAL_DATE = "01-10-2025"
_CASH = f"{_SYMBOL}({_ISIN}) Cash Dividend USD 0.913 per Share"
_DIVIDEND_ROW = f"Dividends,Data,USD,{_PAID_DATE},{_CASH} (Ordinary Dividend),9.13\n"
_MARKERS = (_SYMBOL, _ISIN, "1.37", "9.13", _PAID_DATE)


def _wht_row(day: str, amount: str, suffix: str) -> str:
    code = "" if amount.startswith("-") else "Re"
    return f"Withholding Tax,Data,USD,{day},{_CASH}{suffix},{amount},{code}\n"


# Each case's withholding rows, and the statement line of the one whose
# stated country differs from the ISIN's (None when every row is consistent).
_CASES = {
    "paid-nl": ((_wht_row(_PAID_DATE, "-1.37", " - NL Tax"),), 24),
    "reversal-nl": (
        (
            _wht_row(_PAID_DATE, "-1.37", " - US Tax"),
            _wht_row(_REVERSAL_DATE, "1.37", " - NL Tax"),
        ),
        25,
    ),
    "paid-us": ((_wht_row(_PAID_DATE, "-1.37", " - US Tax"),), None),
    "reversal-us": (
        (
            _wht_row(_PAID_DATE, "-1.37", " - US Tax"),
            _wht_row(_REVERSAL_DATE, "1.37", " - US Tax"),
        ),
        None,
    ),
    "paid-no-suffix": ((_wht_row(_PAID_DATE, "-1.37", ""),), None),
}
_REFUSED = sorted(case for case, (_rows, line) in _CASES.items() if line is not None)
_FILED = sorted(case for case, (_rows, line) in _CASES.items() if line is None)


def _write(tmp_path: Path, case: str) -> tuple[Path, Path]:
    """The FY 2025-26 fixture with this file's dividend and ``case``'s
    withholding rows in place of its own income sections."""
    lines = _FY_FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("Dividends,", "Withholding Tax,"))]
    assert len(kept) < len(lines)
    rows, line = _CASES[case]
    text = with_totals(
        "".join(kept)
        + "Dividends,Header,Currency,Date,Description,Amount\n"
        + _DIVIDEND_ROW
        + "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n"
        + "".join(rows)
    )
    if line is not None:
        assert " - NL Tax" in text.splitlines()[line - 1]
    statement = tmp_path / "statement.csv"
    statement.write_text(text, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


def _assert_no_marker(err: str, statement: Path) -> None:
    """``err`` prints no synthetic value of the input, nor either country."""
    input_text = statement.read_text(encoding="utf-8")
    markers = (*_MARKERS, "US", "NL")
    if _REVERSAL_DATE in input_text:
        markers += (_REVERSAL_DATE,)
    for marker in markers:
        assert marker in input_text, marker
        assert marker not in err, marker


_STANDALONE = [
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
    pytest.param("form-67", ("--format", "review-csv"), ("form-67.csv",), id="form-67-review-csv"),
]


@pytest.mark.parametrize("case", _REFUSED)
@pytest.mark.parametrize(("command", "extra", "targets"), _STANDALONE)
def test_standalone_command_refuses_and_removes_its_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    statement, config = _write(tmp_path, case)
    out = tmp_path / "out"
    _stale(out, targets)

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / targets[0])),
            *_FY,
            *extra,
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    line = _CASES[case][1]
    assert line is not None
    assert captured.err == f"error: {stated_country_message(line)}\n"
    _assert_no_marker(captured.err, statement)
    assert list(out.iterdir()) == []


_RUN_ALL_OUTPUTS = (
    "schedule-os.csv",
    "schedule-cg.csv",
    "schedule-cg-ltcg.csv",
    "schedule-fsi.csv",
    "schedule-tr.csv",
    "form-67.csv",
    "form-67-debug.csv",
)


@pytest.mark.parametrize("case", _REFUSED)
def test_run_all_refuses_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    statement, config = _write(tmp_path, case)
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main(
        [
            "run-all",
            *("--fy-input", str(statement), "--config", str(config)),
            *("--output-dir", str(out)),
            *_FY,
            *_ITR,
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err
    line = _CASES[case][1]
    assert line is not None
    assert err == f"error: run-all: {stated_country_message(line)}\n"
    _assert_no_marker(err, statement)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("case", _FILED)
@pytest.mark.parametrize(("command", "extra", "targets"), _STANDALONE)
def test_consistent_or_absent_country_files_as_before(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    statement, config = _write(tmp_path, case)
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / targets[0])),
            *_FY,
            *extra,
        ]
    )

    # #1447: a paid case's upload form-67.csv files 1.37 on 9.13 as a
    # fractional 15.01%, which the filer enters by hand after upload.
    fractional_upload = command == "form-67" and not extra and case.startswith("paid-")
    assert rc == (3 if fractional_upload else 0), capsys.readouterr().err
    assert [path.name for path in out.iterdir()] == [targets[0]]


def test_schedule_os_is_unchanged_by_stated_country(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``schedule-os`` writes the same bytes whatever country a row states."""
    written = []
    for case in ("paid-us", "paid-nl"):
        run_dir = tmp_path / case
        run_dir.mkdir()
        statement, config = _write(run_dir, case)
        output = run_dir / "schedule-os.csv"
        rc = main(
            [
                "schedule-os",
                *("--input", str(statement), "--config", str(config)),
                *("--output", str(output)),
                *_FY,
            ]
        )
        assert rc == 0, capsys.readouterr().err
        written.append(output.read_bytes())
    assert written[0] == written[1]
