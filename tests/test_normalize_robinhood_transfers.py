"""Reciprocal Robinhood brokerage-transfer reconciliation checks."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, localcontext

import pytest

from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityGroup,
    RobinhoodActivityReconciliation,
    RobinhoodCashReconciliation,
    RobinhoodInKindTransferGroup,
    RobinhoodReconciliationError,
    RobinhoodStatementBundle,
    RobinhoodStatementReference,
    RobinhoodTransferDirection,
    RobinhoodTransferReconciliation,
    reconcile_robinhood_transfers,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import (
    RobinhoodEventKind,
)
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementSection,
)
from tests._crash_reports import assert_fixed_refusal
from tests._private_checks import private_test
from tests._repr_text import repr_without_addresses
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
)
from tests._robinhood_refusals import ACTIVITY_BOTH_REFUSAL, ACTIVITY_NEGATIVE_REFUSAL
from tests._robinhood_statements import (
    robinhood_account_summary,
    robinhood_activity,
    robinhood_statement,
)

_START = date(2025, 1, 1)
_END = date(2025, 1, 31)


def _row(
    *,
    row: int,
    transaction_date: date = date(2025, 1, 10),
    code: str = "ITRF",
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
    symbol: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    settle_date: date | None = None,
) -> RobinhoodStatementActivity:
    return robinhood_activity(
        row=row,
        transaction_code=code,
        transaction_date=transaction_date,
        description="Synthetic transfer",
        symbol=symbol,
        settle_date=settle_date,
        quantity=quantity,
        price_usd=price,
        debit_usd=debit,
        credit_usd=credit,
        source_account_reference=source,
        destination_account_reference=destination,
    )


def _statement(
    *,
    account: str,
    family: RobinhoodStatementFamily,
    activities: tuple[RobinhoodStatementActivity, ...],
    start: date = _START,
    end: date = _END,
) -> RobinhoodStatement:
    return robinhood_statement(
        family=family,
        period_start=start,
        period_end=end,
        account_number=account,
        account_summary=robinhood_account_summary(
            opening=(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
        ),
        activities=activities,
    )


def _baseline():
    primary_account = "111111111"
    counterpart_account = "222222222"
    transfer_date = date(2025, 1, 10)
    primary_rows = (
        _row(row=1, transaction_date=transfer_date, debit=Decimal("10.00")),
        _row(row=2, transaction_date=transfer_date, credit=Decimal("5.00")),
        _row(
            row=3,
            transaction_date=transfer_date,
            quantity=Decimal("1"),
            symbol="ONE",
            source=primary_account,
            destination=counterpart_account,
        ),
        _row(
            row=4,
            transaction_date=transfer_date,
            quantity=Decimal("2"),
            symbol="XQGA",
            source=counterpart_account,
            destination=primary_account,
        ),
    )
    counterpart_rows = (
        _row(row=1, transaction_date=transfer_date, credit=Decimal("10.00")),
        _row(row=2, transaction_date=transfer_date, debit=Decimal("5.00")),
        _row(
            row=3,
            transaction_date=transfer_date,
            quantity=Decimal("1.0"),
            symbol="ONE",
            source=primary_account,
            destination=counterpart_account,
        ),
        _row(
            row=4,
            transaction_date=transfer_date,
            quantity=Decimal("2.00"),
            symbol="XQGA",
            source=counterpart_account,
            destination=primary_account,
        ),
    )
    primary_bundle, counterpart_bundle = validate_robinhood_statement_bundles(
        (
            _statement(
                account=primary_account,
                family=RobinhoodStatementFamily.INDIVIDUAL,
                activities=primary_rows,
            ),
            _statement(
                account=counterpart_account,
                family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                activities=counterpart_rows,
            ),
        )
    )
    primary_refs = tuple(RobinhoodStatementReference(0, row.location) for row in primary_rows)
    activity = RobinhoodActivityReconciliation(
        primary_bundle=primary_bundle,
        activity_groups=(
            RobinhoodActivityGroup(
                transaction_code="ITRF",
                event_kinds=(RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,),
                event_ordinals=(1,),
                statement_references=(primary_refs[0],),
            ),
            RobinhoodActivityGroup(
                transaction_code="ITRF",
                event_kinds=(RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,),
                event_ordinals=(2,),
                statement_references=(primary_refs[1],),
            ),
        ),
        in_kind_transfer_groups=(
            RobinhoodInKindTransferGroup(
                event_ordinals=(3,),
                statement_references=(primary_refs[2],),
            ),
            RobinhoodInKindTransferGroup(
                event_ordinals=(4,),
                statement_references=(primary_refs[3],),
            ),
        ),
        pending_statement_events=(),
        pending_trade_groups=(),
        crypto_rows=(),
    )
    cash = RobinhoodCashReconciliation(
        activity_reconciliation=activity,
        periods=(),
    )
    return cash, counterpart_bundle


def test_reconciles_same_date_bidirectional_cash_and_security_groups() -> None:
    cash, counterpart = _baseline()
    result = reconcile_robinhood_transfers(
        cash,
        counterpart,
        same_beneficial_owner=True,
    )

    assert [pair.event_ordinal for pair in result.cash_pairs] == [1, 2]
    assert {pair.direction for pair in result.cash_pairs} == {
        RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART,
        RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY,
    }
    assert {group.event_ordinals for group in result.security_groups} == {
        (3,),
        (4,),
    }
    assert {group.direction for group in result.security_groups} == {
        RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART,
        RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY,
    }
    assert repr(result) == object.__repr__(result)
    assert "111111111" not in repr_without_addresses(result)


def test_same_owner_and_distinct_account_are_required() -> None:
    cash, counterpart = _baseline()
    with pytest.raises(RobinhoodReconciliationError, match="beneficial ownership"):
        reconcile_robinhood_transfers(
            cash,
            counterpart,
            same_beneficial_owner=False,
        )
    with pytest.raises(RobinhoodReconciliationError, match="must differ"):
        reconcile_robinhood_transfers(
            cash,
            cash.activity_reconciliation.primary_bundle,
            same_beneficial_owner=True,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda rows: rows[1:],
            "absent or non-unique",
        ),
        (
            lambda rows: (
                *rows,
                replace(rows[0], location=replace(rows[0].location, row=9)),
            ),
            "absent or non-unique",
        ),
        (
            lambda rows: (
                replace(rows[0], credit_usd=None, debit_usd=Decimal("10.00")),
                *rows[1:],
            ),
            "absent or non-unique",
        ),
    ],
    ids=["missing-counterpart", "duplicate-counterpart", "same-signed"],
)
def test_cash_counterpart_must_be_unique_and_opposite(
    mutate,
    message: str,
) -> None:
    cash, counterpart = _baseline()
    statement = counterpart.statements[0]
    changed = replace(statement, activities=mutate(statement.activities))
    changed_bundle = replace(counterpart, statements=(changed,))
    with pytest.raises(RobinhoodReconciliationError, match=message):
        reconcile_robinhood_transfers(
            cash,
            changed_bundle,
            same_beneficial_owner=True,
        )


def test_coverage_refuses_before_missing_counterpart() -> None:
    cash, counterpart = _baseline()
    statement = counterpart.statements[0]
    later = replace(
        statement,
        period_start=date(2025, 1, 11),
        activities=statement.activities[2:],
    )
    later_bundle = replace(
        counterpart,
        coverage_start=date(2025, 1, 11),
        statements=(later,),
    )
    with pytest.raises(RobinhoodReconciliationError, match="outside shared"):
        reconcile_robinhood_transfers(
            cash,
            later_bundle,
            same_beneficial_owner=True,
        )


@pytest.mark.parametrize(
    "changed_row",
    [
        _row(
            row=3,
            quantity=Decimal("1"),
            symbol="ONE",
            source=None,
            destination="222222222",
        ),
        _row(
            row=3,
            quantity=Decimal("1"),
            symbol="ONE",
            source="111111111",
            destination="333333333",
        ),
        _row(
            row=3,
            quantity=Decimal("1"),
            symbol="ONE",
            source="111111111",
            destination="111111111",
        ),
        _row(
            row=3,
            quantity=Decimal("1"),
            price=Decimal("1"),
            symbol="ONE",
            source="111111111",
            destination="222222222",
        ),
    ],
    ids=["missing-ref", "external-ref", "self-ref", "price-populated"],
)
def test_security_transfer_shape_and_account_references_refuse(
    changed_row: RobinhoodStatementActivity,
) -> None:
    cash, counterpart = _baseline()
    primary = cash.activity_reconciliation.primary_bundle
    statement = primary.statements[0]
    changed = replace(
        statement,
        activities=(
            statement.activities[0],
            statement.activities[1],
            changed_row,
            statement.activities[3],
        ),
    )
    changed_cash = replace(
        cash,
        activity_reconciliation=replace(
            cash.activity_reconciliation,
            primary_bundle=replace(primary, statements=(changed,)),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError):
        reconcile_robinhood_transfers(
            changed_cash,
            counterpart,
            same_beneficial_owner=True,
        )


def test_security_counts_keys_and_aggregate_band_refuse() -> None:
    cash, counterpart = _baseline()
    statement = counterpart.statements[0]

    missing = replace(statement, activities=statement.activities[:-1])
    with pytest.raises(RobinhoodReconciliationError, match="row counts"):
        reconcile_robinhood_transfers(
            cash,
            replace(counterpart, statements=(missing,)),
            same_beneficial_owner=True,
        )

    wrong_key = replace(
        statement,
        activities=(
            *statement.activities[:2],
            replace(statement.activities[2], symbol="OTHER"),
            statement.activities[3],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="row counts"):
        reconcile_robinhood_transfers(
            cash,
            replace(counterpart, statements=(wrong_key,)),
            same_beneficial_owner=True,
        )

    wrong_quantity = replace(
        statement,
        activities=(
            *statement.activities[:2],
            replace(statement.activities[2], quantity=Decimal("1.6")),
            statement.activities[3],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="aggregate quantities"):
        reconcile_robinhood_transfers(
            cash,
            replace(counterpart, statements=(wrong_quantity,)),
            same_beneficial_owner=True,
        )


def test_in_kind_group_refset_bijection_is_required() -> None:
    cash, counterpart = _baseline()
    in_kind = cash.activity_reconciliation.in_kind_transfer_groups
    merged = replace(
        cash.activity_reconciliation,
        in_kind_transfer_groups=(
            RobinhoodInKindTransferGroup(
                event_ordinals=(3, 4),
                statement_references=(
                    *in_kind[0].statement_references,
                    *in_kind[1].statement_references,
                ),
            ),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="does not map"):
        reconcile_robinhood_transfers(
            replace(cash, activity_reconciliation=merged),
            counterpart,
            same_beneficial_owner=True,
        )

    duplicated = replace(
        cash.activity_reconciliation,
        in_kind_transfer_groups=(
            in_kind[0],
            in_kind[0],
            in_kind[1],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="duplicated"):
        reconcile_robinhood_transfers(
            replace(cash, activity_reconciliation=duplicated),
            counterpart,
            same_beneficial_owner=True,
        )


def test_external_cash_and_rec_rows_are_not_transfer_candidates() -> None:
    cash, counterpart = _baseline()
    primary = cash.activity_reconciliation.primary_bundle
    primary_statement = primary.statements[0]
    extra_primary = replace(
        primary_statement,
        activities=(
            *primary_statement.activities,
            _row(
                row=10,
                code="ACH",
                credit=Decimal("1"),
            ),
            _row(
                row=11,
                code="REC",
                quantity=Decimal("1"),
            ),
        ),
    )
    changed_cash = replace(
        cash,
        activity_reconciliation=replace(
            cash.activity_reconciliation,
            primary_bundle=replace(primary, statements=(extra_primary,)),
        ),
    )
    result = reconcile_robinhood_transfers(
        changed_cash,
        counterpart,
        same_beneficial_owner=True,
    )
    assert result.cash_pairs
    assert result.security_groups


def test_duplicate_counterpart_reference_refuses() -> None:
    cash, counterpart = _baseline()
    statement = counterpart.statements[0]
    duplicated = replace(
        statement,
        activities=(
            *statement.activities,
            statement.activities[0],
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="reference is duplicated"):
        reconcile_robinhood_transfers(
            cash,
            replace(counterpart, statements=(duplicated,)),
            same_beneficial_owner=True,
        )


def _itrf_rows(
    bundle: RobinhoodStatementBundle,
) -> dict[RobinhoodStatementReference, RobinhoodStatementActivity]:
    return {
        RobinhoodStatementReference(index, row.location): row
        for index, statement in enumerate(bundle.statements)
        for row in statement.activities
        if row.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
        and row.transaction_code == "ITRF"
    }


def _signed_cash(row: RobinhoodStatementActivity) -> Decimal | None:
    if row.debit_usd is None and row.credit_usd is None:
        return None
    return (row.credit_usd or Decimal()) - (row.debit_usd or Decimal())


def _half_display_unit(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return Decimal(1).scaleb(exponent) / 2


def _transfers_reconcile_with_statements(result: RobinhoodTransferReconciliation) -> bool:
    """Every ITRF row in either account's statements is used exactly once,
    by a cash pair or a security group. A cash pair's rows move one amount
    in opposite directions on one date, its direction following the primary
    row's sign. A security group's rows share one date and symbol on both
    sides, its direction names the source account the rows print, and the
    two sides' quantities agree within their printed precision."""
    primary_bundle = result.cash_reconciliation.activity_reconciliation.primary_bundle
    primary = _itrf_rows(primary_bundle)
    counterpart = _itrf_rows(result.counterpart_bundle)
    used_primary = Counter(
        [
            *(pair.primary_reference for pair in result.cash_pairs),
            *(ref for group in result.security_groups for ref in group.primary_references),
        ]
    )
    used_counterpart = Counter(
        [
            *(pair.counterpart_reference for pair in result.cash_pairs),
            *(ref for group in result.security_groups for ref in group.counterpart_references),
        ]
    )
    if used_primary != Counter(primary.keys()) or used_counterpart != Counter(counterpart.keys()):
        return False
    for pair in result.cash_pairs:
        primary_row = primary[pair.primary_reference]
        counterpart_row = counterpart[pair.counterpart_reference]
        amount = _signed_cash(primary_row)
        expected_direction = (
            RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY
            if amount is not None and amount > 0
            else RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART
        )
        if (
            amount is None
            or _signed_cash(counterpart_row) != -amount
            or primary_row.transaction_date != counterpart_row.transaction_date
            or pair.direction is not expected_direction
        ):
            return False
    for group in result.security_groups:
        rows = [
            *(primary[ref] for ref in group.primary_references),
            *(counterpart[ref] for ref in group.counterpart_references),
        ]
        quantities = [row.quantity for row in rows]
        if any(quantity is None for quantity in quantities) or not group.primary_references:
            return False
        known = [quantity for quantity in quantities if quantity is not None]
        primary_sum = sum(known[: len(group.primary_references)], Decimal())
        counterpart_sum = sum(known[len(group.primary_references) :], Decimal())
        expected_direction = (
            RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART
            if rows[0].source_account_reference == primary_bundle.account_number
            else RobinhoodTransferDirection.COUNTERPART_TO_PRIMARY
        )
        if (
            len({(row.transaction_date, row.symbol, row.source_account_reference) for row in rows})
            != 1
            or group.direction is not expected_direction
            or abs(primary_sum - counterpart_sum)
            > max(_half_display_unit(quantity) for quantity in known)
        ):
            return False
    return True


