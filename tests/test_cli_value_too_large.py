"""A value too large or too precise to compute is a typed refusal (#832).

A huge but finite parsed value can overflow ``decimal`` arithmetic, lose its
value in an XLSX audit cell, or fail to round while the CLI prints its report.
Each is refused: exit 4 with ``error: <command>: a value in the input is too
large or too precise to compute (<exception class>)`` (or the writer's
contract error, or an exact product's own refusal since #1250, or a reader's
digit-limit refusal since #1275 and #1276), and the run leaves no output.

Since #1275 and #1276 every reader refuses a number past its read-time digit
limit first, so each test below whose absurd input the reader now refuses
stubs that limit out of the reader to keep the later check pinned, and a
sibling test pins the reader's refusal of the same input led by a marker.
"""

from __future__ import annotations

import argparse
import ast
import decimal
import errno
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy_rule115 import Rule115Policy, Rule115Resolution
from tests._exact_refusals import INEXACT_PRODUCT
from tests._ibkr_run_all import FY_2025_26_INPUT, run_all_fy
from tests._ibkr_user_toml import CUSTODIAN_TOML
from tests._netbenefits_e2e import nb_e2e_bundle
from tests._number_limits import MONEY, PRICE, QUANTITY
from tests._source_all import NETBENEFITS_ALL, STOCKPLAN_ALL, SourceAll
from tests._stockplan_fa_exports import setup_stockplan_exports

_FIXTURES = Path(__file__).parent / "fixtures"
_MESSAGE = "a value in the input is too large or too precise to compute"
_ACCOUNT = """\
[account]
number = "U12345678"
holder_name = "Sample Holder"
opening_date = 2020-01-01
status = "BeneficialOwner"
us_person = true
"""
_FX = """
[fx]
max_carry_forward_days = 14
"""


# The digit run each sibling test's breaching number is led by (#1276).
_MARKER = "9464"


def _without_read_limits(monkeypatch: pytest.MonkeyPatch, *readers: str) -> None:
    """Stub the read-time digit limit (#1275, #1276) out of each ``readers``
    module under ``ibkr_fa.parser``, so an absurd value it reads reaches the
    check after it."""
    for reader in readers:
        monkeypatch.setattr(
            f"ibkr_fa.parser.{reader}.number_limit_breach",
            lambda _value, _kind: None,
            raising=False,
        )


def _refusal(command: str, exc: type[decimal.DecimalException]) -> str:
    return f"error: {command}: {_MESSAGE} ({exc.__name__})\n"


def _refused_run(command: str, exc: type[decimal.DecimalException]) -> str:
    """The stderr a refused ``command`` run ends with: StockPlan points at its contract."""
    pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if command.startswith("stockplan-") else ""
    return _refusal(command, exc) + pointer


def _edit(fixture: str, *edits: tuple[str, str]) -> str:
    text = (_FIXTURES / fixture).read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


def _schedule_fa(
    tmp_path: Path,
    statement: str,
    *,
    custodian: bool = False,
    extra: tuple[str, ...] = (),
    stale: bool = False,
) -> tuple[int, Path]:
    """Run ``schedule-fa`` on ``statement`` for CY 2025 into ``out/``, empty or,
    if ``stale``, holding an earlier run's Table A3 and A2 outputs."""
    source = tmp_path / "statement.csv"
    source.write_text(statement, encoding="utf-8")
    config = tmp_path / "user.toml"
    config.write_text(_ACCOUNT + (CUSTODIAN_TOML if custodian else "") + _FX, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    suffix = ".xlsx" if "xlsx" in extra else ".csv"
    if stale:
        (out / f"schedule-fa{suffix}").write_text("stale\n", encoding="utf-8")
        (out / f"schedule-fa-a2{suffix}").write_text("stale\n", encoding="utf-8")
    rc = main(
        [
            "schedule-fa",
            *("--input", str(source), "--config", str(config)),
            *("--output", str(out / f"schedule-fa{suffix}")),
            *("--calendar-year", "2025", "--no-fetch-prices"),
            *("--price-cache-dir", str(tmp_path / "cache")),
            *extra,
        ]
    )
    return rc, out


# --- Raw decimal exceptions --------------------------------------------------


def test_schedule_fa_cost_basis_that_overflows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1250 made the A3 initial value's cost x rate exact-or-refuse, so a cost
    basis past the Decimal exponent is refused there, naming no value, before
    it can escape as a raw ``Overflow``. Since #1276 the Flex Open Positions
    reader refuses that cost basis first, by its digit limit (the next test),
    so this one stubs the limit out to reach the product."""
    _without_read_limits(monkeypatch, "_cells")
    statement = _edit("e2e_year_end_2025.csv", ('"85.2","852"', '"85.2","1E999999"'))

    rc, out = _schedule_fa(tmp_path, statement)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: InvariantError: Schedule FA A3 initial value, INR: {INEXACT_PRODUCT}\n"
    )
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_schedule_fa_cost_basis_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the same cost basis past the Decimal exponent (here led by a
    marker) refuses when the Flex Open Positions section is read, with the money
    digit limit, before any arithmetic: exit 4, the fixed refusal alone, no
    value printed, and an earlier run's outputs are gone."""
    statement = _edit("e2e_year_end_2025.csv", ('"85.2","852"', f'"85.2","{_MARKER}E999999"'))

    rc, out = _schedule_fa(tmp_path, statement, stale=True)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'Open Positions (LOT)' at row 0, column 'CostBasisMoney': "
        f"{MONEY}\n"
    )
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert list(out.iterdir()) == []


