"""Synthetic Robinhood brokerage-statement text, in the statement's
``pdftotext -layout`` layout, shared by the statement parser tests and the
Robinhood normalize-coverage, number-limit, exact-check, refusal-chain, pipeline
and bundle tests (#1813).

:func:`brokerage_statement` builds a whole one-month statement: page 1, the
holdings, Account Activity and pending-trade pages, Program Banks and the Deposit
Sweep ledger (deposit-sweep layout only), the Stock Lending pages and the
disclosures. :func:`crypto_statement` builds a Crypto statement, and
:func:`option_activity_statement` adds an option trade row to Account Activity or
the pending table.
"""

from __future__ import annotations

from ibkr_fa.parser.robinhood_statement import RobinhoodStatementFamily, RobinhoodStatementLayout
from tests._robinhood_statements import fixed_line, loaned_securities_page


def statement_line(starts: tuple[int, ...], values: tuple[str, ...], *, width: int = 220) -> str:
    return fixed_line(starts, values, width=width, fit=False)


def position_header(section: str, *, shift: int = 0) -> tuple[str, tuple[int, ...]]:
    """The holdings header, with every column ``shift`` characters further right."""

    starts = tuple(start + shift for start in (34, 52, 64, 75, 88, 103, 119, 130))
    header = statement_line((0, *starts), (section, *HOLDINGS_LABELS))
    return header, starts


def portfolio_total_lines(layout: RobinhoodStatementLayout) -> tuple[str, ...]:
    """The cash rows and portfolio total printed under ``Total Securities``."""

    if layout is RobinhoodStatementLayout.DEPOSIT_SWEEP:
        cash = ("Brokerage Cash Balance $5.00 1.54%", "Deposit Sweep Balance $20.00 6.15%")
    else:
        cash = ("Brokerage Cash Balance $25.00 7.69%",)
    return (*cash, "Total Priced Portfolio $325.00")


def position_page(
    *,
    loaned: bool = False,
    layout: RobinhoodStatementLayout = RobinhoodStatementLayout.DEPOSIT_SWEEP,
) -> str:
    section = "Loaned Securities" if loaned else "Securities Held in Account"
    header, starts = position_header(section)
    if loaned:
        rows = [
            statement_line(
                (0, *starts),
                (
                    "Loaned Example",
                    "LOAN",
                    "Margin",
                    "1",
                    "$100.0000",
                    "$100.00",
                    "$1.00",
                    "2.50%",
                    "33.33%",
                ),
            )
        ]
    else:
        rows = [
            statement_line(
                (0, *starts),
                (
                    "Example Equity",
                    "EQTY",
                    "Margin",
                    "1",
                    "$100.0000",
                    "$100.00",
                    "$1.00",
                    "2.50%",
                    "33.33%",
                ),
            ),
            "OPT 1/17/2025 Call $10.00",
            statement_line(
                (0, *starts),
                (
                    "",
                    "OPT",
                    "Margin",
                    "1",
                    "$2.0000",
                    "$200.00",
                    "$0.00",
                    "",
                    "66.67%",
                ),
            ),
        ]
    return "\n".join(
        (
            "Portfolio Summary",
            header,
            *rows,
            "Total Securities $300.00 100.00%",
            *portfolio_total_lines(layout),
        )
    )


def statement_activity_header(
    *, pending: bool = False
) -> tuple[str, tuple[str, ...], tuple[int, ...]]:
    if pending:
        labels = PENDING_LABELS
        starts = (0, 42, 55, 69, 82, 95, 103, 116, 129)
    else:
        labels = ACTIVITY_LABELS
        starts = (0, 100, 112, 125, 139, 151, 161, 174, 187)
    return statement_line(starts, labels), labels, starts


