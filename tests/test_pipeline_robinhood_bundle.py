"""Discovery and scope checks for the Robinhood bundle loader."""

from __future__ import annotations

import errno
import json
import os
import tomllib
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from shutil import copy2
from types import SimpleNamespace
from typing import cast

import pytest

import ibkr_fa.parser.robinhood_1042s as tax_form_parser
import ibkr_fa.parser.robinhood_statement.parser as statement_parser
import ibkr_fa.pipeline_robinhood as robinhood_pipeline
import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
import ibkr_fa.pipeline_robinhood_entities as entities
from ibkr_fa import cli
from ibkr_fa.compute.robinhood_security_identities import (
    RobinhoodIdentityCollisionError,
    RobinhoodSecurityIdentities,
)
from ibkr_fa.config import (
    FaEntityOverride,
    RobinhoodConfig,
    RobinhoodSecurityReceiptResolution,
    RobinhoodSecurityReceiptTreatment,
    UserConfig,
    load_user_config,
)
from ibkr_fa.exceptions import IbkrFaError, ParseError
from ibkr_fa.models.reference_identity import (
    SEC_SUBMISSION_URL_TEMPLATE,
    ObservedIdentityInput,
)
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodInventoryBlockerKind,
    RobinhoodSecurityIdentityResult,
    RobinhoodSecurityQuantityResult,
    resolve_robinhood_security_identities,
    validate_robinhood_corrected_statement,
)
from ibkr_fa.output._cleanup import OutputCollisionError
from ibkr_fa.parser.robinhood_statement import (
    RobinhoodStatement,
    parse_robinhood_statement,
    read_robinhood_statement,
)
from ibkr_fa.pipeline import FinancialYear
from ibkr_fa.pipeline_robinhood import (
    RobinhoodLegStatus,
    RobinhoodPipelineError,
    run_robinhood_options,
)
from ibkr_fa.pipeline_robinhood_bundle import (
    RobinhoodBundle,
    RobinhoodBundleError,
    identity_failure_guidance,
    load_robinhood_bundle,
    resolved_robinhood_quantities,
)
from ibkr_fa.pipeline_robinhood_models import RobinhoodLegReport
from ibkr_fa.robinhood_identity_sources import (
    NASDAQ_TRADED_URL,
    SEC_COMPANY_TICKERS_EXCHANGE_URL,
    IdentitySourceAudit,
    identity_input_fingerprint,
)
from ibkr_fa.types import Isin
from tests._crash_reports import assert_replaced_unchained
from tests._no_echo import assert_no_markers
from tests._private_checks import private_test
from tests._private_data import private_pdfs
from tests._robinhood_account_inputs import ZQ_ISSUER as _ZQ_ISSUER
from tests._robinhood_account_inputs import ZQ_NEW as _ZQ_NEW
from tests._robinhood_account_inputs import pair_with_security as _pair_with_security
from tests._robinhood_account_inputs import pair_with_zq_symbols as _pair_with_zq_symbols
from tests._robinhood_account_inputs import security_config as _reconciliation_security
from tests._robinhood_bundle_stubs import bundle_config_text as _config_text
from tests._robinhood_bundle_stubs import patch_bundle_sources as _patch_sources
from tests._robinhood_bundle_stubs import with_bundle_texts as _with_bundle_texts
from tests._robinhood_bundle_stubs import write_bundle as _write_bundle
from tests._robinhood_entity_stubs import stub_zero_match_sources
from tests._robinhood_fa_address import ADDRESS_SUPERSEDED_ONE as _ADDRESS_ONE
from tests._robinhood_identity_inputs import ISSUER_AQ_ISIN as _ISSUER_AQ
from tests._robinhood_identity_inputs import NAME_SUPERSEDED_ONE as _NAME_ONE
from tests._robinhood_identity_inputs import NATURE_SUPERSEDED_ONE as _NATURE_ONE
from tests._robinhood_identity_inputs import automatic_entity_override as _automatic
from tests._robinhood_identity_inputs import gleif_nature_evidence as _nature
from tests._robinhood_identity_inputs import issuer_identity_sources as _identity_sources
from tests._robinhood_identity_sources import IDENTITY_CACHE_INTEGRITY as _INTEGRITY
from tests._robinhood_identity_sources import load_snapshot as _load_snapshot
from tests._robinhood_identity_sources import nasdaq_bytes as _nasdaq_bytes
from tests._robinhood_identity_sources import snapshot_refusal as _snapshot_refusal
from tests._robinhood_identity_sources import tamper_snapshot as _tamper
from tests._robinhood_identity_sources import tickers_bytes as _tickers_bytes
from tests._robinhood_private import (
    load_private_accounts_reconciliation,
    private_robinhood_dataset,
)
from tests._robinhood_refusals import (
    CORRECTED_STATEMENT_MISMATCH,
    EVENT_REFERENCES,
    MIXED_FAMILIES_REFUSAL,
)
from tests._robinhood_statement_pages import brokerage_statement as _brokerage_statement
from tests._robinhood_statement_pages import crypto_statement as _crypto_statement
from tests._robinhood_toml import (
    robinhood_corrected_statement_toml,
    robinhood_option_classification_toml,
    robinhood_report_toml,
    robinhood_security_toml,
)

_FY = FinancialYear(2025)


def test_transition_sidecar_path_resolves_beside_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(
        _config_text(
            parent_fields=(
                'cusip_transitions_file = "overrides/robinhood/cusip-transitions.toml"\n'
            )
        ),
        encoding="utf-8",
    )
    _patch_sources(monkeypatch)
    observed: list[tuple[Path, str | None, frozenset[str]]] = []
    monkeypatch.setattr(
        bundle_mod,
        "load_configured_cusip_transitions",
        lambda config_path, configured_path, *, account_numbers: (
            observed.append((config_path, configured_path, account_numbers)) or ()
        ),
    )

    load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )

    assert observed == [
        (
            config,
            "overrides/robinhood/cusip-transitions.toml",
            frozenset({"100000001", "200000002"}),
        )
    ]


def test_scope_warns_when_manual_cusip_transition_was_used() -> None:
    scope = bundle_mod.RobinhoodScopeStatus(
        crypto_statement_count=0,
        cusip_transition_references=("1:8",),
    )
    assert scope.warnings == (
        "1 statement CUSIP contradiction was accepted by configured "
        "account/ticker/date transitions; activity CUSIP remained authoritative "
        f"and the statement corroborated symbol only. {EVENT_REFERENCES}1:8.",
    )


def test_presentation_cache_failure_replaces_synthetic_source_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    artifact = tmp_path / "bad-sources.json"
    audit = IdentitySourceAudit("f" * 64, False, "cache integrity failed", artifact)
    _patch_sources(monkeypatch, presentation_source_audit=audit)

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )

    assert result.presentation_identity_source_audit == audit
    assert any("cache integrity failed" in item for item in result.entity_address_warnings)


