"""Tests for :mod:`ibkr_fa.prices.cache` (issue #14).

The cache doubles as the immutable audit artifact, so round-trip
fidelity and the cache-hit/miss/refresh logic are verified here with an
injected fetcher (no network).
"""

from __future__ import annotations

import csv
import errno
import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import pytest

from ibkr_fa.exceptions import InvariantError
from ibkr_fa.prices.cache import (
    PriceCacheError,
    _cache_path,
    load_or_fetch,
    read_cache,
    write_cache,
    write_cache_lock,
)
from ibkr_fa.prices.series import (
    CONTEMPORANEOUS_BASIS,
    PriceSeries,
    PriceSourceContradictionError,
)
from ibkr_fa.prices.yahoo import DailyCloseFetcher, YahooResponseFormatError
from ibkr_fa.types import Ticker
from tests._crash_reports import assert_replaced_unchained
from tests._price_cache import CACHE_COLUMNS as _COLUMNS
from tests._price_cache import CACHE_HEADER as _HEADER
from tests._price_cache import CACHE_MARKER_DATE as _MARKER_DATE
from tests._price_cache import CACHE_MARKER_DESCRIPTION as _MARKER_DESCRIPTION
from tests._price_cache import CACHE_MARKER_KEY as _MARKER_KEY
from tests._price_cache import CACHE_MARKER_NUMBER as _MARKER_NUMBER
from tests._price_cache import CACHE_MARKER_SOURCE as _MARKER_SOURCE
from tests._price_cache import CACHE_MARKER_SYMBOL as _MARKER_SYMBOL
from tests._price_cache import (
    CACHE_NOW,
    high_below_close_message,
    unsupported_layout_message,
    write_example_cache,
    write_high_below_close_cache,
    write_unsupported_layout_cache,
)
from tests._price_cache import EXAMPLE_END as _EXAMPLE_END
from tests._price_cache import EXAMPLE_START as _EXAMPLE_START
from tests._price_cache import HIGH_CACHE_BODY as _HIGH_BODY
from tests._price_cache import HIGH_CACHE_MARKERS as _HIGH_MARKERS
from tests._price_cache import HIGH_CACHE_ROW as _HIGH_ROW
from tests._price_cache import MARKER_CACHE_BODY as _MARKER_BODY
from tests._price_cache import MARKER_CACHE_ROW as _ROW
from tests._price_cache import RecordingFetcher as _RecordingFetcher
from tests._price_cache import assert_cache_refuses as _assert_cache_refuses
from tests._price_cache import assert_price_cache_refusal as _assert_refusal
from tests._price_cache import cache_entries as _entries
from tests._price_cache import cache_series as _series
from tests._price_cache import edit_once as _edit
from tests._price_cache import fail_fetch as _fail_fetch
from tests._price_cache import read_marker_cache as _read_marker_cache
from tests._price_cache import split_ratios_edit as _ratios
from tests._price_cache import with_payload_hash as _hashed
from tests._yahoo_prices import repeated_date_fetcher, split_fetcher


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    series = _series(
        has_split=True,
        split_ratios={date(2026, 1, 5): Decimal("2")},
        price_basis=CONTEMPORANEOUS_BASIS,
    )
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        series,
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    out = read_cache(path)
    assert out is not None
    restored, cstart, cend, fetch_symbol = out
    assert restored.ticker == series.ticker
    assert restored.currency == "USD"
    assert restored.has_split_in_range is True
    assert restored.closes == series.closes
    assert restored.source == series.source
    assert restored.fetched_at == series.fetched_at
    assert restored.split_ratios == series.split_ratios
    assert restored.price_basis == CONTEMPORANEOUS_BASIS
    assert (cstart, cend) == (date(2025, 12, 20), date(2025, 12, 31))
    assert fetch_symbol == "XQAA"


def test_cache_roundtrips_multiple_split_ratios(tmp_path: Path) -> None:
    series = _series(
        has_split=True,
        split_ratios={
            date(2026, 1, 5): Decimal("2"),
            date(2026, 2, 5): Decimal("4"),
        },
        price_basis=CONTEMPORANEOUS_BASIS,
    )
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        series,
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    restored = read_cache(path)
    assert restored is not None
    assert restored[0].split_ratios == series.split_ratios


def test_read_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_cache(tmp_path / "nope.csv") is None


def test_read_malformed_file_refuses(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("# ticker=X\nthis is not valid\n", encoding="utf-8")
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path)
    _assert_refusal(str(exc.value), path, "has no payload_sha256 line")


# #1140's test caches. Their symbol, source, date and close are all input, and
# the exact refusal text holds none of them.
_XQCH = _series(
    ticker="XQCH",
    source="synthetic-source-77",
    closes={date(2025, 3, 4): Decimal("4.8219")},
    highs={date(2025, 3, 4): Decimal("4.8219")},
)
_XQCH_ROW = "2025-03-04,4.8219,4.8219\n"
_UNUSABLE_CLOSES = ("NaN", "0", "-1")
_SPLIT_FLAG_VALUE = "has a has_split_in_range value that isn't exactly True or False (line {})"
_SPLIT_FLAG_REPEATED = "has more than one has_split_in_range line (line {})"
# #1170's fixed phrases, by line.
_CLOSE_TEXT = (
    "has close text outside the supported ASCII decimal grammar (line {}, column close_usd)"
)
_MALFORMED_RECORD = (
    "has malformed CSV quoting, a multiline record or an over-limit cell "
    "(line {}, field CSV record)"
)


