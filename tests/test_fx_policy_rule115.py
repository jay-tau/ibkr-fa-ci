"""Tests for the Rule 115(1), Explanation (2)(e) FX policy (Rule115Policy)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.fx import (
    DEFAULT_MAX_CARRY_FORWARD_DAYS,
    AuditedFxPolicy,
    FxPolicy,
    Rule115Policy,
    Rule115Resolution,
)
from ibkr_fa.fx.ttbr_loader import SANE_RATE_LOWER, SANE_RATE_UPPER, load_ttbr
from tests._crash_reports import assert_prints_no_marker


@pytest.fixture
def policy() -> Rule115Policy:
    return Rule115Policy()


# ============================================================================
# Constructor / wiring
# ============================================================================


def test_default_max_carry_forward_days_matches_shared_constant(
    policy: Rule115Policy,
) -> None:
    assert policy.max_carry_forward_days == DEFAULT_MAX_CARRY_FORWARD_DAYS == 14


def test_explicit_max_carry_forward_days_propagates() -> None:
    p = Rule115Policy(max_carry_forward_days=5)
    assert p.max_carry_forward_days == 5


def test_currency_classvar_is_usd() -> None:
    assert Rule115Policy.currency == "USD"


def test_negative_max_carry_forward_days_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        Rule115Policy(max_carry_forward_days=-1)
    assert str(exc.value) == "max_carry_forward_days must be non-negative"


def test_satisfies_fxpolicy_protocol() -> None:
    p: FxPolicy = Rule115Policy()
    assert p.currency == "USD"
    assert isinstance(p.rate_for(date(2025, 5, 17)), Decimal)


# ============================================================================
# Preceding-month-end resolution — exact hits, no carry-back
# ============================================================================


def test_month_end_exact_hit(policy: Rule115Policy) -> None:
    """Event 2025-05-17 → specified 2025-04-30 (Wed, published)."""
    # 2025-04-30 TT BUY = 84.25 per the bundled SBI snapshot.
    assert policy.rate_for(date(2025, 5, 17)) == Decimal("84.25")


def test_year_boundary_january_rolls_to_prior_december(
    policy: Rule115Policy,
) -> None:
    """Event 2025-01-15 → specified 2024-12-31 (published)."""
    # 2024-12-31 TT BUY = 85.20 per the bundled snapshot.
    assert policy.rate_for(date(2025, 1, 15)) == Decimal("85.20")


def test_leap_year_february_twenty_nine(policy: Rule115Policy) -> None:
    """Event 2024-03-15 → specified 2024-02-29 (leap day, published)."""
    # 2024-02-29 TT BUY = 82.49 per the bundled snapshot.
    assert policy.rate_for(date(2024, 3, 15)) == Decimal("82.49")


def test_weekend_with_published_rate_no_carry_back(
    policy: Rule115Policy,
) -> None:
    """Negative control: 2026-01-31 is a Saturday but DOES have a
    published rate, so February-2026 events must NOT carry back.

    Guards against test over-fit that always assumes Sat/Sun → carry.
    """
    # 2026-01-31 TT BUY = 91.35 — exact hit, no carry-back.
    assert policy.rate_for(date(2026, 2, 15)) == Decimal("91.35")


# ============================================================================
# Carry-back — single day (weekend gap)
# ============================================================================


def test_carry_back_one_day_sunday_to_saturday_august(
    policy: Rule115Policy,
) -> None:
    """Event 2025-09-15 → specified 2025-08-31 (Sun, absent) →
    carries back 1 day to 2025-08-30 (Sat, present)."""
    # 2025-08-30 TT BUY = 87.7.
    assert policy.rate_for(date(2025, 9, 15)) == Decimal("87.7")


def test_carry_back_one_day_sunday_to_saturday_november(
    policy: Rule115Policy,
) -> None:
    """Event 2025-12-15 → specified 2025-11-30 (Sun, absent) →
    carries back 1 day to 2025-11-29 (Sat, present)."""
    # 2025-11-29 TT BUY = 88.95.
    assert policy.rate_for(date(2025, 12, 15)) == Decimal("88.95")


def test_carry_back_one_day_saturday_to_friday_february_2026(
    policy: Rule115Policy,
) -> None:
    """Event 2026-03-15 → specified 2026-02-28 (Sat, absent) →
    carries back 1 day to 2026-02-27 (Fri, present)."""
    # 2026-02-27 TT BUY = 90.56.
    assert policy.rate_for(date(2026, 3, 15)) == Decimal("90.56")


# ============================================================================
# Carry-back — multi-day (real weekday gap in the archive)
# ============================================================================


def test_carry_back_two_days_weekday_gap_april_2025(
    policy: Rule115Policy,
) -> None:
    """Event 2025-04-01 → specified 2025-03-31 (Mon, absent) →
    2025-03-30 (Sun, absent) → 2025-03-29 (Sat, present). 2-day
    carry-back across a real Mon+Sun weekday gap in the archive."""
    # 2025-03-29 TT BUY = 85.1.
    assert policy.rate_for(date(2025, 4, 1)) == Decimal("85.1")


# ============================================================================
# Same-month events resolve to the same lookup
# ============================================================================


def test_same_month_events_resolve_to_same_specified_date(
    policy: Rule115Policy,
) -> None:
    """Two May-2025 events both resolve to specified 2025-04-30 and
    therefore the same rate. Locks the rule's month-granular
    semantic and indirectly exercises the (year, month) PME cache."""
    rate_early = policy.rate_for(date(2025, 5, 10))
    rate_late = policy.rate_for(date(2025, 5, 25))
    assert rate_early == rate_late == Decimal("84.25")


# ============================================================================
# Error cases
# ============================================================================


# An event very early in the bundled archive's coverage gap, kept off the lines
# a traceback prints so that the traceback check tests the error.
_PRE_ARCHIVE_EVENT = date(2000, 1, 15)


def test_pre_archive_raises_missing_fx_rate(policy: Rule115Policy) -> None:
    """Event whose specified date predates the earliest archive row
    raises MissingFxRateError with last_available=None. The error keeps
    neither the event's date nor the month-end before it (#1368)."""
    with pytest.raises(MissingFxRateError) as exc_info:
        policy.rate_for(_PRE_ARCHIVE_EVENT)
    err = exc_info.value
    assert err.currency == "USD"
    assert err.last_available is None
    assert "no prior rate available" in str(err)
    assert_prints_no_marker(
        err, markers=("2000-01-15", "2000, 1, 15", "1999-12-31", "1999, 12, 31")
    )


