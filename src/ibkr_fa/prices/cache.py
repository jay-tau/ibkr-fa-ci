"""On-disk price cache that doubles as the FA peak-value audit artifact.

For a tax tool, a network price source is only acceptable if a filed
return is reproducible and the exact prices used can be shown. This
module persists every fetched :class:`PriceSeries` as a per-ticker CSV
with a provenance header (source URL, fetch timestamp, currency,
split ratios/basis, covered span) and SHA-256 integrity. Re-runs read
the cache instead of refetching (deterministic output), and the CSV is
a human-readable record of the closes that drove each peak.

The cache is keyed by ticker and records the ``[covered_start,
covered_end]`` span it holds; :func:`load_or_fetch` reuses it only when
it fully covers the requested window, otherwise it refetches and
overwrites.

Every read checks the cache's ``payload_sha256`` line. A cache file that
exists but has no valid hash, doesn't parse or fails a check raises
:class:`PriceCacheError` (#940): it is never used, refetched or
overwritten, even when a refresh was asked for. Only an absent cache is a
miss.

The file format (#1170). Lines are physical lines as ``str.splitlines``
splits the text, numbered from 1, counting comments, metadata and blank lines.

- A line starting with ``# `` is a comment. One with an ``=`` is a metadata
  line: the stripped text before its first ``=`` is the key, the rest the
  value. A key appears once; a second line with the same key refuses.
- An empty line is skipped.
- Every other line is one strict CSV record. A record can't span lines:
  quoting left open at the end of a line refuses, and nothing is repaired.
- The first record is the table header, exactly ``date,close_usd`` (legacy)
  or ``date,close_usd,high_usd``. Every later record is a data row with the
  header's width. A blank high cell, or the legacy header, means no high is
  available for that date; a present high below its row's close contradicts
  the close and refuses.
- Close and high cells (and each split ratio) match the ASCII decimal grammar
  ``[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?``, which covers
  the text :func:`write_cache` writes. Underscores, padding, other digits and
  non-finite words refuse.
- ``split_ratios`` is a JSON object of ISO date keys and ratio strings, as
  :func:`write_cache` writes it. Two keys for one date, a key that isn't a
  date and a ratio that isn't a positive decimal string refuse.

An adjacent ``.csv.lock`` file contains the SHA-256 of an explicitly reviewed
manual cache. A valid lock overrides refresh. A lock that doesn't verify, or a
locked cache that fails a check or doesn't match the request, raises
:class:`PriceCacheError`; it is never replaced from the network.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from ibkr_fa.exceptions import IbkrFaError, InvariantError, errno_name, raise_replacing
from ibkr_fa.number_checks import NumberKind, number_limit_breach
from ibkr_fa.prices.series import (
    CONTEMPORANEOUS_BASIS,
    CURRENT_SPLIT_BASIS,
    PriceSeries,
)
from ibkr_fa.prices.yahoo import DailyCloseFetcher
from ibkr_fa.types import Ticker

# The default price-cache folder's name. Each caller puts it under its own
# parent folder when no cache folder is given.
DEFAULT_PRICE_CACHE_DIR_NAME: Final[str] = ".ibkr-fa-price-cache"

_HEADER_PREFIX = "# "
_SAFE_TICKER = re.compile(r"[^A-Za-z0-9._-]")
# The two table headers (#1170): legacy v1 rows have no high column.
_TABLE_HEADERS = (("date", "close_usd"), ("date", "close_usd", "high_usd"))
_HIGH_COL_INDEX = 2
_HASH_KEY = "payload_sha256"
_SPLIT_FLAG_KEY = "has_split_in_range"
_SPLIT_RATIOS_KEY = "split_ratios"
# Every metadata key the reader knows. A refusal names a key only through this
# map's own value, never the parsed text (#1170).
_METADATA_KEYS = {
    key: key
    for key in (
        _HASH_KEY,
        _SPLIT_RATIOS_KEY,
        "covered_start",
        "covered_end",
        "ticker",
        "currency",
        _SPLIT_FLAG_KEY,
        "source",
        "fetched_at",
        "price_basis",
        "fetch_symbol",
    )
}
# Close, high and split-ratio text (#1170): finite ASCII decimal and exponent
# forms only, which covers ``str(Decimal)`` for every finite value.
_DECIMAL_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_COLUMNS = "columns date, close_usd and high_usd"
_JSON_NUMBER = object()


class PriceCacheError(IbkrFaError):
    """A price cache file or its lock exists but can't be used (#940)."""


