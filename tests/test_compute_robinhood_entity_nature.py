from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from ibkr_fa.compute.robinhood_entity_nature import (
    _OVERFLOW_ALIASES,
    RobinhoodEntityNatureAudit,
    RobinhoodEntityNatureEvidence,
    bind_robinhood_fund_entities,
    merge_robinhood_entity_natures,
    resolve_robinhood_entity_natures,
    robinhood_entity_nature_supersession_warnings,
)
from ibkr_fa.compute.robinhood_entity_overrides import (
    RobinhoodFundEntityAudit,
    RobinhoodFundEntityEvidence,
)
from ibkr_fa.compute.robinhood_identity import resolve_robinhood_sec_entity_overrides
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    GleifLegalAddressRecord,
    IdentitySourceBundle,
    SecFundRecord,
    SecSubmissionRecord,
    SecTickerRecord,
)
from ibkr_fa.reference.gleif_elf import lookup_gleif_elf
from ibkr_fa.types import Isin
from tests._gleif_records import GLEIF_RECORD
from tests._robinhood_identity_inputs import AQ_BROKER_NAME as _AQ
from tests._robinhood_identity_inputs import AX_BROKER_NAME as _AX
from tests._robinhood_identity_inputs import NATURE_SUPERSEDED_ONE as _NATURE_ONE
from tests._robinhood_identity_inputs import gleif_nature_evidence as _nature
from tests._robinhood_identity_inputs import issuer_identity_sources as _identity_sources
from tests._robinhood_identity_inputs import lapsed_gleif_record as _gleif_record
from tests._robinhood_identity_inputs import observed_aq_identity as _observed
from tests._robinhood_security import robinhood_security

_ISIN = Isin("US99992A1097")
# A synthetic LEI with valid ISO 17442 check digits.
_LEI = "999900SAMPLE00000161"


def _security(
    *,
    isin: Isin = _ISIN,
    symbol: str = "XQAQ",
    sub_category: str = "COMMON",
    depositary: bool = False,
    issuer_country_code: str = "US",
) -> RobinhoodSecurityConfig:
    return robinhood_security(
        symbol=symbol,
        cusip=isin[2:-1],
        isin=isin,
        sub_category=sub_category,
        issuer_country_code=issuer_country_code,
        is_depositary_receipt=depositary,
    )


def _record(
    *,
    isin: Isin = _ISIN,
    legal_form_id: str = "XTIQ",
    legal_form_other: str = "",
    category: str = "GENERAL",
    registration_status: str = "ISSUED",
    entity_status: str = "ACTIVE",
    successor: bool = False,
    country_iso2: str = "US",
) -> GleifLegalAddressRecord:
    return replace(
        GLEIF_RECORD,
        isin=isin,
        lei=_LEI,
        country_iso2=country_iso2,
        category=category,
        legal_form_id=legal_form_id,
        legal_form_other=legal_form_other,
        entity_status=entity_status,
        registration_status=registration_status,
        has_successor=successor,
        source_sha256="a" * 64,
    )


def _empty_sources() -> IdentitySourceBundle:
    return IdentitySourceBundle({}, {}, {}, {}, {"source": "a" * 64})


_XQAJ = Isin("US99990D1054")
_XQAJ_FUND = SecFundRecord(9991004, "S000999904", "C000999941", "XQAJ")


def _fund_sources(forms: tuple[str, ...] = ("497", "N-CEN", "NPORT-P")) -> IdentitySourceBundle:
    return IdentitySourceBundle(
        {},
        {},
        {"XQAJ": (_XQAJ_FUND,)},
        {9991004: SecSubmissionRecord(9991004, "SAMPLE FUNDS AJ", "DE", forms)},
        {"source": "a" * 64},
    )


def _registrant(isin: Isin = _XQAJ, **changes: object) -> RobinhoodFundEntityEvidence:
    """Accepted N-CEN registrant evidence for XQAJ's exact SEC class (synthetic)."""
    evidence = RobinhoodFundEntityEvidence(
        isin=isin,
        name="SAMPLE FUNDS AJ - Sample Fund AJ ETF",
        address="100 SAMPLE BLVD, SAMPLEVILLE, US-PA, US",
        zip_code="19990",
        cik=_XQAJ_FUND.cik,
        series_id=_XQAJ_FUND.series_id,
        class_id=_XQAJ_FUND.class_id,
        ticker="XQAJ",
        ncen_accession="0009991004-25-000001",
        ncen_report_date=date(2024, 12, 31),
        registrant_lei=_LEI,
        registrant_name="SAMPLE FUNDS AJ",
        series_name="Sample Fund AJ ETF",
        gleif_source_sha256="b" * 64,
    )
    return replace(evidence, **changes)


