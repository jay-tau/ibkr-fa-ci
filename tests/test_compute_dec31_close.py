"""Tests for the NetBenefits Dec-31 closing-price resolver (#310)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Context, Decimal, Inexact, Rounded, localcontext

import pytest

from ibkr_fa.compute.dec31_close import (
    CloseSource,
    ClosingPriceError,
    ResolvedClose,
    resolve_dec31_close,
)
from ibkr_fa.exceptions import InvariantError
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.reference import us_market_calendar
from ibkr_fa.types import Ticker
from tests._dec31_close import (
    MARKER_PRICE,
    MARKER_REASON,
    MARKER_SOURCE,
    MARKER_SOURCE_DATE,
    MARKER_SYMBOL,
    MARKET_SESSION_REFUSAL,
    OVERRIDE_SESSION_REFUSAL,
    assert_markers_absent,
    unsupported_year_refusal,
)
from tests._dec31_close import assert_absent as _assert_absent
from tests._exact_refusals import INEXACT_PRODUCT, INEXACT_SUM

# Synthetic lot acquired 2025-12-31, 3.482 sh, Dec-31 close 213.47 ->
# holding total 743.30 (== the purchase-day FMV total).
_DEC31 = date(2025, 12, 31)
_QTY = Decimal("3.482")
_CLOSE = Decimal("213.47")
_STATEMENT_TOTAL = Decimal("743.30")


def _series(
    closes: dict[date, Decimal],
    *,
    currency: str = "USD",
    has_split_in_range: bool = False,
    ticker: str = "XESP",
) -> PriceSeries:
    return PriceSeries(
        ticker=Ticker(ticker),
        currency=currency,
        closes=closes,
        has_split_in_range=has_split_in_range,
        source="test",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _mismatch(src_a: CloseSource, src_b: CloseSource) -> str:
    return (
        f"Dec-31 close mismatch: the {src_a.value} and {src_b.value} totals differ by "
        "more than the tolerance; refusing rather than file an unverified closing value"
    )


# Fixed text (#1047): the configured carry-back window and the CY's Dec 31
# only, never the override's own priced_on date.
_OVERRIDE_OUT_OF_WINDOW = "the price override's date is not within 3 days on/before 2025-12-31"


@pytest.fixture
def dec31_closed(monkeypatch: pytest.MonkeyPatch) -> date:
    """A controlled calendar: CY2025 with 2025-12-31 listed as a closure, so
    the required session is Tuesday 2025-12-30. Returns that session."""
    monkeypatch.setattr(us_market_calendar, "_CLOSURES", us_market_calendar._CLOSURES | {_DEC31})
    return date(2025, 12, 30)


# --- acceptance paths -----------------------------------------------------------


def test_yahoo_fallback_resolves_to_the_dec31_close() -> None:
    # Statement unpriced ("-"), no override -> flagged market fallback.
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=None,
        series=_series({_DEC31: _CLOSE}),
    )
    assert resolved == ResolvedClose(
        total_usd=_CLOSE * _QTY,
        per_share_usd=_CLOSE,
        source=CloseSource.YAHOO_FALLBACK,
        provenance=resolved.provenance,
    )
    assert resolved.per_share_usd == Decimal("213.47")
    assert "not broker-stated" in resolved.provenance


def test_statement_primary_when_present_and_yahoo_agrees() -> None:
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=_STATEMENT_TOTAL,
        series=_series({_DEC31: _CLOSE}),
    )
    assert resolved.source is CloseSource.STATEMENT
    assert resolved.total_usd == _STATEMENT_TOTAL
    assert resolved.per_share_usd is None


def test_statement_vs_yahoo_mismatch_refuses() -> None:
    """The refusal names the two sources, not their totals, the
    difference or the tolerance (#1047)."""
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=Decimal("950.00"),
            series=_series({_DEC31: _CLOSE}),
        )
    assert str(exc.value) == _mismatch(CloseSource.STATEMENT, CloseSource.YAHOO_FALLBACK)
    # Statement total, Yahoo total (213.47 x 3.482), their difference and
    # the default tolerance (3.482 x 0.005 + 0.01).
    _assert_absent(str(exc.value), "950", "743.30254", "206.69746", "0.02741")


