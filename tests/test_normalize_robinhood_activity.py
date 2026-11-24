"""Activity-event reconciliation checks for Robinhood."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountReconciliation,
    RobinhoodReconciliationError,
    RobinhoodStatementReference,
    reconcile_robinhood_activity,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    apply_cusip_transitions,
)
from ibkr_fa.parser.robinhood import (
    RobinhoodEvent,
    RobinhoodEventKind,
    parse_robinhood_activity,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementSection,
)
from tests._crash_reports import assert_fixed_refusal
from tests._private_checks import private_test
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
    load_private_report_records,
)
from tests._robinhood_refusals import CUSIP_CONTRADICTION
from tests._robinhood_statements import activity_rows_csv as _csv
from tests._robinhood_statements import (
    robinhood_account_summary,
    robinhood_activity,
    robinhood_statement,
)

_JAN_START = date(2025, 1, 1)
_JAN_END = date(2025, 1, 31)
_FEB_START = date(2025, 2, 1)
_FEB_END = date(2025, 2, 28)
_CUSIP_A = "99961A106"
_CUSIP_B = "99961B104"

type _Contract = tuple[str, date, str, Decimal]


def _row(
    *,
    row: int,
    code: str,
    transaction_date: date,
    section: RobinhoodStatementSection = RobinhoodStatementSection.ACCOUNT_ACTIVITY,
    settle_date: date | None = None,
    symbol: str | None = None,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
    description: str = "Synthetic activity",
    cusips: tuple[str, ...] = (),
    contract: _Contract | None = None,
) -> RobinhoodStatementActivity:
    option_symbol, expiration, right, strike = contract or (None, None, None, None)
    return robinhood_activity(
        row=row,
        transaction_code=code,
        transaction_date=transaction_date,
        section=section,
        description=description,
        symbol=symbol,
        settle_date=settle_date,
        quantity=quantity,
        price_usd=price,
        debit_usd=debit,
        credit_usd=credit,
        cusips=cusips,
        option_symbol=option_symbol,
        option_expiration=expiration,
        option_right=right,
        option_strike_usd=strike,
    )


def _statement(
    *,
    account: str = "111111111",
    family: RobinhoodStatementFamily = RobinhoodStatementFamily.INDIVIDUAL,
    start: date = _JAN_START,
    end: date = _JAN_END,
    opening: tuple[
        Decimal | None,
        Decimal | None,
        Decimal | None,
        Decimal | None,
    ] = (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    ),
    closing: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    ),
    activities: tuple[RobinhoodStatementActivity, ...] = (),
) -> RobinhoodStatement:
    return robinhood_statement(
        family=family,
        period_start=start,
        period_end=end,
        account_number=account,
        account_summary=robinhood_account_summary(opening=opening, closing=closing),
        activities=activities,
    )


def _bundle(*statements: RobinhoodStatement):
    return validate_robinhood_statement_bundles(statements)[0]


def _event(
    *,
    ordinal: int,
    kind: RobinhoodEventKind,
    activity_date: date,
    settle_date: date,
    instrument: str | None = None,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    amount: Decimal | None = None,
    contract: _Contract | None = None,
) -> RobinhoodEvent:
    issuer_name = None
    cusip = None
    suffix = None
    if kind in {
        RobinhoodEventKind.EQUITY_BUY,
        RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
        RobinhoodEventKind.EQUITY_SELL,
        RobinhoodEventKind.SECURITY_RECEIPT,
        RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
    }:
        issuer_name = "Synthetic issuer"
        cusip = "99971T103"
    if kind is RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER:
        suffix = "S"
    code = {
        RobinhoodEventKind.ACH_DEPOSIT: "ACH",
        RobinhoodEventKind.ACH_WITHDRAWAL: "ACH",
        RobinhoodEventKind.EQUITY_BUY: "Buy",
        RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY: "Buy",
        RobinhoodEventKind.EQUITY_SELL: "Sell",
        RobinhoodEventKind.SECURITY_RECEIPT: "REC",
        RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER: "ITRF",
        RobinhoodEventKind.OPTION_BUY_TO_OPEN: "BTO",
        RobinhoodEventKind.OPTION_SELL_TO_CLOSE: "STC",
    }[kind]
    option_symbol, expiration, right, strike = contract or (None, None, None, None)
    return RobinhoodEvent(
        kind=kind,
        source_record_index=ordinal,
        activity_date=activity_date,
        process_date=activity_date,
        settle_date=settle_date,
        instrument=instrument,
        description="Synthetic event",
        trans_code=code,
        quantity=quantity,
        quantity_suffix=suffix,
        price_usd=price,
        amount_usd=amount,
        issuer_name=issuer_name,
        cusip=cusip,
        option_symbol=option_symbol,
        option_expiration=expiration,
        option_right=right,
        option_strike_usd=strike,
    )


def _baseline():
    rows = (
        _row(
            row=1,
            code="ACH",
            transaction_date=date(2025, 1, 5),
            credit=Decimal("100.00"),
        ),
        _row(
            row=2,
            code="Buy",
            transaction_date=date(2025, 1, 10),
            symbol="EQTY",
            quantity=Decimal("1.0000"),
            price=Decimal("10.0049"),
            debit=Decimal("10.00"),
        ),
        _row(
            row=3,
            code="REC",
            transaction_date=date(2025, 1, 15),
            quantity=Decimal("0.9999"),
        ),
        _row(
            row=4,
            code="ITRF",
            transaction_date=date(2025, 1, 20),
            symbol="XFER",
            quantity=Decimal("1"),
        ),
        _row(
            row=5,
            code="ITRF",
            transaction_date=date(2025, 1, 20),
            symbol="XFER",
            quantity=Decimal("0.25"),
        ),
        _row(
            row=6,
            code="COIN",
            transaction_date=date(2025, 1, 21),
            debit=Decimal("5.00"),
        ),
        _row(
            row=7,
            code="Buy",
            transaction_date=date(2025, 1, 31),
            section=RobinhoodStatementSection.PENDING_TRADES,
            settle_date=date(2025, 2, 3),
            symbol="IGNORED",
            quantity=Decimal("2"),
            price=Decimal("10.0049"),
            debit=Decimal("20.00"),
        ),
    )
    primary = _bundle(_statement(activities=rows))
    counterpart = _bundle(
        _statement(
            account="222222222",
            family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            activities=(
                _row(
                    row=1,
                    code="ADVFEE",
                    transaction_date=date(2025, 1, 5),
                    debit=Decimal("1.00"),
                ),
            ),
        )
    )
    events = (
        _event(
            ordinal=1,
            kind=RobinhoodEventKind.ACH_DEPOSIT,
            activity_date=date(2025, 1, 5),
            settle_date=date(2025, 1, 5),
            amount=Decimal("100.00"),
        ),
        _event(
            ordinal=2,
            kind=RobinhoodEventKind.EQUITY_BUY,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 13),
            instrument="EQTY",
            quantity=Decimal("1.0000"),
            price=Decimal("10.00"),
            amount=Decimal("-10.00"),
        ),
        _event(
            ordinal=3,
            kind=RobinhoodEventKind.SECURITY_RECEIPT,
            activity_date=date(2025, 1, 15),
            settle_date=date(2025, 1, 15),
            instrument="REWARD",
            quantity=Decimal("1.0000"),
        ),
        _event(
            ordinal=4,
            kind=RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
            activity_date=date(2025, 1, 20),
            settle_date=date(2025, 1, 20),
            instrument="XFER",
            quantity=Decimal("1.0000"),
        ),
        _event(
            ordinal=5,
            kind=RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
            activity_date=date(2025, 1, 20),
            settle_date=date(2025, 1, 20),
            instrument="XFER",
            quantity=Decimal("0.2500"),
        ),
        _event(
            ordinal=6,
            kind=RobinhoodEventKind.EQUITY_BUY,
            activity_date=date(2025, 1, 31),
            settle_date=date(2025, 2, 3),
            instrument="PEND",
            quantity=Decimal("2"),
            price=Decimal("10.00"),
            amount=Decimal("-20.00"),
        ),
        _event(
            ordinal=7,
            kind=RobinhoodEventKind.ACH_DEPOSIT,
            activity_date=date(2025, 2, 5),
            settle_date=date(2025, 2, 5),
            amount=Decimal("1.00"),
        ),
    )
    return primary, counterpart, events


def test_reconciles_exact_banded_rec_itrf_pending_crypto_and_counterpart() -> None:
    primary, counterpart, events = _baseline()
    result = reconcile_robinhood_activity(primary, (counterpart,), events)

    assert {group.transaction_code for group in result.activity_groups} == {
        "ACH",
        "Buy",
        "REC",
    }
    assert len(result.in_kind_transfer_groups) == 1
    assert result.in_kind_transfer_groups[0].event_ordinals == (4, 5)
    assert {item.event_ordinal for item in result.pending_statement_events} == {
        6,
        7,
    }
    assert len(result.pending_trade_groups) == 1
    assert result.pending_trade_groups[0].event_ordinals == (6,)
    assert len(result.crypto_rows) == 1
    all_refs = [ref for group in result.activity_groups for ref in group.statement_references]
    assert all(ref.statement_index == 0 for ref in all_refs)
    assert repr(result) == object.__repr__(result)
    assert "Synthetic event" not in repr(result)


def test_settle_date_selects_statement_but_activity_date_matches_row() -> None:
    first = _statement(
        closing=(Decimal("1"), Decimal("0"), Decimal("0"), Decimal("1")),
    )
    settled_row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 31),
        symbol="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        debit=Decimal("10.00"),
    )
    second = _statement(
        start=_FEB_START,
        end=_FEB_END,
        opening=(Decimal("1"), Decimal("0"), Decimal("0"), Decimal("1")),
        activities=(settled_row,),
    )
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 3),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    result = reconcile_robinhood_activity(_bundle(first, second), (), (event,))
    assert result.activity_groups[0].statement_references[0].statement_index == 1


def test_exact_duplicates_reconcile_by_equal_multiplicity() -> None:
    row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        debit=Decimal("10.00"),
    )
    bundle = _bundle(
        _statement(
            activities=(
                row,
                replace(row, location=replace(row.location, row=2)),
            )
        )
    )
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 13),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    result = reconcile_robinhood_activity(
        bundle,
        (),
        (event, replace(event, source_record_index=2)),
    )
    assert result.activity_groups[0].event_ordinals == (1, 2)

    with pytest.raises(RobinhoodReconciliationError, match="multiplicities"):
        reconcile_robinhood_activity(bundle, (), (event,))


def test_blank_symbol_multi_instrument_group_refuses() -> None:
    rows = (
        _row(
            row=1,
            code="REC",
            transaction_date=date(2025, 1, 10),
            quantity=Decimal("1"),
        ),
        _row(
            row=2,
            code="REC",
            transaction_date=date(2025, 1, 10),
            quantity=Decimal("1"),
        ),
    )
    events = tuple(
        _event(
            ordinal=index,
            kind=RobinhoodEventKind.SECURITY_RECEIPT,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 10),
            instrument=instrument,
            quantity=Decimal("1"),
        )
        for index, instrument in ((1, "ONE"), (2, "XQGB"))
    )
    with pytest.raises(RobinhoodReconciliationError, match="multiple instruments"):
        reconcile_robinhood_activity(_bundle(_statement(activities=rows)), (), events)


def test_per_row_quantity_is_exact_but_rec_quantity_is_banded() -> None:
    buy_row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="EQTY",
        quantity=Decimal("1.01"),
        price=Decimal("10.00"),
        debit=Decimal("10.00"),
    )
    buy = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument="EQTY",
        quantity=Decimal("1.0"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    with pytest.raises(RobinhoodReconciliationError, match="quantity or price"):
        reconcile_robinhood_activity(_bundle(_statement(activities=(buy_row,))), (), (buy,))

    rec_row = _row(
        row=1,
        code="REC",
        transaction_date=date(2025, 1, 10),
        quantity=Decimal("1.01"),
    )
    rec = _event(
        ordinal=1,
        kind=RobinhoodEventKind.SECURITY_RECEIPT,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument="REWARD",
        quantity=Decimal("1.0"),
    )
    assert reconcile_robinhood_activity(
        _bundle(_statement(activities=(rec_row,))),
        (),
        (rec,),
    ).activity_groups


def test_banded_matching_preserves_decimal_display_scale() -> None:
    ambiguous_rows = tuple(
        _row(
            row=index,
            code="REC",
            transaction_date=date(2025, 1, 10),
            quantity=Decimal(quantity),
        )
        for index, quantity in enumerate(
            ("1.06", "1.0", "0.9", "1.000"),
            start=1,
        )
    )
    ambiguous_events = tuple(
        _event(
            ordinal=index,
            kind=RobinhoodEventKind.SECURITY_RECEIPT,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 10),
            instrument="REWARD",
            quantity=Decimal(quantity),
        )
        for index, quantity in enumerate(
            ("1.00", "1.06", "0.95", "1.0"),
            start=1,
        )
    )
    with pytest.raises(
        RobinhoodReconciliationError,
        match="display-precision matching is ambiguous",
    ):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=ambiguous_rows)),
            (),
            ambiguous_events,
        )

    unique_rows = tuple(
        _row(
            row=index,
            code="REC",
            transaction_date=date(2025, 1, 10),
            quantity=Decimal(quantity),
        )
        for index, quantity in enumerate(("1.0", "1.00", "1.05"), start=1)
    )
    unique_events = tuple(
        _event(
            ordinal=index,
            kind=RobinhoodEventKind.SECURITY_RECEIPT,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 10),
            instrument="REWARD",
            quantity=Decimal(quantity),
        )
        for index, quantity in enumerate(("1.00", "1.04", "1.04"), start=1)
    )
    assert reconcile_robinhood_activity(
        _bundle(_statement(activities=unique_rows)),
        (),
        unique_events,
    ).activity_groups


def test_matching_search_budget_refuses_pathological_candidate_graph() -> None:
    rows = tuple(
        _row(
            row=index,
            code="REC",
            transaction_date=date(2025, 1, 10),
            quantity=Decimal(quantity),
        )
        for index, quantity in enumerate(
            ("0.8", "0.9", "1.0", "1.1", "1.2", "1.3", "1.4", "1.5"),
            start=1,
        )
    )
    events = tuple(
        _event(
            ordinal=index,
            kind=RobinhoodEventKind.SECURITY_RECEIPT,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 10),
            instrument="REWARD",
            quantity=Decimal("1"),
        )
        for index in range(1, len(rows) + 1)
    )
    with pytest.raises(RobinhoodReconciliationError, match="safe search budget"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=rows)),
            (),
            events,
        )


def test_price_band_passes_refuses_and_detects_ambiguous_assignment() -> None:
    baseline_row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.0049"),
        debit=Decimal("10.00"),
    )
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    assert reconcile_robinhood_activity(
        _bundle(_statement(activities=(baseline_row,))),
        (),
        (event,),
    ).activity_groups
    with pytest.raises(RobinhoodReconciliationError, match="quantity or price"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=(replace(baseline_row, price_usd=Decimal("10.006")),))),
            (),
            (event,),
        )

    rows = (
        replace(baseline_row, price_usd=Decimal("10.35")),
        replace(
            baseline_row,
            location=replace(baseline_row.location, row=2),
            price_usd=Decimal("10.45"),
        ),
    )
    events = (
        replace(event, price_usd=Decimal("10"), source_record_index=1),
        replace(event, price_usd=Decimal("10.4"), source_record_index=2),
    )
    with pytest.raises(RobinhoodReconciliationError, match="ambiguous"):
        reconcile_robinhood_activity(_bundle(_statement(activities=rows)), (), events)


def test_exact_matching_ambiguity_refuses_distinct_event_and_row_classes() -> None:
    base_row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        debit=Decimal("10.00"),
    )
    rows = (
        base_row,
        replace(
            base_row,
            location=replace(base_row.location, row=2),
            symbol=None,
        ),
    )
    base_event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    events = (
        base_event,
        _event(
            ordinal=2,
            kind=RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
            activity_date=date(2025, 1, 10),
            settle_date=date(2025, 1, 10),
            instrument="EQTY",
            quantity=Decimal("1"),
            price=Decimal("10.00"),
            amount=Decimal("-10.00"),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="exact matching is ambiguous"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=rows)),
            (),
            events,
        )


def test_identity_check_preserves_supported_blank_symbol_row() -> None:
    base_row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        debit=Decimal("10.00"),
        cusips=("99971T103",),
    )
    rows = (
        base_row,
        replace(
            base_row,
            location=replace(base_row.location, row=2),
            symbol=None,
            cusips=(),
        ),
    )
    base_event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    events = (
        base_event,
        replace(base_event, source_record_index=2),
    )
    reconciliation = reconcile_robinhood_activity(
        _bundle(_statement(activities=rows)),
        (),
        events,
    )

    checked = apply_cusip_transitions(
        reconciliation,
        events,
        account_number="111111111",
        transitions=(),
    )

    assert checked.cusip_transition_uses == ()


def test_blank_symbol_activity_row_must_print_its_event_cusip() -> None:
    primary, _, events = _baseline()
    statement = primary.statements[0]
    events = tuple(
        replace(event, cusip=_CUSIP_A) if event.source_record_index == 2 else event
        for event in events
    )

    def printing(cusip: str):
        return _bundle(
            replace(
                statement,
                activities=tuple(
                    replace(row, symbol=None, cusips=(cusip,)) if row.location.row == 2 else row
                    for row in statement.activities
                ),
            )
        )

    assert reconcile_robinhood_activity(printing(_CUSIP_A), (), events).activity_groups
    with pytest.raises(RobinhoodReconciliationError, match="statement CUSIP contradicts"):
        reconcile_robinhood_activity(printing(_CUSIP_B), (), events)


def test_in_kind_aggregate_requires_equal_keys_counts_and_max_ulp_band() -> None:
    primary, _, events = _baseline()
    assert reconcile_robinhood_activity(primary, (), events).in_kind_transfer_groups

    within_band = tuple(
        replace(event, quantity=Decimal("1.4")) if event.source_record_index == 4 else event
        for event in events
    )
    assert reconcile_robinhood_activity(
        primary,
        (),
        within_band,
    ).in_kind_transfer_groups

    issued = tuple(event for event in events if event.source_record_index != 5)
    with pytest.raises(RobinhoodReconciliationError, match="row counts"):
        reconcile_robinhood_activity(primary, (), issued)

    wrong_key = tuple(
        replace(event, instrument="OTHER") if event.source_record_index == 4 else event
        for event in events
    )
    with pytest.raises(RobinhoodReconciliationError, match="group keys"):
        reconcile_robinhood_activity(primary, (), wrong_key)

    bad_events = tuple(
        replace(event, quantity=Decimal("1.6")) if event.source_record_index == 4 else event
        for event in events
    )
    with pytest.raises(RobinhoodReconciliationError, match="aggregate quantity"):
        reconcile_robinhood_activity(primary, (), bad_events)


def test_pending_trade_requires_exact_quantity_banded_price_and_no_symbol() -> None:
    primary, _, events = _baseline()
    result = reconcile_robinhood_activity(primary, (), events)
    assert result.pending_trade_groups[0].event_ordinals == (6,)

    pending = next(event for event in events if event.source_record_index == 6)
    bad = tuple(
        replace(pending, quantity=Decimal("2.1")) if event.source_record_index == 6 else event
        for event in events
    )
    with pytest.raises(
        RobinhoodReconciliationError,
        match="pending trade quantity or price",
    ):
        reconcile_robinhood_activity(primary, (), bad)


def test_pending_trade_multiplicity_and_exact_or_banded_ambiguity_refuse() -> None:
    primary, _, events = _baseline()
    statement = primary.statements[0]
    pending_row = next(
        row
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
    )
    duplicated = replace(
        statement,
        activities=(
            *statement.activities,
            replace(
                pending_row,
                location=replace(pending_row.location, row=8),
            ),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="multiplicities"):
        reconcile_robinhood_activity(_bundle(duplicated), (), events)

    exact_rows = (
        replace(pending_row, symbol=None, price_usd=Decimal("10.00")),
        replace(
            pending_row,
            location=replace(pending_row.location, row=8),
            symbol="IGNORED",
            price_usd=Decimal("10.00"),
        ),
    )
    exact_events = (
        next(event for event in events if event.source_record_index == 6),
        _event(
            ordinal=8,
            kind=RobinhoodEventKind.DIVIDEND_REINVESTMENT_BUY,
            activity_date=date(2025, 1, 31),
            settle_date=date(2025, 2, 3),
            instrument="OTHER",
            quantity=Decimal("2"),
            price=Decimal("10.00"),
            amount=Decimal("-20.00"),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="exact matching is ambiguous"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=exact_rows)),
            (),
            exact_events,
        )

    band_rows = (
        replace(pending_row, symbol=None, price_usd=Decimal("10.35")),
        replace(
            pending_row,
            location=replace(pending_row.location, row=8),
            symbol="IGNORED",
            price_usd=Decimal("10.45"),
        ),
    )
    band_events = (
        replace(exact_events[0], price_usd=Decimal("10")),
        replace(exact_events[1], price_usd=Decimal("10.4")),
    )
    with pytest.raises(
        RobinhoodReconciliationError,
        match="display-precision matching is ambiguous",
    ):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=band_rows)),
            (),
            band_events,
        )


def _pending_printing(*cusips: str):
    """``_baseline`` with a symbol-less pending row printing ``cusips``."""

    primary, _, events = _baseline()
    statement = primary.statements[0]
    rows = tuple(
        replace(row, symbol=None, cusips=cusips)
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
        else row
        for row in statement.activities
    )
    events = tuple(
        replace(event, cusip=_CUSIP_A) if event.source_record_index == 6 else event
        for event in events
    )
    return _bundle(replace(statement, activities=rows)), events


@pytest.mark.parametrize("cusips", [(_CUSIP_A,), (_CUSIP_A, _CUSIP_B)])
def test_pending_row_printing_its_trade_cusip_first_proves_the_trade(
    cusips: tuple[str, ...],
) -> None:
    primary, events = _pending_printing(*cusips)
    result = reconcile_robinhood_activity(primary, (), events)
    assert result.pending_trade_groups[0].event_ordinals == (6,)


@pytest.mark.parametrize("cusips", [(_CUSIP_B,), (_CUSIP_B, _CUSIP_A)])
def test_pending_row_printing_another_cusip_first_refuses(cusips: tuple[str, ...]) -> None:
    primary, events = _pending_printing(*cusips)
    with pytest.raises(RobinhoodReconciliationError, match=CUSIP_CONTRADICTION) as raised:
        reconcile_robinhood_activity(primary, (), events)
    assert raised.value.message == CUSIP_CONTRADICTION


def test_pending_rows_are_checked_against_the_trades_they_pair_with() -> None:
    primary, _, events = _baseline()
    pending_row = next(
        row
        for row in primary.statements[0].activities
        if row.location.section is RobinhoodStatementSection.PENDING_TRADES
    )
    trade_a = replace(
        next(event for event in events if event.source_record_index == 6),
        cusip=_CUSIP_A,
    )
    trade_b = replace(
        trade_a,
        source_record_index=8,
        instrument="OTHER",
        cusip=_CUSIP_B,
        quantity=Decimal("1"),
        price_usd=Decimal("20.00"),
    )

    def rows(two_at_ten: str, one_at_twenty: str):
        return _bundle(
            _statement(
                activities=(
                    replace(
                        pending_row,
                        symbol=None,
                        price_usd=Decimal("10.00"),
                        cusips=(two_at_ten,),
                    ),
                    replace(
                        pending_row,
                        location=replace(pending_row.location, row=8),
                        symbol=None,
                        quantity=Decimal("1"),
                        price_usd=Decimal("20.00"),
                        cusips=(one_at_twenty,),
                    ),
                )
            )
        )

    result = reconcile_robinhood_activity(rows(_CUSIP_A, _CUSIP_B), (), (trade_a, trade_b))
    assert result.pending_trade_groups[0].event_ordinals == (6, 8)
    with pytest.raises(RobinhoodReconciliationError, match="statement CUSIP contradicts"):
        reconcile_robinhood_activity(rows(_CUSIP_B, _CUSIP_A), (), (trade_a, trade_b))


_EXPIRY = date(2025, 12, 19)
_STRIKE_100: _Contract = ("XQOA", _EXPIRY, "Call", Decimal("100.00"))
_STRIKE_120: _Contract = ("XQOA", _EXPIRY, "Call", Decimal("120.00"))
_CONTRACT = "statement option contract contradicts activity option contract"
_ACCOUNT = "Account Activity"
_PENDING = "Executed Trades Pending Settlement"
# Where each close settles: its statement, section and first row.
_CLOSES = {
    "same-month": (date(2025, 1, 20), date(2025, 1, 21), _ACCOUNT, "statement 1 page 2 row 3"),
    "next-month": (date(2025, 2, 20), date(2025, 2, 21), _ACCOUNT, "statement 2 page 2 row 1"),
    "pending": (date(2025, 1, 31), date(2025, 2, 3), _PENDING, "statement 1 page 2 row 3"),
}


def _option_trades(close: str, *contracts: _Contract):
    """Two XQOA Calls bought on 5 Jan for $100 and $200 and sold for $150 and
    $250, with the closes settling as ``_CLOSES[close]`` says. The statement
    prints the honest contracts, strikes $100.00 then $120.00; the CSV closes
    say ``contracts``."""

    close_date, settle_date, section, _first = _CLOSES[close]
    opened = date(2025, 1, 5)
    opens = tuple(
        _row(
            row=row,
            code="BTO",
            transaction_date=opened,
            symbol="XQOA",
            quantity=Decimal("1"),
            price=price,
            debit=price * 100,
            contract=contract,
        )
        for row, price, contract in (
            (1, Decimal("1.00"), _STRIKE_100),
            (2, Decimal("2.00"), _STRIKE_120),
        )
    )
    pending = section == _PENDING
    closes = tuple(
        _row(
            row=row,
            code="STC",
            transaction_date=close_date,
            section=(
                RobinhoodStatementSection.PENDING_TRADES
                if pending
                else RobinhoodStatementSection.ACCOUNT_ACTIVITY
            ),
            settle_date=settle_date if pending else None,
            symbol=None if pending else "XQOA",
            quantity=Decimal("1"),
            price=price,
            credit=price * 100,
            contract=contract,
        )
        for row, price, contract in (
            (1 if close == "next-month" else 3, Decimal("1.50"), _STRIKE_100),
            (2 if close == "next-month" else 4, Decimal("2.50"), _STRIKE_120),
        )
    )
    if close == "next-month":
        statements = (
            _statement(activities=opens),
            _statement(start=_FEB_START, end=_FEB_END, activities=closes),
        )
    else:
        statements = (_statement(activities=(*opens, *closes)),)
    events = (
        *(
            _event(
                ordinal=ordinal,
                kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
                activity_date=opened,
                settle_date=date(2025, 1, 6),
                instrument="XQOA",
                quantity=Decimal("1"),
                price=price,
                amount=-price * 100,
                contract=contract,
            )
            for ordinal, price, contract in (
                (1, Decimal("1.00"), _STRIKE_100),
                (2, Decimal("2.00"), _STRIKE_120),
            )
        ),
        *(
            _event(
                ordinal=ordinal,
                kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
                activity_date=close_date,
                settle_date=settle_date,
                instrument=contract[0],
                quantity=Decimal("1"),
                price=price,
                amount=price * 100,
                contract=contract,
            )
            for ordinal, price, contract in zip(
                (3, 4), (Decimal("1.50"), Decimal("2.50")), contracts, strict=True
            )
        ),
    )
    return _bundle(*statements), events


def _references(*rows: tuple[int, int]) -> tuple[tuple[int, int, int], ...]:
    """(statement index, page, row) for each (statement index, row) on page 2."""

    return tuple((statement, 2, row) for statement, row in rows)


def _group_rows(group) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (ref.statement_index, ref.location.page, ref.location.row)
        for ref in group.statement_references
    )


@pytest.mark.parametrize("close", list(_CLOSES))
def test_honest_option_contracts_reconcile_in_the_same_groups(close: str) -> None:
    primary, events = _option_trades(close, _STRIKE_100, _STRIKE_120)

    result = reconcile_robinhood_activity(primary, (), events)

    groups = [(group.event_ordinals, _group_rows(group)) for group in result.activity_groups]
    pending = [(group.event_ordinals, _group_rows(group)) for group in result.pending_trade_groups]
    opens = [((1,), _references((0, 1))), ((2,), _references((0, 2)))]
    closes = {
        "same-month": [((3,), _references((0, 3))), ((4,), _references((0, 4)))],
        "next-month": [((3,), _references((1, 1))), ((4,), _references((1, 2)))],
        "pending": [((3,), _references((0, 3))), ((4,), _references((0, 4)))],
    }[close]
    if close == "pending":
        assert (groups, pending) == (opens, closes)
    else:
        assert (groups, pending) == ([*opens, *closes], [])


@pytest.mark.parametrize("close", list(_CLOSES))
@pytest.mark.parametrize(
    "first_close",
    [
        pytest.param(_STRIKE_120, id="swapped-strikes"),
        pytest.param(("XQOA", date(2026, 1, 16), "Call", Decimal("100.00")), id="other-expiry"),
        pytest.param(("XQOA", _EXPIRY, "Put", Decimal("100.00")), id="other-right"),
    ],
)
def test_a_close_whose_contract_contradicts_its_row_refuses(
    close: str, first_close: _Contract
) -> None:
    """The $150 close's group refuses first; the swapped-strike $250 close
    contradicts its row too."""

    second_close = _STRIKE_100 if first_close == _STRIKE_120 else _STRIKE_120
    primary, events = _option_trades(close, first_close, second_close)
    _close_date, _settle_date, section, first_row = _CLOSES[close]

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(primary, (), events)

    assert raised.value.message == f"{_CONTRACT} ({section}: {first_row}; column Description)"


def test_a_pending_close_for_another_underlying_refuses() -> None:
    other = ("XQOB", _EXPIRY, "Call", Decimal("100.00"))
    primary, events = _option_trades("pending", other, _STRIKE_120)

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(primary, (), events)

    assert raised.value.message == (
        f"{_CONTRACT} ({_PENDING}: statement 1 page 2 row 3; column Description)"
    )


def _two_closes(
    *,
    pending: bool,
    row_prices: tuple[Decimal, Decimal],
    row_contracts: tuple[_Contract, _Contract],
    event_prices: tuple[Decimal, Decimal],
    event_contracts: tuple[_Contract, _Contract],
):
    """Two STC closes in one numeric group: the same day, quantity and amount."""

    section = (
        RobinhoodStatementSection.PENDING_TRADES
        if pending
        else RobinhoodStatementSection.ACCOUNT_ACTIVITY
    )
    rows = tuple(
        _row(
            row=row,
            code="STC",
            transaction_date=date(2025, 1, 31 if pending else 20),
            section=section,
            settle_date=date(2025, 2, 3) if pending else None,
            symbol=None if pending else contract[0],
            quantity=Decimal("1"),
            price=price,
            credit=Decimal("150.00"),
            contract=contract,
        )
        for row, price, contract in zip((1, 2), row_prices, row_contracts, strict=True)
    )
    events = tuple(
        _event(
            ordinal=ordinal,
            kind=RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            activity_date=date(2025, 1, 31 if pending else 20),
            settle_date=date(2025, 2, 3) if pending else date(2025, 1, 21),
            instrument=contract[0],
            quantity=Decimal("1"),
            price=price,
            amount=Decimal("150.00"),
            contract=contract,
        )
        for ordinal, price, contract in zip((1, 2), event_prices, event_contracts, strict=True)
    )
    return _bundle(_statement(activities=rows)), events


@pytest.mark.parametrize("pending", [False, True], ids=["activity", "pending"])
def test_honest_same_priced_closes_of_two_strikes_are_one_group(pending: bool) -> None:
    price = Decimal("1.50")
    primary, events = _two_closes(
        pending=pending,
        row_prices=(price, price),
        row_contracts=(_STRIKE_100, _STRIKE_120),
        event_prices=(price, price),
        event_contracts=(_STRIKE_120, _STRIKE_100),
    )

    result = reconcile_robinhood_activity(primary, (), events)

    groups = result.pending_trade_groups if pending else result.activity_groups
    assert [(group.event_ordinals, _group_rows(group)) for group in groups] == [
        ((1, 2), _references((0, 1), (0, 2)))
    ]


def test_contracts_never_settle_a_pending_display_precision_tie() -> None:
    other = ("XQOB", _EXPIRY, "Call", Decimal("100.00"))
    primary, events = _two_closes(
        pending=True,
        row_prices=(Decimal("1.00"), Decimal("1.01")),
        row_contracts=(_STRIKE_100, other),
        event_prices=(Decimal("1.0050"), Decimal("1.0050")),
        event_contracts=(_STRIKE_100, other),
    )

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(primary, (), events)

    assert raised.value.message == "pending trade display-precision matching is ambiguous"


@pytest.mark.parametrize("pending", [False, True], ids=["activity", "pending"])
def test_an_exact_pairing_that_contradicts_contracts_does_not_fall_to_the_band(
    pending: bool,
) -> None:
    """Exact prices pair each close with the row printing the other strike;
    the band would allow the agreeing swap, but the exact pass is accepted."""

    primary, events = _two_closes(
        pending=pending,
        row_prices=(Decimal("1.00"), Decimal("1.01")),
        row_contracts=(_STRIKE_120, _STRIKE_100),
        event_prices=(Decimal("1.00"), Decimal("1.01")),
        event_contracts=(_STRIKE_100, _STRIKE_120),
    )

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(primary, (), events)

    section = _PENDING if pending else _ACCOUNT
    assert raised.value.message == (
        f"{_CONTRACT} ({section}: statement 1 page 2 row 1, statement 1 page 2 row 2;"
        " column Description)"
    )


def test_issued_residuals_precoverage_ordinals_and_statement_cash_guards_refuse() -> None:
    primary, _, events = _baseline()
    with pytest.raises(RobinhoodReconciliationError, match="before statement coverage"):
        reconcile_robinhood_activity(
            primary,
            (),
            (
                _event(
                    ordinal=20,
                    kind=RobinhoodEventKind.ACH_DEPOSIT,
                    activity_date=date(2024, 12, 1),
                    settle_date=date(2024, 12, 1),
                    amount=Decimal("1.00"),
                ),
            ),
        )
    with pytest.raises(RobinhoodReconciliationError, match="signature sets"):
        reconcile_robinhood_activity(
            primary,
            (),
            (
                *events[:-1],
                _event(
                    ordinal=20,
                    kind=RobinhoodEventKind.ACH_DEPOSIT,
                    activity_date=date(2025, 1, 25),
                    settle_date=date(2025, 1, 25),
                    amount=Decimal("1.00"),
                ),
            ),
        )
    with pytest.raises(RobinhoodReconciliationError, match="ordinals"):
        reconcile_robinhood_activity(
            primary,
            (),
            (*events, replace(events[0], source_record_index=1)),
        )

    both_cash = _row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 5),
        debit=Decimal("1.00"),
        credit=Decimal("1.00"),
    )
    with pytest.raises(RobinhoodReconciliationError, match="both debit and credit"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=(both_cash,))),
            (),
            (
                _event(
                    ordinal=1,
                    kind=RobinhoodEventKind.ACH_DEPOSIT,
                    activity_date=date(2025, 1, 5),
                    settle_date=date(2025, 1, 5),
                    amount=Decimal("1.00"),
                ),
            ),
        )

    negative_cash = _row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 5),
        credit=Decimal("-1.00"),
    )
    with pytest.raises(RobinhoodReconciliationError, match="non-negative"):
        reconcile_robinhood_activity(
            _bundle(_statement(activities=(negative_cash,))),
            (),
            (
                _event(
                    ordinal=1,
                    kind=RobinhoodEventKind.ACH_DEPOSIT,
                    activity_date=date(2025, 1, 5),
                    settle_date=date(2025, 1, 5),
                    amount=Decimal("1.00"),
                ),
            ),
        )


def test_straddling_nontrade_requires_supported_pending_evidence() -> None:
    bundle = _bundle(_statement())
    event = _event(
        ordinal=1,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 1),
        amount=Decimal("1.00"),
    )
    with pytest.raises(RobinhoodReconciliationError, match="non-trade"):
        reconcile_robinhood_activity(bundle, (), (event,))

    trade = _event(
        ordinal=2,
        kind=RobinhoodEventKind.EQUITY_BUY,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 3),
        instrument="EQTY",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        amount=Decimal("-10.00"),
    )
    with pytest.raises(RobinhoodReconciliationError, match="lacks pending"):
        reconcile_robinhood_activity(bundle, (), (trade,))


# --- #1190: the counterpart check survives the payload removal -------------

_CLEAN_MARKERS = ("SYNTHCLEANACCOUNT", "SYNTHCLEANNAME", "SYNTHCLEANDESC")
_COUNTERPART_REFUSAL = (
    "Robinhood reconciliation refused: counterpart statement bundles are duplicated or "
    "self-referential"
)


def _marked_bundle(account: str):
    """A bundle of one January ACH deposit whose registration and row
    description are markers."""
    row = _row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 5),
        credit=Decimal("1.00"),
        description="SYNTHCLEANDESC",
    )
    statement = _statement(account=account, activities=(row,))
    return _bundle(replace(statement, registration_lines=("SYNTHCLEANNAME",)))


def _deposit(ordinal: int = 1) -> RobinhoodEvent:
    return _event(
        ordinal=ordinal,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=date(2025, 1, 5),
        settle_date=date(2025, 1, 5),
        amount=Decimal("1.00"),
    )


def test_a_distinct_counterpart_bundle_reconciles() -> None:
    primary = _marked_bundle("SYNTHCLEANACCOUNT")
    result = reconcile_robinhood_activity(primary, (_marked_bundle("222222222"),), (_deposit(),))

    (group,) = result.activity_groups
    assert group.event_ordinals == (1,)
    assert result.primary_bundle is primary


@pytest.mark.parametrize(
    ("counterparts", "events"),
    [
        pytest.param("self", (1,), id="self"),
        pytest.param("duplicate", (1,), id="duplicate"),
        pytest.param("self", (1, 1), id="self-before-duplicate-ordinals"),
        pytest.param("duplicate", (), id="duplicate-before-unmatched-row"),
    ],
)
def test_duplicate_or_self_counterparts_refuse_first_without_a_value(
    counterparts: str,
    events: tuple[int, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The counterpart check runs before any other activity check: a
    repeated event ordinal or a statement row with no event refuses only
    after it. The refusal is the complete fixed message, with no chain and
    no account, registration or row text."""
    primary = _marked_bundle("SYNTHCLEANACCOUNT")
    other = _marked_bundle("222222222")
    bundles = (primary,) if counterparts == "self" else (other, other)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(
            primary, bundles, tuple(_deposit(ordinal) for ordinal in events)
        )

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _COUNTERPART_REFUSAL,
        capsys,
        markers=_CLEAN_MARKERS,
    )


