"""Each income command's advice for a blank Form 67 row (#1469).

``robinhood-income``, ``netbenefits-income`` and ``fidelity-cma-income`` run
their real income pipelines on #1177's smallest synthetic setups
(``tests/_form67_broker_runs.py``). At the income engine's input,
the run's dividends and withholding are replaced by the case's payments, and
every FX rate is ₹85.

The missing Indian tax warning and each Schedule FSI filing CSV "Manual fill
guidance" row offer an exact [[form67.tax_payable_overrides]] amount only for a
row one clears. A no-DTAA row's advice names the [form67.section_91] inputs
too, and a row no override clears gets its gap instead. A run whose only blank
row is a DTAA row an override alone clears prints today's text. Following the
no-DTAA advice in user.toml and rerunning files a section 91 credit, and the
override alone still refuses as before.
"""

from __future__ import annotations

import csv
import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

import ibkr_fa.pipeline_fidelity_cma_income as cma_income
import ibkr_fa.pipeline_netbenefits_income as netbenefits_income
import ibkr_fa.pipeline_robinhood_core as robinhood_core
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.types import Isin, Ticker
from tests._form67_broker_runs import (
    inject_income,
    run_fidelity_cma_income,
    run_netbenefits_income,
    run_robinhood,
)
from tests._form67_broker_runs import output_dir as _output_dir
from tests._ftc_warnings import CH_GAP as _CH_GAP
from tests._ftc_warnings import (
    KY_SECTION_91_NEED,
    KY_SECTION_91_REFUSAL,
    MISSING_INDIAN_TAX_WARNING,
    override_sentence,
    same_day_gap,
    wont_clear,
)
from tests._ftc_warnings import blank_row_warning as _warning
from tests._ibkr_form67_runs import KY_OVERRIDE as _KY_OVERRIDE
from tests._ibkr_form67_runs import SECTION_91_INPUTS as _SECTION_91

_ON = date(2025, 12, 15)
_US_TIN = 'US = "123456789"'
# TINs for the other countries, so a relief row's blank TIN adds no warning.
_OTHER_TINS = 'CH = "999-00-0041"\nKY = "999-00-0136"'


@dataclass(frozen=True)
class _Payment:
    """One synthetic dividend and the tax withheld on it, in USD."""

    isin: Isin
    gross: str
    tax: str


_US = Isin("US99995B1044")
_CH = Isin("CH99995C1002")  # CH has no vendored DTAA row (#249)
_KY = Isin("KY99995C1000")  # vendored as no_dtaa: a section 91 credit
_US_PAYMENT = _Payment(_US, "100.00", "25.00")
_CH_PAYMENT = _Payment(_CH, "100.00", "15.00")
_KY_PAYMENT = _Payment(_KY, "100.00", "10.00")
_CASES: dict[str, tuple[_Payment, ...]] = {
    "clearable": (_US_PAYMENT,),
    # #1469's example: two same-day dividends of one security, each withheld.
    "same-day": (_US_PAYMENT, _Payment(_US, "40.00", "10.00")),
    "unvendored": (_CH_PAYMENT,),
    "no-dtaa": (_KY_PAYMENT,),
    "mixed": (_US_PAYMENT, _CH_PAYMENT),
}

_SAME_DAY_GAP = same_day_gap("US/DIVIDEND")


# Per case: the run's missing Indian tax warning, then each Schedule FSI
# guidance row in file order (CBDT code order: US "2" before CH "41").
_ADVICE: dict[str, tuple[str, tuple[str, ...]]] = {
    "clearable": (MISSING_INDIAN_TAX_WARNING, (MISSING_INDIAN_TAX_WARNING,)),
    "same-day": (
        _warning(wont_clear(_SAME_DAY_GAP, "US/DIVIDEND")),
        (_warning(wont_clear(_SAME_DAY_GAP, "US/DIVIDEND")),),
    ),
    "unvendored": (
        _warning(wont_clear(_CH_GAP, "CH/DIVIDEND")),
        (_warning(wont_clear(_CH_GAP, "CH/DIVIDEND")),),
    ),
    "no-dtaa": (
        _warning(override_sentence("KY"), KY_SECTION_91_NEED),
        (_warning(override_sentence("KY"), KY_SECTION_91_NEED),),
    ),
    "mixed": (
        _warning(override_sentence("US"), wont_clear(_CH_GAP, "CH/DIVIDEND")),
        (MISSING_INDIAN_TAX_WARNING, _warning(wont_clear(_CH_GAP, "CH/DIVIDEND"))),
    ),
}


def _inject(monkeypatch: pytest.MonkeyPatch, engine: ModuleType, case: str) -> None:
    """Replace the dividends and withholding where ``engine`` calls the income
    engine with the case's payments, and price every dividend, withholding and
    capital gain at ₹85."""
    payments = _CASES[case]
    inject_income(
        monkeypatch,
        engine,
        dividends=[
            Dividend(
                symbol=Ticker(f"X{payment.isin[:2]}"),
                isin=payment.isin,
                payment_date=_ON,
                amount_gross=Decimal(payment.gross),
                currency="USD",
                description=f"X{payment.isin[:2]} synthetic dividend",
                kind=DistributionKind.CASH_DIVIDEND,
                type_slice="Cash Dividend",
            )
            for payment in payments
        ],
        withholding_taxes=[
            WithholdingTax(
                symbol=Ticker(f"X{payment.isin[:2]}"),
                isin=payment.isin,
                payment_date=_ON,
                amount=Decimal(payment.tax).copy_negate(),
                currency="USD",
                description=f"X{payment.isin[:2]} synthetic withholding",
            )
            for payment in payments
        ],
        rate=Decimal("85"),
    )


