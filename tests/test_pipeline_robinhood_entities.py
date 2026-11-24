from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest

import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa.compute.robinhood_entity_nature import merge_robinhood_entity_natures
from ibkr_fa.compute.robinhood_fa import (
    RobinhoodFaManualKind,
    RobinhoodFaResult,
    compute_robinhood_fa,
)
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    RobinhoodFoundEntityAddress,
    RobinhoodSecEntityAudit,
    RobinhoodUnverifiedEntityAddress,
    merge_robinhood_entity_overrides,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodConfig, RobinhoodSecurityConfig
from ibkr_fa.exceptions import IbkrFaError
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    GleifLegalAddressLookup,
    IdentitySourceBundle,
    ObservedIdentityInput,
    RegistrantLookup,
    SecTickerRecord,
)
from ibkr_fa.robinhood_fund_entity_sources import load_robinhood_fund_entity_evidence
from ibkr_fa.robinhood_identity_sources import (
    NASDAQ_TRADED_URL,
    SEC_COMPANY_TICKERS_MF_URL,
    IdentitySourceAudit,
    SecEntityCacheError,
)
from ibkr_fa.robinhood_legal_entity_sources import (
    LegalAddressSourceError,
    _artifact_path,
    load_gleif_registrant_by_lei,
)
from ibkr_fa.types import Isin
from tests._entity_source_texts import (
    GLEIF_CACHE_INTEGRITY,
    GLEIF_HTTP_503,
    NASDAQ_UNAVAILABLE,
    NCEN_SOURCE_FAILURE,
)
from tests._entity_transport import FixedReplyOpener as _RecordingOpener
from tests._entity_transport import RecordingOpener as _SourceOpener
from tests._entity_transport import blocked_opener as _blocked
from tests._gleif_records import GLEIF_RECORD
from tests._robinhood_entity_stubs import stub_zero_match_sources
from tests._robinhood_fa_inputs import (
    FA_ISIN,
    FA_SYMBOL,
    FlatFx,
    fa_config,
    fa_income,
    fa_options,
    fa_paired,
    fa_price_series,
    fa_security,
)
from tests._robinhood_fund import FUND_LEI as _FUND_LEI
from tests._robinhood_fund import NEWER_NCEN_FILING as _NEWER
from tests._robinhood_fund import OLDER_NCEN_FILING as _OLDER
from tests._robinhood_fund import fund_index_responses as _index_responses
from tests._robinhood_fund import fund_registrant_record as _fund_registrant
from tests._robinhood_fund import fund_registrant_reply as _registrant_response
from tests._robinhood_fund import ncen_document_url as _document_url
from tests._robinhood_fund import ncen_identity as _ncen
from tests._robinhood_fund import ncen_xml as _ncen_xml
from tests._robinhood_gleif import exact_pattern as _exactly
from tests._robinhood_gleif import issuer_record as _record
from tests._robinhood_gleif import issuer_reply as _response
from tests._robinhood_gleif import legal_address_cache_refusal as _cache_refusal
from tests._robinhood_gleif import legal_address_fetch_refusal as _fetch_refusal
from tests._robinhood_gleif import legal_address_raw_type_refusal as _raw_type_refusal
from tests._robinhood_gleif import reseal_record as _reseal
from tests._robinhood_gleif import with_raw as _with_raw
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN as _ISIN
from tests._robinhood_identity_inputs import issuer_identity_sources as _identity_sources
from tests._robinhood_identity_sources import IDENTITY_CACHE_INTEGRITY as _INTEGRITY
from tests._robinhood_identity_sources import ISSUER_AQ_SUBMISSION as _ISSUER_AQ
from tests._robinhood_identity_sources import NASDAQ_ROWS as _ROWS
from tests._robinhood_identity_sources import funds_bytes as _funds_bytes
from tests._robinhood_identity_sources import load_snapshot as _load_snapshot
from tests._robinhood_identity_sources import nasdaq_bytes as _nasdaq_bytes
from tests._robinhood_identity_sources import snapshot_refusal as _snapshot_refusal
from tests._robinhood_identity_sources import tamper_snapshot as _tamper
from tests._robinhood_presentation import ETF_FUND as _ETF_FUND
from tests._robinhood_presentation import ETF_NCEN as _NCEN
from tests._robinhood_presentation import ETF_SECURITY as _ETF
from tests._robinhood_presentation import GLEIF_ZERO_MATCH as _ZERO_MATCH
from tests._robinhood_presentation import ISSUER_AQ_CIK as _CIK
from tests._robinhood_presentation import ISSUER_AQ_OBSERVED as _OBSERVED
from tests._robinhood_presentation import ISSUER_AQ_SECURITY as _SECURITY
from tests._robinhood_presentation import ISSUER_AQ_SOURCES as _SOURCES
from tests._robinhood_presentation import PRESENTATION_QUANTITIES as _QUANTITIES
from tests._robinhood_presentation import etf_filing as _etf_filing
from tests._robinhood_presentation import etf_identity_sources as _etf_sources
from tests._robinhood_presentation import resolve_presentation as _resolve
from tests._robinhood_presentation import stub_issuer_aq_sources as _patch_other_sources
from tests._robinhood_presentation import use_identity_snapshot as _use_snapshot
from tests._robinhood_presentation import use_legal_entity_opener as _use_gleif
from tests._robinhood_security import configured_securities, robinhood_security


