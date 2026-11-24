"""Parse the observed Fidelity Cash Management Account monthly statements.

The parser is intentionally limited to the two monthly valuation variants
documented by ADR 0004. Year-end reports, securities other than SPAXX, and
FDIC/Program Bank sweep positions refuse rather than being guessed.

Each row is read whole, and only in its own section. Every line of the
sections below must be one of the shapes listed, a blank line or page
furniture, else it refuses. Page furniture is what each page repeats: the
report title and its period line, ``Account #``, the holder's ``- INDIVIDUAL -
TOD`` registration (alone or after ``Holdings`` or ``Activity``), the page
number (``<n> of <n>``), the page's mailing code and a lone margin ``S``.
Rows, totals and the fixed lines below may also end in a margin ``S``. ``…``
stands for ``Account`` or, in the Free Credit Balance (FCB) layout, ``Net
Account``. Text extraction merges the statement's two columns, so the shapes
below are what it prints; wording other than labels is Fidelity's fixed text.

- **Account Summary**, from its heading to ``Holdings``. Read: ``… Value:``,
  ``Accrued Interest (AI)`` and ``Ending … Value Incl. AI`` with one amount;
  ``Beginning … Value``, ``Additions``, ``Subtractions``, ``Change in
  Investment Value *`` and ``Ending … Value`` with a This Period amount or
  dash and a Year-to-Date amount or dash. Each is printed once; the
  Accrued Interest amount may be printed at the start of the line above its
  label. The
  FCB layout's Balance Details also give ``Market Value of Holdings`` and
  ``Free Credit Balance``, read only here. Read by shape only: ``Change in
  Account Value``, ``This Period Year-to-Date``, the Deposits, Withdrawals,
  Exchanges, ``Cards, Checking & Bill Payments`` and Taxes Withheld detail
  rows, the Income Summary, the holdings chart and legend, the appreciation
  footnote, the rounding note, the trusted-contact notice and, in the FCB
  layout, the Balance Details' ending value, a ``Credit Balance`` line and
  the FCB note. Any line may end in a right-hand chart, legend or Income
  Summary item. The cover's ``Your … Value:`` line is read on its own and
  must match.
- **Holdings**, from its heading to ``Activity``. Read: the SPAXX row and
  its ``MARKET (SPAXX) <yield>%`` line; ``Total Core Account (<n>% of
  account`` with two dollar amounts (ending value, income) or three (a
  beginning value, ``unavailable`` when the SPAXX row's is), then
  ``holdings)`` as the next line; ``Total Holdings`` with two; in the FCB
  layout, ``Net Account Value`` after ``Total Holdings``, read only here.
  Read by shape only: ``Core Account``, the three-line column header, the
  yield line, the EAI note and, in the FCB layout, a ``Credit Balance``
  line.
- **Activity sections**, each from its heading to its total. Its column header
  must come before its first row and may repeat with ``<section>
  (continued)`` on later pages:

  - ``Dividends, Interest & Other Income``: ``(Includes dividend
    reinvestment)``, ``Settlement Symbol/``, ``Date Security Name CUSIP
    Description Quantity Price Amount``; credit-interest rows, and SPAXX
    dividend rows continued on a ``MARKET`` line.
  - ``Deposits`` and ``Withdrawals``: ``Date Reference Description Amount``.
    A row may wrap its description onto the next line: text with no ``$`` or
    decimal number. Registration lines remain page furniture.
  - ``Exchanges In`` and ``Exchanges Out``: ``Symbol/``, then the income
    section's column header; a dash as both Quantity and Price.
  - ``Taxes Withheld``: ``Date Security Description Amount``; SPAXX
    ``Non-Resident Tax`` rows and the ``Total Federal Taxes Withheld`` line.
  - ``Core Fund Activity``: the core-account note, ``Settlement Account``,
    ``Date Type Transaction Description Quantity Price Amount Balance``;
    SPAXX buys, sells and reinvestments, each followed by its second line
    (``MORNING TRADE @ <n>``, ``@<n>`` or ``REINVEST @ $<n>.<nnn>``).
  - ``Debit Card Summary Debit Card Activity``: the ``TRANSACTIONS``
    subheading before the rows, which have a purchase (``Trans. Date``) and
    a posting date. The posting date is in the statement period, and the
    purchase date is on or before it. ``Total Purchases`` and the card's
    ``Subtotal`` must equal the rows' sum; the card's balance summary is read
    by shape only.

  A description is free text, so a number at its end is part of it.
- **Dates**: every supported activity row starts ``MM/DD``. A credit-interest
  date and a card purchase date are in the period or, on a statement that
  isn't the account's opening statement, in the calendar month before it. The
  opening statement (a partial first month or a dash beginning value) keeps
  them inside its period. Every other row date is in the period. Apart from
  the period line under each report title, a line anywhere in the statement
  that starts with ``MM/DD`` or ``MM/DD/YYYY``, ``YYYY-MM-DD``,
  ``DD-Mon-YYYY``, ``DD.MM.YYYY``, or a month name or three-letter
  abbreviation and a day number refuses unless a supported row reads it.
  Year-end reports have no such rows.

The year-end report parser reads ``Dividends``, ``Interest`` and ``Taxes
Withheld`` in its Account Summary and reads none of its other lines. A line
starting with ``Dividends`` or ``Taxes Withheld`` must be that row whole; a
line with ``Interest`` before a value must be ``Interest <amount>`` or
``Ending Account Value as of Dec 31, <year> $<amount> Interest <amount>``. Its
Holdings, from the heading to ``Total Holdings``, hold ``Core Account`` (also
``(continued)``), its two-line column header, the SPAXX row with its ``MARKET
(SPAXX)`` line, the yield line, and ``Total Core Account`` (wrapped as
above) and ``Total Holdings`` with two dollar amounts each.
"""

from __future__ import annotations

import re
from calendar import monthrange
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, DecimalException, InvalidOperation
from enum import StrEnum
from functools import wraps
from itertools import pairwise
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import ConfigError, IbkrFaError, ParseError
from ibkr_fa.models import (
    DistributionKind,
    Dividend,
    FidelityCmaSourceRecord,
    InterestCredit,
    WithholdingTax,
)
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._fidelity_text import FULL_MONTHS as _MONTHS
from ibkr_fa.parser._fidelity_text import MONEY_DOLLAR_OPTIONAL as _MONEY
from ibkr_fa.parser._fidelity_text import MONEY_DOLLAR_REQUIRED as _DOLLAR_MONEY
from ibkr_fa.parser._fidelity_text import PRICE_DOLLAR_OPTIONAL as _PRICE
from ibkr_fa.parser._fidelity_text import WHOLE_NUMBER as _UNSIGNED
from ibkr_fa.parser._fidelity_text import YEARLESS_DATE as _MM_DD
from ibkr_fa.parser._fidelity_text import FullDateFailure, full_date, yearless_dates
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.path_checks import is_dir
from ibkr_fa.types import ISIN_PATTERN, Isin, Ticker

_SECTION: Final[str] = "Fidelity CMA monthly statement"
_PDF_SOURCE: Final[str] = "Fidelity CMA statement PDF"
_SPAXX_CUSIP: Final[str] = "31617H102"
_SPAXX_SYMBOL: Final[Ticker] = Ticker("SPAXX")
_QTY_HALF_ULP: Final[Decimal] = Decimal("0.0005")

_VALUE: Final[str] = rf"(?:{_MONEY}|-)"
_QUANTITY: Final[str] = rf"-?{_UNSIGNED}\.[0-9]{{3}}"
_DATE: Final[str] = rf"(?:{'|'.join(_MONTHS)}) [0-9]{{1,2}}, [0-9]{{4}}"

_REPORT_RE: Final = re.compile(
    rf"^[ \t\f]*INVESTMENT REPORT[ \t]*\n"
    rf"[ \t]*(?P<start>{_DATE})[ \t]*-[ \t]*(?P<end>{_DATE})[ \t]*$",
    re.MULTILINE,
)
_REPORT_LABEL_RE: Final = re.compile(r"^[ \t\f]*INVESTMENT REPORT[ \t]*$", re.MULTILINE)
_YEAR_END_REPORT_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<year>[0-9]{{4}}) YEAR-END INVESTMENT REPORT[ \t]*\n"
    rf"[ \t]*(?P<start>{_DATE})[ \t]*-[ \t]*(?P<end>{_DATE})[ \t]*$",
    re.MULTILINE,
)
_YEAR_END_LABEL_RE: Final = re.compile(
    r"^[ \t\f]*[0-9]{4} YEAR-END INVESTMENT REPORT[ \t]*$", re.MULTILINE
)
_MAIN_ID_RE: Final = re.compile(
    r"^[ \t\f]*FIDELITY® CASH MANAGEMENT ACCOUNT[ \t]+(?P<holder>.+?)[ \t]+-[ \t]*\n"
    r"[ \t]*INDIVIDUAL TOD[ \t]*\n"
    r"[ \t]*Account Number:[ \t]*(?P<account>[A-Z][0-9]{2}-[0-9]{6})[ \t]*$",
    re.MULTILINE,
)
_SHORT_ACCOUNT_RE: Final = re.compile(
    r"^[ \t\f]*Account #[ \t]*([A-Z][0-9]{2}-[0-9]{6})[ \t]*$", re.MULTILINE
)
_SUMMARY_HOLDER_RE: Final = re.compile(
    r"^[ \t\f]*Account Summary[ \t]+(.+?)[ \t]+-[ \t]+INDIVIDUAL[ \t]+-[ \t]+TOD[ \t]*$",
    re.MULTILINE,
)
_FBS_NFS_RE: Final = re.compile(
    r"Brokerage services provided by Fidelity Brokerage Services LLC.*?"
    r"Brokerage accounts carried by National Financial Services LLC",
    re.DOTALL,
)
_HOLDINGS_HEADING_RE: Final = re.compile(r"^[ \t\f]*Holdings(?:[ \t]+.*)?$", re.MULTILINE)
_ACTIVITY_HEADING_RE: Final = re.compile(r"^[ \t\f]*Activity(?:[ \t]+.*)?$", re.MULTILINE)
_LINE_RE: Final = re.compile(r"^.*$", re.MULTILINE)


