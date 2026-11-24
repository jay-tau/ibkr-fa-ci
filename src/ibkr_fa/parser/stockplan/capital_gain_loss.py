"""Parse the Morgan Stanley StockPlan "IN - Capital Gain/Loss Report" .xlsx (#365).

This workbook holds two sheets we consume for Schedule FA / CG:

* ``Releases Report`` — one row per GSU (RSU) vest: Vest Date, Order Number,
  Price (= Fair Market Value per share at vest, the §49(2AA) cost basis), Quantity
  (GSUs vested), Net Share Proceeds (= shares actually deposited/held after
  sell-to-cover). This is the rawest per-vest ledger for reconstructing the
  Dec-31 Schedule FA holdings.
* ``Capital Gains Report`` — one row per SALE (Sale Date, price, shares, gross
  proceeds, Acquisition Date), or a single "No records found" row when there are
  no sales.

Fail-closed ("it's taxes — never be wrong"): exact headers required; a vest must
be a completed ``Release`` for the configured symbol with positive FMV / shares;
duplicate order numbers, unknown types/statuses, or wrong-signed figures refuse;
a Capital Gains Report not run strictly after its reporting period ends refuses.
A vest, sale or acquisition dated after its own sheet's run date refuses too,
checked once every other check of that sheet has passed (#1176).
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ibkr_fa.exceptions import ParseError
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser.stockplan import _xlsx
from ibkr_fa.types import Ticker

_RELEASES_SHEET = "Releases Report"
_CG_SHEET = "Capital Gains Report"
_REQUIRED_SHEETS = frozenset({_RELEASES_SHEET, _CG_SHEET})
_OPTIONAL_SHEETS = frozenset({"Quote History"})
_DATE = r"\d{2}-[A-Za-z]{3}-\d{4}"
_RELEASES_TITLE_RE = re.compile(rf".+ - Releases Report \(report run on (?P<run_on>{_DATE})\)")
_CG_TITLE_RE = re.compile(
    rf".+ - Capital Gains Report \(report run on (?P<run_on>{_DATE}) "
    rf"for reporting period (?P<start>{_DATE}) to (?P<end>{_DATE})\)"
)

_RELEASES_HEADER: tuple[str, ...] = (
    "Vest Date",
    "Order Number",
    "Plan",
    "Type",
    "Status",
    "Price",
    "Quantity",
    "Net Cash Proceeds",
    "Net Share Proceeds",
    "Tax Payment Method",
)
_CG_HEADER: tuple[str, ...] = (
    "Sale Date",
    "Sale Price Per Share",
    "Number of Shares Sold",
    "Share Class",
    "Total Gross Proceeds",
    "Acquisition Date",
)
_CG_SECTION_ROW: tuple[object, ...] = (
    "Sale Details",
    None,
    None,
    None,
    None,
    "Acquisition Details",
)
_RELEASE_TYPE = "Release"
_COMPLETE_STATUS = "Complete"
_TAX_PAYMENT_METHOD = "Fractional Shares"
_NO_RECORDS = "No records found"


@dataclass(frozen=True, slots=True)
class StockplanVest:
    """One GSU (RSU) vest from the ``Releases Report`` sheet.

    Attributes:
        vest_date: The vesting date (the Schedule FA "date of acquiring interest").
        order_number: The vest's unique order/confirmation number.
        fmv_per_share: Fair Market Value per share at vest (USD) — the cost basis.
        gsus_vested: GSUs vested (gross, before sell-to-cover).
        shares_deposited: Shares actually deposited/held after sell-to-cover.
    """

    vest_date: _dt.date
    order_number: str
    fmv_per_share: Decimal
    gsus_vested: Decimal
    shares_deposited: Decimal


@dataclass(frozen=True, slots=True)
class StockplanSale:
    """One sale from the ``Capital Gains Report`` sheet."""

    sale_date: _dt.date
    price_per_share: Decimal
    shares_sold: Decimal
    share_class: str
    gross_proceeds: Decimal
    acquisition_date: _dt.date


def _require(
    condition: bool, *, sheet: str, row_index: int, column: str | None, message: str
) -> None:
    if not condition:
        raise ParseError(
            section=sheet, row_index=row_index, column=column, message=message, raw_value=""
        )


def _capital_gains_title_dates(title: str, path: Path) -> tuple[_dt.date, _dt.date, _dt.date]:
    """Parse and validate the title's ``(run_on, start, end)`` dates.

    The report must be run strictly after the period ends: one run earlier, or on
    the last day, can miss sales made after it was run (#680).
    """
    match = _CG_TITLE_RE.fullmatch(title)
    assert match is not None  # title was validated immediately before this call
    run_on, start, end = (
        _xlsx.title_date(
            match.group(name), section=_CG_SHEET, path=path, report="Capital Gains report"
        )
        for name in ("run_on", "start", "end")
    )
    _require(
        start <= end,
        sheet=_CG_SHEET,
        row_index=0,
        column=None,
        message="Capital Gains reporting period ends before it starts",
    )
    _xlsx.require_run_after_coverage(run_on, end, section=_CG_SHEET, path=path, missed="sales")
    return run_on, start, end


def parse_stockplan_releases(path: Path, *, symbol: Ticker) -> tuple[list[StockplanVest], _dt.date]:
    """Parse the ``Releases Report`` sheet into vests for the configured ``symbol``,
    and the date in its title's closing ``(report run on …)``.

    Raises:
        ParseError: Corrupt file / missing sheet or header, a title run date
            that isn't a real date, an unconfirmed currency, a non-``Release``
            / non-``Complete`` / wrong-symbol row, a duplicate order number,
            a non-positive FMV / shares figure, or a vest dated after the
            run date. The run date check comes after every other check, so
            it never hides one of their refusals. The same day passes, since
            the title gives no run time; the separate year-end freshness rule
            is the caller's (#1144). Its refusals carry no cell value: they
            name the sheet and row, the column when applicable, and the rule
            broken, in fixed text.
    """
    rows = _xlsx.load_sheet_rows(
        path,
        _RELEASES_SHEET,
        required_sheets=_REQUIRED_SHEETS,
        optional_sheets=_OPTIONAL_SHEETS,
    )
    title = _xlsx.require_matching_text_row(
        rows,
        0,
        _RELEASES_TITLE_RE,
        section=_RELEASES_SHEET,
        path=path,
        description="StockPlan releases-report title",
    )
    match = _RELEASES_TITLE_RE.fullmatch(title)
    assert match is not None  # title was validated immediately before this call
    run_on = _xlsx.title_date(
        match.group("run_on"), section=_RELEASES_SHEET, path=path, report="Releases Report"
    )
    header_row = 1
    _xlsx.require_exact_row(
        rows,
        header_row,
        _RELEASES_HEADER,
        section=_RELEASES_SHEET,
        path=path,
        description="header row",
    )

    vests: list[StockplanVest] = []
    vest_rows: list[int] = []
    seen_orders: set[str] = set()
    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
        if _xlsx.is_blank_row(row):
            continue
        if _xlsx.single_text(row) == _xlsx.USD_STATEMENT:
            continue
        _xlsx.require_no_trailing_values(
            row, len(_RELEASES_HEADER), section=_RELEASES_SHEET, row_index=offset
        )
        if not _xlsx.is_data_row(row):
            _xlsx.refuse_unknown_row(section=_RELEASES_SHEET, row_index=offset)
        order_number = str(_xlsx.cell(row, 1) or "").strip()
        _require(
            bool(order_number),
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Order Number",
            message="Release row has a blank Order Number",
        )
        row_type = str(_xlsx.cell(row, 3) or "").strip()
        _require(
            row_type == _RELEASE_TYPE,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Type",
            message=f"unexpected row Type; only {_RELEASE_TYPE!r} rows are supported",
        )
        status = str(_xlsx.cell(row, 4) or "").strip()
        _require(
            status == _COMPLETE_STATUS,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Status",
            message=f"vest Status is not {_COMPLETE_STATUS!r}",
        )
        plan = str(_xlsx.cell(row, 2) or "").strip()
        _require(
            plan == symbol,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Plan",
            message="row Plan does not match the configured [stockplan.security] symbol",
        )
        _require(
            order_number not in seen_orders,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Order Number",
            message="duplicate vest Order Number",
        )
        seen_orders.add(order_number)

        vest_date = _xlsx.to_date(
            _xlsx.cell(row, 0), section=_RELEASES_SHEET, row_index=offset, column="Vest Date"
        )
        fmv = _xlsx.to_decimal(
            _xlsx.cell(row, 5),
            section=_RELEASES_SHEET,
            row_index=offset,
            column="Price",
            kind=NumberKind.PRICE,
        )
        gsus = _xlsx.to_decimal(
            _xlsx.cell(row, 6),
            section=_RELEASES_SHEET,
            row_index=offset,
            column="Quantity",
            kind=NumberKind.QUANTITY,
        )
        deposited = _xlsx.to_decimal(
            _xlsx.cell(row, 8),
            section=_RELEASES_SHEET,
            row_index=offset,
            column="Net Share Proceeds",
            kind=NumberKind.QUANTITY,
        )
        net_cash = _xlsx.to_decimal(
            _xlsx.cell(row, 7),
            section=_RELEASES_SHEET,
            row_index=offset,
            column="Net Cash Proceeds",
            kind=NumberKind.MONEY,
        )
        tax_method = str(_xlsx.cell(row, 9) or "").strip()
        _require(
            net_cash == 0,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Net Cash Proceeds",
            message="non-zero release cash is unsupported",
        )
        _require(
            tax_method == _TAX_PAYMENT_METHOD,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Tax Payment Method",
            message=f"unexpected Tax Payment Method; only {_TAX_PAYMENT_METHOD!r} is validated",
        )
        _require(
            fmv > 0,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Price",
            message="Fair Market Value (Price) must be positive",
        )
        _require(
            deposited > 0,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Net Share Proceeds",
            message="Net Share Proceeds (shares deposited) must be positive",
        )
        _require(
            0 < deposited <= gsus,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Net Share Proceeds",
            message="shares deposited must be > 0 and <= GSUs vested",
        )
        vests.append(
            StockplanVest(
                vest_date=vest_date,
                order_number=order_number,
                fmv_per_share=fmv,
                gsus_vested=gsus,
                shares_deposited=deposited,
            )
        )
        vest_rows.append(offset)
    _xlsx.require_usd_currency(rows, path, section=_RELEASES_SHEET)
    for offset, vest in zip(vest_rows, vests, strict=True):
        _require(
            vest.vest_date <= run_on,
            sheet=_RELEASES_SHEET,
            row_index=offset,
            column="Vest Date",
            message="completed vest date must not be after this sheet's report run date",
        )
    return vests, run_on


def parse_stockplan_capital_gains_report(
    path: Path, *, symbol: Ticker
) -> tuple[list[StockplanSale], tuple[_dt.date, _dt.date]]:
    """Parse sales and the title-stated reporting-period coverage.

    ``Share Class`` must match the configured trading symbol.

    Raises:
        ParseError: Corrupt file / missing sheet or header, an unconfirmed
            currency, a wrong-symbol or impossible sale row, unproven zero
            sales, a reporting period that is malformed, inverted or not
            over before the report was run, or a sale or acquisition dated
            after the run date. The run date check comes after every other
            check, so it never hides one of their refusals; a row's sale date
            is checked before its acquisition date. The same day passes, since
            the title gives no run time. Its refusals carry no cell value:
            they name the sheet and row, the column when applicable, and the
            rule broken, in fixed text.
    """
    rows = _xlsx.load_sheet_rows(
        path,
        _CG_SHEET,
        required_sheets=_REQUIRED_SHEETS,
        optional_sheets=_OPTIONAL_SHEETS,
    )
    title = _xlsx.require_matching_text_row(
        rows,
        0,
        _CG_TITLE_RE,
        section=_CG_SHEET,
        path=path,
        description="StockPlan capital-gains-report title",
    )
    _xlsx.require_exact_row(
        rows,
        1,
        _CG_SECTION_ROW,
        section=_CG_SHEET,
        path=path,
        description="Sale Details / Acquisition Details section row",
    )
    header_row = 2
    _xlsx.require_exact_row(
        rows,
        header_row,
        _CG_HEADER,
        section=_CG_SHEET,
        path=path,
        description="header row",
    )

    sales: list[StockplanSale] = []
    sale_rows: list[int] = []
    saw_no_records = False
    for offset, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
        if _xlsx.is_blank_row(row):
            continue
        first = _xlsx.cell(row, 0)
        if first == _NO_RECORDS:
            # A clean sentinel row carries nothing beyond column 0. A "sentinel"
            # row that ALSO holds sale figures is corrupt — refuse rather than
            # skip it and hide the data.
            _require(
                all(_xlsx.cell(row, i) in (None, "") for i in range(1, len(row))),
                sheet=_CG_SHEET,
                row_index=offset,
                column=None,
                message=f"{_NO_RECORDS!r} row also carries sale data — ambiguous/corrupt",
            )
            _require(
                not saw_no_records,
                sheet=_CG_SHEET,
                row_index=offset,
                column=None,
                message=f"duplicate {_NO_RECORDS!r} row",
            )
            saw_no_records = True
            continue
        if _xlsx.single_text(row) == _xlsx.USD_STATEMENT:
            continue
        _xlsx.require_no_trailing_values(row, len(_CG_HEADER), section=_CG_SHEET, row_index=offset)
        if not _xlsx.is_data_row(row):
            _xlsx.refuse_unknown_row(section=_CG_SHEET, row_index=offset)
        sale_date = _xlsx.to_date(
            _xlsx.cell(row, 0), section=_CG_SHEET, row_index=offset, column="Sale Date"
        )
        price = _xlsx.to_decimal(
            _xlsx.cell(row, 1),
            section=_CG_SHEET,
            row_index=offset,
            column="Sale Price Per Share",
            kind=NumberKind.PRICE,
        )
        shares = _xlsx.to_decimal(
            _xlsx.cell(row, 2),
            section=_CG_SHEET,
            row_index=offset,
            column="Number of Shares Sold",
            kind=NumberKind.QUANTITY,
        )
        gross = _xlsx.to_decimal(
            _xlsx.cell(row, 4),
            section=_CG_SHEET,
            row_index=offset,
            column="Total Gross Proceeds",
            kind=NumberKind.MONEY,
        )
        acquisition_date = _xlsx.to_date(
            _xlsx.cell(row, 5), section=_CG_SHEET, row_index=offset, column="Acquisition Date"
        )
        share_class = str(_xlsx.cell(row, 3) or "").strip()
        _require(
            share_class == symbol,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Share Class",
            message="row Share Class does not match the configured [stockplan.security] symbol",
        )
        _require(
            shares > 0,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Number of Shares Sold",
            message="Number of Shares Sold must be positive",
        )
        _require(
            price >= 0,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Sale Price Per Share",
            message="Sale Price Per Share must not be negative",
        )
        _require(
            gross >= 0,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Total Gross Proceeds",
            message="Total Gross Proceeds must not be negative",
        )
        sales.append(
            StockplanSale(
                sale_date=sale_date,
                price_per_share=price,
                shares_sold=shares,
                share_class=share_class,
                gross_proceeds=gross,
                acquisition_date=acquisition_date,
            )
        )
        sale_rows.append(offset)
    _xlsx.require_usd_currency(rows, path, section=_CG_SHEET)
    if saw_no_records and sales:
        raise ParseError(
            section=_CG_SHEET,
            row_index=header_row,
            column=None,
            message=(
                f"{path.name} has BOTH a 'No records found' sentinel and sale row(s) — "
                "ambiguous/corrupt; refusing rather than risk under-reporting sales"
            ),
            raw_value="",
        )
    if not sales and not saw_no_records:
        raise ParseError(
            section=_CG_SHEET,
            row_index=header_row,
            column=None,
            message=(
                f"{path.name} has no sale rows AND no {_NO_RECORDS!r} sentinel — cannot "
                "confirm zero sales (possibly truncated); refusing rather than assume none"
            ),
            raw_value="",
        )
    run_on, start, end = _capital_gains_title_dates(title, path)
    for offset, sale in zip(sale_rows, sales, strict=True):
        _require(
            sale.sale_date <= run_on,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Sale Date",
            message="sale date must not be after this sheet's report run date",
        )
        _require(
            sale.acquisition_date <= run_on,
            sheet=_CG_SHEET,
            row_index=offset,
            column="Acquisition Date",
            message="acquisition date must not be after this sheet's report run date",
        )
    return sales, (start, end)
