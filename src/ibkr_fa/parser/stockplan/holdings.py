"""Parse the Morgan Stanley StockPlan "Vested Share Holdings" .xlsx (#365).

The ``Holdings Report`` sheet is an aggregate of the currently-held position:
one row per fund (a ``Cash - USD`` row plus one row per stock, e.g.
``GOOG - NASDAQ``). We consume the configured stock's row:

* ``Number of Shares`` — total shares held on the report's Selected Date.
* ``Acquisition Value`` — Fair Market Value at vest x total held shares (USD).

The report is a snapshot as of its Selected Date, not its run date: its
``Market Value`` is the market price as at the Selected Date x the held shares.
The ``Quote History`` sheet gives that date, as the ``Quote Date`` of the
price used, so the stock's ``Market Value`` must equal that price x its
``Number of Shares`` exactly (#1290).

This is the fail-closed reconciliation anchor for the Schedule FA
reconstruction: the Releases vests before the Selected Date must reconcile
exactly to these figures, proving no vest lot is missing. Its currency is
confirmed by that reconciliation against the (USD-footer-confirmed) Releases
report, so this sheet carries no currency statement of its own.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser.stockplan import _xlsx
from ibkr_fa.types import Ticker

_SHEET = "Holdings Report"
_QUOTES_SHEET = "Quote History"
_CASH_FUND = "Cash - USD"
_REQUIRED_SHEETS = frozenset({_SHEET})
# Optional while the Holdings Report sheet is read, so that sheet's refusals
# come first; then required, as the Selected Date's source (#1290).
_OPTIONAL_SHEETS = frozenset({_QUOTES_SHEET})
_DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
_TITLE_RE = re.compile(rf".+ - Holdings Report \(report run on (?P<run_on>{_DATE})\)")
_DEFINITION_RE = re.compile(
    r"Market Value Definition = Market Price as at Selected Date \* Total Held Shares"
    r"Acquisition Value Definition = Fair Market Value at Vest/Exercise \* Total Held Shares"
    r"(?:The numbers on this statement reflect post-split values from stock split on "
    r"\d{1,2}/\d{1,2}/\d{4})?"
)
_HEADER: tuple[str, ...] = (
    "Employee Number",
    "Savings Plan Name",
    "Fund Name",
    "Number of Shares",
    "Acquisition Value",
    "Market Value",
)
_QUOTES_TITLE_RE = re.compile(re.escape(_QUOTES_SHEET))
_QUOTES_HEADER: tuple[str, ...] = ("Fund", "Quote Date", "Price")
_UNEXPECTED_FUND = (
    f"expected only {_CASH_FUND!r} and the configured [stockplan.security] symbol and "
    "listing exchange"
)


@dataclass(frozen=True, slots=True)
class StockplanHoldings:
    """The configured stock's aggregate holdings from the ``Holdings Report``.

    Attributes:
        shares: Total shares held on the Selected Date.
        acquisition_value: FMV-at-vest x held shares (USD) — the reconciliation
            anchor for the sum of the held vests' cost bases.
        selected_date: The date the report shows the holdings as of: the
            ``Quote History`` sheet's one ``Quote Date``, on or before the title's
            ``(report run on …)`` date.
    """

    shares: Decimal
    acquisition_value: Decimal
    selected_date: date


def _validate_cash_row(
    shares: Decimal,
    acquisition_value: Decimal,
    market_value: Decimal,
    *,
    row_index: int,
) -> None:
    if shares < 0 or acquisition_value < 0 or market_value < 0:
        raise ParseError(
            section=_SHEET,
            row_index=row_index,
            column="Number of Shares / Acquisition Value / Market Value",
            message="Cash - USD values must not be negative",
            raw_value="",
        )
    if not shares == acquisition_value == market_value:
        raise ParseError(
            section=_SHEET,
            row_index=row_index,
            column="Number of Shares / Acquisition Value / Market Value",
            message="Cash - USD shares, acquisition value, and market value must match",
            raw_value="",
        )


def parse_stockplan_holdings(
    path: Path, *, symbol: Ticker, listing_exchange: str
) -> StockplanHoldings:
    """Parse the ``Holdings Report`` sheet for the configured ``symbol``, or refuse.

    The stock's ``Fund Name`` must be ``"<symbol> - <listing_exchange>"`` (e.g.
    ``"GOOG - NASDAQ"``). Exactly one validated ``Cash - USD`` row is required;
    every other fund refuses. Once that sheet passes, the ``Quote History``
    sheet must give the Selected Date (:func:`_selected_date_and_price`), and
    the stock's ``Market Value`` must equal its quote price x ``Number of
    Shares`` exactly.

    Raises:
        ParseError: Corrupt file / missing sheet or header, a title run date
            that isn't a real date, zero / multiple rows matching ``symbol``
            (an ambiguous holdings report), a missing or malformed ``Quote
            History`` sheet, or a ``Market Value`` that isn't the quote price x
            ``Number of Shares``. Its refusals carry no cell value:
            they name the sheet and row, the column when applicable, and the
            rule broken, in fixed text, and never an employee number.
    """
    rows = _xlsx.load_sheet_rows(
        path,
        _SHEET,
        required_sheets=_REQUIRED_SHEETS,
        optional_sheets=_OPTIONAL_SHEETS,
    )
    title = _xlsx.require_matching_text_row(
        rows,
        0,
        _TITLE_RE,
        section=_SHEET,
        path=path,
        description="StockPlan holdings-report title",
    )
    match = _TITLE_RE.fullmatch(title)
    assert match is not None  # title was validated immediately before this call
    run_on = _xlsx.title_date(
        match.group("run_on"), section=_SHEET, path=path, report="Holdings Report"
    )
    header_row = 1
    _xlsx.require_exact_row(
        rows,
        header_row,
        _HEADER,
        section=_SHEET,
        path=path,
        description="header row",
    )

    # The stock rows' (shares, acquisition value, market value, row index).
    matches: list[tuple[Decimal, Decimal, Decimal, int]] = []
    identity: tuple[str, str] | None = None
    cash_rows = 0
    expected_fund = f"{symbol} - {listing_exchange}"
    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
        if _xlsx.is_blank_row(row):
            continue
        text = _xlsx.single_text(row)
        if text is not None and _DEFINITION_RE.fullmatch(text.replace("\xa0", " ")):
            continue
        _xlsx.require_no_trailing_values(row, len(_HEADER), section=_SHEET, row_index=offset)
        if not _xlsx.is_data_row(row):
            _xlsx.refuse_unknown_row(section=_SHEET, row_index=offset)
        employee_number = str(_xlsx.cell(row, 0) or "").strip()
        plan_name = str(_xlsx.cell(row, 1) or "").strip()
        if not employee_number or not plan_name:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Employee Number / Savings Plan Name",
                message="Holdings row must carry a nonblank employee and plan identity",
                raw_value="",
            )
        row_identity = (employee_number, plan_name)
        if identity is None:
            identity = row_identity
        elif row_identity != identity:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Employee Number / Savings Plan Name",
                message="Holdings rows have inconsistent employee/plan identities",
                raw_value="",
            )

        fund_name = str(_xlsx.cell(row, 2) or "").strip()
        shares = _xlsx.to_decimal(
            _xlsx.cell(row, 3),
            section=_SHEET,
            row_index=offset,
            column="Number of Shares",
            kind=NumberKind.QUANTITY,
        )
        acquisition_value = _xlsx.to_decimal(
            _xlsx.cell(row, 4),
            section=_SHEET,
            row_index=offset,
            column="Acquisition Value",
            kind=NumberKind.MONEY,
        )
        market_value = _xlsx.to_decimal(
            _xlsx.cell(row, 5),
            section=_SHEET,
            row_index=offset,
            column="Market Value",
            kind=NumberKind.MONEY,
        )

        if fund_name == _CASH_FUND:
            cash_rows += 1
            _validate_cash_row(shares, acquisition_value, market_value, row_index=offset)
            continue

        if fund_name != expected_fund:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Fund Name",
                message=f"unexpected holding; {_UNEXPECTED_FUND}",
                raw_value="",
            )
        if shares <= 0 or acquisition_value <= 0 or market_value <= 0:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Number of Shares",
                message=(
                    "the configured stock's holdings must have positive shares, "
                    "acquisition value, and market value"
                ),
                raw_value="",
            )
        matches.append((shares, acquisition_value, market_value, offset))

    if cash_rows != 1:
        raise ParseError(
            section=_SHEET,
            row_index=header_row,
            column="Fund Name",
            message=f"expected exactly one {_CASH_FUND!r} row, found {cash_rows}",
            raw_value="",
        )
    if len(matches) != 1:
        raise ParseError(
            section=_SHEET,
            row_index=header_row,
            column="Fund Name",
            message=(
                f"expected exactly one holdings row for the configured stock, found {len(matches)}"
            ),
            raw_value="",
        )
    [(shares, acquisition_value, market_value, stock_row)] = matches
    selected_date, price = _selected_date_and_price(path, expected_fund, run_on)
    _require_market_value(price, shares, market_value, row_index=stock_row)
    return StockplanHoldings(
        shares=shares, acquisition_value=acquisition_value, selected_date=selected_date
    )


def _require_market_value(
    price: Decimal, shares: Decimal, market_value: Decimal, *, row_index: int
) -> None:
    """Refuse a stock ``market_value`` that isn't ``price`` x ``shares`` exactly,
    which ties the ``Quote History`` sheet's Quote Date to the snapshot."""
    refusal = ParseError(
        section=_SHEET,
        row_index=row_index,
        column="Market Value",
        message=(
            f"Market Value must equal the {_QUOTES_SHEET!r} sheet's Price x Number of "
            "Shares exactly"
        ),
        raw_value="",
    )
    if exactly(operator.mul, price, shares, refusal=lambda _: refusal) != market_value:
        raise refusal


