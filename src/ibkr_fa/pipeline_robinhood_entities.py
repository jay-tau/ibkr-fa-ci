"""Resolve Robinhood A3 address, name, and nature presentation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ibkr_fa.compute.robinhood_entity_nature import (
    RobinhoodEntityNatureAudit,
    RobinhoodEntityNatureEvidence,
    bind_robinhood_fund_entities,
    resolve_robinhood_entity_natures,
)
from ibkr_fa.compute.robinhood_entity_overrides import (
    GleifLegalAddressAudit,
    RobinhoodAdrEntityAudit,
    RobinhoodFundEntityAudit,
    RobinhoodFundEntityEvidence,
    resolve_robinhood_adr_entity_overrides,
    resolve_robinhood_fund_entity_overrides,
    resolve_robinhood_legal_entity_overrides,
)
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodSecEntityAudit,
    RobinhoodSecEntityEvidence,
    RobinhoodUnverifiedEntityAddress,
    combine_robinhood_entity_overrides,
    is_us_common_stock,
    resolve_robinhood_sec_entity_overrides,
    robinhood_unverified_entity_addresses,
)
from ibkr_fa.compute.robinhood_lots import (
    reconcile_robinhood_security_lots,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import IdentitySourceBundle, ObservedIdentityInput
from ibkr_fa.normalize.robinhood import (
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.robinhood_adr_entity_sources import load_robinhood_adr_entity_evidence
from ibkr_fa.robinhood_fund_entity_sources import load_robinhood_fund_entity_evidence
from ibkr_fa.robinhood_identity_sources import (
    IdentitySourceAudit,
    identity_input_fingerprint,
    load_identity_sources,
    load_post_fy_identity_sources,
)
from ibkr_fa.robinhood_legal_entity_sources import load_robinhood_legal_entity_evidence


@dataclass(frozen=True, slots=True)
class RobinhoodPresentationEvidence:
    """All automatic A3 presentation evidence for one reporting calendar year."""

    entity_overrides: tuple[FaEntityOverride, ...]
    unverified_addresses: tuple[RobinhoodUnverifiedEntityAddress, ...]
    legal_address_audit: GleifLegalAddressAudit
    nature_evidence: tuple[RobinhoodEntityNatureEvidence, ...]
    nature_audit: RobinhoodEntityNatureAudit
    fund_evidence: tuple[RobinhoodFundEntityEvidence, ...]
    fund_audit: RobinhoodFundEntityAudit
    adr_audit: RobinhoodAdrEntityAudit
    sec_entity_evidence: tuple[RobinhoodSecEntityEvidence, ...]
    sec_entity_audit: RobinhoodSecEntityAudit
    identity_source_audit: IdentitySourceAudit | None


def _held_cusips(
    quantities: RobinhoodSecurityQuantityResult,
    calendar_year: int,
) -> frozenset[str]:
    start = date(calendar_year, 1, 1)
    end = date(calendar_year, 12, 31)
    lots = reconcile_robinhood_security_lots(quantities)
    return (
        frozenset(
            item.cusip
            for item in lots.holding_intervals
            if item.from_date <= end and (item.to_date is None or item.to_date >= start)
        )
        | frozenset(
            item.cusip for item in quantities.acquisitions if start <= item.activity_date <= end
        )
        | frozenset(
            item.cusip for item in quantities.movements if start <= item.activity_date <= end
        )
        | frozenset(
            item.cusip
            for item in quantities.anchors
            if start <= item.period_end <= end and item.statement_quantity > 0
        )
    )


def resolve_robinhood_presentation_evidence(
    *,
    robinhood: RobinhoodConfig,
    securities: tuple[RobinhoodSecurityConfig, ...],
    configured: tuple[FaEntityOverride, ...],
    observed: tuple[ObservedIdentityInput, ...],
    manual_fields: dict[str, dict[str, str]],
    identity_sources: IdentitySourceBundle | None,
    quantities: RobinhoodSecurityQuantityResult,
    calendar_year: int,
    cache_dir: Path,
    fetch: bool,
) -> RobinhoodPresentationEvidence:
    """Resolve only presentation evidence needed by CY-overlapping securities."""

    held = _held_cusips(quantities, calendar_year)
    held_securities = tuple(item for item in securities if item.cusip in held)
    # Owner decisions 1(a)/2(a) on #793: a series ETF takes its cells only from
    # its N-CEN registrant and SEC fund evidence, never from a direct ISIN
    # record, and a configured one takes no automatic cell.
    direct_securities = tuple(
        item
        for item in held_securities
        if not item.is_depositary_receipt and item.sub_category != "ETF"
    )
    legal = load_robinhood_legal_entity_evidence(
        direct_securities,
        configured,
        cache_dir=cache_dir,
        fetch=fetch,
    )
    direct_overrides, legal_audit, gleif_outcomes = resolve_robinhood_legal_entity_overrides(
        direct_securities,
        legal,
    )
    adr_overrides, adr_records, adr_audit = resolve_robinhood_adr_entity_overrides(
        held_securities,
        load_robinhood_adr_entity_evidence(
            held_securities,
            observed,
            configured,
            cutoff=date(calendar_year, 12, 31),
            cache_dir=cache_dir,
            fetch=fetch,
            sec_user_agent=robinhood.sec_user_agent,
        ),
    )
    configured_by_isin = {item.isin: item for item in configured}
    held_funds = tuple(
        item
        for item in held_securities
        if item.sub_category == "ETF"
        and not item.is_depositary_receipt
        and item.instrument.isin not in configured_by_isin
    )
    direct_resolved = {item.isin for item in direct_overrides}
    # Owner decision 2(a) on #792: every candidate ranks the post-FY snapshots,
    # including one whose GLEIF outcome then keeps it from the SEC fallback.
    sec_candidates = tuple(
        item
        for item in direct_securities
        if item.instrument.isin not in direct_resolved
        and is_us_common_stock(item)
        and not (
            item.instrument.isin in configured_by_isin
            and configured_by_isin[item.instrument.isin].prefer_configured_address
            and configured_by_isin[item.instrument.isin].prefer_configured_name
            and configured_by_isin[item.instrument.isin].prefer_configured_nature
        )
    )

    def load_post_fy() -> tuple[IdentitySourceBundle | None, IdentitySourceAudit]:
        return load_post_fy_identity_sources(
            {item.instrument.symbol for item in sec_candidates},
            required_fund_symbols={item.instrument.symbol for item in held_funds},
            cache_dir=cache_dir,
            financial_year_end=date(calendar_year + 1, 3, 31),
            fetch=fetch,
            sec_user_agent=robinhood.sec_user_agent,
            current_identity_fingerprint=identity_input_fingerprint(
                observed,
                manual_fields,
            ),
        )

    identity_source_audit = None
    if sec_candidates or held_funds:
        identity_sources, identity_source_audit = load_post_fy()
        if identity_sources is None and identity_source_audit.artifact_path is None and fetch:
            _fetched, fetch_audit = load_identity_sources(
                observed,
                manual_fields,
                cache_dir=cache_dir,
                fetch=fetch,
                sec_user_agent=robinhood.sec_user_agent,
            )
            if fetch_audit.failure is not None:
                identity_source_audit = fetch_audit
            else:
                identity_sources, identity_source_audit = load_post_fy()
    sec_overrides, sec_evidence, sec_audit = resolve_robinhood_sec_entity_overrides(
        sec_candidates,
        observed,
        identity_sources,
        legal.records,
        gleif_outcomes,
    )
    fund_overrides, fund_evidence, fund_audit = resolve_robinhood_fund_entity_overrides(
        held_funds,
        load_robinhood_fund_entity_evidence(
            held_funds,
            identity_sources,
            cutoff=date(calendar_year, 12, 31),
            cache_dir=cache_dir,
            fetch=fetch,
            sec_user_agent=robinhood.sec_user_agent,
        ),
    )
    nature_evidence, nature_audit = resolve_robinhood_entity_natures(
        held_securities,
        configured,
        (*legal.records, *adr_records),
        identity_sources,
        depositary_isins=frozenset(item.isin for item in adr_records),
        sec_common_isins=frozenset(item.isin for item in sec_evidence),
        lapsed_gleif_isins=frozenset(
            item.isin for item in sec_evidence if item.name_source == "broker"
        ),
        fund_evidence=fund_evidence,
    )
    fund_overrides, fund_evidence, fund_audit = bind_robinhood_fund_entities(
        fund_overrides,
        fund_evidence,
        fund_audit,
        nature_evidence,
    )
    entity_overrides, routes = combine_robinhood_entity_overrides(
        legal=direct_overrides,
        sec=sec_overrides,
        adr=adr_overrides,
        fund=fund_overrides,
    )
    return RobinhoodPresentationEvidence(
        entity_overrides=entity_overrides,
        unverified_addresses=robinhood_unverified_entity_addresses(
            configured,
            held_securities,
            entity_overrides,
            routes,
            frozenset(item.isin for item in nature_evidence),
        ),
        legal_address_audit=legal_audit,
        nature_evidence=nature_evidence,
        nature_audit=nature_audit,
        fund_evidence=fund_evidence,
        fund_audit=fund_audit,
        adr_audit=adr_audit,
        sec_entity_evidence=sec_evidence,
        sec_entity_audit=sec_audit,
        identity_source_audit=identity_source_audit,
    )
