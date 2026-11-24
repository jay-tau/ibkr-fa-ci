from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Context, Decimal, getcontext, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from ibkr_fa.compute.robinhood_a2_peak import (
    RobinhoodA2Peak,
    RobinhoodA2PeakError,
    _option_anchor_trade_quantity,
    _statement_only_crypto_cash,
    compute_robinhood_a2_peaks,
    replay_robinhood_account_equities,
    robinhood_required_equity_sessions,
    robinhood_required_option_sessions,
)
from ibkr_fa.compute.robinhood_options import (
    RobinhoodOptionBlocker,
    RobinhoodOptionBlockerKind,
    RobinhoodOptionContract,
    RobinhoodOptionsResult,
    RobinhoodOptionValuationAnchor,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import RobinhoodConfig
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.fx.policy import FxPolicy
from ibkr_fa.models.money import to_whole_rupees
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountReconciliation,
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.activity import (
    RobinhoodCryptoStatementRow,
    RobinhoodStatementReference,
)
from ibkr_fa.normalize.robinhood.bundle import RobinhoodReconciliationError
from ibkr_fa.normalize.robinhood.inventory import (
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.normalize.robinhood.security import RobinhoodSecurityEventReference
from ibkr_fa.parser.robinhood import RobinhoodEventKind
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatementActivity,
    RobinhoodStatementSection,
    SourceLocation,
)
from ibkr_fa.prices.series import MassiveOptionCloseSeries, OptionCloseBar, PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_unchained_refusal
from tests._exact_refusals import INEXACT_PRODUCT
from tests._robinhood_equity import held_equity
from tests._robinhood_fa_inputs import FlatFx
from tests._robinhood_option_series import option_series
from tests._robinhood_refusals import ACTIVITY_BOTH_REFUSAL, OPTION_BLOCKER_MESSAGES
from tests._robinhood_security import configured_securities, robinhood_security
from tests._robinhood_statements import robinhood_account_config


class _LaterHighFx(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("80") if event_date <= date(2025, 1, 10) else Decimal("100")


def _crypto_cash(
    *,
    settle_date: date | None = None,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
) -> dict[date, Decimal]:
    """The A2 crypto-cash map for one statement-only ``COIN`` row."""
    location = SourceLocation(
        page=2,
        section=RobinhoodStatementSection.ACCOUNT_ACTIVITY,
        row=1,
    )
    row = RobinhoodStatementActivity(
        location=location,
        description="Crypto transfer",
        symbol=None,
        account_type="Margin",
        transaction_code="COIN",
        transaction_date=date(2025, 1, 10),
        settle_date=settle_date,
        quantity=None,
        quantity_suffix=None,
        price_usd=None,
        debit_usd=debit,
        credit_usd=credit,
    )
    account = SimpleNamespace(
        activity=SimpleNamespace(
            crypto_rows=(RobinhoodCryptoStatementRow(RobinhoodStatementReference(0, location)),),
            primary_bundle=SimpleNamespace(statements=(SimpleNamespace(activities=(row,)),)),
        )
    )
    return _statement_only_crypto_cash(cast("RobinhoodAccountReconciliation", account))


@pytest.mark.parametrize(
    ("settle_date", "expected_date"),
    [
        (date(2025, 1, 13), date(2025, 1, 13)),
        (None, date(2025, 1, 10)),
    ],
)
def test_statement_only_crypto_cash_uses_settlement_then_transaction_date(
    settle_date: date | None,
    expected_date: date,
) -> None:
    assert _crypto_cash(settle_date=settle_date, debit=Decimal("5")) == {
        expected_date: Decimal("-5")
    }


@pytest.mark.parametrize(
    ("debit", "credit", "expected"),
    [
        ("5.00", None, "-5.00"),
        (None, "5.00", "5.00"),
        ("0.00", None, "0"),
        ("-0", None, "0"),
        (None, "0", "0"),
        (None, "-0.000", "0"),
    ],
)
def test_statement_only_crypto_cash_carries_the_exact_signed_cell(
    debit: str | None,
    credit: str | None,
    expected: str,
) -> None:
    """#1185: the A2 crypto-cash ledger gets the cell's exact signed amount,
    and a zero cell of any sign or scale as canonical zero."""
    cash = _crypto_cash(
        debit=None if debit is None else Decimal(debit),
        credit=None if credit is None else Decimal(credit),
    )
    assert [value.as_tuple() for value in cash.values()] == [Decimal(expected).as_tuple()]


def test_statement_only_crypto_cash_refusals_are_unchanged() -> None:
    """#1185: a row with neither cell, or with both, refuses as before."""
    with pytest.raises(RobinhoodA2PeakError) as missing:
        _crypto_cash()
    assert str(missing.value) == "statement-only Crypto cash row lacks an amount"
    with pytest.raises(RobinhoodReconciliationError) as both:
        _crypto_cash(debit=Decimal("0"), credit=Decimal("0.00"))
    assert str(both.value) == f"Robinhood reconciliation refused: {ACTIVITY_BOTH_REFUSAL}"


def _case(
    tmp_path: Path,
    *,
    missing_session: date | None = None,
    opening_date: date = date(2025, 1, 5),
    trade_date: date = date(2025, 1, 10),
    bar_start: date = date(2025, 1, 10),
):
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 12, 31),
        right="Call",
        strike_usd=Decimal("250"),
    )
    statement = SimpleNamespace(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        account_summary=SimpleNamespace(
            opening_brokerage_cash_usd=None,
            opening_deposit_sweep_usd=None,
            opening_total_securities_usd=None,
            closing_brokerage_cash_usd=Decimal("-50"),
            closing_deposit_sweep_usd=Decimal(),
            closing_total_securities_usd=Decimal("100"),
        ),
        sweep_activity=(),
    )
    account = SimpleNamespace(
        evidence=SimpleNamespace(
            events=(
                SimpleNamespace(
                    activity_date=trade_date,
                    amount_usd=Decimal("-50"),
                ),
            )
        ),
        activity=SimpleNamespace(
            crypto_rows=(),
            primary_bundle=SimpleNamespace(
                account_number="111111111",
                statements=(statement,),
            ),
        ),
        cash=SimpleNamespace(
            periods=(SimpleNamespace(closing_unsettled_usd=Decimal()),),
        ),
    )
    configured = robinhood_account_config(
        "111111111",
        opening_date=opening_date,
        address="500 Colonial Center Parkway",
        zip_code="32746",
        source="test",
    )
    trade = SimpleNamespace(
        contract=contract,
        activity_date=trade_date,
        settle_date=trade_date,
        quantity=Decimal("1"),
        kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
    )
    anchor = SimpleNamespace(
        contract=contract,
        statement_date=date(2025, 12, 31),
        quantity=Decimal("1"),
    )
    options = SimpleNamespace(
        blockers=(),
        trades=(trade,),
        validated_valuation_anchors=(anchor,),
    )
    quantities = SimpleNamespace(
        blockers=(),
        identity_blockers=(),
        movements=(),
        residuals=(),
        anchors=(),
    )
    bars = {
        day: OptionCloseBar(day, Decimal("1"), Decimal("1"), 1)
        for day in us_market_sessions(bar_start, date(2025, 12, 31))
        if day != missing_session
    }
    series = option_series(
        "O:XQAQ251231C00250000",
        "XQAQ",
        contract,
        bars,
        artifact_path=tmp_path / "cache.json",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    return (
        SimpleNamespace(accounts=(account,)),
        RobinhoodConfig(accounts=(configured,)),
        quantities,
        options,
        contract,
        series,
    )


def test_option_close_and_multiplier_feed_native_usd_peak(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(tmp_path)
    (peak,) = compute_robinhood_a2_peaks(
        paired,
        config=config,
        securities=configured_securities(config),
        quantities=quantities,
        options=options,
        equity_prices={},
        option_prices={contract: series},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )
    assert (peak.peak_date, peak.peak_usd, peak.peak_inr) == (
        date(2025, 1, 10),
        Decimal("50"),
        Decimal("4000"),
    )


@pytest.mark.parametrize(
    "field",
    [
        pytest.param({"underlying": "XQAR"}, id="underlying"),
        pytest.param({"expiration": date(2025, 12, 30)}, id="expiration"),
        pytest.param({"strike_usd": Decimal("251")}, id="strike"),
        pytest.param({"multiplier": Decimal("10")}, id="multiplier"),
    ],
)
def test_a2_peak_refuses_another_contracts_price_series(
    tmp_path: Path, field: dict[str, object]
) -> None:
    """#1417: the held XQAQ 2025-12-31 $250 call (multiplier 100) gets a close
    series that differs in one identity field. With its own series the same case
    files a $50 / ₹4,000 peak (the test above); another contract's closes refuse."""
    paired, config, quantities, options, contract, series = _case(tmp_path)
    with pytest.raises(
        RobinhoodA2PeakError, match=r"\ARobinhood option price identity is invalid\Z"
    ):
        compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={contract: replace(series, **field)},
            calendar_year=2025,
            fx_policy=FlatFx(),
        )


class _Fx8535(FxPolicy):
    currency = "USD"

    @override
    def rate_for(self, event_date: date) -> Decimal:
        return Decimal("85.35")


def test_peak_converts_to_its_exact_inr_value(tmp_path: Path) -> None:
    """#1260: a 28-digit option close on 2025-03-03 makes that day's account
    value $55.2372583479789103690685413, worth ₹4714.4999… at ₹85.35/$, which
    files ₹4714. Main's 28-digit product was ₹4714.5, which files ₹4715."""
    paired, config, quantities, options, contract, series = _case(tmp_path)
    peak_day = date(2025, 3, 3)
    close = Decimal("1.052372583479789103690685413")
    series = replace(
        series, bars={**series.bars, peak_day: OptionCloseBar(peak_day, close, Decimal("1"), 1)}
    )
    (peak,) = compute_robinhood_a2_peaks(
        paired,
        config=config,
        securities=configured_securities(config),
        quantities=quantities,
        options=options,
        equity_prices={},
        option_prices={contract: series},
        calendar_year=2025,
        fx_policy=_Fx8535(),
    )
    assert (peak.peak_date, peak.peak_usd) == (peak_day, Decimal("55.2372583479789103690685413"))
    assert peak.peak_inr == Decimal("4714.499999999999999999999999955")
    assert to_whole_rupees(peak.peak_inr) == Decimal("4714")


def test_inr_override_selects_after_daily_conversion(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(tmp_path)
    with pytest.warns(UserWarning, match="peak override active"):
        (peak,) = compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={contract: series},
            calendar_year=2025,
            fx_policy=_LaterHighFx(),
            peak_currency="inr",
        )
    assert (peak.peak_date, peak.peak_usd, peak.peak_inr) == (
        date(2025, 1, 11),
        Decimal("50"),
        Decimal("5000"),
    )


def test_cash_sweep_stays_inside_a2_account_value(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(tmp_path)
    account = paired.accounts[0]
    statement = account.activity.primary_bundle.statements[0]
    statement.account_summary.closing_brokerage_cash_usd = Decimal("-70")
    statement.account_summary.closing_deposit_sweep_usd = Decimal("20")
    statement.sweep_activity = (
        SimpleNamespace(
            transaction_date=date(2025, 1, 10),
            debit_usd=None,
            credit_usd=Decimal("20"),
        ),
    )

    (peak,) = compute_robinhood_a2_peaks(
        paired,
        config=config,
        securities=configured_securities(config),
        quantities=quantities,
        options=options,
        equity_prices={},
        option_prices={contract: series},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )

    assert (peak.peak_date, peak.peak_usd) == (date(2025, 1, 10), Decimal("50"))


def test_option_statement_can_use_settlement_basis() -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 2, 21),
        right="Call",
        strike_usd=Decimal("250"),
    )
    trade = SimpleNamespace(
        contract=contract,
        activity_date=date(2025, 1, 31),
        settle_date=date(2025, 2, 3),
        quantity=Decimal("1"),
        kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
    )
    anchor = SimpleNamespace(
        contract=contract,
        statement_date=date(2025, 1, 31),
        quantity=Decimal(),
    )
    assert (
        _option_anchor_trade_quantity(
            cast(
                "RobinhoodOptionsResult",
                SimpleNamespace(trades=(trade,)),
            ),
            cast("RobinhoodOptionValuationAnchor", anchor),
        )
        == 1
    )


