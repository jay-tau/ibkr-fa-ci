"""Tests for :mod:`ibkr_fa.parser.activity_statement._totals` (#908).

The Dividends, Withholding Tax and Interest parsers validate every printed
``Total`` and ``Total <Section> in <CCY>`` row and check sums when no FX is
needed, so a row lost from an edited or cut-short statement refuses instead
of shrinking the income every schedule files.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_withholding_tax,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement._totals import PrintedTotals
from ibkr_fa.parser.activity_statement.multi_section import Section
from tests._crash_reports import assert_unchained_refusal
from tests._income_blocks import COLUMNS, HEADER_LINE, block, summary_row, total_row
from tests._number_limits import MONEY
from tests._statement_period import ALL_DATES

_FIXTURES = Path(__file__).parent / "fixtures"
_LINE = HEADER_LINE + 1
"""The file line of a test block's first row."""

# Name- and code-shaped cells a currency refusal must not echo (#1027).
_MARKERS = ("Synthetic Holder Two", "U1234567", "ANN")


def _row(kind: KnownActivitySection, amount: str, currency: str = "USD") -> dict[str, str]:
    """One detail row of ``kind``; a positive amount is valid in every section."""
    description = {
        KnownActivitySection.DIVIDENDS: "XQAQ(US99992A1097) Cash Dividend USD 1.00 per Share",
        KnownActivitySection.WITHHOLDING_TAX: (
            "XQAQ(US99992A1097) Cash Dividend USD 1.00 per Share - US Tax"
        ),
        KnownActivitySection.INTEREST: f"{currency} Credit Interest for Jun-2025",
    }[kind]
    return {
        "Currency": currency,
        "Date": "2025-06-30",
        "Description": description,
        "Amount": amount,
    }


def _debit(amount: str, currency: str) -> dict[str, str]:
    """One Interest debit-interest row."""
    return {
        "Currency": currency,
        "Date": "2025-06-30",
        "Description": f"{currency} Debit Interest for Jun-2025",
        "Amount": amount,
    }


# Each section's parser, returning how many records it parsed.
_PARSERS: dict[KnownActivitySection, Callable[[Section], int]] = {
    KnownActivitySection.DIVIDENDS: lambda rows: len(
        parse_activity_statement_dividends(rows, period=ALL_DATES)
    ),
    KnownActivitySection.WITHHOLDING_TAX: lambda rows: len(
        parse_activity_statement_withholding_tax(rows, period=ALL_DATES)
    ),
    KnownActivitySection.INTEREST: lambda rows: sum(
        map(len, parse_activity_statement_interest(rows, period=ALL_DATES))
    ),
}
_KINDS = pytest.mark.parametrize("kind", list(_PARSERS), ids=lambda kind: kind.value)


def _parse(kind: KnownActivitySection, rows: list[dict[str, str]]) -> int:
    """Parse ``rows`` as one block of ``kind``; return how many records it parsed."""
    return _PARSERS[kind](block(kind, rows))


def _refusal(kind: KnownActivitySection, rows: list[dict[str, str]]) -> ParseError:
    with pytest.raises(ParseError) as exc:
        _parse(kind, rows)
    assert exc.value.section == kind.value
    return exc.value


# The modules whose read-time digit limit (#1276) an income row meets, and
# which a test stubs out to reach the exact sum behind it: ``_cells`` parses
# the readers' Amount cells, and the Dividends reader checks its per-share
# amount itself.
_AMOUNT_READERS = (
    "ibkr_fa.parser.activity_statement._totals",
    "ibkr_fa.parser.activity_statement.dividends",
    "ibkr_fa.parser._cells",
)


