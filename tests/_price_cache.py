"""The price series, marker caches, cache writers, refusal texts and refusal
checks that the price cache tests and the commands' price cache tests share,
plus the CY 2025 statement's first priced ticker and a command run's price flags.
Every value is synthetic.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa.prices.cache import (
    PriceCacheError,
    _cache_path,
    load_or_fetch,
    read_cache,
    write_cache,
    write_cache_lock,
)
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.reference.us_market_calendar import us_market_sessions
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_no_markers, render_without_frames

# The price cache tests' clock, 1 June 2026 12:00 UTC: cache_series stamps it as
# fetched_at. The Yahoo price tests use YAHOO_NOW (tests/_yahoo_prices.py),
# 00:00 UTC the same day.
CACHE_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
# The #940 example: CY 2025 plus the loader's 10-day lookback.
EXAMPLE_START = date(2024, 12, 22)
EXAMPLE_END = date(2025, 12, 31)


def cache_series(
    *,
    ticker: str = "XQAA",
    closes: dict[date, Decimal] | None = None,
    currency: str = "USD",
    has_split: bool = False,
    highs: dict[date, Decimal] | None = None,
    split_ratios: dict[date, Decimal] | None = None,
    price_basis: str = "current-split-basis",
    source: str = "https://example/chart",
) -> PriceSeries:
    return PriceSeries(
        ticker=Ticker(ticker),
        currency=currency,
        closes=closes
        or {date(2025, 12, 29): Decimal("128.40"), date(2025, 12, 30): Decimal("127.5")},
        highs=highs or {},
        has_split_in_range=has_split,
        source=source,
        fetched_at=CACHE_NOW,
        split_ratios=split_ratios or {},
        price_basis=price_basis,
    )


def with_payload_hash(text: str) -> str:
    """``text`` with a valid ``payload_sha256`` line in place of any it has."""
    body = "".join(
        line for line in text.splitlines(keepends=True) if not line.startswith("# payload_sha256=")
    )
    return f"# payload_sha256={hashlib.sha256(body.encode()).hexdigest()}\n{body}"


def price_cache_refusal(path: Path, problem: str, *, locked: bool) -> str:
    """The exact #940 refusal for the cache file at ``path``.

    It names the cache folder, not the file: the file name is built from the
    holding's symbol, which is input (#1047).
    """
    advice = (
        "Restore or rebuild a reviewed cache with a valid payload hash, then re-lock it; "
        "or move both it and its .csv.lock file aside so the next online run fetches a "
        "fresh one."
        if locked
        else "Move it aside so the next online run fetches a fresh one, or restore the original."
    )
    return (
        f"Price cache folder {path.parent}: the cache file with one holding's prices "
        f"{problem}. {advice}"
    )


def assert_price_cache_refusal(
    message: str, path: Path, problem: str, *, locked: bool = False
) -> None:
    """``message`` is exactly the refusal, without the symbol-built file names (#1047)."""
    assert message == price_cache_refusal(path, problem, locked=locked)
    for name in (path.name, f"{path.name}.lock"):
        assert name.lower() not in message.lower()


def write_example_cache(cache_dir: Path, ticker: str = "X") -> Path:
    """Write the #940 example cache for ``ticker`` and return its path.

    A contemporaneous-basis cache covering ``EXAMPLE_START`` to
    ``EXAMPLE_END`` with every close $100, whose ``payload_sha256`` line
    is then deleted and whose 3 June 2025 close is changed to $999.
    """
    path = _cache_path(cache_dir, Ticker(ticker))
    write_cache(
        path,
        cache_series(
            ticker=ticker,
            closes=dict.fromkeys(us_market_sessions(EXAMPLE_START, EXAMPLE_END), Decimal("100")),
            price_basis=CONTEMPORANEOUS_BASIS,
        ),
        covered_start=EXAMPLE_START,
        covered_end=EXAMPLE_END,
        fetch_symbol=ticker,
    )
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("# payload_sha256=")]
    assert len(kept) == len(lines) - 1
    assert kept.count("2025-06-03,100,\n") == 1
    path.write_text(
        "".join(kept).replace("2025-06-03,100,\n", "2025-06-03,999,\n"), encoding="utf-8"
    )
    return path


