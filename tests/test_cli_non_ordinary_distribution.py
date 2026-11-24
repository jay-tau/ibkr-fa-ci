"""IBKR return of capital, long-term capital gain and cash in lieu rows refuse (#1310).

ibkr-fa can't yet file any of these three kinds. Schedule OS used to move an
in-FY row to its ``_excluded_distributions:`` audit block; its withholding
could be credited against an ordinary dividend. The capital-gains
computations ignored the row. Schedule FA added held symbols' rows to Table
A3's gross amount paid, or reported unmatched symbols as orphans. Now
``schedule-os``, ``schedule-fsi``,
``schedule-tr``, ``form-67`` and ``run-all`` refuse an in-FY row,
``schedule-cg`` and ``schedule-cg-ltcg`` (and so ``run-all``) a row on any
date, and ``schedule-fa`` (and ``run-all --cy-input``) a row in the calendar
year: exit 4, fixed text naming the kind and the row index, and no output files.
Every symbol, ISIN, date and amount here is synthetic, and no refusal prints
one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.compute.schedule_os import NonOrdinaryDistributionError
from ibkr_fa.models import DistributionKind
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_cg import run_schedule_cg
from ibkr_fa.pipeline_cg_ltcg import run_schedule_cg_ltcg
from ibkr_fa.pipeline_fa import run_fa_a3
from ibkr_fa.pipeline_os import run_schedule_os
from tests._income_blocks import with_totals

_FIXTURES = Path(__file__).parent / "fixtures"
_FY_FIXTURE = _FIXTURES / "e2e_activity_statement_schedule_tr_with_wht_fy2025_26.csv"
_CY_FIXTURE = _FIXTURES / "e2e_activity_statement_with_dividends_2025.csv"
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
_CASH_ROW = f"Dividends,Data,USD,{_DATE},{_CASH},10.00\n"
_CASH_WHT_ROW = f"Withholding Tax,Data,USD,{_DATE},{_CASH} - US Tax,-1.50,\n"

# id -> (kind, the tested row's Description)
_KINDS = {
    "return-of-capital": (
        DistributionKind.RETURN_OF_CAPITAL,
        f"{_PREFIX} Return Of Capital USD 0.37 per Share",
    ),
    "long-term-capital-gain": (
        DistributionKind.LONG_TERM_CAPITAL_GAIN,
        f"{_PREFIX} Long-Term Capital Gain USD 0.37 per Share",
    ),
    "cash-in-lieu": (DistributionKind.CASH_IN_LIEU, f"{_PREFIX} Cash in Lieu USD 3.70"),
}
_MARKERS = (_SYMBOL, _ISIN, "3.70", _DATE)


def _row(description: str, payment_date: str = _DATE) -> str:
    return f"Dividends,Data,USD,{payment_date},{description},3.70\n"


def _wht(description: str, payment_date: str = _DATE) -> str:
    return f"Withholding Tax,Data,USD,{payment_date},{description} - US Tax,-0.92,\n"


def _message(kind: DistributionKind, row_index: int) -> str:
    return (
        f"{kind.value} in the Dividends section (Dividends-section row index"
        f" {row_index}; 0-based, so data row {row_index + 1}): ibkr-fa can't yet"
        " file an IBKR return of capital, long-term capital gain distribution or"
        " cash in lieu. Skipping it, or counting it as a dividend, could misstate"
        " your income, capital gains, foreign tax credit or Schedule FA, so this"
        " run stops. Locate the row in your IBKR Activity Statement CSV by its"
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


def _cy_statement(dividends: tuple[str, ...], period: str | None = None) -> str:
    """The CY 2025 fixture (one XQBB lot) with ``dividends`` as its Dividends
    rows, optionally with another ``Period``."""
    lines = _CY_FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("Dividends,")]
    assert len(lines) - len(kept) == 4
    text = "".join(kept)
    if period is not None:
        old = 'Statement,Data,Period,"January 1, 2025 - December 31, 2025"'
        assert text.count(old) == 1
        text = text.replace(old, f'Statement,Data,Period,"{period}"')
    return with_totals(
        text + "Dividends,Header,Currency,Date,Description,Amount\n" + "".join(dividends)
    )


_XQBB_CASH = "XQBB(US99995B1044) Cash Dividend USD 0.21 per Share (Ordinary Dividend)"
_XQBB_CASH_ROW = f"Dividends,Data,USD,16-03-2025,{_XQBB_CASH},2.10\n"


def _xqbb_row(description: str, payment_date: str = "15-09-2025") -> str:
    """The issue's example 4: the CY fixture's second USD 2.10 XQBB dividend
    as another kind."""
    return f"Dividends,Data,USD,{payment_date},{description},2.10\n"


_XQBB_KINDS = {
    "return-of-capital": "XQBB(US99995B1044) Return Of Capital USD 0.21 per Share",
    "long-term-capital-gain": "XQBB(US99995B1044) Long-Term Capital Gain USD 0.21 per Share",
    "cash-in-lieu": "XQBB(US99995B1044) Cash in Lieu USD 2.10",
}
_XQBB_MARKERS = ("XQBB", "US99995B1044", "15-09-2025")


def _write(run_dir: Path, statement_text: str, config_text: str = _CONFIG) -> tuple[Path, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    statement = run_dir / "statement.csv"
    statement.write_text(statement_text, encoding="utf-8")
    config = run_dir / "user.toml"
    config.write_text(config_text, encoding="utf-8")
    return statement, config


def _stale(out: Path, names: tuple[str, ...]) -> None:
    out.mkdir()
    for name in names:
        (out / name).write_bytes(b"STALE")


def _assert_no_marker(text: str, statement: Path, markers: tuple[str, ...] = _MARKERS) -> None:
    input_text = statement.read_text(encoding="utf-8")
    for marker in markers:
        assert marker in input_text, marker
        assert marker not in text, marker


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


def _run_all_argv(statement: Path, config: Path, out: Path) -> list[str]:
    return [
        "run-all",
        *("--fy-input", str(statement), "--config", str(config)),
        *("--output-dir", str(out)),
        *_FY,
        *_ITR,
    ]


def _schedule_fa_argv(statement: Path, config: Path, out: Path) -> list[str]:
    return [
        "schedule-fa",
        *("--input", str(statement), "--config", str(config)),
        *("--output", str(out / "schedule-fa.csv")),
        *("--calendar-year", "2025"),
    ]


def _run_all_errors(err: str) -> list[str]:
    error_label = cli._RUN_ALL_STATUS_LABEL["error"]
    return [line.split()[1] for line in err.splitlines() if line.lstrip().startswith(error_label)]


# ---- the issue's examples 1-3 through every FY command --------------------------

# example -> (dividend rows, withholding rows, the tested row's index); the
# tested row is filled in per kind.
_EXAMPLES: dict[str, Callable[[str], tuple[tuple[str, ...], tuple[str, ...], int]]] = {
    "with-cash-dividend-and-withholding": lambda d: (
        (_CASH_ROW, _row(d)),
        (_CASH_WHT_ROW, _wht(d)),
        1,
    ),
    "alone-with-withholding": lambda d: ((_row(d),), (_wht(d),), 0),
    "with-cash-dividend-no-withholding": lambda d: ((_CASH_ROW, _row(d)), (), 1),
}

_FY_COMMANDS = [
    pytest.param("schedule-os", ("--format", "filing-csv"), ("schedule-os.csv",), id="schedule-os"),
    pytest.param("schedule-fsi", (), ("schedule-fsi.csv",), id="schedule-fsi"),
    pytest.param("schedule-tr", _ITR, ("schedule-tr.csv",), id="schedule-tr"),
    pytest.param("form-67", (), ("form-67.csv", "form-67-debug.csv"), id="form-67"),
    pytest.param("schedule-cg", _ITR, ("schedule-cg.csv",), id="schedule-cg"),
    pytest.param("schedule-cg-ltcg", _ITR, ("schedule-cg-ltcg.csv",), id="schedule-cg-ltcg"),
]


@pytest.mark.parametrize("example", sorted(_EXAMPLES))
@pytest.mark.parametrize("kind_id", sorted(_KINDS))
@pytest.mark.parametrize(("command", "extra", "targets"), _FY_COMMANDS)
def test_fy_command_refuses_an_in_fy_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    extra: tuple[str, ...],
    targets: tuple[str, ...],
    kind_id: str,
    example: str,
) -> None:
    kind, description = _KINDS[kind_id]
    dividends, withholding, row_index = _EXAMPLES[example](description)
    statement, config = _write(tmp_path, _statement(dividends, withholding, _base()))
    out = tmp_path / "out"
    _stale(out, targets)

    rc = main(_command(command, extra, targets[0])(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_message(kind, row_index)}\n"
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
    "schedule-fa.csv",
    "schedule-fa-a2.csv",
)


@pytest.mark.parametrize("example", sorted(_EXAMPLES))
@pytest.mark.parametrize("kind_id", sorted(_KINDS))
def test_run_all_refuses_an_in_fy_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind_id: str, example: str
) -> None:
    """The shared Form 67 readiness check runs Schedule OS dispatch before
    any schedule, so ``run-all`` stops there."""
    kind, description = _KINDS[kind_id]
    dividends, withholding, row_index = _EXAMPLES[example](description)
    statement, config = _write(tmp_path, _statement(dividends, withholding, _base()))
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main(_run_all_argv(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: run-all: {_message(kind, row_index)}\n"
    _assert_no_marker(captured.err, statement)
    assert list(out.iterdir()) == []


# ---- the issue's example 4 through Schedule FA ----------------------------------


@pytest.mark.parametrize("kind_id", sorted(_KINDS))
def test_schedule_fa_refuses_a_row_in_the_calendar_year(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind_id: str
) -> None:
    """Table A3 used to add the row to XQBB's gross amount paid (exit 3)."""
    kind, _ = _KINDS[kind_id]
    statement, config = _write(
        tmp_path, _cy_statement((_XQBB_CASH_ROW, _xqbb_row(_XQBB_KINDS[kind_id])))
    )
    out = tmp_path / "out"
    _stale(out, ("schedule-fa.csv", "schedule-fa-a2.csv"))

    rc = main(_schedule_fa_argv(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_message(kind, 1)}\n"
    _assert_no_marker(captured.err, statement, _XQBB_MARKERS)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("kind_id", sorted(_KINDS))
def test_run_all_schedule_fa_leg_refuses_a_row_in_the_calendar_year(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind_id: str
) -> None:
    """The FY schedules read an ordinary ``--fy-input``; Schedule FA refuses
    the ``--cy-input``'s row, and the run removes every target."""
    kind, _ = _KINDS[kind_id]
    fy_input, config = _write(tmp_path / "fy", _statement((_CASH_ROW,), (_CASH_WHT_ROW,), _base()))
    cy_input, _ = _write(
        tmp_path / "cy", _cy_statement((_XQBB_CASH_ROW, _xqbb_row(_XQBB_KINDS[kind_id])))
    )
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main([*_run_all_argv(fy_input, config, out), "--cy-input", str(cy_input)])

    assert rc == 4
    err = capsys.readouterr().err
    assert f"error: {_message(kind, 1)}\n" in err
    assert _run_all_errors(err) == ["schedule-fa"]
    _assert_no_marker(err, cy_input, ("US99995B1044", "15-09-2025"))
    assert list(out.iterdir()) == []


# ---- statements wider than the FY ------------------------------------------------

# where -> (the statement's Period, the tested row's date)
_OUTSIDE_FY = {
    "before-the-fy": ("April 1, 2024 - March 31, 2026", "12-09-2024"),
    "after-the-fy": ("April 1, 2025 - June 30, 2026", "12-05-2026"),
}


def _outside_fy_statement(where: str, description: str, *, with_row: bool) -> str:
    period, row_date = _OUTSIDE_FY[where]
    tested = ((_row(description, row_date),), (_wht(description, row_date),))
    dividends = (*tested[0], _CASH_ROW) if with_row else (_CASH_ROW,)
    withholding = (*tested[1], _CASH_WHT_ROW) if with_row else (_CASH_WHT_ROW,)
    return _statement(dividends, withholding, _base(period))


@pytest.mark.parametrize("where", sorted(_OUTSIDE_FY))
@pytest.mark.parametrize("kind_id", sorted(_KINDS))
@pytest.mark.parametrize(
    ("command", "target"),
    [
        pytest.param("schedule-cg", "schedule-cg.csv", id="schedule-cg"),
        pytest.param("schedule-cg-ltcg", "schedule-cg-ltcg.csv", id="schedule-cg-ltcg"),
    ],
)
def test_capital_gains_command_refuses_a_row_outside_the_fy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    target: str,
    kind_id: str,
    where: str,
) -> None:
    """A return of capital or cash in lieu before the FY can change the cost
    of a lot sold in it, so the capital-gains commands refuse any date."""
    kind, description = _KINDS[kind_id]
    statement, config = _write(tmp_path, _outside_fy_statement(where, description, with_row=True))
    out = tmp_path / "out"
    _stale(out, (target,))

    rc = main(_command(command, _ITR, target)(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_message(kind, 0)}\n"
    _assert_no_marker(captured.err, statement, (*_MARKERS, _OUTSIDE_FY[where][1]))
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("where", sorted(_OUTSIDE_FY))
@pytest.mark.parametrize("kind_id", sorted(_KINDS))
def test_run_all_capital_gains_legs_refuse_a_row_outside_the_fy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind_id: str, where: str
) -> None:
    """Schedule OS dispatch skips the row, so the run reaches its schedules;
    both capital-gains legs refuse it, and the run removes every target."""
    kind, description = _KINDS[kind_id]
    statement, config = _write(tmp_path, _outside_fy_statement(where, description, with_row=True))
    out = tmp_path / "out"
    _stale(out, _RUN_ALL_OUTPUTS)

    rc = main(_run_all_argv(statement, config, out))

    assert rc == 4
    err = capsys.readouterr().err
    assert err.count(f"error: {_message(kind, 0)}\n") == 2
    assert _run_all_errors(err) == ["schedule-cg", "schedule-cg-ltcg"]
    assert list(out.iterdir()) == []


