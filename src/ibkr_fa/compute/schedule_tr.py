"""Schedule TR (Summary of Tax Relief Claimed) compute layer.

Schedule TR aggregates Schedule FSI's per-(country, head-of-income)
rows into per-country totals. A country gets a TR row only when its
foreign tax, summed from each FSI row's tax rounded to whole rupees,
is positive. The row carries that total (summed across all heads of
income), the assessee's foreign TIN, and optionally the relief
available in India and the Section under which relief is claimed
(§90 / §90A / §91 per the Income-tax Act, 1961).

Relief population (#36 / #37, PR #277): ``tax_relief_inr_total`` is
the sum of each contributing FSI row's relief rounded to whole
rupees; FSI relief is Form 67's §90 / §91 credit. It and
``section_under_which_claimed`` are filled together, only when every
contributing FSI row has a computed relief AND the country's
positive-relief rows resolve to exactly one section. Both are left
``None`` (manual fill) when some row has no computed relief
(incomplete evidence), or when the country's relief is all zero
before rounding (no positive-relief row names a section). Positive
relief that rounds to zero files ``0`` with its section. Either zero
is a Form 67 credit that files as ₹0, which every command that writes
Schedule TR refuses before writing it (#601). Positive-relief rows on
both §90 and §91 are inconsistent input, not manual fill: the compute
refuses them. See :func:`compute_schedule_tr_from_fsi` for the exact
gating.

The TIN is copied from the FSI rows (stamped from
``[account.foreign_tins]``) and is ``None`` when the country has none.

Form 67's credit, which the FSI relief carries, is computed in
:mod:`ibkr_fa.compute.form67` from:

* Exact source-level Indian tax (``[[form67.tax_payable_overrides]]``).
* Per-country DTAA reference data (CSV mapping country → §90
  article number / §91 unilateral fallback), plus
  ``[form67.section_91]`` for a §91 credit.

Aggregation semantics (matches Schedule TR portal contract):

* Schedule FSI carries per-(cbdt_country_code, head_of_income)
  rows. Schedule TR collapses the head dimension by summing
  ``foreign_tax_inr`` across all heads for each country.

* **Portal-reconciliation rounding** (T3 r1 P2 #1 fix for PR
  #216): the ITR-2 portal labels Schedule TR's foreign-tax-
  paid total as "total of (c) of Schedule FSI" — i.e., the SUM
  of FSI's portal-displayed (HALF_UP rounded) cells, NOT the
  HALF_UP round of the full-precision sum. Two FSI rows with
  ``foreign_tax_inr = 2.50`` each display as ₹3 + ₹3 = ₹6 on
  the portal, but their full-precision sum is 5.00 which would
  round to ₹5 (off by ₹1). The compute layer therefore sums
  each contributing FSI row's filed whole-rupee tax
  (:attr:`~ibkr_fa.models.schedule_fsi_row.ScheduleFsiRow.filed`,
  #900) into :attr:`ScheduleTrRow.foreign_tax_inr_total` (the
  portal-displayed cell), so the TR total agrees with the
  user's per-row arithmetic on Schedule FSI. The un-rounded
  full-precision sum is preserved separately in
  :attr:`ScheduleTrRow.foreign_tax_inr_total_full_precision`
  for FSI ⇄ TR audit reconciliation (PR #216 T2/T3 r2 follow-
  up); the audit field does NOT drive any portal-displayed
  cell.

* Countries whose summed, rounded foreign tax is not positive are
  SILENTLY DROPPED from Schedule TR: e.g. a capital-gains-only
  country with no foreign WHT, or FSI rows totalling ₹0.40 of tax.
  They have nothing to relieve.

* ``event_count`` on the TR row counts the number of FSI rows
  that fed into it (not the underlying source events themselves
  — that's already aggregated at the FSI layer).

* FSI rows whose relief the portal's read-only cell would overwrite
  (#900) are carried on
  :attr:`ScheduleTrComputeResult.fsi_rows_with_overwritten_relief`,
  so every TR writer refuses the result.

See :mod:`ibkr_fa.compute.schedule_fsi` for the per-(country, head)
upstream aggregation that produces the input, and
:func:`ibkr_fa.pipeline_tr.run_schedule_tr` for the pipeline that
runs this compute and the registered TR writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ibkr_fa.compute.schedule_fsi import ScheduleFsiComputeResult
from ibkr_fa.invariants import require
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.schedule_tr_row import ScheduleTrRow, TaxReliefSection

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class ScheduleTrComputeResult:
    """Result of the Schedule TR compute stage.

    Each registered Schedule TR writer consumes a single
    :class:`ScheduleTrComputeResult` value and emits the entire
    portal-form output (one row per
    :class:`~ibkr_fa.models.ScheduleTrRow` in :attr:`rows`) in
    one ``write`` call — mirroring the Schedule FSI / OS / CG
    writers' single-call contract.

    Attributes:
        rows: Materialised per-country aggregate rows, sorted by
            ``cbdt_country_code`` for deterministic output. At
            most one row per ``cbdt_country_code`` (verified at
            construction). Empty list is valid (e.g., a statement
            with no foreign tax paid — pure capital-gains FSI
            output with no WHT, no broker interest WHT, etc.).
        fsi_rows_with_overwritten_relief: The aggregated Schedule FSI
            rows whose relief the portal's read-only FSI cell would
            overwrite (#900). Every TR writer refuses a result that
            carries any, through
            :func:`~ibkr_fa.compute.schedule_fsi.require_fsi_relief_matches_portal`.

    Invariants enforced in :meth:`__post_init__`:

    * No duplicate ``cbdt_country_code`` values across :attr:`rows`.
      A duplicate indicates an aggregation bug.
    * Rows are sorted by ``cbdt_country_code`` lexicographically
      (matches the Schedule FSI sort convention; CBDT codes are
      vendored as strings).
    * Every row in :attr:`fsi_rows_with_overwritten_relief` has its
      relief overwritten.
    """

    rows: list[ScheduleTrRow]
    fsi_rows_with_overwritten_relief: tuple[ScheduleFsiRow, ...] = ()

    def __post_init__(self) -> None:
        for fsi_row in self.fsi_rows_with_overwritten_relief:
            require(
                fsi_row.filed.portal_overwrites_relief,
                what=(
                    "ScheduleTrComputeResult.fsi_rows_with_overwritten_relief "
                    "must only carry FSI rows whose relief the portal overwrites"
                ),
                country=fsi_row.iso_alpha2,
                head=fsi_row.head_of_income.value,
            )
        seen: set[str] = set()
        for row in self.rows:
            require(
                row.cbdt_country_code not in seen,
                what=(
                    "ScheduleTrComputeResult.rows must contain at most "
                    "one row per cbdt_country_code; duplicate indicates "
                    "an aggregation bug in compute"
                ),
                cbdt_country_code=row.cbdt_country_code,
            )
            seen.add(row.cbdt_country_code)
        expected_order = sorted(self.rows, key=lambda r: r.cbdt_country_code)
        require(
            self.rows == expected_order,
            what=(
                "ScheduleTrComputeResult.rows must be sorted by "
                "cbdt_country_code ascending for deterministic output"
            ),
        )


def compute_schedule_tr_from_fsi(
    fsi_result: ScheduleFsiComputeResult,
) -> ScheduleTrComputeResult:
    """Aggregate Schedule FSI rows into per-country Schedule TR rows.

    Collapses Schedule FSI's per-(country, head_of_income) rows
    into per-country rows by summing ``foreign_tax_inr`` across
    all heads for each country. The (country, head) dimension that
    Schedule FSI surfaces separately is collapsed here because
    Schedule TR is per-country only — the head_of_income dimension
    matters for the FSI portal display but not for the TR relief
    summary.

    A country whose foreign tax, summed from each FSI row's tax
    rounded to whole rupees, is not positive is SILENTLY DROPPED —
    it has nothing to relieve. A capital-gains-only country with no
    foreign WHT (the common DriveWealth-via-IBKR scenario for
    non-US securities) would therefore be absent from the TR output
    even though it appears in the FSI output.

    FTC (issues #36/#37): ``tax_relief_inr_total`` and
    ``section_under_which_claimed`` are populated by aggregating the
    per-country relief carried on the enriched FSI rows (whose relief is
    Form 67's §90/§91 credit — the single source of truth, so FSI / TR /
    Form 67 agree). The relief is the sum of each row's relief rounded
    to whole rupees. Both fields are filled ONLY when both conditions
    hold:

    * EVERY one of the country's FSI rows has a computed relief; one
      uncomputed row (e.g. a Capital-Gains bucket with foreign tax)
      leaves both ``None`` so a partial total is never shown;
    * the positive-relief rows resolve to exactly one section (``90``
      for a treaty/DTAA credit, ``91`` for unilateral relief). A
      country whose relief is all zero before rounding has no
      positive-relief row, so both stay ``None``; positive relief that
      rounds to zero files ``0`` with its section. Either zero is a
      Form 67 credit that files as ₹0, which every command that writes
      Schedule TR refuses before writing it (#601).

    Positive-relief rows on both §90 and §91 are inconsistent input,
    not manual fill: they refuse, ahead of every blank or omit
    outcome for that country. The writer renders ``None`` as an
    EMPTY cell.

    Args:
        fsi_result: The Schedule FSI compute result whose ``rows``
            attribute carries the per-(country, head) breakdown.
            Pass the full
            :class:`~ibkr_fa.compute.schedule_fsi.ScheduleFsiComputeResult`
            from
            :func:`~ibkr_fa.compute.schedule_fsi.compute_schedule_fsi`.

    Returns:
        A :class:`ScheduleTrComputeResult` with one row per
        positive-foreign-tax country, sorted by CBDT code.

    Raises:
        :class:`~ibkr_fa.invariants.InvariantError`: If a country's
            positive-relief rows resolve to more than one section
            (no supported producer emits that), or if the
            aggregated row fails a
            :class:`~ibkr_fa.models.ScheduleTrRow` invariant
            (extremely defensive — the inputs come from a
            validated FSI result, so this should never fire in
            practice).
    """
    per_country_tax_portal: dict[str, Decimal] = {}
    per_country_tax_full_precision: dict[str, Decimal] = {}
    per_country_event_count: dict[str, int] = {}
    per_country_meta: dict[str, tuple[str, str]] = {}  # cbdt -> (iso, country_name)
    per_country_tin: dict[str, str | None] = {}  # cbdt -> assessee foreign TIN
    # #36/#37: per-country FTC, joined from the enriched FSI rows (whose relief
    # is the single source of truth = Form 67's §90/§91 credit).
    per_country_relief_portal: dict[str, Decimal] = {}
    overwritten_relief: list[ScheduleFsiRow] = []
    per_country_relief_uncomputed: dict[str, bool] = {}
    # cbdt -> (head, section) of each positive-relief row.
    per_country_positive_relief: dict[str, list[tuple[HeadOfIncome, TaxReliefSection]]] = {}
    for fsi_row in fsi_result.rows:
        cbdt = fsi_row.cbdt_country_code
        filed = fsi_row.filed
        if filed.portal_overwrites_relief:
            overwritten_relief.append(fsi_row)
        # T3 r1 P2 #1 fix: sum EACH FSI row's filed whole-rupee
        # foreign tax into the portal-displayed total, matching the
        # ITR-2 portal's "total of (c) of Schedule FSI in respect of
        # each country" formula. Two FSI rows of ₹2.50 each show as
        # ₹3 + ₹3 = ₹6 on the portal, not ₹5 = round(2.50 + 2.50).
        # T2 r2 P2 / T3 r2 P4 follow-up: the full-precision sum (here,
        # ₹5.00) is also tracked separately so the audit cell can
        # render the true FSI⇄TR full-precision reconciliation value
        # distinctly from the portal-displayed cell.
        per_country_tax_portal[cbdt] = (
            per_country_tax_portal.get(cbdt, _ZERO) + filed.foreign_tax_inr
        )
        per_country_tax_full_precision[cbdt] = (
            per_country_tax_full_precision.get(cbdt, _ZERO) + fsi_row.foreign_tax_inr
        )
        per_country_event_count[cbdt] = per_country_event_count.get(cbdt, 0) + 1
        # FSI invariants guarantee (iso, country_name) is consistent
        # for the same cbdt code: every (iso → cbdt) mapping is unique
        # in the vendored country-codes CSV (T2 r1 verification), so
        # two FSI rows sharing a cbdt_country_code share their full
        # (iso, country_name) tuple by construction. First-seen wins
        # here; subsequent rows for the same country structurally
        # agree.
        if cbdt not in per_country_meta:
            per_country_meta[cbdt] = (fsi_row.iso_alpha2, fsi_row.country_name_portal)
            # The TIN is per-country (same on every FSI row of a country, as
            # it is stamped from foreign_tins[iso]); first-seen wins.
            per_country_tin[cbdt] = fsi_row.taxpayer_id_number
        # FTC accumulation. A country's relief is filled only when EVERY one
        # of its rows has a computed relief; a single uncomputed (None) row —
        # e.g. a Capital-Gains bucket with foreign tax, or a dividend row mixed
        # with un-credited foreign interest — leaves the whole country
        # manual-fill so a PARTIAL (understated) total is never shown. The
        # per-row filed whole-rupee relief is summed, matching the
        # foreign-tax portal convention above.
        # The filed relief is None exactly when the raw relief is.
        raw_relief = fsi_row.tax_relief_inr
        filed_relief = filed.tax_relief_inr
        if raw_relief is None or filed_relief is None:
            per_country_relief_uncomputed[cbdt] = True
        else:
            per_country_relief_uncomputed.setdefault(cbdt, False)
            per_country_relief_portal[cbdt] = (
                per_country_relief_portal.get(cbdt, _ZERO) + filed_relief
            )
            if raw_relief > _ZERO:
                # A populated DTAA article marks a treaty (§90) credit; its
                # absence on a POSITIVE credit marks a §91 unilateral credit.
                per_country_positive_relief.setdefault(cbdt, []).append(
                    (
                        fsi_row.head_of_income,
                        TaxReliefSection.SECTION_90
                        if fsi_row.dtaa_article is not None
                        else TaxReliefSection.SECTION_91,
                    )
                )

    rows: list[ScheduleTrRow] = []
    for cbdt in sorted(per_country_tax_portal):
        iso, country_name = per_country_meta[cbdt]
        positive_relief = per_country_positive_relief.get(cbdt, [])
        country_sections = {section for _, section in positive_relief}
        # Checked before the omission and completeness gates below: one
        # country's relief on both §90 and §91 has no supported producer, so
        # it refuses rather than being left blank or dropped.
        require(
            len(country_sections) <= 1,
            what=(
                "Schedule TR: a country's positive-relief FSI rows resolve to more than one section"
            ),
            cbdt_country_code=cbdt,
            iso_alpha2=iso,
            sections=tuple(sorted(str(section) for section in country_sections)),
            positive_relief_rows=tuple(f"{head}: §{section}" for head, section in positive_relief),
        )
        foreign_tax_portal = per_country_tax_portal[cbdt]
        if foreign_tax_portal <= 0:
            # Country has no positive foreign tax to relieve — drop.
            # ``foreign_tax_portal == 0`` when EVERY contributing FSI
            # row's foreign_tax_inr is below 0.5 (each rounds HALF_UP
            # to 0; 0.5 itself rounds to 1). That includes FSI rows
            # with zero foreign tax, which FSI allows (e.g. capital
            # gains with no foreign WHT).
            continue
        # FTC: fill relief + section only when every contributing FSI row
        # had a computed relief AND the positive-relief rows resolve to
        # exactly one section; otherwise both stay None (manual fill, per
        # the model's symmetric coupling).
        section = (
            None
            if per_country_relief_uncomputed.get(cbdt, True) or len(country_sections) != 1
            else next(iter(country_sections))
        )
        relief_total = None if section is None else per_country_relief_portal.get(cbdt, _ZERO)
        rows.append(
            ScheduleTrRow(
                cbdt_country_code=cbdt,
                country_name_portal=country_name,
                iso_alpha2=iso,
                foreign_tax_inr_total=foreign_tax_portal,
                event_count=per_country_event_count[cbdt],
                foreign_tax_inr_total_full_precision=per_country_tax_full_precision[cbdt],
                tax_relief_inr_total=relief_total,
                section_under_which_claimed=section,
                taxpayer_id_number=per_country_tin[cbdt],
            )
        )
    return ScheduleTrComputeResult(
        rows=rows, fsi_rows_with_overwritten_relief=tuple(overwritten_relief)
    )


__all__ = [
    "ScheduleTrComputeResult",
    "compute_schedule_tr_from_fsi",
]
