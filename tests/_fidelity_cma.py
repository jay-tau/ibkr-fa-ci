"""Synthetic Fidelity CMA inputs and statement texts, the ``pdftotext`` stub and
the refusal texts that several test modules share.

Account and transaction values are synthetic. The statement texts retain the
broker and sweep fund names and the sweep fund identifiers the parser keys on.
A module that needs a variant passes the builders' keywords, changes a result
with ``dataclasses.replace()`` or edits a copy of a text. The refusal texts are
written out here, never read from ``src``, so a test compares the code's text
with an independent copy.
"""

from __future__ import annotations

import subprocess
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.parser.fidelity_cma import (
    CmaCashActivity,
    CmaCashActivityKind,
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaIncomeCredit,
    CmaIncomeKind,
    CmaPosition,
    CmaStatement,
    CmaValuationVariant,
    CmaWithholding,
    CmaYearEndIncomeSummary,
    CmaYearEndPosition,
    parse_statement,
)
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker

_ACCOUNT = "Z99-999999"
_HOLDER = "TAX PAYER"

# --- refusal texts several modules expect -------------------------------------
# Each is the text every site pins; a site that pins more of the refusal writes
# the rest around it.

A2_PRICING_2024_REFUSAL = (
    "Fidelity CMA Schedule FA Table A2 daily pricing does not support calendar year 2024"
)
YEAR_END_HOLDINGS_REFUSAL = "CMA year-end holdings do not match the December monthly statement"
YEAR_END_TOTALS_REFUSAL = "totals do not match the year-end report"
YEAR_END_WITHHOLDING_SIGN_REFUSAL = "year-end withholding must not be positive"
WITHHOLDING_CENTS_REFUSAL = "withholding is not a bounded whole-cent"
UNMATCHED_SALE_REFUSAL = "SPAXX sale cannot be matched to complete prior acquisition history"
HELD_OVER_24_MONTHS_REFUSAL = "in-FY SPAXX holdings over 24 months"
TOTAL_HOLDINGS_REFUSAL = "Total Holdings row is malformed: expected exactly two dollar amounts"
CARD_ORDER_REFUSAL = "a debit-card purchase date is after its posting date"
TRANSACTION_DATE_REFUSAL = (
    "a transaction date is invalid, ambiguous, or outside the statement period"
)


def cma_config(
    *,
    spaxx_isin: str,
    account_status: str = "BeneficialOwner",
    us_tin: str | None = None,
    cma_status: str | None = None,
) -> str:
    """The ``[account]`` and ``[fidelity_cma]`` TOML of ``Tax Payer``'s synthetic
    CMA, with ``account_status``, an ``[account.foreign_tins]`` US entry only
    with ``us_tin``, and a ``[fidelity_cma]`` status only with ``cma_status``.

    The caller passes the sweep fund's real ISIN: the securities guard allows it
    only in each module's own config constant.
    """
    foreign_tins = "" if us_tin is None else f'[account.foreign_tins]\nUS = "{us_tin}"\n\n'
    status = "" if cma_status is None else f'status = "{cma_status}"\n'
    return (
        "[account]\n"
        'number = "U1234567"\n'
        'holder_name = "Tax Payer"\n'
        "opening_date = 2020-01-01\n"
        f'status = "{account_status}"\n'
        "\n"
        f"{foreign_tins}"
        "[fidelity_cma]\n"
        f'account_id = "{_ACCOUNT}"\n'
        f'spaxx_isin = "{spaxx_isin}"\n'
        f"{status}"
    )


