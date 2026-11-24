from __future__ import annotations

from dataclasses import replace

import pytest

from ibkr_fa.compute import robinhood_identity
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodIdentityFailure,
    RobinhoodIdentityResolution,
    RobinhoodSecEntityAudit,
    merge_robinhood_entity_overrides,
    observed_robinhood_identity_inputs,
    resolve_robinhood_sec_entity_overrides,
    resolve_robinhood_security_table,
    resolve_robinhood_standard_identities,
    robinhood_entity_override_supersession_warnings,
    robinhood_identity_manual_fields,
)
from ibkr_fa.compute.robinhood_security_identities import (
    RobinhoodIdentityCollisionError,
    RobinhoodSecurityIdentities,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig
from ibkr_fa.models.reference_identity import (
    GleifLegalAddressRecord,
    IdentitySourceBundle,
    ObservedIdentityInput,
    SecSubmissionRecord,
)
from ibkr_fa.robinhood_identity_sources import identity_input_fingerprint
from ibkr_fa.types import Isin, isin_check_digit
from tests._no_echo import CUSIP, HOLDER, TICKER, assert_no_markers
from tests._robinhood_account_inputs import ZQ_ISSUER as _ZQ_ISSUER
from tests._robinhood_account_inputs import ZQ_NEW as _ZQ_NEW
from tests._robinhood_account_inputs import ZQ_OLD as _ZQ_OLD
from tests._robinhood_account_inputs import pair_with_zq_symbols as _pair_with_zq_symbols
from tests._robinhood_fa_address import ADDRESS_SUPERSEDED_ONE as _ADDRESS_ONE
from tests._robinhood_identity_inputs import AQ_BROKER_NAME as _AQ
from tests._robinhood_identity_inputs import AX_BROKER_NAME as _AX
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN as _ISSUER_AQ
from tests._robinhood_identity_inputs import NAME_SUPERSEDED_ONE as _NAME_ONE
from tests._robinhood_identity_inputs import ZQ_AMBIGUOUS_CHECKS as _ZQ_AMBIGUOUS
from tests._robinhood_identity_inputs import automatic_entity_override as _automatic
from tests._robinhood_identity_inputs import issuer_identity_sources as _sources
from tests._robinhood_identity_inputs import lapsed_gleif_record as _gleif_record
from tests._robinhood_identity_inputs import observed_aq_identity as _observed
from tests._robinhood_identity_inputs import zq_identity_sources as _zq_sources
from tests._robinhood_security import robinhood_security


def test_resolves_domestic_common_stock() -> None:
    observed, sources = _sources()
    result = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (),
    )
    assert result.failures == ()
    (security,) = result.generated
    assert security.instrument.symbol == "XQAQ"
    assert security.cusip == "99992A109"
    assert security.instrument.isin == Isin("US99992A1097")
    assert security.instrument.listing_exchange == "NASDAQ"
    assert security.sub_category == "COMMON"
    assert security.asset_class == "STK"
    assert security.instrument.issuer_country_code == "US"
    assert security.is_depositary_receipt is False
    assert security.source.startswith("automatic official identity snapshot ")


def test_sec_business_address_fallback_is_exact_and_portal_safe() -> None:
    observed, sources = _sources(business_zip="99990-0123")
    security = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (),
    ).generated[0]

    overrides, evidence, audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        sources,
        (),
        {security.instrument.isin: "zero-match"},
    )

    assert overrides == (
        FaEntityOverride(
            isin=security.instrument.isin,
            address="1 ISSUER WAY, SAMPLETOWN, US-CA, US",
            zip_code="99990",
        ),
    )
    assert evidence[0].cik == 9991029
    assert evidence[0].zip_transformation == "us-zip5-from-zip-plus4"
    assert evidence[0].name_source == "broker"
    assert audit.resolved == 1
    assert audit.zip5_from_zip4 == 1


