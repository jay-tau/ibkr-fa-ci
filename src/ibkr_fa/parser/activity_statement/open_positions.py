"""Read and check an Activity Statement's Open Positions section.

:func:`build_dec31_close_prices` reads each holding's period-end close price
from the ``Summary`` rows; :func:`ibkr_fa.compute.lot_matching.match_fifo`
marks each surviving open lot with it.

After matching, :func:`validate_reconciliation` hard-validates the
per-symbol sum of *signed* open-lot residual quantities (positive for
LONG, negative for SHORT — matching IBKR's own sign convention on Open
Positions Summary's ``Quantity`` column) against the Open Positions
Summary aggregate for that symbol, adding and comparing with every digit
kept (#1311). Symbols whose opens are fully
consumed by closures legitimately drop out of Open Positions and
out of the reconciliation. Mismatch beyond ε = 0.0001 shares
raises a :class:`ParseError` — Schedule FA correctness demands the
parser refuses output rather than silently drop a trade. It then ties
each lot-detail ``Lot`` row to the ``Summary`` row it splits, so a
holding only a ``Lot`` row shows refuses rather than dropping out (#1124).
Last, :func:`validate_open_positions_values` refuses a ``Summary`` row
whose ``Value`` is more than 0.01 from its ``Quantity`` times ``Close
Price``, computed with every digit kept (#1315).

The cell helpers and messages these checks share with the Financial
Instrument Information and Trades readers stay in
:mod:`ibkr_fa.parser.activity_statement.trades`, which this module imports
(#2007).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import MAX_PREC, Decimal, DecimalException, Inexact, localcontext
from functools import partial
from typing import Final

from ibkr_fa.exceptions import ParseError
from ibkr_fa.invariants import require
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.number_checks import NumberKind
from ibkr_fa.parser._cells import STOCKS_ONLY, require_cell
from ibkr_fa.parser._exact import distance_from_product, exactly
from ibkr_fa.parser._scope_guards import require_usd
from ibkr_fa.parser.activity_statement.multi_section import FILE_AN_ISSUE
from ibkr_fa.parser.activity_statement.trades import (
    _NO_CODES,
    _ONE_TO_ONE,
    _STOCKS,
    _ZERO_QUANTITY,
    _decimal,
    _non_empty,
    _precision_refusal,
)
from ibkr_fa.types import Ticker

# Tolerance for the reconstructed-quantity-vs-Open-Positions-aggregate
# cross-check. Activity Statement quantities are reported to 4 decimal
# places for fractional shares; tolerance below that is just rounding
# noise, above that signals a real discrepancy.
_QUANTITY_RECONCILIATION_TOLERANCE: Final[Decimal] = Decimal("0.0001")

# The Open Positions row kinds (``DataDiscriminator``) the readers know
# (#1093). ``Summary`` rows carry each position's totals; ``Lot`` rows,
# from lot-detail mode, split a Summary row into its lots. Prices and
# reconciled position totals come from Summary rows only. After its Summary
# checks, :func:`validate_reconciliation` ties every Lot row to the one
# Summary row it splits (#1124, :func:`_check_lot_rows`).
_OPEN_POSITIONS: Final[str] = "Open Positions"
_SUMMARY: Final[str] = "Summary"
_LOT: Final[str] = "Lot"
_DUPLICATE_SYMBOL: Final[str] = (
    "the Symbol appears more than once in Open Positions; refusing to silently "
    "overwrite the earlier close price."
)


def _is_summary_row(discriminator: str, *, row_index: int) -> bool:
    """Whether an Open Positions row is a ``Summary`` row (#1093).

    A ``Lot`` row isn't one; reconciliation checks it against its
    ``Summary`` row afterwards (#1124).

    Raises:
        ParseError: If the row is of any other kind, which could hold a
            holding no check sees. The refusal doesn't print the cell.
    """
    if discriminator in {_SUMMARY, _LOT}:
        return discriminator == _SUMMARY
    raise ParseError(
        section=_OPEN_POSITIONS,
        row_index=row_index,
        column="DataDiscriminator",
        message=(
            "must be 'Summary' or 'Lot', the only Open Positions row kinds this tool reads; "
            "another kind could hold a holding no check sees. Refusing rather than skipping "
            f"it. {FILE_AN_ISSUE}"
        ),
        raw_value="",
    )


def build_dec31_close_prices(rows: Iterable[dict[str, str]]) -> dict[Ticker, Decimal]:
    """Build a Symbol -> period-end close price map from Open Positions.

    For an Activity Statement ending on Dec 31 (Schedule FA's period, #1288),
    the Open Positions Summary's ``Close Price`` column is the close
    price on the **statement-period end date** (Dec 31). Schedule FA's
    Closing Balance must use this period-end price — NOT the trade-day
    close price (``Trades.C. Price``), which the parser does not read.
    :func:`ibkr_fa.compute.lot_matching.match_fifo` marks each surviving
    open lot with it.

    Issue #133: without this lookup, a lot acquired mid-year and still
    held at Dec 31 would record its acquisition-day close as its
    year-end close, over- or under-stating Schedule FA's Closing
    Balance by however much the price moved between acquisition and
    Dec 31.

    The function itself is date-agnostic: it returns whatever the
    ``Close Price`` column contains. The "Dec 31" semantics for
    production runs are enforced by the pipeline's
    :func:`~ibkr_fa.compute.source_coverage.require_calendar_year_period`
    check before this function is called; the name reflects the
    Schedule FA use case.

    Args:
        rows: Data rows from the :class:`KnownActivitySection.OPEN_POSITIONS`
            blocks. Every row must be a stock with multiplier 1 and no
            code (#913). Its ``DataDiscriminator`` must then be one of the
            two row kinds (#1093): the function consumes ``Summary`` rows
            and skips ``Lot`` rows, the per-lot rows IBKR adds in
            lot-detail mode, reading no price from them. The pipeline
            passes the same flattened rows to
            :func:`validate_reconciliation`, which ties each ``Lot`` row
            to its ``Summary`` row in four passes (#1124). ``Total`` rows
            are already dropped by the section splitter.

    Returns:
        Lookup dict ``{Symbol -> Dec 31 Close Price}``. Empty if input
        has no ``Summary`` rows.

    Raises:
        ParseError: If the ``DataDiscriminator`` column is missing from
            a row; if a row's ``Asset Category`` isn't ``Stocks``, its
            ``Mult`` isn't ``1`` or its ``Code`` (when the export has
            one) isn't blank; if its ``DataDiscriminator`` is neither
            ``Summary`` nor ``Lot``; if a ``Summary`` row is missing a required column;
            if a ``Summary`` row's ``Currency`` isn't ``USD`` (v1 is
            USD-only, multi-currency tracked at #52); if the same
            Symbol appears in more than one ``Summary`` row (would
            silently overwrite); or if the ``Close Price`` value isn't
            a finite decimal. No refusal carries a cell value, in its
            message or ``raw_value``, because a shifted cell can hold
            the holder's name or account number.
    """
    out: dict[Ticker, Decimal] = {}
    materialised = list(rows)
    open_positions_section = "Open Positions"
    summary_count = 0
    for row_index, row in enumerate(materialised):
        if "DataDiscriminator" not in row:
            # Missing the column entirely is a schema-drift signal
            # (every recognised Open Positions schema carries this
            # header column — see multi_section.py). Refuse rather
            # than silently classify the row as non-Summary.
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column="DataDiscriminator",
                message=(
                    "required column 'DataDiscriminator' missing from "
                    "the Open Positions row; can't classify the row "
                    "as Summary or otherwise. IBKR may have changed "
                    "the section's schema."
                ),
                raw_value="",
            )
        checks = {"Asset Category": (_STOCKS, STOCKS_ONLY), "Mult": ("1", _ONE_TO_ONE)}
        if "Code" in row:
            checks["Code"] = ("", _NO_CODES)
        for column, (expected, why) in checks.items():
            require_cell(
                row,
                column,
                expected,
                section=open_positions_section,
                row_index=row_index,
                why=why,
            )
        if not _is_summary_row(row["DataDiscriminator"], row_index=row_index):
            # A Lot row. The postcondition below locks "non-empty out
            # iff at least one Summary row in" against summary_count.
            continue
        summary_count += 1
        try:
            symbol = row["Symbol"]
            currency_raw = row["Currency"]
            close_price_raw = row["Close Price"]
        except KeyError as exc:
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column=str(exc.args[0]),
                message=(
                    f"required column {exc.args[0]!r} missing from "
                    f"the Open Positions row; can't extract Dec 31 "
                    f"close price."
                ),
                raw_value="",
            ) from exc
        # Tier-3 PR #134 review (GPT-5.5 high) — without this check
        # a non-USD Summary row would silently feed its Close Price
        # into an OpenLot whose currency_primary is set to USD by
        # the Trades parser, producing a wrong-currency mark_price
        # and a wrong-currency Schedule FA closing balance. v1 is
        # USD-only across the whole pipeline (same check exists on
        # the Trades parser, parse_activity_statement_trades).
        # Multi-currency support is tracked at #52.
        currency = _non_empty(
            currency_raw,
            row_index=row_index,
            column="Currency",
            section=open_positions_section,
        )
        require_usd(
            currency, section=open_positions_section, row_index=row_index, column="Currency"
        )
        sym = _non_empty(
            symbol, row_index=row_index, column="Symbol", section=open_positions_section
        )
        ticker = Ticker(sym)
        if ticker in out:
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column="Symbol",
                message=_DUPLICATE_SYMBOL,
                raw_value="",
            )
        out[ticker] = _decimal(
            close_price_raw,
            row_index=row_index,
            column="Close Price",
            kind=NumberKind.PRICE,
            section=open_positions_section,
        )
    # Postcondition (Tier-1 r2 GPT-5.5 suggestion): strengthened from
    # bool-equality to count-equality. Every accepted Summary row must
    # produce exactly one out-entry; the only paths that don't add to
    # out (duplicate-symbol, missing column, non-decimal Close Price)
    # all raise ParseError before the loop continues, so reaching the
    # postcondition with len(out) < summary_count signals a future
    # refactor that silently drops a valid Summary row.
    require(
        len(out) == summary_count,
        what="build_dec31_close_prices post: one out-entry per accepted Summary row",
        summary_row_count=summary_count,
        output_count=len(out),
    )
    return out


def validate_reconciliation(
    open_lots: Iterable[OpenLot],
    open_positions_rows: Iterable[dict[str, str]],
) -> None:
    """Cross-check reconstructed-lot quantities against Open Positions Summary.

    Per-symbol: sum of reconstructed *signed* :attr:`OpenLot.quantity`
    (positive for ``Side.LONG``, negative for ``Side.SHORT`` —
    matching IBKR's own sign convention on Open Positions Summary's
    ``Quantity`` column) must equal the Open Positions Summary
    ``Quantity`` for that symbol, with ε = 0.0001 shares tolerance
    for fractional rounding. The sum and its difference from ``Quantity``
    keep every digit (#1311).

    A mismatch signals either a parser bug (we dropped or
    miscounted a trade), an unsupported edge case the Codes-allow-
    list missed, or a corrupted input file. In any case the safe
    thing to do is refuse output — Schedule FA correctness is more
    important than producing a result.

    ``Lot`` rows, which IBKR adds in lot-detail mode, supply no holdings.
    After every check on the ``Summary`` rows, each ``Lot`` row is tied
    to the one ``Summary`` row it splits (#1124), in four passes where
    the first failure wins:

    1. each ``Lot`` row has a non-empty ``Symbol`` and ``Currency`` and a
       finite, non-zero ``Quantity``;
    2. each ``Summary`` row whose ``Symbol`` has ``Lot`` rows is that
       Symbol's only one and has a non-empty ``Currency``;
    3. each ``Lot`` row has such a ``Summary`` row, with the same
       ``Currency`` and a ``Quantity`` of the same sign;
    4. each such ``Summary`` row's ``Quantity`` is exactly the sum of its
       ``Lot`` quantities, added without rounding.

    Each pass goes in row order. A ``Summary`` row in one Open Positions
    block covers ``Lot`` rows in another, since the blocks are flattened.

    Args:
        open_lots: The surviving open lots from
            :func:`ibkr_fa.compute.lot_matching.match_fifo`.
        open_positions_rows: Data rows from every
            :class:`KnownActivitySection.OPEN_POSITIONS` block, flattened
            in file order. Row indices in refusals count from zero across
            the blocks.

    Raises:
        ParseError: On any per-symbol quantity mismatch beyond
            tolerance, on a per-symbol sum or difference that can't be
            kept exactly, on a symbol present in Open Positions but
            missing from reconstructed lots, on an Open Positions
            ``Quantity`` that isn't a finite decimal, on a missing
            required Open Positions column, on a ``DataDiscriminator``
            other than ``Summary`` or ``Lot``, after the per-row checks
            on reconstructed lots for a symbol with no Open Positions
            ``Summary`` row (reported one past the last row, since no
            row is at fault), and last on a ``Lot`` row that fails one
            of the passes above. No refusal carries a cell value, a symbol
            or a quantity, in its message or ``raw_value``, because a
            shifted cell can hold the holder's name or account number.
    """
    by_symbol_quantities: dict[str, list[Decimal]] = {}
    for lot in open_lots:
        # Signed contribution: LONG adds positive shares, SHORT
        # subtracts (matching IBKR's negative-quantity convention
        # on Open Positions Summary for short positions). Issue
        # #109 — without the sign, a partially-covered short or a
        # long+short pair on the same symbol would silently
        # reconcile to the wrong absolute quantity. copy_negate()
        # never rounds; unary minus rounds to the context (#1311).
        signed_qty = lot.quantity if lot.side == Side.LONG else lot.quantity.copy_negate()
        by_symbol_quantities.setdefault(str(lot.symbol), []).append(signed_qty)

    open_positions_section = "Open Positions"
    open_positions_symbols: set[str] = set()
    # Each read Summary row's index, Symbol and Quantity, for the Lot checks.
    summaries: list[tuple[int, str, Decimal]] = []
    materialised = list(open_positions_rows)
    for row_index, row in enumerate(materialised):
        try:
            data_discriminator = row["DataDiscriminator"]
        except KeyError as exc:
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column="DataDiscriminator",
                message=(
                    "required column 'DataDiscriminator' missing from "
                    "Open Positions row; can't reconcile."
                ),
                raw_value="",
            ) from exc
        if not _is_summary_row(data_discriminator, row_index=row_index):
            # A Lot row: _check_lot_rows checks it below.
            continue

        symbol = _reconciled_cell(row, "Symbol", row_index=row_index)
        qty_str = _reconciled_cell(row, "Quantity", row_index=row_index)
        open_positions_symbols.add(symbol)
        expected_qty = _decimal(
            qty_str,
            row_index=row_index,
            column="Quantity",
            kind=NumberKind.QUANTITY,
            section=open_positions_section,
        )

        quantities = by_symbol_quantities.get(symbol)
        if quantities is None:
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column="Symbol",
                message=(
                    "this Summary row's Symbol has no reconstructed "
                    "open lots from Trades. "
                    "Possible causes: the holding was bought before the "
                    "statement period (re-export the Activity Statement "
                    "starting from your account's first trade), trades "
                    "section is truncated, or "
                    "the position came from a transfer-in (see "
                    "https://github.com/jay-tau/ibkr-fa/issues/107) "
                    "rather than a market trade. Refusing rather than "
                    "producing partial FA-A3 output."
                ),
                raw_value="",
            )
        # The reconstructed total minus Quantity, with every digit kept
        # (#1311): rounded to the context, it could pass a mismatch past the
        # tolerance or refuse a match within it.
        difference = exactly(
            _total_less,
            expected_qty,
            *quantities,
            refusal=partial(
                _precision_refusal,
                section=open_positions_section,
                row_index=row_index,
                column="Quantity",
                at="the reconciliation arithmetic",
            ),
        )
        if difference.copy_abs() > _QUANTITY_RECONCILIATION_TOLERANCE:
            raise ParseError(
                section=open_positions_section,
                row_index=row_index,
                column="Quantity",
                message=(
                    f"reconciliation failed: the total quantity "
                    f"reconstructed from Trades for this Summary row's "
                    f"Symbol differs from its Quantity by more than "
                    f"{_QUANTITY_RECONCILIATION_TOLERANCE}. If part of the "
                    f"holding was bought before the statement period, "
                    f"re-export the Activity Statement starting from your "
                    f"account's first trade. Otherwise, refusing output to "
                    f"avoid corrupting Schedule FA — please file an issue "
                    f"with a redacted statement."
                ),
                raw_value="",
            )
        summaries.append((row_index, symbol, expected_qty))
    # Also catch symbols that the Trades section leaves open but that
    # have no Open Positions Summary row.
    # No Open Positions row is at fault, so the error points one past the
    # last row, where the missing rows were expected (as the splitter's
    # duplicate-section refusal does).
    extras = set(by_symbol_quantities) - open_positions_symbols
    if extras:
        raise ParseError(
            section=open_positions_section,
            row_index=len(materialised),
            column=None,
            message=(
                f"the Open Positions section has no Summary row for "
                f"{len(extras)} symbol(s) that the Trades section leaves "
                f"open; each needs one. No existing row is at fault, so "
                f"this points one past the last row. This shouldn't "
                f"happen — file an issue with a redacted statement."
            ),
            raw_value="",
        )
    _check_lot_rows(materialised, summaries)


def _total_less(subtrahend: Decimal, *terms: Decimal) -> Decimal:
    """``sum(terms) - subtrahend``, for :func:`~ibkr_fa.parser._exact.exactly`."""
    return sum(terms, subtrahend.copy_negate())


def _reconciled_cell(row: dict[str, str], column: str, *, row_index: int) -> str:
    """Return an Open Positions row's ``column`` cell, refusing a missing column."""
    try:
        return row[column]
    except KeyError as exc:
        raise ParseError(
            section=_OPEN_POSITIONS,
            row_index=row_index,
            column=column,
            message=f"required column {column!r} missing from Open Positions row; can't reconcile.",
            raw_value="",
        ) from exc


def _lot_refusal(row_index: int, column: str, message: str) -> ParseError:
    """An Open Positions refusal from :func:`_check_lot_rows`; it prints no cell."""
    return ParseError(
        section=_OPEN_POSITIONS,
        row_index=row_index,
        column=column,
        message=message,
        raw_value="",
    )


def _read_lot_row(row: dict[str, str], *, row_index: int) -> tuple[int, str, str, Decimal]:
    """Pass 1 of :func:`_check_lot_rows`: a ``Lot`` row's index, Symbol, Currency and Quantity.

    Raises:
        ParseError: If the ``Symbol``, ``Quantity`` or ``Currency`` column is
            missing, the ``Symbol`` or ``Currency`` is blank, or the
            ``Quantity`` isn't a finite, non-zero decimal.
    """
    symbol = _reconciled_cell(row, "Symbol", row_index=row_index)
    quantity_raw = _reconciled_cell(row, "Quantity", row_index=row_index)
    currency = _reconciled_cell(row, "Currency", row_index=row_index)
    _non_empty(symbol, row_index=row_index, column="Symbol", section=_OPEN_POSITIONS)
    _non_empty(currency, row_index=row_index, column="Currency", section=_OPEN_POSITIONS)
    quantity = _decimal(
        quantity_raw,
        row_index=row_index,
        column="Quantity",
        kind=NumberKind.QUANTITY,
        section=_OPEN_POSITIONS,
    )
    if quantity == 0:
        raise _lot_refusal(row_index, "Quantity", _ZERO_QUANTITY)
    return row_index, symbol, currency, quantity


def _check_lot_rows(
    rows: list[dict[str, str]],
    summaries: list[tuple[int, str, Decimal]],
) -> None:
    """Tie every Open Positions ``Lot`` row to the ``Summary`` row it splits (#1124).

    Runs the four passes listed on :func:`validate_reconciliation`, after
    its other checks. ``Lot`` rows supply no holding or price; the checks
    read one only to tie it to its ``Summary`` row.

    Args:
        rows: The flattened Open Positions rows, every one already checked
            to be a ``Summary`` or ``Lot`` row.
        summaries: Each ``Summary`` row's index, ``Symbol`` and
            ``Quantity``, in row order.

    Raises:
        ParseError: On the first failure. No refusal carries a cell value.
    """
    lots = [
        _read_lot_row(row, row_index=row_index)
        for row_index, row in enumerate(rows)
        if row["DataDiscriminator"] == _LOT
    ]
    lot_symbols = {symbol for _row_index, symbol, _currency, _quantity in lots}
    # Symbol -> its Summary row's index, Currency and Quantity, in row order.
    covering: dict[str, tuple[int, str, Decimal]] = {}
    for row_index, symbol, quantity in summaries:
        if symbol not in lot_symbols:
            # No Lot rows split this Summary row, which reconciliation has read.
            continue
        if symbol in covering:
            raise _lot_refusal(row_index, "Symbol", _DUPLICATE_SYMBOL)
        currency = _non_empty(
            _reconciled_cell(rows[row_index], "Currency", row_index=row_index),
            row_index=row_index,
            column="Currency",
            section=_OPEN_POSITIONS,
        )
        covering[symbol] = (row_index, currency, quantity)

    for row_index, symbol, currency, quantity in lots:
        if symbol not in covering:
            raise _lot_refusal(
                row_index,
                "Symbol",
                "this Lot row's Symbol has no Open Positions Summary row; refusing rather "
                "than dropping a holding that only the lot detail shows.",
            )
        _summary_index, summary_currency, summary_quantity = covering[symbol]
        if currency != summary_currency:
            raise _lot_refusal(
                row_index,
                "Currency",
                "this Lot row's Currency differs from its Summary row's Currency; can't reconcile.",
            )
        # A zero Summary Quantity has no sign, so no Lot row can split it.
        if summary_quantity.is_zero() or quantity.is_signed() != summary_quantity.is_signed():
            raise _lot_refusal(
                row_index,
                "Quantity",
                "this Lot row's Quantity has a different sign from its Summary row's "
                "Quantity; can't reconcile.",
            )

    for symbol, (row_index, _summary_currency, summary_quantity) in covering.items():
        # Add exactly: a sum rounded to the context's precision could hide a lot.
        try:
            with localcontext(prec=MAX_PREC) as context:
                context.traps[Inexact] = True
                total = sum(
                    (
                        quantity
                        for _index, lot_symbol, _currency, quantity in lots
                        if lot_symbol == symbol
                    ),
                    Decimal(0),
                )
        except (DecimalException, MemoryError) as exc:
            raise _lot_refusal(
                row_index,
                "Quantity",
                "the Lot rows for this Summary row's Symbol can't be added up exactly, "
                "so they can't be checked against its Quantity.",
            ) from exc
        if total != summary_quantity:
            raise _lot_refusal(
                row_index,
                "Quantity",
                "the Lot rows for this Summary row's Symbol don't add up exactly to its "
                "Quantity; can't reconcile.",
            )


# An Open Positions Summary row's Value may differ from its Quantity times
# Close Price by at most this much: the Flex PositionValue check's tolerance,
# since both are IBKR's own per-row product of the same two printed cells.
_VALUE_TOLERANCE: Final[Decimal] = Decimal("0.01")


def validate_open_positions_values(open_positions_rows: Iterable[dict[str, str]]) -> None:
    """Check each Open Positions ``Summary`` row's ``Value`` against its cells (#1315).

    A ``Summary`` row's ``Value`` must be within 0.01 of its ``Quantity``
    times its ``Close Price``, as the Flex Query reader checks each row's
    ``PositionValue``. A row that contradicts itself is refused, since the
    tool can't tell which cell is wrong. The product, the difference and its
    magnitude keep every digit
    (:func:`ibkr_fa.parser._exact.distance_from_product`). ``Lot`` rows are
    not checked: no reader takes a price from them.

    The pipeline runs this after :func:`validate_reconciliation`, so every
    row has passed the earlier Open Positions checks: each is a ``Summary``
    or ``Lot`` row of a stock with ``Mult`` ``1``, so the product has no
    multiplier. If a later change admits another ``Mult``, this check must
    multiply by it.

    Args:
        open_positions_rows: Data rows from every
            :class:`KnownActivitySection.OPEN_POSITIONS` block, flattened
            in file order, as :func:`validate_reconciliation` takes them.

    Raises:
        ParseError: If a row's ``DataDiscriminator`` column is missing or is
            neither ``Summary`` nor ``Lot``; or, for a ``Summary`` row, if
            its ``Quantity``, ``Close Price`` or ``Value`` column is missing,
            a cell isn't a finite decimal within its digit limits, the
            arithmetic can't be kept exactly, or ``Value`` is more than 0.01
            from ``Quantity`` times ``Close Price``. No refusal carries a
            cell value, in its message or ``raw_value``.
    """
    for row_index, row in enumerate(open_positions_rows):
        discriminator = _reconciled_cell(row, "DataDiscriminator", row_index=row_index)
        if not _is_summary_row(discriminator, row_index=row_index):
            continue
        quantity = _summary_decimal(row, "Quantity", NumberKind.QUANTITY, row_index=row_index)
        close_price = _summary_decimal(row, "Close Price", NumberKind.PRICE, row_index=row_index)
        value = _summary_decimal(row, "Value", NumberKind.MONEY, row_index=row_index)
        distance = distance_from_product(
            value,
            quantity,
            close_price,
            refusal=partial(
                _precision_refusal,
                section=_OPEN_POSITIONS,
                row_index=row_index,
                column="Value",
                at="the Value arithmetic",
            ),
        )
        if distance > _VALUE_TOLERANCE:
            raise ParseError(
                section=_OPEN_POSITIONS,
                row_index=row_index,
                column="Value",
                message=f"must equal Quantity * Close Price within {_VALUE_TOLERANCE}",
                raw_value="",
            )


def _summary_decimal(
    row: dict[str, str], column: str, kind: NumberKind, *, row_index: int
) -> Decimal:
    """Read an Open Positions row's ``column`` as a finite decimal within ``kind``'s limits."""
    return _decimal(
        _reconciled_cell(row, column, row_index=row_index),
        row_index=row_index,
        column=column,
        kind=kind,
        section=_OPEN_POSITIONS,
    )