def test_presentation_reuses_post_fy_snapshot_for_sec_address_and_nature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_other_sources(monkeypatch)
    gleif_lookup = GleifLegalAddressLookup((), {_ISIN: "zero-match"}, 1)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: gleif_lookup,
    )
    source_audit = IdentitySourceAudit(
        "f" * 64,
        True,
        None,
        tmp_path / "sources.json",
    )
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: (_SOURCES, source_audit),
    )

    result = _resolve(tmp_path, identity_sources=_SOURCES)

    assert result.entity_overrides == (
        FaEntityOverride(
            isin=_ISIN,
            address="1 ISSUER WAY, SAMPLETOWN, US-CA, US",
            zip_code="99990",
        ),
    )
    assert result.nature_evidence[0].value == "Corporation"
    assert result.sec_entity_audit.resolved == 1
    assert result.identity_source_audit == source_audit


_XQAJ = robinhood_security(
    symbol="XQAJ", cusip="99990D105", isin="US99990D1054", sub_category="ETF"
)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize("security", [_SECURITY, _XQAJ], ids=["common-stock", "etf"])
def test_presentation_refuses_a_corrupt_post_fy_snapshot_without_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    security: RobinhoodSecurityConfig,
    fetch: bool,
) -> None:
    stub_zero_match_sources(monkeypatch, security.cusip, _ISIN)
    _sources, audit, _opener = _load_snapshot(tmp_path)
    assert audit.artifact_path is not None
    _tamper(audit.artifact_path)
    monkeypatch.setattr(
        entities,
        "load_identity_sources",
        lambda *_args, **_kwargs: pytest.fail("corruption must not trigger a fetch"),
    )

    # #853: on b41cf75 the corruption became a counted audit and a warning.
    with pytest.raises(SecEntityCacheError) as refusal:
        _resolve(tmp_path, security=security, fetch=fetch)
    # #1526: the harness's config has no SEC contact.
    assert str(refusal.value) == _snapshot_refusal(
        tmp_path, _INTEGRITY, fetch=fetch, set_contact=True
    )


def test_presentation_failed_fallback_fetch_still_counts_the_source_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {_ISIN: "zero-match"}, 1),
    )
    fetch_audit = IdentitySourceAudit(
        "f" * 64,
        False,
        NASDAQ_UNAVAILABLE,
        None,
    )
    fetches: list[bool] = []
    monkeypatch.setattr(
        entities,
        "load_identity_sources",
        lambda *_args, fetch, **_kwargs: fetches.append(fetch) or (None, fetch_audit),
    )

    result = _resolve(tmp_path, identity_sources=_SOURCES, fetch=True)

    assert fetches == [True]
    assert result.entity_overrides == ()
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        0,
        (("source-unavailable", 1),),
    )
    assert result.identity_source_audit == fetch_audit


def test_gleif_address_remains_first_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_other_sources(monkeypatch)
    gleif = FaEntityOverride(
        isin=_ISIN,
        address="GLEIF LEGAL ADDRESS, SAMPLETON, US-DE, US",
        zip_code="99991",
    )
    record = replace(
        GLEIF_RECORD,
        isin=_ISIN,
        lei="999900SAMPLE00000258",
        legal_name="Sample Issuer AQ Inc.",
        address_lines=("GLEIF LEGAL ADDRESS",),
        jurisdiction="US-CA",
        golden_copy_publish_date="2026-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((record,), {}, 1),
    )
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: pytest.fail("resolved GLEIF address needs no SEC source"),
    )

    result = _resolve(tmp_path)

    assert result.entity_overrides == (gleif,)
    assert result.sec_entity_evidence == ()


def test_preferred_configured_address_does_not_disable_sec_nature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {_ISIN: "zero-match"}, 1),
    )
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: (
            _SOURCES,
            IdentitySourceAudit("f" * 64, True, None, tmp_path / "sources.json"),
        ),
    )
    configured = FaEntityOverride(
        isin=_ISIN,
        address="REVIEWED ADDRESS",
        zip_code="99999",
        prefer_configured_address=True,
    )

    result = _resolve(tmp_path, configured=(configured,))
    merged = merge_robinhood_entity_overrides(
        result.entity_overrides,
        (configured,),
    )

    assert merged[_ISIN].address == "REVIEWED ADDRESS"
    assert result.nature_evidence[0].value == "Corporation"


def test_fully_pinned_common_stock_does_not_require_sec_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {}, 0),
    )
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: pytest.fail("fully pinned security needs no SEC source"),
    )
    configured = FaEntityOverride(
        isin=_ISIN,
        address="REVIEWED ADDRESS",
        zip_code="99999",
        name="REVIEWED NAME",
        nature_of_entity="Reviewed Corporation",
        prefer_configured_address=True,
        prefer_configured_name=True,
        prefer_configured_nature=True,
    )

    result = _resolve(tmp_path, configured=(configured,))

    assert result.sec_entity_evidence == ()
    assert result.identity_source_audit is None


# ---- #792: which GLEIF outcomes may lead to the SEC fallback -----------------

_ISSUER_AQ_LEI = "999900SAMPLE00000258"
_OTHER_LEI = "999900SAMPLE00010055"
_ISSUER_AQ_SEC = FaEntityOverride(
    isin=_ISIN,
    address="1 ISSUER WAY, SAMPLETOWN, US-CA, US",
    zip_code="99990",
)


def _gleif(**changes: Any) -> dict[str, object]:
    """XQAQ's raw GLEIF record: ``LAPSED`` and otherwise valid unless changed."""
    fields: dict[str, Any] = {
        "lei": _ISSUER_AQ_LEI,
        "legal_name": "Sample Issuer AQ Inc.",
        "registration_status": "LAPSED",
    }
    return _record(**(fields | changes))


