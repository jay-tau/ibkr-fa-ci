"""Price cache and lock refusals keep nothing of the error they replace (#1225).

A price cache file, or its ``.csv.lock``, that exists but can't be read refuses
with a ``PriceCacheError`` naming only the cache folder and a fixed problem.
The error it replaces can hold the cache's or lock's path, whose file name is
built from the holding's symbol, a filesystem error's text, a decoder's bytes
or a ``MemoryError``'s text. The refusal keeps none of it: it has no context,
even under a caller's ``except``, and its only cause is the content-free
stand-in :func:`ibkr_fa.exceptions.raise_replacing` leaves when the replaced
error, or the caller's, is a crash. So ``IBKR_FA_TRACEBACK=1`` still re-raises
a refusal that replaced a decode error, and still prints one that replaced
only a filesystem error.

Every refusal leaves the cache folder as it was and calls no fetcher, offline
or online, refresh or not; the checks run in the order they did before.
"""

from __future__ import annotations

import errno
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import NamedTuple

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.prices.cache import (
    PriceCacheError,
    _cache_lock_path,
    _cache_path,
    load_or_fetch,
    write_cache,
    write_cache_lock,
)
from ibkr_fa.prices.series import CONTEMPORANEOUS_BASIS, PriceSeries
from ibkr_fa.types import Ticker
from tests._activity_statement_e2e import ACTIVITY_STATEMENT_E2E_USER_TOML
from tests._crash_reports import assert_no_markers, assert_replaced_unchained
from tests._ibkr_run_all import CY_2025_INPUT
from tests._price_cache import CACHE_NOW
from tests._price_cache import CY_2025_FIRST_PRICED_TICKER as _CLI_SYMBOL
from tests._price_cache import RecordingFetcher as _RecordingFetcher
from tests._price_cache import assert_price_cache_refusal as _assert_refusal
from tests._price_cache import cache_series as _series
from tests._price_cache import price_cache_refusal as _refusal
from tests._price_cache import price_flags as _price_flags

# A synthetic holding only this module uses: its symbol names the cache file.
_TICKER = Ticker("XQKV")
# The CY 2025 statement's first priced holding, whose cache the CLI reads first.
_CLI_TICKER = Ticker(_CLI_SYMBOL)
_START = date(2025, 12, 20)
_END = date(2025, 12, 31)

# One neutral marker per failure: a filesystem error's text, a decoder's bytes
# or a ``MemoryError``'s text.
_LOCK_LSTAT = "QZ25CLLST"
_LOCK_READ = "QZ25CLRD"
_LOCK_BYTES = "QZ25CLBYT"
_LOCK_MEMORY = "QZ25CLMEM"
_LOCKED_READ = "QZ25CKRD"
_LOCKED_MEMORY = "QZ25CKMEM"
_CACHE_LSTAT = "QZ25CCLST"
_CACHE_READ = "QZ25CCRD"
_CACHE_BYTES = "QZ25CCBYT"
_CACHE_MEMORY = "QZ25CCMEM"
_CALLER = "QZ25CCALL"

_LOCK_UNREADABLE = "has a lock file that can't be read"
_UNREADABLE = "can't be read"


class _Mode(NamedTuple):
    fetch: bool
    refresh: bool


_MODES = pytest.mark.parametrize(
    "mode",
    [
        pytest.param(_Mode(fetch=False, refresh=False), id="offline"),
        pytest.param(_Mode(fetch=True, refresh=False), id="online"),
        pytest.param(_Mode(fetch=True, refresh=True), id="online-refresh"),
    ],
)
_ONLINE = pytest.mark.parametrize("online", [False, True], ids=["offline", "online"])


def _expected_series(ticker: Ticker = _TICKER) -> PriceSeries:
    return _series(ticker=ticker, price_basis=CONTEMPORANEOUS_BASIS)


def _valid_cache(cache_dir: Path, ticker: Ticker = _TICKER) -> Path:
    """Write an unlocked cache for ``ticker`` that covers the request; return
    its real, symbol-built path."""
    path = _cache_path(cache_dir, ticker)
    write_cache(
        path,
        _expected_series(ticker),
        covered_start=date(2025, 12, 1),
        covered_end=_END,
        fetch_symbol=ticker,
    )
    return path


def _load(cache_dir: Path, fetcher: _RecordingFetcher, mode: _Mode) -> PriceSeries | None:
    return load_or_fetch(
        _TICKER,
        start=_START,
        end=_END,
        cache_dir=cache_dir,
        fetcher=fetcher,
        now=CACHE_NOW,
        fetch=mode.fetch,
        refresh=mode.refresh,
    )


