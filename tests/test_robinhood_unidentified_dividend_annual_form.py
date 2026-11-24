"""#1504: an unidentified Robinhood dividend's missing Form 1042-S is named on
the first run.

A dividend whose symbol resolves to no security gets an identity blocker and
no payment. Its account and Activity Date still decide whether its year needs
a Form 1042-S, so that year counts toward the annual-form requirement as it
would once the dividend is identified: the first run names the securities
remedy and the missing-form remedy together, and following both clears the
run in one re-run. The runs reuse #1487's harness (real income compute behind
the existing pipeline stubs) with synthetic inputs only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlockerCause,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
)
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from tests._no_echo import assert_no_markers
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_blockers import INCOME_BLOCKER_LINES as _LINES
from tests._robinhood_income_blockers import compute_income_case as _compute
from tests._robinhood_income_blockers import income_case as _case
from tests._robinhood_income_blockers import run_income_command as _run
from tests._robinhood_income_blockers import unconfigured_security_entry as _security_entry
from tests._robinhood_income_inputs import (
    INCOME_ACCOUNT,
    INCOME_CUSIP,
    INCOME_OTHER_ACCOUNT,
    INCOME_SYMBOL,
    income_base_events,
    income_config,
    income_event,
    income_packet,
)
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_refusals import OS_UNRESOLVED as _OS_REFUSAL

_Cause = RobinhoodIncomeBlockerCause
_Kind = RobinhoodIncomeBlockerKind
_K = RobinhoodEventKind
_IDENTITY_LINE = _LINES[_Cause.UNCONFIGURED_SECURITY]
_MISSING_FORM_LINE = _LINES[_Cause.ANNUAL_FORM_MISSING]
_UNCONFIGURED = replace(income_config(), securities=())
# The first account's only source income: a June 2025 dividend, with no tax
# withheld, in a security with no [[robinhood.securities]] entry, and no Form
# 1042-S. With no foreign tax the re-run has no Form 67 credit to compute,
# so a cleared run exits 0, as in #1497's case.
_FIRST_RUN = _case(income_base_events(with_tax=False), packets=(), config=_UNCONFIGURED)
# The form the missing-form line asks for: the dividend, nothing withheld.
_FORM = income_packet(tax="0.00", rate="0.00")


def _annual_form_blockers(income: RobinhoodIncomeResult) -> set[tuple[object, ...]]:
    """The blockers an annual-form year raises: a missing, set-aside or
    duplicated form, and evidence short of the form's year."""
    return {
        (blocker.kind, blocker.causes, blocker.account_indices, blocker.tax_years)
        for blocker in income.blockers
        if blocker.kind is _Kind.ANNUAL_FORM_COVERAGE
        or _Cause.EVIDENCE_SHORT_OF_FORM_YEAR in blocker.causes
    }


