"""Activity Statement section refusals echo no cell (#1040).

A column-shifted or malformed row can put the holder's name or account
number in any cell, and errors end up in screenshots and pasted issues. So
a refusal in the section parsers, the shared date and description helpers,
the printed-totals check and the splitter names its section and row or file
line, the column when it concerns one cell or a header column's position
when relevant, and the rule broken, in fixed text with an empty
``raw_value``. Each case pins the exact message and checks that the markers
and its listed values are absent from ``str(error)``. Every row here is synthetic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from ibkr_fa.exceptions import ParseError
from ibkr_fa.parser.activity_statement import (
    KnownActivitySection,
    parse_activity_statement_account_info,
    parse_activity_statement_dividends,
    parse_activity_statement_interest,
    parse_activity_statement_withholding_tax,
    split_activity_statement_sections,
)
from ibkr_fa.parser.activity_statement.multi_section import Section
from ibkr_fa.parser.activity_statement.net_asset_value import parse_net_asset_value
from ibkr_fa.parser.activity_statement.performance_summary import check_cost_adjustments
from ibkr_fa.parser.activity_statement.statement_period import parse_statement_period
from tests._change_in_nav_checks import RE_EXPORT_HINT
from tests._crash_reports import assert_no_markers
from tests._income_blocks import COLUMNS, HEADER_LINE, block, summary_row, total_row
from tests._statement_period import ALL_DATES

_HOLDER = "Synthetic Holder Two"
_ACCOUNT = "U1234567"
_MARKERS = (_HOLDER, _ACCOUNT, "ANN")
_LINE = HEADER_LINE + 1
"""The file line of an income block's first row."""

_DIVIDEND = {
    "Currency": "USD",
    "Date": "2025-06-30",
    "Description": "XQAQ(US99992A1097) Cash Dividend USD 1.00 per Share",
    "Amount": "10.00",
}
_WHT = {
    **_DIVIDEND,
    "Description": "XQAQ(US99992A1097) Cash Dividend USD 1.00 per Share - US Tax",
    "Amount": "-1.50",
}
_INTEREST = {**_DIVIDEND, "Description": "USD Credit Interest for Jun-2025"}
# The cells of the valid rows that identify a security or carry a figure or date.
_ROW_VALUES = ("XQAQ", "US99992A1097", "2025-06-30", "10.00", "1.50", "Jun-2025")

_UNEXPECTED = (
    "Refusing rather than dropping it. If IBKR now emits this, please file an issue at "
    "https://github.com/jay-tau/ibkr-fa/issues naming it."
)
_FULL_YEAR = "January 1, 2025 - December 31, 2025"
_REPEATED_PERIOD = (
    "the Statement section has more than one Period row; refusing to choose one, because "
    "the period decides which events the report covers. Re-export the Activity Statement "
    "as one statement for one period."
)
_DECIMAL = "must be a decimal number"
_FINITE = "must be a finite decimal"
_SHAPE = (
    "description doesn't match the expected 'SYMBOL(ISIN) ...' shape. The parser accepts "
    "any free-form suffix (e.g., 'Cash Dividend USD 0.25 per Share (Ordinary Dividend)', "
    "'... - US Tax') but the structural prefix is required. If you believe this is a "
    "valid IBKR description shape, please file an issue at "
    "https://github.com/jay-tau/ibkr-fa/issues with the redacted description text."
)


def _dividends(*rows: dict[str, str]) -> Callable[[Path], object]:
    return lambda _: parse_activity_statement_dividends(
        block(KnownActivitySection.DIVIDENDS, list(rows)), period=ALL_DATES
    )


def _interest(*rows: dict[str, str]) -> Callable[[Path], object]:
    return lambda _: parse_activity_statement_interest(
        block(KnownActivitySection.INTEREST, list(rows)), period=ALL_DATES
    )


def _wht(*rows: dict[str, str], columns: tuple[str, ...] = COLUMNS) -> Callable[[Path], object]:
    return lambda _: parse_activity_statement_withholding_tax(
        block(KnownActivitySection.WITHHOLDING_TAX, list(rows), columns), period=ALL_DATES
    )


def _field_rows(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"Field Name": name, "Field Value": value} for name, value in pairs]


def _change_in_nav(ending_value: str) -> Callable[[Path], object]:
    rows = _field_rows(("Starting Value", "0"), ("Ending Value", ending_value))
    section = Section(
        kind=KnownActivitySection.CHANGE_IN_NAV,
        columns=("Field Name", "Field Value"),
        header_line=20,
        rows=rows,
        row_lines=[21, 22],
    )
    return lambda _: parse_net_asset_value(None, [section])


