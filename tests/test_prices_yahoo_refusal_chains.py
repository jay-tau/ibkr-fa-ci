"""A Yahoo chart body that isn't JSON stays out of the refusal's chain (#1225).

``json.loads`` fails on a truncated document (``JSONDecodeError``, whose
``doc`` is the whole body), on bytes that aren't UTF-8 (``UnicodeDecodeError``,
whose ``object`` is the whole body) and on a deeply nested array
(``RecursionError``). Each refuses with ``YahooResponseFormatError("", "the
body isn't JSON")``. That error is frozen, so the reader raises it after its
handler, from a content-free stand-in naming only the parser error's class and
line: the refusal has no context (except a caller's own handled exception), and
``IBKR_FA_TRACEBACK=1`` still re-raises it as an error that replaced a crash.
Retries, the price cache and the CLI's exit, streams and output cleanup are
unchanged.
"""

from __future__ import annotations

import io
import json
import re
import urllib.request
import urllib.response
from collections.abc import Callable
from datetime import date, datetime
from email.message import Message
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.exceptions import ReplacedCrashError
from ibkr_fa.prices import yahoo
from ibkr_fa.prices.cache import _cache_path, load_or_fetch, write_cache
from ibkr_fa.prices.loader import load_price_series
from ibkr_fa.prices.series import PriceSeries
from ibkr_fa.prices.yahoo import (
    YahooResponseFormatError,
    _default_url_opener_result,
    fetch_daily_closes_usd,
    fetch_daily_closes_usd_retrying,
)
from ibkr_fa.types import Ticker
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML
from tests._crash_reports import (
    assert_prints_no_marker,
    assert_replaced_unchained,
    crash_pattern,
    raise_context,
)
from tests._deep_json import DEEP_JSON
from tests._ibkr_run_all import CY_2025_INPUT
from tests._price_cache import CY_2025_FIRST_PRICED_TICKER as _STATEMENT_TICKER
from tests._price_cache import cache_series as _series
from tests._yahoo_prices import NOT_JSON_REFUSAL as _NOT_JSON_REFUSAL
from tests._yahoo_prices import YAHOO_NOW

# The holding's symbol (the request and the body name it) and one neutral
# marker per kind of body content.
_SYMBOL = "QZ25YSYM"
_DOCUMENT = "QZ25YDOC"
_BYTES = "QZ25YBYTE"
_HOLDING = Ticker(_SYMBOL)
_START = date(2025, 3, 1)
_END = date(2025, 3, 31)


class _Body(NamedTuple):
    """A response body ``json.loads`` refuses, the class of the parser's error
    and the markers the body carries."""

    raw: bytes
    parser_error: type[BaseException]
    markers: tuple[str, ...]


# A document cut off after a member name; the JSON error's ``doc`` is the body.
_TRUNCATED = _Body(
    f'{{"chart": {{"result": [{{"meta": {{"symbol": "{_SYMBOL}"}}, "{_DOCUMENT}": '.encode(),
    json.JSONDecodeError,
    (_SYMBOL, _DOCUMENT),
)
# One byte that isn't UTF-8, so the decode error's ``object`` holds the body.
_NOT_UTF8 = _Body(
    f'{{"chart": {{"result": [{{"meta": {{"symbol": "{_SYMBOL}"}}, "{_BYTES}": "'.encode()
    + b'\xff"}]}}',
    UnicodeDecodeError,
    (_SYMBOL, _BYTES),
)
# Deeper than either Python's parser recurses; its error holds no content.
_NESTED = _Body(DEEP_JSON, RecursionError, ())
_BODIES = pytest.mark.parametrize(
    "body",
    [
        pytest.param(_TRUNCATED, id="truncated-json"),
        pytest.param(_NOT_UTF8, id="not-utf-8"),
        pytest.param(_NESTED, id="deeply-nested"),
    ],
)
_REFUSAL = YahooResponseFormatError("", "the body isn't JSON")


def _assert_refusal(error: BaseException, body: _Body) -> None:
    """``error`` is exactly the not-JSON refusal. No marker of ``body`` is
    anywhere in its exception graph; it has no context, and its only cause is
    the stand-in naming the parser error's class and a line."""
    assert type(error) is YahooResponseFormatError
    assert error == _REFUSAL
    assert str(error) == _NOT_JSON_REFUSAL
    assert_replaced_unchained(error, body.parser_error, markers=body.markers)