def test_network_failure_with_no_fallback_refuses() -> None:
    with pytest.raises(ClosingPriceError, match="no Dec-31 close available"):
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=None,
        )


@pytest.mark.parametrize(("remedy", "suffix"), [(None, ""), ("ask the caller", "; ask the caller")])
def test_no_source_refusal_names_no_config_table(remedy: str | None, suffix: str) -> None:
    # #1116: the resolver can't know which setting, if any, supplies a close, so
    # the caller's remedy follows the fixed text.
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=None,
            missing_source_remedy=remedy,
        )
    assert str(exc.value) == (
        "no Dec-31 close available for 2025-12-31: there is no statement total, no override "
        f"and no usable Yahoo close{suffix}"
    )


def test_non_session_dec31_uses_the_last_session_close(dec31_closed: date) -> None:
    # #1165: with Dec-31 a non-session day, the required session is the last
    # one before it, and its close is the filed per-share price.
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=Decimal("2"),
        statement_total_usd=None,
        series=_series({date(2025, 12, 29): Decimal("1"), dec31_closed: Decimal("205.00")}),
    )
    assert resolved.source is CloseSource.YAHOO_FALLBACK
    assert resolved.per_share_usd == Decimal("205.00")
    assert resolved.total_usd == Decimal("410.00")


def test_bar_after_the_session_refuses(dec31_closed: date) -> None:
    # #1165: a bar dated on the non-session Dec-31 contradicts the calendar, so
    # the session close beside it is refused, not filed.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("2"),
            statement_total_usd=None,
            series=_series({dec31_closed: Decimal("205.00"), _DEC31: Decimal("205.00")}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


def test_session_beyond_the_staleness_window_refuses(dec31_closed: date) -> None:
    # #1165: the required session must still sit within the staleness ceiling.
    with pytest.raises(ClosingPriceError) as market:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("2"),
            statement_total_usd=None,
            series=_series({dec31_closed: Decimal("205.00")}),
            max_staleness_days=0,
        )
    assert str(market.value) == MARKET_SESSION_REFUSAL
    with pytest.raises(ClosingPriceError) as override:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("2"),
            statement_total_usd=None,
            series=None,
            override_per_share_usd=Decimal("205.00"),
            override_priced_on=dec31_closed,
            max_staleness_days=0,
        )
    assert str(override.value) == (
        "the price override's date is not within 0 days on/before 2025-12-31"
    )


# --- required session (#1165) --------------------------------------------------


def test_market_close_from_an_earlier_session_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    # 2025-12-31 is a session: the Dec-30 close is not its close.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("1"),
            statement_total_usd=None,
            series=_series({date(2025, 12, 30): Decimal(MARKER_PRICE)}, ticker=MARKER_SYMBOL),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL
    assert_markers_absent(caught.value, MARKER_SYMBOL, MARKER_PRICE, MARKER_SOURCE_DATE)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "series",
    [None, _series({_DEC31: Decimal(MARKER_PRICE)}, ticker=MARKER_SYMBOL)],
    ids=["no-market-evidence", "valid-session-bar"],
)
def test_override_off_the_required_session_refuses(
    series: PriceSeries | None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Never falls through to the valid market close.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("1"),
            statement_total_usd=None,
            series=series,
            override_per_share_usd=Decimal(MARKER_PRICE),
            override_priced_on=date(2025, 12, 30),
            override_label=f"{MARKER_SOURCE}: {MARKER_REASON}",
        )
    assert str(caught.value) == OVERRIDE_SESSION_REFUSAL
    assert_markers_absent(
        caught.value, MARKER_PRICE, MARKER_SOURCE, MARKER_REASON, MARKER_SOURCE_DATE
    )
    if series is not None:
        assert_markers_absent(caught.value, MARKER_SYMBOL)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_non_session_bar_without_the_session_bar_refuses() -> None:
    # Sunday 2025-12-28 lies inside the old three-day window.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({date(2025, 12, 28): _CLOSE}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


