"""Cash, sweep, and unsettled reconciliation checks for Robinhood."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    localcontext,
)

import pytest

from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountReconciliation,
    RobinhoodActivityGroup,
    RobinhoodActivityReconciliation,
    RobinhoodCryptoStatementRow,
    RobinhoodPendingStatementEvent,
    RobinhoodPendingTradeGroup,
    RobinhoodReconciliationError,
    RobinhoodStatementReference,
    reconcile_robinhood_cash,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.activity import robinhood_statement_signed_amount
from ibkr_fa.normalize.robinhood.cash import _sweep_signed
from ibkr_fa.parser.robinhood import (
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementLayout,
    RobinhoodStatementSection,
)
from tests._crash_reports import assert_fixed_refusal
from tests._private_checks import private_test
from tests._robinhood_cash import CASH_CLEAN_MARKERS as _CLEAN_MARKERS
from tests._robinhood_cash import CASH_JAN_END as _JAN_END
from tests._robinhood_cash import CASH_JAN_START as _JAN_START
from tests._robinhood_cash import MARKED_CASH_SWEEP as _MARKED_SWEEP
from tests._robinhood_cash import cash_activity_row as _activity_row
from tests._robinhood_cash import cash_baseline as _baseline
from tests._robinhood_cash import cash_reconciliation_event as _event
from tests._robinhood_cash import cash_statement as _statement
from tests._robinhood_cash import cash_sweep_row as _sweep_row
from tests._robinhood_cash import cash_with_faults as _with_faults
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)
from tests._robinhood_refusals import (
    ACTIVITY_BOTH_REFUSAL,
    ACTIVITY_NEGATIVE_REFUSAL,
    CASH_BOTH_REFUSAL,
    CASH_IDENTITY_REFUSAL,
    CASH_NEGATIVE_REFUSAL,
    PENDING_MATCH_REFUSAL,
    PENDING_SETTLE_REFUSAL,
)
from tests._robinhood_refusals import PENDING_CASH_REFUSAL as _PENDING_CASH_REFUSAL


def test_reconciles_na_numeric_sweep_unsettled_crypto_and_lifecycle() -> None:
    activity, events = _baseline()
    result = reconcile_robinhood_cash(activity, events)

    assert len(result.periods) == 2
    first, second = result.periods
    assert first.closing_unsettled_usd == Decimal("-10.00")
    assert first.program_bank_present
    assert second.closing_unsettled_usd == 0
    assert result.activity_reconciliation.crypto_rows
    assert repr(result) == object.__repr__(result)


def test_combined_cash_sweep_endpoint_and_sweep_net_mismatches_refuse() -> None:
    activity, events = _baseline()
    statements = activity.primary_bundle.statements

    bad_close = replace(
        statements[1],
        account_summary=replace(
            statements[1].account_summary,
            closing_brokerage_cash_usd=Decimal("16"),
            closing_portfolio_value_usd=Decimal("41"),
        ),
    )
    bad_bundle = replace(
        activity.primary_bundle,
        statements=(statements[0], bad_close),
    )
    with pytest.raises(RobinhoodReconciliationError, match="combined cash"):
        reconcile_robinhood_cash(
            replace(activity, primary_bundle=bad_bundle),
            events,
        )

    bad_open = replace(
        statements[1],
        sweep_activity=(
            replace(
                statements[1].sweep_activity[0],
                balance_usd=Decimal("19"),
            ),
            *statements[1].sweep_activity[1:],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="opening balance"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(statements[0], bad_open),
                ),
            ),
            events,
        )

    bad_sweep_close = replace(
        statements[1],
        sweep_activity=(
            *statements[1].sweep_activity[:-1],
            replace(
                statements[1].sweep_activity[-1],
                balance_usd=Decimal("24"),
            ),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="closing balance"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(statements[0], bad_sweep_close),
                ),
            ),
            events,
        )

    bad_net = replace(
        statements[1],
        sweep_activity=(
            statements[1].sweep_activity[0],
            replace(
                statements[1].sweep_activity[1],
                credit_usd=Decimal("4"),
            ),
            statements[1].sweep_activity[2],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="Sweep net"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(statements[0], bad_net),
                ),
            ),
            events,
        )

    no_ledger_first = replace(statements[0], sweep_activity=())
    with pytest.raises(RobinhoodReconciliationError, match="lacks the ledger"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(no_ledger_first, statements[1]),
                ),
            ),
            events,
        )


def test_legacy_layout_has_no_program_bank_and_refuses_sweep_evidence() -> None:
    ach = _activity_row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 10),
        credit=Decimal("10"),
    )
    statement = _statement(
        start=_JAN_START,
        end=_JAN_END,
        opening=(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        closing=(Decimal("10"), Decimal("0"), Decimal("0"), Decimal("10")),
        activities=(ach,),
        layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
    )
    bundle = validate_robinhood_statement_bundles((statement,))[0]
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        amount=Decimal("10"),
    )
    activity = RobinhoodActivityReconciliation(
        primary_bundle=bundle,
        activity_groups=(
            RobinhoodActivityGroup(
                transaction_code="ACH",
                event_kinds=(RobinhoodEventKind.ACH_DEPOSIT,),
                event_ordinals=(1,),
                statement_references=(RobinhoodStatementReference(0, ach.location),),
            ),
        ),
        in_kind_transfer_groups=(),
        pending_statement_events=(),
        pending_trade_groups=(),
        crypto_rows=(),
    )
    period = reconcile_robinhood_cash(activity, (event,)).periods[0]
    assert not period.program_bank_present

    with pytest.raises(RobinhoodReconciliationError, match="legacy statement"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    bundle,
                    statements=(
                        replace(
                            statement,
                            sweep_activity=(
                                _sweep_row(
                                    row=1,
                                    transaction_date=_JAN_START,
                                    balance=Decimal("0"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            (event,),
        )


def test_pending_trade_cash_and_next_period_representation_must_match() -> None:
    activity, events = _baseline()
    statements = activity.primary_bundle.statements

    bad_pending = replace(
        statements[0],
        activities=tuple(
            replace(row, debit_usd=Decimal("9.00"))
            if row.location.section is RobinhoodStatementSection.PENDING_TRADES
            else row
            for row in statements[0].activities
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="trade-only unsettled"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(bad_pending, statements[1]),
                ),
            ),
            events,
        )


def test_backdated_nontrade_row_is_excluded_from_pending_lifecycle() -> None:
    activity, events = _baseline()
    statements = activity.primary_bundle.statements
    coin = _activity_row(
        row=3,
        code="COIN",
        transaction_date=date(2025, 1, 30),
        credit=Decimal("1"),
    )
    second = replace(
        statements[1],
        activities=(*statements[1].activities, coin),
        account_summary=replace(
            statements[1].account_summary,
            closing_brokerage_cash_usd=Decimal("16"),
            closing_portfolio_value_usd=Decimal("41"),
        ),
    )
    activity_with_coin = replace(
        activity,
        primary_bundle=replace(
            activity.primary_bundle,
            statements=(statements[0], second),
        ),
        crypto_rows=(
            *activity.crypto_rows,
            RobinhoodCryptoStatementRow(RobinhoodStatementReference(1, coin.location)),
        ),
    )
    # February's combined cash identity holds only with the COIN credit in its net.
    reconcile_robinhood_cash(activity_with_coin, events)


def test_final_pending_period_remains_open() -> None:
    pending = _activity_row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 31),
        section=RobinhoodStatementSection.PENDING_TRADES,
        settle_date=date(2025, 2, 3),
        quantity=Decimal("1.0000"),
        price=Decimal("10.0000"),
        debit=Decimal("10.00"),
    )
    statement = _statement(
        start=_JAN_START,
        end=_JAN_END,
        opening=(None, None, None, None),
        closing=(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")),
        activities=(pending,),
        layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
    )
    bundle = validate_robinhood_statement_bundles((statement,))[0]
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 3),
        amount=Decimal("-10.00"),
        quantity=Decimal("1.0000"),
        price=Decimal("10.00"),
    )
    activity = RobinhoodActivityReconciliation(
        primary_bundle=bundle,
        activity_groups=(),
        in_kind_transfer_groups=(),
        pending_statement_events=(),
        pending_trade_groups=(
            RobinhoodPendingTradeGroup(
                event_ordinals=(1,),
                statement_references=(RobinhoodStatementReference(0, pending.location),),
            ),
        ),
        crypto_rows=(),
    )
    # The event is pending statement evidence in #459's primary disposition.
    activity = replace(
        activity,
        pending_statement_events=(RobinhoodPendingStatementEvent(1),),
    )
    result = reconcile_robinhood_cash(activity, (event,))
    assert result.periods[0].closing_unsettled_usd == Decimal("-10.00")


def test_disposition_reference_and_cash_cell_guards_refuse() -> None:
    activity, events = _baseline()
    with pytest.raises(RobinhoodReconciliationError, match="dispositions"):
        reconcile_robinhood_cash(
            replace(activity, activity_groups=activity.activity_groups[:-1]),
            events,
        )
    with pytest.raises(RobinhoodReconciliationError, match="ordinals"):
        reconcile_robinhood_cash(activity, (*events, replace(events[0], source_record_index=1)))

    invalid_ref = replace(
        activity.activity_groups[0],
        statement_references=(
            RobinhoodStatementReference(
                99,
                activity.activity_groups[0].statement_references[0].location,
            ),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="row dispositions"):
        reconcile_robinhood_cash(
            replace(
                activity,
                activity_groups=(
                    invalid_ref,
                    *activity.activity_groups[1:],
                ),
            ),
            events,
        )

    statements = activity.primary_bundle.statements
    bad_coin = replace(
        statements[0],
        activities=(
            replace(
                statements[0].activities[0],
                credit_usd=Decimal("-1"),
            ),
            statements[0].activities[1],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="non-negative"):
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(
                    activity.primary_bundle,
                    statements=(bad_coin, statements[1]),
                ),
            ),
            events,
        )


def _signed_total(statement: RobinhoodStatement, label: str) -> Decimal | None:
    """Credit minus debit on the statement's printed ``label`` total row."""
    totals = [total for total in statement.totals if total.label == label]
    if not totals:
        return None
    (total,) = totals
    values = dict(total.values)
    return values["Credit"] - values["Debit"]


