"""Decide Robinhood A3 entity overrides from GLEIF, F-6 and N-CEN evidence (#634).

The ``ibkr_fa.robinhood_*_sources`` loaders only fetch and cache. Each
decision here checks one security's evidence (lifecycle, name, country,
address and portal shape) and either builds an
:class:`~ibkr_fa.config.FaEntityOverride` or counts why it stays manual.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    F6_MISSING_CONTACT,
    LEI_PATTERN,
    NCEN_MISSING_CONTACT,
    SET_SEC_CONTACT,
    F6DepositaryIdentity,
    GleifLegalAddressLookup,
    GleifLegalAddressRecord,
    GleifRegistrantRecord,
    NcenFundIdentity,
    RegistrantLookup,
    depositary_key,
)
from ibkr_fa.types import Isin

A3_NAME_MAX_LENGTH: Final[int] = 125
A3_ADDRESS_MAX_LENGTH: Final[int] = 200
A3_ZIP_MAX_LENGTH: Final[int] = 8
FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@")
# The only GLEIF outcomes after which the SEC business-address fallback may
# run: a verified exact-ISIN zero-match, or a LAPSED record that passes every
# other legal-address check (owner decision on #792).
SEC_FALLBACK_GLEIF_OUTCOMES: Final[frozenset[str]] = frozenset({"zero-match", "lapsed"})
_REVIEWED_ADDRESS_ALIASES: Final[Mapping[tuple[Isin, tuple[str, ...], str, str, str, str], str]] = {
    (
        Isin("CH1134540470"),
        ("Förrlibuckstrasse 190",),
        "Zürich",
        "CH-ZH",
        "CH",
        "8005",
    ): "Forrlibuckstrasse 190, Zurich, CH-ZH, CH",
}


@dataclass(frozen=True, slots=True)
class GleifLegalAddressAudit:
    """Privacy-safe resolution counts for one Robinhood bundle."""

    requested: int
    resolved: int
    cache_hits: int
    failure_counts: tuple[tuple[str, int], ...] = ()
    reviewed_ascii_aliases: int = 0

    @property
    def warnings(self) -> tuple[str, ...]:
        result = []
        if self.failure_counts:
            details = ", ".join(f"{name}:{count}" for name, count in self.failure_counts)
            result.append(
                "GLEIF legal-address evidence was unavailable for "
                f"{sum(count for _name, count in self.failure_counts)} securities "
                f"({details})"
            )
        if self.reviewed_ascii_aliases:
            result.append(
                f"{self.reviewed_ascii_aliases} GLEIF legal address used a reviewed ASCII "
                "portal alias; the exact Unicode source remains in the immutable cache"
            )
        return tuple(result)


def _contact_step(failure_counts: tuple[tuple[str, int], ...], missing_contact: str) -> str:
    """The step an ADR or ETF warning adds when an SEC fetch failed only for
    the run's fair-access contact (#1526); nothing otherwise."""
    if missing_contact in dict(failure_counts):
        return f"; to fetch from SEC, {SET_SEC_CONTACT}"
    return ""


