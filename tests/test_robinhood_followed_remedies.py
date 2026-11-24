"""#1470: each Robinhood remedy, followed word for word, gets the rerun past it.

The interest-payer and option-classification remedies each spell out a
config entry: following one builds that entry with every placeholder
filled in, loads it with the real loader and reruns the step that refused.
The option-classification refusal also names the option blockers the run
already found, so the rerun stops at nothing it didn't name (#1522).
``robinhood-cg``'s option capital-gain reason names a workpaper it doesn't
write, so following it runs the command it names and finds that file. The
FX-policy warnings end with the step their NetBenefits and StockPlan
siblings give.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import ibkr_fa.config as config_module
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa import cli
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeBlockerKind, RobinhoodIncomeResult
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlockerKind,
    RobinhoodOptionClassificationError,
    RobinhoodOptionsResult,
    compute_robinhood_options,
    option_blocker_summary,
)
from ibkr_fa.config import (
    FxConfig,
    RobinhoodInterestPayerCountry,
    RobinhoodOptionClassificationConfig,
    RobinhoodOptionTreatment,
)
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_inputs import (
    INCOME_ACCOUNT,
    income_event,
    income_payer_country,
    payer_case,
)
from tests._robinhood_option_inputs import (
    OPTION_FINANCIAL_YEAR,
    closed_option_events,
    option_config,
    option_paired,
)
from tests._robinhood_option_inputs import option_config as _option_config
from tests._robinhood_option_inputs import option_paired as _option_paired
from tests._robinhood_option_runs import BTO as _BTO
from tests._robinhood_option_runs import MAY_JUNE_2025_TRADES as _MAY_JUNE_2025_TRADES
from tests._robinhood_option_runs import STC as _STC
from tests._robinhood_option_runs import option_cg_run as _option_cg_run
from tests._robinhood_option_runs import option_run as _option_run
from tests._robinhood_option_runs import option_trade as _option_trade
from tests._robinhood_refusals import FNO_FEMA_WARNING, OPTION_CLASSIFICATION_RECIPE
from tests._robinhood_remedies import FOLLOWED_REMEDY_ACCOUNT as _ACCOUNT
from tests._robinhood_remedies import followed_remedy_entry as _followed

# The option-classification refusal for account 1 in FY 2025-26. With no
# option blocker found, it reads exactly so (#1522); it names the account
# and the year (#1554).
_CLASSIFICATION_REFUSAL = (
    f"{FNO_FEMA_WARNING} {OPTION_CLASSIFICATION_RECIPE.format(accounts='account 1')}"
)
_ALSO_FOUND = "; the entry won't clear the unresolved option blockers the run also found: "
# A long call bought and sold to close on one day: no entry tells the order
# its lots were matched in.
_SAME_DAY_ROUND_TRIP = (
    _option_trade(_BTO, 2, date(2025, 5, 1), "-615243.00"),
    _option_trade(_STC, 3, date(2025, 5, 1), "1615243.00"),
)


def test_followed_interest_payer_remedy_loads_and_files_the_interest(tmp_path: Path) -> None:
    """Account 1 earns brokerage interest that no entry covers, as in
    ``test_one_uncovered_interest_bearing_account_blocks_ftc``. On a29658d
    the remedy did not spell out every required field, so the entry it
    describes refused at load time."""
    interest = income_event(RobinhoodEventKind.CASH_INTEREST, 2, date(2026, 1, 9), "0.25")
    first = income_payer_country(INCOME_ACCOUNT)

    def compute(*entries: RobinhoodInterestPayerCountry) -> RobinhoodIncomeResult:
        return payer_case("both", (first, *entries), other_rows=(interest,), other_account=_ACCOUNT)

    refused = compute()
    (blocker,) = refused.blockers
    assert blocker.kind is RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY
    (remedy,) = core.interest_payer_reasons(refused)

    entry, keys = _followed(tmp_path, remedy, "interest_payer_countries")
    assert keys == config_module._KNOWN_ROBINHOOD_INTEREST_PAYER_COUNTRY

    rerun = compute(entry)
    assert rerun.blockers == ()
    assert core.interest_payer_reasons(rerun) == ()
    assert Decimal("0.25") in {item.amount_gross for item in rerun.interest_credits}


@pytest.mark.parametrize(
    ("events", "statements", "stops"),
    [
        pytest.param(closed_option_events(), None, (), id="closed-round-trip"),
        pytest.param(
            _SAME_DAY_ROUND_TRIP,
            (),
            (RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY,),
            id="same-day-round-trip",
        ),
    ],
)
def test_followed_option_classification_remedy_loads_and_clears_the_refusal(
    tmp_path: Path,
    events: tuple[Any, ...],
    statements: tuple[Any, ...] | None,
    stops: tuple[RobinhoodOptionBlockerKind, ...],
) -> None:
    """One option round trip in an account with no classification. On a29658d
    the refusal named two of the entry's six required fields, so the entry it
    described refused at load time. Bought and sold on one day, the trip's
    lots can't be matched: on 5baa3de the refusal didn't say so, and the
    rerun stopped at that blocker (#1522). Every command's rerun prints
    :func:`option_blocker_summary` of these blockers."""
    paired = option_paired(events, statements=statements, account_number=_ACCOUNT)
    unclassified = option_config(None, account_number=_ACCOUNT)

    def compute(*entries: RobinhoodOptionClassificationConfig) -> RobinhoodOptionsResult:
        fy = OPTION_FINANCIAL_YEAR
        return compute_robinhood_options(
            paired,
            config=replace(unclassified, option_classifications=entries),
            financial_year_start=fy.start_date,
            financial_year_end=fy.end_date,
            capital_gains_fx_policy=AuditedFxStub(Decimal("80")),
            business_fx_policy=AuditedFxStub(Decimal("85"), specified_date=fy.end_date),
        )

    with pytest.raises(RobinhoodOptionClassificationError) as refusal:
        compute()

    entry, keys = _followed(tmp_path, str(refusal.value), "option_classifications")
    assert keys == config_module._REQUIRED_ROBINHOOD_OPTION_CLASSIFICATION

    rerun = compute(entry)
    (classification,) = rerun.classifications
    assert classification.treatment is RobinhoodOptionTreatment.CAPITAL_GAINS
    hard = core.hard_option_blockers(rerun)
    assert tuple(blocker.kind for blocker in hard) == stops
    assert str(refusal.value) == _CLASSIFICATION_REFUSAL + (
        f"{_ALSO_FOUND}{option_blocker_summary(hard)}" if hard else ""
    )


def test_followed_robinhood_options_classification_refusal_names_the_rerun_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``robinhood-options`` on the same-day round trip with no classification
    (#1522). Run 1 refuses for the entry and names the lot blocker; with
    the entry it describes, run 2 stops at exactly the blocker it named."""
    bundle = _option_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _SAME_DAY_ROUND_TRIP
    )
    bundle.paired = _option_paired(_SAME_DAY_ROUND_TRIP, statements=(), account_number=_ACCOUNT)
    bundle.robinhood = _option_config(None, account_number=_ACCOUNT)

    assert _main("robinhood-options", tmp_path) == 4
    first = capsys.readouterr().err
    (tmp_path / "followed").mkdir()
    entry, _keys = _followed(tmp_path / "followed", first, "option_classifications")
    bundle.robinhood = replace(bundle.robinhood, option_classifications=(entry,))

    assert _main("robinhood-options", tmp_path) == 4
    rerun = capsys.readouterr().err
    prefix = f"error: {FNO_FEMA_WARNING} Robinhood options contain unresolved blockers: "
    assert rerun.startswith(prefix)
    assert first == f"error: {_CLASSIFICATION_REFUSAL}{_ALSO_FOUND}{rerun.removeprefix(prefix)}"