def test_sec_business_address_fallback_rejects_changed_name_or_foreign_address() -> None:
    observed, sources = _sources()
    security = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (),
    ).generated[0]
    changed_submission = {
        **sources.sec_submissions_by_cik,
        9991029: SecSubmissionRecord(
            9991029,
            "Samplewood Holdings Corp",
            "CA",
            ("10-K",),
            "1 ISSUER WAY",
            "",
            "SAMPLETOWN",
            "CA",
            "99990",
        ),
    }
    changed = replace(sources, sec_submissions_by_cik=changed_submission)
    _overrides, _evidence, changed_audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        changed,
        (),
        {security.instrument.isin: "zero-match"},
    )
    foreign = replace(
        sources,
        sec_submissions_by_cik={
            9991029: replace(
                sources.sec_submissions_by_cik[9991029],
                business_state_or_country="U0",
            )
        },
    )
    _overrides, _evidence, foreign_audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        foreign,
        (),
        {security.instrument.isin: "zero-match"},
    )

    assert changed_audit.failure_counts == (("name-mismatch", 1),)
    assert foreign_audit.failure_counts == (("business-country", 1),)


def test_sec_fallback_can_cross_check_brand_name_with_exact_isin_gleif_name() -> None:
    _observed, sources = _sources()
    observed = ObservedIdentityInput("99992A109", "XQAQ", "DISPLAY NAME VARIANT")
    security = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")

    overrides, evidence, audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        sources,
        (_gleif_record(security.instrument.isin, "Sample Issuer AQ Inc."),),
        {security.instrument.isin: "lapsed"},
    )

    assert len(overrides) == 1
    assert evidence[0].name_source == "gleif-lapsed"
    assert audit.resolved == 1

    inactive = replace(
        _gleif_record(security.instrument.isin, "Sample Issuer AQ Inc."),
        entity_status="INACTIVE",
    )
    _overrides, _evidence, inactive_audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        sources,
        (inactive,),
        {security.instrument.isin: "lapsed"},
    )
    assert inactive_audit.failure_counts == (("name-mismatch", 1),)


def test_sec_fallback_needs_a_conflicting_gleif_name_to_agree_despite_the_broker() -> None:
    observed, sources = _sources()
    security = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")

    overrides, evidence, audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        sources,
        (_gleif_record(security.instrument.isin, "Samplewood Inc."),),
        {security.instrument.isin: "lapsed"},
    )

    assert (overrides, evidence) == ((), ())
    assert audit.failure_counts == (("name-mismatch", 1),)


def test_sec_fallback_counts_securities_their_gleif_outcome_blocks() -> None:
    observed, sources = _sources()
    security = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")
    blocked = (
        robinhood_security(symbol="ONE", cusip="99971M108", isin="US99971M1080"),
        robinhood_security(symbol="XQGC", cusip="99971N106", isin="US99971N1063"),
    )
    outcomes = {
        security.instrument.isin: "zero-match",
        blocked[0].instrument.isin: "country-mismatch",
        blocked[1].instrument.isin: "multi-match",
    }

    overrides, _evidence, audit = resolve_robinhood_sec_entity_overrides(
        (*blocked, security), (observed,), sources, (), outcomes
    )
    _overrides, _evidence, unavailable = resolve_robinhood_sec_entity_overrides(
        (*blocked, security), (observed,), None, (), outcomes
    )
    _overrides, _evidence, only_blocked = resolve_robinhood_sec_entity_overrides(
        blocked, (observed,), sources, (), outcomes
    )

    assert [item.isin for item in overrides] == [security.instrument.isin]
    assert audit.gleif_blocked_counts == (("country-mismatch", 1), ("multi-match", 1))
    assert audit.warnings == (
        "SEC business-address fallback supplied 1 security",
        "GLEIF evidence blocked the SEC business-address fallback for 2 securities "
        "(country-mismatch:1, multi-match:1)",
    )
    assert unavailable.failure_counts == (("source-unavailable", 1),)
    assert unavailable.gleif_blocked_counts == audit.gleif_blocked_counts
    assert unavailable.warnings[-1] == (
        "SEC business-address fallback supplied no address for 1 security (source-unavailable:1)"
    )
    assert only_blocked == RobinhoodSecEntityAudit(
        0, gleif_blocked_counts=(("country-mismatch", 1), ("multi-match", 1))
    )


