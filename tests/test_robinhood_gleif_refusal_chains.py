"""GLEIF refusals keep no input they replaced (#1224).

A held security's exact-ISIN GLEIF lookup and a held ADR's or ETF's registrant
lookup refuse an unusable cache or a failed fetch with a
``GleifLegalAddressError`` that names only the cache folder and a fixed
problem. On 2c018e3 each refusal kept the error it replaced as a suppressed
context, which could hold the request URL (built from the ISIN or LEI), an HTTP
error's reason and headers, a transport error's text, a response's or cache's
bytes or JSON document, a non-finite operand, or a path through the artifact's
fingerprint folder, whose name is derived from the ISIN or LEI. The refusal now
keeps none of it: it has no context, and its only cause is the content-free
stand-in that :func:`ibkr_fa.exceptions.raise_replacing` leaves when the
replaced error was or chained a crash, so ``IBKR_FA_TRACEBACK=1`` still
re-raises it.

Each refusal graph test checks, in order: the unchanged class, args and complete
message; no marker anywhere in the exception graph (fields, args, ``str``,
``repr``, rendered traces, every cause and context, suppressed or not); then
the chain's shape. Markers stay in module constants and builders, off the line
that calls the code under test, since a rendered traceback prints that line.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import NoReturn

import pytest

import ibkr_fa.robinhood_legal_entity_sources as legal
from ibkr_fa import cli
from ibkr_fa.models.reference_identity import (
    GleifLegalAddressLookup,
    GleifLegalAddressRecord,
    GleifRegistrantRecord,
)
from ibkr_fa.robinhood_legal_entity_sources import (
    GleifLegalAddressError,
    LegalAddressSourceError,
    _artifact_path,
    _registrant_artifact_path,
    _registrant_source_url,
    _source_url,
    load_gleif_registrant_by_lei,
    load_robinhood_legal_entity_evidence,
)
from ibkr_fa.types import Isin
from tests._crash_reports import (
    CONTEXT,
    assert_no_markers,
    assert_plain_refusal,
    raise_context,
)
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import denied, write_undecodable, write_unparsable
from tests._entity_cache import files as _files
from tests._entity_source_texts import GLEIF_HTTP_503
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._entity_transport import RecordingOpener as _FundOpener
from tests._entity_transport import blocked_opener as _blocked
from tests._entity_transport import (  # noqa: F401 - autouse fixture
    close_http_errors as _close_http_errors,
)
from tests._entity_transport import http_error
from tests._no_echo import CUSIP, FILENAME, STRERROR
from tests._robinhood_adr import AdrOpener as _AdrOpener
from tests._robinhood_adr import adr_security as _adr
from tests._robinhood_adr import cached_f6 as _cached_adr
from tests._robinhood_adr import resolve_adr as _resolve_adr
from tests._robinhood_cli import OWNED_FILENAMES as _OWNED
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_fund import FUND_LEI as _FUND_LEI
from tests._robinhood_fund import cached_ncen as _cached_etf
from tests._robinhood_fund import resolve_fund as _resolve_etf
from tests._robinhood_gleif import CUSIP_ISIN as _CUSIP_ISIN
from tests._robinhood_gleif import CUSIP_SECURITY as _CUSIP_SECURITY
from tests._robinhood_gleif import GLEIF_LEI as _LEI
from tests._robinhood_gleif import gleif_security as _security
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_gleif import legal_address_cache_refusal as _cache_refusal
from tests._robinhood_gleif import legal_address_fetch_refusal as _fetch_refusal
from tests._robinhood_gleif import registrant_cache_refusal as _registrant_cache_refusal
from tests._robinhood_gleif import registrant_fetch_refusal as _registrant_fetch_refusal
from tests._robinhood_gleif import registrant_reply as _registrant_response
from tests._robinhood_held_bundle import cache_folder as _cache_folder
from tests._robinhood_held_bundle import held_bundle
from tests._robinhood_toml import robinhood_security_toml

# One neutral marker per kind of replaced content.
_REASON = "Synthetic Transport Reason"
_HEADER = "Synthetic Response Header"
_PARTIAL = "Synthetic Partial Response"
_STATUS_LINE = "Synthetic Status Line"
_RESPONSE_BYTES = "Synthetic Response Bytes"
_RESPONSE_DOCUMENT = "Synthetic Response Document"
_CACHE_BYTES = "Synthetic Cache Bytes"
_CACHE_DOCUMENT = "Synthetic Cache Document"
_NON_FINITE_KEY = "Synthetic Non-Finite Key"
_CLEANUP = "Synthetic Cleanup Failure"
_FETCH_FAILED = "a network or cache-file operation failed"
_NOT_RETRIEVED = "GLEIF legal-address evidence could not be retrieved"


def _no_request(_request: urllib.request.Request, _timeout: float) -> NoReturn:
    raise AssertionError("GLEIF transport must not touch the network")


@pytest.fixture(autouse=True)
def _block_gleif_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the real GLEIF transport closed for every test."""
    monkeypatch.setattr(legal, "_url_open", _no_request)


