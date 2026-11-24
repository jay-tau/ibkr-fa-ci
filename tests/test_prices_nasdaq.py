"""Nasdaq mutual-fund daily NAV fetch tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ibkr_fa.prices.nasdaq import (
    NasdaqPriceFormatError,
    fetch_nasdaq_mutual_fund_daily_closes_usd,
)
from ibkr_fa.prices.series import PriceSourceContradictionError
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_prints_no_marker
from tests._nasdaq_prices import NASDAQ_VALID_ROW as _VALID_ROW
from tests._nasdaq_prices import fetch_nasdaq_rows as _fetch
from tests._nasdaq_prices import nasdaq_payload as _payload
from tests._nasdaq_prices import nasdaq_row_message as _message


def test_fetches_identity_checked_usd_nav_series() -> None:
    seen: list[str] = []

    def open_url(url: str, _timeout: float) -> bytes:
        seen.append(url)
        return _payload()

    result = fetch_nasdaq_mutual_fund_daily_closes_usd(
        Ticker("XQCN"),
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        url_opener=open_url,
    )
    assert result is not None
    assert result.currency == "USD"
    assert result.closes[date(2025, 12, 31)] == Decimal(1)
    assert "assetclass=mutualfunds" in seen[0]
    assert "fromdate=2025-01-01" in seen[0]


def test_refuses_redirected_or_malformed_history() -> None:
    assert (
        fetch_nasdaq_mutual_fund_daily_closes_usd(
            Ticker("XQCN"),
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            url_opener=lambda _url, _timeout: _payload(symbol="OTHER"),
        )
        is None
    )
    assert (
        fetch_nasdaq_mutual_fund_daily_closes_usd(
            Ticker("XQCN"),
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            now=datetime(2026, 1, 1, tzinfo=UTC),
            url_opener=lambda _url, _timeout: b"{}",
        )
        is None
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("1.0000", "1.0001"),
        ("1.0000", "1.0000"),
        ("n/a", "1.0000"),
        ("1.0000", "n/a"),
        ("1.0000", "1,2"),
    ],
    ids=["different", "equal", "first-unusable", "second-unusable", "second-malformed"],
)
def test_two_rows_on_one_date_raise(first: str, second: str) -> None:
    """#942: two rows dated 2 June 2025 contradict each other, even when their
    closes agree or one of them has no usable close. The message names the
    source, but not the symbol or the row date, which are input (#1047). The
    duplicate is found before the second row's close is read (#1138)."""
    with pytest.raises(PriceSourceContradictionError) as exc:
        _fetch(
            {"date": "06/02/2025", "close": first},
            {"date": "06/02/2025", "close": second},
        )
    assert exc.value == PriceSourceContradictionError("Nasdaq")
    message = str(exc.value)
    assert message == (
        "Nasdaq returned two price bars for one date for a holding; the price source "
        "contradicts itself, so its prices are refused"
    )
    for value in ("XQCN", "2025-06-02", "06/02/2025"):
        assert value.lower() not in message.lower()


# #1368's example: a symbol and a repeated date that are markers, kept off the
# lines a traceback prints so that the traceback check tests the error.
_MARKED_SYMBOL = "XQ731942"
_MARKED_ROWS: tuple[dict[str, object], ...] = (
    {"date": "07/29/2031", "close": "1.0000"},
    {"date": "07/29/2031", "close": "1.0001"},
)
_MARKERS = ("731942", "2031-07-29", "2031, 7, 29", "07/29/2031")


def test_two_rows_on_one_date_keep_neither_the_symbol_nor_the_date() -> None:
    """#1368: two rows dated 29 July 2031. The error keeps only the source:
    neither the symbol nor the date is in its ``str``, ``repr``, args, fields,
    traceback or chain."""
    with pytest.raises(PriceSourceContradictionError) as exc:
        fetch_nasdaq_mutual_fund_daily_closes_usd(
            Ticker(_MARKED_SYMBOL),
            start=date(2031, 7, 1),
            end=date(2031, 7, 31),
            now=datetime(2031, 8, 1, tzinfo=UTC),
            url_opener=lambda _url, _timeout: _payload(symbol=_MARKED_SYMBOL, rows=_MARKED_ROWS),
        )
    assert str(exc.value) == (
        "Nasdaq returned two price bars for one date for a holding; the price source "
        "contradicts itself, so its prices are refused"
    )
    assert_prints_no_marker(exc.value, markers=_MARKERS)
    assert exc.value == PriceSourceContradictionError("Nasdaq")


def test_two_rows_on_one_date_before_start_are_ignored() -> None:
    series = _fetch(
        {"date": "12/31/2024", "close": "1.0000"},
        {"date": "12/31/2024", "close": "1.0001"},
        {"date": "01/02/2025", "close": "1.0000"},
    )
    assert series is not None
    assert series.closes == {date(2025, 1, 2): Decimal("1.0000")}


# Every value here is a close or high that isn't in Nasdaq's price format
# (#1138). On cab08f7 each was read as a different price, skipped, or ended
# the fetch with no series.
_MALFORMED: tuple[object, ...] = (
    "1,2",
    "1.0,0",
    ",1",
    "12,345,67",
    "1,23",
    "$1$0",
    "$$1",
    "1$",
    "1e3",
    "1_000",
    "\uff11.00",
    " 1.00",
    "1.00 ",
    "",
    "-1",
    "+1",
    "NaN",
    "Infinity",
    "$0.00",
    1.5,
    True,
    None,
    [],
)


def _refusal(fields: dict[str, object], malformed: object) -> str:
    """The message of the refusal for a row dated 17 June 2025 with ``fields``,
    read after ``_VALID_ROW``. It names no symbol, date, valid close or
    ``malformed`` value, which are input (#1047)."""
    with pytest.raises(NasdaqPriceFormatError) as exc:
        _fetch(_VALID_ROW, {"date": "06/17/2025", **fields})
    message = str(exc.value)
    markers = ["XQCN", "2025-06-17", "06/17/2025", "4.8213"]
    if malformed != "":
        markers.append(malformed if isinstance(malformed, str) else json.dumps(malformed))
    for marker in markers:
        assert marker.lower() not in message.lower()
    return message


@pytest.mark.parametrize("malformed", _MALFORMED)
def test_a_close_not_in_nasdaqs_price_format_refuses(malformed: object) -> None:
    assert _refusal({"close": malformed}, malformed) == _message(2, "close")


@pytest.mark.parametrize("malformed", _MALFORMED)
def test_a_high_not_in_nasdaqs_price_format_refuses(malformed: object) -> None:
    assert _refusal({"close": "$1.00", "high": malformed}, malformed) == _message(2, "high")


def test_a_high_below_its_close_refuses() -> None:
    assert _refusal({"close": "$1.00", "high": "$0.50"}, "$0.50") == _message(
        2, "high", "the high is below the close"
    )


@pytest.mark.parametrize("high", ["1,2", None], ids=["grouped-wrong", "null"])
def test_a_high_is_checked_when_the_close_is_n_a(high: object) -> None:
    assert _refusal({"close": "N/A", "high": high}, high) == _message(2, "high")


def test_a_row_without_a_high_key_has_no_high() -> None:
    series = _fetch(_VALID_ROW, {"date": "06/17/2025", "close": "$1.00"})
    assert series is not None
    assert series.closes == {date(2025, 6, 16): Decimal("4.8213"), date(2025, 6, 17): Decimal(1)}
    assert series.highs == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [("$1.00", "1.00"), ("1", "1"), ("1.0000", "1.0000"), ("$1,234.5678", "1234.5678")],
)
def test_prices_in_nasdaqs_format_read_as_before(text: str, expected: str) -> None:
    series = _fetch({"date": "06/02/2025", "close": text, "high": text})
    assert series is not None
    assert str(series.closes[date(2025, 6, 2)]) == expected
    assert str(series.highs[date(2025, 6, 2)]) == expected