def _netbenefits_lot_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cells: str
) -> tuple[Path, Path]:
    """The e2e NetBenefits bundle and config, with the bundle's Dec-31 open lot's
    Value and Gain/loss cells set to ``cells``."""
    bundle, config = nb_e2e_bundle(tmp_path, monkeypatch)
    lots = bundle / "open-lots.csv"
    text = lots.read_text(encoding="utf-8")
    edited = text.replace("626.24,-42.72", cells)
    assert edited != text
    lots.write_text(edited, encoding="utf-8")
    return bundle, config


def test_netbenefits_fa_lot_value_that_overflows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reader's ``Value - Cost basis`` cross-check overflows on a Value past
    the Decimal exponent, and the CLI refuses the raw ``Overflow``. Since #1275
    the reader refuses that Value first, by its digit limit (the next test), so
    this one stubs the limit out to reach the cross-check."""
    monkeypatch.setattr(
        "ibkr_fa.parser.netbenefits.lots_csv.number_limit_breach",
        lambda _value, _kind: None,
        raising=False,
    )
    bundle, config = _netbenefits_lot_value(tmp_path, monkeypatch, "1E1000000,1E1000000")
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config)),
            *("--output", str(out / "schedule-fa-a3.csv"), "--calendar-year", "2025"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _refusal("netbenefits-fa", decimal.Overflow)
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_netbenefits_fa_lot_value_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1275: the same Value past the Decimal exponent (here led by a marker)
    refuses when the lots CSV is read, with the money digit limit, before any
    arithmetic: exit 4, the fixed refusal alone, no value printed, no output."""
    bundle, config = _netbenefits_lot_value(tmp_path, monkeypatch, "7242E1000000,7242E1000000")
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            "netbenefits-fa",
            *("--input", str(bundle), "--config", str(config)),
            *("--output", str(out / "schedule-fa-a3.csv"), "--calendar-year", "2025"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        "error: ParseError in section 'open lots open-lots.csv' at row 2, column 'Value': "
        f"{MONEY}\n"
    )
    assert captured.out == ""
    assert "7242" not in captured.err
    assert list(out.iterdir()) == []


