"""Tests for :mod:`ibkr_fa.prices.yahoo` (issue #14).

The HTTP transport is injected (``url_opener``) so no test touches the
network; the real transport is called with ``urllib.request.urlopen``
patched. Verifies: raw ``quote.close`` is used (never ``adjclose``),
exchange-timezone date conversion, currency passthrough, split
detection, ``end``-window filtering, and that a transport error, a
provider error object or an empty series returns ``None``, while a
malformed response raises ``YahooResponseFormatError`` (#1139) and two
bars on one date on or before ``end`` raise (#942).
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import urllib.response
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from email.message import Message
from functools import partial

import pytest

from ibkr_fa.prices import yahoo
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSourceContradictionError
from ibkr_fa.prices.yahoo import (
    YahooPriceFailureKind,
    YahooPriceFetchResult,
    YahooResponseFormatError,
    _default_url_opener_result,
    _YahooTransportResult,
    fetch_daily_closes_usd,
    fetch_daily_closes_usd_result,
    fetch_daily_closes_usd_retrying,
)
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_prints_no_marker, assert_unchained_refusal
from tests._entity_transport import (  # noqa: F401 - autouse fixture
    close_http_errors as _close_http_errors,
)
from tests._entity_transport import http_error
from tests._yahoo_prices import EXACT_DAY as _EXACT_DAY
from tests._yahoo_prices import MONDAY as _MONDAY
from tests._yahoo_prices import NO_ZONE_REFUSAL, YAHOO_NOW, repeated_date_payload, split_fetcher
from tests._yahoo_prices import NOT_JSON_REFUSAL as _NOT_JSON_REFUSAL
from tests._yahoo_prices import SPLIT_DAY as _SPLIT_DAY
from tests._yahoo_prices import SPLIT_KEY as _SPLIT_KEY
from tests._yahoo_prices import TUESDAY as _TUESDAY
from tests._yahoo_prices import bar_epoch as _epoch
from tests._yahoo_prices import chart_payload as _payload
from tests._yahoo_prices import epoch_at as _at
from tests._yahoo_prices import fetch_payload as _fetch
from tests._yahoo_prices import one_split as _one_split
from tests._yahoo_prices import payload_opener as _opener
from tests._yahoo_prices import stamped_payload as _stamped_payload
from tests._yahoo_prices import without_zone as _without_zone

_BARS: dict[date, float | None] = {date(2025, 3, 3): 100.0, date(2025, 3, 4): 101.0}


def _edited(
    edit: Callable[[dict], object], *, highs: dict[date, float | None] | None = None
) -> bytes:
    """A :func:`_payload` with two March 2025 bars, after ``edit`` changes its
    ``chart.result[0]`` in place."""
    payload = json.loads(_payload(bars=_BARS, highs=highs))
    edit(payload["chart"]["result"][0])
    return json.dumps(payload).encode()


def _refusal(payload: bytes) -> str:
    """The message of the format error that ``payload`` raises."""
    with pytest.raises(YahooResponseFormatError) as exc:
        _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    return str(exc.value)


def test_uses_raw_close_not_adjclose() -> None:
    payload = _payload(
        bars={date(2025, 3, 3): 100.0, date(2025, 3, 4): 110.0},
        adj={date(2025, 3, 3): 99.0, date(2025, 3, 4): 108.0},
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert series.closes[date(2025, 3, 3)] == Decimal("100")
    assert series.closes[date(2025, 3, 4)] == Decimal("110")  # raw, not 108
    assert series.currency == "USD"
    assert series.has_split_in_range is False
    assert series.price_basis == CONTEMPORANEOUS_BASIS


def test_bars_after_end_are_excluded() -> None:
    payload = _payload(
        bars={date(2025, 3, 3): 100.0, date(2025, 6, 2): 200.0},
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert date(2025, 3, 3) in series.closes
    assert date(2025, 6, 2) not in series.closes  # after the window end


def test_null_close_skipped() -> None:
    payload = _payload(bars={date(2025, 3, 3): 100.0, date(2025, 3, 4): None})
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert list(series.closes) == [date(2025, 3, 3)]


def test_split_in_range_flagged() -> None:
    split_day = date(2025, 6, 2)
    payload = _payload(
        bars={date(2025, 3, 3): 100.0},
        splits={
            str(_epoch(split_day)): {
                "date": _epoch(split_day),
                "numerator": 2,
                "denominator": 1,
            }
        },
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert series.has_split_in_range is True
    assert str(series.closes[date(2025, 3, 3)]) == "200.0"
    assert series.split_ratios == {split_day: Decimal("2")}


def test_reverse_and_multiple_later_splits_restore_contemporaneous_close() -> None:
    first = date(2025, 6, 2)
    second = date(2025, 9, 2)
    payload = _payload(
        bars={date(2025, 3, 3): 100.0},
        splits={
            str(_epoch(first)): {
                "date": _epoch(first),
                "numerator": 1,
                "denominator": 10,
            },
            str(_epoch(second)): {
                "date": _epoch(second),
                "numerator": 3,
                "denominator": 2,
            },
        },
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    # Exact in 28 digits, so its value and text are kept (#1307).
    assert str(series.closes[date(2025, 3, 3)]) == "15.000"


def test_later_split_normalizes_high_on_the_same_basis() -> None:
    split_day = date(2026, 3, 2)
    payload = _payload(
        bars={date(2025, 3, 3): 100.0},
        highs={date(2025, 3, 3): 110.0},
        splits={
            str(_epoch(split_day)): {
                "date": _epoch(split_day),
                "numerator": 10,
                "denominator": 1,
            }
        },
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert str(series.closes[date(2025, 3, 3)]) == "1000.0"
    assert str(series.highs[date(2025, 3, 3)]) == "1100.0"


_VALID_SPLIT = {"date": _epoch(_SPLIT_DAY), "numerator": 2, "denominator": 1}
_SPLITS_REFUSAL = "Yahoo price response, events.splits: the splits aren't an object"
_SPLIT_DATE_REFUSAL = (
    "Yahoo price response, events.splits record 2, date: "
    "the split date isn't a whole number that converts to a date"
)


def _second_split(key: str, record: object) -> Callable[[dict], object]:
    """An edit giving ``events.splits`` a valid record, then ``record`` under ``key``."""
    return lambda result: result.update(events={"splits": {_SPLIT_KEY: _VALID_SPLIT, key: record}})


def _ratio_part_refusal(part: str) -> str:
    return (
        f"Yahoo price response, events.splits record 2, {part}: "
        "the split ratio part isn't a number above zero"
    )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        *(
            pytest.param(
                lambda result, splits=splits: result.update(events={"splits": splits}),
                _SPLITS_REFUSAL,
                id=f"splits-{splits!r}",
            )
            for splits in ([], "", 0, False, None)
        ),
        pytest.param(
            lambda result: result.update(events=None),
            "Yahoo price response, events: the events aren't an object",
            id="events-null",
        ),
        pytest.param(
            _second_split("1", "2:1"),
            "Yahoo price response, events.splits record 2: the split record isn't an object",
            id="record-not-object",
        ),
        pytest.param(
            _second_split("1", {"date": True, "numerator": 2, "denominator": 1}),
            _SPLIT_DATE_REFUSAL,
            id="date-true",
        ),
        pytest.param(
            _second_split("1", {"date": "1_000", "numerator": 2, "denominator": 1}),
            _SPLIT_DATE_REFUSAL,
            id="date-text",
        ),
        pytest.param(
            _second_split("1", {"date": 10**20, "numerator": 2, "denominator": 1}),
            _SPLIT_DATE_REFUSAL,
            id="date-out-of-range",
        ),
        pytest.param(
            _second_split(" 1000 ", {"numerator": 2, "denominator": 1}),
            "Yahoo price response, events.splits record 2, key: "
            "the split has no date and its key isn't a whole number that converts to a date",
            id="no-date-spaced-key",
        ),
        pytest.param(
            _second_split("1", {"date": 1, "numerator": -2, "denominator": -1}),
            _ratio_part_refusal("numerator"),
            id="negative-parts",
        ),
        pytest.param(
            _second_split("1", {"date": 1, "numerator": "2", "denominator": 1}),
            _ratio_part_refusal("numerator"),
            id="numerator-text",
        ),
        pytest.param(
            _second_split("1", {"date": 1, "numerator": 2, "denominator": True}),
            _ratio_part_refusal("denominator"),
            id="denominator-true",
        ),
        pytest.param(
            _second_split("1", {"date": 1, "numerator": 2, "denominator": 0}),
            _ratio_part_refusal("denominator"),
            id="denominator-zero",
        ),
    ],
)
def test_malformed_split_refuses_series(edit: Callable[[dict], object], message: str) -> None:
    """A malformed ``events`` or split record refuses the response rather than
    being read as no splits, a split on another date or another ratio (#1139).
    Split records count from 1 in response order."""
    assert _refusal(_edited(edit)) == message


@pytest.mark.parametrize(
    ("events", "ratios"),
    [
        pytest.param({}, {}, id="no-splits-key"),
        pytest.param({"other": 1}, {}, id="other-key"),
        pytest.param(
            {"splits": {_SPLIT_KEY: {"numerator": 3, "denominator": 1}}},
            {_SPLIT_DAY: Decimal(3)},
            id="digit-key-no-date",
        ),
    ],
)
def test_events_without_splits_or_dated_by_key_give_a_series(
    events: dict, ratios: dict[date, Decimal]
) -> None:
    payload = _edited(lambda result: result.update(events=events))
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert series.split_ratios == ratios
    assert series.has_split_in_range is bool(ratios)
    assert series.closes[date(2025, 3, 3)] == Decimal("100.0") * (ratios.get(_SPLIT_DAY) or 1)


# ---- #1307: closes and highs on their own day's split basis are exact -----

_NO_EXACT_VALUE = (
    "Yahoo price response, indicators.quote[0].{name}[{index}]: the {name} on its own "
    "day's split basis has no exact value that fits: a unit price may have at most 9 "
    "digits before the decimal point and 20 after it"
)


@pytest.mark.parametrize(
    ("numerator", "denominator", "close", "high", "expected", "ratio"),
    [
        pytest.param(
            1, 3, 30.0, 33.0, ("10", "11"), "0.3333333333333333333333333333", id="1-for-3"
        ),
        pytest.param(
            2, 3, 30.0, 31.5, ("20", "21"), "0.6666666666666666666666666667", id="2-for-3"
        ),
        pytest.param(1, 2, 2e-20, 4e-20, ("1E-20", "2E-20"), "0.5", id="padded-past-20-places"),
        pytest.param(
            1,
            3,
            300000000.0,
            330000000.0,
            ("100000000", "110000000.0000000000000000000"),
            "0.3333333333333333333333333333",
            id="rounded-within-the-limits",
        ),
    ],
)
def test_split_adjusted_values_are_exact(
    numerator: int,
    denominator: int,
    close: float,
    high: float,
    expected: tuple[str, str],
    ratio: str,
) -> None:
    """#1307: main multiplied by the 28-digit stored ratio, giving
    ``9.999999999999999999999999999`` and ``11.00000000000000000000000000``
    under a 1-for-3 split, ``20.00000000000000000000000000`` and ``21.0…0`` (26
    places) under 2-for-3, ``1.0E-20`` (21 places) for a padded product, and
    ``99999999.99999999999999999999``, within the limits but 1E-20 short, for a
    9-digit close under 1-for-3. Each is now the exact value with no trailing
    zeros after the point, within a unit price's limits. That high's 28-digit
    product rounds to exactly its value, within the limits, so it keeps its
    text. The stored ratio stays the 28-digit provenance."""
    series = split_fetcher(close, high, numerator, denominator)(
        Ticker("XQAA"), start=date(2025, 3, 1), end=date(2025, 3, 31), now=YAHOO_NOW
    )
    assert series is not None
    assert (str(series.closes[_EXACT_DAY]), str(series.highs[_EXACT_DAY])) == expected
    assert {day: str(value) for day, value in series.split_ratios.items()} == {_SPLIT_DAY: ratio}


def test_two_splits_on_one_date_multiply_exactly() -> None:
    """#1307: two split records on one date multiply. A 1-for-3 and a 3-for-1
    split cancel exactly; the stored ratio is their 28-digit product, and main's
    close was ``30.00000000000000000000000000``."""
    payload = _payload(
        bars={_EXACT_DAY: 30.0},
        splits={
            "1": {"date": _epoch(_SPLIT_DAY), "numerator": 1, "denominator": 3},
            "2": {"date": _epoch(_SPLIT_DAY), "numerator": 3, "denominator": 1},
        },
    )
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert str(series.closes[_EXACT_DAY]) == "30"
    assert {day: str(value) for day, value in series.split_ratios.items()} == {
        _SPLIT_DAY: "0.9999999999999999999999999999"
    }


def test_a_split_adjusts_only_the_bars_before_its_date() -> None:
    """A bar on the split's own date is already on the new basis: only earlier
    bars are put back, as on main (#1307 keeps the rule)."""
    the_day_before = date(2025, 5, 30)
    payload = _payload(bars={the_day_before: 30.0, _SPLIT_DAY: 10.0}, splits=_one_split(1, 3))
    series = _fetch(payload, start=date(2025, 5, 1), end=date(2025, 6, 30))
    assert series is not None
    assert {day: str(value) for day, value in series.closes.items()} == {
        the_day_before: "10",
        _SPLIT_DAY: "10.0",
    }


@pytest.mark.parametrize(
    ("numerator", "denominator", "closes", "highs", "message"),
    [
        pytest.param(
            1,
            3,
            (30.0, 29.99),
            (30.0, 29.99),
            _NO_EXACT_VALUE.format(name="close", index=1),
            id="close-1-for-3",
        ),
        pytest.param(
            1,
            3,
            (30.0, 30.0),
            (30.0, 31.99),
            _NO_EXACT_VALUE.format(name="high", index=1),
            id="high-1-for-3",
        ),
        pytest.param(
            10,
            1,
            (30.0, 100000000.0),
            (30.0, 100000000.0),
            _NO_EXACT_VALUE.format(name="close", index=1),
            id="close-past-9-whole-digits",
        ),
        pytest.param(
            10,
            1,
            (30.0, 30.0),
            (30.0, 100000000.0),
            _NO_EXACT_VALUE.format(name="high", index=1),
            id="high-past-9-whole-digits",
        ),
        pytest.param(
            10**4299,
            1,
            (30.0, 30.0),
            (30.0, 30.0),
            _NO_EXACT_VALUE.format(name="close", index=0),
            id="past-integer-text-limit",
        ),
    ],
)
def test_split_adjusted_value_with_no_exact_decimal_within_the_limits_refuses(
    numerator: int,
    denominator: int,
    closes: tuple[float, float],
    highs: tuple[float, float],
    message: str,
) -> None:
    """#1307: a close or high whose exact value on its own day's split basis
    has no decimal within a unit price's limits is never rounded: the response
    refuses, naming the bar's field. On main the first is read as
    ``9.996666666666666666666666666``, the second as ``10.66333333333333333333333333``,
    and the next two as ``1000000000.0``, a unit price past the limit. The last
    has more than 4,300 whole digits, beyond Python's integer-to-text limit."""
    payload = _payload(
        bars=dict(zip(_BARS, closes, strict=True)),
        highs=dict(zip(_BARS, highs, strict=True)),
        splits=_one_split(numerator, denominator),
    )
    assert _refusal(payload) == message


def _second_bar_on_the_first_date(result: dict) -> None:
    result["timestamp"][1] = result["timestamp"][0]


@pytest.mark.parametrize(
    ("edit", "error", "message"),
    [
        pytest.param(
            lambda result: result["indicators"]["quote"][0].update(high=[29.99, 100.0]),
            YahooResponseFormatError,
            "Yahoo price response, indicators.quote[0].high[1]: the high is below the close",
            id="later-high-below-close",
        ),
        pytest.param(
            lambda result: result["indicators"]["quote"][0].update(close=[29.99, 0.0]),
            YahooResponseFormatError,
            "Yahoo price response, indicators.quote[0].close[1]: "
            "the close isn't null or a number above zero",
            id="later-close-not-positive",
        ),
        pytest.param(
            _second_bar_on_the_first_date,
            PriceSourceContradictionError,
            "Yahoo returned two price bars for one date for a holding; "
            "the price source contradicts itself, so its prices are refused",
            id="later-bar-on-the-same-date",
        ),
    ],
)
def test_bar_checks_keep_their_order_before_the_split_adjustment(
    edit: Callable[[dict], object], error: type[Exception], message: str
) -> None:
    """#1307: the first bar's close has no exact value under a 1-for-3 split,
    but every bar's own checks run first, so a later bar's refusal is the one
    raised, as on main."""

    def edits(result: dict) -> None:
        result["indicators"]["quote"][0].update(close=[29.99, 101.0], high=[29.99, 102.0])
        result.update(events={"splits": _one_split(1, 3)})
        edit(result)

    with pytest.raises(error) as exc:
        _fetch(_edited(edits), start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert str(exc.value) == message


# The refused close, and a digit run inside it.
_EXACT_MARKERS = ("7.13579", "13579")


@pytest.mark.parametrize("name", ["close", "high"])
def test_split_adjusted_refusal_names_no_response_value(name: str) -> None:
    """#1307: the refusal is fixed text naming the field; it holds neither the
    refused value nor its digits, and chains nothing."""
    close, high = (7.13579, 7.2) if name == "close" else (6.0, 7.13579)
    body = _payload(
        symbol="XQAA", bars={_EXACT_DAY: close}, highs={_EXACT_DAY: high}, splits=_one_split(1, 3)
    )
    for marker in _EXACT_MARKERS:
        assert marker in body.decode()
    with pytest.raises(YahooResponseFormatError) as exc:
        split_fetcher(close, high, 1, 3)(
            Ticker("XQAA"), start=date(2025, 3, 1), end=date(2025, 3, 31), now=YAHOO_NOW
        )
    field = f"indicators.quote[0].{name}[0]"
    message = _NO_EXACT_VALUE.format(name=name, index=0)
    assert_unchained_refusal(
        exc.value,
        YahooResponseFormatError(field, message.split(f"{field}: ", 1)[1]),
        message,
        markers=_EXACT_MARKERS,
    )


def test_non_usd_currency_passthrough() -> None:
    payload = _payload(currency="GBP", bars={date(2025, 3, 3): 100.0})
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None
    assert series.currency == "GBP"


def test_symbol_mismatch_returns_none() -> None:
    # Requested ticker "X" but Yahoo resolved a different symbol -> refuse
    # (a valid-but-wrong-company match must not be valued).
    payload = _payload(symbol="WRONG", bars={date(2025, 3, 3): 100.0})
    assert _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31)) is None


def test_symbol_match_is_case_insensitive() -> None:
    payload = _payload(symbol="x", bars={date(2025, 3, 3): 100.0})
    series = _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31))
    assert series is not None


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            lambda result, zone=zone: result["meta"].update(exchangeTimezoneName=zone),
            id=repr(zone),
        )
        for zone in ("Not/AZone", "", None, 5)
    ]
    + [pytest.param(_without_zone, id="missing")],
)
def test_missing_or_unknown_timezone_refuses(edit: Callable[[dict], object]) -> None:
    """With no known exchange zone the bar dates aren't known, so the response
    is refused; there is no default zone (#1139)."""
    assert _refusal(_edited(edit)) == NO_ZONE_REFUSAL


