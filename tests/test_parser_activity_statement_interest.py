"""Tests for :mod:`ibkr_fa.parser.activity_statement.interest`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.schedule_os_interest import (
    UnprovenInterestError,
    require_no_unproven_interest,
)
from ibkr_fa.exceptions import InvariantError, ParseError
from ibkr_fa.models import InterestCredit, InterestNonCredit, InterestRowGroup
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_interest,
)
from ibkr_fa.parser.activity_statement.multi_section import Section
from tests._activity_statement_rows import make_interest_row
from tests._crash_reports import assert_prints_no_marker
from tests._income_blocks import COLUMNS, HEADER_LINE, block, closed, summary_row, total_row
from tests._statement_period import ALL_DATES

# ---- test fixtures --------------------------------------------------------

# The file line of a test block's first data row; the rest follow on.
_FIRST_LINE = HEADER_LINE + 1


def parse(rows: list[dict[str, str]]) -> tuple[list[InterestCredit], list[InterestNonCredit]]:
    """Parse ``rows`` as one Interest block starting on :data:`_FIRST_LINE`,
    closed by their ``Total`` row."""
    return parse_block(closed(rows))


def parse_block(
    rows: list[dict[str, str]],
) -> tuple[list[InterestCredit], list[InterestNonCredit]]:
    """Parse exactly ``rows`` as one Interest block starting on :data:`_FIRST_LINE`."""
    return parse_activity_statement_interest(
        block(KnownActivitySection.INTEREST, rows), period=ALL_DATES
    )


def parse_credits(rows: list[dict[str, str]]) -> list[InterestCredit]:
    """Parse ``rows`` that hold only credits, closed by their ``Total`` row."""
    interest_credits, non_credits = parse(rows)
    assert non_credits == []
    return interest_credits


# ---- happy path -----------------------------------------------------------


def test_single_interest_credit_parsed() -> None:
    """Happy path: one well-formed Interest row → one InterestCredit
    with all fields round-tripped from the source row (date parsed
    DD-MM-YYYY, amount as Decimal, source_row_index 0)."""
    [credit] = parse_credits([make_interest_row()])
    assert credit.credit_date == date(2025, 4, 30)
    assert credit.amount_gross == Decimal("1.23")
    assert credit.currency == "USD"
    assert credit.description == "USD Credit Interest for Apr-2025"
    assert credit.source_row_index == 0


def test_input_order_preserved() -> None:
    """Interest rows are emitted chronologically by IBKR; the parser
    preserves input order so the downstream audit trail matches the
    source statement row order."""
    rows = [
        make_interest_row(credit_date="30-04-2025", description="USD Credit Interest for Apr-2025"),
        make_interest_row(credit_date="31-05-2025", description="USD Credit Interest for May-2025"),
        make_interest_row(credit_date="30-06-2025", description="USD Credit Interest for Jun-2025"),
    ]
    parsed = parse_credits(rows)
    assert [c.credit_date for c in parsed] == [
        date(2025, 4, 30),
        date(2025, 5, 31),
        date(2025, 6, 30),
    ]
    assert [c.source_row_index for c in parsed] == [0, 1, 2]


def test_empty_input_returns_empty_lists() -> None:
    """Accounts whose cash balance never exceeded IBKR's minimum-
    balance threshold for credit interest emit zero Interest rows.
    The parser must accept this without raising."""
    assert parse([]) == ([], [])


# ---- footer / summary row filtering ---------------------------------------


def test_total_row_is_checked_not_returned() -> None:
    """IBKR prints a per-currency Total row as a 'Data' row with
    Currency='Total'. The parser checks it against the rows above it
    (#908) and returns only the credits."""
    interest_credits, non_credits = parse_block([make_interest_row(), total_row("1.23")])
    assert (len(interest_credits), non_credits) == (1, [])


def test_summary_row_is_checked_not_returned() -> None:
    """Multi-currency accounts end a block with a 'Total Interest in
    <CCY>' summary after the last Total row. The parser checks it
    (#908) and returns only the credits."""
    rows = [make_interest_row(), total_row("1.23"), summary_row("Interest", "USD", "1.23")]
    interest_credits, non_credits = parse_block(rows)
    assert (len(interest_credits), non_credits) == (1, [])


def test_empty_date_non_summary_row_raises_parse_error() -> None:
    """A real credit row whose Date is blank (e.g., a malformed /
    truncated export) must fail loudly so the user notices the bad
    row — silent skipping would corrupt downstream Schedule OS
    totals."""
    bad_row = make_interest_row(credit_date="")
    # Description does NOT start with "Total Interest in" — keep the
    # default human-readable label so this row is unmistakably a
    # malformed credit row, not a summary footer.
    with pytest.raises(ParseError) as exc:
        parse_credits([bad_row])
    assert exc.value.column == "Date"


# ---- malformed-row rejection ----------------------------------------------


def test_missing_required_column_raises_parse_error() -> None:
    """Without the shape-drift guard, an Activity Statement whose
    Interest section header has changed would raise a raw KeyError
    past the CLI's documented-error catch."""
    bad_row = make_interest_row()
    del bad_row["Amount"]
    with pytest.raises(ParseError) as exc:
        parse_credits([bad_row])
    assert exc.value.section == "Interest"
    assert exc.value.column == "Amount"


@pytest.mark.parametrize(
    "bad_date",
    [
        "30/04/2025",  # slashes not dashes
        "30-4-2025",  # single-digit month
        "April 30, 2025",  # long form
        "30.04.2025",  # dot separator
    ],
)
def test_malformed_date_raises_parse_error(bad_date: str) -> None:
    """The parser accepts DD-MM-YYYY OR YYYY-MM-DD only. Slash-
    delimited, single-digit-component, long-form, and dot-separated
    variants all raise ParseError pointing at the Date column."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(credit_date=bad_date)])
    assert exc.value.column == "Date"


def test_iso_yyyy_mm_dd_date_accepted() -> None:
    """IBKR emits two date shapes in the Interest section depending
    on per-account preference: DD-MM-YYYY (e.g., '30-04-2025') AND
    YYYY-MM-DD (e.g., '2025-04-30'). Both must be accepted so the
    parser doesn't reject otherwise-valid real-world statements."""
    [credit] = parse_credits([make_interest_row(credit_date="2025-04-30")])
    assert credit.credit_date == date(2025, 4, 30)


def test_invalid_calendar_date_raises_parse_error() -> None:
    """Date string matches DD-MM-YYYY but isn't a real date
    (e.g., 30-02-2025 — Feb has no 30th)."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(credit_date="30-02-2025")])
    assert exc.value.column == "Date"


def test_malformed_amount_raises_parse_error() -> None:
    """Non-numeric Amount (e.g., a stray free-form string) must
    surface as a ParseError pointing at the Amount column, not as
    a raw decimal.InvalidOperation past the CLI's documented-error
    catch."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(amount="not-a-number")])
    assert exc.value.column == "Amount"


@pytest.mark.parametrize(
    "bad_amount",
    ["NaN", "Infinity", "-Infinity"],
)
def test_non_finite_amount_raises_parse_error(bad_amount: str) -> None:
    """NaN/Infinity would propagate into per-credit INR conversion
    and corrupt totals. Reject at parse time."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(amount=bad_amount)])
    assert exc.value.column == "Amount"
    assert "finite" in exc.value.message.lower()


def test_zero_amount_is_unproven() -> None:
    """A zero row isn't a credit, and it isn't debit interest either
    (that needs a negative amount), so it's unproven (#905). The
    parser returns it with its file line instead of dropping it."""
    interest_credits, [row] = parse([make_interest_row(amount="0")])
    assert interest_credits == []
    assert row == InterestNonCredit(
        group=InterestRowGroup.UNPROVEN_NON_CREDIT,
        amount=Decimal("0"),
        currency="USD",
        row_date=date(2025, 4, 30),
        description="USD Credit Interest for Apr-2025",
        file_line=_FIRST_LINE,
    )


def test_negative_credit_labelled_row_is_unproven() -> None:
    """A negative correction to a credit changes filed interest, but
    the statement doesn't prove by how much, so it's unproven (#905),
    not silently dropped."""
    interest_credits, [row] = parse([make_interest_row(amount="-1.23")])
    assert interest_credits == []
    assert row.group is InterestRowGroup.UNPROVEN_NON_CREDIT
    assert row.group.unproven
    assert row.amount == Decimal("-1.23")


@pytest.mark.parametrize(
    "description",
    ["USD Debit Interest for Apr-2025", "usd debit interest for apr-2025", "DEBIT INTEREST"],
)
def test_negative_debit_interest_row_is_debit_interest(description: str) -> None:
    """Debit interest (margin interest the user PAID, a negative
    amount whose description says debit interest, case-insensitive)
    isn't income. IBKR's official docs at
    https://ibkrguides.com/reportingreference/reportguide/interest_default.htm
    confirm the same Interest section carries both credit AND debit
    interest. It comes back as debit interest, which nothing refuses."""
    interest_credits, [row] = parse([make_interest_row(amount="-1.23", description=description)])
    assert interest_credits == []
    assert row.group is InterestRowGroup.DEBIT_INTEREST
    assert not row.group.unproven


def test_zero_debit_interest_row_is_unproven() -> None:
    """Rule 3 needs a negative amount: a zero row labelled debit
    interest falls through to unproven."""
    _credits, [row] = parse(
        [make_interest_row(amount="0", description="USD Debit Interest for Apr-2025")]
    )
    assert row.group is InterestRowGroup.UNPROVEN_NON_CREDIT


def test_debit_interest_row_in_exotic_currency_is_not_raised() -> None:
    """A debit-interest row in EUR / GBP (common on European margin
    accounts) is debit interest, NOT raised on the v1 USD-only
    currency check, which only applies to positive rows."""
    _credits, [row] = parse(
        [
            make_interest_row(
                amount="-2.50", currency="EUR", description="EUR Debit Interest for Apr-2025"
            )
        ]
    )
    assert row.group is InterestRowGroup.DEBIT_INTEREST
    assert row.currency == "EUR"


def test_negative_amount_with_malformed_date_raises_on_date() -> None:
    """T3 codex-emulation P2 finding on PR #186 round 2: shape
    validation runs BEFORE the classification, so a malformed row
    (bad date + negative amount) raises on the underlying shape bug
    rather than coming back as a non-credit row."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(credit_date="not-a-date", amount="-1.23")])
    assert exc.value.column == "Date"


def test_negative_amount_with_empty_description_raises_on_description() -> None:
    """Companion to the negative-amount + bad-date test: an empty
    Description on a negative-amount row still raises on the
    description shape bug (T3 codex-emulation P2 finding on
    PR #186 round 2), so structural malformations surface even when
    the row would otherwise be a non-credit row."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(description="", amount="-1.23")])
    assert exc.value.column == "Description"


def test_negative_amount_with_non_finite_amount_raises_on_amount() -> None:
    """A negative-amount row that's ALSO non-finite (NaN or
    Infinity) raises on the amount-finiteness check — finite-Decimal
    validation precedes the sign-based classification."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(amount="-Infinity")])
    assert exc.value.column == "Amount"
    assert "finite" in exc.value.message.lower()


def test_mixed_credit_and_debit_rows_classify_every_row() -> None:
    """Realistic scenario: a margin account's Interest section
    interleaves credit (positive USD) and debit (negative, sometimes
    other currencies, each currency in its own run) rows. Credits come
    back as InterestCredit, each debit row as debit interest with its
    own file line."""
    rows = [
        make_interest_row(credit_date="30-04-2025", amount="1.23"),
        make_interest_row(
            credit_date="30-04-2025", amount="-0.50", description="USD Debit Interest for Apr-2025"
        ),
        make_interest_row(credit_date="31-05-2025", amount="1.45"),
        make_interest_row(credit_date="30-06-2025", amount="1.67"),
        total_row("3.85"),
        make_interest_row(
            credit_date="31-05-2025",
            amount="-2.10",
            currency="EUR",
            description="EUR Debit Interest for May-2025",
        ),
        total_row("-2.10"),
    ]
    interest_credits, non_credits = parse_block(rows)
    assert [str(c.amount_gross) for c in interest_credits] == ["1.23", "1.45", "1.67"]
    assert [(r.group, r.file_line) for r in non_credits] == [
        (InterestRowGroup.DEBIT_INTEREST, _FIRST_LINE + 1),
        (InterestRowGroup.DEBIT_INTEREST, _FIRST_LINE + 5),
    ]


@pytest.mark.parametrize(
    "accrual_description",
    [
        "USD Interest Accrual for Apr-2025",
        "Accrual Reversal",
        "Change in Interest Accrual",
        "USD INTEREST ACCRUAL",
        "accrual posted on 2025-04-30",
    ],
)
@pytest.mark.parametrize("amount", ["1.23", "0", "-1.23"])
def test_accrual_row_is_unproven_whatever_its_sign(accrual_description: str, amount: str) -> None:
    """IBKR's Interest section can carry accrual rows alongside
    realized cash credits. Counting one as a credit could double-count
    interest; dropping one could lose a credit. So any row whose
    description mentions 'accrual' (case-insensitive) is unproven
    (#905), whatever its sign."""
    interest_credits, [row] = parse(
        [make_interest_row(amount=amount, description=accrual_description)]
    )
    assert interest_credits == []
    assert row.group is InterestRowGroup.UNPROVEN_ACCRUAL
    assert row.group.unproven


def test_credit_row_with_accrual_in_description_text_is_unproven() -> None:
    """Even a positive USD row whose description merely MENTIONS
    'accrual' is unproven, not a credit and not dropped: the accrual
    check runs first."""
    interest_credits, [row] = parse(
        [make_interest_row(amount="1.23", description="Credit Interest with accrual adjustment")]
    )
    assert interest_credits == []
    assert row.group is InterestRowGroup.UNPROVEN_ACCRUAL


def test_debit_interest_accrual_adjustment_is_unproven() -> None:
    """The accrual check wins over the debit-interest check, so a
    negative debit-interest accrual adjustment is unproven."""
    _credits, [row] = parse(
        [make_interest_row(amount="-2.00", description="USD Debit Interest accrual adjustment")]
    )
    assert row.group is InterestRowGroup.UNPROVEN_ACCRUAL


def test_non_usd_accrual_row_is_unproven_not_raised() -> None:
    """The USD-only check applies to positive rows that don't mention an
    accrual; an accrual row in another currency is unproven, so the
    parser raises nothing new."""
    _credits, [row] = parse(
        [make_interest_row(amount="1.23", currency="EUR", description="Interest Accrual")]
    )
    assert row.group is InterestRowGroup.UNPROVEN_ACCRUAL


def test_mixed_credit_debit_and_accrual_rows_classify_every_row() -> None:
    """Realistic margin-account + accrual-tracking scenario: the
    Interest section interleaves realized credit, debit, accrual,
    and accrual-change rows. Every row lands in exactly one group."""
    rows = [
        make_interest_row(credit_date="30-04-2025", amount="1.23"),
        make_interest_row(credit_date="30-04-2025", amount="0.05", description="Interest Accrual"),
        make_interest_row(credit_date="31-05-2025", amount="1.45"),
        make_interest_row(
            credit_date="31-05-2025", amount="-0.50", description="USD Debit Interest for May-2025"
        ),
        make_interest_row(
            credit_date="30-06-2025", amount="0.10", description="Change in Interest Accrual"
        ),
        make_interest_row(credit_date="30-06-2025", amount="1.67"),
    ]
    interest_credits, non_credits = parse(rows)
    assert [str(c.amount_gross) for c in interest_credits] == ["1.23", "1.45", "1.67"]
    assert [(r.group, r.file_line) for r in non_credits] == [
        (InterestRowGroup.UNPROVEN_ACCRUAL, _FIRST_LINE + 1),
        (InterestRowGroup.DEBIT_INTEREST, _FIRST_LINE + 3),
        (InterestRowGroup.UNPROVEN_ACCRUAL, _FIRST_LINE + 4),
    ]


def test_credit_row_in_non_usd_currency_still_raises() -> None:
    """Positive (credit) interest in EUR / GBP / etc. is still
    a v1 multi-currency limitation — the parser raises so the user
    knows their non-USD credit isn't being silently swallowed.
    Zero, negative and accrual rows in any currency come back
    classified instead; positive rows in non-USD surface as a
    ParseError pointing at #52."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(amount="1.23", currency="EUR")])
    assert exc.value.column == "Currency"
    assert "USD" in exc.value.message


# ---- credit-interest wording (#1280) --------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# A neutral marker for a label IBKR's wording doesn't have.
_MARKER = "SYNTHQPOST"
_UNKNOWN_LABEL = f"EUR {_MARKER} for Jun-2025"


@pytest.mark.parametrize("month", _MONTHS)
def test_credit_interest_wording_is_a_credit(month: str) -> None:
    """A positive row is a credit only in IBKR's credit-interest wording,
    ``<CCY> Credit Interest for <Mon>-<YYYY>``, for every month."""
    description = f"USD Credit Interest for {month}-2025"
    [credit] = parse_credits([make_interest_row(description=description)])
    assert credit.description == description


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("USD Debit Interest correction for Jun-2025", id="debit-correction"),
        pytest.param("USD SYNTHQPOST for Jun-2025", id="unknown-label"),
        pytest.param("usd credit interest for jun-2025", id="lower-case"),
        pytest.param("USD credit interest for JUN-2025", id="other-case-words"),
        pytest.param("USD Credit Interest for June-2025", id="full-month"),
        pytest.param("USD Credit Interest for 06-2025", id="numeric-month"),
        pytest.param("USD Credit Interest for Jun-25", id="two-digit-year"),
        pytest.param("USD Credit Interest for Jun 2025", id="no-hyphen"),
        pytest.param("USD Credit Interest for Jun-\u0662\u0660\u0662\u0665", id="non-ascii-digits"),
        pytest.param("USD  Credit Interest for Jun-2025", id="double-space"),
        pytest.param(" USD Credit Interest for Jun-2025", id="leading-space"),
        pytest.param("USD Credit Interest for Jun-2025 ", id="trailing-space"),
        pytest.param("USD Credit Interest for Jun-2025\n", id="trailing-newline"),
        pytest.param("USD Credit Interest for Jun-2025 SYNTHQPOST", id="trailing-text"),
        pytest.param("Credit Interest for Jun-2025", id="no-currency"),
        pytest.param("EUR Credit Interest for Jun-2025", id="other-currency"),
        pytest.param("USD Credit Interest", id="no-month"),
    ],
)
def test_positive_row_in_other_wording_is_unproven(description: str) -> None:
    """Any other positive row is unproven (#1280): a refund of debit
    interest isn't interest earned, and a label the parser doesn't know
    could be anything. Near misses of the wording, including one naming a
    currency other than the row's, are unproven too."""
    interest_credits, [row] = parse([make_interest_row(description=description, amount="3.17")])
    assert interest_credits == []
    assert row == InterestNonCredit(
        group=InterestRowGroup.UNPROVEN_POSITIVE,
        amount=Decimal("3.17"),
        currency="USD",
        row_date=date(2025, 4, 30),
        description=description,
        file_line=_FIRST_LINE,
    )
    assert row.group.unproven


def test_non_usd_positive_row_in_other_wording_still_raises() -> None:
    """The v1 USD-only check still covers every positive row whose
    description doesn't mention an accrual, so a non-USD row refuses
    whatever its description, as before #1280."""
    # Not on the raising line: the traceback the check reads prints that line.
    row = make_interest_row(currency="EUR", description=_UNKNOWN_LABEL)
    with pytest.raises(ParseError) as exc:
        parse([row])
    assert (exc.value.column, exc.value.raw_value) == ("Currency", "")
    assert "USD" in exc.value.message
    assert_prints_no_marker(exc.value, markers=(_MARKER, _UNKNOWN_LABEL))


@pytest.mark.parametrize(
    ("description", "marker"),
    [
        ("USD Debit Interest correction for Jun-2025", "correction"),
        ("USD SYNTHQPOST for Jun-2025", "SYNTHQPOST"),
    ],
)
def test_unproven_positive_row_refusal_prints_no_input(description: str, marker: str) -> None:
    """The commands that use interest refuse the row naming only its line
    and reason; none of the row's cells is anywhere in the refusal (#1047)."""
    _credits, non_credits = parse([make_interest_row(description=description, amount="3.17")])

    with pytest.raises(UnprovenInterestError) as exc:
        require_no_unproven_interest(non_credits)

    reason = "a positive amount without IBKR's credit-interest wording"
    assert f"line {_FIRST_LINE} ({reason})" in str(exc.value)
    markers = (description, marker, "Jun-2025", "3.17", "30-04-2025", "2025-04-30")
    assert_prints_no_marker(exc.value, markers=markers)


def test_empty_description_raises_parse_error() -> None:
    """Description is the audit-trail link — empty means a
    malformed row."""
    with pytest.raises(ParseError) as exc:
        parse_credits([make_interest_row(description="")])
    assert exc.value.column == "Description"


# ---- row-index threading --------------------------------------------------


def test_row_index_in_error_matches_source_position() -> None:
    """Multi-row input: the row_index on the ParseError must point
    at the OFFENDING row, not the first row."""
    rows = [
        make_interest_row(credit_date="30-04-2025"),
        make_interest_row(credit_date="31-05-2025"),
        make_interest_row(credit_date="BROKEN"),  # row 2
    ]
    with pytest.raises(ParseError) as exc:
        parse_credits(rows)
    assert exc.value.row_index == 2


def test_source_row_index_skips_over_filtered_footer_rows() -> None:
    """source_row_index reflects the source CSV row position
    (including any skipped footer rows), so a reviewer can map back
    to the exact line in their statement."""
    rows = [
        make_interest_row(credit_date="30-04-2025"),
        total_row("1.23"),
        make_interest_row(credit_date="31-05-2025"),
        total_row("1.23"),
    ]
    parsed, non_credits = parse_block(rows)
    assert (len(parsed), non_credits) == (2, [])
    assert parsed[0].source_row_index == 0
    assert parsed[1].source_row_index == 2


def test_non_credit_file_line_skips_over_filtered_footer_rows() -> None:
    """A non-credit row carries its own file line, not its position
    among the parsed rows, so a footer before it doesn't shift it."""
    rows = [
        make_interest_row(),
        total_row("1.23"),
        make_interest_row(amount="-1.23"),
        total_row("-1.23"),
    ]
    _credits, [row] = parse_block(rows)
    assert row.file_line == _FIRST_LINE + 2


def test_rows_and_lines_of_different_lengths_are_refused() -> None:
    """The parser pairs each row with its file line; a block whose
    two lists disagree is a splitter bug, so it raises a typed error
    rather than a raw ``zip`` ValueError."""
    lineless = Section(
        kind=KnownActivitySection.INTEREST,
        columns=COLUMNS,
        header_line=HEADER_LINE,
        rows=[make_interest_row()],
        row_lines=[],
    )
    with pytest.raises(InvariantError, match="file line"):
        parse_activity_statement_interest(lineless, period=ALL_DATES)