def _assert_refusal(
    error: BaseException,
    text: str,
    *,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
) -> None:
    """``error`` is exactly ``GleifLegalAddressError(text)`` and replaced the
    error it caught (:func:`assert_plain_refusal`)."""
    assert_plain_refusal(error, GleifLegalAddressError, text, crash=crash, markers=markers)


# --- The two public loaders ----------------------------------------------------


@dataclass(frozen=True)
class _Route:
    """One public GLEIF loader, with what its refusals and cache look like."""

    kind: str  # As the private errors name the source: legal-address or registrant.
    identifier: str  # Part of the request URL; the fingerprint folder is derived from it.
    url: str
    response: bytes
    artifact: Callable[[Path], Path]
    cache_refusal: Callable[..., str]  # (cache_dir, problem, *, fetch)
    fetch_refusal: Callable[[Path, str], str]
    call: Callable[[Path, bool, legal.LegalAddressOpener | None], object]
    outcome: Callable[[object], tuple[object, bool]]  # The evidence and whether it was cached.
    evidence: object  # What ``outcome`` gives as the evidence ``response`` holds.


def _registrant_record(url: str, response: bytes) -> GleifRegistrantRecord:
    """The record ``response`` holds: the defaults of ``issuer_record`` in
    tests/_robinhood_gleif.py, written out here."""
    return GleifRegistrantRecord(
        lei=_LEI,
        legal_name="SAMPLE ISSUER AG INC.",
        address_lines=("C/O SAMPLE REGISTERED AGENT", "SAMPLE AGENT CENTER", "99 SAMPLE ST"),
        city="SAMPLETON",
        region="US-DE",
        country_iso2="US",
        postal_code="99991",
        jurisdiction="US-DE",
        category="GENERAL",
        legal_form_id="XTIQ",
        legal_form_other="",
        entity_status="ACTIVE",
        registration_status="ISSUED",
        registration_last_update="2026-03-10T09:30:00Z",
        has_successor=False,
        golden_copy_publish_date="2026-08-23T00:00:00Z",
        source_url=url,
        source_sha256=hashlib.sha256(response).hexdigest(),
    )


def _isin_outcome(result: object) -> tuple[object, bool]:
    assert isinstance(result, GleifLegalAddressLookup)
    return (result.records, result.failures), result.cache_hits == 1


def _registrant_outcome(result: object) -> tuple[object, bool]:
    assert isinstance(result, tuple)
    record, cached = result
    assert isinstance(cached, bool)
    return record, cached


