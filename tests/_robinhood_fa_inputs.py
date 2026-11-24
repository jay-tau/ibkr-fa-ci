"""The synthetic two-account Robinhood Schedule FA inputs: one configured equity
(``FA_SYMBOL``), the two accounts' statements, activity and reconciliation, and
the income, option and price results Schedule FA reads, shared by the Robinhood
Schedule FA compute, pipeline and CLI tests (#1772).

The builders read ``FA_SYMBOL`` and ``fa_statement`` from this module when they
run, so a test that patches either patches it here."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import override

from ibkr_fa.compute.robinhood_income import (
    RobinhoodDividendPayment,
    RobinhoodIncomeBlocker,
    RobinhoodIncomeBlockerKind,
    RobinhoodIncomeEventReference,
    RobinhoodIncomeResult,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionClassificationEvidence,
    RobinhoodOptionContract,
    RobinhoodOptionFaDisposition,
    RobinhoodOptionReconstruction,
    RobinhoodOptionValuationAnchor,
)
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodOptionTreatment,
    RobinhoodSecurityConfig,
)
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.models.dividend import Dividend
from ibkr_fa.normalize.robinhood import (
    RobinhoodActivityReportEvidence,
    RobinhoodPositionReference,
    reconcile_robinhood_accounts,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodAccountSummary,
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementIncome,
    RobinhoodStatementLayout,
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    SourceLocation,
)
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import previous_us_market_session, us_market_sessions
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._robinhood_security import robinhood_security
from tests._robinhood_statements import (
    robinhood_account_config,
    robinhood_account_summary,
    robinhood_activity,
    robinhood_cash_event,
    robinhood_statement,
)

FA_START = date(2025, 1, 1)
FA_OPENING = date(2025, 1, 5)
FA_END = date(2025, 12, 31)
FA_CUSIP = "99971T103"
FA_ISIN = Isin("US99971T1034")
FA_SYMBOL = "ABCD"


class FlatFx(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("80")


def fa_account(number: str) -> RobinhoodAccountConfig:
    return robinhood_account_config(
        number,
        opening_date=FA_OPENING,
        address="500 Colonial Center Parkway, Lake Mary, FL",
        zip_code="32746",
    )


def fa_security() -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=FA_SYMBOL, cusip=FA_CUSIP, isin=FA_ISIN, source="synthetic issuer evidence"
    )


def fa_income_rows(dividends: Decimal = Decimal()) -> tuple[RobinhoodStatementIncome, ...]:
    values = (
        ("Dividends", dividends),
        ("Capital Gains Distributions", Decimal()),
        ("Interest Earned", Decimal()),
        ("Stock Lending", Decimal()),
    )
    return tuple(
        RobinhoodStatementIncome(
            location=SourceLocation(
                page=1,
                section=RobinhoodStatementSection.INCOME_EXPENSE,
                row=index,
            ),
            label=label,
            this_period_usd=amount,
            year_to_date_usd=amount,
        )
        for index, (label, amount) in enumerate(values, start=1)
    )


def fa_summary(
    cash: Decimal,
    securities: Decimal,
    *,
    opening_cash: Decimal | None = None,
    opening_securities: Decimal | None = None,
) -> RobinhoodAccountSummary:
    opening_portfolio = (
        None
        if opening_cash is None or opening_securities is None
        else opening_cash + opening_securities
    )
    return robinhood_account_summary(
        opening=(
            opening_cash,
            None if opening_cash is None else Decimal(),
            opening_securities,
            opening_portfolio,
        ),
        closing=(cash, Decimal(), securities, cash + securities),
        cash_label="Net Account Balance",
        closing_percents=(Decimal(), Decimal("100") if securities else Decimal(), Decimal()),
    )


def fa_statement_row(
    row: int,
    event: RobinhoodEvent,
) -> RobinhoodStatementActivity:
    return robinhood_activity(
        row=row,
        transaction_code=event.trans_code,
        transaction_date=event.activity_date,
        description=event.description,
        symbol=event.instrument,
        settle_date=event.settle_date,
        quantity=event.quantity,
        quantity_suffix=event.quantity_suffix,
        price_usd=event.price_usd,
        debit_usd=(
            abs(event.amount_usd) if event.amount_usd is not None and event.amount_usd < 0 else None
        ),
        credit_usd=(
            event.amount_usd if event.amount_usd is not None and event.amount_usd > 0 else None
        ),
        cusips=(event.cusip,) if event.cusip is not None else (),
    )


def fa_statement(
    number: str,
    family: RobinhoodStatementFamily,
    events: tuple[RobinhoodEvent, ...],
    *,
    cash: Decimal,
    position_quantity: Decimal,
    dividends: Decimal = Decimal(),
    start: date = FA_START,
    end: date = FA_END,
    opening_cash: Decimal | None = None,
    opening_securities: Decimal | None = None,
    price: Decimal = Decimal("15"),
) -> RobinhoodStatement:
    positions = (
        (
            RobinhoodStatementPosition(
                location=SourceLocation(
                    page=1,
                    section=RobinhoodStatementSection.SECURITIES,
                    row=1,
                ),
                description="Synthetic Corporation",
                symbol_or_cusip=FA_SYMBOL,
                account_type="Margin",
                quantity=position_quantity,
                price_usd=price,
                market_value_usd=position_quantity * price,
                estimated_dividend_usd=Decimal(),
                yield_percent=None,
                portfolio_percent=Decimal("100"),
            ),
        )
        if position_quantity
        else ()
    )
    return robinhood_statement(
        family=family,
        layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
        period_start=start,
        period_end=end,
        account_number=number,
        account_summary=fa_summary(
            cash,
            position_quantity * price,
            opening_cash=opening_cash,
            opening_securities=opening_securities,
        ),
        income=fa_income_rows(dividends),
        positions=positions,
        activities=tuple(
            fa_statement_row(index, event) for index, event in enumerate(events, start=1)
        ),
    )


def fa_buy(quantity: str = "2") -> RobinhoodEvent:
    amount = Decimal(quantity) * Decimal("-10")
    return RobinhoodEvent(
        kind=RobinhoodEventKind.EQUITY_BUY,
        source_record_index=2,
        activity_date=date(2025, 1, 10),
        process_date=date(2025, 1, 10),
        settle_date=date(2025, 1, 10),
        instrument=FA_SYMBOL,
        description=f"Synthetic Corporation\nCUSIP: {FA_CUSIP}",
        trans_code="Buy",
        quantity=Decimal(quantity),
        quantity_suffix=None,
        price_usd=Decimal("10"),
        amount_usd=amount,
        issuer_name="Synthetic Corporation",
        cusip=FA_CUSIP,
    )


def fa_sale() -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=RobinhoodEventKind.EQUITY_SELL,
        source_record_index=4,
        activity_date=date(2025, 6, 10),
        process_date=date(2025, 6, 10),
        settle_date=date(2025, 6, 10),
        instrument=FA_SYMBOL,
        description=f"Synthetic Corporation\nCUSIP: {FA_CUSIP}",
        trans_code="Sell",
        quantity=Decimal("1"),
        quantity_suffix=None,
        price_usd=Decimal("15"),
        amount_usd=Decimal("14.99"),
        issuer_name="Synthetic Corporation",
        cusip=FA_CUSIP,
    )


def fa_dividend() -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=RobinhoodEventKind.CASH_DIVIDEND,
        source_record_index=3,
        activity_date=date(2025, 3, 15),
        process_date=date(2025, 3, 15),
        settle_date=date(2025, 3, 15),
        instrument=FA_SYMBOL,
        description="Synthetic dividend",
        trans_code="CDIV",
        quantity=None,
        quantity_suffix=None,
        price_usd=None,
        amount_usd=Decimal("10"),
        record_date=date(2025, 3, 10),
        payment_date=date(2025, 3, 15),
        description_shares=Decimal("2"),
        description_rate_usd=Decimal("5"),
    )


def fa_cash() -> RobinhoodEvent:
    return replace(
        robinhood_cash_event(date(2025, 1, 11), "ACH Deposit"),
        source_record_index=2,
        amount_usd=Decimal("5"),
    )


def fa_paired(*, sold: bool, dividend: bool):
    first_events = [fa_buy()]
    if dividend:
        first_events.append(fa_dividend())
    if sold:
        first_events.append(fa_sale())
    return fa_paired_events(
        tuple(first_events),
        position_quantity=Decimal("1") if sold else Decimal("2"),
        dividends=Decimal("10") if dividend else Decimal(),
    )


def fa_paired_events(
    first_events: tuple[RobinhoodEvent, ...],
    *,
    position_quantity: Decimal,
    dividends: Decimal = Decimal(),
):
    second_events = (fa_cash(),)
    first_cash = sum(
        (event.amount_usd for event in first_events if event.amount_usd is not None),
        Decimal(),
    )
    statements = (
        fa_statement(
            "111111111",
            RobinhoodStatementFamily.INDIVIDUAL,
            tuple(first_events),
            cash=first_cash,
            position_quantity=position_quantity,
            dividends=dividends,
        ),
        fa_statement(
            "222222222",
            RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            second_events,
            cash=Decimal("5"),
            position_quantity=Decimal(),
        ),
    )
    bundles = validate_robinhood_statement_bundles(statements)
    reports = tuple(
        RobinhoodActivityReportEvidence(
            events=events,
            start_date=FA_OPENING,
            end_date=FA_END,
        )
        for events in (tuple(first_events), second_events)
    )
    accounts = (fa_account("111111111"), fa_account("222222222"))
    return reconcile_robinhood_accounts(bundles, reports, accounts), accounts


def fa_income(
    *,
    dividend: bool,
    blocked: bool = False,
) -> RobinhoodIncomeResult:
    payments = (
        (
            RobinhoodDividendPayment(
                event_reference=RobinhoodIncomeEventReference(0, 3),
                security_config_index=0,
                source_record_date=date(2025, 3, 10),
                source_payment_date=date(2025, 3, 15),
                jurisdiction=CountryCode("US"),
                withholding_references=(),
                in_financial_year=False,
                dividend=Dividend(
                    symbol=Ticker(FA_SYMBOL),
                    isin=FA_ISIN,
                    payment_date=date(2025, 3, 15),
                    amount_gross=Decimal("10"),
                    currency="USD",
                    description="Synthetic dividend",
                    source_row_index=3,
                    kind=DistributionKind.CASH_DIVIDEND,
                    type_slice="Cash Dividend USD",
                    holding_date=date(2025, 3, 9),
                ),
            ),
        )
        if dividend
        else ()
    )
    blockers = (
        (
            RobinhoodIncomeBlocker(
                kind=RobinhoodIncomeBlockerKind.EVIDENCE_COVERAGE,
                account_indices=(0,),
            ),
        )
        if blocked
        else ()
    )
    return RobinhoodIncomeResult(
        dividend_payments=payments,
        interest_payments=(),
        stock_lending_payments=(),
        fee_payments=(),
        withholding_payments=(),
        annual_buckets=(),
        blockers=blockers,
        excluded_out_of_fy_event_references=(),
        excluded_packet_indices=(),
        zero_round_unreported_interest_references=(),
        unreported_lending_fee_references=(),
    )


def fa_options() -> RobinhoodOptionReconstruction:
    return RobinhoodOptionReconstruction(
        financial_year_end=date(2026, 3, 31),
        trades=(),
        lot_slices=(),
        open_lots=(),
        valuation_anchors=(),
        classifications=(),
        capital_gain_slices=(),
        business_totals=(),
        blockers=(),
        excluded_out_of_fy_close_references=(),
        a2_gross_credit_references=(),
    )


def fa_anchor_only_options(*, classified: bool) -> RobinhoodOptionReconstruction:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol=FA_SYMBOL,
        expiration=date(2026, 12, 18),
        right="Call",
        strike_usd=Decimal("100"),
    )
    classifications = (
        (
            RobinhoodOptionClassificationEvidence(
                account_index=0,
                financial_year=2025,
                treatment=RobinhoodOptionTreatment.CAPITAL_GAINS,
                source="synthetic reviewed advice",
                reason="synthetic long-dated option",
                schedule_fa_disposition=RobinhoodOptionFaDisposition.TABLE_D,
            ),
        )
        if classified
        else ()
    )
    return replace(
        fa_options(),
        valuation_anchors=(
            RobinhoodOptionValuationAnchor(
                contract=contract,
                position_reference=RobinhoodPositionReference(
                    account_index=0,
                    statement_index=0,
                    location=SourceLocation(
                        page=1,
                        section=RobinhoodStatementSection.SECURITIES,
                        row=99,
                    ),
                ),
                statement_date=FA_END,
                quantity=Decimal("1"),
                displayed_price_usd=Decimal("2"),
                market_value_usd=Decimal("200"),
            ),
        ),
        classifications=classifications,
    )


def fa_price_series(price: str = "15") -> dict[Ticker, PriceSeries]:
    # Every US market session, from the one that seeds January 1.
    sessions = us_market_sessions(previous_us_market_session(FA_START), FA_END)
    closes = {day: Decimal(price) for day in sessions}
    return {
        Ticker(FA_SYMBOL): PriceSeries(
            ticker=Ticker(FA_SYMBOL),
            currency="USD",
            closes=closes,
            has_split_in_range=False,
            source="synthetic",
            fetched_at=datetime(2026, 1, 1),
        )
    }


def fa_config(
    accounts: tuple[RobinhoodAccountConfig, RobinhoodAccountConfig],
) -> RobinhoodConfig:
    return RobinhoodConfig(accounts=accounts, securities=(fa_security(),))


def fa_entity() -> dict[Isin, FaEntityOverride]:
    return {
        FA_ISIN: FaEntityOverride(
            isin=FA_ISIN,
            address="One Synthetic Way",
            zip_code="99990",
            name="Synthetic Corporation",
            nature_of_entity="Listed company shares",
        )
    }


def fa_same_day_sale(lots: int = 3, day: date = date(2025, 6, 10)) -> tuple[RobinhoodEvent, ...]:
    """``lots`` 1-share lots bought for $10 each on the same day (rows 1 to
    ``lots``), then 1 share sold for $1.70 on ``day`` (the next row), which
    takes the same part of each lot."""
    buys = tuple(replace(fa_buy("1"), source_record_index=row) for row in range(1, lots + 1))
    sale = replace(
        fa_sale(),
        source_record_index=lots + 1,
        activity_date=day,
        process_date=day,
        settle_date=day,
        price_usd=Decimal("1.70"),
        amount_usd=Decimal("1.70"),
    )
    return (*buys, sale)