def _config(base: str, form67: str) -> str:
    """``base`` with the other countries' TINs beside its US one, and ``form67``."""
    assert base.count(_US_TIN) == 1
    return base.replace(_US_TIN, f"{_US_TIN}\n{_OTHER_TINS}") + form67


# --- Each command's smallest existing synthetic run ----------------------------
# Each takes (tmp_path, monkeypatch, form67 TOML), writes to tmp_path / "out" and
# returns the CLI exit code.


def _robinhood_income(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form67: str) -> int:
    return run_robinhood(
        tmp_path,
        monkeypatch,
        functools.partial(_config, form67=form67),
        command="robinhood-income",
        country="US",
    )


def _netbenefits_income(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form67: str) -> int:
    return run_netbenefits_income(tmp_path, monkeypatch, functools.partial(_config, form67=form67))


def _fidelity_cma_income(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form67: str) -> int:
    return run_fidelity_cma_income(tmp_path, monkeypatch, functools.partial(_config, form67=form67))


@dataclass(frozen=True)
class _Command:
    name: str
    run: Callable[[Path, pytest.MonkeyPatch, str], int]
    engine: ModuleType


_COMMANDS = (
    _Command("robinhood-income", _robinhood_income, robinhood_core),
    _Command("netbenefits-income", _netbenefits_income, netbenefits_income),
    _Command("fidelity-cma-income", _fidelity_cma_income, cma_income),
)
_COMMAND_IDS = [command.name for command in _COMMANDS]


def _guidance(out: Path) -> tuple[str, ...]:
    """The Schedule FSI filing CSV's "Manual fill guidance" texts, in file order."""
    with (out / "schedule-fsi.csv").open(newline="", encoding="utf-8") as handle:
        return tuple(row[1] for row in csv.reader(handle) if row[:1] == ["Manual fill guidance"])


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
@pytest.mark.parametrize("case", list(_CASES))
def test_blank_row_advice_offers_an_override_only_where_one_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
    case: str,
) -> None:
    """The run prints the missing Indian tax warning once, with the case's
    per-row advice, and each Schedule FSI row's guidance gives only that row's
    own advice. A row no override clears is never offered one."""
    out = _output_dir(tmp_path)
    _inject(monkeypatch, command.engine, case)

    command.run(tmp_path, monkeypatch, "")

    captured = capsys.readouterr()
    warning, guidance = _ADVICE[case]
    lines = [
        line
        for line in (captured.out + captured.err).splitlines()
        if "INDIAN TAX CALCULATION REQUIRED" in line
    ]
    assert len(lines) == 1
    assert lines[0].endswith(warning)
    assert _guidance(out) == guidance


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
def test_following_the_no_dtaa_advice_files_a_section_91_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
) -> None:
    """The KY advice asks for the override and both [form67.section_91] inputs.
    With exactly those in user.toml, the rerun files KY's section 91 credit:
    the lower of the ₹850 withheld and 30% of the ₹8,500 dividend."""
    out = _output_dir(tmp_path)
    _inject(monkeypatch, command.engine, "no-dtaa")

    rc = command.run(tmp_path, monkeypatch, _KY_OVERRIDE + _SECTION_91)

    captured = capsys.readouterr()
    assert rc != 4
    assert "INDIAN TAX CALCULATION REQUIRED" not in captured.out + captured.err
    with (out / "form-67.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[1:] == [
        ["1", "1345", "", "7", "", "8500", "850", "10.00", "3000", "", "", "", "", "850", "850"]
    ]


@pytest.mark.parametrize("command", _COMMANDS, ids=_COMMAND_IDS)
def test_no_dtaa_override_alone_still_refuses_for_the_section_91_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: _Command,
) -> None:
    """As before #1469: the KY override without [form67.section_91] refuses."""
    out = _output_dir(tmp_path)
    _inject(monkeypatch, command.engine, "no-dtaa")

    rc = command.run(tmp_path, monkeypatch, _KY_OVERRIDE)

    captured = capsys.readouterr()
    assert rc == 4
    assert KY_SECTION_91_REFUSAL in captured.err
    assert list(out.iterdir()) == []


def test_robinhood_action_names_the_section_91_inputs_for_a_no_dtaa_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Robinhood's own BLANK Form 67 action (#1461) offers the KY override, and
    now names the [form67.section_91] inputs its section 91 credit needs too."""
    _output_dir(tmp_path)
    _inject(monkeypatch, robinhood_core, "no-dtaa")

    _robinhood_income(tmp_path, monkeypatch, "")

    captured = capsys.readouterr()
    (action,) = [line for line in captured.out.splitlines() if "form-67.csv is withheld" in line]
    assert action.endswith(
        "one or more Form 67 entries lack Indian tax payable and credit, so form-67.csv is "
        "withheld; add an exact [[form67.tax_payable_overrides]] amount for each country and "
        "source of income and rerun, or enter Form 67 on the portal by hand using "
        f"form-67-debug.csv as a reference. {KY_SECTION_91_NEED}"
    )