def test_resolves_etf_only_with_sec_fund_record() -> None:
    observed, sources = _sources(
        symbol="XQDG",
        cusip="99972N105",
        name="SAMPLE ISSUER DG ETF Trust",
        nasdaq_name="SAMPLE ISSUER DG ETF Trust Units",
        etf="Y",
        fund=True,
    )
    result = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (),
    )
    assert result.failures == ()
    assert result.generated[0].sub_category == "ETF"


# --- Every observed broker name must agree (#1082) ------------------------------

_EXTRA_WORD = "SAMPLE ISSUER AQ NEUTRAL TWO"


def _checks(observed: ObservedIdentityInput, sources: IdentitySourceBundle) -> tuple[str, ...]:
    """The failed checks of the one CUSIP, or () when it resolves."""
    result = resolve_robinhood_standard_identities((observed,), sources, ())
    assert len(result.generated) + len(result.failures) == 1
    return result.failures[0].checks if result.failures else ()


@pytest.mark.parametrize(
    ("etf", "names", "expected"),
    [
        pytest.param(False, (_AQ, _AX), ("nasdaq-name-mismatch", "sec-name-mismatch"), id="mixed"),
        pytest.param(True, (_AQ, _AX), ("nasdaq-name-mismatch",), id="etf-mixed"),
        pytest.param(False, (_EXTRA_WORD,), ("nasdaq-name-mismatch",), id="extra-word"),
        pytest.param(True, (_EXTRA_WORD,), ("nasdaq-name-mismatch",), id="etf-extra-word"),
    ],
)
def test_every_broker_name_must_agree_word_for_word_with_nasdaq(
    etf: bool, names: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """One agreeing name never outvotes a conflicting one. After dropping noise
    and ``CO``, no broker word may be absent from the Nasdaq name."""
    _unused, sources = _sources(name=_AQ, nasdaq_name=_AQ, etf="Y" if etf else "N", fund=etf)

    assert _checks(_observed(*names), sources) == expected


def test_etf_whose_broker_names_agree_still_resolves_through_its_fund_record() -> None:
    _unused, sources = _sources(name=_AQ, nasdaq_name=_AQ, etf="Y", fund=True)

    result = resolve_robinhood_standard_identities(
        (_observed(_AQ, "Sample Issuer AQ, Inc."),), sources, ()
    )

    assert result.failures == ()
    assert result.generated[0].sub_category == "ETF"


def test_broker_co_still_needs_todays_either_way_agreement() -> None:
    """Dropping ``CO`` alone would let ``SAMPLE CO`` pass against a Nasdaq name
    without ``CO``; today's check still refuses it."""
    _unused, sources = _sources(nasdaq_name="SAMPLE ISSUER AQ Units", etf="Y", fund=True)

    assert _checks(_observed("SAMPLE CO"), sources) == ("nasdaq-name-mismatch",)


@pytest.mark.parametrize(
    ("names", "nasdaq_name"),
    [
        pytest.param(("SAMPLE ISSUER",), _AQ, id="shortened"),
        pytest.param((_AQ,), "Sample Issuer AQ Inc. - Class B Capital Stock", id="class-b"),
        pytest.param(("SAMPLE ISSUER AQ CO",), "SAMPLE ISSUER AQ COMPANY", id="co-company"),
        pytest.param(("SAMPLE ISSUER AQ CO",), _AQ, id="co-inc"),
        pytest.param((_AQ, "Sample Issuer AQ, Inc."), _AQ, id="variants"),
    ],
)
def test_broker_names_within_the_nasdaq_name_still_resolve(
    names: tuple[str, ...], nasdaq_name: str
) -> None:
    _unused, sources = _sources(name=nasdaq_name, nasdaq_name=nasdaq_name)

    assert _checks(_observed(*names), sources) == ()


def _fallback(
    observed: ObservedIdentityInput, gleif: tuple[GleifLegalAddressRecord, ...]
) -> tuple[tuple[FaEntityOverride, ...], tuple[str, ...], RobinhoodSecEntityAudit]:
    """The SEC business-address fallback for ``XQAQ``, whose SEC names are ``_AQ``."""
    _unused, sources = _sources(name=_AQ, nasdaq_name=_AQ)
    security = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")
    overrides, evidence, audit = resolve_robinhood_sec_entity_overrides(
        (security,),
        (observed,),
        sources,
        gleif,
        {security.instrument.isin: "lapsed" if gleif else "zero-match"},
    )
    return overrides, tuple(item.name_source for item in evidence), audit


@pytest.mark.parametrize("names", [(_AQ, _AX), ()], ids=["mixed", "no-name"])
def test_sec_fallback_without_gleif_needs_every_broker_name_to_agree(
    names: tuple[str, ...],
) -> None:
    assert _fallback(_observed(*names), ()) == (
        (),
        (),
        RobinhoodSecEntityAudit(0, (("name-mismatch", 1),)),
    )


def test_sec_fallback_without_gleif_accepts_agreeing_name_variants() -> None:
    overrides, name_sources, _audit = _fallback(_observed(_AQ, "Sample Issuer AQ, Inc."), ())

    assert len(overrides) == 1
    assert name_sources == ("broker",)


@pytest.mark.parametrize("names", [(_AQ, _AX), ()], ids=["mixed", "no-name"])
def test_sec_fallback_names_a_lapsed_gleif_source_unless_every_broker_name_agrees(
    names: tuple[str, ...],
) -> None:
    """The agreeing lapsed record still gates the address; the broker becomes
    the name source only when every one of its names agrees."""
    gleif = (_gleif_record(Isin("US99992A1097"), _AQ),)

    overrides, name_sources, audit = _fallback(_observed(*names), gleif)

    assert len(overrides) == 1
    assert name_sources == ("gleif-lapsed",)
    assert audit == RobinhoodSecEntityAudit(1)


def test_refuses_adr_and_unproved_jurisdiction() -> None:
    observed, sources = _sources(
        symbol="XQCD",
        cusip="99972A103",
        name="SAMPLE ISSUER CD Limited",
        nasdaq_name="SAMPLE ISSUER CD Limited American Depositary Shares",
        state="",
        forms=("20-F", "F-6"),
    )
    result = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (),
    )
    assert result.generated == ()
    assert "security-type-unsupported" in result.failures[0].checks
    assert "us-jurisdiction-unproved" in result.failures[0].checks


