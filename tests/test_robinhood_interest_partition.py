"""#1477: an unresolved brokerage/program-bank interest partition names itself.

When the statements don't settle which of an account's interest credits are
brokerage and which are program-bank interest, every one of them is left
without a kind, so even a reviewed US entry for that account/FY declaring
both kinds leaves them uncovered. The payer recipe can't clear that, so it
isn't printed for such an account. The partition always blocks Schedule OS,
so the Schedule OS refusal both commands print names the partition instead.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ibkr_fa.compute.robinhood_income import (
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeResult,
    compute_robinhood_income,
)
from ibkr_fa.config import (
    RobinhoodConfig,
    RobinhoodInterestKind,
    RobinhoodInterestPayerCountry,
)
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.parser.robinhood_1042s import Robinhood1042SPacket
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementLayout
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_income_inputs import (
    ALL_US_PAYERS,
    INCOME_ACCOUNT,
    INCOME_FY_END,
    INCOME_FY_START,
    INCOME_OTHER_ACCOUNT,
    INCOME_PAYMENT_DATE,
    income_config,
    income_event,
    income_packet,
    income_paired,
    income_payer_country,
    sweep_interest,
)
from tests._robinhood_pipeline_stubs import stub_income_bundle as _stub_income_bundle
from tests._robinhood_refusals import FTC_OS_REMOVED, INTEREST_PAYER_RECIPE
from tests._robinhood_refusals import OS_UNRESOLVED as _OS_REFUSAL
from tests._robinhood_security import configured_securities

_PARTITION = (
    "Robinhood interest isn't split into brokerage and program-bank interest: the "
    "statements don't settle which interest credits are which, so no "
    "[[robinhood.interest_payer_countries]] entry can clear this refusal; complete "
    "Schedule OS, Schedule FSI, Schedule TR and Form 67 by hand"
)
_DAY = INCOME_PAYMENT_DATE
_CREDIT = income_event(RobinhoodEventKind.CASH_INTEREST, 1, _DAY, "1.00")
# Each way the statements leave the partition unresolved, as the synthetic
# reconciliation can build it: extra interest credits for the first account,
# then the keywords for its 2025 statement.
_BANK = {"program_bank": True}
_CAUSES: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {
    "program-bank-without-sweep-ledger": ((), _BANK),
    # No program-bank period, so only the roster check marks the period unsafe.
    "program-banks-listed-without-sweep-rows": ((), {"sweep_banks": (object(),)}),
    "unrecognised-sweep-row": (
        (),
        {**_BANK, "sweep_rows": (sweep_interest(_DAY, "1.00", description="SWEEP"),)},
    ),
    "interest-payment-sweep-row-not-a-credit": (
        (),
        {**_BANK, "sweep_rows": (sweep_interest(_DAY, "1.00", debit=True),)},
    ),
    "sweep-count-matches-neither-none-nor-all": (
        (),
        {
            **_BANK,
            "sweep_rows": (
                sweep_interest(_DAY, "1.00"),
                sweep_interest(_DAY, "1.00"),
            ),
        },
    ),
    "credit-outside-every-statement-period": (
        (income_event(RobinhoodEventKind.CASH_INTEREST, 2, date(2024, 12, 20), "1.00"),),
        {},
    ),
    # _income gives this statement the older, net-account layout.
    "sweep-rows-on-the-older-layout": (
        (),
        {**_BANK, "sweep_rows": (sweep_interest(_DAY, "1.00"),)},
    ),
}
_COMPLETE = ALL_US_PAYERS
_ONLY_OTHER_ACCOUNT = (income_payer_country(INCOME_OTHER_ACCOUNT),)


def _entry_with(*kinds: RobinhoodInterestKind) -> tuple[RobinhoodInterestPayerCountry, ...]:
    return (
        income_payer_country(INCOME_ACCOUNT, interest_kinds=frozenset(kinds)),
        *_ONLY_OTHER_ACCOUNT,
    )


def _case(
    payers: tuple[RobinhoodInterestPayerCountry, ...], cause: str | None
) -> tuple[RobinhoodConfig, Any, tuple[Robinhood1042SPacket, ...]]:
    """The first account's FY 2025-26 interest credit and its 1042-S, with the
    partition left unresolved by ``cause`` (``None``: brokerage interest)."""
    extra, statement = ((), {}) if cause is None else _CAUSES[cause]
    paired = income_paired((_CREDIT, *extra), **statement)
    if cause == "sweep-rows-on-the-older-layout":
        first, other = paired.accounts
        bundle_2025, *rest = first.activity.primary_bundle.statements
        legacy = replace(bundle_2025, layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT)
        first = replace(
            first,
            activity=replace(
                first.activity,
                primary_bundle=replace(first.activity.primary_bundle, statements=(legacy, *rest)),
            ),
        )
        paired = replace(paired, accounts=(first, other))
    packets = (income_packet(income_code="01", gross="1.00", tax="0.00", rate="0.00"),)
    return income_config(interest_payer_countries=payers), paired, packets


def _income(
    payers: tuple[RobinhoodInterestPayerCountry, ...], cause: str | None
) -> RobinhoodIncomeResult:
    config, paired, packets = _case(payers, cause)
    return compute_robinhood_income(
        paired,
        packets,
        config=config,
        securities=configured_securities(config),
        financial_year_start=INCOME_FY_START,
        financial_year_end=INCOME_FY_END,
    )


def _run(
    command: str,
    payers: tuple[RobinhoodInterestPayerCountry, ...],
    cause: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run ``command`` with real income computation and the existing pipeline stubs."""
    _stub_income_bundle(monkeypatch, tmp_path, *_case(payers, cause))
    code = _main(command, tmp_path)
    captured = capsys.readouterr()
    # Nothing is written, or every file written is removed, as on main.
    out_dir = tmp_path / "out"
    assert not out_dir.exists() or not any(out_dir.iterdir())
    return code, captured.out, captured.err