_ISIN_ROUTE = _Route(
    kind="legal-address",
    identifier=CUSIP,
    url=_source_url(_CUSIP_ISIN),
    response=_response(),
    artifact=lambda cache_dir: _artifact_path(cache_dir, _CUSIP_ISIN),
    cache_refusal=_cache_refusal,
    fetch_refusal=_fetch_refusal,
    call=lambda cache_dir, fetch, opener: load_robinhood_legal_entity_evidence(
        (_CUSIP_SECURITY,), (), cache_dir=cache_dir, fetch=fetch, opener=opener
    ),
    outcome=_isin_outcome,
    # The one record, bound to the held security's ISIN, and no failure.
    evidence=(
        (
            GleifLegalAddressRecord(
                isin=_CUSIP_ISIN,
                **asdict(
                    _registrant_record(
                        "https://api.gleif.org/api/v1/lei-records"
                        f"?filter%5Bisin%5D={_CUSIP_ISIN}&page%5Bsize%5D=2",
                        _response(),
                    )
                ),
            ),
        ),
        {},
    ),
)
_REGISTRANT_ROUTE = _Route(
    kind="registrant",
    identifier=_LEI,
    url=_registrant_source_url(_LEI),
    response=_registrant_response(),
    artifact=lambda cache_dir: _registrant_artifact_path(cache_dir, _LEI),
    cache_refusal=_registrant_cache_refusal,
    fetch_refusal=_registrant_fetch_refusal,
    call=lambda cache_dir, fetch, opener: load_gleif_registrant_by_lei(
        _LEI, cache_dir=cache_dir, fetch=fetch, opener=opener
    ),
    outcome=_registrant_outcome,
    evidence=_registrant_record(
        f"https://api.gleif.org/api/v1/lei-records/{_LEI}", _registrant_response()
    ),
)
_ROUTES = [
    pytest.param(_ISIN_ROUTE, id="exact-isin"),
    pytest.param(_REGISTRANT_ROUTE, id="registrant"),
]


def _refusal(
    route: _Route,
    cache_dir: Path,
    *,
    fetch: bool = True,
    opener: legal.LegalAddressOpener | None = None,
) -> GleifLegalAddressError:
    with pytest.raises(GleifLegalAddressError) as refusal:
        route.call(cache_dir, fetch, opener)
    return refusal.value


def _sealed(route: _Route, cache_dir: Path) -> Path:
    """Fetch and seal a real artifact for ``route``; return its path."""
    route.call(cache_dir, True, _RecordingOpener(route.response))
    path = route.artifact(cache_dir)
    assert path.is_file()
    return path


@pytest.mark.parametrize("route", _ROUTES)
def test_default_transport_fetches_then_reuses_the_sealed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    """The control for every transport fake below: the real default opener
    fetches through ``_url_open``, seals the artifact and reuses it offline."""
    requested: list[str] = []

    def serve(request: urllib.request.Request, _timeout: float) -> tuple[str, bytes]:
        requested.append(request.full_url)
        return request.full_url, route.response

    monkeypatch.setattr(legal, "_url_open", serve)
    fetched, fetched_cached = route.outcome(route.call(tmp_path, True, None))
    sealed = route.artifact(tmp_path).read_bytes()
    monkeypatch.setattr(legal, "_url_open", _no_request)

    cached, cached_cached = route.outcome(route.call(tmp_path, True, None))

    assert requested == [route.url]
    assert fetched == route.evidence
    assert (fetched, fetched_cached) == (cached, False)
    assert cached_cached
    assert route.artifact(tmp_path).read_bytes() == sealed


# --- Transport -------------------------------------------------------------------


def _http_error(code: int) -> Callable[[str], Exception]:
    """An HTTP error for the requested URL, with a reason and a header."""
    return lambda url: http_error(url, code, _REASON, header=_HEADER)


def _transport(error: Callable[[], Exception]) -> Callable[[str], Exception]:
    return lambda _url: error()


# Each failure with the problem it names, the attempts it takes, its markers,
# and whether it keeps the request URL.
_TRANSPORT_FAILURES = [
    pytest.param(
        _http_error(404),
        "GLEIF legal-address request failed with HTTP 404",
        1,
        (_REASON, _HEADER),
        True,
        id="http",
    ),
    pytest.param(
        _http_error(503),
        GLEIF_HTTP_503,
        3,
        (_REASON, _HEADER),
        True,
        id="http-retried",
    ),
    pytest.param(
        _transport(lambda: urllib.error.URLError(_REASON)),
        _NOT_RETRIEVED,
        3,
        (_REASON,),
        False,
        id="url",
    ),
    pytest.param(
        _transport(lambda: urllib.error.URLError(OSError(errno.ECONNREFUSED, STRERROR, FILENAME))),
        _NOT_RETRIEVED,
        3,
        (STRERROR, FILENAME),
        False,
        id="url-os-reason",
    ),
    pytest.param(
        _transport(lambda: http.client.IncompleteRead(_PARTIAL.encode())),
        _NOT_RETRIEVED,
        3,
        (_PARTIAL,),
        False,
        id="partial-read",
    ),
    pytest.param(
        _transport(lambda: http.client.BadStatusLine(_STATUS_LINE)),
        _NOT_RETRIEVED,
        3,
        (_STATUS_LINE,),
        False,
        id="bad-status-line",
    ),
    pytest.param(
        _transport(lambda: TimeoutError(_REASON)),
        _NOT_RETRIEVED,
        3,
        (_REASON,),
        False,
        id="timeout",
    ),
    pytest.param(
        _transport(lambda: OSError(errno.ECONNRESET, STRERROR, FILENAME)),
        _NOT_RETRIEVED,
        3,
        (STRERROR, FILENAME),
        False,
        id="os-error",
    ),
]


