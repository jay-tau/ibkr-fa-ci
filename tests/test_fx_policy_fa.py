"""Tests for the Schedule FA FX policy (FaExactDatePolicy)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx import DEFAULT_MAX_CARRY_FORWARD_DAYS, FaExactDatePolicy, FxPolicy
from ibkr_fa.fx.ttbr_loader import SANE_RATE_LOWER, SANE_RATE_UPPER, load_ttbr
from tests._crash_reports import assert_prints_no_marker


@pytest.fixture
def policy() -> FaExactDatePolicy:
    return FaExactDatePolicy()


def test_default_currency_is_usd(policy: FaExactDatePolicy) -> None:
    assert policy.currency == "USD"


def test_default_max_carry_forward_days_is_14(policy: FaExactDatePolicy) -> None:
    assert policy.max_carry_forward_days == DEFAULT_MAX_CARRY_FORWARD_DAYS == 14


def test_negative_max_carry_forward_days_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        FaExactDatePolicy(max_carry_forward_days=-1)
    assert str(exc.value) == "max_carry_forward_days must be non-negative"


def test_zero_max_carry_forward_days_disables_carry_forward() -> None:
    """With a 0-day cap, only exact hits succeed; any gap → error."""
    p = FaExactDatePolicy(max_carry_forward_days=0)
    # 2026-04-30 is in the archive — exact hit succeeds.
    assert p.rate_for(date(2026, 4, 30)) == Decimal("94.6")
    # 2025-01-05 is a Sunday — unpublished. Under cap=0, even a 1-day
    # carry-forward must fail.
    with pytest.raises(MissingFxRateError):
        p.rate_for(date(2025, 1, 5))


def test_exact_date_hit_returns_published_rate(policy: FaExactDatePolicy) -> None:
    """Pinned spot check: 2026-04-30 in the bundled snapshot."""
    assert policy.rate_for(date(2026, 4, 30)) == Decimal("94.6")


def test_carry_forward_for_weekend(policy: FaExactDatePolicy) -> None:
    """Sunday 2026-04-26 has no publication; carry forward Friday's rate."""
    sunday = date(2026, 4, 26)
    friday = date(2026, 4, 24)
    # Verify the assumption: Friday is in the archive, Sunday isn't.
    fri_rate = policy.rate_for(friday)
    sunday_rate = policy.rate_for(sunday)
    assert sunday_rate == fri_rate


def test_carry_forward_for_holiday(policy: FaExactDatePolicy) -> None:
    """2025-12-25 (Christmas) had no SBI publication → carry forward."""
    christmas = date(2025, 12, 25)
    rate = policy.rate_for(christmas)
    # Should equal the most recent prior published rate (likely Dec 24).
    prior = policy.rate_for(date(2025, 12, 24))
    assert rate == prior


# Kept off the lines a traceback prints, so that the traceback check tests the error.
_PRE_ARCHIVE_EVENT = date(2019, 12, 31)


