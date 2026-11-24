"""Shared builders for synthetic Robinhood test inputs (#1672).

Typed statement evidence (:func:`robinhood_statement`,
:func:`robinhood_account_summary`, :func:`robinhood_activity`), the activity
report's CSV text (:func:`activity_csv`), the parser tests' sample report
(:data:`ACTIVITY_ROWS`, :func:`activity_rows_csv`) with its row and column
indexes, one of its rows edited (:func:`one_row_activity_csv`), dividend and
ADR fee description texts (:func:`distribution_description`,
:func:`adr_fee_description`), one fixed-width statement line
(:func:`fixed_line`), a printed table moved to wider columns
(:func:`widened_table`), a Stock Lending page (:func:`loaned_securities_page`),
a configured account (:func:`robinhood_account_config`)
and an activity report's cash deposit (:func:`robinhood_cash_event`). Each test
module's own factory keeps its defaults and builds through these, so a new
model field is added here once.
"""

from __future__ import annotations

import csv
import io
import itertools
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Final, Literal

from ibkr_fa.config import RobinhoodAccountConfig, TaxpayerStatus
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
    RobinhoodStatementSweepActivity,
    RobinhoodStatementSweepBank,
    RobinhoodStockLoanedSecurity,
    SourceLocation,
)

ACTIVITY_HEADER: Final = (
    "Activity Date",
    "Process Date",
    "Settle Date",
    "Instrument",
    "Description",
    "Trans Code",
    "Quantity",
    "Price",
    "Amount",
)
ACTIVITY_FOOTER: Final = (
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    (
        "The data provided is for informational purposes only. Please consult a "
        "professional tax service or personal tax advisor if you need instructions "
        "on how to calculate cost basis or questions regarding your specific tax "
        "situation. Reminder: This data does not include Robinhood Crypto or "
        "Robinhood Spending activity."
    ),
)


def activity_csv(
    rows: Iterable[Sequence[str]],
    *,
    header: Sequence[str] = ACTIVITY_HEADER,
    separator: Sequence[str] | None = ("",),
    footer: Sequence[str] | None = ACTIVITY_FOOTER,
    trailing: Iterable[Sequence[str]] = (),
) -> str:
    """An activity report: ``header``, ``rows``, the blank ``separator`` row,
    the disclaimer ``footer`` and any ``trailing`` rows, every cell quoted.
    ``None`` leaves the separator or footer out."""
    out = io.StringIO(newline="")
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    if separator is not None:
        writer.writerow(separator)
    if footer is not None:
        writer.writerow(footer)
    writer.writerows(trailing)
    return out.getvalue()