def _run(
    run_dir: Path,
    capsys: pytest.CaptureFixture[str],
    statement_text: str,
    argv: Callable[[Path, Path, Path], list[str]],
) -> tuple[int, str, str, dict[str, bytes]]:
    """Run ``argv(statement, config, out)`` on ``statement_text``; return its
    exit, its stdout and stderr with ``run_dir`` masked, and its output files."""
    statement, config = _write(run_dir, statement_text)
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


@pytest.mark.parametrize("where", sorted(_OUTSIDE_FY))
@pytest.mark.parametrize("kind_id", sorted(_KINDS))
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(_command("schedule-os", (), "schedule-os.csv"), id="schedule-os"),
        pytest.param(_command("schedule-fsi", (), "schedule-fsi.csv"), id="schedule-fsi"),
        pytest.param(_command("schedule-tr", _ITR, "schedule-tr.csv"), id="schedule-tr"),
        pytest.param(_command("form-67", (), "form-67.csv"), id="form-67"),
    ],
)
def test_a_row_outside_the_fy_changes_no_income_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
    kind_id: str,
    where: str,
) -> None:
    """In a statement wider than the FY, a row (and its withholding) dated
    before or after the FY is skipped as before: each income command gives
    exactly what it gives without the row."""
    _, description = _KINDS[kind_id]
    baseline = _run(
        tmp_path / "without",
        capsys,
        _outside_fy_statement(where, description, with_row=False),
        argv,
    )
    assert baseline[0] in (0, 3)
    assert baseline[3]

    result = _run(
        tmp_path / "with", capsys, _outside_fy_statement(where, description, with_row=True), argv
    )

    assert result == baseline