def _activity_covers_its_sources(
    account: RobinhoodAccountReconciliation,
    report_rows: int,
) -> bool:
    """Every report row (``report_rows`` of them, read without the parser)
    lands in exactly one activity group, in-kind group or pending event;
    every Account Activity row of the account's statements in exactly one
    activity group, in-kind group or crypto row; and managed trades bind
    only to a managed account."""
    activity = account.activity
    ordinals = sorted(
        [
            *(ordinal for group in activity.activity_groups for ordinal in group.event_ordinals),
            *(
                ordinal
                for group in activity.in_kind_transfer_groups
                for ordinal in group.event_ordinals
            ),
            *(item.event_ordinal for item in activity.pending_statement_events),
        ]
    )
    references = sorted(
        (reference.statement_index, reference.location.page, reference.location.row)
        for reference in (
            *(ref for group in activity.activity_groups for ref in group.statement_references),
            *(
                ref
                for group in activity.in_kind_transfer_groups
                for ref in group.statement_references
            ),
            *(row.statement_reference for row in activity.crypto_rows),
        )
    )
    statement_rows = sorted(
        (index, row.location.page, row.location.row)
        for index, statement in enumerate(activity.primary_bundle.statements)
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
    )
    managed_trades = any(
        event.kind
        in {RobinhoodEventKind.MANAGED_EQUITY_BUY, RobinhoodEventKind.MANAGED_EQUITY_SELL}
        for event in account.evidence.events
    )
    return (
        ordinals == list(range(1, report_rows + 1))
        and references == statement_rows
        and (
            not managed_trades
            or activity.primary_bundle.family is RobinhoodStatementFamily.MANAGED_INDIVIDUAL
        )
    )


