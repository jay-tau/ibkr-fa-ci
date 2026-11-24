"""The Dec-31 NetBenefits ESPP positions for Schedule FA Table A3 (#632).

The open-lots CSV is a download-dated snapshot and the year-end statement is
unpriced, so the Dec-31 position is rebuilt from the lots, reconciled to the
trade confirmations, and valued at a resolved Dec-31 close. Two steps, so every
lot-reconstruction refusal fires before the caller loads prices:

* :func:`reconstruct_espp_dec31_lots` folds next-CY gap-window sales back into
  their Dec-31 purchase and refuses every other CY-overlapping closed lot. It
  needs no prices.
* :func:`resolve_espp_dec31_positions` joins each lot to its confirmation,
  resolves and anchors its Dec-31 close, and values it.

:func:`espp_discount_mismatch` and :func:`confirmation_config_mismatch` are
shared with the ESPP capital-gains adapter, which raises its own error type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.dec31_close import (
    CloseSource,
    ClosingPriceError,
    Dec31SessionRoutes,
    ResolvedClose,
    per_share_tolerance,
    resolve_dec31_close,
)
from ibkr_fa.compute.exact_inr import checked_addition, checked_subtraction
from ibkr_fa.compute.holding_valuation import value_holding_lot
from ibkr_fa.config import NetBenefitsConfig, NetBenefitsPriceOverride
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.invariants import require
from ibkr_fa.models import OpenLot
from ibkr_fa.models.holding_lot import HoldingLot
from ibkr_fa.models.open_lot import Side
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot, EspLot
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.types import PeakBasis

# Year-end window for selecting a config price override, matching
# resolve_dec31_close's default staleness ceiling, so a near-date override is
# passed on (and refused if it isn't the required session), not ignored.
_OVERRIDE_STALENESS_DAYS: Final[int] = 3

# The routes a Dec-31 close refusal names (#1549).
_PRICE_OVERRIDES: Final[str] = "[[netbenefits.price_overrides]]"
_BY_HAND: Final[str] = "complete the NetBenefits Schedule FA by hand"
_FMV_PER_SHARE: Final[str] = (
    "the purchase confirmation's Market Value at Purchase divided by the shares purchased, "
    "to the cent"
)


def _market_close_route(session: date) -> str:
    return (
        f"the required session is {session.isoformat()}: move this security's price cache "
        "file, and any .lock beside it, out of the price cache directory and rerun with price "
        "fetching on; if the fresh series still has no usable close for that session, "
        f"{_BY_HAND}"
    )


def _override_date_route(session: date) -> str:
    return (
        f"set that {_PRICE_OVERRIDES} entry's price_date to {session.isoformat()}, the "
        f"required session, and its price_per_share_usd to {_FMV_PER_SHARE}; if the rounded "
        f"value is refused as price_per_share_usd, or the market close then differs, {_BY_HAND}"
    )


def _mismatch_route(session: date) -> str:
    return (
        f"check the price_per_share_usd of the {_PRICE_OVERRIDES} entry dated "
        f"{session.isoformat()} against {_FMV_PER_SHARE}, and correct it if it differs; if "
        "the rounded value is refused as price_per_share_usd, or it matches and the "
        f"market close still differs, {_BY_HAND}"
    )


_SESSION_ROUTES: Final[Dec31SessionRoutes] = Dec31SessionRoutes(
    market_close=_market_close_route,
    override_date=_override_date_route,
    mismatch=_mismatch_route,
)


def _anchor_route(source: CloseSource) -> str:
    """The route for a resolved close that differs from the confirmation FMV."""
    if source is CloseSource.OVERRIDE:
        return (
            f"set the price_per_share_usd of the {_PRICE_OVERRIDES} entry for this close to "
            f"{_FMV_PER_SHARE}; if the rounded value is refused as price_per_share_usd, "
            f"or the market close then differs, {_BY_HAND}"
        )
    return _BY_HAND


def _missing_close_route(
    valuation_date: date, *, peak_basis: PeakBasis, prices_fetched: bool
) -> str:
    """The route for no override and no price series (#1565). An override
    gives only the close, so under the high peak basis the route also names
    how the A3 peak gets its intraday high."""
    override = f"supply a {_PRICE_OVERRIDES} entry for {valuation_date.isoformat()}"
    if peak_basis == "close":
        return override
    high = (
        'the A3 peak under [schedule_fa] peak_basis = "high" needs that day\'s intraday '
        "high, which only a price series supplies"
    )
    if prices_fetched:
        return f"{override}, then enter the A3 peak by hand: {high}"
    return f"rerun without --no-fetch-prices: {high}, and the fetched series gives the close too"


class NetBenefitsFaError(IbkrFaError):
    """A NetBenefits Table A3 run cannot proceed with certainty.

    Raised for discovery / reconciliation refusals (missing or duplicate input,
    a lot that can't be matched or proven held on Dec-31, a config gap). The CLI
    maps it (like every :class:`IbkrFaError`) to the domain-error exit code.
    """


@dataclass(frozen=True, slots=True)
class EsppDiscountMismatch:
    """A confirmation price outside the tolerance of ``FMV/share x (1 - discount)``.

    Attributes:
        fmv_per_share_usd: The confirmation's FMV per share.
        expected_price_usd: ``fmv_per_share_usd x (1 - discount)``.
        deviation_usd: ``|price - expected_price_usd|``.
        tolerance_usd: The allowed deviation.
    """

    fmv_per_share_usd: Decimal
    expected_price_usd: Decimal
    deviation_usd: Decimal
    tolerance_usd: Decimal


def espp_discount_mismatch(
    purchase: EspPurchase, discount_pct: Decimal
) -> EsppDiscountMismatch | None:
    """Check ``purchase price ~= FMV/share x (1 - discount)``; None when it holds.

    The confirmation parser proves ``shares x price == contribution`` but defers
    this identity (it needs the plan discount %, which lives only in config), so
    a self-consistent but wrong FMV would otherwise be accepted.

    ponytail: assumes a NO-LOOKBACK plan (MSFT's: discount off the purchase-date
    FMV). A lookback plan prices off min(offering-start, purchase) FMV and would
    false-refuse here; upgrade = thread the offering-start FMV when such a
    security is ever configured.
    """
    fmv_per_share = purchase.fmv_per_share_usd
    expected = fmv_per_share * (Decimal("1") - discount_pct / Decimal("100"))
    # FMV/share is FMV_total/shares (shares carry up to a 3-4 dp half-ULP), so
    # allow a per-share half-cent envelope rather than demanding exact equality.
    tol = fmv_per_share * Decimal("0.0005") + Decimal("0.01")
    deviation = abs(purchase.purchase_price_per_share_usd - expected)
    if deviation > tol:
        return EsppDiscountMismatch(fmv_per_share, expected, deviation, tol)
    return None


def confirmation_label(purchase_date: date, purchase_dates: Iterable[date]) -> str:
    """Name the confirmation for ``purchase_date`` by its 1-based place among
    the unique ``purchase_dates`` in date order, such as ``confirmation 2 (by
    purchase date)``."""
    number = sum(1 for other in purchase_dates if other <= purchase_date)
    return f"confirmation {number} (by purchase date)"


def confirmation_config_mismatch(purchase: EspPurchase, cfg: NetBenefitsConfig) -> str | None:
    """Name the first of participant number, symbol and CUSIP on which the
    confirmation differs from config, in fixed text without either value; None
    if none."""
    sec = cfg.security
    if purchase.participant_no != cfg.participant_no:
        return "the PARTICIPANT NO. differs from netbenefits.participant_no in user.toml"
    if purchase.symbol != sec.instrument.symbol:
        return "the SYMBOL differs from netbenefits.security.symbol in user.toml"
    if purchase.cusip != sec.cusip:
        return "the CUSIP differs from netbenefits.security.cusip in user.toml"
    return None


def check_confirmation_belongs_to_config(
    purchase: EspPurchase, cfg: NetBenefitsConfig, *, label: str
) -> None:
    """Refuse a confirmation whose participant/symbol/CUSIP doesn't match
    config, naming it by ``label`` (see :func:`confirmation_label`)."""
    problem = confirmation_config_mismatch(purchase, cfg)
    if problem is not None:
        raise NetBenefitsFaError(f"{label}: {problem}")


@dataclass(frozen=True, slots=True)
class EsppDec31Lot:
    """One lot held at Dec-31 and the lots-CSV data rows it was built from.

    Attributes:
        lot: The open lot, or the position folded from it and its gap-window
            closed lots.
        open_row: The open lot's 1-based data row in the open-lots CSV, or
            ``None`` for a purchase fully sold the next CY.
        closed_rows: The 1-based data rows in the closed-lots CSV of the
            gap-window lots folded in, in file order.
    """

    lot: EspLot
    open_row: int | None
    closed_rows: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        require(
            self.open_row is not None or bool(self.closed_rows),
            what="an ESPP Dec-31 lot needs an open-lots or a closed-lots data row",
        )

    @property
    def subject(self) -> str:
        """The lot as refusals name it, such as ``open-lots data row 2`` or
        ``the position folded from open-lots data row 2 and closed-lots data
        row 1``."""
        if not self.closed_rows:
            return f"open-lots data row {self.open_row}"
        parts = [] if self.open_row is None else [f"open-lots data row {self.open_row}"]
        parts.append(lots_data_rows("closed-lots", self.closed_rows))
        return "the position folded from " + " and ".join(parts)


def lots_data_rows(csv: str, rows: Sequence[int]) -> str:
    """Name 1-based lots-CSV data rows, such as ``closed-lots data row 1`` or
    ``closed-lots data rows 1, 2 and 4`` for ``csv`` ``closed-lots``."""
    if len(rows) == 1:
        return f"{csv} data row {rows[0]}"
    return f"{csv} data rows {', '.join(map(str, rows[:-1]))} and {rows[-1]}"


@dataclass(frozen=True, slots=True)
class EsppDec31Position:
    """One Dec-31 ESPP position, valued and matched to its confirmation.

    Attributes:
        lot: The position valued at ``close``, with the FMV cost basis.
        close: The resolved Dec-31 close and its provenance.
        purchase: The trade confirmation the lot reconciled to.
    """

    lot: OpenLot
    close: ResolvedClose
    purchase: EspPurchase


@dataclass(frozen=True, slots=True)
class EsppDec31Positions:
    """The resolved Dec-31 positions plus what Table A2 needs.

    Attributes:
        positions: One :class:`EsppDec31Position` per lot, in input order.
        securities_usd: The exact sum of the resolved Dec-31 closes (#1168).
        security_name: The issuer name on the confirmations.
    """

    positions: tuple[EsppDec31Position, ...]
    securities_usd: Decimal
    security_name: str


def _cy_open_lots(open_lots: Iterable[EspLot], cy_end: date) -> list[tuple[int, EspLot]]:
    """CY-filter open lots to ``acquire_date <= cy_end``, each with its 1-based
    data row; require unique dates."""
    kept = [(row, lot) for row, lot in enumerate(open_lots, start=1) if lot.acquire_date <= cy_end]
    seen: dict[date, int] = {}
    for row, lot in kept:
        if lot.acquire_date in seen:
            raise NetBenefitsFaError(
                f"{lots_data_rows('open-lots', [seen[lot.acquire_date], row])} share an "
                "acquisition date; the bundle is ambiguous"
            )
        seen[lot.acquire_date] = row
    return kept


def partition_cy_closed_lots(
    closed_lots: Iterable[EspClosedLot], cy_end: date
) -> list[tuple[int, EspClosedLot]]:
    """Return the reconstructable *gap-window* closed lots, each with its 1-based
    data row; refuse the rest.

    Schedule FA Table A3 discloses every foreign asset HELD during the calendar
    year ("held at any time during the relevant calendar year ending Dec-31").
    A closed lot is a GAP-WINDOW lot iff it was acquired ON Dec-31 (``cy_end``)
    and disposed strictly AFTER it: those shares were held at the year-end close,
    so they belong in the Dec-31 position even though a download-dated open-lots
    snapshot (taken after the later sale) no longer shows them. These are folded
    back into their Dec-31 purchase by :func:`reconstruct_dec31_positions`.

    Every OTHER closed lot overlapping the CY — a mid-year acquisition, or a
    disposal on or before Dec-31 (including a same-day Dec-31 round-trip) — is a
    disclosable *intra-CY* holding that needs the closed-lot proceeds/peak path
    (#I13, not wired yet). Rather than silently emit an A3 that omits it, refuse.
    """
    cy_start = date(cy_end.year, 1, 1)
    gap_window: list[tuple[int, EspClosedLot]] = []
    for row, lot in enumerate(closed_lots, start=1):
        if not (lot.acquire_date <= cy_end and lot.sold_or_transferred_date >= cy_start):
            continue  # not held during the CY at all → neither A3 nor a Dec-31-quantity concern
        if lot.acquire_date == cy_end and lot.sold_or_transferred_date > cy_end:
            gap_window.append((row, lot))
            continue
        raise NetBenefitsFaError(
            f"{lots_data_rows('closed-lots', [row])} overlaps CY{cy_end.year} but is not a "
            f"Dec-31-held gap-window lot (acquired on Dec-31 and sold after it); "
            f"NetBenefits intra-CY closed-lot Schedule FA A3 disclosure is not yet supported "
            f"(see #I13) — refusing rather than emit an A3 that omits a disclosable holding"
        )
    return gap_window


def _fold_gap_window_into_position(
    open_lot: tuple[int, EspLot] | None,
    gap_rows: list[tuple[int, EspClosedLot]],
    confirmations_by_date: Mapping[date, EspPurchase],
    *,
    acquire_date: date,
) -> EsppDec31Lot:
    """Reconstruct one Dec-31 purchase's held position from its surviving open lot
    (if any) plus the gap-window closed lots sold the next CY.

    Returns a synthetic :class:`EspLot` whose quantity/contribution are the SUM of
    the evidence. The caller's ``_matching_confirmation`` then enforces the *exact*
    reconcile against the trade confirmation (``shares`` and ``contribution``),
    which is the guardrail that refuses when a stock split, transfer, or incomplete
    export between Dec-31 and the sale makes the evidence fail to sum to the
    authoritative Dec-31 purchase. As defence-in-depth against a mixed-basis export
    whose totals happen to coincide, each gap-window row's per-share paid cost is
    checked against the confirmation price here (a split would halve it). The
    per-share cost of the synthetic lot is taken from the confirmation (not
    ``contribution / quantity``) so it matches the confirmation exactly under
    ``_matching_confirmation`` (sub-cent per-lot rounding otherwise trips the exact
    price check); the real reconcile is on the totals.
    """
    purchase = confirmations_by_date.get(acquire_date)
    open_row, held = (None, None) if open_lot is None else open_lot
    gaps = [g for _, g in gap_rows]
    quantity = (held.quantity if held else Decimal("0")) + sum(
        (g.quantity for g in gaps), Decimal("0")
    )
    contribution = (held.contribution_usd if held else Decimal("0")) + sum(
        (g.contribution_usd for g in gaps), Decimal("0")
    )
    if purchase is not None:
        expected = purchase.purchase_price_per_share_usd
        tol = expected * Decimal("0.0005") + Decimal("0.01")
        for row, g in gap_rows:
            per_share = g.contribution_usd / g.quantity
            if abs(per_share - expected) > tol:
                raise NetBenefitsFaError(
                    f"{lots_data_rows('closed-lots', [row])}: the gap-window lot's per-share cost "
                    f"differs from the purchase price on "
                    f"{confirmation_label(acquire_date, confirmations_by_date)} by more than the "
                    f"rounding tolerance; a split/transfer or foreign row between Dec-31 and the "
                    f"sale can't be reconciled — refusing"
                )
    cost_per_share = (
        purchase.purchase_price_per_share_usd if purchase is not None else contribution / quantity
    )
    return EsppDec31Lot(
        EspLot(
            acquire_date=acquire_date,
            quantity=quantity,
            contribution_usd=contribution,
            cost_per_share_paid=cost_per_share,
            fidelity_term=held.fidelity_term if held else gaps[0].fidelity_term,
        ),
        open_row=open_row,
        closed_rows=tuple(row for row, _ in gap_rows),
    )


def reconstruct_dec31_positions(
    cy_open_lots: list[tuple[int, EspLot]],
    gap_window: list[tuple[int, EspClosedLot]],
    confirmations_by_date: Mapping[date, EspPurchase],
) -> list[EsppDec31Lot]:
    """Fold gap-window closed lots into the Dec-31 positions they belong to.

    An open lot is returned unchanged unless a gap-window lot shares its Dec-31
    acquisition date; a Dec-31 purchase fully sold the next CY (no surviving open
    lot) is reconstructed from its gap-window lots alone. A gap-window lot is
    NEVER silently dropped — an unconsumed one becomes its own reconstructed
    position and is reconciled against its confirmation downstream (or refuses if
    that confirmation is absent). Gap-window lots are, by construction, all
    acquired on Dec-31, so they never collide with a mid-year open lot.
    """
    gaps_by_date: dict[date, list[tuple[int, EspClosedLot]]] = {}
    for row, lot in gap_window:
        gaps_by_date.setdefault(lot.acquire_date, []).append((row, lot))
    result: list[EsppDec31Lot] = []
    for row, lot in cy_open_lots:
        gaps = gaps_by_date.pop(lot.acquire_date, [])
        result.append(
            EsppDec31Lot(lot, open_row=row)
            if not gaps
            else _fold_gap_window_into_position(
                (row, lot), gaps, confirmations_by_date, acquire_date=lot.acquire_date
            )
        )
    for acquire_date, gaps in gaps_by_date.items():
        result.append(
            _fold_gap_window_into_position(
                None, gaps, confirmations_by_date, acquire_date=acquire_date
            )
        )
    return result


def reconstruct_espp_dec31_lots(
    open_lots: Iterable[EspLot],
    closed_lots: Iterable[EspClosedLot],
    confirmations_by_date: Mapping[date, EspPurchase],
    *,
    cy_end: date,
) -> list[EsppDec31Lot]:
    """Rebuild the lots held at ``cy_end`` from the open- and closed-lots exports,
    each with the 1-based lots-CSV data rows it was built from.

    Refuses a CY-overlapping closed lot that is not a gap-window lot, two open
    lots on one date, a gap-window lot whose per-share cost disagrees with its
    confirmation, and a year with no lot to report.
    """
    gap_window = partition_cy_closed_lots(closed_lots, cy_end)
    lots = reconstruct_dec31_positions(
        _cy_open_lots(open_lots, cy_end), gap_window, confirmations_by_date
    )
    if not lots:
        raise NetBenefitsFaError(
            f"no ESPP lot acquired on or before Dec-31; nothing to report for CY{cy_end.year}"
        )
    return lots


def _matching_confirmation(
    held: EsppDec31Lot,
    confirmations_by_date: Mapping[date, EspPurchase],
    cfg: NetBenefitsConfig,
) -> tuple[EspPurchase, str]:
    """Find and validate the confirmation for ``held`` and name it; refuse on
    any mismatch."""
    lot = held.lot
    purchase = confirmations_by_date.get(lot.acquire_date)
    if purchase is None:
        raise NetBenefitsFaError(
            f"{held.subject}: no purchase confirmation covers this position; the FMV cost "
            f"basis cannot be sourced"
        )
    label = confirmation_label(lot.acquire_date, confirmations_by_date)
    check_confirmation_belongs_to_config(purchase, cfg, label=label)
    # Cross-document identity join (the parsers each verify their own document;
    # this confirms the CSV lot and the PDF describe the SAME purchase).
    if purchase.shares != lot.quantity:
        raise NetBenefitsFaError(f"{held.subject}: the Quantity differs from the shares on {label}")
    if purchase.contribution_usd != lot.contribution_usd:
        raise NetBenefitsFaError(
            f"{held.subject}: the Cost basis differs from the Accumulated Contributions on {label}"
        )
    if purchase.purchase_price_per_share_usd != lot.cost_per_share_paid:
        raise NetBenefitsFaError(
            f"{held.subject}: the Cost basis/share differs from the purchase price on {label}"
        )
    return purchase, label


def _check_discount(purchase: EspPurchase, discount_pct: Decimal, *, label: str) -> None:
    """Refuse a confirmation whose price breaks the plan discount identity."""
    if espp_discount_mismatch(purchase, discount_pct) is not None:
        raise NetBenefitsFaError(
            f"{label}: the purchase price is not the FMV per share less the "
            f"netbenefits.espp_discount_pct discount, within the rounding tolerance"
        )


def _override_for(
    overrides: Iterable[NetBenefitsPriceOverride], valuation_date: date
) -> NetBenefitsPriceOverride | None:
    """The price override dated within the year-end window on/before
    ``valuation_date``, so a near-date override reaches
    :func:`resolve_dec31_close`, which refuses it unless it is dated on the
    required US market session, rather than being silently ignored. Refuses
    if two overrides fall in the window (ambiguous)."""
    window = timedelta(days=_OVERRIDE_STALENESS_DAYS)
    matches = [
        (index, o)
        for index, o in enumerate(overrides)
        if timedelta(0) <= valuation_date - o.price_date <= window
    ]
    if len(matches) > 1:
        entries = [f"netbenefits.price_overrides[{index}]" for index, _ in matches]
        raise NetBenefitsFaError(
            f"{', '.join(entries[:-1])} and {entries[-1]} are each dated within "
            f"{_OVERRIDE_STALENESS_DAYS} days on/before the valuation date; the override is "
            f"ambiguous"
        )
    return matches[0][1] if matches else None


def _resolve_close(
    lot: EspLot,
    cfg: NetBenefitsConfig,
    valuation_date: date,
    series: PriceSeries | None,
    *,
    peak_basis: PeakBasis,
    prices_fetched: bool,
) -> ResolvedClose:
    """Resolve the Dec-31 close from the config override and/or Yahoo.

    The statement is unpriced, so ``statement_total_usd`` is ``None`` (honest
    provenance — :func:`resolve_dec31_close` labels the result OVERRIDE/Yahoo,
    not "statement"). The caller anchors the result to the confirmation FMV.
    ``peak_basis`` and ``prices_fetched`` pick the no-close route.
    """
    override = _override_for(cfg.price_overrides, valuation_date)
    return resolve_dec31_close(
        valuation_date=valuation_date,
        quantity=lot.quantity,
        statement_total_usd=None,
        series=series,
        override_per_share_usd=override.price_per_share_usd if override else None,
        override_priced_on=override.price_date if override else None,
        override_label=f"{override.source}: {override.reason}" if override else "",
        max_staleness_days=_OVERRIDE_STALENESS_DAYS,
        missing_source_remedy=_missing_close_route(
            valuation_date, peak_basis=peak_basis, prices_fetched=prices_fetched
        ),
        session_routes=_SESSION_ROUTES,
    )


def _holding_lot(lot: EspLot, purchase: EspPurchase, cfg: NetBenefitsConfig) -> HoldingLot:
    """The reconciled lot, with the confirmation's FMV at purchase as its cost basis."""
    sec = cfg.security
    return HoldingLot(
        currency="USD",
        symbol=sec.instrument.symbol,
        description=purchase.security_description,
        isin=sec.instrument.isin,
        issuer_country_code=sec.instrument.issuer_country_code,
        quantity=lot.quantity,
        cost_basis_money=purchase.fmv_total_usd,
        acquisition_date=purchase.purchase_date,
        side=Side.LONG,
    )


def resolve_espp_dec31_positions(
    lots: Iterable[EsppDec31Lot],
    confirmations_by_date: Mapping[date, EspPurchase],
    *,
    cfg: NetBenefitsConfig,
    calendar_year: int,
    series: PriceSeries | None,
    peak_basis: PeakBasis,
    prices_fetched: bool,
) -> EsppDec31Positions:
    """Match each lot to its confirmation, resolve + anchor its Dec-31 close,
    and value it. Every lot must be purchased ON Dec-31 (else refuse).
    ``peak_basis`` (``[schedule_fa] peak_basis``) and ``prices_fetched``
    (whether a missing ``series`` was looked up online) pick the route a
    refusal for no close at all names (#1565).

    Raises:
        NetBenefitsFaError: A lot has no matching confirmation, disagrees with
            it, breaks the plan discount, is not a Dec-31 purchase, or has two
            price overrides in the carry-back window.
        ClosingPriceError: The Dec-31 close cannot be resolved, or disagrees
            with the confirmation FMV.
        InvariantError: The closes can't be added exactly; the refusal names
            the lot's 1-based position in ``lots``. Or the anchor tolerance or
            the close's difference from the confirmation FMV can't be computed
            exactly (#1250).
    """
    cy_end = date(calendar_year, 12, 31)
    positions: list[EsppDec31Position] = []
    securities_usd = Decimal("0")
    security_name = ""
    for ordinal, held in enumerate(lots, start=1):
        lot = held.lot
        purchase, label = _matching_confirmation(held, confirmations_by_date, cfg)
        _check_discount(purchase, cfg.espp_discount_pct, label=label)
        if purchase.purchase_date != cy_end:
            raise NetBenefitsFaError(
                f"{held.subject}: {label} is not a Dec-31 purchase, so the lot is held across "
                f"part of CY{calendar_year}; its in-CY dividend can't be proven here — wire "
                f"dividends via #I10"
            )
        resolved = _resolve_close(
            lot, cfg, cy_end, series, peak_basis=peak_basis, prices_fetched=prices_fetched
        )
        # The lot is purchased ON Dec-31 (guarded above), so the confirmation's FMV
        # at purchase IS the broker-stated Dec-31 value — anchor the resolved close
        # to it (catches a typoed override even with --no-fetch-prices).
        anchor_tol = per_share_tolerance(lot.quantity, at="NetBenefits FA, close anchor tolerance")
        anchor_difference = checked_subtraction(
            resolved.total_usd,
            purchase.fmv_total_usd,
            at="NetBenefits FA, close anchor difference",
        )
        if anchor_difference.copy_abs() > anchor_tol:
            raise ClosingPriceError(
                f"{held.subject}: the Dec-31 close ({resolved.source.value}) differs from the "
                f"Market Value at Purchase on {label} by more than the tolerance; "
                f"{_anchor_route(resolved.source)}"
            )
        positions.append(
            EsppDec31Position(
                lot=value_holding_lot(
                    _holding_lot(lot, purchase, cfg), report_date=cy_end, close=resolved
                ),
                close=resolved,
                purchase=purchase,
            )
        )
        securities_usd = checked_addition(
            securities_usd,
            resolved.total_usd,
            at=f"NetBenefits FA securities, input lot {ordinal}, field total_usd",
        )
        security_name = purchase.security_description
    return EsppDec31Positions(tuple(positions), securities_usd, security_name)
