"""Synthetic contract checks for the Robinhood brokerage-statement parser."""

from __future__ import annotations

import re
import subprocess
from dataclasses import fields
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest

import ibkr_fa.parser.robinhood_statement.parser as statement_parser
from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    RobinhoodStatementFamily,
    RobinhoodStatementKind,
    RobinhoodStatementLayout,
    RobinhoodStatementSection,
    RobinhoodStockLoanKind,
    parse_robinhood_statement,
    read_robinhood_statement,
    read_robinhood_statement_kind,
)
from tests._private_checks import private_test
from tests._repr_text import repr_without_addresses
from tests._robinhood_private import (
    private_pdf_page_count,
    private_robinhood_pdfs,
    read_private_statement,
)
from tests._robinhood_refusals import CRYPTO_REFUSAL, MIXED_FAMILIES_REFUSAL
from tests._robinhood_refusals import MALFORMED_ACTIVITY_CONTRACT_REFUSAL as _ACTIVITY_MALFORMED
from tests._robinhood_refusals import MALFORMED_TOTAL_REFUSAL as _MALFORMED_TOTAL
from tests._robinhood_statement_pages import ACTIVITY_OPTION_CONTRACT as _ACTIVITY_CONTRACT
from tests._robinhood_statement_pages import LEDGER_HEADER as _LEDGER_HEADER
from tests._robinhood_statement_pages import LEDGER_LABELS
from tests._robinhood_statement_pages import LEDGER_STARTS as _LEDGER_STARTS
from tests._robinhood_statement_pages import LEDGERS as _LEDGERS
from tests._robinhood_statement_pages import NOT_MONEY_BANK_ROW as _NOT_MONEY
from tests._robinhood_statement_pages import OVERFLOW_LINES as _OVERFLOW_LINES
from tests._robinhood_statement_pages import PENDING_TOTAL as _PENDING_TOTAL
from tests._robinhood_statement_pages import PROGRAM_BANK_ROW as _BANK_ROW
from tests._robinhood_statement_pages import STATEMENT_ACTIVITY_TOTAL as _ACTIVITY_TOTAL
from tests._robinhood_statement_pages import brokerage_statement as _statement
from tests._robinhood_statement_pages import crypto_statement as _crypto_statement
from tests._robinhood_statement_pages import ledger_page as _ledger_page
from tests._robinhood_statement_pages import option_activity_statement as _option_activity
from tests._robinhood_statement_pages import portfolio_total_lines as _portfolio_total_lines
from tests._robinhood_statement_pages import position_header as _position_header
from tests._robinhood_statement_pages import position_page as _position_page
from tests._robinhood_statement_pages import statement_activity_header as _activity_header
from tests._robinhood_statement_pages import statement_activity_page as _activity_page
from tests._robinhood_statement_pages import statement_activity_total as _activity_total
from tests._robinhood_statement_pages import statement_ledger as _ledger
from tests._robinhood_statement_pages import statement_line as _fixed
from tests._robinhood_statement_pages import stock_loan_activity_page as _stock_loan_activity_page
from tests._robinhood_statement_pages import stock_loaned_page as _stock_loaned_page
from tests._robinhood_statement_pages import sweep_bank_page as _sweep_bank_page
from tests._robinhood_statements import loaned_securities_page


def test_parse_complete_individual_sweep_statement() -> None:
    statement = parse_robinhood_statement(_statement())

    assert statement.family is RobinhoodStatementFamily.INDIVIDUAL
    assert statement.layout is RobinhoodStatementLayout.DEPOSIT_SWEEP
    assert statement.account_type_label == "Individual"
    assert statement.account_summary.closing_portfolio_value_usd == 325
    assert statement.account_summary.closing_deposit_sweep_usd == 20
    assert len(statement.positions) == 2
    assert statement.positions[0].yield_percent == 2.5
    assert statement.positions[1].description == "OPT 1/17/2025 Call $10.00"
    assert statement.positions[1].option_right == "Call"
    assert statement.positions[1].option_strike_usd == 10
    assert len(statement.activities) == 3
    assert statement.activities[0].description == "Purchase\nCUSIP: 99971T103"
    assert statement.activities[0].cusips == ("99971T103",)
    assert statement.activities[1].source_account_reference == "111111111"
    assert statement.activities[1].destination_account_reference == "222222222"
    assert statement.activities[1].description == (
        "Internal Security Transfer from 111111111 to 222222222\nUnlisted Continuation"
    )
    assert statement.activities[2].description == "Pending Buy"
    assert len(statement.sweep_banks) == 1
    assert len(statement.sweep_activity) == 3
    assert len(statement.stock_loaned_securities) == 1
    assert len(statement.stock_loan_activity) == 1
    assert len(statement.stock_collateral_activity) == 3
    assert {total.label for total in statement.totals}.issuperset(
        {"Total Securities", "Total Market Value", "Total Swept Funds"}
    )
    securities_total = next(
        total for total in statement.totals if total.label == "Total Securities"
    )
    assert securities_total.values == (("Amount1", Decimal("300.00")),)
    assert statement.document_identifier == "1234567"


def test_pending_trades_require_their_printed_total() -> None:
    pending_page = _activity_page(pending=True)
    total_line = next(line for line in pending_page.splitlines() if _PENDING_TOTAL in line)
    missing_total = _statement().replace(f"\n{total_line}", "", 1)

    with pytest.raises(
        ParseError,
        match="Total Executed Trades Pending Settlement is missing",
    ):
        parse_robinhood_statement(missing_total)


def test_unrecognized_pending_total_fails_as_column_shift() -> None:
    changed = _statement().replace(
        _PENDING_TOTAL,
        "Changed Executed Trades Pending Settlement",
        1,
    )

    with pytest.raises(ParseError, match="column-shifted"):
        parse_robinhood_statement(changed)


def test_pending_total_must_reconcile_and_is_optional_without_rows() -> None:
    _header, _labels, starts = _activity_header(pending=True)
    correct_total = next(
        line for line in _activity_page(pending=True).splitlines() if _PENDING_TOTAL in line
    )
    wrong_total_line = _fixed(
        starts,
        (
            _PENDING_TOTAL,
            "",
            "",
            "",
            "",
            "",
            "",
            "$9.00",
            "$0.00",
        ),
    )
    wrong_total = _statement().replace(
        correct_total,
        wrong_total_line,
        1,
    )
    with pytest.raises(ParseError, match="pending-trade debit/credit sums"):
        parse_robinhood_statement(wrong_total)

    empty_pending = "\n".join(
        (
            "Executed Trades Pending Settlement",
            _activity_header(pending=True)[0],
        )
    )
    statement = parse_robinhood_statement(
        _statement().replace(_activity_page(pending=True), empty_pending, 1)
    )
    assert not any(
        row.location.section is RobinhoodStatementSection.PENDING_TRADES
        for row in statement.activities
    )


def test_managed_legacy_and_na_opening_variants() -> None:
    statement = parse_robinhood_statement(
        _statement(
            family=RobinhoodStatementFamily.MANAGED_INDIVIDUAL,
            layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
            opening_na=True,
        )
    )

    assert statement.family is RobinhoodStatementFamily.MANAGED_INDIVIDUAL
    assert statement.layout is RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
    assert statement.account_summary.opening_portfolio_value_usd is None
    assert statement.account_summary.closing_deposit_sweep_usd == 0
    assert statement.sweep_banks == ()


def test_markerless_page1_overflow_is_accepted() -> None:
    statement = parse_robinhood_statement(_statement(markerless_overflow=True))
    assert statement.page_count == 11
    assert statement.disclosure_pages == (11,)
    assert _rows(statement) == _BASE_ROWS


def test_decorated_continuation_headings_use_the_same_family_gate() -> None:
    text = (
        _statement()
        .replace("Account Activity\n", "Account Activity (continued)\n")
        .replace(
            "Stock Lending - Loan Activity\n",
            "Stock Lending - Loan Activity (continued)\n",
        )
    )
    statement = parse_robinhood_statement(text)
    assert statement.activities
    assert statement.stock_loan_activity


def test_bare_old_account_title_remains_unproven() -> None:
    statement = parse_robinhood_statement(
        _statement().replace("Individual Account #: 111111111", "Account #: 111111111")
    )
    assert statement.family is RobinhoodStatementFamily.UNSPECIFIED
    assert statement.account_type_label is None


def test_allocation_percentages_follow_labels_not_row_order() -> None:
    text = _statement().replace(
        "Equities 25.00%\nPortfolio Value $260.00 $325.00\nOptions 66.67%",
        "Options 66.67%\nPortfolio Value $260.00 $325.00\nEquities 25.00%",
    )
    statement = parse_robinhood_statement(text)
    assert statement.account_summary.closing_equities_percent == 25
    assert statement.account_summary.closing_options_percent == Decimal("66.67")


def test_unknown_income_and_partial_position_rows_refuse() -> None:
    extra_income = _statement().replace(
        "Stock Lending $0.05 $0.05",
        "Stock Lending $0.05 $0.05\nUnsupported Expense $1.00 $1.00",
    )
    with pytest.raises(ParseError, match="unsupported row"):
        parse_robinhood_statement(extra_income)

    header, starts = _position_header("Securities Held in Account")
    partial = _fixed(
        (0, *starts),
        ("Incomplete", "BAD", "Margin", "1", "", "", "", "", ""),
    )
    with pytest.raises(ParseError, match="incomplete or column-shifted"):
        parse_robinhood_statement(_statement().replace(header, f"{header}\n{partial}"))

    trailing = parse_robinhood_statement(
        _statement().replace(
            "Total Securities $300.00",
            "Trailing Position Description\nTotal Securities $300.00",
        )
    )
    assert trailing.positions[-1].description.endswith("Trailing Position Description")


def test_generic_position_continuation_stays_with_preceding_row() -> None:
    statement = parse_robinhood_statement(
        _statement().replace(
            "OPT 1/17/2025 Call $10.00",
            "Generic Position Note\nOPT 1/17/2025 Call $10.00",
        )
    )
    assert statement.positions[0].description.endswith("Generic Position Note")
    assert "Generic Position Note" not in statement.positions[1].description
    assert statement.positions[0].option_right is None
    assert (statement.positions[1].option_right, statement.positions[1].option_strike_usd) == (
        "Call",
        Decimal("10.00"),
    )


def test_option_arithmetic_refuses_before_portfolio_total() -> None:
    with pytest.raises(ParseError, match="quantity and price"):
        parse_robinhood_statement(_statement().replace("$2.0000", "$1.5000", 1))


def test_stock_loan_activity_consumes_or_refuses_every_line() -> None:
    starts = (0, 42, 55, 72, 86, 116)
    loan_row = _fixed(
        starts,
        ("Loan", "LOAN", "SLJNL", "1/10/2025", "1", ""),
    )
    wrapped_page = _stock_loan_activity_page().replace(
        loan_row,
        f"Wrapped Loan Description\n{loan_row}",
    )
    statement = parse_robinhood_statement(
        _statement().replace(_stock_loan_activity_page(), wrapped_page)
    )
    assert statement.stock_loan_activity[0].description == ("Wrapped Loan Description\nLoan")

    shifted_page = _stock_loan_activity_page().replace("1/10/2025", "1/10/2025*")
    with pytest.raises(ParseError, match="incomplete or column-shifted"):
        parse_robinhood_statement(_statement().replace(_stock_loan_activity_page(), shifted_page))

    empty_description_row = _fixed(
        starts,
        ("", "LOAN", "SLJNL", "1/10/2025", "1", ""),
    )
    leading_page = _stock_loan_activity_page().replace(
        loan_row,
        f"Loan\n{empty_description_row}",
    )
    leading = parse_robinhood_statement(
        _statement().replace(_stock_loan_activity_page(), leading_page)
    )
    assert leading.stock_loan_activity[0].description == "Loan"

    trailing_page = _stock_loan_activity_page().replace(
        loan_row,
        f"{loan_row}\nUnexpected Trailing Text",
    )
    with pytest.raises(ParseError, match="description is not attached"):
        parse_robinhood_statement(_statement().replace(_stock_loan_activity_page(), trailing_page))


_LOAN_STARTS = (0, 42, 55, 72, 86, 116)


def _loan_row(
    description: str = "",
    *,
    transaction: str = "SLJNL",
    lent: str = "",
    returned: str = "",
) -> str:
    return _fixed(_LOAN_STARTS, (description, "LOAN", transaction, "1/10/2025", lent, returned))


def _with_pages(page: str, *contents: str, text: str | None = None) -> str:
    """Replace the page printing ``page`` with one page per content, renumbered."""
    pages = [part.rsplit("\nPage ", 1)[0] for part in (text or _statement()).split("\f")]
    index = pages.index(page)
    pages[index : index + 1] = contents
    return "\f".join(
        f"{page}\nPage {number} of {len(pages)}\n" for number, page in enumerate(pages, start=1)
    )


def _with_loan_pages(*bodies: tuple[str, ...]) -> str:
    """Replace the fixture's Loan Activity page with one page per body, renumbered."""
    header = _stock_loan_activity_page().splitlines()[:2]
    return _with_pages(
        _stock_loan_activity_page(), *("\n".join((*header, *body)) for body in bodies)
    )


def test_stock_loan_rows_carry_their_kind_and_attach_name_above_and_cusip_below() -> None:
    base = parse_robinhood_statement(_statement()).stock_loan_activity
    assert [(row.kind, row.shares, row.description) for row in base] == [
        (RobinhoodStockLoanKind.LEND, Decimal("1"), "Loan")
    ]

    statement = parse_robinhood_statement(
        _with_loan_pages(
            (
                "Example Lent Shares",
                _loan_row(lent="2"),
                "CUSIP: 99971T103",
                "Example Returned Shares",
                "Second Name Line",
                _loan_row(returned="1"),
            ),
            ("CUSIP: 99972H108",),
        )
    )

    assert [(row.kind, row.shares, row.description) for row in statement.stock_loan_activity] == [
        (RobinhoodStockLoanKind.LEND, Decimal("2"), "Example Lent Shares\nCUSIP: 99971T103"),
        (
            RobinhoodStockLoanKind.RETURN,
            Decimal("1"),
            "Example Returned Shares\nSecond Name Line\nCUSIP: 99972H108",
        ),
    ]


_TRANSACTION_REFUSED = "column 'Transaction': Stock Lending loan transaction code is unsupported"
_ONE_SHARE_COLUMN = (
    "Stock Lending loan row must fill exactly one of "
    "Shares Lent From Account and Shares Returned To Account"
)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        pytest.param(
            _loan_row("Loan", transaction="XXXXX", lent="1"), _TRANSACTION_REFUSED, id="unknown"
        ),
        pytest.param(
            _loan_row("Loan", transaction="", lent="1"), _TRANSACTION_REFUSED, id="blank-code"
        ),
        pytest.param(
            _loan_row("Loan", transaction="LEND", lent="1"), _TRANSACTION_REFUSED, id="lend-code"
        ),
        pytest.param(
            _loan_row("Loan", lent="-1"),
            "column 'Shares Lent From Account': Stock Lending loan share count is not positive",
            id="negative",
        ),
        pytest.param(
            _loan_row("Loan", returned="0"),
            "column 'Shares Returned To Account': Stock Lending loan share count is not positive",
            id="zero",
        ),
        pytest.param(
            _loan_row("Loan", lent="-"),
            "column 'Shares Lent From Account': number has an unsupported format",
            id="dash",
        ),
        pytest.param(
            _loan_row("Loan", returned="N/A"),
            "column 'Shares Returned To Account': number has an unsupported format",
            id="n-a",
        ),
        pytest.param(_loan_row("Loan", lent="1", returned="1"), _ONE_SHARE_COLUMN, id="both"),
        pytest.param(_loan_row("Loan"), _ONE_SHARE_COLUMN, id="neither"),
    ],
)
def test_stock_loan_row_refuses_unknown_transaction_or_impossible_shares(
    row: str, message: str
) -> None:
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(_with_loan_pages((row,)))

    assert message in str(raised.value)


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(("CUSIP: 99971T103", _loan_row("Loan", lent="1")), id="cusip-above-row"),
        pytest.param(
            (_loan_row("Loan", lent="1"), "CUSIP: 99971T103", "CUSIP: 99972H108"),
            id="second-cusip",
        ),
        pytest.param(
            (_loan_row("Loan", lent="1"), "Extra Text", "CUSIP: 99971T103"),
            id="text-between-row-and-cusip",
        ),
        pytest.param(
            (_loan_row("Loan", lent="1"), "Next Name", _loan_row(returned="1")),
            id="no-cusip-before-next-name",
        ),
        pytest.param((_loan_row("Loan", lent="1"), "CUSIP: 12345"), id="malformed-cusip"),
        pytest.param((_loan_row("Loan", lent="1"), f"CUSIP: {'1' * 50}"), id="oversized-cusip"),
        pytest.param(
            (_loan_row("Loan", lent="1"), "CUSIP: 99971T103", "Dangling Name"),
            id="name-without-row",
        ),
    ],
)
def test_stock_loan_line_that_belongs_to_no_single_row_refuses(lines: tuple[str, ...]) -> None:
    with pytest.raises(ParseError, match="Stock Lending loan description is not attached"):
        parse_robinhood_statement(_with_loan_pages(lines))


@pytest.mark.parametrize(
    ("before", "after", "gap"),
    [
        pytest.param(
            (_loan_row("Loan", lent="1"),),
            ("CUSIP: 99971T103",),
            "nonloan-page",
            id="cusip-after-nonloan-page",
        ),
        pytest.param(
            (_loan_row("Loan", lent="1"),),
            ("CUSIP: 99971T103",),
            "empty-loan-page",
            id="cusip-after-empty-loan-page",
        ),
        pytest.param(
            (_loan_row("Loan", lent="1"),),
            ("CUSIP: 99971T103",),
            "section",
            id="cusip-after-section",
        ),
        pytest.param(
            ("Dangling Name",),
            (_loan_row(lent="1"),),
            "nonloan-page",
            id="name-after-nonloan-page",
        ),
        pytest.param(
            ("Dangling Name",),
            (_loan_row(lent="1"),),
            "section",
            id="name-after-section",
        ),
    ],
)
def test_stock_loan_description_cannot_cross_a_table_boundary(
    before: tuple[str, ...], after: tuple[str, ...], gap: str
) -> None:
    if gap == "section":
        # An empty collateral ledger, a table start that ends the loan rows.
        boundary = ("Stock Lending - Collateral Activity (continued)", _LEDGER_HEADER)
        text = _with_loan_pages((*before, *boundary), after)
    else:
        text = _with_loan_pages(before, (), after)
        if gap == "nonloan-page":
            header = "\n".join(_stock_loan_activity_page().splitlines()[:2])
            text = text.replace(f"{header}\nPage", "Important Information\nPage")

    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)

    assert raised.value.message == "Stock Lending loan description is not attached"
    assert raised.value.raw_value == ""
    assert raised.value.row_index > 0


@pytest.mark.parametrize(
    ("bodies", "descriptions"),
    [
        pytest.param(
            ((_loan_row("Loan", lent="1"), _loan_row("Return", returned="1")),),
            ["Loan", "Return"],
            id="consecutive-rows-without-cusip",
        ),
        pytest.param(
            (("First Name Line",), ("Second Name Line", _loan_row(lent="1"))),
            ["First Name Line\nSecond Name Line"],
            id="leading-name-continues-on-next-page",
        ),
    ],
)
def test_stock_loan_rows_without_cusip_and_continued_leading_names_are_accepted(
    bodies: tuple[tuple[str, ...], ...], descriptions: list[str]
) -> None:
    statement = parse_robinhood_statement(_with_loan_pages(*bodies))
    assert [row.description for row in statement.stock_loan_activity] == descriptions


def test_stock_loan_activity_heading_must_be_single_and_above_its_header() -> None:
    page = _stock_loan_activity_page()
    with pytest.raises(ParseError, match="loan-activity heading is repeated"):
        parse_robinhood_statement(_statement().replace(page, f"{page}\n{page}"))

    heading, header, *rows = page.splitlines()
    with pytest.raises(ParseError, match="loan-activity header is missing"):
        parse_robinhood_statement(_statement().replace(page, "\n".join((header, heading, *rows))))