def _resolve_gleif(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
    *,
    sources: IdentitySourceBundle = _SOURCES,
    fetch: bool = True,
):
    _patch_other_sources(monkeypatch)
    _use_gleif(monkeypatch, _RecordingOpener(_response(records)) if fetch else _blocked)
    _use_snapshot(monkeypatch, tmp_path, sources)
    return _resolve(tmp_path, fetch=fetch)


def _sec_natures(result: entities.RobinhoodPresentationEvidence) -> set[Isin]:
    return {
        item.isin for item in result.nature_evidence if item.source == "sec-state-of-incorporation"
    }


def _assert_sec_blocked(result: entities.RobinhoodPresentationEvidence) -> None:
    assert result.entity_overrides == ()
    assert result.sec_entity_evidence == ()
    assert _sec_natures(result) == set()


@pytest.mark.parametrize(
    ("records", "reason"),
    [
        pytest.param(
            [_gleif(registration_status="ISSUED", legal_country="CA")],
            "country-mismatch",
            id="country-mismatch",
        ),
        pytest.param(
            [_gleif(registration_status="ISSUED", entity_status="INACTIVE")],
            "lifecycle",
            id="inactive",
        ),
        pytest.param(
            [_gleif(registration_status="ISSUED", successor=_OTHER_LEI)],
            "lifecycle",
            id="successor",
        ),
        pytest.param([_gleif(), _gleif()], "multi-match", id="multi-match"),
        pytest.param(
            [_gleif(registration_status="ISSUED", postal_code=None)],
            "incomplete",
            id="incomplete",
        ),
        pytest.param(
            [_gleif(registration_status="ISSUED", legal_lines=["=2+2"])],
            "portal-shape",
            id="portal-shape",
        ),
    ],
)
def test_unusable_gleif_outcome_blocks_the_sec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
    reason: str,
) -> None:
    result = _resolve_gleif(tmp_path, monkeypatch, records)

    _assert_sec_blocked(result)
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        0, gleif_blocked_counts=((reason, 1),)
    )


def test_gleif_source_failure_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(_url: str) -> bytes:
        raise LegalAddressSourceError(GLEIF_HTTP_503)

    _patch_other_sources(monkeypatch)
    _use_gleif(monkeypatch, unreachable)
    _use_snapshot(monkeypatch, tmp_path)

    with pytest.raises(
        IbkrFaError,
        match=_exactly(_fetch_refusal(tmp_path, GLEIF_HTTP_503)),
    ):
        _resolve(tmp_path, fetch=True)


@pytest.mark.parametrize("fetch", [True, False])
@pytest.mark.parametrize(
    ("content", "problem"),
    [
        pytest.param(b"{}\n", GLEIF_CACHE_INTEGRITY, id="corrupt"),
        pytest.param(
            b"\xff\xfe",
            "GLEIF legal-address cache artifact is not valid UTF-8 JSON",
            id="unreadable",
        ),
    ],
)
def test_invalid_gleif_cache_artifact_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    problem: str,
    fetch: bool,
) -> None:
    _resolve_gleif(tmp_path, monkeypatch, [])
    path = _artifact_path(tmp_path, _ISIN)
    path.write_bytes(content)
    _use_gleif(monkeypatch, _blocked)

    with pytest.raises(IbkrFaError, match=_exactly(_cache_refusal(tmp_path, problem, fetch=fetch))):
        _resolve(tmp_path, fetch=fetch)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param({"lei": "NOT-A-VALID-LEI"}, "incomplete", id="invalid-lei"),
        pytest.param({"legal_name": ""}, "incomplete", id="missing-legal-name"),
        pytest.param({"entity_status": "INACTIVE"}, "lifecycle", id="inactive"),
        pytest.param({"successor": _OTHER_LEI}, "lifecycle", id="successor"),
        pytest.param({"legal_country": "CA"}, "country-mismatch", id="country-mismatch"),
        pytest.param({"postal_code": None}, "incomplete", id="incomplete-address"),
        pytest.param({"legal_lines": ["=2+2"]}, "portal-shape", id="portal-shape"),
    ],
)
def test_lapsed_record_failing_another_guard_blocks_the_sec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    reason: str,
) -> None:
    result = _resolve_gleif(tmp_path, monkeypatch, [_gleif(**changes)])

    _assert_sec_blocked(result)
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        0, gleif_blocked_counts=((reason, 1),)
    )


@pytest.mark.parametrize("status", ["ISSUED", "LAPSED"])
def test_successor_named_without_an_lei_blocks_every_gleif_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    # #829: on 97194f8 the ISSUED record filed its address and GLEIF nature,
    # and the LAPSED one let the SEC fallback file the SEC address and nature.
    result = _resolve_gleif(
        tmp_path,
        monkeypatch,
        [_gleif(registration_status=status, successor_name="Successor Corp")],
    )

    _assert_sec_blocked(result)
    assert result.nature_evidence == ()
    assert result.legal_address_audit.failure_counts == (("lifecycle", 1),)
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        0, gleif_blocked_counts=(("lifecycle", 1),)
    )
    assert result.nature_audit.failure_counts == (("lifecycle", 1),)


_SUBMISSION = _SOURCES.sec_submissions_by_cik[_CIK]


def _submission(**changes: Any) -> IdentitySourceBundle:
    return replace(_SOURCES, sec_submissions_by_cik={_CIK: replace(_SUBMISSION, **changes)})


@pytest.mark.parametrize(
    "records",
    [pytest.param([], id="zero-match"), pytest.param([_gleif()], id="lapsed")],
)
def test_zero_match_or_valid_lapsed_record_reaches_the_sec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
) -> None:
    result = _resolve_gleif(tmp_path, monkeypatch, records)

    assert result.entity_overrides == (_ISSUER_AQ_SEC,)
    assert [item.isin for item in result.sec_entity_evidence] == [_ISIN]
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(1)