def statement_activity_page(*, pending: bool = False) -> str:
    header, _, starts = statement_activity_header(pending=pending)
    heading = "Executed Trades Pending Settlement" if pending else "Account Activity"
    if pending:
        row = statement_line(
            starts,
            (
                "Pending Buy",
                "Margin",
                "Buy",
                "1/30/2025",
                "2/3/2025",
                "1",
                "$10.0000",
                "$10.00",
                "",
            ),
        )
        total = statement_line(
            starts,
            (
                PENDING_TOTAL,
                "",
                "",
                "",
                "",
                "",
                "",
                "$10.00",
                "$0.00",
            ),
        )
    else:
        first = statement_line(
            starts,
            (
                "",
                "CASH",
                "Margin",
                "ITRF",
                "1/10/2025",
                "",
                "",
                "",
                "$10.00",
            ),
        )
        second = statement_line(
            starts,
            (
                "Purchase",
                "EQTY",
                "Margin",
                "Buy",
                "1/11/2025",
                "1",
                "$5.0000",
                "$5.00",
                "",
            ),
        )
        total = statement_line(
            starts,
            (
                STATEMENT_ACTIVITY_TOTAL,
                "",
                "",
                "",
                "",
                "",
                "",
                "$5.00",
                "$10.00",
            ),
        )
        return "\n".join(
            (
                heading,
                header,
                second,
                "CUSIP: 99971T103",
                "Internal Security Transfer from 111111111 to 222222222",
                first,
                "Unlisted Continuation",
                total,
            )
        )
    return "\n".join((heading, header, row, total))


def ledger_page(
    *,
    heading: str,
    opening: str,
    closing: str,
    total: str,
    closing_movement: bool = False,
) -> str:
    starts = (0, 47, 63, 78, 93)
    header = statement_line(starts, LEDGER_LABELS)
    rows = [
        statement_line(starts, (opening, "1/1/2025", "", "", "$10.00")),
        statement_line(starts, ("Movement", "1/10/2025", "", "$10.00", "$20.00")),
    ]
    if closing_movement:
        rows.append(statement_line(starts, (closing, "1/31/2025", "$20.00", "", "$0.00")))
        debit, credit = "$20.00", "$10.00"
    else:
        rows.append(statement_line(starts, (closing, "1/31/2025", "", "", "$20.00")))
        debit, credit = "$0.00", "$10.00"
    rows.append(statement_line(starts, (total, "", debit, credit, "")))
    return "\n".join((heading, header, *rows))


def sweep_bank_page() -> str:
    return "\n".join(
        (
            "Deposit Sweep Program Banks",
            "Bank                                                        Balance",
            "Example Program Bank                                         $20.00",
        )
    )


def stock_loaned_page() -> str:
    starts = (0, 55, 72, 91, 106)
    return loaned_securities_page(
        starts, [("Loaned Example", "LOAN", "1", "$5.0000", "$5.00")], "$5.00", line=statement_line
    )


def stock_loan_activity_page() -> str:
    starts = (0, 42, 55, 72, 86, 116)
    return "\n".join(
        (
            "Stock Lending - Loan Activity",
            statement_line(
                starts,
                (
                    "Description",
                    "Symbol",
                    "Transaction",
                    "Date",
                    "Shares Lent From Account",
                    "Shares Returned To Account",
                ),
            ),
            statement_line(starts, ("Loan", "LOAN", "SLJNL", "1/10/2025", "1", "")),
        )
    )


