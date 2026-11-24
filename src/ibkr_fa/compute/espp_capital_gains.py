"""Schedule CG for sold Fidelity ESPP lots — §49(2AA) FMV basis, 24-month split.

When ESPP shares are sold, two Indian rules diverge from the Fidelity data:

1. **Cost of acquisition = the historical gross INR FMV taken into account
   under §17(2)(vi) (§49(2AA))**, NOT the contribution / US "Cost basis" in
   the closed-lots CSV. The configured INR value is tied to the purchase
   confirmation's USD FMV and purchase date.
2. **STCG (<= 24 months) / LTCG (> 24 months)** is the foreign-equity holding
   rule computed from the acquire / sale *dates* — never Fidelity's US
   "Short/Long" ``Term``.

This module is a thin **adapter**: each :class:`EspClosedLot` is matched to its
purchase confirmation (for the FMV), and a :class:`ClosedLot` is built with the
USD FMV cost basis and, as
:attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`, the
§49(2AA) historical INR cost. The lot is routed through the existing
:func:`~ibkr_fa.compute.schedule_cg.compute_schedule_cg_stcg_lines` /
:func:`~ibkr_fa.compute.schedule_cg_ltcg.compute_schedule_cg_ltcg_lines`
engines, which file that INR cost and apply the §112 / slab routing and the
Rule 115 FX to the proceeds. Engine output is never edited afterwards.

.. warning::

   The exact §49(2AA) FX interaction has no squarely on-point ITAT ruling.
   Because the statute anchors cost to the FMV already taken into account under
   §17(2)(vi), this adapter requires that historical gross INR FMV and refuses
   to substitute either an acquisition-date proxy or a sale-date revaluation.

Both NetBenefits capital-gains commands run the compute through
:func:`proven_zero_capital_gains`, which first checks that the already-loaded
statements and closed lots prove the FY had no sale (#666).

This path is exercised by synthetic fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.espp_positions import (
    confirmation_config_mismatch,
    confirmation_label,
    espp_discount_mismatch,
    lots_data_rows,
)
from ibkr_fa.compute.schedule_cg import (
    ScheduleCgStcgComputeResult,
    compute_schedule_cg_stcg_lines,
)
from ibkr_fa.compute.schedule_cg_ltcg import (
    ScheduleCgLtcgComputeResult,
    compute_schedule_cg_ltcg_lines,
)
from ibkr_fa.compute.schedule_cg_portal_amounts import require_schedule_cg_filed_amounts
from ibkr_fa.compute.source_coverage import SETTLEMENT_BOUND, first_coverage_gap
from ibkr_fa.config import NetBenefitsConfig
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.context import TaxFxContext
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.models.closed_lot import BasisProvenance, ClosedLot, StatutoryInrCostBasis
from ibkr_fa.models.tax_year import FinancialYear
from ibkr_fa.parser.netbenefits.bundle import NetBenefitsBundle
from ibkr_fa.parser.netbenefits.confirmation_pdf import EspPurchase
from ibkr_fa.parser.netbenefits.lots_csv import EspClosedLot
from ibkr_fa.parser.netbenefits.statement_pdf import ReportType, StatementData

# Fidelity prints fractional ESPP shares to 3-4 dp; allow that rounding envelope
# when checking a lot didn't dispose more than its purchase held.
_SHARE_TOLERANCE: Decimal = Decimal("0.001")
_CENT: Decimal = Decimal("0.01")
# The window the statements must cover to prove the FY had no sale, as refusals
# name it; the NetBenefits income loader prints it too.
CAPITAL_GAINS_WINDOW: Final = (
    "the window from the later of the FY start and "
    "netbenefits.account.account_opening_date through ten days after the FY end"
)


class EsppCapitalGainsError(IbkrFaError):
    """An ESPP closed lot cannot be turned into a defensible CG line."""


@dataclass(frozen=True, slots=True)
class EsppCapitalGainsResult:
    """The STCG + LTCG compute results for sold ESPP lots.

    Attributes:
        stcg: The full STCG compute result (its ``stcg_lines`` are the in-FY
            short-term gains; its audit buckets surface the long-term and
            out-of-scope partitions).
        ltcg: The full LTCG compute result (its ``ltcg_lines`` are the in-FY
            long-term gains).
    """

    stcg: ScheduleCgStcgComputeResult
    ltcg: ScheduleCgLtcgComputeResult


def _purchases_by_date(purchases: Iterable[EspPurchase]) -> dict[date, EspPurchase]:
    by_date: dict[date, EspPurchase] = {}
    for purchase in purchases:
        if purchase.purchase_date in by_date:
            raise EsppCapitalGainsError(
                "two purchase confirmations share a purchase date; the FMV cost basis is ambiguous"
            )
        by_date[purchase.purchase_date] = purchase
    return by_date


def _check_no_oversell(
    closed_lots: Iterable[EspClosedLot], by_date: dict[date, EspPurchase]
) -> None:
    """Refuse if the total disposed from any one purchase exceeds what it bought.

    Sums EVERY disposal in the export (any FY, sales and transfers alike), so two
    rows each <= shares but together > shares are caught.

    ponytail: only disposals present in THIS closed-lots export are summed —
    disposals from a prior export (a lot split-sold across separate filings) are
    not tracked. Upgrade path: pass the prior-period disposed quantity per lot.
    """
    disposed: dict[date, Decimal] = {}
    rows: dict[date, list[int]] = {}
    for row, lot in enumerate(closed_lots, start=1):
        # Unmatched out-of-FY rows can't be attributed to a purchase; an
        # unmatched in-FY row is refused later by _match (missing FMV).
        if lot.acquire_date in by_date:
            disposed[lot.acquire_date] = disposed.get(lot.acquire_date, Decimal(0)) + lot.quantity
            rows.setdefault(lot.acquire_date, []).append(row)
    for acquire_date, total in disposed.items():
        shares = by_date[acquire_date].shares
        if total > shares + _SHARE_TOLERANCE:
            raise EsppCapitalGainsError(
                f"the disposals in {lots_data_rows('closed-lots', rows[acquire_date])} exceed "
                f"the shares {confirmation_label(acquire_date, by_date)} purchased"
            )


def _match(lot: EspClosedLot, by_date: dict[date, EspPurchase], *, row: int) -> EspPurchase:
    purchase = by_date.get(lot.acquire_date)
    if purchase is None:
        raise EsppCapitalGainsError(
            f"{lots_data_rows('closed-lots', [row])}: no purchase confirmation covers this lot — "
            f"FMV-at-purchase is the §49(2AA) cost of acquisition and cannot be derived "
            f"from the closed-lots CSV"
        )
    return purchase


def _to_closed_lot(
    lot: EspClosedLot,
    purchase: EspPurchase,
    cfg: NetBenefitsConfig,
    *,
    row: int,
    label: str,
) -> ClosedLot:
    """Build a ClosedLot with the §49(2AA) FMV cost basis, refusing on any
    mismatch. ``row`` is the lot's 1-based closed-lots data row and ``label``
    names its confirmation (see :func:`confirmation_label`)."""
    value = cfg.cg_fmv_for(purchase.purchase_date)
    if value is None:
        raise EsppCapitalGainsError(
            f"no [[netbenefits.cg_fmv_values]] entry for the purchase date of {label}; "
            "section 49(2AA) cost must use the historical gross INR FMV taken into account "
            "under section 17(2)(vi), and ibkr-fa will not substitute a proxy FX conversion"
        )
    sec = cfg.security
    problem = confirmation_config_mismatch(purchase, cfg)
    if problem is not None:
        raise EsppCapitalGainsError(f"{label}: {problem}")
    # Tie THIS closed lot to THAT confirmation via the discounted US basis. The
    # tolerance scales with the disposed quantity (a partial sale's slice), since
    # the per-share price carries Fidelity's cent rounding — the FA path's EXACT
    # equality only holds for a whole-purchase OPEN lot, not a partial sale.
    price = purchase.purchase_price_per_share_usd
    if abs(lot.quantity * price - lot.contribution_usd) > lot.quantity * Decimal("0.005") + _CENT:
        raise EsppCapitalGainsError(
            f"{lots_data_rows('closed-lots', [row])}: the Cost basis doesn't reconcile with "
            f"the Quantity at the purchase price on {label}"
        )
    # Validate the FMV itself (the §49(2AA) basis, the number that gets filed).
    if espp_discount_mismatch(purchase, cfg.espp_discount_pct) is not None:
        raise EsppCapitalGainsError(
            f"{label}: the purchase price is not the FMV per share less the "
            f"netbenefits.espp_discount_pct discount, within the rounding tolerance, so the FMV "
            f"cost basis of {lots_data_rows('closed-lots', [row])} is unreliable"
        )
    # §49(2AA) USD FMV, full precision: FMV per share x disposed quantity. The
    # engines show it as the audit USD cost; they file the INR override below.
    cost_basis = purchase.fmv_per_share_usd * lot.quantity
    return ClosedLot(
        currency_primary="USD",
        sub_category=None,
        symbol=sec.instrument.symbol,
        description=purchase.security_description,
        isin=sec.instrument.isin,
        listing_exchange=sec.instrument.listing_exchange,
        issuer_country_code=sec.instrument.issuer_country_code,
        quantity=lot.quantity,
        cost_basis_money=cost_basis,
        proceeds_money=lot.proceeds_usd,
        open_date_time=lot.acquire_date,
        close_date_time=lot.sold_or_transferred_date,
        basis_provenance=BasisProvenance.S49_2AA,
        # The historical INR FMV, prorated to the disposed slice.
        cost_basis_inr_override=StatutoryInrCostBasis(
            amount_inr=value.section17_gross_fmv_total_inr * lot.quantity / purchase.shares,
            source=f"Section 49(2AA) historical gross INR FMV: {value.source}",
        ),
    )


def compute_espp_capital_gains(
    closed_lots: Iterable[EspClosedLot],
    purchases: Iterable[EspPurchase],
    *,
    cfg: NetBenefitsConfig,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
) -> EsppCapitalGainsResult:
    """Compute Schedule CG STCG + LTCG for sold ESPP lots in one financial year.

    Args:
        closed_lots: All disposed ESPP lots from ``View closed lots.csv``.
        purchases: The purchase confirmations (the FMV-at-purchase evidence).
        cfg: The NetBenefits config (security identity + participant number).
        financial_year_start / financial_year_end: The Indian FY being reported.
        fx_policy: Rule 115(1), Explanation (2)(f), capital-gains FX policy
            for the sale proceeds.

    Returns:
        An :class:`EsppCapitalGainsResult` with the STCG/LTCG compute results.

    Raises:
        EsppCapitalGainsError: On missing/ambiguous USD or historical INR FMV
            evidence, an identity/cost-basis mismatch, an oversell, or an in-FY
            closed lot with proceeds <= 0 (the export's "Date sold or
            transferred" doesn't show whether it was a sale or a transfer).
        ScheduleCgDroppedLotError: For a sold lot neither engine would emit a
            line for (pre-2024-07-23 long-term / Indian-source / out-of-FY);
            both engines run with ``strict=True``.
        InvariantError: If a schedule's filed consideration, cost or balance,
            with the section 49(2AA) cost, falls outside the CBDT 14-digit bound.
    """
    closed = list(closed_lots)
    by_date = _purchases_by_date(purchases)
    _check_no_oversell(closed, by_date)

    built: list[ClosedLot] = []
    for row, lot in enumerate(closed, start=1):
        if not (financial_year_start <= lot.sold_or_transferred_date <= financial_year_end):
            # Out-of-FY: not this FY's gain. Don't demand its FMV (a missing
            # confirmation for an out-of-FY closure must not block this filing).
            continue
        if lot.proceeds_usd <= 0:
            # The parser refuses negative proceeds; this also guards direct callers.
            raise EsppCapitalGainsError(
                f"{lots_data_rows('closed-lots', [row])}: the Proceeds are not positive; the "
                f"closed-lots export doesn't show whether it was a sale or a transfer, so its "
                f"Schedule CG treatment is unknown"
            )
        purchase = _match(lot, by_date, row=row)
        label = confirmation_label(lot.acquire_date, by_date)
        built.append(_to_closed_lot(lot, purchase, cfg, row=row, label=label))

    stcg = compute_schedule_cg_stcg_lines(
        built,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=fx_policy,
        strict=True,
    )
    ltcg = compute_schedule_cg_ltcg_lines(
        built,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        fx_policy=fx_policy,
        strict=True,
    )
    require_schedule_cg_filed_amounts(stcg.stcg_lines, schedule="STCG")
    require_schedule_cg_filed_amounts(ltcg.ltcg_lines, schedule="LTCG")
    return EsppCapitalGainsResult(stcg=stcg, ltcg=ltcg)


def require_capital_gains_coverage(
    statements: Iterable[StatementData],
    cfg: NetBenefitsConfig,
    financial_year: FinancialYear,
    *,
    error: type[IbkrFaError],
) -> None:
    """Refuse unless the statements can prove the FY had no NetBenefits sale (#666).

    The Stock Plan Services reports (year-end reports excluded: a summary's
    silence proves nothing) must cover, with no gap, the window from the later of
    the FY start and ``netbenefits.account.account_opening_date`` through
    :data:`SETTLEMENT_BOUND` past the FY end. The statement parser refuses any
    dated line it doesn't read, and it reads no sale or transfer row, so a parsed
    covering set shows no disposal settled in the window. An opening date after the
    FY end refuses: an empty window would prove nothing. ``error`` is the calling
    pipeline's error type. No refusal prints a value from a file.
    """
    if cfg.account.account_opening_date > financial_year.end_date:
        raise error(
            "netbenefits.account.account_opening_date is after the end of "
            f"FY {financial_year}, so the statements can't prove that FY had no sale; "
            "correct the date, or don't run NetBenefits capital gains for that FY"
        )
    gap = first_coverage_gap(
        (
            (s.period_start, s.period_end)
            for s in statements
            if s.report_type is ReportType.STOCK_PLAN_SERVICES
        ),
        window_start=max(financial_year.start_date, cfg.account.account_opening_date),
        window_end=financial_year.end_date + SETTLEMENT_BOUND,
    )
    if gap is not None and gap.short_end:
        raise error(
            "the statements (year-end reports excluded) end before ten days after the FY "
            f"end, so they don't cover {CAPITAL_GAINS_WINDOW}; supply the statement(s) "
            "covering the gap"
        )
    if gap is not None:
        raise error(
            f"the statements (year-end reports excluded) leave a gap in {CAPITAL_GAINS_WINDOW} "
            "(coverage is needed to prove no sale was missed there)"
        )


def require_no_in_fy_disposals(
    closed_lots: Sequence[EspClosedLot],
    financial_year: FinancialYear,
    *,
    error: type[IbkrFaError],
) -> None:
    """Refuse a closed-lots row dated in the FY, whatever its proceeds (#666).

    ibkr-fa can't yet check a NetBenefits sale against the statements (#1150), so
    an FY with a sale or transfer files no capital gains. The refusal names the
    row by its 1-based data row and prints no date, quantity or amount.
    """
    for row, lot in enumerate(closed_lots, start=1):
        if financial_year.start_date <= lot.sold_or_transferred_date <= financial_year.end_date:
            raise error(
                f"{lots_data_rows('closed-lots', [row])}, Date sold or transferred: a sale or "
                f"transfer in FY {financial_year}; ibkr-fa can't yet check NetBenefits sales "
                "against the statements, so it files no capital gains for that FY"
            )


def proven_zero_capital_gains(
    statements: Iterable[StatementData],
    bundle: NetBenefitsBundle,
    cfg: NetBenefitsConfig,
    financial_year: FinancialYear,
    fx_ctx: TaxFxContext,
    *,
    error: type[IbkrFaError],
) -> EsppCapitalGainsResult:
    """The FY's ESPP capital gains, once the already-loaded statements and bundle
    prove it had no sale (#666): :func:`require_capital_gains_coverage`, then
    :func:`require_no_in_fy_disposals`, then the compute. ``error`` is the calling
    pipeline's error type; each caller loads its inputs in its own order first."""
    require_capital_gains_coverage(statements, cfg, financial_year, error=error)
    require_no_in_fy_disposals(bundle.closed_lots, financial_year, error=error)
    return compute_espp_capital_gains(
        bundle.closed_lots,
        bundle.confirmations_by_date.values(),
        cfg=cfg,
        financial_year_start=financial_year.start_date,
        financial_year_end=financial_year.end_date,
        fx_policy=fx_ctx.capital_gains,
    )
