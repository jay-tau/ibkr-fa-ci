"""#1222: identity source refusals keep no input they caught.

The refusal sites covered here replace caught errors through
:func:`ibkr_fa.exceptions.raise_replacing`: they have no context, and their
only cause is a content-free ``ReplacedCrashError`` stand-in when the caught
error was or chained a crash, so ``IBKR_FA_TRACEBACK=1`` still re-raises them.
On 2c018e3 these sites kept the caught error as a cause or suppressed context,
which could hold a transport URL or reason, response or cache bytes, a JSON
document, a source or cached flag, a non-finite operand, a cached timestamp or
a path under a fingerprint folder.

Each refusal test checks, in order: the unchanged class, args and complete
message; no marker anywhere in the exception graph (fields, args, ``str``,
``repr``, rendered traces, every cause and context, suppressed or not); then
the chain's shape. Markers stay in module constants and builders, off the line
that calls the code under test, since a rendered traceback prints that line.
"""

from __future__ import annotations

import errno
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from ibkr_fa import cli
from ibkr_fa.cli import main
from ibkr_fa.models.reference_identity import SEC_SUBMISSION_URL_TEMPLATE
from ibkr_fa.robinhood_identity_sources import (
    IdentitySourceError,
    IdentitySourceOpener,
    SecEntityCacheError,
    _fetch_bytes,
    _parse_nasdaq_traded,
    _parse_sec_tickers,
    _parse_submission,
    _read_source,
    _write_artifact,
    identity_input_fingerprint,
    sec_source_opener,
)
from tests._crash_reports import assert_plain_refusal as _assert_refusal
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import write_undecodable
from tests._entity_source_texts import IDENTITY_CACHE_MALFORMED, NASDAQ_UNAVAILABLE
from tests._entity_transport import (  # noqa: F401 - autouse fixture
    close_http_errors as _close_http_errors,
)
from tests._entity_transport import http_error
from tests._no_echo import FILENAME, STRERROR
from tests._robinhood_identity_sources import IDENTITY_CACHE_INVALID_RECORD as _INVALID_RECORD
from tests._robinhood_identity_sources import IDENTITY_CACHE_NON_FINITE as _NON_FINITE
from tests._robinhood_identity_sources import IDENTITY_CACHE_UNREADABLE as _UNREADABLE
from tests._robinhood_identity_sources import IDENTITY_INPUTS as _INPUTS
from tests._robinhood_identity_sources import MANUAL_IDENTITIES as _MANUAL
from tests._robinhood_identity_sources import NASDAQ_HEADER as _HEADER
from tests._robinhood_identity_sources import NASDAQ_ROWS as _ROWS
from tests._robinhood_identity_sources import NASDAQ_TRAILER as _TRAILER
from tests._robinhood_identity_sources import SEC_CONTACT as _SEC_CONTACT
from tests._robinhood_identity_sources import SNAPSHOTS as _SNAPSHOTS
from tests._robinhood_identity_sources import cache_state as _cache_state
from tests._robinhood_identity_sources import covering_snapshot as _covering
from tests._robinhood_identity_sources import load_post_fy_snapshot as _post_fy
from tests._robinhood_identity_sources import load_snapshot as _load
from tests._robinhood_identity_sources import no_network as _no_network
from tests._robinhood_identity_sources import read_artifact as _artifact
from tests._robinhood_identity_sources import resealed as _resealed
from tests._robinhood_identity_sources import snapshot_refusal as _snapshot_refusal
from tests._robinhood_identity_sources import source_responses as _sources


