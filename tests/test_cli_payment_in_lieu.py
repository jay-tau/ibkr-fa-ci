"""An in-FY IBKR payment in lieu of a dividend refuses every income command (#1281).

A ``Payment In Lieu Of Dividend`` row is other income, not a dividend, and
ibkr-fa can't yet file an IBKR one as other income with its withholding.
Schedule OS used to leave it out of filed income figures. When its
withholding shared an ordinary dividend's payment key, it was credited
against that dividend. ``schedule-os``, ``schedule-fsi``, ``schedule-tr``, ``form-67``
and ``run-all`` now exit 4 with fixed text naming the row index and leave no
output. ``schedule-cg`` and Schedule FA Table A3 still don't read the row's
kind, and out-of-FY rows are skipped as before. Every symbol, ISIN, date and
amount here is synthetic, and no refusal prints one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.cli import main
from ibkr_fa.compute import aggregate_dividends_by_symbol_inr
from ibkr_fa.compute.schedule_os import PaymentInLieuOfDividendError
from ibkr_fa.models import DistributionKind, Dividend
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_os import run_schedule_os
from ibkr_fa.types import Isin, Ticker
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

_SYMBOL = "XQPL"
_ISIN = "US99995Q1021"
_PREFIX = f"{_SYMBOL}({_ISIN})"
_DATE = "12-09-2025"
_CASH = f"{_PREFIX} Cash Dividend USD 1.00 per Share (Ordinary Dividend)"
_IN_LIEU = f"{_PREFIX} Payment In Lieu Of Dividend USD 0.37 per Share"
_CASH_ROW = f"Dividends,Data,USD,{_DATE},{_CASH},10.00\n"
_IN_LIEU_ROW = f"Dividends,Data,USD,{_DATE},{_IN_LIEU},3.70\n"
_CASH_WHT_ROW = f"Withholding Tax,Data,USD,{_DATE},{_CASH} - US Tax,-1.50,\n"
_IN_LIEU_WHT_ROW = f"Withholding Tax,Data,USD,{_DATE},{_IN_LIEU} - US Tax,-0.92,\n"
_MARKERS = (_SYMBOL, _ISIN, "0.37", "3.70", _DATE)


def _message(row_index: int) -> str:
    return (
        "Payment In Lieu Of Dividend in the Dividends section (Dividends-section"
        f" row index {row_index}; 0-based, so data row {row_index + 1}): ibkr-fa"
        " can't yet file an IBKR payment in lieu of a dividend as other income"
        " with its withholding. Leaving it out would understate your income and"
        " could credit any withholding against ordinary dividends, so this run"
        " stops. Locate the row in your IBKR Activity Statement CSV by its"
        " Dividends-section row index."
    )


def _base(period: str | None = None) -> str:
    """The FY 2025-26 fixture (Statement, Account Information, Trades) without
    its income sections, optionally with another ``Period``."""
    lines = _FY_FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("Dividends,", "Withholding Tax,"))]
    assert len(kept) < len(lines)
    text = "".join(kept)
    if period is not None:
        old = 'Statement,Data,Period,"April 1, 2025 - March 31, 2026"'
        assert text.count(old) == 1
        text = text.replace(old, f'Statement,Data,Period,"{period}"')
    return text


def _statement(dividends: tuple[str, ...], withholding: tuple[str, ...], base: str) -> str:
    text = base
    if dividends:
        text += "Dividends,Header,Currency,Date,Description,Amount\n" + "".join(dividends)
    if withholding:
        text += "Withholding Tax,Header,Currency,Date,Description,Amount,Code\n" + "".join(
            withholding
        )
    return with_totals(text)


def _write(
    run_dir: Path,
    dividends: tuple[str, ...],
    withholding: tuple[str, ...],
    base: str | None = None,
) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    statement = run_dir / "statement.csv"
    statement.write_text(
        _statement(dividends, withholding, _base() if base is None else base), encoding="utf-8"
    )
    config = run_dir / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    return statement, config


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


def _assert_no_marker(text: str, statement: Path) -> None:
    input_text = statement.read_text(encoding="utf-8")
    markers = _MARKERS
    if "Withholding Tax,Data," in input_text:
        markers += ("0.92",)
    for marker in markers:
        assert marker in input_text, marker
        assert marker not in text, marker
    # Alternate renderings of the verified gross amount and payment date.
    for marker in ("3.7", "2025-09-12", "12/09/2025"):
        assert marker not in text, marker


# ---- the refusal ---------------------------------------------------------------

# (dividend rows, withholding rows, the payment in lieu's row index)
_REFUSED = {
    "issue-repro": ((_CASH_ROW, _IN_LIEU_ROW), (_CASH_WHT_ROW, _IN_LIEU_WHT_ROW), 1),
    "in-lieu-alone": ((_IN_LIEU_ROW,), (_IN_LIEU_WHT_ROW,), 0),
    "no-withholding": ((_CASH_ROW, _IN_LIEU_ROW), (), 1),
}

_REFUSING = [
    pytest.param("schedule-os", ("--format", "filing-csv"), ("schedule-os.csv",), id="schedule-os"),
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
]


@pytest.mark.parametrize("case", sorted(_REFUSED))
@pytest.mark.parametrize(("command", "extra", "targets"), _REFUSING)
def test_income_command_refuses_an_in_fy_payment_in_lieu(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
) -> None:
    dividends, withholding, row_index = _REFUSED[case]
    statement, config = _write(tmp_path, dividends, withholding)
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
    assert captured.err == f"error: {_message(row_index)}\n"
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


def _run_all_argv(statement: Path, config: Path, out: Path) -> list[str]:
    return [
        "run-all",
        *("--fy-input", str(statement), "--config", str(config)),
        *("--output-dir", str(out)),
        *_FY,
        *_ITR,
    ]


@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_run_all_refuses_an_in_fy_payment_in_lieu(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    dividends, withholding, row_index = _REFUSED[case]
    statement, config = _write(tmp_path, dividends, withholding)
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main(_run_all_argv(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: run-all: {_message(row_index)}\n"
    _assert_no_marker(captured.err, statement)
    assert list(out.iterdir()) == []


def test_the_refusal_holds_no_row_content(tmp_path: Path) -> None:
    dividends, withholding, row_index = _REFUSED["issue-repro"]
    statement, config = _write(tmp_path, dividends, withholding)

    with pytest.raises(PaymentInLieuOfDividendError) as excinfo:
        run_schedule_os(
            input_path=statement,
            config_path=config,
            output_path=tmp_path / "schedule-os.csv",
            financial_year=FinancialYear(start_year=2025),
        )

    error = excinfo.value
    assert str(error) == _message(row_index)
    assert error.args == (_message(row_index),)
    assert vars(error) == {"source_row_index": row_index}
    for text in (str(error), repr(error), *map(repr, error.args), repr(vars(error))):
        _assert_no_marker(text, statement)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_a_matching_change_in_nav_line_still_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1279's check reads the same rows as payments in lieu, so a printed
    ``Payment In Lieu Of Dividends`` line that agrees passes it, and
    Schedule OS then refuses the row."""
    dividends, withholding, row_index = _REFUSED["issue-repro"]
    change_in_nav = (
        "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
        "Current Total,Change\n"
        "Net Asset Value,Data,Cash,0,11.28,0,11.28,11.28\n"
        "Net Asset Value,Data,Total,0,11.28,0,11.28,11.28\n"
        "Change in NAV,Header,Field Name,Field Value\n"
        "Change in NAV,Data,Starting Value,0\n"
        "Change in NAV,Data,Dividends,10.00\n"
        "Change in NAV,Data,Payment In Lieu Of Dividends,3.70\n"
        "Change in NAV,Data,Withholding Tax,-2.42\n"
        "Change in NAV,Data,Ending Value,11.28\n"
    )
    statement, config = _write(tmp_path, dividends, withholding, base=_base() + change_in_nav)
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            "schedule-os",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / "schedule-os.csv")),
            *_FY,
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_message(row_index)}\n"
    _assert_no_marker(captured.err, statement)
    assert list(out.iterdir()) == []