def _cash_periods_match_statements(account: RobinhoodAccountReconciliation) -> bool:
    """One period per statement. Each statement prints a ``Total Funds Paid
    and Received`` (credit minus debit); where it prints opening balances,
    opening cash plus that total is the closing cash; and its period flags
    Program Bank evidence exactly when the statement prints sweep banks or
    sweep activity."""
    statements = account.activity.primary_bundle.statements
    if len(account.cash.periods) != len(statements):
        return False
    for period, statement in zip(account.cash.periods, statements, strict=True):
        summary = statement.account_summary
        activity_total = _signed_total(statement, "Total Funds Paid and Received")
        opening = (summary.opening_brokerage_cash_usd, summary.opening_deposit_sweep_usd)
        if not (
            activity_total is not None
            and period.program_bank_present
            == bool(statement.sweep_banks or statement.sweep_activity)
            and (
                opening[0] is None
                or opening[1] is None
                or opening[0] + opening[1] + activity_total
                == summary.closing_brokerage_cash_usd + summary.closing_deposit_sweep_usd
            )
        ):
            return False
    return True


@private_test
def test_private_cash_reconciliation_is_complete_and_redacted() -> None:
    paired = load_private_accounts_reconciliation()
    if not all(
        account.cash.activity_reconciliation is account.activity
        and _cash_periods_match_statements(account)
        for account in paired.accounts
    ):
        pytest.fail("private cash reconciliation retention audit failed", pytrace=False)