def _without_digit_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let every income Amount past the read-time digit limits through (#1276).

    ``raising=False`` keeps the stub harmless where the readers have no limit.
    """
    for module in _AMOUNT_READERS:
        monkeypatch.setattr(
            f"{module}.number_limit_breach", lambda _value, _kind: None, raising=False
        )


# ---- per-currency Total rows ----------------------------------------------


@_KINDS
def test_rows_matching_their_total_and_summary_parse(kind: KnownActivitySection) -> None:
    rows = [
        _row(kind, "10.00"),
        _row(kind, "5.00"),
        total_row("15.00"),
        summary_row(kind.value, "USD", "15.00"),
    ]
    assert _parse(kind, rows) == 2


@_KINDS
def test_a_lost_row_refuses_at_the_total(kind: KnownActivitySection) -> None:
    """The issue's example: a 5.00 row was lost, so the rows add up to
    10.00 but IBKR's Total says 15.00. The refusal names the lines it
    compared, not the amounts (#1040)."""
    error = _refusal(kind, [_row(kind, "10.00"), total_row("15.00")])
    assert (error.row_index, error.column, error.raw_value) == (_LINE + 1, "Amount", "")
    assert error.message.startswith(
        f"the 'Total' row on line {_LINE + 1} doesn't equal the sum of the rows on line {_LINE}."
    )
    assert "10.00" not in str(error)
    assert "15.00" not in str(error)


@_KINDS
def test_a_missing_total_names_the_header_and_last_row(kind: KnownActivitySection) -> None:
    error = _refusal(kind, [_row(kind, "10.00"), _row(kind, "5.00")])
    assert error.row_index == _LINE + 1
    assert f"header is on line {HEADER_LINE}" in error.message
    assert f"its last row (line {_LINE + 1})" in error.message


@_KINDS
def test_a_row_after_the_last_total_refuses(kind: KnownActivitySection) -> None:
    error = _refusal(kind, [_row(kind, "10.00"), total_row("10.00"), _row(kind, "5.00")])
    assert f"its last row (line {_LINE + 2})" in error.message


@_KINDS
@pytest.mark.parametrize("runs_before", [0, 1])
def test_a_total_with_no_rows_above_it_refuses(
    kind: KnownActivitySection, runs_before: int
) -> None:
    rows = [*[_row(kind, "10.00"), total_row("10.00")] * runs_before, total_row("0")]
    error = _refusal(kind, rows)
    assert error.row_index == _LINE + 2 * runs_before
    assert "has no rows above it" in error.message


@_KINDS
@pytest.mark.parametrize(("column", "value"), [("Date", "2025-06-30"), ("Description", "Total")])
def test_a_total_with_a_populated_cell_refuses_even_when_it_matches(
    kind: KnownActivitySection, column: str, value: str
) -> None:
    error = _refusal(kind, [_row(kind, "10.00"), {**total_row("10.00"), column: value}])
    assert (error.row_index, error.column) == (_LINE + 1, column)


@_KINDS
@pytest.mark.parametrize(
    "currency",
    ["Total in USD", "TOTAL", " Total", "total", *(f"Total {marker}" for marker in _MARKERS)],
)
def test_a_total_whose_currency_is_not_exactly_total_refuses(
    kind: KnownActivitySection, currency: str
) -> None:
    """IBKR's per-currency Total has Currency 'Total'. A near-miss, such
    as a 'Total in USD' conversion row, isn't a shape the parser can
    check, so it refuses rather than passing as a detail row, without
    echoing the cell (#1027)."""
    error = _refusal(kind, [_row(kind, "10.00"), {**total_row("10.00"), "Currency": currency}])
    assert (error.row_index, error.column, error.raw_value) == (_LINE + 1, "Currency", "")
    assert not any(marker in str(error) for marker in _MARKERS)


@_KINDS
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "", "ten"])
def test_a_total_whose_amount_is_not_a_finite_decimal_refuses(
    kind: KnownActivitySection, amount: str
) -> None:
    error = _refusal(kind, [_row(kind, "10.00"), total_row(amount)])
    assert (error.row_index, error.column) == (_LINE + 1, "Amount")
    assert "finite decimal" in error.message


@_KINDS
@pytest.mark.parametrize(
    ("total", "accepted"),
    [("100000000000000.00000000000000000001", True), ("1E+14", False)],
)
def test_sums_past_the_default_precision_stay_exact(
    kind: KnownActivitySection, total: str, accepted: bool
) -> None:
    """At Decimal's default 28 digits, 1E+14 + 1E-20 (35 digits, each amount
    within the money limit) rounds to 1E+14, so a lost row would go
    unnoticed; the check adds exactly instead."""
    rows = [_row(kind, "1E+14"), _row(kind, "0.00000000000000000001"), total_row(total)]
    if accepted:
        assert _parse(kind, rows) == 2
    else:
        error = _refusal(kind, rows)
        assert error.message.startswith(
            f"the 'Total' row on line {_LINE + 2} doesn't equal the sum of the rows on "
            f"lines {_LINE}-{_LINE + 1}."
        )