def test_robinhood_cg_stops_on_a_corrupt_post_fy_identity_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    issuer_aq = robinhood_security_toml(symbol="XQAQ", cusip="99992A109", isin="US99992A1097")
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(securities=issuer_aq), encoding="utf-8")
    _patch_sources(monkeypatch)
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_presentation_evidence",
        entities.resolve_robinhood_presentation_evidence,
    )
    stub_zero_match_sources(monkeypatch, "99992A109", Isin("US99992A1097"))
    cache = tmp_path / "cache"
    _sources, audit, _opener = _load_snapshot(cache)
    assert audit.artifact_path is not None
    _tamper(audit.artifact_path)
    monkeypatch.setattr(
        robinhood_pipeline,
        "_compute_options",
        lambda *_args: pytest.fail("the run must stop at the snapshot"),
    )
    monkeypatch.chdir(tmp_path)

    # #853: on b41cf75 robinhood-cg went on without showing the corruption.
    exit_code = cli.main(
        [
            "robinhood-cg",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
            "--no-fetch-prices",
            "--price-cache-dir",
            "cache",
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 4
    # #1071: the relative cache folder prints as typed. #1526: the config has no
    # SEC contact.
    refusal = _snapshot_refusal(Path("cache"), _INTEGRITY, fetch=False, set_contact=True)
    assert printed.err == f"error: {refusal}\n"


_ZQ_CIK = 9991187


def _zq_source_responses() -> dict[str, bytes]:
    """Official Nasdaq and SEC files that list ``ZQXA`` for one US issuer."""
    return {
        NASDAQ_TRADED_URL: _nasdaq_bytes(
            ("Y|ZQXA|Sample Issuer ZQ Inc. - Common Stock|Q|Q|N|100|N|N|ZQXA|ZQXA|N",)
        ),
        SEC_COMPANY_TICKERS_EXCHANGE_URL: _tickers_bytes(
            ([_ZQ_CIK, "Sample Issuer ZQ Inc.", "ZQXA", "Nasdaq"],)
        ),
        SEC_SUBMISSION_URL_TEMPLATE.format(cik=_ZQ_CIK): json.dumps(
            {
                "cik": _ZQ_CIK,
                "name": "Sample Issuer ZQ Inc.",
                "stateOfIncorporation": "CA",
                "filings": {"recent": {"form": ["10-K"]}},
            }
        ).encode(),
    }


@pytest.mark.parametrize("fetch", [False, True], ids=["offline", "fetch-enabled"])
def test_loader_reuses_the_snapshot_of_the_canonical_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fetch: bool,
) -> None:
    """#1187: the loader fingerprints the canonical observation, ``ZQXA``, so
    the immutable snapshot fetched for it is reused without a request, left
    unchanged, and resolves one identity. Before #1187 the observation had no
    symbol, so its fingerprint selected no snapshot."""
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    paired = _pair_with_zq_symbols(transition=True, pending=("ZQX", _ZQ_NEW))
    monkeypatch.setattr(
        bundle_mod, "reconcile_robinhood_accounts", lambda *_args, **_kwargs: paired
    )
    corrected = ObservedIdentityInput(_ZQ_NEW, "ZQXA", _ZQ_ISSUER)
    cache = tmp_path / "cache"
    _unused, seeded, _opener = _load_snapshot(
        cache, responses=_zq_source_responses(), inputs=(corrected,), manual={}
    )
    assert seeded.artifact_path is not None
    artifact = seeded.artifact_path.read_bytes()
    monkeypatch.setattr(
        "ibkr_fa.robinhood_identity_sources._url_open",
        lambda *_args: pytest.fail("a cached snapshot needs no request"),
    )

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
        fetch_identities=fetch,
        identity_cache_dir=cache,
    )

    assert result.identity_source_audit == IdentitySourceAudit(
        identity_input_fingerprint((corrected,), {}), True, None, seeded.artifact_path
    )
    assert result.identity_resolution is not None
    assert result.identity_resolution.failures == ()
    assert [(item.instrument.symbol, item.cusip) for item in result.securities.generated] == [
        ("ZQXA", _ZQ_NEW)
    ]
    assert seeded.artifact_path.read_bytes() == artifact


def test_identity_generation_failure_survives_successful_presentation_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    presentation_audit = IdentitySourceAudit("a" * 64, True, None, tmp_path / "post-fy.json")
    _patch_sources(monkeypatch, presentation_source_audit=presentation_audit)
    monkeypatch.setattr(
        bundle_mod,
        "observed_robinhood_identity_inputs",
        lambda _paired: (ObservedIdentityInput("999999999", "MISS", "Missing Inc."),),
    )
    source_audit = IdentitySourceAudit(
        "b" * 64,
        False,
        "identity generation cache unavailable",
        None,
    )
    monkeypatch.setattr(
        bundle_mod,
        "load_identity_sources",
        lambda *_args, **_kwargs: (None, source_audit),
    )

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )

    assert result.identity_source_audit == source_audit
    assert result.presentation_identity_source_audit == presentation_audit


def _generate_identities(
    monkeypatch: pytest.MonkeyPatch,
    *observed: ObservedIdentityInput,
) -> list[tuple[object, ...]]:
    """Generate XQAQ from official sources and capture the identities the loader resolves."""

    issuer_aq, sources = _identity_sources()
    monkeypatch.setattr(
        bundle_mod,
        "observed_robinhood_identity_inputs",
        lambda _paired: (*observed, issuer_aq),
    )
    monkeypatch.setattr(
        bundle_mod,
        "load_identity_sources",
        lambda *_args, **_kwargs: (sources, IdentitySourceAudit("c" * 64, True, None, None)),
    )
    resolved: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_identities",
        lambda _paired, securities: resolved.append(securities) or object(),
    )
    return resolved


def test_generated_identities_stay_out_of_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(securities=robinhood_security_toml()), encoding="utf-8")
    _patch_sources(monkeypatch)
    resolved = _generate_identities(
        monkeypatch, ObservedIdentityInput("99972B101", "XQCA", "SAMPLE ISSUER CA Corp.")
    )

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )

    assert result.config == load_user_config(config)
    assert result.robinhood is result.config.robinhood
    assert [item.cusip for item in result.robinhood.securities] == ["99972B101"]
    assert [item.cusip for item in result.securities.generated] == ["99992A109"]
    assert result.securities.securities == (
        *result.robinhood.securities,
        *result.securities.generated,
    )
    assert resolved == [result.securities.securities]


def test_generated_symbol_collision_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(
        _config_text(securities=robinhood_security_toml(symbol="XQAQ")),
        encoding="utf-8",
    )
    _patch_sources(monkeypatch)
    resolved = _generate_identities(
        monkeypatch, ObservedIdentityInput("999999999", "MISS", "Missing Inc.")
    )

    with pytest.raises(RobinhoodIdentityCollisionError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
        )

    message = str(raised.value)
    assert (
        "\n  same symbol: [[robinhood.securities]] entry 1 and generated identity 1 "
        "(from the automatic official identity snapshot "
    ) in message
    assert_no_markers(message, "XQAQ", "99972B101", "99992A109", "US99972B1017")
    assert resolved == []


def test_failure_guidance_omits_generated_identities() -> None:
    paired, _accounts = _pair_with_security()

    def guidance(securities: RobinhoodSecurityIdentities) -> str:
        bundle = SimpleNamespace(paired=paired, securities=securities, identity_resolution=None)
        return identity_failure_guidance(cast("RobinhoodBundle", bundle))

    assert "[[robinhood.securities]]" in guidance(RobinhoodSecurityIdentities(()))
    assert guidance(RobinhoodSecurityIdentities((), (_reconciliation_security(),))) == ""