def test_sweep_and_stock_lending_tables_consume_or_refuse_text() -> None:
    sweep_ledger = _ledger_page(
        heading="Deposit Sweep Activity",
        opening="Opening Sweep Balance",
        closing="Closing Sweep Balance",
        total="Total Swept Funds",
    )
    bad_ledger = sweep_ledger.replace(
        "Total Swept Funds",
        "Unknown Ledger Text\nTotal Swept Funds",
    )
    with pytest.raises(ParseError, match="unsupported in-table line"):
        parse_robinhood_statement(_statement().replace(sweep_ledger, bad_ledger))

    bank_intro = (
        "As of the closing date of this statement, the annual percentage yield "
        "is described below.\n"
        "interest on your swept cash, minus any fees paid to Robinhood.\n"
    )
    sweep_page_with_intro = f"{_sweep_bank_page()}\n\n{bank_intro}"
    wrapped_bank_page = sweep_page_with_intro.replace(
        "Example Program Bank                                         $20.00",
        f"Example Program\n{_fixed((0, 60), ('Bank', '$20.00'))}",
    )
    wrapped_bank = parse_robinhood_statement(
        _statement().replace(_sweep_bank_page(), wrapped_bank_page)
    )
    assert wrapped_bank.sweep_banks[0].bank_name == "Example Program\nBank"

    bank_row = "Example Program Bank                                         $20.00"
    malformed_bank_page = sweep_page_with_intro.replace(
        bank_row, f"{_fixed((0, 60), ('Example Program Bank', 'NOT-MONEY'))}\n{bank_row}"
    )
    with pytest.raises(ParseError, match="Program Bank row is incomplete"):
        parse_robinhood_statement(_statement().replace(_sweep_bank_page(), malformed_bank_page))

    loaned_starts = (0, 55, 72, 91, 106)
    loaned_row = _fixed(
        loaned_starts,
        ("Loaned Example", "LOAN", "1", "$5.0000", "$5.00"),
    )
    trailing_loaned_page = _stock_loaned_page().replace(
        "Total Market Value $5.00",
        "Trailing Loaned Note\nTotal Market Value $5.00",
    )
    trailing_loaned = parse_robinhood_statement(
        _statement().replace(_stock_loaned_page(), trailing_loaned_page)
    )
    assert trailing_loaned.stock_loaned_securities[0].description.endswith("Trailing Loaned Note")

    partial_loaned_page = _stock_loaned_page().replace(
        loaned_row,
        _fixed(loaned_starts, ("Loaned Example", "LOAN", "1", "", "")),
    )
    with pytest.raises(ParseError, match="security row is incomplete"):
        parse_robinhood_statement(_statement().replace(_stock_loaned_page(), partial_loaned_page))


def test_ungrouped_thousands_in_income_refuse_instead_of_truncating() -> None:
    with pytest.raises(ParseError, match="Income and Expense Summary row is malformed"):
        parse_robinhood_statement(
            _statement().replace(
                "Dividends $1.00 $1.00",
                "Dividends $1234.56 $1234.56",
            )
        )


# ---- #909: summary rows and holdings totals are read whole ------------------

_AS_ROW = "Portfolio Value $260.00 $325.00"
_INCOME_ROW = "Dividends $1.00 $1.00"
_TOTAL_ROW = "Total Securities $300.00 100.00%"
_PRICED_ROW = "Total Priced Portfolio $325.00"
_UNEQUAL_TOTAL = "holdings total does not equal the rows it covers"


def _refusal(text: str) -> ParseError:
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)
    return raised.value


@pytest.mark.parametrize(
    ("row", "changed", "section", "label"),
    [
        (_AS_ROW, "Portfolio Value €260.00 €325.00", "Account Summary", "Portfolio Value"),
        (_AS_ROW, "Portfolio Value $260.00* $325.00*", "Account Summary", "Portfolio Value"),
        (_AS_ROW, "Portfolio Value $260.00 $325.00 $999.00", "Account Summary", "Portfolio Value"),
        (_AS_ROW, "Portfolio Value $260.00 $325.00 restated", "Account Summary", "Portfolio Value"),
        (_INCOME_ROW, "Dividends €1.00 €1.00", "Income and Expense Summary", "Dividends"),
        (_INCOME_ROW, "Dividends $1.00* $1.00*", "Income and Expense Summary", "Dividends"),
        (_INCOME_ROW, "Dividends $1.00 $1.00 $1.00", "Income and Expense Summary", "Dividends"),
        (_INCOME_ROW, "Dividends $1.00 $1.00 restated", "Income and Expense Summary", "Dividends"),
    ],
    ids=[
        "account-euro",
        "account-marker",
        "account-third-value",
        "account-leftover-text",
        "income-euro",
        "income-marker",
        "income-third-value",
        "income-leftover-text",
    ],
)
def test_summary_rows_refuse_anything_but_their_two_usd_cells(
    row: str, changed: str, section: str, label: str
) -> None:
    error = _refusal(_statement().replace(row, changed, 1))

    assert (error.message, error.column, error.raw_value) == (
        f"{section} row is malformed",
        label,
        "",
    )
    assert not any(amount in str(error) for amount in ("1.00", "260.00", "325.00", "999.00"))


def _two_column_summary(*extra: tuple[str, str, str, str]) -> str:
    """The Account Summary rows as the statement prints them: the allocation
    legend sits in a column to their right and shares their lines."""

    rows = (
        ("Brokerage Cash Balance", "$5.00", "$5.00", ""),
        ("", "", "", "Cash and Cash Equivalents"),
        ("Deposit Sweep Balance", "$5.00", "$20.00", "8.33%"),
        ("", "", "", "Equities"),
        ("Total Securities *", "$250.00", "$300.00", ""),
        ("", "", "", "25.00%"),
        ("Portfolio Value", "$260.00", "$325.00", "Options"),
        ("", "", "", "66.67%"),
        *extra,
    )
    return "".join(f"{_fixed((0, 30, 45, 62), row)}\n" for row in rows)


def _with_summary(summary: str) -> str:
    text = _statement()
    start = text.index("Brokerage Cash Balance $5.00 $5.00")
    return text[:start] + summary + text[text.index("Income and Expense Summary") :]


def test_account_summary_rows_may_share_lines_with_the_allocation_legend() -> None:
    summary = parse_robinhood_statement(_with_summary(_two_column_summary())).account_summary

    assert summary.closing_portfolio_value_usd == Decimal("325.00")
    assert (
        summary.closing_cash_percent,
        summary.closing_equities_percent,
        summary.closing_options_percent,
    ) == (Decimal("8.33"), Decimal(25), Decimal("66.67"))

    third_value = _two_column_summary().replace(
        _fixed((0, 30, 45), ("Total Securities *", "$250.00", "$300.00")),
        _fixed((0, 30, 45, 62), ("Total Securities *", "$250.00", "$300.00", "$999.00")),
    )
    error = _refusal(_with_summary(third_value))
    assert (error.message, error.column) == (
        "Account Summary row is malformed",
        "Total Securities *",
    )

    error = _refusal(_with_summary(_two_column_summary(("", "", "", "$999.00"))))
    assert (error.message, error.column) == ("Account Summary contains an unsupported row", None)


def test_account_summary_row_refuses_a_legend_label_and_percentage_together() -> None:
    text = _statement().replace(_AS_ROW, f"{_AS_ROW} Options 66.67%", 1)
    error = _refusal(text.replace("\nOptions 66.67%", "", 1))
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "Account Summary row is malformed",
        "Portfolio Value",
        1,
        "",
    )
    assert "260.00" not in str(error) and "325.00" not in str(error)


@pytest.mark.parametrize(
    ("text", "message", "column"),
    [
        (
            _statement().replace(_AS_ROW, f"Margin Balance $1.00 $1.00\n{_AS_ROW}"),
            "Account Summary contains an unsupported row",
            None,
        ),
        (
            _statement(layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT).replace(
                "Total Securities * ", "Deposit Sweep Balance $0.00 $0.00\nTotal Securities * ", 1
            ),
            "Account Summary contains an unsupported row",
            "Deposit Sweep Balance",
        ),
        (
            _statement().replace(_AS_ROW, f"Net Account Balance $25.00 $25.00\n{_AS_ROW}"),
            "Account Summary layout generation is unsupported",
            None,
        ),
        (
            _statement().replace("Deposit Sweep Balance $5.00 $20.00\n", ""),
            "Account Summary row is missing",
            "Deposit Sweep Balance",
        ),
    ],
    ids=["unknown-row", "other-layout-row", "both-cash-rows", "missing-row"],
)
def test_account_summary_holds_only_its_layout_rows_and_the_legend(
    text: str, message: str, column: str | None
) -> None:
    error = _refusal(text)
    assert (error.message, error.column) == (message, column)


@pytest.mark.parametrize("second", ["agrees", "disagrees"])
@pytest.mark.parametrize(
    ("row", "disagreeing", "section", "label"),
    [
        (_AS_ROW, "Portfolio Value $260.00 $999.00", "Account Summary", "Portfolio Value"),
        (_INCOME_ROW, "Dividends $9.00 $9.00", "Income and Expense Summary", "Dividends"),
    ],
    ids=["portfolio-value", "dividends"],
)
def test_a_second_summary_row_with_the_same_label_refuses(
    row: str, disagreeing: str, section: str, label: str, second: str
) -> None:
    duplicate = row if second == "agrees" else disagreeing
    error = _refusal(_statement().replace(row, f"{row}\n{duplicate}", 1))
    assert (error.message, error.column) == (f"{section} row is duplicated", label)


_SECOND_INCOME_SUMMARY = (
    "Income and Expense Summary This Period Year to Date\n"
    "Dividends $9.00 $9.00\n"
    "Capital Gains Distributions $0.00 $0.00\n"
    "Interest Earned $0.00 $0.00\n"
    "Stock Lending $0.00 $0.00"
)
_SECOND_ACCOUNT_SUMMARY = (
    "Account Summary Opening Balance Closing Balance\n"
    "Brokerage Cash Balance $999.00 $999.00\n"
    "Cash and Cash Equivalents 100.00%\n"
    "Deposit Sweep Balance $0.00 $0.00\n"
    "Total Securities * $0.00 $0.00\n"
    "Equities 0.00%\n"
    "Portfolio Value $999.00 $999.00\n"
    "Options 0.00%\n"
    f"{_SECOND_INCOME_SUMMARY}"
)


@pytest.mark.parametrize(
    ("page", "heading"),
    [
        (_SECOND_ACCOUNT_SUMMARY, "Account Summary"),
        (_SECOND_INCOME_SUMMARY, "Income and Expense Summary"),
    ],
    ids=["account-and-income", "income-only"],
)
def test_a_second_summary_block_refuses_instead_of_being_ignored(page: str, heading: str) -> None:
    """The second block balances on its own (999 = 999 + 0 + 0), so only the
    one-block rule can catch it."""

    added_page = len(_statement().split("\f"))  # it goes in before the last page
    error = _refusal(_statement(extra_pages=(page,)))
    assert (error.message, error.row_index) == (f"{heading} is duplicated", added_page)


@pytest.mark.parametrize(
    ("removed", "message", "column"),
    [
        (
            "Income and Expense Summary This Period Year to Date\n",
            "Income and Expense Summary is missing",
            None,
        ),
        (
            "Account Summary Opening Balance Closing Balance\n",
            "Account Summary is missing",
            None,
        ),
        (
            "Interest Earned $0.10 $0.10\n",
            "Income and Expense Summary row is missing",
            "Interest Earned",
        ),
    ],
    ids=["income-heading", "account-heading", "income-row"],
)
def test_a_missing_summary_heading_or_row_refuses(
    removed: str, message: str, column: str | None
) -> None:
    error = _refusal(_statement().replace(removed, "", 1))
    assert (error.message, error.column, error.row_index) == (message, column, 1)


def test_income_summary_before_the_account_summary_refuses() -> None:
    text = _statement()
    account = text.index("Account Summary Opening Balance")
    income = text.index("Income and Expense Summary")
    end = text.index("Public disclosure text.")
    reordered = text[:account] + text[income:end] + text[account:income] + text[end:]

    error = _refusal(reordered)
    assert error.message == "Income and Expense Summary precedes the Account Summary"


def test_income_summary_ends_before_another_table_on_the_page() -> None:
    pages = _statement().split("\f")
    loan_page = next(
        index
        for index, page in enumerate(pages)
        if page.startswith("Stock Lending - Loan Activity")
    )
    loan_table = pages.pop(loan_page).rsplit("\nPage ", 1)[0]
    pages = [page.rsplit("\nPage ", 1)[0] for page in pages]
    pages[0] += f"\n{loan_table}"
    text = "\f".join(
        f"{page}\nPage {index} of {len(pages)}\n" for index, page in enumerate(pages, start=1)
    )

    statement = parse_robinhood_statement(text)

    assert [row.label for row in statement.income] == [
        "Dividends",
        "Capital Gains Distributions",
        "Interest Earned",
        "Stock Lending",
    ]
    assert len(statement.stock_loan_activity) == 1


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ("Total Securities $999.00 100.00%", _UNEQUAL_TOTAL),
        ("Total Securities * $300.00 $9.00 100.00%", _UNEQUAL_TOTAL),
        ("Total Securities €300.00 100.00%", _MALFORMED_TOTAL),
        ("Total Securities $300.00* 100.00%", _MALFORMED_TOTAL),
        ("Total Securities $300.00 $1.00 $1.00 100.00%", _MALFORMED_TOTAL),
        ("Total Securities $300.00 100.00% restated", _MALFORMED_TOTAL),
        ("Total Securities $300.00", _MALFORMED_TOTAL),
        ("Total Securities* $300.00 100.00%", _MALFORMED_TOTAL),
    ],
    ids=[
        "different-value",
        "different-dividend",
        "euro",
        "marker",
        "extra-value",
        "leftover-text",
        "no-percentage",
        "glued-marker",
    ],
)
def test_holdings_total_is_read_whole_and_equals_the_rows_it_covers(
    changed: str, message: str
) -> None:
    error = _refusal(_statement().replace(f"\n{_TOTAL_ROW}", f"\n{changed}", 1))
    assert (error.message, error.column, error.row_index) == (message, "Total Securities", 2)
    assert not any(amount in str(error) for amount in ("300.00", "999.00", "9.00"))


def test_a_missing_holdings_total_refuses() -> None:
    """Without its total block the rows run to the page end and the total is
    missing. Without only ``Total Securities``, its ``Deposit Sweep Balance``
    row sits among the rows, where the ``Deposit Sweep`` prefix starts no table."""

    block = "\n".join((_TOTAL_ROW, *_portfolio_total_lines(RobinhoodStatementLayout.DEPOSIT_SWEEP)))
    error = _refusal(_statement().replace(f"\n{block}", "", 1))
    assert (error.message, error.column, error.row_index) == (
        "holdings total is missing",
        "Total Securities",
        2,
    )

    error = _refusal(_statement().replace(f"\n{_TOTAL_ROW}", "", 1))
    assert (error.message, error.column, error.row_index) == (_NO_TABLE["holdings"], None, 2)


def test_holdings_total_keeps_its_marker_and_dividend_cell() -> None:
    statement = parse_robinhood_statement(
        _statement().replace(_TOTAL_ROW, "Total Securities * $300.00 $1.00 92.31%", 1)
    )
    [total] = [total for total in statement.totals if total.label == "Total Securities"]
    assert total.values == (("Amount1", Decimal("300.00")), ("Amount2", Decimal("1.00")))


_SWEEP_ROW = "Deposit Sweep Balance $20.00 6.15%"


@pytest.mark.parametrize(
    ("old", "new", "message", "column"),
    [
        (
            _PRICED_ROW,
            "Total Priced Portfolio $999.00",
            _UNEQUAL_TOTAL,
            "Total Priced Portfolio",
        ),
        (_PRICED_ROW, "Total Priced Portfolio €325.00", _MALFORMED_TOTAL, "Total Priced Portfolio"),
        (
            _PRICED_ROW,
            "Total Priced Portfolio $325.00 100.00%",
            _MALFORMED_TOTAL,
            "Total Priced Portfolio",
        ),
        (f"\n{_PRICED_ROW}", "", "holdings total is missing", "Total Priced Portfolio"),
        (_SWEEP_ROW, "Deposit Sweep Balance $20.00", _MALFORMED_TOTAL, "Deposit Sweep Balance"),
        (
            _SWEEP_ROW,
            f"{_SWEEP_ROW}\nDeposit Sweep Balance $0.00 0.00%",
            "holdings total block row is duplicated",
            "Deposit Sweep Balance",
        ),
        (
            _SWEEP_ROW,
            "Margin Balance $20.00 6.15%",
            "holdings total block contains an unsupported row",
            "Total Priced Portfolio",
        ),
        (
            _TOTAL_ROW,
            f"{_PRICED_ROW}\n{_TOTAL_ROW}",
            "holdings total is misplaced",
            "Total Priced Portfolio",
        ),
        (
            _PRICED_ROW,
            f"{_PRICED_ROW}\n{_TOTAL_ROW}",
            "holdings total is duplicated",
            "Total Securities",
        ),
        (
            _TOTAL_ROW,
            f"{_TOTAL_ROW}\n{_TOTAL_ROW}",
            "holdings total is duplicated",
            "Total Securities",
        ),
        (
            _PRICED_ROW,
            f"{_PRICED_ROW}\n{_PRICED_ROW}",
            "holdings total is duplicated",
            "Total Priced Portfolio",
        ),
    ],
    ids=[
        "different-value",
        "euro",
        "extra-cell",
        "missing",
        "cash-row-without-percentage",
        "duplicated-cash-row",
        "unknown-row",
        "priced-inside-the-table",
        "second-securities-total",
        "second-securities-total-before-cash",
        "second-priced-total",
    ],
)
def test_priced_portfolio_total_is_read_whole_and_equals_the_rows_it_covers(
    old: str, new: str, message: str, column: str
) -> None:
    error = _refusal(_statement().replace(old, new, 1))
    assert (error.message, error.column, error.row_index) == (message, column, 2)
    assert error.raw_value == ""
    assert not any(amount in str(error) for amount in ("20.00", "300.00", "325.00", "999.00"))


def test_portfolio_cash_row_cannot_hide_behind_the_page_footer() -> None:
    pages = _statement().split("\f")
    footer = f"Page 2 of {len(pages)}"
    pages[1] = (
        pages[1]
        .replace(f"\n{footer}\n", "\n")
        .replace(_SWEEP_ROW, f"Deposit Sweep Balance €20.00 6.15% {footer}")
        .replace(_PRICED_ROW, "Total Priced Portfolio $305.00")
    )
    error = _refusal("\f".join(pages))
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        _MALFORMED_TOTAL,
        "Deposit Sweep Balance",
        2,
        "",
    )
    assert "20.00" not in str(error) and "305.00" not in str(error)


@pytest.mark.parametrize(
    ("separator", "amount"),
    [
        (" ", "$9.00"),
        (" ", "€1000.00"),
        (" ", "USD 1000"),
        (" ", "1000 USD"),
        (" ", "(1000)"),
        (" ", "1000"),
        (" ", "2025"),
        (" ", "\u0661\u0660\u0660\u0660"),
        (" ", "\uff11\uff10\uff10\uff10"),
        ("\t", "€1000"),
        ("\u00a0", "€1000"),
    ],
)
def test_unknown_holdings_total_refuses_but_total_named_descriptions_parse(
    separator: str, amount: str
) -> None:
    """Inside a holdings table, a line that isn't a row, opens with the word
    ``Total`` and carries any digit is an unsupported total, not a description.
    A position named ``Total ...`` and a digit-free ``Total ...`` description
    line still parse."""

    text = _statement().replace("Example Equity", "Total Gadgets ", 1)
    text = text.replace(_TOTAL_ROW, f"Total Return Fund\n{_TOTAL_ROW}", 1)
    statement = parse_robinhood_statement(text)
    assert statement.positions[0].description == "Total Gadgets"
    assert statement.positions[-1].description.endswith("Total Return Fund")

    error = _refusal(
        text.replace(_TOTAL_ROW, f"Total{separator}Other Securities {amount}\n{_TOTAL_ROW}", 1)
    )
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "holdings total label is unsupported",
        "Total",
        2,
        "",
    )
    assert "9.00" not in str(error) and "1000" not in str(error)