# ---- what doesn't change ---------------------------------------------------------


def _run(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    statement_text: str,
    argv: Callable[[Path, Path, Path], list[str]],
) -> tuple[int, str, str, dict[str, bytes]]:
    """Run ``argv(statement, config, out)`` on ``statement_text``; return its
    exit, its stdout and stderr with ``run_dir`` masked, and its output files."""
    run_dir.mkdir(parents=True)
    statement = run_dir / "statement.csv"
    statement.write_text(statement_text, encoding="utf-8")
    config = run_dir / "user.toml"
    config.write_text(_CONFIG, encoding="utf-8")
    out = run_dir / "out"
    out.mkdir()
    capsys.readouterr()
    rc = main(argv(statement, config, out))
    captured = capsys.readouterr()
    files = {path.name: path.read_bytes() for path in sorted(out.iterdir())}
    return (
        rc,
        captured.out.replace(str(run_dir), "<run>"),
        captured.err.replace(str(run_dir), "<run>"),
        files,
    )


def _command(command: str, extra: tuple[str, ...], target: str) -> Callable[..., list[str]]:
    def argv(statement: Path, config: Path, out: Path) -> list[str]:
        return [
            command,
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / target)),
            *_FY,
            *extra,
        ]

    return argv


_WIDER_PERIOD = "April 1, 2024 - March 31, 2026"
_EARLIER = "12-09-2024"


