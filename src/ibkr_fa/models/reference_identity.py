"""Reference-source identity evidence for Robinhood securities (#634).

Nasdaq, SEC, GLEIF, Form F-6 and N-CEN records as the fetchers in
``ibkr_fa.robinhood_*_sources`` return them. Compute reads these records
without importing a network or cache module.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from ibkr_fa.types import Isin

SEC_SUBMISSION_URL_TEMPLATE: Final[str] = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
LEI_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Z0-9]{20}\Z")
# What an SEC lookup says when the run's fair-access contact is missing or
# would be refused: the key only, never its value (#1526).
SET_SEC_CONTACT: Final[str] = "set robinhood.sec_user_agent to a name and an email address"
# A Form F-6 or N-CEN fetch that failed for that reason alone.
F6_MISSING_CONTACT: Final[str] = "f6-missing-contact"
NCEN_MISSING_CONTACT: Final[str] = "ncen-missing-contact"


class NasdaqFlag(StrEnum):
    """A Nasdaq Trader ``Y``/``N``/blank field value, kept verbatim.

    The official field definitions allow a blank ("not available") for
    ``ETF`` and ``NextShares``; collapsing that to ``N`` would let the
    resolver read an unknown as a proved negative, so the blank is
    carried through as :attr:`UNAVAILABLE` instead.
    """

    YES = "Y"
    NO = "N"
    UNAVAILABLE = ""


@dataclass(frozen=True, slots=True, repr=False)
class ObservedIdentityInput:
    """One security's identity evidence as observed in broker documents.

    ``description`` is the raw source description; normalization for the
    fingerprint happens in
    :func:`ibkr_fa.robinhood_identity_sources.identity_input_fingerprint`.

    The repr is redacted: a CUSIP/symbol/description triple *is* a holding
    disclosure, and these objects travel through failure paths, tracebacks
    and debug logging.
    """

    cusip: str
    symbol: str
    description: str

    def __repr__(self) -> str:
        return "ObservedIdentityInput(<redacted>)"


@dataclass(frozen=True, slots=True)
class NasdaqTradedRecord:
    """One row of Nasdaq Trader's ``nasdaqtraded.txt`` symbol directory.

    Flags keep the upstream ``Y``/``N``/blank vocabulary (see
    :class:`NasdaqFlag`) so the cached snapshot reads as evidence of what
    the directory actually said. ``test_issue`` is documented as
    ``Y``/``N`` only, so a blank there is schema drift.
    """

    symbol: str
    name: str
    listing_exchange: str
    etf: NasdaqFlag
    test_issue: NasdaqFlag
    next_shares: NasdaqFlag


@dataclass(frozen=True, slots=True)
class SecTickerRecord:
    """One row of SEC ``company_tickers_exchange.json``.

    ``exchange`` is ``""`` when the upstream value is ``null`` (the file
    carries JSON nulls for filers with no listed exchange).
    """

    cik: int
    name: str
    symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class SecFundRecord:
    """One investment-company class row of SEC ``company_tickers_mf.json``."""

    cik: int
    series_id: str
    class_id: str
    symbol: str


@dataclass(frozen=True, slots=True)
class SecSubmissionRecord:
    """Filer metadata from one ``CIK##########.json`` submissions entry.

    ``forms`` is the sorted, de-duplicated set of every current and older
    filing ``form`` value present in that JSON — enough to prove the
    domestic filing family (10-K/10-Q/8-K vs 20-F/40-F) without keeping
    per-filing rows. Business-address fields are optional evidence: missing,
    null, or malformed upstream members become empty strings so one unusable
    address cannot invalidate otherwise sound security identity evidence.
    """

    cik: int
    name: str
    state_of_incorporation: str
    forms: tuple[str, ...]
    business_street1: str = ""
    business_street2: str = ""
    business_city: str = ""
    business_state_or_country: str = ""
    business_zip_code: str = ""


@dataclass(frozen=True, slots=True, repr=False)
class IdentitySourceBundle:
    """Normalized official records for exactly one identity fingerprint.

    Records are filtered to the observed input symbols (and the CIKs those
    symbols match): the snapshot is already input-specific, and keeping
    12,000 unrelated rows would bloat every artifact.

    ``nasdaq_by_symbol`` holds at most one row per symbol — a duplicate
    symbol anywhere in the upstream file is refused outright. The SEC
    mappings are tuple-valued: the upstream files may legitimately list a
    ticker under more than one filer, and preserving every candidate lets
    the resolver block that one security instead of silently picking one.

    The repr is count-only; the keys are the user's holdings.
    """

    nasdaq_by_symbol: Mapping[str, NasdaqTradedRecord]
    sec_tickers_by_symbol: Mapping[str, tuple[SecTickerRecord, ...]]
    sec_funds_by_symbol: Mapping[str, tuple[SecFundRecord, ...]]
    sec_submissions_by_cik: Mapping[int, SecSubmissionRecord]
    source_sha256: Mapping[str, str]

    def __repr__(self) -> str:
        return (
            "IdentitySourceBundle("
            f"nasdaq={len(self.nasdaq_by_symbol)}, "
            f"sec_tickers={len(self.sec_tickers_by_symbol)}, "
            f"sec_funds={len(self.sec_funds_by_symbol)}, "
            f"sec_submissions={len(self.sec_submissions_by_cik)}, "
            f"sources={len(self.source_sha256)})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class GleifLegalAddressRecord:
    """One normalized GLEIF legal-entity record bound to an exact ISIN.

    Every raw field it was built from had its expected JSON type; the loader
    refuses any other response (#821).
    """

    isin: Isin
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


@dataclass(frozen=True, slots=True, repr=False)
class GleifRegistrantRecord:
    """One GLEIF legal-entity record selected by exact registrant LEI."""

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


@dataclass(frozen=True, slots=True, repr=False)
class F6DepositaryIdentity:
    """The issuer and depositary named on one Form F-6 cover."""

    issuer_name: str
    depositary_name: str
    depositary_lei: str
    document_url: str
    document_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class NcenFundIdentity:
    """One exact class-bound N-CEN identity selected for a reporting cutoff."""

    cik: int
    series_id: str
    class_id: str
    ticker: str
    registrant_name: str
    registrant_lei: str
    series_name: str
    series_lei: str
    class_name: str
    accession: str
    form: str
    report_date: date
    filing_date: date
    acceptance_datetime: str
    document_url: str
    document_sha256: str


@dataclass(frozen=True, slots=True, repr=False)
class GleifLegalAddressLookup:
    """Every exact-ISIN GLEIF record found, and each other requested ISIN's outcome.

    ``failures`` maps each requested ISIN without exactly one record to its
    loader outcome (``zero-match``, ``multi-match``, ``cache-miss`` or
    ``adr-excluded``).
    """

    records: tuple[GleifLegalAddressRecord, ...]
    failures: Mapping[Isin, str]
    cache_hits: int

    @property
    def requested(self) -> int:
        return len(self.records) + len(self.failures)

    @property
    def failure_counts(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(Counter(self.failures.values()).items()))


@dataclass(frozen=True, slots=True, repr=False)
class RegistrantLookup[IdentityT]:
    """Per-ISIN filing identities with their exact-LEI GLEIF records, plus fetch counts."""

    evidence: tuple[tuple[Isin, IdentityT, GleifRegistrantRecord], ...]
    requested: int
    selection_cache_hits: int
    gleif_cache_hits: int
    failure_counts: tuple[tuple[str, int], ...] = ()


def name_tokens(value: str) -> list[str]:
    """Return the upper-cased alphanumeric runs of one entity name."""

    return re.findall(r"[A-Z0-9]+", value.upper())


def depositary_key(value: str) -> tuple[str, ...]:
    """Return the comparison key for one depositary bank name."""

    tokens = name_tokens(value)
    if tokens[-2:] == ["N", "A"]:
        tokens[-2:] = ["NATIONAL", "ASSOCIATION"]
    return tuple(tokens)