def test_stockplan_fa_vest_fmv_that_overflows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A vest FMV past the Decimal exponent overflows the arithmetic, and the
    exact product refuses with its fixed ``InvariantError``. Since #1276 the
    StockPlan reader refuses that FMV first, by its digit limit (the next test),
    so this one stubs the limit out to reach the arithmetic."""
    _without_read_limits(monkeypatch, "stockplan._xlsx")
    # A text cell: a numeric cell could not hold the value.
    input_dir, config, _ = setup_stockplan_exports(
        tmp_path, vests=[("2025-08-14", "V1", "1E999999", 10, 10)]
    )
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config)),
            *("--output", str(out / "schedule-fa.csv"), "--calendar-year", "2025"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: InvariantError: StockPlan vest acquisition value: {INEXACT_PRODUCT}\n"
        f"{cli._STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_stockplan_fa_vest_fmv_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the same text-cell FMV past the Decimal exponent (here led by a
    marker) refuses when the Releases sheet is read, with the unit price digit
    limit, before any arithmetic: exit 4, the fixed refusal and the contract
    pointer alone, no value printed, and an earlier run's output is gone."""
    input_dir, config, _ = setup_stockplan_exports(
        tmp_path, vests=[("2025-08-14", "V1", f"{_MARKER}E999999", 10, 10)]
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedule-fa.csv").write_text("stale\n", encoding="utf-8")

    rc = main(
        [
            "stockplan-fa",
            *("--input", str(input_dir), "--config", str(config)),
            *("--output", str(out / "schedule-fa.csv"), "--calendar-year", "2025"),
            "--no-fetch-prices",
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: ParseError in section 'Releases Report' at row 2, column 'Price': {PRICE}\n"
        f"{cli._STOCKPLAN_CONTRACT_POINTER}\n"
    )
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert list(out.iterdir()) == []


# --- XLSX audit cells --------------------------------------------------------

# XQBB's lot (acquired 2024-06-15, so no peak without a price series): Quantity,
# MarkPrice, PositionValue, CostBasisPrice, CostBasisMoney.
_XQBB_LOT = '"10","95.4","954","85.2","852"'


@pytest.mark.parametrize(
    "lot",
    [
        pytest.param('"1E-400","1E400","1","1E400","1"', id="close-reads-back-infinite"),
        pytest.param('"1E400","1E-400","1","1E-400","1"', id="close-reads-back-zero"),
        pytest.param(
            '"1E-308","1.7976931348623157E308","1.7976931348623157","1E308","1"',
            id="close-text-reads-back-infinite",
        ),
    ],
)
def test_xlsx_audit_cell_the_workbook_cannot_store_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], lot: str
) -> None:
    """A lot whose close the workbook can't store as a number is refused by the
    XLSX writer. Since #1276 the Flex Open Positions reader refuses each lot's
    quantity first, by its digit limit (the next test), so this one stubs the
    limit out to reach the writer."""
    _without_read_limits(monkeypatch, "_cells")
    statement = _edit("e2e_year_end_2025.csv", (_XQBB_LOT, lot))

    rc, out = _schedule_fa(tmp_path, statement, extra=("--format", "xlsx"))

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err.startswith("error: WriterContractError: row 1 cell at index 16 ")
    assert "cannot store" in captured.err
    assert captured.out == ""
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    "lot",
    [
        pytest.param(
            f'"{_MARKER}E-403","1E400","9.464","1E400","1"', id="close-reads-back-infinite"
        ),
        pytest.param(f'"{_MARKER}E397","1E-400","9.464","1E-400","1"', id="close-reads-back-zero"),
        pytest.param(
            f'"{_MARKER}E-311","1.7976931348623157E308","17.0133678283369557848","1E308","1"',
            id="close-text-reads-back-infinite",
        ),
    ],
)
def test_xlsx_audit_lot_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lot: str
) -> None:
    """#1276: each of those lots, its quantity here led by a marker (and its
    position value scaled to match), refuses when the Flex Open Positions
    section is read, at the quantity it reads first, with the quantity digit
    limit: exit 4, the fixed refusal alone, no value printed, and an earlier
    run's outputs are gone."""
    statement = _edit("e2e_year_end_2025.csv", (_XQBB_LOT, lot))

    rc, out = _schedule_fa(tmp_path, statement, extra=("--format", "xlsx"), stale=True)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: ParseError in section 'Open Positions (LOT)' at row 0, column 'Quantity': "
        f"{QUANTITY}\n"
    )
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert list(out.iterdir()) == []


# --- Values that cannot be rounded for printing -------------------------------