def _cache_path(cache_dir: Path, ticker: Ticker) -> Path:
    return cache_dir / f"{_SAFE_TICKER.sub('_', str(ticker))}.csv"


def _cache_lock_path(path: Path) -> Path:
    return path.with_suffix(f"{path.suffix}.lock")


def _cache_error(path: Path, problem: str, *, locked: bool) -> PriceCacheError:
    """Refuse ``path`` with a fixed ``problem`` phrase, never cache content.

    The message names the cache folder, not ``path``: the file name is built
    from the holding's symbol, which is input (#1047). The read-handler
    refusals covered by #1225 keep nothing of the error they replace, which
    can hold that path or a decoder's bytes: they are raised through
    :func:`~ibkr_fa.exceptions.raise_replacing`, so each has no context,
    suppressed or not, and as cause at most that function's content-free
    stand-in (#1225).
    """
    if locked:
        advice = (
            "Restore or rebuild a reviewed cache with a valid payload hash, then re-lock it; "
            "or move both it and its .csv.lock file aside so the next online run fetches a "
            "fresh one."
        )
    else:
        advice = (
            "Move it aside so the next online run fetches a fresh one, or restore the original."
        )
    return PriceCacheError(
        f"Price cache folder {path.parent}: the cache file with one holding's prices "
        f"{problem}. {advice}"
    )


def write_cache_lock(path: Path) -> Path:
    """Lock one verified cache file to its exact bytes."""

    lock_path = _cache_lock_path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lock_path.write_text(f"{digest}\n", encoding="ascii")
    return lock_path


def _cache_locked(path: Path) -> bool:
    """Return whether a ``.csv.lock`` pins ``path``.

    Raises:
        PriceCacheError: If a lock exists but can't be read, doesn't hold a
            SHA-256 digest, or doesn't match the cache file's bytes, or its
            cache file is missing.
    """
    lock_path = _cache_lock_path(path)
    try:
        lock_path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise_replacing(
            _cache_error(
                path, f"has a lock file that can't be read ({errno_name(exc)})", locked=True
            ),
            exc,
        )
    try:
        expected = lock_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise_replacing(
            _cache_error(
                path, f"has a lock file that can't be read ({errno_name(exc)})", locked=True
            ),
            exc,
        )
    except (UnicodeError, MemoryError) as exc:
        raise_replacing(_cache_error(path, "has a lock file that can't be read", locked=True), exc)
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise _cache_error(path, "has a lock file that doesn't hold a SHA-256 digest", locked=True)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise_replacing(_cache_error(path, "is missing but has a lock file", locked=True), exc)
    except OSError as exc:
        raise_replacing(_cache_error(path, f"can't be read ({errno_name(exc)})", locked=True), exc)
    except MemoryError as exc:
        raise_replacing(_cache_error(path, "can't be read", locked=True), exc)
    if hashlib.sha256(data).hexdigest() != expected:
        raise _cache_error(path, "doesn't match its lock file", locked=True)
    return True