@pytest.mark.parametrize(
    ("valuation_date", "bar_date", "override", "statement"),
    [
        (date(2024, 12, 31), date(2024, 12, 31), False, None),
        (date(2024, 12, 31), date(2024, 12, 30), False, None),
        (date(2023, 12, 31), date(2023, 12, 31), False, None),
        (date(2024, 12, 31), None, True, None),
        (date(2024, 12, 31), date(2024, 12, 31), False, _STATEMENT_TOTAL),
    ],
    ids=[
        "cy2024-session-bar",
        "cy2024-dec30-bar",
        "cy2023-sunday-bar",
        "cy2024-override",
        "cy2024-statement-cross-check",
    ],
)
def test_unsupported_year_refuses_per_share_evidence(
    valuation_date: date,
    bar_date: date | None,
    override: bool,
    statement: Decimal | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    price = Decimal(MARKER_PRICE)
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=valuation_date,
            quantity=_QTY,
            statement_total_usd=price * _QTY if statement is not None else None,
            series=None if bar_date is None else _series({bar_date: price}, ticker=MARKER_SYMBOL),
            override_per_share_usd=price if override else None,
            override_priced_on=valuation_date if override else None,
            override_label=f"{MARKER_SOURCE}: {MARKER_REASON}" if override else "",
        )
    assert str(caught.value) == unsupported_year_refusal(valuation_date.year)
    assert_markers_absent(caught.value, MARKER_PRICE)
    if bar_date is not None:
        assert_markers_absent(caught.value, MARKER_SYMBOL, bar_date.isoformat())
    if override:
        assert_markers_absent(
            caught.value, MARKER_SOURCE, MARKER_REASON, valuation_date.isoformat()
        )
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "series",
    [None, _series({date(2024, 12, 31): _CLOSE}, has_split_in_range=True)],
    ids=["no-series", "split-series"],
)
def test_statement_only_resolution_needs_no_calendar(series: PriceSeries | None) -> None:
    # Owner decision (#1165): only per-share resolution is gated on the
    # calendar year; a statement total alone keeps its existing checks.
    resolved = resolve_dec31_close(
        valuation_date=date(2024, 12, 31),
        quantity=_QTY,
        statement_total_usd=_STATEMENT_TOTAL,
        series=series,
    )
    assert resolved.source is CloseSource.STATEMENT
    assert resolved.total_usd == _STATEMENT_TOTAL


def test_earlier_session_refusal_precedes_the_mismatch() -> None:
    # Refused today as a statement/Yahoo mismatch; the session refusal now
    # fires first, so it still refuses.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=Decimal("950.00"),
            series=_series({date(2025, 12, 30): _CLOSE}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


def test_unsupported_year_refusal_precedes_the_mismatch() -> None:
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=date(2024, 12, 31),
            quantity=_QTY,
            statement_total_usd=Decimal("950.00"),
            series=_series({date(2024, 12, 31): _CLOSE}),
        )
    assert str(caught.value) == unsupported_year_refusal(2024)


def test_split_refusal_precedes_the_unsupported_year() -> None:
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=date(2024, 12, 31),
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({date(2024, 12, 31): _CLOSE}, has_split_in_range=True),
        )
    assert str(caught.value) == (
        "split in range for 2024-12-31: Yahoo and any per-share override (x the current, "
        "rebased quantity) are unsafe and there is no broker statement total to fall back on; "
        "the price cache holds this security's closes on the current-split basis, so move its "
        "cache file, and any .lock beside it, out of the price cache directory and rerun with "
        "price fetching on"
    )


