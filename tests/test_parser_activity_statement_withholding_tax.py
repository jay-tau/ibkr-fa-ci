"""Tests for :mod:`ibkr_fa.parser.activity_statement.withholding_tax`."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import WithholdingTax
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_withholding_tax,
    parse_statement_period,
    split_activity_statement_sections,
)
from ibkr_fa.types import Isin, Ticker
from tests._activity_statement_rows import make_wht_row
from tests._income_blocks import (
    COLUMNS,
    HEADER_LINE,
    block,
    closed,
    summary_row,
    total_row,
)
from tests._private_checks import private_test
from tests._private_data import private_dataset
from tests._statement_period import ALL_DATES

# ---- test fixtures --------------------------------------------------------


def parse(rows: list[dict[str, str]]) -> list[WithholdingTax]:
    """Parse ``rows`` as one Withholding Tax block, closed by their ``Total`` row."""
    return parse_block(closed(rows))


def parse_block(
    rows: list[dict[str, str]], columns: tuple[str, ...] = COLUMNS
) -> list[WithholdingTax]:
    """Parse exactly ``rows`` as one Withholding Tax block."""
    return parse_activity_statement_withholding_tax(
        block(KnownActivitySection.WITHHOLDING_TAX, rows, columns), period=ALL_DATES
    )


# ---- happy path -----------------------------------------------------------


def test_single_withholding_tax_parsed() -> None:
    [wht] = parse([make_wht_row()])
    assert wht.symbol == Ticker("XQAA")
    assert wht.isin == Isin("US99990A3095")
    assert wht.payment_date == date(2026, 2, 18)
    assert wht.amount == Decimal("-0.70")
    assert wht.currency == "USD"
    assert wht.description == "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax"


def test_input_order_preserved() -> None:
    """WHT rows are emitted chronologically by IBKR; the parser
    preserves input order so downstream pairing with dividend rows
    can do chronological reconciliation."""
    rows = [
        make_wht_row(
            payment_date="01-03-2026",
            description="A(US99971J1050) Cash Dividend USD 1 per Share - US Tax",
        ),
        make_wht_row(
            payment_date="02-03-2026",
            description="B(US99971J1051) Cash Dividend USD 1 per Share - US Tax",
        ),
        make_wht_row(
            payment_date="03-03-2026",
            description="C(US99971J1052) Cash Dividend USD 1 per Share - US Tax",
        ),
    ]
    wht_rows = parse(rows)
    assert [str(w.symbol) for w in wht_rows] == ["A", "B", "C"]


def test_empty_input_returns_empty_list() -> None:
    """Accounts with a W-9 on file at IBKR (US persons) have no
    Withholding Tax section, and accounts with no foreign-
    dividend-paying holdings during the reporting period also
    have no section. Both produce []."""
    assert parse([]) == []


def test_positive_amount_accepted_as_reversal() -> None:
    """IBKR emits POSITIVE amounts for WHT reversals (treaty-rate
    correction, refund, mid-year W-8BEN registration). The parser
    accepts them; pairing them with the originating debit is
    compute-layer responsibility (#33). Distinguishes from the
    Dividends parser which is positive-only."""
    row = make_wht_row(amount="0.70")
    [wht] = parse([row])
    assert wht.amount == Decimal("0.70")


# ---- footer / summary row handling ----------------------------------------


def test_total_row_is_checked_not_returned() -> None:
    """IBKR prints a per-currency Total row as a 'Data' row with
    Currency='Total', as in the Dividends section. The parser checks
    it against the rows above it (#908) and returns only the WHT."""
    out = parse_block([make_wht_row(), total_row("-0.70")])
    assert len(out) == 1
    assert out[0].symbol == Ticker("XQAA")


def test_summary_row_is_checked_not_returned() -> None:
    """Multi-currency accounts end a block with a 'Total Withholding
    Tax in <CCY>' summary after the last Total row. The parser checks
    it (#908) and returns only the WHT."""
    rows = [make_wht_row(), total_row("-0.70"), summary_row("Withholding Tax", "USD", "-0.70")]
    assert len(parse_block(rows)) == 1


def test_total_row_with_a_code_refuses() -> None:
    """The section's Code column is empty on IBKR's Total row; a
    populated one is a shape the parser can't check (#908)."""
    columns = (*COLUMNS, "Code")
    rows = [{**make_wht_row(), "Code": ""}, {**total_row("-0.70"), "Code": "Re"}]
    with pytest.raises(ParseError) as exc:
        parse_block(rows, columns)
    assert (exc.value.row_index, exc.value.column) == (41, "Code")


def test_real_wht_row_with_missing_date_rejected() -> None:
    """Sister to the Dividends-parser dual-check test: a real WHT
    row with a missing Date should fail loudly, not be silently
    dropped (which would under-report Schedule FSI / Form 67
    foreign-tax-paid totals)."""
    row = make_wht_row(payment_date="")
    with pytest.raises(ParseError) as exc:
        parse([row])
    assert exc.value.column == "Date"


# ---- rejection cases ------------------------------------------------------


def test_missing_required_column_raises() -> None:
    """Same KeyError-escapes-CLI-catch concern as the Trades and
    Dividends parsers (Codex P2 on PR #111): without an explicit
    pre-check, a section whose header is missing a required
    column would raise raw KeyError past the documented-error
    catch and produce an exit-1 traceback."""
    row = make_wht_row()
    del row["Amount"]
    with pytest.raises(ParseError) as exc:
        parse([row])
    assert exc.value.column == "Amount"


def test_non_usd_currency_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(currency="EUR")])
    assert exc.value.column == "Currency"
    assert "USD only" in exc.value.message
    assert "issues/52" in exc.value.message


