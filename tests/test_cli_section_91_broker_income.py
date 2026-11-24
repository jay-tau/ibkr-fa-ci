"""Broker income and combined commands with the exact section 91 credit (#1177).

``robinhood-income``, ``netbenefits-income``, ``stockplan-income``,
``fidelity-cma-income`` and the combined ``robinhood-all``, ``netbenefits-all``
and ``stockplan-all`` each run their real income pipeline on the smallest
synthetic setup an existing CLI test of that command uses. At the income
engine's input, the run's dividends and withholding are replaced by one
synthetic Bermuda (no-treaty) dividend and its withholding, and the FX policies
the engine builds return the case's flat rate. The ``[form67]`` BM DIVIDEND
override and ``[form67.section_91]`` come from the run's user.toml. Robinhood's
loader is stubbed, as in its CLI tests, so its config is loaded from that same
file.

Each of these commands checks Form 67 readiness, then the Schedule FSI portal
rule, before it writes anything. The combined commands do so in a preflight
before any leg runs, so a refusal prints no leg header or summary. The
ordinary repro refuses at the portal preflight, the product correction newly
refuses at the corrected portal check, and the two readiness cases refuse at
readiness. Each prints its complete fixed message for country input row 1
after the command's own prefix and leaves no output. A repeating average rate
that files the same ₹3 either way succeeds.

Only Robinhood can configure a non-US security. NetBenefits requires a US
ISIN, StockPlan accepts only GOOG and the Fidelity CMA reads its US money
market fund, so a Bermuda payment reaches those commands only through the
injection at the engine's input.
"""

from __future__ import annotations

import csv
import functools
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

import ibkr_fa.pipeline_fidelity_cma_income as cma_income
import ibkr_fa.pipeline_netbenefits_income as netbenefits_income
import ibkr_fa.pipeline_robinhood_core as robinhood_core
import ibkr_fa.pipeline_robinhood_outputs as robinhood_outputs
import ibkr_fa.pipeline_stockplan_income as stockplan_income
from ibkr_fa import cli
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.output.income_bundle import INCOME_FILENAMES
from ibkr_fa.pipeline_stockplan_income import _ZERO_SALES_UNPROVEN
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_no_markers
from tests._form67_broker_runs import (
    inject_income,
    run_fidelity_cma_income,
    run_netbenefits_income,
    run_robinhood,
)
from tests._form67_broker_runs import output_dir as _output_dir
from tests._netbenefits_income import INCOME_CONFIG as _NETBENEFITS_CONFIG
from tests._section_91_inputs import (
    _BM_ISIN,
    _DESCRIPTION,
    _EXACT_PORTAL,
    _EXACT_ZERO,
    _MARKERS,
    _ON,
    _ORDINARY,
    _PREFIX,
    _PREFLIGHT_PORTAL,
    _PREFLIGHT_ZERO,
    _PRODUCT,
    _READINESS,
    _SYMBOL,
    _Country,
    _wht,
)
from tests._section_91_scenarios import _READINESS_INCOME, _READINESS_TAX
from tests._source_all import SUMMARY_FOOTER as _SUMMARY_FOOTER
from tests._source_all import run_netbenefits_all_real_income as _run_netbenefits_all
from tests._source_all import run_stockplan_all_real_income as _run_stockplan_all
from tests._stockplan_income import STOCKPLAN_INCOME_CONFIG as _STOCKPLAN_CONFIG
from tests._stockplan_income import run_income_cli as _run_stockplan_income
from tests._stockplan_income import write_dividend_report as _write_report

_US_TIN = 'US = "123456789"'


@dataclass(frozen=True)
class _Case:
    """One synthetic Bermuda payment and the run's section 91 inputs."""

    country: _Country
    tax: str
    income: str
    fx: str


_CASES = {
    "ordinary": _Case(_ORDINARY, "300000", "2800000", "70"),
    "product": _Case(_PRODUCT, "0.25", "1", "1"),
    "readiness": _Case(_READINESS, _READINESS_TAX, _READINESS_INCOME, "1"),
    "control": _Case(_ORDINARY, "100000", "2800000", "70"),
}
# Readiness runs first, so its two refusals win; the portal rule refuses the
# other two (the ordinary repro by the former credit, the product by the exact one).
_REFUSALS = {
    "ordinary": _PREFLIGHT_PORTAL,
    "product": _EXACT_PORTAL,
    "readiness": _EXACT_ZERO,
    "control": _PREFLIGHT_ZERO,
}
# 1 x 10 / 3 = 3.33..., below the ₹9 foreign tax: ₹3 under either formula.
_REPEATING = _Case(_Country(gross="10", wht="-9", payable="3"), "1", "3", "1")


def _form67_toml(case: _Case) -> str:
    return f"""
[form67]
[[form67.tax_payable_overrides]]
country = "BM"
source = "DIVIDEND"
tax_payable_normal_inr = "{case.country.payable}"

[form67.section_91]
total_indian_tax_before_91_relief_inr = "{case.tax}"
total_income_inr = "{case.income}"
"""