def test_missing_option_close_on_held_session_refuses(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(
        tmp_path,
        missing_session=date(2025, 1, 13),
    )
    with pytest.raises(RobinhoodA2PeakError, match="option close coverage is incomplete"):
        compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={contract: series},
            calendar_year=2025,
            fx_policy=FlatFx(),
        )


@pytest.mark.parametrize(
    "kinds",
    [
        (RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,),
        (RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,),
        (
            RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION,
            RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,
        ),
    ],
)
def test_option_blockers_refuse_the_peak_with_each_kind_message(
    tmp_path: Path,
    kinds: tuple[RobinhoodOptionBlockerKind, ...],
) -> None:
    """#802: the guard refuses on any option blocker and names each kind, located
    at its first blocker (#1134)."""
    paired, config, quantities, options, contract, series = _case(tmp_path)
    located = {
        RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE: (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.PRE_CUTOVER_HOLDING_RULE,
                account_index=0,
                located_at=RobinhoodSecurityEventReference(0, 7),
                columns=("Activity Date",),
            ),
            "first at account 1, activity report row 7; column Activity Date",
        ),
        RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION: (
            RobinhoodOptionBlocker(
                kind=RobinhoodOptionBlockerKind.OPEN_BUSINESS_POSITION, account_index=0
            ),
            "first at account 1, option positions open at the financial year's end",
        ),
    }
    options.blockers = tuple(located[kind][0] for kind in kinds)
    with pytest.raises(RobinhoodA2PeakError) as raised:
        compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={contract: series},
            calendar_year=2025,
            fx_policy=FlatFx(),
        )
    assert "inventory evidence" not in str(raised.value)
    for kind in kinds:
        assert f"{OPTION_BLOCKER_MESSAGES[kind]} (1 blocker; {located[kind][1]})" in str(
            raised.value
        )