@pytest.mark.parametrize(
    "unsupported",
    [
        "Total Account Activity $9.00",
        "Total Executed Trades Pending Settlement 2025",
        "Total Stock Lending - $9.00",
        "Total Deposit Sweep $9.00",
        "Total Important Information 2025",
        _position_header("Total Loaned Securities 2025")[0],
    ],
    ids=["activity", "pending-trades", "stock-lending", "deposit-sweep", "information", "header"],
)
def test_unknown_holdings_total_cannot_end_a_table(unsupported: str) -> None:
    header, starts = _position_header("Securities Held in Account")
    loaned_header, _starts = _position_header("Loaned Securities")
    positions = _position_page().splitlines()[2:5]
    page = "\n".join(
        (
            "Portfolio Summary",
            header,
            *positions,
            unsupported,
            loaned_header,
            "Total Securities * $300.00 $1.00 92.31%",
            *_portfolio_total_lines(RobinhoodStatementLayout.DEPOSIT_SWEEP),
        )
    )
    error = _refusal(_statement(position_pages=(page,)))
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "holdings total label is unsupported",
        "Total",
        2,
        "",
    )
    assert "9.00" not in str(error) and "2025" not in str(error)

    named_page = _position_page().replace(
        "Example Equity".ljust(starts[0]), "Total Deposit Sweep".ljust(starts[0]), 1
    )
    statement = parse_robinhood_statement(_statement(position_pages=(named_page,)))
    assert statement.positions[0].description == "Total Deposit Sweep"


def test_missing_holdings_table_names_the_total_and_a_physical_page() -> None:
    error = _refusal(_statement(position_pages=()))
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "holdings total is missing",
        "Total Securities",
        1,
        "",
    )


@pytest.mark.parametrize("empty_loaned", [True, False], ids=["empty-loaned", "loaned-row"])
def test_held_and_loaned_holdings_tables_can_share_a_page(empty_loaned: bool) -> None:
    header, _starts = _position_header("Securities Held in Account")
    loaned_header, _starts = _position_header("Loaned Securities")
    equity, option_description, option = _position_page().splitlines()[2:5]
    option_rows = (option_description, option)
    page = "\n".join(
        (
            "Portfolio Summary",
            header,
            equity,
            *(option_rows if empty_loaned else ()),
            loaned_header,
            *(option_rows if not empty_loaned else ()),
            "Total Securities * $300.00 $1.00 92.31%",
            *_portfolio_total_lines(RobinhoodStatementLayout.DEPOSIT_SWEEP),
        )
    )
    statement = parse_robinhood_statement(_statement(position_pages=(page,)))
    assert [row.symbol_or_cusip for row in statement.positions] == ["EQTY", "OPT"]
    assert statement.positions[1].location.section is (
        RobinhoodStatementSection.SECURITIES
        if empty_loaned
        else RobinhoodStatementSection.LOANED_SECURITIES
    )
    [total] = [total for total in statement.totals if total.label == "Total Securities"]
    assert total.location.section is RobinhoodStatementSection.LOANED_SECURITIES
    assert total.values == (("Amount1", Decimal("300.00")), ("Amount2", Decimal("1.00")))


def test_a_holdings_table_after_total_securities_refuses() -> None:
    error = _refusal(_statement(position_pages=(_position_page(), _position_page(loaned=True))))
    assert (error.message, error.column, error.row_index) == (
        "holdings total is misplaced",
        "Total Securities",
        3,
    )


def _spread_position_pages() -> tuple[str, str, str]:
    """The printed layout: the held table repeats its header on each page with
    no per-page total, and ``Total Securities`` closes the Loaned Securities
    table, on its own page, for the rows of both tables."""

    header, starts = _position_header("Securities Held in Account")
    loaned_header, _starts = _position_header("Loaned Securities")
    equity, option_description, option = _position_page().splitlines()[2:5]
    total = _fixed(
        (0, starts[4], starts[5], starts[7]),
        ("Total Securities *", "$300.00", "$1.00", "92.31%"),
    )
    return (
        "\n".join(("Portfolio Summary", header, equity)),
        "\n".join((header, option_description, option)),
        "\n".join(
            (
                "Portfolio Summary",
                loaned_header,
                total,
                *_portfolio_total_lines(RobinhoodStatementLayout.DEPOSIT_SWEEP),
            )
        ),
    )


def test_holdings_spread_over_pages_as_printed_parse() -> None:
    statement = parse_robinhood_statement(_statement(position_pages=_spread_position_pages()))

    assert [(row.location.page, row.symbol_or_cusip) for row in statement.positions] == [
        (2, "EQTY"),
        (3, "OPT"),
    ]
    assert statement.positions[1].description == "OPT 1/17/2025 Call $10.00"
    [securities_total] = [row for row in statement.totals if row.label == "Total Securities"]
    assert securities_total.location.page == 4
    assert securities_total.location.section is RobinhoodStatementSection.LOANED_SECURITIES
    assert securities_total.values == (
        ("Amount1", Decimal("300.00")),
        ("Amount2", Decimal("1.00")),
    )


# ---- #1007: each total is printed once and ends its table -------------------


def _ledger_total(kind: str, *cells: str) -> str:
    """The ledger's total row, or one with these Date, Debit, Credit and Balance cells."""
    label = _LEDGERS[kind][3]
    debit, credit = ("$20.00", "$10.00") if kind == "collateral" else ("$0.00", "$10.00")
    return _fixed(_LEDGER_STARTS, (label, *(cells or ("", debit, credit, ""))))


class _Table(NamedTuple):
    page: str  # the fixture page that prints the table
    number: int  # that page's number in _statement()
    label: str  # the table's total label
    row: str  # the fixture's total row
    wrong: str  # a well-formed total row that disagrees with the table's rows
    continued: str  # a later page's heading and column header
    subject: str  # how the refusals name the table


def _ledger_table(kind: str, number: int) -> _Table:
    heading, _opening, _closing, label = _LEDGERS[kind]
    return _Table(
        _ledger(kind),
        number,
        label,
        _ledger_total(kind),
        _ledger_total(kind, "", "$99.00", "$99.00", ""),
        f"{heading} (continued)\n{_LEDGER_HEADER}",
        heading,
    )


_TABLES = {
    "sweep": _ledger_table("sweep", 6),
    "collateral": _ledger_table("collateral", 9),
    "activity": _Table(
        _activity_page(),
        3,
        _ACTIVITY_TOTAL,
        _activity_total(),
        _activity_total({"Debit": "$99.00", "Credit": "$99.00"}),
        f"Account Activity (continued)\n{_activity_header()[0]}",
        "activity",
    ),
    "pending": _Table(
        _activity_page(pending=True),
        4,
        _PENDING_TOTAL,
        _activity_total(pending=True),
        _activity_total({"Debit": "$99.00", "Credit": "$99.00"}, pending=True),
        f"Executed Trades Pending Settlement (continued)\n{_activity_header(pending=True)[0]}",
        "activity",
    ),
    "loaned": _Table(
        _stock_loaned_page(),
        7,
        "Total Market Value",
        "Total Market Value $5.00",
        "Total Market Value $99.00",
        "",
        "Stock Lending market-value",
    ),
}


def _table_refusal(kind: str, text: str, message: str, *, page: int = 0) -> None:
    """The refusal names the page and the table's total label, and no amount."""

    table = _TABLES[kind]
    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        f"{table.subject} {message}",
        table.label,
        page or table.number,
        "",
    )
    assert re.search(r"[0-9]\.[0-9]", str(error)) is None


@pytest.mark.parametrize("second", ["agrees", "disagrees"])
@pytest.mark.parametrize(
    ("kind", "where"),
    [
        *(
            pytest.param(kind, where, id=f"{kind}-{where}")
            for kind in ("sweep", "collateral", "activity", "pending")
            for where in ("same-page", "later-page")
        ),
        pytest.param("loaned", "same-page", id="loaned-same-page"),
    ],
)
def test_a_second_total_refuses_whether_it_agrees_or_not(
    kind: str, where: str, second: str
) -> None:
    table = _TABLES[kind]
    duplicate = table.row if second == "agrees" else table.wrong
    if where == "same-page":
        text = _with_pages(table.page, table.page.replace(table.row, f"{table.row}\n{duplicate}"))
    else:
        text = _with_pages(table.page, table.page, f"{table.continued}\n{duplicate}")
    _table_refusal(kind, text, "total is repeated", page=table.number + (where == "later-page"))


@pytest.mark.parametrize(
    "trailing",
    [
        "dated-row",
        "Unknown text",
        "Portfolio Summary Unexpected text\nHidden malformed row",
        "Loaned Securities\nHidden malformed row",
    ],
    ids=["dated-row", "unknown-text", "heading-with-text", "bare-heading"],
)
@pytest.mark.parametrize("kind", ["sweep", "collateral", "loaned"])
def test_nothing_but_another_table_may_follow_a_ledger_or_loaned_total(
    kind: str, trailing: str
) -> None:
    """Unsupported rows and text below the total refuse. A heading-like line
    that starts no table refuses, so what is printed under it can't hide there.
    """

    if trailing == "dated-row":
        trailing = (
            _fixed((0, 55, 72, 91, 106), ("Second Loan", "LOAN2", "1", "$7.0000", "$7.00"))
            if kind == "loaned"
            else _fixed(_LEDGER_STARTS, ("Movement", "1/20/2025", "", "$99.00", "$119.00"))
        )
    page = _TABLES[kind].page
    _table_refusal(
        kind, _with_pages(page, f"{page}\n{trailing}"), "total is followed by an unsupported line"
    )


_REMINDERS = {
    "loaned": (
        "As a reminder, you can sell your shares as usual, even if they are on loan. The "
        "provisions of the Securities Investor Protection Act of 1970 may not protect you "
        "with respect to loaned securities."
    ),
    "collateral": (
        "Collateral held for securities loaned through Stock Lending is at least equal to the "
        "market value of such securities. The provisions of the Securities Investor Protection "
        "Act of 1970 may not protect you with respect to such loaned securities."
    ),
}


def _ending_page(page: str, *tail: str) -> str:
    """The statement with ``page``'s marker printed at its top, so ``tail`` ends it."""
    pages = _statement().split("\f")
    [index] = [index for index, part in enumerate(pages) if part.startswith(f"{page}\nPage ")]
    marker = pages[index].removeprefix(f"{page}\n").strip()
    pages[index] = "\n".join((marker, page, *tail)) + "\n"
    return "\f".join(pages)


@pytest.mark.parametrize("kind", list(_REMINDERS))
def test_a_stock_lending_total_may_end_its_page_with_its_sipc_reminder(kind: str) -> None:
    statement = parse_robinhood_statement(
        _ending_page(_TABLES[kind].page, "", _REMINDERS[kind], "")
    )
    base = parse_robinhood_statement(_statement())
    for rows in ("stock_loaned_securities", "stock_collateral_activity"):
        assert [row.description for row in getattr(statement, rows)] == [
            row.description for row in getattr(base, rows)
        ]
    assert [(total.label, total.values) for total in statement.totals] == [
        (total.label, total.values) for total in base.totals
    ]


@pytest.mark.parametrize(
    "change",
    ["text-after", "twice", "altered", "truncated", "other-tables", "before-the-marker"],
)
@pytest.mark.parametrize("kind", list(_REMINDERS))
def test_only_the_tables_exact_reminder_may_end_its_page(kind: str, change: str) -> None:
    page = _TABLES[kind].page
    own = _REMINDERS[kind]
    [other] = [reminder for name, reminder in _REMINDERS.items() if name != kind]
    text = {
        "text-after": _ending_page(page, "", own, "Unknown text"),
        "twice": _ending_page(page, "", own, own),
        "altered": _ending_page(page, "", f"{own} restated"),
        "truncated": _ending_page(page, "", own[:-1]),
        "other-tables": _ending_page(page, "", other),
        "before-the-marker": _with_pages(page, f"{page}\n\n{own}"),
    }[change]
    _table_refusal(kind, text, "total is followed by an unsupported line")


@pytest.mark.parametrize("reminder", list(_REMINDERS))
def test_the_sweep_ledger_has_no_reminder(reminder: str) -> None:
    page = _TABLES["sweep"].page
    _table_refusal(
        "sweep",
        _ending_page(page, "", _REMINDERS[reminder]),
        "total is followed by an unsupported line",
    )


@pytest.mark.parametrize("where", ["same-page", "later-page"])
@pytest.mark.parametrize("kind", ["activity", "pending"])
def test_an_activity_row_below_its_section_total_refuses(kind: str, where: str) -> None:
    """The fixture's ``Purchase`` or ``Pending Buy`` row, moved below the
    total, would otherwise enter the sums the total was checked against. On
    the total's page only blank lines, the whole-line marker or another
    table may follow it (#1022)."""

    table = _TABLES[kind]
    row = table.page.splitlines()[2]
    without = table.page.replace(f"{row}\n", "", 1)
    if where == "same-page":
        text = _with_pages(table.page, f"{without}\n{row}")
        _table_refusal(kind, text, "total is followed by an unsupported line")
    else:
        text = _with_pages(table.page, without, f"{table.continued}\n{row}")
        _table_refusal(kind, text, "row follows its section total", page=table.number + 1)


def _ledger_shapes(kind: str) -> dict[str, str]:
    label = _LEDGERS[kind][3]
    _date, debit, credit, _balance = (
        ("", "$20.00", "$10.00", "") if kind == "collateral" else ("", "$0.00", "$10.00", "")
    )
    return {
        "date": _ledger_total(kind, "1/31/2025", debit, credit, ""),
        "balance": _ledger_total(kind, "", debit, credit, "$999.00"),
        "label-marker": _ledger_total(kind).replace(label, f"{label}*", 1),
        "words-after-label": _ledger_total(kind).replace(label, f"{label} restated", 1),
        "no-dollar": _ledger_total(kind, "", debit[1:], credit[1:], ""),
    }


def _activity_shapes(*, pending: bool) -> dict[str, str]:
    label, other = (
        (_PENDING_TOTAL, _ACTIVITY_TOTAL) if pending else (_ACTIVITY_TOTAL, _PENDING_TOTAL)
    )
    debit, credit = ("10.00", "0.00") if pending else ("5.00", "10.00")
    changes: dict[str, dict[str, str]] = {
        "date": {"Trade Date" if pending else "Date": "1/31/2025"},
        **({} if pending else {"symbol": {"Symbol": "EQTY"}}),
        "quantity": {"Qty": "1"},
        "price": {"Price": "$5.0000"},
        "label-marker": {"Description": f"{label}*"},
        "text-before-label": {"Description": f"X {label}"},
        "no-dollar": {"Debit": debit, "Credit": credit},
        "swapped-label": {"Description": other},
    }
    return {shape: _activity_total(cells, pending=pending) for shape, cells in changes.items()}


_TOTAL_SHAPES = {
    "sweep": _ledger_shapes("sweep"),
    "collateral": _ledger_shapes("collateral"),
    "activity": _activity_shapes(pending=False),
    "pending": _activity_shapes(pending=True),
    "loaned": {
        "euro": "Total Market Value €5.00",
        "amount-marker": "Total Market Value $5.00*",
        "words-after-amount": "Total Market Value $5.00 Unknown text",
        "no-dollar": "Total Market Value 5.00",
        "label-marker": "Total Market Value* $5.00",
    },
}


@pytest.mark.parametrize("followed", [False, True], ids=["alone", "then-the-right-total"])
@pytest.mark.parametrize(
    ("kind", "shape"),
    [
        pytest.param(kind, shape, id=f"{kind}-{shape}")
        for kind, shapes in _TOTAL_SHAPES.items()
        for shape in shapes
    ],
)
def test_a_total_row_has_its_whole_supported_shape(kind: str, shape: str, followed: bool) -> None:
    table = _TABLES[kind]
    row = _TOTAL_SHAPES[kind][shape]
    changed = table.page.replace(table.row, f"{row}\n{table.row}" if followed else row)
    _table_refusal(kind, _with_pages(table.page, changed), "total is malformed")


@pytest.mark.parametrize("kind", ["sweep", "collateral", "activity", "pending"])
def test_a_total_row_has_no_text_before_its_first_column(kind: str) -> None:
    table = _TABLES[kind]
    heading, *lines = table.page.splitlines()
    indented = "\n".join((heading, *(f"  {line}" for line in lines)))
    parse_robinhood_statement(_with_pages(table.page, indented))
    changed = indented.replace(f"  {table.row}", f"X {table.row}")
    _table_refusal(kind, _with_pages(table.page, changed), "total is malformed")


@pytest.mark.parametrize("kind", list(_LEDGERS))
def test_a_present_ledger_requires_its_opening_closing_and_total(kind: str) -> None:
    table = _TABLES[kind]
    empty = "\n".join(table.page.splitlines()[:2])
    _table_refusal(
        kind,
        _with_pages(table.page, empty),
        "opening, closing, or total row is missing",
    )
    omitted = parse_robinhood_statement(_with_pages(table.page))
    rows = "stock_collateral_activity" if kind == "collateral" else "sweep_activity"
    assert getattr(omitted, rows) == ()


@pytest.mark.parametrize("kind", list(_LEDGERS))
@pytest.mark.parametrize(
    ("change", "message", "label"),
    [
        pytest.param(change, message, label, id=change)
        for change, message, label in (
            ("repeated-opening", "must open with exactly one opening row", 1),
            ("row-before-opening", "must open with exactly one opening row", 1),
            ("label-with-text", "row label is malformed", 1),
            ("repeated-closing", "row follows its closing row", 2),
            ("dated-row-after-closing", "row follows its closing row", 2),
            ("total-before-closing", "total precedes its closing row", 3),
        )
    ],
)
def test_a_ledger_opens_and_closes_once_before_its_total(
    kind: str, change: str, message: str, label: int
) -> None:
    heading, header, opening, movement, closing, total = _ledger(kind).splitlines()
    closing_balance = "$0.00" if kind == "collateral" else "$20.00"
    rows = {
        "repeated-opening": (opening, opening, movement, closing, total),
        "row-before-opening": (movement, opening, movement, closing, total),
        "label-with-text": (
            _fixed(_LEDGER_STARTS, (f"{_LEDGERS[kind][1]}*", "1/1/2025", "", "", "$10.00")),
            movement,
            closing,
            total,
        ),
        "repeated-closing": (opening, movement, closing, closing, total),
        "dated-row-after-closing": (
            opening,
            movement,
            closing,
            _fixed(_LEDGER_STARTS, ("Movement", "1/31/2025", "", "", closing_balance)),
            total,
        ),
        "total-before-closing": (opening, movement, total, closing),
    }[change]
    error = _refusal(_with_pages(_ledger(kind), "\n".join((heading, header, *rows))))
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        f"{heading} {message}",
        _LEDGERS[kind][label],
        _TABLES[kind].number,
        "",
    )
    assert re.search(r"[0-9]\.[0-9]", str(error)) is None