# Every income command reads a statement whose period contains the FY (#1284).
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(_command("schedule-os", (), "schedule-os.csv"), id="schedule-os"),
        pytest.param(_command("schedule-fsi", (), "schedule-fsi.csv"), id="schedule-fsi"),
        pytest.param(_command("schedule-tr", _ITR, "schedule-tr.csv"), id="schedule-tr"),
        pytest.param(_command("form-67", (), "form-67.csv"), id="form-67"),
        pytest.param(_run_all_argv, id="run-all"),
    ],
)
def test_an_out_of_fy_payment_in_lieu_changes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
) -> None:
    """In a statement wider than the FY, a payment in lieu (and its
    withholding) from before the FY is skipped as before: each command gives
    exactly what it gives without the row."""
    base = _base(_WIDER_PERIOD)
    without = _statement((_CASH_ROW,), (_CASH_WHT_ROW,), base)
    earlier_in_lieu = _IN_LIEU_ROW.replace(_DATE, _EARLIER)
    earlier_wht = _IN_LIEU_WHT_ROW.replace(_DATE, _EARLIER)
    with_row = _statement((earlier_in_lieu, _CASH_ROW), (earlier_wht, _CASH_WHT_ROW), base)

    baseline = _run(tmp_path / "without", capsys, without, argv)
    assert baseline[0] in (0, 3)
    assert baseline[3]

    result = _run(tmp_path / "with", capsys, with_row, argv)

    assert result == baseline


def test_schedule_cg_ignores_the_payment_in_lieu(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _command("schedule-cg", _ITR, "schedule-cg.csv")
    dividends, withholding, _ = _REFUSED["issue-repro"]
    baseline = _run(
        tmp_path / "without", capsys, _statement((_CASH_ROW,), (_CASH_WHT_ROW,), _base()), argv
    )
    assert baseline[0] == 0
    assert baseline[3]

    result = _run(tmp_path / "with", capsys, _statement(dividends, withholding, _base()), argv)

    assert result == baseline


def test_schedule_fa_a3_still_sums_a_payment_in_lieu() -> None:
    """Table A3 column 10 sums every distribution in the calendar year,
    whatever its kind."""

    class _Rate:
        currency = "USD"

        def rate_for(self, event_date: date) -> Decimal:
            return Decimal("85")

    def dividend(kind: DistributionKind, amount: str) -> Dividend:
        return Dividend(
            symbol=Ticker(_SYMBOL),
            isin=Isin(_ISIN),
            payment_date=date(2025, 9, 12),
            amount_gross=Decimal(amount),
            currency="USD",
            description=f"{_PREFIX} {kind.value} USD 0.37 per Share",
            kind=kind,
        )

    totals = aggregate_dividends_by_symbol_inr(
        [
            dividend(DistributionKind.CASH_DIVIDEND, "10.00"),
            dividend(DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND, "3.70"),
        ],
        calendar_year=2025,
        fx_policy=_Rate(),
    )

    assert totals == {Ticker(_SYMBOL): Decimal("1164.50")}