def test_override_session_refusal_precedes_the_market_refusal() -> None:
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({date(2025, 12, 30): _CLOSE}),
            override_per_share_usd=_CLOSE,
            override_priced_on=date(2025, 12, 30),
        )
    assert str(caught.value) == OVERRIDE_SESSION_REFUSAL


# --- override -------------------------------------------------------------------


def test_override_used_and_outranks_yahoo() -> None:
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=None,
        series=_series({_DEC31: _CLOSE}),
        override_per_share_usd=_CLOSE,
        override_priced_on=_DEC31,
        override_label="SPS year-end statement",
    )
    assert resolved.source is CloseSource.OVERRIDE
    assert resolved.per_share_usd == _CLOSE
    assert "SPS year-end statement" in resolved.provenance


def test_override_on_the_last_session_before_a_non_session_dec31(dec31_closed: date) -> None:
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=Decimal("2"),
        statement_total_usd=None,
        series=None,
        override_per_share_usd=Decimal("205.00"),
        override_priced_on=dec31_closed,
    )
    assert resolved.source is CloseSource.OVERRIDE
    assert resolved.total_usd == Decimal("410.00")
    assert resolved.provenance.endswith("(unlabelled)")


def test_override_out_of_window_refuses() -> None:
    """The refusal doesn't print the override's priced_on date (#1047)."""
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=None,
            override_per_share_usd=_CLOSE,
            override_priced_on=date(2025, 12, 20),
        )
    assert str(exc.value) == _OVERRIDE_OUT_OF_WINDOW
    _assert_absent(str(exc.value), "2025-12-20", "priced_on")


def test_out_of_window_override_refuses_beside_a_statement() -> None:
    # #1165: a supplied override is per-share evidence, so an unusable one
    # refuses instead of silently dropping out beside the statement total.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=_STATEMENT_TOTAL,
            series=None,
            override_per_share_usd=_CLOSE,
            override_priced_on=date(2025, 12, 20),
        )
    assert str(caught.value) == _OVERRIDE_OUT_OF_WINDOW


def test_out_of_window_override_outranks_yahoo_and_refuses() -> None:
    # A supplied-but-unusable override outranks the Yahoo fallback: surface the
    # config problem rather than silently filing a market price instead.
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({_DEC31: _CLOSE}),
            override_per_share_usd=_CLOSE,
            override_priced_on=date(2025, 12, 20),
        )
    assert str(exc.value) == _OVERRIDE_OUT_OF_WINDOW
    assert "2025-12-20" not in str(exc.value)


def test_override_future_date_refuses() -> None:
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=None,
            override_per_share_usd=_CLOSE,
            override_priced_on=date(2026, 1, 2),
        )
    assert str(exc.value) == _OVERRIDE_OUT_OF_WINDOW
    assert "2026-01-02" not in str(exc.value)


def test_override_missing_priced_on_refuses() -> None:
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=None,
            override_per_share_usd=_CLOSE,
            override_priced_on=None,
        )
    assert str(exc.value) == _OVERRIDE_OUT_OF_WINDOW
    assert "None" not in str(exc.value)


def test_override_vs_statement_disagreement_refuses() -> None:
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=_STATEMENT_TOTAL,
            series=None,
            override_per_share_usd=Decimal("500.00"),
            override_priced_on=_DEC31,
        )
    assert str(exc.value) == _mismatch(CloseSource.STATEMENT, CloseSource.OVERRIDE)
    # Statement total, override total (500.00 x 3.482) and their difference.
    _assert_absent(str(exc.value), "743.30", "1741", "997.7")


def test_all_three_sources_agree_picks_statement() -> None:
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=_STATEMENT_TOTAL,
        series=_series({_DEC31: _CLOSE}),
        override_per_share_usd=_CLOSE,
        override_priced_on=_DEC31,
    )
    assert resolved.source is CloseSource.STATEMENT


# --- split / currency / staleness disqualify Yahoo ------------------------------


