"""Synthetic Robinhood statements and activity CSVs for the exact-normalizer
tests (#1308).

Everything goes through the real readers: :func:`statement` prints a
brokerage statement's text for :func:`parse_robinhood_statement`, and
:func:`activity_csv` an activity report for :func:`parse_robinhood_activity`.
The helpers below then run the normalizer the way ``load_robinhood_bundle``
does. Every printed total is computed here with every digit kept, so a long
cell reaches the normalizer exactly as printed. Values are synthetic: the
symbols are neutral markers and the CUSIPs carry no real issuer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from typing import Final

from ibkr_fa.config import RobinhoodAccountConfig, RobinhoodSecurityReceiptResolution
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodActivityReportEvidence,
    RobinhoodCashReconciliation,
    RobinhoodSecurityQuantityResult,
    reconcile_robinhood_accounts,
    reconcile_robinhood_activity,
    reconcile_robinhood_cash,
    reconcile_robinhood_security_quantities,
    resolve_robinhood_security_identities,
    resolve_robinhood_security_quantities,
    validate_robinhood_inventory_partition,
    validate_robinhood_statement_bundles,
)
from ibkr_fa.parser.robinhood import parse_robinhood_activity
from ibkr_fa.parser.robinhood_statement import RobinhoodStatement, parse_robinhood_statement
from tests._robinhood_security import robinhood_security
from tests._robinhood_statement_pages import (
    ACTIVITY_LABELS,
    HOLDINGS_LABELS,
    LEDGER_LABELS,
    PENDING_LABELS,
    PENDING_TOTAL,
    STATEMENT_ACTIVITY_TOTAL,
)
from tests._robinhood_statements import (
    activity_csv,
    loaned_securities_page,
    robinhood_account_config,
)
from tests._robinhood_statements import fixed_line as _fixed

INDIVIDUAL: Final = "111111111"
MANAGED: Final = "222222222"
SYMBOL: Final = "XQAA"
CUSIP: Final = "99971T103"
SECURITY: Final = robinhood_security(symbol=SYMBOL, cusip=CUSIP)
START: Final = date(2025, 1, 1)
END: Final = date(2025, 1, 31)
NA: Final = "N/A"


def exactly(*terms: str | int | Decimal) -> Decimal:
    """The sum of ``terms``, at a precision that keeps every digit."""
    with localcontext(Context(prec=200)):
        return sum((Decimal(term) for term in terms), Decimal())


def usd(value: str | Decimal) -> str:
    """``value`` printed as a statement prints money, with at least two
    places: ``$1,234.50``."""
    amount = Decimal(value)
    exponent = amount.as_tuple().exponent
    assert isinstance(exponent, int)
    if exponent > -2:
        amount = amount.quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    return f"{sign}${amount.copy_abs():,f}"


# Every numeric column is 45 characters wide, so in-limit long cells fit.
_HOLDINGS: Final = (0, 40, 58, 70, 115, 160, 205, 250, 262)
_ACTIVITY: Final = (0, 100, 112, 125, 139, 151, 196, 241, 286)
_LEDGER: Final = (0, 47, 63, 108, 153)
_LOANED: Final = (0, 55, 72, 117, 162)
_PENDING: Final = (0, 42, 55, 69, 82, 95, 140, 185, 230)


@dataclass(frozen=True)
class Position:
    """One holdings row: ``quantity`` at ``price``, worth ``value``."""

    quantity: str
    value: str
    price: str = "$1.00"
    symbol: str = SYMBOL
    description: str = "Synthetic Equity"
    loaned: bool = False


@dataclass(frozen=True)
class Loan:
    """One Stock Lending - Loaned Securities row."""

    shares: str
    value: str
    price: str = "$1.00"
    symbol: str = SYMBOL


@dataclass(frozen=True)
class Row:
    """One Account Activity row; ``lines`` print below it."""

    code: str
    day: str
    quantity: str = ""
    price: str = ""
    debit: str = ""
    credit: str = ""
    symbol: str = ""
    description: str = ""
    lines: tuple[str, ...] = ()
    settles: str = ""


@dataclass(frozen=True)
class Sweep:
    """The Deposit Sweep partition: ``opening`` and the ledger's movements,
    each a ``(debit, credit)`` pair of printed amounts."""

    opening: str
    movements: tuple[tuple[str, str], ...]


def _sweep_closing(sweep: Sweep) -> Decimal:
    return exactly(
        sweep.opening,
        *(credit or 0 for _debit, credit in sweep.movements),
        *(f"-{debit}" for debit, _credit in sweep.movements if debit),
    )


def _page1(
    *,
    account: str,
    managed: bool,
    opening: tuple[str, str, str] | None,
    closing_cash: Decimal,
    securities: Decimal,
    sweep: Sweep | None,
) -> str:
    label = "Managed Individual Account" if managed else "Individual Account"
    cash, opening_securities, opening_value = (
        (NA, NA, NA) if opening is None else tuple(usd(value) for value in opening)
    )
    if sweep is None:
        summary = f"Net Account Balance {cash} {usd(closing_cash)}\n"
    else:
        sweep_opening = NA if opening is None else usd(sweep.opening)
        summary = (
            f"Brokerage Cash Balance {cash} {usd(closing_cash)}\n"
            "Cash and Cash Equivalents 100.00%\n"
            f"Deposit Sweep Balance {sweep_opening} {usd(_sweep_closing(sweep))}\n"
        )
        cash = ""
    closing_value = exactly(closing_cash, securities, *((_sweep_closing(sweep),) if sweep else ()))
    return (
        "TAX PAYER                         1/1/2025 to 1/31/2025\n"
        "1 TEST ROAD\n"
        f"{label} #: {account}\n"
        "Robinhood Financial LLC introduces the account.\n"
        "Robinhood Securities, LLC carries the account.\n"
        "Account Summary Opening Balance Closing Balance\n"
        f"{summary}"
        + ("Cash and Cash Equivalents 100.00%\n" if sweep is None else "")
        + f"Total Securities * {opening_securities} {usd(securities)}\n"
        "Equities 0.00%\n"
        f"Portfolio Value {opening_value} {usd(closing_value)}\n"
        "Options 0.00%\n"
        "Income and Expense Summary This Period Year to Date\n"
        "Dividends $0.00 $0.00\n"
        "Capital Gains Distributions $0.00 $0.00\n"
        "Interest Earned $0.00 $0.00\n"
        "Stock Lending $0.00 $0.00\n"
        "Public disclosure text.                                      1234567\n"
    )


def _holdings_header(section: str) -> str:
    return _fixed(_HOLDINGS, (section, *HOLDINGS_LABELS))


def _holdings_page(
    positions: Sequence[Position], cash: Decimal, sweep: Sweep | None
) -> tuple[str, Decimal]:
    def rows(loaned: bool) -> list[str]:
        return [
            _fixed(
                _HOLDINGS,
                (
                    position.description,
                    position.symbol,
                    "Margin",
                    position.quantity,
                    position.price,
                    position.value,
                    "$0.00",
                    "",
                    "0.00%",
                ),
            )
            for position in positions
            if position.loaned is loaned
        ]

    securities = exactly(*(position.value.lstrip("$").replace(",", "") for position in positions))
    loaned = rows(loaned=True)
    sweep_lines = (f"Deposit Sweep Balance {usd(_sweep_closing(sweep))} 0.00%",) if sweep else ()
    priced = exactly(cash, securities, *((_sweep_closing(sweep),) if sweep else ()))
    lines = (
        "Portfolio Summary",
        _holdings_header("Securities Held in Account"),
        *rows(loaned=False),
        *((_holdings_header("Loaned Securities"), *loaned) if loaned else ()),
        f"Total Securities {usd(securities)} 100.00%",
        f"Brokerage Cash Balance {usd(cash)} 0.00%",
        *sweep_lines,
        f"Total Priced Portfolio {usd(priced)}",
    )
    return "\n".join(lines), securities


def _activity_page(rows: Sequence[Row]) -> str:
    header = _fixed(_ACTIVITY, ACTIVITY_LABELS)
    lines: list[str] = []
    for row in rows:
        lines.append(
            _fixed(
                _ACTIVITY,
                (
                    row.description,
                    row.symbol,
                    "Margin",
                    row.code,
                    row.day,
                    row.quantity,
                    row.price,
                    row.debit,
                    row.credit,
                ),
            )
        )
        lines.extend(row.lines)
    debit = exactly(*(_money(row.debit) for row in rows))
    credit = exactly(*(_money(row.credit) for row in rows))
    total = _fixed(
        _ACTIVITY,
        (STATEMENT_ACTIVITY_TOTAL, "", "", "", "", "", "", usd(debit), usd(credit)),
    )
    return "\n".join(("Account Activity", header, *lines, total))


def _pending_page(rows: Sequence[Row]) -> str:
    """The Executed Trades Pending Settlement table: each row's ``day`` is its
    trade date and ``settles`` its settle date."""
    header = _fixed(_PENDING, PENDING_LABELS)
    lines: list[str] = []
    for row in rows:
        lines.append(
            _fixed(
                _PENDING,
                (
                    row.description,
                    "Margin",
                    row.code,
                    row.day,
                    row.settles,
                    row.quantity,
                    row.price,
                    row.debit,
                    row.credit,
                ),
            )
        )
        lines.extend(row.lines)
    debit = exactly(*(_money(row.debit) for row in rows))
    credit = exactly(*(_money(row.credit) for row in rows))
    total = _fixed(
        _PENDING,
        (PENDING_TOTAL, "", "", "", "", "", "", usd(debit), usd(credit)),
    )
    return "\n".join(("Executed Trades Pending Settlement", header, *lines, total))


def _money(cell: str) -> Decimal:
    return Decimal(cell.lstrip("$").replace(",", "")) if cell else Decimal()


def _sweep_pages(sweep: Sweep) -> tuple[str, str]:
    closing = _sweep_closing(sweep)
    banks = "\n".join(
        (
            "Deposit Sweep Program Banks",
            "Bank                                                        Balance",
            f"Example Program Bank                                         {usd(closing)}",
        )
    )
    header = _fixed(_LEDGER, LEDGER_LABELS)
    balance = Decimal(sweep.opening)
    rows = [_fixed(_LEDGER, ("Opening Sweep Balance", "1/1/2025", "", "", usd(balance)))]
    for debit, credit in sweep.movements:
        balance = exactly(balance, credit or 0, f"-{debit}" if debit else 0)
        rows.append(
            _fixed(
                _LEDGER,
                (
                    "Movement",
                    "1/10/2025",
                    usd(debit) if debit else "",
                    usd(credit) if credit else "",
                    usd(balance),
                ),
            )
        )
    rows.append(_fixed(_LEDGER, ("Closing Sweep Balance", "1/31/2025", "", "", usd(closing))))
    debit_total = exactly(*(debit or 0 for debit, _credit in sweep.movements))
    credit_total = exactly(*(credit or 0 for _debit, credit in sweep.movements))
    rows.append(_fixed(_LEDGER, ("Total Swept Funds", "", usd(debit_total), usd(credit_total), "")))
    return banks, "\n".join(("Deposit Sweep Activity", header, *rows))


def _loaned_page(loans: Sequence[Loan]) -> str:
    total = exactly(*(loan.value.lstrip("$").replace(",", "") for loan in loans))
    return loaned_securities_page(
        _LOANED,
        [("Synthetic Equity", loan.symbol, loan.shares, loan.price, loan.value) for loan in loans],
        usd(total),
        line=_fixed,
    )


def statement(
    *,
    account: str = INDIVIDUAL,
    managed: bool = False,
    opening: tuple[str, str, str] | None = None,
    closing_cash: str | Decimal = "0",
    positions: Sequence[Position] = (),
    loans: Sequence[Loan] = (),
    rows: Sequence[Row] = (),
    pending: Sequence[Row] = (),
    sweep: Sweep | None = None,
) -> str:
    """One January 2025 statement's text.

    ``opening`` is the Account Summary's opening ``(cash, securities,
    portfolio value)``, or N/A. Each total is the exact sum of what it covers.
    """
    cash = Decimal(closing_cash)
    holdings, securities = _holdings_page(positions, cash, sweep)
    contents = [
        _page1(
            account=account,
            managed=managed,
            opening=opening,
            closing_cash=cash,
            securities=securities,
            sweep=sweep,
        ),
        holdings,
        _activity_page(rows),
        *((_pending_page(pending),) if pending else ()),
        *(_sweep_pages(sweep) if sweep else ()),
        *((_loaned_page(loans),) if loans else ()),
        "Important Information\nVariable public disclosure prose.",
    ]
    count = len(contents)
    return "\f".join(
        f"{content}\nPage {index} of {count}\n" for index, content in enumerate(contents, 1)
    )


def account(number: str) -> RobinhoodAccountConfig:
    return robinhood_account_config(number, address="1 Test Road", zip_code="00000", source="test")


def parse(text: str) -> RobinhoodStatement:
    return parse_robinhood_statement(text)


def one_account(text: str, rows: Sequence[Sequence[str]]) -> RobinhoodCashReconciliation:
    """One account's statement and CSV ``rows``, through the activity and
    cash reconciliations that ``reconcile_robinhood_accounts`` runs."""
    events = parse_robinhood_activity(activity_csv(rows))
    (bundle,) = validate_robinhood_statement_bundles((parse(text),))
    return reconcile_robinhood_cash(reconcile_robinhood_activity(bundle, (), events), events)


def pair(
    first: tuple[str, Sequence[Sequence[str]]],
    second: tuple[str, Sequence[Sequence[str]]],
) -> RobinhoodAccountsReconciliation:
    """The individual and managed accounts' ``(statement, CSV rows)``,
    read and paired as ``load_robinhood_bundle`` does."""
    statements = tuple(parse(text) for text, _rows in (first, second))
    reports = tuple(
        RobinhoodActivityReportEvidence(parse_robinhood_activity(activity_csv(rows)), START, END)
        for _text, rows in (first, second)
    )
    bundles = validate_robinhood_statement_bundles(statements)
    return reconcile_robinhood_accounts(bundles, reports, (account(INDIVIDUAL), account(MANAGED)))


def quantities(
    paired: RobinhoodAccountsReconciliation,
    resolutions: tuple[RobinhoodSecurityReceiptResolution, ...] | None = None,
    *,
    transfer_quantities: dict[int, Decimal] | None = None,
) -> RobinhoodSecurityQuantityResult:
    """The account quantities, as ``load_robinhood_bundle`` reconciles them,
    or through ``resolutions`` as the legs that resolve REC rows do, or with
    the exact ``transfer_quantities`` those legs pass on."""
    identities = resolve_robinhood_security_identities(paired, (SECURITY,))
    if resolutions is None:
        result = reconcile_robinhood_security_quantities(
            paired, identities, transfer_quantities=transfer_quantities
        )
    else:
        result = resolve_robinhood_security_quantities(paired, identities, resolutions)
    validate_robinhood_inventory_partition(paired, result)
    return result


# The managed account's statement and report: one $10.00 deposit.
MANAGED_ACCOUNT: Final = (
    statement(
        account=MANAGED,
        managed=True,
        closing_cash="10.00",
        rows=(Row("ACH", "1/10/2025", credit="$10.00", description="ACH Deposit"),),
    ),
    (("1/10/2025", "1/10/2025", "1/10/2025", "", "ACH Deposit", "ACH", "", "", "$10.00"),),
)