def test_stale_beyond_max_days_raises_with_zero_cap() -> None:
    """With cap=0, even a 1-day carry-back must fail. Use a month
    whose preceding-month-end is known-absent (2025-09 → 2025-08-31,
    Sunday, absent from the archive)."""
    p = Rule115Policy(max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(date(2025, 9, 15))
    err = exc_info.value
    assert err.last_available == date(2025, 8, 30)
    assert err.max_carry_forward_days == 0
    # The date the policy looked up: a 1-day cap resolves it.
    resolved = Rule115Policy(max_carry_forward_days=1).rate_for_audited(date(2025, 9, 15))
    assert resolved.specified_date == date(2025, 8, 31)


def test_max_carry_ceiling_boundary() -> None:
    """cap=1 succeeds for the 1-day-back cases but FAILS for the
    2-day weekday-gap case; raising cap to 2 then succeeds."""
    p1 = Rule115Policy(max_carry_forward_days=1)
    # 1-day carry-back: August and November succeed at cap=1.
    assert p1.rate_for(date(2025, 9, 15)) == Decimal("87.7")
    assert p1.rate_for(date(2025, 12, 15)) == Decimal("88.95")
    # 2-day carry-back: April 2025 fails at cap=1.
    with pytest.raises(MissingFxRateError) as exc_info:
        p1.rate_for(date(2025, 4, 1))
    assert exc_info.value.last_available == date(2025, 3, 29)

    # cap=2 makes the same April event succeed, for the date cap=1 looked up.
    p2 = Rule115Policy(max_carry_forward_days=2)
    assert p2.rate_for(date(2025, 4, 1)) == Decimal("85.1")
    assert p2.rate_for_audited(date(2025, 4, 1)).specified_date == date(2025, 3, 31)


# #1368's example: an event date past the archive's end, kept off the lines a
# traceback prints so that the traceback check tests the error.
_MARKED_EVENT = date(2031, 8, 14)


def test_stale_refusal_keeps_no_specified_or_event_date() -> None:
    """#1368: the error keeps no requested date, so neither the event's date
    nor the month-end before it is in its ``str``, ``repr``, args, fields,
    traceback or chain. The expected message follows the live archive end, so
    a TTBR refresh doesn't break it (#301)."""
    archive_end = max(load_ttbr("USD"))
    with pytest.raises(MissingFxRateError) as exc_info:
        Rule115Policy().rate_for(_MARKED_EVENT)
    assert str(exc_info.value) == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(e) "
        "(preceding-month-end for a dividend)]; latest available prior rate is "
        f"{archive_end.isoformat()}, beyond the carry-forward limit of 14 days. The "
        "bundled TTBR archive may need updating; see https://github.com/jay-tau/ibkr-fa "
        "for the latest snapshot."
    )
    assert_prints_no_marker(
        exc_info.value, markers=("2031-07-31", "2031, 7, 31", "2031-08-14", "2031, 8, 14")
    )
    assert exc_info.value == MissingFxRateError(
        currency="USD",
        last_available=archive_end,
        max_carry_forward_days=14,
        policy_kind="Rule 115(1), Explanation (2)(e) (preceding-month-end for a dividend)",
    )