def _split_flags(*values: str) -> Callable[[str], str]:
    """An edit that writes one ``has_split_in_range`` line per value in place of
    the cache's ``False`` line (line 6 of a cache that ``_hashed`` writes)."""
    lines = "".join(f"# has_split_in_range={value}\n" for value in values)
    return lambda text: text.replace("# has_split_in_range=False\n", lines)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize(
    ("series", "edit", "problem"),
    [
        pytest.param(
            _series(),
            lambda text: f"{text}2025-12-29,1,\n",
            "has two rows for one date (line 16, column date)",
            id="duplicate-date",
        ),
        pytest.param(
            _series(),
            lambda text: text.replace("# currency=USD\n", ""),
            "doesn't parse",
            id="missing-field",
        ),
        # #1170: a split_ratios value that isn't a JSON object now names its line.
        pytest.param(
            _series(),
            lambda text: text.replace("split_ratios={}", "split_ratios=[]"),
            "has split_ratios that is not a JSON object (line 7, key split_ratios)",
            id="ratios-not-object",
        ),
        pytest.param(
            _series(),
            lambda text: text.replace("2025-12-20", "2026-01-01"),
            "has covered_start after covered_end",
            id="reversed-span",
        ),
        pytest.param(
            _series(),
            lambda text: text.replace(",128.40,", ",0,").replace(",127.5,", ",-1,"),
            "has a close that isn't a positive price (line 14, column close_usd)",
            id="no-close",
        ),
        pytest.param(
            _series(),
            lambda text: text.replace("2025-12-29,128.40,\n", "").replace(
                "2025-12-30,127.5,\n", ""
            ),
            "has no usable close",
            id="no-rows",
        ),
        pytest.param(
            _series(has_split=True, price_basis=CONTEMPORANEOUS_BASIS),
            lambda text: text,
            "records a split on the contemporaneous basis without its ratios",
            id="split-without-ratios",
        ),
        # #1140: the first row on a date refuses before a second one can. #1170's
        # decimal grammar refuses NaN text before the positive-price check.
        *(
            pytest.param(
                _XQCH,
                lambda text, close=close: text.replace(
                    _XQCH_ROW, f"2025-03-04,{close},\n{_XQCH_ROW}"
                ),
                _CLOSE_TEXT.format(14)
                if close == "NaN"
                else "has a close that isn't a positive price (line 14, column close_usd)",
                id=f"close-{close}-then-usable",
            )
            for close in _UNUSABLE_CLOSES
        ),
        # The date check comes before the close check.
        *(
            pytest.param(
                _XQCH,
                lambda text, close=close: text.replace(
                    _XQCH_ROW, f"{_XQCH_ROW}2025-03-04,{close},\n"
                ),
                "has two rows for one date (line 15, column date)",
                id=f"usable-then-close-{close}",
            )
            for close in _UNUSABLE_CLOSES
        ),
        *(
            pytest.param(
                _XQCH, _split_flags(value), _SPLIT_FLAG_VALUE.format(6), id=f"split-flag-{name}"
            )
            for value, name in (
                ("true", "lowercase"),
                ("yes", "yes"),
                ("1", "one"),
                ("", "empty"),
                (" True", "leading-space"),
                ("False ", "trailing-space"),
            )
        ),
        pytest.param(
            _XQCH,
            _split_flags("yes", "False"),
            _SPLIT_FLAG_VALUE.format(6),
            id="split-flag-invalid-then-false",
        ),
        pytest.param(
            _XQCH,
            _split_flags("False", "yes"),
            _SPLIT_FLAG_VALUE.format(7),
            id="split-flag-false-then-invalid",
        ),
        pytest.param(
            _XQCH,
            _split_flags("True", "False"),
            _SPLIT_FLAG_REPEATED.format(7),
            id="split-flag-true-then-false",
        ),
        pytest.param(
            _XQCH,
            _split_flags("False", "True"),
            _SPLIT_FLAG_REPEATED.format(7),
            id="split-flag-false-then-true",
        ),
    ],
)
def test_read_cache_refuses_content_that_fails_a_check(
    tmp_path: Path,
    series: PriceSeries,
    edit: Callable[[str], str],
    problem: str,
    locked: bool,
) -> None:
    """The cache refuses on read and through ``load_or_fetch``, locked or not,
    and nothing is fetched or written (#940, #1140)."""
    path = _cache_path(tmp_path, series.ticker)
    write_cache(
        path,
        series,
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol=series.ticker,
    )
    original = path.read_text(encoding="utf-8")
    edited = edit(original)
    assert edited != original or problem == (
        "records a split on the contemporaneous basis without its ratios"
    )
    path.write_text(_hashed(edited), encoding="utf-8")
    if locked:
        write_cache_lock(path)
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path, locked=locked)
    _assert_refusal(str(exc.value), path, problem, locked=locked)
    before = sorted((entry.name, entry.read_bytes()) for entry in tmp_path.iterdir())
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            series.ticker,
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=_fail_fetch,
            now=CACHE_NOW,
        )
    _assert_refusal(str(exc.value), path, problem, locked=locked)
    assert sorted((entry.name, entry.read_bytes()) for entry in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("make", "problem"),
    [
        # An OSError adds its errno name; a decode error has none (#1047).
        (Path.mkdir, "can't be read (EISDIR)"),
        (lambda path: path.write_bytes(b"\xff\n"), "can't be read"),
    ],
    ids=["directory", "not-utf8"],
)
def test_read_cache_refuses_a_file_it_cannot_read(
    tmp_path: Path, make: Callable[[Path], object], problem: str
) -> None:
    path = tmp_path / "XQAA.csv"
    make(path)
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path)
    _assert_refusal(str(exc.value), path, problem)


@pytest.mark.parametrize("fetch", [True, False], ids=["online", "offline"])
def test_example_cache_without_hash_refuses(tmp_path: Path, fetch: bool) -> None:
    path = write_example_cache(tmp_path)
    before = path.read_bytes()
    fetcher = _RecordingFetcher(_series(ticker="X"))
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("X"),
            start=_EXAMPLE_START,
            end=_EXAMPLE_END,
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
            fetch=fetch,
        )
    _assert_refusal(str(exc.value), path, "has no payload_sha256 line")
    assert fetcher.calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("fetch", [True, False], ids=["online", "offline"])
@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda text: text.replace("128.40", "999.00"), "doesn't match its payload_sha256 line"),
        # #1170: these refused as unparseable; each now names its line.
        (lambda text: _hashed(text.replace("128.40", "abc")), _CLOSE_TEXT.format(14)),
        (
            lambda text: _hashed(text.replace("128.40", "1" * (csv.field_size_limit() + 1))),
            _MALFORMED_RECORD.format(14),
        ),
    ],
    ids=["wrong-hash", "unparseable", "oversized-field"],
)
def test_invalid_cache_refuses_without_fetching(
    tmp_path: Path, fetch: bool, edit: Callable[[str], str], problem: str
) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    original = path.read_text(encoding="utf-8")
    edited = edit(original)
    assert edited != original
    path.write_text(edited, encoding="utf-8")
    before = path.read_bytes()
    fetcher = _RecordingFetcher(_series())
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
            fetch=fetch,
        )
    _assert_refusal(str(exc.value), path, problem)
    assert fetcher.calls == []
    assert path.read_bytes() == before


def test_load_or_fetch_miss_calls_fetcher_and_writes(tmp_path: Path) -> None:
    fetcher = _RecordingFetcher(_series())
    out = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert out is not None
    assert len(fetcher.calls) == 1
    # Cache written with a hash that read_cache accepts.
    assert read_cache(_cache_path(tmp_path, Ticker("XQAA"))) is not None


def test_load_or_fetch_offline_miss_returns_none(tmp_path: Path) -> None:
    fetcher = _RecordingFetcher(_series())
    out = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
        fetch=False,
    )
    assert out is None
    assert fetcher.calls == []
    assert list(tmp_path.iterdir()) == []