# --- #1185: exact statement cash signing and canonical zero -------------------

_PREFIX = "Robinhood reconciliation refused: "
_REFUSALS = {
    "activity": {
        "negative": _PREFIX + ACTIVITY_NEGATIVE_REFUSAL,
        "both": _PREFIX + ACTIVITY_BOTH_REFUSAL,
    },
    "cash": {
        "negative": _PREFIX + CASH_NEGATIVE_REFUSAL,
        "both": _PREFIX + CASH_BOTH_REFUSAL,
    },
}
_SETTLE_REFUSAL = _PREFIX + PENDING_SETTLE_REFUSAL
_MATCH_REFUSAL = _PREFIX + PENDING_MATCH_REFUSAL
_CASH_MARKERS = {
    "activity": ("SYNTHCASHACCOUNT", "SYNTHCASHID", "SYNTHCASHDESC", "SYNTHCASHSOURCE"),
    "cash": ("SYNTHCASHREASON",),
}
# 29 significant digits: one more than Python's default context precision.
_LONG = "100000000000000000000000000.01"
_MARKED_ACTIVITY = replace(
    _activity_row(row=1, code="ACH", transaction_date=_JAN_START),
    description="SYNTHCASHDESC",
    symbol="SYNTHCASHID",
    account_type="SYNTHCASHACCOUNT",
    source_account_reference="SYNTHCASHSOURCE",
)


