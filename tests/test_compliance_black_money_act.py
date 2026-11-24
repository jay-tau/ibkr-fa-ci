"""Tests for :mod:`ibkr_fa.compliance.black_money_act` (#63)."""

from __future__ import annotations

import re

from ibkr_fa.compliance import BMA_DISCLOSURE_ADVISORY


def test_advisory_names_the_actual_statute() -> None:
    """The advisory must name the actual statute by full title so a
    user searching for the citation can find it without prior
    familiarity.
    """
    assert "Black Money" in BMA_DISCLOSURE_ADVISORY
    assert "Undisclosed Foreign Income and Assets" in BMA_DISCLOSURE_ADVISORY
    assert "Imposition of Tax" in BMA_DISCLOSURE_ADVISORY
    assert "2015" in BMA_DISCLOSURE_ADVISORY


def test_advisory_names_concrete_penalty_amounts() -> None:
    """The penalty figures must be specified concretely so a user
    underestimating the disclosure obligation sees the real cost.
    The statutorily-correct sections (T1-r1 fix; PR #181 T5 P2
    update; PR #181 T1-r2 correctness Bill→Act P2 update):

    * Section 3 — flat 30% tax on undisclosed income/asset value.
    * Section 41 — 300% penalty of the §3 tax.
    * Section 43 — flat ₹10 lakh penalty for failure to disclose
      in a furnished return (the ibkr-fa hook), subject to the
      ₹20 lakh aggregate-foreign-asset (other than immovable
      property) carve-out introduced by Finance (No.2) Act, 2024
      effective 1 October 2024 (replacing the earlier ₹5 lakh
      foreign-bank-only carve-out). The carve-out is a
      peak-balance test against the previous year (the statutory
      proviso reads "at any time during the relevant previous
      year"), not a year-end snapshot against the AY.
    * Section 50 — 6 mo to 7 yr rigorous imprisonment for wilful
      non-disclosure in a furnished return. Matching ₹20 lakh
      aggregate-foreign-asset (other than immovable property)
      carve-out enacted by Finance Act, 2026 (Act 4 of 2026;
      Presidential assent 30 March 2026), deemed to have been
      inserted with effect from 1 October 2024.

    Earlier draft cited §41 / §50 / §51 incorrectly (those are
    300%-tax-penalty / criminal-prosecution / 3-10-yr-tax-evasion
    respectively, not the disclosure-failure penalties this
    advisory is about); T1-r1 fixed the citations. PR #181 T5 P2
    updated the carve-out scope from ₹5L bank-only to ₹20L all-
    non-immovable-property per Finance (No.2) Act, 2024. PR #181
    T1-r2 correctness P2 updated "Finance Bill, 2026 ... not yet
    enacted" → "Finance Act, 2026 ... deemed inserted with effect
    from 1 October 2024" because the Bill received Presidential
    assent 30 March 2026 as Act 4 of 2026.
    """
    assert "₹10 lakh" in BMA_DISCLOSURE_ADVISORY
    assert "Section 3" in BMA_DISCLOSURE_ADVISORY
    assert "Section 41" in BMA_DISCLOSURE_ADVISORY
    assert "Section 43" in BMA_DISCLOSURE_ADVISORY
    assert "Section 50" in BMA_DISCLOSURE_ADVISORY
    assert "6 months to 7 years" in BMA_DISCLOSURE_ADVISORY
    assert "30%" in BMA_DISCLOSURE_ADVISORY
    assert "300%" in BMA_DISCLOSURE_ADVISORY
    # ₹20 lakh aggregate carve-out for foreign assets other than
    # immovable property — Finance (No.2) Act, 2024, effective
    # 1 October 2024 (PR #181 T5 P2).
    assert "₹20 lakh" in BMA_DISCLOSURE_ADVISORY
    assert "Finance (No.2) Act, 2024" in BMA_DISCLOSURE_ADVISORY
    assert "1 October 2024" in BMA_DISCLOSURE_ADVISORY
    # PR #181 T1-r2 correctness P2: §50 carve-out is enacted, not
    # proposed — Finance Act, 2026 (Act 4 of 2026), 30 March 2026.
    assert "Finance Act, 2026" in BMA_DISCLOSURE_ADVISORY
    assert "Act 4 of 2026" in BMA_DISCLOSURE_ADVISORY
    assert "30 March 2026" in BMA_DISCLOSURE_ADVISORY
    # PR #181 T1-r2 correctness P3: "other than immovable property"
    # scope must be locked; peak-balance / previous-year framing
    # must be locked (the statutory test is the PY peak, not the
    # AY year-end snapshot).
    assert "other than immovable property" in BMA_DISCLOSURE_ADVISORY
    assert "at any time" in BMA_DISCLOSURE_ADVISORY
    assert "previous year" in BMA_DISCLOSURE_ADVISORY
    # Anti-regression: must NOT cite Section 51 (which is about
    # wilful tax evasion at 3-10 yr, not disclosure failure).
    assert not re.search(r"\bSection 51\b", BMA_DISCLOSURE_ADVISORY)
    # Anti-regression: the obsolete ₹5 lakh bank-only carve-out must
    # NOT appear in the live advisory (superseded by Finance (No.2)
    # Act, 2024). The string may still appear in module docstring
    # historical context but not in the runtime advisory text.
    assert "₹5 lakh" not in BMA_DISCLOSURE_ADVISORY
    # PR #181 T1-r2 correctness P2 anti-regression: must NOT cite
    # "Finance Bill, 2026" (stale; the Bill is now Finance Act, 2026
    # since 30 March 2026). The module docstring may still mention
    # the Bill→Act transition for historical clarity but the live
    # runtime advisory must reference the enacted Act.
    assert "Finance Bill, 2026" not in BMA_DISCLOSURE_ADVISORY
    assert "not yet enacted" not in BMA_DISCLOSURE_ADVISORY
    # PR #181 T1-r2 correctness P3 hardening: ordering / association
    # lock — the three §43 claims (statute name, threshold, scope)
    # must appear in that order and be associated, not floating
    # independently.
    assert re.search(
        r"Finance \(No\.2\) Act, 2024.*?₹20 lakh.*?other than immovable property",
        BMA_DISCLOSURE_ADVISORY,
        re.DOTALL,
    )
    # PR #181 T1-r2 correctness P3 hardening: same for the §50
    # Finance Act, 2026 enactment.
    assert re.search(
        r"Finance Act, 2026.*?Section 50.*?1 October 2024",
        BMA_DISCLOSURE_ADVISORY,
        re.DOTALL,
    )