@pytest.mark.parametrize(
    "bad_date",
    [
        "18/02/2026",  # slashes instead of dashes
        "18-2-2026",  # un-padded month
        "february-18-2026",  # textual month
    ],
)
def test_malformed_date_rejected(bad_date: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(payment_date=bad_date)])
    assert exc.value.column == "Date"
    assert "DD-MM-YYYY" in exc.value.message


def test_iso_yyyy_mm_dd_date_accepted() -> None:
    """IBKR's Withholding Tax section ships dates as DD-MM-YYYY (IBKR
    India flavor) OR YYYY-MM-DD (IBKR LLC / US flavor). Both must be
    accepted (shared _dates helper)."""
    [wht] = parse([make_wht_row(payment_date="2026-02-18")])
    assert wht.payment_date == date(2026, 2, 18)


def test_impossible_date_rejected() -> None:
    """Feb 30 looks like DD-MM-YYYY but isn't a real date."""
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(payment_date="30-02-2026")])
    assert exc.value.column == "Date"


def test_non_decimal_amount_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(amount="not-a-number")])
    assert exc.value.column == "Amount"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_amount_rejected(bad_value: str) -> None:
    """Tier-1 PR #136 r2-pattern: NaN / Infinity in Amount column
    would propagate into the per-payment INR conversion and raise
    raw InvalidOperation past the ParseError contract. Rejected
    up front."""
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(amount=bad_value)])
    assert exc.value.column == "Amount"
    assert "finite" in exc.value.message.lower()


def test_zero_amount_rejected() -> None:
    """Key invariant divergence from the Dividends parser: zero is
    rejected (IBKR doesn't emit zero-tax rows), but BOTH signs are
    accepted (negative = paid; positive = reversal)."""
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(amount="0")])
    assert exc.value.column == "Amount"
    assert "non-zero" in exc.value.message.lower()


def test_unparseable_description_rejected() -> None:
    """A description that doesn't begin with the required
    SYMBOL(ISIN) prefix is structurally malformed. Uses the same
    shared helper as the Dividends parser, so the failure path is
    identical."""
    rows = [make_wht_row(description="Some random text without the expected format")]
    with pytest.raises(ParseError) as exc:
        parse(rows)
    assert exc.value.column == "Description"
    assert "SYMBOL(ISIN)" in exc.value.message


def test_empty_description_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_wht_row(description="")])
    assert exc.value.column == "Description"


def test_description_lowercase_symbol_rejected() -> None:
    """Mirror of the Dividends test: lowercase symbol isn't a
    valid ticker shape."""
    rows = [
        make_wht_row(description="xqaa(US99990A3095) Cash Dividend USD 0.35 per Share - US Tax")
    ]
    with pytest.raises(ParseError) as exc:
        parse(rows)
    assert exc.value.column == "Description"


@pytest.mark.parametrize(
    "description",
    [
        "XQCY-A(US99972D1072) Cash Dividend USD 0.94 per Share - US Tax",
        "XQCZ-B(US99971S1050) Cash Dividend USD 0.35 per Share - US Tax",
        "XQDA-B(US99971Q1094) Cash Dividend USD 0.10 per Share - US Tax",
    ],
)
def test_widened_description_accepts_hyphenated_symbols(description: str) -> None:
    """PR #137 Tier-3 widened the shared regex to accept hyphens
    for share-class tickers, exercised here with made-up examples. The WHT parser inherits
    this for free via the shared helper."""
    [wht] = parse([make_wht_row(description=description)])
    assert "-" in str(wht.symbol)


@pytest.mark.parametrize(
    ("suffix", "country"),
    [
        (" - US Tax", "US"),
        (" - NL Tax", "NL"),
        ("", None),
        # Any other ending states no country (#1282 owner decision).
        (" - nl Tax", None),
        (" - USA Tax", None),
        (" - US Tax ", None),
        (" - US tax", None),
        ("- US Tax", None),
        (" -  US Tax", None),
        (" - US Tax - X", None),
    ],
)
def test_stated_tax_country_is_read_from_the_exact_suffix(suffix: str, country: str | None) -> None:
    """#1282: the trailing ``" - XX Tax"`` names the country that took the tax."""
    description = "XQTC(US99995T1028) Cash Dividend USD 0.913 per Share" + suffix
    [wht] = parse([make_wht_row(description=description)])
    assert wht.tax_country_iso2 == country
    assert wht.description == description