@pytest.mark.parametrize(
    ("kind", "split", "rows"),
    [
        ("sweep", 4, "sweep_activity"),
        ("collateral", 4, "stock_collateral_activity"),
        ("activity", 5, "activities"),
        ("pending", 3, "activities"),
    ],
)
def test_a_ledger_or_activity_section_spread_over_two_pages_has_one_final_total(
    kind: str, split: int, rows: str
) -> None:
    table = _TABLES[kind]
    lines = table.page.splitlines()
    text = _with_pages(
        table.page,
        "\n".join(lines[:split]),
        "\n".join((table.continued, *lines[split:])),
    )

    statement = parse_robinhood_statement(text)
    base = parse_robinhood_statement(_statement())
    assert [
        (row.description, row.debit_usd, row.credit_usd) for row in getattr(statement, rows)
    ] == [(row.description, row.debit_usd, row.credit_usd) for row in getattr(base, rows)]
    [spread] = [total for total in statement.totals if total.label == table.label]
    [printed] = [total for total in base.totals if total.label == table.label]
    assert (spread.location.page, spread.values) == (table.number + 1, printed.values)


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize(
    ("upper", "lower"),
    [
        (_ledger("sweep"), _stock_loaned_page()),
        (_stock_loaned_page(), _stock_loan_activity_page()),
        (_ledger("sweep"), _ledger("collateral")),
        (_ledger("collateral"), _ledger("sweep")),
    ],
    ids=[
        "below-a-ledger",
        "below-a-loaned-table",
        "collateral-below-sweep",
        "sweep-below-collateral",
    ],
)
def test_another_table_may_start_below_a_finished_table(
    upper: str, lower: str, continued: bool
) -> None:
    heading, *rest = lower.splitlines()
    below = "\n".join((f"{heading} (continued)" if continued else heading, *rest))
    text = _with_pages(lower, text=_with_pages(upper, f"{upper}\n\n{below}"))

    statement = parse_robinhood_statement(text)
    base = parse_robinhood_statement(_statement())
    for rows in (
        "sweep_activity",
        "stock_collateral_activity",
        "stock_loaned_securities",
        "stock_loan_activity",
    ):
        assert [row.description for row in getattr(statement, rows)] == [
            row.description for row in getattr(base, rows)
        ]
    assert [total.values for total in statement.totals] == [total.values for total in base.totals]


_LOANED_STARTS = (0, 55, 72, 91, 106)


def _loaned_row(symbol: str, price: str, value: str, description: str = "") -> str:
    return _fixed(_LOANED_STARTS, (description, symbol, "1", price, value))


def _loaned_table(*lines: str) -> str:
    return _with_pages(
        _stock_loaned_page(), loaned_securities_page(_LOANED_STARTS, lines, "$7.00", line=_fixed)
    )


_FIRST_LOAN = _loaned_row("AAA", "$5.0000", "$5.00")
_SECOND_LOAN = _loaned_row("BBB", "$2.0000", "$2.00")


@pytest.mark.parametrize(
    ("inline", "beta"),
    [
        ("", "Samplewood Inc\nCUSIP: 99961L102"),
        ("Samplewood inline", "Samplewood Inc\nSamplewood inline\nCUSIP: 99961L102"),
    ],
    ids=["names-above-rows", "name-above-and-inline"],
)
def test_a_loaned_row_takes_the_lines_above_it_and_its_cusip_below(inline: str, beta: str) -> None:
    text = _loaned_table(
        "Examplar Corp",
        _FIRST_LOAN,
        "CUSIP: 99971R107",
        "Samplewood Inc",
        _loaned_row("BBB", "$2.0000", "$2.00", inline),
        "CUSIP: 99961L102",
    )
    statement = parse_robinhood_statement(text)
    assert [(row.symbol, row.description) for row in statement.stock_loaned_securities] == [
        ("AAA", "Examplar Corp\nCUSIP: 99971R107"),
        ("BBB", beta),
    ]


_SECOND_LOAN_NAMED = _loaned_row("BBB", "$2.0000", "$2.00", "Samplewood Inc")


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(
            ("Examplar Corp", _FIRST_LOAN, "Samplewood Inc", _SECOND_LOAN, "CUSIP: 99961L102"),
            id="name-under-a-row-with-a-row-below",
        ),
        pytest.param(
            ("CUSIP: 99971R107", "Examplar Corp", _FIRST_LOAN, _SECOND_LOAN_NAMED),
            id="cusip-above-row",
        ),
        pytest.param(
            (
                "Examplar Corp",
                _FIRST_LOAN,
                "CUSIP: 99971R107",
                "CUSIP: 99961L102",
                _SECOND_LOAN_NAMED,
            ),
            id="second-cusip",
        ),
        pytest.param(
            ("Examplar Corp", _FIRST_LOAN, "Extra Text", "CUSIP: 99971R107", _SECOND_LOAN_NAMED),
            id="text-between-row-and-cusip",
        ),
        pytest.param(
            ("Examplar Corp", _FIRST_LOAN, "CUSIP: 12345", _SECOND_LOAN_NAMED), id="malformed-cusip"
        ),
    ],
)
def test_a_loaned_description_line_that_belongs_to_no_single_row_refuses(
    lines: tuple[str, ...],
) -> None:
    error = _refusal(_loaned_table(*lines))
    assert (error.message, error.row_index, error.raw_value) == (
        "Stock Lending description is not attached",
        7,
        "",
    )


@pytest.mark.parametrize("repeat", ["table", "heading", "header"])
def test_a_page_prints_one_loaned_table(repeat: str) -> None:
    page = _stock_loaned_page()
    heading, header, row, total = page.splitlines()
    changed = {
        "table": f"{page}\n{page}",
        "heading": "\n".join((heading, header, row, total, heading)),
        "header": "\n".join((heading, header, row, total, header)),
    }[repeat]
    error = _refusal(_with_pages(page, changed))
    assert (error.message, error.row_index) == (
        "Stock Lending securities table is repeated on the page",
        7,
    )


def test_a_heading_inside_the_loaned_rows_leaves_the_table_without_its_total() -> None:
    page = _stock_loaned_page()
    changed = page.replace("Total Market Value", "Portfolio Summary\nTotal Market Value")
    error = _refusal(_with_pages(page, changed))
    assert (error.message, error.column, error.row_index) == (
        "Stock Lending market-value total is missing",
        "Total Market Value",
        7,
    )


@pytest.mark.parametrize(
    "legend",
    [
        "99.00%\nCash and Cash Equivalents 8.33%",
        "Cash and Cash Equivalents 8.33%\n99.00%",
        "Cash and Cash Equivalents\n8.33%\n99.00%",
    ],
    ids=["before-the-first-label", "after-a-labelled-percentage", "after-a-wrapped-percentage"],
)
def test_an_allocation_percentage_that_no_label_owns_refuses(legend: str) -> None:
    owned = legend.replace("99.00%\n", "").replace("\n99.00%", "")
    summary = parse_robinhood_statement(
        _statement().replace("Cash and Cash Equivalents 8.33%", owned, 1)
    ).account_summary
    assert summary.closing_cash_percent == Decimal("8.33")

    error = _refusal(_statement().replace("Cash and Cash Equivalents 8.33%", legend, 1))
    assert (error.message, error.row_index, error.raw_value) == (
        "Portfolio Summary allocation has an extra percentage",
        1,
        "",
    )
    assert "99.00" not in str(error)


@pytest.mark.parametrize("position", ["above", "below"])
@pytest.mark.parametrize(
    "page",
    [2, 5, 6, 7, 8, 9, 10, 11],
    ids=[
        "holdings",
        "activity",
        "pending",
        "program-banks",
        "sweep-ledger",
        "loaned",
        "loan-activity",
        "collateral-ledger",
    ],
)
def test_a_page_marker_line_holds_only_the_marker(page: int, position: str) -> None:
    """A table page's marker above or below its tables carries no other text.

    Spreading the holdings over pages 2-4 exercises each listed reader.
    """

    text = _statement(position_pages=_spread_position_pages())
    marker = f"Page {page} of 12"
    if position == "above":
        pages = text.split("\f")
        lines = pages[page - 1].splitlines()
        pages[page - 1] = "\n".join((marker, *(line for line in lines if line != marker))) + "\n"
        text = "\f".join(pages)
    parse_robinhood_statement(text)
    assert text.splitlines().count(marker) == 1
    error = _refusal(text.replace(marker, f"Unexpected text {marker}"))
    assert (error.message, error.row_index, error.raw_value) == (
        "page marker line carries other text",
        page,
        "",
    )


@pytest.mark.parametrize(
    "text",
    [_crypto_statement(), _crypto_statement().upper(), _crypto_statement().lower()],
    ids=["as-printed", "upper", "lower"],
)
def test_crypto_statement_is_identified_in_any_letter_case_and_refused(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(statement_parser, "pdftotext_layout", lambda _path, **_: text)

    assert read_robinhood_statement_kind(Path("crypto.pdf")) is RobinhoodStatementKind.CRYPTO
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)
    assert raised.value.message == CRYPTO_REFUSAL


def test_brokerage_text_that_mentions_robinhood_crypto_parses_as_brokerage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _statement().replace("Unlisted Continuation", "Transfer to Robinhood Crypto")
    monkeypatch.setattr(statement_parser, "pdftotext_layout", lambda _path, **_: text)

    assert read_robinhood_statement_kind(Path("brokerage.pdf")) is RobinhoodStatementKind.BROKERAGE
    assert parse_robinhood_statement(text).activities[1].description == (
        "Internal Security Transfer from 111111111 to 222222222\nTransfer to Robinhood Crypto"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Unrelated document\nThis data does not include Robinhood Crypto\n",
        _crypto_statement().replace("Robinhood Crypto", "Example Exchange"),
        _crypto_statement().replace("01-2025", "January 2025"),
        f"{_crypto_statement()}\fUnrelated page\n",
    ],
    ids=["marker-only", "no-marker", "no-period-line", "untitled-page"],
)
def test_text_that_is_not_positively_crypto_goes_to_the_brokerage_parser(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(statement_parser, "pdftotext_layout", lambda _path, **_: text)

    assert read_robinhood_statement_kind(Path("other.pdf")) is RobinhoodStatementKind.BROKERAGE
    with pytest.raises(ParseError) as raised:
        parse_robinhood_statement(text)
    assert raised.value.message not in {CRYPTO_REFUSAL, MIXED_FAMILIES_REFUSAL}


@pytest.mark.parametrize(
    "text",
    [
        f"{_crypto_statement()}\f{_statement()}",
        f"{_statement()}\f{_crypto_statement()}",
        _crypto_statement().replace("Bitcoin (BTC)", "Transfer from Robinhood Securities"),
    ],
    ids=["crypto-first", "brokerage-first", "crypto-naming-rhs"],
)
def test_text_mixing_crypto_and_brokerage_families_refuses(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr(statement_parser, "pdftotext_layout", lambda _path, **_: text)

    with pytest.raises(ParseError) as triaged:
        read_robinhood_statement_kind(Path("mixed.pdf"))
    with pytest.raises(ParseError) as parsed:
        parse_robinhood_statement(text)
    for raised in (triaged, parsed):
        assert raised.value.message == MIXED_FAMILIES_REFUSAL
        assert raised.value.raw_value == ""


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (
            _statement().replace("Page 1 of 10", "Page 2 of 10", 1),
            "statement page marker does not match physical pagination",
        ),
        (
            _statement().replace("Individual Account #: 111111111", ""),
            "statement must contain exactly one page-1 account identity",
        ),
        (
            _statement().replace("1/1/2025 to 1/31/2025", "1/31/2025 to 1/1/2025"),
            "statement period start is after its end",
        ),
        (
            _statement().replace("Robinhood Securities, LLC", "Unknown Custodian"),
            "statement RHF/RHS legal-role disclosure is missing",
        ),
        (
            _statement().replace(
                "Portfolio Value $260.00 $325.00", "Portfolio Value $260.00 $324.00"
            ),
            "closing Account Summary components do not equal portfolio value",
        ),
        (
            _statement().replace("$200.00", "$199.00", 1),
            "position quantity and price do not reproduce market value",
        ),
        # The renamed total keeps its amounts, so it reads as a shifted continuation.
        (
            _statement().replace(_ACTIVITY_TOTAL, "Unknown Activity Total"),
            "activity row continuation is column-shifted",
        ),
        (
            _statement().replace("Example Program Bank", "Example Program Bank\nUnexpected"),
            "non-zero Deposit Sweep balance has no Program Bank rows",
        ),
        (
            _statement()
            .replace("Variable public disclosure prose.", "Unknown Page")
            .replace("Important Information\n", ""),
            "statement page has no accepted content family",
        ),
    ],
    ids=[
        "wrong-page-marker",
        "missing-identity",
        "backwards-period",
        "missing-custodian",
        "bad-account-summary",
        "bad-option-value",
        "missing-activity-total",
        "shifted-sweep-bank",
        "unknown-page",
    ],
)
def test_structural_and_arithmetic_failures_refuse(bad: str, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse_robinhood_statement(bad)


def test_model_repr_and_errors_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = parse_robinhood_statement(_statement())
    assert "111111111" not in repr_without_addresses(statement)
    assert "TAX PAYER" not in repr(statement)

    private_path = tmp_path / "SECRET-STATEMENT.pdf"

    def fail(*args: object, **kwargs: object) -> object:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=["pdftotext", str(private_path)],
            stderr=f"failed to open {private_path}",
        )

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ParseError) as caught:
        read_robinhood_statement(private_path)
    rendered = str(caught.value)
    assert str(private_path) not in rendered
    assert "failed to open" not in rendered


@private_test
def test_private_statement_tree_is_fully_classified() -> None:
    """Each PDF in a statement folder parses as a brokerage statement, or
    refuses as Crypto exactly when it sits in a ``crypto`` folder (the
    layout the paired loader relies on); a parsed statement's page count
    matches the PDF's own page tree."""
    root, paths = private_robinhood_pdfs("statement")
    for path in paths:
        in_crypto_folder = "crypto" in path.relative_to(root).parts[:-1]
        statement = read_private_statement(path)
        if isinstance(statement, str):
            if statement != CRYPTO_REFUSAL or not in_crypto_folder:
                pytest.fail("private statement bundle classification failed", pytrace=False)
            continue
        if in_crypto_folder:
            pytest.fail("private statement bundle classification failed", pytrace=False)
        valid = (
            statement.period_start <= statement.period_end
            and statement.page_count == private_pdf_page_count(path)
        )
        if not valid:
            pytest.fail("private statement bundle retention audit failed", pytrace=False)


# ---- #1009: a description holds at most one identity; a header, each label once --


def _identity_refusal(
    text: str, message: str, *, page: int, column: str | None = "Description"
) -> None:
    """The refusal names the page and the column, and no account, symbol, date or amount."""

    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        message,
        column,
        page,
        "",
    )
    rendered = str(error)
    assert f"row {page}" in rendered
    for value in ("111111111", "222222222", "OPT", "XQGD", "EQTY"):
        assert value not in rendered
    assert re.search(r"\$|[0-9][./][0-9]", rendered) is None


_PAIR = "Internal Security Transfer from 111111111 to 222222222"
_SWAPPED_PAIR = "Internal Security Transfer from 222222222 to 111111111"
_SECOND_TRANSFER = "activity description holds more than one transfer identity"
_MALFORMED_TRANSFER = "activity transfer identity is malformed"


def _itrf_statement(description: str, *below: str) -> str:
    """The fixture plus a quantity-only ITRF row, described by ``description``
    and the lines ``below`` it, just above the Account Activity total."""

    _header, _labels, starts = _activity_header()
    row = _fixed(starts, (description, "EQTY", "Margin", "ITRF", "1/12/2025", "1", "", "", ""))
    page = _activity_page().replace(_activity_total(), "\n".join((row, *below, _activity_total())))
    return _statement().replace(_activity_page(), page)


def test_one_well_formed_transfer_pair_reads_as_before() -> None:
    statement = parse_robinhood_statement(_itrf_statement(_PAIR))
    row = statement.activities[2]
    assert row.description == _PAIR
    assert (row.source_account_reference, row.destination_account_reference) == (
        "111111111",
        "222222222",
    )
    assert statement.activities[1].source_account_reference == "111111111"


@pytest.mark.parametrize(
    ("description", "below", "message"),
    [
        pytest.param(_PAIR, (_SWAPPED_PAIR,), _SECOND_TRANSFER, id="pair-then-swapped"),
        pytest.param(_SWAPPED_PAIR, (_PAIR,), _SECOND_TRANSFER, id="swapped-then-pair"),
        pytest.param(_PAIR, (_PAIR,), _SECOND_TRANSFER, id="agreeing-pairs"),
        pytest.param(_PAIR, (_SWAPPED_PAIR.lower(),), _SECOND_TRANSFER, id="lower-case-second"),
        pytest.param(f"{_PAIR}X", (), _MALFORMED_TRANSFER, id="destination-not-a-token"),
        pytest.param(
            _PAIR,
            ("Internal Security Transfer from 222222222 to UNKNOWN",),
            _SECOND_TRANSFER,
            id="pair-then-unknown",
        ),
        pytest.param(
            "Internal Security Transfer from 111111111X to 222222222",
            (),
            _MALFORMED_TRANSFER,
            id="source-not-a-token",
        ),
        pytest.param("Internal Security Transfer", (), _MALFORMED_TRANSFER, id="phrase-alone"),
    ],
)
def test_a_second_or_malformed_transfer_identity_refuses(
    description: str, below: tuple[str, ...], message: str
) -> None:
    _identity_refusal(_itrf_statement(description, *below), message, page=3)


# ---- #1094: every CUSIP line in an activity description is read whole --------


_MALFORMED_CUSIP = "activity row has a malformed CUSIP line"


def _under_last_row(*lines: str, pending: bool) -> tuple[str, str]:
    """The fixture with ``lines`` under its section's last dated row, and that
    row's own description: the ITRF row ``_itrf_statement`` adds, or the
    pending trade. Either row is ``activities[2]``."""

    if not pending:
        return _itrf_statement(_PAIR, *lines), _PAIR
    page = _activity_page(pending=True)
    total = _activity_total(pending=True)
    changed = page.replace(total, "\n".join((*lines, total)))
    return _statement().replace(page, changed), "Pending Buy"


def _cusip_refusal(text: str, *, page: int, markers: tuple[str, ...]) -> None:
    """The refusal names the page and the Description column, and no marker."""

    error = _refusal(text)
    assert (error.section, error.message, error.column, error.row_index, error.raw_value) == (
        "Robinhood brokerage statement",
        _MALFORMED_CUSIP,
        "Description",
        page,
        "",
    )
    for marker in markers:
        assert marker in text
        assert marker not in str(error)


@pytest.mark.parametrize("pending", [False, True], ids=["account-activity", "pending-trades"])
@pytest.mark.parametrize(
    ("lines", "markers"),
    [
        pytest.param(("CUSIP: 999990104*",), ("99999",), id="footnote-mark"),
        pytest.param(("CUSIP: 99999010",), ("99999",), id="eight-characters"),
        pytest.param(("CUSIP: 9999901040",), ("99999",), id="ten-characters"),
        pytest.param(("CUSIP:999990104",), ("99999",), id="no-space"),
        pytest.param(("CUSIP: 999990104 (see note 1)",), ("99999", "note"), id="trailing-note"),
        pytest.param(("CUSIP: 99999010x",), ("99999",), id="lower-case-character"),
        pytest.param(
            ("CUSIP: 99999010x", "CUSIP: 999990203"), ("99999",), id="malformed-then-good"
        ),
        pytest.param(("cusip: 999990104",), ("99999",), id="lower-case-label"),
        pytest.param(("CUSIP\t999990104",), ("99999",), id="tab-separated"),
        pytest.param(("CUSIP: Synthetic Holder Two",), ("Synthetic", "Holder"), id="words"),
        pytest.param(("CUSIP: Total Funds Paid and Received",), ("Total Funds",), id="total-label"),
    ],
)
def test_a_malformed_activity_cusip_line_refuses(
    lines: tuple[str, ...], markers: tuple[str, ...], pending: bool
) -> None:
    text, _description = _under_last_row(*lines, pending=pending)
    _cusip_refusal(
        text,
        page=4 if pending else 3,
        markers=markers,
    )