def test_transport_failure_returns_none() -> None:
    assert _fetch(None, start=date(2025, 3, 1), end=date(2025, 3, 31)) is None


def test_typed_transport_and_response_failures() -> None:
    transport = fetch_daily_closes_usd_result(
        Ticker("X"),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        now=YAHOO_NOW,
        url_opener=_opener(None),
    )
    assert transport.failure is YahooPriceFailureKind.TRANSPORT
    with pytest.raises(YahooResponseFormatError) as exc:
        fetch_daily_closes_usd_result(
            Ticker("X"),
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            now=YAHOO_NOW,
            url_opener=_opener(b"not json"),
        )
    assert str(exc.value) == _NOT_JSON_REFUSAL


def test_retrying_fetcher_retries_only_transient_failures(monkeypatch) -> None:
    calls = 0
    sleeps = []
    series = _fetch(
        _payload(bars={date(2025, 3, 3): 100.0}),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
    )
    assert series is not None

    def fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return (
            YahooPriceFetchResult(None, YahooPriceFailureKind.RATE_LIMIT, 429)
            if calls == 1
            else YahooPriceFetchResult(series)
        )

    monkeypatch.setattr(yahoo, "fetch_daily_closes_usd_result", fetch)
    assert (
        fetch_daily_closes_usd_retrying(
            Ticker("X"),
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            retry_sleep=sleeps.append,
        )
        is series
    )
    assert calls == 2
    assert sleeps == [0.5]


