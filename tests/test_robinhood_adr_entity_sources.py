from __future__ import annotations

import errno
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ibkr_fa.compute.robinhood_entity_overrides import RobinhoodAdrEntityAudit
from ibkr_fa.robinhood_adr_entity_sources import AdrEntitySourceError
from ibkr_fa.robinhood_identity_sources import SecEntityCacheError
from ibkr_fa.robinhood_legal_entity_sources import GleifLegalAddressError, _registrant_artifact_path
from tests._crash_reports import assert_replaced_unchained
from tests._deep_json import DEEP_JSON
from tests._entity_cache import dangle as _dangle
from tests._entity_cache import files as _files
from tests._entity_cache import reseal_file as _reseal_artifact
from tests._entity_transport import blocked_opener as _blocked
from tests._no_echo import FILENAME, STRERROR
from tests._robinhood_adr import ADR_DEPOSITARY_LEI as _LEI
from tests._robinhood_adr import ADR_ISIN as _ISIN
from tests._robinhood_adr import AdrOpener as _Opener
from tests._robinhood_adr import adr_security as _security
from tests._robinhood_adr import assert_f6_refusal as _assert_f6_refusal
from tests._robinhood_adr import cached_f6 as _cached_f6
from tests._robinhood_adr import depositary_gleif_reply as _gleif_direct
from tests._robinhood_adr import resolve_adr as _resolve
from tests._robinhood_gleif import exact_pattern as _exactly
from tests._robinhood_gleif import registrant_cache_refusal as _registrant_cache_refusal
from tests._robinhood_gleif import registrant_raw_type_refusal as _registrant_raw_type_refusal
from tests._robinhood_gleif import reseal_record as _reseal


def test_exact_f6_depositary_resolves_name_address_and_legal_record(
    tmp_path: Path,
) -> None:
    opener = _Opener()

    overrides, records, audit = _resolve(
        _security(),
        tmp_path,
        fetch=True,
        sec_opener=opener,
        gleif_opener=opener,
    )

    assert overrides[0].name == "Citibank, National Association"
    assert overrides[0].address == "5800 S Corporate Place, Sioux Falls, US-SD, US"
    assert overrides[0].zip_code == "57108"
    assert records[0].isin == _ISIN
    assert records[0].legal_form_id == "62VJ"
    assert audit == RobinhoodAdrEntityAudit(1, 1, 0, 0)

    cached = _resolve(
        _security(),
        tmp_path,
        fetch=False,
        sec_opener=_blocked,
        gleif_opener=_blocked,
    )

    assert cached[:2] == (overrides, records)
    assert cached[2] == RobinhoodAdrEntityAudit(1, 1, 1, 1)


def test_mismatched_f6_issuer_stays_manual(tmp_path: Path) -> None:
    opener = _Opener(issuer="Another Company Limited")

    overrides, records, audit = _resolve(
        _security(),
        tmp_path,
        fetch=True,
        sec_opener=opener,
        gleif_opener=opener,
    )

    assert overrides == records == ()
    assert audit.failure_counts == (("f6-source-failure", 1),)
    assert not any("api.gleif.org" in url for url in opener.calls)


def test_depositary_naming_its_successor_only_by_name_stays_manual(tmp_path: Path) -> None:
    opener = _Opener(gleif=_gleif_direct(successor_name="Successor Corp"))

    # #829: on 97194f8 the depositary's name and address were filed.
    overrides, records, audit = _resolve(
        _security(), tmp_path, fetch=True, sec_opener=opener, gleif_opener=opener
    )

    assert overrides == records == ()
    assert audit.failure_counts == (("gleif-lifecycle", 1),)


def test_non_adr_security_is_ignored(tmp_path: Path) -> None:
    result = _resolve(
        _security(depositary=False),
        tmp_path,
        fetch=False,
        sec_opener=_blocked,
        gleif_opener=_blocked,
    )

    assert result == ((), (), RobinhoodAdrEntityAudit(0, 0, 0, 0))


def test_depositary_registrant_with_an_unexpected_raw_type_refuses(tmp_path: Path) -> None:
    payload = json.loads(_gleif_direct())
    payload["data"]["attributes"]["entity"]["legalAddress"]["region"] = 123
    opener = _Opener(gleif=json.dumps(payload).encode())

    # #821: on 59d949e the depositary's address was filed without its region.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_raw_type_refusal(tmp_path, "data.attributes.entity.legalAddress.region")
        ),
    ):
        _resolve(_security(), tmp_path, fetch=True, sec_opener=opener, gleif_opener=opener)
    assert not list(tmp_path.rglob("gleif-registrant.json"))


