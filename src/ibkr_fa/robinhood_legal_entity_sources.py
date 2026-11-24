"""Exact-ISIN GLEIF legal-entity evidence for Robinhood Schedule FA A3."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn, cast

from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.evidence_cache import (
    HASH_KEY,
    SHA256_PATTERN,
    UrlOpen,
    as_object,
    as_text,
    canonical_sha256,
    collapsed,
    fetch_with_retries,
    is_schema,
    publish_once,
)
from ibkr_fa.exceptions import IbkrFaError, errno_name, raise_replacing
from ibkr_fa.models.reference_identity import (
    LEI_PATTERN,
    GleifLegalAddressLookup,
    GleifLegalAddressRecord,
    GleifRegistrantRecord,
)
from ibkr_fa.robinhood_identity_sources import SET_IDENTITY_TOO, move_aside_remedy
from ibkr_fa.types import Isin

GLEIF_LEI_RECORDS_URL: Final[str] = "https://api.gleif.org/api/v1/lei-records"
GLEIF_LEI_RECORD_URL_TEMPLATE: Final[str] = f"{GLEIF_LEI_RECORDS_URL}/{{lei}}"
CACHE_SUBDIRECTORY: Final[str] = "robinhood-legal-entities"
# Bumped by #821, whose full raw-type check older artifacts never passed, and
# by #829, which counts a successor named without an LEI. An artifact from an
# older version is never read; the record is fetched again, or is a cache miss
# when fetching is off.
SCHEMA_VERSION: Final[int] = 4
REGISTRANT_SCHEMA_VERSION: Final[int] = 3

_ARTIFACT_NAME: Final[str] = "gleif.json"
_REGISTRANT_ARTIFACT_NAME: Final[str] = "gleif-registrant.json"
_FINGERPRINT_DOMAIN: Final[str] = "ibkr-fa/robinhood-gleif-legal-address"
_REGISTRANT_FINGERPRINT_DOMAIN: Final[str] = "ibkr-fa/robinhood-gleif-registrant"
_HOST: Final[str] = "api.gleif.org"
_USER_AGENT: Final[str] = "ibkr-fa Schedule-FA legal-address fetch"
_MAX_RESPONSE_BYTES: Final[int] = 4 * 1024 * 1024
_RETRY_BACKOFF_SECONDS: Final[float] = 2.0
_SKIP_LOOKUP: Final[str] = (
    "set prefer_configured_address, prefer_configured_name and prefer_configured_nature "
    f"in its [[fa.entities]] entry to skip the GLEIF lookup. {SET_IDENTITY_TOO}"
)
# A held security is never named: its ISIN may be built from a statement
# CUSIP (#1044).
_SKIP_SECURITY_LOOKUP: Final[str] = (
    f"each held security, review its legal entity yourself and {_SKIP_LOOKUP}"
)
_FETCH_FAILED: Final[str] = "a network or cache-file operation failed"
# A registrant's LEI is never named either: the held ADR or ETF picks it
# (#1086).
_SKIP_REGISTRANT_LOOKUP: Final[str] = (
    f"each held ADR and ETF, review its legal entity yourself and {_SKIP_LOOKUP}"
)

type LegalAddressOpener = Callable[[str], bytes]


class LegalAddressSourceError(ValueError):
    """A GLEIF response or immutable cache artifact is unusable."""


class GleifRawTypeError(LegalAddressSourceError):
    """A raw GLEIF response field has a JSON type the loader does not expect.

    ``path`` is the field's JSON path; the value is never kept or printed.
    """

    def __init__(self, path: str) -> None:
        super().__init__(f"GLEIF response field {path} has an unexpected JSON type")
        self.path = path


class GleifLegalAddressError(IbkrFaError):
    """A held security's GLEIF evidence is unusable.

    Its exact-ISIN or registrant cache artifact is unusable or its fetch
    failed, a GLEIF response has a raw field of an unexpected JSON type, or a
    registrant lookup was asked for an invalid LEI. The message names no ISIN
    or LEI.
    """


@dataclass(frozen=True, slots=True)
class _GleifEntityValues:
    lei: str
    legal_name: str
    address_lines: tuple[str, ...]
    city: str
    region: str
    country_iso2: str
    postal_code: str
    jurisdiction: str
    category: str
    legal_form_id: str
    legal_form_other: str
    entity_status: str
    registration_status: str
    registration_last_update: str
    has_successor: bool
    golden_copy_publish_date: str
    source_url: str
    source_sha256: str


# The 16 text fields of :class:`_GleifEntityValues`, in the order a cache
# reader reads them. With ``address_lines`` and ``has_successor`` they are the
# 18 fields both GLEIF records share (#1650).
_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "lei",
    "legal_name",
    "city",
    "region",
    "country_iso2",
    "postal_code",
    "jurisdiction",
    "category",
    "legal_form_id",
    "legal_form_other",
    "entity_status",
    "registration_status",
    "registration_last_update",
    "golden_copy_publish_date",
    "source_url",
    "source_sha256",
)


def _shared_fields(
    source: _GleifEntityValues | GleifLegalAddressRecord | GleifRegistrantRecord,
) -> dict[str, Any]:
    return {key: getattr(source, key) for key in ("address_lines", "has_successor", *_TEXT_FIELDS)}


def _fingerprint(isin: Isin) -> str:
    payload = {
        "domain": _FINGERPRINT_DOMAIN,
        "schema": SCHEMA_VERSION,
        "isin": str(isin),
    }
    return canonical_sha256(payload)


def _artifact_path(cache_dir: Path, isin: Isin) -> Path:
    return cache_dir / CACHE_SUBDIRECTORY / _fingerprint(isin) / _ARTIFACT_NAME


def _registrant_fingerprint(lei: str) -> str:
    return canonical_sha256(
        {
            "domain": _REGISTRANT_FINGERPRINT_DOMAIN,
            "schema": REGISTRANT_SCHEMA_VERSION,
            "lei": lei,
        }
    )


def _registrant_artifact_path(cache_dir: Path, lei: str) -> Path:
    return cache_dir / CACHE_SUBDIRECTORY / _registrant_fingerprint(lei) / _REGISTRANT_ARTIFACT_NAME


def _source_url(isin: Isin) -> str:
    query = urllib.parse.urlencode({"filter[isin]": str(isin), "page[size]": "2"})
    return f"{GLEIF_LEI_RECORDS_URL}?{query}"


def _registrant_source_url(lei: str) -> str:
    return GLEIF_LEI_RECORD_URL_TEMPLATE.format(lei=lei)


def _url_open(request: urllib.request.Request, timeout: float) -> tuple[str, bytes]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.geturl(), response.read(_MAX_RESPONSE_BYTES + 1)


def _fetch_bytes(
    url: str,
    *,
    url_open: UrlOpen | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.api+json"},
    )
    final_url, body = fetch_with_retries(
        request,
        transport=url_open if url_open is not None else _url_open,
        backoff_seconds=_RETRY_BACKOFF_SECONDS,
        sleep=sleep,
        refuse_status=_refuse_status,
        refuse_unreachable=_refuse_unreachable,
    )
    split = urllib.parse.urlsplit(final_url)
    if split.scheme != "https" or split.hostname != _HOST:
        raise LegalAddressSourceError(
            "GLEIF legal-address request redirected to an unexpected host"
        )
    if len(body) > _MAX_RESPONSE_BYTES:
        raise LegalAddressSourceError("GLEIF legal-address response is implausibly large")
    return body


def _refuse_status(exc: urllib.error.HTTPError) -> NoReturn:
    raise LegalAddressSourceError(
        f"GLEIF legal-address request failed with HTTP {exc.code}"
    ) from exc


def _refuse_unreachable(exc: Exception) -> NoReturn:
    raise LegalAddressSourceError("GLEIF legal-address evidence could not be retrieved") from exc


def _default_opener() -> LegalAddressOpener:
    last_request = 0.0

    def open_url(url: str) -> bytes:
        nonlocal last_request
        delay = 0.12 - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)
        body = _fetch_bytes(url)
        last_request = time.monotonic()
        return body

    return open_url


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _folder(cache_dir: Path) -> Path:
    """The GLEIF cache folder an error names: the cache root as given, joined
    with a fixed name; never an artifact's path, whose folder name is built
    from an ISIN or LEI (#1044)."""
    return cache_dir / CACHE_SUBDIRECTORY


def _problem(exc: BaseException, operation: str) -> str:
    """Fixed text for why GLEIF evidence is unusable (#1044).

    A :class:`LegalAddressSourceError`'s own text, which is fixed text and at
    most a JSON field location; ``operation`` and an ``OSError``'s errno name;
    and never any other error's text.
    """
    if isinstance(exc, LegalAddressSourceError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"{operation} ({errno_name(exc)})"
    if isinstance(exc, MemoryError):
        return "it is too large to read"
    if isinstance(exc, RecursionError):
        return "its JSON is nested too deeply"
    return "it is malformed"


# The raw readers below take each response field's JSON path. GLEIF sends
# ``null`` for an absent value, so they accept it; any other unexpected type
# refuses with the path, before the value is cleaned, compared or cached (#821).
def _raw_object(
    container: Mapping[str, object] | None,
    key: str,
    path: str,
) -> Mapping[str, object] | None:
    value = None if container is None else container.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise GleifRawTypeError(_child(path, key))
    return cast("Mapping[str, object]", value)


def _raw_list(container: Mapping[str, object] | None, key: str, path: str) -> list[object]:
    value = None if container is None else container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise GleifRawTypeError(_child(path, key))
    return cast("list[object]", value)


def _raw_text(container: Mapping[str, object] | None, key: str, path: str) -> str:
    value = None if container is None else container.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GleifRawTypeError(_child(path, key))
    return collapsed(value)


def _raw_lines(address: Mapping[str, object] | None, path: str) -> tuple[str, ...]:
    lines = []
    for index, line in enumerate(_raw_list(address, "addressLines", path)):
        if not isinstance(line, str):
            raise GleifRawTypeError(f"{_child(path, 'addressLines')}[{index}]")
        if collapsed(line):
            lines.append(collapsed(line))
    return tuple(lines)


def _publish_date(root: Mapping[str, object] | None) -> str:
    meta = _raw_object(root, "meta", "")
    return _raw_text(_raw_object(meta, "goldenCopy", "meta"), "publishDate", "meta.goldenCopy")


def _entity_payload(record: GleifLegalAddressRecord | GleifRegistrantRecord) -> dict[str, object]:
    return {**_shared_fields(record), "address_lines": list(record.address_lines)}


def _record_payload(record: GleifLegalAddressRecord) -> dict[str, object]:
    return {"isin": str(record.isin), **_entity_payload(record)}


def _entity_values(
    attrs: Mapping[str, object],
    *,
    path: str,
    publish_date: str,
    url: str,
    source_sha256: str,
) -> _GleifEntityValues:
    entity = _raw_object(attrs, "entity", path)
    registration = _raw_object(attrs, "registration", path)
    entity_path = _child(path, "entity")
    legal_name = _raw_object(entity, "legalName", entity_path)
    address = _raw_object(entity, "legalAddress", entity_path)
    legal_form = _raw_object(entity, "legalForm", entity_path)
    successor = _raw_object(entity, "successorEntity", entity_path)
    address_path = _child(entity_path, "legalAddress")
    form_path = _child(entity_path, "legalForm")
    successor_path = _child(entity_path, "successorEntity")
    registration_path = _child(path, "registration")
    successor_lei = _raw_text(successor, "lei", successor_path)
    successors = _raw_list(entity, "successorEntities", entity_path)
    # GLEIF names a successor without an LEI by its name alone (#829).
    successor_name = _raw_text(successor, "name", successor_path)
    values = _GleifEntityValues(
        lei=_raw_text(attrs, "lei", path),
        legal_name=_raw_text(legal_name, "name", _child(entity_path, "legalName")),
        address_lines=_raw_lines(address, address_path),
        city=_raw_text(address, "city", address_path),
        region=_raw_text(address, "region", address_path),
        country_iso2=_raw_text(address, "country", address_path).upper(),
        postal_code=_raw_text(address, "postalCode", address_path),
        jurisdiction=_raw_text(entity, "jurisdiction", entity_path),
        category=_raw_text(entity, "category", entity_path),
        legal_form_id=_raw_text(legal_form, "id", form_path),
        legal_form_other=_raw_text(legal_form, "other", form_path),
        entity_status=_raw_text(entity, "status", entity_path),
        registration_status=_raw_text(registration, "status", registration_path),
        registration_last_update=_raw_text(registration, "lastUpdateDate", registration_path),
        has_successor=bool(successor_lei or successor_name or successors),
        golden_copy_publish_date=publish_date,
        source_url=url,
        source_sha256=source_sha256,
    )
    if entity is None or registration is None or legal_name is None or address is None:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid record")
    return values


def _isin_record(isin: Isin, values: _GleifEntityValues) -> GleifLegalAddressRecord:
    return GleifLegalAddressRecord(isin=isin, **_shared_fields(values))


def _registrant_record(values: _GleifEntityValues) -> GleifRegistrantRecord:
    return GleifRegistrantRecord(**_shared_fields(values))


def _cached_field_error(cache: str, key: str) -> LegalAddressSourceError:
    return LegalAddressSourceError(
        f"GLEIF {cache} cache field record.{key} has an unexpected JSON type"
    )


def _cached_text(payload: Mapping[str, object], key: str, *, cache: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise _cached_field_error(cache, key)
    return collapsed(value)


def _cached_values(
    payload: Mapping[str, object],
    *,
    cache: str,
    isin: bool = False,
) -> tuple[str | None, _GleifEntityValues]:
    """Read a cached record's shared fields, and its ``isin`` when asked.

    It reads ``address_lines``, then ``has_successor``, then ``isin`` if
    asked, then each of :data:`_TEXT_FIELDS` in order. The first malformed
    field decides the refusal, which names the ``cache`` label and the field.
    """
    raw_lines = payload.get("address_lines")
    if not isinstance(raw_lines, list) or not all(isinstance(line, str) for line in raw_lines):
        raise _cached_field_error(cache, "address_lines")
    raw_successor = payload.get("has_successor")
    if not isinstance(raw_successor, bool):
        raise _cached_field_error(cache, "has_successor")
    cached_isin = _cached_text(payload, "isin", cache=cache) if isin else None
    text = {key: _cached_text(payload, key, cache=cache) for key in _TEXT_FIELDS}
    return cached_isin, _GleifEntityValues(
        address_lines=tuple(cast("list[str]", raw_lines)),
        has_successor=raw_successor,
        **text,
    )


def _record_from_payload(payload: Mapping[str, object]) -> GleifLegalAddressRecord:
    isin, values = _cached_values(payload, cache="legal-address", isin=True)
    if not isin or not values.source_url or not values.source_sha256:
        raise LegalAddressSourceError("GLEIF legal-address cache contains an invalid record")
    return _isin_record(Isin(isin), values)


def _record_values(
    item: object,
    *,
    path: str,
    publish_date: str,
    url: str,
    source_sha256: str,
) -> _GleifEntityValues:
    if item is None:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid record")
    if not isinstance(item, dict):
        raise GleifRawTypeError(path)
    attrs = _raw_object(cast("Mapping[str, object]", item), "attributes", path)
    if attrs is None:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid record")
    return _entity_values(
        attrs,
        path=_child(path, "attributes"),
        publish_date=publish_date,
        url=url,
        source_sha256=source_sha256,
    )


def _parse_response(
    raw: bytes,
    *,
    isin: Isin,
    url: str,
) -> tuple[GleifLegalAddressRecord | None, str, str]:
    """Return the record, its outcome and the response's golden-copy publish date."""
    try:
        payload = json.loads(raw)
    except ValueError:  # UnicodeDecodeError too
        raise LegalAddressSourceError("GLEIF legal-address response is malformed") from None
    root = as_object(payload)
    if root is None:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid schema")
    publish_date = _publish_date(root)
    data = root.get("data")
    if data is not None and not isinstance(data, list):
        raise GleifRawTypeError("data")
    if data is None:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid schema")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    # Every record is checked, including each of a multiple match.
    values = [
        _record_values(
            item,
            path=f"data[{index}]",
            publish_date=publish_date,
            url=url,
            source_sha256=source_sha256,
        )
        for index, item in enumerate(cast("list[object]", data))
    ]
    if not publish_date:
        raise LegalAddressSourceError("GLEIF legal-address response has an invalid schema")
    if not values:
        return None, "zero-match", publish_date
    if len(values) != 1:
        return None, "multi-match", publish_date
    return _isin_record(isin, values[0]), "record", publish_date


def _parse_registrant_response(
    raw: bytes,
    *,
    lei: str,
    url: str,
) -> GleifRegistrantRecord:
    try:
        payload = json.loads(raw)
    except ValueError:  # UnicodeDecodeError too
        raise LegalAddressSourceError("GLEIF registrant response is malformed") from None
    root = as_object(payload)
    publish_date = _publish_date(root)
    item = _raw_object(root, "data", "")
    if root is None or item is None:
        raise LegalAddressSourceError("GLEIF registrant response has an invalid record")
    record_id = _raw_text(item, "id", "data")
    values = _record_values(
        item,
        path="data",
        publish_date=publish_date,
        url=url,
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    if not publish_date or record_id != lei or values.lei != lei:
        raise LegalAddressSourceError("GLEIF registrant response has an invalid record")
    return _registrant_record(values)


def _artifact_payload(
    *,
    isin: Isin,
    fetched_at: datetime,
    outcome: str,
    record: GleifLegalAddressRecord | None,
    source_url: str,
    source_sha256: str,
    source_bytes: int,
    golden_copy_publish_date: str,
) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "requested_isin": str(isin),
        "fetched_at": fetched_at.isoformat(),
        "outcome": outcome,
        "golden_copy_publish_date": golden_copy_publish_date,
        "source": {
            "url": source_url,
            "sha256": source_sha256,
            "bytes": source_bytes,
        },
        "record": None if record is None else _record_payload(record),
    }


def _write_artifact(path: Path, payload: Mapping[str, object]) -> None:
    publish_once(path, payload, refuse=_refuse_publication)


def _refuse_publication(exc: OSError) -> NoReturn:
    if isinstance(exc, FileExistsError):
        raise LegalAddressSourceError("GLEIF legal-address cache artifact already exists") from None
    raise LegalAddressSourceError(
        f"GLEIF legal-address cache artifact could not be written ({errno_name(exc)})"
    ) from None


def _read_artifact(path: Path, *, isin: Isin) -> tuple[GleifLegalAddressRecord | None, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegalAddressSourceError(
            f"GLEIF legal-address cache artifact could not be read ({errno_name(exc)})"
        ) from None
    except ValueError:  # UnicodeDecodeError too
        raise LegalAddressSourceError(
            "GLEIF legal-address cache artifact is not valid UTF-8 JSON"
        ) from None
    artifact = as_object(raw)
    if artifact is None:
        raise LegalAddressSourceError("GLEIF legal-address cache artifact is malformed")
    expected = as_text(artifact.get(HASH_KEY))
    payload = {key: value for key, value in artifact.items() if key != HASH_KEY}
    if not expected or canonical_sha256(payload) != expected:
        raise LegalAddressSourceError(
            "GLEIF legal-address cache artifact failed its integrity check"
        )
    if not is_schema(artifact.get("schema"), SCHEMA_VERSION) or artifact.get(
        "requested_isin"
    ) != str(isin):
        raise LegalAddressSourceError("GLEIF legal-address cache artifact does not match its ISIN")
    outcome = as_text(artifact.get("outcome"))
    source = as_object(artifact.get("source"))
    source_url = "" if source is None else as_text(source.get("url"))
    source_sha256 = "" if source is None else as_text(source.get("sha256"))
    source_bytes = None if source is None else source.get("bytes")
    if (
        outcome not in {"record", "zero-match", "multi-match"}
        or not as_text(artifact.get("fetched_at"))
        or not as_text(artifact.get("golden_copy_publish_date"))
        or source_url != _source_url(isin)
        or SHA256_PATTERN.fullmatch(source_sha256) is None
        or isinstance(source_bytes, bool)
        or not isinstance(source_bytes, int)
        or source_bytes <= 0
    ):
        raise LegalAddressSourceError("GLEIF legal-address cache artifact is malformed")
    raw_record = artifact.get("record")
    if raw_record is None:
        if outcome == "record":
            raise LegalAddressSourceError("GLEIF legal-address cache contains an invalid outcome")
        return None, outcome
    record_payload = as_object(raw_record)
    if record_payload is None:
        raise LegalAddressSourceError("GLEIF legal-address cache contains an invalid record")
    record = _record_from_payload(record_payload)
    if (
        outcome != "record"
        or record.isin != isin
        or record.source_url != source_url
        or record.source_sha256 != source_sha256
        or record.golden_copy_publish_date != as_text(artifact.get("golden_copy_publish_date"))
    ):
        raise LegalAddressSourceError("GLEIF legal-address cache record is for another ISIN")
    return record, outcome


def _registrant_artifact_payload(
    record: GleifRegistrantRecord,
    *,
    fetched_at: datetime,
    source_bytes: int,
) -> dict[str, object]:
    return {
        "schema": REGISTRANT_SCHEMA_VERSION,
        "requested_lei": record.lei,
        "fetched_at": fetched_at.isoformat(),
        "golden_copy_publish_date": record.golden_copy_publish_date,
        "source": {
            "url": record.source_url,
            "sha256": record.source_sha256,
            "bytes": source_bytes,
        },
        "record": _entity_payload(record),
    }


def _read_registrant_artifact(
    path: Path,
    *,
    lei: str,
) -> GleifRegistrantRecord:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LegalAddressSourceError(
            f"GLEIF registrant cache artifact could not be read ({errno_name(exc)})"
        ) from None
    except ValueError:  # UnicodeDecodeError too
        raise LegalAddressSourceError(
            "GLEIF registrant cache artifact is not valid UTF-8 JSON"
        ) from None
    artifact = as_object(raw)
    if artifact is None:
        raise LegalAddressSourceError("GLEIF registrant cache artifact is malformed")
    expected = as_text(artifact.get(HASH_KEY))
    payload = {key: value for key, value in artifact.items() if key != HASH_KEY}
    source = as_object(artifact.get("source"))
    raw_record = as_object(artifact.get("record"))
    if (
        not expected
        or canonical_sha256(payload) != expected
        or not is_schema(artifact.get("schema"), REGISTRANT_SCHEMA_VERSION)
        or artifact.get("requested_lei") != lei
        or not as_text(artifact.get("fetched_at"))
        or not as_text(artifact.get("golden_copy_publish_date"))
        or source is None
        or raw_record is None
    ):
        raise LegalAddressSourceError("GLEIF registrant cache artifact is malformed")
    record = _registrant_record_from_cache(raw_record)
    source_url = as_text(source.get("url"))
    source_sha256 = as_text(source.get("sha256"))
    source_bytes = source.get("bytes")
    if (
        record.lei != lei
        or source_url != _registrant_source_url(lei)
        or record.source_url != source_url
        or record.source_sha256 != source_sha256
        or record.golden_copy_publish_date != as_text(artifact.get("golden_copy_publish_date"))
        or SHA256_PATTERN.fullmatch(source_sha256) is None
        or isinstance(source_bytes, bool)
        or not isinstance(source_bytes, int)
        or source_bytes <= 0
    ):
        raise LegalAddressSourceError("GLEIF registrant cache record is inconsistent")
    return record


def _registrant_record_from_cache(
    payload: Mapping[str, object],
) -> GleifRegistrantRecord:
    _, values = _cached_values(payload, cache="registrant")
    return _registrant_record(values)


def load_gleif_registrant_by_lei(
    lei: str,
    *,
    cache_dir: Path,
    fetch: bool,
    opener: LegalAddressOpener | None = None,
    now: datetime | None = None,
) -> tuple[GleifRegistrantRecord | None, bool]:
    """Load one immutable GLEIF registrant record by exact LEI.

    Returns the record and whether it came from the cache. The record is
    ``None`` only when fetching is off and no artifact exists.

    Raises:
        GleifLegalAddressError: the LEI is invalid, the cache artifact is
            unusable, the fetch failed, or the response has a raw field of an
            unexpected JSON type. The message names no LEI, cached or fetched
            value, or artifact path (its folder name is the LEI's
            fingerprint). A cache or fetch refusal chains nothing that does:
            it has no context, and its only possible cause is the
            content-free stand-in :func:`~ibkr_fa.exceptions.raise_replacing`
            leaves for a replaced error that was or chained a crash.
    """

    if LEI_PATTERN.fullmatch(lei) is None:
        raise GleifLegalAddressError(
            "GLEIF registrant lookup was asked for an LEI that is not valid. For each ADR or "
            f"ETF, review its legal entity yourself and {_SKIP_LOOKUP}"
        )
    path = _registrant_artifact_path(cache_dir, lei)
    try:
        try:
            path.lstat()
        except FileNotFoundError:
            cached = None
        else:
            cached = _read_registrant_artifact(path, lei=lei)
    except (MemoryError, OSError, RecursionError, ValueError) as exc:
        refusal = GleifLegalAddressError(
            "GLEIF registrant cache for a held ADR or ETF is unusable "
            f"({_problem(exc, 'could not inspect it')}): {_folder(cache_dir)}. "
            f"{move_aside_remedy(fetch=fetch)}, or, for {_SKIP_REGISTRANT_LOOKUP}"
        )
        raise_replacing(refusal, exc)
    if cached is not None:
        return cached, True
    if not fetch:
        return None, False
    url = _registrant_source_url(lei)
    transport = opener if opener is not None else _default_opener()
    try:
        raw = transport(url)
        record = _parse_registrant_response(raw, lei=lei, url=url)
        _write_artifact(
            path,
            _registrant_artifact_payload(
                record,
                fetched_at=now if now is not None else datetime.now(UTC),
                source_bytes=len(raw),
            ),
        )
    except GleifRawTypeError as exc:
        raise GleifLegalAddressError(
            "GLEIF registrant response for a held ADR or ETF has a field of an unexpected JSON "
            f"type: {exc.path} (cache {_folder(cache_dir)}). For {_SKIP_REGISTRANT_LOOKUP}"
        ) from None
    except (MemoryError, OSError, RecursionError, ValueError) as exc:
        refusal = GleifLegalAddressError(
            "GLEIF registrant evidence for a held ADR or ETF could not be fetched and cached "
            f"({_problem(exc, _FETCH_FAILED)}): {_folder(cache_dir)}. Re-run when GLEIF is "
            f"reachable, or, for {_SKIP_REGISTRANT_LOOKUP}"
        )
        raise_replacing(refusal, exc)
    return record, False


def load_robinhood_legal_entity_evidence(
    securities: Iterable[RobinhoodSecurityConfig],
    configured: Iterable[FaEntityOverride],
    *,
    cache_dir: Path,
    fetch: bool,
    opener: LegalAddressOpener | None = None,
    now: datetime | None = None,
) -> GleifLegalAddressLookup:
    """Load exact-ISIN GLEIF legal-entity records without deciding any override.

    Raises:
        GleifLegalAddressError: a cache artifact is unusable, a fetch failed, or
            a response has a raw field of an unexpected JSON type. The message
            names no ISIN, cached or fetched value, or artifact path (its
            folder name is the ISIN's fingerprint). A cache or fetch refusal
            chains nothing that does: it has no context, and its only possible
            cause is the content-free stand-in
            :func:`~ibkr_fa.exceptions.raise_replacing` leaves for a replaced
            error that was or chained a crash.
    """

    configured_by_isin = {item.isin: item for item in configured}
    transport = opener if opener is not None else _default_opener()
    records: list[GleifLegalAddressRecord] = []
    failures: dict[Isin, str] = {}
    cache_hits = 0
    for security in sorted(securities, key=lambda item: item.instrument.isin):
        isin = security.instrument.isin
        manual = configured_by_isin.get(isin)
        if (
            manual is not None
            and manual.prefer_configured_address
            and manual.prefer_configured_nature
            and manual.prefer_configured_name
        ):
            continue
        if security.is_depositary_receipt:
            failures[isin] = "adr-excluded"
            continue
        path = _artifact_path(cache_dir, isin)
        record: GleifLegalAddressRecord | None
        outcome: str
        try:
            try:
                path.lstat()
            except FileNotFoundError:
                cache_result = None
            else:
                cache_result = _read_artifact(path, isin=isin)
        except (MemoryError, OSError, RecursionError, ValueError) as exc:
            refusal = GleifLegalAddressError(
                "GLEIF legal-address cache for a held security is unusable "
                f"({_problem(exc, 'could not inspect it')}): {_folder(cache_dir)}. "
                f"{move_aside_remedy(fetch=fetch)}, or, for {_SKIP_SECURITY_LOOKUP}"
            )
            raise_replacing(refusal, exc)
        if cache_result is not None:
            record, outcome = cache_result
            cache_hits += 1
        elif not fetch:
            failures[isin] = "cache-miss"
            continue
        else:
            url = _source_url(isin)
            try:
                raw = transport(url)
                record, outcome, golden = _parse_response(raw, isin=isin, url=url)
                _write_artifact(
                    path,
                    _artifact_payload(
                        isin=isin,
                        fetched_at=now if now is not None else datetime.now(UTC),
                        outcome=outcome,
                        record=record,
                        source_url=url,
                        source_sha256=hashlib.sha256(raw).hexdigest(),
                        source_bytes=len(raw),
                        golden_copy_publish_date=golden,
                    ),
                )
            except GleifRawTypeError as exc:
                raise GleifLegalAddressError(
                    "GLEIF legal-address response for a held security has a field of an "
                    f"unexpected JSON type: {exc.path} (cache {_folder(cache_dir)}). For "
                    f"{_SKIP_SECURITY_LOOKUP}"
                ) from None
            except (MemoryError, OSError, RecursionError, ValueError) as exc:
                refusal = GleifLegalAddressError(
                    "GLEIF legal-address evidence for a held security could not be fetched and "
                    f"cached ({_problem(exc, _FETCH_FAILED)}): {_folder(cache_dir)}. Re-run when "
                    f"GLEIF is reachable, or, for {_SKIP_SECURITY_LOOKUP}"
                )
                raise_replacing(refusal, exc)
        if record is None:
            failures[isin] = outcome
            continue
        records.append(record)
    return GleifLegalAddressLookup(
        records=tuple(sorted(records, key=lambda item: item.isin)),
        failures=failures,
        cache_hits=cache_hits,
    )
