"""Synthetic Robinhood option trades, statement positions and option
classifications, the stand-in reconciled accounts the option reconstruction
reads, and the reconstruction and compute runs over them, shared by the
Robinhood options compute, pipeline and CLI tests (#1772)."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionReconstruction,
    RobinhoodOptionsResult,
    compute_robinhood_options,
    reconstruct_robinhood_options,
)
from ibkr_fa.config import (
    RobinhoodAccountConfig,
    RobinhoodConfig,
    RobinhoodOptionClassificationConfig,
    RobinhoodOptionTreatment,
    RobinhoodOptionTurnoverBasis,
)
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
)
from ibkr_fa.parser.robinhood import RobinhoodEvent, RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    SourceLocation,
)
from ibkr_fa.pipeline import FinancialYear
from tests._fx_stubs import AuditedFxStub
from tests._robinhood_statements import robinhood_account_config, robinhood_cash_event

OPTION_ACCOUNT = "111111111"
OPTION_OTHER_ACCOUNT = "222222222"
OPTION_SYMBOL = "ABCD"
OPTION_EXPIRATION = date(2025, 12, 19)
OPTION_STRIKE = Decimal("100.00")
OPTION_FY_START = date(2025, 4, 1)
OPTION_FY_END = date(2026, 3, 31)
OPTION_FINANCIAL_YEAR = FinancialYear(2025)


@dataclass(frozen=True)
class StandInStatement:
    period_start: date
    period_end: date
    positions: tuple[RobinhoodStatementPosition, ...]


@dataclass(frozen=True)
class StandInBundle:
    account_number: str
    coverage_start: date
    coverage_end: date
    statements: tuple[StandInStatement, ...]


@dataclass(frozen=True)
class StandInActivity:
    primary_bundle: StandInBundle


@dataclass(frozen=True)
class StandInAccount:
    evidence: RobinhoodActivityReportEvidence
    activity: StandInActivity


@dataclass(frozen=True)
class StandInPaired:
    accounts: tuple[StandInAccount, StandInAccount]


def option_account(number: str, opening_date: date = date(2025, 1, 1)) -> RobinhoodAccountConfig:
    return robinhood_account_config(
        number, opening_date=opening_date, address="synthetic", zip_code="", source="synthetic"
    )


def option_classification(
    treatment: RobinhoodOptionTreatment,
    *,
    account_number: str = OPTION_ACCOUNT,
    financial_year: int = 2025,
) -> RobinhoodOptionClassificationConfig:
    business = treatment is RobinhoodOptionTreatment.SPECULATIVE_BUSINESS
    return RobinhoodOptionClassificationConfig(
        account_number=account_number,
        financial_year=financial_year,
        fema_risk_acknowledged=True,
        treatment=treatment,
        source="synthetic reviewed advice",
        reason="synthetic option classification",
        books_maintained=False if business else None,
        other_speculative_business=False if business else None,
        nature_of_business_code="21010" if business else None,
        turnover_basis=(RobinhoodOptionTurnoverBasis.ICAI_5_11A_DIFFERENCES if business else None),
        external_expenses_inr=Decimal() if business else None,
        bp_additions_inr=Decimal() if business else None,
        bp_deductions_inr=Decimal() if business else None,
    )


def option_config(
    treatment: RobinhoodOptionTreatment | None,
    *,
    opening_date: date = date(2025, 1, 1),
    financial_year: int = 2025,
    account_number: str = OPTION_ACCOUNT,
) -> RobinhoodConfig:
    return RobinhoodConfig(
        accounts=(
            option_account(account_number, opening_date),
            option_account(OPTION_OTHER_ACCOUNT),
        ),
        option_classifications=(
            ()
            if treatment is None
            else (option_classification(treatment, financial_year=financial_year),)
        ),
    )


def option_event(
    kind: RobinhoodEventKind,
    ordinal: int,
    activity_date: date,
    settle_date: date,
    *,
    quantity: str = "2",
    price: str = "1.00",
    amount: str = "-199.99",
    right: str = "Call",
    symbol: str = OPTION_SYMBOL,
    description: str = "synthetic option",
    expiration: date = OPTION_EXPIRATION,
) -> RobinhoodEvent:
    return RobinhoodEvent(
        kind=kind,
        source_record_index=ordinal,
        activity_date=activity_date,
        process_date=activity_date,
        settle_date=settle_date,
        instrument=symbol,
        description=description,
        trans_code=("BTO" if kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN else "STC"),
        quantity=Decimal(quantity),
        quantity_suffix=None,
        price_usd=Decimal(price),
        amount_usd=Decimal(amount),
        option_symbol=symbol,
        option_expiration=expiration,
        option_right=right,
        option_strike_usd=OPTION_STRIKE,
    )


def option_cash_event() -> RobinhoodEvent:
    return robinhood_cash_event(OPTION_FY_START, "synthetic cash")


def option_statement_position(
    statement_row: int,
    *,
    quantity: str = "2",
    price: str = "1.20",
    page: int = 1,
    symbol: str = OPTION_SYMBOL,
    description: str = "synthetic option position",
    expiration: date = OPTION_EXPIRATION,
    right: str = "Call",
) -> RobinhoodStatementPosition:
    quantity_value = Decimal(quantity)
    price_value = Decimal(price)
    return RobinhoodStatementPosition(
        location=SourceLocation(
            page=page,
            section=RobinhoodStatementSection.SECURITIES,
            row=statement_row,
        ),
        description=description,
        symbol_or_cusip=symbol,
        account_type="Cash",
        quantity=quantity_value,
        price_usd=price_value,
        market_value_usd=quantity_value * price_value * Decimal("100"),
        estimated_dividend_usd=Decimal(),
        yield_percent=None,
        portfolio_percent=Decimal("1"),
        option_expiration=expiration,
        option_right=right,
        option_strike_usd=OPTION_STRIKE,
    )


def option_paired(
    events: tuple[RobinhoodEvent, ...],
    *,
    statements: tuple[StandInStatement, ...] | None = None,
    report_start: date = OPTION_FY_START,
    report_end: date = OPTION_FY_END,
    other_statements: tuple[StandInStatement, ...] = (),
    account_number: str = OPTION_ACCOUNT,
) -> RobinhoodAccountsReconciliation:
    if statements is None:
        statements = (
            StandInStatement(
                period_start=date(2025, 5, 1),
                period_end=date(2025, 5, 31),
                positions=(option_statement_position(1),),
            ),
            StandInStatement(
                period_start=date(2025, 6, 1),
                period_end=date(2025, 6, 30),
                positions=(),
            ),
        )
    accounts = (
        StandInAccount(
            evidence=RobinhoodActivityReportEvidence(events, report_start, report_end),
            activity=StandInActivity(
                StandInBundle(
                    account_number=account_number,
                    coverage_start=report_start,
                    coverage_end=report_end,
                    statements=statements,
                )
            ),
        ),
        StandInAccount(
            evidence=RobinhoodActivityReportEvidence(
                (option_cash_event(),),
                report_start,
                report_end,
            ),
            activity=StandInActivity(
                StandInBundle(
                    account_number=OPTION_OTHER_ACCOUNT,
                    coverage_start=report_start,
                    coverage_end=report_end,
                    statements=other_statements,
                )
            ),
        ),
    )
    return cast("RobinhoodAccountsReconciliation", StandInPaired(accounts))


def closed_option_events() -> tuple[RobinhoodEvent, RobinhoodEvent]:
    return (
        option_event(
            RobinhoodEventKind.OPTION_BUY_TO_OPEN,
            2,
            date(2025, 5, 1),
            date(2025, 5, 2),
        ),
        option_event(
            RobinhoodEventKind.OPTION_SELL_TO_CLOSE,
            3,
            date(2025, 6, 1),
            date(2025, 6, 2),
            price="1.50",
            amount="300.00",
        ),
    )


def reconstruct_synthetic_options(
    paired: RobinhoodAccountsReconciliation,
    treatment: RobinhoodOptionTreatment | None,
    *,
    opening_date: date = date(2025, 1, 1),
    financial_year: FinancialYear = OPTION_FINANCIAL_YEAR,
) -> RobinhoodOptionReconstruction:
    return reconstruct_robinhood_options(
        paired,
        config=option_config(
            treatment,
            opening_date=opening_date,
            financial_year=financial_year.start_year,
        ),
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
    )


def compute_synthetic_options(
    paired: RobinhoodAccountsReconciliation,
    treatment: RobinhoodOptionTreatment | None,
    *,
    opening_date: date = date(2025, 1, 1),
    financial_year: FinancialYear = OPTION_FINANCIAL_YEAR,
    capital_rate: Decimal = Decimal("80"),
    business_rate: Decimal = Decimal("85"),
) -> RobinhoodOptionsResult:
    result = compute_robinhood_options(
        paired,
        config=option_config(
            treatment,
            opening_date=opening_date,
            financial_year=financial_year.start_year,
        ),
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        capital_gains_fx_policy=AuditedFxStub(capital_rate),
        # Keyed by the FY end: a business lookup for any other date raises KeyError.
        business_fx_policy=AuditedFxStub(
            {financial_year.end_date: business_rate}, specified_date=financial_year.end_date
        ),
    )
    # #1145: robinhood-fa runs the reconstruction alone, so on every input it
    # must give what the full compute gives, every blocker included.
    reconstruction = reconstruct_synthetic_options(
        paired, treatment, opening_date=opening_date, financial_year=financial_year
    )
    assert all(
        getattr(reconstruction, field.name) == getattr(result, field.name)
        for field in fields(RobinhoodOptionReconstruction)
    )
    return result


# Synthetic privacy markers: none may reach a blocker summary or CLI output.
SYNTH_DESCRIPTION = "SYNTHDESC-5521"
SYNTH_UNDERLYING = "ZQXW"
SYNTH_EXPIRATION = date(2031, 2, 21)
SYNTH_MARKERS = (SYNTH_DESCRIPTION, SYNTH_UNDERLYING, SYNTH_EXPIRATION.isoformat(), "736251")
# By default, synthetic trades move 736251.00 of cash; these are its exact premiums
# per contract for 1, 2 and 2.5 contracts.
SYNTH_PRICES = {"1": "7362.51", "2": "3681.255", "2.5": "2945.004"}


def synth_option_trade(
    kind: RobinhoodEventKind,
    row: int,
    activity_date: date,
    *,
    quantity: str = "2",
    price: str | None = None,
    amount: str = "736251.00",
    right: str = "Call",
) -> RobinhoodEvent:
    """A marked synthetic trade at activity report row ``row``: ``amount`` paid
    on a purchase or received on a sale."""
    return option_event(
        kind,
        row,
        activity_date,
        activity_date + timedelta(days=1),
        quantity=quantity,
        price=SYNTH_PRICES[quantity] if price is None else price,
        amount=f"-{amount}" if kind is RobinhoodEventKind.OPTION_BUY_TO_OPEN else amount,
        right=right,
        symbol=SYNTH_UNDERLYING,
        description=SYNTH_DESCRIPTION,
        expiration=SYNTH_EXPIRATION,
    )
