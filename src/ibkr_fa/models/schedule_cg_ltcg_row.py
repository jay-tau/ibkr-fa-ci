"""Schedule CG LTCG line: one foreign long-term capital-gains row.

Each :class:`ScheduleCgLtcgLine` represents one disposed tax lot whose
holding period is **> 24 months** (the foreign-equity LTCG threshold per
Section 2(42A) of the Income-tax Act, 1961, post the Finance (No. 2) Act,
2024 simplification), with sale proceeds converted to INR via
Rule 115(1), Explanation (2)(f) (TTBR of the last day of the month
immediately preceding the **transfer** date). The cost side is either
converted at that rate for ordinary foreign-currency-cost shares or
supplied as a statutory historical INR basis by a source adapter.

Rate-regime context (Finance (No. 2) Act, 2024):

* Disposals **on or after 2024-07-23**: LTCG taxed at flat **12.5%
  without indexation**. v1 supports this regime only.
* Disposals **before 2024-07-23**: LTCG taxed at **20% with
  indexation** (cost basis multiplied by CII_disposal_year /
  CII_acquisition_year). Out of v1 scope — requires bundled CII
  reference data and an indexed-cost-basis field on the model.
  Tracked at follow-up issue created at PR open.

The model is the LTCG counterpart to
:class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgStcgLine` and is
structurally identical except for the holding-period invariant
(which requires LONG-TERM rather than SHORT-TERM). Same Rule 115
single-conversion semantics, same OWASP escapes apply at the writer
layer, and the same negative-proceeds tolerance (the upstream
:class:`~ibkr_fa.models.closed_lot.ClosedLot` permits net proceeds
to go below zero on deep losses).

The companion
:class:`~ibkr_fa.compute.schedule_cg_ltcg.ScheduleCgLtcgComputeResult`
partitions :class:`~ibkr_fa.models.ClosedLot` records into five
buckets:

* :class:`ScheduleCgLtcgLine` rows (in-FY, > 24mo, non-Indian-source,
  post-2024-07-23 disposal).
* STCG-deferred rows (in-FY, <= 24mo) — handled by the
  :func:`~ibkr_fa.compute.schedule_cg.compute_schedule_cg_stcg_lines`
  pipeline, surfaced here as an audit block.
* Pre-2024-07-23 rejects (in-FY, > 24mo, disposed before
  2024-07-23) — requires indexation; out of v1 scope.
* Indian-source exclusions — defense-in-depth bucket (parser hard-
  rejects IN ISINs upstream per #143).
* Out-of-FY exclusions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.invariants import require
from ibkr_fa.models.schedule_cg_row import (
    ScheduleCgExactAmounts,
    is_short_term_holding,
    require_cg_line_amounts_and_holding_days,
    require_cg_line_fx_issuer_and_derived,
)
from ibkr_fa.types import Isin, Ticker

NEW_REGIME_CUTOVER_DATE: Final[date] = date(2024, 7, 23)
"""Finance (No. 2) Act, 2024 LTCG rate-regime cutover date.

Disposals on or AFTER this date use the new regime: 12.5% rate without
indexation. Disposals strictly BEFORE this date use the old regime:
20% rate with indexation (cost basis adjusted via CBDT-published
Cost Inflation Index).