def _failing_transport(
    monkeypatch: pytest.MonkeyPatch, failures: Iterator[Callable[[str], Exception]]
) -> tuple[list[str], list[float]]:
    """Make each attempt of the real default opener raise the next failure;
    return the URLs requested and the backoff slept."""
    requested: list[str] = []
    slept: list[float] = []

    def failing(request: urllib.request.Request, _timeout: float) -> NoReturn:
        requested.append(request.full_url)
        raise next(failures)(request.full_url)

    monkeypatch.setattr(legal, "_url_open", failing)
    monkeypatch.setattr(legal, "_fetch_bytes", partial(legal._fetch_bytes, sleep=slept.append))
    return requested, slept


@pytest.mark.parametrize(
    ("failure", "problem", "attempts", "markers", "names_url"), _TRANSPORT_FAILURES
)
@pytest.mark.parametrize("route", _ROUTES)
def test_transport_failure_stays_out_of_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: _Route,
    failure: Callable[[str], Exception],
    problem: str,
    attempts: int,
    markers: tuple[str, ...],
    names_url: bool,
) -> None:
    requested, slept = _failing_transport(monkeypatch, iter([failure] * attempts))

    error = _refusal(route, tmp_path)

    _assert_refusal(
        error,
        route.fetch_refusal(tmp_path, problem),
        crash=LegalAddressSourceError,
        markers=(*markers, route.identifier) if names_url else markers,
    )
    # Retries and backoff are unchanged, and nothing is cached.
    assert requested == [route.url] * attempts
    assert slept == [2.0, 4.0][: attempts - 1]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("route", _ROUTES)
def test_transport_refusal_keeps_nothing_from_earlier_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    failures = [
        _transport(lambda: http.client.IncompleteRead(_PARTIAL.encode())),
        _transport(lambda: urllib.error.URLError(_STATUS_LINE)),
        _http_error(404),
    ]
    requested, slept = _failing_transport(monkeypatch, iter(failures))

    error = _refusal(route, tmp_path)

    # The final attempt decides the refusal, as before.
    _assert_refusal(
        error,
        route.fetch_refusal(tmp_path, "GLEIF legal-address request failed with HTTP 404"),
        crash=LegalAddressSourceError,
        markers=(_PARTIAL, _STATUS_LINE, _REASON, _HEADER, route.identifier),
    )
    assert requested == [route.url] * 3
    assert slept == [2.0, 4.0]


# --- Responses -------------------------------------------------------------------


def _undecodable_response() -> bytes:
    """A decode error keeps the response's bytes."""
    return b'{"' + _RESPONSE_BYTES.encode() + b'": "\xff"}'


def _unparsable_response() -> bytes:
    """A JSON error keeps the response's document."""
    return f'{{"{_RESPONSE_DOCUMENT}": '.encode()


@pytest.mark.parametrize(
    ("response", "marker"),
    [
        pytest.param(_undecodable_response(), _RESPONSE_BYTES, id="bytes"),
        pytest.param(_unparsable_response(), _RESPONSE_DOCUMENT, id="document"),
    ],
)
@pytest.mark.parametrize("route", _ROUTES)
def test_response_content_stays_out_of_the_refusal(
    tmp_path: Path, route: _Route, response: bytes, marker: str
) -> None:
    opener = _RecordingOpener(response)

    error = _refusal(route, tmp_path, opener=opener)

    _assert_refusal(
        error,
        route.fetch_refusal(tmp_path, f"GLEIF {route.kind} response is malformed"),
        crash=LegalAddressSourceError,
        markers=(marker,),
    )
    assert opener.calls == [route.url]
    assert list(tmp_path.iterdir()) == []


