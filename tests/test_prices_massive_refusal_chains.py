"""Massive option-close refusals keep no request, cache or key content (#1225).

A failed Massive request, an option cache artifact that can't be published or
read, and an undecodable API key file each refuse with a plain
``MassiveOptionPriceError`` naming a fixed problem. The error it replaces can
hold the request URL, which names the held contract, the response's reason
and headers, a partial read or status line, the artifact's contract- and
date-derived file name, the cached document or bytes, the cached fetch time,
or the key file's bytes. The refusal keeps none of it: it has no context, and
its only cause is the content-free stand-in
:func:`ibkr_fa.exceptions.raise_replacing` leaves for a crash, so
``IBKR_FA_TRACEBACK=1`` still re-raises it as an error that replaced a crash.
On 2c018e3 each site raised ``from`` the caught error.

Each refusal test checks, in order: the unchanged class, args and complete
message; no marker anywhere in the exception graph; then the chain's shape.
Markers stay in module constants and builders, off the line that calls the
loader, since a rendered traceback prints that line.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import pytest

import ibkr_fa.pipeline_robinhood_option_prices as option_prices
from ibkr_fa.pipeline_robinhood_option_prices import collect_robinhood_option_price_audit
from ibkr_fa.prices import massive_options
from ibkr_fa.prices.massive_options import (
    MassiveOptionPriceError,
    load_or_fetch_massive_option_closes,
)
from ibkr_fa.prices.series import MassiveOptionCloseSeries, OptionCloseBar
from tests._crash_reports import assert_replaced_unchained
from tests._entity_transport import (  # noqa: F401 - autouse fixture
    close_http_errors as _close_http_errors,
)
from tests._entity_transport import http_error
from tests._massive_option_replies import MR_SYMBOL as _MR_SYMBOL
from tests._massive_option_replies import mr_bars, mr_overview
from tests._robinhood_option_audit import held_call_options as _options
from tests._robinhood_option_audit import massive_key_config as _config

# The contract this module holds, in its file-name form: the request URLs and
# the artifact's file name carry it.
_SYMBOL = "XQMVZ"
_CONTRACT = "XQMVZ250117C00250000"
_TICKER = f"O:{_CONTRACT}"
_EXPIRATION = date(2025, 1, 17)
_START = date(2025, 1, 1)
_END = date(2025, 1, 17)
_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_OVERVIEW_URL = f"https://api.massive.com/v3/reference/options/contracts/{_TICKER}?as_of=2025-01-17"
_BARS_URL = (
    f"https://api.massive.com/v2/aggs/ticker/{_TICKER}/range/1/day/2025-01-01/2025-01-17"
    "?adjusted=false&sort=asc&limit=5000"
)
_INTERVAL = 12.1

# One neutral marker per kind of content.
_AUTH = "QZ25MAUTH"
_REASON = "QZ25MREASN"
_HEADER = "QZ25MHEADR"
_STRERROR = "QZ25MSTRER"
_PARTIAL = "QZ25MPART"
_STATUS_LINE = "QZ25MSTAT"
_DOCUMENT = "QZ25MDOC"
_BYTES = "QZ25MBYTES"
_FETCHED_AT = "QZ25MFETCH"
_KEY = "QZ25MKEY"
_CALLER = "QZ25MCALL"
# What every fetch's input carries: the contract and the API key.
_FETCH = (_CONTRACT, _AUTH)

_UNPARSABLE = f'{{"{_DOCUMENT}": '
_UNDECODABLE_KEY = f"MASSIVE_API_KEY={_KEY}".encode() + b"\xff\n"

# The artifact a valid fetch of this contract publishes, as before.
_ARTIFACT_SHA256 = "bd206fbf7e1286d45c0cfb6e322e2036225b33fb115733390de8f2c6fab4e46b"
_RACER = b'{"written": "by another run"}\n'


def _overview() -> bytes:
    return mr_overview().replace(_MR_SYMBOL.encode(), _SYMBOL.encode())


def _bars() -> bytes:
    return mr_bars().replace(_MR_SYMBOL.encode(), _SYMBOL.encode())


def _artifact(tmp_path: Path) -> Path:
    path = massive_options._artifact_path(tmp_path / "cache", _TICKER, _START, _END)
    assert _CONTRACT in path.name
    return path


def _key_file(tmp_path: Path) -> Path:
    path = tmp_path / "env"
    path.write_text(f"MASSIVE_API_KEY={_AUTH}\n", encoding="utf-8")
    return path


def _http_error(code: int) -> Callable[[str], Exception]:
    """An HTTP error for the requested URL, with a reason and a header."""
    return lambda url: http_error(url, code, _REASON, header=_HEADER)


def _called(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("the network must not be used")


class _Massive:
    """A fake Massive API. It records each request's URL and answers with the
    next reply: response bytes, or a builder of the error to raise from the
    request's URL. With no reply left, it fails the test. ``slept`` records
    the loader's pacing."""

    def __init__(self, *replies: bytes | Callable[[str], Exception]) -> None:
        self.replies = list(replies)
        self.requested: list[str] = []
        self.slept: list[float] = []

    def __call__(self, request: urllib.request.Request, _timeout: float) -> bytes:
        if not self.replies:
            _called()
        self.requested.append(request.full_url)
        reply = self.replies.pop(0)
        if isinstance(reply, bytes):
            return reply
        raise reply(request.full_url)