@dataclass(frozen=True, slots=True)
class RobinhoodAdrEntityAudit:
    """Privacy-safe ADR depositary resolution coverage."""

    requested: int
    resolved: int
    selection_cache_hits: int
    gleif_cache_hits: int
    failure_counts: tuple[tuple[str, int], ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        if not self.failure_counts:
            return ()
        details = ", ".join(f"{name}:{count}" for name, count in self.failure_counts)
        return (
            "ADR depositary identity remains manual for "
            f"{sum(count for _name, count in self.failure_counts)} securities "
            f"({details}){_contact_step(self.failure_counts, F6_MISSING_CONTACT)}",
        )


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodFundEntityEvidence:
    """One filed ETF name/address plus its SEC and GLEIF binders."""

    isin: Isin
    name: str
    address: str
    zip_code: str
    cik: int
    series_id: str
    class_id: str
    ticker: str
    ncen_accession: str
    ncen_report_date: date
    registrant_lei: str
    registrant_name: str
    series_name: str
    gleif_source_sha256: str


@dataclass(frozen=True, slots=True)
class RobinhoodFundEntityAudit:
    """Privacy-safe ETF registrant resolution coverage."""

    requested: int
    resolved: int
    selection_cache_hits: int
    gleif_cache_hits: int
    failure_counts: tuple[tuple[str, int], ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        if not self.failure_counts:
            return ()
        details = ", ".join(f"{name}:{count}" for name, count in self.failure_counts)
        return (
            "ETF registrant identity remains manual for "
            f"{sum(count for _name, count in self.failure_counts)} securities "
            f"({details}){_contact_step(self.failure_counts, NCEN_MISSING_CONTACT)}",
        )


def _portal_safe(value: str, *, max_length: int) -> bool:
    rendered = " ".join(value.replace(",", " ").split())
    return (
        bool(rendered)
        and value.isascii()
        and value.isprintable()
        and not value.lstrip().startswith(FORMULA_PREFIXES)
        and len(rendered) <= max_length
    )


def raw_portal_safe(value: str, *, max_length: int) -> bool:
    """True for nonempty printable ASCII of at most ``max_length`` characters
    that doesn't start, after leading whitespace, with a formula prefix."""
    return (
        bool(value)
        and len(value) <= max_length
        and value.isascii()
        and value.isprintable()
        and not value.lstrip().startswith(FORMULA_PREFIXES)
    )


def gleif_lifecycle_active(
    record: GleifLegalAddressRecord | GleifRegistrantRecord, *, allow_lapsed: bool
) -> bool:
    """True for an ACTIVE entity with no successor whose registration is
    ISSUED, or ISSUED or LAPSED when ``allow_lapsed``."""
    return (
        record.registration_status in ({"ISSUED", "LAPSED"} if allow_lapsed else {"ISSUED"})
        and record.entity_status == "ACTIVE"
        and not record.has_successor
    )


def _legal_address(record: GleifLegalAddressRecord | GleifRegistrantRecord) -> str:
    parts = [*record.address_lines, record.city]
    if record.region:
        parts.append(record.region)
    parts.append(record.country_iso2)
    return ", ".join(parts)


def _legal_override(  # noqa: PLR0911
    record: GleifLegalAddressRecord,
    security: RobinhoodSecurityConfig,
    *,
    allow_lapsed: bool = False,
) -> tuple[FaEntityOverride | None, str]:
    if LEI_PATTERN.fullmatch(record.lei) is None or not record.legal_name:
        return None, "incomplete"
    if not gleif_lifecycle_active(record, allow_lapsed=allow_lapsed):
        return None, "lifecycle"
    if record.country_iso2 != security.instrument.issuer_country_code:
        return None, "country-mismatch"
    if not record.address_lines or not record.city or not record.postal_code:
        return None, "incomplete"
    address = _legal_address(record)
    alias = _REVIEWED_ADDRESS_ALIASES.get(
        (
            security.instrument.isin,
            record.address_lines,
            record.city,
            record.region,
            record.country_iso2,
            record.postal_code,
        )
    )
    if alias is not None:
        decomposed = "".join(
            character
            for character in unicodedata.normalize("NFD", address)
            if unicodedata.category(character) != "Mn"
        )
        if alias != decomposed or not alias.isascii():
            return None, "portal-shape"
        address = alias
    if not _portal_safe(address, max_length=A3_ADDRESS_MAX_LENGTH) or not raw_portal_safe(
        record.postal_code, max_length=A3_ZIP_MAX_LENGTH
    ):
        return None, "portal-shape"
    return (
        FaEntityOverride(
            isin=security.instrument.isin,
            address=address,
            zip_code=record.postal_code,
        ),
        "resolved-ascii-alias" if alias is not None else "resolved",
    )


def _filed_override(
    security: RobinhoodSecurityConfig,
    record: GleifRegistrantRecord,
    name: str,
) -> tuple[FaEntityOverride | None, str]:
    """The country, address and portal-shape checks an ADR or ETF override
    runs after its own checks, and the override filed under ``name``."""
    if record.country_iso2 != security.instrument.issuer_country_code:
        return None, "country-mismatch"
    if not record.address_lines or not record.city or not record.postal_code:
        return None, "incomplete-address"
    address = _legal_address(record)
    if (
        not _portal_safe(name, max_length=A3_NAME_MAX_LENGTH)
        or not _portal_safe(address, max_length=A3_ADDRESS_MAX_LENGTH)
        or not _portal_safe(record.postal_code, max_length=A3_ZIP_MAX_LENGTH)
    ):
        return None, "portal-shape"
    return (
        FaEntityOverride(
            isin=security.instrument.isin,
            name=name,
            address=address,
            zip_code=record.postal_code,
        ),
        "resolved",
    )


def _depositary_override(
    security: RobinhoodSecurityConfig,
    identity: F6DepositaryIdentity,
    record: GleifRegistrantRecord,
) -> tuple[FaEntityOverride | None, str]:
    if not gleif_lifecycle_active(record, allow_lapsed=False):
        return None, "gleif-lifecycle"
    if depositary_key(record.legal_name) != depositary_key(identity.depositary_name):
        return None, "depositary-name-mismatch"
    return _filed_override(security, record, record.legal_name)


def _legal_record(
    security: RobinhoodSecurityConfig,
    record: GleifRegistrantRecord,
) -> GleifLegalAddressRecord:
    return GleifLegalAddressRecord(
        isin=security.instrument.isin,
        lei=record.lei,
        legal_name=record.legal_name,
        address_lines=record.address_lines,
        city=record.city,
        region=record.region,
        country_iso2=record.country_iso2,
        postal_code=record.postal_code,
        jurisdiction=record.jurisdiction,
        category=record.category,
        legal_form_id=record.legal_form_id,
        legal_form_other=record.legal_form_other,
        entity_status=record.entity_status,
        registration_status=record.registration_status,
        registration_last_update=record.registration_last_update,
        has_successor=record.has_successor,
        golden_copy_publish_date=record.golden_copy_publish_date,
        source_url=record.source_url,
        source_sha256=record.source_sha256,
    )


def _normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _registrant_override(
    security: RobinhoodSecurityConfig,
    identity: NcenFundIdentity,
    record: GleifRegistrantRecord,
) -> tuple[FaEntityOverride | None, str]:
    if not gleif_lifecycle_active(record, allow_lapsed=False):
        return None, "gleif-lifecycle"
    if record.category != "FUND" or _normalized_name(record.legal_name) != _normalized_name(
        identity.registrant_name
    ):
        return None, "registrant-identity-mismatch"
    return _filed_override(security, record, f"{record.legal_name} - {identity.series_name}")


def _unresolved_outcome(
    record: GleifLegalAddressRecord,
    security: RobinhoodSecurityConfig,
    reason: str,
) -> str:
    """Return ``lapsed`` only for a LAPSED record that fails nothing else."""

    if record.registration_status != "LAPSED":
        return reason
    override, lapsed_reason = _legal_override(record, security, allow_lapsed=True)
    return "lapsed" if override is not None else lapsed_reason


def resolve_robinhood_legal_entity_overrides(
    securities: Iterable[RobinhoodSecurityConfig],
    lookup: GleifLegalAddressLookup,
) -> tuple[tuple[FaEntityOverride, ...], GleifLegalAddressAudit, dict[Isin, str]]:
    """Resolve legal-address overrides without changing security identity.

    Also returns each requested ISIN's outcome: ``resolved``, the loader
    outcome, the failed check, or ``lapsed`` for a LAPSED record that passes
    every other check. The audit still counts the
    first failed check, so a LAPSED record with a valid LEI and legal name
    counts as ``lifecycle``.
    """

    security_by_isin = {item.instrument.isin: item for item in securities}
    resolved: list[FaEntityOverride] = []
    outcomes = dict(lookup.failures)
    failures: Counter[str] = Counter(dict(lookup.failure_counts))
    reviewed_ascii_aliases = 0
    for record in lookup.records:
        security = security_by_isin[record.isin]
        override, reason = _legal_override(record, security)
        if override is None:
            failures[reason] += 1
            outcomes[record.isin] = _unresolved_outcome(record, security, reason)
            continue
        if reason == "resolved-ascii-alias":
            reviewed_ascii_aliases += 1
        resolved.append(override)
        outcomes[record.isin] = "resolved"
    return (
        tuple(sorted(resolved, key=lambda item: item.isin)),
        GleifLegalAddressAudit(
            requested=lookup.requested,
            resolved=len(resolved),
            cache_hits=lookup.cache_hits,
            failure_counts=tuple(sorted(failures.items())),
            reviewed_ascii_aliases=reviewed_ascii_aliases,
        ),
        outcomes,
    )


def resolve_robinhood_adr_entity_overrides(
    securities: Iterable[RobinhoodSecurityConfig],
    lookup: RegistrantLookup[F6DepositaryIdentity],
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[GleifLegalAddressRecord, ...],
    RobinhoodAdrEntityAudit,
]:
    """Resolve ADR depositary overrides and their ISIN-bound GLEIF records."""

    security_by_isin = {item.instrument.isin: item for item in securities}
    overrides: list[FaEntityOverride] = []
    records: list[GleifLegalAddressRecord] = []
    failures: Counter[str] = Counter(dict(lookup.failure_counts))
    for isin, identity, record in lookup.evidence:
        security = security_by_isin[isin]
        override, reason = _depositary_override(security, identity, record)
        if override is None:
            failures[reason] += 1
            continue
        overrides.append(override)
        records.append(_legal_record(security, record))
    return (
        tuple(sorted(overrides, key=lambda item: item.isin)),
        tuple(sorted(records, key=lambda item: item.isin)),
        RobinhoodAdrEntityAudit(
            requested=lookup.requested,
            resolved=len(overrides),
            selection_cache_hits=lookup.selection_cache_hits,
            gleif_cache_hits=lookup.gleif_cache_hits,
            failure_counts=tuple(sorted(failures.items())),
        ),
    )


def resolve_robinhood_fund_entity_overrides(
    securities: Iterable[RobinhoodSecurityConfig],
    lookup: RegistrantLookup[NcenFundIdentity],
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[RobinhoodFundEntityEvidence, ...],
    RobinhoodFundEntityAudit,
]:
    """Resolve exact SEC-series ETF registrants and their GLEIF legal addresses."""

    security_by_isin = {item.instrument.isin: item for item in securities}
    overrides: list[FaEntityOverride] = []
    evidence: list[RobinhoodFundEntityEvidence] = []
    failures: Counter[str] = Counter(dict(lookup.failure_counts))
    for isin, identity, record in lookup.evidence:
        override, reason = _registrant_override(security_by_isin[isin], identity, record)
        if override is None:
            failures[reason] += 1
            continue
        overrides.append(override)
        assert override.address is not None  # _registrant_override always sets it
        evidence.append(
            RobinhoodFundEntityEvidence(
                isin=isin,
                name=override.name or "",
                address=override.address,
                zip_code=override.zip_code,
                cik=identity.cik,
                series_id=identity.series_id,
                class_id=identity.class_id,
                ticker=identity.ticker,
                ncen_accession=identity.accession,
                ncen_report_date=identity.report_date,
                registrant_lei=identity.registrant_lei,
                registrant_name=record.legal_name,
                series_name=identity.series_name,
                gleif_source_sha256=record.source_sha256,
            )
        )
    return (
        tuple(sorted(overrides, key=lambda item: item.isin)),
        tuple(sorted(evidence, key=lambda item: item.isin)),
        RobinhoodFundEntityAudit(
            requested=lookup.requested,
            resolved=len(overrides),
            selection_cache_hits=lookup.selection_cache_hits,
            gleif_cache_hits=lookup.gleif_cache_hits,
            failure_counts=tuple(sorted(failures.items())),
        ),
    )