def test_exact_elf_label_is_used_verbatim() -> None:
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(),),
        _empty_sources(),
    )

    assert evidence[0].value == "Corporation"
    assert evidence[0].transformation == "verbatim"
    assert evidence[0].elf_code == "XTIQ"
    assert audit.resolved == 1


def test_overlength_elf_label_uses_reviewed_subsequence_alias() -> None:
    evidence, _audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(legal_form_id="H1UM"),),
        _empty_sources(),
    )

    assert evidence[0].value == "For-Profit Corporation"
    assert evidence[0].raw_value == "For-Profit Corporation General Stock"
    assert evidence[0].transformation == "reviewed-overflow-alias"


def test_reserved_fund_other_uses_exact_allowlist() -> None:
    evidence, _audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(legal_form_id="8888", legal_form_other="FUND", category="FUND"),),
        _empty_sources(),
    )

    assert evidence[0].value == "Fund"
    assert evidence[0].raw_value == "FUND"


def test_9999_record_remains_manual() -> None:
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(legal_form_id="9999"),),
        _empty_sources(),
    )

    assert evidence == ()
    assert audit.failure_counts == (("no-legal-form", 1),)


def _assert_nature_manual(
    record: GleifLegalAddressRecord, reason: str, *, issuer_country_code: str = "US"
) -> None:
    """Assert that the input leaves the nature manual without SEC fallback."""
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(issuer_country_code=issuer_country_code),),
        (),
        (record,),
        _empty_sources(),
    )

    assert evidence == ()
    assert audit.failure_counts == ((reason, 1),)


@pytest.mark.parametrize(
    ("legal_form_other", "category"),
    [
        pytest.param("Trust", "FUND", id="other-outside-allowlist"),
        pytest.param("FUND", "GENERAL", id="general-category"),
    ],
)
def test_reserved_form_outside_fund_allowlist_remains_manual(
    legal_form_other: str, category: str
) -> None:
    record = _record(legal_form_id="8888", legal_form_other=legal_form_other, category=category)

    _assert_nature_manual(record, "reserved-form")


def test_record_country_other_than_issuer_country_remains_manual() -> None:
    # The default record resolves for a US issuer (test_exact_elf_label_is_used_verbatim).
    _assert_nature_manual(_record(), "country-mismatch", issuer_country_code="CA")


def test_unvendored_elf_code_remains_manual() -> None:
    assert lookup_gleif_elf("ZZZZ") is None

    _assert_nature_manual(_record(legal_form_id="ZZZZ"), "elf-label-unavailable")


def test_inactive_elf_code_remains_manual() -> None:
    # Only the status refuses it: a short label for the record's country.
    entry = lookup_gleif_elf("UFIY")
    assert entry is not None
    assert (entry.status, entry.country_iso2) == ("INAC", "US")
    assert 0 < len(entry.english_name) <= 34

    _assert_nature_manual(_record(legal_form_id="UFIY"), "elf-label-unavailable")


def test_unlabelled_elf_code_remains_manual() -> None:
    # Use FR for both countries so only the empty label refuses this active entry.
    entry = lookup_gleif_elf("157O")
    assert entry is not None
    assert (entry.status, entry.country_iso2, entry.english_name) == ("ACTV", "FR", "")

    _assert_nature_manual(
        _record(legal_form_id="157O", country_iso2="FR"),
        "elf-label-unavailable",
        issuer_country_code="FR",
    )


def test_overlength_elf_label_without_reviewed_alias_remains_manual() -> None:
    entry = lookup_gleif_elf("12N6")
    assert entry is not None
    assert (entry.status, entry.country_iso2) == ("ACTV", "US")
    assert len(entry.english_name) > 34
    assert entry.english_name not in _OVERFLOW_ALIASES

    _assert_nature_manual(_record(legal_form_id="12N6"), "elf-label-unusable")