@private_test
def test_private_reciprocal_transfers_reconcile_without_leaking_source_facts() -> None:
    result = load_private_accounts_reconciliation().transfers
    if not _transfers_reconcile_with_statements(result):
        pytest.fail("private reciprocal transfer retention audit failed", pytrace=False)


# --- #1185: exact cash signs pair transfers ----------------------------------

# 29 significant digits: one more than Python's default context precision.
_LONG = "100000000000000000000000000.01"
_REFUSED = "Robinhood reconciliation refused: "
_CASH_MARKERS = ("SYNTHCASHACCOUNT", "SYNTHCASHID", "SYNTHCASHDESC", "SYNTHCASHSOURCE")


def _with_first_cash_rows(
    primary_cells: dict[str, object],
    counterpart_cells: dict[str, object],
) -> tuple[RobinhoodCashReconciliation, RobinhoodStatementBundle]:
    """:func:`_baseline` with fields of each account's first (cash ITRF) row
    replaced."""
    cash, counterpart = _baseline()
    activity = cash.activity_reconciliation
    bundle = activity.primary_bundle

    def changed(statement_bundle: RobinhoodStatementBundle, cells: dict[str, object]):
        (statement,) = statement_bundle.statements
        rows = (replace(statement.activities[0], **cells), *statement.activities[1:])
        return replace(statement_bundle, statements=(replace(statement, activities=rows),))

    return (
        replace(
            cash,
            activity_reconciliation=replace(
                activity, primary_bundle=changed(bundle, primary_cells)
            ),
        ),
        changed(counterpart, counterpart_cells),
    )