def test_manual_cusip_is_not_regenerated() -> None:
    observed, sources = _sources()
    manual = robinhood_security(
        symbol="XQAQ", cusip="99992A109", isin="US99992A1097", source="manual"
    )
    result = resolve_robinhood_standard_identities(
        (observed,),
        sources,
        (manual,),
    )
    assert result.generated == ()
    assert result.failures == ()


def test_automatic_address_pair_wins_while_manual_name_and_nature_fill_gaps() -> None:
    automatic = FaEntityOverride(
        isin=Isin("US99992A1097"),
        address="Sample Agent Center, Sampleton, US-DE, US",
        zip_code="99991",
    )
    manual = FaEntityOverride(
        isin=Isin("US99992A1097"),
        address="Reviewed Address",
        zip_code="99999",
        name="Reviewed Name",
        nature_of_entity="Corporation",
    )

    merged = merge_robinhood_entity_overrides((automatic,), (manual,))

    assert merged[manual.isin] == FaEntityOverride(
        isin=manual.isin,
        address="Sample Agent Center, Sampleton, US-DE, US",
        zip_code="99991",
        name="Reviewed Name",
        nature_of_entity="Corporation",
    )


def test_manual_address_pair_is_used_when_automatic_pair_is_unavailable() -> None:
    manual = FaEntityOverride(
        isin=Isin("US99992A1097"),
        address="Reviewed Address",
        zip_code="99999",
    )

    assert merge_robinhood_entity_overrides((), (manual,)) == {manual.isin: manual}


def test_explicit_preference_keeps_configured_address_pair() -> None:
    automatic = FaEntityOverride(
        isin=Isin("US99992A1097"),
        address="Sample Agent Center, Sampleton, US-DE, US",
        zip_code="99991",
    )
    manual = FaEntityOverride(
        isin=automatic.isin,
        address="Reviewed Address",
        zip_code="99999",
        prefer_configured_address=True,
    )

    assert merge_robinhood_entity_overrides((automatic,), (manual,)) == {manual.isin: manual}


