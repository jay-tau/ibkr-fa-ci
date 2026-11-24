"""#1487: each Robinhood income blocker names its accounts, years, cause and remedy.

Before, ``robinhood-income`` and ``robinhood-all`` printed only a fixed line
for eleven of the thirteen income blocker kinds. Now each cause gets one
line: the accounts (by statement family) and tax years it covers, what is
unresolved, and the file or setting that clears it, or which schedules to
complete by hand. A blocker that stops Schedule OS is named in the Schedule
OS refusal both commands print; one that stops only FTC is named in the FTC
reasons. Each case, from ``tests/_robinhood_income_blockers.py``, runs the
real income compute behind the existing pipeline stubs, as #1477's tests do,
with synthetic inputs only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ibkr_fa.config as config_module
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.pipeline_robinhood_bundle import _tax_form_paths
from tests._no_echo import assert_no_markers
from tests._robinhood_income_blockers import FILLED_PLACEHOLDERS as _FILLED
from tests._robinhood_income_blockers import INCOME_BLOCKER_CASES as _CASES
from tests._robinhood_income_blockers import INCOME_BLOCKER_LINES as _LINES
from tests._robinhood_income_blockers import INCOME_BUY as _BUY
from tests._robinhood_income_blockers import OS_BY_HAND as _OS
from tests._robinhood_income_blockers import REMEDY_ACCOUNT as _ACCOUNT
from tests._robinhood_income_blockers import STATEMENT_MARKER as _MARKER
from tests._robinhood_income_blockers import STATEMENT_MARKER_DELTA as _MARKER_DELTA
from tests._robinhood_income_blockers import compute_income_case as _compute
from tests._robinhood_income_blockers import income_blocker_remedy as _remedy
from tests._robinhood_income_blockers import income_case as _case
from tests._robinhood_income_blockers import loaded_remedy_entry as _loaded
from tests._robinhood_income_blockers import run_income_command as _run
from tests._robinhood_income_inputs import (
    CGD_LABEL,
    INCOME_ACCOUNT,
    INCOME_CUSIP,
    INCOME_ISIN,
    INCOME_OTHER_ACCOUNT,
    INCOME_PAYMENT_DATE,
    INCOME_SYMBOL,
    income_base_events,
    income_config,
    income_event,
    income_packet,
    income_paired,
)
from tests._robinhood_refusals import OS_UNRESOLVED
from tests._robinhood_remedies import lending_fee_entry, security_entry

_Cause = RobinhoodIncomeBlockerCause
_Kind = RobinhoodIncomeBlockerKind
_K = RobinhoodEventKind
_event = income_event
_packet = income_packet
_DAY = INCOME_PAYMENT_DATE


def _kind_of(cause: RobinhoodIncomeBlockerCause) -> RobinhoodIncomeBlockerKind:
    return _Kind(_LINES[cause].partition(" ")[0].partition(":")[0])


def test_every_cause_has_a_text_and_a_case() -> None:
    assert set(core._INCOME_BLOCKER_TEXT) == set(_Cause)
    assert set(_LINES) == set(_CASES) == set(_Cause)


@pytest.mark.parametrize("cause", _Cause)
def test_each_case_raises_its_cause(cause: RobinhoodIncomeBlockerCause) -> None:
    """Each case raises the blocker it is for. An FTC-only cause's case stops
    nothing else, so robinhood-income reaches its FTC reasons."""
    income = _compute(_CASES[cause]())
    raised = [blocker for blocker in income.blockers if cause in blocker.causes]

    assert raised
    assert {blocker.kind for blocker in raised} == {_kind_of(cause)}
    if not raised[0].stops_schedule_os:
        assert income.schedule_os_complete


@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
@pytest.mark.parametrize("cause", _Cause)
def test_both_commands_print_each_cause_once(
    cause: RobinhoodIncomeBlockerCause,
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each cause's line, once, beside the fixed line that was all main
    printed; the exit code is main's. No amount or account number."""
    code, out, err, _loaded = _run(command, _CASES[cause](), tmp_path, monkeypatch, capsys)
    os_refused = not _compute(_CASES[cause]()).schedule_os_complete
    # Only robinhood-income's Schedule OS refusal prints on stderr; its
    # income-ftc report and robinhood-all's, refusals included, print on stdout.
    output, other = (err, out) if os_refused and command == "robinhood-income" else (out, err)

    assert other == ""
    assert output.count(_LINES[cause]) == 1, output
    assert code == 4
    if os_refused:
        fixed = OS_UNRESOLVED
    elif command == "robinhood-all":
        fixed = "unresolved income blockers"
    else:
        fixed = "FTC schedules remain blocked"
    assert fixed in output
    assert_no_markers(out + err, _MARKER, "4,321.09", INCOME_ACCOUNT, INCOME_OTHER_ACCOUNT)


