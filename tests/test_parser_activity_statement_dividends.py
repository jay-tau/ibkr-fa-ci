"""Tests for :mod:`ibkr_fa.parser.activity_statement.dividends`."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import DistributionKind, Dividend
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_dividends,
    parse_statement_period,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.dividends import classify_distribution
from ibkr_fa.types import Isin, Ticker
from tests._activity_statement_rows import make_dividend_row
from tests._income_blocks import block, closed, summary_row, total_row
from tests._private_checks import private_test
from tests._private_data import private_dataset
from tests._statement_period import ALL_DATES

# ---- test fixtures --------------------------------------------------------


def parse(rows: list[dict[str, str]]) -> list[Dividend]:
    """Parse ``rows`` as one Dividends block, closed by their ``Total`` row."""
    return parse_block(closed(rows))


def parse_block(rows: list[dict[str, str]]) -> list[Dividend]:
    """Parse exactly ``rows`` as one Dividends block."""
    return parse_activity_statement_dividends(
        block(KnownActivitySection.DIVIDENDS, rows), period=ALL_DATES
    )


# ---- happy path -----------------------------------------------------------


def test_single_dividend_parsed() -> None:
    [div] = parse([make_dividend_row()])
    assert div.symbol == Ticker("XQAA")
    assert div.isin == Isin("US99990A3095")
    assert div.payment_date == date(2026, 2, 18)
    assert div.amount_gross == Decimal("2.80")
    assert div.currency == "USD"
    assert (
        div.description == "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)"
    )


def test_input_order_preserved() -> None:
    """Dividends are emitted chronologically by IBKR; the parser
    preserves input order so downstream aggregation can do
    chronological audits if needed."""
    rows = [
        make_dividend_row(
            payment_date="01-03-2026", description="A(US99971J1050) Cash Dividend USD 1 per Share"
        ),
        make_dividend_row(
            payment_date="02-03-2026", description="B(US99971J1051) Cash Dividend USD 1 per Share"
        ),
        make_dividend_row(
            payment_date="03-03-2026", description="C(US99971J1052) Cash Dividend USD 1 per Share"
        ),
    ]
    dividends = parse(rows)
    assert [str(d.symbol) for d in dividends] == ["A", "B", "C"]


def test_empty_input_returns_empty_list() -> None:
    """Buy-and-hold accounts with no holdings during dividend
    ex-dates have no Dividends section, but the splitter may also
    emit an empty section. Both produce []."""
    assert parse([]) == []


# ---- footer / summary row handling ----------------------------------------


def test_total_row_is_checked_not_returned() -> None:
    """IBKR prints a per-currency Total row as a 'Data' row with
    Currency='Total'. The parser checks it against the rows above it
    (#908) and returns only the dividends."""
    dividends = parse_block([make_dividend_row(), total_row("2.80")])
    assert len(dividends) == 1
    assert dividends[0].symbol == Ticker("XQAA")


def test_summary_row_is_checked_not_returned() -> None:
    """Multi-currency accounts end a block with a 'Total Dividends in
    <CCY>' summary after the last Total row. The parser checks it
    (#908) and returns only the dividends."""
    rows = [make_dividend_row(), total_row("2.80"), summary_row("Dividends", "USD", "2.80")]
    assert len(parse_block(rows)) == 1


def test_real_dividend_with_missing_date_rejected() -> None:
    """GitHub Codex PR #137 review (P2): the prior heuristic
    skipped EVERY row with an empty Date, but that's broader than
    the intended 'Total Dividends in <CCY>' summary case. A real
    payment row with a missing Date should fail loudly, not be
    silently dropped (which would under-report column 10)."""
    # Real payment shape: SYMBOL(ISIN) Cash Dividend, but Date is
    # accidentally empty (malformed input).
    row = make_dividend_row(payment_date="")
    with pytest.raises(ParseError) as exc:
        parse([row])
    assert exc.value.column == "Date"


# ---- rejection cases ------------------------------------------------------


def test_missing_required_column_raises() -> None:
    """Codex P2 on PR #111 (Trades) parallel: a Dividends section
    whose header is missing a required column would raise raw
    KeyError past the CLI's documented-error catch without the
    explicit pre-check."""
    row = make_dividend_row()
    del row["Amount"]
    with pytest.raises(ParseError) as exc:
        parse([row])
    assert exc.value.column == "Amount"


def test_non_usd_currency_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(currency="EUR")])
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
        parse([make_dividend_row(payment_date=bad_date)])
    assert exc.value.column == "Date"
    assert "DD-MM-YYYY" in exc.value.message


