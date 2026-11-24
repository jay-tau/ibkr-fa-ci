"""Build Schedule FA Table A3 rows from parsed lots + FX policy + config.

Composes the per-stage compute primitives (:mod:`lot_overlap`,
:mod:`initial_value`, :mod:`closing_value`, :mod:`dividends`) and
reference-data lookups (:mod:`ibkr_fa.reference.country_codes`)
into a sequence of typed :class:`FaA3Row` records ready for the
writer.

This module makes the v1 "missing column" decision concrete: one
portal field the compute layer doesn't populate from the IBKR
statement alone — :attr:`FaA3Row.peak_value_inr` — is computed from an
external daily-close price source (issue #14) when one is supplied via
``prices_by_ticker``, and left :data:`None` (rendered as an empty cell
with a CLI warning) for any lot whose peak cannot be established with
certainty, with the reason in :attr:`FaA3Row.peak_blank_reason` (see
:mod:`ibkr_fa.compute.fa_a3_peak_value` for the refusal conditions).
:attr:`FaA3Row.gross_amount_paid_credited_inr` is conditionally
populated from the optional ``dividends_by_symbol_inr`` kwarg —
when the pipeline parsed a Dividends section (Activity Statement
post-#31), this kwarg is provided and the column is filled; when
absent (Flex Query inputs), the kwarg defaults to ``None`` and the
column is left blank. :func:`build_fa_a3_rows` gives each symbol's
total to its first lot. A source whose credits carry a
:attr:`~ibkr_fa.models.Dividend.holding_date` instead splits each
credit across the lots :func:`held_on` that date with
:func:`split_a3_credit_by_holding`. That pro-rata-by-holding rule is
ibkr-fa's allocation policy (ADR 0006); only Robinhood uses it today.
:attr:`FaA3Row.gross_proceeds_from_sale_inr` is computed via
:func:`gross_proceeds_inr_for_lot`: closed lots with proven pre-fee
consideration, or Robinhood's recorded Amount filed as consideration
(#1383), use ``gross_proceeds_money * TTBR(December 31)``, substituting
``gross_proceeds_money_exact`` when the Decimal rounds its share; closed
lots without gross proceeds (each source's basis is on
:attr:`~ibkr_fa.models.ClosedLot.gross_proceeds_money`) get ``None``
for manual fill; open lots get
:class:`Decimal` 0 (no disposal).
The CLI is responsible for surfacing a loud per-column warning
enumerating which fields the user must fill in manually before
pasting into the portal.

It also handles three per-row identity fields:

* ``address_of_entity`` -> a source-proved or configured per-ISIN entity
  value when supplied, otherwise blank (#1289): the holding's country doesn't
  prove the issuer's address. Generic IBKR output warns when it is missing.
  The Robinhood pipeline supplies validated exact-ISIN GLEIF legal addresses.
* ``nature_of_entity`` -> an explicit source-specific value or per-ISIN
  ``[[fa.entities]]`` value. Generic IBKR rows stay blank with a loud
  manual-fill warning; broker instrument classifications do not prove
  the entity's legal nature.
* ``zip_code`` -> the matching source-proved or configured postal code,
  otherwise blank. Generic IBKR output warns when it is missing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from itertools import chain
from typing import Final

from ibkr_fa.compute.closing_value import closing_value_inr_for_lot
from ibkr_fa.compute.exact_inr import exact_fraction
from ibkr_fa.compute.fa_a3_gross_proceeds import gross_proceeds_inr_for_lot
from ibkr_fa.compute.fa_a3_peak_value import resolve_fa_a3_peak
from ibkr_fa.compute.fa_peak import warn_if_inr_peak_currency
from ibkr_fa.compute.initial_value import initial_value_inr_for_lot
from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.config import FaEntityOverride
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models import ClosedLot, FaA3PeakBlankReason, FaA3Row, OpenLot
from ibkr_fa.prices.series import DEFAULT_MAX_PRICE_STALENESS_DAYS, PriceSeries
from ibkr_fa.reference.country_codes import lookup_country
from ibkr_fa.types import CountryCode, Isin, PeakBasis, PeakCurrency, Ticker

# Default ZIP remains blank. Neither address (#1289) nor nature of entity has a
# generic default: the holding's country doesn't prove the issuer's address,
# and IBKR's instrument type does not establish the issuer's legal form.
_DEFAULT_ZIP_CODE: Final[str] = ""


def held_on(open_date: date, close_date: date | None, day: date) -> bool:
    """Whether a lot open from ``open_date`` to ``close_date`` is held at the end of ``day``.

    A lot opened on ``day`` is held; a lot closed on ``day`` is not.
    ``close_date=None`` means the lot is still open.
    """
    return open_date <= day and (close_date is None or day < close_date)


def split_a3_credit_by_holding(
    amount: Fraction,
    quantities: Sequence[Decimal | Fraction],
) -> tuple[Fraction, ...] | None:
    """Split one A3 credit across the lots held on its holding date.

    Pro rata by quantity, in caller order: each holder gets the exact
    rational ``amount * quantity / total quantity`` (#1243), so the shares
    sum to ``amount`` exactly. A ``Fraction`` quantity is a lot's exact
    quantity after a pro-rata sale (#1263). ``None`` if a Decimal quantity
    has no :func:`~ibkr_fa.compute.exact_inr.exact_fraction`: it needs more
    digits than exact arithmetic allows.

    Raises:
        InvariantError: If ``quantities`` is empty, their total is not
            positive, or the shares do not conserve ``amount``.
    """
    require(len(quantities) > 0, what="an A3 credit split needs at least one holder")
    exact_quantities: list[Fraction] = []
    for quantity in quantities:
        exact = quantity if isinstance(quantity, Fraction) else exact_fraction(quantity)
        if exact is None:
            return None
        exact_quantities.append(exact)
    total_quantity = sum(exact_quantities, Fraction())
    require(
        total_quantity > 0,
        what="an A3 credit split needs a positive total holder quantity",
        total_quantity=total_quantity,
    )
    shares = tuple(amount * quantity / total_quantity for quantity in exact_quantities)
    remaining = amount - sum(shares, Fraction())
    require(
        remaining == 0,
        what="an A3 credit split did not conserve the credit amount",
        remaining=remaining,
    )
    return shares


def _build_one(
    lot: OpenLot | ClosedLot,
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
    nature_of_entity: str | None,
    resolved_nature_of_entity: str | None,
    zip_code: str,
    gross_amount_paid_credited_inr: Decimal | None,
    peak: Decimal | FaA3PeakBlankReason,
    entity_override: FaEntityOverride | None = None,
) -> FaA3Row:
    """Build a single :class:`FaA3Row` from one lot.

    Shared between the open-lot and closed-lot iterations of
    :func:`build_fa_a3_rows`. The lot type determines:

    * ``closing_value_inr`` (computed for OpenLot; ``Decimal("0")``
      short-circuit for ClosedLot — see
      :func:`closing_value_inr_for_lot`).
    * ``audit_close_price_usd`` (populated for OpenLot, ``None`` for
      ClosedLot) and ``audit_ttbr_dec31`` (also populated for a
      ClosedLot when its Schedule FA gross credits use that rate).
    * ``audit_disposal_date`` (``None`` for OpenLot,
      ``close_date_time`` for ClosedLot).
    * ``gross_proceeds_from_sale_inr`` (``Decimal("0")`` for OpenLot — see
      :func:`gross_proceeds_inr_for_lot`).

    The remaining fields are common to both lot types.
    """
    country = lookup_country(lot.issuer_country_code)
    initial_inr = initial_value_inr_for_lot(lot, fx_policy=fx_policy)
    closing_inr = closing_value_inr_for_lot(lot, calendar_year=calendar_year, fx_policy=fx_policy)
    gross_proceeds_inr = gross_proceeds_inr_for_lot(
        lot,
        calendar_year=calendar_year,
        fx_policy=fx_policy,
    )
    ttbr_acquisition = fx_policy.rate_for(lot.open_date_time)

    if isinstance(lot, OpenLot):
        audit_close_price_usd = lot.mark_price
        audit_ttbr_dec31 = fx_policy.rate_for(date(calendar_year, 12, 31))
        audit_disposal_date = None
    else:
        audit_close_price_usd = None
        gross_credit_uses_dec31 = any(
            value is not None and value > 0
            for value in (gross_amount_paid_credited_inr, gross_proceeds_inr)
        )
        audit_ttbr_dec31 = (
            fx_policy.rate_for(date(calendar_year, 12, 31)) if gross_credit_uses_dec31 else None
        )
        audit_disposal_date = lot.close_date_time

    # Per-ISIN overrides supply the address and ZIP, and optionally the name.
    # Without one the address stays blank and ZIP uses the call's value (#1289).
    address_of_entity = entity_override.address if entity_override else ""
    if address_of_entity is None:
        # An entry without address stops its run before any row is built (#1449).
        raise invariant_error(what="an [[fa.entities]] entry without address reached an A3 row")
    row_zip_code = entity_override.zip_code if entity_override else zip_code
    name_of_entity = (
        entity_override.name
        if (entity_override is not None and entity_override.name is not None)
        else lot.description
    )
    row_nature = (
        resolved_nature_of_entity
        if resolved_nature_of_entity is not None
        else (
            entity_override.nature_of_entity
            if entity_override is not None and entity_override.nature_of_entity is not None
            else (nature_of_entity or "")
        )
    )

    return FaA3Row(
        country_name=country.country_name,
        country_code=country.cbdt_code,
        name_of_entity=name_of_entity,
        address_of_entity=address_of_entity,
        zip_code=row_zip_code,
        nature_of_entity=row_nature,
        date_of_acquiring=lot.open_date_time,
        initial_value_inr=initial_inr,
        peak_value_inr=None if isinstance(peak, FaA3PeakBlankReason) else peak,
        closing_value_inr=closing_inr,
        gross_amount_paid_credited_inr=gross_amount_paid_credited_inr,
        gross_proceeds_from_sale_inr=gross_proceeds_inr,
        source_isin=lot.isin,
        source_ticker=lot.symbol,
        source_country_iso2=CountryCode(lot.issuer_country_code),
        audit_cost_basis_usd=lot.cost_basis_money,
        audit_acquisition_date=lot.open_date_time,
        audit_close_price_usd=audit_close_price_usd,
        audit_ttbr_acquisition=ttbr_acquisition,
        audit_ttbr_dec31=audit_ttbr_dec31,
        audit_disposal_date=audit_disposal_date,
        peak_blank_reason=peak if isinstance(peak, FaA3PeakBlankReason) else None,
    )


def build_fa_a3_rows(
    overlap: CyOverlap,
    *,
    calendar_year: int,
    fx_policy: FxPolicy,
    nature_of_entity: str | None = None,
    nature_by_isin: Mapping[Isin, str] | None = None,
    zip_code: str = _DEFAULT_ZIP_CODE,
    dividends_by_symbol_inr: dict[Ticker, Decimal] | None = None,
    prices_by_ticker: Mapping[Ticker, PriceSeries] | None = None,
    prices_fetched: bool = False,
    max_price_staleness_days: int = DEFAULT_MAX_PRICE_STALENESS_DAYS,
    entity_overrides: Mapping[Isin, FaEntityOverride] | None = None,
    peak_basis: PeakBasis = "close",
    peak_currency: PeakCurrency = "native",
    refuse_disposal_day_peak: bool = False,
) -> list[FaA3Row]:
    """Build all Schedule FA Table A3 rows for the calendar year.

    Pure function: no I/O beyond what ``fx_policy.rate_for`` does
    internally (and that's already cached). Open lots are emitted
    before closed lots, preserving the input order within each
    partition.

    Args:
        overlap: The filtered lots from
            :func:`ibkr_fa.compute.overlapping_lots_for_cy`. v1 uses
            its full output as the input to A3.
        calendar_year: The CY being reported (e.g., ``2025`` for
            AY 2026-27). Drives the year-end TTBR lookup and the
            ``audit_ttbr_dec31`` field for open lots.
        fx_policy: The :class:`FxPolicy` to use for INR conversion.
            Same instance is used for every per-lot lookup so caching
            inside the policy is effective.
        nature_of_entity: Explicit source-specific free-text label for the
            portal's "Nature of entity" cell. ``None`` leaves generic IBKR
            rows blank unless the matching per-ISIN entity override supplies
            a value. Per-ISIN values take precedence.
        nature_by_isin: Final per-ISIN legal-nature values for source-specific
            pipelines. These take precedence over configured entity values
            without changing address or ZIP ownership.
        zip_code: Issuer postal code for rows without an entity override.
            Default empty, a manual-fill gap.
        dividends_by_symbol_inr: Per-symbol INR dividend totals for
            the CY, typically from
            :func:`ibkr_fa.compute.aggregate_dividends_by_symbol_inr`.
            When provided, each symbol's full total is attributed to
            the symbol's first open lot, else its first closed lot, each
            list in input order; the symbol's other rows get
            :class:`Decimal` 0. Symbols not in the
            map (or lots whose symbol has no in-CY dividend) also
            get 0. This preserves the per-symbol total (sum across
            rows = correct) while never leaving a row blank (which
            would fail the portal's "whole min 0" validation —
            issue #137 Tier-1 P1.1 and P1.2). When the kwarg is
            ``None`` (default — e.g., Flex Query input without a
            Dividends section), every row's column 10 is left
            ``None`` and the CLI surfaces it as a manual-fill
            warning. Issue #31. Robinhood instead splits each credit
            with :func:`split_a3_credit_by_holding` and passes one lot
            per call, so first-lot attribution has no effect there.
        prices_by_ticker: Per-ticker :class:`PriceSeries` of raw daily
            closes used to compute each lot's
            :attr:`FaA3Row.peak_value_inr` (issue #14). Each lot's
            peak is resolved via
            :func:`ibkr_fa.compute.fa_a3_peak_value.resolve_fa_a3_peak`
            (the per-symbol series is looked up by ``lot.symbol``); a
            lot whose ticker is absent (or every lot, when this is
            ``None``, the default), or whose peak can't be
            established with certainty, gets ``None`` (manual-fill)
            and its :class:`FaA3PeakBlankReason` (#962).
            Exception: under the close basis, an open lot acquired on
            Dec-31 takes its closing value, with or without a series.
        prices_fetched: Whether ``prices_by_ticker`` was loaded with
            price fetching on. A lot whose ticker has no series is
            ``NO_USD_SERIES`` when it was, else ``PRICES_NOT_FETCHED``
            (``--no-fetch-prices``, or no prices supplied).
        max_price_staleness_days: Carry-forward ceiling passed through
            to the peak compute (a price gap larger than this -> that
            lot's peak is ``None``).
        peak_currency: ``"native"`` (default) selects the foreign-currency
            peak before conversion. ``"inr"`` selects the maximum daily
            INR-converted value and emits a warning.
        refuse_disposal_day_peak: Passed to
            :func:`ibkr_fa.compute.fa_a3_peak_value.resolve_fa_a3_peak`. When
            true, a closed lot's close window includes its disposal day and a
            peak selected on that day is ``None`` (manual-fill). Fidelity CMA
            sets it for SPAXX (#622).

    Returns:
        ``list[FaA3Row]`` with one row per lot, open lots first,
        each lot's input order preserved within its partition.

    Raises:
        CountryCodeNotFoundError: If any lot's
            ``issuer_country_code`` is not in
            ``src/ibkr_fa/reference/itr_country_codes.csv``. The
            error is a fixed lookup refusal naming the CSV to extend;
            it doesn't name the ISO code (#1368).
        MissingFxRateError: Propagated from
            :meth:`FxPolicy.rate_for` if any lot's
            ``open_date_time`` (or ``Dec 31 of calendar_year`` for
            an open lot's year-end TTBR audit field) cannot be
            sourced.
    """
    warn_if_inr_peak_currency(peak_currency)

    # Build the per-row attribution map. When dividends data is
    # available (kwarg not None), give every row a concrete Decimal
    # (full per-symbol total to the first lot of that symbol, zero
    # to the rest). When kwarg is None, every row gets None — the
    # pre-#31 manual-fill warning behaviour is preserved.
    #
    # Issue #137 Tier-1 P1.1: previously the per-symbol total was
    # naively assigned to EVERY lot of that symbol, leading to
    # N x double-counting at portal-sum time. Attributing to the
    # first lot only preserves the symbol total without inflation.
    #
    # Issue #137 Tier-1 P1.2: previously, symbols absent from the
    # dividends dict got None per row, which created the "some rows
    # blank, some populated" pattern that suppressed the column-10
    # manual-fill warning while leaving blank cells the portal then
    # rejects. Defaulting to Decimal("0") for absent symbols when
    # we DO have dividend data fixes this — every row gets a value
    # the portal accepts.
    per_row_dividend: list[Decimal | None] = []
    if dividends_by_symbol_inr is None:
        per_row_dividend = [None] * (len(overlap.open_lots) + len(overlap.closed_lots))
    else:
        symbols_seen: set[Ticker] = set()
        all_lots: list[OpenLot | ClosedLot] = [*overlap.open_lots, *overlap.closed_lots]
        for lot in all_lots:
            symbol_total = dividends_by_symbol_inr.get(lot.symbol, Decimal("0"))
            if lot.symbol in symbols_seen or symbol_total == 0:
                per_row_dividend.append(Decimal("0"))
            else:
                per_row_dividend.append(symbol_total)
                symbols_seen.add(lot.symbol)

    rows: list[FaA3Row] = []

    def _peak_for(lot: OpenLot | ClosedLot) -> Decimal | FaA3PeakBlankReason:
        series = prices_by_ticker.get(lot.symbol) if prices_by_ticker is not None else None
        # Recompute closing here (cheap, deterministic) so the resolver compares
        # the fetched series with the SAME closing value _build_one files.
        closing_inr = closing_value_inr_for_lot(
            lot, calendar_year=calendar_year, fx_policy=fx_policy
        )
        return resolve_fa_a3_peak(
            lot,
            series=series,
            fx_policy=fx_policy,
            calendar_year=calendar_year,
            closing_inr=closing_inr,
            basis=peak_basis,
            peak_currency=peak_currency,
            max_price_staleness_days=max_price_staleness_days,
            refuse_disposal_day_peak=refuse_disposal_day_peak,
            prices_fetched=prices_fetched,
        )

    for lot in chain(overlap.open_lots, overlap.closed_lots):
        rows.append(
            _build_one(
                lot,
                calendar_year=calendar_year,
                fx_policy=fx_policy,
                nature_of_entity=nature_of_entity,
                resolved_nature_of_entity=(
                    nature_by_isin.get(lot.isin) if nature_by_isin else None
                ),
                zip_code=zip_code,
                gross_amount_paid_credited_inr=per_row_dividend[len(rows)],
                peak=_peak_for(lot),
                entity_override=entity_overrides.get(lot.isin) if entity_overrides else None,
            )
        )
    # Postcondition: one row per input lot, no implicit drops. Free
    # given the straight-line iteration above, but the assertion
    # locks the contract against future refactors that might add
    # filtering / deduplication here without the caller noticing.
    expected_count = len(overlap.open_lots) + len(overlap.closed_lots)
    require(
        len(rows) == expected_count,
        what=(
            "build_fa_a3_rows postcondition: output row count must equal "
            "len(overlap.open_lots) + len(overlap.closed_lots)"
        ),
        actual=len(rows),
        expected=expected_count,
        open_lots=len(overlap.open_lots),
        closed_lots=len(overlap.closed_lots),
    )
    return rows


@dataclass(frozen=True, slots=True)
class FaA3ManualFillGaps:
    """The A3 cells a run leaves blank for the user to fill by hand.

    A blank cell can't be filed: both vendored CBDT schemas require every
    Table A3 element, each text element at least one character long. So the
    CLI names each column any row leaves blank.

    Attributes:
        name_of_entity: ``(ticker, ISIN)`` of each holding whose Name of
            entity is whitespace only (#1369). An empty name refuses at the row.
        address_of_entity: ``(ticker, ISIN)`` of each holding whose Address of
            entity is blank (#1289).
        zip_code: ``(ticker, ISIN)`` of each holding whose ZIP Code is blank.
        nature_of_entity: ``(ticker, ISIN)`` of each holding whose Nature of
            entity is blank.
        peak: ``(ticker, ISIN, reason)`` of each holding whose peak value is
            blank, with the engine's reason (#962).
        no_rows: The run has no rows (the user enters any row by hand, peak
            included).
        dividends: The gross amount paid/credited column is unresolved.
        gross_proceeds: ``(ticker, ISIN)`` of each holding whose gross
            proceeds are blank.
    """

    name_of_entity: frozenset[tuple[Ticker, Isin]]
    address_of_entity: frozenset[tuple[Ticker, Isin]]
    zip_code: frozenset[tuple[Ticker, Isin]]
    nature_of_entity: frozenset[tuple[Ticker, Isin]]
    peak: frozenset[tuple[Ticker, Isin, FaA3PeakBlankReason]]
    no_rows: bool
    dividends: bool
    gross_proceeds: frozenset[tuple[Ticker, Isin]]


def name_of_entity_is_blank(row: FaA3Row) -> bool:
    """Whether ``row``'s Name of entity is whitespace only (#1369).

    The row refuses an empty name; output-format checks still apply.
    """
    return row.name_of_entity.strip() == ""


def fa_a3_manual_fill_gaps(
    rows: Sequence[FaA3Row], *, dividends_resolved: bool
) -> FaA3ManualFillGaps:
    """Find the A3 cells ``rows`` leave blank.

    Dividends are column-wide (``dividends_resolved``); name, address, ZIP,
    nature, peak and proceeds are per-row. A whitespace-only name, address or
    ZIP is blank, as the CBDT JSON fragment treats it.
    """
    return FaA3ManualFillGaps(
        name_of_entity=frozenset(
            (row.source_ticker, row.source_isin) for row in rows if name_of_entity_is_blank(row)
        ),
        address_of_entity=frozenset(
            (row.source_ticker, row.source_isin)
            for row in rows
            if row.address_of_entity.strip() == ""
        ),
        zip_code=frozenset(
            (row.source_ticker, row.source_isin) for row in rows if row.zip_code.strip() == ""
        ),
        nature_of_entity=frozenset(
            (row.source_ticker, row.source_isin) for row in rows if row.nature_of_entity == ""
        ),
        peak=frozenset(
            (row.source_ticker, row.source_isin, row.peak_blank_reason)
            for row in rows
            if row.peak_blank_reason is not None
        ),
        no_rows=not rows,
        dividends=not dividends_resolved,
        gross_proceeds=frozenset(
            (row.source_ticker, row.source_isin)
            for row in rows
            if row.gross_proceeds_from_sale_inr is None
        ),
    )


def fa_a3_peak_blank_rows(
    rows: Sequence[FaA3Row],
) -> tuple[tuple[FaA3PeakBlankReason, tuple[int, ...]], ...]:
    """Each reason a row's peak is blank, in reason order, with the 1-based
    positions of the rows it blanks (#1549, #1552)."""
    return tuple(
        (reason, positions)
        for reason in FaA3PeakBlankReason
        if (
            positions := tuple(
                index for index, row in enumerate(rows, start=1) if row.peak_blank_reason is reason
            )
        )
    )