def test_before_earliest_archive_date_raises(policy: FaExactDatePolicy) -> None:
    with pytest.raises(MissingFxRateError) as exc_info:
        policy.rate_for(_PRE_ARCHIVE_EVENT)
    err = exc_info.value
    assert err.currency == "USD"
    assert err.last_available is None
    assert str(err) == (
        "MissingFxRateError: no USD rate available [Schedule FA exact-date FX lookup]; "
        "no prior rate available in the bundled archive. The bundled TTBR archive may "
        "need updating; see https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )
    # The requested date is the event's, which is input (#1047, #1368).
    assert_prints_no_marker(err, markers=("2019-12-31", "2019, 12, 31"))


def test_stale_beyond_max_days_raises() -> None:
    """If the most recent published rate is too far in the past, fail.

    Anchored on the *live* archive end rather than a hard-coded date so a
    routine TTBR refresh that appends newer rows cannot invalidate the
    boundary. The scheduled refresh (scripts/refresh_ttbr.py /
    .github/workflows/ttbr-refresh.yml) gates on this suite, so a date
    hard-coded to the snapshot's last row would deadlock the workflow:
    every refresh that extends the archive would break it.
    """
    p = FaExactDatePolicy(max_carry_forward_days=3)
    archive_end = max(load_ttbr(FaExactDatePolicy.currency))
    far = archive_end + timedelta(days=30)  # 30-day gap >> 3-day cap
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(far)
    err = exc_info.value
    assert err.last_available == archive_end
    assert err.max_carry_forward_days == 3


def test_stale_refusal_names_the_prior_rate_date_not_the_event_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1047: the refusal names the fixed lookup step and the latest
    prior public rate date, never the event date it was asked for or
    the day gap. The policy serves acquisitions, closing values,
    dividend credits and peak valuations alike, so it doesn't guess
    the event kind. With a 0-day cap any gap refuses; the default cap
    would carry 2025-03-29's rate forward."""
    rates = {date(2025, 3, 29): Decimal("85.1"), date(2026, 1, 1): Decimal("90.0")}
    monkeypatch.setattr("ibkr_fa.fx.policy_fa.load_ttbr", lambda _currency: rates)
    p = FaExactDatePolicy(max_carry_forward_days=0)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(date(2025, 4, 1))
    msg = str(exc_info.value)
    assert msg == (
        "MissingFxRateError: no USD rate available [Schedule FA exact-date FX lookup]; "
        "latest available prior rate is 2025-03-29, beyond the carry-forward limit of "
        "0 days. The bundled TTBR archive may need updating; see "
        "https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )
    assert "2025-03-29" in msg
    assert "2025-04-01" not in msg
    assert "3 days" not in msg


# #1368's example: an event date past the archive's end, kept off the lines a
# traceback prints so that the traceback check tests the error.
_MARKED_EVENT = date(2031, 7, 29)


def test_stale_refusal_keeps_no_event_date() -> None:
    """#1368: the error keeps no requested date, so the event's date is in
    neither its ``str``, ``repr``, args, fields, traceback nor chain. The
    expected message follows the live archive end (see
    test_stale_beyond_max_days_raises)."""
    archive_end = max(load_ttbr(FaExactDatePolicy.currency))
    with pytest.raises(MissingFxRateError) as exc_info:
        FaExactDatePolicy().rate_for(_MARKED_EVENT)
    assert str(exc_info.value) == (
        "MissingFxRateError: no USD rate available [Schedule FA exact-date FX lookup]; "
        f"latest available prior rate is {archive_end.isoformat()}, beyond the "
        "carry-forward limit of 14 days. The bundled TTBR archive may need updating; "
        "see https://github.com/jay-tau/ibkr-fa for the latest snapshot."
    )
    assert_prints_no_marker(exc_info.value, markers=("2031-07-29", "2031, 7, 29"))
    assert exc_info.value == MissingFxRateError(
        currency="USD",
        last_available=archive_end,
        max_carry_forward_days=14,
        policy_kind="Schedule FA exact-date FX lookup",
    )


def test_within_max_days_after_archive_end_succeeds() -> None:
    """A date a few days after the last archived rate carries forward.

    Archive-end-relative for refresh-robustness (see
    test_stale_beyond_max_days_raises).
    """
    p = FaExactDatePolicy(max_carry_forward_days=14)
    archive_end = max(load_ttbr(FaExactDatePolicy.currency))
    just_after = archive_end + timedelta(days=7)  # within the 14-day cap
    last_rate = p.rate_for(archive_end)
    just_after_rate = p.rate_for(just_after)
    assert just_after_rate == last_rate


def test_carry_forward_boundary_is_exactly_max_days() -> None:
    """The staleness cap is inclusive: a gap of exactly ``max_carry_forward_days``
    still carries forward, but one day more raises.

    Locks the ``gap_days > self._max_days`` boundary against an off-by-one
    regression (``>`` silently becoming ``>=`` or vice versa). Anchored on
    the live archive end so it survives TTBR refreshes.
    """
    cap = 14
    p = FaExactDatePolicy(max_carry_forward_days=cap)
    archive_end = max(load_ttbr(FaExactDatePolicy.currency))

    at_cap = archive_end + timedelta(days=cap)
    assert p.rate_for(at_cap) == p.rate_for(archive_end)

    beyond_cap = archive_end + timedelta(days=cap + 1)
    with pytest.raises(MissingFxRateError) as exc_info:
        p.rate_for(beyond_cap)
    assert exc_info.value.last_available == archive_end


def test_rate_for_is_deterministic(policy: FaExactDatePolicy) -> None:
    d = date(2026, 4, 30)
    assert policy.rate_for(d) == policy.rate_for(d)


def test_returns_positive_decimal(policy: FaExactDatePolicy) -> None:
    rate = policy.rate_for(date(2026, 4, 30))
    assert isinstance(rate, Decimal)
    assert rate > 0


def test_satisfies_fxpolicy_protocol() -> None:
    p: FxPolicy = FaExactDatePolicy()
    assert p.currency == "USD"
    assert isinstance(p.rate_for(date(2026, 4, 30)), Decimal)


# ============================================================================
# Postcondition: rate bounds (PR #123, issue #117)
# ============================================================================


def test_policy_returned_rate_is_in_sane_bounds() -> None:
    """Smoke test that the bundled USD archive's rates all fall
    within the policy's postcondition window. If they didn't, every
    lookup would raise InvariantError. Free given the loader-level
    invariant from PR #123, but tested here to lock the policy's
    postcondition against future refactors that might bypass the
    loader."""
    policy = FaExactDatePolicy(max_carry_forward_days=14)
    # Sample a date known to have a rate in the archive (PR #122
    # confirmed 2025-12-31 has 89.47).
    rate = policy.rate_for(date(2025, 12, 31))
    assert SANE_RATE_LOWER < rate <= SANE_RATE_UPPER