def test_receipt_resolution_may_name_a_generated_identity() -> None:
    # Synthetic reward: 0.052876 displays as 0.0529; 0.38245 + 0.052876 = 0.435326.
    paired, accounts = _pair_with_security(
        include_receipt=True,
        position_quantity_override=Decimal("0.435326"),
        managed_quantity=Decimal("0.38245"),
        managed_cost_usd=Decimal("45.89"),
        receipt_quantity=Decimal("0.0529"),
    )
    resolution = RobinhoodSecurityReceiptResolution(
        account_number=paired.accounts[1].activity.primary_bundle.account_number,
        source_record_index=4,
        cusip="99971T103",
        treatment=RobinhoodSecurityReceiptTreatment.PROMOTIONAL_REWARD,
        exact_quantity=Decimal("0.052876"),
        reward_fair_market_value_usd=Decimal("6.35"),
        income_reported_financial_year=2024,
        income_reported=True,
        source="reward confirmation and filed return",
        reason="promotional reward",
    )
    securities = RobinhoodSecurityIdentities((), (_reconciliation_security(),))
    bundle = SimpleNamespace(
        paired=paired,
        robinhood=RobinhoodConfig(accounts=accounts, security_receipt_resolutions=(resolution,)),
        securities=securities,
        identities=resolve_robinhood_security_identities(paired, securities.securities),
    )

    result = resolved_robinhood_quantities(cast("RobinhoodBundle", bundle))

    (receipt,) = (
        item
        for item in result.acquisitions
        if item.event_reference.account_index == 1 and item.event_reference.event_ordinal == 4
    )
    assert receipt.quantity == Decimal("0.052876")
    assert result.identity_blockers == ()
    assert not any(
        blocker.kind is RobinhoodInventoryBlockerKind.SECURITY_RECEIPT
        for blocker in result.blockers
    )


def test_loader_resolves_identities_once_and_keeps_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    resolved: list[object] = []

    def resolve(*_args: object) -> object:
        resolved.append(object())
        return resolved[-1]

    reconciled: list[object] = []
    monkeypatch.setattr(bundle_mod, "resolve_robinhood_security_identities", resolve)
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_security_quantities",
        lambda _paired, identities: reconciled.append(identities) or object(),
    )
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_quantities",
        lambda *_args: pytest.fail("the loader applied receipt resolutions"),
    )

    result = load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert len(resolved) == 1
    assert reconciled == resolved
    assert result.identities is resolved[0]


def test_resolved_quantities_reuse_the_loaders_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities = object()
    receipts = (object(),)
    quantities = object()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_identities",
        lambda *_args: pytest.fail("identities were resolved again"),
    )
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_quantities",
        lambda *args: calls.append(args) or quantities,
    )
    bundle = SimpleNamespace(
        paired=object(),
        securities=RobinhoodSecurityIdentities(()),
        identities=identities,
        robinhood=SimpleNamespace(security_receipt_resolutions=receipts),
    )

    result = resolved_robinhood_quantities(cast("RobinhoodBundle", bundle))

    assert result is quantities
    ((paired, passed_identities, passed_receipts),) = calls
    assert paired is bundle.paired
    assert passed_identities is identities
    assert passed_receipts is receipts


def test_unconfigured_activity_csv_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle", extra_csv=True)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    with pytest.raises(RobinhoodBundleError, match="exactly the two"):
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
        )


def test_crypto_statement_is_audited_but_out_of_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle", crypto=True)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )

    assert result.scope.crypto_statement_count == 1
    assert result.scope.warnings == (
        "1 Robinhood Crypto statement was detected and excluded as out of scope",
    )


def test_brokerage_statement_that_mentions_robinhood_crypto_is_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _brokerage_statement().replace("Unlisted Continuation", "Transfer to Robinhood Crypto")
    input_dir, config = _with_bundle_texts(monkeypatch, tmp_path, {b"brokerage": text})
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read_robinhood_statement)
    parsed: list[RobinhoodStatement] = []
    monkeypatch.setattr(
        bundle_mod,
        "validate_robinhood_statement_bundles",
        lambda statements: parsed.extend(statements) or (),
    )

    result = load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert result.scope.crypto_statement_count == 0
    assert [statement.activities[1].description for statement in parsed] == [
        "Internal Security Transfer from 111111111 to 222222222\nTransfer to Robinhood Crypto"
    ]