@private_test
def test_private_activity_reconciliation_is_complete_and_redacted() -> None:
    paired = load_private_accounts_reconciliation()
    records = load_private_report_records()
    first, second = paired.accounts
    if not (
        _activity_covers_its_sources(first, len(records[first.report_index]))
        and _activity_covers_its_sources(second, len(records[second.report_index]))
    ):
        pytest.fail("private activity reconciliation retention audit failed", pytrace=False)


# --- #1185: exact statement cash reaches activity matching --------------------

# 29 significant digits: one more than Python's default context precision.
_LONG = "100000000000000000000000000.01"
# The widest Amount the activity CSV reads (#1275): 15 whole digits and cents.
_WIDEST_CSV = "999,999,999,999,999.99"
_ACTIVITY_REFUSED = "Robinhood reconciliation refused: statement activity "
_CASH_MARKERS = ("SYNTHCASHACCOUNT", "SYNTHCASHID", "SYNTHCASHDESC", "SYNTHCASHSOURCE")


def _ach(side: str, amount: Decimal):
    """One statement ACH row with ``amount`` in its ``side`` cell and the
    CSV event printing the same signed amount."""
    row = _row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 5),
        debit=amount if side == "debit" else None,
        credit=amount if side == "credit" else None,
    )
    event = _event(
        ordinal=1,
        kind=(
            RobinhoodEventKind.ACH_DEPOSIT
            if side == "credit"
            else RobinhoodEventKind.ACH_WITHDRAWAL
        ),
        activity_date=date(2025, 1, 5),
        settle_date=date(2025, 1, 5),
        amount=amount if side == "credit" else amount.copy_negate(),
    )
    return row, event