def test_active_lapsed_record_can_supply_stable_legal_form() -> None:
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(registration_status="LAPSED"),),
        _empty_sources(),
        lapsed_gleif_isins=frozenset({_ISIN}),
    )

    assert evidence[0].value == "Corporation"
    assert evidence[0].source == "gleif-elf-lapsed"
    assert evidence[0].transformation == "verbatim-lapsed-record"
    assert audit.resolved == 1


def test_lapsed_legal_form_still_requires_active_entity_without_successor() -> None:
    for record in (
        _record(registration_status="LAPSED", entity_status="INACTIVE"),
        _record(registration_status="LAPSED", successor=True),
    ):
        evidence, audit = resolve_robinhood_entity_natures(
            (_security(),),
            (),
            (record,),
            _empty_sources(),
            lapsed_gleif_isins=frozenset({_ISIN}),
        )

        assert evidence == ()
        assert audit.failure_counts == (("lifecycle", 1),)


def test_lapsed_legal_form_without_sec_corroboration_remains_manual() -> None:
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(registration_status="LAPSED"),),
        _empty_sources(),
    )

    assert evidence == ()
    assert audit.failure_counts == (("lifecycle", 1),)


def test_exact_sec_fund_class_and_forms_prove_investment_company() -> None:
    security = _security(isin=_XQAJ, symbol="XQAJ", sub_category="ETF")

    evidence, audit = resolve_robinhood_entity_natures(
        (security,),
        (),
        (),
        _fund_sources(),
        fund_evidence=(_registrant(),),
    )

    assert evidence[0].value == "Investment Company"
    assert evidence[0].sec_cik == 9991004
    assert evidence[0].sec_series_id == "S000999904"
    assert evidence[0].sec_class_id == "C000999941"
    assert evidence[0].sec_forms == ("N-CEN", "NPORT-P")
    assert audit.resolved == 1


def test_sec_state_of_incorporation_proves_common_stock_corporation() -> None:
    sources = IdentitySourceBundle(
        {},
        {"XQAQ": (SecTickerRecord(9991029, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"),)},
        {},
        {9991029: SecSubmissionRecord(9991029, "Sample Issuer AQ Inc.", "CA", ("10-K",))},
        {"source": "a" * 64},
    )

    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (),
        sources,
        sec_common_isins=frozenset({_ISIN}),
    )

    assert evidence[0].value == "Corporation"
    assert evidence[0].source == "sec-state-of-incorporation"
    assert evidence[0].sec_cik == 9991029
    assert evidence[0].sec_forms == ("10-K",)
    assert audit.resolved == 1


def test_lapsed_name_bridge_uses_independent_sec_nature() -> None:
    sources = IdentitySourceBundle(
        {},
        {"XQAQ": (SecTickerRecord(9991029, "Sample Issuer AQ Inc.", "XQAQ", "Nasdaq"),)},
        {},
        {9991029: SecSubmissionRecord(9991029, "Sample Issuer AQ Inc.", "CA", ("10-K",))},
        {"source": "a" * 64},
    )

    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(registration_status="LAPSED"),),
        sources,
        sec_common_isins=frozenset({_ISIN}),
    )

    assert evidence[0].source == "sec-state-of-incorporation"
    assert audit.resolved == 1


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param((_AQ, _AX), ("sec-state-of-incorporation", "Corporation"), id="mixed"),
        pytest.param((_AQ,), ("gleif-elf-lapsed", "For-Profit Corporation"), id="agreeing"),
    ],
)
def test_lapsed_legal_form_needs_every_broker_name_to_agree(
    names: tuple[str, ...], expected: tuple[str, str]
) -> None:
    """With an agreeing lapsed GLEIF record, the SEC fallback names the broker
    as its name source, which lets the lapsed legal form supply the nature,
    only when every broker name agrees (#1082)."""
    _unused, sources = _identity_sources(name=_AQ, nasdaq_name=_AQ)
    record = replace(_gleif_record(_ISIN, _AQ), legal_form_id="H1UM")
    _overrides, sec_evidence, _audit = resolve_robinhood_sec_entity_overrides(
        (_security(),), (_observed(*names),), sources, (record,), {_ISIN: "lapsed"}
    )

    # As ``pipeline_robinhood_entities`` builds both sets.
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (record,),
        sources,
        sec_common_isins=frozenset(item.isin for item in sec_evidence),
        lapsed_gleif_isins=frozenset(
            item.isin for item in sec_evidence if item.name_source == "broker"
        ),
    )

    assert [(item.source, item.value) for item in evidence] == [expected]
    assert audit.resolved == 1