@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
def test_both_commands_describe_unsupported_form_conditions(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An amendment refuses even with supported income code, rate and amounts."""
    code, out, err, _loaded = _run(
        command, _case(packets=(_packet(amended=True),)), tmp_path, monkeypatch, capsys
    )

    # Both commands print their report, refusals included, on stdout.
    assert err == ""
    assert out.count(_LINES[_Cause.UNSUPPORTED_FORM_CONTENT]) == 1, out
    assert code == 4


def test_one_line_per_cause_however_many_blockers_raise_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Capital Gains Distributions in each account's statement for a
    different year: two blockers, one line naming both accounts and years."""
    case = _case(
        statement_deltas={
            (INCOME_ACCOUNT, date(2025, 1, 1), CGD_LABEL): _MARKER_DELTA,
            (INCOME_OTHER_ACCOUNT, date(2026, 1, 1), CGD_LABEL): _MARKER_DELTA,
        }
    )
    assert len(_compute(case).blockers) == 2
    line = (
        "unsupported-income (Individual and Managed Individual accounts, 2025 and 2026): a "
        "statement's Income summary shows Capital Gains Distributions, which ibkr-fa doesn't "
        f"compute{_OS}"
    )
    for command in ("robinhood-income", "robinhood-all"):
        _code, out, err, _loaded = _run(command, case, tmp_path, monkeypatch, capsys)
        # robinhood-income's Schedule OS refusal prints on stderr; robinhood-all's
        # report, its leg refusals included, on stdout.
        output, other = (err, out) if command == "robinhood-income" else (out, err)
        assert other == ""
        assert output.count(line) == 1
        assert output.count("unsupported-income") == 1


def test_a_blocker_raised_for_two_causes_stays_one_blocker() -> None:
    """One statement's Dividends and Interest Earned lines both disagree: one
    blocker, as before, carrying both causes, and a line for each."""
    deltas = {
        (INCOME_ACCOUNT, date(2025, 1, 1), label): _MARKER_DELTA
        for label in ("Dividends", "Interest Earned")
    }
    income = _compute(_case(statement_deltas=deltas))

    (blocker,) = income.blockers
    assert blocker.causes == (_Cause.STATEMENT_DIVIDENDS, _Cause.STATEMENT_INTEREST)
    assert core.income_blocker_reasons(income, {0: "Individual"}, schedule_os=True) == (
        _LINES[_Cause.STATEMENT_DIVIDENDS],
        _LINES[_Cause.STATEMENT_INTEREST],
    )
    assert core.income_blocker_reasons(income, {0: "Individual"}, schedule_os=False) == ()


def test_accounts_sharing_a_family_are_named_with_their_index() -> None:
    """As Schedule OS names them when both accounts share a statement family."""
    blocker = RobinhoodIncomeBlocker(
        kind=_Kind.UNSUPPORTED_INCOME,
        account_indices=(0, 1),
        tax_years=(2025,),
        causes=(_Cause.CAPITAL_GAINS_DISTRIBUTIONS,),
    )
    income = cast("Any", SimpleNamespace(blockers=(blocker,)))

    (line,) = core.income_blocker_reasons(
        income, {0: "Individual", 1: "Individual"}, schedule_os=True
    )
    assert line.startswith(
        "unsupported-income (Individual (index 0) and Individual (index 1) accounts, 2025): "
    )


def test_a_blocker_with_no_cause_refuses_to_render() -> None:
    income = cast(
        "Any", SimpleNamespace(blockers=(RobinhoodIncomeBlocker(kind=_Kind.JURISDICTION),))
    )

    with pytest.raises(InvariantError, match="names no cause"):
        core.income_blocker_reasons(income, {}, schedule_os=False)


# Followed remedies: each named file or setting clears its blocker on a rerun.


def test_followed_lending_fee_source_remedy_loads_and_files_the_fee(tmp_path: Path) -> None:
    """The entry the remedy spells out, every placeholder filled in, loads
    and clears the blocker; the fee is then filed."""
    lending = _event(_K.STOCK_LENDING_INCOME, 2, _DAY, "10.00")
    config = income_config(other_account=_ACCOUNT)

    def compute(config: RobinhoodConfig) -> RobinhoodIncomeResult:
        return _compute(
            (
                config,
                income_paired((_BUY,), other_rows=(lending,), other_account=_ACCOUNT),
                (),
            )
        )

    (blocker,) = compute(config).blockers
    assert blocker.causes == (_Cause.LENDING_FEE_SOURCE,)

    text, keys = lending_fee_entry(_remedy(_Cause.LENDING_FEE_SOURCE), _FILLED)
    assert set(keys) == config_module._KNOWN_ROBINHOOD_STOCK_LENDING_FEE_FSI_SOURCE
    entry = _loaded(tmp_path, "stock_lending_fee_fsi_sources", text)

    rerun = compute(replace(config, stock_lending_fee_fsi_sources=(entry,)))
    assert rerun.blockers == ()
    assert Decimal("10.00") in {item.amount_gross for item in rerun.filing_other_source_incomes}


def test_followed_security_remedy_loads_and_identifies_the_payment(tmp_path: Path) -> None:
    """An entry with exactly the keys the remedy lists loads and identifies
    the dividend, so neither it nor its withholding blocks any more."""
    case = _CASES[_Cause.UNCONFIGURED_SECURITY]()
    assert any(_Cause.UNCONFIGURED_SECURITY in item.causes for item in _compute(case).blockers)

    text, keys = security_entry(
        _remedy(_Cause.UNCONFIGURED_SECURITY),
        INCOME_SYMBOL,
        INCOME_CUSIP,
        INCOME_ISIN,
    )
    assert set(keys) == config_module._KNOWN_ROBINHOOD_SECURITY
    entry = _loaded(tmp_path, "securities", text)

    config, paired, packets = case
    assert _compute((replace(config, securities=(entry,)), paired, packets)).blockers == ()


def test_followed_missing_form_remedy_clears_the_blocker_and_both_commands_read_the_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The loader reads tax forms from the folder the remedy names, both
    commands ask it to, and with the form the account/year is complete."""
    assert "the input's tax-forms folder" in _remedy(_Cause.ANNUAL_FORM_MISSING)
    form = tmp_path / "in" / "tax-forms" / "form-1042-s.pdf"
    form.parent.mkdir(parents=True)
    form.write_bytes(b"%PDF-1.4 synthetic\n")
    assert _tax_form_paths(tmp_path / "in") == (form,)

    for command in ("robinhood-income", "robinhood-all"):
        _code, _out, _err, loaded = _run(
            command, _CASES[_Cause.ANNUAL_FORM_MISSING](), tmp_path, monkeypatch, capsys
        )
        assert loaded["include_tax_forms"] is True

    config, paired, _packets = _CASES[_Cause.ANNUAL_FORM_MISSING]()
    assert _compute((config, paired, (_packet(),))).blockers == ()


def test_followed_form_year_coverage_remedy_clears_the_blocker() -> None:
    """A 2026 form for 2026 interest: evidence through March 2026 is short of
    the form's year; through 31 December 2026 it reconciles."""
    assert "[[robinhood.reports]] end_date" in _remedy(_Cause.EVIDENCE_SHORT_OF_FORM_YEAR)
    assert "end_date" in config_module._KNOWN_ROBINHOOD_REPORT
    events = (
        *income_base_events(),
        _event(_K.CASH_INTEREST, 4, date(2026, 2, 10), "7.00"),
    )
    packets = (
        _packet(),
        _packet(tax_year=2026, income_code="01", gross="7.00", tax="0.00", rate="0.00"),
    )

    short = _compute(_case(events, packets))
    (blocker,) = short.blockers
    assert (blocker.causes, blocker.tax_years) == ((_Cause.EVIDENCE_SHORT_OF_FORM_YEAR,), (2026,))

    followed = _case(events, packets, extra_period=(date(2026, 4, 1), date(2026, 12, 31)))
    assert _compute(followed).blockers == ()


def test_followed_financial_year_coverage_remedy_clears_the_blocker() -> None:
    assert "start_date and end_date" in _remedy(_Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR)
    assert {"start_date", "end_date"} <= config_module._KNOWN_ROBINHOOD_REPORT
    short = _compute(_CASES[_Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR]())
    assert {cause for item in short.blockers for cause in item.causes} >= {
        _Cause.EVIDENCE_SHORT_OF_FINANCIAL_YEAR
    }

    assert _compute(_case()).blockers == ()