def _entries(cache_dir: Path) -> dict[str, bytes | None]:
    """Every entry of ``cache_dir`` with its bytes (``None`` for a folder),
    read without the ``Path`` methods these tests make fail."""
    entries: dict[str, bytes | None] = {}
    for entry in sorted(cache_dir.iterdir()):
        if entry.is_dir():
            entries[entry.name] = None
            continue
        with entry.open("rb") as file:
            entries[entry.name] = file.read()
    return entries


def _assert_cache_refusal(
    error: BaseException,
    path: Path,
    problem: str,
    *,
    locked: bool,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    """``error`` is a plain ``PriceCacheError`` whose one arg is its complete
    message, with the lock's advice when ``locked``, and no attribute of its
    own. Then no marker is anywhere in its exception graph; it has no context,
    and its only cause is the stand-in for ``crash``, if there is one."""
    message = _refusal(path, problem, locked=locked)
    assert type(error) is PriceCacheError
    assert error.args == (message,)
    assert vars(error) == {}
    _assert_refusal(str(error), path, problem, locked=locked)
    assert_replaced_unchained(error, crash, markers=markers)


# --- Failures, each for exactly one real cache or lock path -----------------


def _fail(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    target: Path,
    error: Callable[[], BaseException],
) -> None:
    """``Path.<method>`` raises ``error()`` for ``target`` only."""
    original = getattr(Path, method)

    def failing(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            raise error()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)


def _denied(
    monkeypatch: pytest.MonkeyPatch, method: str, target: Path, strerror: str
) -> tuple[str, ...]:
    """``Path.<method>`` fails on ``target`` with its real path as the file
    name, as ``os.lstat`` and ``open`` do."""
    _fail(monkeypatch, method, target, lambda: PermissionError(errno.EACCES, strerror, str(target)))
    return (strerror,)


def _out_of_memory(
    monkeypatch: pytest.MonkeyPatch, method: str, target: Path, text: str
) -> tuple[str, ...]:
    _fail(monkeypatch, method, target, lambda: MemoryError(text))
    return (text,)


def _folder(path: Path) -> None:
    """A folder in place of ``path``: reading it raises a real
    ``IsADirectoryError`` naming the path."""
    path.unlink(missing_ok=True)
    path.mkdir()


# Each damage takes the unlocked valid cache's path and returns the markers of
# its failure; the symbol that names the cache and lock files is one more.
_Damage = Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]]


