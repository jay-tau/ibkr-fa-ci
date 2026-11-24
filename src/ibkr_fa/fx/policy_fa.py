"""Schedule FA FX policy: SBI TTBR on the requested disclosure date.

The official Income Tax Department Schedule FA guide maps initial
investment, peak balance, and calendar-year foreign income to the
investment date, peak date, and December 31 respectively. All Schedule
FA gross credits, including sale/redemption proceeds, use December 31
under the project's uniform disclosure policy. Callers pass the
applicable date; this policy resolves its SBI TT-Buying rate.

When SBI did not publish a rate on that date (Sundays, public holidays,
days when SBI partially published), the convention is to **carry
forward** the most recent prior published rate. This module enforces
that with a configurable staleness ceiling so an absent
``MissingFxRateError`` doesn't silently use a year-old rate after a
gap in the upstream archive.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Final

from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.fx.ttbr_loader import SANE_RATE_LOWER, SANE_RATE_UPPER, load_ttbr
from ibkr_fa.invariants import require

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

DEFAULT_MAX_CARRY_FORWARD_DAYS: Final[int] = 14
# The policy serves acquisitions, closing values, dividend credits and peak
# valuations alike and gets only a date, so its refusal names this fixed step
# rather than a guessed event kind (#1047).
_FA_LOOKUP_STEP: Final[str] = "Schedule FA exact-date FX lookup"


def carry_back_rate(
    lookup_date: date,
    *,
    rates: Mapping[date, Decimal],
    sorted_dates: Sequence[date],
    max_carry_forward_days: int,
    currency: str,
    policy_kind: Callable[[], str],
) -> tuple[Decimal, date]:
    """Return the rate for ``lookup_date`` and the date it was published.

    That is the rate published on ``lookup_date`` itself, else the one
    published on the latest date strictly before it, if that date is at
    most ``max_carry_forward_days`` earlier. A later date's rate is never
    used. ``sorted_dates`` holds ``rates``' dates in ascending order.

    Raises:
        MissingFxRateError: With ``last_available=None`` when nothing was
            published before ``lookup_date``, else with the latest prior
            date when it is too old. ``policy_kind`` is called for the
            citation only then.

    The caller checks the returned rate's bounds.
    """
    rate = rates.get(lookup_date)
    if rate is not None:
        return rate, lookup_date

    from bisect import bisect_left  # noqa: PLC0415  — stdlib lazy import

    idx = bisect_left(sorted_dates, lookup_date)
    if idx == 0:
        raise MissingFxRateError(
            currency=currency,
            last_available=None,
            max_carry_forward_days=max_carry_forward_days,
            policy_kind=policy_kind(),
        )
    prior = sorted_dates[idx - 1]
    if (lookup_date - prior).days > max_carry_forward_days:
        raise MissingFxRateError(
            currency=currency,
            last_available=prior,
            max_carry_forward_days=max_carry_forward_days,
            policy_kind=policy_kind(),
        )
    return rates[prior], prior


class FaExactDatePolicy(FxPolicy):
    """Schedule FA FX policy: TTBR on the exact event date.

    Resolution algorithm for ``rate_for(event_date)``:

    1. If the bundled archive has a published rate for ``event_date``,
       return it.
    2. Otherwise, find the most recent published date strictly before
       ``event_date`` (binary search over the sorted archive).
       a. If that date is more than ``max_carry_forward_days`` before
          ``event_date``, raise :class:`MissingFxRateError` naming the
          Schedule FA exact-date FX lookup step.
       b. Otherwise, return that prior rate.
    3. If ``event_date`` is before the earliest published date in the
       archive, raise :class:`MissingFxRateError` with
       ``last_available=None``.

    The carry-forward direction is always *backwards in time* — never
    forward. Tax rules require using a rate that was knowable on or
    before the event date; using a future rate would be anachronistic.

    Schedule FA gross-credit callers request December 31. Taxable income
    in Schedule OS and Schedule CG uses separate Rule 115 policies and
    may therefore have different INR amounts. See
    ``docs/portal/fx-policy-choice.md``.

    Args:
        max_carry_forward_days: Maximum number of days the policy will
            carry forward a prior rate. Default 14 — covers typical
            holiday/weekend gaps. Pass a larger value when running
            against a stale bundled archive (the CLI may surface this
            as a flag).

    Attributes:
        currency: Always ``"USD"`` for v1.
    """

    currency: ClassVar[str] = "USD"

    def __init__(
        self,
        *,
        max_carry_forward_days: int = DEFAULT_MAX_CARRY_FORWARD_DAYS,
    ) -> None:
        if max_carry_forward_days < 0:
            msg = "max_carry_forward_days must be non-negative"
            raise ValueError(msg)
        self._max_days = max_carry_forward_days
        # Materialise the sorted-by-date list once. The loader's
        # functools.cache means this is essentially free on repeated
        # construction.
        rates = load_ttbr(self.currency)
        self._rates: dict[date, Decimal] = dict(rates)
        self._sorted_dates: list[date] = sorted(self._rates)

    @property
    def max_carry_forward_days(self) -> int:
        """The configured staleness ceiling (read-only)."""
        return self._max_days

    def rate_for(self, event_date: date) -> Decimal:
        rate, _ = carry_back_rate(
            event_date,
            rates=self._rates,
            sorted_dates=self._sorted_dates,
            max_carry_forward_days=self._max_days,
            currency=self.currency,
            policy_kind=lambda: _FA_LOOKUP_STEP,
        )
        self._require_sane_rate(rate, event_date=event_date)
        return rate

    @staticmethod
    def _require_sane_rate(rate: Decimal, *, event_date: date) -> None:
        """Postcondition: the returned rate must fall within sane bounds.

        Free given load_ttbr's load-time invariants on the same
        bounds, but the per-call assertion documents the policy's
        contract and catches any future refactor that bypasses the
        loader-level check (e.g., a cached patched value).
        """
        require(
            SANE_RATE_LOWER < rate <= SANE_RATE_UPPER,
            what=(
                f"FaExactDatePolicy.rate_for postcondition: rate must be in "
                f"({SANE_RATE_LOWER}, {SANE_RATE_UPPER}]"
            ),
            rate=rate,
            event_date=event_date,
        )