@pytest.mark.parametrize(
    "records",
    [pytest.param([], id="zero-match"), pytest.param([_gleif()], id="lapsed")],
)
@pytest.mark.parametrize(
    ("sources", "reason"),
    [
        pytest.param(_submission(name="Samplewood Holdings Corp"), "name-mismatch", id="name"),
        pytest.param(_submission(business_state_or_country="U0"), "business-country", id="country"),
        pytest.param(_submission(forms=("8-K",)), "filing-unavailable", id="filing"),
        pytest.param(_submission(business_zip_code="9999"), "address-incomplete", id="zip"),
    ],
)
def test_eligible_gleif_outcome_still_needs_every_sec_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
    sources: IdentitySourceBundle,
    reason: str,
) -> None:
    result = _resolve_gleif(tmp_path, monkeypatch, records, sources=sources)

    assert result.entity_overrides == ()
    assert result.sec_entity_evidence == ()
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(0, ((reason, 1),))


@pytest.mark.parametrize(
    ("ticker_name", "submission_name", "gleif_name"),
    [
        pytest.param(
            "Sample Issuer AQ Inc.", "Sample Issuer AQ Inc.", "Samplewood Inc.", id="both"
        ),
        pytest.param(
            "Sample Issuer AQ Inc.",
            "SAMPLE ISSUER AQ INC /CA/",
            "SAMPLE ISSUER AQ INC /NY/",
            id="submission",
        ),
        pytest.param(
            "SAMPLE ISSUER AQ INC /CA/",
            "Sample Issuer AQ Inc.",
            "SAMPLE ISSUER AQ INC /NY/",
            id="ticker",
        ),
    ],
)
def test_lapsed_gleif_name_conflict_beats_broker_name_agreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ticker_name: str,
    submission_name: str,
    gleif_name: str,
) -> None:
    sources = replace(
        _submission(name=submission_name),
        sec_tickers_by_symbol={"XQAQ": (SecTickerRecord(_CIK, ticker_name, "XQAQ", "Nasdaq"),)},
    )

    result = _resolve_gleif(
        tmp_path,
        monkeypatch,
        [_gleif(legal_name=gleif_name)],
        sources=sources,
    )

    assert result.entity_overrides == ()
    assert result.sec_entity_evidence == ()
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(0, (("name-mismatch", 1),))


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        pytest.param(("entity", "successorEntity"), "unexpected", "", id="successor-string"),
        pytest.param(
            ("entity", "successorEntity"),
            {"lei": 123},
            "entity.successorEntity.lei",
            id="successor-lei-number",
        ),
        pytest.param(("entity", "legalAddress", "region"), 123, "", id="region-number"),
        # #821: on 59d949e this was lapsed, and the SEC fallback ran.
        pytest.param(("entity", "jurisdiction"), 1, "", id="jurisdiction-number"),
    ],
)
def test_lapsed_record_with_an_unexpected_raw_type_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
    value: object,
    field: str,
) -> None:
    with pytest.raises(
        IbkrFaError,
        match=_exactly(
            _raw_type_refusal(tmp_path, f"data[0].attributes.{field or '.'.join(path)}")
        ),
    ):
        _resolve_gleif(tmp_path, monkeypatch, [_with_raw(_gleif(), *path, value=value)])
    assert not _artifact_path(tmp_path, _ISIN).exists()


def test_cached_lapsed_record_still_reaches_the_sec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = _resolve_gleif(tmp_path, monkeypatch, [_gleif()])
    cached = _resolve_gleif(tmp_path, monkeypatch, [], fetch=False)

    assert fetched.entity_overrides == cached.entity_overrides == (_ISSUER_AQ_SEC,)
    assert cached.legal_address_audit.cache_hits == 1


def test_cached_lapsed_record_with_an_unexpected_type_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolve_gleif(tmp_path, monkeypatch, [_gleif()])
    path = _artifact_path(tmp_path, _ISIN)
    _reseal(path, lambda record: record.update(region=123))

    # Before #1044 the refusal printed this artifact's path, whose folder name
    # is the ISIN's fingerprint.
    with pytest.raises(
        IbkrFaError,
        match=_exactly(
            _cache_refusal(
                tmp_path,
                "GLEIF legal-address cache field record.region has an unexpected JSON type",
                fetch=False,
            )
        ),
    ):
        _resolve_gleif(tmp_path, monkeypatch, [], fetch=False)


_XQCA_ISIN = Isin("US99972B1017")
_XQCA_CIK = 9991035
_XQCA = robinhood_security(symbol="XQCA", cusip="99972B101", isin=_XQCA_ISIN)
_XQCA_SOURCES = _identity_sources(
    symbol="XQCA",
    name="Sample Issuer CA Corp",
    nasdaq_name="Sample Issuer CA Corporation - Common Stock",
    state="WA",
    business_street1="2 ISSUER WAY",
    business_region="WA",
    business_zip="99980",
    cik=_XQCA_CIK,
    digest="c" * 64,
)[1]
_TWO_SOURCES = replace(
    _SOURCES,
    nasdaq_by_symbol={**_SOURCES.nasdaq_by_symbol, **_XQCA_SOURCES.nasdaq_by_symbol},
    sec_tickers_by_symbol={
        **_SOURCES.sec_tickers_by_symbol,
        **_XQCA_SOURCES.sec_tickers_by_symbol,
    },
    sec_submissions_by_cik={
        **_SOURCES.sec_submissions_by_cik,
        **_XQCA_SOURCES.sec_submissions_by_cik,
    },
    source_sha256={**_SOURCES.source_sha256, **_XQCA_SOURCES.source_sha256},
)


