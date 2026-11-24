"""Robinhood A3 entity decisions from hand-built evidence, with no cache or opener (#634)."""

from __future__ import annotations

from dataclasses import replace

import pytest

import ibkr_fa.compute.robinhood_entity_overrides as overrides_mod
from ibkr_fa.compute.robinhood_entity_overrides import (
    GleifLegalAddressAudit,
    RobinhoodAdrEntityAudit,
    RobinhoodFundEntityAudit,
    resolve_robinhood_adr_entity_overrides,
    resolve_robinhood_fund_entity_overrides,
    resolve_robinhood_legal_entity_overrides,
)
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    combine_robinhood_entity_overrides,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    F6DepositaryIdentity,
    GleifLegalAddressLookup,
    GleifLegalAddressRecord,
    GleifRegistrantRecord,
    NcenFundIdentity,
    RegistrantLookup,
)
from ibkr_fa.types import Isin
from tests._gleif_records import GLEIF_RECORD
from tests._robinhood_fund import REGISTRANT_LEI as _LEI
from tests._robinhood_fund import fund_registrant_record as _fund_registrant
from tests._robinhood_fund import ncen_identity as _ncen
from tests._robinhood_fund import registrant_record as _registrant
from tests._robinhood_security import robinhood_security

_ISIN = Isin("US99992E1010")
_OTHER_ISIN = Isin("US99992A1097")
_CH_ISIN = Isin("CH99970F1040")
_ALIAS_ISIN = Isin("CH1134540470")


def _security(*, isin: Isin = _ISIN, country: str = "US") -> RobinhoodSecurityConfig:
    return robinhood_security(
        cusip=str(isin)[2:11],
        isin=isin,
        listing_exchange="NYSE",
        issuer_country_code=country,
    )


def _legal(*, isin: Isin = _ISIN, **changes: object) -> GleifLegalAddressRecord:
    record = replace(
        GLEIF_RECORD,
        isin=isin,
        lei=_LEI,
        legal_name="SAMPLE ISSUER AG INC.",
        registration_last_update="2026-03-10T09:30:00Z",
        source_url="https://api.gleif.org/api/v1/lei-records?filter%5Bisin%5D=US99992E1010",
    )
    return replace(record, **changes)


def _f6(depositary_name: str = "SAMPLE DEPOSITARY BANK, N.A.") -> F6DepositaryIdentity:
    return F6DepositaryIdentity(
        issuer_name="Sample Issuer AX Holding Limited",
        depositary_name=depositary_name,
        depositary_lei=_LEI,
        document_url="https://www.sec.gov/Archives/edgar/data/9991001/000999100920000061/f6.htm",
        document_sha256="c" * 64,
    )


def _resolve_legal(
    record: GleifLegalAddressRecord,
    security: RobinhoodSecurityConfig,
) -> tuple[tuple[FaEntityOverride, ...], GleifLegalAddressAudit, dict[Isin, str]]:
    return resolve_robinhood_legal_entity_overrides(
        (security,),
        GleifLegalAddressLookup((record,), failures={}, cache_hits=1),
    )


def _resolve_adr(
    identity: F6DepositaryIdentity,
    record: GleifRegistrantRecord,
    security: RobinhoodSecurityConfig,
) -> tuple[
    tuple[FaEntityOverride, ...],
    tuple[GleifLegalAddressRecord, ...],
    RobinhoodAdrEntityAudit,
]:
    return resolve_robinhood_adr_entity_overrides(
        (security,),
        RegistrantLookup(((security.instrument.isin, identity, record),), 1, 1, 1),
    )


def test_legal_decision_adds_its_reasons_to_the_fetch_counts() -> None:
    lookup = GleifLegalAddressLookup(
        (
            _legal(),
            _legal(isin=_OTHER_ISIN, registration_status="LAPSED"),
        ),
        failures={_CH_ISIN: "cache-miss"},
        cache_hits=2,
    )

    overrides, audit, outcomes = resolve_robinhood_legal_entity_overrides(
        (_security(), _security(isin=_OTHER_ISIN)),
        lookup,
    )

    assert overrides == (
        FaEntityOverride(
            isin=_ISIN,
            address="99 SAMPLE ST, SAMPLETON, US-DE, US",
            zip_code="99991",
        ),
    )
    assert audit == GleifLegalAddressAudit(3, 1, 2, (("cache-miss", 1), ("lifecycle", 1)))
    assert outcomes == {_ISIN: "resolved", _OTHER_ISIN: "lapsed", _CH_ISIN: "cache-miss"}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param({"lei": "NOT-AN-LEI"}, "incomplete", id="invalid-lei"),
        pytest.param({"registration_status": "RETIRED"}, "lifecycle", id="retired"),
        pytest.param({"entity_status": "INACTIVE"}, "lifecycle", id="inactive"),
        pytest.param({"has_successor": True}, "lifecycle", id="successor"),
        pytest.param({"country_iso2": "CA"}, "country-mismatch", id="country"),
        pytest.param({"address_lines": ()}, "incomplete", id="no-street"),
        pytest.param({"postal_code": ""}, "incomplete", id="no-postal"),
        pytest.param({"address_lines": ("SOCIÉTÉ ADDRESS",)}, "portal-shape", id="non-ascii"),
        pytest.param({"address_lines": ("=2+2",)}, "portal-shape", id="formula-prefix"),
        pytest.param({"address_lines": ("A" * 200,)}, "portal-shape", id="long-address"),
        pytest.param({"postal_code": "99991-1234"}, "portal-shape", id="long-postal"),
    ],
)
def test_legal_decision_refuses_unusable_records(changes: dict[str, object], reason: str) -> None:
    overrides, audit, outcomes = _resolve_legal(replace(_legal(), **changes), _security())

    assert overrides == ()
    assert audit.failure_counts == ((reason, 1),)
    assert outcomes == {_ISIN: reason}