def _load(
    tmp_path: Path,
    massive: _Massive,
    key_file: Path | None,
    *,
    opener: massive_options.UrlOpener | None = None,
) -> MassiveOptionCloseSeries | None:
    return load_or_fetch_massive_option_closes(
        underlying=_SYMBOL,
        expiration=_EXPIRATION,
        strike_usd=Decimal("250"),
        right="Call",
        multiplier=Decimal("100"),
        start=_START,
        end=_END,
        cache_dir=tmp_path / "cache",
        fetch=True,
        api_key_file=key_file,
        opener=opener or massive,
        now=_NOW,
        sleep=massive.slept.append,
    )


def _refusal(
    tmp_path: Path,
    massive: _Massive,
    key_file: Path | None,
    *,
    opener: massive_options.UrlOpener | None = None,
) -> BaseException:
    with pytest.raises(MassiveOptionPriceError) as refusal:
        _load(tmp_path, massive, key_file, opener=opener)
    return refusal.value


def _assert_refusal(
    error: BaseException,
    message: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    """``error`` is a plain ``MassiveOptionPriceError`` whose one arg is its
    whole ``message``, with no attribute of its own. No marker is anywhere in
    its exception graph; it has no context, and its only cause is the stand-in
    for a ``crash``, which names that class and a line."""
    assert type(error) is MassiveOptionPriceError
    assert error.args == (message,)
    assert vars(error) == {}
    assert str(error) == message
    assert_replaced_unchained(error, crash, markers=markers)


# --- Transport ----------------------------------------------------------------

_HTTP = (*_FETCH, _REASON, _HEADER)
_REQUEST_FAILED = "Massive request failed"
_TRANSPORT_FAILURES = [
    pytest.param(
        _http_error(429), "Massive rate limit reached; rerun later", None, _HTTP, id="http-429"
    ),
    pytest.param(
        _http_error(503), "Massive request failed with HTTP 503", None, _HTTP, id="http-503"
    ),
    pytest.param(
        lambda url: urllib.error.URLError(f"{_REASON} {url}"),
        _REQUEST_FAILED,
        None,
        (*_FETCH, _REASON),
        id="url",
    ),
    pytest.param(
        lambda url: urllib.error.URLError(OSError(errno.ECONNREFUSED, _STRERROR, url)),
        _REQUEST_FAILED,
        None,
        (*_FETCH, _STRERROR),
        id="url-os-reason",
    ),
    pytest.param(
        lambda url: TimeoutError(errno.ETIMEDOUT, _STRERROR, url),
        _REQUEST_FAILED,
        None,
        (*_FETCH, _STRERROR),
        id="timeout",
    ),
    pytest.param(
        lambda url: OSError(errno.ECONNRESET, _STRERROR, url),
        _REQUEST_FAILED,
        None,
        (*_FETCH, _STRERROR),
        id="os-error",
    ),
    pytest.param(
        lambda url: http.client.IncompleteRead(f"{_PARTIAL} {url}".encode()),
        _REQUEST_FAILED,
        http.client.IncompleteRead,
        (*_FETCH, _PARTIAL),
        id="partial-read",
    ),
    pytest.param(
        lambda url: http.client.BadStatusLine(f"{_STATUS_LINE} {url}"),
        _REQUEST_FAILED,
        http.client.BadStatusLine,
        (*_FETCH, _STATUS_LINE),
        id="bad-status-line",
    ),
]


@pytest.mark.parametrize("failing", ["overview", "bars"])
@pytest.mark.parametrize(("failure", "message", "crash", "markers"), _TRANSPORT_FAILURES)
def test_transport_refusal_keeps_no_request_content(
    tmp_path: Path,
    failure: Callable[[str], Exception],
    message: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
    failing: str,
) -> None:
    replies = [failure] if failing == "overview" else [_overview(), failure]
    massive = _Massive(*replies)

    error = _refusal(tmp_path, massive, _key_file(tmp_path))

    _assert_refusal(error, message, crash, markers)
    # One paced request each, no retry, and no artifact.
    assert massive.requested == [_OVERVIEW_URL, _BARS_URL][: len(replies)]
    assert massive.slept == [_INTERVAL] * len(replies)
    assert not (tmp_path / "cache").exists()


def _raise_caller(error: Exception) -> NoReturn:
    raise error


@pytest.mark.parametrize(
    ("caller", "crash"),
    [
        pytest.param(lambda: OSError(errno.EIO, _CALLER), None, id="domain-caller"),
        pytest.param(lambda: LookupError(_CALLER), LookupError, id="crash-caller"),
    ],
)
def test_transport_refusal_keeps_no_caller_exception(
    tmp_path: Path, caller: Callable[[], Exception], crash: type[BaseException] | None
) -> None:
    """Under a caller's handler, the transport error chains the caller's
    exception. A domain one leaves the refusal with no cause; a crash leaves
    only the stand-in naming its class. Neither is the refusal's context."""
    massive = _Massive(lambda url: urllib.error.URLError(f"{_REASON} {url}"))
    key_file = _key_file(tmp_path)
    try:
        _raise_caller(caller())
    except (OSError, LookupError):
        error = _refusal(tmp_path, massive, key_file)

    _assert_refusal(error, _REQUEST_FAILED, crash, (*_FETCH, _REASON, _CALLER))
    assert massive.requested == [_OVERVIEW_URL]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        pytest.param(_http_error(429), "Massive rate limit reached; rerun later", id="http-429"),
        pytest.param(
            lambda url: urllib.error.URLError(f"{_REASON} {url}"), _REQUEST_FAILED, id="url"
        ),
        pytest.param(
            lambda url: http.client.IncompleteRead(f"{_PARTIAL} {url}".encode()),
            _REQUEST_FAILED,
            id="partial-read",
        ),
    ],
)
def test_option_price_audit_reports_the_same_transport_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Callable[[str], Exception],
    message: str,
) -> None:
    """The Robinhood option-price audit still reports the refusal's text."""
    options, _contract = _options(_SYMBOL)
    _key_file(tmp_path)
    massive = _Massive(failure)
    loader = partial(
        load_or_fetch_massive_option_closes, opener=massive, now=_NOW, sleep=massive.slept.append
    )
    monkeypatch.setattr(option_prices, "load_or_fetch_massive_option_closes", loader)

    audit = collect_robinhood_option_price_audit(
        options=options,
        config=_config(),
        config_path=tmp_path / "user.toml",
        input_dir=tmp_path,
        calendar_year=2025,
        fetch=True,
        price_cache_dir=tmp_path / "prices",
    )

    assert audit.errors == (message,)
    assert audit.warnings == ()
    assert audit.series_by_contract == {}
    assert massive.requested == [_OVERVIEW_URL]
    assert massive.slept == [_INTERVAL]
    assert not (tmp_path / "prices").exists()


