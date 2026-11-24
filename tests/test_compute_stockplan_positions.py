"""Tests for compute/stockplan_positions.py — the Dec-31 StockPlan RSU holding (#922).

Hand-built vests (no workbooks, no pipeline). The numbers mirror the
synthetic pipeline fixtures: four CY 2025 vests totalling 16.158 shares.
Each refusal is matched in full; none chains another exception (#1290).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ibkr_fa.compute.stockplan_positions import (
    StockplanDec31Vests,
    StockplanFaError,
    reconstruct_stockplan_dec31_vests,
    stockplan_holding_lots,
)
from ibkr_fa.models.instrument_identity import InstrumentIdentity
from ibkr_fa.models.open_lot import Side
from ibkr_fa.parser.stockplan.capital_gain_loss import StockplanVest
from ibkr_fa.types import CountryCode, Isin, Ticker
from tests._stockplan_fa_cases import MISMATCH

_CY_END = date(2025, 12, 31)


def _vest(vest_date: str, order: str, fmv: str, shares: str) -> StockplanVest:
    return StockplanVest(
        vest_date=date.fromisoformat(vest_date),
        order_number=order,
        fmv_per_share=Decimal(fmv),
        gsus_vested=Decimal(shares),
        shares_deposited=Decimal(shares),
    )


_V1 = _vest("2025-08-14", "V1", "196.42", "6.214")
_V2 = _vest("2025-09-15", "V2", "203.18", "3.107")
_V3 = _vest("2025-10-14", "V3", "211.95", "3.729")
_V4 = _vest("2025-11-14", "V4", "224.63", "3.108")
_V5 = _vest("2026-01-14", "V5", "231.05", "3.108")
_HELD = (_V1, _V2, _V3, _V4)
# The Holdings Report's Selected Date and the Releases Report's run date: both
# reports exported on one day after every fixture vest (#1290).
_SELECTED = date(2026, 6, 22)

_RELEASES_BEFORE = (
    "the Releases Report was run before the Holdings Report's Selected Date, so a vest the "
    "Holdings Report counts can be missing from it; export the Releases Report on or after "
    "the Selected Date, for example both reports on the same day"
)
_VEST_ON_SELECTED = (
    "a Releases Report vest is dated on the Holdings Report's Selected Date, so the Holdings "
    "Report may or may not include it; re-export the Vested Share Holdings report with a later "
    "Selected Date, and the Releases Report on or after that new Selected Date, for example "
    "both on the same day"
)
_STALE = (
    "StockPlan year-end freshness: the Holdings Report's Selected Date (the 'Quote History' "
    "sheet's Quote Date) is not strictly after Dec-31 of the calendar year, so it can't show "
    "which vests were still held then; re-export the Vested Share Holdings report with a "
    "Selected Date after year-end, and the Releases Report on or after that Selected Date, "
    "for example both on the same day"
)
_NO_HELD = (
    "the Releases Report has no Vest Date on or before Dec-31 of the calendar year; "
    "nothing to report"
)


def _totals(*vests: StockplanVest) -> tuple[Decimal, Decimal]:
    """The Holdings aggregate ``(shares, acquisition value)`` for ``vests``."""
    shares = sum((v.shares_deposited for v in vests), Decimal("0"))
    acq = sum((v.fmv_per_share * v.shares_deposited for v in vests), Decimal("0"))
    return shares, acq


def _reconstruct(
    vests: list[StockplanVest],
    anchor: tuple[StockplanVest, ...],
    *,
    selected_date: date = _SELECTED,
    releases_run_on: date = _SELECTED,
) -> StockplanDec31Vests:
    """Reconstruct ``vests`` against a Holdings Report holding exactly ``anchor``."""
    shares, acq = _totals(*anchor)
    return reconstruct_stockplan_dec31_vests(
        vests,
        shares,
        acq,
        _CY_END,
        selected_date=selected_date,
        releases_run_on=releases_run_on,
    )


def _refusal(vests: list[StockplanVest], anchor: tuple[StockplanVest, ...], **dates: date) -> str:
    with pytest.raises(StockplanFaError) as excinfo:
        _reconstruct(vests, anchor, **dates)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    return str(excinfo.value)


def test_exact_reconciliation_returns_the_held_vests_in_date_order() -> None:
    dec31 = _reconstruct([_V3, _V1, _V4, _V2], _HELD)
    assert dec31.held == _HELD
    assert dec31.held_shares == Decimal("16.158")
    assert dec31.reconciled_as_of == date(2025, 11, 14)


@pytest.mark.parametrize(
    ("selected_date", "reconciled_as_of"),
    [
        pytest.param(date(2026, 1, 13), date(2025, 11, 14), id="selected-before-the-later-vest"),
        pytest.param(date(2026, 1, 15), date(2026, 1, 14), id="selected-after-the-later-vest"),
    ],
)
def test_vest_after_dec31_is_not_held(selected_date: date, reconciled_as_of: date) -> None:
    # The Holdings Report counts the later vest only when it is before the Selected Date.
    anchor = tuple(v for v in (*_HELD, _V5) if v.vest_date < selected_date)
    dec31 = _reconstruct([*_HELD, _V5], anchor, selected_date=selected_date)
    assert dec31.held == _HELD
    assert dec31.held_shares == Decimal("16.158")
    assert dec31.reconciled_as_of == reconciled_as_of


def test_later_vest_left_out_of_the_holdings_refuses() -> None:
    # Before #1290 the first prefix matching the Holdings totals won, so a later
    # vest before the Selected Date could stay outside it (#878's limitation).
    assert _refusal([*_HELD, _V5], _HELD) == MISMATCH


@pytest.mark.parametrize("later_date", ["2025-10-14", "2026-01-14"])
def test_vest_the_context_would_round_away_still_counts(later_date: str) -> None:
    # Rounded to the default 28 digits, both totals equal the first vest's
    # alone. The sums are exact, so the later vest counts and the Holdings
    # totals, which leave it out, don't reconcile.
    first = _vest("2025-07-15", "A", "1.00", "100000000.00")
    later = _vest(later_date, "B", "1.00", "0.00000000000000000001")
    assert _totals(first, later) == _totals(first)
    assert _refusal([first, later], (first,)) == MISMATCH


def test_vest_value_keeps_every_digit() -> None:
    # Price x shares has 41 significant digits. Rounded to 28 it equals this
    # Acquisition Value, so the totals matched only through rounding; exactly,
    # they don't.
    one = "1.00000000000000000001"
    vest = _vest("2025-07-15", "A", one, one)
    shares, value = Decimal(one), Decimal("1.00000000000000000002")
    assert _totals(vest) == (shares, value)
    with pytest.raises(StockplanFaError) as excinfo:
        reconstruct_stockplan_dec31_vests(
            [vest], shares, value, _CY_END, selected_date=_SELECTED, releases_run_on=_SELECTED
        )
    assert str(excinfo.value) == MISMATCH


def test_exact_sums_reconcile_past_the_context_precision() -> None:
    # 32 significant digits: rounded to 28, the sum would miss the Holdings
    # totals; exactly, it equals them, and the held shares keep every digit.
    whole = _vest("2025-07-15", "A", "1", "100000000000")
    tiny = _vest("2025-08-14", "B", "1", "0.00000000000000000001")
    total = Decimal("100000000000.00000000000000000001")
    assert _totals(whole, tiny) != (total, total)
    dec31 = reconstruct_stockplan_dec31_vests(
        [whole, tiny], total, total, _CY_END, selected_date=_SELECTED, releases_run_on=_SELECTED
    )
    assert dec31.held == (whole, tiny)
    assert dec31.held_shares == total


def test_vest_that_left_before_year_end_is_not_filed_as_held() -> None:
    # #1290: vest A left in December and vest B, after Dec-31, has the same
    # shares and value. The Holdings totals equal either vest alone.
    vest_a = _vest("2025-07-15", "A", "100", "1")
    vest_b = _vest("2026-01-14", "B", "100", "1")
    assert _refusal([vest_a, vest_b], (vest_a,)) == MISMATCH


def test_two_vests_on_one_date_are_both_held() -> None:
    vest_a = _vest("2025-10-14", "A", "211.95", "3.729")
    vest_b = _vest("2025-10-14", "B", "211.95", "0.689")
    dec31 = _reconstruct([vest_b, vest_a], (vest_a, vest_b))
    assert dec31.held == (vest_a, vest_b)
    assert dec31.reconciled_as_of == date(2025, 10, 14)


def test_holdings_counting_one_of_two_vests_on_a_date_refuses() -> None:
    vest_a = _vest("2026-01-14", "A", "231.05", "1")
    vest_b = _vest("2026-01-14", "B", "231.05", "2")
    assert _refusal([*_HELD, vest_a, vest_b], (*_HELD, vest_a)) == MISMATCH


@pytest.mark.parametrize(
    ("vests", "anchor", "dates", "message"),
    [
        pytest.param(
            list(_HELD),
            _HELD,
            {"releases_run_on": _SELECTED - timedelta(days=1)},
            _RELEASES_BEFORE,
            id="releases-run-the-day-before",
        ),
        pytest.param(
            [*_HELD, _V5],
            (*_HELD, _V5),
            {"selected_date": _V5.vest_date},
            _VEST_ON_SELECTED,
            id="vest-on-the-selected-date-counted",
        ),
        pytest.param(
            [*_HELD, _V5],
            _HELD,
            {"selected_date": _V5.vest_date},
            _VEST_ON_SELECTED,
            id="vest-on-the-selected-date-not-counted",
        ),
        pytest.param(
            list(_HELD),
            _HELD,
            {"selected_date": _CY_END, "releases_run_on": _CY_END},
            _STALE,
            id="selected-on-dec31",
        ),
        pytest.param(
            list(_HELD),
            (_V1, _V2),
            {"selected_date": date(2025, 10, 1)},
            _STALE,
            id="selected-before-a-held-vest",
        ),
    ],
)
def test_selected_date_rule_refuses(
    vests: list[StockplanVest],
    anchor: tuple[StockplanVest, ...],
    dates: dict[str, date],
    message: str,
) -> None:
    assert _refusal(vests, anchor, **dates) == message


@pytest.mark.parametrize(
    ("vests", "anchor", "dates", "message"),
    [
        pytest.param(
            [_V5],
            (_V1,),
            {"selected_date": _CY_END, "releases_run_on": date(2025, 12, 1)},
            _NO_HELD,
            id="no-held-vest-first",
        ),
        pytest.param(
            [*_HELD, _V5],
            (_V1,),
            {"selected_date": _V5.vest_date, "releases_run_on": date(2026, 1, 13)},
            _RELEASES_BEFORE,
            id="releases-run-before-vest-on-date",
        ),
        pytest.param(
            [*_HELD, _V5],
            (_V1,),
            {"selected_date": _V5.vest_date},
            _VEST_ON_SELECTED,
            id="vest-on-date-before-mismatch",
        ),
        pytest.param(
            list(_HELD),
            (_V1,),
            {"selected_date": _CY_END, "releases_run_on": _CY_END},
            MISMATCH,
            id="mismatch-before-freshness",
        ),
    ],
)
def test_refusals_come_in_order(
    vests: list[StockplanVest],
    anchor: tuple[StockplanVest, ...],
    dates: dict[str, date],
    message: str,
) -> None:
    # Each input also breaks the next rule; the first rule broken refuses.
    assert _refusal(vests, anchor, **dates) == message


_JULY_VEST = _vest("2025-07-01", "A", "196.42", "6.214")
_REEXPORTED_ON = date(2026, 1, 15)


@pytest.mark.parametrize(
    ("vests", "exported_on", "message", "reconciled_as_of"),
    [
        pytest.param(
            [_JULY_VEST, _V5],
            _V5.vest_date,
            _VEST_ON_SELECTED,
            _V5.vest_date,
            id="vest-on-the-selected-date",
        ),
        pytest.param(
            [_JULY_VEST],
            date(2025, 12, 30),
            _STALE,
            _JULY_VEST.vest_date,
            id="selected-date-before-year-end",
        ),
    ],
)
def test_selected_date_remedies_clear_once_both_reports_move(
    vests: list[StockplanVest], exported_on: date, message: str, reconciled_as_of: date
) -> None:
    # #1425: both reports were exported on one day. A new Selected Date for
    # the Holdings Report alone trades the refusal for check 4.1's; with the
    # Releases Report exported on that day too, the run passes.
    def holdings(selected_date: date) -> tuple[StockplanVest, ...]:
        return tuple(v for v in vests if v.vest_date < selected_date)

    assert (
        _refusal(
            vests, holdings(exported_on), selected_date=exported_on, releases_run_on=exported_on
        )
        == message
    )
    assert (
        _refusal(
            vests,
            holdings(_REEXPORTED_ON),
            selected_date=_REEXPORTED_ON,
            releases_run_on=exported_on,
        )
        == _RELEASES_BEFORE
    )
    dec31 = _reconstruct(
        vests,
        holdings(_REEXPORTED_ON),
        selected_date=_REEXPORTED_ON,
        releases_run_on=_REEXPORTED_ON,
    )
    assert dec31.held == (_JULY_VEST,)
    assert dec31.held_shares == _JULY_VEST.shares_deposited
    assert dec31.reconciled_as_of == reconciled_as_of


@pytest.mark.parametrize(
    ("shares", "acq"),
    [
        pytest.param(Decimal("99.0"), _totals(*_HELD)[1], id="shares"),
        pytest.param(_totals(*_HELD)[0], _totals(*_HELD)[1] + Decimal("0.01"), id="value"),
    ],
)
def test_mismatch_refuses(shares: Decimal, acq: Decimal) -> None:
    with pytest.raises(StockplanFaError) as excinfo:
        reconstruct_stockplan_dec31_vests(
            list(_HELD), shares, acq, _CY_END, selected_date=_SELECTED, releases_run_on=_SELECTED
        )
    assert str(excinfo.value) == MISMATCH
    assert str(shares) not in str(excinfo.value)
    assert str(acq) not in str(excinfo.value)


_INSTRUMENT = InstrumentIdentity(
    symbol=Ticker("XQCB"),
    isin=Isin("US99971P1012"),
    listing_exchange="NASDAQ",
    issuer_country_code=CountryCode("US"),
)


def test_holding_lots_carry_the_fmv_cost_basis_per_vest() -> None:
    vests = (_vest("2025-03-25", "A", "100", "10"), _V1)
    lots = stockplan_holding_lots(vests, _INSTRUMENT, "SAMPLE ISSUER CB Inc.")
    # 196.42 x 6.214 = 1220.55388
    assert [lot.cost_basis_money for lot in lots] == [Decimal("1000"), Decimal("1220.55388")]
    assert [lot.quantity for lot in lots] == [Decimal("10"), Decimal("6.214")]
    assert [lot.acquisition_date for lot in lots] == [date(2025, 3, 25), date(2025, 8, 14)]
    for lot in lots:
        assert (lot.currency, lot.symbol, lot.isin, lot.issuer_country_code) == (
            "USD",
            "XQCB",
            "US99971P1012",
            "US",
        )
        assert lot.description == "SAMPLE ISSUER CB Inc."
        assert lot.side is Side.LONG


def test_holding_lot_cost_keeps_every_digit() -> None:
    # #1314: 2 x 123456793.90642939150401836969 needs 29 digits; main rounded
    # the cost to 246913587.8128587830080367394.
    vest = _vest("2025-08-14", "A", "123456793.90642939150401836969", "2")
    [lot] = stockplan_holding_lots((vest,), _INSTRUMENT, "SAMPLE ISSUER CB Inc.")
    assert str(lot.cost_basis_money) == "246913587.81285878300803673938"