# --- Writing the artifact ----------------------------------------------------------

_EXISTS = "GLEIF legal-address cache artifact already exists"
_RACED = b"written by a concurrent run"


class _RacingOpener:
    """Serve the response after writing the artifact, as a concurrent run would."""

    def __init__(self, path: Path, response: bytes) -> None:
        self.path = path
        self.response = response

    def __call__(self, _url: str) -> bytes:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(_RACED)
        return self.response


def _link_fails(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linking the staged file fails, naming both paths as ``os.link`` does."""

    def failing(self: Path, target: Path) -> NoReturn:
        raise OSError(errno.EXDEV, STRERROR, str(target), None, str(self))

    monkeypatch.setattr(Path, "hardlink_to", failing)


def _folder_denied(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating the fingerprint folder fails, naming it as ``os.mkdir`` does."""
    mkdir = Path.mkdir

    def failing(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if self == path.parent:
            raise PermissionError(errno.EACCES, STRERROR, str(self))
        mkdir(self, mode, parents, exist_ok)

    monkeypatch.setattr(Path, "mkdir", failing)


def _failing_cleanup(path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Make removing the staged file fail; return the attempts."""
    attempts: list[Path] = []
    unlink = Path.unlink

    def failing(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(f".{path.name}."):
            attempts.append(self)
            raise PermissionError(errno.EPERM, _CLEANUP, str(self))
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing)
    return attempts


# Each write failure: whether a concurrent run writes the artifact first, any
# other failure, the problem named, the crash replaced, the markers besides the
# fingerprint folder, and the bytes left in that folder (no folder: None).
_WRITE_FAILURES = [
    pytest.param(True, None, _EXISTS, LegalAddressSourceError, (), [_RACED], id="exists"),
    pytest.param(
        False,
        _link_fails,
        "GLEIF legal-address cache artifact could not be written (EXDEV)",
        LegalAddressSourceError,
        (STRERROR,),
        [],
        id="link-fails",
    ),
    pytest.param(
        False,
        _folder_denied,
        f"{_FETCH_FAILED} (EACCES)",
        None,
        (STRERROR,),
        None,
        id="folder-denied",
    ),
]


@pytest.mark.parametrize(("race", "fail", "problem", "crash", "markers", "left"), _WRITE_FAILURES)
@pytest.mark.parametrize("route", _ROUTES)
def test_write_failure_stays_out_of_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: _Route,
    race: bool,
    fail: Callable[[Path, pytest.MonkeyPatch], None] | None,
    problem: str,
    crash: type[BaseException] | None,
    markers: tuple[str, ...],
    left: list[bytes] | None,
) -> None:
    path = route.artifact(tmp_path)
    opener = _RacingOpener(path, route.response) if race else _RecordingOpener(route.response)
    if fail is not None:
        fail(path, monkeypatch)

    error = _refusal(route, tmp_path, opener=opener)

    _assert_refusal(
        error,
        route.fetch_refusal(tmp_path, problem),
        crash=crash,
        markers=(*markers, path.parent.name),
    )
    # The staged file is removed, and an artifact already there is kept.
    if left is None:
        assert list(tmp_path.iterdir()) == []
    else:
        assert [item.read_bytes() for item in path.parent.iterdir()] == left


@pytest.mark.parametrize(
    ("race", "fail", "markers"),
    [
        pytest.param(True, None, (), id="exists"),
        pytest.param(False, _link_fails, (STRERROR,), id="link-fails"),
    ],
)
@pytest.mark.parametrize("route", _ROUTES)
def test_failed_cleanup_still_decides_the_write_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: _Route,
    race: bool,
    fail: Callable[[Path, pytest.MonkeyPatch], None] | None,
    markers: tuple[str, ...],
) -> None:
    path = route.artifact(tmp_path)
    opener = _RacingOpener(path, route.response) if race else _RecordingOpener(route.response)
    if fail is not None:
        fail(path, monkeypatch)
    attempts = _failing_cleanup(path, monkeypatch)

    error = _refusal(route, tmp_path, opener=opener)

    # As before, the failed cleanup, attempted once, names the problem; the
    # link failure it replaced was the crash.
    _assert_refusal(
        error,
        route.fetch_refusal(tmp_path, f"{_FETCH_FAILED} (EPERM)"),
        crash=LegalAddressSourceError,
        markers=(*markers, _CLEANUP, path.parent.name),
    )
    assert len(attempts) == 1
    assert attempts[0].parent == path.parent
    assert sorted(item.name for item in path.parent.iterdir()) == sorted(
        [attempts[0].name, *([path.name] if race else [])]
    )
    if race:
        assert path.read_bytes() == _RACED


# --- Reading the artifact ------------------------------------------------------------


def _cache_bytes(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    return write_undecodable(path, _CACHE_BYTES)


def _cache_document(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    return write_unparsable(path, _CACHE_DOCUMENT)


def _non_finite(
    literal: str, operand: str
) -> Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]]:
    """The hash's encoder names the non-finite operand it refused, and on
    Python 3.14 the key it sits under."""

    def damage(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
        path.write_text(
            f'{{"payload_sha256": "x", "{_NON_FINITE_KEY}": {literal}}}', encoding="utf-8"
        )
        return (f"JSON compliant: {operand}", _NON_FINITE_KEY)

    return damage


def _dangling(path: Path, _monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """A real ``FileNotFoundError`` keeps the artifact's path."""
    _dangle(path)
    return (path.parent.name,)


_denied = partial(denied, strerror=STRERROR)


_UNREADABLE = "GLEIF {kind} cache artifact is not valid UTF-8 JSON"
# Each damage with the problem it names and the crash it replaced.
_CACHE_DAMAGE = [
    pytest.param(_cache_bytes, _UNREADABLE, LegalAddressSourceError, id="bytes"),
    pytest.param(_cache_document, _UNREADABLE, LegalAddressSourceError, id="document"),
    pytest.param(_non_finite("NaN", "nan"), "it is malformed", ValueError, id="nan"),
    pytest.param(_non_finite("Infinity", "inf"), "it is malformed", ValueError, id="inf"),
    pytest.param(_non_finite("-Infinity", "-inf"), "it is malformed", ValueError, id="-inf"),
    pytest.param(
        _dangling,
        "GLEIF {kind} cache artifact could not be read (ENOENT)",
        LegalAddressSourceError,
        id="dangling",
    ),
    pytest.param(
        _denied("read_text"),
        "GLEIF {kind} cache artifact could not be read (EACCES)",
        LegalAddressSourceError,
        id="read",
    ),
    # A filesystem error the loader catches itself is no crash, as before.
    pytest.param(_denied("lstat"), "could not inspect it (EACCES)", None, id="lstat"),
]


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(("damage", "problem", "crash"), _CACHE_DAMAGE)
@pytest.mark.parametrize("route", _ROUTES)
def test_cache_content_stays_out_of_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: _Route,
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    problem: str,
    crash: type[BaseException] | None,
    fetch: bool,
) -> None:
    path = _sealed(route, tmp_path)
    markers = damage(path, monkeypatch)
    written = _files(tmp_path)

    error = _refusal(route, tmp_path, fetch=fetch, opener=_blocked)

    _assert_refusal(
        error,
        route.cache_refusal(tmp_path, problem.format(kind=route.kind), fetch=fetch),
        crash=crash,
        markers=markers,
    )
    # Neither a cache miss nor a fetch, and the artifact is left as it was.
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("document_first", [True, False], ids=["document-first", "lstat-first"])
def test_first_unusable_cache_by_isin_is_the_one_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document_first: bool, fetch: bool
) -> None:
    """Of two held securities whose caches fail in different ways, the one
    whose ISIN sorts first is refused, as before."""
    securities = (_security(), _CUSIP_SECURITY)
    load_robinhood_legal_entity_evidence(
        securities, (), cache_dir=tmp_path, fetch=True, opener=_RecordingOpener(_response())
    )
    first, second = (
        _artifact_path(tmp_path, isin)
        for isin in sorted(item.instrument.isin for item in securities)
    )
    unparsable, denied = (first, second) if document_first else (second, first)
    document = _cache_document(unparsable, monkeypatch)
    filesystem = _denied("lstat")(denied, monkeypatch)
    written = _files(tmp_path)

    with pytest.raises(GleifLegalAddressError) as refusal:
        load_robinhood_legal_entity_evidence(
            securities, (), cache_dir=tmp_path, fetch=fetch, opener=_blocked
        )

    problem, crash, markers = (
        (_UNREADABLE.format(kind="legal-address"), LegalAddressSourceError, document)
        if document_first
        else ("could not inspect it (EACCES)", None, filesystem)
    )
    _assert_refusal(
        refusal.value, _cache_refusal(tmp_path, problem, fetch=fetch), crash=crash, markers=markers
    )
    assert _files(tmp_path) == written


@pytest.mark.parametrize("route", _ROUTES)
def test_refusal_under_a_caller_handler_links_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    """A loader called while its caller handles an exception doesn't link that
    exception as the refusal's context either."""
    markers = _cache_document(_sealed(route, tmp_path), monkeypatch)

    try:
        raise_context()
    except ValueError:
        error = _refusal(route, tmp_path, fetch=False, opener=_blocked)

    _assert_refusal(
        error,
        route.cache_refusal(tmp_path, _UNREADABLE.format(kind=route.kind), fetch=False),
        crash=LegalAddressSourceError,
        markers=(*markers, CONTEXT),
    )


# --- The ADR and ETF callers ---------------------------------------------------------


def _adr_registrant(cache_dir: Path) -> Path:
    """Seal a held ADR's F-6 and GLEIF registrant artifacts; return the
    registrant's."""
    _cached_adr(cache_dir)
    [path] = cache_dir.rglob("gleif-registrant.json")
    return path


def _etf_registrant(cache_dir: Path) -> Path:
    """Seal a held ETF's N-CEN selection and GLEIF registrant artifacts;
    return the registrant's."""
    _cached_etf(cache_dir)
    return _registrant_artifact_path(cache_dir, _FUND_LEI)


def _resolve_held_adr(
    cache_dir: Path, *, fetch: bool, gleif_opener: legal.LegalAddressOpener
) -> object:
    sec_opener = _AdrOpener() if fetch else _blocked
    return _resolve_adr(
        _adr(), cache_dir, fetch=fetch, sec_opener=sec_opener, gleif_opener=gleif_opener
    )


def _resolve_held_etf(
    cache_dir: Path, *, fetch: bool, gleif_opener: legal.LegalAddressOpener
) -> object:
    return _resolve_etf(
        cache_dir,
        fetch=fetch,
        sec_user_agent=None,
        sec_opener=_FundOpener({}),
        gleif_opener=gleif_opener,
    )


_CALLERS = [
    pytest.param(_adr_registrant, _resolve_held_adr, id="adr"),
    pytest.param(_etf_registrant, _resolve_held_etf, id="etf"),
]


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(("sealed", "resolve"), _CALLERS)
def test_caller_relays_the_registrant_cache_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sealed: Callable[[Path], Path],
    resolve: Callable[..., object],
    fetch: bool,
) -> None:
    markers = _cache_document(sealed(tmp_path), monkeypatch)
    written = _files(tmp_path)

    with pytest.raises(GleifLegalAddressError) as refusal:
        resolve(tmp_path, fetch=fetch, gleif_opener=_blocked)

    _assert_refusal(
        refusal.value,
        _registrant_cache_refusal(tmp_path, _UNREADABLE.format(kind="registrant"), fetch=fetch),
        crash=LegalAddressSourceError,
        markers=markers,
    )
    assert _files(tmp_path) == written


