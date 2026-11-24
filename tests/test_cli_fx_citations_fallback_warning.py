"""Tests for :func:`ibkr_fa.cli._fx_citations_fallback_warning_lines`
(issue #207).

The helper emits a stderr advisory when an FxPolicy was constructed
with an ``assessment_year_start`` that has no registered citation
snapshot AND the +1 forward-compat fallback rule directed the lookup
to the latest registered snapshot. Mirrors the sister
:func:`_portal_labels_only_fallback_warning_lines` helper for the
output-side portal-labels snapshot.
"""

from __future__ import annotations

from ibkr_fa.cli import (
    _fx_citations_fallback_warning_lines,
    _portal_labels_fallback_warning_lines,
    _portal_labels_only_fallback_warning_lines,
)

# ============================================================================
# _fx_citations_fallback_warning_lines
# ============================================================================


def test_no_advisory_for_registered_ay_2026() -> None:
    """AY 2026-27 has its own snapshot — no fallback, no advisory."""
    assert _fx_citations_fallback_warning_lines(assessment_year_start=2026) == []


def test_no_advisory_for_registered_ay_2027() -> None:
    """AY 2027-28 has its own snapshot — no fallback, no advisory."""
    assert _fx_citations_fallback_warning_lines(assessment_year_start=2027) == []


def test_advisory_fires_for_ay_2028_forward_compat_fallback() -> None:
    """AY 2028 triggers the +1 forward-compat fallback. The advisory
    text is DYNAMIC: it names both the requested AY (2028-29) and
    the fallback snapshot AY (2027-28). Mirrors RubberDuck #2 from
    the issue #207 plan critique.
    """
    lines = _fx_citations_fallback_warning_lines(assessment_year_start=2028)
    assert len(lines) == 1
    advisory = lines[0]
    assert "AY 2028-29" in advisory
    assert "(AY 2027-28)" in advisory
    assert "NOT AY 2028-29 rules" in advisory
    assert "https://github.com/jay-tau/ibkr-fa/issues/207" in advisory


def test_no_advisory_for_ay_too_old() -> None:
    """AY 2025-26 (older than the earliest registered 2026) is
    unsupported, not falling back — predicate returns False (no
    advisory). The corresponding ``for_ay(2025)`` would raise
    :class:`UnknownAssessmentYearError` at policy construction
    time and never reach the CLI renderer.
    """
    assert _fx_citations_fallback_warning_lines(assessment_year_start=2025) == []


def test_no_advisory_for_ay_too_new() -> None:
    """AY 2029 (latest + 2) is too far ahead for the +1 rule —
    same "unsupported, not falling back" rationale as too-old.
    """
    assert _fx_citations_fallback_warning_lines(assessment_year_start=2029) == []


# ============================================================================
# Combined helper: _portal_labels_fallback_warning_lines
# ============================================================================


def test_combined_helper_emits_both_when_both_fall_back() -> None:
    """When both portal labels AND FX citations would fall back
    (i.e., asking for AY 2028 where neither has a registered
    snapshot), the combined helper concatenates both advisory
    blocks.
    """
    lines = _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=True,
        portal_labels_assessment_year_start=2028,
    )
    assert len(lines) == 2  # one portal advisory + one FX advisory
    portal_msg, fx_msg = lines
    assert "Portal labels for AY 2028-29" in portal_msg
    assert "issues/65" in portal_msg
    assert "FX-policy citations for AY 2028-29" in fx_msg
    assert "issues/207" in fx_msg


def test_combined_helper_omits_portal_block_when_portal_did_not_fall_back() -> None:
    """For AY 2028, FX citations fall back (since I just registered
    only 2026 + 2027), but the writer may have a registered portal-
    labels snapshot for 2028 already. In that case the FX advisory
    still fires; the portal block is omitted.
    """
    lines = _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=False,
        portal_labels_assessment_year_start=2028,
    )
    # Portal advisory is suppressed (no portal fallback) but FX
    # advisory still fires (FX always falls back for AY 2028).
    assert len(lines) == 1
    assert "FX-policy citations for AY 2028-29" in lines[0]


def test_combined_helper_emits_portal_only_when_fx_did_not_fall_back() -> None:
    """For AY 2027 (where FX has its own snapshot) but portal
    labels are missing for that AY (so the writer fell back), only
    the portal advisory fires. Tests the asymmetric case.
    """
    lines = _portal_labels_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=True,
        portal_labels_assessment_year_start=2027,
    )
    assert len(lines) == 1
    assert "Portal labels for AY 2027-28" in lines[0]
    # No FX advisory — AY 2027 has its own snapshot.
    assert "FX-policy citations" not in lines[0]


def test_combined_helper_emits_nothing_when_neither_falls_back() -> None:
    """The canonical no-op path: both subsystems have a registered
    snapshot for the requested AY. Default state for AY 2026-27
    and AY 2027-28 runs.
    """
    assert (
        _portal_labels_fallback_warning_lines(
            portal_labels_used_forward_compat_fallback=False,
            portal_labels_assessment_year_start=2026,
        )
        == []
    )
    assert (
        _portal_labels_fallback_warning_lines(
            portal_labels_used_forward_compat_fallback=False,
            portal_labels_assessment_year_start=2027,
        )
        == []
    )


# ============================================================================
# _portal_labels_only_fallback_warning_lines (extracted helper)
# ============================================================================


def test_portal_only_helper_returns_portal_advisory_isolation() -> None:
    """The extracted ``_portal_labels_only_fallback_warning_lines``
    helper returns exactly the portal advisory in isolation — used
    by tests that want to assert portal-fallback behaviour without
    the FX advisory side-effect.
    """
    lines = _portal_labels_only_fallback_warning_lines(
        portal_labels_used_forward_compat_fallback=True,
        portal_labels_assessment_year_start=2028,
    )
    assert len(lines) == 1
    assert "Portal labels for AY 2028-29" in lines[0]
    assert "FX-policy citations" not in lines[0]


def test_portal_only_helper_empty_when_no_fallback() -> None:
    assert (
        _portal_labels_only_fallback_warning_lines(
            portal_labels_used_forward_compat_fallback=False,
            portal_labels_assessment_year_start=2028,
        )
        == []
    )
