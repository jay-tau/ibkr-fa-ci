"""Two-account Robinhood report assignment and reconciliation checks."""

from __future__ import annotations

import re
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any, cast

import pytest

import ibkr_fa.normalize.robinhood.accounts as accounts_mod
import ibkr_fa.normalize.robinhood.inventory as inventory_mod
import ibkr_fa.pipeline_robinhood_core as core
from ibkr_fa.compute.robinhood_identity import observed_robinhood_identity_inputs
from ibkr_fa.compute.robinhood_lots import (
    reconcile_robinhood_security_lots,
)
from ibkr_fa.config import (
    RobinhoodSecurityConfig,
    RobinhoodSecurityReceiptResolution,
    RobinhoodSecurityReceiptTreatment,
    _parse_robinhood_security,
    _parse_robinhood_security_receipt_resolution,
    load_user_config,
)
from ibkr_fa.exceptions import ConfigError
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodInventoryBlockerKind,
    RobinhoodInventoryUnprovedFact,
    RobinhoodPositionReference,
    RobinhoodPositionSnapshotBasis,
    RobinhoodQuantityMovementKind,
    RobinhoodReconciliationError,
    RobinhoodSecurityEventReference,
    RobinhoodSecurityIdentityBlockerKind,
    RobinhoodSecurityQuantityResult,
    reconcile_robinhood_accounts,
    reconcile_robinhood_activity,
    reconcile_robinhood_cash,
    reconcile_robinhood_security_quantities,
    render_robinhood_security_toml,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.identity_exceptions import (
    RobinhoodCusipTransition,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    SourceLocation,
)
from ibkr_fa.pipeline_robinhood_bundle import resolved_robinhood_quantities
from ibkr_fa.pipeline_robinhood_outputs import IDENTITY_GUIDANCE_FILENAME, IdentityGuidanceFile
from tests._crash_reports import assert_fixed_refusal
from tests._private_checks import private_test
from tests._repr_text import repr_without_addresses
from tests._robinhood_account_inputs import ACCOUNT_OPENING as _OPENING
from tests._robinhood_account_inputs import CANCELLING_PAIR as _PAIR
from tests._robinhood_account_inputs import HELD_SECTION as _HELD
from tests._robinhood_account_inputs import NEG_ID as _NEG_ID
from tests._robinhood_account_inputs import NEG_MARKERS as _NEG_MARKERS
from tests._robinhood_account_inputs import ONE_ROW as _ONE
from tests._robinhood_account_inputs import PERIOD_END as _END
from tests._robinhood_account_inputs import ZQ_MARKERS as _ZQ_MARKERS
from tests._robinhood_account_inputs import ZQ_NEW as _ZQ_NEW
from tests._robinhood_account_inputs import ZQ_OLD as _ZQ_OLD
from tests._robinhood_account_inputs import account_config as _account
from tests._robinhood_account_inputs import account_statement as _statement
from tests._robinhood_account_inputs import accounts_pair as _pair
from tests._robinhood_account_inputs import cash_event as _cash_event
from tests._robinhood_account_inputs import checked_cusip as _checked_cusip
from tests._robinhood_account_inputs import managed_buy_event as _managed_event
from tests._robinhood_account_inputs import negative_identities as _negative_identities
from tests._robinhood_account_inputs import negative_position as _negative_position
from tests._robinhood_account_inputs import negative_rows_pair as _negative_rows_pair
from tests._robinhood_account_inputs import pair_with_security as _pair_with_security
from tests._robinhood_account_inputs import pair_with_zq_symbols as _pair_with_zq_symbols
from tests._robinhood_account_inputs import receipt_bundle as _receipt_bundle
from tests._robinhood_account_inputs import security_config as _security_config
from tests._robinhood_account_inputs import statement_bundle as _bundle
from tests._robinhood_account_inputs import statement_position as _position
from tests._robinhood_account_inputs import statement_row as _row
from tests._robinhood_account_inputs import statement_summary as _summary
from tests._robinhood_account_inputs import with_managed_statement as _with_managed_statement
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
    load_private_report_records,
    report_cusip,
)
from tests._robinhood_refusals import (
    CUSIP_CONTRADICTION,
    NEGATIVE_POSITION_REFUSAL,
    REC_CONTRADICTION,
    TRANSFER_CONTRADICTION,
)
from tests._robinhood_security import identity_guidance_line
from tests._robinhood_toml import robinhood_security_receipt_resolution_toml, robinhood_toml, write


def _pair_with_security_transfer(
    *,
    source_account_index: int,
    source_quantity: Decimal = Decimal("2.0000"),
    destination_quantity: Decimal = Decimal("2.00004"),
    account_numbers: tuple[str, str] = ("111111111", "222222222"),
    symbol: str = "ABCD",
    issuer: str = "Synthetic",
):
    """Two accounts where one buys ``source_quantity`` at $10 and moves it in
    kind to the other, whose row displays ``destination_quantity``. Returns
    the paired accounts and their resolved identities."""
    transfer_date = date(2025, 1, 12)
    families = (
        RobinhoodStatementFamily.INDIVIDUAL,
        RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
    )
    destination_index = 1 - source_account_index
    source_kind = (
        RobinhoodEventKind.EQUITY_BUY
        if source_account_index == 0
        else RobinhoodEventKind.MANAGED_EQUITY_BUY
    )
    source_buy = RobinhoodEvent(
        kind=source_kind,
        source_record_index=1,
        activity_date=date(2025, 1, 10),
        process_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument=symbol,
        description=f"{issuer} buy\nCUSIP: 99971T103",
        trans_code="Buy",
        quantity=source_quantity,
        quantity_suffix=None,
        price_usd=Decimal("10"),
        amount_usd=-source_quantity * Decimal("10"),
        issuer_name=f"{issuer} buy",
        cusip="99971T103",
    )
    report_events: list[list[RobinhoodEvent]] = [[], []]
    statement_rows: list[list[RobinhoodStatementActivity]] = [[], []]
    report_events[source_account_index].append(source_buy)
    statement_rows[source_account_index].append(
        _row(
            row=1,
            code="Buy",
            transaction_date=source_buy.activity_date,
            symbol=symbol,
            cusip="99971T103",
            quantity=source_quantity,
            price=Decimal("10"),
            debit=source_quantity * Decimal("10"),
        )
    )
    for account_index, quantity in enumerate((source_quantity, destination_quantity)):
        transfer = RobinhoodEvent(
            kind=RobinhoodEventKind.IN_KIND_SECURITY_TRANSFER,
            source_record_index=len(report_events[account_index]) + 1,
            activity_date=transfer_date,
            process_date=transfer_date,
            settle_date=date(2025, 1, 13),
            instrument=symbol,
            description=f"{issuer} transfer\nCUSIP: 99971T103",
            trans_code="ITRF",
            quantity=quantity,
            quantity_suffix="S" if account_index == 0 else None,
            price_usd=None,
            amount_usd=None,
            issuer_name=f"{issuer} transfer",
            cusip="99971T103",
        )
        report_events[account_index].append(transfer)
        statement_rows[account_index].append(
            replace(
                _row(
                    row=2,
                    code="ITRF",
                    transaction_date=transfer_date,
                    symbol=symbol,
                    cusip="99971T103",
                    quantity=quantity,
                ),
                source_account_reference=account_numbers[source_account_index],
                destination_account_reference=account_numbers[destination_index],
            )
        )

    bundles = []
    reports = []
    for account_index in range(2):
        position_quantity = (
            destination_quantity if account_index == destination_index else Decimal()
        )
        positions = (
            (
                _position(
                    row=1,
                    symbol=symbol,
                    quantity=position_quantity,
                ),
            )
            if position_quantity
            else ()
        )
        statement = _statement(
            account=account_numbers[account_index],
            family=families[account_index],
            activities=tuple(statement_rows[account_index]),
            positions=positions,
            closing=(
                Decimal(),
                Decimal(),
                position_quantity * Decimal("10"),
                position_quantity * Decimal("10"),
            ),
        )
        bundles.append(_bundle(statement))
        reports.append(
            RobinhoodActivityReportEvidence(
                events=tuple(report_events[account_index]),
                start_date=_OPENING,
                end_date=_END,
            )
        )
    accounts = tuple(_account(number) for number in account_numbers)
    paired = reconcile_robinhood_accounts(bundles, reports, accounts)
    return paired, resolve_robinhood_security_identities(paired, (_security_config(symbol=symbol),))


def test_assigns_reversed_reports_and_runs_each_layer_once() -> None:
    bundles, reports, accounts = _pair()
    result = reconcile_robinhood_accounts(
        bundles,
        tuple(reversed(reports)),
        tuple(reversed(accounts)),
    )

    assert tuple(account.report_index for account in result.accounts) == (1, 0)
    assert result.accounts[0].evidence is reports[0]
    assert result.accounts[1].evidence is reports[1]
    assert result.accounts[0].source_bundle is bundles[0]
    assert result.accounts[1].source_bundle is bundles[1]
    assert result.account_labels == {0: "Individual", 1: "Managed Individual"}
    assert result.accounts[0].cash.activity_reconciliation is result.accounts[0].activity
    assert result.accounts[1].cash.activity_reconciliation is result.accounts[1].activity
    assert len(result.transfers.cash_pairs) == 1
    assert result.transfers.counterpart_bundle is result.accounts[1].activity.primary_bundle
    rendered = (
        repr_without_addresses(result)
        + repr_without_addresses(result.accounts[0])
        + repr_without_addresses(result.accounts[0].evidence)
    )
    assert "111111111" not in rendered
    assert "222222222" not in rendered


# Each _pair() cash magnitude printed with more digits than the context holds.
_PRECISE_CASH = {
    "long-prec28": (
        28,
        {
            Decimal("10"): Decimal("100000000000000000000000000.01"),
            Decimal("5"): Decimal("500000000000000000000000000.03"),
            Decimal("1"): Decimal("300000000000000000000000000.07"),
        },
    ),
    "cents-prec6": (
        6,
        {
            Decimal("10"): Decimal("1234567.89"),
            Decimal("5"): Decimal("7654321.01"),
            Decimal("1"): Decimal("1000000.07"),
        },
    ),
}