def test_unknown_pdf_that_mentions_robinhood_crypto_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, config = _with_bundle_texts(
        monkeypatch,
        tmp_path,
        {
            b"brokerage": _brokerage_statement(),
            b"other": "Unrelated document\nThis data does not include Robinhood Crypto\n",
        },
    )
    (input_dir / "statements" / "other.pdf").write_bytes(b"other")
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read_robinhood_statement)

    with pytest.raises(ParseError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert raised.value.message == "statement page marker is missing"


def test_pdf_mixing_crypto_and_brokerage_families_refuses_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    texts = {b"brokerage": _brokerage_statement()}
    input_dir, config = _with_bundle_texts(monkeypatch, tmp_path, texts)
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read_robinhood_statement)
    load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)
    texts[b"mixed"] = f"{_crypto_statement()}\f{_brokerage_statement()}"
    (input_dir / "statements" / "SECRET-mixed.pdf").write_bytes(b"mixed")

    with pytest.raises(ParseError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)
    exit_code = cli.main(
        [
            "robinhood-cg",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    assert raised.value.message == MIXED_FAMILIES_REFUSAL
    assert exit_code == 4
    printed = capsys.readouterr()
    rendered = printed.out + printed.err
    assert str(raised.value) in rendered
    for private in ("SECRET", str(tmp_path), "111111111", "333333333", "TAX PAYER"):
        assert private not in rendered


def test_corrected_selection_excludes_superseded_statement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    original = input_dir / "statements" / "original.pdf"
    corrected = input_dir / "statements" / "corrected.pdf"
    original.write_bytes(b"original")
    corrected.write_bytes(b"corrected")
    config = tmp_path / "user.toml"
    config.write_text(
        _config_text(
            corrected=robinhood_corrected_statement_toml(
                selected_file="statements/corrected.pdf",
                supersedes='["statements/original.pdf"]',
            )
        ),
        encoding="utf-8",
    )
    _patch_sources(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        bundle_mod,
        "read_robinhood_statement",
        lambda path: seen.append(path.name) or path,
    )
    load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
    )
    assert "corrected.pdf" in seen
    assert "original.pdf" not in seen


def test_corrected_selection_must_match_account_and_period() -> None:
    selected = parse_robinhood_statement(_brokerage_statement())
    old = replace(selected, period_end=date(2025, 2, 28))
    with pytest.raises(RobinhoodBundleError, match="does not match"):
        validate_robinhood_corrected_statement(selected, old)


def test_corrected_statement_mismatch_refuses_before_the_next_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = parse_robinhood_statement(_brokerage_statement())
    statements = {
        "selected.pdf": selected,
        "first.pdf": replace(selected, account_number="222222222"),
    }
    seen: list[str] = []

    def read(path: Path) -> object:
        seen.append(path.name)
        if path.name == "second.pdf":
            raise AssertionError("the second superseded statement was read")
        return statements[path.name]

    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", read)
    config = SimpleNamespace(
        corrected_statements=(
            SimpleNamespace(selected_file="selected.pdf", supersedes=("first.pdf", "second.pdf")),
        )
    )

    with pytest.raises(RobinhoodBundleError) as raised:
        bundle_mod._validate_corrected_statements(
            Path("/robinhood-input"), cast("RobinhoodConfig", config)
        )

    assert str(raised.value) == CORRECTED_STATEMENT_MISMATCH
    assert seen == ["selected.pdf", "first.pdf"]


def _record_statement_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every statement read and extraction; every extraction fails."""

    calls: list[str] = []
    read = bundle_mod.read_robinhood_statement

    def record_read(path: Path) -> object:
        calls.append(f"read {path.name}")
        return read(path)

    def fail_extraction(path: Path, **kwargs: object) -> str:
        calls.append(f"pdftotext {path.name}")
        raise ParseError(
            section=str(kwargs["source"]),
            row_index=0,
            column=None,
            message="pdftotext failed",
            raw_value="",
        )

    monkeypatch.setattr(bundle_mod, "read_robinhood_activity", lambda _path: ())
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", record_read)
    monkeypatch.setattr(statement_parser, "pdftotext_layout", fail_extraction)
    return calls


def _pdf(path: Path) -> None:
    path.write_bytes(path.name.encode())


def _directory(path: Path) -> None:
    path.mkdir()


def _dangling_symlink(path: Path) -> None:
    path.symlink_to(path.with_name("gone.pdf"))


def _corrected_bundle(
    tmp_path: Path,
    files: dict[str, Callable[[Path], None]],
    *selections: tuple[str, str],
) -> tuple[Path, Path]:
    input_dir = _write_bundle(tmp_path / "bundle")
    for relative, place in files.items():
        path = input_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        place(path)
    config = tmp_path / "user.toml"
    config.write_text(
        _config_text(
            corrected="".join(
                robinhood_corrected_statement_toml(
                    selected_file=selected, supersedes=f'["{superseded}"]'
                )
                for selected, superseded in selections
            )
        ),
        encoding="utf-8",
    )
    return input_dir, config


_SELECTION = ("statements/corrected.pdf", "statements/original.pdf")


@pytest.mark.parametrize(
    ("selection", "files"),
    [
        pytest.param(_SELECTION, {"statements/original.pdf": _pdf}, id="selected-missing"),
        pytest.param(_SELECTION, {"statements/corrected.pdf": _pdf}, id="superseded-missing"),
        pytest.param(
            _SELECTION,
            {"statements/corrected.pdf": _dangling_symlink, "statements/original.pdf": _pdf},
            id="selected-dangling-symlink",
        ),
        pytest.param(
            _SELECTION,
            {"statements/corrected.pdf": _directory, "statements/original.pdf": _pdf},
            id="selected-directory",
        ),
        pytest.param(
            _SELECTION,
            {"statements/corrected.pdf": _pdf, "statements/original.pdf": _directory},
            id="superseded-directory",
        ),
        pytest.param(
            ("elsewhere/corrected.pdf", "statements/original.pdf"),
            {"elsewhere/corrected.pdf": _pdf, "statements/original.pdf": _pdf},
            id="selected-outside-statements",
        ),
    ],
)
def test_corrected_selection_naming_a_missing_pdf_refuses_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: tuple[str, str],
    files: dict[str, Callable[[Path], None]],
) -> None:
    input_dir, config = _corrected_bundle(tmp_path, files, selection)
    calls = _record_statement_reads(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == "corrected-statement selection names a missing PDF"
    assert calls == []


def test_every_corrected_selection_is_checked_before_any_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, config = _corrected_bundle(
        tmp_path,
        {
            "statements/first.pdf": _pdf,
            "statements/first-original.pdf": _pdf,
            "statements/second-original.pdf": _pdf,
        },
        ("statements/first.pdf", "statements/first-original.pdf"),
        ("statements/second.pdf", "statements/second-original.pdf"),
    )
    calls = _record_statement_reads(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == "corrected-statement selection names a missing PDF"
    assert calls == []


def test_corrected_statement_mismatch_refuses_before_other_statements_are_extracted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, config = _corrected_bundle(
        tmp_path,
        {
            "statements/corrected.pdf": _pdf,
            "statements/original.pdf": _pdf,
            "statements/unrelated.pdf": _pdf,
        },
        _SELECTION,
    )
    calls = _record_statement_reads(monkeypatch)
    selected = parse_robinhood_statement(_brokerage_statement())
    statements = {
        "corrected.pdf": selected,
        "original.pdf": replace(selected, account_number="222222222"),
    }
    monkeypatch.setattr(
        bundle_mod,
        "read_robinhood_statement",
        lambda path: calls.append(f"read {path.name}") or statements[path.name],
    )

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == CORRECTED_STATEMENT_MISMATCH
    assert calls == ["read corrected.pdf", "read original.pdf"]


def _record_extractions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record statement reads and statement and tax-form extractions; every extraction fails."""

    calls = _record_statement_reads(monkeypatch)

    def fail_extraction(path: Path, **kwargs: object) -> str:
        calls.append(f"tax-form pdftotext {path.name}")
        raise ParseError(
            section=str(kwargs["source"]),
            row_index=0,
            column=None,
            message="pdftotext failed",
            raw_value="",
        )

    monkeypatch.setattr(tax_form_parser, "pdftotext_layout", fail_extraction)
    return calls


def _unreadable(path: Path) -> None:
    _pdf(path)
    path.chmod(0)


def _symlink_loop(path: Path) -> None:
    other = path.with_name("b.pdf")
    path.symlink_to(other.name)
    other.symlink_to(path.name)


_UNREADABLE_ENTRIES = [
    pytest.param("extra.pdf", _dangling_symlink, id="dangling-symlink"),
    pytest.param("extra.pdf", _directory, id="directory"),
    pytest.param(
        "extra.pdf",
        _unreadable,
        id="unreadable",
        marks=pytest.mark.skipif(
            os.name != "posix" or os.geteuid() == 0,
            reason="the platform does not enforce file permissions",
        ),
    ),
    pytest.param("a.pdf", _symlink_loop, id="symlink-loop"),
]


def _bundle_with_entry(tmp_path: Path, relative: str, place: Callable[[Path], None]) -> Path:
    input_dir = _write_bundle(tmp_path / "bundle")
    path = input_dir / relative
    path.parent.mkdir(exist_ok=True)
    place(path)
    return input_dir


@pytest.mark.parametrize("folder", ["statements", "tax-forms"])
@pytest.mark.parametrize(("name", "place"), _UNREADABLE_ENTRIES)
def test_unreadable_pdf_entry_refuses_naming_its_relative_path_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
    name: str,
    place: Callable[[Path], None],
) -> None:
    input_dir = _bundle_with_entry(tmp_path, f"{folder}/{name}", place)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
            include_tax_forms=True,
        )

    assert str(raised.value) == f"{folder}/{name} is not a readable PDF file"
    assert calls == []


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)
@pytest.mark.parametrize("folder", ["statements", "tax-forms"])
def test_inaccessible_pdf_target_refuses_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    locked = input_dir / "locked"
    locked.mkdir()
    target = locked / "target.pdf"
    _pdf(target)
    entry = input_dir / folder / "extra.pdf"
    entry.parent.mkdir(exist_ok=True)
    entry.symlink_to(target)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    locked.chmod(0)
    try:
        with pytest.raises(RobinhoodBundleError) as raised:
            load_robinhood_bundle(
                input_dir=input_dir,
                config_path=config,
                financial_year=_FY,
                include_tax_forms=True,
            )
    finally:
        locked.chmod(0o700)

    assert str(raised.value) == f"{folder}/extra.pdf is not a readable PDF file"
    assert calls == []


@pytest.mark.parametrize(("name", "place"), _UNREADABLE_ENTRIES)
def test_tax_form_entries_are_not_checked_without_tax_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    place: Callable[[Path], None],
) -> None:
    input_dir = _bundle_with_entry(tmp_path, f"tax-forms/{name}", place)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        tax_form_parser,
        "pdftotext_layout",
        lambda path, **_kwargs: calls.append(f"tax-form pdftotext {path.name}"),
    )

    result = load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert result.packets == ()
    assert calls == []


def test_unreadable_unselected_statement_refuses_before_the_corrected_pair_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, config = _corrected_bundle(
        tmp_path,
        {
            "statements/corrected.pdf": _pdf,
            "statements/original.pdf": _pdf,
            "statements/extra.pdf": _dangling_symlink,
        },
        _SELECTION,
    )
    calls = _record_statement_reads(monkeypatch)

    def extract(path: Path, **_kwargs: object) -> str:
        calls.append(f"pdftotext {path.name}")
        return _brokerage_statement()

    monkeypatch.setattr(statement_parser, "pdftotext_layout", extract)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == "statements/extra.pdf is not a readable PDF file"
    assert calls == []