class _ByIsin:
    """Serve each exact-ISIN GLEIF request from its own response."""

    def __init__(self, responses: dict[Isin, bytes]) -> None:
        self.responses = responses

    def __call__(self, url: str) -> bytes:
        (body,) = (body for isin, body in self.responses.items() if str(isin) in url)
        return body


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
@pytest.mark.parametrize("zero_match", [_ISIN, _XQCA_ISIN], ids=["xqaq", "xqca"])
def test_only_the_zero_match_stock_of_two_reaches_the_sec_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    zero_match: Isin,
    reverse: bool,
) -> None:
    other = _XQCA_ISIN if zero_match == _ISIN else _ISIN
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "_held_cusips",
        lambda _quantities, _calendar_year: frozenset({_SECURITY.cusip, _XQCA.cusip}),
    )
    _use_gleif(
        monkeypatch,
        _ByIsin({zero_match: _response([]), other: _response([_gleif(), _gleif()])}),
    )
    decided: list[object] = []
    decide = entities.resolve_robinhood_legal_entity_overrides
    monkeypatch.setattr(
        entities,
        "resolve_robinhood_legal_entity_overrides",
        lambda *args: decided.append(decide(*args)) or decided[-1],
    )
    _use_snapshot(monkeypatch, tmp_path, _TWO_SOURCES)
    securities = (_SECURITY, _XQCA)
    observed = (*_OBSERVED, ObservedIdentityInput("99972B101", "XQCA", "Sample Issuer CA Corp"))

    result = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=securities[::-1] if reverse else securities,
        configured=(),
        observed=observed[::-1] if reverse else observed,
        manual_fields={},
        identity_sources=None,
        quantities=_QUANTITIES,
        calendar_year=2025,
        cache_dir=tmp_path,
        fetch=True,
    )

    assert [item.isin for item in result.entity_overrides] == [zero_match]
    assert [item.isin for item in result.sec_entity_evidence] == [zero_match]
    assert _sec_natures(result) == {zero_match}
    (decision,) = decided
    assert cast("tuple[object, object, object]", decision)[2] == {
        zero_match: "zero-match",
        other: "multi-match",
    }
    assert result.legal_address_audit.requested == 2
    assert result.legal_address_audit.failure_counts == (("multi-match", 1), ("zero-match", 1))
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        1, gleif_blocked_counts=(("multi-match", 1),)
    )


_SYNTHETIC_CIK = 9991065
_SYNTHETIC_SOURCES = _identity_sources(
    symbol=FA_SYMBOL,
    name="Synthetic Corporation",
    nasdaq_name="Synthetic Corporation - Common Stock",
    state="DE",
    business_street1="1 SYNTHETIC WAY",
    business_city="DOVER",
    business_region="DE",
    business_zip="19901",
    cik=_SYNTHETIC_CIK,
    digest="d" * 64,
)[1]
_REVIEWED_ADDRESS = FaEntityOverride(
    isin=FA_ISIN,
    address="1 REVIEWED WAY, DOVER, US-DE, US",
    zip_code="19901",
)


@pytest.mark.parametrize(
    ("configured", "expected", "identity_task"),
    [
        # #1289: an unresolved holding files a blank address, not its country.
        pytest.param((), ("", "", ""), True, id="absent"),
        pytest.param(
            (_REVIEWED_ADDRESS,),
            ("1 REVIEWED WAY, DOVER, US-DE, US", "19901", ""),
            True,
            id="partial",
        ),
        pytest.param(
            (
                replace(
                    _REVIEWED_ADDRESS,
                    name="Synthetic Corporation",
                    nature_of_entity="Reviewed Corporation",
                ),
            ),
            ("1 REVIEWED WAY, DOVER, US-DE, US", "19901", "Reviewed Corporation"),
            False,
            id="complete",
        ),
    ],
)
def test_cache_only_gleif_miss_leaves_the_identity_to_configuration_or_manual_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: tuple[FaEntityOverride, ...],
    expected: tuple[str, str, str],
    identity_task: bool,
) -> None:
    security = fa_security()
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "_held_cusips",
        lambda _quantities, _calendar_year: frozenset({security.cusip}),
    )
    _use_gleif(monkeypatch, _blocked)
    _use_snapshot(monkeypatch, tmp_path, _SYNTHETIC_SOURCES)

    presentation = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=(security,),
        configured=configured,
        observed=(ObservedIdentityInput(security.cusip, FA_SYMBOL, "Synthetic Corporation"),),
        manual_fields={},
        identity_sources=None,
        quantities=_QUANTITIES,
        calendar_year=2025,
        cache_dir=tmp_path,
        fetch=False,
    )
    paired, accounts = fa_paired(sold=False, dividend=False)
    config = fa_config(accounts)
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

    assert presentation.legal_address_audit.failure_counts == (("cache-miss", 1),)
    _assert_sec_blocked(presentation)
    assert presentation.sec_entity_audit == RobinhoodSecEntityAudit(
        0, gleif_blocked_counts=(("cache-miss", 1),)
    )
    assert {(row.address_of_entity, row.zip_code, row.nature_of_entity) for row in fa.a3_rows} == {
        expected
    }
    assert {item.kind for item in fa.manual_work} == (
        {RobinhoodFaManualKind.A3_IDENTITY} if identity_task else set()
    )