@pytest.mark.parametrize(
    ("value", "precision"),
    [(_LONG, 28), ("1234567.89", 6), ("10.00", 28), ("10.00", 6)],
    ids=["long-prec28", "cents-prec6", "control-prec28", "control-prec6"],
)
def test_cash_transfer_counterpart_is_the_exact_opposite_amount(
    value: str,
    precision: int,
) -> None:
    """#1185: each cash transfer's counterpart is looked up by negating its
    exact signed amount without rounding, so a transfer printing more digits
    than the context holds still pairs with its counterpart row."""
    amount = Decimal(value)
    cash, counterpart = _with_first_cash_rows(
        {"debit_usd": amount},
        {"credit_usd": amount},
    )
    with localcontext(Context(prec=precision)):
        result = reconcile_robinhood_transfers(cash, counterpart, same_beneficial_owner=True)

    assert [pair.event_ordinal for pair in result.cash_pairs] == [1, 2]
    assert result.cash_pairs[0].direction is RobinhoodTransferDirection.PRIMARY_TO_COUNTERPART


def test_transfers_differing_past_digit_28_no_longer_pair() -> None:
    """#1185, a fail-closed consequence of exact signing: a debit and a
    counterpart credit that differ only past digit 28 rounded to the same
    value on main, which paired them. Their exact digits disagree, so the
    counterpart is now absent."""
    cash, counterpart = _with_first_cash_rows(
        {"debit_usd": Decimal(_LONG)},
        {"credit_usd": Decimal("100000000000000000000000000.02")},
    )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_transfers(cash, counterpart, same_beneficial_owner=True)
    assert str(raised.value) == _REFUSED + "cash transfer counterpart is absent or non-unique"