def test_malformed_json_refuses() -> None:
    assert _refusal(b"not json") == _NOT_JSON_REFUSAL


def test_chart_error_returns_none() -> None:
    payload = _payload(bars={date(2025, 3, 3): 100.0}, error={"code": "Not Found"})
    assert _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31)) is None


def test_chart_error_without_result_returns_none() -> None:
    """A provider error object means no data, before ``chart.result`` is read."""
    payload = _payload(error={"code": "Not Found"}, result=None)
    result = fetch_daily_closes_usd_result(
        Ticker("X"),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        now=YAHOO_NOW,
        url_opener=_opener(payload),
    )
    assert result == YahooPriceFetchResult(None, YahooPriceFailureKind.RESPONSE)


@pytest.mark.parametrize("error", [False, 0, "", []])
def test_chart_error_that_is_not_an_error_object_refuses(error: object) -> None:
    payload = _payload(bars={date(2025, 3, 3): 100.0}, error=error)
    assert _refusal(payload) == (
        "Yahoo price response, chart.error: the error isn't null or an object with a code"
    )


def test_empty_result_refuses() -> None:
    assert _refusal(_payload(result=[])) == (
        "Yahoo price response, chart.result: the result isn't a non-empty array"
    )


def test_all_closes_after_end_returns_none() -> None:
    payload = _payload(bars={date(2025, 6, 2): 200.0})
    assert _fetch(payload, start=date(2025, 3, 1), end=date(2025, 3, 31)) is None