def test_split_with_only_yahoo_refuses() -> None:
    with pytest.raises(ClosingPriceError, match="split in range"):
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({_DEC31: _CLOSE}, has_split_in_range=True),
        )


def test_split_falls_back_to_statement_total() -> None:
    # A split is split-neutral for the broker-stated TOTAL, so it still resolves.
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=_STATEMENT_TOTAL,
        series=_series({_DEC31: _CLOSE}, has_split_in_range=True),
    )
    assert resolved.source is CloseSource.STATEMENT
    assert resolved.total_usd == _STATEMENT_TOTAL


def test_split_with_statement_uses_statement_even_with_override() -> None:
    # Under a split the statement TOTAL is split-neutral and authoritative; the
    # (now-unsafe) per-share override is disqualified, not a refusal trigger.
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=_STATEMENT_TOTAL,
        series=_series({_DEC31: _CLOSE}, has_split_in_range=True),
        override_per_share_usd=_CLOSE,
        override_priced_on=_DEC31,
    )
    assert resolved.source is CloseSource.STATEMENT
    assert resolved.total_usd == _STATEMENT_TOTAL


def test_split_with_override_and_no_statement_refuses() -> None:
    with pytest.raises(ClosingPriceError, match="split in range"):
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({_DEC31: _CLOSE}, has_split_in_range=True),
            override_per_share_usd=_CLOSE,
            override_priced_on=_DEC31,
        )


def test_non_usd_series_refuses() -> None:
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({_DEC31: _CLOSE}, currency="EUR"),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


def test_stale_yahoo_close_refuses() -> None:
    # The nearest prior close is 30 days old: no close for the required session.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({date(2025, 12, 1): _CLOSE}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


def test_year_end_staleness_default_rejects_week_old_close() -> None:
    # A Christmas-week data hole refuses rather than filing a week-old close as
    # the year-end value.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({date(2025, 12, 26): _CLOSE}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


@pytest.mark.parametrize(
    "bad_close", [Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")]
)
@pytest.mark.parametrize("statement", [None, _STATEMENT_TOTAL], ids=["alone", "with-statement"])
def test_non_finite_or_non_positive_session_close_refuses(
    bad_close: Decimal, statement: Decimal | None
) -> None:
    # A corrupt cache / provider value must never reach a filed figure, nor
    # silently drop out of the cross-check beside a statement total.
    with pytest.raises(ClosingPriceError) as caught:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=statement,
            series=_series({_DEC31: bad_close}),
        )
    assert str(caught.value) == MARKET_SESSION_REFUSAL


# --- input validation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "what", "bad"),
    [
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=Decimal("0"),
                statement_total_usd=_STATEMENT_TOTAL,
                series=_series({_DEC31: _CLOSE}),
            ),
            "quantity",
            "0",
        ),
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=Decimal("-1"),
                statement_total_usd=_STATEMENT_TOTAL,
                series=_series({_DEC31: _CLOSE}),
            ),
            "quantity",
            "-1",
        ),
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=Decimal("NaN"),
                statement_total_usd=_STATEMENT_TOTAL,
                series=_series({_DEC31: _CLOSE}),
            ),
            "quantity",
            "NaN",
        ),
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=Decimal("0"),
                series=_series({_DEC31: _CLOSE}),
            ),
            "statement_total_usd",
            "0",
        ),
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=Decimal("-5"),
                series=_series({_DEC31: _CLOSE}),
            ),
            "statement_total_usd",
            "-5",
        ),
        (
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=_STATEMENT_TOTAL,
                series=_series({_DEC31: _CLOSE}),
                override_per_share_usd=Decimal("0"),
                override_priced_on=_DEC31,
            ),
            "override_per_share_usd",
            "0",
        ),
    ],
    ids=[
        "quantity-zero",
        "quantity-negative",
        "quantity-nan",
        "statement-total-zero",
        "statement-total-negative",
        "override-zero",
    ],
)
def test_non_positive_or_non_finite_inputs_refuse(
    call: Callable[[], ResolvedClose], what: str, bad: str
) -> None:
    """The refusal names the input, not its value (#1047)."""
    with pytest.raises(ClosingPriceError) as exc:
        call()
    assert str(exc.value) == f"{what} must be a positive, finite amount"
    assert f"got {bad}" not in str(exc.value)