def test_snapshot_selection_still_counts_blocked_sec_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner decision 2(a): a blocked candidate still ranks snapshots.

    The earlier snapshot covers both common stocks; the later one misses the
    blocked XQAA. Dropping XQAA from the ranking would tie them and pick
    the later snapshot, with its other XQAQ address and XQAJ class.
    """
    xqaa = robinhood_security(symbol="XQAA", cusip="99990A309", isin="US99990A3095")
    xqaj = robinhood_security(
        symbol="XQAJ", cusip="99990D105", isin="US99990D1054", sub_category="ETF"
    )
    observed = (
        ObservedIdentityInput(_SECURITY.cusip, "XQAQ", "Sample Issuer AQ Inc."),
        ObservedIdentityInput(xqaa.cusip, "XQAA", "Sample Issuer AA Inc."),
        ObservedIdentityInput(xqaj.cusip, "XQAJ", "SAMPLE FUND AJ ETF"),
    )
    nasdaq = {
        NASDAQ_TRADED_URL: _nasdaq_bytes(
            (
                *_ROWS,
                "Y|XQAA|Sample Issuer AA Inc. - Class A Common Stock|Q|Q|N|100|N|N|XQAA|XQAA|N",
            )
        )
    }
    earlier, earlier_audit, _opener = _load_snapshot(
        tmp_path, responses=nasdaq, inputs=observed, now=datetime(2026, 8, 17, tzinfo=UTC)
    )
    moved = json.loads(json.dumps(_ISSUER_AQ))
    moved["addresses"]["business"]["street1"] = "2 Issuer Way"
    _load_snapshot(
        tmp_path,
        responses={
            **nasdaq,
            SEC_SUBMISSION_URL_TEMPLATE.format(cik=_CIK): json.dumps(moved).encode(),
            SEC_COMPANY_TICKERS_MF_URL: _funds_bytes(
                [[9991004, "S000999904", "C000999109", "XQAJ"]]
            ),
        },
        inputs=(observed[0], observed[2]),
        now=datetime(2026, 8, 18, tzinfo=UTC),
    )
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "_held_cusips",
        lambda _quantities, _calendar_year: frozenset(item.cusip for item in observed),
    )
    fund_sources: list[IdentitySourceBundle | None] = []

    def funds(securities, sources, **_kwargs):
        # The unstubbed loader binds the registrant to the snapshot's XQAJ class.
        fund_sources.append(sources)
        (fund,) = sources.sec_funds_by_symbol["XQAJ"]
        identity = replace(_ncen(), cik=fund.cik, series_id=fund.series_id, class_id=fund.class_id)
        return RegistrantLookup(
            tuple((item.instrument.isin, identity, _fund_registrant()) for item in securities),
            1,
            0,
            0,
        )

    monkeypatch.setattr(entities, "load_robinhood_fund_entity_evidence", funds)
    _use_gleif(
        monkeypatch,
        _ByIsin(
            {
                _ISIN: _response([]),
                xqaa.instrument.isin: _response([_gleif(), _gleif()]),
                xqaj.instrument.isin: _response([]),
            }
        ),
    )

    result = entities.resolve_robinhood_presentation_evidence(
        robinhood=RobinhoodConfig(accounts=()),
        securities=(_SECURITY, xqaa, xqaj),
        configured=(),
        observed=observed,
        manual_fields={},
        identity_sources=None,
        quantities=_QUANTITIES,
        calendar_year=2025,
        cache_dir=tmp_path,
        fetch=True,
    )

    assert result.identity_source_audit is not None
    assert result.identity_source_audit.artifact_path == earlier_audit.artifact_path
    # Overrides come in ISIN order: the XQAJ fund's sorts before XQAQ's.
    fund, issuer_aq = result.entity_overrides
    assert issuer_aq == FaEntityOverride(
        isin=_ISIN,
        address="1 Issuer Way, Sampletown, US-CA, US",
        zip_code="99990",
    )
    assert fund.isin == xqaj.instrument.isin
    assert fund_sources == [earlier]
    (fund_nature,) = (item for item in result.nature_evidence if item.isin == xqaj.instrument.isin)
    assert fund_nature.sec_class_id == "C000999941"
    assert result.sec_entity_audit == RobinhoodSecEntityAudit(
        1, gleif_blocked_counts=(("multi-match", 1),)
    )


# ---- #793: an ETF's A3 identity comes only from its N-CEN registrant ---------

_REGISTRANT_CELLS = (
    "SAMPLE FUND TRUST - Sample Fund AU ETF",
    "1 FUND PLAZA, SAMPLETOWN, US-CA, US",
    "99992",
    "Investment Company",
)
# A direct exact-ISIN record that passes every GLEIF address check and whose
# legal form files "Corporation".
_DIRECT_GLEIF = _response([_record()])


def _cells(fa: RobinhoodFaResult) -> tuple[str, str, str, str]:
    (row,) = fa.a3_rows
    return (row.name_of_entity, row.address_of_entity, row.zip_code, row.nature_of_entity)


def _assert_identity_manual(fa: RobinhoodFaResult) -> None:
    assert {item.kind for item in fa.manual_work} == {RobinhoodFaManualKind.A3_IDENTITY}
    assert not fa.cbdt_json_eligible


def _assert_no_automatic_cell(presentation: entities.RobinhoodPresentationEvidence) -> None:
    assert presentation.entity_overrides == ()
    assert presentation.nature_evidence == ()
    assert presentation.fund_evidence == ()


def test_etf_files_its_registrant_cells_and_investment_company(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ETF never reaches the direct GLEIF lookup, so the exact-ISIN record
    on offer is never read (#1694)."""
    presentation, fa, opener = _etf_filing(tmp_path, monkeypatch, gleif=_DIRECT_GLEIF)

    assert opener.calls == []
    assert _cells(fa) == _REGISTRANT_CELLS
    assert fa.manual_work == ()
    assert fa.cbdt_json_eligible
    assert [item.source for item in presentation.nature_evidence] == [
        "sec-registered-investment-company"
    ]