def test_load_or_fetch_hit_skips_fetcher(tmp_path: Path) -> None:
    write_cache(
        _cache_path(tmp_path, Ticker("XQAA")),
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _RecordingFetcher(_series())
    out = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert out is not None
    assert fetcher.calls == []  # served from cache


def test_load_or_fetch_partial_cache_refetches(tmp_path: Path) -> None:
    # Cache covers only part of the requested window -> must refetch.
    write_cache(
        _cache_path(tmp_path, Ticker("XQAA")),
        _series(),
        covered_start=date(2025, 12, 25),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _RecordingFetcher(_series())
    load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),  # earlier than covered_start
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert len(fetcher.calls) == 1


def test_load_or_fetch_refresh_ignores_cache(tmp_path: Path) -> None:
    write_cache(
        _cache_path(tmp_path, Ticker("XQAA")),
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _RecordingFetcher(_series())
    load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
        refresh=True,
    )
    assert len(fetcher.calls) == 1


def test_locked_cache_ignores_refresh_and_never_fetches(tmp_path: Path) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    cached = _series()
    write_cache(
        path,
        cached,
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    write_cache_lock(path)
    fetcher = _RecordingFetcher(_series(closes={date(2025, 12, 29): Decimal("999")}))
    result = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
        refresh=True,
    )
    assert result is not None
    assert result.closes == cached.closes
    assert fetcher.calls == []


def test_tampered_locked_cache_fails_without_fetching(tmp_path: Path) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    lock_path = write_cache_lock(path)
    original = path.read_text(encoding="utf-8")
    edited = original.replace("128.40", "999")
    assert edited != original
    path.write_text(edited, encoding="utf-8")
    before = (path.read_bytes(), lock_path.read_bytes())
    fetcher = _RecordingFetcher(_series())
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    _assert_refusal(str(exc.value), path, "doesn't match its lock file", locked=True)
    assert fetcher.calls == []
    assert (path.read_bytes(), lock_path.read_bytes()) == before


def test_non_ascii_cache_lock_fails_without_fetching(tmp_path: Path) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    lock_path = write_cache_lock(path)
    lock_path.write_bytes(b"\xef\xbb\xbf" + lock_path.read_bytes())
    before = (path.read_bytes(), lock_path.read_bytes())
    fetcher = _RecordingFetcher(_series())
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    _assert_refusal(str(exc.value), path, "has a lock file that can't be read", locked=True)
    assert fetcher.calls == []
    assert (path.read_bytes(), lock_path.read_bytes()) == before


def _hashless(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(
        "".join(line for line in lines if not line.startswith("# payload_sha256=")),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("covered_start", "after_write", "after_lock", "problem"),
    [
        (date(2025, 12, 1), _hashless, None, "has no payload_sha256 line"),
        (
            date(2025, 12, 1),
            None,
            lambda path: path.with_name(f"{path.name}.lock").write_text("abc\n", encoding="ascii"),
            "has a lock file that doesn't hold a SHA-256 digest",
        ),
        (date(2025, 12, 1), None, Path.unlink, "is missing but has a lock file"),
        (
            date(2025, 12, 25),
            None,
            None,
            "doesn't match this request (span, ticker, fetch symbol, price basis or fetch day)",
        ),
    ],
    ids=["no-hash", "not-a-digest", "cache-missing", "short-span"],
)
def test_bad_locked_cache_refuses_without_fetching(
    tmp_path: Path,
    covered_start: date,
    after_write: Callable[[Path], None] | None,
    after_lock: Callable[[Path], object] | None,
    problem: str,
) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=covered_start,
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    if after_write is not None:
        after_write(path)
    write_cache_lock(path)
    if after_lock is not None:
        after_lock(path)
    before = sorted((entry.name, entry.read_bytes()) for entry in tmp_path.iterdir())
    fetcher = _RecordingFetcher(_series())
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    _assert_refusal(str(exc.value), path, problem, locked=True)
    assert fetcher.calls == []
    assert sorted((entry.name, entry.read_bytes()) for entry in tmp_path.iterdir()) == before


def test_a_locked_cache_moved_aside_clears_only_an_online_run(tmp_path: Path) -> None:
    """#1471: moving a refused locked cache and its lock aside, as the refusal
    says, leaves no cache, so an offline lookup still gets no prices; only an
    online run fetches and writes a fresh one, as the refusal now says."""
    cache_dir = tmp_path / "cache"
    path = _cache_path(cache_dir, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 25),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    lock_path = write_cache_lock(path)
    fetcher = _RecordingFetcher(_series())

    def lookup(*, fetch: bool) -> PriceSeries | None:
        return load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=cache_dir,
            fetcher=fetcher,
            now=CACHE_NOW,
            fetch=fetch,
        )

    with pytest.raises(PriceCacheError) as exc:
        lookup(fetch=True)
    _assert_refusal(
        str(exc.value),
        path,
        "doesn't match this request (span, ticker, fetch symbol, price basis or fetch day)",
        locked=True,
    )
    aside = tmp_path / "aside"
    aside.mkdir()
    for moved in (path, lock_path):
        moved.rename(aside / moved.name)
    assert lookup(fetch=False) is None
    assert fetcher.calls == []
    assert lookup(fetch=True) is fetcher.result
    assert len(fetcher.calls) == 1
    assert read_cache(path) is not None
    assert not lock_path.exists()


# A symbol shaped like an account number: the cache file name is built from it.
_MARKER_TICKER = Ticker("U1234567")
# An errno with no symbolic name, so it prints as "unknown error".
_UNMAPPED_ERRNO = 99999


def _write_marker_cache(cache_dir: Path, *, locked: bool) -> Path:
    """Write a valid cache for ``_MARKER_TICKER``, locked if asked, and return its path."""
    path = _cache_path(cache_dir, _MARKER_TICKER)
    write_cache(
        path,
        _series(ticker=_MARKER_TICKER),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol=_MARKER_TICKER,
    )
    if locked:
        write_cache_lock(path)
    return path


def _assert_no_symbol(message: str) -> None:
    """``message`` has neither the symbol nor the file names built from it (#1047)."""
    for name in (_MARKER_TICKER, f"{_MARKER_TICKER}.csv", f"{_MARKER_TICKER}.csv.lock"):
        assert name.lower() not in message.lower()


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_cache_refusal_names_the_folder_not_the_symbol_built_file(
    tmp_path: Path, locked: bool
) -> None:
    """#1047: a cache refusal names the cache folder, which the CLI or --input
    gives, and "the cache file with one holding's prices". It never names
    ``U1234567.csv`` or ``U1234567.csv.lock``, which are built from the
    statement's Symbol cell."""
    path = _write_marker_cache(tmp_path, locked=locked)
    text = path.read_text(encoding="utf-8")
    assert "128.40" in text
    path.write_text(text.replace("128.40", "999"), encoding="utf-8")
    fetcher = _RecordingFetcher(_series(ticker=_MARKER_TICKER))
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            _MARKER_TICKER,
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    message = str(exc.value)
    if locked:
        assert message == (
            f"Price cache folder {tmp_path}: the cache file with one holding's prices "
            "doesn't match its lock file. Restore or rebuild a reviewed cache with a valid "
            "payload hash, then re-lock it; or move both it and its .csv.lock file aside so "
            "the next online run fetches a fresh one."
        )
    else:
        assert message == (
            f"Price cache folder {tmp_path}: the cache file with one holding's prices "
            "doesn't match its payload_sha256 line. Move it aside so the next online run "
            "fetches a fresh one, or restore the original."
        )
    _assert_no_symbol(message)
    assert fetcher.calls == []


@pytest.mark.parametrize(
    ("locked", "target", "method", "error", "problem"),
    [
        (
            False,
            "cache",
            "lstat",
            OSError(errno.ELOOP, "Synthetic Holder Two", "U1234567.csv"),
            "can't be read (ELOOP)",
        ),
        (
            False,
            "cache",
            "read_text",
            PermissionError(errno.EACCES, "Synthetic Holder Two", "U1234567.csv"),
            "can't be read (EACCES)",
        ),
        (
            False,
            "cache",
            "read_text",
            OSError("Synthetic Holder Two"),
            "can't be read (unknown error)",
        ),
        (
            False,
            "cache",
            "read_text",
            OSError(_UNMAPPED_ERRNO, "Synthetic Holder Two"),
            "can't be read (unknown error)",
        ),
        (
            True,
            "lock",
            "lstat",
            PermissionError(errno.EACCES, "Synthetic Holder Two", "U1234567.csv.lock"),
            "has a lock file that can't be read (EACCES)",
        ),
        (
            True,
            "lock",
            "read_text",
            PermissionError(errno.EACCES, "Synthetic Holder Two", "U1234567.csv.lock"),
            "has a lock file that can't be read (EACCES)",
        ),
        (
            True,
            "cache",
            "read_bytes",
            OSError(errno.EIO, "Synthetic Holder Two", "U1234567.csv"),
            "can't be read (EIO)",
        ),
    ],
    ids=[
        "cache-lstat-eloop",
        "cache-read-eacces",
        "cache-read-no-errno",
        "cache-read-unmapped-errno",
        "lock-lstat-eacces",
        "lock-read-eacces",
        "locked-cache-read-eio",
    ],
)
def test_cache_os_error_prints_its_errno_name_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked: bool,
    target: str,
    method: str,
    error: OSError,
    problem: str,
) -> None:
    """#1047: an OSError reading the cache or its lock adds the errno's
    symbolic name, or "unknown error" when there is none, to the fixed
    phrase. Its text, file name and the symbol-built file names stay out."""
    assert _UNMAPPED_ERRNO not in errno.errorcode
    path = _write_marker_cache(tmp_path, locked=locked)
    failing = path if target == "cache" else path.with_name(f"{path.name}.lock")
    original = getattr(Path, method)

    def _fail(self: Path, *args: object, **kwargs: object) -> object:
        if self == failing:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, _fail)
    fetcher = _RecordingFetcher(_series(ticker=_MARKER_TICKER))
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            _MARKER_TICKER,
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    message = str(exc.value)
    _assert_refusal(message, path, problem, locked=locked)
    _assert_no_symbol(message)
    assert "synthetic holder two" not in message.lower()
    assert "errno" not in message.lower()
    # #1225: nor is any of it in the refusal's chain, suppressed or not.
    assert_replaced_unchained(exc.value, markers=("Synthetic Holder Two", _MARKER_TICKER))
    assert fetcher.calls == []