def _cost_adjustment(value: str) -> Callable[[Path], object]:
    columns = ("Asset Category", "Symbol", "Cost Adj.", "Realized Total")
    rows = [
        dict(zip(columns, ("Stocks", "XQAQ", "0", "12.25"), strict=True)),
        dict(zip(columns, ("Stocks", "XQAQ", value, "12.25"), strict=True)),
    ]
    section = Section(
        kind=KnownActivitySection.REALIZED_UNREALIZED_PERFORMANCE_SUMMARY,
        columns=columns,
        header_line=4,
        rows=rows,
        row_lines=[5, 6],
    )
    return lambda _: check_cost_adjustments([section])


def _split(*lines: str) -> Callable[[Path], object]:
    """Split a statement whose first two lines are a valid ``Statement`` block."""
    text = "\n".join(
        (
            "Statement,Header,Field Name,Field Value",
            "Statement,Data,Title,Activity Statement",
            *lines,
        )
    )

    def run(tmp_path: Path) -> object:
        path = tmp_path / "statement.csv"
        path.write_text(f"{text}\n", encoding="utf-8")
        return split_activity_statement_sections(path)

    return run


def _period(*values: str) -> Callable[[Path], object]:
    """One Period row per value; the first is the ``Statement`` section's third row (index 2)."""
    rows = _field_rows(
        ("BrokerName", "Interactive Brokers LLC"),
        ("Title", "Activity Statement"),
        *(("Period", value) for value in values),
    )
    return lambda _: parse_statement_period(rows)


@dataclass(frozen=True)
class _Case:
    run: Callable[[Path], object]
    section: str
    row_index: int
    column: str | None
    message: str
    values: tuple[str, ...]
    """Values the case used that must not appear, besides the markers."""