# The parser tests' sample activity rows, which together cover every event
# kind. Other test modules index them by position, so keep the order (#1793).
ACTIVITY_ROWS: tuple[tuple[str, ...], ...] = (
    ("1/2/2025", "1/2/2025", "1/3/2025", "", "ACH CANCEL", "ACH", "", "", "($10.00)"),
    ("1/2/2025", "1/2/2025", "1/3/2025", "", "ACH Deposit", "ACH", "", "", "$10.00"),
    ("1/2/2025", "1/2/2025", "1/3/2025", "", "ACH Withdrawal", "ACH", "", "", "($3.00)"),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "ADR",
        "ADR Fee: R/D 2025-01-01 P/D 2025-01-02 - 2 shares at 0.01",
        "AFEE",
        "",
        "",
        "($0.02)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "DIV",
        "Cash Div: R/D 2025-01-01 P/D 2025-01-02 - 2 shares at 1.25",
        "CDIV",
        "",
        "",
        "$2.50",
    ),
    ("1/2/2025", "1/2/2025", "1/3/2025", "", "Interest Payment", "INT", "", "", "$0.10"),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "DRP",
        "Dividend Reinvestment Example\nCUSIP: 99971T103\nDividend Reinvestment",
        "Buy",
        # 1.8350 * 2.00 = 3.67
        "1.8350",
        "$2.00",
        "($3.67)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "BUY",
        "Ordinary Purchase Example, Inc.\nCUSIP: 99961C102",
        "Buy",
        "1",
        "$10.00",
        "($10.00)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "SEL",
        "Sale Example Corp.\nCUSIP: 99961D100",
        "Sell",
        "1.500000",
        "$12.00",
        "$17.99",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "",
        "External debit card transfer - account ending in 0001",
        "DCF",
        "",
        "",
        "$5.00",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "TAX",
        "Foreign Tax Witholding at $0.50",
        "DTAX",
        "",
        "",
        "($0.50)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "XFR",
        "Transfer Example-Co\nCUSIP: 99961E108",
        "ITRF",
        "2S",
        "",
        "",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "",
        "Instant bank transfer - account ending in 0002",
        "RTP",
        "",
        "",
        "$6.00",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "",
        "Transfer from Brokerage to Brokerage",
        "ITRF",
        "",
        "",
        "$7.00",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "",
        "Transfer from Brokerage to Brokerage",
        "ITRF",
        "",
        "",
        "($8.00)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "DIV",
        (
            "Cash Div: R/D 2025-01-01 P/D 2025-01-02 - 2 shares at 1.25 "
            "new dividend IN nra tax withhold"
        ),
        "NRAT",
        "",
        "",
        "($0.63)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "OPT",
        "OPT 1/17/2025 Call $10.00",
        "BTO",
        "1",
        "$2.00",
        "($200.25)",
    ),
    (
        "1/3/2025",
        "1/3/2025",
        "1/6/2025",
        "OPT",
        "OPT 1/17/2025 Call $10.00",
        "STC",
        "1",
        "$3.00",
        "$299.75",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "IPO",
        "Primary Issue Example\nCUSIP: 99961F105\nPrimary Issue",
        "Buy",
        "2",
        "$5.00",
        "($10.00)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "REC",
        "Receipt Example Ltd.\nCUSIP: 99961G103",
        "REC",
        "3.0000",
        "",
        "",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "LND",
        "Stock Lending",
        "SLIP",
        "",
        "",
        "$0.05",
    ),
    # Keep extension rows at the tail: existing refusal tests use source indexes.
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "MBY",
        "Managed Buy Example\nCUSIP: 99961H101\nManaged Trade",
        "Buy",
        # 1.8350 * 10.00 = 18.35
        "1.8350",
        "$10.00",
        "($18.35)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "MSL",
        "Managed Sale Example\nCUSIP: 99961J107\nManaged Trade",
        "Sell",
        # 1.8350 * 12.00 = 22.02
        "1.8350",
        "$12.00",
        "$22.02",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "",
        "Management Fee",
        "ADVFEE",
        "",
        "",
        "($0.25)",
    ),
    (
        "1/2/2025",
        "1/2/2025",
        "1/3/2025",
        "XFR",
        "Managed Transfer Example\nCUSIP: 99961K104",
        "ITRF",
        "2",
        "",
        "",
    ),
)


def activity_rows_csv(
    rows: tuple[tuple[str, ...], ...] = ACTIVITY_ROWS,
    *,
    header: tuple[str, ...] = ACTIVITY_HEADER,
    separator: tuple[str, ...] | None = ("",),
    footer: tuple[str, ...] | None = ACTIVITY_FOOTER,
    trailing: tuple[tuple[str, ...], ...] = (),
) -> str:
    """:func:`activity_csv` with :data:`ACTIVITY_ROWS` as the default ``rows``."""
    return activity_csv(rows, header=header, separator=separator, footer=footer, trailing=trailing)


# Rows of ACTIVITY_ROWS, and the activity CSV's columns (#1823). Row and column
# numbers overlap (DIVIDEND_ROW and DESCRIPTION_COLUMN are both 4), so each
# name says which it is.
ADR_FEE_ROW, DIVIDEND_ROW, BUY_ROW, SELL_ROW = 3, 4, 7, 8
FOREIGN_TAX_ROW, TRANSFER_ROW, NRA_ROW = 10, 11, 15
OPTION_OPEN_ROW, OPTION_CLOSE_ROW = 16, 17
DESCRIPTION_COLUMN, QUANTITY_COLUMN, PRICE_COLUMN, AMOUNT_COLUMN = 4, 6, 7, 8


def one_row_activity_csv(index: int, cells: dict[int, str]) -> str:
    """An activity CSV of row ``index`` alone, with ``cells`` replaced."""
    row = list(ACTIVITY_ROWS[index])
    for column, value in cells.items():
        row[column] = value
    return activity_rows_csv((tuple(row),))


def distribution_description(shares: str, rate: str, *, nra: bool = False) -> str:
    """A cash dividend's description text; ``nra`` adds the NRA withholding suffix."""
    suffix = " new dividend IN nra tax withhold" if nra else ""
    return f"Cash Div: R/D 2025-01-01 P/D 2025-01-02 - {shares} shares at {rate}{suffix}"