def test_followed_robinhood_cg_option_reason_writes_the_workpaper_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``robinhood-cg`` alone, with a capital-treated option round trip, then
    the command its reason names with the flags it names. On a29658d the
    reason named ``robinhood-options.csv`` but no way to write it."""
    _option_cg_run(
        tmp_path, monkeypatch, RobinhoodOptionTreatment.CAPITAL_GAINS, _MAY_JUNE_2025_TRADES
    )
    assert _main("robinhood-cg", tmp_path) == 3
    (reason,) = (
        line.removeprefix("  Action: ")
        for line in capsys.readouterr().out.splitlines()
        if "option capital gains" in line
    )
    named = re.search(r"entry from (\S+);", reason)
    assert named is not None
    workpaper = tmp_path / "out" / named[1]
    assert not workpaper.exists()

    command = re.search(r"run (\S+) with the same ((?:--[a-z-]+(?:, | and )?)+)", reason)
    assert command is not None, reason
    # _main passes exactly these flags, with robinhood-cg's values.
    assert set(re.findall(r"--[a-z-]+", command[2])) == {
        "--input",
        "--config",
        "--output-dir",
        "--financial-year",
        "--itr-form",
    }
    assert _main(command[1], tmp_path) == 3

    assert workpaper.is_file()
    assert (tmp_path / "out" / "schedule-cg.xlsx").is_file()


def test_robinhood_fx_policy_warnings_end_with_the_siblings_step(tmp_path: Path) -> None:
    """Both Robinhood FX-policy warnings end with the step the NetBenefits and
    StockPlan warnings give, word for word; following it (the flag, for AY
    2027-28) leaves neither. On a29658d they named no step."""
    sibling = SimpleNamespace(
        fx_policy_appropriate_for_ay=False,
        manual_fill_warnings=(),
        out_of_fy_disposals=0,
        table_f_loss_across_bands=(),
    )
    (income_line,) = cli._render_income_warnings(
        cast("Any", sibling), debug=False, output_dir=tmp_path
    )
    (cg_line,) = cli._render_netbenefits_cg_warnings(cast("Any", sibling))
    assert income_line == cg_line
    step = income_line.partition("; ")[2]
    assert step.startswith("pass --suppress-cross-ay-warning ")

    def warnings(*, suppress_cross_ay_warning: bool) -> tuple[str, ...]:
        fx_ctx = TaxFxContext.for_fy(
            FinancialYear(2026), FxConfig(), suppress_cross_ay_warning=suppress_cross_ay_warning
        )
        # As the income engine sets both (compute/income_engine.py).
        appropriate = fx_ctx.fx_policy_appropriate_for_ay
        assembled = SimpleNamespace(
            form67_result=SimpleNamespace(entries=()),
            fx_policy_appropriate_for_ay=appropriate,
            wht_fx_policy_appropriate_for_ay=appropriate,
        )
        return core.income_warnings(cast("Any", assembled))

    assert warnings(suppress_cross_ay_warning=False) == (
        "Robinhood income FX policy is not independently verified for this assessment "
        f"year; {step}",
        "Robinhood withholding FX policy is not independently verified for this assessment "
        f"year; {step}",
    )
    assert warnings(suppress_cross_ay_warning=True) == ()
