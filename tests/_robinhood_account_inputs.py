"""The synthetic two-account Robinhood statement inputs: an individual and a
managed account's statements, rows, positions, bundles, activity reports and
configs, the managed buy and its configured security, the security receipt
bundle, and the CUSIP-transition and negative-position cases, shared by the
Robinhood normalize accounts, identity, capital gains, pipeline and loader
tests (#1784)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import (
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodSecurityConfig,
    RobinhoodSecurityReceiptResolution,
)
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    reconcile_robinhood_accounts,
    resolve_robinhood_security_identities,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.normalize.robinhood.identity_exceptions import RobinhoodCusipTransition
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodAccountSummary,
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementLayout,
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    RobinhoodStockLoanedSecurity,
    SourceLocation,
)
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from ibkr_fa.types import cusip_check_digit
from tests._robinhood_security import robinhood_security
from tests._robinhood_statements import (
    robinhood_account_config,
    robinhood_account_summary,
    robinhood_activity,
    robinhood_statement,
)

_START = date(2025, 1, 1)
ACCOUNT_OPENING = date(2025, 1, 5)
PERIOD_END = date(2025, 1, 31)


def statement_summary(
    *,
    opening: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None] = (
        None,
        None,
        None,
        None,
    ),
    closing: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal(),
        Decimal(),
        Decimal(),
        Decimal(),
    ),
) -> RobinhoodAccountSummary:
    return robinhood_account_summary(
        opening=opening, closing=closing, cash_label="Net Account Balance"
    )


def statement_row(
    *,
    row: int,
    code: str,
    transaction_date: date,
    section: RobinhoodStatementSection = RobinhoodStatementSection.ACCOUNT_ACTIVITY,
    settle_date: date | None = None,
    symbol: str | None = None,
    cusip: str | None = None,
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
        symbol=symbol,
        settle_date=settle_date,
        quantity=quantity,
        price_usd=price,
        debit_usd=debit,
        credit_usd=credit,
        cusips=(cusip,) if cusip is not None else (),
    )


def account_statement(
    *,
    account: str,
    family: RobinhoodStatementFamily,
    activities: tuple[RobinhoodStatementActivity, ...],
    start: date = _START,
    end: date = PERIOD_END,
    opening: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None] = (
        None,
        None,
        None,
        None,
    ),
    closing: tuple[Decimal, Decimal, Decimal, Decimal] = (
        Decimal(),
        Decimal(),
        Decimal(),
        Decimal(),
    ),
    positions: tuple[RobinhoodStatementPosition, ...] = (),
    stock_loaned: tuple[RobinhoodStockLoanedSecurity, ...] = (),
) -> RobinhoodStatement:
    return robinhood_statement(
        family=family,
        layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
        period_start=start,
        period_end=end,
        account_number=account,
        account_summary=statement_summary(opening=opening, closing=closing),
        positions=positions,
        activities=activities,
        stock_loaned_securities=stock_loaned,
    )


def statement_position(
    *,
    row: int,
    symbol: str,
    quantity: Decimal,
    price: Decimal = Decimal("10"),
    section: RobinhoodStatementSection = RobinhoodStatementSection.SECURITIES,
) -> RobinhoodStatementPosition:
    return RobinhoodStatementPosition(
        location=SourceLocation(page=1, section=section, row=row),
        description="Synthetic position",
        symbol_or_cusip=symbol,
        account_type="Margin",
        quantity=quantity,
        price_usd=price,
        market_value_usd=quantity * price,
        estimated_dividend_usd=Decimal(),
        yield_percent=None,
        portfolio_percent=Decimal("100"),
    )


def statement_bundle(statement: RobinhoodStatement):
    return validate_robinhood_statement_bundles((statement,))[0]


def cash_event(
    *,
    ordinal: int,
    kind: RobinhoodEventKind,
    activity_date: date,
    amount: Decimal,
    settle_date: date | None = None,
) -> RobinhoodEvent:
    codes = {
        RobinhoodEventKind.ACH_DEPOSIT: ("ACH", "ACH Deposit"),
        RobinhoodEventKind.INTER_ACCOUNT_CASH_IN: (
            "ITRF",
            "Transfer from Brokerage to Brokerage",
        ),
        RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT: (
            "ITRF",
            "Transfer from Brokerage to Brokerage",
        ),
        RobinhoodEventKind.MANAGEMENT_FEE: ("ADVFEE", "Management Fee"),
    }
    code, description = codes[kind]
    return RobinhoodEvent(
        kind=kind,
        source_record_index=ordinal,
        activity_date=activity_date,
        process_date=activity_date,
        settle_date=settle_date or activity_date,
        instrument=None,
        description=description,
        trans_code=code,
        quantity=None,
        quantity_suffix=None,
        price_usd=None,
        amount_usd=amount,
    )


def managed_buy_event(
    *,
    ordinal: int = 1,
    price: Decimal = Decimal("10.00"),
    settle_date: date = date(2025, 1, 10),
) -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=RobinhoodEventKind.MANAGED_EQUITY_BUY,
        source_record_index=ordinal,
        activity_date=date(2025, 1, 10),
        process_date=date(2025, 1, 10),
        settle_date=settle_date,
        instrument="A",
        description="Managed trade\nCUSIP: 99971T103\nManaged Trade",
        trans_code="Buy",
        quantity=Decimal("1"),
        quantity_suffix=None,
        price_usd=price,
        amount_usd=Decimal("-10.00"),
        issuer_name="Managed trade",
        cusip="99971T103",
    )


def account_config(number: str) -> RobinhoodAccountConfig:
    return robinhood_account_config(number, opening_date=ACCOUNT_OPENING)


def accounts_pair():
    transfer_date = date(2025, 1, 12)
    individual_statement = account_statement(
        account="111111111",
        family=RobinhoodStatementFamily.INDIVIDUAL,
        activities=(
            statement_row(
                row=1,
                code="ACH",
                transaction_date=date(2025, 1, 10),
                credit=Decimal("10"),
            ),
            statement_row(
                row=2,
                code="ITRF",
                transaction_date=transfer_date,
                debit=Decimal("5"),
            ),
        ),
    )
    managed_statement = account_statement(
        account="222222222",
        family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
        activities=(
            statement_row(
                row=1,
                code="ADVFEE",
                transaction_date=date(2025, 1, 11),
                debit=Decimal("1"),
            ),
            statement_row(
                row=2,
                code="ITRF",
                transaction_date=transfer_date,
                credit=Decimal("5"),
            ),
        ),
    )
    individual_report = RobinhoodActivityReportEvidence(
        events=(
            cash_event(
                ordinal=1,
                kind=RobinhoodEventKind.ACH_DEPOSIT,
                activity_date=date(2025, 1, 10),
                amount=Decimal("10"),
            ),
            cash_event(
                ordinal=2,
                kind=RobinhoodEventKind.INTER_ACCOUNT_CASH_OUT,
                activity_date=transfer_date,
                amount=Decimal("-5"),
            ),
        ),
        start_date=ACCOUNT_OPENING,
        end_date=PERIOD_END,
    )
    managed_report = RobinhoodActivityReportEvidence(
        events=(
            cash_event(
                ordinal=1,
                kind=RobinhoodEventKind.MANAGEMENT_FEE,
                activity_date=date(2025, 1, 11),
                amount=Decimal("-1"),
            ),
            cash_event(
                ordinal=2,
                kind=RobinhoodEventKind.INTER_ACCOUNT_CASH_IN,
                activity_date=transfer_date,
                amount=Decimal("5"),
            ),
        ),
        start_date=ACCOUNT_OPENING,
        end_date=PERIOD_END,
    )
    return (
        (statement_bundle(individual_statement), statement_bundle(managed_statement)),
        (individual_report, managed_report),
        (account_config("111111111"), account_config("222222222")),
    )


def pair_with_security(
    *,
    include_receipt: bool = False,
    position_quantity_override: Decimal | None = None,
    managed_quantity: Decimal = Decimal("1"),
    managed_cost_usd: Decimal = Decimal("10"),
    receipt_quantity: Decimal = Decimal("1"),
    event_symbol: str = "A",
    event_cusip: str = "99971T103",
    statement_cusip: str | None = None,
    statement_symbol: str = "ABCD",
    position_symbol: str = "ABCD",
    issuer_name: str = "Managed trade",
    receipt_symbol: str = "ABCD",
    receipt_issuer: str = "Synthetic receipt",
    transitions: tuple[RobinhoodCusipTransition, ...] = (),
    managed_start: date = ACCOUNT_OPENING,
):
    bundles, reports, accounts = accounts_pair()
    managed_event = replace(
        managed_buy_event(ordinal=3),
        instrument=event_symbol,
        issuer_name=issuer_name,
        cusip=event_cusip,
        quantity=managed_quantity,
        amount_usd=-managed_cost_usd,
    )
    assert managed_event.quantity is not None
    receipt = RobinhoodEvent(
        kind=RobinhoodEventKind.SECURITY_RECEIPT,
        source_record_index=4,
        activity_date=date(2025, 1, 14),
        process_date=date(2025, 1, 14),
        settle_date=date(2025, 1, 14),
        instrument=receipt_symbol,
        description=f"{receipt_issuer}\nCUSIP: 99971T103",
        trans_code="REC",
        quantity=receipt_quantity,
        quantity_suffix=None,
        price_usd=None,
        amount_usd=None,
        issuer_name=receipt_issuer,
        cusip="99971T103",
    )
    activity_rows = [
        *bundles[1].statements[0].activities,
        statement_row(
            row=3,
            code="Buy",
            transaction_date=managed_event.activity_date,
            symbol=statement_symbol,
            cusip=statement_cusip or managed_event.cusip,
            quantity=managed_event.quantity,
            price=managed_event.price_usd,
            debit=managed_cost_usd,
        ),
    ]
    report_events = [*reports[1].events, managed_event]
    position_quantity = managed_event.quantity
    if include_receipt:
        assert receipt.quantity is not None
        activity_rows.append(
            statement_row(
                row=4,
                code="REC",
                transaction_date=receipt.activity_date,
                symbol=receipt_symbol,
                cusip=receipt.cusip,
                quantity=receipt.quantity,
            )
        )
        report_events.append(receipt)
        position_quantity += receipt.quantity
    if position_quantity_override is not None:
        position_quantity = position_quantity_override
    managed_statement = replace(
        bundles[1].statements[0],
        activities=tuple(activity_rows),
        positions=(
            statement_position(
                row=1,
                symbol=position_symbol,
                quantity=position_quantity,
            ),
        ),
        account_summary=statement_summary(
            closing=(
                Decimal(),
                Decimal(),
                position_quantity * Decimal("10"),
                position_quantity * Decimal("10"),
            )
        ),
    )
    managed_bundle = statement_bundle(managed_statement)
    managed_report = replace(
        reports[1],
        events=tuple(report_events),
        start_date=managed_start,
    )
    paired = reconcile_robinhood_accounts(
        (bundles[0], managed_bundle),
        (reports[0], managed_report),
        accounts,
        transitions,
    )
    return paired, accounts


def security_config(
    *,
    symbol: str = "ABCD",
    cusip: str = "99971T103",
    isin: str = "US99971T1034",
) -> RobinhoodSecurityConfig:
    return robinhood_security(symbol=symbol, cusip=cusip, isin=isin, source="synthetic evidence")


def receipt_bundle(
    paired: RobinhoodAccountsReconciliation,
    accounts: tuple[RobinhoodAccountConfig, ...],
    securities: tuple[RobinhoodSecurityConfig, ...],
    receipts: tuple[RobinhoodSecurityReceiptResolution, ...],
) -> RobinhoodBundle:
    """What a run loads from ``paired`` and a config with ``securities`` and
    ``receipts``, as far as its quantities and identity guidance read."""
    return cast(
        "RobinhoodBundle",
        SimpleNamespace(
            paired=paired,
            robinhood=RobinhoodConfig(
                accounts=accounts, securities=securities, security_receipt_resolutions=receipts
            ),
            securities=RobinhoodSecurityIdentities(securities),
            identities=resolve_robinhood_security_identities(paired, securities),
            identity_resolution=None,
        ),
    )


# ---- #1187: the CUSIP-transition case ----


def checked_cusip(body: str) -> str:
    return body + cusip_check_digit(body)


ZQ_NEW = checked_cusip("99987Q10")
ZQ_OLD = checked_cusip("99987Q20")
ZQ_ISSUER = "SAMPLE ISSUER ZQ INC"
_ZQ_DESCRIPTION = "SYNTHZQDESCRIPTION"
_ZQ_TRANSITION = RobinhoodCusipTransition("222222222", "ZQXA", ZQ_OLD, ZQ_NEW, date(2025, 1, 1))

# Markers every ``pair_with_zq_symbols`` input holds.
ZQ_MARKERS = (ZQ_NEW, ZQ_ISSUER, _ZQ_DESCRIPTION, "ZQXA", "111111111", "222222222")


def pair_with_zq_symbols(
    *,
    transition: bool,
    conflict: bool = False,
    pending: tuple[str, str] | None = None,
    transitions: tuple[RobinhoodCusipTransition, ...] | None = None,
) -> RobinhoodAccountsReconciliation:
    """Managed account 222222222 buys one ``ZQXA`` share of ``ZQ_NEW`` on 2025-01-10.

    Its statement row prints ``ZQXA`` under ``ZQ_OLD``, accepted by
    ``_ZQ_TRANSITION``, when ``transition``; under ``ZQ_NEW`` otherwise.
    ``conflict`` adds a second ``ZQXA`` buy on 2025-01-14 whose row prints
    ``ZQXAB`` under ``ZQ_NEW``: a managed row pairs with an activity symbol
    it extends, so this is how the statement can name a second symbol while
    the reports name only ``ZQXA``. ``pending`` (activity symbol, CUSIP) adds
    a pending managed buy; the pending table prints no symbol.
    ``transitions`` replaces the configured transitions.
    """
    bundles, reports, accounts = accounts_pair()
    buys = [(3, date(2025, 1, 10), "ZQXA", ZQ_OLD if transition else ZQ_NEW)]
    if conflict:
        buys.append((4, date(2025, 1, 14), "ZQXAB", ZQ_NEW))
    events = tuple(
        replace(
            managed_buy_event(ordinal=ordinal),
            activity_date=day,
            process_date=day,
            settle_date=day,
            instrument="ZQXA",
            description=f"{ZQ_ISSUER}\nCUSIP: {ZQ_NEW}\nManaged Trade",
            issuer_name=ZQ_ISSUER,
            cusip=ZQ_NEW,
        )
        for ordinal, day, _symbol, _printed in buys
    )
    rows = tuple(
        statement_row(
            row=ordinal,
            code="Buy",
            transaction_date=day,
            symbol=symbol,
            cusip=printed,
            quantity=Decimal("1"),
            price=Decimal("10.00"),
            debit=Decimal("10.00"),
        )
        for ordinal, day, symbol, printed in buys
    )
    pending_rows: tuple[RobinhoodStatementActivity, ...] = ()
    pending_events: tuple[RobinhoodEvent, ...] = ()
    if pending is not None:
        symbol, cusip = pending
        pending_rows = (
            statement_row(
                row=5,
                code="Buy",
                transaction_date=PERIOD_END,
                section=RobinhoodStatementSection.PENDING_TRADES,
                settle_date=date(2025, 2, 3),
                cusip=cusip,
                quantity=Decimal("2"),
                price=Decimal("10.00"),
                debit=Decimal("20.00"),
            ),
        )
        pending_events = (
            replace(
                managed_buy_event(ordinal=5),
                activity_date=PERIOD_END,
                process_date=PERIOD_END,
                settle_date=date(2025, 2, 3),
                instrument=symbol,
                description=f"{ZQ_ISSUER}\nCUSIP: {cusip}\nManaged Trade",
                quantity=Decimal("2"),
                amount_usd=Decimal("-20.00"),
                issuer_name=ZQ_ISSUER,
                cusip=cusip,
            ),
        )
    held = Decimal(len(buys))
    managed = bundles[1].statements[0]
    managed_bundle = statement_bundle(
        replace(
            managed,
            activities=tuple(
                replace(row, description=_ZQ_DESCRIPTION)
                for row in (*managed.activities, *rows, *pending_rows)
            ),
            positions=(statement_position(row=1, symbol="ZQXA", quantity=held),),
            account_summary=statement_summary(
                closing=(Decimal(), Decimal(), held * Decimal("10"), held * Decimal("10"))
            ),
        )
    )
    if transitions is None:
        transitions = (_ZQ_TRANSITION,) if transition else ()
    return reconcile_robinhood_accounts(
        (bundles[0], managed_bundle),
        (reports[0], replace(reports[1], events=(*reports[1].events, *events, *pending_events))),
        accounts,
        transitions,
    )


# ---- #1183: the negative-position case ----

# Neutral markers for each text a refusal could echo: the account number, the
# security's symbol and issuer name, and every statement row's description.
# The CUSIP is the fixture's checksum-valid one.
_NEG_ACCOUNT = "SYNTHNEGACCOUNT"
NEG_ID = "SYNTHNEGID"
_NEG_NAME = "SYNTHNEGNAME"
_NEG_DESC = "SYNTHNEGDESC"
NEG_MARKERS = (_NEG_ACCOUNT, NEG_ID, _NEG_NAME, _NEG_DESC, "99971T103")

HELD_SECTION = RobinhoodStatementSection.SECURITIES
_LOANED = RobinhoodStatementSection.LOANED_SECURITIES
_LENT = RobinhoodStatementSection.STOCK_LOANED_SECURITIES
_NEG_PRICE = Decimal("100")
# Each table has its own page and first row number, so a message's page and
# row name exactly one source row.
_NEG_PAGES = {HELD_SECTION: 2, _LOANED: 3, _LENT: 4}
_NEG_FIRST_ROWS = {HELD_SECTION: 11, _LOANED: 21, _LENT: 31}


def negative_position(
    section: RobinhoodStatementSection, row: int, symbol: str, quantity: Decimal
) -> RobinhoodStatementPosition:
    return replace(
        statement_position(
            row=row, symbol=symbol, quantity=quantity, price=_NEG_PRICE, section=section
        ),
        location=SourceLocation(page=_NEG_PAGES[section], section=section, row=row),
        description=_NEG_DESC,
    )


def with_managed_statement(
    paired: RobinhoodAccountsReconciliation, **changes: object
) -> RobinhoodAccountsReconciliation:
    """``paired`` with these field ``changes`` to the managed account's one
    statement."""
    managed = paired.accounts[1]
    (statement,) = managed.activity.primary_bundle.statements
    bundle = replace(managed.activity.primary_bundle, statements=(replace(statement, **changes),))
    activity = replace(managed.activity, primary_bundle=bundle)
    return replace(
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


def negative_rows_pair(
    *,
    held: tuple[Decimal, ...] = (),
    loaned: tuple[Decimal, ...] = (),
    lent: tuple[Decimal, ...] = (),
    symbol: str = NEG_ID,
    securities_total: Decimal | None = None,
    options: tuple[RobinhoodStatementPosition, ...] = (),
) -> RobinhoodAccountsReconciliation:
    """``pair_with_security`` with the managed account's one bought share
    named by the markers, and these statement rows at $100 a share in place
    of its position. The securities total is the positions' market value
    unless ``securities_total`` is given."""
    paired, _accounts = pair_with_security(
        event_symbol=NEG_ID,
        statement_symbol=NEG_ID,
        issuer_name=_NEG_NAME,
    )
    positions = (
        *(
            negative_position(section, row, symbol, quantity)
            for section, quantities in ((HELD_SECTION, held), (_LOANED, loaned))
            for row, quantity in enumerate(quantities, start=_NEG_FIRST_ROWS[section])
        ),
        *options,
    )
    loans = tuple(
        RobinhoodStockLoanedSecurity(
            location=SourceLocation(page=_NEG_PAGES[_LENT], section=_LENT, row=row),
            description=_NEG_DESC,
            symbol=symbol,
            shares_lent=shares,
            price_usd=_NEG_PRICE,
            market_value_usd=shares * _NEG_PRICE,
        )
        for row, shares in enumerate(lent, start=_NEG_FIRST_ROWS[_LENT])
    )
    total = (
        sum((position.market_value_usd for position in positions), Decimal())
        if securities_total is None
        else securities_total
    )
    return with_managed_statement(
        paired,
        account_number=_NEG_ACCOUNT,
        positions=positions,
        stock_loaned_securities=loans,
        account_summary=statement_summary(closing=(Decimal(), Decimal(), total, total)),
    )


def negative_identities(paired: RobinhoodAccountsReconciliation):
    return resolve_robinhood_security_identities(paired, (security_config(symbol=NEG_ID),))


ONE_ROW = (Decimal("1"),)
CANCELLING_PAIR = (Decimal("2"), Decimal("-1"))