def test_failed_registrant_route_leaves_every_etf_cell_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SEC fund nature stays usable here; it still fills no cell alone."""
    presentation, fa, opener = _etf_filing(
        tmp_path, monkeypatch, gleif=_DIRECT_GLEIF, registrant=None
    )

    _assert_no_automatic_cell(presentation)
    assert opener.calls == []
    assert presentation.nature_audit.failure_counts == (("registrant-unresolved", 1),)
    assert _cells(fa)[1:] == ("", "", "")
    _assert_identity_manual(fa)


def test_registrant_naming_its_successor_only_by_name_leaves_every_etf_cell_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SEC fund nature stays usable here; it still fills no cell alone."""
    lei = _NCEN.registrant_lei
    registrant, _cache_hit = load_gleif_registrant_by_lei(
        lei,
        cache_dir=tmp_path,
        fetch=True,
        opener=_RecordingOpener(_registrant_response(lei=lei, successor_name="Successor Corp")),
    )

    # #829: on 97194f8 the ETF filed this registrant's cells and Investment Company.
    presentation, fa, _opener = _etf_filing(
        tmp_path, monkeypatch, gleif=_ZERO_MATCH, registrant=registrant
    )

    _assert_no_automatic_cell(presentation)
    assert presentation.fund_audit.failure_counts == (("gleif-lifecycle", 1),)
    assert presentation.nature_audit.failure_counts == (("registrant-unresolved", 1),)
    assert _cells(fa)[1:] == ("", "", "")
    _assert_identity_manual(fa)


def test_malformed_newer_ncen_filing_leaves_the_etf_manual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A duplicated series ID; the older filing would resolve the ETF.
    newer_xml = _ncen_xml(
        report_date="2025-06-30",
        ticker=FA_SYMBOL,
        series_id_xml=f"<mgmtInvSeriesId>{_NCEN.series_id}</mgmtInvSeriesId>" * 2,
    )
    sec_opener = _SourceOpener(
        _index_responses(
            [_NEWER, _OLDER],
            {
                _document_url(_NCEN.cik, _NEWER["accessionNumber"]): newer_xml,
                _document_url(_NCEN.cik, _OLDER["accessionNumber"]): _ncen_xml(ticker=FA_SYMBOL),
            },
        )
    )
    gleif_opener = _SourceOpener(
        {f"https://api.gleif.org/api/v1/lei-records/{_FUND_LEI}": _registrant_response()}
    )

    # #1083: on 18146d8 the older filing filed the ETF's registrant cells.
    presentation, fa, _opener = _etf_filing(
        tmp_path,
        monkeypatch,
        gleif=_ZERO_MATCH,
        fund_loader=partial(
            load_robinhood_fund_entity_evidence, sec_opener=sec_opener, gleif_opener=gleif_opener
        ),
    )

    assert presentation.fund_audit.warnings == (NCEN_SOURCE_FAILURE,)
    _assert_no_automatic_cell(presentation)
    assert _cells(fa)[1:] == ("", "", "")
    # No blocker and A3 identity manual work: the FA leg is MANUAL.
    assert fa.blockers == ()
    _assert_identity_manual(fa)


def test_registrant_without_sec_fund_nature_fills_no_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presentation, fa, _opener = _etf_filing(
        tmp_path,
        monkeypatch,
        gleif=_ZERO_MATCH,
        sources=_etf_sources(forms=("497",)),
    )

    _assert_no_automatic_cell(presentation)
    assert presentation.nature_audit.failure_counts == (("sec-fund-unproved", 1),)
    assert presentation.fund_audit.resolved == 0
    assert presentation.fund_audit.failure_counts == (("nature-unbound", 1),)
    _assert_identity_manual(fa)


@pytest.mark.parametrize(
    "change",
    [{"cik": _NCEN.cik + 1}, {"series_id": "S000999101"}, {"class_id": "C000999101"}],
    ids=["cik", "series", "class"],
)
def test_registrant_and_sec_fund_nature_must_name_the_same_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
) -> None:
    presentation, fa, _opener = _etf_filing(
        tmp_path,
        monkeypatch,
        gleif=_ZERO_MATCH,
        sources=_etf_sources(replace(_ETF_FUND, **change)),
    )

    _assert_no_automatic_cell(presentation)
    assert presentation.nature_audit.failure_counts == (("registrant-mismatch", 1),)
    assert presentation.fund_audit.failure_counts == (("nature-unbound", 1),)
    _assert_identity_manual(fa)


_ETF_REVIEWED = FaEntityOverride(
    isin=_ETF.instrument.isin,
    address="1 REVIEWED WAY, DOVER, US-DE, US",
    zip_code="19901",
    name="Reviewed Trust - Reviewed ETF",
    nature_of_entity="Reviewed Investment Company",
)
_PREFERENCES = ("prefer_configured_address", "prefer_configured_name", "prefer_configured_nature")


