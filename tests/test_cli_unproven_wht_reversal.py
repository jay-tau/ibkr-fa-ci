"""FTC commands refuse a current-FY WHT reversal of an unproven year (#893).

A refund cancels the tax of the year its original was withheld in. With no
equal-and-opposite original, or with candidates in more than one financial
year, ``schedule-fsi``, ``schedule-tr``, ``form-67`` and ``run-all`` exit 4 and
leave no output. The refusal says which of the two it is, never the
reversal's ISIN, amount, currency or dates, which are input (#1047). A refund
whose only original is in a prior FY is still excluded from this FY, with the
amendment advisory.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from tests._foreign_tax_refusals import unproven_reversal_message
from tests._income_blocks import with_totals

_FIXTURE = (
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
"""
_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")


def _wht(day: str, amount: str) -> str:
    code = "" if amount.startswith("-") else "Re"
    return (
        f"Withholding Tax,Data,USD,{day},XQBA(US99995C1027) Cash Dividend USD 0.26 "
        f"per Share - US Tax,{amount},{code},,,,,,,,,,\n"
    )


# The fixture's one withholding row, on its dividend's date.
_PAID = _wht("12-09-2025", "-0.39")
# The fixture's withholding rows: that row and its Total.
_FIXTURE_ROWS = _PAID + "Withholding Tax,Data,Total,,,-0.39,,,,,,,,,,,\n"
# The fixture's FY 2025-26 Period, and one that also holds the FY 2024-25
# originals some cases list: every row must fall inside it (#1143).
_FY_PERIOD = 'Statement,Data,Period,"April 1, 2025 - March 31, 2026"'
_WIDE_PERIOD = 'Statement,Data,Period,"January 1, 2025 - March 31, 2026"'
_ROWS = {
    "candidates-in-two-fys": _wht("12-03-2025", "-0.39") + _PAID + _wht("01-10-2025", "0.39"),
    "refund-without-original": _PAID + _wht("01-10-2025", "0.60"),
    "prior-fy-original": _wht("12-03-2025", "-0.39") + _wht("15-04-2025", "0.39") + _PAID,
}
# Each refused case: how the refusal names its reversal, and the input values
# the old refusal printed for it (ISIN, amount, dates), which it never prints
# now (#1047).
_REFUSALS = {
    "candidates-in-two-fys": (
        "a reversal with candidate originals in more than one financial year",
        ("US99995C1027", "0.39", "2025-10-01", "2025-03-12", "2025-09-12"),
    ),
    "refund-without-original": (
        "a reversal with no original",
        ("US99995C1027", "0.60", "2025-10-01"),
    ),
}


def _assert_no_echo(err: str, values: tuple[str, ...]) -> None:
    """``err`` prints none of ``values`` nor the Currency cell (#1047)."""
    for value in values:
        assert value.lower() not in err.lower(), value
    assert re.search(r"(?i)\bUSD\b", err) is None


def _inputs(tmp_path: Path, case: str) -> tuple[Path, Path]:
    """The FY 2025-26 fixture with ``case``'s withholding rows and their Total,
    its Period widened back to January 1, 2025."""
    text = _FIXTURE.read_text(encoding="utf-8")
    assert text.count(_FIXTURE_ROWS) == 1
    assert text.count(_FY_PERIOD) == 1
    text = text.replace(_FY_PERIOD, _WIDE_PERIOD)
    statement = tmp_path / "statement.csv"
    statement.write_text(with_totals(text.replace(_FIXTURE_ROWS, _ROWS[case])), encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


_STANDALONE = [
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
    pytest.param("form-67", ("--format", "review-csv"), ("form-67.csv",), id="form-67-review-csv"),
]


@pytest.mark.parametrize("case", sorted(_REFUSALS))
@pytest.mark.parametrize(("command", "extra", "targets"), _STANDALONE)
def test_standalone_command_refuses_and_removes_its_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    statement, config = _inputs(tmp_path, case)
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
    named, values = _REFUSALS[case]
    assert captured.err == f"error: {unproven_reversal_message(named)}\n"
    _assert_no_echo(captured.err, values)
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


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_run_all_refuses_and_removes_every_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    statement, config = _inputs(tmp_path, case)
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
    named, values = _REFUSALS[case]
    assert err == f"error: run-all: {unproven_reversal_message(named)}\n"
    _assert_no_echo(err, values)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    ("command", "extra"), [("schedule-fsi", ()), ("schedule-tr", _ITR), ("form-67", ())]
)
def test_prior_fy_reversal_is_excluded_with_its_advisory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    extra: tuple[str, ...],
) -> None:
    statement, config = _inputs(tmp_path, "prior-fy-original")

    rc = main(
        [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(tmp_path / f"{command}.csv")),
            *_FY,
            *extra,
        ]
    )

    assert rc == 3  # the dividend's Indian tax payable is blank (#1328)
    assert (
        "ℹ️  Excluded 1 cross-FY WHT reversal(s) totalling 0.39 USD from per-country "  # noqa: RUF001
        "foreign-tax totals — these reverse WHT claimed in a prior FY"
    ) in capsys.readouterr().err
