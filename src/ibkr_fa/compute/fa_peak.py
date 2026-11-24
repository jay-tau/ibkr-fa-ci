"""Select a Schedule FA peak from daily native values and convert it to INR.

Peaks are native-first by default (``docs/portal/fx-policy-choice.md``):
the highest native value wins and is converted at its own date's TTBR.
``peak_currency="inr"`` instead scores every day at that day's TTBR. In
both modes the earliest maximum wins. Each caller keeps its own daily
replay and only offers the resulting values here.

Each INR score and the winner's INR value are the exact product of the
offered value and its TTBR (:func:`~ibkr_fa.compute.exact_inr.checked_product`,
#1260), so INR ties and the filed rupee follow the exact amounts.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ibkr_fa.compute.exact_inr import checked_product
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.types import PeakCurrency

_INR_PEAK_WARNING = (
    'Schedule FA peak override active: schedule_fa.peak_currency = "inr" '
    "selects the maximum daily INR-converted value instead of selecting "
    "the native-currency peak first."
)
_PEAK_INR = "Schedule FA peak, INR"


def warn_if_inr_peak_currency(peak_currency: PeakCurrency) -> None:
    """Warn when the non-default INR peak is used."""

    if peak_currency == "inr":
        warnings.warn(_INR_PEAK_WARNING, UserWarning, stacklevel=1)


@dataclass(frozen=True, slots=True)
class SelectedPeak:
    """The winning day, its native value and its TTBR conversion."""

    on_date: date
    value_native: Decimal
    ttbr: Decimal
    value_inr: Decimal


class PeakSelector:
    """Offer daily native values one at a time, then ask for the peak.

    Native mode looks up only the winner's TTBR, in :meth:`result`. INR
    mode looks up each offered day's TTBR in :meth:`offer` and reuses the
    winner's. ``MissingFxRateError`` and a ``checked_product`` refusal
    propagate to the caller.
    """

    def __init__(self, *, fx_policy: FxPolicy, peak_currency: PeakCurrency) -> None:
        self._fx_policy = fx_policy
        self._peak_currency = peak_currency
        self._best: tuple[Decimal, date, Decimal, Decimal | None] | None = None

    def offer(self, day: date, value_native: Decimal) -> None:
        ttbr = self._fx_policy.rate_for(day) if self._peak_currency == "inr" else None
        score = value_native if ttbr is None else checked_product(value_native, ttbr, at=_PEAK_INR)
        if self._best is None or score > self._best[0]:
            self._best = (score, day, value_native, ttbr)

    def result(self) -> SelectedPeak | None:
        if self._best is None:
            return None
        _, day, value_native, ttbr = self._best
        if ttbr is None:
            ttbr = self._fx_policy.rate_for(day)
        return SelectedPeak(
            on_date=day,
            value_native=value_native,
            ttbr=ttbr,
            value_inr=checked_product(value_native, ttbr, at=_PEAK_INR),
        )


def select_peak(
    values: Iterable[tuple[date, Decimal]],
    *,
    fx_policy: FxPolicy,
    peak_currency: PeakCurrency,
) -> SelectedPeak | None:
    """Select the peak of ``(day, native_value)`` pairs; ``None`` when empty."""

    selector = PeakSelector(fx_policy=fx_policy, peak_currency=peak_currency)
    for day, value_native in values:
        selector.offer(day, value_native)
    return selector.result()