def _config(base: str, case: _Case) -> str:
    """``base`` with a Bermuda TIN beside its US one, so the relief row is
    complete, and the case's ``[form67]`` tables."""
    assert base.count(_US_TIN) == 1
    return base.replace(_US_TIN, f'{_US_TIN}\nBM = "999-00-0091"') + _form67_toml(case)


def _dividend(country: _Country) -> Dividend:
    return Dividend(
        symbol=Ticker(_SYMBOL),
        isin=_BM_ISIN,
        payment_date=_ON,
        amount_gross=Decimal(country.gross),
        currency="USD",
        description=_DESCRIPTION,
        kind=DistributionKind.CASH_DIVIDEND,
        type_slice="Cash Dividend",
    )


def _inject(monkeypatch: pytest.MonkeyPatch, engine: ModuleType, case: _Case) -> None:
    """Replace the dividends and withholding where ``engine`` calls the income
    engine with the case's Bermuda payment, and price every dividend,
    withholding and capital gain at ``case.fx``."""
    inject_income(
        monkeypatch,
        engine,
        dividends=[_dividend(case.country)],
        withholding_taxes=[_wht(case.country)],
        rate=Decimal(case.fx),
    )


# --- Each command's smallest existing synthetic run ----------------------------
# Each takes (tmp_path, monkeypatch, case), uses tmp_path / "out" as its output
# directory, and returns the CLI exit code.


def _robinhood(command: str) -> Callable[[Path, pytest.MonkeyPatch, _Case], int]:
    def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
        return run_robinhood(
            tmp_path,
            monkeypatch,
            functools.partial(_config, case=case),
            command=command,
            country="BM",
        )

    return run


def _netbenefits_income(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
    return run_netbenefits_income(tmp_path, monkeypatch, functools.partial(_config, case=case))


def _netbenefits_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
    # The FA and CG legs are stubbed; the readiness preflight and income leg are real.
    config = _config(_NETBENEFITS_CONFIG, case)
    return _run_netbenefits_all(tmp_path, monkeypatch, config=config, stub_income=False)[0]


def _stockplan_income(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
    _write_report(tmp_path / "in")
    config = tmp_path / "user.toml"
    config.write_text(_config(_STOCKPLAN_CONFIG, case), encoding="utf-8")
    return _run_stockplan_income(tmp_path / "in", config, tmp_path / "out")


def _stockplan_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
    # The FA and CG legs are stubbed; the readiness preflight and income leg are real.
    config = _config(_STOCKPLAN_CONFIG, case)
    return _run_stockplan_all(tmp_path, monkeypatch, config=config, stub_income=False)[0]


def _fidelity_cma_income(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _Case) -> int:
    return run_fidelity_cma_income(tmp_path, monkeypatch, functools.partial(_config, case=case))


def _summary(orchestrator: str, *legs: tuple[str, str]) -> str:
    """A source ``*-all`` run's closing stderr summary of ``(status, leg)`` lines."""
    return "\n".join(
        (
            cli._RUN_ALL_RULE,
            f"  {orchestrator} summary (FY 2025-26)",
            cli._RUN_ALL_RULE,
            *(f"  {cli._RUN_ALL_STATUS_LABEL[status]}  {leg}" for status, leg in legs),
            _SUMMARY_FOOTER,
            "",
        )
    )


@dataclass(frozen=True)
class _Command:
    """How to run one command, and what its refusals and clean runs print."""

    name: str
    run: Callable[[Path, pytest.MonkeyPatch, _Case], int]
    engine: ModuleType
    owned: tuple[str, ...]
    clean_exit: int = 0
    clean_err: str = ""
    prefix: str = ""
    trailer: str = ""


_STOCKPLAN_TRAILER = f"{cli._STOCKPLAN_CONTRACT_POINTER}\n"
_COMMANDS = (
    _Command(
        "robinhood-income",
        _robinhood("robinhood-income"),
        robinhood_core,
        (
            *robinhood_outputs.OS_FILENAMES,
            *robinhood_outputs.INCOME_FILENAMES,
            robinhood_outputs.IDENTITY_GUIDANCE_FILENAME,
        ),
    ),
    _Command(
        "robinhood-all",
        _robinhood("robinhood-all"),
        robinhood_core,
        (*robinhood_outputs.ALL_FILENAMES, robinhood_outputs.IDENTITY_GUIDANCE_FILENAME),
    ),
    _Command("netbenefits-income", _netbenefits_income, netbenefits_income, INCOME_FILENAMES),
    _Command(
        "netbenefits-all",
        _netbenefits_all,
        netbenefits_income,
        tuple(name for names in cli._NETBENEFITS_ALL_TARGETS.values() for name in names),
        clean_err=_summary(
            "netbenefits-all",
            ("ok", "netbenefits-fa"),
            ("ok", "netbenefits-income"),
            ("ok", "netbenefits-cg"),
        ),
        prefix="netbenefits-all: ",
    ),
    # A standalone run can't prove zero sales, so even a clean one is manual fill (#882).
    _Command(
        "stockplan-income",
        _stockplan_income,
        stockplan_income,
        INCOME_FILENAMES,
        clean_exit=3,
        clean_err=f"warning: {_ZERO_SALES_UNPROVEN}\n",
        trailer=_STOCKPLAN_TRAILER,
    ),
    # Table A2 is always manual fill.
    _Command(
        "stockplan-all",
        _stockplan_all,
        stockplan_income,
        tuple(name for names in cli._STOCKPLAN_ALL_TARGETS.values() for name in names),
        clean_exit=3,
        clean_err=_summary(
            "stockplan-all",
            ("ok", "stockplan-income"),
            ("ok", "stockplan-fa"),
            ("ok", "Schedule CG"),
            ("manual_fill", "Schedule FA Table A2 (complete daily cash/NAV evidence unavailable)"),
        ),
        prefix="stockplan-all: ",
        trailer=_STOCKPLAN_TRAILER,
    ),
    # Unchanged since 1f885b5: with eligible US withholding, the CMA run calls any
    # foreign-tax Schedule FSI row without a DTAA article incomplete, and a
    # no-treaty row has none, so the run is manual fill.
    _Command(
        "fidelity-cma-income",
        _fidelity_cma_income,
        cma_income,
        INCOME_FILENAMES,
        clean_exit=3,
        clean_err=(
            "warning: FSI/Form 67 Indian-tax and relief columns are incomplete; provide an "
            "exact per-source override and, for §91, the statutory average-rate inputs\n"
        ),
    ),
)
_COMMAND_IDS = [command.name for command in _COMMANDS]


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
@pytest.mark.parametrize("case", list(_REFUSALS))
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
def test_section_91_refusal_prints_its_fixed_message_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
    case: str,
    stale: bool,
) -> None:
    """Exit 4 with exactly the command's error line for the case's fixed message
    (and StockPlan's contract pointer), nothing on stdout (so no combined-command
    leg ran), no marker, and an empty output directory: a stale copy of every
    file the command clears is gone too.

    Fails on 1f885b5 for every case: the ordinary repro refused with the old
    portal text, the control with today's zero-credit ``ConfigError``, and the
    product and readiness inputs passed (₹2 and ₹1 filed).
    """
    out = _output_dir(tmp_path)
    if stale:
        for name in command.owned:
            (out / name).write_text("stale", encoding="utf-8")
    _inject(monkeypatch, command.engine, _CASES[case])

    rc = command.run(tmp_path, monkeypatch, _CASES[case])

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == (
        f"error: {command.prefix}{_PREFIX} 1, {_REFUSALS[case]}\n{command.trailer}"
    )
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_MARKERS)
    assert list(out.iterdir()) == []