def test_missing_fx_rate_error_message_names_the_rule() -> None:
    """User-facing message must contain a Rule 115(1), Explanation (2)(e) discriminator
    so users can tell which policy failed. It names the event kind, a
    dividend, but not the payment date or the specified date derived
    from it, which are input (#1047)."""
    p = Rule115Policy(max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(date(2025, 9, 15))
    msg = str(exc_info.value)
    assert msg == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(e) "
        "(preceding-month-end for a dividend)]; latest available prior rate is "
        "2025-08-30, beyond the carry-forward limit of 0 days. The bundled TTBR archive "
        "may need updating; see https://github.com/jay-tau/ibkr-fa for the latest "
        "snapshot."
    )
    assert "2025-09-15" not in msg
    assert "2025-08-31" not in msg


def test_dividend_refusal_names_the_prior_rate_date_not_the_event_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1047: a Rule 115 dividend lookup that refuses names the latest
    prior public rate date, never the payment date, the specified
    date derived from it, or the day gap. With a 0-day cap any gap
    refuses; the default cap would carry 2025-03-29's rate forward."""
    rates = {date(2025, 3, 29): Decimal("85.1"), date(2026, 1, 1): Decimal("90.0")}
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)
    p = Rule115Policy(max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(date(2025, 4, 1))
    msg = str(exc_info.value)
    assert msg == (
        "MissingFxRateError: no USD rate available [Rule 115(1), Explanation (2)(e) "
        "(preceding-month-end for a dividend)]; latest available prior rate is "
        "2025-03-29, beyond the carry-forward limit of 0 days. The bundled TTBR archive "
        "may need updating; see https://github.com/jay-tau/ibkr-fa for the latest "
        "snapshot."
    )
    assert "2025-03-29" in msg
    # The payment date and its specified date (the preceding month-end).
    assert "2025-04-01" not in msg
    assert "2025-03-31" not in msg
    assert "2 days" not in msg


# ============================================================================
# Postcondition: sane rate bounds
# ============================================================================


def test_returned_rate_is_in_sane_bounds(policy: Rule115Policy) -> None:
    rate = policy.rate_for(date(2025, 5, 17))
    assert SANE_RATE_LOWER < rate <= SANE_RATE_UPPER


def test_sane_bounds_postcondition_fires_on_corrupted_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatch the in-memory rate map to inject an out-of-bounds
    value and assert the policy's postcondition raises InvariantError.

    Mirrors the equivalent guard in test_fx_policy_fa.py: the
    loader-level invariant already protects against this at parse
    time, but the per-call assertion locks the policy's own
    contract against future refactors (e.g., a cached patched value)
    that might bypass the loader.
    """
    p = Rule115Policy()
    # Inject a poisoned rate for 2025-04-30 (the specified date for
    # any May-2025 event). The value exceeds SANE_RATE_UPPER.
    poisoned = Decimal("999.99")
    monkeypatch.setitem(p._rates, date(2025, 4, 30), poisoned)
    with pytest.raises(InvariantError, match="postcondition"):
        p.rate_for(date(2025, 5, 17))


# ============================================================================
# rate_for_audited / Rule115Resolution (issue #160)
# ============================================================================


def test_rate_for_audited_no_carry_back_returns_specified_date_as_rate_date(
    policy: Rule115Policy,
) -> None:
    """Event 2025-06-15 → specified 2025-05-31 (Sat, published).
    No carry-back, so rate_date == specified_date.
    """

    resolution = policy.rate_for_audited(date(2025, 6, 15))
    assert isinstance(resolution, Rule115Resolution)
    assert resolution.specified_date == date(2025, 5, 31)
    assert resolution.rate_date == date(2025, 5, 31)
    # 2025-05-31 TT BUY = 85.10
    assert resolution.rate == Decimal("85.10")


def test_rate_for_audited_one_day_carry_back_surfaces_actual_publication_date(
    policy: Rule115Policy,
) -> None:
    """The Sep 15 / Aug 31 example from issue #160.

    Event 2025-09-15 → specified 2025-08-31 (Sun, NOT published) →
    carry-back to 2025-08-30 (Sat, published, TT BUY 87.70).
    rate_for_audited must surface the Aug 30 date so the auditor can
    independently verify the rate against the SBI archive.
    """

    resolution = policy.rate_for_audited(date(2025, 9, 15))
    assert isinstance(resolution, Rule115Resolution)
    assert resolution.specified_date == date(2025, 8, 31)
    assert resolution.rate_date == date(2025, 8, 30)
    assert resolution.rate == Decimal("87.70")


def test_rate_for_and_rate_for_audited_return_same_rate(
    policy: Rule115Policy,
) -> None:
    """rate_for must be exactly rate_for_audited(...).rate for every
    event date — they share the same lookup algorithm by
    construction; this regression-tests against a future refactor
    that desyncs the two paths.
    """
    sample_dates = [
        date(2025, 1, 15),  # Jan rolls to prior Dec 31
        date(2025, 5, 17),  # Apr 30 exact hit
        date(2025, 6, 15),  # May 31 Sat published, exact hit
        date(2025, 9, 15),  # Aug 31 Sun absent, carry-back to Aug 30
        date(2025, 12, 15),  # Nov 30 Sun absent, carry-back to Nov 29
        date(2026, 2, 15),  # Jan 31 Sat published, exact hit
    ]
    for event_date in sample_dates:
        assert policy.rate_for(event_date) == policy.rate_for_audited(event_date).rate


def test_rate_for_audited_raises_missing_when_carry_back_window_exhausted() -> None:
    """rate_for_audited must raise MissingFxRateError under the same
    conditions as rate_for (no rate within max_carry_forward_days).
    """
    p = Rule115Policy(max_carry_forward_days=1)
    # Pick a date whose specified-date carry-back would exceed 1 day.
    # 2025-04-01 → specified 2025-03-31 (Mon, absent) → 2025-03-30
    # (Sun, absent) → 2025-03-29 (Sat, present). Gap = 2 days > 1.
    with pytest.raises(MissingFxRateError):
        p.rate_for_audited(date(2025, 4, 1))


def test_rule115_resolution_rejects_rate_date_after_specified_date() -> None:
    """Rule115Resolution.__post_init__ enforces rate_date <=
    specified_date (carry-back is strictly backwards in time).
    """

    with pytest.raises(InvariantError, match="rate_date"):
        Rule115Resolution(
            rate=Decimal("85"),
            specified_date=date(2025, 5, 31),
            rate_date=date(2025, 6, 1),
        )


def test_rule115_resolution_rejects_non_positive_rate() -> None:

    with pytest.raises(InvariantError, match="rate"):
        Rule115Resolution(
            rate=Decimal("0"),
            specified_date=date(2025, 5, 31),
            rate_date=date(2025, 5, 31),
        )


def test_rule115_policy_satisfies_audited_fx_policy_protocol() -> None:
    """Locks the runtime-checkable AuditedFxPolicy contract.

    The Schedule OS / CG compute requires an AuditedFxPolicy and calls
    ``rate_for_audited`` on it (#611). If a future refactor removes or
    renames that method on :class:`Rule115Policy`, every run would
    raise. This one-liner catches that at test time rather than at
    next-real-data-run time. (T4-r1 finding.)
    """

    assert isinstance(Rule115Policy(), AuditedFxPolicy)


# ============================================================================
# Issue #207: AY-aware policy_kind_str + per-AY citation resolution
# ============================================================================
#
# Each FxPolicy class accepts an optional ``assessment_year_start``
# constructor kwarg (default None preserves legacy AY 2026-27
# wording). When set, :meth:`policy_kind_str` resolves the citation
# from the per-AY snapshot in :mod:`ibkr_fa.fx.policy_citations`.


def test_policy_kind_str_default_returns_legacy_rule_115_2e() -> None:
    """Default constructor (no ``assessment_year_start``) preserves
    AY 2026-27 ``"Rule 115(1), Explanation (2)(e)"`` — legacy callers see no change.
    """
    assert Rule115Policy().policy_kind_str() == "Rule 115(1), Explanation (2)(e)"


def test_policy_kind_str_ay_2026_returns_rule_115_2e() -> None:
    """Explicit AY 2026-27 returns the same legacy citation."""
    assert (
        Rule115Policy(assessment_year_start=2026).policy_kind_str()
        == "Rule 115(1), Explanation (2)(e)"
    )


def test_policy_kind_str_ay_2027_returns_rule_206_sl_no_5() -> None:
    """AY 2027-28 resolves the Rule 206(2)(b)(i) Sl. No. 5 absorption."""
    assert (
        Rule115Policy(assessment_year_start=2027).policy_kind_str() == "Rule 206(2)(b)(i) Sl. No. 5"
    )


def test_policy_kind_str_ay_2028_falls_back_to_ay_2027_snapshot() -> None:
    """AY 2028 (latest_registered + 1) falls back to AY 2027-28 via
    the +1 forward-compat rule. The CLI advisory at
    :func:`ibkr_fa.cli._fx_citations_fallback_warning_lines`
    surfaces the fallback to the user; the citation itself is the
    AY 2027-28 text.
    """
    assert (
        Rule115Policy(assessment_year_start=2028).policy_kind_str() == "Rule 206(2)(b)(i) Sl. No. 5"
    )


def test_policy_kind_for_ay_2027_renders_into_audit_trail() -> None:
    """The audit-trail string composed in :meth:`_policy_kind_for`
    uses :meth:`policy_kind_str` so the AY-aware citation flows
    through to :class:`MissingFxRateError.policy_kind` and the
    rendered error message.
    """
    p = Rule115Policy(assessment_year_start=2027)
    audit = p._policy_kind_for()
    assert audit == "Rule 206(2)(b)(i) Sl. No. 5 (preceding-month-end for a dividend)"


def test_policy_kind_for_default_keeps_legacy_audit_trail() -> None:
    """Default constructor renders the legacy AY 2026-27 citation
    and names the event kind, without the payment date (#1047).
    """
    p = Rule115Policy()
    audit = p._policy_kind_for()
    assert audit == "Rule 115(1), Explanation (2)(e) (preceding-month-end for a dividend)"


def test_missing_fx_rate_error_under_ay_2027_cites_rule_206() -> None:
    """End-to-end: a failed lookup under
    ``assessment_year_start=2027`` raises MissingFxRateError whose
    ``policy_kind`` discriminator carries the Rule 206 citation.
    """
    p = Rule115Policy(assessment_year_start=2027, max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc:
        p.rate_for(date(1900, 1, 15))
    assert exc.value.policy_kind is not None
    assert "Rule 206(2)(b)(i) Sl. No. 5" in exc.value.policy_kind
    assert "Rule 115(1), Explanation (2)(e)" not in exc.value.policy_kind


def test_require_sane_rate_under_ay_2027_cites_rule_206() -> None:
    """The sanity-postcondition error rendered by
    :meth:`_require_sane_rate` uses the AY-aware citation too.
    Mirrors the T3 codex-emulation finding from PR #188 r2 that
    locked the subclass-aware classmethod dispatch; the AY-aware
    instance dispatch is the same idea extended one more axis.
    """
    p = Rule115Policy(assessment_year_start=2027)
    # Force a sanity violation by passing a rate outside the
    # SANE_RATE bounds. _require_sane_rate raises InvariantError
    # with the policy citation in the ``what`` field.
    insane_rate = Decimal("0")
    with pytest.raises(InvariantError) as exc:
        p._require_sane_rate(insane_rate, event_date=date(2025, 5, 5))
    msg = str(exc.value)
    assert "Rule 206(2)(b)(i) Sl. No. 5" in msg
    assert "Rule 115(1), Explanation (2)(e)" not in msg


# ============================================================================
# Issue #207: Rule115OtherSourcesPolicy AY-aware subclass-footgun
# ============================================================================
#
# Rule115OtherSourcesPolicy overrides __init__ to take an extra
# ``previous_year_end`` arg. The AY-aware kwarg MUST pass through
# super().__init__ correctly — RubberDuck #8 flagged that a missing
# pass-through would silently leave the subclass legacy while the
# pipeline appears AY-aware.


def test_rule115_other_sources_default_returns_legacy_explanation_2c() -> None:
    """Default constructor (no AY kwarg) returns the legacy
    ``"Rule 115(1), Explanation (2)(c)"`` citation.
    """
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    p = Rule115OtherSourcesPolicy(previous_year_end=date(2026, 3, 31))
    assert p.policy_kind_str() == "Rule 115(1), Explanation (2)(c)"


def test_rule115_other_sources_ay_2027_maps_to_rule_206_sl_no_3() -> None:
    """The AY 2027-28 citation for "Other Sources" resolves cleanly
    to ``"Rule 206(2)(b)(i) Sl. No. 3"`` — the Table-verbatim
    FY-end absorption identified in
    ``docs/architecture.md`` "Terminology equivalence" subsection.
    No behavioural change is needed; ``Rule115OtherSourcesPolicy``'s
    existing FY-end logic applies verbatim under both regimes.

    Issue #252 / PR #255 corrected PR #253's initial mis-citation
    that pointed at Sl. No. 5 (dividends) with a "behavioural
    change pending" legacy marker.
    """
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    p = Rule115OtherSourcesPolicy(
        previous_year_end=date(2026, 3, 31),
        assessment_year_start=2027,
    )
    assert p.policy_kind_str() == "Rule 206(2)(b)(i) Sl. No. 3"


def test_rule115_other_sources_ay_2027_audit_trail_cites_rule_206_sl_no_3() -> None:
    """End-to-end: the audit-trail string (rendered by the
    overridden :meth:`_policy_kind_for`) cites the clean
    ``Rule 206(2)(b)(i) Sl. No. 3`` citation under AY 2027-28+
    with the FY-end specified-date wording preserved verbatim. That
    date comes from the FY; the interest's own date, which is input,
    is not printed (#1047).
    """
    from ibkr_fa.fx import Rule115OtherSourcesPolicy  # noqa: PLC0415

    p = Rule115OtherSourcesPolicy(
        previous_year_end=date(2026, 3, 31),
        assessment_year_start=2027,
    )
    audit = p._policy_kind_for()
    assert audit == (
        "Rule 206(2)(b)(i) Sl. No. 3 (last day of previous year 2026-03-31 for interest received)"
    )


def test_policy_kind_str_unsupported_ay_raises_unknown_assessment_year() -> None:
    """``policy_kind_str()`` propagates the
    :class:`~ibkr_fa.fx.policy_citations.UnknownAssessmentYearError`
    when the requested AY is outside the registered snapshots' +1
    forward-compat fallback window.

    T4 r1 of PR #253 noted this exception path is exercised only
    indirectly by the ``for_ay`` test suite — adding the direct
    end-to-end coverage closes the loop. AY 2029 = latest +2
    (registered: 2026, 2027) which is outside the +1 fallback.
    """
    from ibkr_fa.fx.policy_citations import UnknownAssessmentYearError  # noqa: PLC0415

    p = Rule115Policy(assessment_year_start=2029)
    with pytest.raises(UnknownAssessmentYearError) as exc_info:
        p.policy_kind_str()
    assert exc_info.value.assessment_year_start == 2029