def test_address_and_name_preferences_are_independent() -> None:
    automatic = FaEntityOverride(
        isin=Isin("US99992D2027"),
        address="Registrant legal address",
        zip_code="99991",
        name="SAMPLE FUND TRUST - Sample Fund AU ETF",
    )
    manual_address = FaEntityOverride(
        isin=automatic.isin,
        address="Reviewed address",
        zip_code="99999",
        name="Old name",
        prefer_configured_address=True,
    )
    manual_name = FaEntityOverride(
        isin=automatic.isin,
        address="Old address",
        zip_code="88888",
        name="Reviewed name",
        prefer_configured_name=True,
    )

    address_result = merge_robinhood_entity_overrides((automatic,), (manual_address,))[
        automatic.isin
    ]
    name_result = merge_robinhood_entity_overrides((automatic,), (manual_name,))[automatic.isin]

    assert (address_result.address, address_result.zip_code, address_result.name) == (
        "Reviewed address",
        "99999",
        automatic.name,
    )
    assert (name_result.address, name_result.zip_code, name_result.name) == (
        automatic.address,
        automatic.zip_code,
        "Reviewed name",
    )


_ISSUER_CA = Isin("US99972B1017")
_ADDRESS_TWO = (
    "automatic legal-address evidence superseded 2 configured Robinhood entity addresses; "
    "set prefer_configured_address = true to retain reviewed values"
)
_NAME_TWO = (
    "automatic legal-issuer evidence superseded 2 configured Robinhood entity names; "
    "set prefer_configured_name = true to retain reviewed values"
)


def test_address_supersession_is_counted_singular_and_plural() -> None:
    automatic = (_automatic(_ISSUER_AQ), _automatic(_ISSUER_CA))
    configured = tuple(
        FaEntityOverride(isin=isin, address="Old address", zip_code="19801")
        for isin in (_ISSUER_AQ, _ISSUER_CA)
    )

    assert robinhood_entity_override_supersession_warnings(automatic, configured[:1]) == (
        _ADDRESS_ONE,
    )
    assert robinhood_entity_override_supersession_warnings(automatic, configured) == (_ADDRESS_TWO,)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (
            FaEntityOverride(isin=_ISSUER_AQ, address="1 Legal Way", zip_code="99999"),
            (_ADDRESS_ONE,),
        ),
        (FaEntityOverride(isin=_ISSUER_AQ, address="1 Legal Way", zip_code="19801"), ()),
        (
            FaEntityOverride(
                isin=_ISSUER_AQ,
                address="Old address",
                zip_code="99999",
                prefer_configured_address=True,
            ),
            (),
        ),
        (FaEntityOverride(isin=_ISSUER_CA, address="Old address", zip_code="99999"), ()),
    ],
    ids=["zip-only", "same-pair", "preferred", "no-automatic-override"],
)
def test_address_supersession_counts_only_a_replaced_pair(
    configured: FaEntityOverride,
    expected: tuple[str, ...],
) -> None:
    assert (
        robinhood_entity_override_supersession_warnings((_automatic(_ISSUER_AQ),), (configured,))
        == expected
    )


def test_name_supersession_is_counted_singular_and_plural() -> None:
    automatic = (
        _automatic(_ISSUER_AQ, name="Sample Issuer AQ Inc."),
        _automatic(_ISSUER_CA, name="SAMPLE ISSUER CA"),
    )
    configured = tuple(
        FaEntityOverride(isin=isin, address="1 Legal Way", zip_code="19801", name="Old name")
        for isin in (_ISSUER_AQ, _ISSUER_CA)
    )

    assert robinhood_entity_override_supersession_warnings(automatic, configured[:1]) == (
        _NAME_ONE,
    )
    assert robinhood_entity_override_supersession_warnings(automatic, configured) == (_NAME_TWO,)


@pytest.mark.parametrize(
    ("automatic_name", "configured_name", "preferred"),
    [
        ("Sample Issuer AQ Inc.", None, False),
        (None, "Old name", False),
        ("Sample Issuer AQ Inc.", "Old name", True),
        ("Sample Issuer AQ Inc.", "Sample Issuer AQ Inc.", False),
    ],
    ids=["no-configured-name", "no-automatic-name", "preferred", "same-name"],
)
def test_name_supersession_skips_unreplaced_names(
    automatic_name: str | None,
    configured_name: str | None,
    preferred: bool,
) -> None:
    configured = FaEntityOverride(
        isin=_ISSUER_AQ,
        address="1 Legal Way",
        zip_code="19801",
        name=configured_name,
        prefer_configured_name=preferred,
    )

    assert (
        robinhood_entity_override_supersession_warnings(
            (_automatic(_ISSUER_AQ, name=automatic_name),), (configured,)
        )
        == ()
    )