def test_sec_common_fallback_does_not_relabel_partnership_as_corporation() -> None:
    security = _security(symbol="XQFA")
    sources = IdentitySourceBundle(
        {},
        {"XQFA": (SecTickerRecord(9991070, "Sample Issuer FA LP", "XQFA", "NYSE"),)},
        {},
        {9991070: SecSubmissionRecord(9991070, "Sample Issuer FA LP", "DE", ("10-K",))},
        {"source": "a" * 64},
    )

    evidence, audit = resolve_robinhood_entity_natures(
        (security,),
        (),
        (),
        sources,
        sec_common_isins=frozenset({_ISIN}),
    )

    assert evidence == ()
    assert audit.failure_counts == (("gleif-unavailable", 1),)


def test_fund_name_or_unverified_form_never_proves_nature() -> None:
    security = _security(isin=_XQAJ, symbol="XQAJ", sub_category="ETF")

    evidence, audit = resolve_robinhood_entity_natures(
        (security,),
        (),
        (),
        _fund_sources(forms=("497",)),
        fund_evidence=(_registrant(),),
    )

    assert evidence == ()
    assert audit.failure_counts == (("sec-fund-unproved", 1),)


@pytest.mark.parametrize(
    ("registrant", "reason"),
    [
        pytest.param((), "registrant-unresolved", id="no-registrant"),
        pytest.param((_registrant(isin=_ISIN),), "registrant-unresolved", id="other-isin"),
        pytest.param((_registrant(cik=_XQAJ_FUND.cik + 1),), "registrant-mismatch", id="cik"),
        pytest.param((_registrant(series_id="S000999101"),), "registrant-mismatch", id="series"),
        pytest.param((_registrant(class_id="C000999101"),), "registrant-mismatch", id="class"),
    ],
)
def test_etf_nature_needs_its_registrant_class(
    registrant: tuple[RobinhoodFundEntityEvidence, ...],
    reason: str,
) -> None:
    """#793: the direct record's legal form never stands in for the registrant."""
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(isin=_XQAJ, symbol="XQAJ", sub_category="ETF"),),
        (),
        (_record(isin=_XQAJ),),
        _fund_sources(),
        fund_evidence=registrant,
    )

    assert evidence == ()
    assert audit.failure_counts == ((reason, 1),)


@pytest.mark.parametrize(
    "preferred",
    [False, True],
    ids=["no-preference", "preferred"],
)
def test_configured_etf_gets_no_automatic_nature(preferred: bool) -> None:
    manual = FaEntityOverride(isin=_XQAJ, address="Reviewed", prefer_configured_nature=preferred)

    evidence, audit = resolve_robinhood_entity_natures(
        (_security(isin=_XQAJ, symbol="XQAJ", sub_category="ETF"),),
        (manual,),
        (_record(isin=_XQAJ),),
        _fund_sources(),
        fund_evidence=(_registrant(),),
    )

    assert evidence == ()
    assert audit == RobinhoodEntityNatureAudit(resolved=0)


def test_fund_binding_drops_registrant_cells_without_a_bound_nature() -> None:
    bound, unbound = _registrant(), _registrant(isin=_ISIN)
    overrides = tuple(
        FaEntityOverride(isin=item.isin, name=item.name, address=item.address, zip_code="1")
        for item in (unbound, bound)
    )
    audit = RobinhoodFundEntityAudit(3, 2, 0, 0, (("ncen-cache-miss", 1),))
    nature = RobinhoodEntityNatureEvidence(
        isin=_XQAJ,
        value="Investment Company",
        source="sec-registered-investment-company",
        transformation="exact-filing-form-classification",
        raw_value="Investment Company",
    )

    kept, evidence, bound_audit = bind_robinhood_fund_entities(
        overrides, (unbound, bound), audit, (nature,)
    )

    assert kept == overrides[1:]
    assert evidence == (bound,)
    assert bound_audit == RobinhoodFundEntityAudit(
        3, 1, 0, 0, (("nature-unbound", 1), ("ncen-cache-miss", 1))
    )
    assert bind_robinhood_fund_entities(overrides[1:], (bound,), audit, (nature,)) == (
        overrides[1:],
        (bound,),
        audit,
    )