def test_custom_tolerance_can_reject_a_sub_dollar_drift() -> None:
    # 213.47 * 3.482 = 743.30254 vs statement 743.30 -> diff 0.00254; a $0.001
    # tolerance refuses it (the default, 3.482 * 0.005 + 0.01 = 0.02741, accepts it).
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=_STATEMENT_TOTAL,
            series=_series({_DEC31: _CLOSE}),
            tolerance=Decimal("0.001"),
        )
    assert str(exc.value) == _mismatch(CloseSource.STATEMENT, CloseSource.YAHOO_FALLBACK)
    _assert_absent(str(exc.value), "743.30", "0.00254", "0.001")


def test_negative_staleness_refuses() -> None:
    # An invalid window must be a clear input error, not a confusing
    # "no close available" false refusal.
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=None,
            series=_series({_DEC31: _CLOSE}),
            max_staleness_days=-1,
        )
    assert str(exc.value) == "max_staleness_days must be >= 0"


@pytest.mark.parametrize("bad_tol", [Decimal("Infinity"), Decimal("NaN"), Decimal("-1")])
def test_invalid_tolerance_refuses(bad_tol: Decimal) -> None:
    # A non-finite/negative tolerance must refuse (an Infinity would otherwise
    # silently defeat the mismatch guard; a NaN would raise a low-level error).
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=_QTY,
            statement_total_usd=Decimal("950.00"),
            series=_series({_DEC31: _CLOSE}),
            tolerance=bad_tol,
        )
    assert str(exc.value) == "tolerance must be a non-negative, finite amount"


# --- exact totals and cross-check (#1250) ---------------------------------------


def test_override_total_keeps_every_digit_in_any_context() -> None:
    """The override total is the exact 33-digit product of a 30-digit price
    and 3.482 shares, also at a hostile 3-digit context that traps rounding.
    Main rounded it to 28 digits, 743.3025400000000000000000000 (#1250)."""
    price = Decimal("213.470000000000000000000000001")
    exact = Decimal("743.302540000000000000000000003482")
    for context in (Context(), Context(prec=3, traps=[Inexact, Rounded])):
        with localcontext(context):
            resolved = resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=None,
                series=None,
                override_per_share_usd=price,
                override_priced_on=_DEC31,
            )
        assert resolved.source is CloseSource.OVERRIDE
        assert resolved.per_share_usd == price
        assert resolved.total_usd.compare_total(exact) == 0


def test_yahoo_total_keeps_every_digit() -> None:
    """The Yahoo total is the exact 33-digit product of a 30-digit close and
    3.482 shares. Main rounded it to 28 digits, 743.3025400000000000000000000
    (#1250)."""
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=_QTY,
        statement_total_usd=None,
        series=_series({_DEC31: Decimal("213.469999999999999999999999999")}),
    )
    assert resolved.source is CloseSource.YAHOO_FALLBACK
    assert resolved.total_usd.compare_total(Decimal("743.302539999999999999999999996518")) == 0


def test_cross_check_accepts_an_exact_difference_on_the_tolerance() -> None:
    """One share: the override and Yahoo totals differ by exactly the default
    0.015 tolerance, so they agree and the override files every digit. Main
    rounded each total to 28 digits (1000.005000000000000000000001 and
    999.9900000000000000000000005), a difference of 0.0150000000000000000000005,
    and refused on that rounding artefact. The tolerance edge #1250 newly
    accepts."""
    override = Decimal("1000.005000000000000000000000500001")
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=Decimal("1"),
        statement_total_usd=None,
        series=_series({_DEC31: Decimal("999.990000000000000000000000500001")}),
        override_per_share_usd=override,
        override_priced_on=_DEC31,
    )
    assert resolved.source is CloseSource.OVERRIDE
    assert resolved.per_share_usd == override
    assert resolved.total_usd.compare_total(Decimal("1000.005000000000000000000000500001")) == 0