def test_option_carried_into_year_uses_prior_session_close(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(
        tmp_path,
        opening_date=date(2024, 1, 1),
        trade_date=date(2024, 12, 30),
        bar_start=date(2024, 12, 31),
    )
    (peak,) = compute_robinhood_a2_peaks(
        paired,
        config=config,
        securities=configured_securities(config),
        quantities=quantities,
        options=options,
        equity_prices={},
        option_prices={contract: series},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )
    assert (peak.peak_date, peak.peak_usd) == (date(2025, 1, 1), Decimal("50"))


def _option_trade(
    contract: RobinhoodOptionContract,
    activity_date: date,
    kind: RobinhoodEventKind,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract,
        activity_date=activity_date,
        settle_date=activity_date,
        quantity=Decimal("1"),
        kind=kind,
    )


@pytest.mark.parametrize(
    ("expiration", "opened", "closed", "expected"),
    [
        (
            date(2025, 1, 17),
            date(2025, 1, 15),
            date(2025, 1, 17),
            {date(2025, 1, 15), date(2025, 1, 16)},
        ),
        (date(2025, 12, 31), date(2024, 12, 30), date(2025, 1, 2), {date(2024, 12, 31)}),
        (
            date(2025, 5, 16),
            date(2025, 4, 16),
            date(2025, 4, 21),
            {date(2025, 4, 16), date(2025, 4, 17)},
        ),
        (
            date(2025, 3, 21),
            date(2025, 3, 19),
            None,
            {date(2025, 3, 19), date(2025, 3, 20), date(2025, 3, 21)},
        ),
    ],
)
def test_required_option_sessions_cover_each_held_day(
    expiration: date,
    opened: date,
    closed: date | None,
    expected: set[date],
) -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=expiration,
        right="Call",
        strike_usd=Decimal("250"),
    )
    trades = [_option_trade(contract, opened, RobinhoodEventKind.OPTION_BUY_TO_OPEN)]
    if closed is not None:
        trades.append(_option_trade(contract, closed, RobinhoodEventKind.OPTION_SELL_TO_CLOSE))
    options = SimpleNamespace(trades=tuple(trades), validated_valuation_anchors=())
    assert robinhood_required_option_sessions(
        cast("RobinhoodOptionsResult", options),
        calendar_year=2025,
    ) == {contract: frozenset(expected)}