@pytest.mark.parametrize(("sealed", "resolve"), _CALLERS)
def test_caller_relays_the_registrant_fetch_refusal(
    tmp_path: Path, sealed: Callable[[Path], Path], resolve: Callable[..., object]
) -> None:
    """The caller's own SEC evidence is cached; the registrant's GLEIF
    response is undecodable."""
    path = sealed(tmp_path)
    path.unlink()
    opener = _RecordingOpener(_undecodable_response())

    with pytest.raises(GleifLegalAddressError) as refusal:
        resolve(tmp_path, fetch=True, gleif_opener=opener)

    _assert_refusal(
        refusal.value,
        _registrant_fetch_refusal(tmp_path, "GLEIF registrant response is malformed"),
        crash=LegalAddressSourceError,
        markers=(_RESPONSE_BYTES,),
    )
    assert len(opener.calls) == 1
    assert list(path.parent.iterdir()) == []


# --- The CLI ------------------------------------------------------------------------

_DIRECT_CUSIP = "99972B101"
_DIRECT = _security(isin=Isin("US99972B1017"), cusip=_DIRECT_CUSIP)


def _held_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, adr: bool) -> Path:
    """A stubbed bundle that holds one configured security in 2025: a direct
    common stock, or an ADR. The real loader and presentation caller read its
    sealed GLEIF artifact in the input's default cache folder; returns that
    artifact's path (the exact-ISIN one, or the ADR's registrant)."""
    if adr:
        return _adr_registrant(held_bundle(tmp_path, monkeypatch))
    cache_dir = held_bundle(
        tmp_path, monkeypatch, securities=robinhood_security_toml(), cusip=_DIRECT_CUSIP
    )
    load_robinhood_legal_entity_evidence(
        (_DIRECT,), (), cache_dir=cache_dir, fetch=True, opener=_RecordingOpener(_response())
    )
    return _artifact_path(cache_dir, _DIRECT.instrument.isin)