def write_cache(
    path: Path,
    series: PriceSeries,
    *,
    covered_start: date,
    covered_end: date,
    fetch_symbol: str,
) -> None:
    """Write ``series`` to ``path`` as a provenance-headed CSV.

    ``fetch_symbol`` is the price-source symbol actually queried (equal
    to the IBKR ticker unless a ``[prices].ticker_overrides`` entry
    redirected it). It is recorded so the audit artifact shows the
    holding -> fetched-symbol mapping, and so :func:`load_or_fetch` can
    refuse to reuse a cache produced under a different (e.g. since
    added / changed / removed) override.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    split_ratios = json.dumps(
        {day.isoformat(): str(ratio) for day, ratio in sorted(series.split_ratios.items())},
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_lines = [
        f"{_HEADER_PREFIX}ticker={series.ticker}",
        f"{_HEADER_PREFIX}fetch_symbol={fetch_symbol}",
        f"{_HEADER_PREFIX}currency={series.currency}",
        f"{_HEADER_PREFIX}has_split_in_range={series.has_split_in_range}",
        f"{_HEADER_PREFIX}split_ratios={split_ratios}",
        f"{_HEADER_PREFIX}price_basis={series.price_basis}",
        f"{_HEADER_PREFIX}source={series.source}",
        f"{_HEADER_PREFIX}fetched_at={series.fetched_at.isoformat()}",
        f"{_HEADER_PREFIX}covered_start={covered_start.isoformat()}",
        f"{_HEADER_PREFIX}covered_end={covered_end.isoformat()}",
        "date,close_usd,high_usd",
    ]
    for day in sorted(series.closes):
        high = series.highs.get(day)
        high_str = str(high) if high is not None else ""
        payload_lines.append(f"{day.isoformat()},{series.closes[day]},{high_str}")
    payload = "\n".join(payload_lines) + "\n"
    warning = (
        f"{_HEADER_PREFIX}ibkr-fa price cache - regenerated from the price source; do not edit\n"
    )
    digest = hashlib.sha256(f"{warning}{payload}".encode()).hexdigest()
    path.write_text(
        warning + f"{_HEADER_PREFIX}{_HASH_KEY}={digest}\n" + payload,
        encoding="utf-8",
    )


def read_cache(
    path: Path,
    *,
    locked: bool = False,
) -> tuple[PriceSeries, date, date, str] | None:
    """Read a cache file written by :func:`write_cache`.

    Returns ``(series, covered_start, covered_end, fetch_symbol)``, or
    ``None`` only when no cache file exists. ``fetch_symbol`` falls back
    to the series ticker for caches written before that field existed.

    Raises:
        PriceCacheError: If the file exists but can't be read, has no
            ``payload_sha256`` line or one that doesn't match, doesn't
            parse, or fails a check (#940). ``locked`` only picks the
            advice in its message. These checks name the 1-based file line
            of the first problem (#1140, #1170). Metadata lines first, top to
            bottom: a ``has_split_in_range`` value other than exactly ``True``
            or ``False``, then any metadata key a line repeats. Then the table,
            top to bottom: a record that isn't one strict single-line CSV
            record, a header other than the two supported ones (or cells
            before it, or a second one), a row whose width differs from the
            header's, a data row on a date an earlier row already has, close
            text outside the decimal grammar, a close that isn't a positive
            price, high text outside the grammar, or a high below its close; a
            file with no header refuses at the line after its last. Then
            ``split_ratios``: text that isn't a JSON object, then member by
            member a date key that can't be read or repeats a date, or a ratio
            that isn't a positive decimal string. These format refusals do
            not echo the file's text or chain parser exceptions.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise_replacing(
            _cache_error(path, f"can't be read ({errno_name(exc)})", locked=locked), exc
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise_replacing(
            _cache_error(path, f"can't be read ({errno_name(exc)})", locked=locked), exc
        )
    except (UnicodeError, MemoryError) as exc:
        raise_replacing(_cache_error(path, "can't be read", locked=locked), exc)
    try:
        return _parse_cache(path, text, locked=locked)
    except (
        csv.Error,
        KeyError,
        ValueError,
        IndexError,
        InvalidOperation,
        InvariantError,
        RecursionError,
        MemoryError,
    ):
        pass
    # Raised after the handler, so the parser's exception, whose text can hold
    # input, isn't chained to it (#1170).
    raise _cache_error(path, "doesn't parse", locked=locked)


def _parse_cache(path: Path, text: str, *, locked: bool) -> tuple[PriceSeries, date, date, str]:
    lines = text.splitlines(keepends=True)
    hash_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(f"{_HEADER_PREFIX}{_HASH_KEY}=")
        ),
        None,
    )
    if hash_index is None:
        raise _cache_error(path, f"has no {_HASH_KEY} line", locked=locked)
    expected = lines[hash_index].partition("=")[2].strip()
    hashed_content = "".join(lines[:hash_index] + lines[hash_index + 1 :])
    if hashlib.sha256(hashed_content.encode()).hexdigest() != expected:
        raise _cache_error(path, f"doesn't match its {_HASH_KEY} line", locked=locked)
    physical_lines = text.splitlines()
    meta, meta_line_numbers = _read_metadata(path, physical_lines, locked=locked)
    closes, highs = _read_table(path, physical_lines, locked=locked)
    split_ratios = (
        _read_split_ratios(
            path,
            meta[_SPLIT_RATIOS_KEY],
            meta_line_numbers[_SPLIT_RATIOS_KEY],
            locked=locked,
        )
        if _SPLIT_RATIOS_KEY in meta
        else {}
    )
    covered_start = date.fromisoformat(meta["covered_start"])
    covered_end = date.fromisoformat(meta["covered_end"])
    if covered_start > covered_end:
        raise _cache_error(path, "has covered_start after covered_end", locked=locked)
    if not closes:
        raise _cache_error(path, "has no usable close", locked=locked)
    series = PriceSeries(
        ticker=Ticker(meta["ticker"]),
        currency=meta["currency"],
        closes=closes,
        highs=highs,
        has_split_in_range=meta[_SPLIT_FLAG_KEY] == "True",
        source=meta.get("source", str(path)),
        fetched_at=datetime.fromisoformat(meta["fetched_at"]),
        split_ratios=split_ratios,
        price_basis=meta.get("price_basis", CURRENT_SPLIT_BASIS),
    )
    if (
        series.price_basis == CONTEMPORANEOUS_BASIS
        and series.has_split_in_range
        and not series.split_ratios
    ):
        raise _cache_error(
            path,
            "records a split on the contemporaneous basis without its ratios",
            locked=locked,
        )
    return (
        series,
        covered_start,
        covered_end,
        meta.get("fetch_symbol", meta["ticker"]),
    )


