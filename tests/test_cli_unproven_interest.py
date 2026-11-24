"""The commands that use interest refuse an unproven Interest row (#905).

An accrual-labelled row, a positive row without IBKR's credit-interest wording
(#1280), or a negative or zero row that isn't debit interest, may change the
interest received without showing by how much.
``schedule-os``, ``schedule-fsi``, ``schedule-tr``, ``form-67`` and ``run-all``
exit 4, name the row's file line and why it's unproven, and leave no output.
The refusal and the ``schedule-fa`` report never print the row's Description,
amount, date or currency, which are input (#1027, #1047). ``schedule-cg`` and
``schedule-cg-ltcg`` don't use interest and still run. ``schedule-fa`` leaves
Table A2's Interest nature for manual fill: the CSV keeps A3 and the resolved
A2 rows and adds the blank placeholder row; the CBDT JSON fragment refuses.
Debit interest is left out everywhere, so it changes no file.
"""

from __future__ import annotations

import csv
import re
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.pipeline_fa import run_fa_a3
from tests._ibkr_schedule_fa_inputs import SCHEDULE_FA_CONFIG as _FULL_CONFIG
from tests._ibkr_schedule_fa_inputs import flat_close_fetcher as _flat_close_fetcher
from tests._income_blocks import with_totals

_FIXTURES = Path(__file__).parent / "fixtures"
_FY_FIXTURE = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
_CY_FIXTURE = _FIXTURES / "e2e_activity_statement_with_nav_2025.csv"
_FY_CONFIG = """\
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
"""
_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")

_HEADER = "Interest,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"


def _row(day: str, description: str, amount: str, currency: str = "USD") -> str:
    return f"Interest,Data,{currency},{day},{description},{amount},,,,,,,,,,,\n"


_CREDIT = _row("30-06-2025", "USD Credit Interest for Jun-2025", "10.00")
_DEBIT = _row("30-06-2025", "USD Debit Interest for Jun-2025", "-3.00")
_ACCRUAL = "its description mentions an accrual"
_POSITIVE = "a positive amount without IBKR's credit-interest wording"
_NON_CREDIT = "a negative or zero amount that isn't debit interest"
# A neutral marker for a label IBKR's credit-interest wording doesn't have.
_MARKER = "SYNTHQPOST"
# Each unproven row, and the reason the refusal gives for it.
_UNPROVEN = {
    "positive-debit-correction": (
        _row("30-06-2025", "USD Debit Interest correction for Jun-2025", "3.17"),
        _POSITIVE,
    ),
    "positive-unknown-label": (
        _row("30-06-2025", f"USD {_MARKER} for Jun-2025", "3.17"),
        _POSITIVE,
    ),
    "negative-correction": (
        _row("30-06-2025", "USD Credit Interest for Jun-2025", "-4.00"),
        _NON_CREDIT,
    ),
    "positive-accrual": (
        _row("30-06-2025", "Credit Interest with accrual adjustment", "1.23"),
        _ACCRUAL,
    ),
    "debit-interest-accrual": (
        _row("30-06-2025", "USD Debit Interest accrual adjustment", "-2.00"),
        _ACCRUAL,
    ),
}


def _refusal(line: int, reason: str) -> str:
    """The whole refusal naming one unproven row by its line and reason (#1047)."""
    return (
        "the Activity Statement's Interest section has row(s) that may change "
        "the interest you received but don't prove by how much: "
        f"line {line} ({reason}). The run refuses rather than guess the "
        "interest. Only a positive row whose description reads exactly '<CCY> "
        "Credit Interest for <Mon>-<YYYY>' counts as an interest credit, and "
        "only a negative 'Debit Interest' row is left out as margin interest "
        "you paid."
    )


def _assert_no_echo(text: str, row: str) -> None:
    """``text`` prints none of ``row``'s Description, Amount or Date (#1047),
    nor :data:`_MARKER` when the Description holds it (#1280).

    Compared case-insensitively; the date is checked as the cell and in the
    ISO form the old refusal printed.
    """
    _section, _kind, _currency, day, description, amount, *_rest = row.split(",")
    dd, mm, yyyy = day.split("-")
    marker = (_MARKER,) if _MARKER in description else ()
    for value in (description, amount, day, f"{yyyy}-{mm}-{dd}", *marker):
        assert value.lower() not in text.lower(), value


