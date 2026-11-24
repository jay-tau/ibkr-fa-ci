"""Compute Schedule CG STCG (foreign short-term capital-gains) lines.

This module is the CG counterpart to :mod:`ibkr_fa.compute.schedule_os`.
It takes parsed :class:`~ibkr_fa.models.closed_lot.ClosedLot` records
plus the selected :class:`~ibkr_fa.fx.policy.AuditedFxPolicy` (Rule 115(1),
Explanation (2)(f),
for v1, via
:class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`)
and produces a :class:`ScheduleCgStcgComputeResult` ready for the CG
writer.

v1 scope (issue #29): foreign-source **short-term** capital gains
only (holding period <= 24 months per Section 2(42A) for foreign
equities). Long-term (LTCG) lots are partitioned into a deferred-LTCG
audit block; the ``schedule-cg-ltcg`` subcommand
(:mod:`ibkr_fa.compute.schedule_cg_ltcg`) handles them.

The ``excluded_indian_source`` bucket is defense-in-depth only: the
upstream Activity Statement Trades parser already hard-rejects ISINs
with prefix ``IN`` for FA purposes (#143), so this bucket is
unreachable through the current parser path. It is kept in the
result shape so a future parser relaxation that admits IN ISINs
does not silently route them through the foreign-STCG aggregate.

Algorithm:

1. Materialise the input iterable to a list once (so a generator
   argument is not re-consumed by the defense-in-depth currency
   guard).
2. Validate every closed lot's ``currency_primary == "USD"`` —
   multi-currency support is out of v1 scope (issue #52).
3. Partition each lot into one of four buckets via
   ``(close_date in FY) -> (issuer_country_iso2 == "IN") ->
   is_short_term_holding(open_date, close_date)``:

   * ``out_of_fy`` if ``close_date`` is outside ``[fy_start, fy_end]``.
   * ``excluded_indian_source`` if ISIN prefix is ``"IN"``
     (defense-in-depth; unreachable through current parser path).
   * ``excluded_ltcg_deferred`` if the lot is held > 24 calendar months
     per :func:`~ibkr_fa.models.schedule_cg_row.is_short_term_holding`
     (LTCG; handled by :mod:`ibkr_fa.compute.schedule_cg_ltcg`).
   * Otherwise: STCG. Apply Rule 115 FX **once** at the transfer
     (close) event date, build a :class:`ScheduleCgStcgLine`. A lot
     with :attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`
     takes that statutory INR cost instead of its converted USD cost.
     The INR cost, proceeds and gain keep every digit of the lot's
     amounts (#1184; see :func:`_line_inr_amounts`).

4. Sort STCG lines by ``(close_date, symbol)`` for deterministic
   output.

Rule 115 single-conversion rationale (T5 Codex review):

Rule 115(1), read with Explanation (2)(f), prescribes the capital-gains
specified date as the
last day of the calendar month immediately preceding the month in
which the capital asset is **transferred**. There is no
acquisition-date FX conversion in the rule text; both the cost
basis (in USD) and the proceeds (in USD) are converted at the
**same transfer-date TTBR**, and the realized INR gain is the
difference at that rate.

The special conversion/reconversion pathway is the **first proviso to
section 48 plus Rule 115A**, and applies only to non-residents
transferring shares or debentures of an Indian company. The second
proviso is indexation. For ordinary foreign-currency-cost shares held
by a resident, *Havells India Ltd v ACIT*, ITA 4695/Del/2012, para 14,
supports computing the foreign-currency gain first and then applying
Rule 115.

Fail-fast on:

* Any non-USD closed lot (multi-currency out of v1; #52).
* Any closed lot whose ``issuer_country_code`` is malformed
  (defense-in-depth — :class:`ClosedLot` already validates this at
  construction, so reaching this check indicates a parser bypass).
* With ``strict=True``, any Indian-source or out-of-FY lot, since
  neither schedule would emit a line for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Final, Literal

from ibkr_fa.compute.exact_inr import (
    INEXACT_PRODUCT_REFUSAL,
    checked_product,
    checked_subtraction,
    exact_decimal,
    exact_fraction,
)
from ibkr_fa.compute.lot_matching import require_indian_basis
from ibkr_fa.compute.schedule_cg_portal_amounts import (
    ScheduleCgPortalAmounts,
    schedule_cg_portal_amounts,
)
from ibkr_fa.compute.schedule_cg_table_f import (
    ScheduleCgTableFRow,
    cg_line_gains,
    schedule_cg_table_f_row,
)
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.invariants import invariant_error, require
from ibkr_fa.models import ClosedLot
from ibkr_fa.models.schedule_cg_row import (
    ScheduleCgExactAmounts,
    ScheduleCgStcgLine,
    is_short_term_holding,
)
from ibkr_fa.types import Isin, Ticker

_INDIAN_ISIN_PREFIX: Final[str] = "IN"
_RULE_115_COST_BASIS_SOURCE: Final[str] = (
    "Rule 115(1), Explanation (2)(f): USD basis x transfer TTBR"
)


def _line_inr_amounts(
    lot: ClosedLot, fx_rate: Decimal, *, schedule: Literal["STCG", "LTCG"], position: int
) -> tuple[Decimal, str, Decimal, Decimal, ScheduleCgExactAmounts | None]:
    """The lot's INR cost and its source, its INR proceeds and gain, and any exact amounts.

    The cost is the statutory override, else the USD cost at ``fx_rate``
    (Rule 115). Each conversion and the gain keep every digit (#1184): an
    exact :func:`~ibkr_fa.compute.exact_inr.checked_product` and
    :func:`~ibkr_fa.compute.exact_inr.checked_subtraction`, refusing with
    ``schedule``, the lot's 1-based input ``position`` and the column.

    A lot with an exact cost or proceeds (#1241) also gets the line's
    :class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgExactAmounts`: the same
    arithmetic on its rationals. Each INR amount it rounds is then
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of its rational. The
    Decimal arithmetic still runs first and refuses as before.
    """
    at = f"Schedule CG {schedule}, input lot {position}, column"
    override = lot.cost_basis_inr_override
    if override is None:
        cost = checked_product(lot.cost_basis_money, fx_rate, at=f"{at} cost_basis_inr")
        source = _RULE_115_COST_BASIS_SOURCE
    else:
        cost, source = override.amount_inr, override.source
    proceeds = checked_product(lot.proceeds_money, fx_rate, at=f"{at} proceeds_inr")
    gain = checked_subtraction(proceeds, cost, at=f"{at} realized_gain_inr")
    if lot.cost_basis_money_exact is None and lot.proceeds_money_exact is None:
        return cost, source, proceeds, gain, None
    exact = _exact_amounts(lot, fx_rate, at=at)
    return (
        _decimal(cost, exact.cost_basis_inr),
        source,
        _decimal(proceeds, exact.proceeds_inr),
        _decimal(gain, exact.realized_gain_inr),
        exact,
    )


def _rational(amount: Decimal, exact: Fraction | None, *, at: str) -> Fraction:
    """``exact``, or ``amount`` as a rational, refusing at ``at`` if it has none."""
    rational = exact_fraction(amount) if exact is None else exact
    if rational is None:
        raise invariant_error(what=f"{at}{INEXACT_PRODUCT_REFUSAL}")
    return rational


def _exact_amounts(lot: ClosedLot, fx_rate: Decimal, *, at: str) -> ScheduleCgExactAmounts:
    """:func:`_line_inr_amounts`' conversions on the lot's exact amounts (#1241)."""
    cost_at, proceeds_at = f"{at} cost_basis_inr", f"{at} proceeds_inr"
    cost_usd = _rational(lot.cost_basis_money, lot.cost_basis_money_exact, at=cost_at)
    proceeds_usd = _rational(lot.proceeds_money, lot.proceeds_money_exact, at=proceeds_at)
    override = lot.cost_basis_inr_override
    cost_inr = (
        cost_usd * _rational(fx_rate, None, at=cost_at)
        if override is None
        else _rational(override.amount_inr, None, at=cost_at)
    )
    proceeds_inr = proceeds_usd * _rational(fx_rate, None, at=proceeds_at)
    return ScheduleCgExactAmounts(
        cost_basis_usd=cost_usd,
        proceeds_usd=proceeds_usd,
        cost_basis_inr=cost_inr,
        proceeds_inr=proceeds_inr,
        realized_gain_inr=proceeds_inr - cost_inr,
    )


def _decimal(amount: Decimal, exact: Fraction) -> Decimal:
    """``amount`` where it is ``exact``, else :func:`~ibkr_fa.compute.exact_inr.exact_decimal`."""
    return amount if exact_fraction(amount) == exact else exact_decimal(exact)


class ScheduleCgDroppedLotError(IbkrFaError):
    """A ``strict`` caller passed a lot that no Schedule CG line would carry."""


def _refuse_dropped_lots(schedule: Literal["STCG", "LTCG"], buckets: dict[str, int]) -> None:
    """Refuse a ``strict`` run if any of ``buckets`` (name to lot count) is non-empty.

    The message gives each bucket's name and count only: no symbol, ISIN,
    date or amount.
    """
    dropped = {bucket: count for bucket, count in buckets.items() if count}
    if dropped:
        details = ", ".join(f"{bucket}: {count}" for bucket, count in dropped.items())
        raise ScheduleCgDroppedLotError(
            f"Schedule CG {schedule}: closed lots would get no line in either schedule "
            f"({details}); refusing rather than silently omitting them"
        )


def require_usd_closed_lots(materialised: list[ClosedLot], function_name: str) -> None:
    """Refuse any closed lot whose ``currency_primary`` isn't ``"USD"`` (#52).

    Both CG engines run it right after
    :func:`~ibkr_fa.compute.lot_matching.require_indian_basis`. The
    message starts with ``function_name``.
    """
    non_usd = [lot for lot in materialised if lot.currency_primary != "USD"]
    require(
        not non_usd,
        what=(
            f"{function_name}: v1 supports USD only; "
            "multi-currency support is tracked at #52. The parser at "
            "trades.py already gates this, so reaching this check "
            "indicates a parser bypass or schema drift."
        ),
        non_usd_count=len(non_usd),
        non_usd_currencies=tuple(sorted({lot.currency_primary for lot in non_usd})),
    )


@dataclass(frozen=True, slots=True)
class DeferredLtcgRow:
    """A closed lot whose holding period crosses the STCG threshold.

    Surfaced in the writer's ``_excluded_ltcg_deferred:`` audit block
    so the user can reconcile these lots against the
    ``schedule-cg-ltcg`` output and the IBKR Trades section.

    Attributes:
        closed_lot: The :class:`ClosedLot` deferred to LTCG.
        holding_period_days: ``(close - open).days`` — the lot's
            calendar-month holding period exceeds
            :data:`STCG_HOLDING_THRESHOLD_MONTHS` (24); the day-count
            is preserved for audit reference.
    """

    closed_lot: ClosedLot
    holding_period_days: int


@dataclass(frozen=True, slots=True)
class ScheduleCgStcgComputeResult:
    """Four-bucket result of the Schedule CG STCG compute stage.

    The Schedule CG writer consumes a single instance and emits the
    entire CSV (main lines + audit blocks) in one :meth:`write` call,
    mirroring the Schedule OS writer's single-call contract.

    Attributes:
        stcg_lines: In-FY, non-Indian-source, holding-period
            <= 24-month lines, sorted by ``(close_date, symbol)``.
            Becomes the body of the CG CSV.
        excluded_ltcg_deferred: In-FY, non-Indian-source closed lots
            with holding period > 24 months. Rendered in the
            ``_excluded_ltcg_deferred:`` audit block.
        excluded_indian_source: In-FY closed lots whose ISIN-derived
            issuer country is ``"IN"``. Domestic CG; out of v1 scope.
            Rendered in the ``_excluded_indian_source:`` audit block.
        excluded_out_of_fy: Closed lots whose ``close_date`` is outside
            the requested financial year. Rendered in the
            ``_excluded_out_of_fy:`` audit block so a multi-period
            statement's stray rows are visible to the user.
    """

    stcg_lines: list[ScheduleCgStcgLine]
    excluded_ltcg_deferred: list[DeferredLtcgRow]
    excluded_indian_source: list[ClosedLot]
    excluded_out_of_fy: list[ClosedLot]

    @property
    def portal_amounts(self) -> ScheduleCgPortalAmounts:
        """The whole-rupee portal figures of :attr:`stcg_lines`, computed on access."""
        return schedule_cg_portal_amounts(self.stcg_lines, schedule="STCG")

    @property
    def table_f(self) -> ScheduleCgTableFRow:
        """Schedule CG Table F row 3 of :attr:`stcg_lines`, computed on access (#1360).

        Split to :attr:`portal_amounts`' balance, the filed result.
        """
        return schedule_cg_table_f_row(
            cg_line_gains(self.stcg_lines),
            result_inr=self.portal_amounts.balance_inr,
        )


def compute_schedule_cg_stcg_lines(
    closed_lots: Iterable[ClosedLot],
    *,
    financial_year_start: date,
    financial_year_end: date,
    fx_policy: AuditedFxPolicy,
    strict: bool = False,
) -> ScheduleCgStcgComputeResult:
    """Compute the Schedule CG STCG lines for an FY.

    Args:
        closed_lots: All FIFO-matched :class:`ClosedLot` records (the
            output of :func:`~ibkr_fa.compute.lot_matching.match_fifo`).
            Outside-FY rows are partitioned into
            :attr:`ScheduleCgStcgComputeResult.excluded_out_of_fy`
            (visible audit, not silently dropped). A generator
            argument is materialised to a list before any check so
            the iterator is never exhausted by the precondition pass.
        financial_year_start: First day of the Indian FY being
            reported (e.g., ``date(2025, 4, 1)`` for FY 2025-26).
        financial_year_end: Last day of the Indian FY being reported
            (e.g., ``date(2026, 3, 31)`` for FY 2025-26).
        fx_policy: The :class:`~ibkr_fa.fx.policy.AuditedFxPolicy` to
            use for per-event FX conversion. v1 production uses
            :class:`~ibkr_fa.fx.policy_rule115_capital_gains.Rule115CapitalGainsPolicy`
            (Rule 115 preceding-month-end with carry-back). The
            policy is queried once per STCG row at the lot's
            transfer (close) date — single conversion per Rule 115(1),
            Explanation (2)(f); see module docstring. The line's rate
            and ``fx_specified_date`` both come from that one
            resolution.
        strict: Refuse when any lot lands in ``excluded_indian_source``
            or ``excluded_out_of_fy``. For callers whose every lot must
            get a line in this schedule or the LTCG one; the
            ``excluded_ltcg_deferred`` bucket never refuses.

    Returns:
        A :class:`ScheduleCgStcgComputeResult` with four buckets;
        see the class docstring.

    Raises:
        InvariantError: If any input closed lot has
            ``currency_primary != "USD"``. v1 only supports USD;
            multi-currency tracking is at #52. Defense-in-depth —
            the parser already gates this, so reaching this check
            indicates a parser bypass or schema drift. Also raised by
            :class:`ScheduleCgStcgLine` when the policy's specified
            date is not strictly before the close date (a mis-wired
            policy, e.g. the FY-end clause (c) policy).
        MissingFxRateError: If the transfer-event specified date
            has no Rule 115 rate available within the policy's
            carry-back window.
        BrokerBasisError: If a broker-matched lot's holding-period
            date moved (#599); see
            :func:`~ibkr_fa.compute.lot_matching.require_indian_basis`.
        ScheduleCgDroppedLotError: With ``strict=True``, if any lot
            lands in a bucket that refuses (see ``strict``).
    """
    materialised = list(closed_lots)
    require_indian_basis(materialised)
    require_usd_closed_lots(materialised, "compute_schedule_cg_stcg_lines")

    stcg_lines: list[ScheduleCgStcgLine] = []
    excluded_ltcg_deferred: list[DeferredLtcgRow] = []
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
        if not is_short_term_holding(lot.open_date_time, lot.close_date_time):
            excluded_ltcg_deferred.append(
                DeferredLtcgRow(closed_lot=lot, holding_period_days=holding_period_days)
            )
            continue

        resolution = fx_policy.rate_for_audited(lot.close_date_time)
        fx_rate = resolution.rate

        # T5 (Codex CLI gpt-5.5) PR #163 P1 fix: Rule 115 prescribes
        # ONE specified date for capital gains — the transfer-event's
        # preceding-month-end. Both the cost basis and the proceeds are
        # converted at the SAME transfer-date TTBR; the realized INR
        # gain is the difference at that rate. The double-conversion
        # special conversion/reconversion pathway is the first proviso
        # to section 48 plus Rule 115A, which applies only to non-residents
        # transferring shares/debentures of an Indian company —
        # NOT to a resident assessee transferring foreign equities.
        (cost_basis_inr, cost_basis_inr_source, proceeds_inr, realized_gain_inr, exact_amounts) = (
            _line_inr_amounts(lot, fx_rate, schedule="STCG", position=position)
        )

        stcg_lines.append(
            ScheduleCgStcgLine(
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
            "STCG",
            {"Indian-source": len(excluded_indian_source), "out-of-FY": len(excluded_out_of_fy)},
        )
    stcg_lines.sort(key=lambda line: (line.close_date, str(line.symbol)))

    return ScheduleCgStcgComputeResult(
        stcg_lines=stcg_lines,
        excluded_ltcg_deferred=excluded_ltcg_deferred,
        excluded_indian_source=excluded_indian_source,
        excluded_out_of_fy=excluded_out_of_fy,
    )