@_KINDS
def test_a_sum_past_the_exponent_range_refuses(
    kind: KnownActivitySection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 9E+999999 rows can't be added exactly. Since #1276 the reader
    refuses an Amount that size by its digit limit first
    (:func:`test_an_amount_past_its_digit_limit_refuses_when_read`), so this
    stubs the limit out to reach the sum."""
    _without_digit_limits(monkeypatch)
    error = _refusal(kind, [_row(kind, "9E+999999"), _row(kind, "9E+999999"), total_row("1")])
    assert (error.row_index, error.column) == (_LINE + 1, "Amount")
    assert "too large or too small to add up exactly" in error.message


def test_an_exact_sum_that_runs_out_of_memory_refuses() -> None:
    class UnaddableDecimal(Decimal):
        def __radd__(self, other):
            raise MemoryError("synthetic allocation failure")

    kind = KnownActivitySection.INTEREST
    totals = PrintedTotals(kind.value, block(kind, []))
    with pytest.raises(ParseError) as exc:
        totals.detail("USD", UnaddableDecimal("0.01"), _LINE)
    assert (exc.value.section, exc.value.row_index, exc.value.column) == (
        "Interest",
        _LINE,
        "Amount",
    )
    assert "enough memory" in exc.value.message


@_KINDS
def test_an_exact_sum_below_the_default_minimum_exponent_parses(
    kind: KnownActivitySection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact sum below Decimal's default minimum exponent still adds up.
    Since #1276 the reader refuses an Amount with that many places by its
    digit limit first (the next test), so this stubs the limit out to reach
    the sum."""
    _without_digit_limits(monkeypatch)
    assert _parse(kind, [_row(kind, "1E-1000000"), total_row("1E-1000000")]) == 1


@_KINDS
@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("7242" + "0" * 27, id="31-whole-digits"),
        pytest.param("7242E+999999", id="past-the-exponent-range"),
        pytest.param("7242E-1000000", id="below-the-minimum-exponent"),
    ],
)
def test_an_amount_past_its_digit_limit_refuses_when_read(
    kind: KnownActivitySection, amount: str
) -> None:
    """#1276: the amounts the exact-sum tests above once read refuse when the
    detail row is read, with the money limit and no value, before any sum."""
    rows = [_row(kind, amount), _row(kind, amount), total_row("1")]
    with pytest.raises(ParseError) as exc:
        _parse(kind, rows)
    assert_unchained_refusal(
        exc.value,
        ParseError(section=kind.value, row_index=0, column="Amount", message=MONEY, raw_value=""),
        f"ParseError in section {kind.value!r} at row 0, column 'Amount': {MONEY}",
        markers=("7242",),
    )


@_KINDS
@pytest.mark.parametrize("column", COLUMNS)
def test_a_footer_missing_a_required_column_refuses_before_footer_checks(
    kind: KnownActivitySection, column: str
) -> None:
    total = total_row("10.00")
    del total[column]
    error = _refusal(kind, [_row(kind, "10.00"), total])
    assert (error.row_index, error.column) == (1, column)
    assert "required column" in error.message


@_KINDS
@pytest.mark.parametrize("summary", [False, True])
def test_a_footer_missing_a_declared_cell_refuses(
    kind: KnownActivitySection, summary: bool
) -> None:
    rows = [{**_row(kind, "10.00"), "Code": ""}, total_row("10.00")]
    if summary:
        rows[-1]["Code"] = ""
        rows.append(summary_row(kind.value, "USD", "10.00"))
    with pytest.raises(ParseError) as exc:
        _PARSERS[kind](block(kind, rows, (*COLUMNS, "Code")))
    assert (exc.value.row_index, exc.value.column) == (_LINE + len(rows) - 1, "Code")
    assert "missing its 'Code' cell" in exc.value.message


def test_two_blocks_each_with_its_own_total_parse() -> None:
    sections = split_activity_statement_sections(
        _FIXTURES / "e2e_activity_statement_multi_block_dividends_2025.csv"
    )
    blocks = sections[KnownActivitySection.DIVIDENDS]
    assert [
        len(parse_activity_statement_dividends(block, period=ALL_DATES)) for block in blocks
    ] == [1, 1]