@pytest.mark.parametrize(
    "preferred",
    [(), *((flag,) for flag in _PREFERENCES), _PREFERENCES],
    ids=["no-preference", "address", "name", "nature", "all"],
)
@pytest.mark.parametrize(
    "missing",
    [{}, {"name": None}, {"nature_of_entity": None}, {"zip_code": ""}],
    ids=["complete", "no-name", "no-nature", "no-zip"],
)
def test_configured_etf_takes_no_automatic_cell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preferred: tuple[str, ...],
    missing: dict[str, object],
) -> None:
    """Every automatic source would resolve; none may fill or replace a cell."""
    configured = replace(_ETF_REVIEWED, **missing, **dict.fromkeys(preferred, True))

    presentation, fa, opener = _etf_filing(
        tmp_path,
        monkeypatch,
        gleif=_DIRECT_GLEIF,
        configured=(configured,),
    )

    _assert_no_automatic_cell(presentation)
    assert opener.calls == []
    assert presentation.identity_source_audit is None
    name, address, zip_code, nature = _cells(fa)
    assert (address, zip_code, nature) == (
        configured.address,
        configured.zip_code,
        configured.nature_of_entity or "",
    )
    if not missing:
        assert name == configured.name
        assert fa.manual_work == ()
        assert fa.cbdt_json_eligible
    else:
        _assert_identity_manual(fa)


# ---- #1449: an [[fa.entities]] entry without address ---------------------------------


def _addressless() -> FaEntityOverride:
    """XQAQ's entry with a name and no address."""
    return FaEntityOverride(isin=_ISIN, name="Tax Payer Holdings Inc.")


def test_an_addressless_entry_records_the_gleif_address_it_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry leaves the GLEIF route as an entry-free holding takes it; the
    address found is recorded, with its route, for the filer to verify."""
    _patch_other_sources(monkeypatch)
    opener = _RecordingOpener(_DIRECT_GLEIF)
    _use_gleif(monkeypatch, opener)
    _use_snapshot(monkeypatch, tmp_path)

    result = _resolve(tmp_path, configured=(_addressless(),), fetch=True)

    (automatic,) = result.entity_overrides
    assert opener.calls
    assert automatic.address is not None
    assert automatic.zip_code == "99991"
    assert result.unverified_addresses == (
        RobinhoodUnverifiedEntityAddress(
            entry_index=0,
            entry=_addressless(),
            found=RobinhoodFoundEntityAddress(
                RobinhoodEntityRoute.LEGAL, automatic.address, "99991"
            ),
            etf=False,
            nature_missing=False,
        ),
    )


def test_an_addressless_entry_records_the_sec_address_after_a_gleif_zero_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a GLEIF zero-match the SEC filer's business address is found."""
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {_ISIN: "zero-match"}, 1),
    )
    _use_snapshot(monkeypatch, tmp_path)
    other = FaEntityOverride(isin=Isin("US99995A1052"), name="Tax Payer Holdings Inc.")

    result = _resolve(tmp_path, configured=(other, _addressless()), identity_sources=_SOURCES)

    assert result.entity_overrides == (_ISSUER_AQ_SEC,)
    assert result.unverified_addresses == (
        RobinhoodUnverifiedEntityAddress(
            entry_index=1,
            entry=_addressless(),
            found=RobinhoodFoundEntityAddress(
                RobinhoodEntityRoute.SEC, "1 ISSUER WAY, SAMPLETOWN, US-CA, US", "99990"
            ),
            etf=False,
            nature_missing=False,
        ),
    )


def test_an_addressless_entry_with_no_automatic_address_records_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GLEIF finds nothing and the SEC snapshot is unavailable: the entry is
    recorded with no address found. An entry with an address is not."""
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "load_robinhood_legal_entity_evidence",
        lambda *_args, **_kwargs: GleifLegalAddressLookup((), {_ISIN: "zero-match"}, 1),
    )
    monkeypatch.setattr(
        entities,
        "load_post_fy_identity_sources",
        lambda *_args, **_kwargs: (None, IdentitySourceAudit("f" * 64, False, "absent", None)),
    )

    result = _resolve(tmp_path, configured=(_addressless(),))
    addressed = _resolve(tmp_path, configured=(replace(_addressless(), address="1 Sample Road"),))

    assert result.entity_overrides == addressed.entity_overrides == ()
    assert result.unverified_addresses == (
        RobinhoodUnverifiedEntityAddress(0, _addressless(), None, etf=False, nature_missing=True),
    )
    assert addressed.unverified_addresses == ()


def test_an_addressless_etf_entry_is_recorded_without_a_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0011: a configured ETF takes no automatic route, so the N-CEN
    evidence loader is never asked for it and no GLEIF request is made."""
    _patch_other_sources(monkeypatch)
    monkeypatch.setattr(
        entities,
        "_held_cusips",
        lambda _quantities, _calendar_year: frozenset({_ETF.cusip}),
    )
    requested: list[tuple[RobinhoodSecurityConfig, ...]] = []

    def funds(securities, *_args, **_kwargs):
        requested.append(tuple(securities))
        return RegistrantLookup((), 0, 0, 0)

    monkeypatch.setattr(entities, "load_robinhood_fund_entity_evidence", funds)
    opener = _RecordingOpener(_DIRECT_GLEIF)
    _use_gleif(monkeypatch, opener)
    entry = FaEntityOverride(isin=_ETF.instrument.isin, name="Tax Payer Holdings Inc.")

    result = _resolve(tmp_path, security=_ETF, configured=(entry,), fetch=True)

    assert requested == [()]
    assert opener.calls == []
    assert result.entity_overrides == ()
    assert result.unverified_addresses == (
        RobinhoodUnverifiedEntityAddress(0, entry, None, etf=True, nature_missing=True),
    )


def test_an_addressless_entry_for_a_security_not_held_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_other_sources(monkeypatch)
    _use_gleif(monkeypatch, _RecordingOpener(_DIRECT_GLEIF))
    _use_snapshot(monkeypatch, tmp_path)
    entry = FaEntityOverride(isin=Isin("US99995A1052"))

    result = _resolve(tmp_path, configured=(entry,), fetch=True)

    assert len(result.entity_overrides) == 1
    assert result.unverified_addresses == ()
