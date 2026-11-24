"""Unit tests for :mod:`ibkr_fa.compute.fa_a3_dividends` (#626)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ibkr_fa.compute.fa_a3_dividends import classify_orphan_dividends, resolve_fa_a3_dividends
from ibkr_fa.exceptions import MissingFxRateError
from ibkr_fa.fx.policy_fa import FaExactDatePolicy
from ibkr_fa.models import (
    BasisProvenance,
    ClosedLot,
    Dividend,
    DividendEvidence,
    OpenLot,
    Side,
)
from ibkr_fa.models.fa_orphan_dividend import OrphanDividendInfo
from ibkr_fa.types import CountryCode, Isin, Ticker

_ISIN = Isin("US99971K1024")
_FX = FaExactDatePolicy(max_carry_forward_days=14)


def _dividend(symbol: str, payment_date: date, amount: str = "5") -> Dividend:
    return Dividend(
        symbol=Ticker(symbol),
        isin=_ISIN,
        payment_date=payment_date,
        amount_gross=Decimal(amount),
        currency="USD",
        description=f"{symbol}({_ISIN}) Cash Dividend USD 0.50 per Share",
    )


def _open_lot(symbol: str, side: Side) -> OpenLot:
    return OpenLot(
        currency_primary="USD",
        symbol=Ticker(symbol),
        description=symbol,
        isin=_ISIN,
        issuer_country_code=CountryCode("US"),
        report_date=date(2025, 12, 31),
        quantity=Decimal("10"),
        mark_price=Decimal("100"),
        cost_basis_money=Decimal("950"),
        side=side,
        open_date_time=date(2025, 6, 1),
    )


def _closed_lot(symbol: str, close_date: date) -> ClosedLot:
    return ClosedLot(
        currency_primary="USD",
        sub_category="COMMON",
        symbol=Ticker(symbol),
        description=symbol,
        isin=_ISIN,
        listing_exchange="NASDAQ",
        issuer_country_code=CountryCode("US"),
        quantity=Decimal("4"),
        cost_basis_money=Decimal("300"),
        proceeds_money=Decimal("320"),
        open_date_time=date(2025, 1, 15),
        close_date_time=close_date,
        gross_proceeds_money=None,
        basis_provenance=BasisProvenance.ENGINE_FIFO,
    )


def _classify(
    totals: dict[Ticker, Decimal] | None,
    *,
    a3_symbols: frozenset[Ticker] = frozenset(),
    dividends: tuple[Dividend, ...] = (),
    open_lots: tuple[OpenLot, ...] = (),
    closed_lots: tuple[ClosedLot, ...] = (),
) -> tuple[OrphanDividendInfo, ...]:
    return classify_orphan_dividends(
        totals,
        a3_symbols=a3_symbols,
        dividends=dividends,
        open_lots=open_lots,
        closed_lots=closed_lots,
        calendar_year=2025,
    )


def test_unresolved_dividends_have_no_orphans() -> None:
    assert _classify(None, dividends=(_dividend("ORPH", date(2025, 3, 1)),)) == ()


def test_symbols_with_an_a3_row_are_not_orphans() -> None:
    totals = {Ticker("HELD"): Decimal("440")}
    dividends = (_dividend("HELD", date(2025, 3, 1)),)
    assert _classify(totals, a3_symbols=frozenset({Ticker("HELD")}), dividends=dividends) == ()


def test_previously_held_orphan_carries_the_latest_close() -> None:
    totals = {Ticker("SOLD"): Decimal("440")}
    orphans = _classify(
        totals,
        dividends=(_dividend("SOLD", date(2025, 3, 1)),),
        closed_lots=(
            _closed_lot("SOLD", date(2025, 5, 2)),
            _closed_lot("SOLD", date(2025, 9, 30)),
            _closed_lot("SOLD", date(2025, 7, 1)),
        ),
    )
    assert orphans == (
        OrphanDividendInfo(
            symbol=Ticker("SOLD"),
            classification="previously_held",
            total_amount_inr=Decimal("440"),
            latest_payment_date=date(2025, 3, 1),
            last_sold_date=date(2025, 9, 30),
        ),
    )


def test_long_open_lot_counts_as_held_without_a_sale() -> None:
    orphans = _classify(
        {Ticker("LONG"): Decimal("440")},
        dividends=(_dividend("LONG", date(2025, 3, 1)),),
        open_lots=(_open_lot("LONG", Side.LONG),),
    )
    assert [(o.classification, o.last_sold_date) for o in orphans] == [("previously_held", None)]


def test_short_only_payment_in_lieu_is_never_held() -> None:
    """T3 PR #170 P2: a SHORT open is borrowed, not owned and sold."""
    orphans = _classify(
        {Ticker("SHRTSYM"): Decimal("440")},
        dividends=(_dividend("SHRTSYM", date(2025, 9, 30)),),
        open_lots=(_open_lot("SHRTSYM", Side.SHORT),),
    )
    assert [(o.classification, o.last_sold_date) for o in orphans] == [("never_held", None)]