def test_cross_check_keeps_a_long_difference_on_an_exact_tolerance() -> None:
    """The difference needs 31 digits. Main rounded the Yahoo total up and
    refused; rounding only the subtraction or abs would also refuse it."""
    price = Decimal("1000.00499999999999999999999999999999")
    resolved = resolve_dec31_close(
        valuation_date=_DEC31,
        quantity=Decimal("1"),
        statement_total_usd=Decimal("999.99"),
        series=_series({_DEC31: price}),
        tolerance=Decimal("0.01499999999999999999999999999999"),
    )
    assert resolved.source is CloseSource.STATEMENT
    assert resolved.total_usd.compare_total(Decimal("999.99")) == 0


def test_cross_check_refuses_an_exact_difference_past_the_tolerance() -> None:
    """One share: the override total is 0.01500000000000000000000000000001 above
    Yahoo's 999.99, just past the default 0.015 tolerance. Main rounded the
    override total to 1000.005, on the tolerance, and filed it (#1250).
    Rounding only the subtraction or its absolute value would also pass it."""
    with pytest.raises(ClosingPriceError) as exc:
        resolve_dec31_close(
            valuation_date=_DEC31,
            quantity=Decimal("1"),
            statement_total_usd=None,
            series=_series({_DEC31: Decimal("999.99")}),
            override_per_share_usd=Decimal("1000.00500000000000000000000000000001"),
            override_priced_on=_DEC31,
        )
    assert str(exc.value) == (
        "Dec-31 close mismatch: the override and yahoo_fallback totals differ by more than the "
        "tolerance; refusing rather than file an unverified closing value"
    )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        pytest.param(
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=None,
                series=None,
                override_per_share_usd=Decimal("1" * 999),
                override_priced_on=_DEC31,
            ),
            f"Dec-31 close, override total: {INEXACT_PRODUCT}",
            id="override-total",
        ),
        pytest.param(
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=_QTY,
                statement_total_usd=None,
                series=_series({_DEC31: Decimal("1" * 999)}),
            ),
            f"Dec-31 close, Yahoo total: {INEXACT_PRODUCT}",
            id="yahoo-total",
        ),
        # Both totals are the 1000-digit quantity itself, but quantity x 0.005
        # needs 1001 digits.
        pytest.param(
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=Decimal("9" * 1000),
                statement_total_usd=None,
                series=_series({_DEC31: Decimal("1")}),
                override_per_share_usd=Decimal("1"),
                override_priced_on=_DEC31,
            ),
            f"Dec-31 close, cross-check tolerance: {INEXACT_PRODUCT}",
            id="cross-check-tolerance",
        ),
        # 1E+990 - 1E-20 needs 1011 digits; the tolerance covers it, so only
        # the cap refuses.
        pytest.param(
            lambda: resolve_dec31_close(
                valuation_date=_DEC31,
                quantity=Decimal("1"),
                statement_total_usd=Decimal("1E+990"),
                series=_series({_DEC31: Decimal("1E-20")}),
                tolerance=Decimal("1E+991"),
            ),
            f"Dec-31 close, cross-check difference: {INEXACT_SUM}",
            id="cross-check-difference",
        ),
    ],
)
def test_exact_close_arithmetic_refuses_past_the_cap(
    call: Callable[[], ResolvedClose], message: str
) -> None:
    """Each exact total, default tolerance and difference refuses past the
    1000-digit cap with its fixed label, no context and no chained error.
    Main rounded each to 28 digits and resolved a close (#1250)."""
    with pytest.raises(InvariantError) as exc:
        call()
    assert str(exc.value) == f"InvariantError: {message}"
    assert exc.value.context == ()
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