@pytest.mark.parametrize("pending", [False, True], ids=["account-activity", "pending-trades"])
@pytest.mark.parametrize("where", ["own-cell", "above-first-row", "continuation-page"])
def test_a_malformed_cusip_line_refuses_naming_the_page_that_prints_it(
    where: str, pending: bool
) -> None:
    """In the dated row's Description cell, above the page's first dated row,
    or on a later page under a row that starts on the page before."""

    marked = "CUSIP: Synthetic Marker Q"
    page = _activity_page(pending=pending)
    heading, header, row, *rest = page.splitlines()
    number = 4 if pending else 3
    if where == "own-cell":
        assert row[: len(marked)].strip() in {"Purchase", "Pending Buy"}
        text = _with_pages(page, "\n".join((heading, header, marked + row[len(marked) :], *rest)))
    elif where == "above-first-row":
        text = _with_pages(page, "\n".join((heading, header, marked, row, *rest)))
    else:
        continued = "\n".join((f"{heading} (continued)", header, marked, *rest))
        text = _with_pages(page, "\n".join((heading, header, row)), continued)
        number += 1
    _cusip_refusal(text, page=number, markers=("Synthetic", "Marker"))


@pytest.mark.parametrize("pending", [False, True], ids=["account-activity", "pending-trades"])
@pytest.mark.parametrize(
    ("lines", "cusips"),
    [
        pytest.param((), (), id="no-cusip-line"),
        pytest.param(("CUSIPRO Synthetic Holdings",), (), id="longer-first-word"),
        pytest.param(("CUSIP: 999990104",), ("999990104",), id="one-line"),
        pytest.param(
            ("CUSIP: 999990104", "CUSIP: 999990203"),
            ("999990104", "999990203"),
            id="spillover",
        ),
    ],
)
def test_activity_rows_without_a_malformed_cusip_line_read_as_before(
    lines: tuple[str, ...], cusips: tuple[str, ...], pending: bool
) -> None:
    text, description = _under_last_row(*lines, pending=pending)
    row = parse_robinhood_statement(text).activities[2]
    assert (row.description, row.cusips) == ("\n".join((description, *lines)), cusips)


_OPTION_LINE = "OPT 1/17/2025 Call $10.00"
_OPTION_ROW = _position_page().splitlines()[4]
_SECOND_CONTRACT = "position description holds more than one option contract"
_MALFORMED_CONTRACT = "position option contract is malformed"
_OTHER_SYMBOL = "position option contract symbol is not the row's Sym/Cusip"


def _with_option(line: str, row: str = _OPTION_ROW) -> str:
    """The fixture with the option row's contract line and row replaced."""

    return _statement().replace(f"{_OPTION_LINE}\n{_OPTION_ROW}", f"{line}\n{row}")


def _wide_holdings(cell: str, symbol: str = "OPT") -> str:
    """The fixture with wider holdings columns and the option row's
    description in its own Description cell."""

    header, starts = _position_header("Securities Held in Account", shift=40)
    equity = ("Example Equity", "EQTY", "Margin", "1", "$100.0000", "$100.00", "$1.00", "2.50%")
    option = (cell, symbol, "Margin", "1", "$2.0000", "$200.00", "$0.00", "", "66.67%")
    page = "\n".join(
        (
            "Portfolio Summary",
            header,
            _fixed((0, *starts), (*equity, "33.33%")),
            _fixed((0, *starts), option),
            _TOTAL_ROW,
            *_portfolio_total_lines(RobinhoodStatementLayout.DEPOSIT_SWEEP),
        )
    )
    return _statement(position_pages=(page,))


@pytest.mark.parametrize(
    "text",
    [_statement(), _wide_holdings(_OPTION_LINE)],
    ids=["line-above", "own-cell"],
)
def test_one_contract_whose_symbol_is_its_cell_reads_as_an_option(text: str) -> None:
    row = parse_robinhood_statement(text).positions[1]
    assert (
        row.description,
        row.symbol_or_cusip,
        row.option_expiration,
        row.option_right,
        row.option_strike_usd,
    ) == (_OPTION_LINE, "OPT", date(2025, 1, 17), "Call", Decimal("10.00"))


def test_a_strike_with_a_thousands_comma_reads_as_before() -> None:
    row = parse_robinhood_statement(_with_option("OPT 1/17/2025 Call $1,000.00")).positions[1]
    assert (row.option_right, row.option_strike_usd) == ("Call", Decimal("1000.00"))


_PUT = "XQGD 1/17/2025 Put $20.00"
_PRICED_AS_EQUITY = _OPTION_ROW.replace("$2.0000", "$200.0000")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        *(
            pytest.param(build(contracts), _SECOND_CONTRACT, id=f"{where}-{order}")
            for where, build in (("line-above", _with_option), ("own-cell", _wide_holdings))
            for order, contracts in (
                ("call-put", f"{_OPTION_LINE} {_PUT}"),
                ("put-call", f"{_PUT} {_OPTION_LINE}"),
                ("agreeing", f"{_OPTION_LINE} {_OPTION_LINE}"),
            )
        ),
        pytest.param(
            _statement().replace(_TOTAL_ROW, f"Estimated Yield: {_PUT}\n{_TOTAL_ROW}"),
            _SECOND_CONTRACT,
            id="trailing-estimated-yield",
        ),
        pytest.param(
            _with_option(f"{_OPTION_LINE} XQGD 1/17/2025 Put 20.00"),
            _SECOND_CONTRACT,
            id="second-without-dollar",
        ),
        pytest.param(_with_option(f"{_OPTION_LINE}\n{_PUT}"), _OTHER_SYMBOL, id="two-lines-above"),
        pytest.param(
            _with_option(f"Estimated Yield: EQTY 1/17/2025 Call $10.00\n{_OPTION_LINE}"),
            "position option contract is attached to a row read as an equity",
            id="contract-under-equity",
        ),
        *(
            pytest.param(_with_option(line), _MALFORMED_CONTRACT, id=case)
            for case, line in (
                ("three-decimals", "OPT 1/17/2025 Call $10.001"),
                ("misgrouped-comma", "OPT 1/17/2025 Call $1,0.00"),
                ("no-digits", "OPT 1/17/2025 Call $,.00"),
                ("symbol-inside-a-word", "fooOPT 1/17/2025 Call $10.00"),
            )
        ),
        *(
            pytest.param(_with_option(line, _PRICED_AS_EQUITY), _MALFORMED_CONTRACT, id=case)
            for case, line in (
                ("strike-without-dollar", "OPT 1/17/2025 Call 10.00"),
                ("two-digit-year", "OPT 1/17/25 Call $10.00"),
                ("lower-case-right", "OPT 1/17/2025 call $10.00"),
                ("no-strike", "OPT 1/17/2025 Call"),
                ("no-expiration", "OPT Call $10.00"),
            )
        ),
        pytest.param(
            _with_option("OPT 2/30/2025 Call $10.00"),
            "date is not a valid calendar date",
            id="invalid-calendar-date",
        ),
        pytest.param(_with_option("XQGD 1/17/2025 Call $10.00"), _OTHER_SYMBOL, id="other-symbol"),
        pytest.param(
            _with_option(_OPTION_LINE, _OPTION_ROW.replace("OPT", "XQGD")),
            _OTHER_SYMBOL,
            id="other-cell",
        ),
    ],
)
def test_a_second_malformed_or_misattributed_contract_refuses(text: str, message: str) -> None:
    _identity_refusal(text, message, page=2)


def test_malformed_contracts_priced_as_equities_parse_on_their_own() -> None:
    """The equity-priced row parses as an equity without a contract line, so
    the refusals above come from the contract, not the arithmetic."""

    row = parse_robinhood_statement(_with_option("Plain Note", _PRICED_AS_EQUITY)).positions[1]
    assert (row.option_right, row.market_value_usd) == (None, Decimal("200.00"))


_ACTIVITY_MARKERS = ("XQZZ", "11/22/2033", "$987.65", "XQZY")
_ACTIVITY_SECOND = "activity description holds more than one option contract"
_ACTIVITY_OTHER_SYMBOL = "activity option contract symbol is not the row's Symbol"
_ACTIVITY_NO_CONTRACT = "option activity row has no option contract"
_ACTIVITY_NOT_OPTION = "activity row holds an option contract but is not an option trade"
_SECTIONS = pytest.mark.parametrize("pending", [False, True], ids=["activity", "pending"])


def _option_activity_refusal(text: str, message: str, *, pending: bool) -> None:
    """The refusal names the page and Description, and no symbol, expiry or strike."""

    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        message,
        "Description",
        4 if pending else 3,
        "",
    )
    rendered = str(error)
    for marker in _ACTIVITY_MARKERS:
        assert marker not in rendered


@_SECTIONS
@pytest.mark.parametrize("code", ["BTO", "STC"])
def test_an_option_trade_row_reads_its_one_contract(code: str, pending: bool) -> None:
    statement = parse_robinhood_statement(
        _option_activity(_ACTIVITY_CONTRACT, code=code, pending=pending)
    )
    row = next(row for row in statement.activities if row.transaction_code == code)
    section = (
        RobinhoodStatementSection.PENDING_TRADES
        if pending
        else RobinhoodStatementSection.ACCOUNT_ACTIVITY
    )
    assert (
        row.location.section,
        row.description,
        row.symbol,
        row.option_symbol,
        row.option_expiration,
        row.option_right,
        row.option_strike_usd,
    ) == (
        section,
        _ACTIVITY_CONTRACT,
        None if pending else "XQZZ",
        "XQZZ",
        date(2033, 11, 22),
        "Call",
        Decimal("987.65"),
    )
    assert all(other.option_symbol is None for other in statement.activities if other is not row)


@_SECTIONS
@pytest.mark.parametrize(
    ("description", "below", "code", "message"),
    [
        pytest.param(
            _ACTIVITY_CONTRACT,
            ("XQZZ 11/22/2033 Put $987.65",),
            "BTO",
            _ACTIVITY_SECOND,
            id="second-contract",
        ),
        pytest.param(
            _ACTIVITY_CONTRACT,
            (_ACTIVITY_CONTRACT,),
            "STC",
            _ACTIVITY_SECOND,
            id="agreeing-second-contract",
        ),
        pytest.param(
            "XQZZ 11/22/2033 Call 987.65",
            (),
            "BTO",
            _ACTIVITY_MALFORMED,
            id="strike-without-dollar",
        ),
        pytest.param(
            "XQZZ 11/22/2033 call $987.65", (), "STC", _ACTIVITY_MALFORMED, id="lower-case-right"
        ),
        pytest.param(
            "XQZZ 2/30/2033 Call $987.65",
            (),
            "BTO",
            _ACTIVITY_MALFORMED,
            id="invalid-calendar-date",
        ),
        pytest.param("", (), "BTO", _ACTIVITY_NO_CONTRACT, id="bto-empty-description"),
        pytest.param("", (), "STC", _ACTIVITY_NO_CONTRACT, id="stc-empty-description"),
        pytest.param(
            "SAMPLE ISSUER OPTION MARKER", (), "BTO", _ACTIVITY_NO_CONTRACT, id="bto-no-contract"
        ),
        pytest.param(
            "SAMPLE ISSUER OPTION MARKER", (), "STC", _ACTIVITY_NO_CONTRACT, id="stc-no-contract"
        ),
        pytest.param(_ACTIVITY_CONTRACT, (), "Buy", _ACTIVITY_NOT_OPTION, id="buy-with-contract"),
    ],
)
def test_an_option_activity_contract_must_be_one_whole_contract_on_an_option_trade(
    description: str, below: tuple[str, ...], code: str, message: str, pending: bool
) -> None:
    text = _option_activity(description, *below, code=code, pending=pending)
    _option_activity_refusal(text, message, pending=pending)


@pytest.mark.parametrize("symbol", ["XQZY", ""], ids=["other-symbol", "blank-symbol"])
def test_an_account_activity_contract_symbol_must_be_the_rows_symbol(symbol: str) -> None:
    text = _option_activity(_ACTIVITY_CONTRACT, symbol=symbol)
    _option_activity_refusal(text, _ACTIVITY_OTHER_SYMBOL, pending=False)


@_SECTIONS
def test_a_fund_named_with_the_word_call_is_not_an_option_contract(pending: bool) -> None:
    statement = parse_robinhood_statement(
        _option_activity("SAMPLE ISSUER COVERED CALL FUND", code="Buy", pending=pending)
    )
    row = statement.activities[3 if pending else 2]
    assert (row.description, row.transaction_code, row.option_symbol) == (
        "SAMPLE ISSUER COVERED CALL FUND",
        "Buy",
        None,
    )


_REPEATED_LABEL = "table column label is repeated"
_HEADER_TEXT = "table column header holds unsupported text"
_HOLDINGS_HEADER = _position_header("Securities Held in Account")[0]
# Each caller's page, the heading above its header, the header, and its last label.
_HEADERS = {
    "holdings": (2, "Portfolio Summary", _HOLDINGS_HEADER, "% of Total Portfolio"),
    "activity": (3, "Account Activity", _activity_header()[0], "Credit"),
    "pending": (
        4,
        "Executed Trades Pending Settlement",
        _activity_header(pending=True)[0],
        "Credit",
    ),
    "sweep": (6, "Deposit Sweep Activity", _LEDGER_HEADER, "Balance"),
    "loaned": (7, *_stock_loaned_page().splitlines()[:2], "MKT Value"),
    "loan": (8, *_stock_loan_activity_page().splitlines()[:2], "Shares Returned To Account"),
    "collateral": (9, "Stock Lending - Collateral Activity", _LEDGER_HEADER, "Balance"),
}


def _with_header(caller: str, header: str) -> str:
    """The fixture with the caller's column header replaced by ``header``."""

    _page, heading, printed, _label = _HEADERS[caller]
    assert _statement().count(f"{heading}\n{printed}") == 1
    return _statement().replace(f"{heading}\n{printed}", f"{heading}\n{header}")


@pytest.mark.parametrize("extra", ["repeated-label", "notes"])
@pytest.mark.parametrize("caller", _HEADERS)
def test_a_header_with_a_repeated_label_or_other_text_refuses(caller: str, extra: str) -> None:
    page, _heading, header, label = _HEADERS[caller]
    if extra == "repeated-label":
        text = _with_header(caller, f"{header}   {label}")
        _identity_refusal(text, _REPEATED_LABEL, page=page, column=label)
    else:
        text = _with_header(caller, f"{header}   Notes")
        _identity_refusal(text, _HEADER_TEXT, page=page, column=None)


_LEDGER_WITH = (0, 47, 63, 78, 86, 93)  # a sixth column between Credit and Balance


@pytest.mark.parametrize(
    ("caller", "header", "message", "column"),
    [
        pytest.param(
            "sweep",
            _fixed(_LEDGER_WITH, ("Description", "Date", "Debit", "Credit", "Debit", "Balance")),
            _REPEATED_LABEL,
            "Debit",
            id="second-debit-between-credit-and-balance",
        ),
        pytest.param(
            "sweep",
            _fixed(_LEDGER_WITH, ("Description", "Date", "Debit", "Credit", "Notes", "Balance")),
            _HEADER_TEXT,
            None,
            id="notes-between-credit-and-balance",
        ),
        pytest.param(
            "sweep",
            f"{_LEDGER_HEADER}   111111111",
            _HEADER_TEXT,
            None,
            id="account-number-after-the-ledger-labels",
        ),
        pytest.param(
            "sweep",
            f"Notes  {_LEDGER_HEADER}",
            _HEADER_TEXT,
            None,
            id="text-before-the-ledger-labels",
        ),
        pytest.param(
            "sweep",
            _fixed((0, 47, 63, 68, 93), LEDGER_LABELS),
            _HEADER_TEXT,
            None,
            id="labels-with-no-blank-between",
        ),
        pytest.param(
            "sweep",
            _fixed(
                (0, 47, 63, 73, 78, 93),
                ("Description", "Date", "Debit", "Debit", "Credit", "Balance"),
            ),
            _HEADER_TEXT,
            None,
            id="extra-debit-attached-to-credit",
        ),
        pytest.param(
            "sweep",
            _fixed(
                (0, 47, 63, 78, 87, 93),
                ("Description", "Date", "Debit", "Credit", "Credit", "Balance"),
            ),
            _HEADER_TEXT,
            None,
            id="extra-credit-attached-to-balance",
        ),
        pytest.param(
            "holdings",
            _HOLDINGS_HEADER.replace(f"Account{' ' * 8}Sym", "Account  Extra Sym"),
            _HEADER_TEXT,
            None,
            id="extra-between-holdings-heading-and-labels",
        ),
    ],
)
def test_text_between_or_around_the_labels_refuses(
    caller: str, header: str, message: str, column: str | None
) -> None:
    assert header != _HEADERS[caller][2]
    _identity_refusal(
        _with_header(caller, header), message, page=_HEADERS[caller][0], column=column
    )


@pytest.mark.parametrize("caller", ["sweep", "collateral"])
def test_an_empty_ledger_continuation_after_its_total_reads_as_before(caller: str) -> None:
    expected = parse_robinhood_statement(_statement())
    statement = parse_robinhood_statement(_statement(extra_pages=(_TABLES[caller].continued,)))
    assert statement.sweep_activity == expected.sweep_activity
    assert statement.stock_collateral_activity == expected.stock_collateral_activity
    assert statement.totals == expected.totals


@pytest.mark.parametrize("extra", ["repeated-label", "notes"])
@pytest.mark.parametrize("caller", ["sweep", "collateral"])
def test_a_malformed_ledger_header_after_its_total_refuses(caller: str, extra: str) -> None:
    header = _TABLES[caller].continued
    if extra == "repeated-label":
        text = _statement(extra_pages=(f"{header}   Balance",))
        _identity_refusal(text, _REPEATED_LABEL, page=10, column="Balance")
    else:
        text = _statement(extra_pages=(f"{header}   Notes",))
        _identity_refusal(text, _HEADER_TEXT, page=10, column=None)


# ---- #1019: validate covered table layout -------------------------------

_HELD_STARTS = _position_header("Securities Held in Account")[1]
_HIDDEN_HOLDING = _fixed(
    (0, *_HELD_STARTS),
    ("Hidden Holding", "HIDE", "Margin", "1", "$5.0000", "$5.00", "$0.00", "", "1.00%"),
)
_LEDGER_MOVEMENT = _fixed(_LEDGER_STARTS, ("Movement", "1/20/2025", "$99.00", "", "$99.00"))
_NO_TABLE = {
    "holdings": "holdings table holds a heading line that starts no table",
    "loan": "Stock Lending loan-activity table holds a heading line that starts no table",
}


class _Heading(NamedTuple):
    page: str  # the fixture page that prints the table, its heading first
    number: int  # that page's number in _statement()
    hidden: str  # a line shaped like one of the table's rows


_HEADINGS = {
    "Portfolio Summary": _Heading(_position_page(), 2, _HIDDEN_HOLDING),
    "Account Activity": _Heading(
        _activity_page(),
        3,
        _fixed(
            _activity_header()[2],
            ("Hidden Buy", "HIDE", "Margin", "Buy", "1/12/2025", "1", "$99.0000", "$99.00", ""),
        ),
    ),
    "Executed Trades Pending Settlement": _Heading(
        _activity_page(pending=True),
        4,
        _fixed(
            _activity_header(pending=True)[2],
            ("Hidden Buy", "Margin", "Buy", "1/30/2025", "2/3/2025", "1", "$99.0000", "$99.00", ""),
        ),
    ),
    "Deposit Sweep Program Banks": _Heading(
        _sweep_bank_page(), 5, _fixed((0, 60), ("Hidden Program Bank", "$99.00"))
    ),
    "Deposit Sweep Activity": _Heading(_ledger("sweep"), 6, _LEDGER_MOVEMENT),
    "Stock Lending - Loaned Securities": _Heading(
        _stock_loaned_page(), 7, _loaned_row("HIDE", "$99.0000", "$99.00", "Hidden Loan")
    ),
    "Stock Lending - Loan Activity": _Heading(
        _stock_loan_activity_page(), 8, _loan_row("Hidden Loan", lent="5")
    ),
    "Stock Lending - Collateral Activity": _Heading(_ledger("collateral"), 9, _LEDGER_MOVEMENT),
}


def _rows(statement: RobinhoodStatement) -> list[dict[str, object]]:
    """Every covered table row and total, without its location."""

    return [
        {field.name: getattr(row, field.name) for field in fields(row) if field.name != "location"}
        for name in (
            "positions",
            "activities",
            "sweep_banks",
            "sweep_activity",
            "stock_loaned_securities",
            "stock_loan_activity",
            "stock_collateral_activity",
            "totals",
        )
        for row in getattr(statement, name)
    ]