def test_required_option_sessions_omit_contracts_never_held_in_year() -> None:
    expired = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2024, 11, 15),
        right="Call",
        strike_usd=Decimal("250"),
    )
    untraded = RobinhoodOptionContract(
        account_index=1,
        symbol="XQCA",
        expiration=date(2025, 6, 20),
        right="Put",
        strike_usd=Decimal("300"),
    )
    options = SimpleNamespace(
        trades=(
            _option_trade(expired, date(2024, 10, 1), RobinhoodEventKind.OPTION_BUY_TO_OPEN),
            _option_trade(expired, date(2024, 10, 8), RobinhoodEventKind.OPTION_SELL_TO_CLOSE),
        ),
        validated_valuation_anchors=(
            SimpleNamespace(
                contract=untraded,
                statement_date=date(2025, 1, 31),
                quantity=Decimal(),
            ),
        ),
    )
    assert (
        robinhood_required_option_sessions(
            cast("RobinhoodOptionsResult", options),
            calendar_year=2025,
        )
        == {}
    )


def test_required_option_sessions_refuse_unsupported_year(tmp_path: Path) -> None:
    paired, config, quantities, options, contract, series = _case(tmp_path)
    with pytest.raises(RobinhoodA2PeakError, match="does not support calendar year 2026"):
        robinhood_required_option_sessions(options, calendar_year=2026)
    with pytest.raises(RobinhoodA2PeakError, match="does not support calendar year 2026"):
        compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={contract: series},
            calendar_year=2026,
            fx_policy=FlatFx(),
        )


def test_required_option_sessions_refuse_negative_inventory() -> None:
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 12, 19),
        right="Call",
        strike_usd=Decimal("250"),
    )
    options = SimpleNamespace(
        trades=(
            _option_trade(contract, date(2025, 2, 3), RobinhoodEventKind.OPTION_SELL_TO_CLOSE),
        ),
        validated_valuation_anchors=(),
    )
    with pytest.raises(RobinhoodA2PeakError, match="option inventory became negative"):
        robinhood_required_option_sessions(
            cast("RobinhoodOptionsResult", options),
            calendar_year=2025,
        )


@pytest.mark.parametrize(
    ("opening_date", "trade_date"),
    [
        (date(2025, 1, 5), date(2025, 1, 10)),
        (date(2024, 1, 1), date(2024, 12, 30)),
    ],
)
def test_required_option_sessions_are_exactly_the_priced_sessions(
    tmp_path: Path,
    opening_date: date,
    trade_date: date,
) -> None:
    paired, config, quantities, options, contract, series = _case(
        tmp_path,
        opening_date=opening_date,
        trade_date=trade_date,
        bar_start=date(2024, 12, 2),
    )
    required = robinhood_required_option_sessions(options, calendar_year=2025)
    assert required.keys() == {contract}

    def peaks(bars: set[date]) -> None:
        compute_robinhood_a2_peaks(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            options=options,
            equity_prices={},
            option_prices={
                contract: replace(
                    series,
                    bars={day: bar for day, bar in series.bars.items() if day in bars},
                )
            },
            calendar_year=2025,
            fx_policy=FlatFx(),
        )

    assert required[contract] <= series.bars.keys()
    peaks(set(required[contract]))
    for day in sorted(required[contract]):
        with pytest.raises(RobinhoodA2PeakError, match="option close coverage is incomplete"):
            peaks(set(required[contract]) - {day})


@pytest.mark.parametrize(
    ("opening_date", "movement_date", "required_session"),
    [
        (date(2024, 1, 1), date(2024, 12, 30), date(2024, 12, 31)),
        (date(2025, 1, 11), date(2025, 1, 11), date(2025, 1, 10)),
    ],
)
def test_equity_requirements_include_carry_back_sessions(
    opening_date: date,
    movement_date: date,
    required_session: date,
) -> None:
    paired, config, quantities = held_equity(
        opening_date=opening_date,
        movement_date=movement_date,
    )
    required = robinhood_required_equity_sessions(
        paired,
        config=config,
        securities=configured_securities(config),
        quantities=quantities,
        calendar_year=2025,
    )
    assert required[Ticker("TEST")] >= {required_session}


def test_required_equity_sessions_refuse_unsupported_year() -> None:
    paired, config, quantities = held_equity(
        opening_date=date(2025, 1, 1),
        movement_date=date(2026, 1, 5),
    )
    with pytest.raises(RobinhoodA2PeakError, match="does not support calendar year 2026"):
        robinhood_required_equity_sessions(
            paired,
            config=config,
            securities=configured_securities(config),
            quantities=quantities,
            calendar_year=2026,
        )


