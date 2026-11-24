"""Tests for the Rule 128(5)(ii) Form 67 / FTC FX policy (:class:`Rule128Policy`)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx import (
    DEFAULT_MAX_CARRY_FORWARD_DAYS,
    AuditedFxPolicy,
    FxPolicy,
    Rule115Policy,
    Rule128Policy,
)


@pytest.fixture
def policy() -> Rule128Policy:
    return Rule128Policy()


# ============================================================================
# Constructor / wiring
# ============================================================================


def test_default_max_carry_forward_days_matches_shared_constant(
    policy: Rule128Policy,
) -> None:
    assert policy.max_carry_forward_days == DEFAULT_MAX_CARRY_FORWARD_DAYS == 14


def test_explicit_max_carry_forward_days_propagates() -> None:
    p = Rule128Policy(max_carry_forward_days=5)
    assert p.max_carry_forward_days == 5


def test_currency_classvar_is_usd() -> None:
    assert Rule128Policy.currency == "USD"


def test_negative_max_carry_forward_days_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Rule128Policy(max_carry_forward_days=-1)


def test_satisfies_fxpolicy_protocol() -> None:
    p: FxPolicy = Rule128Policy()
    assert p.currency == "USD"
    assert isinstance(p.rate_for(date(2025, 5, 17)), Decimal)


def test_satisfies_audited_fx_policy_protocol() -> None:
    """Rule 128 (like Rule 115) supports the audited-rate protocol so
    a future Form 67 / Schedule TR review writer can surface
    (specified_date, rate_date) pairs that match an actual SBI
    archive row (#160-style audit-trail)."""
    p: AuditedFxPolicy = Rule128Policy()
    resolution = p.rate_for_audited(date(2025, 5, 17))
    assert resolution.rate > 0
    assert resolution.specified_date == date(2025, 4, 30)
    assert resolution.rate_date == date(2025, 4, 30)


def test_is_rule115_policy_subclass() -> None:
    """Rule 128(5)(ii) shares the preceding-month-end algorithm with
    Rule 115(1), Explanation (2)(e). Inheriting from :class:`Rule115Policy` reuses the
    SBI TTBR archive + carry-back logic + sanity bounds without
    duplication. Locks the inheritance so a future refactor doesn't
    accidentally split the implementations and let one drift from
    the other."""
    policy = Rule128Policy()
    assert isinstance(policy, Rule115Policy)


# ============================================================================
# POLICY_KIND citation
# ============================================================================


def test_policy_kind_cites_rule_128_5_ii() -> None:
    """Rule 128(5)(ii) is the correct sub-clause for FTC FX
    conversion (verified against
    https://indiankanoon.org/doc/82134701/). NOT Rule 26 (TDS
    deducted by an Indian payer on payments TO a non-resident —
    different rule entirely, also "preceding-day" not
    "preceding-month-end"), and NOT Rule 128's other sub-clauses
    (which deal with FTC eligibility / aggregation, not FX)."""
    assert Rule128Policy.POLICY_KIND == "Rule 128(5)(ii)"


def test_policy_kind_overrides_rule_115_parent() -> None:
    """Subclass must override the parent's POLICY_KIND so any
    MissingFxRateError raised on an FTC rate lookup cites Rule 128,
    not Rule 115. Defence-in-depth against a future refactor that
    drops the override."""
    assert Rule128Policy.POLICY_KIND != Rule115Policy.POLICY_KIND


def test_policy_kind_for_event_renders_foreign_tax_noun() -> None:
    """The rendered policy_kind_for cites the foreign-tax-paid noun
    (not the parent's "a dividend" noun) so a MissingFxRateError
    on an FTC rate lookup reads correctly for the auditor. It names no
    date: the tax-paid date is input (#1047)."""
    rendered = Rule128Policy()._policy_kind_for()
    assert rendered == "Rule 128(5)(ii) (preceding-month-end for foreign tax paid)"


# ============================================================================
# Preceding-month-end resolution — exact hits, no carry-back
# ============================================================================


def test_month_end_exact_hit(policy: Rule128Policy) -> None:
    """Event 2025-05-17 → specified 2025-04-30 (Wed, published)."""
    # 2025-04-30 TT BUY = 84.25 per the bundled SBI snapshot.
    assert policy.rate_for(date(2025, 5, 17)) == Decimal("84.25")


def test_january_event_rolls_to_prior_december(policy: Rule128Policy) -> None:
    """Year-boundary lookup: Event 2025-01-15 → specified
    2024-12-31 (published). Locks the calendar-month rollover so
    the inherited :meth:`_preceding_month_end` doesn't quietly
    return 2025-12-31 instead.
    """
    # 2024-12-31 TT BUY = 85.20 per the bundled snapshot.
    assert policy.rate_for(date(2025, 1, 15)) == Decimal("85.20")


# ============================================================================
# Carry-back behaviour (inherited from Rule115Policy)
# ============================================================================


def test_carry_back_when_month_end_unpublished(policy: Rule128Policy) -> None:
    """Event 2025-09-15 → specified 2025-08-31 (Sun, absent) →
    carries back 1 day to 2025-08-30 (Sat, present). Same
    inherited carry-back semantics as Rule115Policy."""
    # 2025-08-30 TT BUY = 87.7 per the bundled snapshot.
    assert policy.rate_for(date(2025, 9, 15)) == Decimal("87.7")


def test_same_month_events_resolve_to_same_specified_date(
    policy: Rule128Policy,
) -> None:
    """Two events in the same calendar month resolve to the same
    preceding-month-end specified date and therefore the same rate.
    Locks the month-granular semantic of Rule 128(5)(ii)."""
    rate_early = policy.rate_for(date(2025, 5, 10))
    rate_late = policy.rate_for(date(2025, 5, 25))
    assert rate_early == rate_late == Decimal("84.25")


# ============================================================================
# Error cases — error message must cite Rule 128, not Rule 115
# ============================================================================


def test_missing_fx_rate_error_cites_rule_128(policy: Rule128Policy) -> None:
    """When the FTC FX lookup fails (specified date predates the
    archive), the raised :class:`MissingFxRateError` must cite
    Rule 128(5)(ii) in its policy_kind field — NOT inherit the
    parent's Rule 115(1), Explanation (2)(e) tag. This is what differentiates an
    FTC failure from a dividend-OS failure in a user-facing error.
    """
    with pytest.raises(MissingFxRateError) as exc_info:
        # An event whose preceding-month-end predates the bundled archive.
        policy.rate_for(date(2000, 1, 15))
    err = exc_info.value
    assert err.currency == "USD"
    assert "Rule 128(5)(ii)" in str(err)
    assert "foreign tax paid" in str(err)
    assert "Rule 115" not in str(err)
    # Neither the tax-paid date nor its specified date (#1047).
    assert "2000-01-15" not in str(err)
    assert "1999-12-31" not in str(err)


def test_stale_beyond_max_days_cites_rule_128() -> None:
    """The carry-back-exhausted error path also cites Rule 128. Use
    cap=0 + an event whose preceding-month-end is known-absent
    (2025-09 → 2025-08-31, Sunday, absent from the archive)."""
    p = Rule128Policy(max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(date(2025, 9, 15))
    err = exc_info.value
    assert str(err) == (
        "MissingFxRateError: no USD rate available [Rule 128(5)(ii) (preceding-month-end "
        "for foreign tax paid)]; latest available prior rate is 2025-08-30, beyond the "
        "carry-forward limit of 0 days. The bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )
    # Neither the tax-paid date nor its specified date (#1047).
    assert "2025-09-15" not in str(err)
    assert "2025-08-31" not in str(err)


# ============================================================================
# Documented behaviour: SAME rate as Rule 115 for the same event date
# ============================================================================


def test_rule_128_and_rule_115_return_same_rate_for_same_event_date() -> None:
    """Pin the algorithmic equivalence: Rule 128(5)(ii) and
    Rule 115(1), Explanation (2)(e), BOTH use the
    preceding-calendar-month-end of the event date with the same
    carry-back, so they MUST return the same rate for the same event
    date. This is the algorithmic guarantee that justifies the
    subclass-not-duplication implementation choice.

    A future change to Rule 115's algorithm would automatically
    propagate to Rule 128 — desired. A future change that intends
    to differentiate them would intentionally break this test, and
    THEN the implementation should fork the classes."""
    r115 = Rule115Policy()
    r128 = Rule128Policy()
    for event_date in (
        date(2025, 5, 17),  # exact hit
        date(2025, 1, 15),  # year-boundary
        date(2025, 9, 15),  # carry-back
        date(2024, 3, 15),  # leap year
    ):
        assert r115.rate_for(event_date) == r128.rate_for(event_date), (
            f"Rule 115 and Rule 128 must agree on {event_date}"
        )


# ============================================================================
# Public API
# ============================================================================


def test_exported_from_fx_package() -> None:
    """Rule128Policy must be re-exported from ``ibkr_fa.fx`` so
    callers can import it via the same convenient path as the other
    policies. Without this, the Form 67 / FSI / TR wiring would
    have to reach into the private submodule."""
    from ibkr_fa import fx  # noqa: PLC0415

    assert "Rule128Policy" in fx.__all__
    assert fx.Rule128Policy is Rule128Policy


# ============================================================================
# Issue #207: AY-aware policy_kind_str
# ============================================================================


def test_policy_kind_str_default_returns_legacy_rule_128_5ii() -> None:
    assert Rule128Policy().policy_kind_str() == "Rule 128(5)(ii)"


def test_policy_kind_str_ay_2027_returns_rule_76_5ii() -> None:
    """AY 2027-28 Rule 76(7)(b) absorption (Income-tax Rules, 2026)."""
    assert Rule128Policy(assessment_year_start=2027).policy_kind_str() == "Rule 76(7)(b)"


def test_missing_fx_rate_error_under_ay_2027_cites_rule_76() -> None:
    """End-to-end: a failed lookup under
    ``assessment_year_start=2027`` raises MissingFxRateError whose
    ``policy_kind`` discriminator carries the Rule 76 citation.
    """
    from datetime import date  # noqa: PLC0415

    import pytest  # noqa: PLC0415

    from ibkr_fa.exceptions import MissingFxRateError  # noqa: PLC0415

    p = Rule128Policy(assessment_year_start=2027, max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc:
        p.rate_for(date(1900, 1, 15))
    assert exc.value.policy_kind is not None
    assert "Rule 76(7)(b)" in exc.value.policy_kind
    assert "Rule 128(5)(ii)" not in exc.value.policy_kind
    # The event noun must still cite "foreign tax paid" (NOT
    # "dividend paid") — the parent Rule115Policy's noun was
    # overridden in :meth:`_policy_kind_for`.
    assert "foreign tax paid" in exc.value.policy_kind