class RecordingFetcher:
    def __init__(self, result: PriceSeries | None) -> None:
        self.result = result
        self.calls: list[tuple[Ticker, date, date]] = []
        self.fetch_symbols: list[str | None] = []

    def __call__(
        self,
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        self.calls.append((ticker, start, end))
        self.fetch_symbols.append(fetch_symbol)
        return self.result


def fail_fetch(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    now: datetime,
    fetch_symbol: str | None = None,
) -> PriceSeries | None:
    pytest.fail("load_or_fetch called the fetcher")


def price_source_called(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("the price source was called")


# The CY 2025 statement's first priced holding, so its cache is read first. The
# statement is tests/_ibkr_run_all.py's CY_2025_INPUT.
CY_2025_FIRST_PRICED_TICKER = "XQBB"


def price_flags(monkeypatch: pytest.MonkeyPatch, *, online: bool) -> tuple[str, ...]:
    """The run's price flags. Online, the pipeline's default fetcher fails the
    test if called."""
    if not online:
        return ("--no-fetch-prices",)
    monkeypatch.setattr("ibkr_fa.prices.loader.fetch_daily_closes_usd", price_source_called)
    return ()


# Distinct privacy markers. The refusal checks assert that a refusal prints none
# that its input holds.
CACHE_MARKER_SYMBOL = "XQ_CACHE_MARKER"
CACHE_MARKER_DESCRIPTION = "Synthetic Cache Description"
CACHE_MARKER_SOURCE = "synthetic-cache-source"
CACHE_MARKER_DATE = "2025-02-13"
CACHE_MARKER_NUMBER = "6_731.4289"
CACHE_MARKER_KEY = "synthetic-cache-key"
ALL_CACHE_MARKERS = (
    CACHE_MARKER_SYMBOL,
    CACHE_MARKER_DESCRIPTION,
    CACHE_MARKER_SOURCE,
    CACHE_MARKER_DATE,
    CACHE_MARKER_NUMBER,
    CACHE_MARKER_KEY,
)
_MARKER_START = date(2025, 2, 3)
_MARKER_END = date(2025, 2, 28)
# The writer's layout, line 1 being the payload_sha256 line ``with_payload_hash`` adds:
# comment/metadata on lines 2-12, the table header on 13, rows on 14-15, then a comment.
MARKER_CACHE_BODY = (
    "# ibkr-fa price cache - regenerated from the price source; do not edit\n"
    f"# ticker={CACHE_MARKER_SYMBOL}\n"
    f"# fetch_symbol={CACHE_MARKER_SYMBOL}\n"
    "# currency=USD\n"
    "# has_split_in_range=False\n"
    "# split_ratios={}\n"
    "# price_basis=contemporaneous\n"
    f"# source={CACHE_MARKER_SOURCE}\n"
    "# fetched_at=2026-06-01T12:00:00+00:00\n"
    "# covered_start=2025-02-01\n"
    "# covered_end=2025-02-28\n"
    "date,close_usd,high_usd\n"
    f"{CACHE_MARKER_DATE},6731.4289,6800.5\n"
    "2025-02-14,6740.1,\n"
    f"# {CACHE_MARKER_DESCRIPTION}\n"
)
CACHE_HEADER = "date,close_usd,high_usd\n"
MARKER_CACHE_ROW = f"{CACHE_MARKER_DATE},6731.4289,6800.5\n"
CACHE_COLUMNS = "columns date, close_usd and high_usd"
# Text the stdlib parsers put in their exceptions; no refusal prints it.
_PARSER_TEXTS = ("expected after", "unexpected end of data", "field larger", "Expecting")


# The #1170 layout refusal the filing-pipeline tests put in a command's cache.
UNSUPPORTED_LAYOUT = f"has an unsupported table header (line 13, {CACHE_COLUMNS})"
# The markers that cache holds; no stderr or error may print them.
CACHE_MARKERS = (
    CACHE_MARKER_SYMBOL,
    CACHE_MARKER_DESCRIPTION,
    CACHE_MARKER_SOURCE,
    CACHE_MARKER_DATE,
)


def write_unsupported_layout_cache(cache_dir: Path, ticker: str) -> Path:
    """Write a correctly hashed cache for ``ticker`` whose table header swaps
    ``close_usd`` and ``high_usd``, and return its path (#1170). The reader
    refuses it with :data:`UNSUPPORTED_LAYOUT` before using or fetching
    anything; its own ticker line is a marker, not ``ticker``."""
    path = _cache_path(cache_dir, Ticker(ticker))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = MARKER_CACHE_BODY.replace(CACHE_HEADER, "date,high_usd,close_usd\n")
    path.write_text(with_payload_hash(body), encoding="utf-8")
    return path


def unsupported_layout_message(cache_dir: Path, *, locked: bool = False) -> str:
    """The complete refusal for :func:`write_unsupported_layout_cache`'s cache
    in ``cache_dir``, the folder as the command was given it."""
    return price_cache_refusal(cache_dir / "x.csv", UNSUPPORTED_LAYOUT, locked=locked)


# The folded high-below-close check's markers, in the same marker order:
# symbol, description, source, date, close, high, reason.
HIGH_CACHE_MARKERS = (
    "QZ_HIGH_CACHE",
    "synthetic-high-description",
    "synthetic-high-source",
    "2025-12-31",
    "211.3579",
    "210.2468",
    "synthetic-high-reason",
)
HIGH_CACHE_ROW = "2025-12-31,211.3579,210.2468\n"
# A cache whose one row has a high below its close; the row is on line 14.
# Its span covers the refusal tests' February request.
HIGH_CACHE_BODY = (
    MARKER_CACHE_BODY.replace(CACHE_MARKER_SYMBOL, HIGH_CACHE_MARKERS[0])
    .replace(CACHE_MARKER_DESCRIPTION, HIGH_CACHE_MARKERS[1])
    .replace(CACHE_MARKER_SOURCE, HIGH_CACHE_MARKERS[2])
    .replace("# covered_end=2025-02-28\n", "# covered_end=2025-12-31\n")
    .replace(MARKER_CACHE_ROW + "2025-02-14,6740.1,\n", HIGH_CACHE_ROW)
    + f"# {HIGH_CACHE_MARKERS[6]}\n"
)
HIGH_BELOW_CLOSE = "has a high below its close (line 14, column high_usd)"


def write_high_below_close_cache(cache_dir: Path, ticker: str) -> Path:
    """Write a correctly hashed cache for ``ticker`` whose one row (31 December
    2025) has a high below its close, and return its path. The reader refuses it
    with :data:`HIGH_BELOW_CLOSE` before using or fetching anything."""
    path = _cache_path(cache_dir, Ticker(ticker))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(with_payload_hash(HIGH_CACHE_BODY), encoding="utf-8")
    return path


def high_below_close_message(cache_dir: Path, *, locked: bool = False) -> str:
    """The complete refusal for :func:`write_high_below_close_cache`'s cache."""
    return price_cache_refusal(cache_dir / "x.csv", HIGH_BELOW_CLOSE, locked=locked)


def edit_once(old: str, new: str) -> Callable[[str], str]:
    """Replace the one occurrence of ``old`` in the cache body with ``new``."""

    def edit(body: str) -> str:
        assert body.count(old) == 1, old
        return body.replace(old, new)

    return edit


def split_ratios_edit(value: str) -> Callable[[str], str]:
    """Write ``value`` as line 7's split_ratios, with the split flag True."""
    return lambda body: edit_once("# split_ratios={}\n", f"# split_ratios={value}\n")(
        edit_once("# has_split_in_range=False\n", "# has_split_in_range=True\n")(body)
    )


def _assert_clean_refusal(
    error: PriceCacheError,
    path: Path,
    problem: str,
    *,
    locked: bool,
    text: str,
    markers: tuple[str, ...],
) -> None:
    """``error`` is exactly the refusal, chains no exception and prints none of
    ``markers`` that ``text`` holds (its symbol and source always), parser
    diagnostics or the symbol-built file name (#1170)."""
    assert type(error) is PriceCacheError
    assert_price_cache_refusal(str(error), path, problem, locked=locked)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = render_without_frames(error)
    present = [marker for marker in markers if marker in text]
    assert {markers[0], markers[2]} <= set(present)
    for shown in (str(error), rendered):
        assert_no_markers(shown, *present, *_PARSER_TEXTS, path.name)


def cache_entries(directory: Path) -> list[tuple[str, bytes]]:
    return sorted((entry.name, entry.read_bytes()) for entry in directory.iterdir())


def assert_cache_refuses(
    tmp_path: Path,
    text: str,
    problem: str,
    *,
    locked: bool,
    markers: tuple[str, ...] = ALL_CACHE_MARKERS,
) -> None:
    """``text`` as a cache for ``markers[0]`` refuses on read and through
    ``load_or_fetch`` with refresh and fetch on or off, never fetching or
    changing a file (#1170)."""
    ticker = Ticker(markers[0])
    path = _cache_path(tmp_path, ticker)
    path.write_text(text, encoding="utf-8")
    if locked:
        write_cache_lock(path)
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path, locked=locked)
    _assert_clean_refusal(exc.value, path, problem, locked=locked, text=text, markers=markers)
    before = cache_entries(tmp_path)
    for refresh in (False, True):
        for fetch in (False, True):
            with pytest.raises(PriceCacheError) as exc:
                load_or_fetch(
                    ticker,
                    start=_MARKER_START,
                    end=_MARKER_END,
                    cache_dir=tmp_path,
                    fetcher=fail_fetch,
                    now=CACHE_NOW,
                    refresh=refresh,
                    fetch=fetch,
                )
            _assert_clean_refusal(
                exc.value, path, problem, locked=locked, text=text, markers=markers
            )
            assert cache_entries(tmp_path) == before


def read_marker_cache(tmp_path: Path, body: str) -> tuple[PriceSeries, date, date, str]:
    path = _cache_path(tmp_path, Ticker(CACHE_MARKER_SYMBOL))
    path.write_text(with_payload_hash(body), encoding="utf-8")
    out = read_cache(path)
    assert out is not None
    return out