@pytest.mark.parametrize("kind_id", sorted(_KINDS))
def test_a_row_before_the_calendar_year_changes_nothing_in_schedule_fa(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind_id: str
) -> None:
    """In a statement that starts before the calendar year (#1288), a row
    dated before it is dropped as before: Schedule FA gives exactly what it
    gives without the row."""
    period = "January 1, 2024 - December 31, 2025"
    without = _cy_statement((_XQBB_CASH_ROW,), period)
    with_row = _cy_statement(
        (_xqbb_row(_XQBB_KINDS[kind_id], "16-09-2024"), _XQBB_CASH_ROW), period
    )
    baseline = _run(tmp_path / "without", capsys, without, _schedule_fa_argv)
    assert baseline[0] == 3
    assert baseline[3]

    result = _run(tmp_path / "with", capsys, with_row, _schedule_fa_argv)

    assert result == baseline


# ---- refusals that run first keep their place -------------------------------------

_ROC_KIND, _ROC = _KINDS["return-of-capital"]
_PIL = f"{_PREFIX} Payment In Lieu Of Dividend USD 0.37 per Share"
_PIL_MESSAGE_START = (
    "error: Payment In Lieu Of Dividend in the Dividends section (Dividends-section row index"
)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param(_PIL, _ROC, f"{_PIL_MESSAGE_START} 0;", id="payment-in-lieu-first"),
        pytest.param(
            _ROC, _PIL, f"error: {_message(_ROC_KIND, 0)}\n", id="return-of-capital-first"
        ),
        pytest.param(
            f"{_PREFIX} Mystery Distribution USD 0.37 per Share",
            _ROC,
            "error: Unrecognised dividend distribution type in the Dividends section"
            " (Dividends-section row index 0;",
            id="unrecognised-first",
        ),
        pytest.param(
            f"{_ROC} extra",
            _ROC,
            "error: Malformed dividend description in the Dividends section"
            " (Dividends-section row index 0;",
            id="malformed-first",
        ),
    ],
)
def test_the_first_refusing_row_decides_the_schedule_os_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    first: str,
    second: str,
    expected: str,
) -> None:
    statement, config = _write(tmp_path, _statement((_row(first), _row(second)), (), _base()))
    out = tmp_path / "out"
    out.mkdir()

    rc = main(_command("schedule-os", (), "schedule-os.csv")(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(expected)
    assert captured.err.count("error: ") == 1
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    ("command", "extra", "target"),
    [
        pytest.param("schedule-cg", _ITR, "schedule-cg.csv", id="schedule-cg"),
        pytest.param("schedule-cg-ltcg", _ITR, "schedule-cg-ltcg.csv", id="schedule-cg-ltcg"),
    ],
)
def test_capital_gains_commands_still_ignore_the_other_refused_kinds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    extra: tuple[str, ...],
    target: str,
) -> None:
    """Payments in lieu and unrecognised or malformed rows keep their
    handling in the capital-gains commands: they're skipped, so the first
    of the three kinds decides the error."""
    others = (
        _row(_PIL),
        _row(f"{_PREFIX} Mystery Distribution USD 0.37 per Share"),
        _row(f"{_ROC} extra"),
    )
    statement, config = _write(tmp_path, _statement((*others, _row(_ROC)), (), _base()))
    out = tmp_path / "out"
    out.mkdir()

    rc = main(_command(command, extra, target)(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == f"error: {_message(_ROC_KIND, 3)}\n"
    assert list(out.iterdir()) == []


_ORDINARY = f"{_PREFIX} Cash Dividend USD 0.37 per Share"
_UNPROVEN_INTEREST = "Interest,Data,USD,30-04-2025,USD Interest Paid for Apr-2025,1.23\n"
_CHANGE_IN_NAV = (
    "Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,"
    "Current Total,Change\n"
    "Net Asset Value,Data,Cash,0,1.00,0,1.00,1.00\n"
    "Net Asset Value,Data,Total,0,1.00,0,1.00,1.00\n"
    "Change in NAV,Header,Field Name,Field Value\n"
    "Change in NAV,Data,Starting Value,0\n"
    "Change in NAV,Data,Dividends,1.00\n"
    "Change in NAV,Data,Ending Value,1.00\n"
)
_OTHER_ACCOUNT = _CONFIG.replace('number = "U12345678"', 'number = "U87654321"')


def _fy_parse_error(description: str) -> str:
    return _statement((_row(description), _row(_ORDINARY).replace(",3.70", ",abc")), (), _base())


def _fy_change_in_nav(description: str) -> str:
    return _statement((_row(description),), (), _base() + _CHANGE_IN_NAV)


def _fy_unproven_interest(description: str) -> str:
    return with_totals(
        _statement((_row(description),), (), _base())
        + "Interest,Header,Currency,Date,Description,Amount\n"
        + _UNPROVEN_INTEREST
    )


def _fy_partial_period(description: str) -> str:
    return _statement((_row(description),), (), _base("April 2, 2025 - March 31, 2026"))


def _fy_plain(description: str) -> str:
    return _statement((_row(description),), (), _base())


_PARSE_ERROR = "ParseError in section 'Dividends' at row 1, column 'Amount'"
_NAV_ERROR = "Change in NAV 'Dividends' amount"
_INTEREST_ERROR = "the Activity Statement's Interest section has row(s)"
_PERIOD_ERROR = "does not fully contain the Indian financial year 2025-26"
_ACCOUNT_ERROR = "ConfigError"

_INCOME_ARGVS = [
    pytest.param(_command("schedule-os", (), "schedule-os.csv"), id="schedule-os"),
    pytest.param(_command("schedule-fsi", (), "schedule-fsi.csv"), id="schedule-fsi"),
    pytest.param(_command("schedule-tr", _ITR, "schedule-tr.csv"), id="schedule-tr"),
    pytest.param(_command("form-67", (), "form-67.csv"), id="form-67"),
    pytest.param(_run_all_argv, id="run-all"),
]
_CG_ARGVS = [
    pytest.param(_command("schedule-cg", _ITR, "schedule-cg.csv"), id="schedule-cg"),
    pytest.param(_command("schedule-cg-ltcg", _ITR, "schedule-cg-ltcg.csv"), id="schedule-cg-ltcg"),
]
_EARLIER_FY = [
    pytest.param(_fy_parse_error, _CONFIG, _PARSE_ERROR, id="parse-error"),
    pytest.param(_fy_partial_period, _CONFIG, _PERIOD_ERROR, id="period"),
    pytest.param(_fy_plain, _OTHER_ACCOUNT, _ACCOUNT_ERROR, id="account-id"),
]
_EARLIER_INCOME_ONLY = [
    pytest.param(_fy_change_in_nav, _CONFIG, _NAV_ERROR, id="change-in-nav"),
    pytest.param(_fy_unproven_interest, _CONFIG, _INTEREST_ERROR, id="unproven-interest"),
]


def _assert_earlier_refusal_wins(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
    build: Callable[[str], str],
    config_text: str,
    expected: str,
) -> None:
    """The statement with a Return Of Capital gives exactly what it gives with
    an ordinary dividend in the row's place: the earlier refusal, exit 4."""

    def run(name: str, description: str) -> tuple[int, str, str, list[str]]:
        statement, config = _write(tmp_path / name, build(description), config_text)
        out = tmp_path / name / "out"
        out.mkdir()
        capsys.readouterr()
        rc = main(argv(statement, config, out))
        captured = capsys.readouterr()
        return (
            rc,
            captured.out.replace(str(tmp_path / name), "<run>"),
            captured.err.replace(str(tmp_path / name), "<run>"),
            sorted(path.name for path in out.iterdir()),
        )

    ordinary = run("ordinary", _ORDINARY)
    result = run("return-of-capital", _ROC)

    assert result == ordinary
    assert result[0] == 4
    assert expected in result[2]
    assert "Return Of Capital in the Dividends section" not in result[2]
    assert result[3] == []


@pytest.mark.parametrize(("build", "config_text", "expected"), _EARLIER_FY + _EARLIER_INCOME_ONLY)
@pytest.mark.parametrize("argv", _INCOME_ARGVS)
def test_earlier_refusals_still_win_in_the_income_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
    build: Callable[[str], str],
    config_text: str,
    expected: str,
) -> None:
    _assert_earlier_refusal_wins(tmp_path, capsys, argv, build, config_text, expected)


@pytest.mark.parametrize(("build", "config_text", "expected"), _EARLIER_FY)
@pytest.mark.parametrize("argv", _CG_ARGVS)
def test_earlier_refusals_still_win_in_the_capital_gains_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
    build: Callable[[str], str],
    config_text: str,
    expected: str,
) -> None:
    _assert_earlier_refusal_wins(tmp_path, capsys, argv, build, config_text, expected)