@pytest.mark.parametrize(
    ("value", "precision"),
    [(_LONG, 28), ("1234567.89", 6), ("10.00", 28), ("10.00", 6)],
    ids=["long-prec28", "cents-prec6", "control-prec28", "control-prec6"],
)
@pytest.mark.parametrize("side", ["credit", "debit"])
def test_statement_cash_matches_the_same_exact_activity_amount(
    side: str,
    value: str,
    precision: int,
) -> None:
    """#1185: statement cash is signed without rounding, so a deposit or
    withdrawal whose statement cell and CSV amount print the same digits
    matches at any context precision. Before, only the statement side was
    rounded, a false contradiction once the digits outran the precision."""
    row, event = _ach(side, Decimal(value))
    bundle = _bundle(_statement(activities=(row,)))
    with localcontext(Context(prec=precision)):
        result = reconcile_robinhood_activity(bundle, (), (event,))

    (group,) = result.activity_groups
    assert group.event_ordinals == (1,)
    assert group.statement_references == (RobinhoodStatementReference(0, row.location),)


@pytest.mark.parametrize(
    ("printed", "precision"),
    [(_WIDEST_CSV, 28), (_WIDEST_CSV, 16), ("1,234,567.89", 6)],
    ids=["prec28", "prec16", "prec6"],
)
def test_csv_withdrawal_and_statement_debit_keep_the_same_digits(
    printed: str, precision: int
) -> None:
    """#1185: the CSV's parenthesised amount is negated without rounding too,
    so a withdrawal printing more digits than the context holds still matches
    the statement debit printing the same digits.

    The CSV reader takes at most 15 whole digits and cents (#1275), so no CSV
    amount outruns the default 28-digit context: there the widest amount it
    reads matches. Under 16 digits that widest amount outruns the context.
    """
    with localcontext(Context(prec=precision)):
        (event,) = parse_robinhood_activity(
            _csv(
                (
                    (
                        "1/5/2025",
                        "1/5/2025",
                        "1/5/2025",
                        "",
                        "ACH Withdrawal",
                        "ACH",
                        "",
                        "",
                        f"(${printed})",
                    ),
                )
            )
        )
        row, _ = _ach("debit", Decimal(printed.replace(",", "")))
        result = reconcile_robinhood_activity(_bundle(_statement(activities=(row,))), (), (event,))

    (group,) = result.activity_groups
    assert group.event_ordinals == (1,)