@pytest.mark.parametrize("case", sorted(_PRECISE_CASH))
def test_paired_reconciliation_keeps_every_digit_of_statement_cash(case: str) -> None:
    """#1185: statement cash is signed and negated without rounding, so a
    deposit, fee and inter-account transfer printing the same digits in the
    statements and the activity reports match, reconcile and pair at any
    context precision. Before, rounding only the statement side refused
    every report assignment."""
    precision, magnitudes = _PRECISE_CASH[case]

    def precise(value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        magnitude = magnitudes[value.copy_abs()]
        return magnitude.copy_negate() if value < 0 else magnitude

    bundles, reports, accounts = _pair()
    bundles = tuple(
        replace(
            bundle,
            statements=tuple(
                replace(
                    statement,
                    activities=tuple(
                        replace(
                            row,
                            debit_usd=precise(row.debit_usd),
                            credit_usd=precise(row.credit_usd),
                        )
                        for row in statement.activities
                    ),
                )
                for statement in bundle.statements
            ),
        )
        for bundle in bundles
    )
    reports = tuple(
        replace(
            report,
            events=tuple(
                replace(event, amount_usd=precise(event.amount_usd)) for event in report.events
            ),
        )
        for report in reports
    )
    with localcontext(Context(prec=precision)):
        result = reconcile_robinhood_accounts(bundles, reports, accounts)

    assert tuple(account.report_index for account in result.accounts) == (0, 1)
    assert [group.event_ordinals for group in result.accounts[0].activity.activity_groups] == [
        (1,),
        (2,),
    ]
    assert len(result.transfers.cash_pairs) == 1


@pytest.mark.parametrize("which", ["bundles", "reports", "accounts"])
def test_requires_exactly_two_inputs(which: str) -> None:
    bundles, reports, accounts = _pair()
    values = {
        "bundles": (bundles[:1], reports, accounts),
        "reports": (bundles, reports[:1], accounts),
        "accounts": (bundles, reports, accounts[:1]),
    }[which]
    with pytest.raises(RobinhoodReconciliationError, match="exactly two"):
        reconcile_robinhood_accounts(*values)


def test_rejects_duplicate_and_mismatched_account_sets() -> None:
    bundles, reports, accounts = _pair()
    with pytest.raises(RobinhoodReconciliationError, match="statement accounts are duplicated"):
        reconcile_robinhood_accounts((bundles[0], bundles[0]), reports, accounts)
    with pytest.raises(RobinhoodReconciliationError, match="configured accounts are duplicated"):
        reconcile_robinhood_accounts(bundles, reports, (accounts[0], accounts[0]))
    wrong = replace(accounts[1], number="333333333")
    with pytest.raises(RobinhoodReconciliationError, match="account identities disagree"):
        reconcile_robinhood_accounts(bundles, reports, (accounts[0], wrong))


def test_rejects_ambiguous_and_absent_assignments() -> None:
    bundles, reports, accounts = _pair()
    duplicate_statement = replace(
        bundles[0].statements[0],
        account_number="222222222",
    )
    ambiguous_bundles = (bundles[0], _bundle(duplicate_statement))
    ambiguous_accounts = (_account("111111111"), _account("222222222"))
    with pytest.raises(RobinhoodReconciliationError, match="ambiguous"):
        reconcile_robinhood_accounts(
            ambiguous_bundles,
            (reports[0], reports[0]),
            ambiguous_accounts,
        )

    bad = replace(
        reports[0],
        events=(replace(reports[0].events[0], amount_usd=Decimal("11")),),
    )
    with pytest.raises(RobinhoodReconciliationError, match="report 0/bundle 0") as exc:
        reconcile_robinhood_accounts(bundles, (bad, reports[1]), accounts)
    assert all(number not in str(exc.value) for number in ("111111111", "222222222"))


_OPTION_MARKER = "SAMPLE ISSUER OPTION MARKER"
_MARKER_CONTRACT = ("XQZZ", date(2033, 11, 22), "Call", Decimal("987.65"))
_OTHER_CONTRACT = ("XQZZ", date(2033, 11, 22), "Call", Decimal("100.00"))


def _contract_text(contract: tuple[str, date, str, Decimal]) -> str:
    symbol, expiration, right, strike = contract
    return f"{symbol} {expiration.month}/{expiration.day}/{expiration.year} {right} ${strike}"


def test_a_contradicting_option_contract_reaches_the_assignment_refusal_without_values() -> None:
    """Swapped closing strikes on the individual account refuse its only
    family-valid assignment, and the aggregate prints no contract, date,
    amount or account number."""

    bundles, reports, accounts = _pair()
    trades = (
        ("BTO", 3, date(2025, 1, 14), Decimal("1.00"), _MARKER_CONTRACT, _MARKER_CONTRACT),
        ("BTO", 4, date(2025, 1, 14), Decimal("2.00"), _OTHER_CONTRACT, _OTHER_CONTRACT),
        ("STC", 5, date(2025, 1, 20), Decimal("1.50"), _MARKER_CONTRACT, _OTHER_CONTRACT),
        ("STC", 6, date(2025, 1, 20), Decimal("2.50"), _OTHER_CONTRACT, _MARKER_CONTRACT),
    )
    rows = []
    events = []
    for code, ordinal, day, price, printed, reported in trades:
        amount = price * 100
        symbol, expiration, right, strike = printed
        rows.append(
            replace(
                _row(
                    row=ordinal,
                    code=code,
                    transaction_date=day,
                    symbol=symbol,
                    quantity=Decimal("1"),
                    price=price,
                    debit=amount if code == "BTO" else None,
                    credit=amount if code == "STC" else None,
                ),
                description=f"{_OPTION_MARKER}\n{_contract_text(printed)}",
                option_symbol=symbol,
                option_expiration=expiration,
                option_right=right,
                option_strike_usd=strike,
            )
        )
        symbol, expiration, right, strike = reported
        events.append(
            RobinhoodEvent(
                kind=(
                    RobinhoodEventKind.OPTION_BUY_TO_OPEN
                    if code == "BTO"
                    else RobinhoodEventKind.OPTION_SELL_TO_CLOSE
                ),
                source_record_index=ordinal,
                activity_date=day,
                process_date=day,
                settle_date=day,
                instrument=symbol,
                description=_contract_text(reported),
                trans_code=code,
                quantity=Decimal("1"),
                quantity_suffix=None,
                price_usd=price,
                amount_usd=-amount if code == "BTO" else amount,
                option_symbol=symbol,
                option_expiration=expiration,
                option_right=right,
                option_strike_usd=strike,
            )
        )
    statement = bundles[0].statements[0]
    individual = _bundle(
        replace(
            statement,
            account_number="999900001",
            activities=(*statement.activities, *rows),
        )
    )
    report = replace(reports[0], events=(*reports[0].events, *events))

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(
            (individual, bundles[1]),
            (report, reports[1]),
            (_account("999900001"), accounts[1]),
        )

    message = raised.value.message
    assert message == (
        "no activity report assignment reconciles ("
        "report 0/bundle 0: statement option contract contradicts activity option contract "
        "(Account Activity: statement 1 page 2 row 5; column Description); "
        "report 0/bundle 1: issued activity signature sets disagree; "
        "report 1/bundle 0: issued activity signature sets disagree)"
    )
    for marker in (_OPTION_MARKER, "XQZZ", "11/22/2033", "2033-11-22", "987.65", "999900001"):
        assert marker not in message


def test_attested_start_and_opening_date_disambiguate_identical_activity() -> None:
    activity_date = date(2025, 1, 20)
    row = _row(
        row=1,
        code="ACH",
        transaction_date=activity_date,
        credit=Decimal("10"),
    )
    bundles = (
        _bundle(
            _statement(
                account="111111111",
                family=RobinhoodStatementFamily.INDIVIDUAL,
                activities=(row,),
            )
        ),
        _bundle(
            _statement(
                account="222222222",
                family=RobinhoodStatementFamily.INDIVIDUAL,
                activities=(row,),
            )
        ),
    )
    event = _cash_event(
        ordinal=1,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=activity_date,
        amount=Decimal("10"),
    )
    reports = (
        RobinhoodActivityReportEvidence((event,), date(2025, 1, 5), _END),
        RobinhoodActivityReportEvidence((event,), date(2025, 1, 15), _END),
    )
    accounts = (
        replace(_account("111111111"), opening_date=date(2025, 1, 5)),
        replace(_account("222222222"), opening_date=date(2025, 1, 15)),
    )

    selected = accounts_mod._assign_reports(reports, bundles, accounts, ())

    assert tuple(item[0] for item in selected) == (0, 1)


def test_account_family_disambiguates_economically_identical_trades() -> None:
    row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="A",
        cusip="99971T103",
        quantity=Decimal("1"),
        price=Decimal("10"),
        debit=Decimal("10"),
    )
    bundles = (
        _bundle(
            _statement(
                account="111111111",
                family=RobinhoodStatementFamily.INDIVIDUAL,
                activities=(row,),
            )
        ),
        _bundle(
            _statement(
                account="222222222",
                family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                activities=(row,),
            )
        ),
    )
    managed = _managed_event()
    ordinary = replace(managed, kind=RobinhoodEventKind.EQUITY_BUY)
    reports = (
        RobinhoodActivityReportEvidence((ordinary,), _OPENING, _END),
        RobinhoodActivityReportEvidence((managed,), _OPENING, _END),
    )
    accounts = (_account("111111111"), _account("222222222"))

    selected = accounts_mod._assign_reports(reports, bundles, accounts, ())

    assert tuple(item[0] for item in selected) == (0, 1)


def test_complete_statement_tail_disambiguates_identical_activity() -> None:
    row = _row(
        row=1,
        code="ACH",
        transaction_date=date(2025, 1, 10),
        credit=Decimal("10"),
    )
    first_a = _statement(
        account="111111111",
        family=RobinhoodStatementFamily.INDIVIDUAL,
        activities=(row,),
    )
    first_b = replace(first_a, account_number="222222222")
    second_b = _statement(
        account="222222222",
        family=RobinhoodStatementFamily.INDIVIDUAL,
        activities=(),
        start=date(2025, 2, 1),
        end=date(2025, 2, 28),
        opening=(Decimal(), Decimal(), Decimal(), Decimal()),
    )
    bundles = (
        _bundle(first_a),
        validate_robinhood_statement_bundles((first_b, second_b))[0],
    )
    event = _cash_event(
        ordinal=1,
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        activity_date=date(2025, 1, 10),
        amount=Decimal("10"),
    )
    reports = (
        RobinhoodActivityReportEvidence((event,), _OPENING, _END),
        RobinhoodActivityReportEvidence((event,), _OPENING, date(2025, 2, 28)),
    )
    accounts = (_account("111111111"), _account("222222222"))

    selected = accounts_mod._assign_reports(reports, bundles, accounts, ())

    assert tuple(item[0] for item in selected) == (0, 1)


@pytest.mark.parametrize(
    ("bundle_change", "account_change", "message"),
    [
        (
            lambda bundle: bundle,
            lambda account: replace(account, opening_date=date(2024, 12, 31)),
            "outside the first statement period",
        ),
        (
            lambda bundle: replace(bundle, opening_components_unavailable=False),
            lambda account: account,
            "required opening layout",
        ),
    ],
)
def test_report_independent_bundle_errors_remain_direct(
    bundle_change,
    account_change,
    message: str,
) -> None:
    bundles, reports, accounts = _pair()
    with pytest.raises(RobinhoodReconciliationError, match=message) as exc:
        reconcile_robinhood_accounts(
            (bundle_change(bundles[0]), bundles[1]),
            reports,
            (account_change(accounts[0]), accounts[1]),
        )
    assert "no activity report assignment" not in str(exc.value)
    assert "bundle 0:" not in str(exc.value)


def test_managed_activity_cannot_bind_to_individual_family() -> None:
    bundles, reports, accounts = _pair()
    managed_event = _managed_event()
    managed_row = _row(
        row=1,
        code="Buy",
        transaction_date=managed_event.activity_date,
        symbol="A",
        cusip="99971T103",
        quantity=Decimal("1"),
        price=Decimal("10.00"),
        debit=Decimal("10"),
    )
    individual = _bundle(
        _statement(
            account="111111111",
            family=RobinhoodStatementFamily.INDIVIDUAL,
            activities=(managed_row,),
        )
    )
    managed_report = RobinhoodActivityReportEvidence(
        events=(managed_event,),
        start_date=_OPENING,
        end_date=_END,
    )
    with pytest.raises(RobinhoodReconciliationError, match="non-managed"):
        reconcile_robinhood_accounts(
            (individual, bundles[1]),
            (managed_report, reports[1]),
            accounts,
        )


def test_validates_range_after_assignment() -> None:
    bundles, reports, accounts = _pair()
    bad = replace(reports[0], end_date=date(2025, 1, 9))
    with pytest.raises(RobinhoodReconciliationError, match="attested report range"):
        reconcile_robinhood_accounts(bundles, (bad, reports[1]), accounts)


def test_validates_report_start_and_account_opening_after_assignment() -> None:
    bundles, reports, accounts = _pair()
    late_start = replace(reports[0], start_date=date(2025, 1, 6))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(bundles, (late_start, reports[1]), accounts)
    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity report starts "
        "after the Individual account's opening date; report 0/bundle 1: activity report "
        "starts after the Managed Individual account's opening date)"
    )
    assert raised.value.evidence_period

    late_opening = replace(accounts[0], opening_date=date(2025, 1, 11))
    with pytest.raises(RobinhoodReconciliationError, match="predates"):
        reconcile_robinhood_accounts(bundles, reports, (late_opening, accounts[1]))


def test_refuses_attested_tail_without_statement_coverage() -> None:
    bundles, reports, accounts = _pair()
    uncovered = replace(reports[0], end_date=date(2025, 2, 28))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(bundles, (uncovered, reports[1]), accounts)
    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity report ends "
        "after the Individual account's last statement; report 0/bundle 1: activity report "
        "ends after the Managed Individual account's last statement)"
    )
    assert raised.value.evidence_period