def test_iso_yyyy_mm_dd_date_accepted() -> None:
    """IBKR's Dividends section ships dates as DD-MM-YYYY (IBKR India
    flavor) OR YYYY-MM-DD (IBKR LLC / US flavor). Both must be
    accepted so otherwise-valid statements aren't rejected (shared
    _dates helper)."""
    [dividend] = parse([make_dividend_row(payment_date="2026-02-18")])
    assert dividend.payment_date == date(2026, 2, 18)


def test_impossible_date_rejected() -> None:
    """Feb 30 looks like DD-MM-YYYY but isn't a real date."""
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(payment_date="30-02-2026")])
    assert exc.value.column == "Date"


def test_non_decimal_amount_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(amount="not-a-number")])
    assert exc.value.column == "Amount"


@pytest.mark.parametrize("bad_value", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_amount_rejected(bad_value: str) -> None:
    """Tier-1 PR #136 r2-pattern: NaN / Infinity in Amount column
    would propagate into downstream INR conversion and raise
    raw InvalidOperation past the ParseError contract. Rejected
    up front."""
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(amount=bad_value)])
    assert exc.value.column == "Amount"
    assert "finite" in exc.value.message.lower()


def test_zero_amount_rejected() -> None:
    """Per the model invariant (and IBKR's emission convention):
    Dividends section is positive-only. A zero suggests a
    misclassified accrual reversal."""
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(amount="0")])
    assert exc.value.column == "Amount"
    assert "positive" in exc.value.message.lower()


def test_negative_amount_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(amount="-1.50")])
    assert exc.value.column == "Amount"
    assert "positive" in exc.value.message.lower()


def test_unparseable_description_rejected() -> None:
    """A description that doesn't begin with the required
    SYMBOL(ISIN) prefix is structurally malformed. PR #137 Tier-1
    Finding 4: previously the regex required "Cash Dividend\\b" and
    refused everything else with a pointer to #31 — but #31 is the
    issue this PR closes (cash-dividend parsing). The widened
    regex now accepts any distribution type (Cash, Special, Choice,
    PIL, Return Of Capital, etc.) after the SYMBOL(ISIN) prefix,
    so only structurally-malformed rows raise."""
    rows = [make_dividend_row(description="Some random text without the expected format")]
    with pytest.raises(ParseError) as exc:
        parse(rows)
    assert exc.value.column == "Description"
    assert "SYMBOL(ISIN)" in exc.value.message


@pytest.mark.parametrize(
    "description",
    [
        "XQAA(US99990A3095) Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
        "XQAQ(US99992A1097) Special Dividend USD 1.00 per Share",
        "XQAJ(US99990D1054) Long-Term Capital Gain USD 0.50 per Share",
        "XQDA.B(US99971Q1094) Cash Dividend USD 0.10 per Share",
        "XQKC(US99971T1034) Payment In Lieu Of Dividend USD 0.05 per Share",
        "ABC(US99972H1087) Choice Dividend USD 0.15 per Share",
        "DEF(US99971R1078) Return Of Capital USD 0.25 per Share",
    ],
)
def test_widened_description_accepts_all_distribution_types(description: str) -> None:
    """PR #137 Tier-1 Finding 4: the widened regex accepts any
    IBKR distribution type beginning with the structural
    SYMBOL(ISIN) prefix. Pre-fix, all but Cash Dividend would have
    raised ParseError and blocked the entire pipeline."""
    [div] = parse([make_dividend_row(description=description)])
    # Per-row symbol + ISIN extraction works for all types.
    assert div.description == description