@pytest.mark.parametrize(
    ("changes", "reason", "outcome"),
    [
        pytest.param({}, "lifecycle", "lapsed", id="valid"),
        pytest.param({"lei": "NOT-AN-LEI"}, "incomplete", "incomplete", id="invalid-lei"),
        pytest.param({"legal_name": ""}, "incomplete", "incomplete", id="no-legal-name"),
        pytest.param({"entity_status": "INACTIVE"}, "lifecycle", "lifecycle", id="inactive"),
        pytest.param({"has_successor": True}, "lifecycle", "lifecycle", id="successor"),
        pytest.param({"country_iso2": "CA"}, "lifecycle", "country-mismatch", id="country"),
        pytest.param({"postal_code": ""}, "lifecycle", "incomplete", id="no-postal"),
        pytest.param({"address_lines": ("=2+2",)}, "lifecycle", "portal-shape", id="formula"),
    ],
)
def test_lapsed_record_is_sec_eligible_only_when_otherwise_valid(
    changes: dict[str, object],
    reason: str,
    outcome: str,
) -> None:
    record = replace(_legal(), registration_status="LAPSED")

    overrides, audit, outcomes = _resolve_legal(replace(record, **changes), _security())

    assert overrides == ()
    assert audit.failure_counts == ((reason, 1),)
    assert outcomes == {_ISIN: outcome}


def _alias_record(street: str = "Förrlibuckstrasse 190") -> GleifLegalAddressRecord:
    return _legal(
        isin=_ALIAS_ISIN,
        address_lines=(street,),
        city="Zürich",
        region="CH-ZH",
        country_iso2="CH",
        postal_code="8005",
    )


def test_legal_decision_applies_the_reviewed_ascii_alias() -> None:
    overrides, audit, _outcomes = _resolve_legal(
        _alias_record(), _security(isin=_ALIAS_ISIN, country="CH")
    )

    assert overrides == (
        FaEntityOverride(
            isin=_ALIAS_ISIN,
            address="Forrlibuckstrasse 190, Zurich, CH-ZH, CH",
            zip_code="8005",
        ),
    )
    assert audit.reviewed_ascii_aliases == 1


def test_legal_decision_refuses_a_changed_source_address_without_alias() -> None:
    overrides, audit, _outcomes = _resolve_legal(
        _alias_record("Förrlibuckstrasse 191"),
        _security(isin=_ALIAS_ISIN, country="CH"),
    )

    assert overrides == ()
    assert audit.failure_counts == (("portal-shape", 1),)
    assert audit.reviewed_ascii_aliases == 0


def test_legal_decision_refuses_an_alias_that_is_not_the_source_transliteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = (_ALIAS_ISIN, ("Förrlibuckstrasse 190",), "Zürich", "CH-ZH", "CH", "8005")
    monkeypatch.setattr(
        overrides_mod,
        "_REVIEWED_ADDRESS_ALIASES",
        {key: "Another Street 1, Zurich, CH-ZH, CH"},
    )

    overrides, audit, _outcomes = _resolve_legal(
        _alias_record(), _security(isin=_ALIAS_ISIN, country="CH")
    )

    assert overrides == ()
    assert audit.failure_counts == (("portal-shape", 1),)


def test_adr_decision_resolves_depositary_and_binds_its_gleif_record() -> None:
    lookup = RegistrantLookup(
        ((_ISIN, _f6(), _registrant()),),
        requested=2,
        selection_cache_hits=1,
        gleif_cache_hits=1,
        failure_counts=(("f6-cache-miss", 1),),
    )

    overrides, records, audit = resolve_robinhood_adr_entity_overrides((_security(),), lookup)

    assert overrides == (
        FaEntityOverride(
            isin=_ISIN,
            name="Sample Depositary Bank, National Association",
            address="1 Sample Plaza, Sampleville, US-SD, US",
            zip_code="99992",
        ),
    )
    assert records[0].isin == _ISIN
    assert records[0].legal_form_id == "62VJ"
    assert audit == RobinhoodAdrEntityAudit(2, 1, 1, 1, (("f6-cache-miss", 1),))


