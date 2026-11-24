"""Compute Schedule CG LTCG (foreign long-term capital-gains) lines.

This module is the LTCG counterpart to :mod:`ibkr_fa.compute.schedule_cg`
(which handles STCG). It takes parsed
:class:`~ibkr_fa.models.closed_lot.ClosedLot` records plus the selected
:class:`~ibkr_fa.fx.policy.AuditedFxPolicy` (Rule 115(1), Explanation (2)(f), via
:class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`)
and produces a :class:`ScheduleCgLtcgComputeResult` ready for the LTCG
writer.

v1 scope (issue #30): foreign-source **long-term** capital gains under
the **new regime** (12.5% without indexation), introduced by the
Finance (No. 2) Act, 2024 with effect from 2024-07-23. Pre-cutover
disposals (which require 20%-with-indexation via CBDT-published
Cost Inflation Index data) are routed to a dedicated
``excluded_pre_2024_07_23`` audit bucket and rejected — out of v1
scope (CII data is not bundled yet).

Algorithm:

1. Materialise the input iterable to a list once (so a generator
   argument is not re-consumed by the defense-in-depth currency
   guard).
2. Validate every closed lot's ``currency_primary == "USD"`` —
   multi-currency support is out of v1 scope (issue #52).
3. Partition each lot into one of FIVE buckets via
   ``(close_date in FY) -> (issuer_country_iso2 == "IN") ->
   is_short_term_holding(open_date, close_date) ->
   (close_date >= NEW_REGIME_CUTOVER_DATE)``:

   * ``excluded_out_of_fy`` if ``close_date`` is outside
     ``[fy_start, fy_end]``.
   * ``excluded_indian_source`` if ISIN prefix is ``"IN"``
     (defense-in-depth; unreachable through current parser path
     since the upstream Activity Statement Trades parser hard-rejects
     IN ISINs per #143).
   * ``excluded_stcg`` if the lot is short-term per
     :func:`~ibkr_fa.models.schedule_cg_row.is_short_term_holding`
     (handled by the schedule-cg / STCG pipeline; surfaced here as
     an audit block so the LTCG CSV is reconcilable against the
     full Trades section).
   * ``excluded_pre_2024_07_23`` if the lot is long-term but the
     disposal date is strictly before
     :data:`~ibkr_fa.models.schedule_cg_ltcg_row.NEW_REGIME_CUTOVER_DATE`
     (old-regime 20%-with-indexation; out of v1 scope).
   * Otherwise: LTCG. Apply Rule 115 FX once at the transfer
     (close) event date, build a :class:`ScheduleCgLtcgLine`. A lot
     with :attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`
     takes that statutory INR cost instead of its converted USD cost.
     The INR cost, proceeds and gain keep every digit of the lot's
     amounts, as for STCG (#1184).

4. Sort LTCG lines by ``(close_date, symbol)`` for deterministic
   output.

Rule 115 single-conversion semantics:

Identical to the STCG pipeline — see
:mod:`ibkr_fa.compute.schedule_cg` for the full rationale. One
specified date per income head; for capital gains, the
transfer-event's preceding-month-end. Both cost basis and proceeds
are converted at the same transfer-date TTBR.

Fail-fast on:

* Any non-USD closed lot (multi-currency out of v1; #52).
* With ``strict=True``, any pre-2024-07-23 long-term, Indian-source or
  out-of-FY lot, since neither schedule would emit a line for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final

from ibkr_fa.compute.lot_matching import require_indian_basis
from ibkr_fa.compute.schedule_cg import (
    _line_inr_amounts,
    _refuse_dropped_lots,
    require_usd_closed_lots,
)
from ibkr_fa.compute.schedule_cg_portal_amounts import (
    ScheduleCgPortalAmounts,
    schedule_cg_portal_amounts,
)
from ibkr_fa.compute.schedule_cg_table_f import (
    ScheduleCgTableFRow,
    cg_line_gains,
    schedule_cg_table_f_row,
)
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.models import ClosedLot
from ibkr_fa.models.schedule_cg_ltcg_row import (
    NEW_REGIME_CUTOVER_DATE,
    ScheduleCgLtcgLine,
)
from ibkr_fa.models.schedule_cg_row import is_short_term_holding
from ibkr_fa.types import Isin, Ticker

_INDIAN_ISIN_PREFIX: Final[str] = "IN"


@dataclass(frozen=True, slots=True)
class DeferredStcgRow:
    """A closed lot whose holding period is short-term.

    Surfaced in the writer's ``_excluded_stcg:`` audit block so the
    LTCG CSV is reconcilable against the full Trades section. The
    actual STCG processing happens in
    :func:`~ibkr_fa.compute.schedule_cg.compute_schedule_cg_stcg_lines`
    (the ``schedule-cg`` subcommand).

    Attributes:
        closed_lot: The :class:`ClosedLot` deferred to STCG.
        holding_period_days: ``(close - open).days`` for audit.
    """

    closed_lot: ClosedLot
    holding_period_days: int


@dataclass(frozen=True, slots=True)
class PreCutoverLtcgRow:
    """A long-term lot disposed before the 2024-07-23 new-regime cut-in.

    Surfaced in the writer's ``_excluded_pre_2024_07_23:`` audit block
    so the user knows the row was visible to the compute layer but
    not emitted because the old-regime 20%-with-indexation pathway
    is out of v1 scope (requires CBDT Cost Inflation Index data).

    Attributes:
        closed_lot: The :class:`ClosedLot` with a pre-cutover disposal.
        holding_period_days: ``(close - open).days`` — strictly > 720
          (long-term) AND ``close_date < NEW_REGIME_CUTOVER_DATE``.
    """

    closed_lot: ClosedLot
    holding_period_days: int


@dataclass(frozen=True, slots=True)
class ScheduleCgLtcgComputeResult:
    """Five-bucket result of the Schedule CG LTCG compute stage.

    The Schedule CG LTCG writer consumes a single instance and emits
    the entire CSV (main lines + audit blocks) in one
    :meth:`~ibkr_fa.output.base.ScheduleCgLtcgWriter.write` call,
    mirroring the STCG writer's single-call contract.

    Attributes:
        ltcg_lines: In-FY, non-Indian-source, long-term, post-2024-07-23
            disposal lines, sorted by ``(close_date, symbol)``. Becomes
            the body of the LTCG CSV.
        excluded_stcg: In-FY, non-Indian-source short-term lots
            (handled by the schedule-cg / STCG pipeline; surfaced
            here for audit).
        excluded_pre_2024_07_23: In-FY, non-Indian-source long-term
            lots whose disposal date is strictly before 2024-07-23
            (old-regime indexation pathway; out of v1 scope).
        excluded_indian_source: In-FY closed lots whose ISIN-derived
            issuer country is ``"IN"``. Defense-in-depth only —
            unreachable through current parser path per #143.
        excluded_out_of_fy: Closed lots whose ``close_date`` is
            outside the requested financial year.
    """

    ltcg_lines: list[ScheduleCgLtcgLine]
    excluded_stcg: list[DeferredStcgRow]
    excluded_pre_2024_07_23: list[PreCutoverLtcgRow]
    excluded_indian_source: list[ClosedLot]
    excluded_out_of_fy: list[ClosedLot]

    @property
    def portal_amounts(self) -> ScheduleCgPortalAmounts:
        """The whole-rupee portal figures of :attr:`ltcg_lines`, computed on access."""
        return schedule_cg_portal_amounts(self.ltcg_lines, schedule="LTCG")

    @property
    def table_f(self) -> ScheduleCgTableFRow:
        """Schedule CG Table F row 5 of :attr:`ltcg_lines`, computed on access (#1360).

        Split to :attr:`portal_amounts`' balance, the filed result.
        """
        return schedule_cg_table_f_row(
            cg_line_gains(self.ltcg_lines),
            result_inr=self.portal_amounts.balance_inr,
        )


def compute_schedule_cg_ltcg_lines(
    closed_lots: Iterable[ClosedLot],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
    strict: bool = False,
) -> ScheduleCgLtcgComputeResult:
    """Compute the Schedule CG LTCG lines for an FY.

    Args:
        closed_lots: All FIFO-matched :class:`ClosedLot` records.
            A generator argument is materialised to a list before any
            check so the iterator is never exhausted by the
            precondition pass.
        financial_year_start: First day of the Indian FY being
            reported (e.g., ``date(2025, 4, 1)`` for FY 2025-26).
        financial_year_end: Last day of the Indian FY being reported.
        fx_policy: The :class:`~ibkr_fa.fx.policy.AuditedFxPolicy` to
            use for per-event FX conversion. v1 production uses
            :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
            (Rule 115 preceding-month-end with carry-back). The
            policy is queried once per LTCG row at the lot's
            transfer (close) date — single conversion per Rule 115(1),
            Explanation (2)(f); see module docstring. The line's rate
            and ``fx_specified_date`` both come from that one
            resolution.
        strict: Refuse when any lot lands in
            ``excluded_pre_2024_07_23``, ``excluded_indian_source`` or
            ``excluded_out_of_fy``. For callers whose every lot must get
            a line in this schedule or the STCG one; the
            ``excluded_stcg`` bucket never refuses.

    Returns:
        A :class:`ScheduleCgLtcgComputeResult` with five buckets;
        see the class docstring.

    Raises:
        InvariantError: If any input closed lot has
            ``currency_primary != "USD"``, or from
            :class:`ScheduleCgLtcgLine` when the policy's specified
            date is not strictly before the close date (a mis-wired
            policy).
        MissingFxRateError: If the transfer-event specified date
            has no Rule 115 rate available within the policy's
            carry-back window.
        BrokerBasisError: If a broker-matched lot's holding-period
            date moved (#599); see
            :func:`~ibkr_fa.compute.lot_matching.require_indian_basis`.
        ScheduleCgDroppedLotError: With ``strict=True``, if any lot
            lands in a bucket that refuses (see ``strict``); defined in
            :mod:`ibkr_fa.compute.schedule_cg`.
    """
    materialised = list(closed_lots)
    require_indian_basis(materialised)
    require_usd_closed_lots(materialised, "compute_schedule_cg_ltcg_lines")

    ltcg_lines: list[ScheduleCgLtcgLine] = []
    excluded_stcg: list[DeferredStcgRow] = []
    excluded_pre_2024_07_23: list[PreCutoverLtcgRow] = []
    excluded_indian_source: list[ClosedLot] = []
    excluded_out_of_fy: list[ClosedLot] = []

    for position, lot in enumerate(materialised, start=1):
        if not (financial_year_start <= lot.close_date_time <= financial_year_end):
            excluded_out_of_fy.append(lot)
            continue

        issuer_country = str(lot.issuer_country_code)
        if issuer_country == _INDIAN_ISIN_PREFIX:
            excluded_indian_source.append(lot)
            continue

        holding_period_days = (lot.close_date_time - lot.open_date_time).days
        if is_short_term_holding(lot.open_date_time, lot.close_date_time):
            excluded_stcg.append(
                DeferredStcgRow(closed_lot=lot, holding_period_days=holding_period_days)
            )
            continue

        # Long-term lot. Apply the new-regime cut-in gate (Finance (No.
        # 2) Act, 2024, effective 2024-07-23). Pre-cutover disposals
        # need indexation via CBDT CII data — out of v1 scope.
        if lot.close_date_time < NEW_REGIME_CUTOVER_DATE:
            excluded_pre_2024_07_23.append(
                PreCutoverLtcgRow(closed_lot=lot, holding_period_days=holding_period_days)
            )
            continue

        resolution = fx_policy.rate_for_audited(lot.close_date_time)
        fx_rate = resolution.rate

        (cost_basis_inr, cost_basis_inr_source, proceeds_inr, realized_gain_inr, exact_amounts) = (
            _line_inr_amounts(lot, fx_rate, schedule="LTCG", position=position)
        )

        ltcg_lines.append(
            ScheduleCgLtcgLine(
                symbol=Ticker(str(lot.symbol)),
                isin=Isin(str(lot.isin)),
                issuer_country_iso2=issuer_country,
                description=lot.description,
                open_date=lot.open_date_time,
                close_date=lot.close_date_time,
                holding_period_days=holding_period_days,
                quantity=lot.quantity,
                cost_basis_usd=lot.cost_basis_money,
                proceeds_usd=lot.proceeds_money,
                fx_specified_date=resolution.specified_date,
                fx_rate_publication_date=resolution.rate_date,
                fx_rate_inr_per_usd=fx_rate,
                cost_basis_inr=cost_basis_inr,
                cost_basis_inr_source=cost_basis_inr_source,
                proceeds_inr=proceeds_inr,
                realized_gain_inr=realized_gain_inr,
                exact_amounts=exact_amounts,
            )
        )

    if strict:
        _refuse_dropped_lots(
            "LTCG",
            {
                "pre-2024-07-23 long-term": len(excluded_pre_2024_07_23),
                "Indian-source": len(excluded_indian_source),
                "out-of-FY": len(excluded_out_of_fy),
            },
        )
    ltcg_lines.sort(key=lambda line: (line.close_date, str(line.symbol)))

    return ScheduleCgLtcgComputeResult(
        ltcg_lines=ltcg_lines,
        excluded_stcg=excluded_stcg,
        excluded_pre_2024_07_23=excluded_pre_2024_07_23,
        excluded_indian_source=excluded_indian_source,
        excluded_out_of_fy=excluded_out_of_fy,
    )