@pytest.mark.parametrize("marker", ["N/A", "n/a"])
def test_an_n_a_close_gives_no_close_for_that_date(marker: str) -> None:
    series = _fetch(_VALID_ROW, {"date": "06/17/2025", "close": marker, "high": "$9.00"})
    assert series is not None
    assert series.closes == {date(2025, 6, 16): Decimal("4.8213")}
    assert series.highs == {}


def test_an_n_a_high_gives_no_high() -> None:
    series = _fetch({"date": "06/17/2025", "close": "$1.00", "high": "N/A"})
    assert series is not None
    assert series.closes == {date(2025, 6, 17): Decimal(1)}
    assert series.highs == {}


def test_a_malformed_first_row_on_a_repeated_date_refuses_before_the_duplicate() -> None:
    with pytest.raises(NasdaqPriceFormatError) as exc:
        _fetch({"date": "06/02/2025", "close": "1,2"}, {"date": "06/02/2025", "close": "$1.00"})
    assert str(exc.value) == _message(1, "close")


def test_malformed_prices_before_start_are_not_read() -> None:
    series = _fetch(
        {"date": "12/31/2024", "close": "1,2", "high": "1,2"},
        {"date": "01/02/2025", "close": "1.0000"},
    )
    assert series is not None
    assert series.closes == {date(2025, 1, 2): Decimal("1.0000")}


def test_a_row_before_start_without_a_close_key_still_ends_the_fetch() -> None:
    assert _fetch({"date": "12/30/2024"}, {"date": "06/02/2025", "close": "$1.00"}) is None