_CASES = {
    # ---- Marker cases: a name reaches the site. ----
    "total-row-empty-cell-marker": _Case(
        _dividends(_DIVIDEND, {**total_row("10.00"), "Date": _HOLDER}),
        "Dividends",
        _LINE + 1,
        "Date",
        f"the 'Total' row on line {_LINE + 1} has a value in its 'Date' cell, which IBKR "
        f"leaves empty. {RE_EXPORT_HINT}",
        _ROW_VALUES,
    ),
    "total-row-amount-marker": _Case(
        _dividends(_DIVIDEND, total_row(_HOLDER)),
        "Dividends",
        _LINE + 1,
        "Amount",
        f"the 'Total' row on line {_LINE + 1} needs a finite decimal Amount.",
        _ROW_VALUES,
    ),
    "dividends-date-marker": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Date": _HOLDER}),
        "Dividends",
        1,
        "Date",
        "must match DD-MM-YYYY or YYYY-MM-DD",
        _ROW_VALUES,
    ),
    "withholding-tax-description-marker": _Case(
        _wht(_WHT, {**_WHT, "Description": _HOLDER}),
        "Withholding Tax",
        1,
        "Description",
        _SHAPE,
        _ROW_VALUES,
    ),
    "dividends-description-marker": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Description": _HOLDER}),
        "Dividends",
        1,
        "Description",
        _SHAPE,
        _ROW_VALUES,
    ),
    "account-information-field-name-marker": _Case(
        lambda _: parse_activity_statement_account_info(
            _field_rows(
                ("Account Type", "Individual"),
                (_HOLDER, _ACCOUNT),
                (_HOLDER, "Joint"),
            )
        ),
        "Account Information",
        2,
        "Field Name",
        "Activity Statement Account Information row repeats an earlier row's Field Name; "
        "refusing to silently overwrite the earlier value. This usually means the export "
        "contains multiple accounts (out of v1 scope) or is malformed.",
        ("Individual", "Joint"),
    ),
    "dividends-amount-marker": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Amount": _HOLDER}),
        "Dividends",
        1,
        "Amount",
        _DECIMAL,
        _ROW_VALUES,
    ),
    "interest-amount-marker": _Case(
        _interest(_INTEREST, {**_INTEREST, "Amount": _HOLDER}),
        "Interest",
        1,
        "Amount",
        _DECIMAL,
        _ROW_VALUES,
    ),
    "withholding-tax-amount-marker": _Case(
        _wht(_WHT, {**_WHT, "Amount": _HOLDER}),
        "Withholding Tax",
        1,
        "Amount",
        _DECIMAL,
        _ROW_VALUES,
    ),
    "withholding-tax-code-marker": _Case(
        _wht({**_WHT, "Code": ""}, {**_WHT, "Code": _HOLDER}, columns=(*COLUMNS, "Code")),
        "Withholding Tax",
        1,
        "Code",
        "must be blank, or 'Re' on a positive amount (a reversal refunds tax). The parser "
        "reads a row by its amount's sign, so any other code could change the row's "
        "meaning unread.",
        _ROW_VALUES,
    ),
    "withholding-tax-code-short-name": _Case(
        _wht({**_WHT, "Code": ""}, {**_WHT, "Code": "ANN"}, columns=(*COLUMNS, "Code")),
        "Withholding Tax",
        1,
        "Code",
        "must be blank, or 'Re' on a positive amount (a reversal refunds tax). The parser "
        "reads a row by its amount's sign, so any other code could change the row's "
        "meaning unread.",
        _ROW_VALUES,
    ),
    "unknown-section-marker": _Case(
        _split(f"{_HOLDER},Header,Symbol,Quantity", f"{_HOLDER},Data,XQAQ,10"),
        "<file>",
        3,
        None,
        "line 3 belongs to a section this tool neither parses nor lists as safe to ignore, "
        f"so it can't tell whether the section changes a filed figure. {_UNEXPECTED}",
        ("XQAQ",),
    ),
    "row-type-marker": _Case(
        _split(f"Statement,{_HOLDER},BrokerName,Interactive Brokers LLC"),
        "Statement",
        3,
        None,
        "line 3 has a row type other than 'Header', 'Data', 'SubTotal' or 'Total', the "
        "only ones this section's rows may have, so the row could hold a figure no parser "
        f"reads. {_UNEXPECTED}",
        (),
    ),
    "repeated-header-column-marker": _Case(
        _split(f"Dividends,Header,Currency,Date,{_HOLDER},Description,Amount,{_HOLDER}"),
        "Dividends",
        3,
        None,
        "section header's column name 6 repeats column name 3 (the first name after "
        "'Header' is 1); refusing because dict(zip(...)) on data rows would silently "
        "overwrite the earlier value. Either the file is malformed or IBKR has changed "
        "this section's schema.",
        (),
    ),
    "undeclared-header-column-marker": _Case(
        _split(f"Dividends,Header,Currency,Date,{_HOLDER},Description,Amount"),
        "Dividends",
        3,
        None,
        "the header on line 3 has a column name 3 (the first name after 'Header' is 1) "
        "that this block shape neither reads nor lists as safe to ignore. The header must "
        "fit one declared block shape, so its values could change a filed figure unseen. "
        f"{_UNEXPECTED}",
        (),
    ),
    "change-in-nav-value-marker": _Case(
        _change_in_nav(_HOLDER), "Change in NAV", 1, "Field Value", _DECIMAL, ()
    ),
    "cost-adjustment-marker": _Case(
        _cost_adjustment(_HOLDER),
        "Realized & Unrealized Performance Summary",
        6,
        "Cost Adj.",
        "line 6 has a 'Cost Adj.' cell that isn't blank or a zero decimal. A non-zero "
        "adjustment changes a holding's cost basis, which the lots built from the Trades "
        "section may not reflect, so the run refuses rather than file an unverified cost.",
        ("XQAQ", "12.25"),
    ),
    "period-shape-marker": _Case(
        _period(_HOLDER),
        "Statement",
        2,
        "Period",
        "the Period doesn't match the expected shape '<Month> <Day>, <Year> - <Month> "
        "<Day>, <Year>' (e.g., 'January 1, 2025 - December 31, 2025').",
        (),
    ),
    "period-month-word": _Case(
        _period("Smarch 1, 2025 - December 31, 2025"),
        "Statement",
        2,
        "Period",
        "the Period contains an unrecognised month name; expected one of ['april', "
        "'august', 'december', 'february', 'january', 'july', 'june', 'march', 'may', "
        "'november', 'october', 'september'].",
        ("Smarch", "December 31", "2025"),
    ),
    "period-repeated-marker": _Case(
        _period(_FULL_YEAR, f"{_HOLDER} Synthetic Marker Q"),
        "Statement",
        3,
        "Period",
        _REPEATED_PERIOD,
        ("Synthetic Marker Q",),
    ),
    # ---- Rule cases: a marker would fail an earlier check. ----
    "total-without-rows": _Case(
        _dividends(total_row("1234567.89")),
        "Dividends",
        _LINE,
        None,
        f"the 'Total' row on line {_LINE} has no rows above it since the previous 'Total' "
        f"row or the block's header. {RE_EXPORT_HINT}",
        ("1234567.89", "1234567"),
    ),
    "total-mismatch": _Case(
        _dividends(
            {**_DIVIDEND, "Amount": "100.00"},
            {**_DIVIDEND, "Amount": "200.00"},
            total_row("1234567.89"),
        ),
        "Dividends",
        _LINE + 2,
        "Amount",
        f"the 'Total' row on line {_LINE + 2} doesn't equal the sum of the rows on lines "
        f"{_LINE}-{_LINE + 1}. A row may be missing or changed. {RE_EXPORT_HINT}",
        ("100.00", "200.00", "300", "1234567.89", "1234567"),
    ),
    "summary-before-total": _Case(
        _dividends(
            {**_DIVIDEND, "Amount": "100.00"},
            summary_row("Dividends", "USD", "1234567.89"),
            total_row("100.00"),
        ),
        "Dividends",
        _LINE + 1,
        None,
        f"the summary row on line {_LINE + 1} comes before a 'Total' row for the rows on "
        f"line {_LINE}. {RE_EXPORT_HINT}",
        ("100.00", "1234567.89", "1234567"),
    ),
    "summary-after-header": _Case(
        _dividends(summary_row("Dividends", "USD", "1234567.89")),
        "Dividends",
        _LINE,
        None,
        f"the summary row on line {_LINE} has no 'Total' row above it. {RE_EXPORT_HINT}",
        ("1234567.89", "1234567"),
    ),
    "summary-mismatch": _Case(
        _dividends(
            {**_DIVIDEND, "Amount": "100.00"},
            total_row("100.00"),
            summary_row("Dividends", "USD", "1234567.89"),
        ),
        "Dividends",
        _LINE + 2,
        "Amount",
        f"the summary row on line {_LINE + 2} doesn't equal the sum of the 'Total' rows on "
        f"line(s) {_LINE + 1}. {RE_EXPORT_HINT}",
        ("100.00", "1234567.89", "1234567"),
    ),
    "dividends-date-not-a-day": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Date": "2025-02-30"}),
        "Dividends",
        1,
        "Date",
        "must be a real calendar date",
        (*_ROW_VALUES, "2025-02-30", "2025", "February"),
    ),
    "dividends-amount-nan": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Amount": "NaN1234567"}),
        "Dividends",
        1,
        "Amount",
        _FINITE,
        (*_ROW_VALUES, "NaN1234567", "1234567"),
    ),
    "interest-amount-nan": _Case(
        _interest(_INTEREST, {**_INTEREST, "Amount": "NaN1234567"}),
        "Interest",
        1,
        "Amount",
        _FINITE,
        (*_ROW_VALUES, "NaN1234567", "1234567"),
    ),
    "withholding-tax-amount-nan": _Case(
        _wht(_WHT, {**_WHT, "Amount": "NaN1234567"}),
        "Withholding Tax",
        1,
        "Amount",
        _FINITE,
        (*_ROW_VALUES, "NaN1234567", "1234567"),
    ),
    "change-in-nav-value-nan": _Case(
        _change_in_nav("NaN1234567"),
        "Change in NAV",
        1,
        "Field Value",
        _FINITE,
        ("NaN1234567", "1234567"),
    ),
    "dividends-amount-negative": _Case(
        _dividends(_DIVIDEND, {**_DIVIDEND, "Amount": "-1234567.89"}),
        "Dividends",
        1,
        "Amount",
        "dividend amount must be positive; reversals are emitted in the Change in Dividend "
        "Accruals section, not here.",
        (*_ROW_VALUES, "1234567.89", "1234567"),
    ),
    "period-not-a-day": _Case(
        _period("February 30, 2025 - December 31, 2025"),
        "Statement",
        2,
        "Period",
        "the Period contains a date that isn't a real calendar date.",
        ("February 30", "December 31", "2025"),
    ),
    "period-reversed": _Case(
        _period("December 31, 2025 - January 1, 2025"),
        "Statement",
        2,
        "Period",
        "the Period's start date is after its end date; refusing to interpret a reverse range.",
        ("December 31", "January 1", "2025-12-31", "2025-01-01", "2025"),
    ),
    "period-repeated-march": _Case(
        _period(_FULL_YEAR, "March 1, 2025 - March 31, 2025"),
        "Statement",
        3,
        "Period",
        _REPEATED_PERIOD,
        ("January", "December", "March", "2025"),
    ),
    "period-repeated-same": _Case(
        _period(_FULL_YEAR, _FULL_YEAR),
        "Statement",
        3,
        "Period",
        _REPEATED_PERIOD,
        ("January", "December", "2025"),
    ),
}


@pytest.mark.parametrize("case", _CASES.values(), ids=_CASES.keys())
def test_refusal_names_its_place_and_rule_without_a_cell(tmp_path: Path, case: _Case) -> None:
    with pytest.raises(ParseError) as exc:
        case.run(tmp_path)
    error = exc.value
    assert (error.section, error.row_index, error.column) == (
        case.section,
        case.row_index,
        case.column,
    )
    assert error.message == case.message
    assert error.raw_value == ""
    assert_no_markers(str(error), *_MARKERS, *case.values)
