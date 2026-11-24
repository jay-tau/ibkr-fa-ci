"""The synthetic common stock and ETF that the Robinhood presentation-entity
tests run through ``resolve_robinhood_presentation_evidence`` (#1831).

:func:`resolve_presentation` resolves the stock ``XQAQ`` (or another security)
with no accounts for calendar year 2025. :func:`etf_filing` stubs the sources,
resolves the synthetic ETF and computes Schedule FA from the result. The
``stub_``/``use_`` helpers patch ``ibkr_fa.pipeline_robinhood_entities``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.compute.robinhood_entity_nature import merge_robinhood_entity_natures
from ibkr_fa.compute.robinhood_fa import compute_robinhood_fa
from ibkr_fa.compute.robinhood_identity import merge_robinhood_entity_overrides
from ibkr_fa.config import FaEntityOverride, RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifRegistrantRecord,
    IdentitySourceBundle,
    ObservedIdentityInput,
    RegistrantLookup,
    SecFundRecord,
    SecSubmissionRecord,
)
from ibkr_fa.normalize.robinhood import RobinhoodSecurityQuantityResult
from ibkr_fa.robinhood_identity_sources import IdentitySourceAudit
from ibkr_fa.robinhood_legal_entity_sources import (
    LegalAddressOpener,
    load_robinhood_legal_entity_evidence,
)
from tests._entity_transport import FixedReplyOpener
from tests._robinhood_entity_stubs import stub_registrant_sources
from tests._robinhood_fa_inputs import (
    FA_SYMBOL,
    FlatFx,
    fa_config,
    fa_income,
    fa_options,
    fa_paired,
    fa_price_series,
    fa_security,
)
from tests._robinhood_fund import fund_registrant_record, ncen_identity
from tests._robinhood_gleif import issuer_reply
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN, issuer_identity_sources
from tests._robinhood_security import configured_securities, robinhood_security

ISSUER_AQ_SECURITY = robinhood_security(symbol="XQAQ", cusip="99992A109", isin=ISSUER_AQ_ISIN)
ISSUER_AQ_OBSERVED = (ObservedIdentityInput("99992A109", "XQAQ", "Sample Issuer AQ Inc."),)
# A placeholder ``quantities``: the presentation step reads it only through
# ``_held_cusips``, which callers stub.
PRESENTATION_QUANTITIES = cast("RobinhoodSecurityQuantityResult", object())
ISSUER_AQ_CIK = 9991029
ISSUER_AQ_SOURCES = issuer_identity_sources(cik=ISSUER_AQ_CIK)[1]


def stub_issuer_aq_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """:func:`stub_registrant_sources` for the stock's CUSIP."""
    stub_registrant_sources(monkeypatch, ISSUER_AQ_SECURITY.cusip)


def resolve_presentation(
    tmp_path: Path,
    *,
    security: RobinhoodSecurityConfig = ISSUER_AQ_SECURITY,
    configured: tuple[FaEntityOverride, ...] = (),
    identity_sources: IdentitySourceBundle | None = None,
    fetch: bool = False,
):
    """Resolve ``security``'s presentation evidence with no accounts, the
    stock's observed row and calendar year 2025, caching under ``tmp_path``."""
    return entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=(security,),
        configured=configured,
        observed=ISSUER_AQ_OBSERVED,
        manual_fields={},
        identity_sources=identity_sources,
        quantities=PRESENTATION_QUANTITIES,
        calendar_year=2025,
        cache_dir=tmp_path,
        fetch=fetch,
    )


def use_legal_entity_opener(monkeypatch: pytest.MonkeyPatch, opener: LegalAddressOpener) -> None:
    """Make the presentation step load GLEIF legal-entity evidence through
    ``opener``."""
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        partial(load_robinhood_legal_entity_evidence, opener=opener),
    )


def use_identity_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sources: IdentitySourceBundle = ISSUER_AQ_SOURCES,
) -> None:
    """Make the presentation step's post-FY identity snapshot ``sources``."""
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: (
            sources,
            IdentitySourceAudit("f" * 64, True, None, tmp_path / "sources.json"),
        ),
    )