def test_latest_payment_date_ignores_payments_outside_the_calendar_year() -> None:
    orphans = _classify(
        {Ticker("ORPH"): Decimal("440")},
        dividends=(
            _dividend("ORPH", date(2025, 3, 1)),
            _dividend("ORPH", date(2025, 11, 30)),
            _dividend("ORPH", date(2026, 1, 15)),
            _dividend("ORPH", date(2024, 12, 31)),
        ),
    )
    assert [o.latest_payment_date for o in orphans] == [date(2025, 11, 30)]


def test_orphans_are_sorted_by_symbol() -> None:
    symbols = ("XQJA", "XQHA", "XQHB")
    orphans = _classify(
        {Ticker(symbol): Decimal("440") for symbol in symbols},
        dividends=tuple(_dividend(symbol, date(2025, 3, 1)) for symbol in symbols),
    )
    assert [str(o.symbol) for o in orphans] == ["XQHA", "XQHB", "XQJA"]


# ---- resolve_fa_a3_dividends ------------------------------------------------


def _resolve(
    dividends: list[Dividend], evidence: DividendEvidence, *, calendar_year: int = 2025
) -> tuple[dict[Ticker, Decimal] | None, bool]:
    return resolve_fa_a3_dividends(
        dividends, calendar_year=calendar_year, fx_policy=_FX, dividend_evidence=evidence
    )


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (DividendEvidence.NOT_DETERMINABLE, (None, False)),  # Flex Query
        (DividendEvidence.ABSENT, ({}, True)),
        (DividendEvidence.PRESENT, (None, False)),  # a section with no parsed rows
    ],
)
def test_empty_dividends_prove_zero_only_when_evidence_is_absent(
    evidence: DividendEvidence, expected: tuple[dict[Ticker, Decimal] | None, bool]
) -> None:
    assert _resolve([], evidence) == expected


def test_in_year_dividends_aggregate_at_the_december_31_rate() -> None:
    dividends = [_dividend("ORPH", date(2025, 3, 1)), _dividend("ORPH", date(2025, 6, 1), "2")]
    assert _resolve(dividends, DividendEvidence.PRESENT) == (
        {Ticker("ORPH"): Decimal("7") * _FX.rate_for(date(2025, 12, 31))},
        False,
    )


def test_an_aggregate_with_no_in_year_payment_is_manual_fill() -> None:
    dividends = [_dividend("ORPH", date(2024, 12, 31))]
    for evidence in DividendEvidence:
        assert _resolve(dividends, evidence) == (None, False)


def test_a_missing_december_31_rate_raises() -> None:
    """#699: a dividend that needs a missing Dec-31 TTBR refuses, never manual fill."""
    with pytest.raises(MissingFxRateError):
        _resolve(
            [_dividend("ORPH", date(2099, 3, 16))], DividendEvidence.PRESENT, calendar_year=2099
        )