def test_settlement_basis_anchor_preserves_trade_date_a2_quantity() -> None:
    statement = SimpleNamespace(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        account_summary=SimpleNamespace(
            opening_brokerage_cash_usd=None,
            opening_deposit_sweep_usd=None,
            opening_total_securities_usd=None,
            closing_brokerage_cash_usd=Decimal(),
            closing_deposit_sweep_usd=Decimal(),
            closing_total_securities_usd=Decimal(),
        ),
        sweep_activity=(),
    )
    account = SimpleNamespace(
        evidence=SimpleNamespace(
            events=(
                SimpleNamespace(
                    activity_date=date(2025, 1, 31),
                    amount_usd=Decimal("-10"),
                ),
            )
        ),
        activity=SimpleNamespace(
            crypto_rows=(),
            primary_bundle=SimpleNamespace(
                account_number="111111111",
                statements=(statement,),
            ),
        ),
        cash=SimpleNamespace(
            periods=(SimpleNamespace(closing_unsettled_usd=Decimal("-10")),),
        ),
    )
    configured = robinhood_account_config(
        "111111111",
        opening_date=date(2025, 1, 31),
        address="500 Colonial Center Parkway",
        zip_code="32746",
        source="test",
    )
    security = robinhood_security()
    quantities = SimpleNamespace(
        blockers=(),
        identity_blockers=(),
        movements=(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=date(2025, 1, 31),
                signed_quantity=Decimal("1"),
            ),
        ),
        residuals=(),
        anchors=(
            SimpleNamespace(
                account_index=0,
                statement_index=0,
                cusip=security.cusip,
                statement_quantity=Decimal(),
                trade_date_quantity=Decimal("1"),
                tolerance=Decimal(),
            ),
        ),
    )
    series = PriceSeries(
        ticker=Ticker("TEST"),
        currency="USD",
        closes={
            day: Decimal("15")
            for day in us_market_sessions(
                date(2025, 1, 31),
                date(2025, 12, 31),
            )
        },
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    (peak,) = compute_robinhood_a2_peaks(
        cast("RobinhoodAccountsReconciliation", SimpleNamespace(accounts=(account,))),
        config=RobinhoodConfig(accounts=(configured,)),
        securities=RobinhoodSecurityIdentities((security,)),
        quantities=cast("RobinhoodSecurityQuantityResult", quantities),
        options=cast(
            "RobinhoodOptionsResult",
            SimpleNamespace(blockers=(), trades=(), validated_valuation_anchors=()),
        ),
        equity_prices={Ticker("TEST"): series},
        option_prices={},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )
    assert (peak.peak_date, peak.peak_usd) == (date(2025, 1, 31), Decimal("5"))


def test_same_day_equity_movements_and_residual_are_netted(tmp_path: Path) -> None:
    statement = SimpleNamespace(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        account_summary=SimpleNamespace(
            opening_brokerage_cash_usd=None,
            opening_deposit_sweep_usd=None,
            opening_total_securities_usd=None,
            closing_brokerage_cash_usd=Decimal(),
            closing_deposit_sweep_usd=Decimal(),
            closing_total_securities_usd=Decimal(),
        ),
        sweep_activity=(),
    )
    account = SimpleNamespace(
        evidence=SimpleNamespace(events=()),
        activity=SimpleNamespace(
            crypto_rows=(),
            primary_bundle=SimpleNamespace(
                account_number="111111111",
                statements=(statement,),
            ),
        ),
        cash=SimpleNamespace(
            periods=(SimpleNamespace(closing_unsettled_usd=Decimal()),),
        ),
    )
    configured = robinhood_account_config(
        "111111111",
        opening_date=date(2025, 1, 5),
        address="500 Colonial Center Parkway",
        zip_code="32746",
        source="test",
    )
    security = robinhood_security()
    quantities = SimpleNamespace(
        blockers=(),
        identity_blockers=(),
        movements=(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=date(2025, 1, 8),
                signed_quantity=Decimal("2"),
            ),
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=date(2025, 1, 10),
                signed_quantity=Decimal("-2.00004"),
            ),
        ),
        residuals=(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=date(2025, 1, 10),
                quantity=Decimal("0.00004"),
            ),
        ),
        anchors=(
            SimpleNamespace(
                account_index=0,
                statement_index=0,
                cusip=security.cusip,
                statement_quantity=Decimal(),
                trade_date_quantity=Decimal(),
                tolerance=Decimal(),
            ),
        ),
    )
    options = SimpleNamespace(blockers=(), trades=(), validated_valuation_anchors=())
    prices = {day: Decimal("1") for day in us_market_sessions(date(2025, 1, 8), date(2025, 12, 31))}
    series = PriceSeries(
        ticker=Ticker("TEST"),
        currency="USD",
        closes=prices,
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    (peak,) = compute_robinhood_a2_peaks(
        cast("RobinhoodAccountsReconciliation", SimpleNamespace(accounts=(account,))),
        config=RobinhoodConfig(accounts=(configured,)),
        securities=RobinhoodSecurityIdentities((security,)),
        quantities=cast("RobinhoodSecurityQuantityResult", quantities),
        options=cast("RobinhoodOptionsResult", options),
        equity_prices={Ticker("TEST"): series},
        option_prices={},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )
    assert (peak.peak_date, peak.peak_usd) == (date(2025, 1, 8), Decimal("2"))


# ---- #1267: exact daily account values --------------------------------------

_LONG_CASH = "-49.9999999999999999999999999999"
_TINY = "0.0000000000000000000000000001"
_A2_CAP_REFUSAL = f"InvariantError: Robinhood A2 peak, account value USD: {INEXACT_PRODUCT}"


def _with_close(
    series: MassiveOptionCloseSeries, day: date, close: str
) -> MassiveOptionCloseSeries:
    return replace(
        series, bars={**series.bars, day: OptionCloseBar(day, Decimal(close), Decimal("1"), 1)}
    )


def _peak_of(
    case: tuple[Any, ...],
    *,
    series: MassiveOptionCloseSeries | None = None,
    equity_prices: Mapping[Ticker, PriceSeries] | None = None,
    securities: RobinhoodSecurityIdentities | None = None,
) -> RobinhoodA2Peak:
    paired, config, quantities, options, contract, case_series = case
    (peak,) = compute_robinhood_a2_peaks(
        paired,
        config=config,
        securities=securities or configured_securities(config),
        quantities=quantities,
        options=options,
        equity_prices=equity_prices or {},
        option_prices={contract: series or case_series},
        calendar_year=2025,
        fx_policy=FlatFx(),
    )
    return peak


@pytest.mark.parametrize(
    ("precision", "close", "value"),
    [
        (None, "1.0000624" + "9" * 22, "50.00624" + "9" * 22),
        (60, "1.0000624" + "9" * 54, "50.00624" + "9" * 54),
    ],
    ids=["default-context", "robinhood-fa-context"],
)
def test_long_option_value_files_its_exact_rupee(
    tmp_path: Path, precision: int | None, close: str, value: str
) -> None:
    """A one-contract option closing just under $1.0000625 is worth just under
    $100.00625, so the day's account value, after the -$50 cash, is just under
    $50.00625: ₹4000.4999… at ₹80/$, which files ₹4000. Main rounded the
    option value to $100.00625 and filed ₹4001, in the default 28-digit
    context and in robinhood-fa's 60-digit one."""
    case = _case(tmp_path)
    series = _with_close(case[5], date(2025, 3, 3), close)
    with localcontext(Context(prec=precision) if precision else getcontext()):
        peak = _peak_of(case, series=series)
    assert (peak.peak_date, peak.peak_usd) == (date(2025, 3, 3), Decimal(value))
    with localcontext(Context(prec=100)):
        assert peak.peak_inr == Decimal(value) * 80
    assert to_whole_rupees(peak.peak_inr) == Decimal("4000")


def test_value_that_ties_only_when_rounded_moves_the_peak_day(tmp_path: Path) -> None:
    """Every day from 2025-01-10 is worth $50. On 2025-03-03 a close one unit
    past 28 digits above $1 makes the day worth $50.000000000000000000000000001.
    Main rounded it to $50, so the tie kept 2025-01-10; the exact value wins."""
    case = _case(tmp_path)
    series = _with_close(case[5], date(2025, 3, 3), "1." + "0" * 28 + "1")
    peak = _peak_of(case, series=series)
    assert (peak.peak_date, peak.peak_usd) == (
        date(2025, 3, 3),
        Decimal("50.000000000000000000000000001"),
    )


def test_cash_ledger_and_statement_cash_keep_every_digit(tmp_path: Path) -> None:
    """A $0.0000000000000000000000000001 credit beside the -$50 trade on
    2025-01-10 leaves cash of $-49.9999999999999999999999999999, which the
    Dec-31 statement shows. The day's net, the running cash and the statement's
    cash total are exact, so they match and the peak is $50 and that unit. Main
    rounded all three to $-50 and computed a $50 peak."""
    case = _case(tmp_path)
    account = case[0].accounts[0]
    account.evidence.events = (
        *account.evidence.events,
        SimpleNamespace(activity_date=date(2025, 1, 10), amount_usd=Decimal(_TINY)),
    )
    statement = account.activity.primary_bundle.statements[0]
    statement.account_summary.closing_brokerage_cash_usd = Decimal(_LONG_CASH)
    peak = _peak_of(case)
    assert (peak.peak_date, peak.peak_usd) == (
        date(2025, 1, 10),
        Decimal("50.0000000000000000000000000001"),
    )


def _with_crypto_rows(case: tuple[Any, ...], *amounts: str) -> None:
    """Add statement-only ``COIN`` credits of ``amounts`` on 2025-01-10 to the
    case's account."""
    account = case[0].accounts[0]
    statement = account.activity.primary_bundle.statements[0]
    rows = tuple(
        RobinhoodStatementActivity(
            location=SourceLocation(
                page=2, section=RobinhoodStatementSection.ACCOUNT_ACTIVITY, row=index
            ),
            description="Crypto transfer",
            symbol=None,
            account_type="Margin",
            transaction_code="COIN",
            transaction_date=date(2025, 1, 10),
            settle_date=None,
            quantity=None,
            quantity_suffix=None,
            price_usd=None,
            debit_usd=None,
            credit_usd=Decimal(amount),
        )
        for index, amount in enumerate(amounts, start=1)
    )
    statement.activities = rows
    account.activity.crypto_rows = tuple(
        RobinhoodCryptoStatementRow(RobinhoodStatementReference(0, row.location)) for row in rows
    )


def test_statement_only_crypto_cash_sums_a_day_exactly(tmp_path: Path) -> None:
    """Two ``COIN`` credits on one day, $50 and $0.0000000000000000000000000001,
    sum to $50.0000000000000000000000000001; main rounded that to $50."""
    case = _case(tmp_path)
    _with_crypto_rows(case, "50", _TINY)
    assert _statement_only_crypto_cash(case[0].accounts[0]) == {
        date(2025, 1, 10): Decimal("50.0000000000000000000000000001")
    }


def test_crypto_cash_joins_the_day_cash_exactly(tmp_path: Path) -> None:
    """A $0.0000000000000000000000000001 ``COIN`` credit on the -$50 trade day
    makes that day's cash $-49.9999999999999999999999999999, which the Dec-31
    statement shows, and the peak $50 and that unit. Main rounded the day's
    cash to $-50 and computed a $50 peak."""
    case = _case(tmp_path)
    _with_crypto_rows(case, _TINY)
    statement = case[0].accounts[0].activity.primary_bundle.statements[0]
    statement.account_summary.closing_brokerage_cash_usd = Decimal(_LONG_CASH)
    peak = _peak_of(case)
    assert (peak.peak_date, peak.peak_usd) == (
        date(2025, 1, 10),
        Decimal("50.0000000000000000000000000001"),
    )


def test_option_quantity_ledger_and_anchor_keep_every_digit(tmp_path: Path) -> None:
    """Two buys on 2025-01-10, one contract and 0.0000000000000000000000000001
    of one, which the Dec-31 statement holds. The day's net, the ledger, the
    statement check's trade-date sum and the contracts times the multiplier are
    exact, so the peak is $50.00000000000000000000000001. Main rounded the
    ledger to one contract and refused at the statement."""
    case = _case(tmp_path)
    options = case[3]
    (trade,) = options.trades
    options.trades = (trade, SimpleNamespace(**{**vars(trade), "quantity": Decimal(_TINY)}))
    (anchor,) = options.validated_valuation_anchors
    anchor.quantity = Decimal("1.0000000000000000000000000001")
    peak = _peak_of(case)
    assert (peak.peak_date, peak.peak_usd) == (
        date(2025, 1, 10),
        Decimal("50.00000000000000000000000001"),
    )


def _equity_case(
    tmp_path: Path, quantities: tuple[str, ...], anchor: str, tolerance: str
) -> tuple[tuple[Any, ...], RobinhoodSecurityIdentities, dict[Ticker, PriceSeries]]:
    """``_case`` with no option and no cash, and buys of ``quantities`` shares
    of a $1 equity on 2025-01-10, checked against ``anchor`` shares within
    ``tolerance`` on Dec 31."""
    paired, config, _quantities, options, contract, series = _case(tmp_path)
    account = paired.accounts[0]
    account.evidence.events = ()
    statement = account.activity.primary_bundle.statements[0]
    statement.account_summary.closing_brokerage_cash_usd = Decimal()
    options.trades = options.validated_valuation_anchors = ()
    security = robinhood_security()
    quantities_result = SimpleNamespace(
        blockers=(),
        identity_blockers=(),
        movements=tuple(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=date(2025, 1, 10),
                signed_quantity=Decimal(quantity),
            )
            for quantity in quantities
        ),
        residuals=(),
        anchors=(
            SimpleNamespace(
                account_index=0,
                statement_index=0,
                cusip=security.cusip,
                statement_quantity=Decimal(anchor),
                trade_date_quantity=Decimal(anchor),
                tolerance=Decimal(tolerance),
            ),
        ),
    )
    prices = PriceSeries(
        ticker=Ticker("TEST"),
        currency="USD",
        closes={
            day: Decimal(1) for day in us_market_sessions(date(2025, 1, 10), date(2025, 12, 31))
        },
        has_split_in_range=False,
        source="test",
        fetched_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    case = (paired, config, quantities_result, options, contract, series)
    return case, RobinhoodSecurityIdentities((security,)), {Ticker("TEST"): prices}


def test_equity_ledger_and_value_keep_every_digit(tmp_path: Path) -> None:
    """Buys of 2 and 0.0000000000000000000000000001 shares on one day, which
    the Dec-31 statement holds, are worth $2.0000000000000000000000000001 at
    $1. Main rounded the ledger to 2 shares and refused at the statement."""
    case, securities, prices = _equity_case(
        tmp_path, ("2", _TINY), "2.0000000000000000000000000001", "0"
    )
    peak = _peak_of(case, equity_prices=prices, securities=securities)
    assert (peak.peak_date, peak.peak_usd) == (
        date(2025, 1, 10),
        Decimal("2.0000000000000000000000000001"),
    )


def test_equity_statement_check_compares_the_exact_difference(tmp_path: Path) -> None:
    """A ledger of 1.0000000000000000000000000001 shares is
    1.00000000000000000000000000005 from a statement's 0.00000000000000000000000000005,
    past a 1.00000000000000000000000000001 tolerance, so it refuses. Main
    rounded both the ledger and the difference to 1 and passed (#1267 lists
    this refusal exactness creates)."""
    case, securities, prices = _equity_case(
        tmp_path, ("1", _TINY), "0.00000000000000000000000000005", "1.00000000000000000000000000001"
    )
    with pytest.raises(RobinhoodA2PeakError) as raised:
        _peak_of(case, equity_prices=prices, securities=securities)
    assert str(raised.value) == "Robinhood daily equity inventory misses a statement"


def test_value_past_the_digit_cap_refuses_without_echo(tmp_path: Path) -> None:
    """An option close of 1,000 digits makes a contract's value need more than
    the cap, so A2 refuses with the fixed text. The refusal carries no context
    and chains nothing, and its text, repr and args hold none of the close."""
    case = _case(tmp_path)
    series = _with_close(case[5], date(2025, 3, 3), "47391" * 200)
    with pytest.raises(InvariantError) as raised:
        _peak_of(case, series=series)
    expected = InvariantError(what=_A2_CAP_REFUSAL.removeprefix("InvariantError: "), context=())
    assert_unchained_refusal(raised.value, expected, _A2_CAP_REFUSAL, markers=("47391",))


def test_settlement_basis_statement_check_sums_exactly() -> None:
    """Buys of one contract and 0.0000000000000000000000000001 of one trade on
    2025-01-31 and settle after it, so the statement shows none. The trade-date,
    settlement-date and pending sums and their difference are exact, so the
    pending trades explain the gap and the trade-date quantity is
    1.0000000000000000000000000001. Main rounded it to one contract."""
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 2, 21),
        right="Call",
        strike_usd=Decimal("250"),
    )
    trades = tuple(
        SimpleNamespace(
            contract=contract,
            activity_date=date(2025, 1, 31),
            settle_date=date(2025, 2, 3),
            quantity=Decimal(quantity),
            kind=RobinhoodEventKind.OPTION_BUY_TO_OPEN,
        )
        for quantity in ("1", _TINY)
    )
    anchor = SimpleNamespace(
        contract=contract, statement_date=date(2025, 1, 31), quantity=Decimal()
    )
    quantity = _option_anchor_trade_quantity(
        cast("RobinhoodOptionsResult", SimpleNamespace(trades=trades)),
        cast("RobinhoodOptionValuationAnchor", anchor),
    )
    assert quantity == Decimal("1.0000000000000000000000000001")