def test_adr_and_explicit_manual_preference_stay_manual() -> None:
    manual = FaEntityOverride(
        isin=_ISIN,
        address="Reviewed",
        nature_of_entity="Reviewed Company",
        prefer_configured_nature=True,
    )
    evidence, audit = resolve_robinhood_entity_natures(
        (_security(depositary=True),),
        (manual,),
        (_record(),),
        _empty_sources(),
    )

    assert evidence == ()
    assert audit.failure_counts == ()


@pytest.mark.parametrize("sub_category", ["COMMON", "ETF"])
def test_adr_legal_form_requires_proven_depositary_record(sub_category: str) -> None:
    security = _security(depositary=True, sub_category=sub_category)
    record = _record()

    excluded, audit = resolve_robinhood_entity_natures(
        (security,),
        (),
        (record,),
        _empty_sources(),
    )
    resolved, resolved_audit = resolve_robinhood_entity_natures(
        (security,),
        (),
        (record,),
        _empty_sources(),
        depositary_isins=frozenset({_ISIN}),
    )

    assert excluded == ()
    assert audit.failure_counts == (("adr-excluded", 1),)
    assert resolved[0].value == "Corporation"
    assert resolved_audit.resolved == 1


def test_nature_merge_is_source_first_with_independent_opt_out() -> None:
    automatic, _audit = resolve_robinhood_entity_natures(
        (_security(),),
        (),
        (_record(),),
        _empty_sources(),
    )
    fallback = FaEntityOverride(
        isin=_ISIN,
        address="Reviewed",
        nature_of_entity="Reviewed Company",
        prefer_configured_address=True,
    )
    preferred = FaEntityOverride(
        isin=_ISIN,
        address="Reviewed",
        nature_of_entity="Reviewed Company",
        prefer_configured_nature=True,
    )

    assert merge_robinhood_entity_natures(automatic, (fallback,))[_ISIN] == "Corporation"
    assert merge_robinhood_entity_natures(automatic, (preferred,))[_ISIN] == "Reviewed Company"


_ISSUER_AQ = Isin("US99992A1097")
_ISSUER_CA = Isin("US99972B1017")
_NATURE_TWO = (
    "automatic legal-form evidence superseded 2 configured Robinhood nature values; "
    "set prefer_configured_nature = true to retain reviewed values"
)


def test_nature_supersession_is_counted_singular_and_plural() -> None:
    automatic = (_nature(_ISSUER_AQ), _nature(_ISSUER_CA))
    configured = tuple(
        FaEntityOverride(isin=isin, address="Old address", nature_of_entity="Company")
        for isin in (_ISSUER_AQ, _ISSUER_CA)
    )

    assert robinhood_entity_nature_supersession_warnings(automatic, configured[:1]) == (
        _NATURE_ONE,
    )
    assert robinhood_entity_nature_supersession_warnings(automatic, configured) == (_NATURE_TWO,)


@pytest.mark.parametrize(
    "configured",
    [
        FaEntityOverride(isin=_ISSUER_AQ, address="Old address"),
        FaEntityOverride(isin=_ISSUER_CA, address="Old address", nature_of_entity="Company"),
        FaEntityOverride(
            isin=_ISSUER_AQ,
            address="Old address",
            nature_of_entity="Company",
            prefer_configured_nature=True,
        ),
        FaEntityOverride(isin=_ISSUER_AQ, address="Old address", nature_of_entity="Corporation"),
    ],
    ids=["no-configured-nature", "no-automatic-nature", "preferred", "same-nature"],
)
def test_nature_supersession_skips_unreplaced_values(configured: FaEntityOverride) -> None:
    assert (
        robinhood_entity_nature_supersession_warnings((_nature(_ISSUER_AQ),), (configured,)) == ()
    )