_NO_LEDGER = "program-bank-without-sweep-ledger"


@pytest.mark.parametrize(
    ("payers", "cause", "partition_only"),
    [
        pytest.param(_COMPLETE, _NO_LEDGER, (0,), id="complete-entry-unresolved"),
        pytest.param(
            _entry_with(RobinhoodInterestKind.BROKERAGE),
            _NO_LEDGER,
            (),
            id="brokerage-only-entry-unresolved",
        ),
        pytest.param(_ONLY_OTHER_ACCOUNT, _NO_LEDGER, (), id="missing-entry-unresolved"),
        pytest.param(_ONLY_OTHER_ACCOUNT, None, (), id="missing-entry-resolved"),
        pytest.param(_COMPLETE, None, (), id="complete-entry-resolved"),
    ],
)
def test_partition_only_accounts_are_those_whose_entry_declares_both_kinds(
    payers: tuple[RobinhoodInterestPayerCountry, ...],
    cause: str | None,
    partition_only: tuple[int, ...],
) -> None:
    """A credit the partition leaves without a kind could be either kind, so
    only an entry that declares both leaves the partition as the sole cause."""
    assert _income(payers, cause).partition_only_payer_accounts == partition_only


@pytest.mark.parametrize("cause", _CAUSES)
def test_every_cause_blocks_the_partition_for_a_complete_entry(cause: str) -> None:
    income = _income(_COMPLETE, cause)

    assert {blocker.kind for blocker in income.blockers if blocker.account_indices == (0,)} >= {
        RobinhoodIncomeBlockerKind.PROGRAM_BANK_PARTITION,
        RobinhoodIncomeBlockerKind.INTEREST_PAYER_COUNTRY,
    }
    assert income.partition_only_payer_accounts == (0,)


@pytest.mark.parametrize("cause", _CAUSES)
def test_complete_entry_robinhood_income_names_the_partition_not_the_recipe(
    cause: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``robinhood-income`` stops at Schedule OS before its FTC report, as on
    main; the refusal now says why and that no payer entry clears it."""
    code, out, err = _run("robinhood-income", _COMPLETE, cause, tmp_path, monkeypatch, capsys)

    assert code == 4
    assert err == f"error: {_OS_REFUSAL}\n{_PARTITION}\n"
    assert INTEREST_PAYER_RECIPE not in out + err


@pytest.mark.parametrize("cause", _CAUSES)
def test_complete_entry_robinhood_all_names_the_partition_not_the_recipe(
    cause: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run("robinhood-all", _COMPLETE, cause, tmp_path, monkeypatch, capsys)

    assert code == 4
    assert err == ""
    assert (
        "  error        schedule-os\n"
        f"                 -> {_OS_REFUSAL}\n"
        f"{_PARTITION}\n"
        "  error        income-ftc\n"
        "                 -> unresolved income blockers\n"
        "  ok           schedule-fa\n"
    ) in out
    assert out.count(_PARTITION) == 1
    assert INTEREST_PAYER_RECIPE not in out


@pytest.mark.parametrize(
    "payers",
    [
        pytest.param(_ONLY_OTHER_ACCOUNT, id="missing"),
        pytest.param(_entry_with(RobinhoodInterestKind.BROKERAGE), id="brokerage-only"),
        pytest.param(_entry_with(RobinhoodInterestKind.PROGRAM_BANK), id="program-bank-only"),
    ],
)
def test_incomplete_entry_with_unresolved_partition_prints_both_texts(
    payers: tuple[RobinhoodInterestPayerCountry, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``robinhood-all`` keeps the recipe on its FTC leg. ``robinhood-income``
    stops at Schedule OS before its FTC report, so that refusal now gives the
    recipe after the partition text (#1497); on main it gave none."""
    cause = _NO_LEDGER
    code, out, err = _run("robinhood-all", payers, cause, tmp_path, monkeypatch, capsys)
    assert code == 4
    assert (
        f"                 -> {_OS_REFUSAL}\n"
        f"{_PARTITION}\n"
        "  error        income-ftc\n"
        "                 -> unresolved income blockers\n"
        f"{INTEREST_PAYER_RECIPE}\n"
    ) in out

    code, out, err = _run("robinhood-income", payers, cause, tmp_path, monkeypatch, capsys)
    assert code == 4
    assert err == f"error: {_OS_REFUSAL}\n{_PARTITION}\n{INTEREST_PAYER_RECIPE}\n"


def test_missing_entry_with_resolved_partition_prints_mains_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(
        "robinhood-income", _ONLY_OTHER_ACCOUNT, None, tmp_path, monkeypatch, capsys
    )
    assert (code, err) == (4, "")
    assert out.startswith(
        "Robinhood income-ftc: error\n"
        f"  Action: {FTC_OS_REMOVED}\n"
        f"  Action: {INTEREST_PAYER_RECIPE}\n\n"
    )

    code, out, err = _run("robinhood-all", _ONLY_OTHER_ACCOUNT, None, tmp_path, monkeypatch, capsys)
    assert (code, err) == (4, "")
    assert out.startswith(
        "ROBINHOOD SUMMARY — FY 2025-26 / CY 2025\n"
        "STATUS: error\n"
        "  ok           equity-cg\n"
        "                 -> output removed because the run failed\n"
        "  ok           schedule-os\n"
        "                 -> output removed because the run failed\n"
        "  error        income-ftc\n"
        "                 -> unresolved income blockers\n"
        f"{INTEREST_PAYER_RECIPE}\n"
        "  ok           schedule-fa\n"
    )
    assert _PARTITION not in out
