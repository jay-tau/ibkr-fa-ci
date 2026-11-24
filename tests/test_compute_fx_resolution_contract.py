"""Engine FX dates come from the policy's resolution, never from compute (#611).

Every engine function that labels a line with an FX specified date takes an
:class:`~ibkr_fa.fx.policy.AuditedFxPolicy` and reads the rate, the specified
date and the rate date from one ``rate_for_audited`` call per event. The
sentinel dates below are neither a preceding month-end nor the FY-end, so a
date worked out in compute would show up as a mismatch.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import ClassVar
from unittest.mock import Mock

import pytest

from ibkr_fa.compute.espp_capital_gains import compute_espp_capital_gains
from ibkr_fa.compute.robinhood_capital_gains import _compute as _robinhood_cg_from_lots
from ibkr_fa.compute.robinhood_options import compute_robinhood_options
from ibkr_fa.compute.schedule_cg import compute_schedule_cg_stcg_lines
from ibkr_fa.compute.schedule_cg_ltcg import compute_schedule_cg_ltcg_lines
from ibkr_fa.compute.schedule_os import compute_schedule_os_dividend_lines
from ibkr_fa.compute.schedule_os_interest import compute_schedule_os_interest_lines
from ibkr_fa.compute.schedule_os_other_income import compute_schedule_os_other_income_lines
from ibkr_fa.compute.schedule_os_substitute_dividend import (
    compute_schedule_os_substitute_dividend_lines,
)
from ibkr_fa.config import RobinhoodOptionTreatment
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.policy import AuditedFxPolicy
from ibkr_fa.fx.policy_rule115 import Rule115OtherSourcesPolicy
from ibkr_fa.models import OtherSourceIncome, SubstituteDividendIncome
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._compute_builders import espp_closed_lot as _espp_closed
from tests._compute_builders import espp_config as _espp_cfg
from tests._compute_builders import espp_purchase as _espp_purchase
from tests._compute_builders import ltcg_closed_lot as _ltcg_lot
from tests._compute_builders import make_interest_credit as _make_credit
from tests._compute_builders import stcg_closed_lot as _stcg_lot
from tests._fx_stubs import AuditedFxStub
from tests._os_dividend_inputs import make_div
from tests._robinhood_cg_inputs import cg_base_events as _rh_cg_events
from tests._robinhood_cg_inputs import cg_base_slices as _rh_cg_slices
from tests._robinhood_cg_inputs import cg_config as _rh_cg_config
from tests._robinhood_cg_inputs import cg_lot_result as _rh_cg_lot_result
from tests._robinhood_cg_inputs import cg_paired as _rh_cg_paired
from tests._robinhood_option_inputs import closed_option_events as _closed_events
from tests._robinhood_option_inputs import option_config as _config
from tests._robinhood_option_inputs import option_paired as _paired

_FY_START = date(2025, 4, 1)
_FY_END = date(2026, 3, 31)
_CLOSE_DATE = date(2025, 12, 10)
# Before every event date below, so the CG and dividend date-order
# invariants hold.
_SPECIFIED_DATE = date(2025, 5, 17)
_RATE_DATE = date(2025, 5, 9)
_RATE = Decimal("91.2345")

# One (rate, specified date, rate date) per emitted line.
type _FxFields = tuple[Decimal, date, date]
type _Case = Callable[[AuditedFxPolicy], list[_FxFields]]


def _cg_stcg(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_cg_stcg_lines(
        [_stcg_lot(close_date_time=_CLOSE_DATE)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.stcg_lines
    ]


def _cg_ltcg(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_cg_ltcg_lines(
        [_ltcg_lot(close_date_time=_CLOSE_DATE)],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.ltcg_lines
    ]


def _espp_capital_gains(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_espp_capital_gains(
        [_espp_closed()],
        [_espp_purchase()],
        cfg=_espp_cfg(),
        financial_year_start=date(2026, 4, 1),
        financial_year_end=date(2027, 3, 31),
        fx_policy=fx,
    )
    lines = (*result.stcg.stcg_lines, *result.ltcg.ltcg_lines)
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in lines
    ]


def _robinhood_capital_gains(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = _robinhood_cg_from_lots(
        _rh_cg_paired(_rh_cg_events()),
        _rh_cg_lot_result(_rh_cg_slices()),
        config=_rh_cg_config(),
        securities=_rh_cg_config().securities,
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    lines = (*result.stcg.stcg_lines, *result.ltcg.ltcg_lines)
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in lines
    ]


def _os_dividends(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_os_dividend_lines(
        [make_div(payment_date=date(2025, 6, 15))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.lines
    ]


def _os_interest(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_os_interest_lines(
        [_make_credit(credit_date=date(2025, 6, 5))],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.lines
    ]


def _os_other_income(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_os_other_income_lines(
        [
            OtherSourceIncome(
                nature="Securities lending fee",
                payment_date=date(2025, 6, 1),
                amount_gross=Decimal("0.11"),
                currency="USD",
                description="Stock Lending",
                source_row_index=1,
                source_country_iso2=CountryCode("US"),
            )
        ],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.lines
    ]


def _os_substitute_dividends(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_schedule_os_substitute_dividend_lines(
        [
            SubstituteDividendIncome(
                symbol=Ticker("TEST"),
                isin=Isin("BM99970C1019"),
                issuer_country_iso2=CountryCode("BM"),
                source_country_iso2=CountryCode("US"),
                payment_date=date(2025, 7, 1),
                amount_gross=Decimal("10"),
                currency="USD",
                description="synthetic substitute dividend",
            )
        ],
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        fx_policy=fx,
    )
    return [
        (line.fx_rate_inr_per_usd, line.fx_specified_date, line.fx_rate_publication_date)
        for line in result.lines
    ]


def _robinhood_option_capital_gains(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_robinhood_options(
        _paired(_closed_events()),
        config=_config(RobinhoodOptionTreatment.CAPITAL_GAINS),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        capital_gains_fx_policy=fx,
        business_fx_policy=AuditedFxStub(specified_date=_FY_END),
    )
    return [
        (line.fx_rate, line.fx_specified_date, line.fx_rate_date)
        for line in result.capital_gain_lines
    ]


def _robinhood_option_business(fx: AuditedFxPolicy) -> list[_FxFields]:
    result = compute_robinhood_options(
        _paired(_closed_events()),
        config=_config(RobinhoodOptionTreatment.SPECULATIVE_BUSINESS),
        financial_year_start=_FY_START,
        financial_year_end=_FY_END,
        capital_gains_fx_policy=AuditedFxStub(),
        business_fx_policy=fx,
    )
    return [
        (summary.fx_rate, summary.fx_specified_date, summary.fx_rate_date)
        for summary in result.business_summaries
    ]


_CASES: tuple[tuple[str, _Case], ...] = (
    ("schedule-cg-stcg", _cg_stcg),
    ("schedule-cg-ltcg", _cg_ltcg),
    ("espp-capital-gains", _espp_capital_gains),
    ("robinhood-capital-gains", _robinhood_capital_gains),
    ("schedule-os-dividends", _os_dividends),
    ("schedule-os-interest", _os_interest),
    ("schedule-os-other-income", _os_other_income),
    ("schedule-os-substitute-dividends", _os_substitute_dividends),
    ("robinhood-option-capital-gains", _robinhood_option_capital_gains),
    ("robinhood-option-business", _robinhood_option_business),
)


@pytest.mark.parametrize("case", [pytest.param(case, id=name) for name, case in _CASES])
def test_lines_carry_the_policy_resolution(case: _Case, monkeypatch: pytest.MonkeyPatch) -> None:
    fx = AuditedFxStub(_RATE, specified_date=_SPECIFIED_DATE, rate_date=_RATE_DATE)
    monkeypatch.setattr(fx, "rate_for", Mock(side_effect=AssertionError("use rate_for_audited")))
    audited = Mock(wraps=fx.rate_for_audited)
    monkeypatch.setattr(fx, "rate_for_audited", audited)
    lines = case(fx)

    assert lines
    assert set(lines) == {(_RATE, _SPECIFIED_DATE, _RATE_DATE)}
    assert audited.call_count == len(lines)


@pytest.mark.parametrize("case", [_cg_stcg, _cg_ltcg], ids=["stcg", "ltcg"])
@pytest.mark.parametrize(
    "make_policy",
    [
        pytest.param(lambda: AuditedFxStub(specified_date=_CLOSE_DATE), id="on-close-date"),
        pytest.param(
            lambda: Rule115OtherSourcesPolicy(previous_year_end=_FY_END),
            id="clause-c-fy-end-policy",
        ),
    ],
)
def test_cg_specified_date_not_before_close_date_raises(
    case: _Case, make_policy: Callable[[], AuditedFxPolicy]
) -> None:
    """A mis-wired policy whose specified date is on or after the close
    date is refused by the CG line, not paired with a local label."""
    with pytest.raises(InvariantError, match="fx_specified_date must be strictly"):
        case(make_policy())


class _RateOnlyFxPolicy:
    """A plain :class:`~ibkr_fa.fx.policy.FxPolicy` with no audited resolution."""

    currency: ClassVar[str] = "USD"

    def rate_for(self, event_date: date) -> Decimal:
        del event_date
        return _RATE


@pytest.mark.parametrize("case", [pytest.param(case, id=name) for name, case in _CASES])
def test_rate_only_policy_raises_instead_of_making_up_a_date(case: _Case) -> None:
    with pytest.raises(AttributeError, match="rate_for_audited"):
        case(_RateOnlyFxPolicy())  # ty: ignore[invalid-argument-type]