@pytest.mark.parametrize("zero", ["0", "0.00", "-0", "-0.000"])
@pytest.mark.parametrize("side", ["debit_usd", "credit_usd"])
def test_zero_cash_transfer_row_still_has_an_unsupported_shape(zero: str, side: str) -> None:
    """#1185: a zero cash ITRF row of any sign or scale still refuses."""
    cells: dict[str, object] = {"debit_usd": None, "credit_usd": None}
    cells[side] = Decimal(zero)
    cash, counterpart = _with_first_cash_rows({}, cells)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_transfers(cash, counterpart, same_beneficial_owner=True)
    assert str(raised.value) == _REFUSED + "cash ITRF row has an unsupported shape"


@pytest.mark.parametrize(
    ("debit", "credit", "message"),
    [
        ("-10.00", None, ACTIVITY_NEGATIVE_REFUSAL),
        ("-10.00", "10.00", ACTIVITY_NEGATIVE_REFUSAL),
        ("10.00", "10.00", ACTIVITY_BOTH_REFUSAL),
        ("0", "0.00", ACTIVITY_BOTH_REFUSAL),
    ],
)
def test_counterpart_transfer_rows_retain_each_signing_refusal(
    debit: str,
    credit: str | None,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """#1185: the counterpart's cash rows reach the fixed signing refusals,
    negative first, with no row value, description or account."""
    cash, counterpart = _with_first_cash_rows(
        {},
        {
            "description": "SYNTHCASHDESC",
            "symbol": "SYNTHCASHID",
            "account_type": "SYNTHCASHACCOUNT",
            "source_account_reference": "SYNTHCASHSOURCE",
            "debit_usd": Decimal(debit),
            "credit_usd": None if credit is None else Decimal(credit),
        },
    )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_transfers(cash, counterpart, same_beneficial_owner=True)
    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _REFUSED + message,
        capsys,
        markers=_CASH_MARKERS,
    )
