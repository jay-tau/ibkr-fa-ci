"""Tests for :mod:`ibkr_fa.fx.policy_citations` package (issue #207).

Mirrors :mod:`tests.test_portal_labels_is_forward_compat_fallback` for
the per-AY snapshot pattern (selector / fallback / unknown error).
The package itself is documented in
``src/ibkr_fa/fx/policy_citations/__init__.py``.
"""

from __future__ import annotations

import pytest

from ibkr_fa.fx import policy_citations
from ibkr_fa.fx.policy_citations import (
    CURRENT_AY_START,
    UnknownAssessmentYearError,
    _ay_2026_27,
    _ay_2027_28,
    for_ay,
    is_forward_compat_fallback,
    latest_registered_ay,
)

# ============================================================================
# Snapshot module identity
# ============================================================================


def test_for_ay_2026_returns_ay_2026_27_snapshot() -> None:
    assert for_ay(2026) is _ay_2026_27


def test_for_ay_2027_returns_ay_2027_28_snapshot() -> None:
    assert for_ay(2027) is _ay_2027_28


def test_current_ay_start_pinned_to_2026() -> None:
    """``CURRENT_AY_START`` MUST stay AY 2026-27 even after AY 2027-28
    is registered — switching the module-level re-exports default
    would silently re-cite Rule 206 for callers that haven't yet
    threaded the AY kwarg, regressing AY 2026-27 filing-season
    behaviour. See the package ``__init__.py`` docstring rationale.
    """
    assert CURRENT_AY_START == 2026


# ============================================================================
# AY 2026-27 snapshot — verbatim 1961-Act citations
# ============================================================================


def test_ay_2026_27_rule_115_2e_verbatim() -> None:
    assert _ay_2026_27.POLICY_KIND_RULE_115_2E == "Rule 115(1), Explanation (2)(e)"


def test_ay_2026_27_rule_115_2f_verbatim() -> None:
    assert _ay_2026_27.POLICY_KIND_RULE_115_2F == "Rule 115(1), Explanation (2)(f)"


def test_ay_2026_27_rule_115_explanation_2c_verbatim() -> None:
    assert _ay_2026_27.POLICY_KIND_RULE_115_EXPLANATION_2C == "Rule 115(1), Explanation (2)(c)"


def test_ay_2026_27_rule_128_5ii_verbatim() -> None:
    assert _ay_2026_27.POLICY_KIND_RULE_128_5II == "Rule 128(5)(ii)"


# ============================================================================
# AY 2027-28 snapshot — Rule 206 / 207 / 76 absorptions
# ============================================================================


def test_ay_2027_28_rule_115_2e_maps_to_rule_206_sl_no_5() -> None:
    """Rule 115(1), Explanation (2)(e) dividends absorbed into Rule 206(2)(b)(i)
    Sl. No. 5 verbatim per the Income-tax Rules, 2026 (G.S.R. 198(E)
    20-03-2026).
    """
    assert _ay_2027_28.POLICY_KIND_RULE_115_2E == "Rule 206(2)(b)(i) Sl. No. 5"


def test_ay_2027_28_rule_115_2f_maps_to_rule_206_sl_no_6() -> None:
    """Rule 115(1), Explanation (2)(f) capital-asset transfers absorbed into
    Rule 206(2)(b)(i) Sl. No. 6 verbatim.
    """
    assert _ay_2027_28.POLICY_KIND_RULE_115_2F == "Rule 206(2)(b)(i) Sl. No. 6"


def test_ay_2027_28_rule_115_explanation_2c_maps_to_rule_206_sl_no_3() -> None:
    """Rule 115(1), Explanation (2)(c) "Other Sources" residual is
    absorbed by **Rule 206(2)(b)(i) Sl. No. 3** under the
    Income-tax Rules, 2026 — NOT Sl. No. 5 (which is dividends).
    Sl. No. 3 covers "Income chargeable under... 'Income from
    other sources' (excluding dividends and interest on
    securities)" with the verbatim FY-end ("last day of the tax
    year of the assessee") specified date — equivalent to the
    1961-Act "last day of the previous year" for resident-
    individual assessees. ``Rule115OtherSourcesPolicy``'s existing
    FY-end logic applies verbatim — no behavioural change is
    needed, only the citation refresh.

    PR #253 of issue #207 initially mis-cited this as Sl. No. 5
    with a "behavioural change pending" legacy marker; issue #252
    / PR #255 corrected the citation. See
    :mod:`ibkr_fa.fx.policy_citations._ay_2027_28` module docstring
    + ``docs/architecture.md`` "Terminology equivalence" subsection
    for the verification trail.
    """
    assert _ay_2027_28.POLICY_KIND_RULE_115_EXPLANATION_2C == "Rule 206(2)(b)(i) Sl. No. 3"