def test_statement_symlink_loop_keeps_the_input_collision_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _bundle_with_entry(tmp_path, "statements/a.pdf", _symlink_loop)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir(mode=0o700)
    (output_dir / "robinhood-options.csv").write_text("previous run", encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(OutputCollisionError) as raised:
        run_robinhood_options(
            input_dir=input_dir,
            config_path=config,
            output_dir=output_dir,
            financial_year=_FY,
            itr_form="ITR-2",
        )

    assert str(raised.value).startswith("could not check output target ")
    assert calls == []


def test_robinhood_fa_refuses_an_unreadable_statement_with_its_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir = _bundle_with_entry(tmp_path, "statements/extra.pdf", _dangling_symlink)
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    exit_code = cli.main(
        [
            "robinhood-fa",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 4
    assert printed.err == "error: statements/extra.pdf is not a readable PDF file\n"
    assert str(tmp_path) not in printed.out + printed.err
    assert calls == []


def _self_loop(path: Path) -> None:
    """Replace ``path`` with a link to itself."""

    path.unlink(missing_ok=True)
    path.symlink_to(path.name)


def _csv(path: Path) -> None:
    path.write_text("placeholder", encoding="utf-8")


_MISSING_REPORT = "each Robinhood report must be an existing file directly under activity/"
_UNCONFIGURED_REPORT = "activity/ must contain exactly the two configured report CSVs"
_MISSING_SELECTION = "corrected-statement selection names a missing PDF"


@pytest.mark.parametrize(
    ("files", "selections", "message"),
    [
        pytest.param({"activity/one.csv": _self_loop}, (), _MISSING_REPORT, id="configured-report"),
        pytest.param(
            {"activity/extra.csv": _self_loop}, (), _UNCONFIGURED_REPORT, id="unconfigured-report"
        ),
        pytest.param(
            {"other/corrected.pdf": _self_loop, "statements/original.pdf": _pdf},
            (("other/corrected.pdf", "statements/original.pdf"),),
            _MISSING_SELECTION,
            id="selected-outside-statements",
        ),
        pytest.param(
            {"statements/corrected.pdf": _pdf, "other/original.pdf": _self_loop},
            (("statements/corrected.pdf", "other/original.pdf"),),
            _MISSING_SELECTION,
            id="superseded-outside-statements",
        ),
        pytest.param(
            {"statements/corrected.PDF": _self_loop, "statements/original.pdf": _pdf},
            (("statements/corrected.PDF", "statements/original.pdf"),),
            "statements/corrected.PDF is a broken or looping link",
            id="selected-not-named-pdf",
        ),
    ],
)
def test_looping_report_or_corrected_statement_refuses_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, Callable[[Path], None]],
    selections: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    input_dir, config = _corrected_bundle(tmp_path, files, *selections)
    calls = _record_statement_reads(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == message
    assert calls == []


@pytest.mark.parametrize(
    ("files", "selections", "failing", "message"),
    [
        pytest.param({}, (), "activity/one.csv", _MISSING_REPORT, id="configured-report"),
        pytest.param(
            {"activity/extra.csv": _csv},
            (),
            "activity/extra.csv",
            _UNCONFIGURED_REPORT,
            id="unconfigured-report",
        ),
        pytest.param(
            {"other/corrected.pdf": _pdf, "statements/original.pdf": _pdf},
            (("other/corrected.pdf", "statements/original.pdf"),),
            "other/corrected.pdf",
            _MISSING_SELECTION,
            id="selected-outside-statements",
        ),
    ],
)
def test_report_or_corrected_statement_that_cannot_be_resolved_refuses_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, Callable[[Path], None]],
    selections: tuple[tuple[str, str], ...],
    failing: str,
    message: str,
) -> None:
    input_dir, config = _corrected_bundle(tmp_path, files, *selections)
    calls = _record_statement_reads(monkeypatch)
    resolve = Path.resolve

    def fail_one(path: Path, strict: bool = False) -> Path:
        if path == input_dir / failing:
            raise OSError("injected resolution failure")
        return resolve(path, strict)

    monkeypatch.setattr(Path, "resolve", fail_one)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert str(raised.value) == message
    assert calls == []


def test_robinhood_cg_refuses_a_looping_report_with_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir, config = _corrected_bundle(tmp_path, {"activity/one.csv": _self_loop})
    calls = _record_statement_reads(monkeypatch)

    exit_code = cli.main(
        [
            "robinhood-cg",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 4
    assert printed.err == f"error: {_MISSING_REPORT}\n"
    assert str(tmp_path) not in printed.out + printed.err
    assert calls == []


def test_looping_report_keeps_the_input_collision_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir, config = _corrected_bundle(tmp_path, {"activity/one.csv": _self_loop})
    output_dir = tmp_path / "out"
    output_dir.mkdir(mode=0o700)
    (output_dir / "robinhood-options.csv").write_text("previous run", encoding="utf-8")
    calls = _record_statement_reads(monkeypatch)

    with pytest.raises(OutputCollisionError) as raised:
        run_robinhood_options(
            input_dir=input_dir,
            config_path=config,
            output_dir=output_dir,
            financial_year=_FY,
            itr_form="ITR-2",
        )

    assert str(raised.value).startswith("could not check output target ")
    assert calls == []


_PERMISSIONS_ENFORCED = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="the platform does not enforce file permissions",
)

# A link's target is a path the user never typed and no listing found (#1248).
_LINK_TARGET = "SYNTHLINKTARGET"


def _relink(path: Path, target: Path) -> None:
    path.unlink(missing_ok=True)
    path.symlink_to(target)


def _escaping_link(path: Path) -> None:
    """Link ``path``, one folder below the input, to a file outside the input."""

    target = path.parents[2] / "outside" / _LINK_TARGET / path.name
    target.parent.mkdir(parents=True)
    _pdf(target)
    _relink(path, target)


def _looping_link(path: Path) -> None:
    """Link ``path``, one folder below the input, into a two-link loop in the input."""

    folder = path.parents[1] / _LINK_TARGET
    folder.mkdir()
    (folder / "b").symlink_to("c")
    (folder / "c").symlink_to("b")
    _relink(path, folder / "b")


def _unreadable_link(path: Path) -> None:
    """Link ``path``, one folder below the input, to an unreadable PDF in the input."""

    target = path.parents[1] / "kept" / f"{_LINK_TARGET}.pdf"
    target.parent.mkdir()
    _pdf(target)
    target.chmod(0)
    _relink(path, target)


_ESCAPING = "symlink escapes the input directory"
_TYPED_ESCAPING = "escapes the configured input directory"
_CORRECTED = ("other/corrected.pdf", "statements/original.pdf")

# (files, corrected-statement selections, refusal). On Python 3.13 and later
# ``resolve()`` doesn't raise for a loop, so a looping link inside the input is
# refused by the later check with the same message, chaining nothing on main too.
_MARKED_LINKS = [
    pytest.param(
        {"activity/one.csv": _escaping_link},
        (),
        f"activity report {_TYPED_ESCAPING}",
        id="escaping-report",
    ),
    pytest.param(
        {"other/corrected.pdf": _escaping_link, "statements/original.pdf": _pdf},
        (_CORRECTED,),
        f"selected statement {_TYPED_ESCAPING}",
        id="escaping-selected-statement",
    ),
    pytest.param(
        {"statements/linked.pdf": _escaping_link},
        (),
        f"statement PDF {_ESCAPING}",
        id="escaping-statement",
    ),
    pytest.param(
        {"tax-forms/linked.pdf": _escaping_link},
        (),
        f"tax-form PDF {_ESCAPING}",
        id="escaping-tax-form",
    ),
    pytest.param({"activity/one.csv": _looping_link}, (), _MISSING_REPORT, id="looping-report"),
    pytest.param(
        {"activity/extra.csv": _looping_link},
        (),
        _UNCONFIGURED_REPORT,
        id="looping-unconfigured-report",
    ),
    pytest.param(
        {"other/corrected.pdf": _looping_link, "statements/original.pdf": _pdf},
        (_CORRECTED,),
        _MISSING_SELECTION,
        id="looping-selected-statement",
    ),
    pytest.param(
        {"statements/linked.pdf": _looping_link},
        (),
        "statements/linked.pdf is not a readable PDF file",
        id="looping-statement",
    ),
    pytest.param(
        {"tax-forms/linked.pdf": _looping_link},
        (),
        "tax-forms/linked.pdf is not a readable PDF file",
        id="looping-tax-form",
    ),
    pytest.param(
        {"statements/linked.pdf": _unreadable_link},
        (),
        "statements/linked.pdf is not a readable PDF file",
        id="unreadable-statement",
        marks=_PERMISSIONS_ENFORCED,
    ),
    pytest.param(
        {"tax-forms/linked.pdf": _unreadable_link},
        (),
        "tax-forms/linked.pdf is not a readable PDF file",
        id="unreadable-tax-form",
        marks=_PERMISSIONS_ENFORCED,
    ),
]


@pytest.mark.parametrize(("files", "selections", "message"), _MARKED_LINKS)
def test_link_refusal_chains_no_error_naming_the_link_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, Callable[[Path], None]],
    selections: tuple[tuple[str, str], ...],
    message: str,
) -> None:
    """The refusal keeps its message and chains nothing, including any error
    naming the link's target or its resolved path (#1248)."""

    input_dir, config = _corrected_bundle(tmp_path, files, *selections)
    calls = _record_extractions(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
            include_tax_forms=True,
        )

    assert str(raised.value) == message
    assert_replaced_unchained(raised.value, markers=(_LINK_TARGET,))
    assert calls == []