def annual_statement() -> CmaStatement:
    """Calendar 2025 in one statement: 10 units at $1 bought on 1 January from a
    $10 opening deposit, and a $5 deposit and a $3 withdrawal on 1 June, which
    leave $2 free credit balance and a $12 ending account value."""
    return CmaStatement(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        account_id=_ACCOUNT,
        account_holder=_HOLDER,
        currency="USD",
        valuation_variant=CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB,
        beginning_account_value_usd=None,
        additions_usd=Decimal("15"),
        subtractions_usd=Decimal("-3"),
        change_in_investment_value_usd=Decimal(0),
        ending_account_value_usd=Decimal("12"),
        accrued_interest_usd=Decimal(0),
        account_value_usd=Decimal("12"),
        free_credit_balance_usd=Decimal("2"),
        position=CmaPosition(
            beginning_market_value_usd=None,
            quantity=Decimal("10"),
            price_per_unit_usd=Decimal(1),
            ending_market_value_usd=Decimal("10"),
            estimated_annual_income_usd=Decimal(0),
            estimated_yield_percent=Decimal(0),
        ),
        core_transactions=(
            CmaCoreTransaction(
                date(2025, 1, 1),
                CmaCoreTransactionKind.BUY,
                Decimal("10"),
                Decimal(1),
                Decimal("10"),
                Decimal("10"),
            ),
        ),
        income_credits=(),
        withholdings=(),
        cash_activity=(
            CmaCashActivity(
                date(2025, 1, 1),
                CmaCashActivityKind.DEPOSIT,
                "opening deposit",
                Decimal("10"),
            ),
            CmaCashActivity(
                date(2025, 6, 1),
                CmaCashActivityKind.DEPOSIT,
                "mixed-day deposit",
                Decimal("5"),
            ),
            CmaCashActivity(
                date(2025, 6, 1),
                CmaCashActivityKind.WITHDRAWAL,
                "mixed-day withdrawal",
                Decimal("-3"),
            ),
        ),
        debit_card_transactions=(),
    )


def monthly_statement(year: int, month: int) -> CmaStatement:
    """One calendar month holding 100 units at $1.0000 throughout, with no
    activity."""
    return CmaStatement(
        period_start=date(year, month, 1),
        period_end=date(year, month, monthrange(year, month)[1]),
        account_id=_ACCOUNT,
        account_holder=_HOLDER,
        currency="USD",
        valuation_variant=CmaValuationVariant.ACCOUNT_VALUE,
        beginning_account_value_usd=Decimal("100.00"),
        additions_usd=Decimal(0),
        subtractions_usd=Decimal(0),
        change_in_investment_value_usd=Decimal(0),
        ending_account_value_usd=Decimal("100.00"),
        accrued_interest_usd=Decimal(0),
        account_value_usd=Decimal("100.00"),
        free_credit_balance_usd=None,
        position=CmaPosition(
            beginning_market_value_usd=Decimal("100.00"),
            quantity=Decimal("100.000"),
            price_per_unit_usd=Decimal("1.0000"),
            ending_market_value_usd=Decimal("100.00"),
            estimated_annual_income_usd=Decimal(0),
            estimated_yield_percent=Decimal(0),
        ),
        core_transactions=(),
        income_credits=(),
        withholdings=(),
        cash_activity=(),
        debit_card_transactions=(),
    )


def core_transaction(
    kind: CmaCoreTransactionKind, day: date, quantity: str, price: str, amount: str
) -> CmaCoreTransaction:
    """A SPAXX row of ``quantity`` units at ``price`` for ``amount`` USD, with a
    zero balance."""
    return CmaCoreTransaction(
        date=day,
        kind=kind,
        quantity=Decimal(quantity),
        price_per_unit_usd=Decimal(price),
        amount_usd=Decimal(amount),
        balance_usd=Decimal(0),
    )


def monthly_history(
    *transactions: CmaCoreTransaction,
    start: date,
    end: date = date(2026, 4, 30),
    price: Decimal,
    carried_cash: Decimal | None = None,
    carry_position_value: bool = False,
) -> tuple[CmaStatement, ...]:
    """Gap-free monthly statements with no income from ``start`` (a partial
    first month if it isn't the 1st) through the month of ``end``, each holding
    at ``price`` the SPAXX units the ``transactions`` leave.

    The first statement carries ``carried_cash`` and no units in; ``None`` is a
    dash beginning account value. Each later one begins at the previous account
    value. With ``carry_position_value``, the position's beginning market value
    is the units carried in (``None`` in the first statement); otherwise it is
    ``None``.
    """
    statements: list[CmaStatement] = []
    held = Decimal(0)
    cash = Decimal(0) if carried_cash is None else carried_cash
    period_start = start
    while period_start <= end:
        period_end = period_start.replace(day=monthrange(period_start.year, period_start.month)[1])
        carried = held if statements else None
        core = tuple(t for t in transactions if period_start <= t.date <= period_end)
        held += sum((t.quantity for t in core), Decimal(0))
        base = monthly_statement(period_start.year, period_start.month)
        statements.append(
            replace(
                base,
                period_start=period_start,
                beginning_account_value_usd=(
                    statements[-1].account_value_usd if statements else carried_cash
                ),
                ending_account_value_usd=held + cash,
                account_value_usd=held + cash,
                position=replace(
                    base.position,
                    beginning_market_value_usd=carried if carry_position_value else None,
                    quantity=held,
                    price_per_unit_usd=price,
                    ending_market_value_usd=held,
                ),
                core_transactions=core,
            )
        )
        period_start = period_end + timedelta(days=1)
    return tuple(statements)


