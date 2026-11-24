"""Resolve Robinhood A3 legal nature from GLEIF ELF and SEC fund evidence."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Final

from ibkr_fa.compute.robinhood_entity_overrides import (
    RobinhoodFundEntityAudit,
    RobinhoodFundEntityEvidence,
    gleif_lifecycle_active,
    raw_portal_safe,
)
from ibkr_fa.compute.robinhood_identity import (
    SEC_ANNUAL_FORMS,
    US_STATE_CODES,
    is_us_common_stock,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import GleifLegalAddressRecord, IdentitySourceBundle
from ibkr_fa.reference.gleif_elf import lookup_gleif_elf
from ibkr_fa.types import Isin

_NATURE_MAX_LENGTH: Final[int] = 34
_FUND_OTHER: Final[dict[str, str]] = {"fund": "Fund"}
_OVERFLOW_ALIASES: Final[dict[str, str]] = {
    "For-Profit Corporation General Stock": "For-Profit Corporation",
}
_INVESTMENT_COMPANY_FORMS: Final[frozenset[str]] = frozenset(
    {"N-CEN", "N-CSR", "N-CSRS", "NPORT-P"}
)
_CORPORATION_NAME_TOKENS: Final[frozenset[str]] = frozenset(
    {"INC", "INCORPORATED", "CORP", "CORPORATION"}
)


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodEntityNatureEvidence:
    """One automatic nature value plus exact public-source provenance."""

    isin: Isin
    value: str
    source: str
    transformation: str
    raw_value: str
    lei: str = ""
    elf_code: str = ""
    sec_cik: int | None = None
    sec_series_id: str = ""
    sec_class_id: str = ""
    sec_forms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RobinhoodEntityNatureAudit:
    """Privacy-safe automatic nature coverage."""

    resolved: int
    failure_counts: tuple[tuple[str, int], ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        if not self.failure_counts:
            return ()
        details = ", ".join(f"{name}:{count}" for name, count in self.failure_counts)
        return (
            "legal nature remains manual for "
            f"{sum(count for _name, count in self.failure_counts)} securities "
            f"({details})",
        )


def _portal_safe(value: str) -> bool:
    return raw_portal_safe(value, max_length=_NATURE_MAX_LENGTH)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(value.casefold().split())


def _is_contiguous_subsequence(alias: str, raw: str) -> bool:
    alias_tokens = _tokens(alias)
    raw_tokens = _tokens(raw)
    return bool(alias_tokens) and any(
        raw_tokens[index : index + len(alias_tokens)] == alias_tokens
        for index in range(len(raw_tokens) - len(alias_tokens) + 1)
    )


def _gleif_evidence(  # noqa: PLR0911
    record: GleifLegalAddressRecord,
    security: RobinhoodSecurityConfig,
) -> tuple[RobinhoodEntityNatureEvidence | None, str]:
    if not gleif_lifecycle_active(record, allow_lapsed=True):
        return None, "lifecycle"
    if record.country_iso2 != security.instrument.issuer_country_code:
        return None, "country-mismatch"
    lapsed = record.registration_status == "LAPSED"
    if record.legal_form_id == "9999":
        return None, "no-legal-form"
    if record.legal_form_id == "8888":
        value = (
            _FUND_OTHER.get(record.legal_form_other.casefold())
            if record.category == "FUND"
            else None
        )
        if value is None or not _portal_safe(value):
            return None, "reserved-form"
        return (
            RobinhoodEntityNatureEvidence(
                isin=security.instrument.isin,
                value=value,
                source=("gleif-legal-form-other-lapsed" if lapsed else "gleif-legal-form-other"),
                transformation=(
                    "reviewed-case-normalization-lapsed-record"
                    if lapsed
                    else "reviewed-case-normalization"
                ),
                raw_value=record.legal_form_other,
                lei=record.lei,
                elf_code=record.legal_form_id,
            ),
            "resolved",
        )
    entry = lookup_gleif_elf(record.legal_form_id)
    if (
        entry is None
        or entry.status != "ACTV"
        or not entry.english_name
        or (entry.country_iso2 and entry.country_iso2 != record.country_iso2)
    ):
        return None, "elf-label-unavailable"
    raw = " ".join(entry.english_name.split())
    if _portal_safe(raw):
        return (
            RobinhoodEntityNatureEvidence(
                isin=security.instrument.isin,
                value=raw,
                source="gleif-elf-lapsed" if lapsed else "gleif-elf",
                transformation="verbatim-lapsed-record" if lapsed else "verbatim",
                raw_value=raw,
                lei=record.lei,
                elf_code=record.legal_form_id,
            ),
            "resolved",
        )
    alias = _OVERFLOW_ALIASES.get(raw)
    if alias is None or not _portal_safe(alias) or not _is_contiguous_subsequence(alias, raw):
        return None, "elf-label-unusable"
    return (
        RobinhoodEntityNatureEvidence(
            isin=security.instrument.isin,
            value=alias,
            source="gleif-elf-lapsed" if lapsed else "gleif-elf",
            transformation=(
                "reviewed-overflow-alias-lapsed-record" if lapsed else "reviewed-overflow-alias"
            ),
            raw_value=raw,
            lei=record.lei,
            elf_code=record.legal_form_id,
        ),
        "resolved",
    )


def _sec_fund_evidence(
    security: RobinhoodSecurityConfig,
    sources: IdentitySourceBundle | None,
) -> RobinhoodEntityNatureEvidence | None:
    if sources is None:
        return None
    fund_rows = sources.sec_funds_by_symbol.get(security.instrument.symbol, ())
    if len(fund_rows) != 1:
        return None
    fund = fund_rows[0]
    submission = sources.sec_submissions_by_cik.get(fund.cik)
    if submission is None:
        return None
    matched_forms = tuple(sorted(set(submission.forms) & _INVESTMENT_COMPANY_FORMS))
    if not matched_forms:
        return None
    return RobinhoodEntityNatureEvidence(
        isin=security.instrument.isin,
        value="Investment Company",
        source="sec-registered-investment-company",
        transformation="exact-filing-form-classification",
        raw_value="Investment Company",
        sec_cik=fund.cik,
        sec_series_id=fund.series_id,
        sec_class_id=fund.class_id,
        sec_forms=matched_forms,
    )


def _etf_evidence(
    security: RobinhoodSecurityConfig,
    sources: IdentitySourceBundle | None,
    registrant: RobinhoodFundEntityEvidence | None,
) -> tuple[RobinhoodEntityNatureEvidence | None, str]:
    evidence = _sec_fund_evidence(security, sources)
    if evidence is None:
        return None, "sec-fund-unproved"
    if registrant is None:
        return None, "registrant-unresolved"
    if (evidence.sec_cik, evidence.sec_series_id, evidence.sec_class_id) != (
        registrant.cik,
        registrant.series_id,
        registrant.class_id,
    ):
        return None, "registrant-mismatch"
    return evidence, "resolved"


def _sec_common_evidence(
    security: RobinhoodSecurityConfig,
    sources: IdentitySourceBundle | None,
) -> RobinhoodEntityNatureEvidence | None:
    if sources is None or not is_us_common_stock(security):
        return None
    ticker_rows = sources.sec_tickers_by_symbol.get(security.instrument.symbol, ())
    if len(ticker_rows) != 1:
        return None
    ticker = ticker_rows[0]
    submission = sources.sec_submissions_by_cik.get(ticker.cik)
    if (
        submission is None
        or submission.state_of_incorporation not in US_STATE_CODES
        or not set(submission.forms) & SEC_ANNUAL_FORMS
        or not any(
            set(re.findall(r"[A-Z0-9]+", name.upper())) & _CORPORATION_NAME_TOKENS
            for name in (ticker.name, submission.name)
        )
    ):
        return None
    forms = tuple(sorted(set(submission.forms) & SEC_ANNUAL_FORMS))
    return RobinhoodEntityNatureEvidence(
        isin=security.instrument.isin,
        value="Corporation",
        source="sec-state-of-incorporation",
        transformation="exact-incorporation-classification",
        raw_value=submission.state_of_incorporation,
        sec_cik=ticker.cik,
        sec_forms=forms,
    )


def resolve_robinhood_entity_natures(  # noqa: PLR0912
    securities: tuple[RobinhoodSecurityConfig, ...],
    configured: tuple[FaEntityOverride, ...],
    gleif_records: tuple[GleifLegalAddressRecord, ...],
    sec_sources: IdentitySourceBundle | None,
    *,
    depositary_isins: frozenset[Isin] = frozenset(),
    sec_common_isins: frozenset[Isin] = frozenset(),
    lapsed_gleif_isins: frozenset[Isin] = frozenset(),
    fund_evidence: tuple[RobinhoodFundEntityEvidence, ...] = (),
) -> tuple[tuple[RobinhoodEntityNatureEvidence, ...], RobinhoodEntityNatureAudit]:
    """Resolve source-first legal nature independently from address evidence.

    A series ETF is the exception (#793): its nature comes only from SEC fund
    evidence for the same CIK, series and class as its accepted N-CEN
    registrant in ``fund_evidence``, and a configured ETF gets none.
    """

    configured_by_isin = {item.isin: item for item in configured}
    records_by_isin = {item.isin: item for item in gleif_records}
    funds_by_isin = {item.isin: item for item in fund_evidence}
    result: list[RobinhoodEntityNatureEvidence] = []
    failures: Counter[str] = Counter()
    for security in sorted(securities, key=lambda item: item.instrument.isin):
        manual = configured_by_isin.get(security.instrument.isin)
        if security.sub_category == "ETF" and not security.is_depositary_receipt:
            if manual is None:
                evidence, reason = _etf_evidence(
                    security,
                    sec_sources,
                    funds_by_isin.get(security.instrument.isin),
                )
                if evidence is None:
                    failures[reason] += 1
                else:
                    result.append(evidence)
            continue
        if manual is not None and manual.prefer_configured_nature:
            continue
        if security.is_depositary_receipt and security.instrument.isin not in depositary_isins:
            failures["adr-excluded"] += 1
            continue
        evidence = None
        reason = "gleif-unavailable"
        record = records_by_isin.get(security.instrument.isin)
        if record is not None and (
            record.registration_status != "LAPSED" or security.instrument.isin in lapsed_gleif_isins
        ):
            evidence, reason = _gleif_evidence(record, security)
        elif record is not None:
            reason = "lifecycle"
        if evidence is None:
            evidence = _sec_fund_evidence(security, sec_sources)
        if evidence is None and security.instrument.isin in sec_common_isins:
            evidence = _sec_common_evidence(security, sec_sources)
        if evidence is None:
            failures[reason] += 1
        else:
            result.append(evidence)
    return (
        tuple(sorted(result, key=lambda item: item.isin)),
        RobinhoodEntityNatureAudit(
            resolved=len(result),
            failure_counts=tuple(sorted(failures.items())),
        ),
    )


def bind_robinhood_fund_entities(
    overrides: tuple[FaEntityOverride, ...],
    evidence: tuple[RobinhoodFundEntityEvidence, ...],
    audit: RobinhoodFundEntityAudit,
    natures: tuple[RobinhoodEntityNatureEvidence, ...],
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[RobinhoodFundEntityEvidence, ...],
    RobinhoodFundEntityAudit,
]:
    """Keep an ETF registrant's name, address and ZIP only beside its bound nature (#793)."""

    bound = {item.isin for item in natures}
    unbound = sum(1 for item in evidence if item.isin not in bound)
    if not unbound:
        return overrides, evidence, audit
    failures = Counter(dict(audit.failure_counts))
    failures["nature-unbound"] += unbound
    return (
        tuple(item for item in overrides if item.isin in bound),
        tuple(item for item in evidence if item.isin in bound),
        replace(
            audit,
            resolved=audit.resolved - unbound,
            failure_counts=tuple(sorted(failures.items())),
        ),
    )


def merge_robinhood_entity_natures(
    automatic: tuple[RobinhoodEntityNatureEvidence, ...],
    configured: tuple[FaEntityOverride, ...],
) -> dict[Isin, str]:
    """Use automatic nature first unless the configured value is preferred."""

    merged = {item.isin: item.value for item in automatic}
    for item in configured:
        if item.nature_of_entity is not None and (
            item.prefer_configured_nature or item.isin not in merged
        ):
            merged[item.isin] = item.nature_of_entity
    return merged


def robinhood_entity_nature_supersession_warnings(
    automatic: tuple[RobinhoodEntityNatureEvidence, ...],
    configured: tuple[FaEntityOverride, ...],
) -> tuple[str, ...]:
    """Count the configured nature values the merge replaces."""

    automatic_nature = {item.isin: item.value for item in automatic}
    superseded = sum(
        1
        for item in configured
        if item.nature_of_entity is not None
        and item.isin in automatic_nature
        and not item.prefer_configured_nature
        and item.nature_of_entity != automatic_nature[item.isin]
    )
    if not superseded:
        return ()
    return (
        "automatic legal-form evidence superseded "
        f"{superseded} configured Robinhood nature value"
        f"{'s' if superseded != 1 else ''}; set "
        "prefer_configured_nature = true to retain reviewed values",
    )