@pytest.mark.parametrize(
    ("day", "in_other_account", "expected"),
    [
        pytest.param(
            date(2025, 6, 16),
            False,
            {(_Kind.ANNUAL_FORM_COVERAGE, (_Cause.ANNUAL_FORM_MISSING,), (0,), (2025,))},
            id="in-fy",
        ),
        pytest.param(
            date(2025, 2, 10),
            False,
            {(_Kind.ANNUAL_FORM_COVERAGE, (_Cause.ANNUAL_FORM_MISSING,), (0,), (2025,))},
            id="fa-calendar-year-before-fy",
        ),
        pytest.param(date(2026, 2, 10), False, set(), id="fy-end-year"),
        pytest.param(date(2024, 12, 16), False, set(), id="before-fa-calendar-year"),
        pytest.param(
            date(2025, 6, 16),
            True,
            {
                (_Kind.ANNUAL_FORM_COVERAGE, (_Cause.ANNUAL_FORM_MISSING,), (1,), (2025,)),
                (_Kind.EVIDENCE_COVERAGE, (_Cause.EVIDENCE_SHORT_OF_FORM_YEAR,), (1,), (2025,)),
            },
            id="evidence-short-of-form-year",
        ),
    ],
)
def test_an_unidentified_dividend_needs_the_annual_form_an_identified_one_needs(
    day: date, in_other_account: bool, expected: set[tuple[object, ...]]
) -> None:
    """The same dividend, identified or not, raises the same annual-form
    blockers. In the second account the report starts at the FY start, short
    of 1 January of the form's year. On main the unidentified dividend raised
    none."""
    dividend = income_event(_K.CASH_DIVIDEND, 2, day, "100.00")
    buy = income_event(_K.EQUITY_BUY, 1, date(2024, 12, 2), "-1")
    rows: dict[str, Any] = (
        {"events": (buy,), "other_rows": (dividend,), "other_report_start": date(2025, 4, 1)}
        if in_other_account
        else {"events": (buy, dividend)}
    )

    def blockers(config: RobinhoodConfig) -> set[tuple[object, ...]]:
        return _annual_form_blockers(_compute(_case(packets=(), config=config, **rows)))

    identified = blockers(income_config())
    assert identified == expected
    assert blockers(_UNCONFIGURED) == identified


@pytest.mark.parametrize("command", ["robinhood-income", "robinhood-all"])
def test_the_first_run_names_the_securities_and_missing_form_remedies_once_each(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``robinhood-income`` names the missing form in its Schedule OS
    refusal after the identity line; ``robinhood-all`` on its ``income-ftc``
    leg, where its missing-form lines print. On main neither printed it."""
    code, out, err, _loaded = _run(command, _FIRST_RUN, tmp_path, monkeypatch, capsys)
    # robinhood-income's Schedule OS refusal prints on stderr; robinhood-all's
    # report, its leg refusals included, on stdout.
    output, other = (err, out) if command == "robinhood-income" else (out, err)

    assert code == 4
    assert other == ""
    assert output.count(_IDENTITY_LINE) == output.count(_MISSING_FORM_LINE) == 1, output
    if command == "robinhood-income":
        assert output.startswith(f"error: {_OS_REFUSAL}\n{_IDENTITY_LINE}\n")
        assert output.endswith(f"\n{_MISSING_FORM_LINE}\n")
    else:
        assert (
            "  error        income-ftc\n"
            "                 -> unresolved income blockers\n"
            f"{_MISSING_FORM_LINE}\n"
        ) in output
    assert_no_markers(out + err, INCOME_ACCOUNT, INCOME_OTHER_ACCOUNT, "100.00")


def test_following_every_printed_remedy_clears_the_run_in_one_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first run's remedies are the securities entry and the Form 1042-S,
    and it prints no other line: the statement's Dividends line counts the
    unidentified dividend (#1507). The entry, read by the real config loader,
    and the form, added together: the next ``robinhood-income`` run files
    Schedule OS and FTC."""
    config, paired, packets = _FIRST_RUN
    causes = {cause for blocker in _compute(_FIRST_RUN).blockers for cause in blocker.causes}
    assert causes == {_Cause.ANNUAL_FORM_MISSING, _Cause.UNCONFIGURED_SECURITY}
    assert all(core._INCOME_BLOCKER_TEXT[cause][1] is not None for cause in causes)
    assert packets == ()

    followed = replace(
        config,
        securities=(_security_entry(tmp_path, INCOME_SYMBOL, INCOME_CUSIP),),
    )
    assert _compute((followed, paired, (_FORM,))).blockers == ()

    _stub_income_bundle(monkeypatch, tmp_path, followed, paired, (_FORM,))

    code = _main("robinhood-income", tmp_path)

    output = capsys.readouterr()
    assert code == 0, output
    assert _OS_REFUSAL not in output.out + output.err
    assert {path.name for path in (tmp_path / "out").iterdir()} >= {
        "schedule-os.xlsx",
        "schedule-fsi.csv",
    }