def _cy_parse_error(description: str) -> str:
    return _cy_statement(
        (_XQBB_CASH_ROW, _xqbb_row(description), _xqbb_row(_XQBB_CASH).replace(",2.10", ",abc"))
    )


def _cy_partial_period(description: str) -> str:
    return _cy_statement(
        (_XQBB_CASH_ROW, _xqbb_row(description)), "January 2, 2025 - December 31, 2025"
    )


def _cy_plain(description: str) -> str:
    return _cy_statement((_XQBB_CASH_ROW, _xqbb_row(description)))


@pytest.mark.parametrize(
    ("build", "config_text", "expected"),
    [
        pytest.param(
            _cy_parse_error,
            _CONFIG,
            "ParseError in section 'Dividends' at row 2, column 'Amount'",
            id="parse-error",
        ),
        pytest.param(
            _cy_partial_period,
            _CONFIG,
            "period must end on Dec 31, 2025 and start on or before Jan 1, 2025",
            id="period",
        ),
        pytest.param(_cy_plain, _OTHER_ACCOUNT, _ACCOUNT_ERROR, id="account-id"),
    ],
)
def test_earlier_refusals_still_win_in_schedule_fa(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build: Callable[[str], str],
    config_text: str,
    expected: str,
) -> None:
    def xqbb(description: str) -> str:
        return build(_XQBB_KINDS["return-of-capital"] if description == _ROC else _XQBB_CASH)

    _assert_earlier_refusal_wins(tmp_path, capsys, _schedule_fa_argv, xqbb, config_text, expected)


