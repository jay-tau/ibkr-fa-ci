"""Tests for :mod:`ibkr_fa.compliance.itr_form_selection` (#64)."""

from __future__ import annotations

from ibkr_fa.compliance import ITR_FORM_SELECTION_ADVISORY


def test_advisory_names_all_four_itr_forms() -> None:
    """The advisory must name each of ITR-1 (excluded), ITR-4
    (also excluded as the presumptive-scheme sibling), ITR-2
    (default), and ITR-3 (when business income present)
    explicitly. T1-r1 added the ITR-4 mention to close a real
    user gap (presumptive-scheme filers who would otherwise
    reach for Sugam after being told ITR-1 is out).
    """
    assert "ITR-1" in ITR_FORM_SELECTION_ADVISORY
    assert "ITR-2" in ITR_FORM_SELECTION_ADVISORY
    assert "ITR-3" in ITR_FORM_SELECTION_ADVISORY
    assert "ITR-4" in ITR_FORM_SELECTION_ADVISORY
    # ITR-1 (Sahaj) + ITR-4 (Sugam) parentheticals help
    # recognition for users familiar only with the colloquial
    # form names.
    assert "Sahaj" in ITR_FORM_SELECTION_ADVISORY
    assert "Sugam" in ITR_FORM_SELECTION_ADVISORY


def test_advisory_excludes_itr_1_and_itr_4_for_foreign_asset_holders() -> None:
    """ITR-1 (Sahaj) and ITR-4 (Sugam) both bear the foreign-asset
    exclusion on their form headers. The advisory must call both
    out so a user with a foreign equity holding doesn't default
    to either.
    """
    assert "ITR-1" in ITR_FORM_SELECTION_ADVISORY
    assert "ITR-4" in ITR_FORM_SELECTION_ADVISORY
    # Single phrase 'cannot use ITR-1 (Sahaj) or ITR-4 (Sugam)' covers
    # both forms; locking the OR-joined phrasing prevents a future
    # maintainer from splitting it and silently dropping ITR-4
    # (T3-r1 P3 — symmetry assertion).
    assert "cannot use ITR-1 (Sahaj) or ITR-4 (Sugam)" in ITR_FORM_SELECTION_ADVISORY


def test_advisory_names_business_income_disqualifiers_for_itr_2() -> None:
    """ITR-2 is unavailable to assessees with business / profession
    income. The advisory must name the common categories ibkr-fa
    users might overlook (intraday trading, F&O, freelance,
    consulting, partnership distributive share) so the user
    can correctly self-select ITR-3 when applicable.
    """
    assert "business" in ITR_FORM_SELECTION_ADVISORY
    assert "profession" in ITR_FORM_SELECTION_ADVISORY
    assert "intraday" in ITR_FORM_SELECTION_ADVISORY
    assert "F&O" in ITR_FORM_SELECTION_ADVISORY
    assert "freelance" in ITR_FORM_SELECTION_ADVISORY


def test_advisory_distinguishes_delivery_capital_gains_from_business_income() -> None:
    """A common confusion: LTCG / STCG on delivery-based equity is
    typically NOT business income, even though it involves
    'trading'. The advisory must say this explicitly so an ibkr-fa
    user with only-delivery activity isn't pushed into ITR-3 by
    accident.

    T3-r1 P2 nuance: 'typically NOT' is intentional; CBDT Circular
    6/2016 carves out a stock-in-trade path where delivery-based
    listed-share gains ARE business income. The advisory must
    flag this carve-out so a frequent-trader user is not
    auto-routed to ITR-2 when ITR-3 is correct for their
    fact-pattern. Both the 'typically NOT' framing and the
    'Circular 6/2016' / 'stock-in-trade' citations are locked.
    """
    assert "delivery" in ITR_FORM_SELECTION_ADVISORY
    assert "*typically* NOT" in ITR_FORM_SELECTION_ADVISORY
    assert "stock-in-trade" in ITR_FORM_SELECTION_ADVISORY
    assert "Circular 6/2016" in ITR_FORM_SELECTION_ADVISORY


def test_advisory_qualifies_section_43_5_derivative_exception() -> None:
    """The Income-tax Act §43(5) defines speculative business
    Citing the section anchors the advisory in statutory law.
    Proviso (d) is exchange-bound, so foreign derivatives must not
    be described as automatically non-speculative.
    """
    assert "Section 43(5)" in ITR_FORM_SELECTION_ADVISORY
    assert "proviso (d)" in ITR_FORM_SELECTION_ADVISORY
    assert "eligible derivatives" in ITR_FORM_SELECTION_ADVISORY
    assert "prescribed/notified recognised stock exchange" in ITR_FORM_SELECTION_ADVISORY
    assert "foreign-exchange derivatives do not automatically qualify" in (
        ITR_FORM_SELECTION_ADVISORY
    )


def test_advisory_prompts_ca_consultation_when_in_doubt() -> None:
    """ibkr-fa cannot determine business income from IBKR inputs
    alone, so the user must self-assess. The advisory must
    direct the user at a CA for the genuinely ambiguous case.
    """
    assert "consult a CA" in ITR_FORM_SELECTION_ADVISORY


def test_advisory_csv_safe_no_formula_injection_triggers() -> None:
    """Defense-in-depth: the advisory is CLI-only, not a CSV cell,
    but a future maintainer might splice it into a CSV. Guard
    against the writer-contract formula-injection rule by
    confirming no line starts with =, +, -, @, or |.
    """
    for line in ITR_FORM_SELECTION_ADVISORY.splitlines():
        if not line:
            continue
        assert not line.startswith(("=", "+", "-", "@", "|")), line