def _page1(
    *,
    family: RobinhoodStatementFamily,
    layout: RobinhoodStatementLayout,
    opening_na: bool,
) -> str:
    account_label = (
        "Managed Individual Account"
        if family is RobinhoodStatementFamily.MANAGED_INDIVIDUAL
        else "Individual Account"
    )
    opening = "N/A" if opening_na else "$5.00"
    securities_opening = "N/A" if opening_na else "$250.00"
    portfolio_opening = (
        "N/A"
        if opening_na
        else ("$260.00" if layout is RobinhoodStatementLayout.DEPOSIT_SWEEP else "$275.00")
    )
    if layout is RobinhoodStatementLayout.DEPOSIT_SWEEP:
        summary = (
            f"Brokerage Cash Balance {opening} $5.00\n"
            "Cash and Cash Equivalents 8.33%\n"
            f"Deposit Sweep Balance {opening} $20.00\n"
            f"Total Securities * {securities_opening} $300.00\n"
            "Equities 25.00%\n"
            f"Portfolio Value {portfolio_opening} $325.00\n"
            "Options 66.67%\n"
        )
    else:
        opening_cash = "N/A" if opening_na else "$25.00"
        summary = (
            f"Net Account Balance {opening_cash} $25.00\n"
            "Cash and Cash Equivalents 7.69%\n"
            f"Total Securities * {securities_opening} $300.00\n"
            "Equities 30.77%\n"
            f"Portfolio Value {portfolio_opening} $325.00\n"
            "Options 61.54%\n"
        )
    return (
        "TAX PAYER                         1/1/2025 to 1/31/2025\n"
        "1 TEST ROAD\n"
        f"{account_label} #: 111111111\n"
        "Robinhood Financial LLC introduces the account.\n"
        "Robinhood Securities, LLC carries the account.\n"
        "Account Summary Opening Balance Closing Balance\n"
        f"{summary}"
        "Income and Expense Summary This Period Year to Date\n"
        "Dividends $1.00 $1.00\n"
        "Capital Gains Distributions $0.00 $0.00\n"
        "Interest Earned $0.10 $0.10\n"
        "Stock Lending $0.05 $0.05\n"
        "Public disclosure text.                                      1234567\n"
    )


# The markerless overflow page: the approved ``Total Securities`` footnote,
# wrapped at other words than on the holdings page, then page-1 footnote prose.
OVERFLOW_LINES = (
    "* “Total Securities” includes those securities held in your account as well as those "
    "on loan through Stock Lending (identified herein as “Loaned Securities” or “Loaned",
    "Stocks”). The provisions of the Securities Investor Protection Act of 1970 may not "
    "protect you with respect to Loaned Securities.",
    "Variable page-1 footnote prose.",
)


def brokerage_statement(
    *,
    family: RobinhoodStatementFamily = RobinhoodStatementFamily.INDIVIDUAL,
    layout: RobinhoodStatementLayout = RobinhoodStatementLayout.DEPOSIT_SWEEP,
    opening_na: bool = False,
    markerless_overflow: bool = False,
    position_pages: tuple[str, ...] | None = None,
    extra_pages: tuple[str, ...] = (),
) -> str:
    contents = [
        _page1(family=family, layout=layout, opening_na=opening_na),
        *((position_page(layout=layout),) if position_pages is None else position_pages),
        statement_activity_page(),
        statement_activity_page(pending=True),
    ]
    if layout is RobinhoodStatementLayout.DEPOSIT_SWEEP:
        contents.extend(
            (
                sweep_bank_page(),
                ledger_page(
                    heading="Deposit Sweep Activity",
                    opening="Opening Sweep Balance",
                    closing="Closing Sweep Balance",
                    total="Total Swept Funds",
                ),
            )
        )
    contents.extend(
        (
            stock_loaned_page(),
            stock_loan_activity_page(),
            ledger_page(
                heading="Stock Lending - Collateral Activity",
                opening="Opening Collateral Balance",
                closing="Closing Collateral Balance",
                total="Total Collateral Movements",
                closing_movement=True,
            ),
            *extra_pages,
            "Important Information\nVariable public disclosure prose.",
        )
    )
    if markerless_overflow:
        contents.insert(1, "\n".join(OVERFLOW_LINES))
    total = len(contents)
    pages = []
    for index, content in enumerate(contents, start=1):
        if markerless_overflow and index == 2:
            pages.append(content)
        else:
            pages.append(f"{content}\nPage {index} of {total}\n")
    return "\f".join(pages)


# The holdings (after the section name), Account Activity, pending and ledger
# tables' column labels, and the activity and pending tables' total labels.
HOLDINGS_LABELS = (
    "Sym/Cusip",
    "Acct Type",
    "Qty",
    "Price",
    "Mkt Value",
    "Est. Dividend",
    "Yield",
    "% of Total Portfolio",
)
ACTIVITY_LABELS = (
    "Description",
    "Symbol",
    "Acct Type",
    "Transaction",
    "Date",
    "Qty",
    "Price",
    "Debit",
    "Credit",
)
PENDING_LABELS = (
    "Description",
    "Acct Type",
    "Transaction",
    "Trade Date",
    "Settle Date",
    "Qty",
    "Price",
    "Debit",
    "Credit",
)
LEDGER_LABELS = ("Description", "Date", "Debit", "Credit", "Balance")
STATEMENT_ACTIVITY_TOTAL = "Total Funds Paid and Received"
PENDING_TOTAL = "Total Executed Trades Pending Settlement"