@pytest.mark.parametrize(
    ("identity", "changes", "reason"),
    [
        pytest.param(_f6(), {"registration_status": "LAPSED"}, "gleif-lifecycle", id="lapsed"),
        pytest.param(_f6(), {"has_successor": True}, "gleif-lifecycle", id="successor"),
        pytest.param(
            _f6("OTHER DEPOSITARY BANK"),
            {},
            "depositary-name-mismatch",
            id="depositary-name",
        ),
        pytest.param(_f6(), {"country_iso2": "GB"}, "country-mismatch", id="country"),
        pytest.param(_f6(), {"city": ""}, "incomplete-address", id="no-city"),
        pytest.param(
            _f6(),
            {"legal_name": "=Sample Depositary Bank, National Association"},
            "portal-shape",
            id="formula-name",
        ),
        pytest.param(_f6(), {"postal_code": "@99992"}, "portal-shape", id="formula-zip"),
    ],
)
def test_adr_decision_refuses_unusable_evidence(
    identity: F6DepositaryIdentity,
    changes: dict[str, object],
    reason: str,
) -> None:
    overrides, records, audit = _resolve_adr(identity, _registrant(**changes), _security())

    assert overrides == records == ()
    assert audit.failure_counts == ((reason, 1),)


@pytest.mark.parametrize(
    ("street_length", "resolved"),
    [pytest.param(194, True, id="collapsed-fits"), pytest.param(197, False, id="collapsed-long")],
)
def test_portal_text_check_measures_collapsed_length(street_length: int, resolved: bool) -> None:
    record = _registrant(address_lines=("A" * street_length,), city="B", region="")

    overrides, _records, _audit = _resolve_adr(_f6(), record, _security())

    assert bool(overrides) is resolved


def test_fund_decision_names_registrant_and_series() -> None:
    lookup = RegistrantLookup(((_ISIN, _ncen(), _fund_registrant()),), 1, 0, 1)

    overrides, evidence, audit = resolve_robinhood_fund_entity_overrides((_security(),), lookup)

    assert overrides == (
        FaEntityOverride(
            isin=_ISIN,
            name="SAMPLE FUND TRUST - Sample Fund AU ETF",
            address="99 SAMPLE ST, SAMPLETON, US-DE, US",
            zip_code="99991",
        ),
    )
    assert evidence[0].ncen_accession == "0009991009-25-000024"
    assert evidence[0].registrant_name == "SAMPLE FUND TRUST"
    assert evidence[0].gleif_source_sha256 == "a" * 64
    assert audit == RobinhoodFundEntityAudit(1, 1, 0, 1)


@pytest.mark.parametrize(
    ("identity", "changes", "reason"),
    [
        pytest.param(_ncen(), {"entity_status": "INACTIVE"}, "gleif-lifecycle", id="inactive"),
        pytest.param(
            _ncen("Other Fund Advisors, LLC"),
            {},
            "registrant-identity-mismatch",
            id="registrant-name",
        ),
        pytest.param(
            _ncen(),
            {"category": "GENERAL"},
            "registrant-identity-mismatch",
            id="non-fund-category",
        ),
        pytest.param(_ncen(), {"country_iso2": "IE"}, "country-mismatch", id="country"),
        pytest.param(_ncen(), {"postal_code": ""}, "incomplete-address", id="no-postal"),
        pytest.param(_ncen(), {"address_lines": ("99 SAMPLE ST\t",)}, "portal-shape", id="tab"),
    ],
)
def test_fund_decision_refuses_unusable_evidence(
    identity: NcenFundIdentity,
    changes: dict[str, object],
    reason: str,
) -> None:
    overrides, evidence, audit = resolve_robinhood_fund_entity_overrides(
        (_security(),),
        RegistrantLookup(((_ISIN, identity, _fund_registrant(**changes)),), 1, 0, 1),
    )

    assert overrides == evidence == ()
    assert audit.failure_counts == ((reason, 1),)


def test_later_automatic_sources_win_legal_sec_adr_fund() -> None:
    def override(isin: Isin, source: str) -> FaEntityOverride:
        return FaEntityOverride(isin=isin, address=source)

    combined = combine_robinhood_entity_overrides(
        legal=(override(_ISIN, "legal"), override(_OTHER_ISIN, "legal")),
        sec=(override(_ISIN, "sec"), override(_OTHER_ISIN, "sec")),
        adr=(override(_ISIN, "adr"), override(_CH_ISIN, "adr")),
        fund=(override(_CH_ISIN, "fund"),),
    )

    assert combined == (
        (
            override(_CH_ISIN, "fund"),
            override(_OTHER_ISIN, "sec"),
            override(_ISIN, "adr"),
        ),
        {
            _CH_ISIN: RobinhoodEntityRoute.FUND,
            _OTHER_ISIN: RobinhoodEntityRoute.SEC,
            _ISIN: RobinhoodEntityRoute.ADR,
        },
    )
