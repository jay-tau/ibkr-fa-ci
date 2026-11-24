"""Parse closed-lot rows out of the IBKR Flex Query Trades section.

The Trades section (LOT-detail variant — see PR #90 /
``ibkr_fa.parser.multi_section``) interleaves two row types:

* ``LevelOfDetail == "EXECUTION"`` — one row per trade execution.
  These have ``OpenDateTime`` / ``HoldingPeriodDateTime`` blank and
  populate the cash-flow fields (``TradeMoney``, ``Proceeds``, etc.).
  They yield no :class:`ClosedLot`. Their cash-flow fields are never
  read; their ``Open/CloseIndicator`` is, and a closing execution's
  ``ISIN`` and ``Quantity`` too (see the closing-execution check below).
* ``LevelOfDetail == "CLOSED_LOT"`` — one row per FIFO-matched lot
  pair. These populate ``OpenDateTime``, ``HoldingPeriodDateTime``,
  ``CostBasis``, ``FifoPnlRealized``; the cash-flow fields
  (``TradeMoney``, ``Proceeds``, ``Taxes``, ``IBCommission``,
  ``IBCommissionCurrency``, ``NetCash``, ``ClosePrice``) are blank.
  Each becomes one :class:`ClosedLot` tagged
  :attr:`~ibkr_fa.models.BasisProvenance.BROKER_MATCHED`, with net
  proceeds derived as ``CostBasis + FifoPnlRealized`` because
  ``Proceeds`` is blank. The sum keeps every digit (#1311). A partial
  disposal of one open lot produces one CLOSED_LOT row per matched slice.

IBKR does not emit closed lots as a Flex section of their own: enabling
Closed Lots in the Flex Query adds the ``LevelOfDetail``,
``OpenDateTime`` and ``HoldingPeriodDateTime`` columns to the Trades
schema and interleaves CLOSED_LOT rows with the EXECUTION rows.

A row whose ``LevelOfDetail`` is anything OTHER than those two values
(including the empty string or a missing column) is rejected loudly
— silently dropping unknown row types would mask real schema drift.

This module also enforces v1's hard input requirements (the parser
is where bad data gets rejected loudly):

- Every row, CLOSED_LOT and EXECUTION alike, must have ``AssetClass``
  ``STK``, checked after ``LevelOfDetail``: v1 reads only stocks, as the
  Activity Statement Trades parser does (#913, #1093).
- Every CLOSED_LOT row must have ``Buy/Sell == "SELL"`` (a long-side
  closure). ``"BUY"`` is a short cover; short positions are
  liabilities and out of Schedule FA Table A3 scope — reject loudly
  rather than silently disclose a non-asset.
- Every CLOSED_LOT row must be USD-denominated. Multi-currency
  support is Tracker 7 (#52); v1 hard-fails with a pointer.
- Every closed lot's ``OpenDateTime`` must be >= 2020-01-06 (the
  earliest date the bundled SBI TTBR archive covers). Schedule FA's
  initial value is FX-converted on the cash-outlay date, which is
  ``OpenDateTime``.
- Every row must have a non-empty ISIN, and an ``IssuerCountryCode``
  equal to its ISIN prefix (#794).

Each lot carries its ``HoldingPeriodDateTime`` date. The engine, not
this parser, refuses a broker-matched lot whose holding-period date
differs from its open date (#599, #640; see
:func:`ibkr_fa.compute.lot_matching.require_indian_basis`).

These checks reject the whole batch at the first offending row so
no half-correct output can ever be produced.

Schedule FA Table A3 lists a holding sold during the year only through its
CLOSED_LOT rows, so once every row has passed the checks above the parser
also checks that the closed lots account for every closing execution
(#1286). Every EXECUTION row's ``Open/CloseIndicator`` must be exactly
``O`` or ``C``. For each ISIN, the closing executions' quantities, without
their signs, must add up exactly to its CLOSED_LOT rows' quantities.
"""

from __future__ import annotations

import operator
from collections.abc import Iterable
from decimal import Decimal
from fractions import Fraction
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.models import BasisProvenance, ClosedLot
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import STOCKS_ONLY, parse_decimal_cell, require_cell, require_columns
from ibkr_fa.parser._dates import parse_yyyymmdd, parse_yyyymmdd_with_time
from ibkr_fa.parser._exact import exactly
from ibkr_fa.parser._isin import validate_isin_or_raise
from ibkr_fa.parser._scope_guards import (
    require_foreign_mapped_country,
    require_isin_prefix_country,
    require_ttbr_covered_acquisition,
    require_usd,
)
from ibkr_fa.types import CountryCode, Isin, Ticker