_HUGE = "1" + "0" * 28


def test_option_ledger_past_28_digits_does_not_go_negative() -> None:
    """Buys of 1E28 contracts and one more on 2025-05-01, then sales of 1E28 on
    2025-05-02 and the last one on 2025-05-03. The exact ledger holds that one
    contract through 2025-05-02, so those two sessions need closes. Main's
    28-digit price-session replay dropped it and refused for negative
    inventory (#1267 lists this newly accepted class)."""
    contract = RobinhoodOptionContract(
        account_index=0,
        symbol="XQAQ",
        expiration=date(2025, 12, 19),
        right="Call",
        strike_usd=Decimal("100"),
    )
    trades = tuple(
        SimpleNamespace(
            contract=contract,
            activity_date=day,
            settle_date=day,
            quantity=Decimal(quantity),
            kind=kind,
        )
        for day, quantity, kind in (
            (date(2025, 5, 1), _HUGE, RobinhoodEventKind.OPTION_BUY_TO_OPEN),
            (date(2025, 5, 1), "1", RobinhoodEventKind.OPTION_BUY_TO_OPEN),
            (date(2025, 5, 2), _HUGE, RobinhoodEventKind.OPTION_SELL_TO_CLOSE),
            (date(2025, 5, 3), "1", RobinhoodEventKind.OPTION_SELL_TO_CLOSE),
        )
    )
    required = robinhood_required_option_sessions(
        cast(
            "RobinhoodOptionsResult",
            SimpleNamespace(trades=trades, validated_valuation_anchors=()),
        ),
        calendar_year=2025,
    )
    assert required == {contract: frozenset({date(2025, 5, 1), date(2025, 5, 2)})}


