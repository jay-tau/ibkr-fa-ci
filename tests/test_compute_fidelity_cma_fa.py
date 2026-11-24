"""Fidelity CMA Schedule FA daily account-value bound tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Context, Decimal, localcontext
from typing import override

import pytest

import ibkr_fa.compute.fidelity_cma_fa as cma_fa_mod
from ibkr_fa.compute.fa_a3 import build_fa_a3_rows
from ibkr_fa.compute.fidelity_cma_capital_gains import build_fidelity_cma_lot_inventory
from ibkr_fa.compute.fidelity_cma_fa import (
    FidelityCmaFaError,
    FidelityCmaPeakBounds,
    _price,
    compute_fidelity_cma_peak_bounds,
)
from ibkr_fa.compute.lot_overlap import CyOverlap
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import OpenLot, Side
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.parser.fidelity_cma import (
    CmaCashActivity,
    CmaCashActivityKind,
    CmaCoreTransaction,
    CmaCoreTransactionKind,
    CmaStatement,
    validate_statement_bundle,
)
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference.us_market_calendar import (
    us_market_session_on_or_before,
    us_market_sessions,
)
from ibkr_fa.types import CountryCode, Isin, PeakCurrency, Ticker
from tests._crash_reports import assert_unchained_refusal
from tests._exact_refusals import INEXACT_PRODUCT
from tests._fidelity_cma import A2_PRICING_2024_REFUSAL, session_series
from tests._fidelity_cma_fa_inputs import (
    ACCRUED_INTEREST_REFUSAL,
    NO_SESSION_CLOSE,
    NON_SESSION_CLOSE,
    accrued_interest_statements,
    assert_no_marker,
    inverting_statement,
    marker_statement,
)
from tests._fidelity_cma_fa_inputs import CMA_MARKER_ACCOUNT as ACCOUNT
from tests._fidelity_cma_fa_inputs import CMA_MARKER_HOLDER as HOLDER
from tests._fidelity_cma_fa_inputs import CMA_MARKER_NAV as NAV
from tests._fidelity_cma_fa_inputs import late_interest_statements as _late_interest_statements
from tests._fidelity_cma_fa_inputs import mixed_day_statement as _statement
from tests._fx_stubs import AuditedFxStub


class _DateFx(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("200") if event_date == date(2025, 1, 1) else Decimal("80")


def _series(*, price: str = "1", year: int = 2025) -> PriceSeries:
    # Every US market session, from the one that seeds January 1.
    return session_series(
        Ticker("XQCN"),
        us_market_session_on_or_before(date(year, 1, 1)),
        date(year, 12, 31),
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        price=price,
    )


def test_mixed_day_produces_distinct_lower_and_upper_bounds() -> None:
    result = compute_fidelity_cma_peak_bounds(
        validate_statement_bundle((_statement(),)),
        calendar_year=2025,
        series=_series(),
        fx_policy=AuditedFxStub(Decimal("80")),
    )
    assert result.lower_inr == Decimal("960")
    assert result.upper_inr == Decimal("1200")
    assert result.closing_usd == Decimal("12")
    assert result.closing_inr == Decimal("960")


def test_peak_bounds_default_to_native_before_conversion() -> None:
    native = compute_fidelity_cma_peak_bounds(
        validate_statement_bundle((_statement(),)),
        calendar_year=2025,
        series=_series(),
        fx_policy=_DateFx(),
    )
    with pytest.warns(UserWarning, match="peak override active"):
        inr = compute_fidelity_cma_peak_bounds(
            validate_statement_bundle((_statement(),)),
            calendar_year=2025,
            series=_series(),
            fx_policy=_DateFx(),
            peak_currency="inr",
        )
    assert (native.lower_date, native.lower_inr) == (
        date(2025, 6, 1),
        Decimal("960"),
    )
    assert (native.upper_date, native.upper_inr) == (
        date(2025, 6, 1),
        Decimal("1200"),
    )
    assert (inr.lower_date, inr.lower_inr) == (
        date(2025, 1, 1),
        Decimal("2000"),
    )
    assert (inr.upper_date, inr.upper_inr) == (
        date(2025, 1, 1),
        Decimal("2000"),
    )


def test_sweep_fund_a3_peak_defaults_to_native_before_conversion() -> None:
    series = _series(price="11")
    # The $10 seed close is carried to 2025-01-01, the day TTBR is 200.
    series = replace(series, closes={**series.closes, date(2024, 12, 31): Decimal("10")})
    lot = OpenLot(
        currency_primary="USD",
        symbol=Ticker("XQCN"),
        description="SAMPLE ISSUER CN",
        isin=Isin("US99971W1062"),
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("1"),
        mark_price=Decimal("11"),
        cost_basis_money=Decimal("10"),
        side=Side.LONG,
        open_date_time=date(2025, 1, 1),
    )

    def peak(peak_currency: PeakCurrency) -> Decimal | None:
        (row,) = build_fa_a3_rows(
            CyOverlap(open_lots=[lot], closed_lots=[]),
            calendar_year=2025,
            fx_policy=_DateFx(),
            prices_by_ticker={Ticker("XQCN"): series},
            peak_currency=peak_currency,
        )
        return row.peak_value_inr

    assert peak("native") == Decimal("880")
    with pytest.warns(UserWarning, match="peak override active"):
        assert peak("inr") == Decimal("2000")


def test_month_end_reconciliation_refuses() -> None:
    with pytest.raises(FidelityCmaFaError, match="month-end cash or quantity"):
        compute_fidelity_cma_peak_bounds(
            validate_statement_bundle((_statement(ending_fcb="3"),)),
            calendar_year=2025,
            series=_series(),
            fx_policy=AuditedFxStub(Decimal("80")),
        )


def test_statement_price_must_match_fetched_nav() -> None:
    with pytest.raises(FidelityCmaFaError, match="execution price"):
        compute_fidelity_cma_peak_bounds(
            validate_statement_bundle((_statement(),)),
            calendar_year=2025,
            series=_series(price="1.01"),
            fx_policy=AuditedFxStub(Decimal("80")),
        )


def test_late_interest_is_bounded_across_following_statement_window() -> None:
    result = compute_fidelity_cma_peak_bounds(
        validate_statement_bundle(_late_interest_statements()),
        calendar_year=2025,
        series=_series(),
        fx_policy=AuditedFxStub(Decimal("80")),
    )
    assert result.lower_inr == Decimal("800")
    assert result.upper_inr == Decimal("880")
    assert result.closing_inr == Decimal("800")


@pytest.mark.parametrize(
    ("accrued_in", "refusal"),
    [
        ((1,), ACCRUED_INTEREST_REFUSAL),
        (
            (1, 2),
            "non-zero accrued interest is unsupported for CMA peak bounds: statements 1 and 2 "
            "(by period start, year-end reports excluded) show it, so enter the Fidelity CMA "
            "Schedule FA by hand.",
        ),
    ],
    ids=["one-statement", "two-statements"],
)
def test_earlier_statement_accrued_interest_refuses(
    accrued_in: tuple[int, ...], refusal: str
) -> None:
    """The refusal names each statement with accrued interest by its position
    and gives the by-hand route (#1552)."""
    first, second = accrued_interest_statements()
    if 2 in accrued_in:
        second = replace(second, accrued_interest_usd=Decimal("1"))
    with pytest.raises(FidelityCmaFaError) as excinfo:
        compute_fidelity_cma_peak_bounds(
            validate_statement_bundle((first, second)),
            calendar_year=2025,
            series=_series(),
            fx_policy=AuditedFxStub(Decimal("80")),
        )
    assert type(excinfo.value) is FidelityCmaFaError
    assert str(excinfo.value) == refusal


@pytest.mark.parametrize("debit_first", [False, True], ids=["credit-first", "debit-first"])
def test_native_first_bounds_can_invert_in_inr(debit_first: bool) -> None:
    result = compute_fidelity_cma_peak_bounds(
        validate_statement_bundle((inverting_statement(debit_first=debit_first),)),
        calendar_year=2025,
        series=_series(),
        fx_policy=FaExactDatePolicy(max_carry_forward_days=14),
    )
    assert (result.lower_date, result.lower_inr) == (date(2025, 12, 10), Decimal("527755.00"))
    assert (result.upper_date, result.upper_inr) == (date(2025, 3, 10), Decimal("521220.00"))


def _refusal(series: PriceSeries) -> str:
    with pytest.raises(FidelityCmaFaError) as excinfo:
        compute_fidelity_cma_peak_bounds(
            validate_statement_bundle((marker_statement(),)),
            calendar_year=2025,
            series=series,
            fx_policy=AuditedFxStub(Decimal("80")),
        )
    assert type(excinfo.value) is FidelityCmaFaError
    return str(excinfo.value)


@pytest.mark.parametrize("missing", [date(2025, 7, 10), date(2025, 12, 31)])
def test_a_session_without_a_nav_refuses(missing: date) -> None:
    """A missing session's NAV is never carried from an earlier session (#1135)."""
    series = _series(price=NAV)
    series = replace(series, closes={k: v for k, v in series.closes.items() if k != missing})
    message = _refusal(series)
    assert message == NO_SESSION_CLOSE
    assert_no_marker(message)


@pytest.mark.parametrize(
    "bar_day", [date(2025, 7, 12), date(2025, 7, 4)], ids=["saturday", "listed-closure"]
)
def test_a_nav_dated_on_a_non_session_refuses(bar_day: date) -> None:
    """A NAV dated on a weekend or a listed closure is never used (#1135)."""
    series = _series(price=NAV)
    series = replace(series, closes={**series.closes, bar_day: Decimal(NAV)})
    message = _refusal(series)
    assert message == NON_SESSION_CLOSE
    assert_no_marker(message)


def test_a_calendar_year_the_market_calendar_lacks_refuses_before_any_nav_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CY2024 refuses up front: its closures aren't listed (#1135)."""
    bundle = validate_statement_bundle((marker_statement(2024),))
    series = _series(price=NAV, year=2024)
    with monkeypatch.context() as patch:
        patch.setattr(cma_fa_mod, "_price", lambda *_args: pytest.fail("no NAV may be read"))
        with pytest.raises(FidelityCmaFaError) as excinfo:
            compute_fidelity_cma_peak_bounds(
                bundle, calendar_year=2024, series=series, fx_policy=AuditedFxStub(Decimal("80"))
            )
    message = str(excinfo.value)
    assert message == A2_PRICING_2024_REFUSAL
    assert_no_marker(message, year=2024)
    # Nothing else refuses: with 2024 listed, the same input is priced.
    monkeypatch.setattr(cma_fa_mod, "supports_calendar_year", lambda _year: True)
    result = compute_fidelity_cma_peak_bounds(
        bundle, calendar_year=2024, series=series, fx_policy=AuditedFxStub(Decimal("80"))
    )
    assert (result.lower_inr, result.upper_inr) == (Decimal("997.840"), Decimal("1237.840"))


def _opened_in_june() -> CmaStatement:
    """``marker_statement()`` with its first activity on its start, Sunday 2025-06-01."""
    base = marker_statement()
    (buy,) = base.core_transactions
    opening, *rest = base.cash_activity
    june = date(2025, 6, 1)
    return replace(
        base,
        period_start=june,
        core_transactions=(replace(buy, date=june),),
        cash_activity=(replace(opening, date=june), *rest),
    )


@pytest.mark.parametrize(
    "earlier_bars",
    [{}, {date(2025, 5, 26): Decimal(NAV)}],
    ids=["no-earlier-bar", "closure-bar-before-the-seed"],
)
def test_a_later_opening_needs_navs_only_from_its_seed_session(
    earlier_bars: dict[date, Decimal],
) -> None:
    """The window starts at Friday 2025-05-30, the seed session of 2025-06-01.
    A bar before it, here on the listed closure 2025-05-26, is ignored."""
    sessions = us_market_sessions(date(2025, 5, 30), date(2025, 12, 31))
    series = replace(_series(), closes={**earlier_bars, **dict.fromkeys(sessions, Decimal(NAV))})
    result = compute_fidelity_cma_peak_bounds(
        validate_statement_bundle((_opened_in_june(),)),
        calendar_year=2025,
        series=series,
        fx_policy=AuditedFxStub(Decimal("80")),
    )
    assert (result.lower_date, result.lower_inr) == (date(2025, 6, 1), Decimal("997.840"))
    assert (result.upper_date, result.upper_inr) == (date(2025, 6, 1), Decimal("1237.840"))


def test_january_first_takes_its_seed_nav_within_the_staleness_limit() -> None:
    """January 1, a listed closure, is priced at the 2024-12-31 seed session's NAV,
    one day earlier: refused with no staleness allowed, used with one day."""
    series = _series()
    series = replace(series, closes={**series.closes, date(2024, 12, 31): Decimal(NAV)})
    with pytest.raises(FidelityCmaFaError) as excinfo:
        _price(series, date(2025, 1, 1), 0)
    assert str(excinfo.value) == "SPAXX daily NAV coverage is incomplete"
    assert _price(series, date(2025, 1, 1), 1) == Decimal(NAV)


def test_a3_initial_value_of_an_open_spaxx_share_is_the_exact_half_up() -> None:
    """#1244: 2.010 of 3.015 units bought for USD 3.02 cost two thirds of it,
    exactly ₹75.50 at ₹37.50, which files ₹76. A 28-digit two thirds rounded
    down, so it filed ₹75."""
    base = _statement()
    (buy,) = base.core_transactions
    statement = replace(
        base,
        position=replace(base.position, quantity=Decimal("2.010")),
        core_transactions=(
            replace(buy, quantity=Decimal("3.015"), amount_usd=Decimal("3.02")),
            CmaCoreTransaction(
                date(2025, 4, 4),
                CmaCoreTransactionKind.SELL,
                Decimal("-1.005"),
                Decimal(1),
                Decimal("-1.01"),
                Decimal("2.01"),
            ),
        ),
    )
    inventory = build_fidelity_cma_lot_inventory(
        validate_statement_bundle((statement,)),
        spaxx_isin=Isin("US99971W1062"),
        report_date=date(2025, 12, 31),
        mark_price=Decimal(1),
    )

    (row,) = build_fa_a3_rows(
        CyOverlap(open_lots=list(inventory.open_lots), closed_lots=[]),
        calendar_year=2025,
        fx_policy=AuditedFxStub(Decimal("37.50")),
        prices_by_ticker={Ticker("SPAXX"): replace(_series(), ticker=Ticker("SPAXX"))},
    )
    assert (row.initial_value_inr, row.filed.initial_value_inr) == (Decimal("75.5"), Decimal(76))


# ---- #1267: exact daily account values --------------------------------------

_TINY = "0.0000000000000000000000000001"
_LONG_DAY = date(2025, 7, 10)
_CAP_REFUSAL = f"InvariantError: Fidelity CMA A2 peak, account value USD: {INEXACT_PRODUCT}"


def _bounds(statement: CmaStatement, series: PriceSeries | None = None) -> FidelityCmaPeakBounds:
    return compute_fidelity_cma_peak_bounds(
        validate_statement_bundle((statement,)),
        calendar_year=2025,
        series=series or _series(),
        fx_policy=AuditedFxStub(Decimal("80")),
    )


def _with_nav(nav: str) -> PriceSeries:
    """``_series()`` with ``nav`` on Thursday 2025-07-10."""
    series = _series()
    return replace(series, closes={**series.closes, _LONG_DAY: Decimal(nav)})


def test_long_nav_files_its_exact_rupee() -> None:
    """A NAV of $1.000625 less a unit in its 30th place makes 2025-07-10's 10
    units and $2 cash worth $12.00625 less 10 such units: ₹960.4999… at ₹80/$,
    which files ₹960. Main rounded the units' value to $10.00625 and filed the
    ₹961 lower bound."""
    result = _bounds(_statement(), _with_nav("1.000624" + "9" * 24))
    value = Decimal("12.00624" + "9" * 24)
    assert (result.lower_date, result.lower_usd) == (_LONG_DAY, value)
    with localcontext(Context(prec=100)):
        assert result.lower_inr == value * 80
    assert to_whole_rupees(result.lower_inr) == Decimal(960)


def test_nav_that_ties_only_when_rounded_moves_the_lower_bound_day() -> None:
    """From 2025-06-01 every day is worth $12. A NAV a unit in its 30th place
    above $1 on 2025-07-10 makes that day worth $12.00000000000000000000000000001.
    Main rounded it to $12, so the tie kept 2025-06-01; the exact value wins."""
    result = _bounds(_statement(), _with_nav("1." + "0" * 29 + "1"))
    assert (result.lower_date, result.lower_usd) == (
        _LONG_DAY,
        Decimal("12.00000000000000000000000000001"),
    )


def test_free_credit_balance_keeps_every_digit() -> None:
    """A $0.0000000000000000000000000001 deposit beside the opening one and a
    withdrawal of it on 2025-07-01 leave that unit in the free credit balance
    from January through June. So 2025-06-01's lower bound is $12 and that
    unit, and its upper bound, adding the $5 credit, $15 and that unit. Main
    rounded the January deposits to $10 and computed $12 and $15."""
    base = _statement()
    statement = replace(
        base,
        cash_activity=(
            *base.cash_activity,
            CmaCashActivity(
                date(2025, 1, 1), CmaCashActivityKind.DEPOSIT, "tiny credit", Decimal(_TINY)
            ),
            CmaCashActivity(
                date(2025, 7, 1),
                CmaCashActivityKind.WITHDRAWAL,
                "tiny debit",
                Decimal(_TINY).copy_negate(),
            ),
        ),
    )
    result = _bounds(statement)
    assert (result.lower_date, result.lower_usd) == (
        date(2025, 6, 1),
        Decimal("12.0000000000000000000000000001"),
    )
    assert (result.upper_date, result.upper_usd) == (
        date(2025, 6, 1),
        Decimal("15.0000000000000000000000000001"),
    )


def test_spaxx_quantity_and_amount_keep_every_digit() -> None:
    """10.0000000000000000000000000001 units bought at $1 with a deposit of
    that amount, all still held on Dec 31. The free credit balance, the
    quantity, each day's value and the Dec-31 account value are exact, so the
    month-end check passes and 2025-06-01's lower bound is $12 and that unit.
    Main rounded the quantity to 10 and refused at the month-end check."""
    long = Decimal("10.0000000000000000000000000001")
    base = _statement()
    (buy,) = base.core_transactions
    opening, *rest = base.cash_activity
    statement = replace(
        base,
        account_value_usd=Decimal("12.0000000000000000000000000001"),
        position=replace(base.position, quantity=long, ending_market_value_usd=long),
        core_transactions=(replace(buy, quantity=long, amount_usd=long, balance_usd=long),),
        cash_activity=(replace(opening, amount_usd=long), *rest),
    )
    result = _bounds(statement)
    assert (result.lower_date, result.lower_usd) == (
        date(2025, 6, 1),
        Decimal("12.0000000000000000000000000001"),
    )
    assert result.closing_usd == Decimal("12.0000000000000000000000000001")


def test_value_past_the_digit_cap_refuses_without_echo() -> None:
    """A NAV of 1,000 digits on 2025-07-10 makes the units' value need more
    than the cap, so A2 refuses with the fixed text. The refusal carries no
    context and chains nothing, and its text, repr and args hold none of the
    NAV, the holder or the account."""
    series = replace(
        _series(price=NAV), closes={**_series(price=NAV).closes, _LONG_DAY: Decimal("47391" * 200)}
    )
    with pytest.raises(InvariantError) as raised:
        _bounds(marker_statement(), series)
    expected = InvariantError(what=_CAP_REFUSAL.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(
        raised.value, expected, _CAP_REFUSAL, markers=("47391", HOLDER, ACCOUNT)
    )


def test_purchase_amount_leaves_an_exact_free_credit_balance() -> None:
    """A $12 deposit buys 10 units at $1 for $10.0000000000000000000000000001,
    leaving $1.9999999999999999999999999999 of free credit balance, which the
    June credit and debit take to the month-end's $3.9999999999999999999999999999.
    Each balance is exact, so 2025-06-01's lower bound is
    $13.9999999999999999999999999999 and the Dec-31 value matches. Main rounded
    the balance to $2 and refused at the month-end check."""
    base = _statement()
    (buy,) = base.core_transactions
    opening, *rest = base.cash_activity
    statement = replace(
        base,
        account_value_usd=Decimal("13.9999999999999999999999999999"),
        free_credit_balance_usd=Decimal("3.9999999999999999999999999999"),
        core_transactions=(replace(buy, amount_usd=Decimal("10.0000000000000000000000000001")),),
        cash_activity=(replace(opening, amount_usd=Decimal(12)), *rest),
    )
    result = _bounds(statement)
    assert (result.lower_date, result.lower_usd) == (
        date(2025, 6, 1),
        Decimal("13.9999999999999999999999999999"),
    )
    assert result.closing_usd == Decimal("13.9999999999999999999999999999")


def test_daily_check_holds_for_a_purchase_change_past_28_digits() -> None:
    """Guard: 10 units bought at $1 for $0.0000000000000000000000000001, all
    of a deposit of that amount, change the day's value by
    $9.9999999999999999999999999999. The daily account-value check adds the
    exact changes, so it holds and the bounds are main's. Rounding that change
    alone to $10 would make the check refuse."""
    base = _statement()
    (buy,) = base.core_transactions
    opening, *rest = base.cash_activity
    statement = replace(
        base,
        core_transactions=(replace(buy, amount_usd=Decimal(_TINY)),),
        cash_activity=(replace(opening, amount_usd=Decimal(_TINY)), *rest),
    )
    result = _bounds(statement)
    assert (result.lower_date, result.lower_usd) == (date(2025, 6, 1), Decimal(12))
    assert (result.upper_date, result.upper_usd) == (date(2025, 6, 1), Decimal(15))


# ---- #1533: a NAV only on days that hold or trade SPAXX ---------------------

_FIRST_BUY = date(2025, 1, 10)  # a Friday; Thursday 2025-01-09 is a listed closure


def _bought_on_january_10(*earlier: CmaCoreTransaction) -> CmaStatement:
    """``_statement()`` with its 10 units bought on Friday 2025-01-10, after
    ``earlier``: its $10 opening deposit stays cash until then."""
    base = _statement()
    (buy,) = base.core_transactions
    return replace(base, core_transactions=(*earlier, replace(buy, date=_FIRST_BUY)))


def _round_trip_on_january_6() -> CmaStatement:
    """``_bought_on_january_10()`` plus 5 units bought and redeemed on Monday
    2025-01-06, which starts and ends with none."""
    day = date(2025, 1, 6)
    return _bought_on_january_10(
        CmaCoreTransaction(
            day, CmaCoreTransactionKind.BUY, Decimal(5), Decimal(1), Decimal(5), Decimal(5)
        ),
        CmaCoreTransaction(
            day, CmaCoreTransactionKind.SELL, Decimal(-5), Decimal(1), Decimal(-5), Decimal(0)
        ),
    )


def _redeemed_in_march() -> CmaStatement:
    """``_statement()`` with its 10 units redeemed on Monday 2025-03-03 and
    bought back on Monday 2025-03-10, so 2025-03-04 to 2025-03-09 hold none."""
    base = _statement()
    (buy,) = base.core_transactions
    redeemed = date(2025, 3, 3)
    return replace(
        base,
        core_transactions=(
            buy,
            CmaCoreTransaction(
                redeemed,
                CmaCoreTransactionKind.SELL,
                Decimal(-10),
                Decimal(1),
                Decimal(-10),
                Decimal(0),
            ),
            replace(buy, date=date(2025, 3, 10)),
        ),
    )


def _without(series: PriceSeries, first: date, last: date) -> PriceSeries:
    """``series`` without its closes dated ``first`` through ``last``."""
    closes = {day: close for day, close in series.closes.items() if not first <= day <= last}
    assert len(closes) < len(series.closes)
    return replace(series, closes=closes)


@pytest.mark.parametrize(
    ("statement", "first", "last"),
    [
        (_bought_on_january_10(), date(2024, 12, 31), date(2025, 1, 9)),
        (_redeemed_in_march(), date(2025, 3, 4), date(2025, 3, 9)),
    ],
    ids=["before-the-first-buy", "after-a-full-redemption"],
)
def test_a_day_that_neither_holds_nor_trades_spaxx_needs_no_nav(
    statement: CmaStatement, first: date, last: date
) -> None:
    """#1533's repro: each day whose NAV is a close dated ``first`` through
    ``last`` neither holds nor trades SPAXX, so that NAV multiplies zero. A
    series without those closes gives the full series' bounds and Dec-31
    values; main refused it."""
    assert _bounds(statement, _without(_series(), first, last)) == _bounds(statement)


@pytest.mark.parametrize(
    ("statement", "missing"),
    [
        (_bought_on_january_10(), _FIRST_BUY),
        (_round_trip_on_january_6(), date(2025, 1, 6)),
        (_redeemed_in_march(), date(2025, 3, 3)),
        (_redeemed_in_march(), date(2025, 3, 10)),
    ],
    ids=["buy-from-none", "same-day-round-trip", "full-redemption", "buy-back"],
)
def test_a_day_that_trades_spaxx_still_needs_its_nav(
    statement: CmaStatement, missing: date
) -> None:
    """Guard (#1533): a core transaction's execution-price check reads its
    day's NAV, so a series without it refuses, even on a day that starts with
    no SPAXX, or starts and ends with none."""
    with pytest.raises(FidelityCmaFaError) as excinfo:
        _bounds(statement, _without(_series(), missing, missing))
    assert type(excinfo.value) is FidelityCmaFaError
    assert str(excinfo.value) == NO_SESSION_CLOSE