class _Opener:
    """A Yahoo transport answering each request with the next reply (``None``:
    a transport failure); records the URLs requested."""

    def __init__(self, *replies: bytes | None) -> None:
        self.replies = list(replies)
        self.urls: list[str] = []

    def __call__(self, url: str, _timeout: float) -> bytes | None:
        self.urls.append(url)
        return self.replies.pop(0)


def _chart_paths(urls: list[str]) -> list[str]:
    """The path of each URL requested."""
    return [urlsplit(url).path for url in urls]


_CHART_PATH = f"/v8/finance/chart/{_SYMBOL}"


class _Fetcher:
    """The retrying Yahoo fetcher over ``opener``, recording each retry sleep."""

    def __init__(self, opener: _Opener) -> None:
        self.opener = opener
        self.sleeps: list[float] = []

    def __call__(
        self,
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        return fetch_daily_closes_usd_retrying(
            ticker,
            start=start,
            end=end,
            now=now,
            fetch_symbol=fetch_symbol,
            url_opener=self.opener,
            retry_sleep=self.sleeps.append,
        )


def _fetch(fetcher: _Fetcher) -> PriceSeries | None:
    return fetcher(_HOLDING, start=_START, end=_END, now=YAHOO_NOW)


# --- The retrying fetcher ------------------------------------------------------


@_BODIES
def test_a_body_that_is_not_json_refuses_on_the_first_attempt(body: _Body) -> None:
    fetcher = _Fetcher(_Opener(body.raw))

    with pytest.raises(YahooResponseFormatError) as refusal:
        _fetch(fetcher)

    _assert_refusal(refusal.value, body)
    assert _chart_paths(fetcher.opener.urls) == [_CHART_PATH]
    assert fetcher.sleeps == []


@_BODIES
def test_a_transport_failure_is_retried_once_before_the_refusal(body: _Body) -> None:
    """A transient failure still waits half a second and tries again; the
    body that isn't JSON then refuses without a further retry."""
    fetcher = _Fetcher(_Opener(None, body.raw))

    with pytest.raises(YahooResponseFormatError) as refusal:
        _fetch(fetcher)

    _assert_refusal(refusal.value, body)
    assert _chart_paths(fetcher.opener.urls) == [_CHART_PATH, _CHART_PATH]
    assert fetcher.sleeps == [0.5]


# --- The price cache loaders ---------------------------------------------------


def _load_or_fetch(cache_dir: Path, fetcher: _Fetcher) -> object:
    return load_or_fetch(
        _HOLDING, start=_START, end=_END, cache_dir=cache_dir, fetcher=fetcher, now=YAHOO_NOW
    )


def _load_price_series(cache_dir: Path, fetcher: _Fetcher) -> object:
    return load_price_series(
        {_HOLDING: (_START, _END)}, cache_dir=cache_dir, fetcher=fetcher, now=YAHOO_NOW
    )


def _entries(cache_dir: Path) -> dict[str, bytes] | None:
    """Every entry of ``cache_dir`` with its bytes; ``None`` if it's absent."""
    if not cache_dir.exists():
        return None
    return {path.name: path.read_bytes() for path in cache_dir.iterdir()}


@pytest.mark.parametrize("cached", [False, True], ids=["no-cache", "cache-for-another-span"])
@pytest.mark.parametrize(
    "load", [_load_or_fetch, _load_price_series], ids=["load_or_fetch", "load_price_series"]
)
@_BODIES
def test_price_loaders_write_no_cache_for_a_body_that_is_not_json(
    tmp_path: Path,
    body: _Body,
    load: Callable[[Path, _Fetcher], object],
    cached: bool,
) -> None:
    """The loader fetches the holding's chart once and refuses. No cache is
    written, and a valid one for another span is left as it was."""
    cache_dir = tmp_path / "cache"
    if cached:
        write_cache(
            _cache_path(cache_dir, _HOLDING),
            _series(ticker=_SYMBOL),
            covered_start=date(2025, 12, 25),
            covered_end=date(2025, 12, 31),
            fetch_symbol=_SYMBOL,
        )
    before = _entries(cache_dir)
    fetcher = _Fetcher(_Opener(body.raw))

    with pytest.raises(YahooResponseFormatError) as refusal:
        load(cache_dir, fetcher)

    _assert_refusal(refusal.value, body)
    assert _chart_paths(fetcher.opener.urls) == [_CHART_PATH]
    assert fetcher.sleeps == []
    assert _entries(cache_dir) == before
    assert (before is not None) is cached


# --- A caller's handler ----------------------------------------------------------


@_BODIES
def test_refusal_under_a_caller_handler_has_only_that_exception_as_context(
    body: _Body,
) -> None:
    """The refusal is raised after the reader's own handler, so a caller's
    handled exception is its context, as for any raise in a handler; the
    parser's error is still only the stand-in."""
    fetcher = _Fetcher(_Opener(body.raw))

    try:
        raise_context()
    except ValueError as caller:
        handled = caller
        with pytest.raises(YahooResponseFormatError) as refusal:
            _fetch(fetcher)

    error = refusal.value
    assert type(error) is YahooResponseFormatError
    assert error == _REFUSAL
    assert str(error) == _NOT_JSON_REFUSAL
    if body.markers:
        assert_prints_no_marker(error, markers=body.markers)
    assert error.__context__ is handled
    stand_in = error.__cause__
    assert type(stand_in) is ReplacedCrashError
    assert re.fullmatch(crash_pattern(body.parser_error), str(stand_in)), str(stand_in)
    assert stand_in.__cause__ is None
    assert stand_in.__context__ is None


# --- The CLI -------------------------------------------------------------------


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[str]:
    """Send the CLI's default price fetch through the real Yahoo fetcher and
    transport, with ``urlopen`` answering every request with ``body``; returns
    the URLs requested."""
    urls: list[str] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> urllib.response.addinfourl:
        del timeout
        urls.append(request.full_url)
        return urllib.response.addinfourl(io.BytesIO(body), Message(), request.full_url, 200)

    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", fetch_daily_closes_usd)
    monkeypatch.setattr(yahoo, "_default_url_opener_result", _default_url_opener_result)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return urls


def _schedule_fa_argv(tmp_path: Path) -> list[str]:
    """Online ``schedule-fa`` over the CY 2025 statement, whose earlier run's
    outputs are in ``out``."""
    config = tmp_path / "user.toml"
    config.write_text(ACTIVITY_STATEMENT_E2E_USER_TOML, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    for stale in ("schedule-fa.csv", "schedule-fa-a2.csv"):
        (out / stale).write_bytes(b"an earlier run")
    return [
        "schedule-fa",
        *("--input", str(CY_2025_INPUT), "--config", str(config)),
        *("--output", str(out / "schedule-fa.csv"), "--calendar-year", "2025"),
        *("--price-cache-dir", str(tmp_path / "cache")),
    ]


@_BODIES
def test_schedule_fa_refuses_a_body_that_is_not_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: _Body,
) -> None:
    """The statement's first priced holding is fetched once; the run exits 4
    with the fixed message only, removes its earlier outputs and writes no
    price cache."""
    urls = _serve(monkeypatch, body.raw)
    argv = _schedule_fa_argv(tmp_path)

    assert main(argv) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {_NOT_JSON_REFUSAL}\n"
    assert _chart_paths(urls) == [f"/v8/finance/chart/{_STATEMENT_TICKER}"]
    assert list((tmp_path / "out").iterdir()) == []
    assert not (tmp_path / "cache").exists()


@_BODIES
def test_traceback_mode_still_reraises_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: _Body,
) -> None:
    """``IBKR_FA_TRACEBACK=1`` re-raises the refusal, after cleanup, as a
    domain error that replaced a crash, with nothing of the parser's error but
    its stand-in."""
    urls = _serve(monkeypatch, body.raw)
    argv = _schedule_fa_argv(tmp_path)
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with pytest.raises(YahooResponseFormatError) as refusal:
        main(argv)

    assert cli._holds_crash(refusal.value)
    _assert_refusal(refusal.value, body)
    assert capsys.readouterr().out == ""
    assert _chart_paths(urls) == [f"/v8/finance/chart/{_STATEMENT_TICKER}"]
    assert list((tmp_path / "out").iterdir()) == []
    assert not (tmp_path / "cache").exists()