# This parser consumes Trades-section rows; section name is preserved
# so :class:`ParseError` context points at the actual upstream
# section. The subset selected by ``LevelOfDetail == "CLOSED_LOT"``
# isn't a section in its own right (see the module docstring).
_SECTION: Final[str] = "Trades"

# Columns added to the Trades schema by the LOT-detail variant. If any
# of these is missing from a row dict, the caller passed in legacy
# 27-col Trades rows (Closed Lots not enabled in their Flex Query) —
# that's a configuration error, raise loudly.
_REQUIRED_LOT_DETAIL_COLUMNS: Final[tuple[str, ...]] = (
    "LevelOfDetail",
    "OpenDateTime",
    "HoldingPeriodDateTime",
)

_CLOSED_LOT_LEVEL: Final[str] = "CLOSED_LOT"
_EXECUTION_LEVEL: Final[str] = "EXECUTION"
_OPENING_INDICATOR: Final[str] = "O"
_CLOSING_INDICATOR: Final[str] = "C"
_LONG_CLOSE_BUY_SELL: Final[str] = "SELL"
_SHORT_COVER_BUY_SELL: Final[str] = "BUY"


def _decimal(raw: str, *, row_index: int, column: str, kind: NumberKind) -> Decimal:
    return parse_decimal_cell(raw, section=_SECTION, row_index=row_index, column=column, kind=kind)


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


def _validate_level_of_detail(raw: str, *, row_index: int) -> str:
    if raw not in {_CLOSED_LOT_LEVEL, _EXECUTION_LEVEL}:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="LevelOfDetail",
            message=(
                "unrecognised LevelOfDetail; expected one of "
                f"{_CLOSED_LOT_LEVEL!r} or {_EXECUTION_LEVEL!r}. An "
                f"empty value or unknown row type indicates Flex Query "
                f"schema drift; please open an issue with a redacted "
                f"sample row."
            ),
            raw_value="",
        )
    return raw


def _validate_buy_sell(raw: str, *, row_index: int) -> None:
    """Reject CLOSED_LOT rows that don't represent long-side closures.

    ``Buy/Sell == "SELL"`` means a long position was sold — Schedule
    FA Table A3 does declare those (the held-during-CY asset). v1
    rejects everything else:

    * ``"BUY"`` indicates a short-cover (buying back a short
      position to close it). Short positions are liabilities, not
      foreign assets, and therefore out of FA Table A3 scope. Today's
      :class:`ClosedLot` model doesn't carry a side field; rather
      than silently include short-cover rows in the FA computation,
      reject them at parse time so the user is told upfront that v1
      doesn't model their account.
    * Any other value indicates Flex Query schema drift; reject.
    """
    # Engine gap: ClosedLot has no side, and lot_overlap relies on this refusal.
    if raw == _SHORT_COVER_BUY_SELL:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Buy/Sell",
            message=(
                "CLOSED_LOT row has Buy/Sell='BUY' (short cover). Short "
                "positions are liabilities, not Schedule FA Table A3 "
                "assets, and v1 does not model them. If your account "
                "uses short-selling, please open an issue."
            ),
            raw_value="",
        )
    if raw != _LONG_CLOSE_BUY_SELL:
        raise ParseError(
            section=_SECTION,
            row_index=row_index,
            column="Buy/Sell",
            message=(
                "unrecognised Buy/Sell on CLOSED_LOT row; "
                f"expected {_LONG_CLOSE_BUY_SELL!r} (long-side close)."
            ),
            raw_value="",
        )


