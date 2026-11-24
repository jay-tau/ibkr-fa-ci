"""SEC F-6 and GLEIF depositary evidence for Robinhood ADRs."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Final, NoReturn

from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.evidence_cache import (
    HASH_KEY,
    SHA256_PATTERN,
    canonical_sha256,
    collapsed,
    is_schema,
    open_sealed_artifact,
    publish_once,
)
from ibkr_fa.exceptions import raise_replacing
from ibkr_fa.models.reference_identity import (
    F6_MISSING_CONTACT,
    F6DepositaryIdentity,
    GleifRegistrantRecord,
    ObservedIdentityInput,
    RegistrantLookup,
    depositary_key,
    name_tokens,
)
from ibkr_fa.robinhood_identity_sources import (
    SET_IDENTITY_TOO,
    IdentitySourceError,
    IdentitySourceOpener,
    SecEntityCacheError,
    sec_move_aside_remedy,
    sec_source_opener,
)
from ibkr_fa.robinhood_legal_entity_sources import (
    LegalAddressOpener,
    load_gleif_registrant_by_lei,
)
from ibkr_fa.types import Isin

CACHE_SUBDIRECTORY: Final[str] = "robinhood-adr-entities"
SCHEMA_VERSION: Final[int] = 1

_ARTIFACT_NAME: Final[str] = "f6-depositary.json"
_FINGERPRINT_DOMAIN: Final[str] = "ibkr-fa/robinhood-f6-depositary"
_MAX_DOCUMENT_BYTES: Final[int] = 8 * 1024 * 1024
_F6_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"https://www\.sec\.gov/Archives/edgar/data/[0-9]+/"
    r"(?P<accession>[0-9]{18})/(?P<document>[A-Za-z0-9._-]*f6[A-Za-z0-9._-]*\.html?)",
    re.IGNORECASE,
)
_COMPANY_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "ADR",
        "ADS",
        "AMERICAN",
        "CORP",
        "CORPORATION",
        "DEPOSITARY",
        "HOLDING",
        "HOLDINGS",
        "INC",
        "INCORPORATED",
        "LIMITED",
        "LTD",
        "PLC",
        "SHARES",
    }
)
# ponytail: add another reviewed depositary only when supplied ADR evidence needs it.
_DEPOSITARY_LEIS: Final[Mapping[tuple[str, ...], str]] = {
    ("CITIBANK", "NATIONAL", "ASSOCIATION"): "E57ODZWZ7FF32TWEFA76",
    ("THE", "BANK", "OF", "NEW", "YORK", "MELLON"): "HPFHU0OQ28E4N0NFVK49",
}


class AdrEntitySourceError(ValueError):
    """Configured SEC F-6 evidence or its immutable cache is unusable."""


class _F6TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def _cached_text(artifact: Mapping[str, object], key: str) -> str:
    value = artifact.get(key)
    if not isinstance(value, str):
        raise AdrEntitySourceError("ADR depositary cache is malformed")
    return collapsed(value)


def _company_key(value: str) -> tuple[str, ...]:
    tokens = name_tokens(value)
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return tuple(tokens)


def _f6_url(source: str, cutoff: date) -> str:
    matches = list(_F6_URL_PATTERN.finditer(source))
    if len(matches) != 1:
        raise AdrEntitySourceError("ADR evidence must contain one SEC Form F-6 URL")
    accession = matches[0].group("accession")
    filing_year = 2000 + int(accession[10:12])
    if filing_year > cutoff.year:
        raise AdrEntitySourceError("ADR Form F-6 was filed after the reporting period")
    return matches[0].group(0)


def _fingerprint(
    security: RobinhoodSecurityConfig,
    description: str,
    cutoff: date,
) -> str:
    return canonical_sha256(
        {
            "domain": _FINGERPRINT_DOMAIN,
            "schema": SCHEMA_VERSION,
            "isin": str(security.instrument.isin),
            "cusip": security.cusip,
            "symbol": security.instrument.symbol,
            "issuer_country_code": security.instrument.issuer_country_code,
            "description": collapsed(description),
            "source": security.source,
            "cutoff": cutoff.isoformat(),
        }
    )


def _artifact_path(
    cache_dir: Path,
    security: RobinhoodSecurityConfig,
    description: str,
    cutoff: date,
) -> Path:
    return (
        cache_dir
        / CACHE_SUBDIRECTORY
        / _fingerprint(security, description, cutoff)
        / _ARTIFACT_NAME
    )


def _marker_value(parts: list[str], marker: str) -> str:
    indexes = [index for index, value in enumerate(parts) if marker in value.casefold()]
    if len(indexes) != 1 or indexes[0] == 0:
        raise AdrEntitySourceError("SEC Form F-6 has ambiguous identity fields")
    return parts[indexes[0] - 1]


def _parse_f6(raw: bytes, description: str, url: str) -> F6DepositaryIdentity:
    if not raw or len(raw) > _MAX_DOCUMENT_BYTES:
        raise AdrEntitySourceError("SEC Form F-6 has an invalid size")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise AdrEntitySourceError("SEC Form F-6 is not valid UTF-8") from exc
    parser = _F6TextParser()
    parser.feed(text)
    parser.close()
    joined = " ".join(parser.parts).upper()
    if "FORM F-6" not in joined or "AMERICAN DEPOSITARY" not in joined:
        raise AdrEntitySourceError("configured SEC document is not Form F-6")
    issuer = _marker_value(
        parser.parts,
        "exact name of issuer of deposited securities as specified in its charter",
    )
    depositary = _marker_value(
        parser.parts,
        "exact name of depositary as specified in its charter",
    )
    if not _company_key(description) or _company_key(issuer) != _company_key(description):
        raise AdrEntitySourceError("SEC Form F-6 is for another issuer")
    lei = _DEPOSITARY_LEIS.get(depositary_key(depositary))
    if lei is None:
        raise AdrEntitySourceError("SEC Form F-6 names an unreviewed depositary")
    return F6DepositaryIdentity(
        issuer_name=issuer,
        depositary_name=depositary,
        depositary_lei=lei,
        document_url=url,
        document_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _write_artifact(
    path: Path,
    *,
    fingerprint: str,
    fetched_at: datetime,
    identity: F6DepositaryIdentity,
    source_bytes: int,
) -> None:
    payload: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "fetched_at": fetched_at.isoformat(),
        "source_bytes": source_bytes,
        "issuer_name": identity.issuer_name,
        "depositary_name": identity.depositary_name,
        "depositary_lei": identity.depositary_lei,
        "document_url": identity.document_url,
        "document_sha256": identity.document_sha256,
    }
    publish_once(path, payload, refuse=_refuse_publication)


def _refuse_publication(exc: OSError) -> NoReturn:
    if isinstance(exc, FileExistsError):
        raise AdrEntitySourceError("ADR depositary cache already exists") from exc
    raise AdrEntitySourceError("ADR depositary cache could not be written") from exc


def _read_artifact(
    path: Path,
    *,
    fingerprint: str,
    description: str,
    source_url: str,
) -> F6DepositaryIdentity | None:
    """Read one cached F-6 identity, or ``None`` when no artifact exists."""

    opened = open_sealed_artifact(path, label="ADR depositary cache", error=AdrEntitySourceError)
    if opened is None:
        return None
    artifact, actual = opened
    expected = _cached_text(artifact, HASH_KEY)
    source_bytes = artifact.get("source_bytes")
    identity = F6DepositaryIdentity(
        issuer_name=_cached_text(artifact, "issuer_name"),
        depositary_name=_cached_text(artifact, "depositary_name"),
        depositary_lei=_cached_text(artifact, "depositary_lei"),
        document_url=_cached_text(artifact, "document_url"),
        document_sha256=_cached_text(artifact, "document_sha256"),
    )
    if (
        not expected
        or actual != expected
        or not is_schema(artifact.get("schema"), SCHEMA_VERSION)
        or artifact.get("fingerprint") != fingerprint
        or not _cached_text(artifact, "fetched_at")
        or isinstance(source_bytes, bool)
        or not isinstance(source_bytes, int)
        or source_bytes <= 0
        or not identity.issuer_name
        or _company_key(identity.issuer_name) != _company_key(description)
        or not identity.depositary_name
        or _DEPOSITARY_LEIS.get(depositary_key(identity.depositary_name)) != identity.depositary_lei
        or identity.document_url != source_url
        or SHA256_PATTERN.fullmatch(identity.document_sha256) is None
    ):
        raise AdrEntitySourceError("ADR depositary cache is malformed")
    return identity


def load_robinhood_adr_entity_evidence(  # noqa: PLR0912, PLR0915
    securities: Iterable[RobinhoodSecurityConfig],
    observed: Iterable[ObservedIdentityInput],
    configured: Iterable[FaEntityOverride],
    *,
    cutoff: date,
    cache_dir: Path,
    fetch: bool,
    sec_user_agent: str | None,
    sec_opener: IdentitySourceOpener | None = None,
    gleif_opener: LegalAddressOpener | None = None,
    now: datetime | None = None,
) -> RegistrantLookup[F6DepositaryIdentity]:
    """Load supplied ADR Form F-6 evidence without ticker-specific mappings.

    Raises:
        SecEntityCacheError: an ADR's Form F-6 cache artifact is unusable. It
            names the cache folder and a fixed problem, never a cached value,
            an ISIN or the artifact's path, and chains nothing that does: no
            context, and as its cause only the content-free stand-in
            :func:`~ibkr_fa.exceptions.raise_replacing` leaves for the error
            it replaced.
    """

    descriptions: dict[tuple[str, str], set[str]] = {}
    for item in observed:
        if item.description.strip():
            descriptions.setdefault((item.cusip, item.symbol), set()).add(
                collapsed(item.description)
            )
    configured_by_isin = {item.isin: item for item in configured}
    evidence: list[tuple[Isin, F6DepositaryIdentity, GleifRegistrantRecord]] = []
    failures: Counter[str] = Counter()
    requested = selection_hits = gleif_hits = 0
    for security in sorted(securities, key=lambda item: item.instrument.isin):
        if not security.is_depositary_receipt:
            continue
        manual = configured_by_isin.get(security.instrument.isin)
        if (
            manual is not None
            and manual.prefer_configured_address
            and manual.prefer_configured_name
            and manual.prefer_configured_nature
        ):
            continue
        requested += 1
        broker_descriptions = descriptions.get((security.cusip, security.instrument.symbol), set())
        if len(broker_descriptions) != 1:
            failures["broker-description"] += 1
            continue
        description = next(iter(broker_descriptions))
        try:
            url = _f6_url(security.source, cutoff)
        except AdrEntitySourceError:
            failures["f6-evidence"] += 1
            continue
        fingerprint = _fingerprint(security, description, cutoff)
        path = _artifact_path(cache_dir, security, description, cutoff)
        try:
            identity = _read_artifact(
                path,
                fingerprint=fingerprint,
                description=description,
                source_url=url,
            )
        except (AdrEntitySourceError, MemoryError) as exc:
            problem = (
                "ADR depositary cache is unreadable" if isinstance(exc, MemoryError) else str(exc)
            )
            # Neither the ISIN, which may be built from a statement CUSIP, nor
            # the artifact's path, whose folder name is a fingerprint (#1071).
            remedy = sec_move_aside_remedy(fetch=fetch, sec_user_agent=sec_user_agent)
            refusal = SecEntityCacheError(
                f"SEC Form F-6 cache for a held ADR is unusable ({problem}): "
                f"{cache_dir / CACHE_SUBDIRECTORY}. {remedy}, or "
                "review each held ADR's legal entity yourself and set "
                "prefer_configured_address, prefer_configured_name and prefer_configured_nature "
                f"in its [[fa.entities]] entry to skip the lookup. {SET_IDENTITY_TOO}"
            )
            raise_replacing(refusal, exc)
        if identity is not None:
            selection_hits += 1
        elif not fetch:
            failures["f6-cache-miss"] += 1
            continue
        else:
            try:
                opener = sec_opener if sec_opener is not None else sec_source_opener(sec_user_agent)
            except IdentitySourceError:
                # Only the contact check refuses here (#1526).
                failures[F6_MISSING_CONTACT] += 1
                continue
            try:
                raw = opener(url)
                identity = _parse_f6(raw, description, url)
                _write_artifact(
                    path,
                    fingerprint=fingerprint,
                    fetched_at=now if now is not None else datetime.now(UTC),
                    identity=identity,
                    source_bytes=len(raw),
                )
            except (AdrEntitySourceError, IdentitySourceError, OSError):
                failures["f6-source-failure"] += 1
                continue
        record, gleif_hit = load_gleif_registrant_by_lei(
            identity.depositary_lei,
            cache_dir=cache_dir,
            fetch=fetch,
            opener=gleif_opener,
            now=now,
        )
        gleif_hits += int(gleif_hit)
        if record is None:
            failures["gleif-cache-miss"] += 1
            continue
        evidence.append((security.instrument.isin, identity, record))
    return RegistrantLookup(
        evidence=tuple(evidence),
        requested=requested,
        selection_cache_hits=selection_hits,
        gleif_cache_hits=gleif_hits,
        failure_counts=tuple(sorted(failures.items())),
    )