def test_refuses_range_cutting_a_statement_period() -> None:
    bundles, reports, accounts = _pair()
    first = replace(
        bundles[0].statements[0],
        period_end=date(2025, 1, 10),
        activities=(bundles[0].statements[0].activities[0],),
        account_summary=_summary(closing=(Decimal("10"), Decimal(), Decimal(), Decimal("10"))),
    )
    second = _statement(
        account="111111111",
        family=RobinhoodStatementFamily.INDIVIDUAL,
        start=date(2025, 1, 11),
        end=_END,
        opening=(Decimal("10"), Decimal(), Decimal(), Decimal("10")),
        closing=(Decimal("5"), Decimal(), Decimal(), Decimal("5")),
        activities=(bundles[0].statements[0].activities[1],),
    )
    split_bundle = validate_robinhood_statement_bundles((first, second))[0]
    cut = replace(reports[0], end_date=date(2025, 1, 15))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts((split_bundle, bundles[1]), (cut, reports[1]), accounts)
    # #1503: ending before the other account's first statement does is now a
    # failure evidence for the run's period clears, as cutting a statement is,
    # so both assignments fail only such checks and the loader adds the remedy.
    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity report ends "
        "partway through one of the Individual account's statement periods; report 0/bundle 1: "
        "activity report range contains no complete statement period of the Managed Individual "
        "account)"
    )
    assert raised.value.evidence_period
    reconcile_robinhood_accounts(
        (split_bundle, bundles[1]), (replace(cut, end_date=_END), reports[1]), accounts
    )


def test_a_period_refusal_of_only_the_other_assignment_names_no_remedy() -> None:
    """#1498: the accounts open on different days, so the managed report fails
    the individual account's opening date check, as on main. The right
    assignment fails for another reason (the individual report ends before
    its event), so evidence for the run's period isn't the remedy and the
    refusal isn't marked for one."""
    bundles, reports, accounts = _pair()
    accounts = (accounts[0], replace(accounts[1], opening_date=date(2025, 1, 11)))
    managed = replace(reports[1], start_date=date(2025, 1, 11))
    reconcile_robinhood_accounts(bundles, (reports[0], managed), accounts)

    short = replace(reports[0], end_date=date(2025, 1, 9))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(bundles, (short, managed), accounts)

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity event falls "
        "outside the attested report range; report 0/bundle 1: activity event falls outside "
        "the attested report range; report 1/bundle 0: activity report starts after the "
        "Individual account's opening date)"
    )
    assert not raised.value.evidence_period


def test_late_reports_that_both_hold_managed_trades_name_no_remedy() -> None:
    """#1503: both reports hold a managed trade and start a day late. Each
    assignment gives one to the Individual account, which no evidence for
    the run's period clears, so the refusal isn't marked for that remedy."""
    bundles, reports, accounts = _pair()
    managed = tuple(
        replace(
            report,
            start_date=date(2025, 1, 6),
            events=(*report.events, _managed_event(ordinal=3)),
        )
        for report in reports
    )
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(bundles, managed, accounts)

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: managed activity is "
        "assigned to a non-managed statement account; report 0/bundle 1: activity report starts "
        "after the Managed Individual account's opening date; report 1/bundle 0: managed "
        "activity is assigned to a non-managed statement account; report 1/bundle 1: activity "
        "report starts after the Managed Individual account's opening date)"
    )
    assert not raised.value.evidence_period


def test_a_late_managed_report_names_the_remedy_when_its_other_pairing_is_never_right() -> None:
    """#1503: the managed report holds a managed trade and starts a day late.
    The other assignment fails only by giving that report to the Individual
    account, which rules it out whatever the period, so the refusal is
    marked; starting the report on the opening date, as the remedy says,
    reconciles."""
    with pytest.raises(RobinhoodReconciliationError) as raised:
        _pair_with_security(managed_start=date(2025, 1, 6))

    assert raised.value.message == (
        "no activity report assignment reconciles (report 1/bundle 0: managed activity is "
        "assigned to a non-managed statement account; report 1/bundle 1: activity report starts "
        "after the Managed Individual account's opening date)"
    )
    assert raised.value.evidence_period
    _pair_with_security()


def test_a_crossed_report_with_an_event_before_its_accounts_opening_names_the_remedy() -> None:
    """#1508: the Managed Individual account opens on 11 January. Giving it
    the Individual report, whose deposit is on 10 January, is never right,
    whatever dates the evidence covers, so that assignment is set aside. The
    managed report also ends before its statement does, so the other
    assignment fails only that and the refusal is marked; ending the report
    with the statement, as the remedy says, reconciles."""
    bundles, reports, accounts = _pair()
    accounts = (accounts[0], replace(accounts[1], opening_date=date(2025, 1, 11)))
    managed = replace(reports[1], start_date=date(2025, 1, 11))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(
            bundles, (reports[0], replace(managed, end_date=date(2025, 1, 20))), accounts
        )

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 1: activity event predates "
        "the configured account opening date; report 1/bundle 0: activity report starts after "
        "the Individual account's opening date; report 1/bundle 1: activity report range "
        "contains no complete statement period of the Managed Individual account)"
    )
    assert raised.value.evidence_period
    reconcile_robinhood_accounts(bundles, (reports[0], managed), accounts)


def test_assignments_both_set_aside_for_events_before_the_opening_name_no_remedy() -> None:
    """#1508: both accounts open on 11 January, after the Individual report's
    10 January deposit, so each assignment gives that report to an account
    it predates. Both are set aside, and the refusal isn't marked."""
    bundles, reports, accounts = _pair()
    late = tuple(replace(account, opening_date=date(2025, 1, 11)) for account in accounts)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(bundles, reports, late)

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity event predates "
        "the configured account opening date; report 0/bundle 1: activity event predates the "
        "configured account opening date)"
    )
    assert not raised.value.evidence_period


@pytest.mark.parametrize(
    ("row", "marked"),
    [
        (None, True),
        (_row(row=1, code="COIN", transaction_date=date(2025, 1, 10), credit=Decimal("10")), False),
        (_row(row=1, code="ACH", transaction_date=date(2025, 1, 4), credit=Decimal("10")), False),
    ],
    ids=["statement-activity", "crypto-row", "row-before-opening"],
)
def test_activity_a_named_accounts_statements_show_keeps_the_remedy(
    row: RobinhoodStatementActivity | None, marked: bool
) -> None:
    """#1516: the Individual report covers only April 2026, so it holds no
    event by the end of FY 2025-26. A report from the account's opening date
    to then, as the remedy asks, holds the deposit its statement shows, so
    the refusal is marked; the full report then reconciles. When the
    statement's only row is Crypto cash, which no report holds, or is dated
    before the opening date, the refusal isn't marked."""
    bundles, reports, accounts = _pair()
    if row is not None:
        bundles = (_bundle(replace(bundles[0].statements[0], activities=(row,))), bundles[1])
    day = date(2026, 4, 5)
    late = RobinhoodActivityReportEvidence(
        events=(
            _cash_event(
                ordinal=1, kind=RobinhoodEventKind.ACH_DEPOSIT, activity_date=day, amount=Decimal(3)
            ),
        ),
        start_date=day.replace(day=1),
        end_date=date(2026, 4, 30),
    )
    financial_year = FinancialYear(2025)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(
            bundles, (late, reports[1]), accounts, financial_year=financial_year
        )

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity report starts "
        "after the Individual account's opening date; report 0/bundle 1: activity report "
        "starts after the Managed Individual account's opening date)"
    )
    assert raised.value.evidence_period is marked
    if marked:
        reconcile_robinhood_accounts(bundles, reports, accounts, financial_year=financial_year)


def test_an_event_by_the_fy_end_in_a_named_report_keeps_the_remedy() -> None:
    """#1508's rule stays beside #1516's: the Individual statement shows no
    activity, but the Individual report, ending after that statement, holds
    a 10 February 2025 deposit. A report from the opening date to the end of
    FY 2025-26, as the remedy asks, holds it too, so the refusal is marked.
    Supplying that period's evidence then reconciles."""
    bundles, reports, accounts = _pair()
    idle = _bundle(replace(bundles[0].statements[0], activities=()))
    managed = _bundle(
        replace(bundles[1].statements[0], activities=bundles[1].statements[0].activities[:1])
    )
    managed_report = replace(reports[1], events=reports[1].events[:1])
    day = date(2025, 2, 10)
    report = RobinhoodActivityReportEvidence(
        events=(
            _cash_event(
                ordinal=1, kind=RobinhoodEventKind.ACH_DEPOSIT, activity_date=day, amount=Decimal(3)
            ),
        ),
        start_date=_OPENING,
        end_date=date(2025, 2, 28),
    )
    financial_year = FinancialYear(2025)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_accounts(
            (idle, managed), (report, managed_report), accounts, financial_year=financial_year
        )

    assert raised.value.message == (
        "no activity report assignment reconciles (report 0/bundle 0: activity report ends "
        "after the Individual account's last statement; report 0/bundle 1: activity report "
        "ends after the Managed Individual account's last statement)"
    )
    assert raised.value.evidence_period

    completed = []
    for bundle in (idle, managed):
        individual = bundle is idle
        cash = Decimal(3) if individual else Decimal()
        tail = _statement(
            account=bundle.account_number,
            family=bundle.family,
            start=date(2025, 2, 1),
            end=financial_year.end_date,
            opening=(Decimal(), Decimal(), Decimal(), Decimal()),
            closing=(cash, Decimal(), Decimal(), cash),
            activities=(
                (_row(row=1, code="ACH", transaction_date=day, credit=Decimal(3)),)
                if individual
                else ()
            ),
        )
        completed.append(validate_robinhood_statement_bundles((bundle.statements[0], tail))[0])
    full_reports = tuple(
        replace(item, end_date=financial_year.end_date) for item in (report, managed_report)
    )
    reconcile_robinhood_accounts(completed, full_reports, accounts, financial_year=financial_year)


def test_managed_price_uses_cusip_and_derived_source_band() -> None:
    # Band = half-ulp(18.70) + half-ulp(row price) + half-ulp(37.40) / 2
    #      = 0.005 + 0.0005 + 0.0025 = 0.008, so 18.708 is inside and 18.709 is not.
    event = replace(
        _managed_event(),
        quantity=Decimal("2"),
        price_usd=Decimal("18.70"),
        amount_usd=Decimal("-37.40"),
    )
    row = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        symbol="ABCD",
        cusip="99971T103",
        quantity=Decimal("2"),
        price=Decimal("18.708"),
        debit=Decimal("37.40"),
    )
    bundle = _bundle(
        _statement(
            account="222222222",
            family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            activities=(row,),
        )
    )
    assert reconcile_robinhood_activity(bundle, (), (event,)).activity_groups

    outside = replace(row, price_usd=Decimal("18.709"))
    with pytest.raises(RobinhoodReconciliationError, match="quantity or price"):
        reconcile_robinhood_activity(
            _bundle(
                _statement(
                    account="222222222",
                    family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                    activities=(outside,),
                )
            ),
            (),
            (event,),
        )

    for bad_identity in (
        replace(row, cusips=("99971J105", "99971T103")),
        replace(row, symbol="ZZZZ"),
    ):
        with pytest.raises(RobinhoodReconciliationError, match="quantity or price"):
            reconcile_robinhood_activity(
                _bundle(
                    _statement(
                        account="222222222",
                        family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
                        activities=(bad_identity,),
                    )
                ),
                (),
                (event,),
            )


def test_pending_managed_trade_uses_same_band_and_cash_path() -> None:
    # Same 0.008 band as above: 18.708 sits inside it.
    pending = _row(
        row=1,
        code="Buy",
        transaction_date=date(2025, 1, 10),
        section=RobinhoodStatementSection.PENDING_TRADES,
        settle_date=date(2025, 2, 3),
        symbol="ABCD",
        cusip="99971T103",
        quantity=Decimal("2"),
        price=Decimal("18.708"),
        debit=Decimal("37.40"),
    )
    bundle = _bundle(
        _statement(
            account="222222222",
            family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            activities=(pending,),
        )
    )
    event = replace(
        _managed_event(settle_date=date(2025, 2, 3)),
        quantity=Decimal("2"),
        price_usd=Decimal("18.70"),
        amount_usd=Decimal("-37.40"),
    )
    activity = reconcile_robinhood_activity(bundle, (), (event,))
    cash = reconcile_robinhood_cash(activity, (event,))
    assert cash.periods[0].closing_unsettled_usd == Decimal("-37.40")


def test_resolves_security_by_cusip_and_preserves_symbol_provenance() -> None:
    paired, _accounts = _pair_with_security()
    result = resolve_robinhood_security_identities(paired, (_security_config(),))

    assert result.blockers == ()
    assert len(result.resolved) == 1
    assert result.resolved[0].config_index == 0
    assert result.resolved[0].observed_symbols == ("A", "ABCD")
    assert result.resolved[0].event_references