# Indian tax of ₹0 on the dividend and before section 91 relief: the exact
# credit, and so the Schedule FSI relief, is exactly ₹0 (#1359).
_EXACTLY_ZERO = _Case(_Country(gross="10", wht="-9", payable="0"), "0", "2800000", "70")


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
def test_exactly_zero_relief_still_refuses_before_any_schedule_tr_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
) -> None:
    """A relief of exactly ₹0, which no command files (#1491), never reaches
    these commands' Schedule TR file: each refuses the zero credit at its
    Form 67 readiness check (#601) with the fixed preflight message before
    writing anything."""
    out = _output_dir(tmp_path)
    _inject(monkeypatch, command.engine, _EXACTLY_ZERO)

    rc = command.run(tmp_path, monkeypatch, _EXACTLY_ZERO)

    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err == (
        f"error: {command.prefix}{_PREFIX} 1, {_PREFLIGHT_ZERO}\n{command.trailer}"
    )
    assert captured.out == ""
    assert_no_markers(captured.out + captured.err, *_MARKERS)
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
def test_repeating_average_rate_files_the_three_rupee_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
) -> None:
    """A repeating average rate (1/3) is no reason to refuse: the run exits and
    prints to stderr exactly as a clean run of that command does, form-67.csv
    files ₹3 in both credit columns (min(₹9 tax paid, 1 x 10 / 3)), and no
    marker is printed. Passes on 1f885b5 too: ₹3 either way."""
    out = _output_dir(tmp_path)
    _inject(monkeypatch, command.engine, _REPEATING)

    rc = command.run(tmp_path, monkeypatch, _REPEATING)

    captured = capsys.readouterr()
    assert rc == command.clean_exit
    assert captured.err == command.clean_err
    with (out / "form-67.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[1:] == [
        ["1", "1441", "", "7", "", "10", "9", "90.00", "3", "", "", "", "", "3", "3"]
    ]
    assert_no_markers(captured.out + captured.err, *_MARKERS)