def _parse_one(row: dict[str, str], *, row_index: int) -> ClosedLot:
    _validate_buy_sell(row["Buy/Sell"], row_index=row_index)
    currency = row["CurrencyPrimary"]
    require_usd(currency, section=_SECTION, row_index=row_index, column="CurrencyPrimary")

    open_dt = parse_yyyymmdd_with_time(
        _non_empty(row["OpenDateTime"], row_index=row_index, column="OpenDateTime"),
        section=_SECTION,
        row_index=row_index,
        column="OpenDateTime",
    )
    require_ttbr_covered_acquisition(open_dt, section=_SECTION, row_index=row_index)

    # HoldingPeriodDateTime is parsed (and its time component validated)
    # so the engine can refuse a calendar date that differs from
    # OpenDateTime (#599, see the module docstring). FX conversion uses
    # OpenDateTime.
    holding_dt = parse_yyyymmdd_with_time(
        _non_empty(
            row["HoldingPeriodDateTime"],
            row_index=row_index,
            column="HoldingPeriodDateTime",
        ),
        section=_SECTION,
        row_index=row_index,
        column="HoldingPeriodDateTime",
    )

    close_dt = parse_yyyymmdd(
        _non_empty(row["TradeDate"], row_index=row_index, column="TradeDate"),
        section=_SECTION,
        row_index=row_index,
        column="TradeDate",
    )

    isin = validate_isin_or_raise(row["ISIN"], section=_SECTION, row_index=row_index, column="ISIN")

    # #147: the same normalisation and refusals as parse_open_positions.
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

    cost_basis = _decimal(
        _non_empty(row["CostBasis"], row_index=row_index, column="CostBasis"),
        row_index=row_index,
        column="CostBasis",
        kind=NumberKind.MONEY,
    )
    fifo_pnl = _decimal(
        _non_empty(row["FifoPnlRealized"], row_index=row_index, column="FifoPnlRealized"),
        row_index=row_index,
        column="FifoPnlRealized",
        kind=NumberKind.MONEY,
    )
    # IBKR leaves the Proceeds column blank on CLOSED_LOT rows, so we
    # derive it from the FIFO identity proceeds = cost_basis + pnl.
    # Verified against every CLOSED_LOT row in a private Flex Query export.
    # Added with every digit kept (#1311): the proceeds are Schedule CG's
    # consideration.
    proceeds = exactly(
        operator.add,
        cost_basis,
        fifo_pnl,
        refusal=lambda _out_of_memory: ParseError(
            section=_SECTION,
            row_index=row_index,
            column="FifoPnlRealized",
            message="the proceeds arithmetic exceeds the supported precision",
            raw_value="",
        ),
    )

    return ClosedLot(
        currency_primary=currency,
        sub_category=row["SubCategory"],
        symbol=Ticker(_non_empty(row["Symbol"], row_index=row_index, column="Symbol")),
        description=row["Description"],
        isin=Isin(isin),
        listing_exchange=row["ListingExchange"],
        issuer_country_code=CountryCode(issuer_country),
        quantity=_decimal(
            _non_empty(row["Quantity"], row_index=row_index, column="Quantity"),
            row_index=row_index,
            column="Quantity",
            kind=NumberKind.QUANTITY,
        ),
        cost_basis_money=cost_basis,
        proceeds_money=proceeds,
        open_date_time=open_dt,
        close_date_time=close_dt,
        basis_provenance=BasisProvenance.BROKER_MATCHED,
        holding_period_date_time=holding_dt,
    )


def _require_closed_lots_for_closing_executions(
    executions: list[tuple[int, dict[str, str]]],
    lots: list[tuple[int, ClosedLot]],
) -> None:
    """Refuse unless each ISIN's closed lots add up to its closing executions.

    ``executions`` and ``lots`` pair each row with its index in the Trades
    section. An EXECUTION row's ``Open/CloseIndicator`` must be exactly
    ``O`` or ``C``; anything else, a combined form included, refuses. Per
    ISIN, the closing executions' quantities without their signs must add
    up exactly to the CLOSED_LOT quantities. A mismatch refuses at the
    ISIN's first closing EXECUTION or CLOSED_LOT row, with no cell value
    in the refusal.
    """
    closing: dict[str, Fraction] = {}
    closed: dict[str, Fraction] = {}
    first_row: dict[str, int] = {}
    for row_index, row in executions:
        indicator = row["Open/CloseIndicator"]
        if indicator == _OPENING_INDICATOR:
            continue
        if indicator != _CLOSING_INDICATOR:
            raise ParseError(
                section=_SECTION,
                row_index=row_index,
                column="Open/CloseIndicator",
                message=(
                    f"unrecognised Open/CloseIndicator on an EXECUTION row; "
                    f"expected {_OPENING_INDICATOR!r} (opening) or "
                    f"{_CLOSING_INDICATOR!r} (closing)."
                ),
                raw_value="",
            )
        isin = validate_isin_or_raise(
            row["ISIN"], section=_SECTION, row_index=row_index, column="ISIN"
        )
        quantity = _decimal(
            _non_empty(row["Quantity"], row_index=row_index, column="Quantity"),
            row_index=row_index,
            column="Quantity",
            kind=NumberKind.QUANTITY,
        )
        closing[isin] = closing.get(isin, Fraction(0)) + abs(Fraction(quantity))
        first_row.setdefault(isin, row_index)
    for row_index, lot in lots:
        closed[lot.isin] = closed.get(lot.isin, Fraction(0)) + Fraction(lot.quantity)
        first_row[lot.isin] = min(first_row.get(lot.isin, row_index), row_index)
    for isin in sorted(first_row, key=first_row.__getitem__):
        if closing.get(isin, Fraction(0)) != closed.get(isin, Fraction(0)):
            raise ParseError(
                section=_SECTION,
                row_index=first_row[isin],
                column="Quantity",
                message=(
                    "the CLOSED_LOT quantities for this row's security don't add "
                    "up to its closing EXECUTION quantities, so a holding sold "
                    "during the year can't be checked against its closed lots. "
                    "Re-run the Flex Query over the whole calendar year with the "
                    "Closed Lots checkbox in the Trades section turned on."
                ),
                raw_value="",
            )