@pytest.mark.parametrize("fetch", [True, False])
def test_resealed_depositary_registrant_cache_refuses(tmp_path: Path, fetch: bool) -> None:
    opener = _Opener()
    _resolve(_security(), tmp_path, fetch=True, sec_opener=opener, gleif_opener=opener)
    path = _registrant_artifact_path(tmp_path, _LEI)
    _reseal(path, lambda record: record.update(region=123))
    written = _files(tmp_path)

    # #826: on e16b0be the depositary's address was filed without its region.
    with pytest.raises(
        GleifLegalAddressError,
        match=_exactly(
            _registrant_cache_refusal(
                tmp_path,
                "GLEIF registrant cache field record.region has an unexpected JSON type",
                fetch=fetch,
            )
        ),
    ):
        _resolve(_security(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    assert _files(tmp_path) == written


def test_cache_only_run_without_the_registrant_artifact_counts_a_gleif_miss(
    tmp_path: Path,
) -> None:
    opener = _Opener()
    _resolve(_security(), tmp_path, fetch=True, sec_opener=opener, gleif_opener=opener)
    _registrant_artifact_path(tmp_path, _LEI).unlink()

    overrides, records, audit = _resolve(
        _security(), tmp_path, fetch=False, sec_opener=_blocked, gleif_opener=_blocked
    )

    assert overrides == records == ()
    assert audit.failure_counts == (("gleif-cache-miss", 1),)


def _tamper(path: Path) -> None:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["issuer_name"] = "Another Company Limited"
    path.write_text(json.dumps(artifact), encoding="utf-8")


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        pytest.param(_tamper, "ADR depositary cache is malformed", id="integrity"),
        pytest.param(
            lambda path: path.write_bytes(b"not json"),
            "ADR depositary cache is unreadable",
            id="not-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(DEEP_JSON),
            "ADR depositary cache is nested too deeply",
            id="deep-json",
        ),
        pytest.param(
            lambda path: path.write_bytes(b'{"payload_sha256":"x","unexpected":NaN}'),
            "ADR depositary cache contains a non-finite number",
            id="nonfinite-number",
        ),
        pytest.param(_dangle, "ADR depositary cache could not be read (ENOENT)", id="dangling"),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(schema=True)),
            "ADR depositary cache is malformed",
            id="schema-true",
        ),
        pytest.param(
            lambda path: _reseal_artifact(path, lambda artifact: artifact.update(schema=1.0)),
            "ADR depositary cache is malformed",
            id="schema-float",
        ),
        pytest.param(
            lambda path: _reseal_artifact(
                path, lambda artifact: artifact.update(fingerprint="0" * 64)
            ),
            "ADR depositary cache is malformed",
            id="fingerprint",
        ),
    ],
)
def test_unusable_f6_cache_refuses_without_fetching(
    tmp_path: Path,
    damage: Callable[[Path], object],
    problem: str,
    fetch: bool,
) -> None:
    path = _cached_f6(tmp_path)
    damage(path)
    written = _files(tmp_path)

    # #846: on 24a8caf each counted as f6-cache-invalid, was treated as missing,
    # escaped untyped, or (schema) was read as a valid cache hit.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve(_security(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    _assert_f6_refusal(str(refusal.value), tmp_path, path, problem, fetch=fetch)
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
def test_f6_cache_inspection_failure_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch: bool,
) -> None:
    path = _cached_f6(tmp_path)
    written = _files(tmp_path)
    lstat = Path.lstat

    def inaccessible(self: Path):
        if self == path:
            raise PermissionError("cache inspection denied")
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", inaccessible)

    # #846: on 24a8caf the cache was never inspected with lstat and was read.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve(_security(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    _assert_f6_refusal(
        str(refusal.value),
        tmp_path,
        path,
        "ADR depositary cache could not be inspected (unknown error)",
        fetch=fetch,
    )
    assert _files(tmp_path) == written


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("method", "failure"),
    [
        pytest.param("lstat", "could not be inspected", id="lstat"),
        pytest.param("read_text", "could not be read", id="read"),
    ],
)
@pytest.mark.parametrize(
    ("code", "name"),
    [
        pytest.param(errno.EACCES, "EACCES", id="eacces"),
        pytest.param(None, "unknown error", id="no-errno"),
        pytest.param(max(errno.errorcode) + 1, "unknown error", id="unnamed-errno"),
    ],
)
def test_f6_cache_os_error_prints_only_its_errno_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    failure: str,
    code: int | None,
    name: str,
    fetch: bool,
) -> None:
    path = _cached_f6(tmp_path)
    written = _files(tmp_path)
    original = getattr(Path, method)

    def failing(self: Path, *args: object, **kwargs: object):
        if self == path:
            raise OSError(code, STRERROR, FILENAME)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, failing)

    # #1071: on a43eebd the refusal printed the ISIN and the artifact's path,
    # dropped the errno, and chained the OSError.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve(_security(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    message = str(refusal.value)
    _assert_f6_refusal(
        message, tmp_path, path, f"ADR depositary cache {failure} ({name})", fetch=fetch
    )
    # #1223: the refusal keeps only a stand-in for the private error it
    # replaced, not that error or the OSError it wrapped.
    assert_replaced_unchained(refusal.value, AdrEntitySourceError, markers=(STRERROR, FILENAME))
    assert _files(tmp_path) == written


def test_cache_only_run_without_the_f6_artifact_counts_an_f6_miss(tmp_path: Path) -> None:
    overrides, records, audit = _resolve(
        _security(), tmp_path, fetch=False, sec_opener=_blocked, gleif_opener=_blocked
    )

    assert overrides == records == ()
    assert audit.failure_counts == (("f6-cache-miss", 1),)


@pytest.mark.parametrize("fetch", [True, False])
def test_f6_cache_hash_memory_failure_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetch: bool
) -> None:
    path = _cached_f6(tmp_path)
    written = _files(tmp_path)
    dumps = json.dumps

    def exhausted(payload, **kwargs):
        if "fetched_at" in payload:
            raise MemoryError("private cached value")
        return dumps(payload, **kwargs)

    monkeypatch.setattr(json, "dumps", exhausted)

    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve(_security(), tmp_path, fetch=fetch, sec_opener=_blocked, gleif_opener=_blocked)
    _assert_f6_refusal(
        str(refusal.value), tmp_path, path, "ADR depositary cache is unreadable", fetch=fetch
    )
    assert_replaced_unchained(refusal.value, MemoryError, markers=("private cached value",))
    assert _files(tmp_path) == written