# --- Publishing the artifact --------------------------------------------------


def test_artifact_published_meanwhile_keeps_no_artifact_name(tmp_path: Path) -> None:
    """Another run publishes the artifact while this one fetches. The real
    hard link then fails with the staging and artifact paths, both named for
    the contract and dates."""
    path = _artifact(tmp_path)
    massive = _Massive(_overview(), _bars())

    def racing(request: urllib.request.Request, timeout: float) -> bytes:
        reply = massive(request, timeout)
        if len(massive.requested) == 2:
            path.parent.mkdir()
            path.write_bytes(_RACER)
        return reply

    error = _refusal(tmp_path, massive, _key_file(tmp_path), opener=racing)

    _assert_refusal(error, "Massive option cache already exists", None, _FETCH)
    # The other run's artifact stays as it wrote it, and the staging file is gone.
    assert list(path.parent.iterdir()) == [path]
    assert path.read_bytes() == _RACER
    assert massive.requested == [_OVERVIEW_URL, _BARS_URL]
    assert massive.slept == [_INTERVAL, _INTERVAL]


# --- Reading the artifact -----------------------------------------------------


def _not_json(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A JSON error keeps the document."""
    path.write_text(_UNPARSABLE, encoding="utf-8")
    return (_DOCUMENT,)


def _not_utf8(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A decode error keeps the file's bytes."""
    path.write_bytes(b'{"' + _BYTES.encode() + b'": "\xff"}')
    return (_BYTES,)


def _directory(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A real ``IsADirectoryError`` keeps the artifact's path."""
    path.mkdir()
    return ()


def _denied(path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Reading the artifact fails with its real path as the file name, as
    ``open`` does."""
    path.write_text("{}\n", encoding="utf-8")
    original = Path.read_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise PermissionError(errno.EACCES, _STRERROR, str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)
    return (_STRERROR,)


def _not_an_object(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Indexing a JSON array by a fixed key: a ``TypeError`` with fixed text."""
    path.write_text("[]\n", encoding="utf-8")
    return ()


def _no_metadata(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A missing fixed key: a ``KeyError`` naming only that key."""
    path.write_text("{}\n", encoding="utf-8")
    return ()


def _snapshot(folder: Path) -> dict[str, bytes | None]:
    """Every entry in ``folder``: a file's bytes, or ``None`` for a folder."""
    return {
        entry.name: None if entry.is_dir() else entry.read_bytes() for entry in folder.iterdir()
    }


@pytest.mark.parametrize(
    ("damage", "crash"),
    [
        pytest.param(_not_json, json.JSONDecodeError, id="document"),
        pytest.param(_not_utf8, UnicodeDecodeError, id="bytes"),
        pytest.param(_directory, None, id="directory"),
        pytest.param(_denied, None, id="denied"),
        pytest.param(_not_an_object, TypeError, id="array"),
        pytest.param(_no_metadata, KeyError, id="no-metadata"),
    ],
)
def test_malformed_artifact_keeps_no_cache_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    crash: type[BaseException] | None,
) -> None:
    path = _artifact(tmp_path)
    path.parent.mkdir()
    markers = damage(path, monkeypatch)
    written = _snapshot(path.parent)
    massive = _Massive()

    error = _refusal(tmp_path, massive, tmp_path / "missing-env")

    _assert_refusal(error, "Massive option cache is malformed", crash, (_CONTRACT, *markers))
    # Refused, not refetched, and left as it was.
    assert massive.slept == []
    assert _snapshot(path.parent) == written


def _resealed(path: Path, fetched_at: str) -> None:
    """The artifact with ``fetched_at`` as its fetch time, hashed again: its
    schema, identity and hash stay valid."""
    artifact = json.loads(path.read_text(encoding="utf-8"))
    metadata = {**artifact["metadata"], "fetched_at": fetched_at}
    artifact["metadata"] = metadata
    artifact["payload_sha256"] = massive_options._payload_hash(
        metadata, artifact["overview_raw"], artifact["bars_raw"]
    )
    path.write_text(json.dumps(artifact), encoding="utf-8")


def test_invalid_cached_fetch_time_keeps_no_cached_text(tmp_path: Path) -> None:
    """``datetime.fromisoformat``'s error quotes the cached fetch time."""
    assert _load(tmp_path, _Massive(_overview(), _bars()), _key_file(tmp_path)) is not None
    path = _artifact(tmp_path)
    _resealed(path, _FETCHED_AT)
    written = path.read_bytes()
    massive = _Massive()

    error = _refusal(tmp_path, massive, None)

    _assert_refusal(
        error, "Massive option cache metadata is invalid", ValueError, (_CONTRACT, _FETCHED_AT)
    )
    assert massive.requested == []
    assert massive.slept == []
    assert path.read_bytes() == written


# --- The API key file -----------------------------------------------------------


def test_undecodable_key_file_keeps_no_key_bytes(tmp_path: Path) -> None:
    """The decode error keeps the key file's bytes. The key file's path is the
    one the user configured."""
    key_file = tmp_path / "env"
    key_file.write_bytes(_UNDECODABLE_KEY)
    massive = _Massive()

    error = _refusal(tmp_path, massive, key_file)

    _assert_refusal(error, "Massive API key file is unavailable", UnicodeDecodeError, (_KEY,))
    # The key is read before any request.
    assert massive.requested == []
    assert massive.slept == []
    assert not (tmp_path / "cache").exists()


def test_malformed_artifact_refuses_before_the_key_file_is_read(tmp_path: Path) -> None:
    """With both a malformed artifact and an undecodable key file, the cache
    refuses first, as before."""
    path = _artifact(tmp_path)
    path.parent.mkdir()
    path.write_text(_UNPARSABLE, encoding="utf-8")
    key_file = tmp_path / "env"
    key_file.write_bytes(_UNDECODABLE_KEY)
    massive = _Massive()

    error = _refusal(tmp_path, massive, key_file)

    _assert_refusal(
        error, "Massive option cache is malformed", json.JSONDecodeError, (_CONTRACT, _DOCUMENT)
    )
    assert massive.requested == []
    assert massive.slept == []


# --- Valid controls -----------------------------------------------------------


def test_valid_fetch_and_reuse_are_unchanged(tmp_path: Path) -> None:
    """A fetch returns the same bars after the same paced requests and
    publishes the same artifact; a later load reuses it with no request."""
    massive = _Massive(_overview(), _bars())

    series = _load(tmp_path, massive, _key_file(tmp_path))

    path = _artifact(tmp_path)
    assert series == MassiveOptionCloseSeries(
        massive_ticker=_TICKER,
        underlying=_SYMBOL,
        expiration=_EXPIRATION,
        strike_usd=Decimal("250"),
        multiplier=Decimal("100"),
        bars={
            date(2025, 1, 15): OptionCloseBar(date(2025, 1, 15), Decimal("1.25"), Decimal(2), 1),
            date(2025, 1, 16): OptionCloseBar(date(2025, 1, 16), Decimal("1.5"), Decimal(3), 2),
        },
        artifact_path=path.resolve(),
        fetched_at=_NOW,
    )
    assert massive.requested == [_OVERVIEW_URL, _BARS_URL]
    assert massive.slept == [_INTERVAL, _INTERVAL]
    assert list(path.parent.iterdir()) == [path]
    published = path.read_bytes()
    assert hashlib.sha256(published).hexdigest() == _ARTIFACT_SHA256

    reuse = _Massive()
    reused = _load(tmp_path, reuse, None)

    assert reused == series
    assert reuse.requested == []
    assert reuse.slept == []
    assert path.read_bytes() == published