@pytest.mark.parametrize(
    ("side", "statement", "csv_amount"),
    [
        # 15 whole digits and 14 places: rounds down to the CSV amount.
        ("credit", "100000000000000.00000000000001", "$100,000,000,000,000.00"),
        # 14 whole digits and 15 places: rounds up to it.
        ("debit", "99999999999999.999999999999999", "($100,000,000,000,000.00)"),
    ],
    ids=["credit", "debit"],
)
def test_amounts_differing_past_digit_28_no_longer_match(
    side: str,
    statement: str,
    csv_amount: str,
) -> None:
    """#1185, a fail-closed consequence of exact signing: a statement cell
    and a CSV amount that differ only past digit 28 rounded to the same
    value on main, which matched them. Their exact digits disagree, so
    activity matching now refuses them.

    Both are within their readers' limits (#1275): the CSV amount has the 15
    whole digits and cents the CSV allows, and the statement cell's 29 digits
    fit the statement's 15 whole digits and 20 places.
    """
    (event,) = parse_robinhood_activity(
        _csv(
            (
                (
                    "1/5/2025",
                    "1/5/2025",
                    "1/5/2025",
                    "",
                    "ACH Deposit" if side == "credit" else "ACH Withdrawal",
                    "ACH",
                    "",
                    "",
                    csv_amount,
                ),
            )
        )
    )
    row, _ = _ach(side, Decimal(statement))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(_bundle(_statement(activities=(row,))), (), (event,))
    assert str(raised.value) == (
        "Robinhood reconciliation refused: issued activity signature sets disagree"
    )