_BASE_ROWS = _rows(parse_robinhood_statement(_statement()))


def _with_table(
    heading: str, *, above: tuple[str, ...] = (), line: str | None = None, gap: tuple[str, ...] = ()
) -> str:
    """The fixture with ``heading``'s page changed: ``above`` its heading line,
    that line replaced by ``line``, and ``gap`` between it and the header."""

    table = _HEADINGS[heading]
    first, header, *rest = table.page.splitlines()
    assert first == heading
    changed = (*above, heading if line is None else line, *gap, header, *rest)
    return _with_pages(table.page, "\n".join(changed))


def _unread_refusal(text: str, message: str, page: int) -> None:
    """The refusal names the page and the table, and none of the unread text."""

    error = _refusal(text)
    assert (error.message, error.row_index, error.raw_value) == (message, page, "")
    rendered = str(error)
    assert f"row {page}" in rendered
    assert "Unexpected" not in rendered
    assert "Hidden" not in rendered
    assert re.search(r"\$|[0-9][./][0-9]", rendered) is None


def _move_marker(text: str, number: int, above: str) -> str:
    """Move page ``number``'s marker line to just above its line ``above``."""

    pages = text.split("\f")
    marker = f"Page {number} of {len(pages)}"
    lines = [line for line in pages[number - 1].splitlines() if line != marker]
    lines.insert(lines.index(above), marker)
    pages[number - 1] = "\n".join(lines) + "\n"
    return "\f".join(pages)


def _after_marker(text: str, number: int, *lines: str) -> str:
    """Print ``lines`` below page ``number``'s marker, at the end of that page."""

    marker = f"Page {number} of {len(text.split(chr(12)))}\n"
    assert text.count(marker) == 1
    return text.replace(marker, marker + "".join(f"{line}\n" for line in lines))


@pytest.mark.parametrize("heading", _HEADINGS)
def test_a_whole_heading_reads_as_before(heading: str) -> None:
    line = f"{heading} (continued)"
    assert _rows(parse_robinhood_statement(_with_table(heading, line=line))) == _BASE_ROWS


_HEADING_REPEATED = {
    "Stock Lending - Loaned Securities": "Stock Lending securities table is repeated on the page",
    "Stock Lending - Loan Activity": "Stock Lending loan-activity heading is repeated",
}


@pytest.mark.parametrize("change", ["text", "continued-text", "twice"])
@pytest.mark.parametrize("heading", _HEADINGS)
def test_a_heading_line_holds_only_its_heading_once(heading: str, change: str) -> None:
    line = {
        "text": f"{heading} Unexpected text",
        "continued-text": f"{heading} (continued) Unexpected text",
        "twice": f"{heading}\n{heading}",
    }[change]
    message = (
        _HEADING_REPEATED.get(heading, f"{heading} heading is repeated on the page")
        if change == "twice"
        else f"{heading} heading line carries other text"
    )
    _unread_refusal(_with_table(heading, line=line), message, _HEADINGS[heading].number)


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize("gap", ["text", "row"])
@pytest.mark.parametrize("heading", _HEADINGS)
def test_only_blanks_and_the_marker_come_between_a_heading_and_its_header(
    heading: str, gap: str, continued: bool
) -> None:
    table = _HEADINGS[heading]
    line = f"{heading} (continued)" if continued else heading
    marker = f"Page {table.number} of 10"
    header = table.page.splitlines()[1]
    spaced = _move_marker(_with_table(heading, line=line, gap=("", "")), table.number, header)
    assert _rows(parse_robinhood_statement(spaced)) == _BASE_ROWS
    assert f"{line}\n\n\n{marker}\n{header}" in spaced

    unread = "Unexpected text" if gap == "text" else table.hidden
    _unread_refusal(
        _with_table(heading, line=line, gap=(unread,)),
        f"{heading} heading is not followed by its column header",
        table.number,
    )


@pytest.mark.parametrize("above", ["text", "row"])
@pytest.mark.parametrize("heading", _HEADINGS)
def test_nothing_unread_comes_above_a_tables_heading(heading: str, above: str) -> None:
    table = _HEADINGS[heading]
    line = "Unexpected text" if above == "text" else table.hidden
    _unread_refusal(
        _with_table(heading, above=(line,)),
        f"{heading} heading is preceded by an unsupported line",
        table.number,
    )


@pytest.mark.parametrize(
    "heading",
    [
        "Portfolio Summary",
        "Account Activity",
        "Executed Trades Pending Settlement",
        "Deposit Sweep Program Banks",
    ],
)
def test_a_header_above_its_heading_refuses(heading: str) -> None:
    table = _HEADINGS[heading]
    first, header, *rest = table.page.splitlines()
    _unread_refusal(
        _with_pages(table.page, "\n".join((header, first, *rest))),
        f"{heading} heading is not followed by its column header",
        table.number,
    )


@pytest.mark.parametrize("heading", ["", "Portfolio Summary"], ids=["headingless", "heading"])
@pytest.mark.parametrize("unread", ["Unexpected text", _HIDDEN_HOLDING], ids=["text", "row"])
def test_nothing_unread_comes_above_a_headingless_holdings_header(
    unread: str, heading: str
) -> None:
    pages = _spread_position_pages()
    continued = pages[1]
    above = "\n".join(line for line in (unread, heading) if line)
    text = _statement(position_pages=(pages[0], f"{above}\n{continued}", pages[2]))
    subject = f"{heading} heading" if heading else "holdings header"
    _unread_refusal(text, f"{subject} is preceded by an unsupported line", 3)


@pytest.mark.parametrize("where", ["before-the-marker", "after-the-marker"])
@pytest.mark.parametrize("unread", ["Unexpected text", _HIDDEN_HOLDING], ids=["text", "row"])
@pytest.mark.parametrize("prefix", ["Deposit Sweep", "Stock Lending -"])
@pytest.mark.parametrize("page", [2, 3])
def test_holdings_rows_end_only_where_another_table_starts(
    page: int, prefix: str, unread: str, where: str
) -> None:
    pages = list(_spread_position_pages())
    if where == "before-the-marker":
        pages[page - 2] = f"{pages[page - 2]}\n{prefix}\n{unread}"
        text = _statement(position_pages=tuple(pages))
    else:
        text = _after_marker(_statement(position_pages=tuple(pages)), page, prefix, unread)
    _unread_refusal(text, _NO_TABLE["holdings"], page)


def test_holdings_rows_end_where_another_table_starts() -> None:
    """A held page's rows end at another table's heading; that table's reader
    reads what follows."""

    pages = list(_spread_position_pages())
    activity = _activity_page()
    pages[1] = f"{pages[1]}\n\n{activity}"
    text = _with_pages(activity, text=_statement(position_pages=tuple(pages)))
    statement = parse_robinhood_statement(text)
    spread = parse_robinhood_statement(_statement(position_pages=_spread_position_pages()))
    assert _rows(statement) == _rows(spread)
    assert {
        row.location.page
        for row in statement.activities
        if row.location.section is (RobinhoodStatementSection.ACCOUNT_ACTIVITY)
    } == {3}


@pytest.mark.parametrize("where", ["before-the-marker", "after-the-marker"])
@pytest.mark.parametrize("unread", ["Unexpected text", _HIDDEN_HOLDING], ids=["text", "row"])
def test_nothing_but_another_table_follows_the_holdings_total(unread: str, where: str) -> None:
    page = _position_page()
    if where == "before-the-marker":
        text = _with_pages(page, f"{page}\n{unread}")
    else:
        text = _after_marker(_statement(), 2, unread)
    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "holdings total is followed by an unsupported line",
        "Total Priced Portfolio",
        2,
        "",
    )
    assert "Unexpected" not in str(error) and "Hidden" not in str(error)


_PORTFOLIO_NOTE = (
    "* “Total Securities” includes those securities held in your account as well as those "
    "on loan through Stock Lending (identified herein as “Loaned Securities” or “Loaned "
    "Stocks”). The provisions of the Securities Investor Protection Act of 1970 may not "
    "protect you with respect",
    "to Loaned Securities.",
)


def test_the_holdings_total_may_end_its_page_with_its_footnote() -> None:
    text = _ending_page(_position_page(), "", *_PORTFOLIO_NOTE, "")
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


@pytest.mark.parametrize(
    "change",
    [
        "text-after",
        "twice",
        "altered",
        "truncated",
        "first-line-only",
        "split",
        "joined",
        "other-tables",
        "before-the-marker",
    ],
)
def test_only_the_exact_holdings_footnote_may_end_its_page(change: str) -> None:
    page = _position_page()
    first, second = _PORTFOLIO_NOTE
    text = {
        "text-after": _ending_page(page, "", first, second, "Unexpected text"),
        "twice": _ending_page(page, "", first, second, first, second),
        "altered": _ending_page(page, "", first, f"{second} restated"),
        "truncated": _ending_page(page, "", first, second[:-1]),
        "first-line-only": _ending_page(page, "", first),
        "split": _ending_page(page, "", first, "", second),
        "joined": _ending_page(page, "", f"{first} {second}"),
        "other-tables": _ending_page(page, "", _REMINDERS["loaned"]),
        "before-the-marker": _with_pages(page, f"{page}\n\n{first}\n{second}"),
    }[change]
    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        "holdings total is followed by an unsupported line",
        "Total Priced Portfolio",
        2,
        "",
    )
    assert "Unexpected" not in str(error)


@pytest.mark.parametrize("below", ["Account Activity", "Deposit Sweep Activity"])
def test_another_table_may_start_below_the_holdings_total(below: str) -> None:
    lower = _HEADINGS[below].page
    holdings = _position_page()
    text = _with_pages(lower, text=_with_pages(holdings, f"{holdings}\n\n{lower}"))
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


@pytest.mark.parametrize("upper", ["Deposit Sweep Activity", "Stock Lending - Loaned Securities"])
def test_a_headingless_holdings_header_may_start_below_a_finished_table(upper: str) -> None:
    upper_page = _HEADINGS[upper].page
    body = _position_page().removeprefix("Portfolio Summary\n")
    text = _with_pages(_position_page(), text=_with_pages(upper_page, f"{upper_page}\n\n{body}"))
    statement = parse_robinhood_statement(text)
    assert [row.symbol_or_cusip for row in statement.positions] == ["EQTY", "OPT"]
    assert _rows(statement) == _BASE_ROWS


# Loan Activity reads to a real boundary.

_LOAN_PAGE = _stock_loan_activity_page()
_LOAN_HEADER, _LOAN_FIXTURE_ROW = _LOAN_PAGE.splitlines()[1:]


def test_the_page_marker_does_not_end_the_loan_rows() -> None:
    moved = _move_marker(_statement(), 8, _LOAN_FIXTURE_ROW)
    assert f"{_LOAN_HEADER}\nPage 8 of 10\n{_LOAN_FIXTURE_ROW}" in moved
    assert _rows(parse_robinhood_statement(moved)) == _BASE_ROWS

    second = parse_robinhood_statement(
        _after_marker(_statement(), 8, _loan_row("Hidden", lent="5"))
    )
    assert [(row.description, row.shares) for row in second.stock_loan_activity] == [
        ("Loan", Decimal("1")),
        ("Hidden", Decimal("5")),
    ]

    error = _refusal(_after_marker(_statement(), 8, "Unexpected text"))
    assert (error.message, error.row_index) == (
        "Stock Lending loan description is not attached",
        8,
    )
    assert "Unexpected" not in str(error)


@pytest.mark.parametrize("where", ["before-the-marker", "after-the-marker"])
@pytest.mark.parametrize("label", ["Securities Held in Account", "Loaned Securities"])
def test_a_heading_line_that_starts_no_table_cannot_end_the_loan_rows(
    label: str, where: str
) -> None:
    lines = (label, "Unexpected text", _loan_row("Hidden Loan", lent="5"))
    if where == "before-the-marker":
        text = _with_pages(_LOAN_PAGE, "\n".join((_LOAN_PAGE, *lines)))
    else:
        text = _after_marker(_statement(), 8, *lines)
    _unread_refusal(text, _NO_TABLE["loan"], 8)


def test_a_bare_holdings_heading_cannot_end_the_loan_rows() -> None:
    _unread_refusal(
        _with_pages(_LOAN_PAGE, f"{_LOAN_PAGE}\nPortfolio Summary\nUnexpected text"),
        "Portfolio Summary heading is not followed by its column header",
        8,
    )


def test_a_collateral_ledger_may_start_below_the_loan_rows() -> None:
    collateral = _ledger("collateral")
    text = _with_pages(collateral, text=_with_pages(_LOAN_PAGE, f"{_LOAN_PAGE}\n\n{collateral}"))
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


# Program Banks: bank rows, then one disclosure paragraph accepted by its shape.

_BANK_HEADER = "Bank                                                        Balance"
_PROSE = (
    "Synthetic disclosure 2025: deposits at each program bank earned a yield of 0.01% APY "
    "this month.",
    "Each bank insures $250,000.00 per depositor, and 3 program banks held the balances above.",
    "synthetic closing prose with no amount in it at all, wrapping onto a third line of text.",
)
_HIDDEN_BANK = _fixed((0, 60), ("Hidden Program Bank", "$99.00"))
# Prose whose text at the Balance column opens with an amount after one blank,
# and the same amount after a column gap, as a hidden row would print it.
_RUN_ON = "Synthetic prose runs on past the Balance column at a fee of 12.50 more."
_GAPPED = f"{'Synthetic prose with an amount after a column gap'.ljust(58)}  12.50 more."
_BROKEN_BANK = _fixed((0, 60), ("hidden Program Bank", "$99.00 BROKEN"))


def _bank_lines(*lines: str) -> str:
    """The fixture with these lines printed under the Program Banks header."""

    page = _sweep_bank_page()
    return _with_pages(page, "\n".join(("Deposit Sweep Program Banks", _BANK_HEADER, *lines)))


@pytest.mark.parametrize(
    "lines",
    [
        (_BANK_ROW, "", *_PROSE),
        (_BANK_ROW, *_PROSE[1:2], "", ""),
        (_BANK_ROW, "", f"   {_PROSE[0]}", _PROSE[2]),
        ("Example Program", _fixed((0, 60), ("Bank", "$20.00")), "", *_PROSE),
        (_BANK_ROW, "", _RUN_ON, *_PROSE),
        (_BANK_ROW, "", *_PROSE, _RUN_ON),
    ],
    ids=[
        "paragraph",
        "one-line-then-blanks",
        "indented",
        "after-a-wrapped-name",
        "opening-with-a-run-on-amount",
        "run-on-amount-inside",
    ],
)
def test_a_disclosure_paragraph_may_follow_the_bank_rows(lines: tuple[str, ...]) -> None:
    """Prose after the last bank row, digits and all, is not read."""

    statement = parse_robinhood_statement(_bank_lines(*lines))
    name = "Example Program\nBank" if lines[0] == "Example Program" else "Example Program Bank"
    assert [(bank.bank_name, bank.balance_usd) for bank in statement.sweep_banks] == [
        (name, Decimal("20.00"))
    ]


def test_a_page_without_bank_rows_may_print_only_the_disclosure() -> None:
    """With no bank rows on a page, the paragraph may open right under its header.
    When the statement has no bank rows, the Deposit Sweep balance must be zero."""

    page = "\n".join(("Deposit Sweep Program Banks", _BANK_HEADER, "", *_PROSE))
    legacy = RobinhoodStatementLayout.LEGACY_NET_ACCOUNT
    statement = parse_robinhood_statement(_statement(layout=legacy, extra_pages=(page,)))
    assert statement.sweep_banks == ()
    assert _rows(statement) == _rows(parse_robinhood_statement(_statement(layout=legacy)))

    error = _refusal(_with_pages(_sweep_bank_page(), page))
    assert (error.message, error.row_index) == (
        "non-zero Deposit Sweep balance has no Program Bank rows",
        0,
    )


def test_a_table_may_start_below_the_disclosure_paragraph() -> None:
    sweep = _ledger("sweep")
    banks = "\n".join(("Deposit Sweep Program Banks", _BANK_HEADER, _BANK_ROW, "", *_PROSE))
    text = _with_pages(sweep, text=_with_pages(_sweep_bank_page(), f"{banks}\n\n{sweep}"))
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


def test_a_page_marker_above_a_bank_row_is_not_its_name() -> None:
    text = _move_marker(_statement(), 5, _BANK_ROW)
    assert f"Page 5 of 10\n{_BANK_ROW}" in text
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


_IN_PARAGRAPH = "Deposit Sweep Program Banks disclosure holds a bank row"
_AFTER_PARAGRAPH = "Deposit Sweep Program Banks disclosure is followed by an unsupported line"


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        pytest.param(
            ("unexpected text", _BANK_ROW),
            "Program Bank name line is not attached to a balance row",
            id="text-under-the-header",
        ),
        pytest.param(
            (_BROKEN_BANK, _BANK_ROW),
            "Program Bank row is incomplete or column-shifted",
            id="broken-row-under-the-header",
        ),
        pytest.param(
            ("As of synthetic", "", _BANK_ROW),
            "Program Bank name line is not attached to a balance row",
            id="as-of-line",
        ),
        pytest.param(
            (*_PROSE, "", _BANK_ROW),
            "Program Bank row is incomplete or column-shifted",
            id="prose-before-the-first-row",
        ),
        pytest.param(
            (_BANK_ROW, "", _PROSE[0], _HIDDEN_BANK, *_PROSE[1:]),
            _IN_PARAGRAPH,
            id="hidden-row-inside",
        ),
        pytest.param(
            (_BANK_ROW, "", _PROSE[0], _BROKEN_BANK, *_PROSE[1:]),
            _IN_PARAGRAPH,
            id="broken-row-inside",
        ),
        pytest.param(
            (_BANK_ROW, "", *_PROSE, _HIDDEN_BANK),
            _IN_PARAGRAPH,
            id="hidden-row-ending-it",
        ),
        pytest.param(
            (_BANK_ROW, "", _PROSE[0], _GAPPED, *_PROSE[1:]),
            _IN_PARAGRAPH,
            id="gap-aligned-amount-inside",
        ),
        pytest.param(
            (_BANK_ROW, "", _GAPPED, *_PROSE),
            "Program Bank row is incomplete or column-shifted",
            id="gap-aligned-amount-opening-it",
        ),
        pytest.param(
            (_RUN_ON.removesuffix(" more."), _BANK_ROW),
            "Program Bank row is incomplete or column-shifted",
            id="row-amount-without-a-gap",
        ),
        pytest.param(
            (_PROSE[0], _GAPPED, *_PROSE[1:]),
            _IN_PARAGRAPH,
            id="no-rows-gap-aligned-amount-inside",
        ),
        pytest.param(
            (*_PROSE, "", "Unexpected text"),
            _AFTER_PARAGRAPH,
            id="no-rows-text-after",
        ),
        pytest.param(
            (_BANK_ROW, "", *_PROSE, "", _HIDDEN_BANK),
            _AFTER_PARAGRAPH,
            id="hidden-row-after",
        ),
        pytest.param(
            (_BANK_ROW, "", *_PROSE, "", _BROKEN_BANK),
            _AFTER_PARAGRAPH,
            id="broken-row-after",
        ),
        pytest.param(
            (_BANK_ROW, "", *_PROSE, "", "Unexpected text"),
            _AFTER_PARAGRAPH,
            id="second-paragraph",
        ),
        pytest.param(
            (_BANK_ROW, "", _PROSE[0], "", "Unexpected text"),
            _AFTER_PARAGRAPH,
            id="text-after-a-one-line-paragraph",
        ),
    ],
)
def test_program_banks_refuse_any_other_line(lines: tuple[str, ...], message: str) -> None:
    _unread_refusal(_bank_lines(*lines), message, 5)


_SHIFTED = "Program Bank row is incomplete or column-shifted"