def parse_closed_lots(rows: Iterable[dict[str, str]]) -> list[ClosedLot]:
    """Parse the CLOSED_LOT rows of a Trades section into ``list[ClosedLot]``.

    Args:
        rows: One dict per data row of the Trades section (output of
            :func:`ibkr_fa.parser.multi_section.split_sections` keyed
            by :attr:`KnownSection.TRADES`). Each row must come from
            the LOT-detail variant of the Trades schema (must contain
            the ``LevelOfDetail``, ``OpenDateTime``, and
            ``HoldingPeriodDateTime`` columns).

    Returns:
        ``list[ClosedLot]`` — one record per CLOSED_LOT row, in input
        order. EXECUTION rows yield no record; they are only checked
        against the closed lots (see the module docstring).
        :class:`ClosedLot.proceeds_money` is **derived** from
        ``CostBasis + FifoPnlRealized``, with every digit kept, because
        IBKR leaves the ``Proceeds`` column blank on CLOSED_LOT rows.

    Raises:
        ParseError: If any of the LOT-detail columns is absent
            (caller passed legacy 27-col Trades rows); if a row's
            ``LevelOfDetail`` is neither ``"CLOSED_LOT"`` nor
            ``"EXECUTION"`` (schema drift); if a row's ``AssetClass`` isn't
            ``"STK"``; or if any per-row
            validation on a CLOSED_LOT row fails (non-USD currency,
            pre-2020 acquisition, malformed date, blank required field,
            non-decimal numeric, proceeds that can't be added up exactly).
            Once every row has passed those checks,
            also if an EXECUTION row's ``Open/CloseIndicator`` is neither
            ``"O"`` nor ``"C"``, if a closing execution's ``ISIN`` or
            ``Quantity`` is malformed, or if an ISIN's CLOSED_LOT
            quantities don't add up exactly to its closing executions'
            quantities. No ParseError carries a cell value, in its
            message or ``raw_value``, because a shifted cell can hold the
            holder's name or account number.
    """
    executions: list[tuple[int, dict[str, str]]] = []
    lots: list[tuple[int, ClosedLot]] = []
    for row_index, row in enumerate(rows):
        require_columns(
            row,
            _REQUIRED_LOT_DETAIL_COLUMNS,
            section=_SECTION,
            row_index=row_index,
            message=lambda missing: (
                f"required LOT-detail column(s) {missing!r} missing from the "
                f"Trades section row. The Flex Query was probably exported "
                f"without Closed Lots enabled — re-run the query with the "
                f"Closed Lots checkbox in the Trades section turned on."
            ),
        )
        level = _validate_level_of_detail(row["LevelOfDetail"], row_index=row_index)
        require_cell(
            row, "AssetClass", "STK", section=_SECTION, row_index=row_index, why=STOCKS_ONLY
        )
        if level == _EXECUTION_LEVEL:
            executions.append((row_index, row))
            continue
        lots.append((row_index, _parse_one(row, row_index=row_index)))
    _require_closed_lots_for_closing_executions(executions, lots)
    return [lot for _, lot in lots]