# ---- Interest: every row counts ----------------------------------------------


def test_interest_total_covers_credits_and_debit_interest() -> None:
    rows = [_row(KnownActivitySection.INTEREST, "10.00"), _debit("-2.50", "USD"), total_row("7.50")]
    interest_credits, non_credits = parse_activity_statement_interest(
        block(KnownActivitySection.INTEREST, rows), period=ALL_DATES
    )
    assert (len(interest_credits), len(non_credits)) == (1, 1)


def test_interest_total_over_credits_only_refuses_with_debit_interest_in_the_run() -> None:
    """The Total adds up every row, not only the credits the parser keeps."""
    rows = [
        _row(KnownActivitySection.INTEREST, "10.00"),
        _debit("-2.50", "USD"),
        total_row("10.00"),
    ]
    error = _refusal(KnownActivitySection.INTEREST, rows)
    assert error.message.startswith(
        f"the 'Total' row on line {_LINE + 2} doesn't equal the sum of the rows on "
        f"lines {_LINE}-{_LINE + 1}."
    )


@pytest.mark.parametrize("currency", ["EUR", "ANN"])
def test_interest_run_mixing_currencies_refuses(currency: str) -> None:
    """The refusal doesn't list the run's currencies (#1027)."""
    rows = [
        _debit("-2.50", currency),
        _row(KnownActivitySection.INTEREST, "10.00"),
        total_row("7.50"),
    ]
    error = _refusal(KnownActivitySection.INTEREST, rows)
    assert "are in more than one currency. IBKR prints" in error.message
    assert error.raw_value == ""
    assert currency not in str(error)


def test_interest_row_labelled_as_summary_refuses_even_with_a_date() -> None:
    """A 'Total Interest in USD' row with a Date would otherwise pass as a
    credit of the whole summary amount."""
    summary = {**summary_row("Interest", "USD", "10.00"), "Date": "2025-06-30"}
    rows = [_row(KnownActivitySection.INTEREST, "10.00"), total_row("10.00"), summary]
    error = _refusal(KnownActivitySection.INTEREST, rows)
    assert (error.row_index, error.column) == (_LINE + 2, "Date")


# ---- summary rows ------------------------------------------------------------


def _usd_runs(kind: KnownActivitySection) -> list[dict[str, str]]:
    return [_row(kind, "10.00"), _row(kind, "5.00"), total_row("15.00")]


def _uncheckable_interest_runs() -> list[dict[str, str]]:
    """A non-USD debit-interest run, then a USD credit run: a 'Total
    Interest in USD' summary over them would need FX to check."""
    return [
        _debit("-2.50", "EUR"),
        total_row("-2.50"),
        _row(KnownActivitySection.INTEREST, "10.00"),
        total_row("10.00"),
    ]


_BLOCKS = pytest.mark.parametrize(
    ("kind", "runs"),
    [
        *(pytest.param(kind, _usd_runs(kind), id=kind.value) for kind in _PARSERS),
        pytest.param(
            KnownActivitySection.INTEREST, _uncheckable_interest_runs(), id="Interest-uncheckable"
        ),
    ],
)


def test_summary_over_a_non_usd_run_is_accepted_unchecked() -> None:
    rows = [*_uncheckable_interest_runs(), summary_row("Interest", "USD", "123.45")]
    interest_credits, non_credits = parse_activity_statement_interest(
        block(KnownActivitySection.INTEREST, rows), period=ALL_DATES
    )
    assert (len(interest_credits), len(non_credits)) == (1, 1)


@_KINDS
@pytest.mark.parametrize(("amount", "accepted"), [("15.00", True), ("15.01", False)])
def test_summary_over_usd_totals_is_checked(
    kind: KnownActivitySection, amount: str, accepted: bool
) -> None:
    rows = [*_usd_runs(kind), summary_row(kind.value, "USD", amount)]
    if accepted:
        assert _parse(kind, rows) == 2
    else:
        error = _refusal(kind, rows)
        assert (error.row_index, error.column, error.raw_value) == (_LINE + 3, "Amount", "")
        assert error.message.startswith(
            f"the summary row on line {_LINE + 3} doesn't equal the sum of the 'Total' "
            f"rows on line(s) {_LINE + 2}."
        )