def test_statement_cusip_contradiction_requires_exact_transition() -> None:
    with pytest.raises(RobinhoodReconciliationError, match="statement CUSIP contradicts"):
        _pair_with_security(
            event_symbol="ABCD",
            event_cusip="99992G106",
            statement_cusip="99992G205",
        )


def _pair_with_pending_trade(
    statement_cusip: str,
    transitions: tuple[RobinhoodCusipTransition, ...] = (),
):
    bundles, reports, accounts = _pair()
    statement = bundles[0].statements[0]
    pending = _row(
        row=3,
        code="Buy",
        transaction_date=_END,
        section=RobinhoodStatementSection.PENDING_TRADES,
        settle_date=date(2025, 2, 3),
        cusip=statement_cusip,
        quantity=Decimal("2"),
        price=Decimal("10.00"),
        debit=Decimal("20.00"),
    )
    trade = RobinhoodEvent(
        kind=RobinhoodEventKind.EQUITY_BUY,
        source_record_index=3,
        activity_date=_END,
        process_date=_END,
        settle_date=date(2025, 2, 3),
        instrument="PEND",
        description="Synthetic buy\nCUSIP: 99961A106",
        trans_code="Buy",
        quantity=Decimal("2"),
        quantity_suffix=None,
        price_usd=Decimal("10.00"),
        amount_usd=Decimal("-20.00"),
        issuer_name="Synthetic buy",
        cusip="99961A106",
    )
    return reconcile_robinhood_accounts(
        (_bundle(replace(statement, activities=(*statement.activities, pending))), bundles[1]),
        (replace(reports[0], events=(*reports[0].events, trade)), reports[1]),
        accounts,
        transitions,
    )


def test_pending_row_cusip_must_be_its_trade_cusip() -> None:
    paired = _pair_with_pending_trade("99961A106")
    assert paired.accounts[0].activity.pending_trade_groups[0].event_ordinals == (3,)


@pytest.mark.parametrize(
    "transitions",
    [
        (),
        (
            RobinhoodCusipTransition(
                "111111111",
                "PEND",
                "99961A106",
                "99961B104",
                date(2025, 2, 1),
            ),
        ),
    ],
)
def test_pending_row_cusip_contradiction_refuses_even_with_transition(
    transitions: tuple[RobinhoodCusipTransition, ...],
) -> None:
    with pytest.raises(
        RobinhoodReconciliationError,
        match=f"report 0/bundle 0: {CUSIP_CONTRADICTION}",
    ):
        _pair_with_pending_trade("99961B104", transitions)


@pytest.mark.parametrize(
    ("new_cusip_from", "event_cusip", "statement_cusip", "isin"),
    [
        (date(2026, 7, 1), "99992G106", "99992G205", "US99992G1067"),
        (date(2025, 1, 10), "99992G205", "99992G106", "US99992G2057"),
    ],
)
def test_transition_keeps_activity_cusip_authoritative_end_to_end(
    new_cusip_from: date,
    event_cusip: str,
    statement_cusip: str,
    isin: str,
) -> None:
    transition = RobinhoodCusipTransition(
        account_number="222222222",
        symbol="ABCD",
        old_cusip="99992G106",
        new_cusip="99992G205",
        new_cusip_from=new_cusip_from,
    )
    paired, _accounts = _pair_with_security(
        event_symbol="ABCD",
        event_cusip=event_cusip,
        statement_cusip=statement_cusip,
        transitions=(transition,),
    )
    use = paired.accounts[1].activity.cusip_transition_uses[0]
    assert use.activity_cusip == event_cusip
    assert use.statement_cusip == statement_cusip
    assert paired.cusip_transition_references == ("1:3",)
    assert paired.accounts[1].evidence.events[-1].cusip == event_cusip
    assert paired.accounts[1].activity.primary_bundle.statements[0].activities[-1].cusips == (
        statement_cusip,
    )

    identities = resolve_robinhood_security_identities(
        paired,
        (
            _security_config(
                cusip=event_cusip,
                isin=isin,
            ),
        ),
    )
    quantities = reconcile_robinhood_security_quantities(paired, identities)
    assert identities.blockers == ()
    assert quantities.blockers == ()
    assert [(item.account_index, item.cusip, item.quantity) for item in quantities.quantities] == [
        (1, event_cusip, Decimal("1"))
    ]


@pytest.mark.parametrize(
    "transition",
    [
        RobinhoodCusipTransition(
            "111111111",
            "ABCD",
            "99992G106",
            "99992G205",
            date(2026, 7, 1),
        ),
        RobinhoodCusipTransition(
            "222222222",
            "ABCD",
            "99992G106",
            "99992G205",
            date(2025, 1, 1),
        ),
        RobinhoodCusipTransition(
            "222222222",
            "ABCD",
            "99972B101",
            "99992G205",
            date(2026, 7, 1),
        ),
    ],
)
def test_transition_refuses_wrong_account_date_or_pair(
    transition: RobinhoodCusipTransition,
) -> None:
    with pytest.raises(RobinhoodReconciliationError, match="CUSIP"):
        _pair_with_security(
            event_symbol="ABCD",
            event_cusip="99992G106",
            statement_cusip="99992G205",
            transitions=(transition,),
        )


def test_transition_does_not_rewrite_stale_position_cusip() -> None:
    transition = RobinhoodCusipTransition(
        account_number="222222222",
        symbol="ABCD",
        old_cusip="99992G106",
        new_cusip="99992G205",
        new_cusip_from=date(2026, 7, 1),
    )
    paired, _accounts = _pair_with_security(
        event_symbol="ABCD",
        event_cusip=transition.old_cusip,
        statement_cusip=transition.new_cusip,
        position_symbol=transition.new_cusip,
        transitions=(transition,),
    )
    identities = resolve_robinhood_security_identities(
        paired,
        (
            _security_config(
                cusip=transition.old_cusip,
                isin="US99992G1067",
            ),
        ),
    )
    quantities = reconcile_robinhood_security_quantities(paired, identities)
    assert any(
        blocker.kind is RobinhoodInventoryBlockerKind.POSITION_IDENTITY
        for blocker in quantities.blockers
    )


# --- #1187: one symbol rule for the guidance and automatic lookup ----------------


_ZQ_PENDING_ONLY = _checked_cusip("99987R10")
_ZQ_ABBREVIATION = "ZQX"


def _guidance_symbols(paired: RobinhoodAccountsReconciliation) -> dict[str, str]:
    document = tomllib.loads(render_robinhood_security_toml(paired))
    return {entry["cusip"]: entry["symbol"] for entry in document["robinhood"]["securities"]}


def _lookup_symbols(paired: RobinhoodAccountsReconciliation) -> dict[str, str]:
    return {item.cusip: item.symbol for item in observed_robinhood_identity_inputs(paired)}


@pytest.mark.parametrize("transition", [False, True], ids=["printed-current", "transition"])
@pytest.mark.parametrize(
    ("conflict", "pending", "expected"),
    [
        pytest.param(
            False,
            ("ZQX", _ZQ_NEW),
            {_ZQ_NEW: "ZQXA"},
            id="statement-symbol-beats-abbreviation",
        ),
        pytest.param(True, None, {_ZQ_NEW: None}, id="conflicting-statement-symbols"),
        pytest.param(
            False,
            ("ZQXC", _ZQ_PENDING_ONLY),
            {_ZQ_NEW: "ZQXA", _ZQ_PENDING_ONLY: "ZQXC"},
            id="no-statement-symbol",
        ),
    ],
)
def test_guidance_and_automatic_lookup_select_the_same_symbol(
    transition: bool,
    conflict: bool,
    pending: tuple[str, str] | None,
    expected: dict[str, str | None],
) -> None:
    """#1187: both read the canonical statement symbols and one rule. One
    statement symbol wins; with none, one report symbol is used; two stay
    unresolved (the guidance's ``REPLACE``, automatic lookup's ""), whatever
    the reports say."""
    paired = _pair_with_zq_symbols(transition=transition, conflict=conflict, pending=pending)

    assert paired.cusip_transition_references == (("1:3",) if transition else ())
    assert _lookup_symbols(paired) == {
        cusip: "" if symbol is None else symbol for cusip, symbol in expected.items()
    }
    assert _guidance_symbols(paired) == {
        cusip: "REPLACE" if symbol is None else symbol for cusip, symbol in expected.items()
    }


@pytest.mark.parametrize(
    ("transition", "expected"),
    [
        pytest.param(False, {_ZQ_NEW: "ZQXA", _ZQ_OLD: "ZQXC"}, id="printed-current"),
        pytest.param(True, {_ZQ_NEW: "ZQXA", _ZQ_OLD: None}, id="transition"),
    ],
)
def test_a_cusip_an_accepted_transition_replaced_gets_no_symbol(
    transition: bool, expected: dict[str, str | None]
) -> None:
    """A pending ``ZQXC`` trade uses ``_ZQ_OLD``. With the transition, the
    statement's ``ZQXA`` row prints ``_ZQ_OLD`` and the transition replaces it
    with ``_ZQ_NEW``, so the history holds both CUSIPs of the transition,
    which ADR 0006 leaves unsupported: neither consumer names a symbol for
    ``_ZQ_OLD``. Without it, ``_ZQ_OLD`` is just another security."""
    paired = _pair_with_zq_symbols(transition=transition, pending=("ZQXC", _ZQ_OLD))

    assert _lookup_symbols(paired) == {
        cusip: "" if symbol is None else symbol for cusip, symbol in expected.items()
    }
    assert _guidance_symbols(paired) == {
        cusip: "REPLACE" if symbol is None else symbol for cusip, symbol in expected.items()
    }


_ZQ_REFUSAL = (
    "Robinhood reconciliation refused: no activity report assignment reconciles "
    "(report 1/bundle 0: managed activity is assigned to a non-managed statement account; "
    "report 1/bundle 1: {})"
)


@pytest.mark.parametrize(
    ("transition", "message"),
    [
        pytest.param(
            RobinhoodCusipTransition("111111111", "ZQXA", _ZQ_OLD, _ZQ_NEW, date(2025, 1, 1)),
            _ZQ_REFUSAL.format(CUSIP_CONTRADICTION),
            id="other-account",
        ),
        pytest.param(
            RobinhoodCusipTransition("222222222", "ZQXA", _ZQ_OLD, _ZQ_NEW, date(2025, 1, 11)),
            _ZQ_REFUSAL.format(
                "statement CUSIP contradiction is not covered by the configured transition"
            ),
            id="later-date",
        ),
        pytest.param(
            RobinhoodCusipTransition(
                "222222222", "ZQXA", _checked_cusip("99987Q30"), _ZQ_NEW, date(2025, 1, 1)
            ),
            _ZQ_REFUSAL.format(
                "statement CUSIP contradiction is not covered by the configured transition"
            ),
            id="other-old-cusip",
        ),
    ],
)
def test_pending_abbreviation_keeps_invalid_transition_refusals(
    transition: RobinhoodCusipTransition,
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(RobinhoodReconciliationError) as raised:
        _pair_with_zq_symbols(
            transition=True,
            pending=(_ZQ_ABBREVIATION, _ZQ_NEW),
            transitions=(transition,),
        )

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        message,
        capsys,
        markers=(*_ZQ_MARKERS, _ZQ_OLD, _ZQ_ABBREVIATION, transition.old_cusip),
    )


def test_missing_security_config_is_typed_and_private() -> None:
    paired, _accounts = _pair_with_security()
    result = resolve_robinhood_security_identities(
        paired,
        (),
    )

    assert result.resolved == ()
    assert len(result.blockers) == 1
    assert result.blockers[0].kind is RobinhoodSecurityIdentityBlockerKind.MISSING_CONFIG
    assert "99971T103" not in repr(result.blockers[0])


def test_configured_symbol_contradiction_hard_refuses() -> None:
    paired, _accounts = _pair_with_security()
    with pytest.raises(RobinhoodReconciliationError, match="canonical symbol"):
        resolve_robinhood_security_identities(paired, (_security_config(symbol="ZZZZ"),))