def test_orphan_dividend_that_cannot_be_rounded_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An orphan dividend of USD 1E30 can't be rounded for the CLI's report.
    Since #1276 the Activity Statement's Dividends reader refuses that amount
    (and its printed Total) first, by its digit limit (the next test), so this
    one stubs the limit out of both readers to reach the rounding."""
    _without_read_limits(
        monkeypatch, "activity_statement.dividends", "activity_statement._totals", "_cells"
    )
    statement = _edit(
        "e2e_activity_statement_with_orphan_dividend_2025.csv",
        ("Cash Dividend USD 5.00 per Share,5.00,", "Cash Dividend USD 5.00 per Share,1E30,"),
        ("Total,,,7.10,", "Total,,,1000000000000000000000000000002.10,"),
    )

    rc, out = _schedule_fa(tmp_path, statement)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _refusal("schedule-fa", decimal.InvalidOperation)
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_orphan_dividend_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the same 31-digit orphan dividend (here led by a marker) refuses
    when the Dividends section is read, with the money digit limit, before any
    arithmetic: exit 4, the fixed refusal alone, no value printed, and an
    earlier run's outputs are gone."""
    statement = _edit(
        "e2e_activity_statement_with_orphan_dividend_2025.csv",
        (
            "Cash Dividend USD 5.00 per Share,5.00,",
            f"Cash Dividend USD 5.00 per Share,{_MARKER}E27,",
        ),
        ("Total,,,7.10,", f"Total,,,{_MARKER}000000000000000000000000002.10,"),
    )

    rc, out = _schedule_fa(tmp_path, statement, stale=True)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: ParseError in section 'Dividends' at row 1, column 'Amount': {MONEY}\n"
    )
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert list(out.iterdir()) == []


def test_a2_period_end_cash_that_cannot_be_rounded_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Period-end cash of USD 1E30 can't be rounded for the CLI's Table A2
    period-end reference lines. Since #1276 the Net Asset Value reader refuses
    that cash first, by its digit limit (the next test), so this one stubs the
    limit out to reach the rounding."""
    _without_read_limits(monkeypatch, "_cells")
    statement = _edit(
        "e2e_activity_statement_with_nav_2025.csv",
        ("Data,Cash,0,100.00,0,100.00,100.00", "Data,Cash,0,1E30,0,1E30,1E30"),
        ("Data,Stock,0,2007.60,0,2007.60,2007.60", "Data,Stock,0,0,-1E30,-1E30,-1E30"),
        ("Data,Total,0,2107.60,0,2107.60,2107.60", "Data,Total,0,1E30,-1E30,0,0"),
        ("Ending Value,2107.60", "Ending Value,0"),
    )

    rc, out = _schedule_fa(tmp_path, statement, custodian=True)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _refusal("schedule-fa", decimal.InvalidOperation)
    assert captured.out == ""
    assert list(out.iterdir()) == []


def test_a2_period_end_cash_past_its_digit_limit_is_refused_when_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#1276: the same 31-digit period-end cash (here led by a marker) refuses
    when the Net Asset Value section is read, with the money digit limit, before
    any arithmetic: exit 4, the fixed refusal alone, no value printed, and an
    earlier run's outputs are gone."""
    huge = f"{_MARKER}E27"
    statement = _edit(
        "e2e_activity_statement_with_nav_2025.csv",
        ("Data,Cash,0,100.00,0,100.00,100.00", f"Data,Cash,0,{huge},0,{huge},{huge}"),
        ("Data,Stock,0,2007.60,0,2007.60,2007.60", f"Data,Stock,0,0,-{huge},-{huge},-{huge}"),
        ("Data,Total,0,2107.60,0,2107.60,2107.60", f"Data,Total,0,{huge},-{huge},0,0"),
        ("Ending Value,2107.60", "Ending Value,0"),
    )

    rc, out = _schedule_fa(tmp_path, statement, custodian=True, stale=True)

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == (
        f"error: ParseError in section 'Net Asset Value' at row 0, column 'Current Total': "
        f"{MONEY}\n"
    )
    assert captured.out == ""
    assert _MARKER not in captured.err
    assert list(out.iterdir()) == []