# ---- privacy ------------------------------------------------------------------------

_PRIVATE_SYMBOL = "XQRC"
_PRIVATE_ISIN = "US99995R1004"
_PRIVATE_PREFIX = f"{_PRIVATE_SYMBOL}({_PRIVATE_ISIN})"
_PRIVATE_DATE = "14-10-2025"
_PRIVATE_KINDS = {
    "return-of-capital": f"{_PRIVATE_PREFIX} Return Of Capital USD 0.25 per Share",
    "long-term-capital-gain": f"{_PRIVATE_PREFIX} Long-Term Capital Gain USD 0.25 per Share",
    "cash-in-lieu": f"{_PRIVATE_PREFIX} Cash in Lieu USD 473812.00",
}
_PRIVATE_MARKERS = (_PRIVATE_SYMBOL, _PRIVATE_ISIN, "473812", _PRIVATE_DATE)


def _private_fy(kind_id: str) -> str:
    description = _PRIVATE_KINDS[kind_id]
    return _statement(
        (_CASH_ROW, f"Dividends,Data,USD,{_PRIVATE_DATE},{description},473812.00\n"),
        (_CASH_WHT_ROW,),
        _base(),
    )


def _private_cy(kind_id: str) -> str:
    description = _PRIVATE_KINDS[kind_id]
    return _cy_statement(
        (_XQBB_CASH_ROW, f"Dividends,Data,USD,{_PRIVATE_DATE},{description},473812.00\n")
    )