def test_one_source_symbol_cannot_resolve_to_multiple_cusips() -> None:
    paired, _accounts = _pair_with_security()
    account = paired.accounts[1]
    extra = replace(
        _managed_event(ordinal=4),
        cusip="99972H108",
    )
    forged = replace(
        paired,
        accounts=(
            paired.accounts[0],
            replace(
                account,
                evidence=replace(
                    account.evidence,
                    events=(*account.evidence.events, extra),
                ),
            ),
        ),
    )
    with pytest.raises(RobinhoodReconciliationError, match="multiple CUSIPs"):
        resolve_robinhood_security_identities(
            forged,
            (),
        )


def test_security_toml_renderer_is_deterministic_and_write_free() -> None:
    paired, _accounts = _pair_with_security()
    rendered = render_robinhood_security_toml(paired)

    assert rendered == render_robinhood_security_toml(paired)
    assert 'symbol = "ABCD"' in rendered
    assert 'cusip = "99971T103"' in rendered
    assert 'isin = "REPLACE"' in rendered
    assert (
        'is_depositary_receipt = "REPLACE"  # replace with a bare true or false, without quotes'
    ) in rendered.splitlines()
    assert "identity_checks_failed = name-mismatch, unsupported-class" in (
        render_robinhood_security_toml(
            paired,
            failure_checks={"99971T103": ("name-mismatch", "unsupported-class")},
        )
    )
    assert (
        render_robinhood_security_toml(
            paired,
            securities=(_security_config(),),
        )
        == ""
    )


def _filled(text: str, replacements: dict[str, str]) -> str:
    """``text`` with each key replaced by its value; each key occurs once."""
    for old, new in replacements.items():
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    return text


# What a filer writes over each placeholder of a generated security block.
_SECURITY_FILLS = {
    'isin = "REPLACE"': 'isin = "US99971T1034"',
    'listing_exchange = "REPLACE"': 'listing_exchange = "XNAS"',
    'sub_category = "REPLACE"': 'sub_category = "COMMON"',
    'asset_class = "REPLACE"': 'asset_class = "STK"',
    'issuer_country_code = "REPLACE"': 'issuer_country_code = "US"',
    'source = "REPLACE"': 'source = "Synthetic source"',
}
_DEPOSITARY_LINE = (
    'is_depositary_receipt = "REPLACE"  # replace with a bare true or false, without quotes'
)


def test_security_block_loads_once_filled_as_its_comments_say() -> None:
    """The generated block parses as TOML and refuses unedited. Filled in
    with the depositary flag still quoted, it refuses at that field; with
    the bare boolean its comment asks for, it loads (#1423)."""
    paired, _accounts = _pair_with_security()
    rendered = render_robinhood_security_toml(paired)

    def load(text: str) -> RobinhoodSecurityConfig:
        return _parse_robinhood_security(tomllib.loads(text)["robinhood"]["securities"][0], 0)

    with pytest.raises(ConfigError):
        load(rendered)

    line = _DEPOSITARY_LINE
    filled = _filled(rendered, _SECURITY_FILLS)
    with pytest.raises(ConfigError) as quoted:
        load(_filled(filled, {line: line.replace('"REPLACE"', '"false"')}))
    assert "is_depositary_receipt" in str(quoted.value)
    assert "must be a boolean (true or false)" in str(quoted.value)

    security = load(_filled(filled, {line: line.replace('"REPLACE"', "false")}))
    assert security.is_depositary_receipt is False


@private_test
def test_private_security_identity_audit_is_explicitly_blocked() -> None:
    """With no configured securities, the blockers and the generated TOML
    cover exactly the CUSIPs the reports print, read without the parser:
    one missing-config blocker per CUSIP, holding every row that prints it,
    and one ``[[robinhood.securities]]`` block per CUSIP."""
    paired = load_private_accounts_reconciliation()
    records = load_private_report_records()
    rows_by_cusip: dict[str, set[RobinhoodSecurityEventReference]] = defaultdict(set)
    for account_index, account in enumerate(paired.accounts):
        for row_index, row in enumerate(records[account.report_index], start=1):
            cusip = report_cusip(row[4])
            if cusip is not None:
                rows_by_cusip[cusip].add(RobinhoodSecurityEventReference(account_index, row_index))

    result = resolve_robinhood_security_identities(paired, ())
    blockers_match = (
        result.resolved == ()
        and all(
            blocker.kind is RobinhoodSecurityIdentityBlockerKind.MISSING_CONFIG
            for blocker in result.blockers
        )
        and sorted(sorted(blocker.event_references) for blocker in result.blockers)
        == sorted(sorted(references) for references in rows_by_cusip.values())
    )
    rendered = tomllib.loads(render_robinhood_security_toml(paired))
    toml_matches = set(rendered) <= {"robinhood"} and [
        entry["cusip"] for entry in rendered.get("robinhood", {}).get("securities", [])
    ] == sorted(rows_by_cusip)
    if not (blockers_match and toml_matches):
        pytest.fail("private security identity audit disagrees with the reports", pytrace=False)


def test_security_quantity_ledger_records_acquisition_and_position_anchor() -> None:
    paired, _accounts = _pair_with_security()
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    result = reconcile_robinhood_security_quantities(paired, identities)

    acquisition = next(record for record in result.acquisitions if record.cusip == "99971T103")
    assert acquisition.quantity == 1
    assert acquisition.cost_usd == 10
    assert any(anchor.cusip == "99971T103" for anchor in result.anchors)
    assert any(
        quantity.cusip == "99971T103" and quantity.quantity == 1 for quantity in result.quantities
    )
    assert not any(
        blocker.cusip == "99971T103"
        and blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        for blocker in result.blockers
    )


def test_option_rows_and_positions_are_routed_outside_the_security_ledger() -> None:
    paired, _accounts = _pair_with_security()
    managed = paired.accounts[1]
    option = RobinhoodEvent(
        kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        source_record_index=4,
        activity_date=date(2025, 1, 15),
        process_date=date(2025, 1, 15),
        settle_date=date(2025, 1, 16),
        instrument="OPT",
        description="OPT 3/21/2025 Call $10.00",
        trans_code="BTO",
        quantity=Decimal("1"),
        quantity_suffix=None,
        price_usd=Decimal("2.00"),
        amount_usd=Decimal("-200.00"),
        option_symbol="OPT",
        option_expiration=date(2025, 3, 21),
        option_right="Call",
        option_strike_usd=Decimal("10.00"),
    )
    # A zero-priced option position keeps the statement's securities total.
    option_position = replace(
        _position(row=2, symbol="OPT", quantity=Decimal("1"), price=Decimal()),
        option_expiration=date(2025, 3, 21),
        option_right="Call",
        option_strike_usd=Decimal("10.00"),
    )
    statement = managed.activity.primary_bundle.statements[0]
    bundle = replace(
        managed.activity.primary_bundle,
        statements=(replace(statement, positions=(*statement.positions, option_position)),),
    )
    activity = replace(managed.activity, primary_bundle=bundle)
    forged = replace(
        paired,
        accounts=(
            paired.accounts[0],
            replace(
                managed,
                evidence=replace(managed.evidence, events=(*managed.evidence.events, option)),
                activity=activity,
                cash=replace(managed.cash, activity_reconciliation=activity),
            ),
        ),
    )
    identities = resolve_robinhood_security_identities(forged, (_security_config(),))

    result = reconcile_robinhood_security_quantities(forged, identities)

    assert result.option_event_references == (RobinhoodSecurityEventReference(1, 4),)
    assert result.option_position_references == (
        RobinhoodPositionReference(1, 0, option_position.location),
    )
    assert not any(
        blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        for blocker in result.blockers
    )


@pytest.mark.parametrize(
    ("trade", "settlement", "statement", "pending_delta", "basis"),
    [
        (
            Decimal("1"),
            Decimal(),
            Decimal(),
            Decimal("1"),
            RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE,
        ),
        (
            Decimal(),
            Decimal("1"),
            Decimal(),
            Decimal("-1"),
            RobinhoodPositionSnapshotBasis.TRADE_DATE,
        ),
    ],
)
def test_pending_trade_snapshot_selects_the_unique_matching_basis(
    trade: Decimal,
    settlement: Decimal,
    statement: Decimal,
    pending_delta: Decimal,
    basis: RobinhoodPositionSnapshotBasis,
) -> None:
    decision = inventory_mod._resolve_position_snapshot(
        trade_quantity=trade,
        settlement_quantity=settlement,
        statement_quantity=statement,
        tolerance=Decimal(),
        pending_delta=pending_delta,
    )
    assert not isinstance(decision, RobinhoodInventoryBlockerKind)
    assert decision[3] is basis
    assert decision[1] == trade
    assert decision[2] == settlement


def test_pending_trade_snapshot_refuses_unproved_or_ambiguous_basis() -> None:
    assert (
        inventory_mod._resolve_position_snapshot(
            trade_quantity=Decimal("1"),
            settlement_quantity=Decimal(),
            statement_quantity=Decimal(),
            tolerance=Decimal(),
            pending_delta=None,
        )
        is RobinhoodInventoryBlockerKind.SNAPSHOT_BASIS
    )
    assert (
        inventory_mod._resolve_position_snapshot(
            trade_quantity=Decimal("1"),
            settlement_quantity=Decimal(),
            statement_quantity=Decimal("0.5"),
            tolerance=Decimal("0.5"),
            pending_delta=Decimal("1"),
        )
        is RobinhoodInventoryBlockerKind.SNAPSHOT_BASIS
    )


def test_security_receipt_blocks_basis_but_preserves_quantity() -> None:
    paired, _accounts = _pair_with_security(include_receipt=True)
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    result = reconcile_robinhood_security_quantities(paired, identities)

    assert any(
        blocker.kind is RobinhoodInventoryBlockerKind.SECURITY_RECEIPT
        and blocker.unproved_fact is RobinhoodInventoryUnprovedFact.BASIS
        for blocker in result.blockers
    )
    assert any(
        quantity.cusip == "99971T103" and quantity.quantity == 2 for quantity in result.quantities
    )
    assert not any(
        blocker.cusip == "99971T103"
        and blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        for blocker in result.blockers
    )


def test_security_receipt_requires_one_row_bound_resolution() -> None:
    paired, _accounts = _pair_with_security(include_receipt=True)
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    with pytest.raises(
        RobinhoodReconciliationError,
        match=r"\[\[robinhood\.security_receipt_resolutions\]\]",
    ) as exc:
        resolve_robinhood_security_quantities(paired, identities, ())
    assert "source_record_index" in str(exc.value)
    assert "exact_quantity" in str(exc.value)
    assert "displayed_quantity" not in str(exc.value)
    assert "income_reported = true" in str(exc.value)
    assert "99971T103" not in str(exc.value)


def test_security_receipt_prompt_prints_no_quantity_or_date() -> None:
    """The REC prompt names the row by internal reference only: never its
    displayed quantity, nor the financial year its date falls in (#1044)."""
    paired, _accounts = _pair_with_security(
        include_receipt=True, receipt_quantity=Decimal("1234.5678")
    )
    account = paired.accounts[1]
    when = date(2031, 6, 15)
    events = tuple(
        replace(event, activity_date=when, process_date=when, settle_date=when)
        if event.kind is RobinhoodEventKind.SECURITY_RECEIPT
        else event
        for event in account.evidence.events
    )
    paired = replace(
        paired,
        accounts=(
            paired.accounts[0],
            replace(account, evidence=replace(account.evidence, events=events)),
        ),
    )
    identities = resolve_robinhood_security_identities(paired, (_security_config(),))

    with pytest.raises(RobinhoodReconciliationError) as raised:
        resolve_robinhood_security_quantities(paired, identities, ())

    message = str(raised.value)
    assert message == (
        "Robinhood reconciliation refused: Robinhood REC quantity and cost basis are "
        "unresolved. Add one completed config block for every listed source row; do not set "
        "income_reported=true until the reward FMV is included in the applicable Indian "
        "return/workpaper:\n\n"
        "# REC account_index=1, source_record_index=4\n"
        "[[robinhood.security_receipt_resolutions]]\n"
        'account_number = "REPLACE_WITH_ACCOUNT_NUMBER"\n'
        "source_record_index = 4\n"
        'cusip = "REPLACE_WITH_REC_CUSIP"\n'
        'treatment = "promotional-reward"\n'
        'exact_quantity = "REPLACE_WITH_DOCUMENTED_QUANTITY"\n'
        'reward_fair_market_value_usd = "REPLACE_WITH_DOCUMENTED_VALUE"\n'
        'income_reported_financial_year = "REPLACE_WITH_FINANCIAL_YEAR_START_YEAR"'
        "  # replace with the bare FY start year, without quotes\n"
        "income_reported = true\n"
        'source = "Robinhood reward confirmation and filed Indian return/workpaper"\n'
        'reason = "Promotional stock reward reported as income at documented FMV"'
    )
    assert "1234.5678" not in message
    assert re.search(r"\b2031\b", message) is None