# ---- the real transport, with urlopen patched (#1141) ------------------------
# The autouse fixture replaces yahoo._default_url_opener_result; the name
# imported above is the real function, bound before that patch.

_CHART_REQUEST_URL = "https://query1.finance.yahoo.com/v8/finance/chart/XQAA"


def _http_error(code: int) -> urllib.error.HTTPError:
    return http_error(_CHART_REQUEST_URL, code, "error")


# Each error is built inside its test. A partial has no __name__, so the
# cases keep their index ids (error0-expected0, ...).
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            partial(urllib.error.URLError, "refused"),
            _YahooTransportResult(None, YahooPriceFailureKind.TRANSPORT),
        ),
        (
            partial(OSError, "unreachable"),
            _YahooTransportResult(None, YahooPriceFailureKind.TRANSPORT),
        ),
        (
            partial(_http_error, 429),
            _YahooTransportResult(None, YahooPriceFailureKind.RATE_LIMIT, 429),
        ),
        (partial(_http_error, 500), _YahooTransportResult(None, YahooPriceFailureKind.HTTP, 500)),
    ],
)
def test_real_transport_maps_each_urlopen_failure(
    monkeypatch: pytest.MonkeyPatch, error: Callable[[], OSError], expected: _YahooTransportResult
) -> None:
    calls: list[tuple[str, float]] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        calls.append((request.full_url, timeout))
        raise error()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert _default_url_opener_result(_CHART_REQUEST_URL, 0.2) == expected
    assert calls == [(_CHART_REQUEST_URL, 0.2)]