def _zero_sweep(text: str) -> str:
    """``text`` with its closing Deposit Sweep balance held as cash instead:
    $0.00 swept and $25.00 brokerage cash."""

    for old, new in (
        ("Brokerage Cash Balance $5.00 $5.00", "Brokerage Cash Balance $5.00 $25.00"),
        ("Deposit Sweep Balance $5.00 $20.00", "Deposit Sweep Balance $5.00 $0.00"),
        ("Brokerage Cash Balance $5.00 1.54%", "Brokerage Cash Balance $25.00 7.69%"),
        ("Deposit Sweep Balance $20.00 6.15%", "Deposit Sweep Balance $0.00 0.00%"),
    ):
        assert text.count(old) == 1
        text = text.replace(old, new)
    return text


def test_the_zero_sweep_statement_reads_a_zero_bank_row() -> None:
    statement = parse_robinhood_statement(
        _zero_sweep(_bank_lines(_BANK_ROW.replace("$20.00", " $0.00")))
    )
    assert [(bank.bank_name, bank.balance_usd) for bank in statement.sweep_banks] == [
        ("Example Program Bank", Decimal("0.00"))
    ]


@pytest.mark.parametrize(
    ("text", "message", "page"),
    [
        pytest.param(_zero_sweep(_bank_lines(_NOT_MONEY)), _SHIFTED, 5, id="only-line"),
        pytest.param(
            _statement(
                layout=RobinhoodStatementLayout.LEGACY_NET_ACCOUNT,
                extra_pages=("\n".join(("Deposit Sweep Program Banks", _BANK_HEADER, _NOT_MONEY)),),
            ),
            _SHIFTED,
            8,
            id="only-line-legacy",
        ),
        pytest.param(_bank_lines(_BANK_ROW, _NOT_MONEY), _SHIFTED, 5, id="below-the-row"),
        pytest.param(
            _bank_lines(_BANK_ROW, "", _PROSE[0], _NOT_MONEY, *_PROSE[1:]),
            _IN_PARAGRAPH,
            5,
            id="inside-the-paragraph",
        ),
        pytest.param(
            _bank_lines(_BANK_ROW, "", *_PROSE, _NOT_MONEY),
            _IN_PARAGRAPH,
            5,
            id="ending-the-paragraph",
        ),
    ],
)
def test_a_balance_cell_token_after_a_column_gap_refuses(
    text: str, message: str, page: int
) -> None:
    """Whatever token follows a column gap in the Balance cell, a line that is
    not a bank row refuses, with or without bank rows on its page."""

    _unread_refusal(text, message, page)
    assert "NOT-MONEY" not in str(_refusal(text))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_bank_lines(_BANK_ROW.replace("$20.00", " 20.00")), id="20.00"),
        pytest.param(
            _zero_sweep(_bank_lines(_BANK_ROW.replace("$20.00", "  0.00"))), id="0.00-zero-sweep"
        ),
    ],
)
def test_a_bank_balance_without_a_dollar_sign_refuses(text: str) -> None:
    _unread_refusal(text, "Program Bank balance has no dollar sign", 5)
    assert _refusal(text).column == "Balance"


# The intro sentence each table may print between its heading and its header.


# Each approved sentence's fixed wording, split where its date slot goes.
_APPROVED = {
    "Executed Trades Pending Settlement": (
        "These transactions may not be reflected in the other summaries",
    ),
    "Deposit Sweep Activity": (
        "These transactions reflect movements of uninvested cash to and from program banks.",
    ),
    "Stock Lending - Loan Activity": (
        "These transactions are specific to Stock Lending and represent all loan activity "
        "this month.",
    ),
    "Stock Lending - Collateral Activity": (
        "These transactions reflect daily movements of collateral to and from Stock Lending "
        "custodian banks.",
    ),
    "Deposit Sweep Program Banks": (
        "These are the program banks where your uninvested cash is held as of ",
        ". Balances are insured by FDIC, not SIPC.",
    ),
    "Stock Lending - Loaned Securities": (
        "These transactions are specific to Stock Lending and represent the securities on loan "
        "as of ",
        "",
    ),
}


def test_six_tables_have_an_approved_intro_sentence() -> None:
    assert {**dict.fromkeys(_HEADINGS, ()), **_APPROVED} == statement_parser._INTRO_SENTENCES


# Each approved table below a finished table on the same page, that page's number.
_UPPER = {
    "Executed Trades Pending Settlement": (_ledger("sweep"), 5),
    "Deposit Sweep Program Banks": (_ledger("sweep"), 5),
    "Deposit Sweep Activity": (_ledger("collateral"), 8),
    "Stock Lending - Loaned Securities": (_ledger("sweep"), 6),
    "Stock Lending - Loan Activity": (_stock_loaned_page(), 7),
    "Stock Lending - Collateral Activity": (_ledger("sweep"), 6),
}
# Whose sentence each approved table refuses: another approved table's.
_OTHER = {
    "Executed Trades Pending Settlement": "Deposit Sweep Activity",
    "Deposit Sweep Program Banks": "Stock Lending - Loaned Securities",
    "Deposit Sweep Activity": "Stock Lending - Collateral Activity",
    "Stock Lending - Loaned Securities": "Deposit Sweep Program Banks",
    "Stock Lending - Loan Activity": "Executed Trades Pending Settlement",
    "Stock Lending - Collateral Activity": "Stock Lending - Loan Activity",
}
# Each approved table with the dates to try: two only where a sentence has a date slot.
_DATED = [
    (heading, date)
    for heading in _APPROVED
    for date in (
        ("1/31/2025", "12/1/2024")
        if any(len(_APPROVED[name]) > 1 for name in (heading, _OTHER[heading]))
        else ("1/31/2025",)
    )
]


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize("where", ["own-page", "below-a-finished-table"])
@pytest.mark.parametrize(("heading", "date"), _DATED)
def test_an_approved_sentence_follows_its_own_heading_only(
    heading: str, where: str, continued: bool, date: str
) -> None:
    table = _HEADINGS[heading]
    first, header, *rest = table.page.splitlines()
    line = f"{first} (continued)" if continued else first
    upper, upper_number = _UPPER[heading]

    def placed(name: str) -> str:
        page = "\n".join((line, date.join(_APPROVED[name]), "", header, *rest))
        if where == "own-page":
            return _with_pages(table.page, page)
        return _with_pages(table.page, text=_with_pages(upper, f"{upper}\n\n{page}"))

    assert _rows(parse_robinhood_statement(placed(heading))) == _BASE_ROWS
    _unread_refusal(
        placed(_OTHER[heading]),
        f"{heading} heading is not followed by its column header",
        table.number if where == "own-page" else upper_number,
    )


def _sentence(heading: str, slot: str = "1/31/2025") -> str:
    """``heading``'s approved sentence, with ``slot`` in its date slot if it has one."""
    return slot.join(_APPROVED[heading])


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize("heading", _APPROVED)
def test_a_tables_approved_sentence_may_follow_its_heading(heading: str, continued: bool) -> None:
    line = f"{heading} (continued)" if continued else heading
    indented = _with_table(heading, line=line, gap=("", f"  {_sentence(heading)}  ", ""))
    assert _rows(parse_robinhood_statement(indented)) == _BASE_ROWS


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize("owner", _APPROVED)
@pytest.mark.parametrize("heading", [heading for heading in _HEADINGS if heading not in _APPROVED])
def test_a_table_with_no_approved_sentence_refuses_every_sentence(
    heading: str, owner: str, continued: bool
) -> None:
    line = f"{heading} (continued)" if continued else heading
    _unread_refusal(
        _with_table(heading, line=line, gap=("", f"  {_sentence(owner)}  ", "")),
        f"{heading} heading is not followed by its column header",
        _HEADINGS[heading].number,
    )


# An approved sentence with no date slot.
_PLAIN = "Deposit Sweep Activity"


@pytest.mark.parametrize(
    "gap",
    [
        (_sentence(_PLAIN), _sentence(_PLAIN)),
        (f"{_sentence(_PLAIN)} extra",),
        (_sentence(_PLAIN)[:-1],),
    ],
    ids=["twice", "extra", "truncated"],
)
def test_only_the_tables_exact_sentence_may_follow_its_heading(gap: tuple[str, ...]) -> None:
    _unread_refusal(
        _with_table(_PLAIN, gap=gap),
        f"{_PLAIN} heading is not followed by its column header",
        _HEADINGS[_PLAIN].number,
    )


# The two approved sentences with a date slot: at the end, and in the middle.
_ENDING = "Stock Lending - Loaned Securities"
_MIDDLE = "Deposit Sweep Program Banks"


@pytest.mark.parametrize(
    ("heading", "slot"),
    [
        pytest.param(_ENDING, "2025", id="ending-slot-2025"),
        pytest.param(_MIDDLE, "1,234.56", id="middle-slot-1,234.56"),
    ],
)
def test_a_template_sentence_takes_any_digits_in_its_slots(heading: str, slot: str) -> None:
    text = _with_table(heading, gap=(_sentence(heading, slot),))
    assert _rows(parse_robinhood_statement(text)) == _BASE_ROWS


@pytest.mark.parametrize(
    ("heading", "line"),
    [
        pytest.param(_ENDING, _sentence(_ENDING).replace("These", "Those"), id="other-words"),
        pytest.param(_ENDING, _sentence(_ENDING, ""), id="missing-slot"),
        pytest.param(_ENDING, _sentence(_ENDING, "January"), id="words-in-the-slot"),
        pytest.param(_ENDING, _sentence(_ENDING, "1/31/2025 2025"), id="extra-slot"),
        pytest.param(_ENDING, _sentence(_ENDING, "1/31/2025."), id="extra-text"),
        pytest.param(_ENDING, _sentence(_ENDING, "1/31/"), id="dangling-separator"),
        pytest.param(_MIDDLE, _sentence(_MIDDLE, ""), id="missing-middle-slot"),
        pytest.param(_MIDDLE, _sentence(_MIDDLE, "1/31/2025 1/31/2025"), id="extra-middle-slot"),
        pytest.param(_MIDDLE, _sentence(_MIDDLE).replace("Balances", "Deposits"), id="other-tail"),
    ],
)
def test_a_template_sentence_refuses_other_wording_or_slots(heading: str, line: str) -> None:
    _unread_refusal(
        _with_table(heading, gap=(line,)),
        f"{heading} heading is not followed by its column header",
        _HEADINGS[heading].number,
    )


_LOWER_TABLES = [
    pytest.param(_ledger("sweep"), "Stock Lending - Loaned Securities", 6, id="below-a-ledger"),
    pytest.param(
        _stock_loaned_page(), "Stock Lending - Loan Activity", 7, id="below-a-loaned-table"
    ),
    pytest.param(
        _ledger("sweep"), "Stock Lending - Collateral Activity", 6, id="collateral-below-sweep"
    ),
    pytest.param(_ledger("collateral"), "Deposit Sweep Activity", 8, id="sweep-below-collateral"),
]


@pytest.mark.parametrize("continued", [False, True], ids=["heading", "continued-heading"])
@pytest.mark.parametrize(("upper", "lower", "page"), _LOWER_TABLES)
def test_a_table_below_a_finished_table_follows_the_same_heading_rules(
    upper: str, lower: str, page: int, continued: bool
) -> None:
    """The four layouts of #1007: the lower table's sentence parses and any
    other line between its heading and header refuses, as on its own page."""

    lower_page = _HEADINGS[lower].page
    first, *rest = lower_page.splitlines()
    line = f"{first} (continued)" if continued else first

    def stacked(gap: str) -> str:
        below = "\n".join((line, gap, *rest))
        return _with_pages(lower_page, text=_with_pages(upper, f"{upper}\n\n{below}"))

    assert _rows(parse_robinhood_statement(stacked(_sentence(lower)))) == _BASE_ROWS
    _unread_refusal(
        stacked("Unexpected text"), f"{lower} heading is not followed by its column header", page
    )


# ---- #1022: the Income and activity readers stop only at a boundary --------

_INCOME_HEADING = "Income and Expense Summary This Period Year to Date"
_LAST_INCOME_ROW = "Stock Lending $0.05 $0.05"
_IDENTIFIER_LINE = "Public disclosure text.                                      1234567"
_UNSUPPORTED_INCOME = "Income and Expense Summary contains an unsupported row"


def _injected_refusal(
    text: str, message: str, *injected: str, page: int, column: str | None = None
) -> None:
    """The refusal names the page, and none of the injected text or amounts."""

    error = _refusal(text)
    assert (error.message, error.column, error.row_index, error.raw_value) == (
        message,
        column,
        page,
        "",
    )
    rendered = str(error)
    assert f"row {page}" in rendered
    for value in ("Unexpected", "Hidden", *injected):
        assert value not in rendered
    assert re.search(r"\$|[0-9][.,/][0-9]", rendered) is None


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (f"{_INCOME_HEADING} Unexpected text", "table column header holds unsupported text"),
        ("Income and Expense Summary", "table column header is malformed"),
    ],
    ids=["text-after-the-labels", "bare-heading"],
)
def test_the_income_heading_line_holds_its_column_labels_only(line: str, message: str) -> None:
    _injected_refusal(_statement().replace(_INCOME_HEADING, line, 1), message, page=1)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (_INCOME_HEADING, f"{_INCOME_HEADING}\nUnexpected text"),
        ("Dividends $1.00 $1.00", "Dividends $1.00 $1.00\nUnexpected text"),
        (_INCOME_HEADING, f"{_INCOME_HEADING}\nHidden Income $5.00"),
        (_INCOME_HEADING, f"{_INCOME_HEADING}\n   Hidden Income $5.00 $5.00"),
        ("Dividends $1.00 $1.00", "Dividends $1.00 $1.00\n   Hidden Income $5.00 $5.00"),
        (_LAST_INCOME_ROW, f"{_LAST_INCOME_ROW}\nUnexpected text\n"),
        (_IDENTIFIER_LINE, f"{_IDENTIFIER_LINE}\nUnexpected text"),
        (
            _IDENTIFIER_LINE,
            _IDENTIFIER_LINE.replace("Public disclosure text.", "Unexpected text").replace(
                "1234567", "Unexpected"
            ),
        ),
        (
            _IDENTIFIER_LINE,
            _IDENTIFIER_LINE.replace("Public disclosure text.", "   Hidden Income $5.00 $5.00"),
        ),
        (_IDENTIFIER_LINE, f"Unexpected text $5.00\n{_IDENTIFIER_LINE}"),
        (_IDENTIFIER_LINE, f"Unexpected  text\n{_IDENTIFIER_LINE}"),
        (_IDENTIFIER_LINE, f"Unexpected text\nDividends text\n{_IDENTIFIER_LINE}"),
    ],
    ids=[
        "text-under-the-heading",
        "text-between-rows",
        "one-amount-row-under-the-heading",
        "indented-row-under-the-heading",
        "indented-row-between-rows",
        "text-after-the-last-row-apart-from-the-identifier",
        "text-below-the-identifier-line",
        "paragraph-without-an-identifier",
        "padded-row-before-the-identifier",
        "paragraph-line-with-an-amount",
        "paragraph-line-with-a-column-gap",
        "paragraph-line-opening-with-an-income-label",
    ],
)
def test_every_income_block_line_is_a_row_or_the_closing_paragraph(old: str, new: str) -> None:
    """Under the heading, only the Income rows, blank lines and the page
    marker; below the last row, one paragraph that ends at the document
    identifier. A paragraph line refuses if it holds a money-shaped amount,
    a cell after a gap of two or more blanks, or starts with an Income label.
    On the identifier line, only the text before the identifier is checked."""

    text = _statement()
    assert text.count(old) == 1
    _injected_refusal(text.replace(old, new), _UNSUPPORTED_INCOME, page=1)


_BASE_STATEMENT = parse_robinhood_statement(_statement())


def _income_values(statement: RobinhoodStatement) -> list[tuple[str, Decimal, Decimal]]:
    return [(row.label, row.this_period_usd, row.year_to_date_usd) for row in statement.income]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            _IDENTIFIER_LINE,
            "Synthetic closing note, line one with a $7 figure\n"
            "and 3 more words, line two\n"
            f"{_IDENTIFIER_LINE}",
        ),
        (_IDENTIFIER_LINE, _IDENTIFIER_LINE.replace("Public disclosure text.", "")),
        ("Dividends $1.00 $1.00\n", "\nDividends $1.00 $1.00\n\n"),
    ],
    ids=["several-paragraph-lines", "identifier-alone", "blank-lines-between-rows"],
)
def test_the_income_block_reads_as_before(old: str, new: str) -> None:
    statement = parse_robinhood_statement(_statement().replace(old, new, 1))
    assert _income_values(statement) == _income_values(_BASE_STATEMENT)
    assert statement.document_identifier == "1234567"
    assert _rows(statement) == _BASE_ROWS


def test_the_page_marker_may_sit_between_the_income_rows() -> None:
    text = _move_marker(_statement(), 1, "Interest Earned $0.10 $0.10")
    assert _income_values(parse_robinhood_statement(text)) == _income_values(_BASE_STATEMENT)


@pytest.mark.parametrize(
    "ending",
    [
        "paragraph-at-page-end",
        "marker-after-identifier",
        "table-after-identifier",
        "rows-at-page-end",
    ],
)
def test_the_income_block_validates_each_closing_boundary(ending: str) -> None:
    text = _statement()
    if ending == "table-after-identifier":
        loan = _stock_loan_activity_page()
        text = _with_pages(
            loan, text=text.replace(_IDENTIFIER_LINE, f"{_IDENTIFIER_LINE}\n{loan}", 1)
        )
    else:
        pages = text.split("\f")
        marker = f"Page 1 of {len(pages)}"
        body = pages[0].replace(marker, "").rstrip()
        if ending == "rows-at-page-end":
            body = body.removesuffix(_IDENTIFIER_LINE).rstrip()
        pages[0] = (
            f"{body}\n{marker}" if ending == "marker-after-identifier" else f"{marker}\n{body}"
        )
        text = "\f".join(pages)
    statement = parse_robinhood_statement(text)
    assert _income_values(statement) == _income_values(_BASE_STATEMENT)
    assert _rows(statement) == _BASE_ROWS
    assert statement.document_identifier == (None if ending == "rows-at-page-end" else "1234567")


def test_a_table_may_start_right_below_the_income_rows() -> None:
    loan = _stock_loan_activity_page()
    text = _with_pages(
        loan,
        text=_statement().replace(f"{_IDENTIFIER_LINE}\n", f"{loan}\n", 1),
    )
    statement = parse_robinhood_statement(text)
    assert statement.document_identifier is None
    assert _rows(statement) == _BASE_ROWS


_ACTIVITY_KINDS = {"activity": False, "pending": True}


def _below_activity(kind: str, lower: str, *, gap: str = "\n\n") -> str:
    """The fixture with ``lower`` printed below the ``kind`` table's total on
    its page, and moved off its own page when it has one."""

    upper = _TABLES[kind].page
    text = _with_pages(upper, f"{upper}{gap}{lower}")
    return _with_pages(lower, text=text) if f"{lower}\nPage " in _statement() else text


@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_a_sweep_ledger_may_start_below_a_finished_activity_table(kind: str) -> None:
    statement = parse_robinhood_statement(_below_activity(kind, _ledger("sweep")))
    assert _rows(statement) == _BASE_ROWS


@pytest.mark.parametrize("trailing", ["Unexpected text", "dated-row"])
@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_nothing_but_another_table_may_follow_an_activity_total(kind: str, trailing: str) -> None:
    heading = "Executed Trades Pending Settlement" if _ACTIVITY_KINDS[kind] else "Account Activity"
    line = _HEADINGS[heading].hidden if trailing == "dated-row" else trailing
    _table_refusal(
        kind,
        _below_activity(kind, line, gap="\n"),
        "total is followed by an unsupported line",
    )
    assert "Unexpected" not in str(_refusal(_below_activity(kind, line, gap="\n")))


def test_pending_trades_below_a_finished_account_activity_table_still_refuse() -> None:
    _injected_refusal(
        _below_activity("activity", _activity_page(pending=True)),
        "activity table header is missing or ambiguous",
        page=3,
    )


