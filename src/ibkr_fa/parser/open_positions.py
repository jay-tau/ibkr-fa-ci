"""Parse the IBKR Flex Query "Open Positions" (LOT) section into typed records.

Consumes ``list[dict[str, str]]`` rows from
:func:`ibkr_fa.parser.multi_section.split_sections` and produces
``list[OpenLot]``.

This module also enforces v1's hard input requirements (the parser is
where bad data gets rejected loudly):

- Every row must have ``LevelOfDetail`` ``LOT``, since each row is read
  as one lot, and then ``AssetClass`` ``STK``, since each lot is read as
  a stock (#1093).
- Every row must be USD-denominated. Multi-currency support is
  Tracker 7 (#52); v1 hard-fails with a pointer to the issue.
- Every lot must have an acquisition date >= 2020-01-06 (the earliest
  date the bundled SBI TTBR archive covers).
- Every lot's ``HoldingPeriodDateTime`` must equal its
  ``OpenDateTime``. IBKR documents a shifted holding-period date for
  US wash sales, and ``CostBasisMoney`` may then include the US
  disallowed-loss add-back, which the Indian return does not
  recognise. Flex can't show the actual cost, so such rows are
  refused with a pointer to the Activity Statement input (#599).
- Every row must have a non-empty ISIN (FA Table A3's entity
  identifier), and an ``IssuerCountryCode`` equal to its ISIN prefix
  (#794).
- ``PositionValue`` must equal ``Quantity * MarkPrice`` within 0.01,
  and ``CostBasisPrice`` must be non-negative. The lot keeps neither
  column, but a row that contradicts itself is refused. The product, the
  difference and its magnitude keep every digit (#1311,
  :func:`ibkr_fa.parser._exact.distance_from_product`).

These checks reject the whole file at the first offending row so
no half-correct output can ever be produced.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import ParseError, ReplacedCrashError, crash_site
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import STOCKS_ONLY, parse_decimal_cell, require_cell
from ibkr_fa.parser._dates import parse_yyyymmdd, refuse_shifted_holding_period
from ibkr_fa.parser._exact import distance_from_product
from ibkr_fa.parser._isin import validate_isin_or_raise
from ibkr_fa.parser._scope_guards import (
    require_foreign_mapped_country,
    require_isin_prefix_country,
    require_ttbr_covered_acquisition,
    require_usd,
)
from ibkr_fa.types import CountryCode, Isin, Ticker

_SECTION = "Open Positions (LOT)"

# Each row is read as one stock lot (#1093). ``LevelOfDetail`` is checked
# first, so a SUMMARY row, which has no open date, refuses as one.
_LOT_ONLY: Final[str] = (
    "each Open Positions row is read as one lot, so a SUMMARY or other row would be counted "
    "as another holding. Set the Flex Query's Open Positions Level of Detail to Lot"
)

# ``PositionValue`` should equal ``Quantity * MarkPrice``. Tiny rounding
# differences are normal; anything beyond one cent is a data-integrity
# problem.
_POSITION_VALUE_TOLERANCE: Final[Decimal] = Decimal("0.01")


def _decimal(raw: str, *, row_index: int, column: str, kind: NumberKind) -> Decimal:
    return parse_decimal_cell(raw, section=_SECTION, row_index=row_index, column=column, kind=kind)


def _yyyymmdd(raw: str, *, row_index: int, column: str) -> _dt.date:
    """Parse IBKR's compact date encoding ``YYYYMMDD``."""
    return parse_yyyymmdd(raw, section=_SECTION, row_index=row_index, column=column)


def _side(raw: str, *, row_index: int) -> Side:
    try:
        return Side(raw)
    except ValueError as exc:
        crash = ReplacedCrashError(crash_site(exc))
    # After the handler: the ValueError names the side.
    valid = ", ".join(s.value for s in Side)
    raise ParseError(
        section=_SECTION,
        row_index=row_index,
        column="Side",
        message=f"must be one of: {valid}",
        raw_value="",
    ) from crash


def _non_empty(raw: str, *, row_index: int, column: str) -> str:
    if not raw:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column=column,
            message="must be non-empty",
            raw_value="",
        )
    return raw


def _validate_position_value(
    raw: str, *, quantity: Decimal, mark_price: Decimal, row_index: int
) -> None:
    position_value = _decimal(
        raw, row_index=row_index, column="PositionValue", kind=NumberKind.MONEY
    )
    # Exact (#1311): a product or difference rounded to the context could pass
    # a row past the tolerance, or refuse one within it.
    refusal = ParseError(
        section=_SECTION,
        row_index=row_index,
        column="PositionValue",
        message="the PositionValue arithmetic exceeds the supported precision",
        raw_value="",
    )
    distance = distance_from_product(
        position_value, quantity, mark_price, refusal=lambda _out_of_memory: refusal
    )
    if distance > _POSITION_VALUE_TOLERANCE:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="PositionValue",
            message=f"must equal Quantity * MarkPrice within {_POSITION_VALUE_TOLERANCE}",
            raw_value="",
        )