def test_load_or_fetch_prior_day_cache_refetches(tmp_path: Path) -> None:
    # A cache fetched on a prior day must be re-fetched (so an
    # intervening split is observed) even though it covers the window.
    stale = PriceSeries(
        ticker=Ticker("XQAA"),
        currency="USD",
        closes={date(2025, 12, 29): Decimal("128.40")},
        has_split_in_range=False,
        source="https://example/chart",
        fetched_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),  # month before CACHE_NOW
    )
    write_cache(
        _cache_path(tmp_path, Ticker("XQAA")),
        stale,
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _RecordingFetcher(_series())
    load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert len(fetcher.calls) == 1  # not served from the prior-day cache


def test_normalized_prior_day_cache_is_reusable(tmp_path: Path) -> None:
    normalized = PriceSeries(
        ticker=Ticker("XQAA"),
        currency="USD",
        closes={date(2025, 12, 29): Decimal("128.40")},
        has_split_in_range=True,
        source="https://example/chart",
        fetched_at=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        split_ratios={date(2026, 2, 1): Decimal("2")},
        price_basis=CONTEMPORANEOUS_BASIS,
    )
    write_cache(
        _cache_path(tmp_path, Ticker("XQAA")),
        normalized,
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fetcher = _RecordingFetcher(_series())
    out = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert out == normalized
    assert fetcher.calls == []


def test_load_or_fetch_fetch_failure_returns_none(tmp_path: Path) -> None:
    fetcher = _RecordingFetcher(None)
    out = load_or_fetch(
        Ticker("ZZZ"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
    )
    assert out is None


@pytest.mark.parametrize("cached", [False, True], ids=["no-cache", "partial-cache"])
def test_load_or_fetch_writes_no_cache_for_two_bars_on_one_date(
    tmp_path: Path, cached: bool
) -> None:
    """#942: the fetcher runs the Yahoo parser on a response with two bars on
    2 June 2025. The refusal leaves before the cache write: no cache is
    written, and one that doesn't cover the request is left as it was."""
    path = _cache_path(tmp_path, Ticker("X"))
    if cached:
        write_cache(
            path,
            _series(ticker="X"),
            covered_start=date(2025, 12, 25),
            covered_end=date(2025, 12, 31),
            fetch_symbol="X",
        )
    before = path.read_bytes() if cached else None
    with pytest.raises(PriceSourceContradictionError):
        load_or_fetch(
            Ticker("X"),
            start=date(2025, 6, 1),
            end=date(2025, 6, 30),
            cache_dir=tmp_path,
            fetcher=repeated_date_fetcher,
            now=CACHE_NOW,
        )
    assert list(tmp_path.iterdir()) == ([path] if cached else [])
    if cached:
        assert path.read_bytes() == before


# ---- #1307: a split-adjusted Yahoo series reads back from its cache ---------

_SPLIT_BAR = date(2025, 3, 3)


class _CountingFetcher:
    """Counts the calls to the fetcher it wraps."""

    def __init__(self, fetcher: DailyCloseFetcher) -> None:
        self.fetcher = fetcher
        self.calls = 0

    def __call__(
        self,
        ticker: Ticker,
        *,
        start: date,
        end: date,
        now: datetime,
        fetch_symbol: str | None = None,
    ) -> PriceSeries | None:
        self.calls += 1
        return self.fetcher(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        pytest.param(1, 3, ("10", "11"), id="1-for-3"),
        pytest.param(2, 3, ("20", "22"), id="2-for-3"),
        pytest.param(1, 10, ("3.00", "3.30"), id="1-for-10"),
    ],
)
def test_split_adjusted_series_reads_back_from_the_cache_it_writes(
    tmp_path: Path, numerator: int, denominator: int, expected: tuple[str, str]
) -> None:
    """#1307, the issue's Example 1: a close of 30.0 and a high of 33.0 with a
    split on 2 June 2025. Main wrote ``9.999999999999999999999999999`` under
    1-for-3 and ``20.00000000000000000000000000`` under 2-for-3, which the
    second call's cache read refuses (#1276). Now the first call fetches and
    writes the exact values, and the second reads them back unchanged without
    fetching. 1-for-10 was already exact and keeps its text."""
    fetcher = _CountingFetcher(split_fetcher(30.0, 33.0, numerator, denominator))
    results = [
        load_or_fetch(
            Ticker("XQAA"),
            start=_EXAMPLE_START,
            end=_EXAMPLE_END,
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
        for _ in range(2)
    ]
    assert fetcher.calls == 1
    for series in results:
        assert series is not None
        assert (str(series.closes[_SPLIT_BAR]), str(series.highs[_SPLIT_BAR])) == expected


@pytest.mark.parametrize(
    ("close", "high", "field"),
    [
        pytest.param(29.99, 29.99, "close", id="close"),
        pytest.param(30.0, 31.99, "high", id="high"),
    ],
)
def test_split_adjusted_value_refusal_writes_no_cache(
    tmp_path: Path, close: float, high: float, field: str
) -> None:
    """#1307: under a 1-for-3 split, 29.99 has no exact decimal within a unit
    price's limits. The fetch refuses, and nothing is written to the cache."""
    with pytest.raises(YahooResponseFormatError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=_EXAMPLE_START,
            end=_EXAMPLE_END,
            cache_dir=tmp_path,
            fetcher=split_fetcher(close, high, 1, 3),
            now=CACHE_NOW,
        )
    assert str(exc.value) == (
        f"Yahoo price response, indicators.quote[0].{field}[0]: the {field} on its own day's "
        "split basis has no exact value that fits: a unit price may have at most 9 digits "
        "before the decimal point and 20 after it"
    )
    assert list(tmp_path.iterdir()) == []


# ---- daily HIGH column, v2 cache format + v1 backward-compat ---------------


def test_v2_cache_roundtrips_highs(tmp_path: Path) -> None:
    series = _series(
        closes={date(2025, 12, 29): Decimal("128.40"), date(2025, 12, 30): Decimal("127.5")},
        highs={date(2025, 12, 29): Decimal("129.10")},  # 12-30 has NO high
    )
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        series,
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    out = read_cache(path)
    assert out is not None
    restored = out[0]
    assert restored.highs == {date(2025, 12, 29): Decimal("129.10")}
    assert restored.closes == series.closes
    # A v2 row with a blank high cell round-trips as "no high for that date".
    assert date(2025, 12, 30) not in restored.highs


def test_v2_cache_keeps_every_populated_high(tmp_path: Path) -> None:
    """#1139 checks a Yahoo high's JSON type in the response parse; the cache
    reads its high cells as CSV text and still keeps each one, including a
    high equal to its close."""
    series = _series(
        closes={date(2025, 12, 29): Decimal("128.40"), date(2025, 12, 30): Decimal("127.5")},
        highs={date(2025, 12, 29): Decimal("129.10"), date(2025, 12, 30): Decimal("127.5")},
    )
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        series,
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    out = read_cache(path)
    assert out is not None
    assert out[0].highs == series.highs


def test_legacy_v1_two_column_cache_reads_without_refetch(tmp_path: Path) -> None:
    """A valid-hash 2-column (v1) cache must load with empty highs, keeping
    close-basis prices. An unguarded row[2] would refuse as unparseable."""
    path = tmp_path / "XQAA.csv"
    path.write_text(
        _hashed(
            "# ticker=XQAA\n"
            "# fetch_symbol=XQAA\n"
            "# currency=USD\n"
            "# has_split_in_range=False\n"
            "# source=https://example/chart\n"
            "# fetched_at=2026-01-01T00:00:00+00:00\n"
            "# covered_start=2025-12-20\n"
            "# covered_end=2025-12-31\n"
            "date,close_usd\n"
            "2025-12-29,128.40\n"
            "2025-12-30,127.5\n"
        ),
        encoding="utf-8",
    )
    out = read_cache(path)
    assert out is not None
    restored = out[0]
    assert restored.closes == {
        date(2025, 12, 29): Decimal("128.40"),
        date(2025, 12, 30): Decimal("127.5"),
    }
    assert restored.highs == {}


def test_hashless_cache_refuses_without_refetching(tmp_path: Path) -> None:
    path = _cache_path(tmp_path, Ticker("XQAA"))
    path.write_text(
        "# ticker=XQAA\n"
        "# fetch_symbol=XQAA\n"
        "# currency=USD\n"
        "# has_split_in_range=False\n"
        "# source=https://example/chart\n"
        "# fetched_at=2026-06-01T12:00:00+00:00\n"
        "# covered_start=2025-12-20\n"
        "# covered_end=2025-12-31\n"
        "date,close_usd\n"
        "2025-12-29,128.40\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    fetcher = _RecordingFetcher(_series(closes={date(2025, 12, 29): Decimal("130")}))
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("XQAA"),
            start=date(2025, 12, 20),
            end=date(2025, 12, 31),
            cache_dir=tmp_path,
            fetcher=fetcher,
            now=CACHE_NOW,
        )
    _assert_refusal(str(exc.value), path, "has no payload_sha256 line")
    assert fetcher.calls == []
    assert path.read_bytes() == before


def test_price_series_rejects_high_without_matching_close() -> None:
    """Invariant: highs.keys() <= closes.keys() — a high for a date with no close
    is un-anchored (can't be validated against its own close), so it's refused."""
    with pytest.raises(InvariantError):
        PriceSeries(
            ticker=Ticker("X"),
            currency="USD",
            closes={date(2025, 12, 30): Decimal("100")},
            highs={date(2025, 12, 31): Decimal("110")},  # no close on 12-31
            has_split_in_range=False,
            source="test",
            fetched_at=CACHE_NOW,
        )


def test_cache_reader_refuses_bad_closes(tmp_path: Path) -> None:
    """A legacy or corrupt cache row with a non-finite or non-positive close
    refuses the cache at the first such row (#1140), so it can't poison a
    close-basis peak or hide a second row on its date."""
    path = tmp_path / "XQAA.csv"
    path.write_text(
        _hashed(
            "# ticker=XQAA\n"
            "# fetch_symbol=XQAA\n"
            "# currency=USD\n"
            "# has_split_in_range=False\n"
            "# source=https://example/chart\n"
            "# fetched_at=2026-01-01T00:00:00+00:00\n"
            "# covered_start=2025-12-20\n"
            "# covered_end=2025-12-31\n"
            "date,close_usd,high_usd\n"
            "2025-12-29,0,0\n"  # non-positive close (line 11) -> refuses
            "2025-12-30,NaN,\n"  # non-finite close
            "2025-12-31,127.5,129.1\n"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path)
    _assert_refusal(
        str(exc.value), path, "has a close that isn't a positive price (line 11, column close_usd)"
    )


def test_cache_hash_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    original = path.read_text(encoding="utf-8")
    edited = original.replace("128.40", "999.00")
    assert edited != original
    path.write_text(edited, encoding="utf-8")
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path)
    _assert_refusal(str(exc.value), path, "doesn't match its payload_sha256 line")


def test_cache_hash_covers_rows_before_hash_header(tmp_path: Path) -> None:
    path = tmp_path / "XQAA.csv"
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 20),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[1:1] = ["date,close_usd,high_usd\n", "2025-12-28,9999.00,\n"]
    path.write_text("".join(lines), encoding="utf-8")
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path)
    _assert_refusal(str(exc.value), path, "doesn't match its payload_sha256 line")


# ---- #1170: cache layout, CSV structure, metadata and numeric text ----------

_HIGH_TEXT = "has high text outside the supported ASCII decimal grammar (line {}, column high_usd)"
_RATIO = (
    "has a split ratio outside the supported positive finite decimal form "
    "(line 7, key split_ratios, entry {}, field ratio)"
)
_RATIO_DATE_REPEATED = (
    "has more than one ratio for a split date (line 7, key split_ratios, entry {}, field date)"
)
_RATIO_DATE_UNREADABLE = (
    "has a split date that cannot be read (line 7, key split_ratios, entry {}, field date)"
)
_RATIOS_NOT_OBJECT = "has split_ratios that is not a JSON object (line 7, key split_ratios)"
_REPEATED_KEY = "has a repeated metadata key (line {}, key {})"
# Close/high text outside the grammar, besides the marker number.
_BAD_NUMBERS = (
    " 6731.4289",
    "6731.4289 ",
    "\u0666\u0667\u0663\u0661",  # Arabic-Indic digits
    "\uff16\uff17\uff13\uff11",  # fullwidth digits
    "NaN",
    "sNaN",
    "Infinity",
    "-Infinity",
    "inf",
    "1e",
    ".",
    "0x10",
)


def _after(line: str, *new: str) -> Callable[[str], str]:
    """Insert ``new`` lines right after ``line``."""
    return _edit(line, line + "".join(new))


_LAYOUT_REFUSALS = [
    *(
        pytest.param(
            _edit(_HEADER, f"{header}\n"),
            f"has an unsupported table header (line 13, {_COLUMNS})",
            id=f"header-{index}",
        )
        for index, header in enumerate(
            (
                "date,high_usd,close_usd",  # the #1170 example: columns swapped
                "date,close_usd,volume_usd",
                "date,close_usd,high_usd,volume_usd",
                "date",
                "date,close_usd,",
                "date, close_usd,high_usd",
            )
        )
    ),
    pytest.param(
        _edit(_HEADER, "Date,Close_USD,High_USD\n"),
        "has cells before the table header (line 13, field CSV record)",
        id="capitalized-header",
    ),
    pytest.param(
        _edit(_HEADER, f"x,y\n{_HEADER}"),
        "has cells before the table header (line 13, field CSV record)",
        id="cells-before-header",
    ),
    pytest.param(
        _edit(_HEADER, f"{_ROW}{_HEADER}"),
        "has cells before the table header (line 13, field CSV record)",
        id="row-before-header",
    ),
    pytest.param(
        _edit(_HEADER, " \n" + _HEADER),
        "has cells before the table header (line 13, field CSV record)",
        id="whitespace-line-before-header",
    ),
    pytest.param(
        _edit(_HEADER, ""),
        "has cells before the table header (line 13, field CSV record)",
        id="rows-without-header",
    ),
    pytest.param(
        _edit(_HEADER + _ROW + "2025-02-14,6740.1,\n", ""),
        "has no supported table header (line 14, field table header)",
        id="no-header",
    ),
    pytest.param(
        _after(_ROW, _HEADER),
        "has more than one table header (line 15, field table header)",
        id="second-header",
    ),
    pytest.param(
        _after(_ROW, "date,close_usd\n"),
        "has more than one table header (line 15, field table header)",
        id="second-header-legacy",
    ),
    pytest.param(
        _after(_ROW, "date,high_usd,close_usd\n"),
        "has more than one table header (line 15, field table header)",
        id="second-header-unsupported",
    ),
    pytest.param(
        _edit(_ROW, f"{_MARKER_DATE},6731.4289\n"),
        f"has a row whose width differs from the table header (line 14, {_COLUMNS})",
        id="missing-cell",
    ),
    pytest.param(
        _edit(_ROW, f"{_MARKER_DATE},6731.4289,6800.5,\n"),
        f"has a row whose width differs from the table header (line 14, {_COLUMNS})",
        id="extra-cell",
    ),
    pytest.param(
        _edit(_HEADER, "date,close_usd\n"),
        f"has a row whose width differs from the table header (line 14, {_COLUMNS})",
        id="legacy-header-wide-rows",
    ),
    pytest.param(
        _after(_ROW, "   \n"),
        f"has a row whose width differs from the table header (line 15, {_COLUMNS})",
        id="whitespace-line-in-table",
    ),
]

_CSV_REFUSALS = [
    pytest.param(
        _edit(_ROW, f'{_MARKER_DATE},"6731"4289,6800.5\n'),
        _MALFORMED_RECORD.format(14),
        id="text-after-quote",
    ),
    pytest.param(
        _edit(_ROW, f'{_MARKER_DATE},"6731\n4289",6800.5\n'),
        _MALFORMED_RECORD.format(14),
        id="multiline-record",
    ),
    pytest.param(
        _edit(_ROW, f'{_MARKER_DATE},"6731.4289,6800.5\n'),
        _MALFORMED_RECORD.format(14),
        id="unclosed-quote",
    ),
    pytest.param(
        _edit(_ROW, f"{_MARKER_DATE},{'1' * (csv.field_size_limit() + 1)},6800.5\n"),
        _MALFORMED_RECORD.format(14),
        id="over-limit-cell",
    ),
    pytest.param(
        _edit(_HEADER, f'"date,close_usd\n{_HEADER}'),
        _MALFORMED_RECORD.format(13),
        id="malformed-before-header",
    ),
    # The starting line counts the blank, comment and metadata lines above it.
    pytest.param(
        _edit(
            _HEADER + _ROW,
            f"\n# {_MARKER_DESCRIPTION}\n# {_MARKER_KEY}=1\n{_HEADER}\n\n"
            f'{_MARKER_DATE},"6731"4289,6800.5\n',
        ),
        _MALFORMED_RECORD.format(19),
        id="line-after-comments-and-blanks",
    ),
]

_NUMBER_REFUSALS = [
    *(
        pytest.param(
            _edit(_ROW, f"{_MARKER_DATE},{text},6800.5\n"),
            _CLOSE_TEXT.format(14),
            id=f"close-{index}",
        )
        for index, text in enumerate((_MARKER_NUMBER, "", *_BAD_NUMBERS))
    ),
    *(
        pytest.param(
            _edit(_ROW, f"{_MARKER_DATE},6731.4289,{text}\n"),
            _HIGH_TEXT.format(14),
            id=f"high-{index}",
        )
        for index, text in enumerate((_MARKER_NUMBER, *_BAD_NUMBERS))
    ),
    pytest.param(
        _edit("2025-02-14,6740.1,\n", "2025-02-14,6_740.1,\n"),
        _CLOSE_TEXT.format(15),
        id="close-second-row",
    ),
]

_KEY_LINES = {
    "ticker": (3, f"# ticker={_MARKER_SYMBOL}\n"),
    "fetch_symbol": (4, f"# fetch_symbol={_MARKER_SYMBOL}\n"),
    "currency": (5, "# currency=USD\n"),
    "split_ratios": (7, "# split_ratios={}\n"),
    "price_basis": (8, "# price_basis=contemporaneous\n"),
    "source": (9, f"# source={_MARKER_SOURCE}\n"),
    "fetched_at": (10, "# fetched_at=2026-06-01T12:00:00+00:00\n"),
    "covered_start": (11, "# covered_start=2025-02-01\n"),
    "covered_end": (12, "# covered_end=2025-02-28\n"),
}

_METADATA_REFUSALS = [
    # Each known key repeated right after its line, with the same value.
    *(
        pytest.param(_after(line, line), _REPEATED_KEY.format(number + 1, key), id=f"repeat-{key}")
        for key, (number, line) in _KEY_LINES.items()
    ),
    # A later line, even with the writer's value, can't replace an earlier one.
    pytest.param(
        _edit("# currency=USD\n", "# currency=XQZ\n# currency=USD\n"),
        _REPEATED_KEY.format(6, "currency"),
        id="conflicting-currency-then-usd",
    ),
    pytest.param(
        _edit("# covered_start=2025-02-01\n", "# covered_start=x\n# covered_start=2025-02-01\n"),
        _REPEATED_KEY.format(12, "covered_start"),
        id="invalid-then-valid-value",
    ),
    pytest.param(
        _after(f"# ticker={_MARKER_SYMBOL}\n", f"#  ticker = {_MARKER_SYMBOL}\n"),
        _REPEATED_KEY.format(4, "ticker"),
        id="repeat-with-spaces",
    ),
    pytest.param(
        _after(f"# source={_MARKER_SOURCE}\n", "# source=synthetic-cache-source-2\n"),
        _REPEATED_KEY.format(10, "source"),
        id="repeat-other-value",
    ),
    pytest.param(
        _after("# covered_end=2025-02-28\n", "#  payload_sha256 = 0\n"),
        _REPEATED_KEY.format(13, "payload_sha256"),
        id="repeat-hash-key-spaced",
    ),
    pytest.param(
        _after("# covered_end=2025-02-28\n", f"# {_MARKER_KEY}=1\n", f"# {_MARKER_KEY}=1\n"),
        "has a repeated metadata key (line 14, field metadata key)",
        id="repeat-unknown-key",
    ),
    # Metadata lines are all checked before the table: the repeat at the end
    # refuses before the bad close on line 14 (#1140's order).
    pytest.param(
        lambda body: (
            _edit(_ROW, f"{_MARKER_DATE},{_MARKER_NUMBER},6800.5\n")(body) + "# currency=USD\n"
        ),
        _REPEATED_KEY.format(17, "currency"),
        id="repeat-after-bad-close",
    ),
]

_RATIO_REFUSALS = [
    *(
        pytest.param(_ratios(value), _RATIOS_NOT_OBJECT, id=f"not-object-{index}")
        for index, value in enumerate(("{", "[]", '"2"', "null", "2", "", '{"2026-01-05":"2"} x'))
    ),
    *(
        pytest.param(_ratios(f'{{"2026-01-05":{value}}}'), _RATIO.format(1), id=f"ratio-{index}")
        for index, value in enumerate(
            (
                '"1_2"',
                '" 2"',
                '"2 "',
                '"\u0662"',  # an Arabic-Indic digit
                '""',
                '"NaN"',
                '"Infinity"',
                '"0"',
                '"-2"',
                '"0E+3"',
                "true",
                "null",
                '["2"]',
                '{"x":"2"}',
                # JSON numbers refuse too: the owner kept the writer's strings only.
                "2",
                "2.5",
                "NaN",
                "1e400",
                "1" * 10_000,  # Number hooks must not hit Python's integer digit limit.
            )
        )
    ),
    pytest.param(
        _ratios('{"2026-01-05":"2","2026-02-05":"1_2"}'), _RATIO.format(2), id="ratio-second"
    ),
    pytest.param(
        _ratios(f'{{"{_MARKER_DATE}":"2","{_MARKER_DATE}":"2"}}'),
        _RATIO_DATE_REPEATED.format(2),
        id="repeated-date-equal",
    ),
    pytest.param(
        _ratios(f'{{"{_MARKER_DATE}":"2","{_MARKER_DATE}":"3"}}'),
        _RATIO_DATE_REPEATED.format(2),
        id="repeated-date-unequal",
    ),
    pytest.param(
        _ratios(f'{{"2026-01-05":"2","{_MARKER_DATE}":"2","20250213":"2"}}'),
        _RATIO_DATE_REPEATED.format(3),
        id="two-spellings-one-date",
    ),
    pytest.param(
        _ratios(f'{{"{_MARKER_DESCRIPTION}":"2"}}'),
        _RATIO_DATE_UNREADABLE.format(1),
        id="unreadable-date",
    ),
    pytest.param(
        _ratios('{"2026-01-05":"2","2026-02-30":"2"}'),
        _RATIO_DATE_UNREADABLE.format(2),
        id="impossible-date",
    ),
    # The date checks come before the ratio check in each entry.
    pytest.param(
        _ratios(f'{{"{_MARKER_DATE}":"2","{_MARKER_DATE}":"1_2"}}'),
        _RATIO_DATE_REPEATED.format(2),
        id="repeated-date-bad-ratio",
    ),
    # The table is read before split_ratios.
    pytest.param(
        lambda body: _ratios("[]")(_edit(_ROW, f"{_MARKER_DATE},{_MARKER_NUMBER},6800.5\n")(body)),
        _CLOSE_TEXT.format(14),
        id="bad-close-before-ratios",
    ),
]


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize(
    ("edit", "problem"),
    [*_LAYOUT_REFUSALS, *_CSV_REFUSALS, *_NUMBER_REFUSALS, *_METADATA_REFUSALS, *_RATIO_REFUSALS],
)
def test_cache_text_outside_the_format_refuses(
    tmp_path: Path, edit: Callable[[str], str], problem: str, locked: bool
) -> None:
    """#1170: a correctly hashed cache outside the supported layout, CSV
    structure, metadata or number text refuses with its fixed, located message,
    locked or not, and through every ``load_or_fetch`` mode without fetching or
    changing a file."""
    body = edit(_MARKER_BODY)
    assert body != _MARKER_BODY
    _assert_cache_refuses(tmp_path, _hashed(body), problem, locked=locked)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize("trap_invalid", [True, False], ids=["trapped", "untrapped"])
@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(
            _edit(_ROW, f"{_MARKER_DATE},1e999999999999999999999999999,6800.5\n"),
            id="close",
        ),
        pytest.param(
            _edit(_ROW, f"{_MARKER_DATE},6731.4289,1e999999999999999999999999999\n"),
            id="high",
        ),
        pytest.param(_ratios('{"2026-01-05":"1e999999999999999999999999999"}'), id="ratio"),
    ],
)
def test_unrepresentable_decimal_refuses_without_a_parser_chain(
    tmp_path: Path, edit: Callable[[str], str], trap_invalid: bool, locked: bool
) -> None:
    """An unrepresentable exponent refuses even when Decimal returns NaN."""
    with localcontext() as context:
        context.traps[InvalidOperation] = trap_invalid
        _assert_cache_refuses(tmp_path, _hashed(edit(_MARKER_BODY)), "doesn't parse", locked=locked)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize("value", ["0", "f" * 64], ids=["other-value", "digest-shaped"])
