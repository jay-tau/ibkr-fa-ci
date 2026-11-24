"""The GLEIF lookup inputs and refusal texts the Robinhood legal-entity source
tests share (#1786): one held security, the issuer record GLEIF serves for it,
a loader that looks it up, the cache edits the tests make, and the whole
refusal each GLEIF cache, fetch or raw-type failure gives.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

from ibkr_fa.compute.robinhood_entity_overrides import (
    GleifLegalAddressAudit,
    resolve_robinhood_legal_entity_overrides,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.robinhood_legal_entity_sources import (
    LegalAddressOpener,
    _registrant_artifact_path,
    load_gleif_registrant_by_lei,
    load_robinhood_legal_entity_evidence,
)
from ibkr_fa.types import Isin, isin_check_digit
from tests._entity_cache import reseal_file
from tests._entity_cache_remedies import MOVE_ASIDE, SET_IDENTITY_TOO
from tests._entity_transport import FixedReplyOpener
from tests._gleif_records import gleif_address, gleif_record, gleif_reply
from tests._no_echo import CUSIP
from tests._robinhood_security import robinhood_security

GLEIF_ISIN = Isin("US99992E1010")
GLEIF_LEI = "999900SAMPLE00000161"
# A held security whose ISIN is built from the synthetic statement CUSIP (#1044).
CUSIP_ISIN = Isin(f"US{CUSIP}{isin_check_digit(f'US{CUSIP}')}")
_SKIP = (
    "set prefer_configured_address, prefer_configured_name and prefer_configured_nature in "
    f"its [[fa.entities]] entry to skip the GLEIF lookup. {SET_IDENTITY_TOO}"
)
_SECURITY_SKIP = f"each held security, review its legal entity yourself and {_SKIP}"
_REGISTRANT_SKIP = f"each held ADR and ETF, review its legal entity yourself and {_SKIP}"


def exact_pattern(message: str) -> str:
    """A ``pytest.raises`` pattern for ``message`` and nothing more."""
    return rf"\A{re.escape(message)}\Z"


def _folder(cache_dir: Path) -> Path:
    """The cache folder every GLEIF refusal names, never an artifact's
    fingerprint folder (#1044)."""
    return cache_dir / "robinhood-legal-entities"


def legal_address_cache_refusal(cache_dir: Path, problem: str, *, fetch: bool) -> str:
    """The whole exact-ISIN cache refusal, so it can't also carry a cached value."""
    return (
        f"GLEIF legal-address cache for a held security is unusable ({problem}): "
        f"{_folder(cache_dir)}. {MOVE_ASIDE[fetch]}, or, for {_SECURITY_SKIP}"
    )


def legal_address_fetch_refusal(cache_dir: Path, problem: str) -> str:
    """The whole exact-ISIN fetch refusal, so it can't also carry a fetched value."""
    return (
        "GLEIF legal-address evidence for a held security could not be fetched and cached "
        f"({problem}): {_folder(cache_dir)}. Re-run when GLEIF is reachable, or, for "
        f"{_SECURITY_SKIP}"
    )


def legal_address_raw_type_refusal(cache_dir: Path, path: str) -> str:
    """The whole exact-ISIN raw-type refusal, so it can't also carry the raw value."""
    return (
        "GLEIF legal-address response for a held security has a field of an unexpected JSON "
        f"type: {path} (cache {_folder(cache_dir)}). For {_SECURITY_SKIP}"
    )


def registrant_cache_refusal(cache_dir: Path, problem: str, *, fetch: bool) -> str:
    """The whole registrant cache refusal, so it can't also carry a cached value."""
    return (
        f"GLEIF registrant cache for a held ADR or ETF is unusable ({problem}): "
        f"{_folder(cache_dir)}. {MOVE_ASIDE[fetch]}, or, for {_REGISTRANT_SKIP}"
    )


def registrant_fetch_refusal(cache_dir: Path, problem: str) -> str:
    """The whole registrant fetch refusal, so it can't also carry a fetched value."""
    return (
        "GLEIF registrant evidence for a held ADR or ETF could not be fetched and cached "
        f"({problem}): {_folder(cache_dir)}. Re-run when GLEIF is reachable, or, for "
        f"{_REGISTRANT_SKIP}"
    )


def registrant_raw_type_refusal(cache_dir: Path, path: str) -> str:
    """The whole registrant raw-type refusal, so it can't also carry the raw value."""
    return (
        "GLEIF registrant response for a held ADR or ETF has a field of an unexpected JSON "
        f"type: {path} (cache {_folder(cache_dir)}). For {_REGISTRANT_SKIP}"
    )


def gleif_security(
    *,
    isin: Isin = GLEIF_ISIN,
    cusip: str = "99992E101",
    country: str = "US",
    depositary: bool = False,
    source: str = "manual",
) -> RobinhoodSecurityConfig:
    """One held security, symbol XQAG, for the exact-ISIN lookup."""
    return robinhood_security(
        symbol="XQAG",
        cusip=cusip,
        isin=isin,
        issuer_country_code=country,
        is_depositary_receipt=depositary,
        source=source,
    )


# The held security with the ISIN built from the statement CUSIP (#1044).
CUSIP_SECURITY = gleif_security(isin=CUSIP_ISIN, cusip=CUSIP)


def issuer_record(
    *,
    lei: str = GLEIF_LEI,
    legal_name: str = "SAMPLE ISSUER AG INC.",
    legal_lines: list[str] | None = None,
    legal_city: str = "SAMPLETON",
    legal_region: str = "US-DE",
    legal_country: str = "US",
    postal_code: str | None = "99991",
    registration_status: str = "ISSUED",
    entity_status: str = "ACTIVE",
    successor: str | None = None,
    successor_name: str | None = None,
    legal_form_id: str = "XTIQ",
    legal_form_other: str | None = None,
) -> dict[str, object]:
    """The raw GLEIF record of the held security's issuer."""
    return gleif_record(
        lei=lei,
        legal_name=legal_name,
        legal_address=gleif_address(
            (
                ["C/O SAMPLE REGISTERED AGENT", "SAMPLE AGENT CENTER", "99 SAMPLE ST"]
                if legal_lines is None
                else legal_lines
            ),
            city=legal_city,
            region=legal_region,
            country=legal_country,
            postal_code=postal_code,
        ),
        headquarters_address=gleif_address(
            ["1 SAMPLE LANE"],
            city="SAMPLE CITY",
            region="CA-BC",
            country="CA",
            postal_code="V9Z 9Z9",
        ),
        jurisdiction="US-DE",
        category="GENERAL",
        legal_form_id=legal_form_id,
        legal_form_other=legal_form_other,
        entity_status=entity_status,
        successor_lei=successor,
        successor_name=successor_name,
        registration_status=registration_status,
        last_update="2026-03-10T09:30:00Z",
    )


def issuer_reply(records: list[dict[str, object]] | None = None) -> bytes:
    """GLEIF's exact-ISIN reply: ``records``, by default one :func:`issuer_record`."""
    return gleif_reply(
        [issuer_record()] if records is None else records, publish_date="2026-08-23T00:00:00Z"
    )


def registrant_reply(*, lei: str = GLEIF_LEI, name: str = "SAMPLE ISSUER AG INC.") -> bytes:
    """GLEIF's exact-LEI reply: one :func:`issuer_record` with ``lei`` and ``name``."""
    return gleif_reply(issuer_record(lei=lei, legal_name=name), publish_date="2026-08-23T00:00:00Z")


def load_gleif(
    tmp_path: Path,
    *,
    response: bytes | None = None,
    security: RobinhoodSecurityConfig | None = None,
    configured: tuple[FaEntityOverride, ...] = (),
    fetch: bool = True,
    opener: LegalAddressOpener | None = None,
) -> tuple[tuple[FaEntityOverride, ...], GleifLegalAddressAudit, LegalAddressOpener]:
    """Look up and resolve one held security (by default :func:`gleif_security`)
    with its cache in ``tmp_path``. Unless ``opener`` is given, a
    :class:`FixedReplyOpener` serves ``response or issuer_reply()``.
    Returns the overrides, the audit and the opener used."""
    transport = opener if opener is not None else FixedReplyOpener(response or issuer_reply())
    securities = (security or gleif_security(),)
    lookup = load_robinhood_legal_entity_evidence(
        securities,
        configured,
        cache_dir=tmp_path,
        fetch=fetch,
        opener=transport,
    )
    overrides, audit, _outcomes = resolve_robinhood_legal_entity_overrides(securities, lookup)
    return overrides, audit, transport


def cached_registrant(tmp_path: Path) -> Path:
    """Fetch and cache :func:`registrant_reply` for :data:`GLEIF_LEI` in
    ``tmp_path``; returns the cache artifact's path."""
    load_gleif_registrant_by_lei(
        GLEIF_LEI, cache_dir=tmp_path, fetch=True, opener=FixedReplyOpener(registrant_reply())
    )
    return _registrant_artifact_path(tmp_path, GLEIF_LEI)


def reseal_record(path: Path, change: Callable[[dict[str, object]], object]) -> None:
    """Change a cached record, then re-hash so the artifact passes its integrity check."""
    reseal_file(path, lambda artifact: change(artifact["record"]))


def with_raw(record: dict[str, object], *path: str, value: object) -> dict[str, object]:
    """Set one raw attribute below ``attributes``, whatever its JSON type."""
    target = cast("dict[str, object]", record["attributes"])
    for key in path[:-1]:
        target = cast("dict[str, object]", target[key])
    target[path[-1]] = value
    return record