def _read_metadata(
    path: Path, physical_lines: list[str], *, locked: bool
) -> tuple[dict[str, str], dict[str, int]]:
    """Each metadata key's stripped value and its 1-based line.

    Every line is checked as it's read, before it's stored, so a later line
    can't hide or replace an earlier one (#1140, #1170).
    """
    meta: dict[str, str] = {}
    line_numbers: dict[str, int] = {}
    for line_number, line in enumerate(physical_lines, start=1):
        if not line.startswith(_HEADER_PREFIX):
            continue
        key, separator, value = line[len(_HEADER_PREFIX) :].partition("=")
        if not separator:
            continue
        key = key.strip()
        if key == _SPLIT_FLAG_KEY:
            # A boolean flag is written as exactly True or False (#1140). Its
            # value check comes before its repeat check.
            if value not in ("True", "False"):
                raise _cache_error(
                    path,
                    f"has a {_SPLIT_FLAG_KEY} value that isn't exactly True or False "
                    f"(line {line_number})",
                    locked=locked,
                )
            if key in meta:
                raise _cache_error(
                    path,
                    f"has more than one {_SPLIT_FLAG_KEY} line (line {line_number})",
                    locked=locked,
                )
        elif key in meta:
            known = _METADATA_KEYS.get(key)
            where = "field metadata key" if known is None else f"key {known}"
            raise _cache_error(
                path,
                f"has a repeated metadata key (line {line_number}, {where})",
                locked=locked,
            )
        meta[key] = value.strip()
        line_numbers[key] = line_number
    return meta, line_numbers


def _csv_record(line: str) -> list[str] | None:
    """``line``'s cells as one strict CSV record, or ``None`` if it isn't one.

    ``line`` holds no line break, so quoting left open at its end is an error
    here: a record never continues on the next line (#1170).
    """
    try:
        records = list(csv.reader([line], strict=True))
    except csv.Error:
        return None
    return records[0] if len(records) == 1 else None