_RECEIPT_YEAR_LINE = (
    'income_reported_financial_year = "REPLACE_WITH_FINANCIAL_YEAR_START_YEAR"'
    "  # replace with the bare FY start year, without quotes"
)


def _load_receipt(text: str) -> RobinhoodSecurityReceiptResolution:
    raw = tomllib.loads(text)["robinhood"]["security_receipt_resolutions"][0]
    return _parse_robinhood_security_receipt_resolution(raw, 0)


def _filled_receipt(block: str, year: str) -> str:
    """The prompt's block for ``_pair_with_security``'s REC row (account 2,
    one share worth $10, received on January 14, 2025), with ``year`` as
    the income year."""
    filled = _filled(
        block,
        {
            "REPLACE_WITH_ACCOUNT_NUMBER": "222222222",
            "REPLACE_WITH_REC_CUSIP": "99971T103",
            "REPLACE_WITH_DOCUMENTED_QUANTITY": "1",
            "REPLACE_WITH_DOCUMENTED_VALUE": "10",
        },
    )
    placeholder = '"REPLACE_WITH_FINANCIAL_YEAR_START_YEAR"'
    return _filled(filled, {_RECEIPT_YEAR_LINE: _RECEIPT_YEAR_LINE.replace(placeholder, year)})


def test_receipt_block_loads_once_filled_as_its_comments_say() -> None:
    """The refusal's block parses as TOML and refuses unedited. Filled in
    with the year still quoted, it refuses at that field; with the bare
    year its comment asks for, it loads (#1423), and the rerun has no
    blocker left (#1522)."""
    paired, _accounts = _pair_with_security(include_receipt=True)
    identities = resolve_robinhood_security_identities(paired, (_security_config(),))
    with pytest.raises(RobinhoodReconciliationError) as raised:
        resolve_robinhood_security_quantities(paired, identities, ())
    message = str(raised.value)
    block = message[message.index("[[robinhood.security_receipt_resolutions]]") :]

    with pytest.raises(ConfigError):
        _load_receipt(block)

    with pytest.raises(ConfigError) as quoted:
        _load_receipt(_filled_receipt(block, '"2024"'))
    assert "income_reported_financial_year" in str(quoted.value)
    assert "must be an integer FY start year" in str(quoted.value)

    resolution = _load_receipt(_filled_receipt(block, "2024"))
    assert resolution.income_reported_financial_year == 2024

    rerun = resolve_robinhood_security_quantities(paired, identities, (resolution,))
    assert rerun.blockers == ()
    assert rerun.identity_blockers == ()


def test_followed_receipt_prompt_and_identity_guidance_clear_the_rerun(tmp_path: Path) -> None:
    """#1522: the REC row's security has no ``[[robinhood.securities]]``
    entry. The prompt also names the private identity guidance file: the
    receipt block alone leaves the security's identity blocker. Following
    both, the rerun's quantities have no blocker. On 5baa3de the prompt
    named the receipt block only."""
    paired, accounts = _pair_with_security(include_receipt=True)
    with pytest.raises(RobinhoodReconciliationError) as raised:
        core.fa_prices(
            _receipt_bundle(paired, accounts, (), ()),
            FinancialYear(2024),
            cast("Any", None),
            tmp_path / "bundle",
            tmp_path / "user.toml",
            fetch_prices=False,
            price_cache_dir=None,
            price_fetcher=None,
            write_guidance=IdentityGuidanceFile(tmp_path),
        )
    path = tmp_path / IDENTITY_GUIDANCE_FILENAME
    note = f"\n\n{identity_guidance_line(path)}"
    message = str(raised.value)
    assert message.endswith(note)
    receipt = message.removesuffix(note)
    resolution = _load_receipt(
        _filled_receipt(
            receipt[receipt.index("[[robinhood.security_receipt_resolutions]]") :], "2024"
        )
    )
    guidance = _filled(
        _filled(path.read_text(encoding="utf-8"), _SECURITY_FILLS),
        {_DEPOSITARY_LINE: _DEPOSITARY_LINE.replace('"REPLACE"', "false")},
    )
    security = _parse_robinhood_security(tomllib.loads(guidance)["robinhood"]["securities"][0], 0)

    receipt_only = resolved_robinhood_quantities(
        _receipt_bundle(paired, accounts, (), (resolution,))
    )
    assert receipt_only.identity_blockers
    rerun = resolved_robinhood_quantities(
        _receipt_bundle(paired, accounts, (security,), (resolution,))
    )
    assert rerun.blockers == ()
    assert rerun.identity_blockers == ()


def test_promotional_reward_resolution_supplies_exact_quantity_and_basis() -> None:
    # Synthetic reward: 0.052876 displays as 0.0529; the position is
    # 0.38245 managed + 0.052876 reward = 0.435326, costing 45.89 + 6.35 = 52.24.
    paired, _accounts = _pair_with_security(
        include_receipt=True,
        position_quantity_override=Decimal("0.435326"),
        managed_quantity=Decimal("0.38245"),
        managed_cost_usd=Decimal("45.89"),
        receipt_quantity=Decimal("0.0529"),
    )
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    quantities = reconcile_robinhood_security_quantities(paired, identities)
    receipt = next(
        acquisition
        for acquisition in quantities.acquisitions
        if acquisition.event_kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    account_number = paired.accounts[
        receipt.event_reference.account_index
    ].activity.primary_bundle.account_number
    resolved = resolve_robinhood_security_quantities(
        paired,
        identities,
        (
            RobinhoodSecurityReceiptResolution(
                account_number=account_number,
                source_record_index=receipt.event_reference.event_ordinal,
                cusip=receipt.cusip,
                treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
                exact_quantity=Decimal("0.052876"),
                reward_fair_market_value_usd=Decimal("6.35"),
                income_reported_financial_year=2024,
                income_reported=True,
                source="reward confirmation and filed return",
                reason="promotional reward",
            ),
        ),
    )

    resolved_receipt = next(
        acquisition
        for acquisition in resolved.acquisitions
        if acquisition.event_reference == receipt.event_reference
    )
    resolved_movement = next(
        movement
        for movement in resolved.movements
        if movement.event_references == (receipt.event_reference,)
    )
    assert receipt.quantity == Decimal("0.0529")
    assert resolved_receipt.quantity == Decimal("0.052876")
    assert resolved_receipt.cost_usd == Decimal("6.35")
    assert resolved_movement.signed_quantity == Decimal("0.052876")
    assert all(
        anchor.ledger_quantity == anchor.statement_quantity
        for anchor in resolved.anchors
        if anchor.cusip == receipt.cusip
    )
    assert not any(
        blocker.kind is RobinhoodInventoryBlockerKind.SECURITY_RECEIPT
        for blocker in resolved.blockers
    )
    lots = reconcile_robinhood_security_lots(resolved)
    assert not lots.blockers
    assert sum((item.quantity for item in lots.open_fragments), Decimal()) == Decimal("0.435326")
    assert sum((item.cost_usd for item in lots.open_fragments), Decimal()) == Decimal("52.24")


@pytest.mark.parametrize("exact_quantity", [Decimal("0.5"), Decimal("1.6")])
def test_promotional_reward_quantity_must_unambiguously_round_trip(
    exact_quantity: Decimal,
) -> None:
    paired, _accounts = _pair_with_security(include_receipt=True)
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    raw = reconcile_robinhood_security_quantities(paired, identities)
    receipt = next(
        item for item in raw.acquisitions if item.event_kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    account_number = paired.accounts[
        receipt.event_reference.account_index
    ].activity.primary_bundle.account_number
    resolution = RobinhoodSecurityReceiptResolution(
        account_number=account_number,
        source_record_index=receipt.event_reference.event_ordinal,
        cusip=receipt.cusip,
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=exact_quantity,
        reward_fair_market_value_usd=Decimal("5"),
        income_reported_financial_year=2024,
        income_reported=True,
        source="reward confirmation and filed return",
        reason="promotional reward",
    )

    with pytest.raises(RobinhoodReconciliationError, match="contradicts"):
        resolve_robinhood_security_quantities(paired, identities, (resolution,))


def test_duplicate_promotional_reward_resolution_refuses_before_reconciliation() -> None:
    paired, _accounts = _pair_with_security(include_receipt=True)
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    raw = reconcile_robinhood_security_quantities(paired, identities)
    receipt = next(
        item for item in raw.acquisitions if item.event_kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    account_number = paired.accounts[
        receipt.event_reference.account_index
    ].activity.primary_bundle.account_number
    resolution = RobinhoodSecurityReceiptResolution(
        account_number=account_number,
        source_record_index=receipt.event_reference.event_ordinal,
        cusip=receipt.cusip,
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=receipt.quantity,
        reward_fair_market_value_usd=Decimal("5"),
        income_reported_financial_year=2024,
        income_reported=True,
        source="reward confirmation and filed return",
        reason="promotional reward",
    )

    with pytest.raises(RobinhoodReconciliationError, match="unique REC rows"):
        resolve_robinhood_security_quantities(
            paired,
            identities,
            (resolution, resolution),
        )


def test_position_anchor_mismatch_blocks_quantity_and_resolved_reachability() -> None:
    paired, _accounts = _pair_with_security(position_quantity_override=Decimal("2"))
    identities = resolve_robinhood_security_identities(
        paired,
        (_security_config(),),
    )
    result = reconcile_robinhood_security_quantities(paired, identities)

    assert any(
        blocker.kind is RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH
        and blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        for blocker in result.blockers
    )
    assert not any(quantity.cusip == "99971T103" for quantity in result.quantities)


def test_position_without_account_movement_becomes_anchor_blocker() -> None:
    paired, _accounts = _pair_with_security()
    individual = paired.accounts[0]
    statement = replace(
        individual.activity.primary_bundle.statements[0],
        positions=(
            _position(
                row=1,
                symbol="ABCD",
                quantity=Decimal("1"),
            ),
        ),
        account_summary=_summary(
            closing=(
                Decimal(),
                Decimal(),
                Decimal("10"),
                Decimal("10"),
            )
        ),
    )
    bounded = replace(
        individual.activity.primary_bundle,
        statements=(statement,),
    )
    activity = replace(individual.activity, primary_bundle=bounded)
    forged = replace(
        paired,
        accounts=(
            replace(
                individual,
                activity=activity,
                cash=replace(individual.cash, activity_reconciliation=activity),
            ),
            paired.accounts[1],
        ),
    )
    identities = resolve_robinhood_security_identities(
        forged,
        (_security_config(),),
    )
    result = reconcile_robinhood_security_quantities(forged, identities)

    assert any(
        blocker.account_index == 0 and blocker.kind is RobinhoodInventoryBlockerKind.ANCHOR_MISMATCH
        for blocker in result.blockers
    )


def test_loaned_position_requires_matching_stock_lending_source() -> None:
    paired, _accounts = _pair_with_security()
    managed = paired.accounts[1]
    statement = managed.activity.primary_bundle.statements[0]
    loaned = replace(
        statement.positions[0],
        location=SourceLocation(
            page=1,
            section=RobinhoodStatementSection.LOANED_SECURITIES,
            row=1,
        ),
    )
    bounded = replace(
        managed.activity.primary_bundle,
        statements=(replace(statement, positions=(loaned,)),),
    )
    activity = replace(managed.activity, primary_bundle=bounded)
    forged = replace(
        paired,
        accounts=(
            paired.accounts[0],
            replace(
                managed,
                activity=activity,
                cash=replace(managed.cash, activity_reconciliation=activity),
            ),
        ),
    )
    identities = resolve_robinhood_security_identities(
        forged,
        (_security_config(),),
    )
    with pytest.raises(RobinhoodReconciliationError, match="loaned security source"):
        reconcile_robinhood_security_quantities(forged, identities)


# ---- #1183: a negative equity row refuses before any sum can net it ----------

# A position symbol that no activity row or event binds to a CUSIP.
_NEG_UNRESOLVED = "SYNTHNEGOTHER"
_NEG_HELD_ROW_12 = NEGATIVE_POSITION_REFUSAL.format(
    account_index=1, section="Securities Held in Account", page=2, row=12, column="Qty"
)


def _negative_quantities(paired: RobinhoodAccountsReconciliation) -> object:
    return reconcile_robinhood_security_quantities(paired, _negative_identities(paired))


def _negative_lots(paired: RobinhoodAccountsReconciliation) -> object:
    return reconcile_robinhood_security_lots(
        resolve_robinhood_security_quantities(paired, _negative_identities(paired), ())
    )


_NEG_PATHS = pytest.mark.parametrize(
    "reconcile", [_negative_quantities, _negative_lots], ids=["quantities", "lots"]
)


@_NEG_PATHS
@pytest.mark.parametrize("symbol", [_NEG_ID, _NEG_UNRESOLVED], ids=["known", "unresolved"])
@pytest.mark.parametrize(
    ("held", "loaned", "lent", "message"),
    [
        pytest.param(_PAIR, (), (), _NEG_HELD_ROW_12, id="held-cancelling-pair"),
        pytest.param(
            (Decimal("-1"),),
            (),
            (),
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Securities Held in Account", page=2, row=11, column="Qty"
            ),
            id="held-lone",
        ),
        pytest.param(
            (),
            _PAIR,
            _PAIR,
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Loaned Securities", page=3, row=22, column="Qty"
            ),
            id="loaned-cancelling-pair",
        ),
        pytest.param(
            (),
            (Decimal("-1"),),
            (Decimal("-1"),),
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Loaned Securities", page=3, row=21, column="Qty"
            ),
            id="loaned-lone",
        ),
    ],
)
def test_negative_equity_position_refuses_before_any_sum(
    capsys: pytest.CaptureFixture[str],
    reconcile: Callable[[RobinhoodAccountsReconciliation], object],
    symbol: str,
    held: tuple[Decimal, ...],
    loaned: tuple[Decimal, ...],
    lent: tuple[Decimal, ...],
    message: str,
) -> None:
    """With a known identity, before #1183 the +2/$200 and -1/-$100 rows of one
    section netted to the bought share, with no quantity or lot blocker. A
    negative position row now refuses before the loaned-partition and
    securities-total sums and before its identity lookup, and a Loaned
    Securities row before its mirrored Stock Lending row."""
    paired = _negative_rows_pair(held=held, loaned=loaned, lent=lent, symbol=symbol)

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile(paired)

    assert_fixed_refusal(
        raised.value, RobinhoodReconciliationError, message, capsys, markers=(*_NEG_MARKERS, symbol)
    )


