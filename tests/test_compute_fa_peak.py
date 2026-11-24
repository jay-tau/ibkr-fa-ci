"""Tests for :mod:`ibkr_fa.compute.fa_peak`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

import pytest

from ibkr_fa.compute.fa_peak import SelectedPeak, select_peak
from ibkr_fa.exceptions import InvariantError, MissingFxRateError
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.types import PeakCurrency
from tests._crash_reports import assert_prints_no_marker
from tests._exact_refusals import INEXACT_PRODUCT


class _RecordingPolicy:
    """FxPolicy double with exact-date rates that records every lookup."""

    currency: ClassVar[str] = "USD"

    def __init__(self, rates: dict[date, Decimal]) -> None:
        self._rates = rates
        self.calls: list[date] = []

    def rate_for(self, event_date: date) -> Decimal:
        self.calls.append(event_date)
        try:
            return self._rates[event_date]
        except KeyError as exc:
            raise MissingFxRateError(
                currency=self.currency,
                last_available=None,
                max_carry_forward_days=0,
            ) from exc


_D1, _D2, _D3 = date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5)


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_tie_picks_earliest_date(peak_currency: PeakCurrency) -> None:
    fx = _RecordingPolicy({_D1: Decimal("80"), _D2: Decimal("80"), _D3: Decimal("80")})
    values = [(_D1, Decimal("10")), (_D2, Decimal("12")), (_D3, Decimal("12"))]
    peak = select_peak(values, fx_policy=fx, peak_currency=peak_currency)
    assert peak == SelectedPeak(
        on_date=_D2, value_native=Decimal("12"), ttbr=Decimal("80"), value_inr=Decimal("960")
    )


def test_native_and_inr_pick_different_days_when_only_ttbr_moves() -> None:
    fx = _RecordingPolicy({_D1: Decimal("80"), _D2: Decimal("90")})
    values = [(_D1, Decimal("100")), (_D2, Decimal("99"))]
    native = select_peak(values, fx_policy=fx, peak_currency="native")
    inr = select_peak(values, fx_policy=fx, peak_currency="inr")
    assert native == SelectedPeak(
        on_date=_D1, value_native=Decimal("100"), ttbr=Decimal("80"), value_inr=Decimal("8000")
    )
    assert inr == SelectedPeak(
        on_date=_D2, value_native=Decimal("99"), ttbr=Decimal("90"), value_inr=Decimal("8910")
    )


def test_native_mode_looks_up_only_the_winner_rate() -> None:
    fx = _RecordingPolicy({_D2: Decimal("80")})
    values = [(_D1, Decimal("10")), (_D2, Decimal("12")), (_D3, Decimal("11"))]
    peak = select_peak(values, fx_policy=fx, peak_currency="native")
    assert peak is not None and peak.on_date == _D2
    assert fx.calls == [_D2]


def test_inr_mode_reuses_the_winner_rate() -> None:
    fx = _RecordingPolicy({_D1: Decimal("80"), _D2: Decimal("80"), _D3: Decimal("80")})
    values = [(_D1, Decimal("10")), (_D2, Decimal("12")), (_D3, Decimal("11"))]
    select_peak(values, fx_policy=fx, peak_currency="inr")
    assert fx.calls == [_D1, _D2, _D3]


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_missing_rate_raises(peak_currency: PeakCurrency) -> None:
    fx = _RecordingPolicy({})
    with pytest.raises(MissingFxRateError):
        select_peak([(_D1, Decimal("10"))], fx_policy=fx, peak_currency=peak_currency)


@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_empty_input_returns_none(peak_currency: PeakCurrency) -> None:
    fx = _RecordingPolicy({})
    assert select_peak([], fx_policy=fx, peak_currency=peak_currency) is None
    assert fx.calls == []


# ---- #1260: INR scores and the winner's INR value are exact -----------------

_PEAK_INR_REFUSAL = f"InvariantError: Schedule FA peak, INR: {INEXACT_PRODUCT}"
# A 1000-digit value whose INR product needs more digits than the cap, and one
# whose product overflows; the digit run marks the value in the absence checks.
_PAST_THE_CAP = pytest.mark.parametrize(
    ("value", "marker"),
    [("47391" * 200, "47391"), ("9E+999999", "999999")],
    ids=["past-1000-digits", "past-the-largest-exponent"],
)


def test_inr_scores_that_tie_only_when_rounded_pick_the_exact_maximum() -> None:
    """Under the INR override, ₹9180 and ₹9180.00000000000000000000000009 tie
    once rounded to 28 digits, so main kept the earlier day. The exact scores
    differ, so the later day wins."""
    fx = _RecordingPolicy({_D1: Decimal("90"), _D2: Decimal("90")})
    later = Decimal("102.000000000000000000000000001")
    peak = select_peak([(_D1, Decimal("102")), (_D2, later)], fx_policy=fx, peak_currency="inr")
    assert peak == SelectedPeak(
        on_date=_D2,
        value_native=later,
        ttbr=Decimal("90"),
        value_inr=Decimal("9180.00000000000000000000000009"),
    )


def test_winner_converts_to_its_exact_inr_value() -> None:
    """$55.2372583479789103690685413 at ₹85.35/$ is ₹4714.4999…955 and files
    ₹4714. Main's 28-digit product was ₹4714.5, which files ₹4715."""
    fx = _RecordingPolicy({_D1: Decimal("85.35")})
    value = Decimal("55.2372583479789103690685413")
    peak = select_peak([(_D1, value)], fx_policy=fx, peak_currency="native")
    assert peak is not None
    assert peak.value_inr.compare_total(Decimal("4714.499999999999999999999999955")) == 0
    assert to_whole_rupees(peak.value_inr) == Decimal("4714")


@_PAST_THE_CAP
@pytest.mark.parametrize("peak_currency", ["native", "inr"])
def test_inr_product_refuses_when_it_cannot_be_exact(
    value: str, marker: str, peak_currency: PeakCurrency
) -> None:
    """The INR product refuses past the cap or the exponent limit: the score
    under the INR override, the winner's value under the native default. The
    refusal carries no context and chains nothing. Main rounded the long
    product and raised a raw Overflow for the other."""
    fx = _RecordingPolicy({_D1: Decimal("85.35")})
    with pytest.raises(InvariantError) as caught:
        select_peak([(_D1, Decimal(value))], fx_policy=fx, peak_currency=peak_currency)
    assert str(caught.value) == _PEAK_INR_REFUSAL
    assert caught.value.context == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert_prints_no_marker(caught.value, markers=(marker,))
