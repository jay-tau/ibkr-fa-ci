"""The stubbed Robinhood bundle that the bundle loader's tests and other
Robinhood pipeline tests run.

``bundle_config_text`` returns the bundle's ``user.toml``: the default two
accounts, the reports ``activity/one.csv`` and ``activity/two.csv``, then the
given corrected-statement and security entries, with the given ``[robinhood]``
fields. ``write_bundle`` writes those two placeholder reports and one
placeholder statement PDF; ``crypto`` makes that statement a crypto one and
adds a brokerage one, and ``extra_csv`` adds a third report.
``patch_bundle_sources`` stubs the loader's readers, checks, reconciliations
and resolvers: no activity rows, two paired stand-in accounts with no events,
and empty presentation evidence with ``presentation_source_audit`` as its
identity source audit. The statement parser's ``pdftotext_layout`` then returns
a Robinhood Crypto statement for the crypto placeholder and ``Brokerage`` for
any other. ``with_bundle_texts`` writes that bundle and the default config
under ``tmp_path``, patches its sources, makes each placeholder statement's
text ``texts[<its bytes>]`` and returns the input folder and the config path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import ibkr_fa.parser.robinhood_statement.parser as statement_parser
import ibkr_fa.pipeline_robinhood_bundle as bundle_mod
from ibkr_fa.compute.robinhood_entity_overrides import GleifLegalAddressAudit
from ibkr_fa.normalize.robinhood import (
    RobinhoodAccountsReconciliation,
    RobinhoodSecurityQuantityResult,
)
from ibkr_fa.robinhood_identity_sources import IdentitySourceAudit
from tests._robinhood_statement_pages import crypto_statement
from tests._robinhood_toml import robinhood_report_toml, robinhood_toml


@dataclass(frozen=True)
class _Bundle:
    account_number: str
    statements: tuple[object, ...] = ()


@dataclass(frozen=True)
class _Activity:
    primary_bundle: _Bundle
    cusip_transition_uses: tuple[object, ...] = ()


@dataclass(frozen=True)
class _Evidence:
    start_date: object
    end_date: object
    events: tuple[object, ...] = ()


@dataclass(frozen=True)
class _Account:
    activity: _Activity
    evidence: _Evidence


@dataclass(frozen=True)
class _Paired:
    accounts: tuple[_Account, _Account]
    cusip_transition_references: tuple[str, ...] = ()


def bundle_config_text(
    *, corrected: str = "", parent_fields: str = "", securities: str = ""
) -> str:
    return robinhood_toml(
        parent_fields=parent_fields,
        extra=(
            robinhood_report_toml(file="activity/one.csv")
            + robinhood_report_toml(file="activity/two.csv")
            + corrected
            + securities
        ),
    )


def write_bundle(tmp_path: Path, *, crypto: bool = False, extra_csv: bool = False) -> Path:
    (tmp_path / "activity").mkdir(parents=True)
    (tmp_path / "statements").mkdir()
    for name in ("one.csv", "two.csv"):
        (tmp_path / "activity" / name).write_text("placeholder", encoding="utf-8")
    if extra_csv:
        (tmp_path / "activity" / "extra.csv").write_text("placeholder", encoding="utf-8")
    (tmp_path / "statements" / "one.pdf").write_bytes(b"crypto" if crypto else b"brokerage")
    if crypto:
        (tmp_path / "statements" / "brokerage.pdf").write_bytes(b"brokerage")
    return tmp_path


def patch_bundle_sources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    presentation_source_audit: IdentitySourceAudit | None = None,
) -> None:
    paired = cast(
        "RobinhoodAccountsReconciliation",
        _Paired(
            (
                _Account(_Activity(_Bundle("100000001")), _Evidence(None, None)),
                _Account(_Activity(_Bundle("200000002")), _Evidence(None, None)),
            )
        ),
    )
    monkeypatch.setattr(bundle_mod, "read_robinhood_activity", lambda _path: ())
    monkeypatch.setattr(bundle_mod, "read_robinhood_statement", lambda path: path)
    monkeypatch.setattr(
        bundle_mod,
        "_validate_corrected_statements",
        lambda *_args: None,
    )
    monkeypatch.setattr(bundle_mod, "validate_robinhood_statement_bundles", lambda _items: ())
    monkeypatch.setattr(
        bundle_mod, "reconcile_robinhood_accounts", lambda *_args, **_kwargs: paired
    )
    monkeypatch.setattr(
        bundle_mod, "validate_robinhood_report_coverage", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_security_identities",
        lambda *_args: object(),
    )
    quantities = cast("RobinhoodSecurityQuantityResult", object())
    monkeypatch.setattr(
        bundle_mod,
        "reconcile_robinhood_security_quantities",
        lambda *_args: quantities,
    )
    monkeypatch.setattr(bundle_mod, "validate_robinhood_inventory_partition", lambda *_args: None)
    monkeypatch.setattr(
        bundle_mod,
        "resolve_robinhood_presentation_evidence",
        lambda **_kwargs: SimpleNamespace(
            entity_overrides=(),
            unverified_addresses=(),
            legal_address_audit=GleifLegalAddressAudit(0, 0, 0),
            nature_evidence=(),
            nature_audit=None,
            fund_evidence=(),
            fund_audit=None,
            adr_audit=None,
            sec_entity_evidence=(),
            sec_entity_audit=None,
            identity_source_audit=presentation_source_audit,
        ),
    )
    monkeypatch.setattr(
        statement_parser,
        "pdftotext_layout",
        lambda path, **_kwargs: (
            crypto_statement() if path.read_bytes() == b"crypto" else "Brokerage"
        ),
    )


def with_bundle_texts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, texts: dict[bytes, str]
) -> tuple[Path, Path]:
    input_dir = write_bundle(tmp_path / "bundle")
    config = tmp_path / "user.toml"
    config.write_text(bundle_config_text(), encoding="utf-8")
    patch_bundle_sources(monkeypatch)
    monkeypatch.setattr(
        statement_parser,
        "pdftotext_layout",
        lambda path, **_kwargs: texts[path.read_bytes()],
    )
    return input_dir, config