def test_merge_keeps_configured_then_generated_apart() -> None:
    configured = robinhood_security(symbol="XQCA", cusip="99972B101", isin="US99972B1017")
    generated = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")

    merged = RobinhoodSecurityIdentities((configured,), (generated,))

    assert merged == RobinhoodSecurityIdentities(configured=(configured,), generated=(generated,))
    assert merged.securities == (configured, generated)


def test_identity_table_has_no_merge_wrapper() -> None:
    """``RobinhoodSecurityIdentities`` merges and checks collisions itself (#1137)."""
    assert not hasattr(robinhood_identity, "merge_robinhood_security_identities")


_COLLISION = (
    "Robinhood security identities collide; every security needs its own CUSIP, ISIN and symbol:"
)
_SNAPSHOT = "automatic official identity snapshot 0123456789ab"


@pytest.mark.parametrize("other", ["configured", "generated"])
def test_merge_rejects_duplicate_canonical_symbol(other: str) -> None:
    """A generated symbol may repeat a configured one, or another generated one."""
    issuer_aq, sources = _sources()
    (generated,) = resolve_robinhood_standard_identities((issuer_aq,), sources, ()).generated
    assert generated.source.startswith("automatic official identity snapshot ")
    if other == "configured":
        inputs = (issuer_aq,)
        configured = (
            robinhood_security(symbol="XQCA", cusip="99972B101", isin="US99972B1017"),
            robinhood_security(
                symbol="XQAQ", cusip="99992B107", isin="US99992B1070", source="reviewed"
            ),
        )
        pair = (
            "[[robinhood.securities]] entry 2 and generated identity 1 "
            f"(from the {generated.source})"
        )
    else:
        inputs = (issuer_aq, ObservedIdentityInput("99972B101", "XQAQ", "Sample Issuer AQ Inc."))
        configured = ()
        # Generated entries are sorted by CUSIP, so 99972B101 comes first.
        pair = (
            f"generated identity 1 (from the {generated.source}) and generated identity 2 "
            f"(from the {generated.source})"
        )

    with pytest.raises(RobinhoodIdentityCollisionError) as raised:
        resolve_robinhood_security_table(inputs, sources, configured)

    assert str(raised.value) == f"{_COLLISION}\n  same symbol: {pair}"
    assert_no_markers(
        str(raised.value), "XQAQ", "99992A109", "99992B107", "99972B101", "US99992A1097"
    )


@pytest.mark.parametrize(
    ("duplicate", "pair"),
    [
        (
            robinhood_security(
                symbol="XQCA", cusip="99992A109", isin="US99972B1017", source=_SNAPSHOT
            ),
            "same CUSIP: [[robinhood.securities]] entry 1 and generated identity 1"
            f" (from the {_SNAPSHOT})",
        ),
        (
            robinhood_security(
                symbol="XQCA", cusip="99972B101", isin="US99992A1097", source=_SNAPSHOT
            ),
            "same ISIN: [[robinhood.securities]] entry 1 and generated identity 1"
            f" (from the {_SNAPSHOT})",
        ),
    ],
    ids=["cusip", "isin"],
)
def test_merge_rejects_duplicate_cusip_or_isin(
    duplicate: RobinhoodSecurityConfig, pair: str
) -> None:
    """Config loading refuses these, so only entries built directly can collide here."""
    configured = robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")
    with pytest.raises(RobinhoodIdentityCollisionError) as raised:
        RobinhoodSecurityIdentities(
            (configured,),
            (duplicate,),
        )
    assert str(raised.value) == f"{_COLLISION}\n  {pair}"