# One withheld SPAXX dividend and one credit-interest row in 2025.
SPAXX_DIVIDEND = CmaIncomeCredit(
    date(2025, 4, 15), CmaIncomeKind.SPAXX_DIVIDEND, Decimal("1.00"), "99971W106"
)
DIVIDEND_WITHHOLDING = CmaWithholding(date(2025, 4, 15), Decimal("-0.25"))
CREDIT_INTEREST = CmaIncomeCredit(
    date(2025, 5, 10), CmaIncomeKind.CREDIT_INTEREST, Decimal("0.50"), None
)


def year_end_summary(
    *,
    dividends_usd: str = "0",
    interest_usd: str = "0",
    withholding_usd: str = "0",
    position: CmaYearEndPosition | None = None,
) -> CmaYearEndIncomeSummary:
    """The 2025 year-end report for ``TAX PAYER``'s CMA with these totals and
    holding."""
    return CmaYearEndIncomeSummary(
        tax_year=2025,
        account_id=_ACCOUNT,
        account_holder=_HOLDER,
        currency="USD",
        dividends_usd=Decimal(dividends_usd),
        interest_usd=Decimal(interest_usd),
        withholding_usd=Decimal(withholding_usd),
        position=position,
    )


def income_year_end(*, position: CmaYearEndPosition | None = None) -> CmaYearEndIncomeSummary:
    """The year-end report whose totals are ``SPAXX_DIVIDEND``,
    ``CREDIT_INTEREST`` and ``DIVIDEND_WITHHOLDING``."""
    return year_end_summary(
        dividends_usd="1.00", interest_usd="0.50", withholding_usd="-0.25", position=position
    )


def session_series(
    ticker: Ticker,
    start: date,
    end: date,
    *,
    fetched_at: datetime,
    price: str = "1",
    overrides: Mapping[date, Decimal] | None = None,
) -> PriceSeries:
    """A synthetic USD close of ``price`` on every US market session from
    ``start`` through ``end``, or the close ``overrides`` gives for that day."""
    closes = {
        day: (overrides or {}).get(day, Decimal(price)) for day in us_market_sessions(start, end)
    }
    return PriceSeries(
        ticker=ticker,
        currency="USD",
        closes=closes,
        has_split_in_range=False,
        source="synthetic",
        fetched_at=fetched_at,
    )


def patch_pdftotext(
    monkeypatch: pytest.MonkeyPatch, texts: Mapping[str, str], *, by_name: bool = False
) -> None:
    """Stub the ``pdftotext -enc UTF-8 -layout <pdf> -`` shell-out
    (``subprocess.run``) to print ``texts[key]``, where ``key`` is the PDF's
    path as the loader passes it or, with ``by_name``, its file name."""

    def pdftotext(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:4] == ["pdftotext", "-enc", "UTF-8", "-layout"]
        assert args[5] == "-"
        key = Path(args[4]).name if by_name else args[4]
        return subprocess.CompletedProcess(args, 0, stdout=texts[key], stderr="")

    monkeypatch.setattr(subprocess, "run", pdftotext)


# --- statement texts ----------------------------------------------------------

# The page furniture every page repeats, as text extraction prints it.
MONTHLY_PAGE_BREAK = f"""\
3 of 6
MR_CE _BNXYZ_BBBBB 0000000
INVESTMENT REPORT
January 1, 2026 - January 31, 2026
Account # {_ACCOUNT}
{_HOLDER} - INDIVIDUAL - TOD
"""


