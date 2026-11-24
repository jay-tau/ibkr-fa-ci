"""The synthetic Robinhood capital gains inputs: one configured security's
trades, lot slices and lot results over a stand-in account pair, the CG compute
over them, and the canonical, traded and same-day sale-order cases, shared by
the Robinhood capital gains compute, FX resolution, pipeline, filing route and
income tests (#1790)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import cast

from ibkr_fa.compute.robinhood_capital_gains import (
    RobinhoodCapitalGainsResult,
    compute_robinhood_capital_gains,
)
from ibkr_fa.compute.robinhood_capital_gains import _compute as _compute_from_lots
from ibkr_fa.compute.robinhood_lots import (
    RobinhoodLotBlocker,
    RobinhoodLotSlice,
    RobinhoodLotSliceKind,
    RobinhoodSecurityLotResult,
)
from ibkr_fa.config import RobinhoodAccountConfig, RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodSecurityEventReference,
    reconcile_robinhood_accounts,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.types import Isin
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_account_inputs import accounts_pair as _reconciliation_pair
from tests._robinhood_account_inputs import security_config as _reconciliation_security
from tests._robinhood_account_inputs import statement_bundle as _reconciliation_bundle
from tests._robinhood_account_inputs import statement_position as _statement_position
from tests._robinhood_account_inputs import statement_row as _statement_row
from tests._robinhood_cg import FILING_BY_HAND
from tests._robinhood_refusals import EVENT_REFERENCES
from tests._robinhood_security import configured_securities, robinhood_security
from tests._robinhood_statements import robinhood_account_config, robinhood_cash_event

CG_CUSIP = "99971T103"
CG_ISIN = Isin("US99971T1034")
CG_SYMBOL = "ABCD"
CG_FY_START = date(2025, 4, 1)
CG_FY_END = date(2026, 3, 31)
CG_SALE_DATE = date(2025, 6, 15)
CG_RATE = Decimal("85")


@dataclass(frozen=True)
class _Statement:
    activities: tuple[object, ...] = ()


@dataclass(frozen=True)
class BundleStandIn:
    account_number: str
    statements: tuple[_Statement, ...] = (_Statement(),)


@dataclass(frozen=True)
class ActivityStandIn:
    primary_bundle: BundleStandIn


@dataclass(frozen=True)
class _Account:
    evidence: RobinhoodActivityReportEvidence
    activity: ActivityStandIn


@dataclass(frozen=True)
class PairedStandIn:
    accounts: tuple[_Account, _Account]


def _account(number: str) -> RobinhoodAccountConfig:
    return robinhood_account_config(
        number, opening_date=date(2020, 1, 1), address="synthetic", zip_code="", source="synthetic"
    )


def cg_security(*, asset_class: str = "STK") -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=CG_SYMBOL, cusip=CG_CUSIP, isin=CG_ISIN, asset_class=asset_class, source="synthetic"
    )


def cg_config(*, securities: tuple[RobinhoodSecurityConfig, ...] | None = None) -> RobinhoodConfig:
    return RobinhoodConfig(
        accounts=(_account("111111111"), _account("222222222")),
        securities=(cg_security(),) if securities is None else securities,
    )


def cg_trade(
    kind: RobinhoodEventKind,
    ordinal: int,
    activity_date: date,
    quantity: str,
    price: str,
    amount: str,
) -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=kind,
        source_record_index=ordinal,
        activity_date=activity_date,
        process_date=activity_date,
        settle_date=activity_date,
        instrument=CG_SYMBOL,
        description=f"Synthetic {CG_SYMBOL}",
        trans_code=(
            "Sell"
            if kind
            in {
                RobinhoodEventKind.EQUITY_SELL,
                RobinhoodEventKind.MANAGED_EQUITY_SELL,
            }
            else "Buy"
        ),
        quantity=Decimal(quantity),
        quantity_suffix=None,
        price_usd=Decimal(price),
        amount_usd=Decimal(amount),
        issuer_name="Synthetic Corporation",
        cusip=CG_CUSIP,
    )


def _cash_event(day: date = date(2025, 1, 1)) -> RobinhoodEvent:
    return robinhood_cash_event(day, "Synthetic cash")


def cg_paired(
    events: tuple[RobinhoodEvent, ...],
    *,
    report_start: date = date(2020, 1, 1),
    report_end: date = date(2030, 12, 31),
    second_report_start: date | None = None,
) -> RobinhoodAccountsReconciliation:
    accounts = (
        _Account(
            evidence=RobinhoodActivityReportEvidence(events, report_start, report_end),
            activity=ActivityStandIn(BundleStandIn("111111111")),
        ),
        _Account(
            evidence=RobinhoodActivityReportEvidence(
                (_cash_event(second_report_start or date(2025, 1, 1)),),
                report_start if second_report_start is None else second_report_start,
                report_end,
            ),
            activity=ActivityStandIn(BundleStandIn("222222222")),
        ),
    )
    # The adapter and identity resolver only consume the retained evidence and
    # primary-bundle identity. Building unrelated cash/statement fixtures here
    # would test the already-covered reconciliation layers again.
    return cast("RobinhoodAccountsReconciliation", PairedStandIn(accounts))


def cg_reference(ordinal: int) -> RobinhoodSecurityEventReference:
    return RobinhoodSecurityEventReference(0, ordinal)


def cg_slice(
    lot_ordinal: int,
    sale_ordinals: tuple[int, ...],
    *,
    quantity: str,
    cost: str,
    original_date: date,
    entry_date: date | None = None,
) -> RobinhoodLotSlice:
    return RobinhoodLotSlice(
        lot_id=cg_reference(lot_ordinal),
        account_index=0,
        cusip=CG_CUSIP,
        movement_references=tuple(cg_reference(ordinal) for ordinal in sale_ordinals),
        quantity=Decimal(quantity),
        cost_usd=Decimal(cost),
        original_date=original_date,
        entry_date=original_date if entry_date is None else entry_date,
        kind=RobinhoodLotSliceKind.DISPOSAL,
    )


def cg_lot_result(
    slices: tuple[RobinhoodLotSlice, ...],
    *,
    blockers: tuple[RobinhoodLotBlocker, ...] = (),
    identity_blockers=(),
) -> RobinhoodSecurityLotResult:
    return RobinhoodSecurityLotResult(
        slices=slices,
        open_fragments=(),
        holding_intervals=(),
        blockers=blockers,
        identity_blockers=identity_blockers,
    )


def cg_base_events(*, first_sale_amount: str = "50.00") -> tuple[RobinhoodEvent, ...]:
    return (
        cg_trade(
            RobinhoodEventKind.EQUITY_BUY,
            1,
            date(2024, 1, 1),
            "1.0000",
            "10.00",
            "-10.00",
        ),
        cg_trade(
            RobinhoodEventKind.EQUITY_BUY,
            2,
            date(2025, 1, 15),
            "3.0000",
            "20.00",
            "-60.00",
        ),
        cg_trade(
            RobinhoodEventKind.EQUITY_SELL,
            3,
            CG_SALE_DATE,
            "1.0000",
            "50.00",
            first_sale_amount,
        ),
        cg_trade(
            RobinhoodEventKind.EQUITY_SELL,
            4,
            CG_SALE_DATE,
            "2.0000",
            "50.00",
            "100.00",
        ),
    )


def cg_base_slices() -> tuple[RobinhoodLotSlice, ...]:
    return (
        cg_slice(
            1,
            (3, 4),
            quantity="1.0000",
            cost="10.00",
            original_date=date(2024, 1, 1),
        ),
        cg_slice(
            2,
            (3, 4),
            quantity="2.0000",
            cost="40.00",
            original_date=date(2025, 1, 15),
        ),
    )


def cg_from_lots(
    events: tuple[RobinhoodEvent, ...],
    lot_result: RobinhoodSecurityLotResult,
    *,
    config: RobinhoodConfig | None = None,
    financial_year_start: date = CG_FY_START,
    financial_year_end: date = CG_FY_END,
    report_start: date = date(2020, 1, 1),
    report_end: date = date(2030, 12, 31),
    second_report_start: date | None = None,
):
    config = cg_config() if config is None else config
    return _compute_from_lots(
        cg_paired(
            events,
            report_start=report_start,
            report_end=report_end,
            second_report_start=second_report_start,
        ),
        lot_result,
        config=config,
        securities=config.securities,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=AuditedFxStub(CG_RATE),
    )


def canonical_cg_case() -> tuple[RobinhoodAccountsReconciliation, RobinhoodConfig]:
    bundles, reports, accounts = _reconciliation_pair()
    financial_year_start = date(2024, 4, 1)
    financial_year_end = date(2025, 3, 31)
    buy = cg_trade(
        RobinhoodEventKind.MANAGED_EQUITY_BUY,
        3,
        date(2025, 1, 10),
        "1.0000",
        "10.00",
        "-10.00",
    )
    sale = cg_trade(
        RobinhoodEventKind.MANAGED_EQUITY_SELL,
        4,
        date(2025, 2, 15),
        "1.0000",
        "20.00",
        "20.00",
    )
    individual_statement = replace(
        bundles[0].statements[0],
        period_start=financial_year_start,
        period_end=financial_year_end,
    )
    managed_statement = replace(
        bundles[1].statements[0],
        period_start=financial_year_start,
        period_end=financial_year_end,
        activities=(
            *bundles[1].statements[0].activities,
            _statement_row(
                row=3,
                code="Buy",
                transaction_date=buy.activity_date,
                symbol=CG_SYMBOL,
                cusip=CG_CUSIP,
                quantity=buy.quantity,
                price=buy.price_usd,
                debit=Decimal("10.00"),
            ),
            _statement_row(
                row=4,
                code="Sell",
                transaction_date=sale.activity_date,
                symbol=CG_SYMBOL,
                cusip=CG_CUSIP,
                quantity=sale.quantity,
                price=sale.price_usd,
                credit=Decimal("20.00"),
            ),
        ),
    )
    paired = reconcile_robinhood_accounts(
        (
            _reconciliation_bundle(individual_statement),
            _reconciliation_bundle(managed_statement),
        ),
        (
            replace(
                reports[0],
                start_date=financial_year_start,
                end_date=financial_year_end,
            ),
            replace(
                reports[1],
                events=(*reports[1].events, buy, sale),
                start_date=financial_year_start,
                end_date=financial_year_end,
            ),
        ),
        accounts,
    )
    return paired, RobinhoodConfig(
        accounts=accounts,
        securities=(_reconciliation_security(),),
    )


def traded_cg_result(
    *trades: tuple[date, str, str, str],
    financial_year_start: date,
    rate: Decimal | Mapping[date, Decimal] = Decimal("84.15"),
    report_start: date = date(2024, 4, 1),
    report_end: date | None = None,
    held: Decimal = Decimal(),
    security: RobinhoodSecurityConfig | None = None,
) -> RobinhoodCapitalGainsResult:
    """:func:`compute_robinhood_capital_gains` over managed-account trades.

    Each trade is ``(date, quantity, price, Amount)``; a negative Amount buys.
    Both accounts open on ``report_start`` and their statements and reports
    cover it to ``report_end`` (default: the FY end). The managed statement
    closes holding ``held`` shares at $10. ``security`` replaces the traded
    security's configured entry.
    """
    bundles, reports, accounts = _reconciliation_pair()
    financial_year_end = date(financial_year_start.year + 1, 3, 31)
    end = financial_year_end if report_end is None else report_end
    accounts = tuple(replace(account, opening_date=report_start) for account in accounts)
    events = tuple(
        cg_trade(
            RobinhoodEventKind.MANAGED_EQUITY_BUY
            if amount.startswith("-")
            else RobinhoodEventKind.MANAGED_EQUITY_SELL,
            ordinal,
            day,
            quantity,
            price,
            amount,
        )
        for ordinal, (day, quantity, price, amount) in enumerate(trades, start=3)
    )
    rows = tuple(
        _statement_row(
            row=ordinal,
            code="Buy" if amount.startswith("-") else "Sell",
            transaction_date=day,
            symbol=CG_SYMBOL,
            cusip=CG_CUSIP,
            quantity=Decimal(quantity),
            price=Decimal(price),
            debit=-Decimal(amount) if amount.startswith("-") else None,
            credit=None if amount.startswith("-") else Decimal(amount),
        )
        for ordinal, (day, quantity, price, amount) in enumerate(trades, start=3)
    )
    individual, managed = (bundle.statements[0] for bundle in bundles)
    paired = reconcile_robinhood_accounts(
        (
            _reconciliation_bundle(replace(individual, period_start=report_start, period_end=end)),
            _reconciliation_bundle(
                replace(
                    managed,
                    period_start=report_start,
                    period_end=end,
                    activities=(*managed.activities, *rows),
                    positions=(
                        (_statement_position(row=1, symbol=CG_SYMBOL, quantity=held),)
                        if held
                        else managed.positions
                    ),
                    account_summary=replace(
                        managed.account_summary,
                        closing_total_securities_usd=held * 10
                        if held
                        else managed.account_summary.closing_total_securities_usd,
                    ),
                )
            ),
        ),
        (
            replace(reports[0], start_date=report_start, end_date=end),
            replace(
                reports[1],
                events=(*reports[1].events, *events),
                start_date=report_start,
                end_date=end,
            ),
        ),
        accounts,
    )
    config = RobinhoodConfig(
        accounts=accounts, securities=(security or _reconciliation_security(),)
    )
    return compute_robinhood_capital_gains(
        paired,
        config=config,
        securities=configured_securities(config),
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=AuditedFxStub(rate),
    )


# ---- #1540: same-day sales whose order decides the short/long-term split ----

# The issue's example: one share bought on 2023-01-03 for $120 (long-term by
# the sale) and one on 2025-01-02 for $20 (short-term), both sold on
# 2025-06-13 in the managed account, at the ₹85 Rule 115 rate. Trades take
# managed activity rows 3 onward: buys 1:3 and 1:4, sales 1:5 and 1:6.
LONG_TERM_BUY = (date(2023, 1, 3), "1.0000", "120.00", "-120.00")
SHORT_TERM_BUY = (date(2025, 1, 2), "1.0000", "20.00", "-20.00")
SALE_ORDER_DAY = date(2025, 6, 13)
SALE_AT_100 = (SALE_ORDER_DAY, "1.0000", "100.00", "100.00")
SALE_AT_110 = (SALE_ORDER_DAY, "1.0000", "110.00", "110.00")
# The refusal line the issue's example gets: its sales are rows 5 and 6.
SALE_ORDER_REASON = (
    "Robinhood's activity report gives each sale a date but no time. Sales of one security "
    "on one day got different cash per share and sold both short-term and long-term lots, "
    "so the order they were executed in decides how the gain splits between short-term and "
    f"long-term. {EVENT_REFERENCES}sales 1:5, 1:6 (lots 1:3, 1:4). Robinhood's trade "
    f"confirmations give each sale's execution time. {FILING_BY_HAND}, matching the lots, "
    "oldest first, to the sales in execution order."
)


def sale_order_case(
    *trades: tuple[date, str, str, str],
) -> RobinhoodCapitalGainsResult:
    """The real CG compute over ``trades`` (default: the issue's example) in FY 2025-26."""
    return traded_cg_result(
        *(trades or (LONG_TERM_BUY, SHORT_TERM_BUY, SALE_AT_100, SALE_AT_110)),
        financial_year_start=date(2025, 4, 1),
        rate=Decimal("85"),
        report_start=date(2023, 1, 1),
    )