def _fy_inputs(tmp_path: Path, interest: str) -> tuple[Path, Path]:
    """The FY 2025-26 fixture with ``interest`` (header and rows) and its
    ``Total`` row appended."""
    statement = tmp_path / "statement.csv"
    statement.write_text(
        with_totals(_FY_FIXTURE.read_text(encoding="utf-8") + interest), encoding="utf-8"
    )
    config = tmp_path / "user.toml"
    config.write_text(_FY_CONFIG, encoding="utf-8")
    return statement, config


def _unproven_line() -> int:
    """The file line of the unproven row: after the fixture, header and credit."""
    return len(_FY_FIXTURE.read_text(encoding="utf-8").splitlines()) + 3


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


_REFUSING = [
    pytest.param("schedule-os", (), ("schedule-os.csv",), id="schedule-os"),
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
]


@pytest.mark.parametrize("case", sorted(_UNPROVEN))
@pytest.mark.parametrize(("command", "extra", "targets"), _REFUSING)
def test_interest_command_refuses_and_removes_its_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    row, reason = _UNPROVEN[case]
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + row)
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
    assert captured.err == f"error: {_refusal(_unproven_line(), reason)}\n"
    _assert_no_echo(captured.err, row)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("command", ["schedule-cg", "schedule-cg-ltcg"])
@pytest.mark.parametrize("case", sorted(_UNPROVEN))
def test_capital_gains_commands_ignore_interest(tmp_path: Path, case: str, command: str) -> None:
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + _UNPROVEN[case][0])
    output = tmp_path / f"{command}.csv"

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(output)),
            *_FY,
            *_ITR,
        ]
    )

    assert rc == 0
    assert output.exists()


def test_refusal_names_the_unproven_row_in_a_second_interest_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row, reason = _UNPROVEN["negative-correction"]
    # Block 1: header + credit + its Total; block 2: header + the unproven row.
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + _HEADER + row)

    rc = main(
        [
            "schedule-os",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(tmp_path / "schedule-os.csv")),
            *_FY,
        ]
    )

    assert rc == 4
    err = capsys.readouterr().err
    assert err == f"error: {_refusal(_unproven_line() + 2, reason)}\n"
    _assert_no_echo(err, row)
    assert not (tmp_path / "schedule-os.csv").exists()


