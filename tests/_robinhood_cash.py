"""The synthetic two-month Robinhood cash reconciliation, shared by the Robinhood
cash normalize and no-echo pipeline tests (#1827).

:func:`cash_activity_row` and :func:`cash_statement` wrap the generic
builders in :mod:`tests._robinhood_statements` with this setup's defaults.
:func:`cash_sweep_row` builds a sweep row; :func:`cash_reconciliation_event`
builds an event. :func:`cash_baseline` builds and validates the two statements
and returns the reconciliation input and its events; :func:`cash_with_faults`
marks its statements with :data:`CASH_CLEAN_MARKERS` and applies each named
fault.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityGroup,
    RobinhoodActivityReconciliation,
    RobinhoodCryptoStatementRow,
    RobinhoodPendingTradeGroup,
    RobinhoodStatementReference,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import (
    RobinhoodEvent,
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementLayout,
    RobinhoodStatementSection,
    RobinhoodStatementSweepActivity,
    RobinhoodStatementSweepBank,
    SourceLocation,
)
from tests._robinhood_statements import (
    robinhood_account_summary,
    robinhood_activity,
    robinhood_statement,
)

CASH_JAN_START = date(2025, 1, 1)
CASH_JAN_END = date(2025, 1, 31)
CASH_FEB_START = date(2025, 2, 1)
CASH_FEB_END = date(2025, 2, 28)


def cash_activity_row(
    *,
    row: int,
    code: str,
    transaction_date: date,
    section: RobinhoodStatementSection = RobinhoodStatementSection.ACCOUNT_ACTIVITY,
    settle_date: date | None = None,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
) -> RobinhoodStatementActivity:
    return robinhood_activity(
        row=row,
        transaction_code=code,
        transaction_date=transaction_date,
        section=section,
        settle_date=settle_date,
        quantity=quantity,
        price_usd=price,
        debit_usd=debit,
        credit_usd=credit,
    )


def cash_sweep_row(
    *,
    row: int,
    transaction_date: date,
    balance: Decimal,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
    description: str = "Sweep movement",
) -> RobinhoodStatementSweepActivity:
    return RobinhoodStatementSweepActivity(
        location=SourceLocation(
            page=3,
            section=RobinhoodStatementSection.SWEEP_ACTIVITY,
            row=row,
        ),
        description=description,
        transaction_date=transaction_date,
        debit_usd=debit,
        credit_usd=credit,
        balance_usd=balance,
    )


def cash_statement(
    *,
    start: date,
    end: date,
    opening: tuple[
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
    ],
    closing: tuple[Decimal, Decimal, Decimal, Decimal],
    activities: tuple[RobinhoodStatementActivity, ...],
    sweep_activity: tuple[RobinhoodStatementSweepActivity, ...] = (),
    layout: RobinhoodStatementLayout = RobinhoodStatementLayout.DEPOSIT_SWEEP,
    sweep_bank: bool = False,
) -> RobinhoodStatement:
    summary = robinhood_account_summary(
        opening=opening,
        closing=closing,
        cash_label=(
            "Net Account Balance"
            if layout is RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
            else "Brokerage Cash Balance"
        ),
        closing_percents=(Decimal("100"), Decimal("0"), Decimal("0")),
    )
    banks = (
        (
            RobinhoodStatementSweepBank(
                location=SourceLocation(
                    page=3,
                    section=RobinhoodStatementSection.SWEEP_BANKS,
                    row=1,
                ),
                bank_name="Program Bank",
                balance_usd=closing[1],
            ),
        )
        if sweep_bank
        else ()
    )
    return robinhood_statement(
        layout=layout,
        period_start=start,
        period_end=end,
        account_type_label="Individual",
        page_count=3,
        account_summary=summary,
        activities=activities,
        sweep_banks=banks,
        sweep_activity=sweep_activity,
    )


def cash_reconciliation_event(
    *,
    ordinal: int,
    kind: RobinhoodEventKind,
    activity_date: date,
    settle_date: date,
    amount: Decimal,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
) -> RobinhoodEvent:
    code = "ACH" if kind is RobinhoodEventKind.ACH_DEPOSIT else "Buy"
    return RobinhoodEvent(
        kind=kind,
        source_record_index=ordinal,
        activity_date=activity_date,
        process_date=activity_date,
        settle_date=settle_date,
        instrument=None if kind is RobinhoodEventKind.ACH_DEPOSIT else "EQTY",
        description="Synthetic event",
        trans_code=code,
        quantity=quantity,
        quantity_suffix=None,
        price_usd=price,
        amount_usd=amount,
        issuer_name=(None if kind is RobinhoodEventKind.ACH_DEPOSIT else "Synthetic issuer"),
        cusip=None if kind is RobinhoodEventKind.ACH_DEPOSIT else "123456789",
    )


def cash_baseline():
    coin = cash_activity_row(
        row=1,
        code="COIN",
        transaction_date=date(2025, 1, 15),
        credit=Decimal("1.00"),
    )
    pending = cash_activity_row(
        row=2,
        code="Buy",
        transaction_date=date(2025, 1, 31),
        section=RobinhoodStatementSection.PENDING_TRADES,
        settle_date=date(2025, 2, 3),
        quantity=Decimal("1.0000"),
        price=Decimal("10.0000"),
        debit=Decimal("10.00"),
    )
    first = cash_statement(
        start=CASH_JAN_START,
        end=CASH_JAN_END,
        opening=(None, None, None, None),
        closing=(
            Decimal("10"),
            Decimal("20"),
            Decimal("0"),
            Decimal("30"),
        ),
        activities=(coin, pending),
        sweep_activity=(
            cash_sweep_row(
                row=1,
                transaction_date=CASH_JAN_START,
                balance=Decimal("0"),
                description="Opening Sweep Balance",
            ),
            cash_sweep_row(
                row=2,
                transaction_date=date(2025, 1, 15),
                balance=Decimal("20"),
                credit=Decimal("20"),
            ),
            cash_sweep_row(
                row=3,
                transaction_date=CASH_JAN_END,
                balance=Decimal("20"),
                description="Closing Sweep Balance",
            ),
        ),
        sweep_bank=True,
    )
    settled_buy = cash_activity_row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 31),
        quantity=Decimal("1.0000"),
        price=Decimal("10.0000"),
        debit=Decimal("10.00"),
    )
    ach = cash_activity_row(
        row=2,
        code="ACH",
        transaction_date=date(2025, 2, 10),
        credit=Decimal("20.00"),
    )
    second = cash_statement(
        start=CASH_FEB_START,
        end=CASH_FEB_END,
        opening=(
            Decimal("10"),
            Decimal("20"),
            Decimal("0"),
            Decimal("30"),
        ),
        closing=(
            Decimal("15"),
            Decimal("25"),
            Decimal("0"),
            Decimal("40"),
        ),
        activities=(settled_buy, ach),
        sweep_activity=(
            cash_sweep_row(
                row=1,
                transaction_date=CASH_FEB_START,
                balance=Decimal("20"),
                description="Opening Sweep Balance",
            ),
            cash_sweep_row(
                row=2,
                transaction_date=date(2025, 2, 10),
                balance=Decimal("25"),
                credit=Decimal("5"),
            ),
            cash_sweep_row(
                row=3,
                transaction_date=CASH_FEB_END,
                balance=Decimal("25"),
                description="Closing Sweep Balance",
            ),
        ),
        sweep_bank=True,
    )
    bundle = validate_robinhood_statement_bundles((first, second))[0]
    buy = cash_reconciliation_event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 3),
        amount=Decimal("-10.00"),
        quantity=Decimal("1.0000"),
        price=Decimal("10.00"),
    )
    deposit = cash_reconciliation_event(
        ordinal=2,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=date(2025, 2, 10),
        settle_date=date(2025, 2, 10),
        amount=Decimal("20.00"),
    )
    buy_ref = RobinhoodStatementReference(1, settled_buy.location)
    ach_ref = RobinhoodStatementReference(1, ach.location)
    pending_ref = RobinhoodStatementReference(0, pending.location)
    crypto_ref = RobinhoodStatementReference(0, coin.location)
    activity = RobinhoodActivityReconciliation(
        primary_bundle=bundle,
        activity_groups=(
            RobinhoodActivityGroup(
                transaction_code="Buy",
                event_kinds=(RobinhoodEventKind.EQUITY_BUY,),
                event_ordinals=(1,),
                statement_references=(buy_ref,),
            ),
            RobinhoodActivityGroup(
                transaction_code="ACH",
                event_kinds=(RobinhoodEventKind.ACH_DEPOSIT,),
                event_ordinals=(2,),
                statement_references=(ach_ref,),
            ),
        ),
        in_kind_transfer_groups=(),
        pending_statement_events=(),
        pending_trade_groups=(
            RobinhoodPendingTradeGroup(
                event_ordinals=(1,),
                statement_references=(pending_ref,),
            ),
        ),
        crypto_rows=(RobinhoodCryptoStatementRow(crypto_ref),),
    )
    return activity, (buy, deposit)


MARKED_CASH_SWEEP = cash_sweep_row(
    row=1,
    transaction_date=CASH_JAN_START,
    balance=Decimal("0"),
    description="SYNTHCASHREASON",
)
CASH_CLEAN_MARKERS = ("SYNTHCLEANACCOUNT", "SYNTHCLEANNAME", "SYNTHCLEANDESC")


def cash_with_faults(*faults: str):
    """:func:`cash_baseline` with marked statements and each named fault:
    ``identity`` breaks February's combined cash identity, ``pending-cash``
    January's pending-trade cash, ``late`` the pending trade's settlement
    period, ``drift`` its settled row's quantity exponent and
    ``disposition`` the coverage of February's ACH row."""
    activity, events = cash_baseline()
    first, second = (
        replace(
            statement,
            account_number="SYNTHCLEANACCOUNT",
            registration_lines=("SYNTHCLEANNAME",),
            activities=tuple(
                replace(row, description="SYNTHCLEANDESC") for row in statement.activities
            ),
        )
        for statement in activity.primary_bundle.statements
    )
    coin, pending = first.activities
    settled, ach = second.activities
    if "pending-cash" in faults:
        pending = replace(pending, debit_usd=Decimal("11.00"))
    if "late" in faults:
        pending = replace(pending, settle_date=date(2025, 3, 1))
    if "drift" in faults:
        settled = replace(settled, quantity=Decimal("1.0"))
    first = replace(first, activities=(coin, pending))
    second = replace(second, activities=(settled, ach))
    if "identity" in faults:
        second = replace(
            second,
            account_summary=replace(
                second.account_summary,
                closing_brokerage_cash_usd=Decimal("16"),
                closing_portfolio_value_usd=Decimal("41"),
            ),
        )
    activity = replace(
        activity,
        primary_bundle=replace(activity.primary_bundle, statements=(first, second)),
    )
    if "disposition" in faults:
        buy_group, ach_group = activity.activity_groups
        activity = replace(
            activity,
            activity_groups=(buy_group, replace(ach_group, statement_references=())),
        )
    return activity, events
