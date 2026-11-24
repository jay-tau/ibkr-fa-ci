"""Parse one Robinhood brokerage statement into typed source evidence.

Each Account Summary and Income and Expense Summary row, and each holdings
total row, is read whole: its label, then exactly its value cells, with every
money cell in USD (``$``). The parser checks these totals against the rows
they cover:

- Account Summary ``Portfolio Value``: the cash rows (Brokerage Cash Balance
  and Deposit Sweep Balance, or Net Account Balance) plus ``Total Securities
  *``, at opening and at close.
- Holdings ``Total Securities``: printed once, closing the last holdings table.
  It covers every holdings row, held and loaned. Its market value is their
  ``Mkt Value`` sum and its estimated dividend, when printed, their
  ``Est. Dividend`` sum. The market-value sum must also equal the Account Summary's closing
  ``Total Securities *``.
- ``Total Priced Portfolio``: printed once, after ``Total Securities``. It is
  ``Total Securities`` plus the Brokerage Cash Balance and Deposit Sweep
  Balance rows printed between the two.
- ``Total Funds Paid and Received`` (Account Activity) and ``Total Executed
  Trades Pending Settlement`` (pending trades): printed once per section,
  after its last row, on whichever page that falls. Each is the debit and
  credit sums of its section's rows on every page.
- ``Total Swept Funds`` and ``Total Collateral Movements``: printed once per
  ledger, after its closing row. Each is the debit and credit sums of its
  ledger's rows on every page. The ledger opens with its opening row and
  closes with its closing row, and the running balance must carry from one
  to the other.
- Stock Lending ``Total Market Value``: printed once per page, ending that
  page's one loaned-securities table. It is the sum of that page's market
  values.

Each holdings and Stock Lending row's market value must also reconcile to
quantity times price, using a 100-share multiplier for holdings options.
The row check allows its established tolerance from the printed quantity
and price, plus a cent. Every total, running balance and row
check is computed with every digit (#1295, :mod:`ibkr_fa.parser._exact`), so
a number past Decimal's default 28 digits can neither pass a value its rows
don't give nor refuse one they do.

Activity and ledger totals, and Stock Lending ``Total Market Value``, are
whole rows: their exact label and one ``$`` amount per value cell. Each ends
its table. Below an activity, ledger or loaned-securities total on that page,
only blank lines, the page marker and the start of another table may follow.
Below ``Total Market Value`` and ``Total Collateral Movements``, that table's
fixed SIPC reminder may also end the page. Below ``Total Priced Portfolio``, which
ends the holdings, the same holds, with the fixed ``Total Securities``
footnote in place of a reminder.

Holdings, Account Activity, pending trades, Program Banks, sweep activity
and the three Stock Lending tables open with a separate heading
(``Portfolio Summary`` for the holdings), the whole line ``<heading>`` or
``<heading> (continued)``, printed once on its page. Between a heading and
its column header only blank lines,
the page marker and at most one intro line may appear. The intro line must
be the table's approved sentence in ``_INTRO_SENTENCES``: its fixed wording,
with a run of digits, such as a date, in each of its slots. Executed Trades
Pending Settlement, Deposit Sweep Activity, Stock Lending - Loan Activity and
Stock Lending - Collateral Activity each have one with no slot; Deposit Sweep
Program Banks and Stock Lending - Loaned Securities each have one with a date
slot. Portfolio Summary and Account Activity have none.
Above a page's first table only blank lines and the page marker may
appear, and on page 1 its identity block and summaries above a heading. A
holdings header may start without its heading only as its page's first line
after those, under the holdings rows above it, or where another table's
reader stops. Each covered reader validates its table through the page end
or the start of another table (``_starts_table``). Program Banks may finish
with one adjacent disclosure paragraph accepted by layout without reading
its lines. The Income and Expense Summary block reads the same way, and on
page 1 may end with one paragraph ending at the document identifier,
likewise accepted by layout (``_income_paragraph``).

The Account Summary heading line, like the Income and Expense Summary one,
holds its heading and then only its column labels, ``Opening Balance`` and
``Closing Balance``, in order and separated by blanks. Outside the approved
overflow footnote, a line that starts with a holdings label, ``Securities
Held in Account`` or ``Loaned
Securities``, is only ever a holdings header. A line that starts with
``Important Information`` is only ever a disclosure page's heading: the whole
line, as its page's first line apart from blank lines and the page marker.
Page 2 may open with the ``Total Securities`` footnote and then print no page
marker. Both pages are accepted by layout without reading their prose
(``_check_fixed_pages``): below the disclosure heading no line may hold a
money amount, a date or a ``%``, and the overflow page must open with the
approved footnote (``_PORTFOLIO_FOOTNOTE``), however its words wrap, with no
money amount or date in any line after it. Every table row this parser reads
holds a money amount or a date, so none can sit unread on either page.
"""

from __future__ import annotations

import contextlib
import operator
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from functools import partial, reduce
from pathlib import Path
from typing import Final, NoReturn

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser._pdf import pdftotext_layout
from ibkr_fa.parser._robinhood_money import ROBINHOOD_WHOLE_DOLLARS as _MONEY_WHOLE
from ibkr_fa.parser.robinhood_statement.models import (
    RobinhoodAccountSummary,
    RobinhoodStatement,
    RobinhoodStatementActivity,
    RobinhoodStatementFamily,
    RobinhoodStatementIncome,
    RobinhoodStatementKind,
    RobinhoodStatementLayout,
    RobinhoodStatementPosition,
    RobinhoodStatementSection,
    RobinhoodStatementSweepActivity,
    RobinhoodStatementSweepBank,
    RobinhoodStatementTotal,
    RobinhoodStockCollateralActivity,
    RobinhoodStockLoanActivity,
    RobinhoodStockLoanedSecurity,
    RobinhoodStockLoanKind,
    SourceLocation,
)
from ibkr_fa.types import STANDARD_OPTION_MULTIPLIER

_SECTION: Final[str] = "Robinhood brokerage statement"
_CURRENCY: Final[str] = "USD"
_CRYPTO_MARKER: Final[str] = "Robinhood Crypto"
_CRYPTO_TITLE: Final[str] = "Crypto Statement"
_CRYPTO_MONTH_RE: Final = re.compile(r"(?:0[1-9]|1[0-2])-[0-9]{4}")
_BROKERAGE_ENTITIES: Final[tuple[str, ...]] = ("Robinhood Securities", "Robinhood Financial")
_OVERFLOW_MARKER: Final[str] = "* “Total Securities” includes"
_DISCLOSURE_MARKER: Final[str] = "Important Information"

_ACCOUNT_RE: Final = re.compile(
    r"^[ \t]*(?:(?P<label>Managed Individual|Individual) Account|Account)"
    r"[ \t]*#:[ \t]*(?P<number>[0-9]+)[ \t]*$",
    re.MULTILINE,
)
_PERIOD_RE: Final = re.compile(
    r"(?P<start>[0-9]{1,2}/[0-9]{1,2}/[0-9]{4})[ \t]+"
    r"(?:to|-|\u2013)[ \t]+(?P<end>[0-9]{1,2}/[0-9]{1,2}/[0-9]{4})"
)
_PAGE_RE: Final = re.compile(r"Page[ \t]+(?P<number>[0-9]+)[ \t]+of[ \t]+(?P<total>[0-9]+)")
_DOCUMENT_ID_RE: Final = re.compile(r"[ \t]{20,}(?P<identifier>[0-9]{7})[ \t]*$")
# Each whole word Call or Put, in any letter case, starts an option contract
# candidate, which must be one whole contract.
_OPTION_WORD_RE: Final = re.compile(r"\b(?:call|put)\b", re.IGNORECASE)
_OPTION_RE: Final = re.compile(
    r"(?<!\S)(?P<symbol>[A-Z][A-Z0-9.-]*)[ \t]+"
    r"(?P<expiration>[0-9]{1,2}/[0-9]{1,2}/[0-9]{4})[ \t]+"
    rf"(?P<right>Call|Put)[ \t]+\$(?P<strike>{_MONEY_WHOLE}\.[0-9]{{2}})(?!\S)"
)
# Each occurrence of the phrase, in any letter case, is a transfer identity,
# which must be one whole pair.
_TRANSFER_PHRASE_RE: Final = re.compile(r"Internal Security Transfer", re.IGNORECASE)
_TRANSFER_RE: Final = re.compile(
    r"Internal Security Transfer from[ \t]+(?P<source>[0-9]+)"
    r"[ \t]+to[ \t]+(?P<destination>[0-9]+)(?!\S)",
    re.IGNORECASE,
)
_CUSIP_RE: Final = re.compile(r"^CUSIP: (?P<cusip>[A-Z0-9]{9})$", re.MULTILINE)
# A line whose first word is CUSIP, in any letter case: followed by a colon,
# a blank or the line end.
_CUSIP_LINE_RE: Final = re.compile(r"\s*CUSIP(?:[:\s]|$)", re.IGNORECASE)