@_BLOCKS
@pytest.mark.parametrize("amount", ["NaN", "sNaN", "-Infinity", ""])
def test_summary_with_a_non_finite_amount_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]], amount: str
) -> None:
    rows = [*runs, summary_row(kind.value, "USD", amount)]
    error = _refusal(kind, rows)
    assert (error.row_index, error.column) == (_LINE + len(runs), "Amount")


@_BLOCKS
@pytest.mark.parametrize(
    "label", ["", "usd", "US", "USDX", "USD ", "U$D", "Synthetic Holder Two", "U1234567"]
)
def test_summary_with_a_missing_or_malformed_currency_label_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]], label: str
) -> None:
    summary = {
        **summary_row(kind.value, "USD", "15.00"),
        "Description": f"Total {kind.value} in {label}",
    }
    error = _refusal(kind, [*runs, summary])
    assert (error.row_index, error.column, error.raw_value) == (
        _LINE + len(runs),
        "Description",
        "",
    )
    assert not any(marker in str(error) for marker in _MARKERS)


@_BLOCKS
def test_summary_labelled_in_another_case_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]]
) -> None:
    summary = {
        **summary_row(kind.value, "USD", "15.00"),
        "Description": f"TOTAL {kind.value.upper()} IN USD",
    }
    error = _refusal(kind, [*runs, summary])
    assert error.column == "Description"


@_BLOCKS
def test_second_summary_refuses(kind: KnownActivitySection, runs: list[dict[str, str]]) -> None:
    summary = summary_row(kind.value, "USD", "15.00")
    error = _refusal(kind, [*runs, summary, summary])
    assert error.row_index == _LINE + len(runs) + 1
    assert f"comes after the summary row on line {_LINE + len(runs)}" in error.message


@_BLOCKS
def test_run_after_the_summary_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]]
) -> None:
    """A summary before the last Total: the run after it refuses at its
    first row."""
    rows = [*runs, summary_row(kind.value, "USD", "15.00"), _row(kind, "5.00"), total_row("5.00")]
    error = _refusal(kind, rows)
    assert error.row_index == _LINE + len(runs) + 1
    assert "comes after the summary row" in error.message


@_KINDS
@pytest.mark.parametrize(
    "row", [{}, {"Currency": "EUR", "Date": "", "Description": "", "Amount": "sNaN"}]
)
def test_a_bad_row_after_the_summary_refuses_before_any_row_check(
    kind: KnownActivitySection, row: dict[str, str]
) -> None:
    rows = [*_usd_runs(kind), summary_row(kind.value, "USD", "15.00"), row]
    error = _refusal(kind, rows)
    assert (error.row_index, error.column) == (_LINE + 4, None)
    assert "comes after the summary row" in error.message


@_BLOCKS
def test_summary_inside_a_run_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]]
) -> None:
    """A summary before the Total of the rows above it."""
    *body, last_total = runs
    error = _refusal(kind, [*body, summary_row(kind.value, "USD", "15.00"), last_total])
    assert error.row_index == _LINE + len(body)
    assert "comes before a 'Total' row" in error.message


@_KINDS
def test_summary_with_no_total_above_it_refuses(kind: KnownActivitySection) -> None:
    error = _refusal(kind, [summary_row(kind.value, "USD", "0")])
    assert error.row_index == _LINE
    assert "has no 'Total' row above it" in error.message


@_BLOCKS
@pytest.mark.parametrize(
    ("label", "cell"),
    [
        ("USD", "EUR"),
        ("USD", "Synthetic Holder Two"),
        ("USD", "U1234567"),
        ("USD", "ANN"),
        ("ANN", "USD"),
    ],
)
def test_summary_whose_currency_cell_differs_from_its_label_refuses(
    kind: KnownActivitySection, runs: list[dict[str, str]], label: str, cell: str
) -> None:
    """Even when the amount matches. Neither currency is echoed (#1027)."""
    summary = {**summary_row(kind.value, label, "15.00"), "Currency": cell}
    error = _refusal(kind, [*runs, summary])
    assert (error.row_index, error.column, error.raw_value) == (
        _LINE + len(runs),
        "Currency",
        "",
    )
    assert label not in str(error)
    assert cell not in str(error)