def _assert_no_private_marker(text: str, statement: Path) -> None:
    input_text = statement.read_text(encoding="utf-8")
    for marker in _PRIVATE_MARKERS:
        assert marker in input_text, marker
        assert marker not in text, marker
    for marker in ("2025-10-14", "14/10/2025", "473,812"):
        assert marker not in text, marker


@pytest.mark.parametrize("kind_id", sorted(_KINDS))
@pytest.mark.parametrize(
    ("argv", "cy"),
    [
        *(pytest.param(*param.values, False, id=param.id) for param in _INCOME_ARGVS),
        *(pytest.param(*param.values, False, id=param.id) for param in _CG_ARGVS),
        pytest.param(_schedule_fa_argv, True, id="schedule-fa"),
    ],
)
def test_no_refusal_prints_the_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Callable[[Path, Path, Path], list[str]],
    cy: bool,
    kind_id: str,
) -> None:
    statement, config = _write(tmp_path, _private_cy(kind_id) if cy else _private_fy(kind_id))
    out = tmp_path / "out"
    out.mkdir()

    rc = main(argv(statement, config, out))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert _message(_KINDS[kind_id][0], 1) in captured.err
    _assert_no_private_marker(captured.out + captured.err, statement)
    assert list(out.iterdir()) == []