def adr_fee_description(shares: str, rate: str) -> str:
    """An ADR fee's description text."""
    return f"ADR Fee: R/D 2025-01-01 P/D 2025-01-02 - {shares} shares at {rate}"


def fixed_line(
    starts: Sequence[int],
    cells: Sequence[str],
    *,
    width: int | None = None,
    fit: bool = True,
) -> str:
    """``cells`` placed at the column ``starts`` of a ``width``-character line
    (by default 60 past the last start), trailing blanks stripped. With
    ``fit``, each cell but the last must be narrower than its column."""
    if fit:
        widths = [end - start for start, end in itertools.pairwise(starts)]
        assert all(len(cell) < column for cell, column in zip(cells, widths, strict=False)), cells
    chars = [" "] * (starts[-1] + 60 if width is None else width)
    for start, cell in zip(starts, cells, strict=True):
        chars[start : start + len(cell)] = cell
    return "".join(chars).rstrip()


def widened_table(
    page: str,
    starts: tuple[int, ...],
    wide: tuple[int, ...],
    edits: Mapping[str, Mapping[int, str]],
    *,
    key: Callable[[list[str]], str],
    line: Callable[[tuple[int, ...], tuple[str, ...]], str],
) -> str:
    """``page`` with its table columns moved from the printed ``starts`` to
    ``wide``: each line is cut into cells at ``starts``, the cells of the row
    whose ``key`` is in ``edits`` are replaced, and ``line`` (the caller's
    width and fit check) renders the cells at ``wide``."""
    ends = (*starts[1:], None)
    lines = []
    for printed in page.splitlines():
        cells = [printed[start:end].strip() for start, end in zip(starts, ends, strict=True)]
        for column, value in edits.get(key(cells), {}).items():
            cells[column] = value
        lines.append(line(wide, tuple(cells)))
    return "\n".join(lines)


def loaned_securities_page(
    starts: tuple[int, ...],
    rows: Iterable[tuple[str, ...] | str],
    total: str,
    *,
    line: Callable[[tuple[int, ...], tuple[str, ...]], str],
) -> str:
    """The Stock Lending - Loaned Securities page: its header row and each
    cell tuple in ``rows`` rendered by ``line`` (the caller's width and fit
    check) at ``starts``, each ``str`` row printed as is, then ``Total Market
    Value {total}``."""
    header = ("Description", "Symbol", "Shares Lent", "Price", "MKT Value")
    return "\n".join(
        (
            "Stock Lending - Loaned Securities",
            line(starts, header),
            *(row if isinstance(row, str) else line(starts, row) for row in rows),
            f"Total Market Value {total}",
        )
    )


def robinhood_account_config(
    number: str,
    *,
    opening_date: date = date(2025, 1, 5),
    address: str = "Synthetic address",
    zip_code: str = "",
    source: str = "synthetic statement",
) -> RobinhoodAccountConfig:
    """One configured beneficially owned US Robinhood account."""
    return RobinhoodAccountConfig(
        number=number,
        opening_date=opening_date,
        status=TaxpayerStatus.BENEFICIAL_OWNER,
        institution_name="Robinhood Securities, LLC",
        address=address,
        zip_code=zip_code,
        country_iso2="US",
        same_beneficial_owner=True,
        source=source,
    )


def robinhood_cash_event(day: date, description: str) -> RobinhoodEvent:
    """A USD 1.00 ACH deposit on ``day``, at activity report record 1."""
    return RobinhoodEvent(
        kind=RobinhoodEventKind.ACH_DEPOSIT,
        source_record_index=1,
        activity_date=day,
        process_date=day,
        settle_date=day,
        instrument=None,
        description=description,
        trans_code="ACH",
        quantity=None,
        quantity_suffix=None,
        price_usd=None,
        amount_usd=Decimal("1.00"),
    )


def robinhood_account_summary(
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
    cash_label: str = "Brokerage Cash Balance",
    closing_percents: tuple[Decimal, Decimal, Decimal] = (Decimal(), Decimal(), Decimal()),
) -> RobinhoodAccountSummary:
    """An Account Summary. ``opening`` and ``closing`` are the brokerage
    cash, deposit sweep, total securities and portfolio value;
    ``closing_percents`` the cash, equities and options shares."""
    return RobinhoodAccountSummary(
        source_cash_label=cash_label,
        opening_brokerage_cash_usd=opening[0],
        closing_brokerage_cash_usd=closing[0],
        opening_deposit_sweep_usd=opening[1],
        closing_deposit_sweep_usd=closing[1],
        opening_total_securities_usd=opening[2],
        closing_total_securities_usd=closing[2],
        opening_portfolio_value_usd=opening[3],
        closing_portfolio_value_usd=closing[3],
        closing_cash_percent=closing_percents[0],
        closing_equities_percent=closing_percents[1],
        closing_options_percent=closing_percents[2],
    )