def monthly_statement_text(*, fcb: bool = False) -> str:
    """A synthetic statement in the observed layout: the Account Summary's right
    column (holdings chart, Income Summary) merges into its rows, the Total Core
    Account parenthetical wraps, and a page break splits Core Fund Activity."""
    label = "Net Account" if fcb else "Account"
    aggregate = "107.00" if fcb else "105.00"
    beginning = "102.00" if fcb else "100.00"
    balance_details = ""
    holdings_cash = ""
    if fcb:
        balance_details = """\
Balance Details    This Period    Year-to-Date
Market Value of Holdings    $105.00
Free Credit Balance    $2.00
Ending Net Account Value    $107.00
Free credit balances (FCB) include cash credits from the sale of long positions,
deposits, cash dividends, and interest payments which have not been transferred to
a money market fund or FDIC core position. FCB also includes positions, FCB
and credit balances that exceed the amount required to satisfy your margin
obligations. Refer to the back of your statement for more information.
"""
        holdings_cash = "Credit Balance    2.00\nNet Account Value    $107.00\n"
    return f"""\
INVESTMENT REPORT
January 1, 2026 - January 31, 2026

FIDELITY® CASH MANAGEMENT ACCOUNT {_HOLDER} -
INDIVIDUAL TOD
Account Number: {_ACCOUNT}
Your {label} Value: ${aggregate}
1 of 6
INVESTMENT REPORT
January 1, 2026 - January 31, 2026
Account # {_ACCOUNT}
Account Summary {_HOLDER} - INDIVIDUAL - TOD
{label} Value: ${aggregate}    Account Holdings
Change in Account Value    $5.00
This Period    Year-to-Date
Beginning {label} Value ${beginning} ${beginning}
Additions 10.00 10.00
  Deposits    10.00    10.00
Subtractions -5.00 -5.00
    100% Core Account ($105)
  Withdrawals    -2.00    -2.00
  Cards, Checking & Bill Payments    -2.75    -2.75
    Top Holdings
  Taxes Withheld    -0.25    -0.25    Percent of
Change in Investment Value * 0.00 0.00    Description    Value    Account
Ending {label} Value ${aggregate} ${aggregate}    Fidelity Government Money Market    $105    100%
Accrued Interest (AI) 0.00    Total    $105    100%    S
Ending {label} Value Incl. AI ${aggregate}
    Please note that, due to rounding, percentages may not add to 100%.
{balance_details}MR_CE _BNXYZ_BBBBB 0000000
*    Reflects appreciation or depreciation of your holdings due to price changes, transactions
from Other Activity In or Out and Multi-currency transactions, plus any distribution and \
   Income Summary
income earned during the statement period.
    This Period    Year-to-Date
    Taxable    $1.10    $1.10
    Dividends    1.00    1.00
    Interest    0.10    0.10
    Total    $1.10    $1.10
{MONTHLY_PAGE_BREAK}Holdings {_HOLDER} - INDIVIDUAL - TOD
Core Account
    Beginning    Price    Ending
    Market Value    Quantity    Per Unit    Market Value    EAI ($) /
Description    Jan 1, 2026    Jan 31, 2026    Jan 31, 2026    Jan 31, 2026    EY (%)
FIDELITY GOVERNMENT MONEY $100.00 105.000 $1.0000 $105.00 $4.20
MARKET (SPAXX) 4.000%
-- 7-day yield: 3.95%
Total Core Account (100% of account $100.00 $105.00 $4.20
holdings)
Total Holdings $105.00 $4.20
{holdings_cash}EAI    Estimated Annual Income (EAI) & Estimated Yield (EY) - EAI is an estimate of \
annual income for a specific security position over the next rolling 12 months. EAI may be \
negative on short
& EY    positions. EY is calculated by dividing the current EAI for a security position by its \
statement closing date market value. EAI and EY are estimates only and may include return of \
principal
and/or capital gains, which would render them overstated. Actual income and yield might be \
lower or higher than the estimated amounts. For calculation details, refer to the
"Additional Information and Endnotes" section.
Activity {_HOLDER} - INDIVIDUAL - TOD
Dividends, Interest & Other Income
(Includes dividend reinvestment)
Settlement    Symbol/
Date Security Name CUSIP Description Quantity Price Amount
01/10 CREDIT INTEREST - - $0.10
01/31 FIDELITY GOVERNMENT MONEY 31617H102 Dividend Received - - $1.00
MARKET
Total Dividends, Interest & Other Income $1.10
Deposits
Date Reference Description Amount
01/15 PAYROLL DEPOSIT $10.00
Total Deposits $10.00
Withdrawals
Date Reference Description Amount
01/20 ACH PAYMENT -$2.00
Total Withdrawals -$2.00
Exchanges In
Symbol/
Date Security Name CUSIP Description Quantity Price Amount
01/16 Z99-000001-0 Transferred From - - $1.00
Total Exchanges In $1.00
Exchanges Out
Symbol/
Date Security Name CUSIP Description Quantity Price Amount
01/17 Z99-000001-0 Transferred To - - -$1.00
Total Exchanges Out -$1.00
Taxes Withheld
Date Security Description Amount
01/31 FIDELITY GOVERNMENT MONEY MARKET Non-Resident Tax -$0.25
Total Federal Taxes Withheld -0.25
Total Taxes Withheld -$0.25
Core Fund Activity
For more information about the operation of your core account, please refer to your \
Customer Agreement.
Settlement    Account
Date Type Transaction Description Quantity Price Amount Balance
01/15 CASH You Bought FIDELITY GOVERNMENT MONEY MARKET 4.250 $1.0000 $4.25 $104.25
MR_CE _BNXYZ_BBBBB 0000000
MORNING TRADE @ 1
{MONTHLY_PAGE_BREAK}Core Fund Activity (continued)
Date Type Transaction Description Quantity Price Amount Balance
01/31 CASH Reinvestment FIDELITY GOVERNMENT MONEY MARKET 0.750 $1.0000 $0.75 $105.00
REINVEST @ $1.000    S
Total Core Fund Activity $5.00
Debit Card Summary    Debit Card Activity
Beginning Balance    $0.00    Fidelity® Debit Card (*0000)
Total Purchases    -3.00
TRANSACTIONS
Total Payments    0.00
Ending Balance    $0.00    Trans. Date Post Date    Location Reference/Description    Amount
Purchases
01/21 01/22 TEST MERCHANT -$3.00
Total Purchases -$3.00
Fidelity® Debit Card (*0000) Subtotal -$3.00
Total Debit Card Activity -$3.00
S

Brokerage services provided by Fidelity Brokerage Services LLC.
Brokerage accounts carried by National Financial Services LLC.
"""


