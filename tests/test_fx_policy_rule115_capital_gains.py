"""Unit tests for Rule115CapitalGainsPolicy: distinct discriminator
from Rule115Policy while sharing the underlying algorithm."""

from __future__ import annotations

from datetime import date

import pytest

from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx.policy_rule115 import Rule115Policy
from ibkr_fa.fx.policy_rule115_capital_gains import Rule115CapitalGainsPolicy


def test_policy_kind_is_capital_gains_sub_rule() -> None:
    assert Rule115CapitalGainsPolicy.POLICY_KIND == "Rule 115(1), Explanation (2)(f)"
    assert Rule115Policy.POLICY_KIND == "Rule 115(1), Explanation (2)(e)"


def test_rate_for_returns_same_value_as_parent_policy_for_known_date() -> None:
    """The math is identical between the two policies — only the
    rendered ``policy_kind`` differs."""
    cg = Rule115CapitalGainsPolicy()
    div = Rule115Policy()
    sample_date = date(2025, 3, 15)
    assert cg.rate_for(sample_date) == div.rate_for(sample_date)


def test_missing_rate_error_carries_capital_gains_event_noun() -> None:
    """Force a missing-rate failure on a date that's far before the
    bundled archive; the raised :class:`MissingFxRateError` should
    name the capital-gains sub-rule and event-noun."""
    cg = Rule115CapitalGainsPolicy(max_carry_forward_days=0)
    far_past = date(1900, 1, 15)
    with pytest.raises(MissingFxRateError) as exc:
        cg.rate_for(far_past)
    msg = str(exc.value)
    assert msg == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(f) "
        "(preceding-month-end for a capital-asset transfer)]; no prior rate available "
        "in the bundled archive. The bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )
    # Neither the transfer date nor its specified date (#1047).
    assert "1900-01-15" not in msg
    assert "1899-12-31" not in msg


# ============================================================================
# Issue #207: AY-aware policy_kind_str
# ============================================================================


def test_policy_kind_str_default_returns_rule_115_explanation_2f() -> None:
    assert Rule115CapitalGainsPolicy().policy_kind_str() == "Rule 115(1), Explanation (2)(f)"


def test_policy_kind_str_ay_2027_returns_rule_206_sl_no_6() -> None:
    """AY 2027-28 Rule 206(2)(b)(i) Sl. No. 6 capital-gains
    absorption (Income-tax Rules, 2026).
    """
    assert (
        Rule115CapitalGainsPolicy(assessment_year_start=2027).policy_kind_str()
        == "Rule 206(2)(b)(i) Sl. No. 6"
    )


def test_missing_fx_rate_error_under_ay_2027_cites_rule_206_sl_no_6() -> None:
    """End-to-end: a failed CG lookup under
    ``assessment_year_start=2027`` raises MissingFxRateError whose
    ``policy_kind`` discriminator carries the Rule 206 Sl. No. 6
    citation (not Sl. No. 5 — that's dividends).
    """
    cg = Rule115CapitalGainsPolicy(assessment_year_start=2027, max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc:
        cg.rate_for(date(1900, 1, 15))
    assert exc.value.policy_kind is not None
    assert "Rule 206(2)(b)(i) Sl. No. 6" in exc.value.policy_kind
    assert "Rule 115(1), Explanation (2)(f)" not in exc.value.policy_kind
    assert "capital-asset transfer" in exc.value.policy_kind