def test_ay_2027_28_rule_128_5ii_maps_to_rule_76_5ii() -> None:
    """Rule 128(5)(ii) FTC FX absorbed into Rule 76(7)(b) verbatim
    per the Rules-2026.
    """
    assert _ay_2027_28.POLICY_KIND_RULE_128_5II == "Rule 76(7)(b)"


# ============================================================================
# is_forward_compat_fallback — 4-branch coverage
# ============================================================================


def test_is_forward_compat_fallback_registered_ay_returns_false() -> None:
    """AY 2026 has its own snapshot — no fallback."""
    assert is_forward_compat_fallback(2026) is False


def test_is_forward_compat_fallback_latest_registered_returns_false() -> None:
    """AY 2027 has its own snapshot — no fallback, even though it's
    the latest registered.
    """
    assert is_forward_compat_fallback(2027) is False


def test_is_forward_compat_fallback_plus_one_returns_true() -> None:
    """AY 2028 (latest + 1) triggers the +1 forward-compat fallback
    to the latest registered AY 2027 snapshot.
    """
    assert is_forward_compat_fallback(2028) is True


def test_is_forward_compat_fallback_plus_two_returns_false() -> None:
    """AY 2029 (latest + 2) is too far ahead for the +1 rule —
    ``for_ay`` would raise. The predicate returns False (not True)
    because the AY is unsupported, not falling back.
    """
    assert is_forward_compat_fallback(2029) is False


def test_is_forward_compat_fallback_older_than_earliest_returns_false() -> None:
    """AY 2025 (older than the earliest registered 2026) is
    unsupported — predicate returns False (not True).
    """
    assert is_forward_compat_fallback(2025) is False


# ============================================================================
# latest_registered_ay helper
# ============================================================================


def test_latest_registered_ay_returns_2027_after_207_ships() -> None:
    """Helper used by the CLI advisory to populate the dynamic
    "Using AY <latest>" message text. Pinned to 2027 in this PR;
    bumps when a new snapshot is registered.
    """
    assert latest_registered_ay() == 2027


# ============================================================================
# UnknownAssessmentYearError — for_ay raises
# ============================================================================


def test_for_ay_too_old_raises_unknown_assessment_year_error() -> None:
    with pytest.raises(UnknownAssessmentYearError) as exc_info:
        for_ay(2020)
    err = exc_info.value
    assert err.assessment_year_start == 2020
    assert err.earliest_registered == 2026
    assert err.latest_registered == 2027
    assert "2020-21" in str(err)
    assert "src/ibkr_fa/fx/policy_citations/_ay_2020_21.py" in str(err)


def test_for_ay_too_new_raises_unknown_assessment_year_error() -> None:
    """AY 2029 is +2 from the latest registered (2027) — outside the
    +1 forward-compat fallback window. Raises.
    """
    with pytest.raises(UnknownAssessmentYearError) as exc_info:
        for_ay(2029)
    err = exc_info.value
    assert err.assessment_year_start == 2029
    assert "2029-30" in str(err)


def test_for_ay_plus_one_returns_latest_does_not_raise() -> None:
    """AY 2028 hits the +1 forward-compat fallback — returns the
    latest registered snapshot (AY 2027-28) without raising.
    """
    snapshot = for_ay(2028)
    assert snapshot is _ay_2027_28
    # Reading a constant off the fallback snapshot gives the
    # AY 2027-28 citation, not an AY 2028-29 one.
    assert snapshot.POLICY_KIND_RULE_115_2E == "Rule 206(2)(b)(i) Sl. No. 5"


def test_unknown_assessment_year_error_inherits_lookup_error() -> None:
    """Per the docstring contract — legacy ``except LookupError``
    call sites must keep working.
    """
    with pytest.raises(LookupError):
        for_ay(2020)


# ============================================================================
# Module-level re-exports point at CURRENT_AY_START snapshot
# ============================================================================


def test_module_level_reexports_match_current_ay() -> None:
    """The module-level constants (the legacy import path) must
    track ``CURRENT_AY_START`` — i.e., always equal to the
    AY 2026-27 snapshot's constants in this PR.
    """
    assert policy_citations.POLICY_KIND_RULE_115_2E == _ay_2026_27.POLICY_KIND_RULE_115_2E
    assert policy_citations.POLICY_KIND_RULE_115_2F == _ay_2026_27.POLICY_KIND_RULE_115_2F
    assert (
        policy_citations.POLICY_KIND_RULE_115_EXPLANATION_2C
        == _ay_2026_27.POLICY_KIND_RULE_115_EXPLANATION_2C
    )
    assert policy_citations.POLICY_KIND_RULE_128_5II == _ay_2026_27.POLICY_KIND_RULE_128_5II