def test_form67_inr_tax_rate_that_cannot_be_rounded_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 1E-16 USD dividend whose 1E12 withholding is refunded in full at a
    lower rate files ₹1E13 of net tax but an INR rate near 1.11E29 %."""
    statement = tmp_path / "statement.csv"
    statement.write_text(
        _edit(
            "e2e_activity_statement_form67_same_year_wht_refund_fy2025_26.csv",
            (
                "USD,12-02-2026,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share "
                "(Ordinary Dividend),100.00,",
                "USD,15-04-2025,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share "
                "(Ordinary Dividend),0.0000000000000001,",
            ),
            (
                "USD,12-02-2026,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share - US Tax,"
                "-25.00,",
                "USD,15-04-2025,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share - US Tax,"
                "-1000000000000,",
            ),
            (
                "USD,12-03-2026,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share - US Tax,"
                "25.00,Re",
                "USD,15-05-2025,XQBA(US99995C1027) Cash Dividend USD 10.00 per Share - US Tax,"
                "1000000000000,Re",
            ),
            # The withholding Total stays 0.00: the refund cancels the tax.
            ("Dividends,Data,Total,,,100.00,", "Dividends,Data,Total,,,0.0000000000000001,"),
        ),
        encoding="utf-8",
    )
    config = tmp_path / "user.toml"
    config.write_text(_ACCOUNT + "\n[fx]\nmax_carry_forward_days = 4\n", encoding="utf-8")
    audited = Rule115Policy.rate_for_audited

    def rate_for_audited(self: Rule115Policy, event_date: date) -> Rule115Resolution:
        resolution = audited(self, event_date)
        rate = Decimal("90") if event_date == date(2025, 4, 15) else Decimal("80")
        return Rule115Resolution(
            rate=rate, specified_date=resolution.specified_date, rate_date=resolution.rate_date
        )

    monkeypatch.setattr(Rule115Policy, "rate_for_audited", rate_for_audited)
    out = tmp_path / "out"
    out.mkdir()

    rc = main(
        [
            "form-67",
            *("--input", str(statement), "--config", str(config)),
            *("--output", str(out / "form-67.csv"), "--financial-year", "2025-26"),
            *("--format", "review-csv"),
        ]
    )

    assert rc == 4
    captured = capsys.readouterr()
    assert captured.err == _refusal("form-67", decimal.InvalidOperation)
    assert captured.out == ""
    assert list(out.iterdir()) == []


# --- Every command -------------------------------------------------------------

_FY = ("--financial-year", "2025-26")
_ITR = ("--itr-form", "ITR-2")


def _source_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        *("--input", str(tmp_path / "in"), "--config", str(tmp_path / "user.toml")),
        *("--output-dir", str(tmp_path / "out")),
        *_FY,
        *_ITR,
        *extra,
    ]


def _file_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        *("--input", str(tmp_path / "in.csv"), "--config", str(tmp_path / "user.toml")),
        *("--output", str(tmp_path / "out" / "output.csv")),
        *extra,
    ]


# Each command other than init: its argv, and the function it calls first that
# can fail on a value, which the test makes raise.
_COMMANDS: dict[str, tuple[Callable[[Path], list[str]], str]] = {
    "schedule-fa": (lambda p: _file_args(p, "--calendar-year", "2025"), "run_fa_a3"),
    "netbenefits-fa": (lambda p: _file_args(p, "--calendar-year", "2025"), "run_netbenefits_fa"),
    "netbenefits-income": (_source_args, "run_netbenefits_income"),
    "netbenefits-cg": (_source_args, "run_netbenefits_cg"),
    "netbenefits-all": (_source_args, "netbenefits_form67_readiness"),
    "fidelity-cma-income": (
        lambda p: _source_args(p, "--tax-forms", str(p / "forms")),
        "run_fidelity_cma_income",
    ),
    "fidelity-cma-cg": (_source_args, "run_fidelity_cma_cg"),
    "fidelity-cma-fa": (
        lambda p: _file_args(p, "--calendar-year", "2025"),
        "run_fidelity_cma_fa",
    ),
    "stockplan-income": (_source_args, "run_stockplan_income"),
    "stockplan-fa": (lambda p: _file_args(p, "--calendar-year", "2025"), "run_stockplan_fa"),
    "stockplan-all": (_source_args, "stockplan_form67_readiness"),
    "robinhood-cg": (_source_args, "run_robinhood_cg"),
    "robinhood-income": (_source_args, "run_robinhood_income"),
    "robinhood-fa": (_source_args, "run_robinhood_fa"),
    "robinhood-options": (_source_args, "run_robinhood_options"),
    "robinhood-all": (_source_args, "run_robinhood_all"),
    "schedule-os": (lambda p: _file_args(p, *_FY), "run_schedule_os"),
    "schedule-cg": (lambda p: _file_args(p, *_FY, *_ITR), "run_schedule_cg"),
    "schedule-cg-ltcg": (lambda p: _file_args(p, *_FY, *_ITR), "run_schedule_cg_ltcg"),
    "schedule-fsi": (lambda p: _file_args(p, *_FY), "run_schedule_fsi"),
    "schedule-tr": (lambda p: _file_args(p, *_FY, *_ITR), "run_schedule_tr"),
    "form-67": (lambda p: _file_args(p, *_FY), "run_form67"),
    "run-all": (
        lambda p: [
            *("--fy-input", str(p / "in.csv"), "--config", str(p / "user.toml")),
            *("--output-dir", str(p / "out")),
            *_FY,
            *_ITR,
        ],
        "_compute_schedule_fsi_for_pipeline",
    ),
    "merge": (
        lambda p: [
            *("--fragment", str(p / "fragment.json"), "--itr", str(p / "itr.json")),
            *("--output", str(p / "out" / "merged.json")),
        ],
        "merge_cbdt_fragment",
    ),
}

# One instance of each kind of failure every command refuses with exit 4.
_FAILURES: dict[str, Callable[[], Exception]] = {
    "domain": lambda: IbkrFaError("the run refused"),
    "filesystem": lambda: PermissionError(errno.EACCES, "Permission denied", "in.csv"),
    "decimal": decimal.Overflow,
}


def test_every_command_but_init_is_covered() -> None:
    parser = cli._build_parser()
    [commands] = [
        action.choices
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert set(_COMMANDS) == set(commands) - {"init"}


@pytest.mark.parametrize("failure", _FAILURES)
@pytest.mark.parametrize("command", _COMMANDS)
def test_every_command_refuses_a_failure_with_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    failure: str,
) -> None:
    argv, entry = _COMMANDS[command]
    error = _FAILURES[failure]()

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    monkeypatch.setattr(cli, entry, fail)
    (tmp_path / "out").mkdir()

    assert main([command, *argv(tmp_path)]) == 4

    err = capsys.readouterr().err
    if failure == "decimal":
        assert err.endswith(_refused_run(command, decimal.Overflow))
    elif failure == "filesystem":
        # Fixed text and the errno name, never the OS text or file name (#1049).
        pointer = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n" if command.startswith("stockplan-") else ""
        assert err.endswith(f"error: {command}: a file operation failed (EACCES)\n{pointer}")
        assert "Permission denied" not in err
        assert "in.csv" not in err
    else:
        assert str(error) in err


def _catches_domain_errors(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.ExceptHandler)
        and ast.unparse(node.type or ast.Pass()) == "_DOMAIN_ERRORS"
        for node in ast.walk(function)
    )


def test_every_handler_refuses_through_the_shared_tuple() -> None:
    """Each command's handler (or the step or orchestrator it calls), and the
    internal stockplan-all CG leg and run-all preflight, catch
    ``cli._DOMAIN_ERRORS`` itself; no other except clause names
    ``IbkrFaError``, so no handler keeps a copy of the tuple."""
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    dispatch: dict[str, str] = {}  # command -> the function main dispatches it to
    for node in ast.walk(functions["_main"]):
        if isinstance(node, ast.If) and ast.unparse(node.test).startswith("args.command =="):
            assert isinstance(node.test, ast.Compare)
            [command] = node.test.comparators
            [dispatched] = node.body
            assert isinstance(command, ast.Constant)
            assert isinstance(command.value, str)
            assert isinstance(dispatched, ast.Return)
            call = dispatched.value
            assert isinstance(call, ast.Call)
            if ast.unparse(call.func) == "_stockplan_exit":  # adds the contract pointer
                [call] = call.args
                assert isinstance(call, ast.Call)
            assert isinstance(call.func, ast.Name)
            dispatch[command.value] = call.func.id
    assert set(dispatch) == set(_COMMANDS) | {"init"}

    def covered(name: str) -> bool:
        called = {
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        return any(
            _catches_domain_errors(functions[function])
            for function in (name, *called)
            if function in functions
        )

    assert [command for command, name in dispatch.items() if not covered(name)] == ["init"]
    assert _catches_domain_errors(functions["_run_stockplan_cg_status"])
    assert _catches_domain_errors(functions["_run_all_form67_preflight"])
    assert [
        ast.unparse(node.type)
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.type is not None
        and "IbkrFaError" in ast.unparse(node.type)
    ] == []
    assert (IbkrFaError, OSError, decimal.DecimalException) == cli._DOMAIN_ERRORS


# --- Orchestrators ------------------------------------------------------------


def _raise_overflow(*_args: object, **_kwargs: object) -> NoReturn:
    raise decimal.Overflow


def test_run_all_readiness_overflow_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_compute_schedule_fsi_for_pipeline", _raise_overflow)

    assert run_all_fy(tmp_path, FY_2025_26_INPUT) == 4

    assert capsys.readouterr().err == _refusal("run-all", decimal.Overflow)
    assert list((tmp_path / "out").iterdir()) == []


def test_run_all_leg_overflow_fails_the_run_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Schedule TR runs after OS, CG, CG-LTCG and FSI have written theirs."""
    monkeypatch.setattr(cli, "run_schedule_tr", _raise_overflow)

    assert run_all_fy(tmp_path, FY_2025_26_INPUT) == 4

    assert _refusal("schedule-tr", decimal.Overflow) in capsys.readouterr().err
    assert list((tmp_path / "out").iterdir()) == []