@pytest.fixture(autouse=True)
def _block_identity_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the real identity transport closed for every test."""

    def _blocked(_request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        raise AssertionError("identity source transport must not touch the network")

    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources._url_open", _blocked)


# --- Transport ----------------------------------------------------------------

_CIK = 9990917
_SUBMISSION_URL = SEC_SUBMISSION_URL_TEMPLATE.format(cik=_CIK)
# The CIK-bearing part of the URL, which says which security is held.
_URL_MARKER = "CIK0009990917"
_REASON = "Synthetic Transport Reason"
_HEADER_MARKER = "Synthetic Response Header"
_PARTIAL = "Synthetic Partial Response"
_STATUS_LINE = "Synthetic Status Line"
_OPENER_TEXT = "Synthetic Opener Text"
_CALLER = "Synthetic Caller Context"
_SUBMISSIONS = "the SEC submissions bulk archive"


def _http_error(code: int) -> urllib.error.HTTPError:
    return http_error(_SUBMISSION_URL, code, _REASON, header=_HEADER_MARKER)


_HTTP_MARKERS = (_URL_MARKER, _REASON, _HEADER_MARKER)
_TRANSPORT_FAILURES = [
    pytest.param(
        lambda: _http_error(403), "request failed with HTTP 403", 1, None, _HTTP_MARKERS, id="http"
    ),
    pytest.param(
        lambda: _http_error(503),
        "request failed with HTTP 503",
        3,
        None,
        _HTTP_MARKERS,
        id="http-retried",
    ),
    pytest.param(
        lambda: urllib.error.URLError(_REASON),
        "could not be retrieved",
        3,
        None,
        (_REASON,),
        id="url",
    ),
    pytest.param(
        lambda: urllib.error.URLError(OSError(errno.ECONNREFUSED, STRERROR, FILENAME)),
        "could not be retrieved",
        3,
        None,
        (STRERROR, FILENAME),
        id="url-os-reason",
    ),
    pytest.param(
        lambda: http.client.IncompleteRead(_PARTIAL.encode()),
        "could not be retrieved",
        3,
        http.client.IncompleteRead,
        (_PARTIAL,),
        id="partial-read",
    ),
    pytest.param(
        lambda: http.client.BadStatusLine(_STATUS_LINE),
        "could not be retrieved",
        3,
        http.client.BadStatusLine,
        (_STATUS_LINE,),
        id="bad-status-line",
    ),
    pytest.param(
        lambda: TimeoutError(_REASON), "could not be retrieved", 3, None, (_REASON,), id="timeout"
    ),
    pytest.param(
        lambda: OSError(errno.ECONNRESET, STRERROR, FILENAME),
        "could not be retrieved",
        3,
        None,
        (STRERROR, FILENAME),
        id="os-error",
    ),
]


@pytest.mark.parametrize(
    ("failure", "problem", "attempts", "crash", "markers"), _TRANSPORT_FAILURES
)
def test_transport_refusal_replaces_the_transport_error(
    failure: Callable[[], Exception],
    problem: str,
    attempts: int,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    requested: list[str] = []
    slept: list[float] = []

    def failing(request: urllib.request.Request, _timeout: float) -> NoReturn:
        requested.append(request.full_url)
        raise failure()

    with pytest.raises(IdentitySourceError) as refusal:
        _fetch_bytes(
            _SUBMISSION_URL, sec_user_agent=_SEC_CONTACT, url_open=failing, sleep=slept.append
        )

    _assert_refusal(
        refusal.value,
        IdentitySourceError,
        f"{_SUBMISSIONS} {problem}",
        crash=crash,
        markers=markers,
    )
    # Retries and backoff are unchanged.
    assert requested == [_SUBMISSION_URL] * attempts
    assert slept == [5.0, 10.0][: attempts - 1]


def test_transport_refusal_keeps_nothing_from_earlier_attempts() -> None:
    failures = iter(
        (
            http.client.IncompleteRead(_PARTIAL.encode()),
            urllib.error.URLError(_REASON),
            _http_error(404),
        )
    )
    slept: list[float] = []

    def failing(_request: urllib.request.Request, _timeout: float) -> NoReturn:
        raise next(failures)

    with pytest.raises(IdentitySourceError) as refusal:
        _fetch_bytes(
            _SUBMISSION_URL, sec_user_agent=_SEC_CONTACT, url_open=failing, sleep=slept.append
        )

    # The final attempt decides the refusal, as before; the crash on the first
    # attempt was retried, so it is no crash of the refusal's.
    _assert_refusal(
        refusal.value,
        IdentitySourceError,
        f"{_SUBMISSIONS} request failed with HTTP 404",
        crash=None,
        markers=(*_HTTP_MARKERS, _PARTIAL),
    )
    assert slept == [5.0, 10.0]


def _raise_caller_context() -> NoReturn:
    raise LookupError(_CALLER)


def test_transport_refusal_keeps_no_caller_exception() -> None:
    """A transport error raised while a caller handles an exception chains it,
    which already made the CLI treat the refusal as replacing a crash. That
    classification stays, through a stand-in describing the caller's crash without its message."""

    def failing(_request: urllib.request.Request, _timeout: float) -> NoReturn:
        raise _http_error(403)

    try:
        _raise_caller_context()
    except LookupError:
        with pytest.raises(IdentitySourceError) as refusal:
            _fetch_bytes(_SUBMISSION_URL, sec_user_agent=_SEC_CONTACT, url_open=failing)

    _assert_refusal(
        refusal.value,
        IdentitySourceError,
        f"{_SUBMISSIONS} request failed with HTTP 403",
        crash=LookupError,
        markers=(*_HTTP_MARKERS, _CALLER),
    )