@pytest.mark.parametrize("traceback", [True, False], ids=["traceback", "no-traceback"])
@pytest.mark.parametrize(("files", "selections", "message"), _MARKED_LINKS)
def test_robinhood_income_prints_only_the_link_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    files: dict[str, Callable[[Path], None]],
    selections: tuple[tuple[str, str], ...],
    message: str,
    traceback: bool,
) -> None:
    """With nothing chained, ``IBKR_FA_TRACEBACK=1`` has no crash to re-raise,
    so the run prints the refusal alone either way (#1248)."""

    input_dir, config = _corrected_bundle(tmp_path, files, *selections)
    calls = _record_extractions(monkeypatch)
    if traceback:
        monkeypatch.setenv("IBKR_FA_TRACEBACK", "1")

    exit_code = cli.main(
        [
            "robinhood-income",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 4
    assert printed.err == f"error: {message}\n"
    assert_no_markers(printed.out, _LINK_TARGET)
    assert calls == []


_PROBLEMS = {
    "unlistable": "is not a readable folder",
    "linked-folder": "is a linked folder; replace it with a real folder",
    "broken-link": "is a broken or looping link",
    "looping-link": "is a broken or looping link",
}


def _hide_pdfs(path: Path, kind: str, request: pytest.FixtureRequest) -> None:
    """Place an entry that discovery used to skip, with the PDF it hides."""

    if kind == "unlistable":
        path.mkdir(exist_ok=True)
        _pdf(path / "hidden.pdf")
        path.chmod(0)
        request.addfinalizer(lambda: path.chmod(0o700))
    elif kind == "linked-folder":
        target = path.parent.parent / "linked"
        target.mkdir()
        _pdf(target / "hidden.pdf")
        path.symlink_to(target)
    elif kind == "broken-link":
        path.symlink_to(path.with_name("gone"))
    else:
        path.symlink_to(path.name)


# (suffix to the statements/ or tax-forms/ folder, kind)
_HIDING_ENTRIES = [
    pytest.param("/2025", "unlistable", id="unlistable-subfolder", marks=_PERMISSIONS_ENFORCED),
    pytest.param("", "unlistable", id="unlistable-folder", marks=_PERMISSIONS_ENFORCED),
    pytest.param("/2025", "linked-folder", id="linked-folder"),
    pytest.param("/2025", "broken-link", id="broken-link"),
    pytest.param("/2025", "looping-link", id="looping-link"),
]


@pytest.mark.parametrize("folder", ["statements", "tax-forms"])
@pytest.mark.parametrize(("suffix", "kind"), _HIDING_ENTRIES)
def test_entry_hiding_pdfs_refuses_naming_its_relative_path_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    folder: str,
    suffix: str,
    kind: str,
) -> None:
    entry = f"{folder}{suffix}"
    input_dir = _bundle_with_entry(tmp_path, entry, lambda path: _hide_pdfs(path, kind, request))
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
            include_tax_forms=True,
        )

    assert str(raised.value) == f"{entry} {_PROBLEMS[kind]}"
    assert calls == []


@pytest.mark.parametrize("kind", ["broken-link", "looping-link"])
def test_tax_forms_link_that_cannot_be_followed_refuses_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    kind: str,
) -> None:
    input_dir = _bundle_with_entry(
        tmp_path, "tax-forms", lambda path: _hide_pdfs(path, kind, request)
    )
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
            include_tax_forms=True,
        )

    assert str(raised.value) == "tax-forms must be a directory when present"
    assert calls == []


@_PERMISSIONS_ENFORCED
@pytest.mark.parametrize(
    ("folder", "message"),
    [
        ("statements", "statements is not a readable folder"),
        ("tax-forms", "tax-forms must be a directory when present"),
    ],
)
def test_folder_linked_into_an_unsearchable_folder_refuses_before_any_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    folder: str,
    message: str,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    root = input_dir / folder
    root.mkdir(exist_ok=True)
    _pdf(root / "hidden.pdf")
    blocked = input_dir / "blocked"
    blocked.mkdir()
    root.rename(blocked / folder)
    root.symlink_to(blocked / folder)
    blocked.chmod(0)
    request.addfinalizer(lambda: blocked.chmod(0o700))
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(RobinhoodBundleError) as raised:
        load_robinhood_bundle(
            input_dir=input_dir,
            config_path=config,
            financial_year=_FY,
            include_tax_forms=True,
        )

    assert str(raised.value) == message
    assert calls == []


# Each takes a built bundle and returns the input folder to load, the folder to
# lock, and the path the check then fails to stat.
def _bundle_in_locked_folder(input_dir: Path) -> tuple[Path, Path, Path]:
    blocked = input_dir.with_name("blocked")
    blocked.mkdir()
    moved = input_dir.rename(blocked / input_dir.name)
    return moved, blocked, moved


def _activity_linked_into_locked_folder(input_dir: Path) -> tuple[Path, Path, Path]:
    blocked = input_dir / "blocked"
    blocked.mkdir()
    activity = input_dir / "activity"
    activity.rename(blocked / "activity")
    activity.symlink_to(blocked / "activity")
    return input_dir, blocked, activity


def _locked_activity(input_dir: Path) -> tuple[Path, Path, Path]:
    activity = input_dir / "activity"
    return input_dir, activity, activity.resolve() / "one.csv"


def _selection_linked_into_locked_folder(input_dir: Path) -> tuple[Path, Path, Path]:
    blocked = input_dir / "blocked"
    blocked.mkdir()
    _pdf(blocked / "corrected.pdf")
    (input_dir / "statements" / "corrected.pdf").symlink_to(blocked / "corrected.pdf")
    _pdf(input_dir / "statements" / "original.pdf")
    return input_dir, blocked, blocked.resolve() / "corrected.pdf"