def _validate_cost_basis_price(raw: str, *, row_index: int) -> None:
    if _decimal(raw, row_index=row_index, column="CostBasisPrice", kind=NumberKind.PRICE) < 0:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="CostBasisPrice",
            message="must be non-negative",
            raw_value="",
        )


def _parse_one(row: dict[str, str], *, row_index: int) -> OpenLot:
    require_cell(row, "LevelOfDetail", "LOT", section=_SECTION, row_index=row_index, why=_LOT_ONLY)
    require_cell(row, "AssetClass", "STK", section=_SECTION, row_index=row_index, why=STOCKS_ONLY)
    currency = row["CurrencyPrimary"]
    require_usd(currency, section=_SECTION, row_index=row_index, column="CurrencyPrimary")

    report_date = _yyyymmdd(row["ReportDate"], row_index=row_index, column="ReportDate")

    open_dt = _yyyymmdd(row["OpenDateTime"], row_index=row_index, column="OpenDateTime")
    holding_dt = _yyyymmdd(
        row["HoldingPeriodDateTime"],
        row_index=row_index,
        column="HoldingPeriodDateTime",
    )
    # Schedule FA's "initial value" is the lifetime acquisition cost
    # converted at the TTBR rate on the date the cost basis was actually
    # paid in INR — that's OpenDateTime, not HoldingPeriodDateTime.
    # Validating against OpenDateTime keeps fail-fast aligned with what
    # the FX layer will actually look up. A HoldingPeriodDateTime that
    # differs (US wash sale) is then refused (#599, see the module
    # docstring).
    require_ttbr_covered_acquisition(open_dt, section=_SECTION, row_index=row_index)
    refuse_shifted_holding_period(
        open_dt, holding_dt, section=_SECTION, row_index=row_index, cost_column="CostBasisMoney"
    )

    isin = validate_isin_or_raise(row["ISIN"], section=_SECTION, row_index=row_index, column="ISIN")

    # #147: the same IN and CBDT refusals as the Activity Statement
    # parser (#143). Normalise (strip + upper) first, so a lowercase or
    # padded 'in' still hits the IN refusal (PR #169 P3-1), then refuse
    # an empty or whitespace-only cell as missing rather than as an
    # unmapped country (PR #169 P3-2). #794: the result must also equal
    # the ISIN prefix, the Activity Statement's country.
    issuer_country = _non_empty(
        row["IssuerCountryCode"].strip().upper(),
        row_index=row_index,
        column="IssuerCountryCode",
    )
    require_foreign_mapped_country(
        issuer_country,
        section=_SECTION,
        row_index=row_index,
        column="IssuerCountryCode",
    )
    require_isin_prefix_country(issuer_country, isin=isin, section=_SECTION, row_index=row_index)

    quantity = _decimal(
        row["Quantity"], row_index=row_index, column="Quantity", kind=NumberKind.QUANTITY
    )
    mark_price = _decimal(
        row["MarkPrice"], row_index=row_index, column="MarkPrice", kind=NumberKind.PRICE
    )
    _validate_position_value(
        row["PositionValue"], quantity=quantity, mark_price=mark_price, row_index=row_index
    )
    _validate_cost_basis_price(row["CostBasisPrice"], row_index=row_index)

    return OpenLot(
        currency_primary=currency,
        symbol=Ticker(row["Symbol"]),
        description=row["Description"],
        isin=Isin(isin),
        issuer_country_code=CountryCode(issuer_country),
        report_date=report_date,
        quantity=quantity,
        mark_price=mark_price,
        cost_basis_money=_decimal(
            row["CostBasisMoney"],
            row_index=row_index,
            column="CostBasisMoney",
            kind=NumberKind.MONEY,
        ),
        side=_side(row["Side"], row_index=row_index),
        open_date_time=open_dt,
    )


def parse_open_positions(rows: Iterable[dict[str, str]]) -> list[OpenLot]:
    """Parse the rows of an Open Positions LOT section into ``list[OpenLot]``.

    Whether the ``ReportDate`` suits the report is the caller's check
    (:func:`~ibkr_fa.compute.source_coverage.require_open_lots_at_calendar_year_end`).

    Args:
        rows: One dict per data row (output of
            :func:`ibkr_fa.parser.multi_section.split_sections`).

    Returns:
        ``list[OpenLot]`` — one record per row, in input order.

    Raises:
        ParseError: On the first malformed or v1-rejected row. The
            error's ``row_index`` is zero-based within the section
            (matches the ``ReportDate``/``CurrencyPrimary`` etc. column
            location in the source CSV's section). No refusal carries a
            cell value, in its message or ``raw_value``, because a shifted
            cell can hold the holder's name or account number.
    """
    return [_parse_one(row, row_index=i) for i, row in enumerate(rows)]