def test_second_payload_hash_line_refuses(tmp_path: Path, locked: bool, value: str) -> None:
    """#1170: the first payload_sha256 line is the hash, and it covers a second
    one, which refuses as a repeated key."""
    body = f"# payload_sha256={value}\n{_MARKER_BODY}"
    text = f"# payload_sha256={hashlib.sha256(body.encode()).hexdigest()}\n{body}"
    _assert_cache_refuses(tmp_path, text, _REPEATED_KEY.format(2, "payload_sha256"), locked=locked)


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_split_flag_repeat_keeps_its_1140_message(tmp_path: Path, locked: bool) -> None:
    """#1170 keeps #1140's split-flag phrase for a repeated has_split_in_range."""
    edit = _after("# has_split_in_range=False\n", "# has_split_in_range=False\n")
    _assert_cache_refuses(
        tmp_path, _hashed(edit(_MARKER_BODY)), _SPLIT_FLAG_REPEATED.format(7), locked=locked
    )


def test_marker_cache_reads_its_values(tmp_path: Path) -> None:
    series, start, end, fetch_symbol = _read_marker_cache(tmp_path, _MARKER_BODY)
    assert series.closes == {
        date(2025, 2, 13): Decimal("6731.4289"),
        date(2025, 2, 14): Decimal("6740.1"),
    }
    assert series.highs == {date(2025, 2, 13): Decimal("6800.5")}
    assert (series.source, series.price_basis, series.split_ratios) == (
        _MARKER_SOURCE,
        CONTEMPORANEOUS_BASIS,
        {},
    )
    assert (start, end, fetch_symbol) == (date(2025, 2, 1), date(2025, 2, 28), _MARKER_SYMBOL)