def test_real_transport_returns_the_response_bytes_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float]] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> urllib.response.addinfourl:
        calls.append((request.full_url, timeout))
        return urllib.response.addinfourl(io.BytesIO(b'{"chart": {}}'), Message(), "", 200)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert _default_url_opener_result(_CHART_REQUEST_URL, 0.2) == _YahooTransportResult(
        b'{"chart": {}}', http_status=200
    )
    assert calls == [(_CHART_REQUEST_URL, 0.2)]


# ---- daily HIGH (peak_basis = "high", multi-round panel review) ------------


def test_high_parsed_alongside_close() -> None:
    payload = _payload(
        bars={date(2025, 3, 3): 100.0, date(2025, 3, 4): 101.0},
        highs={date(2025, 3, 3): 105.0, date(2025, 3, 4): 106.0},
    )
    series = _fetch(payload, start=date(2025, 3, 3), end=date(2025, 3, 4))
    assert series is not None
    assert series.highs == {
        date(2025, 3, 3): Decimal("105.0"),
        date(2025, 3, 4): Decimal("106.0"),
    }


def test_high_below_close_refuses() -> None:
    """A real daily high never sits below the day's close, so a bar whose high
    is below its close refuses the response (#1139)."""
    payload = _payload(
        bars={date(2025, 3, 3): 100.0},
        highs={date(2025, 3, 3): 99.0},  # below close
    )
    assert _refusal(payload) == (
        "Yahoo price response, indicators.quote[0].high[0]: the high is below the close"
    )


def test_missing_high_keeps_close_no_high() -> None:
    payload = _payload(
        bars={date(2025, 3, 3): 100.0, date(2025, 3, 4): 101.0},
        highs={date(2025, 3, 3): 105.0, date(2025, 3, 4): None},  # 03-04 high absent
    )
    series = _fetch(payload, start=date(2025, 3, 3), end=date(2025, 3, 4))
    assert series is not None
    assert set(series.closes) == {date(2025, 3, 3), date(2025, 3, 4)}
    assert series.highs == {date(2025, 3, 3): Decimal("105.0")}


def test_no_high_array_yields_empty_highs() -> None:
    """A payload without a ``high`` array (older/degraded response) parses closes
    with empty highs; the high basis then fails closed."""
    payload = _payload(bars={date(2025, 3, 3): 100.0})
    series = _fetch(payload, start=date(2025, 3, 3), end=date(2025, 3, 3))
    assert series is not None
    assert series.highs == {}


_FIRST_CLOSE_REFUSAL = (
    "Yahoo price response, indicators.quote[0].close[0]: "
    "the close isn't null or a number above zero"
)


def test_non_positive_close_refuses() -> None:
    """A 0/negative close is a bad bar (Yahoo emits null for gaps), so it
    refuses the response rather than being skipped (#1139)."""
    payload = _payload(bars={date(2025, 3, 3): 0.0, date(2025, 3, 4): 101.0})
    assert _refusal(payload) == _FIRST_CLOSE_REFUSAL


def test_nan_close_with_valid_high_refuses() -> None:
    """A NaN close refuses the response, before its high is compared with it."""
    payload = _payload(
        bars={date(2025, 3, 3): float("nan"), date(2025, 3, 4): 101.0},
        highs={date(2025, 3, 3): 105.0, date(2025, 3, 4): 106.0},
    )
    assert _refusal(payload) == _FIRST_CLOSE_REFUSAL


