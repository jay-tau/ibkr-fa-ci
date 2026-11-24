"""ADR and ETF entity-cache refusals keep no cached content in their chains (#1223).

A held ADR's Form F-6 cache or a held ETF's N-CEN selection cache refuses with
a ``SecEntityCacheError`` that names only the cache folder and a fixed problem.
The private error it replaces can hold the artifact's path, whose folder name
is a fingerprint of the security's identifiers, the cache's bytes or JSON
document, a non-finite operand or a cached date. The refusal keeps none of it:
it has no context, and its only cause is the content-free stand-in that
:func:`ibkr_fa.exceptions.raise_replacing` leaves, so ``IBKR_FA_TRACEBACK=1``
still re-raises it as an error that replaced a crash.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from ibkr_fa import cli
from ibkr_fa.models.reference_identity import ObservedIdentityInput
from ibkr_fa.robinhood_adr_entity_sources import (
    AdrEntitySourceError,
    load_robinhood_adr_entity_evidence,
)
from ibkr_fa.robinhood_adr_entity_sources import _artifact_path as _f6_path
from ibkr_fa.robinhood_fund_entity_sources import (
    FundEntitySourceError,
    load_robinhood_fund_entity_evidence,
)
from ibkr_fa.robinhood_fund_entity_sources import _artifact_path as _ncen_path
from ibkr_fa.robinhood_identity_sources import SecEntityCacheError
from ibkr_fa.types import Isin, cusip_check_digit, isin_check_digit
from tests._crash_reports import (
    CONTEXT,
    assert_no_markers,
    assert_replaced_unchained,
    raise_context,
)
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import denied, write_undecodable, write_unparsable
from tests._entity_cache import files as _files
from tests._entity_transport import blocked_opener as _blocked
from tests._robinhood_adr import adr_security as _adr
from tests._robinhood_adr import assert_f6_refusal as _assert_f6_refusal
from tests._robinhood_adr import cached_f6 as _cached_adr
from tests._robinhood_adr import resolve_adr as _resolve_adr
from tests._robinhood_cli import OWNED_FILENAMES as _OWNED
from tests._robinhood_cli import outputs_left as _left
from tests._robinhood_cli import robinhood_main as _main
from tests._robinhood_fund import assert_ncen_refusal as _assert_ncen_refusal
from tests._robinhood_fund import cached_ncen as _cached_etf
from tests._robinhood_fund import reseal_ncen_identity as _reseal_identity
from tests._robinhood_fund import resolve_cached_fund as _resolve_etf
from tests._robinhood_held_bundle import cache_folder as _cache_folder
from tests._robinhood_held_bundle import held_bundle
from tests._robinhood_presentation import ETF_FUND as _ETF_FUND
from tests._robinhood_presentation import GLEIF_ZERO_MATCH as _ZERO_MATCH
from tests._robinhood_presentation import etf_filing as _etf_filing
from tests._robinhood_security import robinhood_security

# One neutral marker per kind of cached content.
_STRERROR = "QZ23STRERR"
_BYTES = "QZ23BYTES"
_DOCUMENT = "QZ23DOC"
_REPORT_DATE = "QZ23REPORT"
_FILING_DATE = "QZ23FILING"
_CUTOFF = date(2025, 12, 31)


def _assert_refusal(
    error: BaseException,
    replaced: type[BaseException],
    markers: tuple[str, ...],
) -> None:
    """``error`` is a plain ``SecEntityCacheError`` whose one arg is its whole
    message, with no attribute of its own. No marker is anywhere in its
    exception graph; it has no context, and its only cause is the stand-in for
    the ``replaced`` error, which names that class and a line."""
    assert type(error) is SecEntityCacheError
    assert error.args == (str(error),)
    assert vars(error) == {}
    assert_replaced_unchained(error, replaced, markers=markers)


# --- Damage to one cache artifact -------------------------------------------


def _undecodable(path: Path) -> tuple[str, ...]:
    return write_undecodable(path, _BYTES)


def _unparsable(path: Path) -> tuple[str, ...]:
    return write_unparsable(path, _DOCUMENT)


def _nonfinite(literal: str, operand: str) -> Callable[[Path], tuple[str, ...]]:
    """The hash's encoder names the non-finite operand it refused."""

    def damage(path: Path) -> tuple[str, ...]:
        path.write_text(f'{{"payload_sha256": "x", "unexpected": {literal}}}', encoding="utf-8")
        return (f"JSON compliant: {operand}",)

    return damage


