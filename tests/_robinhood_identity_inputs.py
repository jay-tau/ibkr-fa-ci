"""Synthetic in-memory official identity sources (Nasdaq, SEC ticker,
submission and fund records) and the identity values that the Robinhood
identity compute, entity-nature, bundle, entity, no-echo, pipeline and CG
filing-route tests share (#1808).

:func:`issuer_identity_sources` builds one issuer's observed input and source
bundle; :func:`zq_identity_sources` lists one issuer under several symbols.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from ibkr_fa.compute.robinhood_entity_nature import RobinhoodEntityNatureEvidence
from ibkr_fa.compute.robinhood_identity import observed_robinhood_identity_inputs
from ibkr_fa.config import FaEntityOverride
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifLegalAddressRecord,
    IdentitySourceBundle,
    NasdaqFlag,
    NasdaqTradedRecord,
    ObservedIdentityInput,
    SecFundRecord,
    SecSubmissionRecord,
    SecTickerRecord,
)
from ibkr_fa.normalize.robinhood.accounts import RobinhoodAccountsReconciliation
from ibkr_fa.types import Isin
from tests._gleif_records import GLEIF_RECORD
from tests._robinhood_account_inputs import ZQ_NEW


def issuer_identity_sources(
    *,
    symbol: str = "XQAQ",
    cusip: str = "99992A109",
    name: str = "Sample Issuer AQ Inc.",
    nasdaq_name: str = "Sample Issuer AQ Inc. - Common Stock",
    etf: str = "N",
    state: str = "CA",
    forms: tuple[str, ...] = ("10-K",),
    fund: bool = False,
    business_street1: str = "1 ISSUER WAY",
    business_city: str = "SAMPLETOWN",
    business_region: str = "CA",
    business_zip: str = "99990",
    cik: int = 9991029,
    digest: str = "a" * 64,
) -> tuple[ObservedIdentityInput, IdentitySourceBundle]:
    return (
        ObservedIdentityInput(cusip, symbol, name),
        IdentitySourceBundle(
            nasdaq_by_symbol={
                symbol: NasdaqTradedRecord(
                    symbol,
                    nasdaq_name,
                    "Q",
                    NasdaqFlag(etf),
                    NasdaqFlag.NO,
                    NasdaqFlag.NO,
                )
            },
            sec_tickers_by_symbol={symbol: (SecTickerRecord(cik, name, symbol, "Nasdaq"),)},
            sec_funds_by_symbol=(
                {symbol: (SecFundRecord(cik, "S1", "C1", symbol),)} if fund else {}
            ),
            sec_submissions_by_cik={
                cik: SecSubmissionRecord(
                    cik,
                    name,
                    state,
                    forms,
                    business_street1,
                    "",
                    business_city,
                    business_region,
                    business_zip,
                )
            },
            source_sha256={
                SEC_SUBMISSION_URL_TEMPLATE.format(cik=cik): digest,
            },
        ),
    )


def lapsed_gleif_record(isin: Isin, legal_name: str) -> GleifLegalAddressRecord:
    return replace(
        GLEIF_RECORD,
        isin=isin,
        legal_name=legal_name,
        registration_status="LAPSED",
        registration_last_update="2025-01-01T00:00:00Z",
        golden_copy_publish_date="2026-08-01T00:00:00Z",
    )


def observed_aq_identity(*names: str) -> ObservedIdentityInput:
    """The collector's evidence for CUSIP 99992A109 (``XQAQ``) when its activity
    rows print ``names``; an empty name for no name."""
    events = tuple(
        SimpleNamespace(cusip="99992A109", instrument="XQAQ", issuer_name=name)
        for name in names or ("",)
    )
    paired = SimpleNamespace(
        accounts=(
            SimpleNamespace(
                evidence=SimpleNamespace(events=events),
                activity=SimpleNamespace(
                    primary_bundle=SimpleNamespace(statements=()), cusip_transition_uses=()
                ),
            ),
        )
    )
    (observed,) = observed_robinhood_identity_inputs(
        cast("RobinhoodAccountsReconciliation", paired)
    )
    return observed


# Two broker issuer names for the one CUSIP that disagree (#1082).
AQ_BROKER_NAME = "SAMPLE ISSUER AQ INC"
AX_BROKER_NAME = "SAMPLE ISSUER AX CORP"

ISSUER_AQ_ISIN = Isin("US99992A1097")
# The warning when automatic legal-issuer evidence replaces one configured
# [[fa.entities]] name.
NAME_SUPERSEDED_ONE = (
    "automatic legal-issuer evidence superseded 1 configured Robinhood entity name; "
    "set prefer_configured_name = true to retain reviewed values"
)
# The warning when automatic legal-form evidence replaces one configured
# [[fa.entities]] nature.
NATURE_SUPERSEDED_ONE = (
    "automatic legal-form evidence superseded 1 configured Robinhood nature value; "
    "set prefer_configured_nature = true to retain reviewed values"
)


def gleif_nature_evidence(isin: Isin, value: str = "Corporation") -> RobinhoodEntityNatureEvidence:
    return RobinhoodEntityNatureEvidence(
        isin=isin, value=value, source="GLEIF", transformation="ELF code", raw_value="XTIQ"
    )


def automatic_entity_override(isin: Isin, *, name: str | None = None) -> FaEntityOverride:
    return FaEntityOverride(isin=isin, address="1 Legal Way", zip_code="19801", name=name)


# The failed checks when the broker's symbols match no single official listing
# (#1187).
ZQ_AMBIGUOUS_CHECKS = ("broker-symbol-ambiguous", "nasdaq-symbol-missing", "sec-ticker-not-unique")


def zq_identity_sources(
    *symbols: str,
    name: str = "Sample Issuer ZQ Inc.",
    exchange: str = "Nasdaq",
    state: str = "CA",
) -> IdentitySourceBundle:
    """One issuer's official records, listing each of ``symbols`` (default
    ``ZQXA``) on Nasdaq and at the SEC under ``exchange``."""
    bundles = [
        issuer_identity_sources(
            symbol=symbol,
            cusip=ZQ_NEW,
            name=name,
            nasdaq_name=f"{name} - Common Stock",
            state=state,
        )[1]
        for symbol in symbols or ("ZQXA",)
    ]
    return replace(
        bundles[0],
        nasdaq_by_symbol={
            symbol: record
            for bundle in bundles
            for symbol, record in bundle.nasdaq_by_symbol.items()
        },
        sec_tickers_by_symbol={
            symbol: tuple(replace(record, exchange=exchange) for record in records)
            for bundle in bundles
            for symbol, records in bundle.sec_tickers_by_symbol.items()
        },
    )