def test_null_close_with_null_high_is_no_bar() -> None:
    payload = _payload(
        bars={date(2025, 3, 3): None, date(2025, 3, 4): 101.0},
        highs={date(2025, 3, 3): None, date(2025, 3, 4): None},
    )
    series = _fetch(payload, start=date(2025, 3, 3), end=date(2025, 3, 4))
    assert series is not None
    assert series.closes == {date(2025, 3, 4): Decimal("101.0")}
    assert series.highs == {}


def _set_quote(name: str, value: object) -> Callable[[dict], object]:
    return lambda result: result["indicators"]["quote"][0].update({name: value})


def _set_second(array: str, value: object) -> Callable[[dict], object]:
    """An edit setting the second bar's ``array`` entry (``timestamp``, or a
    quote array) to ``value``."""

    def edit(result: dict) -> None:
        values = (
            result["timestamp"] if array == "timestamp" else result["indicators"]["quote"][0][array]
        )
        values[1] = value

    return edit


_SECOND_CLOSE_REFUSAL = (
    "Yahoo price response, indicators.quote[0].close[1]: "
    "the close isn't null or a number above zero"
)
_SECOND_HIGH_REFUSAL = (
    "Yahoo price response, indicators.quote[0].high[1]: the high isn't null or a finite number"
)
_SECOND_TIMESTAMP_REFUSAL = (
    "Yahoo price response, timestamp[1]: the timestamp isn't a whole number that converts to a date"
)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"[]", "Yahoo price response: the body isn't an object", id="body-array"),
        pytest.param(
            json.dumps({"chart": []}).encode(),
            "Yahoo price response, chart: the chart isn't an object",
            id="chart-array",
        ),
        pytest.param(
            json.dumps({"chart": {"result": [None], "error": None}}).encode(),
            "Yahoo price response, chart.result[0]: the result isn't an object",
            id="result-null",
        ),
        pytest.param(
            _edited(lambda result: result.update(meta=[])),
            "Yahoo price response, meta: the meta isn't an object",
            id="meta-array",
        ),
        pytest.param(
            _edited(lambda result: result["meta"].update(symbol=None)),
            "Yahoo price response, meta.symbol: the symbol isn't a string",
            id="symbol-null",
        ),
        pytest.param(
            _edited(lambda result: result.update(indicators=None)),
            "Yahoo price response, indicators: the indicators aren't an object",
            id="indicators-null",
        ),
        pytest.param(
            _edited(lambda result: result["indicators"].update(quote=["x"])),
            "Yahoo price response, indicators.quote[0]: the quote isn't an object",
            id="quote-text",
        ),
        *(
            pytest.param(
                _edited(_set_quote("high", high)),
                "Yahoo price response, indicators.quote[0].high: the highs aren't an array",
                id=f"highs-{high!r}",
            )
            for high in ("12", None)
        ),
        pytest.param(
            _edited(_set_quote("high", [200.0])),
            "Yahoo price response, indicators.quote[0].high: "
            "the array isn't the same length as timestamp",
            id="high-shorter-than-timestamp",
        ),
        pytest.param(
            _edited(_set_quote("close", "12")),
            "Yahoo price response, indicators.quote[0].close: the closes aren't an array",
            id="close-text",
        ),
        pytest.param(
            _edited(_set_quote("close", [100.0, 101.0, 102.0])),
            "Yahoo price response, indicators.quote[0].close: "
            "the array isn't the same length as timestamp",
            id="close-longer-than-timestamp",
        ),
        pytest.param(
            _edited(_set_second("timestamp", True)), _SECOND_TIMESTAMP_REFUSAL, id="ts-bool"
        ),
        pytest.param(
            _edited(_set_second("timestamp", None)), _SECOND_TIMESTAMP_REFUSAL, id="ts-null"
        ),
        *(
            pytest.param(
                _edited(_set_second("close", close)), _SECOND_CLOSE_REFUSAL, id=f"close-{close!r}"
            )
            for close in ("1_000", "1e3", " 12 ", "100.0", True, 0, -1)
        ),
        *(
            pytest.param(
                _edited(_set_second("high", high), highs=dict.fromkeys(_BARS, 200.0)),
                _SECOND_HIGH_REFUSAL,
                id=f"high-{high!r}",
            )
            for high in ("1_000", True)
        ),
    ],
)
def test_malformed_arrays_and_values_refuse(body: bytes, message: str) -> None:
    """Yahoo sends timestamps as integers and prices as numbers: text, bools,
    a missing timestamp or arrays of another length refuse the response
    rather than being read or zipped (#1139). Indexes count from 0."""
    assert _refusal(body) == message


# ---- #942: two bars on one exchange-local date ------------------------------


def test_two_bars_on_one_date_raise() -> None:
    """The $100 bar contradicts the $10 one, so the response is refused instead
    of keeping Monday $10 and Tuesday $20. It never becomes a failure kind.
    The message names the source, but not the symbol or the bar date, which
    are input (#1047)."""
    with pytest.raises(PriceSourceContradictionError) as exc:
        fetch_daily_closes_usd_result(
            Ticker("U1234567"),
            start=date(2025, 6, 1),
            end=date(2025, 6, 30),
            now=YAHOO_NOW,
            url_opener=_opener(repeated_date_payload("U1234567")),
        )
    assert exc.value == PriceSourceContradictionError("Yahoo")
    message = str(exc.value)
    assert message == (
        "Yahoo returned two price bars for one date for a holding; the price source "
        "contradicts itself, so its prices are refused"
    )
    assert "u1234567" not in message.lower()
    assert _MONDAY.isoformat() not in message