@_PERMISSIONS_ENFORCED
@pytest.mark.parametrize(
    ("lock", "selections"),
    [
        pytest.param(_bundle_in_locked_folder, (), id="input-folder"),
        pytest.param(_activity_linked_into_locked_folder, (), id="activity-folder"),
        pytest.param(_locked_activity, (), id="configured-report"),
        pytest.param(_selection_linked_into_locked_folder, (_SELECTION,), id="corrected-selection"),
    ],
)
def test_bundle_entry_checks_raise_eacces_instead_of_calling_the_entry_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    lock: Callable[[Path], tuple[Path, Path, Path]],
    selections: tuple[tuple[str, str], ...],
) -> None:
    """#1238: the input, activity, report and selection checks raise EACCES, not "missing"."""
    built, config = _corrected_bundle(tmp_path, {}, *selections)
    input_dir, locked, path = lock(built)
    locked.chmod(0)
    request.addfinalizer(lambda: locked.chmod(0o700))
    calls = _record_extractions(monkeypatch)

    with pytest.raises(PermissionError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert type(raised.value) is PermissionError
    assert str(raised.value) == f"[Errno {errno.EACCES}] Permission denied: {str(path)!r}"
    assert calls == []


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [
        *_HIDING_ENTRIES,
        pytest.param("", "broken-link", id="broken-tax-forms"),
        pytest.param("", "looping-link", id="looping-tax-forms"),
    ],
)
def test_tax_form_entries_hiding_pdfs_are_not_checked_without_tax_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    suffix: str,
    kind: str,
) -> None:
    input_dir = _bundle_with_entry(
        tmp_path, f"tax-forms{suffix}", lambda path: _hide_pdfs(path, kind, request)
    )
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        tax_form_parser,
        "pdftotext_layout",
        lambda path, **_kwargs: calls.append(f"tax-form pdftotext {path.name}"),
    )

    result = load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert result.packets == ()
    assert calls == []


def test_robinhood_income_refuses_a_linked_tax_form_folder_with_its_relative_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_dir = _bundle_with_entry(
        tmp_path, "tax-forms/2025", lambda path: _hide_pdfs(path, "linked-folder", request)
    )
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    exit_code = cli.main(
        [
            "robinhood-income",
            "--input",
            str(input_dir),
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "out"),
            "--financial-year",
            "2025-26",
            "--itr-form",
            "ITR-2",
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 4
    assert printed.err == (
        "error: tax-forms/2025 is a linked folder; replace it with a real folder\n"
    )
    assert str(tmp_path) not in printed.out + printed.err
    assert calls == []


@_PERMISSIONS_ENFORCED
def test_unlistable_statement_folder_keeps_the_input_collision_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    input_dir = _bundle_with_entry(
        tmp_path, "statements/2025", lambda path: _hide_pdfs(path, "unlistable", request)
    )
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    calls = _record_extractions(monkeypatch)

    with pytest.raises(OutputCollisionError) as raised:
        run_robinhood_options(
            input_dir=input_dir,
            config_path=config,
            output_dir=tmp_path / "out",
            financial_year=_FY,
            itr_form="ITR-2",
        )

    # The folder as listed and the errno name, not its strerror (#1048).
    assert str(raised.value) == (
        f"could not list {input_dir / 'statements' / '2025'} "
        "to check it against the output targets (EACCES)"
    )
    assert os.strerror(errno.EACCES).lower() not in str(raised.value).lower()
    assert calls == []


def test_linked_tax_forms_and_pdf_links_are_still_read_and_other_entries_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    forms = input_dir / "forms"
    elsewhere = input_dir / "elsewhere"
    forms.mkdir()
    elsewhere.mkdir()
    for path in (forms / "form.pdf", elsewhere / "linked.pdf", elsewhere / "ignored.pdf"):
        _pdf(path)
    (input_dir / "tax-forms").symlink_to(forms)
    (input_dir / "statements" / "link.pdf").symlink_to(elsewhere / "linked.pdf")
    for folder in (input_dir / "statements", forms):
        (folder / "notes.txt").write_text("notes", encoding="utf-8")
        (folder / "notes").symlink_to(elsewhere / "ignored.pdf")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    read: list[Path] = []
    monkeypatch.setattr(
        bundle_mod, "read_robinhood_statement", lambda path: read.append(path) or path
    )
    monkeypatch.setattr(bundle_mod, "read_robinhood_1042s_packet", lambda path: path)

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
        include_tax_forms=True,
    )

    resolved = input_dir.resolve()
    assert read == [resolved / "elsewhere" / "linked.pdf", resolved / "statements" / "one.pdf"]
    assert result.packets == (resolved / "forms" / "form.pdf",)


def test_missing_tax_forms_folder_means_no_tax_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    monkeypatch.setattr(bundle_mod, "read_robinhood_1042s_packet", lambda path: path)

    result = load_robinhood_bundle(
        input_dir=input_dir,
        config_path=config,
        financial_year=_FY,
        include_tax_forms=True,
    )

    assert result.packets == ()


_EXTRACTION_LABELS = {
    "doc_kind": "Robinhood statement PDFs",
    "source": "Robinhood bundle discovery",
}


def _statement_dir(tmp_path: Path, names: tuple[str, ...]) -> Path:
    statements = tmp_path / "bundle" / "statements"
    statements.mkdir(parents=True)
    for name in names:
        (statements / name).write_bytes(name.encode())
    return statements.parent


def _stub_extractor(
    monkeypatch: pytest.MonkeyPatch,
    texts: dict[str, str],
) -> list[tuple[str, tuple[object, ...], dict[str, object]]]:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def extract(path: Path, *args: object, **kwargs: object) -> str:
        calls.append((path.name, args, kwargs))
        return texts[path.name]

    monkeypatch.setattr(statement_parser, "pdftotext_layout", extract)
    return calls


_NO_CORRECTIONS = cast("RobinhoodConfig", SimpleNamespace(corrected_statements=()))


def test_statement_triage_splits_crypto_from_brokerage_with_todays_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    texts = {
        "a.pdf": _crypto_statement(),
        "b.pdf": _crypto_statement().upper(),
        "c.pdf": _brokerage_statement(),
        "d.pdf": _crypto_statement().lower(),
    }
    input_dir = _statement_dir(tmp_path, tuple(texts))
    calls = _stub_extractor(monkeypatch, texts)

    brokerage, crypto = bundle_mod._statement_paths(input_dir, _NO_CORRECTIONS)

    assert [path.name for path in brokerage] == ["c.pdf"]
    assert [path.name for path in crypto] == ["a.pdf", "b.pdf", "d.pdf"]
    assert calls == [(name, (), _EXTRACTION_LABELS) for name in sorted(texts)]


def test_statement_triage_refuses_a_bundle_of_only_crypto_statements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _statement_dir(tmp_path, ("only.pdf",))
    _stub_extractor(monkeypatch, {"only.pdf": _crypto_statement()})

    with pytest.raises(RobinhoodBundleError) as raised:
        bundle_mod._statement_paths(input_dir, _NO_CORRECTIONS)

    assert str(raised.value) == "Robinhood bundle contains no brokerage statements"