def _selected_date_and_price(path: Path, stock_fund: str, run_on: date) -> tuple[date, Decimal]:
    """The ``Quote History`` sheet's Selected Date and the stock's price, or refuse.

    Row 1 is the title ``Quote History`` and row 2 the header ``Fund``,
    ``Quote Date``, ``Price``. After them, a row is blank or one quote: exactly
    one ``Cash - USD`` quote of 1 and one ``stock_fund`` quote, on the same
    ``Quote Date``, which is the Selected Date. A Selected Date after the
    Holdings Report's ``run_on`` refuses: a report can't show holdings it
    hasn't seen. Refusals are fixed text, like the Holdings sheet's.
    """
    rows = _xlsx.load_sheet_rows(
        path, _QUOTES_SHEET, required_sheets=_REQUIRED_SHEETS | _OPTIONAL_SHEETS
    )
    _xlsx.require_matching_text_row(
        rows, 0, _QUOTES_TITLE_RE, section=_QUOTES_SHEET, path=path, description="title row"
    )
    header_row = 1
    _xlsx.require_exact_row(
        rows,
        header_row,
        _QUOTES_HEADER,
        section=_QUOTES_SHEET,
        path=path,
        description="header row",
    )
    quotes: dict[str, list[tuple[date, Decimal, int]]] = {_CASH_FUND: [], stock_fund: []}
    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
        if _xlsx.is_blank_row(row):
            continue
        _xlsx.require_no_trailing_values(
            row, len(_QUOTES_HEADER), section=_QUOTES_SHEET, row_index=offset
        )
        if not _xlsx.is_data_row(row):
            _xlsx.refuse_unknown_row(section=_QUOTES_SHEET, row_index=offset)
        fund = str(_xlsx.cell(row, 0) or "").strip()
        if fund not in quotes:
            raise ParseError(
                section=_QUOTES_SHEET,
                row_index=offset,
                column="Fund",
                message=f"unexpected quote; {_UNEXPECTED_FUND}",
                raw_value="",
            )
        quote_date = _xlsx.to_date(
            _xlsx.cell(row, 1), section=_QUOTES_SHEET, row_index=offset, column="Quote Date"
        )
        price = _xlsx.to_decimal(
            _xlsx.cell(row, 2),
            section=_QUOTES_SHEET,
            row_index=offset,
            column="Price",
            kind=NumberKind.PRICE,
        )
        quotes[fund].append((quote_date, price, offset))
    if any(len(found) != 1 for found in quotes.values()):
        raise ParseError(
            section=_QUOTES_SHEET,
            row_index=header_row,
            column="Fund",
            message=(
                f"expected exactly one {_CASH_FUND!r} quote and one quote for the configured stock"
            ),
            raw_value="",
        )
    [(cash_date, cash_price, cash_row)] = quotes[_CASH_FUND]
    [(selected_date, price, stock_row)] = quotes[stock_fund]
    if cash_price != 1:
        raise ParseError(
            section=_QUOTES_SHEET,
            row_index=cash_row,
            column="Price",
            message=f"the {_CASH_FUND!r} quote must be 1",
            raw_value="",
        )
    if cash_date != selected_date:
        raise ParseError(
            section=_QUOTES_SHEET,
            row_index=stock_row,
            column="Quote Date",
            message=f"the {_CASH_FUND!r} and stock quotes must share one Quote Date",
            raw_value="",
        )
    if selected_date > run_on:
        raise ParseError(
            section=_QUOTES_SHEET,
            row_index=stock_row,
            column="Quote Date",
            message="the Quote Date must not be after the Holdings Report's run date",
            raw_value="",
        )
    return selected_date, price