def _words(text: str) -> str:
    """``text`` as fixed wording: its words in order, any spaces between them."""
    return r"[ \t]+".join(map(re.escape, text.split()))


def _lines(*bodies: str) -> re.Pattern[str]:
    """Whole lines that are one of ``bodies``, with an optional margin ``S``."""
    return re.compile(rf"^[ \t\f]*(?:{'|'.join(bodies)})(?:[ \t]+S)?[ \t]*$", re.MULTILINE)


# Page furniture printed anywhere: a page number, the page's mailing code and a
# lone margin ``S``.
_FURNITURE_RE: Final = _lines(
    r"[0-9]+ of [0-9]+", r"[A-Z]+_[A-Z]+[ \t]+_[A-Z]+_[A-Z]+[ \t]+[0-9]+", "S"
)
_PageHeaders = tuple[re.Pattern[str], ...]


def _page_headers(report: re.Pattern[str], holder: str) -> _PageHeaders:
    """What each page repeats: its report title and period, ``Account #``, the
    holder's registration (alone or after a section title) and the furniture."""
    registration = re.compile(
        rf"^[ \t\f]*(?:(?:Holdings|Activity)[ \t]+)?{_words(holder)}"
        r"[ \t]+-[ \t]+INDIVIDUAL[ \t]+-[ \t]+TOD[ \t]*$",
        re.MULTILINE,
    )
    return (report, _SHORT_ACCOUNT_RE, registration, _FURNITURE_RE)


def _check_section_heading(
    match: re.Match[str], section: str, pages: _PageHeaders, *, year_end: bool = False
) -> None:
    if _lines(_words(section)).fullmatch(match.group()) is None and not any(
        pattern.fullmatch(match.group()) for pattern in pages
    ):
        prefix = "year-end " if year_end else ""
        raise _fail(f"{prefix}{section} section heading is malformed", field=section)


_MONTH_WORDS: Final[str] = "|".join([*_MONTHS, *(month[:3] for month in _MONTHS), "Sept"])
# A line starting with a date in any of these forms: MM/DD (or MM/DD/YYYY),
# YYYY-MM-DD, DD-Mon-YYYY, DD.MM.YYYY, or a month name or abbreviation and a day.
_DATED_LINE_RE: Final = re.compile(
    rf"^[ \t\f]*(?:[0-9]{{1,2}}/[0-9]{{1,2}}"
    rf"|[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}"
    rf"|[0-9]{{1,2}}-(?:{_MONTH_WORDS})-[0-9]{{2,4}}"
    rf"|[0-9]{{1,2}}\.[0-9]{{1,2}}\.[0-9]{{2,4}}"
    rf"|(?:{_MONTH_WORDS})\.?[ \t]+[0-9]{{1,2}})\b",
    re.MULTILINE | re.IGNORECASE,
)

_SPAXX_POSITION_RE: Final = re.compile(
    rf"^[ \t\f]*FIDELITY GOVERNMENT MONEY[ \t]+"
    rf"(?P<begin>unavailable|{_DOLLAR_MONEY})[ \t]+"
    rf"(?P<quantity>{_QUANTITY})[ \t]+(?P<price>\${_UNSIGNED}\.[0-9]{{4}})[ \t]+"
    rf"(?P<ending>{_DOLLAR_MONEY})[ \t]+(?P<eai>{_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_SPAXX_SYMBOL_RE: Final = re.compile(
    r"^[ \t\f]*MARKET[ \t]+\(SPAXX\)[ \t]+(?P<yield>-?[0-9]+\.[0-9]{3})%"
    r"(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_YEAR_END_SPAXX_POSITION_RE: Final = re.compile(
    rf"^[ \t\f]*FIDELITY GOVERNMENT MONEY[ \t]+"
    rf"(?P<quantity>{_QUANTITY})[ \t]+(?P<price>\${_UNSIGNED}\.[0-9]{{4}})[ \t]+"
    rf"(?P<ending>{_DOLLAR_MONEY})[ \t]+(?P<income>{_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*\n"
    r"[ \t\f]*MARKET[ \t]+\(SPAXX\)(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_TOTAL_HOLDINGS_LINE_RE: Final = re.compile(r"^[ \t\f]*Total Holdings\b.*$", re.MULTILINE)
_TOTAL_CORE_LINE_RE: Final = re.compile(r"^[ \t\f]*Total Core Account\b.*$", re.MULTILINE)
_TOTAL_HOLDINGS_RE: Final = re.compile(
    rf"^[ \t\f]*Total Holdings[ \t]+(?P<ending>{_DOLLAR_MONEY})[ \t]+"
    rf"(?P<income>{_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
# The parenthetical wraps: ``(<n>% of account`` precedes the amounts on the
# first line; ``holdings)`` is the whole next line.
_TOTAL_CORE_RE: Final = re.compile(
    rf"^[ \t\f]*Total Core Account[ \t]+\((?P<limit_rate_core_total>[0-9]+)% of account"
    rf"(?:[ \t]+(?P<beginning>{_DOLLAR_MONEY}|unavailable))?[ \t]+"
    rf"(?P<ending>{_DOLLAR_MONEY})[ \t]+(?P<income>{_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*\n"
    r"[ \t\f]*holdings\)(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_MONTH_DATE: Final[str] = (
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{1,2}, [0-9]{4}"
)
_YIELD: Final[str] = (
    r"--[ \t]+(?:[0-9]+-day[ \t]+yield:[ \t]+"
    r"(?P<limit_rate_yield>-?[0-9]+\.[0-9]{1,2})%|not available)"
)
_EAI_NOTE: Final = (
    rf"EAI[ \t]+{_words('Estimated Annual Income (EAI) & Estimated Yield (EY) - EAI is an')}"
    rf"[ \t]+{_words('estimate of annual income for a specific security position over the')}"
    rf"[ \t]+{_words('next rolling')}[ \t]+[0-9]+"
    rf"[ \t]+{_words('months. EAI may be negative on short')}",
    rf"&[ \t]+EY[ \t]+{_words('positions. EY is calculated by dividing the current EAI for a')}"
    rf"[ \t]+{_words('security position by its statement closing date market value. EAI and')}"
    rf"[ \t]+{_words('EY are estimates only and may include return of principal')}",
    _words(
        "and/or capital gains, which would render them overstated. Actual income and yield "
        "might be lower or higher than the estimated amounts. For calculation details, "
        "refer to the"
    ),
    _words('"Additional Information and Endnotes" section.'),
)
# Holdings lines read by shape only: headings, the column header, the core
# fund's yield and the EAI note.
_HOLDINGS_TEXT_RE: Final = _lines(
    _words("Core Account"),
    _words("Core Account (continued)"),
    _words("Beginning Price Ending"),
    _words("Market Value Quantity Per Unit Market Value EAI ($) /"),
    rf"Description(?:[ \t]+{_MONTH_DATE}){{4}}[ \t]+EY[ \t]+\(%\)",
    _YIELD,
    *_EAI_NOTE,
)
_YEAR_END_HOLDINGS_TEXT_RE: Final = _lines(
    _words("Core Account"),
    _words("Core Account (continued)"),
    _words("Price Total"),
    _words("Description Quantity Per Unit Market Value Income Earned"),
    _YIELD,
)
_NET_ACCOUNT_VALUE_RE: Final = re.compile(
    rf"^[ \t\f]*Net Account Value[ \t]+(?P<amount>{_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_STRUCTURAL_FDIC_RE: Final = re.compile(
    r"^[ \t\f]*(?:FDIC DEPOSIT SWEEP|PROGRAM BANK)\b", re.MULTILINE
)

_CORE_ROW_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<date>{_MM_DD})[ \t]+CASH[ \t]+"
    rf"(?P<kind>You Bought|You Sold|Reinvestment)[ \t]+"
    rf"FIDELITY GOVERNMENT MONEY MARKET[ \t]+"
    rf"(?P<quantity>{_QUANTITY})[ \t]+(?P<price>{_PRICE})[ \t]+"
    rf"(?P<amount>{_MONEY})[ \t]+(?P<balance>{_MONEY})"
    rf"(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_INCOME_ROW_RE: Final = re.compile(
    rf"(?:^[ \t\f]*(?P<div_date>{_MM_DD})[ \t]+FIDELITY GOVERNMENT MONEY[ \t]+"
    rf"(?P<cusip>{_SPAXX_CUSIP})[ \t]+Dividend Received[ \t]+-[ \t]+-[ \t]+"
    rf"(?P<div_amount>{_MONEY})(?:[ \t]+S)?[ \t]*\n[ \t\f]*MARKET(?:[ \t]+S)?[ \t]*$)"
    rf"|(?:^[ \t\f]*(?P<int_date>{_MM_DD})[ \t]+CREDIT INTEREST[ \t]+-[ \t]+-[ \t]+"
    rf"(?P<int_amount>{_MONEY})(?:[ \t]+S)?[ \t]*$)",
    re.MULTILINE,
)
_WHT_ROW_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<date>{_MM_DD})[ \t]+FIDELITY GOVERNMENT MONEY MARKET[ \t]+"
    rf"Non-Resident Tax[ \t]+(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_CASH_ROW_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<date>{_MM_DD})[ \t]+(?P<description>.+?)[ \t]+"
    rf"(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_EXCHANGE_ROW_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<date>{_MM_DD})[ \t]+(?P<description>.+?)[ \t]+(?P<quantity>-)[ \t]+"
    rf"(?P<price>-)[ \t]+(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_FEDERAL_WHT_RE: Final = re.compile(
    rf"^[ \t\f]*Total Federal Taxes Withheld[ \t]+(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_YEAR_END_INTEREST_RE: Final = re.compile(
    rf"[ \t\f]*(?:Ending Account Value as of Dec 31, (?P<year>[0-9]{{4}})[ \t]+"
    rf"(?P<limit_money_year_end_value>{_DOLLAR_MONEY})[ \t]+)?"
    rf"Interest[ \t]+(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*"
)
# ``Interest`` before a value anywhere in a line; the line must be a whole
# ``_YEAR_END_INTEREST_RE`` row.
_YEAR_END_INTEREST_LINE_RE: Final = re.compile(r"\bInterest[ \t]+[-$0-9]")
_CARD_ROW_RE: Final = re.compile(
    rf"^[ \t\f]*(?P<transaction_date>{_MM_DD})[ \t]+(?P<post_date>{_MM_DD})[ \t]+"
    rf"(?P<description>.+?)[ \t]+(?P<amount>{_MONEY})(?:[ \t]+S)?[ \t]*$",
    re.MULTILINE,
)
_CREDIT_BALANCE: Final[str] = rf"Credit Balance[ \t]+(?P<limit_money_credit_balance>{_MONEY})"
# A core fund row's second line.
_CORE_CONTINUATION_RE: Final = _lines(
    r"MORNING TRADE[ \t]+@[ \t]+(?P<limit_price_morning>[0-9]+)",
    r"@(?P<limit_price_continuation>[0-9]+)",
    r"REINVEST[ \t]+@[ \t]+(?P<limit_price_reinvest>\$[0-9]+\.[0-9]{3})",
)
# Wrapped descriptions have no dollar sign or decimal number; registrations are furniture.
_CASH_CONTINUATION_RE: Final = re.compile(
    r"^[ \t\f]*(?![^\n]*\$)(?![^\n]*[0-9]\.[0-9])"
    r"(?![^\n]*[ \t]-[ \t]+INDIVIDUAL[ \t]+-[ \t]+TOD[ \t]*$)"
    r"[A-Za-z][^\n]*$",
    re.MULTILINE,
)
_ACTIVITY_COLUMNS: Final[str] = "Date Security Name CUSIP Description Quantity Price Amount"
_CARD: Final[str] = rf"{_words('Fidelity® Debit Card')}[ \t]+\([^()\s]*[0-9][^()\s]*\)"
_CARD_COLUMNS: Final[str] = "Trans. Date Post Date Location Reference/Description Amount"