def test_empty_description_rejected() -> None:
    with pytest.raises(ParseError) as exc:
        parse([make_dividend_row(description="")])
    assert exc.value.column == "Description"


def test_description_lowercase_symbol_rejected() -> None:
    """A lowercase 'symbol' isn't a valid ticker shape — refuse so
    we don't silently construct an invalid Ticker."""
    rows = [make_dividend_row(description="xqaa(US99990A3095) Cash Dividend USD 0.35 per Share")]
    with pytest.raises(ParseError) as exc:
        parse(rows)
    assert exc.value.column == "Description"


@pytest.mark.parametrize(
    "description",
    [
        "XQCY-A(US99972D1072) Cash Dividend USD 0.94 per Share (Ordinary Dividend)",
        "XQCZ-B(US99971S1050) Cash Dividend USD 0.35 per Share",
        "XQDA-B(US99971Q1094) Cash Dividend USD 0.10 per Share",
    ],
)
def test_widened_description_accepts_hyphenated_symbols(description: str) -> None:
    """PR #137 Tier-3 (Codex GPT-5.5 high P2): the symbol charset
    now accepts hyphens, exercised here with made-up tickers.
    Pre-fix the regex was ``[A-Z][A-Z0-9.]*``, which
    would reject these and raise ParseError aborting the whole
    FA run on the first such row."""
    [div] = parse([make_dividend_row(description=description)])
    assert div.description == description
    # Symbol contains the hyphen (preserved verbatim from the
    # description, matching trades.py's opaque-symbol convention).
    assert "-" in str(div.symbol)


def test_widened_description_accepts_optional_whitespace_before_isin() -> None:
    """Defense in depth: the regex now tolerates optional whitespace
    between symbol and the ``(`` opening the ISIN group. Pre-fix the
    regex required no whitespace (`[A-Z0-9.]*\\(`). Reality may
    emit ``"SYMBOL (US...)"`` with a separator space."""
    [div] = parse(
        [make_dividend_row(description="XQAA (US99990A3095) Cash Dividend USD 0.35 per Share")]
    )
    assert str(div.symbol) == "XQAA"


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
    """End-to-end against the private FY 25-26 statement: the parsed
    dividends are the section's payment rows read with :mod:`csv`, field
    for field and in file order, and they add up to the section's own
    ``Total`` rows."""
    [sample] = private_dataset("ibkr/activity-statement-fy2025-26.csv")
    sections = split_activity_statement_sections(sample)
    period = parse_statement_period(sections[KnownActivitySection.STATEMENT][0].rows)
    blocks = sections.get(KnownActivitySection.DIVIDENDS, [])
    dividends = [
        d for section in blocks for d in parse_activity_statement_dividends(section, period=period)
    ]
    payments, totals = _csv_section(sample, "Dividends")
    assert [
        (str(d.symbol), str(d.isin), d.payment_date, d.amount_gross, d.description)
        for d in dividends
    ] == [_csv_fields(row) for row in payments]
    assert sum((d.amount_gross for d in dividends), Decimal(0)) == sum(totals, Decimal(0))


# ---- #154 source_row_index threading ------------------------------------


def test_source_row_index_threaded_through_parser() -> None:
    """#154: parser threads the 0-based row index onto each Dividend
    so downstream error messages (notably
    UnrecognisedDistributionTypeError) can point the user at the
    specific source row in their IBKR Activity Statement CSV."""
    rows = [
        make_dividend_row(
            payment_date="01-03-2026",
            description="A(US99971J1050) Cash Dividend USD 1 per Share",
        ),
        make_dividend_row(
            payment_date="02-03-2026",
            description="B(US99971J1051) Cash Dividend USD 1 per Share",
        ),
        make_dividend_row(
            payment_date="03-03-2026",
            description="C(US99971J1052) Cash Dividend USD 1 per Share",
        ),
    ]
    dividends = parse(rows)
    assert [d.source_row_index for d in dividends] == [0, 1, 2]


