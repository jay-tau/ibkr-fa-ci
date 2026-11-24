"""Parse the Morgan Stanley StockPlan "IN - Dividend Report" .xlsx into the
shared :class:`~ibkr_fa.models.Dividend` + :class:`~ibkr_fa.models.WithholdingTax`
models (#365).

The report is a paired ledger (one sheet, ``IN - Dividend Report``): each
dividend is a ``Dividend Paid`` row (positive ``Cash``) followed by an
``IRS Nonresident Alien Withholding`` row (negative ``Cash``). All amounts are
USD (the report's own footer states so). We extract only the gross dividend and
the signed withholding; downstream FX (Rule 115) and the Schedule OS / FSI / TR
/ Form 67 compute are the shared engines — this parser writes no tax logic.

Fail-closed ("it's taxes — never be wrong"): the exact header row must be
present; a ``Dividend Paid`` must be strictly positive and an
``IRS Nonresident Alien Withholding`` strictly negative; a data row for a
different ``Symbol`` than configured, or with an ``Activity`` we don't
recognise, raises rather than being silently dropped; a report not run strictly
after its reporting period ends refuses, and so does a row whose ``Entry Date``
falls outside that period (inclusive). Non-data rows (the title, the "Unable
to get quote…" quote-history errors, and the currency footer) are skipped — they
carry no dividend figure.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal
from pathlib import Path

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import Dividend, WithholdingTax
from ibkr_fa.models.distribution_kind import DistributionKind
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.parser.stockplan import _xlsx
from ibkr_fa.types import Isin, Ticker

_SHEET = "IN - Dividend Report"
_REQUIRED_SHEETS = frozenset({_SHEET})
_OPTIONAL_SHEETS = frozenset({"Quote History"})
# The title row states the report's coverage, e.g. "... for reporting period
# 01-Jan-1900 to 21-Jun-2026". The pipeline requires this to span the FY so a
# truncated report can't silently drop an in-FY dividend (never-be-wrong). Each
# date token is digit-bounded so a malformed year like "20260" fails to match
# (and is refused) rather than truncating to "2026".
_DATE = r"(?<!\d)\d{2}-[A-Za-z]{3}-\d{4}(?!\d)"
_TITLE_RE = re.compile(
    rf".+ - IN - Dividend Report \(report run on (?P<run_on>{_DATE}) "
    rf"for reporting period (?P<start>{_DATE}) to (?P<end>{_DATE})\)"
)
_HEADER: tuple[str, ...] = (
    "Entry Date",
    "Activity",
    "Cash",
    "Symbol",
    "Number of Units or Shares",
    "Unit or Share Price",
    "Book Value",
)
_ACTIVITY_DIVIDEND = "Dividend Paid"
_ACTIVITY_WITHHOLDING = "IRS Nonresident Alien Withholding"
_KNOWN_ACTIVITIES = frozenset({_ACTIVITY_DIVIDEND, _ACTIVITY_WITHHOLDING})
_NO_RECORDS = "No records found"


def _parse_coverage(title: str, path: Path) -> tuple[_dt.date, _dt.date]:
    """The ``(start, end)`` coverage in the title's closing clause, or refuse.

    Only the closing ``(report run on … for reporting period … to …)`` counts:
    a date in the title's prefix is never used. A date that isn't a real date
    refuses. The report must also be run strictly after the period ends, since
    it can't hold a dividend paid after its run (#682).
    """
    match = _TITLE_RE.fullmatch(title)
    assert match is not None  # title was validated immediately before this call
    run_on, start, end = (
        _xlsx.title_date(match.group(name), section=_SHEET, path=path, report="Dividend Report")
        for name in ("run_on", "start", "end")
    )
    _xlsx.require_run_after_coverage(run_on, end, section=_SHEET, path=path, missed="dividends")
    return start, end


def _require_in_coverage(
    payment_date: _dt.date, coverage: tuple[_dt.date, _dt.date], *, offset: int, activity: str
) -> None:
    """Refuse a row dated outside the title's reporting period (inclusive).

    The title and the rows would contradict each other, so neither can be
    trusted to say which dividends the report holds (#912).
    """
    start, end = coverage
    if not start <= payment_date <= end:
        raise ParseError(
            section=_SHEET,
            row_index=offset,
            column="Entry Date",
            message=f"'{activity}' is dated outside the report's stated reporting period",
            raw_value="",
        )


def _refuse_duplicate(
    payment_date: _dt.date, seen: set[_dt.date], *, offset: int, activity: str
) -> None:
    """Refuse a second row for ``payment_date`` (a doubled figure), else record it."""
    if payment_date in seen:
        raise ParseError(
            section=_SHEET,
            row_index=offset,
            column="Entry Date",
            message=f"duplicate '{activity}' for an Entry Date an earlier row already has",
            raw_value="",
        )
    seen.add(payment_date)


def _consume_no_records(row: tuple[object, ...], *, row_index: int, already_seen: bool) -> bool:
    """Validate and consume an exact zero-activity sentinel row."""
    if _xlsx.cell(row, 0) != _NO_RECORDS:
        return False
    if any(_xlsx.cell(row, i) not in (None, "") for i in range(1, len(row))):
        raise ParseError(
            section=_SHEET,
            row_index=row_index,
            column=None,
            message=f"{_NO_RECORDS!r} row also carries data",
            raw_value="",
        )
    if already_seen:
        raise ParseError(
            section=_SHEET,
            row_index=row_index,
            column=None,
            message=f"duplicate {_NO_RECORDS!r} row",
            raw_value="",
        )
    return True


def _is_quote_notice(text: str, patterns: tuple[re.Pattern[str], ...], *, row_index: int) -> bool:
    """Whether ``text`` is one of the quote ``patterns``' notices.

    A notice's fallback price is matched by shape but never kept; it must
    still be within a unit price's read-time digit limits (#1276).
    """
    for pattern in patterns:
        notice = pattern.fullmatch(text)
        if notice is None:
            continue
        price = notice.groupdict().get("price")
        breach = None if price is None else number_limit_breach(Decimal(price), NumberKind.PRICE)
        if breach is not None:
            raise ParseError(
                section=_SHEET, row_index=row_index, column=None, message=breach, raw_value=""
            )
        return True
    return False


def _validate_auxiliary_fields(row: tuple[object, ...], *, row_index: int) -> None:
    """Validate known StockPlan columns that do not feed the tax models."""
    for index, column in (
        (4, "Number of Units or Shares"),
        (5, "Unit or Share Price"),
    ):
        value = _xlsx.cell(row, index)
        if value not in (None, ""):
            raise ParseError(
                section=_SHEET,
                row_index=row_index,
                column=column,
                message=f"{column} is populated but its StockPlan semantics are unsupported",
                raw_value="",
            )
    book_value = _xlsx.to_decimal(
        _xlsx.cell(row, 6),
        section=_SHEET,
        row_index=row_index,
        column="Book Value",
        kind=NumberKind.MONEY,
    )
    if book_value != 0:
        raise ParseError(
            section=_SHEET,
            row_index=row_index,
            column="Book Value",
            message="Book Value must be zero for the validated StockPlan dividend schema",
            raw_value="",
        )


def _require_activity_evidence(
    *,
    path: Path,
    header_row: int,
    saw_no_records: bool,
    dividends: list[Dividend],
    withholdings: list[WithholdingTax],
) -> None:
    if saw_no_records and (dividends or withholdings):
        raise ParseError(
            section=_SHEET,
            row_index=header_row,
            column=None,
            message=f"report has both {_NO_RECORDS!r} and dividend/withholding rows",
            raw_value="",
        )
    if not saw_no_records and not dividends and not withholdings:
        raise ParseError(
            section=_SHEET,
            row_index=header_row,
            column=None,
            message=(
                f"{path.name} has no dividend/withholding rows and no {_NO_RECORDS!r} sentinel; "
                "cannot prove zero activity"
            ),
            raw_value="",
        )


def parse_stockplan_dividend_report(
    path: Path, *, symbol: Ticker, isin: Isin
) -> tuple[list[Dividend], list[WithholdingTax], tuple[_dt.date, _dt.date]]:
    """Parse ``path`` into ``(dividends, withholdings, coverage)`` for the security.

    Args:
        path: The "IN - Dividend Report" .xlsx.
        symbol: The configured trading symbol (e.g. ``Ticker("GOOG")``); every
            data row's ``Symbol`` must match it.
        isin: The configured ISIN, stamped onto every produced model.

    Returns:
        The parsed dividends, withholdings, and the report's ``(start, end)``
        coverage dates (from the title row) so the caller can enforce that the
        report spans the financial year.

    Raises:
        ParseError: Missing/renamed sheet or header, a title date that isn't a
            real date, a report not run strictly after its coverage ends, a row whose
            ``Entry Date`` falls outside the coverage (inclusive), a symbol
            mismatch, an unrecognised activity, or a wrong-signed
            dividend/withholding. Its refusals carry no cell value: they name
            the sheet and row, the column when applicable, and the rule broken,
            in fixed text.
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
        description="StockPlan dividend-report title with reporting period",
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
    coverage = _parse_coverage(title, path)
    fund = rf"\[\.{re.escape(str(symbol))}\.[A-Za-z0-9_-]+\.\]"
    quote_errors = (
        re.compile(
            rf"Unable to get quote as at \d{{4}}/\d{{2}}/\d{{2}}, "
            rf"funds = \([^)]*{fund}[^)]*\),\s*"
        ),
        re.compile(
            rf"Unable to retrieve market price for fund: {fund} on date: "
            rf"\d{{4}}/\d{{2}}/\d{{2}}\. Returned \$(?P<price>\d+(?:\.\d+)?) USD instead\."
        ),
    )

    dividends: list[Dividend] = []
    withholdings: list[WithholdingTax] = []
    # This report lists one cash dividend (and one NRA withholding) per payment
    # date. A second row for the same date is a corrupt/double-exported report:
    # the downstream orphan-WHT guard keys on the (ISIN, date) SET so it can't
    # see a duplicate, and both rows would SUM — silently doubling income or the
    # FTC. Refuse rather than file a doubled figure (never-be-wrong).
    div_dates: set[_dt.date] = set()
    wht_dates: set[_dt.date] = set()
    saw_no_records = False
    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
        if _xlsx.is_blank_row(row):
            continue
        if _consume_no_records(row, row_index=offset, already_seen=saw_no_records):
            saw_no_records = True
            continue
        text = _xlsx.single_text(row)
        if text == _xlsx.USD_STATEMENT or (
            text is not None and _is_quote_notice(text, quote_errors, row_index=offset)
        ):
            continue
        _xlsx.require_no_trailing_values(row, len(_HEADER), section=_SHEET, row_index=offset)
        if not _xlsx.is_data_row(row):
            _xlsx.refuse_unknown_row(section=_SHEET, row_index=offset)
        _validate_auxiliary_fields(row, row_index=offset)
        activity_cell = _xlsx.cell(row, 1)
        activity = str(activity_cell).strip() if activity_cell is not None else ""
        if activity not in _KNOWN_ACTIVITIES:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Activity",
                message=(
                    "unrecognised dividend-report Activity; expected one of "
                    f"{sorted(_KNOWN_ACTIVITIES)}"
                ),
                raw_value="",
            )
        symbol_cell = _xlsx.cell(row, 3)
        row_symbol = str(symbol_cell).strip() if symbol_cell is not None else ""
        if row_symbol != symbol:
            raise ParseError(
                section=_SHEET,
                row_index=offset,
                column="Symbol",
                message="row Symbol does not match the configured [stockplan.security] symbol",
                raw_value="",
            )
        cash = _xlsx.to_decimal(
            _xlsx.cell(row, 2),
            section=_SHEET,
            row_index=offset,
            column="Cash",
            kind=NumberKind.MONEY,
        )
        payment_date = _xlsx.to_date(
            _xlsx.cell(row, 0), section=_SHEET, row_index=offset, column="Entry Date"
        )
        _require_in_coverage(payment_date, coverage, offset=offset, activity=activity)
        if activity == _ACTIVITY_DIVIDEND:
            if cash <= 0:
                raise ParseError(
                    section=_SHEET,
                    row_index=offset,
                    column="Cash",
                    message=f"'{_ACTIVITY_DIVIDEND}' must be a positive gross amount",
                    raw_value="",
                )
            _refuse_duplicate(payment_date, div_dates, offset=offset, activity=_ACTIVITY_DIVIDEND)
            dividends.append(
                Dividend(
                    symbol=symbol,
                    isin=isin,
                    payment_date=payment_date,
                    amount_gross=cash,
                    currency="USD",
                    description=f"{symbol}({isin}) Cash Dividend",
                    kind=DistributionKind.CASH_DIVIDEND,
                )
            )
        else:  # _ACTIVITY_WITHHOLDING
            if cash >= 0:
                raise ParseError(
                    section=_SHEET,
                    row_index=offset,
                    column="Cash",
                    message=f"'{_ACTIVITY_WITHHOLDING}' must be a negative (withheld) amount",
                    raw_value="",
                )
            _refuse_duplicate(
                payment_date, wht_dates, offset=offset, activity=_ACTIVITY_WITHHOLDING
            )
            withholdings.append(
                WithholdingTax(
                    symbol=symbol,
                    isin=isin,
                    payment_date=payment_date,
                    amount=cash,
                    currency="USD",
                    description=f"{symbol}({isin}) Withholding Tax",
                )
            )
    _xlsx.require_usd_currency(rows, path, section=_SHEET)
    _require_activity_evidence(
        path=path,
        header_row=header_row,
        saw_no_records=saw_no_records,
        dividends=dividends,
        withholdings=withholdings,
    )
    return dividends, withholdings, coverage