# The ledger pages' column starts and header, and each ledger's heading and
# opening, closing and total labels.
LEDGER_STARTS = (0, 47, 63, 78, 93)
LEDGER_HEADER = statement_line(LEDGER_STARTS, LEDGER_LABELS)
LEDGERS = {
    "sweep": (
        "Deposit Sweep Activity",
        "Opening Sweep Balance",
        "Closing Sweep Balance",
        "Total Swept Funds",
    ),
    "collateral": (
        "Stock Lending - Collateral Activity",
        "Opening Collateral Balance",
        "Closing Collateral Balance",
        "Total Collateral Movements",
    ),
}


def statement_ledger(kind: str) -> str:
    heading, opening, closing, total = LEDGERS[kind]
    return ledger_page(
        heading=heading,
        opening=opening,
        closing=closing,
        total=total,
        closing_movement=kind == "collateral",
    )


def statement_activity_total(
    changes: dict[str, str] | None = None, *, pending: bool = False
) -> str:
    """The section's total row, with ``changes`` to its cells."""
    _header, labels, starts = statement_activity_header(pending=pending)
    cells = {
        **dict.fromkeys(labels, ""),
        "Description": PENDING_TOTAL if pending else STATEMENT_ACTIVITY_TOTAL,
        "Debit": "$10.00" if pending else "$5.00",
        "Credit": "$0.00" if pending else "$10.00",
        **(changes or {}),
    }
    return statement_line(starts, tuple(cells[label] for label in labels))


def crypto_statement() -> str:
    """Synthetic Crypto-statement text.

    Each page opens with the family's title block, a ``Crypto Statement`` line
    and an ``MM-YYYY`` line, and the text names Robinhood Crypto. The account
    and page lines are generic shapes brokerage statements also carry, so they
    must not count as brokerage evidence. Everything else is made up.
    """

    bodies = (
        "Account #: 333333333\nCrypto Holdings          Quantity          Market Value\n",
        "Bitcoin (BTC)            0.01000000        $1,000.00\n",
    )
    return "\f".join(
        f"Crypto Statement\n01-2025\n{body}Page {number} of {len(bodies)}\nRobinhood Crypto, LLC\n"
        for number, body in enumerate(bodies, start=1)
    )


# An option contract as an activity row's description prints it.
ACTIVITY_OPTION_CONTRACT = "XQZZ 11/22/2033 Call $987.65"


def option_activity_statement(
    description: str,
    *below: str,
    code: str = "BTO",
    symbol: str = "XQZZ",
    pending: bool = False,
) -> str:
    """The fixture plus a ``code`` row in Account Activity or the pending table,
    described by ``description`` and the lines ``below`` it, just above the
    section's total. The pending table has no Symbol cell, so ``symbol`` goes
    unprinted there."""

    _header, labels, starts = statement_activity_header(pending=pending)
    cells = {
        "Description": description,
        "Symbol": symbol,
        "Acct Type": "Margin",
        "Transaction": code,
        "Date": "1/12/2025",
        "Trade Date": "1/31/2025",
        "Settle Date": "2/3/2025",
        "Qty": "1",
        "Price": "$1.0000",
        "Debit": "",
        "Credit": "",
    }
    row = statement_line(starts, tuple(cells[label] for label in labels))
    page = statement_activity_page(pending=pending)
    total = statement_activity_total(pending=pending)
    return brokerage_statement().replace(page, page.replace(total, "\n".join((row, *below, total))))


# A Program Banks row, and one whose Balance cell holds a token that is not an amount.
PROGRAM_BANK_ROW = "Example Program Bank                                         $20.00"
NOT_MONEY_BANK_ROW = statement_line((0, 61), ("Second Program Bank", "NOT-MONEY"))