def test_each_row_carries_its_file_line() -> None:
    """#1282: a refusal names the row by its statement line."""
    rows = [
        make_wht_row(description="XQTC(US99995T1028) Cash Dividend - NL Tax"),
        make_wht_row(description="XQTC(US99995T1028) Cash Dividend"),
    ]
    parsed = parse(rows)
    assert [row.file_line for row in parsed] == [HEADER_LINE + 1, HEADER_LINE + 2]
    assert [row.tax_country_iso2 for row in parsed] == ["NL", None]


# ---- end-to-end against a private FY 25-26 statement ----------------------


def _csv_section(path: Path, section: str) -> tuple[list[dict[str, str]], list[Decimal]]:
    """``section``'s payment rows and ``Total`` amounts, read with :mod:`csv`.

    Reads ``path`` without the splitter or the parser: each ``Header``
    row names the columns of the ``Data`` rows after it. A ``Data`` row
    whose Currency is ``Total`` is a printed total, one whose Description
    starts with ``Total <section> in`` is a summary, and any other is a
    payment.
    """
    payments: list[dict[str, str]] = []
    totals: list[Decimal] = []
    columns: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for name, kind, *values in (cells for cells in csv.reader(f) if len(cells) > 1):
            if name != section:
                continue
            if kind == "Header":
                columns = values
            elif kind == "Data":
                row = dict(zip(columns, values, strict=False))
                if row["Currency"] == "Total":
                    totals.append(Decimal(row["Amount"]))
                elif not row["Description"].startswith(f"Total {section} in "):
                    payments.append(row)
    return payments, totals


def _csv_fields(row: dict[str, str]) -> tuple[str, str, date, Decimal, str]:
    """A payment row's symbol, ISIN, date, amount and description, read without the parser."""
    symbol, rest = row["Description"].split("(", 1)
    isin = rest.split(")", 1)[0]
    shape = "%Y-%m-%d" if row["Date"][4:5] == "-" else "%d-%m-%Y"
    payment_date = datetime.strptime(row["Date"], shape).date()
    return symbol.strip(), isin, payment_date, Decimal(row["Amount"]), row["Description"]


@private_test
def test_works_against_private_fy2025_26_statement() -> None:
    """End-to-end against the private FY 25-26 statement: the parsed WHT
    rows are the section's payment rows read with :mod:`csv`, field for
    field and in file order, and they add up to the section's own
    ``Total`` rows."""
    [sample] = private_dataset("ibkr/activity-statement-fy2025-26.csv")
    sections = split_activity_statement_sections(sample)
    period = parse_statement_period(sections[KnownActivitySection.STATEMENT][0].rows)
    blocks = sections.get(KnownActivitySection.WITHHOLDING_TAX, [])
    wht_rows = [
        w
        for section in blocks
        for w in parse_activity_statement_withholding_tax(section, period=period)
    ]
    payments, totals = _csv_section(sample, "Withholding Tax")
    assert [
        (str(w.symbol), str(w.isin), w.payment_date, w.amount, w.description) for w in wht_rows
    ] == [_csv_fields(row) for row in payments]
    assert sum((w.amount for w in wht_rows), Decimal(0)) == sum(totals, Decimal(0))


# ---- Code (#913) ------------------------------------------------------------


@pytest.mark.parametrize(("amount", "code"), [("-0.70", ""), ("0.70", ""), ("0.70", "Re")])
def test_code_blank_or_re_on_a_reversal_parses(amount: str, code: str) -> None:
    """#913: a Withholding Tax block with a ``Code`` column still parses.
    ``Re`` marks a reversal, so it may sit only on a positive amount."""
    [wht] = parse([{**make_wht_row(amount=amount), "Code": code}])
    assert wht.amount == Decimal(amount)


@pytest.mark.parametrize(("amount", "code"), [("-0.70", "Re"), ("0.70", "Ca"), ("-0.70", "Re;X")])
def test_any_other_code_refuses(amount: str, code: str) -> None:
    """#913: the parser reads a row by its amount's sign, so ``Re`` on tax
    paid, or any other code, could change the row's meaning unread. The
    error carries no cell value (#1040)."""
    with pytest.raises(ParseError) as exc:
        parse([{**make_wht_row(amount=amount), "Code": code}])
    error = exc.value
    assert (error.section, error.row_index, error.column, error.raw_value) == (
        "Withholding Tax",
        0,
        "Code",
        "",
    )
    assert error.message.startswith("must be blank, or 'Re' on a positive amount")