def test_sec_opener_refusal_replaces_the_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The transport the ADR and fund loaders build through ``sec_source_opener``."""

    def failing(_request: urllib.request.Request, _timeout: float) -> NoReturn:
        raise http.client.IncompleteRead(_PARTIAL.encode())

    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources._url_open", failing)
    monkeypatch.setattr("ibkr_fa.robinhood_identity_sources.time.sleep", lambda _seconds: None)
    opener = sec_source_opener(_SEC_CONTACT)

    with pytest.raises(IdentitySourceError) as refusal:
        opener(_SUBMISSION_URL)

    _assert_refusal(
        refusal.value,
        IdentitySourceError,
        f"{_SUBMISSIONS} could not be retrieved",
        crash=http.client.IncompleteRead,
        markers=(_PARTIAL,),
    )


_OPENER_FAILURES = [
    pytest.param(lambda: urllib.error.URLError(_OPENER_TEXT), None, (_OPENER_TEXT,), id="url"),
    pytest.param(
        lambda: http.client.BadStatusLine(_OPENER_TEXT),
        http.client.BadStatusLine,
        (_OPENER_TEXT,),
        id="http",
    ),
    pytest.param(lambda: TimeoutError(_OPENER_TEXT), None, (_OPENER_TEXT,), id="timeout"),
    pytest.param(
        lambda: OSError(errno.EIO, _OPENER_TEXT, FILENAME),
        None,
        (_OPENER_TEXT, FILENAME),
        id="os-error",
    ),
    pytest.param(lambda: ValueError(_OPENER_TEXT), ValueError, (_OPENER_TEXT,), id="value-error"),
]


@pytest.mark.parametrize(("failure", "crash", "markers"), _OPENER_FAILURES)
def test_opener_refusal_replaces_the_opener_error(
    failure: Callable[[], Exception], crash: type[BaseException] | None, markers: tuple[str, ...]
) -> None:
    def failing(_url: str) -> NoReturn:
        raise failure()

    with pytest.raises(IdentitySourceError) as refusal:
        _read_source(failing, _SUBMISSION_URL)

    _assert_refusal(
        refusal.value,
        IdentitySourceError,
        f"{_SUBMISSIONS} could not be retrieved",
        crash=crash,
        markers=markers,
    )


@pytest.mark.parametrize(("failure", "_crash", "_markers"), _OPENER_FAILURES)
def test_opener_failure_still_becomes_the_same_audit_failure(
    tmp_path: Path, failure: Callable[[], Exception], _crash: object, _markers: object
) -> None:
    def failing(_url: str) -> NoReturn:
        raise failure()

    bundle, audit, _opener = _load(tmp_path, opener=failing)

    assert bundle is None
    assert audit.failure == NASDAQ_UNAVAILABLE
    assert audit.artifact_path is None


# --- Source parsing -----------------------------------------------------------

_SOURCE_BYTES = "Synthetic Source Bytes"
_SOURCE_FLAG = "SYNTHETIC SOURCE FLAG"
_SOURCE_DOCUMENT = "Synthetic Source Document"
_NASDAQ = "the Nasdaq Trader traded-symbol directory"


def _nasdaq_with_row(row: str | bytes) -> bytes:
    encoded = row if isinstance(row, bytes) else row.encode()
    return f"{_HEADER}\r\n".encode() + encoded + f"\r\n{_TRAILER}\r\n".encode()


def _flagged_row() -> str:
    _traded, *rest = _ROWS[0].split("|")
    return "|".join((_SOURCE_FLAG, *rest))


_NOT_UTF8 = f"{_SOURCE_BYTES} ".encode() + b"\xff"
_TRUNCATED_JSON = f'{{"{_SOURCE_DOCUMENT}": '.encode()
_JSON_NOT_UTF8 = f'{{"{_SOURCE_BYTES}": "'.encode() + b'\xff"}'

_SOURCE_FAILURES = [
    pytest.param(
        lambda: _parse_nasdaq_traded(_nasdaq_with_row(_NOT_UTF8)),
        f"{_NASDAQ} is not valid UTF-8",
        UnicodeDecodeError,
        (_SOURCE_BYTES,),
        id="nasdaq-bytes",
    ),
    pytest.param(
        lambda: _parse_nasdaq_traded(_nasdaq_with_row(_flagged_row())),
        f"{_NASDAQ} contains an unexpected flag value",
        ValueError,
        (_SOURCE_FLAG,),
        id="nasdaq-flag",
    ),
    pytest.param(
        lambda: _parse_sec_tickers(_TRUNCATED_JSON, wanted=frozenset()),
        "the SEC ticker/exchange dataset is not valid JSON",
        json.JSONDecodeError,
        (_SOURCE_DOCUMENT,),
        id="tickers-document",
    ),
    pytest.param(
        lambda: _parse_sec_tickers(_JSON_NOT_UTF8, wanted=frozenset()),
        "the SEC ticker/exchange dataset is not valid JSON",
        UnicodeDecodeError,
        (_SOURCE_BYTES,),
        id="tickers-bytes",
    ),
    pytest.param(
        lambda: _parse_submission(_TRUNCATED_JSON, cik=_CIK),
        f"{_SUBMISSIONS} is not valid JSON",
        json.JSONDecodeError,
        (_SOURCE_DOCUMENT,),
        id="submission-document",
    ),
]


@pytest.mark.parametrize(("parse", "text", "crash", "markers"), _SOURCE_FAILURES)
def test_source_refusal_replaces_the_parse_error(
    parse: Callable[[], object],
    text: str,
    crash: type[BaseException],
    markers: tuple[str, ...],
) -> None:
    with pytest.raises(IdentitySourceError) as refusal:
        parse()

    _assert_refusal(refusal.value, IdentitySourceError, text, crash=crash, markers=markers)


# --- Artifact write -----------------------------------------------------------

# Stands for a fingerprint folder: its name is derived from the holdings.
_FINGERPRINT = "5a" * 32
_CLEANUP = "Synthetic Cleanup Failure"
_EXISTS = "the identity source cache artifact already exists"
_NOT_WRITTEN = "the identity source cache artifact could not be written"


def _target(cache_dir: Path) -> Path:
    path = cache_dir / "robinhood-identities" / _FINGERPRINT / "sources.json"
    path.parent.mkdir(parents=True)
    return path


def _existing(path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text("kept", encoding="utf-8")


def _unlinkable(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(self: Path, target: Path) -> NoReturn:
        raise OSError(errno.EXDEV, STRERROR, str(target), None, str(self))

    monkeypatch.setattr(Path, "hardlink_to", failing)


_LINK_FAILURES = [
    pytest.param(_existing, _EXISTS, ["sources.json"], (_FINGERPRINT,), id="exists"),
    pytest.param(_unlinkable, _NOT_WRITTEN, [], (_FINGERPRINT, STRERROR), id="link-fails"),
]


@pytest.mark.parametrize(("fail", "text", "left", "markers"), _LINK_FAILURES)
def test_write_refusal_replaces_the_link_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: Callable[[Path, pytest.MonkeyPatch], None],
    text: str,
    left: list[str],
    markers: tuple[str, ...],
) -> None:
    path = _target(tmp_path)
    fail(path, monkeypatch)

    with pytest.raises(IdentitySourceError) as refusal:
        _write_artifact(path, {"schema": 2})

    _assert_refusal(refusal.value, IdentitySourceError, text, crash=None, markers=markers)
    # The staging file is still removed, and an existing artifact kept.
    assert sorted(item.name for item in path.parent.iterdir()) == left
    if left:
        assert path.read_text(encoding="utf-8") == "kept"


def _failing_cleanup(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Path], PermissionError]:
    """Make removing a staging file fail; return the attempts and the error."""
    attempts: list[Path] = []
    error = PermissionError(errno.EACCES, _CLEANUP)
    unlink = Path.unlink

    def failing(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(".sources.json."):
            attempts.append(self)
            raise error
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing)
    return attempts, error


@pytest.mark.parametrize(("fail", "text", "_left", "markers"), _LINK_FAILURES)
def test_write_refusal_with_a_failed_cleanup_still_gives_way_to_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: Callable[[Path, pytest.MonkeyPatch], None],
    text: str,
    _left: object,
    markers: tuple[str, ...],
) -> None:
    path = _target(tmp_path)
    fail(path, monkeypatch)
    attempts, cleanup_error = _failing_cleanup(monkeypatch)

    with pytest.raises(PermissionError) as raised:
        _write_artifact(path, {"schema": 2})

    # As before, the cleanup failure propagates once removal was attempted,
    # with the refusal, active during cleanup, as its context.
    assert raised.value is cleanup_error
    assert len(attempts) == 1
    refusal = raised.value.__context__
    assert refusal is not None
    _assert_refusal(refusal, IdentitySourceError, text, crash=None, markers=markers)


def _racing_opener(path: Path) -> IdentitySourceOpener:
    """Serve every source, writing ``path`` first as a concurrent run would."""
    responses = _sources()
    last = SEC_SUBMISSION_URL_TEMPLATE.format(cik=9991029)

    def opener(url: str) -> bytes:
        if url == last:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("written by a concurrent run", encoding="utf-8")
        return responses[url]

    return opener


@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleaned", "cleanup-fails"])
@pytest.mark.parametrize("race", [True, False], ids=["exists", "link-fails"])
def test_write_failure_still_becomes_the_same_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: bool, cleanup_fails: bool
) -> None:
    path = tmp_path / "robinhood-identities" / identity_input_fingerprint(_INPUTS, _MANUAL)
    path /= "sources.json"
    opener = _racing_opener(path) if race else None
    if not race:
        _unlinkable(path, monkeypatch)
    if cleanup_fails:
        _failing_cleanup(monkeypatch)

    bundle, audit, _opener = _load(tmp_path, opener=opener)

    assert bundle is None
    if cleanup_fails:
        assert audit.failure == "identity cache I/O failed"
    else:
        assert audit.failure == (_EXISTS if race else _NOT_WRITTEN)
    assert audit.artifact_path == (path if race else None)


# --- Cache and snapshot search ------------------------------------------------

_CACHED_FLAG = "Synthetic Cached Flag"
_CACHE_BYTES = "Synthetic Cache Bytes"
_CACHE_DOCUMENT = "Synthetic Cache Document"
_NON_FINITE_KEY = "Synthetic Non-Finite Key"
# The text a non-finite operand's error prints before the operand itself.
_NON_FINITE_TEXT = "not JSON compliant"
_TIMESTAMP = "Synthetic Cached Timestamp"
# Python 3.14 names an invalid date's year; no traceback line number is this large.
_TIMESTAMP_YEAR = "9123"


def _flagged_record() -> dict[str, str]:
    """A cached Nasdaq record, valid but for its ETF flag."""
    return {
        "symbol": "XQZZ",
        "name": "Sample Issuer ZZ Inc.",
        "listing_exchange": "Q",
        "etf": _CACHED_FLAG,
        "test_issue": "N",
        "next_shares": "N",
    }


def _cache_bytes(path: Path) -> None:
    write_undecodable(path, _CACHE_BYTES)


def _cache_document(path: Path) -> None:
    path.write_text(_CACHE_DOCUMENT, encoding="utf-8")


def _non_finite_key(path: Path) -> None:
    """A verified artifact plus a key holding infinity; the hash is checked after
    re-hashing, which refuses the operand first (Python 3.14 notes the key)."""
    artifact = _artifact(path)
    artifact[_NON_FINITE_KEY] = float("inf")
    path.write_text(json.dumps(artifact), encoding="utf-8")


_CACHE_FAILURES = [
    pytest.param(
        _resealed(lambda artifact: artifact["nasdaq"].append(_flagged_record())),
        _INVALID_RECORD,
        ValueError,
        (_CACHED_FLAG,),
        _INVALID_RECORD,
        id="cached-flag",
    ),
    pytest.param(
        _cache_bytes, _UNREADABLE, UnicodeDecodeError, (_CACHE_BYTES,), _UNREADABLE, id="bytes"
    ),
    pytest.param(
        _cache_document,
        IDENTITY_CACHE_MALFORMED,
        json.JSONDecodeError,
        (_CACHE_DOCUMENT,),
        IDENTITY_CACHE_MALFORMED,
        id="document",
    ),
    pytest.param(
        _non_finite_key,
        _NON_FINITE,
        ValueError,
        (_NON_FINITE_KEY, _NON_FINITE_TEXT),
        _NON_FINITE,
        id="non-finite",
    ),
    pytest.param(
        _resealed(lambda artifact: artifact.update(fetched_at=_TIMESTAMP)),
        _INVALID_RECORD,
        ValueError,
        (_TIMESTAMP,),
        None,
        id="timestamp-text",
    ),
    pytest.param(
        _resealed(
            lambda artifact: artifact.update(fetched_at=f"{_TIMESTAMP_YEAR}-02-30T00:00:00+00:00")
        ),
        _INVALID_RECORD,
        ValueError,
        (_TIMESTAMP_YEAR,),
        None,
        id="timestamp-date",
    ),
]
# The own-artifact lookup sees a dangling link as no artifact and fetches, so
# this one is for the snapshot search only.
_DANGLING = pytest.param(
    _dangle,
    "the identity source cache artifact could not be read (ENOENT)",
    None,
    (),
    None,
    id="dangling-link",
)


@pytest.mark.parametrize("snapshot", _SNAPSHOTS)
@pytest.mark.parametrize(
    ("damage", "problem", "crash", "markers", "_own"), [*_CACHE_FAILURES, _DANGLING]
)
def test_snapshot_refusal_replaces_the_cache_error(
    tmp_path: Path,
    snapshot: Callable[[Path], Path],
    damage: Callable[[Path], object],
    problem: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
    _own: object,
) -> None:
    path = snapshot(tmp_path)
    damage(path)
    written = _cache_state(tmp_path)

    with pytest.raises(SecEntityCacheError) as refusal:
        _post_fy(tmp_path)

    # A fingerprint folder's name is derived from the holdings, so no error
    # may keep a path through it.
    _assert_refusal(
        refusal.value,
        SecEntityCacheError,
        _snapshot_refusal(tmp_path, problem, fetch=False),
        crash=crash,
        markers=(*markers, path.parent.name),
    )
    assert _cache_state(tmp_path) == written


@pytest.mark.parametrize(("damage", "_problem", "_crash", "_markers", "own"), _CACHE_FAILURES)
def test_own_artifact_failure_keeps_its_audit_failure(
    tmp_path: Path,
    damage: Callable[[Path], object],
    _problem: object,
    _crash: object,
    _markers: object,
    own: str | None,
) -> None:
    """``load_identity_sources`` returns the same failure, or still serves an
    artifact whose timestamp only the snapshot search parses."""
    path = _covering(tmp_path)
    damage(path)

    bundle, audit, _blocked = _load(tmp_path, opener=_no_network)

    assert audit.failure == own
    assert (bundle is None) == (own is not None)
    assert audit.artifact_path == path


# --- CLI crash classification -------------------------------------------------

_TRACEBACK = "IBKR_FA_TRACEBACK"
_CLASSIFIED = [
    pytest.param(
        _cache_document,
        IDENTITY_CACHE_MALFORMED,
        json.JSONDecodeError,
        (_CACHE_DOCUMENT,),
        id="crash-classified",
    ),
    pytest.param(
        _dangle,
        "the identity source cache artifact could not be read (ENOENT)",
        None,
        (),
        id="domain-only",
    ),
]


@pytest.mark.parametrize("traceback_flag", [False, True], ids=["default", "traceback-flag"])
@pytest.mark.parametrize(("damage", "problem", "crash", "markers"), _CLASSIFIED)
def test_cli_still_classifies_a_snapshot_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path], object],
    problem: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
    traceback_flag: bool,
) -> None:
    """Under ``IBKR_FA_TRACEBACK=1`` a refusal that replaced a crash still
    re-raises, through its stand-in; one that replaced only a filesystem error
    still prints. Without the flag both print the unchanged refusal."""
    cache_dir = tmp_path / "cache"
    path = _covering(cache_dir)
    damage(path)

    def run_robinhood_fa(**_kwargs: object) -> NoReturn:
        _post_fy(cache_dir)
        raise AssertionError("the snapshot search must refuse")

    monkeypatch.setattr(cli, "run_robinhood_fa", run_robinhood_fa)
    if traceback_flag:
        monkeypatch.setenv(_TRACEBACK, "1")
    else:
        monkeypatch.delenv(_TRACEBACK, raising=False)
    (tmp_path / "out").mkdir()
    argv = [
        *("robinhood-fa", "--input", str(tmp_path / "in"), "--config", str(tmp_path / "user.toml")),
        *("--output-dir", str(tmp_path / "out"), "--financial-year", "2025-26"),
        *("--itr-form", "ITR-2"),
    ]
    refusal = _snapshot_refusal(cache_dir, problem, fetch=False)

    if traceback_flag and crash is not None:
        with pytest.raises(SecEntityCacheError) as raised:
            main(argv)
        _assert_refusal(
            raised.value,
            SecEntityCacheError,
            refusal,
            crash=crash,
            markers=(*markers, path.parent.name),
        )
        return
    assert main(argv) == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"error: {refusal}\n"