# The Account Summary prints its holdings chart, legend and Income Summary in a
# right-hand column, which text extraction merges into the summary's lines.
_RIGHT_COLUMN: Final[str] = "|".join(
    (
        _words("Account Holdings"),
        _words("Percent of"),
        _words("Description Value Account"),
        rf"{_words('Fidelity Government Money Market')}[ \t]+"
        r"(?P<limit_money_chart>\$[0-9,]+)[ \t]+(?P<limit_rate_chart>[0-9]+)%",
        r"Total[ \t]+(?P<limit_money_chart_total>\$[0-9,]+)[ \t]+"
        r"(?P<limit_rate_chart_total>[0-9]+)%",
        rf"{_words('Please note that, due to rounding, percentages may not add to')}"
        r"[ \t]+[0-9]+%\.",
        _words("Income Summary"),
        _words("This Period Year-to-Date"),
        rf"(?:Taxable|Dividends|Interest|Total)[ \t]+"
        rf"(?P<limit_money_income_current>{_VALUE})[ \t]+(?P<limit_money_income_ytd>{_VALUE})",
    )
)
# Account Summary lines read by shape only: the period columns' header, the
# Additions and Subtractions detail rows, the Income Summary, the holdings chart
# and the footnotes.
_SUMMARY_TEXT: Final = (
    rf"Change in Account Value[ \t]+(?P<limit_money_account_change>{_MONEY})",
    _words("Top Holdings"),
    r"(?P<limit_rate_core_chart>[0-9]+)%[ \t]+Core Account[ \t]+"
    r"\((?P<limit_money_core_chart>\$[0-9,]+)\)",
    rf"(?:Deposits|Withdrawals|Exchanges In|Exchanges Out|Taxes Withheld|"
    rf"{_words('Cards, Checking & Bill Payments')})[ \t]+"
    rf"(?P<limit_money_detail_current>{_VALUE})[ \t]+(?P<limit_money_detail_ytd>{_VALUE})",
    rf"\*[ \t]+{_words('Reflects appreciation or depreciation of your holdings due to')}"
    rf"[ \t]+{_words('price changes, transactions')}",
    _words(
        "from Other Activity In or Out and Multi-currency transactions, plus any distribution and"
    ),
    _words("income earned during the statement period."),
    rf"{_words('In order to comply with')}[ \t]+[A-Z]+[ \t]+Rule[ \t]+[0-9]+,"
    rf"[ \t]+{_words('Fidelity is asking clients to review')}",
    _words("or update trusted contact information. To learn more, go to"),
    rf"{_words('www.fidelity.com/trustedcontact or contact a representative.')}"
    r"[ \t]+[^ \t\n]*[0-9][^ \t\n]*",
)
# Lines only the Free Credit Balance layout prints: its Balance Details block
# and the Free Credit Balance note.
_FCB_SUMMARY_TEXT: Final = (
    _words("Balance Details"),
    rf"Ending Net Account Value[ \t]+(?P<limit_money_net_value>{_DOLLAR_MONEY})",
    _CREDIT_BALANCE,
    rf"{_words('Free credit balances')}[ \t]+\([A-Z]+\)[ \t]+"
    rf"{_words('include cash credits from the sale of long positions,')}",
    _words("deposits, cash dividends, and interest payments which have not been transferred to"),
    rf"{_words('a money market fund or FDIC core position.')}[ \t]+[A-Z]+[ \t]+"
    rf"{_words('also includes positions,')}[ \t]+[A-Z]+",
    _words("and credit balances that exceed the amount required to satisfy your margin"),
    _words("obligations. Refer to the back of your statement for more information."),
)


class CmaValuationVariant(StrEnum):
    ACCOUNT_VALUE = "account-value"
    NET_ACCOUNT_VALUE_WITH_FCB = "net-account-value-with-free-credit-balance"


class CmaCoreTransactionKind(StrEnum):
    BUY = "You Bought"
    SELL = "You Sold"
    REINVESTMENT = "Reinvestment"


class CmaIncomeKind(StrEnum):
    SPAXX_DIVIDEND = "SPAXX dividend"
    CREDIT_INTEREST = "Credit Interest"


class CmaCashActivityKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER_IN = "transfer in"
    TRANSFER_OUT = "transfer out"


@dataclass(frozen=True, slots=True, repr=False)
class CmaPosition:
    beginning_market_value_usd: Decimal | None
    quantity: Decimal
    price_per_unit_usd: Decimal
    ending_market_value_usd: Decimal
    estimated_annual_income_usd: Decimal
    estimated_yield_percent: Decimal
    symbol: str = "SPAXX"
    description: str = "FIDELITY GOVERNMENT MONEY MARKET"


@dataclass(frozen=True, slots=True, repr=False)
class CmaCoreTransaction:
    date: date
    kind: CmaCoreTransactionKind
    quantity: Decimal
    price_per_unit_usd: Decimal
    amount_usd: Decimal
    balance_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class CmaIncomeCredit:
    date: date
    kind: CmaIncomeKind
    amount_usd: Decimal
    cusip: str | None


@dataclass(frozen=True, slots=True, repr=False)
class CmaWithholding:
    date: date
    amount_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class CmaCashActivity:
    date: date
    kind: CmaCashActivityKind
    description: str
    amount_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class CmaDebitCardTransaction:
    transaction_date: date
    post_date: date
    description: str
    amount_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class CmaStatement:
    period_start: date
    period_end: date
    account_id: str
    account_holder: str
    currency: str
    valuation_variant: CmaValuationVariant
    beginning_account_value_usd: Decimal | None
    additions_usd: Decimal
    subtractions_usd: Decimal
    change_in_investment_value_usd: Decimal
    ending_account_value_usd: Decimal
    accrued_interest_usd: Decimal
    account_value_usd: Decimal
    free_credit_balance_usd: Decimal | None
    position: CmaPosition
    core_transactions: tuple[CmaCoreTransaction, ...]
    income_credits: tuple[CmaIncomeCredit, ...]
    withholdings: tuple[CmaWithholding, ...]
    cash_activity: tuple[CmaCashActivity, ...]
    debit_card_transactions: tuple[CmaDebitCardTransaction, ...]


@dataclass(frozen=True, slots=True, repr=False)
class CmaYearEndIncomeSummary:
    tax_year: int
    account_id: str
    account_holder: str
    currency: str
    dividends_usd: Decimal
    interest_usd: Decimal
    withholding_usd: Decimal
    position: CmaYearEndPosition | None = None


@dataclass(frozen=True, slots=True, repr=False)
class CmaYearEndPosition:
    """Dec-31 SPAXX holding corroborated by the year-end report."""

    quantity: Decimal
    price_per_unit_usd: Decimal
    market_value_usd: Decimal
    income_earned_usd: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class _Summary:
    variant: CmaValuationVariant
    beginning: Decimal | None
    additions: Decimal
    subtractions: Decimal
    change: Decimal
    ending: Decimal
    accrued_interest: Decimal
    aggregate: Decimal
    market_value: Decimal | None
    free_credit_balance: Decimal | None


@dataclass(frozen=True, slots=True, repr=False)
class _ListedSection:
    rows: tuple[re.Match[str], ...]
    total: Decimal
    lines: dict[str, list[re.Match[str]]]


def _fail(message: str, *, field: str | None = None) -> ParseError:
    return ParseError(
        section=_SECTION,
        row_index=0,
        column=field,
        message=message,
        raw_value="",
    )