def _cli_refusal(cache_dir: Path, *, adr: bool, problem: str) -> str:
    """As every run below prints it: with fetching off, as robinhood-options
    always runs and the others with --no-fetch-prices."""
    refusal = _registrant_cache_refusal if adr else _cache_refusal
    return refusal(cache_dir, problem, fetch=False)


@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("command", _OWNED)
@pytest.mark.parametrize("adr", [False, True], ids=["direct", "adr"])
def test_every_command_prints_only_the_gleif_refusal_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adr: bool,
    command: str,
    stale: bool,
) -> None:
    path = _held_bundle(tmp_path, monkeypatch, adr=adr)
    markers = _cache_document(path, monkeypatch)
    if stale:
        for name in _OWNED[command]:
            (tmp_path / "out" / name).write_bytes(b"stale")
    written = _files(_cache_folder(tmp_path))

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    kind = "registrant" if adr else "legal-address"
    assert captured.err == (
        "error: "
        + _cli_refusal(_cache_folder(tmp_path), adr=adr, problem=_UNREADABLE.format(kind=kind))
        + "\n"
    )
    assert_no_markers(captured.err, *markers)
    assert _left(tmp_path) == []
    assert _files(_cache_folder(tmp_path)) == written


@pytest.mark.parametrize(
    ("adr", "damage", "problem", "crash"),
    [
        pytest.param(
            False,
            _denied("read_text"),
            "GLEIF legal-address cache artifact could not be read (EACCES)",
            LegalAddressSourceError,
            id="direct-private-wrapper",
        ),
        pytest.param(
            True,
            _cache_document,
            _UNREADABLE.format(kind="registrant"),
            LegalAddressSourceError,
            id="adr-private-wrapper",
        ),
        pytest.param(
            False, _denied("lstat"), "could not inspect it (EACCES)", None, id="direct-filesystem"
        ),
    ],
)
def test_traceback_mode_classifies_the_gleif_refusal_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adr: bool,
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    problem: str,
    crash: type[BaseException] | None,
) -> None:
    """``IBKR_FA_TRACEBACK=1`` re-raises a domain error that replaced a crash.
    The private ``ValueError`` wrapper counts as one, so a refusal that
    replaced it is still re-raised after cleanup, with nothing of the replaced
    error but its stand-in. A filesystem error the loader caught itself is no
    crash, so that refusal is still printed."""
    path = _held_bundle(tmp_path, monkeypatch, adr=adr)
    markers = damage(path, monkeypatch)
    for name in _OWNED["robinhood-fa"]:
        (tmp_path / "out" / name).write_bytes(b"stale")
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")
    text = _cli_refusal(_cache_folder(tmp_path), adr=adr, problem=problem)

    if crash is None:
        assert _main("robinhood-fa", tmp_path, "--no-fetch-prices") == 4
        captured = capsys.readouterr()
        assert captured.err == f"error: {text}\n"
        assert_no_markers(captured.err, *markers)
    else:
        with pytest.raises(GleifLegalAddressError) as refusal:
            _main("robinhood-fa", tmp_path, "--no-fetch-prices")
        assert cli._holds_crash(refusal.value)
        _assert_refusal(refusal.value, text, crash=crash, markers=markers)
        assert capsys.readouterr().out == ""
    assert _left(tmp_path) == []