# #1368's example: a symbol and a repeated date that are markers, kept off the
# lines a traceback prints so that the traceback check tests the error.
_MARKED_SYMBOL = "XQ731942"
_MARKED_DAY = date(2031, 7, 29)
_MARKERS = ("731942", "2031-07-29", "2031, 7, 29")


def test_two_bars_on_one_date_keep_neither_the_symbol_nor_the_date() -> None:
    """#1368: two bars on Tuesday 29 July 2031, then one on the Wednesday. The
    error keeps only the source: neither the symbol nor the date is in its
    ``str``, ``repr``, args, fields, traceback or chain."""
    payload = _stamped_payload(
        [
            (_at(_MARKED_DAY, 14), 100.0),
            (_at(_MARKED_DAY, 18), 10.0),
            (_at(date(2031, 7, 30), 18), 20.0),
        ],
        symbol=_MARKED_SYMBOL,
    )
    with pytest.raises(PriceSourceContradictionError) as exc:
        fetch_daily_closes_usd_result(
            Ticker(_MARKED_SYMBOL),
            start=date(2031, 7, 1),
            end=date(2031, 7, 31),
            now=datetime(2031, 8, 1, tzinfo=UTC),
            url_opener=_opener(payload),
        )
    assert str(exc.value) == (
        "Yahoo returned two price bars for one date for a holding; the price source "
        "contradicts itself, so its prices are refused"
    )
    assert_prints_no_marker(exc.value, markers=_MARKERS)
    assert exc.value == PriceSourceContradictionError("Yahoo")


@pytest.mark.parametrize(("first", "second"), [(None, 10.0), (100.0, None)])
def test_two_bars_on_one_date_raise_when_one_has_no_close(
    first: float | None, second: float | None
) -> None:
    payload = _stamped_payload(
        [(_at(_MONDAY, 14), first), (_at(_MONDAY, 18), second), (_at(_TUESDAY, 18), 20.0)]
    )
    with pytest.raises(PriceSourceContradictionError):
        _fetch(payload, start=date(2025, 6, 1), end=date(2025, 6, 30))


def test_two_bars_on_one_date_after_end_are_ignored() -> None:
    payload = _stamped_payload(
        [(_at(date(2025, 5, 30), 18), 50.0), (_at(_MONDAY, 14), 100.0), (_at(_MONDAY, 18), 10.0)]
    )
    series = _fetch(payload, start=date(2025, 5, 1), end=date(2025, 5, 31))
    assert series is not None
    assert series.closes == {date(2025, 5, 30): Decimal("50.0")}


def test_retrying_fetcher_raises_two_bars_on_one_date_without_retrying() -> None:
    urls: list[str] = []
    sleeps: list[float] = []

    def open_url(url: str, _timeout: float) -> bytes:
        urls.append(url)
        return repeated_date_payload()

    with pytest.raises(PriceSourceContradictionError):
        fetch_daily_closes_usd_retrying(
            Ticker("X"),
            start=date(2025, 6, 1),
            end=date(2025, 6, 30),
            now=YAHOO_NOW,
            url_opener=open_url,
            retry_sleep=sleeps.append,
        )
    assert len(urls) == 1
    assert sleeps == []


def test_undatable_timestamp_refuses_series() -> None:
    """A bar whose timestamp gives no date could hide a repeated date, so the
    response is refused as malformed, even when that bar has no close."""
    payload = _stamped_payload([(_at(_MONDAY, 18), 100.0), ("x", None)])
    with pytest.raises(YahooResponseFormatError) as exc:
        fetch_daily_closes_usd_result(
            Ticker("X"),
            start=date(2025, 6, 1),
            end=date(2025, 6, 30),
            now=YAHOO_NOW,
            url_opener=_opener(payload),
        )
    assert str(exc.value) == _SECOND_TIMESTAMP_REFUSAL


def test_retrying_fetcher_raises_a_format_error_without_retrying() -> None:
    urls: list[str] = []
    sleeps: list[float] = []

    def open_url(url: str, _timeout: float) -> bytes:
        urls.append(url)
        return _edited(_without_zone)

    with pytest.raises(YahooResponseFormatError):
        fetch_daily_closes_usd_retrying(
            Ticker("X"),
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            now=YAHOO_NOW,
            url_opener=open_url,
            retry_sleep=sleeps.append,
        )
    assert len(urls) == 1
    assert sleeps == []


# ---- #1139: the refusal text names no response value -----------------------

_PRIVATE_DAY = date(2025, 3, 3)
_PRIVATE_SPLIT_KEY = "1700000123"
# Every case's response carries these; the zone case adds Synthetic/Zone.
_PRIVATE_MARKERS = ("XQYH", "4.8217", _PRIVATE_SPLIT_KEY)