def _decimal_errors[T, **P](parser: Callable[P, T]) -> Callable[P, T]:
    """Normalize arithmetic failures at both statement parser boundaries."""

    @wraps(parser)
    def checked(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return parser(*args, **kwargs)
        except DecimalException as exc:
            raise _fail(
                "statement contains monetary values outside supported arithmetic range"
            ) from exc

    return checked


def _money(raw: str, *, kind: NumberKind = NumberKind.MONEY) -> Decimal:
    try:
        value = Decimal(raw.replace("$", "").replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - regex pre-validates
        raise _fail("statement contains a malformed monetary value") from exc
    if not value.is_finite():
        raise _fail("statement contains a non-finite monetary value")
    breach = number_limit_breach(value, kind)
    if breach is not None:
        raise _fail(breach)
    return value


def _quantity_tolerance(price: Decimal) -> Decimal:
    """How far quantity times ``price`` may sit from its printed amount: half a
    unit in the quantity's third decimal place times ``price``, plus half a cent."""
    return _QTY_HALF_ULP * price + Decimal("0.005")


_LIMIT_KINDS: Final = {
    "money": NumberKind.MONEY,
    "price": NumberKind.PRICE,
    "rate": NumberKind.RATE,
}


def _check_unused_numbers(row: re.Match[str]) -> None:
    """Apply limits to recognized numeric cells whose values are not retained."""
    for name, raw in row.groupdict().items():
        if name.startswith("limit_") and raw not in {None, "-"}:
            _money(raw, kind=_LIMIT_KINDS[name.split("_", 2)[1]])


def _only(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise _fail(f"expected exactly one {what}; found {len(matches)}")
    return matches[0]


def _consistent(values: Iterable[Decimal], what: str) -> Decimal:
    values = tuple(values)
    if not values or len(set(values)) != 1:
        raise _fail(f"{what} is missing or inconsistent across the statement")
    return values[0]


def _full_date(raw: str) -> date:
    parsed = full_date(raw)
    if parsed is FullDateFailure.MALFORMED:
        raise _fail("statement contains a malformed period date")
    if parsed is FullDateFailure.NOT_A_CALENDAR_DATE:
        raise _fail("statement contains an invalid period date")
    return parsed


def _transaction_date(
    raw: str, start: date, end: date, *, allow_previous_month: bool = False
) -> date:
    month, day = (int(part) for part in raw.split("/"))
    try:
        previous_month_end = start - timedelta(days=1)
    except OverflowError as exc:
        raise _fail(
            "a transaction date is invalid, ambiguous, or outside the statement period"
        ) from exc
    earliest = previous_month_end.replace(day=1) if allow_previous_month else start
    candidates = yearless_dates(month, day, {earliest.year, start.year, end.year}, earliest, end)
    if len(candidates) != 1:
        raise _fail("a transaction date is invalid, ambiguous, or outside the statement period")
    return candidates[0]


def _opening_statement(start: date, beginning: Decimal | None) -> bool:
    """Whether the statement is the account's opening statement: a partial
    first month or a dash beginning value, which a bundle allows only on its
    earliest statement (ADR 0004 §4)."""
    return start.day != 1 or beginning is None


def _parse_identity(text: str, expected_account_id: str | None) -> tuple[date, date, str, str]:
    if "YEAR-END INVESTMENT REPORT" in text:
        raise _fail("year-end reports are not supported by the monthly statement parser")
    reports = list(_REPORT_RE.finditer(text))
    if len(reports) != len(_REPORT_LABEL_RE.findall(text)) or not reports:
        raise _fail("monthly report header is missing or malformed")
    periods = {(match.group("start"), match.group("end")) for match in reports}
    if len(periods) != 1:
        raise _fail("report period is inconsistent across pages")
    start_raw, end_raw = periods.pop()
    period_start, period_end = _full_date(start_raw), _full_date(end_raw)
    if period_start > period_end:
        raise _fail("statement period starts after it ends")
    if (
        period_start.year != period_end.year
        or period_start.month != period_end.month
        or period_end.day != monthrange(period_end.year, period_end.month)[1]
    ):
        raise _fail("monthly statement period must end on that calendar month's final day")
    account_id, holder = _account_identity(
        text,
        expected_account_id,
        inconsistent_account="account identity is inconsistent across pages",
        inconsistent_holder="account-holder identity is inconsistent across pages",
        wrong_account="statement account does not match the configured account",
    )
    return period_start, period_end, account_id, holder


def _account_identity(
    text: str,
    expected_account_id: str | None,
    *,
    inconsistent_account: str,
    inconsistent_holder: str,
    wrong_account: str,
) -> tuple[str, str]:
    """The identity header's account and holder, after requiring one account and
    one holder across ``text``'s pages, the configured account and the FBS/NFS
    carrying disclosure, in that order. The keyword texts word the account,
    holder and configured-account refusals."""
    main = _only(_MAIN_ID_RE, text, "CMA identity header")
    account_id, holder = main.group("account"), main.group("holder").strip()
    if len({account_id, *_SHORT_ACCOUNT_RE.findall(text)}) != 1:
        raise _fail(inconsistent_account)
    if {value.strip() for value in _SUMMARY_HOLDER_RE.findall(text)} != {holder}:
        raise _fail(inconsistent_holder)
    if expected_account_id is not None and account_id != expected_account_id:
        raise _fail(wrong_account)
    if _FBS_NFS_RE.search(text) is None:
        raise _fail("FBS/NFS carrying disclosure is missing")
    return account_id, holder


_Walk = list[tuple[str, re.Match[str]]]


def _walk(
    region: str,
    kinds: Iterable[tuple[str, re.Pattern[str]]],
    page_headers: _PageHeaders,
    last: Iterable[tuple[str, re.Pattern[str]]] = (),
) -> _Walk:
    """Split ``region`` into its non-blank lines, each matched whole by the first
    of ``kinds`` that fits (a match may span lines), else a page header
    (``"page"``), else one of ``last``, else ``""`` with the bare line."""
    candidates = (*kinds, *(("page", pattern) for pattern in page_headers), *last)
    result: _Walk = []
    for line in _LINE_RE.finditer(region):
        if (result and line.start() <= result[-1][1].end()) or not line.group().strip():
            continue
        match = next(
            (
                (kind, found)
                for kind, pattern in candidates
                if (found := pattern.match(region, line.start())) is not None
            ),
            ("", line),
        )
        _check_unused_numbers(match[1])
        result.append(match)
    return result


def _starting_with(line: re.Match[str], labels: Iterable[str]) -> str | None:
    content = " ".join(line.group().split())
    return next((label for label in labels if content.startswith(label)), None)


def _labelled_rows(region: str, grammar: dict[str, str], what: str) -> dict[str, list[str]]:
    """Read every line of ``region`` that starts with one of ``grammar``'s labels
    (the longest that fits) as that label, its value and nothing else.

    Returns each label's values.
    """
    labels = sorted(grammar, key=lambda label: -len(label))
    values: dict[str, list[str]] = {label: [] for label in grammar}
    for line in _LINE_RE.finditer(region):
        content = line.group().strip(" \t\f")
        label = next((label for label in labels if content.startswith(label)), None)
        if label is None:
            continue
        row = re.fullmatch(rf"{re.escape(label)}[ \t]+({grammar[label]})(?:[ \t]+S)?", content)
        if row is None:
            expected = "one amount or dash" if grammar[label] == _VALUE else "one amount"
            raise _fail(
                f"{what} row is malformed: expected {expected} and nothing else", field=label
            )
        values[label].append(row.group(1))
    return values


def _summary_line(body: str) -> re.Pattern[str]:
    """An Account Summary line: ``body``, then optionally a right-column item."""
    return _lines(rf"{body}(?:[ \t]+(?:{_RIGHT_COLUMN}))?")


def _summary_kinds(
    variant: CmaValuationVariant, rows: tuple[str, ...], one_value: tuple[str, ...]
) -> list[tuple[str, re.Pattern[str]]]:
    """The Account Summary's line kinds: its rows, then the lines read by shape."""
    kinds = [
        (
            row,
            _summary_line(
                rf"{_words(row)}[ \t]+"
                + (
                    rf"(?P<value>{_MONEY})"
                    if row in one_value
                    else rf"(?P<value>{_VALUE})[ \t]+(?P<limit_money_ytd>{_VALUE})"
                )
            ),
        )
        for row in rows
    ]
    # Text extraction can print the Accrued Interest amount on the line above
    # its label.
    kinds += [
        ("ai value", _summary_line(rf"(?P<value>{_MONEY})")),
        ("ai label", _summary_line(_words("Accrued Interest (AI)"))),
    ]
    texts = _SUMMARY_TEXT
    if variant is CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB:
        kinds += [
            (
                label,
                _summary_line(rf"{_words(label)}[ \t]+(?P<value>{_DOLLAR_MONEY})"),
            )
            for label in ("Market Value of Holdings", "Free Credit Balance")
        ]
        texts += _FCB_SUMMARY_TEXT
    kinds.append(("text", _summary_line(f"(?:{'|'.join(texts)})")))
    kinds.append(("text", _lines(_RIGHT_COLUMN)))
    return kinds


def _summary_lines(
    summary_text: str,
    pages: _PageHeaders,
    variant: CmaValuationVariant,
    rows: tuple[str, ...],
    one_value: tuple[str, ...],
) -> dict[str, list[re.Match[str]]]:
    """Walk the Account Summary; return each row's lines, each printed once."""
    kinds = _summary_kinds(variant, rows, one_value)
    found: dict[str, list[re.Match[str]]] = {kind: [] for kind, _pattern in kinds}
    balance_rows = tuple(
        row for row in ("Market Value of Holdings", "Free Credit Balance") if row in found
    )
    labels = sorted((*rows, *balance_rows), key=lambda row: -len(row))
    ai_row = "Accrued Interest (AI)"
    ai_malformed = _fail("Account Summary row is malformed: expected one amount", field=ai_row)
    previous = ""
    for kind, line in _walk(summary_text, kinds, pages):
        if kind == "page":
            continue
        if not kind:
            row = _starting_with(line, labels)
            if row is None:
                raise _fail("Account Summary contains an unsupported line")
            expected = (
                "one amount"
                if row in (*one_value, *balance_rows)
                else "This Period and Year-to-Date amounts or dashes"
            )
            raise _fail(f"Account Summary row is malformed: expected {expected}", field=row)
        if (kind == "ai label") != (previous == "ai value"):
            raise ai_malformed
        found[kind].append(line)
        previous = kind
    if previous == "ai value":
        raise ai_malformed
    found[ai_row] += found["ai value"]
    for row in (
        *rows,
        *(kind for kind in ("Market Value of Holdings", "Free Credit Balance") if kind in found),
    ):
        if not found[row]:
            raise _fail(f"{row} row is missing, malformed or outside Account Summary", field=row)
        if len(found[row]) != 1:
            raise _fail(f"{row} row is duplicated in Account Summary", field=row)
    return found


def _parse_summary(text: str, summary_text: str, pages: _PageHeaders) -> _Summary:
    normal = re.findall(
        rf"^[ \t\f]*Your Account Value:[ \t]+({_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*$",
        text,
        re.MULTILINE,
    )
    net = re.findall(
        rf"^[ \t\f]*Your Net Account Value:[ \t]+({_DOLLAR_MONEY})(?:[ \t]+S)?[ \t]*$",
        text,
        re.MULTILINE,
    )
    if (len(normal), len(net)) not in {(1, 0), (0, 1)}:
        raise _fail("statement has an unknown or ambiguous account valuation layout")
    if normal:
        variant = CmaValuationVariant.ACCOUNT_VALUE
        label = "Account"
        cover = _money(normal[0])
    else:
        variant = CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB
        label = "Net Account"
        cover = _money(net[0])

    aggregate_row = f"{label} Value:"
    beginning_row = f"Beginning {label} Value"
    ending_row = f"Ending {label} Value"
    with_ai_row = f"Ending {label} Value Incl. AI"
    ai_row = "Accrued Interest (AI)"
    rows = (
        aggregate_row,
        beginning_row,
        "Additions",
        "Subtractions",
        "Change in Investment Value *",
        ending_row,
        ai_row,
        with_ai_row,
    )
    found = _summary_lines(summary_text, pages, variant, rows, (aggregate_row, ai_row, with_ai_row))

    def value(row: str) -> Decimal:
        raw = found[row][0].group("value")
        return Decimal(0) if raw == "-" else _money(raw)

    aggregate = _consistent(
        (cover, value(aggregate_row), value(with_ai_row)), "aggregate account value"
    )
    beginning = None if found[beginning_row][0].group("value") == "-" else value(beginning_row)
    additions = value("Additions")
    subtractions = value("Subtractions")
    change = value("Change in Investment Value *")
    ending = value(ending_row)
    accrued_interest = value(ai_row)
    beginning_operand = Decimal(0) if beginning is None else beginning
    if beginning_operand + additions + subtractions + change != ending:
        raise _fail("Account Summary components do not reproduce the ending value")
    if ending + accrued_interest != aggregate:
        raise _fail("ending value plus accrued interest does not reproduce account value")
    fcb_layout = variant is CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB
    return _Summary(
        variant,
        beginning,
        additions,
        subtractions,
        change,
        ending,
        accrued_interest,
        aggregate,
        value("Market Value of Holdings") if fcb_layout else None,
        value("Free Credit Balance") if fcb_layout else None,
    )


def _total_values(row: re.Match[str]) -> tuple[Decimal, Decimal]:
    return _money(row.group("ending")), _money(row.group("income"))


def _holdings(
    region: str,
    pages: _PageHeaders,
    kinds: list[tuple[str, re.Pattern[str]]],
    *,
    year_end: bool = False,
) -> dict[str, list[re.Match[str]]]:
    """Walk a Holdings ``region``; each kind but ``"text"`` and ``"credit
    balance"`` must be printed once."""
    prefix = "year-end " if year_end else ""
    core_amounts = "two dollar amounts" if year_end else "two or three dollar amounts"
    malformed = {
        "Total Holdings": "Total Holdings row is malformed: expected exactly two dollar amounts",
        "Total Core Account": (
            f"Total Core Account row is malformed: expected its share of holdings "
            f"and {core_amounts}"
        ),
        "MARKET (SPAXX)": "SPAXX symbol/yield row is malformed",
        "FIDELITY GOVERNMENT MONEY": "SPAXX holdings row is malformed",
        "Net Account Value": "Net Account Value row is malformed: expected one dollar amount",
    }
    found: dict[str, list[re.Match[str]]] = {kind: [] for kind, _pattern in kinds}
    for kind, line in _walk(region, kinds, pages):
        if kind == "page":
            continue
        if not kind:
            row = _starting_with(line, malformed)
            if row is None:
                raise _fail(f"{prefix}Holdings contains an unsupported line")
            raise _fail(prefix + malformed[row], field=row)
        found[kind].append(line)
    for kind, lines in found.items():
        if kind not in {"text", "credit balance"} and len(lines) != 1:
            label = {
                "SPAXX holdings": "FIDELITY GOVERNMENT MONEY",
                "SPAXX symbol/yield": "MARKET (SPAXX)",
            }.get(kind, kind)
            raise _fail(f"expected exactly one {prefix}{kind} row; found {len(lines)}", field=label)
    return found


def _require_rows_in(text: str, regions: dict[str, str]) -> None:
    """Refuse a Free Credit Balance layout row printed outside its section."""
    for pattern, label, region in (
        *(
            (
                _summary_line(rf"{_words(label)}[ \t]+{_DOLLAR_MONEY}"),
                label,
                "Account Summary",
            )
            for label in ("Market Value of Holdings", "Free Credit Balance")
        ),
        (_NET_ACCOUNT_VALUE_RE, "Net Account Value", "Holdings"),
    ):
        if len(pattern.findall(text)) != len(pattern.findall(regions[region])):
            raise _fail(f"{label} row is outside {region}", field=label)


def _parse_position(
    holdings: str, pages: _PageHeaders, summary: _Summary
) -> tuple[CmaPosition, Decimal | None]:
    if _STRUCTURAL_FDIC_RE.search(holdings):
        raise _fail("FDIC Deposit Sweep or Program Bank positions are unsupported")
    fcb_layout = summary.variant is CmaValuationVariant.NET_ACCOUNT_VALUE_WITH_FCB
    kinds = [
        ("SPAXX holdings", _SPAXX_POSITION_RE),
        ("SPAXX symbol/yield", _SPAXX_SYMBOL_RE),
        ("Total Core Account", _TOTAL_CORE_RE),
        ("Total Holdings", _TOTAL_HOLDINGS_RE),
        ("text", _HOLDINGS_TEXT_RE),
    ]
    if fcb_layout:
        kinds += [
            ("Net Account Value", _NET_ACCOUNT_VALUE_RE),
            ("credit balance", _lines(_CREDIT_BALANCE)),
        ]
    found = _holdings(holdings, pages, kinds)
    position_match = found["SPAXX holdings"][0]
    begin_raw = position_match.group("begin")
    beginning = None if begin_raw == "unavailable" else _money(begin_raw)
    quantity = _money(position_match.group("quantity"), kind=NumberKind.QUANTITY)
    price = _money(position_match.group("price"), kind=NumberKind.PRICE)
    ending = _money(position_match.group("ending"))
    eai = _money(position_match.group("eai"))
    estimated_yield = _money(found["SPAXX symbol/yield"][0].group("yield"), kind=NumberKind.RATE)
    if quantity <= 0 or price <= 0 or ending <= 0:
        raise _fail("SPAXX holding quantity, price, and value must be positive")
    tolerance = _quantity_tolerance(price)
    if abs(quantity * price - ending) > tolerance:
        raise _fail("SPAXX quantity and price do not reproduce its market value")

    total_holdings = found["Total Holdings"][0]
    total_core = found["Total Core Account"][0]
    if _total_values(total_holdings) != (ending, eai) or _total_values(total_core) != (
        ending,
        eai,
    ):
        raise _fail("SPAXX values do not match Holdings totals")
    core_beginning = total_core.group("beginning")
    if (
        core_beginning is not None
        and (None if core_beginning == "unavailable" else _money(core_beginning)) != beginning
    ):
        raise _fail(
            "Total Core Account beginning value does not match the core fund holding",
            field="Total Core Account",
        )

    if not fcb_layout:
        if summary.aggregate != ending:
            raise _fail("account-value layout contains inconsistent cash or holdings totals")
        return CmaPosition(beginning, quantity, price, ending, eai, estimated_yield), None
    net_value_match = found["Net Account Value"][0]
    if net_value_match.start() < total_holdings.start():
        raise _fail("Net Account Value row precedes Total Holdings", field="Net Account Value")
    assert summary.market_value is not None and summary.free_credit_balance is not None
    net_value = _money(net_value_match.group("amount"))
    if (
        summary.free_credit_balance <= 0
        or summary.market_value != ending
        or net_value != summary.aggregate
        or summary.market_value + summary.free_credit_balance != summary.aggregate
    ):
        raise _fail("FCB and holdings do not reproduce Net Account Value")
    return (
        CmaPosition(beginning, quantity, price, ending, eai, estimated_yield),
        summary.free_credit_balance,
    )


def _listed_section(
    text: str,
    pages: _PageHeaders,
    *,
    header: str,
    columns: str,
    total: str,
    row_pattern: re.Pattern[str],
    heading: str | None = None,
    preamble: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
    continuation: re.Pattern[str] | None = None,
    continued: bool = False,
    lines: tuple[tuple[str, re.Pattern[str]], ...] = (),
    dash_quantity_price: bool = False,
    required: bool = False,
    detect_orphan_rows: bool = True,
) -> _ListedSection | None:
    """Read the section titled ``heading`` (default: ``header`` as printed)
    through its total.

    Its ``preamble`` and ``columns`` header must come in order before the first
    row. Every other line must
    be a ``row_pattern`` row, a ``notes`` line, ``<header> (continued)``, one of
    ``lines`` (returned by kind), a repeated page header or, right after a row,
    a ``continuation`` line (required after every row when ``continued``).
    ``dash_quantity_price`` refuses a dated row with an amount whose Quantity
    and Price cells aren't the ``row_pattern`` dashes.
    """
    header_re = re.compile(
        rf"^[ \t\f]*{heading or re.escape(header)}(?:[ \t]+S)?[ \t]*$", re.MULTILINE
    )
    total_re = re.compile(
        rf"^[ \t\f]*{re.escape(total)}[ \t]+(?P<amount>{_MONEY})"
        rf"(?:[ \t]+S)?[ \t]*$",
        re.MULTILINE,
    )
    headers = list(header_re.finditer(text))
    totals = list(total_re.finditer(text))
    if not headers:
        if required or totals or (detect_orphan_rows and row_pattern.search(text)):
            raise _fail(f"{header} section is missing or displaced")
        return None
    if len(headers) != 1 or len(totals) != 1:
        raise _fail(f"{header} section is duplicated or malformed")
    if totals[0].start() <= headers[0].end():
        raise _fail(f"{header} section total precedes its header")
    body = text[headers[0].end() : totals[0].start()]
    if dash_quantity_price and any(
        row_pattern.fullmatch(line.group()) is None for line in _CASH_ROW_RE.finditer(body)
    ):
        raise _fail(f"{header} row has an unsupported Quantity or Price")
    kinds = [
        ("row", row_pattern),
        ("columns", _lines(_words(columns))),
        ("continued", _lines(_words(f"{header} (continued)"))),
        *lines,
    ]
    if notes:
        kinds.append(("note", _lines(*map(_words, notes))))
    if preamble:
        kinds.append(("preamble", _lines(*map(_words, preamble))))
    found = _section_lines(
        body, pages, kinds, continuation, preamble=preamble, header=header, continued=continued
    )
    return _ListedSection(tuple(found["row"]), _money(totals[0].group("amount")), found)


def _section_lines(
    body: str,
    pages: _PageHeaders,
    kinds: list[tuple[str, re.Pattern[str]]],
    continuation: re.Pattern[str] | None,
    *,
    preamble: tuple[str, ...],
    header: str,
    continued: bool,
) -> dict[str, list[re.Match[str]]]:
    """Walk an activity section's ``body``; see ``_listed_section``. A row's
    second line is tried last, after the page headers."""
    last = () if continuation is None else (("continuation", continuation),)
    found: dict[str, list[re.Match[str]]] = {kind: [] for kind, _pattern in (*kinds, *last)}
    no_columns = _fail(f"{header} section column header is missing or unsupported")
    no_second_line = _fail(f"{header} section contains a row without its second line")
    previous = ""
    header_open = True
    preamble_read = 0
    for kind, line in _walk(body, kinds, pages, last):
        if kind == "page":
            continue
        if kind in {"", "row", "continuation"} and not found["columns"]:
            raise no_columns
        if not kind or (kind == "continuation" and previous != "row"):
            raise _fail(f"{header} section contains a line that is not a supported row")
        if continued and previous == "row" and kind != "continuation":
            raise no_second_line
        if kind == "continued":
            invalid_header = not found["columns"] or bool(preamble_read)
            header_open = True
        elif kind == "preamble":
            invalid_header = (
                not header_open
                or preamble_read == len(preamble)
                or _lines(_words(preamble[preamble_read])).fullmatch(line.group()) is None
            )
            preamble_read += 1
        elif kind == "columns":
            invalid_header = not header_open or (
                preamble_read != len(preamble) and (not found["columns"] or preamble_read > 0)
            )
            header_open = False
            preamble_read = 0
        else:
            invalid_header = bool(preamble_read)
        if invalid_header:
            raise no_columns
        header_open = header_open and kind != "row"
        found[kind].append(line)
        previous = kind
    if continued and previous == "row":
        raise no_second_line
    if not found["columns"] or preamble_read or not found["row"]:
        raise (
            no_columns
            if not found["columns"] or preamble_read
            else _fail(f"{header} section has no rows")
        )
    return found


def _parse_core(
    text: str, pages: _PageHeaders, start: date, end: date, position: CmaPosition
) -> tuple[CmaCoreTransaction, ...]:
    section = _listed_section(
        text,
        pages,
        header="Core Fund Activity",
        columns="Date Type Transaction Description Quantity Price Amount Balance",
        total="Total Core Fund Activity",
        row_pattern=_CORE_ROW_RE,
        preamble=(
            "For more information about the operation of your core account, please refer "
            "to your Customer Agreement.",
            "Settlement Account",
        ),
        continuation=_CORE_CONTINUATION_RE,
        continued=True,
        required=True,
    )
    assert section is not None
    transactions: list[CmaCoreTransaction] = []
    for match in section.rows:
        kind = CmaCoreTransactionKind(match.group("kind"))
        quantity = _money(match.group("quantity"), kind=NumberKind.QUANTITY)
        price = _money(match.group("price"), kind=NumberKind.PRICE)
        amount = _money(match.group("amount"))
        balance = _money(match.group("balance"))
        if kind is CmaCoreTransactionKind.SELL:
            if quantity >= 0 or amount >= 0:
                raise _fail("SPAXX sale quantity and amount must be negative")
        elif quantity <= 0 or amount <= 0:
            raise _fail("SPAXX buy/reinvestment quantity and amount must be positive")
        tolerance = _quantity_tolerance(price)
        if price <= 0 or balance < 0 or abs(quantity * price - amount) > tolerance:
            raise _fail("SPAXX activity amount, price, quantity, or balance is inconsistent")
        transactions.append(
            CmaCoreTransaction(
                _transaction_date(match.group("date"), start, end),
                kind,
                quantity,
                price,
                amount,
                balance,
            )
        )
    result = tuple(transactions)
    if (
        position.beginning_market_value_usd is not None
        and position.beginning_market_value_usd + result[0].amount_usd != result[0].balance_usd
    ):
        raise _fail("first SPAXX activity balance does not follow its beginning value")
    for previous, current in pairwise(result):
        if previous.balance_usd + current.amount_usd != current.balance_usd:
            raise _fail("SPAXX running balance is inconsistent")
    if result[-1].balance_usd != position.ending_market_value_usd:
        raise _fail("final SPAXX balance does not match Holdings")
    if sum((item.amount_usd for item in result), Decimal(0)) != section.total:
        raise _fail("SPAXX activity rows do not match their section total")
    return result


def _parse_income(
    text: str, pages: _PageHeaders, start: date, end: date
) -> tuple[CmaIncomeCredit, ...]:
    section = _listed_section(
        text,
        pages,
        header="Dividends, Interest & Other Income",
        columns=_ACTIVITY_COLUMNS,
        total="Total Dividends, Interest & Other Income",
        preamble=("(Includes dividend reinvestment)", "Settlement Symbol/"),
        row_pattern=_INCOME_ROW_RE,
        required=True,
    )
    assert section is not None
    income: list[CmaIncomeCredit] = []
    for match in section.rows:
        if match.group("div_date") is not None:
            income.append(
                CmaIncomeCredit(
                    _transaction_date(match.group("div_date"), start, end),
                    CmaIncomeKind.SPAXX_DIVIDEND,
                    _money(match.group("div_amount")),
                    match.group("cusip"),
                )
            )
        else:
            # On the opening statement, parse_statement refuses a date before
            # the period after its other checks (#1274).
            income.append(
                CmaIncomeCredit(
                    _transaction_date(
                        match.group("int_date"),
                        start,
                        end,
                        allow_previous_month=True,
                    ),
                    CmaIncomeKind.CREDIT_INTEREST,
                    _money(match.group("int_amount")),
                    None,
                )
            )
    result = tuple(income)
    if any(item.amount_usd <= 0 for item in result):
        raise _fail("income credits must be positive")
    if sum((item.amount_usd for item in result), Decimal(0)) != section.total:
        raise _fail("income rows do not match their section total")
    return result


def _parse_withholding(
    text: str, pages: _PageHeaders, start: date, end: date
) -> tuple[CmaWithholding, ...]:
    section = _listed_section(
        text,
        pages,
        header="Taxes Withheld",
        columns="Date Security Description Amount",
        total="Total Taxes Withheld",
        row_pattern=_WHT_ROW_RE,
        lines=(("federal", _FEDERAL_WHT_RE),),
    )
    if section is None:
        return ()
    result = tuple(
        CmaWithholding(
            _transaction_date(match.group("date"), start, end),
            _money(match.group("amount")),
        )
        for match in section.rows
    )
    if any(item.amount_usd >= 0 for item in result):
        raise _fail("positive or zero withholding/correction rows are unsupported")
    federal = [line.group("amount") for line in section.lines["federal"]]
    row_total = sum((item.amount_usd for item in result), Decimal(0))
    if len(federal) != 1 or _money(federal[0]) != row_total or section.total != row_total:
        raise _fail("withholding rows do not match federal and section totals")
    return result


def _parse_cash_section(
    text: str,
    pages: _PageHeaders,
    start: date,
    end: date,
    *,
    header: str,
    kind: CmaCashActivityKind,
    sign: int,
) -> tuple[CmaCashActivity, ...]:
    exchange = kind in {CmaCashActivityKind.TRANSFER_IN, CmaCashActivityKind.TRANSFER_OUT}
    section = _listed_section(
        text,
        pages,
        header=header,
        columns=_ACTIVITY_COLUMNS if exchange else "Date Reference Description Amount",
        total=f"Total {header}",
        row_pattern=_EXCHANGE_ROW_RE if exchange else _CASH_ROW_RE,
        preamble=("Symbol/",) if exchange else (),
        continuation=None if exchange else _CASH_CONTINUATION_RE,
        dash_quantity_price=exchange,
        detect_orphan_rows=False,
    )
    if section is None:
        return ()
    result = tuple(
        CmaCashActivity(
            _transaction_date(match.group("date"), start, end),
            kind,
            match.group("description").strip(),
            _money(match.group("amount")),
        )
        for match in section.rows
    )
    if any(item.amount_usd * sign <= 0 for item in result):
        raise _fail(f"{header} contains a row with the wrong sign")
    if sum((item.amount_usd for item in result), Decimal(0)) != section.total:
        raise _fail(f"{header} rows do not match their section total")
    return result


def _parse_debit_card(
    text: str, pages: _PageHeaders, start: date, end: date
) -> tuple[CmaDebitCardTransaction, ...]:
    section = _listed_section(
        text,
        pages,
        header="Debit Card Activity",
        heading=r"Debit Card Summary[ \t]+Debit Card Activity",
        columns="TRANSACTIONS",
        total="Total Debit Card Activity",
        row_pattern=_CARD_ROW_RE,
        notes=("TRANSACTIONS (continued)", "Purchases", "Purchases (continued)", _CARD_COLUMNS),
        lines=(
            ("purchases", _lines(rf"Total Purchases[ \t]+(?P<amount>{_DOLLAR_MONEY})")),
            ("subtotal", _lines(rf"{_CARD}[ \t]+Subtotal[ \t]+(?P<amount>{_DOLLAR_MONEY})")),
            # The card's balance summary, read by shape only.
            (
                "balance",
                _lines(
                    rf"Beginning Balance[ \t]+(?P<limit_money_card_begin>{_DOLLAR_MONEY})"
                    rf"[ \t]+{_CARD}",
                    rf"Total Payments[ \t]+(?P<limit_money_card_payments>{_MONEY})",
                    rf"Total Purchases[ \t]+"
                    rf"(?P<limit_money_card_purchases>-?{_UNSIGNED}\.[0-9]{{2}})",
                    rf"Ending Balance[ \t]+(?P<limit_money_card_end>{_DOLLAR_MONEY})"
                    rf"[ \t]+{_words(_CARD_COLUMNS)}",
                    rf"{_CARD}[ \t]+\(continued\)",
                ),
            ),
        ),
    )
    if section is None:
        return ()
    for kind in ("purchases", "subtotal"):
        if [_money(line.group("amount")) for line in section.lines[kind]] != [section.total]:
            raise _fail("debit-card Purchases and card subtotals do not match the section total")
    # No output reads the purchase date, so it may be in the month before a full
    # month's period; a partial first period starts on the opening date.
    # On a dash-beginning statement, parse_statement refuses one before the
    # period after its other checks (#1274).
    result = tuple(
        CmaDebitCardTransaction(
            _transaction_date(
                match.group("transaction_date"),
                start,
                end,
                allow_previous_month=start.day == 1,
            ),
            _transaction_date(match.group("post_date"), start, end),
            match.group("description").strip(),
            _money(match.group("amount")),
        )
        for match in section.rows
    )
    if any(item.amount_usd >= 0 for item in result):
        raise _fail("positive or zero debit-card rows are unsupported")
    if sum((item.amount_usd for item in result), Decimal(0)) != section.total:
        raise _fail("debit-card rows do not match their section total")
    return result


def _require_dated_lines_read(text: str, report: re.Pattern[str], rows_read: int) -> None:
    """Refuse unless the lines starting in a documented date form are the
    ``rows_read`` activity rows, setting aside each ``report`` title's period line.

    Every read section accounts for each of its lines, so a count suffices."""
    period_lines = {
        text.rfind("\n", 0, match.start("start")) + 1 for match in report.finditer(text)
    }
    dated = [line for line in _DATED_LINE_RE.finditer(text) if line.start() not in period_lines]
    if len(dated) != rows_read:
        raise _fail("statement contains unsupported or malformed dated activity")


def _cross_check_dividend_reinvestment(
    income: tuple[CmaIncomeCredit, ...],
    withholding: tuple[CmaWithholding, ...],
    core: tuple[CmaCoreTransaction, ...],
) -> None:
    dividends = tuple(item for item in income if item.kind is CmaIncomeKind.SPAXX_DIVIDEND)
    dividend_dates = {item.date for item in dividends}
    reinvestment_dates = {
        item.date for item in core if item.kind is CmaCoreTransactionKind.REINVESTMENT
    }
    if {item.date for item in withholding} - dividend_dates or reinvestment_dates != dividend_dates:
        raise _fail("SPAXX dividend, withholding, and reinvestment dates do not reconcile")
    for payment_date in dividend_dates:
        gross = sum(
            (item.amount_usd for item in dividends if item.date == payment_date),
            Decimal(0),
        )
        taxes = [item.amount_usd for item in withholding if item.date == payment_date]
        reinvestments = [
            item.amount_usd
            for item in core
            if item.date == payment_date and item.kind is CmaCoreTransactionKind.REINVESTMENT
        ]
        if len(reinvestments) != 1 or gross + sum(taxes, Decimal(0)) != reinvestments[0]:
            raise _fail("SPAXX dividend, withholding, and reinvestment do not reconcile")


@_decimal_errors
def parse_statement(
    text: str,
    *,
    expected_currency: str,
    expected_account_id: str | None = None,
) -> CmaStatement:
    """Parse one observed Fidelity CMA monthly statement.

    ``expected_currency`` is mandatory: a dollar sign alone is not treated as
    proof of ISO currency. The observed layout supports only caller-confirmed
    USD statements.
    """
    if expected_currency != "USD":
        raise _fail("only explicitly confirmed USD CMA statements are supported")
    start, end, account_id, holder = _parse_identity(text, expected_account_id)
    if _STRUCTURAL_FDIC_RE.search(text):
        raise _fail("FDIC Deposit Sweep or Program Bank positions are unsupported")
    if re.search(
        r"^[ \t\f]*(?:Stocks|Bonds|Certificates of Deposit|Securities Bought & Sold)"
        r"[ \t]*$",
        text,
        re.MULTILINE,
    ):
        raise _fail("non-SPAXX holdings or securities activity are unsupported")

    summary_header = _only(_SUMMARY_HOLDER_RE, text, "Account Summary")
    pages = _page_headers(_REPORT_RE, holder)
    holdings_header = _HOLDINGS_HEADING_RE.search(text, summary_header.end())
    if holdings_header is None:
        raise _fail("Holdings section is missing after Account Summary")
    _check_section_heading(holdings_header, "Holdings", pages)
    activity_header = _ACTIVITY_HEADING_RE.search(text, holdings_header.end())
    if activity_header is None:
        raise _fail("Activity section is missing")
    _check_section_heading(activity_header, "Activity", pages)
    summary_text = text[summary_header.end() : holdings_header.start()]
    holdings_text = text[holdings_header.end() : activity_header.start()]
    _require_rows_in(text, {"Account Summary": summary_text, "Holdings": holdings_text})
    summary = _parse_summary(text, summary_text, pages)
    position, fcb = _parse_position(holdings_text, pages, summary)
    core = _parse_core(text, pages, start, end, position)
    income = _parse_income(text, pages, start, end)
    withholding = _parse_withholding(text, pages, start, end)
    cash = (
        _parse_cash_section(
            text,
            pages,
            start,
            end,
            header="Deposits",
            kind=CmaCashActivityKind.DEPOSIT,
            sign=1,
        )
        + _parse_cash_section(
            text,
            pages,
            start,
            end,
            header="Withdrawals",
            kind=CmaCashActivityKind.WITHDRAWAL,
            sign=-1,
        )
        + _parse_cash_section(
            text,
            pages,
            start,
            end,
            header="Exchanges In",
            kind=CmaCashActivityKind.TRANSFER_IN,
            sign=1,
        )
        + _parse_cash_section(
            text,
            pages,
            start,
            end,
            header="Exchanges Out",
            kind=CmaCashActivityKind.TRANSFER_OUT,
            sign=-1,
        )
    )
    cards = _parse_debit_card(text, pages, start, end)
    _cross_check_dividend_reinvestment(income, withholding, core)

    consumed_rows = len(core) + len(income) + len(withholding) + len(cash) + len(cards)
    _require_dated_lines_read(text, _REPORT_RE, consumed_rows)
    # Checked last, so a statement with another defect keeps that defect's refusal.
    if any(item.transaction_date > item.post_date for item in cards):
        raise _fail("a debit-card purchase date is after its posting date")
    # No interest or card purchase predates the account (#1274). Every other
    # date was read inside the period.
    if _opening_statement(start, summary.beginning) and (
        any(item.date < start for item in income)
        or any(item.transaction_date < start for item in cards)
    ):
        raise _fail("a transaction date is invalid, ambiguous, or outside the statement period")

    return CmaStatement(
        start,
        end,
        account_id,
        holder,
        expected_currency,
        summary.variant,
        summary.beginning,
        summary.additions,
        summary.subtractions,
        summary.change,
        summary.ending,
        summary.accrued_interest,
        summary.aggregate,
        fcb,
        position,
        core,
        income,
        withholding,
        cash,
        cards,
    )


def _parse_year_end_position(
    text: str, holdings_start: int, pages: _PageHeaders
) -> CmaYearEndPosition | None:
    """Read the SPAXX holding in the Holdings from ``holdings_start`` through
    ``Total Holdings``, or ``None`` when the report prints no holdings."""
    total_lines = list(_TOTAL_HOLDINGS_LINE_RE.finditer(text))
    if not total_lines:
        if _TOTAL_CORE_LINE_RE.search(text) or "FIDELITY GOVERNMENT MONEY" in text:
            raise _fail("year-end SPAXX holdings are incomplete or malformed")
        return None
    if len(total_lines) != 1 or total_lines[0].start() < holdings_start:
        raise _fail(
            "year-end Total Holdings row is duplicated or outside Holdings",
            field="Total Holdings",
        )
    holdings_end = total_lines[0].end()
    for outside in (text[:holdings_start], text[holdings_end:]):
        if _TOTAL_CORE_LINE_RE.search(outside) or _YEAR_END_SPAXX_POSITION_RE.search(outside):
            raise _fail("year-end SPAXX holdings row is outside Holdings")
    found = _holdings(
        text[holdings_start:holdings_end],
        pages,
        [
            ("SPAXX holdings", _YEAR_END_SPAXX_POSITION_RE),
            ("Total Core Account", _TOTAL_CORE_RE),
            ("Total Holdings", _TOTAL_HOLDINGS_RE),
            ("text", _YEAR_END_HOLDINGS_TEXT_RE),
        ],
        year_end=True,
    )
    position_match = found["SPAXX holdings"][0]
    quantity = _money(position_match.group("quantity"), kind=NumberKind.QUANTITY)
    price = _money(position_match.group("price"), kind=NumberKind.PRICE)
    market_value = _money(position_match.group("ending"))
    income_earned = _money(position_match.group("income"))
    tolerance = _quantity_tolerance(price)
    if (
        quantity <= 0
        or price <= 0
        or market_value <= 0
        or income_earned < 0
        or abs(quantity * price - market_value) > tolerance
    ):
        raise _fail("year-end SPAXX quantity, price, value, or income is inconsistent")
    total_core = found["Total Core Account"][0]
    if total_core.group("beginning") is not None:
        raise _fail(
            "year-end Total Core Account row is malformed: expected its share of holdings "
            "and two dollar amounts",
            field="Total Core Account",
        )
    if _total_values(found["Total Holdings"][0]) != (
        market_value,
        income_earned,
    ) or _total_values(total_core) != (market_value, income_earned):
        raise _fail("year-end SPAXX values do not match Holdings totals")
    return CmaYearEndPosition(
        quantity,
        price,
        market_value,
        income_earned,
    )


def _year_end_interest(summary: str, tax_year: int) -> Iterator[Decimal]:
    """Read each year-end summary line with ``Interest`` before a value, which
    must be ``Interest <amount>`` or the tax year's ``Ending Account Value as
    of Dec 31`` line with Interest as its right-hand column."""
    for line in _LINE_RE.finditer(summary):
        if _YEAR_END_INTEREST_LINE_RE.search(line.group()) is None:
            continue
        row = _YEAR_END_INTEREST_RE.fullmatch(line.group())
        if row is None or row.group("year") not in {None, str(tax_year)}:
            raise _fail("year-end Interest row is malformed", field="Interest")
        amount = _money(row.group("amount"))
        _check_unused_numbers(row)
        yield amount


@_decimal_errors
def parse_year_end_income_summary(
    text: str,
    *,
    expected_currency: str,
    expected_account_id: str | None = None,
) -> CmaYearEndIncomeSummary:
    """Parse the observed annual income summary used to corroborate monthly rows."""
    if expected_currency != "USD":
        raise _fail("only explicitly confirmed USD CMA reports are supported")
    reports = list(_YEAR_END_REPORT_RE.finditer(text))
    if len(reports) != len(_YEAR_END_LABEL_RE.findall(text)) or not reports:
        raise _fail("year-end report header is missing or malformed")
    report_values = {
        (match.group("year"), match.group("start"), match.group("end")) for match in reports
    }
    if len(report_values) != 1:
        raise _fail("year-end report period is inconsistent across pages")
    year_raw, start_raw, end_raw = report_values.pop()
    tax_year = int(year_raw)
    period_start = _full_date(start_raw)
    if (
        period_start.year != tax_year
        or period_start != date(tax_year, 1, 1)
        or _full_date(end_raw) != date(tax_year, 12, 31)
    ):
        raise _fail("year-end report does not cover one complete calendar year")

    account_id, holder = _account_identity(
        text,
        expected_account_id,
        inconsistent_account="account identity is inconsistent across year-end pages",
        inconsistent_holder="account-holder identity is inconsistent across year-end pages",
        wrong_account="year-end account does not match the configured account",
    )

    _require_dated_lines_read(text, _YEAR_END_REPORT_RE, 0)

    summary_header = _only(_SUMMARY_HOLDER_RE, text, "year-end Account Summary")
    pages = _page_headers(_YEAR_END_REPORT_RE, holder)
    holdings_header = _HOLDINGS_HEADING_RE.search(text, summary_header.end())
    if holdings_header is None:
        raise _fail("year-end Holdings section is missing")
    _check_section_heading(holdings_header, "Holdings", pages, year_end=True)
    summary = text[summary_header.end() : holdings_header.start()]
    values = _labelled_rows(
        summary, {"Dividends": _MONEY, "Taxes Withheld": _MONEY}, "year-end Account Summary"
    )
    dividends = _consistent(map(_money, values["Dividends"]), "year-end dividends")
    interest = _consistent(_year_end_interest(summary, tax_year), "year-end interest")
    withholding = _consistent(map(_money, values["Taxes Withheld"]), "year-end withholding")
    if dividends < 0 or interest < 0 or withholding > 0:
        raise _fail("year-end income or withholding has an unsupported sign")
    position = _parse_year_end_position(text, holdings_header.end(), pages)
    return CmaYearEndIncomeSummary(
        tax_year,
        account_id,
        holder,
        expected_currency,
        dividends,
        interest,
        withholding,
        position,
    )


def _validate_bundle_opening(result: tuple[CmaStatement, ...]) -> None:
    if result[0].period_start.day != 1 and result[0].beginning_account_value_usd is not None:
        raise _fail("partial first statement has a carried beginning value")
    if any(item.beginning_account_value_usd is None for item in result[1:]):
        raise _fail("only the earliest bundle statement may have a dash beginning value")
    if any(item.period_start.day != 1 for item in result[1:]):
        raise _fail("only the earliest bundle statement may cover a partial month")


@dataclass(frozen=True, slots=True, repr=False)
class CmaStatementBundle:
    """One account's gap-free monthly statements, in period order.

    Every construction, ``dataclasses.replace`` included, runs the bundle
    checks, so an instance is always a validated bundle.
    """

    statements: tuple[CmaStatement, ...]

    def __post_init__(self) -> None:
        result = self.statements
        if not isinstance(result, tuple):
            raise _fail("statement bundle must be a tuple")
        if not result:
            raise _fail("statement bundle is empty")
        if len({item.account_id for item in result}) != 1:
            raise _fail("statement bundle contains multiple accounts")
        if len({item.account_holder for item in result}) != 1:
            raise _fail("statement bundle contains inconsistent account holders")
        if len({item.currency for item in result}) != 1:
            raise _fail("statement bundle contains multiple currencies")
        _validate_bundle_opening(result)
        for previous, current in pairwise(result):
            if (
                current.period_start <= previous.period_end
                or current.period_start <= previous.period_start
            ):
                raise _fail("statement bundle contains duplicate or overlapping periods")
            if current.period_start != previous.period_end + timedelta(days=1):
                raise _fail("statement bundle has a gap")
            assert current.beginning_account_value_usd is not None
            if previous.account_value_usd != current.beginning_account_value_usd:
                raise _fail("adjacent statement ending and beginning values do not match")

    def covers_from(self, day: date) -> bool:
        """Whether the bundle starts by ``day`` or later with no carried
        account value, at the account's opening (ADR 0004 §4).
        This checks the start only; callers enforce their required end."""
        first = self.statements[0]
        return first.period_start <= day or first.beginning_account_value_usd is None

    def through(self, end: date) -> CmaStatementBundle:
        """The statements ending on or before ``end``; the last must end on ``end``."""
        return validate_statement_bundle(
            (item for item in self.statements if item.period_end <= end),
            expected_end=end,
        )


def validate_statement_bundle(
    statements: Iterable[CmaStatement],
    *,
    expected_start: date | None = None,
    expected_end: date | None = None,
) -> CmaStatementBundle:
    """Sort and validate a gap-free requested monthly statement window."""
    bundle = CmaStatementBundle(tuple(sorted(statements, key=lambda item: item.period_start)))
    if expected_start is not None and bundle.statements[0].period_start != expected_start:
        raise _fail("statement bundle does not start at the requested date")
    if expected_end is not None and bundle.statements[-1].period_end != expected_end:
        raise _fail("statement bundle does not end at the requested date")
    return bundle


class CmaDiscoveryError(IbkrFaError):
    """The CMA statement folder cannot be read as one set of statements."""


def load_fidelity_cma_statements(
    input_dir: Path,
    *,
    expected_account_id: str,
    spaxx_isin: str,
) -> tuple[CmaStatementBundle, dict[int, CmaYearEndIncomeSummary], FidelityCmaSourceRecord]:
    """Render each PDF in ``input_dir`` once and return the validated monthly
    statements, the year-end reports keyed by tax year, and the statements'
    source record."""
    if not is_dir(input_dir):
        raise CmaDiscoveryError("monthly-statement input must be a directory")
    statements: list[CmaStatement] = []
    year_ends: dict[int, CmaYearEndIncomeSummary] = {}
    paths = sorted(input_dir.glob("*.pdf"))
    if not paths:
        raise CmaDiscoveryError("no monthly statement PDFs found")
    for path in paths:
        text = pdftotext_layout(
            path,
            doc_kind="Fidelity CMA statement PDFs",
            source=_PDF_SOURCE,
        )
        if "YEAR-END INVESTMENT REPORT" in text:
            summary = parse_year_end_income_summary(
                text,
                expected_currency="USD",
                expected_account_id=expected_account_id,
            )
            if summary.tax_year in year_ends:
                raise CmaDiscoveryError("duplicate CMA year-end report")
            year_ends[summary.tax_year] = summary
        else:
            statements.append(
                parse_statement(
                    text,
                    expected_currency="USD",
                    expected_account_id=expected_account_id,
                )
            )
    bundle = validate_statement_bundle(statements)
    return bundle, year_ends, to_source_record(bundle, spaxx_isin=spaxx_isin)


def to_income_models(
    statement: CmaStatement,
    *,
    spaxx_isin: str,
) -> tuple[tuple[Dividend, ...], tuple[InterestCredit, ...], tuple[WithholdingTax, ...]]:
    """Map parsed income to existing source-neutral models.

    The statement carries only a CUSIP, so the SPAXX ISIN must come from
    separately verified configuration; it is never derived or fabricated.
    """
    if ISIN_PATTERN.fullmatch(spaxx_isin) is None:
        raise ConfigError(
            field="fidelity_cma.spaxx_isin",
            message="must be a verified 12-character ISO 6166 ISIN",
        )
    if any(
        item.kind is not CmaIncomeKind.SPAXX_DIVIDEND
        and item.kind is not CmaIncomeKind.CREDIT_INTEREST
        for item in statement.income_credits
    ):
        raise _fail("unsupported CMA income classification")
    isin = Isin(spaxx_isin)
    dividends = tuple(
        Dividend(
            symbol=_SPAXX_SYMBOL,
            isin=isin,
            payment_date=item.date,
            amount_gross=item.amount_usd,
            currency=statement.currency,
            description="FIDELITY GOVERNMENT MONEY MARKET Dividend Received",
            kind=DistributionKind.CASH_DIVIDEND,
            type_slice="Dividend Received",
        )
        for item in statement.income_credits
        if item.kind is CmaIncomeKind.SPAXX_DIVIDEND
    )
    interests = tuple(
        InterestCredit(
            credit_date=item.date,
            amount_gross=item.amount_usd,
            currency=statement.currency,
            description="CREDIT INTEREST",
        )
        for item in statement.income_credits
        if item.kind is CmaIncomeKind.CREDIT_INTEREST
    )
    withholdings = tuple(
        WithholdingTax(
            symbol=_SPAXX_SYMBOL,
            isin=isin,
            payment_date=item.date,
            amount=item.amount_usd,
            currency=statement.currency,
            description="FIDELITY GOVERNMENT MONEY MARKET Non-Resident Tax",
        )
        for item in statement.withholdings
    )
    return dividends, interests, withholdings


def to_source_record(
    bundle: CmaStatementBundle,
    *,
    spaxx_isin: str,
) -> FidelityCmaSourceRecord:
    """Map every statement's income to models once, keeping statement order."""
    dividends: list[Dividend] = []
    interests: list[InterestCredit] = []
    withholdings: list[WithholdingTax] = []
    for statement in bundle.statements:
        statement_dividends, statement_interests, statement_withholdings = to_income_models(
            statement,
            spaxx_isin=spaxx_isin,
        )
        dividends.extend(statement_dividends)
        interests.extend(statement_interests)
        withholdings.extend(statement_withholdings)
    return FidelityCmaSourceRecord(
        dividends=tuple(dividends),
        interest_credits=tuple(interests),
        withholding_tax=tuple(withholdings),
    )
