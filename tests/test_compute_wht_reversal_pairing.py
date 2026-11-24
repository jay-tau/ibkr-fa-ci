"""Tests for :mod:`ibkr_fa.compute.wht_reversal_pairing` (issue #224).

Covers:
* :func:`pair_wht_reversals` — heuristic LIFO matching of reversal
  rows (positive amounts) to original tax-paid rows (negative
  amounts) on same ISIN + equal-and-opposite amount.
* :func:`classify_ambiguity` — Indian-FY-boundary classification
  for candidate-original date sets.
* :func:`indian_fy_start_for` — calendar → Indian FY mapping.
* :class:`WhtPairingResult` and :class:`WhtCrossFyReversalReport`
  data shapes.

The pairing helper is mode-agnostic (ADR-skip happens BEFORE
pairing in the compute aggregators per RubberDuck #8 pre-impl
critique); tests cover the helper in isolation.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.wht_reversal_pairing import (
    WhtCrossFyReversalReport,
    classify_ambiguity,
    indian_fy_start_for,
    pair_wht_reversals,
)
from ibkr_fa.models import WithholdingTax
from ibkr_fa.types import Isin, Ticker


def _wht(
    *,
    amount: Decimal,
    payment_date: date,
    isin: str = "US99992A1097",
    symbol: str = "XQAQ",
) -> WithholdingTax:
    """Test helper: WHT row with default XQAQ US-ISIN identity."""
    return WithholdingTax(
        symbol=Ticker(symbol),
        isin=Isin(isin),
        payment_date=payment_date,
        amount=amount,
        currency="USD",
        description=f"{symbol}({isin}) test - US Tax",
    )


# ============================================================================
# pair_wht_reversals — basic matching
# ============================================================================


def test_empty_input_returns_empty_result() -> None:
    result = pair_wht_reversals([])
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset()
    assert result.ambiguous_reversal_candidates == {}


def test_only_originals_returns_empty_pairings() -> None:
    """Negative-only rows pair with nothing — they ARE candidates,
    not reversals.
    """
    rows = [
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),
        _wht(amount=Decimal("-3.00"), payment_date=date(2025, 5, 15)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset()
    assert result.ambiguous_reversal_candidates == {}


def test_only_reversals_all_orphan() -> None:
    """Positive-only rows have no candidates → all orphan."""
    rows = [
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 4, 15)),
        _wht(amount=Decimal("3.00"), payment_date=date(2025, 5, 15)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset({0, 1})


def test_single_pair_match_succeeds() -> None:
    """One original + one matching reversal → exact LIFO pair."""
    rows = [
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 5, 10)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {1: date(2025, 4, 15)}
    assert result.orphan_reversal_indices == frozenset()
    assert result.ambiguous_reversal_candidates == {}


def test_amount_mismatch_orphan() -> None:
    """Reversal of +3 USD does NOT pair with original of -5 USD —
    equal-and-opposite required.
    """
    rows = [
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),
        _wht(amount=Decimal("3.00"), payment_date=date(2025, 5, 10)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset({1})


def test_isin_mismatch_orphan() -> None:
    """Reversal on XQAQ does NOT pair with XQCA original."""
    rows = [
        _wht(
            amount=Decimal("-5.00"),
            payment_date=date(2025, 4, 15),
            isin="US99972B1017",
            symbol="XQCA",
        ),
        _wht(
            amount=Decimal("5.00"),
            payment_date=date(2025, 5, 10),
            isin="US99992A1097",
            symbol="XQAQ",
        ),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset({1})


def test_currency_mismatch_orphan() -> None:
    """Reversal in USD does NOT pair with original in EUR — currency
    is part of the (isin, currency) per-stack key (T3 r0 P3 of
    PR #254). The pairing helper's defensive contract: even if two
    rows have the same ISIN + equal numeric magnitudes, they don't
    pair across currencies. v1 callers' single-currency invariant
    already prevents this from arising in practice, but the helper
    must not silently mis-pair a future multi-currency case (#52).
    """
    rows = [
        WithholdingTax(
            symbol=Ticker("XQAQ"),
            isin=Isin("US99992A1097"),
            payment_date=date(2025, 4, 15),
            amount=Decimal("-5.00"),
            currency="EUR",
            description="XQAQ(US99992A1097) test - EUR Tax",
        ),
        WithholdingTax(
            symbol=Ticker("XQAQ"),
            isin=Isin("US99992A1097"),
            payment_date=date(2025, 5, 10),
            amount=Decimal("5.00"),
            currency="USD",
            description="XQAQ(US99992A1097) test - US Tax refund",
        ),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset({1})


# ============================================================================
# Exact amounts past 28 digits (#1312)
# ============================================================================


def test_equal_and_opposite_pair_past_28_digits_pairs() -> None:
    """#1312: a 29-digit pair is equal and opposite, so it pairs. Unary minus
    rounded the reversal to 28 digits, so main left it an orphan."""
    rows = [
        _wht(amount=Decimal("-123456789.12345678901234567891"), payment_date=date(2025, 9, 12)),
        _wht(amount=Decimal("123456789.12345678901234567891"), payment_date=date(2025, 10, 20)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {1: date(2025, 9, 12)}
    assert result.orphan_reversal_indices == frozenset()
    assert result.ambiguous_reversal_candidates == {}


def test_reversal_equal_only_at_28_digits_is_an_orphan() -> None:
    """#1312: the reversal is $1E-20 more than the tax, so it has no
    equal-and-opposite original. Main rounded it to the tax and paired them."""
    rows = [
        _wht(amount=Decimal("-123456789.1234567890123456789"), payment_date=date(2025, 9, 12)),
        _wht(amount=Decimal("123456789.12345678901234567891"), payment_date=date(2025, 10, 20)),
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {}
    assert result.orphan_reversal_indices == frozenset({1})
    assert result.ambiguous_reversal_candidates == {}


# ============================================================================
# Sort-tiebreak for same-date original + reversal (RubberDuck #6)
# ============================================================================


def test_same_date_original_reversal_pairs_correctly() -> None:
    """Same-date in-cycle correction: original sorts BEFORE reversal
    on the same date via the (date, sign-first) tiebreak.
    """
    rows = [
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 4, 15)),  # reversal listed first
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),  # original listed second
    ]
    result = pair_wht_reversals(rows)
    # Reversal at index 0 pairs with original at index 1.
    assert result.matched_originals == {0: date(2025, 4, 15)}
    assert result.orphan_reversal_indices == frozenset()


# ============================================================================
# LIFO heuristic on multiple equal-amount candidates
# ============================================================================


def test_multiple_candidates_lifo_picks_most_recent() -> None:
    """Multiple unpaired negatives with same ISIN + same amount;
    LIFO picks the most-recent (highest payment_date).
    """
    rows = [
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),  # older
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 6, 15)),  # most recent
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 7, 1)),  # reversal
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {2: date(2025, 6, 15)}
    assert result.ambiguous_reversal_candidates == {
        2: frozenset({date(2025, 4, 15), date(2025, 6, 15)}),
    }


def test_multiple_reversals_chain_pair_in_order() -> None:
    """Two originals + two reversals on the same ISIN: each reversal
    pairs with the most recent unpaired original (LIFO).
    """
    rows = [
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 4, 15)),
        _wht(amount=Decimal("-5.00"), payment_date=date(2025, 5, 15)),
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 6, 1)),  # pairs with May (LIFO)
        _wht(amount=Decimal("5.00"), payment_date=date(2025, 7, 1)),  # pairs with Apr
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {
        2: date(2025, 5, 15),
        3: date(2025, 4, 15),
    }
    # First reversal: 2 candidates → ambiguous.
    # Second reversal: only 1 candidate left (Apr) → unambiguous.
    assert result.ambiguous_reversal_candidates == {
        2: frozenset({date(2025, 4, 15), date(2025, 5, 15)}),
    }


# ============================================================================
# Cross-FY pairing — the issue #224 reproducer
# ============================================================================


def test_issue_224_reproducer_cross_fy_pair_detected() -> None:
    """Issue #224 example: original FY 2024-25 (Mar 15, 2025) +
    refund FY 2025-26 (Apr 10, 2025). The helper records the
    pairing; the caller computes the cross-FY exclusion decision.
    """
    rows = [
        _wht(amount=Decimal("-1000.00"), payment_date=date(2025, 3, 15)),  # FY 2024-25
        _wht(amount=Decimal("1000.00"), payment_date=date(2025, 4, 10)),  # FY 2025-26
    ]
    result = pair_wht_reversals(rows)
    assert result.matched_originals == {1: date(2025, 3, 15)}
    assert result.orphan_reversal_indices == frozenset()


# ============================================================================
# classify_ambiguity — Indian-FY boundary detection
# ============================================================================


def test_classify_ambiguity_single_candidate_returns_false() -> None:
    """One candidate is unambiguous — no pairing decision to make."""
    assert classify_ambiguity(frozenset({date(2025, 6, 1)})) is False


def test_classify_ambiguity_candidates_same_fy_returns_false() -> None:
    """Two candidates in same Indian FY (e.g., both April 2025 and
    May 2025) → same-FY ambiguity (informational only).
    """
    assert classify_ambiguity(frozenset({date(2025, 4, 15), date(2025, 5, 20)})) is False


def test_classify_ambiguity_candidates_cross_fy_returns_true() -> None:
    """Candidates spanning the Apr 1 FY boundary (e.g., Mar 15
    2025 + May 20 2025) → cross-FY ambiguity. Note both dates are
    in CALENDAR year 2025, but Mar 15 is FY 2024-25 and May 20 is
    FY 2025-26.
    """
    assert classify_ambiguity(frozenset({date(2025, 3, 15), date(2025, 5, 20)})) is True


def test_classify_ambiguity_candidates_in_calendar_year_boundary() -> None:
    """Cross-calendar-year candidates within the SAME Indian FY
    (e.g., Dec 15 + Feb 15) → same-FY (NOT cross-FY): both fall
    within FY ``year-1``-``year``.
    """
    assert classify_ambiguity(frozenset({date(2025, 12, 15), date(2026, 2, 15)})) is False


def test_classify_ambiguity_takes_no_financial_year_start() -> None:
    """The Apr 1 boundary comes from each date, so no FY anchor is passed (#1137)."""
    assert "financial_year_start" not in inspect.signature(classify_ambiguity).parameters


# ============================================================================
# indian_fy_start_for — calendar → FY mapping
# ============================================================================


def test_indian_fy_start_for_april_is_current_year() -> None:
    """April 1 starts a new Indian FY."""
    assert indian_fy_start_for(date(2025, 4, 1)) == date(2025, 4, 1)


def test_indian_fy_start_for_march_is_prior_year() -> None:
    """March belongs to the FY that started the prior April."""
    assert indian_fy_start_for(date(2025, 3, 31)) == date(2024, 4, 1)


def test_indian_fy_start_for_december_is_prior_year() -> None:
    """December belongs to the FY that started the prior April."""
    assert indian_fy_start_for(date(2025, 12, 31)) == date(2025, 4, 1)


# ============================================================================
# WhtCrossFyReversalReport — default constructor + field semantics
# ============================================================================


def test_cross_fy_report_default_is_all_zero() -> None:
    """Empty-default report shipped on Form67ComputeResult /
    ScheduleFsiComputeResult when no WHT is processed.
    """
    rep = WhtCrossFyReversalReport()
    assert rep.cross_fy_excluded_count == 0
    assert rep.cross_fy_excluded_amount_foreign == {}
    assert rep.orphan_reversals_count == 0
    assert rep.ambiguous_cross_fy_count == 0
    assert rep.ambiguous_same_fy_count == 0


def test_cross_fy_report_carries_constructor_values() -> None:
    """Fields are read-only post-construction; values pass through."""
    rep = WhtCrossFyReversalReport(
        cross_fy_excluded_count=3,
        cross_fy_excluded_amount_foreign={"USD": Decimal("15.50")},
        orphan_reversals_count=1,
        ambiguous_cross_fy_count=2,
        ambiguous_same_fy_count=4,
    )
    assert rep.cross_fy_excluded_count == 3
    assert rep.cross_fy_excluded_amount_foreign == {"USD": Decimal("15.50")}
    assert rep.orphan_reversals_count == 1
    assert rep.ambiguous_cross_fy_count == 2
    assert rep.ambiguous_same_fy_count == 4
