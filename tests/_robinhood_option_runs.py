"""Synthetic Robinhood option trades, the option-step runs that load them, and
one located option blocker of each kind, shared by the Robinhood pipeline and
CLI tests (#1766)."""

from __future__ import annotations

from dataclasses import fields
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_income import RobinhoodIncomeResult
from ibkr_fa.compute.robinhood_options import RobinhoodOptionBlocker, RobinhoodOptionBlockerKind
from ibkr_fa.config import RobinhoodOptionTreatment
from ibkr_fa.normalize.robinhood.inventory import RobinhoodPositionReference
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import RobinhoodStatementSection, SourceLocation
from tests._fx_stubs import rule115_rates
from tests._robinhood_cg import ACCOUNT_LABELS
from tests._robinhood_income_inputs import shared_user_config as _shared_config
from tests._robinhood_option_inputs import option_config as _option_config
from tests._robinhood_option_inputs import option_event as _option
from tests._robinhood_option_inputs import option_paired as _option_paired
from tests._robinhood_pipeline_stubs import stand_in_bundle, stand_in_options, stub_fa_around_income
from tests._robinhood_refusals import OPTION_BLOCKER_MESSAGES

# Synthetic markers: none may reach a refusal, its chained exceptions or the CLI output.
OPTION_DESCRIPTION = "SYNTHDESC-7740"
OPTION_UNDERLYING = "ZQXR"


BTO = RobinhoodEventKind.OPTION_BUY_TO_OPEN
STC = RobinhoodEventKind.OPTION_SELL_TO_CLOSE


def option_trade(
    kind: RobinhoodEventKind, row: int, activity_date: date, amount: str
) -> RobinhoodEvent:
    """One marked contract at activity report row ``row`` for ``amount`` of
    cash. It expires after every report window here."""
    return _option(
        kind,
        row,
        activity_date,
        activity_date + timedelta(days=1),
        quantity="1",
        price=str(Decimal(amount).copy_abs().scaleb(-2)),
        amount=amount,
        symbol=OPTION_UNDERLYING,
        description=OPTION_DESCRIPTION,
        expiration=date(2031, 2, 21),
    )


# Traded in CY 2025: business income needs the March 31, 2026 rate.
MAY_JUNE_2025_TRADES = (
    option_trade(BTO, 2, date(2025, 5, 1), "-615243.00"),
    option_trade(STC, 3, date(2025, 6, 2), "1615243.00"),
)


def option_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    treatment: RobinhoodOptionTreatment,
    trades: tuple[RobinhoodEvent, ...],
) -> Any:
    """Load ``trades`` under ``treatment``. Each command runs its real option
    step and event check; source income is empty, and the prices and
    Schedule FA itself are stubbed."""
    bundle = stand_in_bundle(has_options=True)
    bundle.paired = _option_paired(trades, statements=())
    bundle.robinhood = _option_config(treatment)
    bundle.securities = bundle.entity_overrides = bundle.nature_by_isin = None
    bundle.config = _shared_config(tmp_path)
    stub_fa_around_income(monkeypatch, bundle)
    monkeypatch.setattr(pipeline, "_compute_options", core.compute_options)
    monkeypatch.setattr(pipeline, "_reconstruct_options", core.reconstruct_options)
    monkeypatch.setattr(
        pipeline,
        "_compute_source_income",
        lambda *_args: RobinhoodIncomeResult(
            **{field.name: () for field in fields(RobinhoodIncomeResult)}
        ),
    )
    return bundle


def option_cg_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    treatment: RobinhoodOptionTreatment,
    trades: tuple[RobinhoodEvent, ...],
) -> None:
    """:func:`option_run` with labelled accounts and an activity report from
    2023, so a long-term trade is covered. The equity CG has no sale."""
    rule115_rates(monkeypatch)
    bundle = option_run(tmp_path, monkeypatch, treatment, trades)
    bundle.paired = SimpleNamespace(
        accounts=_option_paired(trades, statements=(), report_start=date(2023, 1, 1)).accounts,
        account_labels=ACCOUNT_LABELS,
    )


def _at_row_7(kind: RobinhoodOptionBlockerKind, *columns: str) -> RobinhoodOptionBlocker:
    return RobinhoodOptionBlocker(
        kind=kind,
        account_index=0,
        located_at=RobinhoodSecurityEventReference(0, 7),
        columns=columns,
    )


# One located blocker of each kind, and its summary written out (#1134).
LOCATED_BLOCKERS: dict[RobinhoodOptionBlockerKind, tuple[RobinhoodOptionBlocker, str]] = {
    RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY: (
        _at_row_7(RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY, "Trans Code", "Quantity"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.NEGATIVE_INVENTORY]} (1 blocker; "
        "first at account 1, activity report row 7; columns Trans Code and Quantity)",
    ),
    RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION: (
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, account_index=0
        ),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION]} (1 blocker; "
        "first at account 1, option positions open at the financial year's end)",
    ),
    RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY: (
        _at_row_7(RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY, "Activity Date", "Trans Code"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.ORDERING_AMBIGUITY]} (1 blocker; "
        "first at account 1, activity report row 7; columns Activity Date and Trans Code)",
    ),
    RobinhoodOptionBlockerKind.POSITION_RECONCILIATION: (
        RobinhoodOptionBlocker(
            kind=RobinhoodOptionBlockerKind.POSITION_RECONCILIATION,
            account_index=0,
            located_at=RobinhoodPositionReference(
                0, 1, SourceLocation(page=3, section=RobinhoodStatementSection.SECURITIES, row=4)
            ),
            columns=("Qty",),
        ),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.POSITION_RECONCILIATION]} "
        "(1 blocker; first at account 1, Securities Held in Account: statement 2 page 3 row 4; "
        "column Qty)",
    ),
    RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE: (
        _at_row_7(RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE, "Activity Date"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE]} "
        "(1 blocker; first at account 1, activity report row 7; column Activity Date)",
    ),
    RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION: (
        _at_row_7(RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION, "Quantity", "Price", "Amount"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.PREMIUM_RECONCILIATION]} (1 blocker; "
        "first at account 1, activity report row 7; columns Quantity, Price and Amount)",
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT: (
        _at_row_7(RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT, "Quantity"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.UNSUPPORTED_CONTRACT]} (1 blocker; "
        "first at account 1, activity report row 7; column Quantity)",
    ),
    RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE: (
        _at_row_7(RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE, "Description", "Quantity"),
        f"{OPTION_BLOCKER_MESSAGES[RobinhoodOptionBlockerKind.UNSUPPORTED_LIFECYCLE]} (1 blocker; "
        "first at account 1, activity report row 7; columns Description and Quantity)",
    ),
}


def blocked_options(*kinds: RobinhoodOptionBlockerKind) -> SimpleNamespace:
    """A CY2025 options result with trades, so ``robinhood-all`` passes its no-trades branch."""
    options = stand_in_options(capital=True, has_trades=True)
    options.blockers = tuple(LOCATED_BLOCKERS[kind][0] for kind in kinds)
    return options
