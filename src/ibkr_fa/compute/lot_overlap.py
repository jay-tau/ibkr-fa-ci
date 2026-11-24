"""Filter parsed lots down to those overlapping a target calendar year.

Schedule FA Table A3 reports each foreign asset that the taxpayer
held **at any point** during the calendar year. This module decides,
per lot, whether the lot has any overlap with the requested CY:

* An open lot (still held on Dec 31 of CY) overlaps iff
  ``open_date_time <= Dec 31 of CY``. Open lots in the input come
  from a year-end snapshot the pipeline already validated to have
  ``ReportDate == Dec 31 of CY``, so the time-window predicate is
  effectively automatic. Short-side lots are filtered out — a short
  position is a liability, not a foreign asset to disclose.
* A closed lot overlaps iff
  ``open_date_time <= Dec 31 of CY ∧ close_date_time >= Jan 1 of CY``.
  A lot that opened and closed entirely in a prior year doesn't
  overlap; nor does a lot that opened entirely in a future year.

This module contains **no** FX, no INR conversion, no portal-row
construction — only the time-window filter. Downstream
``compute-fa-a3-*`` modules consume the filtered lots and produce
the per-row INR figures.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ibkr_fa.compute.lot_matching import require_indian_basis
from ibkr_fa.invariants import require
from ibkr_fa.models import ClosedLot, OpenLot, Side


@dataclass(frozen=True, slots=True)
class CyOverlap:
    """Lots overlapping a calendar year, partitioned by open/closed.

    Attributes:
        open_lots: Long open lots whose ``open_date_time`` is on or
            before Dec 31 of the target CY. In v1, since the open
            positions input is a year-end snapshot, this is the input
            list with short-side lots removed.
        closed_lots: Closed lots whose
            ``[open_date_time, close_date_time]`` interval intersects
            the target CY (inclusive of both endpoints).
    """

    open_lots: list[OpenLot]
    closed_lots: list[ClosedLot]


def overlapping_lots_for_cy(
    open_lots: Iterable[OpenLot],
    closed_lots: Iterable[ClosedLot],
    *,
    calendar_year: int,
) -> CyOverlap:
    """Return the lots from each input that overlap ``calendar_year``.

    Args:
        open_lots: Open positions, typically from
            :func:`ibkr_fa.parser.parse_open_positions`.
        closed_lots: Closed-lot rows, typically from
            :func:`ibkr_fa.parser.parse_closed_lots`.
        calendar_year: The CY to filter against (e.g., ``2025`` for
            AY 2026-27).

    Returns:
        :class:`CyOverlap` with two filtered lists. Input order is
        preserved within each list.

    Raises:
        BrokerBasisError: If a broker-matched closed lot's
            holding-period date moved (#599); checked on every input
            lot, before the CY filter. See
            :func:`~ibkr_fa.compute.lot_matching.require_indian_basis`.

    Notes:
        * **Short positions are dropped** from ``open_lots``. Schedule
          FA Table A3 declares foreign asset HOLDINGS; a short
          position is a liability and out of scope.
        * **Closed-lot side filtering happens at parse time.**
          :func:`ibkr_fa.parser.parse_closed_lots` rejects CLOSED_LOT
          rows with ``Buy/Sell == "BUY"`` (short cover) so every
          :class:`ClosedLot` reaching this function is a long-side
          closure by construction.
        * Inclusive boundary semantics: a lot opened on Dec 31 of CY
          is INCLUDED (it overlapped CY for one day). A lot closed on
          Jan 1 of CY is INCLUDED (it overlapped CY for one day).
        * **Closed lots are also capped at Dec 31 of CY.** If an
          input ``closed_lots`` list contains a disposal AFTER the
          target CY (e.g., the Flex Query's Trades date range
          extends past year-end), that same holding ALSO appears in
          ``open_lots`` from the Dec-31 snapshot the pipeline requires.
          Including it in both partitions would double-count the lot
          in downstream FA-A3 row construction. The cap drops the
          closed-lot row in that case; the open-lot row carries the
          asset through the year-end snapshot.
    """
    cy_start = date(calendar_year, 1, 1)
    cy_end = date(calendar_year, 12, 31)
    all_closed = list(closed_lots)
    require_indian_basis(all_closed)
    open_overlap = [
        lot for lot in open_lots if lot.side == Side.LONG and lot.open_date_time <= cy_end
    ]
    closed_overlap = [
        lot
        for lot in all_closed
        if lot.open_date_time <= cy_end and cy_start <= lot.close_date_time <= cy_end
    ]
    # Postconditions: defence-in-depth verification that the filter
    # produced lots matching the documented contract. Free given the
    # comprehensions above, but the assertions guard against future
    # refactors that might subtly change the predicates.
    for lot in open_overlap:
        require(
            lot.side == Side.LONG,
            what="overlapping_lots_for_cy post: every output open lot must be Side.LONG",
            side=lot.side,
            symbol=lot.symbol,
        )
        require(
            lot.open_date_time <= cy_end,
            what=(
                "overlapping_lots_for_cy post: every output open lot's "
                "open_date_time must be on or before Dec 31 of CY"
            ),
            open_date_time=lot.open_date_time,
            cy_end=cy_end,
        )
    for lot in closed_overlap:
        require(
            cy_start <= lot.close_date_time <= cy_end,
            what=(
                "overlapping_lots_for_cy post: every output closed lot's "
                "close_date_time must be within [Jan 1, Dec 31] of CY"
            ),
            close_date_time=lot.close_date_time,
            cy_start=cy_start,
            cy_end=cy_end,
        )
    return CyOverlap(open_lots=open_overlap, closed_lots=closed_overlap)
