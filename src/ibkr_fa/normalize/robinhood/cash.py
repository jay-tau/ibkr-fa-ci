"""Reconcile Robinhood cash, sweeps, and unsettled trade evidence."""

from __future__ import annotations

import operator
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, DecimalTuple
from itertools import pairwise

from ibkr_fa.normalize.robinhood.activity import (
    ROBINHOOD_TRADE_CODES,
    ROBINHOOD_TRADE_KINDS,
    RobinhoodActivityReconciliation,
    RobinhoodStatementReference,
    robinhood_exact,
    robinhood_exact_sum,
    robinhood_signed_cash,
    robinhood_statement_signed_amount,
)
from ibkr_fa.normalize.robinhood.bundle import (
    RobinhoodReconciliationError,
    opening_components,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementLayout,
    RobinhoodStatementSection,
    RobinhoodStatementSweepActivity,
)

type _PendingLifecycleKey = tuple[
    str,
    object,
    DecimalTuple | None,
    DecimalTuple | None,
    DecimalTuple | None,
]

_CASH_IDENTITY = "combined cash identity"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCashPeriod:
    """One statement period's exact cash and unsettled evidence."""

    closing_unsettled_usd: Decimal
    program_bank_present: bool


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodCashReconciliation:
    """Complete #460 evidence, excluding reciprocal transfer proof."""

    activity_reconciliation: RobinhoodActivityReconciliation
    periods: tuple[RobinhoodCashPeriod, ...]


def _fail(message: str) -> RobinhoodReconciliationError:
    return RobinhoodReconciliationError(message)


def _signed_cash(
    debit: Decimal | None,
    credit: Decimal | None,
) -> Decimal | None:
    return robinhood_signed_cash(debit, credit, row_kind="cash")


def _sweep_signed(row: RobinhoodStatementSweepActivity) -> Decimal | None:
    return _signed_cash(row.debit_usd, row.credit_usd)


def _statement_row_map(
    activity: RobinhoodActivityReconciliation,
    section: RobinhoodStatementSection,
) -> dict[RobinhoodStatementReference, RobinhoodStatementActivity]:
    result: dict[RobinhoodStatementReference, RobinhoodStatementActivity] = {}
    for statement_index, statement in enumerate(activity.primary_bundle.statements):
        for row in statement.activities:
            if row.location.section is not section:
                continue
            reference = RobinhoodStatementReference(statement_index, row.location)
            if reference in result:
                raise _fail("statement row reference is duplicated")
            result[reference] = row
    return result


def _validate_dispositions(
    activity: RobinhoodActivityReconciliation,
    events: dict[int, RobinhoodEvent],
) -> None:
    event_dispositions: Counter[int] = Counter()
    for group in activity.activity_groups:
        event_dispositions.update(group.event_ordinals)
    for group in activity.in_kind_transfer_groups:
        event_dispositions.update(group.event_ordinals)
    event_dispositions.update(item.event_ordinal for item in activity.pending_statement_events)
    if event_dispositions != Counter(events.keys()):
        raise _fail("activity event dispositions are incomplete or duplicated")

    account_rows = _statement_row_map(
        activity,
        RobinhoodStatementSection.ACCOUNT_ACTIVITY,
    )
    account_dispositions: Counter[RobinhoodStatementReference] = Counter()
    for group in activity.activity_groups:
        account_dispositions.update(group.statement_references)
    for group in activity.in_kind_transfer_groups:
        account_dispositions.update(group.statement_references)
    account_dispositions.update(row.statement_reference for row in activity.crypto_rows)
    if account_dispositions != Counter(account_rows.keys()):
        raise _fail("Account Activity row dispositions are incomplete or duplicated")

    pending_rows = _statement_row_map(
        activity,
        RobinhoodStatementSection.PENDING_TRADES,
    )
    pending_dispositions: Counter[RobinhoodStatementReference] = Counter()
    for group in activity.pending_trade_groups:
        pending_dispositions.update(group.statement_references)
    if pending_dispositions != Counter(pending_rows.keys()):
        raise _fail("Pending Trades row dispositions are incomplete or duplicated")


def _statement_activity_net(statement: RobinhoodStatement) -> Decimal:
    return robinhood_exact_sum(
        (
            value
            for row in statement.activities
            if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
            if (value := robinhood_statement_signed_amount(row)) is not None
        ),
        label="Account Activity net",
    )


def _sweep_net(statement: RobinhoodStatement) -> Decimal:
    return robinhood_exact_sum(
        (value for row in statement.sweep_activity if (value := _sweep_signed(row)) is not None),
        label="Deposit Sweep net",
    )


def _opening_available(statement: RobinhoodStatement) -> bool:
    values = opening_components(statement)
    if all(value is None for value in values):
        return False
    if any(value is None for value in values):
        raise _fail("statement opening components mix unavailable and numeric")
    return True