@_NEG_PATHS
@pytest.mark.parametrize(
    ("lent", "row"),
    [(_PAIR, 32), ((Decimal("-1"), Decimal("2")), 31)],
    ids=["negative-second", "negative-first"],
)
def test_negative_stock_lending_row_refuses_before_the_partition_sums(
    capsys: pytest.CaptureFixture[str],
    reconcile: Callable[[RobinhoodAccountsReconciliation], object],
    lent: tuple[Decimal, ...],
    row: int,
) -> None:
    """The Stock Lending rows +2 and -1 sum to the one share its Loaned
    Securities position mirrors, and the securities total reconciles, so
    before #1183 both sources agreed."""
    paired = _negative_rows_pair(loaned=_ONE, lent=lent)

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile(paired)

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        NEGATIVE_POSITION_REFUSAL.format(
            account_index=1,
            section="Stock Lending - Loaned Securities",
            page=4,
            row=row,
            column="Shares Lent",
        ),
        capsys,
        markers=_NEG_MARKERS,
    )


def _absolute(values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    return tuple(abs(value) for value in values)


@pytest.mark.parametrize(
    ("held", "loaned", "lent", "total", "negative", "existing"),
    [
        pytest.param(
            _PAIR,
            (),
            (),
            Decimal("999"),
            _NEG_HELD_ROW_12,
            "statement position sections do not reconcile to securities total",
            id="securities-total",
        ),
        pytest.param(
            (),
            _PAIR,
            (),
            None,
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1, section="Loaned Securities", page=3, row=22, column="Qty"
            ),
            "loaned security source sections disagree",
            id="loaned-sections",
        ),
        pytest.param(
            (),
            _ONE,
            (Decimal("2"), Decimal("-2")),
            None,
            NEGATIVE_POSITION_REFUSAL.format(
                account_index=1,
                section="Stock Lending - Loaned Securities",
                page=4,
                row=32,
                column="Shares Lent",
            ),
            "loaned security quantities or values disagree",
            id="loaned-quantities",
        ),
    ],
)
def test_sign_refusal_precedes_an_existing_partition_refusal(
    capsys: pytest.CaptureFixture[str],
    held: tuple[Decimal, ...],
    loaned: tuple[Decimal, ...],
    lent: tuple[Decimal, ...],
    total: Decimal | None,
    negative: str,
    existing: str,
) -> None:
    """Each input also breaks a partition check that refused it before
    #1183. The sign refusal now comes first; with every row made nonnegative,
    the same check still refuses."""
    with pytest.raises(RobinhoodReconciliationError) as raised:
        _negative_quantities(
            _negative_rows_pair(held=held, loaned=loaned, lent=lent, securities_total=total)
        )

    assert_fixed_refusal(
        raised.value, RobinhoodReconciliationError, negative, capsys, markers=_NEG_MARKERS
    )

    with pytest.raises(RobinhoodReconciliationError) as control:
        _negative_quantities(
            _negative_rows_pair(
                held=_absolute(held),
                loaned=_absolute(loaned),
                lent=_absolute(lent),
                securities_total=total,
            )
        )

    assert str(control.value) == f"Robinhood reconciliation refused: {existing}"


@pytest.mark.parametrize(
    ("held", "loaned", "lent"),
    [
        pytest.param(_ONE, (), (), id="held"),
        pytest.param((Decimal("1"), Decimal("0")), (), (), id="held-zero"),
        pytest.param((Decimal("1"), Decimal("-0")), (), (), id="held-negative-zero"),
        pytest.param((), _ONE, _ONE, id="loaned"),
        pytest.param(
            (),
            (Decimal("1"), Decimal("-0")),
            (Decimal("1"), Decimal("-0")),
            id="loaned-negative-zero",
        ),
    ],
)
def test_positive_and_zero_rows_keep_their_quantities(
    held: tuple[Decimal, ...],
    loaned: tuple[Decimal, ...],
    lent: tuple[Decimal, ...],
) -> None:
    """A positive or zero row, a negative zero included, is not negative: the
    bought share still reconciles and becomes one open lot, as before #1183."""
    paired = _negative_rows_pair(held=held, loaned=loaned, lent=lent)
    quantities = resolve_robinhood_security_quantities(paired, _negative_identities(paired), ())
    lots = reconcile_robinhood_security_lots(quantities)

    assert quantities.blockers == ()
    assert [(item.account_index, item.cusip, item.quantity) for item in quantities.quantities] == [
        (1, "99971T103", Decimal("1"))
    ]
    assert lots.blockers == ()
    assert [
        (fragment.account_index, fragment.quantity, fragment.cost_usd)
        for fragment in lots.open_fragments
    ] == [(1, Decimal("1"), Decimal("10"))]


def _negative_option(
    expiration: date | None, right: str | None, strike: Decimal | None
) -> RobinhoodStatementPosition:
    return replace(
        _negative_position(_HELD, 12, _NEG_ID, Decimal("-1")),
        option_expiration=expiration,
        option_right=right,
        option_strike_usd=strike,
    )


def test_negative_option_position_keeps_its_option_routing() -> None:
    """A row with the full option identity is an option, not equity: a
    negative one still goes to the option engine, which refuses it there."""
    option = _negative_option(date(2025, 2, 21), "Call", Decimal("10"))
    paired = _negative_rows_pair(held=_ONE, options=(option,))
    result = reconcile_robinhood_security_quantities(paired, _negative_identities(paired))

    assert result.option_position_references == (RobinhoodPositionReference(1, 0, option.location),)
    assert result.blockers == ()
    assert [(item.account_index, item.quantity) for item in result.quantities] == [
        (1, Decimal("1"))
    ]


@pytest.mark.parametrize(
    ("expiration", "right", "strike"),
    [
        (date(2025, 2, 21), None, None),
        (None, "Call", None),
        (None, None, Decimal("10")),
    ],
    ids=["expiration", "right", "strike"],
)
def test_negative_row_with_partial_option_identity_keeps_its_refusal(
    expiration: date | None, right: str | None, strike: Decimal | None
) -> None:
    paired = _negative_rows_pair(held=_ONE, options=(_negative_option(expiration, right, strike),))

    with pytest.raises(RobinhoodReconciliationError) as raised:
        _negative_quantities(paired)

    assert str(raised.value) == (
        "Robinhood reconciliation refused: statement position has partial option identity"
    )


@pytest.mark.parametrize("source_account_index", [0, 1])
def test_security_transfer_uses_one_conserved_quantity_in_both_directions(
    source_account_index: int,
) -> None:
    result = reconcile_robinhood_security_quantities(
        *_pair_with_security_transfer(source_account_index=source_account_index)
    )
    transfer = result.transfers[0]

    assert transfer.source_account_index == source_account_index
    assert transfer.destination_account_index == 1 - source_account_index
    transfer_movements = [movement for movement in result.movements if movement.transfer_index == 0]
    assert len(transfer_movements) == 2
    assert (
        sum(
            (movement.signed_quantity for movement in transfer_movements),
            Decimal(),
        )
        == 0
    )
    assert {abs(movement.signed_quantity) for movement in transfer_movements} == {
        transfer.ledger_quantity
    }
    assert len(result.residuals) == (1 if source_account_index == 1 else 0)
    assert not any(
        blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
        for blocker in result.blockers
    )


def _rows_route_once(
    paired: RobinhoodAccountsReconciliation,
    result: RobinhoodSecurityQuantityResult,
    report_rows: dict[int, int],
) -> bool:
    """Every report row (``report_rows[account_index]`` of them, read without
    the parser) is routed exactly once: to a movement or transfer leg, as an
    option, or as inventory-neutral."""
    routed = sorted(
        [
            *(ref for movement in result.movements for ref in movement.event_references),
            *result.option_event_references,
            *result.neutral_event_references,
        ]
    )
    return routed == [
        RobinhoodSecurityEventReference(account_index, ordinal)
        for account_index in range(len(paired.accounts))
        for ordinal in range(1, report_rows[account_index] + 1)
    ]


def _movements_keep_their_sides(
    paired: RobinhoodAccountsReconciliation,
    result: RobinhoodSecurityQuantityResult,
) -> bool:
    """An acquisition adds and a disposal removes its row's quantity; a
    transfer moves its source rows' quantity out of one account and into
    the other."""
    quantities = {
        RobinhoodSecurityEventReference(account_index, event.source_record_index): event.quantity
        for account_index, account in enumerate(paired.accounts)
        for event in account.evidence.events
    }
    for movement in result.movements:
        if movement.kind in {
            RobinhoodQuantityMovementKind.ACQUISITION,
            RobinhoodQuantityMovementKind.DISPOSAL,
        }:
            sign = 1 if movement.kind is RobinhoodQuantityMovementKind.ACQUISITION else -1
            (reference,) = movement.event_references
            quantity = quantities[reference]
            if quantity is None or movement.signed_quantity != sign * quantity:
                return False
            continue
        if movement.transfer_index is None:
            return False
        transfer = result.transfers[movement.transfer_index]
        moved = sum(
            (quantities[ref] or Decimal() for ref in transfer.source_event_references),
            Decimal(),
        )
        sign = 1 if movement.kind is RobinhoodQuantityMovementKind.TRANSFER_IN else -1
        if transfer.ledger_quantity != moved or movement.signed_quantity != sign * moved:
            return False
    return True