def test_merge_names_every_colliding_pair() -> None:
    configured = (
        robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097"),
        robinhood_security(symbol="XQCA", cusip="99972B101", isin="US99972B1017"),
    )
    generated = (
        robinhood_security(symbol="XQCA", cusip="99992B107", isin="US99992B1070", source=_SNAPSHOT),
        robinhood_security(symbol="XQAQ", cusip="99992H104", isin="US99992H1041", source=_SNAPSHOT),
    )

    with pytest.raises(RobinhoodIdentityCollisionError) as raised:
        RobinhoodSecurityIdentities(configured, generated)

    assert str(raised.value).splitlines()[1:] == [
        "  same symbol: [[robinhood.securities]] entry 1 and generated identity 2"
        f" (from the {_SNAPSHOT})",
        "  same symbol: [[robinhood.securities]] entry 2 and generated identity 1"
        f" (from the {_SNAPSHOT})",
    ]


def test_collision_names_each_security_by_reference_only() -> None:
    """Neither a symbol, a CUSIP nor an ISIN: a generated identity's come from
    a statement (#1044)."""
    isin = f"US{CUSIP}{isin_check_digit(f'US{CUSIP}')}"
    other_cusip = "99971M109"
    other_isin = f"US{other_cusip}{isin_check_digit(f'US{other_cusip}')}"
    configured = (robinhood_security(symbol=HOLDER, cusip=CUSIP, isin=isin),)
    generated = (
        robinhood_security(symbol=TICKER, cusip=CUSIP, isin=isin, source=_SNAPSHOT),
        robinhood_security(symbol=HOLDER, cusip=other_cusip, isin=other_isin, source=_SNAPSHOT),
    )

    with pytest.raises(RobinhoodIdentityCollisionError) as raised:
        RobinhoodSecurityIdentities(configured, generated)

    message = str(raised.value)
    assert message == (
        f"{_COLLISION}\n"
        "  same CUSIP: [[robinhood.securities]] entry 1 and generated identity 1"
        f" (from the {_SNAPSHOT})\n"
        "  same ISIN: [[robinhood.securities]] entry 1 and generated identity 1"
        f" (from the {_SNAPSHOT})\n"
        "  same symbol: [[robinhood.securities]] entry 1 and generated identity 2"
        f" (from the {_SNAPSHOT})"
    )
    assert_no_markers(message, HOLDER, TICKER, CUSIP, isin, other_cusip, other_isin)


def test_manual_fields_keep_the_identity_cache_fingerprint() -> None:
    securities = (
        robinhood_security(symbol="XQAQ", cusip="99992A109", isin="US99992A1097", source="manual"),
        robinhood_security(
            symbol="XQAX",
            cusip="99992F108",
            isin="US99992F1084",
            listing_exchange="NYSE",
            issuer_country_code="CN",
            is_depositary_receipt=True,
            source="manual",
        ),
    )
    manual = robinhood_identity_manual_fields(securities)
    assert manual == {
        "XQAQ": {
            "cusip": "99992A109",
            "isin": "US99992A1097",
            "listing_exchange": "NASDAQ",
            "sub_category": "COMMON",
            "asset_class": "STK",
            "issuer_country_code": "US",
            "is_depositary_receipt": "False",
        },
        "XQAX": {
            "cusip": "99992F108",
            "isin": "US99992F1084",
            "listing_exchange": "NYSE",
            "sub_category": "COMMON",
            "asset_class": "STK",
            "issuer_country_code": "CN",
            "is_depositary_receipt": "True",
        },
    }
    observed = (
        ObservedIdentityInput(cusip="99992A109", symbol="XQAQ", description="SAMPLE ISSUER AQ INC"),
        ObservedIdentityInput(cusip="99992F108", symbol="XQAX", description="SAMPLE ISSUER AX ADS"),
    )
    # SHA-256 of the canonical JSON of the fingerprint payload (domain, schema
    # version 2, the sorted upper-cased observed triples and these manual
    # fields), worked out apart from the loader. If it changes, every cached
    # identity snapshot stops matching.
    assert (
        identity_input_fingerprint(observed, manual)
        == "cae860d41ce6bc4f71e85cabcfa3e312a16fc1b60765f37b015fc6cacac2b7f9"
    )


# --- #1187: canonical statement symbols select the observed symbol ---------------

_ZQ_ISIN = Isin(f"US{_ZQ_NEW}{isin_check_digit(f'US{_ZQ_NEW}')}")


