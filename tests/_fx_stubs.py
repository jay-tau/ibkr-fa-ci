"""Shared FX stubs for compute and pipeline tests.

- ``FixedRatePolicy``: one INR rate for every date.
- ``DateAwareRatePolicy``: a rate per date; records each date asked for.
- ``CalendarPolicy``: a rate per date; records each date asked for and raises
  ``MissingFxRateError`` for a date it has no rate for.
- ``AuditedFxStub``: an audited FX policy (#611).
- ``rule115_rates``: the vendored TTBR rates the FY policies read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx.policy_rule115 import Rule115Resolution, preceding_month_end
from ibkr_fa.fx.ttbr_loader import load_ttbr


class FixedRatePolicy:
    """Stub FxPolicy that returns the same INR rate for every date."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rate: Decimal) -> None:
        self._rate = rate

    def rate_for(self, event_date: date) -> Decimal:
        return self._rate


class DateAwareRatePolicy:
    """Stub FxPolicy that returns different rates on different dates."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rates: dict[date, Decimal]) -> None:
        self._rates = rates
        self.requested_dates: list[date] = []

    def rate_for(self, event_date: date) -> Decimal:
        self.requested_dates.append(event_date)
        return self._rates[event_date]


class CalendarPolicy:
    """FxPolicy double — maps date->rate so tests can assert what date
    was looked up."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rates: dict[date, Decimal]) -> None:
        self._rates = rates
        self.requested_dates: list[date] = []

    def rate_for(self, event_date: date) -> Decimal:
        self.requested_dates.append(event_date)
        try:
            return self._rates[event_date]
        except KeyError as exc:
            raise MissingFxRateError(
                currency=self.currency,
                last_available=None,
                max_carry_forward_days=0,
            ) from exc


class AuditedFxStub:
    """A USD :class:`~ibkr_fa.fx.policy.AuditedFxPolicy` with canned answers.

    ``rate`` is one flat rate, or a map keyed by event date; an event
    date missing from the map raises ``KeyError``. The specified date is
    the preceding month-end of the event date unless ``specified_date``
    fixes it (e.g. the FY-end for Rule 115(1), Explanation (2)(c)
    income). ``rate_date`` defaults to the specified date (no carry-back).

    ``max_carry_forward_days`` and ``assessment_year_start`` are the
    Rule 115 policy constructor kwargs. They are accepted and ignored so
    a pipeline test can monkeypatch a policy class with this stub.
    """

    currency: ClassVar[str] = "USD"

    def __init__(
        self,
        rate: Decimal | Mapping[date, Decimal] = Decimal("85"),
        *,
        specified_date: date | None = None,
        rate_date: date | None = None,
        max_carry_forward_days: int | None = None,
        assessment_year_start: int | None = None,
    ) -> None:
        del max_carry_forward_days, assessment_year_start
        self._rate = rate
        self._specified_date = specified_date
        self._rate_date = rate_date

    def rate_for(self, event_date: date) -> Decimal:
        return self.rate_for_audited(event_date).rate

    def rate_for_audited(self, event_date: date) -> Rule115Resolution:
        rate = self._rate if isinstance(self._rate, Decimal) else self._rate[event_date]
        specified = self._specified_date or preceding_month_end(event_date)
        return Rule115Resolution(
            rate=rate,
            specified_date=specified,
            rate_date=self._rate_date or specified,
        )


def rule115_rates(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep: Callable[[date], bool] = lambda _day: True,
    overrides: dict[date, Decimal] | None = None,
) -> None:
    """The FY policies read the vendored TTBR's ``keep`` dates, then ``overrides``."""
    rates = {day: rate for day, rate in load_ttbr("USD").items() if keep(day)}
    rates.update(overrides or {})
    monkeypatch.setattr("ibkr_fa.fx.policy_rule115.load_ttbr", lambda _currency: rates)