def _read_table(
    path: Path, physical_lines: list[str], *, locked: bool
) -> tuple[dict[date, Decimal], dict[date, Decimal]]:
    """The closes and highs in the table: every line that isn't a comment,
    metadata or empty, top to bottom (#1140, #1170)."""
    header: tuple[str, ...] | None = None
    closes: dict[date, Decimal] = {}
    highs: dict[date, Decimal] = {}
    for line_number, line in enumerate(physical_lines, start=1):
        if not line or line.startswith(_HEADER_PREFIX):
            continue
        row = _csv_record(line)
        if row is None:
            problem = "has malformed CSV quoting, a multiline record or an over-limit cell"
            raise _cache_error(
                path, f"{problem} (line {line_number}, field CSV record)", locked=locked
            )
        if header is None:
            if tuple(row) in _TABLE_HEADERS:
                header = tuple(row)
                continue
            problem = (
                f"has an unsupported table header (line {line_number}, {_COLUMNS})"
                if row[0] == "date"
                else f"has cells before the table header (line {line_number}, field CSV record)"
            )
            raise _cache_error(path, problem, locked=locked)
        if row[0] == "date":
            raise _cache_error(
                path,
                f"has more than one table header (line {line_number}, field table header)",
                locked=locked,
            )
        if len(row) != len(header):
            raise _cache_error(
                path,
                f"has a row whose width differs from the table header "
                f"(line {line_number}, {_COLUMNS})",
                locked=locked,
            )
        _read_row(path, row, line_number, closes, highs, locked=locked)
    if header is None:
        raise _cache_error(
            path,
            f"has no supported table header (line {len(physical_lines) + 1}, field table header)",
            locked=locked,
        )
    return closes, highs


def _finite_decimal(text: str) -> Decimal:
    """Refuse unrepresentable exponents even when the caller disables Decimal traps."""
    value = Decimal(text)
    if not value.is_finite():
        raise InvalidOperation
    return value


def _require_within_limits(
    path: Path, value: Decimal, line_number: int, name: str, *, locked: bool
) -> None:
    """Refuse a close or high past a unit price's read-time digit limits (#1276)."""
    breach = number_limit_breach(value, NumberKind.PRICE)
    if breach is not None:
        raise _cache_error(
            path,
            f"has a {name} with too many digits: {breach} (line {line_number}, column {name}_usd)",
            locked=locked,
        )


def _read_row(
    path: Path,
    row: list[str],
    line_number: int,
    closes: dict[date, Decimal],
    highs: dict[date, Decimal],
    *,
    locked: bool,
) -> None:
    """Check one data row's cells and store its close and any high."""
    day = date.fromisoformat(row[0])
    # No row is skipped: each one refuses or enters closes, so a date
    # already in closes is a second row for it, whatever its close.
    if day in closes:
        raise _cache_error(
            path,
            f"has two rows for one date (line {line_number}, column date)",
            locked=locked,
        )
    if _DECIMAL_TEXT.fullmatch(row[1]) is None:
        raise _cache_error(
            path,
            "has close text outside the supported ASCII decimal grammar "
            f"(line {line_number}, column close_usd)",
            locked=locked,
        )
    close_dec = _finite_decimal(row[1])
    # A zero or negative close is a bad bar. The Yahoo and Nasdaq readers
    # refuse one; a legacy or hand-built cache can still hold one. Refuse
    # the cache (#1140): skipping the row could hide a second row on its
    # date. The grammar above already refuses NaN and Infinity.
    if close_dec <= 0:
        raise _cache_error(
            path,
            f"has a close that isn't a positive price (line {line_number}, column close_usd)",
            locked=locked,
        )
    _require_within_limits(path, close_dec, line_number, "close", locked=locked)
    closes[day] = close_dec
    # A blank high cell, or a legacy header with no high column, means no high
    # is available for this date. A present high must be a number at or above
    # the close: a real daily high never sits below it, and Yahoo's reader
    # refuses that bar too, so the cache refuses rather than dropping the high.
    if len(row) > _HIGH_COL_INDEX and row[_HIGH_COL_INDEX]:
        if _DECIMAL_TEXT.fullmatch(row[_HIGH_COL_INDEX]) is None:
            raise _cache_error(
                path,
                "has high text outside the supported ASCII decimal grammar "
                f"(line {line_number}, column high_usd)",
                locked=locked,
            )
        high_dec = _finite_decimal(row[_HIGH_COL_INDEX])
        _require_within_limits(path, high_dec, line_number, "high", locked=locked)
        if high_dec < close_dec:
            raise _cache_error(
                path,
                f"has a high below its close (line {line_number}, column high_usd)",
                locked=locked,
            )
        highs[day] = high_dec


def _json_number(_text: str) -> object:
    """Keep a JSON number unconverted: a split ratio is a string (#1170)."""
    return _JSON_NUMBER


