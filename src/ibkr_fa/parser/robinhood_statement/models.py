"""Typed source evidence from one Robinhood brokerage statement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class RobinhoodStatementKind(StrEnum):
    """Triage of one statement PDF.

    Crypto means a positively identified Robinhood Crypto statement; brokerage
    means only "not crypto", so the brokerage parser accepts or refuses it.
    """

    BROKERAGE = "brokerage"
    CRYPTO = "crypto"


class RobinhoodStatementFamily(StrEnum):
    """Accepted brokerage statement account-title families."""

    UNSPECIFIED = "unspecified"
    INDIVIDUAL = "individual"
    MANAGED_INDIVIDUAL = "managed-individual"


class RobinhoodStatementLayout(StrEnum):
    """Account Summary layout generation."""

    LEGACY_NET_ACCOUNT = "legacy-net-account"
    DEPOSIT_SWEEP = "deposit-sweep"


class RobinhoodStatementSection(StrEnum):
    """Source section owning a parsed row."""

    ACCOUNT_SUMMARY = "Account Summary"
    INCOME_EXPENSE = "Income and Expense Summary"
    SECURITIES = "Securities Held in Account"
    LOANED_SECURITIES = "Loaned Securities"
    ACCOUNT_ACTIVITY = "Account Activity"
    PENDING_TRADES = "Executed Trades Pending Settlement"
    SWEEP_BANKS = "Deposit Sweep Program Banks"
    SWEEP_ACTIVITY = "Deposit Sweep Activity"
    STOCK_LOANED_SECURITIES = "Stock Lending - Loaned Securities"
    STOCK_LOAN_ACTIVITY = "Stock Lending - Loan Activity"
    STOCK_COLLATERAL_ACTIVITY = "Stock Lending - Collateral Activity"


@dataclass(frozen=True, slots=True, repr=False)
class SourceLocation:
    """Physical source location inside the extracted PDF."""

    page: int
    section: RobinhoodStatementSection
    row: int


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodAccountSummary:
    """Normalized source rows from Account Summary."""

    source_cash_label: str
    opening_brokerage_cash_usd: Decimal | None
    closing_brokerage_cash_usd: Decimal
    opening_deposit_sweep_usd: Decimal | None
    closing_deposit_sweep_usd: Decimal
    opening_total_securities_usd: Decimal | None
    closing_total_securities_usd: Decimal
    opening_portfolio_value_usd: Decimal | None
    closing_portfolio_value_usd: Decimal
    closing_cash_percent: Decimal
    closing_equities_percent: Decimal
    closing_options_percent: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementIncome:
    """One Income and Expense Summary row."""

    location: SourceLocation
    label: str
    this_period_usd: Decimal
    year_to_date_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementPosition:
    """One held/loaned equity or option position."""

    location: SourceLocation
    description: str
    symbol_or_cusip: str
    account_type: str
    quantity: Decimal
    price_usd: Decimal
    market_value_usd: Decimal
    estimated_dividend_usd: Decimal
    yield_percent: Decimal | None
    portfolio_percent: Decimal
    option_expiration: date | None = None
    option_right: str | None = None
    option_strike_usd: Decimal | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementActivity:
    """One Account Activity or pending-settlement row."""

    location: SourceLocation
    description: str
    symbol: str | None
    account_type: str
    transaction_code: str
    transaction_date: date
    settle_date: date | None
    quantity: Decimal | None
    quantity_suffix: str | None
    price_usd: Decimal | None
    debit_usd: Decimal | None
    credit_usd: Decimal | None
    cusips: tuple[str, ...] = ()
    source_account_reference: str | None = None
    destination_account_reference: str | None = None
    option_symbol: str | None = None
    option_expiration: date | None = None
    option_right: str | None = None
    option_strike_usd: Decimal | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementSweepBank:
    """One Program Bank balance row."""

    location: SourceLocation
    bank_name: str
    balance_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementSweepActivity:
    """One Deposit Sweep Activity ledger row."""

    location: SourceLocation
    description: str
    transaction_date: date
    debit_usd: Decimal | None
    credit_usd: Decimal | None
    balance_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStockLoanedSecurity:
    """One Stock Lending - Loaned Securities row."""

    location: SourceLocation
    description: str
    symbol: str
    shares_lent: Decimal
    price_usd: Decimal
    market_value_usd: Decimal


class RobinhoodStockLoanKind(StrEnum):
    """Direction of one Stock Lending - Loan Activity row.

    Both directions print the same ``SLJNL`` transaction code; the share column
    the row fills says which one it is.
    """

    LEND = "lend"
    RETURN = "return"


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStockLoanActivity:
    """One Stock Lending - Loan Activity row: shares lent from, or returned to, the account."""

    location: SourceLocation
    description: str
    symbol: str
    transaction_date: date
    kind: RobinhoodStockLoanKind
    shares: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStockCollateralActivity:
    """One Stock Lending - Collateral Activity row."""

    location: SourceLocation
    description: str
    transaction_date: date
    debit_usd: Decimal | None
    credit_usd: Decimal | None
    balance_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatementTotal:
    """One source subtotal with named monetary columns."""

    location: SourceLocation
    label: str
    values: tuple[tuple[str, Decimal], ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodStatement:
    """Lossless typed evidence from one accepted brokerage statement."""

    family: RobinhoodStatementFamily
    layout: RobinhoodStatementLayout
    period_start: date
    period_end: date
    account_number: str
    account_type_label: str | None
    registration_lines: tuple[str, ...]
    base_currency: str
    page_count: int
    document_identifier: str | None
    account_summary: RobinhoodAccountSummary
    income: tuple[RobinhoodStatementIncome, ...]
    positions: tuple[RobinhoodStatementPosition, ...]
    activities: tuple[RobinhoodStatementActivity, ...]
    sweep_banks: tuple[RobinhoodStatementSweepBank, ...]
    sweep_activity: tuple[RobinhoodStatementSweepActivity, ...]
    stock_loaned_securities: tuple[RobinhoodStockLoanedSecurity, ...]
    stock_loan_activity: tuple[RobinhoodStockLoanActivity, ...]
    stock_collateral_activity: tuple[RobinhoodStockCollateralActivity, ...]
    totals: tuple[RobinhoodStatementTotal, ...]
    disclosure_pages: tuple[int, ...]