def test_advisory_disclaims_tool_role_and_prompts_ca_consultation() -> None:
    """The advisory must make explicit that ibkr-fa produces the
    *mechanically correct* disclosure but the *decision to file* is
    the user's, and direct them at a CA familiar with foreign-asset
    disclosure for the actual filing decision.
    """
    assert "mechanically correct" in BMA_DISCLOSURE_ADVISORY
    assert "decision to file" in BMA_DISCLOSURE_ADVISORY
    assert "CA" in BMA_DISCLOSURE_ADVISORY


def test_advisory_documents_rnor_carve_out() -> None:
    """The Resident-but-Not-Ordinarily-Resident (RNOR) carve-out is
    the one common case where the disclosure obligation does NOT
    apply — naming it explicitly prevents a confused RNOR user from
    treating the advisory as binding when it isn't.
    """
    assert "RNOR" in BMA_DISCLOSURE_ADVISORY
    assert "resident-but-not-ordinarily-resident" in BMA_DISCLOSURE_ADVISORY


def test_advisory_states_non_quietable_design() -> None:
    """The advisory should self-document that --quiet does not
    suppress it (matching the §111A / §112A advisory pattern from
    PR #176). This sets the user's expectation correctly.
    """
    assert "--quiet" in BMA_DISCLOSURE_ADVISORY
    assert "non-quietable" in BMA_DISCLOSURE_ADVISORY


def test_advisory_csv_safe_no_formula_injection_triggers() -> None:
    """Defense-in-depth: the advisory is CLI-only, not a CSV cell,
    but a future maintainer might splice it into a CSV. Guard
    against the writer-contract formula-injection rule by
    confirming no line starts with =, +, -, @, or |.
    """
    for line in BMA_DISCLOSURE_ADVISORY.splitlines():
        if not line:
            continue
        assert not line.startswith(("=", "+", "-", "@", "|")), line