@pytest.mark.parametrize(
    ("close", "high"),
    [
        ("+6731.4289", "6800.5"),
        ("6731.", "6800.50"),
        (".5", ""),
        ("6.7314289E+3", "6.8005e3"),
        ("67314289e-4", "+6800.5"),
        ("1E+1", "1E+1"),
    ],
)
def test_decimal_forms_in_the_grammar_read(tmp_path: Path, close: str, high: str) -> None:
    """#1170 keeps signs, fractions and exponents, including what ``str(Decimal)``
    writes; a blank high stays absent."""
    body = _edit(_ROW, f"{_MARKER_DATE},{close},{high}\n")(_MARKER_BODY)
    series = _read_marker_cache(tmp_path, body)[0]
    assert series.closes[date(2025, 2, 13)] == Decimal(close)
    assert series.highs.get(date(2025, 2, 13)) == (Decimal(high) if high else None)


def test_comments_blank_lines_and_one_unknown_key_are_allowed(tmp_path: Path) -> None:
    """#1170: comments, empty lines and a single unknown key change nothing."""
    body = _edit(_HEADER, f"\n# {_MARKER_DESCRIPTION}\n# {_MARKER_KEY}=1\n\n{_HEADER}")(
        _MARKER_BODY
    )
    body = _after(_ROW, "\n", f"# {_MARKER_DESCRIPTION}\n")(body) + "\n"
    assert _read_marker_cache(tmp_path, body) == _read_marker_cache(tmp_path, _MARKER_BODY)