def test_source_row_index_accounts_for_skipped_total_footer() -> None:
    """#154: the per-currency Total row isn't a dividend. The row index
    threaded onto DOWNSTREAM Dividend records reflects the input
    position (not the post-skip position) so the user can locate the
    row verbatim in their CSV."""
    rows = [
        make_dividend_row(
            payment_date="01-03-2026",
            description="A(US99971J1050) Cash Dividend USD 1 per Share",
        ),
        # Index 1: the first run's Total row.
        total_row("2.80"),
        make_dividend_row(
            payment_date="03-03-2026",
            description="C(US99971J1052) Cash Dividend USD 1 per Share",
        ),
        total_row("2.80"),
    ]
    dividends = parse_block(rows)
    assert [str(d.symbol) for d in dividends] == ["A", "C"]
    # Source indices reflect ORIGINAL row positions (0 and 2, not 0 and 1).
    assert [d.source_row_index for d in dividends] == [0, 2]


# ---- classifier happy paths -----------------------------------------------


@pytest.mark.parametrize(
    ("type_slice", "expected"),
    [
        ("Cash Dividend USD 0.35 per Share", DistributionKind.CASH_DIVIDEND),
        (
            "Cash Dividend USD 0.35 per Share (Ordinary Dividend)",
            DistributionKind.CASH_DIVIDEND,
        ),
        ("Special Dividend USD 1.00 per Share", DistributionKind.SPECIAL_DIVIDEND),
        ("Choice Dividend USD 0.25 per Share", DistributionKind.CHOICE_DIVIDEND),
        ("Return Of Capital USD 0.50 per Share", DistributionKind.RETURN_OF_CAPITAL),
        (
            "Long-Term Capital Gain USD 0.05 per Share",
            DistributionKind.LONG_TERM_CAPITAL_GAIN,
        ),
        (
            "Payment In Lieu Of Dividend USD 0.10 per Share",
            DistributionKind.PAYMENT_IN_LIEU_OF_DIVIDEND,
        ),
        ("Cash in Lieu USD 12.34", DistributionKind.CASH_IN_LIEU),
    ],
)
def test_classify_distribution_known_kinds(type_slice: str, expected: DistributionKind) -> None:
    """Every allow + reject enum member classifies from its canonical
    IBKR type-slice. Locks the spec contract end-to-end."""
    assert classify_distribution(type_slice) == expected


def test_classify_distribution_case_insensitive() -> None:
    """Case folding: a future IBKR casing change (e.g., "CASH DIVIDEND"
    or "cash dividend") must still classify correctly."""
    assert (
        classify_distribution("CASH DIVIDEND USD 0.35 per Share") == DistributionKind.CASH_DIVIDEND
    )
    assert (
        classify_distribution("cash dividend USD 0.35 per Share") == DistributionKind.CASH_DIVIDEND
    )
    assert (
        classify_distribution("rEtUrN oF cApItAl USD 0.50 per Share")
        == DistributionKind.RETURN_OF_CAPITAL
    )


_KNOWN_KINDS = [
    kind
    for kind in DistributionKind
    if kind not in {DistributionKind.UNRECOGNISED, DistributionKind.MALFORMED}
]


@pytest.mark.parametrize("kind", _KNOWN_KINDS, ids=lambda kind: kind.name)
@pytest.mark.parametrize(
    "payload",
    [
        "0.25 per Share",
        "0.25 per Share (Ordinary Dividend)",
        "0.123456 per Share",
        "3 per Share",
    ],
)
def test_classify_distribution_accepts_every_label_in_the_grammar(
    kind: DistributionKind, payload: str
) -> None:
    """#1096: every kind label classifies when the whole slice is
    ``<label> USD <amount> per Share[ (Ordinary Dividend)]``."""
    assert classify_distribution(f"{kind.value} USD {payload}") == kind


# ---- classifier failure modes ---------------------------------------------


def test_classify_distribution_missing_usd_marker_returns_malformed() -> None:
    """A type-slice without the ``" USD "`` marker classifies as
    :attr:`MALFORMED` so the compute layer raises ``missing_usd_marker``."""
    assert classify_distribution("Cash Dividend") == DistributionKind.MALFORMED
    assert classify_distribution("") == DistributionKind.MALFORMED
    assert classify_distribution("Cash Dividend EUR 0.35 per Share") == DistributionKind.MALFORMED