# The ids keep the module names pytest gave these cases before #1777.
@pytest.mark.parametrize(
    ("harness", "orchestrator", "readiness"),
    [
        pytest.param(
            NETBENEFITS_ALL,
            "netbenefits-all",
            "netbenefits_form67_readiness",
            id="tests.test_cli_netbenefits_all-netbenefits-all-netbenefits_form67_readiness",
        ),
        pytest.param(
            STOCKPLAN_ALL,
            "stockplan-all",
            "stockplan_form67_readiness",
            id="tests.test_cli_stockplan_all-stockplan-all-stockplan_form67_readiness",
        ),
    ],
)
def test_source_all_readiness_overflow_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    orchestrator: str,
    readiness: str,
) -> None:
    harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, readiness, _raise_overflow)

    rc, _calls, out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == 4
    assert capsys.readouterr().err == _refused_run(orchestrator, decimal.Overflow)
    assert list(out.iterdir()) == []


# The ids keep the module names pytest gave these cases before #1777.
@pytest.mark.parametrize(
    ("harness", "handler", "pipeline", "leg"),
    [
        pytest.param(
            NETBENEFITS_ALL,
            "_run_netbenefits_cg",
            "run_netbenefits_cg",
            "netbenefits-cg",
            id="tests.test_cli_netbenefits_all-_run_netbenefits_cg-run_netbenefits_cg-netbenefits-cg",
        ),
        pytest.param(
            STOCKPLAN_ALL,
            "_run_stockplan_fa",
            "run_stockplan_fa",
            "stockplan-fa",
            id="tests.test_cli_stockplan_all-_run_stockplan_fa-run_stockplan_fa-stockplan-fa",
        ),
    ],
)
def test_source_all_leg_overflow_fails_the_run_and_removes_every_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    harness: SourceAll,
    handler: str,
    pipeline: str,
    leg: str,
) -> None:
    """The other legs write their outputs; this leg's real handler runs and its
    pipeline overflows."""
    real = getattr(cli, handler)
    harness.patch_handlers(monkeypatch, harness.all_ok)
    monkeypatch.setattr(cli, handler, real)
    monkeypatch.setattr(cli, pipeline, _raise_overflow)

    rc, _calls, out = harness.run(tmp_path, monkeypatch, harness.all_ok, patch=False)

    assert rc == 4
    assert _refusal(leg, decimal.Overflow) in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_stockplan_all_cg_leg_overflow_fails_the_run_and_removes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CG leg runs after income and FA wrote their outputs."""
    real = cli._run_stockplan_cg_status
    STOCKPLAN_ALL.patch_handlers(monkeypatch, STOCKPLAN_ALL.all_ok)
    monkeypatch.setattr(cli, "_run_stockplan_cg_status", real)
    monkeypatch.setattr(cli, "inspect_stockplan_cg_evidence", _raise_overflow)

    rc, _calls, out = STOCKPLAN_ALL.run(tmp_path, monkeypatch, STOCKPLAN_ALL.all_ok, patch=False)

    assert rc == 4
    assert _refusal("stockplan-all", decimal.Overflow) in capsys.readouterr().err
    assert list(out.iterdir()) == []
