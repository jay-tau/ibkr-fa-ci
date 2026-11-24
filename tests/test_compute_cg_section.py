"""Tests for :mod:`ibkr_fa.compute.cg_section` (CLI advisories, #62)."""

from __future__ import annotations

import re

from ibkr_fa.compute import (
    LTCG_SECTION_112_VS_112A_ADVISORY,
    STCG_SLAB_VS_111A_ADVISORY,
)

# ---- advisory copy invariants ----------------------------------------------


def test_ltcg_advisory_mentions_section_112_not_112a_distinction() -> None:
    """The LTCG advisory must explicitly contrast Section 112
    (foreign equity, no exemption) with Section 112A (Indian
    listed, ₹1.25 lakh exemption) so a user reading the stderr
    output cannot miss the mis-classification risk.

    Uses word-boundary regex assertions rather than naive
    ``in`` substring checks because ``"Section 112"`` is a
    prefix of ``"Section 112A"`` — a substring check would
    pass even if the advisory only said "NOT Section 112A"
    without the positive Section 112 attribution
    (T3-r1 finding).
    """
    advisory = LTCG_SECTION_112_VS_112A_ADVISORY
    # Positive attribution: the advisory must claim the output
    # IS Section 112, not just "NOT Section 112A". Word boundary
    # `(?!A)` prevents the §112-substring match from §112A.
    assert re.search(r"\bSection 112\b(?!A)", advisory)
    # Negative attribution: the advisory must explicitly disclaim
    # §112A.
    assert re.search(r"\bNOT Section 112A\b", advisory)
    assert "₹1.25 lakh exemption" in advisory
    # Right portal cell named explicitly; the wrong one is named
    # by the regime ('Section 112A row') rather than a specific
    # item number to avoid stale-cell-number drift against future
    # ITR-2 utility releases.
    assert "Item 8(e)" in advisory
    assert "B8e" in advisory
    assert "Section 112A row" in advisory


def test_stcg_advisory_mentions_slab_not_111a_distinction() -> None:
    """The STCG advisory must explicitly contrast slab-rate STCG
    (foreign equity, no special-rate section under the Act) with
    Section 111A (Indian listed, flat rate) so the user does not
    enter foreign STCG in the §111A row and trigger the flat
    rate on a slab-rate liability.

    The advisory must NOT call foreign STCG "Section 111" — §111
    of the Income-tax Act is about Recognised Provident Fund
    accumulated balances, not residual STCG (T5-r2 finding).
    """
    advisory = STCG_SLAB_VS_111A_ADVISORY
    # Positive attribution: slab-rate under the regular Chapter
    # IV-D provisions, NOT a special-rate section.
    assert "slab" in advisory
    assert "Chapter IV-D" in advisory
    # Negative attribution: must disclaim §111A.
    assert re.search(r"\bNOT Section 111A\b", advisory)
    # Anti-regression: must NOT claim foreign STCG is taxed
    # under Section 111 (which is about RPF accumulated
    # balances per the Income-tax Act, 1961).
    assert not re.search(r"\bunder Section 111\b(?!A)", advisory)
    assert "Item 5" in advisory
    assert "Section 111A row" in advisory


def test_advisory_strings_csv_safe() -> None:
    """Defense-in-depth: the advisories are CLI-stderr-only strings,
    not CSV cells, but a future maintainer might splice one of them
    into a CSV cell. Guard against the writer-contract formula-
    injection rule by confirming neither string starts with the
    leading characters the guard rejects (=, +, -, @, |) on any
    line (the leading '⚠' warning glyph is safe — it's a Unicode
    BMP character outside the C0-control / ASCII-formula set).
    """
    for advisory in (
        LTCG_SECTION_112_VS_112A_ADVISORY,
        STCG_SLAB_VS_111A_ADVISORY,
    ):
        for line in advisory.splitlines():
            if not line:
                continue
            assert not line.startswith(("=", "+", "-", "@", "|")), line