def _sign_activity(debit: Decimal | None, credit: Decimal | None) -> Decimal | None:
    return robinhood_statement_signed_amount(
        replace(_MARKED_ACTIVITY, debit_usd=debit, credit_usd=credit)
    )


def _sign_sweep(debit: Decimal | None, credit: Decimal | None) -> Decimal | None:
    return _sweep_signed(replace(_MARKED_SWEEP, debit_usd=debit, credit_usd=credit))


_CALLERS: dict[str, Callable[[Decimal | None, Decimal | None], Decimal | None]] = {
    "activity": _sign_activity,
    "cash": _sign_sweep,
}


def _cell(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


@pytest.mark.parametrize("precision", [28, 6], ids=["prec28", "prec6"])
@pytest.mark.parametrize(
    "value",
    [_LONG, "12345678901234567890123456789012.345678", "1234567.89", "10.00", "0.01"],
)
@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_signing_keeps_every_source_digit_and_leaves_the_context_alone(
    caller: str,
    value: str,
    precision: int,
) -> None:
    """#1185: a credit is its cell unchanged and a debit the same digits
    negated, at any context precision. Signing neither rounds nor sets or
    clears a flag; the caller's precision, rounding and traps stay as set."""
    amount = Decimal(value)
    sign = _CALLERS[caller]
    with localcontext(Context(prec=precision)) as active:
        active.flags[Clamped] = True
        traps = dict(active.traps)
        credit = sign(None, amount)
        debit = sign(amount, None)
        assert (active.prec, active.rounding) == (precision, ROUND_HALF_EVEN)
        assert dict(active.traps) == traps
        assert [flag for flag, raised in active.flags.items() if raised] == [Clamped]
    assert credit is not None and debit is not None
    assert credit.as_tuple() == amount.as_tuple()
    assert debit.as_tuple() == amount.as_tuple()._replace(sign=1)


@pytest.mark.parametrize("rounding", [ROUND_HALF_EVEN, ROUND_FLOOR])
@pytest.mark.parametrize("zero", ["0", "0.00", "-0", "-0.000"])
@pytest.mark.parametrize("side", ["debit", "credit"])
@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_a_lone_zero_cell_signs_to_canonical_zero(
    caller: str,
    side: str,
    zero: str,
    rounding: str,
) -> None:
    """#1185: any single populated zero, whatever its sign or scale, is
    exactly ``Decimal("0")`` in every rounding mode, so lifecycle keys that
    compare Decimal tuples still pair a zero debit with a zero credit."""
    cells = (Decimal(zero), None) if side == "debit" else (None, Decimal(zero))
    with localcontext(Context(rounding=rounding)):
        result = _CALLERS[caller](*cells)
    assert result is not None
    assert result.as_tuple() == Decimal("0").as_tuple()


@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_a_row_without_either_cell_has_no_signed_amount(caller: str) -> None:
    assert _CALLERS[caller](None, None) is None


@pytest.mark.parametrize(
    ("debit", "credit", "refusal"),
    [
        ("-0.01", None, "negative"),
        (None, "-0.01", "negative"),
        ("-" + _LONG, None, "negative"),
        ("-1.00", "1.00", "negative"),
        ("1.00", "-1.00", "negative"),
        ("0", "-1.00", "negative"),
        ("-1.00", "0.00", "negative"),
        ("1.00", "1.00", "both"),
        (_LONG, "0.01", "both"),
        ("0", "0.00", "both"),
        ("-0", "-0.000", "both"),
        ("0.00", "1.00", "both"),
    ],
)
@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_signing_refusals_keep_their_messages_and_negative_first_order(
    caller: str,
    debit: str | None,
    credit: str | None,
    refusal: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185: a negative cell refuses before a row populating both cells,
    including when the other populated cell is zero, and both-populated
    zeros still refuse. Each refusal is the complete fixed message."""
    with pytest.raises(RobinhoodReconciliationError) as raised:
        _CALLERS[caller](_cell(debit), _cell(credit))
    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _REFUSALS[caller][refusal],
        capsys,
        markers=_CASH_MARKERS[caller],
    )


def _zero_lifecycle(
    pending: tuple[str | None, str | None],
    settled: tuple[str | None, str | None],
):
    """:func:`_baseline` plus a zero-cash pending buy, with the baseline
    pending buy's date, quantity and price, that settles as a zero-cash
    February row. Zero adds nothing to either period's cash."""
    activity, events = _baseline()
    first, second = activity.primary_bundle.statements
    pending_buy = first.activities[1]
    zero_pending = replace(
        pending_buy,
        location=replace(pending_buy.location, row=3),
        debit_usd=_cell(pending[0]),
        credit_usd=_cell(pending[1]),
    )
    settled_buy = second.activities[0]
    zero_settled = replace(
        settled_buy,
        location=replace(settled_buy.location, row=3),
        debit_usd=_cell(settled[0]),
        credit_usd=_cell(settled[1]),
    )
    buy_group, *other_groups = activity.activity_groups
    (pending_group,) = activity.pending_trade_groups
    activity = replace(
        activity,
        primary_bundle=replace(
            activity.primary_bundle,
            statements=(
                replace(first, activities=(*first.activities, zero_pending)),
                replace(second, activities=(*second.activities, zero_settled)),
            ),
        ),
        activity_groups=(
            replace(
                buy_group,
                statement_references=(
                    *buy_group.statement_references,
                    RobinhoodStatementReference(1, zero_settled.location),
                ),
            ),
            *other_groups,
        ),
        pending_trade_groups=(
            replace(
                pending_group,
                statement_references=(
                    *pending_group.statement_references,
                    RobinhoodStatementReference(0, zero_pending.location),
                ),
            ),
        ),
    )
    return activity, events


_ZERO_LIFECYCLES = [
    pytest.param(("0.00", None), (None, "0.00"), id="debit-0.00-credit-0.00"),
    pytest.param(("0", None), (None, "0.000"), id="debit-0-credit-0.000"),
    pytest.param(("-0", None), (None, "0.00"), id="debit--0-credit-0.00"),
    pytest.param(("-0.000", None), (None, "-0"), id="debit--0.000-credit--0"),
    pytest.param((None, "0.00"), ("-0.00", None), id="credit-0.00-debit--0.00"),
]


@pytest.mark.parametrize(("pending", "settled"), _ZERO_LIFECYCLES)
def test_pending_zero_cash_trade_still_matches_its_zero_cash_settlement(
    pending: tuple[str | None, str | None],
    settled: tuple[str | None, str | None],
) -> None:
    """#1185: zero signs to one canonical zero, so a pending zero debit and
    the next period's settled zero credit keep equal lifecycle keys whatever
    the zeros' signs and scales."""
    activity, events = _zero_lifecycle(pending, settled)
    result = reconcile_robinhood_cash(activity, events)

    first, second = result.periods
    assert first.closing_unsettled_usd == Decimal("-10.00")
    assert second.closing_unsettled_usd == 0


@pytest.mark.parametrize(
    ("pending", "settled"),
    [pytest.param(None, None, id="nonzero"), *_ZERO_LIFECYCLES[:2]],
)
def test_lifecycle_refusals_keep_their_complete_messages(
    pending: tuple[str | None, str | None] | None,
    settled: tuple[str | None, str | None] | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185: a valid nonzero or zero lifecycle reconciles. Its settled row's
    quantity exponent drifting, or its pending row settling outside the next
    statement period, still refuses with each complete message."""
    if pending is None or settled is None:
        activity, events = _baseline()
        pending_index, settled_index = 1, 0
    else:
        activity, events = _zero_lifecycle(pending, settled)
        pending_index, settled_index = 2, 2
    reconcile_robinhood_cash(activity, events)
    first, second = activity.primary_bundle.statements

    def changed(statement: RobinhoodStatement, index: int, **fields: object):
        rows = list(statement.activities)
        rows[index] = replace(rows[index], **fields)
        return replace(statement, activities=tuple(rows))

    drift = changed(second, settled_index, quantity=Decimal("1.0"))
    late = changed(first, pending_index, settle_date=date(2025, 3, 1))
    for statements, message in (
        ((first, drift), _MATCH_REFUSAL),
        ((late, second), _SETTLE_REFUSAL),
    ):
        with pytest.raises(RobinhoodReconciliationError) as raised:
            reconcile_robinhood_cash(
                replace(
                    activity,
                    primary_bundle=replace(activity.primary_bundle, statements=statements),
                ),
                events,
            )
        assert_fixed_refusal(raised.value, RobinhoodReconciliationError, message, capsys)


def test_lifecycle_amounts_differing_past_digit_28_no_longer_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185, a fail-closed consequence of exact signing: a pending debit and
    its next-period settlement that differ only past digit 28 rounded to the
    same lifecycle key on main, which matched them. Their exact digits
    disagree, so the lifecycle now refuses. February's opening balances are
    unavailable here, so no cash identity runs on the long amounts."""
    activity, (buy, deposit) = _baseline()
    first, second = activity.primary_bundle.statements
    first = replace(
        first,
        activities=(
            first.activities[0],
            replace(first.activities[1], debit_usd=Decimal(_LONG)),
        ),
    )
    second = replace(
        second,
        activities=(
            replace(second.activities[0], debit_usd=Decimal("100000000000000000000000000.02")),
            *second.activities[1:],
        ),
        account_summary=replace(
            second.account_summary,
            opening_brokerage_cash_usd=None,
            opening_deposit_sweep_usd=None,
            opening_total_securities_usd=None,
            opening_portfolio_value_usd=None,
        ),
    )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(activity.primary_bundle, statements=(first, second)),
            ),
            (replace(buy, amount_usd=Decimal("-" + _LONG)), deposit),
        )
    assert_fixed_refusal(raised.value, RobinhoodReconciliationError, _MATCH_REFUSAL, capsys)


@pytest.mark.parametrize("zero", ["0", "0.00", "-0", "-0.000"])
@pytest.mark.parametrize("side", ["debit_usd", "credit_usd"])
def test_zero_sweep_movement_keeps_the_sweep_net(zero: str, side: str) -> None:
    """#1185: a populated zero sweep cell adds canonical zero to the Deposit
    Sweep net, which still reconciles to the Account Summary."""
    activity, events = _baseline()
    first, second = activity.primary_bundle.statements
    zero_row = replace(
        _sweep_row(row=4, transaction_date=date(2025, 2, 11), balance=Decimal("25")),
        **{side: Decimal(zero)},
    )
    second = replace(
        second,
        sweep_activity=(*second.sweep_activity[:2], zero_row, second.sweep_activity[2]),
    )
    # February's Deposit Sweep net reconciles only if the zero row adds nothing.
    reconcile_robinhood_cash(
        replace(
            activity,
            primary_bundle=replace(activity.primary_bundle, statements=(first, second)),
        ),
        events,
    )


@pytest.mark.parametrize(
    ("debit", "credit", "refusal"),
    [
        ("-5", None, "negative"),
        ("-5", "5", "negative"),
        ("0", "-5", "negative"),
        ("5", "5", "both"),
        ("0", "0.00", "both"),
    ],
)
@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_cash_reconciliation_retains_each_signing_refusal(
    caller: str,
    debit: str,
    credit: str | None,
    refusal: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185: Account Activity and Deposit Sweep rows reach the same fixed
    refusals through public cash reconciliation, before any cash identity."""
    activity, events = _baseline()
    first, second = activity.primary_bundle.statements
    if caller == "activity":
        marker = "SYNTHCASHDESC"
        coin = replace(
            first.activities[0],
            description=marker,
            debit_usd=_cell(debit),
            credit_usd=_cell(credit),
        )
        first = replace(first, activities=(coin, *first.activities[1:]))
    else:
        marker = "SYNTHCASHREASON"
        movement = replace(
            first.sweep_activity[1],
            description=marker,
            debit_usd=_cell(debit),
            credit_usd=_cell(credit),
        )
        first = replace(
            first,
            sweep_activity=(first.sweep_activity[0], movement, *first.sweep_activity[2:]),
        )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_cash(
            replace(
                activity,
                primary_bundle=replace(activity.primary_bundle, statements=(first, second)),
            ),
            events,
        )
    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _REFUSALS[caller][refusal],
        capsys,
        markers=(marker,),
    )


# --- #1190: cash identities and the pending lifecycle still refuse first -----

_IDENTITY_REFUSAL = _PREFIX + CASH_IDENTITY_REFUSAL
_DISPOSITION_REFUSAL = _PREFIX + "Account Activity row dispositions are incomplete or duplicated"


def test_marked_baseline_reconciles() -> None:
    """The markers alone change no cash figure or lifecycle key."""
    activity, events = _with_faults()
    first, second = reconcile_robinhood_cash(activity, events).periods
    assert first.closing_unsettled_usd == Decimal("-10.00")
    assert second.closing_unsettled_usd == 0


@pytest.mark.parametrize(
    ("faults", "message"),
    [
        pytest.param(("identity",), _IDENTITY_REFUSAL, id="identity"),
        pytest.param(("pending-cash",), _PENDING_CASH_REFUSAL, id="pending-cash"),
        pytest.param(("late",), _SETTLE_REFUSAL, id="late"),
        pytest.param(("drift",), _MATCH_REFUSAL, id="drift"),
        pytest.param(("disposition", "identity"), _DISPOSITION_REFUSAL, id="disposition-first"),
        pytest.param(("pending-cash", "identity"), _PENDING_CASH_REFUSAL, id="january-first"),
        pytest.param(("identity", "late"), _IDENTITY_REFUSAL, id="identity-before-lifecycle"),
        pytest.param(("identity", "drift"), _IDENTITY_REFUSAL, id="identity-before-match"),
        pytest.param(("late", "drift"), _SETTLE_REFUSAL, id="settle-before-match"),
    ],
)
def test_cash_and_lifecycle_refusals_keep_their_messages_and_order(
    faults: tuple[str, ...],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1190: unused period fields are gone, but every per-period
    check and the lifecycle check after them still run in their order.
    Dispositions refuse first, then January's checks before February's,
    then the lifecycle, whose settlement check comes before its match.
    Each refusal is the complete fixed message with no account, holder or
    row text."""
    activity, events = _with_faults(*faults)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_cash(activity, events)
    assert_fixed_refusal(
        raised.value, RobinhoodReconciliationError, message, capsys, markers=_CLEAN_MARKERS
    )