def _validate_cash_identities(
    statement: RobinhoodStatement,
    *,
    activity_net: Decimal,
    sweep_net: Decimal,
) -> None:
    summary = statement.account_summary
    sweep_partition_available = statement.layout is RobinhoodStatementLayout.DEPOSIT_SWEEP
    if not sweep_partition_available and (
        statement.sweep_activity
        or statement.sweep_banks
        or summary.opening_deposit_sweep_usd not in {None, Decimal()}
        or summary.closing_deposit_sweep_usd != 0
    ):
        raise _fail("legacy statement unexpectedly carries Deposit Sweep evidence")

    opening_available = _opening_available(statement)
    if sweep_partition_available and not opening_available and not statement.sweep_activity:
        raise _fail("earliest Deposit Sweep period lacks the ledger needed to partition cash")
    if sweep_partition_available and statement.sweep_activity:
        if statement.sweep_activity[-1].balance_usd != summary.closing_deposit_sweep_usd:
            raise _fail("Deposit Sweep closing balance disagrees with Account Summary")
        if opening_available and (
            statement.sweep_activity[0].balance_usd != summary.opening_deposit_sweep_usd
        ):
            raise _fail("Deposit Sweep opening balance disagrees with Account Summary")

    if opening_available:
        opening_brokerage = summary.opening_brokerage_cash_usd
        opening_sweep = summary.opening_deposit_sweep_usd
        assert opening_brokerage is not None and opening_sweep is not None
        if robinhood_exact_sum(
            (opening_sweep, activity_net), start=opening_brokerage, label=_CASH_IDENTITY
        ) != robinhood_exact(
            operator.add,
            summary.closing_brokerage_cash_usd,
            summary.closing_deposit_sweep_usd,
            label=_CASH_IDENTITY,
        ):
            raise _fail("combined cash identity does not reconcile")
        if sweep_partition_available and (
            robinhood_exact(operator.add, opening_sweep, sweep_net, label="Deposit Sweep identity")
            != summary.closing_deposit_sweep_usd
        ):
            raise _fail("Deposit Sweep net does not reconcile")


def _unsettled_for_period(
    statement: RobinhoodStatement,
    events: Iterable[RobinhoodEvent],
) -> tuple[Decimal, Decimal]:
    unsettled: list[RobinhoodEvent] = [
        event
        for event in events
        if event.amount_usd is not None
        and event.activity_date <= statement.period_end < event.settle_date
    ]
    all_cash = sum(
        (event.amount_usd for event in unsettled if event.amount_usd is not None),
        Decimal(),
    )
    pending_trade = sum(
        (
            event.amount_usd
            for event in unsettled
            if event.kind in ROBINHOOD_TRADE_KINDS and event.amount_usd is not None
        ),
        Decimal(),
    )
    return all_cash, pending_trade


def _statement_pending_net(statement: RobinhoodStatement) -> Decimal:
    return sum(
        (
            value
            for row in statement.activities
            if row.location.section is RobinhoodStatementSection.PENDING_TRADES
            if (value := robinhood_statement_signed_amount(row)) is not None
        ),
        Decimal(),
    )


def _decimal_source_key(value: Decimal | None) -> DecimalTuple | None:
    return value.as_tuple() if value is not None else None


def _pending_lifecycle_key(
    row: RobinhoodStatementActivity,
) -> _PendingLifecycleKey:
    return (
        row.transaction_code,
        row.transaction_date,
        _decimal_source_key(row.quantity),
        _decimal_source_key(row.price_usd),
        _decimal_source_key(robinhood_statement_signed_amount(row)),
    )


def _pending_rows(statement: RobinhoodStatement) -> tuple[RobinhoodStatementActivity, ...]:
    return tuple(
        row
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
    )


def _settled_preperiod_trade_rows(
    statement: RobinhoodStatement,
) -> tuple[RobinhoodStatementActivity, ...]:
    return tuple(
        row
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
        and row.transaction_code in ROBINHOOD_TRADE_CODES
        and row.transaction_date < statement.period_start
    )


def _validate_pending_lifecycle(
    statements: tuple[RobinhoodStatement, ...],
) -> None:
    for current, following in pairwise(statements):
        pending = _pending_rows(current)
        if any(
            row.settle_date is None
            or not (following.period_start <= row.settle_date <= following.period_end)
            for row in pending
        ):
            raise _fail("pending trade does not settle in the next statement period")
        pending_counter = Counter(_pending_lifecycle_key(row) for row in pending)
        settled_counter = Counter(
            _pending_lifecycle_key(row) for row in _settled_preperiod_trade_rows(following)
        )
        if pending_counter != settled_counter:
            raise _fail("pending trades do not match next-period settlements")


def reconcile_robinhood_cash(
    activity: RobinhoodActivityReconciliation,
    events: Iterable[RobinhoodEvent],
) -> RobinhoodCashReconciliation:
    """Require exact per-period cash and pending-trade agreement."""

    ordered_events = tuple(sorted(events, key=lambda event: event.source_record_index))
    event_map = {event.source_record_index: event for event in ordered_events}
    if len(event_map) != len(ordered_events):
        raise _fail("cash reconciliation event ordinals are duplicated")
    _validate_dispositions(activity, event_map)

    periods: list[RobinhoodCashPeriod] = []
    for statement in activity.primary_bundle.statements:
        _validate_cash_identities(
            statement,
            activity_net=_statement_activity_net(statement),
            sweep_net=_sweep_net(statement),
        )
        unsettled, pending_trade = _unsettled_for_period(statement, ordered_events)
        if pending_trade != _statement_pending_net(statement):
            raise _fail("trade-only unsettled cash disagrees with Pending Trades")
        periods.append(
            RobinhoodCashPeriod(
                closing_unsettled_usd=unsettled,
                program_bank_present=bool(statement.sweep_banks or statement.sweep_activity),
            )
        )

    _validate_pending_lifecycle(activity.primary_bundle.statements)
    return RobinhoodCashReconciliation(
        activity_reconciliation=activity,
        periods=tuple(periods),
    )