def test_quoted_cells_read_as_their_text(tmp_path: Path) -> None:
    """#1170 parses each record as strict CSV, so well-formed quoting reads as
    the cell's text; it's never repaired."""
    body = _edit(
        _HEADER + _ROW, f'"date","close_usd","high_usd"\n"{_MARKER_DATE}","6731.4289",""\n'
    )(_MARKER_BODY)
    series = _read_marker_cache(tmp_path, body)[0]
    assert series.closes[date(2025, 2, 13)] == Decimal("6731.4289")
    assert date(2025, 2, 13) not in series.highs


def test_missing_optional_metadata_keeps_its_defaults(tmp_path: Path) -> None:
    """#1170 keeps the defaults for a cache with no fetch_symbol, source,
    split_ratios or price_basis line."""
    body = _MARKER_BODY
    for line in (
        f"# fetch_symbol={_MARKER_SYMBOL}\n",
        "# split_ratios={}\n",
        "# price_basis=contemporaneous\n",
        f"# source={_MARKER_SOURCE}\n",
    ):
        body = _edit(line, "")(body)
    path = _cache_path(tmp_path, Ticker(_MARKER_SYMBOL))
    series, _start, _end, fetch_symbol = _read_marker_cache(tmp_path, body)
    assert fetch_symbol == _MARKER_SYMBOL
    assert series.source == str(path)
    assert series.split_ratios == {}
    assert series.price_basis == "current-split-basis"