def _anchors_match_statement_positions(
    paired: RobinhoodAccountsReconciliation,
    result: RobinhoodSecurityQuantityResult,
) -> bool:
    """Each anchor's statement quantity is the sum of the statement positions
    it cites; its ledger quantity is within its tolerance of that; and its
    trade- and settlement-date quantities agree exactly when it rests on
    both, the quantity on its basis being the statement's."""
    for anchor in result.anchors:
        statement = paired.accounts[anchor.account_index].activity.primary_bundle.statements[
            anchor.statement_index
        ]
        cited = {reference.location for reference in anchor.position_references}
        basis_quantities = {
            RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT: (
                anchor.trade_date_quantity,
                anchor.settlement_date_quantity,
            ),
            RobinhoodPositionSnapshotBasis.TRADE_DATE: (anchor.trade_date_quantity,),
            RobinhoodPositionSnapshotBasis.SETTLEMENT_DATE: (anchor.settlement_date_quantity,),
        }[anchor.snapshot_basis]
        if not (
            all(
                reference.account_index == anchor.account_index
                and reference.statement_index == anchor.statement_index
                for reference in anchor.position_references
            )
            and anchor.period_end == statement.period_end
            and anchor.statement_quantity
            == sum(
                (
                    position.quantity
                    for position in statement.positions
                    if position.location in cited
                ),
                Decimal(),
            )
            and abs(anchor.ledger_quantity - anchor.statement_quantity) <= anchor.tolerance
            and all(quantity == anchor.statement_quantity for quantity in basis_quantities)
            and (anchor.trade_date_quantity == anchor.settlement_date_quantity)
            == (anchor.snapshot_basis is RobinhoodPositionSnapshotBasis.TRADE_AND_SETTLEMENT)
        ):
            return False
    return True


@private_test
def test_private_security_quantities_are_explicitly_scoped() -> None:
    paired = load_private_accounts_reconciliation()
    records = load_private_report_records()
    identities = resolve_robinhood_security_identities(paired, ())
    result = reconcile_robinhood_security_quantities(paired, identities)

    quantity_blocked = {
        (blocker.account_index, blocker.cusip)
        for blocker in result.blockers
        if blocker.unproved_fact is RobinhoodInventoryUnprovedFact.QUANTITY
    }
    report_rows = {
        account_index: len(records[account.report_index])
        for account_index, account in enumerate(paired.accounts)
    }
    if not (
        result.identity_blockers == identities.blockers
        and _rows_route_once(paired, result, report_rows)
        and _movements_keep_their_sides(paired, result)
        and _anchors_match_statement_positions(paired, result)
        and all(
            (quantity.account_index, quantity.cusip) not in quantity_blocked
            for quantity in result.quantities
        )
    ):
        pytest.fail("private security quantity audit disagrees with its sources", pytrace=False)


# ---- #1186: an exact quantity may drop the trailing zeros of its display ------

# Neutral markers for each text a refusal could echo: the account number, the
# security's symbol, its issuer and description, and the resolution's source
# and reason. The CUSIP is the fixture's checksum-valid one.
_Q_ACCOUNT = "SYNTHQACCOUNT"
_Q_ID = "SYNTHQID"
_Q_DESC = "SYNTHQDESC"
_Q_SOURCE = "SYNTHQSOURCE"
_Q_REASON = "SYNTHQREASON"
_Q_TRANSFER_MARKERS = (_Q_ACCOUNT, _Q_ID, _Q_DESC, "99971T103")
_Q_RECEIPT_MARKERS = (*_Q_TRANSFER_MARKERS, _Q_SOURCE, _Q_REASON)
_REC_CONTRADICTS = f"Robinhood reconciliation refused: {REC_CONTRADICTION}"
_TRANSFER_CONTRADICTS = f"Robinhood reconciliation refused: {TRANSFER_CONTRADICTION}"
# Each exact quantity spelled without, then with, its display's trailing zeros.
_SPELLINGS = pytest.mark.parametrize(
    ("short", "padded"),
    [
        pytest.param(Decimal("1"), Decimal("1.0000"), id="1-for-1.0000"),
        pytest.param(Decimal("0.05"), Decimal("0.0500"), id="0.05-for-0.0500"),
    ],
)
# Exact quantities that aren't the displayed number, or not under both
# rounding rules, or can't be rounded at all, against the display they refuse.
_NOT_THE_DISPLAY = pytest.mark.parametrize(
    ("exact", "display"),
    [
        pytest.param("2", "1.0000", id="other-whole-number"),
        pytest.param("0.06", "0.0500", id="other-fraction"),
        pytest.param("1.00005", "1.0000", id="half-up-disagrees"),
        pytest.param("1.00005", "1.0001", id="half-even-disagrees"),
        pytest.param("11e-1000027", "100e-1000028", id="quantum-underflows"),
        pytest.param("NaN", "1.0000", id="nan"),
        pytest.param("Infinity", "1.0000", id="infinity"),
    ],
)


def _receipt_pair(display: Decimal) -> RobinhoodAccountsReconciliation:
    """``_pair_with_security`` whose managed account buys one share and
    receives a REC row displaying ``display``, every text named by the
    markers."""
    paired, _accounts = _pair_with_security(
        include_receipt=True,
        receipt_quantity=display,
        event_symbol=_Q_ID,
        statement_symbol=_Q_ID,
        position_symbol=_Q_ID,
        issuer_name=_Q_DESC,
        receipt_symbol=_Q_ID,
        receipt_issuer=_Q_DESC,
    )
    return _with_managed_statement(paired, account_number=_Q_ACCOUNT)


def _resolve_receipt(
    paired: RobinhoodAccountsReconciliation,
    resolutions: tuple[RobinhoodSecurityReceiptResolution, ...],
) -> RobinhoodSecurityQuantityResult:
    identities = resolve_robinhood_security_identities(paired, (_security_config(symbol=_Q_ID),))
    return resolve_robinhood_security_quantities(paired, identities, resolutions)


def _receipt_resolution(exact: Decimal) -> RobinhoodSecurityReceiptResolution:
    return RobinhoodSecurityReceiptResolution(
        account_number=_Q_ACCOUNT,
        source_record_index=4,
        cusip="99971T103",
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=exact,
        reward_fair_market_value_usd=Decimal("5"),
        income_reported_financial_year=2024,
        income_reported=True,
        source=_Q_SOURCE,
        reason=_Q_REASON,
    )


def _resolved_receipt_outcome(
    paired: RobinhoodAccountsReconciliation,
    exact: Decimal,
    resolutions: tuple[RobinhoodSecurityReceiptResolution, ...],
) -> tuple[object, ...]:
    """Resolve the REC row, check it took ``exact`` with the reward's $5 cost
    and left no blocker, and return its quantities and open lots."""
    resolved = _resolve_receipt(paired, resolutions)
    lots = reconcile_robinhood_security_lots(resolved)
    (receipt,) = (
        item
        for item in resolved.acquisitions
        if item.event_kind is RobinhoodEventKind.SECURITY_RECEIPT
    )
    assert (receipt.quantity, receipt.cost_usd) == (exact, Decimal("5"))
    assert resolved.blockers == ()
    assert lots.blockers == ()
    return (
        tuple((item.account_index, item.cusip, item.quantity) for item in resolved.quantities),
        tuple((item.account_index, item.quantity, item.cost_usd) for item in lots.open_fragments),
    )


@_SPELLINGS
def test_receipt_exact_quantity_may_drop_the_displays_trailing_zeros(
    short: Decimal, padded: Decimal
) -> None:
    """Before #1186 the short spelling refused, although it rounds to the
    display under both rules, while the padded one resolved. Both now give
    the same quantities, cost and lots."""
    paired = _receipt_pair(padded)

    outcomes = [
        _resolved_receipt_outcome(paired, exact, (_receipt_resolution(exact),))
        for exact in (short, padded)
    ]

    assert (
        outcomes[0]
        == outcomes[1]
        == (
            ((1, "99971T103", 1 + padded),),
            ((1, Decimal("1"), Decimal("10")), (1, padded, Decimal("5"))),
        )
    )


@_SPELLINGS
def test_receipt_exact_quantity_from_config_may_drop_the_displays_trailing_zeros(
    tmp_path: Path, short: Decimal, padded: Decimal
) -> None:
    """The config keeps each string's spelling, and REC resolution accepts
    both the same way."""
    paired = _with_managed_statement(_receipt_pair(padded), account_number="200000002")
    outcomes = []
    for exact in (short, padded):
        block = robinhood_security_receipt_resolution_toml(
            account_number="200000002",
            cusip="99971T103",
            exact_quantity=f'"{exact}"',
        )
        config = load_user_config(write(tmp_path, robinhood_toml(extra=block)))
        assert config.robinhood is not None
        resolutions = config.robinhood.security_receipt_resolutions
        assert resolutions[0].exact_quantity.as_tuple() == exact.as_tuple()
        outcomes.append(_resolved_receipt_outcome(paired, exact, resolutions))

    assert outcomes[0] == outcomes[1]


@_NOT_THE_DISPLAY
def test_receipt_exact_quantity_that_is_not_the_display_still_refuses(
    capsys: pytest.CaptureFixture[str], exact: str, display: str
) -> None:
    paired = _receipt_pair(Decimal(display))

    with pytest.raises(RobinhoodReconciliationError) as raised:
        _resolve_receipt(paired, (_receipt_resolution(Decimal(exact)),))

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _REC_CONTRADICTS,
        capsys,
        markers=_Q_RECEIPT_MARKERS,
    )


@pytest.mark.parametrize(
    ("exact", "display"),
    [
        pytest.param("1e-1000000000", "10e-1000000001", id="quantum-invalid-operation"),
        pytest.param("1e1000001", "10e1000000", id="quantum-overflow"),
    ],
)
def test_receipt_exact_quantity_with_unrepresentable_quantum_still_refuses(
    capsys: pytest.CaptureFixture[str], exact: str, display: str
) -> None:
    paired = _receipt_pair(Decimal("1"))
    managed = paired.accounts[1]
    events = tuple(
        replace(event, quantity=Decimal(display))
        if event.kind is RobinhoodEventKind.SECURITY_RECEIPT
        else event
        for event in managed.evidence.events
    )
    paired = replace(
        paired,
        accounts=(
            paired.accounts[0],
            replace(managed, evidence=replace(managed.evidence, events=events)),
        ),
    )

    with pytest.raises(RobinhoodReconciliationError) as raised:
        _resolve_receipt(paired, (_receipt_resolution(Decimal(exact)),))

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _REC_CONTRADICTS,
        capsys,
        markers=_Q_RECEIPT_MARKERS,
    )


def _transfer_pair(source: Decimal, destination: Decimal):
    return _pair_with_security_transfer(
        source_account_index=0,
        source_quantity=source,
        destination_quantity=destination,
        account_numbers=(_Q_ACCOUNT, "222222222"),
        symbol=_Q_ID,
        issuer=_Q_DESC,
    )


@_SPELLINGS
def test_resolved_transfer_quantity_may_drop_the_displays_trailing_zeros(
    short: Decimal, padded: Decimal
) -> None:
    """Before #1186 the short spelling of the transfer's displayed quantity
    refused; both spellings now move the same quantity and lot."""
    paired, identities = _transfer_pair(padded, padded)
    outcomes = []
    for exact in (short, padded):
        result = reconcile_robinhood_security_quantities(
            paired, identities, transfer_quantities={0: exact}
        )
        lots = reconcile_robinhood_security_lots(result)
        assert result.transfers[0].resolved_quantity == exact
        assert result.blockers == ()
        assert lots.blockers == ()
        outcomes.append(
            (
                tuple(
                    movement.signed_quantity
                    for movement in result.movements
                    if movement.transfer_index == 0
                ),
                tuple((item.account_index, item.quantity) for item in result.quantities),
                tuple(
                    (item.account_index, item.quantity, item.cost_usd)
                    for item in lots.open_fragments
                ),
            )
        )

    assert (
        outcomes[0]
        == outcomes[1]
        == (
            (-padded, padded),
            ((0, Decimal()), (1, padded)),
            ((1, padded, padded * 10),),
        )
    )


@_NOT_THE_DISPLAY
def test_resolved_transfer_quantity_that_is_not_the_display_still_refuses(
    capsys: pytest.CaptureFixture[str], exact: str, display: str
) -> None:
    paired, identities = _transfer_pair(Decimal(display), Decimal(display))

    with pytest.raises(RobinhoodReconciliationError) as raised:
        reconcile_robinhood_security_quantities(
            paired, identities, transfer_quantities={0: Decimal(exact)}
        )

    assert_fixed_refusal(
        raised.value,
        RobinhoodReconciliationError,
        _TRANSFER_CONTRADICTS,
        capsys,
        markers=_Q_TRANSFER_MARKERS,
    )