_MONEY_BODY: Final[str] = rf"{_MONEY_WHOLE}\.[0-9]+"
# A whole part of 0, ungrouped digits, or digits grouped by commas in threes.
_NUMBER_BODY: Final[str] = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?"
_MONEY_TOKEN_RE: Final = re.compile(
    rf"(?<![-($0-9,])(?:-?\$?{_MONEY_BODY}|\(\$?{_MONEY_BODY}\))(?![0-9,.%])"
)
_NUMBER_TOKEN_RE: Final = re.compile(rf"-?{_NUMBER_BODY}")
_USD_CELL_RE: Final = re.compile(rf"-?\${_MONEY_BODY}|\(\${_MONEY_BODY}\)")
# A token's first character after a column gap of two or more blanks.
_GAP_TOKEN_RE: Final = re.compile(r"(?<=\s\s)\S")
_DATE_TOKEN_RE: Final = re.compile(r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")
_PERCENT_TOKEN_RE: Final = re.compile(r"-?[0-9]+(?:\.[0-9]+)?%")
_LOAN_TRANSACTION_CODE: Final[str] = "SLJNL"
_OPTION_TRADE_CODES: Final[frozenset[str]] = frozenset({"BTO", "STC"})
_LEDGER_LABELS: Final[tuple[str, ...]] = ("Description", "Date", "Debit", "Credit", "Balance")
_LOANED_LABELS: Final[tuple[str, ...]] = (
    "Description",
    "Symbol",
    "Shares Lent",
    "Price",
    "MKT Value",
)
_LOANED_TOTAL: Final[str] = "Total Market Value"
# The fixed SIPC reminders printed under the loaned-securities and
# collateral totals, and the ``Total Securities`` footnote printed under the
# holdings' ``Total Priced Portfolio``, each as its printed lines.
_LOANED_FOOTNOTE: Final[tuple[str, ...]] = (
    "As a reminder, you can sell your shares as usual, even if they are on loan. The "
    "provisions of the Securities Investor Protection Act of 1970 may not protect you "
    "with respect to loaned securities.",
)
_COLLATERAL_FOOTNOTE: Final[tuple[str, ...]] = (
    "Collateral held for securities loaned through Stock Lending is at least equal to the "
    "market value of such securities. The provisions of the Securities Investor Protection "
    "Act of 1970 may not protect you with respect to such loaned securities.",
)
_PORTFOLIO_FOOTNOTE: Final[tuple[str, ...]] = (
    "* “Total Securities” includes those securities held in your account as well as those "
    "on loan through Stock Lending (identified herein as “Loaned Securities” or “Loaned "
    "Stocks”). The provisions of the Securities Investor Protection Act of 1970 may not "
    "protect you with respect",
    "to Loaned Securities.",
)
_LOAN_LABELS: Final[tuple[str, ...]] = (
    "Description",
    "Symbol",
    "Transaction",
    "Date",
    "Shares Lent From Account",
    "Shares Returned To Account",
)
_ACTIVITY_TOTALS: Final[dict[RobinhoodStatementSection, str]] = {
    RobinhoodStatementSection.ACCOUNT_ACTIVITY: "Total Funds Paid and Received",
    RobinhoodStatementSection.PENDING_TRADES: "Total Executed Trades Pending Settlement",
}

# The holdings tables' heading. It is not a ``RobinhoodStatementSection``:
# holdings rows record their holdings label's section.
_PORTFOLIO_HEADING: Final[str] = "Portfolio Summary"
_CONTENT_HEADINGS: Final[frozenset[str]] = frozenset(
    {_PORTFOLIO_HEADING, *(section.value for section in RobinhoodStatementSection)}
)
_HOLDINGS_LABELS: Final[tuple[str, ...]] = (
    RobinhoodStatementSection.SECURITIES.value,
    RobinhoodStatementSection.LOANED_SECURITIES.value,
)
_ALLOCATION_LABELS: Final[tuple[str, ...]] = ("Cash and Cash Equivalents", "Equities", "Options")
_ACCOUNT_SUMMARY_COLUMNS: Final[tuple[str, ...]] = ("Opening Balance", "Closing Balance")
_ACCOUNT_SUMMARY_ROWS: Final[dict[RobinhoodStatementLayout, tuple[str, ...]]] = {
    RobinhoodStatementLayout.DEPOSIT_SWEEP: (
        "Brokerage Cash Balance",
        "Deposit Sweep Balance",
        "Total Securities *",
        "Portfolio Value",
    ),
    RobinhoodStatementLayout.LEGACY_NET_ACCOUNT: (
        "Net Account Balance",
        "Total Securities *",
        "Portfolio Value",
    ),
}
_INCOME_LABELS: Final[tuple[str, ...]] = (
    "Dividends",
    "Capital Gains Distributions",
    "Interest Earned",
    "Stock Lending",
)
_INCOME_COLUMNS: Final[tuple[str, ...]] = ("This Period", "Year to Date")
_SECURITIES_TOTAL: Final[str] = "Total Securities"
_PORTFOLIO_TOTAL: Final[str] = "Total Priced Portfolio"
_HOLDINGS_TOTAL_MALFORMED: Final[str] = "holdings total row is malformed"
_PORTFOLIO_CASH_LABELS: Final[tuple[str, ...]] = ("Brokerage Cash Balance", "Deposit Sweep Balance")
# Holdings totals are handled separately. The other prefixes end the rows
# only where they start a table; otherwise they refuse.
_POSITION_END_MARKERS: Final[tuple[str, ...]] = (
    "Total Securities",
    "Total Priced Portfolio",
    RobinhoodStatementSection.ACCOUNT_ACTIVITY.value,
    RobinhoodStatementSection.PENDING_TRADES.value,
    "Stock Lending -",
    "Deposit Sweep",
    "Important Information",
)
_TRAILING_DESCRIPTION_PREFIXES: Final[tuple[str, ...]] = (
    "CUSIP:",
    "Estimated Yield:",
    "Weighted Average Return Rate:",
    "Dividend Reinvestment",
    "Cash Div:",
    "ADR Fee:",
)
_OVERFLOW_PAGE: Final[int] = 2
_SUMMARY_VALUE_COUNT: Final[int] = 2
_SECURITIES_TOTAL_MONEY_CELLS: Final[int] = 2
_BANK_NAME_FRAGMENT_MAX_WORDS: Final[int] = 6


@dataclass(slots=True)
class _ActivityBuilder:
    location: SourceLocation
    descriptions: list[str]
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

    def finish(self) -> RobinhoodStatementActivity:
        """Build the row from its final description.

        Descriptions may contain zero or one complete identity. Each required
        column label appears exactly once. An activity row's identity is its
        Internal Security Transfer pair, ``Internal Security Transfer from
        <digits> to <digits>``, each account number a whole token.

        A ``BTO`` or ``STC`` row's description holds exactly one complete
        option contract, read like a holdings contract (``_option_match``).
        On Account Activity its symbol is the row's Symbol cell; the pending
        table prints no Symbol, so reconciliation compares it with the trade's.
        Any other row refuses a complete contract, but not a bare Call or Put.

        ``_activity_pages`` read every CUSIP line whole and refused a malformed
        one, so each is ``CUSIP: `` and the nine-character CUSIP; ``cusips``
        holds them in printed order.
        """

        description = "\n".join(part for part in self.descriptions if part)
        contract = self._contract(description)
        if not description:
            _fail(
                page=self.location.page,
                message="activity row has no source description",
            )
        transfer = _transfer_pair(description, page=self.location.page)
        cusips = tuple(match.group("cusip") for match in _CUSIP_RE.finditer(description))
        return RobinhoodStatementActivity(
            location=self.location,
            description=description,
            symbol=self.symbol,
            account_type=self.account_type,
            transaction_code=self.transaction_code,
            transaction_date=self.transaction_date,
            settle_date=self.settle_date,
            quantity=self.quantity,
            quantity_suffix=self.quantity_suffix,
            price_usd=self.price_usd,
            debit_usd=self.debit_usd,
            credit_usd=self.credit_usd,
            cusips=cusips,
            source_account_reference=transfer[0] if transfer else None,
            destination_account_reference=transfer[1] if transfer else None,
            option_symbol=contract[0] if contract else None,
            option_expiration=contract[1] if contract else None,
            option_right=contract[2] if contract else None,
            option_strike_usd=contract[3] if contract else None,
        )

    def _contract(self, description: str) -> tuple[str, date, str, Decimal] | None:
        page = self.location.page
        if self.transaction_code not in _OPTION_TRADE_CODES:
            if _OPTION_RE.search(description) is not None:
                _fail(
                    page=page,
                    column="Description",
                    message="activity row holds an option contract but is not an option trade",
                )
            return None
        match = _option_match(description, page=page, row="activity")
        if match is None:
            _fail(
                page=page,
                column="Description",
                message="option activity row has no option contract",
            )
        symbol = match.group("symbol")
        if (
            self.location.section is RobinhoodStatementSection.ACCOUNT_ACTIVITY
            and symbol != self.symbol
        ):
            _fail(
                page=page,
                column="Description",
                message="activity option contract symbol is not the row's Symbol",
            )
        try:
            fields = _option_fields(match, page=page)
        except ParseError:
            fields = None
        # Refused outside the handler, so the inner refusal isn't chained.
        if fields is None:
            _fail(
                page=page,
                column="Description",
                message="activity option contract is malformed",
            )
        return (symbol, *fields)


@dataclass(slots=True)
class _StatementParts:
    income: list[RobinhoodStatementIncome] = field(default_factory=list)
    positions: list[RobinhoodStatementPosition] = field(default_factory=list)
    activities: list[RobinhoodStatementActivity] = field(default_factory=list)
    sweep_banks: list[RobinhoodStatementSweepBank] = field(default_factory=list)
    sweep_activity: list[RobinhoodStatementSweepActivity] = field(default_factory=list)
    stock_loaned: list[RobinhoodStockLoanedSecurity] = field(default_factory=list)
    stock_loans: list[RobinhoodStockLoanActivity] = field(default_factory=list)
    stock_collateral: list[RobinhoodStockCollateralActivity] = field(default_factory=list)
    totals: list[RobinhoodStatementTotal] = field(default_factory=list)


def _fail(*, page: int = 0, column: str | None = None, message: str) -> NoReturn:
    raise ParseError(
        section=_SECTION,
        row_index=page,
        column=column,
        message=message,
        raw_value="",
    )


def _date(raw: str, *, page: int, column: str = "Date") -> date:
    if _DATE_TOKEN_RE.fullmatch(raw) is None:
        _fail(page=page, column=column, message="date has an unsupported format")
    month, day, year = (int(part) for part in raw.split("/"))
    with contextlib.suppress(ValueError):
        return date(year, month, day)
    # Refused outside the handler, so no chained error names the date's parts.
    _fail(page=page, column=column, message="date is not a valid calendar date")


def _within_limits(value: Decimal, kind: NumberKind, *, page: int, column: str) -> Decimal:
    breach = number_limit_breach(value, kind)
    if breach is not None:
        _fail(page=page, column=column, message=breach)
    return value


def _decimal(raw: str, *, page: int, column: str) -> Decimal:
    """A share count."""
    value = raw.strip()
    if _NUMBER_TOKEN_RE.fullmatch(value) is None:
        _fail(page=page, column=column, message="number has an unsupported format")
    return _within_limits(
        Decimal(value.replace(",", "")), NumberKind.QUANTITY, page=page, column=column
    )


def _money(raw: str, *, page: int, column: str, kind: NumberKind = NumberKind.MONEY) -> Decimal:
    value = raw.strip()
    if _MONEY_TOKEN_RE.fullmatch(value) is None:
        _fail(page=page, column=column, message="money has an unsupported format")
    negative = value.startswith("(") or value.startswith("-")
    digits = (
        value.removeprefix("(")
        .removesuffix(")")
        .removeprefix("-")
        .removeprefix("$")
        .replace(",", "")
    )
    parsed = Decimal(digits)
    # copy_negate, not unary minus: the minus would round to the context.
    signed = parsed.copy_negate() if negative else parsed
    return _within_limits(signed, kind, page=page, column=column)


def _money_or_none(
    raw: str, *, page: int, column: str, kind: NumberKind = NumberKind.MONEY
) -> Decimal | None:
    value = raw.strip()
    if value in {"", "-", "N/A"}:
        return None
    return _money(value, page=page, column=column, kind=kind)


def _quantity(raw: str, *, page: int) -> tuple[Decimal | None, str | None]:
    value = raw.strip()
    if value in {"", "-", "N/A"}:
        return None, None
    suffix = "S" if value.endswith("S") else None
    numeric = value.removesuffix("S")
    return _decimal(numeric, page=page, column="Qty"), suffix


def _percent(raw: str, *, page: int) -> Decimal:
    value = raw.strip()
    if _PERCENT_TOKEN_RE.fullmatch(value) is None:
        _fail(page=page, column="Percent", message="percentage has an unsupported format")
    return _within_limits(
        Decimal(value.removesuffix("%")), NumberKind.RATE, page=page, column="Percent"
    )


def _transfer_pair(description: str, *, page: int) -> tuple[str, str] | None:
    """Read an activity description's one transfer pair, or ``None`` without one.

    A second identity refuses whether it agrees or not, and so does one that
    isn't a whole pair. The refusals hold no account numbers.
    """

    phrases = list(_TRANSFER_PHRASE_RE.finditer(description))
    if not phrases:
        return None
    if len(phrases) > 1:
        _fail(
            page=page,
            column="Description",
            message="activity description holds more than one transfer identity",
        )
    match = _TRANSFER_RE.match(description, phrases[0].start())
    if match is None:
        _fail(page=page, column="Description", message="activity transfer identity is malformed")
    return match.group("source"), match.group("destination")


def _option_match(description: str, *, page: int, row: str) -> re.Match[str] | None:
    """Find a description's one option contract, or ``None`` without one.

    The contract is ``<symbol> <M/D/YYYY> <Call|Put> $<strike>``: the symbol a
    whole token, the strike grouped like any statement amount, with two
    decimals. A second candidate refuses whether it agrees or not, and so does
    one that isn't a whole contract. ``row`` names the row kind in the refusals.
    """

    candidates = _OPTION_WORD_RE.findall(description)
    if not candidates:
        return None
    if len(candidates) > 1:
        _fail(
            page=page,
            column="Description",
            message=f"{row} description holds more than one option contract",
        )
    match = _OPTION_RE.search(description)
    if match is None:
        _fail(page=page, column="Description", message=f"{row} option contract is malformed")
    return match


def _option_fields(match: re.Match[str], *, page: int) -> tuple[date, str, Decimal]:
    return (
        _date(match.group("expiration"), page=page, column="Description"),
        match.group("right"),
        _within_limits(
            Decimal(match.group("strike").replace(",", "")),
            NumberKind.PRICE,
            page=page,
            column="Description",
        ),
    )


def _option_contract(
    description: str, symbol: str, *, page: int
) -> tuple[date, str, Decimal] | None:
    """Read a holdings description's one option contract, or ``None`` without one.

    The contract follows ``_option_match``, its symbol equal to the row's
    ``Sym/Cusip`` cell.
    """

    match = _option_match(description, page=page, row="position")
    if match is None:
        return None
    if match.group("symbol") != symbol:
        _fail(
            page=page,
            column="Description",
            message="position option contract symbol is not the row's Sym/Cusip",
        )
    return _option_fields(match, page=page)


def _half_ulp(raw: str) -> Decimal:
    value = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    value = value.removeprefix("(").removesuffix(")").removeprefix("-")
    if "." not in value:
        return Decimal("0")
    places = len(value.partition(".")[2])
    return Decimal((0, (5,), -places - 1))


def _market_value_tolerance(
    quantity_cell: str,
    price_cell: str,
    quantity: Decimal,
    price: Decimal,
    *scale: Decimal,
    add: Callable[[Iterable[Decimal]], Decimal],
    multiply: Callable[..., Decimal],
) -> Decimal:
    """How far a row's quantity times price may sit from its market value.

    Half the last printed place for cells with a decimal point, zero otherwise,
    weighted by the other factor and ``scale``, plus a cent. The callers pass
    the factors and the exact ``add`` and ``multiply`` they check with.
    """
    return add(
        (
            multiply(_half_ulp(quantity_cell), price, *scale),
            multiply(_half_ulp(price_cell), quantity, *scale),
            Decimal("0.01"),
        )
    )


def _exact_sum(
    terms: Iterable[Decimal],
    *,
    at: str,
    page: int = 0,
    column: str | None = None,
    start: Decimal = Decimal(),
) -> Decimal:
    """Add ``terms`` to ``start`` with every digit kept.

    Where it can't be had, refuses as ``"<at> exceeds the supported precision"``
    at ``page`` and ``column``; ``at`` is fixed text naming the check.
    """
    return exactly(
        _add, start, *terms, refusal=partial(_precision_error, at=at, page=page, column=column)
    )


def _exact_product(*factors: Decimal, at: str, page: int = 0, column: str | None = None) -> Decimal:
    """The product of ``factors`` with every digit kept, refusing as :func:`_exact_sum`."""
    return exactly(
        _multiply, *factors, refusal=partial(_precision_error, at=at, page=page, column=column)
    )


def _add(start: Decimal, *terms: Decimal) -> Decimal:
    return sum(terms, start)


def _multiply(*factors: Decimal) -> Decimal:
    return reduce(operator.mul, factors)


def _precision_error(_out_of_memory: bool, *, at: str, page: int, column: str | None) -> ParseError:
    return ParseError(
        section=_SECTION,
        row_index=page,
        column=column,
        message=f"{at} exceeds the supported precision",
        raw_value="",
    )


def _split_pages(text: str) -> list[str]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def _has_crypto_title(page: str) -> bool:
    lines = iter(stripped for line in page.splitlines() if (stripped := line.strip()))
    title, month = next(lines, ""), next(lines, "")
    return (
        title.casefold() == _CRYPTO_TITLE.casefold()
        and _CRYPTO_MONTH_RE.fullmatch(month) is not None
    )


def _is_crypto(text: str) -> bool:
    """Identify a Robinhood Crypto statement by its own statement family.

    Every page of a Crypto statement opens with a ``Crypto Statement`` title
    and an ``MM-YYYY`` line, and its text names Robinhood Crypto; letter case
    is ignored. Text that has a Crypto title page and also names Robinhood
    Securities or Robinhood Financial, as every brokerage statement does,
    refuses as mixed. Anything else, including brokerage text that only
    mentions Robinhood Crypto, is not Crypto.
    """

    pages = _split_pages(text)
    titled = sum(_has_crypto_title(page) for page in pages)
    if not titled:
        return False
    folded = text.casefold()
    if any(entity.casefold() in folded for entity in _BROKERAGE_ENTITIES):
        _fail(message="statement mixes the Robinhood Crypto and brokerage statement families")
    return titled == len(pages) and _CRYPTO_MARKER.casefold() in folded


def _is_overflow_page(page_number: int, page: str) -> bool:
    """Whether ``page`` is page 2 opening with the ``Total Securities`` footnote.

    Its opener may wrap at any word, with blank lines and the whole-line
    page marker anywhere among its lines.
    That page may print no page marker. ``_check_fixed_pages`` reads it whole.
    """

    return page_number == _OVERFLOW_PAGE and " ".join(
        content
        for line in page.splitlines()
        if (content := line.strip()) and _PAGE_RE.fullmatch(content) is None
    ).startswith(_OVERFLOW_MARKER)


def _disclosure_heading(lines: list[str]) -> int | None:
    """The index of the page's disclosure heading, or ``None`` on another page.

    That heading is the whole line ``Important Information``, as the page's
    first line apart from blank lines and the page marker.
    """

    first = next(
        (
            index
            for index, line in enumerate(lines)
            if (content := line.strip()) and _PAGE_RE.fullmatch(content) is None
        ),
        None,
    )
    if first is None or lines[first].strip() != _DISCLOSURE_MARKER:
        return None
    return first


def _pages(text: str) -> tuple[str, ...]:
    if _is_crypto(text):
        _fail(message="Robinhood Crypto statements are not supported by the brokerage parser")
    pages = _split_pages(text)
    if not pages or any(not page.strip() for page in pages):
        _fail(message="statement page structure is empty or malformed")
    result = tuple(pages)
    total = len(result)
    for physical, page in enumerate(result, start=1):
        markers = list(_PAGE_RE.finditer(page))
        if not markers:
            if _is_overflow_page(physical, page):
                continue
            _fail(page=physical, message="statement page marker is missing")
        if len(markers) != 1:
            _fail(page=physical, message="statement page marker is duplicated")
        marker = markers[0]
        if int(marker.group("number")) != physical or int(marker.group("total")) != total:
            _fail(page=physical, message="statement page marker does not match physical pagination")
    return result


def _line_has_heading(line: str, heading: str) -> bool:
    return line.strip().startswith(heading)


def _page_has_heading(page: str, heading: str) -> bool:
    return any(_line_has_heading(line, heading) for line in page.splitlines())


def _is_content_heading(line: str) -> bool:
    return any(_line_has_heading(line, heading) for heading in _CONTENT_HEADINGS)


def _is_page_marker(content: str, *, page: int) -> bool:
    """Whether a stripped table line is the page's marker.

    Other text on the marker's line would be skipped with it, so that line
    refuses.
    """

    if _PAGE_RE.search(content) is None:
        return False
    if _PAGE_RE.fullmatch(content) is None:
        _fail(page=page, message="page marker line carries other text")
    return True


def _has_labels(line: str, labels: tuple[str, ...]) -> bool:
    return all(label in line for label in labels)


def _is_position_header(line: str) -> bool:
    """A holdings header, which prints its heading and its columns on one line."""

    return line.strip().startswith(_HOLDINGS_LABELS) and _has_labels(
        line, ("Sym/Cusip", "Acct Type")
    )


def _is_activity_header(line: str) -> bool:
    return (
        _has_labels(line, ("Description", "Transaction", "Date", "Debit", "Credit"))
        and "Balance" not in line
        and "Shares Lent" not in line
    )


def _is_bank_header(line: str) -> bool:
    return re.sub(r"[ \t]+", " ", line).strip() == "Bank Balance"


_TABLE_HEADERS: Final[dict[str, Callable[[str], bool]]] = {
    _PORTFOLIO_HEADING: _is_position_header,
    RobinhoodStatementSection.ACCOUNT_ACTIVITY.value: _is_activity_header,
    RobinhoodStatementSection.PENDING_TRADES.value: _is_activity_header,
    RobinhoodStatementSection.SWEEP_BANKS.value: _is_bank_header,
    RobinhoodStatementSection.SWEEP_ACTIVITY.value: partial(_has_labels, labels=_LEDGER_LABELS),
    RobinhoodStatementSection.STOCK_LOANED_SECURITIES.value: partial(
        _has_labels, labels=_LOANED_LABELS
    ),
    RobinhoodStatementSection.STOCK_LOAN_ACTIVITY.value: partial(_has_labels, labels=_LOAN_LABELS),
    RobinhoodStatementSection.STOCK_COLLATERAL_ACTIVITY.value: partial(
        _has_labels, labels=_LEDGER_LABELS
    ),
}
# Each table's one approved intro sentence, which may be printed between its
# heading and its column header: its fixed wording, split where a digit slot
# (``_DIGIT_SLOT``) goes. Empty where none is approved.
_INTRO_SENTENCES: Final[dict[str, tuple[str, ...]]] = {
    **dict.fromkeys(_TABLE_HEADERS, ()),
    RobinhoodStatementSection.PENDING_TRADES.value: (
        "These transactions may not be reflected in the other summaries",
    ),
    RobinhoodStatementSection.SWEEP_ACTIVITY.value: (
        "These transactions reflect movements of uninvested cash to and from program banks.",
    ),
    RobinhoodStatementSection.STOCK_LOAN_ACTIVITY.value: (
        "These transactions are specific to Stock Lending and represent all loan activity "
        "this month.",
    ),
    RobinhoodStatementSection.STOCK_COLLATERAL_ACTIVITY.value: (
        "These transactions reflect daily movements of collateral to and from Stock Lending "
        "custodian banks.",
    ),
    RobinhoodStatementSection.SWEEP_BANKS.value: (
        "These are the program banks where your uninvested cash is held as of ",
        ". Balances are insured by FDIC, not SIPC.",
    ),
    RobinhoodStatementSection.STOCK_LOANED_SECURITIES.value: (
        "These transactions are specific to Stock Lending and represent the securities on loan "
        "as of ",
        "",
    ),
}
# One run of digits with its separators, such as a date or an amount.
_DIGIT_SLOT: Final[str] = r"[0-9]+(?:[.,/][0-9]+)*"


def _is_intro(content: str, heading: str) -> bool:
    """Whether the stripped line ``content`` is ``heading``'s approved intro sentence.

    It is the sentence's fixed wording exactly, with one run of digits in
    each slot.
    """

    parts = _INTRO_SENTENCES[heading]
    pattern = _DIGIT_SLOT.join(re.escape(part) for part in parts)
    return bool(parts) and re.fullmatch(pattern, content) is not None


def _table_heading(content: str) -> str | None:
    """The table whose heading is the whole stripped line ``content``.

    That is ``<heading>`` or ``<heading> (continued)``.
    """

    return next(
        (heading for heading in _TABLE_HEADERS if content in {heading, f"{heading} (continued)"}),
        None,
    )


def _require_column_header(lines: list[str], index: int, heading: str, *, page: int) -> None:
    """Refuse unless the ``heading`` at ``lines[index]`` is followed by its column header.

    Only blank lines, the whole-line page marker and at most one intro line,
    the table's approved sentence after trimming (``_is_intro``), may come
    between.
    """

    intro = False
    for line in lines[index + 1 :]:
        content = line.strip()
        if not content or _is_page_marker(content, page=page):
            continue
        if _TABLE_HEADERS[heading](line):
            return
        if intro or not _is_intro(content, heading):
            break
        intro = True
    _fail(page=page, message=f"{heading} heading is not followed by its column header")


def _starts_table(lines: list[str], index: int, *, page: int) -> bool:
    """Whether ``lines[index]`` starts a table that its own reader reads.

    That is a holdings header, or a table heading, the whole line ``<heading>``
    or ``<heading> (continued)``, followed by that table's column header. Only
    blank lines, the whole-line page marker and at most one intro line, the
    table's approved sentence after trimming, may come between. A heading
    followed by anything else refuses.
    """

    content = lines[index].strip()
    if _is_position_header(content):
        return True
    heading = _table_heading(content)
    if heading is None:
        return False
    _require_column_header(lines, index, heading, page=page)
    return True


def _after_total(
    lines: list[str],
    start: int,
    *,
    page: int,
    table: str,
    label: str,
    footnote: tuple[str, ...] = (),
) -> None:
    """Refuse anything printed below a table's final total, from ``lines[start]``.

    Up to the next table on the page, or the page end, only blank lines and
    the page marker may follow. A table with a fixed ``footnote`` may also
    print it once, its lines adjacent and in order, as the page's last
    non-blank lines.
    """

    for index in range(start, len(lines)):
        content = lines[index].strip()
        if not content or _is_page_marker(content, page=page):
            continue
        if content.startswith(label):
            _fail(page=page, column=label, message=f"{table} total is repeated")
        end = index + len(footnote)
        if (
            footnote
            and tuple(line.strip() for line in lines[index:end]) == footnote
            and not any(line.strip() for line in lines[end:])
        ):
            return
        if _starts_table(lines, index, page=page):
            return
        _fail(page=page, column=label, message=f"{table} total is followed by an unsupported line")


def _check_table_headings(pages: tuple[str, ...]) -> None:
    """Refuse a heading line, or headingless holdings header, that leaves text unread.

    A line that opens with a table heading (``_TABLE_HEADERS``) must be the
    whole heading, printed once on its page and followed by its column header
    (``_starts_table``). Outside the exact opening overflow footnote, a line
    that opens with a holdings label, ``Securities
    Held in Account`` or ``Loaned Securities``, must be a holdings header
    (``_is_position_header``), which ``_position_pages`` reads whole. A line
    that opens with ``Important Information`` must be its page's disclosure
    heading (``_disclosure_heading``). Above a page's first table, only blank
    lines and the page marker may appear, plus page 1's identity block and
    summaries when that table opens with its heading. Each later table on the
    page starts where the reader of the table above it stops.
    """

    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        disclosure = _disclosure_heading(lines)
        heading_lines = lines
        if disclosure is None and _is_overflow_page(page_number, page):
            heading_lines = _after_footnote(
                _printed_lines(lines, page=page_number), page=page_number
            )
        seen: set[str] = set()
        for index, line in enumerate(heading_lines):
            content = line.strip()
            if content.startswith(_DISCLOSURE_MARKER) and index != disclosure:
                _fail(
                    page=page_number,
                    message=(
                        f"{_DISCLOSURE_MARKER} heading is not its page's first line"
                        if content == _DISCLOSURE_MARKER
                        else f"{_DISCLOSURE_MARKER} heading line carries other text"
                    ),
                )
            label = next((label for label in _HOLDINGS_LABELS if content.startswith(label)), None)
            if label is not None and not _is_position_header(content):
                _fail(page=page_number, message=f"{label} line is not a holdings header")
            heading = next(
                (heading for heading in _TABLE_HEADERS if content.startswith(heading)), None
            )
            if heading is None:
                continue
            if _table_heading(content) != heading:
                _fail(page=page_number, message=f"{heading} heading line carries other text")
            if heading in seen:
                _fail(page=page_number, message=f"{heading} heading is repeated on the page")
            seen.add(heading)
        # Every table start, so every heading is checked for its column header.
        starts = [
            index for index in range(len(lines)) if _starts_table(lines, index, page=page_number)
        ]
        if not starts:
            continue
        first = starts[0]
        heading = _table_heading(lines[first].strip())
        if heading is not None and page_number == 1:
            continue
        if any(
            line.strip() and not _is_page_marker(line.strip(), page=page_number)
            for line in lines[:first]
        ):
            subject = "holdings header" if heading is None else f"{heading} heading"
            _fail(page=page_number, message=f"{subject} is preceded by an unsupported line")


def _validate_page_families(pages: tuple[str, ...]) -> tuple[int, ...]:
    """Admit each page by its family, and return the disclosure pages.

    A disclosure page opens with its heading (``_disclosure_heading``). Page 2
    may be the overflow page (``_is_overflow_page``). Any other page needs a
    line that starts with a content heading (``_CONTENT_HEADINGS``), each read
    by its reader or refused (``_check_table_headings``). The disclosure and
    overflow pages are read by ``_check_fixed_pages``.
    """

    disclosure_pages: list[int] = []
    for page_number, page in enumerate(pages, start=1):
        if _disclosure_heading(page.splitlines()) is not None:
            disclosure_pages.append(page_number)
        elif not _is_overflow_page(page_number, page) and not any(
            _page_has_heading(page, heading) for heading in _CONTENT_HEADINGS
        ):
            _fail(page=page_number, message="statement page has no accepted content family")
    return tuple(disclosure_pages)


def _holds_row_cell(content: str, *, percent: bool) -> bool:
    """Whether ``content`` holds a money amount or a date, or, with ``percent``, a ``%``.

    Every table row this parser reads holds a money amount or a date.
    """

    return (
        _MONEY_TOKEN_RE.search(content) is not None
        or _DATE_TOKEN_RE.search(content) is not None
        or (percent and "%" in content)
    )


def _check_fixed_pages(pages: tuple[str, ...]) -> None:
    """Refuse a disclosure or overflow page that could hold a table row.

    Both pages are accepted by layout without reading their prose. Their
    lines are taken trimmed, without blank lines and the whole-line page
    marker. Below a disclosure page's heading, no line may hold a money
    amount, a date or a ``%``; column gaps are allowed, as the page prints
    in two columns. The overflow page opens with the approved ``Total
    Securities`` footnote (``_PORTFOLIO_FOOTNOTE``), its words wrapped onto
    any number of whole lines, and no line after it may hold a money amount
    or a date (``_holds_row_cell``).
    """

    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        heading = _disclosure_heading(lines)
        if heading is not None:
            printed = _printed_lines(lines[heading + 1 :], page=page_number)
            if any(_holds_row_cell(content, percent=True) for content in printed):
                _fail(page=page_number, message="disclosure page holds an unsupported line")
        elif _is_overflow_page(page_number, page):
            printed = _after_footnote(_printed_lines(lines, page=page_number), page=page_number)
            if any(_holds_row_cell(content, percent=False) for content in printed):
                _fail(page=page_number, message="overflow page holds an unsupported line")


def _printed_lines(lines: list[str], *, page: int) -> list[str]:
    """The trimmed non-blank ``lines``, without the whole-line page marker."""

    return [
        content
        for line in lines
        if (content := line.strip()) and not _is_page_marker(content, page=page)
    ]


def _after_footnote(printed: list[str], *, page: int) -> list[str]:
    """The overflow page's lines after its opening ``Total Securities`` footnote.

    The page's first lines, joined by one blank, must be the footnote's text.
    """

    footnote = " ".join(_PORTFOLIO_FOOTNOTE)
    text = ""
    for index, content in enumerate(printed):
        text = f"{text} {content}" if text else content
        if text == footnote:
            return printed[index + 1 :]
        if not footnote.startswith(text):
            break
    _fail(page=page, message="overflow page does not open with the Total Securities footnote")


def _identity(
    pages: tuple[str, ...],
) -> tuple[
    RobinhoodStatementFamily,
    str | None,
    str,
    date,
    date,
    tuple[str, ...],
    str | None,
]:
    text = "\f".join(pages)
    accounts = list(_ACCOUNT_RE.finditer(text))
    if len(accounts) != 1:
        _fail(message="statement must contain exactly one page-1 account identity")
    account = accounts[0]
    if account.start() >= len(pages[0]):
        _fail(message="statement account identity must occur on physical page 1")
    label = account.group("label")
    if label == "Managed Individual":
        family = RobinhoodStatementFamily.MANAGED_INDIVIDUAL
    elif label == "Individual":
        family = RobinhoodStatementFamily.INDIVIDUAL
    else:
        family = RobinhoodStatementFamily.UNSPECIFIED

    periods = list(_PERIOD_RE.finditer(text))
    if len(periods) != 1:
        _fail(message="statement must contain exactly one reporting period")
    period = periods[0]
    start = _date(period.group("start"), page=1, column="Period")
    end = _date(period.group("end"), page=1, column="Period")
    if start > end:
        _fail(page=1, column="Period", message="statement period start is after its end")

    page1_lines = pages[0].splitlines()
    account_line = next(
        (index for index, line in enumerate(page1_lines) if _ACCOUNT_RE.fullmatch(line)),
        None,
    )
    period_line = next(
        (index for index, line in enumerate(page1_lines) if _PERIOD_RE.search(line)),
        None,
    )
    if account_line is None or period_line is None:
        _fail(page=1, message="page-1 identity block is malformed")
    period_on_line = _PERIOD_RE.search(page1_lines[period_line])
    assert period_on_line is not None
    right_column = period_on_line.start()
    registration = tuple(
        part for line in page1_lines[:account_line] if (part := line[:right_column].strip())
    )
    if not registration:
        _fail(page=1, message="statement registration block is missing")

    identifiers = [
        match.group("identifier")
        for line in page1_lines
        if (match := _DOCUMENT_ID_RE.search(line)) is not None
    ]
    if len(identifiers) > 1:
        _fail(page=1, message="statement document identifier is ambiguous")

    if "Robinhood Financial" not in text or "Robinhood Securities" not in text:
        _fail(message="statement RHF/RHS legal-role disclosure is missing")

    return (
        family,
        label,
        account.group("number"),
        start,
        end,
        registration,
        identifiers[0] if identifiers else None,
    )


def _numbered_lines(pages: tuple[str, ...]) -> Iterator[tuple[int, int, str]]:
    for page_number, page in enumerate(pages, start=1):
        for row, line in enumerate(page.splitlines(), start=1):
            yield page_number, row, line


def _heading_row(pages: tuple[str, ...], heading: str) -> tuple[int, int]:
    """Locate the statement's one line that opens with ``heading``."""

    found = [
        (page_number, row)
        for page_number, row, line in _numbered_lines(pages)
        if _line_has_heading(line, heading)
    ]
    if not found:
        _fail(page=1, message=f"{heading} is missing")
    if len(found) > 1:
        _fail(page=found[1][0], message=f"{heading} is duplicated")
    return found[0]


def _usd(raw: str, *, page: int, column: str, message: str) -> Decimal:
    if _USD_CELL_RE.fullmatch(raw) is None:
        _fail(page=page, column=column, message=message)
    return _money(raw, page=page, column=column)


def _is_allocation_text(text: str) -> bool:
    """An allocation legend label, a percentage, or a label and its percentage."""

    for label in _ALLOCATION_LABELS:
        if text == label:
            return True
        if text.startswith(label) and text[len(label) :][:1].isspace():
            text = text[len(label) :].strip()
            break
    return _PERCENT_TOKEN_RE.fullmatch(text) is not None


def _summary_cells(
    content: str, label: str, *, page: int, message: str, legend: bool
) -> tuple[str, str]:
    """Return a summary row's two value cells and refuse anything else in the row
    with ``message``.

    The Account Summary's allocation legend shares its lines, to the right of
    the rows, so there (``legend``) one trailing legend label or percentage
    belongs to the legend, not the row.
    """

    tail = content[len(label) :]
    cells = tail.split(maxsplit=_SUMMARY_VALUE_COUNT)
    trailing = cells[_SUMMARY_VALUE_COUNT] if len(cells) > _SUMMARY_VALUE_COUNT else ""
    if (
        not tail[:1].isspace()
        or len(cells) < _SUMMARY_VALUE_COUNT
        or (
            trailing
            and not (
                legend and (trailing in _ALLOCATION_LABELS or _PERCENT_TOKEN_RE.fullmatch(trailing))
            )
        )
    ):
        _fail(page=page, column=label, message=message)
    return cells[0], cells[1]


def _allocation_percent(lines: list[str], label: str, *, page: int = 1) -> tuple[int, Decimal]:
    """Return the line index and value of ``label``'s percentage: the first one
    on its line or the two lines after it, before another label's line."""

    matches = [index for index, line in enumerate(lines) if label in line]
    if len(matches) != 1:
        _fail(page=page, message="Portfolio Summary allocation label is missing or duplicated")
    start = matches[0]
    other_labels = set(_ALLOCATION_LABELS) - {label}
    for index in range(start, min(start + 3, len(lines))):
        line = lines[index]
        if index != start and any(other in line for other in other_labels):
            break
        tokens = _PERCENT_TOKEN_RE.findall(line)
        if tokens:
            if len(tokens) != 1:
                _fail(page=page, message="Portfolio Summary allocation is ambiguous")
            return index, _percent(tokens[0], page=page)
    _fail(page=page, message="Portfolio Summary allocation percentage is missing")


def _allocation_percents(block: list[tuple[int, str]]) -> tuple[Decimal, ...]:
    """Read the allocation legend: each label owns exactly one percentage.

    A percentage on any other line belongs to no label, or is a second one
    for a label, so it refuses.
    """

    lines = [line for _page_number, line in block]
    owned = [_allocation_percent(lines, label) for label in _ALLOCATION_LABELS]
    owned_lines = {index for index, _value in owned}
    for index, (page_number, line) in enumerate(block):
        if index not in owned_lines and _PERCENT_TOKEN_RE.search(line):
            _fail(page=page_number, message="Portfolio Summary allocation has an extra percentage")
    return tuple(value for _index, value in owned)


def _account_summary_rows(
    block: list[tuple[int, str]], *, page: int
) -> tuple[RobinhoodStatementLayout, dict[str, tuple[int, str]]]:
    """Find each Account Summary row once; every other line is the allocation legend."""

    labels = {label for rows in _ACCOUNT_SUMMARY_ROWS.values() for label in rows}
    rows: dict[str, tuple[int, str]] = {}
    for page_number, line in block:
        content = line.strip()
        label = next((label for label in labels if content.startswith(label)), None)
        if label is None:
            if content and not _is_allocation_text(content):
                _fail(page=page_number, message="Account Summary contains an unsupported row")
            continue
        if label in rows:
            _fail(page=page_number, column=label, message="Account Summary row is duplicated")
        rows[label] = (page_number, content)
    sweep = "Brokerage Cash Balance" in rows
    if sweep == ("Net Account Balance" in rows):
        _fail(page=page, message="Account Summary layout generation is unsupported")
    layout = (
        RobinhoodStatementLayout.DEPOSIT_SWEEP
        if sweep
        else RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
    )
    expected = _ACCOUNT_SUMMARY_ROWS[layout]
    for label, (page_number, _content) in rows.items():
        if label not in expected:
            _fail(
                page=page_number,
                column=label,
                message="Account Summary contains an unsupported row",
            )
    for label in expected:
        if label not in rows:
            _fail(page=page, column=label, message="Account Summary row is missing")
    return layout, rows


def _account_summary(
    pages: tuple[str, ...],
    start: tuple[int, int],
    end: tuple[int, int],
) -> tuple[RobinhoodStatementLayout, RobinhoodAccountSummary]:
    """Read the Account Summary block from its heading line at ``start`` to ``end``.

    The heading line holds the heading and then only its column labels,
    ``Opening Balance`` and ``Closing Balance``, in order and separated by
    blanks. The block is the lines between it and ``end``.
    """

    heading_page, heading_row = start
    _column_starts(
        pages[heading_page - 1].splitlines()[heading_row - 1],
        _ACCOUNT_SUMMARY_COLUMNS,
        page=heading_page,
        heading=RobinhoodStatementSection.ACCOUNT_SUMMARY.value,
    )
    block = [
        (page_number, line)
        for page_number, row, line in _numbered_lines(pages)
        if start < (page_number, row) < end
    ]
    layout, rows = _account_summary_rows(block, page=start[0])
    expected = _ACCOUNT_SUMMARY_ROWS[layout]
    values: dict[str, tuple[Decimal | None, Decimal]] = {}
    message = "Account Summary row is malformed"
    for label in expected:
        page_number, content = rows[label]
        opening_raw, closing_raw = _summary_cells(
            content, label, page=page_number, message=message, legend=True
        )
        values[label] = (
            None
            if opening_raw == "N/A"
            else _usd(opening_raw, page=page_number, column=label, message=message),
            _usd(closing_raw, page=page_number, column=label, message=message),
        )
    cash_label = expected[0]
    opening_cash, closing_cash = values[cash_label]
    if layout is RobinhoodStatementLayout.DEPOSIT_SWEEP:
        opening_sweep, closing_sweep = values["Deposit Sweep Balance"]
    else:
        opening_sweep = None if opening_cash is None else Decimal("0")
        closing_sweep = Decimal("0")
    opening_securities, closing_securities = values["Total Securities *"]
    opening_portfolio, closing_portfolio = values["Portfolio Value"]
    percentages = _allocation_percents(block)

    closing_sum = _exact_sum(
        (closing_sweep, closing_securities),
        start=closing_cash,
        at="closing Account Summary sum",
        page=1,
    )
    if closing_sum != closing_portfolio:
        _fail(page=1, message="closing Account Summary components do not equal portfolio value")
    opening_values = (opening_cash, opening_sweep, opening_securities, opening_portfolio)
    if all(value is not None for value in opening_values):
        assert opening_cash is not None
        assert opening_sweep is not None
        assert opening_securities is not None
        assert opening_portfolio is not None
        opening_sum = _exact_sum(
            (opening_sweep, opening_securities),
            start=opening_cash,
            at="opening Account Summary sum",
            page=1,
        )
        if opening_sum != opening_portfolio:
            _fail(page=1, message="opening Account Summary components do not equal portfolio value")
    elif any(value is not None for value in opening_values):
        _fail(page=1, message="opening Account Summary values mix N/A and numeric states")

    return layout, RobinhoodAccountSummary(
        source_cash_label=cash_label,
        opening_brokerage_cash_usd=opening_cash,
        closing_brokerage_cash_usd=closing_cash,
        opening_deposit_sweep_usd=opening_sweep,
        closing_deposit_sweep_usd=closing_sweep,
        opening_total_securities_usd=opening_securities,
        closing_total_securities_usd=closing_securities,
        opening_portfolio_value_usd=opening_portfolio,
        closing_portfolio_value_usd=closing_portfolio,
        closing_cash_percent=percentages[0],
        closing_equities_percent=percentages[1],
        closing_options_percent=percentages[2],
    )


def _income_paragraph(lines: list[str], start: int) -> None:
    """Accept, unread, the paragraph from ``lines[start]`` below page 1's last Income row.

    It runs over adjacent lines, up to a blank line, the page marker or the
    start of another table, and ends with the document-identifier line. No
    line in it, counting only the identifier line's text before its
    identifier, may hold a money amount or a cell after a column gap, or start
    with an Income label. Below it only blank lines, the page marker and the
    start of another table may follow, as below a table's total.
    """

    message = "Income and Expense Summary contains an unsupported row"
    end = next(
        (
            index
            for index in range(start, len(lines))
            if not (content := lines[index].strip())
            or _is_page_marker(content, page=1)
            or _starts_table(lines, index, page=1)
        ),
        len(lines),
    )
    identifier = _DOCUMENT_ID_RE.search(lines[end - 1])
    if identifier is None:
        _fail(page=1, message=message)
    texts = (
        *(line.strip() for line in lines[start : end - 1]),
        lines[end - 1][: identifier.start()].strip(),
    )
    if any(
        _MONEY_TOKEN_RE.search(text)
        or _GAP_TOKEN_RE.search(text)
        or text.startswith(_INCOME_LABELS)
        for text in texts
    ):
        _fail(page=1, message=message)
    for index in range(end, len(lines)):
        content = lines[index].strip()
        if _starts_table(lines, index, page=1):
            return
        if content and not _is_page_marker(content, page=1):
            _fail(page=1, message=message)


def _income(pages: tuple[str, ...], heading: tuple[int, int]) -> list[RobinhoodStatementIncome]:
    """Read the Income and Expense Summary block from its ``heading`` line.

    The heading line holds the heading and then only its column labels,
    ``This Period`` and ``Year to Date``, in order and separated by blanks.
    The block runs to the page end, or to a line that starts another table
    (``_starts_table``), whose own reader reads what follows. Each line in it
    is an Income row, a blank line or the whole-line page marker. On page 1,
    once the ``Stock Lending`` row, the last Income row, is read, the next
    other line opens the paragraph that ends at the document identifier
    (``_income_paragraph``), which ends the block. Anything else refuses.
    """

    page_number, heading_row = heading
    page_lines = pages[page_number - 1].splitlines()
    _column_starts(
        page_lines[heading_row - 1],
        _INCOME_COLUMNS,
        page=page_number,
        heading=RobinhoodStatementSection.INCOME_EXPENSE.value,
    )
    found: dict[str, tuple[int, str]] = {}
    for row, line in enumerate(page_lines[heading_row:], start=heading_row + 1):
        content = line.strip()
        if not content or _is_page_marker(content, page=page_number):
            continue
        if _starts_table(page_lines, row - 1, page=page_number):
            break
        label = next((label for label in _INCOME_LABELS if content.startswith(label)), None)
        if label is None and page_number == 1 and _INCOME_LABELS[-1] in found:
            _income_paragraph(page_lines, row - 1)
            break
        if label is None:
            _fail(
                page=page_number, message="Income and Expense Summary contains an unsupported row"
            )
        if label in found:
            _fail(
                page=page_number,
                column=label,
                message="Income and Expense Summary row is duplicated",
            )
        found[label] = (row, line)
    rows: list[RobinhoodStatementIncome] = []
    message = "Income and Expense Summary row is malformed"
    for label in _INCOME_LABELS:
        if label not in found:
            _fail(
                page=page_number,
                column=label,
                message="Income and Expense Summary row is missing",
            )
        row, line = found[label]
        this_period, year_to_date = _summary_cells(
            line.strip(),
            label,
            page=page_number,
            message=message,
            legend=False,
        )
        rows.append(
            RobinhoodStatementIncome(
                location=SourceLocation(
                    page=page_number,
                    section=RobinhoodStatementSection.INCOME_EXPENSE,
                    row=row,
                ),
                label=label,
                this_period_usd=_usd(this_period, page=page_number, column=label, message=message),
                year_to_date_usd=_usd(
                    year_to_date, page=page_number, column=label, message=message
                ),
            )
        )
    label_columns = {len(line) - len(line.lstrip()) for _row, line in found.values()}
    if len(label_columns) != 1:
        _fail(page=page_number, message="Income and Expense Summary rows are misaligned")
    return rows


def _column_starts(
    header: str, labels: tuple[str, ...], *, page: int, heading: str = ""
) -> tuple[int, ...]:
    """Find where each column starts in a table's column header.

    Descriptions may contain zero or one complete identity. Each required
    column label appears exactly once. A header contains only its column
    labels, in order and separated by blanks, and, for holdings, its supported
    section ``heading`` before them. A repeated label refuses naming that
    label; any other text refuses naming no column.
    """

    starts: list[int] = []
    gaps: list[str] = []
    cursor = 0
    for label in labels:
        try:
            position = header.index(label, cursor)
        except ValueError:
            _fail(page=page, message="table column header is malformed")
        gaps.append(header[cursor:position])
        starts.append(position)
        cursor = position + len(label)
    gaps.append(header[cursor:])
    for label, position in zip(labels, starts, strict=True):
        if any(
            match.start() != position
            for match in re.finditer(rf"(?<!\S){re.escape(label)}(?!\S)", header)
        ):
            _fail(page=page, column=label, message="table column label is repeated")
    lead = gaps[0].strip()
    if (
        (lead and (lead != heading or not gaps[0][-1:].isspace()))
        or not all(gap.isspace() for gap in gaps[1:-1])
        or gaps[-1].strip()
    ):
        _fail(page=page, message="table column header holds unsupported text")
    return tuple(starts)


def _cells(line: str, starts: tuple[int, ...]) -> tuple[str, ...]:
    ends: tuple[int | None, ...] = (*starts[1:], None)
    return tuple(line[start:end].strip() for start, end in zip(starts, ends, strict=True))


def _append_position_description(
    parts: _StatementParts,
    index: int,
    descriptions: list[str],
) -> None:
    previous = parts.positions[index]
    parts.positions[index] = replace(
        previous, description="\n".join((previous.description, *descriptions))
    )


def _holdings_total_cells(content: str, label: str, *, page: int) -> list[str]:
    tail = content[len(label) :]
    if not content.startswith(label) or not tail[:1].isspace():
        _fail(page=page, column=label, message=_HOLDINGS_TOTAL_MALFORMED)
    return tail.split()


def _securities_total(content: str, *, page: int) -> tuple[Decimal, ...]:
    """Read a ``Total Securities`` row whole.

    Its cells are the market value, the estimated dividend when printed, and
    the percentage of the portfolio. The label may carry its ``*`` footnote
    marker.
    """

    cells = _holdings_total_cells(content, _SECURITIES_TOTAL, page=page)
    if cells[:1] == ["*"]:
        cells = cells[1:]
    message = _HOLDINGS_TOTAL_MALFORMED
    match cells:
        case [*money, percent] if (
            0 < len(money) <= _SECURITIES_TOTAL_MONEY_CELLS
            and _PERCENT_TOKEN_RE.fullmatch(percent) is not None
        ):
            values = tuple(
                _usd(cell, page=page, column=_SECURITIES_TOTAL, message=message) for cell in money
            )
            _percent(percent, page=page)
            return values
        case _:
            _fail(page=page, column=_SECURITIES_TOTAL, message=message)


def _portfolio_total(
    lines: list[str],
    securities_row: int,
    *,
    page: int,
    securities_value: Decimal,
) -> int:
    """Read the rows from under ``Total Securities`` through ``Total Priced Portfolio``.

    Only the cash rows that ``Total Priced Portfolio`` adds to ``Total
    Securities`` may sit between the two. Returns the ``Total Priced
    Portfolio`` row number.
    """

    cash: dict[str, Decimal] = {}
    message = _HOLDINGS_TOTAL_MALFORMED
    for row, line in enumerate(lines[securities_row:], start=securities_row + 1):
        content = line.strip()
        if not content or _PAGE_RE.fullmatch(content):
            continue
        if content.startswith(_SECURITIES_TOTAL):
            _fail(page=page, column=_SECURITIES_TOTAL, message="holdings total is duplicated")
        if content.startswith(_PORTFOLIO_TOTAL):
            match _holdings_total_cells(content, _PORTFOLIO_TOTAL, page=page):
                case [value]:
                    total = _usd(value, page=page, column=_PORTFOLIO_TOTAL, message=message)
                case _:
                    _fail(page=page, column=_PORTFOLIO_TOTAL, message=message)
            add = partial(
                _exact_sum,
                at="Total Priced Portfolio sum",
                page=page,
                column=_PORTFOLIO_TOTAL,
            )
            covered = add((add(cash.values()),), start=securities_value)
            if total != covered:
                _fail(
                    page=page,
                    column=_PORTFOLIO_TOTAL,
                    message="holdings total does not equal the rows it covers",
                )
            return row
        label = next((label for label in _PORTFOLIO_CASH_LABELS if content.startswith(label)), None)
        if label is None:
            _fail(
                page=page,
                column=_PORTFOLIO_TOTAL,
                message="holdings total block contains an unsupported row",
            )
        if label in cash:
            _fail(page=page, column=label, message="holdings total block row is duplicated")
        match _holdings_total_cells(content, label, page=page):
            case [value, percent] if _PERCENT_TOKEN_RE.fullmatch(percent) is not None:
                cash[label] = _usd(value, page=page, column=label, message=message)
                _percent(percent, page=page)
            case _:
                _fail(page=page, column=label, message=message)
    _fail(page=page, column=_PORTFOLIO_TOTAL, message="holdings total is missing")


def _position_pages(  # noqa: PLR0912, PLR0915
    pages: tuple[str, ...],
    *,
    parts: _StatementParts,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Read the holdings tables and their one printed total.

    ``Total Securities`` closes the last holdings table and covers every
    holdings row, held and loaned. Returns the (page, row) of it and of
    ``Total Priced Portfolio``. A table's rows end only at its total or where
    another table starts (``_starts_table``): another holdings header, or a
    table heading. Any other line that starts with an ``_POSITION_END_MARKERS``
    prefix but starts no table refuses.

    Descriptions may contain zero or one complete identity. Each required
    column label appears exactly once. A holdings row's identity is its option
    contract, read from its final description, after every attached line. The
    contract's symbol must be the row's ``Sym/Cusip`` cell. A row whose
    description holds a contract is read as an option, with a multiplier of
    100, or refuses; it is never read as an equity.
    """

    securities: tuple[int, int] | None = None
    portfolio_row = 0
    last_page = 1
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        for header_index, header in enumerate(lines):
            if not _is_position_header(header):
                continue
            section = (
                RobinhoodStatementSection.SECURITIES
                if _line_has_heading(header, RobinhoodStatementSection.SECURITIES.value)
                else RobinhoodStatementSection.LOANED_SECURITIES
            )
            heading = section.value
            if securities is not None:
                _fail(
                    page=page_number,
                    column=_SECURITIES_TOTAL,
                    message="holdings total is misplaced",
                )
            last_page = page_number
            labels = (
                "Sym/Cusip",
                "Acct Type",
                "Qty",
                "Price",
                "Mkt Value",
                "Est. Dividend",
                "Yield",
                "% of Total Portfolio",
            )
            starts = _column_starts(header, labels, page=page_number, heading=heading)
            pending: list[str] = []
            last_index: int | None = None
            for line_index, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
                total_label = next(
                    (
                        marker
                        for marker in (_SECURITIES_TOTAL, _PORTFOLIO_TOTAL)
                        if _line_has_heading(line, marker)
                    ),
                    None,
                )
                if total_label is not None:
                    if pending:
                        if last_index is None:
                            _fail(
                                page=page_number,
                                message="position description has no source row",
                            )
                        _append_position_description(parts, last_index, pending)
                        pending.clear()
                    if total_label != _SECURITIES_TOTAL:
                        _fail(
                            page=page_number,
                            column=total_label,
                            message="holdings total is misplaced",
                        )
                    money = _securities_total(line.strip(), page=page_number)
                    covered = (
                        _exact_sum(
                            (row.market_value_usd for row in parts.positions),
                            at="holdings market value sum",
                            page=page_number,
                            column=_SECURITIES_TOTAL,
                        ),
                        _exact_sum(
                            (row.estimated_dividend_usd for row in parts.positions),
                            at="holdings estimated dividend sum",
                            page=page_number,
                            column=_SECURITIES_TOTAL,
                        ),
                    )
                    if money != covered[: len(money)]:
                        _fail(
                            page=page_number,
                            column=_SECURITIES_TOTAL,
                            message="holdings total does not equal the rows it covers",
                        )
                    parts.totals.append(
                        RobinhoodStatementTotal(
                            location=SourceLocation(
                                page=page_number,
                                section=section,
                                row=line_index,
                            ),
                            label=total_label,
                            values=tuple(
                                (f"Amount{index}", value)
                                for index, value in enumerate(money, start=1)
                            ),
                        )
                    )
                    securities = (page_number, line_index)
                    portfolio_row = _portfolio_total(
                        lines,
                        line_index,
                        page=page_number,
                        securities_value=money[0],
                    )
                    break
                sliced = _cells(line, starts)
                content = line.strip()
                if (
                    content.split(maxsplit=1)[:1] == ["Total"]
                    and any(character.isdigit() for character in content)
                    and not (
                        _NUMBER_TOKEN_RE.fullmatch(sliced[2].removesuffix("S"))
                        and _MONEY_TOKEN_RE.fullmatch(sliced[3])
                        and _MONEY_TOKEN_RE.fullmatch(sliced[4])
                    )
                ):
                    _fail(
                        page=page_number,
                        column="Total",
                        message="holdings total label is unsupported",
                    )
                if _starts_table(lines, line_index - 1, page=page_number):
                    if pending:
                        if last_index is None:
                            _fail(
                                page=page_number,
                                message="position description has no source row",
                            )
                        _append_position_description(parts, last_index, pending)
                        pending.clear()
                    break
                if any(_line_has_heading(line, marker) for marker in _POSITION_END_MARKERS):
                    _fail(
                        page=page_number,
                        message="holdings table holds a heading line that starts no table",
                    )
                quantity, quantity_suffix = _quantity(sliced[2], page=page_number)
                if quantity_suffix is not None:
                    _fail(
                        page=page_number,
                        column="Qty",
                        message="position quantity suffix is unsupported",
                    )
                price = _money_or_none(
                    sliced[3], page=page_number, column="Price", kind=NumberKind.PRICE
                )
                market_value = _money_or_none(sliced[4], page=page_number, column="Mkt Value")
                prefix = line[: starts[0]].strip()
                if quantity is None or price is None or market_value is None:
                    if not content or _is_page_marker(content, page=page_number):
                        continue
                    if last_index is not None and content.startswith(
                        _TRAILING_DESCRIPTION_PREFIXES
                    ):
                        _append_position_description(parts, last_index, [content])
                    elif any(sliced[2:]) or (
                        sliced[0] and sliced[1] and " " not in sliced[0] and " " not in sliced[1]
                    ):
                        _fail(
                            page=page_number,
                            message="position row is incomplete or column-shifted",
                        )
                    else:
                        if _OPTION_RE.search(content) and pending and last_index is not None:
                            _append_position_description(parts, last_index, pending)
                            pending.clear()
                        pending.append(content)
                    continue

                leading = [*pending]
                if last_index is not None and pending and prefix:
                    _append_position_description(parts, last_index, pending)
                    leading.clear()
                pending.clear()
                description = "\n".join(part for part in (*leading, prefix) if part)
                if not description:
                    _fail(page=page_number, message="position row has no source description")
                estimated = _money(sliced[5], page=page_number, column="Est. Dividend")
                yield_percent = _percent(sliced[6], page=page_number) if sliced[6] else None
                portfolio_percent = _percent(sliced[7], page=page_number)
                option = _option_contract(description, sliced[0], page=page_number)
                multiplier = STANDARD_OPTION_MULTIPLIER if option else Decimal("1")
                check = "position market value check"
                add = partial(_exact_sum, at=check, page=page_number, column="Mkt Value")
                multiply = partial(_exact_product, at=check, page=page_number, column="Mkt Value")
                tolerance = _market_value_tolerance(
                    sliced[2],
                    sliced[3],
                    quantity.copy_abs(),
                    price.copy_abs(),
                    multiplier,
                    add=add,
                    multiply=multiply,
                )
                product = multiply(quantity, price, multiplier)
                if add((product, market_value.copy_negate())).copy_abs() > tolerance:
                    _fail(
                        page=page_number,
                        column="Mkt Value",
                        message="position quantity and price do not reproduce market value",
                    )
                parts.positions.append(
                    RobinhoodStatementPosition(
                        location=SourceLocation(
                            page=page_number,
                            section=section,
                            row=line_index,
                        ),
                        description=description,
                        symbol_or_cusip=sliced[0],
                        account_type=sliced[1],
                        quantity=quantity,
                        price_usd=price,
                        market_value_usd=market_value,
                        estimated_dividend_usd=estimated,
                        yield_percent=yield_percent,
                        portfolio_percent=portfolio_percent,
                        option_expiration=option[0] if option else None,
                        option_right=option[1] if option else None,
                        option_strike_usd=option[2] if option else None,
                    )
                )
                last_index = len(parts.positions) - 1
            if pending:
                _fail(
                    page=page_number,
                    message="position description is not attached to a row",
                )
    if securities is None:
        _fail(page=last_page, column=_SECURITIES_TOTAL, message="holdings total is missing")
    # A row read with a contract keeps it: a second candidate attached later refuses.
    for row in parts.positions:
        page = row.location.page
        contract = _option_contract(row.description, row.symbol_or_cusip, page=page)
        if contract is not None and row.option_right is None:
            _fail(
                page=page,
                column="Description",
                message="position option contract is attached to a row read as an equity",
            )
    return securities, (securities[0], portfolio_row)


def _refuse_stray_totals(
    pages: tuple[str, ...],
    total_rows: tuple[tuple[int, int], ...],
    summary: tuple[tuple[int, int], tuple[int, int]],
) -> None:
    """Refuse a holdings total row printed anywhere but where it was read."""

    for page_number, row, line in _numbered_lines(pages):
        label = next(
            (
                label
                for label in (_SECURITIES_TOTAL, _PORTFOLIO_TOTAL)
                if line.strip().startswith(label)
            ),
            None,
        )
        in_summary = summary[0] < (page_number, row) < summary[1]
        if label is not None and not in_summary and (page_number, row) not in total_rows:
            _fail(page=page_number, column=label, message="holdings total is duplicated")


def _activity_schema(header: str) -> tuple[tuple[str, ...], str]:
    if "Trade Date" in header:
        return (
            (
                "Description",
                "Acct Type",
                "Transaction",
                "Trade Date",
                "Settle Date",
                "Qty",
                "Price",
                "Debit",
                "Credit",
            ),
            "Trade Date",
        )
    return (
        (
            "Description",
            "Symbol",
            "Acct Type",
            "Transaction",
            "Date",
            "Qty",
            "Price",
            "Debit",
            "Credit",
        ),
        "Date",
    )


def _require_whole_cusip_line(text: str, *, page: int) -> None:
    """Refuse a CUSIP line that is not ``CUSIP: `` and nine upper-case letters or digits."""
    if _CUSIP_LINE_RE.match(text) and _CUSIP_RE.fullmatch(text) is None:
        _fail(page=page, column="Description", message="activity row has a malformed CUSIP line")


def _activity_pages(  # noqa: PLR0912, PLR0915
    pages: tuple[str, ...],
    *,
    parts: _StatementParts,
) -> None:
    """Read the Account Activity and pending-trades sections.

    A page holds at most one activity section: the one its heading names,
    with its one column header. Its rows run from under that header to the
    page end, where they continue on a later page of the same section, or to
    the section's total, which ends the section. Below the total only blank
    lines, the page marker and the start of another table (``_after_total``)
    may follow; that table's own reader reads it. Another table starting
    above the total refuses, and so does an activity header on a page with
    no activity heading. A dated row prints nothing but blanks left of its
    Description column.

    Every description line, in a dated row's Description cell or on a line of
    its own, is checked as it is read: a CUSIP line, one whose first word is
    ``CUSIP`` in any letter case, is read whole and must be ``CUSIP: `` and
    nine upper-case letters or digits. A malformed one refuses, naming the
    page that prints it.
    """

    pending_by_section: dict[RobinhoodStatementSection, list[str]] = {
        RobinhoodStatementSection.ACCOUNT_ACTIVITY: [],
        RobinhoodStatementSection.PENDING_TRADES: [],
    }
    current_by_section: dict[RobinhoodStatementSection, _ActivityBuilder | None] = {
        RobinhoodStatementSection.ACCOUNT_ACTIVITY: None,
        RobinhoodStatementSection.PENDING_TRADES: None,
    }
    debits_by_section: dict[RobinhoodStatementSection, list[Decimal]] = {
        RobinhoodStatementSection.ACCOUNT_ACTIVITY: [],
        RobinhoodStatementSection.PENDING_TRADES: [],
    }
    credits_by_section: dict[RobinhoodStatementSection, list[Decimal]] = {
        RobinhoodStatementSection.ACCOUNT_ACTIVITY: [],
        RobinhoodStatementSection.PENDING_TRADES: [],
    }
    totals_by_section: dict[RobinhoodStatementSection, tuple[Decimal, Decimal] | None] = {
        RobinhoodStatementSection.ACCOUNT_ACTIVITY: None,
        RobinhoodStatementSection.PENDING_TRADES: None,
    }
    for page_number, page in enumerate(pages, start=1):
        if _page_has_heading(page, RobinhoodStatementSection.ACCOUNT_ACTIVITY.value):
            section = RobinhoodStatementSection.ACCOUNT_ACTIVITY
        elif _page_has_heading(page, RobinhoodStatementSection.PENDING_TRADES.value):
            section = RobinhoodStatementSection.PENDING_TRADES
        else:
            if any(_is_activity_header(line) for line in page.splitlines()):
                _fail(page=page_number, message="activity table header has no heading")
            continue
        lines = page.splitlines()
        candidates = [
            (index, line) for index, line in enumerate(lines) if _is_activity_header(line)
        ]
        if len(candidates) != 1:
            _fail(page=page_number, message="activity table header is missing or ambiguous")
        header_index, header = candidates[0]
        labels, date_label = _activity_schema(header)
        starts = _column_starts(header, labels, page=page_number)
        date_index = labels.index(date_label)
        settle_index = labels.index("Settle Date") if "Settle Date" in labels else None
        symbol_index = labels.index("Symbol") if "Symbol" in labels else None
        total_label = _ACTIVITY_TOTALS[section]
        pending = pending_by_section[section]
        current = current_by_section[section]
        for line_index, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
            sliced = _cells(line, starts)
            date_raw = sliced[date_index]
            description = sliced[labels.index("Description")]
            _require_whole_cusip_line(
                description if _DATE_TOKEN_RE.fullmatch(date_raw) else line.strip(),
                page=page_number,
            )
            if any(label in line for label in _ACTIVITY_TOTALS.values()):
                # Description first and Debit, Credit last, in both schemas.
                description, *others, debit_raw, credit_raw = sliced
                message = "activity total is malformed"
                if line[: starts[0]].strip() or description != total_label or any(others):
                    _fail(page=page_number, column=total_label, message=message)
                debit = _usd(debit_raw, page=page_number, column=total_label, message=message)
                credit = _usd(credit_raw, page=page_number, column=total_label, message=message)
                if totals_by_section[section] is not None:
                    _fail(
                        page=page_number, column=total_label, message="activity total is repeated"
                    )
                if pending and current is not None:
                    current.descriptions.extend(pending)
                    pending.clear()
                if current is not None:
                    parts.activities.append(current.finish())
                    current = None
                if pending:
                    _fail(
                        page=page_number,
                        message="activity description is not attached before the source total",
                    )
                totals_by_section[section] = (debit, credit)
                parts.totals.append(
                    RobinhoodStatementTotal(
                        location=SourceLocation(
                            page=page_number,
                            section=section,
                            row=line_index,
                        ),
                        label=total_label,
                        values=(("Debit", debit), ("Credit", credit)),
                    )
                )
                _after_total(
                    lines, line_index, page=page_number, table="activity", label=total_label
                )
                break
            if _starts_table(lines, line_index - 1, page=page_number):
                _fail(page=page_number, message="another table starts before the activity total")
            if _DATE_TOKEN_RE.fullmatch(date_raw):
                if totals_by_section[section] is not None:
                    _fail(
                        page=page_number,
                        column=total_label,
                        message="activity row follows its section total",
                    )
                if line[: starts[0]].strip():
                    _fail(
                        page=page_number,
                        column="Description",
                        message="activity row holds text before its Description column",
                    )
                leading: list[str] = []
                if current is not None:
                    if pending:
                        if description:
                            current.descriptions.extend(pending)
                        else:
                            leading.extend(pending)
                        pending.clear()
                    parts.activities.append(current.finish())
                else:
                    leading.extend(pending)
                    pending.clear()
                descriptions = leading
                if description:
                    descriptions.append(description)
                debit = _money_or_none(
                    sliced[labels.index("Debit")],
                    page=page_number,
                    column="Debit",
                )
                credit = _money_or_none(
                    sliced[labels.index("Credit")],
                    page=page_number,
                    column="Credit",
                )
                quantity, suffix = _quantity(sliced[labels.index("Qty")], page=page_number)
                current = _ActivityBuilder(
                    location=SourceLocation(
                        page=page_number,
                        section=section,
                        row=line_index,
                    ),
                    descriptions=descriptions,
                    symbol=(sliced[symbol_index] or None) if symbol_index is not None else None,
                    account_type=sliced[labels.index("Acct Type")],
                    transaction_code=sliced[labels.index("Transaction")],
                    transaction_date=_date(date_raw, page=page_number),
                    settle_date=(
                        _date(sliced[settle_index], page=page_number, column="Settle Date")
                        if settle_index is not None and sliced[settle_index]
                        else None
                    ),
                    quantity=quantity,
                    quantity_suffix=suffix,
                    price_usd=_money_or_none(
                        sliced[labels.index("Price")],
                        page=page_number,
                        column="Price",
                        kind=NumberKind.PRICE,
                    ),
                    debit_usd=debit,
                    credit_usd=credit,
                )
                if debit is not None:
                    debits_by_section[section].append(debit)
                if credit is not None:
                    credits_by_section[section].append(credit)
                continue

            content = line.strip()
            if not content or _is_page_marker(content, page=page_number):
                continue
            non_description = any(sliced[index] for index in range(1, len(sliced)))
            if non_description:
                _fail(page=page_number, message="activity row continuation is column-shifted")
            if current is not None and content.startswith(_TRAILING_DESCRIPTION_PREFIXES):
                current.descriptions.append(content)
            else:
                pending.append(content)
        current_by_section[section] = current

    for section, current in current_by_section.items():
        pending = pending_by_section[section]
        if pending and current is not None:
            current.descriptions.extend(pending)
            pending.clear()
        if current is not None:
            parts.activities.append(current.finish())
        if pending:
            _fail(message="activity description is not attached to a row")

    account_total = totals_by_section[RobinhoodStatementSection.ACCOUNT_ACTIVITY]
    if account_total is None:
        _fail(message="Total Funds Paid and Received is missing")
    if (
        _exact_sum(
            debits_by_section[RobinhoodStatementSection.ACCOUNT_ACTIVITY],
            at="Account Activity debit sum",
        )
        != account_total[0]
        or _exact_sum(
            credits_by_section[RobinhoodStatementSection.ACCOUNT_ACTIVITY],
            at="Account Activity credit sum",
        )
        != account_total[1]
    ):
        _fail(message="Account Activity debit/credit sums do not match the source total")
    pending_total = totals_by_section[RobinhoodStatementSection.PENDING_TRADES]
    pending_rows = any(
        row.location.section is RobinhoodStatementSection.PENDING_TRADES for row in parts.activities
    )
    if pending_rows and pending_total is None:
        _fail(message="Total Executed Trades Pending Settlement is missing")
    if pending_total is not None and (
        _exact_sum(
            debits_by_section[RobinhoodStatementSection.PENDING_TRADES],
            at="pending-trade debit sum",
        )
        != pending_total[0]
        or _exact_sum(
            credits_by_section[RobinhoodStatementSection.PENDING_TRADES],
            at="pending-trade credit sum",
        )
        != pending_total[1]
    ):
        _fail(message="pending-trade debit/credit sums do not match the source total")


def _ledger_table(  # noqa: PLR0912, PLR0915
    pages: tuple[str, ...],
    *,
    heading: str,
    section: RobinhoodStatementSection,
    total_label: str,
    opening_label: str,
    closing_label: str,
    parts: _StatementParts,
    collateral: bool,
    footnote: tuple[str, ...],
) -> None:
    """Read one ledger, which may run over several pages.

    Its first row is its one opening row and its last its one closing row.
    Its one total follows the closing row and ends the ledger; the ledger's
    fixed ``footnote``, if it has one, may end the total's page.
    """

    row_type = RobinhoodStockCollateralActivity if collateral else RobinhoodStatementSweepActivity
    rows: list[RobinhoodStatementSweepActivity | RobinhoodStockCollateralActivity] = []
    closing: Decimal | None = None
    total: tuple[Decimal, Decimal] | None = None
    ledger_page: int | None = None
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        if not _page_has_heading(page, heading):
            continue
        ledger_page = page_number
        headers: list[int] = []
        in_table = False
        for index, line in enumerate(lines):
            if _line_has_heading(line, heading):
                in_table = True
            elif _starts_table(lines, index, page=page_number):
                in_table = False
            if in_table and _has_labels(line, _LEDGER_LABELS):
                headers.append(index)
        if len(headers) != 1:
            _fail(page=page_number, message=f"{heading} header is missing or ambiguous")
        header_index = headers[0]
        starts = _column_starts(lines[header_index], _LEDGER_LABELS, page=page_number)
        if total is not None:
            _after_total(
                lines, header_index + 1, page=page_number, table=heading, label=total_label
            )
            continue
        for line_index, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
            sliced = _cells(line, starts)
            description = sliced[0]
            if description.startswith(total_label):
                message = f"{heading} total is malformed"
                if (
                    line[: starts[0]].strip()
                    or description != total_label
                    or sliced[1]
                    or sliced[4]
                ):
                    _fail(page=page_number, column=total_label, message=message)
                total = (
                    _usd(sliced[2], page=page_number, column=total_label, message=message),
                    _usd(sliced[3], page=page_number, column=total_label, message=message),
                )
                if closing is None:
                    _fail(
                        page=page_number,
                        column=total_label,
                        message=f"{heading} total precedes its closing row",
                    )
                parts.totals.append(
                    RobinhoodStatementTotal(
                        location=SourceLocation(page=page_number, section=section, row=line_index),
                        label=total_label,
                        values=(("Debit", total[0]), ("Credit", total[1])),
                    )
                )
                _after_total(
                    lines,
                    line_index,
                    page=page_number,
                    table=heading,
                    label=total_label,
                    footnote=footnote,
                )
                break
            label = next(
                (
                    label
                    for label in (opening_label, closing_label)
                    if description.startswith(label)
                ),
                None,
            )
            if label is None and not _DATE_TOKEN_RE.fullmatch(sliced[1]):
                content = line.strip()
                if not content or _is_page_marker(content, page=page_number):
                    continue
                _fail(
                    page=page_number,
                    message=f"{heading} contains an unsupported in-table line",
                )
            if label is not None and description != label:
                _fail(page=page_number, column=label, message=f"{heading} row label is malformed")
            if closing is not None:
                _fail(
                    page=page_number,
                    column=closing_label,
                    message=f"{heading} row follows its closing row",
                )
            if (label == opening_label) == bool(rows):
                _fail(
                    page=page_number,
                    column=opening_label,
                    message=f"{heading} must open with exactly one opening row",
                )
            if label == opening_label and (sliced[2] or sliced[3]):
                _fail(
                    page=page_number,
                    message=f"{heading} opening row unexpectedly carries a movement",
                )
            rows.append(
                row_type(
                    location=SourceLocation(page=page_number, section=section, row=line_index),
                    description=description,
                    transaction_date=_date(sliced[1], page=page_number),
                    debit_usd=_money_or_none(sliced[2], page=page_number, column="Debit"),
                    credit_usd=_money_or_none(sliced[3], page=page_number, column="Credit"),
                    balance_usd=_money(sliced[4], page=page_number, column="Balance"),
                )
            )
            if label == closing_label:
                closing = rows[-1].balance_usd
    if ledger_page is None:
        return
    if not rows or closing is None or total is None:
        _fail(
            page=ledger_page,
            column=total_label,
            message=f"{heading} opening, closing, or total row is missing",
        )
    running = rows[0].balance_usd
    debit_sum = Decimal()
    credit_sum = Decimal()
    for row in rows:
        debit = row.debit_usd or Decimal()
        credit = row.credit_usd or Decimal()
        debit_sum = _exact_sum((debit_sum, debit), at=f"{heading} debit sum")
        credit_sum = _exact_sum((credit_sum, credit), at=f"{heading} credit sum")
        running = _exact_sum(
            (running, debit.copy_negate(), credit), at=f"{heading} running balance"
        )
        if running != row.balance_usd:
            _fail(message=f"{heading} running balance does not reconcile")
    if running != closing or debit_sum != total[0] or credit_sum != total[1]:
        _fail(message=f"{heading} source totals do not reconcile")
    if collateral:
        parts.stock_collateral.extend(
            row for row in rows if isinstance(row, RobinhoodStockCollateralActivity)
        )
    else:
        parts.sweep_activity.extend(
            row for row in rows if isinstance(row, RobinhoodStatementSweepActivity)
        )


def _has_gap_token(line: str, start: int) -> bool:
    """Whether a token in ``line`` from column ``start`` follows a column gap
    of two or more blanks, as a table row prints its cells; prose runs on with
    single blanks."""

    return _GAP_TOKEN_RE.search(line, start) is not None


def _is_bank_row(line: str, balance_start: int) -> bool:
    """Whether ``line`` is a Program Bank row: its whole ``Balance`` cell is
    one amount after a column gap."""

    cell = line[balance_start:].strip()
    return _MONEY_TOKEN_RE.fullmatch(cell) is not None and _has_gap_token(line, balance_start)


def _sweep_banks(  # noqa: PLR0912
    pages: tuple[str, ...],
    *,
    parts: _StatementParts,
) -> None:
    """Read the Program Banks tables.

    Under the bank header come the bank rows, each a name and, after a column
    gap, one amount with ``$`` in the ``Balance`` column, the name wrapped or
    not onto the line right above. Blank lines and the page marker may come
    between them. After the last row, or right under the header on a page
    with no bank rows, one disclosure paragraph may follow: adjacent lines
    that are not read, none with a token after a column gap in its
    ``Balance`` cell (``_has_gap_token``). Below the paragraph only blank
    lines and the page marker may follow. The table ends at the page end or
    where another table starts (``_starts_table``). Anything else refuses,
    whatever token follows a column gap in the ``Balance`` cell.
    """

    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        if not _page_has_heading(page, RobinhoodStatementSection.SWEEP_BANKS.value):
            continue
        headers = [(index, line) for index, line in enumerate(lines) if _is_bank_header(line)]
        if len(headers) != 1:
            _fail(page=page_number, message="Deposit Sweep Program Banks header is missing")
        header_index, header = headers[0]
        balance_start = header.index("Balance")
        end = next(
            (
                index
                for index in range(header_index + 1, len(lines))
                if _starts_table(lines, index, page=page_number)
            ),
            len(lines),
        )
        body = range(header_index + 1, end)
        # The paragraph may open after a bank row, or anywhere on a page without one.
        prose = not any(_is_bank_row(lines[index], balance_start) for index in body)
        wrap = ""
        paragraph = ""  # "open" while its lines run, "closed" after them
        for index in body:
            line = lines[index]
            content = line.strip()
            if not content or _is_page_marker(content, page=page_number):
                if paragraph:
                    paragraph = "closed"
                continue
            balance_raw = line[balance_start:].strip()
            bank = line[:balance_start].strip()
            gapped = _has_gap_token(line, balance_start)
            if paragraph == "closed":
                _fail(
                    page=page_number,
                    message=(
                        "Deposit Sweep Program Banks disclosure is followed by an unsupported line"
                    ),
                )
            if paragraph and gapped:
                _fail(
                    page=page_number,
                    message="Deposit Sweep Program Banks disclosure holds a bank row",
                )
            if paragraph:
                continue
            if _is_bank_row(line, balance_start):
                bank = "\n".join((wrap, bank)) if wrap else bank
                wrap = ""
                if not bank:
                    _fail(page=page_number, message="Program Bank row has no source name")
                parts.sweep_banks.append(
                    RobinhoodStatementSweepBank(
                        location=SourceLocation(
                            page=page_number,
                            section=RobinhoodStatementSection.SWEEP_BANKS,
                            row=index + 1,
                        ),
                        bank_name=bank,
                        balance_usd=_usd(
                            balance_raw,
                            page=page_number,
                            column="Balance",
                            message="Program Bank balance has no dollar sign",
                        ),
                    )
                )
                prose = True
                continue
            if (
                not balance_raw
                and index + 1 < end
                and _is_bank_row(lines[index + 1], balance_start)
                and len(bank.split()) <= _BANK_NAME_FRAGMENT_MAX_WORDS
                and bank[0].isupper()
                and "%" not in bank
            ):
                wrap = bank
                continue
            if prose and not gapped:
                paragraph = "open"
                continue
            if balance_raw:
                _fail(
                    page=page_number,
                    message="Program Bank row is incomplete or column-shifted",
                )
            _fail(
                page=page_number,
                message="Program Bank name line is not attached to a balance row",
            )


def _loaned_securities(  # noqa: PLR0912, PLR0915
    lines: list[str], *, page: int, parts: _StatementParts
) -> None:
    """Read one page's Stock Lending - Loaned Securities table.

    A page prints one such table, ended by one ``Total Market Value`` row:
    the label and one ``$`` amount, equal to the page's market values. A
    row's description is the lines above it, after the previous row's
    ``CUSIP:`` line, plus its own Description cell. The ``CUSIP:`` line right
    under a row closes that row. Any other line right under a row could
    belong to it or to the next row, so it refuses when another row follows.
    Lines between the last row and the total belong to the last row. Below
    the total, the table's fixed SIPC reminder may end the page.
    """

    headings = [
        index
        for index, line in enumerate(lines)
        if _line_has_heading(line, RobinhoodStatementSection.STOCK_LOANED_SECURITIES.value)
    ]
    headers = [index for index, line in enumerate(lines) if _has_labels(line, _LOANED_LABELS)]
    if len(headings) > 1 or len(headers) > 1:
        _fail(page=page, message="Stock Lending securities table is repeated on the page")
    if not headers:
        _fail(page=page, message="Stock Lending securities header is missing")
    starts = _column_starts(lines[headers[0]], _LOANED_LABELS, page=page)
    first_row = len(parts.stock_loaned)
    pending: list[str] = []
    cusip_due = False
    under_row = False
    for line_index, line in enumerate(lines[headers[0] + 1 :], start=headers[0] + 2):
        content = line.strip()
        if not content or _is_page_marker(content, page=page):
            continue
        if content.startswith(_LOANED_TOTAL):
            if pending:
                if len(parts.stock_loaned) == first_row:
                    _fail(page=page, message="Stock Lending description has no source row")
                previous = parts.stock_loaned[-1]
                parts.stock_loaned[-1] = replace(
                    previous, description="\n".join((previous.description, *pending))
                )
            tail = content[len(_LOANED_TOTAL) :]
            cells = tail.split()
            message = "Stock Lending market-value total is malformed"
            if not tail[:1].isspace() or len(cells) != 1:
                _fail(page=page, column=_LOANED_TOTAL, message=message)
            total = _usd(cells[0], page=page, column=_LOANED_TOTAL, message=message)
            covered = parts.stock_loaned[first_row:]
            covered_sum = _exact_sum(
                (row.market_value_usd for row in covered),
                at="Stock Lending market-value sum",
                page=page,
            )
            if covered_sum != total:
                _fail(page=page, message="Stock Lending market-value total does not reconcile")
            parts.totals.append(
                RobinhoodStatementTotal(
                    location=SourceLocation(
                        page=page,
                        section=RobinhoodStatementSection.STOCK_LOANED_SECURITIES,
                        row=line_index,
                    ),
                    label=_LOANED_TOTAL,
                    values=(("Market Value", total),),
                )
            )
            _after_total(
                lines,
                line_index,
                page=page,
                table="Stock Lending market-value",
                label=_LOANED_TOTAL,
                footnote=_LOANED_FOOTNOTE,
            )
            return
        sliced = _cells(line, starts)
        is_row = (
            _NUMBER_TOKEN_RE.fullmatch(sliced[2]) is not None
            and _MONEY_TOKEN_RE.fullmatch(sliced[3]) is not None
            and _MONEY_TOKEN_RE.fullmatch(sliced[4]) is not None
        )
        if not is_row:
            if _is_content_heading(content):
                break
            if any(sliced[1:]):
                _fail(
                    page=page,
                    message="Stock Lending security row is incomplete or column-shifted",
                )
            if cusip_due and _CUSIP_RE.fullmatch(content):
                previous = parts.stock_loaned[-1]
                parts.stock_loaned[-1] = replace(
                    previous, description=f"{previous.description}\n{content}"
                )
                cusip_due = False
                continue
            if content.startswith("CUSIP:"):
                _fail(page=page, message="Stock Lending description is not attached")
            under_row = under_row or cusip_due
            cusip_due = False
            pending.append(content)
            continue
        if under_row:
            _fail(page=page, message="Stock Lending description is not attached")
        description = "\n".join(part for part in (*pending, sliced[0]) if part)
        pending.clear()
        if not description:
            _fail(page=page, message="Stock Lending security row has no source description")
        shares = _decimal(sliced[2], page=page, column="Shares Lent")
        price = _money(sliced[3], page=page, column="Price", kind=NumberKind.PRICE)
        market = _money(sliced[4], page=page, column="MKT Value")
        check = "Stock Lending market value check"
        add = partial(_exact_sum, at=check, page=page)
        multiply = partial(_exact_product, at=check, page=page)
        tolerance = _market_value_tolerance(
            sliced[2], sliced[3], shares, price, add=add, multiply=multiply
        )
        if add((multiply(shares, price), market.copy_negate())).copy_abs() > tolerance:
            _fail(page=page, message="Stock Lending market value does not reconcile")
        parts.stock_loaned.append(
            RobinhoodStockLoanedSecurity(
                location=SourceLocation(
                    page=page,
                    section=RobinhoodStatementSection.STOCK_LOANED_SECURITIES,
                    row=line_index,
                ),
                description=description,
                symbol=sliced[1],
                shares_lent=shares,
                price_usd=price,
                market_value_usd=market,
            )
        )
        cusip_due = True
    _fail(page=page, column=_LOANED_TOTAL, message="Stock Lending market-value total is missing")


def _stock_lending_tables(  # noqa: PLR0912, PLR0915
    pages: tuple[str, ...],
    *,
    parts: _StatementParts,
) -> None:
    loan_pending: list[str] = []
    loan_cusip_due = False
    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        if _page_has_heading(page, RobinhoodStatementSection.STOCK_LOANED_SECURITIES.value):
            _loaned_securities(lines, page=page_number, parts=parts)

        if _page_has_heading(page, RobinhoodStatementSection.STOCK_LOAN_ACTIVITY.value):
            if loan_cusip_due and parts.stock_loans[-1].location.page < page_number - 1:
                loan_cusip_due = False
            heading_indices = [
                index
                for index, line in enumerate(lines)
                if _line_has_heading(line, RobinhoodStatementSection.STOCK_LOAN_ACTIVITY.value)
            ]
            if len(heading_indices) != 1:
                _fail(page=page_number, message="Stock Lending loan-activity heading is repeated")
            labels = _LOAN_LABELS
            header_index, header = next(
                (
                    (index, line)
                    for index, line in enumerate(
                        lines[heading_indices[0] + 1 :], start=heading_indices[0] + 1
                    )
                    if all(label in line for label in labels)
                ),
                (-1, ""),
            )
            if header_index < 0:
                _fail(page=page_number, message="Stock Lending loan-activity header is missing")
            starts = _column_starts(header, labels, page=page_number)
            # A row's description is the lines above it plus its own Description
            # cell; the CUSIP line printed right under it closes it. Any other line
            # under a row could belong to it or to the next row, so it refuses.
            # The rows end only where another table starts, or at the page end.
            for line_index, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
                sliced = _cells(line, starts)
                if _DATE_TOKEN_RE.fullmatch(sliced[3]):
                    description = "\n".join(part for part in (*loan_pending, sliced[0]) if part)
                    loan_pending.clear()
                    if not description:
                        _fail(
                            page=page_number,
                            message="Stock Lending loan row has no source description",
                        )
                    if sliced[2] != _LOAN_TRANSACTION_CODE:
                        _fail(
                            page=page_number,
                            column=labels[2],
                            message="Stock Lending loan transaction code is unsupported",
                        )
                    lent, returned = sliced[4], sliced[5]
                    if bool(lent) == bool(returned):
                        _fail(
                            page=page_number,
                            message=(
                                "Stock Lending loan row must fill exactly one of "
                                "Shares Lent From Account and Shares Returned To Account"
                            ),
                        )
                    kind, column, raw = (
                        (RobinhoodStockLoanKind.LEND, labels[4], lent)
                        if lent
                        else (RobinhoodStockLoanKind.RETURN, labels[5], returned)
                    )
                    shares = _decimal(raw, page=page_number, column=column)
                    if shares <= 0:
                        _fail(
                            page=page_number,
                            column=column,
                            message="Stock Lending loan share count is not positive",
                        )
                    parts.stock_loans.append(
                        RobinhoodStockLoanActivity(
                            location=SourceLocation(
                                page=page_number,
                                section=RobinhoodStatementSection.STOCK_LOAN_ACTIVITY,
                                row=line_index,
                            ),
                            description=description,
                            symbol=sliced[1],
                            transaction_date=_date(sliced[3], page=page_number),
                            kind=kind,
                            shares=shares,
                        )
                    )
                    loan_cusip_due = True
                    continue

                content = line.strip()
                if not content or _is_page_marker(content, page=page_number):
                    continue
                if _is_content_heading(content):
                    if loan_pending:
                        _fail(
                            page=page_number,
                            message="Stock Lending loan description is not attached",
                        )
                    if not _starts_table(lines, line_index - 1, page=page_number):
                        _fail(
                            page=page_number,
                            message=(
                                "Stock Lending loan-activity table holds a heading line "
                                "that starts no table"
                            ),
                        )
                    loan_cusip_due = False
                    break
                is_cusip = content.startswith("CUSIP:")
                if loan_cusip_due and is_cusip and _CUSIP_RE.fullmatch(content):
                    previous = parts.stock_loans[-1]
                    parts.stock_loans[-1] = replace(
                        previous, description=f"{previous.description}\n{content}"
                    )
                    loan_cusip_due = False
                    continue
                if loan_cusip_due or is_cusip:
                    _fail(
                        page=page_number,
                        message="Stock Lending loan description is not attached",
                    )
                if any(sliced[1:]):
                    _fail(
                        page=page_number,
                        message="Stock Lending loan row is incomplete or column-shifted",
                    )
                loan_pending.append(content)
        else:
            if loan_pending:
                _fail(
                    page=page_number - 1,
                    message="Stock Lending loan description is not attached",
                )
            loan_cusip_due = False
    if loan_pending:
        _fail(message="Stock Lending loan description is not attached")


def parse_robinhood_statement(text: str) -> RobinhoodStatement:
    """Parse one ``pdftotext -layout`` Robinhood brokerage statement."""

    pages = _pages(text)
    disclosure_pages = _validate_page_families(pages)
    (
        family,
        account_type_label,
        account_number,
        period_start,
        period_end,
        registration,
        document_identifier,
    ) = _identity(pages)
    summary_heading = _heading_row(pages, RobinhoodStatementSection.ACCOUNT_SUMMARY.value)
    income_heading = _heading_row(pages, RobinhoodStatementSection.INCOME_EXPENSE.value)
    if income_heading < summary_heading:
        _fail(
            page=income_heading[0],
            message="Income and Expense Summary precedes the Account Summary",
        )
    layout, account_summary = _account_summary(pages, summary_heading, income_heading)
    parts = _StatementParts()
    parts.income.extend(_income(pages, income_heading))
    total_rows = _position_pages(pages, parts=parts)
    _refuse_stray_totals(pages, total_rows, (summary_heading, income_heading))
    portfolio_page, portfolio_row = total_rows[1]
    _after_total(
        pages[portfolio_page - 1].splitlines(),
        portfolio_row,
        page=portfolio_page,
        table="holdings",
        label=_PORTFOLIO_TOTAL,
        footnote=_PORTFOLIO_FOOTNOTE,
    )
    summed_positions = _exact_sum(
        (position.market_value_usd for position in parts.positions),
        at="summed position values",
    )
    if summed_positions != account_summary.closing_total_securities_usd:
        _fail(message="summed position values do not equal closing Total Securities")
    _activity_pages(pages, parts=parts)
    _sweep_banks(pages, parts=parts)
    _ledger_table(
        pages,
        heading=RobinhoodStatementSection.SWEEP_ACTIVITY.value,
        section=RobinhoodStatementSection.SWEEP_ACTIVITY,
        total_label="Total Swept Funds",
        opening_label="Opening Sweep Balance",
        closing_label="Closing Sweep Balance",
        parts=parts,
        collateral=False,
        footnote=(),
    )
    _stock_lending_tables(pages, parts=parts)
    _ledger_table(
        pages,
        heading=RobinhoodStatementSection.STOCK_COLLATERAL_ACTIVITY.value,
        section=RobinhoodStatementSection.STOCK_COLLATERAL_ACTIVITY,
        total_label="Total Collateral Movements",
        opening_label="Opening Collateral Balance",
        closing_label="Closing Collateral Balance",
        parts=parts,
        collateral=True,
        footnote=_COLLATERAL_FOOTNOTE,
    )
    if parts.sweep_banks:
        bank_sum = _exact_sum(
            (bank.balance_usd for bank in parts.sweep_banks), at="Program Bank balance sum"
        )
        if bank_sum != account_summary.closing_deposit_sweep_usd:
            _fail(message="Program Bank balances do not equal closing Deposit Sweep balance")
    elif account_summary.closing_deposit_sweep_usd != 0:
        _fail(message="non-zero Deposit Sweep balance has no Program Bank rows")

    for page_number, page in enumerate(pages, start=1):
        lines = page.splitlines()
        if any(_page_has_heading(page, heading) for heading in _TABLE_HEADERS) or any(
            _is_position_header(line) for line in lines
        ):
            for line in lines:
                _is_page_marker(line.strip(), page=page_number)
    _check_table_headings(pages)
    _check_fixed_pages(pages)

    return RobinhoodStatement(
        family=family,
        layout=layout,
        period_start=period_start,
        period_end=period_end,
        account_number=account_number,
        account_type_label=account_type_label,
        registration_lines=registration,
        base_currency=_CURRENCY,
        page_count=len(pages),
        document_identifier=document_identifier,
        account_summary=account_summary,
        income=tuple(parts.income),
        positions=tuple(parts.positions),
        activities=tuple(parts.activities),
        sweep_banks=tuple(parts.sweep_banks),
        sweep_activity=tuple(parts.sweep_activity),
        stock_loaned_securities=tuple(parts.stock_loaned),
        stock_loan_activity=tuple(parts.stock_loans),
        stock_collateral_activity=tuple(parts.stock_collateral),
        totals=tuple(parts.totals),
        disclosure_pages=disclosure_pages,
    )


def read_robinhood_statement_kind(path: Path) -> RobinhoodStatementKind:
    """Say whether one statement PDF is a Robinhood Crypto statement, without parsing it.

    Only a positively identified Crypto statement is ``CRYPTO``; a PDF that
    mixes Crypto and brokerage statement families refuses.
    """

    text = pdftotext_layout(
        path,
        doc_kind="Robinhood statement PDFs",
        source="Robinhood bundle discovery",
    )
    return RobinhoodStatementKind.CRYPTO if _is_crypto(text) else RobinhoodStatementKind.BROKERAGE


def read_robinhood_statement(path: Path) -> RobinhoodStatement:
    """Extract and parse one statement PDF without exposing its path."""

    text = pdftotext_layout(
        path,
        doc_kind="Robinhood brokerage statement PDFs",
        source=_SECTION,
    )
    return parse_robinhood_statement(text)