v1 of :mod:`~ibkr_fa.compute.schedule_cg_ltcg` supports only the
new regime; the compute layer rejects pre-cutover disposals with a
clear error pointing at the follow-up issue for indexation support.
The cutover-date constant lives in the model module so the compute
classification logic and the model invariants share one source of
truth.
"""


@dataclass(frozen=True, slots=True)
class ScheduleCgLtcgLine:
    """One foreign-source LTCG line ready for Schedule CG.

    Each instance represents a single in-FY, non-Indian-source closed
    lot whose holding period satisfies the LTCG threshold
    (``close_date > open_date + 24 calendar months``) AND whose
    disposal date is on or after the Finance (No. 2) Act, 2024
    cutover (2024-07-23 — the new-regime 12.5%-without-indexation
    cut-in), with cost basis and proceeds pre-converted to INR via
    Rule 115(1), Explanation (2)(f), TTBR at the transfer preceding-month-end.

    Attributes (identical to
    :class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgStcgLine`
    apart from the holding-period invariant):

        symbol: Ticker of the closed instrument.
        isin: ISIN of the closed instrument (ISO 6166).
        issuer_country_iso2: ISO 3166-1 alpha-2 country code derived
            from the ISIN's leading two characters. Always
            non-``"IN"`` in this dataclass.
        description: Verbatim audit copy of
            :attr:`~ibkr_fa.models.ClosedLot.description`.
        open_date: Date the lot was originally opened.
        close_date: Date the lot was closed. Strictly **>** ``open_date
            + 24 calendar months`` (LTCG threshold); strictly **>=**
            :data:`NEW_REGIME_CUTOVER_DATE` (v1 scope).
        holding_period_days: ``(close_date - open_date).days``.
            Non-negative.
        quantity: Number of shares disposed in this slice. Strictly
            positive.
        cost_basis_usd: Cost basis in USD. Non-negative.
        proceeds_usd: Gross sale proceeds in USD. May be negative
            (IBKR commissions on a tiny / deep-loss sale can exceed
            gross proceeds; see the ScheduleCgStcgLine docstring for
            the canonical rationale).
        fx_specified_date: Rule 115 capital-gains specified date — last day of
            the calendar month immediately preceding
            :attr:`close_date`. Always strictly before
            :attr:`close_date`.
        fx_rate_publication_date: The SBI archive date the rate
            actually came from. On or before :attr:`fx_specified_date`;
            earlier only after a carry-back (#741).
        fx_rate_inr_per_usd: INR-per-USD rate applied at the
            transfer event. Strictly positive.
        cost_basis_inr: Statutory INR cost basis at full
            :class:`~decimal.Decimal` precision. For ordinary purchased shares,
            this is ``cost_basis_usd * fx_rate_inr_per_usd``. A lot with
            :attr:`~ibkr_fa.models.closed_lot.ClosedLot.cost_basis_inr_override`
            (section 49(2AA) employee shares) carries that INR amount instead.
        cost_basis_inr_source: Audit provenance for ``cost_basis_inr``.
        proceeds_inr: ``proceeds_usd * fx_rate_inr_per_usd`` at full
            :class:`~decimal.Decimal` precision. May be negative.
        realized_gain_inr: ``proceeds_inr - cost_basis_inr`` at full
            :class:`~decimal.Decimal` precision. May be negative —
            an LTCG loss is a valid Schedule CG row (Section 74's
            8-AY carry-forward applies to long-term losses too,
            with the restriction that they can only set off against
            long-term gains).
        exact_amounts: The exact amounts, as for
            :class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgStcgLine` (#1241).

    Invariants enforced in :meth:`__post_init__`:

    * ``quantity``, ``cost_basis_usd``, ``proceeds_usd``,
      ``fx_rate_inr_per_usd``, ``cost_basis_inr``, ``proceeds_inr`` and
      ``realized_gain_inr`` are finite :class:`~decimal.Decimal` values
    * ``quantity > 0``
    * ``cost_basis_usd >= 0``
    * ``fx_rate_inr_per_usd > 0``
    * ``cost_basis_inr >= 0``
    * ``close_date >= open_date``
    * ``holding_period_days == (close_date - open_date).days``
    * ``holding_period_days >= 0``
    * Calendar-month LTCG cutoff: ``close_date > open_date +
      24 calendar months`` (the inverse of
      :func:`~ibkr_fa.models.schedule_cg_row.is_short_term_holding`).
    * ``close_date >= NEW_REGIME_CUTOVER_DATE`` (v1 scope; pre-
      cutover disposals require indexation, not implemented).
    * ``fx_specified_date < close_date``
    * ``fx_rate_publication_date`` is a :class:`~datetime.date` (not a
      :class:`~datetime.datetime`) and ``<= fx_specified_date``
    * ``issuer_country_iso2 != "IN"`` and 2-letter uppercase ISO
      3166-1 alpha-2 code.
    * ``isin`` matches ISO 6166.
    * ``proceeds_inr == proceeds_usd * fx_rate_inr_per_usd`` and
      ``realized_gain_inr == proceeds_inr - cost_basis_inr``, each
      against the exact result, as for
      :class:`~ibkr_fa.models.schedule_cg_row.ScheduleCgStcgLine` (#1184);
      ``cost_basis_inr`` is not tied to the rate. With ``exact_amounts``,
      they hold of the exact amounts instead
      (:func:`~ibkr_fa.models.schedule_cg_row.require_exact_amounts`).

    Notably absent: ``proceeds_usd >= 0`` and ``proceeds_inr >= 0`` —
    negative net proceeds are legitimate (see STCG line docstring
    for the rationale; writer escapes via
    :func:`~ibkr_fa.output._writer_contract.csv_safe_signed`).
    """

    symbol: Ticker
    isin: Isin
    issuer_country_iso2: str
    description: str
    open_date: date
    close_date: date
    holding_period_days: int
    quantity: Decimal
    cost_basis_usd: Decimal
    proceeds_usd: Decimal
    fx_specified_date: date
    fx_rate_publication_date: date
    fx_rate_inr_per_usd: Decimal
    cost_basis_inr: Decimal
    cost_basis_inr_source: str
    proceeds_inr: Decimal
    realized_gain_inr: Decimal
    exact_amounts: ScheduleCgExactAmounts | None = None

    def __post_init__(self) -> None:
        require_cg_line_amounts_and_holding_days(self, "ScheduleCgLtcgLine")
        # Defense-in-depth: the compute layer is responsible for
        # routing short-term lots to the STCG bucket. A misrouted
        # short-term lot reaching this constructor would silently
        # over-report LTCG and under-report STCG. The invariant is
        # the inverse of is_short_term_holding (calendar-month
        # arithmetic with last-day clamping, matching Section 2(42A)).
        require(
            not is_short_term_holding(self.open_date, self.close_date),
            what=(
                "ScheduleCgLtcgLine: close_date must be strictly after "
                "open_date + 24 calendar months (foreign-equity LTCG "
                "threshold per Section 2(42A); short-term lots belong "
                "in the STCG pipeline — see schedule-cg subcommand)"
            ),
            open_date=self.open_date,
            close_date=self.close_date,
            symbol=self.symbol,
            isin=self.isin,
        )
        # v1 scope: only the new regime (12.5% without indexation,
        # disposals on or after Finance (No. 2) Act, 2024 cut-in
        # date 2024-07-23). Pre-cutover disposals require indexation
        # via CBDT-published Cost Inflation Index — out of v1 scope.
        require(
            self.close_date >= NEW_REGIME_CUTOVER_DATE,
            what=(
                "ScheduleCgLtcgLine.close_date must be on or after "
                f"{NEW_REGIME_CUTOVER_DATE.isoformat()} (Finance (No. 2) "
                "Act, 2024 new-regime cut-in date). Pre-cutover disposals "
                "use the 20%-with-indexation regime and require CBDT "
                "Cost Inflation Index data — out of v1 scope"
            ),
            close_date=self.close_date,
            cutover_date=NEW_REGIME_CUTOVER_DATE,
            symbol=self.symbol,
            isin=self.isin,
        )
        require_cg_line_fx_issuer_and_derived(
            self, "ScheduleCgLtcgLine", "ScheduleCgLtcgComputeResult", "LTCG"
        )