def _private_payload(edit: Callable[[dict], object]) -> bytes:
    """A New York response for XQYH with a close of 4.8217 on 3 March 2025 and
    a split keyed 1700000123, after ``edit``."""
    payload = json.loads(
        _payload(
            symbol="XQYH",
            bars={_PRIVATE_DAY: 4.8217},
            highs={_PRIVATE_DAY: 4.9},
            splits={_PRIVATE_SPLIT_KEY: {"numerator": 2, "denominator": 1}},
        )
    )
    edit(payload["chart"]["result"][0])
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    ("edit", "markers", "message"),
    [
        pytest.param(
            lambda result: result["meta"].update(exchangeTimezoneName="Synthetic/Zone"),
            (*_PRIVATE_MARKERS, "Synthetic/Zone"),
            NO_ZONE_REFUSAL,
            id="zone",
        ),
        pytest.param(
            lambda result: result["indicators"]["quote"][0].update(high=[4.8]),
            _PRIVATE_MARKERS,
            "Yahoo price response, indicators.quote[0].high[0]: the high is below the close",
            id="high-below-close",
        ),
        pytest.param(
            lambda result: result["indicators"]["quote"][0].update(close=[4.8217, 4.8217]),
            _PRIVATE_MARKERS,
            "Yahoo price response, indicators.quote[0].close: "
            "the array isn't the same length as timestamp",
            id="close-length",
        ),
        pytest.param(
            lambda result: result["events"]["splits"][_PRIVATE_SPLIT_KEY].update(numerator=-2),
            _PRIVATE_MARKERS,
            "Yahoo price response, events.splits record 1, numerator: "
            "the split ratio part isn't a number above zero",
            id="split-numerator",
        ),
    ],
)
def test_format_refusal_names_no_response_value(
    edit: Callable[[dict], object], markers: tuple[str, ...], message: str
) -> None:
    """The refusal names the field and rule only: no symbol, zone, price,
    split key or date from the response (#1139)."""
    body = _private_payload(edit)
    with pytest.raises(YahooResponseFormatError) as exc:
        fetch_daily_closes_usd(
            Ticker("XQYH"),
            start=date(2025, 3, 1),
            end=date(2025, 3, 31),
            now=YAHOO_NOW,
            url_opener=_opener(body),
        )
    text = str(exc.value)
    assert text == message
    for marker in (*markers, str(_epoch(_PRIVATE_DAY))):
        assert marker in body.decode()
        assert marker.lower() not in text.lower()
    assert re.search(r"\d{4}-\d{2}-\d{2}", text) is None


# ---- #1139: Yahoo's reply for a window with no bars --------------------------


def _no_bars_reply(edit: Callable[[dict], object]) -> bytes:
    """Yahoo's reply for a window with no bars, as for a ticker renamed or
    delisted before the fetch: a null currency, no ``timestamp`` and an empty
    quote object, after ``edit``."""
    payload = json.loads(_payload())
    result = payload["chart"]["result"][0]
    result["meta"]["currency"] = None
    del result["timestamp"]
    result["indicators"] = {"quote": [{}], "adjclose": [{}]}
    edit(result)
    return json.dumps(payload).encode()


def _no_bars_edit(meta: dict | None = None, **fields: object) -> Callable[[dict], None]:
    def edit(result: dict) -> None:
        result["meta"].update(meta or {})
        result.update(fields)

    return edit


@pytest.mark.parametrize("currency", [None, "USD"])
def test_no_bars_reply_returns_none(currency: str | None) -> None:
    """Owner decision (#1139): this exact reply is no data, so the holding falls
    back to manual-fill as before rather than refusing the run."""
    result = fetch_daily_closes_usd_result(
        Ticker("X"),
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        now=YAHOO_NOW,
        url_opener=_opener(_no_bars_reply(_no_bars_edit({"currency": currency}))),
    )
    assert result == YahooPriceFetchResult(None, YahooPriceFailureKind.RESPONSE)


_CURRENCY_REFUSAL = "Yahoo price response, meta.currency: the currency isn't a string"
_NO_TIMESTAMPS_REFUSAL = "Yahoo price response, timestamp: the timestamps aren't an array"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            _no_bars_edit({"currency": "USD"}, timestamp=[_epoch(date(2025, 3, 3))]),
            "Yahoo price response, indicators.quote[0].close: the closes aren't an array",
            id="timestamps-with-empty-quote",
        ),
        pytest.param(
            _no_bars_edit({"currency": "USD"}, timestamp=None),
            _NO_TIMESTAMPS_REFUSAL,
            id="null-timestamp",
        ),
        pytest.param(
            _no_bars_edit({"currency": "USD"}, indicators={"quote": [{"close": []}]}),
            _NO_TIMESTAMPS_REFUSAL,
            id="quote-without-timestamps",
        ),
        pytest.param(
            _no_bars_edit(
                timestamp=[_epoch(date(2025, 3, 3))], indicators={"quote": [{"close": [100.0]}]}
            ),
            _CURRENCY_REFUSAL,
            id="bars-with-null-currency",
        ),
        pytest.param(_no_bars_edit({"currency": 5}), _CURRENCY_REFUSAL, id="currency-number"),
        pytest.param(
            lambda result: result["meta"].pop("currency"), _CURRENCY_REFUSAL, id="no-currency"
        ),
        pytest.param(
            _no_bars_edit({"exchangeTimezoneName": "Not/AZone"}), NO_ZONE_REFUSAL, id="unknown-zone"
        ),
        pytest.param(
            _no_bars_edit(events=None),
            "Yahoo price response, events: the events aren't an object",
            id="events-null",
        ),
        pytest.param(
            _no_bars_edit(indicators={"quote": []}),
            "Yahoo price response, indicators.quote: the quotes aren't a non-empty array",
            id="no-quote-object",
        ),
    ],
)
def test_near_no_bars_replies_refuse(edit: Callable[[dict], object], message: str) -> None:
    """Only the exact no-bars reply is no data: timestamps with an empty quote,
    a quote without timestamps, bars with a null currency, a missing or
    non-text currency, and a bad zone, events or quote array each refuse."""
    assert _refusal(_no_bars_reply(edit)) == message