YEAR_END_TEXT = f"""\
2025 YEAR-END INVESTMENT REPORT
January 1, 2025 - December 31, 2025
FIDELITY® CASH MANAGEMENT ACCOUNT {_HOLDER} -
INDIVIDUAL TOD
Account Number: {_ACCOUNT}
Account # {_ACCOUNT}
Account Summary {_HOLDER} - INDIVIDUAL - TOD
Dividends 1.00
Interest 0.50
Taxes Withheld -0.25
Holdings
Core Account
    Price    Total
Description    Quantity    Per Unit    Market Value    Income Earned
FIDELITY GOVERNMENT MONEY 10.000 $1.0000 $10.00 $0.50
MARKET (SPAXX)
-- 7-day yield: 3.95%
2 of 4
MR_CE _BNXYZ_BBBBB 0000000
2025 YEAR-END INVESTMENT REPORT
January 1, 2025 - December 31, 2025
Account # {_ACCOUNT}
{_HOLDER} - INDIVIDUAL - TOD
Core Account (continued)
Total Core Account (100% of account $10.00 $0.50
holdings)
Total Holdings $10.00 $0.50
Brokerage services provided by Fidelity Brokerage Services LLC.
Brokerage accounts carried by National Financial Services LLC.
"""


def parse_monthly_text(text: str) -> CmaStatement:
    """``text`` parsed as a USD monthly statement of ``_ACCOUNT``."""
    return parse_statement(
        text,
        expected_currency="USD",
        expected_account_id=_ACCOUNT,
    )
