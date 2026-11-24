"""The stubbed Robinhood address-verification run that several test modules
share (#1872), from the ``[[fa.entities]]`` address tests (#1449).

- ``ADDRESS_GUIDANCE_HEADER``: the header of that file's address section;
- ``stub_address_bundle``: makes every command load the #1243 bundle with the
  given ``[[fa.entities]]`` entries, and returns it;
- ``address_verification_run``: a command on that bundle, captured.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import ibkr_fa.pipeline_robinhood as pipeline
from ibkr_fa.compute.robinhood_identity import (
    RobinhoodEntityRoute,
    robinhood_unverified_entity_addresses,
)
from ibkr_fa.config import FaEntityOverride, RobinhoodSecurityConfig, load_user_config
from ibkr_fa.normalize.robinhood import RobinhoodSecurityIdentityResult
from ibkr_fa.pipeline_robinhood_bundle import RobinhoodBundle
from tests._fa_cli_runs import CliRun as _Run
from tests._fa_cli_runs import robinhood_run as _invoke
from tests._robinhood_a3_load import load_two_accounts
from tests._robinhood_fa_inputs import FA_ISIN as _ISIN
from tests._robinhood_fa_inputs import fa_buy as _buy
from tests._robinhood_fa_inputs import fa_entity as _entity
from tests._robinhood_security import configured_securities

_AUTOMATIC = _entity()[_ISIN]
ADDRESS_GUIDANCE_HEADER = (
    "# Private: Schedule FA Table A3 issuer addresses for [[fa.entities]] entries\n"
    "# that have no address. Nothing below has been filed. For each block, verify\n"
    "# the address against the issuer's own filings (annual report or regulatory\n"
    "# filings), uncomment its address and zip lines, correcting them if they are\n"
    "# wrong, and replace that ISIN's [[fa.entities]] entry in user.toml with the\n"
    "# block. prefer_configured_address = true files your verified address instead\n"
    "# of any automatic one. Then re-run.\n"
)


def stub_address_bundle(
    monkeypatch: pytest.MonkeyPatch,
    run_dir: Path,
    entries: str,
    *,
    automatic: tuple[FaEntityOverride, ...] = (_AUTOMATIC,),
    held_etf: bool = False,
    unconfigured: bool = False,
) -> RobinhoodBundle:
    """Make every command load the #1243 bundle (2 shares of one common stock
    held at Dec 31) with ``entries`` appended to its user.toml and loaded; return
    it. The bundle is a real :class:`RobinhoodBundle` whose automatic GLEIF
    overrides are ``automatic`` and whose unverified addresses are those
    :func:`robinhood_unverified_entity_addresses` gives for the held security
    (marked an ETF with ``held_etf``), an automatic override's nature counting
    as automatic nature evidence. With ``unconfigured`` its identity
    table leaves the security out, so identity guidance is due too."""
    run_dir.mkdir()
    stub = load_two_accounts(monkeypatch, run_dir, _buy("2"))
    config_path = run_dir / "user.toml"
    with config_path.open("a", encoding="utf-8") as handle:
        handle.write(entries)
    config = load_user_config(config_path)
    held: tuple[RobinhoodSecurityConfig, ...] = stub.robinhood.securities
    if held_etf:
        held = tuple(replace(item, sub_category="ETF") for item in held)
    bundle = RobinhoodBundle(
        config=config,
        robinhood=stub.robinhood,
        securities=(
            configured_securities(replace(stub.robinhood, securities=()))
            if unconfigured
            else stub.securities
        ),
        paired=stub.paired,
        identities=cast("RobinhoodSecurityIdentityResult", stub.identities),
        packets=stub.packets,
        quantities=stub.quantities,
        scope=stub.scope,
        automatic_entity_overrides=automatic,
        unverified_entity_addresses=robinhood_unverified_entity_addresses(
            config.fa.entities,
            held,
            automatic,
            {item.isin: RobinhoodEntityRoute.LEGAL for item in automatic},
            {item.isin for item in automatic if item.nature_of_entity is not None},
        ),
    )
    monkeypatch.setattr(pipeline, "_load", lambda **_kwargs: bundle)
    return bundle


def address_verification_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    entries: str,
    command: str,
    *extra: str,
    automatic: tuple[FaEntityOverride, ...] = (_AUTOMATIC,),
    held_etf: bool = False,
) -> _Run:
    """``command`` on :func:`stub_address_bundle`'s bundle in ``tmp_path / label``."""
    run_dir = tmp_path / label
    stub_address_bundle(monkeypatch, run_dir, entries, automatic=automatic, held_etf=held_etf)
    return _invoke(run_dir, capsys, command, *extra)
