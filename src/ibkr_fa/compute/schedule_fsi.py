"""Compute Schedule FSI per-(country, head-of-income) aggregates.

Aggregates the FX-converted income lines produced by the per-
schedule compute layers (Schedule OS dividends + Schedule CG STCG +
Schedule CG LTCG + Schedule OS interest) into the cross-country,
cross-head bucket structure ITR-2 Schedule FSI requires.

Aggregators (issue #35):

* Foreign-source **dividends** via
  :func:`compute_schedule_fsi_from_dividends` (foreign-source details
  from Schedule OS row 1a(i)).
* Foreign-source **capital gains** (STCG + LTCG combined) via
  :func:`compute_schedule_fsi_from_capital_gains`. STCG and LTCG
  share the single
  :attr:`~ibkr_fa.models.HeadOfIncome.CAPITAL_GAINS` bucket per
  Schedule FSI's portal layout (no STCG / LTCG split on FSI; the
  per-rate split happens on Schedule SI).
* Foreign-source **broker credit-interest** (Schedule OS row
  1b(ix)) via :func:`compute_schedule_fsi_from_interest`. All
  in-FY credit interest is attributed to a SINGLE
  ``(broker_domicile_iso2, OTHER_SOURCES)`` row. The caller
  must state the payer's country; there is no default (#698).
* **Substitute dividends** and reviewed residual **other income**
  via :func:`compute_schedule_fsi_from_substitute_dividends`
  and :func:`compute_schedule_fsi_from_other_income`, one Other
  Sources row per source country.
* Reviewed foreign **business/profession income** via
  :func:`compute_schedule_fsi_from_business_income`.
* Foreign **withholding tax** on dividends
  (:class:`~ibkr_fa.models.withholding_tax.WithholdingTax`) via
  :func:`compute_schedule_fsi_from_withholding_tax`. Emits
  per-country tax-only fragments
  (:class:`ScheduleFsiWhtContribution`) that are layered onto
  the matching income rows by the
  :func:`_merge_results` primitive, populating the
  ``foreign_tax_inr`` column. FX conversion uses Rule 128(5)(ii)
  preceding-month-end SBI TTBR
  (:class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`).
  Reversals (positive amounts in IBKR's sign convention) are
  netted per-country against tax-paid (negative) amounts before
  emission; net-refund countries are dropped.

The ``schedule-fsi`` subcommand runs in :mod:`ibkr_fa.pipeline_fsi`; the
writers are :mod:`ibkr_fa.output.schedules.fsi` (filing CSV),
:mod:`ibkr_fa.output.schedule_fsi_review_csv` and
:mod:`ibkr_fa.output.schedule_fsi_xlsx`; the per-AY portal labels are in
:mod:`ibkr_fa.output.portal_labels`.

The income-only aggregators (dividends, capital gains, broker
interest) emit ``foreign_tax_inr = Decimal("0")`` on every row.
The :func:`compute_schedule_fsi_from_withholding_tax` aggregator
contributes the ``foreign_tax_inr`` column via a tax-only
fragment type (:class:`ScheduleFsiWhtContribution`) that the
:func:`_merge_results` primitive layers onto the
matching income rows after income aggregation. Orphan WHT (a
country with foreign tax withheld but no FSI income on the
matching head) raises :class:`~ibkr_fa.invariants.InvariantError`
— see :func:`_merge_results`' "Withholding-tax fragments"
docstring section for the rationale and the (#183 ADR-rerouting)
edge case it surfaces.

Why aggregate at THIS layer (not at the writer):

* **Single source of truth** for Schedule FSI numbers. Schedule TR
  (#36) is built from these rows
  (:func:`~ibkr_fa.compute.schedule_tr.compute_schedule_tr_from_fsi`)
  rather than re-aggregated from raw events, so a per-row filter
  change can't make the two schedules drift apart. Form 67's (#37)
  relief is copied onto the matching rows
  (:func:`~ibkr_fa.compute.schedule_fsi_relief.enrich_fsi_with_form67_relief`).
* **Audit-friendly invariants**. The aggregation invariants
  (positive income, non-negative tax, at-most-one row per
  ``(country, head)`` pair) are enforced once here on
  :class:`~ibkr_fa.models.ScheduleFsiRow.__post_init__` rather
  than re-derived from scratch in each writer.
* **Decoupling**. The writer stays a thin
  :class:`ScheduleFsiComputeResult` → CSV/XLSX transform. New
  FSI consumers (PDF writer, JSON-for-utility writer, etc.) can
  reuse the compute result without re-implementing the
  aggregation logic.

Schedule FA / OS / CG analog: this module's role mirrors
:mod:`ibkr_fa.compute.fa_a3` (FA-A3 row aggregation),
:mod:`ibkr_fa.compute.schedule_os` (OS line construction), etc.

Issue #35.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Final

from ibkr_fa.compute.exact_inr import (
    checked_addition,
    checked_exact_sum,
    exact_decimal,
    exact_inr_sum,
    exact_signed_inr_sum,
)
from ibkr_fa.compute.foreign_tax import net_foreign_tax_by_country
from ibkr_fa.compute.form67 import Form67ComputeResult, require_no_refused_form67_rows
from ibkr_fa.compute.schedule_cg import ScheduleCgStcgComputeResult
from ibkr_fa.compute.schedule_cg_ltcg import ScheduleCgLtcgComputeResult
from ibkr_fa.compute.schedule_os import (
    ScheduleOsComputeResult,
    ScheduleOsDividendLine,
)
from ibkr_fa.compute.schedule_os_interest import (
    ScheduleOsInterestComputeResult,
)
from ibkr_fa.compute.schedule_os_other_income import (
    OTHER_INCOME_LABEL,
    ScheduleOsOtherIncomeComputeResult,
)
from ibkr_fa.compute.schedule_os_periods import DIVIDEND_LABEL, split_whole_rupees
from ibkr_fa.compute.schedule_os_substitute_dividend import (
    ScheduleOsSubstituteDividendComputeResult,
)
from ibkr_fa.compute.section_91 import (
    EXACT_PORTAL,
    PREFLIGHT_PORTAL,
    Section91Check,
    section_91_error,
)
from ibkr_fa.compute.wht_reversal_pairing import WhtCrossFyReversalReport
from ibkr_fa.config import ScheduleOsIndianAdrRoutingMode
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.invariants import require
from ibkr_fa.models._identifier_checks import is_iso_alpha2
from ibkr_fa.models.schedule_cg_row import exact_amount
from ibkr_fa.models.schedule_fsi_row import HeadOfIncome, ScheduleFsiRow
from ibkr_fa.models.withholding_tax import WithholdingTax
from ibkr_fa.reference.country_codes import lookup_country

# Locates a failed addition of one country's capital-gains nets (#1166).
_CG_NETS: Final[str] = "Schedule FSI capital gains, fields stcg_net and ltcg_net"
# Schedule OS files Indian dividends in 1a(i); their share of it never reaches FSI.
_INDIA: Final[str] = "IN"


@dataclass(frozen=True, slots=True)
class ScheduleFsiComputeResult:
    """Result of the Schedule FSI compute stage.

    Each Schedule FSI writer (the filing CSV, review CSV and XLSX
    formats bound in :mod:`ibkr_fa.output`) consumes a single
    :class:`ScheduleFsiComputeResult` value and writes the whole
    schedule from the :class:`~ibkr_fa.models.ScheduleFsiRow`
    values in :attr:`rows` in one ``write`` call — mirroring the
    Schedule OS / CG writers' single-call contract.

    Attributes:
        rows: Materialised per-(country, head-of-income) aggregate
            rows, sorted by ``(cbdt_country_code, head_of_income)``
            for deterministic output. At most one row per
            ``(cbdt_country_code, head_of_income)`` pair (verified
            at construction). Empty list is valid (e.g., a
            statement with no foreign income — return-of-capital
            only, or purely Indian-source dividends).

    Invariants enforced in :meth:`__post_init__`:

    * No duplicate ``(cbdt_country_code, head_of_income)`` pairs
      across :attr:`rows`. A duplicate indicates an aggregation
      bug (the compute function should have summed both
      contributions into one row).
    * Rows are sorted by ``(cbdt_country_code, head_of_income)``
      ascending. The sort key is the CBDT code lexicographically
      (NOT numerically — CBDT codes are vendored as strings) and
      then the StrEnum value (alphabetical: ``"Capital Gains"``
      before ``"Other Sources"``).

    Note on lexicographic vs numeric sort: lexicographic sort
    produces a portal-row ordering that may surprise a human
    auditor (e.g., Antigua-and-Barbuda code ``"1268"`` sorts
    BEFORE the United States code ``"2"`` because ``'1' < '2'``
    character-wise). A numeric sort (``int(code)``) would
    produce the more human-readable ordering but requires
    every CBDT code to be a strict integer — vendoring as
    strings preserves any future leading-zero CBDT might
    introduce. The portal upload accepts rows in any order
    (per CBDT ITR-2 Instructions; rows are keyed on country
    code, not row index), so the lexicographic choice is
    semantically safe; pin the human-readability tradeoff in
    :func:`~ibkr_fa.compute.schedule_fsi.compute_schedule_fsi_from_dividends`'s
    regression test
    ``test_multi_country_sort_order_is_cbdt_code_lexicographic``.
    """

    rows: list[ScheduleFsiRow]
    cross_fy_reversal_report: WhtCrossFyReversalReport = field(
        default_factory=WhtCrossFyReversalReport
    )

    def __post_init__(self) -> None:
        seen: set[tuple[str, HeadOfIncome]] = set()
        for row in self.rows:
            key = (row.cbdt_country_code, row.head_of_income)
            require(
                key not in seen,
                what=(
                    "ScheduleFsiComputeResult.rows must contain at most "
                    "one row per (cbdt_country_code, head_of_income) pair; "
                    "duplicate indicates an aggregation bug in compute"
                ),
                cbdt_country_code=row.cbdt_country_code,
                head_of_income=row.head_of_income,
            )
            seen.add(key)
        expected_order = sorted(
            self.rows, key=lambda r: (r.cbdt_country_code, r.head_of_income.value)
        )
        require(
            self.rows == expected_order,
            what=(
                "ScheduleFsiComputeResult.rows must be sorted by "
                "(cbdt_country_code, head_of_income) ascending for "
                "deterministic output"
            ),
        )


def require_fsi_relief_matches_portal(
    rows: Iterable[ScheduleFsiRow], form67_result: Form67ComputeResult | None = None
) -> None:
    """Refuse a row whose relief the portal's read-only FSI cell would overwrite.

    The portal fills relief as ``min(tax paid, tax payable)`` from the
    whole-rupee cells, so a row's :attr:`~ScheduleFsiRow.filed` values decide
    (#900). Rows with a blank relief or tax payable are not compared. Every
    operation that writes Schedule FSI or TR calls this before its first
    write, and every FSI and TR writer calls it again.

    The message names the row by its country, which every FSI row looks up
    in the CBDT country table, and its head of income, but not the amounts,
    which come from the statements (#1047).

    Given the run's ``form67_result``, a row whose relief is one of its
    section 91 credits is checked first with the former formula's credit,
    refusing with the fixed preflight message, and last with the exact
    credit, refusing with the fixed corrected-value message (#1177).
    Within either section 91 phase, several refusals name the first country
    input row. The other rows keep the message above, between the two phases.
    """
    rows = tuple(rows)
    section_91 = _section_91_relief_rows(rows, form67_result)
    for check, index in section_91:
        legacy = dataclasses.replace(rows[index], tax_relief_inr=check.legacy_credit_inr)
        if legacy.filed.portal_overwrites_relief:
            raise section_91_error(check.position, PREFLIGHT_PORTAL)
    routed = {index for _, index in section_91}
    for index, row in enumerate(rows):
        if index in routed:
            continue
        filed = row.filed
        require(
            not filed.portal_overwrites_relief,
            what=(
                "Schedule FSI filing would diverge from the portal's read-only "
                "relief (min of tax paid and Indian tax payable) on the "
                f"{row.iso_alpha2} {row.head_of_income.value} row; resolve the "
                "filing amounts manually before filing"
            ),
            country=row.iso_alpha2,
            head=row.head_of_income.value,
            computed_relief=filed.tax_relief_inr,
            portal_relief=filed.portal_relief_inr,
        )
    for check, index in section_91:
        if rows[index].filed.portal_overwrites_relief:
            raise section_91_error(check.position, EXACT_PORTAL)


def require_fsi_write_ready(
    fsi_rows: Iterable[ScheduleFsiRow], form67_result: Form67ComputeResult
) -> None:
    """Refuse a REFUSED Form 67 row
    (:func:`~ibkr_fa.compute.form67.require_no_refused_form67_rows`, #601),
    then a relief the portal's read-only cell would overwrite
    (:func:`require_fsi_relief_matches_portal`, #900).

    The callers use this before their first schedule write. The Form 67
    check goes first, so a run with both problems refuses
    with the Form 67 check's error in each of them, as ``run-all`` does (#1480).
    """
    require_no_refused_form67_rows(form67_result)
    require_fsi_relief_matches_portal(fsi_rows, form67_result)


def _section_91_relief_rows(
    rows: tuple[ScheduleFsiRow, ...], form67_result: Form67ComputeResult | None
) -> list[tuple[Section91Check, int]]:
    """Each Other Sources row whose relief is a section 91 credit of
    ``form67_result``: its check and the row's index, by country input row.

    Relief enrichment copies the credit onto the country's one Other Sources
    row, whose foreign tax is the entry's, so the Form 67 credit checks also
    cover the row's relief.
    """
    if form67_result is None:
        return []
    checks = form67_result._section_91_checks
    credit_by_country = {
        (entry.cbdt_country_code, entry.iso_alpha2): (
            checks[entry.iso_alpha2],
            entry.total_credit_inr,
        )
        for entry in form67_result.entries
        if entry.iso_alpha2 in checks
    }
    found: list[tuple[Section91Check, int]] = []
    for index, row in enumerate(rows):
        credit = credit_by_country.get((row.cbdt_country_code, row.iso_alpha2))
        if (
            credit is not None
            and row.head_of_income is HeadOfIncome.OTHER_SOURCES
            and row.tax_relief_inr is not None
            and row.tax_relief_inr == credit[1]
        ):
            found.append((credit[0], index))
    return sorted(found, key=lambda pair: pair[0].position)


def compute_schedule_fsi_from_dividends(
    os_result: ScheduleOsComputeResult,
) -> ScheduleFsiComputeResult:
    """Aggregate foreign-source dividends into per-country FSI rows.

    Consumes the foreign-source dividend lines produced by
    :func:`~ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`
    (specifically
    :attr:`~ibkr_fa.compute.schedule_os.ScheduleOsComputeResult.lines`,
    which carries the in-FY non-Indian-source ordinary dividends
    after Rule 115(1), Explanation (2)(e) FX conversion) and emits one
    :class:`~ibkr_fa.models.ScheduleFsiRow` per source country
    under :attr:`~ibkr_fa.models.HeadOfIncome.OTHER_SOURCES`.

    Domestic Indian-source dividends (Schedule OS row 1a(i),
    :attr:`~ibkr_fa.compute.schedule_os.ScheduleOsComputeResult.domestic_lines`)
    are **NOT** included — Schedule FSI is by definition
    "income from outside India", so Indian-source rows are out
    of scope.

    Only foreign dividends contribute here. The
    :attr:`~ibkr_fa.models.ScheduleFsiRow.foreign_tax_inr` field
    is ``Decimal("0")`` on every row: withholding tax arrives
    separately, as the per-country fragments of
    :func:`compute_schedule_fsi_from_withholding_tax`, which
    :func:`_merge_results` layers onto the merged income rows.

    Args:
        os_result: The Schedule OS compute result whose
            ``lines`` attribute carries the foreign-source
            dividend pipeline output. The FSI compute consumes
            already-FX-converted INR amounts (single source of
            truth — does not re-FX-convert from the source
            USD).

    Returns:
        A :class:`ScheduleFsiComputeResult` with one
        :class:`~ibkr_fa.models.ScheduleFsiRow` per source country
        that had at least one foreign-source dividend line, all
        under :attr:`~ibkr_fa.models.HeadOfIncome.OTHER_SOURCES`.
        Empty :attr:`~ScheduleFsiComputeResult.rows` if
        ``os_result.lines`` is empty (e.g., return-of-capital-
        only statement, or purely Indian-source dividends).

    Raises:
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If any dividend line's
            :attr:`~ibkr_fa.compute.schedule_os.ScheduleOsDividendLine.issuer_country_iso2`
            is not in the vendored
            :data:`~ibkr_fa.reference.country_codes._COUNTRY_CSV`
            mapping. Defense-in-depth — the OS compute layer
            already validates ISO alpha-2 shape, but the FSI
            layer additionally requires the CBDT code is
            vendored so the portal output cell can be filled.
            Fail fast rather than silently dropping a country
            with no mapping.
    """
    # Group by ISO alpha-2; lookup CBDT code + portal name once
    # per country (load_country_codes is cached so the lookup
    # is effectively free per-line, but per-group is more
    # readable).
    per_country_lines: dict[str, list[ScheduleOsDividendLine]] = {}
    for line in os_result.lines:
        per_country_lines.setdefault(line.issuer_country_iso2, []).append(line)

    rows: list[ScheduleFsiRow] = []
    for iso_alpha2, lines in per_country_lines.items():
        country = lookup_country(iso_alpha2)
        total_income_inr = exact_inr_sum(
            (line.amount_gross_inr for line in lines),
            label="Schedule FSI dividend income by country",
        )
        rows.append(
            ScheduleFsiRow(
                cbdt_country_code=country.cbdt_code,
                country_name_portal=country.country_name_portal,
                iso_alpha2=iso_alpha2,
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr=total_income_inr,
                foreign_tax_inr=Decimal("0"),
                event_count=len(lines),
            )
        )

    # Sort by (cbdt_country_code, head_of_income.value) — same
    # invariant enforced in __post_init__, so sort here for
    # deterministic output regardless of input ordering.
    rows.sort(key=lambda r: (r.cbdt_country_code, r.head_of_income.value))
    return ScheduleFsiComputeResult(rows=rows)


def compute_schedule_fsi_from_capital_gains(
    cg_stcg_result: ScheduleCgStcgComputeResult | None = None,
    cg_ltcg_result: ScheduleCgLtcgComputeResult | None = None,
) -> ScheduleFsiComputeResult:
    """Aggregate foreign-source capital gains into per-country FSI rows.

    Consumes the in-FY, non-Indian-source capital-gains lines
    produced by
    :func:`~ibkr_fa.compute.schedule_cg.compute_schedule_cg_stcg_lines`
    (STCG, holding period ≤ 24 months) and
    :func:`~ibkr_fa.compute.schedule_cg_ltcg.compute_schedule_cg_ltcg_lines`
    (LTCG, holding period > 24 months, new-regime post-2024-07-23
    disposals) and emits at most ONE
    :class:`~ibkr_fa.models.ScheduleFsiRow` per source country
    under :attr:`~ibkr_fa.models.HeadOfIncome.CAPITAL_GAINS`.

    STCG and LTCG share the SINGLE Capital Gains bucket on
    Schedule FSI — the portal form does NOT distinguish STCG vs
    LTCG on the FSI page (the per-tax-rate split happens on
    Schedule SI and Schedule CG itself, NOT on FSI).

    Section 70 set-off asymmetry per country:

    Section 70 of the Income-tax Act, 1961 restricts capital-gains
    loss set-off ASYMMETRICALLY:

    * **Section 70(2)** — short-term capital loss (STCL) can be
      set off against income from "any other capital asset", i.e.
      either short-term OR long-term capital gains.
    * **Section 70(3)** — long-term capital loss (LTCL) can be set
      off only against income from "any other capital asset not
      being a short-term capital asset", i.e. LTCG only — NEVER
      against STCG.

    The aggregator applies this asymmetry per-country before
    summing into a single Capital Gains row:

    * Both STCG_net > 0 and LTCG_net > 0 → row income = sum.
    * STCG_net < 0 and LTCG_net ≥ 0 (Section 70(2)) → row income
      = max(0, STCG_net + LTCG_net); both buckets' events count.
    * STCG_net ≥ 0 and LTCG_net < 0 (Section 70(3)) → row income
      = STCG_net only; LTCG events are EXCLUDED (their loss is
      carried forward via Section 74 on Schedule CFL).
    * Both STCG_net ≤ 0 and LTCG_net ≤ 0 → no row.

    Cross-country set-off is NOT applied — CBDT FTC contract:

    Section 70 set-off is conceptually computed on the FY-aggregate
    STCG/LTCG (not per-country). However, the per-country FTC
    formula in Rule 128(5)(i) of the Income-tax Rules, 1962 caps
    the credit for each (country, source) pair at the LOWER of
    the foreign tax paid and the Indian tax payable on that
    same per-country income; CBDT Notification 9/2017
    governing Form 67 reinforces this by requiring per-country
    statement of income and foreign tax under Rule 128(8)(i).
    The CBDT-canonical interpretation is therefore that
    Schedule FSI reports per-country GROSS capital-gains
    income WITHOUT cross-country set-off — losses from one
    country cannot be used to reduce the FTC-eligible income
    of another. The aggregator confines Section 70
    (within-head, across-source) to be intra-country only:

    * a US STCL is NOT permitted to offset a UK LTCG on
      Schedule FSI (even though Section 70(2) would allow
      it in the aggregate Schedule CG computation); the
      US row is dropped (loss carried forward via Section
      74) and the UK row is reported gross.
    * within a single country, however, intra-head Section
      70(2)/70(3) IS applied because STCG and LTCG share
      the SINGLE "Capital Gains" head on Schedule FSI (the
      portal form does not split STCG/LTCG on the FSI
      page; the per-rate split happens on Schedule SI and
      Schedule CG).

    Authority: Rule 128(5)(i) (per-country/per-source FTC
    cap), Rule 128(8)(i) (Form 67 per-country statement
    requirement), CBDT Notification 9/2017 (Form 67 filing
    procedure), ITR-2 instructions for Schedule FSI
    (per-country income reporting).

    Net-loss / zero-net handling per country:

    The ``income_inr > 0`` invariant on
    :class:`~ibkr_fa.models.ScheduleFsiRow` means a country whose
    post-set-off Capital Gains income is ≤ 0 produces NO FSI
    row — there is nothing positive to report as "income from
    outside India". The carried-forward losses are still
    surfaced upstream via Schedule CG itself and remain
    available on Schedule CFL.

    Args:
        cg_stcg_result: The Schedule CG STCG compute result whose
            ``stcg_lines`` attribute carries the in-FY non-Indian-
            source short-term lines. ``None`` is treated as "no
            STCG to aggregate".
        cg_ltcg_result: The Schedule CG LTCG compute result whose
            ``ltcg_lines`` attribute carries the in-FY non-Indian-
            source long-term post-2024-07-23 lines. ``None`` is
            treated as "no LTCG to aggregate".

    Returns:
        A :class:`ScheduleFsiComputeResult` with at most one
        :class:`~ibkr_fa.models.ScheduleFsiRow` per source
        country, all under
        :attr:`~ibkr_fa.models.HeadOfIncome.CAPITAL_GAINS`.
        Empty ``rows`` if both inputs are ``None`` / empty, or
        if every country's post-set-off net CG is ≤ 0.

        ``event_count`` on each emitted row reflects every
        underlying closure event that contributed to the
        computation — including LOSS events when the country
        is net-positive overall (matching the audit-
        reconciliation purpose of
        :attr:`~ibkr_fa.models.ScheduleFsiRow.event_count`).
        Exception: under the Section 70(3) branch (positive
        STCG + negative LTCG net), only the STCG events are
        counted because the LTCG events are carried forward
        and do NOT contribute to this year's FSI for the
        country.

    Raises:
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If any CG line's ``issuer_country_iso2`` is not in
            the vendored country-code mapping. Defense-in-depth
            (the CG compute layer validates 2-letter alpha-2 shape;
            this layer additionally requires the CBDT code is
            vendored). Raised at INGESTION for every appearing
            country — not gated by the per-country net-loss
            filter — so unmapped codes surface immediately even
            when the underlying P&L is a loss.
        :class:`~ibkr_fa.exceptions.InvariantError`: If a country's
            STCG or LTCG net, or any addition of the two, can't be
            added exactly (#1166); these sums never round. Checked
            before the net-loss filter, so a dropped row refuses too.

    Where a country's line carries exact amounts (#1241), its nets also
    add the lines' exact gains, those exact nets decide the set-off, and
    the row's income is
    :func:`~ibkr_fa.compute.exact_inr.exact_decimal` of the exact income,
    so it files as the exact HALF_UP.
    """
    # Each gain keeps its 1-based position in its schedule's lines, which a
    # failed net names (#1166), and its exact gain, if any (#1241).
    per_country_stcg: dict[str, list[tuple[int, Decimal, Fraction | None]]] = {}
    per_country_ltcg: dict[str, list[tuple[int, Decimal, Fraction | None]]] = {}
    if cg_stcg_result is not None:
        for position, stcg_line in enumerate(cg_stcg_result.stcg_lines, start=1):
            per_country_stcg.setdefault(stcg_line.issuer_country_iso2, []).append(
                (
                    position,
                    stcg_line.realized_gain_inr,
                    exact_amount(stcg_line, "realized_gain_inr"),
                )
            )
    if cg_ltcg_result is not None:
        for position, ltcg_line in enumerate(cg_ltcg_result.ltcg_lines, start=1):
            per_country_ltcg.setdefault(ltcg_line.issuer_country_iso2, []).append(
                (
                    position,
                    ltcg_line.realized_gain_inr,
                    exact_amount(ltcg_line, "realized_gain_inr"),
                )
            )

    all_countries = set(per_country_stcg.keys()) | set(per_country_ltcg.keys())

    # Fail-fast country-code validation: invoke lookup_country for
    # every appearing ISO alpha-2 BEFORE the net-loss filter, so an
    # unmapped country raises CountryCodeNotFoundError at ingestion
    # regardless of its P&L sign. Without this, a loss-only country
    # with an unvendored ISO code would silently pass today and
    # crash later when the same data turns net-positive (T4 r1 F1).
    for iso_alpha2 in all_countries:
        lookup_country(iso_alpha2)

    rows: list[ScheduleFsiRow] = []
    # Sorted, so the same input always refuses at the same country's net.
    for iso_alpha2 in sorted(all_countries):
        stcg_gains = per_country_stcg.get(iso_alpha2, [])
        ltcg_gains = per_country_ltcg.get(iso_alpha2, [])
        exact = any(rational is not None for *_, rational in (*stcg_gains, *ltcg_gains))
        stcg_net, stcg_exact = checked_exact_sum(
            stcg_gains,
            at=lambda position: (
                f"Schedule FSI capital gains STCG net, input row {position}, "
                "column realized_gain_inr"
            ),
            exact=exact,
        )
        ltcg_net, ltcg_exact = checked_exact_sum(
            ltcg_gains,
            at=lambda position: (
                f"Schedule FSI capital gains LTCG net, input row {position}, "
                "column realized_gain_inr"
            ),
            exact=exact,
        )

        # Section 70(2) / 70(3) per-country asymmetric set-off.
        # Branch table (S = stcg_net, L = ltcg_net):
        if stcg_net >= 0 and ltcg_net < 0:
            # Section 70(3): LTCL CANNOT offset STCG. The LTCG
            # loss is carried forward via Section 74 (Schedule
            # CFL) and does NOT appear on this year's FSI for
            # this country. Only STCG events contribute to the
            # FSI row's income figure; the LTCG events are
            # represented separately on Schedule CG and CFL,
            # not on FSI.
            fsi_income = stcg_net
            event_count = len(stcg_gains)
        else:
            # Every other case is a straight sum, and both buckets'
            # events count toward the audit row because both
            # contributed to the computation:
            # - Both non-negative.
            # - Section 70(2): STCL offsets LTCG. Sum may go either
            #   way; if it goes non-positive the row is dropped
            #   below.
            # - Both losses: nothing positive to report. The sum is
            #   still added exactly (#1166).
            fsi_income = checked_addition(stcg_net, ltcg_net, at=_CG_NETS)
            event_count = len(stcg_gains) + len(ltcg_gains)

        if stcg_exact is not None and ltcg_exact is not None:
            # The same set-off on the exact nets (#1241), which decide it.
            stcg_only = stcg_exact >= 0 and ltcg_exact < 0
            fsi_income = exact_decimal(stcg_exact if stcg_only else stcg_exact + ltcg_exact)
            event_count = len(stcg_gains) + (0 if stcg_only else len(ltcg_gains))

        if fsi_income <= 0:
            continue
        country = lookup_country(iso_alpha2)
        rows.append(
            ScheduleFsiRow(
                cbdt_country_code=country.cbdt_code,
                country_name_portal=country.country_name_portal,
                iso_alpha2=iso_alpha2,
                head_of_income=HeadOfIncome.CAPITAL_GAINS,
                income_inr=fsi_income,
                foreign_tax_inr=Decimal("0"),
                event_count=event_count,
            )
        )

    rows.sort(key=lambda r: (r.cbdt_country_code, r.head_of_income.value))
    return ScheduleFsiComputeResult(rows=rows)


class InterestPayerCountryError(IbkrFaError):
    """Interest reached Schedule FSI without a supported payer country (#698)."""


def compute_schedule_fsi_from_business_income(
    income_inr: Decimal,
    *,
    source_country_iso2: str,
    event_count: int,
) -> ScheduleFsiComputeResult:
    """Materialise one positive foreign business-income contribution."""

    if income_inr <= 0:
        return ScheduleFsiComputeResult(rows=[])
    normalised_iso = source_country_iso2.upper()
    country = lookup_country(normalised_iso)
    return ScheduleFsiComputeResult(
        rows=[
            ScheduleFsiRow(
                cbdt_country_code=country.cbdt_code,
                country_name_portal=country.country_name_portal,
                iso_alpha2=normalised_iso,
                head_of_income=HeadOfIncome.BUSINESS_PROFESSION,
                income_inr=income_inr,
                foreign_tax_inr=Decimal(),
                event_count=event_count,
            )
        ]
    )


def compute_schedule_fsi_from_interest(
    interest_result: ScheduleOsInterestComputeResult | None = None,
    *,
    broker_domicile_iso2: str | None,
) -> ScheduleFsiComputeResult:
    """Aggregate broker credit-interest into a single per-broker FSI row.

    Consumes the in-FY, Rule-115(1)-Explanation-(2)(c) FX-converted
    interest credits produced by
    :func:`~ibkr_fa.compute.schedule_os_interest.compute_schedule_os_interest_lines`
    and emits at most ONE :class:`~ibkr_fa.models.ScheduleFsiRow`
    attributed to the broker's domicile country under
    :attr:`~ibkr_fa.models.HeadOfIncome.OTHER_SOURCES`.

    Why broker-domicile attribution (not per-issuer):

    IBKR cash-balance credit interest is paid by the broker on idle
    USD cash above the minimum-balance threshold — there is no
    underlying security and therefore no per-issuer country.
    Schedule FSI's per-country split must therefore attribute the
    interest to the broker's domicile country: US for IBKR LLC (the
    primary US entity used by Indian retail investors via the
    ``IBKR.com/in`` / ``IBKR LLC`` account), IE for IBKR Ireland,
    HU for IBKR Hungary, etc. The caller states that country in
    ``broker_domicile_iso2``; there is no default (#698).
    See :class:`~ibkr_fa.models.InterestCredit` class docstring for
    the underlying tax-law reasoning.

    Single-row construction:

    Unlike dividends (per-issuer) and CG (per-issuer), all credit-
    interest in the FY is attributed to a SINGLE source country
    (the broker's domicile). The aggregator therefore emits at most
    one row whose ``income_inr`` is the sum of all credits and
    ``event_count`` is the count of credits. The row is dropped
    only if the input ``interest_result`` is ``None`` or has zero
    lines (which yields no FSI row to merge into).

    Args:
        interest_result: The Schedule OS interest compute result
            whose ``lines`` attribute carries the in-FY credit-
            interest events with their Rule 115(1), Explanation (2)(c)
            FY-end FX conversion already applied. ``None`` is
            treated as "no interest to aggregate".
        broker_domicile_iso2: The ISO 3166-1 alpha-2 country code
            of the broker's domicile, required with no default.
            Normalised to uppercase before lookup so that a
            caller can pass ``"us"`` / ``"Us"`` without
            tripping the case-sensitive country-code mapping.
            Must be in the vendored country-code mapping, and is
            checked even when there is no interest. ``None`` means
            the source stated no payer; it is allowed only when
            ``interest_result`` is ``None`` or has no lines.

    Returns:
        A :class:`ScheduleFsiComputeResult` with at most one
        :class:`~ibkr_fa.models.ScheduleFsiRow` under
        :attr:`~ibkr_fa.models.HeadOfIncome.OTHER_SOURCES` for the
        broker's domicile country. The emitted row's
        ``iso_alpha2`` is the normalised (uppercase) form of
        ``broker_domicile_iso2``. Empty ``rows`` if
        ``interest_result`` is ``None`` or has no lines.

    Raises:
        InterestPayerCountryError: If ``broker_domicile_iso2`` is
            ``None`` while there are interest lines, or is not a
            string.
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If ``broker_domicile_iso2`` (after uppercase
            normalisation) is not in the vendored country-code
            mapping.
    """
    lines = () if interest_result is None else interest_result.lines
    if broker_domicile_iso2 is None:
        if lines:
            raise InterestPayerCountryError(
                "interest income has no stated payer country; the source must state it"
            )
        return ScheduleFsiComputeResult(rows=[])
    if not isinstance(broker_domicile_iso2, str):
        raise InterestPayerCountryError(
            "interest payer country must be an ISO 3166-1 alpha-2 string, "
            f"got {broker_domicile_iso2!r}"
        )

    # Normalise the ISO before any further processing. This function's
    # one caller in the package, compute_schedule_fsi, gets the ISO from
    # compute_income_schedules (compute/income_engine.py), which has
    # already refused every payer country except "US"
    # (_require_us_interest_payer), so on that path .upper() changes
    # nothing. A direct call with "us" still finds its country rather
    # than raising a spurious CountryCodeNotFoundError.
    # The other aggregators (dividends, CG) receive iso_alpha2 from
    # typed line objects whose own invariants enforce uppercase, so
    # they don't need a similar normalisation step.
    normalised_iso = broker_domicile_iso2.upper()

    # Fail-fast country validation, before the no-interest return: an
    # unmapped broker-domicile ISO must surface at this layer whether or
    # not there is interest (same defensive-validation pattern as the
    # CG aggregator).
    country = lookup_country(normalised_iso)
    if not lines:
        return ScheduleFsiComputeResult(rows=[])

    total_income_inr = exact_inr_sum(
        (line.amount_gross_inr for line in lines),
        label="Schedule FSI interest income",
    )
    # The interest compute layer guarantees each line.amount_gross_inr
    # is strictly positive (positive credit * positive FX rate); the
    # sum of strictly positive Decimals is therefore also strictly
    # positive whenever there is at least one line. No drop-if-zero
    # branch is needed.
    rows = [
        ScheduleFsiRow(
            cbdt_country_code=country.cbdt_code,
            country_name_portal=country.country_name_portal,
            iso_alpha2=normalised_iso,
            head_of_income=HeadOfIncome.OTHER_SOURCES,
            income_inr=total_income_inr,
            foreign_tax_inr=Decimal("0"),
            event_count=len(lines),
        )
    ]
    return ScheduleFsiComputeResult(rows=rows)


def compute_schedule_fsi_from_substitute_dividends(
    result: ScheduleOsSubstituteDividendComputeResult | None = None,
) -> ScheduleFsiComputeResult:
    """Aggregate substitute dividends by source country under Other Sources."""

    if result is None or not result.lines:
        return ScheduleFsiComputeResult(rows=[])
    grouped: dict[str, list[Decimal]] = {}
    for line in result.lines:
        grouped.setdefault(line.source_country_iso2, []).append(line.amount_gross_inr)
    return _signed_other_sources_result(
        grouped, label="Schedule FSI substitute dividend income by country"
    )


def compute_schedule_fsi_from_other_income(
    result: ScheduleOsOtherIncomeComputeResult | None = None,
) -> ScheduleFsiComputeResult:
    """Aggregate reviewed residual other income by source country."""

    if result is None or not result.lines:
        return ScheduleFsiComputeResult(rows=[])
    require(
        all(line.source_country_iso2 is not None for line in result.lines),
        what="Schedule FSI other-income lines require a reviewed source country",
    )
    grouped: dict[str, list[Decimal]] = {}
    for line in result.lines:
        assert line.source_country_iso2 is not None
        grouped.setdefault(line.source_country_iso2, []).append(line.amount_gross_inr)
    return _signed_other_sources_result(grouped, label="Schedule FSI other income by country")


def _signed_other_sources_result(
    grouped: dict[str, list[Decimal]], *, label: str
) -> ScheduleFsiComputeResult:
    """One Other Sources row per country in ``grouped``, with the exact signed
    sum of its amounts (refused as ``label``) and one event per amount."""
    rows: list[ScheduleFsiRow] = []
    for iso_alpha2, amounts in grouped.items():
        country = lookup_country(iso_alpha2)
        rows.append(
            ScheduleFsiRow(
                cbdt_country_code=country.cbdt_code,
                country_name_portal=country.country_name_portal,
                iso_alpha2=iso_alpha2,
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                income_inr=exact_signed_inr_sum(amounts, label=label),
                foreign_tax_inr=Decimal(),
                event_count=len(amounts),
            )
        )
    rows.sort(key=lambda row: (row.cbdt_country_code, row.head_of_income.value))
    return ScheduleFsiComputeResult(rows=rows)


@dataclass(frozen=True, slots=True)
class ScheduleFsiWhtContribution:
    """One per-country foreign-tax fragment from the WHT aggregator.

    A "tax-only" contribution that must be layered onto a
    matching :class:`~ibkr_fa.models.ScheduleFsiRow` (same
    ``(cbdt_country_code, head_of_income)`` key) at merge time.
    It is NOT a :class:`ScheduleFsiRow` because the latter's
    ``income_inr > 0`` invariant would reject a tax-only row;
    :func:`_merge_results` takes these fragments through its
    separate ``wht_contributions`` argument instead.

    Per-country *income* is contributed by the separate income
    aggregators; the WHT aggregator only contributes the tax column.

    Invariants:

    * ``cbdt_country_code`` is non-empty (vendored from
      :func:`~ibkr_fa.reference.country_codes.lookup_country`).
    * ``country_name_portal`` is non-empty (vendored).
    * ``iso_alpha2`` is the 2-letter uppercase ISO 3166-1 code.
    * ``foreign_tax_inr > 0``. Per-country contributions where
      the FY reversals exceed FY tax-paid (i.e. NET refund) are
      dropped by the aggregator before construction — Schedule
      FSI has no representation for a negative tax credit and
      the user wouldn't claim FTC anyway.
    * ``event_count >= 1`` — the count of net contributing rows
      (negative-paid + positive-reversal). No writer or merge step
      reads it; in particular it is not added to the merged FSI
      row's own ``event_count`` (that field counts income events).

    Attributes:
        cbdt_country_code: CBDT country code (the "income from"
            country) — same field semantics as
            :attr:`ScheduleFsiRow.cbdt_country_code`.
        country_name_portal: ITR-2 portal country-name spelling.
        iso_alpha2: ISO 3166-1 alpha-2 code (uppercase).
        head_of_income: Which FSI head this tax attaches to.
            The WHT aggregator emits only
            :attr:`~ibkr_fa.models.HeadOfIncome.OTHER_SOURCES`
            (Dividends-section WHT).
        foreign_tax_inr: Net foreign tax paid in INR after
            within-country reversal netting, converted via the
            configured :class:`~ibkr_fa.fx.policy.FxPolicy`
            (Rule 128(5)(ii) for FTC purposes — per-event
            preceding-month-end TTBR on the
            :attr:`~ibkr_fa.models.WithholdingTax.payment_date`).
            Always strictly positive.
        event_count: Number of in-FY WHT rows (paid + reversal)
            attributed to this country/head.
    """

    cbdt_country_code: str
    country_name_portal: str
    iso_alpha2: str
    head_of_income: HeadOfIncome
    foreign_tax_inr: Decimal
    event_count: int

    def __post_init__(self) -> None:
        """Fail-fast structural invariants. See class docstring."""
        require(
            self.cbdt_country_code != "",
            what="ScheduleFsiWhtContribution.cbdt_country_code must be non-empty",
        )
        require(
            self.country_name_portal != "",
            what="ScheduleFsiWhtContribution.country_name_portal must be non-empty",
        )
        require(
            is_iso_alpha2(self.iso_alpha2),
            what=(
                "ScheduleFsiWhtContribution.iso_alpha2 must be a 2-letter uppercase ISO 3166-1 code"
            ),
            iso_alpha2=self.iso_alpha2,
        )
        require(
            self.foreign_tax_inr > 0,
            what=(
                "ScheduleFsiWhtContribution.foreign_tax_inr must be > 0 "
                "(net-refund per-country contributions are dropped before "
                "construction)"
            ),
            foreign_tax_inr=str(self.foreign_tax_inr),
        )
        require(
            self.event_count >= 1,
            what="ScheduleFsiWhtContribution.event_count must be >= 1",
            event_count=self.event_count,
        )


@dataclass(frozen=True, slots=True)
class ScheduleFsiWhtAggregateResult:
    """Return type of :func:`compute_schedule_fsi_from_withholding_tax`.

    Wraps the per-country WHT contributions list together with a
    cross-FY-reversal diagnostic report (issue #224), so callers
    can surface user advisories without re-running the heuristic
    pairing pass.

    Attributes:
        contributions: One :class:`ScheduleFsiWhtContribution` per
            country with net positive in-FY WHT (after the cross-FY
            reversal exclusion described below). Sorted by
            ``(cbdt_country_code, head_of_income.value)`` for
            deterministic output.
        cross_fy_reversal_report: Diagnostic counts from the cross-
            FY WHT-reversal pairing pass. See
            :class:`~ibkr_fa.compute.wht_reversal_pairing.WhtCrossFyReversalReport`
            for field semantics. Surfaced by the FSI pipeline into
            :class:`~ibkr_fa.pipeline_fsi_models.ScheduleFsiRunReport`
            for CLI advisories.
    """

    contributions: list[ScheduleFsiWhtContribution]
    cross_fy_reversal_report: WhtCrossFyReversalReport = field(
        default_factory=WhtCrossFyReversalReport
    )


def compute_schedule_fsi_from_withholding_tax(
    withholding_tax: Iterable[WithholdingTax],
    *,
    fx_policy: FxPolicy,
    financial_year_start: date,
    financial_year_end: date,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode = ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
) -> ScheduleFsiWhtAggregateResult:
    """Aggregate IBKR Dividends-section WHT per source country.

    Consumes the parsed ``WithholdingTax`` list, filters to the
    FY by :attr:`~ibkr_fa.models.WithholdingTax.payment_date`,
    converts each row's signed USD amount to INR via the
    configured Rule 128(5)(ii) :class:`~ibkr_fa.fx.policy.FxPolicy`,
    nets within-country reversals against tax-paid amounts, and
    emits one :class:`ScheduleFsiWhtContribution` per country
    whose net is strictly positive (i.e. net tax paid > net
    refunds).

    Sign convention (CRITICAL — see
    :class:`~ibkr_fa.models.WithholdingTax` docstring):

    * IBKR emits TAX PAID as a NEGATIVE amount (money leaving
      the cash balance, mirroring the positive Dividends-section
      row of which it's a reduction).
    * REVERSALS (refunds of previously-withheld tax — e.g.
      treaty-rate reclassification after the fact) appear as
      POSITIVE amounts in the same section.
    * The compute layer must NET reversals against tax-paid
      values per country before emitting. This function takes
      the absolute value of the negative sum:

      ::

          per_country_sum_signed = sum(line.amount_inr for line in country_lines)
          # ^ negative magnitude is the net tax paid; positive sum means
          #   reversals exceeded tax paid (net refund) -> drop the country.
          foreign_tax_inr = -per_country_sum_signed
          if foreign_tax_inr <= 0: drop.

    Per-country attribution:

    The WHT is attributed to the SOURCE country (the
    withholding jurisdiction), which is the depositary country
    embedded in the WHT row's ISIN prefix:
    ``withholding_tax.isin[:2]``. This mirrors the dividend
    aggregator's default attribution.

    ADR-routing parity with Schedule OS (issue #225 fix): when
    ``adr_routing_mode`` is
    :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`,
    Schedule OS reroutes Indian-underlying ADR dividends (ISINs
    in :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
    to :class:`~ibkr_fa.compute.schedule_os.ScheduleOsDomesticDividendLine`
    (issuer_country_iso2='IN') — they're absent from the FSI
    dividend lines too. To preserve scope symmetry with the
    income side, this aggregator SKIPS WHT rows whose ISIN is in
    the mapping so they cannot orphan the depositary country's
    FSI row or get silently absorbed into the depositary's bucket
    alongside unrelated same-country foreign WHT. Default
    DEPOSITARY mode preserves the legacy
    depositary-key-with-no-skipping behavior. Added in #225.

    FX policy:

    Each row's FX is computed on its individual
    :attr:`~ibkr_fa.models.WithholdingTax.payment_date` via the
    supplied :class:`~ibkr_fa.fx.policy.FxPolicy`. The expected
    policy is :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`
    (preceding-calendar-month-end SBI TTBR per Rule 128(5)(ii)),
    but any FxPolicy with the same ``currency`` is accepted (so
    test code can pass a deterministic stub without invoking
    the SBI archive). Reversals use the FX on their own
    ``payment_date``, NOT the FX of the original tax-paid row
    they cancel — Rule 128(5)(ii) is per-event and the reversal
    is its own event for FX-conversion purposes.

    Out of scope for v1 (deferred):

    * WHT rows whose ``currency != "USD"``. v1 raises
      :class:`~ibkr_fa.invariants.InvariantError` for any non-USD
      WHT row to surface multi-currency holdings explicitly
      (issue #52 will add the multi-currency story across the
      whole compute stack, including a per-currency FX policy
      registry).
    * WHT from sister sections (Interest section's optional
      withholding column at #139, Flex Query's Cash Transactions
      section's inline WHT at #12) — the caller is responsible
      for filtering to Dividends-section WHT only.

    Args:
        withholding_tax: Iterable of parsed
            :class:`~ibkr_fa.models.WithholdingTax` instances.
            All-or-nothing FY filtering happens here (lines
            outside ``[financial_year_start, financial_year_end]``
            are silently skipped).
        fx_policy: An FxPolicy whose ``currency == "USD"``. Used
            to convert each in-FY row's USD amount to INR on the
            row's own ``payment_date``.
        financial_year_start: Inclusive lower bound for the
            ``payment_date`` filter. Typically ``date(YYYY, 4, 1)``.
        financial_year_end: Inclusive upper bound. Typically
            ``date(YYYY+1, 3, 31)``.
        adr_routing_mode: Schedule OS's Indian-ADR routing mode
            (default
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.DEPOSITARY`).
            When
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`,
            Indian-underlying ADR WHT rows (ISINs in
            :data:`~ibkr_fa.reference.indian_adr_mapping.INDIAN_ADR_MAPPING`)
            are silently skipped so the WHT scope mirrors the
            dividend-side rerouting in
            :func:`~ibkr_fa.compute.schedule_os.compute_schedule_os_dividend_lines`.
            See the "Per-country attribution" section above and
            issue #225.

    Returns:
        A :class:`ScheduleFsiWhtAggregateResult` whose
        :attr:`~ScheduleFsiWhtAggregateResult.contributions` list
        carries one :class:`ScheduleFsiWhtContribution` per source
        country with a strictly-positive net tax-paid in INR
        (sorted by ``(cbdt_country_code, head_of_income.value)``
        for deterministic output; empty if every country's net is
        <= 0 or there are no in-FY rows), and whose
        :attr:`~ScheduleFsiWhtAggregateResult.cross_fy_reversal_report`
        carries the issue #224 pairing diagnostics (cross-FY
        excluded counts) for downstream RunReport + CLI surfacing.

    Raises:
        :class:`~ibkr_fa.compute.wht_reversal_pairing.UnprovenWhtReversalError`:
            If an in-FY reversal has no equal-and-opposite original
            or candidate originals in more than one FY (#893).
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If any in-FY WHT row's ``isin[:2]`` is not in the
            vendored country-code mapping. Fail-fast: the first
            unknown ISO encountered raises before any contributions
            are returned (same defensive-validation pattern as the
            dividend / CG / interest aggregators).
        :class:`~ibkr_fa.invariants.InvariantError`: If any
            in-FY WHT row's ``currency`` is not the policy's
            currency (v1 strict-USD guard).
    """
    require(
        fx_policy.currency == "USD",
        what=(
            "compute_schedule_fsi_from_withholding_tax requires a USD FX "
            "policy in v1 (multi-currency support deferred to #52)"
        ),
        fx_policy_currency=fx_policy.currency,
    )

    netting = net_foreign_tax_by_country(
        withholding_tax,
        fx_policy=fx_policy,
        financial_year_start=financial_year_start,
        financial_year_end=financial_year_end,
        adr_routing_mode=adr_routing_mode,
    )

    contributions: list[ScheduleFsiWhtContribution] = []
    for country in netting.countries.values():
        # Negate: a NEGATIVE signed total represents tax paid; the
        # positive magnitude is the tax-paid value we want on FSI.
        # A POSITIVE signed total means reversals exceeded paid (net
        # refund) — drop the country.
        # copy_negate, unlike unary minus, never rounds an exact total (#1167).
        foreign_tax_inr = country.signed_inr.copy_negate()
        if foreign_tax_inr <= 0:
            continue
        contributions.append(
            ScheduleFsiWhtContribution(
                cbdt_country_code=country.cbdt_country_code,
                country_name_portal=country.country_name_portal,
                iso_alpha2=country.iso_alpha2,
                head_of_income=HeadOfIncome.OTHER_SOURCES,
                foreign_tax_inr=foreign_tax_inr,
                event_count=country.event_count,
            )
        )

    contributions.sort(key=lambda c: (c.cbdt_country_code, c.head_of_income.value))
    return ScheduleFsiWhtAggregateResult(
        contributions=contributions,
        cross_fy_reversal_report=netting.cross_fy_reversal_report,
    )


def _merge_results(
    *results: ScheduleFsiComputeResult,
    wht_contributions: Iterable[ScheduleFsiWhtContribution] | None = None,
    wht_cross_fy_reversal_report: WhtCrossFyReversalReport | None = None,
) -> ScheduleFsiComputeResult:
    """Merge per-aggregator FSI results into a single result.

    Implements the multi-aggregator merge contract documented on
    :func:`compute_schedule_fsi`: rows sharing the same
    ``(cbdt_country_code, head_of_income)`` key are combined by
    summing ``income_inr``, ``foreign_tax_inr``, and
    ``event_count``; rows with distinct keys are preserved. The two
    INR sums are exact or refuse (:func:`exact_signed_inr_sum`).

    For each key, ``country_name_portal`` and ``iso_alpha2`` are
    asserted to be identical across all contributors (both
    derive deterministically from
    :func:`~ibkr_fa.reference.country_codes.lookup_country` of
    the same CBDT code, so any drift indicates an aggregator
    bug). The merged row re-runs all
    :class:`~ibkr_fa.models.ScheduleFsiRow` invariants at
    construction.

    Withholding-tax fragments (``wht_contributions``):

    The WHT aggregator outputs
    :class:`ScheduleFsiWhtContribution` fragments that cannot
    be packaged as :class:`ScheduleFsiComputeResult` rows
    because their ``income_inr`` is by definition zero (WHT is
    a tax-only contributor) and :class:`ScheduleFsiRow`
    requires ``income_inr > 0``. Instead, the WHT fragments
    are layered onto the merged income rows AFTER income
    aggregation: for each fragment, find the matching
    ``(cbdt_country_code, head_of_income)`` row and add its
    ``foreign_tax_inr`` to the row's ``foreign_tax_inr`` field. That
    addition is exact and never rounds (:func:`checked_addition`,
    #1167); a refusal names the fragment's 1-based position.

    Orphan WHT (a fragment whose ``(cbdt_country_code,
    head_of_income)`` does NOT match any merged income row)
    raises :class:`~ibkr_fa.invariants.InvariantError`. This is
    a strict-v1 contract: Indian FTC requires the foreign
    income to be declared on Schedule FSI before a credit can
    be claimed against it (Rule 128(5)(i) per-country cap), so
    an orphan WHT means the user's data is incomplete and the
    pipeline should refuse rather than silently lose the
    credit. Common causes: (a) IBKR mis-classification of the
    underlying instrument, (b) the income row was filtered out
    by a downstream stage, (c) the user has incomplete data in
    their Activity Statement export. Issue #225's Indian-ADR
    case is now handled structurally upstream — when
    ``adr_routing_mode=UNDERLYING_ISSUER`` is active, mapped
    Indian-ADR WHT rows are skipped by
    :func:`compute_schedule_fsi_from_withholding_tax` so they
    cannot reach this orphan check.

    The fragment's own ``event_count`` is NOT added to the
    merged row's ``event_count``. ``event_count`` on
    :class:`~ibkr_fa.models.ScheduleFsiRow` counts *income*
    events (dividends + CG closures + interest credits), not
    tax events; conflating them would inflate the audit-trail
    count beyond the number of income records the user can
    actually reconcile.
    """
    merged: dict[tuple[str, HeadOfIncome], ScheduleFsiRow] = {}
    for result in results:
        for row in result.rows:
            key = (row.cbdt_country_code, row.head_of_income)
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                continue
            require(
                existing.iso_alpha2 == row.iso_alpha2,
                what=(
                    "ScheduleFsiRow merge: iso_alpha2 must be identical "
                    "across contributors for the same CBDT code (both "
                    "should derive from lookup_country(); drift "
                    "indicates an aggregator bug)"
                ),
                cbdt_country_code=row.cbdt_country_code,
                existing_iso_alpha2=existing.iso_alpha2,
                new_iso_alpha2=row.iso_alpha2,
            )
            require(
                existing.country_name_portal == row.country_name_portal,
                what=(
                    "ScheduleFsiRow merge: country_name_portal must be "
                    "identical across contributors for the same CBDT "
                    "code (both should derive from lookup_country())"
                ),
                cbdt_country_code=row.cbdt_country_code,
                existing_country_name_portal=existing.country_name_portal,
                new_country_name_portal=row.country_name_portal,
            )
            merged[key] = ScheduleFsiRow(
                cbdt_country_code=existing.cbdt_country_code,
                country_name_portal=existing.country_name_portal,
                iso_alpha2=existing.iso_alpha2,
                head_of_income=existing.head_of_income,
                income_inr=exact_signed_inr_sum(
                    (existing.income_inr, row.income_inr),
                    label="Schedule FSI income by country and head",
                ),
                foreign_tax_inr=exact_signed_inr_sum(
                    (existing.foreign_tax_inr, row.foreign_tax_inr),
                    label="Schedule FSI foreign tax by country and head",
                ),
                event_count=existing.event_count + row.event_count,
            )

    # Layer WHT fragments onto the merged income rows. See the
    # docstring's "Withholding-tax fragments" section for the
    # orphan contract.
    if wht_contributions is not None:
        for position, fragment in enumerate(wht_contributions, start=1):
            key = (fragment.cbdt_country_code, fragment.head_of_income)
            existing = merged.get(key)
            require(
                existing is not None,
                what=(
                    "Orphan WHT fragment: foreign tax was withheld for "
                    "this (country, head) but no income aggregator "
                    "produced a matching row. Schedule FSI FTC (Rule "
                    "128(5)(i)) requires the foreign income to be "
                    "declared before a credit can be claimed against "
                    "it. Common causes: (a) IBKR mis-classification of "
                    "the underlying instrument, (b) the income row was "
                    "filtered out by a downstream stage, (c) the user "
                    "has incomplete data in their Activity Statement "
                    "export. (Issue #225 Indian-ADR mis-routing is now "
                    "handled structurally: when "
                    "adr_routing_mode=UNDERLYING_ISSUER is active, "
                    "mapped Indian-ADR WHT rows are skipped by "
                    "compute_schedule_fsi_from_withholding_tax so they "
                    "cannot reach this check.)"
                ),
                cbdt_country_code=fragment.cbdt_country_code,
                head_of_income=fragment.head_of_income.value,
                iso_alpha2=fragment.iso_alpha2,
                foreign_tax_inr=str(fragment.foreign_tax_inr),
            )
            # `require` above raises if existing is None; this assert
            # narrows the type for ty/mypy on the path that continues.
            assert existing is not None
            require(
                existing.iso_alpha2 == fragment.iso_alpha2,
                what=(
                    "WHT fragment iso_alpha2 must match the merged "
                    "income row for the same CBDT code (both should "
                    "derive from lookup_country(); drift indicates an "
                    "aggregator bug)"
                ),
                cbdt_country_code=fragment.cbdt_country_code,
                existing_iso_alpha2=existing.iso_alpha2,
                fragment_iso_alpha2=fragment.iso_alpha2,
            )
            require(
                existing.country_name_portal == fragment.country_name_portal,
                what=(
                    "WHT fragment country_name_portal must match the "
                    "merged income row for the same CBDT code (both "
                    "should derive from lookup_country())"
                ),
                cbdt_country_code=fragment.cbdt_country_code,
                existing_country_name_portal=existing.country_name_portal,
                fragment_country_name_portal=fragment.country_name_portal,
            )
            merged[key] = ScheduleFsiRow(
                cbdt_country_code=existing.cbdt_country_code,
                country_name_portal=existing.country_name_portal,
                iso_alpha2=existing.iso_alpha2,
                head_of_income=existing.head_of_income,
                income_inr=existing.income_inr,
                foreign_tax_inr=checked_addition(
                    existing.foreign_tax_inr,
                    fragment.foreign_tax_inr,
                    at=f"Schedule FSI WHT merge, contribution {position}, column foreign_tax_inr",
                ),
                event_count=existing.event_count,
            )

    merged_rows = sorted(
        merged.values(),
        key=lambda r: (r.cbdt_country_code, r.head_of_income.value),
    )
    cross_fy_reversal_report = (
        wht_cross_fy_reversal_report
        if wht_cross_fy_reversal_report is not None
        else WhtCrossFyReversalReport()
    )
    return ScheduleFsiComputeResult(
        rows=merged_rows,
        cross_fy_reversal_report=cross_fy_reversal_report,
    )


def _split_os_field(
    amounts_by_country: Mapping[str, Sequence[Decimal]],
    countries: Sequence[str],
    *,
    filed: Decimal,
    label: str,
) -> Iterator[tuple[str, Decimal, Decimal]]:
    """Yield each country's exact INR and whole-rupee share of one Schedule OS field."""
    exact = [exact_signed_inr_sum(amounts_by_country[iso], label=label) for iso in countries]
    shares = split_whole_rupees(
        exact, target=filed, what=f"{label} whole rupees must split across its countries"
    )
    yield from zip(countries, exact, shares, strict=True)


def _schedule_os_field_shares(
    os_result: ScheduleOsComputeResult,
    interest_result: ScheduleOsInterestComputeResult | None,
    interest_country_iso2: str | None,
) -> Iterator[tuple[str, Decimal, Decimal]]:
    """Yield ``(country, exact INR, whole rupees)`` per country of each filed
    Schedule OS field Schedule FSI's Other Sources income comes from (#1319).

    1a(i) splits across its foreign issuer countries in ISO order, then
    India's dividends; 1b(ix) is wholly its payer country's; each 1e nature
    splits across its source countries in ISO order.
    """
    dividends: dict[str, list[Decimal]] = {}
    for line in os_result.lines:
        dividends.setdefault(line.issuer_country_iso2, []).append(line.amount_gross_inr)
    countries = sorted(dividends)
    if os_result.domestic_lines:
        dividends[_INDIA] = [line.amount_gross_inr for line in os_result.domestic_lines]
        countries.append(_INDIA)
    if countries:
        yield from _split_os_field(
            dividends,
            countries,
            filed=os_result.ordinary_dividend_total_inr_rounded,
            label=DIVIDEND_LABEL,
        )
    if interest_result is not None and interest_result.lines:
        assert interest_country_iso2 is not None
        yield from _split_os_field(
            {interest_country_iso2: [interest_result.total_inr_full]},
            [interest_country_iso2],
            filed=interest_result.total_inr_rounded,
            label="Schedule OS 1b(ix) interest",
        )
    for nature, _exact, filed in os_result.other_income_totals:
        by_country: dict[str, list[Decimal]] = {}
        for line in os_result.other_income_lines:
            if line.nature == nature:
                require(
                    line.source_country_iso2 is not None,
                    what="Schedule FSI other income requires a reviewed source country",
                )
                assert line.source_country_iso2 is not None
                by_country.setdefault(line.source_country_iso2, []).append(line.amount_gross_inr)
        yield from _split_os_field(
            by_country, sorted(by_country), filed=filed, label=OTHER_INCOME_LABEL
        )


def _file_schedule_os_rupees(
    result: ScheduleFsiComputeResult,
    *,
    os_result: ScheduleOsComputeResult,
    interest_result: ScheduleOsInterestComputeResult | None,
    interest_country_iso2: str | None,
) -> ScheduleFsiComputeResult:
    """Set each Other Sources row's filed income to its Schedule OS rupees (#1319).

    A country's rupees are the sum of its shares of the filed Schedule OS
    fields, so the Other Sources rows add up to those fields less India's
    dividends. Refuses unless each Other Sources row's exact income is
    exactly its country's lines in those fields, and every foreign country
    in them has a row.
    """
    exact_by_country: dict[str, Decimal] = {}
    rupees_by_country: dict[str, Decimal] = {}
    for iso, exact, rupees in _schedule_os_field_shares(
        os_result, interest_result, interest_country_iso2
    ):
        exact_by_country[iso] = checked_addition(
            exact_by_country.get(iso, Decimal()),
            exact,
            at="Schedule FSI Other Sources income from Schedule OS",
        )
        rupees_by_country[iso] = rupees_by_country.get(iso, Decimal()) + rupees
    exact_by_country.pop(_INDIA, None)
    other_sources = [row for row in result.rows if row.head_of_income is HeadOfIncome.OTHER_SOURCES]
    require(
        sorted(row.iso_alpha2 for row in other_sources) == sorted(exact_by_country)
        and all(row.income_inr == exact_by_country[row.iso_alpha2] for row in other_sources),
        what="Schedule FSI Other Sources income must be exactly its Schedule OS fields' lines",
    )
    return dataclasses.replace(
        result,
        rows=[
            dataclasses.replace(row, filed_income_inr=rupees_by_country[row.iso_alpha2])
            if row.head_of_income is HeadOfIncome.OTHER_SOURCES
            else row
            for row in result.rows
        ],
    )


def compute_schedule_fsi(
    *,
    os_result: ScheduleOsComputeResult,
    cg_stcg_result: ScheduleCgStcgComputeResult | None = None,
    cg_ltcg_result: ScheduleCgLtcgComputeResult | None = None,
    interest_result: ScheduleOsInterestComputeResult | None = None,
    substitute_dividend_result: ScheduleOsSubstituteDividendComputeResult | None = None,
    other_income_result: ScheduleOsOtherIncomeComputeResult | None = None,
    broker_domicile_iso2: str | None,
    withholding_tax: Iterable[WithholdingTax] | None = None,
    wht_fx_policy: FxPolicy | None = None,
    financial_year_start: date | None = None,
    financial_year_end: date | None = None,
    adr_routing_mode: ScheduleOsIndianAdrRoutingMode = ScheduleOsIndianAdrRoutingMode.DEPOSITARY,
    additional_income_results: Iterable[ScheduleFsiComputeResult] = (),
) -> ScheduleFsiComputeResult:
    """Aggregate all FSI-eligible income into per-country FSI rows.

    Top-level entry point for the Schedule FSI compute. Dispatches
    to per-source aggregators and merges their results via
    :func:`_merge_results` (field-wise sum on
    ``(cbdt_country_code, head_of_income)`` key collisions).

    Currently dispatched:

    * :func:`compute_schedule_fsi_from_dividends` (foreign-source
      ordinary dividends from Schedule OS 1a(i) → OTHER_SOURCES).
    * :func:`compute_schedule_fsi_from_capital_gains` (Schedule CG
      STCG + LTCG foreign-source closures → CAPITAL_GAINS;
      ``None`` defaults are treated as empty).
    * :func:`compute_schedule_fsi_from_interest` (Schedule OS
      broker credit-interest → OTHER_SOURCES, attributed to the
      broker's domicile country; ``None`` default is treated as
      empty).
    * :func:`compute_schedule_fsi_from_withholding_tax` (Dividends-
      section foreign withholding tax → ``foreign_tax_inr`` column
      layered onto existing income rows via :func:`_merge_results`'s
      ``wht_contributions`` kwarg; ``None`` default is treated as
      empty).

    Each Other Sources row then files its country's shares of the filed
    Schedule OS fields it comes from (:attr:`ScheduleFsiRow.filed_income_inr`,
    #1319), not the HALF_UP of its exact income, so the rows add up to those
    fields: 1a(i) less India's share, 1b(ix) and each 1e nature. Each field's
    whole rupees split across its countries by
    :func:`~ibkr_fa.compute.schedule_os_periods.split_whole_rupees`. ITR-3
    validation rule 894 refuses an upload whose FSI Other Sources income is
    more than Schedule OS's when relief is claimed against Other Sources. The
    exact income stays on the row for the relief reconciliation and the
    audit columns.

    Args:
        os_result: The Schedule OS compute result whose
            ``lines`` attribute carries the foreign-source
            dividend pipeline output. Required.
        cg_stcg_result: The Schedule CG STCG compute result
            whose ``stcg_lines`` attribute carries the in-FY
            non-Indian-source short-term lines. Default
            ``None`` = no STCG contribution.
        cg_ltcg_result: The Schedule CG LTCG compute result
            whose ``ltcg_lines`` attribute carries the in-FY
            non-Indian-source long-term new-regime lines.
            Default ``None`` = no LTCG contribution.
        interest_result: The Schedule OS interest compute result
            whose ``lines`` attribute carries the in-FY
            broker credit-interest events. Default ``None`` =
            no interest contribution.
        broker_domicile_iso2: The ISO 3166-1 alpha-2 country code
            of the broker's domicile, used to attribute all
            broker credit-interest. Required, with no default;
            ``None`` only when there is no interest. See
            :func:`compute_schedule_fsi_from_interest` for the
            broker-domicile attribution rationale and checks.
        withholding_tax: Iterable of parsed
            :class:`~ibkr_fa.models.WithholdingTax` rows from the
            Dividends section. Default ``None`` = no WHT
            contribution. When provided, ``wht_fx_policy``,
            ``financial_year_start``, and ``financial_year_end``
            are required.
        wht_fx_policy: The :class:`~ibkr_fa.fx.policy.FxPolicy`
            (typically
            :class:`~ibkr_fa.fx.policy_rule128.Rule128Policy`)
            used to convert each in-FY WHT row's USD amount to
            INR on its own ``payment_date``. Required when
            ``withholding_tax`` is non-None.
        financial_year_start: Inclusive lower bound for the WHT
            ``payment_date`` filter. Required when
            ``withholding_tax`` is non-None.
        financial_year_end: Inclusive upper bound for the WHT
            ``payment_date`` filter. Required when
            ``withholding_tax`` is non-None.
        adr_routing_mode: The Indian-ADR routing mode the caller's
            Schedule OS dispatch used (default
            :attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.DEPOSITARY`).
            Threaded through to
            :func:`compute_schedule_fsi_from_withholding_tax` so
            the WHT aggregation's source-country scope mirrors the
            dividend-side rerouting. See issue #225.
        additional_income_results: Precomputed income-only FSI
            contributions from source-specific adapters.

    Returns:
        A merged :class:`ScheduleFsiComputeResult` across all
        FSI-eligible income sources, sorted by
        ``(cbdt_country_code, head_of_income)``. Same-key rows
        from different income aggregators (e.g. US dividends and
        US broker interest both under ``(US, OTHER_SOURCES)``)
        are summed via :func:`_merge_results`. WHT contributions
        are layered onto the matching income rows by adding
        their ``foreign_tax_inr`` to the row's tax column
        (income and event_count are NOT modified by WHT).

    Raises:
        :class:`~ibkr_fa.invariants.InvariantError`: If an Other Sources
            row's exact income isn't exactly its country's lines in the
            Schedule OS fields, or a foreign country in them has no row. If
            ``withholding_tax`` is non-None but any of
            ``wht_fx_policy``, ``financial_year_start``,
            ``financial_year_end`` is None. Also if any WHT
            row's currency mismatches the FX policy. Also for
            orphan WHT (foreign tax was withheld for a
            ``(country, head)`` with no matching income row) —
            see :func:`_merge_results`' "Withholding-tax
            fragments" docstring section.
        :class:`~ibkr_fa.reference.country_codes.CountryCodeNotFoundError`:
            If any aggregator's source country is not in the
            vendored country-code mapping.
        InterestPayerCountryError: As
            :func:`compute_schedule_fsi_from_interest` raises it.

    Multi-aggregator merge contract:

    When multiple INCOME aggregators produce
    :class:`ScheduleFsiComputeResult` values that overlap on the
    same ``(cbdt_country_code, head_of_income)`` key — e.g. a US
    dividend aggregator and a US interest aggregator both
    emitting ``(US, OTHER_SOURCES)`` with positive income —
    list-concatenation is INCORRECT (it would violate the
    at-most-one-row-per-key invariant). Each aggregator-pair
    sharing a key MUST be merged by summing ``income_inr``,
    summing ``foreign_tax_inr``, and summing ``event_count``;
    the ``country_name_portal`` and ``iso_alpha2`` MUST be
    identical for the same CBDT code (vendored from a single
    source); the merged row re-triggers the
    :class:`~ibkr_fa.models.ScheduleFsiRow` invariants. This
    contract is implemented by :func:`_merge_results`.

    Withholding-tax (tax-only) contributor:

    The WHT aggregator produces tax-only fragments
    (:class:`ScheduleFsiWhtContribution`) rather than full
    :class:`ScheduleFsiComputeResult` rows, because a row with
    ``income_inr = 0`` would fail
    :class:`~ibkr_fa.models.ScheduleFsiRow.__post_init__`. The
    dispatcher passes these via ``_merge_results(...,
    wht_contributions=...)`` and the merge primitive layers
    them onto the already-merged income rows. Orphan WHT
    (fragment with no matching income row) raises — see the
    merge primitive's docstring for the rationale.
    """
    dividend_result = compute_schedule_fsi_from_dividends(os_result)
    cg_result = compute_schedule_fsi_from_capital_gains(
        cg_stcg_result=cg_stcg_result,
        cg_ltcg_result=cg_ltcg_result,
    )
    interest_fsi_result = compute_schedule_fsi_from_interest(
        interest_result,
        broker_domicile_iso2=broker_domicile_iso2,
    )
    substitute_dividend_fsi_result = compute_schedule_fsi_from_substitute_dividends(
        substitute_dividend_result
    )
    other_income_fsi_result = compute_schedule_fsi_from_other_income(other_income_result)
    wht_contributions: Iterable[ScheduleFsiWhtContribution] | None = None
    wht_cross_fy_reversal_report: WhtCrossFyReversalReport | None = None
    if withholding_tax is not None:
        require(
            wht_fx_policy is not None
            and financial_year_start is not None
            and financial_year_end is not None,
            what=(
                "compute_schedule_fsi: when withholding_tax is provided, "
                "wht_fx_policy, financial_year_start, and "
                "financial_year_end are all required"
            ),
            wht_fx_policy_provided=wht_fx_policy is not None,
            financial_year_start_provided=financial_year_start is not None,
            financial_year_end_provided=financial_year_end is not None,
        )
        # `require` above guarantees these are non-None for ty/mypy.
        assert wht_fx_policy is not None
        assert financial_year_start is not None
        assert financial_year_end is not None
        wht_aggregate_result = compute_schedule_fsi_from_withholding_tax(
            withholding_tax,
            fx_policy=wht_fx_policy,
            financial_year_start=financial_year_start,
            financial_year_end=financial_year_end,
            adr_routing_mode=adr_routing_mode,
        )
        wht_contributions = wht_aggregate_result.contributions
        wht_cross_fy_reversal_report = wht_aggregate_result.cross_fy_reversal_report
    merged = _merge_results(
        dividend_result,
        cg_result,
        interest_fsi_result,
        substitute_dividend_fsi_result,
        other_income_fsi_result,
        *additional_income_results,
        wht_contributions=wht_contributions,
        wht_cross_fy_reversal_report=wht_cross_fy_reversal_report,
    )
    return _file_schedule_os_rupees(
        merged,
        os_result=os_result,
        interest_result=interest_result,
        interest_country_iso2=(
            None if broker_domicile_iso2 is None else broker_domicile_iso2.upper()
        ),
    )