def _dangling(path: Path) -> tuple[str, ...]:
    """A real ``FileNotFoundError`` keeps the artifact's path, whose folder
    name is the fingerprint."""
    _dangle(path)
    return (path.parent.name,)


_denied = partial(denied, strerror=_STRERROR)


_NONFINITE = [
    pytest.param(_nonfinite("NaN", "nan"), "contains a non-finite number", id="nan"),
    pytest.param(_nonfinite("Infinity", "inf"), "contains a non-finite number", id="inf"),
    pytest.param(_nonfinite("-Infinity", "-inf"), "contains a non-finite number", id="-inf"),
]
_CONTENT = [
    pytest.param(_undecodable, "is unreadable", id="bytes"),
    pytest.param(_unparsable, "is unreadable", id="document"),
    *_NONFINITE,
    pytest.param(_dangling, "could not be read (ENOENT)", id="dangling"),
]
_FILESYSTEM = [
    pytest.param(_denied("lstat"), "could not be inspected (EACCES)", id="lstat"),
    pytest.param(_denied("read_text"), "could not be read (EACCES)", id="read"),
]


# --- The ADR Form F-6 cache --------------------------------------------------


def _adr_refusal(tmp_path: Path, *, fetch: bool) -> SecEntityCacheError:
    written = _files(tmp_path)
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_adr(_adr(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    # Neither a cache miss nor a fetch, and the artifact is left as it was.
    assert _files(tmp_path) == written
    return refusal.value


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(("damage", "problem"), _CONTENT)
def test_f6_cache_content_stays_out_of_the_refusal(
    tmp_path: Path,
    damage: Callable[[Path], tuple[str, ...]],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_adr(tmp_path)
    markers = damage(path)

    error = _adr_refusal(tmp_path, fetch=fetch)

    _assert_f6_refusal(str(error), tmp_path, path, f"ADR depositary cache {problem}", fetch=fetch)
    _assert_refusal(error, AdrEntitySourceError, markers)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(("damage", "problem"), _FILESYSTEM)
def test_f6_cache_filesystem_failure_stays_out_of_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_adr(tmp_path)
    markers = damage(path, monkeypatch)

    error = _adr_refusal(tmp_path, fetch=fetch)

    _assert_f6_refusal(str(error), tmp_path, path, f"ADR depositary cache {problem}", fetch=fetch)
    _assert_refusal(error, AdrEntitySourceError, markers)


_SECOND_CUSIP = f"99992E10{cusip_check_digit('99992E10')}"
_SECOND_ISIN = Isin(f"US{_SECOND_CUSIP}{isin_check_digit(f'US{_SECOND_CUSIP}')}")
_SECOND_ADR = robinhood_security(
    symbol="XQAY",
    cusip=_SECOND_CUSIP,
    isin=_SECOND_ISIN,
    listing_exchange="NYSE",
    is_depositary_receipt=True,
    source=(
        "ADS programme Form F-6: "
        "https://www.sec.gov/Archives/edgar/data/9991079/000999100920000062/xqay-f6.htm"
    ),
)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("document_first", [True, False], ids=["document-first", "lstat-first"])
def test_first_unusable_f6_cache_by_isin_is_the_one_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document_first: bool,
    fetch: bool,
) -> None:
    """The second ADR's ISIN sorts first. Each of the two caches fails in its
    own way, and only the first one read reaches the refusal."""
    first = _f6_path(tmp_path, _SECOND_ADR, "Sample Issuer AY", _CUTOFF)
    second = _f6_path(tmp_path, _adr(), "Sample Issuer AX", _CUTOFF)
    for path in (first, second):
        path.parent.mkdir(parents=True)
    unparsable, denied = (first, second) if document_first else (second, first)
    document = _unparsable(unparsable)
    denied.write_text("{}", encoding="utf-8")
    filesystem = _denied("lstat")(denied, monkeypatch)
    written = _files(tmp_path)

    with pytest.raises(SecEntityCacheError) as refusal:
        load_robinhood_adr_entity_evidence(
            (_adr(), _SECOND_ADR),
            (
                ObservedIdentityInput("99992F108", "XQAX", "Sample Issuer AX"),
                ObservedIdentityInput(_SECOND_CUSIP, "XQAY", "Sample Issuer AY"),
            ),
            (),
            cutoff=_CUTOFF,
            cache_dir=tmp_path,
            fetch=fetch,
            sec_user_agent="test@example.com",
            sec_opener=_blocked,
            gleif_opener=_blocked,
        )

    problem, markers = (
        ("is unreadable", document)
        if document_first
        else ("could not be inspected (EACCES)", filesystem)
    )
    _assert_f6_refusal(
        str(refusal.value), tmp_path, first, f"ADR depositary cache {problem}", fetch=fetch
    )
    _assert_refusal(refusal.value, AdrEntitySourceError, markers)
    assert _files(tmp_path) == written


# --- The ETF N-CEN selection cache -------------------------------------------


def _etf_refusal(tmp_path: Path, *, fetch: bool) -> SecEntityCacheError:
    written = _files(tmp_path)
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve_etf(tmp_path, fetch=fetch)
    # Neither a cache miss nor a fetch, and the artifact is left as it was.
    assert _files(tmp_path) == written
    return refusal.value


def _cached_dates(**dates: str) -> Callable[[Path], tuple[str, ...]]:
    """Reseal the cached identity with ``dates``, so the artifact passes every
    check before the date parse; ``date.fromisoformat`` quotes the text."""

    def damage(path: Path) -> tuple[str, ...]:
        _reseal_identity(**dates)(path)
        return tuple(dates.values())[:1]

    return damage


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        *(
            pytest.param(*param.values[:1], f"N-CEN selection cache {param.values[1]}", id=param.id)
            for param in _CONTENT
        ),
        pytest.param(
            _cached_dates(report_date=_REPORT_DATE),
            "SEC N-CEN cached report date is malformed",
            id="report-date",
        ),
        pytest.param(
            _cached_dates(filing_date=_FILING_DATE),
            "SEC N-CEN cached filing date is malformed",
            id="filing-date",
        ),
        # The report date is parsed first.
        pytest.param(
            _cached_dates(report_date=_REPORT_DATE, filing_date=_FILING_DATE),
            "SEC N-CEN cached report date is malformed",
            id="both-dates",
        ),
    ],
)
def test_ncen_cache_content_stays_out_of_the_refusal(
    tmp_path: Path,
    damage: Callable[[Path], tuple[str, ...]],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_etf(tmp_path)
    markers = damage(path)

    error = _etf_refusal(tmp_path, fetch=fetch)

    _assert_ncen_refusal(str(error), tmp_path, path, problem, fetch=fetch, set_contact=True)
    _assert_refusal(error, FundEntitySourceError, markers)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(("damage", "problem"), _FILESYSTEM)
def test_ncen_cache_filesystem_failure_stays_out_of_the_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_etf(tmp_path)
    markers = damage(path, monkeypatch)

    error = _etf_refusal(tmp_path, fetch=fetch)

    _assert_ncen_refusal(
        str(error),
        tmp_path,
        path,
        f"N-CEN selection cache {problem}",
        fetch=fetch,
        set_contact=True,
    )
    _assert_refusal(error, FundEntitySourceError, markers)


def test_presentation_evidence_relays_the_ncen_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production caller, with fetching on, refuses before any request."""
    path = _ncen_path(tmp_path, _ETF_FUND, _CUTOFF)
    path.parent.mkdir(parents=True)
    markers = _unparsable(path)
    written = _files(tmp_path)

    with pytest.raises(SecEntityCacheError) as refusal:
        _etf_filing(
            tmp_path,
            monkeypatch,
            gleif=_ZERO_MATCH,
            fund_loader=partial(
                load_robinhood_fund_entity_evidence,
                sec_opener=_blocked,
                gleif_opener=_blocked,
            ),
        )

    _assert_ncen_refusal(
        str(refusal.value),
        tmp_path,
        path,
        "N-CEN selection cache is unreadable",
        fetch=True,
        set_contact=True,
    )
    _assert_refusal(refusal.value, FundEntitySourceError, markers)
    assert _files(tmp_path) == written


@pytest.mark.parametrize(
    ("cached", "resolve", "assert_message", "problem", "replaced", "set_contact"),
    [
        pytest.param(
            _cached_adr,
            lambda cache: _resolve_adr(
                _adr(), cache, fetch=False, sec_opener=_blocked, gleif_opener=_blocked
            ),
            _assert_f6_refusal,
            "ADR depositary cache is unreadable",
            AdrEntitySourceError,
            False,
            id="adr",
        ),
        pytest.param(
            _cached_etf,
            lambda cache: _resolve_etf(cache, fetch=False),
            _assert_ncen_refusal,
            "N-CEN selection cache is unreadable",
            FundEntitySourceError,
            True,
            id="etf",
        ),
    ],
)
def test_refusal_under_a_caller_handler_links_nothing(
    tmp_path: Path,
    cached: Callable[[Path], Path],
    resolve: Callable[[Path], object],
    assert_message: Callable[..., None],
    problem: str,
    replaced: type[BaseException],
    set_contact: bool,
) -> None:
    """A loader called while its caller handles an exception doesn't link that
    exception as the refusal's context either."""
    path = cached(tmp_path)
    markers = _unparsable(path)

    try:
        raise_context()
    except ValueError:
        with pytest.raises(SecEntityCacheError) as refusal:
            resolve(tmp_path)

    assert_message(
        str(refusal.value), tmp_path, path, problem, fetch=False, set_contact=set_contact
    )
    _assert_refusal(refusal.value, replaced, (*markers, CONTEXT))


# --- The CLI ------------------------------------------------------------------


def _held_adr_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, parent_fields: str = ""
) -> Path:
    """A stubbed bundle that holds the configured ADR in 2025. The real loader
    and presentation caller read its F-6 cache in the input's default cache
    folder; returns the artifact's path there. ``parent_fields`` go in
    ``[robinhood]``."""
    cache_dir = held_bundle(tmp_path, monkeypatch, parent_fields=parent_fields)
    path = _f6_path(cache_dir, _adr(), "Sample Issuer AX", _CUTOFF)
    path.parent.mkdir(parents=True)
    return path