def _json_object_members(text: str) -> tuple[tuple[str, object], ...] | None:
    """``text``'s top-level JSON object as its members in order, repeats kept,
    or ``None`` if it isn't one. Each nested object is a tuple of members too,
    and each number is :data:`_JSON_NUMBER`."""
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=tuple,
            parse_int=_json_number,
            parse_float=_json_number,
            parse_constant=_json_number,
        )
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, tuple) else None


def _iso_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _read_split_ratios(
    path: Path, text: str, line_number: int, *, locked: bool
) -> dict[date, Decimal]:
    """The ``split_ratios`` value on ``line_number`` as ``date -> ratio`` (#1170).

    Each member is checked in order before it's stored, so a repeated date
    refuses before either ratio could be dropped.
    """
    where = f"line {line_number}, key {_SPLIT_RATIOS_KEY}"
    members = _json_object_members(text)
    if members is None:
        raise _cache_error(
            path, f"has split_ratios that is not a JSON object ({where})", locked=locked
        )
    ratios: dict[date, Decimal] = {}
    for entry, (key, value) in enumerate(members, start=1):
        day = _iso_date(key)
        if day is None:
            raise _cache_error(
                path,
                f"has a split date that cannot be read ({where}, entry {entry}, field date)",
                locked=locked,
            )
        if day in ratios:
            raise _cache_error(
                path,
                f"has more than one ratio for a split date ({where}, entry {entry}, field date)",
                locked=locked,
            )
        if (
            not isinstance(value, str)
            or _DECIMAL_TEXT.fullmatch(value) is None
            or (ratio := _finite_decimal(value)) <= 0
        ):
            raise _cache_error(
                path,
                "has a split ratio outside the supported positive finite decimal form "
                f"({where}, entry {entry}, field ratio)",
                locked=locked,
            )
        ratios[day] = ratio
    return ratios


def load_or_fetch(
    ticker: Ticker,
    *,
    start: date,
    end: date,
    cache_dir: Path,
    fetcher: DailyCloseFetcher,
    now: datetime,
    refresh: bool = False,
    fetch: bool = True,
    fetch_symbol: str | None = None,
) -> PriceSeries | None:
    """Return a cached series covering ``[start, end]`` or fetch one.

    Reuses a contemporaneous-basis cache whenever it fully covers the
    requested span and was produced under the same ``fetch_symbol``.
    Legacy current-split-basis caches retain the same-day restriction.
    The fetch-symbol condition prevents a cache produced under one
    ``[prices].ticker_overrides`` mapping from being reused after the
    override is added / changed / removed. A valid cache that doesn't
    match is refetched and overwritten; with ``fetch=False`` it gives
    ``None`` and the network is never called. ``refresh`` refetches over
    a valid unlocked cache even when it matches; a valid lock overrides it.

    Raises:
        PriceCacheError: If the cache file exists but fails a
            :func:`read_cache` check, or a ``.csv.lock`` exists and doesn't
            verify, or its cache doesn't match this request. Every existing
            cache is checked first, even under ``refresh``; nothing is
            fetched or written (#940, #1170).
        PriceSourceContradictionError: If the fetcher refuses a response with
            two bars on one date. No cache is written (#942).
        YahooResponseFormatError: If the fetcher refuses a malformed Yahoo
            response. No cache is written (#1139).
    """
    requested = fetch_symbol if fetch_symbol is not None else str(ticker)
    path = _cache_path(cache_dir, ticker)
    locked = _cache_locked(path)
    cached = read_cache(path, locked=locked)
    if cached is not None and (locked or not refresh):
        series, covered_start, covered_end, cached_fetch_symbol = cached
        if (
            covered_start <= start
            and covered_end >= end
            and (
                series.price_basis == CONTEMPORANEOUS_BASIS
                or series.fetched_at.date() == now.date()
            )
            and series.ticker == ticker
            and cached_fetch_symbol == requested
        ):
            return series
    if locked:
        raise _cache_error(
            path,
            "doesn't match this request (span, ticker, fetch symbol, price basis or fetch day)",
            locked=True,
        )
    if not fetch:
        return None
    fetched = fetcher(ticker, start=start, end=end, now=now, fetch_symbol=fetch_symbol)
    if fetched is None:
        return None
    write_cache(path, fetched, covered_start=start, covered_end=end, fetch_symbol=requested)
    return fetched