def _lock_lstat_denied(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    write_cache_lock(path)
    return _denied(monkeypatch, "lstat", _cache_lock_path(path), _LOCK_LSTAT)


def _lock_read_denied(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    write_cache_lock(path)
    return _denied(monkeypatch, "read_text", _cache_lock_path(path), _LOCK_READ)


def _lock_folder(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    _folder(_cache_lock_path(path))
    return ()


def _lock_not_ascii(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """The ASCII decoder's error keeps the lock's bytes."""
    _cache_lock_path(path).write_bytes(_LOCK_BYTES.encode() + b"\xff\n")
    return (_LOCK_BYTES,)


def _lock_out_of_memory(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    write_cache_lock(path)
    return _out_of_memory(monkeypatch, "read_text", _cache_lock_path(path), _LOCK_MEMORY)


def _locked_cache_missing(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A real ``FileNotFoundError`` names the missing cache's path."""
    write_cache_lock(path)
    path.unlink()
    return ()


def _locked_read_denied(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    write_cache_lock(path)
    return _denied(monkeypatch, "read_bytes", path, _LOCKED_READ)


def _locked_folder(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """The lock holds a digest; the cache it locks is now a folder."""
    write_cache_lock(path)
    _folder(path)
    return ()


def _locked_out_of_memory(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    write_cache_lock(path)
    return _out_of_memory(monkeypatch, "read_bytes", path, _LOCKED_MEMORY)


def _lock_if(path: Path, locked: bool) -> None:
    if locked:
        write_cache_lock(path)


def _cache_lstat_denied(locked: bool) -> _Damage:
    def damage(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        _lock_if(path, locked)
        return _denied(monkeypatch, "lstat", path, _CACHE_LSTAT)

    return damage


def _cache_read_denied(locked: bool) -> _Damage:
    def damage(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        _lock_if(path, locked)
        return _denied(monkeypatch, "read_text", path, _CACHE_READ)

    return damage


def _cache_not_utf8(locked: bool) -> _Damage:
    """The UTF-8 decoder's error keeps the cache's bytes, which a lock may pin."""

    def damage(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        path.write_bytes(_CACHE_BYTES.encode() + b"\xff\n")
        _lock_if(path, locked)
        return (_CACHE_BYTES,)

    return damage


def _cache_out_of_memory(locked: bool) -> _Damage:
    def damage(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        _lock_if(path, locked)
        return _out_of_memory(monkeypatch, "read_text", path, _CACHE_MEMORY)

    return damage


def _cache_folder(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    _folder(path)
    return ()


# read_cache's failures (name, damage, problem, crash), unlocked or under a
# lock the cache matches.
_READ_CACHE_FAILURES = (
    ("lstat-denied", _cache_lstat_denied, f"{_UNREADABLE} (EACCES)", None),
    ("read-denied", _cache_read_denied, f"{_UNREADABLE} (EACCES)", None),
    ("not-utf8", _cache_not_utf8, _UNREADABLE, UnicodeDecodeError),
    ("out-of-memory", _cache_out_of_memory, _UNREADABLE, MemoryError),
)


# (damage, problem, locked, crash): every refusal site in ``_cache_locked`` and
# ``read_cache`` that replaces an error.
_REFUSALS = [
    # The lock's lstat, then its text.
    pytest.param(
        _lock_lstat_denied, f"{_LOCK_UNREADABLE} (EACCES)", True, None, id="lock-lstat-denied"
    ),
    pytest.param(
        _lock_read_denied, f"{_LOCK_UNREADABLE} (EACCES)", True, None, id="lock-read-denied"
    ),
    pytest.param(_lock_folder, f"{_LOCK_UNREADABLE} (EISDIR)", True, None, id="lock-folder"),
    pytest.param(_lock_not_ascii, _LOCK_UNREADABLE, True, UnicodeDecodeError, id="lock-not-ascii"),
    pytest.param(_lock_out_of_memory, _LOCK_UNREADABLE, True, MemoryError, id="lock-out-of-memory"),
    # The bytes of the cache a lock pins.
    pytest.param(
        _locked_cache_missing,
        "is missing but has a lock file",
        True,
        None,
        id="locked-cache-missing",
    ),
    pytest.param(
        _locked_read_denied, f"{_UNREADABLE} (EACCES)", True, None, id="locked-cache-bytes-denied"
    ),
    pytest.param(
        _locked_folder, f"{_UNREADABLE} (EISDIR)", True, None, id="locked-cache-bytes-folder"
    ),
    pytest.param(
        _locked_out_of_memory, _UNREADABLE, True, MemoryError, id="locked-cache-bytes-out-of-memory"
    ),
    # The cache's lstat, then its text, unlocked or under a lock it matches.
    *(
        pytest.param(
            make(locked),
            problem,
            locked,
            crash,
            id=f"{'locked' if locked else 'unlocked'}-cache-{name}",
        )
        for locked in (False, True)
        for name, make, problem, crash in _READ_CACHE_FAILURES
    ),
    pytest.param(_cache_folder, f"{_UNREADABLE} (EISDIR)", False, None, id="unlocked-cache-folder"),
]


@_MODES
@pytest.mark.parametrize(("damage", "problem", "locked", "crash"), _REFUSALS)
def test_cache_refusal_keeps_nothing_of_the_error_it_replaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: _Damage,
    problem: str,
    locked: bool,
    crash: type[BaseException] | None,
    mode: _Mode,
) -> None:
    path = _valid_cache(tmp_path)
    markers = (*damage(path, monkeypatch), _TICKER)
    before = _entries(tmp_path)
    fetcher = _RecordingFetcher(_expected_series())

    with pytest.raises(PriceCacheError) as refusal:
        _load(tmp_path, fetcher, mode)

    _assert_cache_refusal(refusal.value, path, problem, locked=locked, crash=crash, markers=markers)
    # Never fetched over, and the cache and its lock are left as they were.
    assert fetcher.calls == []
    assert _entries(tmp_path) == before


# --- Controls: what still reads, and what is still a miss ---------------------


@_ONLINE
@pytest.mark.parametrize("locked", [False, True], ids=["unlocked", "locked"])
def test_valid_cache_still_returns_its_series_without_fetching(
    tmp_path: Path, locked: bool, online: bool
) -> None:
    """Locked or not, a valid cache that covers the request is the series."""
    path = _valid_cache(tmp_path)
    _lock_if(path, locked)
    before = _entries(tmp_path)
    fetcher = _RecordingFetcher(_expected_series())

    result = _load(tmp_path, fetcher, _Mode(fetch=online, refresh=False))

    assert result == _expected_series()
    assert fetcher.calls == []
    assert _entries(tmp_path) == before


@_ONLINE
def test_missing_cache_without_a_lock_is_still_a_miss(tmp_path: Path, online: bool) -> None:
    """Only a lock makes a missing cache refuse (``locked-cache-missing``
    above); without one it is fetched once online and is ``None`` offline."""
    fetcher = _RecordingFetcher(_expected_series())

    result = _load(tmp_path, fetcher, _Mode(fetch=online, refresh=False))

    if online:
        assert result == _expected_series()
        assert fetcher.calls == [(_TICKER, _START, _END)]
        assert list(_entries(tmp_path)) == [_cache_path(tmp_path, _TICKER).name]
    else:
        assert result is None
        assert fetcher.calls == []
        assert _entries(tmp_path) == {}


# --- Two failures: only the first one checked reaches the refusal -------------


def _lock_lstat_and_cache_lstat_denied(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    _cache_lstat_denied(locked=False)(path, monkeypatch)
    return _lock_lstat_denied(path, monkeypatch)


def _lock_not_ascii_and_cache_not_utf8(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    _cache_not_utf8(locked=False)(path, monkeypatch)
    return _lock_not_ascii(path, monkeypatch)


def _lock_lstat_denied_and_cache_not_utf8(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    _cache_not_utf8(locked=False)(path, monkeypatch)
    return _lock_lstat_denied(path, monkeypatch)


def _lock_not_ascii_and_cache_lstat_denied(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    _cache_lstat_denied(locked=False)(path, monkeypatch)
    return _lock_not_ascii(path, monkeypatch)


def _locked_bytes_denied_then_not_utf8(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    """The lock pins non-UTF-8 bytes that its own check can't read either."""
    _cache_not_utf8(locked=True)(path, monkeypatch)
    return _denied(monkeypatch, "read_bytes", path, _LOCKED_READ)


def _locked_bytes_out_of_memory_then_lstat_denied(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, ...]:
    _cache_lstat_denied(locked=True)(path, monkeypatch)
    return _out_of_memory(monkeypatch, "read_bytes", path, _LOCKED_MEMORY)


@pytest.mark.parametrize(
    ("damage", "problem", "crash"),
    [
        # The lock is checked before the cache's own read.
        pytest.param(
            _lock_lstat_and_cache_lstat_denied,
            f"{_LOCK_UNREADABLE} (EACCES)",
            None,
            id="lock-lstat-before-cache-lstat",
        ),
        pytest.param(
            _lock_lstat_denied_and_cache_not_utf8,
            f"{_LOCK_UNREADABLE} (EACCES)",
            None,
            id="lock-lstat-before-cache-text",
        ),
        pytest.param(
            _lock_not_ascii_and_cache_lstat_denied,
            _LOCK_UNREADABLE,
            UnicodeDecodeError,
            id="lock-text-before-cache-lstat",
        ),
        pytest.param(
            _lock_not_ascii_and_cache_not_utf8,
            _LOCK_UNREADABLE,
            UnicodeDecodeError,
            id="lock-text-before-cache-text",
        ),
        # The lock's check reads the cache's bytes before read_cache does.
        pytest.param(
            _locked_bytes_denied_then_not_utf8,
            f"{_UNREADABLE} (EACCES)",
            None,
            id="locked-bytes-before-cache-text",
        ),
        pytest.param(
            _locked_bytes_out_of_memory_then_lstat_denied,
            _UNREADABLE,
            MemoryError,
            id="locked-bytes-before-cache-lstat",
        ),
    ],
)
def test_first_failure_checked_is_the_one_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: _Damage,
    problem: str,
    crash: type[BaseException] | None,
) -> None:
    """Each damage makes two checks fail and returns the first one's markers."""
    path = _valid_cache(tmp_path)
    markers = (*damage(path, monkeypatch), _TICKER)
    before = _entries(tmp_path)
    fetcher = _RecordingFetcher(_expected_series())

    with pytest.raises(PriceCacheError) as refusal:
        _load(tmp_path, fetcher, _Mode(fetch=True, refresh=False))

    _assert_cache_refusal(refusal.value, path, problem, locked=True, crash=crash, markers=markers)
    assert fetcher.calls == []
    assert _entries(tmp_path) == before


# --- Under a caller's handler ---------------------------------------------------


def _caller_domain_error() -> BaseException:
    return OSError(errno.EIO, _CALLER)


def _caller_crash() -> BaseException:
    return ValueError(_CALLER)


@pytest.mark.parametrize(
    ("caller", "caller_crash"),
    [
        pytest.param(_caller_domain_error, None, id="caller-oserror"),
        pytest.param(_caller_crash, ValueError, id="caller-valueerror"),
    ],
)
@pytest.mark.parametrize(
    ("damage", "problem", "replaced_crash"),
    [
        pytest.param(
            _lock_lstat_denied, f"{_LOCK_UNREADABLE} (EACCES)", None, id="replaced-oserror"
        ),
        pytest.param(
            _lock_not_ascii, _LOCK_UNREADABLE, UnicodeDecodeError, id="replaced-decode-error"
        ),
    ],
)
def test_refusal_under_a_caller_handler_links_none_of_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: Callable[[], BaseException],
    caller_crash: type[BaseException] | None,
    damage: _Damage,
    problem: str,
    replaced_crash: type[BaseException] | None,
) -> None:
    """The caller's exception is neither the refusal's context nor its cause.
    The only link is a stand-in, and only when the replaced error or the
    caller's is a crash: the replaced one's if it is one, else the caller's."""
    path = _valid_cache(tmp_path)
    markers = (*damage(path, monkeypatch), _TICKER)
    before = _entries(tmp_path)
    fetcher = _RecordingFetcher(_expected_series())

    try:
        raise caller()
    except (OSError, ValueError):
        with pytest.raises(PriceCacheError) as refusal:
            _load(tmp_path, fetcher, _Mode(fetch=True, refresh=False))

    _assert_cache_refusal(
        refusal.value,
        path,
        problem,
        locked=True,
        crash=replaced_crash or caller_crash,
        markers=(*markers, _CALLER),
    )
    assert fetcher.calls == []
    assert _entries(tmp_path) == before


# --- The CLI ------------------------------------------------------------------


class _CliCase(NamedTuple):
    damage: _Damage
    problem: str
    crash: type[BaseException] | None


_CLI_CASES = pytest.mark.parametrize(
    "case",
    [
        # A decode error is a crash, so the debug flag re-raises the refusal.
        pytest.param(
            _CliCase(_lock_not_ascii, _LOCK_UNREADABLE, UnicodeDecodeError), id="lock-not-ascii"
        ),
        # A filesystem error is a domain error, so the refusal still prints.
        pytest.param(
            _CliCase(_lock_lstat_denied, f"{_LOCK_UNREADABLE} (EACCES)", None),
            id="lock-lstat-denied",
        ),
    ],
)


def _schedule_fa_argv(tmp_path: Path, cache_dir: Path, flags: tuple[str, ...]) -> list[str]:
    """``schedule-fa`` on the CY 2025 statement; an earlier run's outputs are
    in its output folder."""
    config = tmp_path / "user.toml"
    config.write_text(ACTIVITY_STATEMENT_E2E_USER_TOML, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    output = out / "schedule-fa.csv"
    for stale in (output, out / "schedule-fa-a2.csv"):
        stale.write_bytes(b"an earlier run")
    return [
        "schedule-fa",
        *("--input", str(CY_2025_INPUT), "--config", str(config)),
        *("--output", str(output), "--calendar-year", "2025"),
        *("--price-cache-dir", str(cache_dir)),
        *flags,
    ]


@_CLI_CASES
@_ONLINE
@pytest.mark.parametrize("traceback_flag", [False, True], ids=["default", "traceback-flag"])
def test_schedule_fa_refuses_an_unreadable_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: _CliCase,
    online: bool,
    traceback_flag: bool,
) -> None:
    """Exit 4 with only the refusal, no output left and the cache folder as it
    was. Under ``IBKR_FA_TRACEBACK=1`` a refusal that replaced a crash is
    re-raised after cleanup, holding only the stand-in; one that replaced a
    filesystem error prints as it does without the flag."""
    cache_dir = tmp_path / "cache"
    path = _valid_cache(cache_dir, _CLI_TICKER)
    markers = (*case.damage(path, monkeypatch), _CLI_TICKER)
    before = _entries(cache_dir)
    argv = _schedule_fa_argv(tmp_path, cache_dir, _price_flags(monkeypatch, online=online))
    if traceback_flag:
        monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    if traceback_flag and case.crash is not None:
        with pytest.raises(PriceCacheError) as raised:
            main(argv)
        assert cli._holds_crash(raised.value)
        _assert_cache_refusal(
            raised.value, path, case.problem, locked=True, crash=case.crash, markers=markers
        )
        assert capsys.readouterr() == ("", "")
    else:
        assert main(argv) == 4
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"error: {_refusal(path, case.problem, locked=True)}\n"
        assert_no_markers(captured.err, *markers, path.name)
    assert list((tmp_path / "out").iterdir()) == []
    assert _entries(cache_dir) == before