@pytest.mark.parametrize(
    ("parent_fields", "set_contact"),
    [
        pytest.param("", True, id="no-contact"),
        pytest.param('sec_user_agent = "Tax Payer taxpayer@example.com"\n', False, id="contact"),
    ],
)
@pytest.mark.parametrize("stale", [False, True], ids=["fresh", "stale"])
@pytest.mark.parametrize("command", _OWNED)
def test_every_command_prints_only_the_f6_refusal_and_leaves_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    stale: bool,
    parent_fields: str,
    set_contact: bool,
) -> None:
    """#1526: the refusal's recovery step names robinhood.sec_user_agent only
    when user.toml has no usable contact."""
    path = _held_adr_bundle(tmp_path, monkeypatch, parent_fields=parent_fields)
    markers = _unparsable(path)
    if stale:
        for name in _OWNED[command]:
            (tmp_path / "out" / name).write_bytes(b"stale")
    written = _files(_cache_folder(tmp_path))

    # robinhood-options fetches no prices, so it has no such flag.
    no_fetch = () if command == "robinhood-options" else ("--no-fetch-prices",)
    assert _main(command, tmp_path, *no_fetch) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert captured.err.endswith("\n")
    _assert_f6_refusal(
        captured.err[len("error: ") : -1],
        _cache_folder(tmp_path),
        path,
        "ADR depositary cache is unreadable",
        fetch=False,
        set_contact=set_contact,
    )
    assert_no_markers(captured.err, *markers, "taxpayer@example.com")
    assert _left(tmp_path) == []
    assert _files(_cache_folder(tmp_path)) == written


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        pytest.param(
            lambda path, _monkeypatch: _unparsable(path), "is unreadable", id="private-wrapper"
        ),
        pytest.param(_denied("lstat"), "could not be inspected (EACCES)", id="filesystem"),
    ],
)
def test_traceback_mode_still_reraises_the_f6_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    damage: Callable[[Path, pytest.MonkeyPatch], tuple[str, ...]],
    problem: str,
) -> None:
    """``IBKR_FA_TRACEBACK=1`` re-raises a domain error that replaced a crash.
    The private ``ValueError`` wrapper counts as one, whether it wrapped a
    parse failure or a filesystem error, so the refusal is still re-raised,
    after cleanup, with nothing of the replaced error but its stand-in."""
    path = _held_adr_bundle(tmp_path, monkeypatch)
    path.write_text("{}", encoding="utf-8")
    markers = damage(path, monkeypatch)
    for name in _OWNED["robinhood-fa"]:
        (tmp_path / "out" / name).write_bytes(b"stale")
    monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    with pytest.raises(SecEntityCacheError) as refusal:
        _main("robinhood-fa", tmp_path, "--no-fetch-prices")

    assert cli._holds_crash(refusal.value)
    _assert_f6_refusal(
        str(refusal.value),
        _cache_folder(tmp_path),
        path,
        f"ADR depositary cache {problem}",
        fetch=False,
        set_contact=True,
    )
    _assert_refusal(refusal.value, AdrEntitySourceError, markers)
    assert capsys.readouterr().out == ""
    assert _left(tmp_path) == []
