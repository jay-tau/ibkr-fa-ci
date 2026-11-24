"""Fetch daily closing prices from Yahoo's public chart API (#14).

Best-effort enrichment: a network error, an HTTP error, a provider error
object (``chart.error`` with a string ``code``), a resolved symbol that
isn't the one requested, Yahoo's reply for a window with no bars (no
``timestamp``, an empty quote object and a null or text currency, as for a
ticker renamed or delisted before the fetch), and a response with no close
on or before ``end`` each return ``None``, preserving the caller's existing
handling of unavailable prices.

A response that doesn't have the shape this reader accepts raises
:class:`YahooResponseFormatError` (#1139), naming the field and the rule it
breaks. The exchange timezone is required: there is no default zone, since
a guessed zone can move a price across days and years. Two bars on one
exchange-local date on or before ``end`` raise
:class:`~ibkr_fa.prices.series.PriceSourceContradictionError` (#942).
Neither error is retried, and nothing is cached.

Correctness guards baked into the parse (see the rubber-duck review on
the originating PR):

* Uses ``indicators.quote[0].close``, never ``adjclose``
  (which is dividend-adjusted and wrong for a market-value disclosure).
* Reverses Yahoo's retroactive split rebasing, producing the closing
  price on the basis that existed that day: each close and high times
  its later dated split ratios, exactly (#1307). A value with no exact
  decimal within a unit price's digit limits, as most closes under a
  1-for-3 split have, is never rounded: the response is refused with a
  :class:`YahooResponseFormatError` naming the bar.
* Records the quote ``currency`` from ``meta`` so the compute can
  refuse non-USD listings (a wrong-currency Yahoo match).
* Converts bar timestamps to a date in the **exchange timezone**
  (``meta.exchangeTimezoneName``), not UTC, to avoid off-by-one dates
  around the day boundary / DST.
* Queries ``events=split`` over a span that extends to *now*, so
  post-window splits are included in normalization and provenance.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ibkr_fa.exceptions import IbkrFaError, ReplacedCrashError, crash_site
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.prices.series import (
    CONTEMPORANEOUS_BASIS,
    PriceSeries,
    PriceSourceContradictionError,
)
from ibkr_fa.types import Ticker

_CHART_URL: Final[str] = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_USER_AGENT: Final[str] = "Mozilla/5.0 (ibkr-fa Schedule-FA peak-value price fetch)"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
# A split record with no ``date`` is dated by its key: whole ASCII digits.
_DIGITS: Final[re.Pattern[str]] = re.compile(r"[0-9]+")

# Injectable HTTP transport: (url, timeout) -> response bytes, or None
# on any retrieval failure. Tests pass a stub; production uses urllib.
UrlOpener = Callable[[str, float], "bytes | None"]


class DailyCloseFetcher(Protocol):
    """Structural type for a price fetcher (injectable in tests).

    ``fetch_symbol`` is the optional price-source symbol override (the
    ``[prices].ticker_overrides`` ``yahoo_symbol``); when ``None`` the
    fetcher uses ``ticker`` as the symbol.
    """

    def __call__(
        self,
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None: ...


class YahooPriceFailureKind(StrEnum):
    """Why Yahoo could not produce an identity-safe price series."""

    RATE_LIMIT = "rate-limit"
    TRANSPORT = "transport"
    HTTP = "http"
    RESPONSE = "response"


@dataclass(frozen=True, slots=True)
class YahooResponseFormatError(IbkrFaError):
    """A Yahoo chart response doesn't have the shape the reader accepts (#1139).

    A malformed response is refused rather than read as no series: a guessed
    zone, split or price could move a value across days or years, and "no
    series" could send the holding to manual-fill past refusals that the
    response would otherwise reach. The message names the field and the rule
    it breaks, never a symbol, date, price or other response text. A body that
    isn't JSON keeps none of the parser's error, which can hold the body: the
    refusal has no context of its own, and its cause is a content-free
    :class:`~ibkr_fa.exceptions.ReplacedCrashError` naming the parser error's
    class and line (#1225).

    Args:
        field: The field's fixed path in the response, such as
            ``meta.exchangeTimezoneName``, with any array index (0-based) or
            split record position (1-based); empty for the body itself.
        rule: The rule the field breaks.
    """

    field: str
    rule: str

    def __str__(self) -> str:
        where = f", {self.field}" if self.field else ""
        return f"Yahoo price response{where}: {self.rule}"


@dataclass(frozen=True, slots=True)
class YahooPriceFetchResult:
    series: PriceSeries | None
    failure: YahooPriceFailureKind | None = None
    http_status: int | None = None


@dataclass(frozen=True, slots=True)
class _YahooTransportResult:
    raw: bytes | None
    failure: YahooPriceFailureKind | None = None
    http_status: int | None = None


def _default_url_opener_result(url: str, timeout: float) -> _YahooTransportResult:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _YahooTransportResult(
                raw=response.read(),
                http_status=getattr(response, "status", None),
            )
    except urllib.error.HTTPError as exc:
        return _YahooTransportResult(
            None,
            (
                YahooPriceFailureKind.RATE_LIMIT
                if exc.code == _HTTP_TOO_MANY_REQUESTS
                else YahooPriceFailureKind.HTTP
            ),
            exc.code,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return _YahooTransportResult(None, YahooPriceFailureKind.TRANSPORT)


def _to_epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def fetch_daily_closes_usd_result(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    url_opener: UrlOpener | None = None,
    fetch_symbol: str | None = None,
) -> YahooPriceFetchResult:
    """Fetch one split-aware series and retain its failure category.

    Raises:
        YahooResponseFormatError: If the response doesn't have the accepted
            shape (#1139), or a close or high on its own day's split basis
            has no exact decimal within a unit price's digit limits (#1307).
        PriceSourceContradictionError: If two bars on or before ``end`` fall
            on the same exchange-local date (#942).
    """
    requested = fetch_symbol if fetch_symbol is not None else str(ticker)
    now = now or datetime.now(UTC)
    period1 = _to_epoch(start)
    period2 = _to_epoch(now.date() + timedelta(days=2))
    url = (
        _CHART_URL.format(ticker=requested)
        + f"?period1={period1}&period2={period2}&interval=1d&events=split"
    )
    if url_opener is None:
        transport = _default_url_opener_result(url, timeout)
        if transport.raw is None:
            return YahooPriceFetchResult(
                None,
                transport.failure,
                transport.http_status,
            )
        raw = transport.raw
    else:
        raw = url_opener(url, timeout)
        if raw is None:
            return YahooPriceFetchResult(None, YahooPriceFailureKind.TRANSPORT)
    series = _parse(
        ticker,
        raw,
        end=end,
        now=now,
        source=url,
        requested_symbol=requested,
    )
    return YahooPriceFetchResult(
        series,
        None if series is not None else YahooPriceFailureKind.RESPONSE,
    )


def fetch_daily_closes_usd(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    url_opener: UrlOpener | None = None,
    fetch_symbol: str | None = None,
) -> PriceSeries | None:
    """Compatibility wrapper returning only the resolved series."""

    return fetch_daily_closes_usd_result(
        ticker,
        start=start,
        end=end,
        now=now,
        timeout=timeout,
        url_opener=url_opener,
        fetch_symbol=fetch_symbol,
    ).series


def fetch_daily_closes_usd_retrying(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime | None = None,
    fetch_symbol: str | None = None,
    url_opener: UrlOpener | None = None,
    max_attempts: int = 3,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> PriceSeries | None:
    """Retry only transient Yahoo transport/rate-limit failures.

    A :class:`YahooResponseFormatError` or
    :class:`~ibkr_fa.prices.series.PriceSourceContradictionError` propagates
    from the first attempt, without a retry.
    """

    for attempt in range(max_attempts):
        result = fetch_daily_closes_usd_result(
            ticker,
            start=start,
            end=end,
            now=now,
            fetch_symbol=fetch_symbol,
            url_opener=url_opener,
        )
        if result.series is not None:
            return result.series
        if result.failure not in {
            YahooPriceFailureKind.RATE_LIMIT,
            YahooPriceFailureKind.TRANSPORT,
        }:
            return None
        if attempt + 1 < max_attempts:
            retry_sleep(0.5 * (2**attempt))
    return None


def _object(value: object, field: str, rule: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise YahooResponseFormatError(field, rule)
    return cast("dict[str, object]", value)


def _array(value: object, field: str, rule: str) -> list[object]:
    if not isinstance(value, list) or not value:
        raise YahooResponseFormatError(field, rule)
    return cast("list[object]", value)


def _bar_array(quote: dict[str, object], name: str, length: int) -> list[object]:
    """``quote[name]`` (``close`` or ``high``), an array as long as ``timestamp``."""
    values = quote.get(name)
    field = f"indicators.quote[0].{name}"
    if not isinstance(values, list):
        raise YahooResponseFormatError(field, f"the {name}s aren't an array")
    if len(values) != length:
        raise YahooResponseFormatError(field, "the array isn't the same length as timestamp")
    return cast("list[object]", values)


def _finite_number(value: object) -> Decimal | None:
    """``value`` as a Decimal when it's a finite JSON number (not a bool), else ``None``."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _positive_number(value: object) -> Decimal | None:
    number = _finite_number(value)
    return number if number is not None and number > 0 else None


def _epoch_day(stamp: object, tz: ZoneInfo) -> date | None:
    """The exchange-local date of a JSON integer (not a bool) epoch timestamp,
    or ``None`` when ``stamp`` isn't one or ``datetime`` can't date it."""
    if not isinstance(stamp, int) or isinstance(stamp, bool):
        return None
    try:
        return datetime.fromtimestamp(stamp, tz).date()
    except (OverflowError, OSError, ValueError):
        return None


def _exchange_zone(name: object) -> ZoneInfo:
    if isinstance(name, str) and name:
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError, OSError):
            return ZoneInfo(name)
    raise YahooResponseFormatError(
        "meta.exchangeTimezoneName", "the exchange timezone is missing or not a known zone"
    )


def _split_day(key: str, split: dict[str, object], field: str, tz: ZoneInfo) -> date:
    """A split record's date: its ``date``, or when absent, its key."""
    if "date" in split:
        day = _epoch_day(split["date"], tz)
        if day is None:
            raise YahooResponseFormatError(
                f"{field}, date", "the split date isn't a whole number that converts to a date"
            )
        return day
    day = None
    if _DIGITS.fullmatch(key):
        with contextlib.suppress(ValueError):  # more digits than int() reads
            day = _epoch_day(int(key), tz)
    if day is None:
        raise YahooResponseFormatError(
            f"{field}, key",
            "the split has no date and its key isn't a whole number that converts to a date",
        )
    return day


def _split_ratios(
    result: dict[str, object], tz: ZoneInfo
) -> tuple[dict[date, Decimal], dict[date, Fraction]]:
    """Each split date's ratio as stored provenance, a Decimal at the context's
    precision, and exactly (#1307); absent ``events`` or ``events.splits``
    means none."""
    if "events" not in result:
        return {}, {}
    events = _object(result["events"], "events", "the events aren't an object")
    if "splits" not in events:
        return {}, {}
    splits = _object(events["splits"], "events.splits", "the splits aren't an object")
    ratios: dict[date, Decimal] = {}
    exact: dict[date, Fraction] = {}
    for position, (key, value) in enumerate(splits.items(), start=1):
        field = f"events.splits record {position}"
        split = _object(value, field, "the split record isn't an object")
        split_day = _split_day(key, split, field, tz)
        parts = []
        for name in ("numerator", "denominator"):
            part = _positive_number(split.get(name))
            if part is None:
                raise YahooResponseFormatError(
                    f"{field}, {name}", "the split ratio part isn't a number above zero"
                )
            parts.append(part)
        ratios[split_day] = ratios.get(split_day, Decimal(1)) * (parts[0] / parts[1])
        exact[split_day] = (
            exact.get(split_day, Fraction(1)) * Fraction(parts[0]) / Fraction(parts[1])
        )
    return ratios, exact


def _chart_result(raw: bytes) -> dict[str, object] | None:
    """``chart.result[0]``, or ``None`` for a provider error object."""
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # The parser's error can hold the response. The refusal is frozen, so
        # ``raise_replacing`` can't clear its context: keep only the
        # content-free stand-in and raise after the handler.
        stand_in = ReplacedCrashError(crash_site(exc))
    else:
        stand_in = None
    if stand_in is not None:
        raise YahooResponseFormatError("", "the body isn't JSON") from stand_in
    chart = _object(
        _object(payload, "", "the body isn't an object").get("chart"),
        "chart",
        "the chart isn't an object",
    )
    error = chart.get("error")
    if error is not None:
        if isinstance(error, dict) and isinstance(
            cast("dict[str, object]", error).get("code"), str
        ):
            return None
        raise YahooResponseFormatError(
            "chart.error", "the error isn't null or an object with a code"
        )
    results = _array(chart.get("result"), "chart.result", "the result isn't a non-empty array")
    return _object(results[0], "chart.result[0]", "the result isn't an object")


_NO_CURRENCY: Final[tuple[str, str]] = ("meta.currency", "the currency isn't a string")


def _meta(result: dict[str, object]) -> tuple[str | None, str, ZoneInfo]:
    """``meta``'s currency (text or null), resolved symbol and known exchange
    timezone."""
    meta = _object(result.get("meta"), "meta", "the meta isn't an object")
    currency = meta.get("currency")
    if "currency" not in meta or not (currency is None or isinstance(currency, str)):
        raise YahooResponseFormatError(*_NO_CURRENCY)
    symbol = meta.get("symbol")
    if not isinstance(symbol, str):
        raise YahooResponseFormatError("meta.symbol", "the symbol isn't a string")
    return currency, symbol, _exchange_zone(meta.get("exchangeTimezoneName"))


def _bar_day(index: int, stamp: object, tz: ZoneInfo) -> date:
    day = _epoch_day(stamp, tz)
    if day is None:
        raise YahooResponseFormatError(
            f"timestamp[{index}]", "the timestamp isn't a whole number that converts to a date"
        )
    return day


def _require_within_limits(value: Decimal | None, field: str) -> None:
    """Refuse a close or high past a unit price's read-time digit limits (#1276)."""
    breach = None if value is None else number_limit_breach(value, NumberKind.PRICE)
    if breach is not None:
        raise YahooResponseFormatError(field, breach)


def _bar_prices(index: int, close: object, high: object) -> tuple[Decimal | None, Decimal | None]:
    """One bar's close and high, each ``None`` when null."""
    close_dec = None if close is None else _positive_number(close)
    if close is not None and close_dec is None:
        raise YahooResponseFormatError(
            f"indicators.quote[0].close[{index}]", "the close isn't null or a number above zero"
        )
    _require_within_limits(close_dec, f"indicators.quote[0].close[{index}]")
    high_dec = None if high is None else _finite_number(high)
    if high is not None and high_dec is None:
        raise YahooResponseFormatError(
            f"indicators.quote[0].high[{index}]", "the high isn't null or a finite number"
        )
    _require_within_limits(high_dec, f"indicators.quote[0].high[{index}]")
    if close_dec is not None and high_dec is not None and high_dec < close_dec:
        raise YahooResponseFormatError(
            f"indicators.quote[0].high[{index}]", "the high is below the close"
        )
    return close_dec, high_dec


def _decimal_within_limits(value: Fraction) -> Decimal | None:
    """``value`` as a Decimal with no trailing zeros after the point, or ``None``
    when no decimal within a unit price's read-time digit limits is exactly
    ``value``."""
    places = NumberKind.PRICE.decimal_places
    scaled = value * 10**places
    if scaled.denominator != 1:
        return None
    digits = scaled.numerator
    while places and digits % 10 == 0:
        digits //= 10
        places -= 1
    sign, coefficient, _ = Decimal(digits).as_tuple()
    decimal = Decimal((sign, coefficient, -places))
    return decimal if number_limit_breach(decimal, NumberKind.PRICE) is None else None


def _on_own_day_basis(
    value: Decimal, factor: Decimal, exact_factor: Fraction, field: str, name: str
) -> Decimal:
    """``value`` times its bar's later split ratios, exactly (#1307).

    ``factor`` is the product of the stored ratios, as before #1307, and
    ``exact_factor`` the exact product. Where ``value * factor`` is the exact
    value and within a unit price's read-time digit limits, it is kept, text
    included; otherwise the exact value is returned with no trailing zeros
    after the point.

    Raises:
        YahooResponseFormatError: If no decimal within those limits is
            exactly the value, naming ``field``.
    """
    exact = _decimal_within_limits(Fraction(value) * exact_factor)
    if exact is None:
        raise YahooResponseFormatError(
            field,
            f"the {name} on its own day's split basis has no exact value that fits: "
            f"{NumberKind.PRICE.refusal}",
        )
    product = value * factor
    if product == exact and number_limit_breach(product, NumberKind.PRICE) is None:
        return product
    return exact


def _parse(
    ticker: Ticker,
    raw: bytes,
    *,
    end: date,
    now: datetime,
    source: str,
    requested_symbol: str,
) -> PriceSeries | None:
    """Parse one chart response into a series, or ``None`` if it has no data.

    Returns ``None`` for a provider error object, a resolved symbol that
    isn't ``requested_symbol``, a no-bars reply (no ``timestamp``, an empty
    ``indicators.quote[0]`` and a null or text currency), or no close on or
    before ``end``. Every bar is checked; bars after ``end`` are then
    dropped. A null close is no bar and a null high is no high.

    Each kept close and high is then put back on its own day's split basis:
    the provider's value times every later split's ``numerator /
    denominator``, exactly (#1307). Where the 28-digit product of the stored
    ratios is already that value within a unit price's read-time digit
    limits, it is kept, text included; otherwise the value has no trailing
    zeros after the point. Nothing is rounded.

    Raises:
        YahooResponseFormatError: If the response doesn't have the accepted
            shape, including a high below its close (#1139), or, once every
            bar has passed those checks, a kept close or high on its own
            day's split basis has no exact decimal within a unit price's
            read-time digit limits (#1307).
        PriceSourceContradictionError: If two timestamps on or before ``end``
            fall on the same exchange-local date, whether or not either bar
            has a close (#942).
    """
    result = _chart_result(raw)
    if result is None:
        return None
    currency, resolved_symbol, tz = _meta(result)
    # Identity guard: refuse a response whose resolved symbol does
    # not match the symbol we REQUESTED (``requested_symbol`` — the
    # IBKR ticker, or the ticker_overrides yahoo_symbol when one
    # applied). A symbol that resolves to a DIFFERENT instrument
    # (exchange-suffix / ADR-vs-ordinary divergence, or a Yahoo
    # redirect) would otherwise be valued confidently and wrongly.
    # Strict string match is conservative: at worst it refuses a
    # legitimately punctuation-different symbol, degrading to
    # manual-fill (safe). It cannot catch same-string ticker REUSE
    # across time — that is why ticker_overrides is ISIN-bound at
    # the config layer.
    if resolved_symbol.upper() != requested_symbol.upper():
        return None
    indicators = _object(result.get("indicators"), "indicators", "the indicators aren't an object")
    quotes = _array(
        indicators.get("quote"), "indicators.quote", "the quotes aren't a non-empty array"
    )
    quote = _object(quotes[0], "indicators.quote[0]", "the quote isn't an object")
    splits, exact_splits = _split_ratios(result, tz)
    if "timestamp" not in result and not quote:
        # Yahoo's reply for a window with no bars, e.g. for a ticker renamed or
        # delisted before the fetch: no timestamps, an empty quote object and a
        # null or text currency. No data, as before #1139 (owner decision).
        return None
    if currency is None:
        raise YahooResponseFormatError(*_NO_CURRENCY)
    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list):
        raise YahooResponseFormatError("timestamp", "the timestamps aren't an array")
    raw_closes = _bar_array(quote, "close", len(timestamps))
    # Daily HIGH powers the opt-in FA-A3 ``peak_basis = "high"`` (intraday
    # peak). With no ``high`` array no bar has a high, and the high-basis
    # weekday-completeness gate fails closed rather than guessing.
    raw_highs = (
        _bar_array(quote, "high", len(timestamps)) if "high" in quote else [None] * len(timestamps)
    )

    closes: dict[date, Decimal] = {}
    highs: dict[date, Decimal] = {}
    bar_dates: set[date] = set()
    priced: list[tuple[int, date, Decimal, Decimal | None]] = []
    bars = zip(timestamps, raw_closes, raw_highs, strict=True)
    for index, (stamp, close, high) in enumerate(bars):
        bar_date = _bar_day(index, stamp, tz)
        close_dec, high_dec = _bar_prices(index, close, high)
        if bar_date > end:
            # Bars after the valuation window are fetched only to
            # observe splits; they don't belong in the series.
            continue
        if bar_date in bar_dates:
            raise PriceSourceContradictionError("Yahoo")
        bar_dates.add(bar_date)
        if close_dec is not None:
            priced.append((index, bar_date, close_dec, high_dec))

    # After every bar's checks, so that each refusal above keeps its order.
    for index, bar_date, close_dec, high_dec in priced:
        later = [split_day for split_day in splits if split_day > bar_date]
        split_factor = math.prod((splits[day] for day in later), start=Decimal(1))
        exact_factor = math.prod((exact_splits[day] for day in later), start=Fraction(1))
        closes[bar_date] = _on_own_day_basis(
            close_dec, split_factor, exact_factor, f"indicators.quote[0].close[{index}]", "close"
        )
        if high_dec is not None:
            highs[bar_date] = _on_own_day_basis(
                high_dec, split_factor, exact_factor, f"indicators.quote[0].high[{index}]", "high"
            )

    if not closes:
        return None

    return PriceSeries(
        ticker=ticker,
        currency=currency,
        closes=closes,
        highs=highs,
        has_split_in_range=bool(splits),
        source=source,
        fetched_at=now,
        split_ratios=splits,
        price_basis=CONTEMPORANEOUS_BASIS,
    )
