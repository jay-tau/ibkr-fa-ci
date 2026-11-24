"""Resolve standard Robinhood security identities from official source records."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ibkr_fa.compute.robinhood_entity_overrides import (
    A3_ADDRESS_MAX_LENGTH,
    A3_ZIP_MAX_LENGTH,
    SEC_FALLBACK_GLEIF_OUTCOMES,
    gleif_lifecycle_active,
    raw_portal_safe,
)
from ibkr_fa.compute.robinhood_security_identities import RobinhoodSecurityIdentities
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodSecurityConfig,
)
from ibkr_fa.models.instrument_identity import InstrumentIdentity
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifLegalAddressRecord,
    IdentitySourceBundle,
    ObservedIdentityInput,
)
from ibkr_fa.normalize.robinhood.accounts import (
    RobinhoodAccountsReconciliation,
)
from ibkr_fa.normalize.robinhood.security import (
    canonical_statement_symbols,
    observed_symbol,
    replaced_statement_cusips,
)
from ibkr_fa.types import CountryCode, Isin, Ticker, cusip_check_digit_matches, isin_check_digit

_DESCRIPTION_SEPARATOR: Final[str] = "\x1f"
_CUSIP_LENGTH: Final[int] = 9
_NAME_TOKEN = re.compile(r"[A-Z0-9]+")
_NAME_NOISE: Final[frozenset[str]] = frozenset(
    {
        "A",
        "CLASS",
        "COMMON",
        "COMPANY",
        "CORP",
        "CORPORATION",
        "INC",
        "INCORPORATED",
        "SHARE",
        "SHARES",
        "STOCK",
        "THE",
    }
)
_UNSUPPORTED_NAME_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "ADR",
        "ADS",
        "BENEFICIAL",
        "DEPOSITARY",
        "DEPOSITORY",
        "ETN",
        "NOTE",
        "NOTES",
        "ORDINARY",
        "PREFERRED",
        "RIGHT",
        "RIGHTS",
        "UNIT",
        "UNITS",
        "WARRANT",
        "WARRANTS",
    }
)
US_STATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "AK",
        "AL",
        "AR",
        "AS",
        "AZ",
        "CA",
        "CO",
        "CT",
        "DC",
        "DE",
        "FL",
        "GA",
        "GU",
        "HI",
        "IA",
        "ID",
        "IL",
        "IN",
        "KS",
        "KY",
        "LA",
        "MA",
        "MD",
        "ME",
        "MI",
        "MN",
        "MO",
        "MP",
        "MS",
        "MT",
        "NC",
        "ND",
        "NE",
        "NH",
        "NJ",
        "NM",
        "NV",
        "NY",
        "OH",
        "OK",
        "OR",
        "PA",
        "PR",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VA",
        "VI",
        "VT",
        "WA",
        "WI",
        "WV",
        "WY",
    }
)
_NASDAQ_EXCHANGE: Final[dict[str, str]] = {
    "A": "NYSE AMERICAN",
    "N": "NYSE",
    "P": "ARCA",
    "Q": "NASDAQ",
    "V": "IEX",
    "Z": "CBOE",
}
_SEC_EXCHANGE: Final[dict[str, str]] = {
    "Nasdaq": "NASDAQ",
    "NYSE": "NYSE",
    "CBOE": "CBOE",
}
_SEC_EXCHANGE_COMPATIBLE: Final[dict[str, frozenset[str]]] = {
    "NASDAQ": frozenset({"NASDAQ"}),
    "NYSE": frozenset({"NYSE"}),
    "NYSE AMERICAN": frozenset({"NYSE"}),
    "ARCA": frozenset({"NYSE"}),
    "CBOE": frozenset({"CBOE"}),
    "IEX": frozenset({"IEX"}),
}
_US_ZIP_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A(\d{5})(?:-\d{4})?\Z")
SEC_ANNUAL_FORMS: Final[frozenset[str]] = frozenset({"10-K", "10-K/A"})


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodIdentityFailure:
    """One unresolved CUSIP with machine-checked failed requirements."""

    cusip: str
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodIdentityResolution:
    """Generated standard identities plus explicit unresolved securities."""

    generated: tuple[RobinhoodSecurityConfig, ...]
    failures: tuple[RobinhoodIdentityFailure, ...]


@dataclass(frozen=True, slots=True, repr=False)
class RobinhoodSecEntityEvidence:
    """One SEC business-address fallback with immutable source provenance."""

    isin: Isin
    cik: int
    address: str
    zip_code: str
    source_url: str
    source_sha256: str
    zip_transformation: str
    name_source: str


@dataclass(frozen=True, slots=True)
class RobinhoodSecEntityAudit:
    """Privacy-safe SEC business-address fallback coverage.

    ``gleif_blocked_counts`` counts the candidates whose GLEIF outcome kept
    them from the fallback, by that outcome.
    """

    resolved: int
    failure_counts: tuple[tuple[str, int], ...] = ()
    zip5_from_zip4: int = 0
    gleif_blocked_counts: tuple[tuple[str, int], ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        result = []
        if self.resolved:
            result.append(
                f"SEC business-address fallback supplied {self.resolved} "
                f"securit{'y' if self.resolved == 1 else 'ies'}"
            )
        for counts, message in (
            (self.gleif_blocked_counts, "GLEIF evidence blocked the SEC business-address fallback"),
            (self.failure_counts, "SEC business-address fallback supplied no address"),
        ):
            if counts:
                total = sum(count for _name, count in counts)
                details = ", ".join(f"{name}:{count}" for name, count in counts)
                result.append(
                    f"{message} for {total} securit{'y' if total == 1 else 'ies'} ({details})"
                )
        if self.zip5_from_zip4:
            result.append(
                f"{self.zip5_from_zip4} SEC ZIP+4 value"
                f"{' was' if self.zip5_from_zip4 == 1 else 's were'} reduced to ZIP5; "
                "the exact source remains in the immutable cache"
            )
        return tuple(result)


def _normalised_name(value: str) -> tuple[str, ...]:
    return tuple(token for token in _NAME_TOKEN.findall(value.upper()) if token not in _NAME_NOISE)


def _name_agrees(left: str, right: str) -> bool:
    left_tokens = _normalised_name(left)
    right_tokens = _normalised_name(right)
    if not left_tokens or not right_tokens:
        return False
    left_set = set(left_tokens)
    right_set = set(right_tokens)
    return left_set <= right_set or right_set <= left_set


def _broker_name_agrees_with_nasdaq(broker: str, nasdaq: str) -> bool:
    """Require ``_name_agrees`` and nonempty broker words after dropping noise
    and ``CO``. Every remaining broker word must be in the normalized Nasdaq
    name; Nasdaq may add words (class wording, "Units")."""
    broker_tokens = set(_normalised_name(broker)) - {"CO"}
    return (
        _name_agrees(broker, nasdaq)
        and bool(broker_tokens)
        and broker_tokens <= set(_normalised_name(nasdaq))
    )


def _strict_name_agrees(left: str, right: str) -> bool:
    left_tokens = set(_normalised_name(left)) - {"CO"}
    right_tokens = set(_normalised_name(right)) - {"CO"}
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    smaller, larger = (
        (left_tokens, right_tokens)
        if len(left_tokens) < len(right_tokens)
        else (right_tokens, left_tokens)
    )
    return smaller < larger and larger - smaller <= US_STATE_CODES


def _observed_names(item: ObservedIdentityInput) -> tuple[str, ...]:
    return tuple(part for part in item.description.split(_DESCRIPTION_SEPARATOR) if part)


def is_us_common_stock(security: RobinhoodSecurityConfig) -> bool:
    """Whether ``security`` is a US issuer's common stock, not a depositary
    receipt: the only securities SEC common-stock evidence covers."""
    return (
        not security.is_depositary_receipt
        and security.sub_category == "COMMON"
        and security.asset_class == "STK"
        and security.instrument.issuer_country_code == "US"
    )


def _sec_entity_override(  # noqa: PLR0911
    security: RobinhoodSecurityConfig,
    source_identity: ObservedIdentityInput | None,
    sources: IdentitySourceBundle,
    gleif: GleifLegalAddressRecord | None,
) -> tuple[FaEntityOverride | None, RobinhoodSecEntityEvidence | None, str]:
    if not is_us_common_stock(security):
        return None, None, "unsupported-security"
    sec_rows = sources.sec_tickers_by_symbol.get(security.instrument.symbol, ())
    nasdaq = sources.nasdaq_by_symbol.get(security.instrument.symbol)
    if (
        source_identity is None
        or source_identity.symbol != security.instrument.symbol
        or len(sec_rows) != 1
        or nasdaq is None
    ):
        return None, None, "identity-mismatch"
    sec = sec_rows[0]
    listing_exchange = _NASDAQ_EXCHANGE.get(nasdaq.listing_exchange)
    sec_exchange = _SEC_EXCHANGE.get(sec.exchange)
    if (
        listing_exchange != security.instrument.listing_exchange
        or sec_exchange
        not in _SEC_EXCHANGE_COMPATIBLE.get(security.instrument.listing_exchange, ())
        or not _name_agrees(nasdaq.name, sec.name)
    ):
        return None, None, "identity-mismatch"
    submission = sources.sec_submissions_by_cik.get(sec.cik)
    # Every broker name observed for the CUSIP must agree, and there must be
    # one: ``all`` over no names is true.
    observed_names = _observed_names(source_identity)
    observed_match = (
        bool(observed_names)
        and submission is not None
        and all(
            _strict_name_agrees(name, sec.name) and _strict_name_agrees(name, submission.name)
            for name in observed_names
        )
    )
    gleif_match = (
        gleif is not None
        and gleif_lifecycle_active(gleif, allow_lapsed=True)
        and gleif.country_iso2 == security.instrument.issuer_country_code
        and _strict_name_agrees(gleif.legal_name, sec.name)
        and submission is not None
        and _strict_name_agrees(gleif.legal_name, submission.name)
    )
    # A GLEIF legal name, when there is one, must agree itself: broker
    # agreement never overrides a conflicting GLEIF record (#792).
    if submission is None or not (gleif_match if gleif is not None else observed_match):
        return None, None, "name-mismatch"
    if not set(submission.forms) & SEC_ANNUAL_FORMS:
        return None, None, "filing-unavailable"
    if submission.business_state_or_country not in US_STATE_CODES:
        return None, None, "business-country"
    zip_match = _US_ZIP_PATTERN.fullmatch(submission.business_zip_code)
    if not submission.business_street1 or not submission.business_city or zip_match is None:
        return None, None, "address-incomplete"
    address = ", ".join(
        part
        for part in (
            submission.business_street1,
            submission.business_street2,
            submission.business_city,
            f"US-{submission.business_state_or_country}",
            "US",
        )
        if part
    )
    zip_code = zip_match.group(1)
    if not raw_portal_safe(address, max_length=A3_ADDRESS_MAX_LENGTH) or not raw_portal_safe(
        zip_code, max_length=A3_ZIP_MAX_LENGTH
    ):
        return None, None, "portal-shape"
    url = SEC_SUBMISSION_URL_TEMPLATE.format(cik=sec.cik)
    digest = sources.source_sha256.get(url)
    if digest is None:
        return None, None, "source-digest"
    transformation = (
        "us-zip5-from-zip-plus4" if submission.business_zip_code != zip_code else "verbatim"
    )
    if observed_match:
        name_source = "broker"
    else:
        assert gleif is not None
        name_source = f"gleif-{gleif.registration_status.casefold()}"
    return (
        FaEntityOverride(
            isin=security.instrument.isin,
            address=address,
            zip_code=zip_code,
        ),
        RobinhoodSecEntityEvidence(
            isin=security.instrument.isin,
            cik=sec.cik,
            address=address,
            zip_code=zip_code,
            source_url=url,
            source_sha256=digest,
            zip_transformation=transformation,
            name_source=name_source,
        ),
        transformation,
    )


def resolve_robinhood_sec_entity_overrides(
    securities: tuple[RobinhoodSecurityConfig, ...],
    observed: tuple[ObservedIdentityInput, ...],
    sources: IdentitySourceBundle | None,
    gleif_records: tuple[GleifLegalAddressRecord, ...],
    gleif_outcomes: Mapping[Isin, str],
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[RobinhoodSecEntityEvidence, ...],
    RobinhoodSecEntityAudit,
]:
    """Resolve exact US common-stock SEC business addresses as an A3 fallback.

    Only a security whose GLEIF outcome is in ``SEC_FALLBACK_GLEIF_OUTCOMES``
    reaches the SEC checks; every other one is counted by that outcome.
    """

    observed_by_cusip = {item.cusip: item for item in observed}
    gleif_by_isin = {item.isin: item for item in gleif_records}
    overrides: list[FaEntityOverride] = []
    evidence: list[RobinhoodSecEntityEvidence] = []
    failures: Counter[str] = Counter()
    blocked = Counter(
        gleif_outcomes[item.instrument.isin]
        for item in securities
        if gleif_outcomes[item.instrument.isin] not in SEC_FALLBACK_GLEIF_OUTCOMES
    )
    eligible = tuple(
        item
        for item in securities
        if gleif_outcomes[item.instrument.isin] in SEC_FALLBACK_GLEIF_OUTCOMES
    )
    gleif_blocked_counts = tuple(sorted(blocked.items()))
    zip5_from_zip4 = 0
    if not eligible:
        return (), (), RobinhoodSecEntityAudit(0, gleif_blocked_counts=gleif_blocked_counts)
    if sources is None:
        return (
            (),
            (),
            RobinhoodSecEntityAudit(
                resolved=0,
                failure_counts=(("source-unavailable", len(eligible)),),
                gleif_blocked_counts=gleif_blocked_counts,
            ),
        )
    for security in sorted(eligible, key=lambda item: item.instrument.isin):
        override, item, reason = _sec_entity_override(
            security,
            observed_by_cusip.get(security.cusip),
            sources,
            gleif_by_isin.get(security.instrument.isin),
        )
        if override is None or item is None:
            failures[reason] += 1
            continue
        zip5_from_zip4 += reason != "verbatim"
        overrides.append(override)
        evidence.append(item)
    return (
        tuple(overrides),
        tuple(evidence),
        RobinhoodSecEntityAudit(
            resolved=len(overrides),
            failure_counts=tuple(sorted(failures.items())),
            zip5_from_zip4=zip5_from_zip4,
            gleif_blocked_counts=gleif_blocked_counts,
        ),
    )


def observed_robinhood_identity_inputs(
    paired: RobinhoodAccountsReconciliation,
) -> tuple[ObservedIdentityInput, ...]:
    """Return deterministic CUSIP/symbol/name evidence from broker sources.

    Each CUSIP's symbol is the private guidance's: ``observed_symbol`` over
    the canonical statement symbols, activity symbols and replaced CUSIPs,
    or "" when that names none (#1187).
    """

    symbols: dict[str, set[str]] = {}
    descriptions: dict[str, set[str]] = {}
    for account in paired.accounts:
        for event in account.evidence.events:
            if event.cusip is None:
                continue
            symbols.setdefault(event.cusip, set())
            descriptions.setdefault(event.cusip, set())
            if event.instrument:
                symbols[event.cusip].add(event.instrument)
            if event.issuer_name:
                descriptions[event.cusip].add(event.issuer_name)
    statement_symbols = canonical_statement_symbols(paired)
    replaced = replaced_statement_cusips(paired)
    result = []
    for cusip in sorted(symbols):
        symbol = observed_symbol(cusip, statement_symbols, symbols, replaced)
        description = _DESCRIPTION_SEPARATOR.join(sorted(descriptions[cusip]))
        result.append(ObservedIdentityInput(cusip, "" if symbol is None else symbol, description))
    return tuple(result)


def _resolve_one(  # noqa: PLR0912
    item: ObservedIdentityInput,
    sources: IdentitySourceBundle,
) -> RobinhoodSecurityConfig | RobinhoodIdentityFailure:
    checks: list[str] = []
    if not item.symbol:
        checks.append("broker-symbol-ambiguous")
    if len(item.cusip) != _CUSIP_LENGTH or not cusip_check_digit_matches(item.cusip):
        checks.append("cusip-invalid")
    nasdaq = sources.nasdaq_by_symbol.get(item.symbol)
    if nasdaq is None:
        checks.append("nasdaq-symbol-missing")
    elif (
        nasdaq.test_issue != "N"
        or nasdaq.next_shares != "N"
        or nasdaq.listing_exchange not in _NASDAQ_EXCHANGE
    ):
        checks.append("nasdaq-security-unsupported")
    etf = nasdaq is not None and nasdaq.etf == "Y"
    sec = None
    if not etf:
        sec_rows = sources.sec_tickers_by_symbol.get(item.symbol, ())
        if len(sec_rows) != 1:
            checks.append("sec-ticker-not-unique")
        else:
            sec = sec_rows[0]
    observed_names = _observed_names(item)
    if not observed_names:
        checks.append("broker-description-missing")
    # Every observed name must agree, so a conflicting name is never outvoted.
    if (
        nasdaq is not None
        and observed_names
        and not all(_broker_name_agrees_with_nasdaq(name, nasdaq.name) for name in observed_names)
    ):
        checks.append("nasdaq-name-mismatch")
    if (
        sec is not None
        and observed_names
        and not all(_name_agrees(name, sec.name) for name in observed_names)
    ):
        checks.append("sec-name-mismatch")
    if nasdaq is not None and sec is not None and not _name_agrees(nasdaq.name, sec.name):
        checks.append("source-name-mismatch")
    listing_exchange = _NASDAQ_EXCHANGE.get(nasdaq.listing_exchange) if nasdaq is not None else None
    sec_exchange = _SEC_EXCHANGE.get(sec.exchange) if sec is not None else None
    if sec is not None and (
        listing_exchange is None
        or sec_exchange not in _SEC_EXCHANGE_COMPATIBLE.get(listing_exchange, ())
    ):
        checks.append("exchange-mismatch")

    if nasdaq is not None and nasdaq.etf not in {"Y", "N"}:
        checks.append("etf-flag-invalid")
    name_tokens = set(_NAME_TOKEN.findall(nasdaq.name.upper())) if nasdaq else set()
    if not etf and name_tokens & _UNSUPPORTED_NAME_TOKENS:
        checks.append("security-type-unsupported")
    if etf:
        fund_rows = sources.sec_funds_by_symbol.get(item.symbol, ())
        if len(fund_rows) != 1:
            checks.append("sec-fund-missing")
    elif sec is not None:
        submission = sources.sec_submissions_by_cik.get(sec.cik)
        if (
            submission is None
            or submission.state_of_incorporation not in US_STATE_CODES
            or not any(form in SEC_ANNUAL_FORMS for form in submission.forms)
        ):
            checks.append("us-jurisdiction-unproved")

    if checks:
        return RobinhoodIdentityFailure(item.cusip, tuple(sorted(set(checks))))
    assert nasdaq is not None and listing_exchange is not None
    isin_body = f"US{item.cusip}"
    source_id = hashlib.sha256(
        "\n".join(
            f"{url}={digest}" for url, digest in sorted(sources.source_sha256.items())
        ).encode()
    ).hexdigest()[:12]
    return RobinhoodSecurityConfig(
        instrument=InstrumentIdentity(
            symbol=Ticker(item.symbol),
            isin=Isin(f"{isin_body}{isin_check_digit(isin_body)}"),
            listing_exchange=listing_exchange,
            issuer_country_code=CountryCode("US"),
        ),
        cusip=item.cusip,
        sub_category="ETF" if etf else "COMMON",
        asset_class="STK",
        is_depositary_receipt=False,
        source=f"automatic official identity snapshot {source_id}",
    )


def robinhood_identity_manual_fields(
    securities: tuple[RobinhoodSecurityConfig, ...],
) -> dict[str, dict[str, str]]:
    """The configured filing fields of each security, keyed by symbol.

    The identity-cache fingerprint covers these keys and string values, so
    changing either invalidates every cached identity snapshot.
    """

    return {
        item.instrument.symbol: {
            "cusip": item.cusip,
            "isin": str(item.instrument.isin),
            "listing_exchange": item.instrument.listing_exchange,
            "sub_category": item.sub_category,
            "asset_class": item.asset_class,
            "issuer_country_code": item.instrument.issuer_country_code,
            "is_depositary_receipt": str(item.is_depositary_receipt),
        }
        for item in securities
    }


def resolve_robinhood_standard_identities(
    inputs: tuple[ObservedIdentityInput, ...],
    sources: IdentitySourceBundle,
    configured: tuple[RobinhoodSecurityConfig, ...],
) -> RobinhoodIdentityResolution:
    """Resolve missing standard identities; manual CUSIPs remain untouched."""

    manual = {security.cusip for security in configured}
    generated: list[RobinhoodSecurityConfig] = []
    failures: list[RobinhoodIdentityFailure] = []
    for item in inputs:
        if item.cusip in manual:
            continue
        result = _resolve_one(item, sources)
        if isinstance(result, RobinhoodSecurityConfig):
            generated.append(result)
        else:
            failures.append(result)
    return RobinhoodIdentityResolution(
        generated=tuple(sorted(generated, key=lambda item: item.cusip)),
        failures=tuple(sorted(failures, key=lambda item: item.cusip)),
    )


def merge_robinhood_entity_overrides(
    automatic: tuple[FaEntityOverride, ...],
    configured: tuple[FaEntityOverride, ...],
) -> dict[Isin, FaEntityOverride]:
    """Use exact legal addresses unless a configured pair is explicitly preferred."""

    merged = {item.isin: item for item in configured}
    for generated in automatic:
        manual = merged.get(generated.isin)
        merged[generated.isin] = FaEntityOverride(
            isin=generated.isin,
            address=(
                manual.address
                if manual is not None and manual.prefer_configured_address
                else generated.address
            ),
            zip_code=(
                manual.zip_code
                if manual is not None and manual.prefer_configured_address
                else generated.zip_code
            ),
            name=(
                manual.name
                if manual is not None and manual.prefer_configured_name
                else generated.name or (None if manual is None else manual.name)
            ),
            nature_of_entity=(
                manual.nature_of_entity
                if manual is not None and manual.prefer_configured_nature
                else generated.nature_of_entity
                or (None if manual is None else manual.nature_of_entity)
            ),
            prefer_configured_address=(
                False if manual is None else manual.prefer_configured_address
            ),
            prefer_configured_nature=False if manual is None else manual.prefer_configured_nature,
            prefer_configured_name=False if manual is None else manual.prefer_configured_name,
        )
    return merged


def robinhood_entity_override_supersession_warnings(
    automatic: tuple[FaEntityOverride, ...],
    configured: tuple[FaEntityOverride, ...],
) -> tuple[str, ...]:
    """Count the configured address pairs and names the merge replaces. An
    entry without address has no pair to replace (#1449)."""

    by_isin = {item.isin: item for item in automatic}
    superseded = sum(
        1
        for item in configured
        if item.isin in by_isin
        and item.address is not None
        and not item.prefer_configured_address
        and (item.address, item.zip_code)
        != (by_isin[item.isin].address, by_isin[item.isin].zip_code)
    )
    supersession = (
        ()
        if not superseded
        else (
            "automatic legal-address evidence superseded "
            f"{superseded} configured Robinhood entity address"
            f"{'es' if superseded != 1 else ''}; set "
            "prefer_configured_address = true to retain reviewed values",
        )
    )
    automatic_names = {item.isin: item.name for item in automatic if item.name is not None}
    name_superseded = sum(
        1
        for item in configured
        if item.name is not None
        and item.isin in automatic_names
        and not item.prefer_configured_name
        and item.name != automatic_names[item.isin]
    )
    name_warning = (
        ()
        if not name_superseded
        else (
            "automatic legal-issuer evidence superseded "
            f"{name_superseded} configured Robinhood entity name"
            f"{'s' if name_superseded != 1 else ''}; set "
            "prefer_configured_name = true to retain reviewed values",
        )
    )
    return (*supersession, *name_warning)


class RobinhoodEntityRoute(StrEnum):
    """The automatic route a Robinhood entity override came from (#1449): GLEIF's
    exact-ISIN legal address, the SEC filer's business address, the GLEIF legal
    address of the depositary a Form F-6 names, or that of an ETF's N-CEN
    registrant."""

    LEGAL = "legal"
    SEC = "sec"
    ADR = "adr"
    FUND = "fund"


def combine_robinhood_entity_overrides(
    *,
    legal: tuple[FaEntityOverride, ...],
    sec: tuple[FaEntityOverride, ...],
    adr: tuple[FaEntityOverride, ...],
    fund: tuple[FaEntityOverride, ...],
) -> tuple[tuple[FaEntityOverride, ...], dict[Isin, RobinhoodEntityRoute]]:
    """Keep one automatic override per ISIN, and the route it came from; later
    sources win: legal < SEC < ADR < fund."""

    combined = {
        item.isin: (item, route)
        for route, items in (
            (RobinhoodEntityRoute.LEGAL, legal),
            (RobinhoodEntityRoute.SEC, sec),
            (RobinhoodEntityRoute.ADR, adr),
            (RobinhoodEntityRoute.FUND, fund),
        )
        for item in items
    }
    kept = sorted(combined.values(), key=lambda pair: pair[0].isin)
    return tuple(item for item, _ in kept), {item.isin: route for item, route in kept}


@dataclass(frozen=True, slots=True)
class RobinhoodFoundEntityAddress:
    """An automatic address and ZIP, and the route they came from (#1449)."""

    route: RobinhoodEntityRoute
    address: str
    zip_code: str


@dataclass(frozen=True, slots=True)
class RobinhoodUnverifiedEntityAddress:
    """A held security's ``[[fa.entities]]`` entry that leaves out ``address``,
    and the automatic address found for it, which nothing files until the
    filer verifies it in ``user.toml`` (#1449).

    Attributes:
        entry_index: The entry's position in ``[[fa.entities]]``.
        entry: The configured entry.
        found: The automatic address found, or ``None`` when none was.
        etf: Whether the security is a series ETF, which ADR 0011 keeps off
            every automatic route once it has an entry.
        nature_missing: Whether the entry sets no ``nature_of_entity`` and no
            automatic nature evidence gives one, so Table A3's nature of
            entity would be blank too (#1568).
    """

    entry_index: int
    entry: FaEntityOverride
    found: RobinhoodFoundEntityAddress | None
    etf: bool
    nature_missing: bool


def robinhood_unverified_entity_addresses(
    configured: tuple[FaEntityOverride, ...],
    held: tuple[RobinhoodSecurityConfig, ...],
    automatic: tuple[FaEntityOverride, ...],
    routes: Mapping[Isin, RobinhoodEntityRoute],
    natures: Collection[Isin],
) -> tuple[RobinhoodUnverifiedEntityAddress, ...]:
    """Each configured entry without ``address`` for a ``held`` security, in
    entry order. An entry for a security not held is left out, as any unused
    entry is. ``natures`` holds the ISINs with automatic nature evidence."""

    held_by_isin = {item.instrument.isin: item for item in held}
    automatic_by_isin = {item.isin: item for item in automatic}
    unverified = []
    for index, entry in enumerate(configured):
        security = held_by_isin.get(entry.isin)
        if entry.address is not None or security is None:
            continue
        automatic_entry = automatic_by_isin.get(entry.isin)
        found = (
            None
            if automatic_entry is None or automatic_entry.address is None
            else RobinhoodFoundEntityAddress(
                routes[entry.isin], automatic_entry.address, automatic_entry.zip_code
            )
        )
        unverified.append(
            RobinhoodUnverifiedEntityAddress(
                entry_index=index,
                entry=entry,
                found=found,
                etf=security.sub_category == "ETF" and not security.is_depositary_receipt,
                nature_missing=entry.nature_of_entity is None and entry.isin not in natures,
            )
        )
    return tuple(unverified)


def resolve_robinhood_security_table(
    inputs: tuple[ObservedIdentityInput, ...],
    sources: IdentitySourceBundle,
    configured: tuple[RobinhoodSecurityConfig, ...],
) -> tuple[RobinhoodSecurityIdentities, RobinhoodIdentityResolution]:
    """Generate the missing identities and add them after the configured ones."""

    resolution = resolve_robinhood_standard_identities(inputs, sources, configured)
    return RobinhoodSecurityIdentities(configured, resolution.generated), resolution