def test_equity_ledger_past_28_digits_does_not_go_negative() -> None:
    """The equity ledger holds 1E28 shares and one more bought on 2025-05-01,
    then one after selling 1E28 on 2025-05-02, and none after selling it on
    2025-05-03. Main's 28-digit replay dropped that share and refused for
    negative inventory (#1267 lists this newly accepted class)."""
    security = robinhood_security()
    quantities = SimpleNamespace(
        movements=tuple(
            SimpleNamespace(
                account_index=0,
                cusip=security.cusip,
                activity_date=day,
                signed_quantity=Decimal(quantity),
            )
            for day, quantity in (
                (date(2025, 5, 1), _HUGE),
                (date(2025, 5, 1), "1"),
                (date(2025, 5, 2), "-" + _HUGE),
                (date(2025, 5, 3), "-1"),
            )
        ),
        residuals=(),
        anchors=(),
    )
    replay = replay_robinhood_account_equities(
        account_index=0,
        account=cast("RobinhoodAccountReconciliation", SimpleNamespace()),
        quantities=cast("RobinhoodSecurityQuantityResult", quantities),
        start=date(2025, 5, 1),
        end=date(2025, 5, 3),
    )
    assert replay.daily_quantities == {
        date(2025, 5, 1): {security.cusip: Decimal("1" + "0" * 27 + "1")},
        date(2025, 5, 2): {security.cusip: Decimal(1)},
        date(2025, 5, 3): {},
    }
