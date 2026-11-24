"""Heuristic WHT-reversal-to-original pairing for cross-FY exclusion.

Issue #224. The IBKR Activity Statement's Withholding Tax section does
NOT carry a settlement-metadata field (transaction ID, original-of
pointer, etc.) that lets us deterministically link a reversal/refund
(positive amount) to the originating tax-paid row (negative amount).
Pairing must be heuristic:

* **Same source account, ISIN and currency** — the reversal applies to
  a WHT row from the same instrument in the same account
  (:attr:`~ibkr_fa.models.WithholdingTax.source_account_index`;
  single-account sources leave it ``None``, and ``None`` rows pair
  with each other).
* **Equal-and-opposite amount** — a refund of ``+$5 USD`` cancels an
  original of ``-$5 USD`` exactly. Partial refunds (refund magnitude
  less than the original) are NOT supported in v1; such pairings
  return ``orphan`` (see :data:`WhtPairingResult.orphan_reversal_indices`).
* **LIFO** — when multiple unpaired originals match a reversal, the
  most recent (highest payment_date) is chosen. Real-world IBKR
  refunds typically happen within the same settlement cycle (days to
  weeks) so LIFO maximizes accuracy in the common case.

When the pairing decision **affects FY classification** — e.g., the
LIFO-chosen most-recent original is in the current FY but an
alternative candidate exists in a prior FY (or vice versa) — the
reversal is counted in :attr:`WhtCrossFyReversalReport.ambiguous_cross_fy_count`.
Pairings that are ambiguous but all candidates share the same FY
classification are counted in
:attr:`WhtCrossFyReversalReport.ambiguous_same_fy_count`; the FY-level
inclusion decision is unchanged regardless of which specific original
was matched.

Pairing itself never refuses. :func:`require_proven_reversal_originals`
refuses a current-FY reversal that is an orphan or whose candidates
span financial years (#893): the year of the tax it cancels is then
unproven, so any current-FY total would be a guess.

:func:`~ibkr_fa.compute.foreign_tax.net_foreign_tax_by_country`
applies pairing **BEFORE** the
FY-scope filter: when a reversal in the current FY pairs with an
original whose ``payment_date`` is in a prior FY, the reversal is
EXCLUDED from current-FY per-country totals to prevent the
"silently absorbs unrelated same-country WHT and understates the
current-FY claim" sub-case (a2) documented at
:mod:`ibkr_fa.compute.foreign_tax`. The corresponding prior-FY
over-claim sub-case (b) is detected at the LATER FY's run (when the
reversal becomes visible in the statement period) and surfaced via
a CLI advisory recommending prior-FY return amendment.

ADR-skip ordering matters: under
:attr:`~ibkr_fa.config.ScheduleOsIndianAdrRoutingMode.UNDERLYING_ISSUER`,
mapped Indian-ADR WHT rows are excluded from compute aggregation
(issue #225). To prevent these skipped rows from polluting the
pairing report's counts, callers MUST apply the ADR-skip filter
**before** invoking :func:`pair_wht_reversals` — the helper itself
is mode-agnostic.

See ``docs/architecture.md`` and the issue #224 closure trail for
the design rationale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final

from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models import WithholdingTax

# Indian FY runs April 1 → March 31 — the calendar month that
# delimits the FY boundary.
_INDIAN_FY_START_MONTH: Final[int] = 4

# Minimum candidate-set size that qualifies as "ambiguous" — a
# reversal with only one candidate original is unambiguous.
_AMBIGUITY_MIN_CANDIDATES: Final[int] = 2


@dataclass(frozen=True, slots=True)
class WhtCrossFyReversalReport:
    """Diagnostic counts from one
    :func:`~ibkr_fa.compute.wht_reversal_pairing.pair_wht_reversals`-driven
    cross-FY exclusion pass at a compute aggregator (issue #224).

    Surfaced via :class:`~ibkr_fa.compute.form67.Form67ComputeResult`
    and the FSI WHT aggregate result so that the pipeline / RunReport
    / CLI layers can emit user advisories WITHOUT re-running the
    pairing logic. All counts are bounded by the size of the
    in-FY-and-post-ADR-skip reversal subset.

    The report intentionally does NOT carry an INR-magnitude field
    for excluded reversals: an excluded reversal's INR equivalent
    at the **reversal-date** FX rate (per Rule 128(5)(ii)) is NOT
    necessarily equal to the INR amount by which the prior FY's
    return should be amended (which used the **original-date** FX
    rate). Reporting the foreign-currency magnitude avoids the
    cross-FY FX-rate ambiguity. See the issue #224 design note in
    :mod:`~ibkr_fa.compute.wht_reversal_pairing` module docstring
    for the rationale (RubberDuck #3 pre-implementation critique).

    Attributes:
        cross_fy_excluded_count: Number of reversals in the
            requested FY whose LIFO-matched original was in a
            **prior** FY — these reversals were EXCLUDED from the
            current-FY per-country WHT totals to prevent the
            "absorbs unrelated same-country WHT and understates
            current-FY claim" sub-case (a2) documented at
            :mod:`ibkr_fa.compute.foreign_tax`.
        cross_fy_excluded_amount_foreign: Mapping ``currency -> sum
            of foreign-currency magnitudes`` for the excluded
            reversals (always positive magnitudes; a reversal of
            ``+$5 USD`` contributes ``Decimal("5")`` to the USD
            entry). v1 expects a single currency entry (USD) per
            run; multi-currency support pending #52. Reported in
            foreign-currency rather than INR per the design note
            above.
        orphan_reversals_count: Number of reversals with no
            matching unpaired original visible in the statement.
            :func:`require_proven_reversal_originals` refuses them,
            so a report returned with netted totals counts none.
        ambiguous_cross_fy_count: Number of reversals whose
            candidate-original set **spans the Indian FY boundary**
            (Apr 1) — i.e., the LIFO choice between candidates may
            materially affect the cross-FY exclusion decision.
            Refused like orphans.
        ambiguous_same_fy_count: Number of reversals with multiple
            candidate originals where all candidates fall in the
            same Indian FY (LIFO choice is informational only —
            the cross-FY classification is unchanged either way).
    """

    cross_fy_excluded_count: int = 0
    cross_fy_excluded_amount_foreign: Mapping[str, Decimal] = field(default_factory=dict)
    orphan_reversals_count: int = 0
    ambiguous_cross_fy_count: int = 0
    ambiguous_same_fy_count: int = 0


def indian_fy_start_for(payment_date: date) -> date:
    """Return the Indian-FY start date (April 1 of the appropriate
    year) for ``payment_date``.

    Indian FY runs April 1 to March 31; a payment_date in (e.g.)
    Mar 2025 belongs to FY 2024-25 (Indian FY start April 1, 2024)
    while May 2025 belongs to FY 2025-26 (April 1, 2025). The Apr
    1 boundary is fixed statute and is derivable from the payment
    date alone — no per-call FY anchor is needed.
    """
    year = (
        payment_date.year if payment_date.month >= _INDIAN_FY_START_MONTH else payment_date.year - 1
    )
    return date(year, _INDIAN_FY_START_MONTH, 1)


def classify_ambiguity(candidate_dates: frozenset[date]) -> bool:
    """Return ``True`` iff the candidate-original date set spans
    the Indian FY boundary.

    "Spans" means at least one candidate is in a different Indian
    FY than another. Shared netting refuses a current-FY reversal
    with this ambiguity because its original's year is unproven.
    """
    if len(candidate_dates) < _AMBIGUITY_MIN_CANDIDATES:
        return False
    fy_starts = {indian_fy_start_for(d) for d in candidate_dates}
    return len(fy_starts) >= _AMBIGUITY_MIN_CANDIDATES


@dataclass(frozen=True, slots=True)
class WhtPairingResult:
    """Outcome of running :func:`pair_wht_reversals` over a set of
    :class:`~ibkr_fa.models.WithholdingTax` rows.

    All index sets/mappings key on the row's position in the
    materialized input tuple, so the caller can correlate back to
    the source rows without storing the rows in this result.

    Attributes:
        matched_originals: For each reversal row index (positive
            amount) that successfully paired, maps to the
            ``payment_date`` of the matched original (the LIFO-
            chosen most recent candidate). Indices NOT present are
            either originals (negative amount, never keyed here) or
            orphan reversals (see below).
        orphan_reversal_indices: Indices of reversal rows with NO
            matching unpaired original (same source account, ISIN
            and currency, equal-and-opposite amount) visible in the
            input.
        ambiguous_reversal_candidates: For each reversal that had
            **more than one** candidate original (same source
            account, ISIN and currency, equal-and-opposite amount),
            maps the reversal index to the
            frozenset of all candidate-original ``payment_date``
            values. The helper applied LIFO (the max date in this
            set is the chosen pairing — also recorded in
            :attr:`matched_originals`). Shared netting uses the
            full candidate set to refuse a current-FY reversal
            whose candidates span Indian financial years.
    """

    matched_originals: Mapping[int, date] = field(default_factory=dict)
    orphan_reversal_indices: frozenset[int] = field(default_factory=frozenset)
    ambiguous_reversal_candidates: Mapping[int, frozenset[date]] = field(default_factory=dict)


def pair_wht_reversals(
    wht_rows: Sequence[WithholdingTax],
) -> WhtPairingResult:
    """Pair each reversal (positive amount) to its most recent
    matching original (negative amount) via the LIFO heuristic.

    Algorithm:

    1. Sort row indices by ``(payment_date, sign-first, original_index)``
       — chronological, with same-date negatives processed BEFORE
       same-date positives so an in-cycle correction (original and
       reversal on the same date) pairs correctly. ``original_index``
       breaks remaining ties to ensure deterministic output.
    2. Maintain per-(source account, ISIN, currency) stacks of
       unpaired NEGATIVE row indices.
    3. For each row in sorted order:

       * **Negative (original / paid)** — push the index onto its
         stack.
       * **Positive (reversal)** — collect all unpaired negatives
         on the stack whose amount is equal-and-opposite to the
         reversal. Amounts compare exactly: the reversal is negated
         with ``copy_negate()``, which never rounds (#1312), so a
         pair matches only when every digit does. If none match,
         mark the reversal as orphan. If
         one or more match, pick the most recent (LIFO, by
         ``payment_date``) and record the pairing; if more than
         one match, also surface the full candidate set under
         :attr:`WhtPairingResult.ambiguous_reversal_candidates`
         so the caller can compute severity (e.g., FY-boundary
         spanning) per its own scope.

    Args:
        wht_rows: Sequence of :class:`~ibkr_fa.models.WithholdingTax`
            rows to pair. The caller MUST apply any pre-filtering
            (ADR-skip for #225) BEFORE invoking this helper —
            filtered-out rows MUST NOT appear in the input so the
            pairing report's counts are not polluted by them.

    Returns:
        A :class:`WhtPairingResult` with per-row pairing decisions
        + ambiguity / orphan flags. The result references row
        positions in ``wht_rows``; the caller correlates back via
        index.
    """
    rows = tuple(wht_rows)
    if not rows:
        return WhtPairingResult()

    # Sort by (date, sign-first-negative, original index). Same-date
    # negatives sort BEFORE positives so an in-cycle correction
    # pairs correctly. Sign sort key: 0 for negative, 1 for positive.
    sorted_indices = sorted(
        range(len(rows)),
        key=lambda i: (rows[i].payment_date, 0 if rows[i].amount < 0 else 1, i),
    )

    # Per-(source account, ISIN, currency) stack of unpaired negative
    # row indices. The account keeps one account's refund from pairing
    # with another account's withholding (#893). Currency is part of
    # the key so a future multi-currency setup (issue #52) doesn't
    # falsely pair (e.g.) -5 EUR with +5 USD; in v1 the compute
    # callers' single-currency invariant already prevents this, so it
    # is a defensive contract on the helper itself. T3 r0 P3 of PR #254.
    stacks: dict[tuple[int | None, str, str], list[int]] = {}

    matched_originals: dict[int, date] = {}
    orphan_reversal_indices: set[int] = set()
    ambiguous_reversal_candidates: dict[int, frozenset[date]] = {}

    for idx in sorted_indices:
        row = rows[idx]
        key = (row.source_account_index, row.isin, row.currency)
        if not row.is_reversal:
            # Negative-amount row — push as candidate for future
            # reversals on the same key.
            stacks.setdefault(key, []).append(idx)
            continue

        # Positive-amount row (reversal). Look for equal-and-opposite
        # candidates on its stack.
        candidates = stacks.get(key, [])
        # copy_negate, unlike unary minus, never rounds, so amounts
        # compare exactly (#1312).
        target_amount = row.amount.copy_negate()

        # Collect all unpaired matching candidates (preserves
        # insertion-order chronology; the LAST entry is the most
        # recent → LIFO).
        matching_idxs = [
            cand_idx for cand_idx in candidates if rows[cand_idx].amount == target_amount
        ]
        if not matching_idxs:
            orphan_reversal_indices.add(idx)
            continue

        # LIFO match: most recent by chronological insertion order.
        match_idx = matching_idxs[-1]
        candidates.remove(match_idx)
        matched_originals[idx] = rows[match_idx].payment_date

        if len(matching_idxs) > 1:
            # Surface the FULL candidate-date set for the caller to
            # compute severity (e.g., does the set span the Indian
            # Apr-Mar FY boundary?).
            ambiguous_reversal_candidates[idx] = frozenset(
                rows[c].payment_date for c in matching_idxs
            )

    return WhtPairingResult(
        matched_originals=matched_originals,
        orphan_reversal_indices=frozenset(orphan_reversal_indices),
        ambiguous_reversal_candidates=ambiguous_reversal_candidates,
    )


class UnprovenWhtReversalError(IbkrFaError):
    """A current-FY withholding reversal whose original's year is unproven (#893)."""


def require_proven_reversal_originals(
    *,
    adr_filtered_rows: Sequence[WithholdingTax],
    pairing_result: WhtPairingResult,
    financial_year_start: date,
    financial_year_end: date,
) -> None:
    """Refuse current-FY reversals that are orphans or ambiguous across FYs.

    An orphan (no equal-and-opposite original, including a partial
    refund) or a reversal whose candidate originals span the Apr 1
    boundary cancels tax of an unproven year, so netting it into this
    FY, or excluding it, would guess. Reversals outside the FY don't
    affect its totals and are left alone.

    Raises:
        UnprovenWhtReversalError: Naming, for each such reversal, its
            source account index (when set) and whether it has no
            original or candidates in more than one financial year. Its
            ISIN, amount, currency and dates are input, so the message
            doesn't print them (#1047).
    """
    unproven: list[str] = []
    for filtered_idx, row in enumerate(adr_filtered_rows):
        if not row.is_reversal:
            continue
        if not (financial_year_start <= row.payment_date <= financial_year_end):
            continue
        if filtered_idx in pairing_result.orphan_reversal_indices:
            originals = "no original"
        else:
            candidate_dates = pairing_result.ambiguous_reversal_candidates.get(filtered_idx)
            if candidate_dates is None or not classify_ambiguity(candidate_dates):
                continue
            originals = "candidate originals in more than one financial year"
        account = (
            ""
            if row.source_account_index is None
            else f" in source account {row.source_account_index}"
        )
        unproven.append(f"a reversal{account} with {originals}")
    if unproven:
        raise UnprovenWhtReversalError(
            f"withholding-tax reversal(s) in FY {financial_year_start.isoformat()} → "
            f"{financial_year_end.isoformat()} cancel tax of an unproven year: "
            + "; ".join(unproven)
            + ". A reversal pairs only with an equal-and-opposite withholding row "
            "of the same source account, ISIN and currency on or before its date. "
            "With none, or with candidates in more than one financial year, the "
            "year of the tax it cancels is unknown, so the run refuses rather than "
            "guess. If the statement starts after the original withholding, use "
            "one that covers it."
        )


def classify_cross_fy_exclusions(
    *,
    adr_filtered_rows: Sequence[WithholdingTax],
    pairing_result: WhtPairingResult,
    financial_year_start: date,
    financial_year_end: date,
) -> frozenset[int]:
    """Return the filtered-row indices of reversals in the current
    FY whose LIFO-matched original is in a PRIOR FY.

    Used by :func:`~ibkr_fa.compute.foreign_tax.net_foreign_tax_by_country`
    to skip these rows from per-country totals (issue #224 fix), so
    Form 67, Schedule FSI and the orphan-WHT guard share one
    cross-FY exclusion decision.
    """
    excluded: set[int] = set()
    for filtered_idx, row in enumerate(adr_filtered_rows):
        if not row.is_reversal:
            continue
        if not (financial_year_start <= row.payment_date <= financial_year_end):
            continue
        matched_original_date = pairing_result.matched_originals.get(filtered_idx)
        if matched_original_date is None:
            continue
        if matched_original_date < financial_year_start:
            excluded.add(filtered_idx)
    return frozenset(excluded)


def build_cross_fy_report(
    *,
    adr_filtered_rows: Sequence[WithholdingTax],
    pairing_result: WhtPairingResult,
    cross_fy_excluded_filtered_indices: frozenset[int],
    financial_year_start: date,
    financial_year_end: date,
) -> WhtCrossFyReversalReport:
    """Build the user-facing diagnostic report from the pairing
    result + cross-FY exclusion classification.

    Counts are scoped to reversals **in the current FY** only
    (orphan / ambiguous reversals outside the FY are silent — they
    don't affect this run's totals and the user's filing for the
    out-of-FY reversal happens at that year's run).

    Single source of truth for the report shape, built once per
    :func:`~ibkr_fa.compute.foreign_tax.net_foreign_tax_by_country`
    call (T3 r0 P3 of PR #254).
    """
    excluded_amount_foreign: dict[str, Decimal] = {}
    orphan_count = 0
    ambiguous_cross_fy = 0
    ambiguous_same_fy = 0

    for filtered_idx, row in enumerate(adr_filtered_rows):
        if not row.is_reversal:
            continue
        if not (financial_year_start <= row.payment_date <= financial_year_end):
            continue
        if filtered_idx in cross_fy_excluded_filtered_indices:
            excluded_amount_foreign[row.currency] = (
                excluded_amount_foreign.get(row.currency, Decimal("0")) + row.amount
            )
        if filtered_idx in pairing_result.orphan_reversal_indices:
            orphan_count += 1
        candidate_dates = pairing_result.ambiguous_reversal_candidates.get(filtered_idx)
        if candidate_dates is not None:
            if classify_ambiguity(candidate_dates):
                ambiguous_cross_fy += 1
            else:
                ambiguous_same_fy += 1

    return WhtCrossFyReversalReport(
        cross_fy_excluded_count=len(cross_fy_excluded_filtered_indices),
        cross_fy_excluded_amount_foreign=dict(excluded_amount_foreign),
        orphan_reversals_count=orphan_count,
        ambiguous_cross_fy_count=ambiguous_cross_fy,
        ambiguous_same_fy_count=ambiguous_same_fy,
    )