def test_legacy_layout_reads_with_blank_and_absent_highs(tmp_path: Path) -> None:
    body = _edit(_HEADER + _ROW, f"date,close_usd\n{_MARKER_DATE},6731.4289\n")(
        _edit("2025-02-14,6740.1,\n", "2025-02-14,6740.1\n")(_MARKER_BODY)
    )
    series = _read_marker_cache(tmp_path, body)[0]
    assert series.closes == {
        date(2025, 2, 13): Decimal("6731.4289"),
        date(2025, 2, 14): Decimal("6740.1"),
    }
    assert series.highs == {}


def test_writer_split_ratios_round_trip_unchanged(tmp_path: Path) -> None:
    """#1170: the writer's ratio strings, for several split dates, read back to
    the same ratios, and rewriting them gives the same bytes."""
    series = _series(
        has_split=True,
        split_ratios={
            date(2025, 1, 2): Decimal("2"),
            date(2025, 6, 3): Decimal("0.5"),
            date(2025, 9, 4): Decimal("1E+1"),
            date(2025, 11, 5): Decimal("1.5"),
        },
        price_basis=CONTEMPORANEOUS_BASIS,
        highs={date(2025, 12, 29): Decimal("1.3E+2")},
    )
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    span = {"covered_start": date(2025, 12, 20), "covered_end": date(2025, 12, 31)}
    write_cache(first, series, fetch_symbol="XQAA", **span)
    assert '"2025-09-04":"1E+1"' in first.read_text(encoding="utf-8")
    restored = read_cache(first)
    assert restored is not None
    assert restored[0] == series
    write_cache(second, restored[0], fetch_symbol="XQAA", **span)
    assert second.read_bytes() == first.read_bytes()


@pytest.mark.parametrize("fetch", [True, False], ids=["online", "offline"])
def test_refresh_checks_an_existing_cache_first(tmp_path: Path, fetch: bool) -> None:
    """#1170: refresh used to skip the reader and overwrite whatever was there.
    The #940 hashless example now refuses under refresh too, unchanged."""
    path = write_example_cache(tmp_path)
    before = _entries(tmp_path)
    with pytest.raises(PriceCacheError) as exc:
        load_or_fetch(
            Ticker("X"),
            start=_EXAMPLE_START,
            end=_EXAMPLE_END,
            cache_dir=tmp_path,
            fetcher=_fail_fetch,
            now=CACHE_NOW,
            refresh=True,
            fetch=fetch,
        )
    _assert_refusal(str(exc.value), path, "has no payload_sha256 line")
    assert _entries(tmp_path) == before


def test_refresh_replaces_a_valid_unlocked_cache(tmp_path: Path) -> None:
    """#1170: a valid unlocked cache still refreshes: it's fetched and replaced."""
    path = _cache_path(tmp_path, Ticker("XQAA"))
    write_cache(
        path,
        _series(),
        covered_start=date(2025, 12, 1),
        covered_end=date(2025, 12, 31),
        fetch_symbol="XQAA",
    )
    fresh = _series(closes={date(2025, 12, 29): Decimal("130")})
    fetcher = _RecordingFetcher(fresh)
    out = load_or_fetch(
        Ticker("XQAA"),
        start=date(2025, 12, 20),
        end=date(2025, 12, 31),
        cache_dir=tmp_path,
        fetcher=fetcher,
        now=CACHE_NOW,
        refresh=True,
    )
    assert out == fresh
    assert len(fetcher.calls) == 1
    restored = read_cache(path)
    assert restored is not None
    assert restored[0].closes == fresh.closes


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_unsupported_layout_cache_refuses_with_its_message(tmp_path: Path, locked: bool) -> None:
    """The shared pipeline-test cache refuses with exactly its message."""
    path = write_unsupported_layout_cache(tmp_path, "XQAA")
    if locked:
        write_cache_lock(path)
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path, locked=locked)
    assert str(exc.value) == unsupported_layout_message(tmp_path, locked=locked)


# ---- folded into #1170: a cached high below its close refuses ------------------

_HIGH_LINE = "has a high below its close (line {}, column high_usd)"


def _high_row(close: str, high: str) -> Callable[[str], str]:
    return _edit(_HIGH_ROW, f"2025-12-31,{close},{high}\n")


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        pytest.param(lambda body: body, _HIGH_LINE.format(14), id="marker-row"),
        pytest.param(_high_row("10", "9"), _HIGH_LINE.format(14), id="ten-nine"),
        pytest.param(_high_row("10", "0"), _HIGH_LINE.format(14), id="zero-high"),
        pytest.param(_high_row("10", "-1"), _HIGH_LINE.format(14), id="negative-high"),
        pytest.param(_high_row("10", "9.99999999E+0"), _HIGH_LINE.format(14), id="exponent"),
        # The line counts the blank, comment and metadata lines above the row.
        pytest.param(
            _edit(
                _HEADER,
                f"\n# {_HIGH_MARKERS[1]}\n# {_MARKER_KEY}=1\n\n{_HEADER}",
            ),
            _HIGH_LINE.format(18),
            id="after-comments-and-blanks",
        ),
        # Earlier checks keep their priority: text, then the positive close.
        pytest.param(_high_row("211.3579", "2_10.2468"), _HIGH_TEXT.format(14), id="bad-text"),
        pytest.param(
            _high_row("0", "-1"),
            "has a close that isn't a positive price (line 14, column close_usd)",
            id="bad-close",
        ),
        pytest.param(
            _after(_HIGH_ROW, "2025-12-31,211.3579,212\n"),
            _HIGH_LINE.format(14),
            id="before-repeated-date",
        ),
    ],
)
def test_cached_high_below_its_close_refuses(
    tmp_path: Path, edit: Callable[[str], str], problem: str, locked: bool
) -> None:
    """A present high below its row's close contradicts it. It used to be
    dropped silently, so the high basis lost the day while the close basis used
    the close; now the cache refuses, never fetching or changing a file."""
    _assert_cache_refuses(
        tmp_path, _hashed(edit(_HIGH_BODY)), problem, locked=locked, markers=_HIGH_MARKERS
    )


@pytest.mark.parametrize(
    ("high", "kept"),
    [("211.3579", True), ("211.358", True), ("2.113579E+2", True), ("", False)],
    ids=["equal", "above", "equal-exponent", "blank"],
)
def test_high_at_or_above_its_close_or_blank_reads(tmp_path: Path, high: str, kept: bool) -> None:
    """An equal or higher high is kept as written; a blank one stays absent."""
    series = _read_marker_cache(tmp_path, _high_row("211.3579", high)(_HIGH_BODY))[0]
    assert series.closes == {date(2025, 12, 31): Decimal("211.3579")}
    assert series.highs == ({date(2025, 12, 31): Decimal(high)} if kept else {})


@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_high_below_close_cache_refuses_with_its_message(tmp_path: Path, locked: bool) -> None:
    """The shared pipeline-test cache refuses with exactly its message."""
    path = write_high_below_close_cache(tmp_path, "XQAA")
    if locked:
        write_cache_lock(path)
    with pytest.raises(PriceCacheError) as exc:
        read_cache(path, locked=locked)
    assert str(exc.value) == high_below_close_message(tmp_path, locked=locked)