def test_accepted_transition_and_pending_abbreviation_resolve_one_identity() -> None:
    """Before #1187 automatic lookup keyed the statement's ``ZQXA`` row by its
    printed old CUSIP, saw activity symbols ``ZQXA`` and ``ZQX``, and found
    no symbol, so the official sources resolved nothing."""
    paired = _pair_with_zq_symbols(transition=True, pending=("ZQX", _ZQ_NEW))
    observed = observed_robinhood_identity_inputs(paired)

    assert observed == (ObservedIdentityInput(_ZQ_NEW, "ZQXA", _ZQ_ISSUER),)
    result = resolve_robinhood_standard_identities(observed, _zq_sources(), ())
    assert result.failures == ()
    (security,) = result.generated
    assert (security.instrument.symbol, security.cusip, security.instrument.isin) == (
        "ZQXA",
        _ZQ_NEW,
        _ZQ_ISIN,
    )


@pytest.mark.parametrize("transition", [False, True], ids=["printed-current", "transition"])
def test_conflicting_statement_symbols_resolve_no_identity(transition: bool) -> None:
    """The statement names ``ZQXA`` and ``ZQXAB``; the reports name ``ZQXA``.
    The sources list both, so choosing either would resolve an identity.
    Before #1187 the printed-current case fell back to the reports' ``ZQXA`` and
    the transition case took ``ZQXAB``, the one symbol its printed CUSIP kept."""
    paired = _pair_with_zq_symbols(transition=transition, conflict=True)
    observed = observed_robinhood_identity_inputs(paired)

    assert observed == (ObservedIdentityInput(_ZQ_NEW, "", _ZQ_ISSUER),)
    assert resolve_robinhood_standard_identities(
        observed, _zq_sources("ZQXA", "ZQXAB"), ()
    ) == RobinhoodIdentityResolution((), (RobinhoodIdentityFailure(_ZQ_NEW, _ZQ_AMBIGUOUS),))


@pytest.mark.parametrize(
    ("sources", "checks"),
    [
        pytest.param(
            _zq_sources(name="Sample Issuer ZR Inc."),
            ("nasdaq-name-mismatch", "sec-name-mismatch"),
            id="name-disagreement",
        ),
        pytest.param(_zq_sources(exchange="NYSE"), ("exchange-mismatch",), id="exchange-mismatch"),
        pytest.param(_zq_sources(state=""), ("us-jurisdiction-unproved",), id="jurisdiction"),
    ],
)
def test_transition_symbol_keeps_every_source_refusal(
    sources: IdentitySourceBundle, checks: tuple[str, ...]
) -> None:
    paired = _pair_with_zq_symbols(transition=True, pending=("ZQX", _ZQ_NEW))

    assert resolve_robinhood_standard_identities(
        observed_robinhood_identity_inputs(paired), sources, ()
    ) == RobinhoodIdentityResolution((), (RobinhoodIdentityFailure(_ZQ_NEW, checks),))


def test_a_cusip_an_accepted_transition_replaced_resolves_no_identity() -> None:
    """A pending ``ZQXC`` trade still uses the CUSIP the accepted ``ZQXA``
    transition replaced. That CUSIP gets no symbol, so it resolves no
    identity even though the sources list ``ZQXC``. Before #1187 automatic
    lookup observed both CUSIPs as ``ZQXA`` and the identity table refused
    them as colliding."""
    paired = _pair_with_zq_symbols(transition=True, pending=("ZQXC", _ZQ_OLD))
    observed = observed_robinhood_identity_inputs(paired)

    assert observed == (
        ObservedIdentityInput(_ZQ_NEW, "ZQXA", _ZQ_ISSUER),
        ObservedIdentityInput(_ZQ_OLD, "", _ZQ_ISSUER),
    )
    securities, resolution = resolve_robinhood_security_table(
        observed, _zq_sources("ZQXA", "ZQXC"), ()
    )
    assert [(item.instrument.symbol, item.cusip) for item in securities.securities] == [
        ("ZQXA", _ZQ_NEW)
    ]
    assert resolution.failures == (RobinhoodIdentityFailure(_ZQ_OLD, _ZQ_AMBIGUOUS),)