def test_a_ledger_inside_unfinished_activity_rows_refuses() -> None:
    """The rows run on to the next page, where the total is printed; a table
    can't start between them."""

    lines = _activity_page().splitlines()
    ledger = _ledger("sweep")
    text = _with_pages(
        _activity_page(),
        "\n".join((*lines[:5], "", ledger)),
        "\n".join((_TABLES["activity"].continued, *lines[5:])),
    )
    _injected_refusal(
        _with_pages(ledger, text=text),
        "another table starts before the activity total",
        page=3,
    )


def test_an_activity_header_without_its_heading_refuses() -> None:
    """The activity reader keeps its missing-heading refusal before the layout check."""

    disclosure = "Important Information\nVariable public disclosure prose."
    hidden = _HEADINGS["Account Activity"].hidden
    _injected_refusal(
        _with_pages(disclosure, f"{disclosure}\n{_activity_header()[0]}\n{hidden}"),
        "activity table header has no heading",
        page=10,
    )


def _indented(page: str, margin: str = "") -> str:
    """``page`` with every line under its heading indented 20 blanks, and
    ``margin`` printed in that indent on its first row."""

    heading, header, row, *rest = page.splitlines()
    pad = " " * 20
    return "\n".join(
        (heading, pad + header, margin.ljust(20) + row, *(pad + line for line in rest))
    )


@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_an_indented_activity_table_reads_as_before(kind: str) -> None:
    page = _TABLES[kind].page
    assert _rows(parse_robinhood_statement(_with_pages(page, _indented(page)))) == _BASE_ROWS


@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_text_before_a_dated_rows_description_column_refuses(kind: str) -> None:
    table = _TABLES[kind]
    _injected_refusal(
        _with_pages(table.page, _indented(table.page, "Unexpected text")),
        "activity row holds text before its Description column",
        page=table.number,
        column="Description",
    )


def _with_activity_qty(kind: str, quantity: str) -> str:
    """The fixture with its ``Purchase`` or ``Pending Buy`` row's Qty cell set to ``quantity``."""

    table = _TABLES[kind]
    _header, labels, starts = _activity_header(pending=_ACTIVITY_KINDS[kind])
    start, end = starts[labels.index("Qty")], starts[labels.index("Price")]
    row = table.page.splitlines()[2]
    assert row[start:end].strip() == "1"
    changed = row[:start] + quantity.ljust(end - start) + row[end:]
    return _with_pages(table.page, table.page.replace(row, changed, 1))


@pytest.mark.parametrize("quantity", ["1,,0", "1,0", "1,00", "1,0000", ",100", "1,000,00"])
@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_an_activity_qty_groups_its_digits_in_threes(kind: str, quantity: str) -> None:
    _injected_refusal(
        _with_activity_qty(kind, quantity),
        "number has an unsupported format",
        quantity,
        page=_TABLES[kind].number,
        column="Qty",
    )


@pytest.mark.parametrize(
    "quantity", ["0", "10", "1000", "1,000", "0.5", "1000.25", "1,000.25", "123,456"]
)
@pytest.mark.parametrize("kind", _ACTIVITY_KINDS)
def test_an_activity_qty_reads_plain_or_grouped_digits(kind: str, quantity: str) -> None:
    statement = parse_robinhood_statement(_with_activity_qty(kind, quantity))
    row = 2 if _ACTIVITY_KINDS[kind] else 0
    assert statement.activities[row].quantity == Decimal(quantity.replace(",", ""))


def _holding_with(quantity: str, price: str) -> str:
    """The fixture with its first holding's Qty and Price set, at its $100.00 market value."""

    starts = _HELD_STARTS
    row = _fixed(
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
    )
    changed = _fixed(
        (0, *starts),
        (
            "Example Equity",
            "EQTY",
            "Margin",
            quantity,
            price,
            "$100.00",
            "$1.00",
            "2.50%",
            "33.33%",
        ),
    )
    assert _statement().count(row) == 1
    return _statement().replace(row, changed, 1)


def test_holdings_and_stock_lending_shares_group_their_digits_in_threes() -> None:
    assert parse_robinhood_statement(_holding_with("10", "$10.0000")).positions[0].quantity == 10
    _injected_refusal(
        _holding_with("1,0", "$10.0000"),
        "number has an unsupported format",
        "1,0",
        page=2,
        column="Qty",
    )

    loaned = _stock_loaned_page()
    row = _fixed(_LOANED_STARTS, ("Loaned Example", "LOAN", "1", "$5.0000", "$5.00"))
    changed = _fixed(_LOANED_STARTS, ("Loaned Example", "LOAN", "1,0", "$0.5000", "$5.00"))
    _injected_refusal(
        _with_pages(loaned, loaned.replace(row, changed, 1)),
        "Stock Lending security row is incomplete or column-shifted",
        "1,0",
        page=7,
    )

    _injected_refusal(
        _with_loan_pages((_loan_row("Loan", lent="1,,0"),)),
        "number has an unsupported format",
        "1,,0",
        page=8,
        column="Shares Lent From Account",
    )


# ---- #1026: a heading line is its whole heading, read by a reader, or it refuses

_AS_HEADING = "Account Summary Opening Balance Closing Balance"
_MALFORMED_HEADER = "table column header is malformed"


@pytest.mark.parametrize(
    ("line", "message"),
    [
        pytest.param(f"{_AS_HEADING}   Hidden Row $5.00 $5.00", _HEADER_TEXT, id="hidden-row"),
        pytest.param(
            f"{_AS_HEADING}   Portfolio Value $999.00 $999.00", _HEADER_TEXT, id="summary-row"
        ),
        pytest.param(f"{_AS_HEADING} Unexpected text", _HEADER_TEXT, id="text-after-the-labels"),
        pytest.param("Account Summary Unexpected text", _MALFORMED_HEADER, id="text-for-labels"),
        pytest.param("Account Summary", _MALFORMED_HEADER, id="bare-heading"),
        pytest.param(
            "Account Summary Closing Balance Opening Balance",
            _MALFORMED_HEADER,
            id="labels-reversed",
        ),
        pytest.param(
            "Account SummaryX Opening Balance Closing Balance", _HEADER_TEXT, id="glued-heading"
        ),
        pytest.param(f"{_AS_HEADING}   99.00%", _HEADER_TEXT, id="legend-percentage"),
        pytest.param(
            f"{_AS_HEADING}   Cash and Cash Equivalents 99.00%",
            _HEADER_TEXT,
            id="legend-label-and-percentage",
        ),
    ],
)
def test_the_account_summary_heading_line_holds_its_column_labels_only(
    line: str, message: str
) -> None:
    text = _statement()
    assert text.count(_AS_HEADING) == 1
    _injected_refusal(text.replace(_AS_HEADING, line), message, "SummaryX", "5.00", "99.00", page=1)


def test_a_column_spaced_account_summary_heading_line_reads_as_before() -> None:
    spaced = "Account Summary          Opening Balance          Closing Balance"
    statement = parse_robinhood_statement(_statement().replace(_AS_HEADING, spaced, 1))
    assert statement.account_summary == _BASE_STATEMENT.account_summary
    assert _rows(statement) == _BASE_ROWS


def _not_a_header(line: str) -> str:
    label = next(
        label
        for label in ("Securities Held in Account", "Loaned Securities")
        if line.startswith(label)
    )
    return f"{label} line is not a holdings header"


@pytest.mark.parametrize(
    "page",
    [
        f"Securities Held in Account\nUnexpected text\n{_HIDDEN_HOLDING}",
        "Loaned Securities\nHidden Income $5.00 $5.00",
        f"Loaned Securities Unexpected text\n{_HIDDEN_HOLDING}",
        f"Securities Held in Account Unexpected text\n{_HIDDEN_HOLDING}",
        f"Securities Held in Accounts\n{_HIDDEN_HOLDING}",
    ],
    ids=["held-label", "loaned-label", "loaned-label-text", "held-label-text", "held-label-glued"],
)
def test_a_holdings_label_page_without_a_holdings_header_refuses(page: str) -> None:
    """A holdings label starts a holdings page only as a holdings header."""

    _injected_refusal(
        _statement(extra_pages=(page,)), _not_a_header(page), "Accounts", "5.00", page=10
    )


_EQUITY_ROW = _position_page().splitlines()[2]
_PENDING_ROW = _activity_page(pending=True).splitlines()[2]
# Where a line goes in the fixture: below or above an anchor line, on a page.
_IN_TABLE = {
    "after-the-first-held-row": (_EQUITY_ROW, "below", 2),
    "above-total-securities": (_TOTAL_ROW, "above", 2),
    "after-the-activity-continuation": ("Unlisted Continuation", "below", 3),
    "after-the-pending-row": (_PENDING_ROW, "below", 4),
    "below-the-bank-row": (_BANK_ROW, "below", 5),
}


def _in_table(place: str, line: str) -> str:
    anchor, where, _page = _IN_TABLE[place]
    text = _statement()
    assert text.count(f"{anchor}\n") == 1
    changed = f"{anchor}\n{line}\n" if where == "below" else f"{line}\n{anchor}\n"
    return text.replace(f"{anchor}\n", changed)


@pytest.mark.parametrize(
    "line",
    [
        "Securities Held in Account",
        "Securities Held in Account Unexpected text",
        "Loaned Securities",
        "Loaned Securities Unexpected text",
    ],
)
@pytest.mark.parametrize("place", _IN_TABLE)
def test_a_holdings_label_inside_another_tables_lines_refuses(place: str, line: str) -> None:
    """The holdings rows, activity rows and Program Banks paragraph would
    otherwise take the label line as text."""

    _injected_refusal(_in_table(place, line), _not_a_header(line), page=_IN_TABLE[place][2])


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("Important Information", "Important Information heading is not its page's first line"),
        (
            "Important Information Unexpected text",
            "Important Information heading line carries other text",
        ),
    ],
    ids=["whole", "decorated"],
)
@pytest.mark.parametrize(
    "place",
    ["after-the-activity-continuation", "after-the-pending-row", "below-the-bank-row"],
)
def test_the_disclosure_heading_inside_a_tables_lines_refuses(
    place: str, line: str, message: str
) -> None:
    _injected_refusal(_in_table(place, line), message, page=_IN_TABLE[place][2])


_DISCLOSURE = "Important Information"
_DISCLOSURE_PROSE = "Variable public disclosure prose."
_DISCLOSURE_UNSUPPORTED = "disclosure page holds an unsupported line"
_OVERFLOW_UNSUPPORTED = "overflow page holds an unsupported line"
_NO_FOOTNOTE = "overflow page does not open with the Total Securities footnote"
_NO_FAMILY = "statement page has no accepted content family"
_HIDDEN_INCOME = "Hidden Income $5.00 $5.00"
# Prose as these pages print it, in two columns, with digits but no money
# amount, date or percent sign.
_TWO_COLUMN_PROSE = (
    f"{'Synthetic left column prose, SIPC since 1970.'.ljust(60)}Synthetic right column prose.",
    f"{'Call 1-800-000-0000 with any questions.'.ljust(60)}Rule 15c3-3 applies.",
)
# Table-shaped rows, each holding a money amount or a date.
_HIDDEN_ROWS = {
    "holding-row": _HIDDEN_HOLDING,
    "income-row": _HIDDEN_INCOME,
    "ledger-row": _LEDGER_MOVEMENT,
    "loan-row": _loan_row("Hidden Loan", lent="5"),
    "bank-row": _HIDDEN_BANK,
    "right-column-row": f"{'Synthetic left column prose.'.ljust(60)}{_HIDDEN_INCOME}",
    "money-in-prose": "Synthetic prose with a fee of 12.50 each.",
    "date-in-prose": "Synthetic prose dated 1/31/2025.",
}


def _with_disclosure(*lines: str) -> str:
    """The fixture with its disclosure page, page 10, printing ``lines``."""

    return _with_pages(f"{_DISCLOSURE}\n{_DISCLOSURE_PROSE}", "\n".join(lines))


@pytest.mark.parametrize(
    "lines",
    [(), (_DISCLOSURE_PROSE,), ("", _DISCLOSURE_PROSE, ""), _TWO_COLUMN_PROSE],
    ids=["heading-only", "prose", "prose-between-blanks", "two-column-prose-with-digits"],
)
def test_a_disclosure_page_holds_prose_without_money_dates_or_percent_signs(
    lines: tuple[str, ...],
) -> None:
    statement = parse_robinhood_statement(_with_disclosure(_DISCLOSURE, *lines))
    assert statement.disclosure_pages == (10,)
    assert _rows(statement) == _BASE_ROWS


def test_the_page_marker_may_print_above_the_disclosure_heading() -> None:
    text = _move_marker(_statement(), 10, _DISCLOSURE)
    assert f"Page 10 of 10\n{_DISCLOSURE}\n" in text
    statement = parse_robinhood_statement(text)
    assert statement.disclosure_pages == (10,)
    assert _rows(statement) == _BASE_ROWS


@pytest.mark.parametrize(
    ("text", "message"),
    [
        *(
            pytest.param(
                _with_disclosure(_DISCLOSURE, _DISCLOSURE_PROSE, row),
                _DISCLOSURE_UNSUPPORTED,
                id=name,
            )
            for name, row in _HIDDEN_ROWS.items()
        ),
        pytest.param(
            _with_disclosure(_DISCLOSURE, "Synthetic prose at a 5% rate."),
            _DISCLOSURE_UNSUPPORTED,
            id="percent-in-prose",
        ),
        pytest.param(
            _with_disclosure(_DISCLOSURE, "Equities 99.00%"),
            _DISCLOSURE_UNSUPPORTED,
            id="allocation-legend-line",
        ),
        pytest.param(
            _statement(extra_pages=(f"Important Information Unexpected text\n{_HIDDEN_HOLDING}",)),
            _NO_FAMILY,
            id="decorated-heading-page",
        ),
        pytest.param(
            _statement(extra_pages=(f"Important Informational hidden page\n{_HIDDEN_INCOME}",)),
            _NO_FAMILY,
            id="glued-heading-page",
        ),
        pytest.param(
            _with_disclosure("Unexpected text", _DISCLOSURE, _DISCLOSURE_PROSE),
            _NO_FAMILY,
            id="text-above-the-heading",
        ),
        pytest.param(
            _with_disclosure(_HIDDEN_HOLDING, _DISCLOSURE, _DISCLOSURE_PROSE),
            _NO_FAMILY,
            id="row-above-the-heading",
        ),
        pytest.param(
            _with_disclosure(_DISCLOSURE, "Loaned Securities are described in synthetic prose."),
            "Loaned Securities line is not a holdings header",
            id="holdings-label-in-the-prose",
        ),
        pytest.param(
            _statement().replace("Page 10 of 10", "Unexpected text Page 10 of 10"),
            "page marker line carries other text",
            id="text-on-the-marker-line",
        ),
    ],
)
def test_a_disclosure_page_refuses_a_line_that_could_be_a_table_row(
    text: str, message: str
) -> None:
    _injected_refusal(
        text, message, "Informational", "Movement", "5.00", "12.50", "5%", "99.00", page=10
    )


def _overflow(*lines: str, marker: bool = False) -> str:
    """The markerless-overflow fixture, its page 2 printing ``lines``, and
    with ``marker`` its own page marker below them."""

    pages = _statement(markerless_overflow=True).split("\f")
    assert pages[1] == "\n".join(_OVERFLOW_LINES)
    pages[1] = "\n".join((*lines, *(("Page 2 of 11",) if marker else ())))
    return "\f".join(pages)


_FOOTNOTE_LINES = _OVERFLOW_LINES[:2]
# The footnote wrapped onto three lines, with a blank line inside it.
_REWRAPPED_FOOTNOTE = (
    "* “Total Securities” includes those securities held in your account as well as those",
    "",
    "on loan through Stock Lending (identified herein as “Loaned Securities” or “Loaned Stocks”).",
    "The provisions of the Securities Investor Protection Act of 1970 may not protect you with "
    "respect to Loaned Securities.",
)


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(_OVERFLOW_LINES, id="fixture"),
        pytest.param(_PORTFOLIO_NOTE, id="footnote-as-on-the-holdings-page"),
        pytest.param(
            (
                *_REWRAPPED_FOOTNOTE,
                *_TWO_COLUMN_PROSE,
                "Synthetic program bank prose at a 0.01% yield.",
            ),
            id="rewrapped-footnote-then-two-column-prose-with-a-percent-sign",
        ),
    ],
)
@pytest.mark.parametrize("marker", [False, True], ids=["markerless", "with-marker"])
def test_an_overflow_page_opens_with_the_footnote_then_holds_no_money_or_dates(
    lines: tuple[str, ...], marker: bool
) -> None:
    statement = parse_robinhood_statement(_overflow(*lines, marker=marker))
    assert statement.disclosure_pages == (11,)
    assert _rows(statement) == _BASE_ROWS


@pytest.mark.parametrize("marker", ["none", "below", "above"])
def test_the_overflow_footnote_may_wrap_at_any_word(marker: str) -> None:
    words = " ".join(_PORTFOLIO_NOTE).split()
    for boundary in range(1, len(words)):
        first = " ".join(words[:boundary])
        text = _overflow(
            first,
            "",
            " ".join(words[boundary:]),
            *_TWO_COLUMN_PROSE,
            "Synthetic prose at a 0.01% yield.",
            marker=marker != "none",
        )
        if marker == "above":
            text = _move_marker(text, 2, first)
        statement = parse_robinhood_statement(text)
        assert statement.disclosure_pages == (11,)
        assert statement.account_summary == _BASE_STATEMENT.account_summary
        assert _rows(statement) == _BASE_ROWS


@pytest.mark.parametrize(
    ("text", "message"),
    [
        *(
            pytest.param(_overflow(*_OVERFLOW_LINES, row), _OVERFLOW_UNSUPPORTED, id=name)
            for name, row in _HIDDEN_ROWS.items()
        ),
        pytest.param(
            _overflow(*_PORTFOLIO_NOTE, "Deposit Sweep Program Banks", "Bank Balance"),
            "Deposit Sweep Program Banks heading is preceded by an unsupported line",
            id="table-after-the-footnote",
        ),
        pytest.param(
            _overflow(*_OVERFLOW_LINES, "Loaned Securities Unexpected text"),
            "Loaned Securities line is not a holdings header",
            id="holdings-label-after-the-footnote",
        ),
        pytest.param(
            _overflow(*_OVERFLOW_LINES, "Important Information"),
            "Important Information heading is not its page's first line",
            id="disclosure-heading-after-the-footnote",
        ),
        pytest.param(
            _overflow("* “Total Securities” includes Unexpected text", *_OVERFLOW_LINES[1:]),
            _NO_FOOTNOTE,
            id="altered-first-line",
        ),
        pytest.param(
            _overflow("* “Total Securities” includesX", *_OVERFLOW_LINES[1:]),
            _NO_FOOTNOTE,
            id="glued-first-line",
        ),
        pytest.param(
            _overflow(_FOOTNOTE_LINES[0], _HIDDEN_HOLDING, *_OVERFLOW_LINES[1:]),
            _NO_FOOTNOTE,
            id="row-inside-the-footnote",
        ),
        pytest.param(
            _overflow(_FOOTNOTE_LINES[0], *_OVERFLOW_LINES[2:]),
            _NO_FOOTNOTE,
            id="truncated-footnote",
        ),
        pytest.param(
            _overflow(_FOOTNOTE_LINES[0], f"{_FOOTNOTE_LINES[1]} Unexpected text"),
            _NO_FOOTNOTE,
            id="text-after-the-footnote-on-its-line",
        ),
        pytest.param(
            _overflow(*_OVERFLOW_LINES, marker=True).replace(
                "Page 2 of 11", "Unexpected text Page 2 of 11"
            ),
            "page marker line carries other text",
            id="text-on-the-marker-line",
        ),
    ],
)
def test_an_overflow_page_refuses_a_line_that_could_be_a_table_row(text: str, message: str) -> None:
    _injected_refusal(text, message, "includesX", "Movement", "5.00", "12.50", page=2)