@pytest.mark.parametrize(
    "type_slice",
    [
        "Cash Dividend USD nonsense",
        "Cash Dividend USD -5 per Share (New Adjustment)",
        "Cash Dividend USD 0.25 per Share (Return of Capital)",
        "Cash Dividend USD 0.25 per Share - US Tax",
        "Special Dividend USD  per Share",
        "Cash Dividend USD 0.25 USD 9 per Share",
        "Cash Dividend USD -5 per Share",
        "Cash Dividend USD +5 per Share",
        "Cash Dividend USD 0.25 per Share (Synthetic Holder Two)",
        "Cash Dividend USD 0.25 per Share Synthetic Marker Q",
        "Cash in Lieu USD 12.34 (Ordinary Dividend)",
    ],
)
def test_classify_distribution_known_label_outside_the_grammar_returns_malformed(
    type_slice: str,
) -> None:
    """#1096: a known label no longer classifies on its own. Another
    qualifier, a suffix, a second amount, a sign or a footnote after
    ``" USD "`` makes the slice :attr:`MALFORMED`."""
    assert classify_distribution(type_slice) == DistributionKind.MALFORMED


def test_classify_distribution_unknown_keyword_returns_unrecognised() -> None:
    """A well-formed type-slice with an unknown kind keyword
    classifies as :attr:`UNRECOGNISED` so the compute layer raises
    ``unsupported_kind`` and the user adds the kind to the allow or
    reject list."""
    assert (
        classify_distribution("Mystery Distribution USD 0.10 per Share")
        == DistributionKind.UNRECOGNISED
    )
    assert classify_distribution("Unknown Type USD 1.00") == DistributionKind.UNRECOGNISED
    # #1096: an unknown label stays UNRECOGNISED whatever follows the marker.
    assert (
        classify_distribution("Mystery Distribution USD 0.10 per Share (Synthetic Marker Q)")
        == DistributionKind.UNRECOGNISED
    )


def test_classify_distribution_substring_collision_with_allow_kind_rejected() -> None:
    """A keyword that STARTS WITH but doesn't EQUAL an allow-listed
    kind (e.g., ``"Cash Dividend In Lieu Of Stock"``) must NOT be
    silently accepted as the allow-list entry. The classifier uses
    exact-match (after case-fold), not prefix-match. T5-r20 risk-
    equivalent: a novel IBKR label whose first words collide with an
    allow-list entry would corrupt the Schedule OS aggregate."""
    assert (
        classify_distribution("Cash Dividend In Lieu Of Stock USD 0.10 per Share")
        == DistributionKind.UNRECOGNISED
    )


def test_classify_distribution_does_not_falsely_match_failure_marker_strings() -> None:
    """Defence-in-depth: a hostile or corrupted statement that emits
    a description with the literal string ``"<unrecognised>"`` or
    ``"<malformed>"`` as its kind keyword MUST classify as
    UNRECOGNISED, NOT short-circuit to the failure marker enum
    members directly. The failure markers are excluded from the
    case-fold lookup map at module-import time."""
    assert (
        classify_distribution("<unrecognised> USD 0.10 per Share") == DistributionKind.UNRECOGNISED
    )
    assert classify_distribution("<malformed> USD 0.10 per Share") == DistributionKind.UNRECOGNISED


def test_classify_distribution_never_raises() -> None:
    """The classifier is a total function over :class:`str` — it
    NEVER raises. Adversarial / corrupted / empty inputs all resolve
    to :attr:`UNRECOGNISED` or :attr:`MALFORMED`. Locks the
    no-raise contract so the parser can call this without a
    try/except wrapper."""
    # Various edge cases that could plausibly trip a regex-based
    # implementation but must NOT raise.
    for problematic in ("", " ", "\n", "USD", " USD ", "\x00bad", "USD\x00"):
        kind = classify_distribution(problematic)
        assert kind in (DistributionKind.MALFORMED, DistributionKind.UNRECOGNISED)