_FY_2025_26 = FinancialYear(start_year=2025)

# name -> (a call of the pipeline on (statement, config, output), whether it
# reads the calendar-year statement)
_RUNNERS: dict[str, tuple[Callable[[Path, Path, Path], object], bool]] = {
    "run_schedule_os": (
        lambda statement, config, output: run_schedule_os(
            input_path=statement,
            config_path=config,
            output_path=output,
            financial_year=_FY_2025_26,
        ),
        False,
    ),
    "run_schedule_cg": (
        lambda statement, config, output: run_schedule_cg(
            input_path=statement,
            config_path=config,
            output_path=output,
            financial_year=_FY_2025_26,
        ),
        False,
    ),
    "run_schedule_cg_ltcg": (
        lambda statement, config, output: run_schedule_cg_ltcg(
            input_path=statement,
            config_path=config,
            output_path=output,
            financial_year=_FY_2025_26,
        ),
        False,
    ),
    "run_fa_a3": (
        lambda statement, config, output: run_fa_a3(
            input_path=statement, config_path=config, output_path=output, calendar_year=2025
        ),
        True,
    ),
}


@pytest.mark.parametrize("kind_id", sorted(_KINDS))
@pytest.mark.parametrize("runner", sorted(_RUNNERS))
def test_the_refusal_holds_no_row_content(tmp_path: Path, runner: str, kind_id: str) -> None:
    run, cy = _RUNNERS[runner]
    statement, config = _write(tmp_path, _private_cy(kind_id) if cy else _private_fy(kind_id))

    with pytest.raises(NonOrdinaryDistributionError) as excinfo:
        run(statement, config, tmp_path / "out.csv")

    error = excinfo.value
    kind = _KINDS[kind_id][0]
    assert str(error) == _message(kind, 1)
    assert error.args == (_message(kind, 1),)
    assert vars(error) == {"kind": kind, "source_row_index": 1}
    for text in (str(error), repr(error), *map(repr, error.args), repr(vars(error))):
        _assert_no_private_marker(text, statement)
    assert error.__cause__ is None
    assert error.__context__ is None