# The synthetic ETF, whose A3 identity comes only from its N-CEN registrant (#793).
ETF_SECURITY = replace(fa_security(), sub_category="ETF")
ETF_NCEN = ncen_identity()
ETF_FUND = SecFundRecord(ETF_NCEN.cik, ETF_NCEN.series_id, ETF_NCEN.class_id, FA_SYMBOL)


def etf_identity_sources(fund: SecFundRecord = ETF_FUND, forms: tuple[str, ...] = ("N-CEN",)):
    """An identity snapshot that lists ``fund`` under the ETF's symbol, with an
    SEC submission for the fund's CIK that lists ``forms``."""
    return IdentitySourceBundle(
        nasdaq_by_symbol={},
        sec_tickers_by_symbol={},
        sec_funds_by_symbol={FA_SYMBOL: (fund,)},
        sec_submissions_by_cik={
            fund.cik: SecSubmissionRecord(fund.cik, "SAMPLE FUND TRUST", "DE", forms)
        },
        source_sha256={SEC_SUBMISSION_URL_TEMPLATE.format(cik=fund.cik): "e" * 64},
    )


ETF_REGISTRANT = fund_registrant_record(
    address_lines=("1 FUND PLAZA",),
    city="SAMPLETOWN",
    region="US-CA",
    postal_code="99992",
)
GLEIF_ZERO_MATCH = issuer_reply([])


def etf_filing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gleif: bytes,
    registrant: GleifRegistrantRecord | None = ETF_REGISTRANT,
    sources: IdentitySourceBundle | None = None,
    configured: tuple[FaEntityOverride, ...] = (),
    fund_loader: Callable[..., RegistrantLookup[Any]] | None = None,
):
    """Resolve the ETF's presentation evidence and compute Schedule FA from it.

    The fund lookup finds ``registrant`` (``None``: an N-CEN cache miss) unless
    ``fund_loader`` replaces it, and the GLEIF opener offers ``gleif``. Returns the
    presentation result, the Schedule FA result and the GLEIF opener.
    """
    stub_issuer_aq_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "_held_cusips",
        lambda _quantities, _calendar_year: frozenset({ETF_SECURITY.cusip}),
    )

    def funds(securities, *_args, **_kwargs):
        requested = tuple(securities)
        if registrant is None:
            return RegistrantLookup((), len(requested), 0, 0, (("ncen-cache-miss", 1),))
        return RegistrantLookup(
            tuple((item.instrument.isin, ETF_NCEN, registrant) for item in requested),
            len(requested),
            0,
            0,
        )

    monkeypatch.setattr(entities, "load_robinhood_fund_entity_evidence", fund_loader or funds)
    opener = FixedReplyOpener(gleif)
    use_legal_entity_opener(monkeypatch, opener)
    use_identity_snapshot(
        monkeypatch, tmp_path, etf_identity_sources() if sources is None else sources
    )
    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=(ETF_SECURITY,),
        configured=configured,
        observed=(ObservedIdentityInput(ETF_SECURITY.cusip, FA_SYMBOL, "Synthetic ETF"),),
        manual_fields={},
        identity_sources=None,
        quantities=PRESENTATION_QUANTITIES,
        calendar_year=2025,
        cache_dir=tmp_path,
        fetch=True,
    )
    paired, accounts = fa_paired(sold=False, dividend=False)
    config = replace(fa_config(accounts), securities=(ETF_SECURITY,))
    fa = compute_robinhood_fa(
        paired,
        config=config,
        securities=configured_securities(config),
        income=fa_income(dividend=False),
        options=fa_options(),
        calendar_year=2025,
        fx_policy=FlatFx(),
        prices_by_ticker=fa_price_series(),
        entity_overrides=merge_robinhood_entity_overrides(
            presentation.entity_overrides, configured
        ),
        nature_by_isin=merge_robinhood_entity_natures(presentation.nature_evidence, configured),
    )
    return presentation, fa, opener