def test_refusal_prints_no_holder_name_or_account_from_a_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Description carrying a name and an account number is never echoed (#1047).

    ``accrual`` in the Description makes the row unproven; the refusal names
    its line and reason only, not its Description, amount or date.
    """
    row = _row("01-03-2026", "Synthetic Holder Two U1234567 accrual adjustment", "1234567.89")
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + row)

    rc = main(
        [
            "schedule-os",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(tmp_path / "schedule-os.csv")),
            *_FY,
        ]
    )

    assert rc == 4
    err = capsys.readouterr().err
    assert err == f"error: {_refusal(_line_of(statement, row), _ACCRUAL)}\n"
    for marker in ("Synthetic Holder Two", "U1234567", "1234567.89", "2026-03-01"):
        assert marker.lower() not in err.lower(), marker
    assert not (tmp_path / "schedule-os.csv").exists()


# An accrual row in another currency; the refusal and the report name it
# without its currency (#1027) and only by its line and reason (#1047).
_ACCRUAL_DESCRIPTION = "Credit Interest with accrual adjustment"


@pytest.mark.parametrize("currency", ["ANN", "EUR"])
def test_refusal_names_a_non_usd_unproven_row_without_its_currency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], currency: str
) -> None:
    row = _row("30-06-2025", _ACCRUAL_DESCRIPTION, "1.23", currency)
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + row)

    rc = main(
        [
            "schedule-os",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(tmp_path / "schedule-os.csv")),
            *_FY,
        ]
    )

    assert rc == 4
    err = capsys.readouterr().err
    assert err == f"error: {_refusal(_line_of(statement, row), _ACCRUAL)}\n"
    # A whole token: fixed text such as "cannot" holds the letters of ANN.
    assert re.search(rf"(?i)\b{currency}\b", err) is None
    _assert_no_echo(err, row)


_RUN_ALL_OUTPUTS = (
    "schedule-os.csv",
    "schedule-cg.csv",
    "schedule-cg-ltcg.csv",
    "schedule-fsi.csv",
    "schedule-tr.csv",
    "form-67.csv",
    "form-67-debug.csv",
)


@pytest.mark.parametrize(
    "case", ["positive-accrual", "positive-debit-correction", "positive-unknown-label"]
)
def test_run_all_refuses_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    row, reason = _UNPROVEN[case]
    statement, config = _fy_inputs(tmp_path, _HEADER + _CREDIT + row)
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
    err = capsys.readouterr().err
    assert err == f"error: run-all: {_refusal(_unproven_line(), reason)}\n"
    _assert_no_echo(err, row)
    assert list(out.iterdir()) == []


_FY_COMMANDS = [
    pytest.param("schedule-os", (), ("schedule-os.csv",), id="schedule-os"),
    pytest.param("schedule-cg", _ITR, ("schedule-cg.csv",), id="schedule-cg"),
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
]


def _fy_run(
    tmp_path: Path,
    name: str,
    interest: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> tuple[int, dict[str, bytes]]:
    """Run ``command`` on the fixture plus ``interest``; return its exit and files."""
    run_dir = tmp_path / name
    run_dir.mkdir()
    statement, config = _fy_inputs(run_dir, interest)
    out = run_dir / "out"
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
    return rc, {path.name: path.read_bytes() for path in sorted(out.iterdir())}


@pytest.mark.parametrize(
    ("without", "with_debit"),
    [("", _HEADER + _DEBIT), (_HEADER + _CREDIT, _HEADER + _CREDIT + _DEBIT)],
    ids=["debit-alone", "credit-and-debit"],
)
@pytest.mark.parametrize(("command", "extra", "targets"), _FY_COMMANDS)
def test_debit_interest_changes_no_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    without: str,
    with_debit: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    """Debit interest is margin interest paid, not income: no refusal, and the
    same files as the statement without it."""
    baseline = _fy_run(tmp_path, "baseline", without, command, extra, targets)
    capsys.readouterr()
    assert baseline[0] in (0, 3)
    assert baseline[1]
    assert _fy_run(tmp_path, "debit", with_debit, command, extra, targets) == baseline
    assert "error" not in capsys.readouterr().err


# ---- schedule-fa (CY 2025, Table A2) ----------------------------------------

_CY_DIVIDENDS = (
    "Dividends,Header,Currency,Date,Description,Amount,,,,,,,,,,,\n"
    "Dividends,Data,USD,16-03-2025,"
    "XQBB(US99995B1044) Cash Dividend USD 0.50 per Share "
    "(Ordinary Dividend),5.00,,,,,,,,,,,\n"
)
_ENDING_VALUE = "Change in NAV,Data,Ending Value,2107.60\n"
_CHANGE_IN_NAV = "Change in NAV,Header,Field Name,Field Value\n" + _ENDING_VALUE


def _fa_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    interest: str,
    output_name: str,
    *flags: str,
    change_in_nav: bool = True,
) -> tuple[int, Path, Path]:
    """Run ``schedule-fa`` on the NAV fixture plus a dividend and ``interest``.

    Its ``Change in NAV`` prints the dividend's and every ``interest`` row's
    total, as IBKR does, for ``schedule-fa`` to check (#1324);
    ``change_in_nav=False`` leaves the section out instead.

    Returns the exit code, the statement and the run's output folder, which
    holds ``output_name``.
    """
    monkeypatch.setattr(
        "ibkr_fa.cli.run_fa_a3",
        partial(
            run_fa_a3,
            price_fetcher=_flat_close_fetcher("100"),
            price_now=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    )
    run_dir = tmp_path / name
    run_dir.mkdir()
    text = _CY_FIXTURE.read_text(encoding="utf-8")
    assert text.count("Open Positions,Header") == 1
    assert text.count(_CHANGE_IN_NAV) == 1
    if change_in_nav:
        interest_total = sum(Decimal(row.split(",")[5]) for row in interest.splitlines()[1:])
        text = text.replace(
            _ENDING_VALUE,
            "Change in NAV,Data,Dividends,5.00\n"
            f"Change in NAV,Data,Interest,{interest_total}\n{_ENDING_VALUE}",
        )
    else:
        text = text.replace(_CHANGE_IN_NAV, "")
    statement = run_dir / "statement.csv"
    statement.write_text(
        with_totals(
            text.replace(
                "Open Positions,Header", _CY_DIVIDENDS + interest + "Open Positions,Header"
            )
        ),
        encoding="utf-8",
    )
    config = run_dir / "user.toml"
    config.write_text(_FULL_CONFIG, encoding="utf-8")
    out = run_dir / "out"
    out.mkdir()
    rc = main(
        [
            "schedule-fa",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / output_name)),
            *("--calendar-year", "2025"),
            *flags,
        ]
    )
    return rc, statement, out


def _line_of(statement: Path, row: str) -> int:
    lines = statement.read_text(encoding="utf-8").splitlines()
    assert lines.count(row.rstrip("\n")) == 1
    return lines.index(row.rstrip("\n")) + 1


def _a2_natures(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header, *rows = csv.reader(handle)
    column = header.index(
        "Gross amount paid/credited to the account during the period - Nature of amount"
    )
    return [row[column] for row in rows]


@pytest.mark.parametrize("case", sorted(_UNPROVEN))
def test_schedule_fa_csv_leaves_a2_interest_for_manual_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    row, reason = _UNPROVEN[case]
    baseline_rc, _statement, baseline_out = _fa_main(
        tmp_path, monkeypatch, "baseline", _HEADER + _CREDIT, "schedule-fa.csv"
    )
    baseline_report = capsys.readouterr().out
    rc, statement, out = _fa_main(
        tmp_path, monkeypatch, "unproven", _HEADER + _CREDIT + row, "schedule-fa.csv"
    )
    report = capsys.readouterr().out

    # A2's peak is always manual-fill on the CSV path, so both exit 3.
    assert (baseline_rc, rc) == (3, 3)
    # A3 unchanged.
    assert (out / "schedule-fa.csv").read_bytes() == (baseline_out / "schedule-fa.csv").read_bytes()
    assert _a2_natures(baseline_out / "schedule-fa-a2.csv") == ["Interest", "Dividend"]
    assert _a2_natures(out / "schedule-fa-a2.csv") == ["Dividend", ""]
    gross = "Gross amount paid/credited to the account during the period"
    assert gross not in baseline_report
    assert gross in report
    assert "unproven" not in baseline_report
    # The report names the row by its line and reason only (#1047).
    assert f"     - line {_line_of(statement, row)} ({reason})\n" in report
    _assert_no_echo(report, row)


@pytest.mark.parametrize("currency", ["ANN", "EUR"])
def test_schedule_fa_report_names_a_non_usd_unproven_row_without_its_currency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    currency: str,
) -> None:
    row = _row("30-06-2025", _ACCRUAL_DESCRIPTION, "1.23", currency)
    # A statement that prints Change in NAV refuses a row outside its base
    # currency (#1324), so this one leaves the section out.
    rc, statement, _out = _fa_main(
        tmp_path,
        monkeypatch,
        "unproven",
        _HEADER + _CREDIT + row,
        "schedule-fa.csv",
        change_in_nav=False,
    )
    report = capsys.readouterr().out

    assert rc == 3
    assert f"     - line {_line_of(statement, row)} ({_ACCRUAL})\n" in report
    # A whole token: fixed text such as "cannot" holds the letters of ANN.
    assert re.search(rf"(?i)\b{currency}\b", report) is None
    _assert_no_echo(report, row)


@pytest.mark.parametrize("case", sorted(_UNPROVEN))
def test_schedule_fa_cbdt_json_refuses_with_no_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    flags = ("schedule-fa.json", "--format", "cbdt-json-fragment", *_ITR)
    baseline_rc, _statement, baseline_out = _fa_main(
        tmp_path, monkeypatch, "baseline", _HEADER + _CREDIT, *flags
    )
    capsys.readouterr()
    rc, _statement, out = _fa_main(
        tmp_path, monkeypatch, "unproven", _HEADER + _CREDIT + _UNPROVEN[case][0], *flags
    )

    assert baseline_rc == 0
    assert (baseline_out / "schedule-fa.json").exists()
    assert rc == 3
    assert "A2 GrossAmtPaidCredited / NatureOfAmount" in capsys.readouterr().err
    assert list(out.iterdir()) == []