def robinhood_activity(
    *,
    row: int,
    transaction_code: str,
    transaction_date: date,
    section: RobinhoodStatementSection = RobinhoodStatementSection.ACCOUNT_ACTIVITY,
    description: str = "Synthetic activity",
    symbol: str | None = None,
    settle_date: date | None = None,
    quantity: Decimal | None = None,
    quantity_suffix: str | None = None,
    price_usd: Decimal | None = None,
    debit_usd: Decimal | None = None,
    credit_usd: Decimal | None = None,
    cusips: tuple[str, ...] = (),
    source_account_reference: str | None = None,
    destination_account_reference: str | None = None,
    option_symbol: str | None = None,
    option_expiration: date | None = None,
    option_right: str | None = None,
    option_strike_usd: Decimal | None = None,
) -> RobinhoodStatementActivity:
    """One margin-account activity row, at ``row`` of ``section`` on page 2."""
    return RobinhoodStatementActivity(
        location=SourceLocation(page=2, section=section, row=row),
        description=description,
        symbol=symbol,
        account_type="Margin",
        transaction_code=transaction_code,
        transaction_date=transaction_date,
        settle_date=settle_date,
        quantity=quantity,
        quantity_suffix=quantity_suffix,
        price_usd=price_usd,
        debit_usd=debit_usd,
        credit_usd=credit_usd,
        cusips=cusips,
        source_account_reference=source_account_reference,
        destination_account_reference=destination_account_reference,
        option_symbol=option_symbol,
        option_expiration=option_expiration,
        option_right=option_right,
        option_strike_usd=option_strike_usd,
    )


_DEFAULT_SUMMARY: Final = robinhood_account_summary()


class _Default(Enum):
    """A keyword whose default follows from another keyword."""

    FAMILY_LABEL = "family label"


def robinhood_statement(
    *,
    family: RobinhoodStatementFamily = RobinhoodStatementFamily.INDIVIDUAL,
    layout: RobinhoodStatementLayout = RobinhoodStatementLayout.DEPOSIT_SWEEP,
    period_start: date = date(2025, 1, 1),
    period_end: date = date(2025, 1, 31),
    account_number: str = "111111111",
    account_type_label: str | None | Literal[_Default.FAMILY_LABEL] = _Default.FAMILY_LABEL,
    registration_lines: tuple[str, ...] = ("TAX PAYER",),
    base_currency: str = "USD",
    page_count: int = 2,
    account_summary: RobinhoodAccountSummary = _DEFAULT_SUMMARY,
    income: tuple[RobinhoodStatementIncome, ...] = (),
    positions: tuple[RobinhoodStatementPosition, ...] = (),
    activities: tuple[RobinhoodStatementActivity, ...] = (),
    sweep_banks: tuple[RobinhoodStatementSweepBank, ...] = (),
    sweep_activity: tuple[RobinhoodStatementSweepActivity, ...] = (),
    stock_loaned_securities: tuple[RobinhoodStockLoanedSecurity, ...] = (),
) -> RobinhoodStatement:
    """One January 2025 statement with no document identifier, stock loan or
    collateral activity, totals or disclosure pages; override any other field
    by keyword. The account-type label defaults to the family's value, or none
    for an unspecified family."""
    if account_type_label is _Default.FAMILY_LABEL:
        account_type_label = (
            None if family is RobinhoodStatementFamily.UNSPECIFIED else family.value
        )
    return RobinhoodStatement(
        family=family,
        layout=layout,
        period_start=period_start,
        period_end=period_end,
        account_number=account_number,
        account_type_label=account_type_label,
        registration_lines=registration_lines,
        base_currency=base_currency,
        page_count=page_count,
        document_identifier=None,
        account_summary=account_summary,
        income=income,
        positions=positions,
        activities=activities,
        sweep_banks=sweep_banks,
        sweep_activity=sweep_activity,
        stock_loaned_securities=stock_loaned_securities,
        stock_loan_activity=(),
        stock_collateral_activity=(),
        totals=(),
        disclosure_pages=(),
    )