@pytest.mark.parametrize(
    ("debit", "credit", "message"),
    [
        ("-1.00", None, "debit/credit must be non-negative"),
        ("-1.00", "1.00", "debit/credit must be non-negative"),
        ("0", "-1.00", "debit/credit must be non-negative"),
        ("1.00", "1.00", "row populates both debit and credit"),
        ("0.00", "0", "row populates both debit and credit"),
    ],
)
def test_activity_matching_retains_each_signing_refusal(
    debit: str,
    credit: str | None,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185: a negative cell refuses before a row populating both, with the
    complete fixed message and no row value, description or account."""
    row = replace(
        _row(row=1, code="ACH", transaction_date=date(2025, 1, 5)),
        description="SYNTHCASHDESC",
        symbol="SYNTHCASHID",
        account_type="SYNTHCASHACCOUNT",
        source_account_reference="SYNTHCASHSOURCE",
        debit_usd=Decimal(debit),
        credit_usd=None if credit is None else Decimal(credit),
    )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_activity(
            _bundle(_statement(activities=(row,))),
            (),
            (
                _event(
                    ordinal=1,
                    kind=RobinhoodEventKind.ACH_DEPOSIT,
                    activity_date=date(2025, 1, 5),
                    settle_date=date(2025, 1, 5),
                    amount=Decimal("1.00"),
                ),
            ),
        )
    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _ACTIVITY_REFUSED + message,
        capsys,
        markers=_CASH_MARKERS,
    )