def test_every_statement_is_extracted_before_any_is_parsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_dir = _write_bundle(tmp_path / "bundle")
    (input_dir / "statements" / "a.pdf").write_bytes(b"malformed")
    (input_dir / "statements" / "b.pdf").write_bytes(b"unreadable")
    config = tmp_path / "user.toml"
    config.write_text(_config_text(), encoding="utf-8")
    _patch_sources(monkeypatch)
    extraction_error = ParseError(
        section="Robinhood bundle discovery",
        row_index=0,
        column=None,
        message="pdftotext failed",
        raw_value="",
    )
    texts = {"a.pdf": "Robinhood Securities\nnot a statement\n", "one.pdf": "Brokerage"}

    def extract(path: Path, **_kwargs: object) -> str:
        if path.name == "b.pdf":
            raise extraction_error
        return texts[path.name]

    monkeypatch.setattr(statement_parser, "pdftotext_layout", extract)
    monkeypatch.setattr(
        bundle_mod,
        "read_robinhood_statement",
        lambda path: parse_robinhood_statement(texts[path.name]),
    )

    with pytest.raises(ParseError) as raised:
        load_robinhood_bundle(input_dir=input_dir, config_path=config, financial_year=_FY)

    assert raised.value is extraction_error


def _entity_address_warnings(
    configured: tuple[FaEntityOverride, ...] = (),
    **changes: object,
) -> tuple[str, ...]:
    bundle = RobinhoodBundle(
        config=cast("UserConfig", SimpleNamespace(fa=SimpleNamespace(entities=configured))),
        robinhood=RobinhoodConfig(accounts=()),
        securities=RobinhoodSecurityIdentities(()),
        paired=cast("RobinhoodAccountsReconciliation", None),
        identities=cast("RobinhoodSecurityIdentityResult", None),
        packets=(),
        quantities=cast("RobinhoodSecurityQuantityResult", None),
        scope=bundle_mod.RobinhoodScopeStatus(crypto_statement_count=0),
    )
    return replace(bundle, **changes).entity_address_warnings


def _audit(warning: str) -> object:
    return SimpleNamespace(warnings=(warning,))


@pytest.mark.parametrize("artifact", [None, Path("/cache/identity-sources.json")])
def test_entity_address_warnings_keep_todays_order(artifact: Path | None) -> None:
    configured = FaEntityOverride(
        isin=_ISSUER_AQ,
        address="Old address",
        zip_code="19801",
        name="Old name",
        nature_of_entity="Company",
    )

    warnings = _entity_address_warnings(
        (configured,),
        automatic_entity_overrides=(_automatic(_ISSUER_AQ, name="Sample Issuer AQ Inc."),),
        automatic_nature_evidence=(_nature(_ISSUER_AQ),),
        legal_address_audit=_audit("legal-address audit"),
        nature_audit=_audit("nature audit"),
        fund_entity_audit=_audit("fund audit"),
        adr_entity_audit=_audit("ADR audit"),
        sec_entity_audit=_audit("SEC audit"),
        presentation_identity_source_audit=IdentitySourceAudit(
            "f" * 64, False, "cache integrity failed", artifact
        ),
    )

    assert warnings == (
        _ADDRESS_ONE,
        _NAME_ONE,
        _NATURE_ONE,
        "legal-address audit",
        "nature audit",
        "fund audit",
        "ADR audit",
        "SEC audit",
        # Never the artifact's path: its folder name is a fingerprint (#1044).
        "cache integrity failed",
    )


def test_entity_address_warnings_are_empty_without_evidence_or_failures() -> None:
    assert _entity_address_warnings() == ()
    assert (
        _entity_address_warnings(
            presentation_identity_source_audit=IdentitySourceAudit("f" * 64, True, None, None)
        )
        == ()
    )


@private_test
def test_private_bundle_loads_without_disclosing_values(tmp_path: Path) -> None:
    paired = load_private_accounts_reconciliation()
    statements_root, ranges = private_robinhood_dataset()
    raw = tomllib.loads(ranges.read_text(encoding="utf-8"))
    opening_by_family = {item["family"]: item["opening_date"] for item in raw["accounts"]}

    bundle_root = tmp_path / "bundle"
    activity_dir = bundle_root / "activity"
    statement_dir = bundle_root / "statements"
    activity_dir.mkdir(parents=True)
    statement_dir.mkdir()
    report_blocks: list[str] = []
    for spec in raw["reports"]:
        source = ranges.parent / spec["file"]
        target = activity_dir / Path(spec["file"]).name
        copy2(source, target)
        report_blocks.append(
            robinhood_report_toml(
                file=f"activity/{target.name}",
                start_date=spec["start_date"].isoformat(),
                end_date=spec["end_date"].isoformat(),
            )
        )
    for source in private_pdfs(statements_root):
        relative = source.relative_to(statements_root)
        target = statement_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(source, target)

    account_blocks = []
    for account in paired.accounts:
        family = account.source_bundle.family.value
        account_blocks.append(
            f"""
[[robinhood.accounts]]
number = "{account.activity.primary_bundle.account_number}"
opening_date = {opening_by_family[family].isoformat()}
status = "BeneficialOwner"
institution_name = "Robinhood Securities, LLC"
address = "private statement"
country_iso2 = "US"
same_beneficial_owner = true
source = "private statement"
"""
        )
    config = tmp_path / "user.toml"
    base_config = (
        '[account]\nholder_name = "Tax Payer"\n' + "".join(account_blocks) + "".join(report_blocks)
    )
    config.write_text(
        base_config,
        encoding="utf-8",
    )
    result = _loaded_bundle(bundle_root, config)
    if result is None:
        pytest.fail("private bundle failed to load", pytrace=False)
    if _account_bindings(result.paired) != _account_bindings(paired):
        pytest.fail("private bundle pairs reports and statements differently", pytrace=False)

    config.write_text(
        base_config
        + "".join(
            robinhood_option_classification_toml(
                account_number=str(account.activity.primary_bundle.account_number),
                treatment="capital-gains",
            )
            for account in paired.accounts
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    report = _options_outcome(bundle_root, config, output_dir)
    # Either outcome is accepted; a report must name only outputs it wrote.
    if report is not None and (
        (report.status is RobinhoodLegStatus.NOT_REQUIRED) != (not report.outputs)
        or not all(path.is_file() for path in report.outputs)
    ):
        pytest.fail("private options run reported outputs it did not write", pytrace=False)


def _loaded_bundle(bundle_root: Path, config: Path) -> RobinhoodBundle | None:
    """The loaded bundle, or ``None``; the caller fails after the ``except``
    block, so pytest prints no source exception."""
    try:
        return load_robinhood_bundle(
            input_dir=bundle_root,
            config_path=config,
            financial_year=_FY,
        )
    except Exception:
        return None


def _account_bindings(
    paired: RobinhoodAccountsReconciliation,
) -> dict[str, tuple[object, ...]]:
    """Each account's family, attested report range and statement coverage."""
    return {
        account.activity.primary_bundle.account_number: (
            account.source_bundle.family,
            account.evidence.start_date,
            account.evidence.end_date,
            account.activity.primary_bundle.coverage_start,
            account.activity.primary_bundle.coverage_end,
        )
        for account in paired.accounts
    }


def _options_outcome(
    bundle_root: Path, config: Path, output_dir: Path
) -> RobinhoodLegReport | None:
    """The options run's report, or ``None`` for a typed refusal. Any other
    exception fails after the ``except`` block, so pytest prints no source
    exception."""
    try:
        return run_robinhood_options(
            input_dir=bundle_root,
            config_path=config,
            output_dir=output_dir,
            financial_year=_FY,
            itr_form="ITR-2",
        )
    except RobinhoodPipelineError as exc:
        # The F&O warning wrapper re-raises any exception as this typed
        # error; one wrapping an untyped exception is a bug, not a refusal.
        untyped = exc.__cause__ is not None and not isinstance(exc.__cause__, IbkrFaError)
    except IbkrFaError:
        untyped = False
    except Exception:
        untyped = True
    if untyped:
        pytest.fail("private options run failed with an untyped error", pytrace=False)
    return None
